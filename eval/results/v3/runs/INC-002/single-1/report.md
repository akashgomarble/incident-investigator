# Incident investigation report: INC-002
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The reported api-gateway symptom is a downstream effect. Onset was ~09:45-09:53Z. Config change cfg-2207 (infra-automation, cost-optimisation profile 'small') reduced portfolio-service db.pool.max_size from 50 to 5 at 09:52Z. Normal steady load (~560 req/s, ~12 needed concurrent connections) immediately exceeded the 5-connection ceiling; the HikariPool saturated (total=5, active=5, idle=0, waiting up to 57), request acquisition timed out at 3000ms, first error 09:53:19Z. portfolio-service p99 rose to ~4.2s and 5xx to ~22%, and api-gateway then returned 504 'upstream timeout portfolio-service' with p99 >4s. The human-suspected gateway HTTP/2 deploy (dep-5220, 08:00Z) is contradicted: onset was ~1h45m later after flat latency, and order-service received the same h2 change with no impact. Root cause is a config_change originating in portfolio-service.

## Probable root cause
**Cost-optimisation config shrank portfolio-service DB pool to 5, causing pool exhaustion** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Cost-optimisation config shrank portfolio-service DB pool to 5, causing pool exhaustion | config_change | portfolio-service | cfg-2207 | 0.90 (high) | active |
| 2 | H2: Gateway HTTP/2 upgrade (dep-5220) to upstreams caused the outage | bad_deployment | api-gateway | dep-5220 | 0.05 (low) | rejected |
| 3 | H3: Shared ledger-db / infrastructure degradation | infrastructure_failure | ledger-db | - | 0.03 (low) | rejected |

