# Incident investigation report: INC-008
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause: auth-service capacity saturation. At 06:50Z inbound login traffic stepped ~3.8x (F10/F15) and this same surge is present at api-gateway ingress (~1266 -> ~4900 req/s, F23/F24/F29), so it is real upstream load, not an auth-internal retry or metering artifact. auth-service has no deployment and no functional config change in the window (F1, F2), yet its CPU stepped in the same 06:50Z bucket from ~38% to ~97-98% and pinned there (F9/F16), self-throttling spiked ~450x (F17), p99 rose to ~3.8s (F7) and requests were rejected with 503 'accept queue full' (F8). Alternatives are contradicted by direct measurement: no auth-service release (F1/F6), no step at the portfolio-service deploy timestamp and a flat shared redis-cache (F13/F14), and user-db/redis-cache healthy on all exposed metrics at onset (F19-F22). Confidence 0.85 (high); the only unresolved element is the provenance of the external surge, which is unmeasurable with the current telemetry and does not change the origin or the fix.

## Probable root cause
**Capacity saturation: auth-service login traffic surged ~3.8x and saturated its provisioned CPU** -- capacity_saturation in `auth-service`, triggered by `null` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H4: Capacity saturation: auth-service login traffic surged ~3.8x and saturated its provisioned CPU | capacity_saturation | auth-service | null | 0.85 (high) | active |
| 2 | H2: Shared downstream dependency (redis-cache/user-db) degraded, auth-service is the victim | dependency_failure | redis-cache | null | 0.05 (low) | rejected |
| 3 | H1: Reporter's theory: recurrence of bcrypt/crypto regression in auth-service | bad_deployment | auth-service | null | 0.03 (low) | rejected |
| 4 | H3: portfolio-service v4.4.0 deploy caused the incident via shared redis-cache | bad_deployment | portfolio-service | dep-5803 | 0.03 (low) | rejected |
| 5 | H5: Misconfigured logging change (cfg-2801) altered runtime behavior | config_change | auth-service | cfg-2801 | 0.03 (low) | rejected |

### H4: Capacity saturation: auth-service login traffic surged ~3.8x and saturated its provisioned CPU
At 06:50Z auth-service inbound login traffic stepped ~3.8x (390-395 -> ~1460-1540 req/s), and the same surge is present at api-gateway ingress (~1266 -> ~4900 req/s), so it is real inbound load upstream of auth-service, not an auth-internal retry/metering artifact. With no release (F1) and no functional config change (F2) on auth-service, its fixed CPU allocation saturated (~97-98%), p99 rose to ~3.8s, self-throttling spiked ~450x, and requests were rejected with 503 'accept queue full'. The origin of the incident is auth-service's saturated provisioned capacity; the provenance of the external surge itself is not identifiable with the exposed metric set (no caller-identity/route metric).

**Causal chain**
1. External inbound login traffic surges ~3.8-3.9x; the identical step is present at api-gateway ingress, proving the load originates upstream/external to auth-service rather than being internal retry amplification [F10, F15, F23, F24, F29]
2. auth-service CPU steps in the same 06:50Z bucket as the load and pins at ~97-98% (its provisioned ceiling), while memory stays flat and no restart occurs [F9, F15, F16, F12, F27]
3. Because auth-service has no spare compute, request queueing grows and p99 latency rises to ~3.8-4.0s (error signal steps one bucket later at 06:54Z, after the load/CPU step and the queue build-up) [F7, F16, F34]
4. The accept queue fills; auth-service rejects with 503 'request rejected: accept queue full' and self-throttling rises ~450x above baseline [F8, F11, F17]
5. api-gateway, which passes the same traffic through without CPU saturation, only inherits the multi-second latency once auth-service starts 503-ing (its volume ramp precedes its latency jump) [F25, F26, F28]

