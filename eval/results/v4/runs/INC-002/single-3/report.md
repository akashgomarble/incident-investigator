# Incident investigation report: INC-002
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The portfolio page timeouts were caused by a config change, not the gateway deploy. At 09:52Z infra-automation applied cfg-2207 to portfolio-service, shrinking db.pool.max_size from 50 to 5 ('cost-optimisation profile small'). Steady-state concurrent DB usage was ~12 connections, so the 5-slot HikariPool saturated immediately (total=5, active=5, idle=0, waiting=40+), and /v1/portfolio/holdings requests began failing with 'could not acquire JDBC connection within 3000ms'. This propagated to api-gateway as upstream timeouts/504s, raising gateway p99 from ~205ms to ~4.3s at 09:50Z. The reported api-gateway v9.3.0 deploy (dep-5220) went live at 08:00Z but had ~1h50m of zero latency impact before onset, and gateway CPU/error profile is consistent with downstream timeouts, so the deploy is ruled out as the trigger; ledger-db was healthy, ruling out the shared DB. Fix: revert db.pool.max_size to 50 (roll back cfg-2207).

## Probable root cause
**portfolio-service DB connection pool shrunk to 5 by cost-optimisation config cfg-2207** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service DB connection pool shrunk to 5 by cost-optimisation config cfg-2207 | config_change | portfolio-service | cfg-2207 | 0.90 (high) | active |
| 2 | H2: api-gateway v9.3.0 HTTP/2-to-upstream deploy dep-5220 caused the timeouts | bad_deployment | api-gateway | dep-5220 | 0.07 (low) | rejected |
| 3 | H3: ledger-db / infrastructure degradation behind portfolio-service | dependency_failure | ledger-db | - | 0.03 (low) | rejected |

### H1: portfolio-service DB connection pool shrunk to 5 by cost-optimisation config cfg-2207
At 09:52Z infra-automation applied config cfg-2207 to portfolio-service, reducing db.pool.max_size from 50 to 5. Steady-state demand was ~12 concurrent connections, so the 5-connection HikariPool saturated immediately (total=5, active=5, idle=0, waiting=40+). Requests to /v1/portfolio/holdings blocked up to the 3s acquire timeout, failing with 'could not acquire JDBC connection', which propagated to api-gateway as upstream timeouts and 504s, inflating gateway p99 to ~4.3s.

**Causal chain**
1. cfg-2207 sets db.pool.max_size 50->5 on portfolio-service [F1, F2]
2. pool_in_use was ~12, so 5 is undersized; pool pins at 5.0 [F3]
3. HikariPool exhaustion: total=5, active=5, idle=0, waiting=40+ [F4]
4. requests fail 'could not acquire JDBC connection within 3000ms' [F5]
5. portfolio p99 jumps ~180ms -> ~4.1s [F7]
6. gateway returns upstream timeout 504s and p99 rises to ~4.3s [F8, F6]

**Supporting evidence**
  - `F1` ✔ Config change cfg-2207 on portfolio-service reduced db.pool.max_size from 50 to 5 at 2026-03-10T09:52:00Z under a 'cost-optimisation profile small'.
  - `F2` ✔ portfolio-service logged the pool config being applied: 'config change cfg-2207 applied: db.pool.max_size=5' at 09:52:13Z.
  - `F3` ✔ portfolio-service db_pool_in_use was ~12 connections through 09:40, then collapsed to exactly 5.0 (saturated) from 10:00 onward.
  - `F4` ✔ portfolio-service emitted HikariPool exhaustion errors with the pool pinned at total=5, active=5, idle=0 and dozens of waiting requests, first at 09:54:29Z.
  - `F5` ✔ portfolio-service logged 'could not acquire JDBC connection within 3000ms' repeated 142 times starting 09:53:19Z.
  - `F6` ✔ api-gateway p99 latency jumped from ~210ms to ~3100ms in the 09:50 bucket and stayed ~4300ms; it was steady ~205ms for the preceding two hours.
  - `F7` ✔ portfolio-service latency_p99_ms shows the same onset: ~180ms through 09:40, then ~3031ms at 09:50 and ~4155ms thereafter.
  - `F8` ✔ api-gateway emitted 134 WARN 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' entries, first at 09:53:53Z.
  - `F11` ✔ portfolio-service request_rate was flat and stable ~550-575 req/s across the whole window, including at onset.
  - `F12` ✔ ledger-db slow_queries_per_min remained ~1/min and connections_active stable ~55-62 through the window, showing the shared DB was healthy.
  - `F14` ✔ Past incident HIST-115 (order-service, 2025-09-25) had identical symptoms ('connection is not available', 'pool exhausted') caused by a datasource pool size overridden by a wrong environment profile (config_change).
  - `F15` ✔ The fault propagates gateway to portfolio-service: gateway 504s are 'upstream timeout portfolio-service', and the failing endpoint /v1/portfolio/holdings is the one that needs JDBC connections.
