# Incident investigation report: INC-012
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Payment failures ("ledger write failed: No space left on device") originate in the shared ledger-db, not in payment-service v6.0.0. ledger-db disk climbed steadily from 77.67% at 11:00 to 100% by ~14:54 — well before and independent of the 14:46 deploy — driven by an inactive replication slot 'analytics_cdc' retaining tens of GB of WAL. payment-service errors began at 15:00:26 and, critically, continued unchanged (320 more errors through 16:57) AFTER the v6.0.0 rollback at 15:12, so the rollback did not restore service. The deploy came 14 minutes before onset and is correlational, not causal. Error rate remains ~60% at the latest data.

## Probable root cause
**ledger-db disk exhaustion from inactive replication slot retaining WAL** -- resource_exhaustion in `ledger-db` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk exhaustion from inactive replication slot retaining WAL | resource_exhaustion | ledger-db | - | 0.90 (high) | active |
| 2 | H2: payment-service v6.0.0 mandate-flow release caused the ledger failures | bad_deployment | payment-service | dep-6203 | 0.05 (low) | rejected |
| 3 | H3: bank-partner-api dependency failure | dependency_failure | bank-partner-api | - | 0.02 (low) | rejected |
| 4 | H4: Config/feature-flag change | config_change | - | - | 0.02 (low) | rejected |

### H1: ledger-db disk exhaustion from inactive replication slot retaining WAL
An inactive replication slot 'analytics_cdc' on the shared ledger-db retained tens of GB of WAL, filling the data volume to 100% well before the deploy. Once full, PostgreSQL could not write WAL or extend relation files, so every payment-service ledger write failed with 'No space left on device', producing the ~60% payment error rate. The fault originates in ledger-db/infrastructure capacity, not in the application release.

**Causal chain**
1. Inactive replication slot analytics_cdc stops advancing, so PostgreSQL retains WAL indefinitely [F4]
2. Retained WAL + normal data growth fill the data volume; disk_used_pct climbs from 77% to 100% between 11:00 and 14:54 [F3, F13]
3. At 100% disk, PostgreSQL cannot write WAL or extend relation files ('No space left on device' PANICs and errors) [F5, F8]
4. payment-service ledger writes fail, error rate jumps to ~60% at 14:56-15:00 [F1, F2]

**Supporting evidence**
  - `F3` ✔ ledger-db disk_used_pct rose monotonically from 77.67% at 11:00 to 100% by the 14:54 bucket and remained 100% through 16:51.
  - `F4` ✔ ledger-db logged repeated 'replication slot analytics_cdc is inactive; retaining ... MB of WAL' warnings from 12:19:34Z onward, with values up to 92599 MB.
  - `F5` ✔ ledger-db produced PANIC/ERROR 'No space left on device' on pg_wal/xlogtemp and base files starting at 15:01:01Z.
  - `F13` ✔ The ledger-db disk was already above 90% at 13:10:01Z, over 90 minutes before the v6.0.0 deploy at 14:46Z.
  - `F9` ✔ ledger-db replication_lag_ms climbed from ~926ms at 11:00 to ~5222ms at 16:51 across the whole window.
  - `F12` ✔ Historical incident HIST-114 (2025-04-18) had the same signature — 'ledger-db disk full from retained WAL' caused by an inactive replication slot — categorized resource_exhaustion.
  - `F1` ✔ payment-service emitted 354 ERRORs 'ledger write failed: ERROR: could not extend file: No space left on device', first at 2026-06-01T15:00:26Z.
  - `F8` ✔ ledger-db errors persisted at full rate in the last hour (16:01-16:57), 149 ERRORs including 'No space left on device'.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 'analytics_cdc' slot belongs to a CDC consumer that stopped/never started, so the slot is the source of the WAL growth (the consumer service is not in the telemetry roster).
  - No other workload wrote enough to fill the remaining ~22% of disk in the pre-deploy window (consistent with the steady linear climb).

### H2: payment-service v6.0.0 mandate-flow release caused the ledger failures
The new UPI mandate flow in v6.0.0 introduced a new MANDATE_HOLD ledger entry type that writes incorrectly to ledger-db, causing ledger write failures and the 60% payment error rate.

**Causal chain**
(none given)

**Supporting evidence**
  - `F6` ✔ The only deployment in the window is payment-service v6.0.0 at 2026-06-01T14:46:00Z (new UPI mandate flow, new MANDATE_HOLD ledger entry type), rolled back at 15:12:00Z.
**Contradicting evidence**
  - `F3` ✔ ledger-db disk_used_pct rose monotonically from 77.67% at 11:00 to 100% by the 14:54 bucket and remained 100% through 16:51.
  - `F7` ✔ After the v6.0.0 rollback at 15:12:00Z, payment-service continued to emit 320 identical 'No space left on device' ledger write failures from 15:12:08Z through 16:57:54Z.
  - `F13` ✔ The ledger-db disk was already above 90% at 13:10:01Z, over 90 minutes before the v6.0.0 deploy at 14:46Z.
  - `F1` ✔ payment-service emitted 354 ERRORs 'ledger write failed: ERROR: could not extend file: No space left on device', first at 2026-06-01T15:00:26Z.
**Rejected because:** Contradicted on timing and recovery: ledger-db disk was already >90% at 13:10, ~96 minutes before the 14:46 deploy, and the error is a DB-level 'No space left on device', not a schema/entry-type error. Decisively, rolling back v6.0.0 at 15:12 did NOT recover service — payment-service emitted 320 identical disk-space errors through 16:57. A release-caused fault would clear on rollback.

