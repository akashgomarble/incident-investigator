"""Deterministic report assembly shared by the multi-agent system and the baseline, so both
are held to identical evidence rules:
  * confidence is clamped (critic cap; 0.3 ceiling without any verified supporting finding)
  * the outcome is decided by rule, not by rhetoric
  * 'facts' may only cite verified observations -- anything else is demoted to an assumption
"""
from __future__ import annotations

from typing import Any, Optional

from .models import (Action, FactItem, FinalReport, Finding, Hypothesis, RankedHypothesis, TimelineItem,
                     confidence_label)

UNSUPPORTED_CEILING = 0.3
TIE_MARGIN = 0.1
# decide_outcome verdict for a team run that a budget cut before the critic reviewed the current
# hypotheses.  Distinct from None, which means "this system has no critic" (baseline, ablation).
UNREVIEWED = "unreviewed"


def rank(hyps: list[Hypothesis], findings: dict[str, Finding], cap: Optional[float]) -> list[RankedHypothesis]:
    rows = []
    for h in hyps:
        c = raw = h.confidence
        if h.status == "active":
            if not any(findings[f].verified for f in h.supporting if f in findings):
                c = raw = min(raw, UNSUPPORTED_CEILING)
            if cap is not None:
                c = min(raw, cap)
        rows.append((h, round(c, 2), round(raw, 2)))
    rows.sort(key=lambda t: (t[0].status != "active", -t[1], -t[2]))
    return [RankedHypothesis(
        rank=i + 1, hypothesis_id=h.id, title=h.title, statement=h.statement, category=h.category,
        root_cause_service=h.root_cause_service, trigger_change_id=h.trigger_change_id, confidence=c,
        pre_cap_confidence=raw, confidence_label=confidence_label(c), status=h.status, causal_chain=h.causal_chain,
        supporting_evidence=h.supporting, contradicting_evidence=h.contradicting, assumptions=h.assumptions,
        rejection_reason=h.rejection_reason) for i, (h, c, raw) in enumerate(rows)]


def decide_outcome(ranked: list[RankedHypothesis], threshold: float, final_verdict: Optional[str]) -> tuple[str, str]:
    active = [r for r in ranked if r.status == "active"]
    if not active:
        return "inconclusive", "no active hypothesis"
    top = active[0]
    if final_verdict == "inconclusive":
        return "inconclusive", "critic judged the evidence unable to separate the hypotheses"
    # A root cause is only declared when nothing stands against it.  If the run stopped while the
    # critic was still asking for evidence, or before any review of the current hypotheses finished,
    # the confidence cap alone is not enough: the report says "inconclusive" and ranks its suspects.
    if final_verdict == "needs_evidence":
        return "inconclusive", "the critic's last review still asked for evidence and the run ended before it was supplied"
    if final_verdict == UNREVIEWED:
        return "inconclusive", "the run was cut before a critic review of the current hypotheses completed"
    if top.confidence < threshold:
        return "inconclusive", f"top hypothesis confidence {top.confidence:.2f} is below the {threshold:.2f} threshold"
    # Every rival is checked, not only the runner-up: the runner-up may be a second reading of the
    # same cause while a genuinely different explanation sits right behind it.
    for second in active[1:]:
        different = (second.root_cause_service, second.category) != (top.root_cause_service, top.category)
        # Compare pre-cap confidences: the critic cap clamps every active hypothesis to the same
        # ceiling, which would otherwise turn a clear lead into a spurious tie.
        if top.pre_cap_confidence is not None and second.pre_cap_confidence is not None:
            margin = top.pre_cap_confidence - second.pre_cap_confidence
        else:
            margin = top.confidence - second.confidence
        if different and second.confidence >= threshold and margin < TIE_MARGIN:
            return "inconclusive", (f"two different explanations are nearly tied "
                                    f"({top.hypothesis_id} {top.confidence:.2f} vs {second.hypothesis_id} {second.confidence:.2f})")
    return "root_cause_identified", "top hypothesis is above threshold and clearly ahead"


def render_ranked(ranked: list[RankedHypothesis]) -> str:
    out = []
    for r in ranked:
        out.append(f"#{r.rank} {r.hypothesis_id} [{r.status}] confidence {r.confidence:.2f} ({r.confidence_label}) -- {r.title}\n"
                   f"   category={r.category.value} service={r.root_cause_service} trigger={r.trigger_change_id}\n"
                   f"   {r.statement}\n   supporting={r.supporting_evidence} contradicting={r.contradicting_evidence}\n"
                   f"   assumptions={r.assumptions}" + (f"\n   rejected: {r.rejection_reason}" if r.rejection_reason else ""))
    return "\n".join(out) or "(no hypotheses)"


