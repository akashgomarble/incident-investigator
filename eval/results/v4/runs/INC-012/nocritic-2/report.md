# Incident investigation report: INC-012
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The root cause of INC-012 is storage exhaustion on the shared ledger-db Postgres: an inactive replication slot ('analytics_cdc') retained growing WAL (33.9GB -> 69.5GB -> 77.4GB), driving /var/lib/postgresql/data to 98% by 14:48:20Z and to 100% around 15:00Z, after which the DB could not extend base files or write pg_wal/xlogtemp (F5, F16, F4). payment-service's ~60% error rate is a victim symptom of that full disk: its dominant error is 'ledger write failed: ... No space left on device' (354 entries from 15:00:26Z), matching the ledger-db PANICs from 15:01:01Z, not a code/validation/dependency defect (F3, F4, F6, F15). payment-service v6.0.0 (dep-6203, deployed 14:46:00Z) is at most a coincidental accelerator, not the origin: the disk was already 98% full 12 minutes before onset and before the deploy's steady-state traffic could matter (F7, F13, F14, F16). Confidence in this conclusion is medium (top hypothesis 0.60, per the reviewed ranking); rollback of payment-service alone did not clear the incident, consistent with the fault lying in the shared datastore (F8, F18).

## Probable root cause
**ledger-db disk exhaustion from retained WAL (inactive replication slot) is the origin; payment-service v6.0.0 is a coincidental accelerator/victim** -- resource_exhaustion in `ledger-db`, triggered by `null` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk exhaustion from retained WAL (inactive replication slot) is the origin; payment-service v6.0.0 is a coincidental accelerator/victim | resource_exhaustion | ledger-db | null | 0.60 (medium) | active |
| 2 | H2: payment-service v6.0.0's new MANDATE_HOLD write pattern accelerated disk/WAL consumption on ledger-db until it filled | bad_deployment | payment-service | dep-6203 | 0.30 (low) | active |
| 3 | H3: A schema/contract change (migration, validation config, or flag) rejects the new MANDATE_HOLD entry type and breaks ledger writes | config_change | ledger-db (write-path validation) | null | 0.03 (low) | rejected |
| 4 | H4: External bank-partner-api dependency failure is the origin; payment failures are the surface symptom (unverifiable) | dependency_failure | bank-partner-api | null | 0.02 (low) | rejected |

### H1: ledger-db disk exhaustion from retained WAL (inactive replication slot) is the origin; payment-service v6.0.0 is a coincidental accelerator/victim
The shared ledger-db filled its disk because an inactive replication slot ('analytics_cdc') retained growing WAL (33.9GB -> 69.5GB -> 77.4GB), pushing /var/lib/postgresql/data to 98% at 14:48:20Z. At ~15:00Z the disk hit 100%, and Postgres could no longer extend base files or write pg_wal/xlogtemp, producing 'No space left on device' PANICs. payment-service's ledger writes hit that full disk and returned 'ledger write failed: ... No space left on device' (60% error rate). The fault originates in the ledger-db storage/WAL-retention subsystem (infrastructure/resource), not in payment-service code; v6.0.0's new MANDATE_HOLD writes merely added write volume against an already nearly-full disk.

**Causal chain**
1. Inactive replication slot analytics_cdc retains WAL, growing 33.9GB -> 77.4GB by 14:49Z [F5, F16]
2. ledger-db disk reaches 98% by 14:48:20Z, before any payment-service error [F5, F16, F14]
3. Disk hits 100%; ledger-db emits PANIC 'could not write to pg_wal/xlogtemp' and 'could not extend file' ENOSPC from 15:01:01Z [F4, F6, F15]
4. payment-service ledger writes fail against the full shared disk, error rate jumps to ~60% at 15:00Z [F1, F2, F3, F6]
5. Rolling back payment-service alone cannot free ledger-db disk, so failures persist on the shared datastore used by four services [F18, F10]

