# Incident investigation report: INC-008
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The login slowness/failures on auth-service are a load-driven CPU saturation: an exogenous ~4x surge in login request rate entered above auth-service at ~06:51Z, drove CPU from ~38% to ~97-99%, filled the accept queue (503 "accept queue full" on POST /v1/auth/login) and stepped p99 from ~90 ms to ~3.8 s. The surge is edge-originated: api-gateway request_rate stepped +3,500-4,000 req/s in the same 06:51 bucket while portfolio-service (carrier of the only deployment dep-5803) and every other peer stayed statistically flat, so no internal peer or release is the origin. The reporter's bcrypt-regression suspicion (H1) is ruled out — no auth-service deployment or config exists in the window or prior week, memory is flat, and request_rate rose rather than stayed flat; the shared-dependency-first hypothesis (H3) is also ruled out by flat user-db and redis-cache metrics with no pre-onset movement. Confidence in H2 as the cause is medium (0.55): the within-auth-service mechanism and the above-auth-service origin are both verified, but the split between external end-user/bot traffic and an unmeasured internal caller hitting the login endpoint is inferred, not measured. The incident is ongoing at last data (08:59Z), so mitigation should proceed while provenance decomposition is completed.

## Probable root cause
**Exogenous (edge-originated) login request-rate surge saturates auth-service** -- capacity_saturation in `auth-service`, triggered by `null` (confidence 0.55, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: Exogenous (edge-originated) login request-rate surge saturates auth-service | capacity_saturation | auth-service | null | 0.55 (medium) | active |
| 2 | H4: portfolio-service v4.4.0 CSV-export deployment drives the surge | bad_deployment | portfolio-service | dep-5803 | 0.15 (low) | active |
| 3 | H3: Shared dependency (user-db/redis-cache) degraded first; auth-service is a downstream victim | dependency_failure | redis-cache | null | 0.05 (low) | rejected |
| 4 | H1: Reporter's suspicion: bcrypt cost regression re-introduced (auth-service code) | bad_deployment | auth-service | null | 0.03 (low) | rejected |

### H2: Exogenous (edge-originated) login request-rate surge saturates auth-service
Something upstream of auth-service multiplied inbound login request rate ~4x beginning 06:51Z. auth-service absorbed the load until instances hit ~97-99% CPU, the accept queue filled, and it began rejecting POST /v1/auth/login with 503 while p99 stepped to ~3.8s. No auth-service fault, dependency fault, or release is involved — the origin is the surge itself, which entered at the edge (api-gateway).

**Causal chain**
1. External/edge client traffic surge begins 06:51Z: api-gateway request_rate steps from ~1268 req/s flat (06:30-06:48) to ~4700-5250 req/s, a +3,500-4,000 req/s jump in the same 06:51 bucket as auth-service's onset [F24, F28]
2. auth-service login request rate climbs monotonically from 06:51Z: 375 -> 529 -> 715 -> 1012 -> 1814 req/s, roughly 4x baseline [F13, F11]
3. auth-service CPU follows the rate surge (not preceding it): ~38% through 06:42, 56.79 avg (max 96.96) at 06:48, pinned ~97-99% thereafter [F14, F10]
4. CPU saturation stalls request acceptance; accept queue fills, producing server-side accept-queue-exhaustion 503s on /v1/auth/login and a p99 step to ~3.8s (error rate 0.2% -> ~14%) [F9, F7, F8, F10]
5. user-db connection surge to ~77-80 is auth-service's own connection pool filling under tripled load — concurrent with, not preceding, the auth onset; DB CPU/replication lag/slow queries stay flat [F16, F18, F19, F20]
6. No peer service (portfolio-service, pricing-engine, market-data-service, payment-service, settlement-service) contributed rate increase — only the gateway moved, consistent with an edge-originated end-user/bot surge rather than internal fan-out [F25, F27, F28]

**Supporting evidence**
  - `F24` ✔ api-gateway request_rate was flat ~1,239-1,277 req/s for every 3-min bucket from 06:30 to 06:48 (06:48 avg 1268.22), then STEPPED at the 06:51 bucket to 2102.78 avg (max 2490.76), 3530.13 at 06:54, 4486.2 at 06:57, and plateaued ~4,700-5,200 req/s (07:03 avg 4902.66; 07:30 avg 5247.87). The step begins in the same 06:51 bucket as auth-service's request-rate onset.
  - `F28` ✔ The gateway step magnitude is consistent with the auth-service step being an edge-originated login surge, not a peer fan-out: api-gateway gained ~+3,500-4,000 req/s above baseline (1268 -> plateau ~4,700-5,250) at the same 06:51 bucket, while auth-service's established step was ~+1,100 req/s net (375 -> ~1,490). No peer service contributes any rate increase to gateway or auth traffic in this window.
  - `F13` ✔ At the onset minute (06:51Z), auth-service request_rate jumped FIRST: 06:50 avg 375.35 req/s, 06:51 avg 529.5, then 715.14 (06:52), 770.48 (06:53), 1012.36 (06:54), and 1813.74 by 06:59 — a monotonic climb starting exactly at 06:51, whereas p99/CPU step-change appears in the 06:48 (cpu) and 06:54 (p99) buckets.
  - `F11` ✔ auth-service request_rate roughly tripled/quadrupled at the same onset: ~388–399 req/s baseline through 06:30, then 849.02 avg (max 1813.74) at 06:45 and ~1490–1510 req/s from 07:00 onward, sustained through 08:45.
  - `F14` ✔ auth-service CPU (6-min buckets) was ~38% through 06:42 (38.49 avg, max 40.45), then rose to 56.79 avg (max 96.96) at 06:48 and 96.74 avg (max 97.33) at 06:54, pinned ~97–99% thereafter — CPU saturation starts within the same bucket as the latency step and follows the request-rate surge, not before it.
  - `F10` ✔ auth-service CPU stepped from a ~38% baseline to ~97%: 38.14% avg (max 40.74) at 06:30, then 69.41% avg (max 97.33) at 06:45 and 97.38% avg at 07:00, pinned high through 08:45 — i.e., the service is CPU-saturated, consistent with the accept-queue-full rejections.
  - `F9` ✔ The 503 error signature is NOT a downstream timeout or upstream refusal — it is server-side accept-queue exhaustion. All 372 ERROR log entries in 05:00–08:59 are a single pattern: 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)', first seen 06:55:42Z, last 08:58:57Z (result truncated at 30 of 372).
  - `F7` ✔ auth-service p99 latency was a flat ~90 ms baseline from 05:00 through 06:51, then stepped to ~3.7–4.0 s. The 3-minute-resolution query shows 92.09 ms at 06:51 and 3765.92 ms at 06:54 — a step-change with no gradual ramp. The 10-min query corroborates: last normal bucket 06:40 (88.3 ms), first degraded bucket 06:50 (2288.53 ms avg, 4012.81 max).
  - `F8` ✔ auth-service error_rate (HTTP 5xx) was ~0.2% baseline until 06:40, then stepped to ~14%: 0.21% at 06:40, 7.11% (max 14.76%) at 06:50, and 14.12% at 07:00, staying ~13.8–14.1% through 08:50.
  - `F16` ✔ user-db CPU stayed flat at ~18% (min 16.25, max 19.99, mean 18.03) through the entire window, but user-db connections_active STEPPED from ~25 to ~76-80 in the 06:52 bucket: 25.02 avg at 06:48, then 76.2 avg (max 79.84) at 06:52, holding ~77-80 through 07:20. The DB connection surge is concurrent with (not before) the auth-side onset and the DB itself shows no CPU stress.
  - `F18` ✔ Interpretation: neither of auth-service's declared dependencies moved before auth-service did. user-db and redis-cache both show flat/low CPU (18%, 22%) with no pre-onset movement; user-db's connection surge is simultaneous with the auth onset and is consistent with auth-service's own connection pool being filled by the tripled request load, not a DB-side cause. Combined with the flat memory, the pattern is a self-inflicted, load-driven CPU saturation on auth-service (accept-queue full), not a bcrypt-cost regression (which would raise per-request CPU with FLAT request_rate) and not a shared-cache/DB degradation.
  - `F19` ✔ user-db replication_lag_ms was flat ~30 ms across 06:30–07:00Z with no movement before the 06:51Z onset: min 27.65, max 31.55, mean 29.87 ms; e.g. 28.96 at 06:40, 30.66 at 06:50, 30.0 at 06:52, 30.94 at 06:54. No replication-lag step precedes or accompanies the auth-service degradation.
  - `F20` ✔ user-db slow_queries_per_min was flat ~0.3/min across 06:30–07:00Z with no movement before 06:51Z: min 0.18, max 0.42, mean 0.31; values 0.27 at 06:40, 0.29 at 06:50, 0.30 at 06:52, 0.41 at 06:54. No slow-query surge precedes the onset.
  - `F12` ✔ auth-service did not restart during the window (restarts metric flat at 0.0 for all 240 points, 05:00–08:45), so the degradation is not explained by a crash/restart of auth-service instances.
  - `F15` ✔ auth-service memory_pct stayed flat ~47–57% across the whole onset window (min 46.97, max 57.07, mean 51.99) with no step at 06:51–06:54 — no evidence of a memory/allocation-driven (e.g. bcrypt cost) defect.
  - `F25` ✔ portfolio-service request_rate shows NO step or surge at any point 06:30-07:30: 563.07 (06:30), 569.72 (06:51), 583.24 (06:54), 580.73 (07:00), 540.84 (07:06), 585.86 (07:30); summary min 507.71, max 618.2, mean 567.19 across 61 points. Its rate is unchanged before, at, and after 06:51.
  - `F27` ✔ The three other peers are equally flat across 06:30-07:30 with no step at/before 06:51: pricing-engine 652.84 (06:51), 640.91 (07:00), min 587.47/max 691.02/mean 643.17; market-data-service 950.35 (06:51), 924.99 (07:00), min 864.56/max 1024.93/mean 946.62; payment-service 100.44 (06:51), 103.07 (07:00), min 92.38/max 108.61/mean 99.61; settlement-service 64.44 (06:51), 64.62 (07:00), min 57.48/max 68.83/mean 63.82. None of the six requested peers besides api-gateway moved.
