# Incident investigation report: INC-002
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Portfolio page timeouts originate in portfolio-service, not api-gateway. At 09:52:13 infra-automation applied config change cfg-2207 reducing portfolio-service's JDBC pool max_size from 50 to 5 under a cost-optimisation profile. Steady-state pool demand was ~12 connections (db_pool_in_use avg ~12 before the change), so the pool saturated immediately: db_pool_in_use flatlined at exactly 5.0, HikariPool logged "total=5, active=5, idle=0, waiting=43..57", and /v1/portfolio/holdings requests failed to acquire a connection. api-gateway's 504s on /v1/portfolio/* are a downstream symptom of upstream timeout. The gateway envoy/HTTP-2 upgrade (dep-5220) deployed at 08:00 caused no symptoms for ~1h50m; steady request rates and healthy ledger-db rule out load and DB faults. The report's suspicion of the gateway upgrade is not supported.

## Probable root cause
**portfolio-service JDBC pool shrunk to 5 by config change cfg-2207** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.92, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service JDBC pool shrunk to 5 by config change cfg-2207 | config_change | portfolio-service | cfg-2207 | 0.92 (high) | active |
| 2 | H2: api-gateway envoy/HTTP-2 upgrade (dep-5220) introduced the fault | bad_deployment | api-gateway | dep-5220 | 0.05 (low) | rejected |
| 3 | H3: ledger-db / downstream datastore failure or capacity issue | infrastructure_failure | ledger-db | - | 0.02 (low) | rejected |

### H1: portfolio-service JDBC pool shrunk to 5 by config change cfg-2207
An automated cost-optimisation config change (cfg-2207) reduced portfolio-service's HikariCP datasource pool max_size from 50 to 5 at 09:52. Steady-state demand (~12 connections) exceeds 5, so the pool saturated, /v1/portfolio/holdings blocked and timed out after 3000ms, and api-gateway returned 504s on /v1/portfolio/*. api-gateway is only the symptom holder.

**Causal chain**
1. cfg-2207 sets portfolio-service db.pool.max_size 50 -> 5 [F3, F4]
2. pool demand ~12 > new cap 5, so db_pool_in_use pins at 5 and HikariPool exhausts (total=5, active=5, idle=0, waiting=43) [F6, F5]
3. connection acquisition blocks 3000ms then fails on /v1/portfolio/holdings [F5, F4]
4. portfolio-service latency/errors rise at 09:50 bucket, gateway p99 and 504s follow [F2, F1, F8]

**Supporting evidence**
  - `F2` ✔ portfolio-service latency p99 and error_rate jumped at the same 09:50 bucket (p99 ~3031ms, error_rate 13.5%), matching the gateway onset — the upstream, not the gateway, is where the latency originates.
  - `F3` ✔ Config change cfg-2207 on portfolio-service changed db.pool.max_size from 50 to 5 at 09:52, authored by infra-automation, described as applying cost-optimisation profile 'small'.
  - `F4` ✔ portfolio-service log confirms the change applied at 09:52:13: 'config change cfg-2207 applied: db.pool.max_size=5', and the last healthy line before it was 'served batch of requests, p50=221ms' at 09:48:12.
  - `F5` ✔ portfolio-service HikariPool logged repeated 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' from 09:54:29, and 77 occurrences of 'GET /v<n>/portfolio/holdings failed: could not acquire JDBC connection within <n>ms' starting 09:53:19.
  - `F6` ✔ portfolio-service db_pool_in_use averaged ~12 connections before the change and then dropped to and held exactly 5.0 from the 10:00 bucket onward, matching the new max_size=5 cap.
  - `F8` ✔ api-gateway logs show only 504 'upstream timeout portfolio-service GET /v1/portfolio/holdings' warnings (first 09:53:53), i.e. it is waiting on portfolio-service — the failure message does not fit an envoy/HTTP-2-internal fault.
  - `F9` ✔ Request rates were flat across the incident (api-gateway ~1250 req/s, portfolio-service ~565 req/s throughout), so a traffic surge did not cause the saturation.
  - `F10` ✔ ledger-db (portfolio-service's DB) was healthy throughout: cpu_pct flat ~30% and connections_active ~55-66 with no spike or drop at onset.
  - `F11` ✔ Historical incident HIST-115 is a directly analogous case: order-service datasource pool size overridden by a wrong environment profile, causing 'connection is not available'/'pool exhausted', resolved by reverting config.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 'small' profile meant for a non-prod/smaller service was misapplied to production portfolio-service
  - No other change at 09:52 masked or contributed to the saturation

### H2: api-gateway envoy/HTTP-2 upgrade (dep-5220) introduced the fault
The gateway upgrade dep-5220 (envoy 1.29->1.30, HTTP/2 to upstreams) at 08:00 broke upstream communication with portfolio-service, producing 504s and upstream timeouts.

**Causal chain**
1. dep-5220 -> upstream resets -> 504 at gateway [F7]

**Supporting evidence**
  - `F7` ✔ The single gateway deployment dep-5220 (v9.3.0, envoy 1.29->1.30, 'upstream protocol h2 for portfolio-service and order-service') landed at 08:00, ~1h50m before the 09:50 onset, and was never rolled back.
  - `F12` ✔ Historical incident HIST-116 (gateway envoy upgrade causing 504s) exists as a lead, but that class of fault would produce upstream resets at deploy time, not pool-exhaustion logs inside portfolio-service.
**Contradicting evidence**
  - `F1` ✔ api-gateway p99 latency jumped from ~207ms (09:40 bucket) to 3119ms avg at 09:50, then ~4300ms sustained; error_rate rose from 0.23% to 4.44% at 09:50 then ~7.2%.
  - `F3` ✔ Config change cfg-2207 on portfolio-service changed db.pool.max_size from 50 to 5 at 09:52, authored by infra-automation, described as applying cost-optimisation profile 'small'.
  - `F4` ✔ portfolio-service log confirms the change applied at 09:52:13: 'config change cfg-2207 applied: db.pool.max_size=5', and the last healthy line before it was 'served batch of requests, p50=221ms' at 09:48:12.
  - `F5` ✔ portfolio-service HikariPool logged repeated 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' from 09:54:29, and 77 occurrences of 'GET /v<n>/portfolio/holdings failed: could not acquire JDBC connection within <n>ms' starting 09:53:19.
  - `F6` ✔ portfolio-service db_pool_in_use averaged ~12 connections before the change and then dropped to and held exactly 5.0 from the 10:00 bucket onward, matching the new max_size=5 cap.
  - `F8` ✔ api-gateway logs show only 504 'upstream timeout portfolio-service GET /v1/portfolio/holdings' warnings (first 09:53:53), i.e. it is waiting on portfolio-service — the failure message does not fit an envoy/HTTP-2-internal fault.
  - `F9` ✔ Request rates were flat across the incident (api-gateway ~1250 req/s, portfolio-service ~565 req/s throughout), so a traffic surge did not cause the saturation.
**Rejected because:** The deploy preceded the 09:50 onset by ~1h50m with a fully healthy period in between, was never rolled back, and the concrete failure evidence (HikariPool exhaustion with total=5) lives inside portfolio-service and post-dates the 09:52 pool config change. Gateway logs show only upstream-timeout 504s, not HTTP/2 stream errors.

### H3: ledger-db / downstream datastore failure or capacity issue
The shared ledger-db degraded or ran out of capacity, causing portfolio-service connection waits and 504s.

**Causal chain**
1. ledger-db slow -> portfolio waits *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ ledger-db (portfolio-service's DB) was healthy throughout: cpu_pct flat ~30% and connections_active ~55-66 with no spike or drop at onset.
  - `F5` ✔ portfolio-service HikariPool logged repeated 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' from 09:54:29, and 77 occurrences of 'GET /v<n>/portfolio/holdings failed: could not acquire JDBC connection within <n>ms' starting 09:53:19.
  - `F6` ✔ portfolio-service db_pool_in_use averaged ~12 connections before the change and then dropped to and held exactly 5.0 from the 10:00 bucket onward, matching the new max_size=5 cap.
**Rejected because:** ledger-db cpu_pct (~30%) and connections_active (~55-66) were flat and healthy across onset. The failure mode is client-side pool exhaustion (total=5), not server-side slowness; failed acquisition was a pool limit, not a DB refusal.

## Facts (verified observations)
- api-gateway p99 rose sharply in the 09:50 bucket and stayed elevated; error_rate rose in the same bucket. [F1]
- portfolio-service latency and error rate rose in the same 09:50 bucket, indicating the fault is in the upstream service. [F2]
- Config change cfg-2207 reduced portfolio-service db.pool.max_size from 50 to 5 at 09:52. [F3]
- portfolio-service applied cfg-2207 at 09:52:13 after serving healthy traffic (p50=221ms) at 09:48. [F4]
- portfolio-service HikariPool exhausted with total=5, active=5, idle=0 and dozens waiting, and /v1/portfolio/holdings failed to acquire connections. [F5]
- portfolio-service db_pool_in_use was ~12 before and pinned at exactly 5.0 after the change. [F6]
- The gateway deploy dep-5220 occurred at 08:00 (~1h50m before onset) and was not rolled back. [F7]
- api-gateway logged only upstream-timeout 504s toward portfolio-service. [F8]
- Request rates were flat across both services during the incident. [F9]
- ledger-db CPU and active connections were healthy and unchanged at onset. [F10]
- A prior incident (HIST-115) had the same pattern: a datasource pool size overridden by a wrong profile. [F11]

## Assumptions (not verified)
- The cost-optimisation profile 'small' was intended for a smaller/non-prod workload and was misapplied to production portfolio-service.
- No unobserved change occurred at 09:52 that contributed to the saturation.
- HikariPool total=5 in the error logs directly reflects the new db.pool.max_size=5 setting.

## Timeline
- `2026-03-10T08:00:00Z` api-gateway deploy dep-5220 (envoy 1.30, HTTP/2 upstreams) goes live; no symptoms follow. [F7]
- `2026-03-10T09:48:12Z` portfolio-service still serving normally (p50=221ms). [F4]
- `2026-03-10T09:50:00Z` Onset: gateway and portfolio-service latency/error buckets spike. [F1, F2]
- `2026-03-10T09:52:13Z` portfolio-service applies config change cfg-2207: db.pool.max_size=5. [F3, F4]
- `2026-03-10T09:53:19Z` First 'could not acquire JDBC connection within 3000ms' errors in portfolio-service. [F5]
- `2026-03-10T09:53:53Z` First gateway 504 'upstream timeout portfolio-service' warning. [F8]
- `2026-03-10T09:54:29Z` HikariPool logs total=5, active=5, idle=0, waiting=43. [F5]
- `2026-03-10T10:00:00Z` db_pool_in_use pinned at exactly 5.0; incident continues at current time. [F6]

## Recommended actions
- **[immediate]** Revert config change cfg-2207 to restore portfolio-service db.pool.max_size=50 -- The pool cap of 5 is below steady-state demand (~12); restoring 50 removes the saturation immediately. [F3, F6]
- **[short_term]** Add a guardrail preventing the cost-optimisation 'small' profile from being applied to production portfolio-service, and validate pool size against observed demand before applying -- cfg-2207 was applied automatically by infra-automation with no demand check; the same class of fault recurred in HIST-115. [F3, F11]
- **[short_term]** Do not roll back gateway dep-5220 based on this incident; treat gateway 504s as downstream symptom -- Gateway deploy preceded onset by ~2h with no symptoms; its errors are upstream timeouts toward portfolio-service. [F7, F8]
- **[short_term]** Add alerting on JDBC pool saturation (db_pool_in_use near max_size, waiting count > 0) for all services -- The pool pinned at its cap silently; earlier alerting would have caught the misconfiguration before user impact. [F5, F6]

## Data gaps
- No telemetry from bank-partner-api / kyc-provider (external, has_telemetry=false) — not relevant here but unavailable.
- portfolio-service WARN log window (09:30-10:30) was truncated (returned 30 of 148); later errors were confirmed via targeted INFO query but the full 10:30-11:59 error tail was not enumerated.
- No change-management metadata explaining why infra-automation selected the 'small' profile for portfolio-service.
- db_pool_in_use reflects used connections, not configured max directly, but its pin at exactly 5.0 plus HikariPool total=5 corroborate the cap.

## Investigation stats
- latency 46.99s, LLM calls 5, tokens 71145 (in 63999 / out 7146)
- tool calls 18 (0 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
