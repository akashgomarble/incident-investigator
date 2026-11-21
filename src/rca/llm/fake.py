"""Deterministic scripted LLM used by the automated tests and by `--llm fake`.

It is a TEST DOUBLE, not an investigator: it follows a fixed generic script (query the reported
service, list recent changes, blame the most recent deploy) so that the orchestration, tool,
grounding and recovery machinery can be exercised offline, for free and reproducibly.  Its
diagnoses are not meaningful and it is never used for the reported evaluation numbers.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from ..tools.datastore import fmt_ts, parse_ts
from .base import LLMError, LLMResponse, ToolCall


class FakeLLM:
    def __init__(self, critic_verdicts: list[str] | None = None, malformed_once: set[str] | None = None,
                 fail_times: dict[str, int] | None = None, prose_once: set[str] | None = None):
        self.critic_verdicts = list(critic_verdicts or ["needs_evidence", "accept"])
        self.malformed_once = set(malformed_once or ())     # roles whose first submit is schema-invalid
        self.prose_once = set(prose_once or ())             # roles that first answer in prose instead of a tool call
        self.fail_times = dict(fail_times or {})            # role -> number of LLMError raises
        self.calls: list[str] = []
        self._n = 0

    # ------------------------------------------------------------------ plumbing
    def _resp(self, messages: list[dict[str, Any]], calls: list[tuple[str, dict[str, Any]]], text: str = "") -> LLMResponse:
        tcs = []
        for name, args in calls:
            self._n += 1
            tcs.append(ToolCall(id=f"fake_{self._n}", name=name, arguments=args))
        raw: dict[str, Any] = {"role": "assistant", "content": text}
        if tcs:
            raw["tool_calls"] = [{"id": t.id, "type": "function", "function": {"name": t.name, "arguments": json.dumps(t.arguments)}} for t in tcs]
        prompt = sum(len(str(m.get("content", ""))) for m in messages) // 4
        return LLMResponse(content=text, tool_calls=tcs, prompt_tokens=prompt, completion_tokens=len(json.dumps(raw)) // 4,
                           model="fake", raw_message=raw)

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
                       role: str = "", meta: dict[str, Any] | None = None) -> LLMResponse:
        self.calls.append(role)
        if self.fail_times.get(role, 0) > 0:
            self.fail_times[role] -= 1
            raise LLMError(f"simulated provider outage for {role}")
        if role in self.prose_once:
            self.prose_once.discard(role)
            return self._resp(messages, [], text="Let me think about this incident first.")
        if role in self.malformed_once:
            self.malformed_once.discard(role)
            return self._resp(messages, [("submit", {"oops": "not the schema"})])
        meta = meta or {}
        offered = {t["function"]["name"] for t in (tools or [])}
        tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool" and str(m.get("content", "")).startswith("{")]
        handler = getattr(self, f"_{role}")
        return self._resp(messages, handler(meta, offered, tool_msgs))

    @staticmethod
    def _window(st: Any, minutes: int = 120) -> dict[str, str]:
        now = parse_ts(st.incident.now)
        return {"start": fmt_ts(now - timedelta(minutes=minutes)), "end": fmt_ts(now)}

    # ------------------------------------------------------------------ roles
    def _orchestrator(self, meta: dict, offered: set[str], _: list) -> list[tuple[str, dict]]:
        st = meta["state"]
        svc = st.incident.affected_service
        if meta.get("phase") == "replan":
            reqs = meta["critique"].evidence_requests
            return [("submit", {"decision": "investigate", "reason": "critic asked for more evidence",
                                "tasks": [{"key": f"r{i}", "agent": r.agent, "objective": r.question} for i, r in enumerate(reqs)]})]
        return [("submit", {"initial_theories": ["a recent change broke it", "a dependency is failing"], "tasks": [
            {"key": "a", "agent": "observability", "objective": f"Find onset and error signatures on {svc}"},
            {"key": "b", "agent": "dependency", "objective": f"List recent changes and dependencies of {svc}"},
            {"key": "c", "agent": "observability", "objective": f"Check health of the dependencies of {svc}", "depends_on": ["b"]}]})]

    @staticmethod
    def _findings_from(tool_msgs: list[dict]) -> tuple[list[dict], list[dict]]:
        findings, gaps = [], []
        for m in tool_msgs:
            cid, tool = m.get("call_id", "?"), m.get("tool", "?")
            if not m.get("ok"):
                gaps.append({"description": f"{tool} failed: {m.get('error', {}).get('type')}", "tool": tool, "impact": "data not available"})
                continue
            d = m.get("data") or {}
            if tool == "query_logs" and d.get("entries"):
                e = d["entries"][0]
                findings.append({"statement": f"{d['service']} logged {d.get('total_matched')} WARN+ lines; first: {e['message'][:80]}",
                                 "kind": "observation", "services": [d["service"]], "time": e["ts"],
                                 "refs": [{"call_id": cid, "quote": e["message"][:60]}]})
            elif tool == "query_metrics" and d.get("summary"):
                findings.append({"statement": f"{d['service']} {d['metric']} ranged {d['summary'].get('min')}..{d['summary'].get('max')}",
                                 "kind": "observation", "services": [d["service"]],
                                 "refs": [{"call_id": cid, "quote": f'"metric":"{d["metric"]}"'}]})
            elif tool == "get_deployments":
                for dep in d.get("deployments", []):
                    findings.append({"statement": f"deployment {dep['id']} of {dep['service']} at {dep['deployed_at']}: {dep['summary']}",
                                     "kind": "observation", "services": [dep["service"]], "time": dep["deployed_at"],
                                     "refs": [{"call_id": cid, "quote": dep["id"]}]})
            elif tool == "get_config_changes":
                for ch in d.get("changes", []):
                    findings.append({"statement": f"config change {ch['id']} on {ch['service']}: {ch['key']}",
                                     "kind": "observation", "services": [ch["service"]], "time": ch["changed_at"],
                                     "refs": [{"call_id": cid, "quote": ch["id"]}]})
            elif tool == "get_service_dependencies":
                findings.append({"statement": f"{d['service']['name']} has {len(d.get('edges', []))} dependency edges",
                                 "kind": "observation", "services": [d["service"]["name"]],
                                 "refs": [{"call_id": cid, "quote": d["service"]["name"]}]})
            if m.get("partial"):
                gaps.append({"description": f"{tool} returned a partial result", "tool": tool, "impact": "items may be missing"})
        return findings, gaps

    def _observability(self, meta: dict, offered: set[str], tool_msgs: list[dict]) -> list[tuple[str, dict]]:
        st = meta["state"]
        if "query_logs" in offered and not tool_msgs:
            svc, w = st.incident.affected_service, self._window(st)
            return [("query_logs", {"service": svc, **w}), ("query_metrics", {"service": svc, "metric": "error_rate", **w}),
                    ("query_metrics", {"service": svc, "metric": "latency_p99_ms", **w})]
        findings, gaps = self._findings_from(tool_msgs)
        return [("submit", {"findings": findings, "gaps": gaps, "answer": f"collected {len(findings)} observations"})]

    def _dependency(self, meta: dict, offered: set[str], tool_msgs: list[dict]) -> list[tuple[str, dict]]:
        st = meta["state"]
        if "get_deployments" in offered and not tool_msgs:
            w = self._window(st, 360)
            return [("get_deployments", w), ("get_config_changes", w),
                    ("get_service_dependencies", {"service": st.incident.affected_service})]
        findings, gaps = self._findings_from(tool_msgs)
        return [("submit", {"findings": findings, "gaps": gaps, "answer": f"found {len(findings)} changes/edges"})]

    def _reasoning(self, meta: dict, offered: set[str], _: list) -> list[tuple[str, dict]]:
        st = meta["state"]
        deploys = [f for f in st.findings.values() if f.statement.startswith("deployment ")]
        obs = [f.id for f in st.findings.values() if f.agent == "observability"][:3]
        existing = list(st.hypotheses)
        hyps = []
        if deploys:
            d = deploys[-1]
            hyps.append({"id": existing[0] if existing else None, "title": f"Recent deployment on {d.services[0]}",
                         "statement": d.statement, "category": "bad_deployment", "root_cause_service": d.services[0],
                         "trigger_change_id": d.refs[0].quote, "supporting": [d.id] + obs, "contradicting": [],
                         "causal_chain": [{"claim": "deployment went out", "finding_ids": [d.id]},
                                          {"claim": "errors followed", "finding_ids": obs[:1]}],
                         "assumptions": ["the deployment preceded onset"], "confidence": 0.7})
        hyps.append({"id": existing[1] if len(existing) > 1 else None, "title": "Unidentified dependency failure",
                     "statement": "a dependency of the reported service is degraded", "category": "dependency_failure",
                     "root_cause_service": None, "supporting": obs[:1], "contradicting": [], "causal_chain": [],
                     "assumptions": ["no direct evidence yet"], "confidence": 0.2})
        return [("submit", {"hypotheses": hyps, "discriminating_questions": ["did errors start before the deployment?"]})]

    def _critic(self, meta: dict, offered: set[str], _: list) -> list[tuple[str, dict]]:
        st = meta["state"]
        verdict = self.critic_verdicts[min(len(st.critiques), len(self.critic_verdicts) - 1)]
        top = st.ranked()[0].id if st.hypotheses else "H1"
        out: dict[str, Any] = {"verdict": verdict, "summary": f"scripted review: {verdict}", "confidence_cap": 0.8 if verdict == "accept" else 0.45,
                               "challenges": [{"hypothesis_id": top, "issue": "temporal order not established", "severity": "major"}]}
        if verdict == "needs_evidence":
            n = len(st.critiques) + 1
            out["evidence_requests"] = [{"agent": "observability", "hypothesis_ids": [top], "rationale": "establish order",
                                         "question": f"Follow-up {n}: confirm when errors on {st.incident.affected_service} began relative to the last change"}]
        return [("submit", out)]

    def _reporting(self, meta: dict, offered: set[str], _: list) -> list[tuple[str, dict]]:
        st = meta["state"]
        ok = [f for f in st.findings.values() if f.verified][:3]
        return [("submit", {"outcome": "root_cause_identified", "summary": "Scripted report.",
                            "facts": [{"statement": f.statement, "finding_ids": [f.id]} for f in ok]
                            + [{"statement": "a fact with a bogus citation", "finding_ids": ["F999"]}],
                            "assumptions": ["scripted assumption"],
                            "timeline": [{"time": f.time or st.incident.reported_at, "event": f.statement, "finding_ids": [f.id]} for f in ok[:2]],
                            "recommended_actions": [{"action": "roll back the suspected change", "priority": "immediate",
                                                     "rationale": "fastest mitigation", "finding_ids": [f.id for f in ok[:1]]}],
                            "data_gaps": [g.description for g in st.gaps]})]

    def _baseline(self, meta: dict, offered: set[str], tool_msgs: list[dict]) -> list[tuple[str, dict]]:
        st = meta["state"]
        if "query_logs" in offered and not tool_msgs:
            svc, w = st.incident.affected_service, self._window(st)
            return [("query_logs", {"service": svc, **w}), ("query_metrics", {"service": svc, "metric": "error_rate", **w}),
                    ("get_deployments", self._window(st, 360))]
        findings, gaps = self._findings_from(tool_msgs)
        for i, f in enumerate(findings, 1):
            f["id"] = f"F{i}"
        deploys = [f for f in findings if f["statement"].startswith("deployment ")]
        hyps = [{"title": "Recent deployment", "statement": deploys[-1]["statement"], "category": "bad_deployment",
                 "root_cause_service": deploys[-1]["services"][0], "trigger_change_id": deploys[-1]["refs"][0]["quote"],
                 "supporting": [deploys[-1]["id"]], "contradicting": [], "confidence": 0.8}] if deploys else []
        return [("submit", {"findings": findings, "hypotheses": hyps, "outcome": "root_cause_identified" if hyps else "inconclusive",
                            "summary": "Scripted baseline report.", "facts": [{"statement": f["statement"], "finding_ids": [f["id"]]} for f in findings[:2]],
                            "data_gaps": [g["description"] for g in gaps]})]
