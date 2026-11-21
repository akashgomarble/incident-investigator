"""Regression tests for defects found in code review."""
import asyncio

import httpx
import pytest

from rca.assemble import decide_outcome, rank
from rca.llm.base import LLMError
from rca.llm.openai_compat import OpenAICompatClient
from rca.models import EvidenceRef, Finding, Hypothesis, RootCauseCategory


def _hyp(hid, service, conf):
    return Hypothesis(id=hid, title=hid, statement="s", category=RootCauseCategory("bad_deployment"),
                      root_cause_service=service, confidence=conf, supporting=["F1"])


def _findings():
    return {"F1": Finding(id="F1", agent="observability", task_id="T1", kind="observation", statement="x",
                          evidence=[EvidenceRef(call_id="tc-001", quote="abcd")], verified=True)}


def test_critic_cap_does_not_turn_a_clear_lead_into_a_tie():
    ranked = rank([_hyp("H1", "svc-a", 0.9), _hyp("H2", "svc-b", 0.7)], _findings(), cap=0.65)
    assert [r.confidence for r in ranked] == [0.65, 0.65]          # the cap still applies to what is reported
    assert ranked[0].hypothesis_id == "H1"
    assert decide_outcome(ranked, 0.5, "accept")[0] == "root_cause_identified"


def test_a_genuine_near_tie_is_still_inconclusive():
    ranked = rank([_hyp("H1", "svc-a", 0.72), _hyp("H2", "svc-b", 0.68)], _findings(), cap=0.9)
    assert decide_outcome(ranked, 0.5, "accept")[0] == "inconclusive"


@pytest.mark.parametrize("body", ["<html>bad gateway</html>", '{"choices": [{}]}'])
def test_malformed_2xx_body_becomes_llm_error(body, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    client = OpenAICompatClient(base_url="http://llm.test", api_key="k", model="m", max_retries=1)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body)))

    async def go():
        with pytest.raises(LLMError):
            await client.complete([{"role": "user", "content": "hi"}])
        await client.aclose()
    asyncio.run(go())
    assert client.retries == 2


_real_sleep = asyncio.sleep


async def _no_sleep(_s):
    await _real_sleep(0)


# ---- third review ---------------------------------------------------------------------------
def test_an_open_evidence_request_or_a_missing_review_blocks_a_conclusion():
    from rca.assemble import UNREVIEWED
    ranked = rank([_hyp("H1", "svc-a", 0.9)], _findings(), cap=0.6)
    assert decide_outcome(ranked, 0.5, "accept")[0] == "root_cause_identified"
    assert decide_outcome(ranked, 0.5, None)[0] == "root_cause_identified"        # systems without a critic: cap only
    for verdict in ("needs_evidence", UNREVIEWED):
        outcome, why = decide_outcome(ranked, 0.5, verdict)
        assert outcome == "inconclusive" and ("critic" in why)


def test_run_cut_before_or_during_review_is_inconclusive_but_degraded_modes_still_answer():
    from conftest import run
    from rca.config import Budget, Settings
    from rca.llm.fake import FakeLLM
    from rca.runner import investigate

    def go(system="multi", **kw):
        return run(investigate("INC-001", FakeLLM(), system=system, settings=Settings(), sim_latency_s=0, **kw))

    unreviewed = go(budget=Budget(max_llm_calls=8))             # budget ends the run after reasoning, before the critic
    assert unreviewed.report.ranked_hypotheses and not unreviewed.state.critiques
    assert unreviewed.report.outcome == "inconclusive" and any("before any critic review" in w for w in unreviewed.report.warnings)
    open_request = go(budget=Budget(max_llm_calls=9))           # ...and after a review that asked for more evidence
    assert open_request.state.critiques[-1].verdict == "needs_evidence" and open_request.report.outcome == "inconclusive"
    # Degraded modes keep answering, flagged and capped at 0.6: a critic outage, and the no-Critic ablation.
    outage = go(agent_faults={"critic": 99}).report
    assert outage.termination_reason == "critic_failed_unreviewed"
    assert all(h.confidence <= 0.6 for h in outage.ranked_hypotheses if h.status == "active")
    ablation = go(system="nocritic").report
    assert ablation.termination_reason == "critic_disabled" and ablation.outcome == "root_cause_identified"


def _hin(hid, title):
    from rca.models import HypothesisIn
    return HypothesisIn(id=hid, title=title, statement="s", category="bad_deployment", confidence=0.5)


