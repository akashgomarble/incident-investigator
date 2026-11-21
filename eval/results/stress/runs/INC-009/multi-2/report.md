# Incident investigation report: INC-009
*System: multi_agent | outcome: **inconclusive** | ended: critic_accepted*

## Summary
The reviewed analysis ranks H1 (portfolio-service v4.5.0's unindexed trades query saturating the shared ledger-db) first, but only at 0.30 confidence — below the 0.50 bar — so this report is formally inconclusive. H1 remains the best-supported origin: dep-5905 deployed at 08:56:00Z adding "SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2" with no migration, and the earliest degraded signal is a ledger-db slow-query WARN at 08:58:38Z carrying the verbatim plan "Seq Scan on trades (rows=48211907)" attributed to application_name=portfolio-service. In the same 08:55-09:00 bucket, ledger-db CPU stepped 29-30% to ~96%, connections_active rose from ~60 to ~195, order-service p99 jumped from ~151ms to ~5.3s and 5xx from ~0.2% to ~9.4%, all while order-service itself was unchanged. The confidence cap is deliberate: ledger-db exposes no per-query execution-time or autovacuum-activity telemetry and no index registry, so the 96% CPU cannot be attributed directly to that statement, and H2/H3 cannot be directly excluded or confirmed. Immediate action should be a low-risk rollback of dep-5905 (or an index on trades(account_id, executed_at)), which is also the discriminating test between H1 and the alternatives.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service v4.5.0 unindexed trades query saturates shared ledger-db | bad_deployment | portfolio-service | dep-5905 | 0.30 (low) | active |
| 2 | H2: DBA autovacuum config change on ledger-db caused CPU/IO contention | config_change | ledger-db | cfg-2903 | 0.06 (low) | active |
| 3 | H3: ledger-db resource/IO saturation independent of any change (IOPS throttle / capacity) | capacity_saturation | ledger-db | null | 0.05 (low) | active |
| 4 | H4: order-service holds DB transactions open on a slow external dependency call, amplifying timeouts | dependency_failure | pricing-engine | null | 0.02 (low) | active |
| 5 | H5: Autovacuum change is coincidental; timeouts pre-date it | unknown | ledger-db | null | 0.02 (low) | rejected |

### H1: portfolio-service v4.5.0 unindexed trades query saturates shared ledger-db
dep-5905 (portfolio-service v4.5.0, 08:56Z) added a query 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at' with no migration/index. At execution time ledger-db runs this as a Seq Scan over ~48.2M trades rows, driving ledger-db CPU from ~30% to ~96% and lifting connections_active from ~60 to ~195. Because ledger-db is shared by order-service, portfolio-service, payment-service and settlement-service, order-service INSERT INTO orders statements now queue behind that load and hit the statement timeout, producing p99 >5s and ~9.4% 5xx. Fault origin is the portfolio-service release; ledger-db is the casualty and order-service the visible victim.

**Causal chain**
1. dep-5905 deploys portfolio-service v4.5.0 at 08:56Z adding a new trades query on account_id + executed_at with no migration/index [F1, F15, F14]
2. The new query executes against the shared ledger-db as a Seq Scan on trades (rows=48,211,907), attributed verbatim to application_name=portfolio-service [F11, F21]
3. ledger-db CPU steps from ~29-30.6% (flat 08:00-08:50) to 56.67% at 08:55 and ~95.9% at 09:00, and connections_active steps ~60 -> ~195 starting 08:56-08:57, concurrent with the latency jump [F12, F26]
4. Saturated shared DB makes order-service INSERT INTO orders exceed statement timeout -> 'canceling statement due to statement timeout (INSERT INTO orders)', first_seen 09:00:42Z, 284 occurrences (mechanism of contention inferred: CPU starvation vs lock vs connection-slot saturation not directly measured) [F10]
5. order-service p99 steps from ~151ms to ~2878ms at 08:58 then ~5283ms at 09:00 and stays 5000-5700ms; HTTP 5xx steps from ~0.2% to ~9.4% [F8, F9]
6. Both shared-DB callers verified degraded together: portfolio-service logs its own slow P&L query (up to 8.4s) from 08:59:39Z, confirming DB-wide pressure rather than an order-service-specific fault [F13]
7. order-service and portfolio-service's only shared downstream component is ledger-db, so portfolio-service can harm order-service only through that shared DB [F18]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - (none)

### H2: DBA autovacuum config change on ledger-db caused CPU/IO contention
cfg-2903 (08:00Z) lowered autovacuum_vacuum_scale_factor from 0.2 to 0.1 on ledger-db, making autovacuum 'more aggressive'. The resulting autovacuum worker activity (extra CPU/IO, possible lock or planner/bloat effects) degrades the shared ledger-db, so order-service INSERT INTO orders times out and p99 rises. This is the reporter's suspicion and the only config change in the window.

**Causal chain**
1. cfg-2903 at 08:00Z lowers autovacuum_vacuum_scale_factor 0.2 -> 0.1 on ledger-db [F2, F17]
2. More frequent autovacuum triggers more aggressive vacuuming, raising DB CPU/IO (mechanism plausible but unverified — no autovacuum worker metric exists) *(assumed)*
3. Degraded shared ledger-db makes order-service INSERT INTO orders hit the statement timeout [F10]
4. order-service p99 and 5xx step up at 08:58-09:00; ledger-db CPU steps to ~96% at 08:55-09:00 [F8, F9, F12]

**Supporting evidence**
  - `F2` ✔ Only ONE config change across all services in the last 24h: cfg-2903 on ledger-db, key autovacuum_vacuum_scale_factor, old_value 0.2 -> new_value 0.1, changed_at 2026-05-05T08:00:00Z, author dba-team, description 'More aggressive autovacuum'.
  - `F7` ✔ Timeline ordering of the two changes versus onset (reported 09:10, latest data 10:59): autovacuum change at 08:00, portfolio-service deploy at 08:56, both before the 09:10 report; neither has been rolled back (dep-5905 rolled_back_at null). This is only the change picture — the data retrieved does not by itself show which change caused the latency.
  - `F12` ✔ ledger-db CPU stepped concurrently with the latency jump: flat ~29-30.6% from 08:00 to 08:50, then 08:55 bucket avg 56.67% (max 96.36), 09:00 avg 95.87% (max 96.81), staying ~95-97% through 09:10.
  - `F10` ✔ The dominant order-service error signature is a ledger-db statement timeout on INSERT: pattern 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders) orderId=...' with 284 occurrences; first_seen 09:00:42Z (orderId=15a39d3b). An isolated earlier WARN (08:05:59Z) is a single 'retrying idempotent request (attempt 2) after connection reset'.
  - `F22` ✔ cfg-2903 (ledger-db autovacuum_vacuum_scale_factor 0.2 -> 0.1, 2026-05-05T08:00:00Z) is the ONLY config change in ledger-db and the ONLY config change across all services over 2026-04-20..2026-05-05T10:59; no revert/rollback entry for it appears, so as of latest data the autovacuum change is still in effect.
