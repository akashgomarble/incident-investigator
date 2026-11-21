"""Orchestrator: plans the investigation, schedules specialist tasks as a dependency graph
(independent tasks in parallel), runs the reasoning <-> critic feedback loop, and decides
when to stop.  The LLM proposes plans; code enforces every limit."""
from __future__ import annotations

import asyncio
import re

from ..assemble import UNREVIEWED, build_report, decide_outcome, rank, render_ranked
from ..models import (Critique, FinalReport, Plan, PlannedTask, ReplanDecision, ReportDraft, Task)
from ..state import BudgetExceeded, InvestigationState
from . import prompts as P
from .base import AgentError, AgentRuntime, run_agent
from .team import gap_for_failed_task, run_critic, run_reasoning, run_reporting, run_specialist

RECOVERABLE = (AgentError,)


def default_plan(affected: str) -> Plan:
    """Used when the planner LLM fails: a generic, service-agnostic plan."""
    return Plan(initial_theories=["a recent change (deploy/config) introduced the fault",
                                  "a dependency or shared component of the reported service is failing"],
                tasks=[
                    PlannedTask(key="a", agent="observability", objective=(
                        f"Establish when the symptoms on {affected} started, the exact error signatures, and which key metrics changed versus the hour before.")),
                    PlannedTask(key="b", agent="dependency", objective=(
                        f"List all deployments and config changes across all services in the 6 hours before the report, and map the upstream/downstream dependencies of {affected} including shared components.")),
                    PlannedTask(key="c", agent="observability", depends_on=["b"], objective=(
                        f"Check logs and metrics of the direct dependencies of {affected} found by the dependency mapping for failures starting at or before the symptom onset.")),
                ])


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