**Supporting evidence**
  - `F7` ✔ auth-service p99 latency was flat at ~85-95 ms through 06:52:00Z, then jumped at the 06:54:00Z bucket to avg 3759.86 ms / max 3849.2 ms and stayed ~3.5-4.0s for the rest of the window.
  - `F8` ✔ The earliest auth-service error log is at 06:55:42Z; all 372 ERROR logs in 06:00-09:00Z are the single pattern 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' — no bcrypt/crypto, timeout, connection-refused, or OOM signature appears.
  - `F9` ✔ auth-service CPU jumped from a ~33-40% baseline (06:00-06:45) to avg 84.31 / max 97.33 in the 06:50 bucket, and stayed ~96-98% through 08:50.
  - `F10` ✔ auth-service request_rate rose from a ~393-400 req/s baseline to avg 1078.03 / max 1813.74 in the 06:50 bucket, then settled ~1480-1520 req/s — roughly a 3.8x traffic surge at the same time as the CPU and latency jumps.
  - `F11` ✔ auth-service error_rate rose from ~0.2% baseline to avg 7.11 / max 14.76 in the 06:50 bucket, plateauing ~14% (HTTP 5xx) thereafter.
  - `F12` ✔ auth-service memory_pct stayed flat across the whole window (~51-55% avg, max 57.07), so the degradation is not memory/OOM-driven; the resource anomaly is CPU-only.
  - `F15` ✔ The auth-service onset is a simultaneous, single-bucket step at 06:50Z in BOTH load and saturation signals: request_rate jumps from ~390-395 req/s baseline (06:45 bucket avg 390.98) to avg 680.57/max 1012.36 in the 06:50 bucket, then 1475.5/max 1813.74 in the 06:55 bucket, settling ~1460-1540 req/s. auth-service CPU shows the identical 06:50 step (see next finding). Same timestamp, same bucket = coupled onset, and no other bucket in 05:00-08:59 shows a comparable discontinuity.
  - `F16` ✔ auth-service resource usage is consistent with a CPU-bound/compute-saturation regression, not a downstream wait: cpu_pct is flat at ~37-38% through 06:40 (06:40 bucket avg 38.46, max 40.45) then steps to avg 84.31/max 97.33 in the 06:50 bucket and pins at observed max 99.54 (06:50) / 98.84 (07:10) and sustains 96.7-97.7% avg for the rest of the window. A saturated-on-CPU service at ~97-98% with latency ~3.8s and no free capacity is the signature of compute-bound saturation, not of an idle/blocked thread pool waiting on a downstream.
  - `F17` ✔ auth-service self-throttling/rejection spikes in the SAME 06:50 bucket: rate_limited_per_min goes from ~1.7-2.2 req/min baseline (06:40 bucket avg 1.89) to avg 438.67/max 919.45 at 06:50, then plateaus ~890-915 req/min (avg 901.07 at 07:00; summary mean 469.4). The service is rejecting/limiting ~450x its baseline volume at the exact onset minute — an overload-saturation indicator on auth-service itself, not a downstream-induced wait.
  - `F23` ✔ api-gateway request_rate is FLAT at ~1250 req/s from 05:00Z through 06:30Z (10/15-min buckets all avg 1237-1267), then steps up starting in the 06:45Z bucket (avg 2532.91, max 4859.74) and settles ~4875-4934 req/s from 07:00Z onward — a persistent ~3.9x elevation present at the gateway ingress itself, not just inside auth-service.
  - `F24` ✔ Zoomed to 120s buckets over 06:40-07:00Z, api-gateway request_rate climbs continuously from ~1266/1228 req/s (06:40/06:48) via 1500.82 (06:50) and 2327.7 (06:52) to 3294.15 (06:54), 3939.86 (06:56) and 4790.49 (06:58) — a smooth accelerating ramp, not a single hard step, agreeing in onset time with the auth-service surge (06:50Z).
  - `F25` ✔ api-gateway latency_p99_ms stays flat ~200-220 ms through the 06:51Z bucket (avg 207.77), then jumps to avg 3869.46 / max 4024.51 at 06:54Z and holds ~3.8-4.0s — showing the gateway only inherits the multi-second latency once upstream auth starts returning 503s, while the gateway volume ramp began ~1-2 buckets earlier.
  - `F26` ✔ api-gateway CPU is NOT saturated despite the ~3.9x traffic increase: cpu_pct stays flat at avg 36.9-44.9% across 06:40-07:10Z (e.g. 06:50 avg 40.93, 06:56 avg 43.39, mean 41.37). Unlike auth-service (which pins ~97-98%), the gateway has abundant headroom — the org-level episode is not gateway-side compute saturation.
  - `F27` ✔ auth-service restarts metric is exactly 0 for every 120s bucket across 06:40-07:00Z (min/max/mean all 0.0) — auth-service did NOT restart at or around the 06:50Z onset.
  - `F29` ✔ Interpretation: the ~3.8-3.9x request_rate surge is measured at api-gateway ingress too, so it is external/upstream of auth-service (real inbound load), not an auth-internal retry amplification or a metering artifact confined to auth-service. The gateway passes it through without saturating, while auth-service (which receives the same traffic) pins at ~97% CPU and begins 503-ing — consistent with H4 (auth-service capacity saturation), but the *origin* of the external load surge is not identified by these metrics.
  - `F34` ✔ auth-service error_rate is FLAT at the 06:50 and 06:52 buckets (avg 0.22 and 0.16, i.e. still baseline ~0.18-0.28) and only steps at 06:54 (avg 6.71/max 13.19), plateauing ~13.6-14.5% from 06:56 onward (summary max 14.76). So using the 120s-bucket resolution, auth-service's error onset is at 06:54Z, two buckets after the 06:50Z CPU/load step. This confirms the degradation is NOT present in the 06:50-06:52 span in the error signal.
**Contradicting evidence**
  - `F14` ✔ redis-cache — the SHARED component that portfolio-service and auth-service both depend on (per F5) — shows no step change at the 05:50Z deploy nor at the 06:50-06:55Z auth onset: cpu_pct stays 21-22% flat across the entire 05:00-08:50 window (06:40 bucket avg 22.32, 06:50 bucket avg 21.57, 07:00 avg 22.14; summary min 19.51 max 25.59). No downstream cache resource anomaly exists to explain the auth degradation.
