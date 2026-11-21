# Incident investigation report: INC-009
*System: multi_agent | outcome: **inconclusive** | ended: critic_disabled*

## Summary
The investigation ended inconclusive. The reviewed top hypothesis (H2, shared-host resource exhaustion / IOPS throttling starving ledger-db, confidence 0.10) is well below the 0.50 support threshold: it is supported only by historical precedent (F5) and is contradicted by the observed metrics (F17 shows flat disk usage; F12/F16 show the load is a portfolio-service Seq Scan, not a storage event). The evidence strongly points to a different, non-ranked mechanism: portfolio-service v4.5.0 deployed at 08:56Z (F1) introduced an unindexed Seq Scan over the 48.2M-row trades table (F12/F15), which drove ledger-db CPU to ~95-96% and tripled active connections (F10), flooding the shared DB with multi-second queries (F9/F16) and causing collateral statement timeouts on order-service INSERTs (F8/F18). The reporter's autovacuum suspicion (H1) is rejected on timing and mechanism (F7, F14), and no lock/blocking evidence exists (F13). Because the reviewed ranking fixed H2 on top at 0.10, and the alternative explanation — while strongly evidenced — has not been formally ranked and still rests on unavailable host/IOPS telemetry, the honest outcome is inconclusive; immediate mitigation should focus on the 08:56Z portfolio-service change.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: Shared-host resource exhaustion / IOPS throttling starves ledger-db (HIST-110 pattern) | infrastructure_failure | ledger-db hosting/storage layer | null | 0.10 (low) | active |
| 2 | H1: DBA autovacuum scale-factor change on ledger-db degrades query latency (reporter's suspicion) | config_change | ledger-db | tc-002 | 0.05 (low) | rejected |
| 3 | H3: A caller other than order-service creates lock contention / long transactions on ledger-db | data_issue | ledger-db | tc-002 | 0.05 (low) | rejected |

### H2: Shared-host resource exhaustion / IOPS throttling starves ledger-db (HIST-110 pattern)
ledger-db's host or storage volume is starved of a shared resource (cloud volume burst credits exhausted -> IOPS throttle, or another tenant's workload) independent of any order-service or portfolio-service change, and the reported latency is the visible symptom of that infrastructure/resource degradation. HIST-110 (2026-01-15) recorded exactly this mechanism for the identical symptom set.

**Causal chain**
1. Host/volume resource for ledger-db (IOPS burst credits or co-tenant load) is exhausted, throttling IO *(assumed)*
2. ledger-db query execution slows for all callers, including order-service's INSERTs [F8]
3. order-service p99 exceeds 5s with statement timeouts (F5 historical precedent, HIST-110) [F5]

**Supporting evidence**
  - `F5` ✔ Historical precedent HIST-110 (2026-01-15) matches the current symptom set most closely: 'Slow orders from ledger-db IOPS throttling', services [order-service, ledger-db], symptoms [order latency, statement timeouts, slow queries]; root cause was cloud volume burst credits exhausted -> IOPS throttled (not autovacuum). HIST-111 (2025-05-22) shows ledger-db slow queries/high db cpu caused by a new report query without index (bad_deployment), involving settlement-service.
