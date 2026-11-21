# Incident investigation report: INC-012
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
payment-service error rate jumped from ~0.2% to ~61% at 15:00, with "ledger write failed: No space left on device". The fault does NOT originate in payment-service: the shared ledger-db (postgres) filled its disk from WAL retained by an inactive replication slot 'analytics_cdc'. Disk on ledger-db climbed monotonically 89% (13:00) → 100% (~14:45), before the 14:46 v6.0.0 deploy; the first write failures at 15:00 hit every service on ledger-db — payment-service AND order-service (a different team with no deploy) simultaneously. Rolling back v6.0.0 at 15:12 did not reduce the error rate (still ~61-63% through 16:59). Root cause is resource_exhaustion at ledger-db caused by the orphaned replication slot; the v6.0.0 deployment is coincidental timing.

## Probable root cause
**ledger-db disk exhaustion from orphaned replication slot (resource_exhaustion)** -- resource_exhaustion in `ledger-db` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk exhaustion from orphaned replication slot (resource_exhaustion) | resource_exhaustion | ledger-db | - | 0.90 (high) | active |
| 2 | H2: payment-service v6.0.0 introduced a bad ledger write path | bad_deployment | payment-service | dep-6203 | 0.05 (low) | rejected |

### H1: ledger-db disk exhaustion from orphaned replication slot (resource_exhaustion)
An inactive replication slot 'analytics_cdc' on the shared ledger-db retained WAL until the data volume reached 100% full; once full, all writers to ledger-db (payment-service, order-service, etc.) failed with 'No space left on device', surfacing as payment-service 61% error rate.

**Causal chain**
1. Orphaned replication slot 'analytics_cdc' accumulates WAL on ledger-db [F6, F12]
2. WAL accumulation drives disk_used_pct from 89% to 100% by ~14:45, before any deploy [F5]
3. Disk full causes all ledger-db writes to fail at 15:00 (PANIC / could not extend file) [F7]
4. Shared-datastore failure propagates to every caller: payment-service AND order-service fail simultaneously [F4, F8, F9]
5. Rollback of the unrelated payment deploy does not help because disk remains full [F10]

**Supporting evidence**
  - `F4` ✔ payment-service errors are all 'ledger write failed: ERROR: could not extend file: No space left on device', first seen 15:00:26.
  - `F5` ✔ ledger-db disk_used_pct rose monotonically from 89.18% at 13:00 to 100% by ~14:45 and stayed at 100% through 16:45 — i.e. the disk was already full before/at the 14:46 deploy.
  - `F6` ✔ ledger-db reported an inactive replication slot 'analytics_cdc' retaining tens of GB of WAL starting 13:02:25 (e.g. 92599 MB) and repeated warnings through 14:49:47.
  - `F7` ✔ ledger-db logged PANIC / could-not-extend-file 'No space left on device' errors (153 pg_wal/xlogtemp panics, 139 file extend errors) beginning 15:01:01.
  - `F8` ✔ order-service — a different team with no deployment in the window — also began failing at 15:00:42 with 'INSERT INTO orders failed: No space left on device' (236 errors).
  - `F9` ✔ ledger-db is a shared datastore used by order-service, portfolio-service, payment-service and settlement-service.
  - `F10` ✔ The rollback of v6.0.0 at 15:12 did not restore service: payment-service error_rate remained ~61-63% for the rest of the window (last bucket 16:45 avg 62.49).
  - `F12` ✔ ledger-db replication_lag_ms rose steadily from ~2468ms (13:00) to ~5226ms (16:45), indicating a lagging/unconsumed replication stream.
  - `F13` ✔ Historical incident HIST-114 (2025-04-18) on ledger-db had the identical pattern: 'Inactive replication slot retained WAL until disk filled', category resource_exhaustion.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 'analytics_cdc' slot is genuinely orphaned/inactive and not a required consumer
  - WAL retention is the dominant disk consumer (slot MBs are large and consistent with filling the volume)

### H2: payment-service v6.0.0 introduced a bad ledger write path
The new MANDATE_HOLD ledger entry type in v6.0.0 caused malformed/failed ledger writes, producing the error surge.

**Causal chain**
(none given)

**Supporting evidence**
  - `F1` ✔ payment-service error_rate was ~0.2% until 14:45, then jumped to 61.02% in the 15:00 bucket and stayed ~61-63% through 16:45.
  - `F2` ✔ The only change in the window was deployment dep-6203 of payment-service v6.0.0 at 14:46 ('New UPI mandate flow', new ledger entry type MANDATE_HOLD), rolled back at 15:12.
