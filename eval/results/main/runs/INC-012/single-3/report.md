# Incident investigation report: INC-012
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The payment-service outage is a symptom, not the fault. The shared ledger-db Postgres filled to 100% disk, driven by an inactive replication slot ('analytics_cdc') retaining tens of GB of WAL. Disk climbed steadily 89%->100% between 13:00 and 14:50; Postgres then failed with 'No space left on device' / 'could not extend file'. All write-path clients of ledger-db broke at ~15:00 - payment-service ("ledger write failed") AND order-service ("INSERT INTO orders failed"), the latter with no deployment of its own. This rules out the v6.0.0 deploy (rolled back 15:12 without recovery; disk was already ~98% before the 14:46 deploy) and points to resource_exhaustion originating in ledger-db. The v6.0.0 'new ledger entry type' is coincidental; payment-service's write-heavy path made it the loudest victim.

## Probable root cause
**ledger-db disk full from inactive replication slot retaining WAL** -- resource_exhaustion in `ledger-db` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk full from inactive replication slot retaining WAL | resource_exhaustion | ledger-db | - | 0.85 (high) | active |
| 2 | H2: payment-service v6.0.0 mandate flow / new ledger entry type caused failures | bad_deployment | payment-service | dep-6203 | 0.05 (low) | rejected |
| 3 | H3: Credential/TLS or clock issue on payment-service | credential_expiry | payment-service | - | 0.02 (low) | rejected |

### H1: ledger-db disk full from inactive replication slot retaining WAL
On the shared ledger-db Postgres, the replication slot 'analytics_cdc' went inactive and retained tens of GB of WAL, steadily consuming disk from ~89% up to 100% by ~14:50. Once full, Postgres could no longer write WAL or extend files, so all write-path clients of ledger-db (payment-service, order-service) failed with 'No space left on device'. The fault originates in ledger-db/infrastructure, not in any service code.

**Causal chain**
1. analytics_cdc replication slot inactive -> WAL retained (F2, F13) [F2, F13]
2. retained WAL -> disk fills 89%->100% by 14:50 (F1, F3) [F1, F3]
3. disk 100% -> Postgres PANIC / cannot extend file (F4) [F4]
4. shared ledger-db write failures -> payment-service AND order-service 5xx at ~15:00 (F5, F8) [F5, F8]
5. errors persist after rollback because fault is not in the deploy (F9) [F9]

**Supporting evidence**
  - `F1` ✔ ledger-db disk usage climbed steadily from ~89% at 13:00 to 100% by 14:50 and stayed pinned at 100% through 16:50.
  - `F2` ✔ ledger-db logs repeatedly report the inactive replication slot 'analytics_cdc' retaining large amounts of WAL (up to ~92 GB) starting well before the incident.
  - `F3` ✔ ledger-db emitted disk-space warnings rising 90% -> 98% in the hours before the failure.
  - `F4` ✔ ledger-db threw PANIC 'No space left on device' writing WAL and 'could not extend file' errors starting 15:01.
  - `F5` ✔ payment-service logged 354 'ledger write failed: ERROR: could not extend file: No space left on device' errors starting 15:00:26.
  - `F8` ✔ order-service, a service NOT deployed in this window and also a client of ledger-db, began failing at 15:00:42 with 'INSERT INTO orders failed: No space left on device' and its error_rate jumped from ~0.2% to ~48% at 15:00.
  - `F11` ✔ ledger-db is a shared Postgres datastore used by order-service, portfolio-service, payment-service and settlement-service.
  - `F12` ✔ ledger-db replication_lag_ms rose monotonically across the window (2340ms -> 5213ms), consistent with a stuck/retained WAL on a replication slot.
  - `F13` ✔ A prior incident HIST-114 (2025-04-18) had the same root cause: 'Inactive replication slot retained WAL until disk filled', category resource_exhaustion on ledger-db.
**Contradicting evidence**
  - `F7` ✔ The only deployment in the window was payment-service v6.0.0 at 14:46, rolled back at 15:12; it is the sole code change and there were zero config/flag changes.
**Assumptions**
  - The 'analytics_cdc' slot retention is the dominant cause of the disk growth vs. ordinary data growth (log shows repeated large WAL retention, but total disk budget unknown)
  - No concurrent capacity/disk resize event explains the fill

### H2: payment-service v6.0.0 mandate flow / new ledger entry type caused failures
The v6.0.0 release's new MANDATE_HOLD ledger flow produced excessive writes or an error that broke ledger writes, causing payment failures.

**Causal chain**
(none given)

**Supporting evidence**
  - `F7` ✔ The only deployment in the window was payment-service v6.0.0 at 14:46, rolled back at 15:12; it is the sole code change and there were zero config/flag changes.
**Contradicting evidence**
  - `F1` ✔ ledger-db disk usage climbed steadily from ~89% at 13:00 to 100% by 14:50 and stayed pinned at 100% through 16:50.
  - `F2` ✔ ledger-db logs repeatedly report the inactive replication slot 'analytics_cdc' retaining large amounts of WAL (up to ~92 GB) starting well before the incident.
  - `F8` ✔ order-service, a service NOT deployed in this window and also a client of ledger-db, began failing at 15:00:42 with 'INSERT INTO orders failed: No space left on device' and its error_rate jumped from ~0.2% to ~48% at 15:00.
  - `F9` ✔ Error rate for payment-service remained ~60% after the 15:12 rollback, through the last data point 16:50.
