"""Reliability wrapper around the tools: validation, budget, timeout, bounded retry with
backoff, circuit breaker, and an append-only call log that later serves as the evidence
ledger for grounding checks.  Agents never call tool implementations directly."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import ValidationError

from ..config import Budget
from .base import RETRYABLE, TOOL_SPECS, ToolCallRecord, ToolError, ToolResult
from .datastore import DataStore
from .faults import MESSAGES, FaultInjector
from .impl import IMPLS, ToolUserError


class ToolExecutor:
    def __init__(self, store: DataStore, injector: FaultInjector | None = None, budget: Budget | None = None,
                 sim_latency_s: float = 0.01, sim_timeout_s: float = 0.05):
        self.store, self.injector, self.budget = store, injector or FaultInjector(), budget or Budget()
        self.sim_latency_s, self.sim_timeout_s = sim_latency_s, sim_timeout_s
        self.calls: list[ToolCallRecord] = []
        self._seq = 0
        self._consecutive_failures: dict[str, int] = {}
        self.total_attempts = 0

    # -- public
    @property
    def logical_calls(self) -> int:
        return len(self.calls)

    @property
    def started_calls(self) -> int:
        """Calls begun, including those still running; this is the count the budget is enforced on."""
        return self._seq

    def record(self, call_id: str) -> ToolCallRecord | None:
        return next((c for c in self.calls if c.call_id == call_id), None)

    async def call(self, agent: str, tool: str, raw_args: dict[str, Any]) -> ToolResult:
        self._seq += 1
        call_id = f"tc-{self._seq:03d}"
        t0 = time.perf_counter()
        result = await self._run(call_id, tool, raw_args)
        result.latency_ms = int((time.perf_counter() - t0) * 1000)
        self.calls.append(ToolCallRecord(
            call_id=call_id, agent=agent, tool=tool, args=raw_args, ok=result.ok, partial=result.partial,
            error_type=result.error.type if result.error else None, attempts=result.attempts,
            latency_ms=result.latency_ms, output=result.for_model()))
        return result

    # -- internals
    def _fail(self, call_id: str, tool: str, type_: str, msg: str, attempts: int = 0, **details: Any) -> ToolResult:
        return ToolResult(call_id=call_id, tool=tool, ok=False, attempts=attempts,
                          error=ToolError(type=type_, message=msg, retryable=type_ in RETRYABLE, details=details))  # type: ignore[arg-type]

    async def _run(self, call_id: str, tool: str, raw_args: dict[str, Any]) -> ToolResult:
        spec = TOOL_SPECS.get(tool)
        if spec is None:
            return self._fail(call_id, tool, "invalid_arguments", f"unknown tool '{tool}'", available=list(TOOL_SPECS))
        if self._seq > self.budget.max_tool_calls:
            return self._fail(call_id, tool, "budget_exhausted",
                              "investigation tool-call budget exhausted; conclude with the evidence you have")
        if self._consecutive_failures.get(tool, 0) >= self.budget.breaker_threshold:
            return self._fail(call_id, tool, "circuit_open",
                              f"{tool} failed {self.budget.breaker_threshold} times in a row; circuit open -- "
                              "treat this data source as unavailable and record the gap")
        try:
            inp = spec.input_model(**raw_args)
        except (ValidationError, TypeError) as ex:
            msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in ex.errors()) \
                if isinstance(ex, ValidationError) else str(ex)
            return self._fail(call_id, tool, "invalid_arguments", msg)

        args = inp.model_dump()
        last: ToolResult | None = None
        for attempt in range(1, self.budget.tool_max_retries + 2):
            self.total_attempts += 1
            last = await self._attempt(call_id, tool, inp, args, attempt)
            if last.ok or not (last.error and last.error.retryable):
                break
            if attempt <= self.budget.tool_max_retries:
                await asyncio.sleep(self.budget.tool_backoff_s * 2 ** (attempt - 1))   # exponential backoff
        assert last is not None
        if last.ok or (last.error and last.error.type in ("invalid_arguments", "not_found")):
            self._consecutive_failures[tool] = 0          # caller errors don't trip the breaker
        else:
            self._consecutive_failures[tool] = self._consecutive_failures.get(tool, 0) + 1
            if last.error:
                last.error.message += f" (gave up after {last.attempts} attempts)"
        return last

    async def _attempt(self, call_id: str, tool: str, inp: Any, args: dict[str, Any], attempt: int) -> ToolResult:
        fault = self.injector.pick(tool, args, attempt)

        async def work() -> dict[str, Any]:
            if fault and fault.kind == "timeout":
                await asyncio.sleep(self.sim_timeout_s)
                raise asyncio.TimeoutError
            await asyncio.sleep(self.sim_latency_s)
            if fault and fault.kind in ("unavailable", "rate_limited"):
                raise ConnectionError(fault.kind)
            return IMPLS[tool](self.store, inp)

        try:
            data = await asyncio.wait_for(work(), timeout=self.budget.tool_timeout_s)
        except asyncio.TimeoutError:
            return self._fail(call_id, tool, "timeout", MESSAGES["timeout"], attempt)
        except ConnectionError as ex:
            kind = str(ex)
            return self._fail(call_id, tool, kind, MESSAGES[kind], attempt)
        except ToolUserError as ex:
            return self._fail(call_id, tool, ex.type, str(ex), attempt, **ex.details)
        except Exception as ex:  # noqa: BLE001 -- a tool bug must never crash the investigation
            return self._fail(call_id, tool, "internal", f"{type(ex).__name__}: {ex}", attempt)

        if fault and fault.kind == "partial":
            data, warning = self.injector.degrade(tool, data, fault)
            return ToolResult(call_id=call_id, tool=tool, ok=True, data=data, partial=True,
                              warnings=[warning], attempts=attempt)
        return ToolResult(call_id=call_id, tool=tool, ok=True, data=data, attempts=attempt)
