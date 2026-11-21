# Incident investigation report: INC-009
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
order-service's p99 (~5.2s) and its statement timeouts on "INSERT INTO orders" are the downstream symptom of ledger-db contention caused by an un-indexed query shipped in portfolio-service v4.5.0 (dep-5905, deployed 08:56:00Z). That deploy, which added a query on trades filtered by account_id + executed_at with no migration, coincides with the degradation onset (08:52-08:56Z bucket), while ledger-db's only slow-query signature is "Seq Scan on trades (rows=48211907)" attributed to application_name=portfolio-service — a signature autovacuum does not produce. ledger-db is a shared DB used directly by order-service, portfolio-service, payment-service and settlement-service, so one caller's full-table scans starve the INSERT path of the others; order-service errors only start at 09:00:42Z, ~4 minutes after the DB-level spike. The DBA autovacuum change (cfg-2903, 08:00:00Z) is rejected on timing: a ~52-minute quiet gap between the change and any measurable DB effect, with client latency inflecting exactly at the deploy. Confidence is medium (0.60) because the causal index assertion and the absence of a host-level ledger-db fault rest on unretrieved schema/index inventory and unqueried DB resource metrics.

## Probable root cause
**New un-indexed trades query in portfolio-service v4.5.0 drives Seq Scans on ledger-db, saturating the shared DB and timing out other callers** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: New un-indexed trades query in portfolio-service v4.5.0 drives Seq Scans on ledger-db, saturating the shared DB and timing out other callers | bad_deployment | portfolio-service | dep-5905 | 0.60 (medium) | active |
| 2 | H3: ledger-db host/instance saturation (IOPS throttling, capacity) independent of any application change | infrastructure_failure | ledger-db | null | 0.12 (low) | active |
| 3 | H4: Concurrent load/lock contention or traffic increase on ledger-db unrelated to the new query (e.g. reporting job, long-running transaction) | data_issue | ledger-db | null | 0.10 (low) | active |
| 4 | H2: DBA autovacuum change cfg-2903 on ledger-db degraded query performance (reporter's suspicion) | config_change | ledger-db | cfg-2903 | 0.08 (low) | rejected |

### H1: New un-indexed trades query in portfolio-service v4.5.0 drives Seq Scans on ledger-db, saturating the shared DB and timing out other callers
dep-5905 (portfolio-service v4.5.0, deployed 08:56:00Z) added a query on trades filtered by account_id + executed_at with no index/migration. On the 48M-row trades table this becomes a full Seq Scan, producing ~70-148 slow_queries/min on ledger-db and roughly doubling active connections (~60 -> ~127-204). The shared ledger-db (used directly by order-service, portfolio-service, payment-service and settlement-service) becomes IO/connection contended, so order-service's plain INSERT INTO orders waits behind the scan traffic and is cancelled by statement timeout at 09:00:42Z (284 errors to 10:58Z), surfacing as p99 ~5.2s. Origin is the deployment, not order-service and not autovacuum.

**Causal chain**
1. dep-5905 deploys portfolio-service v4.5.0 with a new query on trades (account_id + executed_at) and no migration/index [F2, F13]
2. The un-indexed predicate forces Seq Scan on trades (rows=48,211,907) in ledger-db, logged as slow queries with application_name=portfolio-service [F9]
3. ledger-db slow_queries_per_min jumps ~0.77-1.61 -> ~70-148/min and connections_active ~60 -> ~127-204 in the 08:56Z bucket, concurrently with the deploy [F11, F13]
4. Seq-scan IO + connection pressure on the shared DB delays unrelated writes from co-tenants; order-service's db_pool_in_use pins at ~44-48 while INSERTs block [F12, F16]
5. order-service INSERT INTO orders exceeds statement timeout -> canceling statement due to statement timeout, first 09:00:42Z, 284 occurrences to 10:58Z; p99 steps to ~5.2s [F8, F7, F18]
6. portfolio-service, the deployer itself, shows the earliest and largest degradation (~3574ms then ~7.0s from the same 08:56Z bucket), consistent with the offending caller being worst hit [F10]
7. All services reaching the shared ledger-db (order-service, portfolio-service, payment-service, settlement-service) are exposed, explaining cross-service symptoms with no order-service release [F4, F16]

**Supporting evidence**
  - `F2` ✔ The only deployment across ALL services in the window is dep-5905: portfolio-service v4.5.0 deployed 2026-05-05T08:56:00Z by k.shah, adding 'new query on trades filtered by account_id + executed_at', 'no migration included', status 'live', rolled_back_at null — i.e. NOT rolled back as of current data.
  - `F9` ✔ ledger-db slow queries start EARLIER than the order-service symptom: first WARN at 08:58:38Z, and every slow-query log line is 'Seq Scan on trades (rows=48211907)' with application_name=portfolio-service - none are attributed to order-service.
  - `F10` ✔ portfolio-service shows the earliest and largest latency degradation: p99 flat at ~173-194 ms through 08:52Z, then 08:56Z bucket avg 3574.3 ms / max 7056.7 ms, rising to ~6.7-7.2 s from 09:00Z - roughly 1.3 s worse than order-service and starting in the same bucket (08:56Z).
  - `F11` ✔ ledger-db itself degraded at the same 08:56Z bucket: slow_queries_per_min jumped from ~0.77-1.61 baseline to avg 70.42 (max 139.68) at 08:56Z and ~135-148/min thereafter; simultaneously connections_active rose from ~60-64 to avg 127.45 (max 199.81) at 08:56Z and ~185-204 after 09:00Z. So the DB-level symptom predates the order-service ERROR logs by ~4 minutes.
  - `F12` ✔ order-service db_pool_in_use saturated concurrently: flat ~13.5-14.4 connections through 08:52Z, then avg 21.72 (max 45.07) at 08:56Z and pinned at ~44-48 (max 51.63) from 09:00Z - consistent with pooled connections held open by blocked INSERT INTO orders statements rather than a pool leak before 08:56Z.
  - `F13` ✔ Only two changes exist in the entire 2026-05-04..2026-05-05 window: cfg-2903 on ledger-db (autovacuum_vacuum_scale_factor 0.2->0.1 at 2026-05-05T08:00:00Z, author dba-team) and dep-5905 on portfolio-service (v4.5.0 at 2026-05-05T08:56:00Z, author k.shah, 'new query on trades filtered by account_id + executed_at', 'no migration included', status 'live', rolled_back_at null). Both predate the 09:10Z report; ordering alone does not discriminate, so timing match to the 08:52-08:56Z onset must.
  - `F15` ✔ The portfolio-service deployment and the symptom onset coincide: dep-5905 deployed 2026-05-05T08:56:00Z carries a 'new query on trades' with no index/migration; ledger-db's slow-query WARNs (all 'Seq Scan on trades (rows=48211907)', application_name=portfolio-service, first at 08:58:38Z) and the p99 inflection for both order-service (08:56Z bucket -> ~1515ms then ~5.2s) and portfolio-service (08:56Z bucket -> ~3574ms then ~7.0s) all fall within the same 08:52-08:56Z window.
  - `F16` ✔ The shared-component chain explains cross-service impact and predicts which callers are hit. ledger-db ('shared':true, type postgres) is a direct 'db' dependency of order-service, portfolio-service, payment-service and settlement-service, and order-service reaches it directly (no ledger-service in the graph). So DB contention from one caller propagates to all four; the observed pattern (portfolio-service worst at ~7.0s, order-service ~5.2s, both starting in the same 08:56Z bucket) matches a shared-DB cause rather than an order-service-local fault.
  - `F17` ✔ Historical precedent directly matches the supported theory (leads only, not evidence for this incident): HIST-111 (2025-05-22) 'Reporting job full table scans slowed ledger-db', services [ledger-db, settlement-service], root_cause 'New report query without index did sequential scans', category 'bad_deployment' - i.e. an un-indexed report query causing Seq Scans on ledger-db, the same signature seen now. The other incident mentioning autovacuum was not retrievable (last search hit the tool-call budget).
  - `F18` ✔ Failure signature is a DB statement timeout propagated to the client layer, not an order-service fault: order-service's only ERROR pattern is 'canceling statement due to statement timeout (INSERT INTO orders)' (first 09:00:42Z, 284 total to 10:58Z), i.e. order-service is a victim of ledger-db write contention, while the originating load is portfolio-service's trade Seq Scans. payment-service and settlement-service share the same ledger-db edge and would be expected to degrade similarly.
**Contradicting evidence**
  - (none)
**Assumptions**
  - portfolio-service's new query truly lacks a supporting index on ledger-db trades (deploy description says 'no migration included'; no schema/index inventory was retrieved)
  - The Seq Scans at 08:58:38Z originate from that new query and not from a pre-existing reporting/job path (application_name only, no query text captured)
  - ledger-db contention (IO/connections/locks) rather than an unrelated DB-host problem is what blocks the INSERTs; no ledger-db cpu_pct/IOPS metrics were queried

### H3: ledger-db host/instance saturation (IOPS throttling, capacity) independent of any application change
The ledger-db database host itself became the bottleneck — storage IOPS throttling or provisioned capacity saturation — producing slow queries, gauge-high connections and statement timeouts for every direct caller including order-service. Origin is infrastructure/capacity at ledger-db, not application code. Weakened but not fully excluded: the same history pattern (HIST-110 'Slow orders from ledger-db IOPS throttling') exists, and no cpu_pct/disk_used_pct/IOPS metric for ledger-db was ever retrieved, so a pure host-level fault cannot be positively excluded. It remains a live alternative because the observable signature (seq scans -> IO pressure) is also what an under-provisioned host would show.

**Causal chain**
1. ledger-db storage/host loses headroom (IOPS throttling or capacity saturation) in the 08:52-08:56Z window *(assumed)*
2. Query latency and concurrent connection count on ledger-db climb (slow_queries ~70-148/min, connections_active ~127-204) [F11]
3. Blocked/queued writes from all ledger-db callers, including order-service's INSERT INTO orders, exceed statement timeout [F8, F18]
4. Every direct ledger-db caller degrades simultaneously (portfolio-service worst at ~7.0s, order-service ~5.2s, both from the 08:56Z bucket) [F10, F7, F16]

**Supporting evidence**
  - `F11` ✔ ledger-db itself degraded at the same 08:56Z bucket: slow_queries_per_min jumped from ~0.77-1.61 baseline to avg 70.42 (max 139.68) at 08:56Z and ~135-148/min thereafter; simultaneously connections_active rose from ~60-64 to avg 127.45 (max 199.81) at 08:56Z and ~185-204 after 09:00Z. So the DB-level symptom predates the order-service ERROR logs by ~4 minutes.
  - `F16` ✔ The shared-component chain explains cross-service impact and predicts which callers are hit. ledger-db ('shared':true, type postgres) is a direct 'db' dependency of order-service, portfolio-service, payment-service and settlement-service, and order-service reaches it directly (no ledger-service in the graph). So DB contention from one caller propagates to all four; the observed pattern (portfolio-service worst at ~7.0s, order-service ~5.2s, both starting in the same 08:56Z bucket) matches a shared-DB cause rather than an order-service-local fault.
  - `F6` ✔ History leads (not evidence for this incident): HIST-110 (2026-01-15) 'Slow orders from ledger-db IOPS throttling' with order-service+ledger-db and symptoms 'order latency','statement timeouts' (root_cause IOPS throttling); HIST-111 (2025-05-22) 'Reporting job full table scans slowed ledger-db' (ledger-db+settlement-service, bad_deployment); HIST-102 portfolio timeouts during ledger-db failover.
**Contradicting evidence**
  - `F9` ✔ ledger-db slow queries start EARLIER than the order-service symptom: first WARN at 08:58:38Z, and every slow-query log line is 'Seq Scan on trades (rows=48211907)' with application_name=portfolio-service - none are attributed to order-service.
  - `F2` ✔ The only deployment across ALL services in the window is dep-5905: portfolio-service v4.5.0 deployed 2026-05-05T08:56:00Z by k.shah, adding 'new query on trades filtered by account_id + executed_at', 'no migration included', status 'live', rolled_back_at null — i.e. NOT rolled back as of current data.
  - `F15` ✔ The portfolio-service deployment and the symptom onset coincide: dep-5905 deployed 2026-05-05T08:56:00Z carries a 'new query on trades' with no index/migration; ledger-db's slow-query WARNs (all 'Seq Scan on trades (rows=48211907)', application_name=portfolio-service, first at 08:58:38Z) and the p99 inflection for both order-service (08:56Z bucket -> ~1515ms then ~5.2s) and portfolio-service (08:56Z bucket -> ~3574ms then ~7.0s) all fall within the same 08:52-08:56Z window.
  - `F13` ✔ Only two changes exist in the entire 2026-05-04..2026-05-05 window: cfg-2903 on ledger-db (autovacuum_vacuum_scale_factor 0.2->0.1 at 2026-05-05T08:00:00Z, author dba-team) and dep-5905 on portfolio-service (v4.5.0 at 2026-05-05T08:56:00Z, author k.shah, 'new query on trades filtered by account_id + executed_at', 'no migration included', status 'live', rolled_back_at null). Both predate the 09:10Z report; ordering alone does not discriminate, so timing match to the 08:52-08:56Z onset must.
**Assumptions**
  - ledger-db host experienced an IOPS/capacity event at ~08:56Z that is not attributable to query mix
  - No ledger-db metric (cpu_pct, disk_used_pct, replication_lag_ms, IOPS) was queried, so this is unmeasured

### H4: Concurrent load/lock contention or traffic increase on ledger-db unrelated to the new query (e.g. reporting job, long-running transaction)
Something other than dep-5905's new query — a reporting/batch job, a long-running transaction holding locks, or a genuine traffic increase — pushed ledger-db past its headroom and produced the statement timeouts. Origin is data/load behaviour on ledger-db. This is the residual 'contention but not the new code' branch; it is retained only at low confidence because the only slow-query signature captured attributes the load to portfolio-service Seq Scans, and there is a same-signature precedent (HIST-111) resolved as an un-indexed report query.

**Causal chain**
1. An unindexed or lock-holding access path (report/backfill/long transaction) starts exercising the trades table heavily at ~08:56Z *(assumed)*
2. ledger-db slow queries and active connections spike (F11), affecting shared callers [F11]
3. order-service INSERT INTO orders blocks and times out, p99 -> ~5.2s [F8, F7]

**Supporting evidence**
  - `F9` ✔ ledger-db slow queries start EARLIER than the order-service symptom: first WARN at 08:58:38Z, and every slow-query log line is 'Seq Scan on trades (rows=48211907)' with application_name=portfolio-service - none are attributed to order-service.
  - `F11` ✔ ledger-db itself degraded at the same 08:56Z bucket: slow_queries_per_min jumped from ~0.77-1.61 baseline to avg 70.42 (max 139.68) at 08:56Z and ~135-148/min thereafter; simultaneously connections_active rose from ~60-64 to avg 127.45 (max 199.81) at 08:56Z and ~185-204 after 09:00Z. So the DB-level symptom predates the order-service ERROR logs by ~4 minutes.
**Contradicting evidence**
  - `F2` ✔ The only deployment across ALL services in the window is dep-5905: portfolio-service v4.5.0 deployed 2026-05-05T08:56:00Z by k.shah, adding 'new query on trades filtered by account_id + executed_at', 'no migration included', status 'live', rolled_back_at null — i.e. NOT rolled back as of current data.
  - `F15` ✔ The portfolio-service deployment and the symptom onset coincide: dep-5905 deployed 2026-05-05T08:56:00Z carries a 'new query on trades' with no index/migration; ledger-db's slow-query WARNs (all 'Seq Scan on trades (rows=48211907)', application_name=portfolio-service, first at 08:58:38Z) and the p99 inflection for both order-service (08:56Z bucket -> ~1515ms then ~5.2s) and portfolio-service (08:56Z bucket -> ~3574ms then ~7.0s) all fall within the same 08:52-08:56Z window.
**Assumptions**
  - Such a job exists and started at ~08:56Z coincidentally with dep-5905; no job/transaction evidence was retrieved
  - The attributed application_name=portfolio-service in the slow-query log could come from portfolio-service code other than the new v4.5.0 query

### H2: DBA autovacuum change cfg-2903 on ledger-db degraded query performance (reporter's suspicion)
cfg-2903 lowered autovacuum_vacuum_scale_factor from 0.2 to 0.1 at 08:00:00Z, making autovacuum more aggressive; the extra vacuum IO on ledger-db would slow queries on ledger tables, delaying order-service's INSERT INTO orders until statement timeout and raising p99. Origin is the config change on ledger-db. This is rejected on timing: ledger-db and both clients held baseline until the 08:56Z bucket, a ~52-minute quiet gap after the change, and the observed slow queries are Seq Scans by portfolio-service, a signature autovacuum does not produce.

**Causal chain**
1. cfg-2903 changes autovacuum_vacuum_scale_factor 0.2 -> 0.1 on ledger-db at 08:00:00Z (author dba-team, 'More aggressive autovacuum') [F1, F13]
2. More aggressive autovacuum triggers additional vacuum runs on ledger tables, consuming IO/CPU and slowing concurrent queries *(assumed)*
3. Slowed ledger-db queries delay order-service's INSERT INTO orders past the statement timeout *(assumed)*
4. order-service p99 rises to ~5.2s and 284 statement-timeout errors appear on INSERT INTO orders [F8, F7]

**Supporting evidence**
  - `F1` ✔ The only config change in the 2026-05-04..2026-05-05 window is cfg-2903 on ledger-db: autovacuum_vacuum_scale_factor changed from 0.2 to 0.1 at 2026-05-05T08:00:00Z by author 'dba-team', described as 'More aggressive autovacuum'. No affected-tables list was returned for this change.
  - `F13` ✔ Only two changes exist in the entire 2026-05-04..2026-05-05 window: cfg-2903 on ledger-db (autovacuum_vacuum_scale_factor 0.2->0.1 at 2026-05-05T08:00:00Z, author dba-team) and dep-5905 on portfolio-service (v4.5.0 at 2026-05-05T08:56:00Z, author k.shah, 'new query on trades filtered by account_id + executed_at', 'no migration included', status 'live', rolled_back_at null). Both predate the 09:10Z report; ordering alone does not discriminate, so timing match to the 08:52-08:56Z onset must.
**Contradicting evidence**
  - `F8` ✔ First order-service ERROR log is at 09:00:42Z; the only pattern is statement timeout against ledger-db: 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)'. 284 such errors were found in 08:00-10:59Z (order-service ERROR count = 284), i.e. roughly 1.5/min sustained, last seen 10:58:29Z.
  - `F7` ✔ order-service p99 latency was flat at ~143-166 ms through 08:52Z, then inflected at the 08:56Z bucket to avg 1514.78 ms / max 5607.8 ms and stepped up to ~5.2 s from 09:00Z onward. The 240-second bucket means the true crossing falls inside 08:52-08:56Z.
  - `F11` ✔ ledger-db itself degraded at the same 08:56Z bucket: slow_queries_per_min jumped from ~0.77-1.61 baseline to avg 70.42 (max 139.68) at 08:56Z and ~135-148/min thereafter; simultaneously connections_active rose from ~60-64 to avg 127.45 (max 199.81) at 08:56Z and ~185-204 after 09:00Z. So the DB-level symptom predates the order-service ERROR logs by ~4 minutes.
  - `F12` ✔ order-service db_pool_in_use saturated concurrently: flat ~13.5-14.4 connections through 08:52Z, then avg 21.72 (max 45.07) at 08:56Z and pinned at ~44-48 (max 51.63) from 09:00Z - consistent with pooled connections held open by blocked INSERT INTO orders statements rather than a pool leak before 08:56Z.
  - `F10` ✔ portfolio-service shows the earliest and largest latency degradation: p99 flat at ~173-194 ms through 08:52Z, then 08:56Z bucket avg 3574.3 ms / max 7056.7 ms, rising to ~6.7-7.2 s from 09:00Z - roughly 1.3 s worse than order-service and starting in the same bucket (08:56Z).
  - `F9` ✔ ledger-db slow queries start EARLIER than the order-service symptom: first WARN at 08:58:38Z, and every slow-query log line is 'Seq Scan on trades (rows=48211907)' with application_name=portfolio-service - none are attributed to order-service.
  - `F14` ✔ The autovacuum change timing does NOT match the degradation: cfg-2903 was applied at 08:00:00Z, and a ledger-db-only re-query confirms it is the sole change and nothing else was applied to ledger-db after 08:00Z up to 08:55Z. Per T1/F11, ledger-db stayed at baseline (~0.77-1.61 slow_queries_per_min, ~60-64 connections_active) through 08:52Z and only broke at the 08:56Z bucket (~70.42/min, ~127 connections) - a ~52-minute gap between change and any measurable DB effect.
  - `F15` ✔ The portfolio-service deployment and the symptom onset coincide: dep-5905 deployed 2026-05-05T08:56:00Z carries a 'new query on trades' with no index/migration; ledger-db's slow-query WARNs (all 'Seq Scan on trades (rows=48211907)', application_name=portfolio-service, first at 08:58:38Z) and the p99 inflection for both order-service (08:56Z bucket -> ~1515ms then ~5.2s) and portfolio-service (08:56Z bucket -> ~3574ms then ~7.0s) all fall within the same 08:52-08:56Z window.
