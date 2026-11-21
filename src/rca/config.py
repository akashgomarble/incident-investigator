"""Runtime limits.  Every loop in the system is bounded by something in here."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader (no dependency).  Existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Budget:
    # --- investigation-level limits (shared by multi-agent and baseline for a fair comparison)
    max_tool_calls: int = 60            # logical tool calls across the whole investigation
    max_llm_calls: int = 40
    max_total_tokens: int = 400_000
    deadline_s: float = 420.0           # wall-clock
    # --- multi-agent loop
    max_rounds: int = 3                 # reasoning<->critic rounds (round 1 + up to 2 feedback loops)
    max_tasks_per_round: int = 3
    max_task_attempts: int = 2          # agent-level retry when a specialist crashes
    max_parallel_tasks: int = 4
    # --- per-agent tool loop
    max_agent_steps: int = 6            # LLM turns inside one specialist task
    max_baseline_steps: int = 16
    # --- tool executor
    tool_timeout_s: float = 2.0
    tool_max_retries: int = 2           # retries after the first attempt, for retryable errors only
    tool_backoff_s: float = 0.05
    breaker_threshold: int = 3          # consecutive hard failures of one tool before the circuit opens
    # --- LLM structured-output repair
    llm_max_repairs: int = 2
    # --- decision thresholds
    conclusive_threshold: float = 0.5   # top-hypothesis confidence below this => inconclusive


@dataclass
class Settings:
    budget: Budget = field(default_factory=Budget)
    data_dir: Path = Path(__file__).resolve().parents[2] / "data"
    seed: int = 7
    verbose: bool = False