**Assumptions**
  - auth-service CPU is the binding constraint (allocation not scaled up) so ~3.8x load necessarily saturates it — no direct provisioned-limit metric is present, inferred from the pin at ~97-98% (F16). If instead the 06:50Z step were a capacity *reduction* (autoscaler min/allocatable change, node event), the mechanism would be a capacity change with the same fix; no deploy/config evidence of such a change exists (F1, F2).
  - The surge is genuine demand rather than a metering artifact — supported by it appearing at gateway ingress (F23/F29). Its provenance (legitimate external clients vs synthetic load test vs a retrying upstream caller) is NOT determinable from the exposed telemetry: no caller-identity metric exists, and no metric isolates inbound volume to auth-service specifically. This is a lower-confidence clause about the driver, not about the origin of the incident.
  - F14 (redis cpu flat) is listed as contradicting only in the narrow sense that it refutes a shared-cache-mediated surge; it does not contradict auth-service capacity saturation.

### H2: Shared downstream dependency (redis-cache/user-db) degraded, auth-service is the victim
A shared login-path dependency (redis-cache or user-db) degraded (e.g. failover/slow), causing auth-service login handlers to block, raising latency and generating rejections.

**Causal chain**
1. redis-cache or user-db degrades (failover/slowness) on the login path *(assumed)*
2. auth-service login handler blocks waiting on the dependency, consuming capacity without producing CPU-bound work *(assumed)*
3. Login p99 rises to ~3.8s and 503s appear [F7, F8]
4. Contradicted: the shared redis-cache is healthy on all four exposed metrics across onset (hit_ratio ~96%, evictions ~2-3/min, connected_clients ~300s, memory ~70%, cpu 21-22% flat) [F14, F21]
5. Contradicted: user-db shows no saturation/slowness/lag at onset (cpu flat ~18%, slow_queries flat, replication_lag flat); its only change is a ~3x rise in connections_active matching the traffic surge, i.e. more work arriving, not the DB degrading [F19, F20]
6. Contradicted by mechanism: the observed signature is CPU pinned ~97-98% plus self-throttling and 'accept queue full', an overload signature, not an idle/blocked-thread wait on a downstream [F16, F17, F8]

**Supporting evidence**
  - `F4` ✔ auth-service login path dependencies: auth-service -> user-db (db), auth-service -> redis-cache (cache), auth-service -> kyc-provider (external), api-gateway -> auth-service (http). redis-cache is marked shared=true and user-db shared=false.
  - `F5` ✔ redis-cache is a SHARED component: it is a cache dependency of auth-service, portfolio-service, pricing-engine and market-data-service. Notably portfolio-service — the one service that did deploy (2026-04-22T05:50Z) — shares redis-cache with auth-service.
**Contradicting evidence**
  - `F14` ✔ redis-cache — the SHARED component that portfolio-service and auth-service both depend on (per F5) — shows no step change at the 05:50Z deploy nor at the 06:50-06:55Z auth onset: cpu_pct stays 21-22% flat across the entire 05:00-08:50 window (06:40 bucket avg 22.32, 06:50 bucket avg 21.57, 07:00 avg 22.14; summary min 19.51 max 25.59). No downstream cache resource anomaly exists to explain the auth degradation.
  - `F18` ✔ No downstream-wait metric is available to support the 'downstream wait' hypothesis: redis-cache exposes only [connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct] — no latency metric; user-db exposes only [connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min] — no latency metric. So the login-path dependencies cannot present a latency-wait signature in this telemetry at all, which itself argues against a downstream-wait diagnosis being evidenced here; and no connection-pool/lag anomaly was observed in the metrics that DO exist (redis cpu flat).
  - `F19` ✔ user-db shows NO step change at the 06:50Z auth-service onset in any of its resource/latency-adjacent metrics: cpu_pct is flat 17.5-18.5% across 06:40-06:58 (06:49 bucket avg 18.06 -> 06:52 avg 18.45, summary min 16.63 max 19.24), slow_queries_per_min flat 0.25-0.35 (06:49 avg 0.31 -> 06:52 avg 0.34, max 0.42), replication_lag_ms flat ~29-31ms (06:49 avg 30.53 -> 06:52 avg 30.26, max 31.55). The database is not saturated, slow-querying or lagging during the login degradation.
  - `F20` ✔ user-db connections_active does step up at 06:52Z: 24.83-25.9 across 06:40-06:49, then 75.49 avg/79.84 max at 06:52, 78.78/81.93 at 06:55, 75.67/79.06 at 06:58 (summary min 23.39 max 81.93 mean 47.35). This is a ~3x rise in concurrent DB connections — the mirror image of auth-service's ~3.8x traffic surge (F10) — but with no accompanying user-db CPU, slow-query or lag increase, i.e. more work arriving, not the database degrading.
  - `F21` ✔ redis-cache is healthy on all four requested metrics with no step at 06:50Z: hit_ratio_pct 95.89-96.34% (06:49 avg 96.34 -> 06:55 avg 95.29; summary min 94.44 max 97.15), connected_clients 303-318 (06:49 avg 311.76 -> 06:52 avg 307.33; summary min 285.87 max 336.28), evictions_per_min 2.0-3.5 (summary min 1.17 max 4.42 mean 2.9), memory_pct ~69-73% (summary min 67.68 max 76.3 mean 70.73). The shared cache shows no eviction storm, memory pressure, client drop or hit-ratio collapse coincident with the 06:50Z degradation.
  - `F16` ✔ auth-service resource usage is consistent with a CPU-bound/compute-saturation regression, not a downstream wait: cpu_pct is flat at ~37-38% through 06:40 (06:40 bucket avg 38.46, max 40.45) then steps to avg 84.31/max 97.33 in the 06:50 bucket and pins at observed max 99.54 (06:50) / 98.84 (07:10) and sustains 96.7-97.7% avg for the rest of the window. A saturated-on-CPU service at ~97-98% with latency ~3.8s and no free capacity is the signature of compute-bound saturation, not of an idle/blocked thread pool waiting on a downstream.
  - `F17` ✔ auth-service self-throttling/rejection spikes in the SAME 06:50 bucket: rate_limited_per_min goes from ~1.7-2.2 req/min baseline (06:40 bucket avg 1.89) to avg 438.67/max 919.45 at 06:50, then plateaus ~890-915 req/min (avg 901.07 at 07:00; summary mean 469.4). The service is rejecting/limiting ~450x its baseline volume at the exact onset minute — an overload-saturation indicator on auth-service itself, not a downstream-induced wait.
  - `F8` ✔ The earliest auth-service error log is at 06:55:42Z; all 372 ERROR logs in 06:00-09:00Z are the single pattern 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' — no bcrypt/crypto, timeout, connection-refused, or OOM signature appears.