**Assumptions**
  - Autovacuum actually ran and consumed IO between 08:00 and 08:56Z — never measured (no autovacuum-activity or bloat metric retrieved)
  - cfg-2903 touched the trades table or other hot tables — no affected-tables list was returned
**Rejected because:** Timing and mechanism both fail. cfg-2903 was applied at 08:00:00Z yet ledger-db stayed at baseline (~0.77-1.61 slow_queries/min, ~60-64 connections) through the 08:52Z bucket and only broke at 08:56Z — a ~52-minute quiet gap, whereas the observed degradation coincides exactly with dep-5905 at 08:56:00Z (F11, F13, F14, F15). The only slow-query signature is 'Seq Scan on trades (rows=48211907)' with application_name=portfolio-service, which an autovacuum parameter change does not create (F9). The change also predates nothing else and no post-08:00 ledger-db change exists to explain a delayed onset (F14).

## Facts (verified observations)
- The only deployment across all services in the 2026-05-04..2026-05-05 window is dep-5905: portfolio-service v4.5.0 deployed 2026-05-05T08:56:00Z, adding a 'new query on trades filtered by account_id + executed_at' with 'no migration included', status 'live' and not rolled back. [F2, F13]
- The only config change in that window is cfg-2903 on ledger-db: autovacuum_vacuum_scale_factor changed 0.2 -> 0.1 at 2026-05-05T08:00:00Z by dba-team; no affected-tables list was returned. [F1, F13]
- ledger-db slow queries begin earlier than the order-service symptom: first WARN at 08:58:38Z, and every slow-query log line is 'Seq Scan on trades (rows=48211907)' with application_name=portfolio-service; none are attributed to order-service. [F9]
- order-service p99 was flat at ~143-166 ms through the 08:52Z bucket, then inflected at the 08:56Z bucket (avg 1514.78 ms, max 5607.8 ms) and stepped up to ~5.2 s from 09:00Z. [F7]
- portfolio-service showed the earliest and largest latency degradation: flat ~173-194 ms through 08:52Z, then 08:56Z bucket avg 3574.3 ms / max 7056.7 ms, rising to ~6.7-7.2 s from 09:00Z. [F10]
- ledger-db itself degraded in the same 08:56Z bucket: slow_queries_per_min jumped from ~0.77-1.61 baseline to avg 70.42 (max 139.68), and connections_active rose from ~60-64 to avg 127.45 (max 199.81), then ~185-204 after 09:00Z — roughly 4 minutes before the first order-service ERROR. [F11]
- order-service db_pool_in_use saturated concurrently: flat ~13.5-14.4 connections through 08:52Z, then avg 21.72 (max 45.07) at 08:56Z and pinned at ~44-48 from 09:00Z, consistent with pooled connections held by blocked INSERTs rather than a pre-08:56Z pool leak. [F12]
- order-service's only ERROR pattern is 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)', first at 09:00:42Z and 284 occurrences to 10:58:29Z — a DB statement timeout propagated to the client, not an order-service-local fault. [F8]
- order-service connects DIRECTLY to ledger-db (db edge), and ledger-db is shared=true, used by order-service, portfolio-service, payment-service and settlement-service — so contention from one caller propagates to all four. [F3, F4, F16]
- Historical precedent matches the signature: HIST-111 (2025-05-22) 'Reporting job full table scans slowed ledger-db' was resolved as bad_deployment with root cause 'New report query without index did sequential scans' on the same ledger-db. [F17]

