# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order placement 500s (INC-001) are caused by order-service deployment dep-5101 (v2.14.0, live 12:10Z), which replaced legacy OrderRules.validateMargin() with a new MarginCheckValidator. The new validator dereferences a null marginProfile (Java NPE at MarginCheckValidator.java:88) for F&O (orderType=FNO) orders, rejecting them with HTTP 500. Error rate jumped from ~0.2% to ~18-20% at 12:12 (first error 12:12:31, ~2 min post-deploy) and stayed elevated; all 426 ERROR lines are the two validator-NPE patterns and every line is FNO. Latency, CPU, memory, DB pool and downstream exchange-gateway/pricing-engine all stayed normal, and no order-service config change exists, ruling out dependency, resource and config causes. The deploy was never rolled back. Recommended fix: roll back v2.14.0 (or hotfix a null-guard), and validate margin-profile defaults for F&O accounts.

## Probable root cause
**Deploy dep-5101 (v2.14.0) introduced a null-dereferencing margin check on F&O orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Deploy dep-5101 (v2.14.0) introduced a null-dereferencing margin check on F&O orders | bad_deployment | order-service | dep-5101 | 0.90 (high) | active |
| 2 | H2: Margin/marginProfile data missing upstream (data_issue) causing NPE | data_issue | ledger-db | - | 0.12 (low) | rejected |
| 3 | H3: Downstream dependency (exchange-gateway/pricing-engine) failure | dependency_failure | exchange-gateway | - | 0.03 (low) | rejected |
| 4 | H4: Resource exhaustion / capacity saturation on order-service | resource_exhaustion | order-service | - | 0.03 (low) | rejected |

### H1: Deploy dep-5101 (v2.14.0) introduced a null-dereferencing margin check on F&O orders
order-service v2.14.0 replaced legacy OrderRules.validateMargin() with a new MarginCheckValidator that dereferences a null marginProfile (MarginCheckValidator.java:88) for F&O order types, throwing NullPointerException and causing HTTP 500 on POST /v1/orders for those orders. The fault originates in the code release.

**Causal chain**
1. dep-5101 deployed at 12:10 adds MarginCheckValidator and removes legacy validateMargin() [F2]
2. First error at 12:12:31 is an NPE inside MarginCheckValidator.validate for orderType=FNO [F3, F4]
3. All 426 errors are NPE/marginProfile-null patterns tied to the new validator, and every line is orderType=FNO [F5, F6]
4. Null marginProfile unhandled by new code path -> order rejected with 500 [F5]

**Supporting evidence**
  - `F1` ✔ order-service error rate jumped from ~0.2% baseline to ~18-20% starting in the 12:12 bucket and remained elevated (~18.7%) through 13:48.
  - `F2` ✔ A single order-service deployment dep-5101 (v2.14.0) went live at 2026-03-03T12:10:00Z, summarized 'Refactor order validation; add margin check for F&O orders' with changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'.
  - `F3` ✔ First order-service error appears at 12:12:31Z, ~2 minutes after the 12:10 deploy: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO'.
  - `F4` ✔ There were zero ERROR-level order-service log entries in the 11:30-12:12 window before/at the deploy, i.e. errors began only after dep-5101.
  - `F5` ✔ All 426 order-service ERROR entries in the window are one of two patterns tied to the new validator: 224x 'NullPointerException at MarginCheckValidator.validate(...) orderType=FNO' and 202x 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account ...'.
  - `F6` ✔ Every NPE log line references orderType=FNO; no other order type appears in the error patterns.
  - `F7` ✔ order-service resource metrics are flat across the incident: latency_p99_ms ~150ms unchanged, cpu_pct ~40%, memory_pct ~60%, db_pool_in_use ~14, request_rate ~232 req/s.
  - `F8` ✔ Downstream exchange-gateway shows no degradation: error_rate stayed ~0.2% and there were zero ERROR logs in the window; pricing-engine also had zero ERROR logs.
  - `F9` ✔ The only config/flag change in the window is unrelated: notification-service email.batch_size 200->250 at 11:21 (cfg-2101); no order-service config change exists.
  - `F10` ✔ dep-5101 is still 'live' with rolled_back_at null; no rollback was performed.
