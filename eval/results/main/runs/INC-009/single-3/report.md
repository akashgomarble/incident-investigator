# Incident investigation report: INC-009
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
At 08:56 portfolio-service v4.5.0 (dep-5905) shipped a new "realised P&L" query reading ledger-db's trades table with no supporting index ("no migration included"). The query does sequential scans over 48.2M rows, driving ledger-db CPU from ~30% to ~96% and slow-query rate from ~1/min to ~140/min, and connections_active from ~62 to ~190. ledger-db is a shared datastore also used by order-service; with the DB CPU-saturated, order-service's INSERT INTO orders hits statement timeouts, producing p99 ~5.2s (vs ~150ms baseline) and ~9.5% 5xx beginning 08:49-09:00. order-service itself was not deployed this week and its own request rate is flat, so the fault originates in the portfolio-service release, not at order-service nor the DBA's autovacuum config change (which predates onset by ~45min and shows no independent signature). Historical incident HIST-111 is the same pattern.

## Probable root cause
**portfolio-service v4.5.0 unindexed trades query saturates shared ledger-db, timing out order-service INSERTs** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service v4.5.0 unindexed trades query saturates shared ledger-db, timing out order-service INSERTs | bad_deployment | portfolio-service | dep-5905 | 0.85 (high) | active |
| 2 | H2: DBA autovacuum config change on ledger-db caused the slowdown | config_change | ledger-db | cfg-2903 | 0.10 (low) | rejected |
| 3 | H3: Capacity saturation of ledger-db under a traffic increase | capacity_saturation | ledger-db | - | 0.05 (low) | rejected |

### H1: portfolio-service v4.5.0 unindexed trades query saturates shared ledger-db, timing out order-service INSERTs
dep-5905 deployed at 08:56 added a realised-P&L query on ledger-db's trades table (WHERE account_id + executed_at ORDER BY executed_at) with no supporting index ('no migration included'). It does 48.2M-row sequential scans at ~140/min, pushing ledger-db CPU from ~30% to ~96% and active connections from ~62 to ~190. Because ledger-db is shared, order-service's INSERT INTO orders contends for the same saturated instance and hits statement timeouts, producing p99 ~5.2s and ~9.5% 5xx from ~08:49-09:00.

**Causal chain**
1. dep-5905 ships a new trades query with no index [F5]
2. Query runs 48.2M-row seq scans, ~140 slow queries/min, 7459-8391ms each [F4, F6, F8]
3. ledger-db CPU 30% -> 96%, connections 62 -> 190 [F7, F9]
4. Shared ledger-db saturated -> order-service INSERT INTO orders exceeds statement timeout [F3, F16]
5. order-service p99 150ms -> 5200ms and error rate 0.2% -> 9.5% [F1, F2, F10]

**Supporting evidence**
  - `F1` ✔ order-service p99 latency was flat ~150ms through 08:36, jumped to 1358ms avg at 08:49 and settled ~5200ms (max 5768ms) from 09:02 onward.
  - `F2` ✔ order-service error rate rose from ~0.2% to 9.46% avg (max 10.0) beginning in the 08:49 bucket alongside the latency jump.
  - `F3` ✔ order-service logs show 284 ERROR entries, all the single pattern 'canceling statement due to statement timeout (INSERT INTO orders)', first seen 09:00:42 and continuing to 10:58:29.
  - `F4` ✔ ledger-db logged 360 WARN entries, all the pattern 'slow query <n>ms: SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 08:58:38.
  - `F5` ✔ portfolio-service v4.5.0 (dep-5905) was deployed at 08:56:00Z with change 'new query on trades filtered by account_id + executed_at' and 'no migration included'; status live, not rolled back.
  - `F6` ✔ portfolio-service logs show 61 WARN entries 'holdings P&L query took <n>ms for account <n>', first seen 08:59:39, with durations up to 8391ms.
  - `F7` ✔ ledger-db CPU was ~30% through 08:36, then rose to avg 95.73/max 97.58 at 09:02 and stayed ~96% thereafter.
  - `F8` ✔ ledger-db slow_queries_per_min was ~1.0/min through 08:36, then 44.91 avg/max 148.81 at 08:49 and ~140/min (max 153.27) from 09:02 onward.
  - `F9` ✔ ledger-db connections_active rose from ~62 through 08:36 to avg 189.54 (max 199.85) at 09:02 and remained ~190-197.
  - `F10` ✔ order-service db_pool_in_use rose from ~14 through 08:36 to avg 46.39 (max 51.63) at 09:02, indicating connections blocked waiting on the DB.
  - `F11` ✔ order-service request_rate was essentially flat (~225-235 req/s) across the entire window, so the degradation is not caused by a traffic surge.
  - `F14` ✔ No deployments were recorded for order-service or ledger-db in the window; the only deployment across all services was dep-5905 on portfolio-service.
  - `F16` ✔ order-service depends on ledger-db (kind db), and ledger-db is flagged shared:true (used by several services).
  - `F17` ✔ The unindexed portfolio-service query and order-service's INSERT into the same ledger-db instance are co-located, so the query's CPU/IO consumption on ledger-db is the mechanism degrading order-service.
  - `F18` ✔ Historical incident HIST-111 (2025-05-22) was 'Reporting job full table scans slowed ledger-db', root cause 'New report query without index did sequential scans', category bad_deployment.
