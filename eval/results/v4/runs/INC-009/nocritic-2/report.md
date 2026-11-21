# Incident investigation report: INC-009
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
order-service's p99 step to ~5s with statement timeouts on INSERT INTO orders is best explained by contention on the SHARED ledger-db instance introduced by portfolio-service v4.5.0 (dep-5905), deployed 08:56Z, which added an unindexed trades query (Seq Scan over ~48.2M rows, attributed to application_name=portfolio-service in ledger-db logs). This is the top-ranked hypothesis at 0.60 confidence (medium) — clearly ahead of the autovacuum config change (0.22), storage IOPS throttling (0.15) and order-service self-saturation (0.10) — and the timing aligns: the order-service step occurs in the 08:57Z bucket (onset between 08:54Z and 08:57Z), the first ledger-db slow-query WARN attributed to portfolio-service is at 08:58:38Z, and ledger-db CPU/connections/slow queries shift in the 08:45-09:00Z bucket. order-service had no deploy or config change in this window, so the cause lies outside it, on the shared datastore it reads. Confidence is medium rather than high because ledger-db exposes no latency metric and the coarse 900s metric buckets cannot prove sub-15-minute causality, and the cfg-2903 autovacuum change remains an unexcluded, timing-plausible alternative.

## Probable root cause
**portfolio-service dep-5905 new unindexed trades query saturates shared ledger-db** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service dep-5905 new unindexed trades query saturates shared ledger-db | bad_deployment | portfolio-service | dep-5905 | 0.60 (medium) | active |
| 2 | H2: ledger-db cfg-2903 autovacuum tuning makes vacuum more aggressive and contends with live queries | config_change | ledger-db | cfg-2903 | 0.22 (low) | active |
| 3 | H3: ledger-db IOPS/burst-credit throttling (infrastructure failure on the shared volume) | infrastructure_failure | ledger-db (backing storage) | null | 0.15 (low) | active |
| 4 | H4: order-service self-saturation (connection-pool / CPU exhaustion) under traffic growth on a degraded dependency | resource_exhaustion | order-service | null | 0.10 (low) | active |

### H1: portfolio-service dep-5905 new unindexed trades query saturates shared ledger-db
portfolio-service v4.5.0 (dep-5905, deployed 08:56Z) added a new trades query filtered by account_id + executed_at with no migration/index. On the shared ledger-db instance this query degrades to a Seq Scan over ~48M rows, driving ledger-db CPU to ~96% and active connections to ~190, raising slow queries from ~1/min to ~141/min. order-service, a co-tenant on the same shared datastore, then sees its own INSERTs into orders hit statement timeouts and its p99 steps from ~150ms to ~5s within the same minute-scale window.

**Causal chain**
1. portfolio-service v4.5.0 deployed at 08:56Z adds 'new query on trades filtered by account_id + executed_at' with no migration included [F2]
2. That query degrades to a Seq Scan on trades scanning ~48.2M rows, issued by application_name=portfolio-service, visible in ledger-db WARN logs from 08:58:38Z (360 entries) [F10]
3. ledger-db is a shared postgres instance called by order-service, portfolio-service, payment-service and settlement-service; the new query's load lands on the same instance order-service uses [F4, F14, F15]
4. ledger-db CPU_pct jumps ~30%->~95.6%, connections_active ~62->~189, slow_queries_per_min ~0.94->~140.7 in the 08:45-09:00Z bucket [F11]
5. order-service p99 steps from ~149ms to ~1.97s/5.6s max at the 08:57Z bucket, first appearance between ~08:54Z and 08:57Z — i.e., immediately after the 08:56Z deploy [F7, F8]
6. order-service then sees statement timeouts on 'INSERT INTO orders' against ledger-db (first_seen 09:00:42Z, count 284), which is the reported symptom [F9]
7. A structurally identical precedent exists: HIST-111, where a new report query without an index caused sequential scans that degraded the shared ledger-db and slowed its callers; resolution was adding an index plus a query review gate [F6]

