# Incident investigation report: INC-001
*System: multi_agent | outcome: **root_cause_identified** | ended: budget_exhausted:deadline*

## Summary
The investigation could not definitively attribute the 500 errors to the recent order-service deployment (v2.14.0). While the deployment (F6, F18) and the NullPointerException signature (F3, F20) are verified, there is insufficient evidence to confirm that the validator lacks a null check or that the null marginProfile originates from missing data. Consequently, the root cause remains ambiguous between a code regression, missing marginProfile data, or a downstream dependency failure. Immediate mitigation and targeted data collection are required to resolve the uncertainty.

## Probable root cause
**Bug introduced by order-service deployment v2.14.0** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bug introduced by order-service deployment v2.14.0 | bad_deployment | order-service | dep-5101 | 0.60 (medium) | active |
| 2 | H2: Missing marginProfile data for some accounts (data issue) | data_issue | ledger-db / market-data-service (data store) | null | 0.45 (low) | active |
| 3 | H3: Downstream service failure returning null marginProfile (dependency failure) | dependency_failure | market-data-service | null | 0.30 (low) | active |

### H1: Bug introduced by order-service deployment v2.14.0
The deployment of order-service version v2.14.0 (deployment id dep-5101) added MarginCheckValidator and removed legacy OrderRules.validateMargin() without proper null handling, causing a NullPointerException when marginProfile is null, which leads to HTTP 500 responses for order placement.

**Causal chain**
1. Deployment dep-5101 introduced MarginCheckValidator that accesses marginProfile without null check [F6]
2. When marginProfile is null for certain accounts, the validator throws NullPointerException [F3]
3. NullPointerException propagates up to HTTP handler, resulting in 500 response *(assumed)*
4. Spike in 500 errors observed after deployment [F1, F2]

**Supporting evidence**
  - `F1` ✔ Order-service error_rate spiked from ~0.2% before 12:12 UTC to ~18% after 12:12 UTC.
  - `F2` ✔ The first error log with HTTP 500 in order-service occurred at 2026-03-03T12:12:44Z.
  - `F3` ✔ All error logs in the examined window share the same signature: "order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>".
  - `F6` ✔ order-service was deployed version v2.14.0 at 2026-03-03T12:10:00Z (deployment id dep-5101) with changes adding MarginCheckValidator and removing legacy OrderRules.validateMargin().
  - `F8` ✔ No configuration changes were recorded for order-service in the last 24 hours.
**Contradicting evidence**
  - (none)
**Assumptions**
  - MarginCheckValidator does not guard against null marginProfile; legacy code previously handled this case.

### H2: Missing marginProfile data for some accounts (data issue)
A subset of account records lack a populated marginProfile in the data store (ledger-db or market-data-service). The new validator expects this field, and when it is absent it triggers a NullPointerException, causing the 500 errors.

**Causal chain**
1. Some accounts have null marginProfile in the data store *(assumed)*
2. MarginCheckValidator accesses marginProfile and throws NullPointerException when null [F3]
3. Exception propagates to order-service HTTP layer, returning 500 *(assumed)*
4. Observed spike in 500 errors after the validator was deployed [F1, F2]

**Supporting evidence**
  - `F3` ✔ All error logs in the examined window share the same signature: "order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>".
  - `F1` ✔ Order-service error_rate spiked from ~0.2% before 12:12 UTC to ~18% after 12:12 UTC.
  - `F2` ✔ The first error log with HTTP 500 in order-service occurred at 2026-03-03T12:12:44Z.
**Contradicting evidence**
  - (none)
**Assumptions**
  - MarginProfile is stored in ledger-db or fetched from market-data-service; recent data ingestion did not populate it for certain accounts.

### H3: Downstream service failure returning null marginProfile (dependency failure)
A downstream dependency (e.g., market-data-service or redis-cache) failed to provide a marginProfile for some requests, returning null. The new validator in order-service does not handle the null, leading to NullPointerException and HTTP 500 responses.

**Causal chain**
1. Market-data-service returned null marginProfile for certain accounts *(assumed)*
2. MarginCheckValidator receives null and throws NullPointerException [F3]
3. Exception bubbles up to order-service, causing 500 responses *(assumed)*
4. Spike in 500 errors observed after deployment of validator [F1, F2]

**Supporting evidence**
  - `F3` ✔ All error logs in the examined window share the same signature: "order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>".
  - `F1` ✔ Order-service error_rate spiked from ~0.2% before 12:12 UTC to ~18% after 12:12 UTC.
  - `F2` ✔ The first error log with HTTP 500 in order-service occurred at 2026-03-03T12:12:44Z.
**Contradicting evidence**
  - `F4` ✔ Payment-service error_rate remained low (~0.2%) throughout the period, showing no comparable spike.
  - `F5` ✔ API-gateway logged zero ERROR entries containing "500" during the same window, indicating no similar spike.
**Assumptions**
  - Market-data-service is a downstream of order-service and provides marginProfile; no errors were logged for it in the observed window.

