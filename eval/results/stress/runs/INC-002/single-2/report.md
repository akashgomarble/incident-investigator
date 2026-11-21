# Incident investigation report: INC-002
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The portfolio page outage originates in portfolio-service, not api-gateway. At 09:52 a config change (cfg-2207, infra-automation, 'Apply cost-optimisation profile small') cut portfolio-service db.pool.max_size from 50 to 5. Within ~1 minute Hikari reported total=5, active=5, idle=0 with 20-57 callers waiting and 3000ms JDBC acquire timeouts; db_pool_in_use pinned at exactly 5.0 (was ~12). portfolio-service p99 jumped from ~180ms to ~4.2s, so api-gateway's /v1/portfolio/holdings calls timed out -> 504s and gateway p99 ~4.3s / 7% 5xx. Request rate (~560/s), CPU (~36%) and memory (~57%) were flat, ruling out load-driven saturation or resource exhaustion. The suspected gateway deploy dep-5220 (h2 upgrade) was at 08:00, ~1h45m before onset, and order-service (the other h2-switched upstream) is entirely unaffected. A comparable past incident (HIST-115) was the same class of pool misconfiguration. Primary fix: revert cfg-2207.

## Probable root cause
**portfolio-service DB connection pool shrunk to 5 by cost-optimisation config change** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.93, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service DB connection pool shrunk to 5 by cost-optimisation config change | config_change | portfolio-service | cfg-2207 | 0.93 (high) | active |
| 2 | H2: Gateway HTTP/2-to-upstream upgrade (dep-5220) broke portfolio-service calls | bad_deployment | api-gateway | dep-5220 | 0.05 (low) | rejected |
| 3 | H3: Load growth / capacity saturation on portfolio-service | capacity_saturation | portfolio-service | - | 0.03 (low) | rejected |
| 4 | H4: ledger-db (shared Postgres) slow/IOPS-throttled causing portfolio-service DB waits | infrastructure_failure | ledger-db | - | 0.03 (low) | rejected |

### H1: portfolio-service DB connection pool shrunk to 5 by cost-optimisation config change
Config change cfg-2207 set portfolio-service db.pool.max_size from 50 to 5 at 09:52, drastically under-provisioning the Hikari connection pool. Under normal load (flat ~560 req/s), requests immediately queued for connections, hit the 3000ms acquire timeout, and portfolio-service slowed to ~4s; api-gateway then timed out on /v1/portfolio/holdings returning 504s, producing the reported gateway p99 spike. Fault originates in portfolio-service configuration (the gateway is only the symptom surface).

**Causal chain**
1. cfg-2207 sets db.pool.max_size 50->5 at 09:52 [F5]
2. Pool sized at 5; db_pool_in_use pins at 5.0 and Hikari reports total=5, idle=0 with long waiting queues [F4, F6]
3. Requests can't acquire JDBC connection within 3000ms -> portfolio-service p99 jumps to ~4s at 09:48 bucket [F3, F4]
4. api-gateway /v1/portfolio/holdings calls time out -> 504s and gateway p99 ~4.3s [F1, F2, F11]

**Supporting evidence**
  - `F3` ✔ portfolio-service p99 latency rose from ~180ms to 2555.91ms avg in the 09:48 bucket and ~4150ms from 10:00 (max 4575.38ms).
  - `F4` ✔ portfolio-service began logging ERROR 'could not acquire JDBC connection within 3000ms' at 09:53:19, and HikariPool reports exhaustion with 'total=5, active=5, idle=0, waiting=43'.
  - `F5` ✔ A config change cfg-2207 on portfolio-service set db.pool.max_size from 50 to 5 at 2026-03-10T09:52:00Z (author infra-automation, 'Apply cost-optimisation profile small to service datasource').
  - `F6` ✔ portfolio-service db_pool_in_use was ~12 connections before the change, then dropped to and pinned at exactly 5.0 for the whole period after 10:00.
  - `F1` ✔ api-gateway p99 latency rose from ~205-215ms (steady 06:00-09:30) to 2148ms avg at the 09:45 bucket and ~4300ms from 10:00 onward; max 4711.85ms.
  - `F2` ✔ api-gateway error_rate (5xx) rose from ~0.2% to 3.75% in the 09:48 bucket and settled at ~7.2% from 10:00 onward.
  - `F11` ✔ api-gateway logs during the incident are dominated by 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504', confirming the gateway symptom is downstream of portfolio-service.
  - `F12` ✔ The pool exhaustion persisted continuously from 09:53 through the end of data at 11:58, still reporting total=5.
  - `F13` ✔ Historical incident HIST-115 (order-service, 2025-09-25) had the same symptom class — 'connection is not available / pool exhausted' — with root cause 'Datasource pool size overridden by a wrong environment profile', category config_change.
  - `F9` ✔ portfolio-service request_rate was flat at ~560-580 req/s across the incident, with no surge at onset.
  - `F10` ✔ portfolio-service cpu_pct stayed flat at ~35-36% and memory_pct at ~57% throughout, showing no resource exhaustion.