**Supporting evidence**
  - `F2` ✔ Exactly one code deployment was recorded across all services in 2026-05-05T00:00Z–10:59Z: portfolio-service v4.5.0 (dep-5905) deployed at 2026-05-05T08:56:00Z by k.shah, summary 'Add realised P&L breakdown to holdings API', with changes ['new query on trades filtered by account_id + executed_at','no migration included'], status 'live' and rolled_back_at null (NOT rolled back).
  - `F10` ✔ ledger-db WARN logs from 08:58:38Z onward are all slow queries attributed to application_name=portfolio-service, each a Seq Scan on trades with rows=48211907, e.g. 'slow query 8690ms: SELECT * FROM trades WHERE account_id=$1 ... Seq Scan on trades (rows=48211907) application_name=portfolio-service'; 360 such entries in the window.
  - `F4` ✔ ledger-db is a SHARED PostgreSQL instance: order-service depends on it via a 'db' edge (order-service -> ledger-db), and portfolio-service (deployed 08:56Z with a new trades query) also targets the same shared datastore per the dependency graph showing ledger-db as type 'postgres', owner 'platform', shared=true.
  - `F14` ✔ ledger-db is a SHARED postgres datastore (owner 'platform', shared=true, has_telemetry=true) with FOUR upstream callers via 'db' edges: order-service, portfolio-service, payment-service and settlement-service. It is the single point of contention where four services meet.
  - `F15` ✔ portfolio-service is also a direct caller of ledger-db. The only two entries in the 2026-05-04T00:00Z–2026-05-05T10:59Z change window are exactly the two services that sit adjacent to order-service through shared ledger-db: dep-5905 on portfolio-service at 08:56:00Z and cfg-2903 on ledger-db at 08:00:00Z. No change touches payment-service, settlement-service, pricing-engine, exchange-gateway, kafka or redis-cache.
  - `F11` ✔ ledger-db resource metrics all shift in the single 08:45:00Z-09:00:00Z bucket and then lock high: slow_queries_per_min from ~1/min (08:30 bucket avg 0.94) to 140.74 avg at 09:00Z; cpu_pct from ~30% to 95.65% avg (max 97.58); connections_active from ~62 to 189.07 avg (max 199.85).
  - `F7` ✔ order-service p99 (latency_p99_ms) was flat at ~147-153ms from 05:00Z through the 08:54:00Z bucket, then stepped to 1968.67ms avg (max 5607.8ms) at the 08:57:00Z bucket and stayed at ~5.1-5.5s thereafter; the transition happened within one 3-minute bucket rather than a gradual ramp.
  - `F8` ✔ In the coarse 8-minute view the order-service p99 rise first appears in the 08:52:00Z bucket (avg 831.55ms), but the 3-minute zoom shows 08:54:00Z was still normal (149.12ms) and the actual step is at 08:57:00Z -- so onset is between ~08:54Z and 08:57Z, not 08:52Z.
  - `F9` ✔ ledger-db errors are statement timeouts on INSERT, logged by order-service: top pattern 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)' with count 284, first_seen 2026-05-05T09:00:42Z.
  - `F6` ✔ Earlier precedent HIST-111 (2025-05-22): 'Reporting job full table scans slowed ledger-db', services [ledger-db, settlement-service], symptoms [slow queries, high db cpu], root_cause 'New report query without index did sequential scans', resolution 'Added index; query review gate'. This establishes a recurring pattern where a NEW QUERY (not a config change) degraded the shared ledger-db and slowed its callers — directly parallel to dep-5905's 'new query on trades ... no migration included'.
  - `F17` ✔ Propagation path independent of order-service: portfolio-service -> ledger-db. portfolio-service's new trades query (dep-5905, 'new query on trades filtered by account_id + executed_at', no migration) writes load onto the SAME shared datastore that order-service reads via its 'db' edge; any contention portfolio-service adds to ledger-db is directly visible to order-service with no order-service change required. This is the candidate propagation path consistent with the report that order-service was not released.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The new trades query is genuinely unindexed (no migration/index — stated in dep-5905 changes) and really does Seq Scan at production data volume.
  - ledger-db saturation (CPU/connections) is what causes order-service INSERT timeouts, rather than an independent mechanism (e.g. lock waits, IOPS throttling).
  - order-service db_pool_in_use/error_rate trends (not queried) do not show a distinct, earlier onset that would point elsewhere.

