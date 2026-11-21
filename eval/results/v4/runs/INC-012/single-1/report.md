# Incident investigation report: INC-012
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Payment failures (error rate ~60% from 15:00) are NOT caused by the payment-service v6.0.0 release. The true fault is in the shared ledger-db (Postgres): disk usage climbed steadily from 84% at 12:00 to 100% at ~15:02, driven by an inactive replication slot 'analytics_cdc' retaining tens of GB of WAL. When the disk filled, writes failed with 'No space left on device', breaking ledger writes for every tenant of ledger-db. The 14:46 deploy of v6.0.0 and the 15:12 rollback are temporally coincidental: errors began only when disk hit 100% (~15:00), the first slot warning (14:34) predates the deploy, co-tenant order-service failed identically without any deploy, and rolling back payment-service did NOT stop the errors (they continue through 16:57 with disk still 100%). Category: resource_exhaustion originating in ledger-db.

## Probable root cause
**ledger-db disk exhaustion from inactive replication slot retaining WAL** -- resource_exhaustion in `ledger-db` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk exhaustion from inactive replication slot retaining WAL | resource_exhaustion | ledger-db | - | 0.90 (high) | active |
| 2 | H2: payment-service v6.0.0 introduced a faulty ledger write / new MANDATE_HOLD entry type | bad_deployment | payment-service | dep-6203 | 0.05 (low) | rejected |

### H1: ledger-db disk exhaustion from inactive replication slot retaining WAL
An inactive replication slot 'analytics_cdc' on the shared ledger-db retained tens of GB of WAL, filling the data volume to 100% around 15:02. With no free space, Postgres could not extend files or write WAL, so every ledger write from its tenants (payment-service, order-service, ...) failed with 'No space left on device'. payment-service's 60% error rate and the ledger write failures are downstream symptoms; v6.0.0 is a coincidental correlation.

**Causal chain**
1. Inactive replication slot 'analytics_cdc' stops advancing, so Postgres retains all WAL [F5]
2. Retained WAL + normal data growth push ledger-db disk from 84% (12:00) to 100% (~15:02) [F4, F5]
3. At 100% disk, Postgres cannot extend files/write WAL -> 'No space left on device' [F3]
4. All ledger-db tenants fail writes: payment-service and order-service both report identical errors [F2, F8]
5. payment-service surfaces ~60% error rate; rollback of v6.0.0 cannot help because fault is in the DB [F1, F7]

**Supporting evidence**
  - `F2` ✔ First payment-service error is 'ledger write failed: ERROR: could not extend file: No space left on device' at 15:00:26; this same pattern accounts for all 354 errors in the window (first_match 15:00:26).
  - `F3` ✔ ledger-db logs show PANIC/could-not-extend-file 'No space left on device' errors starting 15:01:01 and continuing to the end of data (16:57:21).
  - `F4` ✔ ledger-db disk_used_pct rose monotonically from 84.03% at 12:00 to 100.0% by the 15:02 bucket, and remained 100% through 16:59.
  - `F5` ✔ ledger-db warned 'replication slot 'analytics_cdc' is inactive; retaining 33916 MB of WAL' at 14:34:05, growing to 77371 MB by 14:49:47 — before the 14:46 deploy.
  - `F7` ✔ payment-service errors continued AFTER the 15:12 rollback: 295 further errors between 15:20:03 and 16:57:54, all 'No space left on device'.
  - `F8` ✔ order-service, which also uses ledger-db but was not part of the v6.0.0 deploy, shows the identical failure: 'INSERT INTO orders failed: No space left on device' starting 15:00:42 through 16:57:47.
  - `F9` ✔ ledger-db is a shared Postgres used by payment-service, order-service, portfolio-service and settlement-service (dependency graph).
  - `F10` ✔ bank-partner-api error rate stayed flat at ~0.2% throughout (13:00-16:45), so the external dependency was healthy.
  - `F11` ✔ A prior incident HIST-114 'ledger-db disk full from retained WAL' (2025-04-18) had the same root cause: inactive replication slot retaining WAL until disk filled.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 84%-to-100% disk growth is attributable primarily to the retained WAL (slot warnings show 33-77 GB retained), not an unmeasured data load.
  - No other unobserved process independently filled the disk.

### H2: payment-service v6.0.0 introduced a faulty ledger write / new MANDATE_HOLD entry type
The v6.0.0 release (new mandate flow + new MANDATE_HOLD ledger entry type) caused the ledger write failures and 60% payment error rate.

**Causal chain**
(none given)

**Supporting evidence**
  - `F1` ✔ payment-service error_rate jumped from ~0.2% (13:00-14:50) to ~61% at the 15:00 bucket and stayed elevated (~61-64%) through 16:50.
  - `F6` ✔ The only deployment in the window is payment-service dep-6203 v6.0.0 'New UPI mandate flow' at 14:46:00, rolled back at 15:12:00; there were no config/feature-flag changes.
**Contradicting evidence**
  - `F5` ✔ ledger-db warned 'replication slot 'analytics_cdc' is inactive; retaining 33916 MB of WAL' at 14:34:05, growing to 77371 MB by 14:49:47 — before the 14:46 deploy.
  - `F7` ✔ payment-service errors continued AFTER the 15:12 rollback: 295 further errors between 15:20:03 and 16:57:54, all 'No space left on device'.
  - `F8` ✔ order-service, which also uses ledger-db but was not part of the v6.0.0 deploy, shows the identical failure: 'INSERT INTO orders failed: No space left on device' starting 15:00:42 through 16:57:47.
  - `F9` ✔ ledger-db is a shared Postgres used by payment-service, order-service, portfolio-service and settlement-service (dependency graph).