**Contradicting evidence**
  - (none)
**Assumptions**
  - marginProfile can legitimately be null for some F&O accounts and the old validateMargin() tolerated it (new code does not null-guard)
  - no rollback has been attempted yet
  - error_rate metric counts the same 500s seen in logs

### H2: Margin/marginProfile data missing upstream (data_issue) causing NPE
A data problem (missing/absent margin profiles for F&O accounts, e.g. not populated in a datastore) means marginProfile is null for many accounts, and the new validator surfaces it as an NPE. Fault would originate in the data source, not the code.

**Causal chain**
1. marginProfile lookup returns null for affected accounts [F5]
2. new validator fails to handle null -> NPE -> 500 [F3]

**Supporting evidence**
  - `F5` ✔ All 426 order-service ERROR entries in the window are one of two patterns tied to the new validator: 224x 'NullPointerException at MarginCheckValidator.validate(...) orderType=FNO' and 202x 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account ...'.
  - `F6` ✔ Every NPE log line references orderType=FNO; no other order type appears in the error patterns.
**Contradicting evidence**
  - `F2` ✔ A single order-service deployment dep-5101 (v2.14.0) went live at 2026-03-03T12:10:00Z, summarized 'Refactor order validation; add margin check for F&O orders' with changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'.
  - `F3` ✔ First order-service error appears at 12:12:31Z, ~2 minutes after the 12:10 deploy: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO'.
  - `F4` ✔ There were zero ERROR-level order-service log entries in the 11:30-12:12 window before/at the deploy, i.e. errors began only after dep-5101.
  - `F7` ✔ order-service resource metrics are flat across the incident: latency_p99_ms ~150ms unchanged, cpu_pct ~40%, memory_pct ~60%, db_pool_in_use ~14, request_rate ~232 req/s.
**Assumptions**
  - there is an upstream margin-profile data store that could have changed
**Rejected because:** Onset is exactly at the deploy (12:12 first error vs 12:10 deploy) with zero errors before it, error is exclusively in the newly-added validator, and no data/config change or dependency degradation is visible. If the data were broadly null, legacy code/other paths would also fail; scope is strictly FNO through the one new code path. A defect in the new code is far more consistent than a simultaneous data change that conveniently only manifests via the new validator.

### H3: Downstream dependency (exchange-gateway/pricing-engine) failure
A downstream dependency degraded and propagated 500s into order placement.

