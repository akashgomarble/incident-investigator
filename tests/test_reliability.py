"""Simulated failures, bounded retries, circuit breaker, budgets."""
import asyncio
import time

from conftest import DATA, make_executor, run, window
from rca.config import Budget
from rca.tools import impl
from rca.tools.datastore import DataStore
from rca.tools.executor import ToolExecutor
from rca.tools.faults import FaultInjector


def test_transient_timeout_recovered_by_retry():
    ex = make_executor("INC-006")                        # redis-cache logs time out twice
    r = run(ex.call("t", "query_logs", {"service": "redis-cache", **window(ex.store)}))
    assert r.ok and r.attempts == 3 and ex.total_attempts == 3


def test_retries_are_bounded_then_surface_structured_error():
    ex = make_executor("INC-006")                        # market-data metrics unavailable for 4 attempts
    args = {"service": "market-data-service", "metric": "latency_p99_ms", **window(ex.store)}
    r1 = run(ex.call("t", "query_metrics", args))
    assert not r1.ok and r1.error.type == "unavailable" and r1.error.retryable and r1.attempts == 3
    assert "gave up after 3 attempts" in r1.error.message
    r2 = run(ex.call("t", "query_metrics", args))        # the agent asking again later succeeds
    assert r2.ok and r2.attempts == 2


def test_permanent_outage_opens_circuit_breaker():
    ex = make_executor("INC-007")                        # auth-service logs permanently unavailable
    args = {"service": "auth-service", **window(ex.store)}
    types = [run(ex.call("t", "query_logs", args)).error.type for _ in range(5)]
    assert types == ["unavailable"] * 3 + ["circuit_open"] * 2
    assert ex.total_attempts == 9                        # the breaker stops burning attempts


def test_caller_errors_do_not_trip_breaker():
    ex = make_executor("INC-001")
    for _ in range(5):
        assert run(ex.call("t", "query_metrics", {"service": "order-service", "metric": "nope", **window(ex.store)})).error.type == "not_found"
    assert run(ex.call("t", "query_metrics", {"service": "order-service", "metric": "error_rate", **window(ex.store)})).ok


def test_partial_result_is_flagged_not_silently_retried():
    ex = make_executor("INC-011")                        # first config query silently omits the key change
    w = window(ex.store, 360)
    first = run(ex.call("t", "get_config_changes", w))
    assert first.ok and first.partial and first.attempts == 1 and "INCOMPLETE" in first.warnings[0]
    assert "cfg-3120" not in first.for_model()
    second = run(ex.call("t", "get_config_changes", w))
    assert second.ok and not second.partial and "cfg-3120" in second.for_model()


def test_partial_logs_drop_aggregations():
    ds = DataStore(DATA, "INC-001")
    ex = ToolExecutor(ds, FaultInjector([{"tool": "query_logs", "kind": "partial", "times": 1}]), Budget(), sim_latency_s=0)
    r = run(ex.call("t", "query_logs", {"service": "order-service", **window(ds, 60)}))
    assert r.partial and r.data["top_patterns"] == [] and r.data["total_matched"] is None and r.data["truncated"]


def test_tool_call_budget_is_enforced():
    ex = make_executor("INC-001", budget=Budget(max_tool_calls=2))
    w = window(ex.store)
    assert run(ex.call("t", "get_deployments", w)).ok and run(ex.call("t", "get_deployments", w)).ok
    r = run(ex.call("t", "get_deployments", w))
    assert not r.ok and r.error.type == "budget_exhausted"


def test_tool_bug_is_contained(monkeypatch):
    def boom(ds, inp):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(impl.IMPLS, "get_deployments", boom)
    ex = make_executor("INC-001")
    r = run(ex.call("t", "get_deployments", window(ex.store)))
    assert not r.ok and r.error.type == "internal" and "kaboom" in r.error.message and r.attempts == 1


def test_real_hang_is_cut_by_timeout():
    ds = DataStore(DATA, "INC-001")
    ex = ToolExecutor(ds, FaultInjector(), Budget(tool_timeout_s=0.05, tool_max_retries=1, tool_backoff_s=0), sim_latency_s=1.0)
    t0 = time.perf_counter()
    r = run(ex.call("t", "get_deployments", window(ds)))
    assert not r.ok and r.error.type == "timeout" and r.attempts == 2 and time.perf_counter() - t0 < 0.5


def test_chaos_faults_are_deterministic_and_transient():
    def run_once():
        ds = DataStore(DATA, "INC-001")
        ex = ToolExecutor(ds, FaultInjector([], chaos_rate=0.5, seed=3), Budget(), sim_latency_s=0, sim_timeout_s=0)
        async def go():
            return await asyncio.gather(*(ex.call("t", "query_metrics", {"service": "order-service", "metric": m, **window(ds)})
                                          for m in ("error_rate", "cpu_pct", "memory_pct", "latency_p99_ms", "request_rate", "restarts")))
        return [(r.ok, r.attempts) for r in run(go())]
    a, b = run_once(), run_once()
    assert a == b and all(ok for ok, _ in a) and any(n > 1 for _, n in a)


def test_independent_tool_calls_run_in_parallel():
    ds = DataStore(DATA, "INC-001")
    ex = ToolExecutor(ds, FaultInjector(), Budget(), sim_latency_s=0.15)
    async def go():
        return await asyncio.gather(*(ex.call("t", "get_deployments", window(ds)) for _ in range(6)))
    t0 = time.perf_counter()
    rs = run(go())
    assert all(r.ok for r in rs) and time.perf_counter() - t0 < 0.5      # 6 x 0.15s sequential would be 0.9s
    assert sorted(r.call_id for r in rs) == [f"tc-00{i}" for i in range(1, 7)]
