# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order placement 500s began at 12:12:31Z, ~2 minutes after order-service deployment dep-5101 (v2.14.0, 12:10Z) which 'Refactor order validation; add margin check for F&O orders', introducing a new MarginCheckValidator and removing legacy OrderRules.validateMargin(). Error rate jumped from ~0.22% to ~18% (14.74% in the 12:10 bucket) and stayed there. All errors are NullPointerException at MarginCheckValidator.java:88 with 'marginProfile is null' on orderType=FNO (derivatives), matching the support tickets. Latency, CPU, memory, db pool and request rate are flat, and there are no errors in downstream deps (pricing-engine, exchange-gateway, market-data-service, ledger-db) and no order-service config change, ruling out resource, capacity, dependency and config causes. Root cause: a null-dereference regression in the newly deployed validation path in order-service v2.14.0.

## Probable root cause
**dep-5101 (order-service v2.14.0) introduced a null-dereference regression in MarginCheckValidator for F&O orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: dep-5101 (order-service v2.14.0) introduced a null-dereference regression in MarginCheckValidator for F&O orders | bad_deployment | order-service | dep-5101 | 0.90 (high) | active |
| 2 | H2: Upstream margin-profile data (ledger-db) became null/incomplete, causing the new strict validator to fail | data_issue | ledger-db | - | 0.08 (low) | active |
| 3 | H3: Resource exhaustion / capacity saturation in order-service | resource_exhaustion | order-service | - | 0.02 (low) | rejected |

### H1: dep-5101 (order-service v2.14.0) introduced a null-dereference regression in MarginCheckValidator for F&O orders
The v2.14.0 refactor of order validation added a new MarginCheckValidator and removed the legacy OrderRules.validateMargin(); the new validator dereferences marginProfile (line 88) which is null for some/all F&O (derivatives) orders, throwing NullPointerException that surfaces as HTTP 500. The fault originates in order-service's newly deployed code.

**Causal chain**
1. dep-5101 deployed at 12:10:00Z adds MarginCheckValidator and removes legacy margin validation [F2, F3]
2. New validator dereferences null marginProfile at line 88 for orderType=FNO [F4, F5, F11]
3. NPE converts to HTTP 500 on POST /v1/orders [F4, F10]
4. No order-service config change and no downstream errors -> fault is in the release, not config/deps [F8, F9]

**Supporting evidence**
  - `F1` ✔ order-service error_rate was ~0.21-0.24% for all buckets 11:30-12:00, then jumped to 14.74% in the 12:10 bucket and remained ~18.3-18.8% through 13:50.
  - `F2` ✔ A single deployment dep-5101 for order-service v2.14.0 occurred at 2026-03-03T12:10:00Z by r.iyer with summary 'Refactor order validation; add margin check for F&O orders' and changes 'new MarginCheckValidator in validation chain', 'remove legacy OrderRules.validateMargin()'. Status live, never rolled back.
  - `F3` ✔ order-service logs show the deployment going live at 12:10:08Z ('deployment dep-5101 (v2.14.0) rollout complete, all pods ready') and the first NullPointerException at 12:12:31Z, i.e. ~2 minutes later.
  - `F4` ✔ All order-service errors are two patterns: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO' (224) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202); first occurrences 12:12:31Z and 12:12:44Z.
  - `F5` ✔ The failures are limited to orderType=FNO (derivatives) orders, matching the reported scope; the new validator is the faulting frame at line 88.
  - `F6` ✔ order-service latency_p99_ms stayed flat at ~134-165ms before and after 12:10 (no latency spike).
  - `F7` ✔ order-service cpu_pct (~40%), memory_pct (~60%) and db_pool_in_use (~14) are all flat across the onset window; request_rate flat ~230 req/s. No resource-exhaustion signature.
  - `F8` ✔ No configuration/feature-flag change exists for order-service in 2026-03-01..13:59; the only config change in the window is cfg-2101 on notification-service (email.batch_size 200->250).
  - `F9` ✔ Downstream dependencies show no errors in the window: pricing-engine, exchange-gateway and market-data-service logs returned 0 matches; ledger-db only logged one unrelated 'deprecated TLS cipher' warning at 12:15:33Z.
  - `F10` ✔ api-gateway logs 159 WARN 'upstream order-service returned 500 for POST /v1/orders' starting 12:13:07Z, confirming the failures propagate from order-service; no api-gateway-side fault.
  - `F11` ✔ The new code removed the legacy 'OrderRules.validateMargin()' and replaced it with MarginCheckValidator, whose line 88 dereferences a marginProfile that is null; this is a regression in the new validation path, not a downstream data outage (no dependency reports errors).
  - `F12` ✔ Historical incident HIST-109 is a bad_deployment regression (JWT library upgrade) resolved by rollback, corroborating that a release-introduced regression is the plausible class here; HIST-103/110/115 (dependency/infra/config) symptoms (504/timeouts/pool exhaustion) do not match the observed NPE.
**Contradicting evidence**
  - (none)
**Assumptions**
  - All FNO orders reaching the new validator fail because marginProfile is null; failures began with the new pods, not a coincidental data change
  - No unseen order-service flag/config change exists outside the queried window

### H2: Upstream margin-profile data (ledger-db) became null/incomplete, causing the new strict validator to fail
A data-side change made marginProfile null for FNO accounts, and the stricter new validator turns that into a 500.

**Causal chain**
1. marginProfile returns null from datastore *(assumed)*
2. New validator does not null-guard it [F4]

**Supporting evidence**
  - `F4` ✔ All order-service errors are two patterns: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO' (224) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202); first occurrences 12:12:31Z and 12:12:44Z.
