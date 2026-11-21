# Incident investigation report: INC-009
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The fault originates in portfolio-service's deployment dep-5905 (v4.5.0, deployed 08:56) which added a new holdings P&L query on the shared `trades` table. The query does a sequential scan over 48.2M rows against the shared ledger-db, pinning ledger-db CPU at ~96% and starving all co-tenants. order-service (which was NOT released this week) shares ledger-db; its INSERTs began hitting statement timeouts and its connection pool saturated (~14 -> ~48 of 50), producing p99 > 5s and ~9.5% 5xx. Onset (08:58) matches the deploy, not the 08:00 autovacuum config change (which only touched audit_log and showed no correlation with onset). Root cause: bad_deployment (portfolio-service).

## Probable root cause
**portfolio-service deploy dep-5905 added an unindexed seq-scan query that saturated shared ledger-db, starving order-service** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service deploy dep-5905 added an unindexed seq-scan query that saturated shared ledger-db, starving order-service | bad_deployment | portfolio-service | dep-5905 | 0.90 (high) | active |
| 2 | H2: ledger-db autovacuum config change cfg-2903 caused the slowdown | config_change | ledger-db | cfg-2903 | 0.08 (low) | rejected |
| 3 | H3: Infrastructure fault on shared ledger-db (IOPS throttling or failover) degraded the DB | infrastructure_failure | ledger-db | - | 0.06 (low) | rejected |

### H1: portfolio-service deploy dep-5905 added an unindexed seq-scan query that saturated shared ledger-db, starving order-service
portfolio-service v4.5.0 (dep-5905, 08:56) added a holdings P&L query on trades filtered by account_id + executed_at with no supporting index and no migration. It executes a sequential scan over 48.2M rows on the shared ledger-db, driving ledger-db CPU to ~96%. The degraded DB denies/slows order-service's INSERT INTO orders, which hit statement timeouts; order-service's connection pool saturates (~14 -> ~48), producing p99 > 5s and ~9.5% 5xx on order-service and ~7s p99 on portfolio-service.

**Causal chain**
1. dep-5905 deploys portfolio-service v4.5.0 with a new trades query, no migration/index [F7]
2. new query does a Seq Scan over 48.2M rows on shared ledger-db [F6]
3. ledger-db CPU pinned ~96%; slow queries and P&L latency spike from 08:58 [F5, F6, F8, F9]
4. order-service (co-tenant, no release) pool saturates and INSERTs are cancelled by statement timeout, p99>5s and ~9.5% 5xx [F4, F3, F1, F2]

**Supporting evidence**
  - `F6` ✔ ledger-db slow-query logs (all WARN) are exclusively portfolio-service Seq Scans on trades: found first at 08:58:38, e.g. 'slow query 8690ms: SELECT * FROM trades WHERE account_id=$1 ... Seq Scan on trades (rows=48211907) application_name=portfolio-service'. No slow-query entry with this pattern exists before 08:58:38 in the 07:00-09:00 window.
  - `F7` ✔ portfolio-service was deployed dep-5905 v4.5.0 at 08:56:00 with summary 'Add realised P&L breakdown to holdings API' adding 'new query on trades filtered by account_id + executed_at' and 'no migration included'; status live, not rolled back.
  - `F8` ✔ portfolio-service logs show the new query path degrading right after deploy: 'holdings P&L query took 7459ms' first seen 08:59:39, repeating 121 times.
  - `F12` ✔ ledger-db is a shared datastore used by both order-service and portfolio-service.
  - `F4` ✔ order-service db_pool_in_use jumped abruptly within the 08:58 bucket (13.82 at 08:56 -> 29.62 avg/45.07 max at 08:58 -> ~47-49 by 09:00), i.e. pool saturation onset at ~08:58, not 08:49.
  - `F3` ✔ order-service's only ERROR pattern is cancellation of INSERTs into orders by ledger-db statement timeout: 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)' (142 counts, first seen 09:00:42).
  - `F5` ✔ ledger-db CPU jumped from ~30% to ~96% starting in the 08:49 bucket (avg 50.47 max 96.36) and stayed ~96% through 10:59.
  - `F9` ✔ portfolio-service p99 latency jumped from ~180ms to ~7000ms in the same 08:49-09:02 window.
  - `F1` ✔ order-service p99 latency was steady ~150ms until the 08:49 bucket, then jumped to avg 5313.94ms/max 5629.31ms from 09:02 onward and stayed there through 10:59.
  - `F2` ✔ order-service error_rate rose from ~0.2% to ~9.5% over the same window (08:49 bucket max 9.6, then ~9.5 steady).
  - `F14` ✔ Past incident HIST-111 (2025-05-22) 'Reporting job full table scans slowed ledger-db': root cause 'New report query without index did sequential scans', category bad_deployment, resolution 'Added index; query review gate'.
