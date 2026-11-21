# Incident investigation report: INC-012
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Payment failures are NOT caused by payment-service v6.0.0; the root cause is disk exhaustion on the shared ledger-db postgres, driven by the inactive replication slot 'analytics_cdc' retaining tens of GB of WAL. Disk_used_pct rose steadily from 79% at 11:00 to 100% by ~14:54 — hours before the 14:46 deploy — then all write paths failed with 'No space left on device'. The onset (~15:00) is shared across payment-service (~60%), order-service (~48%) and settlement-service (~55%), none of which share the mandate flow; order-service fails with the same disk error on plain INSERTs. The 15:12 rollback of v6.0.0 did not restore service. This matches prior incident HIST-114 (identical root cause on ledger-db). Immediate fix: drop the inactive slot / free disk (or grow volume), then re-run writes; the deploy rollback is a red herring.

## Probable root cause
**ledger-db disk exhaustion from inactive replication slot retaining WAL (resource_exhaustion)** -- resource_exhaustion in `ledger-db` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk exhaustion from inactive replication slot retaining WAL (resource_exhaustion) | resource_exhaustion | ledger-db | - | 0.90 (high) | active |
| 2 | H2: payment-service v6.0.0 mandate/ledger-write defect caused the failure (bad_deployment) | bad_deployment | payment-service | dep-6203 | 0.06 (low) | rejected |
| 3 | H3: order-service or settlement-service deployment overloaded the shared DB (capacity_saturation) | capacity_saturation | ledger-db | - | 0.05 (low) | rejected |

### H1: ledger-db disk exhaustion from inactive replication slot retaining WAL (resource_exhaustion)
The shared ledger-db postgres filled its disk because the inactive replication slot 'analytics_cdc' retained tens of GB of WAL while disk_used_pct climbed from 79% (11:00) to 100% (~14:54). Once full, all write paths failed ('No space left on device'), causing ~60% errors in payment-service and simultaneous ~48%/55% errors in order-service and settlement-service. The payment v6.0.0 deploy is coincidental in timing, not causal.

**Causal chain**
1. Replication slot 'analytics_cdc' inactive -> WAL retained on ledger-db [F6, F7]
2. Retained WAL grows disk_used_pct from 79% (11:00) to 100% (~14:54) [F5, F6]
3. Disk full -> postgres write/panic errors 'No space left on device' [F6]
4. All writers fail identically -> payment, order, settlement error rates jump at ~15:00 [F3, F8, F9]

**Supporting evidence**
  - `F5` ✔ ledger-db disk_used_pct rises monotonically from 79.03% at 11:00 to 100% by ~14:54/15:13 — i.e., it was already ~92% and climbing hours BEFORE the 14:46 deploy, and was full ~6 min before the 15:00 error onset.
  - `F6` ✔ ledger-db logs show an inactive replication slot 'analytics_cdc' repeatedly retaining WAL (37927 MB, 69082 MB, 92599 MB, ...), disk-usage warnings (95% at 14:15:45Z, 98% at 14:48:20Z), and then write failures: 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (153x) and 'could not extend file "base/16402/2619": No space left on device' (139x).
  - `F7` ✔ ledger-db replication_lag_ms climbs steadily from ~2795ms at 13:30 to ~5222ms at 16:46, consistent with a stalled/inactive replica retaining WAL.
  - `F8` ✔ Unrelated ledger-db consumers failed at the SAME time as payment-service: order-service errors jumped from ~0.2% to ~48% at 15:00 and stayed elevated; settlement-service similarly jumped to ~55% at 15:00. Neither runs the mandate flow.
  - `F9` ✔ order-service errors are 'INSERT INTO orders failed: No space left on device' (236 entries) starting 15:00:42Z — the same disk-exhaustion failure, on a different service that never used the MANDATE_HOLD code path.
  - `F11` ✔ A prior incident HIST-114 (2025-04-18) on ledger-db had the identical root cause: 'Inactive replication slot retained WAL until disk filled', category resource_exhaustion, resolution 'Dropped slot; added slot-lag alert'.
  - `F4` ✔ ledger-db (shared postgres, owner=platform) is the common dependency of payment-service, order-service, portfolio-service and settlement-service; payment-service writes to it via db.
