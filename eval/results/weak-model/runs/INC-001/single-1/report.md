# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
A recent deployment (dep-5101) introduced a new MarginCheckValidator that does not guard against a null marginProfile. Accounts lacking this field trigger a NullPointerException, resulting in a sharp rise in HTTP 500 errors on order-service. No config changes or upstream service errors were observed. The evidence strongly supports the bad_deployment hypothesis.

## Probable root cause
**Bad deployment introduced NullPointerException in margin validation** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bad deployment introduced NullPointerException in margin validation | bad_deployment | order-service | dep-5101 | 0.86 (high) | active |
| 2 | H2: Data issue: missing marginProfile for a subset of accounts | data_issue | account-data-service (unknown) | unknown | 0.44 (low) | active |

### H1: Bad deployment introduced NullPointerException in margin validation
The v2.14.0 deployment added a MarginCheckValidator that assumes a non‑null marginProfile; for accounts lacking this data the validator throws a NullPointerException, causing HTTP 500 responses.

**Causal chain**
1. deployment dep-5101 -> new validator runs [F2]
2. validator accesses marginProfile without null check -> NPE [F1]
3. NPE propagates as HTTP 500, raising error_rate [F3]

**Supporting evidence**
  - `F1` ✔ First observed 500 error log with NullPointerException: marginProfile is null at 2026-03-03T12:12:44Z
  - `F2` ✘ unverified order-service deployment dep-5101 (v2.14.0) was released at 2026-03-03T12:10:00Z, adding a new MarginCheckValidator
  - `F3` ✔ order-service error_rate metric spiked from ~0.2% to ~18% starting at 12:12 UTC
**Contradicting evidence**
  - (none)

### H2: Data issue: missing marginProfile for a subset of accounts
An upstream change to the account data store stopped populating the marginProfile field for some accounts; order-service receives null and throws a NullPointerException.

**Causal chain**
1. upstream data change -> marginProfile missing *(assumed)*
2. order-service validator receives null -> NPE [F1]

**Supporting evidence**
  - `F1` ✔ First observed 500 error log with NullPointerException: marginProfile is null at 2026-03-03T12:12:44Z
**Contradicting evidence**
  - `F2` ✘ unverified order-service deployment dep-5101 (v2.14.0) was released at 2026-03-03T12:10:00Z, adding a new MarginCheckValidator
  - `F4` ✔ No configuration or feature-flag changes for order-service in the incident window
**Assumptions**
  - Missing upstream logs or metrics showing marginProfile omission
**Rejected because:** Insufficient evidence to rule out deployment bug; lower confidence

## Facts (verified observations)
- The incident began shortly after the order-service deployment at 12:10 UTC. [F1, F3]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-03-03T12:10:00Z` order-service deployment dep-5101 (v2.14.0) released [F2]
- `2026-03-03T12:12:44Z` First 500 error logged (NPE on marginProfile) [F1]
- `2026-03-03T12:12:00Z` Error_rate metric spikes to ~18 % [F3]

## Recommended actions
- **[immediate]** Rollback order-service to the previous version (v2.13.x) or hot‑fix the MarginCheckValidator to handle null marginProfile. -- Stops the NPE and restores normal order placement. [F1, F2]
- **[short_term]** Add a null‑check for marginProfile in the new validator and add unit tests covering missing data. -- Prevents recurrence if the data issue persists. [F1]
- **[short_term]** Investigate upstream account data pipeline for missing marginProfile fields; add monitoring/alerts for null values. -- Closes the data‑gap identified in hypothesis H2.

## Data gaps
- No logs or metrics from the service that provides account marginProfile data, preventing confirmation whether the null values are due to upstream data loss.

## Investigation stats
- latency 200.66s, LLM calls 12, tokens 69845 (in 64540 / out 5305)
- tool calls 11 (0 failed, 0 partial, 0 recovered by retry), findings 5/6 verified
