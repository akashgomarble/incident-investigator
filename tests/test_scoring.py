"""The scorer itself must be right, otherwise the evaluation means nothing."""
from eval.scoring import score
from rca.models import (CausalStep, EvidenceRef, FactItem, FinalReport, Finding, RankedHypothesis)
from rca.tools.base import ToolCallRecord

CALLS = [ToolCallRecord(call_id="tc-001", agent="a", tool="get_config_changes", args={}, ok=True, partial=False, error_type=None,
                        attempts=1, latency_ms=1, output='{"changes":[{"id":"cfg-2207","key":"db.pool.max_size"}]}')]
TRUTH = {"expected_outcome": "root_cause_identified", "root_cause_services": ["portfolio-service"], "category": "config_change",
         "acceptable_categories": ["config_change"], "trigger_change_id": "cfg-2207", "decoys": ["dep-5220"],
         "key_evidence": ["cfg-2207", "total=5"], "tags": ["decoy-deploy"]}


def report(service="portfolio-service", category="config_change", trigger="cfg-2207", outcome="root_cause_identified", quote="cfg-2207", title="pool"):
    f1 = Finding(id="F1", agent="a", statement="pool config changed", kind="observation", refs=[EvidenceRef(call_id="tc-001", quote=quote)],
                 verified=quote == "cfg-2207")
    h = RankedHypothesis(rank=1, hypothesis_id="H1", title=title, statement="s", category=category, root_cause_service=service,
                         trigger_change_id=trigger, confidence=0.8, confidence_label="high", status="active",
                         causal_chain=[CausalStep(claim="c1", finding_ids=["F1"]), CausalStep(claim="c2", finding_ids=[])],
                         supporting_evidence=["F1", "F7"])
    return FinalReport(incident_id="X", system="multi_agent", outcome=outcome, summary="", ranked_hypotheses=[h],
                       facts=[FactItem(statement="x", finding_ids=["F1"])], assumptions=[], timeline=[], recommended_actions=[],
                       data_gaps=[], findings=[f1], stats={"tool_calls": 3})


def test_correct_diagnosis():
    s = score(report(), TRUTH, CALLS)
    assert s["correct"] and s["service_ok"] and s["category_ok"] and s["trigger_ok"] and s["in_top3"]
    assert not s["wrong_confident"] and not s["decoy_blamed"] and not s["abstained"]
    assert s["citation_precision"] == 1.0 and s["claim_grounding"] == 0.5       # F7 is a dangling citation
    assert s["chain_grounded"] == 0.5 and s["key_evidence_recall"] == 0.5 and s["tool_calls"] == 3


def test_service_name_is_matched_leniently_but_category_strictly():
    assert score(report(service="Portfolio-Service (datasource)"), TRUTH, CALLS)["correct"]
    s = score(report(category="bad_deployment"), TRUTH, CALLS)
    assert not s["correct"] and s["service_ok"] and not s["category_ok"] and s["wrong_confident"]


def test_blaming_the_decoy_is_detected():
    s = score(report(service="api-gateway", category="bad_deployment", trigger="dep-5220"), TRUTH, CALLS)
    assert not s["correct"] and s["decoy_blamed"] and s["wrong_confident"] and s["trigger_ok"] is False


def test_abstaining_on_an_answerable_incident_is_incorrect_but_not_confidently_wrong():
    s = score(report(outcome="inconclusive"), TRUTH, CALLS)
    assert not s["correct"] and s["abstained"] and not s["wrong_confident"]


def test_fabricated_quote_lowers_grounding():
    s = score(report(quote="db.pool.max_size=500"), TRUTH, CALLS)
    assert s["citation_precision"] == 0.0 and s["claim_grounding"] == 0.0 and not s["top_hyp_grounded"]


def test_inconclusive_ground_truth():
    t = {**TRUTH, "expected_outcome": "inconclusive", "root_cause_services": [], "candidate_change_ids": ["dep-5710", "cfg-2705"]}
    good = score(report(outcome="inconclusive", title="JWT upgrade dep-5710 or key rotation cfg-2705"), t, CALLS)
    assert good["correct"] and good["candidates_named"] == 1.0 and not good["wrong_confident"]
    bad = score(report(outcome="root_cause_identified"), t, CALLS)
    assert not bad["correct"] and bad["wrong_confident"]