**Supporting evidence**
  - `F1` ✔ payment-service error_rate jumped from a <1% baseline to ~60.5% at the 1-minute bucket 15:00:00Z (0.21% at 14:59, 60.5% at 15:00), with sustained ~59-67% values after; the earlier 5-min series shows the last normal bucket at 14:55 (avg 0.18) then 61.52 at 15:00:00Z.
  - `F2` ✔ Onset precedes the reported 15:10Z onset by ~10 minutes; the error rate was already ~60% in the 15:00Z bucket and roughly flat at 60-64% from 15:00 to 15:10Z, so the report's timing is late relative to the data.
  - `F3` ✔ payment-service logs show a single dominant error pattern: 'ledger write failed: ERROR: could not extend file: No space left on device', 354 ERROR entries, first_seen 2026-06-01T15:00:26Z.
  - `F4` ✔ ledger-db (the ledger write-path datastore; 'ledger-service' does not exist as a monitored service) logged disk-full failures beginning 15:01:01Z: 153x 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' and 139x 'could not extend file "base/<n>/<n>": No space left on device'.
  - `F5` ✔ ledger-db showed a disk-capacity warning before the outage: 14:48:20Z 'disk usage 98% on /var/lib/postgresql/data', plus an inactive replication slot 'analytics_cdc' retaining growing WAL (33916 MB at 14:34:05Z -> 69518 MB at 14:46:32Z -> 77371 MB at 14:49:47Z).
  - `F6` ✔ The failure mode is a storage/ENOSPC write failure at the database layer, not a validation rejection, timeout, connection error, or 5xx-from-dependency: both payment-service and ledger-db report 'No space left on device' on the ledger write path.
  - `F10` ✔ ledger-db is a shared postgres (owner platform, shared=true) with FOUR services writing to it: order-service, portfolio-service, payment-service and settlement-service; payment-service also depends on kafka (shared, queue-produce) and the external bank-partner-api (no telemetry). ledger-db is therefore a shared component through which a neighbour (e.g. settlement-service, order-service) could hurt payment-service.
  - `F11` ✔ Historical precedent HIST-114 (2025-04-18) is the closest match to 'ledger write failures': ledger-db 'disk full from retained WAL', symptoms 'no space left on device' / 'write failures', caused by an inactive replication slot retaining WAL, resolved by dropping the slot. HIST-111 (2025-05-22) shows a reporting query slowing ledger-db; HIST-102 (2025-09-02) shows portfolio-service timeouts during a ledger-db failover.
  - `F15` ✔ The failing component is ledger-db, a SHARED postgres (owner platform, shared=true) written by FOUR services: order-service, portfolio-service, payment-service and settlement-service (callers via api-gateway). Its error signature is storage exhaustion ('No space left on device' on pg_wal/xlogtemp and base files), not a schema/validation rejection of the new MANDATE_HOLD entry type and not a timeout or 5xx-from-dependency - so the failure is not generated by payment-service's code path itself.
  - `F16` ✔ Pre-onset resource evidence on the shared datastore: ledger-db disk warning at 14:48:20Z ('disk usage 98%') and an inactive replication slot 'analytics_cdc' retaining growing WAL (33916 MB -> 69518 MB -> 77371 MB); at 14:48Z disk was already 98% full, 12 min before the outage, so the resource was near-exhaustion before/independently of the v6.0.0 deploy's steady-state traffic.
  - `F17` ✔ Historical precedent HIST-114 (2025-04-18, match_score 10) is the closest structural match: 'ledger-db disk full from retained WAL', symptoms 'no space left on device'/'write failures', root cause 'Inactive replication slot retained WAL until disk filled', resolution 'Dropped slot; added slot-lag alert'. This is a lead for the fix target (ledger-db slot/WAL), not evidence about INC-012's cause.
  - `F18` ✔ Rollback-alone insufficiency: payment-service v6.0.0 was already rolled back at 15:12:00Z, and a query for ANY payment-service deployment after 15:00Z returns count 0 - i.e. nothing was re-released and the affected service is sitting on the rolled-back state, yet the incident persisted to the 16:59Z data end. Combined with the shared-datastore failure mode, a payment-service-only rollback does not clear the ledger-db full disk that four services depend on.
**Contradicting evidence**
  - (none)
