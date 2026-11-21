"""Multi-agent coordination, feedback loop, limits and failure recovery (scripted LLM, offline)."""
import time

import pytest

from conftest import run
from rca.assemble import decide_outcome, rank
from rca.config import Budget, Settings
from rca.llm.fake import FakeLLM
from rca.models import CausalStep, Finding, Hypothesis
from rca.runner import investigate


def go(incident="INC-001", llm=None, **kw):
    return run(investigate(incident, llm or FakeLLM(), settings=Settings(), sim_latency_s=kw.pop("sim_latency_s", 0.0), **kw))


def kinds(res, kind):
    return [e for e in res.state.events if e["kind"] == kind]


def test_end_to_end_report_has_required_sections():
    res = go()
    r = res.report
    assert r.system == "multi_agent" and r.ranked_hypotheses and r.ranked_hypotheses[0].rank == 1
    top = r.top
    assert top.supporting_evidence and top.confidence_label in ("high", "medium", "low") and top.causal_chain
    assert r.facts and r.assumptions and r.recommended_actions and r.timeline
    assert all(set(f.finding_ids) <= {x.id for x in r.findings} for f in r.facts)
    assert r.stats["tool_calls"] > 0 and r.stats["total_tokens"] > 0 and r.stats["usage_by_agent"].keys() >= {
        "orchestrator", "observability", "dependency", "reasoning", "critic", "reporting"}
    from rca.assemble import render_markdown
    md = render_markdown(r)
    for section in ("Ranked hypotheses", "Facts (verified observations)", "Assumptions (not verified)", "Recommended actions", "Data gaps"):
        assert section in md


def test_task_graph_runs_independent_tasks_in_parallel_and_respects_dependencies():
    res = go(sim_latency_s=0.1)
    waves = [e["parallel"] for e in kinds(res, "wave")]
    assert waves[0] == ["T1", "T2"] and waves[1] == ["T3"]              # T3 depends on T2
    start = {e["id"]: e["t"] for e in kinds(res, "task_start")}
    done = {e["id"]: e["t"] for e in kinds(res, "task_done")}
    assert start["T2"] < done["T1"] and start["T1"] < done["T2"]         # T1 and T2 overlapped in time
    assert start["T3"] >= done["T2"]
    assert res.state.tasks["T3"].depends_on == ["T2"]


def test_parallel_tool_calls_inside_one_agent_turn():
    t0 = time.perf_counter()
    res = go(sim_latency_s=0.2, llm=FakeLLM(critic_verdicts=["accept"]))
    # 9 tool calls of 0.2s each = 1.8s sequentially; two waves of concurrent calls take ~0.4s
    assert res.report.stats["tool_calls"] == 9 and time.perf_counter() - t0 < 1.2


def test_shared_state_flows_between_agents():
    res = go()
    st = res.state
    assert {f.agent for f in st.findings.values()} == {"observability", "dependency"}
    h = st.ranked()[0]
    assert {st.findings[i].agent for i in h.supporting} == {"observability", "dependency"}     # reasoning fused both specialists
    assert st.critiques[0].challenges[0].hypothesis_id == h.id                                    # critic addressed that hypothesis


def test_critic_feedback_loop_gathers_more_evidence_then_accepts():
    res = go()
    st = res.state
    critic_tasks = [t for t in st.tasks.values() if t.origin == "critic"]
    assert len(critic_tasks) == 1 and critic_tasks[0].round == 2 and critic_tasks[0].status == "done"
    assert [c.verdict for c in st.critiques] == ["needs_evidence", "accept"]
    assert len(st.ranked()[0].history) == 2                                 # hypothesis was revised, not recreated
    assert res.report.termination_reason == "critic_accepted" and res.report.stats["rounds"] == 2


def test_feedback_loop_is_bounded_by_max_rounds():
    res = go(llm=FakeLLM(critic_verdicts=["needs_evidence"]))
    assert res.report.termination_reason == "max_rounds_reached"
    assert len(res.state.critiques) == Budget().max_rounds
    assert res.report.outcome == "inconclusive"                             # cap 0.5 from an unconvinced critic