**Contradicting evidence**
  - `F11` ✔ The EARLIEST degraded signal is ledger-db slow-query warnings starting 08:58:38Z, and all 360 WARN entries carry the same signature: 'slow query <n>ms: SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service'. The application_name is portfolio-service, NOT order-service.
  - `F14` ✔ Change window 07:30-09:30Z across ALL services contains exactly one deployment (dep-5905, portfolio-service v4.5.0, 08:56:00Z) and one config change (cfg-2903, ledger-db autovacuum_vacuum_scale_factor 0.2->0.1, 08:00:00Z); a narrowed 08:40-09:20Z config query returned count 0, so no config change occurred near the onset.
  - `F15` ✔ dep-5905 deployment = new query on trades filtered by account_id + executed_at, no migration, status live, rolled_back_at null (not rolled back). This query shape matches the earliest degraded signal's signature: the ledger-db slow query at 08:58:38Z is 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2' attributed to application_name=portfolio-service, which is the same trades-by-account_id+executed_at access pattern the deployment added.
  - `F16` ✔ Change-to-onset timing bracket: dep-5905 at 08:56:00Z precedes the first degraded signal (ledger-db slow-query warning 08:58:38Z) by ~2.5 min, and precedes the reported onset (08:55-09:00 CPU bucket / 09:00 latency step) within the same 2-4 min; cfg-2903 at 08:00:00Z precedes onset by ~58 min with no intervening degradation (ledger-db CPU flat ~29-30.6% 08:00-08:50, first elevated bucket 08:55). dep-5905 is therefore the tighter-bracketing change.
  - `F28` ✔ The ONLY autovacuum evidence anywhere in 08:45-09:15Z is 2 INFO log lines, both 'autovacuum: processed table public.audit_log', at 08:47:50Z and 08:52:26Z (total_matched 2, truncated=false). Both occur BEFORE the 08:55 CPU/connection step, and they name audit_log, not the trades table implicated by the slow query.
  - `F30` ✔ Inference: the autovacuum config change (cfg-2903, 08:00:00Z, scale_factor 0.2->0.1) has no supporting telemetry in this window — the only autovacuum log activity predates the onset, the CPU step begins 55 min after the change, and no worker-count series exists — so autovacuum cannot be shown to be the dominant CPU/IO consumer at 08:55-09:10.
**Assumptions**
  - Autovacuum workers actually launched and consumed significant CPU/IO starting near 08:55 — no autovacuum activity metric was retrieved, and the only autovacuum log lines (08:47:50, 08:52:26 on audit_log) precede onset (F28).
  - Planner/bloat regression from scale_factor could cause the specific INSERT timeout — but the observed slow-query signature is a SELECT seq scan on trades attributed to portfolio-service, not an INSERT plan regression (F11).

