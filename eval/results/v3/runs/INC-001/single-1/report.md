# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order placement 500s are caused by bad deployment dep-5101 (order-service v2.14.0, deployed 12:10:00Z) which added a MarginCheckValidator for F&O orders. The new validator dereferences a null marginProfile, throwing NullPointerException at MarginCheckValidator.java:88 and returning HTTP 500. Onset is time-locked to the deploy (first error 12:12:31Z; error_rate 0.21% -> 18.3% in the 12:12 bucket) and errors are exclusively orderType=FNO, matching the derivatives-only ticket pattern. Alternatives ruled out: no order-service config change; no downstream/ledger-db errors; CPU/memory/DB pool/request_rate all flat (no saturation or load surge). Recommended: roll back dep-5101 / revert to v2.14.0's predecessor, and add null-safety to MarginCheckValidator.

## Probable root cause
**Bad deployment dep-5101 (v2.14.0) introduced a NPE in the new MarginCheckValidator for F&O orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.88, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bad deployment dep-5101 (v2.14.0) introduced a NPE in the new MarginCheckValidator for F&O orders | bad_deployment | order-service | dep-5101 | 0.88 (high) | active |
| 2 | H2: Upstream/dependency fault made marginProfile data unavailable | dependency_failure | ledger-db | - | 0.06 (low) | rejected |
| 3 | H3: Resource exhaustion / capacity saturation in order-service | resource_exhaustion | order-service | - | 0.03 (low) | rejected |

### H1: Bad deployment dep-5101 (v2.14.0) introduced a NPE in the new MarginCheckValidator for F&O orders
Deployment dep-5101 at 12:10:00Z added MarginCheckValidator to the order validation chain for F&O orders and removed the legacy OrderRules.validateMargin(). The new validator dereferences marginProfile, which is null for F&O accounts, throwing NullPointerException at MarginCheckValidator.java:88 and returning HTTP 500. Because only F&O orders take this path, only derivatives order placements fail, giving the sharp ~18% error rate.

**Causal chain**
1. deploy dep-5101 (v2.14.0) lands new MarginCheckValidator for F&O at 12:10:00Z [F4]
2. first FNO orders hit new validator and NPE at 12:12:31Z [F2, F5]
3. NPE 'marginProfile is null' propagates to HTTP 500 'order rejected with 500' [F3]
4. error_rate jumps 0.21% -> 18.3% at 12:12 bucket and stays elevated [F1]
5. alternatives (config, downstream, resources, load) ruled out [F6, F7, F8]

**Supporting evidence**
  - `F1` ✔ order-service error rate jumped from ~0.21% to ~18.3% exactly at the 12:12 bucket, and stayed elevated through 13:48.
  - `F2` ✔ First order-service error appears at 12:12:31Z; 426 ERROR log entries by 13:58:50Z, dominated by NullPointerException in MarginCheckValidator.validate(MarginCheckValidator.java:88) for orderType=FNO.
  - `F3` ✔ The second dominant error message is 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <id>'.
  - `F4` ✔ A single order-service deployment dep-5101 (v2.14.0) occurred at 12:10:00Z, summary 'Refactor order validation; add margin check for F&O orders', changes: new MarginCheckValidator in validation chain, remove legacy OrderRules.validateMargin(); status live, rolled_back_at null.
  - `F5` ✔ All errors are FNO-specific (derivatives orders), matching the report that tickets are mostly derivatives orders; MarginCheckValidator only runs for F&O orders.
  - `F9` ✔ The failing path is local to order-service (MarginCheckValidator in order-service validation chain); marginProfile is dereferenced by the new validator, and the error is thrown during validation before downstream calls, consistent with price/pool data being available but the new code path mishandling F&O accounts.
**Contradicting evidence**
  - (none)
**Assumptions**
  - marginProfile is expected to be populated for F&O accounts via path not covered by the refactor
  - the removal of legacy validateMargin() removed a null-guard/default previously applied

### H2: Upstream/dependency fault made marginProfile data unavailable
A dependency (ledger-db or a margin-profile source) stopped serving margin data, so marginProfile is null when order-service validates F&O orders.

**Causal chain**
1. margin data source degrades at ~12:12 *(assumed)*
2. marginProfile resolves null [F3]
3. validation throws NPE [F2]

**Supporting evidence**
  - `F3` ✔ The second dominant error message is 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <id>'.
**Contradicting evidence**
  - `F7` ✔ Downstream/infra services are clean: pricing-engine error_rate flat ~0.22%, market-data-service/pricing-engine/exchange-gateway/api-gateway have zero ERROR logs, and ledger-db shows only one unrelated TLS cipher warning.
  - `F8` ✔ order-service resource metrics are flat across the onset: CPU ~40%, memory ~60%, db_pool_in_use ~14 connections, request_rate ~232 req/s — no saturation or load surge.
  - `F6` ✔ No config/feature-flag change on order-service in the incident window (empty result); the only config change in the whole window is notification-service cfg-2101 'email.batch_size' 200->250 at 11:21.
  - `F4` ✔ A single order-service deployment dep-5101 (v2.14.0) occurred at 12:10:00Z, summary 'Refactor order validation; add margin check for F&O orders', changes: new MarginCheckValidator in validation chain, remove legacy OrderRules.validateMargin(); status live, rolled_back_at null.
