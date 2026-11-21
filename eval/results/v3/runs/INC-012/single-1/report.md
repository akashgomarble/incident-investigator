# Incident investigation report: INC-012
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Payments broke because the shared ledger-db ran out of disk space (reached 100% at ~14:50–15:00), causing all writes — payment ledger writes, order inserts, WAL appends — to fail with "No space left on device". The disk was already ~89.5% full at 13:00 and climbed steadily BEFORE the payment-service v6.0.0 deploy at 14:46, driven by an inactive replication slot 'analytics_cdc' retaining tens of GB of WAL (up to 92.6 GB). The v6.0.0 mandate flow is a coincidental bystander: rolling it back at 15:12 did NOT recover the service, and errors continued at ~320/period through 16:57. Root cause is resource_exhaustion at ledger-db, matching historical incident HIST-114.

## Probable root cause
**ledger-db disk exhaustion from inactive replication slot retaining WAL** -- resource_exhaustion in `ledger-db` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk exhaustion from inactive replication slot retaining WAL | resource_exhaustion | ledger-db | - | 0.90 (high) | active |
| 2 | H2: payment-service v6.0.0 mandate flow overloaded ledger-db | bad_deployment | payment-service | dep-6203 | 0.05 (low) | rejected |
| 3 | H3: bank-partner-api dependency failure | dependency_failure | bank-partner-api | - | 0.02 (low) | rejected |

### H1: ledger-db disk exhaustion from inactive replication slot retaining WAL
The shared ledger-db filled its filesystem because an inactive replication slot 'analytics_cdc' retained tens of GB of WAL; when the volume hit 100% (~14:50), all Postgres writes failed with 'No space left on device', breaking payment-service (ledger writes), order-service (order inserts) and any other writer. The fault originates in ledger-db, not payment-service.

**Causal chain**
1. Inactive replication slot 'analytics_cdc' retains WAL continuously (33–92 GB) starting before 12:19 [F3, F11]
2. Retained WAL drives ledger-db disk usage from 89.5% (13:00) to 100% (~14:50) [F2, F14]
3. At 100% disk, ledger-db write/PANIC errors 'No space left on device' begin 15:01 [F4]
4. payment-service ledger writes fail -> error_rate 61%, success 35% [F5, F7]
5. order-service on the same shared DB also fails on INSERTs [F8, F9]
6. Rollback of v6.0.0 does not restore service because the disk is still full [F6]

**Supporting evidence**
  - `F1` ✔ payment-service v6.0.0 (new UPI mandate flow, new ledger entry type MANDATE_HOLD) deployed at 14:46 by p.nair and was rolled back at 15:12.
  - `F2` ✔ ledger-db disk usage was already ~89.5% at 13:00 and rose monotonically to 100% by ~14:50, before any payment-service errors began (first error 15:00:26).
  - `F3` ✔ ledger-db logs show an inactive replication slot 'analytics_cdc' retaining tens of GB of WAL (up to 92599 MB) repeatedly, and disk-usage warnings escalating 80%→90%→95%→98%.
  - `F4` ✔ ledger-db emits PANIC/write errors 'No space left on device' for pg_wal and base files beginning 15:01:01, continuation of the disk-full condition.
  - `F5` ✔ payment-service produced 179 'ledger write failed: ... No space left on device' errors in the 15:00–16:00 window, starting 15:00:26.
  - `F6` ✔ After the 15:12 rollback, payment-service continued erroring: 320 more 'No space left on device' errors from 15:12:08 through 16:57:54.
  - `F7` ✔ payment-service error_rate jumped from ~0.22% before 14:54 to avg 61% at 15:03 and stayed ~62% through 16:51; payment_success_rate fell from ~99% to ~35% at 15:00 and stayed there.
  - `F8` ✔ order-service (a different, trading-owned service) also fails on the same shared ledger-db with 'INSERT INTO orders failed: No space left on device' starting 15:00:42 — 236 occurrences.
  - `F9` ✔ ledger-db is a shared postgres datastore used by order-service, portfolio-service, payment-service and settlement-service.
  - `F11` ✔ ledger-db replication_lag_ms climbed steadily from ~926 ms at 11:00 to ~5213 ms at 16:42, consistent with an inactive consumer slot retaining WAL.
  - `F13` ✔ Historical incident HIST-114 matches: 'ledger-db disk full from retained WAL', root cause 'Inactive replication slot retained WAL until disk filled', category resource_exhaustion.
  - `F14` ✔ The disk was already ~95% full at 14:15 and ~96.8% at 14:20, i.e. the exhaustion trajectory predated the 14:46 deploy.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Replication slot 'analytics_cdc' belongs to an inactive analytics/CDC consumer that never caught up.
  - No other large writer contributed comparably.