## Assumptions (not verified)
- portfolio-service's new trades query truly lacks a supporting index on ledger-db.trades — the deploy description states 'no migration included' but no schema/index inventory was retrieved.
- The Seq Scans at 08:58:38Z originate from the new v4.5.0 query and not a pre-existing reporting/job path; only application_name=portfolio-service was captured, no query text.
- ledger-db contention (IO/connections/locks) rather than an unrelated DB-host problem is what blocks order-service's INSERTs; no ledger-db cpu_pct/disk_used_pct/IOPS metrics were queried.
- The 240-second metric bucket granularity is fine enough that the 08:56Z bucket reflects load beginning at or immediately after the 08:56:00Z deploy.
- payment-service and settlement-service, as the other shared ledger-db callers, would show similar degradation (predicted by F4/F16 but not measured).
- (demoted from facts: no verified observation backs it) The autovacuum change timing does not match the degradation: cfg-2903 was applied at 08:00:00Z and nothing else was applied to ledger-db up to 08:55Z, yet ledger-db stayed at baseline (~0.77-1.61 slow_queries/min, ~60-64 connections) through 08:52Z — a ~52-minute gap before any measurable DB effect.
- (demoted from facts: no verified observation backs it) The deployment and symptom onset coincide: dep-5905 at 08:56:00Z, the ledger-db Seq Scan WARNs (first 08:58:38Z), and the p99 inflection for both order-service and portfolio-service all fall within the 08:52-08:56Z window.

