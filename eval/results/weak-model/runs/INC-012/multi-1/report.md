# Incident investigation report: INC-012
*System: multi_agent | outcome: **inconclusive** | ended: budget_exhausted:deadline*

## Summary
The incident is characterized by a rapid increase in payment-service errors with a consistent "ledger write failed: No space left on device" signature. While the evidence points to a storage exhaustion condition, the exact component (payment-service host vs. ledger-db) and root cause remain unverified, leading to an inconclusive conclusion. Immediate mitigation via rollback and disk space remediation is recommended, alongside targeted data collection to resolve the uncertainty.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|

## Facts (verified observations)
- payment-service calls downstream ledger-db (PostgreSQL), Kafka, and makes HTTP calls to bank-partner-api. [F1]
- payment-service receives HTTP requests from api-gateway. [F2]
- The first ERROR log from payment-service appears at 2026-06-01T15:00:26Z. [F4]
- All sampled error logs contain the signature "ledger write failed: ERROR: could not extend file: No space left on device". [F5]
- payment-service error_rate metric jumps from ~0.2% to 61.52% (max 66.98%) in the 15:00‑15:05 bucket. [F6]
- CPU usage for payment-service remains stable around 30% during the incident. [F7]

## Assumptions (not verified)
- The "No space left on device" error originates from either the local filesystem of the payment-service pod or the storage backing ledger-db, but this has not been directly verified.
- The new ledger entry type introduced in payment-service v6.0.0 may increase disk usage, contributing to the observed condition.

## Timeline
- `2026-06-01T15:00:00Z` payment-service error_rate spikes to >60% (F6). CPU usage remains stable (F7). [F6, F7]
- `2026-06-01T15:00:26Z` First ERROR log recorded in payment-service (F4) with "ledger write failed: No space left on device" signature (F5). [F4, F5]
- `2026-06-01T15:10:00Z` Incident reported by on-call SRE; dependencies of payment-service identified (F1, F2). [F1, F2]

## Recommended actions
- **[immediate]** Complete rollback of payment-service to the previous stable version (v5.x) to stop generating the new ledger entry type. -- Error spike began shortly after v6.0.0 release; rolling back removes the new code path that may be causing excessive disk writes.
- **[immediate]** Free up or increase disk capacity on the ledger-db host (or the payment-service pod filesystem) to eliminate the "No space left on device" condition. -- All observed errors are storage‑related (F5). Restoring available space should allow ledger writes to succeed.
- **[short_term]** Collect disk usage metrics and filesystem logs from ledger-db and payment-service nodes to identify which component is out of space. -- Data gap: missing verification of the storage source of the error (see Data Gaps).
- **[short_term]** Retrieve memory usage, DB connection pool, and request latency metrics for payment-service during the incident window. -- These metrics were not gathered (GAPS) and could reveal additional resource pressures.
- **[short_term]** Gather error logs from ledger-service, settlement-service, order-service, and any other services sharing ledger-db or Kafka. -- Missing logs (GAPS) prevent ruling out correlated failures in shared components.
- **[short_term]** Audit recent configuration changes and deployments for payment-service and its dependencies (e.g., schema migrations, storage quota changes). -- Task T2 could not be completed; knowing recent changes may pinpoint the cause.
- **[long_term]** Implement proactive disk‑space alerts and capacity‑planning dashboards for ledger-db and payment-service pods. -- Prevents recurrence of storage‑exhaustion incidents.
- **[long_term]** Add canary testing for new ledger entry types with monitoring of storage impact before full rollout. -- Ensures future changes do not unintentionally fill disks.

## Data gaps
- Memory usage, database connection pool usage, and request latency metrics for payment-service were not retrieved, limiting assessment of overall resource saturation.
- Error logs from services other than payment-service (e.g., settlement-service, order-service) were not collected, preventing confirmation of whether the issue is isolated.
- Ledger-service logs could not be obtained due to unknown service name, leaving the source of the "no space left on device" error unverified.
- List of deployments and configuration changes for payment-service and its immediate dependencies around the incident window was not obtained (task T2).

## What would change this conclusion
- If ledger-db logs show sufficient free space and no disk‑full errors, the root cause would shift away from storage exhaustion on the database.
- If other services sharing ledger-db report identical "no space left on device" errors, the incident would be attributed to a shared component failure rather than payment-service code.
- If a recent configuration change (e.g., increased log retention, schema migration) is found to have filled the disk, that change would become the primary root cause.

## Investigation stats
- latency 681.12s, LLM calls 11, tokens 32932 (in 26182 / out 6750)
- tool calls 6 (1 failed, 0 partial, 0 recovered by retry), findings 6/8 verified
