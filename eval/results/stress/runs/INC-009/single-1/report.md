# Incident investigation report: INC-009
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
order-service p99 >5s and ledger-db statement timeouts are collateral damage from a shared-database CPU saturation caused by portfolio-service deployment dep-5905 (v4.5.0, 2026-05-05T08:56). The release added a new holdings P&L query — "SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2" — that runs as a full Seq Scan on the 48M-row trades table (no supporting index; the deploy notes "no migration included"). These slow full-table scans drove ledger-db CPU from ~30% to ~96% and active connections from ~62 to ~190, saturating the shared Postgres instance. order-service (no release this week, flat CPU/memory) then began timing out on its own writes ("canceling statement due to statement timeout (INSERT INTO orders)"). The DBA autovacuum change (cfg-2903, 08:00) is a red herring: it preceded onset by ~56 minutes and disk usage stayed flat (~71%).

## Probable root cause
**portfolio-service deploy dep-5905 added an unindexed trades query that saturated shared ledger-db** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service deploy dep-5905 added an unindexed trades query that saturated shared ledger-db | bad_deployment | portfolio-service | dep-5905 | 0.86 (high) | active |
| 2 | H2: DBA autovacuum config change on ledger-db caused the saturation | config_change | ledger-db | cfg-2903 | 0.08 (low) | rejected |
| 3 | H3: order-service resource exhaustion (pool/CPU/memory) | resource_exhaustion | order-service | - | 0.05 (low) | rejected |
| 4 | H4: Load-driven capacity saturation on the shared database | capacity_saturation | ledger-db | - | 0.05 (low) | rejected |

### H1: portfolio-service deploy dep-5905 added an unindexed trades query that saturated shared ledger-db
portfolio-service v4.5.0 (dep-5905, 08:56) introduced a new holdings P&L query 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2' with no supporting index ('no migration included'). Under normal request rate it executes as a Seq Scan over the 48M-row trades table, driving ledger-db CPU to ~96%, active connections from ~62 to ~190 and slow queries from ~1/min to ~140/min. The shared ledger-db becomes the bottleneck, so order-service's own writes (INSERT INTO orders) queue and hit statement timeouts, producing p99 >5s and 5xx. order-service is only where the symptom was noticed.

**Causal chain**
1. dep-5905 deploys portfolio-service v4.5.0 at 08:56 with a new trades query and no index migration [F9, F10]
2. New query runs as Seq Scan on 48M-row trades table in ledger-db, attributed application_name=portfolio-service [F7, F8]
3. ledger-db CPU 30%->96%, connections ~62->~190, slow queries ~1->~140/min from the 08:50 bucket [F4, F5, F6]
4. Shared ledger-db saturation delays order-service INSERTs -> statement timeouts, p99 >5s (order-service CPU/memory flat, no release) [F1, F2, F11, F14]

**Supporting evidence**
  - `F1` ✔ order-service p99 latency was ~150ms through 08:40 and broke to 694ms (max 5608ms) in the 08:50 bucket, then 5236ms at 09:00; it stayed ~5.2s through the end of data.
  - `F3` ✔ portfolio-service p99 latency broke in the 08:50 bucket (avg 1539ms, max 7057ms) from ~180ms baseline, then ~7000ms from 09:00 onward.
  - `F4` ✔ ledger-db CPU rose from ~30% baseline to 96.36% max in the 08:50 bucket and ~96% thereafter.
  - `F5` ✔ ledger-db active connections rose from ~62 baseline to ~190-200 in and after the 08:50 bucket.
  - `F6` ✔ ledger-db slow_queries_per_min rose from ~1/min baseline to 139.68 max in the 08:50 bucket and ~140/min thereafter.
  - `F7` ✔ ledger-db WARN logs show slow queries of the exact form 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 2026-05-05T08:58:38Z with 360 matches.
  - `F8` ✔ A ledger-db slow-query log at 08:58:38 is attributed to application_name=portfolio-service, i.e. the full-table scan originates from portfolio-service, not order-service.
  - `F9` ✔ The only deployment in the window is dep-5905, portfolio-service v4.5.0, deployed_at 2026-05-05T08:56:00Z, summary 'Add realised P&L breakdown to holdings API', changes ['new query on trades filtered by account_id + executed_at','no migration included'], status live, not rolled back.
  - `F10` ✔ portfolio-service logs record the rollout completing at 08:56:32Z and the first 'holdings P&L query took 7459ms for account 38649' warning at 08:59:39Z.
  - `F13` ✔ ledger-db disk_used_pct stayed flat around 71% across the whole window (08:50 bucket avg 71.48, max 72.48), showing no disk/bloat symptom correlating with onset.
  - `F14` ✔ order-service CPU stayed flat ~40% and memory ~60% throughout the window; no resource buildup on order-service.
  - `F15` ✔ portfolio-service request_rate was flat ~570 req/s before and after onset (08:48 avg 569.06; 09:00 avg 576.55), ruling out a traffic surge.
  - `F17` ✔ order-service depends on shared ledger-db (kind db); portfolio-service also depends on the same shared ledger-db node (has_telemetry true).