## Timeline
- `2026-05-05T08:00:00Z` cfg-2903 applied to ledger-db: autovacuum_vacuum_scale_factor 0.2 -> 0.1 ('More aggressive autovacuum') by dba-team. No DB-level effect follows for ~52 minutes (F1, F13, F14). [F1, F13, F14]
- `2026-05-05T08:52:00Z` Last healthy bucket: order-service p99 ~143-166 ms, portfolio-service p99 ~173-194 ms, ledger-db ~0.77-1.61 slow_queries/min and ~60-64 connections, order-service pool ~13.5-14.4 (F7, F10, F11, F12). [F7, F10, F11, F12]
- `2026-05-05T08:56:00Z` dep-5905 deployed: portfolio-service v4.5.0 with a new query on trades (account_id + executed_at), 'no migration included' (F2, F15). [F2, F15]
- `2026-05-05T08:56:00Z` Degradation onset in the same bucket: order-service p99 avg 1514.78 ms / max 5607.8 ms; portfolio-service avg 3574.3 ms / max 7056.7 ms; ledger-db slow_queries_per_min avg 70.42 (max 139.68) and connections_active avg 127.45 (max 199.81); order-service db_pool_in_use avg 21.72 (max 45.07) (F7, F10, F11, F12, F15). [F7, F10, F11, F12, F15]
- `2026-05-05T08:58:38Z` First ledger-db slow-query WARN: 'Seq Scan on trades (rows=48211907)' with application_name=portfolio-service — the DB symptom precedes client errors (F9). [F9]
- `2026-05-05T09:00:42Z` First order-service ERROR: 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)'; steady state ~5.2 s p99 and ~44-48 pooled connections in use, slow_queries ~135-148/min and ~185-204 connections at ledger-db (F8, F11, F12, F18). [F8, F11, F12, F18]
- `2026-05-05T09:10:00Z` Incident INC-009 reported by on-call SRE: order-service p99 > 5s with ledger-db statement timeouts (report metadata). []
- `2026-05-05T10:58:29Z` Last observed statement-timeout error (284 total in 08:00-10:59Z, ~1.5/min sustained); dep-5905 still live, not rolled back (F2, F8). [F2, F8]