class Orchestrator:
    def __init__(self, rt: AgentRuntime, use_critic: bool = True):
        self.rt, self.st = rt, rt.state
        self.use_critic = use_critic        # False only for the no-Critic ablation in the evaluation

    # ------------------------------------------------------------------ planning
    def _register(self, planned: list[PlannedTask], origin: str) -> list[Task]:
        """Validate an LLM-proposed task list (dedupe, cap, resolve dependency keys, no cycles)."""
        st = self.st
        existing = {_norm(t.objective): t.id for t in st.tasks.values()}
        key_to_id: dict[str, str] = {}
        created: list[Task] = []
        for p in planned:
            if len(created) >= st.budget.max_tasks_per_round:     # the cap counts tasks created, not duplicates dropped
                break
            dup = existing.get(_norm(p.objective))
            if dup:
                key_to_id[p.key] = dup      # tasks depending on the duplicate get the original's result instead
                st.emit("task_deduped", objective=p.objective)
                continue
            # only earlier tasks of this batch (or existing task ids) may be dependencies => acyclic by construction
            deps = list(dict.fromkeys([key_to_id[d] for d in p.depends_on if d in key_to_id]
                                      + [d for d in p.depends_on if d in st.tasks]))
            dropped = [d for d in p.depends_on if d not in key_to_id and d not in st.tasks]
            if dropped:     # a forward reference or an unknown key: the task runs without waiting, and says so
                st.emit("dependency_dropped", objective=p.objective, unknown=dropped)
                st.warnings.append(f"task '{p.objective[:60]}' named unknown or later dependencies {dropped}; they were ignored")
            t = st.add_task(p.agent, p.objective, deps, origin)
            key_to_id[p.key] = t.id
            existing[_norm(p.objective)] = t.id
            created.append(t)
        return created

    async def plan(self) -> None:
        st = self.st
        user = P.incident_block(st.incident)
        try:
            plan = await run_agent(self.rt, "orchestrator", P.ORCHESTRATOR_PLAN, user, Plan, meta={"state": st, "phase": "plan"})
            if not plan.tasks:
                raise AgentError("planner returned no tasks")
            origin = "plan"
        except RECOVERABLE as ex:
            st.emit("recovery", what="planner failed; using default plan", error=str(ex)[:160])
            st.warnings.append(f"planner failed ({str(ex)[:80]}); default plan used")
            plan, origin = default_plan(st.incident.affected_service), "fallback"
        st.theories = plan.initial_theories
        self._register(plan.tasks, origin)

    async def replan(self, critique: Critique) -> list[Task]:
        st = self.st
        system = P.ORCHESTRATOR_REPLAN.format(max_tasks=st.budget.max_tasks_per_round)
        user = "\n".join([
            P.incident_block(st.incident),
            f"\nCRITIC VERDICT: {critique.verdict} -- {critique.summary}",
            "EVIDENCE REQUESTS:\n" + "\n".join(f"- to {r.agent}: {r.question} (why: {r.rationale})" for r in critique.evidence_requests),
            "\nTASKS ALREADY RUN:\n" + st.tasks_digest(),
            "\nTOOL RELIABILITY:\n" + st.tool_health_digest(),
            f"\nBudget: {st.tool_calls_left} tool calls left; this would be round {st.round + 1} of {st.budget.max_rounds}."])
        try:
            d = await run_agent(self.rt, "orchestrator", system, user, ReplanDecision, meta={"state": st, "phase": "replan", "critique": critique})
            if d.decision == "finalize":
                st.emit("replan", decision="finalize", reason=d.reason)
                return []
            planned = d.tasks
        except RECOVERABLE as ex:   # fall back to a mechanical conversion of the critic's requests
            st.emit("recovery", what="replanner failed; converting critic requests directly", error=str(ex)[:160])
            planned = [PlannedTask(key=f"r{i}", agent=r.agent, objective=r.question) for i, r in enumerate(critique.evidence_requests)]
        st.round += 1
        created = self._register(planned, "critic")
        if not created:         # everything was a duplicate: no new round actually starts
            st.round -= 1
        return created

    # ------------------------------------------------------------------ scheduling
    async def _run_task(self, task: Task, sem: asyncio.Semaphore) -> None:
        st = self.st
        async with sem:
            for attempt in range(1, st.budget.max_task_attempts + 1):
                task.attempts, task.status = attempt, "running"
                st.emit("task_start", id=task.id, agent=task.agent, attempt=attempt)
                try:
                    await run_specialist(self.rt, task)
                    task.status = "done"
                    st.emit("task_done", id=task.id, findings=len(task.finding_ids), answer=task.answer)
                    return
                except BudgetExceeded as ex:
                    task.status, task.error = "failed", f"budget exhausted: {ex}"
                    break
                except RECOVERABLE as ex:
                    task.error = str(ex)
                    st.emit("task_error", id=task.id, attempt=attempt, error=str(ex)[:200])
                except Exception as ex:  # noqa: BLE001 -- an agent bug must degrade, not abort the investigation
                    task.error = f"{type(ex).__name__}: {ex}"
                    st.emit("task_error", id=task.id, attempt=attempt, error=task.error[:200])
            task.status = "failed"
            st.emit("task_failed", id=task.id, attempts=task.attempts, error=(task.error or "unknown error")[:200])
            st.add_gap(gap_for_failed_task(task, task.error or "unknown error"), "orchestrator")

    async def run_pending(self) -> None:
        """Execute pending tasks wave by wave: every task whose dependencies are settled runs concurrently."""
        st = self.st
        sem = asyncio.Semaphore(st.budget.max_parallel_tasks)
        while True:
            pending = [t for t in st.tasks.values() if t.status == "pending"]
            if not pending:
                return
            settled = {t.id for t in st.tasks.values() if t.status in ("done", "failed", "skipped")}
            ready = [t for t in pending if all(d in settled for d in t.depends_on)]
            reason = None
            if not ready:       # cannot happen (deps are acyclic) -- guard against a deadlock anyway
                reason = "unsatisfiable dependencies"
            elif st.budget_problem() or st.tool_calls_left <= 0:
                reason = "investigation budget exhausted"
            if reason:
                for t in pending:
                    t.status, t.error = "skipped", reason
                    st.emit("task_skipped", id=t.id, reason=reason)
                return
            st.emit("wave", parallel=[t.id for t in ready])
            await asyncio.gather(*(self._run_task(t, sem) for t in ready))

    # ------------------------------------------------------------------ main loop
    async def investigate(self) -> FinalReport:
        st = self.st
        st.emit("start", incident=st.incident.id)
        critique: Critique | None = None
        try:
            await self.plan()
            while True:
                await self.run_pending()
                verified_before = sum(f.verified for f in st.findings.values())

                try:
                    await self._with_retry(lambda: run_reasoning(self.rt), "reasoning")
                except RECOVERABLE as ex:
                    st.warnings.append(f"reasoning agent failed: {str(ex)[:120]}")
                    st.termination_reason = "reasoning_agent_failed"
                    break
                if not self.use_critic:
                    # Ablation: one pass, no review. finalize() then applies the 0.6 'unreviewed' cap.
                    st.warnings.append("critic disabled (ablation); analysis unreviewed, confidence capped at 0.6")
                    st.termination_reason = "critic_disabled"
                    break
                try:
                    critique = await self._with_retry(lambda: run_critic(self.rt), "critic")
                except RECOVERABLE as ex:
                    # No review happened => do not let unreviewed confidence stand.
                    st.warnings.append(f"critic failed ({str(ex)[:80]}); confidence capped at 0.6 as unreviewed")
                    critique = Critique(verdict="accept", summary="critic unavailable; analysis unreviewed", confidence_cap=0.6, round=st.round)
                    st.critiques.append(critique)
                    st.termination_reason = "critic_failed_unreviewed"
                    break

                if critique.verdict == "accept":
                    st.termination_reason = "critic_accepted"
                    break
                if critique.verdict == "inconclusive" and not critique.evidence_requests:
                    st.termination_reason = "critic_inconclusive"
                    break
                if st.round >= st.budget.max_rounds:
                    st.termination_reason = "max_rounds_reached"
                    break
                if st.budget_problem() or st.tool_calls_left <= 0:
                    st.termination_reason = f"budget_exhausted:{st.budget_problem() or 'tool_calls'}"
                    break
                if not critique.evidence_requests:
                    st.termination_reason = "critic_gave_no_actionable_requests"
                    break
                new_tasks = await self.replan(critique)
                if not new_tasks:
                    st.termination_reason = "no_new_evidence_paths"
                    break
                await self.run_pending()
                if sum(f.verified for f in st.findings.values()) == verified_before:
                    # the extra round produced nothing verifiable: let reasoning/critic see the gaps once, then stop
                    st.emit("stall", note="feedback round produced no new verified findings")
                    st.budget.max_rounds = st.round
        except BudgetExceeded as ex:
            st.termination_reason = f"budget_exhausted:{ex}"
        return await self.finalize()

    async def _with_retry(self, fn, name: str):  # type: ignore[no-untyped-def]
        try:
            return await fn()
        except RECOVERABLE as ex:
            self.st.emit("recovery", what=f"{name} agent failed; retrying once", error=str(ex)[:160])
            return await fn()

    # ------------------------------------------------------------------ report
    async def finalize(self) -> FinalReport:
        st = self.st
        final = st.critiques[-1] if st.critiques else None
        cap = final.confidence_cap if final else 0.6
        # No critique at all: by design in the no-Critic ablation (None => the 0.6 cap is the only
        # brake); otherwise a budget or failure cut the run before any review, which blocks a conclusion.
        verdict = final.verdict if final else (UNREVIEWED if self.use_critic else None)
        if final is None and self.use_critic and st.hypotheses:
            st.warnings.append("the run ended before any critic review; confidence capped at 0.6 and outcome forced to inconclusive")
        # Hypotheses revised after the last review (budget ran out between reasoning and critic)
        # are unreviewed: the stale critique's verdict must not vouch for them.
        if final and any(h.history and h.history[-1]["round"] > final.round for h in st.hypotheses.values()):
            st.warnings.append(f"hypotheses were revised after the last critic review (round {final.round}); "
                               "confidence capped at 0.6 and outcome forced to inconclusive as unreviewed")
            cap, verdict = min(cap, 0.6), UNREVIEWED
        ranked = rank(st.ranked(), st.findings, cap)
        outcome, why = decide_outcome(ranked, st.budget.conclusive_threshold, verdict)
        st.emit("outcome", outcome=outcome, why=why, termination=st.termination_reason)
        try:
            draft = await self._with_retry(lambda: run_reporting(self.rt, render_ranked(ranked), f"{outcome} ({why})"), "reporting")
        except (AgentError, BudgetExceeded) as ex:
            st.warnings.append(f"reporting agent failed ({str(ex)[:80]}); minimal report generated by code")
            top = next((r for r in ranked if r.status == "active"), None)
            draft = ReportDraft(outcome=outcome,  # type: ignore[arg-type]
                                summary=(f"Automated fallback summary. Leading hypothesis: {top.title} (confidence {top.confidence:.2f})."
                                         if top else "No hypothesis could be formed.") + f" Outcome: {outcome} ({why}).",
                                data_gaps=[g.description for g in st.gaps])
        if draft.outcome != outcome:
            st.warnings.append(f"reporter wrote outcome={draft.outcome}; overridden to {outcome} by rule ({why})")
        report = build_report(
            incident_id=st.incident.id, system="multi_agent", ranked=ranked, findings=st.findings, outcome=outcome,
            summary=draft.summary, facts=draft.facts, assumptions=draft.assumptions, timeline=draft.timeline,
            actions=draft.recommended_actions, data_gaps=draft.data_gaps or [g.description for g in st.gaps],
            what_would_change=draft.what_would_change_the_conclusion, termination_reason=st.termination_reason,
            stats=st.stats(), warnings=st.warnings)
        st.emit("done", outcome=outcome)
        return report
