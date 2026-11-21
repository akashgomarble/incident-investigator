import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from rca.config import Budget, Settings  # noqa: E402
from rca.tools.datastore import DataStore  # noqa: E402
from rca.tools.executor import ToolExecutor  # noqa: E402
from rca.tools.faults import FaultInjector  # noqa: E402

DATA = ROOT / "data"


def run(coro):
    return asyncio.run(coro)


def make_executor(incident: str, *, faults: bool = True, budget: Budget | None = None, **kw) -> ToolExecutor:
    ds = DataStore(DATA, incident)
    return ToolExecutor(ds, FaultInjector(ds.faults if faults else []), budget or Budget(), sim_latency_s=0.0, sim_timeout_s=0.0, **kw)


def window(ds: DataStore, minutes: int = 120) -> dict:
    from datetime import timedelta
    from rca.tools.datastore import fmt_ts, parse_ts
    now = parse_ts(ds.incident.now)
    return {"start": fmt_ts(now - timedelta(minutes=minutes)), "end": fmt_ts(now)}


@pytest.fixture
def settings() -> Settings:
    return Settings()