### H3: ledger-db resource/IO saturation independent of any change (IOPS throttle / capacity)
The shared ledger-db simply ran out of provisioned CPU/IOPS or connection capacity at ~08:55-09:00, independent of the portfolio deploy or the autovacuum change; order-service statement timeouts are the downstream symptom of a shared-datastore capacity limit. Precedent HIST-110 ('Slow orders from ledger-db IOPS throttling') matches this symptom set and was resolved by moving to provisioned IOPS.

**Causal chain**
1. ledger-db CPU saturates to ~95-97% from 08:55 and stays there [F12]
2. Saturated shared DB makes order-service INSERTs hit statement timeout, p99 >5s, 5xx ~9.4% [F10, F8, F9]
3. Historical analogue HIST-110: ledger-db IOPS throttling produced order latency + statement timeouts [F19]

**Supporting evidence**
  - `F12` ✔ ledger-db CPU stepped concurrently with the latency jump: flat ~29-30.6% from 08:00 to 08:50, then 08:55 bucket avg 56.67% (max 96.36), 09:00 avg 95.87% (max 96.81), staying ~95-97% through 09:10.
  - `F19` ✔ Historical precedents both involve ledger-db shared-resource pressure with the same symptom set: HIST-110 (2026-01-15) 'Slow orders from ledger-db IOPS throttling' symptoms [order latency, statement timeouts, slow queries], HIST-111 (2025-05-22) 'Reporting job full table scans slowed ledger-db' root_cause 'New report query without index did sequential scans' category bad_deployment. These are leads, not evidence about the current incident.
  - `F27` ✔ ledger-db disk_used_pct is flat and mild across 08:45-09:15Z (min 69.81, max 72.48, mean 71.17; e.g. 08:55 avg 70.82, 09:15 avg 70.44) with no step at onset. This is a capacity/fill-level gauge, NOT IOPS/throughput, so it cannot confirm or refute an IOPS-saturation (H3) mechanism.
**Contradicting evidence**
  - `F8` ✔ order-service p99 latency was flat at ~145-155ms through 08:56Z (e.g. 08:56 avg 151.35ms, max 153.11ms), then the 08:58-09:00 bucket jumped to avg 2878.21ms / max 5607.8ms and the 09:00 bucket to avg 5282.68ms; it stayed ~5000-5700ms through 10:55. This is a step change, not a gradual ramp.
  - `F11` ✔ The EARLIEST degraded signal is ledger-db slow-query warnings starting 08:58:38Z, and all 360 WARN entries carry the same signature: 'slow query <n>ms: SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service'. The application_name is portfolio-service, NOT order-service.
  - `F16` ✔ Change-to-onset timing bracket: dep-5905 at 08:56:00Z precedes the first degraded signal (ledger-db slow-query warning 08:58:38Z) by ~2.5 min, and precedes the reported onset (08:55-09:00 CPU bucket / 09:00 latency step) within the same 2-4 min; cfg-2903 at 08:00:00Z precedes onset by ~58 min with no intervening degradation (ledger-db CPU flat ~29-30.6% 08:00-08:50, first elevated bucket 08:55). dep-5905 is therefore the tighter-bracketing change.
  - `F26` ✔ ledger-db connections_active stepped from a flat pre-onset level of ~58-65 connections (08:45-08:55 buckets: 63.46, 60.0, 58.15, 63.15, 63.3, 62.31) to 122.61/185.11 at 08:57 and 195.84/199.81 at 08:59, remaining ~178-195 through 09:15. The step begins in the 08:55-08:57 bucket, i.e. at the portfolio-service deploy time (08:56:00Z), concurrent with the CPU and latency step.
  - `F29` ✔ Inference on the dominant consumer at 08:55-09:10: the largest concurrent rises on the shared ledger-db are connections_active (~60 → ~195, starting 08:56-08:57) alongside the CPU step to ~96% (F12), both coinciding with the portfolio-service deploy (08:56:00Z) and its trades-by-account_id+executed_at query appearing as the ledger-db slow-query signature from 08:58:38Z (F11). No autovacuum worker activity is visible in that window. This is consistent with the portfolio-service sequential-scan query (H1) being the dominant consumer, but because per-query exec-time telemetry does not exist for ledger-db, this is inference from proxies, not a direct measurement.
**Assumptions**
  - Ledger-db load grew organically rather than being caused by any change — no traffic-growth metric was retrieved, and the CPU/connection step lands exactly at the 08:56 deploy and the 08:58 portfolio-service seq-scan signature.
  - No IOPS/queue-depth series exists (F25, F27: only disk_used_pct, which is flat ~70-72%), so IOPS saturation can be neither confirmed nor excluded directly.

### H4: order-service holds DB transactions open on a slow external dependency call, amplifying timeouts
order-service's own code path is unchanged, but a call to a non-DB dependency (pricing-engine via http, exchange-gateway/exchange-api external, or redis-cache) became slow, so order-service held its ledger-db transactions open longer, causing INSERT INTO orders to exceed the statement timeout and inflating p99. The ledger-db seq-scan warnings would then be a coincidental second effect.