**Contradicting evidence**
  - `F10` ✔ portfolio-service, which also depends on ledger-db, was NOT materially affected: its error rate stayed ~0.2% throughout 15:00-16:50 and its logs show reads/health/connection-pool OK with only one transient retry.
**Assumptions**
  - portfolio-service largely read-only, so disk-full affects it less; its health does not disprove shared-disk exhaustion
  - the analytics_cdc slot is the dominant WAL retainer (bytes match disk growth trend)

### H2: payment-service v6.0.0 mandate/ledger-write defect caused the failure (bad_deployment)
The new MANDATE_HOLD ledger entry type in v6.0.0 introduced a faulty write path that made ledger writes fail, driving payment-service errors. This is the report's own claim.

**Causal chain**
1. deploy dep-6203 -> new ledger write path fails *(assumed)*
2. payment errors [F2]

**Supporting evidence**
  - `F1` ✔ payment-service v6.0.0 (dep-6203, 'New UPI mandate flow', new ledger entry type MANDATE_HOLD) deployed 2026-06-01T14:46:00Z and was rolled back at 15:12:00Z.
  - `F2` ✔ payment-service error rate jumped from ~0.2% to ~60% starting in the 15:00 bucket (60.85% avg at 15:00, peak 70.45%), and remained ~60-63% through 16:48, after the 15:12 rollback.
**Contradicting evidence**
  - `F5` ✔ ledger-db disk_used_pct rises monotonically from 79.03% at 11:00 to 100% by ~14:54/15:13 — i.e., it was already ~92% and climbing hours BEFORE the 14:46 deploy, and was full ~6 min before the 15:00 error onset.
  - `F6` ✔ ledger-db logs show an inactive replication slot 'analytics_cdc' repeatedly retaining WAL (37927 MB, 69082 MB, 92599 MB, ...), disk-usage warnings (95% at 14:15:45Z, 98% at 14:48:20Z), and then write failures: 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (153x) and 'could not extend file "base/16402/2619": No space left on device' (139x).
  - `F8` ✔ Unrelated ledger-db consumers failed at the SAME time as payment-service: order-service errors jumped from ~0.2% to ~48% at 15:00 and stayed elevated; settlement-service similarly jumped to ~55% at 15:00. Neither runs the mandate flow.
  - `F9` ✔ order-service errors are 'INSERT INTO orders failed: No space left on device' (236 entries) starting 15:00:42Z — the same disk-exhaustion failure, on a different service that never used the MANDATE_HOLD code path.
**Assumptions**
  - deploy timing coincidence with disk filling
**Rejected because:** The error message is a disk-space error on the shared DB, not an application/logic error (tc-005). Disk was already ~92% and climbing hours before the 14:46 deploy (tc-022). order-service and settlement-service, which do not share the mandate flow, failed at the same 15:00 onset with the same disk errors (tc-013/14/16). Rollback at 15:12 did not restore service (errors continue to 16:57, tc-004/tc-005).

### H3: order-service or settlement-service deployment overloaded the shared DB (capacity_saturation)
A change elsewhere drove write volume that saturated ledger-db capacity.

