# Incident investigation report: INC-009
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
order-service p99 >5s and ~9% 5xx errors beginning ~09:00Z are a downstream symptom. Root cause is deployment dep-5905 (portfolio-service v4.5.0, 08:56Z) which added an unindexed holdings P&L query on the 48M-row `trades` table, forcing a Seq Scan per request. Because ledger-db is a shared datastore, the resulting CPU saturation (~96%) and connection explosion (62->190) blocked order-service INSERT INTO orders, which the DB then cancelled via statement timeout from 09:00:42Z. order-service was not released this week. The DBA's autovacuum config change (cfg-2903, 08:00Z) is a red herring: it applied 58 min before onset, only touched public.audit_log, and ledger-db had no anomaly between 07:00 and 08:58Z.

## Probable root cause
**Unindexed portfolio-service P&L query (dep-5905) saturates shared ledger-db, timing out order-service INSERTs** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unindexed portfolio-service P&L query (dep-5905) saturates shared ledger-db, timing out order-service INSERTs | bad_deployment | portfolio-service | dep-5905 | 0.90 (high) | active |
| 2 | H2: Autovacuum tuning change (cfg-2903) on ledger-db caused the degradation | config_change | ledger-db | cfg-2903 | 0.08 (low) | rejected |
| 3 | H4: ledger-db infrastructure/IOPS or resource failure | infrastructure_failure | ledger-db | - | 0.04 (low) | rejected |
| 4 | H3: Traffic surge / capacity saturation on ledger-db | capacity_saturation | ledger-db | - | 0.03 (low) | rejected |

### H1: Unindexed portfolio-service P&L query (dep-5905) saturates shared ledger-db, timing out order-service INSERTs
Deployment dep-5905 (portfolio-service v4.5.0, 08:56Z) added a holdings P&L query on the large `trades` table filtered by account_id + executed_at with no supporting index and no migration. This forces a Seq Scan on 48M rows per request, driving ledger-db CPU to ~96% and slow queries to ~140/min. Since ledger-db is a shared datastore, order-service INSERT INTO orders statements can no longer complete and are cancelled by the DB statement timeout, producing the order-service p99 > 5s and ~9% 5xx errors. order-service itself was not changed.

**Causal chain**
1. dep-5905 deploys v4.5.0 adding an unindexed query on trades (no migration/index) [F9]
2. First ledger-db slow queries appear 08:58:38Z, all from portfolio-service doing a Seq Scan on trades (48M rows) [F4, F13]
3. ledger-db CPU 30%->96%, slow_queries 1->140/min, connections 62->190 [F5, F6, F7]
4. order-service INSERT INTO orders blocks on the saturated shared DB and is cancelled by statement timeout from 09:00:42Z [F3, F8]
5. order-service error_rate jumps to 8.94% at 09:00Z and p99 to ~5.3s [F1, F2]

**Supporting evidence**
  - `F1` ✔ order-service latency_p99_ms was flat ~150ms through 08:32 bucket, then jumped to avg 3113ms / max 5629ms in the 08:51 bucket and settled ~5200-5367ms from 09:10 onward.
  - `F2` ✔ order-service error_rate (HTTP 5xx) jumped to 8.94% at exactly 09:00:00Z from a ~0.2% baseline (per-minute data shows 0.22% at 08:59 and 8.94% at 09:00).
  - `F3` ✔ order-service logs show 70 ERROR entries, all of pattern 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)', first seen 09:00:42Z.
  - `F4` ✔ ledger-db logs show 94 WARN slow-query entries, all 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 08:58:38Z.
  - `F5` ✔ ledger-db CPU jumped from ~30% baseline to avg 71.57%/max 97.58% in the 08:51 bucket and stayed ~96% from 09:10 onward.
  - `F6` ✔ ledger-db slow_queries_per_min jumped from ~1/min baseline to avg 89.51/max 148.81 in the 08:51 bucket and ~140/min thereafter.
  - `F7` ✔ ledger-db connections_active rose from ~62 baseline to avg 142/max 199 in the 08:51 bucket and ~190 thereafter.
  - `F8` ✔ order-service db_pool_in_use rose from ~14 baseline to avg 33/max 51 in the 08:51 bucket, settling ~45, consistent with connections blocked on the slow DB.
  - `F9` ✔ Deployment dep-5905 (portfolio-service v4.5.0, deployed 08:56:00Z) added a 'new query on trades filtered by account_id + executed_at' with 'no migration included' and is live (not rolled back).
  - `F10` ✔ portfolio-service logs confirm deployment dep-5905 rollout complete at 08:56:32Z, immediately followed by 239 WARN 'holdings P&L query took <n>ms' messages first seen 08:59:39Z.
  - `F12` ✔ Request rates were flat across the onset for both order-service (~230 req/s) and portfolio-service (~570 req/s), i.e. no traffic surge.
  - `F13` ✔ ledger-db logs contain NO WARN/ERROR entries in the window 07:00-08:58Z (total_matched 0), so the slow-query pattern began only at 08:58:38Z.
  - `F14` ✔ Historical incident HIST-111 (2025-05-22) had the same class: 'New report query without index did sequential scans' slowing ledger-db, category bad_deployment.
  - `F15` ✔ portfolio-service p99 latency jumped from ~178ms baseline to avg 4490ms in the 08:51 bucket and ~6900ms thereafter.
  - `F16` ✔ portfolio-service depends on ledger-db (db) and order-service also depends on ledger-db (db); ledger-db is a shared postgres datastore.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The `trades` table lacks an index on (account_id, executed_at) so the new query degrades to a seq scan (log shows 'Seq Scan on trades')
  - The statement timeouts in order-service are caused by DB CPU/queue contention, not a separate fault