**Assumptions**
  - WAL retention from the inactive slot is what consumed disk rather than v6.0.0 write volume (exact trajectory of disk_used_pct unquantified)
  - The ENOSPC is the actual cause of payment errors rather than a concurrent phenomenon; note the first payment error (15:00:26Z) marginally precedes the ledger-db PANIC log (15:01:01Z), so either the wall/panel is rounded or payment errors reflect the same full-file system
  - Post-incident data is not available to confirm the error rate only cleared after the disk/slot was addressed

### H2: payment-service v6.0.0's new MANDATE_HOLD write pattern accelerated disk/WAL consumption on ledger-db until it filled
Two rarely-verifiable conditions hold together: the ledger-db disk was already near-full (98% at 14:48:20Z, 2 min after the 14:46Z deploy) and V6.0.0's new mandate flow multiplied write volume and WAL generation on ledger-db. The combined effect is the 15:00Z ENOSPC. The origin of the fault is the v6.0.0 write pattern (bad_deployment) acting through a resource bottleneck; the fix would be to throttle/redesign the mandate writes in addition to freeing disk.

**Causal chain**
1. v6.0.0 deployed 14:46Z adding MANDATE_HOLD ledger entry type [F7, F13, F14]
2. deploy enables a higher-volume write pattern against ledger-db [F7, F13]
3. ledger-db disk grows from 98% to 100%, WAL/slot growth continues [F5, F16]
4. Disk full -> payment-service ledger writes fail with ENOSPC, error rate ~60% [F1, F3, F4]

**Supporting evidence**
  - `F1` ✔ payment-service error_rate jumped from a <1% baseline to ~60.5% at the 1-minute bucket 15:00:00Z (0.21% at 14:59, 60.5% at 15:00), with sustained ~59-67% values after; the earlier 5-min series shows the last normal bucket at 14:55 (avg 0.18) then 61.52 at 15:00:00Z.
  - `F3` ✔ payment-service logs show a single dominant error pattern: 'ledger write failed: ERROR: could not extend file: No space left on device', 354 ERROR entries, first_seen 2026-06-01T15:00:26Z.
  - `F7` ✔ In the entire window 2026-05-31T00:00Z to 2026-06-01T16:59Z across ALL services, there is exactly ONE deployment: payment-service v6.0.0 (dep-6203) deployed 2026-06-01T14:46:00Z, author p.nair, 'New UPI mandate flow', changes 'mandate create/approve endpoints' and 'new ledger entry type MANDATE_HOLD'. No other service deployed in this window.
  - `F13` ✔ The entire window 2026-05-31T00:00Z-2026-06-01T16:59Z contains exactly one deployment AND zero config changes: payment-service v6.0.0 (dep-6203) at 2026-06-01T14:46:00Z, status 'rolled_back' at 15:12:00Z, adding 'new ledger entry type MANDATE_HOLD' and 'mandate create/approve endpoints'. So the only change-combination candidate is v6.0.0 itself; no concurrent dependency-side deploy or flag change exists in the timeline.
  - `F14` ✔ v6.0.0 deployed at 14:46:00Z, i.e. ~14 min BEFORE the 15:00:26Z first 'ledger write failed: ... No space left on device' error and ~17 min before ledger-db's 15:01:01Z PANIC. The change therefore cannot be a >15-min-after causal agent; it is temporally upstream of onset within minutes-to-tens-of-minutes, consistent with a change that accelerates an existing resource drain.
  - `F16` ✔ Pre-onset resource evidence on the shared datastore: ledger-db disk warning at 14:48:20Z ('disk usage 98%') and an inactive replication slot 'analytics_cdc' retaining growing WAL (33916 MB -> 69518 MB -> 77371 MB); at 14:48Z disk was already 98% full, 12 min before the outage, so the resource was near-exhaustion before/independently of the v6.0.0 deploy's steady-state traffic.
