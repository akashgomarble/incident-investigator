# Incident investigation report: INC-009
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
order-service slowness and statement timeouts originate in a bad deployment: portfolio-service v4.5.0 (dep-5905, 08:56) added a holdings P&L query on the shared ledger-db trades table that runs as a Seq Scan over 48M rows. This saturated ledger-db (CPU ~96%, active connections ~190, slow queries ~140/min), starving order-service INSERTs into ledger-db, which hit statement timeouts and pushed p99 to ~5s. Onset was 08:45-08:58, correlating with portfolio-service's new query, which is the sole source of all 360 ledger-db slow-query logs. The DBA autovacuum change (cfg-2903, 08:00) is a red herring: it preceded onset by 45 min and only touched audit_log. Recommended: roll back dep-5905 and add an index on trades(account_id, executed_at).

## Probable root cause
**portfolio-service dep-5905 introduced an unindexed full-table-scan query on ledger-db's trades table, saturating the shared DB and starving order-service INSERTs** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service dep-5905 introduced an unindexed full-table-scan query on ledger-db's trades table, saturating the shared DB and starving order-service INSERTs | bad_deployment | portfolio-service | dep-5905 | 0.86 (high) | active |
| 2 | H2: DBA autovacuum config change cfg-2903 caused ledger-db CPU saturation | config_change | ledger-db | cfg-2903 | 0.05 (low) | rejected |
| 3 | H3: ledger-db infrastructure/IO failure | infrastructure_failure | ledger-db | - | 0.04 (low) | rejected |

### H1: portfolio-service dep-5905 introduced an unindexed full-table-scan query on ledger-db's trades table, saturating the shared DB and starving order-service INSERTs
The portfolio-service release at 08:56 (dep-5905) added a holdings P&L query filtering trades by account_id + executed_at with no supporting index, producing a Seq Scan over 48M rows on the shared ledger-db. This drove ledger-db CPU to ~96% and active connections to ~190, so order-service's INSERT INTO orders could not complete within the statement timeout, producing p99 >5s and timeouts.

**Causal chain**
1. portfolio-service deploy dep-5905 at 08:56 adds a trades query with account_id + executed_at filter and no migration/index [F7]
2. The new query runs as a Seq Scan on trades (rows=48211907) attributed to application_name=portfolio-service [F6]
3. portfolio-service p99 rises to ~7s and its DB pool pins at 50; ledger-db CPU 30%->96%, slow queries 1->140/min, connections 62->190 [F8, F9, F3, F4, F5]
4. Saturated shared ledger-db causes order-service INSERT INTO orders statement timeouts and p99 ~5s [F2, F1]

**Supporting evidence**
  - `F1` ✔ order-service p99 latency was normal (~150ms) through the 08:30 bucket, then jumped in the 08:45 bucket (avg 512ms, max 5607ms) and stayed ~5200ms thereafter.
  - `F2` ✔ order-service logs show 284 'statement timeout' errors on INSERT INTO orders against ledger-db, with the pattern first seen at 09:00:42.
  - `F3` ✔ ledger-db CPU jumped from ~30% to ~96% at the same 08:45 onset bucket and stayed elevated.
  - `F4` ✔ ledger-db slow_queries_per_min rose from ~1/min to ~140/min at the 08:45 onset bucket.
  - `F5` ✔ ledger-db active connections rose from ~62 to ~190 (max ~212) starting in the 08:45 bucket.
  - `F6` ✔ All 360 ledger-db slow-query logs are the same query 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907)' attributed to application_name=portfolio-service, first seen 08:58:38.
  - `F7` ✔ portfolio-service was deployed dep-5905 (v4.5.0) at 08:56:00Z with change summary 'Add realised P&L breakdown to holdings API' and a new 'query on trades filtered by account_id + executed_at'; status is live and rolled_back_at is null.
  - `F8` ✔ portfolio-service also shows the same onset: p99 jumped from ~180ms (08:30 bucket) to ~6964ms (09:00 bucket), and logs 'holdings P&L query took <N>ms' warnings beginning 08:59:39.
  - `F9` ✔ portfolio-service db_pool_in_use pinned at exactly 50 (its max) from the 09:00 bucket onward, up from ~12 before.
  - `F13` ✔ A prior incident HIST-111 (2025-05-22) had the same mechanism: a new report query without an index caused sequential scans and high db cpu on ledger-db.
