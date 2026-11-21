# Incident investigation report: INC-012
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The payment-service v6.0.0 deploy is a red herring. The real fault originates in the shared ledger-db: an inactive replication slot ('analytics_cdc') retained tens of GB of WAL starting ~13:02, driving disk usage from 89% (13:00) to 100% by 15:00:00. Once the disk hit 100%, all writers to ledger-db began failing with 'No space left on device', producing ~60% payment-service error rate, ~236 order-service errors, and Postgres PANIC writes — all starting at ~15:00-15:01. Because the fault is at the shared datastore, rolling back payment-service at 15:12 did not and cannot restore service; error rate stayed flat (~62%) for the next ~105 minutes. The deploy at 14:46 merely coincided with a disk that was already 95%+ full. Root cause category: resource_exhaustion at ledger-db (matches historical incident HIST-114).

## Probable root cause
**ledger-db disk exhaustion from retained WAL (inactive replication slot)** -- resource_exhaustion in `ledger-db` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk exhaustion from retained WAL (inactive replication slot) | resource_exhaustion | ledger-db | - | 0.90 (high) | active |
| 2 | H2: Faulty payment-service v6.0.0 mandate flow (new MANDATE_HOLD ledger entry type) | bad_deployment | payment-service | dep-6203 | 0.03 (low) | rejected |
| 3 | H3: payment-service load/capacity saturation | capacity_saturation | payment-service | - | 0.02 (low) | rejected |
| 4 | H4: bank-partner-api (external) degradation | dependency_failure | bank-partner-api | - | 0.01 (low) | rejected |

### H1: ledger-db disk exhaustion from retained WAL (inactive replication slot)
An inactive replication slot 'analytics_cdc' on the shared ledger-db retained tens of GB of WAL, filling the Postgres data volume to 100% by ~15:00. All writers to ledger-db (payment-service, order-service, etc.) then failed with 'No space left on device', producing the payment-service error spike. The v6.0.0 deploy is coincidental; rollback could not help.

**Causal chain**
1. Inactive replication slot 'analytics_cdc' retains WAL continuously from ~13:02 [F8]
2. ledger-db disk fills from 89% (13:00) to 100% (15:00) [F7]
3. Postgres can no longer extend files / write WAL; PANICs begin 15:01 [F6]
4. All ledger-db writers fail: payment-service 'ledger write failed' from 15:00:26, order-service 'INSERT ... failed' from 15:00:42 [F4, F9]
5. payment-service error rate stays ~62% because the datastore is still full; rollback of v6.0.0 changes nothing [F3, F5]

**Supporting evidence**
  - `F6` ✔ ledger-db logged PANIC/ERROR 'could not write to file "pg_wal/xlogtemp...": No space left on device' and 'could not extend file "base/16402/2619": No space left on device' beginning 15:01:01, 292 total ERROR entries through 16:57.
  - `F7` ✔ ledger-db disk_used_pct climbed steadily from 89.18% (13:00) to 98.52% by 14:30 and saturated at 100.0% from the 15:00 bucket onward, remaining 100% through 16:45.
  - `F8` ✔ ledger-db WARN logs show an inactive replication slot retaining tens of GB of WAL starting at 13:02:25 (e.g. 'retaining 33247 MB of WAL'), well before the 14:46 deploy, plus disk-usage warnings 90% (13:10), 95% (14:15), 98% (14:48).
  - `F9` ✔ order-service — a separate service with no deployment in the window — emitted 236 identical errors 'INSERT INTO orders failed: No space left on device' starting 15:00:42, contemporaneous with payment-service's failures.
  - `F10` ✔ ledger-db is a SHARED datastore read/written by order-service, portfolio-service, payment-service and settlement-service.
  - `F12` ✔ ledger-db replication_lag_ms climbed monotonically from ~2.3s (13:00) to ~5.2s (16:45), consistent with WAL accumulation rather than query load; slow_queries_per_min stayed ~1/min.
  - `F13` ✔ Historical incident HIST-114 (2025-04-18, ledger-db) had the identical root cause: 'Inactive replication slot retained WAL until disk filled', category resource_exhaustion.
  - `F3` ✔ payment-service error_rate was flat at ~0.2% until 14:48, then jumped to ~60.85% (max 66.98) in the 15:00 bucket and remained ~60-63% through 16:48 — i.e., it did NOT recover after the 15:12 rollback.
  - `F5` ✔ payment-service continued emitting the same 'No space left on device' ledger-write errors at full rate after the rollback: 320 further errors from 15:12:08 to 16:57:54.
  - `F4` ✔ All 88 payment-service ERROR logs from 15:00:26 to 15:29:30 are a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device'. No mandate-specific or application-logic errors appear.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 'analytics_cdc' slot is genuinely orphaned (no active consumer) and was not drained before 16:59
  - No metric reports ledger-db filesystem separately from disk_used_pct; disk_used_pct is the full data volume