**Contradicting evidence**
  - `F6` ✔ Historical precedent HIST-101 (2025-08-14, 'Login latency after auth-service release'): symptoms 'login p99 4s' and '503 on login' with 'high cpu', root cause 'bcrypt cost factor raised 10->14 in release v6.4.0', category bad_deployment, resolution 'Rolled back v6.4.0'. The current symptom set (auth p99 3.8s, 503 on /v1/auth/login) is nearly identical, but that past cause was a deployment and no auth-service deployment exists in the current window. Other auth precedents are HIST-109 (JWT clock-skew default -> 401s) and HIST-108 (JWKS key rotation -> 401s), both 401/invalid_token signatures, not latency/503.
**Assumptions**
  - The measured api-gateway step is the same traffic that reaches auth-service's login path; per-endpoint/per-source decomposition at the gateway was never pulled, so the share attributable to external end-users/bots vs an internal caller is inferred from magnitude only
  - auth-service has no autoscaling (untracked in this data source) — consistent with flat restarts F12 but not directly verified; capacity adequate for ~390 req/s but not ~1500 req/s

### H4: portfolio-service v4.4.0 CSV-export deployment drives the surge
The only deployment in the window, portfolio-service dep-5803 v4.4.0 'Holdings export' at 05:50Z, introduced a defect (runaway export loop, cache-key churn, or repeated token/login calls) that generated a large request surge against auth-service's login path, saturating it. Origin is the portfolio-service release; auth-service is downstream victim.