**Contradicting evidence**
  - `F10` ✔ The autovacuum config change cfg-2903 (autovacuum_vacuum_scale_factor 0.2->0.1) was applied at 08:00:20Z, ~45 min BEFORE onset, and autovacuum activity in ledger-db only ever processed table public.audit_log (never the trades table).
  - `F11` ✔ ledger-db disk usage stayed flat ~71% and there was no ledger-db infrastructure error (no ERROR logs; the only ledger-db WARN pattern is the portfolio slow query).
**Assumptions**
  - The 'new query on trades filtered by account_id + executed_at' in dep-5905 is the same statement seen in ledger-db slow logs
  - No index exists on trades(account_id, executed_at)

### H2: DBA autovacuum config change cfg-2903 caused ledger-db CPU saturation
The 08:00 autovacuum_vacuum_scale_factor change made autovacuum more aggressive, consuming ledger-db CPU and slowing queries.

**Causal chain**
(none given)

**Supporting evidence**
  - `F10` ✔ The autovacuum config change cfg-2903 (autovacuum_vacuum_scale_factor 0.2->0.1) was applied at 08:00:20Z, ~45 min BEFORE onset, and autovacuum activity in ledger-db only ever processed table public.audit_log (never the trades table).
**Contradicting evidence**
  - `F6` ✔ All 360 ledger-db slow-query logs are the same query 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907)' attributed to application_name=portfolio-service, first seen 08:58:38.
  - `F7` ✔ portfolio-service was deployed dep-5905 (v4.5.0) at 08:56:00Z with change summary 'Add realised P&L breakdown to holdings API' and a new 'query on trades filtered by account_id + executed_at'; status is live and rolled_back_at is null.
  - `F8` ✔ portfolio-service also shows the same onset: p99 jumped from ~180ms (08:30 bucket) to ~6964ms (09:00 bucket), and logs 'holdings P&L query took <N>ms' warnings beginning 08:59:39.
  - `F1` ✔ order-service p99 latency was normal (~150ms) through the 08:30 bucket, then jumped in the 08:45 bucket (avg 512ms, max 5607ms) and stayed ~5200ms thereafter.
  - `F3` ✔ ledger-db CPU jumped from ~30% to ~96% at the same 08:45 onset bucket and stayed elevated.
**Rejected because:** cfg-2903 applied at 08:00 but degradation began at 08:45 (45 min later); autovacuum activity only touched public.audit_log, never the trades table; all slow queries are attributable to portfolio-service, not vacuum.

### H3: ledger-db infrastructure/IO failure
Ledger-db storage or host failed, causing slow queries and timeouts.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ ledger-db disk usage stayed flat ~71% and there was no ledger-db infrastructure error (no ERROR logs; the only ledger-db WARN pattern is the portfolio slow query).
  - `F6` ✔ All 360 ledger-db slow-query logs are the same query 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907)' attributed to application_name=portfolio-service, first seen 08:58:38.
  - `F12` ✔ order-service request pattern shows only INSERT timeouts; no error logs from payment-service or settlement-service during the window (payment-service 0 matches, settlement-service only a single connection-reset retry at 10:52).
**Rejected because:** No ledger-db ERROR logs, disk usage flat at ~71%, and the slow queries are all one specific portfolio-service seq-scan statement rather than broad IO errors.

