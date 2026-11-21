"""Read-only access to one incident's simulated telemetry.

NOTE: this class deliberately never opens `ground_truth.json`.  Ground truth is only read
by the evaluation scorer (eval/scoring.py); a unit test enforces that separation.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path
from typing import Any

from ..models import IncidentReport

AGENT_VISIBLE_FILES = ("incident.json", "logs.jsonl", "metrics.json", "deployments.json", "config_changes.json", "faults.json")


def parse_ts(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DataStore:
    def __init__(self, data_dir: Path, incident_id: str):
        self.root = Path(data_dir)
        self.dir = self.root / "incidents" / incident_id
        if not self.dir.exists():
            raise FileNotFoundError(f"unknown incident {incident_id!r} under {self.root}")
        self.incident_id = incident_id

    def _json(self, name: str, shared: bool = False) -> Any:
        p = (self.root if shared else self.dir) / name
        return json.loads(p.read_text())

    @cached_property
    def incident(self) -> IncidentReport:
        return IncidentReport(**self._json("incident.json"))

    @cached_property
    def logs(self) -> list[dict[str, Any]]:
        out = [json.loads(l) for l in (self.dir / "logs.jsonl").read_text().splitlines() if l.strip()]
        for e in out:
            e["_t"] = parse_ts(e["ts"])
        return out

    @cached_property
    def metrics(self) -> dict[str, Any]:
        return self._json("metrics.json")

    @cached_property
    def deployments(self) -> list[dict[str, Any]]:
        return self._json("deployments.json")

    @cached_property
    def config_changes(self) -> list[dict[str, Any]]:
        return self._json("config_changes.json")

    @cached_property
    def faults(self) -> list[dict[str, Any]]:
        p = self.dir / "faults.json"
        return json.loads(p.read_text()) if p.exists() else []

    @cached_property
    def topology(self) -> dict[str, Any]:
        return self._json("topology.json", shared=True)

    @cached_property
    def history(self) -> list[dict[str, Any]]:
        return self._json("historical_incidents.json", shared=True)

    # ---- helpers
    @property
    def data_range(self) -> dict[str, str]:
        m = self.metrics
        start = parse_ts(m["start"])
        end = start + timedelta(seconds=m["step_s"] * (m["points"] - 1))
        return {"start": fmt_ts(start), "end": fmt_ts(end)}

    def services(self) -> list[str]:
        return [n["name"] for n in self.topology["nodes"]]

    def node(self, name: str) -> dict[str, Any] | None:
        return next((n for n in self.topology["nodes"] if n["name"] == name), None)


def list_incidents(data_dir: Path) -> list[str]:
    return sorted(p.name for p in (Path(data_dir) / "incidents").iterdir() if p.is_dir())