**Rejected because:** Disk was already ~98% before the 14:46 deploy and 100% by 14:50; an undeployed sibling service (order-service) failed simultaneously with the identical 'No space left on device' error; and errors continued at ~60% after the 15:12 rollback. A code rollback did not restore service.

### H3: Credential/TLS or clock issue on payment-service
A deprecated TLS cipher / clock drift noticed in logs caused the outage.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F4` ✔ ledger-db threw PANIC 'No space left on device' writing WAL and 'could not extend file' errors starting 15:01.
  - `F5` ✔ payment-service logged 354 'ledger write failed: ERROR: could not extend file: No space left on device' errors starting 15:00:26.
  - `F8` ✔ order-service, a service NOT deployed in this window and also a client of ledger-db, began failing at 15:00:42 with 'INSERT INTO orders failed: No space left on device' and its error_rate jumped from ~0.2% to ~48% at 15:00.
**Rejected because:** TLS cipher and clock-drift warnings are single, sparse, and unrelated to the 'No space left on device' errors that dominate both payment-service and ledger-db logs.

## Facts (verified observations)
- ledger-db disk_used_pct reached 100% by ~14:50 and stayed at 100% through 16:50. [F1]
- ledger-db replication slot 'analytics_cdc' was inactive and retaining up to ~92 GB of WAL starting before the incident. [F2]
- ledger-db logged disk usage warnings 90% -> 98% before failing. [F3]
- ledger-db threw PANIC 'No space left on device' and 'could not extend file' errors from 15:01. [F4]
- payment-service threw 354 'ledger write failed: ...No space left on device' errors from 15:00:26. [F5]
- payment-service error_rate jumped from ~0.2% to ~61% at 15:00, not at the 14:46 deploy. [F6]
- The only change was payment-service v6.0.0 (deployed 14:46, rolled back 15:12); no config/flag changes. [F7]
- order-service, not deployed, began 'No space left on device' failures at 15:00:42 and error_rate jumped to ~48%, the same ledger-db symptom. [F8]
- payment-service errors persisted at ~60% after the 15:12 rollback. [F9]
- portfolio-service (also a ledger-db client) was unaffected at ~0.2%. [F10]
- ledger-db is a shared Postgres used by order-, portfolio-, payment-, and settlement-service. [F11]
- ledger-db replication_lag_ms grew monotonically (2340ms -> 5213ms), consistent with retained WAL on a slot. [F12]
- Prior incident HIST-114 had the identical root cause (inactive replication slot retaining WAL filling disk). [F13]

## Assumptions (not verified)
- The repeated 'analytics_cdc inactive, retaining N MB of WAL' messages are caused by a genuinely abandoned CDC consumer rather than a transient hiccup that self-resolved.
- Metric values are per the shared service scope; disk_used_pct reflects the ledger-db data volume that payments and orders write to.

## Timeline
- `2026-06-01T13:02:25Z` ledger-db begins warning that slot 'analytics_cdc' is inactive and retaining WAL; disk ~89%. [F1, F2]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 deployed (disk already ~98%). [F7, F3]
- `2026-06-01T14:48:20Z` ledger-db disk usage 98%. [F3]
- `2026-06-01T14:50:00Z` ledger-db disk reaches 100%. [F1]
- `2026-06-01T15:00:26Z` payment-service begins 'ledger write failed: No space left on device'. [F5]
- `2026-06-01T15:00:42Z` order-service begins 'INSERT INTO orders failed: No space left on device'. [F8]
- `2026-06-01T15:01:01Z` ledger-db PANIC writing WAL: No space left on device. [F4]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rolled back; errors continue. [F7, F9]
- `2026-06-01T16:50:00Z` payment error_rate still ~62%; disk still 100%. [F9, F1]

## Recommended actions
- **[immediate]** Immediately free disk on ledger-db: drop the inactive 'analytics_cdc' replication slot (after confirming it is unused) to release retained WAL, or add disk capacity as a stopgap. -- The disk is pinned at 100% and Postgres PANICs on WAL writes; releasing retained WAL is the direct fix (matches HIST-114 resolution). [F1, F2, F4, F13]
- **[immediate]** Stop treating this as a payment-service deploy problem - the v6.0.0 rollback did not recover service; keep the rollback decision under review but focus on ledger-db. -- Errors persisted after 15:12 rollback and an undeployed service failed identically. [F9, F8, F7]
- **[short_term]** Add alerting on replication-slot lag/retained WAL size and on ledger-db disk >85%. -- Both this and HIST-114 were silent disk fills from a slot; an early alert would have caught it hours earlier. [F2, F13, F1]
- **[long_term]** Audit all replication slots (especially 'analytics_cdc') for ownership/monitoring and add auto-drop or watchdog policy for inactive slots. -- Recurring class of failure; needs governance. [F2, F13]

## Data gaps
- Total disk size/absolute free bytes on ledger-db not available (only disk_used_pct), so the exact contribution of WAL retention vs. ordinary data growth cannot be quantified.
- No ledger-db node/infra event log to confirm whether any disk resize or maintenance occurred.
- bank-partner-api has no telemetry; its state during the incident is unknown (no evidence it was involved, but unverified).

## Investigation stats
- latency 39.96s, LLM calls 5, tokens 53591 (in 47019 / out 6572)
- tool calls 16 (2 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
