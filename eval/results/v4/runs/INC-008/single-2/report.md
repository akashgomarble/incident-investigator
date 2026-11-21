# Incident investigation report: INC-008
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
auth-service (and its caller api-gateway) saw a step-change surge at ~06:45 UTC: auth-service request_rate rose from ~395 to ~1490 req/s (~3.8x) and api-gateway from ~1250 to ~4890 req/s (~3.9x), while every other backend service (portfolio, market-data, pricing, order) stayed flat. auth-service CPU pinned from 38% to ~97%, worker pool saturated 64/64 with queues up to ~98k, latency p99 jumped from ~90ms to ~3.8-4s, error_rate from ~0.2% to ~14%, and 503s ("accept queue full") on /v1/auth/login. The autoscaler wanted 14 replicas but was capped at maxReplicas 8. No auth-service or api-gateway deployment or config change exists in the window; the only changes were a portfolio-service deploy (dep-5803) and an auth-service log.level INFO->WARN change (cfg-2801, cosmetic). Dependencies (redis-cache hit ratio ~96% flat, CPU flat; user-db connections rose proportionally 25->79 with no slow queries) are healthy. The evidence points to capacity_saturation: load on the auth/login path outgrew the provisioned/autoscaler-limited capacity, not a bcrypt code regression (the reported HIST-101 analog) — there is no deployment and the surge is a request-rate change, not a per-request cost change. The origin of the login-path surge (real user/bot traffic vs a retry loop) could not be determined from available telemetry.

## Probable root cause
**Capacity saturation on the auth/login path (autoscaler-capped)** -- capacity_saturation in `auth-service` (confidence 0.72, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Capacity saturation on the auth/login path (autoscaler-capped) | capacity_saturation | auth-service | - | 0.72 (medium) | active |
| 2 | H2: bcrypt cost regression from an auth-service release (reported guess / HIST-101 analog) | bad_deployment | auth-service | - | 0.08 (low) | rejected |
| 3 | H3: shared dependency (redis-cache / user-db) degradation | dependency_failure | redis-cache | - | 0.05 (low) | rejected |
| 4 | H4: auth-service config-change regression | config_change | auth-service | - | 0.05 (low) | rejected |

### H1: Capacity saturation on the auth/login path (autoscaler-capped)
A step-change surge of login-path traffic (~3.8-3.9x at api-gateway and auth-service, isolated from all other services) pushed auth-service CPU to ~97% and saturated its 64/64 worker pool; the autoscaler wanted 14 replicas but was capped at maxReplicas 8, so requests queued and were rejected with 503 (accept queue full), producing ~4s p99 latency and ~14% errors.

**Causal chain**
1. Login-path request rate at api-gateway and auth-service steps up ~3.8-3.9x at 06:45 [F2, F7, F8]
2. auth-service CPU saturates (~38%->97%) under the extra load [F3]
3. worker pool hits 64/64 with growing queues and accept queues overflow [F4]
4. autoscaler cannot add capacity beyond maxReplicas 8 [F5]
5. queue overflow returns 503 and latency rises to ~4s; error_rate ~14% [F1, F6]

**Supporting evidence**
  - `F1` ✔ auth-service latency p99 jumped from ~90ms (through 06:30) to 1555ms avg in the 06:45 bucket and then ~3.8s (avg 3763-3868ms) from 07:00 onward.
  - `F2` ✔ auth-service request_rate rose from ~395 req/s (through 06:30) to ~1490 req/s from 07:00 onward (~3.8x step change).
  - `F3` ✔ auth-service CPU jumped from ~38% to ~97% at the same time as the request-rate surge.
  - `F4` ✔ auth-service logs show worker pool saturated (active=64/64, queue up to 94198/98763) and 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)'.
  - `F5` ✔ auth-service autoscaler is capped: 'autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further'.
  - `F6` ✔ auth-service error_rate rose from ~0.2% to ~14% of requests (HTTP 5xx) at onset; rate_limited_per_min rose from ~2 to ~900.
  - `F7` ✔ api-gateway request_rate simultaneously rose from ~1250 to ~4890 req/s and latency p99 from ~207ms to ~3.9s, while its CPU stayed flat at ~42%.
  - `F8` ✔ Other backend services stayed flat across the onset: portfolio-service ~570 req/s, market-data-service ~940, pricing-engine ~640, order-service ~230.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The ~3.8x request-rate rise reflects genuine offered load rather than an internal retry loop; both produce the same saturation, so this only affects the trigger attribution, not the saturation mechanism.
  - auth-service throughput is CPU-bound on the login path (bcrypt), consistent with CPU pinning at 97%.

### H2: bcrypt cost regression from an auth-service release (reported guess / HIST-101 analog)
A code change in auth-service raised the bcrypt cost factor, increasing per-request CPU and latency, causing worker saturation and 503s.

**Causal chain**
(none given)

**Supporting evidence**
  - `F1` ✔ auth-service latency p99 jumped from ~90ms (through 06:30) to 1555ms avg in the 06:45 bucket and then ~3.8s (avg 3763-3868ms) from 07:00 onward.
  - `F3` ✔ auth-service CPU jumped from ~38% to ~97% at the same time as the request-rate surge.
  - `F4` ✔ auth-service logs show worker pool saturated (active=64/64, queue up to 94198/98763) and 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)'.