## Recommended actions
- **[immediate]** Roll back dep-5905 (portfolio-service v4.5.0) to the previous release to stop the un-indexed trades Seq Scans against ledger-db; confirm the rollback is recorded as rolled_back_at on dep-5905. -- The onset of ledger-db slow queries, the Seq Scan WARNs attributed to portfolio-service, and the p99 inflection in both clients all begin at the 08:56Z deploy bucket, while the deploy remains live and the errors are ongoing (F2, F9, F15, F11). [F2, F9, F15, F11]
- **[immediate]** Do NOT revert cfg-2903 (autovacuum_vacuum_scale_factor 0.2 -> 0.1) as the mitigation; leave it in place unless separate evidence emerges, and record 'no action' against it. -- The change was applied at 08:00:00Z but ledger-db stayed at baseline (~0.77-1.61 slow_queries/min, ~60-64 connections) through 08:52Z, and the only slow-query signature is a portfolio-service Seq Scan that autovacuum parameters do not produce (F14, F11, F9, F13). [F14, F11, F9, F13]
- **[short_term]** Watch ledger-db slow_queries_per_min, connections_active, and order-service/portfolio-service p99 + db_pool_in_use for ~15 minutes after rollback; success criterion is slow_queries_per_min returning toward the ~0.77-1.61 baseline and order-service p99 back to ~143-166 ms with no new statement timeouts. -- The pre-onset baselines are known and the affected signals are already instrumented, so recovery is directly measurable (F7, F10, F11, F12). [F7, F10, F11, F12, F8]
- **[short_term]** Ship a migration adding a composite index on ledger-db trades(account_id, executed_at) (CONCURRENTLY), and re-deploy the v4.5.0 query only after the index exists and its use is confirmed via EXPLAIN in a non-prod environment. -- The deploy explicitly shipped 'no migration included' for a filter on account_id + executed_at, and the observed plan is a 48.2M-row Seq Scan; this is the same failure mode resolved in HIST-111 (F2, F9, F15, F17). [F2, F9, F15, F17]
- **[short_term]** Query ledger-db resource metrics (cpu_pct, disk_used_pct, IOPS/throttling, replication_lag_ms) for 07:00-11:00Z to positively exclude an independent host-level capacity event, and check whether payment-service and settlement-service (the other shared ledger-db callers) also degraded after 08:56Z. -- H3 (host/instance saturation) was retained only because no ledger-db resource metric was ever retrieved, and the shared-component explanation predicts all four callers degrade — testing it discriminates H3/H4 from H1 and confirms blast radius (F4, F16, F18). [F4, F16, F18]
- **[long_term]** Add a deployment gate that requires an index/migration plan for any new query against large shared tables (ledger-db trades), and add DB-side slow-query alerting on ledger-db plus query-level statement-timeout alerting for the four shared callers. -- An un-indexed query on a shared DB was re-deployed into production and took 4 minutes to manifest only through another service's timeouts; the same signature recurred in HIST-111 (F9, F17, F11). [F9, F17, F11]
- **[long_term]** Instrument per-query statement-timeout and Seq-Scan counters per client application_name on ledger-db, and document that order-service, portfolio-service, payment-service and settlement-service share one DB so a change in any one is treated as cross-service risk. -- Detection currently depends on a victim service's 5xx/timeout rate; the shared=true ledger-db edge makes cross-tenant impact structural (F3, F4, F16). [F3, F4, F16]

