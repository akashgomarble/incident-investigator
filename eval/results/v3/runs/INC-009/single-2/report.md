# Incident investigation report: INC-009
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
order-service p99 rose to ~5s because its INSERTs against the shared ledger-db hit statement timeouts. The fault originates in portfolio-service: deploy dep-5905 (v4.5.0, deployed 08:56) added a holdings P&L query filtering trades by account_id + executed_at with no index/migration, which Postgres runs as a Seq Scan over 48.2M rows. This drove ledger-db CPU from ~30% to ~96%, slow queries from ~1/min to ~140/min, and connections from ~62 to ~199 starting ~08:50-09:00, saturating the shared DB and cascading timeouts to order-service (whose own CPU/request rate stayed flat). The reported autovacuum config change (cfg-2903, 08:00) is a red herring: it cannot produce a sequential scan and predates onset by ~50 min with no effect. Remedy: index/optimize portfolio-service's new query or roll back dep-5905.

## Probable root cause
**portfolio-service deploy introduced an unindexed full-table-scan query that saturated shared ledger-db** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service deploy introduced an unindexed full-table-scan query that saturated shared ledger-db | bad_deployment | portfolio-service | dep-5905 | 0.90 (high) | active |
| 2 | H2: Autovacuum config change on ledger-db caused the slowdown | config_change | ledger-db | cfg-2903 | 0.05 (low) | rejected |
| 3 | H3: ledger-db infrastructure failure / capacity saturation independent of any change | infrastructure_failure | ledger-db | - | 0.05 (low) | rejected |

### H1: portfolio-service deploy introduced an unindexed full-table-scan query that saturated shared ledger-db
dep-5905 (portfolio-service v4.5.0, 08:56) added a holdings P&L query filtering trades by account_id + executed_at without a supporting index. Postgres plans it as a sequential scan over 48M rows, driving ledger-db CPU to ~96% and slow queries to ~140/min, which causes order-service INSERTs into the shared ledger-db to hit statement timeouts and p99 to rise to ~5s.

**Causal chain**
1. dep-5905 rolled out portfolio-service v4.5.0 with a new trades query and no migration/index [F3]
2. The new query runs as a Seq Scan on trades (48.2M rows) from application_name=portfolio-service, first seen 08:58:38 [F4]
3. ledger-db CPU/slow-queries/connections spike from ~08:50-09:00 while both apps' CPU and request rates stay flat [F6, F7, F8, F15]
4. order-service INSERT INTO orders statements against shared ledger-db time out, so p99 rises to ~5s [F2, F1, F10]

**Supporting evidence**
  - `F1` ✔ order-service latency p99 jumped from ~150ms (steady through 08:40) to ~5,200-5,600ms beginning in the 08:50 bucket and stayed there.
  - `F2` ✔ order-service emitted 284 ERROR log entries 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)', the first at 09:00:42.
  - `F3` ✔ portfolio-service v4.5.0 (dep-5905) was deployed at 08:56:00 with change 'new query on trades filtered by account_id + executed_at' and 'no migration included', and its rollout completed at 08:56:32.
  - `F4` ✔ ledger-db logged 360 WARN slow queries of the form 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 08:58:38.
  - `F5` ✔ portfolio-service logged 239 WARN 'holdings P&L query took <n>ms' entries, first at 08:59:39 (e.g. 7459ms), matching the deploy's new holdings P&L query.
  - `F6` ✔ ledger-db CPU rose from ~30% (steady 07:00-08:40) to ~96% from the 08:50 bucket onward (max 98.05).
  - `F7` ✔ ledger-db slow_queries_per_min rose from ~1/min (steady through 08:40) to ~140/min from the 08:50/09:00 buckets.
  - `F8` ✔ ledger-db connections_active rose from ~62 (steady) to ~190-199 from the 08:50 bucket onward.
  - `F9` ✔ order-service CPU stayed flat at ~40% throughout (07:00-10:50), so the latency is not caused by order-service's own compute load.
  - `F10` ✔ order-service db_pool_in_use doubled from ~14 to ~46 in the same 08:50-09:00 window, consistent with queries blocking on a saturated shared DB rather than an order-service change.
  - `F11` ✔ ledger-db is a shared Postgres used by order-service, portfolio-service, payment-service and settlement-service.
  - `F12` ✔ No order-service deployment exists in the incident window; the only deployment across all services was dep-5905 (portfolio-service).
  - `F14` ✔ Historical incident HIST-111 (2025-05-22) documents the same pattern: a new report query without an index caused full table scans on ledger-db, categorized as bad_deployment.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 08:58:38 Seq Scan is the exact query added by dep-5905 (logs align on time, filter columns and application_name)
  - No other unlogged workload caused the scan