**Causal chain**
1. portfolio-service v4.4.0 (dep-5803, 05:50Z) ships the new CSV export endpoint — the only deployment in the window [F1, F2]
2. The export code path generates excessive load — directly or via shared redis-cache / token refresh — striking auth-service [F5]
3. auth-service request rate climbs ~4x from 06:51Z, CPU saturates, accept queue fills, 503s and p99 ~3.8s on login [F11, F13, F10, F9, F7]
4. portfolio-service's own request_rate and latency_p99/error_rate remain completely flat across the window including onset [F25, F26]

**Supporting evidence**
  - `F1` ✔ Across ALL services, the only deployment in the window 2026-04-21T00:00Z to 2026-04-22T08:59Z was dep-5803 on portfolio-service (v4.4.0, 'Holdings export', CSV export endpoint) at 2026-04-22T05:50:00Z, status 'live', rolled_back_at null. No deployment of auth-service or any auth-related service occurred in this window.
  - `F5` ✔ redis-cache is a SHARED component ("shared":true) used as a cache by four services: auth-service, portfolio-service, pricing-engine and market-data-service. portfolio-service -- the only service that deployed in the window -- is one of redis-cache's callers.
**Contradicting evidence**
  - `F25` ✔ portfolio-service request_rate shows NO step or surge at any point 06:30-07:30: 563.07 (06:30), 569.72 (06:51), 583.24 (06:54), 580.73 (07:00), 540.84 (07:06), 585.86 (07:30); summary min 507.71, max 618.2, mean 567.19 across 61 points. Its rate is unchanged before, at, and after 06:51.
  - `F26` ✔ portfolio-service latency_p99_ms was flat ~164-195 ms across the whole window with no onset step: 180.76 avg (max 183.83) at 06:51, 182.06 at 06:54, 179.45 at 07:00; summary min 164.72, max 195.22, mean 179.4. Same for error_rate: flat ~0.13-0.32% (0.28 at 06:51, 0.19 at 06:54, 0.15 at 07:30; summary max 0.32). No latency or error degradation on the service that carried dep-5803.
  - `F17` ✔ redis-cache (shared by auth-service, portfolio-service, pricing-engine, market-data-service) shows no anomaly at onset: cpu_pct flat ~22% (min 20.15, max 25.59), hit_ratio_pct flat ~96% (min 94.0, max 97.64), evictions_per_min flat ~2-3 (max 4.46). None of its resource metrics move at 06:51–06:54.
  - `F24` ✔ api-gateway request_rate was flat ~1,239-1,277 req/s for every 3-min bucket from 06:30 to 06:48 (06:48 avg 1268.22), then STEPPED at the 06:51 bucket to 2102.78 avg (max 2490.76), 3530.13 at 06:54, 4486.2 at 06:57, and plateaued ~4,700-5,200 req/s (07:03 avg 4902.66; 07:30 avg 5247.87). The step begins in the same 06:51 bucket as auth-service's request-rate onset.
  - `F28` ✔ The gateway step magnitude is consistent with the auth-service step being an edge-originated login surge, not a peer fan-out: api-gateway gained ~+3,500-4,000 req/s above baseline (1268 -> plateau ~4,700-5,250) at the same 06:51 bucket, while auth-service's established step was ~+1,100 req/s net (375 -> ~1,490). No peer service contributes any rate increase to gateway or auth traffic in this window.
  - `F7` ✔ auth-service p99 latency was a flat ~90 ms baseline from 05:00 through 06:51, then stepped to ~3.7–4.0 s. The 3-minute-resolution query shows 92.09 ms at 06:51 and 3765.92 ms at 06:54 — a step-change with no gradual ramp. The 10-min query corroborates: last normal bucket 06:40 (88.3 ms), first degraded bucket 06:50 (2288.53 ms avg, 4012.81 max).
  - `F12` ✔ auth-service did not restart during the window (restarts metric flat at 0.0 for all 240 points, 05:00–08:45), so the degradation is not explained by a crash/restart of auth-service instances.
