# Incident investigation report: INC-012
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
Payments are broken because the shared postgres ledger-db ran out of disk space, not because of the v6.0.0 code change. The disk was already at 94.88% at 14:00Z — 46 minutes before dep-6203 deployed — ramped to 99.98% by 14:56Z and pinned at 100% from 15:04Z, and every payment-service error is a write-side failure with 'ERROR: could not extend file: No space left on device' (F9, F10, F15). Confidence in this root cause is medium (0.60, top-ranked and clearly ahead of the alternatives): the failure is pre-existing, shared-DB resource exhaustion whose historical twin (HIST-114, inactive replication slot retaining WAL) was fixed operationally, which is exactly why the 15:12Z rollback produced no recovery — error rate stayed flat at ~60% through 16:56Z (F7, F16, F18). v6.0.0's added MANDATE_HOLD write volume is at most an accelerator of an already-filling disk, and cannot be the origin of the failure.

## Probable root cause
**ledger-db disk exhausted by pre-existing growth (retained WAL), not by the release** -- resource_exhaustion in `ledger-db (shared postgres)`, triggered by `null` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H3: ledger-db disk exhausted by pre-existing growth (retained WAL), not by the release | resource_exhaustion | ledger-db (shared postgres) | null | 0.60 (medium) | active |
| 2 | H4: Self-contained regression in v6.0.0's ledger write path independent of disk state | bad_deployment | payment-service (v6.0.0 ledger write path) | dep-6203 | 0.12 (low) | active |
| 3 | H1: v6.0.0 MANDATE_HOLD entry type rejected by a constraint/validator downstream (deployment as trigger, shared component as fault) | bad_deployment | payment-service (v6.0.0 mandate flow) / ledger-db constraint | dep-6203 | 0.10 (low) | rejected |
| 4 | H2: Independent config/feature-flag change or ledger-db schema migration delivered the break, not the release | config_change | platform config / ledger-db schema owner | null | 0.08 (low) | rejected |

### H3: ledger-db disk exhausted by pre-existing growth (retained WAL), not by the release
The shared postgres ledger-db was already at 94.88% disk at 14:00Z — before any deployment — and its disk filled to 100% by 15:04Z, causing write-only failures ('could not extend file: No space left on device'). All services that write to ledger-db (payment-service, settlement-service, and likely order-/portfolio-service) fail simultaneously on writes only; v6.0.0's extra MANDATE_HOLD write volume is at most an accelerator of a pre-existing disk-fill condition, and rolling back the code cannot free the disk.

**Causal chain**
1. ledger-db disk is already 94.88% used at 14:00Z, 46 minutes before dep-6203, and ramps monotonically to 100% at 15:04Z (pre-existing growth, consistent with a retained/inactive replication slot holding WAL, per HIST-114) [F10, F15, F16]
2. Once the filesystem is full, postgres cannot extend relation files, so INSERT/UPDATE paths fail with 'could not extend file: No space left on device' [F9, F10]
3. The failure is write-only, so read paths keep working [F11]
4. Every service that writes to the SHARED ledger-db fails at once: payment-service and settlement-service both step to ~55-60% in the same 14:56Z bucket [F5, F7, F8, F12]
5. Rollback of dep-6203 at 15:12Z does not free disk, so the error rate stays flat ~60% through 16:56Z — a code rollback with no recovery [F7, F8, F18]