## Facts (verified observations)
- Order-service error rate spiked from ~0.2% to ~18% at 12:12 UTC. [F1]
- First 500 error log in order-service recorded at 12:12:44 UTC. [F2]
- All error logs share the signature "java.lang.NullPointerException: marginProfile is null for account <n>". [F3, F20]
- Payment-service error rate remained low, showing no comparable spike. [F4]
- API-gateway logged zero ERROR entries containing "500" during the incident window. [F5]
- order-service was deployed version v2.14.0 at 12:10 UTC (deployment id dep-5101) adding MarginCheckValidator and removing legacy OrderRules.validateMargin(). [F6, F18]
- No configuration changes were recorded for order-service in the last 24 hours. [F8, F19]
- No deployments or config changes were recorded for downstream services (ledger-db, pricing-engine, exchange-gateway, market-data-service, redis-cache) in the incident window. [F14, F15]
- Ledger-db logs contain no entries for the affected account IDs or the term "marginProfile" during the window. [F21, F22]
- Market-data-service logs contain no WARN or higher entries mentioning "marginProfile" and only two generic WARN entries unrelated to marginProfile. [F24, F25]
- Redis-cache produced no WARN or higher logs mentioning "marginProfile". [F26]

## Assumptions (not verified)
- MarginCheckValidator accesses marginProfile without a null guard.
- Legacy OrderRules.validateMargin() previously handled null marginProfile safely.
- Missing marginProfile data for some accounts could cause the observed NPE.
- Downstream services (e.g., market-data-service) could return null marginProfile due to a failure.

## Timeline
- `2026-03-03T12:10:00Z` order-service deployed version v2.14.0 (dep-5101) adding MarginCheckValidator and removing legacy validator. [F6, F18]
- `2026-03-03T12:12:00Z` Order-service error rate spikes to ~18%. [F1]
- `2026-03-03T12:12:44Z` First 500 error log recorded; NPE signature observed. [F2, F3, F20]
- `2026-03-03T12:12:44Z - 2026-03-03T13:59:00Z` Continued high error rate; no similar spikes in payment-service or API-gateway. [F4, F5]

## Recommended actions
- **[immediate]** Rollback order-service to the previous stable version (pre‑v2.14.0) to immediately stop the 500 errors. -- The deployment coincides with the error spike and removing the new validator eliminates the suspected code path. [F6, F1]
- **[immediate]** If rollback is not feasible, disable MarginCheckValidator via feature flag or configuration. -- Temporarily removes the code path that triggers the NPE while preserving the deployment. [F6]
- **[short_term]** Retrieve the source code diff for the v2.14.0 changes to confirm whether marginProfile is accessed without null‑check. -- Directly validates the core assumption of H1 and distinguishes it from data‑issue hypotheses.
- **[short_term]** Query ledger-db and market-data-service for the actual marginProfile values of the accounts appearing in error logs. -- Determines if the null originates from missing data (supporting H2) or from a downstream failure (supporting H3). [F21, F22, F23]
- **[short_term]** Increase logging in order-service around the MarginCheckValidator to capture the source of marginProfile (e.g., log the data source before access). -- Provides observable evidence of whether the null comes from a downstream call.
- **[long_term]** Add a defensive null‑check for marginProfile in MarginCheckValidator and redeploy after testing. -- Prevents similar NPEs regardless of data quality, improving robustness.
- **[long_term]** Implement monitoring alerts for NullPointerException on marginProfile and for missing marginProfile fields in downstream services. -- Early detection of similar issues in future deployments.

## Data gaps
- Source code diff for the v2.14.0 changes (including null‑check presence) was not accessible.
- Detailed logs from ledger-db and market-data-service for the specific account IDs were unavailable, preventing confirmation of missing marginProfile data.
- No logs were retrieved from inventory-service, which could be another upstream source for order requests.
- Potential configuration changes for downstream services (pricing-engine, exchange-gateway, ledger-db, kafka) were not queried.
- Higher‑severity logs (ERROR) from market-data-service were not examined, possibly missing relevant null‑pointer messages.

## What would change this conclusion
- Evidence that the deployed MarginCheckValidator accesses marginProfile without a null guard (e.g., code diff showing missing check).
- Logs from ledger-db or market-data-service confirming that marginProfile is null or missing for the affected accounts.
- Observations of a downstream service returning a null marginProfile (e.g., error logs from market-data-service indicating null returns).
- A successful rollback or disabling of the validator that eliminates the 500 errors, confirming the code change as the root cause.

## Integrity warnings (set by code)
- reporter wrote outcome=inconclusive; overridden to root_cause_identified by rule (top hypothesis is above threshold and clearly ahead)

## Investigation stats
- latency 525.36s, LLM calls 36, tokens 127695 (in 108280 / out 19415)
- tool calls 25 (1 failed, 0 partial, 0 recovered by retry), findings 23/26 verified