### H2: payment-service v6.0.0 mandate flow overloaded ledger-db
The v6.0.0 release's new mandate flow (MANDATE_HOLD ledger entries) wrote abnormally and filled the ledger-db, breaking payments.

**Causal chain**
1. v6.0.0 deployed 14:46 with new ledger entry type [F1]
2. New writes fill ledger-db disk *(assumed)*
3. Writes fail, payments break [F5]

**Supporting evidence**
  - `F1` ✔ payment-service v6.0.0 (new UPI mandate flow, new ledger entry type MANDATE_HOLD) deployed at 14:46 by p.nair and was rolled back at 15:12.
**Contradicting evidence**
  - `F2` ✔ ledger-db disk usage was already ~89.5% at 13:00 and rose monotonically to 100% by ~14:50, before any payment-service errors began (first error 15:00:26).
  - `F6` ✔ After the 15:12 rollback, payment-service continued erroring: 320 more 'No space left on device' errors from 15:12:08 through 16:57:54.
  - `F8` ✔ order-service (a different, trading-owned service) also fails on the same shared ledger-db with 'INSERT INTO orders failed: No space left on device' starting 15:00:42 — 236 occurrences.
  - `F14` ✔ The disk was already ~95% full at 14:15 and ~96.8% at 14:20, i.e. the exhaustion trajectory predated the 14:46 deploy.
  - `F13` ✔ Historical incident HIST-114 matches: 'ledger-db disk full from retained WAL', root cause 'Inactive replication slot retained WAL until disk filled', category resource_exhaustion.
**Assumptions**
  - New entry type writes significantly more data than before.
**Rejected because:** Disk was already 89.5% at 13:00 and 95% at 14:15, well before the 14:46 deploy; the rollback at 15:12 did not recover service and errors continued at 320/period through 16:57; a different service (order-service) sharing ledger-db failed identically at the same time; and the known mechanism is WAL retained by an inactive replication slot.

### H3: bank-partner-api dependency failure
The external bank partner API degraded and caused payment failures.

**Causal chain**
1. Bank API returns errors *(assumed)*
2. payments fail [F7]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ payment-service bank_api_error_rate stayed flat at 0.2% throughout (13:00–16:45), so the external bank-partner-api was healthy and did not cause the failures.
  - `F5` ✔ payment-service produced 179 'ledger write failed: ... No space left on device' errors in the 15:00–16:00 window, starting 15:00:26.
**Assumptions**
  - None.
**Rejected because:** bank_api_error_rate stayed flat at 0.2% throughout, and the actual errors are ledger DB disk failures, not bank-API errors.

## Facts (verified observations)
- payment-service v6.0.0 (new UPI mandate flow, MANDATE_HOLD ledger entry type) deployed 14:46 and rolled back 15:12. [F1]
- ledger-db disk usage was 89.5% at 13:00 and 100% by ~14:50, before payment errors began. [F2, F14]
- ledger-db logs repeatedly show inactive replication slot 'analytics_cdc' retaining up to 92599 MB of WAL and disk-usage warnings up to 98%. [F3]
- ledger-db raised PANIC 'No space left on device' write errors from 15:01:01. [F4]
- payment-service logged 179 'ledger write failed ... No space left on device' errors starting 15:00:26. [F5]
- The 15:12 rollback did not restore service: 320 more errors through 16:57:54. [F6]
- payment-service error_rate rose to ~61% and payment_success_rate fell to ~35%. [F7]
- order-service, sharing ledger-db, failed with 'INSERT INTO orders failed: No space left on device' starting 15:00:42. [F8, F9]
- bank_api_error_rate stayed flat at 0.2%, ruling out the bank partner API. [F10]
- ledger-db replication lag rose from ~926 ms (11:00) to ~5221 ms (16:42). [F11]
- Historical incident HIST-114 was ledger-db disk full from retained WAL due to an inactive replication slot. [F13]

