# Incident investigation report: INC-012
*System: multi_agent | outcome: **root_cause_identified** | ended: budget_exhausted:max_llm_calls*

## Summary
Root cause (confidence 0.72, medium): ledger-db's filesystem filled to 100% because the inactive replication slot 'analytics_cdc' retained WAL (~34GB→77GB at ~2.4–2.9 GB/min), and once WAL/temp and relation files could no longer be extended every writer to the shared ledger-db began failing with 'No space left on device'. This began before the payment-service v6.0.0 deploy (disk was already 89% at 13:00Z and 98.6% at 14:45Z) and did not inflect at 14:46Z, and it never recovered after the 15:12Z rollback (disk pinned at 100%, extend errors continuing to 16:56:40Z), which rules out the writer-code/rollback hypotheses. payment-service v6.0.0 (MANDATE_HOLD / new entry type) is not the origin: its error string, its flat pool/latency profile, and a second writer (order-service) emitting the identical storage error at the same onset all point to the datastore. Immediate fix is on the ledger-db side — reclaim WAL/disk (drop or re-activate the inactive analytics_cdc slot), not further payment-service changes.

## Probable root cause
**Independent ledger-db disk/WAL exhaustion: inactive replication slot 'analytics_cdc' retains WAL until disk full (resource_exhaustion)** -- resource_exhaustion in `ledger-db`, triggered by `null` (confidence 0.72, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: Independent ledger-db disk/WAL exhaustion: inactive replication slot 'analytics_cdc' retains WAL until disk full (resource_exhaustion) | resource_exhaustion | ledger-db | null | 0.72 (medium) | active |
| 2 | H3: v6.0.0 mandate flow accelerated ledger write/WAL volume, tipping an already-near-full disk (bad_deployment as trigger of a resource failure) | bad_deployment | payment-service | dep-6203 | 0.30 (low) | active |
| 3 | H4: Rollback is a red herring: retry storm / queue saturation, not entry-type rejection | unknown | payment-service | dep-6203 | 0.05 (low) | rejected |
| 4 | H6: payment-service-specific resource/downstream failure confined to the changed writer | resource_exhaustion | payment-service | dep-6203 | 0.05 (low) | rejected |
| 5 | H1: MANDATE_HOLD write triggers ledger-db validation/enum contract break (reporter's assumption) | bad_deployment | payment-service | dep-6203 | 0.03 (low) | rejected |
| 6 | H7: Shared kafka broker / producer-consumer contract failure (queue path) | dependency_failure | kafka | dep-6203 | 0.03 (low) | rejected |
| 7 | H5: Version skew / incomplete rollout across the shared journal-entry contract | bad_deployment | payment-service | dep-6203 | 0.02 (low) | rejected |

### H2: Independent ledger-db disk/WAL exhaustion: inactive replication slot 'analytics_cdc' retains WAL until disk full (resource_exhaustion)
An inactive replication slot 'analytics_cdc' on the shared ledger-db retained WAL (growing ~34GB->77GB, ~2.4-2.9 GB/min) driving disk from 89% (13:00Z) to 100% (15:00Z) independent of any deploy. When WAL/temp and relation files could no longer be extended, ledger-db emitted 'No space left on device' and EVERY writer failed. Fault originates in ledger-db storage exhaustion, not in payment-service.

**Causal chain**
1. Inactive replication slot analytics_cdc retains WAL on ledger-db [F19]
2. Retained WAL grows ~34GB->77GB (~2.4-2.9 GB/min), driving disk_used_pct 89%->100% monotonically from 13:00Z, well before the 14:46Z deploy [F19, F17, F34, F36]
3. ledger-db warns at 98% then errors/panics writing pg_wal/xlogtemp and cannot extend base/... files [F18]
4. Disk exhaustion propagates to every shared ledger-db writer, not just the changed one [F11, F27, F28, F29]
5. payment-service, order-service and settlement-service all surface storage-style failures / ~50-60% error rates at the same 14:56-15:00Z onset [F16, F15, F28, F27, F29]
6. Rollback of the writer does not free disk, so failures persist past 15:12Z [F14, F32]

**Supporting evidence**
  - `F16` ✔ Earliest payment-service ERROR is 15:00:26Z, and 100% of the 59 matched ERRORs are a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device'. This rules out validation/reject, timeout, pool-exhaustion and lock/deadlock signatures as the dominant one.
  - `F17` ✔ ledger-db disk_used_pct rises monotonically from 89.18% at 13:00Z to 98.19% at 14:36Z, 99.8% at 14:52Z, and pins at 100.0 from the 15:00Z bucket onward (100%) — i.e. the storage trend predates and is independent of the 14:40Z deploy.
  - `F18` ✔ ledger-db emits the storage-exhaustion directly: WARN 'disk usage 98% on /var/lib/postgresql/data' at 14:48:20Z, then ERROR/PANIC 'could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (first at 15:01:01Z) and 'could not extend file "base/16402/2619"' (first at 15:01:39Z). So the failure is inbound-to-DB storage exhaustion, not a ledger app-tier bug.
  - `F19` ✔ The disk consumption is preceded/explained by ledger-db WAL retention: WARN 'replication slot \'analytics_cdc\' is inactive; retaining <n> MB of WAL' at 14:34:05Z (33916 MB), 14:46:32Z (69518 MB) and 14:49:47Z (77371 MB) — WAL grew from ~34 GB to ~77 GB in ~16 min before disk filled.
  - `F11` ✔ Shared ledger-db is written by FOUR services: order-service, portfolio-service, payment-service and settlement-service (all kind db) — so any ledger-db/schema issue has blast radius beyond payment-service.
  - `F3` ✔ No service named 'ledger-service' exists; the ledger component is the shared datastore 'ledger-db' (type postgres, owner platform, shared=true). payment-service has a direct db edge to ledger-db, and also edges to bank-partner-api (external) and kafka (queue-produce); api-gateway calls payment-service.
  - `F13` ✔ Historical precedents from search_historical_incidents around ledger-db and change-driven failures: HIST-111 (2025-05-22, 'Reporting job full table scans slowed ledger-db', category bad_deployment, resolution 'Added index; query review gate'); HIST-102 (2025-09-02, ledger-db failover, 504 at gateway); HIST-110 (2026-01-15, ledger-db IOPS throttling, order latency, statement timeouts); HIST-114 (2025-04-18, ledger-db disk full from WAL, 'write failures'). These are leads only — none matches a 'new ledger entry type'/'mandate flow' schema-contract failure.
  - `F25` ✔ HIST-114 (2025-04-18) is a near-exact precedent with match_score 7: ledger-db 'disk full from retained WAL', symptoms 'no space left on device','write failures', root cause 'Inactive replication slot retained WAL until disk filled', category resource_exhaustion, resolution 'Dropped slot; added slot-lag alert' — i.e. the same mechanism now seen (inactive analytics_cdc slot) was previously fixed by dropping the slot, not by any schema change.
  - `F27` ✔ order-service, a second shared ledger-db writer, spikes to a ~48-50% error_rate at the same onset as payment-service: flat 0.22% (max 0.29) in the 14:50Z bucket, then 16.1% avg (max 49.57) in the 14:56Z bucket, and sustained ~47-49% through 16:56Z (last avg 47.54). This is the same onset shape as payment-service and shows the fault is datastore-wide, not confined to the changed writer.
  - `F28` ✔ order-service emits the SAME verbatim storage-exhaustion string as payment-service: 236 ERRORs, ALL one pattern 'INSERT INTO orders failed: No space left on device', first at 2026-06-01T15:00:42Z (last 16:57:47Z). This confirms the shared ledger-db disk exhaustion propagates beyond payment-service.
  - `F29` ✔ settlement-service (also a shared ledger-db writer) shows an independent error_rate spike at the same onset: 0.15% in the 14:50Z bucket, then 18.01% avg (max 54.52) at 14:56Z, sustained ~54-56% through 16:56Z (last avg 55.28). This is a THIRD service with the same-timed spike.
  - `F32` ✔ payment-service did NOT recover after the 15:12Z rollback: error_rate remains elevated at ~55-70% (mean 62.29, max 70.45) in every 5-minute bucket from 15:10Z through the final 16:55Z bucket (last avg 63.76), continuing ~1h47m past the rollback. This contradicts the writer-code/rollback hypothesis (H1/H3).
  - `F34` ✔ BEFORE the 14:46Z deploy, disk_used_pct rises monotonically from 89.3% at 13:00Z to 98.62% at 14:45Z (mean 93.93) — i.e. the slope is ~9.3 percentage points per 30 min, the same slope as after 14:46Z. The storage trend is already ~9-10 pp/hour steeper than a '15 minutes after release' onset would allow.
  - `F36` ✔ WAL retained by the inactive slot 'analytics_cdc' grows at ~2.4 GB/min BOTH before and after the deploy: 33916 MB at 14:34:05Z -> 69518 MB at 14:46:32Z is lotionw#35599 MB in 12.45 min ≈ 2.86 GB/min (the 14:46:32Z sample is ~30 s AFTER the 14:46Z deploy, so the big pre-deploy step is pre-deploy growth); 69518 -> 77371 MB from 14:46:32Z to 14:49:47Z is 7853 MB in 3.25 min ≈ 2.42 GB/min. The post-deploy WAL rate is no larger than the pre-deploy rate.
  - `F37` ✔ No ledger-db write-volume/transaction, connection-count, CPU or slow-query metric steps up at 14:46Z: connections_active is flat ~60.5-62.7 (max 66.7) from 14:20Z through 15:08Z; cpu_pct flat ~28.6-31.0 (max 32.8) across 14:20-15:08Z; slow_queries_per_min flat ~0.6-1.3 (max 1.75) with the highest value at 15:08Z, well after the deploy. No step change at 14:44Z->14:48Z in any of them.
  - `F38` ✔ ledger-db replication_lag_ms rises gradually and monotonically from ~3101 ms at 14:20Z to ~3881 ms at 15:08Z, with no discontinuity at the 14:46Z deploy (14:44Z avg 3487.77, 14:48Z avg 3659.38 — same gentle upward drift that was already present from 14:20Z). The lag is a pre-existing, deploy-independent trend consistent with the inactive replication slot, not a post-deploy step.
**Contradicting evidence**
  - `F31` ✔ portfolio-service, a fourth shared ledger-db writer, is COMPLETELY UNAFFECTED: error_rate stays ~0.15-0.25% (max 0.32) across every bucket from 14:50Z through 16:56Z, with no onset at all (summary mean 0.21, max 0.32, missing_points 0). It does NOT show the 'No space left on device' signature; its ERROR-log query also returned total_matched 0.
**Assumptions**
  - The analytics_cdc slot (not write volume) is the dominant driver of the retained-WAL growth; no tool exposes pg_replication_slots to confirm origin/age
  - settlement-service's ~55% metric spike is the same storage signature (its logs show zero ERROR/WARN, so verbatim confirmation is missing)

### H3: v6.0.0 mandate flow accelerated ledger write/WAL volume, tipping an already-near-full disk (bad_deployment as trigger of a resource failure)
payment-service v6.0.0's new mandate flow increased ledger write/WAL volume, advancing the disk-exhaustion threshold and causing the storage error. Fault originates in the release.

**Causal chain**
1. payment-service v6.0.0 (14:46Z) introduces mandate create/approve endpoints writing MANDATE_HOLD [F1, F5, F7]
2. New flow raises ledger write/WAL generation rate above prior baseline *(assumed)*
3. ledger-db disk (already 89-98%) reaches 100% faster [F17, F18, F19]
4. Writers fail on storage; payment-service error rate jumps to 60% [F16, F15]

**Supporting evidence**
  - `F1` ✔ The only deployment in any service within 2026-06-01T13:00Z-16:59Z was payment-service v6.0.0 (dep-6203), deployed 2026-06-01T14:46:00Z by p.nair, summarised as 'New UPI mandate flow' with changes 'mandate create/approve endpoints' and 'new ledger entry type MANDATE_HOLD'; it was rolled back at 2026-06-01T15:12:00Z, 26 minutes after deploy and ~2 minutes after the 15:10Z report.
  - `F5` ✔ The v6.0.0 change introduces a NEW ledger entry type 'MANDATE_HOLD' that payment-service writes into the shared ledger-db; the reported symptom 'ledger write failures' is consistent with the writer, not the datastore, being the changed component. Rollback at 15:12Z only reverts the writer code.
  - `F17` ✔ ledger-db disk_used_pct rises monotonically from 89.18% at 13:00Z to 98.19% at 14:36Z, 99.8% at 14:52Z, and pins at 100.0 from the 15:00Z bucket onward (100%) — i.e. the storage trend predates and is independent of the 14:40Z deploy.
  - `F18` ✔ ledger-db emits the storage-exhaustion directly: WARN 'disk usage 98% on /var/lib/postgresql/data' at 14:48:20Z, then ERROR/PANIC 'could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (first at 15:01:01Z) and 'could not extend file "base/16402/2619"' (first at 15:01:39Z). So the failure is inbound-to-DB storage exhaustion, not a ledger app-tier bug.
  - `F19` ✔ The disk consumption is preceded/explained by ledger-db WAL retention: WARN 'replication slot \'analytics_cdc\' is inactive; retaining <n> MB of WAL' at 14:34:05Z (33916 MB), 14:46:32Z (69518 MB) and 14:49:47Z (77371 MB) — WAL grew from ~34 GB to ~77 GB in ~16 min before disk filled.
**Contradicting evidence**
  - `F34` ✔ BEFORE the 14:46Z deploy, disk_used_pct rises monotonically from 89.3% at 13:00Z to 98.62% at 14:45Z (mean 93.93) — i.e. the slope is ~9.3 percentage points per 30 min, the same slope as after 14:46Z. The storage trend is already ~9-10 pp/hour steeper than a '15 minutes after release' onset would allow.
  - `F35` ✔ AFTER the deploy (14:45Z-15:00Z), disk_used_pct continues along the same trajectory: 98.65 -> 99.26 -> 99.29 -> 99.80 -> 99.97 -> 100.0, i.e. ~1.35 pp over 12 min (~6.8 pp/30 min, slightly SLOWER than the pre-deploy slope, bounded only by hitting 100%). Rate is unchanged-or-lower; there is no inflection at 14:46Z.
  - `F36` ✔ WAL retained by the inactive slot 'analytics_cdc' grows at ~2.4 GB/min BOTH before and after the deploy: 33916 MB at 14:34:05Z -> 69518 MB at 14:46:32Z is lotionw#35599 MB in 12.45 min ≈ 2.86 GB/min (the 14:46:32Z sample is ~30 s AFTER the 14:46Z deploy, so the big pre-deploy step is pre-deploy growth); 69518 -> 77371 MB from 14:46:32Z to 14:49:47Z is 7853 MB in 3.25 min ≈ 2.42 GB/min. The post-deploy WAL rate is no larger than the pre-deploy rate.
  - `F37` ✔ No ledger-db write-volume/transaction, connection-count, CPU or slow-query metric steps up at 14:46Z: connections_active is flat ~60.5-62.7 (max 66.7) from 14:20Z through 15:08Z; cpu_pct flat ~28.6-31.0 (max 32.8) across 14:20-15:08Z; slow_queries_per_min flat ~0.6-1.3 (max 1.75) with the highest value at 15:08Z, well after the deploy. No step change at 14:44Z->14:48Z in any of them.
  - `F15` ✔ payment-service error_rate is flat (~0.19-0.29%) from 14:00Z through the 14:56Z bucket, then jumps to 60.15% in the 15:00Z bucket (max 60.8), sustained ~59-64% through 15:28Z. The climb does NOT begin at ~14:55Z; the last healthy bucket starts 14:56Z and the first failed bucket starts 15:00Z.
  - `F32` ✔ payment-service did NOT recover after the 15:12Z rollback: error_rate remains elevated at ~55-70% (mean 62.29, max 70.45) in every 5-minute bucket from 15:10Z through the final 16:55Z bucket (last avg 63.76), continuing ~1h47m past the rollback. This contradicts the writer-code/rollback hypothesis (H1/H3).
**Assumptions**
  - Write volume from the mandate flow measurably increased WAL/disk growth rate (contradicted by F35/F36 - growth rate is unchanged-or-lower post-deploy)

### H4: Rollback is a red herring: retry storm / queue saturation, not entry-type rejection
Error rate driven by retry storms or queue saturation from the new flow, not by rejected entry type; failures would persist or decay slowly after rollback.

**Causal chain**
1. New mandate flow produces retries/queue pressure [F1]
2. Retry amplification or kafka queue saturation drives sustained error rate *(assumed)*
3. Failure persists after rollback [F14, F32]

**Supporting evidence**
  - `F14` ✔ The v6.0.0 rollback completed at 15:12Z, two minutes after the 15:10Z report; no evidence in the change data indicates whether the rollback restored normal behaviour (the incident window continues to 16:59Z).
  - `F32` ✔ payment-service did NOT recover after the 15:12Z rollback: error_rate remains elevated at ~55-70% (mean 62.29, max 70.45) in every 5-minute bucket from 15:10Z through the final 16:55Z bucket (last avg 63.76), continuing ~1h47m past the rollback. This contradicts the writer-code/rollback hypothesis (H1/H3).
**Contradicting evidence**
  - `F16` ✔ Earliest payment-service ERROR is 15:00:26Z, and 100% of the 59 matched ERRORs are a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device'. This rules out validation/reject, timeout, pool-exhaustion and lock/deadlock signatures as the dominant one.
  - `F18` ✔ ledger-db emits the storage-exhaustion directly: WARN 'disk usage 98% on /var/lib/postgresql/data' at 14:48:20Z, then ERROR/PANIC 'could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (first at 15:01:01Z) and 'could not extend file "base/16402/2619"' (first at 15:01:39Z). So the failure is inbound-to-DB storage exhaustion, not a ledger app-tier bug.
  - `F17` ✔ ledger-db disk_used_pct rises monotonically from 89.18% at 13:00Z to 98.19% at 14:36Z, 99.8% at 14:52Z, and pins at 100.0 from the 15:00Z bucket onward (100%) — i.e. the storage trend predates and is independent of the 14:40Z deploy.
  - `F19` ✔ The disk consumption is preceded/explained by ledger-db WAL retention: WARN 'replication slot \'analytics_cdc\' is inactive; retaining <n> MB of WAL' at 14:34:05Z (33916 MB), 14:46:32Z (69518 MB) and 14:49:47Z (77371 MB) — WAL grew from ~34 GB to ~77 GB in ~16 min before disk filled.
  - `F20` ✔ payment-service itself shows no resource-trend anomaly: db_pool_in_use is flat (avg ~7.75-8.31 connections, max 8.87 across the whole window including the outage) and latency_p99_ms is flat (~312-337 ms) before and during the 15:00Z+ error spike — so the fault is not pool exhaustion or added latency inside payment-service.
  - `F28` ✔ order-service emits the SAME verbatim storage-exhaustion string as payment-service: 236 ERRORs, ALL one pattern 'INSERT INTO orders failed: No space left on device', first at 2026-06-01T15:00:42Z (last 16:57:47Z). This confirms the shared ledger-db disk exhaustion propagates beyond payment-service.
  - `F27` ✔ order-service, a second shared ledger-db writer, spikes to a ~48-50% error_rate at the same onset as payment-service: flat 0.22% (max 0.29) in the 14:50Z bucket, then 16.1% avg (max 49.57) in the 14:56Z bucket, and sustained ~47-49% through 16:56Z (last avg 47.54). This is the same onset shape as payment-service and shows the fault is datastore-wide, not confined to the changed writer.
**Assumptions**
  - a retry/queue mechanism exists independent of the entry type
**Rejected because:** F16/F28 show 100% of errors are a single storage-exhaustion pattern (no timeout/pool/deadlock signatures); F20 shows flat db_pool_in_use and flat p99 latency. While post-rollback persistence (F32) is real, it is fully explained by H2 (disk stays full); no retry/queue signature is present.

### H6: payment-service-specific resource/downstream failure confined to the changed writer
The storage error string in payment-service may originate from a resource or downstream failure specific to payment-service's write path (its own local/temp filesystem, connection to ledger-db, or bank-partner-api/kafka path) rather than from ledger-db's own disk.

**Causal chain**
1. payment-service v6.0.0 write path fails on 'No space left on device' [F16, F1]
2. The 'No space' condition is local to payment-service or its immediate dependency, not ledger-db *(assumed)*
3. Other ledger-db writers would be unaffected [F31]

**Supporting evidence**
  - `F31` ✔ portfolio-service, a fourth shared ledger-db writer, is COMPLETELY UNAFFECTED: error_rate stays ~0.15-0.25% (max 0.32) across every bucket from 14:50Z through 16:56Z, with no onset at all (summary mean 0.21, max 0.32, missing_points 0). It does NOT show the 'No space left on device' signature; its ERROR-log query also returned total_matched 0.
  - `F16` ✔ Earliest payment-service ERROR is 15:00:26Z, and 100% of the 59 matched ERRORs are a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device'. This rules out validation/reject, timeout, pool-exhaustion and lock/deadlock signatures as the dominant one.
**Contradicting evidence**
  - `F18` ✔ ledger-db emits the storage-exhaustion directly: WARN 'disk usage 98% on /var/lib/postgresql/data' at 14:48:20Z, then ERROR/PANIC 'could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (first at 15:01:01Z) and 'could not extend file "base/16402/2619"' (first at 15:01:39Z). So the failure is inbound-to-DB storage exhaustion, not a ledger app-tier bug.
  - `F17` ✔ ledger-db disk_used_pct rises monotonically from 89.18% at 13:00Z to 98.19% at 14:36Z, 99.8% at 14:52Z, and pins at 100.0 from the 15:00Z bucket onward (100%) — i.e. the storage trend predates and is independent of the 14:40Z deploy.
  - `F19` ✔ The disk consumption is preceded/explained by ledger-db WAL retention: WARN 'replication slot \'analytics_cdc\' is inactive; retaining <n> MB of WAL' at 14:34:05Z (33916 MB), 14:46:32Z (69518 MB) and 14:49:47Z (77371 MB) — WAL grew from ~34 GB to ~77 GB in ~16 min before disk filled.
  - `F27` ✔ order-service, a second shared ledger-db writer, spikes to a ~48-50% error_rate at the same onset as payment-service: flat 0.22% (max 0.29) in the 14:50Z bucket, then 16.1% avg (max 49.57) in the 14:56Z bucket, and sustained ~47-49% through 16:56Z (last avg 47.54). This is the same onset shape as payment-service and shows the fault is datastore-wide, not confined to the changed writer.
  - `F28` ✔ order-service emits the SAME verbatim storage-exhaustion string as payment-service: 236 ERRORs, ALL one pattern 'INSERT INTO orders failed: No space left on device', first at 2026-06-01T15:00:42Z (last 16:57:47Z). This confirms the shared ledger-db disk exhaustion propagates beyond payment-service.
  - `F29` ✔ settlement-service (also a shared ledger-db writer) shows an independent error_rate spike at the same onset: 0.15% in the 14:50Z bucket, then 18.01% avg (max 54.52) at 14:56Z, sustained ~54-56% through 16:56Z (last avg 55.28). This is a THIRD service with the same-timed spike.
  - `F11` ✔ Shared ledger-db is written by FOUR services: order-service, portfolio-service, payment-service and settlement-service (all kind db) — so any ledger-db/schema issue has blast radius beyond payment-service.
**Assumptions**
  - payment-service could host or proxy a filesystem that fills independently
**Rejected because:** F18 shows the 'No space left on device'/xlogtemp failure originates inside ledger-db, and F27/F28/F29 show order-service and settlement-service failing at the same onset with order-service emitting the identical storage string. A payment-service-local filesystem failure cannot explain simultaneous failures in three independent writers. F31 is odd but insufficient to localize the fault to payment-service.

### H1: MANDATE_HOLD write triggers ledger-db validation/enum contract break (reporter's assumption)
The new mandate flow writes MANDATE_HOLD into ledger-db; the schema/enum rejects it, producing ledger write failures. Fault originates in the writer's contract change.

**Causal chain**
1. v6.0.0 introduces MANDATE_HOLD ledger entry type [F1, F5, F7]
2. ledger-db schema/enum rejects the unknown entry type *(assumed)*
3. ledger write failures and 60% error rate [F15]

**Supporting evidence**
  - `F1` ✔ The only deployment in any service within 2026-06-01T13:00Z-16:59Z was payment-service v6.0.0 (dep-6203), deployed 2026-06-01T14:46:00Z by p.nair, summarised as 'New UPI mandate flow' with changes 'mandate create/approve endpoints' and 'new ledger entry type MANDATE_HOLD'; it was rolled back at 2026-06-01T15:12:00Z, 26 minutes after deploy and ~2 minutes after the 15:10Z report.
  - `F5` ✔ The v6.0.0 change introduces a NEW ledger entry type 'MANDATE_HOLD' that payment-service writes into the shared ledger-db; the reported symptom 'ledger write failures' is consistent with the writer, not the datastore, being the changed component. Rollback at 15:12Z only reverts the writer code.
  - `F7` ✔ Exactly one deployment exists across all services in the incident window 14:00–15:10Z: payment-service v6.0.0 (dep-6203) deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes include 'new ledger entry type MANDATE_HOLD', status rolled_back at 2026-06-01T15:12:00Z. No ledger-service, DB/schema, or other service deployment is recorded in that window.
  - `F15` ✔ payment-service error_rate is flat (~0.19-0.29%) from 14:00Z through the 14:56Z bucket, then jumps to 60.15% in the 15:00Z bucket (max 60.8), sustained ~59-64% through 15:28Z. The climb does NOT begin at ~14:55Z; the last healthy bucket starts 14:56Z and the first failed bucket starts 15:00Z.
**Contradicting evidence**
  - `F16` ✔ Earliest payment-service ERROR is 15:00:26Z, and 100% of the 59 matched ERRORs are a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device'. This rules out validation/reject, timeout, pool-exhaustion and lock/deadlock signatures as the dominant one.
  - `F17` ✔ ledger-db disk_used_pct rises monotonically from 89.18% at 13:00Z to 98.19% at 14:36Z, 99.8% at 14:52Z, and pins at 100.0 from the 15:00Z bucket onward (100%) — i.e. the storage trend predates and is independent of the 14:40Z deploy.
  - `F18` ✔ ledger-db emits the storage-exhaustion directly: WARN 'disk usage 98% on /var/lib/postgresql/data' at 14:48:20Z, then ERROR/PANIC 'could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (first at 15:01:01Z) and 'could not extend file "base/16402/2619"' (first at 15:01:39Z). So the failure is inbound-to-DB storage exhaustion, not a ledger app-tier bug.
  - `F19` ✔ The disk consumption is preceded/explained by ledger-db WAL retention: WARN 'replication slot \'analytics_cdc\' is inactive; retaining <n> MB of WAL' at 14:34:05Z (33916 MB), 14:46:32Z (69518 MB) and 14:49:47Z (77371 MB) — WAL grew from ~34 GB to ~77 GB in ~16 min before disk filled.
  - `F20` ✔ payment-service itself shows no resource-trend anomaly: db_pool_in_use is flat (avg ~7.75-8.31 connections, max 8.87 across the whole window including the outage) and latency_p99_ms is flat (~312-337 ms) before and during the 15:00Z+ error spike — so the fault is not pool exhaustion or added latency inside payment-service.
  - `F4` ✔ ledger-db had zero deployments and zero config changes between 2026-05-25 and 2026-06-01T16:59Z, so there is no evidence of an independent ledger-db schema/config deploy near onset.
  - `F11` ✔ Shared ledger-db is written by FOUR services: order-service, portfolio-service, payment-service and settlement-service (all kind db) — so any ledger-db/schema issue has blast radius beyond payment-service.
  - `F27` ✔ order-service, a second shared ledger-db writer, spikes to a ~48-50% error_rate at the same onset as payment-service: flat 0.22% (max 0.29) in the 14:50Z bucket, then 16.1% avg (max 49.57) in the 14:56Z bucket, and sustained ~47-49% through 16:56Z (last avg 47.54). This is the same onset shape as payment-service and shows the fault is datastore-wide, not confined to the changed writer.
  - `F28` ✔ order-service emits the SAME verbatim storage-exhaustion string as payment-service: 236 ERRORs, ALL one pattern 'INSERT INTO orders failed: No space left on device', first at 2026-06-01T15:00:42Z (last 16:57:47Z). This confirms the shared ledger-db disk exhaustion propagates beyond payment-service.
  - `F29` ✔ settlement-service (also a shared ledger-db writer) shows an independent error_rate spike at the same onset: 0.15% in the 14:50Z bucket, then 18.01% avg (max 54.52) at 14:56Z, sustained ~54-56% through 16:56Z (last avg 55.28). This is a THIRD service with the same-timed spike.
**Assumptions**
  - ledger-db enforces an enum whitelist MANDATE_HOLD violates
**Rejected because:** F16/F28: 100% of errors in two independent services are 'No space left on device', a storage error, not a validation/enum rejection. F18 confirms inbound DB storage exhaustion. F27/F29 show THREE writers failing simultaneously. No type-rejection signature exists anywhere.

### H7: Shared kafka broker / producer-consumer contract failure (queue path)
v6.0.0's new flow emits changed messages to the shared kafka broker used by order-service and consumed by settlement-service/notification-service; a producer/consumer contract or broker saturation drives multi-service error spikes at the same onset.

**Causal chain**
1. v6.0.0 produces messages to shared kafka [F1, F12]
2. Shared broker/consumer contract issue propagates to payment/order/settlement [F12]
3. Multi-service error spike at ~15:00Z [F15, F27, F29]

**Supporting evidence**
  - `F12` ✔ Shared kafka broker is used by order-service and payment-service (queue-produce), and settlement-service and notification-service (queue-consume) — a producer-side message/schema change from payment-service would propagate to settlement-service and notification-service consumers.
  - `F27` ✔ order-service, a second shared ledger-db writer, spikes to a ~48-50% error_rate at the same onset as payment-service: flat 0.22% (max 0.29) in the 14:50Z bucket, then 16.1% avg (max 49.57) in the 14:56Z bucket, and sustained ~47-49% through 16:56Z (last avg 47.54). This is the same onset shape as payment-service and shows the fault is datastore-wide, not confined to the changed writer.
  - `F29` ✔ settlement-service (also a shared ledger-db writer) shows an independent error_rate spike at the same onset: 0.15% in the 14:50Z bucket, then 18.01% avg (max 54.52) at 14:56Z, sustained ~54-56% through 16:56Z (last avg 55.28). This is a THIRD service with the same-timed spike.
**Contradicting evidence**
  - `F16` ✔ Earliest payment-service ERROR is 15:00:26Z, and 100% of the 59 matched ERRORs are a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device'. This rules out validation/reject, timeout, pool-exhaustion and lock/deadlock signatures as the dominant one.
  - `F28` ✔ order-service emits the SAME verbatim storage-exhaustion string as payment-service: 236 ERRORs, ALL one pattern 'INSERT INTO orders failed: No space left on device', first at 2026-06-01T15:00:42Z (last 16:57:47Z). This confirms the shared ledger-db disk exhaustion propagates beyond payment-service.
  - `F18` ✔ ledger-db emits the storage-exhaustion directly: WARN 'disk usage 98% on /var/lib/postgresql/data' at 14:48:20Z, then ERROR/PANIC 'could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (first at 15:01:01Z) and 'could not extend file "base/16402/2619"' (first at 15:01:39Z). So the failure is inbound-to-DB storage exhaustion, not a ledger app-tier bug.
  - `F30` ✔ settlement-service has NO ERROR or WARN logs at all in 14:50-16:59Z: query for min_level ERROR returned total_matched 0 (data_range 11:00Z-16:59Z, truncated false), and min_level WARN also returned total_matched 0. So its ~55% error spike is visible in metrics but carries no matching application log signature in the log store.
**Assumptions**
  - kafka failures would emit 'No space left on device' error strings in payment-service (unlikely)
**Rejected because:** F16/F28 error strings are DB storage errors ('No space left on device', 'FAILED INSERT'), not kafka produce/consume errors; F18 attributes the failure to ledger-db filesystem; settlement-service shows no queue-consumer log signature (F30). Kafka is not the origin of the observed failure signature.

### H5: Version skew / incomplete rollout across the shared journal-entry contract
Part of the fleet or a dependent service was upgraded so v6.0.0 writers hit old readers. Requires multiple deployments or a partial-rollout record.

**Causal chain**
1. v6.0.0 writers emit new contract while others remain old [F1]
2. Writers hit old readers and writes fail *(assumed)*
3. Skew persists until all services upgraded *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ Exactly one deployment exists across all services in the incident window 14:00–15:10Z: payment-service v6.0.0 (dep-6203) deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes include 'new ledger entry type MANDATE_HOLD', status rolled_back at 2026-06-01T15:12:00Z. No ledger-service, DB/schema, or other service deployment is recorded in that window.
  - `F8` ✔ Widening the deployment query to 13:00–16:59Z still returns only the single dep-6203 record (count 1), i.e. no deployment of any other service (e.g. ledger-service) anywhere near the incident.
  - `F4` ✔ ledger-db had zero deployments and zero config changes between 2026-05-25 and 2026-06-01T16:59Z, so there is no evidence of an independent ledger-db schema/config deploy near onset.
  - `F2` ✔ Across all services there were zero config/flag changes and zero deployments other than payment-service v6.0.0 in the 13:00-16:59Z window, so no flag or config change coincided with the onset.
  - `F16` ✔ Earliest payment-service ERROR is 15:00:26Z, and 100% of the 59 matched ERRORs are a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device'. This rules out validation/reject, timeout, pool-exhaustion and lock/deadlock signatures as the dominant one.
  - `F27` ✔ order-service, a second shared ledger-db writer, spikes to a ~48-50% error_rate at the same onset as payment-service: flat 0.22% (max 0.29) in the 14:50Z bucket, then 16.1% avg (max 49.57) in the 14:56Z bucket, and sustained ~47-49% through 16:56Z (last avg 47.54). This is the same onset shape as payment-service and shows the fault is datastore-wide, not confined to the changed writer.
  - `F28` ✔ order-service emits the SAME verbatim storage-exhaustion string as payment-service: 236 ERRORs, ALL one pattern 'INSERT INTO orders failed: No space left on device', first at 2026-06-01T15:00:42Z (last 16:57:47Z). This confirms the shared ledger-db disk exhaustion propagates beyond payment-service.
  - `F29` ✔ settlement-service (also a shared ledger-db writer) shows an independent error_rate spike at the same onset: 0.15% in the 14:50Z bucket, then 18.01% avg (max 54.52) at 14:56Z, sustained ~54-56% through 16:56Z (last avg 55.28). This is a THIRD service with the same-timed spike.
**Assumptions**
  - multiple deployments or a partial fleet rollout exist
**Rejected because:** F7/F8: exactly one deployment across all services in the window. F4/F2/F22/F23: no ledger-db or config change. F16/F28: failure signature is storage, not a reader contract mismatch.

## Facts (verified observations)
- ledger-db is a shared postgres datastore written by four services (order-service, portfolio-service, payment-service, settlement-service); the failure is datastore-wide, not confined to the changed writer. [F11, F3, F10]
- ledger-db disk_used_pct rose monotonically from 89.18% at 13:00Z to 98.62% at 14:45Z, 99.8% at 14:52Z, and pinned at 100.0% from the 15:00Z bucket onward — the storage trend predates and is independent of the 14:46Z deploy. [F17, F34, F49]
- The disk growth rate did not inflect at the 14:46Z deploy: ~9.3 pp/30 min before the deploy, ~6.8 pp/30 min after (bounded by reaching 100%). [F34, F35]
- WAL retained by the inactive replication slot 'analytics_cdc' grew ~33.9 GB → ~77.4 GB at ~2.4–2.9 GB/min with no larger rate after the deploy (~2.86 GB/min pre-deploy vs ~2.42 GB/min post-deploy). [F19, F36]
- ledger-db itself emitted the storage-exhaustion errors: WARN 'disk usage 98% on /var/lib/postgresql/data' at 14:48:20Z, then ERROR/PANIC 'could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (first 15:01:01Z) and 'could not extend file "base/16402/2619"' (first 15:01:39Z). [F18, F50]
- 100% of payment-service's matched ERRORs (59) are a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device'; earliest at 15:00:26Z. [F16]
- order-service, a second shared ledger-db writer, emitted the identical storage string ('INSERT INTO orders failed: No space left on device', 236 errors, first 15:00:42Z) and spiked to ~48–50% error rate at the same onset. [F27, F28]
- settlement-service, a third shared ledger-db writer, showed an independent error-rate spike at the same onset (0.15% in the 14:50Z bucket → 18.01% avg at 14:56Z, sustained ~54–56% to 16:56Z) with zero ERROR/WARN log lines available in the log store. [F29, F30, F39, F40, F44]
- payment-service shows no resource anomaly of its own: db_pool_in_use flat (~7.75–8.31 connections) and latency_p99_ms flat (~312–337 ms) before and during the error spike. [F20]
- ledger-db write-volume-related metrics show no step at 14:46Z: connections_active flat ~60.5–62.7, cpu_pct flat ~28.6–31.0, slow_queries_per_min flat ~0.6–1.3, and replication_lag_ms rising gently and monotonically from ~3101 ms (14:20Z) to ~3881 ms (15:08Z). [F37, F38, F51]
- payment-service did NOT recover after the 15:12Z rollback: error_rate stayed ~55–70% in every bucket from 15:10Z through the final 16:55Z bucket. [F32]
- The disk-full condition was unresolved at end of data: ledger-db disk_used_pct was exactly 100.0% in every bucket from 15:00Z to 16:57Z, and the base/16402/2619 extend errors continued to 16:56:40Z (~1h44m past rollback). [F49, F50]
- Exactly one deployment existed across all services in the incident window and the full prior week: payment-service v6.0.0 (dep-6203, 'New UPI mandate flow', new entry type MANDATE_HOLD), deployed 14:46Z, rolled back 15:12Z; no ledger-db/schema/migration deployment exists. [F1, F7, F8, F23, F46]
- The change/config tool recorded zero config, flag, schema-migration or replication-slot changes across all services over the full 30-day pre-incident window, and zero ledger-db config/deploy changes over a month. [F21, F22, F45]
- HIST-114 (2025-04-18) is a near-exact precedent (match_score 7): ledger-db 'disk full from retained WAL', symptoms 'no space left on device'/'write failures', root cause 'Inactive replication slot retained WAL until disk filled', resolution 'Dropped slot; added slot-lag alert'. [F25, F47]
- portfolio-service, a fourth shared ledger-db writer, was completely unaffected (error_rate ~0.15–0.25%, no onset, zero matched ERROR logs) despite the disk being full. [F31]
- All 139 extend-failures target one single relation (base/16402/2619) while connections stay flat, so writers whose tables were already allocated are not blocked by the full disk — consistent with one writer remaining unaffected. [F50, F51]
- No named owner/consumer is discoverable for the analytics_cdc slot: no analytics/CDC consumer appears in ledger-db's dependency graph, and 30-day change history shows 0 config changes and 1 unrelated deployment. [F24]
- No historical incident matches a 'new ledger entry type / MANDATE_HOLD / schema-enum' ledger-db failure; the closest schema/deploy precedent (HIST-111) is a missing-index query issue. [F26]

## Assumptions (not verified)
- The analytics_cdc replication slot — rather than mandate-flow write volume — is the dominant driver of the retained-WAL growth; no tool exposes pg_replication_slots to confirm the slot's origin, age or liveness.
- The analytics_cdc slot was already inactive and long-lived before the incident window (only 'retaining <n> MB of WAL' log lines exist; the change-history tools expose no slot creation/drop artifact type), so its disk fill-up is not attributable to a change in the window.
- settlement-service's ~55% metric spike reflects the same ledger-db storage exhaustion as the other writers; its log store contains no ERROR/WARN string to confirm this verbatim (log-store blind spot).
- order-service and settlement-service write to ledger-db tables that were already allocated or write paths that hit the same storage limit; the exact per-writer blocking differs due to relation-level file extension, not a different root cause.
- The relation base/16402/2619 belongs to a table that one of the ledger-db writers (not portfolio-service) writes; log data never names its table/schema owner.
- Any lease/ownership of the analytics_cdc slot by an analytics/CDC pipeline cannot be resolved from available change artifacts; the absence of a named owner may be a tool coverage limit rather than proof no owner exists.
- The 100% disk occupancy at end of data means no remediation (slot drop, WAL archive/free, restart) had taken effect by 16:59Z; this is inferred from the absence of any recovery signal beyond disk/extend-error continuity.

## Timeline
- `2026-06-01T13:00:00Z` ledger-db disk_used_pct already 89.18% and rising monotonically (~9.3 pp/30 min) — ~2.75 hours before the v6.0.0 deploy. [F17, F34]
- `2026-06-01T14:34:05Z` ledger-db WARN: replication slot 'analytics_cdc' is inactive, retaining 33916 MB of WAL. WAL growth begins its measured ramp (~2.9 GB/min). [F19, F36]
- `2026-06-01T14:45:00Z` ledger-db disk_used_pct 98.62% — still pre-deploy; slope unchanged from 13:00Z. [F34, F35]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (dep-6203, 'New UPI mandate flow', new ledger entry type MANDATE_HOLD) deployed by p.nair — the only deployment of any service in the window. [F1, F7, F23]
- `2026-06-01T14:46:32Z` ledger-db WARN: analytics_cdc inactive, retaining 69518 MB of WAL. No step change in ledger-db connections_active, cpu_pct, slow_queries_per_min or replication_lag at deploy time. [F19, F37, F38]
- `2026-06-01T14:48:20Z` ledger-db WARN: 'disk usage 98% on /var/lib/postgresql/data'. [F18]
- `2026-06-01T14:49:47Z` ledger-db WARN: analytics_cdc inactive, retaining 77371 MB of WAL (~2.9 GB/min overall; post-deploy rate ~2.42 GB/min, no larger than pre-deploy). [F19, F36]
- `2026-06-01T14:56:00Z` First errored 5-min buckets: order-service 14:56Z avg 16.1% (max 49.57), settlement-service 14:56Z avg 18.01% (max 54.52). [F27, F29]
- `2026-06-01T15:00:00Z` ledger-db disk_used_pct pins at 100.0%; payment-service error_rate jumps from ~0.2% to 60.15%. [F17, F15, F49]
- `2026-06-01T15:00:26Z` Earliest payment-service ERROR: 'ledger write failed: ERROR: could not extend file: No space left on device' (100% of 59 matched errors are this pattern). [F16]
- `2026-06-01T15:00:42Z` Earliest order-service ERROR: 'INSERT INTO orders failed: No space left on device' (236 errors, last 16:57:47Z). [F28]
- `2026-06-01T15:01:01Z` ledger-db ERROR: 'could not write to file "pg_wal/xlogtemp.<n>": No space left on device'; then 15:01:39Z 'could not extend file "base/16402/2619"'. [F18, F50]
- `2026-06-01T15:10:00Z` Incident INC-012 reported (payment-service error rate 60%, ledger write failures); rollback of v6.0.0 initiated. [F1]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rollback completed, ~2 min after report and 26 min after deploy. [F1, F7, F14]
- `2026-06-01T15:12:00Z–16:56:40Z` No recovery after rollback: payment-service error_rate stays ~55–70%; ledger-db disk_used_pct stays exactly 100.0%; base/16402/2619 extend errors continue to 16:56:40Z; ledger-db connections_active stays flat ~61–64. [F32, F49, F50, F51]

## Recommended actions
- **[immediate]** On ledger-db: free the disk — stop the WAL retention by resolving the inactive replication slot 'analytics_cdc' (drop it if unused, or re-activate/repair its consumer), then archive/remove reclaimable WAL and verify disk_used_pct falls below 100%. Treat as the primary mitigation; do not make further payment-service changes. -- Root cause is ledger-db storage exhaustion from WAL retained by the inactive analytics_cdc slot; disk pinned at 100% and extend errors were still occurring 1h44m after the rollback, so writer-side changes cannot clear it. HIST-114 (same mechanism, match_score 7) was resolved exactly this way ('Dropped slot; added slot-lag alert'). [F19, F25, F47, F49, F50, F32]
- **[immediate]** Do NOT roll payment-service forward again or treat v6.0.0/MANDATE_HOLD as the cause pending disk recovery; keep dep-6203 in its rolled-back state and note in the incident channel that the rollback did not mitigate. -- H3/H1's writer-code mechanism is contradicted: disk slope and WAL rate were unchanged across the 14:46Z deploy, and the 15:12Z rollback produced no recovery while disk stayed at 100%. [F35, F36, F37, F32]
- **[short_term]** Confirm blast radius and recovery: verify ledger-db disk_used_pct returns below 100% and that order-service and settlement-service error rates (spikes at 14:56Z) return to baseline alongside payment-service. -- The fault is datastore-wide; recovery must be observed on all three affected shared writers, not just payment-service. [F11, F27, F28, F29, F49, F32]
- **[short_term]** Add a replication-slot lag/retention monitor and alert on ledger-db (inactive slot → WAL bytes retained → disk_used_pct threshold), and expose pg_wal_size / replication-slot age metrics, which ledger-db currently lacks (its metric set is only connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min). -- The failure was only observable indirectly via disk_used_pct and two sparse 'retaining <n> MB of WAL' WARN lines; a slot-lag alert is the recorded fix for the HIST-114 precedent and would have caught the 34→77 GB ramp before 100%. [F19, F33, F25, F47]
- **[long_term]** Identify and fix the owner/consumer of the analytics_cdc slot (find the analytics/CDC pipeline that stopped consuming) and add a lifecycle gate so CDC slots cannot be left inactive indefinitely. -- The slot is the retention mechanism but its owner/liveness is unresolved — no analytics/CDC node appears in ledger-db's dependency graph or 30-day change history, so the retention can recur. [F24, F48, F19]
- **[long_term]** Add disk-capacity and WAL-retention alerts on the shared ledger-db with capacity headroom, and review shared-datastore write paths for per-relation allocation failure modes (writers hit 'could not extend file base/16402/2619' while other writers kept working). -- The disk trend was visible ~2.75 hours before the outage (89% at 13:00Z) with no alert; per-relation blocking explains why only some of the four writers failed. [F17, F34, F31, F50, F54]

## Data gaps
- No tool exposes pg_replication_slots (or raw DDL/migration files), so the analytics_cdc slot's creation time, age, last-consumed position and owner could not be verified — the slot's liveness/ownership is inferred from log lines plus the HIST-114 precedent only.
- ledger-db has no WAL-size, xlog-bytes, write/transaction-throughput or IOPS metric; WAL growth could only be estimated from 3 sparse 'retaining <n> MB of WAL' WARN lines (14:34:05Z, 14:46:32Z, 14:49:47Z) and disk_used_pct with 3–4 min buckets, so a small (<10%) mandate-driven write increase hidden inside a bucket cannot be fully excluded.
- settlement-service's ~55% error spike has no matching log signature: its logs in 14:50–16:59Z contain zero ERROR/WARN (only benign INFO, confirmed continuous to 16:55:36Z, missing_points 0), and the targeted 'space'/'extend' log queries returned 0 — so its spike could not be verbatim-confirmed as the same storage signature.
- settlement-service's resource metrics (db_pool_in_use, consumer_lag, latency_p99, cpu_pct) were not retrieved (tool budget exhausted), so its profile could not be compared against payment-service's flat profile.
- No catalog/ownership mapping for relation base/16402/2619 was retrievable, so which writer's table is blocked remains undetermined from ledger-db logs.
- No per-writer metric (e.g. connections/write counters per client) exists on ledger-db, so it could not be numerically shown that portfolio-service kept writing while others failed — only the absence of any portfolio-related log line and the absence of per-client metrics.
- Config-change tool returns 0 for all services across 30 days and no schema-migration artifact type exists, so a ledger-db schema/enum migration accompanying MANDATE_HOLD is neither confirmed nor refuted by an artifact (it is only ruled out as a captured deploy/config change).
- No post-rollback remediation signal (slot drop/restart/cleanup) could be checked; only disk_used_pct=100% and continuing extend errors to 16:56:40Z were observed, indicating no effective remediation within the window.
- payment-service deploy/config history beyond the single dep-6203 record could not be enumerated (budget exhausted), and no per-instance breakdown of its error rate was obtained.

## What would change this conclusion
- Finding that the analytics_cdc replication slot is active/healthy (or was newly created within 2026-05-25..2026-06-01), which would move causation toward a change/incomplete-CDC-consumer setup rather than long-term inaction — and would shift blame toward the CDC pipeline owner.
- A measured material increase in ledger-db WAL/write/transaction throughput (or mandate-flow write volume) immediately after 14:46Z that is not explained by the slot — i.e. a real inflection in the growth rate — which would revive H3 (v6.0.0 write amplification as the trigger).
- Evidence that payment-service-specific writes (MANDATE_HOLD, own temp filesystem, or downstream bank-partner-api/kafka path) were the only blocked writes, contradicting the order-service and settlement-service same-onset failures — which would revive H6.
- Any schema/enum rejection or validation error for MANDATE_HOLD in ledger-db logs or DDL, which would revive H1 despite 100% storage-signature errors elsewhere.
- Recovery of payment-service (and peers) at 15:12Z after the rollback once disk data is inspected at finer granularity, or restart of ledger-db alone clearing all three writers, which would change the mitigation ordering.
- Verbatim confirmation that settlement-service's ~55% spike carries a different (non-storage, e.g. kafka-consumer) signature, which would require splitting the incident into a second, distinct failure mode.

## Investigation stats
- latency 199.91s, LLM calls 41, tokens 335272 (in 286344 / out 48928)
- tool calls 72 (17 failed, 0 partial, 0 recovered by retry), findings 54/54 verified