def build_report(*, incident_id: str, system: str, ranked: list[RankedHypothesis], findings: dict[str, Finding],
                 outcome: str, summary: str, facts: list[FactItem], assumptions: list[str], timeline: list[TimelineItem],
                 actions: list[Action], data_gaps: list[str], what_would_change: list[str], termination_reason: str,
                 stats: dict[str, Any], warnings: list[str]) -> FinalReport:
    warnings = list(warnings)

    def keep(ids: list[str], where: str) -> list[str]:
        bad = [i for i in ids if i not in findings]
        if bad:
            warnings.append(f"{where}: removed citations to non-existent findings {bad}")
        return [i for i in ids if i in findings]

    clean_facts: list[FactItem] = []
    assumptions = list(assumptions)
    for f in facts:
        ids = keep(f.finding_ids, "fact")
        solid = [i for i in ids if findings[i].verified and findings[i].kind == "observation"]
        if solid:
            clean_facts.append(FactItem(statement=f.statement, finding_ids=solid))
        else:
            assumptions.append(f"(demoted from facts: no verified observation backs it) {f.statement}")
            warnings.append(f"fact demoted to assumption: {f.statement[:80]}")
    for t in timeline:
        t.finding_ids = keep(t.finding_ids, "timeline")
    for a in actions:
        a.finding_ids = keep(a.finding_ids, "action")
    return FinalReport(
        incident_id=incident_id, system=system, outcome=outcome, summary=summary, ranked_hypotheses=ranked,  # type: ignore[arg-type]
        facts=clean_facts, assumptions=assumptions, timeline=timeline, recommended_actions=actions,
        data_gaps=data_gaps, what_would_change_the_conclusion=what_would_change, findings=list(findings.values()),
        termination_reason=termination_reason, stats=stats, warnings=warnings)


def render_markdown(r: FinalReport) -> str:
    L: list[str] = [f"# Incident investigation report: {r.incident_id}",
                    f"*System: {r.system} | outcome: **{r.outcome}** | ended: {r.termination_reason}*", "", "## Summary", r.summary, ""]
    top = r.top
    if r.outcome == "root_cause_identified" and top:
        L += ["## Probable root cause",
              f"**{top.title}** -- {top.category.value} in `{top.root_cause_service}`"
              + (f", triggered by `{top.trigger_change_id}`" if top.trigger_change_id else "")
              + f" (confidence {top.confidence:.2f}, {top.confidence_label})", ""]
    else:
        L += ["## Probable root cause", "**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.", ""]
    L += ["## Ranked hypotheses", "| # | Hypothesis | Category | Service | Trigger | Confidence | Status |", "|---|---|---|---|---|---|---|"]
    for h in r.ranked_hypotheses:
        L.append(f"| {h.rank} | {h.hypothesis_id}: {h.title} | {h.category.value} | {h.root_cause_service or '-'} | "
                 f"{h.trigger_change_id or '-'} | {h.confidence:.2f} ({h.confidence_label}) | {h.status} |")
    by_id = {f.id: f for f in r.findings}

    def cite(ids: list[str]) -> str:
        return "\n".join(f"  - `{i}` {'✔' if by_id[i].verified else '✘ unverified'} {by_id[i].statement}" for i in ids if i in by_id) or "  - (none)"

    for h in r.ranked_hypotheses:
        L += ["", f"### {h.hypothesis_id}: {h.title}", h.statement, "", "**Causal chain**"]
        L += [f"{n}. {s.claim} " + (f"[{', '.join(s.finding_ids)}]" if s.finding_ids else "*(assumed)*") for n, s in enumerate(h.causal_chain, 1)] or ["(none given)"]
        L += ["", "**Supporting evidence**", cite(h.supporting_evidence), "**Contradicting evidence**", cite(h.contradicting_evidence)]
        if h.assumptions:
            L += ["**Assumptions**"] + [f"  - {a}" for a in h.assumptions]
        if h.rejection_reason:
            L += [f"**Rejected because:** {h.rejection_reason}"]
    L += ["", "## Facts (verified observations)"] + ([f"- {f.statement} [{', '.join(f.finding_ids)}]" for f in r.facts] or ["- (none)"])
    L += ["", "## Assumptions (not verified)"] + ([f"- {a}" for a in r.assumptions] or ["- (none)"])
    L += ["", "## Timeline"] + ([f"- `{t.time}` {t.event} [{', '.join(t.finding_ids)}]" for t in r.timeline] or ["- (none)"])
    L += ["", "## Recommended actions"]
    for prio in ("immediate", "short_term", "long_term"):
        for a in [a for a in r.recommended_actions if a.priority == prio]:
            L.append(f"- **[{prio}]** {a.action} -- {a.rationale}" + (f" [{', '.join(a.finding_ids)}]" if a.finding_ids else ""))
    L += ["", "## Data gaps"] + ([f"- {g}" for g in r.data_gaps] or ["- (none)"])
    if r.what_would_change_the_conclusion:
        L += ["", "## What would change this conclusion"] + [f"- {w}" for w in r.what_would_change_the_conclusion]
    if r.warnings:
        L += ["", "## Integrity warnings (set by code)"] + [f"- {w}" for w in r.warnings]
    s = r.stats
    L += ["", "## Investigation stats",
          f"- latency {s.get('latency_s')}s, LLM calls {s.get('llm_calls')}, tokens {s.get('total_tokens')} "
          f"(in {s.get('prompt_tokens')} / out {s.get('completion_tokens')})",
          f"- tool calls {s.get('tool_calls')} ({s.get('tool_failures')} failed, {s.get('tool_partial')} partial, "
          f"{s.get('tool_recovered_by_retry')} recovered by retry), findings {s.get('findings_verified')}/{s.get('findings')} verified"]
    return "\n".join(L) + "\n"
