# Incident investigation report: INC-002
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
A cost-optimisation config change to portfolio-service (cfg-2207) shrank its HikariCP JDBC pool from 50 to 5 connections at 09:52:13. With steady request load (~565 req/s, unchanged), the 5-connection pool immediately saturated (total=5, active=5, idle=0, 25-57 threads waiting). GET /v1/portfolio/holdings began failing at 09:53:19 with 'could not acquire JDBC connection', calls blocked ~3s and breached the gateway timeout, producing 504s at api-gateway and the observed p99 spike. The gateway deploy (dep-5220, HTTP/2 to upstreams, 08:00) is a red herring: it preceded onset by ~110 minutes; latency was normal all morning until 09:50; the gateway is not saturated (CPU ~42%); and order-service, which received the same HTTP/2 upstream change and shares ledger-db, is completely unaffected. The DB itself is healthy (CPU ~30%, slow queries flat, connections_active normal). Root cause: config_change in portfolio-service.

## Probable root cause
**portfolio-service JDBC pool shrunk to 5 by cost-optimisation config, saturating under normal load** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service JDBC pool shrunk to 5 by cost-optimisation config, saturating under normal load | config_change | portfolio-service | cfg-2207 | 0.90 (high) | active |
| 2 | H2: api-gateway dep-5220 HTTP/2-to-upstream upgrade caused the timeouts | bad_deployment | api-gateway | dep-5220 | 0.05 (low) | rejected |
| 3 | H3: ledger-db (shared database) degradation | infrastructure_failure | ledger-db | - | 0.03 (low) | rejected |
| 4 | H4: Capacity saturation from organic load growth | capacity_saturation | portfolio-service | - | 0.03 (low) | rejected |

### H1: portfolio-service JDBC pool shrunk to 5 by cost-optimisation config, saturating under normal load
Config change cfg-2207 set portfolio-service db.pool.max_size=5 (from 50) at 09:52:13. With unchanged demand (~565 req/s) the 5-connection Hikari pool instantly saturated (total=5, active=5, idle=0, waiting 25-57), so /v1/portfolio/holdings requests blocked ~3s on connection acquire and failed; upstream calls timed out at api-gateway, producing p99 >4s and 504s.

**Causal chain**
1. cfg-2207 sets db.pool.max_size 50->5 at 09:52:13 [F4, F5]
2. pool instantly capped at 5 -> db_pool_in_use flat 5.0 [F7]
3. normal request load saturates 5 connections -> 3s acquire timeouts [F6, F8]
4. portfolio-service errors spike (error_rate ~22%, p99 ~4.1s) [F9]
5. api-gateway upstream timeouts -> 504s, p99 ~4.3s [F10, F1]

**Supporting evidence**
  - `F4` ✔ Config change cfg-2207 changed portfolio-service db.pool.max_size from 50 to 5 at 09:52:00Z.
  - `F5` ✔ portfolio-service logged 'config change cfg-2207 applied: db.pool.max_size=5' at 09:52:13Z, then its first connection-acquire failure at 09:53:19Z.
  - `F6` ✔ portfolio-service errored with 'could not acquire JDBC connection' and HikariPool failures showing total=5, active=5, idle=0, waiting=25-57.
  - `F7` ✔ portfolio-service db_pool_in_use dropped from ~12 connections before to a flat 5.0 (avg and max) from 10:00 onward.
  - `F8` ✔ portfolio-service request_rate stayed flat at ~558-586 req/s across the onset (no load surge).
  - `F9` ✔ portfolio-service error_rate jumped from ~0.2% to 13.5% at 09:50 and ~22% thereafter; latency_p99 jumped from ~180ms to ~3031ms at 09:50.
  - `F10` ✔ api-gateway flagged the failures as upstream timeouts specifically to portfolio-service: 252 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' WARNs, first at 09:53:53Z.
  - `F15` ✔ A past incident HIST-115 has the same signature: 'order-service connection pool misconfiguration' caused by a datasource pool size overridden by a wrong environment profile, category config_change.
**Contradicting evidence**
  - (none)
**Assumptions**
  - No other portfolio-service change occurred at 09:52 (only cfg-2207 appears in the config-change feed for the window).
  - The 5-connection pool was genuinely insufficient for the steady ~565 req/s / ~12-connection working set.

