"""Structured contracts exchanged between agents.

Every hand-off in the system is one of these pydantic models -- agents never pass
free-form prose to each other.  IDs (F*, H*, T*, tc-*) are always assigned by code,
never by the LLM, so references can be validated mechanically.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class RootCauseCategory(str, Enum):
    BAD_DEPLOYMENT = "bad_deployment"            # a code release introduced the fault
    CONFIG_CHANGE = "config_change"              # config / feature-flag change introduced the fault
    DEPENDENCY_FAILURE = "dependency_failure"    # a third-party / external dependency degraded
    RESOURCE_EXHAUSTION = "resource_exhaustion"  # memory, disk, connections, file handles ran out
    CAPACITY_SATURATION = "capacity_saturation"  # legitimate load exceeded provisioned capacity
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"  # node / cache / broker / network failure
    CREDENTIAL_EXPIRY = "credential_expiry"      # certificate / token / secret expired
    DATA_ISSUE = "data_issue"                    # malformed / poison data or schema mismatch
    UNKNOWN = "unknown"


AgentName = Literal["orchestrator", "observability", "dependency", "reasoning", "critic", "reporting", "baseline"]
SpecialistName = Literal["observability", "dependency"]


# --------------------------------------------------------------------------- incident
class IncidentReport(BaseModel):
    id: str
    title: str
    severity: str
    reported_at: str
    now: str = Field(description="Current simulated time; telemetry exists up to this instant")
    reporter: str
    affected_service: str = Field(description="Service where the symptom was *reported* (not necessarily the cause)")
    description: str
    symptoms: list[str] = []


# --------------------------------------------------------------------------- evidence
class EvidenceRef(BaseModel):
    """A pointer into raw tool output.  `quote` must be a short verbatim excerpt of that
    output; the grounding verifier checks it mechanically."""
    call_id: str = Field(description="call_id of the tool call that produced the data, e.g. 'tc-007'")
    quote: str = Field(description="Short verbatim excerpt (id, log line fragment, number) copied from that tool output")


class FindingIn(BaseModel):
    """What a specialist submits.  Code turns it into a `Finding` with an id + verification."""
    statement: str = Field(description="One specific, checkable observation")
    kind: Literal["observation", "inference"] = Field(
        description="'observation' = directly visible in tool output; 'inference' = your interpretation")
    refs: list[EvidenceRef] = Field(default_factory=list, description="Evidence backing the statement")
    services: list[str] = Field(default_factory=list)
    time: Optional[str] = Field(default=None, description="ISO timestamp the observation refers to, if any")


class Finding(FindingIn):
    id: str
    agent: str
    task_id: Optional[str] = None
    verified: bool = False          # set by grounding.verify_finding -- never by an LLM
    verification_note: str = ""


class Gap(BaseModel):
    """Evidence an agent tried to obtain but could not (tool failure, missing telemetry...)."""
    description: str
    tool: Optional[str] = None
    impact: str = Field(default="", description="Which question remains unanswered because of this gap")
    agent: str = ""


class SpecialistResult(BaseModel):
    """Structured output of the Observability / Dependency agents."""
    findings: list[FindingIn] = Field(description="Evidence-backed observations; may be empty if nothing could be retrieved")
    gaps: list[Gap] = Field(default_factory=list)
    answer: str = Field(description="Direct 1-3 sentence answer to the task objective")
    suggested_followups: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- hypotheses
class CausalStep(BaseModel):
    """One hop of the proposed propagation path, e.g. 'deploy d-12 -> pool exhaustion'."""
    claim: str
    finding_ids: list[str] = Field(default_factory=list, description="Findings supporting this hop; empty = assumed")


class HypothesisIn(BaseModel):
    id: Optional[str] = Field(default=None, description="Existing hypothesis id when revising (e.g. 'H2'); omit for new")
    title: str
    statement: str = Field(description="What failed, where, and why")
    category: RootCauseCategory
    root_cause_service: Optional[str] = Field(default=None, description="Service/component where the fault originates")
    trigger_change_id: Optional[str] = Field(default=None, description="Deployment/config-change id that triggered it, if any")
    causal_chain: list[CausalStep] = Field(default_factory=list)
    supporting: list[str] = Field(default_factory=list, description="Finding ids that support it")
    contradicting: list[str] = Field(default_factory=list, description="Finding ids that contradict it")
    assumptions: list[str] = Field(default_factory=list, description="Unverified beliefs the hypothesis depends on")
    confidence: float = Field(ge=0, le=1)
    status: Literal["active", "rejected"] = "active"
    rejection_reason: Optional[str] = None


class Hypothesis(HypothesisIn):
    id: str
    history: list[dict[str, Any]] = Field(default_factory=list)  # confidence per round, for the audit trail


class ReasoningResult(BaseModel):
    hypotheses: list[HypothesisIn]
    discriminating_questions: list[str] = Field(
        default_factory=list, description="Evidence that would best separate the leading hypotheses")
    notes: str = ""


# --------------------------------------------------------------------------- critique
class Challenge(BaseModel):
    hypothesis_id: str
    issue: str
    severity: Literal["minor", "major", "blocking"]


class EvidenceRequest(BaseModel):
    agent: SpecialistName
    question: str = Field(description="A concrete, answerable investigation request")
    rationale: str = ""
    hypothesis_ids: list[str] = Field(default_factory=list)


class Critique(BaseModel):
    verdict: Literal["accept", "needs_evidence", "inconclusive"] = Field(
        description="accept = top hypothesis is adequately proven; needs_evidence = specific obtainable evidence is "
                    "missing; inconclusive = evidence cannot separate hypotheses and no further tool query would help")
    summary: str
    challenges: list[Challenge] = Field(default_factory=list)
    evidence_requests: list[EvidenceRequest] = Field(default_factory=list)
    confidence_cap: float = Field(ge=0, le=1, description="Maximum defensible confidence in the top hypothesis")
    round: int = 0


# --------------------------------------------------------------------------- tasks
class Task(BaseModel):
    id: str
    agent: SpecialistName
    objective: str
    depends_on: list[str] = Field(default_factory=list)
    origin: Literal["plan", "critic", "fallback"] = "plan"
    round: int = 0
    status: Literal["pending", "running", "done", "failed", "skipped"] = "pending"
    attempts: int = 0
    answer: str = ""
    error: Optional[str] = None
    finding_ids: list[str] = Field(default_factory=list)


class PlannedTask(BaseModel):
    key: str = Field(description="Short local key, e.g. 'a'")
    agent: SpecialistName
    objective: str
    depends_on: list[str] = Field(default_factory=list, description="Keys of tasks whose results this task needs")


class Plan(BaseModel):
    initial_theories: list[str] = Field(default_factory=list, description="Candidate explanations worth testing")
    tasks: list[PlannedTask]


class ReplanDecision(BaseModel):
    decision: Literal["investigate", "finalize"]
    reason: str
    tasks: list[PlannedTask] = Field(default_factory=list)


# --------------------------------------------------------------------------- report
class FactItem(BaseModel):
    statement: str
    finding_ids: list[str]


class TimelineItem(BaseModel):
    time: str
    event: str
    finding_ids: list[str] = Field(default_factory=list)


class Action(BaseModel):
    action: str
    priority: Literal["immediate", "short_term", "long_term"]
    rationale: str
    finding_ids: list[str] = Field(default_factory=list)
    hypothesis_id: Optional[str] = None


class ReportDraft(BaseModel):
    """What the Reporting agent writes.  Hypothesis ranking is *not* re-generated here --
    it is taken from shared state so the reporter cannot drift from the reviewed analysis."""
    outcome: Literal["root_cause_identified", "inconclusive"]
    summary: str
    facts: list[FactItem] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    timeline: list[TimelineItem] = Field(default_factory=list)
    recommended_actions: list[Action] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    what_would_change_the_conclusion: list[str] = Field(default_factory=list)


class RankedHypothesis(BaseModel):
    rank: int
    hypothesis_id: str
    title: str
    statement: str
    category: RootCauseCategory
    root_cause_service: Optional[str]
    trigger_change_id: Optional[str]
    confidence: float
    pre_cap_confidence: Optional[float] = None   # before the critic cap; keeps the margin between hypotheses the cap flattens
    confidence_label: Literal["high", "medium", "low"]
    status: str
    causal_chain: list[CausalStep] = []
    supporting_evidence: list[str] = []
    contradicting_evidence: list[str] = []
    assumptions: list[str] = []
    rejection_reason: Optional[str] = None


class FinalReport(BaseModel):
    incident_id: str
    system: str                      # "multi_agent" | "single_agent"
    outcome: Literal["root_cause_identified", "inconclusive"]
    summary: str
    ranked_hypotheses: list[RankedHypothesis]
    facts: list[FactItem]
    assumptions: list[str]
    timeline: list[TimelineItem]
    recommended_actions: list[Action]
    data_gaps: list[str]
    what_would_change_the_conclusion: list[str] = []
    findings: list[Finding]          # the evidence ledger the report cites
    termination_reason: str = ""
    stats: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)   # set by code, e.g. stripped citations

    @property
    def top(self) -> Optional[RankedHypothesis]:
        active = [h for h in self.ranked_hypotheses if h.status == "active"]
        return active[0] if active else None


# Baseline submits everything in one shot, in the same vocabulary.
class BaselineFinding(FindingIn):
    id: str = Field(description="Local id like 'F1' that hypotheses and facts refer to")


class BaselineSubmission(BaseModel):
    findings: list[BaselineFinding]
    hypotheses: list[HypothesisIn] = Field(description="Ranked best-first")
    outcome: Literal["root_cause_identified", "inconclusive"]
    summary: str
    facts: list[FactItem] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    timeline: list[TimelineItem] = Field(default_factory=list)
    recommended_actions: list[Action] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)


def confidence_label(c: float) -> Literal["high", "medium", "low"]:
    return "high" if c >= 0.75 else "medium" if c >= 0.5 else "low"