**Causal chain**
1. order-service depends on pricing-engine (http) and exchange-gateway (http, external exchange-api with has_telemetry=false) — an external slow dependency is structurally plausible [F5]
2. A slow external call holds order-service DB transactions open longer, pushing INSERT INTO orders past the statement timeout *(assumed)*
3. Resulting p99 >5s and 9.4% 5xx [F8, F9]

**Supporting evidence**
  - `F5` ✔ order-service dependency graph (depth 2, both directions): order-service -> ledger-db (db), -> pricing-engine (http), -> exchange-gateway (http), -> kafka (queue-produce); pricing-engine -> market-data-service (http) and -> redis-cache (cache); exchange-gateway -> exchange-api (external, has_telemetry false); api-gateway -> order-service (http). Shared components in the graph: ledger-db, redis-cache, kafka (all shared=true, owner platform).
  - `F3` ✔ order-service was NOT modified: explicit config-change query scoped to order-service for 2026-05-01..2026-05-05 returned {"count":0,"changes":[]} (matches the report's claim that order-service has not been released this week).
**Contradicting evidence**
  - `F8` ✔ order-service p99 latency was flat at ~145-155ms through 08:56Z (e.g. 08:56 avg 151.35ms, max 153.11ms), then the 08:58-09:00 bucket jumped to avg 2878.21ms / max 5607.8ms and the 09:00 bucket to avg 5282.68ms; it stayed ~5000-5700ms through 10:55. This is a step change, not a gradual ramp.
  - `F11` ✔ The EARLIEST degraded signal is ledger-db slow-query warnings starting 08:58:38Z, and all 360 WARN entries carry the same signature: 'slow query <n>ms: SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service'. The application_name is portfolio-service, NOT order-service.
  - `F12` ✔ ledger-db CPU stepped concurrently with the latency jump: flat ~29-30.6% from 08:00 to 08:50, then 08:55 bucket avg 56.67% (max 96.36), 09:00 avg 95.87% (max 96.81), staying ~95-97% through 09:10.
  - `F13` ✔ portfolio-service shows the same downstream symptom: 239 warnings 'holdings P&L query took <n>ms for account <n>', first_seen 08:59:39Z, with durations up to 8016ms/8391ms; ledger-db attributes the slow trades query to portfolio-service. So at least order-service AND portfolio-service are degraded in the same window.
  - `F16` ✔ Change-to-onset timing bracket: dep-5905 at 08:56:00Z precedes the first degraded signal (ledger-db slow-query warning 08:58:38Z) by ~2.5 min, and precedes the reported onset (08:55-09:00 CPU bucket / 09:00 latency step) within the same 2-4 min; cfg-2903 at 08:00:00Z precedes onset by ~58 min with no intervening degradation (ledger-db CPU flat ~29-30.6% 08:00-08:50, first elevated bucket 08:55). dep-5905 is therefore the tighter-bracketing change.
  - `F18` ✔ Dependency map: order-service's only shared downstream component with portfolio-service is ledger-db (shared=true, owner platform, kind edge 'order-service'->'ledger-db' kind='db'); portfolio-service is not a direct caller of order-service (order-service callers are api-gateway), so portfolio-service can only harm order-service through the shared ledger-db.
**Assumptions**
  - pricing-engine / exchange-gateway / redis latency actually rose — no such metric was retrieved in either direction (F31: metrics source circuit-open).
  - No deployment in the window touched a non-DB dependency (F1 shows only dep-5905, portfolio-service).

### H5: Autovacuum change is coincidental; timeouts pre-date it
The autovacuum change at 08:00 is unrelated noise; ledger-db latency was already trending up before this morning for another reason and the 08:58 step is the culmination. This would mean neither change in the window is causal.

**Causal chain**
1. order-service p99 was flat ~145-155ms right up to 08:56 and then stepped abruptly at 08:58 — a step, not a pre-existing ramp [F8]
2. ledger-db CPU was flat 29-30.6% 08:00-08:50 with no pre-onset trend, contradicting pre-existing degradation [F12]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ order-service p99 latency was flat at ~145-155ms through 08:56Z (e.g. 08:56 avg 151.35ms, max 153.11ms), then the 08:58-09:00 bucket jumped to avg 2878.21ms / max 5607.8ms and the 09:00 bucket to avg 5282.68ms; it stayed ~5000-5700ms through 10:55. This is a step change, not a gradual ramp.
  - `F12` ✔ ledger-db CPU stepped concurrently with the latency jump: flat ~29-30.6% from 08:00 to 08:50, then 08:55 bucket avg 56.67% (max 96.36), 09:00 avg 95.87% (max 96.81), staying ~95-97% through 09:10.
  - `F11` ✔ The EARLIEST degraded signal is ledger-db slow-query warnings starting 08:58:38Z, and all 360 WARN entries carry the same signature: 'slow query <n>ms: SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service'. The application_name is portfolio-service, NOT order-service.
  - `F16` ✔ Change-to-onset timing bracket: dep-5905 at 08:56:00Z precedes the first degraded signal (ledger-db slow-query warning 08:58:38Z) by ~2.5 min, and precedes the reported onset (08:55-09:00 CPU bucket / 09:00 latency step) within the same 2-4 min; cfg-2903 at 08:00:00Z precedes onset by ~58 min with no intervening degradation (ledger-db CPU flat ~29-30.6% 08:00-08:50, first elevated bucket 08:55). dep-5905 is therefore the tighter-bracketing change.
**Assumptions**
  - None — advanced only to be tested and is falsified by the flat pre-onset baselines.
**Rejected because:** F8 shows a clean step change from a flat ~150ms baseline (no pre-onset ramp) and F12 shows ledger-db CPU flat at ~30% until the 08:55 bucket. There is no evidence of pre-existing degradation; the fault clearly begins at 08:58, tightly after dep-5905 at 08:56.

## Facts (verified observations)
- Only one deployment occurred across all services in the 24h incident window: dep-5905, portfolio-service v4.5.0, deployed 2026-05-05T08:56:00Z, change 'new query on trades filtered by account_id + executed_at', 'no migration included', status live, rolled_back_at null. [F1, F23]
- Only one config change occurred across all services in the window: cfg-2903 on ledger-db, autovacuum_vacuum_scale_factor 0.2 -> 0.1, at 2026-05-05T08:00:00Z, and it is still in effect with no revert entry as of 10:59Z. [F2, F22]
- order-service was not modified: a config-change query scoped to order-service for 2026-05-01..2026-05-05 returned count 0, and no deployment touched order-service in the window. [F3, F1]
- No config change occurred near onset: a narrowed 08:40-09:20Z config query returned count 0. [F14]
- ledger-db is a shared postgres datastore (owner platform) with four direct DB callers: order-service, portfolio-service, payment-service and settlement-service. [F4]
- order-service's only shared downstream component with portfolio-service is ledger-db, and portfolio-service is not a direct caller of order-service (order-service's callers are api-gateway), so portfolio-service can only harm order-service through the shared ledger-db. [F18, F5]
- The earliest degraded signal is ledger-db slow-query warnings beginning 08:58:38Z; all 360 WARN entries carry the identical signature 'slow query <n>ms: SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907) application_name=portfolio-service' — application_name is portfolio-service, not order-service. [F11]
- order-service p99 latency was flat at ~145-155ms through 08:56Z (08:56 avg 151.35ms, max 153.11ms), then jumped to avg 2878.21ms/max 5607.8ms in the 08:58-09:00 bucket and avg 5282.68ms in the 09:00 bucket, staying ~5000-5700ms through 10:55Z — a step change, not a ramp. [F8]
- order-service 5xx error_rate stepped from a ~0.2% baseline (08:00-08:48, 0.20-0.24) to ~9.4% at the 09:00 bucket and remained ~9.2-9.5% through 10:48Z. [F9]
- The dominant order-service error signature is a ledger-db statement timeout on INSERT: 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders) orderId=...' with 284 occurrences and first_seen 09:00:42Z. [F10]
- ledger-db CPU stepped concurrently with the latency jump: flat ~29-30.6% from 08:00 to 08:50, then 08:55 bucket avg 56.67% (max 96.36), 09:00 avg 95.87% (max 96.81), staying ~95-97% through 09:10Z. [F12]
- ledger-db connections_active stepped from a flat ~58-65 (08:45-08:55 buckets) to 122.61/185.11 at 08:57 and 195.84/199.81 at 08:59, remaining ~178-195 through 09:15Z — the step begins in the 08:55-08:57 bucket, i.e. at the 08:56:00Z deploy time. [F26]
- portfolio-service shows the same downstream symptom: 239 warnings 'holdings P&L query took <n>ms for account <n>' first_seen 08:59:39Z with durations up to 8016ms/8391ms. [F13]
- Timing bracket: dep-5905 at 08:56:00Z precedes the first degraded signal (08:58:38Z) by ~2.5 min, while cfg-2903 at 08:00:00Z precedes onset by ~58 min with no intervening degradation (CPU flat ~29-30.6% from 08:00 to 08:50 and no config change near onset). [F14]
- The only autovacuum evidence in 08:45-09:15Z is two INFO log lines 'autovacuum: processed table public.audit_log' at 08:47:50Z and 08:52:26Z — both before the 08:55 CPU/connection step and naming audit_log, not trades. [F28]
- ledger-db's metric catalogue contains no pg_stat_statements/total_exec_time series, no autovacuum worker-count/activity series and no IOPS/throughput series; available metrics are exactly connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min, and a disk_io request returned not_found. [F25]
- ledger-db disk_used_pct is flat and mild across 08:45-09:15Z (min 69.81, max 72.48, mean 71.17) with no step at onset; it is a fill-level gauge and cannot confirm or refute an IOPS-saturation mechanism. [F27]
- The metrics data source was unavailable (circuit_open) across repeated attempts, so no latency_p99_ms or error_rate series could be retrieved for payment-service, settlement-service or order-service in 08:45-09:15Z. [F31]
- payment-service and settlement-service produced no WARN-or-worse log entries in 08:45-09:15Z (total_matched 0, within their covered log ranges), and settlement-service's full-range log profile is routine with no statement-timeout, slow-query or DB-error pattern. [F32, F33, F34]
- No index/migration/schema registry tool exists in this toolset and no migration record for the trades table could be retrieved; the only schema-adjacent fact is dep-5905's change list reading 'no migration included'. [F20]
- Historical precedents include HIST-111 (2025-05-22) 'Reporting job full table scans slowed ledger-db' with root_cause 'New report query without index did sequential scans', category bad_deployment, and HIST-110 (2026-01-15) 'Slow orders from ledger-db IOPS throttling'; both are leads, not evidence about the current incident. [F6, F19]