**Contradicting evidence**
  - `F18` ✔ Rollback-alone insufficiency: payment-service v6.0.0 was already rolled back at 15:12:00Z, and a query for ANY payment-service deployment after 15:00Z returns count 0 - i.e. nothing was re-released and the affected service is sitting on the rolled-back state, yet the incident persisted to the 16:59Z data end. Combined with the shared-datastore failure mode, a payment-service-only rollback does not clear the ledger-db full disk that four services depend on.
  - `F15` ✔ The failing component is ledger-db, a SHARED postgres (owner platform, shared=true) written by FOUR services: order-service, portfolio-service, payment-service and settlement-service (callers via api-gateway). Its error signature is storage exhaustion ('No space left on device' on pg_wal/xlogtemp and base files), not a schema/validation rejection of the new MANDATE_HOLD entry type and not a timeout or 5xx-from-dependency - so the failure is not generated by payment-service's code path itself.
  - `F16` ✔ Pre-onset resource evidence on the shared datastore: ledger-db disk warning at 14:48:20Z ('disk usage 98%') and an inactive replication slot 'analytics_cdc' retaining growing WAL (33916 MB -> 69518 MB -> 77371 MB); at 14:48Z disk was already 98% full, 12 min before the outage, so the resource was near-exhaustion before/independently of the v6.0.0 deploy's steady-state traffic.
**Assumptions**
  - The new entry type actually increased WAL/write volume materially enough to fill the remaining 2% within 14 minutes (no write-rate or disk-trajectory data)
  - The traffic pattern is genuinely new double-writing rather than an equivalent-volume rewrite of an existing hold
  - Some mechanism connects the new code to disk growth that is not the pre-existing inactive slot (HIST-114 says the slot alone filled the disk)

### H3: A schema/contract change (migration, validation config, or flag) rejects the new MANDATE_HOLD entry type and breaks ledger writes
The new MANDATE_HOLD entry type is rejected by a schema/validation/handler on the write path (e.g. an unvalidated enum or a missing migration), causing ledger write failures. Origin is the contract/config layer, not the DB storage.

**Causal chain**
1. New MANDATE_HOLD entry type is introduced by v6.0.0 [F7, F13]
2. A validator/schema/migration rejects the type *(assumed)*
3. Ledger writes fail [F3]

**Supporting evidence**
  - `F3` ✔ payment-service logs show a single dominant error pattern: 'ledger write failed: ERROR: could not extend file: No space left on device', 354 ERROR entries, first_seen 2026-06-01T15:00:26Z.
**Contradicting evidence**
  - `F3` ✔ payment-service logs show a single dominant error pattern: 'ledger write failed: ERROR: could not extend file: No space left on device', 354 ERROR entries, first_seen 2026-06-01T15:00:26Z.
  - `F4` ✔ ledger-db (the ledger write-path datastore; 'ledger-service' does not exist as a monitored service) logged disk-full failures beginning 15:01:01Z: 153x 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' and 139x 'could not extend file "base/<n>/<n>": No space left on device'.
  - `F5` ✔ ledger-db showed a disk-capacity warning before the outage: 14:48:20Z 'disk usage 98% on /var/lib/postgresql/data', plus an inactive replication slot 'analytics_cdc' retaining growing WAL (33916 MB at 14:34:05Z -> 69518 MB at 14:46:32Z -> 77371 MB at 14:49:47Z).
  - `F6` ✔ The failure mode is a storage/ENOSPC write failure at the database layer, not a validation rejection, timeout, connection error, or 5xx-from-dependency: both payment-service and ledger-db report 'No space left on device' on the ledger write path.
  - `F9` ✔ Zero config changes were recorded across all services in 2026-05-31 to 2026-06-01T16:59Z (count 0). This contrasts with historical precedent HIST-108 (2025-10-01, auth-service) where a config/key change caused failures, so no config/flag change is a temporal candidate here.
  - `F15` ✔ The failing component is ledger-db, a SHARED postgres (owner platform, shared=true) written by FOUR services: order-service, portfolio-service, payment-service and settlement-service (callers via api-gateway). Its error signature is storage exhaustion ('No space left on device' on pg_wal/xlogtemp and base files), not a schema/validation rejection of the new MANDATE_HOLD entry type and not a timeout or 5xx-from-dependency - so the failure is not generated by payment-service's code path itself.
  - `F16` ✔ Pre-onset resource evidence on the shared datastore: ledger-db disk warning at 14:48:20Z ('disk usage 98%') and an inactive replication slot 'analytics_cdc' retaining growing WAL (33916 MB -> 69518 MB -> 77371 MB); at 14:48Z disk was already 98% full, 12 min before the outage, so the resource was near-exhaustion before/independently of the v6.0.0 deploy's steady-state traffic.