**Assumptions**
  - An un-deployed infra event (failover) could have affected a dependency without appearing in the deploy/config feeds
  - The exposed redis/user-db metrics would surface a degradation (no latency metric exists on either, F18/F22), so 'downstream slow but not saturated' can be shown unsupported but not strictly excluded.
**Rejected because:** user-db (F19, F20) and redis-cache (F21) were both measured at onset and show no degradation — cache healthy on all four metrics, DB CPU/slow-queries/lag flat; user-db's only movement (connections_active ~3x, F20) is an effect of the extra traffic, not a cause. Mechanism also contradicts it: auth-service is CPU-pinned at ~97-98% with self-throttling (F16/F17), the signature of its own overload, not a blocked wait on a downstream. F18/F22 mean a slow-not-saturated downstream is unsupported-but-not-strictly-excluded; on the CPU-pinned/self-throttled signature it is an implausible residual.

### H1: Reporter's theory: recurrence of bcrypt/crypto regression in auth-service
A recent auth-service release raised per-login CPU cost (bcrypt cost factor), saturating CPU and causing ~3.8s p99 and 503s, replicating HIST-101.

**Causal chain**
1. An auth-service release raised the bcrypt cost factor *(assumed)*
2. Per-login CPU cost rises, pushing auth-service CPU toward 100% *(assumed)*
3. Login latency grows to ~3.8s [F7]
4. Requests rejected with 503 on /v1/auth/login [F8]
5. Contradicted: no auth-service deployment exists anywhere in the window [F1, F6]
6. Contradicted: the error signature is 'accept queue full', not a crypto/CPU-cost signature; and load stepped 3.8x simultaneously with CPU, inconsistent with a pure per-request cost rise at constant load [F8, F10, F15]

**Supporting evidence**
  - `F7` ✔ auth-service p99 latency was flat at ~85-95 ms through 06:52:00Z, then jumped at the 06:54:00Z bucket to avg 3759.86 ms / max 3849.2 ms and stayed ~3.5-4.0s for the rest of the window.
  - `F9` ✔ auth-service CPU jumped from a ~33-40% baseline (06:00-06:45) to avg 84.31 / max 97.33 in the 06:50 bucket, and stayed ~96-98% through 08:50.