### H2: ledger-db cfg-2903 autovacuum tuning makes vacuum more aggressive and contends with live queries
The DBA config change cfg-2903 at 08:00Z lowered autovacuum_vacuum_scale_factor from 0.2 to 0.1 on ledger-db, making autovacuum far more aggressive. Autovacuum workers running on the large trades table consume CPU/IO and hold locks concurrently with live queries, saturating the shared instance. order-service's INSERTs into orders then suffer lock waits / statement timeouts and its p99 rises.

**Causal chain**
1. cfg-2903 changed autovacuum_vacuum_scale_factor 0.2 -> 0.1 on ledger-db at 08:00Z [F1]
2. More frequent autovacuum runs contend with live queries for CPU/locks on the shared instance *(assumed)*
3. ledger-db CPU/connections/slow-query rate all step up in the 08:45-09:00Z bucket [F11]
4. order-service INSERTs into orders hit statement timeouts and p99 rises to ~5s [F9, F7]

**Supporting evidence**
  - `F1` ✔ Exactly one config change was recorded across all services in 2026-05-05T00:00Z–10:59Z: ledger-db config cfg-2903 changed autovacuum_vacuum_scale_factor from 0.2 to 0.1 (description: 'More aggressive autovacuum') at 2026-05-05T08:00:00Z by dba-team. This confirms the DBA autovacuum change described in the incident report and pins it at 08:00Z.
  - `F11` ✔ ledger-db resource metrics all shift in the single 08:45:00Z-09:00:00Z bucket and then lock high: slow_queries_per_min from ~1/min (08:30 bucket avg 0.94) to 140.74 avg at 09:00Z; cpu_pct from ~30% to 95.65% avg (max 97.58); connections_active from ~62 to 189.07 avg (max 199.85).
  - `F9` ✔ ledger-db errors are statement timeouts on INSERT, logged by order-service: top pattern 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)' with count 284, first_seen 2026-05-05T09:00:42Z.
**Contradicting evidence**
  - `F7` ✔ order-service p99 (latency_p99_ms) was flat at ~147-153ms from 05:00Z through the 08:54:00Z bucket, then stepped to 1968.67ms avg (max 5607.8ms) at the 08:57:00Z bucket and stayed at ~5.1-5.5s thereafter; the transition happened within one 3-minute bucket rather than a gradual ramp.
  - `F8` ✔ In the coarse 8-minute view the order-service p99 rise first appears in the 08:52:00Z bucket (avg 831.55ms), but the 3-minute zoom shows 08:54:00Z was still normal (149.12ms) and the actual step is at 08:57:00Z -- so onset is between ~08:54Z and 08:57Z, not 08:52Z.
  - `F10` ✔ ledger-db WARN logs from 08:58:38Z onward are all slow queries attributed to application_name=portfolio-service, each a Seq Scan on trades with rows=48211907, e.g. 'slow query 8690ms: SELECT * FROM trades WHERE account_id=$1 ... Seq Scan on trades (rows=48211907) application_name=portfolio-service'; 360 such entries in the window.
  - `F6` ✔ Earlier precedent HIST-111 (2025-05-22): 'Reporting job full table scans slowed ledger-db', services [ledger-db, settlement-service], symptoms [slow queries, high db cpu], root_cause 'New report query without index did sequential scans', resolution 'Added index; query review gate'. This establishes a recurring pattern where a NEW QUERY (not a config change) degraded the shared ledger-db and slowed its callers — directly parallel to dep-5905's 'new query on trades ... no migration included'.
**Assumptions**
  - Autovacuum workers, not query load, dominate the CPU/IO budget on ledger-db.
  - The 08:00Z change produced its effect ~57-60 minutes later with a step change rather than a gradual ramp.

