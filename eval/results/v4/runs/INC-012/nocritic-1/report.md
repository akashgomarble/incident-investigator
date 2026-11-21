# Incident investigation report: INC-012
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The incident is a shared-datastore disk exhaustion on ledger-db, not a defect in payment-service v6.0.0 (H1, confidence 0.60, medium). All error evidence is a single 'No space left on device' signature appearing within a ~35 second window across independent components: payment-service (15:00:26Z), order-service (15:00:42Z) — which has no mandate code path — and ledger-db itself (15:01:01Z), with ledger-db emitting pg_wal xlogtemp and base-relation extension PANICs. This blast pattern and the absence of any config/schema change (F3) reject the deployment-cause hypothesis (H2, 0.08) and the config-change hypothesis (H3, 0.05); H4 (infra/volume fault, 0.12) is not excluded but is contradicted by the absence of any app-side saturation and by the generic write-growth signature. Confidence is capped at medium because the disk_used_pct timeline for ledger-db was never retrieved, so the fill onset is inferred from log first-seen times alone.

## Probable root cause
**Shared ledger-db ran out of disk space (No space left on device), failing all writes** -- resource_exhaustion in `ledger-db`, triggered by `null` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Shared ledger-db ran out of disk space (No space left on device), failing all writes | resource_exhaustion | ledger-db | null | 0.60 (medium) | active |
| 2 | H4: ledger-db infrastructure/disk failure (node or volume degraded) rather than runaway write growth | infrastructure_failure | ledger-db | null | 0.12 (low) | active |
| 3 | H2: payment-service v6.0.0 mandate flow introduced the fault (new MANDATE_HOLD ledger entry type / contract mismatch rejected by ledger-db) | bad_deployment | payment-service | dep-6203 | 0.08 (low) | rejected |
| 4 | H3: A schema/migration or config change to ledger-db near 14:50–15:00 introduced an incompatible contract | config_change | ledger-db | null | 0.05 (low) | rejected |

### H1: Shared ledger-db ran out of disk space (No space left on device), failing all writes
The shared Postgres datastore ledger-db exhausted its filesystem (pg_wal / base relations could not be extended). Because payment-service, order-service, portfolio-service and settlement-service all write to this one shared datastore, writes began failing with 'No space left on device' at ~15:00Z. payment-service is simply the loudest writer/traffic source, so its 60% error rate is where the symptom surfaced. The v6.0.0 mandate flow is incidental — its extra ledger writes may have contributed to disk growth but are not the failure mechanism.

**Causal chain**
1. ledger-db's filesystem fills / WAL and base files cannot be extended [F9, F12]
2. Any write to ledger-db fails with 'No space left on device', independent of entry type [F8, F15]
3. All writers to the shared ledger-db are affected simultaneously: payment-service 15:00:26Z, order-service (no mandate path) 15:00:42Z, ledger-db PANICs 15:01:01Z [F14, F16, F5]
4. payment-service's error_rate jumps from ~0.2% to ~62% and stays there because its ledger writes are hard-failures [F7, F13, F8]
5. Latency and app-level resources are unaffected, consistent with a hard write error rather than a saturation of payment-service itself [F10, F11]

