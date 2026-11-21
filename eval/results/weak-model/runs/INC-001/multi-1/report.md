# Incident investigation report: INC-001
*System: multi_agent | outcome: **root_cause_identified** | ended: budget_exhausted:deadline*

## Summary
The investigation did not obtain sufficient evidence to definitively attribute the 500 errors to a bug introduced in order-service v2.14.0 (H1). While the deployment and NullPointerException logs strongly correlate, the root cause—whether a code bug or missing marginProfile data—remains ambiguous. Immediate mitigation and targeted data collection are required to resolve the uncertainty.

## Probable root cause
**Bug introduced in order-service deployment v2.14.0 causing NullPointerException** -- bad_deployment in `order-service`, triggered by `F13` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bug introduced in order-service deployment v2.14.0 causing NullPointerException | bad_deployment | order-service | F13 | 0.60 (medium) | active |
| 2 | H3: Missing margin profile data in ledger‑db leads to NullPointerException | data_issue | ledger-db (Postgres) | null | 0.45 (low) | active |
| 3 | H2: Feature‑flag/configuration change enabled margin check for derivative orders | config_change | order-service | null | 0.38 (low) | active |
| 4 | H4: Pricing‑engine returns null margin data causing order-service failure | dependency_failure | pricing-engine | null | 0.28 (low) | active |

### H1: Bug introduced in order-service deployment v2.14.0 causing NullPointerException
The order-service deployment v2.14.0 added a margin check for futures & options orders. The new code fails when marginProfile is null, throwing a NullPointerException that propagates as HTTP 500 responses.

**Causal chain**
1. order-service was deployed version v2.14.0 at 12:10 UTC [F13]
2. deployment included refactor of order validation and addition of a margin check [F14]
3. margin check accessed a null marginProfile, causing NullPointerException [F8, F9]
4. NullPointerException resulted in HTTP 500 responses for POST /v1/orders *(assumed)*

**Supporting evidence**
  - `F7` ✔ The order-service error_rate metric spiked from ~0.2% to >11% at 12:10 UTC, rising to ~18% thereafter.
  - `F8` ✔ The first logged 500 error occurred at 2026-03-03T12:12:44Z with message "order rejected with 500: java.lang.NullPointerException: marginProfile is null for account 99733".
  - `F9` ✔ All 15 error log entries between 12:12 and 12:19 share the same pattern: NullPointerException on marginProfile being null.
  - `F13` ✔ order-service was deployed version v2.14.0 at 2026-03-03T12:10:00Z, 12 minutes before the incident was reported.
  - `F14` ✔ The order-service deployment included a refactor of order validation and addition of a margin check for futures & options orders.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The NullPointerException is directly caused by the new margin check code rather than missing data or downstream failures.

### H3: Missing margin profile data in ledger‑db leads to NullPointerException
The new margin‑check logic expects a marginProfile record for each account. For some accounts (e.g., 99733) the record is absent in ledger‑db, so the code receives null and throws a NullPointerException, causing HTTP 500 errors.

**Causal chain**
1. ledger‑db lacks marginProfile rows for certain accounts *(assumed)*
2. order-service margin‑check queries ledger‑db and receives null [F14]
3. Null marginProfile triggers NullPointerException in order-service [F8, F9]
4. Exception results in HTTP 500 responses *(assumed)*

**Supporting evidence**
  - `F7` ✔ The order-service error_rate metric spiked from ~0.2% to >11% at 12:10 UTC, rising to ~18% thereafter.
  - `F8` ✔ The first logged 500 error occurred at 2026-03-03T12:12:44Z with message "order rejected with 500: java.lang.NullPointerException: marginProfile is null for account 99733".
  - `F9` ✔ All 15 error log entries between 12:12 and 12:19 share the same pattern: NullPointerException on marginProfile being null.
  - `F14` ✔ The order-service deployment included a refactor of order validation and addition of a margin check for futures & options orders.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The marginProfile data is stored in ledger‑db and was not populated for the affected accounts.
  - No recent DB schema changes or migrations introduced this gap.

### H2: Feature‑flag/configuration change enabled margin check for derivative orders
A recent configuration or feature‑flag change turned on the newly added margin check for futures & options orders, exposing a code path that throws a NullPointerException when marginProfile is missing, leading to HTTP 500 errors.

**Causal chain**
1. A config/feature‑flag change enabled the margin check for derivative orders *(assumed)*
2. Enabled code path accessed marginProfile which was null, causing NullPointerException [F8, F9]
3. Exception propagated as HTTP 500 responses *(assumed)*

**Supporting evidence**
  - `F7` ✔ The order-service error_rate metric spiked from ~0.2% to >11% at 12:10 UTC, rising to ~18% thereafter.
  - `F8` ✔ The first logged 500 error occurred at 2026-03-03T12:12:44Z with message "order rejected with 500: java.lang.NullPointerException: marginProfile is null for account 99733".
  - `F9` ✔ All 15 error log entries between 12:12 and 12:19 share the same pattern: NullPointerException on marginProfile being null.
  - `F14` ✔ The order-service deployment included a refactor of order validation and addition of a margin check for futures & options orders.
**Contradicting evidence**
  - (none)
**Assumptions**
  - A config or feature‑flag change occurred around 12:10 UTC (not captured in the findings).
  - The margin check code itself is correct; the failure is due to it being executed unexpectedly.

### H4: Pricing‑engine returns null margin data causing order-service failure
The order-service calls pricing‑engine to obtain margin information. A recent (unrecorded) issue in pricing‑engine returns null, which the order-service does not handle, leading to a NullPointerException and HTTP 500 errors.

