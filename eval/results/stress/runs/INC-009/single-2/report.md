# Incident investigation report: INC-009
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
order-service p99 rose from ~150ms to ~5.2s with statement timeouts on INSERT INTO orders starting 09:00:42. The fault originates in portfolio-service, not order-service (which was not released). Deployment dep-5905 (portfolio-service v4.5.0, 08:56) added a realised-P&L query on trades filtered by account_id + executed_at with no index; against ~48M rows it forces a Seq Scan, driving the shared ledger-db CPU from ~30% to ~96% and connections from ~61 to ~190 from the 08:50 bucket. order-service, a co-tenant of ledger-db, then suffered connection-pool saturation (~47) and INSERT timeouts. The DBA autovacuum config change (cfg-2903, 08:00) and storage/IOPS degradation (HIST-110 pattern) are ruled out by timing (50-min gap) and flat disk/replication metrics respectively. Best hypothesis: bad_deployment at portfolio-service. Recommend rolling back dep-5905 or adding an index on trades(account_id, executed_at).

## Probable root cause
**Portfolio-service v4.5.0 unindexed query saturates shared ledger-db, starving order-service** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.87, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Portfolio-service v4.5.0 unindexed query saturates shared ledger-db, starving order-service | bad_deployment | portfolio-service | dep-5905 | 0.87 (high) | active |
| 2 | H2: Autovacuum config change cfg-2903 degrades ledger-db | config_change | ledger-db | cfg-2903 | 0.08 (low) | rejected |
| 3 | H3: ledger-db storage/IOPS throttling (infrastructure). | infrastructure_failure | ledger-db | - | 0.05 (low) | rejected |

### H1: Portfolio-service v4.5.0 unindexed query saturates shared ledger-db, starving order-service
The dep-5905 release of portfolio-service v4.5.0 added a realised P&L query on trades filtered by account_id + executed_at with no supporting index. Against the ~48M-row trades table this forces a sequential scan per request, driving ledger-db CPU to ~96% and connections to ~190. order-service, a co-tenant of the same shared ledger-db, then sees its INSERT INTO orders hit statement timeouts, its connection pool fill (~47 in use) and its p99 rise to ~5.2s.

**Causal chain**
1. dep-5905 (portfolio-service v4.5.0, new trades query, no migration/index) goes live 08:56 [F1]
2. New query does Seq Scan on trades (48.2M rows) -> slow queries jump to ~140/min and CPU to ~96% on ledger-db [F4, F5, F6]
3. ledger-db connections climb to ~190; shared DB resources exhausted [F7, F14]
4. order-service, co-tenant of ledger-db, sees pool saturation (~47) and INSERT INTO orders statement timeouts -> p99 ~5.2s, error rate ~9.4% [F8, F9, F10, F11]

