# Incident investigation report: INC-012
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Payment failures (60-63% 5xx from 15:00) were caused by ledger-db running out of disk space, NOT by the payment-service v6.0.0 release. The shared Postgres ledger-db filled its /var/lib/postgresql/data volume because an inactive replication slot ('analytics_cdc') retained tens of GB of WAL; disk climbed steadily from 78% at 11:00 to 100% at 15:00, and Postgres began PANICing on WAL/file extension at 15:01. The v6.0.0 deploy (14:46) was only temporally coincident with the final approach to 100%, and rolling it back at 15:12 did not restore service — errors continued at ~62% through 16:48. Root cause category: resource_exhaustion at ledger-db. Remediation: free disk (drop/recover the replication slot, archive/purge WAL, expand volume), then restart ledger-db; the payment-service rollback was misdirected.

## Probable root cause
**ledger-db disk exhaustion from retained WAL (inactive replication slot)** -- resource_exhaustion in `ledger-db` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk exhaustion from retained WAL (inactive replication slot) | resource_exhaustion | ledger-db | - | 0.90 (high) | active |
| 2 | H2: payment-service v6.0.0 deployment introduced a ledger entry-type fault | bad_deployment | payment-service | dep-6203 | 0.05 (low) | rejected |
| 3 | H3: payment-service DB connection pool exhaustion | resource_exhaustion | payment-service | - | 0.02 (low) | rejected |

### H1: ledger-db disk exhaustion from retained WAL (inactive replication slot)
An inactive replication slot ('analytics_cdc') on the shared ledger-db retained tens of GB of WAL, gradually filling /var/lib/postgresql/data from ~78% (11:00) to 100% (~15:00). Postgres could no longer extend files or write WAL, so all ledger writes failed ('No space left on device'), which surfaced as payment-service 5xx for every payment requiring a ledger entry.

**Causal chain**
1. Inactive replication slot 'analytics_cdc' retains WAL [F3, F14]
2. Retained WAL fills ledger-db disk to 100% [F1, F3]
3. Postgres PANICs / cannot extend files [F4, F10]
4. ledger write fails -> payment-service 5xx ~60% [F5, F6]

**Supporting evidence**
  - `F1` ✔ ledger-db disk usage rose steadily from ~78.6% at 11:00 to 100% by 14:45-15:00 and stayed pinned at 100% through 16:45.
  - `F2` ✔ Disk was already ~90% at 13:00, BEFORE the 14:46 v6.0.0 deploy, and there was a disk 90% warning at 13:10:01.
  - `F3` ✔ ledger-db repeatedly logged that an inactive replication slot 'analytics_cdc' was retaining tens of GB of WAL, beginning at 12:19:34 — before the deploy.
  - `F4` ✔ Postgres on ledger-db began PANICing on WAL writes and failing file extension with 'No space left on device' starting 15:01:01.
  - `F5` ✔ First payment-service ledger-write error appeared at 15:00:26, matching the disk reaching 100%; 354 such errors in the window.
  - `F6` ✔ payment-service error rate jumped from ~0.22% (through 14:48) to 60.85% at 15:00, aligned with the disk filling — not with the 14:46 deploy.
  - `F10` ✔ ledger-db still logged PANIC/file-extension errors at 15:12-16:58, confirming the disk condition persisted past the rollback.
  - `F12` ✔ ledger-db is a shared Postgres datastore used by several services; payment-service writes to it.
  - `F14` ✔ A matching historical incident HIST-114 had the same signature: 'ledger-db disk full from retained WAL', root cause 'Inactive replication slot retained WAL until disk filled', category resource_exhaustion.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 'analytics_cdc' slot is the dominant consumer of the growing disk usage (WAL retention is the visible mechanism but disk composition is not directly broken down)
  - The fault is confined to ledger-db and not a second node

### H2: payment-service v6.0.0 deployment introduced a ledger entry-type fault
The v6.0.0 'new mandate flow / MANDATE_HOLD ledger entry type' corrupted or overloaded ledger writes, causing failures that a rollback would fix.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F2` ✔ Disk was already ~90% at 13:00, BEFORE the 14:46 v6.0.0 deploy, and there was a disk 90% warning at 13:10:01.
  - `F4` ✔ Postgres on ledger-db began PANICing on WAL writes and failing file extension with 'No space left on device' starting 15:01:01.
  - `F5` ✔ First payment-service ledger-write error appeared at 15:00:26, matching the disk reaching 100%; 354 such errors in the window.
  - `F6` ✔ payment-service error rate jumped from ~0.22% (through 14:48) to 60.85% at 15:00, aligned with the disk filling — not with the 14:46 deploy.
  - `F7` ✔ The v6.0.0 deployment (dep-6203, 14:46) was rolled back at 15:12:00.
  - `F8` ✔ After the rollback, payment-service errors continued: 320 ERROR ledger-write failures between 15:12 and 16:57 (last at 16:57:54).
  - `F9` ✔ payment-service error rate remained ~60-63% after the rollback through 16:48 (no recovery).
**Rejected because:** Errors were caused by ledger-db running out of disk ('No space left on device'), disk was already 90% at 13:00 before the 14:46 deploy, the first error (15:00:26) aligns with disk reaching 100% not with the deploy, and the 15:12 rollback did not reduce the error rate (still ~62% at 16:48) — a rollback without recovery contradicts this hypothesis.

### H3: payment-service DB connection pool exhaustion
payment-service exhausted its DB pool, causing write failures.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F13` ✔ payment-service db pool usage stayed flat (~8 connections) throughout, ruling out pool exhaustion as the fault.
  - `F5` ✔ First payment-service ledger-write error appeared at 15:00:26, matching the disk reaching 100%; 354 such errors in the window.
