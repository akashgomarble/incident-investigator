"""The five non-orchestrator agents.  Each one is: build context from shared state -> run the
agent loop -> write its structured result back to shared state."""
from __future__ import annotations

from ..models import Critique, Gap, ReasoningResult, ReportDraft, SpecialistResult, Task
from ..state import InvestigationState
from . import prompts as P
from .base import AgentRuntime, run_agent

SPECIALIST_TOOLS = {
    "observability": ["query_logs", "query_metrics"],
    "dependency": ["get_deployments", "get_config_changes", "get_service_dependencies", "search_historical_incidents"],
}
SPECIALIST_PROMPT = {"observability": P.OBSERVABILITY, "dependency": P.DEPENDENCY}
MAX_TASK_TOOL_CALLS = 8


# ------------------------------------------------------------------------ lint (code-side critic aid)
def lint(state: InvestigationState) -> list[str]:
    """Deterministic checks handed to the Critic so it starts from mechanical facts, not vibes."""
    out: list[str] = []
    for h in state.ranked():
        if h.status != "active":
            continue
        unverified = [f for f in h.supporting if not state.findings[f].verified]
        if unverified:
            out.append(f"{h.id} cites UNVERIFIED findings as support: {unverified}")
        if not [f for f in h.supporting if state.findings[f].verified]:
            out.append(f"{h.id} has no verified supporting finding")
        assumed = [s.claim for s in h.causal_chain if not s.finding_ids]
        if assumed:
            out.append(f"{h.id} causal chain has {len(assumed)} hop(s) with no evidence: {assumed}")
        if not h.causal_chain:
            out.append(f"{h.id} has no causal chain")
        if h.confidence >= 0.75 and not h.contradicting and len([x for x in state.hypotheses.values() if x.status == 'active']) > 1:
            out.append(f"{h.id} is at confidence {h.confidence:.2f} while competing hypotheses are still active")
    failed = [t for t in state.tasks.values() if t.status == "failed"]
    if failed:
        out.append("tasks that FAILED (their questions are unanswered): " + "; ".join(f"{t.id} {t.objective}" for t in failed))
    out.extend(state.warnings[-5:])
    return out


# ------------------------------------------------------------------------ specialists
async def run_specialist(rt: AgentRuntime, task: Task) -> SpecialistResult:
    st = rt.state
    upstream = [st.tasks[d] for d in task.depends_on if d in st.tasks]
    parts = [P.incident_block(st.incident), f"\nYOUR TASK ({task.id}, investigation round {task.round}): {task.objective}"]
    if upstream:
        parts.append("\nRESULTS OF TASKS YOU DEPEND ON:")
        for u in upstream:
            parts.append(f"- {u.id} [{u.status}] {u.objective}\n  answer: {u.answer or u.error or '(no answer)'}\n"
                         + st.findings_digest(u.finding_ids))
    known = [f.id for f in st.findings.values() if f.task_id not in task.depends_on]
    if known:
        parts.append("\nALREADY ESTABLISHED BY THE TEAM (do not re-derive; build on it):\n" + st.findings_digest(known))
    if st.hypotheses:
        parts.append("\nHYPOTHESES UNDER TEST:\n" + "\n".join(
            f"- {h.id} ({h.confidence:.2f}) {h.title}" for h in st.ranked() if h.status == "active"))
    budget = min(MAX_TASK_TOOL_CALLS, st.tool_calls_left)
    system = SPECIALIST_PROMPT[task.agent].format(max_steps=st.budget.max_agent_steps, max_calls=budget)
    result = await run_agent(rt, task.agent, system, "\n".join(parts), SpecialistResult,
                             tools=SPECIALIST_TOOLS[task.agent], max_steps=st.budget.max_agent_steps,
                             max_tool_calls=budget, meta={"task": task, "state": st})
    task.finding_ids = st.add_findings(task.agent, task.id, result.findings)
    for g in result.gaps:
        st.add_gap(g, task.agent)
    task.answer = result.answer
    st.open_questions.extend(result.suggested_followups[:3])
    return result


