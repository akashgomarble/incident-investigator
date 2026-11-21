"""Local web UI (`rca serve`): watch an investigation as it happens.

A POST starts an investigation in a background thread; every event the agents write to the shared
state is pushed to the browser over Server-Sent Events.  A saved run (any directory holding
report.json + trace.json) can be replayed through the same stream, so the UI works without an API
key.  Stdlib only, bound to 127.0.0.1: a local demo tool, not a public service.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..assemble import render_markdown
from ..config import Budget, Settings
from ..models import FinalReport
from ..runner import investigate
from ..state import InvestigationState
from ..tools.datastore import DataStore, list_incidents

ROOT = Settings().data_dir.parent
PAGE = Path(__file__).with_name("index.html")
OUTPUT_PREVIEW = 6000          # characters of raw tool output sent to the browser per call
JOBS: dict[str, "Job"] = {}


class Job:
    """One live or replayed investigation: an append-only event list that stream handlers tail."""

    def __init__(self, job_id: str, info: dict[str, Any]):
        self.id, self.info = job_id, info
        self.events: list[dict[str, Any]] = []
        self.cond = threading.Condition()
        self.done = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task[Any] | None = None
        self.stop = threading.Event()

    def push(self, ev: dict[str, Any]) -> None:
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def finish(self) -> None:
        with self.cond:
            self.done = True
            self.cond.notify_all()


# ------------------------------------------------------------------ enrichment
def _truth(incident_id: str, report: FinalReport, tool_calls: list[Any]) -> dict[str, Any] | None:
    """The answer key and the evaluation scorer's verdict.  Shown only after the run has finished."""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from eval.scoring import load_truth, score          # the same scorer the evaluation uses
        truth = load_truth(Settings().data_dir, incident_id)
        s = score(report, truth, tool_calls)
        return {"expected_outcome": truth["expected_outcome"], "category": truth.get("category"),
                "services": truth.get("root_cause_services", []), "trigger_change_id": truth.get("trigger_change_id"),
                "narrative": truth.get("narrative", ""), "correct": bool(s.get("correct")),
                "wrong_confident": bool(s.get("wrong_confident")), "abstained": bool(s.get("abstained"))}
    except Exception:  # noqa: BLE001 -- the answer key is a convenience; never fail a run over it
        traceback.print_exc()
        return None


def _enrich_live(ev: dict[str, Any], st: InvestigationState) -> dict[str, Any]:
    """Add the detail a viewer wants (raw tool output, quotes, critique items) to a trace event."""
    out = dict(ev)
    kind = ev["kind"]
    if kind == "tool_call":
        rec = st.executor.record(ev["call_id"])
        if rec:
            out.update(output=rec.output[:OUTPUT_PREVIEW], output_len=len(rec.output), latency_ms=rec.latency_ms)
    elif kind == "finding":
        f = st.findings.get(ev["id"])
        if f:
            out.update(finding_kind=f.kind, refs=[r.model_dump() for r in f.refs], note=f.verification_note,
                       services=f.services, time=f.time)
    elif kind == "hypothesis":
        h = st.hypotheses.get(ev["id"])
        if h:
            out.update(statement=h.statement, category=h.category.value, service=h.root_cause_service,
                       trigger=h.trigger_change_id, supporting=h.supporting, contradicting=h.contradicting,
                       chain=[s.model_dump() for s in h.causal_chain], assumptions=h.assumptions,
                       rejection_reason=h.rejection_reason, round=st.round)
    elif kind == "critique" and st.critiques:
        c = st.critiques[-1]
        out.update(challenges=[x.model_dump() for x in c.challenges],
                   evidence_requests=[x.model_dump() for x in c.evidence_requests])
    elif kind == "task_created":
        out["round"] = st.round
    out["stats"] = {"tool_calls": st.executor.logical_calls, "llm_calls": st.llm_calls, "tokens": st.total_tokens,
                    "round": st.round}
    return out