**Supporting evidence**
  - `F5` ✔ ledger-db is a SHARED postgres (owner platform, shared=true) written to by four services: order-service, portfolio-service, payment-service and settlement-service all have 'db' edges into ledger-db. A ledger-schema/entry-type or constraint problem here could affect all four, and any of them could affect payment-service through the same DB. kafka is also shared (owner platform, shared=true) but returned no edges for depth-2 downstream traversal.
  - `F7` ✔ payment-service error_rate shows a step change: ~0.2% at baseline (14:00–14:48Z, e.g. 0.21 at 14:00Z, 0.24 at 14:48Z), then 30.17 in the 14:56Z bucket, then flat ~60-63% (61.21 at 15:04Z ... 60.5 at 16:56Z) with no recovery through 16:56Z.
  - `F8` ✔ settlement-service error_rate shows the SAME step at the same time: ~0.2% baseline (0.17 at 14:48Z), then 27.14 in the 14:56Z bucket, then flat ~54-56% through 16:56Z (55.28 at 16:56Z). The high error rate is therefore not localized to payment-service.
  - `F9` ✔ First (and only) payment-service ERROR signature from 15:00:26Z is a DB write failure: 'ledger write failed: ERROR: could not extend file: No space left on device (payment e3489438)'; the same pattern repeats 354 times, first_seen 15:00:26Z, last 16:57:54Z. No HTTP/gRPC/exception-type signatures appear in the error logs.
  - `F10` ✔ ledger-db disk_used_pct climbs steadily from 94.88% (14:00Z) to 99.98% (14:56Z) and pins at 100.0 from 15:04Z through 16:56Z - consistent with the 'No space left on device' write failures; the ledger store does not expose an error_rate metric (only connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min).
  - `F11` ✔ The failures are writes-only: every payment-service error names a ledger write ('ledger write failed'); no read-path failure signature appears in the error/warn logs (only unrelated GC-pause and TLS-cipher WARNs).
  - `F12` ✔ Onset is a step change at a single interval, not a gradual ramp: both payment-service (30.17) and settlement-service (27.14) show their first elevated bucket at exactly 14:56Z after 0.2%-level baselines, i.e. the jump is simultaneous across services.
  - `F15` ✔ Timing against Task a's onset: v6.0.0 deployed 14:46:00Z, elevated error buckets first appear at 14:56Z (~10 min later), ledger-db disk is already at 94.88% at 14:00Z (pre-deployment) and completes its ramp to 100% at 15:04Z — i.e. disk exhaustion is pre-existing and the DB write errors ('No space left on device' from 15:00:26Z) are resource, not schema, in nature.
  - `F16` ✔ Historical precedent for this exact symptom set ('No space left on device' + write failures) is HIST-114 (2025-04-18, ledger-db): root cause 'Inactive replication slot retained WAL until disk filled' (resource_exhaustion), resolved by dropping the slot — a known disk-fill failure mode, not a new-entry-type rejection.
  - `F18` ✔ Rollback of v6.0.0 at 15:12:00Z historically-aligns with rollback resolving bad-deployment incidents (e.g. HIST-111 'added index; query review gate'), but no past incident confirms rollback resolving a disk-full ledger failure — HIST-114's resolution was operational (dropped replication slot), and Task a reported error rate still flat ~60% at 16:56Z after the 15:12Z rollback.
**Contradicting evidence**
  - `F1` ✔ Exactly one deployment occurred across ALL services in the entire window from 12:00Z to 17:00Z: payment-service v6.0.0 (dep-6203), deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes 'mandate create/approve endpoints' and 'new ledger entry type MANDATE_HOLD', status rolled_back at 15:12:00Z. No other deployment within 30 min before it or at any other time.
  - `F3` ✔ The deployment is the only recorded change that touches ledger entry types: it adds 'new ledger entry type MANDATE_HOLD' bundled with 'mandate create/approve endpoints' in dep-6203. No separate deployment or config change for a shared ledger/serialization/validation library appears in the window.
  - `F14` ✔ The only change touching ledger entry types is the v6.0.0 bundle itself ('new ledger entry type MANDATE_HOLD' + 'mandate create/approve endpoints'); no separate shared-ledger/schema/config change appears in the window, so an independent earlier schema change that would reject MANDATE_HOLD is absent from the change record.
**Assumptions**
  - Error-log signatures are representative of the whole service (settlement-service returned 0 log matches despite a 54-56% metric error rate)
  - The disk ramp is driven by DB-internal retention (e.g. inactive replication slot retaining WAL) rather than purely by v6.0.0's added write volume
  - No log-recovery event was recorded in the un-queried post-16:00Z window that would show disk freed
  - The 14:56Z onset vs 14:46Z deploy gap is explained by error-rate aggregation latency rather than by the deploy being the trigger

### H4: Self-contained regression in v6.0.0's ledger write path independent of disk state
dep-6203 introduced a bug inside payment-service's ledger write path (new mandate endpoints / MANDATE_HOLD write logic) that makes writes fail on its own — e.g. unbounded write retries or a write-on-read fallback — and the disk exhaustion is a downstream consequence of that runaway write volume rather than the origin.

**Causal chain**
1. dep-6203 adds mandate create/approve endpoints and the MANDATE_HOLD ledger write path [F1, F3]
2. That code path amplifies ledger write volume (retries or duplicate writes) and/or fails writes on its own *(assumed)*
3. ledger-db disk fills and writes fail with 'No space left on device' [F9, F10]