**Causal chain**
1. load increase -> disk full *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F1` ✔ payment-service v6.0.0 (dep-6203, 'New UPI mandate flow', new ledger entry type MANDATE_HOLD) deployed 2026-06-01T14:46:00Z and was rolled back at 15:12:00Z.
  - `F5` ✔ ledger-db disk_used_pct rises monotonically from 79.03% at 11:00 to 100% by ~14:54/15:13 — i.e., it was already ~92% and climbing hours BEFORE the 14:46 deploy, and was full ~6 min before the 15:00 error onset.
  - `F12` ✔ No configuration or feature-flag changes were recorded for any service during the incident window.
**Rejected because:** Only one deployment exists in the window (payment-service dep-6203); no config changes (tc-001, tc-002). Disk growth is a steady multi-hour WAL retention trend (tc-022), and slow_queries_per_min stayed ~1/min with no load spike (tc-021).

## Facts (verified observations)
- payment-service v6.0.0 deployed 14:46Z and rolled back 15:12Z; no other deploy or config change occurred in the window. [F1, F12]
- payment, order and settlement services all jumped from ~0.2% to ~48-60% errors at the 15:00 bucket, while the rollback at 15:12 did not reduce errors. [F2, F8]
- All observed errors across payment-service and order-service are 'No space left on device' write failures. [F3, F9]
- ledger-db disk_used_pct climbed from 79.03% (11:00) to 100% (~14:54), already ~92%+ before the 14:46 deploy. [F5]
- ledger-db logs show inactive slot analytics_cdc retaining WAL and write panics on pg_wal/base files at 15:01Z. [F6, F7]
- ledger-db is a shared datastore used by payment-service, order-service, portfolio-service and settlement-service. [F4]
- portfolio-service, also a ledger-db consumer, was unaffected (~0.2% errors), consistent with it being read-dominant. [F10]
- HIST-114 records the identical prior failure mode (inactive replication slot retained WAL until disk filled) on ledger-db. [F11]

## Assumptions (not verified)
- The analytics_cdc inactive slot's retained WAL (up to ~92GB) is the dominant contributor to the observed disk growth.
- portfolio-service is largely read-only, so its health under a disk-full condition does not contradict the shared-disk hypothesis.
- Metric buckets are 720-840s wide, so onset is reported to the nearest ~12-14 min (consistent ~15:00 across services).

## Timeline
- `2026-06-01T11:00:00Z` ledger-db disk_used_pct already 79.03% and rising (~1.5%/20min). [F5]
- `2026-06-01T13:37:25Z` ledger-db WARN: inactive replication slot 'analytics_cdc' retaining 37927 MB of WAL. [F6]
- `2026-06-01T14:15:45Z` ledger-db disk usage 95% warning. [F6]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (dep-6203, mandate flow) deployed. [F1]
- `2026-06-01T14:48:20Z` ledger-db disk usage 98% warning. [F6]
- `2026-06-01T15:00:00Z` Disk ~100%; payment (~60%), order (~48%) and settlement (~55%) error rates spike; ledger write/panic 'No space left on device' begins (~15:00:26). [F3, F8, F9, F5]
- `2026-06-01T15:12:00Z` v6.0.0 rolled back — errors do NOT recover. [F1, F2]
- `2026-06-01T16:57:54Z` Disk still 100%, writes still failing in payment/order services; replication lag ~5.2s and rising. [F3, F7]

## Recommended actions
- **[immediate]** Free disk on ledger-db: drop (or reactivate the consumer for) the inactive replication slot 'analytics_cdc', and/or grow the volume; then confirm disk_used_pct drops below 100%. -- The inactive slot is retaining ~tens of GB of WAL and is the direct cause of the full disk; freeing WAL restores writes for all services. [F6, F11]
- **[immediate]** Retract/close the v6.0.0 rollback as the incident resolution and communicate that the deploy was not the cause. -- Rollback did not restore service and unrelated services failed simultaneously; continued focus on the deploy wastes time. [F2, F8]
- **[short_term]** Add alerting on replication slot lag/retained-WAL bytes and on disk_used_pct at ~85% for ledger-db. -- HIST-114's resolution added a slot-lag alert, but the disk still filled, so alerting either regressed or did not cover this slot; proactive alerting prevents recurrence. [F11, F5]
- **[long_term]** Investigate why the analytics_cdc replication consumer is inactive and add automated slot-retention cleanup/monitoring. -- Recurring pattern (HIST-114) indicates a systemic gap in CDC slot lifecycle management. [F6, F11]

## Data gaps
- No telemetry/owner data for the analytics_cdc replication consumer — could not confirm whether the slot is expected or orphaned, nor exactly when it went inactive.
- bank-partner-api has no telemetry (has_telemetry:false), so a simultaneous external provider issue cannot be fully excluded — though the disk errors are on the DB write path and dominate the evidence.
- No disk/I/O metric exists for the underlying node; disk_used_pct is the only filesystem signal, so free-space recovery after any mitigation cannot yet be observed.
- No deployment or config activity was found for ledger-db/platform itself, so the change that caused the slot to go inactive is not identifiable from available tools.

## Investigation stats
- latency 44.58s, LLM calls 5, tokens 61159 (in 53738 / out 7421)
- tool calls 22 (2 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
