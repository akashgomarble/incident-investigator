# Incident investigation report: INC-008
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
At ~06:48 a concentrated surge of login traffic hit api-gateway (1250->4900 req/s) and auth-service (400->1500 req/s, rate_limited 2->900/min). auth-service's fixed worker pool (64/64, queue up to ~98k) saturates, CPU pins at ~97%, and the autoscaler is blocked at maxReplicas 8 (wants 14), so it sheds traffic with 503 'accept queue full' on /v1/auth/login; p99 rises to ~3.8s and 5xx to ~14%. Dependencies (user-db, redis-cache) and other services are healthy and flat, and there is no auth-service deployment or config change, so the reported bcrypt-regression theory (matching HIST-101) lacks its trigger and is rejected. Root cause is capacity saturation at auth-service under concentrated login load, not a code defect. Confidence 0.68: the mechanism is well-supported, but I could not definitively separate organic demand growth from a retry/malicious flood driving the surge.

## Probable root cause
**auth-service worker-pool/capacity saturation under a concentrated login-traffic surge** -- capacity_saturation in `auth-service` (confidence 0.68, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: auth-service worker-pool/capacity saturation under a concentrated login-traffic surge | capacity_saturation | auth-service | - | 0.68 (medium) | active |
| 2 | H2: bcrypt-cost regression re-introduced in auth-service (per HIST-101) | bad_deployment | auth-service | - | 0.12 (low) | rejected |
| 3 | H3: Dependency failure (user-db / redis-cache / kyc-provider) degrading auth-service | dependency_failure | user-db | - | 0.05 (low) | rejected |

### H1: auth-service worker-pool/capacity saturation under a concentrated login-traffic surge
A large, concentrated surge of login requests through api-gateway drove auth-service's fixed worker pool (64 workers) and replica count (capped at maxReplicas 8) beyond provisioned capacity around 06:48. CPU saturated to ~97%, the accept queue backed up (queue ~90k), and the service returned 503 'accept queue full' on /v1/auth/login, producing the ~4s p99. The fault originates as a capacity ceiling, not a code defect.

**Causal chain**
1. Concentrated login traffic reaches api-gateway, request_rate 1250->4900 req/s at 06:48 [F2]
2. auth-service request_rate rises 400->1500 req/s and CPU saturates to ~97% [F3, F4]
3. Fixed worker pool (64/64) saturates and accept queue backs up to ~90k [F5]
4. Autoscaler blocked at maxReplicas 8, cannot add replicas [F6]
5. auth-service sheds load with 503 accept-queue-full, p99 ->3.8s, 5xx ->14% [F1, F5, F15]

**Supporting evidence**
  - `F1` ✔ auth-service latency p99 was ~89-95ms through 06:36, then jumped to 1921ms avg / 4012ms max in the 06:48 bucket, staying ~3.8s for the rest of the window.
  - `F2` ✔ api-gateway request_rate was ~1250 req/s through 06:36, then rose to 2846 (max 4859) at 06:48 and ~4900 req/s afterwards.
  - `F3` ✔ auth-service request_rate was ~392-400 req/s through 06:36, then rose to 962 (max 1813) at 06:48 and ~1500 req/s afterwards.
  - `F4` ✔ auth-service CPU rose from ~38% to ~97% at the same onset and stayed saturated.
  - `F5` ✔ auth-service logs show worker pool saturation (active=64/64) with queue depths up to ~98,763 and 503s 'accept queue full' on POST /v1/auth/login, first worker-saturation at 06:54:53 and first 503 at 06:55:42.
  - `F6` ✔ auth-service autoscaler reported it wanted 14 replicas but was blocked by maxReplicas 8.
  - `F7` ✔ auth-service rate_limited_per_min was ~2/min through 06:36, then exploded to avg 365 (max 919) at 06:48 and ~900/min afterwards.
  - `F8` ✔ No auth-service deployment exists in the 7 days before the incident (2026-04-15 to 09:00).
  - `F9` ✔ No api-gateway deployment exists in the 7 days before the incident.
  - `F11` ✔ portfolio-service, pricing-engine and market-data-service request rates were flat across the onset (no surge).
  - `F12` ✔ api-gateway CPU stayed flat at ~42% even as its request_rate tripled to ~4900 req/s.
  - `F13` ✔ user-db connections_active rose from ~25 to ~80 with login load but slow_queries_per_min stayed flat ~0.3/min; redis-cache hit_ratio (~96%) and CPU (~22%) and connected_clients (~310) were flat.
  - `F14` ✔ auth-service memory stayed flat ~52% and restarts stayed at 0 throughout.
  - `F15` ✔ auth-service error_rate (5xx) rose from ~0.2% to ~14% and api-gateway error_rate from ~0.2% to ~9%, both at the 06:48 onset.
**Contradicting evidence**
  - `F16` ✔ Historical incident HIST-101 shows the identical symptom cluster (login p99 4s, 503, high cpu) caused by a bcrypt cost factor raised 10->14 in auth-service release v6.4.0.
**Assumptions**
  - The gateway request surge reflects real inbound login attempts reaching auth-service (vs. gateway-internal fan-out).
  - The provisioned ceiling (64 workers, maxReplicas 8) was the binding constraint rather than any single dependency.

### H2: bcrypt-cost regression re-introduced in auth-service (per HIST-101)
A code/config change to auth-service raised password-hash cost (as in HIST-101), making each login CPU-expensive and saturating the service even at normal load.

**Causal chain**
1. bcrypt cost raised -> per-login CPU up [F16]
2. auth-service CPU saturates at ~97% [F4]
3. worker pool saturates, 503s [F5]

**Supporting evidence**
  - `F16` ✔ Historical incident HIST-101 shows the identical symptom cluster (login p99 4s, 503, high cpu) caused by a bcrypt cost factor raised 10->14 in auth-service release v6.4.0.
  - `F1` ✔ auth-service latency p99 was ~89-95ms through 06:36, then jumped to 1921ms avg / 4012ms max in the 06:48 bucket, staying ~3.8s for the rest of the window.
  - `F4` ✔ auth-service CPU rose from ~38% to ~97% at the same onset and stayed saturated.
**Contradicting evidence**
  - `F8` ✔ No auth-service deployment exists in the 7 days before the incident (2026-04-15 to 09:00).
  - `F7` ✔ auth-service rate_limited_per_min was ~2/min through 06:36, then exploded to avg 365 (max 919) at 06:48 and ~900/min afterwards.
  - `F3` ✔ auth-service request_rate was ~392-400 req/s through 06:36, then rose to 962 (max 1813) at 06:48 and ~1500 req/s afterwards.
  - `F11` ✔ portfolio-service, pricing-engine and market-data-service request rates were flat across the onset (no surge).
  - `F17` ✔ auth-service reported 'config reloaded (no changes)' at 06:49 and 06:57, i.e. no effective config change at onset.
  - `F2` ✔ api-gateway request_rate was ~1250 req/s through 06:36, then rose to 2846 (max 4859) at 06:48 and ~4900 req/s afterwards.
**Assumptions**
  - A change could have been applied without a recorded deployment or config-change entry.
**Rejected because:** No auth-service deployment in the prior 7 days (F8), no config change recorded (F17), and request_rate tripled (F3) which a pure cost regression would not cause; the symptom indicates load growth, not per-request cost.

### H3: Dependency failure (user-db / redis-cache / kyc-provider) degrading auth-service
Slowness in a downstream datastore or external provider made login requests slow, backing up the worker pool.

**Causal chain**
1. user-db/redis slows -> auth requests slow *(assumed)*
2. worker pool backs up [F5]

**Supporting evidence**
  - `F13` ✔ user-db connections_active rose from ~25 to ~80 with login load but slow_queries_per_min stayed flat ~0.3/min; redis-cache hit_ratio (~96%) and CPU (~22%) and connected_clients (~310) were flat.
**Contradicting evidence**
  - `F13` ✔ user-db connections_active rose from ~25 to ~80 with login load but slow_queries_per_min stayed flat ~0.3/min; redis-cache hit_ratio (~96%) and CPU (~22%) and connected_clients (~310) were flat.
  - `F1` ✔ auth-service latency p99 was ~89-95ms through 06:36, then jumped to 1921ms avg / 4012ms max in the 06:48 bucket, staying ~3.8s for the rest of the window.
**Assumptions**
  - Dependency latency could degrade without appearing in its own metrics.
**Rejected because:** user-db slow_queries flat ~0.3/min, redis hit_ratio ~96% and CPU ~22%, all flat at onset (F13); dependencies show no degradation.

## Facts (verified observations)
- auth-service p99 latency jumped from ~90ms to ~3.8s beginning in the 06:48 bucket. [F1]
- api-gateway request_rate tripled (1250->~4900 req/s) at 06:48 while its CPU stayed flat at ~42%. [F2, F12]
- auth-service request_rate rose from ~400 to ~1500 req/s and CPU saturated to ~97% at the same onset. [F3, F4]
- auth-service worker pool saturated (64/64, queue up to ~98k) and returned 503 'accept queue full' on /v1/auth/login. [F5]
- auth-service autoscaler was blocked: wanted 14 replicas, maxReplicas is 8. [F6]
- auth-service rate limiting exploded from ~2/min to ~900/min. [F7]
- No auth-service or api-gateway deployment occurred in the prior 7 days; only portfolio-service v4.4.0 deployed in-window and no config changes were recorded. [F8, F9, F10]
- portfolio-service, pricing-engine and market-data-service traffic was flat across the onset, so this was not a system-wide traffic surge. [F11]
- user-db and redis-cache showed no degradation (slow_queries flat ~0.3/min, redis hit_ratio ~96%, CPU ~22%). [F13]
- auth-service memory stayed flat ~52% and restarts stayed 0, ruling out OOM/restart churn. [F14]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-04-22T05:50:00Z` portfolio-service v4.4.0 (CSV export) deployed; its traffic stays flat and it is not on the affected path. [F10, F11]
- `2026-04-22T06:36:00Z` Baseline: auth-service ~400 req/s, p99 ~90ms, CPU ~38%; gateway ~1250 req/s, CPU ~42%. [F1, F2, F3, F4]
- `2026-04-22T06:48:00Z` Onset: gateway request_rate -> ~2850 (max 4859); auth request_rate -> 962 (max 1813); auth CPU -> 77-97%; auth rate_limited -> 366/min; auth p99 -> 1921ms. [F2, F3, F4, F7, F1]
- `2026-04-22T06:49:31Z` auth-service logs 'config reloaded (no changes)'. [F17]
- `2026-04-22T06:54:53Z` First auth-service worker pool saturation (active=64/64, queue=39035). [F5]
- `2026-04-22T06:55:42Z` First 503 'accept queue full' on POST /v1/auth/login. [F5]
- `2026-04-22T06:57:15Z` Autoscaler blocked: desired 14 replicas exceeds maxReplicas 8. [F6]
- `2026-04-22T07:00:00Z` Sustained state: auth ~1500 req/s, CPU ~97%, 5xx ~14%, rate_limited ~900/min; gateway ~4900 req/s, 5xx ~9%. [F3, F4, F15, F7, F2]
- `2026-04-22T08:59:00Z` Latest data: conditions unchanged; incident still ongoing. [F1, F5]

## Recommended actions
- **[immediate]** Immediately raise auth-service replica ceiling above maxReplicas 8 (it wants 14) and/or increase worker pool size to restore capacity. -- Autoscaler is blocked by maxReplicas 8 while CPU is pinned at ~97% and the accept queue overflows. [F6, F4, F5]
- **[immediate]** Identify and throttle/block the source of the login surge at api-gateway (per-client/IP rate limits, bot mitigation) since only the gateway->auth path surged. -- Gateway traffic tripled while its CPU stayed flat at 42%, indicating a concentrated flood, not general load; other services were flat. [F2, F12, F11]
- **[short_term]** Verify no unmerged auth-service change (bcrypt cost / config) by diffing running config against last known-good, to definitively rule out a silent regression. -- Reported symptoms match HIST-101, but no deployment/config change is recorded; confirm before ruling out. [F16, F8, F17]
- **[long_term]** Add admission control / load shedding ahead of auth-service so gateway traffic spikes cannot overflow its accept queue, and pre-scale auth-service ahead of known peak periods. -- Capacity was outgrown with autoscaling capped; protective controls would prevent 503 storms. [F5, F6]

## Data gaps
- Could not identify the origin of the login surge: api-gateway logs are sparse (no per-client/IP or upstream traffic-source detail; 'surge' and 'login' keyword queries returned 0 rows), so organic demand growth vs. retry/bot flood cannot be separated.
- kyc-provider (external, has_telemetry=false) latency is unobservable, so a subtle external-provider contribution to login latency cannot be fully excluded.
- auth-service log queries were truncated (true) at 40 rows per window, so the full event distribution was sampled rather than exhausted.

## Investigation stats
- latency 76.41s, LLM calls 9, tokens 163821 (in 152033 / out 11788)
- tool calls 45 (3 failed, 0 partial, 0 recovered by retry), findings 17/17 verified