**Supporting evidence**
  - `F1` ✔ Exactly one deployment occurred across ALL services in the entire window from 12:00Z to 17:00Z: payment-service v6.0.0 (dep-6203), deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes 'mandate create/approve endpoints' and 'new ledger entry type MANDATE_HOLD', status rolled_back at 15:12:00Z. No other deployment within 30 min before it or at any other time.
  - `F3` ✔ The deployment is the only recorded change that touches ledger entry types: it adds 'new ledger entry type MANDATE_HOLD' bundled with 'mandate create/approve endpoints' in dep-6203. No separate deployment or config change for a shared ledger/serialization/validation library appears in the window.
  - `F15` ✔ Timing against Task a's onset: v6.0.0 deployed 14:46:00Z, elevated error buckets first appear at 14:56Z (~10 min later), ledger-db disk is already at 94.88% at 14:00Z (pre-deployment) and completes its ramp to 100% at 15:04Z — i.e. disk exhaustion is pre-existing and the DB write errors ('No space left on device' from 15:00:26Z) are resource, not schema, in nature.
**Contradicting evidence**
  - `F8` ✔ settlement-service error_rate shows the SAME step at the same time: ~0.2% baseline (0.17 at 14:48Z), then 27.14 in the 14:56Z bucket, then flat ~54-56% through 16:56Z (55.28 at 16:56Z). The high error rate is therefore not localized to payment-service.
  - `F10` ✔ ledger-db disk_used_pct climbs steadily from 94.88% (14:00Z) to 99.98% (14:56Z) and pins at 100.0 from 15:04Z through 16:56Z - consistent with the 'No space left on device' write failures; the ledger store does not expose an error_rate metric (only connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min).
  - `F15` ✔ Timing against Task a's onset: v6.0.0 deployed 14:46:00Z, elevated error buckets first appear at 14:56Z (~10 min later), ledger-db disk is already at 94.88% at 14:00Z (pre-deployment) and completes its ramp to 100% at 15:04Z — i.e. disk exhaustion is pre-existing and the DB write errors ('No space left on device' from 15:00:26Z) are resource, not schema, in nature.
  - `F16` ✔ Historical precedent for this exact symptom set ('No space left on device' + write failures) is HIST-114 (2025-04-18, ledger-db): root cause 'Inactive replication slot retained WAL until disk filled' (resource_exhaustion), resolved by dropping the slot — a known disk-fill failure mode, not a new-entry-type rejection.
  - `F18` ✔ Rollback of v6.0.0 at 15:12:00Z historically-aligns with rollback resolving bad-deployment incidents (e.g. HIST-111 'added index; query review gate'), but no past incident confirms rollback resolving a disk-full ledger failure — HIST-114's resolution was operational (dropped replication slot), and Task a reported error rate still flat ~60% at 16:56Z after the 15:12Z rollback.
**Assumptions**
  - v6.0.0's new writes pushed ledger-db from 94.88% to 100% in ~20 minutes
  - The simultaneous settlement-service failure would require a shared upstream write amplification rather than its own independent path

### H1: v6.0.0 MANDATE_HOLD entry type rejected by a constraint/validator downstream (deployment as trigger, shared component as fault)
payment-service v6.0.0 introduced a new ledger entry type MANDATE_HOLD; a constraint, validator or unconverted consumer of ledger-db rejects that entry type, so every mandate-ledger write fails and the error rate steps to ~60%.

**Causal chain**
1. dep-6203 (payment-service v6.0.0) is deployed at 14:46Z, adding the MANDATE_HOLD ledger entry type [F1, F3]
2. MANDATE_HOLD entries are rejected at write time by a constraint/validator/unconverted consumer of ledger-db *(assumed)*
3. payment-service ledger writes fail -> error rate steps to ~60% [F7]

**Supporting evidence**
  - `F3` ✔ The deployment is the only recorded change that touches ledger entry types: it adds 'new ledger entry type MANDATE_HOLD' bundled with 'mandate create/approve endpoints' in dep-6203. No separate deployment or config change for a shared ledger/serialization/validation library appears in the window.
  - `F12` ✔ Onset is a step change at a single interval, not a gradual ramp: both payment-service (30.17) and settlement-service (27.14) show their first elevated bucket at exactly 14:56Z after 0.2%-level baselines, i.e. the jump is simultaneous across services.
  - `F14` ✔ The only change touching ledger entry types is the v6.0.0 bundle itself ('new ledger entry type MANDATE_HOLD' + 'mandate create/approve endpoints'); no separate shared-ledger/schema/config change appears in the window, so an independent earlier schema change that would reject MANDATE_HOLD is absent from the change record.