### H2: Faulty payment-service v6.0.0 mandate flow (new MANDATE_HOLD ledger entry type)
The new mandate create/approve endpoints and MANDATE_HOLD ledger entry type introduced a bug that causes ledger write failures, so errors started after the 14:46 deploy and the fix is the 15:12 rollback.

**Causal chain**
1. deploy dep-6203 at 14:46 introduces mandate ledger writes [F1]
2. ledger writes fail [F4]

**Supporting evidence**
  - `F1` ✔ Only one deployment occurred in the window: payment-service v6.0.0 (dep-6203) at 2026-06-01T14:46:00Z, summary 'New UPI mandate flow', which was rolled back at 15:12:00Z.
**Contradicting evidence**
  - `F5` ✔ payment-service continued emitting the same 'No space left on device' ledger-write errors at full rate after the rollback: 320 further errors from 15:12:08 to 16:57:54.
  - `F4` ✔ All 88 payment-service ERROR logs from 15:00:26 to 15:29:30 are a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device'. No mandate-specific or application-logic errors appear.
  - `F9` ✔ order-service — a separate service with no deployment in the window — emitted 236 identical errors 'INSERT INTO orders failed: No space left on device' starting 15:00:42, contemporaneous with payment-service's failures.
  - `F7` ✔ ledger-db disk_used_pct climbed steadily from 89.18% (13:00) to 98.52% by 14:30 and saturated at 100.0% from the 15:00 bucket onward, remaining 100% through 16:45.
  - `F3` ✔ payment-service error_rate was flat at ~0.2% until 14:48, then jumped to ~60.85% (max 66.98) in the 15:00 bucket and remained ~60-63% through 16:48 — i.e., it did NOT recover after the 15:12 rollback.
**Rejected because:** Errors persisted at the same ~62% rate for 105+ min after the 15:12 rollback (F5), no mandate-specific errors exist (F4 — only 'No space left on device'), a non-deployed service (order-service) failed identically at the same time (F9), and ledger-db was already 95%+ full before the deploy (F7). The error text is a datastore-level disk error, not application logic.

### H3: payment-service load/capacity saturation
A surge in payment requests saturated payment-service CPU/connection pools, causing the error spike.

**Causal chain**
1. load surge saturates payment-service *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ payment-service internal health signals were flat/healthy: cpu_pct ~30%, db_pool_in_use ~8, bank_api_error_rate ~0.2%, request_rate ~100 req/s, and restarts stayed 0 throughout.
  - `F4` ✔ All 88 payment-service ERROR logs from 15:00:26 to 15:29:30 are a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device'. No mandate-specific or application-logic errors appear.
**Rejected because:** request_rate flat ~100 req/s (F11), cpu_pct flat ~30% (F11), db_pool_in_use flat ~8 (F11), and errors are datastore disk errors not timeouts/queue-full (F4).

### H4: bank-partner-api (external) degradation
The external bank partner degraded, causing payment failures.

**Causal chain**
1. bank-partner-api errors propagate to payment-service *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ payment-service internal health signals were flat/healthy: cpu_pct ~30%, db_pool_in_use ~8, bank_api_error_rate ~0.2%, request_rate ~100 req/s, and restarts stayed 0 throughout.
  - `F4` ✔ All 88 payment-service ERROR logs from 15:00:26 to 15:29:30 are a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device'. No mandate-specific or application-logic errors appear.
**Rejected because:** bank_api_error_rate was flat at 0.2% throughout (F11), and error messages reference the ledger, not the bank API.