# ------------------------------------------------------------------------ reasoning
async def run_reasoning(rt: AgentRuntime) -> ReasoningResult:
    st = rt.state
    parts = [P.incident_block(st.incident),
             "\nINITIAL THEORIES FROM THE ORCHESTRATOR:\n" + ("\n".join(f"- {t}" for t in st.theories) or "(none)"),
             "\nFINDINGS (the only admissible evidence):\n" + st.findings_digest(),
             "\nGAPS (what the team could not determine):\n" + st.gaps_digest(),
             "\nTOOL RELIABILITY THIS INVESTIGATION:\n" + st.tool_health_digest()]
    if st.hypotheses:
        parts.append("\nYOUR PREVIOUS HYPOTHESES (revise these; keep ids):\n" + st.hypotheses_digest())
    if st.critiques:
        c = st.critiques[-1]
        parts.append(f"\nCRITIC'S LAST REVIEW (verdict={c.verdict}, confidence_cap={c.confidence_cap}):\n{c.summary}\n"
                     + "\n".join(f"- [{ch.severity}] {ch.hypothesis_id}: {ch.issue}" for ch in c.challenges))
    result = await run_agent(rt, "reasoning", P.REASONING, "\n".join(parts), ReasoningResult, meta={"state": st})
    st.upsert_hypotheses(result.hypotheses)
    st.open_questions = result.discriminating_questions[:5]
    return result


# ------------------------------------------------------------------------ critic
async def run_critic(rt: AgentRuntime) -> Critique:
    st = rt.state
    b = st.budget
    system = P.CRITIC.format(max_tasks=b.max_tasks_per_round, round=st.round, max_rounds=b.max_rounds,
                             calls_left=st.tool_calls_left)
    parts = [P.incident_block(st.incident),
             "\nHYPOTHESES (ranked):\n" + st.hypotheses_digest(),
             "\nFINDINGS:\n" + st.findings_digest(),
             "\nGAPS:\n" + st.gaps_digest(),
             "\nTASKS RUN SO FAR:\n" + st.tasks_digest(),
             "\nTOOL RELIABILITY:\n" + st.tool_health_digest(),
             "\nAUTOMATED CHECKS:\n" + ("\n".join(f"- {x}" for x in lint(st)) or "(no mechanical issues found)"),
             "\nREASONING AGENT'S OPEN QUESTIONS:\n" + ("\n".join(f"- {q}" for q in st.open_questions) or "(none)")]
    if st.critiques:
        parts.append("\nYOUR PREVIOUS REVIEWS:\n" + "\n".join(
            f"- round {c.round}: {c.verdict} (cap {c.confidence_cap}) {c.summary}" for c in st.critiques))
    if st.round >= b.max_rounds:
        parts.append("\nThis is the FINAL review round: no further evidence can be gathered. Choose 'accept' or 'inconclusive'.")
    critique = await run_agent(rt, "critic", system, "\n".join(parts), Critique, meta={"state": st})
    critique.round = st.round
    known = set(st.hypotheses)
    critique.challenges = [c for c in critique.challenges if c.hypothesis_id in known]
    st.critiques.append(critique)
    st.emit("critique", round=st.round, verdict=critique.verdict, cap=critique.confidence_cap,
            requests=len(critique.evidence_requests), summary=critique.summary)
    return critique


# ------------------------------------------------------------------------ reporting
async def run_reporting(rt: AgentRuntime, ranked_text: str, outcome_hint: str) -> ReportDraft:
    st = rt.state
    parts = [P.incident_block(st.incident),
             f"\nREVIEWED RANKING (fixed; confidences already capped by the Critic). Expected outcome: {outcome_hint}\n{ranked_text}",
             "\nFINDINGS:\n" + st.findings_digest(),
             "\nGAPS:\n" + st.gaps_digest(),
             "\nTOOL RELIABILITY:\n" + st.tool_health_digest(),
             "\nCRITIC REVIEWS:\n" + ("\n".join(f"- round {c.round}: {c.verdict}: {c.summary}" for c in st.critiques) or "(none)"),
             f"\nINVESTIGATION ENDED BECAUSE: {st.termination_reason}"]
    return await run_agent(rt, "reporting", P.REPORTING, "\n".join(parts), ReportDraft, meta={"state": st})


def gap_for_failed_task(task: Task, err: str) -> Gap:
    return Gap(description=f"{task.agent} agent could not complete task {task.id} ('{task.objective}'): {err[:160]}",
               impact="the question this task was meant to answer is unanswered", agent="orchestrator")