**Assumptions**
  - portfolio-service can generate auth-service login traffic at all (no measured edge from portfolio-service to auth-service opens the login endpoint)
  - The ~61-minute gap between the 05:50Z deploy and the 06:51Z onset is explained by lagging/accumulating behavior rather than indicating the deploy is unrelated
**Rejected because:** null

### H3: Shared dependency (user-db/redis-cache) degraded first; auth-service is a downstream victim
A shared dependency of auth-service — most plausibly redis-cache (shared by four services) or user-db — degraded first, stalling auth-service request handling so concurrency and inbound rate climbed, filling the accept queue and producing latency/503s.

**Causal chain**
1. shared redis-cache or user-db degrades first *(assumed)*
2. auth-service request threads block on the dependency *(assumed)*
3. inbound concurrency grows, accept queue fills, 503s + p99 step [F9, F7]

**Supporting evidence**
  - `F4` ✔ auth-service dependency edges: auth-service -> user-db (kind 'db'), auth-service -> redis-cache (kind 'cache'), auth-service -> kyc-provider (kind 'external', has_telemetry false); api-gateway -> auth-service (http). user-db and redis-cache are owned by 'platform'.
  - `F5` ✔ redis-cache is a SHARED component ("shared":true) used as a cache by four services: auth-service, portfolio-service, pricing-engine and market-data-service. portfolio-service -- the only service that deployed in the window -- is one of redis-cache's callers.
**Contradicting evidence**
  - `F17` ✔ redis-cache (shared by auth-service, portfolio-service, pricing-engine, market-data-service) shows no anomaly at onset: cpu_pct flat ~22% (min 20.15, max 25.59), hit_ratio_pct flat ~96% (min 94.0, max 97.64), evictions_per_min flat ~2-3 (max 4.46). None of its resource metrics move at 06:51–06:54.
  - `F16` ✔ user-db CPU stayed flat at ~18% (min 16.25, max 19.99, mean 18.03) through the entire window, but user-db connections_active STEPPED from ~25 to ~76-80 in the 06:52 bucket: 25.02 avg at 06:48, then 76.2 avg (max 79.84) at 06:52, holding ~77-80 through 07:20. The DB connection surge is concurrent with (not before) the auth-side onset and the DB itself shows no CPU stress.
  - `F18` ✔ Interpretation: neither of auth-service's declared dependencies moved before auth-service did. user-db and redis-cache both show flat/low CPU (18%, 22%) with no pre-onset movement; user-db's connection surge is simultaneous with the auth onset and is consistent with auth-service's own connection pool being filled by the tripled request load, not a DB-side cause. Combined with the flat memory, the pattern is a self-inflicted, load-driven CPU saturation on auth-service (accept-queue full), not a bcrypt-cost regression (which would raise per-request CPU with FLAT request_rate) and not a shared-cache/DB degradation.
  - `F9` ✔ The 503 error signature is NOT a downstream timeout or upstream refusal — it is server-side accept-queue exhaustion. All 372 ERROR log entries in 05:00–08:59 are a single pattern: 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)', first seen 06:55:42Z, last 08:58:57Z (result truncated at 30 of 372).
  - `F13` ✔ At the onset minute (06:51Z), auth-service request_rate jumped FIRST: 06:50 avg 375.35 req/s, 06:51 avg 529.5, then 715.14 (06:52), 770.48 (06:53), 1012.36 (06:54), and 1813.74 by 06:59 — a monotonic climb starting exactly at 06:51, whereas p99/CPU step-change appears in the 06:48 (cpu) and 06:54 (p99) buckets.
  - `F19` ✔ user-db replication_lag_ms was flat ~30 ms across 06:30–07:00Z with no movement before the 06:51Z onset: min 27.65, max 31.55, mean 29.87 ms; e.g. 28.96 at 06:40, 30.66 at 06:50, 30.0 at 06:52, 30.94 at 06:54. No replication-lag step precedes or accompanies the auth-service degradation.
  - `F20` ✔ user-db slow_queries_per_min was flat ~0.3/min across 06:30–07:00Z with no movement before 06:51Z: min 0.18, max 0.42, mean 0.31; values 0.27 at 06:40, 0.29 at 06:50, 0.30 at 06:52, 0.41 at 06:54. No slow-query surge precedes the onset.
  - `F21` ✔ redis-cache connected_clients was flat ~285–336 clients (mean 309.28) across 06:30–07:00Z: 313.65 at 06:40, 309.09 at 06:50, 312.63 at 06:52, 308.58 at 06:54. No client-count step before or at the 06:51Z onset — no evidence of a client-connection storm against the shared cache.
  - `F22` ✔ redis-cache memory_pct was flat ~65–76% (mean 70.1) across 06:30–07:00Z: 69.81 at 06:40, 69.85 at 06:50, 69.84 at 06:52, 72.31 at 06:54. No memory pressure step before or at the onset, ruling out memory-driven eviction/eviction-pressure on the shared cache at onset.
  - `F23` ✔ None of the four remaining unmeasured shared-dependency metrics (user-db replication_lag_ms, user-db slow_queries_per_min, redis-cache connected_clients, redis-cache memory_pct) shows any movement before the 06:51Z onset; all four are within tight flat bands through the pre-onset window 06:30–06:50Z. H3 shared-dependency-first is not supported by these metrics.