**Supporting evidence**
  - `F8` ✔ ALL 354 ERROR-level payment-service log lines in 14:00-16:59Z carry a single signature: 'ledger write failed: ERROR: could not extend file: No space left on device (payment <n>)'. First match is 2026-06-01T15:00:26Z and last 15:57:54Z-ish, tightly aligned with the metric onset at 15:00Z.
  - `F9` ✔ ledger-db (the datastore behind ledger writes; there is no service named 'ledger-service') emits 292 ERROR log lines starting 2026-06-01T15:01:01Z, dominated by disk-space signatures: 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (153) and 'could not extend file "base/<n>/<n>": No space left on device' (139). This is the source matching payment-service's 'could not extend file' errors and is ~35s later than the first payment error.
  - `F14` ✔ Non-mandate writers to the shared ledger-db fail simultaneously with disk-space errors: order-service (no MANDATE_HOLD path) emits 236 ERROR lines, single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', first at 2026-06-01T15:00:42Z, last 16:57:47Z. This failure is generic, not entry-type specific.
  - `F15` ✔ payment-service's own error signature is identical in kind: 354 ERROR lines, all one pattern 'ledger write failed: ERROR: could not extend file: No space left on device (payment <n>)', first 2026-06-01T15:00:26Z. The message names no entry type (no MANDATE_HOLD vs legacy distinction), so it does not localize the failure to the new mandate path.
  - `F16` ✔ The onset order across components matches a shared-datastore disk exhaustion, not an app-path defect: payment-service first error 15:00:26Z, order-service first error 15:00:42Z (~16s later), and per T1's finding ledger-db first PANIC at 15:01:01Z. All three are the same 'No space left on device' signature within ~35s.
  - `F5` ✔ ledger-db is a shared datastore written by four services: order-service, portfolio-service, payment-service and settlement-service all hold 'db' edges into ledger-db, so write failures on the new MANDATE_HOLD path can affect and be affected by neighbours through this shared component.
  - `F7` ✔ payment-service error_rate is flat at ~0.2-0.3% from 14:00Z through the 14:54Z bucket (last pre-onset bucket avg 0.19, max 0.28), then jumps to avg 61.9 / max 66.98 in the 15:00:00Z bucket and stays elevated at ~60-64% for the rest of the window (last bucket 16:54Z avg 61.26). The 6-min buckets place onset between 14:54Z and 15:00Z, i.e. ~15:00Z, which is ~2.5h, not the reported ~15 min after release.
  - `F13` ✔ Error onset is at 15:00Z: payment-service error_rate is flat 0.17-0.29% through the 14:57Z bucket (14:57 avg 0.21, max 0.28) and jumps to avg 60.29 / max 60.8 in the 15:00:00Z bucket, staying 58.9-66.4% thereafter. Change dep-6203 (v6.0.0) deployed 14:46:00Z, so the lag from deploy to onset is ~14 min (consistent with the reported '~15 min after release'), but onset does NOT coincide with the deploy.
  - `F10` ✔ payment-service latency_p99_ms shows NO degradation: pre-onset 14:00-14:54Z is ~316-333ms and post-onset 15:00-16:54Z stays in the same band (~309-330ms avg, max 348.32). So the failure is a hard write error, not a latency/saturation symptom.
  - `F11` ✔ payment-service has NO application-level resource saturation: db_pool_in_use stays ~7.1-8.9 connections across the whole window (15:00Z avg 7.99) and cpu_pct stays ~27-33% (15:00Z avg 30.26, max 32.99) both before and after onset.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Disk-fill onset preceded 15:00Z; exact time not measured (disk_used_pct timeline never retrieved, F12/gaps).
  - portfolio-service and settlement-service also write in this window but logged no errors — could be no/low traffic (gap acknowledged in F17).
  - No config/schema change caused it — consistent with F3 (zero config changes recorded), though the datastore's own migration history is unverified.

### H4: ledger-db infrastructure/disk failure (node or volume degraded) rather than runaway write growth
The underlying disk/volume backing ledger-db failed or the node hosting it degraded, producing 'No space left on device' errors independent of write volume. Distinguished from H1 by origin: a storage/node fault rather than the datastore simply filling from accumulated writes.

**Causal chain**
1. ledger-db's backing volume or node degrades, so writes cannot extend files [F9]
2. All writers to ledger-db fail with disk-space errors [F8, F14]

**Supporting evidence**
  - `F9` ✔ ledger-db (the datastore behind ledger writes; there is no service named 'ledger-service') emits 292 ERROR log lines starting 2026-06-01T15:01:01Z, dominated by disk-space signatures: 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (153) and 'could not extend file "base/<n>/<n>": No space left on device' (139). This is the source matching payment-service's 'could not extend file' errors and is ~35s later than the first payment error.
  - `F8` ✔ ALL 354 ERROR-level payment-service log lines in 14:00-16:59Z carry a single signature: 'ledger write failed: ERROR: could not extend file: No space left on device (payment <n>)'. First match is 2026-06-01T15:00:26Z and last 15:57:54Z-ish, tightly aligned with the metric onset at 15:00Z.
  - `F14` ✔ Non-mandate writers to the shared ledger-db fail simultaneously with disk-space errors: order-service (no MANDATE_HOLD path) emits 236 ERROR lines, single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', first at 2026-06-01T15:00:42Z, last 16:57:47Z. This failure is generic, not entry-type specific.
**Contradicting evidence**
  - `F11` ✔ payment-service has NO application-level resource saturation: db_pool_in_use stays ~7.1-8.9 connections across the whole window (15:00Z avg 7.99) and cpu_pct stays ~27-33% (15:00Z avg 30.26, max 32.99) both before and after onset.
  - `F10` ✔ payment-service latency_p99_ms shows NO degradation: pre-onset 14:00-14:54Z is ~316-333ms and post-onset 15:00-16:54Z stays in the same band (~309-330ms avg, max 348.32). So the failure is a hard write error, not a latency/saturation symptom.