**Contradicting evidence**
  - `F5` ✔ ledger-db disk_used_pct rose monotonically from 89.18% at 13:00 to 100% by ~14:45 and stayed at 100% through 16:45 — i.e. the disk was already full before/at the 14:46 deploy.
  - `F7` ✔ ledger-db logged PANIC / could-not-extend-file 'No space left on device' errors (153 pg_wal/xlogtemp panics, 139 file extend errors) beginning 15:01:01.
  - `F8` ✔ order-service — a different team with no deployment in the window — also began failing at 15:00:42 with 'INSERT INTO orders failed: No space left on device' (236 errors).
  - `F9` ✔ ledger-db is a shared datastore used by order-service, portfolio-service, payment-service and settlement-service.
  - `F10` ✔ The rollback of v6.0.0 at 15:12 did not restore service: payment-service error_rate remained ~61-63% for the rest of the window (last bucket 16:45 avg 62.49).
  - `F12` ✔ ledger-db replication_lag_ms rose steadily from ~2468ms (13:00) to ~5226ms (16:45), indicating a lagging/unconsumed replication stream.
**Rejected because:** Errors are disk-full ('No space left on device'), not code/schema errors; disk was already 100% before the 14:46 deploy; the unrelated order-service failed identically at the same minute; the rollback at 15:12 did not reduce the error rate.

## Facts (verified observations)
- Error onset was 15:00; payment-service error_rate jumped from ~0.2% to ~61% at the 15:00 bucket. [F1]
- All payment-service errors were ledger writes failing with 'No space left on device' (first at 15:00:26). [F4]
- ledger-db disk usage reached 100% by ~14:45, before the 14:46 deploy, and stayed full. [F5]
- An inactive replication slot 'analytics_cdc' was retaining tens of GB of WAL since at least 13:02. [F6]
- ledger-db logged PANIC/file-extend 'No space left on device' errors from 15:01:01. [F7]
- order-service, an unrelated team with no deploy, failed with 'No space left on device' from 15:00:42. [F8]
- ledger-db is a shared DB used by order, portfolio, payment and settlement services. [F9]
- The 15:12 rollback of v6.0.0 did not reduce payment-service error rate. [F10]
- payment-service local memory and DB pool metrics were flat, ruling out local exhaustion. [F11]
- Historical incident HIST-114 matched this exact pattern (inactive slot WAL filling ledger-db disk). [F13]

## Assumptions (not verified)
- The 'analytics_cdc' replication slot is orphaned and safe to drop after confirming no active CDC consumer
- WAL retention from the slot is the dominant contributor to the disk growth
- No other disk consumer (e.g. table bloat) materially contributed

## Timeline
- `2026-06-01T13:02:25Z` ledger-db begins warning: inactive replication slot 'analytics_cdc' retaining WAL (33GB, peaking ~92GB) [F6]
- `2026-06-01T13:00:00Z` ledger-db disk_used_pct already 89.18% and climbing [F5]
- `2026-06-01T14:15:45Z` ledger-db logs 'disk usage 95%' warning [F6]
- `2026-06-01T14:45:00Z` ledger-db disk reaches 100% (before the deploy) [F5]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (dep-6203) deployed — coincidental, disk already full [F2, F5]
- `2026-06-01T15:00:26Z` payment-service first 'ledger write failed: No space left on device' [F4]
- `2026-06-01T15:00:42Z` order-service also fails with 'No space left on device' [F8]
- `2026-06-01T15:01:01Z` ledger-db PANIC writing pg_wal — disk exhausted [F7]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rolled back; error rate unchanged [F10]
- `2026-06-01T16:59:00Z` Error rate still ~62%; disk still 100% [F10, F5]

## Recommended actions
- **[immediate]** Free space on ledger-db: drop/invalidate the inactive 'analytics_cdc' replication slot and let WAL reclaim (after confirming no active consumer). -- The orphaned slot is retaining tens of GB of WAL, keeping the volume at 100% and blocking all writes. [F6, F5, F7]
- **[immediate]** Do NOT treat the v6.0.0 rollback as the fix; rollback is already shown not to help. Re-deploy or keep v6.0.0 as appropriate once disk is restored. -- Error rate did not change after the 15:12 rollback; the deploy is coincidental. [F10, F2]
- **[short_term]** Add alerting on replication-slot WAL retention / slot inactivity and on ledger-db disk_used_pct above threshold. -- HIST-114 resolution already called for a slot-lag alert; recurrence suggests it was not adequately covered. [F13]
- **[long_term]** Audit and cap replication slot WAL retention (max_slot_wal_keep_size) and add a watchdog that invalidates slots inactive beyond a limit. -- Prevents a single orphaned slot from filling the shared datastore again. [F6, F12]

## Data gaps
- Whether the 'analytics_cdc' slot has a legitimate active consumer cannot be confirmed from available telemetry (bank-partner-api and vendor systems have no telemetry; no slot-consumer visibility).
- portfolio-service and settlement-service produced no errors in the window despite sharing ledger-db — possibly write frequency or cache differences, not fully explained.
- No explicit post-fix recovery datapoint (disk still 100% at end of window); causal chain is confirmed by mechanism but the resolution has not been observed.

## Investigation stats
- latency 47.08s, LLM calls 7, tokens 91616 (in 84896 / out 6720)
- tool calls 22 (2 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