## Facts (verified observations)
- order-service p99 was ~150ms until the 08:30 bucket, then jumped to ~5300ms from the 08:45/09:00 buckets onward. [F1]
- order-service emitted 284 ledger-db statement-timeout errors on INSERT INTO orders, first at 09:00:42. [F2]
- ledger-db CPU rose from ~30% to ~96% at the 08:45 onset bucket and stayed there. [F3]
- ledger-db slow queries rose from ~1/min to ~140/min at onset. [F4]
- ledger-db active connections rose from ~62 to ~190 at onset. [F5]
- All ledger-db slow queries are the same statement: Seq Scan on trades (rows=48211907), application_name=portfolio-service, first at 08:58:38. [F6]
- portfolio-service v4.5.0 (dep-5905) was deployed at 08:56:00Z adding a new trades query on account_id + executed_at; it is live and not rolled back. [F7]
- portfolio-service p99 rose from ~180ms to ~6964ms and its DB pool pinned at 50. [F8, F9]
- Autovacuum config cfg-2903 applied at 08:00:20Z and autovacuum only ever processed public.audit_log. [F10]
- Ledger-db disk usage stayed flat at ~71% with no ERROR-level logs. [F11]

## Assumptions (not verified)
- The 'new query on trades filtered by account_id + executed_at' in dep-5905 is the exact statement logged by ledger-db slow-query output
- No index on trades(account_id, executed_at) existed before or was added by the deploy (summary says 'no migration included')
- ledger-db is a single shared instance (shared=true) with no per-service query isolation

## Timeline
- `2026-05-05T08:00:00Z` DBA applies cfg-2903 autovacuum_vacuum_scale_factor 0.2->0.1 on ledger-db (no degradation follows) [F10]
- `2026-05-05T08:30:00Z` Last normal bucket: order-service p99 ~150ms, ledger-db CPU ~30%, slow queries ~1/min [F1, F3, F4]
- `2026-05-05T08:45:00Z` Onset bucket: order-service p99 jumps to 512ms, ledger-db CPU 39%->96%, slow queries 19/min, connections 79 [F1, F3, F4, F5]
- `2026-05-05T08:56:00Z` portfolio-service v4.5.0 (dep-5905) deployed with new trades query on account_id + executed_at [F7]
- `2026-05-05T08:58:38Z` First ledger-db slow query: Seq Scan on trades (rows=48211907) from portfolio-service [F6]
- `2026-05-05T09:00:42Z` First order-service INSERT INTO orders statement-timeout error [F2]
- `2026-05-05T09:10:00Z` Incident reported (order p99 >5s, ledger-db statement timeouts) []
- `2026-05-05T10:59:00Z` Still degraded: order-service p99 ~5200ms, ledger-db CPU ~96%, portfolio DB pool pinned at 50 [F1, F9]

## Recommended actions
- **[immediate]** Roll back portfolio-service dep-5905 (v4.5.0) to the prior version -- The new trades query on the shared ledger-db is the trigger; removing it should restore ledger-db CPU and clear order-service timeouts. [F6, F7, F3, F2]
- **[short_term]** Add an index on trades(account_id, executed_at) (and validate the query plan) before re-releasing the P&L breakdown feature -- The query needs a supporting index to avoid a 48M-row seq scan; the same fix resolved prior incident HIST-111. [F6, F13]
- **[short_term]** Do NOT roll back cfg-2903 based on this incident; it is not the cause -- Autovacuum change preceded onset by 45 minutes and only touched audit_log. [F10]
- **[long_term]** Add a query-plan/index review gate and a slow-query load guard for shared ledger-db access -- Prevent unindexed sequential scans from shared services from saturating the shared database. [F13, F6]

## Data gaps
- No EXPLAIN/index metadata for the trades table was directly available; absence of an index is inferred from the Seq Scan plan in the slow-query log
- No deployment record was found for order-service or ledger-db itself, so we cannot rule out an unreleased config drift on those services beyond what logs show (order-service logs only timeouts, no errors)
- The exact time the portfolio P&L query feature was enabled for traffic is inferred from the 08:56 deploy plus 08:58 first slow query; no feature-flag change was listed

## Investigation stats
- latency 50.72s, LLM calls 6, tokens 98947 (in 90837 / out 8110)
- tool calls 24 (0 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
