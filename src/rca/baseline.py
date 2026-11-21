"""Single-agent baseline: one ReAct loop with all six tools, the same tool executor (so the
same faults, retries and budget), the same evidence rules and the same report assembly."""
from __future__ import annotations

from .agents import prompts as P
from .agents.base import AgentError, AgentRuntime, run_agent
from .assemble import build_report, decide_outcome, rank
from .models import BaselineSubmission, FinalReport, FindingIn, Hypothesis
from .state import BudgetExceeded
from .tools.base import TOOL_SPECS


async def run_baseline(rt: AgentRuntime) -> FinalReport:
    st = rt.state
    b = st.budget
    st.emit("start", incident=st.incident.id, system="single_agent")
    system = P.BASELINE.format(max_steps=b.max_baseline_steps, max_calls=b.max_tool_calls)
    try:
        sub = await run_agent(rt, "baseline", system, P.incident_block(st.incident), BaselineSubmission,
                              tools=list(TOOL_SPECS), max_steps=b.max_baseline_steps, max_tool_calls=b.max_tool_calls,
                              meta={"state": st})
        st.termination_reason = "agent_submitted"
    except (AgentError, BudgetExceeded) as ex:
        st.termination_reason = f"baseline_failed:{str(ex)[:100]}"
        st.warnings.append(st.termination_reason)
        sub = BaselineSubmission(findings=[], hypotheses=[], outcome="inconclusive",
                                 summary="The single agent did not produce a result within its limits.")

    # Re-key the agent's local finding ids onto verified Findings.
    ids = st.add_findings("baseline", None, [FindingIn(**bf.model_dump(exclude={"id"})) for bf in sub.findings])
    local = {bf.id.strip(): fid for bf, fid in zip(sub.findings, ids)}

    def remap(ids: list[str]) -> list[str]:
        # An id the agent never declared must not fall through to a global id of the same name
        # (local 'F2' may have been re-keyed to global 'F1'); mark it so it is dropped downstream.
        return [local.get(i.strip(), f"unknown:{i.strip()}") for i in ids]

    hyps = []
    for i, h in enumerate(sub.hypotheses, 1):
        d = h.model_dump()
        d.update(id=f"H{i}", supporting=[x for x in remap(h.supporting) if x in st.findings],
                 contradicting=[x for x in remap(h.contradicting) if x in st.findings])
        for step in d["causal_chain"]:
            step["finding_ids"] = [x for x in remap(step["finding_ids"]) if x in st.findings]
        hyps.append(Hypothesis(**d))
    for f in sub.facts:
        f.finding_ids = remap(f.finding_ids)
    for t in sub.timeline:
        t.finding_ids = remap(t.finding_ids)
    for a in sub.recommended_actions:
        a.finding_ids = remap(a.finding_ids)

    ranked = rank(hyps, st.findings, cap=None)                      # no critic => no cap; same 0.3 ceiling rule applies
    outcome, why = decide_outcome(ranked, b.conclusive_threshold, "inconclusive" if sub.outcome == "inconclusive" else None)
    return build_report(incident_id=st.incident.id, system="single_agent", ranked=ranked, findings=st.findings,
                        outcome=outcome, summary=sub.summary, facts=sub.facts, assumptions=sub.assumptions,
                        timeline=sub.timeline, actions=sub.recommended_actions, data_gaps=sub.data_gaps,
                        what_would_change=[], termination_reason=st.termination_reason, stats=st.stats(), warnings=st.warnings)
