# Incident investigation report: INC-009
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
order-service p99 blew up from ~150ms to ~5.3s with ledger-db statement timeouts on INSERT INTO orders starting 09:00:42Z. The fault is NOT in order-service (no release this week) nor the DBA autovacuum change. Root cause is portfolio-service deploy dep-5905 (v4.5.0, live at 08:56:00Z) which added an unindexed query 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at'; the planner does a Seq Scan over 48.2M trades rows, driving shared ledger-db CPU 30%->96%, slow queries ~1->140/min and connections 62->190. order-service, a co-tenant of ledger-db, then blocks on its INSERTs and times out. Autovacuum cfg-2903 (08:00Z) is ruled out because metrics stayed flat for ~45 min after it and only moved with the 08:56 deploy; payment/settlement are unaffected, ruling out a global DB infrastructure fault. Recommended: roll back dep-5905 or add a composite index on trades(account_id, executed_at) immediately.

## Probable root cause
**portfolio-service deploy introduced an unindexed seq-scan query that saturated shared ledger-db** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service deploy introduced an unindexed seq-scan query that saturated shared ledger-db | bad_deployment | portfolio-service | dep-5905 | 0.90 (high) | active |
| 2 | H2: DBA autovacuum config change caused ledger-db degradation | config_change | ledger-db | cfg-2903 | 0.08 (low) | rejected |
| 3 | H3: Generic ledger-db infrastructure failure (disk/IOPS/storage) independent of any change | infrastructure_failure | ledger-db | - | 0.05 (low) | rejected |

### H1: portfolio-service deploy introduced an unindexed seq-scan query that saturated shared ledger-db
portfolio-service v4.5.0 (dep-5905, 08:56Z) added a query 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at' with no supporting index and no migration. The planner does a Seq Scan over 48.2M trades rows for every call, driving ledger-db CPU 30%->96%, slow queries ~1->140/min and connections 62->190. order-service INSERTs against the same shared DB then queue and hit their statement timeout, producing the reported p99>5s and order timeouts.

**Causal chain**
1. dep-5905 deploys a new unindexed trades query at 08:56Z [F5]
2. query does Seq Scan on 48.2M-row trades table, 1-9s per call [F6]
3. ledger-db CPU/slow-queries/connections spike (08:45 bucket onward, sustained from 09:00) [F7, F8, F9]
4. order-service shares ledger-db and its INSERTs block -> order-service p99 150ms->5.3s, pool 14->46 [F1, F3, F4]
5. order-service INSERT INTO orders statement timeouts begin 09:00:42Z [F2]

**Supporting evidence**
  - `F1` ✔ order-service is not the only ledger-db consumer: ledger-db is a shared postgres used by order-service, portfolio-service, payment-service and settlement-service.
  - `F2` ✔ order-service errors begin at 09:00:42 and are almost entirely ledger-db statement timeouts on INSERT INTO orders (284 ERROR entries).
  - `F3` ✔ order-service p99 latency jumps from ~150ms (stable through the 08:30 bucket) to a 08:45-bucket avg of 512ms (max 5607ms) and then a sustained ~5200-5300ms from 09:00 onward.
  - `F4` ✔ order-service db_pool_in_use rises from ~14 to ~46 connections at the same 08:45-09:00 window, consistent with connections blocked on the DB.
  - `F5` ✔ portfolio-service v4.5.0 (dep-5905) was deployed at 08:56:00Z with a change 'new query on trades filtered by account_id + executed_at' and 'no migration included'; it is still live (not rolled back).
  - `F6` ✔ ledger-db logs show this exact new portfolio-service query doing a Seq Scan on a 48,211,907-row trades table, taking 1-9 seconds per call; first occurrence 08:58:38Z.
  - `F7` ✔ ledger-db CPU jumps from ~30% (flat through 08:30) to a 08:45-bucket max of 96.36% and a sustained ~96% from 09:00.
  - `F8` ✔ ledger-db slow_queries_per_min jumps from ~1/min (through 08:30) to a 08:45-bucket max of 139.68 and a sustained ~140/min from 09:00.
  - `F9` ✔ ledger-db connections_active rises from ~62 to a 08:45-bucket max of 199.81 and a sustained ~190 from 09:00.
  - `F12` ✔ A past incident HIST-111 with the same shape (a new report query without an index doing full table scans on ledger-db, high DB CPU) was classified bad_deployment and resolved by adding an index plus a query review gate.