## Assumptions (not verified)
- Assumption: the 96% ledger-db CPU in 08:55-09:10Z is predominantly consumed by the portfolio-service trades seq-scan query. This is inferred from the slow-query log signature plus the connections_active step at 08:56Z, not from a direct per-query execution-time measurement, which ledger-db does not expose.
- Assumption: the new trades query is genuinely not index-supported at execution time (index absent on trades(account_id, executed_at) or unusable). The verbatim runtime plan shows a Seq Scan, but no index registry, schema snapshot or migration history could be retrieved to confirm absence versus obsolescence/bloat.
- Assumption: autovacuum workers did not launch and consume significant CPU/IO starting near 08:56Z. No autovacuum activity metric exists and the only autovacuum log lines (08:47:50Z, 08:52:26Z on audit_log) precede onset.
- Assumption: ledger-db load did not grow organically into a provisioned CPU/IOPS or connection ceiling at 08:55-09:00Z independent of any change. No IOPS/queue-depth or traffic-growth series exists, and the CPU and connection steps land exactly at the 08:56Z deploy and the 08:58Z seq-scan signature.
- Assumption: no change to a shared dependency (ledger-db, kafka, redis-cache, pricing-engine, exchange-api) outside the 2026-05-04T10:59Z-2026-05-05T10:59Z window caused or contributed to the degradation; the change enumeration was limited to that window.
- Assumption: no non-DB dependency (pricing-engine http, exchange-gateway/exchange-api, redis-cache) became slow in this window. No such latency metric was retrieved in either direction (metrics source circuit-open), and the only deployment in the window is portfolio-service.
- Assumption: payment-service and settlement-service were not visibly degraded in 08:45-09:15Z. This rests on empty WARN+ log queries only; their latency_p99_ms and error_rate are entirely unknown because query_metrics was circuit-open.
- Assumption: the reported p99 (~5s) and 5xx (~9.4%) figures remain representative of the 10:55-10:59Z final minutes, since no fresh metric re-query succeeded at the end of the investigation.
- (demoted from facts: no verified observation backs it) At runtime the trades query is not index-supported: the ledger-db slow-query text contains the verbatim plan 'Seq Scan on trades (rows=48211907)' for the exact access pattern added by dep-5905.
- (demoted from facts: no verified observation backs it) Neither dep-5905 nor cfg-2903 was rolled back through 10:59Z, and order-service p99/error_rate stayed ~5s / ~9.4% with no rollback intervening, so no post-rollback recovery interval exists in the data.

