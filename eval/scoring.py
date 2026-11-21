"""Deterministic scoring of a FinalReport against ground truth.  This is the ONLY module
that reads ground_truth.json.  No LLM judge: every metric is a mechanical check, so results
are reproducible and cannot be flattered by fluent prose."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rca.grounding import ref_stats
from rca.models import FinalReport
from rca.tools.base import ToolCallRecord


def load_truth(data_dir: Path, incident_id: str) -> dict[str, Any]:
    return json.loads((Path(data_dir) / "incidents" / incident_id / "ground_truth.json").read_text())


def _svc_match(service: str | None, acceptable: list[str]) -> bool:
    s = (service or "").lower()
    return any(a.lower() in s for a in acceptable) if s else False


def _matches(h: Any, truth: dict[str, Any]) -> bool:
    return _svc_match(h.root_cause_service, truth["root_cause_services"]) and h.category.value in truth["acceptable_categories"]


def score(report: FinalReport, truth: dict[str, Any], tool_calls: list[ToolCallRecord]) -> dict[str, Any]:
    by_call = {c.call_id: c for c in tool_calls}
    findings = {f.id: f for f in report.findings}
    active = [h for h in report.ranked_hypotheses if h.status == "active"]
    top = active[0] if active else None
    expect_inconclusive = truth["expected_outcome"] == "inconclusive"
    identified = report.outcome == "root_cause_identified"
    out: dict[str, Any] = {"expected_outcome": truth["expected_outcome"], "outcome": report.outcome,
                           "top_service": top.root_cause_service if top else None,
                           "top_category": top.category.value if top else None,
                           "top_trigger": top.trigger_change_id if top else None,
                           "top_confidence": top.confidence if top else None, "tags": truth.get("tags", [])}

    # ---- root-cause accuracy
    if expect_inconclusive:
        # An abstention only counts if the run got far enough to weigh suspects: a baseline whose
        # LLM call failed, or a run cut before any hypothesis, ends "inconclusive" by default and
        # must not be credited for it.
        out["correct"] = (not identified) and bool(report.ranked_hypotheses)
        cands = truth.get("candidate_change_ids", [])
        text = " ".join(f"{h.trigger_change_id} {h.title} {h.statement}" for h in report.ranked_hypotheses)
        out["candidates_named"] = sum(c in text for c in cands) / len(cands) if cands else None
        out["service_ok"] = out["category_ok"] = out["trigger_ok"] = out["in_top3"] = None
        out["wrong_confident"] = identified
        out["decoy_blamed"] = False
    else:
        out["service_ok"] = bool(top and _svc_match(top.root_cause_service, truth["root_cause_services"]))
        out["category_ok"] = bool(top and top.category.value in truth["acceptable_categories"])
        out["correct"] = identified and out["service_ok"] and out["category_ok"]
        out["in_top3"] = any(_matches(h, truth) for h in active[:3])
        trig = truth.get("trigger_change_id")
        out["trigger_ok"] = (bool(top) and top.trigger_change_id == trig) if trig else None
        out["wrong_confident"] = identified and not out["correct"]
        top_text = f"{top.trigger_change_id} {top.title}" if top else ""
        out["decoy_blamed"] = bool(identified and not out["correct"] and any(d in top_text for d in truth.get("decoys", [])))
    out["abstained"] = (not identified) and not expect_inconclusive

    # ---- evidence grounding
    cited: list[str] = []
    for h in report.ranked_hypotheses:
        cited += h.supporting_evidence + h.contradicting_evidence + [i for s in h.causal_chain for i in s.finding_ids]
    for f in report.facts:
        cited += f.finding_ids
    for a in report.recommended_actions:
        cited += a.finding_ids
    for t in report.timeline:
        cited += t.finding_ids
    cited_set = [i for i in dict.fromkeys(cited)]
    existing = [findings[i] for i in cited_set if i in findings]
    good, total = ref_stats(existing, by_call.get)
    out["cited_findings"] = len(cited_set)
    # share of cited findings that trace to real tool output (dangling or unverified citations count against)
    out["claim_grounding"] = round(sum(f.verified for f in existing) / len(cited_set), 3) if cited_set else 0.0
    # share of individual evidence quotes that are literally present in the cited tool output
    out["citation_precision"] = round(good / total, 3) if total else 0.0
    out["top_hyp_grounded"] = bool(top and any(findings[i].verified for i in top.supporting_evidence if i in findings))
    chain = top.causal_chain if top else []
    out["chain_grounded"] = round(sum(1 for s in chain if any(findings[i].verified for i in s.finding_ids if i in findings)) / len(chain), 3) if chain else 0.0
    evidence_text = " ".join(f.statement + " " + " ".join(r.quote for r in f.refs) for f in existing).lower()
    keys = truth.get("key_evidence", [])
    out["key_evidence_recall"] = round(sum(k.lower() in evidence_text for k in keys) / len(keys), 3) if keys else None
    out["has_actions"] = len(report.recommended_actions) > 0
    out["facts"], out["assumptions"] = len(report.facts), len(report.assumptions)

    # ---- cost
    s = report.stats
    for k in ("latency_s", "llm_calls", "prompt_tokens", "completion_tokens", "total_tokens", "tool_calls", "tool_attempts",
              "tool_failures", "tool_partial", "tool_recovered_by_retry", "rounds", "tasks", "tasks_failed"):
        out[k] = s.get(k, 0)
    out["termination"] = report.termination_reason
    return out
