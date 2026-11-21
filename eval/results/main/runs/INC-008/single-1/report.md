# Incident investigation report: INC-008
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Login slowness/503s on auth-service are caused by capacity saturation, not a bcrypt code regression. From the 06:51 UTC bucket, login request rate rose ~3.8x (auth-service ~395->~1500 req/s; api-gateway ~1240->~4850 req/s) at the same instant on both services. auth-service's CPU-bound bcrypt worker pool saturated (active=64/64, CPU ~97%), the accept queue filled, and it began returning 503 on /v1/auth/login (372 errors, first 06:55:42Z) with p99 ~3.8s. The autoscaler wanted 14 replicas but is capped at maxReplicas=8, so it could not add capacity. Downstream components (user-db, redis-cache) absorbed the load without saturating, memory stayed flat (~52%), restarts were 0, and there were no auth-service or api-gateway deployments/config changes in the window — ruling out the reported 'bcrypt regression' (which matches a *different*, 2025 incident that did involve a release). Remaining gap: tooling cannot distinguish an organic traffic surge from a client retry/bot storm, which changes the right mitigation but not the category.

## Probable root cause
**Capacity saturation: ~3.8x login traffic surge overran auth-service provisioned capacity (maxReplicas=8)** -- capacity_saturation in `auth-service` (confidence 0.80, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Capacity saturation: ~3.8x login traffic surge overran auth-service provisioned capacity (maxReplicas=8) | capacity_saturation | auth-service | - | 0.80 (high) | active |
| 2 | H2: bcrypt cost-factor regression after an auth-service release (the reported guess / HIST-101 repeat) | bad_deployment | auth-service | - | 0.08 (low) | rejected |
| 3 | H3: Downstream dependency failure (user-db or redis-cache) causing login slowness | dependency_failure | - | - | 0.05 (low) | rejected |

### H1: Capacity saturation: ~3.8x login traffic surge overran auth-service provisioned capacity (maxReplicas=8)
An external ~3.8x surge in login traffic (visible equally at api-gateway and auth-service from the 06:51 bucket) drove auth-service's CPU-bound bcrypt worker pool to 100% (active=64/64), filled the accept queue, and caused 503s on /v1/auth/login. The autoscaler wanted 14 replicas but the deployment is capped at maxReplicas=8, so it could not add capacity. No faulty code or config change is involved.

**Causal chain**
1. External traffic surge ~3.8x hits the edge [F7, F3]
2. api-gateway passes the surge through (its own CPU flat ~42%) [F8]
3. auth-service CPU-bound bcrypt worker pool (64/64) saturates at ~97% CPU [F4, F1]
4. accept queue fills -> 503 on /v1/auth/login; p99 climbs to ~3.8s [F1, F2, F5]
5. autoscaler blocked at maxReplicas 8 (wanted 14) so no relief [F6]
6. user-db/redis absorb the load without saturating -> no downstream fault [F11, F12]

**Supporting evidence**
  - `F2` ✔ auth-service latency_p99 jumped from ~90ms (bucket 06:42) to 2532ms in the 06:51 bucket and stayed ~3.8s through 08:57.
  - `F3` ✔ auth-service request_rate rose from ~395 req/s (06:42) to ~1480-1520 req/s from 07:00 onward — about a 3.8x increase.
  - `F4` ✔ auth-service CPU jumped from ~38% (06:42) to ~90% in the 06:51 bucket and pinned at ~97% (max 99.54%) thereafter.
  - `F5` ✔ auth-service error_rate rose from ~0.2% to ~14% (max 15.67%) starting the 06:51 bucket.
  - `F6` ✔ auth-service autoscaler repeatedly reports 'desired replicas 14 exceeds maxReplicas 8; cannot scale further' (34 occurrences, first 06:57:15Z).
  - `F7` ✔ api-gateway (the caller of auth-service) request_rate rose from ~1240 req/s (06:42) to ~4850 req/s from 07:00 — the same ~3.8x surge, at the same onset bucket.
  - `F8` ✔ api-gateway CPU stayed flat at ~42% throughout despite the surge (06:51-08:57 all ~40-43%), so the gateway itself was not saturated.
  - `F11` ✔ user-db connections_active rose from ~25 (06:42) to ~79 (~3x) at the same onset, but user-db CPU stayed flat at ~18% and slow_queries_per_min stayed flat at ~0.3/min.
  - `F13` ✔ auth-service memory_pct stayed flat (~52%) and restarts stayed at 0 throughout, ruling out a memory leak / crash-restart loop.
  - `F14` ✔ rate_limited_per_min on auth-service rose from ~2/min to ~900/min during the incident, i.e. rate limiting was active but could not shed enough to protect the pool.
  - `F16` ✔ Historical incident HIST-113 (2026-01-28) shows a precedent of capacity_saturation ('Record market-open volume exceeded provisioned capacity') resolved by raising replica count / pre-scaling.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The surge is genuine load that reached auth-service rather than a metric artifact
  - bcrypt cost factor was not changed (no deploy found)

### H2: bcrypt cost-factor regression after an auth-service release (the reported guess / HIST-101 repeat)
A code release raised bcrypt cost (e.g. 10->14), making each login CPU-heavier and slowing logins with 503s.

**Causal chain**
(none given)

**Supporting evidence**
  - `F1` ✔ auth-service warning/error logs begin at 06:54:53Z with 'worker pool saturated (active=64/64, queue=39035)' and the first 503 'accept queue full' at 06:55:42Z.
  - `F4` ✔ auth-service CPU jumped from ~38% (06:42) to ~90% in the 06:51 bucket and pinned at ~97% (max 99.54%) thereafter.
  - `F5` ✔ auth-service error_rate rose from ~0.2% to ~14% (max 15.67%) starting the 06:51 bucket.
  - `F15` ✔ A past incident HIST-101 (2025-08-14) had similar symptoms (login p99 4s, 503, high cpu) and root cause bcrypt cost factor raised 10->14 in a release; it was resolved by rollback.
**Contradicting evidence**
  - `F3` ✔ auth-service request_rate rose from ~395 req/s (06:42) to ~1480-1520 req/s from 07:00 onward — about a 3.8x increase.
  - `F7` ✔ api-gateway (the caller of auth-service) request_rate rose from ~1240 req/s (06:42) to ~4850 req/s from 07:00 — the same ~3.8x surge, at the same onset bucket.
  - `F9` ✔ No code deployment occurred for auth-service in the 7 days up to the incident; the only deployment in the window was portfolio-service v4.4.0 at 05:50Z (unrelated CSV export).
  - `F10` ✔ The only config change in the window was auth-service log.level INFO->WARN at 03:40Z (a log-volume change), and no api-gateway config changes occurred.
**Rejected because:** No auth-service deployment exists in the prior 7 days (tc-018 count=0), and no config change affects cost factor (only log.level change). The CPU rise coincides exactly with a 3.8x request-rate rise, so per-request cost did not change — volume did. HIST-101 is a prior look-alike, not evidence about this incident.

### H3: Downstream dependency failure (user-db or redis-cache) causing login slowness
A user-db or redis-cache fault slowed auth-service, causing queuing and 503s.

**Causal chain**
(none given)

**Supporting evidence**
  - `F1` ✔ auth-service warning/error logs begin at 06:54:53Z with 'worker pool saturated (active=64/64, queue=39035)' and the first 503 'accept queue full' at 06:55:42Z.
  - `F2` ✔ auth-service latency_p99 jumped from ~90ms (bucket 06:42) to 2532ms in the 06:51 bucket and stayed ~3.8s through 08:57.
**Contradicting evidence**
  - `F11` ✔ user-db connections_active rose from ~25 (06:42) to ~79 (~3x) at the same onset, but user-db CPU stayed flat at ~18% and slow_queries_per_min stayed flat at ~0.3/min.
  - `F12` ✔ redis-cache was healthy throughout: CPU flat ~22%, hit_ratio flat ~96%, no eviction or latency anomaly onset.
**Rejected because:** user-db CPU flat ~18%, slow_queries flat ~0.3/min, and redis-cache CPU ~22% with hit ratio ~96% — neither saturated nor degraded at onset. They absorbed the load, so they are not the origin.

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- Request-rate metrics for api-gateway and auth-service are directly comparable (both req/s) and the simultaneous 06:51 onset reflects a single shared traffic surge rather than two independent events.
- The 540s metric bucket resolution is coarse enough that the true onset lies within the 06:42-06:51 window; log first-match at 06:54:53 is consistent with this.
- No deployment or config change relevant to auth-service's login path occurred outside the queried 7-day/24h windows.

## Timeline
- `2026-04-22T03:40:00Z` auth-service config change log.level INFO->WARN (unrelated to symptom) [F10]
- `2026-04-22T05:50:00Z` portfolio-service v4.4.0 deployed (unrelated CSV export) [F9]
- `2026-04-22T06:51:00Z` Onset: auth-service and api-gateway request rate ~3.8x; auth CPU ~90%; p99 jumps to ~2.5s [F2, F3, F4, F7]
- `2026-04-22T06:54:53Z` auth-service worker pool saturated (active=64/64) [F1]
- `2026-04-22T06:55:42Z` First 503 'accept queue full' on POST /v1/auth/login [F1]
- `2026-04-22T06:57:15Z` Autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further [F6]
- `2026-04-22T07:00:00Z` Steady-state degraded: auth CPU ~97%, error_rate ~14%, p99 ~3.8s, request ~1500 req/s; user-db connections ~79 [F4, F5, F11]
- `2026-04-22T08:58:00Z` Symptoms still ongoing at latest data (autoscaler still capped, 503s continuing) [F6, F1]

## Recommended actions
- **[immediate]** Immediately raise auth-service maxReplicas (from 8, autoscaler wanted 14) and/or manually scale out to relieve the saturated worker pool -- Autoscaler desired 14 but is capped at 8; adding capacity is the direct relief for CPU-bound saturation [F6, F4]
- **[immediate]** Apply emergency rate limiting / load shedding at api-gateway for /v1/auth/login to protect the auth worker pool while capacity is added -- auth-service rate limiting (~900/min) was insufficient to prevent pool saturation; shedding at the edge protects the backend [F14, F1]
- **[short_term]** Determine whether the ~3.8x surge is organic traffic or a client retry/bot storm (e.g. per-IP/per-client request breakdown at the gateway) -- Distinguishes whether to provision more capacity permanently vs block/throttle an abusive client; tooling could not separate these [F7, F3]
- **[short_term]** Verify the bcrypt cost factor / worker-pool sizing has not changed and confirm the served request mix, to definitively retire the 'bcrypt regression' theory -- No deploy was found, but confirming per-request CPU cost is unchanged closes the reported hypothesis [F9, F4]
- **[long_term]** Right-size autoscaling (raise maxReplicas and pre-scale for known peak/login surges) and load-test auth-service to its new ceiling -- A hard maxReplicas cap turned a traffic surge into user-facing 503s; provisioned capacity must match peak demand [F6, F16]

## Data gaps
- Cannot distinguish an organic traffic surge from a client-side retry/bot amplification: api-gateway logs at ERROR/WARN contain no request-origin or per-client breakdown (tc-031 returned 0 matches for 'login'); no per-IP/client metric is available.
- The submission does not identify WHO/WHAT generated the ~3.8x surge; no load-generator or upstream client service is visible in the dependency graph (only api-gateway -> auth-service).
- bcrypt cost factor / per-request CPU cost is not directly exposed as a metric, so H2 is rejected by absence of a deploy rather than by a direct cost measurement.
- kyc-provider (external) has has_telemetry=false, so its health during the window could not be checked (though no provider errors appear on the login error path).

## Investigation stats
- latency 64.83s, LLM calls 8, tokens 141206 (in 130685 / out 10521)
- tool calls 35 (2 failed, 0 partial, 0 recovered by retry), findings 16/16 verified