### H2: Autovacuum tuning change (cfg-2903) on ledger-db caused the degradation
The DBA's cfg-2903 change (autovacuum_vacuum_scale_factor 0.2 -> 0.1) at 08:00Z made autovacuum more aggressive, causing extra I/O/CPU on ledger-db and slowing queries including order-service inserts.

**Causal chain**
1. cfg-2903 applied at 08:00:20Z [F11]
2. ledger-db degradation begins ~08:51-08:58Z [F5, F13]

**Supporting evidence**
  - `F11` ✔ The only config change was cfg-2903 on ledger-db at 08:00:00Z (autovacuum_vacuum_scale_factor 0.2 -> 0.1); ledger-db logs confirm it applied at 08:00:20Z and autovacuum processed only public.audit_log.
**Contradicting evidence**
  - `F4` ✔ ledger-db logs show 94 WARN slow-query entries, all 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 08:58:38Z.
  - `F10` ✔ portfolio-service logs confirm deployment dep-5905 rollout complete at 08:56:32Z, immediately followed by 239 WARN 'holdings P&L query took <n>ms' messages first seen 08:59:39Z.
  - `F13` ✔ ledger-db logs contain NO WARN/ERROR entries in the window 07:00-08:58Z (total_matched 0), so the slow-query pattern began only at 08:58:38Z.
  - `F14` ✔ Historical incident HIST-111 (2025-05-22) had the same class: 'New report query without index did sequential scans' slowing ledger-db, category bad_deployment.
**Assumptions**
  - More aggressive autovacuum could raise DB CPU enough to time out unrelated writes
**Rejected because:** Timing mismatch: cfg-2903 applied at 08:00:20Z but onset is 08:51-08:58Z (58 min later) and ledger-db logs show no anomaly between 07:00 and 08:58Z (F13). The slow queries are all the portfolio-service seq scan (F4), not autovacuum, and autovacuum only touched public.audit_log (F11).

### H4: ledger-db infrastructure/IOPS or resource failure
The ledger-db storage/IO subsystem failed or throttled, causing slow queries and statement timeouts.

**Causal chain**
1. No infra error logs and specific new query pattern [F4]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F4` ✔ ledger-db logs show 94 WARN slow-query entries, all 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 08:58:38Z.
  - `F13` ✔ ledger-db logs contain NO WARN/ERROR entries in the window 07:00-08:58Z (total_matched 0), so the slow-query pattern began only at 08:58:38Z.
  - `F14` ✔ Historical incident HIST-111 (2025-05-22) had the same class: 'New report query without index did sequential scans' slowing ledger-db, category bad_deployment.
**Rejected because:** The slow queries are explicitly attributed to a specific new portfolio-service seq-scan query (F4); no storage/IO error logs and no rollback/failover evidence. Mirrors HIST-111 (bad_deployment).

### H3: Traffic surge / capacity saturation on ledger-db
A load increase outgrew ledger-db provisioned capacity, saturating CPU and timing out order-service writes.

**Causal chain**
1. Request rates flat across onset [F12]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F12` ✔ Request rates were flat across the onset for both order-service (~230 req/s) and portfolio-service (~570 req/s), i.e. no traffic surge.
  - `F4` ✔ ledger-db logs show 94 WARN slow-query entries, all 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 08:58:38Z.