**Contradicting evidence**
  - `F11` ✔ Other ledger-db consumers are essentially unaffected: payment-service produced 0 WARN+ entries and settlement-service only 1 connection-reset warning in the incident window.
**Assumptions**
  - The new portfolio query is executed at high volume (e.g. per user/page) rather than as a one-off report
  - order-service and portfolio-service share the same ledger-db instance/cluster (implied by the shared datastore edge)

### H2: DBA autovacuum config change caused ledger-db degradation
The 08:00Z change cfg-2903 (autovacuum_vacuum_scale_factor 0.2->0.1, 'More aggressive autovacuum') triggered heavier vacuum activity on ledger-db, raising CPU and I/O and causing order INSERT timeouts. This is the change the reporter flagged and it is on the blamed datastore.

**Causal chain**
1. cfg-2903 applied at 08:00Z [F10]
2. increased autovacuum load raises ledger-db CPU [F7]
3. order-service INSERT timeouts follow [F2]

**Supporting evidence**
  - `F10` ✔ The only config change in the window is cfg-2903: ledger-db autovacuum_vacuum_scale_factor 0.2 -> 0.1 at 08:00:00Z.
  - `F2` ✔ order-service errors begin at 09:00:42 and are almost entirely ledger-db statement timeouts on INSERT INTO orders (284 ERROR entries).
**Contradicting evidence**
  - `F7` ✔ ledger-db CPU jumps from ~30% (flat through 08:30) to a 08:45-bucket max of 96.36% and a sustained ~96% from 09:00.
  - `F8` ✔ ledger-db slow_queries_per_min jumps from ~1/min (through 08:30) to a 08:45-bucket max of 139.68 and a sustained ~140/min from 09:00.
  - `F6` ✔ ledger-db logs show this exact new portfolio-service query doing a Seq Scan on a 48,211,907-row trades table, taking 1-9 seconds per call; first occurrence 08:58:38Z.
  - `F5` ✔ portfolio-service v4.5.0 (dep-5905) was deployed at 08:56:00Z with a change 'new query on trades filtered by account_id + executed_at' and 'no migration included'; it is still live (not rolled back).
**Assumptions**
  - Autovacuum scale-factor 0.1 could meaningfully raise resource use on this DB
**Rejected because:** cfg-2903 was applied at 08:00Z but ledger-db CPU was flat at ~30% and slow_queries ~1/min through the 08:30 bucket, ~45 minutes later. The degradation coincides with the 08:56 portfolio-service deploy and its 08:58:38 seq-scan query, not with cfg-2903.

### H3: Generic ledger-db infrastructure failure (disk/IOPS/storage) independent of any change
ledger-db suffered an infrastructure-level slowdown (e.g. IOPS throttling like HIST-110) that slowed all queries and caused order timeouts.

**Causal chain**
1. ledger-db storage/IO degrades *(assumed)*
2. all queries slow, order INSERTs time out [F2]

**Supporting evidence**
  - `F7` ✔ ledger-db CPU jumps from ~30% (flat through 08:30) to a 08:45-bucket max of 96.36% and a sustained ~96% from 09:00.
**Contradicting evidence**
  - `F6` ✔ ledger-db logs show this exact new portfolio-service query doing a Seq Scan on a 48,211,907-row trades table, taking 1-9 seconds per call; first occurrence 08:58:38Z.
  - `F11` ✔ Other ledger-db consumers are essentially unaffected: payment-service produced 0 WARN+ entries and settlement-service only 1 connection-reset warning in the incident window.
  - `F5` ✔ portfolio-service v4.5.0 (dep-5905) was deployed at 08:56:00Z with a change 'new query on trades filtered by account_id + executed_at' and 'no migration included'; it is still live (not rolled back).
**Assumptions**
  - A platform-level storage fault could exist without a deploy
**Rejected because:** The slowdown is attributable to a specific new query pattern (F6) and payment/settlement, which also use ledger-db, are essentially unaffected (F11); an infrastructure fault would degrade all consumers uniformly.