**Contradicting evidence**
  - `F1` ✔ Across ALL services in 2026-04-18T00:00Z–2026-04-22T08:59Z there was exactly ONE deployment: portfolio-service v4.4.0 (dep-5803) deployed 2026-04-22T05:50:00Z by ci-bot, status live, not rolled back. No auth-service deployment of any kind is present.
  - `F6` ✔ The precedent cited in the report (HIST-101, 2025-08-14, 'Login latency after auth-service release', symptoms 'login p99 4s','503 on login','high cpu', root cause 'bcrypt cost factor raised 10->14 in release v6.4.0', resolved by rolling back v6.4.0) is a bad_deployment whose signature requires an auth-service release. There is no auth-service release in the current window, so the same failure mode can be ruled out on change-history grounds.
  - `F8` ✔ The earliest auth-service error log is at 06:55:42Z; all 372 ERROR logs in 06:00-09:00Z are the single pattern 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' — no bcrypt/crypto, timeout, connection-refused, or OOM signature appears.
  - `F10` ✔ auth-service request_rate rose from a ~393-400 req/s baseline to avg 1078.03 / max 1813.74 in the 06:50 bucket, then settled ~1480-1520 req/s — roughly a 3.8x traffic surge at the same time as the CPU and latency jumps.
  - `F15` ✔ The auth-service onset is a simultaneous, single-bucket step at 06:50Z in BOTH load and saturation signals: request_rate jumps from ~390-395 req/s baseline (06:45 bucket avg 390.98) to avg 680.57/max 1012.36 in the 06:50 bucket, then 1475.5/max 1813.74 in the 06:55 bucket, settling ~1460-1540 req/s. auth-service CPU shows the identical 06:50 step (see next finding). Same timestamp, same bucket = coupled onset, and no other bucket in 05:00-08:59 shows a comparable discontinuity.
**Assumptions**
  - A recent auth-service release existed but was not captured in the deploy feed
**Rejected because:** F1 (verified, full window, all services) shows no auth-service deployment of any kind; HIST-101's signature requires an auth-service release (F6). Signature also mismatches: error is 'accept queue full' (F8), and CPU stepped in lockstep with a 3.8x load increase (F10/F15) rather than rising at constant load — the pattern of an external load surge, not a per-request cost regression.

### H3: portfolio-service v4.4.0 deploy caused the incident via shared redis-cache
portfolio-service v4.4.0 (dep-5803, 05:50Z) flooded or disrupted the shared redis-cache, degrading auth-service logins over the shared component.

**Causal chain**
1. portfolio-service v4.4.0 deploys at 05:50Z [F1]
2. portfolio-service load on shared redis-cache changes, harming auth-service *(assumed)*
3. Auth-service login latency rises and 503s appear [F7, F8, F11]
4. Contradicted: no discontinuity in portfolio-service metrics at 05:50Z and a full 60-min quiet gap before the 06:50Z auth onset [F13, F7]
5. Contradicted: the shared redis-cache is flat across the whole window, so no shared-path propagation [F14, F21]

**Supporting evidence**
  - `F1` ✔ Across ALL services in 2026-04-18T00:00Z–2026-04-22T08:59Z there was exactly ONE deployment: portfolio-service v4.4.0 (dep-5803) deployed 2026-04-22T05:50:00Z by ci-bot, status live, not rolled back. No auth-service deployment of any kind is present.
  - `F5` ✔ redis-cache is a SHARED component: it is a cache dependency of auth-service, portfolio-service, pricing-engine and market-data-service. Notably portfolio-service — the one service that did deploy (2026-04-22T05:50Z) — shares redis-cache with auth-service.
**Contradicting evidence**
  - `F13` ✔ Candidate change portfolio-service v4.4.0 (deployed 05:50Z) does NOT step anything at its timestamp: portfolio-service request_rate is flat at ~552-587 req/s across every 10-min bucket from 05:00 to 08:50 (05:40 bucket avg 557.8 -> 05:50 bucket avg 576.91 -> 06:00 avg 572.62), i.e. no discontinuity at 05:50Z and no change at the 06:50-06:55Z auth-service onset either. The deploy is not temporally coincident with the degradation.
  - `F14` ✔ redis-cache — the SHARED component that portfolio-service and auth-service both depend on (per F5) — shows no step change at the 05:50Z deploy nor at the 06:50-06:55Z auth onset: cpu_pct stays 21-22% flat across the entire 05:00-08:50 window (06:40 bucket avg 22.32, 06:50 bucket avg 21.57, 07:00 avg 22.14; summary min 19.51 max 25.59). No downstream cache resource anomaly exists to explain the auth degradation.
  - `F21` ✔ redis-cache is healthy on all four requested metrics with no step at 06:50Z: hit_ratio_pct 95.89-96.34% (06:49 avg 96.34 -> 06:55 avg 95.29; summary min 94.44 max 97.15), connected_clients 303-318 (06:49 avg 311.76 -> 06:52 avg 307.33; summary min 285.87 max 336.28), evictions_per_min 2.0-3.5 (summary min 1.17 max 4.42 mean 2.9), memory_pct ~69-73% (summary min 67.68 max 76.3 mean 70.73). The shared cache shows no eviction storm, memory pressure, client drop or hit-ratio collapse coincident with the 06:50Z degradation.
  - `F7` ✔ auth-service p99 latency was flat at ~85-95 ms through 06:52:00Z, then jumped at the 06:54:00Z bucket to avg 3759.86 ms / max 3849.2 ms and stayed ~3.5-4.0s for the rest of the window.
**Assumptions**
  - The deploy altered portfolio-service cache behavior despite no measurable change in its own metrics
**Rejected because:** F13 shows no step in portfolio-service metrics at 05:50Z and a 60-minute quiet gap before symptom onset (weakened by timing). F14/F21 show the shared redis-cache is flat across the entire window, so no shared-path propagation exists. The only deployment in the window has no temporal or mechanistic link to the 06:50Z onset.

