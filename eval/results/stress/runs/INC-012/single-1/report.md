# Incident investigation report: INC-012
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Payment failures are caused by disk exhaustion on the SHARED ledger-db postgres, not by payment-service v6.0.0. ledger-db's disk grew from ~78% (11:00) to 100% (~15:02) because an inactive replication slot 'analytics_cdc' retained tens of GB of WAL; once full, all ledger-db writers failed with 'No space left on device', collapsing payment success from ~99% to ~35% at 15:00. The v6.0.0 deploy (14:46) and its rollback (15:12) are coincidental: errors persisted unchanged after rollback, and order-service (unrelated code) failed identically at 15:00:42. Remedy is to drop/fix the inactive replication slot and free disk, not to chase the release.

## Probable root cause
**Shared ledger-db disk exhaustion from inactive replication slot retaining WAL** -- resource_exhaustion in `ledger-db` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Shared ledger-db disk exhaustion from inactive replication slot retaining WAL | resource_exhaustion | ledger-db | - | 0.90 (high) | active |
| 2 | H2: payment-service v6.0.0 introduced a faulty ledger write path | bad_deployment | payment-service | dep-6203 | 0.08 (low) | rejected |

### H1: Shared ledger-db disk exhaustion from inactive replication slot retaining WAL
An inactive replication slot 'analytics_cdc' on the shared postgres ledger-db retained tens of GB of WAL, growing the data volume from ~78% at 11:00 to 100% by ~15:02. Once the disk was full, all writers to ledger-db (payment-service ledger writes, order-service inserts) failed with 'No space left on device', causing the observed ~61% payment error rate and low payment success rate. The payment-service v6.0.0 deploy/rollback is coincidental.

**Causal chain**
1. CDC consumer disconnects, leaving replication slot 'analytics_cdc' inactive [F5]
2. Postgres retains WAL for the stale slot (33-92 GB), growing the data volume toward full [F5, F4]
3. disk_used_pct reaches 100% at ~15:02 [F4, F6]
4. ledger-db write/extension fails with 'No space left on device' [F6, F3]
5. payment-service ledger writes fail -> ~61% error rate; order-service inserts also fail [F1, F2, F7]

**Supporting evidence**
  - `F1` ✔ payment-service error rate jumped from ~0.23% to ~61% at the 15:00 bucket and stayed elevated through 16:50 (last value 63.76%).
  - `F2` ✔ payment-service payment success rate collapsed from ~99% to ~35% at 15:00 and remained at ~35% through 16:45.
  - `F3` ✔ payment-service ERROR logs are dominated by ledger write failures 'No space left on device', first seen 15:00:26, 354 matches, continuing to 16:57:54.
  - `F4` ✔ ledger-db disk_used_pct rose monotonically from 78.23% at 11:00 to 100% at 15:02 and stayed pegged at 100% through 16:59.
  - `F5` ✔ ledger-db logs repeatedly report replication slot 'analytics_cdc' is inactive; retaining tens of GB of WAL, from 12:19 (63157 MB) up to 91901 MB, earliest seen 13:02, recurring until 14:49.
  - `F6` ✔ ledger-db emitted disk-usage warnings climbing 90% (13:10) -> 95% (14:15) -> 98% (14:48), then PANIC/ERROR 'No space left on device' writing pg_wal and base files from 15:01:01.
  - `F7` ✔ order-service, which does not run payment-service's code, also logs INSERT failures 'No space left on device' starting 15:00:42 (236 matches) — matching the ledger-db saturation window.
  - `F9` ✔ ledger-db is a shared postgres used by order-service, portfolio-service, payment-service and settlement-service.
  - `F10` ✔ Prior incident HIST-114 (2025-04-18) on ledger-db had the same pattern: 'Inactive replication slot retained WAL until disk filled', resolved by dropping the slot.
  - `F11` ✘ unverified payment-service error rate stayed ~61-64% well after the 15:12 rollback (e.g. avg 63.57 at 16:20), i.e. the rollback did not reduce errors.
  - `F12` ✔ Interpretation: the fault originates in the shared ledger-db storage, whose disk was exhausted by WAL retained by the inactive analytics_cdc replication slot; payment-service's v6.0.0 deploy is temporally near but causally unrelated.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 'analytics_cdc' slot is genuinely inactive (consumer/CDC pipeline down) and not merely slow
  - No other workload contributed materially to disk growth

### H2: payment-service v6.0.0 introduced a faulty ledger write path
The v6.0.0 mandate flow's new MANDATE_HOLD ledger entry type caused ledger write failures, producing the payment errors. This is the reporter's hypothesis.

**Causal chain**
(none given)

