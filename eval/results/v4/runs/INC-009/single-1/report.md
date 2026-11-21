# Incident investigation report: INC-009
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
order-service's 5s+ p99 and 'ledger-db statement timeout (INSERT INTO orders)' errors are collateral damage from a code deployment in another service. portfolio-service dep-5905 (v4.5.0, deployed/rolled out 08:56:32Z) added an unindexed query over the 48.2M-row trades table on the SHARED ledger-db; a Seq Scan on trades (application_name=portfolio-service) began at 08:58:38Z, driving ledger-db CPU from ~30% to ~96%, connections from ~62 to ~190 and slow queries from ~1/min to ~140/min. order-service shares that database, so its INSERTs queued and timed out (p99 151ms -> 5607.8ms at 08:58; error rate 0.23% -> 9.43% at 09:00). Alternatives are ruled out: the DBA autovacuum change cfg-2903 was applied at 08:00Z yet metrics stayed flat for ~56 minutes until the deploy, and there was no failover/disk event (replication lag and disk flat) nor any order-service CPU/load increase. This matches historical incident HIST-111 (unindexed report query causing seq scans on ledger-db). Recommend rolling back dep-5905 or adding a covering index on trades(account_id, executed_at).

## Probable root cause
**portfolio-service dep-5905 unindexed seq-scan query saturates shared ledger-db, starving order-service writes** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service dep-5905 unindexed seq-scan query saturates shared ledger-db, starving order-service writes | bad_deployment | portfolio-service | dep-5905 | 0.85 (high) | active |
| 2 | H2: Aggressive autovacuum config change cfg-2903 on ledger-db causes IO/CPU contention | config_change | ledger-db | cfg-2903 | 0.08 (low) | rejected |
| 3 | H4: order-service capacity/resource saturation under load | capacity_saturation | order-service | - | 0.04 (low) | rejected |
| 4 | H3: ledger-db infrastructure failure (disk/failover/I-OPS throttling) | infrastructure_failure | ledger-db | - | 0.03 (low) | rejected |

### H1: portfolio-service dep-5905 unindexed seq-scan query saturates shared ledger-db, starving order-service writes
The new holdings P&L query shipped in portfolio-service dep-5905 (v4.5.0, 08:56Z) does an unindexed sequential scan over the 48.2M-row trades table on the shared ledger-db. This saturates the shared database (CPU ~96%, connections ~190, ~140 slow queries/min), so order-service INSERTs into orders queue and hit statement timeouts, producing the 5s+ p99 and ~9% error rate at order-service. Fault originates in the portfolio-service release.

**Causal chain**
1. dep-5905 deployed 08:56Z adds new query on trades (account_id + executed_at), no migration/index [F3]
2. portfolio-service latency jumps to ~3.5-7s at 08:56 and its P&L query logs appear 08:59 [F7]
3. ledger-db emits Seq Scan on trades (48.2M rows) slow queries from portfolio-service starting 08:58:38 [F4, F5]
4. shared ledger-db saturates: CPU ~30%->96%, connections ~62->190, slow queries ~1->140/min [F6, F12]
5. order-service INSERT INTO orders hits statement timeouts, p99 ~150ms->5.6s, error rate ~0.2%->9.4% [F1, F2, F11]