### H3: ledger-db IOPS/burst-credit throttling (infrastructure failure on the shared volume)
The underlying cloud volume backing ledger-db exhausted its burst credits, throttling IOPS. Slow IO makes order-service INSERTs and reads on ledger-db time out, producing exactly the observed order-service p99 step and statement timeouts. This is the mechanism recorded in precedent HIST-110, whose symptom set matches this incident almost exactly.

**Causal chain**
1. Cloud volume burst credits exhaust and IOPS are throttled on ledger-db's storage [F5]
2. ledger-db CPU, connections and slow-query rate all spike [F11]
3. order-service INSERTs into orders time out; p99 rises to ~5s [F9, F7]

**Supporting evidence**
  - `F5` ✔ Precedent search returned HIST-110 (2026-01-15, match_score 5): 'Slow orders from ledger-db IOPS throttling', services [order-service, ledger-db], symptoms [order latency, statement timeouts, slow queries], root_cause 'Cloud volume burst credits exhausted; IOPS throttled'. The symptoms match the current incident almost exactly but the recorded cause is a different mechanism (infrastructure IOPS, not config/autovacuum).
  - `F11` ✔ ledger-db resource metrics all shift in the single 08:45:00Z-09:00:00Z bucket and then lock high: slow_queries_per_min from ~1/min (08:30 bucket avg 0.94) to 140.74 avg at 09:00Z; cpu_pct from ~30% to 95.65% avg (max 97.58); connections_active from ~62 to 189.07 avg (max 199.85).
  - `F9` ✔ ledger-db errors are statement timeouts on INSERT, logged by order-service: top pattern 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)' with count 284, first_seen 2026-05-05T09:00:42Z.
**Contradicting evidence**
  - `F2` ✔ Exactly one code deployment was recorded across all services in 2026-05-05T00:00Z–10:59Z: portfolio-service v4.5.0 (dep-5905) deployed at 2026-05-05T08:56:00Z by k.shah, summary 'Add realised P&L breakdown to holdings API', with changes ['new query on trades filtered by account_id + executed_at','no migration included'], status 'live' and rolled_back_at null (NOT rolled back).
  - `F10` ✔ ledger-db WARN logs from 08:58:38Z onward are all slow queries attributed to application_name=portfolio-service, each a Seq Scan on trades with rows=48211907, e.g. 'slow query 8690ms: SELECT * FROM trades WHERE account_id=$1 ... Seq Scan on trades (rows=48211907) application_name=portfolio-service'; 360 such entries in the window.
  - `F6` ✔ Earlier precedent HIST-111 (2025-05-22): 'Reporting job full table scans slowed ledger-db', services [ledger-db, settlement-service], symptoms [slow queries, high db cpu], root_cause 'New report query without index did sequential scans', resolution 'Added index; query review gate'. This establishes a recurring pattern where a NEW QUERY (not a config change) degraded the shared ledger-db and slowed its callers — directly parallel to dep-5905's 'new query on trades ... no migration included'.
  - `F1` ✔ Exactly one config change was recorded across all services in 2026-05-05T00:00Z–10:59Z: ledger-db config cfg-2903 changed autovacuum_vacuum_scale_factor from 0.2 to 0.1 (description: 'More aggressive autovacuum') at 2026-05-05T08:00:00Z by dba-team. This confirms the DBA autovacuum change described in the incident report and pins it at 08:00Z.
**Assumptions**
  - Storage throttling occurred again at 08:56-08:57Z independently of any change.
  - No IOPS/latency metric exists to verify this (ledger-db exposes no latency metric; F12).

### H4: order-service self-saturation (connection-pool / CPU exhaustion) under traffic growth on a degraded dependency
order-service itself became saturated — its DB connection pool or CPU exhausted — independent of latency on the shared datastore. Requests queue at the pool, p99 steps to ~5s, and statement timeouts appear because connections are held too long and queries exceed the timeout with no change in the database.

**Causal chain**
1. order-service connection pool or CPU saturates (traffic growth or slow downstream) *(assumed)*
2. Requests queue, p99 steps from ~150ms to ~5s within one 3-minute bucket [F7, F8]
3. Held/blocked queries exceed the statement timeout and are cancelled [F9]