### H5: Misconfigured logging change (cfg-2801) altered runtime behavior
cfg-2801 (log.level INFO→WARN at 03:40Z) unexpectedly increased CPU or reduced throughput on auth-service, contributing to saturation.

**Causal chain**
1. cfg-2801 changes auth-service log level at 03:40Z [F2]
2. Logging change alters CPU/throughput behavior *(assumed)*
3. CPU saturates and logins slow to ~3.8s with 503s [F9, F7, F8]
4. Contradicted on timing: 03:40Z is three hours before the 06:50Z onset [F2, F7]

**Supporting evidence**
  - `F2` ✔ The only config change in the window affecting auth-service is cfg-2801: key 'log.level', INFO→WARN, at 2026-04-22T03:40:00Z by config-bot ('Reduce log volume'). This is a logging-verbosity change, not a functional/crypto parameter change.
**Contradicting evidence**
  - `F2` ✔ The only config change in the window affecting auth-service is cfg-2801: key 'log.level', INFO→WARN, at 2026-04-22T03:40:00Z by config-bot ('Reduce log volume'). This is a logging-verbosity change, not a functional/crypto parameter change.
  - `F7` ✔ auth-service p99 latency was flat at ~85-95 ms through 06:52:00Z, then jumped at the 06:54:00Z bucket to avg 3759.86 ms / max 3849.2 ms and stayed ~3.5-4.0s for the rest of the window.
**Assumptions**
  - A logging-verbosity reduction could raise CPU cost (contrary to expectation)
**Rejected because:** F2 is a logging-verbosity change with no functional/crypto parameters, and it occurred at 03:40Z — three hours before the 06:50Z onset, so it is not coincident. A reduction from INFO to WARN would not increase CPU. No mechanism or timing link.

## Facts (verified observations)
- auth-service request_rate rose from a ~393-400 req/s baseline to ~1460-1540 req/s (~3.8x), with CPU stepping in the same 06:50Z bucket. [F10, F15, F9]
- The surge is present upstream of auth-service: api-gateway request_rate is flat ~1250 req/s through 06:30Z, ramps from the 06:45Z bucket and settles ~4875-4934 req/s (~3.9x), so the load is genuine inbound traffic rather than an auth-internal retry/metering artifact. [F23, F24]
- auth-service CPU stepped from ~33-40% baseline to avg 84.31 / max 97.33 in the 06:50 bucket and pinned at ~96-98% for the rest of the window, while latency stayed flat ~85-95ms until the 06:54 bucket then jumped to ~3.5-4.0s. [F9, F16, F7]
- The rejection signature is CPU-bound overload, not crypto or a downstream wait: all 372 ERROR logs are 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' and self-throttling (rate_limited_per_min) rose from ~1.9 req/min to ~900 req/min (~450x) in the same 06:50 bucket. [F8, F17]
- The degradation is not memory-driven: auth-service memory_pct stayed flat ~51-55% (max 57.07) across the whole window, and auth-service restarts is exactly 0 across 06:40-07:00Z. [F12, F27]
- All alternatives on the change axis are excluded: there was exactly one deployment in the window across all services (portfolio-service v4.4.0 at 05:50Z) and no auth-service deployment of any kind, and the only auth-service config change is a logging-verbosity change (log.level INFO->WARN, cfg-2801) at 03:40Z. [F1, F2]
- The portfolio-service deploy is not temporally coincident: portfolio-service request_rate is flat ~552-587 req/s across every bucket 05:00-08:50 with no discontinuity at 05:50Z or at the 06:50-06:55Z auth onset, and the shared redis-cache cpu_pct is flat 21-22% across the entire window. [F13, F14]
- Login-path dependencies show no degradation at onset: user-db cpu_pct (17.5-18.5%), slow_queries_per_min (0.25-0.35) and replication_lag_ms (~29-31ms) are flat, and redis-cache is healthy on hit_ratio_pct (95.9-96.3%), connected_clients (303-318), evictions_per_min (2.0-3.5) and memory_pct (~69-73%). [F19, F21]
- Neither login-path dependency exposes any latency metric, so a 'downstream slow but not saturated' signature cannot be observed in this telemetry; the resource metrics that do exist show no anomaly. [F22]
- api-gateway has abundant headroom (cpu_pct flat ~36.9-44.9%) and inherits multi-second latency only once auth-service starts 503-ing (gateway p99 flat ~200-220ms through 06:51Z, then ~3.8-4.0s at 06:54Z), with no gateway-side WARN-or-worse logs - the episode is not gateway-side saturation. [F26, F25, F28]
- auth-service's error_rate stayed at baseline (~0.16-0.22) in the 06:50 and 06:52 buckets and only stepped at 06:54Z, i.e. errors lag the 06:50Z CPU/load step by ~2 buckets, consistent with queue build-up before rejections. [F34]
- kyc-provider (an external login-path dependency) exposes no telemetry, so it cannot be checked directly and its state is only indirectly observable through auth-service; kafka, ledger-db and settlement-service show no step change at the 06:50-06:52Z bucket. [F30, F31, F32, F33]