**Assumptions**
  - Subtle dependency latency could exist without showing in the queried metrics
**Rejected because:** F17/F21/F22: redis-cache zero anomaly at onset (cpu ~22%, hit ratio ~96%, evictions, connected_clients, memory all flat). F19/F20: user-db replication_lag_ms (~30ms) and slow_queries_per_min (~0.3/min) flat with no pre-onset movement. F16/F18: user-db connection surge is CONCURRENT with, not preceding, the auth onset, and DB CPU flat ~18%. F13: request_rate rises first. F9: accept-queue-full is a server-side accept-backlog signature, not a downstream-refusal signature.

### H1: Reporter's suspicion: bcrypt cost regression re-introduced (auth-service code)
A bcrypt cost-factor regression on the auth-service login path burns CPU per login request, causing p99 latency ~3.8s and 503s (accept-queue full). Matches HIST-101 precedent almost exactly.

**Causal chain**
1. auth-service release raises bcrypt cost factor *(assumed)*
2. each login burns far more CPU, saturating instances *(assumed)*
3. accept queue fills, 503s and p99 ~4s on /v1/auth/login [F9, F7]

**Supporting evidence**
  - `F6` ✔ Historical precedent HIST-101 (2025-08-14, 'Login latency after auth-service release'): symptoms 'login p99 4s' and '503 on login' with 'high cpu', root cause 'bcrypt cost factor raised 10->14 in release v6.4.0', category bad_deployment, resolution 'Rolled back v6.4.0'. The current symptom set (auth p99 3.8s, 503 on /v1/auth/login) is nearly identical, but that past cause was a deployment and no auth-service deployment exists in the current window. Other auth precedents are HIST-109 (JWT clock-skew default -> 401s) and HIST-108 (JWKS key rotation -> 401s), both 401/invalid_token signatures, not latency/503.
  - `F10` ✔ auth-service CPU stepped from a ~38% baseline to ~97%: 38.14% avg (max 40.74) at 06:30, then 69.41% avg (max 97.33) at 06:45 and 97.38% avg at 07:00, pinned high through 08:45 — i.e., the service is CPU-saturated, consistent with the accept-queue-full rejections.
  - `F9` ✔ The 503 error signature is NOT a downstream timeout or upstream refusal — it is server-side accept-queue exhaustion. All 372 ERROR log entries in 05:00–08:59 are a single pattern: 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)', first seen 06:55:42Z, last 08:58:57Z (result truncated at 30 of 372).
**Contradicting evidence**
  - `F1` ✔ Across ALL services, the only deployment in the window 2026-04-21T00:00Z to 2026-04-22T08:59Z was dep-5803 on portfolio-service (v4.4.0, 'Holdings export', CSV export endpoint) at 2026-04-22T05:50:00Z, status 'live', rolled_back_at null. No deployment of auth-service or any auth-related service occurred in this window.
  - `F2` ✔ A wider look-back (2026-04-14T00:00Z to 2026-04-21T00:00Z) returned zero deployments for all services, so no release precedes the onset in the week before the incident either.
  - `F3` ✔ The only config change in the window was cfg-2801 on auth-service: key 'log.level' changed INFO -> WARN at 2026-04-22T03:40:00Z by config-bot, described as 'Reduce log volume'. It affects logging verbosity only, not auth logic or bcrypt parameters.
  - `F11` ✔ auth-service request_rate roughly tripled/quadrupled at the same onset: ~388–399 req/s baseline through 06:30, then 849.02 avg (max 1813.74) at 06:45 and ~1490–1510 req/s from 07:00 onward, sustained through 08:45.
  - `F13` ✔ At the onset minute (06:51Z), auth-service request_rate jumped FIRST: 06:50 avg 375.35 req/s, 06:51 avg 529.5, then 715.14 (06:52), 770.48 (06:53), 1012.36 (06:54), and 1813.74 by 06:59 — a monotonic climb starting exactly at 06:51, whereas p99/CPU step-change appears in the 06:48 (cpu) and 06:54 (p99) buckets.
  - `F15` ✔ auth-service memory_pct stayed flat ~47–57% across the whole onset window (min 46.97, max 57.07, mean 51.99) with no step at 06:51–06:54 — no evidence of a memory/allocation-driven (e.g. bcrypt cost) defect.
  - `F12` ✔ auth-service did not restart during the window (restarts metric flat at 0.0 for all 240 points, 05:00–08:45), so the degradation is not explained by a crash/restart of auth-service instances.
**Assumptions**
  - A change to bcrypt cost could occur without appearing as a deployment or config change (untracked runtime toggle)
**Rejected because:** F1/F2: no auth-service (or any auth-related) deployment exists in the window or the entire prior week — a code bcrypt regression requires a release. F3: only config change is log.level INFO->WARN, cannot alter bcrypt cost. F11/F13: request_rate rose ~4x at onset, whereas bcrypt-cost regression raises per-request CPU with FLAT request_rate. F15: memory flat with no step.