## Timeline
- `2026-05-05T08:00:00Z` cfg-2903 applied to ledger-db: autovacuum_vacuum_scale_factor lowered 0.2 -> 0.1 ('More aggressive autovacuum'). [F2]
- `2026-05-05T08:00:00Z-08:50:00Z` Quiescent period after cfg-2903: ledger-db CPU flat at ~29-30.6% and order-service p99 flat at ~145-155ms; no degradation. [F12, F8, F17]
- `2026-05-05T08:47:50Z` Autovacuum INFO log: 'autovacuum: processed table public.audit_log' (first of only two autovacuum lines, both pre-onset). [F28]
- `2026-05-05T08:52:26Z` Autovacuum INFO log: 'autovacuum: processed table public.audit_log' (second and last autovacuum line, still pre-onset). [F28]
- `2026-05-05T08:56:00Z` dep-5905 deployed: portfolio-service v4.5.0 by k.shah, 'Add realised P&L breakdown to holdings API', new query on trades filtered by account_id + executed_at, no migration included. [F1, F15]
- `2026-05-05T08:56:00Z` order-service p99 still at baseline (08:56 bucket avg 151.35ms, max 153.11ms). [F8]
- `2026-05-05T08:57:00Z` ledger-db connections_active steps from ~58-65 to 122.61/185.11, beginning at deploy time. [F26]
- `2026-05-05T08:58:38Z` Earliest degraded signal: ledger-db slow-query WARN 'Seq Scan on trades (rows=48211907)' for SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2, application_name=portfolio-service. [F11, F21]
- `2026-05-05T08:58:00Z-09:00:00Z` order-service p99 jumps to avg 2878.21ms / max 5607.8ms (08:58-09:00 bucket); ledger-db CPU 08:55 bucket avg 56.67% (max 96.36) and 09:00 bucket avg 95.87%. [F8, F12]
- `2026-05-05T08:59:39Z` portfolio-service warns 'holdings P&L query took <n>ms for account <n>' with durations up to 8016ms/8391ms; ledger-db connections_active reaches 195.84/199.81 at 08:59. [F13, F26]
- `2026-05-05T09:00:42Z` First ledger-db statement timeout on INSERT INTO orders in order-service logs (orderId=15a39d3b); signature recurs 284 times. [F10]
- `2026-05-05T09:00:00Z` order-service 5xx error_rate steps from ~0.2% to ~9.4%; p99 settles at ~5282.68ms. [F9, F8]
- `2026-05-05T09:10:00Z` Incident INC-009 reported by on-call SRE: order-service p99 > 5s with ledger-db statement timeouts. [F7]
- `2026-05-05T09:10:00Z-10:59:00Z` No rollback of dep-5905 or cfg-2903; order-service p99 stays ~5000-5700ms and 5xx ~9.2-9.5% through the latest data, so no recovery interval exists. [F23, F22, F24, F8, F9]

