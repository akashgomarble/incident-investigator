"""Synthetic incident dataset generator (deterministic; `python dataset/generate.py`).

Models one fictional brokerage platform and 12 incidents on it.  Each incident gets:
    incident.json  logs.jsonl  metrics.json  deployments.json  config_changes.json  faults.json
    ground_truth.json   <- read ONLY by eval/scoring.py, never by tools or agents
Every scenario has realistic background noise (benign deploys, WARN chatter) and most have a
deliberate decoy: a change or past incident that correlates with the symptom but is not the cause.
"""
from __future__ import annotations

import json
import math
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parents[1] / "data"

# ----------------------------------------------------------------------------- platform
SVC = "service"
NODES = [
    ("api-gateway", SVC, "edge"), ("auth-service", SVC, "identity"), ("order-service", SVC, "trading"),
    ("portfolio-service", SVC, "wealth"), ("pricing-engine", SVC, "trading"), ("market-data-service", SVC, "market-data"),
    ("payment-service", SVC, "payments"), ("settlement-service", SVC, "payments"),
    ("notification-service", SVC, "growth"), ("exchange-gateway", SVC, "trading"),
    ("ledger-db", "postgres", "platform"), ("user-db", "postgres", "platform"),
    ("redis-cache", "redis", "platform"), ("kafka", "kafka", "platform"),
    ("exchange-api", "external", "vendor"), ("bank-partner-api", "external", "vendor"),
    ("kyc-provider", "external", "vendor"), ("email-provider", "external", "vendor"),
]
EDGES = [
    ("api-gateway", "auth-service", "http"), ("api-gateway", "order-service", "http"),
    ("api-gateway", "portfolio-service", "http"), ("api-gateway", "market-data-service", "http"),
    ("api-gateway", "payment-service", "http"),
    ("auth-service", "user-db", "db"), ("auth-service", "redis-cache", "cache"), ("auth-service", "kyc-provider", "external"),
    ("order-service", "ledger-db", "db"), ("order-service", "pricing-engine", "http"),
    ("order-service", "exchange-gateway", "http"), ("order-service", "kafka", "queue-produce"),
    ("portfolio-service", "ledger-db", "db"), ("portfolio-service", "market-data-service", "http"),
    ("portfolio-service", "redis-cache", "cache"),
    ("pricing-engine", "market-data-service", "http"), ("pricing-engine", "redis-cache", "cache"),
    ("market-data-service", "redis-cache", "cache"), ("market-data-service", "exchange-gateway", "http"),
    ("exchange-gateway", "exchange-api", "external"),
    ("payment-service", "ledger-db", "db"), ("payment-service", "bank-partner-api", "external"),
    ("payment-service", "kafka", "queue-produce"),
    ("settlement-service", "kafka", "queue-consume"), ("settlement-service", "ledger-db", "db"),
    ("notification-service", "kafka", "queue-consume"), ("notification-service", "email-provider", "external"),
]
SHARED = {"ledger-db", "redis-cache", "kafka"}

BASE: dict[str, dict[str, float]] = {
    "api-gateway": dict(request_rate=1200, latency_p99_ms=210, cpu_pct=42, memory_pct=58),
    "auth-service": dict(request_rate=380, latency_p99_ms=90, cpu_pct=38, memory_pct=52, rate_limited_per_min=2),
    "order-service": dict(request_rate=220, latency_p99_ms=150, cpu_pct=40, memory_pct=60, db_pool_in_use=14),
    "portfolio-service": dict(request_rate=540, latency_p99_ms=180, cpu_pct=36, memory_pct=57, db_pool_in_use=12),
    "pricing-engine": dict(request_rate=610, latency_p99_ms=60, cpu_pct=40, memory_pct=49),
    "market-data-service": dict(request_rate=900, latency_p99_ms=40, cpu_pct=45, memory_pct=62),
    "payment-service": dict(request_rate=95, latency_p99_ms=320, cpu_pct=30, memory_pct=55, db_pool_in_use=8,
                            bank_api_error_rate=0.2, payment_success_rate=99.2),
    "settlement-service": dict(request_rate=60, latency_p99_ms=240, cpu_pct=28, memory_pct=50, db_pool_in_use=6,
                               consumer_lag=45),
    "notification-service": dict(request_rate=140, latency_p99_ms=110, cpu_pct=25, memory_pct=48, consumer_lag=60),
    "exchange-gateway": dict(request_rate=260, latency_p99_ms=130, cpu_pct=30, memory_pct=44,
                             upstream_latency_ms=120, upstream_error_rate=0.3),
    "ledger-db": dict(cpu_pct=30, connections_active=62, slow_queries_per_min=1, disk_used_pct=71, replication_lag_ms=40),
    "user-db": dict(cpu_pct=18, connections_active=25, slow_queries_per_min=0.3, disk_used_pct=44, replication_lag_ms=30),
    "redis-cache": dict(memory_pct=70, evictions_per_min=3, hit_ratio_pct=96, cpu_pct=22, connected_clients=310),
    "kafka": dict(messages_in_per_s=2400, under_replicated_partitions=0, cpu_pct=35),
}
for _n, _t, _ in NODES:
    if _t == SVC:
        BASE[_n].update(error_rate=0.2, restarts=0)
UNITS = {"request_rate": "req/s", "error_rate": "% of requests (HTTP 5xx)", "latency_p99_ms": "ms", "cpu_pct": "%",
         "memory_pct": "%", "restarts": "count (cumulative)", "db_pool_in_use": "connections",
         "consumer_lag": "messages", "upstream_latency_ms": "ms", "upstream_error_rate": "%",
         "bank_api_error_rate": "%", "payment_success_rate": "%", "rate_limited_per_min": "req/min",
         "connections_active": "connections", "slow_queries_per_min": "queries/min", "disk_used_pct": "%",
         "replication_lag_ms": "ms", "evictions_per_min": "keys/min", "hit_ratio_pct": "%",
         "connected_clients": "clients", "messages_in_per_s": "msg/s", "under_replicated_partitions": "count"}

