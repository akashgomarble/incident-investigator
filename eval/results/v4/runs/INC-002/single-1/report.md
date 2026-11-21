# Incident investigation report: INC-002
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Portfolio page timeouts are caused by a config change (cfg-2207) applied to portfolio-service at 09:52:13, which reduced the JDBC/Hikari connection pool max size from 50 to 5 as part of a "cost-optimisation profile 'small'". With 5 connections against ~565 req/s of steady, unchanged traffic, the pool saturates immediately (HikariPool reports total=5, active=5, idle=0, waiting=25-57), queries wait and blow past the 3000ms acquisition timeout, portfolio-service returns 5xx and gateway emits 504s on /v1/portfolio/holdings. The reported suspicion — the api-gateway HTTP/2 upgrade (dep-5220) deployed at 08:00 — is ruled out: it ran ~1h45m with normal latency before onset. A ledger-db fault is also ruled out: DB CPU, slow queries and connection counts are flat/healthy and other ledger-db consumers (order-service, payment-service) are unaffected.

## Probable root cause
**portfolio-service JDBC pool reduced to 5 by config change cfg-2207** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.93, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service JDBC pool reduced to 5 by config change cfg-2207 | config_change | portfolio-service | cfg-2207 | 0.93 (high) | active |
| 2 | H3: ledger-db / shared datastore degradation | infrastructure_failure | ledger-db | - | 0.04 (low) | rejected |
| 3 | H2: api-gateway HTTP/2 upgrade (dep-5220) broke upstream calls | bad_deployment | api-gateway | dep-5220 | 0.03 (low) | rejected |

### H1: portfolio-service JDBC pool reduced to 5 by config change cfg-2207
cfg-2207 (infra-automation cost-optimisation profile 'small') cut portfolio-service db.pool.max_size 50 -> 5 at 09:52. With steady ~565 req/s the 5-connection Hikari pool saturates instantly (total=5, active=5, idle=0, waiting up to 57), queries exceed the 3000ms acquisition timeout, portfolio-service returns 5xx and the gateway surfaces 504s on /v1/portfolio/holdings.

**Causal chain**
1. cfg-2207 applied to portfolio-service at 09:52:13 setting db.pool.max_size=5 [F4, F5]
2. pool capacity 50->5 saturates under unchanged ~565 req/s load [F9, F10]
3. HikariPool exhausts: total=5, active=5, idle=0, waiting up to 57; 3s acquisition timeouts [F8]
4. portfolio-service latency/errors spike, gateway emits 504s on /v1/portfolio/holdings [F6, F7, F11]

**Supporting evidence**
  - `F4` ✔ Config change cfg-2207 changed portfolio-service db.pool.max_size from 50 to 5 at 09:52:00Z.
  - `F5` ✔ portfolio-service log confirms the config application 1 minute before failures: 'config change cfg-2207 applied: db.pool.max_size=5' at 09:52:13.
  - `F8` ✔ portfolio-service HikariPool exhausted: 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' at 09:54:29; also 'could not acquire JDBC connection within 3000ms'.
  - `F9` ✔ portfolio-service db_pool_in_use was ~12 baseline and clamped at exactly 5.0 from 10:00 onward (max=5).
  - `F6` ✔ portfolio-service latency rose from ~180ms to 3031.47ms at 09:50 and ~4200ms after.
  - `F7` ✔ portfolio-service error_rate rose from ~0.2% to 13.5% at 09:50 and ~22% after.
  - `F11` ✔ api-gateway logged 73 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' WARNs starting 09:53:53.
  - `F10` ✔ portfolio-service request_rate was steady ~550-580 req/s before and during the incident (09:30 avg 567.1, 10:00 avg 572.27) — no load spike.
  - `F15` ✔ Failure propagates gateway -> portfolio-service -> datasource pool: pool max 5 saturates under ~565 req/s, triggering 3s acquisition timeouts and 504s at the gateway.
**Contradicting evidence**
  - (none)
**Assumptions**
  - cfg-2207 was actually applied at runtime to the live pool without a restart (the INFO log line and the metric clamping to 5 support this).
  - Pre-change pool max was genuinely 50 (metric baseline ~12 in use is consistent).