**Contradicting evidence**
  - (none)
**Assumptions**
  - db_pool_in_use metric reflects the same pool governed by db.pool.max_size
  - the pool reduction is the binding constraint (queries themselves are not slow)

### H2: api-gateway v9.3.0 HTTP/2-to-upstream deploy dep-5220 caused the timeouts
The reported suspicion: gateway upgrade enabling HTTP/2 to portfolio-service/order-service upstreams introduced the latency/504s.

**Causal chain**
(none given)

**Supporting evidence**
  - `F9` ✔ Only one deployment occurred in the window: dep-5220 api-gateway v9.3.0 deployed at 08:00:00Z ('enable HTTP/2 to upstreams'), status live, not rolled back.
**Contradicting evidence**
  - `F6` ✔ api-gateway p99 latency jumped from ~210ms to ~3100ms in the 09:50 bucket and stayed ~4300ms; it was steady ~205ms for the preceding two hours.
  - `F8` ✔ api-gateway emitted 134 WARN 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' entries, first at 09:53:53Z.
  - `F10` ✔ api-gateway latency was normal (~205ms) from 08:00 to 09:40 despite dep-5220 being live, i.e. ~1h50m of no impact after the deploy.
  - `F13` ✔ api-gateway CPU was flat ~42% throughout, including during the incident, and error_rate rose from ~0.2% to ~7.2% at 09:50.
  - `F15` ✔ The fault propagates gateway to portfolio-service: gateway 504s are 'upstream timeout portfolio-service', and the failing endpoint /v1/portfolio/holdings is the one that needs JDBC connections.
**Rejected because:** Dep-5220 deployed at 08:00:00Z but gateway latency stayed ~205ms for the next ~1h50m (F10,F6). Onset at 09:50Z coincides with cfg-2207 applied at 09:52Z (F1,F2) — a ~2 minute lag — not with the deploy ~110 minutes earlier. Gateway CPU flat ~42% (F13), and the 504s are explicitly 'upstream timeout portfolio-service' (F8,F15), i.e. caused downstream of the gateway. No rollback of dep-5220 occurred and none needed.