## Facts (verified observations)
- ledger-db is a shared postgres used by order-service, portfolio-service, payment-service and settlement-service. [F1]
- order-service produced 284 ledger-db statement-timeout errors on INSERT INTO orders, first at 09:00:42Z. [F2]
- order-service p99 latency rose from ~150ms to ~5300ms with onset in the 08:45 bucket. [F3]
- order-service db_pool_in_use rose from ~14 to ~46 connections. [F4]
- portfolio-service v4.5.0 (dep-5905) deployed at 08:56:00Z with a new unindexed query on trades and no migration; still live. [F5]
- ledger-db logged the new portfolio-service query doing a Seq Scan on 48,211,907 rows, 1-9s per call, from 08:58:38Z. [F6]
- ledger-db CPU rose from ~30% to ~96% starting in the 08:45 bucket. [F7]
- ledger-db slow_queries_per_min rose from ~1 to ~140 starting in the 08:45 bucket. [F8]
- ledger-db connections_active rose from ~62 to ~190 starting in the 08:45 bucket. [F9]
- The only config change was ledger-db autovacuum_vacuum_scale_factor 0.2->0.1 at 08:00:00Z. [F10]
- payment-service (0 warnings) and settlement-service (1 warning) were essentially unaffected despite sharing ledger-db. [F11]
- HIST-111 was a past bad_deployment of the same shape: an unindexed report query doing full scans on ledger-db. [F12]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-05-05T08:00:00Z` DBA config change cfg-2903: ledger-db autovacuum_vacuum_scale_factor 0.2->0.1 (no metric effect for ~45 min). [F10]
- `2026-05-05T08:45:00Z` ledger-db CPU/slow-queries/connections begin rising; order-service p99 bucket avg jumps to 512ms. [F7, F8, F9, F3]
- `2026-05-05T08:56:00Z` portfolio-service v4.5.0 (dep-5905) deployed with new unindexed trades query. [F5]
- `2026-05-05T08:58:38Z` First ledger-db slow query log: 8690ms seq scan on trades, application_name=portfolio-service. [F6]
- `2026-05-05T09:00:00Z` ledger-db CPU ~96%, ~140 slow queries/min, ~190 connections; order-service p99 ~5300ms and pool ~46. [F7, F8, F9, F3, F4]
- `2026-05-05T09:00:42Z` First order-service statement timeout on INSERT INTO orders. [F2]
- `2026-05-05T09:10:00Z` Incident reported (order-service p99 > 5s, ledger-db statement timeouts). []

## Recommended actions
- **[immediate]** Roll back portfolio-service dep-5905 (v4.5.0) to the previous version to immediately stop the seq-scan load on ledger-db. -- The deploy introduced the unindexed query that is saturating shared ledger-db; removing it should restore order-service latency. [F5, F6]
- **[immediate]** Add a composite index on trades(account_id, executed_at) and verify the planner uses it (no Seq Scan on 48.2M rows). -- Directly removes the seq scan even if the feature is kept; mirrors HIST-111 resolution. [F6, F12]
- **[short_term]** Add per-application DB load attribution and alerting on ledger-db CPU/slow_queries/connections so a single tenant's query regression is caught before it starves co-tenants. -- order-service was the reported victim but portfolio-service was the source; tenant-attribution would shorten detection. [F1, F7, F8, F9]
- **[short_term]** Do not treat/roll back cfg-2903 as the cause; keep the autovacuum change but monitor it separately. -- Metrics were flat for ~45 min after cfg-2903 and only moved with the 08:56 deploy, so it is not the trigger. [F10, F7]
- **[long_term]** Add a pre-deploy query-review/EXPLAIN gate for ledger-db-bound queries so unindexed full scans cannot reach production. -- HIST-111 already recommended a query review gate and this recurred. [F12]

## Data gaps
- No per-application or per-query breakdown beyond application_name in slow-query logs; could not measure portfolio-service's query volume (calls/min) to quantify the load it injected.
- No ledger-db disk_used_pct/replication_lag_ms inspected, so an independent storage-layer contribution cannot be fully excluded (though payment/settlement being clean argues against it).
- portfolio-service call rate / request_rate metric not queried, so it is assumed the new query runs at high volume rather than rarely.
- Could not confirm whether an index on trades(account_id, executed_at) ever existed or was dropped; the deploy says 'no migration included'.

## Investigation stats
- latency 48.05s, LLM calls 5, tokens 62156 (in 54762 / out 7394)
- tool calls 18 (0 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