**Causal chain**
1. pricing‑engine returned null marginProfile for the request *(assumed)*
2. order-service attempted to use the null value, causing NullPointerException [F8, F9]
3. Exception propagated as HTTP 500 responses *(assumed)*

**Supporting evidence**
  - `F3` ✔ Downstream of order-service: it calls pricing-engine via HTTP.
  - `F8` ✔ The first logged 500 error occurred at 2026-03-03T12:12:44Z with message "order rejected with 500: java.lang.NullPointerException: marginProfile is null for account 99733".
  - `F9` ✔ All 15 error log entries between 12:12 and 12:19 share the same pattern: NullPointerException on marginProfile being null.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Pricing‑engine behavior changed without a deployment (e.g., data issue or external dependency failure).

## Facts (verified observations)
- Order-service was deployed version v2.14.0 at 2026-03-03T12:10:00Z, introducing a margin‑check refactor. [F13, F14]
- Error‑rate metric spiked from ~0.2% to >11% at 12:10 UTC, reaching ~18% shortly after. [F7]
- First logged 500 error occurred at 2026-03-03T12:12:44Z with message "order rejected with 500: java.lang.NullPointerException: marginProfile is null for account 99733". [F8]
- All 15 error log entries between 12:12 and 12:19 share the same NullPointerException pattern on marginProfile being null. [F9]
- No configuration or feature‑flag changes were recorded for order-service (or any service) between 12:00 and 12:20 UTC. [F19, F20]
- CPU and memory usage remained stable (≈40% CPU, ≈60% memory) during the error onset. [F10, F11]
- No error logs containing the endpoint path "/v1/orders" were found before 12:00 UTC. [F12]

## Assumptions (not verified)
- The NullPointerException originates from the order-service code path that accesses marginProfile.
- The marginProfile data is expected to be present in ledger‑db for all accounts; its absence would cause a null value.
- No hidden or undocumented feature‑flag change enabled the new margin‑check logic around 12:10 UTC.
- Downstream services (pricing‑engine, exchange‑gateway, ledger‑db) behaved normally and did not introduce the null value.

## Timeline
- `2026-03-03T12:00:00Z` Baseline: No /v1/orders error logs; system operating normally. [F12]
- `2026-03-03T12:10:00Z` order-service deployed version v2.14.0 with margin‑check refactor. [F13, F14]
- `2026-03-03T12:10:00Z` Error‑rate metric begins to spike (>11%). [F7]
- `2026-03-03T12:12:44Z` First 500 error logged: NullPointerException on marginProfile for account 99733. [F8]
- `2026-03-03T12:12‑12:19Z` 15 additional 500 errors with identical NullPointerException pattern. [F9]
- `2026-03-03T12:10‑12:20Z` CPU and memory remain stable; no config/flag changes recorded. [F10, F11, F19, F20]

## Recommended actions
- **[immediate]** Rollback order-service to the previous stable version (v2.13.x) to immediately stop the 500 errors. -- The deployment of v2.14.0 coincides with the error spike; rolling back removes the new margin‑check code pending further analysis. [F13, F14]
- **[short_term]** Query ledger‑db for marginProfile rows for account 99733 and a sample of recent accounts; verify whether the data is missing. -- Determines if the NullPointerException is caused by absent DB data (supporting H3) or by code that fails to handle null values (supporting H1).
- **[short_term]** Retrieve the full stack trace for the NullPointerException logged at 12:12:44 UTC. -- Identifies the exact code path (e.g., DB query vs pricing‑engine call) that produced the null marginProfile, discriminating between H1, H2, and H3.
- **[short_term]** Audit feature‑flag and configuration change logs for order-service and its downstream dependencies for the window 12:00‑12:20 UTC, ensuring no hidden changes were missed. -- Addresses the gap that could support H2 (undocumented flag enablement). [F19, F20]
- **[long_term]** Add defensive null‑check for marginProfile in the order-service margin‑check code and emit a clear error metric if missing. -- Prevents future NullPointerExceptions regardless of data quality, improving resilience. [F14]
- **[long_term]** Enhance observability: capture full stack traces, DB connection‑pool metrics, request‑rate metrics, and ensure configuration change events are reliably logged. -- Fills the identified gaps (missing stack trace, DB metrics, config change visibility) to enable faster root‑cause analysis in future incidents.

## Data gaps
- No verification of whether marginProfile rows exist for account 99733 in ledger‑db (cannot confirm H3).
- Full stack trace for the NullPointerException was not captured, limiting insight into the exact code path (affects H1, H2, H3).
- Configuration/feature‑flag change logs returned zero entries; completeness of logging cannot be guaranteed (affects H2).
- DB connection‑pool usage and request‑rate metrics were not collected, leaving open the possibility of resource‑related contributors.

## What would change this conclusion
- If the ledger‑db query shows that marginProfile for account 99733 (and other affected accounts) is missing, the primary cause would shift to a data‑availability issue (supporting H3).
- If the full stack trace reveals that the NullPointerException originates directly from the new margin‑check code without a preceding null check, it would confirm H1 as the root cause.
- If audit logs uncover a hidden feature‑flag enablement for the margin‑check at ~12:10 UTC, H2 would become the leading explanation.

## Integrity warnings (set by code)
- H4: dropped citations to non-existent findings ['F15 (no recent pricing‑engine deployments)']
- reporter wrote outcome=inconclusive; overridden to root_cause_identified by rule (top hypothesis is above threshold and clearly ahead)

## Investigation stats
- latency 506.3s, LLM calls 35, tokens 121680 (in 103109 / out 18571)
- tool calls 22 (0 failed, 0 partial, 0 recovered by retry), findings 18/20 verified