### H3: ledger-db / shared datastore degradation
The shared ledger-db backing portfolio-service degraded (e.g. IOPS throttling or failover as in HIST-102/HIST-110), causing query stalls that cascade to pool exhaustion and gateway 504s.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F12` ✔ ledger-db cpu_pct flat ~30%, slow_queries_per_min flat ~1.0, connections_active fell from ~62 to ~55 — DB healthy, not saturated.
  - `F13` ✔ order-service and payment-service error_rate stayed flat ~0.2% across 08:00-11:59 despite also using shared ledger-db.
  - `F8` ✔ portfolio-service HikariPool exhausted: 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' at 09:54:29; also 'could not acquire JDBC connection within 3000ms'.
  - `F10` ✔ portfolio-service request_rate was steady ~550-580 req/s before and during the incident (09:30 avg 567.1, 10:00 avg 572.27) — no load spike.
**Rejected because:** ledger-db CPU, slow_queries_per_min and connections_active are flat/healthy (CPU ~30%, slow queries ~1/min, connections fell), and other ledger-db consumers order-service and payment-service show no error rise. F8 shows the bottleneck is the client-side pool cap of 5, not DB slowness.

### H2: api-gateway HTTP/2 upgrade (dep-5220) broke upstream calls
The envoy 1.30 / HTTP/2-to-upstream upgrade deployed to api-gateway at 08:00 introduced the latency and 504s, as the report suspected.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F1` ✔ api-gateway p99 latency (~206ms) rose sharply at 09:50 to 3119.97ms and settled ~4300ms; onset between 09:40 and 09:50.
  - `F2` ✔ api-gateway error_rate (~0.2%) rose to 4.44% at 09:50 and ~7.2% thereafter.
  - `F3` ✔ Only deployment in window: dep-5220 api-gateway v9.3.0 at 08:00, 'enable HTTP/2 to upstreams', live, not rolled back.
  - `F4` ✔ Config change cfg-2207 changed portfolio-service db.pool.max_size from 50 to 5 at 09:52:00Z.
  - `F5` ✔ portfolio-service log confirms the config application 1 minute before failures: 'config change cfg-2207 applied: db.pool.max_size=5' at 09:52:13.
  - `F14` ✔ The api-gateway HTTP/2 upgrade (dep-5220, 08:00) is not the trigger: latency/errors stayed normal for ~1h45m after it, and onset coincides with cfg-2207 (09:52).
**Rejected because:** Deploy was at 08:00 but latency/errors stayed normal (~206ms, 0.2%) until 09:40, ~1h45m later; onset aligns with cfg-2207 at 09:52. The concrete failure signature (HikariPool total=5, active=5) matches the pool config change, not an HTTP/2 upgrade. No rollback of dep-5220 or new deploy coincides with onset.