**Rejected because:** Error onset (~15:00) tracks the disk reaching 100%, not the 14:46 deploy (~15 min gap). The first WAL-retention warning (14:34) predates the deploy. Errors are 'No space left on device', not a MANDATE_HOLD/type error. Rolling back at 15:12 did NOT stop errors (295 continued through 16:57). Co-tenant order-service failed identically with no deploy of its own. This is a shared-infrastructure fault, not a code fault.

## Facts (verified observations)
- payment-service error_rate jumped from ~0.2% to ~61% at the 15:00 bucket and stayed elevated through 16:50. [F1]
- payment-service errors are all 'ledger write failed: ... No space left on device', first at 15:00:26. [F2]
- ledger-db emits 'No space left on device' PANIC/extend-file errors from 15:01 to 16:57. [F3]
- ledger-db disk_used_pct grew from 84.03% at 12:00 to 100% at 15:02 and stayed at 100%. [F4]
- ledger-db warned an inactive replication slot 'analytics_cdc' was retaining up to 77 GB of WAL, first at 14:34 (before the deploy). [F5]
- Only deploy in window: payment-service v6.0.0 at 14:46, rolled back at 15:12; no config changes. [F6]
- Errors continued well after the rollback (295 more from 15:20 to 16:57). [F7]
- order-service, another ledger-db tenant, failed identically ('No space left on device') without any deploy. [F8]
- ledger-db is shared by payment-service, order-service, portfolio-service, settlement-service. [F9]
- bank-partner-api error rate stayed flat at ~0.2% throughout. [F10]
- Historical incident HIST-114 had the same root cause (inactive slot retaining WAL filling ledger-db disk). [F11]

## Assumptions (not verified)
- WAL retention warnings report the slot's retained bytes; the growth from 84% to 100% is dominated by retained WAL.
- No unobserved process independently filled the ledger-db volume.
- ledger-db telemetry (disk_used_pct) accurately reflects the data volume referenced in the 'No space left on device' errors.

## Timeline
- `2026-06-01T12:00:00Z` ledger-db disk already at 84% and climbing steadily; no incident yet. [F4]
- `2026-06-01T14:34:05Z` ledger-db warns replication slot 'analytics_cdc' inactive, retaining 33 GB WAL. [F5]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 deployed (New UPI mandate flow). [F6]
- `2026-06-01T14:48:20Z` ledger-db logs disk usage 98%. [F5]
- `2026-06-01T15:00:26Z` First payment-service ledger write failure (No space left on device); order-service also begins failing at 15:00:42. [F2, F8]
- `2026-06-01T15:01:01Z` ledger-db PANIC: cannot write WAL file, No space left on device. [F3]
- `2026-06-01T15:02:00Z` ledger-db disk reaches 100% and stays there. [F4]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rolled back. [F6]
- `2026-06-01T15:20:00Z` Errors continue (295 more through 16:57) despite rollback; disk still 100%. [F7]

## Recommended actions
- **[immediate]** Immediately free disk on ledger-db: drop/advance the inactive replication slot 'analytics_cdc' and, if needed, remove retained WAL to restore writes. -- The slot is holding tens of GB of WAL and disk is at 100%; clearing it is the direct fix. Same resolution as HIST-114. [F3, F4, F5, F11]
- **[immediate]** Stop treating this as a bad deploy; do not continue rolling back unrelated services. The v6.0.0 rollback will not help. -- Errors persisted after the 15:12 rollback and a co-tenant failed without any deploy. [F7, F8]
- **[short_term]** Add alerting on replication-slot lag / inactive slots and on ledger-db disk_used_pct (warn >=85%, page >=95%). -- Disk grew from 84% to 100% over ~3 hours with no actionable alert; warn only at 98%. [F4, F5]
- **[short_term]** Investigate why the analytics_cdc consumer stopped advancing and add consumer-liveness monitoring. -- Root trigger is the consumer going inactive; preventing recurrence requires fixing the consumer. [F5]
- **[long_term]** Add capacity planning / WAL-size guardrails on the shared ledger-db volume and review co-tenancy blast radius. -- A single shared Postgres disk exhaustion took down multiple payment-critical services simultaneously. [F8, F9]

## Data gaps
- No metric directly attributes disk growth to WAL vs data; the WAL-retention warnings (33-77 GB) strongly suggest WAL, but the slot's exact retained size vs total volume isn't given.
- ledger-db has no memory_pct / iops metric exposed (query for memory_pct returned not_found); disk I/O and inode metrics unavailable.
- No telemetry for bank-partner-api (has_telemetry=false) — ruled out only via payment-service's flat bank_api_error_rate, not direct provider logs.
- portfolio-service logs were not sampled; its failure status on ledger-db is inferred from shared-dependency graph but not directly observed.
- The exact identity/owner of the analytics_cdc consumer and why it went inactive is not visible in the provided data.

## Investigation stats
- latency 51.28s, LLM calls 5, tokens 52389 (in 45586 / out 6803)
- tool calls 16 (1 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