**Assumptions**
  - marginProfile is fetched from ledger-db
**Rejected because:** ledger-db shows no errors (only an unrelated TLS cipher warning), no downstream service shows elevated errors, and the onset aligns exactly with dep-5101 rather than any dependency change; a dependency outage would not be perfectly time-locked to an order-service code deploy.

### H3: Resource exhaustion / capacity saturation in order-service
CPU, memory or DB connection pool exhaustion in order-service causes validation failures and 500s.

**Causal chain**
1. resources saturate at onset *(assumed)*
2. validation throws [F2]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ order-service resource metrics are flat across the onset: CPU ~40%, memory ~60%, db_pool_in_use ~14 connections, request_rate ~232 req/s — no saturation or load surge.
  - `F2` ✔ First order-service error appears at 12:12:31Z; 426 ERROR log entries by 13:58:50Z, dominated by NullPointerException in MarginCheckValidator.validate(MarginCheckValidator.java:88) for orderType=FNO.
  - `F3` ✔ The second dominant error message is 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <id>'.
**Rejected because:** CPU ~40%, memory ~60%, db_pool_in_use ~14 and request_rate ~232 req/s are all flat across the onset; the error is a deterministic NPE on FNO orders, not saturation.

## Facts (verified observations)
- order-service error_rate was ~0.2% until 12:00 bucket, then 18.33% at 12:12 bucket and remained ~18% through 13:48. [F1]
- First order-service ERROR is at 12:12:31Z; 426 errors, dominated by NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) with orderType=FNO. [F2, F5]
- Failures manifest as 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <id>'. [F3]
- Single order-service deploy dep-5101 v2.14.0 at 12:10:00Z added 'new MarginCheckValidator in validation chain' and removed 'legacy OrderRules.validateMargin()'; still live (not rolled back). [F4]
- No order-service config changes in window; only unrelated notification-service email.batch_size change at 11:21. [F6]
- pricing-engine, market-data-service, exchange-gateway, api-gateway have zero ERROR logs and pricing-engine error_rate is flat ~0.22%; ledger-db shows only one TLS cipher warning. [F7]
- order-service CPU ~40%, memory ~60%, db_pool_in_use ~14, request_rate ~232 req/s all flat across onset. [F8]

## Assumptions (not verified)
- marginProfile is expected to be non-null for F&O accounts on the normal path, and the refactor removed the legacy null-guard.
- Errors reported by support for 'derivatives orders' correspond to orderType=FNO in logs.

## Timeline
- `2026-03-03T12:10:00Z` order-service deployment dep-5101 (v2.14.0) goes live: new MarginCheckValidator for F&O orders, legacy validateMargin removed. [F4]
- `2026-03-03T12:12:00Z` order-service error_rate bucket jumps from ~0.2% to 18.33%. [F1]
- `2026-03-03T12:12:31Z` First NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO. [F2]
- `2026-03-03T12:12:44Z` First 'order rejected with 500: NullPointerException: marginProfile is null'. [F3]
- `2026-03-03T12:22:00Z` Incident reported (SEV-1), order-service error rate alert firing. []
- `2026-03-03T13:58:50Z` Errors still occurring (last match); deploy still live, not rolled back. [F2, F4]

## Recommended actions
- **[immediate]** Roll back order-service dep-5101 (v2.14.0) to the prior version immediately. -- Errors are time-locked to this deploy and it is still live with rolled_back_at null; rollback is the fastest mitigation. [F4, F1, F2]
- **[immediate]** Verify error_rate returns to baseline (<1%) after rollback to confirm causality. -- A clean recovery after rollback confirms the deployment as the fault and rules out coincidental dependency causes. [F1]
- **[short_term]** Add null-safety/guard for marginProfile in MarginCheckValidator and validate the F&O margin path in staging before re-release. -- The NPE at line 88 on FNO accounts indicates the new validator assumes marginProfile is always non-null. [F2, F3, F9]
- **[long_term]** Add canary/gradual rollout and F&O-order smoke tests gating validation-chain deploys. -- Deploy caused ~18% failure for F&O orders immediately on full rollout; staged rollout and path-specific tests would catch it earlier. [F4, F5]

## Data gaps
- No visibility into the marginProfile data source/provenance (no logs show where marginProfile is loaded), so we cannot fully confirm whether null is expected for some F&O accounts or a secondary defect.
- No rollback has been attempted yet (rolled_back_at null), so there is no direct post-rollback error_rate evidence to confirm recovery.
- pricing-engine/market-data-service/exchange-gateway/api-gateway had no ERROR-level logs, but only WARN+ were checked; benign INFO-level context was not reviewed.

## Investigation stats
- latency 38.21s, LLM calls 5, tokens 58128 (in 51670 / out 6458)
- tool calls 22 (0 failed, 0 partial, 0 recovered by retry), findings 9/9 verified