**Assumptions**
  - A schema/validator layer exists that could reject the entry type (gap: no schema-registry service in inventory)
**Rejected because:** H3's failure signature does not match: the observed error is storage ENOSPC ('could not extend file', 'could not write to pg_wal/xlogtemp'), not a validation/enum rejection or migration lock. No config changes were recorded (F9), no schema-registry exists in the inventory, and there is no per-entry-type validation error message anywhere in the findings. The disk-full evidence (F4, F6, F15, F16) directly contradicts this mechanism.

### H4: External bank-partner-api dependency failure is the origin; payment failures are the surface symptom (unverifiable)
The bank partner API returned 5xx (as in HIST-105), and payment-service failures are dominated by that dependency; ledger write failures are a separate or secondary phenomenon.

**Causal chain**
1. bank-partner-api begins returning 5xx / errors *(assumed)*
2. payment-service mandate/bank calls fail *(assumed)*
3. payment error rate rises to ~60% [F1, F2]

**Supporting evidence**
  - `F12` ✔ A prior payment-service failure HIST-105 (2025-11-19) had the same surface symptom set ('payment failures', 'bank api errors') but a dependency_failure root cause: the bank partner API returned 5xx for 40 minutes. bank-partner-api has has_telemetry=false, so this path is unverifiable from telemetry. This is a lead only, not evidence about INC-012.
  - `F10` ✔ ledger-db is a shared postgres (owner platform, shared=true) with FOUR services writing to it: order-service, portfolio-service, payment-service and settlement-service; payment-service also depends on kafka (shared, queue-produce) and the external bank-partner-api (no telemetry). ledger-db is therefore a shared component through which a neighbour (e.g. settlement-service, order-service) could hurt payment-service.
**Contradicting evidence**
  - `F3` ✔ payment-service logs show a single dominant error pattern: 'ledger write failed: ERROR: could not extend file: No space left on device', 354 ERROR entries, first_seen 2026-06-01T15:00:26Z.
  - `F4` ✔ ledger-db (the ledger write-path datastore; 'ledger-service' does not exist as a monitored service) logged disk-full failures beginning 15:01:01Z: 153x 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' and 139x 'could not extend file "base/<n>/<n>": No space left on device'.
  - `F5` ✔ ledger-db showed a disk-capacity warning before the outage: 14:48:20Z 'disk usage 98% on /var/lib/postgresql/data', plus an inactive replication slot 'analytics_cdc' retaining growing WAL (33916 MB at 14:34:05Z -> 69518 MB at 14:46:32Z -> 77371 MB at 14:49:47Z).
  - `F6` ✔ The failure mode is a storage/ENOSPC write failure at the database layer, not a validation rejection, timeout, connection error, or 5xx-from-dependency: both payment-service and ledger-db report 'No space left on device' on the ledger write path.
  - `F15` ✔ The failing component is ledger-db, a SHARED postgres (owner platform, shared=true) written by FOUR services: order-service, portfolio-service, payment-service and settlement-service (callers via api-gateway). Its error signature is storage exhaustion ('No space left on device' on pg_wal/xlogtemp and base files), not a schema/validation rejection of the new MANDATE_HOLD entry type and not a timeout or 5xx-from-dependency - so the failure is not generated by payment-service's code path itself.
  - `F16` ✔ Pre-onset resource evidence on the shared datastore: ledger-db disk warning at 14:48:20Z ('disk usage 98%') and an inactive replication slot 'analytics_cdc' retaining growing WAL (33916 MB -> 69518 MB -> 77371 MB); at 14:48Z disk was already 98% full, 12 min before the outage, so the resource was near-exhaustion before/independently of the v6.0.0 deploy's steady-state traffic.
  - `F18` ✔ Rollback-alone insufficiency: payment-service v6.0.0 was already rolled back at 15:12:00Z, and a query for ANY payment-service deployment after 15:00Z returns count 0 - i.e. nothing was re-released and the affected service is sitting on the rolled-back state, yet the incident persisted to the 16:59Z data end. Combined with the shared-datastore failure mode, a payment-service-only rollback does not clear the ledger-db full disk that four services depend on.