**Causal chain**
1. downstream returns errors -> order-service 500s *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F3` ✔ First order-service error appears at 12:12:31Z, ~2 minutes after the 12:10 deploy: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO'.
  - `F8` ✔ Downstream exchange-gateway shows no degradation: error_rate stayed ~0.2% and there were zero ERROR logs in the window; pricing-engine also had zero ERROR logs.
  - `F7` ✔ order-service resource metrics are flat across the incident: latency_p99_ms ~150ms unchanged, cpu_pct ~40%, memory_pct ~60%, db_pool_in_use ~14, request_rate ~232 req/s.
**Assumptions**
  - none
**Rejected because:** exchange-gateway error_rate stayed ~0.2% with zero ERROR logs, pricing-engine had zero errors, and order-service latency/CPU/pool were unchanged. Error text is a local validator NPE, not a downstream timeout/5xx.

### H4: Resource exhaustion / capacity saturation on order-service
CPU, memory, thread or DB-pool exhaustion caused the 500s.

**Causal chain**
1. resource pressure -> 500s *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ order-service resource metrics are flat across the incident: latency_p99_ms ~150ms unchanged, cpu_pct ~40%, memory_pct ~60%, db_pool_in_use ~14, request_rate ~232 req/s.
**Assumptions**
  - none
**Rejected because:** cpu ~40%, memory ~60%, db_pool_in_use ~14/15 and request_rate ~232 req/s are all flat across the incident; no saturation signal and no restart.

## Facts (verified observations)
- order-service error rate rose from ~0.2% to ~18-20% starting 12:12Z and stayed elevated through 13:48Z. [F1]
- order-service v2.14.0 (dep-5101) deployed at 12:10Z added 'new MarginCheckValidator in validation chain' and removed legacy OrderRules.validateMargin(). [F2]
- First error is at 12:12:31Z: NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO. [F3]
- No order-service ERROR logs existed in 11:30-12:12Z before the deploy. [F4]
- All 426 ERROR entries are the two validator patterns: 224x validator NPE and 202x 'marginProfile is null for account' with 500. [F5]
- Every NPE error line is orderType=FNO. [F6]
- order-service latency (~150ms p99), cpu (~40%), memory (~60%) and db_pool_in_use (~14) were flat across the incident. [F7]
- exchange-gateway error_rate stayed ~0.2% and pricing-engine/exchange-gateway logged zero errors in the window. [F8]
- The only config change was unrelated notification-service email.batch_size 200->250 at 11:21Z. [F9]
- dep-5101 remains live with rolled_back_at null (no rollback done). [F10]

## Assumptions (not verified)
- marginProfile can legitimately be null for some F&O accounts and the removed legacy validateMargin() tolerated it, while the new MarginCheckValidator.java:88 does not null-guard.
- The error_rate metric reflects the same 500s logged as 'order rejected with 500'.
- No rollback has yet been attempted on dep-5101.

## Timeline
- `2026-03-03T11:21:00Z` Unrelated config change cfg-2101 notification-service email.batch_size 200->250 [F9]
- `2026-03-03T12:10:00Z` order-service dep-5101 (v2.14.0) deployed: new MarginCheckValidator, legacy validateMargin() removed [F2]
- `2026-03-03T12:12:31Z` First error: NullPointerException at MarginCheckValidator.validate:88 orderType=FNO [F3]
- `2026-03-03T12:12:00Z` error_rate bucket jumps to avg 18.33% (from ~0.2%) [F1]
- `2026-03-03T12:22:00Z` Incident reported by on-call SRE (support tickets, derivatives/F&O orders) []
- `2026-03-03T13:48:00Z` error_rate still ~18.75%; dep-5101 still live, not rolled back [F1, F10]

## Recommended actions
- **[immediate]** Roll back order-service to the pre-v2.14.0 version (or hotfix) immediately. -- Onset coincides exactly with dep-5101 (12:10 deploy, 12:12 first error); rollback removes the faulty MarginCheckValidator code path. [F2, F3, F4, F10]
- **[short_term]** Add a null-guard/default for marginProfile in MarginCheckValidator for F&O orders before re-deploying. -- NPE at MinimumCheckValidator.java:88 on FNO orders shows marginProfile can be null and is unhandled. [F3, F5, F6]
- **[short_term]** Verify which F&O accounts lack a marginProfile and whether the legacy validateMargin() tolerated it, to confirm intended default behavior. -- Distinguishes pure code defect from latent null data, and prevents recurrence. [F5, F6]
- **[long_term]** Add a pre-deploy canary/alert on validator NPEs for F&O order types. -- Incident ran ~1h47m before being noticed via support tickets, not alerting. [F3, F1]

## Data gaps
- No order-service log entries before 12:12:31Z were captured (query returned 0 for earlier windows), so baseline/INFO-level request logs and any pre-deploy margin validation behavior were not directly observed.
- The exact code at MarginCheckValidator.java:88 and whether marginProfile is fetched from ledger-db or another store is unknown (source not available); the root-cause attribution relies on the deploy content and error text.
- No deployment of any other order-service version in the prior weeks was found (only dep-5101 in the Feb 20-Mar 3 window) because the get_deployments query returned just one result; a longer lookback was not performed.

## Investigation stats
- latency 55.49s, LLM calls 5, tokens 61372 (in 54560 / out 6812)
- tool calls 21 (1 failed, 0 partial, 0 recovered by retry), findings 11/11 verified