**Supporting evidence**
  - `F1` ✔ order-service p99 latency jumped from ~150ms to 5607ms between the 08:56 bucket (avg 151.35) and the 08:58 bucket (avg 2878.21, max 5607.8).
  - `F2` ✔ order-service error_rate jumped from ~0.23% to ~9.4% in the 09:00 bucket, and 284 ERROR log entries read 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)' with first occurrence 2026-05-05T09:00:42Z.
  - `F3` ✔ portfolio-service was deployed as dep-5905 (v4.5.0) at 2026-05-05T08:56:00Z; its change summary is 'new query on trades filtered by account_id + executed_at' with 'no migration included', and the rollout completed 08:56:32Z.
  - `F4` ✔ ledger-db WARN slow-query logs (360 matches) are all 'slow query ... SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 2026-05-05T08:58:38Z.
  - `F5` ✔ ledger-db slow_queries_per_min jumped from ~1/min (08:56 bucket avg 1.43) to ~139/min at the 08:58 bucket, and the seq-scan slow-query pattern did not exist before the deploy (0 matches for 'trades' in logs from 06:00 to 08:56:32Z).
  - `F6` ✔ ledger-db CPU jumped from ~30% to ~96% starting in the 08:45 bucket (max 96.36) then averaging ~96% from 09:00 onward, and connections_active rose from ~62 to ~190 with max 199.81 in the 08:45 bucket.
  - `F7` ✔ portfolio-service latency_p99 jumped from ~180ms to 3574ms in the 08:56 bucket (max 7056.7) and ~7000ms by 09:00, with its 'holdings P&L query took <n>ms' WARN logs first appearing at 08:59:39Z.
  - `F9` ✔ order-service has had no code deployment in the window (0 deployments for order-service, and the only deployment in the window was portfolio-service dep-5905).
  - `F11` ✔ order-service's db_pool_in_use rose from ~14 to ~46 as its queries queued behind the saturated shared database; order-service CPU stayed flat ~40% and request_rate flat ~231 req/s, so load did not increase.
  - `F12` ✔ ledger-db is a shared datastore (owner platform, shared=true) used by order-service (edge order-service -> ledger-db, kind db) and equally by portfolio-service's queries appearing in its logs.
  - `F13` ✔ A prior incident HIST-111 (2025-05-22) had the same mechanism: 'New report query without index did sequential scans' on ledger-db causing slow queries and high DB CPU, matching the current pattern.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 'trades' table seq-scanned by portfolio-service is the same shared ledger-db instance order-service writes to (both reference ledger-db; log shows application_name=portfolio-service on ledger-db slow queries).
  - order-service INSERT timeouts are caused by contention/queueing on ledger-db rather than an independent order-service fault (no order-service deploy exists).

### H2: Aggressive autovacuum config change cfg-2903 on ledger-db causes IO/CPU contention
The DBA autovacuum_vacuum_scale_factor change (0.2 -> 0.1) at 08:00Z made autovacuum more aggressive, causing IO/CPU contention on ledger-db that slowed all queries including order-service INSERTs.

**Causal chain**
(none given)

**Supporting evidence**
  - `F8` ✔ config change cfg-2903 (ledger-db, autovacuum_vacuum_scale_factor 0.2 -> 0.1) was applied at 2026-05-05T08:00:00Z / logged 08:00:20Z, ~56 minutes before the degradation began.
**Contradicting evidence**
  - `F5` ✔ ledger-db slow_queries_per_min jumped from ~1/min (08:56 bucket avg 1.43) to ~139/min at the 08:58 bucket, and the seq-scan slow-query pattern did not exist before the deploy (0 matches for 'trades' in logs from 06:00 to 08:56:32Z).
  - `F6` ✔ ledger-db CPU jumped from ~30% to ~96% starting in the 08:45 bucket (max 96.36) then averaging ~96% from 09:00 onward, and connections_active rose from ~62 to ~190 with max 199.81 in the 08:45 bucket.
  - `F3` ✔ portfolio-service was deployed as dep-5905 (v4.5.0) at 2026-05-05T08:56:00Z; its change summary is 'new query on trades filtered by account_id + executed_at' with 'no migration included', and the rollout completed 08:56:32Z.
  - `F4` ✔ ledger-db WARN slow-query logs (360 matches) are all 'slow query ... SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 2026-05-05T08:58:38Z.
**Rejected because:** cfg-2903 was applied at 08:00:20Z but ledger-db CPU/slow-query/connection metrics stayed flat (~30%, ~1/min, ~62) for ~56 minutes until ~08:56-08:58, coinciding with dep-5905's rollout rather than the config change. The dominant slow-query pattern is an unindexed seq scan from portfolio-service, which autovacuum settings do not produce.

### H4: order-service capacity/resource saturation under load
order-service ran out of its own capacity/resources (e.g. CPU or connection pool) under increased load.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ order-service's db_pool_in_use rose from ~14 to ~46 as its queries queued behind the saturated shared database; order-service CPU stayed flat ~40% and request_rate flat ~231 req/s, so load did not increase.
  - `F1` ✔ order-service p99 latency jumped from ~150ms to 5607ms between the 08:56 bucket (avg 151.35) and the 08:58 bucket (avg 2878.21, max 5607.8).
**Rejected because:** order-service CPU stayed flat ~40% and request_rate flat ~231 req/s (F11); the only load-linked metric that rose was db_pool_in_use, a downstream consequence of the saturated shared DB, not local capacity exhaustion.

