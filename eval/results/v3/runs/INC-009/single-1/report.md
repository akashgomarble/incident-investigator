# Incident investigation report: INC-009
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order placement latency/timeouts originate from portfolio-service deploy dep-5905 (v4.5.0, rolled out 08:56:32Z) which added an unindexed query on the shared ledger-db's 48M-row trades table (Seq Scan on trades, rows=48211907). The full-table scans drove ledger-db CPU from ~30% to ~96% and saturated its connections, starving order-service's INSERT INTO orders and causing statement timeouts. Both affected services (portfolio-service, order-service) are the two that query trades/orders heavily; the anomaly begins exactly in the first metric bucket after the deploy (08:58), while the earlier 08:00 autovacuum config change left CPU flat at ~30% for 56 minutes and is not the trigger. Root cause is a bad_deployment in portfolio-service; order-service is a downstream victim of the shared database saturation.

## Probable root cause
**Bad deployment: portfolio-service v4.5.0 unindexed trades query saturates shared ledger-db, starving order-service** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bad deployment: portfolio-service v4.5.0 unindexed trades query saturates shared ledger-db, starving order-service | bad_deployment | portfolio-service | dep-5905 | 0.90 (high) | active |
| 2 | H2: Config change: aggressive autovacuum on ledger-db (cfg-2903) causes CPU/IO saturation | config_change | ledger-db | cfg-2903 | 0.05 (low) | rejected |
| 3 | H3: Resource exhaustion / infrastructure failure at ledger-db (CPU, IOPS or connections) | resource_exhaustion | ledger-db | - | 0.03 (low) | rejected |
| 4 | H4: External dependency (exchange-api / market-data) failure propagating to order-service | dependency_failure | exchange-api | - | 0.02 (low) | rejected |

### H1: Bad deployment: portfolio-service v4.5.0 unindexed trades query saturates shared ledger-db, starving order-service
portfolio-service deploy dep-5905 (v4.5.0, 08:56) introduced a query on the 48M-row trades table filtered by account_id + executed_at with no supporting index ('no migration included'). Postgres runs a Seq Scan on trades (~48M rows) per request, driving ledger-db CPU to ~96% and consuming connections. The shared ledger-db becomes CPU/connection starved, so order-service's INSERT INTO orders exceed their statement timeout, producing order-placement slowness and timeouts.

**Causal chain**
1. dep-5905 deploys new trades query with no index at 08:56 [F6]
2. First bad buckets and first portfolio slow query appear at 08:58 (portfolio p99 185->6963ms, ledger-db CPU 30->96%, order p99 151->2878ms) [F2, F3, F1, F14]
3. ledger-db logs Seq Scan on trades (rows=48211907) application_name=portfolio-service, ~140 slow queries/min [F5, F4]
4. Shared ledger-db saturates; portfolio and order db pools peg (12->50, 14->46) [F9, F8, F10]
5. order-service INSERT INTO orders hit statement timeout against ledger-db [F8]

**Supporting evidence**
  - `F1` ✔ order-service p99 latency was normal (~148-165ms) until the 08:56 bucket, then jumped at 08:58 to 2878ms avg / max 5607ms and stayed ~5.2s through 10:50.
  - `F2` ✔ portfolio-service p99 latency was flat (~175-185ms) through 08:56, then jumped at 08:58 to 6963ms and stayed ~7s.
  - `F3` ✔ ledger-db CPU was ~30% through 08:56, then jumped to ~96% at 08:58 and stayed saturated.
  - `F4` ✔ ledger-db slow queries were ~1/min until 08:30, then spiked in the 08:45-09:00 bucket (avg 19.48, max 139.68) and reached ~140/min from 09:00.
  - `F5` ✔ The dominant slow query at ledger-db is a Seq Scan on the trades table (rows=48211907) attributed to application_name=portfolio-service, first seen 08:58:38.
  - `F6` ✔ portfolio-service was deployed as dep-5905 (v4.5.0) at 08:56, with change summary 'new query on trades filtered by account_id + executed_at' and 'no migration included'; rollout completed 08:56:32. No other service was deployed in the window.
  - `F8` ✔ order-service errors are exclusively 'canceling statement due to statement timeout (INSERT INTO orders)' against ledger-db, first seen 09:00:42; error_rate rose from ~0.2% to ~9.4%, and db_pool_in_use rose from ~14 to ~46.
  - `F9` ✔ portfolio-service's db_pool_in_use jumped from ~12 to pegged at its max of 50 at 08:45-09:00 onward, while its error_rate stayed flat at ~0.2% (it is slow but not erroring).
  - `F10` ✔ ledger-db is a shared datastore used by order-service, portfolio-service, payment-service and settlement-service.
  - `F14` ✔ Inference: portfolio-service's deploy rollout (08:56:32) precedes the first bad metric bucket (08:58) and the first portfolio-slow-query log (08:58:38) by ~1.5-2 minutes; nothing else anomalous occurs between 08:00 and 08:56.
  - `F13` ✔ Historical incident HIST-111 is an exact precedent: a new report query without an index caused sequential scans that slowed ledger-db, root cause bad_deployment, resolution 'Added index; query review gate'.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The deploy's new query is the direct source of the trades Seq Scan (query text matches change summary).
  - order-service's INSERT INTO orders is contending on the same CPU-bound ledger-db instance.

