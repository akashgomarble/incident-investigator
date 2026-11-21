# Incident investigation report: INC-012
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Payment failures were a SYMPTOM, not the fault. The shared ledger-db ran out of disk because the inactive replication slot 'analytics_cdc' retained tens of GB of WAL; disk_used_pct grew from 84% (12:00) to 100% (~14:49) and PostgreSQL PANICked writing files at 15:01. This broke all ledger-db writers simultaneously — payment-service ('ledger write failed: ... No space left on device', error_rate 0.23%->61% at 15:00) AND order-service ('INSERT INTO orders failed: No space left on device', 15:00:42), which was untouched by any deploy. The payment-service v6.0.0 deploy at 14:46 is coincidental: disk growth began hours earlier, and the 15:12 rollback did not recover the error rate (still ~61% at 16:50). CPU (~30%), slow queries (~1/min) and connections (~62) were flat; payment-service restarts were 0. Root cause: resource_exhaustion on ledger-db (inactive replication slot retaining WAL), matching historical incident HIST-114. The incident report's guess (bad_deployment) is refuted by timing, scope and the no-recovery rollback.

## Probable root cause
**ledger-db disk exhaustion from inactive analytics_cdc replication slot retaining WAL** -- resource_exhaustion in `ledger-db` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk exhaustion from inactive analytics_cdc replication slot retaining WAL | resource_exhaustion | ledger-db | - | 0.90 (high) | active |
| 2 | H2: payment-service v6.0.0 deploy (new MANDATE_HOLD ledger entry type) caused the failures | bad_deployment | payment-service | dep-6203 | 0.05 (low) | rejected |
| 3 | H3: ledger-db CPU or query-load saturation from a new heavy query | capacity_saturation | ledger-db | - | 0.03 (low) | rejected |

### H1: ledger-db disk exhaustion from inactive analytics_cdc replication slot retaining WAL
The shared ledger-db ran out of disk because the inactive replication slot 'analytics_cdc' retained tens of GB of WAL, growing disk from 84% (12:00) to 100% (~14:49). Once full, PostgreSQL could not write WAL/data files, so every writer (payment-service ledger writes, order-service order inserts, etc.) failed with 'No space left on device'. The fault ORIGINATES in ledger-db / its replication-slot configuration, not in payment-service.

**Causal chain**
1. analytics_cdc replication slot went inactive and stopped advancing the confirmed WAL position [F7]
2. slot retained WAL, growing disk_used_pct from 84% to 100% over ~3h [F6, F7, F10]
3. disk full -> PostgreSQL PANIC writing pg_wal xlogtemp files [F8]
4. all ledger-db writers fail with 'No space left on device' (payment ledger writes, order inserts) [F1, F9]
5. payment-service error_rate jumps to ~61% at 15:00 and stays there [F2, F15]

