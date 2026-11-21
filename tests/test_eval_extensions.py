"""The evaluation's ablation and stress options: no-Critic team, hard tool faults, intervals."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.run_eval import wilson
from rca.llm.fake import FakeLLM
from rca.runner import investigate
from rca.tools.faults import FaultInjector


def test_nocritic_ablation_runs_one_unreviewed_pass():
    res = asyncio.run(investigate("INC-001", FakeLLM(), system="nocritic"))
    assert res.report.termination_reason == "critic_disabled"
    assert res.state.critiques == []
    assert all(h.confidence <= 0.6 for h in res.report.ranked_hypotheses if h.status == "active")
    assert any("critic disabled" in w for w in res.report.warnings)


def test_hard_chaos_fails_on_every_attempt_and_soft_chaos_only_once():
    hard = FaultInjector([], chaos_hard_rate=1.0, seed=1)
    assert all(hard.pick("query_logs", {"service": "a"}, attempt) is not None for attempt in (1, 2, 3))
    soft = FaultInjector([], chaos_rate=1.0, seed=1)
    assert soft.pick("query_logs", {"service": "a"}, 1) is not None
    assert soft.pick("query_logs", {"service": "a"}, 2) is None
    assert FaultInjector([], seed=1).pick("query_logs", {"service": "a"}, 1) is None


def test_hard_chaos_run_still_produces_a_report_with_failed_calls():
    res = asyncio.run(investigate("INC-001", FakeLLM(), chaos_hard_rate=0.5, seed=3))
    assert res.report.outcome in ("root_cause_identified", "inconclusive")
    assert res.report.stats["tool_failures"] > 0


def test_wilson_interval():
    lo, hi = wilson(35, 36)
    assert 0.85 < lo < 0.87 and 0.99 < hi <= 1.0
    lo, hi = wilson(36, 36)
    assert 0.90 < lo < 0.91 and hi == 1.0