**Contradicting evidence**
  - `F9` ✔ Downstream dependencies show no errors in the window: pricing-engine, exchange-gateway and market-data-service logs returned 0 matches; ledger-db only logged one unrelated 'deprecated TLS cipher' warning at 12:15:33Z.
  - `F1` ✔ order-service error_rate was ~0.21-0.24% for all buckets 11:30-12:00, then jumped to 14.74% in the 12:10 bucket and remained ~18.3-18.8% through 13:50.
  - `F8` ✔ No configuration/feature-flag change exists for order-service in 2026-03-01..13:59; the only config change in the window is cfg-2101 on notification-service (email.batch_size 200->250).
**Assumptions**
  - ledger-db data changed at ~12:12Z despite no logged errors there

### H3: Resource exhaustion / capacity saturation in order-service
CPU, memory, DB pool or request load exhausted capacity causing 500s.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ order-service latency_p99_ms stayed flat at ~134-165ms before and after 12:10 (no latency spike).
  - `F7` ✔ order-service cpu_pct (~40%), memory_pct (~60%) and db_pool_in_use (~14) are all flat across the onset window; request_rate flat ~230 req/s. No resource-exhaustion signature.
**Rejected because:** cpu (~40%), memory (~60%), db_pool_in_use (~14) and request_rate are all flat across onset; latency p99 flat ~150ms; error pattern is a code NPE, not a timeout/pool error (F6, F7).

## Facts (verified observations)
- order-service error_rate rose from ~0.22% (pre-12:10 buckets) to 14.74% in the 12:10 bucket and ~18% thereafter. [F1]
- dep-5101 (order-service v2.14.0) deployed at 12:10:00Z added MarginCheckValidator and removed legacy OrderRules.validateMargin(). [F2]
- First NPE occurred 12:12:31Z, ~2 min after the rollout-complete log at 12:10:08Z. [F3]
- All logged errors are MarginCheckValidator NPEs with 'marginProfile is null' for orderType=FNO. [F4, F5]
- Latency (p99 ~150ms), CPU (~40%), memory (~60%), db_pool_in_use (~14) and request_rate (~230/s) were flat across onset. [F6, F7]
- No order-service config change in the window; only an unrelated notification-service email.batch_size change. [F8]
- No errors in downstream dependencies; ledger-db only logged a deprecated TLS cipher warning. [F9]
- api-gateway recorded 159 'upstream order-service returned 500 for POST /v1/orders' warnings from 12:13:07Z. [F10]

## Assumptions (not verified)
- marginProfile should be populated for FNO orders but is null in the new code path (either a code bug or a missing null-guard); no sample marginProfile payload was available to confirm the exact defect
- No order-service config/flag change occurred outside the 2026-03-01..13:59 query window
- Failure rate (~18%) reflects FNO share of order traffic; non-FNO orders are unaffected

## Timeline
- `2026-03-03T12:10:00Z` dep-5101 order-service v2.14.0 deployed (refactor validation; add margin check for F&O) [F2]
- `2026-03-03T12:10:08Z` Rollout complete, all pods ready [F3]
- `2026-03-03T12:10:00Z` error_rate bucket jumps to 14.74% (from ~0.22%) [F1]
- `2026-03-03T12:12:31Z` First NullPointerException at MarginCheckValidator.validate line 88, orderType=FNO [F3, F4]
- `2026-03-03T12:12:44Z` First 'order rejected with 500: marginProfile is null' [F4]
- `2026-03-03T12:13:07Z` api-gateway begins logging 'upstream order-service returned 500 for POST /v1/orders' [F10]
- `2026-03-03T12:22:00Z` INCIDENT INC-001 reported by on-call SRE []
- `2026-03-03T13:59:00Z` Error rate still ~18%; no rollback performed [F1, F2]

## Recommended actions
- **[immediate]** Roll back order-service to the pre-v2.14.0 version (or disable the new MarginCheckValidator / restore OrderRules.validateMargin()) to stop the 500s. -- Fault is in the code introduced by dep-5101 at 12:10Z; error rate has stayed ~18% since onset and no rollback has been done. [F1, F2, F4]
- **[short_term]** Fix the null-dereference: null-guard marginProfile in MarginCheckValidator.validate (line 88) or ensure margin profiles are populated/loaded before validation for FNO orders. -- The NPE is at a fixed line for a specific order type, indicating a missing null check or unloaded profile. [F4, F5, F11]
- **[short_term]** Verify FNO margin-profile data population from ledger-db to confirm whether the new validator also needs a data-loading fix, not just a null guard. -- Distinguishes a pure code bug from a missing-data dependency; no marginProfile payload was observed in tooling. [F9]
- **[long_term]** Add a canary/staged rollout with F&O order-path smoke tests in CI to catch validation regressions before full rollout. -- A 2-minute post-deploy regression reached ~18% error rate with no staged guardrail. [F3]

## Data gaps
- No raw marginProfile payload or ledger-db row data was available, so the exact reason marginProfile is null (code bug vs. missing data load) is unconfirmed
- order-service logs were truncated (426 ERROR / 447 total matched, 40/20 returned); the full set was not enumerated, though the top patterns cover the period
- exchange-api (external provider) has no telemetry (has_telemetry=false), so a partner-side contribution cannot be directly observed, though upstream order-service NPEs are fully explained endogenously
- No redemption/rollback record exists to test recovery; rollback had not been attempted as of 13:59Z

## Investigation stats
- latency 46.19s, LLM calls 5, tokens 65535 (in 58046 / out 7489)
- tool calls 23 (0 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
