"""Simulated tool failures.

A fault spec (data/incidents/<id>/faults.json) looks like:
    {"tool": "query_logs", "match": {"service": "ledger-db"}, "kind": "timeout", "times": 2}
kind  : timeout | unavailable | rate_limited | partial
times : number of matching *attempts* that fail before the tool works again; null = permanent.
partial faults return data with items removed (`drop_ids`, else the second half) and partial=true.

`chaos_rate` adds seeded random transient faults on top (deterministic per call signature, so
parallel scheduling order does not change which calls fail).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .impl import LIST_KEY


@dataclass
class Fault:
    tool: str
    kind: str
    match: dict[str, Any] = field(default_factory=dict)
    times: Optional[int] = None
    drop_ids: list[str] = field(default_factory=list)
    detail: str = ""
    fired: int = 0

    def applies(self, tool: str, args: dict[str, Any]) -> bool:
        if tool != self.tool or (self.times is not None and self.fired >= self.times):
            return False
        return all(args.get(k) == v for k, v in self.match.items())


MESSAGES = {
    "timeout": "upstream query timed out",
    "unavailable": "backend unavailable (503)",
    "rate_limited": "rate limit exceeded (429)",
}


class FaultInjector:
    def __init__(self, specs: list[dict[str, Any]] | None = None, chaos_rate: float = 0.0, seed: int = 0,
                 chaos_hard_rate: float = 0.0):
        self.faults = [Fault(**{k: v for k, v in s.items() if k in Fault.__dataclass_fields__}) for s in (specs or [])]
        self.chaos_rate, self.seed = chaos_rate, seed
        self.chaos_hard_rate = chaos_hard_rate      # share of distinct calls that fail on every attempt
        self.injected: list[dict[str, Any]] = []

    def _chaos(self, tool: str, args: dict[str, Any], attempt: int) -> Optional[str]:
        if self.chaos_rate <= 0 and self.chaos_hard_rate <= 0:
            return None
        h = hashlib.sha256(f"{self.seed}|{tool}|{json.dumps(args, sort_keys=True)}".encode()).digest()
        # Hard faults: this exact call (tool + arguments) fails on every attempt, so the executor's
        # retries cannot hide it. The agent has to ask differently or record a gap.
        if h[2] / 255 < self.chaos_hard_rate:
            return ("timeout", "unavailable")[h[3] % 2]
        if self.chaos_rate <= 0 or attempt > 1:      # soft chaos faults are transient: a retry succeeds
            return None
        if h[0] / 255 < self.chaos_rate:
            return ("timeout", "unavailable", "rate_limited")[h[1] % 3]
        return None

    def pick(self, tool: str, args: dict[str, Any], attempt: int) -> Optional[Fault]:
        for f in self.faults:
            if f.applies(tool, args):
                f.fired += 1
                self.injected.append({"tool": tool, "kind": f.kind, "attempt": attempt, "scripted": True})
                return f
        kind = self._chaos(tool, args, attempt)
        if kind:
            self.injected.append({"tool": tool, "kind": kind, "attempt": attempt, "scripted": False})
            return Fault(tool=tool, kind=kind, times=1)
        return None

    @staticmethod
    def degrade(tool: str, data: dict[str, Any], fault: Fault) -> tuple[dict[str, Any], str]:
        """Apply a 'partial' fault to successful output."""
        key = LIST_KEY[tool]
        items = list(data.get(key, []))
        if fault.drop_ids:
            kept = [x for x in items if x.get("id") not in fault.drop_ids]
        else:
            kept = items[: max(1, len(items) // 2)] if items else items
        out = {**data, key: kept}
        if "count" in out:
            out["count"] = len(kept)
        if tool == "query_logs":                      # aggregations come from the shard that failed
            out.update(returned=len(kept), top_patterns=[], counts_by_level={}, total_matched=None, truncated=True)
        elif tool == "query_metrics":                 # the summary covered the whole window, including the dropped points
            out["summary"] = None
        elif tool == "get_service_dependencies":      # do not describe nodes whose edges were dropped
            names = {(out.get("service") or {}).get("name")} | {e.get(k) for e in kept for k in ("from", "to")}
            out["nodes"] = [n for n in out.get("nodes", []) if n.get("name") in names]
        warning = fault.detail or "INCOMPLETE RESULT: one or more backend shards did not respond; items may be missing. Retry to get the full result."
        return out, warning