### H3: bank-partner-api dependency failure
The external bank partner API degraded, causing payment failures.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ bank_api_error_rate for payment-service stayed flat at ~0.2% throughout, ruling out the external bank partner as the failing dependency.
  - `F1` ✔ payment-service emitted 354 ERRORs 'ledger write failed: ERROR: could not extend file: No space left on device', first at 2026-06-01T15:00:26Z.
**Rejected because:** bank_api_error_rate stayed flat at ~0.2% throughout, and the actual error text is a database disk-space error, not an API/network error.

### H4: Config/feature-flag change
A configuration change altered ledger write behavior.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ No configuration or feature-flag changes occurred in the window across all services.
**Rejected because:** get_config_changes returned zero changes for all services in the window.

## Facts (verified observations)
- payment-service ledger write failures are 'could not extend file: No space left on device' errors. [F1]
- payment-service error rate jumped from ~0.2% to ~60% starting at the 14:56 bucket. [F2]
- ledger-db disk_used_pct climbed from 77.67% (11:00) to 100% (14:54) and stayed at 100%. [F3, F13]
- ledger-db logged an inactive replication slot 'analytics_cdc' retaining up to 92599 MB of WAL repeatedly from 12:19. [F4]
- ledger-db began emitting 'No space left on device' PANICs/ERRORs at 15:01 and these continued through 16:57. [F5, F8]
- The only deployment was payment-service v6.0.0 at 14:46, rolled back at 15:12. [F6]
- payment-service errors continued after the rollback: 320 disk-space errors from 15:12:08 to 16:57:54. [F7]
- bank_api_error_rate remained ~0.2% throughout. [F10]
- No config or feature-flag changes occurred in the window. [F11]
- A prior incident (HIST-114) had the same ledger-db-WAL-retention signature, categorized resource_exhaustion. [F12]

## Assumptions (not verified)
- The 'analytics_cdc' replication slot is owned by a CDC/analytics consumer that stopped, and that consumer is the source of the retained WAL.
- The deploy of v6.0.0 at 14:46 did not itself contribute meaningful ledger write volume that accelerated the disk fill.

## Timeline
- `2026-06-01T11:00:00Z` ledger-db disk already at 77.67% and rising ~1%/8min; replication lag ~926ms [F3, F9]
- `2026-06-01T12:19:34Z` First 'replication slot analytics_cdc is inactive; retaining 63157 MB of WAL' warning on ledger-db [F4]
- `2026-06-01T13:10:01Z` ledger-db 'disk usage 90%' warning [F13]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (new UPI mandate flow) deployed [F6]
- `2026-06-01T14:48:20Z` ledger-db 'disk usage 98%' warning [F3]
- `2026-06-01T14:54:00Z` ledger-db disk reaches 100% [F3]
- `2026-06-01T15:00:26Z` First payment-service 'ledger write failed: No space left on device' error; error rate ~60% [F1, F2]
- `2026-06-01T15:01:01Z` First ledger-db 'PANIC ... No space left on device' on pg_wal [F5]
- `2026-06-01T15:10:00Z` Incident INC-012 reported (SEV-1) []
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rolled back [F6]
- `2026-06-01T15:12:08Z` payment-service continues emitting identical disk-space ledger failures after rollback [F7]
- `2026-06-01T16:57:54Z` Latest data: payment errors and ledger-db PANICs still ongoing [F7, F8]

## Recommended actions
- **[immediate]** Free disk on ledger-db immediately: drop the inactive replication slot 'analytics_cdc' (after confirming the CDC consumer is dead) to release retained WAL. -- The inactive slot is retaining tens of GB of WAL and the volume is pinned at 100%, the direct cause of all write failures. [F4, F3, F5]
- **[immediate]** Stop treating the payment-service rollback as the fix; it already happened and service did not recover. -- 320 errors continued after the 15:12 rollback, so the release is not the cause; effort should go to the DB. [F7]
- **[immediate]** Verify disk recovers and payment error rate returns to baseline after the slot is dropped. -- Confirms the causal chain and validates recovery. [F3, F2]
- **[short_term]** Add alerting on replication-slot lag/retained-WAL size and on ledger-db disk_used_pct (page at ~85%). -- The disk climbed for ~4 hours with only a soft warning; earlier paging would have prevented the SEV-1. Same gap existed in HIST-114. [F4, F13, F12]
- **[short_term]** Audit/restart the CDC consumer that owns the analytics_cdc slot and add a heartbeat so orphaned slots are detected. -- Root trigger is an inactive consumer letting its slot pin WAL. [F4]
- **[long_term]** Set a max_slot_wal_keep_size / retention cap and expand or monitor ledger-db volume headroom. -- Prevents a single orphaned slot from taking the shared ledger-db offline again. [F4, F12]

## Data gaps
- The CDC consumer service owning the 'analytics_cdc' slot is not in the telemetry roster ('analytics-cdc' is an unknown service), so why the slot became inactive could not be confirmed from logs/metrics.
- No ledger-db filesystem metrics beyond disk_used_pct (e.g., absolute GB free or WAL directory size) were available to quantify exactly how much the slot retention consumed.
- It is not directly proven from telemetry that ledger/payment writes are the only writer filling the disk in the pre-deploy window; the steady linear climb is consistent with the slot but not a direct measurement.

## Investigation stats
- latency 57.54s, LLM calls 6, tokens 88559 (in 77475 / out 11084)
- tool calls 14 (2 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