**Assumptions**
  - That an infrastructure fault (as opposed to natural disk fill from accumulated writes) is the mechanism — no node/volume telemetry was retrieved.

### H2: payment-service v6.0.0 mandate flow introduced the fault (new MANDATE_HOLD ledger entry type / contract mismatch rejected by ledger-db)
The reporter's hypothesis: v6.0.0's new mandate-flow code writes a MANDATE_HOLD ledger entry type that ledger-db rejects (enum/check constraint or contract mismatch between app and schema), causing ledger write failures on the new path. Whether the root is 'new code is buggy' or 'the change exposed an incompatible DB enum/contract' is the same origin (the v6.0.0 change) and not split out.

**Causal chain**
1. v6.0.0 (dep-6203) deployed 14:46Z adds mandate create/approve endpoints and the MANDATE_HOLD ledger entry type [F1]
2. New mandate path writes MANDATE_HOLD entries to ledger-db [F1]
3. Those writes are rejected (assumed: enum/constraint/contract mismatch), producing ledger write failures *(assumed)*
4. payment-service error_rate spikes to ~60% [F7]

**Supporting evidence**
  - `F1` ✔ Exactly one deployment occurred across all services between 2026-06-01T13:00Z and 16:00Z: payment-service v6.0.0 (dep-6203), deployed 14:46:00Z by p.nair, summarised 'New UPI mandate flow', changes 'mandate create/approve endpoints' and 'new ledger entry type MANDATE_HOLD'; status 'rolled_back' at 15:12:00Z.
  - `F7` ✔ payment-service error_rate is flat at ~0.2-0.3% from 14:00Z through the 14:54Z bucket (last pre-onset bucket avg 0.19, max 0.28), then jumps to avg 61.9 / max 66.98 in the 15:00:00Z bucket and stays elevated at ~60-64% for the rest of the window (last bucket 16:54Z avg 61.26). The 6-min buckets place onset between 14:54Z and 15:00Z, i.e. ~15:00Z, which is ~2.5h, not the reported ~15 min after release.
**Contradicting evidence**
  - `F8` ✔ ALL 354 ERROR-level payment-service log lines in 14:00-16:59Z carry a single signature: 'ledger write failed: ERROR: could not extend file: No space left on device (payment <n>)'. First match is 2026-06-01T15:00:26Z and last 15:57:54Z-ish, tightly aligned with the metric onset at 15:00Z.
  - `F14` ✔ Non-mandate writers to the shared ledger-db fail simultaneously with disk-space errors: order-service (no MANDATE_HOLD path) emits 236 ERROR lines, single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', first at 2026-06-01T15:00:42Z, last 16:57:47Z. This failure is generic, not entry-type specific.
  - `F15` ✔ payment-service's own error signature is identical in kind: 354 ERROR lines, all one pattern 'ledger write failed: ERROR: could not extend file: No space left on device (payment <n>)', first 2026-06-01T15:00:26Z. The message names no entry type (no MANDATE_HOLD vs legacy distinction), so it does not localize the failure to the new mandate path.
  - `F16` ✔ The onset order across components matches a shared-datastore disk exhaustion, not an app-path defect: payment-service first error 15:00:26Z, order-service first error 15:00:42Z (~16s later), and per T1's finding ledger-db first PANIC at 15:01:01Z. All three are the same 'No space left on device' signature within ~35s.
  - `F3` ✔ No config changes (flags, toggles, env/app config, schema-config entries) were recorded for any service in 2026-06-01T13:00Z–16:00Z; result was empty with count 0, not truncated.
  - `F17` ✔ Blast radius extends to every service writing to ledger-db except payment/order in this window: portfolio-service and settlement-service returned 0 ERROR lines for 14:30-16:59Z (total_matched 0, not truncated), and api-gateway returned 0 ERROR lines — so the failure is datastore-wide but only visible in the two writers that had matching traffic/log capture.
**Assumptions**
  - That a MANDATE_HOLD-specific rejection mechanism exists (enum/constraint) — never confirmed; no config/schema change found (F3).
  - That the fault is confined to the mandate path.