**Contradicting evidence**
  - (none)
**Assumptions**
  - ledger-db is a single instance shared by portfolio-service and order-service (shared:true implies co-location)
  - the slow-query warnings reported by ledger-db accurately attribute load to portfolio-service
  - order-service uses the same ledger-db endpoint that portfolio-service's new query hits

### H2: DBA autovacuum config change on ledger-db caused the slowdown
cfg-2903 changed ledger-db autovacuum_vacuum_scale_factor from 0.2 to 0.1 at 08:00, more aggressive autovacuum, which the incident report flags. This is rejected: it was applied ~45-49min before onset, the only autovacuum activity in logs targets public.audit_log (not trades), no vacuum errors exist, and the ledger-db CPU/slow-query pattern is dominated by portfolio-service's unindexed SELECT rather than maintenance work.

**Causal chain**
(none given)

**Supporting evidence**
  - `F12` ✔ Config change cfg-2903 changed ledger-db autovacuum_vacuum_scale_factor from 0.2 to 0.1 at 08:00:00Z ('More aggressive autovacuum') and was applied/logged at 08:00:20.
**Contradicting evidence**
  - `F4` ✔ ledger-db logged 360 WARN entries, all the pattern 'slow query <n>ms: SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 08:58:38.
  - `F5` ✔ portfolio-service v4.5.0 (dep-5905) was deployed at 08:56:00Z with change 'new query on trades filtered by account_id + executed_at' and 'no migration included'; status live, not rolled back.
  - `F7` ✔ ledger-db CPU was ~30% through 08:36, then rose to avg 95.73/max 97.58 at 09:02 and stayed ~96% thereafter.
  - `F8` ✔ ledger-db slow_queries_per_min was ~1.0/min through 08:36, then 44.91 avg/max 148.81 at 08:49 and ~140/min (max 153.27) from 09:02 onward.
  - `F13` ✔ ledger-db autovacuum activity after cfg-2903 was on table public.audit_log only; no autovacuum or vacuum error entries mention the trades table.
**Rejected because:** Config applied at 08:00 but symptoms began 08:45-08:58; autovacuum logs show only audit_log processing and no vacuum/IO signature on trades; slow-query logs explicitly attribute the heavy scans to application_name=portfolio-service, which deployed at 08:56.

### H3: Capacity saturation of ledger-db under a traffic increase
Alternative that order-service/ledger-db simply ran out of headroom under rising load. Rejected because order-service request_rate is flat (~225-235 req/s) across the window and ledger-db CPU was steady at ~30% before 08:45, so the load did not grow organically; the extra load appeared only with the new portfolio-service query.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ ledger-db CPU was ~30% through 08:36, then rose to avg 95.73/max 97.58 at 09:02 and stayed ~96% thereafter.
  - `F11` ✔ order-service request_rate was essentially flat (~225-235 req/s) across the entire window, so the degradation is not caused by a traffic surge.
**Rejected because:** request_rate flat (F11) and pre-onset DB CPU stable at ~30% (F7) rule out organic load growth as the trigger.

## Facts (verified observations)
- order-service p99 jumped from ~150ms to ~5200ms (max 5768ms) with onset in the 08:49 bucket. [F1]
- order-service errors rose to ~9.5% 5xx coincident with the latency jump. [F2]
- All order-service errors are 'canceling statement due to statement timeout (INSERT INTO orders)' against ledger-db. [F3]
- ledger-db's dominant slow query is a 48.2M-row Seq Scan on trades attributed to application_name=portfolio-service. [F4]
- portfolio-service v4.5.0 deployed at 08:56 with a new trades query and no migration (no index). [F5, F14]
- ledger-db CPU rose from ~30% to ~96% at onset. [F7]
- ledger-db slow_queries_per_min rose from ~1 to ~140. [F8]
- ledger-db active connections rose from ~62 to ~190. [F9]
- order-service DB pool usage rose from ~14 to ~46 waiting connections. [F10]
- order-service request rate was flat throughout, ruling out a traffic surge. [F11]
- The autovacuum config change cfg-2903 was applied at 08:00, ~45min before onset, and post-change autovacuum only touched public.audit_log. [F12, F13]
- ledger-db disk usage (~71%) and replication lag (~40ms) were flat, ruling out disk/replication faults. [F15]
- ledger-db is a shared datastore that order-service depends on. [F16]
- A prior incident (HIST-111) had the same pattern: an unindexed report query doing seq scans slowed ledger-db. [F18]