## Facts (verified observations)
- auth-service p99 latency was flat ~90 ms through 06:51Z then stepped to ~3.7-4.0 s, with no gradual ramp (92.09 ms at 06:51, 3765.92 ms at 06:54). [F7]
- auth-service HTTP 5xx error_rate was ~0.2% until 06:40, then stepped to ~14% and held ~13.8-14.1% through 08:50. [F8]
- All 372 ERROR log entries in 05:00-08:59 are one pattern: 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)', first at 06:55:42Z — a server-side accept-backlog signature, not a downstream timeout or upstream refusal. [F9]
- auth-service CPU stepped from ~38% baseline to ~97% avg (max 97.33) in the 06:45-06:54 buckets and stayed pinned high through 08:45, consistent with CPU saturation and the accept-queue-full rejections. [F10, F14]
- auth-service request_rate roughly tripled/quadrupled at onset: ~388-399 req/s baseline through 06:30, ~849 avg (max 1813.74) at 06:45, ~1490-1510 req/s sustained from 07:00 onward. [F11]
- At minute resolution the request_rate moved FIRST: 375.35 avg at 06:50, 529.5 at 06:51, climbing monotonically to 1813.74 by 06:59, while CPU stepped in the 06:48 bucket and p99 in the 06:54 bucket — i.e. load precedes saturation, not the reverse. [F13, F14]
- auth-service did not restart during the window (restarts flat at 0.0 across all 240 points, 05:00-08:45). [F12]
- auth-service memory_pct stayed flat ~47-57% (min 46.97, max 57.07) with no step at onset, giving no evidence of a memory/allocation-driven (e.g. bcrypt cost) defect. [F15]
- Across all services the only deployment in 2026-04-21T00:00Z-2026-04-22T08:59Z was dep-5803 on portfolio-service (v4.4.0, 'Holdings export', CSV export) at 05:50Z, not rolled back; no auth-service or auth-related deployment occurred. [F1]
- A look-back over 2026-04-14T00:00Z-2026-04-21T00:00Z returned zero deployments for all services, so no release precedes the onset in the prior week either. [F2]
- The only config change in the window was cfg-2801 on auth-service (log.level INFO -> WARN at 03:40Z), which affects logging verbosity only, not auth logic or bcrypt parameters. [F3]
- api-gateway request_rate was flat ~1,239-1,277 req/s through the 06:48 bucket, then stepped at the 06:51 bucket to 2102.78 avg (max 2490.76) and plateaued ~4,700-5,250 req/s — the same bucket as auth-service's request-rate onset. [F24]
- portfolio-service request_rate (the dep-5803 carrier) shows no step or surge anywhere in 06:30-07:30 (min 507.71, max 618.2, mean 567.19 across 61 points; 569.72 at 06:51, 583.24 at 06:54), so it did not generate the login surge. [F25]
- portfolio-service latency_p99_ms (164-195 ms) and error_rate (~0.13-0.32%) were flat across the whole window with no onset step, so the dep-5803 carrier shows no degradation. [F26]
- All other peer services were flat across 06:30-07:30 with no step at or before 06:51 (pricing-engine ~643, market-data-service ~947, payment-service ~100, settlement-service ~64 req/s means); none of the six peers besides api-gateway moved. [F27]
- redis-cache (shared by auth-service, portfolio-service, pricing-engine, market-data-service) shows no anomaly at onset: cpu_pct flat ~22%, hit_ratio_pct flat ~96%, evictions_per_min flat ~2-3, connected_clients flat ~285-336, memory_pct flat ~65-76% across 06:30-07:00. [F17, F21, F22]
- user-db CPU stayed flat at ~18% with replication_lag_ms flat ~30 ms and slow_queries_per_min flat ~0.3/min through 06:30-07:00; its connections_active step from ~25 to ~76-80 occurs in the 06:52 bucket, concurrent with (not preceding) the auth-side onset. [F16, F19, F20]
- Neither of auth-service's declared dependencies moved before auth-service did; the pattern is a load-driven CPU saturation on auth-service (accept-queue full), not a bcrypt-cost regression (which would raise per-request CPU with flat request_rate) and not a shared-cache/DB degradation. [F16, F17, F19, F20, F21, F22]
- auth-service dependency edges are auth-service -> user-db (db), auth-service -> redis-cache (cache), auth-service -> kyc-provider (external, has_telemetry false), and api-gateway -> auth-service (http); redis-cache is a shared component used by four services. [F4, F5]

## Assumptions (not verified)
- The measured api-gateway step is the same traffic that reaches auth-service's login path; per-endpoint/per-source decomposition at the gateway was never pulled, so the share attributable to external end-users/bots versus an unmeasured internal caller is inferred from magnitude only.
- auth-service has no autoscaling (untracked in this data source) — consistent with the flat restart metric F12 but not directly verified; capacity is assumed adequate for ~390 req/s but not ~1500 req/s.
- kyc-provider is external with no telemetry, and no other shared-path metric (network/route/config on the shared dependency path) was examined; a non-resource failure mode there is assumed absent rather than proven absent.
- The absence of auth-service CPU-per-endpoint metrics means the rule-out of a bcrypt cost component within the CPU burn relies on inference from the 4x request-rate rise, flat memory, and absence of any deployment — not on a direct endpoint-level measurement.
- (demoted from facts: no verified observation backs it) Historical precedent HIST-101 (2025-08-14) had near-identical symptoms (login p99 4s, 503 on login, high CPU) caused by a bcrypt cost factor raised 10->14 in release v6.4.0 and fixed by rollback — but that cause requires a deployment, and none exists now.
- (demoted from facts: no verified observation backs it) The gateway step magnitude (+3,500-4,000 req/s above baseline) versus auth-service's ~+1,100 req/s net step is consistent with an edge-originated login surge rather than peer fan-out; no peer contributes any rate increase to gateway or auth traffic.