**Rejected because:** order-service and portfolio-service request rates are flat across the onset (F12); the added DB load comes from a new unindexed query, not more traffic.

## Facts (verified observations)
- order-service p99 went from ~150ms to ~5.3s with onset in the 08:51 bucket. [F1]
- order-service error_rate jumped from 0.22% at 08:59 to 8.94% at 09:00 (per-minute). [F2]
- order-service errors are exclusively ledger-db statement timeouts on INSERT INTO orders, first seen 09:00:42Z. [F3]
- ledger-db slow queries are all portfolio-service 'SELECT * FROM trades ... Seq Scan on trades (rows=48211907)', first seen 08:58:38Z. [F4]
- ledger-db CPU rose to ~96%, slow_queries to ~140/min, connections to ~190 after the onset. [F5, F6, F7]
- dep-5905 (portfolio-service v4.5.0) deployed 08:56Z added a new query on trades filtered by account_id+executed_at with no migration/index, and is live. [F9, F10]
- The only config change, cfg-2903 autovacuum tuning, applied at 08:00:20Z and only autovacuumed public.audit_log. [F11]
- Request rates were flat for order-service (~230 req/s) and portfolio-service (~570 req/s) across the onset. [F12]
- ledger-db had no WARN/ERROR logs between 07:00 and 08:58Z; the pattern began at 08:58:38Z. [F13]
- ledger-db is a shared postgres datastore used by both order-service and portfolio-service. [F16]

## Assumptions (not verified)
- The `trades` table lacks an index on (account_id, executed_at), so the new query degrades to a seq scan (explicitly shown as 'Seq Scan on trades' in logs).
- order-service statement timeouts are caused by ledger-db CPU/queue contention, not a separate order-service fault.
- Deployment timestamps correspond to actual traffic effect within the minute-level recording resolution.

## Timeline
- `2026-05-05T08:00:20Z` DBA config change cfg-2903 (autovacuum_vacuum_scale_factor 0.2->0.1) applied on ledger-db [F11]
- `2026-05-05T08:56:32Z` Deployment dep-5905 (portfolio-service v4.5.0) rollout complete, adding unindexed trades query [F9, F10]
- `2026-05-05T08:58:38Z` First ledger-db slow query: portfolio-service seq scan on trades (48M rows) [F4]
- `2026-05-05T09:00:00Z` order-service error_rate jumps to 8.94% [F2]
- `2026-05-05T09:00:42Z` First order-service 'canceling statement due to statement timeout (INSERT INTO orders)' error [F3]
- `2026-05-05T09:10:00Z` Incident reported; ledger-db CPU ~96%, slow queries ~140/min, order p99 ~5.4s [F1, F5, F6]
- `2026-05-05T10:45:00Z` Degradation persists (order p99 ~5.2s, ledger-db CPU ~96%) [F1, F5]

## Recommended actions
- **[immediate]** Roll back / revert portfolio-service deployment dep-5905 (v4.5.0) immediately to remove the unindexed trades query. -- The new seq-scan query is the source of ledger-db saturation; removing it should restore order-service within minutes. [F4, F9, F10]
- **[short_term]** Add a composite index on trades(account_id, executed_at) (CONCURRENTLY) and re-deploy the P&L feature behind a query-review gate. -- Eliminates the seq scan so the feature can be re-enabled safely; matches HIST-111 resolution. [F4, F14]
- **[short_term]** Add alerting on ledger-db CPU, slow_queries_per_min and connections_active, and per-application slow-query attribution, so a single tenant's query can be caught before it saturates the shared DB. -- The DB saturated silently for ~2 minutes before order-service errors surfaced. [F5, F6, F7]
- **[long_term]** Add a deployment-time guard that flags queries touching large tables without a supporting index. -- Prevents recurrence of unindexed seq-scan queries on shared datastores (same class as HIST-111). [F14]

## Data gaps
- No direct EXPLAIN/index listing for the `trades` table was available; the missing index is inferred from the 'Seq Scan on trades' log annotation rather than confirmed catalog state.
- order-service logs were truncated (returned 30 of 70 matches) and ledger-db slow-query logs truncated (30 of 94); pattern and first-seen are consistent but full enumeration not retrieved.
- No rollback of dep-5905 has occurred, so recovery-after-revert could not be directly observed to fully close the causal loop.

## Investigation stats
- latency 54.87s, LLM calls 6, tokens 81511 (in 72503 / out 9008)
- tool calls 23 (0 failed, 0 partial, 0 recovered by retry), findings 16/16 verified
