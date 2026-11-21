"""Shared investigation state (the blackboard).

All agents read from and write to this object; none of them talks to another agent directly.
Writes go through methods that assign ids and validate references, so the state is always
internally consistent regardless of what an LLM emitted.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from .config import Budget
from .grounding import verify_finding
from .models import (Critique, Finding, FindingIn, Gap, Hypothesis, HypothesisIn, IncidentReport, Task)
from .tools.executor import ToolExecutor


class BudgetExceeded(Exception):
    pass


class InvestigationState:
    def __init__(self, incident: IncidentReport, executor: ToolExecutor, budget: Budget, verbose: bool = False):
        self.incident, self.executor, self.budget, self.verbose = incident, executor, budget, verbose
        self.findings: dict[str, Finding] = {}
        self.gaps: list[Gap] = []
        self.hypotheses: dict[str, Hypothesis] = {}
        self.critiques: list[Critique] = []
        self.tasks: dict[str, Task] = {}
        self.theories: list[str] = []
        self.open_questions: list[str] = []
        self.warnings: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.on_event: Optional[Callable[[dict[str, Any]], None]] = None   # live listener (the web UI stream)
        self.usage: dict[str, dict[str, int]] = {}
        self.round = 1
        self.termination_reason = ""
        self._t0 = time.perf_counter()

    # ------------------------------------------------------------------ trace / budget
    def emit(self, kind: str, **data: Any) -> None:
        ev = {"t": round(self.elapsed, 3), "kind": kind, **data}
        self.events.append(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:  # noqa: BLE001 -- a broken listener must never affect the investigation
                pass
        if self.verbose:
            detail = " ".join(f"{k}={str(v)[:140]}" for k, v in data.items())
            print(f"[{ev['t']:7.2f}s] {kind:<18} {detail}", flush=True)

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._t0

    def add_usage(self, role: str, prompt: int, completion: int) -> None:
        u = self.usage.setdefault(role, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        u["calls"] += 1
        u["prompt_tokens"] += prompt
        u["completion_tokens"] += completion

    @property
    def llm_calls(self) -> int:
        return sum(u["calls"] for u in self.usage.values())

    @property
    def total_tokens(self) -> int:
        return sum(u["prompt_tokens"] + u["completion_tokens"] for u in self.usage.values())

    def budget_problem(self) -> Optional[str]:
        b = self.budget
        if self.llm_calls >= b.max_llm_calls:
            return "max_llm_calls"
        if self.total_tokens >= b.max_total_tokens:
            return "max_total_tokens"
        if self.elapsed >= b.deadline_s:
            return "deadline"
        return None

    def check_budget(self) -> None:
        p = self.budget_problem()
        if p:
            raise BudgetExceeded(p)

    @property
    def tool_calls_left(self) -> int:
        # started, not finished, calls: the executor refuses on that count, and calls still in flight
        # in a parallel task must not be offered to another agent a second time
        return max(0, self.budget.max_tool_calls - self.executor.started_calls)

    # ------------------------------------------------------------------ writes
    def add_findings(self, agent: str, task_id: str | None, items: list[FindingIn]) -> list[str]:
        ids = []
        for f in items:
            fid = f"F{len(self.findings) + 1}"
            finding = verify_finding(fid, agent, task_id, f, self.executor.record)
            self.findings[fid] = finding
            ids.append(fid)
            self.emit("finding", id=fid, agent=agent, task=task_id, verified=finding.verified, statement=f.statement)
        return ids

    def add_gap(self, gap: Gap, agent: str) -> None:
        gap.agent = agent
        self.gaps.append(gap)
        self.emit("gap", agent=agent, description=gap.description)

    def add_task(self, agent: str, objective: str, depends_on: list[str], origin: str) -> Task:
        t = Task(id=f"T{len(self.tasks) + 1}", agent=agent, objective=objective, depends_on=depends_on,  # type: ignore[arg-type]
                 origin=origin, round=self.round)  # type: ignore[arg-type]
        self.tasks[t.id] = t
        self.emit("task_created", id=t.id, agent=agent, origin=origin, depends_on=depends_on, objective=objective)
        return t

    def _clean_ids(self, ids: list[str], where: str) -> list[str]:
        good = [i for i in dict.fromkeys(x.strip() for x in ids) if i in self.findings]
        bad = [i for i in ids if i.strip() not in self.findings]
        if bad:
            self.warnings.append(f"{where}: dropped citations to non-existent findings {bad}")
        return good

    def upsert_hypotheses(self, items: list[HypothesisIn]) -> None:
        revised: set[str] = set()
        known = set(self.hypotheses)        # only ids that existed before this batch can be revised
        for h in items:
            given = (h.id or "").strip().upper()        # ' h1' must revise H1, not create a duplicate of it
            if given in known and given not in revised:
                hid = given
            else:
                # New, or an id the model reused inside this batch (a second 'H1', or an id that this
                # batch itself just created): give it a fresh id so it cannot overwrite a competitor.
                hid = f"H{len(self.hypotheses) + 1}"
                if given in revised:
                    self.warnings.append(f"reasoning used hypothesis id {given} twice in round {self.round}; "
                                         f"the second one ('{h.title[:60]}') was stored as {hid}")
            revised.add(hid)
            data = h.model_dump()
            data["id"] = hid
            data["supporting"] = self._clean_ids(h.supporting, hid)
            data["contradicting"] = self._clean_ids(h.contradicting, hid)
            for step in data["causal_chain"]:
                step["finding_ids"] = self._clean_ids(step["finding_ids"], f"{hid} causal chain")
            prev = self.hypotheses.get(hid)
            history = (prev.history if prev else []) + [{"round": self.round, "confidence": h.confidence, "status": h.status}]
            self.hypotheses[hid] = Hypothesis(**data, history=history)
            self.emit("hypothesis", id=hid, confidence=h.confidence, status=h.status, title=h.title)
        stale = [i for i, h in self.hypotheses.items() if i not in revised and h.status == "active"]
        if stale:       # left out of the revision: still ranked, at a confidence that predates the newest evidence
            self.warnings.append(f"reasoning did not revisit active hypotheses {stale} in round {self.round}; "
                                 "their confidence predates the latest evidence")

    # ------------------------------------------------------------------ reads
    def ranked(self) -> list[Hypothesis]:
        return sorted(self.hypotheses.values(), key=lambda h: (h.status != "active", -h.confidence))

    def findings_digest(self, ids: list[str] | None = None) -> str:
        rows = []
        for f in self.findings.values():
            if ids is not None and f.id not in ids:
                continue
            tag = ("VERIFIED" if f.verified else "UNVERIFIED") + f"/{f.kind}"
            rows.append(f"- {f.id} [{tag}] ({f.agent}{', ' + f.time if f.time else ''}) {f.statement}"
                        + (f"  refs: {', '.join(r.call_id for r in f.refs)}" if f.refs else ""))
        return "\n".join(rows) or "(none)"

    def gaps_digest(self) -> str:
        return "\n".join(f"- ({g.agent}) {g.description}" + (f" -> impact: {g.impact}" if g.impact else "")
                         for g in self.gaps) or "(none)"

    def hypotheses_digest(self) -> str:
        rows = []
        for h in self.ranked():
            chain = " -> ".join(f"{s.claim} [{','.join(s.finding_ids) or 'ASSUMED'}]" for s in h.causal_chain)
            rows.append(
                f"- {h.id} [{h.status}, confidence {h.confidence:.2f}] {h.title}\n"
                f"    category={h.category.value} service={h.root_cause_service} trigger={h.trigger_change_id}\n"
                f"    statement: {h.statement}\n    causal chain: {chain or '(none)'}\n"
                f"    supporting: {h.supporting}  contradicting: {h.contradicting}\n"
                f"    assumptions: {h.assumptions}" + (f"\n    rejected because: {h.rejection_reason}" if h.rejection_reason else ""))
        return "\n".join(rows) or "(none)"

    def tasks_digest(self) -> str:
        return "\n".join(f"- {t.id} [{t.status}, round {t.round}, {t.agent}] {t.objective}"
                         + (f"\n    answer: {t.answer}" if t.answer else "") + (f"\n    error: {t.error}" if t.error else "")
                         for t in self.tasks.values()) or "(none)"

    def tool_health_digest(self) -> str:
        bad = [c for c in self.executor.calls if not c.ok or c.partial or c.attempts > 1]
        return "\n".join(
            f"- {c.call_id} {c.tool}({', '.join(f'{k}={v}' for k, v in c.args.items() if k in ('service', 'metric'))}) "
            f"by {c.agent}: " + ("FAILED " + str(c.error_type) if not c.ok else "PARTIAL result" if c.partial else f"ok after {c.attempts} attempts")
            for c in bad) or "(all tool calls succeeded on first attempt)"

    def stats(self) -> dict[str, Any]:
        calls = self.executor.calls
        return {
            "latency_s": round(self.elapsed, 2),
            "llm_calls": self.llm_calls,
            "prompt_tokens": sum(u["prompt_tokens"] for u in self.usage.values()),
            "completion_tokens": sum(u["completion_tokens"] for u in self.usage.values()),
            "total_tokens": self.total_tokens,
            "tool_calls": len(calls),
            "tool_attempts": self.executor.total_attempts,
            "tool_failures": sum(1 for c in calls if not c.ok),
            "tool_partial": sum(1 for c in calls if c.partial),
            "tool_recovered_by_retry": sum(1 for c in calls if c.ok and c.attempts > 1),
            "rounds": self.round,
            "tasks": len(self.tasks),
            "tasks_failed": sum(1 for t in self.tasks.values() if t.status == "failed"),
            "findings": len(self.findings),
            "findings_verified": sum(1 for f in self.findings.values() if f.verified),
            "usage_by_agent": self.usage,
        }
