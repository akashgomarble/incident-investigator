# Incident investigation report: INC-008
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Login latency (~3.9s p99) and 503s on /v1/auth/login began abruptly at ~06:48 UTC on 2026-04-22 and persisted unchanged through 08:59. Investigation shows no faulty release or config change on the auth path: the only deployment (dep-5803, portfolio-service, 05:50) is unrelated and its traffic is flat, and the only config change (auth log.level 03:40) predates onset by hours. Instead, a burst of login traffic hit auth-service: its request_rate jumped ~4x (≈400→1500 req/s) at the same instant the api-gateway rate rose ~4x (≈1250→4900 req/s) while unrelated backends (portfolio-service ≈570, order-service ≈230) stayed flat — so the extra traffic is concentrated on the auth/login path, with auth rate-limiting firing at ~900/min. auth-service CPU went 38%→97% and stayed pinned; its worker pool saturated (64/64, queues up to ~98k) and it began rejecting requests ("accept queue full, returning 503"), while the autoscaler could not help ("desired replicas 14 exceeds maxReplicas 8"). Datastores (user-db CPU ~18%, redis CPU ~22%, redis hit ratio ~96%) and the gateway's own CPU (~42%, flat) remained healthy, so the bottleneck is auth-service compute capacity. The reported "bcrypt regression" is a false lead: there was no auth-service release, and the failure signature is accept-queue saturation rather than uniformly slow hashing. Root cause: auth-service capacity saturation — a login traffic surge exceeded provisioned CPU/replica capacity (hard-capped at maxReplicas 8), causing worker-pool/accept-queue exhaustion and 503s.