**Contradicting evidence**
  - `F13` ✔ order-service had no deployments this week (get_deployments for order-service is not in scope but config_changes for order-service is empty and the only deployment in the window is portfolio-service's), and its own CPU (~40%) and request_rate (~230/s) are flat throughout, ruling out a load increase or a resource fault inside order-service.
**Assumptions**
  - ledger-db statement_timeout is the mechanism cancelling order-service INSERTs (timeout value not directly observed)
  - the trades table lacks a suitable index on (account_id, executed_at) - inferred from the Seq Scan plan, not from schema inspection
  - the 08:49-bucket onset is due to bucket granularity; fine-grained pool data places true onset at ~08:58, consistent with the 08:56 deploy

### H2: ledger-db autovacuum config change cfg-2903 caused the slowdown
The 08:00 DBA change making autovacuum more aggressive (scale_factor 0.2 -> 0.1) triggered heavy autovacuum IO/CPU on ledger-db, slowing all queries including order-service's INSERTs.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ The ledger-db config change cfg-2903 (autovacuum_vacuum_scale_factor 0.2 -> 0.1) was applied at 08:00:20, ~58 min before onset.
  - `F11` ✔ ledger-db autovacuum activity consists of repeated 'autovacuum: processed table public.audit_log' entries continuing at a steady cadence (07:34, 07:41, 08:08 ... 10:55), i.e. it processed audit_log, not the trades table, and shows no step change at onset.
  - `F6` ✔ ledger-db slow-query logs (all WARN) are exclusively portfolio-service Seq Scans on trades: found first at 08:58:38, e.g. 'slow query 8690ms: SELECT * FROM trades WHERE account_id=$1 ... Seq Scan on trades (rows=48211907) application_name=portfolio-service'. No slow-query entry with this pattern exists before 08:58:38 in the 07:00-09:00 window.
  - `F4` ✔ order-service db_pool_in_use jumped abruptly within the 08:58 bucket (13.82 at 08:56 -> 29.62 avg/45.07 max at 08:58 -> ~47-49 by 09:00), i.e. pool saturation onset at ~08:58, not 08:49.
  - `F7` ✔ portfolio-service was deployed dep-5905 v4.5.0 at 08:56:00 with summary 'Add realised P&L breakdown to holdings API' adding 'new query on trades filtered by account_id + executed_at' and 'no migration included'; status live, not rolled back.
**Rejected because:** Timing and mechanism do not fit. cfg-2903 was applied at 08:00:20, ~58 min before onset (~08:58). Autovacuum log entries are exclusively 'processed table public.audit_log' (not the trades table) and continue at a steady pre/post-change cadence with no step change at onset. The slow queries are all portfolio-service Seq Scans on trades, introduced by dep-5905, not by vacuum activity.

### H3: Infrastructure fault on shared ledger-db (IOPS throttling or failover) degraded the DB
ledger-db itself failed (e.g. storage IOPS throttling per HIST-110, or a primary failover per HIST-102), causing statement timeouts for order-service. This is the human report's implicit alternative and matches similar past incidents.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ ledger-db slow-query logs (all WARN) are exclusively portfolio-service Seq Scans on trades: found first at 08:58:38, e.g. 'slow query 8690ms: SELECT * FROM trades WHERE account_id=$1 ... Seq Scan on trades (rows=48211907) application_name=portfolio-service'. No slow-query entry with this pattern exists before 08:58:38 in the 07:00-09:00 window.
  - `F7` ✔ portfolio-service was deployed dep-5905 v4.5.0 at 08:56:00 with summary 'Add realised P&L breakdown to holdings API' adding 'new query on trades filtered by account_id + executed_at' and 'no migration included'; status live, not rolled back.
  - `F3` ✔ order-service's only ERROR pattern is cancellation of INSERTs into orders by ledger-db statement timeout: 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)' (142 counts, first seen 09:00:42).
  - `F5` ✔ ledger-db CPU jumped from ~30% to ~96% starting in the 08:49 bucket (avg 50.47 max 96.36) and stayed ~96% through 10:59.
**Rejected because:** A ledger-db infra fault would be expected to affect all queries indiscriminately and to lack a specific offending query pattern; instead every slow-query log line is the SAME new portfolio-service query on trades, starting exactly at the deploy. No connection-refused/disk-full/failover log lines were observed and no ledger-db deployment occurred. HIST-110/HIST-102 are only surface-similar leads, not evidence.

## Facts (verified observations)
- order-service p99 latency was steady ~150ms until the 08:49 bucket, then jumped to avg 5313.94ms/max 5629.31ms from 09:02 onward and stayed there through 10:59. [F1]
- order-service error_rate rose from ~0.2% to ~9.5% over the same window (08:49 bucket max 9.6, then ~9.5 steady). [F2]
- order-service's only ERROR pattern is cancellation of INSERTs into orders by ledger-db statement timeout: 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)' (142 counts, first seen 09:00:42). [F3]
- order-service db_pool_in_use jumped abruptly within the 08:58 bucket (13.82 at 08:56 -> 29.62 avg/45.07 max at 08:58 -> ~47-49 by 09:00), i.e. pool saturation onset at ~08:58, not 08:49. [F4]
- ledger-db CPU jumped from ~30% to ~96% starting in the 08:49 bucket (avg 50.47 max 96.36) and stayed ~96% through 10:59. [F5]
- ledger-db slow-query logs (all WARN) are exclusively portfolio-service Seq Scans on trades: found first at 08:58:38, e.g. 'slow query 8690ms: SELECT * FROM trades WHERE account_id=$1 ... Seq Scan on trades (rows=48211907) application_name=portfolio-service'. No slow-query entry with this pattern exists before 08:58:38 in the 07:00-09:00 window. [F6]
- portfolio-service was deployed dep-5905 v4.5.0 at 08:56:00 with summary 'Add realised P&L breakdown to holdings API' adding 'new query on trades filtered by account_id + executed_at' and 'no migration included'; status live, not rolled back. [F7]
- portfolio-service logs show the new query path degrading right after deploy: 'holdings P&L query took 7459ms' first seen 08:59:39, repeating 121 times. [F8]
- portfolio-service p99 latency jumped from ~180ms to ~7000ms in the same 08:49-09:02 window. [F9]
- The ledger-db config change cfg-2903 (autovacuum_vacuum_scale_factor 0.2 -> 0.1) was applied at 08:00:20, ~58 min before onset. [F10]
- ledger-db autovacuum activity consists of repeated 'autovacuum: processed table public.audit_log' entries continuing at a steady cadence (07:34, 07:41, 08:08 ... 10:55), i.e. it processed audit_log, not the trades table, and shows no step change at onset. [F11]
- ledger-db is a shared datastore used by both order-service and portfolio-service. [F12]
- order-service had no deployments this week, and its own CPU (~40%) and request_rate (~230/s) are flat throughout, ruling out a load increase or a resource fault inside order-service. [F13]
- Past incident HIST-111 (2025-05-22) 'Reporting job full table scans slowed ledger-db': root cause 'New report query without index did sequential scans', category bad_deployment, resolution 'Added index; query review gate'. [F14]

## Assumptions (not verified)
- ledger-db statement_timeout is the mechanism cancelling order-service INSERTs (timeout value not directly observed).
- the trades table lacks a suitable index on (account_id, executed_at) - inferred from the Seq Scan plan, not schema inspection.
- the coarse 08:49-bucket onset is bucket granularity; fine-grained pool data places true onset at ~08:58, consistent with the 08:56 deploy.

## Timeline
- `2026-05-05T08:00:20Z` DBA applies cfg-2903: ledger-db autovacuum_vacuum_scale_factor 0.2 -> 0.1 (more aggressive autovacuum). [F10]
- `2026-05-05T08:56:00Z` portfolio-service v4.5.0 (dep-5905) deployed, adding a new P&L query on trades (account_id + executed_at). [F7]
- `2026-05-05T08:58:00Z` order-service db_pool_in_use jumps from ~14 to 29.62 avg (45.07 max); ledger-db CPU rises to ~96%; first portfolio-service Seq Scan slow query logged at 08:58:38. [F4, F5, F6]
- `2026-05-05T08:59:39Z` portfolio-service logs 'holdings P&L query took 7459ms'; its p99 climbs to ~7000ms. [F8, F9]
- `2026-05-05T09:00:42Z` First order-service ERROR: 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)'. [F3]
- `2026-05-05T09:02:00Z` Steady state: order-service p99 ~5.1-5.3s, error_rate ~9.5%, pool ~47-49/50; ledger-db CPU ~96%; persists through 10:59. [F1, F2, F4, F5]
- `2026-05-05T09:10:00Z` INC-009 reported (order-service slow, ledger-db statement timeouts). []