**Contradicting evidence**
  - `F17` ✔ Disk-related saturation is absent: ledger-db disk_used_pct is essentially flat and slowly declining across 08:00-09:30Z (avg ~70.7-71.8%, min 69.62, max 72.48, mean 71.03), with no step at 08:56Z. Combined with the CPU step, this indicates the bottleneck is CPU/query execution, not disk fill or an IO volume event.
  - `F12` ✔ ledger-db slow-query logs (185 WARN matches, first 08:58:38Z) are ALL the same pattern issued by application_name=portfolio-service: 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907)'. No slow query in the returned window is attributed to order-service; order-service only appears as the client whose INSERTs are cancelled.
  - `F16` ✔ The trades Seq Scan onset at the minute level: slow_queries_per_min is flat ~0.7-1.1/min through the 08:53Z bucket (avg 1.06), then jumps to avg 47.34 (max 139.17) in the 08:56Z bucket and ~137-146/min from 08:59Z on. This matches the 08:56Z portfolio-service v4.5.0 deployment, i.e. the slow-query flood starts with/just after the new query going live, not at the 08:00Z autovacuum change.
  - `F9` ✔ ledger-db slow_queries_per_min jumped in the same 08:56Z bucket: steady ~0.8-1.3/min from 08:00-08:52Z, then 08:56Z avg 70.42 (max 139.68) and 09:00Z avg 143.45, holding ~135-148/min thereafter.
  - `F11` ✔ portfolio-service latency_p99_ms shows the same step and is the worst hit: steady ~174-195ms through 08:52Z, then 08:56Z avg 3574.3 (max 7056.7) and 09:00Z avg 6966.25, holding ~6800-7200ms through 10:00Z — worse than order-service (~5200ms).
**Assumptions**
  - an IOPS/burst-credit mechanism exists on this volume (no metric available to test)
  - infra changes in the window are unverified due to missing tools

### H1: DBA autovacuum scale-factor change on ledger-db degrades query latency (reporter's suspicion)
The 08:00Z change of autovacuum_vacuum_scale_factor from 0.2 to 0.1 on ledger-db made autovacuum more aggressive, causing vacuum IO/lock competition (or bloat-driven plan/IO behaviour change) that slowed ledger-db queries, which order-service experienced as statement timeouts on INSERT INTO orders.

**Causal chain**
1. ledger-db autovacuum scale factor changed 0.2 -> 0.1 at 08:00Z, only ledger-db config change in window (F2) [F2]
2. More aggressive autovacuum would run vacuums on trades, competing with query IO/CPU, degrading all callers of ledger-db *(assumed)*
3. order-service INSERTs then slow and time out, giving p99 > 5s [F8]

**Supporting evidence**
  - `F2` ✔ Exactly one config change was recorded in the window: ledger-db 'autovacuum_vacuum_scale_factor' changed from 0.2 to 0.1 at 2026-05-05T08:00:00Z by dba-team, described 'More aggressive autovacuum'. This is the only ledger-db config change through 10:59Z.
**Contradicting evidence**
  - `F14` ✔ Autovacuum activity is NOT anomalous or IO-heavy: ledger-db INFO logs show the routine 'autovacuum: processed table public.audit_log' cycle continuing at the same cadence (~every 4-30 min) both before and after the 08:00Z config change, up to 08:52:26Z and 10:55:51Z. The only autovacuum-related config line is 'config change cfg-2903 applied: autovacuum_vacuum_scale_factor=0.1' at 08:00:20Z. No vacuum-on-trades, no vacuum blocking, no long-running vacuum entry appears. This argues against autovacuum heavy-vacuum IO competition as the mechanism.
  - `F13` ✔ Ledger-db logs contain NO lock contention, no blocking-chain and no deadlock evidence in the incident window: a WARN search for 'lock' over 08:50-10:59Z returned 0 matches, and a search for 'deadlock' over 05:00-10:59Z returned 0 matches. The only WARN pattern in ledger-db is the portfolio-service slow query. This rules out lock-wait/blocking-chain as the timeout mechanism.
  - `F7` ✔ order-service latency_p99_ms shows a step change: steady ~143-167ms from 08:00-08:52Z, then the 08:56Z bucket (4-min bucket starting 08:56) already averages 1514.78ms (max 5607.8) and the first fully-over-5s bucket is 09:00Z at avg 5223.1ms; it then stays ~5000-5500ms through 10:00Z. Not gradual.
  - `F16` ✔ The trades Seq Scan onset at the minute level: slow_queries_per_min is flat ~0.7-1.1/min through the 08:53Z bucket (avg 1.06), then jumps to avg 47.34 (max 139.17) in the 08:56Z bucket and ~137-146/min from 08:59Z on. This matches the 08:56Z portfolio-service v4.5.0 deployment, i.e. the slow-query flood starts with/just after the new query going live, not at the 08:00Z autovacuum change.
  - `F10` ✔ ledger-db connections_active jumped in the same 08:56Z bucket from ~60-64 (08:00-08:52Z) to avg 127.45 (max 199.81) at 08:56Z and ~185-197 thereafter, while cpu_pct went from ~30% to 63.26% (max 96.36) at 08:56Z and ~95-96% from 09:00Z on. Both onset in the same bucket as the order-service and portfolio-service latency step.