## Timeline
- `2026-04-22T03:40:00Z` Config change cfg-2801 on auth-service: log.level INFO -> WARN by config-bot ('Reduce log volume'). Logging verbosity only. [F3]
- `2026-04-22T05:50:00Z` Only deployment of the window: dep-5803 on portfolio-service (v4.4.0 'Holdings export', CSV export endpoint), status live. [F1]
- `2026-04-22T06:30:00Z` Pre-onset baseline: auth-service ~38% CPU, ~388-399 req/s, p99 ~90 ms; api-gateway ~1,239-1,277 req/s; portfolio-service and all peers flat. [F10, F11, F7, F24, F25, F27]
- `2026-04-22T06:48:00Z` auth-service CPU rises in the 06:48 bucket (56.79 avg, max 96.96) while request_rate has not yet stepped; api-gateway still flat at 1,268.22 req/s. [F14, F24]
- `2026-04-22T06:51:00Z` Onset: auth-service request_rate begins monotonic climb (375.35 at 06:50 -> 529.5 at 06:51 -> 715.14 at 06:52); api-gateway request_rate steps in the same bucket to 2102.78 avg (max 2490.76), eventually plateauing ~4,700-5,250 req/s. [F13, F24, F28]
- `2026-04-22T06:52:00Z` user-db connections_active steps ~25 -> ~76-80, concurrent with (not preceding) the auth onset; user-db CPU, replication lag and slow queries remain flat. redis-cache metrics remain flat. [F16, F19, F20, F17, F21, F22]
- `2026-04-22T06:54:00Z` auth-service p99 steps to 3765.92 ms (from 92.09 ms at 06:51); CPU pinned ~97% avg; request_rate reaches 1012.36 req/s. [F7, F14, F13]
- `2026-04-22T06:55:42Z` First ERROR log: 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)'. [F9]
- `2026-04-22T07:00:00Z` auth-service error_rate ~14.12%, request_rate ~1490-1510 req/s sustained; portfolio-service and all other peers still flat. [F8, F11, F25, F26, F27]
- `2026-04-22T08:58:57Z` Last logged 503 (result truncated at 30 of 372 entries); degradation ongoing through last data at 08:59Z, with CPU pinned high and error_rate ~13.8-14.1% to 08:50. [F9, F10, F8]

## Recommended actions
- **[immediate]** Apply immediate edge-side mitigation on api-gateway for the /v1/auth/login route: enforce per-source rate limiting / admission control on POST /v1/auth/login and, if available, enable caching or challenge (e.g. token/bot) checks for repeated login attempts, to cut the ~4x exogenous rate back toward the ~390 req/s the service is known to handle; verify auth-service CPU (currently ~97%) and 5xx (~14%) fall within minutes. -- F24/F28 show the surge enters at api-gateway (+3,500-4,000 req/s) in the same 06:51 bucket as auth-service's onset; F10/F11 show auth-service CPU pinned ~97% with ~1,490-1,510 req/s, above its verified adequate ~390 req/s capacity. Reducing inbound rate at the edge is the safest direct mitigation and needs no auth-service change. [F24, F28, F10, F11, F13, F9]
- **[immediate]** Verify autoscaling / manually add capacity to auth-service: confirm whether auth-service has a horizontal autoscaler and, if not, scale out instances (or raise CPU-based scaling limits) so that accept-queue depth and p99 recover, then confirm the accept-queue-full 503s (F9) stop. -- F12 shows no restarts and the log signature is accept-queue exhaustion (F9); F10/F14 show pure CPU saturation, so added capacity is the direct inverse of the observed failure mode while edge mitigation takes effect. [F9, F10, F12, F14]
- **[immediate]** Complete traffic provenance decomposition at the edge: pull api-gateway per-endpoint and per-source (IP / user-agent / consumer identity) breakdown for POST /v1/auth/login over 06:45-07:05Z, and auth-service access logs, to attribute the +1,100-1,500 req/s login step to external end-users/bots versus a specific internal caller. -- The reviewed ranking's residual uncertainty is exactly this split: the origin is verified as entering above auth-service and not from any measured peer (F24-F28), but external-vs-internal-caller attribution is inferred. This measurement discriminates the remaining sub-question of H2 and would also detect any unmeasured internal caller. [F24, F25, F27, F28]
- **[short_term]** Do NOT roll back portfolio-service dep-5803 v4.4.0 as a fix for this incident; instead, close out the H4 lead by recording that portfolio-service request_rate, p99 and error_rate were flat and no peer contributed rate to the gateway. -- F25/F26 show portfolio-service rate (507-618 req/s) and latency (164-195 ms) flat with no onset step, and F27 shows every other peer flat; F17/F21/F22 show redis-cache (its only shared link) flat. A rollback would be an unfalsified change with no supporting measurement and would not remove the surge. [F25, F26, F27, F17, F21, F22]
- **[short_term]** Close out the auth-service code-path blame explicitly: confirm the log.level cfg-2801 change (03:40Z) and the absence of any bcrypt/parameter change, and record that no auth-service or auth-related deployment exists in the window or the prior week. -- F1/F2 establish zero deployments for two weeks and F3 shows the sole config change is verbosity-only; F15 (flat memory) and F11/F13 (request_rate rose 4x) are the opposite of the bcrypt flat-load signature, so no code rollback is indicated despite the HIST-101 resemblance (F6). [F1, F2, F3, F15, F11, F13, F6]
- **[short_term]** Instrument and alert on auth-service accept-queue depth / saturation and on a sustained surge of POST /v1/auth/login request rate at the gateway, with an alert on auth CPU > 80% and on the accept-queue-full log pattern, so capacity pressure is caught before 503s begin. -- The onset had a clean discriminable ordering (request_rate first at 06:51, CPU 06:48 bucket, p99 06:54, first 503 log 06:55:42) that could have been alerted on well before user-visible failures; F9/F13/F14 provide the exact signals to alert on. [F9, F13, F14, F10]
- **[long_term]** Establish a load-shedding / protection policy for the login path: per-source rate limits, bot/abuse detection and possibly a lightweight pre-auth screen in front of /v1/auth/login, plus a documented capacity headroom target derived from the ~390 req/s verified-safe level. -- The root cause is a 4x exogenous rate surge against a service sized for ~390 req/s (F11/F13, F24); the durable prevention is to bound how much of that surge can reach an expensive authentication path rather than to add unlimited capacity. [F11, F13, F24, F28]
- **[long_term]** Enable autoscaling with adequate headroom for auth-service and verify scaling behaviour under a synthetic 4x login-rate load test in staging, including confirmatory checks that CPU, accept-queue depth and p99 remain within SLO. -- F10/F14 show the failure mode is pure CPU saturation with no restart (F12); a verified scale-out path converts this class of surge from an outage into a capacity event, and the load test validates it against the observed 06:51 pattern. [F10, F12, F14]