## Assumptions (not verified)
- ledger-db is a single shared Postgres instance serving both portfolio-service and order-service (shared:true implies co-location)
- ledger-db slow-query log attribution of application_name=portfolio-service is accurate
- order-service's INSERT INTO orders targets the same ledger-db instance that the portfolio-service query hits
- latency_p99/error_rate metrics are end-to-end for order-service as reported

## Timeline
- `2026-05-05T08:00:00Z` cfg-2903 applied to ledger-db: autovacuum_vacuum_scale_factor 0.2 -> 0.1 [F12]
- `2026-05-05T08:36:00Z` Last healthy bucket: order-service p99 ~151ms, ledger-db CPU ~30%, slow queries ~0.9/min [F1, F7, F8]
- `2026-05-05T08:49:00Z` Degradation onset: order-service p99 1358ms; ledger-db CPU 50% avg, slow queries 44.9/min; connections rising [F1, F7, F8, F9]
- `2026-05-05T08:56:00Z` portfolio-service v4.5.0 (dep-5905) deployed; new unindexed trades query [F5]
- `2026-05-05T08:58:38Z` First ledger-db slow-query warning: 8690ms Seq Scan on trades, application_name=portfolio-service [F4]
- `2026-05-05T08:59:39Z` First portfolio-service 'holdings P&L query took 7459ms' warning [F6]
- `2026-05-05T09:00:42Z` First order-service INSERT INTO orders statement timeout [F3]
- `2026-05-05T09:02:00Z` Steady-state failure: ledger-db CPU ~96%, slow queries ~140/min, connections ~190; order-service p99 ~5300ms, errors ~9.5% [F2, F7, F8, F9, F10]
- `2026-05-05T10:59:00Z` Incident still ongoing at end of data window [F1, F3]

## Recommended actions
- **[immediate]** Roll back or hotfix portfolio-service v4.5.0 (dep-5905) to stop the unindexed trades query, or disable the realised-P&L code path via feature flag -- The new query is the load source driving ledger-db saturation; removing it should relieve order-service timeouts. [F4, F5, F8]
- **[immediate]** Add an index on trades(account_id, executed_at) (or (executed_at, account_id)) to eliminate the 48.2M-row sequential scan before re-enabling the query -- The Seq Scan on trades is the direct cost driver; an index converts it to an index range scan. [F4, F6]
- **[immediate]** Verify immediate recovery: order-service p99 back to ~150ms, error rate to ~0.2%, ledger-db CPU back to ~30%, slow_queries_per_min back to ~1/min -- Confirms the causal chain and that no other fault remains. [F1, F2, F7, F8]
- **[short_term]** Reassess autovacuum_vacuum_scale_factor change separately; it is not the cause but its interaction with heavy scans on trades should be reviewed -- cfg-2903 is a distractor for this incident but the DBA change context should be closed out. [F12, F13]
- **[long_term]** Add a mandatory query-review/index-check gate and statement_timeout/rate limits for heavy analytical queries against shared ledger-db -- Prevents recurrence; mirrors HIST-111's resolution ('Added index; query review gate'). [F18]

## Data gaps
- No direct evidence of a missing index on trades (schema/index listing not available); inference rests on the 'Seq Scan on trades (rows=48211907)' plan annotation and the 'no migration included' deployment note.
- ledger-db has no per-application or per-statement breakdown metrics beyond the slow-query log, so the exact share of CPU attributable to portfolio-service vs order-service cannot be quantified.
- No ready signal for whether a rollback of dep-5905 was attempted during the window (rollback status is null = not rolled back).
- The reported 'DBA changed autovacuum settings this morning' is only corroborated by cfg-2903; no I/O or bloat metric specific to trades is available to fully exclude any autovacuum side effect.

## Investigation stats
- latency 51.83s, LLM calls 5, tokens 74053 (in 65211 / out 8842)
- tool calls 24 (1 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