**Assumptions**
  - autovacuum could affect the trades table's IO/CPU enough to cause 5s+ timeouts
  - the change is global/per-table in a way that touches trades (target table unknown per gaps)
**Rejected because:** Timing: the config change is at 08:00Z but the latency/CPU/slow-query step is at 08:56Z, a 56-minute quiet gap with steady ~143-167ms p99 in between (F7). Mechanism: autovacuum INFO logs show routine audit_log cycles at unchanged cadence before and after the change, with no vacuum on trades and no blocking (F14), and no lock/deadlock evidence exists (F13). Also the slow-query flood is all attributable to portfolio-service's new Seq Scan (F12, F16), which began at the deployment minute, not the config change.

### H3: A caller other than order-service creates lock contention / long transactions on ledger-db
A service (e.g. settlement-service or payment-service) holds locks or long transactions on ledger-db, and the autovacuum change altered how readily those block; order-service INSERTs into orders are blocked and time out. This treats the autovacuum change as an enabling condition rather than the direct cause.

**Causal chain**
1. Some other ledger-db caller opens long transactions / holds locks on rows order-service's INSERT path needs *(assumed)*
2. order-service INSERT INTO orders waits on the lock and hits the statement timeout [F8]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F13` ✔ Ledger-db logs contain NO lock contention, no blocking-chain and no deadlock evidence in the incident window: a WARN search for 'lock' over 08:50-10:59Z returned 0 matches, and a search for 'deadlock' over 05:00-10:59Z returned 0 matches. The only WARN pattern in ledger-db is the portfolio-service slow query. This rules out lock-wait/blocking-chain as the timeout mechanism.
  - `F12` ✔ ledger-db slow-query logs (185 WARN matches, first 08:58:38Z) are ALL the same pattern issued by application_name=portfolio-service: 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907)'. No slow query in the returned window is attributed to order-service; order-service only appears as the client whose INSERTs are cancelled.
  - `F10` ✔ ledger-db connections_active jumped in the same 08:56Z bucket from ~60-64 (08:00-08:52Z) to avg 127.45 (max 199.81) at 08:56Z and ~185-197 thereafter, while cpu_pct went from ~30% to 63.26% (max 96.36) at 08:56Z and ~95-96% from 09:00Z on. Both onset in the same bucket as the order-service and portfolio-service latency step.
**Assumptions**
  - lock waits exist but were not logged (logging-omission caveat from gaps)
**Rejected because:** F13: searches for 'lock' (08:50-10:59Z) and 'deadlock' (05:00-10:59Z) in ledger-db logs returned zero matches; the sole WARN pattern is portfolio-service's slow query. No blocking-chain evidence exists, and the observed signatures (CPU ~96%, connections tripling) are consistent with CPU contention, not lock waits.

## Facts (verified observations)
- Exactly one deployment occurred in the 2026-05-05T00:00Z-09:30Z window: portfolio-service v4.5.0 at 08:56:00Z (change: new query on trades filtered by account_id + executed_at, no migration). No order-service deployment occurred. [F1]
- Exactly one config change was recorded: ledger-db autovacuum_vacuum_scale_factor changed 0.2 to 0.1 at 08:00:00Z by dba-team; this was the only ledger-db config change through 10:59Z. [F2]
- ledger-db is a shared postgres called directly by four services (order-service, portfolio-service, payment-service, settlement-service). [F3]
- order-service latency_p99_ms stepped from steady ~143-167ms (08:00-08:52Z) to 1514.78ms avg (max 5607.8) in the 08:56Z bucket, then ~5000-5500ms through 10:00Z; the change was not gradual. [F7]
- The first order-service ERROR (statement timeout on INSERT INTO orders) was at 09:00:42Z; 142 such ERRORs occurred in 08:00-10:00Z, all matching 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)'. [F8]
- ledger-db slow_queries_per_min jumped in the 08:56Z bucket from ~0.8-1.3/min to avg 70.42 (max 139.68), then ~135-148/min thereafter. [F9]
- ledger-db connections_active jumped in the 08:56Z bucket from ~60-64 to avg 127.45 (max 199.81), then ~185-197; cpu_pct rose from ~30% to 63.26% (max 96.36) in the same bucket and to ~95-96% from 09:00Z on. [F10]
- portfolio-service latency_p99_ms showed the same step and was the worst hit: ~174-195ms through 08:52Z, then 3574.3 avg at 08:56Z and ~6800-7200ms through 10:00Z. [F11]
- All 185 ledger-db slow-query WARN matches (first 08:58:38Z) are the same pattern issued by application_name=portfolio-service: 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907)'. No slow query in the window is attributed to order-service. [F12, F15]
- Ledger-db logs contain NO lock contention, blocking-chain or deadlock evidence in the incident window ('lock' search 08:50-10:59Z returned 0 matches; 'deadlock' search 05:00-10:59Z returned 0 matches). [F13]
- Autovacuum activity was not anomalous or IO-heavy: ledger-db INFO logs show the routine 'autovacuum: processed table public.audit_log' cycle continuing at unchanged cadence before and after the 08:00Z config change, with no vacuum on trades and no long-running vacuum entry. [F14]
- The trades Seq Scan onset is at the minute level: slow_queries_per_min was flat ~0.7-1.1/min through the 08:53Z bucket, then jumped to avg 47.34 in the 08:56Z bucket and ~137-146/min from 08:59Z on, matching the 08:56Z portfolio-service v4.5.0 deployment rather than the 08:00Z autovacuum change. [F16]
- Disk saturation is absent: ledger-db disk_used_pct is essentially flat and slowly declining across 08:00-09:30Z (mean 71.03%), with no step at 08:56Z, indicating the bottleneck is CPU/query execution, not disk fill or an IO volume event. [F17, F9]
- Interpretation: the statement timeouts are a CPU-contention/collateral effect, not lock waits — the portfolio-service Seq Scan on the 48.2M-row trades table floods ledger-db with multi-second queries, saturating shared CPU so order-service INSERTs exceed their statement timeout; the 08:00Z autovacuum change is temporally separated (55+ min) from the 08:56Z load step. [F12, F16]
- Historical precedent HIST-110 (2026-01-15) matched the current symptom set most closely, with root cause cloud volume burst credits exhausted leading to IOPS throttling; HIST-111 (2025-05-22) shows ledger-db slow queries/high DB CPU caused by a new unindexed report query (bad_deployment). [F5]

## Assumptions (not verified)
- The reviewed ranking is fixed; H2 (shared-host resource exhaustion / IOPS throttling) remains the top-ranked hypothesis at 0.10 confidence but is below the support threshold, so this report does not promote the strongly-evidenced portfolio-service explanation into the formal ranking.
- Determining the causal mechanism (CPU contention from a new unindexed Seq Scan) is treated as an interpretation drawn from verified observations (F12, F15, F16, F10, F18), not as a formally ranked hypothesis.
- The statement timeouts on order-service INSERTs are assumed to be collateral of shared ledger-db CPU saturation rather than resource limits specific to order-service, based on the coincident step in all four series.
- The absence of lock/deadlock evidence is assumed to reflect true absence of lock waits rather than logging omission, though the gaps note list logging omission as a possible explanation.
- Portfolio-service's new query is assumed to be the load source for the slow-query flood because every slow-query WARN in the window carries application_name=portfolio-service, though we did not directly observe request volume for that endpoint.
- The 4-minute metric bucket resolution is assumed adequate to bracket the onset to 08:56Z-09:00Z; finer timing is not established.
- (demoted from facts: no verified observation backs it) The two in-window changes are close to the reported onset (09:10Z): the ledger-db autovacuum change at 08:00Z (70 min before) and the portfolio-service deployment at 08:56Z (14 min before), and portfolio-service shares ledger-db.

## Timeline
- `2026-05-05T08:00:00Z` ledger-db config change cfg-2903 applied: autovacuum_vacuum_scale_factor changed from 0.2 to 0.1 by dba-team; the only ledger-db config change in the window. [F2]
- `2026-05-05T08:00:20Z` Autovacuum routine audit_log cycle continues at unchanged cadence; no vacuum-on-trades and no vacuum blocking observed. [F14]
- `2026-05-05T08:56:00Z` portfolio-service v4.5.0 deployed by k.shah (new query on trades filtered by account_id + executed_at, no migration), status live. [F1]
- `2026-05-05T08:56:00Z` Step change begins in the 08:56Z bucket: order-service p99 143-167ms -> 1514.78ms avg (max 5607.8); portfolio-service p99 -> 3574.3 avg (max 7056.7); ledger-db slow_queries_per_min -> 70.42 avg; ledger-db connections_active -> 127.45 avg and cpu_pct -> 63.26% (max 96.36). [F7, F9, F10, F11]
- `2026-05-05T08:58:38Z` First ledger-db slow-query WARN (8690ms) for the portfolio-service Seq Scan on trades (rows=48211907); all 185 slow-query matches share this pattern. [F12, F15]
- `2026-05-05T09:00:00Z` ledger-db cpu_pct holds ~95-96% and connections_active ~185-197; slow_queries_per_min ~135-148/min; order-service and portfolio-service p99 hold ~5000-5500ms and ~6800-7200ms respectively. [F10, F9, F7, F11, F2, F12]
- `2026-05-05T09:00:42Z` First order-service ERROR: statement timeout on INSERT INTO orders (orderId=15a39d3b); 142 such ERRORs total in 08:00-10:00Z. [F8]
- `2026-05-05T09:10:00Z` Incident INC-009 reported: order-service p99 > 5s with ledger-db statement timeouts. [F6]
- `2026-05-05T10:59:00Z` Latest data; panel still degraded through 10:00Z per metric series. [F9, F11]

## Recommended actions
- **[immediate]** Roll back or disable portfolio-service v4.5.0 (the 08:56Z deployment, change list: new query on trades filtered by account_id + executed_at), or gate the new holdings-API P&L query behind a feature flag, to remove the trades Seq Scan load from ledger-db. -- The slow-query flood begins in the same 08:56Z bucket as the deployment and consists entirely of this new Seq Scan (F12, F15, F16); removing the load is the most direct safe mitigation for the shared DB affecting all four callers. [F1, F12, F15, F16]
- **[immediate]** Add a temporary server-side guard (e.g., lower statement_timeout for the portfolio-service role, or shed/reject the holdings API request) to limit concurrent Seq Scan executions on ledger-db while the fix is prepared. -- Prevents the 48.2M-row sequential scan from saturating shared CPU and continuing to cancel order-service INSERTs; the mechanism is CPU contention, not locking (F10, F13, F18). [F10, F13, F18]
- **[immediate]** Avoid reverting the autovacuum_vacuum_scale_factor change as the primary mitigation; if reverted, do it only as a test and verify no latency improvement to confirm it is not the cause. -- Autovacuum shows no anomalous/heavy activity (F14) and the timing gap (08:00Z change vs 08:56Z step) is 56 minutes with steady latency in between (F7); reverting it alone is unlikely to resolve the incident. [F2, F7, F14]
- **[short_term]** Verify mitigation: confirm ledger-db cpu_pct and slow_queries_per_min return toward baseline and order-service p99 drops below 5s after the new query is removed; then add a supporting index on trades(account_id, executed_at) and re-enable the feature. -- Confirms causal mechanism and provides a durable fix; the query filters on account_id + executed_at with ORDER BY executed_at (F12). [F7, F9, F10, F12]
- **[long_term]** Instrument/deploy a query-level guardrail: CI or pre-deploy check flagging Seq Scans on large tables (trades rows=48211907), and alerting on per-application slow-query rate and shared-DB CPU saturation. -- Prevents recurrence of an unindexed report query on a shared DB; HIST-111 records the same bad_deployment pattern (new unindexed report query causing ledger-db CPU/slow queries). [F5, F12]
- **[long_term]** Adopt DB-host/storage telemetry (IOPS, burst credits, wait events, pg_stat_activity/lock-wait counters) so that the residual H2 IOPS-throttling hypothesis can be directly tested in future incidents. -- No IOPS/burst-credit or wait-event metric exists, leaving H2 untestable; HIST-110 recorded exactly this mechanism for the same symptom set (F5), and the gaps note these data are unavailable. [F5]

## Data gaps
- Cannot state which ledger-db tables the autovacuum change applied to, nor whether other autovacuum parameters changed (config listing returned only one entry, no target-table field).
- Feature-flag and non-DB infra changes in the window are unverified (no tool beyond get_deployments/get_config_changes).
- No host/node-level or IOPS/burst-credit telemetry was available, so the H2 IOPS-throttling hypothesis cannot be directly tested; only ledger-db cpu_pct (peak ~96-98%) was observable.
- ledger-db exposes no query_latency, lock_waits, statement_timeout_count, wait-event, pg_stat_activity or pg_locks metrics; statement-timeout evidence comes only indirectly from order-service ERROR logs.
- Onset cannot be pinned below the 4-minute bucket size (bracketed to 08:56Z-09:00Z); the CPU step was not re-confirmed at 1-minute resolution due to tool-call budget exhaustion.
- order-service error_rate curve, ledger-db disk_used_pct confirmation at finer resolution and replication_lag_ms were not retrieved before the budget was exhausted.
- The exact blocking-chain/session breakdown cannot be produced: no per-session blocking telemetry exists, and the no-lock conclusion rests on absence of lock/deadlock log lines.
- It cannot be fully excluded that the autovacuum scale-factor change affected trades-table bloat/vacuum behavior; no log line records the change's target table beyond routine public.audit_log processing.

## What would change this conclusion
- Evidence that the portfolio-service slow-query flood and ledger-db CPU step precede the 08:56Z deployment (e.g., minute-resolution series showing onset at or before 08:00Z) would undermine the portfolio-service query as the driver.
- Host/storage telemetry showing IOPS throttling, burst-credit exhaustion or a node-level saturation event at 08:56Z would support the top-ranked H2 (HIST-110 pattern) over CPU contention.
- Log evidence of lock waits, blocking chains or deadlocks (e.g., pg_stat_activity or explicit lock log lines) would contradict F13 and reopen H3.
- A measurable latency improvement immediately after reverting the autovacuum_vacuum_scale_factor change, without removing the new portfolio-service query, would support H1.
- Demonstration that the new trades query is served by an index or is not actually executing a Seq Scan (e.g., query plan without the 48.2M-row scan) would remove the primary load source and force re-evaluation.
- Evidence that one of the other ledger-db callers (payment-service or settlement-service) also degraded before 08:56Z would point to a pre-existing shared-DB condition rather than the new deployment.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: The two in-window changes are close to the reported onset (09:10Z): the ledger-d

## Investigation stats
- latency 100.18s, LLM calls 13, tokens 93932 (in 76475 / out 17457)
- tool calls 24 (2 failed, 0 partial, 0 recovered by retry), findings 17/18 verified