## Assumptions (not verified)
- auth-service CPU is the binding constraint (allocation not scaled up), so a ~3.8x load increase necessarily saturates it; no direct provisioned-limit metric exists, and this is inferred from the pin at ~97-98% (F16).
- The surge is genuine demand rather than a metering artifact, supported by it appearing at gateway ingress (F23/F29); the surge's PROVENANCE (legitimate external clients vs synthetic load test vs a retrying upstream caller) is not determinable from the exposed telemetry, as no caller-identity or per-route metric exists.
- If instead the 06:50Z step were a capacity reduction (autoscaler min/allocatable change or node event), the mechanism would be a capacity change with the same remediation direction; no deploy/config evidence of such a change exists (F1, F2).
- user-db and redis-cache would surface a degradation on the exposed resource metrics if one existed (no latency metric is exposed, F18/F22), so 'downstream slow but not saturated' is shown unsupported by available data but not strictly excluded.
- The per-endpoint blast radius is assumed confined to /v1/auth/login based on all 372 ERROR logs naming POST /v1/auth/login (F8); non-login endpoints were not independently verified.
- Deploy and config feeds were only checked from 2026-04-18 onward, so a change slightly earlier than the window boundary is assumed not to be the trigger.
- (demoted from facts: no verified observation backs it) user-db connections_active rose ~3x (24.8-25.9 to ~75-79) from 06:52Z with no accompanying CPU, slow-query or lag increase - the mirror of auth-service's traffic surge, i.e. an effect of more work arriving, not a cause.

## Timeline
- `2026-04-22T03:40:00Z` Only auth-service config change in the window: cfg-2801 log.level INFO->WARN by config-bot; a logging-verbosity change, three hours before onset. [F2]
- `2026-04-22T05:50:00Z` Only deployment in the window across all services: portfolio-service v4.4.0 (dep-5803) by ci-bot; no discontinuity in portfolio-service metrics at this timestamp. [F1, F13]
- `2026-04-22T06:45:00Z` api-gateway request_rate begins to ramp up from its flat ~1250 req/s baseline. [F23, F24]
- `2026-04-22T06:50:00Z` Coupled onset bucket: auth-service request_rate steps ~3.8x and CPU steps from ~38% to ~97%, with self-throttling spiking ~450x to ~900 req/min. [F9, F10, F15, F16, F17]
- `2026-04-22T06:52:00Z` user-db connections_active rises ~3x, mirroring the traffic surge, with no CPU, slow-query or lag increase; auth-service error_rate still at baseline. [F20, F34]
- `2026-04-22T06:54:00Z` auth-service p99 jumps to ~3.8s and error_rate steps to ~6.7 (plateauing ~14%); api-gateway p99 follows to ~3.9s once upstream 503s begin. [F7, F34, F25]
- `2026-04-22T06:55:42Z` Earliest auth-service ERROR log: 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' - the sole error pattern for the window. [F8]
- `2026-04-22T07:06:00Z` Incident INC-008 reported (SEV-2) for slow/failing login, initially hypothesized as a bcrypt regression recurrence. [F6]
- `2026-04-22T08:50:00Z` auth-service CPU still pinned ~96-98% and memory flat, confirming sustained CPU-only saturation with no recovery. [F16, F12]