**Contradicting evidence**
  - `F15` ✔ The api-gateway deploy at 08:00 precedes the 09:45-09:52 onset by ~1h45m, and gateway logs show only 1 WARN between 08:00 and 09:53.
**Assumptions**
  - No other simultaneous change to portfolio-service datasource beyond cfg-2207
  - Normal request volume (~560 req/s) genuinely required the larger pool
**Rejected because:** null

### H2: Gateway HTTP/2-to-upstream upgrade (dep-5220) broke portfolio-service calls
dep-5220 (envoy 1.30, HTTP/2 to portfolio-service/order-service) deployed at 08:00 introduced a gateway-side fault causing 4s latencies and 504s to portfolio-service.

**Causal chain**
1. dep-5220 enables h2 to portfolio-service [F7]
2. gateway cannot complete calls -> 504 [F11]

**Supporting evidence**
  - `F7` ✔ The only code deployment in the window is dep-5220 on api-gateway (v9.3.0) at 2026-03-10T08:00:00Z, 'Upgrade envoy filter chain; enable HTTP/2 to upstreams' for portfolio-service and order-service; it is still live with no rollback.
**Contradicting evidence**
  - `F7` ✔ The only code deployment in the window is dep-5220 on api-gateway (v9.3.0) at 2026-03-10T08:00:00Z, 'Upgrade envoy filter chain; enable HTTP/2 to upstreams' for portfolio-service and order-service; it is still live with no rollback.
  - `F8` ✔ order-service, the other upstream that dep-5220 switched to HTTP/2, shows no latency change: p99 flat ~148-153ms across the whole 08:00-11:59 window.
  - `F4` ✔ portfolio-service began logging ERROR 'could not acquire JDBC connection within 3000ms' at 09:53:19, and HikariPool reports exhaustion with 'total=5, active=5, idle=0, waiting=43'.
  - `F5` ✔ A config change cfg-2207 on portfolio-service set db.pool.max_size from 50 to 5 at 2026-03-10T09:52:00Z (author infra-automation, 'Apply cost-optimisation profile small to service datasource').
  - `F15` ✔ The api-gateway deploy at 08:00 precedes the 09:45-09:52 onset by ~1h45m, and gateway logs show only 1 WARN between 08:00 and 09:53.
**Rejected because:** Deploy at 08:00 is ~1h45m before onset (09:45); order-service, the other h2-switched upstream, is completely unaffected (F8); the errors are inside portfolio-service as JDBC/Hikari pool exhaustion (F4), not HTTP/2 protocol errors; gateway had no errors until the pool change.

### H3: Load growth / capacity saturation on portfolio-service
A traffic surge outgrew portfolio-service capacity, saturating the connection pool and causing timeouts.

**Causal chain**
1. request surge saturates pool *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ portfolio-service request_rate was flat at ~560-580 req/s across the incident, with no surge at onset.
  - `F10` ✔ portfolio-service cpu_pct stayed flat at ~35-36% and memory_pct at ~57% throughout, showing no resource exhaustion.
  - `F6` ✔ portfolio-service db_pool_in_use was ~12 connections before the change, then dropped to and pinned at exactly 5.0 for the whole period after 10:00.
**Rejected because:** Request rate flat (F9) with no surge; CPU ~36% and memory ~57% flat (F10); pool_pool_in_use pinned exactly at the new cap of 5 (F6), indicating an artificial limit, not organic overload.

### H4: ledger-db (shared Postgres) slow/IOPS-throttled causing portfolio-service DB waits
The shared ledger-db degraded, slowing queries and causing portfolio-service connection waits.

**Causal chain**
1. ledger-db slow -> portfolio DB waits *(assumed)*

**Supporting evidence**
  - `F14` ✔ A ledger-db latency query failed with an upstream timeout after 3 attempts, so ledger-db health could not be confirmed.