**Supporting evidence**
  - `F7` ✔ order-service p99 (latency_p99_ms) was flat at ~147-153ms from 05:00Z through the 08:54:00Z bucket, then stepped to 1968.67ms avg (max 5607.8ms) at the 08:57:00Z bucket and stayed at ~5.1-5.5s thereafter; the transition happened within one 3-minute bucket rather than a gradual ramp.
  - `F8` ✔ In the coarse 8-minute view the order-service p99 rise first appears in the 08:52:00Z bucket (avg 831.55ms), but the 3-minute zoom shows 08:54:00Z was still normal (149.12ms) and the actual step is at 08:57:00Z -- so onset is between ~08:54Z and 08:57Z, not 08:52Z.
  - `F9` ✔ ledger-db errors are statement timeouts on INSERT, logged by order-service: top pattern 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)' with count 284, first_seen 2026-05-05T09:00:42Z.
**Contradicting evidence**
  - `F10` ✔ ledger-db WARN logs from 08:58:38Z onward are all slow queries attributed to application_name=portfolio-service, each a Seq Scan on trades with rows=48211907, e.g. 'slow query 8690ms: SELECT * FROM trades WHERE account_id=$1 ... Seq Scan on trades (rows=48211907) application_name=portfolio-service'; 360 such entries in the window.
  - `F11` ✔ ledger-db resource metrics all shift in the single 08:45:00Z-09:00:00Z bucket and then lock high: slow_queries_per_min from ~1/min (08:30 bucket avg 0.94) to 140.74 avg at 09:00Z; cpu_pct from ~30% to 95.65% avg (max 97.58); connections_active from ~62 to 189.07 avg (max 199.85).
  - `F2` ✔ Exactly one code deployment was recorded across all services in 2026-05-05T00:00Z–10:59Z: portfolio-service v4.5.0 (dep-5905) deployed at 2026-05-05T08:56:00Z by k.shah, summary 'Add realised P&L breakdown to holdings API', with changes ['new query on trades filtered by account_id + executed_at','no migration included'], status 'live' and rolled_back_at null (NOT rolled back).
  - `F6` ✔ Earlier precedent HIST-111 (2025-05-22): 'Reporting job full table scans slowed ledger-db', services [ledger-db, settlement-service], symptoms [slow queries, high db cpu], root_cause 'New report query without index did sequential scans', resolution 'Added index; query review gate'. This establishes a recurring pattern where a NEW QUERY (not a config change) degraded the shared ledger-db and slowed its callers — directly parallel to dep-5905's 'new query on trades ... no migration included'.
**Assumptions**
  - order-service's own pool/CPU metrics (not queried) would show saturation at 08:57Z.
  - order-service resource exhaustion would arise with no deploy and no config change on it (F3).