**Supporting evidence**
  - `F1` ✔ payment-service logged 354 ERROR entries 'ledger write failed: ERROR: could not extend file: No space left on device', first seen 2026-06-01T15:00:26Z.
  - `F2` ✔ payment-service error_rate jumped from ~0.23% to 61.35% in the 15:00 bucket and stayed ~61-63% through 16:50, i.e. onset at 15:00, not at the 14:46 deploy.
  - `F6` ✔ ledger-db disk_used_pct rose monotonically from 84.03% at 12:00 to 99.68% at 14:49 and was pinned at 100% from 15:02 onward.
  - `F7` ✔ ledger-db logged 'replication slot 'analytics_cdc' is inactive; retaining <N> MB of WAL' repeatedly from 12:19:34 onward, with retentions of tens of GB (e.g. 63157 MB, 92599 MB).
  - `F8` ✔ ledger-db logged disk-usage warnings: 'disk usage 90%' at 13:10:01, 'disk usage 95%' at 14:15:45, 'disk usage 98%' at 14:48:20, then PANIC 'could not write to file "pg_wal/xlogtemp.37478": No space left on device' at 15:01:01.
  - `F9` ✔ order-service (trading team, not touched by payment-service's deploy) logged 236 ERROR entries 'INSERT INTO orders failed: No space left on device', first seen 2026-06-01T15:00:42Z — the identical DB error at the same onset time.
  - `F10` ✔ ledger-db replication_lag_ms climbed steadily from ~1712 ms at 12:00 to ~5213 ms at 16:59.
  - `F11` ✔ ledger-db cpu_pct stayed flat ~30% (min 25.73, max 33.86) and slow_queries_per_min stayed flat ~1/min across the whole window.
  - `F12` ✔ ledger-db connections_active stayed flat ~62 (min 54.85, max 68.34) — no connection-pool exhaustion.
  - `F14` ✔ Historical post-mortem HIST-114 (2025-04-18) describes the same ledger-db failure: 'no space left on device', root cause 'Inactive replication slot retained WAL until disk filled', category resource_exhaustion.
  - `F16` ✔ Interpretation: the disk growth started at least 3h before the 14:46 deploy and the fault is confined to the shared ledger-db, so the fault originates in ledger-db (inactive analytics_cdc replication slot) and the payment-service deploy is coincidental, not causal.
**Contradicting evidence**
  - `F3` ✔ Only one deployment in the window: payment-service v6.0.0 at 2026-06-01T14:46:00Z, rolled_back at 2026-06-01T15:12:00Z.
  - `F15` ✔ payment-service error rate remained ~61-63% well after the v6.0.0 rollback at 15:12 (e.g. 16:50 bucket avg 61.63), so the rollback did not restore service.
**Assumptions**
  - The analytics_cdc slot was left in place by an analytics/CDC consumer that stopped or was decommissioned
  - disk_used_pct reflects /var/lib/postgresql/data volume

### H2: payment-service v6.0.0 deploy (new MANDATE_HOLD ledger entry type) caused the failures
The new mandate flow / new ledger entry type in v6.0.0 introduced a fault that broke ledger writes, producing the payment-service error spike.

**Causal chain**
(none given)

**Supporting evidence**
  - `F3` ✔ Only one deployment in the window: payment-service v6.0.0 at 2026-06-01T14:46:00Z, rolled_back at 2026-06-01T15:12:00Z.
**Contradicting evidence**
  - `F2` ✔ payment-service error_rate jumped from ~0.23% to 61.35% in the 15:00 bucket and stayed ~61-63% through 16:50, i.e. onset at 15:00, not at the 14:46 deploy.
  - `F6` ✔ ledger-db disk_used_pct rose monotonically from 84.03% at 12:00 to 99.68% at 14:49 and was pinned at 100% from 15:02 onward.
  - `F9` ✔ order-service (trading team, not touched by payment-service's deploy) logged 236 ERROR entries 'INSERT INTO orders failed: No space left on device', first seen 2026-06-01T15:00:42Z — the identical DB error at the same onset time.
  - `F15` ✔ payment-service error rate remained ~61-63% well after the v6.0.0 rollback at 15:12 (e.g. 16:50 bucket avg 61.63), so the rollback did not restore service.
  - `F16` ✔ Interpretation: the disk growth started at least 3h before the 14:46 deploy and the fault is confined to the shared ledger-db, so the fault originates in ledger-db (inactive analytics_cdc replication slot) and the payment-service deploy is coincidental, not causal.
**Rejected because:** Timing and scope contradict it: disk on the shared ledger-db was already 84% three hours before the deploy and hit 100% ~before the deploy; the identical error hit order-service (untouched by the deploy) at the same 15:00 onset; error rate did not recover after rollback (still ~61% at 16:50); errors are disk-space errors, not entry-type/schema errors.

### H3: ledger-db CPU or query-load saturation from a new heavy query
A new/report query saturated ledger-db CPU or caused slow queries, degrading all writers.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ ledger-db cpu_pct stayed flat ~30% (min 25.73, max 33.86) and slow_queries_per_min stayed flat ~1/min across the whole window.
  - `F8` ✔ ledger-db logged disk-usage warnings: 'disk usage 90%' at 13:10:01, 'disk usage 95%' at 14:15:45, 'disk usage 98%' at 14:48:20, then PANIC 'could not write to file "pg_wal/xlogtemp.37478": No space left on device' at 15:01:01.
**Rejected because:** ledger-db cpu_pct stayed flat ~30% (25.73-33.86) and slow_queries_per_min flat ~1/min throughout; the only resource that moved was disk, and the errors are explicitly disk-space errors.

## Facts (verified observations)
- ledger-db disk filled from 84% (12:00) to 100% (~14:49), hours before the 14:46 deploy. [F6, F3]
- ledger-db logs show an inactive replication slot 'analytics_cdc' retaining tens of GB of WAL from 12:19 onward. [F7]
- ledger-db PANICked at 15:01 writing pg_wal files due to no space left on device. [F8]
- Both payment-service and order-service hit identical 'No space left on device' errors at ~15:00; order-service was unaffected by the deploy. [F1, F9]
- payment-service error_rate onset was 15:00 (0.23%->61.35%), later than the 14:46 deploy. [F2, F3]
- The 15:12 rollback of v6.0.0 did not restore service; error rate ~61% at 16:50. [F15, F3]
- ledger-db CPU, slow-query and connection metrics stayed flat; the only moving resource was disk. [F11, F12]
- No config/feature-flag changes occurred in the window. [F4]
- HIST-114 (2025-04-18) documents the same ledger-db failure mode: inactive replication slot retaining WAL filling disk. [F14]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-06-01T12:00:00Z` ledger-db disk already at 84%; replication_lag ~1.7s [F6, F10]
- `2026-06-01T12:19:34Z` First ledger-db warning: replication slot 'analytics_cdc' inactive, retaining 63157 MB of WAL [F7]
- `2026-06-01T13:10:01Z` ledger-db disk usage 90% [F8]
- `2026-06-01T14:15:45Z` ledger-db disk usage 95% [F8]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 deployed (dep-6203) [F3]
- `2026-06-01T14:48:20Z` ledger-db disk usage 98% [F8]
- `2026-06-01T14:49:00Z` ledger-db disk reaches 100% [F6]
- `2026-06-01T15:00:26Z` First payment-service 'ledger write failed: No space left on device' [F1]
- `2026-06-01T15:00:42Z` First order-service 'INSERT INTO orders failed: No space left on device' [F9]
- `2026-06-01T15:01:01Z` ledger-db PANIC writing pg_wal/xlogtemp: No space left on device [F8]
- `2026-06-01T15:12:00Z` v6.0.0 rolled back; error rate unchanged (~61%) [F3, F15]
- `2026-06-01T16:59:00Z` Ledger-db still 100% disk, replication_lag ~5.2s, payment error rate ~61% [F6, F10, F15]

## Recommended actions
- **[immediate]** Free space on ledger-db immediately: drop/disable the inactive 'analytics_cdc' replication slot (after confirming the CDC consumer is truly decommissioned) to release retained WAL. -- Releases tens of GB of retained WAL and restores writes; matches HIST-114 resolution 'Dropped slot'. [F7, F8, F14]
- **[immediate]** After freeing space, VACUUM/checkpoint so PG recovers; verify disk drops and payment/order write success rates recover. -- Confirms the causal chain and service restoration; rollback of v6.0.0 can then be un-done if desired. [F1, F2, F9]
- **[short_term]** Add monitoring/alerting on replication-slot inactive time and retained WAL size, and on ledger-db disk growth rate (proactive warning at 80%). -- HIST-114 resolution explicitly added a slot-lag alert; disk reached 84% unnoticed for hours. [F6, F7, F14]
- **[short_term]** Do NOT treat this as a v6.0.0 code fault; keep the rollback only if desired but note it did not fix the outage. -- Rollback at 15:12 did not recover error rate; misattributing to the deploy delays real remediation. [F15, F9]
- **[long_term]** Automate disk autogrow / WAL-archive alerts and cap WAL retention for unused slots; runbook step to identify long-inactive slots on disk-pressure alerts. -- Prevents recurrence of a shared-datastore outage that took down multiple payment/order flows. [F7, F8]

## Data gaps
- No ledger-db metrics/logs were returned for a disk-usage trend reaching 84% before 12:00 — how long the disk had been growing and when analytics_cdc last advanced is unknown.
- Identity/owner of the analytics_cdc consumer and whether it was decommissioned (the reason the slot went inactive) is not visible in the data.
- No explicit confirmation of the disk volume size / where the retained-WAL MB values sum to, to quantify free-space needed.
- settlement-service and portfolio-service logs show almost no error activity, so their actual impact from the ledger-db outage is unverified (portfolio had only 1 generic retry warning).

## Investigation stats
- latency 54.93s, LLM calls 6, tokens 72483 (in 64410 / out 8073)
- tool calls 19 (1 failed, 0 partial, 0 recovered by retry), findings 16/16 verified
