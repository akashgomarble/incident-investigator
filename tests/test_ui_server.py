"""The web UI server: live stream, replay of a saved run, and input validation.  Offline (scripted LLM)."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from rca.ui import server as ui


@pytest.fixture(scope="module")
def base(tmp_path_factory):  # type: ignore[no-untyped-def]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as ex:
        return ex.code, json.load(ex)


def _stream(base: str, job: str) -> tuple[list[dict], bool]:
    events, ended = [], False
    with urllib.request.urlopen(f"{base}/api/stream/{job}", timeout=60) as r:
        assert r.headers["Content-Type"].startswith("text/event-stream")
        for raw in r:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: end"):
                ended = True
            elif line.startswith("data: ") and not ended:
                events.append(json.loads(line[6:]))
    return events, ended


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ui._Paced, "complete", lambda self, *a, **kw: self.inner.complete(*a, **kw))   # no demo pacing
    real = ui.investigate
    monkeypatch.setattr(ui, "investigate", lambda *a, **kw: real(*a, **{**kw, "sim_latency_s": 0.0}))


def test_page_and_meta(base: str) -> None:
    with urllib.request.urlopen(base + "/") as r:
        assert b"Incident Investigator" in r.read()
    with urllib.request.urlopen(base + "/api/meta") as r:
        meta = json.load(r)
    assert len(meta["incidents"]) >= 10 and meta["budget"]["tool_calls"] > 0
    assert "LLM_API_KEY" not in json.dumps(meta)


def test_live_run_streams_events_then_a_report(base: str) -> None:
    code, j = _post(base, "/api/run", {"incident_id": "INC-003", "system": "multi", "llm": "fake", "crash_agent": "observability"})
    assert code == 200
    events, ended = _stream(base, j["job_id"])
    kinds = [e["kind"] for e in events]
    assert ended and kinds[0] == "start" and kinds[-1] == "report"
    for k in ("agent_start", "agent_end", "task_created", "wave", "tool_call", "finding", "hypothesis", "critique", "outcome"):
        assert k in kinds, k
    assert "task_error" in kinds                                    # the injected crash is visible, and recovered from
    tool = next(e for e in events if e["kind"] == "tool_call")
    assert tool["task"] and tool["output"] and tool["stats"]["tool_calls"] >= 1
    finding = next(e for e in events if e["kind"] == "finding")
    assert finding["refs"] and finding["task"]
    report = events[-1]
    assert report["report"]["incident_id"] == "INC-003" and report["truth"]["expected_outcome"]
    assert kinds.count("agent_start") == kinds.count("agent_end")


def test_single_agent_run(base: str) -> None:
    _, j = _post(base, "/api/run", {"incident_id": "INC-001", "system": "single", "llm": "fake"})
    events, ended = _stream(base, j["job_id"])
    assert ended and events[-1]["kind"] == "report" and events[-1]["report"]["system"] == "single_agent"


def test_replay_of_a_saved_run(base: str) -> None:
    recs = [r for r in ui._recordings() if r["system"] == "multi"]
    if not recs:
        pytest.skip("no saved runs in this checkout")
    _, j = _post(base, "/api/replay", {"recording": recs[0]["id"], "speed": 200})
    events, ended = _stream(base, j["job_id"])
    kinds = [e["kind"] for e in events]
    assert ended and kinds[-1] == "report" and "agent_start" in kinds and "tool_call" in kinds
    assert next(e for e in events if e["kind"] == "tool_call")["output"]


def test_bad_requests_are_rejected(base: str) -> None:
    assert _post(base, "/api/run", {"incident_id": "../../etc", "system": "multi", "llm": "fake"})[0] == 400
    assert _post(base, "/api/run", {"incident_id": "INC-001", "system": "nope", "llm": "fake"})[0] == 400
    assert _post(base, "/api/replay", {"recording": "../../../etc/passwd"})[0] == 400
    assert _post(base, "/api/stop/unknown", {})[0] == 404
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(base + "/api/stream/unknown")


def test_synthesised_agent_activity_for_old_traces() -> None:
    old = [{"t": 0, "kind": "start"}, {"t": 5, "kind": "task_created"}, {"t": 9, "kind": "task_done"},
           {"t": 20, "kind": "hypothesis"}, {"t": 30, "kind": "critique"}, {"t": 30, "kind": "outcome"}, {"t": 40, "kind": "done"}]
    out = ui._synth_agent_events(old)
    marks = [(e["kind"], e["agent"]) for e in out if e["kind"].startswith("agent_")]
    assert marks == [("agent_start", "orchestrator"), ("agent_end", "orchestrator"), ("agent_start", "reasoning"),
                     ("agent_end", "reasoning"), ("agent_start", "critic"), ("agent_end", "critic"),
                     ("agent_start", "reporting"), ("agent_end", "reporting")]
    assert ui._synth_agent_events(out) == out                      # already annotated traces are left alone