def test_repeated_evidence_requests_are_deduplicated_and_stop_the_loop():
    class Stubborn(FakeLLM):
        def _critic(self, meta, offered, _):
            st = meta["state"]
            return [("submit", {"verdict": "needs_evidence", "summary": "again", "confidence_cap": 0.4,
                                "evidence_requests": [{"agent": "observability", "question": st.tasks["T1"].objective}]})]
    res = go(llm=Stubborn())
    assert res.report.termination_reason == "no_new_evidence_paths" and len(res.state.critiques) == 1
    assert any(e["kind"] == "task_deduped" for e in res.state.events)


def test_critic_inconclusive_verdict_ends_investigation_as_inconclusive():
    res = go("INC-007", llm=FakeLLM(critic_verdicts=["inconclusive"]))
    assert res.report.outcome == "inconclusive" and res.report.termination_reason == "critic_inconclusive"
    assert res.report.ranked_hypotheses                                     # still ranked, still actionable


def test_specialist_crash_is_retried():
    res = go(agent_faults={"observability": 1})
    t1 = res.state.tasks["T1"]
    assert t1.attempts == 2 and t1.status == "done" and kinds(res, "task_error")


def test_persistently_failing_specialist_degrades_gracefully():
    res = go(agent_faults={"observability": 99})
    st = res.state
    failed = [t for t in st.tasks.values() if t.status == "failed"]
    assert failed and all(t.attempts == Budget().max_task_attempts for t in failed)
    assert any("observability agent could not complete" in g.description for g in st.gaps)
    assert res.report.ranked_hypotheses and res.report.stats["tasks_failed"] == len(failed)   # report still produced
    assert {f.agent for f in st.findings.values()} == {"dependency"}


def test_planner_and_replanner_failure_fall_back_to_code_paths():
    res = go(llm=FakeLLM(fail_times={"orchestrator": 9}))
    st = res.state
    assert all(t.origin in ("fallback", "critic") for t in st.tasks.values())
    assert [t.agent for t in list(st.tasks.values())[:3]] == ["observability", "dependency", "observability"]
    assert any("default plan" in w for w in st.warnings) and res.report.termination_reason == "critic_accepted"


def test_malformed_and_prose_outputs_are_repaired():
    res = go(llm=FakeLLM(malformed_once={"reasoning", "observability"}, prose_once={"critic"}))
    assert len(kinds(res, "repair")) == 2 and res.report.termination_reason == "critic_accepted"


def test_llm_outage_in_reasoning_is_retried_once():
    res = go(llm=FakeLLM(fail_times={"reasoning": 1}))
    assert any(e["kind"] == "recovery" and "reasoning" in e["what"] for e in res.state.events)
    assert res.report.ranked_hypotheses


def test_critic_failure_caps_unreviewed_confidence():
    res = go(llm=FakeLLM(fail_times={"critic": 9}))
    assert res.report.termination_reason == "critic_failed_unreviewed"
    assert res.report.top.confidence <= 0.6 and any("unreviewed" in w for w in res.report.warnings)


def test_reporter_failure_still_yields_a_report():
    res = go(llm=FakeLLM(fail_times={"reporting": 9}))
    assert "fallback summary" in res.report.summary and res.report.ranked_hypotheses
    assert any("reporting agent failed" in w for w in res.report.warnings)


def test_tool_budget_exhaustion_terminates_cleanly():
    res = go(budget=Budget(max_tool_calls=4))
    assert res.report.stats["tool_calls"] <= 4 + 3                          # later calls are refused, never executed
    assert sum(c.error_type == "budget_exhausted" for c in res.state.executor.calls) >= 1
    assert res.report.termination_reason.startswith("budget_exhausted") or res.report.termination_reason in ("critic_accepted", "no_new_evidence_paths")


def test_llm_call_budget_terminates_with_a_report():
    res = go(budget=Budget(max_llm_calls=5))
    assert res.report.termination_reason.startswith("budget_exhausted") and res.report.summary
    assert res.report.stats["llm_calls"] <= 5 + 1                           # + the always-allowed reporting call