**Contradicting evidence**
  - `F4` ✔ portfolio-service began logging ERROR 'could not acquire JDBC connection within 3000ms' at 09:53:19, and HikariPool reports exhaustion with 'total=5, active=5, idle=0, waiting=43'.
  - `F6` ✔ portfolio-service db_pool_in_use was ~12 connections before the change, then dropped to and pinned at exactly 5.0 for the whole period after 10:00.
  - `F8` ✔ order-service, the other upstream that dep-5220 switched to HTTP/2, shows no latency change: p99 flat ~148-153ms across the whole 08:00-11:59 window.
**Rejected because:** Errors are explicit pool-acquire timeouts at exactly the new pool limit of 5 (F4, F6), not query/statement timeouts; ledger-db latency could not be measured (F14) but pool sizing math fully explains the symptom. order/payment also use ledger-db and are unaffected.

## Facts (verified observations)
- Onset of api-gateway latency/error degradation is the 09:45-09:48 buckets, immediately after the 09:52 config change and 09:53 first portfolio error. [F1, F2, F3, F4, F5]
- portfolio-service Hikari pool is capped at exactly 5 connections with zero idle and many waiters, matching db.pool.max_size=new_value 5. [F4, F6, F5]
- Gateways errors are downstream propagation of portfolio-service timeouts on /v1/portfolio/holdings (504). [F11]
- No load/CPU/memory anomaly accompanied the incident. [F9, F10]
- The suspected gateway h2 deploy (08:00) is timely implausible and its sibling upstream order-service is unaffected. [F7, F8, F15]

## Assumptions (not verified)
- cfg-2207 was the only change affecting the portfolio-service datasource in this window.
- Normal traffic (~560 req/s) genuinely required more than 5 DB connections.
- Absence of earlier errors reflects healthy behavior, not missing telemetry.

## Timeline
- `2026-03-10T08:00:00Z` dep-5220 api-gateway v9.3.0 deployed (envoy 1.30, HTTP/2 to portfolio-service and order-service) [F7]
- `2026-03-10T09:52:00Z` cfg-2207 changes portfolio-service db.pool.max_size 50 -> 5 [F5]
- `2026-03-10T09:48:00Z` portfolio-service p99 rises to 2556ms avg (bucket); gateway p99 2148ms and 5xx 3.75% [F3, F1, F2]
- `2026-03-10T09:53:19Z` portfolio-service first ERROR: could not acquire JDBC connection within 3000ms; Hikari total=5 idle=0 waiting rising [F4]
- `2026-03-10T09:53:53Z` api-gateway first 504 upstream timeout to portfolio-service /v1/portfolio/holdings [F11]
- `2026-03-10T10:00:00Z` Steady state: gateway p99 ~4.3s / 7.2% 5xx; portfolio p99 ~4.15s; db_pool_in_use pinned at 5.0 [F1, F2, F3, F6]
- `2026-03-10T11:58:00Z` Pool exhaustion still ongoing at end of data (total=5) [F12]

## Recommended actions
- **[immediate]** Revert cfg-2207 immediately: restore portfolio-service db.pool.max_size to 50 (or an empirically sized value). -- The pool cap of 5 is the direct cause of the JDBC acquire timeouts; restoring it removes the bottleneck. [F4, F5, F6]
- **[immediate]** Verify recovery by watching portfolio-service Hikari idle>0 / db_pool_in_use and gateway p99 returning to ~200ms after the revert. -- Confirms the causal chain and that no second fault exists. [F6, F1]
- **[short_term]** Add validation/guards so automated cost-optimisation profiles cannot reduce datasource pool size below a service-safe minimum, and require review for prod datasource changes. -- cfg-2207 was applied by infra-automation; HIST-115 was the same class of pool misconfiguration. [F5, F13]
- **[short_term]** Instrument Hikari pool saturation (idle, waiting, acquire-timeout rate) with alerting so pool exhaustion pages before user-visible 504s. -- Exhaustion ran from 09:53 to 11:58 without an automated page. [F4, F12]
- **[long_term]** Do not roll back dep-5220 as the primary mitigation; if desired, validate h2 upgrade separately (order-service already unaffected). -- Timing and sibling-service evidence exonerate the gateway deploy. [F7, F8, F15]

## Data gaps
- ledger-db latency/health could not be retrieved (query timed out 3x) — cannot fully rule out slow-query contributions, though the evidence points to pool sizing.
- No direct datapoint for portfolio-service request concurrency/queue depth beyond db_pool_in_use and Hikari 'waiting' counts.
- No explicit confirmation that cfg-2207 was the sole datasource-affecting change on portfolio-service (assumed).

## Investigation stats
- latency 58.77s, LLM calls 5, tokens 60988 (in 53501 / out 7487)
- tool calls 18 (1 failed, 0 partial, 5 recovered by retry), findings 15/15 verified