## Recommended actions
- **[immediate]** Immediately roll back dep-5905 (portfolio-service v4.5.0), the only deployment in the incident window and the only change tightly bracketing onset (08:56Z deploy vs 08:58:38Z first slow-query signal). This is both the likely mitigation and the decisive causal test: p99/5xx/CPU/connections should recover within minutes if H1 holds. -- dep-5905 added the exact query shape that appears as the earliest degraded ledger-db signal with a 48.2M-row Seq Scan, and it precedes the CPU (~30%->96%), connections_active (~60->195), p99 (~151ms->5.3s) and 5xx (~0.2%->9.4%) steps by ~2.5 min. It is still live (rolled_back_at null) and no other change brackets the onset. [F1, F11, F15, F16, F21, F23, F12, F26, F8, F9]
- **[immediate]** If rollback of a live feature is not acceptable, apply the schema fix instead: add an index on trades(account_id, executed_at) on ledger-db and confirm the plan for 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at' changes from 'Seq Scan on trades' to an index scan. -- The runtime plan is verbatim 'Seq Scan on trades (rows=48211907)' and dep-5905 shipped with 'no migration included'; the historical precedent HIST-111 was resolved exactly this way ('Added index; query review gate'). [F21, F15, F20, F6]
- **[immediate]** Do NOT treat the ledger-db autovacuum change (cfg-2903) as the primary mitigation target, and do not revert it as the first action: the only autovacuum log evidence predates onset and names audit_log, and CPU was flat for ~55 minutes after the 08:00Z change. If a revert is cheap and safe, it can be done second as a confirmatory test with a clear before/after window. -- cfg-2903 has plausible mechanism but poor timing (58-min quiescent gap) and no supporting telemetry; reverting it first would confound the dep-5905 rollback test. [F2, F16, F17, F28, F30]
- **[short_term]** Instrument the discriminating measurement that is currently missing: enable pg_stat_statements (or equivalent per-query/per-application_name exec-time and CPU attribution) plus an autovacuum worker-activity metric and a disk IOPS/queue-depth series on ledger-db, then capture them during a window with the offending query running. -- ledger-db's catalogue has no pg_stat_statements/total_exec_time, no autovacuum-activity and no IOPS/throughput series, so the dominant-CPU-consumer attribution (H1) and the autovacuum (H2) and IOPS-capacity (H3) mechanisms are all currently unmeasurable. This is the single highest-value observability fix. [F25, F27, F29, F30]
- **[short_term]** Complete the scope check the investigation could not finish: after the fix, measure latency_p99_ms and error_rate (and WARN+ logs) for all four ledger-db callers — order-service, portfolio-service, payment-service and settlement-service — to verify the shared-datastore footprint collapses for every caller, not just order-service. -- H1 predicts all co-callers of the shared ledger-db degrade together; order-service and portfolio-service are confirmed degraded, while payment-service and settlement-service only have empty WARN+ logs (no metric coverage) because query_metrics was circuit-open. [F4, F18, F13, F32, F33, F31, F36]
- **[long_term]** Add a pre-merge query review gate plus a staging/production guard that rejects or flags new full-table-scan queries on large shared tables (e.g. force a plan check or require an index migration when a query touches trades), and alert on ledger-db slow-query warnings and connections_active step changes. -- HIST-111 was the same failure mode on the same shared datastore ('New report query without index did sequential scans') and its recorded resolution was 'Added index; query review gate' — a control that evidently did not prevent this recurrence. [F6, F19, F11, F26]
- **[long_term]** Repair the metrics pipeline so query_metrics is no longer circuit-open and restore the missing signal set (disk IOPS/queue depth, autovacuum activity, per-query exec time); during this incident no latency or error metric could be read for any of the three services on repeated attempts. -- The circuit-open metrics source is itself a contributor to the inconclusive outcome: it blocked the payment/settlement scope check and made the orderservice-series dependent on earlier partial captures. [F31, F36, F25]

