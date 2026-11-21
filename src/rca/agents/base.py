"""The one agent loop every agent uses: LLM turn -> (parallel tool calls) -> ... -> a
schema-validated `submit` call.  Bounded by steps, tool calls, repairs and the global budget."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..llm.base import LLMClient, LLMError, function_tool
from ..state import BudgetExceeded, InvestigationState
from ..tools.base import TOOL_SPECS

T = TypeVar("T", bound=BaseModel)
SUBMIT = "submit"


class AgentError(Exception):
    """The agent could not produce a valid structured result within its limits."""


@dataclass
class AgentFaults:
    """Test/chaos hook: make an agent role crash its first N invocations."""
    crash: dict[str, int] = field(default_factory=dict)
    seen: dict[str, int] = field(default_factory=dict)

    def maybe_crash(self, role: str) -> None:
        n = self.seen.get(role, 0)
        self.seen[role] = n + 1
        if n < self.crash.get(role, 0):
            raise AgentError(f"injected crash in {role} agent (invocation {n + 1})")


@dataclass
class AgentRuntime:
    llm: LLMClient
    state: InvestigationState
    faults: AgentFaults = field(default_factory=AgentFaults)


def _json_from_text(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None


async def run_agent(rt: AgentRuntime, role: str, system: str, user: str, output_model: type[T], *,
                    tools: list[str] | None = None, max_steps: int = 1, max_tool_calls: int = 12,
                    meta: dict[str, Any] | None = None) -> T:
    """Run one agent to completion and return its validated structured output."""
    task_id = getattr((meta or {}).get("task"), "id", None)
    rt.state.emit("agent_start", agent=role, task=task_id, phase=(meta or {}).get("phase"))
    ok = False
    try:
        out = await _run_agent(rt, role, system, user, output_model, tools=tools, max_steps=max_steps,
                               max_tool_calls=max_tool_calls, meta=meta, task_id=task_id)
        ok = True
        return out
    finally:
        rt.state.emit("agent_end", agent=role, task=task_id, ok=ok)


async def _run_agent(rt: AgentRuntime, role: str, system: str, user: str, output_model: type[T], *,
                     tools: list[str] | None, max_steps: int, max_tool_calls: int,
                     meta: dict[str, Any] | None, task_id: str | None) -> T:
    st = rt.state
    rt.faults.maybe_crash(role)
    submit_tool = function_tool(SUBMIT, f"Submit your final structured result ({output_model.__name__}). Call exactly once, when done.",
                                output_model.model_json_schema())
    data_tools = [TOOL_SPECS[t].schema() for t in (tools or [])]
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    repairs = tool_calls_made = 0
    notified = False
    total_steps = max_steps + st.budget.llm_max_repairs

    for step in range(total_steps):
        if role != "reporting":          # the final report is always allowed one call
            st.check_budget()
        last_chance = step >= max_steps - 1 or tool_calls_made >= max_tool_calls or st.tool_calls_left <= 0
        offered = [submit_tool] if (last_chance or not data_tools) else data_tools + [submit_tool]
        if last_chance and data_tools and step > 0 and not notified:
            notified = True
            messages.append({"role": "user", "content":
                             "Investigation budget for this task is used up. Call `submit` now with what you have; "
                             "record anything you could not check as a gap."})
        try:
            resp = await rt.llm.complete(messages, offered, role=role, meta=meta)
        except LLMError as ex:
            raise AgentError(f"{role}: {ex}") from ex
        st.add_usage(role, resp.prompt_tokens, resp.completion_tokens)
        st.emit("llm_turn", agent=role, task=task_id, step=step + 1, tool_calls=[c.name for c in resp.tool_calls],
                prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens)
        messages.append(resp.raw_message or {"role": "assistant", "content": resp.content})

        if not resp.tool_calls:                                   # model answered in prose
            parsed = _json_from_text(resp.content)
            if parsed is not None:
                try:
                    return output_model(**parsed)
                except ValidationError:
                    pass
            repairs += 1
            if repairs > st.budget.llm_max_repairs:
                raise AgentError(f"{role}: no valid structured output after {repairs} attempts")
            messages.append({"role": "user", "content": "Respond only by calling a tool. When finished, call `submit`."})
            continue

        submits = [c for c in resp.tool_calls if c.name == SUBMIT]
        others = [c for c in resp.tool_calls if c.name != SUBMIT]

        # Independent tool calls of one turn run concurrently.
        async def run_one(c: Any) -> tuple[Any, str]:
            if c.parse_error:
                return c, json.dumps({"ok": False, "error": {"type": "invalid_arguments", "message": c.parse_error}})
            if c.name not in (tools or []):
                return c, json.dumps({"ok": False, "error": {"type": "invalid_arguments",
                                                             "message": f"tool '{c.name}' is not available to the {role} agent"}})
            res = await st.executor.call(role, c.name, c.arguments)
            st.emit("tool_call", agent=role, task=task_id, call_id=res.call_id, tool=c.name, ok=res.ok, partial=res.partial,
                    attempts=res.attempts, error=res.error.type if res.error else None, args=c.arguments)
            return c, res.for_model()

        if others:
            allowed = max(0, max_tool_calls - tool_calls_made)          # per-task cap also applies within one turn
            run_now, refused = others[:allowed], others[allowed:]
            tool_calls_made += len(run_now)
            for c, out in await asyncio.gather(*(run_one(c) for c in run_now)):
                messages.append({"role": "tool", "tool_call_id": c.id, "content": out})
            for c in refused:
                messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps({"ok": False, "error": {
                    "type": "budget_exhausted", "message": "not executed: this task's tool-call limit is reached"}})})

        if submits:
            c = submits[0]
            if others:      # submitted before seeing the results it just asked for
                messages.append({"role": "tool", "tool_call_id": c.id, "content":
                                 "submit ignored: you requested data in the same turn. Read the results, then submit."})
            else:
                try:
                    if c.parse_error:
                        raise ValueError(c.parse_error)
                    return output_model(**c.arguments)
                except (ValidationError, ValueError) as ex:
                    repairs += 1
                    if repairs > st.budget.llm_max_repairs:
                        raise AgentError(f"{role}: submit failed schema validation {repairs} times: {str(ex)[:300]}") from ex
                    st.emit("repair", agent=role, error=str(ex)[:200])
                    messages.append({"role": "tool", "tool_call_id": c.id, "content":
                                     f"submit rejected, schema validation failed:\n{str(ex)[:1200]}\nFix and call submit again."})
            for extra in submits[1:]:
                messages.append({"role": "tool", "tool_call_id": extra.id, "content": "ignored duplicate submit"})

    raise AgentError(f"{role}: step limit ({total_steps}) reached without a valid submit")


__all__ = ["AgentRuntime", "AgentFaults", "AgentError", "BudgetExceeded", "run_agent"]