## Facts (verified observations)
- ledger-db is a shared PostgreSQL datastore (owner platform, shared=true) with four upstream 'db' callers: order-service, portfolio-service, payment-service and settlement-service — the single point of contention where the four meet. [F14, F4]
- portfolio-service v4.5.0 (dep-5905) was deployed at 2026-05-05T08:56:00Z with changes 'new query on trades filtered by account_id + executed_at' and 'no migration included'; it was the only code deployment in the window and has not been rolled back. [F2]
- ledger-db WARN logs from 08:58:38Z onward are slow queries attributed to application_name=portfolio-service, each a Seq Scan on trades with rows=48211907 (e.g. 'slow query 8690ms ... Seq Scan on trades (rows=48211907) application_name=portfolio-service'), 360 entries in the window. [F10]
- ledger-db resources shift within the single 08:45-09:00Z bucket and stay high: slow_queries_per_min from ~0.94 to 140.74 avg, cpu_pct from ~30% to 95.65% avg (max 97.58), connections_active from ~62 to 189.07 avg (max 199.85). [F11]
- order-service p99 was flat at ~147-153ms from 05:00Z through the 08:54:00Z bucket, then stepped to 1968.67ms avg (max 5607.8ms) at the 08:57:00Z bucket and stayed ~5.1-5.5s; the 3-minute zoom places onset between ~08:54Z and ~08:57Z, not 08:52Z. [F7]
- ledger-db errors observed are statement timeouts on INSERT logged by order-service: 'canceling statement due to statement timeout (INSERT INTO orders)', count 284, first_seen 09:00:42Z. [F9]
- order-service had no deployment or config change in 2026-05-05T00:00-10:59Z nor in 2026-05-04T00:00Z-2026-05-05T00:00Z, consistent with the report that it has not been released this week; any cause in its own code/config is not visible. [F3]
- Exactly one config change was recorded across all services in the window: ledger-db cfg-2903 lowering autovacuum_vacuum_scale_factor from 0.2 to 0.1 at 08:00:00Z by dba-team. [F1]
- The only two changes in the window sit exactly on the two components adjacent to order-service through shared ledger-db: dep-5905 on portfolio-service (08:56Z) and cfg-2903 on ledger-db (08:00Z); no change touches payment-service, settlement-service, pricing-engine, exchange-gateway, kafka or redis-cache. [F15]
- Precedent HIST-110 (2026-01-15) matches the symptom set almost exactly (order latency, statement timeouts, slow queries) but attributes it to cloud volume burst-credit exhaustion / IOPS throttling — a different mechanism from the current top hypothesis. [F5]
- ledger-db exposes no latency_p99_ms metric; its available metrics are connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min, and its buckets are 900s coarse. [F12]
- order-service depends on ledger-db (kind 'db'), pricing-engine and exchange-gateway (http) and kafka (queue-produce); it also has a second shared-component fan-in at kafka, and api-gateway calls order-service. [F13, F16]

## Assumptions (not verified)
- ASSUMPTION: The new trades query in dep-5905 is genuinely unindexed (no migration included) and really does degrade to a Seq Scan at production data volume — the log evidence (rows=48211907 Seq Scan) supports this but the live query plan was not directly inspected.
- ASSUMPTION: ledger-db saturation (CPU ~96% and ~190 active connections) is the mechanism that turns order-service INSERTs into statement timeouts, rather than an independent mechanism such as lock waits or storage throttling; no lock-wait telemetry was available to exclude lock contention.
- ASSUMPTION: order-service's own db_pool_in_use/error_rate trends, which were not queried, do not show a distinct, earlier onset that would point at self-saturation instead of the shared datastore.
- ASSUMPTION: The order-service p99 step (08:57Z) and the first portfolio-service Seq Scan WARN (08:58:38Z) are causally ordered despite being recorded nearly simultaneously; the 900s metric buckets cannot prove strictly which fired first within 08:45-09:00Z.
- ASSUMPTION: The 08:00Z autovacuum change (cfg-2903) did not produce the 08:57Z step; its ~57-minute delay and the absence of autovacuum-attributed log lines make this less likely, but it is not formally excluded.
- ASSUMPTION: No order-service-affecting change occurred before 2026-05-04T00:00Z, the earliest date covered by the change queries; the report's 'not released this week' is treated as corroborating but not independently provable from this data.
- ASSUMPTION: Kafka and redis-cache, the other shared components, did not concurrently degrade at ~08:57Z; they were not tested for utilisation.
- (demoted from facts: no verified observation backs it) Precedent HIST-111 (2025-05-22) records a recurring pattern: 'New report query without index did sequential scans' degraded shared ledger-db and slowed its callers, resolved by adding an index and a query review gate — parallel to dep-5905's unindexed new query.
- (demoted from facts: no verified observation backs it) portfolio-service -> ledger-db is a propagation path that requires no change on order-service: portfolio-service's new trades query loads the same shared datastore order-service reads, so contention is directly visible to order-service.

