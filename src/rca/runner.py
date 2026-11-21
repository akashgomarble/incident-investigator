"""Public entry point: run one investigation with either system."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .agents.base import AgentFaults, AgentRuntime
from .agents.orchestrator import Orchestrator
from .baseline import run_baseline
from .config import Budget, Settings
from .llm.base import LLMClient
from .models import FinalReport
from .state import InvestigationState
from .tools.datastore import DataStore
from .tools.executor import ToolExecutor
from .tools.faults import FaultInjector


@dataclass
class RunResult:
    report: FinalReport
    state: InvestigationState

    def trace(self) -> dict[str, Any]:
        return {"events": self.state.events, "tool_calls": [c.model_dump() for c in self.state.executor.calls],
                "tasks": [t.model_dump() for t in self.state.tasks.values()],
                "critiques": [c.model_dump() for c in self.state.critiques],
                "injected_faults": self.state.executor.injector.injected}

    def save(self, out_dir: Path) -> None:
        from .assemble import render_markdown
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(self.report.model_dump_json(indent=2))
        (out_dir / "report.md").write_text(render_markdown(self.report))
        (out_dir / "trace.json").write_text(json.dumps(self.trace(), indent=1, default=str))


async def investigate(incident_id: str, llm: LLMClient, *, system: str = "multi", settings: Settings | None = None,
                      budget: Budget | None = None, chaos_rate: float = 0.0, agent_faults: dict[str, int] | None = None,
                      use_scripted_faults: bool = True, sim_latency_s: float = 0.01,
                      chaos_hard_rate: float = 0.0, seed: int | None = None,
                      on_event: Callable[[dict[str, Any], InvestigationState], None] | None = None) -> RunResult:
    settings = settings or Settings()
    budget = copy.deepcopy(budget or settings.budget)       # the orchestrator may tighten limits during a run
    store = DataStore(settings.data_dir, incident_id)
    injector = FaultInjector(store.faults if use_scripted_faults else [], chaos_rate=chaos_rate,
                             chaos_hard_rate=chaos_hard_rate, seed=settings.seed if seed is None else seed)
    executor = ToolExecutor(store, injector, budget, sim_latency_s=sim_latency_s)
    state = InvestigationState(store.incident, executor, budget, verbose=settings.verbose)
    if on_event:
        state.on_event = lambda ev: on_event(ev, state)
    rt = AgentRuntime(llm=llm, state=state, faults=AgentFaults(crash=dict(agent_faults or {})))
    if system in ("multi", "multi_agent"):
        report = await Orchestrator(rt).investigate()
    elif system in ("nocritic", "multi_nocritic"):          # ablation: the same team without the Critic
        report = await Orchestrator(rt, use_critic=False).investigate()
    elif system in ("single", "single_agent", "baseline"):
        report = await run_baseline(rt)
    else:
        raise ValueError(f"unknown system {system!r}")
    return RunResult(report=report, state=state)