**Contradicting evidence**
  - `F8` ✔ settlement-service error_rate shows the SAME step at the same time: ~0.2% baseline (0.17 at 14:48Z), then 27.14 in the 14:56Z bucket, then flat ~54-56% through 16:56Z (55.28 at 16:56Z). The high error rate is therefore not localized to payment-service.
  - `F9` ✔ First (and only) payment-service ERROR signature from 15:00:26Z is a DB write failure: 'ledger write failed: ERROR: could not extend file: No space left on device (payment e3489438)'; the same pattern repeats 354 times, first_seen 15:00:26Z, last 16:57:54Z. No HTTP/gRPC/exception-type signatures appear in the error logs.
  - `F11` ✔ The failures are writes-only: every payment-service error names a ledger write ('ledger write failed'); no read-path failure signature appears in the error/warn logs (only unrelated GC-pause and TLS-cipher WARNs).
  - `F16` ✔ Historical precedent for this exact symptom set ('No space left on device' + write failures) is HIST-114 (2025-04-18, ledger-db): root cause 'Inactive replication slot retained WAL until disk filled' (resource_exhaustion), resolved by dropping the slot — a known disk-fill failure mode, not a new-entry-type rejection.
  - `F17` ✔ No historical incident in the searched set combines a 'new ledger entry type' with write failures; the near-symptom ledger-db precedents are HIST-111 (new report query full-table scans, bad_deployment) and HIST-110 (IOPS throttling), and payment-failure precedent HIST-105/HIST-103 were external exchange/bank partner outages — so precedent does not support a schema-rejection root cause.
**Assumptions**
  - A constraint/validator for entry types exists and rejects unknown types rather than accepting them
  - v6.0.0's MANDATE_HOLD writes would produce a constraint/type error message, which is not observed
**Rejected because:** Ruled out by the error mechanism and by scope/timing. The only payment-service error signature is a DB write failure with 'ERROR: could not extend file: No space left on device' (F9), which is a filesystem/extent-allocation error, not a constraint, check, type or deserialization rejection; no non-write (constraint/type) error signature exists at all (F11). Scope also contradicts it: settlement-service shows the identical simultaneous step (F8/F12) despite not receiving dep-6203, and the fault would not be localized to a single-service deployment. Finally the same symptom set was seen historically as a pre-existing disk-fill failure mode (HIST-114/F16) and no precedent combines a new entry type with write failures (F17).

### H2: Independent config/feature-flag change or ledger-db schema migration delivered the break, not the release
A change made alongside (or instead of) v6.0.0 — a feature flag, a config change, or a ledger-db schema/migration performed outside the deployment record — is the actual fault, and the timing coincidence with the release is misleading.