## Data gaps
- Per-endpoint and per-source decomposition of api-gateway traffic to POST /v1/auth/login was never pulled, so the split between external end-user/bot traffic and an unmeasured internal caller is inferred from magnitude, not measured.
- auth-service access logs and api-gateway access logs were not queried for source IP / user-agent / consumer identity, so the origin of the surge cannot be mechanistically attributed.
- kyc-provider is external with has_telemetry:false and no vendor status/error data was available, so an external dependency contribution cannot be formally excluded.
- Blast radius to peer services was only confirmed for the six peers queried (portfolio-service, pricing-engine, market-data-service, payment-service, settlement-service plus api-gateway); order-service and notification-service were not measured.
- The ERROR log result was truncated (30 of 372 matches) and no non-ERROR/WARN or dependency-side logs were pulled, so the exact first second of onset and any cache/DB connection-level error text are unavailable.
- No auth-service CPU-per-endpoint metric exists (available metrics are only cpu_pct, error_rate, latency_p99_ms, memory_pct, rate_limited_per_min, request_rate, restarts), so the rule-out of a bcrypt component within the CPU burn rests on inference rather than direct endpoint-level measurement.
- Autoscaling configuration for auth-service is untracked in this data source, so the capacity argument is consistent with the flat restarts metric but not directly verified.
- Non-resource shared-path failure modes (network/route/config on the user-db or redis-cache path) would not be visible in the metrics examined and were not checked.

## What would change this conclusion
- A per-source/per-endpoint gateway or access-log decomposition showing that the 06:51 login step originated from a specific internal service (or from portfolio-service) rather than external/bot clients would shift the origin to H4 or a new internal-caller hypothesis.
- Evidence of any auth-service code, config or runtime parameter change affecting bcrypt cost (feature flag, runtime toggle) in the window would revive H1 despite the absence of a tracked deployment.
- Movement in redis-cache or user-db metrics (CPU, hit ratio, evictions, replication lag, slow queries, connected clients, memory) before 06:51Z would revive the shared-dependency-first hypothesis H3.
- Showing that the api-gateway step at 06:51 is unrelated to the traffic reaching /v1/auth/login (e.g. it is a different endpoint's traffic) would remove the edge-origin evidence and leave the surge origin unexplained.
- auth-service p99/CPU/503s recovering without any reduction in inbound login rate (and without capacity added) would indicate a cause other than load-driven saturation.
- A kyc-provider or other external dependency outage correlating with the 06:51 onset and end of the degradation would indicate a dependency-failure origin rather than a rate surge.

## Integrity warnings (set by code)
- fact demoted to assumption: Historical precedent HIST-101 (2025-08-14) had near-identical symptoms (login p9
- fact demoted to assumption: The gateway step magnitude (+3,500-4,000 req/s above baseline) versus auth-servi

## Investigation stats
- latency 186.38s, LLM calls 21, tokens 156943 (in 127734 / out 29209)
- tool calls 36 (1 failed, 0 partial, 0 recovered by retry), findings 28/28 verified