### H1: Cost-optimisation config shrank portfolio-service DB pool to 5, causing pool exhaustion
Config change cfg-2207 (infra-automation cost-optimisation profile 'small') reduced portfolio-service db.pool.max_size from 50 to 5 at 09:52Z. Normal steady load of ~560 req/s needs ~12 concurrent connections; with only 5 the HikariPool saturates (active=5, idle=0, waiting 25-57), requests block up to the 3000ms acquisition timeout, portfolio-service p99 rises to ~4.2s and 500s climb to ~22%, and api-gateway times out upstream returning 504s on /v1/portfolio/*.

**Causal chain**
1. cfg-2207 sets db.pool.max_size 50 -> 5 at 09:52Z [F5]
2. pool ceiling now 5 while peak requirement was ~12 in-use [F6]
3. connections exhaust: total=5 active=5 idle=0, waiters queue and hit 3000ms timeout [F3, F4]
4. portfolio-service p99 and 5xx surge [F1, F7]
5. gateway upstream timeouts -> 504 on portfolio routes, gateway p99 up [F8, F2]

**Supporting evidence**
  - `F5` ✔ Config change cfg-2207 on portfolio-service set db.pool.max_size from 50 to 5 at 09:52:00Z, described as applying a cost-optimisation profile.
  - `F6` ✔ portfolio-service db_pool_in_use was ~12 before the change and pinned exactly at 5.0 from 10:00 onward (the new pool ceiling).
  - `F3` ✔ portfolio-service Hikari connection pool is exhausted at exactly 5 connections: 'total=5, active=5, idle=0' with dozens of waiters.
  - `F4` ✔ First portfolio-service JDBC acquisition failure occurred at 09:53:19Z: 'could not acquire JDBC connection within 3000ms'.
  - `F1` ✔ portfolio-service latency p99 jumped from ~180ms pre-09:30 to ~4200ms avg from 09:45 onward.
  - `F7` ✔ portfolio-service error_rate rose from ~0.2% to ~22% (5xx) starting in the 09:45 bucket.
  - `F8` ✔ api-gateway 504s are specifically 'upstream timeout portfolio-service GET /v1/portfolio/holdings', i.e. the gateway fails while waiting on portfolio-service.
  - `F13` ✔ The onset of pool exhaustion (first JDBC error 09:53:19Z) follows the pool-shrink config change (09:52:00Z) by ~1 minute, far tighter than the 08:00 deploy.
  - `F11` ✔ portfolio-service request rate stayed steady at ~560 req/s before and during the incident, and CPU stayed ~36% — no traffic surge or CPU pressure.
  - `F12` ✔ portfolio-service had zero restarts throughout the window, ruling out a crash/restart loop.
**Contradicting evidence**
  - (none)
**Assumptions**
  - No other concurrent change to portfolio-service or ledger-db
  - The 'small' profile's max_size=5 matches the observed total=5 pool ceiling
  - Baseline ~12 in-use is representative of normal demand

### H2: Gateway HTTP/2 upgrade (dep-5220) to upstreams caused the outage
The 08:00Z api-gateway envoy upgrade enabling HTTP/2 to upstreams introduced a fault that stalls portfolio-service calls, driving gateway p99 >4s and 504s.

**Causal chain**
1. dep-5220 deploys at 08:00Z [F9]
2. gateway/portfolio latency rises [F1, F2]

**Supporting evidence**
  - `F9` ✘ unverified The gateway HTTP/2 deploy (dep-5220) landed at 08:00:00Z, ~1h45m before onset, and applied to both portfolio-service and order-service.
**Contradicting evidence**
  - `F10` ✔ order-service, which also received the gateway HTTP/2 upstream change, shows flat p99 (~150ms) throughout the incident window.
  - `F13` ✔ The onset of pool exhaustion (first JDBC error 09:53:19Z) follows the pool-shrink config change (09:52:00Z) by ~1 minute, far tighter than the 08:00 deploy.
  - `F3` ✔ portfolio-service Hikari connection pool is exhausted at exactly 5 connections: 'total=5, active=5, idle=0' with dozens of waiters.
  - `F5` ✔ Config change cfg-2207 on portfolio-service set db.pool.max_size from 50 to 5 at 09:52:00Z, described as applying a cost-optimisation profile.
**Assumptions**
  - HTTP/2 could selectively affect only portfolio paths
**Rejected because:** Onset was ~1h45m after the deploy with completely flat latency in between (F9 vs F1); order-service received the same h2 change but shows zero impact (F10); the errors are explicit DB connection-pool exhaustion in portfolio-service (F3) tied to a config change 1 minute before onset (F5/F13). And in a separate check I searched order-service and it showed no latency impact. The gateway is a downstream victim, not the origin.

### H3: Shared ledger-db / infrastructure degradation
A ledger-db (shared datastore) slowdown or failover caused portfolio-service query slowness and timeouts.

**Causal chain**
1. ledger-db degrades *(assumed)*
2. portfolio-service queries slow [F1]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F3` ✔ portfolio-service Hikari connection pool is exhausted at exactly 5 connections: 'total=5, active=5, idle=0' with dozens of waiters.
  - `F6` ✔ portfolio-service db_pool_in_use was ~12 before the change and pinned exactly at 5.0 from 10:00 onward (the new pool ceiling).
  - `F12` ✔ portfolio-service had zero restarts throughout the window, ruling out a crash/restart loop.
**Rejected because:** Error messages are client-side pool acquisition failures (total=5), not DB-side errors/timeouts; the pool in-use value is clamped at exactly 5 matching the config, and portfolio had no restarts. A DB-side problem would not produce a pool ceiling of exactly 5.

## Facts (verified observations)
- portfolio-service db.pool.max_size was changed from 50 to 5 at 09:52:00Z by cfg-2207 (cost-optimisation profile). [F5]
- The first JDBC acquisition failure in portfolio-service was at 09:53:19Z, ~1 minute after the config change. [F4]
- portfolio-service HikariPool is exhausted at total=5, active=5, idle=0 with many waiters. [F3]
- portfolio-service db_pool_in_use is pinned at exactly 5.0 from 10:00 onward vs ~12 before. [F6]
- portfolio-service p99 jumped to ~4.2s at 09:45 and error_rate to ~22%. [F1, F7]
- api-gateway p99 jumped to ~4.3s and emits 504 'upstream timeout portfolio-service' on /v1/portfolio/holdings. [F2, F8]
- order-service, which also got the HTTP/2 change, shows no latency impact. [F10]
- portfolio-service request rate (~560 req/s) and CPU (~36%) were steady, and restarts were zero. [F11, F12]

## Assumptions (not verified)
- No other concurrent change to portfolio-service or ledger-db occurred around 09:52Z.
- Baseline db_pool_in_use ~12 is representative of normal portfolio-service demand.
- The observed HikariPool total=5 is the direct result of cfg-2207's max_size=5.
- (demoted from facts: no verified observation backs it) The gateway HTTP/2 deploy was at 08:00Z, ~1h45m before onset, and also covered order-service.

## Timeline
- `2026-03-10T08:00:00Z` api-gateway deploy dep-5220 (envoy 1.29->1.30, HTTP/2 to portfolio-service and order-service). [F9]
- `2026-03-10T09:45:00Z` portfolio-service and api-gateway latency begin rising (p99 into seconds). [F1, F2]
- `2026-03-10T09:52:00Z` cfg-2207 applies cost-optimisation profile: portfolio-service db.pool.max_size 50 -> 5. [F5]
- `2026-03-10T09:53:19Z` First portfolio-service error: 'could not acquire JDBC connection within 3000ms'. [F4]
- `2026-03-10T09:53:53Z` api-gateway first 504 'upstream timeout portfolio-service GET /v1/portfolio/holdings'. [F8]
- `2026-03-10T09:54:29Z` HikariPool exhaustion errors: total=5, active=5, idle=0, waiting=43. [F3]
- `2026-03-10T10:00:00Z` db_pool_in_use pinned at 5.0; error_rate ~22% steady; incident ongoing to current time. [F6, F7]

## Recommended actions
- **[immediate]** Revert cfg-2207: restore portfolio-service db.pool.max_size from 5 back to 50 (or appropriate value). -- Pool ceiling of 5 directly matches the observed exhaustion (total=5, active=5, idle=0); restoring capacity should relieve the incident. [F5, F3, F6]
- **[immediate]** Do NOT prioritize rolling back gateway dep-5220; it is not the cause. -- Onset lag, unaffected order-service, and the DB pool errors show the gateway deploy is unrelated. [F9, F10, F13]
- **[short_term]** Add guardrails/validation to infra-automation cost-optimisation profiles so pool sizes cannot be reduced below measured peak demand. -- Prevent recurrence of the same class of config regression (cf. HIST-115). [F5]
- **[long_term]** Add alerting on HikariPool active/total saturation and db pool wait time so exhaustion is detected before user-visible 504s. -- The pool saturated at 09:53 but the user-facing incident was noticed later; earlier detection shortens MTTR. [F3, F4]

## Data gaps
- No ledger-db telemetry was queried directly; a DB-side contribution cannot be fully excluded, though client-side pool errors and the exact 5-connection ceiling strongly argue against it.
- Only portfolio-service and order-service config/deploys were inspected in detail; a change on another shared component (ledger-db, redis-cache) was not exhaustively ruled out.
- The exact mapping of the 'small' cost-optimisation profile values was not independently confirmed against cfg-2207 beyond max_size=5.

## Integrity warnings (set by code)
- fact demoted to assumption: The gateway HTTP/2 deploy was at 08:00Z, ~1h45m before onset, and also covered o

## Investigation stats
- latency 41.21s, LLM calls 5, tokens 62202 (in 55213 / out 6989)
- tool calls 19 (0 failed, 0 partial, 0 recovered by retry), findings 12/13 verified