def _synth_agent_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Traces recorded before agent_start/agent_end existed: infer who was working from the event order."""
    if any(e["kind"] == "agent_start" for e in events) or not events:
        return events
    def mark(kind: str, agent: str, t: float) -> dict[str, Any]:
        return {"t": t, "kind": kind, "agent": agent, "task": None, "inferred": True}

    if events[0].get("system") == "single_agent":
        return [events[0], mark("agent_start", "baseline", events[0]["t"]), *events[1:],
                mark("agent_end", "baseline", events[-1]["t"])]
    out: list[dict[str, Any]] = []
    orch = False
    for i, e in enumerate(events):
        k = e["kind"]
        prev = events[i - 1] if i else None
        nxt = events[i + 1] if i + 1 < len(events) else None
        if orch and k in ("task_created", "replan", "outcome"):
            out.append(mark("agent_end", "orchestrator", e["t"]))
            orch = False
        if k == "hypothesis" and prev and prev["kind"] != "hypothesis":
            out.append(mark("agent_start", "reasoning", prev["t"]))
        if k == "critique":
            out.append(mark("agent_end", "critic", e["t"]))
        if k == "done":
            out.append(mark("agent_end", "reporting", e["t"]))
        out.append(e)
        if k == "start":
            out.append(mark("agent_start", "orchestrator", e["t"]))
            orch = True
        if k == "hypothesis" and (not nxt or nxt["kind"] != "hypothesis"):
            out += [mark("agent_end", "reasoning", e["t"]), mark("agent_start", "critic", e["t"])]
        if k == "critique" and nxt and nxt["kind"] in ("task_created", "replan", "task_deduped", "recovery"):
            out.append(mark("agent_start", "orchestrator", e["t"]))
            orch = True
        if k == "outcome":
            out.append(mark("agent_start", "reporting", e["t"]))
    return out


# ------------------------------------------------------------------ jobs
class _Paced:
    """The offline script answers instantly; pause briefly per model turn so the run can be followed by eye."""

    def __init__(self, inner: Any, delay_s: float = 0.9):
        self.inner, self.delay_s = inner, delay_s

    async def complete(self, *a: Any, **kw: Any) -> Any:
        await asyncio.sleep(self.delay_s)
        return await self.inner.complete(*a, **kw)


def _run_live(job: Job, incident_id: str, system: str, llm_kind: str, chaos: float, agent_faults: dict[str, int]) -> None:
    async def main() -> None:
        if llm_kind == "fake":
            from ..llm.fake import FakeLLM
            llm: Any = _Paced(FakeLLM())
        else:
            from ..llm.openai_compat import OpenAICompatClient
            llm = OpenAICompatClient()
        try:
            res = await investigate(incident_id, llm, system=system, chaos_rate=chaos, agent_faults=agent_faults,
                                    sim_latency_s=0.35 if llm_kind == "fake" else 0.01,
                                    on_event=lambda ev, st: job.push(_enrich_live(ev, st)))
        finally:
            if hasattr(llm, "aclose"):
                await llm.aclose()
        out = ROOT / "runs" / f"ui-{incident_id}-{system}-{llm_kind}"
        res.save(out)
        job.push({"kind": "report", "t": res.report.stats.get("latency_s"), "report": res.report.model_dump(mode="json"),
                  "markdown": render_markdown(res.report), "saved_to": str(out.relative_to(ROOT)),
                  "truth": _truth(incident_id, res.report, res.state.executor.calls)})

    async def guarded() -> None:
        job.loop = asyncio.get_running_loop()
        job.task = asyncio.current_task()
        await main()

    try:
        asyncio.run(guarded())
    except asyncio.CancelledError:
        job.push({"kind": "stopped", "text": "Investigation stopped."})
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        job.push({"kind": "error", "text": f"{type(ex).__name__}: {str(ex)[:400]}"})
    finally:
        job.finish()


def _run_replay(job: Job, run_dir: Path, speed: float) -> None:
    try:
        trace = json.loads((run_dir / "trace.json").read_text())
        report = FinalReport.model_validate_json((run_dir / "report.json").read_text())
        calls = {c["call_id"]: c for c in trace.get("tool_calls", [])}
        findings = {f.id: f for f in report.findings}
        critiques = {c.get("round"): c for c in trace.get("critiques", [])}
        n_calls, last_t, rnd, reviewed = 0, 0.0, 1, False
        for ev in _synth_agent_events(trace["events"]):
            gap = max(0.0, float(ev.get("t", last_t)) - last_t) / speed
            if job.stop.wait(min(gap, 2.5)):       # long model waits are shortened; the shown clock stays true
                job.push({"kind": "stopped", "text": "Replay stopped."})
                return
            last_t = float(ev.get("t", last_t))
            out = dict(ev)
            kind = ev["kind"]
            if kind == "tool_call":
                n_calls += 1
                rec = calls.get(ev["call_id"])
                if rec:
                    out.update(output=rec["output"][:OUTPUT_PREVIEW], output_len=len(rec["output"]),
                               latency_ms=rec.get("latency_ms"))
            elif kind == "finding" and ev["id"] in findings:
                f = findings[ev["id"]]
                out.update(finding_kind=f.kind, refs=[r.model_dump() for r in f.refs], note=f.verification_note,
                           services=f.services, time=f.time, task=ev.get("task") or f.task_id)
            elif kind == "critique":
                rnd, reviewed = ev.get("round", rnd), True
                c = critiques.get(rnd) or {}
                out.update(challenges=c.get("challenges", []), evidence_requests=c.get("evidence_requests", []))
            elif kind == "hypothesis":
                if reviewed:                 # first revision after a review opens the next round
                    rnd, reviewed = rnd + 1, False
                out["round"] = rnd
            elif kind == "task_created":
                out["round"] = rnd + 1 if reviewed else rnd
            out["stats"] = {"tool_calls": n_calls, "round": rnd}
            job.push(out)
        from ..tools.base import ToolCallRecord
        recs = [ToolCallRecord(**c) for c in trace.get("tool_calls", [])]
        job.push({"kind": "report", "t": report.stats.get("latency_s"), "report": report.model_dump(mode="json"),
                  "markdown": render_markdown(report), "saved_to": str(run_dir.relative_to(ROOT)),
                  "truth": _truth(report.incident_id, report, recs)})
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        job.push({"kind": "error", "text": f"{type(ex).__name__}: {str(ex)[:400]}"})
    finally:
        job.finish()


# ------------------------------------------------------------------ catalogue
# Evaluation rounds offered for replay: the final round and the fault stress run.  The earlier
# rounds (main, v2, v3, weak-model) are superseded and would only bury these in the picker.
REPLAY_SETS = ("v4", "stress")


def _recordings() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for base in sorted((ROOT / "eval" / "results").glob("*/runs")):
        if base.parent.name not in REPLAY_SETS:     # earlier rounds stay on disk for the record, not in the picker
            continue
        for d in sorted(base.glob("*/*")):
            if (d / "report.json").is_file() and (d / "trace.json").is_file():
                system, _, trial = d.name.partition("-")
                out.append({"id": str(d.relative_to(ROOT)), "set": f"evaluation run '{base.parent.name}'",
                            "incident": d.parent.name, "system": system, "label": f"{d.parent.name} · {system} · trial {trial}"})
    for d in sorted((ROOT / "runs").glob("*")):
        if (d / "report.json").is_file() and (d / "trace.json").is_file():
            parts = d.name.split("-")
            inc = next((f"{a}-{b}" for a, b in zip(parts, parts[1:]) if a == "INC"), d.name)
            out.append({"id": str(d.relative_to(ROOT)), "set": "your local runs", "incident": inc,
                        "system": "single" if "single" in d.name else "multi", "label": d.name})
    return out


def _meta() -> dict[str, Any]:
    s = Settings()
    incidents = []
    for i in list_incidents(s.data_dir):
        inc = DataStore(s.data_dir, i).incident
        incidents.append(inc.model_dump())
    b = Budget()
    return {"incidents": incidents, "recordings": _recordings(), "has_key": bool(os.environ.get("LLM_API_KEY")),
            "model": os.environ.get("LLM_MODEL", ""),
            "budget": {"tool_calls": b.max_tool_calls, "llm_calls": b.max_llm_calls, "tokens": b.max_total_tokens,
                       "seconds": b.deadline_s, "rounds": b.max_rounds}}


# ------------------------------------------------------------------ http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, default=str).encode())

    def _local_only(self) -> bool:
        """Listening on 127.0.0.1 does not stop a web page in the user's browser from calling us.
        Refuse requests addressed to another hostname (DNS rebinding) or sent by another origin
        (a cross-site POST would start a paid model run)."""
        host = self.headers.get("Host") or ""
        origin = self.headers.get("Origin")
        name = urllib.parse.urlsplit("//" + host).hostname or ""
        if (host and name not in ("127.0.0.1", "localhost")) or (origin is not None and urllib.parse.urlsplit(origin).netloc != host):
            self._json(403, {"error": "cross-origin request refused"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._local_only():
            return
        path = urllib.parse.urlparse(self.path).path
        if path in ("", "/"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/meta":
            return self._json(200, _meta())
        if path.startswith("/api/stream/"):
            return self._stream(path.rsplit("/", 1)[1])
        self._send(404, b"not found", "text/plain")

    def _stream(self, job_id: str) -> None:
        job = JOBS.get(job_id)
        if not job:
            return self._json(404, {"error": "unknown job"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            idx = max(0, int(self.headers.get("Last-Event-ID") or -1) + 1)   # a reconnecting browser resumes where it was
        except ValueError:
            idx = 0
        try:
            while True:
                with job.cond:
                    if idx >= len(job.events) and not job.done:
                        job.cond.wait(timeout=10)
                    batch, finished = job.events[idx:], job.done
                if not batch and not finished:
                    self.wfile.write(b": keep-alive\n\n")
                for ev in batch:
                    self.wfile.write(f"id: {idx}\ndata: {json.dumps(ev, default=str)}\n\n".encode())
                    idx += 1
                self.wfile.flush()
                if finished and idx >= len(job.events):
                    self.wfile.write(b"event: end\ndata: {}\n\n")
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:  # noqa: N802
        if not self._local_only():
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:          # bad Content-Length, undecodable bytes or invalid JSON
            return self._json(400, {"error": "invalid JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "request body must be a JSON object"})
        job_id = f"job{uuid.uuid4().hex[:12]}"       # a timestamp id collides for two requests in one millisecond
        if path == "/api/run":
            inc, system, llm = body.get("incident_id"), body.get("system", "multi"), body.get("llm", "fake")
            if inc not in list_incidents(Settings().data_dir) or system not in ("multi", "single") or llm not in ("real", "fake"):
                return self._json(400, {"error": "unknown incident, system or model option"})
            if llm == "real" and not os.environ.get("LLM_API_KEY"):
                return self._json(400, {"error": "LLM_API_KEY is not set. Copy .env.example to .env, or use the offline script."})
            try:
                chaos = min(1.0, max(0.0, float(body.get("chaos") or 0)))
                crash = str(body.get("crash_agent") or "")
                faults = {crash: 1} if crash in ("observability", "dependency", "reasoning", "critic", "reporting", "orchestrator") else {}
            except (TypeError, ValueError):
                return self._json(400, {"error": "invalid options"})
            job = JOBS[job_id] = Job(job_id, {"mode": "live", "incident": inc, "system": system, "llm": llm})
            threading.Thread(target=_run_live, args=(job, inc, system, llm, chaos, faults), daemon=True).start()
            return self._json(200, {"job_id": job_id})
        if path == "/api/replay":
            rec = next((r for r in _recordings() if r["id"] == body.get("recording")), None)   # only known run dirs
            if not rec:
                return self._json(400, {"error": "unknown recording"})
            try:
                speed = min(200.0, max(1.0, float(body.get("speed") or 8)))
            except (TypeError, ValueError):
                speed = 8.0
            job = JOBS[job_id] = Job(job_id, {"mode": "replay", **rec})
            threading.Thread(target=_run_replay, args=(job, ROOT / rec["id"], speed), daemon=True).start()
            return self._json(200, {"job_id": job_id})
        if path.startswith("/api/stop/"):
            job = JOBS.get(path.rsplit("/", 1)[1])
            if not job:
                return self._json(404, {"error": "unknown job"})
            job.stop.set()
            if job.loop and job.task and not job.done:
                try:
                    job.loop.call_soon_threadsafe(job.task.cancel)
                except RuntimeError:        # the run finished and closed its loop a moment ago
                    pass
            return self._json(200, {"ok": True})
        self._send(404, b"not found", "text/plain")


def serve(port: int = 8000) -> None:
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as ex:
        raise SystemExit(f"Cannot listen on port {port} ({ex.strerror}). Try: rca serve --port {port + 1}") from ex
    srv.daemon_threads = True
    key = "found" if os.environ.get("LLM_API_KEY") else "not set (offline script and replays still work)"
    print(f"Incident Investigator UI on http://localhost:{port}/   (Ctrl+C to stop)\nLLM_API_KEY: {key}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