**Rejected because:** db_pool_in_use stayed flat at ~8 connections throughout the incident (F13); the error text is a Postgres disk error, not a pool timeout.

## Facts (verified observations)
- ledger-db disk usage was already at ~90% by 13:00, before the 14:46 v6.0.0 deployment. [F2]
- Inactive replication slot 'analytics_cdc' was retaining tens of GB of WAL from at least 12:19:34, well before the deploy. [F3]
- ledger-db began PANICing and failing file extension with 'No space left on device' at 15:01:01. [F4]
- First payment-service ledger-write failure was at 15:00:26, aligned with the disk hitting 100%. [F1, F5]
- Rolling back v6.0.0 at 15:12 did not recover the service: errors continued through 16:57 and error rate stayed ~62% at 16:48. [F7, F8, F9]
- ledger-db (shared Postgres) is the dependency that failed, and payment-service merely reported the symptom. [F12, F10]
- payment-service connection pool usage was flat (~8), ruling out pool exhaustion. [F13]
- A prior incident HIST-114 had the identical signature of ledger-db disk full from retained WAL. [F14]

## Assumptions (not verified)
- WAL retained by the 'analytics_cdc' slot is the dominant contributor to the disk fill; exact disk composition by file type is not directly provided.
- The ledger-db change is the only shared component affected; no separate storage/node failure occurred.

## Timeline
- `2026-06-01T11:20:46Z` ledger-db logs 'disk usage 80% on /var/lib/postgresql/data' [F3]
- `2026-06-01T12:19:34Z` First 'analytics_cdc inactive, retaining 63157 MB of WAL' warning [F3]
- `2026-06-01T13:00:00Z` ledger-db disk ~90% (avg 89.69%) — before any deploy [F2]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (dep-6203) deployed [F7]
- `2026-06-01T14:45:00Z` ledger-db disk hits 100% [F1]
- `2026-06-01T15:00:26Z` First payment-service 'ledger write failed: No space left on device' [F5]
- `2026-06-01T15:00:00Z` payment-service error rate jumps to 60.85% [F6]
- `2026-06-01T15:01:01Z` ledger-db PANIC: could not write to pg_wal file, no space left on device [F4]
- `2026-06-01T15:12:00Z` v6.0.0 rolled back — no recovery [F7]
- `2026-06-01T16:57:54Z` payment-service ledger-write errors still occurring; disk still 100% [F8, F10]

## Recommended actions
- **[immediate]** Free space on ledger-db immediately: recover or drop the inactive 'analytics_cdc' replication slot, purge/archive retained WAL, and/or expand the /var/lib/postgresql/data volume; then restart Postgres cleanly. -- Disk is pinned at 100% and Postgres is PANICing; this is the actual fault. Dropping the inactive slot releases the retained WAL. [F1, F3, F4, F14]
- **[immediate]** Stop treating the payment-service rollback as the fix; it has not restored service and error rate is unchanged. -- Rollback at 15:12 did not reduce errors (still ~62% at 16:48), so it is not the causal fix. [F7, F9]
- **[short_term]** Alert on replication-slot WAL retention and on disk usage thresholds on ledger-db (e.g., page at 85%). -- HIST-114 resolution added a slot-lag alert; the same gap recurred, and disk creeping 78%->100% over hours was only a soft warning. [F14, F3]
- **[long_term]** Add an automated check/monitor that removes or flags abandoned replication slots consuming WAL. -- Prevents recurrence of the same failure mode [F3, F14]

## Data gaps
- replication_lag_ms for ledger-db timed out (tc-010) — could not confirm replica/slot lag numbers directly.
- api-gateway logs timed out repeatedly (tc-016, tc-017) — upstream 5xx propagation path unverified from the gateway side.
- No metric directly attributes disk consumption to WAL vs table growth; the WAL-retention link rests on log warnings plus the historical match.
- payment-service request_rate query timed out (tc-015); unable to confirm whether traffic volume also changed.

## Investigation stats
- latency 68.03s, LLM calls 6, tokens 72470 (in 65115 / out 7355)
- tool calls 18 (6 failed, 0 partial, 4 recovered by retry), findings 14/14 verified