## Facts (verified observations)
- Deploy dep-6203 (payment-service v6.0.0) ran at 14:46 and was rolled back at 15:12; no config/flag changes occurred. [F1, F2]
- ledger-db disk usage rose from 89% at 13:00 to 100% by 15:00 and stayed at 100% through 16:45. [F7]
- ledger-db logged that an inactive replication slot 'analytics_cdc' retained tens of GB of WAL from 13:02, with disk warnings at 90/95/98% before the deploy. [F8]
- Postgres began failing file/WAL writes with 'No space left on device' at 15:01:01 (292 ERROR entries). [F6]
- payment-service errors (all 'ledger write failed ... No space left on device') began 15:00:26 and continued at full rate until 16:57, unaffected by the 15:12 rollback. [F3, F4, F5]
- order-service (no deploy in window) emitted 236 identical 'No space left on device' INSERT failures from 15:00:42. [F9]
- ledger-db is a shared datastore used by order-service, portfolio-service, payment-service and settlement-service. [F10]
- payment-service internal metrics (cpu, db pool, bank API error rate, request rate, restarts) were flat and healthy throughout. [F11]
- ledger-db replication lag climbed monotonically from ~2.3s to ~5.2s while slow queries stayed ~1/min. [F12]
- Historical incident HIST-114 documents the same root cause: inactive replication slot retaining WAL until disk filled. [F13]

## Assumptions (not verified)
- The 'analytics_cdc' replication slot is orphaned and had no active consumer during 13:00-17:00.
- disk_used_pct reflects the Postgres data filesystem that contains pg_wal and base/ tablespaces.
- The 60% payment error rate equals approximately the fraction of requests that perform ledger writes.

## Timeline
- `2026-06-01T13:02:25Z` ledger-db warns replication slot 'analytics_cdc' inactive, retaining 33 GB WAL [F8]
- `2026-06-01T13:10:01Z` ledger-db disk usage warning at 90% [F8]
- `2026-06-01T14:15:45Z` ledger-db disk usage warning at 95% [F8]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (dep-6203) deployed [F1]
- `2026-06-01T14:48:20Z` ledger-db disk usage warning at 98% [F8]
- `2026-06-01T15:00:00Z` ledger-db disk reaches 100%; payment-service error rate jumps to ~61% [F7, F3]
- `2026-06-01T15:00:26Z` First payment-service 'ledger write failed ... No space left on device' [F4]
- `2026-06-01T15:00:42Z` First order-service 'INSERT INTO orders failed: No space left on device' [F9]
- `2026-06-01T15:01:01Z` ledger-db Postgres PANIC on pg_wal write ('No space left on device') [F6]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rolled back — error rate does NOT recover [F1, F3, F5]
- `2026-06-01T16:57:54Z` Last payment-service disk-error log; error rate still ~62% at 16:48 [F5, F3]

## Recommended actions
- **[immediate]** Free disk on ledger-db immediately: drop or restore the inactive 'analytics_cdc' replication slot to release retained WAL, and/or expand the data volume. -- The datastore is at 100% disk; the slot's retained WAL is the filler and clearing it is the direct remediation. [F8, F7, F6]
- **[immediate]** Do NOT expect the payment-service v6.0.0 rollback to restore service; verify recovery by monitoring ledger-db disk and writer error rates instead. -- Errors persisted unchanged for 105+ min after the 15:12 rollback, proving the deploy was not the cause. [F5, F3]
- **[short_term]** The v6.0.0 mandate flow is not implicated — re-deploy/keep it rather than treating it as the culprit. -- No mandate-specific errors and identical failures in a non-deployed service; the change is exonerated. [F4, F9]
- **[short_term]** Add alerting on ledger-db disk_used_pct (warn at 80%, page at 90%) and on inactive replication slots / retained WAL, and add automation to drop or alert on orphaned slots. -- Disk climbed gradually for 2 hours with only log warnings; an alert would have enabled earlier action. [F7, F8]
- **[long_term]** Audit all replication slots and CDC consumers (e.g. analytics_cdc) for liveness and add a WAL-retention cap (max_slot_wal_keep_size). -- Recurrence of HIST-114 shows a systemic gap in slot lifecycle management. [F13, F8]

## Data gaps
- No metric distinguishes general disk usage from WAL-specific space on ledger-db; the WAL retention size is only available via log warnings.
- No telemetry exists for bank-partner-api (external node has_telemetry=false), though its error rate via payment-service was flat.
- partial=true results for some log queries (e.g. payment-service and ledger-db ERROR sets truncated at 30-40 of 88-320), so full error text variety was only inferred from the single dominant pattern.
- No evidence of who/what owns the 'analytics_cdc' slot or when it became inactive before 13:02.

## Investigation stats
- latency 63.43s, LLM calls 7, tokens 88103 (in 79247 / out 8856)
- tool calls 24 (1 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