**Assumptions**
  - bank-partner-api could independently degrade at the same time (has_telemetry=false, uncheckable)
**Rejected because:** H4 rests only on a historical precedent lead explicitly labeled 'not evidence about INC-012' (F12). The dominant, verified error signature in INC-012 is ENOSPC on the ledger write path (F3, F4, F6, F15) with a clear pre-onset disk-full ramp (F5, F16); nothing in the findings shows bank-API 5xx at 14:48-15:00Z. HIST-105 is a past incident, not admissible as current-window evidence.

## Facts (verified observations)
- ledger-db showed disk-capacity warning at 14:48:20Z with disk usage 98% on /var/lib/postgresql/data, and an inactive replication slot 'analytics_cdc' retaining growing WAL (33916 MB at 14:34:05Z -> 69518 MB at 14:46:32Z -> 77371 MB at 14:49:47Z). [F5, F16]
- ledger-db logged disk-full failures beginning 15:01:01Z: 153x 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' and 139x 'could not extend file "base/<n>/<n>": No space left on device'. [F4]
- payment-service logs show a single dominant error pattern: 'ledger write failed: ERROR: could not extend file: No space left on device', 354 entries, first_seen 2026-06-01T15:00:26Z. [F3]
- payment-service error_rate jumped from a <1% baseline to ~60.5% in the 1-minute bucket 15:00:00Z (0.21% at 14:59), sustaining ~59-67% afterwards; the 5-minute series shows the last normal bucket at 14:55 (avg 0.18) then 61.52 at 15:00:00Z. [F1]
- The failing component is ledger-db, a shared postgres (owner platform, shared=true) written by four services (order-service, portfolio-service, payment-service, settlement-service); its error signature is storage exhaustion, not rejection of the new MANDATE_HOLD entry type nor a timeout/5xx-from-dependency, so the failure is not generated by payment-service's code path itself. [F15, F10]
- In the entire window 2026-05-31T00:00Z to 2026-06-01T16:59Z there is exactly ONE deployment: payment-service v6.0.0 (dep-6203) at 2026-06-01T14:46:00Z, adding 'new ledger entry type MANDATE_HOLD' and 'mandate create/approve endpoints'; no other service deployed and zero config changes were recorded (F9). [F7, F13, F9]
- v6.0.0 deployed at 14:46:00Z, ~14 min before the 15:00:26Z first ledger-write ENOSPC error and ~17 min before ledger-db's 15:01:01Z PANIC; the disk was already 98% full at 14:48Z, 12 min before the outage. [F14, F16]
- payment-service v6.0.0 was rolled back at 15:12:00Z (status 'rolled_back') and no payment-service deployment after 15:00Z exists (count 0), yet the incident persisted to the 16:59Z data end - consistent with a shared-datastore fault that a payment-service-only rollback cannot clear. [F8]
- Historical precedent HIST-114 (2025-04-18, match_score 10) is the closest structural match: 'ledger-db disk full from retained WAL' with symptoms 'no space left on device'/'write failures', root cause 'Inactive replication slot retained WAL until disk filled', resolved by dropping the slot and adding a slot-lag alert. [F11]
- The first payment error (15:00:26Z) marginally precedes the ledger-db PANIC log (15:01:01Z). [F3, F4]