## Data gaps
- No schema/index inventory for ledger-db was retrieved, so the claim that the new trades query lacks a supporting index on account_id + executed_at rests on the deploy description ('no migration included') only.
- No ledger-db resource metrics (cpu_pct, disk_used_pct, IOPS/throttling, replication_lag_ms) were queried, so an independent host-level saturation cause (H3) cannot be positively excluded and the 08:56Z onset cannot be confirmed at metric level.
- Slow-query log lines captured application_name only, no query text, so attribution of the Seq Scans to the specific new v4.5.0 query is an inference, not a direct match.
- portfolio-service WARN/ERROR logs could not be retrieved (budget_exhausted), so it is unknown whether portfolio-service was the first client to error or only the first to register ledger-db slow queries.
- No per-service config queries were run for portfolio-service, payment-service or settlement-service in the 07:00-10:00Z window, and the targeted order-service config query was not executed (budget exhausted) — an additional flag/change could be unlisted.
- cfg-2903 returned no affected-tables list and no autovacuum-activity/bloat metric was inspected, so whether autovacuum actually ran and consumed IO between 08:00 and 08:56Z is unmeasured; H2 is rejected on timing and signature rather than a positive measurement.
- The 240-second bucket granularity leaves the exact onset minute within 08:52-08:56Z unresolved for both clients and ledger-db, so the sub-4-minute ordering between them is not established.
- payment-service and settlement-service latency was never measured, so the shared-component prediction that all four ledger-db callers degrade is unverified.
- A search for a precedent where an autovacuum_vacuum_scale_factor change alone caused latency hit budget_exhausted, so the negative case against the autovacuum theory lacks a documented counterexample.