BG_INFO = {
    SVC: ["health check ok", "config reloaded (no changes)", "scheduled job heartbeat ok",
          "connection pool stats: healthy", "served batch of requests, p50={ms2}ms"],
    "postgres": ["checkpoint complete: wrote {k} buffers", "autovacuum: processed table public.audit_log"],
    "redis": ["background saving terminated with success", "{k} changes in 300 seconds. Saving..."],
    "kafka": ["log segment rolled for partition orders-{n}", "group coordinator heartbeat ok"],
}
BG_WARN = ["GC pause {ms2}ms exceeded 200ms target", "retrying idempotent request (attempt 2) after connection reset",
           "slow request {ms}ms on /internal/metrics", "clock drift 38ms detected, resyncing with NTP",
           "deprecated TLS cipher offered by client {id}"]


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Scenario:
    def __init__(self, id: str, start: str, seed: int, minutes: int = 360, step_s: int = 60, bg_every: int = 6):
        self.id, self.rng = id, random.Random(seed)
        self.t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        self.minutes, self.step_s, self.bg_every = minutes, step_s, bg_every
        self.n = minutes * 60 // step_s
        self.series: dict[str, dict[str, list[float | None]]] = {}
        self.logs: list[tuple[datetime, str, str, str]] = []
        self.deploys: list[dict[str, Any]] = []
        self.configs: list[dict[str, Any]] = []
        self.faults: list[dict[str, Any]] = []
        self.incident: dict[str, Any] = {}
        self.truth: dict[str, Any] = {}
        self._baseline()

    # ---- time
    def at(self, minute: float) -> datetime:
        return self.t0 + timedelta(minutes=minute)

    def ts(self, minute: float) -> str:
        return iso(self.at(minute))

    def _idx(self, minute: float) -> int:
        return max(0, min(self.n, int(minute * 60 // self.step_s)))

    # ---- metrics
    def _noisy(self, metric: str, v: float, noise: float) -> float:
        if v == 0:
            return 0.0
        if metric == "error_rate":
            return round(max(0.0, v * self.rng.uniform(0.5, 1.6)), 2) if v < 1 else round(v * self.rng.gauss(1, noise), 2)
        out = v * self.rng.gauss(1, noise)
        if metric.endswith("_pct") or metric in ("payment_success_rate",):
            out = min(100.0, out)
        return round(max(0.0, out), 2)

    def _baseline(self) -> None:
        for svc, metrics in BASE.items():
            self.series[svc] = {}
            for metric, base in metrics.items():
                vals: list[float | None] = []
                for i in range(self.n):
                    v = base
                    if metric in ("request_rate", "messages_in_per_s"):
                        v = base * (1 + 0.06 * math.sin(2 * math.pi * i * self.step_s / 86400))
                    noise = 0.0 if metric in ("restarts", "under_replicated_partitions") else \
                        0.008 if metric in ("disk_used_pct", "hit_ratio_pct", "payment_success_rate") else \
                        0.25 if metric in ("consumer_lag", "slow_queries_per_min", "evictions_per_min", "rate_limited_per_min") else 0.04
                    vals.append(self._noisy(metric, v, noise))
                self.series[svc][metric] = vals

    def set(self, svc: str, metric: str, a: float, b: float | None, v: float, v_end: float | None = None,
            noise: float = 0.04) -> None:
        i0, i1 = self._idx(a), self._idx(self.minutes if b is None else b)
        span = max(1, i1 - i0 - 1)
        for i in range(i0, i1):
            target = v if v_end is None else v + (v_end - v) * (i - i0) / span
            self.series[svc][metric][i] = self._noisy(metric, target, noise) if noise else round(target, 2)

    def gap(self, svc: str, metric: str, a: float, b: float | None) -> None:
        for i in range(self._idx(a), self._idx(self.minutes if b is None else b)):
            self.series[svc][metric][i] = None

    # ---- logs
    def _fmt(self, tpl: str) -> str:
        r = self.rng
        return tpl.format(n=r.randint(1, 12), ms=r.randint(800, 9000), ms2=r.randint(210, 480),
                          id="%08x" % r.getrandbits(32), k=r.randint(1000, 99999), w=r.randint(20, 60))

    def log(self, minute: float, svc: str, level: str, msg: str) -> None:
        self.logs.append((self.at(minute) + timedelta(seconds=self.rng.randint(0, 50)), svc, level, self._fmt(msg)))

    def burst(self, svc: str, a: float, b: float | None, level: str, templates: list[str], per_min: float) -> None:
        end = self.minutes if b is None else b
        m = a
        while m < end:
            count = int(per_min) + (1 if self.rng.random() < per_min - int(per_min) else 0)
            for _ in range(count):
                self.log(m + self.rng.random(), svc, level, self.rng.choice(templates))
            m += 1

    def _background_logs(self) -> None:
        for name, typ, _ in NODES:
            if typ == "external":
                continue
            m = self.rng.uniform(0, self.bg_every)
            while m < self.minutes:
                if self.rng.random() < 0.05:
                    self.log(m, name, "WARN", self.rng.choice(BG_WARN))
                else:
                    self.log(m, name, "INFO", self.rng.choice(BG_INFO[typ]))
                m += self.rng.uniform(0.5, 1.5) * self.bg_every

    # ---- changes
    def deploy(self, id: str, svc: str, minute: float, version: str, summary: str, changes: list[str],
               author: str = "ci-bot", rolled_back_at: float | None = None) -> None:
        self.deploys.append({"id": id, "service": svc, "version": version, "deployed_at": self.ts(minute),
                             "author": author, "summary": summary, "changes": changes,
                             "status": "rolled_back" if rolled_back_at is not None else "live",
                             "rolled_back_at": self.ts(rolled_back_at) if rolled_back_at is not None else None})
        self.log(minute, svc, "INFO", f"deployment {id} ({version}) rollout complete, all pods ready")
        if rolled_back_at is not None:
            self.log(rolled_back_at, svc, "INFO", f"rollback of {id} complete, previous version restored")

    def config(self, id: str, svc: str, minute: float, key: str, old: Any, new: Any, description: str,
               author: str = "config-bot") -> None:
        self.configs.append({"id": id, "service": svc, "key": key, "old_value": old, "new_value": new,
                             "changed_at": self.ts(minute), "author": author, "description": description})
        self.log(minute, svc, "INFO", f"config change {id} applied: {key}={new}")

    # ---- output
    def report(self, title: str, reported_min: float, affected: str, description: str, symptoms: list[str],
               severity: str = "SEV-2", reporter: str = "on-call SRE") -> None:
        self.incident = {"id": self.id, "title": title, "severity": severity, "reported_at": self.ts(reported_min),
                         "now": self.ts(self.minutes - 1), "reporter": reporter, "affected_service": affected,
                         "description": description, "symptoms": symptoms}

    def write(self) -> None:
        self._background_logs()
        d = OUT / "incidents" / self.id
        d.mkdir(parents=True, exist_ok=True)
        (d / "incident.json").write_text(json.dumps(self.incident, indent=2))
        self.logs.sort(key=lambda t: t[0])
        end = self.at(self.minutes - 1)
        (d / "logs.jsonl").write_text("\n".join(
            json.dumps({"ts": iso(t), "service": s, "level": l, "message": m}) for t, s, l, m in self.logs if t <= end))
        (d / "metrics.json").write_text(json.dumps(
            {"start": iso(self.t0), "step_s": self.step_s, "points": self.n, "units": UNITS, "series": self.series},
            separators=(",", ":")))
        (d / "deployments.json").write_text(json.dumps(sorted(self.deploys, key=lambda x: x["deployed_at"]), indent=2))
        (d / "config_changes.json").write_text(json.dumps(sorted(self.configs, key=lambda x: x["changed_at"]), indent=2))
        (d / "faults.json").write_text(json.dumps(self.faults, indent=2))
        (d / "ground_truth.json").write_text(json.dumps(self.truth, indent=2))


def truth(services: list[str], category: str, *, trigger: str | None = None, also: list[str] | None = None,
          outcome: str = "root_cause_identified", decoys: list[str] | None = None, key_evidence: list[str] | None = None,
          tags: list[str] | None = None, narrative: str = "") -> dict[str, Any]:
    return {"expected_outcome": outcome, "root_cause_services": services, "category": category,
            "acceptable_categories": [category] + (also or []), "trigger_change_id": trigger,
            "decoys": decoys or [], "key_evidence": key_evidence or [], "tags": tags or [], "narrative": narrative}


# ============================================================================= scenarios
def inc_001() -> Scenario:
    s = Scenario("INC-001", "2026-03-03T08:00:00", seed=101)
    s.deploy("dep-5088", "portfolio-service", 96, "v4.2.1", "Dependency bumps", ["bump grpc 1.62 -> 1.63"])
    s.config("cfg-2101", "notification-service", 201, "email.batch_size", 200, 250, "Tune email batching")
    s.deploy("dep-5101", "order-service", 250, "v2.14.0", "Refactor order validation; add margin check for F&O orders",
             ["new MarginCheckValidator in validation chain", "remove legacy OrderRules.validateMargin()"], author="r.iyer")
    s.set("order-service", "error_rate", 252, None, 18.5)
    s.set("api-gateway", "error_rate", 252, None, 3.4)
    s.burst("order-service", 252, None, "ERROR", [
        "NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId={id}",
        "order rejected with 500: java.lang.NullPointerException: marginProfile is null for account {k}"], 4)
    s.burst("api-gateway", 252, None, "WARN", ["upstream order-service returned 500 for POST /v1/orders (trace {id})"], 1.5)
    s.report("Order placement returning 500s", 262, "order-service",
             "Spike in failed order placements reported by support. Mostly derivatives orders according to tickets.",
             ["HTTP 500 on POST /v1/orders", "order-service error rate alert firing"], severity="SEV-1")
    s.truth = truth(["order-service"], "bad_deployment", trigger="dep-5101",
                    key_evidence=["dep-5101", "MarginCheckValidator"], tags=["clear-signal"],
                    narrative="Release v2.14.0 added a validator that NPEs for F&O orders; errors begin 2 min after rollout.")
    return s


def inc_002() -> Scenario:
    s = Scenario("INC-002", "2026-03-10T06:00:00", seed=102)
    s.deploy("dep-5220", "api-gateway", 120, "v9.3.0", "Upgrade envoy filter chain; enable HTTP/2 to upstreams",
             ["envoy 1.29 -> 1.30", "upstream protocol h2 for portfolio-service and order-service"], author="platform-team")
    s.config("cfg-2199", "order-service", 60, "log.sampling_rate", 0.1, 0.2, "More request log sampling")
    s.config("cfg-2207", "portfolio-service", 232, "db.pool.max_size", 50, 5,
             "Apply cost-optimisation profile 'small' to service datasource", author="infra-automation")
    s.set("portfolio-service", "db_pool_in_use", 233, None, 5, noise=0)
    s.set("portfolio-service", "latency_p99_ms", 233, None, 4200)
    s.set("portfolio-service", "error_rate", 234, None, 22)
    s.set("api-gateway", "latency_p99_ms", 233, None, 4350)
    s.set("api-gateway", "error_rate", 234, None, 7.2)
    s.set("ledger-db", "connections_active", 233, None, 55)
    s.burst("portfolio-service", 233, None, "ERROR", [
        "HikariPool-1 - Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting={w})",
        "GET /v1/portfolio/holdings failed: could not acquire JDBC connection within 3000ms"], 4)
    s.burst("api-gateway", 233, None, "WARN", ["upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504 (trace {id})"], 2)
    s.report("Portfolio page timing out for users", 245, "api-gateway",
             "Users report the portfolio page spinning and timing out. Gateway p99 latency is through the roof. "
             "We shipped a gateway upgrade earlier today (HTTP/2 to upstreams) - strongly suspect that.",
             ["api-gateway p99 > 4s", "504s on /v1/portfolio/*"])
    s.truth = truth(["portfolio-service"], "config_change", trigger="cfg-2207", decoys=["dep-5220"],
                    key_evidence=["cfg-2207", "total=5, active=5"], tags=["misleading-report", "decoy-deploy", "symptom-not-at-cause"],
                    narrative="An automated profile shrank the portfolio-service DB pool 50->5; the gateway deploy 112 min earlier is unrelated.")
    return s


def inc_003() -> Scenario:
    s = Scenario("INC-003", "2026-03-17T07:00:00", seed=103)
    s.deploy("dep-5333", "order-service", 228, "v2.15.2", "Add idempotency keys to order submit",
             ["Idempotency-Key header persisted in orders table", "retry-safe submit handler"], author="a.khan", rolled_back_at=256)
    s.config("cfg-2290", "pricing-engine", 140, "cache.ttl_seconds", 5, 4, "Shorter quote cache TTL")
    s.set("exchange-gateway", "upstream_latency_ms", 241, None, 9200)
    s.set("exchange-gateway", "upstream_error_rate", 241, None, 46)
    s.set("exchange-gateway", "error_rate", 241, None, 44)
    s.set("exchange-gateway", "latency_p99_ms", 241, None, 5100)
    s.set("order-service", "error_rate", 242, None, 31)
    s.set("order-service", "latency_p99_ms", 242, None, 5300)
    s.set("market-data-service", "error_rate", 242, None, 6.5)
    s.set("api-gateway", "error_rate", 242, None, 6.1)
    s.burst("exchange-gateway", 241, None, "ERROR", [
        "upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder",
        "upstream connect timeout after 5000ms host=api.exchange-partner.example op=quoteSnapshot",
        "exchange-api returned 503 Service Unavailable (x-partner-request-id={id})"], 4)
    s.log(243, "exchange-gateway", "WARN", "circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)")
    s.burst("order-service", 242, None, "ERROR", ["exchange-gateway call failed: 504 Gateway Timeout (placeOrder) orderId={id}"], 3)
    s.burst("market-data-service", 242, None, "WARN", ["stale quotes: exchange feed lag {n}s via exchange-gateway"], 1.2)
    s.report("Orders failing since this morning's order-service release", 250, "order-service",
             "Order submit failure rate ~30%. order-service v2.15.2 went out shortly before; on-call is rolling it back.",
             ["order-service error rate 30%", "504s on order submit"], severity="SEV-1")
    s.truth = truth(["exchange-gateway", "exchange-api", "exchange-partner"], "dependency_failure", decoys=["dep-5333"],
                    key_evidence=["api.exchange-partner.example", "rolled_back"], tags=["misleading-report", "decoy-deploy", "external-dependency"],
                    narrative="The external exchange API degraded at 11:01. order-service was healthy for 13 min after its deploy, "
                              "an undeployed service (market-data) is hit too, and the rollback changed nothing.")
    return s


def inc_004() -> Scenario:
    s = Scenario("INC-004", "2026-03-20T00:00:00", seed=104, minutes=96 * 60, step_s=300, bg_every=45)
    H = 60
    s.deploy("dep-5402", "notification-service", 24 * H, "v3.8.0", "Add in-memory template cache for email rendering",
             ["TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment", "render path uses cache"], author="s.menon")
    s.deploy("dep-5415", "auth-service", 61 * H, "v7.1.4", "Patch release", ["bump jackson"])
    s.config("cfg-2410", "kafka", 90 * H, "log.retention.ms", 604800000, 259200000, "Kafka maintenance: reduce retention to 3 days", author="platform-team")
    cycles = [(24 * H, 47 * H), (47 * H, 70 * H), (70 * H, 92 * H), (92 * H, 96 * H)]
    restarts = 0
    for i, (a, b) in enumerate(cycles):
        full = b - a >= 20 * H
        s.set("notification-service", "memory_pct", a, b, 46, 97 if full else 46 + 51 * (b - a) / (22 * H), noise=0.01)
        s.set("notification-service", "restarts", a, b, restarts, noise=0)
        for frac in (0.25, 0.5, 0.75, 0.95):
            if full or frac <= 0.5:
                m = a + (b - a) * frac
                s.log(m, "notification-service", "INFO", f"TemplateCache stats: size={int(9000 * (m - a) / H)} entries, evictions=0")
        if full:
            s.burst("notification-service", b - 90, b - 2, "WARN", ["heap usage {n}% over 90 (old gen), full GC took {ms}ms"], 0.08)
            s.log(b - 1, "notification-service", "ERROR", "container notification-service-7d9f OOMKilled (exit code 137), restarting")
            s.set("notification-service", "consumer_lag", b - 2, b + 25, 18000, 300)
            s.set("notification-service", "error_rate", b - 2, b + 6, 14)
            restarts += 1
    s.set("notification-service", "restarts", 92 * H, None, restarts, noise=0)
    s.report("notification-service keeps getting OOMKilled", 92 * H + 20, "notification-service",
             "Third OOM restart of notification-service this week; trade-confirmation emails were delayed ~25 min. "
             "The latest one happened right after today's Kafka maintenance, so we think the consumer is choking on a replay.",
             ["OOMKilled restarts", "consumer lag spikes", "delayed emails"], severity="SEV-3")
    s.truth = truth(["notification-service"], "bad_deployment", also=["resource_exhaustion"], trigger="dep-5402", decoys=["cfg-2410"],
                    key_evidence=["dep-5402", "evictions=0"], tags=["slow-burn", "needs-wider-window", "misleading-report", "decoy-config"],
                    narrative="v3.8.0 (3 days ago) added an unbounded template cache; memory saw-tooths to OOM every ~22h. "
                              "Two restarts predate the Kafka change the report blames.")
    return s


def inc_005() -> Scenario:
    s = Scenario("INC-005", "2026-04-02T08:00:00", seed=105)
    s.config("cfg-2502", "payment-service", 200, "bank.api.timeout_ms", 3000, 5000, "Raise bank API timeout after partner advisory", author="p.nair")
    s.deploy("dep-5510", "settlement-service", 150, "v1.9.0", "Batch settlement report export", ["new CSV exporter"])
    for m in (20, 140, 230):
        s.log(m, "payment-service", "WARN", f"cert-watcher: client certificate CN=payments-client.orangeinvest.internal expires in {int((255 - m))} minutes (notAfter={s.ts(255)})")
    s.set("payment-service", "bank_api_error_rate", 255, None, 100, noise=0)
    s.set("payment-service", "payment_success_rate", 255, None, 2.5, noise=0.2)
    s.set("payment-service", "latency_p99_ms", 255, None, 140)
    s.set("ledger-db", "slow_queries_per_min", 238, 247, 9)
    s.burst("payment-service", 255, None, "ERROR", [
        "bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=" + s.ts(255),
        "payment {id} marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected"], 3)
    s.report("Customers say payments fail, but the error-rate dashboard is green", 272, "payment-service",
             "Support has ~40 tickets about failed fund transfers in the last 15 minutes. The payment-service HTTP "
             "error-rate panel shows nothing unusual, so this may be a client app issue or the bank side.",
             ["customer-reported payment failures", "payment-service error_rate normal"], reporter="support lead")
    s.truth = truth(["payment-service"], "credential_expiry", decoys=["cfg-2502", "HIST-105"],
                    key_evidence=["expired certificate", "payment_success_rate"], tags=["conflicting-observations", "decoy-config", "misleading-history"],
                    narrative="The mTLS client certificate expired at 12:15. HTTP error_rate stays flat because failures are "
                              "returned as 200 PENDING_RETRY; payment_success_rate and logs show the truth.")
    return s


def inc_006() -> Scenario:
    s = Scenario("INC-006", "2026-04-09T09:00:00", seed=106)
    s.deploy("dep-5604", "auth-service", 150, "v7.2.0", "Session cookie hardening", ["SameSite=strict"])
    s.log(262, "redis-cache", "ERROR", "node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down")
    s.log(262.4, "redis-cache", "WARN", "failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)")
    s.burst("redis-cache", 263, 280, "WARN", ["MASTER <-> REPLICA sync started (full resync) for redis-cache-2", "client reconnect storm: {k} new connections in 10s"], 0.5)
    s.set("redis-cache", "hit_ratio_pct", 262, None, 11, 48, noise=0.03)
    s.set("redis-cache", "memory_pct", 262, None, 6, 31)
    s.set("redis-cache", "connected_clients", 262, 266, 40)
    s.set("market-data-service", "latency_p99_ms", 262, None, 1950, 900)
    s.set("market-data-service", "cpu_pct", 262, None, 91, 78)
    s.set("market-data-service", "error_rate", 262, None, 3.2)
    s.set("pricing-engine", "latency_p99_ms", 262, None, 2300, 1100)
    s.set("pricing-engine", "error_rate", 263, None, 4.1)
    s.set("portfolio-service", "latency_p99_ms", 262, None, 900, 520)
    s.set("api-gateway", "latency_p99_ms", 262, None, 2500, 1300)
    s.burst("market-data-service", 262, None, "WARN", ["cache miss storm: quote:{k} not in redis-cache, rebuilding from exchange feed snapshot ({ms}ms)"], 3)
    s.burst("pricing-engine", 263, None, "ERROR", ["quote computation deadline exceeded: market-data-service took {ms}ms"], 2)
    s.faults = [
        {"tool": "query_logs", "match": {"service": "redis-cache"}, "kind": "timeout", "times": 2},
        {"tool": "query_metrics", "match": {"service": "market-data-service"}, "kind": "unavailable", "times": 4},
        {"tool": "get_deployments", "match": {}, "kind": "partial", "times": 1},
    ]
    s.report("Quote latency degraded across trading screens", 270, "pricing-engine",
             "Quotes are taking 2-3s to load. pricing-engine deadline-exceeded alerts firing. No releases to pricing today as far as we know.",
             ["pricing-engine p99 > 2s", "slow quotes in app"])
    s.truth = truth(["redis-cache"], "infrastructure_failure", key_evidence=["redis-cache-2", "hit_ratio_pct"],
                    tags=["tool-failures", "cascading", "symptom-not-at-cause"],
                    narrative="Redis primary failed and an empty replica was promoted -> cold cache -> miss storm in market-data -> slow pricing.")
    return s


def inc_007() -> Scenario:
    s = Scenario("INC-007", "2026-04-15T10:00:00", seed=107)
    s.deploy("dep-5710", "auth-service", 240, "v7.3.0", "Upgrade JWT library to 4.x; refactor token validation",
             ["jjwt 0.11 -> 0.12 (breaking parser API)", "TokenValidator rewritten"], author="d.verma")
    s.config("cfg-2705", "auth-service", 241, "jwt.signing.key_id", "k-2025-10", "k-2026-04",
             "Scheduled signing-key rotation", author="security-automation")
    s.deploy("dep-5699", "market-data-service", 95, "v5.0.3", "Fix symbol mapping", ["update ISIN map"])
    s.set("auth-service", "error_rate", 242, None, 36)
    s.set("api-gateway", "error_rate", 242, None, 21)
    s.burst("api-gateway", 242, None, "WARN", ["auth-service POST /v1/token/validate -> 401 invalid_token (trace {id})"], 4)
    s.faults = [{"tool": "query_logs", "match": {"service": "auth-service"}, "kind": "unavailable", "times": None}]
    s.report("Users being logged out / 401s on every API call", 252, "auth-service",
             "About a third of token validations fail with 401. Note: the log pipeline for the identity namespace has been "
             "down since this morning (separate ticket), so auth-service logs are not searchable.",
             ["401 invalid_token", "auth-service error rate 36%"], severity="SEV-1")
    s.truth = truth([], "unknown", outcome="inconclusive", decoys=[],
                    key_evidence=["dep-5710", "cfg-2705"], tags=["inconclusive", "tool-failures", "permanent-outage", "two-simultaneous-changes"],
                    narrative="A JWT library upgrade and a signing-key rotation landed 1 minute apart on auth-service and its logs "
                              "are unavailable. Nothing in the reachable data separates the two; the honest answer is inconclusive, "
                              "naming both candidates.")
    s.truth["candidate_change_ids"] = ["dep-5710", "cfg-2705"]
    return s


def inc_008() -> Scenario:
    s = Scenario("INC-008", "2026-04-22T03:00:00", seed=108)
    s.config("cfg-2801", "auth-service", 40, "log.level", "INFO", "WARN", "Reduce log volume")
    s.deploy("dep-5803", "portfolio-service", 170, "v4.4.0", "Holdings export", ["CSV export endpoint"])
    s.log(228, "notification-service", "INFO", "campaign 'ipo-open-reminder' dispatched: 2,140,000 push notifications queued")
    s.set("notification-service", "request_rate", 228, 240, 2100)
    s.set("api-gateway", "request_rate", 230, 240, 1300, 4900)
    s.set("api-gateway", "request_rate", 240, None, 4900)
    s.set("auth-service", "request_rate", 230, 240, 400, 1750)
    s.set("auth-service", "request_rate", 240, None, 1500)
    s.set("auth-service", "cpu_pct", 232, None, 97, noise=0.01)
    s.set("auth-service", "latency_p99_ms", 234, None, 3800)
    s.set("auth-service", "error_rate", 235, None, 14)
    s.set("auth-service", "rate_limited_per_min", 235, None, 900)
    s.set("user-db", "connections_active", 232, None, 78)
    s.set("api-gateway", "latency_p99_ms", 234, None, 3900)
    s.set("api-gateway", "error_rate", 235, None, 9)
    s.burst("auth-service", 234, None, "WARN", ["worker pool saturated (active=64/64, queue={k})"], 2)
    s.burst("auth-service", 235, None, "ERROR", ["request rejected: accept queue full, returning 503 (POST /v1/auth/login)"], 3)
    s.burst("auth-service", 236, None, "WARN", ["autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further"], 0.3)
    s.report("Login is slow or failing", 246, "auth-service",
             "Login latency ~4s and 503s. Looks a lot like the bcrypt regression we had last year after an auth-service release.",
             ["auth-service p99 3.8s", "503 on /v1/auth/login"])
    s.truth = truth(["auth-service", "api-gateway", "notification-service"], "capacity_saturation", also=["resource_exhaustion"],
                    decoys=["HIST-101", "cfg-2801"], key_evidence=["maxReplicas 8", "request_rate"],
                    tags=["misleading-history", "no-change-cause"],
                    narrative="A push campaign quadrupled login traffic; auth-service is capped at 8 replicas and saturates. No deploy is involved.")
    return s


def inc_009() -> Scenario:
    s = Scenario("INC-009", "2026-05-05T05:00:00", seed=109)
    s.config("cfg-2903", "ledger-db", 180, "autovacuum_vacuum_scale_factor", 0.2, 0.1, "More aggressive autovacuum", author="dba-team")
    s.deploy("dep-5905", "portfolio-service", 236, "v4.5.0", "Add realised P&L breakdown to holdings API",
             ["new query on trades filtered by account_id + executed_at", "no migration included"], author="k.shah")
    s.set("ledger-db", "cpu_pct", 238, None, 96, noise=0.01)
    s.set("ledger-db", "slow_queries_per_min", 238, None, 140)
    s.set("ledger-db", "connections_active", 238, None, 192)
    s.set("portfolio-service", "db_pool_in_use", 238, None, 50, noise=0)
    s.set("portfolio-service", "latency_p99_ms", 238, None, 6900)
    s.set("order-service", "latency_p99_ms", 239, None, 5200)
    s.set("order-service", "error_rate", 240, None, 9.4)
    s.set("order-service", "db_pool_in_use", 239, None, 46)
    s.set("api-gateway", "latency_p99_ms", 239, None, 5600)
    s.burst("ledger-db", 238, None, "WARN", [
        "slow query {ms}ms: SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service"], 3)
    s.burst("order-service", 240, None, "ERROR", ["ledger-db: canceling statement due to statement timeout (INSERT INTO orders) orderId={id}"], 2.5)
    s.burst("portfolio-service", 239, None, "WARN", ["holdings P&L query took {ms}ms for account {k}"], 2)
    s.report("Order placement very slow, some orders timing out", 250, "order-service",
             "order-service p99 > 5s with statement timeouts against ledger-db. order-service has not been released this week. "
             "DBA changed autovacuum settings this morning.",
             ["order-service p99 5s", "ledger-db statement timeouts"], severity="SEV-1")
    s.truth = truth(["portfolio-service"], "bad_deployment", trigger="dep-5905", decoys=["cfg-2903", "HIST-110"],
                    key_evidence=["dep-5905", "Seq Scan on trades"], tags=["noisy-neighbour", "symptom-not-at-cause", "decoy-config", "multi-hop"],
                    narrative="portfolio-service v4.5.0 runs an unindexed query that saturates the shared ledger-db; order-service is a victim.")
    return s


def inc_010() -> Scenario:
    s = Scenario("INC-010", "2026-05-12T04:00:00", seed=110)
    s.config("cfg-3004", "settlement-service", 215, "consumer.max.poll.records", 500, 200, "Smaller poll batches to smooth DB writes", author="m.das")
    s.deploy("dep-6010", "order-service", 220, "v2.16.0", "Order events v2",
             ["OrderExecuted event: rename field qty -> quantity", "add field venue", "event schema version header v2"], author="a.khan")
    s.set("settlement-service", "consumer_lag", 222, None, 400, 61000, noise=0.02)
    s.set("settlement-service", "request_rate", 222, None, 0.4)
    s.set("settlement-service", "cpu_pct", 222, None, 71)
    s.burst("settlement-service", 222, None, "ERROR", [
        "failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)",
        "poison message: retrying orders-3@8841207 (attempt {k}), partition blocked"], 3)
    s.burst("notification-service", 222, None, "INFO", ["OrderExecuted v2 consumed (unknown field 'venue' ignored)"], 0.4)
    s.report("Trade settlements are stuck", 262, "settlement-service",
             "No settlements posted for ~40 minutes; settlement consumer lag keeps growing. We changed the consumer poll "
             "settings on settlement-service shortly before this started.",
             ["settlement consumer lag 60k and climbing", "zero settlements processed"])
    s.truth = truth(["order-service"], "bad_deployment", also=["data_issue"], trigger="dep-6010", decoys=["cfg-3004"],
                    key_evidence=["dep-6010", "missing required field 'qty'"], tags=["misleading-report", "decoy-config", "symptom-not-at-cause", "multi-hop"],
                    narrative="order-service v2.16.0 renamed an event field; the v1-only settlement consumer is stuck on the first v2 message. "
                              "The poll-size change 5 min earlier is a coincidence.")
    return s


def inc_011() -> Scenario:
    s = Scenario("INC-011", "2026-05-20T08:00:00", seed=111)
    s.deploy("dep-6111", "market-data-service", 236, "v5.2.0", "Switch quote serialization to protobuf",
             ["protobuf quote payloads for internal callers"], author="v.rao")
    s.config("cfg-3118", "api-gateway", 150, "ratelimit.burst", 200, 250, "Raise burst limit")
    s.config("cfg-3120", "pricing-engine", 244, "flags.pricing.greeks_v2.rollout_pct", 5, 100,
             "Ramp Greeks v2 experiment to all traffic", author="pm-experiments")
    s.set("pricing-engine", "cpu_pct", 245, None, 99, noise=0.005)
    s.set("pricing-engine", "latency_p99_ms", 245, None, 2650)
    s.set("pricing-engine", "error_rate", 246, None, 12.5)
    s.set("order-service", "latency_p99_ms", 246, None, 2900)
    s.set("order-service", "error_rate", 246, None, 8.2)
    s.burst("pricing-engine", 245, None, "WARN", ["GreeksV2Calculator: Monte-Carlo paths=20000 took {ms}ms for symbol opt-{k}"], 3)
    s.burst("pricing-engine", 246, None, "ERROR", ["request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2"], 2.5)
    s.burst("order-service", 246, None, "ERROR", ["pricing-engine quote call timed out after 2500ms orderId={id}"], 2)
    s.faults = [{"tool": "get_config_changes", "match": {}, "kind": "partial", "times": 1, "drop_ids": ["cfg-3120"],
                 "detail": "INCOMPLETE RESULT: config-audit shard 2/3 did not respond; changes may be missing. Retry to get the full result."}]
    s.report("Order quotes timing out", 256, "order-service",
             "Orders failing at the quote step with timeouts. market-data-service shipped a serialization change a few minutes before.",
             ["order-service quote timeouts", "pricing-engine p99 2.6s"])
    s.truth = truth(["pricing-engine"], "config_change", trigger="cfg-3120", decoys=["dep-6111"],
                    key_evidence=["cfg-3120", "GreeksV2"], tags=["tool-failures", "incomplete-results", "decoy-deploy", "misleading-report"],
                    narrative="A feature flag ramped an expensive Monte-Carlo calculator 5%->100%. The first config-change query silently "
                              "omits it (partial result); market-data metrics are healthy.")
    return s


def inc_012() -> Scenario:
    s = Scenario("INC-012", "2026-06-01T11:00:00", seed=112)
    s.deploy("dep-6203", "payment-service", 226, "v6.0.0", "New UPI mandate flow", ["mandate create/approve endpoints", "new ledger entry type MANDATE_HOLD"],
             author="p.nair", rolled_back_at=252)
    s.set("ledger-db", "disk_used_pct", 0, 240, 78, 100, noise=0.002)
    s.set("ledger-db", "disk_used_pct", 240, None, 100, noise=0)
    s.set("ledger-db", "replication_lag_ms", 0, None, 900, 5200)
    for m, pct in ((20, 80), (130, 90), (195, 95), (228, 98)):
        s.log(m, "ledger-db", "WARN", f"disk usage {pct}% on /var/lib/postgresql/data")
    s.burst("ledger-db", 60, 240, "WARN", ["replication slot 'analytics_cdc' is inactive; retaining {k} MB of WAL"], 0.05)
    s.burst("ledger-db", 240, None, "ERROR", ["could not extend file \"base/16402/2619\": No space left on device",
                                              "PANIC: could not write to file \"pg_wal/xlogtemp.{k}\": No space left on device"], 2.5)
    s.set("payment-service", "error_rate", 240, None, 62)
    s.set("payment-service", "payment_success_rate", 240, None, 35)
    s.set("order-service", "error_rate", 240, None, 48)
    s.set("settlement-service", "error_rate", 240, None, 55)
    s.set("api-gateway", "error_rate", 240, None, 17)
    s.burst("payment-service", 240, None, "ERROR", ["ledger write failed: ERROR: could not extend file: No space left on device (payment {id})"], 3)
    s.burst("order-service", 240, None, "ERROR", ["INSERT INTO orders failed: No space left on device orderId={id}"], 2)
    s.faults = [{"tool": "query_metrics", "match": {"service": "ledger-db"}, "kind": "rate_limited", "times": 1}]
    s.report("Payments broken after payment-service v6.0.0", 250, "payment-service",
             "Payment failures started ~15 minutes after the v6.0.0 release (new mandate flow writes a new ledger entry type). Rolling back now.",
             ["payment-service error rate 60%", "ledger write failures"], severity="SEV-1")
    s.truth = truth(["ledger-db"], "resource_exhaustion", also=["infrastructure_failure"], decoys=["dep-6203"],
                    key_evidence=["No space left on device", "disk_used_pct"], tags=["misleading-report", "decoy-deploy", "tool-failures", "symptom-not-at-cause"],
                    narrative="ledger-db's disk filled (WAL retained by an inactive replication slot). Disk was climbing hours before the "
                              "deploy, undeployed services fail the same way, and the rollback did not help.")
    return s


HISTORY = [
    ("HIST-101", "2025-08-14", "Login latency after auth-service release", ["auth-service"], ["login p99 4s", "503 on login", "high cpu"],
     "bcrypt cost factor raised 10->14 in release v6.4.0", "bad_deployment", "Rolled back v6.4.0"),
    ("HIST-102", "2025-09-02", "Portfolio timeouts during ledger-db failover", ["portfolio-service", "ledger-db"], ["portfolio page timeouts", "504 at gateway"],
     "Unplanned ledger-db primary failover; 6 min of refused connections", "infrastructure_failure", "Failover completed; added retry"),
    ("HIST-103", "2025-10-21", "Order failures during exchange partner outage", ["order-service", "exchange-gateway"], ["504 on order submit", "upstream timeouts"],
     "External exchange API outage on the partner side", "dependency_failure", "Failed over to secondary exchange route; partner fixed"),
    ("HIST-104", "2025-11-05", "notification-service lag after Kafka broker rebalance", ["notification-service", "kafka"], ["consumer lag spike", "delayed emails"],
     "Partition reassignment caused consumer group rebalancing storm", "infrastructure_failure", "Throttled reassignment"),
    ("HIST-105", "2025-11-19", "Payment failures: bank partner API outage", ["payment-service"], ["payment failures", "bank api errors"],
     "Bank partner API returned 5xx for 40 minutes (their incident)", "dependency_failure", "Waited for partner; queued retries"),
    ("HIST-106", "2025-06-30", "Settlement mTLS certificate expired", ["settlement-service"], ["tls handshake failures", "expired certificate"],
     "Client certificate expired; no renewal automation", "credential_expiry", "Issued new cert; added expiry alert"),
    ("HIST-107", "2025-12-09", "Cold cache storm after redis failover", ["redis-cache", "market-data-service"], ["cache miss storm", "quote latency"],
     "Redis primary failure promoted an empty replica", "infrastructure_failure", "Warmed cache; fixed replica sync checks"),
    ("HIST-108", "2025-10-01", "401s after JWT signing key rotation", ["auth-service"], ["401 invalid_token", "users logged out"],
     "Key rotation published new key id before all pods loaded the JWKS", "config_change", "Re-published previous key; staged rotation"),
    ("HIST-109", "2025-07-11", "Token validation regression after auth library upgrade", ["auth-service"], ["401 invalid_token", "token validation failures"],
     "JWT library upgrade changed clock-skew default", "bad_deployment", "Rolled back; pinned skew config"),
    ("HIST-110", "2026-01-15", "Slow orders from ledger-db IOPS throttling", ["order-service", "ledger-db"], ["order latency", "statement timeouts", "slow queries"],
     "Cloud volume burst credits exhausted; IOPS throttled", "infrastructure_failure", "Moved to provisioned IOPS"),
    ("HIST-111", "2025-05-22", "Reporting job full table scans slowed ledger-db", ["ledger-db", "settlement-service"], ["slow queries", "high db cpu"],
     "New report query without index did sequential scans", "bad_deployment", "Added index; query review gate"),
    ("HIST-112", "2026-02-03", "Settlement consumer stalled after partition reassignment", ["settlement-service", "kafka"], ["consumer lag growing", "settlements stuck"],
     "Consumer lost partition assignment during broker maintenance", "infrastructure_failure", "Restarted consumer group"),
    ("HIST-113", "2026-01-28", "pricing-engine CPU saturation at market open", ["pricing-engine"], ["quote timeouts", "cpu 99%"],
     "Record market-open volume exceeded provisioned capacity", "capacity_saturation", "Raised replica count; pre-scale before open"),
    ("HIST-114", "2025-04-18", "ledger-db disk full from retained WAL", ["ledger-db"], ["no space left on device", "write failures"],
     "Inactive replication slot retained WAL until disk filled", "resource_exhaustion", "Dropped slot; added slot-lag alert"),
    ("HIST-115", "2025-09-25", "order-service connection pool misconfiguration", ["order-service"], ["connection is not available", "request timed out", "pool exhausted"],
     "Datasource pool size overridden by a wrong environment profile", "config_change", "Reverted config; added validation"),
    ("HIST-116", "2025-12-18", "HTTP/2 resets after gateway envoy upgrade", ["api-gateway"], ["504 at gateway", "upstream resets", "timeouts"],
     "Envoy upgrade changed HTTP/2 max concurrent streams default", "bad_deployment", "Rolled back gateway"),
]

SCENARIOS = [inc_001, inc_002, inc_003, inc_004, inc_005, inc_006, inc_007, inc_008, inc_009, inc_010, inc_011, inc_012]


def main() -> None:
    if (OUT / "incidents").exists():
        shutil.rmtree(OUT / "incidents")
    OUT.mkdir(exist_ok=True)
    callers: dict[str, int] = {}
    for _, to, _ in EDGES:
        callers[to] = callers.get(to, 0) + 1
    (OUT / "topology.json").write_text(json.dumps({
        "nodes": [{"name": n, "type": t, "owner": o, "has_telemetry": t != "external", "shared": n in SHARED} for n, t, o in NODES],
        "edges": [{"from": a, "to": b, "kind": k} for a, b, k in EDGES]}, indent=2))
    (OUT / "historical_incidents.json").write_text(json.dumps(
        [{"id": i, "date": d, "title": t, "services": sv, "symptoms": sy, "root_cause": rc, "category": c, "resolution": r}
         for i, d, t, sv, sy, rc, c, r in HISTORY], indent=2))
    for fn in SCENARIOS:
        s = fn()
        s.write()
        print(f"{s.id}: {len(s.logs):5d} log lines, {len(s.deploys)} deploys, {len(s.configs)} config changes, "
              f"{len(s.faults)} faults -> {s.truth['expected_outcome']} / {s.truth['category']}")


if __name__ == "__main__":
    main()