## Data gaps
- No per-query execution-time or CPU attribution exists for ledger-db (no pg_stat_statements, pg_stat_activity or total_exec_time-by-application_name series; available metrics are only connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min). The claim that the portfolio-service trades seq scan consumes the 96% CPU is therefore an inference from the slow-query log signature plus the connections_active step, not a direct measurement.
- No index registry, schema snapshot or migration history is available for ledger-db or the trades table, so it cannot be stated whether an index on trades(account_id, executed_at) or on account_id exists, existed before 2026-05-05, or was created after onset. The verbatim runtime 'Seq Scan on trades (rows=48211907)' plan is the only index-related evidence.
- No autovacuum worker-count or activity metric exists, and the only autovacuum log lines (08:47:50Z, 08:52:26Z on audit_log) precede onset. H2 (autovacuum-induced CPU/IO contention) can therefore be neither confirmed nor excluded directly.
- No disk IOPS/throughput or queue-depth series exists; disk_used_pct is a flat fill-level gauge (~70-72%) across the window. H3 (IOPS throttling / capacity, as in HIST-110) cannot be confirmed or excluded.
- query_metrics was circuit-open across repeated attempts, so no latency_p99_ms or error_rate series could be read for payment-service, settlement-service or order-service in 08:45-09:15Z. The core baseline-vs-step-up comparison is therefore unavailable for those services, and payment-service health in this window is entirely unknown (empty log result only).
- Change enumeration (deployments and config changes) was scoped to 2026-05-04T10:59Z-2026-05-05T10:59Z; deployments/config changes to shared dependencies outside that window were not enumerated (ledger-db config query timed out; ledger-db deployment query returned 503).
- No non-DB dependency latency metrics (pricing-engine, exchange-gateway/exchange-api, redis-cache) were retrieved in either direction, so H4 (slow external dependency holding DB transactions open) cannot be directly excluded, only disfavored by the concurrent ledger-db slow-query attribution.
- Log result sets were truncated (ledger-db 40/360, order-service 40/285, portfolio-service 20/242), so later entries and any additional distinct error signatures beyond ~09:16Z were not inspected; the ledger-db WARN query filtered by 'execut' to enumerate every distinct slow-query shape was never executed before the tool-call budget ran out.
- No fresh metric re-query succeeded for the final 10:55-10:59Z minutes, so it could not be independently confirmed whether order-service p99/error_rate had begun recovering; the 'no recovery because no rollback' statement rests on earlier captured series.
- Historical search was limited (one query returned 503 before a successful retry, and a second query on 'autovacuum tuning caused high CPU' could not be run), so whether any past incident was specifically caused by autovacuum tuning remains unanswered.

## What would change this conclusion
- Rollback of dep-5905 (or addition of an index on trades(account_id, executed_at)) followed by recovery of order-service p99/5xx, ledger-db CPU and connections_active would promote H1 from low-confidence to confirmed root cause; conversely, no recovery after the query is removed or indexed would falsify or severely weaken H1.
- Per-query execution-time/CPU attribution (pg_stat_statements or pg_stat_activity) showing that the 96% ledger-db CPU is consumed by queries other than the portfolio-service trades statement — or by autovacuum workers on the trades table — would move support to H2 or to a different consumer.
- Direct observation of autovacuum worker activity (worker count, duration, or CPU/IO) on ledger-db starting immediately around 08:56Z, particularly on the trades table, would substantiate H2 and overturn the timing-based exclusion of the 08:00Z config change.
- Disk IOPS/queue-depth evidence showing device-level saturation beginning before or independent of the 08:56Z deploy (as in HIST-110) would support H3 capacity saturation over H1.
- Evidence that a non-DB dependency (pricing-engine, exchange-gateway/exchange-api, redis-cache) latency rose at the same time and that order-service held ledger-db transactions open across those calls would support H4.
- Discovery of a ledger-db-side migration, index build/drop or deployment earlier than the 24h window, or of another shared-dependency change outside the window, would reopen the origin question; finding that an index on trades(account_id, executed_at) already existed before 08:56Z would collapse H1's mechanism.
- Latency/error metrics for payment-service and settlement-service (currently unobtainable due to circuit-open metrics) showing they also stepped up at 08:56-09:00Z would strengthen H1's predicted shared-datastore footprint; showing they did not degrade would weaken the shared-resource saturation narrative and favour a narrower fault.
- Log evidence of additional distinct slow-query or error signatures on ledger-db from applications other than portfolio-service in 08:45-09:15Z would challenge the single-statement attribution underpinning H1.

## Integrity warnings (set by code)
- fact demoted to assumption: At runtime the trades query is not index-supported: the ledger-db slow-query tex
- fact demoted to assumption: Neither dep-5905 nor cfg-2903 was rolled back through 10:59Z, and order-service 

## Investigation stats
- latency 261.98s, LLM calls 31, tokens 253976 (in 212286 / out 41690)
- tool calls 47 (14 failed, 0 partial, 9 recovered by retry), findings 35/36 verified