## What would change this conclusion
- A schema/index inventory showing that ledger-db trades(account_id, executed_at) was already indexed — this would break the mechanism claimed for H1 and shift weight toward H3/H4.
- Explicit query text in ledger-db slow-query logs showing the Seq Scans come from a path other than the new v4.5.0 query (e.g. a pre-existing reporting job).
- ledger-db resource metrics showing a capacity/IOPS-throttling event starting at or before 08:56Z independent of query mix, or a metric-visible saturation before the deploy.
- Reverting dep-5905 without restoring slow_queries_per_min, connections_active and client p99 to baseline — i.e. failure to reproduce the recovery predicted by H1.
- Evidence that slow_queries_per_min and connection counts were already rising before 08:00Z or persistently between 08:00Z and 08:56Z, which would resurrect the autovacuum (H2) timing argument.
- Identification of a reporting/batch job or long-running transaction that started at ~08:56Z coincidentally with dep-5905 and accounts for the Seq Scan load, supporting H4.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: The autovacuum change timing does not match the degradation: cfg-2903 was applie
- fact demoted to assumption: The deployment and symptom onset coincide: dep-5905 at 08:56:00Z, the ledger-db 

## Investigation stats
- latency 109.0s, LLM calls 12, tokens 84895 (in 67220 / out 17675)
- tool calls 24 (1 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