## Facts (verified observations)
- api-gateway p99 latency was normal (~205-215ms) from 08:00 until 09:40, then jumped to 3119.97ms at 09:50 and ~4300ms thereafter. [F1]
- api-gateway error_rate was ~0.2% until 09:40, then rose to 4.44% at 09:50 and ~7.2% from 10:00 onward. [F2]
- The only deployment in the window is dep-5220 to api-gateway (v9.3.0) at 2026-03-10T08:00:00Z ('enable HTTP/2 to upstreams', upstream h2 for portfolio-service and order-service), status live, not rolled back. [F3]
- A config change cfg-2207 to portfolio-service set db.pool.max_size from 50 to 5 at 2026-03-10T09:52:00Z (author infra-automation, 'cost-optimisation profile small'). [F4]
- portfolio-service logged 'config change cfg-2207 applied: db.pool.max_size=5' at 09:52:13, ~1 minute before the first pool error. [F5]
- portfolio-service latency was normal (~180ms) through 09:40 then jumped to 3031.47ms at 09:50 and ~4200ms thereafter. [F6]
- portfolio-service error_rate was ~0.2% until 09:40, then rose to 13.5% at 09:50 and ~22% from 10:00 onward. [F7]
- portfolio-service logged HikariPool-1 connection exhaustion: 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' from 09:54:29 onward (77 'could not acquire JDBC connection within 3000ms' errors, 70 HikariPool errors). [F8]
- portfolio-service db_pool_in_use dropped from ~12 connections (pre-incident baseline) and clamped at exactly 5.0 from 10:00 through 11:50. [F9]
- portfolio-service request_rate was steady at ~550-580 req/s before and after onset — no traffic spike. [F10]
- api-gateway WARN logs show 73 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' entries starting 09:53:53. [F11]
- ledger-db cpu_pct stayed flat ~29-31%, slow_queries_per_min flat ~1.0/min, and connections_active actually fell from ~62 to ~55 across the incident — no DB-level saturation. [F12]
- order-service and payment-service (also consumers of shared ledger-db) error_rate stayed flat at ~0.2% throughout the incident window. [F13]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-03-10T08:00:00Z` api-gateway deploy dep-5220 (v9.3.0, HTTP/2 to upstreams) — latency stays normal [F3]
- `2026-03-10T09:40:00Z` Last normal bucket: gateway p99 ~206ms, error_rate ~0.2% [F1, F14]
- `2026-03-10T09:50:00Z` Onset: gateway p99 3119.97ms, error_rate 4.44%; portfolio-service p99 3031.47ms, error_rate 13.5% [F1, F2, F6, F7]
- `2026-03-10T09:52:00Z` Config change cfg-2207 changes portfolio-service db.pool.max_size 50 -> 5 [F4]
- `2026-03-10T09:52:13Z` portfolio-service logs 'config change cfg-2207 applied: db.pool.max_size=5' [F5]
- `2026-03-10T09:53:19Z` First 'could not acquire JDBC connection within 3000ms' error in portfolio-service [F8]
- `2026-03-10T09:53:53Z` First gateway 504 upstream timeout to portfolio-service [F11]
- `2026-03-10T09:54:29Z` First HikariPool exhaustion (total=5, active=5, idle=0, waiting=43) [F8]
- `2026-03-10T10:05:00Z` Incident reported by SRE (suspecting gateway upgrade) []
- `2026-03-10T10:00:00Z` db_pool_in_use clamped at 5.0; error_rate plateaus ~22% (portfolio) / ~7.2% (gateway) through 11:50 [F9, F2, F7]

## Recommended actions
- **[immediate]** Immediately revert cfg-2207: restore portfolio-service db.pool.max_size to 50 (or a value sized to the ~565 req/s workload) -- The pool cap of 5 is the direct cause of pool exhaustion and 504s; reverting should restore service. [F4, F5, F8, F9]
- **[immediate]** Do NOT roll back the api-gateway dep-5220 for this incident -- Its timeline (08:00 deploy, 09:45 onset) and signature do not match; a needless rollback adds risk and won't fix pool exhaustion. [F3, F14]
- **[short_term]** Quarantine the infra-automation 'cost-optimisation profile small' applied to service datasources and add a guardrail/validation preventing pool max below a workload-derived floor -- An automated cost profile silently cut a live pool 10x with no gate — same failure class as HIST-115. [F4]
- **[short_term]** Add alerting on Hikari pool saturation (active/total ~ 1, waiting>0) per service -- Pool exhaustion was visible for ~1 min before user-facing 504s; earlier detection limits blast radius. [F8, F9]
- **[long_term]** Review config-change rollout process to require canary/staged application of datasource limits and automatic rollback on pool saturation -- Prevents recurrence of an instantly saturating pool-setting change. [F4]

## Data gaps
- No deployment or restart of portfolio-service is recorded, so it is inferred (not directly confirmed) that cfg-2207 hot-applied to the running pool rather than taking effect via a restart.
- The metric series unit/baseline for db_pool_in_use pre-incident (~12) is consistent with an old max of 50 but the pool's configured max at 09:40 is not directly shown; verification relies on cfg-2207's old_value=50.
- No data available from portfolio-service metrics between 09:40 and 09:50 at sub-10-minute resolution, so exact onset between 09:40 and 09:50 is bounded but not pinned; the 09:52 config-apply log line places the trigger precisely.
- Whether any other service also received the 'small' cost-optimisation profile was not exhaustively checked (only portfolio-service change appeared in the config-change window).

## Investigation stats
- latency 68.57s, LLM calls 5, tokens 65667 (in 57904 / out 7763)
- tool calls 19 (1 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