**Causal chain**
1. An unrecorded config/flag change or ledger-db migration alters validation/write behaviour *(assumed)*
2. Ledger writes begin failing at 14:56Z [F7, F8]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F1` ✔ Exactly one deployment occurred across ALL services in the entire window from 12:00Z to 17:00Z: payment-service v6.0.0 (dep-6203), deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes 'mandate create/approve endpoints' and 'new ledger entry type MANDATE_HOLD', status rolled_back at 15:12:00Z. No other deployment within 30 min before it or at any other time.
  - `F2` ✔ The 14:30Z-16:00Z change window contains no config or feature-flag changes at all: get_config_changes returned count 0 with an empty changelist for all services (partial=false). So the v6.0.0 deployment is the only recorded change in the window.
  - `F13` ✔ Across ALL services, the window 10:00Z-17:00Z contains exactly one deployment: payment-service v6.0.0 (dep-6203) at 2026-06-01T14:46:00Z, status rolled_back at 15:12:00Z; no deployment of any shared ledger/serialization/schema component exists before it.
  - `F14` ✔ The only change touching ledger entry types is the v6.0.0 bundle itself ('new ledger entry type MANDATE_HOLD' + 'mandate create/approve endpoints'); no separate shared-ledger/schema/config change appears in the window, so an independent earlier schema change that would reject MANDATE_HOLD is absent from the change record.
**Assumptions**
  - A config/schema change could exist outside the queried change APIs
**Rejected because:** Directly contradicted by change records: get_config_changes returned count 0 with an empty changelist for all services across the 14:30Z-16:00Z window (F2), and across all services only one deployment exists in 10:00Z-17:00Z (F1/F13). F14 explicitly finds no independent earlier schema change that would reject MANDATE_HOLD. The only surviving caveat is un-queried config coverage after 16:00Z, which cannot explain a 14:56Z onset.

## Facts (verified observations)
- ledger-db is a SHARED postgres (owner platform, shared=true) with 'db' edges from order-service, portfolio-service, payment-service and settlement-service — any single-service code fix cannot repair a fault here (F4, F5). [F4, F5]
- ledger-db disk_used_pct was already 94.88% at 14:00Z, i.e. before the 14:46Z deployment, climbed to 99.98% by 14:56Z and pinned at 100.0% from 15:04Z through 16:56Z (F10, F15). [F10]
- The sole payment-service ERROR signature is a DB write failure: 'ledger write failed: ERROR: could not extend file: No space left on device (payment e3489438)', first_seen 15:00:26Z, repeated 354 times through 16:57:54Z; no HTTP/gRPC/exception-type signatures appear (F9). [F9]
- Failures are writes-only: every payment-service error names a ledger write; no read-path failure signature exists in error/warn logs (F11). [F11]
- The error-rate step is not localized to payment-service: settlement-service shows the same simultaneous jump (0.17% at 14:48Z to 27.14 at 14:56Z, then flat ~54-56%), despite not receiving dep-6203 (F8, F12). [F8]
- Exactly one deployment exists across ALL services in the 12:00Z-17:00Z window: payment-service v6.0.0 (dep-6203) at 14:46:00Z, rolled back at 15:12:00Z; no deployment of any shared ledger/serialization/schema component exists at any other time (F1, F13). [F1, F13]
- No config or feature-flag changes occurred in 14:30Z-16:00Z: get_config_changes returned count 0, empty changelist, partial=false (F2). [F2]
- The only change touching ledger entry types is the v6.0.0 bundle itself ('new ledger entry type MANDATE_HOLD' + 'mandate create/approve endpoints'); no independent earlier schema change that would reject MANDATE_HOLD appears in the change record (F3, F14). [F3]
- Historical precedent with this exact symptom set ('No space left on device' + write failures) is HIST-114 (2025-04-18, ledger-db), root cause 'Inactive replication slot retained WAL until disk filled' (resource_exhaustion), resolved by dropping the slot — an operational fix, not a code change (F16). [F16]
- No historical incident combines a 'new ledger entry type' with write failures; near-symptom ledger-db precedents are a bad-deployment full-scan query (HIST-111) and IOPS throttling (HIST-110), and payment-failure precedents were external partner outages (F17). [F17]
- Despite rollback at 15:12:00Z, payment-service error_rate remained flat ~60% through 16:56Z (60.5 at 16:56Z), and no past incident confirms rollback resolving a disk-full ledger failure (F7, F18). [F7]

## Assumptions (not verified)
- Error-log signatures are representative of the whole service: settlement-service returned 0 log matches despite a 54-56% metric error rate, so its failure mechanism is inferred from metrics alone.
- The disk ramp is driven by DB-internal retention (e.g. an inactive replication slot retaining WAL) rather than purely by v6.0.0's added write volume — this is the leading mechanism (HIST-114 precedent) but was not directly observed on ledger-db.
- No log-recovery event occurred in the un-queried post-16:00Z window that would show disk being freed.
- The 14:56Z metric onset vs the 14:46Z deploy gap is explained by aggregation latency in the 480s bucket plus log first-occurrence at 15:00:26Z, rather than by the deploy being the causal trigger.
- F1/F13's 'exactly one deployment' statement is scoped to the 12:00Z-17:00Z window as recorded by the deployment API; an unrecorded out-of-band change cannot be formally excluded (though F2 shows no config changes in 14:30Z-16:00Z).
- (demoted from facts: no verified observation backs it) Onset is a single-interval step change, simultaneous across services, not a gradual ramp — payment-service 30.17 and settlement-service 27.14 both first elevated in the same 14:56Z bucket after ~0.2% baselines (F12).

## Timeline
- `2026-06-01T14:00:00Z` ledger-db disk_used_pct already 94.88% — before any deployment in the incident window; payment-service and settlement-service error rates at ~0.2% baseline (F7, F10, F15). [F7, F10, F15]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (dep-6203) deployed by p.nair — 'New UPI mandate flow', mandate create/approve endpoints, new ledger entry type MANDATE_HOLD. The only deployment across all services in 12:00Z-17:00Z (F1, F13). [F1, F13]
- `2026-06-01T14:56:00Z` Simultaneous error-rate step change across services: payment-service 30.17 and settlement-service 27.14, up from ~0.2% baselines; ledger-db disk reaches 99.98% (F7, F8, F10, F12, F15). [F7, F8, F10, F12, F15]
- `2026-06-01T15:00:26Z` First payment-service ERROR: 'ledger write failed: ERROR: could not extend file: No space left on device' (payment e3489438) (F9, F11). [F9, F11]
- `2026-06-01T15:04:00Z` ledger-db disk_used_pct pins at 100.0%; payment-service error rate flat ~61% (F7, F10). [F7, F10]
- `2026-06-01T15:12:00Z` v6.0.0 rolled back (dep-6203 status rolled_back). Rollback does not restore service: error rate stays flat ~60% (F1, F7, F18). [F1, F7, F18]
- `2026-06-01T16:00:00Z` Change-window audit confirms zero config/feature-flag changes for 14:30Z-16:00Z (count 0, partial=false) — the deployment is the only recorded change (F2). [F2]
- `2026-06-01T16:56:00Z` No recovery: payment-service 60.5%, settlement-service 55.28%, ledger-db disk still 100% (F7, F8, F10). [F7, F8, F10]
- `2026-06-01T16:57:54Z` Last occurrence of the 'No space left on device' ledger write error, 354 repetitions in total (F9). [F9]

## Recommended actions
- **[immediate]** Immediate: free space on ledger-db — connect to the shared postgres and identify/drop the inactive replication slot (or archive/purge retained WAL) that is pinning disk at 100%. This is the documented resolution of the HIST-114 precedent and is the only action that addresses a 100%-full shared volume. -- All evidence points to resource exhaustion at ledger-db: disk at 100% since 15:04Z, writes failing with 'could not extend file: No space left on device', and the identical historical incident resolved by dropping a replication slot. Rolling back code cannot free disk, which is why the 15:12Z rollback changed nothing. [F9, F10, F15, F16, F18]
- **[immediate]** Immediate: stop the bleed on all writers — confirm whether order-service and portfolio-service are also failing on ledger writes (query their error_rate / logs) and, if needed, shed or queue non-critical ledger writes across all four ledger-db clients until disk is freed. Do not treat this as a payment-service-only incident. -- ledger-db is shared by order-, portfolio-, payment- and settlement-service; settlement-service already shows the same simultaneous 54-56% failure without receiving dep-6203, so the blast radius is the shared DB, not one service. [F4, F5, F8, F12]
- **[immediate]** Immediate: keep v6.0.0 rolled back (already done at 15:12Z) but do NOT re-deploy until disk is freed, and do not expect re-deployment to fix the outage. Communicate to the incident channel that rollback is not the mitigation. -- The rollback was applied 1h47m ago and error rate is unchanged (~60% at 16:56Z), matching the pre-existing-disk-fill explanation rather than a bad-deployment explanation. [F7, F18]
- **[short_term]** Short_term: after space is freed, verify recovery end-to-end — ledger-db disk_used_pct back below threshold, disk space not re-filling (monitor over the next hour), payment-service and settlement-service error rates returning to ~0.2%, and no new 'No space left on device' errors; then re-land v6.0.0 with a mandate-write canary. -- Confirms the resource-exhaustion root cause and separates the pre-existing disk condition from any residual v6.0.0 behaviour before re-exposing the mandate flow. [F7, F8, F9, F10]
- **[short_term]** Short_term: establish whether the disk ramp is retention-driven or write-volume-driven — inspect ledger-db WAL retention / replication slots / autovacuum and any checkpointing settings, and (if possible) compare ledger write volume and slow_queries_per_min before vs after 14:46Z. If volume alone explains the ramp, H4 must be re-examined. -- Quantifying the ramp's driver is the one check that could distinguish H3 (retention-driven, deploy incidental) from H4 (v6.0.0 runaway write volume as the origin), and it also prevents the next disk-full incident. [F10, F15, F16]
- **[long_term]** Long_term: add disk-usage alerting and a hard headroom floor on ledger-db (page well before 90%, not 99.98%), plus automated detection of inactive replication slots / retained WAL on the shared postgres. -- The DB sat at 94.88% at 14:00Z with no action and reached 100% before anyone noticed; the failure mode is recurring (HIST-114) and detectable in advance. [F10, F15, F16]
- **[long_term]** Long_term: decouple release risk from shared-DB capacity — add per-release ledger write-volume budget/canary checks (MANDATE_HOLD write rate) and a pre-deploy gate that verifies ledger-db free space; document that any change adding a new ledger entry type requires a capacity review. -- v6.0.0 at minimum accelerated the disk ramp; a release-time write-volume and headroom gate would prevent a future deploy from tipping an already-tight shared volume over. [F1, F3, F14, F15]

## Data gaps
- No config-changes query was run for 16:00Z-16:59Z (get_config_changes was scoped to 14:30Z-16:00Z, count 0), so any config/flag change during or after the rollback period is unverified — it cannot explain a 14:56Z onset, but it limits confidence about the post-rollback state.
- No tool call listed changes inside ledger-db itself (schema migrations, constraint or entry-type additions independent of deployments); the only ledger-schema signal is wording inside dep-6203, so a separate DB-side migration cannot be formally denied.
- Per-service ledger write volume before vs after 14:46Z could not be measured (no ledger write QPS metric exists in the ledger store's metric set), so whether MANDATE_HOLD writes accelerated the disk ramp is unquantified — this is the key gap for distinguishing H3 from H4.
- order-service and portfolio-service error_rate were never queried (budget exhausted), so the true blast radius across the four ledger-db writers is unconfirmed; settlement-service's 54-56% is the only non-payment confirmation.
- ledger-db slow_queries_per_min / connections_active / replication_lag_ms were never queried (budget exhausted), so whether slow queries or connection saturation contributed alongside raw disk exhaustion is unknown, and the WAL-retention mechanism (HIST-114 analogue) is inferred rather than observed on this instance.
- settlement-service ERROR/WARN logs returned 0 matches despite its 54-56% error rate, so the failure signature is confirmed only for payment-service; log evidence inside the other writers is absent.
- The 14:56Z metric bucket is a coarse 480s aggregation, bounding the metric-derived onset to 14:48Z-14:56Z; the tightest onset evidence is the 15:00:26Z first ERROR log, and earlier non-ERROR signatures were not exhaustively checked.
- Historical precedent search was capped (6 results per query) and found no incident titled 'new entry type' or 'schema constraint'; absence of a matching precedent may be a coverage gap rather than true novelty.
- kafka (a shared component payment-service produces to) returned empty edges even at depth-2 traversal, so a shared serialization/queue component cannot be ruled in or out of the ledger write path.

## What would change this conclusion
- Evidence that ledger-db disk space was freed (e.g. via WAL/slot cleanup) without error rates recovering — this would falsify H3 and point back to a code/regression cause (H4) or a discriminating factor other than disk.
- Measurement showing payment-service (or MANDATE_HOLD) write volume alone accounts for the 94.88%->100% ramp and that no retention/WAL mechanism exists — this would move H4 ahead of H3.
- Discovering an independent ledger-db schema/migration/constraint change outside dep-6203 (e.g. a new entry-type constraint) that rejects MANDATE_HOLD — this would revive H1 (currently rejected: the observed error is an extent-allocation failure, not a constraint rejection, and settlement-service fails without dep-6203).
- Order-service and portfolio-service NOT failing on ledger writes while payment- and settlement-service fail simultaneously — that pattern would be hard to reconcile with a purely shared-DB capacity cause.
- A config or feature-flag change recorded after 16:00Z or found outside the queried change APIs (F2's count 0 was scoped to 14:30Z-16:00Z) that correlates with the onset would revive H2.
- A change-record artifact proving dep-6203 predates the disk ramp (e.g. an earlier v6.0.0 canary or MANDATE_HOLD writes starting before 14:46Z) would materially strengthen H4 by tying added writes to the ramp.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: Onset is a single-interval step change, simultaneous across services, not a grad

## Investigation stats
- latency 108.65s, LLM calls 12, tokens 73621 (in 57057 / out 16564)
- tool calls 20 (2 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