### H2: Config change: aggressive autovacuum on ledger-db (cfg-2903) causes CPU/IO saturation
The DBA's 08:00 autovacuum_vacuum_scale_factor change (0.2->0.1) made autovacuum far more aggressive on the 48M-row trades table, saturating ledger-db CPU and slowing all queries, including order-service inserts.

**Causal chain**
1. cfg-2903 changes autovacuum at 08:00 [F7]
2. ledger-db CPU/IO saturates [F3]
3. order-service inserts time out [F8]

**Supporting evidence**
  - `F7` ✔ The only config change in the window was cfg-2903 on ledger-db at 08:00 (autovacuum_vacuum_scale_factor 0.2 -> 0.1, 'More aggressive autovacuum'); ledger-db CPU remained ~30% for the following 56 minutes.
**Contradicting evidence**
  - `F7` ✔ The only config change in the window was cfg-2903 on ledger-db at 08:00 (autovacuum_vacuum_scale_factor 0.2 -> 0.1, 'More aggressive autovacuum'); ledger-db CPU remained ~30% for the following 56 minutes.
  - `F14` ✔ Inference: portfolio-service's deploy rollout (08:56:32) precedes the first bad metric bucket (08:58) and the first portfolio-slow-query log (08:58:38) by ~1.5-2 minutes; nothing else anomalous occurs between 08:00 and 08:56.
  - `F5` ✔ The dominant slow query at ledger-db is a Seq Scan on the trades table (rows=48211907) attributed to application_name=portfolio-service, first seen 08:58:38.
  - `F6` ✔ portfolio-service was deployed as dep-5905 (v4.5.0) at 08:56, with change summary 'new query on trades filtered by account_id + executed_at' and 'no migration included'; rollout completed 08:56:32. No other service was deployed in the window.
**Rejected because:** Timing does not fit: the config change is at 08:00 but ledger-db CPU stayed flat at ~30% for 56 minutes and only spiked at 08:58, exactly when dep-5905 rolled out. The slow queries are explicitly attributed to application_name=portfolio-service with a Seq Scan on trades, not autovacuum.

### H3: Resource exhaustion / infrastructure failure at ledger-db (CPU, IOPS or connections)
ledger-db itself ran out of a resource (CPU/IOPS/connections) independent of any release, degrading all dependent services.

**Causal chain**
1. ledger-db CPU hits 96% [F3]
2. connections rise, pools peg [F8, F9]
3. order-service inserts time out [F8]

**Supporting evidence**
  - `F3` ✔ ledger-db CPU was ~30% through 08:56, then jumped to ~96% at 08:58 and stayed saturated.
**Contradicting evidence**
  - `F14` ✔ Inference: portfolio-service's deploy rollout (08:56:32) precedes the first bad metric bucket (08:58) and the first portfolio-slow-query log (08:58:38) by ~1.5-2 minutes; nothing else anomalous occurs between 08:00 and 08:56.
  - `F6` ✔ portfolio-service was deployed as dep-5905 (v4.5.0) at 08:56, with change summary 'new query on trades filtered by account_id + executed_at' and 'no migration included'; rollout completed 08:56:32. No other service was deployed in the window.
  - `F5` ✔ The dominant slow query at ledger-db is a Seq Scan on the trades table (rows=48211907) attributed to application_name=portfolio-service, first seen 08:58:38.
  - `F12` ✔ No disk pressure on ledger-db (disk_used_pct ~71% flat) and no replication lag (replication_lag_ms ~40ms flat) during the incident.
**Rejected because:** The CPU spike is a consequence of the deployed seq-scan workload (query text and application_name=portfolio-service prove the source) and starts only after the 08:56 deploy. Disk and replication lag were normal (F12). No faulty node/broker.

### H4: External dependency (exchange-api / market-data) failure propagating to order-service
A degradation of an external provider or market-data dependency slowed order placement.

**Causal chain**
1. exchange-api degrades *(assumed)*
2. order-service placement slows [F1]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ order-service errors are exclusively 'canceling statement due to statement timeout (INSERT INTO orders)' against ledger-db, first seen 09:00:42; error_rate rose from ~0.2% to ~9.4%, and db_pool_in_use rose from ~14 to ~46.
  - `F5` ✔ The dominant slow query at ledger-db is a Seq Scan on the trades table (rows=48211907) attributed to application_name=portfolio-service, first seen 08:58:38.
  - `F3` ✔ ledger-db CPU was ~30% through 08:56, then jumped to ~96% at 08:58 and stayed saturated.