**Supporting evidence**
  - `F1` ✔ payment-service error rate jumped from ~0.23% to ~61% at the 15:00 bucket and stayed elevated through 16:50 (last value 63.76%).
  - `F8` ✔ Only one deployment in the window: payment-service v6.0.0 deployed 14:46 (new UPI mandate flow, ledger entry type MANDATE_HOLD), rolled back at 15:12; no config/flag changes occurred.
**Contradicting evidence**
  - `F3` ✔ payment-service ERROR logs are dominated by ledger write failures 'No space left on device', first seen 15:00:26, 354 matches, continuing to 16:57:54.
  - `F4` ✔ ledger-db disk_used_pct rose monotonically from 78.23% at 11:00 to 100% at 15:02 and stayed pegged at 100% through 16:59.
  - `F7` ✔ order-service, which does not run payment-service's code, also logs INSERT failures 'No space left on device' starting 15:00:42 (236 matches) — matching the ledger-db saturation window.
  - `F11` ✘ unverified payment-service error rate stayed ~61-64% well after the 15:12 rollback (e.g. avg 63.57 at 16:20), i.e. the rollback did not reduce errors.
  - `F6` ✔ ledger-db emitted disk-usage warnings climbing 90% (13:10) -> 95% (14:15) -> 98% (14:48), then PANIC/ERROR 'No space left on device' writing pg_wal and base files from 15:01:01.
**Rejected because:** Contradicted by: (a) error message is a storage-space error, not a schema/code error; (b) errors persisted at ~61-64% after the 15:12 rollback (F11); (c) unrelated order-service failed identically at 15:00:42 (F7); (d) disk was already 90%+ before the 14:46 deploy (F4).

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- The 'analytics_cdc' replication slot's consumer is genuinely offline/inactive rather than merely lagging.
- No other workload (e.g., an unindexed report query) contributed materially to the disk growth; the WAL retention is the dominant factor.
- The metric 'disk_used_pct' for ledger-db reflects the same filesystem (/var/lib/postgresql/data) referenced in the disk-usage warnings.

## Timeline
- `2026-06-01T11:00:00Z` ledger-db disk already at 78.23% and climbing [F4]
- `2026-06-01T12:19:34Z` ledger-db warns replication slot 'analytics_cdc' inactive, retaining 63157 MB WAL [F5]
- `2026-06-01T13:10:01Z` ledger-db disk usage warning 90% [F6]
- `2026-06-01T14:15:45Z` ledger-db disk usage warning 95% [F6]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 deployed (new UPI mandate flow) [F8]
- `2026-06-01T14:48:20Z` ledger-db disk usage warning 98% [F6]
- `2026-06-01T15:00:26Z` payment-service first 'No space left on device' ledger write errors; error rate jumps to 61% [F3, F1]
- `2026-06-01T15:00:42Z` order-service also begins failing INSERTs with 'No space left on device' [F7]
- `2026-06-01T15:01:01Z` ledger-db PANIC/could not write pg_wal and base files [F6]
- `2026-06-01T15:02:00Z` ledger-db disk reaches 100% [F4]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rolled back; error rate unchanged (~62%) afterwards [F8, F11]
- `2026-06-01T16:50:00Z` payment error rate still 61.63%, disk still 100% [F11, F4]

## Recommended actions
- **[immediate]** Drop the inactive replication slot 'analytics_cdc' (after confirming the CDC/analytics consumer is truly dead) and reclaim WAL to free ledger-db disk. -- This is the direct driver of disk growth; HIST-114 was resolved the same way. [F5, F6, F10]
- **[immediate]** Do NOT continue treating v6.0.0 as the culprit; verify recovery by monitoring disk_used_pct and payment error after slot removal. -- Rollback did not fix the symptom; further code churn wastes recovery time. [F11, F8]
- **[short_term]** Add a slot-lag/inactive-slot alert and a disk-capacity alert on ledger-db (and free space if needed via WAL archival). -- Detectting inactive slots earlier would have prevented a 4-hour runway to full disk. [F5, F10]
- **[long_term]** Audit monitor/reporting pipelines that own replication slots to keep them connected or auto-drop stale slots. -- Prevents recurrence of the same resource-exhaustion mechanism. [F5, F9]

## Data gaps
- ledger-db slow_queries_per_min and replication_lag_ms could not be retrieved (backend 503 after retries); these might corroborate the WAL/consumer status but are not essential to the conclusion.
- No telemetry for the analytics_cdc consumer service that owns the replication slot was found, so the reason the slot is inactive is unconfirmed.
- No logs available from portfolio-service or settlement-service in the window (0 matches), so impact on those consumers of ledger-db is unverified.

## Investigation stats
- latency 82.05s, LLM calls 6, tokens 69019 (in 61852 / out 7167)
- tool calls 21 (4 failed, 0 partial, 4 recovered by retry), findings 11/12 verified
