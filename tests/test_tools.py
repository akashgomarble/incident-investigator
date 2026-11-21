"""Tools: structured inputs/outputs and caller-error handling."""
from conftest import make_executor, run, window


def test_query_logs_structured_output():
    ex = make_executor("INC-001")
    r = run(ex.call("t", "query_logs", {"service": "order-service", "min_level": "ERROR", **window(ex.store, 60)}))
    assert r.ok and r.call_id == "tc-001" and not r.partial
    d = r.data
    assert d["total_matched"] > d["returned"] and d["truncated"] is True          # truncation is explicit
    assert set(d["entries"][0]) == {"ts", "level", "message"}
    assert d["top_patterns"][0]["count"] > 1 and "first_seen" in d["top_patterns"][0]
    assert all(e["level"] == "ERROR" for e in d["entries"])


def test_query_logs_contains_and_order():
    ex = make_executor("INC-001")
    w = window(ex.store, 60)
    asc = run(ex.call("t", "query_logs", {"service": "order-service", "contains": "nullpointer", "limit": 3, **w})).data
    desc = run(ex.call("t", "query_logs", {"service": "order-service", "contains": "nullpointer", "limit": 3, "order": "desc", **w})).data
    assert asc["entries"][0]["ts"] < desc["entries"][0]["ts"]
    assert all("NullPointer" in e["message"] for e in asc["entries"])


def test_query_metrics_downsamples_and_summarises():
    ex = make_executor("INC-001")
    r = run(ex.call("t", "query_metrics", {"service": "order-service", "metric": "error_rate", "max_points": 8, **window(ex.store, 120)}))
    assert r.ok and len(r.data["points"]) <= 8
    assert r.data["summary"]["max"] > 10 > r.data["summary"]["min"]
    assert "latency_p99_ms" in r.data["available_metrics"] and r.data["unit"]


def test_unknown_metric_lists_alternatives():
    ex = make_executor("INC-001")
    r = run(ex.call("t", "query_metrics", {"service": "order-service", "metric": "nope", **window(ex.store)}))
    assert not r.ok and r.error.type == "not_found" and not r.error.retryable
    assert "error_rate" in r.error.details["available_metrics"] and r.attempts == 1


def test_external_dependency_has_no_telemetry():
    ex = make_executor("INC-003")
    r = run(ex.call("t", "query_logs", {"service": "exchange-api", **window(ex.store)}))
    assert not r.ok and r.error.type == "not_found" and r.error.details["callers"] == ["exchange-gateway"]


def test_invalid_arguments_are_structured_errors():
    ex = make_executor("INC-001")
    assert run(ex.call("t", "query_logs", {"service": "order-service"})).error.type == "invalid_arguments"          # missing window
    assert run(ex.call("t", "query_logs", {"service": "order-service", "start": "yesterday", "end": "now"})).error.type == "invalid_arguments"
    assert run(ex.call("t", "query_metrics", {"service": "x", "metric": "y", "max_points": 9999, **window(ex.store)})).error.type == "invalid_arguments"
    assert run(ex.call("t", "no_such_tool", {})).error.type == "invalid_arguments"
    r = run(ex.call("t", "get_deployments", {"start": "2026-03-03T10:00:00Z", "end": "2026-03-03T09:00:00Z"}))
    assert r.error.type == "invalid_arguments" and "after start" in r.error.message


def test_changes_and_dependencies():
    ex = make_executor("INC-002")
    w = window(ex.store, 360)
    deps = run(ex.call("t", "get_deployments", w)).data
    assert deps["count"] == 1 and deps["deployments"][0]["id"] == "dep-5220" and deps["scope"] == "all services"
    cfg = run(ex.call("t", "get_config_changes", {"service": "portfolio-service", **w})).data
    assert [c["id"] for c in cfg["changes"]] == ["cfg-2207"] and cfg["changes"][0]["new_value"] == 5
    g = run(ex.call("t", "get_service_dependencies", {"service": "order-service", "direction": "downstream"})).data
    assert {e["to"] for e in g["edges"]} >= {"ledger-db", "pricing-engine", "exchange-gateway", "kafka"}
    up = run(ex.call("t", "get_service_dependencies", {"service": "ledger-db", "direction": "upstream"})).data
    assert {e["from"] for e in up["edges"]} == {"order-service", "portfolio-service", "payment-service", "settlement-service"}
    deep = run(ex.call("t", "get_service_dependencies", {"service": "api-gateway", "direction": "downstream", "depth": 3})).data
    assert any(n["name"] == "exchange-api" and not n["has_telemetry"] for n in deep["nodes"])


def test_historical_search_ranks_by_overlap():
    ex = make_executor("INC-002")
    r = run(ex.call("t", "search_historical_incidents", {"query": "connection pool exhausted request timed out"})).data
    assert r["incidents"][0]["id"] == "HIST-115" and "leads" in r["note"]


def test_empty_result_reports_data_range():
    ex = make_executor("INC-001")
    r = run(ex.call("t", "query_logs", {"service": "order-service", "start": "2020-01-01T00:00:00Z", "end": "2020-01-02T00:00:00Z"}))
    assert r.ok and r.data["total_matched"] == 0 and "data_range" in r.data


def test_call_log_is_append_only_evidence_ledger():
    ex = make_executor("INC-001")
    r = run(ex.call("agentA", "get_deployments", window(ex.store, 360)))
    rec = ex.record(r.call_id)
    assert rec.agent == "agentA" and rec.output == r.for_model() and "dep-5101" in rec.output