## Assumptions (not verified)
- WAL retention from the inactive 'analytics_cdc' slot is what consumed disk rather than v6.0.0 write volume; the exact trajectory of disk_used_pct was not quantified (only the 98% point-in-time warning is verified).
- The ENOSPC is the actual cause of the payment errors rather than a concurrent phenomenon; note the first payment error (15:00:26Z) marginally precedes the ledger-db PANIC log (15:01:01Z), so either the wall/panel is rounded or the payment errors reflect the same full file system.
- Post-incident data was not available to confirm that the error rate only cleared after the disk/slot was addressed; the rollback-alone-insufficiency inference rests on the shared-ledger-db disk being full plus the absence of any new deploy, not on a post-rollback metric series.
- The v6.0.0 write pattern (if it increased write volume) added load against an already nearly-full disk rather than being the origin; no write-rate or disk-trajectory data was obtained to quantify this.
- The 'analytics_cdc' slot's WAL-retention behavior was pre-existing rather than newly introduced near the incident; the trigger-vs-cause split rests on timing only, as ledger-db's own deployment/config history is not exposed.
- (demoted from facts: no verified observation backs it) The observed failure mode is a storage/ENOSPC write failure at the database layer, not a validation rejection, timeout, connection error, or 5xx-from-dependency; both payment-service and ledger-db report 'No space left on device' on the ledger write path.

## Timeline
- `2026-06-01T14:34:05Z` Inactive replication slot 'analytics_cdc' holding ~33916 MB of retained WAL on ledger-db. [F5, F16]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (dep-6203) deployed, adding the new MANDATE_HOLD ledger entry type and mandate create/approve endpoints; the only deployment in the entire window. [F7, F13, F14]
- `2026-06-01T14:46:32Z` Retained WAL from the inactive slot grows to ~69518 MB. [F5, F16]
- `2026-06-01T14:48:20Z` ledger-db disk-capacity warning: disk usage 98% on /var/lib/postgresql/data (12 min before the outage). [F5, F16]
- `2026-06-01T14:49:47Z` Retained WAL grows further to ~77371 MB. [F5, F16]
- `2026-06-01T14:55:00Z` Last normal payment-service 5-min error-rate bucket (avg 0.18). [F1]
- `2026-06-01T15:00:26Z` First payment-service 'ledger write failed: ... No space left on device' error (354 ERROR entries total, dominant pattern). [F3]
- `2026-06-01T15:00:00Z` payment-service error_rate jumps to ~60.5% in the 1-minute bucket (from 0.21% at 14:59); onset precedes the 15:10Z report by ~10 minutes. [F1, F2]
- `2026-06-01T15:01:01Z` ledger-db begins logging disk-full PANICs: 'could not write to pg_wal/xlogtemp.<n>' and 'could not extend file base/<n>/<n>' (153x and 139x). [F4, F6]
- `2026-06-01T15:10:00Z` Incident INC-012 reported by on-call SRE (report timing is late relative to the data). [F2]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rolled back (status 'rolled_back'); no further payment-service deployments follow. [F8, F18]
- `2026-06-01T16:59:00Z` Data end: incident persisted; no payment-service deployment after 15:00Z (count 0), consistent with a shared-datastore fault not cleared by rollback. [F18]

## Recommended actions
- **[immediate]** Free disk on ledger-db immediately: drop (or reactivate/advance LSN on) the inactive replication slot 'analytics_cdc' to release the ~77GB of retained WAL, then verify /var/lib/postgresql/data usage drops below ~80%. -- The origin is WAL retained by the inactive slot filling the shared ledger-db disk; dropping the slot is the documented resolution of the closest historical match HIST-114 (F5, F11, F16, F17). [F5, F11, F16, F17]
- **[immediate]** Confirm payment-service error rate returns to <1% and ledger-db stops emitting 'No space left on device' PANICs after disk space is freed; if errors persist after disk is freed, escalate to a ledger-db write-path investigation. -- Distinguishes the resource-exhaustion root cause from residual effects and validates that the payment failures were disk-driven (F1, F3, F4, F6). [F1, F3, F4, F6]
- **[immediate]** Do NOT rely on the payment-service rollback alone as mitigation; keep v6.0.0 rolled back or hold further releases only if write volume must be capped, but treat the ledger-db disk/slot fix as the required remediation. -- v6.0.0 was already rolled back at 15:12Z and the incident persisted to 16:59Z with no new deploy, showing a payment-service-only rollback does not clear the shared full disk (F8, F18). [F8, F18]
- **[short_term]** Add a replication-slot lag/retention alert on ledger-db (e.g. inactive-slot WAL size and slot lag thresholds) and a disk-headroom alert at 85% for /var/lib/postgresql/data. -- HIST-114's resolution explicitly added a slot-lag alert; pre-onset disk was silently at 98% with only a single late warning at 14:48:20Z (F16, F17). [F16, F17]
- **[short_term]** Quantify ledger-db disk_used_pct, connections_active, replication_lag_ms and slow_queries_per_min over the incident window, and confirm whether the ~60% error rate cleared only after the disk/slot fix was applied. -- The exact disk ramp and post-remediation recovery were unquantified (observability gaps), and the rollback-insufficiency inference currently rests on shared-disk failure mode rather than a post-rollback metric series (F18). [F18]
- **[long_term]** Review the 'analytics_cdc' replica consumer and any CDC/analytics pipeline to ensure slots are monitored and cannot be abandoned silently; enforce max_slot_wal_keep_size on ledger-db to bound WAL retention. -- The inactive slot's unbounded WAL retention is the systemic defect that filled the shared datastore, and a retention cap prevents recurrence (F5, F11, F16, F17). [F5, F11, F16, F17]
- **[long_term]** Add disk-space capacity planning and tenant isolation/headroom review for the shared ledger-db, since four services (order, portfolio, payment, settlement) depend on it and single-datastore exhaustion is a cross-service blast radius. -- ledger-db is shared by four writers; its exhaustion caused a SEV-1 across a service that had not itself changed (F10, F15). [F10, F15]