**Rejected because:** All order-service errors are ledger-db statement timeouts (INSERT INTO orders), not exchange/gateway errors; the onset coincides with ledger-db CPU saturation and the portfolio deploy.

## Facts (verified observations)
- order-service p99 rose from ~151ms (08:56) to 2878ms avg / 5607ms max (08:58) and stayed ~5.2s. [F1]
- portfolio-service p99 rose from ~185ms (08:56) to 6963ms (08:58) and stayed ~7s. [F2]
- ledger-db CPU rose from ~30% (08:56) to ~96% (08:58) and stayed saturated. [F3]
- ledger-db slow queries went from ~1/min to ~140/min. [F4]
- The dominant slow query is a Seq Scan on trades (rows=48211907) attributed to application_name=portfolio-service, first at 08:58:38. [F5]
- portfolio-service dep-5905 v4.5.0 rolled out at 08:56:32 adding an unindexed trades query with 'no migration included'. [F6]
- The only config change was ledger-db autovacuum_vacuum_scale_factor 0.2->0.1 at 08:00. [F7]
- order-service errors are all ledger-db INSERT INTO orders statement timeouts; error_rate ~9.4%, pool 14->46. [F8]
- portfolio-service db pool pegged at 50; its error_rate stayed flat (~0.2%). [F9]
- ledger-db is shared by order-service, portfolio-service, payment-service, settlement-service. [F10]
- payment-service and settlement-service latencies stayed flat. [F11]
- ledger-db disk usage (~71%) and replication lag (~40ms) were normal. [F12]
- HIST-111 precedent: unindexed report query caused trades seq scans / high db cpu. [F13]

## Assumptions (not verified)
- The deploy's new 'query on trades filtered by account_id + executed_at' is the literal source of the logged Seq Scan query (text matches).
- order-service INSERT INTO orders and portfolio SELECT share the same ledger-db instance and contend for its CPU/connections.
- The 2-minute metric buckets are aligned such that the 08:56 bucket predates the deploy's effect, making 08:58 the first affected bucket.

## Timeline
- `2026-05-05T08:00:00Z` DBA config change cfg-2903 on ledger-db: autovacuum_vacuum_scale_factor 0.2 -> 0.1 (more aggressive autovacuum). No metric change follows. [F7]
- `2026-05-05T08:56:00Z` portfolio-service deploy dep-5905 (v4.5.0) adds a new unindexed query on trades; no migration included. [F6]
- `2026-05-05T08:56:32Z` dep-5905 rollout complete, all pods ready. [F6]
- `2026-05-05T08:58:00Z` First bad metric bucket: portfolio p99 185->6963ms, order p99 151->2878ms, ledger-db CPU 30->96%, slow queries spike. [F1, F2, F3, F4, F14]
- `2026-05-05T08:58:38Z` First ledger-db slow query: Seq Scan on trades (rows=48211907) application_name=portfolio-service. [F5]
- `2026-05-05T09:00:42Z` First order-service error: ledger-db statement timeout on INSERT INTO orders. [F8]
- `2026-05-05T09:10:00Z` Incident INC-009 reported (SEV-1); condition persists through 10:59 with no recovery. [F1]

## Recommended actions
- **[immediate]** Roll back portfolio-service to the previous version (pre-v4.5.0) to stop the seq-scan workload on ledger-db and confirm ledger-db CPU and order-service latency recover. -- Removal of the offending query is the fastest way to restore the shared database; the fault was introduced by dep-5905. [F6, F3, F1]
- **[short_term]** Add a composite index on trades(account_id, executed_at) (or equivalent) and deploy it via a proper migration, then re-introduce the query. -- The Seq Scan on 48M rows is the direct cause; the change summary noted 'no migration included'. Mirrors HIST-111 resolution. [F5, F13, F6]
- **[long_term]** Add a CI/query-review gate that blocks unindexed full-table scans on large shared tables before deployment. -- Prevents recurrence; HIST-111's resolution explicitly called for a query review gate. [F13]
- **[long_term]** Isolate heavy analytical/reporting queries from the transactional order path (separate replica or resource-group for portfolio/reporting). -- Order placement shares a single CPU-bound ledger-db with portfolio reporting; isolation prevents a reporting regression from taking down order placement. [F10, F8]

## Data gaps
- No EXPLAIN plan or schema/index listing for the trades table was available; the absence of the index is inferred from the logged 'Seq Scan on trades' and the deploy's 'no migration included'.
- Ledger-db logs only expose WARN-level slow-query entries after 08:58; no autovacuum activity logs were returned, so the 08:00 config change's immediate behavior could not be directly observed (only that CPU stayed flat until 08:56).
- No per-query connection/waits attribution on ledger-db to prove exactly which callers were starved vs. served; order-service starvation is inferred from its own timeout errors plus overall connection saturation.

## Investigation stats
- latency 103.68s, LLM calls 9, tokens 197063 (in 179574 / out 17489)
- tool calls 37 (1 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