def test_deadline_terminates():
    res = go(budget=Budget(deadline_s=0.0))
    assert res.report.termination_reason.startswith("budget_exhausted:deadline") and res.report.outcome == "inconclusive"


def test_tool_failures_surface_as_gaps_and_stats():
    res = go("INC-007")                                                     # auth-service logs permanently down
    assert res.report.stats["tool_failures"] >= 1
    assert any("query_logs failed" in g.description for g in res.state.gaps) and res.report.data_gaps


def test_ungrounded_citations_are_stripped_and_facts_demoted():
    res = go()                                                              # the scripted reporter cites a non-existent F999
    assert any("F999" in w for w in res.report.warnings)
    assert any("demoted from facts" in a for a in res.report.assumptions)
    assert all(i != "F999" for f in res.report.facts for i in f.finding_ids)


def test_baseline_uses_same_tools_and_report_contract():
    res = go(system="single")
    r = res.report
    assert r.system == "single_agent" and r.ranked_hypotheses and r.findings and r.stats["llm_calls"] == 2
    assert {c.tool for c in res.state.executor.calls} <= {"query_logs", "query_metrics", "get_deployments", "get_config_changes",
                                                          "get_service_dependencies", "search_historical_incidents"}


# ---- decision rules (pure functions)
def _h(i, conf, svc, support, status="active", cat="bad_deployment"):
    return Hypothesis(id=f"H{i}", title=f"h{i}", statement="s", category=cat, root_cause_service=svc, confidence=conf,
                      supporting=support, status=status, causal_chain=[CausalStep(claim="c", finding_ids=support)])


FINDINGS = {"F1": Finding(id="F1", agent="a", statement="x", kind="observation", verified=True),
            "F2": Finding(id="F2", agent="a", statement="y", kind="observation", verified=False)}


@pytest.mark.parametrize("hyps,cap,verdict,expected", [
    ([_h(1, 0.9, "a", ["F1"]), _h(2, 0.2, "b", ["F1"])], None, "accept", "root_cause_identified"),
    ([_h(1, 0.9, "a", ["F1"])], 0.4, "accept", "inconclusive"),                       # critic cap below threshold
    ([_h(1, 0.9, "a", ["F2"])], None, "accept", "inconclusive"),                      # only unverified support -> 0.3 ceiling
    ([_h(1, 0.6, "a", ["F1"]), _h(2, 0.55, "b", ["F1"])], None, "accept", "inconclusive"),   # near tie between different causes
    ([_h(1, 0.6, "a", ["F1"]), _h(2, 0.55, "a", ["F1"])], None, "accept", "root_cause_identified"),  # tie on the same cause is fine
    ([_h(1, 0.9, "a", ["F1"])], None, "inconclusive", "inconclusive"),                # critic verdict wins
    ([_h(1, 0.9, "a", ["F1"], status="rejected")], None, "accept", "inconclusive"),   # nothing active
    ([], None, None, "inconclusive"),
])
def test_outcome_decision_rules(hyps, cap, verdict, expected):
    ranked = rank(hyps, FINDINGS, cap)
    assert decide_outcome(ranked, 0.5, verdict)[0] == expected


def test_per_task_tool_call_cap_applies_within_a_single_turn():
    from rca.agents.team import MAX_TASK_TOOL_CALLS

    class Greedy(FakeLLM):
        def _observability(self, meta, offered, tool_msgs):
            st = meta["state"]
            if "query_metrics" in offered and not tool_msgs:
                w = self._window(st)
                return [("query_metrics", {"service": st.incident.affected_service, "metric": "error_rate", "max_points": 4 + i, **w})
                        for i in range(MAX_TASK_TOOL_CALLS + 5)]
            return super()._observability(meta, offered, tool_msgs)

    res = go(llm=Greedy(critic_verdicts=["accept"]))
    per_task = [e for e in res.state.events if e["kind"] == "tool_call" and e["agent"] == "observability"]
    assert len(per_task) == 2 * MAX_TASK_TOOL_CALLS          # two observability tasks, each stopped at the cap