**Rejected because:** The error signature is 'No space left on device', a disk-exhaustion message, not an enum/constraint/contract violation (F8, F9, F15). order-service, which has no MANDATE_HOLD path, fails with the same generic disk-space error at essentially the same time (F14, F16). The error message names no entry type (F15). A mandate-flow code defect would not produce disk-space errors on an unrelated service's orders INSERT.

### H3: A schema/migration or config change to ledger-db near 14:50–15:00 introduced an incompatible contract
An unrecorded schema migration or config change (e.g. adding MANDATE_HOLD to a DB enum/check constraint, or altering the ledger schema) landed shortly before onset and caused writes to be rejected.

**Causal chain**
1. A schema/config change to ledger-db is applied near onset (assumed) *(assumed)*
2. Existing/new writes violate the new constraint (assumed) *(assumed)*
3. payment-service and neighbours see write failures [F7]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F3` ✔ No config changes (flags, toggles, env/app config, schema-config entries) were recorded for any service in 2026-06-01T13:00Z–16:00Z; result was empty with count 0, not truncated.
  - `F8` ✔ ALL 354 ERROR-level payment-service log lines in 14:00-16:59Z carry a single signature: 'ledger write failed: ERROR: could not extend file: No space left on device (payment <n>)'. First match is 2026-06-01T15:00:26Z and last 15:57:54Z-ish, tightly aligned with the metric onset at 15:00Z.
  - `F14` ✔ Non-mandate writers to the shared ledger-db fail simultaneously with disk-space errors: order-service (no MANDATE_HOLD path) emits 236 ERROR lines, single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', first at 2026-06-01T15:00:42Z, last 16:57:47Z. This failure is generic, not entry-type specific.
  - `F15` ✔ payment-service's own error signature is identical in kind: 354 ERROR lines, all one pattern 'ledger write failed: ERROR: could not extend file: No space left on device (payment <n>)', first 2026-06-01T15:00:26Z. The message names no entry type (no MANDATE_HOLD vs legacy distinction), so it does not localize the failure to the new mandate path.
**Assumptions**
  - That an unrecorded change occurred — F3 found zero config changes for any service in 13:00–16:00Z, and no migration was observed.
**Rejected because:** F3 shows zero config changes across all services in the window. The error is a filesystem error ('No space left on device'), not a constraint-violation error, and it hits order-service (no mandate path) identically (F14, F8).

## Facts (verified observations)
- ledger-db is a shared Postgres datastore written by four services (order-service, portfolio-service, payment-service, settlement-service); there is no 'ledger-service' service in the inventory. [F4, F5]
- Exactly one deployment occurred in 13:00-16:00Z: payment-service v6.0.0 (dep-6203) at 14:46:00Z adding a new MANDATE_HOLD ledger entry type; it was rolled back at 15:12:00Z. [F1, F2]
- No config changes (flags, toggles, env/app config, schema-config entries) were recorded for any service in 13:00-16:00Z; the result was empty, count 0, not truncated. [F3]
- payment-service error_rate was flat at ~0.19-0.29% through the 14:57Z bucket, then jumped to ~60-62% in the 15:00Z bucket and stayed at ~60-64% for the rest of the window — onset at ~15:00Z, about 14 minutes after the 14:46Z deploy. [F7, F13]
- All 354 ERROR-level payment-service log lines in 14:00-16:59Z carry one signature: 'ledger write failed: ERROR: could not extend file: No space left on device', first at 15:00:26Z; the message names no ledger entry type. [F8, F15]
- order-service, which has no MANDATE_HOLD path, failed simultaneously with the same generic pattern 'INSERT INTO orders failed: No space left on device', 236 ERROR lines, first at 15:00:42Z and last 16:57:47Z. [F14]
- ledger-db emitted 292 ERROR lines from 15:01:01Z, dominated by 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (153) and 'could not extend file "base/<n>/<n>": No space left on device' (139). [F9]
- payment-service latency_p99_ms shows no degradation (316-333ms pre-onset vs 309-330ms post-onset) and there is no app-level saturation (db_pool_in_use ~7.1-8.9, cpu_pct ~27-33% across the window) — the failure is a hard write error, not latency/saturation. [F10, F11]
- portfolio-service, settlement-service and api-gateway returned 0 ERROR lines for 14:30-16:59Z (not truncated), so the datastore-wide failure is visible only in the two ledger writers that had matching traffic/log capture. [F17]

## Assumptions (not verified)
- Assumption: ledging-db disk fill began before 15:00Z; the exact fill curve and crossing time were never measured (F12, data gaps).
- Assumption: portfolio-service and settlement-service had no/low write traffic in the window, so their absence of errors does not mean they were healthy (F17 gap).
- Assumption: no unrecorded ledger-db migration, enum or CHECK-constraint change occurred. F3 shows zero recorded config/schema changes for all services in 13:00-16:00Z, but the datastore's own migration history was not independently verified.
- Assumption: the v6.0.0 mandate flow's extra ledger writes contributed at most to disk growth and are not the failure mechanism (H1 framing).
- Assumption: the ~15:00Z onset and the reported '~15 min after release' are consistent with a fill that crossed the threshold 14 minutes after deploy, rather than with a deploy-triggered code path (F13).
- Assumption: ledger-db is a single-node/volume datastore whose storage is shared by all four writers (F5) and is not sharded per-service.
- (demoted from facts: no verified observation backs it) First-error ordering across components is payment-service 15:00:26Z -> order-service 15:00:42Z -> ledger-db PANIC 15:01:01Z, all the same disk-space signature within ~35s.
- (demoted from facts: no verified observation backs it) ledger-db exposes disk_used_pct and slow_queries_per_min metrics but no error_rate metric; the disk-usage timeline was not retrieved, so disk-fill onset is inferred from log first-seen times only.

## Timeline
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (dep-6203) deployed by p.nair: new UPI mandate flow, mandate create/approve endpoints, new MANDATE_HOLD ledger entry type. [F1]
- `2026-06-01T14:57:00Z` payment-service error_rate still flat and healthy (14:57Z bucket avg 0.21, max 0.28). [F13]
- `2026-06-01T15:00:00Z` Error onset: payment-service error_rate jumps to avg ~60-62% and remains ~60-64% for the rest of the window. No latency or CPU/pool saturation accompanies it. [F7, F13, F10, F11]
- `2026-06-01T15:00:26Z` First payment-service error: 'ledger write failed: ERROR: could not extend file: No space left on device' (no entry type named). [F8, F15]
- `2026-06-01T15:00:42Z` First order-service error: 'INSERT INTO orders failed: No space left on device' — an unrelated, non-mandate writer to the shared ledger-db fails the same way. [F14, F16]
- `2026-06-01T15:01:01Z` ledger-db itself begins emitting ERROR/PANIC log lines: pg_wal/xlogtemp and base-relation 'could not extend file' disk-space failures. [F9, F16]
- `2026-06-01T15:10:00Z` Incident INC-012 reported by on-call SRE; rollback of the suspected release started. [F2]
- `2026-06-01T15:12:00Z` dep-6203 rollback completes (26 minutes after deploy, ~2 minutes after the report). [F2]
- `2026-06-01T15:57:54Z` Last observed payment-service ledger-write error line in the retrieved sample. [F8]
- `2026-06-01T16:54:00Z` payment-service error_rate still elevated (last bucket avg 61.26%) and order-service still failing (last error 16:57:47Z): rollback did not restore service. [F7, F14]

## Recommended actions
- **[immediate]** Treat the v6.0.0 rollback as ineffective and stop attributing the incident to it: do not roll forward/back further on payment-service. Instead declare the ledger-db datastore the incident owner. -- The rollback at 15:12Z was followed by ~105 more minutes of continued failures in payment-service (F7) and order-service (F14); the failing writer with no mandate path (order-service) cannot be fixed by a payment-service change (F14, F16). [F2, F7, F14, F16]
- **[immediate]** Relieve disk pressure on ledger-db immediately: free space on the volume backing the Postgres data directory (e.g. archive/rotate or remove stale WAL and old base relations, expand the volume/LV, or raise the filesystem quota). Verify recovery by checking that ledger-db stops emitting 'No space left on device' and that payment-service error_rate falls back toward the ~0.2% baseline. -- The one observed mechanism is filesystem exhaustion on ledger-db — pg_wal/xlogtemp and base-relation extension failures (F9), mirrored by 'could not extend file' from payment-service (F8, F15). All other hypotheses are rejected by the same evidence. [F8, F9, F15, F16]
- **[short_term]** Capture the ledger-db disk_used_pct (and slow_queries_per_min) timeline for 13:00-17:00Z, and check whether its disk crossed the threshold before 15:00Z, confirming disk fill as the onset mechanism and giving the growth rate. -- The metric exists but was never sampled (F12); this is the single measurement that converts H1 from medium to high confidence and separates it from H4 (storage/node fault). [F12]
- **[short_term]** Once writes are restored, triage the accumulated write backlog and reconcile ledger entries for the 15:00-17:00Z failure window across payment-service and order-service so no partially written transactions are left inconsistent. -- Writes failed hard from 15:00Z onward in at least two services (F8, F14); disk-space failures of this type can leave partial or missing ledger rows. [F8, F14]
- **[short_term]** Confirm blast radius with request-rate metrics for portfolio-service and settlement-service (their zero ERROR counts may reflect no traffic, not health), and sweep api-gateway and any other ledger-db consumer for coincident error-rate elevation. -- F17 shows zero errors for these services but provides no independent proof they had write traffic; the true set of affected writers is unverified. [F17, F5]
- **[long_term]** Add capacity and early-warning alerting on ledger-db disk: alert on disk_used_pct thresholds (e.g. warn at 75%/85%) well before pg_wal cannot be extended, and size the volume/WAL retention so accumulated writes plus a release's extra ledger traffic cannot exhaust it. -- The failure surfaced only as 'No space left on device' at 15:00Z (F8, F9) and no pre-fill warning was observed in the retrieved logs; the shared datastore serves four services (F5), so exhaustion has a wide blast radius. [F5, F8, F9, F17]
- **[long_term]** Instrument ledger writes by entry type so the next occurrence can be localized immediately, and record deploy markers/migration history for ledger-db so schema-vs-capacity questions are answerable from telemetry. -- The error message names no entry type (F15), entry-type log searches returned nothing, and no deploy marker or migration history was retrievable — these gaps are what kept the diagnosis indirect. [F15, F3]

## Data gaps
- ledger-db disk_used_pct / slow_queries_per_min were never sampled (only available_metrics was retrieved, F12): the exact time disk hit 100% is unknown, and disk-fill onset rests on log first-seen times alone.
- No ledger-db migration/schema history was verified; F3's zero-config-change result covers recorded changes only, so an unrecorded enum/CHECK-constraint addition for MANDATE_HOLD cannot be ruled out from telemetry alone.
- Two planned historical-incident searches (new-entry-type/enum write rejection; payment-service release rollback precedents) were never executed (budget_exhausted); only 1 of 3 precedent searches completed (F6).
- No systematic sweep of other services for coincident elevated error_rate/latency was completed; blast radius beyond payment-service + ledger-db is unverified.
- The v6.0.0 deploy timestamp was not independently retrieved from a deploy marker, so the '~15 min after release' report could not be reconciled with telemetry beyond the 14:46Z record in F1.
- Log retrieval was truncated (payment-service 40/354; ledger-db 20/292) and limited to ERROR level, so WARN/INFO context before 15:00Z (e.g. earlier disk warnings) was not inspected.
- payment-service INFO-level logs could not be retrieved (searches for 'ledger entry', 'mandate', 'PAYMENT' returned 0), so failures are not split by ledger entry type directly; the entry-type-confinement question is answered only indirectly via order-service (F14).
- No request-rate metric was checked for portfolio-service/settlement-service, so their zero ERROR counts may mean 'no traffic' rather than 'healthy'.

## What would change this conclusion
- A ledger-db disk_used_pct timeline showing the volume was never near full around 15:00Z — this would break H1 and favor H4 (storage/node/volume fault).
- Evidence of a ledger-db node or volume fault (storage-layer errors, volume detach, IO errors, or host-level disk failure) coincident with 15:00Z rather than write-growth fill.
- A confirmed schema migration or enum/CHECK-constraint change adding MANDATE_HOLD to ledger-db shortly before 15:00Z, plus an entry-type-specific rejection error — this would revive H3/H2.
- Log or trace evidence showing the failure is confined to the mandate path while legacy ledger writes to the same datastore continue to succeed in the same window.
- Evidence that order-service's 'INSERT INTO orders failed' errors are unrelated to the ledger-db disk state (e.g. a separate failure domain), which would remove the strongest rejection of H2.
- Independent retrieval of the deploy marker showing onset coincided with the v6.0.0 deploy rather than occurring ~14 minutes later.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: First-error ordering across components is payment-service 15:00:26Z -> order-ser
- fact demoted to assumption: ledger-db exposes disk_used_pct and slow_queries_per_min metrics but no error_ra

## Investigation stats
- latency 98.83s, LLM calls 12, tokens 76143 (in 61039 / out 15104)
- tool calls 24 (3 failed, 0 partial, 0 recovered by retry), findings 17/17 verified