def test_hypothesis_ids_reused_inside_one_batch_do_not_overwrite_each_other():
    from conftest import make_executor
    from rca.config import Budget
    from rca.state import InvestigationState
    ex = make_executor("INC-001")
    st = InvestigationState(ex.store.incident, ex, Budget())
    st.upsert_hypotheses([_hin("H2", "first"), _hin("H1", "second"), _hin("H1", "third")])
    assert [h.title for h in st.hypotheses.values()] == ["first", "second", "third"]
    st.round = 2
    st.upsert_hypotheses([_hin(" h1", "first, revised"), _hin("H1", "a rival using the same id")])
    assert st.hypotheses["H1"].title == "first, revised" and len(st.hypotheses["H1"].history) == 2
    assert st.hypotheses["H4"].title == "a rival using the same id"
    assert any("twice" in w for w in st.warnings)


def test_a_run_that_never_formed_a_hypothesis_gets_no_credit_for_abstaining():
    from eval.scoring import score
    from rca.models import FinalReport
    truth = {"expected_outcome": "inconclusive", "root_cause_services": [], "acceptable_categories": [], "candidate_change_ids": ["dep-1"]}
    failed = FinalReport(incident_id="X", system="single_agent", outcome="inconclusive", summary="", ranked_hypotheses=[],
                         facts=[], assumptions=[], timeline=[], recommended_actions=[], data_gaps=[], findings=[],
                         termination_reason="baseline_failed: LLM call failed", stats={})
    s = score(failed, truth, [])
    assert not s["correct"] and not s["wrong_confident"]


def test_partial_metrics_and_dependencies_do_not_leak_the_dropped_part():
    from conftest import run, window
    from rca.config import Budget
    from rca.tools.datastore import DataStore
    from rca.tools.executor import ToolExecutor
    from rca.tools.faults import FaultInjector
    from conftest import DATA
    ds = DataStore(DATA, "INC-001")
    ex = ToolExecutor(ds, FaultInjector([{"tool": "query_metrics", "kind": "partial", "times": 1},
                                         {"tool": "get_service_dependencies", "kind": "partial", "times": 1}]), Budget(), sim_latency_s=0)
    svc = ds.incident.affected_service
    m = run(ex.call("observability", "query_metrics", {"service": svc, "metric": "error_rate", **window(ds)}))
    assert m.partial and m.data["summary"] is None
    d = run(ex.call("dependency", "get_service_dependencies", {"service": svc}))
    named = {svc} | {e[k] for e in d.data["edges"] for k in ("from", "to")}
    assert d.partial and {n["name"] for n in d.data["nodes"]} <= named


def test_unknown_task_dependency_is_reported_not_silently_dropped():
    from conftest import make_executor
    from rca.agents.base import AgentRuntime
    from rca.agents.orchestrator import Orchestrator
    from rca.config import Budget
    from rca.llm.fake import FakeLLM
    from rca.models import PlannedTask
    from rca.state import InvestigationState
    ex = make_executor("INC-001")
    st = InvestigationState(ex.store.incident, ex, Budget())
    orch = Orchestrator(AgentRuntime(llm=FakeLLM(), state=st))
    tasks = orch._register([PlannedTask(key="c", agent="observability", objective="check what b found", depends_on=["b"]),
                            PlannedTask(key="b", agent="dependency", objective="map dependencies")], "plan")
    assert tasks[0].depends_on == [] and any("unknown or later dependencies ['b']" in w for w in st.warnings)


def test_cli_reports_usage_errors_in_one_line(capsys, monkeypatch):
    from rca.cli import main
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr("rca.cli.load_dotenv", lambda *a, **k: None)
    assert main(["investigate", "INC-001"]) == 2 and "LLM_API_KEY" in capsys.readouterr().err
    assert main(["investigate", "INC-001", "--llm", "fake", "--agent-fault", "bad"]) == 2
    assert main(["investigate", "INC-999", "--llm", "fake"]) == 2 and "unknown incident" in capsys.readouterr().err


def test_eval_refuses_to_relabel_saved_runs_with_new_flags(tmp_path):
    import json
    from eval.run_eval import config_conflict, saved_config
    cur = {"model": "m1", "llm": "real", "chaos": 0.0, "chaos_hard": 0.0}
    assert saved_config(tmp_path) is None and config_conflict(None, cur) is None
    (tmp_path / "results.json").write_text(json.dumps({"model": "m1", "args": {"llm": "real", "chaos": 0.0}}))   # legacy folder
    assert config_conflict(saved_config(tmp_path), cur) is None
    assert "chaos=0.0 (now 0.3)" in config_conflict(saved_config(tmp_path), {**cur, "chaos": 0.3})
    assert "model" in config_conflict(saved_config(tmp_path), {**cur, "model": "m2"})