## Timeline
- `2026-05-05T08:00:00Z` DBA config change cfg-2903 applied to ledger-db: autovacuum_vacuum_scale_factor lowered 0.2 -> 0.1 ('More aggressive autovacuum'). Only config change across all services in the window. This is the H2 candidate trigger. [F1]
- `2026-05-05T08:52:00Z` Coarse 8-minute view first shows a rise in order-service p99 (avg 831.55ms); the 3-minute zoom shows this bucket is not the true onset. [F8]
- `2026-05-05T08:54:00Z` order-service p99 still normal at 149.12ms; last normal bucket before the step. [F7, F8]
- `2026-05-05T08:56:00Z` portfolio-service v4.5.0 (dep-5905) deployed by k.shah: 'Add realised P&L breakdown to holdings API', adding a new trades query filtered by account_id + executed_at with no migration. Only code deployment in the window; still live (not rolled back). This is the H1 candidate trigger. [F2]
- `2026-05-05T08:57:00Z` order-service p99 steps from ~150ms to 1968.67ms avg (max 5607.8ms) within a single 3-minute bucket and stays ~5.1-5.5s thereafter; onset bounded between 08:54Z and 08:57Z. [F7, F8]
- `2026-05-05T08:58:38Z` First ledger-db WARN slow query attributed to application_name=portfolio-service: Seq Scan on trades with rows=48211907 (e.g. 8690ms). 360 such entries follow in the window. [F10]
- `2026-05-05T09:00:00Z` ledger-db metrics lock high in the 08:45-09:00Z bucket: slow_queries_per_min ~0.94 -> 140.74 avg, cpu_pct ~30% -> 95.65% avg (max 97.58), connections_active ~62 -> 189.07 avg (max 199.85). [F11]
- `2026-05-05T09:00:42Z` First ledger-db statement timeout observed, logged by order-service: 'canceling statement due to statement timeout (INSERT INTO orders)'; 284 occurrences. [F9]
- `2026-05-05T09:10:00Z` Incident INC-009 reported by on-call SRE: order-service p99 > 5s with statement timeouts against ledger-db; DBA autovacuum change noted. [F1, F2]
- `2026-05-05T10:59:00Z` Latest data point; ledger-db CPU/connections/slow queries remain elevated and portfolio-service dep-5905 remains live (not rolled back). [F2, F11]

## Recommended actions
- **[immediate]** Immediately roll back portfolio-service deployment dep-5905 (v4.5.0, deployed 08:56Z) or disable the new 'realised P&L breakdown' trades query behind a feature flag, then confirm within 2-3 minutes that ledger-db slow_queries_per_min, cpu_pct and connections_active fall and that order-service p99 returns toward ~150ms. This is the fastest discriminating mitigation: it removes the only new load source on the shared datastore. -- dep-5905 is the top hypothesis trigger and the only code deployment in the window; 360 ledger-db slow queries are already attributed to application_name=portfolio-service doing Seq Scans on 48.2M rows. Rollback both mitigates and tests H1. [F2, F10, F11, F7]
- **[immediate]** If rollback of dep-5905 does NOT restore order-service p99 within ~10 minutes, as the next safe mitigation temporarily revert ledger-db config cfg-2903 (autovacuum_vacuum_scale_factor back to 0.2) or explicitly pause/limit autovacuum on the trades table, and re-check ledger-db cpu_pct and slow_queries_per_min. Do not apply both mitigations simultaneously without recording the time of each — ordering is the only way to attribute the recovery. -- H2 (cfg-2903) is the leading timing-plausible alternative and is not formally excluded; reverting it is low-risk and, sequenced after the rollback, discriminates H1 from H2. [F1, F11]
- **[short_term]** Add the missing index / migration for the trades query on (account_id, executed_at) — the exact remediation used in precedent HIST-111 ('Added index; query review gate') — and re-run the query with EXPLAIN to confirm a bitmap/index scan replaces the 48.2M-row Seq Scan before re-deploying portfolio-service v4.5.0. -- Fixes the substantive defect behind the top hypothesis and directly follows the documented precedent for the same failure mode on the same shared datastore. [F2, F6, F10]
- **[short_term]** Instrument and verify: confirm order-service db_pool_in_use, error_rate and (once available) ledger-db latency remain normal for 30+ minutes post-mitigation; also capture portfolio-service's own p99/error trend to confirm the blast radius and that no other shared-ledger-db tenant (payment-service, settlement-service) degraded. -- Closes the open verification questions from the investigation: order-service pool metrics and downstream blast radius were never queried, and ledger-db has no latency metric (F12). [F12, F14]
- **[long_term]** Add a query-review / migration gate in CI that blocks deployments whose new SQL has no supporting index (or emits a warning when SQL references a table above a row-count threshold without an index), citing HIST-111 as the prior incident. -- Directly addresses the recurrence risk evidenced by two structurally identical precedents (HIST-111 new-query scan, HIST-110 ledger-db degradation) on the same shared instance. [F6, F5]
- **[long_term]** Add per-caller load attribution and a latency metric to ledger-db telemetry (statement-level latency percentiles and per-application_name load), and add statement_timeout/work_mem or a resource cap for shared-datastore tenants so one callers' unindexed query cannot saturate the instance. -- ledger-db exposes no latency_p99_ms and 900s buckets, which is exactly why this incident could not be pinned to the minute; per-caller isolation prevents any single tenant from saturating four shared callers. [F12, F14, F11]