### H2: api-gateway dep-5220 HTTP/2-to-upstream upgrade caused the timeouts
The api-gateway v9.3.0 deploy (dep-5220, HTTP/2 to portfolio-service/order-service upstreams, 08:00) broke or degraded upstream handling, causing 504s on /v1/portfolio/*.

**Causal chain**
1. dep-5220 enables h2 to portfolio-service at 08:00 [F3]
2. upstream requests time out at gateway [F10]

**Supporting evidence**
  - `F3` ✔ A single deployment dep-5220 (api-gateway v9.3.0) was deployed at 08:00:00Z enabling HTTP/2 to upstreams for portfolio-service and order-service; never rolled back.
  - `F10` ✔ api-gateway flagged the failures as upstream timeouts specifically to portfolio-service: 252 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' WARNs, first at 09:53:53Z.
**Contradicting evidence**
  - `F1` ✔ api-gateway p99 latency was normal (~205-214ms avg) from 08:00 through 09:40, then jumped to ~3120ms at 09:50 and ~4300ms from 10:00 onward.
  - `F11` ✔ order-service, which also received the HTTP/2 upstream change and shares ledger-db, was completely unaffected: p99 flat ~148-151ms throughout.
  - `F13` ✔ api-gateway CPU stayed flat at ~42% (37-47% range), i.e. the gateway itself was not resource-saturated.
  - `F12` ✔ ledger-db was healthy throughout: cpu_pct flat ~30%, slow_queries_per_min flat ~1, connections_active normal (~55-67).
  - `F8` ✔ portfolio-service request_rate stayed flat at ~558-586 req/s across the onset (no load surge).
  - `F7` ✔ portfolio-service db_pool_in_use dropped from ~12 connections before to a flat 5.0 (avg and max) from 10:00 onward.
  - `F6` ✔ portfolio-service errored with 'could not acquire JDBC connection' and HikariPool failures showing total=5, active=5, idle=0, waiting=25-57.
**Assumptions**
  - The gateway deploy could cause latent, delayed failures only for portfolio-service.
**Rejected because:** Timing and scope both refute it: onset is 09:50-09:53, ~110 min after the 08:00 deploy, with normal gateway latency 08:00-09:40 (F1). The gateway is not saturated (CPU ~42%, F13). order-service got the same h2 change (F3) and the same shared ledger-db and is unaffected (F11). The failures are localized to portfolio-service's own JDBC pool, whose cap changed exactly at onset (F4,F6,F7).

### H3: ledger-db (shared database) degradation
ledger-db slowed or refused connections, causing portfolio-service (and the gateway via it) to time out.

**Causal chain**
1. ledger-db degrades at 09:50 *(assumed)*
2. portfolio-service queries time out [F6]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F12` ✔ ledger-db was healthy throughout: cpu_pct flat ~30%, slow_queries_per_min flat ~1, connections_active normal (~55-67).
  - `F11` ✔ order-service, which also received the HTTP/2 upstream change and shares ledger-db, was completely unaffected: p99 flat ~148-151ms throughout.
**Rejected because:** ledger-db metrics are entirely flat/healthy (CPU ~30%, slow queries ~1/min, connections_active normal, F12) and order-service querying the same DB is unaffected (F11).

### H4: Capacity saturation from organic load growth
Traffic outgrew provisioned capacity, exhausting connections.

**Causal chain**
1. request load rises at onset *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ portfolio-service request_rate stayed flat at ~558-586 req/s across the onset (no load surge).
  - `F7` ✔ portfolio-service db_pool_in_use dropped from ~12 connections before to a flat 5.0 (avg and max) from 10:00 onward.
  - `F4` ✔ Config change cfg-2207 changed portfolio-service db.pool.max_size from 50 to 5 at 09:52:00Z.
**Rejected because:** request_rate is flat ~558-586 req/s across onset (F8); pool size was deliberately reduced 50->5 (F4), not outgrown.

## Facts (verified observations)
- api-gateway p99 latency was normal (~207ms) through 09:40, then spiked to ~3120ms at 09:50 and ~4300ms afterward. [F1]
- cfg-2207 changed portfolio-service db.pool.max_size from 50 to 5 at 09:52:00Z. [F4]
- portfolio-service logged 'config change cfg-2207 applied: db.pool.max_size=5' at 09:52:13Z, then its first connection-acquire failure 66s later at 09:53:19Z. [F5]
- portfolio-service Hikari pool sat at total=5, active=5, idle=0 with 25-57 threads waiting. [F6]
- portfolio-service db_pool_in_use fell from ~12 to a flat 5.0 after the change. [F7]
- portfolio-service request_rate was flat ~565 req/s across the onset, ruling out load growth. [F8]
- order-service, which received the same HTTP/2 upstream change and shares ledger-db, stayed flat at ~150ms p99. [F11]
- ledger-db was healthy (CPU ~30%, slow queries ~1/min, connections_active normal). [F12]
- api-gateway CPU stayed flat at ~42%, so the gateway itself was not saturated. [F13]
- portfolio-service's other deps (market-data-service ~40ms p99, redis-cache ~22% CPU) were healthy. [F14]
- A near-identical past incident (HIST-115) was a datasource pool size overridden by a wrong environment profile. [F15]

## Assumptions (not verified)
- The config-change feed for the window is complete (get_config_changes returned count=2 for all services, count=0 for portfolio-service after 11:00), so cfg-2207 is the only portfolio-service change near onset.
- The reported 'gateway upgrade' (dep-5220) is the change the reporter suspected, and it is the only api-gateway deployment in the window.
- portfolio-service's 3s acquire timeout is shorter than api-gateway's upstream timeout, so gateway-side 504s are a consequence rather than the origin.

## Timeline
- `2026-03-10T08:00:00Z` api-gateway v9.3.0 (dep-5220) deployed enabling HTTP/2 to portfolio-service and order-service upstreams; gateway latency stays normal. [F3, F1]
- `2026-03-10T09:40:00Z` Last normal bucket before onset (gateway p99 ~207ms, portfolio p99 ~180ms, error rates ~0.2%). [F1, F9]
- `2026-03-10T09:50:00Z` Onset: portfolio-service and api-gateway latency/error metrics step up sharply. [F1, F9]
- `2026-03-10T09:52:13Z` portfolio-service applies cfg-2207: db.pool.max_size=5. [F5, F4]
- `2026-03-10T09:53:19Z` First 'could not acquire JDBC connection within 3000ms' for GET /v1/portfolio/holdings. [F5, F6]
- `2026-03-10T09:53:53Z` First api-gateway 'upstream timeout portfolio-service ... -> 504'. [F10]
- `2026-03-10T10:00:00Z` Steady degraded state: portfolio db_pool_in_use pinned at 5.0; both services at elevated latency/error rates through 11:59. [F7, F9]

## Recommended actions
- **[immediate]** Revert cfg-2207: restore portfolio-service db.pool.max_size to 50 (or a validated value) immediately. -- Directly removes the fault; H1 attributes the outage to the 50->5 pool shrink at 09:52. [F4, F5, F7]
- **[immediate]** Confirm recovery by watching portfolio-service db_pool_in_use, error_rate and api-gateway p99/504s return to pre-09:50 baselines. -- Verifies the causal chain end-to-end. [F1, F7, F9]
- **[immediate]** Do NOT roll back dep-5220 on the strength of this incident; the gateway deploy is not the cause. -- Timing (110 min lead), gateway health, and unaffected order-service all exonerate the deploy. [F1, F11, F13]
- **[short_term]** Add guardrails/validation to the 'small' cost-optimisation profile so datasource pool sizes cannot be reduced below a service's observed concurrency. -- Matches the historical root cause of HIST-115 (pool overridden by wrong profile). [F15, F4]
- **[short_term]** Alert on config changes that reduce connection-pool max_size and on sustained Hikari 'waiting>0' / pool-in-use pinned at max. -- Would have flagged the 09:52 change within seconds rather than via user reports. [F6, F7]

## Data gaps
- No direct confirmation that reverting cfg-2207 restores service (not yet attempted in the data window) — causal chain is inferred from timing + exact pool-size coincidence, not a controlled rollback.
- Order-of-latency-blocks: 09:50 metric bucket shows a partial step before the 09:52:13 config apply, most consistent with 10-min bucket averaging straddling the onset, but exact second-level onset of the metric step is not resolvable from 10-min buckets.
- ledger-db has no latency_p99_ms metric, so DB responsiveness was assessed only via cpu_pct, slow_queries_per_min and connections_active.

## Investigation stats
- latency 67.97s, LLM calls 6, tokens 89923 (in 81055 / out 8868)
- tool calls 24 (1 failed, 0 partial, 0 recovered by retry), findings 16/16 verified