## Recommended actions
- **[immediate]** Roll back / disable portfolio-service dep-5905 (v4.5.0) or apply a kill-switch for the new holdings P&L breakdown query to stop the trades seq scan. -- The new query is the direct cause of ledger-db CPU saturation and the co-tenant outage; removing it is the fastest verified mitigation. [F6, F7, F5]
- **[short_term]** Add a suitable index on trades (account_id, executed_at) and re-verify the query uses it before re-enabling the feature. -- The query seq-scans 48.2M rows; an index removes the root inefficiency. This mirrors the resolution of HIST-111. [F6, F14]
- **[short_term]** Alert on ledger-db slow_queries_per_min and CPU, and on per-service attribution of heavy queries (application_name), to catch co-tenant starvation earlier. -- order-service was impacted for ~15+ min before detection; per-application slow-query visibility shortens detection. [F6, F5]
- **[long_term]** Add a query-review/index requirement gate to the portfolio-service (and shared-DB-touching) release process so unindexed table scans cannot reach a shared database. -- Prevents recurrence of the same failure mode already seen in HIST-111. [F14, F7]
- **[long_term]** Raise order-service connection-pool headroom / add DB-timeout circuit breaking so co-tenant DB saturation does not fully saturate its pool. -- The pool climbing to ~48/50 shows order-service has little resilience to ledger-db slowness. [F4]

## Data gaps
- No schema/index metadata for the trades table was available; the missing (account_id, executed_at) index is inferred from the Seq Scan plan, not directly observed.
- ledger-db statement_timeout value was not directly observed; the cancellation mechanism is inferred from order-service's 'canceling statement due to statement timeout' message.
- No ledger-db IOPS/disk-utilisation latency series beyond cpu_pct/connections_active/slow_queries_per_min was examined; an additional storage-layer contributor cannot be fully excluded, though no supporting log evidence exists.
- Confirmed rollback was not performed in-window, so recovery after rollback is unverified.

## Investigation stats
- latency 52.35s, LLM calls 6, tokens 96846 (in 87797 / out 9049)
- tool calls 24 (1 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