## Recommended actions
- **[immediate]** Remediate the immediate overload on auth-service by scaling its provisioned CPU (raise the CPU limit/request allocation or add replicas/autoscale headroom for auth-service) so it can absorb the ~1460-1540 req/s inbound load instead of pinning at ~97-98% and 503-ing. -- Verified: auth-service CPU is pinned at ~97-98% with latency ~3.8s, ~450x self-throttling and 503 'accept queue full' errors (F9/F16/F17/F8) - it is the saturated component, and capacity is the fix. [F9, F16, F17, F8]
- **[immediate]** Do NOT roll back or restart auth-service for remediation, and explicitly reject the bcrypt/crypto-regression theory in the incident channel; there is no auth-service deployment or functional config change to revert. -- Verified: no auth-service deployment exists in the window and the only auth-service config change is a logging-verbosity change three hours earlier (F1/F2/F6); restarts are 0 (F27), so a restart/rollback would not address the cause. [F1, F2, F6, F27]
- **[immediate]** Characterize the ~3.9x external surge at api-gateway ingress to identify its provenance (caller identity / source / top client breakdown) and confirm whether it is legitimate demand or a load test / retrying upstream caller; if it is non-legitimate, shed or rate-limit that traffic at the gateway. -- Verified: the surge is real inbound load measured at gateway ingress (~1266 -> ~4900 req/s, F23/F24/F29), but the origin of the load is not identifiable from existing metrics - it is the single remaining unknown and may be an additional mitigation lever. [F23, F24, F29, F10]
- **[short_term]** Add and enforce gateway/edge rate limiting or load shedding for the login path so a 4x traffic step degrades gracefully instead of driving auth-service into 'accept queue full' 503s. -- Verified: api-gateway has abundant CPU headroom (~37-45%) and emitted no self-protective warnings while passing the surge through to auth-service, which then rejected at queue capacity (F26/F28/F8). [F26, F28, F8]
- **[short_term]** Verify the remediation by confirming auth-service CPU drops below saturation and p99 returns to the ~85-95ms baseline and error_rate returns to ~0.2%, then confirm api-gateway p99 returns to ~200-220ms. -- Verified baselines exist for each signal: auth p99 flat ~85-95ms pre-onset (F7), error_rate ~0.2% (F11/F34), gateway p99 ~200-220ms (F25). [F7, F11, F34, F25]
- **[long_term]** Add caller-identity / source and per-route metrics at api-gateway and an auth-service per-endpoint breakdown, so future influxes can be attributed and blast radius measured directly rather than inferred. -- Verified data gap: no metric exposes the origin of inbound load and no gateway metric isolates volume to auth-service, so surge provenance and per-endpoint blast radius are currently unmeasurable (F29, F30). [F29, F30]
- **[long_term]** Adopt capacity/autoscaling guardrails for auth-service (load-proportional scaling and an early-warning alert on the CPU-pin / rate_limited_per_min / accept-queue-full signals) so saturation is detected and absorbed before 503s occur. -- Verified: the onset was a coupled single-bucket step in load and CPU that immediately pinned the service and spiked self-throttling ~450x (F9/F10/F15/F17), with errors following ~2 buckets later (F34) - a signature that preventive capacity alerting can catch earlier. [F9, F10, F15, F17, F34]

## Data gaps
- The provenance of the ~3.8-3.9x request surge (legitimate external clients vs synthetic load test vs a retrying upstream caller) is not determinable: no metric exposes caller identity/source and no gateway metric isolates inbound volume to auth-service. This is the principal residual uncertainty and limits confidence in the driver of the incident, though not in its origin (auth-service capacity).
- auth-service exposes no thread_pool_active / connection-pool metric, so idle-vs-blocked thread state could not be measured; the CPU-bound vs downstream-wait distinction rests on CPU saturation plus self-throttling plus flat memory rather than direct thread-state evidence.
- Per-endpoint blast radius is unanswered: auth-service metrics are service-level only with no endpoint dimension, and the per-path log grouping was not retrieved. Only the aggregate hint that all 372 ERROR lines are POST /v1/auth/login constrains this.
- kyc-provider (an external login-path dependency) exposes no telemetry at all (not_found; only observable through auth-service), so it cannot be checked for a 06:50Z step by any metric.
- The ability to directly measure downstream response time is absent: redis-cache and user-db expose no latency metric, so 'downstream slow but not saturated' cannot be strictly excluded, only shown unsupported by the resource metrics that do exist.
- Deploy/config feeds were only inspected from 2026-04-18 onward, so a change slightly earlier than the window boundary, or an un-deployed infra event (e.g. a redis failover) without deploy/config records, could not be ruled out from those feeds (though redis-cache metrics are flat).
- Some tool queries failed or were never executed due to budget (latency_p99_ms on redis-cache/user-db, thread_pool_active on auth-service, kyc-provider metrics); user-service/session-service do not exist in known_services, so components named in the original report could not be checked under those names.

## What would change this conclusion
- Evidence that the ~3.8-3.9x surge is an internal artifact rather than external load (e.g. a gateway per-route metric showing auth-service inbound volume did not rise, while auth-service internal retry/metering counters did) would break the load-driven chain and reopen the diagnosis.
- Direct measurement showing a login-path dependency degraded at onset - e.g. redis-cache or user-db latency/pool metrics spiking at 06:50Z - would revive the downstream-victim hypothesis (H2); current flat resource metrics argue against it but no latency metric exists on either component.
- The discovery of an auth-service release or a change to a functional/crypto parameter in the window (e.g. a deploy not captured in the feed) would revive the bcrypt/crypto-regression theory (H1).
- Evidence that provisioned CPU capacity on auth-service was reduced or a node/autoscaler event changed allocatable capacity at ~06:50Z would shift the mechanism from an external load surge to a capacity change (same fix direction, different trigger).
- Per-endpoint data showing a comparable step in non-login auth-service endpoints would broaden the blast radius beyond /v1/auth/login and could indicate a different, service-wide mechanism.
- Any downstream latency signal (were one added) showing login-path dependencies slow but not saturated at onset would weaken the CPU-bound interpretation and make a downstream-wait contribution plausible.

## Integrity warnings (set by code)
- fact demoted to assumption: user-db connections_active rose ~3x (24.8-25.9 to ~75-79) from 06:52Z with no ac

## Investigation stats
- latency 226.38s, LLM calls 29, tokens 236552 (in 196554 / out 39998)
- tool calls 48 (6 failed, 0 partial, 0 recovered by retry), findings 34/34 verified