**Supporting evidence**
  - `F1` ✔ A deployment dep-5905 of portfolio-service v4.5.0 went live at 2026-05-05T08:56:00Z adding 'new query on trades filtered by account_id + executed_at' with 'no migration included', status live and not rolled back.
  - `F2` ✔ Only one deployment exists in the 06:00-10:59 window across all services: dep-5905 (portfolio-service). No order-service deployment appears (report's 'order-service has not been released this week' is consistent).
  - `F4` ✔ ledger-db slow query log shows repeated 'Seq Scan on trades (rows=48211907)' with application_name=portfolio-service, first at 2026-05-05T08:58:38Z (8690ms).
  - `F5` ✔ ledger-db slow_queries_per_min was ~1/min through 08:40, then jumped in the 08:50 bucket (avg 28.79, max 139.68) and sustained ~140/min from 09:00 onward.
  - `F6` ✔ ledger-db cpu_pct rose from ~30% (through 08:40) to ~96% from the 08:50 bucket onward (e.g. 95.59 at 09:00), max 98.05.
  - `F7` ✔ ledger-db connections_active rose from ~61 (through 08:40) to ~190 sustained from 08:50 onward, max 212.35.
  - `F8` ✔ order-service latency_p99_ms was ~150ms through the 08:40 bucket, then rose in 08:50 (avg 694, max 5607) and settled ~5200ms from 09:00 onward.
  - `F9` ✔ order-service error_rate jumped from ~0.2% (through 08:40 and 08:50) to ~9.4% from the 09:00 bucket onward.
  - `F10` ✔ order-service db_pool_in_use rose from ~14 (through 08:40) to ~47 sustained from 09:00 onward, max 51.63.
  - `F11` ✔ order-service logs show 284 errors 'canceling statement due to statement timeout (INSERT INTO orders)', first at 2026-05-05T09:00:42Z.
  - `F12` ✔ portfolio-service logs show 'holdings P&L query took <n>ms for account <n>' warnings (239 occurrences) first seen 2026-05-05T08:59:39Z (7459ms).
  - `F14` ✔ ledger-db and order-service are connected: order-service, portfolio-service, payment-service and settlement-service all call ledger-db (a shared postgres).
  - `F15` ✔ Historical incident HIST-111 (2025-05-22) had the same pattern: a new report query without an index caused sequential scans and high DB CPU on ledger-db; root cause bad_deployment. (Lead only, not current evidence.)
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 08:50-bucket rise (slow queries, CPU, connections, p99) reflects the 08:56 deploy plus leading edge of the same bucket; the deploy is the trigger, not the autovacuum change.
  - portfolio-service did not separately have a pre-existing index on (account_id, executed_at) that the plan ignored.

### H2: Autovacuum config change cfg-2903 degrades ledger-db
The DBA's more-aggressive autovacuum change (scale_factor 0.2 -> 0.1) at 08:00 caused vacuum activity that degraded ledger-db, producing the observed CPU/connection saturation and downstream order-service timeouts.

**Causal chain**
1. cfg-2903 applied 08:00 [F3]
2. Autovacuum activity saturates ledger-db [F6]

**Supporting evidence**
  - `F3` ✔ ledger-db config change cfg-2903 lowered autovacuum_vacuum_scale_factor on ledger-db from 0.2 to 0.1 at 2026-05-05T08:00:00Z by dba-team ('More aggressive autovacuum').
**Contradicting evidence**
  - `F4` ✔ ledger-db slow query log shows repeated 'Seq Scan on trades (rows=48211907)' with application_name=portfolio-service, first at 2026-05-05T08:58:38Z (8690ms).
  - `F5` ✔ ledger-db slow_queries_per_min was ~1/min through 08:40, then jumped in the 08:50 bucket (avg 28.79, max 139.68) and sustained ~140/min from 09:00 onward.
  - `F6` ✔ ledger-db cpu_pct rose from ~30% (through 08:40) to ~96% from the 08:50 bucket onward (e.g. 95.59 at 09:00), max 98.05.
  - `F7` ✔ ledger-db connections_active rose from ~61 (through 08:40) to ~190 sustained from 08:50 onward, max 212.35.
  - `F8` ✔ order-service latency_p99_ms was ~150ms through the 08:40 bucket, then rose in 08:50 (avg 694, max 5607) and settled ~5200ms from 09:00 onward.
**Assumptions**
  - Autovacuum, not the new seq-scan query, is the dominant CPU consumer.
**Rejected because:** Config change at 08:00 precedes the 08:50-08:58 onset by ~50 minutes with no symptom in between (CPU/connections/p99 flat until 08:50). Slow-query log attributes the load to portfolio-service seq scans, not autovacuum, and CPU saturates exactly when those queries start.

### H3: ledger-db storage/IOPS throttling (infrastructure).
Cloud volume IOPS throttling on ledger-db (as in HIST-110) degraded query latency and produced downstream timeouts.

**Causal chain**
1. Storage IOPS throttled on ledger-db *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F13` ✔ ledger-db disk_used_pct stayed flat ~71% and replication_lag_ms flat ~40ms through the whole window (no storage or replication degradation).
  - `F4` ✔ ledger-db slow query log shows repeated 'Seq Scan on trades (rows=48211907)' with application_name=portfolio-service, first at 2026-05-05T08:58:38Z (8690ms).
**Assumptions**
  - Burst credits exhausted despite flat disk usage.
**Rejected because:** disk_used_pct flat ~71% and replication_lag_ms flat ~40ms throughout; the slow queries are attributable to an unindexed seq scan from portfolio-service, not storage latency.

## Facts (verified observations)
- Only one deployment occurred in the incident window: dep-5905 (portfolio-service v4.5.0) at 08:56, adding an unindexed trades query; no order-service deployment. [F1, F2]
- ledger-db slow queries began 08:58:38 with 'Seq Scan on trades (rows=48211907) application_name=portfolio-service', from ~1/min to ~140/min. [F4, F5]
- ledger-db CPU rose ~30% -> ~96% and connections ~61 -> ~190 from the 08:50 bucket onward. [F6, F7]
- order-service p99 ~150ms -> ~5.2s, error rate ~0.2% -> ~9.4%, db_pool_in_use ~14 -> ~47; 284 INSERT INTO orders statement timeouts from 09:00:42. [F8, F9, F10, F11]
- ledger-db is shared by order-service, portfolio-service, payment-service and settlement-service. [F14]
- Autovacuum config change cfg-2903 was applied at 08:00, ~50 min before onset. [F3]
- ledger-db disk_used_pct (~71%) and replication_lag_ms (~40ms) stayed flat throughout. [F13]

## Assumptions (not verified)
- The 08:50-bucket aggregates include the leading edge of the same event triggered by the 08:56 deploy; the tight temporal match (deploy 08:56, first slow query 08:58:38, first portfolio warning 08:59:39, first order timeout 09:00:42) supports the deploy as trigger.
- No separate portfolio-service deployment or change preceded 08:56 that could produce the same query.
- The trades table has no index on (account_id, executed_at) that the planner could have used.

## Timeline
- `2026-05-05T08:00:00Z` cfg-2903: ledger-db autovacuum_vacuum_scale_factor 0.2 -> 0.1 (dba-team) [F3]
- `2026-05-05T08:50:00Z` ledger-db slow_queries/CPU/connections and order-service p99 begin rising (08:50 bucket) [F5, F6, F7, F8]
- `2026-05-05T08:56:00Z` dep-5905 portfolio-service v4.5.0 deployed (new unindexed trades query) [F1]
- `2026-05-05T08:58:38Z` First ledger-db slow query: Seq Scan on trades from portfolio-service (8690ms) [F4]
- `2026-05-05T08:59:39Z` First portfolio-service 'holdings P&L query took 7459ms' warning [F12]
- `2026-05-05T09:00:42Z` First order-service statement timeout on INSERT INTO orders [F11]
- `2026-05-05T09:10:00Z` Incident INC-009 reported (SEV-1) []

## Recommended actions
- **[immediate]** Roll back dep-5905 (portfolio-service v4.5.0) or disable the realised-P&L endpoint immediately -- Removing the unindexed seq-scan workload is the direct trigger; ledger-db CPU and order-service latency should recover within minutes. [F1, F4, F5, F6]
- **[immediate]** Add index on trades(account_id, executed_at) before re-enabling the feature -- The query filters by account_id + executed_at ordered by executed_at; an index eliminates the 48M-row seq scan. [F4]
- **[short_term]** Add query-plan/index review gate for new queries against large shared tables (trades) -- Same failure mode as HIST-111; a pre-merge EXPLAIN gate would catch unindexed scans. [F15]
- **[short_term]** Add alerting on ledger-db slow_queries_per_min and CPU with per-application attribution -- Onset was detectable at 08:50-08:58; per-app attribution would have named portfolio-service immediately. [F5, F6]
- **[long_term]** Consider isolating heavy analytical/reporting queries away from the shared transactional ledger-db (read replica) -- Co-tenancy of order-service and portfolio-service on one DB makes any heavy query a cross-service risk. [F14]

## Data gaps
- get_deployments for order-service returned 503 (backend unavailable) after 3 attempts, so order-service-specific deployment history could not be independently confirmed; the all-services deployment query (tc-001) showed no order-service deployment in the window.
- No ledger-db error/critical logs were returned (0 matches) — only WARN slow-query lines; absence of ERROR-level DB logs does not prove nothing else occurred.
- Exact index definitions on the trades table were not directly inspected; the unindexed-status inference rests on the logged 'Seq Scan on trades' plan.

## Investigation stats
- latency 60.99s, LLM calls 6, tokens 88889 (in 80289 / out 8600)
- tool calls 23 (1 failed, 0 partial, 10 recovered by retry), findings 15/15 verified
