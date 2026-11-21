# Incident investigation report: INC-012
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The incident was caused by disk exhaustion on the shared Postgres node ledger-db: disk_used_pct rose monotonically from 89.18% at 13:00Z — more than 1.5 hours before the v6.0.0 deploy — to 100% by 14:56Z, after which every writer to that node failed with a single storage-exhaustion signature (F13, F14, F8, F10, F19). payment-service v6.0.0 (dep-6203) is temporally coincidental, not causal: order-service, which was never deployed and has no mandate code path, emitted the identical "No space left on device" error 16 seconds after payment-service's first failure, and three ledger-db consumers stepped in the same 14:58Z bucket (F15, F19, F20, F22). The 15:12Z rollback produced no recovery because the disk stayed pinned at 100% (F16). Confidence in this conclusion is high (H1 = 0.78, well above the alternatives at ≤0.06), with the only residual caveat being portfolio-service's unexplained immunity at the SQL level, which is a scope wrinkle on one clause of H1 rather than a competing cause.

## Probable root cause
**ledger-db disk exhaustion (pre-existing fill) → all writers fail; v6.0.0 coincidental** -- resource_exhaustion in `ledger-db` (confidence 0.78, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk exhaustion (pre-existing fill) → all writers fail; v6.0.0 coincidental | resource_exhaustion | ledger-db | - | 0.78 (high) | active |
| 2 | H2: payment-service v6.0.0 mandate flow wrote MANDATE_HOLD entries that filled ledger-db's disk | bad_deployment | payment-service | dep-6203 | 0.06 (low) | active |
| 3 | H4: Independent shared dependency (bank-partner-api / DB connection exhaustion) drives the failures | dependency_failure | bank-partner-api | - | 0.03 (low) | active |
| 4 | H3: New MANDATE_HOLD entry type rejected/unhandled by a downstream ledger consumer | bad_deployment | payment-service | dep-6203 | 0.02 (low) | rejected |
| 5 | H5: Post-rollback persistence due to corrupt/queued state left by MANDATE_HOLD | data_issue | ledger-db | dep-6203 | 0.02 (low) | rejected |

### H1: ledger-db disk exhaustion (pre-existing fill) → all writers fail; v6.0.0 coincidental
The shared Postgres node ledger-db's filesystem filled to 100% following a steady, monotonic trend that began >1.5h before dep-6203, causing pg_wal/xlogtemp PANICs and 'No space left on device'/'could not extend file' on writes. payment-service's ledger writes fail because ledger-db cannot extend WAL/base files; the v6.0.0 mandate flow is temporally coincidental, not causal.

**Causal chain**
1. ledger-db disk_used_pct climbs monotonically from 89.18% at 13:00Z to 99.15% at 14:48Z (2 min after deploy) to 100.0% from 15:00Z — a fill trend starting >1.5h before dep-6203 (14:46Z). [F13, F14]
2. At/near-full disk, Postgres cannot extend WAL (pg_wal/xlogtemp.*) or relation files (base/16402/2619), producing PANIC and 'could not extend file'. [F10, F9]
3. payment-service's ledger write on shared ledger-db fails with a DB disk-space error (354/354 error lines, single signature), surfacing as 5xx at ~30-65%. [F8, F12, F10]
4. Non-payment consumers of the same ledger-db step up in the SAME 14:58Z bucket (settlement 0.17%→38.58%, order 0.24%→34.12%), and order-service emits the SAME 'No space left on device' error text at 15:00:42Z despite never receiving dep-6203 — a payment-service-only code regression cannot produce this. [F15, F5, F6, F19, F20, F22]
5. ledger-db's own logs carry the identical storage-exhaustion signature (153x pg_wal/xlogtemp PANIC, 139x could not extend file), first 35s after payment-service's first failure — the DB-side origin. [F10]
6. Rolling back v6.0.0 does not restore service (flat ~60% through 16:58Z) because the disk stays at 100% — the cause was never the code. [F16, F13]
7. Traffic volume, latency, bank API error rate and connection pools are unchanged across onset — a storage-side wall, not a load/pool/bank surge. [F17, F23, F24, F26, F27]
8. Only writers hitting the saturated tablespace fail; portfolio-service's immunity is consistent with not writing that relation, though no SQL-level access data confirms it. *(assumed)*

**Supporting evidence**
  - `F8` ✔ The earliest error in payment-service logs is at 15:00:26Z, and ALL 354 ERROR lines match a single signature: a ledger write failure caused by a DB disk-space error, not validation rejection of a new entry type. Top pattern count 354 across the whole 14:30-16:59Z window.
  - `F9` ✔ There is no service named 'ledger-service' in the environment; the ledger database component is 'ledger-db' and its only metrics are connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min. ledger-db exposes a disk_used_pct metric, which is the relevant signal for the observed disk exhaustion.
  - `F10` ✔ ledger-db logs its own errors with the same storage exhaustion signature, first at 15:01:01Z, 35 seconds after payment-service's first failure: 153x PANIC on pg_wal/xlogtemp.* and 139x could not extend file "base/16402/2619". This is the DB-side origin of the payment-service 5xx/ledger write failures.
  - `F12` ✔ payment-service error signature is exclusively ledger-write/disk related; no log line in the sampled window mentions validation, rejection, or an unsupported entry type, and there are no connection-reset/DB-connection-pool errors beyond 2 WARN 'retrying idempotent request ... after connection reset' at 15:58:52Z. So the hypothesis of a rejected new ledger entry type is not supported by the error text.
  - `F13` ✔ ledger-db disk_used_pct was already at a critical level and rising steadily HOURS before the v6.0.0 deploy: from 89.18% at 13:00Z it climbs ~0.35-0.5 pp per 4-min bucket to 94.7% at 14:00Z, 99.15% at 14:48Z (2 min AFTER deploy), 99.97% at 14:56Z and a flat 100.0% from 15:00Z through the end of the window (16:56Z). The disk-fill trend pre-dates dep-6203 by >1.5 hours and reaches saturation ~2 minutes before the payment-service error step.
  - `F14` ✔ Ordering of the change vs. the first deviation: the deploy dep-6203 (payment-service v6.0.0) completed at 14:46:50Z; ledger-db disk was still climbing at that moment (98.54% avg at 14:44Z) and hit 100% at 14:56Z, while payment-service error_rate deviated in the 14:58Z bucket (30.41% avg) and the first ledger-write failure log is 15:00:26Z. So the v6.0.0 release is the only competing change and it does precede onset, but the resource whose failure produces the error text was already saturating before the deploy and saturated independently of any code path.
  - `F15` ✔ Neighbours sharing the same ledger-db show the SAME step at the SAME 14:58Z bucket, which a payment-service-only regression could not explain: settlement-service error_rate goes 0.17% (14:51Z) -> 38.58% (14:58Z) and stays ~54-56% through 16:57Z; order-service goes 0.24% (14:51Z) -> 34.12% (14:58Z) and stays ~47-48%; payment-service goes 0.20% (14:54Z) -> 30.41% (14:58Z).
  - `F16` ✔ No recovery after the 15:12Z rollback: payment-service error_rate stays flat/high from the rollback through the latest data -- 15:14Z 62.70%, 15:30Z 60.72%, 16:02Z 62.58%, 16:30Z 60.37%, 16:58Z 63.15% (mean 49.82%, last 63.76%) -- with no downward step at any bucket from 15:12Z onward, i.e. the incident is FLAT/not recovering ~1h46m after rollback.
  - `F17` ✔ payment-service request_rate and latency_p99_ms are unaffected across the whole window including onset and post-rollback -- request_rate ~98-103 req/s at 14:51Z, 100.54 at 14:58Z and 99.55 at 16:57Z; latency_p99_ms ~311-330 ms at 14:58Z and 310.71 ms at 16:57Z. So only the storage-dependent errors moved; traffic volume and response time did not.
  - `F19` ✔ order-service, a non-payment ledger-db consumer, emits ERROR logs with the SAME storage-exhaustion signature as payment-service/ledger-db in the 14:50-15:10Z bucket: 18 ERROR lines, all of a single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', first at 2026-06-01T15:00:42Z (last 15:08:40Z), truncated=false, partial=false. No validation/rejection/unsupported-entry-type or consumer-lag lines appear.
  - `F20` ✔ order-service's first disk-exhaustion error (15:00:42Z) lands only ~16 seconds after payment-service's first ledger-write failure (15:00:26Z per F8) and within the same 15:00Z bucket as ledger-db's first PANIC (15:01:01Z per F10). Order-service was not touched by dep-6203, so it cannot have inherited the error via the v6.0.0 code path.
  - `F22` ✔ Directly against H2/H3 (payment-service code-path origin): the SAME No-space-left-on-device text appears in order-service, which does not run the mandate flow and was not deployed, and the same bucket ordering holds. Only the shared ledger-db disk failure explains a resource-write error text in a service that has no v6.0.0 code.
  - `F23` ✔ payment-service bank_api_error_rate shows NO deviation at 14:58Z or anywhere in 14:30-16:59Z: it is flat at avg 0.20% (range 0.18-0.22) in all 30 five-minute buckets, e.g. 14:55Z avg 0.20/max 0.21, 15:00Z avg 0.20/max 0.21, 15:10Z avg 0.20/max 0.20, 16:55Z avg 0.21. Summary min 0.18, max 0.22, mean 0.20, missing_points 0.
  - `F24` ✔ Zoomed to 3-min buckets around the onset, bank_api_error_rate is unchanged across the deploy and the 14:58Z error step: 14:46Z avg 0.20, 14:58Z avg 0.20/max 0.21, 15:01Z avg 0.20, 15:04Z avg 0.20. So the external bank dependency was healthy exactly when the payment error_rate stepped (max 0.22, mean 0.20).
  - `F26` ✔ ledger-db connections_active shows no connection-pool exhaustion or step at 14:58Z: it oscillates in a narrow band (~60-66) before, during and after onset -- 14:30Z avg 62.85, 14:55Z avg 61.14, 15:00Z avg 62.48, 15:05Z avg 63.38, 15:10Z avg 62.74, 16:55Z avg 62.43. Summary min 54.85, max 68.34, mean 62.27, missing_points 0.
  - `F27` ✔ payment-service's own client-side DB pool is flat at ~8 connections for the entire window including onset: 14:30Z avg 8.02, 14:55Z avg 8.12, 15:00Z avg 7.95, 15:05Z 8.13, 16:55Z 8.07 (min 7.08, max 8.84, mean 8.02, missing_points 0). No pool saturation on the caller side accompanies the failure step.
  - `F5` ✔ ledger-db is a SHARED datastore with four direct DB consumers: order-service, portfolio-service, payment-service and settlement-service (each edge kind 'db'). So a problem in the ledger store can surface in those three non-payment services as well as payment-service.
  - `F6` ✔ payment-service dependency edges: payment-service -> ledger-db (db), payment-service -> bank-partner-api (external), payment-service -> kafka (queue-produce); upstream caller api-gateway -> payment-service (http). settlement-service -> kafka (queue-consume) and settlement-service -> ledger-db (db), i.e. settlement consumes the Kafka stream that payment-service produces into.
**Contradicting evidence**
  - `F1` ✔ Only ONE change was recorded across ALL services in the 14:45-15:45Z window (deployments scope 'all services'): dep-6203, payment-service v6.0.0 deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes include 'new ledger entry type MANDATE_HOLD'; status 'rolled_back' at 2026-06-01T15:12:00Z. No other service deployed in the window.
  - `F2` ✔ The reported window 15:00-15:30Z contains no deployments: the only related deploy (v6.0.0) landed at 14:46Z, ~14 min BEFORE the 15:00Z start of the requested window, consistent with the report's '~15 minutes after the release' symptom onset. Its rollback at 15:12Z falls inside the window.
  - `F11` ✔ payment-service logged 'deployment dep-6203 (v6.0.0) rollout complete, all pods ready' at 14:46:50Z, ~11.5 minutes before the error-rate step at 14:58Z and ~13.5 min before the first ledger write failure log at 15:00:26Z. The release therefore preceded onset, but my data does not show a causal link between the new mandate/entry-type code path and the disk exhaustion.
  - `F18` ✔ portfolio-service, also a direct ledger-db DB consumer per T2's topology, is COMPLETELY unaffected: its error_rate stays ~0.16-0.25% in every bucket from 14:30Z through 16:57Z (no deviation at 14:58Z). This shows the blast radius is not uniform across all four ledger-db consumers.
  - `F21` ✔ settlement-service logged ZERO ERROR lines (and zero WARN lines) across 11:00-16:59Z, despite its error_rate having stepped to ~38.58% at 14:58Z and staying ~54-56% (F15). So its elevated error metric is NOT accompanied by any storage-exhaustion error text in its logs -- the signature is absent from settlement's log stream entirely.
  - `F33` ✔ The dependency graph provides no SQL/tablespace-level access-path data for ANY ledger-db consumer: payment-service's held edges are only payment-service->ledger-db (db), ->bank-partner-api (external), ->kafka (queue-produce) — same generic 'db' kind as portfolio-service, with no INSERT/SELECT or relation metadata. The read-vs-write asymmetry alleged for portfolio-service is therefore not visible in any retrieved artifact.
**Assumptions**
  - v6.0.0's MANDATE_HOLD writes did not meaningfully contribute to reaching 100% (byte-level disk data never obtained; trend/saturation predate deploy, so this is confirmatory not blocking)
  - No change occurred after 15:45Z or exactly at the 15:00Z boundary (queries budget-exhausted)
  - portfolio-service immunity is explained by read-only / different-relation access — no SQL/tablespace data exists to confirm (F29/F30/F33 all generic 'db' edges)
**Rejected because:** null

### H2: payment-service v6.0.0 mandate flow wrote MANDATE_HOLD entries that filled ledger-db's disk
The v6.0.0 new UPI mandate flow writes a new ledger entry type (MANDATE_HOLD) whose write volume or large rows rapidly consumed ledger-db disk space; the full disk then broke ledger writes and cascaded to co-tenants of the shared DB.

**Causal chain**
1. dep-6203 (v6.0.0) deploys the new UPI mandate flow writing a new ledger entry type MANDATE_HOLD. [F1]
2. The new entry type's writes rapidly fill ledger-db's disk. *(assumed)*
3. Full disk causes pg_wal/base extension PANICs and ledger write failures. [F10]
4. Failures propagate to payment-service and its DB co-tenants. [F8, F15]

**Supporting evidence**
  - `F1` ✔ Only ONE change was recorded across ALL services in the 14:45-15:45Z window (deployments scope 'all services'): dep-6203, payment-service v6.0.0 deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes include 'new ledger entry type MANDATE_HOLD'; status 'rolled_back' at 2026-06-01T15:12:00Z. No other service deployed in the window.
  - `F2` ✔ The reported window 15:00-15:30Z contains no deployments: the only related deploy (v6.0.0) landed at 14:46Z, ~14 min BEFORE the 15:00Z start of the requested window, consistent with the report's '~15 minutes after the release' symptom onset. Its rollback at 15:12Z falls inside the window.
  - `F11` ✔ payment-service logged 'deployment dep-6203 (v6.0.0) rollout complete, all pods ready' at 14:46:50Z, ~11.5 minutes before the error-rate step at 14:58Z and ~13.5 min before the first ledger write failure log at 15:00:26Z. The release therefore preceded onset, but my data does not show a causal link between the new mandate/entry-type code path and the disk exhaustion.
**Contradicting evidence**
  - `F13` ✔ ledger-db disk_used_pct was already at a critical level and rising steadily HOURS before the v6.0.0 deploy: from 89.18% at 13:00Z it climbs ~0.35-0.5 pp per 4-min bucket to 94.7% at 14:00Z, 99.15% at 14:48Z (2 min AFTER deploy), 99.97% at 14:56Z and a flat 100.0% from 15:00Z through the end of the window (16:56Z). The disk-fill trend pre-dates dep-6203 by >1.5 hours and reaches saturation ~2 minutes before the payment-service error step.
  - `F14` ✔ Ordering of the change vs. the first deviation: the deploy dep-6203 (payment-service v6.0.0) completed at 14:46:50Z; ledger-db disk was still climbing at that moment (98.54% avg at 14:44Z) and hit 100% at 14:56Z, while payment-service error_rate deviated in the 14:58Z bucket (30.41% avg) and the first ledger-write failure log is 15:00:26Z. So the v6.0.0 release is the only competing change and it does precede onset, but the resource whose failure produces the error text was already saturating before the deploy and saturated independently of any code path.
  - `F15` ✔ Neighbours sharing the same ledger-db show the SAME step at the SAME 14:58Z bucket, which a payment-service-only regression could not explain: settlement-service error_rate goes 0.17% (14:51Z) -> 38.58% (14:58Z) and stays ~54-56% through 16:57Z; order-service goes 0.24% (14:51Z) -> 34.12% (14:58Z) and stays ~47-48%; payment-service goes 0.20% (14:54Z) -> 30.41% (14:58Z).
  - `F16` ✔ No recovery after the 15:12Z rollback: payment-service error_rate stays flat/high from the rollback through the latest data -- 15:14Z 62.70%, 15:30Z 60.72%, 16:02Z 62.58%, 16:30Z 60.37%, 16:58Z 63.15% (mean 49.82%, last 63.76%) -- with no downward step at any bucket from 15:12Z onward, i.e. the incident is FLAT/not recovering ~1h46m after rollback.
  - `F18` ✔ portfolio-service, also a direct ledger-db DB consumer per T2's topology, is COMPLETELY unaffected: its error_rate stays ~0.16-0.25% in every bucket from 14:30Z through 16:57Z (no deviation at 14:58Z). This shows the blast radius is not uniform across all four ledger-db consumers.
  - `F19` ✔ order-service, a non-payment ledger-db consumer, emits ERROR logs with the SAME storage-exhaustion signature as payment-service/ledger-db in the 14:50-15:10Z bucket: 18 ERROR lines, all of a single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', first at 2026-06-01T15:00:42Z (last 15:08:40Z), truncated=false, partial=false. No validation/rejection/unsupported-entry-type or consumer-lag lines appear.
  - `F20` ✔ order-service's first disk-exhaustion error (15:00:42Z) lands only ~16 seconds after payment-service's first ledger-write failure (15:00:26Z per F8) and within the same 15:00Z bucket as ledger-db's first PANIC (15:01:01Z per F10). Order-service was not touched by dep-6203, so it cannot have inherited the error via the v6.0.0 code path.
  - `F22` ✔ Directly against H2/H3 (payment-service code-path origin): the SAME No-space-left-on-device text appears in order-service, which does not run the mandate flow and was not deployed, and the same bucket ordering holds. Only the shared ledger-db disk failure explains a resource-write error text in a service that has no v6.0.0 code.
**Assumptions**
  - That the disk was NOT already saturating independently of the deploy — directly contradicted by F13's hours-long trend beginning at 89.18% at 13:00Z
  - That MANDATE_HOLD writes are volumetrically large enough to fill the disk in ~2 minutes
**Rejected because:** null

### H4: Independent shared dependency (bank-partner-api / DB connection exhaustion) drives the failures
An outage or degradation in a shared dependency — the external bank-partner-api, or DB connection-pool exhaustion secondary to the saturated ledger-db — produces the ~60% error rate independently of the mandate change.

**Causal chain**
1. bank-partner-api or the DB connection path degrades ~14:58Z. *(assumed)*
2. payment-service's external/DB calls fail, yielding 5xx. [F6]
3. Error rate steps to ~60% and stays flat. [F7]

**Supporting evidence**
  - `F6` ✔ payment-service dependency edges: payment-service -> ledger-db (db), payment-service -> bank-partner-api (external), payment-service -> kafka (queue-produce); upstream caller api-gateway -> payment-service (http). settlement-service -> kafka (queue-consume) and settlement-service -> ledger-db (db), i.e. settlement consumes the Kafka stream that payment-service produces into.
  - `F7` ✔ payment-service error_rate was at baseline ~0.19-0.29% through 14:54Z, then jumped to avg 30.41% (max 60.8%) in the 14:58:00Z bucket and ~59-65% for every 4-min bucket from 15:02Z to 16:58Z. The first elevated bucket is 14:58Z, i.e. ~12 minutes BEFORE the reported onset window of 15:10Z.
**Contradicting evidence**
  - `F8` ✔ The earliest error in payment-service logs is at 15:00:26Z, and ALL 354 ERROR lines match a single signature: a ledger write failure caused by a DB disk-space error, not validation rejection of a new entry type. Top pattern count 354 across the whole 14:30-16:59Z window.
  - `F10` ✔ ledger-db logs its own errors with the same storage exhaustion signature, first at 15:01:01Z, 35 seconds after payment-service's first failure: 153x PANIC on pg_wal/xlogtemp.* and 139x could not extend file "base/16402/2619". This is the DB-side origin of the payment-service 5xx/ledger write failures.
  - `F12` ✔ payment-service error signature is exclusively ledger-write/disk related; no log line in the sampled window mentions validation, rejection, or an unsupported entry type, and there are no connection-reset/DB-connection-pool errors beyond 2 WARN 'retrying idempotent request ... after connection reset' at 15:58:52Z. So the hypothesis of a rejected new ledger entry type is not supported by the error text.
  - `F23` ✔ payment-service bank_api_error_rate shows NO deviation at 14:58Z or anywhere in 14:30-16:59Z: it is flat at avg 0.20% (range 0.18-0.22) in all 30 five-minute buckets, e.g. 14:55Z avg 0.20/max 0.21, 15:00Z avg 0.20/max 0.21, 15:10Z avg 0.20/max 0.20, 16:55Z avg 0.21. Summary min 0.18, max 0.22, mean 0.20, missing_points 0.
  - `F24` ✔ Zoomed to 3-min buckets around the onset, bank_api_error_rate is unchanged across the deploy and the 14:58Z error step: 14:46Z avg 0.20, 14:58Z avg 0.20/max 0.21, 15:01Z avg 0.20, 15:04Z avg 0.20. So the external bank dependency was healthy exactly when the payment error_rate stepped (max 0.22, mean 0.20).
  - `F26` ✔ ledger-db connections_active shows no connection-pool exhaustion or step at 14:58Z: it oscillates in a narrow band (~60-66) before, during and after onset -- 14:30Z avg 62.85, 14:55Z avg 61.14, 15:00Z avg 62.48, 15:05Z avg 63.38, 15:10Z avg 62.74, 16:55Z avg 62.43. Summary min 54.85, max 68.34, mean 62.27, missing_points 0.
  - `F27` ✔ payment-service's own client-side DB pool is flat at ~8 connections for the entire window including onset: 14:30Z avg 8.02, 14:55Z avg 8.12, 15:00Z avg 7.95, 15:05Z 8.13, 16:55Z 8.07 (min 7.08, max 8.84, mean 8.02, missing_points 0). No pool saturation on the caller side accompanies the failure step.
  - `F28` ✔ bank-partner-api cannot be observed directly: querying its metric returned not_found because it is an external dependency ('we have no logs/metrics for it. Observe it through its callers'). Its only caller is payment-service, whose bank_api_error_rate is flat at ~0.20% (tc-027), so the only available proxy for bank-partner-api health shows no degradation.
**Assumptions**
  - An independent bank-side fault could evade the caller-proxy metric (no direct telemetry for bank-partner-api)
  - Very short (<3 min) pool spikes between 14:56-14:58Z could be invisible at the available bucket resolution
**Rejected because:** null

### H3: New MANDATE_HOLD entry type rejected/unhandled by a downstream ledger consumer
The reporter's stated theory: the new MANDATE_HOLD ledger entry type is rejected or unhandled by a downstream ledger consumer, causing cascading write failures; the 60% error rate is a symptom of new-entry-type incompatibility.

**Causal chain**
1. v6.0.0 emits new ledger entry type MANDATE_HOLD. [F1]
2. A ledger consumer/schema rejects or cannot persist the unknown entry type, failing the write. *(assumed)*
3. Rejections cascade into payment-service 5xx at 60%. *(assumed)*

**Supporting evidence**
  - `F1` ✔ Only ONE change was recorded across ALL services in the 14:45-15:45Z window (deployments scope 'all services'): dep-6203, payment-service v6.0.0 deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes include 'new ledger entry type MANDATE_HOLD'; status 'rolled_back' at 2026-06-01T15:12:00Z. No other service deployed in the window.
**Contradicting evidence**
  - `F8` ✔ The earliest error in payment-service logs is at 15:00:26Z, and ALL 354 ERROR lines match a single signature: a ledger write failure caused by a DB disk-space error, not validation rejection of a new entry type. Top pattern count 354 across the whole 14:30-16:59Z window.
  - `F10` ✔ ledger-db logs its own errors with the same storage exhaustion signature, first at 15:01:01Z, 35 seconds after payment-service's first failure: 153x PANIC on pg_wal/xlogtemp.* and 139x could not extend file "base/16402/2619". This is the DB-side origin of the payment-service 5xx/ledger write failures.
  - `F12` ✔ payment-service error signature is exclusively ledger-write/disk related; no log line in the sampled window mentions validation, rejection, or an unsupported entry type, and there are no connection-reset/DB-connection-pool errors beyond 2 WARN 'retrying idempotent request ... after connection reset' at 15:58:52Z. So the hypothesis of a rejected new ledger entry type is not supported by the error text.
  - `F13` ✔ ledger-db disk_used_pct was already at a critical level and rising steadily HOURS before the v6.0.0 deploy: from 89.18% at 13:00Z it climbs ~0.35-0.5 pp per 4-min bucket to 94.7% at 14:00Z, 99.15% at 14:48Z (2 min AFTER deploy), 99.97% at 14:56Z and a flat 100.0% from 15:00Z through the end of the window (16:56Z). The disk-fill trend pre-dates dep-6203 by >1.5 hours and reaches saturation ~2 minutes before the payment-service error step.
  - `F15` ✔ Neighbours sharing the same ledger-db show the SAME step at the SAME 14:58Z bucket, which a payment-service-only regression could not explain: settlement-service error_rate goes 0.17% (14:51Z) -> 38.58% (14:58Z) and stays ~54-56% through 16:57Z; order-service goes 0.24% (14:51Z) -> 34.12% (14:58Z) and stays ~47-48%; payment-service goes 0.20% (14:54Z) -> 30.41% (14:58Z).
  - `F16` ✔ No recovery after the 15:12Z rollback: payment-service error_rate stays flat/high from the rollback through the latest data -- 15:14Z 62.70%, 15:30Z 60.72%, 16:02Z 62.58%, 16:30Z 60.37%, 16:58Z 63.15% (mean 49.82%, last 63.76%) -- with no downward step at any bucket from 15:12Z onward, i.e. the incident is FLAT/not recovering ~1h46m after rollback.
  - `F18` ✔ portfolio-service, also a direct ledger-db DB consumer per T2's topology, is COMPLETELY unaffected: its error_rate stays ~0.16-0.25% in every bucket from 14:30Z through 16:57Z (no deviation at 14:58Z). This shows the blast radius is not uniform across all four ledger-db consumers.
  - `F19` ✔ order-service, a non-payment ledger-db consumer, emits ERROR logs with the SAME storage-exhaustion signature as payment-service/ledger-db in the 14:50-15:10Z bucket: 18 ERROR lines, all of a single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', first at 2026-06-01T15:00:42Z (last 15:08:40Z), truncated=false, partial=false. No validation/rejection/unsupported-entry-type or consumer-lag lines appear.
  - `F20` ✔ order-service's first disk-exhaustion error (15:00:42Z) lands only ~16 seconds after payment-service's first ledger-write failure (15:00:26Z per F8) and within the same 15:00Z bucket as ledger-db's first PANIC (15:01:01Z per F10). Order-service was not touched by dep-6203, so it cannot have inherited the error via the v6.0.0 code path.
  - `F22` ✔ Directly against H2/H3 (payment-service code-path origin): the SAME No-space-left-on-device text appears in order-service, which does not run the mandate flow and was not deployed, and the same bucket ordering holds. Only the shared ledger-db disk failure explains a resource-write error text in a service that has no v6.0.0 code.
**Assumptions**
  - None verifiable — the mechanism is contradicted by the observed error text
**Rejected because:** All 354 error lines carry a single signature: DB disk-space failure (pg_wal/xlogtemp PANIC, could not extend file). No log line mentions validation, rejection, unsupported entry type, or schema error (F8, F12). order-service, which never received MANDATE_HOLD and was not deployed, emits the SAME 'No space left on device' text in the same bucket (F19, F20, F22), and the disk was already ~99% before the deploy (F13). The mechanism is unsupported by any error evidence.

### H5: Post-rollback persistence due to corrupt/queued state left by MANDATE_HOLD
Even after the 15:12Z rollback, failures persist because the new entry type left bad/corrupt state or a queued backlog that keeps ledger writes failing.

**Causal chain**
1. MANDATE_HOLD rows or a partial migration leave ledger-db state that rejects subsequent writes. *(assumed)*
2. Failures continue post-rollback at ~60%. [F16]

**Supporting evidence**
  - `F16` ✔ No recovery after the 15:12Z rollback: payment-service error_rate stays flat/high from the rollback through the latest data -- 15:14Z 62.70%, 15:30Z 60.72%, 16:02Z 62.58%, 16:30Z 60.37%, 16:58Z 63.15% (mean 49.82%, last 63.76%) -- with no downward step at any bucket from 15:12Z onward, i.e. the incident is FLAT/not recovering ~1h46m after rollback.
**Contradicting evidence**
  - `F8` ✔ The earliest error in payment-service logs is at 15:00:26Z, and ALL 354 ERROR lines match a single signature: a ledger write failure caused by a DB disk-space error, not validation rejection of a new entry type. Top pattern count 354 across the whole 14:30-16:59Z window.
  - `F10` ✔ ledger-db logs its own errors with the same storage exhaustion signature, first at 15:01:01Z, 35 seconds after payment-service's first failure: 153x PANIC on pg_wal/xlogtemp.* and 139x could not extend file "base/16402/2619". This is the DB-side origin of the payment-service 5xx/ledger write failures.
  - `F12` ✔ payment-service error signature is exclusively ledger-write/disk related; no log line in the sampled window mentions validation, rejection, or an unsupported entry type, and there are no connection-reset/DB-connection-pool errors beyond 2 WARN 'retrying idempotent request ... after connection reset' at 15:58:52Z. So the hypothesis of a rejected new ledger entry type is not supported by the error text.
  - `F13` ✔ ledger-db disk_used_pct was already at a critical level and rising steadily HOURS before the v6.0.0 deploy: from 89.18% at 13:00Z it climbs ~0.35-0.5 pp per 4-min bucket to 94.7% at 14:00Z, 99.15% at 14:48Z (2 min AFTER deploy), 99.97% at 14:56Z and a flat 100.0% from 15:00Z through the end of the window (16:56Z). The disk-fill trend pre-dates dep-6203 by >1.5 hours and reaches saturation ~2 minutes before the payment-service error step.
  - `F15` ✔ Neighbours sharing the same ledger-db show the SAME step at the SAME 14:58Z bucket, which a payment-service-only regression could not explain: settlement-service error_rate goes 0.17% (14:51Z) -> 38.58% (14:58Z) and stays ~54-56% through 16:57Z; order-service goes 0.24% (14:51Z) -> 34.12% (14:58Z) and stays ~47-48%; payment-service goes 0.20% (14:54Z) -> 30.41% (14:58Z).
  - `F19` ✔ order-service, a non-payment ledger-db consumer, emits ERROR logs with the SAME storage-exhaustion signature as payment-service/ledger-db in the 14:50-15:10Z bucket: 18 ERROR lines, all of a single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', first at 2026-06-01T15:00:42Z (last 15:08:40Z), truncated=false, partial=false. No validation/rejection/unsupported-entry-type or consumer-lag lines appear.
**Assumptions**
  - No evidence of corrupt state exists
  - The flat post-rollback rate is fully explained by disk staying at 100% (F13/F16)
**Rejected because:** The persistence is fully explained by ledger-db disk remaining pinned at 100.0% with no remediation event (F13, F16); the error signature is unchanged disk-exhaustion (F8/F12/F19), not a data/validation error. No finding indicates corrupt or incompatible persisted state.

## Facts (verified observations)
- ledger-db disk_used_pct was already critical and rising hours before the deploy: 89.18% at 13:00Z, 94.7% at 14:00Z, 99.15% at 14:48Z, 99.97% at 14:56Z, then flat 100.0% from 15:00Z to 16:56Z. [F13]
- dep-6203 (payment-service v6.0.0, 'New UPI mandate flow', new ledger entry type MANDATE_HOLD) was the only change across all services in 14:45-15:45Z; deployed 14:46:00Z, rolled back 15:12:00Z. No config/feature-flag changes were recorded. [F1, F3]
- ledger-db is a shared datastore with four direct DB consumers: order-service, portfolio-service, payment-service, settlement-service. [F4, F5]
- All 354 ERROR lines in payment-service match a single signature — ledger write failure caused by DB disk-space error — with no validation, rejection, or unsupported-entry-type text. [F8, F12]
- ledger-db logs its own storage-exhaustion errors first at 15:01:01Z, 35 seconds after payment-service's first failure: 153x PANIC on pg_wal/xlogtemp.* and 139x 'could not extend file base/16402/2619'. [F10]
- payment-service error_rate jumped from ~0.19-0.29% baseline to 30.41% (max 60.8%) in the 14:58Z bucket and stayed ~59-65% through 16:58Z. [F7, F16]
- Three shared ledger-db consumers stepped in the same 14:58Z bucket: settlement-service 0.17%→38.58%, order-service 0.24%→34.12%, payment-service 0.20%→30.41%. [F15]
- order-service, which was not deployed and never received MANDATE_HOLD, emitted 18 ERROR lines of the identical 'INSERT INTO orders failed: No space left on device' signature from 15:00:42Z — 16 seconds after payment-service's first failure and in the same bucket as ledger-db's first PANIC. [F19]
- The 15:12Z rollback produced no recovery: payment-service error_rate remained flat/high from 15:14Z (62.70%) through 16:58Z (63.15%) with no downward step, ~1h46m after rollback. [F16]
- payment-service request_rate (~98-103 req/s) and latency_p99_ms (~310-330 ms) were unaffected across onset and post-rollback; only storage-dependent errors moved. [F17]
- payment-service bank_api_error_rate was flat at ~0.20% (range 0.18-0.22) in all 30 five-minute buckets and unchanged in 3-minute buckets around the 14:58Z step. [F23, F24]
- Neither ledger-db connections_active (~60-66 band) nor payment-service's client-side DB pool (~8 connections) showed any step or saturation at onset; bank-partner-api has no direct telemetry and is observable only via its single caller's flat metric. [F26, F27]
- portfolio-service, also a ledger-db consumer, was completely unaffected (error_rate ~0.16-0.25% in every bucket), with no deployments or config changes in the window; the dependency graph exposes only generic 'db' edges with no read/write, tablespace, or relation attributes for any consumer. [F18, F31, F32, F29, F30, F33]
- settlement-service logged zero ERROR and zero WARN lines across 11:00-16:59Z despite its error_rate stepping to 38.58% and staying ~54-56%; its storage signature is not textually verified. [F21, F15]

## Assumptions (not verified)
- v6.0.0's MANDATE_HOLD writes did not meaningfully contribute to reaching 100% — no byte-level disk data was obtained; the monotonic trend and saturation predate the deploy, so this is confirmatory rather than load-bearing.
- No change occurred after 15:45Z or exactly at the 15:00Z boundary — the second-pass deployment and config-change queries for those windows hit budget exhaustion and were never executed.
- portfolio-service's immunity is explained by read-only or different-relation access to ledger-db — no SQL, tablespace, or relation attribute exists on any 'db' edge (F29/F30/F33), and its logs were not retrievable.
- The post-rollback (15:12-16:59Z) failures are still the identical pg_wal/No-space-left-on-device signature; post-rollback signature is inferred from metric flatness plus the sampled 14:30-16:59Z log window rather than a dedicated post-rollback log pull.
- settlement-service's elevated error_rate reflects the same storage failure even though it emitted zero ERROR lines at any level, so its failures are either logged at INFO or not logged.
- The absence of a config/feature-flag change is complete for the 14:45-15:45Z window (returned count 0 with partial false), even though later windows could not be queried.
- bank-partner-api was healthy during the incident because its sole caller's bank_api_error_rate was flat; a bank-side fault that never surfaced in the caller metric cannot be excluded from the caller proxy alone.
- (demoted from facts: no verified observation backs it) Historic precedent HIST-114 (2025-04-18) was a ledger-db disk-full incident caused by a write-side resource (retained WAL / inactive replication slot) with 'no space left on device' and write-failure symptoms.

## Timeline
- `2026-06-01T13:00:00Z` ledger-db disk_used_pct at 89.18% and rising ~0.35-0.5pp per 4-min bucket — the disk-fill trend begins >1.5h before any deploy. [F13]
- `2026-06-01T14:00:00Z` ledger-db disk_used_pct reaches 94.7%. [F13]
- `2026-06-01T14:46:00Z` dep-6203 deployed: payment-service v6.0.0 'New UPI mandate flow' (new ledger entry type MANDATE_HOLD) — the only change across all services in the 14:45-15:45Z window. [F1]
- `2026-06-01T14:46:50Z` payment-service logs 'deployment dep-6203 (v6.0.0) rollout complete, all pods ready'. [F11]
- `2026-06-01T14:48:00Z` ledger-db disk_used_pct 99.15%, ~2 minutes after deploy, still climbing on the pre-existing trend. [F13, F14]
- `2026-06-01T14:56:00Z` ledger-db disk_used_pct hits 99.97%, effectively saturating. [F13, F14]
- `2026-06-01T14:58:00Z` Error-rate step in the same bucket for all three writer consumers: settlement-service 38.58%, order-service 34.12%, payment-service 30.41%. [F15, F7]
- `2026-06-01T15:00:00Z` ledger-db disk_used_pct pinned at 100.0% for the remainder of the incident window. [F13]
- `2026-06-01T15:00:26Z` First payment-service ERROR: ledger write failure due to DB disk-space error (pg_wal/No space left on device signature). [F8]
- `2026-06-01T15:00:42Z` order-service first error — identical 'INSERT INTO orders failed: No space left on device' — 16 seconds after payment-service, in a service untouched by dep-6203. [F19, F20]
- `2026-06-01T15:01:01Z` First ledger-db own-log storage exhaustion: PANIC on pg_wal/xlogtemp.* and 'could not extend file base/16402/2619'. [F10]
- `2026-06-01T15:12:00Z` Rollback of dep-6203 (payment-service v6.0.0). [F1]
- `2026-06-01T15:14:00Z` payment-service error_rate 62.70% — no recovery after rollback. [F16]
- `2026-06-01T16:58:00Z` payment-service error_rate still 63.15%; ledger-db disk still at 100.0% — incident flat, not recovering ~1h46m after rollback. [F16, F13]

## Recommended actions
- **[immediate]** Remediate ledger-db disk space now: free space on the shared Postgres node (archive/remove retained WAL or drop an inactive replication slot per HIST-114 precedent) or expand the volume, then verify disk_used_pct drops below ~90% and payments recover. -- ledger-db is pinned at 100% with pg_wal/xlogtemp PANICs and 'could not extend file base/16402/2619'; the rollback of dep-6203 produced no recovery because the disk is still full. Restoring free space is the only action that can restore writes. [F13, F10, F16, F34]
- **[immediate]** Do NOT pursue further rollbacks or reverts of payment-service code as the mitigation; keep dep-6203 rolled back but treat it as unrelated to the failure and re-deploy once the incident is closed. -- v6.0.0 is temporally coincidental: the disk-fill trend predates the deploy by >1.5h, an undeployed service (order-service) emits the identical disk error, and no log line shows validation/rejection of MANDATE_HOLD. [F13, F14, F19, F22, F12]
- **[short_term]** Add disk capacity/utilisation alerting on ledger-db (threshold e.g. 85% warning / 92% page) plus WAL-retention and replication-slot monitoring, and set a growth-rate alarm given the observed ~0.35-0.5pp per 4-min fill rate. -- The disk climbed from 89.18% to 100% over roughly two hours with no evidence of any alerting or remediation event, and HIST-114 shows this class of failure recurring via retained WAL / inactive replication slots. [F13, F34]
- **[short_term]** Confirm that a remediation event actually took effect (space reclaimed or volume resized) and that disk_used_pct decreases; if it does not, escalate to storage/DB platform owners — do not assume a code fix will resolve it. -- The incident stayed flat at 100% disk for 1h46m after rollback; whether no remediation was applied or the applied remediation was insufficient is undetermined. [F16, F13]
- **[short_term]** If any disk-exhaustion errors persist after confirmed free space on ledger-db, re-open with new evidence (e.g. a distinct error signature or a change at the 15:00Z boundary), since the shared-disk explanation would no longer cover the residual failures. -- Second-pass deployment and config-change queries for the narrower 15:00-16:59Z window were budget-exhausted, so a boundary or post-15:45Z change could not be fully excluded. [F1, F3, F16]
- **[long_term]** Validate the shared-node blast-radius model: instrument per-consumer access paths (SQL/relation/tablespace) on ledger-db so that writer-only impact can be predicted, and explicitly determine why portfolio-service (also a ledger-db consumer) was immune. -- Three of four ledger-db consumers failed on the same bucket while portfolio-service stayed at ~0.16-0.25%; the dependency graph exposes only generic 'db' edges with no read/write or relation attribute, so H1's 'all writers fail' clause cannot be confirmed at the SQL level. [F18, F29, F30, F33, F15]
- **[long_term]** Add WAL/replication-slot housekeeping and capacity forecasting for all shared Postgres nodes, and require disk-headroom health checks in release/deploy gates for services that write to shared datastores. -- This is the second known ledger-db disk-full incident (HIST-114, 2025-04-18) with write-failure symptoms caused by retained WAL/inactive replication slot; recurrence should be prevented structurally rather than by ad-hoc cleanup. [F34]
- **[long_term]** Ensure the external bank-partner-api caller-proxy metric (bank_api_error_rate) is retained/paged — it was the only available evidence clearing the external dependency — and keep it in the standard payment-service dashboards. -- bank-partner-api has no direct telemetry and can only be observed through payment-service's caller metric, which was flat at ~0.20% and was the decisive evidence against the dependency-failure hypothesis. [F23, F24, F28]

## Data gaps
- ledger-db disk_used_pct time series in BYTES and node-level filesystem metrics were never retrieved — only the percentage series. Cannot quantify how much space v6.0.0's MANDATE_HOLD writes consumed, only that the trend and saturation predated/independently held.
- Second-pass get_deployments for 15:00-15:20Z and get_config_changes for 15:00-16:59Z returned budget_exhausted — cannot fully exclude a post-15:45Z or 15:00Z-boundary change other than the v6.0.0 deploy/rollback.
- No schema-migration data source exists; whether MANDATE_HOLD required a ledger-db schema migration and whether one was applied is undetermined.
- No consumer/producer topic map or code-search tool — cannot confirm which services actually read MANDATE_HOLD rows versus merely sharing ledger-db.
- Neighbour log signatures (settlement-service, api-gateway, auth-service) could not be queried; settlement-service returned zero ERROR/WARN lines despite a 38-56% error_rate, so its storage signature is textually unverified.
- Both payment-service and ledger-db log results were truncated (30 of 354 and 30 of 292 errors), so a rarer secondary error signature outside the returned/aggregated window cannot be ruled out.
- No payment-service log sample was pulled for the 15:12-16:59Z post-rollback window specifically; post-rollback signature is inferred from metric flatness and the sampled 14:30-16:59Z window.
- No confirmation of any disk-space remediation event (cleanup, volume resize, WAL retention change) between 15:12Z and 16:59Z — cannot explain why the incident stayed flat at 100% rather than slowly recovering.
- The dependency graph exposes no SQL/relation/tablespace or read-vs-write attribute on any ledger-db edge, and portfolio-service logs were not retrievable — H1's blast-radius claim that only writers fail remains unverified at the SQL level.
- bank-partner-api has no direct telemetry; a truly independent bank-side outage could evade detection if it did not surface in the caller's metric, so the external dependency cannot be excluded by direct observation.
- Metric buckets are 5-min (3-min when zoomed) — very short (<3 min) connection-pool spikes at onset cannot be confirmed or excluded.
- No order-service pod/Kubernetes-event data — the 'No space left on device' text is a DB write error surfaced through the app and cannot be attributed to a specific node/filesystem from app logs alone.

## What would change this conclusion
- A ledger-db disk_used_pct time series showing the disk was NOT on an independent upward trend before 14:46Z, or that it only crossed into critical range after the deploy — this would shift weight toward H2.
- Confirmation that the MANDATE_HOLD write volume was large enough to account for the fill rate from 14:46Z to 14:56Z (byte-level disk data), which would make the deploy a material contributor rather than coincidental.
- Evidence of a distinct post-rollback error signature (e.g. validation/schema/unsupported-entry-type errors) in the 15:12-16:59Z window instead of the unchanged disk-exhaustion signature — this would revive H3/H5.
- Discovery of an unrecorded change (deploy or config) at or near the 15:00Z boundary or after 15:45Z that correlates with the error step.
- A bank-partner-api outage or DB connection-pool blow-up (a real spike in ledger-db connections_active or payment-service's pool) coinciding with the 14:58Z step, which would support H4.
- Evidence that the disk was remediated (space reclaimed) well before 16:59Z yet payment-service errors stayed at ~60% — this would break the causal chain from disk exhaustion to the persistent failures.

## Integrity warnings (set by code)
- fact demoted to assumption: Historic precedent HIST-114 (2025-04-18) was a ledger-db disk-full incident caus

## Investigation stats
- latency 170.06s, LLM calls 27, tokens 204534 (in 171517 / out 33017)
- tool calls 42 (6 failed, 0 partial, 0 recovered by retry), findings 34/34 verified