## Data gaps
- ledger-db exposes no error_rate metric (only connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min), so its error rate could not be trended numerically; conclusions rely on logs.
- ledger-db resource metrics (disk_used_pct, connections_active, cpu_pct, replication_lag_ms, slow_queries_per_min) were never trended over time - the exact disk ramp to 98% and connection-pool behavior around 15:00Z are unquantified.
- No post-rollback payment-service error-rate series was obtained, so the claim that rollback alone failed to restore service rests on the shared-disk failure mode plus the absence of a new deploy, not on a direct recovery metric.
- Blast radius across other ledger writers (order-service, settlement-service, portfolio-service) is only partially established; a settlement-service ERROR log query returned 0 matches.
- No kafka (queue) log/metric query was run, so a queue-side contribution is unchecked.
- Searches for schema-registry/enum-validation-rejection and 'new ledger entry type/migration lock' precedents were not executed (budget exhausted), leaving those alternative mechanisms unconfirmed.
- 'ledger-service' does not exist in the inventory (not_found); the ledger is reached only as the shared postgres 'ledger-db', and no schema-registry or feature-flag service is known.
- ledger-db's own deployment/config history is not exposed (only 1 deployment total in window, all payment-service), so it is unknown whether the 'analytics_cdc' slot or a retention setting was recently changed.
- bank-partner-api has has_telemetry=false and could not be queried; a concurrent external 5xx (as in HIST-105) cannot be fully ruled out from telemetry, though the ENOSPC signature dominates.

## What would change this conclusion
- Evidence that the 'analytics_cdc' slot's retained WAL was NOT the disk consumer (e.g. WAL retention attributable to another source, or disk growth tracked to v6.0.0 write volume after the slot was idle) would shift the origin toward v6.0.0 (H2).
- A verified ledger-db disk_used_pct trajectory showing the disk was comfortably below capacity before 14:46Z and only climbed after the v6.0.0 deploy would support the H2 bad_deployment mechanism.
- A per-entry-type schema/validation rejection or migration-lock error (not ENOSPC) in ledger-db or payment-service logs would revive H3.
- Telemetry showing bank-partner-api 5xx during 14:48-15:00Z, or payment errors persisting after the ledger-db disk was freed, would support H4 (dependency_failure) or a non-storage cause.
- Demonstration that the payment-service error rate returned to baseline before 15:12Z (i.e. without any disk/slot remediation) would undercut the shared-disk root cause and point at the service rollback instead.
- Evidence of an earlier pre-window change (e.g. a ledger-db migration or slot/retention config change before 2026-05-25) would introduce a stronger temporal candidate than v6.0.0.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: The observed failure mode is a storage/ENOSPC write failure at the database laye

## Investigation stats
- latency 97.18s, LLM calls 12, tokens 72229 (in 56153 / out 16076)
- tool calls 22 (4 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
