"""Tool contracts: structured inputs (pydantic), structured outputs (ToolResult)."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

ErrorType = Literal[
    "timeout", "unavailable", "rate_limited",          # retryable
    "invalid_arguments", "not_found",                  # caller errors -- fix the arguments
    "circuit_open", "budget_exhausted", "internal",    # terminal for this call
]
RETRYABLE: set[str] = {"timeout", "unavailable", "rate_limited"}


class ToolError(BaseModel):
    type: ErrorType
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    call_id: str
    tool: str
    ok: bool
    data: Optional[dict[str, Any]] = None
    error: Optional[ToolError] = None
    partial: bool = False                  # True => data is incomplete; absence of evidence proves nothing
    warnings: list[str] = Field(default_factory=list)
    attempts: int = 1
    latency_ms: int = 0

    def for_model(self) -> str:
        """Compact JSON shown to the LLM *and* used by the grounding verifier (same bytes)."""
        return self.model_dump_json(exclude_none=True, exclude={"latency_ms"})


class ToolCallRecord(BaseModel):
    call_id: str
    agent: str
    tool: str
    args: dict[str, Any]
    ok: bool
    partial: bool
    error_type: Optional[str]
    attempts: int
    latency_ms: int
    output: str                            # exactly what the model saw


# ----------------------------------------------------------------------------- inputs
class _Window(BaseModel):
    start: str = Field(description="ISO-8601 UTC start, e.g. 2026-03-03T13:00:00Z")
    end: str = Field(description="ISO-8601 UTC end")


class QueryLogsInput(_Window):
    service: str
    min_level: Literal["INFO", "WARN", "ERROR"] = Field(default="WARN", description="Lowest level to include")
    contains: Optional[str] = Field(default=None, description="Case-insensitive substring filter on the message")
    limit: int = Field(default=15, ge=1, le=40)
    order: Literal["asc", "desc"] = Field(default="asc", description="asc = earliest first (good for finding onset)")


class QueryMetricsInput(_Window):
    service: str
    metric: str = Field(description="e.g. error_rate, latency_p99_ms, request_rate, cpu_pct, memory_pct")
    max_points: int = Field(default=16, ge=4, le=48, description="Series is downsampled to at most this many buckets")


class GetDeploymentsInput(_Window):
    service: Optional[str] = Field(default=None, description="Omit to list deployments for ALL services")


class GetConfigChangesInput(_Window):
    service: Optional[str] = Field(default=None, description="Omit to list config/flag changes for ALL services")


class GetServiceDependenciesInput(BaseModel):
    service: str
    direction: Literal["downstream", "upstream", "both"] = Field(
        default="both", description="downstream = what this service calls; upstream = who calls it")
    depth: int = Field(default=1, ge=1, le=3)


class SearchHistoricalIncidentsInput(BaseModel):
    query: str = Field(description="Symptom keywords, e.g. 'connection pool exhausted timeouts'")
    service: Optional[str] = None
    limit: int = Field(default=4, ge=1, le=8)


class ToolSpec(BaseModel):
    name: str
    description: str
    input_model: type[BaseModel]

    def schema(self) -> dict[str, Any]:
        """OpenAI-style function schema."""
        js = self.input_model.model_json_schema()
        js.pop("title", None)
        for p in js.get("properties", {}).values():
            p.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": js}}


TOOL_SPECS: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec(name="query_logs", input_model=QueryLogsInput, description=(
        "Search one service's logs in a time window. Returns matching entries (up to `limit`), total match count, "
        "counts by level and the most frequent message patterns with first-seen time. Check `truncated`/`partial`.")),
    ToolSpec(name="query_metrics", input_model=QueryMetricsInput, description=(
        "Fetch one metric time-series for one service, downsampled to `max_points` buckets (avg and max per bucket) "
        "plus summary stats. Null values mean the datapoint is MISSING (not zero). The response lists the metrics "
        "available for that service.")),
    ToolSpec(name="get_deployments", input_model=GetDeploymentsInput, description=(
        "List code deployments (id, service, version, time, change summary, rollback status) in a time window.")),
    ToolSpec(name="get_config_changes", input_model=GetConfigChangesInput, description=(
        "List configuration and feature-flag changes (id, service, key, old/new value, time) in a time window.")),
    ToolSpec(name="get_service_dependencies", input_model=GetServiceDependenciesInput, description=(
        "Return the dependency graph around a service: what it calls (downstream) and who calls it (upstream), "
        "including shared datastores and external providers, and whether each node has telemetry.")),
    ToolSpec(name="search_historical_incidents", input_model=SearchHistoricalIncidentsInput, description=(
        "Keyword search over past incident post-mortems (symptoms, root cause, resolution). Similar symptoms do "
        "NOT imply the same cause -- treat results as leads to verify, not as evidence about the current incident.")),
]}