**Contradicting evidence**
  - (none)
**Assumptions**
  - The new query has no index on (account_id, executed_at) so Postgres chooses a seq scan
  - order-service and portfolio-service share the same physical ledger-db instance
  - Baseline attestations of ~150ms order-service p99 reflect pre-incident SLO

### H2: DBA autovacuum config change on ledger-db caused the saturation
cfg-2903 changed ledger-db autovacuum_vacuum_scale_factor from 0.2 to 0.1 at 08:00; more aggressive autovacuum could consume CPU/IO and bloat the planner statistics, slowing queries on ledger-db including order-service INSERTs.

**Causal chain**
1. autovacuum change at 08:00 [F12]
2. ledger-db CPU saturation at 08:50 (56 min later, uncorrelated) [F4]

**Supporting evidence**
  - `F12` ✔ The only config change in the window is cfg-2903: ledger-db autovacuum_vacuum_scale_factor 0.2 -> 0.1, changed_at 2026-05-05T08:00:00Z by dba-team.
**Contradicting evidence**
  - `F13` ✔ ledger-db disk_used_pct stayed flat around 71% across the whole window (08:50 bucket avg 71.48, max 72.48), showing no disk/bloat symptom correlating with onset.
  - `F4` ✔ ledger-db CPU rose from ~30% baseline to 96.36% max in the 08:50 bucket and ~96% thereafter.
  - `F6` ✔ ledger-db slow_queries_per_min rose from ~1/min baseline to 139.68 max in the 08:50 bucket and ~140/min thereafter.
  - `F9` ✔ The only deployment in the window is dep-5905, portfolio-service v4.5.0, deployed_at 2026-05-05T08:56:00Z, summary 'Add realised P&L breakdown to holdings API', changes ['new query on trades filtered by account_id + executed_at','no migration included'], status live, not rolled back.
  - `F7` ✔ ledger-db WARN logs show slow queries of the exact form 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service', first seen 2026-05-05T08:58:38Z with 360 matches.
  - `F8` ✔ A ledger-db slow-query log at 08:58:38 is attributed to application_name=portfolio-service, i.e. the full-table scan originates from portfolio-service, not order-service.
**Rejected because:** Precedes onset by ~56 minutes with no change for 50+ min, while onset aligns to the minute with the deploy. Disk usage stayed flat (~71%) so no bloat/disk symptom. The dominant slow query is explicitly a per-request SELECT from portfolio-service, not an autovacuum-induced effect.

### H3: order-service resource exhaustion (pool/CPU/memory)
order-service's own resources (db connection pool, CPU, memory) exhausted, causing its INSERTs to time out.

**Causal chain**
1. order-service local resource exhaustion *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F14` ✔ order-service CPU stayed flat ~40% and memory ~60% throughout the window; no resource buildup on order-service.
  - `F11` ✔ order-service had zero deployments in the window ('count':0, scope order-service), confirming no order-service release this week.
  - `F2` ✔ order-service emitted 284 ERROR entries 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)', first seen 2026-05-05T09:00:42Z.
  - `F1` ✔ order-service p99 latency was ~150ms through 08:40 and broke to 694ms (max 5608ms) in the 08:50 bucket, then 5236ms at 09:00; it stayed ~5.2s through the end of data.
**Rejected because:** order-service CPU flat ~40% and memory flat ~60%; no deploy or config change on order-service. Its db_pool_in_use rose (13->46) only in response to slow ledger-db, and the timeout is on the server side ('canceling statement due to statement timeout'), consistent with downstream DB saturation, not local exhaustion.

### H4: Load-driven capacity saturation on the shared database
Incoming traffic grew beyond provisioned ledger-db capacity, saturating CPU/connections.

**Causal chain**
1. traffic surge exceeds capacity *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F15` ✔ portfolio-service request_rate was flat ~570 req/s before and after onset (08:48 avg 569.06; 09:00 avg 576.55), ruling out a traffic surge.
  - `F9` ✔ The only deployment in the window is dep-5905, portfolio-service v4.5.0, deployed_at 2026-05-05T08:56:00Z, summary 'Add realised P&L breakdown to holdings API', changes ['new query on trades filtered by account_id + executed_at','no migration included'], status live, not rolled back.
**Rejected because:** request_rate into portfolio-service and order-service was flat across onset (~570 req/s and unchanged), and there was a discrete deployment change rather than a gradual load ramp.

