"""The six query tools.  Pure functions of (DataStore, validated input) -> dict."""
from __future__ import annotations

import re
from collections import Counter, deque
from datetime import timedelta
from typing import Any, Callable

from pydantic import BaseModel

from .base import (GetConfigChangesInput, GetDeploymentsInput, GetServiceDependenciesInput, QueryLogsInput,
                   QueryMetricsInput, SearchHistoricalIncidentsInput)
from .datastore import DataStore, fmt_ts, parse_ts

LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}


class ToolUserError(Exception):
    """Raised for caller mistakes; surfaced to the agent as a structured, non-retryable error."""

    def __init__(self, type_: str, message: str, **details: Any):
        super().__init__(message)
        self.type, self.details = type_, details


def _window(inp: Any) -> tuple[Any, Any]:
    try:
        s, e = parse_ts(inp.start), parse_ts(inp.end)
    except ValueError as ex:
        raise ToolUserError("invalid_arguments", f"bad timestamp: {ex}") from ex
    if e <= s:
        raise ToolUserError("invalid_arguments", "end must be after start")
    return s, e


def _require_telemetry(ds: DataStore, service: str) -> None:
    node = ds.node(service)
    if node is None:
        raise ToolUserError("not_found", f"unknown service '{service}'", known_services=ds.services())
    if not node.get("has_telemetry", True):
        raise ToolUserError(
            "not_found",
            f"'{service}' is an external dependency: we have no logs/metrics for it. Observe it through its callers.",
            callers=[e["from"] for e in ds.topology["edges"] if e["to"] == service])


_NUM = re.compile(r"\b[0-9a-f]{8,}\b|\d+(\.\d+)?")


def _pattern(msg: str) -> str:
    return _NUM.sub("<n>", msg)[:110]


def query_logs(ds: DataStore, inp: QueryLogsInput) -> dict[str, Any]:
    s, e = _window(inp)
    _require_telemetry(ds, inp.service)
    lvl = LEVELS[inp.min_level]
    needle = inp.contains.lower() if inp.contains else None
    hits = [x for x in ds.logs if x["service"] == inp.service and s <= x["_t"] <= e
            and LEVELS.get(x["level"], 1) >= lvl and (needle is None or needle in x["message"].lower())]
    counts = Counter(x["level"] for x in hits)
    pats: dict[str, dict[str, Any]] = {}
    for x in hits:
        p = pats.setdefault(_pattern(x["message"]), {"count": 0, "first_seen": x["ts"], "level": x["level"]})
        p["count"] += 1
    top = sorted(({"pattern": k, **v} for k, v in pats.items()), key=lambda d: -d["count"])[:6]
    ordered = hits if inp.order == "asc" else hits[::-1]
    shown = ordered[: inp.limit]
    out = {
        "service": inp.service, "total_matched": len(hits), "returned": len(shown),
        "truncated": len(hits) > len(shown), "counts_by_level": dict(counts),
        "first_match": hits[0]["ts"] if hits else None, "last_match": hits[-1]["ts"] if hits else None,
        "top_patterns": top,
        "entries": [{"ts": x["ts"], "level": x["level"], "message": x["message"]} for x in shown],
    }
    if not hits:
        out["data_range"] = ds.data_range
    return out