### H3: ledger-db infrastructure failure (disk/failover/I-OPS throttling)
ledger-db suffered an infrastructure fault (disk exhaustion, primary failover, or I/O throttling) degrading all dependent services.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ ledger-db replication_lag_ms stayed flat (~40ms, max 43.67) and disk_used_pct stayed flat (~71%) throughout, showing no failover or disk-pressure event.
  - `F4` ✔ ledger-db WARN slow-query logs (360 matches) are all 'slow query ... SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 2026-05-05T08:58:38Z.
**Rejected because:** replication_lag_ms stayed flat ~40ms and disk_used_pct flat ~71% throughout (F10), with no failover or connection-refused indicators; the degradation onset aligns exactly with a code deploy, not an infra event.

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- portfolio-service and order-service both target the same shared ledger-db instance (dependency graph + portfolio-service's queries logged by ledger-db support this).
- The 'trades' table seq scan (48.2M rows) is sufficiently expensive to saturate DB CPU and connection slots, which the metrics corroborate.
- order-service has no other latent fault; absence of any order-service deployment and flat local CPU/load supports this.

## Timeline
- `2026-05-05T08:00:20Z` Config change cfg-2903 applied on ledger-db (autovacuum_vacuum_scale_factor 0.2 -> 0.1); no immediate metric effect. [F8]
- `2026-05-05T08:56:00Z` portfolio-service dep-5905 (v4.5.0) deployed; rollout completes 08:56:32Z. [F3]
- `2026-05-05T08:56:00Z` portfolio-service latency_p99 jumps ~180ms -> 3574ms. [F7]
- `2026-05-05T08:58:00Z` ledger-db slow_queries_per_min jumps ~1 -> ~139; CPU approaches ~96%; 'Seq Scan on trades' slow queries begin (first 08:58:38Z). [F5, F4, F6]
- `2026-05-05T08:58:00Z` order-service latency_p99 jumps ~151ms -> 2878ms (max 5607.8ms). [F1]
- `2026-05-05T09:00:42Z` First order-service 'canceling statement due to statement timeout (INSERT INTO orders)' error; error rate reaches ~9.4%. [F2]
- `2026-05-05T09:10:00Z` Incident reported (order-service p99 > 5s with ledger-db statement timeouts). []
- `2026-05-05T10:59:00Z` Degradation still ongoing at latest data (order-service p99 ~5.2-5.7s, ledger-db CPU ~96%). [F1, F6]

## Recommended actions
- **[immediate]** Roll back portfolio-service deployment dep-5905 (or disable the new holdings realised-P&L query) to relieve shared ledger-db saturation. -- dep-5905's unindexed seq scan on trades is the verified trigger; rollback should restore ledger-db CPU/connections and stop order-service timeouts. [F3, F4, F5, F6]
- **[short_term]** Add a covering index on trades(account_id, executed_at) and/or make the P&L query index-eligible before re-releasing portfolio-service. -- The seq scan over 48.2M rows is the mechanism; an index converts it to an index scan and limits shared-DB impact. [F4, F13]
- **[short_term]** Do NOT treat cfg-2903 as the cause; verify whether reverting autovacuum settings changes anything before acting on them. -- Metrics were flat for ~56 min after cfg-2903 and degraded exactly at the dep-5905 rollout, so the config change is not temporally causal. [F8, F5]
- **[long_term]** Add a pre-deploy query-plan review gate for queries against large shared tables (trades) on ledger-db. -- Same mechanism as prior incident HIST-111; a gate prevents unindexed seq scans reaching shared production data. [F13]
- **[long_term]** Isolate/limit resource-heavy reporting queries from the transactional shared ledger-db (separate read replica or resource group). -- order-service writes were starved by a portfolio-service analytical query on a shared instance; isolation prevents cross-service blast radius. [F12, F6]

## Data gaps
- No EXPLAIN plan/query text beyond the logged 'Seq Scan on trades (rows=48211907)' fragment was available to confirm the index absence definitively; the conclusion rests on the seq-scan log line plus the deploy change description 'no migration included'.
- No order-service-side metric finer than 120s buckets was available before 08:56; precise sub-minute onset alignment relies on ledger-db slow_queries_per_min (120s buckets) and portfolio-service latency (240s buckets) which agree at ~08:56-08:58.
- ledger-db query_logs are truncated (360 of 360 WARN returned at limit) and were not fully enumerated; counts-by-pattern were used, which could omit rarer patterns.
- No direct evidence of whether other consumers of ledger-db beyond order-service and portfolio-service were also affected.

## Investigation stats
- latency 65.73s, LLM calls 7, tokens 109547 (in 99750 / out 9797)
- tool calls 28 (1 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