## Assumptions (not verified)
- The replication slot 'analytics_cdc' belongs to an inactive CDC/analytics consumer and its WAL is unused and safe to drop, subject to operator confirmation.
- No other single source of write volume (e.g., a bulk backfill) contributed comparably to disk growth; WAL retention is the dominant driver per log evidence.

## Timeline
- `2026-06-01T11:00:00Z` ledger-db disk ~89% trajectory begins; replication lag ~926 ms and rising [F11, F2]
- `2026-06-01T11:20:46Z` ledger-db warns 'disk usage 80% on /var/lib/postgresql/data' [F3]
- `2026-06-01T12:19:34Z` Inactive replication slot 'analytics_cdc' retaining 63157 MB of WAL [F3]
- `2026-06-01T13:00:00Z` ledger-db disk already 89.5% (metric) [F2]
- `2026-06-01T14:15:45Z` ledger-db warns 'disk usage 95%' [F14]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 deployed (new mandate flow) [F1]
- `2026-06-01T14:48:20Z` ledger-db warns 'disk usage 98%' [F3]
- `2026-06-01T14:50:00Z` ledger-db disk reaches ~99.6%, then 100% [F2]
- `2026-06-01T15:00:26Z` payment-service first 'ledger write failed: No space left on device' [F5]
- `2026-06-01T15:00:42Z` order-service first 'INSERT INTO orders failed: No space left on device' [F8]
- `2026-06-01T15:01:01Z` ledger-db PANIC on pg_wal write, no space left [F4]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rolled back — service does NOT recover [F1, F6]
- `2026-06-01T16:57:54Z` payment-service still logging disk-full errors [F6]

## Recommended actions
- **[immediate]** Free space on ledger-db immediately: drop or advance the inactive replication slot 'analytics_cdc' (after confirming no consumer needs it) and/or expand the volume; then verify writes resume. -- The inactive slot's retained WAL is what filled the disk; freeing it is the actual fix since rollback did not help. [F3, F6, F13]
- **[immediate]** Stop treating this as a payment-service regression: keep services up, do not re-deploy, and monitor ledger-db disk_used_pct and write success for recovery. -- The fault is in the shared DB, and the v6.0.0 rollback neither caused nor fixed the outage. [F1, F6, F8]
- **[short_term]** Alert on replication-slot lag/retention and on ledger-db disk_used_pct thresholds (e.g. >85%) with automated WAL-retention alerts. -- Disk climbed from 80% to 100% over ~3.5h with only WARNs; earlier alerting would have prevented the outage. HIST-114 already recommended a slot-lag alert. [F3, F13]
- **[long_term]** Add disk-capacity headroom/monitoring for the shared ledger-db and audit all replication slots/data-volume consumers; add periodic slot cleanup. -- A shared datastore serving order/payment/settlement/portfolio services is a single point of failure when its disk fills. [F9, F13]

## Data gaps
- No direct metric for replication slot retention size; WAL retention inferred from log messages only.
- No log/telemetry evidence identifying which consumer owns slot 'analytics_cdc' or why it stopped consuming.
- ledger-db logs were truncated (returned 20 of 298 matched in one window), so the full sequence of disk warnings is only partially captured.
- No evidence yet that disk space was reclaimed between 15:12 and 16:59 (errors persist, implying it was not); a post-remediation confirmation is needed.

## Investigation stats
- latency 77.93s, LLM calls 8, tokens 123205 (in 109880 / out 13325)
- tool calls 23 (1 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