## Data gaps
- No tool exposes ledger-db database-level activity — autovacuum runtime, query plans, IOPS or latency — so cfg-2903 and dep-5905's query could not be correlated directly with the DB slowdown. ledger-db has no latency_p99_ms metric (F12), which is why the causal chain is inferential rather than measured.
- ledger-db metric buckets are 900s coarse, so the exact minute of the ledger-db resource onset within 08:45-09:00Z cannot be pinned and the sub-15-minute ordering of the ledger-db CPU/connection ramp relative to the 08:57Z order-service step is bounded rather than exact.
- portfolio-service's own latency/error metrics were not queried (tool-call budget exhausted), so the blast radius beyond order-service and ledger-db — including whether payment-service and settlement-service, the other shared-ledger-db tenants, degraded — is unconfirmed.
- order-service db_pool_in_use and error_rate were not queried, nor ledger-db disk_used_pct / replication_lag_ms, so connection-pool exhaustion of order-service and IO/disk pressure on ledger-db could not be directly evidenced. This is the main unexcluded opening for H4.
- No lock-wait or lock-contention telemetry was observed (query logs returned only WARN/ERROR samples), so pure row-scan CPU contention could not be distinguished from lock waits — the mechanism underlying H2's claim.
- Change queries are point-in-time snapshots with no history before 2026-05-04T00:00Z, so an order-service change earlier in the week cannot be fully excluded.
- No dependency result exposes per-edge call volume or which caller dominates ledger-db load, so the load contribution of portfolio-service's new query versus order-service's own traffic versus autovacuum could not be quantified; kafka and redis-cache utilisation at ~08:57Z was likewise untested.

## What would change this conclusion
- If rolling back dep-5905 does NOT reduce ledger-db slow_queries_per_min/cpu_pct or restore order-service p99, H1 loses its primary trigger and H2 (cfg-2903 autovacuum) becomes the leading explanation.
- If EXPLAIN on the new trades query shows an index or bitmap scan (i.e. it is already indexed at production volume), the core premise of H1 — the 48.2M-row Seq Scan caused by dep-5905 — collapses.
- Evidence of ledger-db lock waits or lock contention attributable to autovacuum workers (rather than row scans) would shift support to H2 and change the mitigation to autovacuum tuning rather than query indexing.
- Storage-level IOPS throttling / exhausted burst credits on the ledger-db volume (as in HIST-110), or order-service db_pool_in_use/CPU metrics showing saturation with no corresponding ledger-db load change, would support H3 or H4 respectively over H1.
- A deployment or config change to order-service (or to kafka/redis-cache/its http dependencies) in the window before 2026-05-04T00:00Z would reopen paths the current data cannot see.
- If the ledger-db resource onset could be shown to precede 08:56Z by a clear margin at sub-15-minute resolution, dep-5905 — deployed at 08:56Z — could not be the initial trigger, weakening H1 relative to cfg-2903 (08:00Z).

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: Precedent HIST-111 (2025-05-22) records a recurring pattern: 'New report query w
- fact demoted to assumption: portfolio-service -> ledger-db is a propagation path that requires no change on 

## Investigation stats
- latency 130.62s, LLM calls 12, tokens 75647 (in 58712 / out 16935)
- tool calls 21 (1 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