### H3: ledger-db / infrastructure degradation behind portfolio-service
Shared ledger-db slow queries or IOPS throttling degrade portfolio reads, causing timeouts.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F12` ✔ ledger-db slow_queries_per_min remained ~1/min and connections_active stable ~55-62 through the window, showing the shared DB was healthy.
  - `F4` ✔ portfolio-service emitted HikariPool exhaustion errors with the pool pinned at total=5, active=5, idle=0 and dozens of waiting requests, first at 09:54:29Z.
**Rejected because:** ledger-db slow_queries stayed ~1/min and connections_active stable ~55-62 throughout (F12), so the shared DB was healthy. The failures are local connection-acquire timeouts against a 5-slot pool (F4), not query or statement timeouts. (HIST-102 is a similar-symptom but different-cause past incident.)

## Facts (verified observations)
- config cfg-2207 on portfolio-service reduced db.pool.max_size 50 -> 5 at 09:52:00Z. [F1]
- portfolio-service logged 'config change cfg-2207 applied: db.pool.max_size=5' at 09:52:13Z. [F2]
- portfolio-service db_pool_in_use was ~12 before, then pinned at exactly 5.0 from 10:00Z. [F3]
- portfolio-service emitted HikariPool exhaustion errors (total=5, active=5, idle=0, waiting=43). [F4]
- portfolio-service logged 142x 'could not acquire JDBC connection within 3000ms' from 09:53:19Z. [F5]
- api-gateway p99 jumped from ~206ms (09:40) to ~3120ms (09:50) to ~4330ms. [F6]
- portfolio-service p99 jumped from ~180ms (09:40) to ~3031ms (09:50). [F7]
- api-gateway logged 134 upstream-timeout-504 WARNs for portfolio-service /v1/portfolio/holdings from 09:53:53Z. [F8]
- Only deploy in window: api-gateway v9.3.0 dep-5220 at 08:00:00Z, live, not rolled back. [F9]
- api-gateway latency stayed normal ~205ms from 08:00 until 09:40 despite the deploy being live. [F10]
- portfolio-service request_rate was flat ~565 req/s throughout, no traffic surge. [F11]
- ledger-db slow_queries ~1/min and connections_active stable ~55-62, DB healthy. [F12]
- api-gateway CPU flat ~42%; error_rate rose to ~7.2% at 09:50. [F13]
- Historical incident HIST-115 shows identical pool-exhaustion symptoms from an environment profile pool-size override (config_change). [F14]

## Assumptions (not verified)
- db_pool_in_use reflects the same Hikari pool governed by db.pool.max_size (the metric name and the total=5 in logs are consistent).
- The 09:50 latency onset precedes the 09:52:13 config-apply log by 2 minutes due to metric bucket granularity (10-min buckets labelled at bucket start); the config is still the earliest change at onset.
- No other config/deploy changes occurred that are being masked by the tool's 00:00-11:59 window (only 2 config changes and 1 deploy were returned).

## Timeline
- `2026-03-10T08:00:00Z` api-gateway v9.3.0 deployed (dep-5220), enabling HTTP/2 to upstreams; latency remains normal. [F9, F10]
- `2026-03-10T09:40:00Z` Last healthy bucket: gateway p99 ~206ms, portfolio p99 ~180ms, pool_in_use ~12. [F6, F7, F3]
- `2026-03-10T09:50:00Z` Onset: gateway and portfolio p99 jump to ~3100ms. [F6, F7]
- `2026-03-10T09:52:13Z` Portfolio-service applies cfg-2207: db.pool.max_size=5. [F2, F1]
- `2026-03-10T09:53:19Z` First 'could not acquire JDBC connection within 3000ms' error. [F5]
- `2026-03-10T09:53:53Z` First gateway 504 upstream timeout to portfolio-service. [F8]
- `2026-03-10T09:54:29Z` First HikariPool exhaustion log: total=5, active=5, idle=0, waiting=43. [F4]
- `2026-03-10T10:00:00Z` Pool sustained pinned at 5.0; p99 sustained ~4.2s; error_rate ~7%. [F3, F6, F13]
- `2026-03-10T10:05:00Z` Incident INC-002 reported. []

## Recommended actions
- **[immediate]** Revert config cfg-2207 on portfolio-service (restore db.pool.max_size to 50) or roll it back immediately. -- The 5-slot pool is the binding constraint and the direct trigger; restoring 50 returns capacity above the ~12-connection steady state. [F1, F2, F3, F4]
- **[immediate]** Do NOT roll back api-gateway dep-5220 as the incident fix. -- The deploy predates onset by ~1h50m with no impact and the fault is downstream; rolling it back would not address the pool exhaustion. [F9, F10, F15]
- **[short_term]** Add a guardrail/validation on the cost-optimisation profile so pool reductions cannot set max_size below observed concurrency; require review for datasource pool changes. -- Same failure mode as HIST-115 (wrong environment profile overriding pool size); prevents recurrence. [F1, F14]
- **[short_term]** Alert on db_pool_in_use saturation (active==max, waiting>0) and on HikariPool acquire-timeout rate for portfolio-service. -- The pool sat pinned at 5 with waiting 30-57; a saturation alert would have fired at onset. [F3, F4]
- **[long_term]** Audit other services for recently applied 'small' cost-optimisation profiles to shared datasources. -- The change came from automated infra-automation; other services may carry the same undersized setting. [F1]

## Data gaps
- No sub-minute timeline: metrics are 10-min buckets, so the exact ordering of the 09:50 latency jump vs the 09:52:13 config-apply log cannot be resolved more precisely; the bucket labelled 09:50 aggregates 09:50-10:00.
- No direct evidence of query execution time from portfolio-service (only connection-acquire timeouts); cannot fully exclude a slow-query contribution, though ledger-db slow_queries were flat ~1/min.
- No telemetry/evidence from auth-service, market-data-service, redis-cache or kafka was checked; they are not on the failing /v1/portfolio/holdings path in the logs, but not exhaustively ruled out.

## Investigation stats
- latency 59.39s, LLM calls 5, tokens 59572 (in 51871 / out 7701)
- tool calls 16 (1 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