## Facts (verified observations)
- order-service p99 latency broke from ~150ms to 694ms (max 5608ms) in the 08:50 bucket and stayed ~5.2s. [F1]
- order-service logged 284 'canceling statement due to statement timeout (INSERT INTO orders)' errors starting 09:00:42. [F2]
- portfolio-service p99 latency broke in the same 08:50 bucket (avg 1539ms, max 7057ms) and stayed ~7000ms. [F3]
- ledger-db CPU rose from ~30% to ~96% beginning in the 08:50 bucket. [F4]
- ledger-db active connections rose from ~62 to ~190-200 beginning in the 08:50 bucket. [F5]
- ledger-db slow_queries_per_min rose from ~1 to ~140 beginning in the 08:50 bucket. [F6]
- ledger-db slow queries are 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2' executed as Seq Scan on 48,211,907 rows, application_name=portfolio-service. [F7, F8]
- The only deployment in the window is dep-5905 portfolio-service v4.5.0 at 08:56, whose changes are a new trades query with no migration, not rolled back. [F9]
- portfolio-service logged 'deployment dep-5905 (v4.5.0) rollout complete' at 08:56:32 and first slow holdings P&L query (7459ms) at 08:59:39. [F10]
- order-service had zero deployments in the window. [F11]
- The only config change is cfg-2903 (ledger-db autovacuum_vacuum_scale_factor 0.2->0.1) at 08:00. [F12]
- ledger-db disk_used_pct stayed flat near 71% throughout. [F13]
- order-service CPU (~40%) and memory (~60%) stayed flat throughout. [F14]
- portfolio-service request_rate stayed flat ~570 req/s across onset. [F15]
- order-service and portfolio-service both depend on the shared ledger-db. [F17]

## Assumptions (not verified)
- order-service and portfolio-service share the same physical ledger-db instance (both have db edges to the shared ledger-db node).
- The new dep-5905 query has no index on (account_id, executed_at), so Postgres chooses a 48M-row Seq Scan.
- Pre-incident p99 of ~150ms (order-service) and ~180ms (portfolio-service) are the normal baselines.
- The 'new query on trades filtered by account_id + executed_at' in dep-5905 corresponds to the logged 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2' slow query.

## Timeline
- `2026-05-05T08:00:00Z` DBA config change cfg-2903 applied to ledger-db (autovacuum_vacuum_scale_factor 0.2->0.1). No symptom follows for ~50 min. [F12]
- `2026-05-05T08:50:00Z` Onset bucket: order-service p99 150ms->694ms, portfolio-service p99 ->1539ms, ledger-db CPU ->96%, connections ->200, slow queries ->140/min. [F1, F3, F4, F5, F6]
- `2026-05-05T08:56:00Z` Deployment dep-5905 portfolio-service v4.5.0 goes live (new trades query, no index migration). [F9]
- `2026-05-05T08:56:32Z` portfolio-service logs 'deployment dep-5905 (v4.5.0) rollout complete, all pods ready'. [F10]
- `2026-05-05T08:58:38Z` First ledger-db slow query (8690ms, Seq Scan on trades, application_name=portfolio-service). [F7, F8]
- `2026-05-05T08:59:39Z` portfolio-service first 'holdings P&L query took 7459ms' warning. [F10]
- `2026-05-05T09:00:42Z` order-service first 'canceling statement due to statement timeout (INSERT INTO orders)' error. [F2]
- `2026-05-05T09:10:00Z` Incident reported (INC-009) by on-call SRE. [F2]
- `2026-05-05T10:59:00Z` Latest data: services still degraded; dep-5905 still live, not rolled back. [F9, F1]

## Recommended actions
- **[immediate]** Roll back portfolio-service deployment dep-5905 (v4.5.0) to the prior version to remove the unindexed trades query immediately. -- Only change coincident with onset; removing it should stop the Seq Scans and relieve shared ledger-db CPU. [F9, F7, F3, F4]
- **[short_term]** Add a supporting index on trades(account_id, executed_at) (CREATE INDEX CONCURRENTLY) and re-check the query plan before re-releasing the feature. -- The new query scans the 48M-row trades table; an index lets the plan avoid full scans. [F7, F9]
- **[short_term]** Add per-application workloads / statement_timeout guardrails and query-plan checks for shared ledger-db so one service cannot saturate the shared instance. -- A single service's unindexed query starved order-service writes on the shared DB. [F17, F2, F5]
- **[long_term]** Do not revert cfg-2903 on the basis of this incident; keep autovacuum change but verify with DBA that it played no role. -- Autovacuum change preceded onset by ~56 min and disk stayed flat, so it is not the cause. [F12, F13]
- **[long_term]** Add a deployment-level gate that flags new queries with no accompanying migration/index for shared DBs, and alert on ledger-db CPU/connections/slow-query rate. -- Prevent recurrence of unindexed-query-on-shared-DB class of failures. [F4, F6, F9]

## Data gaps
- Historical-incident search backend returned 503 (tc-019), so no comparison to past similar incidents was possible.
- A ledger-db slow_queries_per_min re-query around 08:36-09:10 failed with 503 (tc-025); finer-grained onset timing of slow queries is unavailable beyond the 08:50 bucket and the first log at 08:58:38.
- No visibility into the trades table's index definitions or query plan history to confirm the migration/index gap directly.
- No direct telemetry that order-service and portfolio-service share the same ledger-db instance vs separate schemas; inferred from the shared-node dependency graph.
- Postgres logs for ledger-db only show WARN-level slow queries; no autovacuum/IO metrics to fully quantify autovacuum impact (disk metric only).

## Investigation stats
- latency 68.79s, LLM calls 6, tokens 94264 (in 84168 / out 10096)
- tool calls 27 (4 failed, 0 partial, 7 recovered by retry), findings 18/18 verified