def query_metrics(ds: DataStore, inp: QueryMetricsInput) -> dict[str, Any]:
    s, e = _window(inp)
    _require_telemetry(ds, inp.service)
    m = ds.metrics
    series = m["series"].get(inp.service, {})
    if inp.metric not in series:
        raise ToolUserError("not_found", f"service '{inp.service}' has no metric '{inp.metric}'",
                            available_metrics=sorted(series))
    step, t0 = m["step_s"], parse_ts(m["start"])
    vals = series[inp.metric]
    i0 = max(0, int((s - t0).total_seconds() // step))
    i1 = min(len(vals) - 1, int((e - t0).total_seconds() // step))
    base = {"service": inp.service, "metric": inp.metric, "unit": m.get("units", {}).get(inp.metric, ""),
            "available_metrics": sorted(series)}
    if i1 < i0:
        return {**base, "points": [], "summary": None, "data_range": ds.data_range}
    window = vals[i0: i1 + 1]
    n = len(window)
    bucket = max(1, -(-n // inp.max_points))
    points = []
    for b in range(0, n, bucket):
        chunk = [v for v in window[b: b + bucket] if v is not None]
        ts = fmt_ts(t0 + timedelta(seconds=step * (i0 + b)))
        points.append({"ts": ts, "avg": round(sum(chunk) / len(chunk), 2) if chunk else None,
                       "max": round(max(chunk), 2) if chunk else None})
    present = [v for v in window if v is not None]
    summary = None
    if present:
        summary = {"min": round(min(present), 2), "max": round(max(present), 2),
                   "mean": round(sum(present) / len(present), 2), "first": round(present[0], 2),
                   "last": round(present[-1], 2), "missing_points": n - len(present), "total_points": n}
    else:
        summary = {"missing_points": n, "total_points": n}
    return {**base, "bucket_seconds": bucket * step, "points": points, "summary": summary}


def _in_window(items: list[dict], key: str, s: Any, e: Any, service: str | None) -> list[dict]:
    return sorted((x for x in items if s <= parse_ts(x[key]) <= e and (service is None or x["service"] == service)),
                  key=lambda x: x[key])


def get_deployments(ds: DataStore, inp: GetDeploymentsInput) -> dict[str, Any]:
    s, e = _window(inp)
    if inp.service and ds.node(inp.service) is None:
        raise ToolUserError("not_found", f"unknown service '{inp.service}'", known_services=ds.services())
    items = _in_window(ds.deployments, "deployed_at", s, e, inp.service)
    return {"count": len(items), "deployments": items, "scope": inp.service or "all services"}


def get_config_changes(ds: DataStore, inp: GetConfigChangesInput) -> dict[str, Any]:
    s, e = _window(inp)
    if inp.service and ds.node(inp.service) is None:
        raise ToolUserError("not_found", f"unknown service '{inp.service}'", known_services=ds.services())
    items = _in_window(ds.config_changes, "changed_at", s, e, inp.service)
    return {"count": len(items), "changes": items, "scope": inp.service or "all services"}


def get_service_dependencies(ds: DataStore, inp: GetServiceDependenciesInput) -> dict[str, Any]:
    node = ds.node(inp.service)
    if node is None:
        raise ToolUserError("not_found", f"unknown service '{inp.service}'", known_services=ds.services())
    edges_all = ds.topology["edges"]
    seen_edges: list[dict] = []
    seen_nodes = {inp.service}

    def walk(direction: str) -> None:
        q = deque([(inp.service, 0)])
        visited = {inp.service}
        while q:
            cur, d = q.popleft()
            if d >= inp.depth:
                continue
            for ed in edges_all:
                nxt = ed["to"] if direction == "downstream" and ed["from"] == cur else \
                    ed["from"] if direction == "upstream" and ed["to"] == cur else None
                if nxt is None:
                    continue
                if ed not in seen_edges:
                    seen_edges.append(ed)
                seen_nodes.add(nxt)
                if nxt not in visited:
                    visited.add(nxt)
                    q.append((nxt, d + 1))

    for direction in (["downstream", "upstream"] if inp.direction == "both" else [inp.direction]):
        walk(direction)
    nodes = [n for n in ds.topology["nodes"] if n["name"] in seen_nodes]
    return {"service": node, "edges": seen_edges, "nodes": nodes,
            "note": "edge direction is caller -> callee; 'shared' datastores are used by several services"}


_WORD = re.compile(r"[a-z0-9_]+")
_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "is", "with", "service", "errors", "error"}


def search_historical_incidents(ds: DataStore, inp: SearchHistoricalIncidentsInput) -> dict[str, Any]:
    q = {w for w in _WORD.findall(inp.query.lower()) if w not in _STOP}
    scored = []
    for inc in ds.history:
        if inp.service and inp.service not in inc["services"]:
            continue
        text = " ".join([inc["title"], " ".join(inc["symptoms"]), inc["root_cause"], " ".join(inc["services"])]).lower()
        words = set(_WORD.findall(text))
        score = len(q & words) + (0.5 if inp.service else 0)
        if score > 0:
            scored.append((score, inc))
    scored.sort(key=lambda t: (-t[0], t[1]["id"]))
    return {"count": len(scored[: inp.limit]),
            "incidents": [{**inc, "match_score": sc} for sc, inc in scored[: inp.limit]],
            "note": "past incidents are leads, not evidence about the current incident"}


IMPLS: dict[str, Callable[[DataStore, BaseModel], dict[str, Any]]] = {
    "query_logs": query_logs, "query_metrics": query_metrics, "get_deployments": get_deployments,
    "get_config_changes": get_config_changes, "get_service_dependencies": get_service_dependencies,
    "search_historical_incidents": search_historical_incidents,
}
# Which list inside `data` is truncated when a 'partial' fault fires.
LIST_KEY = {"query_logs": "entries", "query_metrics": "points", "get_deployments": "deployments",
            "get_config_changes": "changes", "get_service_dependencies": "edges",
            "search_historical_incidents": "incidents"}
