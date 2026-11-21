# Incident investigation report: INC-008
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
At ~06:48-06:51 a ~4x request-rate surge hit the edge (api-gateway ~1250->4900 req/s; auth-service ~390->1500 req/s). auth-service CPU saturated at ~97%, its worker pool (64/64) filled, and the autoscaler was capped at maxReplicas=8 while desiring 14, producing accept-queue-full 503s on /v1/auth/login and p99 ~3.8s. The reporter's bcrypt bad-deployment guess is not supported: there was no auth-service deployment (0 in window) and no code-affecting config change (only a log-level change); memory and restarts stayed flat, and the surge simultaneously affected api-gateway (CPU flat, so not CPU-bound) — a load-driven capacity problem. Downstream redis-cache and user-db were healthy, ruling out a dependency fault. Root cause: capacity_saturation at auth-service (fault originates in load exceeding provisioned capacity / autoscaler maxReplicas cap). A key open gap is what generated the surge.

## Probable root cause
**Capacity saturation: edge-wide traffic surge exceeded auth-service provisioned capacity** -- capacity_saturation in `auth-service` (confidence 0.72, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Capacity saturation: edge-wide traffic surge exceeded auth-service provisioned capacity | capacity_saturation | auth-service | - | 0.72 (medium) | active |
| 2 | H2: Bad deployment / bcrypt-style regression in auth-service (reporter's hypothesis) | bad_deployment | auth-service | - | 0.08 (low) | rejected |
| 3 | H3: Downstream dependency failure (user-db or redis-cache) | dependency_failure | user-db | - | 0.05 (low) | rejected |

### H1: Capacity saturation: edge-wide traffic surge exceeded auth-service provisioned capacity
A ~4x request-rate surge hit the edge (api-gateway and auth-service) starting ~06:48-06:51. auth-service's provisioned capacity (worker pool of 64, autoscaler capped at maxReplicas=8 while desiring 14) could not absorb it, driving CPU to ~97%, saturating the worker pool and accept queue, and returning 503s on /v1/auth/login. The fault originates in the load exceeding provisioned capacity, not in auth code.

**Causal chain**
1. Inbound request rate rises ~4x across the edge at ~06:48-06:51 [F6, F2, F14]
2. auth-service CPU saturates (~97%) and its worker pool (64/64) fills [F3, F4]
3. Autoscaler cannot add replicas beyond maxReplicas=8 [F5]
4. Queue overflows, requests rejected with 503, p99 rises to ~3.8s [F4, F1]

**Supporting evidence**
  - `F2` ✔ auth-service request_rate rose ~4x from ~790 req/s baseline to ~1300-1500 req/s at the same 06:51 onset.
  - `F3` ✔ auth-service CPU rose from ~38% to ~97% at 06:51 and stayed saturated.
  - `F4` ✔ auth-service logs show worker pool saturation (active=64/64) and 503 rejections on POST /v1/auth/login beginning 06:54-06:55.
  - `F5` ✔ Auth-service autoscaler repeatedly reports it cannot scale beyond 8 replicas while desiring 14.
  - `F6` ✔ API-gateway request_rate rose ~4x concurrently, from ~1250 to ~4900 req/s at the 06:51 onset bucket.
  - `F11` ✔ auth-service rate_limited_per_min jumped from ~2/min to ~900/min at the 06:51 onset, consistent with a large volume of throttled inbound requests.
  - `F13` ✔ user-db connections_active roughly tripled (25->78) at the 06:51 onset, matching the higher request volume rather than a db fault.
  - `F14` ✔ A 6-minute-resolution view shows auth-service traffic ramping 397->527->1398 req/s between 06:42 and 06:54, a sharp step increase at onset.
  - `F15` ✔ A prior incident (HIST-113) had the same category (capacity_saturation: market-open volume exceeding provisioned replicas) resolved by raising replica count and pre-scaling.
**Contradicting evidence**
  - `F7` ✔ API-gateway CPU stayed flat at ~41-42% despite the 4x traffic increase, indicating it is not CPU-bound and merely proxying the surge.
**Assumptions**
  - The 4x request-rate metric reflects genuine inbound demand rather than a measurement artifact.
  - The worker-pool/maxReplicas limits are the true binding capacity constraint.

### H2: Bad deployment / bcrypt-style regression in auth-service (reporter's hypothesis)
An auth-service code change raised per-login CPU cost (like the 2025 bcrypt cost regression), causing CPU saturation and 503s. This is rejected: there is no auth-service deployment in the window, no code-affecting config change, memory is flat and restarts are zero, and the latency/CPU jump coincides exactly with a 4x traffic increase across multiple services.

**Causal chain**
(none given)

**Supporting evidence**
  - `F16` ✔ The reporter's hypothesis cites HIST-101 (bcrypt cost raised 10->14 in a release), but no auth-service release and no config change affecting auth code occurred in the current window.
**Contradicting evidence**
  - `F8` ✔ No auth-service deployments exist in the window; the only deployment anywhere in the window is portfolio-service v4.4.0 at 05:50, whose request_rate remained flat (~560 req/s).
  - `F9` ✔ The only config change is cfg-2801 on auth-service: log.level INFO->WARN at 03:40, which does not affect request handling.
  - `F10` ✔ auth-service memory_pct stayed flat (~52%) and restarts stayed 0 throughout, contradicting a memory-leak or crash-loop mechanism.
  - `F6` ✔ API-gateway request_rate rose ~4x concurrently, from ~1250 to ~4900 req/s at the 06:51 onset bucket.
  - `F7` ✔ API-gateway CPU stayed flat at ~41-42% despite the 4x traffic increase, indicating it is not CPU-bound and merely proxying the surge.
**Rejected because:** No auth-service deployment (tc-033 count=0) or auth code config change; the only config change is a log level (F9). The onset coincides with a 4x traffic surge affecting api-gateway too (F6), and auth CPU/latency scaling tracks request volume rather than a constant per-request cost increase.

### H3: Downstream dependency failure (user-db or redis-cache)
Slowness in a shared datastore (user-db or redis-cache) slowed auth-service, causing latency and 503s. Rejected: redis hit ratio stayed ~96% with cpu ~22%, and user-db slow_queries stayed flat at ~0.3/min; connection growth matches request volume rather than a db fault.

**Causal chain**
(none given)

**Supporting evidence**
  - `F13` ✔ user-db connections_active roughly tripled (25->78) at the 06:51 onset, matching the higher request volume rather than a db fault.
**Contradicting evidence**
  - `F12` ✔ Downstream redis-cache and user-db remained healthy: redis hit_ratio ~96% and cpu ~22% flat; user-db slow_queries_per_min flat at ~0.3.
  - `F10` ✔ auth-service memory_pct stayed flat (~52%) and restarts stayed 0 throughout, contradicting a memory-leak or crash-loop mechanism.
**Rejected because:** redis-cache hit_ratio ~96% and cpu ~22% flat (F12); user-db slow_queries flat ~0.3/min (F12); no dependency error signatures in logs.

## Facts (verified observations)
- auth-service p99 latency jumped from ~89ms to ~3.8s at the 06:51 bucket and stayed elevated. [F1]
- auth-service request rate rose ~4x at the same onset window. [F2, F14]
- auth-service CPU rose from ~38% to ~97% at 06:51. [F3]
- auth-service logged worker-pool saturation (64/64) and 503 rejections on /v1/auth/login from ~06:54-06:55. [F4]
- Auth-service autoscaler reported desired replicas 14 exceeding maxReplicas 8. [F5]
- API-gateway request rate rose ~4x concurrently at 06:51. [F6]
- API-gateway CPU stayed flat ~42% despite the 4x traffic, so it was not CPU-bound. [F7]
- No auth-service deployment occurred in the window; the only deployment was portfolio-service v4.4.0 with flat traffic. [F8]
- The only config change was auth-service log.level INFO->WARN, unrelated to request handling. [F9]
- auth-service memory stayed flat (~52%) and restarts stayed 0. [F10]
- auth-service rate_limited_per_min jumped from ~2/min to ~900/min at onset. [F11]
- redis-cache and user-db remained healthy (hit ratio ~96%, cpu ~22%, slow queries flat ~0.3/min). [F12]
- user-db active connections roughly tripled (25->78) at onset, tracking request volume. [F13]

## Assumptions (not verified)
- The request_rate metric reflects genuine inbound demand rather than a monitoring artifact or duplicated/replayed traffic.
- maxReplicas=8 and the 64-worker pool are the true binding capacity limits for auth-service.
- No other unobserved component (e.g., a bot/attack source, or a client retry storm) is the underlying trigger of the surge.

## Timeline
- `2026-04-22T03:40:00Z` Config change cfg-2801: auth-service log.level INFO->WARN (benign). [F9]
- `2026-04-22T05:50:00Z` portfolio-service v4.4.0 deployed (only deployment; traffic stayed flat, unrelated to surge). [F8]
- `2026-04-22T06:48:00Z` Edge traffic begins ramping (auth ~527 req/s, then 1398 by 06:54). [F14]
- `2026-04-22T06:51:00Z` Onset: auth p99 89ms->3.8s, CPU 38%->94%, api-gateway rate 1250->4148 req/s, rate-limited 2->705/min. [F1, F2, F3, F6, F11]
- `2026-04-22T06:54:53Z` auth-service worker pool saturated (64/64, large queue). [F4]
- `2026-04-22T06:55:42Z` First 503 'accept queue full' on POST /v1/auth/login. [F4]
- `2026-04-22T06:57:15Z` Autoscaler cannot exceed maxReplicas 8 (desires 14). [F5]
- `2026-04-22T07:06:00Z` Incident reported by on-call SRE. []
- `2026-04-22T08:30:00Z` Sustained 503s and autoscaler cap warnings continue. [F4, F5]

## Recommended actions
- **[immediate]** Immediately raise auth-service maxReplicas (and/or pre-scale replicas) to restore headroom, and reduce the accept-queue-full 503 shedding. -- Autoscaler wanted 14 replicas but was capped at 8, so capacity is the binding constraint. [F5, F4]
- **[immediate]** Identify and throttle/block the source of the 4x traffic surge (e.g., bot or client retry storm) at the edge/rate limiter. -- Root driver is the ~4x inbound request surge hitting the whole edge; rate_limited jumped to ~900/min. [F2, F6, F11]
- **[short_term]** Tune autoscaler policy so desired-replica targets can be met (raise maxReplicas ceiling) and add pre-scaling for known traffic peaks. -- Prior capacity incident HIST-113 was resolved by raising replicas and pre-scaling before peaks. [F15, F5]
- **[long_term]** Add alerting on auth-service worker-pool saturation and accept-queue depth to catch capacity exhaustion earlier than p99/error-rate. -- Worker-pool saturation preceded the 503s and is a leading indicator. [F4]

## Data gaps
- No telemetry identifies the SOURCE of the 4x traffic surge (no upstream/client-field metrics, no WAF/bot data); portfolio-service was NOT the source and no deploy/config explains it.
- The identity of the surge driver (external load, bot, or a retry storm internal to the platform) is unknown.
- No client-side/edge request-mix data (e.g., login-specific vs total) to confirm the surge is concentrated on /v1/auth/login.
- user-db has no latency metric, so its per-query latency during the surge could not be verified.

## Investigation stats
- latency 62.72s, LLM calls 10, tokens 154357 (in 144919 / out 9438)
- tool calls 35 (3 failed, 0 partial, 0 recovered by retry), findings 16/16 verified