**Contradicting evidence**
  - `F2` ✔ auth-service request_rate rose from ~395 req/s (through 06:30) to ~1490 req/s from 07:00 onward (~3.8x step change).
  - `F7` ✔ api-gateway request_rate simultaneously rose from ~1250 to ~4890 req/s and latency p99 from ~207ms to ~3.9s, while its CPU stayed flat at ~42%.
  - `F8` ✔ Other backend services stayed flat across the onset: portfolio-service ~570 req/s, market-data-service ~940, pricing-engine ~640, order-service ~230.
  - `F9` ✔ No auth-service deployment exists in the window (0 deployments for auth-service from 2026-04-01 to 09:00), and no config change to auth-service except 'cfg-2801 log.level INFO->WARN' at 03:40.
  - `F12` ✔ No 'bcrypt' or 'kyc' related log entries exist in auth-service logs during the incident window (both queries returned 0 matches).
**Rejected because:** No auth-service deployment exists in the window (tc-036 count 0); a per-request cost change would not raise request_rate 3.8x (F2/F7); no bcrypt log lines (F12). The observed 3.8x request-rate surge plus flat other services contradicts a pure per-request cost regression.

### H3: shared dependency (redis-cache / user-db) degradation
A degraded shared datastore (redis-cache used by 4 services, or user-db) slowed auth-service, causing backlog and 503s.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ Other backend services stayed flat across the onset: portfolio-service ~570 req/s, market-data-service ~940, pricing-engine ~640, order-service ~230.
  - `F10` ✔ redis-cache is healthy during the incident: hit_ratio_pct flat ~96%, cpu_pct flat ~22%, connected_clients flat ~310.
  - `F11` ✔ user-db shows no degradation: connections_active rose proportionally with load (25->79) and slow_queries_per_min stayed flat at ~0.3.
**Rejected because:** redis-cache hit ratio/CPU/clients are flat and healthy (F10); user-db slow queries flat and connections scale proportionally with load (F11). A shared-cache failure would also hit the other three redis clients, which stayed flat (F8).

### H4: auth-service config-change regression
A configuration/feature-flag change to auth-service caused the slowdown.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ No auth-service deployment exists in the window (0 deployments for auth-service from 2026-04-01 to 09:00), and no config change to auth-service except 'cfg-2801 log.level INFO->WARN' at 03:40.
**Rejected because:** The only auth-service config change is cfg-2801 log.level INFO->WARN at 03:40, ~3h before onset and cosmetic; it cannot raise CPU or latency (F9).

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- The request_rate metric bucket at 06:45 (avg 849/2532) covers partial-minute mixing of the pre- and post-surge periods; onset is between 06:30 and 06:45.
- auth-service 'maxReplicas 8' is the effective autoscaling ceiling and could not be raised automatically during the incident.
- The login path is CPU-bound in auth-service, so worker saturation follows directly from CPU saturation.

## Timeline
- `2026-04-22T03:40:00Z` auth-service config change cfg-2801: log.level INFO->WARN (cosmetic). [F9]
- `2026-04-22T05:50:00Z` portfolio-service deployed v4.4.0 (dep-5803); unrelated (flat error rate). [F13]
- `2026-04-22T06:19:11Z` First anomaly: 'slow request 3759ms on /internal/metrics' (pre-surge noise). []
- `2026-04-22T06:45:00Z` Onset: auth-service request_rate ~395->~1490 and api-gateway ~1250->~4890; latency p99 begins climbing. [F2, F7]
- `2026-04-22T06:54:53Z` auth-service worker pool saturated (active=64/64, queue=39035); CPU pinned ~97%. [F3, F4]
- `2026-04-22T06:55:42Z` First 503: 'accept queue full, returning 503 (POST /v1/auth/login)'. [F4]
- `2026-04-22T06:57:15Z` Autoscaler capped: desired 14 > maxReplicas 8. [F5]
- `2026-04-22T07:00:00Z` Steady state: auth-service p99 ~3.8s, error_rate ~14%, rate_limited ~900/min. [F1, F6]
- `2026-04-22T08:45:00Z` Incident still ongoing at last data (latency ~3.8s, CPU ~97%). [F1, F3]

## Recommended actions
- **[immediate]** Immediately raise auth-service maxReplicas (and/or provisioned capacity) so the autoscaler can add capacity, and pre-scale the login path. -- Autoscaler was capped at maxReplicas 8 (desired 14) while CPU was pinned at 97% and worker queues overflowed. [F3, F4, F5]
- **[immediate]** Add rate limiting / admission control and client-side backoff on the login endpoint to prevent amplification (retry storms) against an already saturated auth path. -- rate_limited_per_min spiked to ~900 and the surge is isolated to the login path; retries amplify pressure. [F6, F7]
- **[short_term]** Investigate the source of the ~3.8x login-path surge (bot/credential-stuffing vs client retry loop vs traffic event) at the edge. -- The origin of the surge is a data gap; understanding it prevents recurrence. [F2, F7, F8]
- **[short_term]** Do NOT roll back auth-service on the basis of the bcrypt hypothesis; verify no auth-service deploy exists before any rollback. -- No auth-service deployment exists in the window; rollback would be a no-op and delay real mitigation. [F9, F12]
- **[long_term]** Add autoscaling headroom and pre-scale before known peak windows for CPU-bound auth paths. -- Mirrors past capacity_saturation incident (HIST-113: raise replica count, pre-scale before open). [F5]

## Data gaps
- No telemetry identifies WHO/WHAT generated the ~3.8x login-path request surge (no access-log client/IP/bot attribution; api-gateway logs are sparse and not per-request).
- api-gateway has no rate_limited_per_min metric (tc-035 not_found), so gateway-side rejection/retry amplification cannot be quantified.
- kyc-provider (external dependency) has no telemetry, so it cannot be fully ruled out as a secondary contributor to login worker time.
- No per-route request-rate breakdown, so it is not provable that the entire surge is /v1/auth/login vs other gateway paths.

## Investigation stats
- latency 75.85s, LLM calls 7, tokens 123668 (in 112546 / out 11122)
- tool calls 43 (3 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