### H2: Autovacuum config change on ledger-db caused the slowdown
cfg-2903 changed ledger-db autovacuum_vacuum_scale_factor from 0.2 to 0.1 at 08:00; more aggressive autovacuum consumed I/O/CPU and starved other queries, causing timeouts.

**Causal chain**
(none given)

**Supporting evidence**
  - `F13` ✔ The only config change in the window was cfg-2903 on ledger-db: autovacuum_vacuum_scale_factor 0.2 -> 0.1 ('More aggressive autovacuum') at 08:00:00.
**Contradicting evidence**
  - `F4` ✔ ledger-db logged 360 WARN slow queries of the form 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 08:58:38.
  - `F6` ✔ ledger-db CPU rose from ~30% (steady 07:00-08:40) to ~96% from the 08:50 bucket onward (max 98.05).
  - `F7` ✔ ledger-db slow_queries_per_min rose from ~1/min (steady through 08:40) to ~140/min from the 08:50/09:00 buckets.
**Rejected because:** The config change at 08:00 precedes the onset by ~50 min without effect (CPU flat ~30% through 08:40), and autovacuum scale-factor cannot turn an indexed lookup into a Seq Scan on trades. The slow-query evidence names portfolio-service's new query, not autovacuum.

### H3: ledger-db infrastructure failure / capacity saturation independent of any change
ledger-db underlying capacity (IOPS/disk) degraded, causing slow queries and timeouts with no triggering release.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F3` ✔ portfolio-service v4.5.0 (dep-5905) was deployed at 08:56:00 with change 'new query on trades filtered by account_id + executed_at' and 'no migration included', and its rollout completed at 08:56:32.
  - `F4` ✔ ledger-db logged 360 WARN slow queries of the form 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 08:58:38.
  - `F6` ✔ ledger-db CPU rose from ~30% (steady 07:00-08:40) to ~96% from the 08:50 bucket onward (max 98.05).
**Rejected because:** Onset coincides exactly with dep-5905 (08:56) and the first slow query names the new portfolio-service query specifically; no disk/IOPS metric alarm is present and CPU saturation is explained by an identifiable query.

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- The 08:58:38 Seq Scan (application_name=portfolio-service) is the exact query introduced by dep-5905
- ledger-db has no supporting index on trades(account_id, executed_at)
- order-service latency/timeouts are caused solely by shared ledger-db saturation (no independent order-service fault)

## Timeline
- `2026-05-05T08:00:00Z` cfg-2903: ledger-db autovacuum_vacuum_scale_factor 0.2 -> 0.1 (no observable effect: CPU still ~30% through 08:40) [F13]
- `2026-05-05T08:56:00Z` dep-5905 deploys portfolio-service v4.5.0 with a new trades query (account_id + executed_at), no migration; rollout complete 08:56:32 [F3]
- `2026-05-05T08:58:38Z` ledger-db logs first slow query: Seq Scan on trades (48,211,907 rows) from application_name=portfolio-service [F4]
- `2026-05-05T08:59:39Z` portfolio-service logs first 'holdings P&L query took 7459ms' [F5]
- `2026-05-05T08:50:00Z` ledger-db CPU (30%->96%), slow_queries_per_min (1->140) and connections (62->199) spike; order-service p99 rises to ~5.2s [F6, F7, F8, F1]
- `2026-05-05T09:00:42Z` order-service first ERROR: ledger-db statement timeout on INSERT INTO orders [F2]

## Recommended actions
- **[immediate]** Roll back dep-5905 (portfolio-service v4.5.0) or disable the new holdings P&L query to immediately relieve ledger-db -- Removing the unindexed Seq Scan should return ledger-db CPU/slow-queries to baseline and clear order-service timeouts [F3, F4, F6]
- **[short_term]** Add a composite index on trades(account_id, executed_at) and ship it via a migration, then re-deploy -- The new query filters on exactly these columns; an index converts the 48M-row Seq Scan into an index scan [F4]
- **[short_term]** Do NOT action the autovacuum config change as the cause; keep cfg-2903 but note it is unrelated -- Evidence shows the config change did not cause the scan or the saturation [F13, F4]
- **[long_term]** Add a query-review/perf gate (EXPLAIN on production-scale data) before deploys touch the shared ledger-db -- Same failure mode as past incident HIST-111; a review gate prevents recurrence [F14]

## Data gaps
- No EXPLAIN plan / index metadata for the trades table was directly available; the Seq Scan is inferred from ledger-db 'slow query ... Seq Scan on trades' log lines rather than a schema/index dump.
- No explicit metric tying ledger-db IOPS/disk to the event; infrastructure_failure for ledger-db cannot be fully excluded from metrics alone, though the query-level evidence points to the deploy.
- No access to deployment rollback status change after 10:59 (whether rollback of dep-5905 was attempted) to confirm recovery.

## Investigation stats
- latency 43.46s, LLM calls 5, tokens 69198 (in 62364 / out 6834)
- tool calls 20 (0 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