## Probable root cause
**auth-service capacity saturation under a login traffic surge** -- capacity_saturation in `auth-service` (confidence 0.72, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: auth-service capacity saturation under a login traffic surge | capacity_saturation | auth-service | - | 0.72 (medium) | active |
| 2 | H2: bcrypt cost-factor regression from an auth-service release (reported guess) | bad_deployment | auth-service | - | 0.08 (low) | rejected |
| 3 | H3: Downstream dependency (user-db/redis/kyc-provider) degradation | dependency_failure | user-db | - | 0.05 (low) | rejected |

### H1: auth-service capacity saturation under a login traffic surge
A burst of automated login traffic concentrated on /v1/auth/login (≈4x request rate, rate-limiting firing ~900/min) saturated auth-service's CPU-bound login path; its 64-worker pool and accept queue filled, producing 503s, and the autoscaler could not add capacity because replicas are hard-capped at maxReplicas 8. Fault originates in auth-service provisioning/limits (capacity), not in code.

**Causal chain**
1. Login traffic surge arrives at api-gateway [F9, F10]
2. Surge concentrates on auth/login path (peers flat) [F10, F11]
3. auth-service CPU-bound login work saturates 64-worker pool [F4, F5]
4. Autoscaler capped at maxReplicas 8 cannot absorb it [F6]
5. Accept queue fills -> 503s on /v1/auth/login [F5, F2]

**Supporting evidence**
  - `F1` ✔ auth-service latency_p99 jumped from ~90ms to ~3.8s in the 06:48 bucket and stayed there through 08:59.
  - `F2` ✔ auth-service error_rate (HTTP 5xx) rose from ~0.2% to ~14% starting in the 06:48 bucket and remained ~14%.
  - `F3` ✔ auth-service request_rate rose from ~400 req/s baseline to ~1500 req/s starting at 06:48.
  - `F4` ✔ auth-service CPU rose from ~38% to ~97% and stayed pinned there.
  - `F5` ✔ auth-service logs show worker pool saturated (active=64/64) with large queues, followed by 503 rejections 'request rejected: accept queue full' on POST /v1/auth/login.
  - `F6` ✔ auth-service autoscaler reported it wanted 14 replicas but was capped at maxReplicas 8.
  - `F9` ✔ api-gateway request_rate rose ~4x from ~1250 to ~4900 req/s at 06:48, while its own CPU stayed flat at ~42%.
  - `F10` ✔ Unrelated backends stayed flat: portfolio-service request_rate ~570 req/s and order-service ~230 req/s throughout, so the surge is not global.
  - `F11` ✔ auth-service rate_limited_per_min jumped from ~2/min to ~900/min, indicating heavy automated login attempts being throttled.
  - `F12` ✔ Datastores were healthy during the incident: user-db CPU flat ~18%, redis-cache CPU flat ~22% and hit_ratio ~96%.
  - `F13` ✔ auth-service restarts stayed at 0 throughout, ruling out crash-looping.
  - `F14` ✘ unverified auth-service memory_pct stayed flat ~52%, ruling out memory exhaustion.
**Contradicting evidence**
  - (none)
**Assumptions**
  - bcrypt hashing on the login path makes login CPU-bound, so request surge translates directly into CPU saturation
  - the gateway---auth rate surge reflects genuine/abusive login demand rather than an artifact of instrumentation
  - no auth-service release occurred outside the queried deployment windows (04:00-09:00 and 2026-04-21T12:00-09:00)

### H2: bcrypt cost-factor regression from an auth-service release (reported guess)
A code release raised the bcrypt work factor, making every login slow (as in HIST-101), which manifests as p99 ~3.8s and 503s.

**Causal chain**
(none given)

**Supporting evidence**
  - `F15` ✔ Historical incident HIST-101 (2025-08-14) had the same symptom pattern but its root cause was a bcrypt cost-factor increase introduced by release v6.4.0 (a deployment), resolution was rollback.
**Contradicting evidence**
  - `F7` ✔ No deployment to auth-service occurred in the incident window; the only deployment across all services was portfolio-service dep-5803 at 05:50, which is unrelated to login.
  - `F8` ✔ The only config change in the window was auth-service log.level INFO->WARN at 03:40, hours before onset.
  - `F3` ✔ auth-service request_rate rose from ~400 req/s baseline to ~1500 req/s starting at 06:48.
  - `F5` ✔ auth-service logs show worker pool saturated (active=64/64) with large queues, followed by 503 rejections 'request rejected: accept queue full' on POST /v1/auth/login.
  - `F9` ✔ api-gateway request_rate rose ~4x from ~1250 to ~4900 req/s at 06:48, while its own CPU stayed flat at ~42%.
  - `F12` ✔ Datastores were healthy during the incident: user-db CPU flat ~18%, redis-cache CPU flat ~22% and hit_ratio ~96%.
**Rejected because:** No auth-service deployment exists in the incident window (only the unrelated portfolio-service dep-5803), so no code change could have raised the cost factor. The failure signature is immediate accept-queue/worker-pool saturation coincident with a ~4x request-rate surge, not uniformly slow hashing; datastores and the gateway's own CPU are healthy. The symptom similarity to HIST-101 is a surface match only.

### H3: Downstream dependency (user-db/redis/kyc-provider) degradation
A dependency of auth-service degraded, causing login requests to block and back up in the worker pool.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F12` ✔ Datastores were healthy during the incident: user-db CPU flat ~18%, redis-cache CPU flat ~22% and hit_ratio ~96%.
  - `F4` ✔ auth-service CPU rose from ~38% to ~97% and stayed pinned there.
**Rejected because:** user-db CPU (~18%) and redis CPU (~22%)/hit_ratio (~96%) are flat and healthy; user-db connections rising is downstream of the auth surge, not a cause. kyc-provider has no telemetry but auth CPU is pinned at 97% (compute, not IO blocking). No dependency shows degradation at onset.

## Facts (verified observations)
- auth-service p99 latency jumped from ~90ms to ~3.8s at 06:48 and stayed there through 08:59. [F1]
- auth-service 5xx error rate rose from ~0.2% to ~14% beginning at 06:48. [F2]
- auth-service request rate rose ~4x (≈400 to ≈1500 req/s) starting at 06:48. [F3]
- auth-service CPU rose from ~38% to ~97% and stayed pinned. [F4]
- auth-service worker pool saturated at 64/64 and it returned 503 'accept queue full' on POST /v1/auth/login. [F5]
- auth-service autoscaler was capped at maxReplicas 8 while desiring 14 replicas. [F6]
- No auth-service deployment occurred in the incident window; the only deployment was unrelated portfolio-service dep-5803 at 05:50. [F7]
- The only config change in the window was auth-service log.level INFO->WARN at 03:40, hours before onset. [F8]
- api-gateway request rate rose ~4x while its own CPU stayed flat at ~42%. [F9]
- Portfolio-service (~570 req/s) and order-service (~230 req/s) request rates stayed flat, so the surge is not global. [F10]
- auth-service rate_limited_per_min jumped from ~2/min to ~900/min. [F11]
- user-db CPU (~18%), redis-cache CPU (~22%) and redis hit_ratio (~96%) were flat and healthy. [F12]
- auth-service restarts remained 0 throughout. [F13]

## Assumptions (not verified)
- The login path is CPU-bound due to bcrypt hashing, so a login request surge directly drives CPU saturation (not explicitly confirmed by per-endpoint metrics).
- The api-gateway->auth request-rate rise reflects actual login demand/abuse rather than a metrics/instrumentation artifact.
- No auth-service deployment occurred outside the two queried deployment windows.
- Observational correlation at 06:48 across request_rate, CPU, latency and errors indicates a single common trigger rather than coincidence.
- (demoted from facts: no verified observation backs it) auth-service memory_pct stayed flat ~52%.

## Timeline
- `2026-04-22T03:40:00Z` auth-service config change (log.level INFO->WARN) — hours before onset, not a trigger. [F8]
- `2026-04-22T05:50:00Z` portfolio-service dep-5803 deployed (v4.4.0); unrelated, its traffic stays flat. [F7, F10]
- `2026-04-22T06:48:00Z` Onset: auth request rate ~4x, api-gateway rate ~4x, auth CPU 38->97%, latency and 5xx begin rising. [F1, F3, F4, F9]
- `2026-04-22T06:54:53Z` auth-service worker pool saturates (active=64/64, queue=39035). [F5]
- `2026-04-22T06:55:42Z` First 503 'accept queue full' on POST /v1/auth/login. [F5]
- `2026-04-22T06:57:15Z` Autoscaler reports desired 14 replicas exceeds maxReplicas 8. [F6]
- `2026-04-22T07:06:00Z` Incident reported (p99 3.8s, 503s). [F1]
- `2026-04-22T08:59:00Z` Latest data: latency, error rate, CPU and request rate still at elevated plateau. [F1, F2, F4]

## Recommended actions
- **[immediate]** Raise auth-service maxReplicas above 8 (and/or add nodes) to let the autoscaler absorb the login surge. -- Autoscaler is blocked at maxReplicas 8 while desiring 14; this is the direct capacity ceiling causing saturation. [F6, F4]
- **[immediate]** Investigate and mitigate the login traffic source (rate-limit/bot protection at the edge, CAPTCHA, or client-side retry loops) since rate-limiting is firing ~900/min. -- The surge is concentrated on the auth/login path and is being rate-limited heavily, indicating abusive or runaway client behavior. [F9, F10, F11]
- **[short_term]** Add per-endpoint/edge visibility to distinguish legitimate login demand from credential-stuffing and to confirm login-specific vs global traffic. -- Current evidence infers concentration on login from peer-service flatness; direct endpoint metrics would confirm. [F10, F9]
- **[short_term]** Do NOT roll back an auth-service release for this incident. -- No auth release exists in the window; a rollback would waste effort and the reported bcrypt narrative is unsupported by the data. [F7, F5]
- **[long_term]** Load-test and right-size the login path (proxy/accept-queue limits, worker pool, pre-scaling) for peak login scenarios. -- The system degraded to accept-queue-full 503s rather than degrading gracefully under a 4x login surge. [F5, F6]

## Data gaps
- No per-endpoint or per-route metrics; the concentration of the surge on /v1/auth/login is inferred from peer-service flatness, not measured directly.
- No client-side/source telemetry to confirm whether the login surge is credential-stuffing/abuse, a retry storm, or legitimate demand.
- kyc-provider is an external dependency with no telemetry; cannot fully exclude a small contribution, though auth CPU saturation points to local compute.
- Deployment history queried only over 2026-04-21T12:00 to 2026-04-22T09:00; an earlier auth release beyond that window is not visible (but the abrupt 06:48 step change argues against it).

## Integrity warnings (set by code)
- fact demoted to assumption: auth-service memory_pct stayed flat ~52%.

## Investigation stats
- latency 63.17s, LLM calls 8, tokens 149634 (in 138496 / out 11138)
- tool calls 39 (3 failed, 0 partial, 0 recovered by retry), findings 14/15 verified
