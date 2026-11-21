# Incident investigation report: INC-001
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The order-placement 500s are most likely caused by the order-service v2.14.0 deployment (dep-5101, 12:10Z), whose new MarginCheckValidator dereferences a missing marginProfile without a null guard on the F&O validation branch, throwing NullPointerException and rejecting with HTTP 500 (confidence 0.60, medium). The evidence is a tight temporal and causal fit: v2.14.0 is the only change touching order-service, it lands ~2 minutes before the first error at 12:12:31Z, the error signature is an in-process NPE at MarginCheckValidator.java:88 with marginProfile null on orderType=FNO, and that exactly matches the deployment's stated purpose ("add margin check for F&O orders") and the derivatives-concentrated ticket pattern. The alternatives — shared datastore fault, external exchange provider, and capacity saturation — are not supported: no config changes exist for the shared datastores, no derivatives-specific dependency exists in the closure, and the jump coincides with the deploy rather than any load shift. Residual uncertainty remains because the error/resource telemetry for the neighbouring services and shared datastores was never retrieved and the logs were truncated, so adjacent-service and datastore-fault hypotheses cannot be independently excluded.

## Probable root cause
**order-service v2.14.0 margin-check refactor introduced a null-handling regression on the F&O validation branch** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.14.0 margin-check refactor introduced a null-handling regression on the F&O validation branch | bad_deployment | order-service | dep-5101 | 0.60 (medium) | active |
| 2 | H2: Shared datastore fault (ledger-db / redis-cache / kafka) breaks order placement via a shared dependency | infrastructure_failure | ledger-db / redis-cache / kafka (shared datastores) | null | 0.07 (low) | active |
| 3 | H3: External exchange-provider degradation (exchange-api) fails order submission | dependency_failure | external exchange-api (via exchange-gateway) | null | 0.04 (low) | active |
| 4 | H4: Capacity/resource saturation (connection or thread pool, rate limit) on the order path caused by a traffic shift | capacity_saturation | order-service (or its inbound capacity provisioning) | null | 0.04 (low) | active |

### H1: order-service v2.14.0 margin-check refactor introduced a null-handling regression on the F&O validation branch
order-service v2.14.0 (dep-5101, deployed 12:10Z) replaced the legacy OrderRules.validateMargin() with a new MarginCheckValidator in the validation chain. The new validator dereferences marginProfile without a null guard at MarginCheckValidator.java:88. For F&O (derivatives) orders where marginProfile is absent/unset, every placement throws NullPointerException, the order is rejected with an HTTP 500, and the order-service error rate jumps from ~0.2% to ~18.5% immediately and plateaus. The F&O concentration is because the new margin check only runs on the derivatives branch of the code.

**Causal chain**
1. order-service v2.14.0 deployed at 12:10Z, refactoring order validation and adding a margin check for F&O orders while removing legacy OrderRules.validateMargin() [F7, F16]
2. The new MarginCheckValidator is invoked on the order-placement path for orderType=FNO [F2, F18]
3. marginProfile is null for those orders and the validator dereferences it at line 88 without a null guard, throwing NPE [F2, F1]
4. The unhandled NPE aborts order placement and surfaces as HTTP 500 on POST /v1/orders [F2, F1]
5. order-service error_rate jumps from ~0.2% baseline to 18.49% in the 12:12 bucket and plateaus flat rather than rising or recovering [F3]
6. Failing order calls propagate upstream as api-gateway 5xx (~3.4%), a fraction of the order-service rate, indicating collateral not an independent gateway fault [F4]

**Supporting evidence**
  - `F1` ✔ order-service ERROR logs on the order path begin at 2026-03-03T12:12:31Z, 10 minutes before the 12:22 report; 426 ERROR entries matched between 12:12:31Z and 13:58:50Z with only two top patterns, both the same NPE.
  - `F2` ✔ Dominant error signature: NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) with reason 'marginProfile is null', affecting orderType=FNO orders — 224 occurrences of the validator NPE and 202 of the 'order rejected with 500' variant.
  - `F3` ✔ order-service error_rate jumped from a stable ~0.2% baseline (11:30–12:05, max 0.32) to 18.49% at the 12:12 bucket and then plateaued: 12:19=18.11, 12:33=18.64, 13:15=18.97, 13:57=18.39, last=18.33 — flat, not rising or recovering.
  - `F7` ✔ Only two deployments occurred in the 09:00–13:59Z window across ALL services: portfolio-service v4.2.1 at 09:36Z (dep-5088, ci-bot, 'bump grpc 1.62 -> 1.63', status live) and order-service v2.14.0 at 12:10Z (dep-5101, r.iyer, 'Refactor order validation; add margin check for F&O orders', changes: 'new MarginCheckValidator in validation chain', 'remove legacy OrderRules.validateMargin()', status live). Neither is recorded as rolled back.
  - `F8` ✔ The order-service v2.14.0 deployment at 12:10Z is the only change that touches order-service and it precedes the incident report at 12:22Z by ~12 minutes; it is an F&O (derivatives) order-validation change, matching the reported symptom that failures are 'mostly derivatives orders'.
  - `F13` ✔ order-service's full order-placement dependency closure (depth 2 downstream): it calls ledger-db (kind=db), pricing-engine (http), exchange-gateway (http) and produces to kafka (queue-produce); pricing-engine in turn calls market-data-service (http) and redis-cache (cache), and exchange-gateway calls external exchange-api (has_telemetry=false). No derivatives-specific microservice appears anywhere in the closure — the F&O distinction is internal to order-service code, not a separate dependency.
  - `F16` ✔ Deployment timeline around onset is confirmed: across all services for 2026-03-01→2026-03-03T12:10Z only two deployments exist — portfolio-service v4.2.1 at 09:36Z (ci-bot, 'bump grpc 1.62 -> 1.63', status live, rolled_back_at null) and order-service v2.14.0 at 12:10Z (r.iyer, 'Refactor order validation; add margin check for F&O orders', changes ['new MarginCheckValidator in validation chain','remove legacy OrderRules.validateMargin()'], status live, rolled_back_at null). Neither is rolled back; the F&O margin-check deployment is the only change touching the failing code path and precedes the 12:12:31Z first error.
  - `F18` ✔ The derivatives-specific path is not a separate service or dependency: the only F&O/derivatives marker anywhere in the change or dependency data is the order-service v2.14.0 change text 'add margin check for F&O orders' plus the removal of 'legacy OrderRules.validateMargin()'. Combined with T1's error being orderType=FNO with marginProfile null in MarginCheckValidator, the derivatives concentration maps to an in-process validation branch, not to a shared component.
  - `F4` ✔ api-gateway error_rate rose in the same window from 0.2% at 12:00 to 1.79% at 12:08 and stabilized at ~3.4% (12:16–13:52, max 3.66) — far below order-service's ~18.5%, consistent with downstream collateral from failing order calls rather than an independent gateway fault.
**Contradicting evidence**
  - (none)
**Assumptions**
  - marginProfile is null because of an internal data/state condition in order-service (missing profile for the affected FNO accounts) rather than a change in an upstream data source; telemetry does not show what populates marginProfile
  - The legacy validateMargin() previously tolerated the missing marginProfile (or defaulted it), so the removal of that path is what exposes the null
  - No other deployment or config change in the wider window touched the order path (deployment lookups for pricing-engine, exchange-gateway, market-data-service, auth-service and config lookups beyond order-service/datastores were not completed)

### H2: Shared datastore fault (ledger-db / redis-cache / kafka) breaks order placement via a shared dependency
ledger-db, redis-cache or kafka — all flagged shared and all on the order-placement closure — degraded around 12:12Z, causing order-service writes/reads on the order path to fail and surface as 500s. The blast radius would include other consumers of the same datastore (portfolio-service deployed at 09:36Z shares ledger-db and redis-cache), which is what an orchestrator 'shared component broke multiple services' theory predicts.

**Causal chain**
1. ledger-db, redis-cache and kafka are shared=true and lie on order-service's dependency closure [F11, F14]
2. The shared datastore degrades or saturates around 12:12Z (assumed — no metric or log telemetry was retrieved for these components) *(assumed)*
3. Order placement operations that touch the shared datastore fail, producing 500s on POST /v1/orders *(assumed)*
4. Error rate jumps to ~18.5% at 12:12Z and plateaus [F3]

**Supporting evidence**
  - `F11` ✔ order-service dependency map: it calls ledger-db (db), pricing-engine (http), exchange-gateway (http) and produces to kafka (queue-produce); api-gateway calls order-service. ledger-db, redis-cache and kafka are flagged shared=true (used by several services), and pricing-engine reaches market-data-service and redis-cache. exchange-api is external (has_telemetry=false).
  - `F14` ✔ Shared components on the failing path: ledger-db (postgres), redis-cache (redis) and kafka (kafka) are all flagged shared=true, used by several services; pricing-engine and market-data-service are NOT shared (shared=false). ledger-db and redis-cache are reachable from multiple branches: order-service→ledger-db and portfolio-service→ledger-db (tc-018); redis-cache is used by pricing-engine (tc-017) and by portfolio-service and market-data-service (tc-018).
  - `F15` ✔ portfolio-service (the only other service deployed in the window, v4.2.1 at 09:36Z) shares ledger-db and redis-cache with the order path and is itself downstream of api-gateway, so a portfolio/ledger/redis fault could plausibly reach order-service through a shared datastore — a neighbour path that must be ruled out. However, T1 found market-data-service flat at baseline and no shared-component fault appeared in telemetry.
**Contradicting evidence**
  - `F1` ✔ order-service ERROR logs on the order path begin at 2026-03-03T12:12:31Z, 10 minutes before the 12:22 report; 426 ERROR entries matched between 12:12:31Z and 13:58:50Z with only two top patterns, both the same NPE.
  - `F2` ✔ Dominant error signature: NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) with reason 'marginProfile is null', affecting orderType=FNO orders — 224 occurrences of the validator NPE and 202 of the 'order rejected with 500' variant.
  - `F10` ✔ No config changes were found for shared components ledger-db, kafka or redis-cache, nor for order-service, in 2026-03-01 → 2026-03-03T13:59Z (count 0 for each scope). So there is no config-level evidence of a shared-DB/cache/queue change behind the onset.
  - `F13` ✔ order-service's full order-placement dependency closure (depth 2 downstream): it calls ledger-db (kind=db), pricing-engine (http), exchange-gateway (http) and produces to kafka (queue-produce); pricing-engine in turn calls market-data-service (http) and redis-cache (cache), and exchange-gateway calls external exchange-api (has_telemetry=false). No derivatives-specific microservice appears anywhere in the closure — the F&O distinction is internal to order-service code, not a separate dependency.
**Assumptions**
  - Datastore degradation would produce a rich client-side error taxonomy (timeouts, connection errors, statement failures), but the observed logs show only two patterns, both an in-process NPE
  - No config change occurred on the shared datastores (verified only for config, not runtime health)

### H3: External exchange-provider degradation (exchange-api) fails order submission
The external exchange-api (exchange-gateway's upstream, has_telemetry=false) degraded around 12:12Z, and order submissions to the exchange failed, surfacing as 500s on order placement — analogous to past incident HIST-103 (exchange partner outage → 504 on order submit).

**Causal chain**
1. order-service calls exchange-gateway, which calls the external exchange-api with no telemetry [F11, F13]
2. The external exchange-api degrades or becomes unavailable around 12:12Z (assumed; unverifiable from monitoring) *(assumed)*
3. Order submission to the exchange fails, surfacing as 500s on POST /v1/orders *(assumed)*
4. Error rate jumps to ~18.5% at 12:12Z and plateaus [F3]

**Supporting evidence**
  - `F11` ✔ order-service dependency map: it calls ledger-db (db), pricing-engine (http), exchange-gateway (http) and produces to kafka (queue-produce); api-gateway calls order-service. ledger-db, redis-cache and kafka are flagged shared=true (used by several services), and pricing-engine reaches market-data-service and redis-cache. exchange-api is external (has_telemetry=false).
  - `F12` ✔ Past incidents are leads only: HIST-103 (exchange partner outage → 504 on order submit, dependency_failure), HIST-110 (ledger-db IOPS throttling → slow orders, statement timeouts) and HIST-114 (ledger-db disk full from retained WAL) all involve order-service / its upstream-dependency chain and are plausible precedents for order failures, but none matches a 12:10Z validation-change trigger.
  - `F13` ✔ order-service's full order-placement dependency closure (depth 2 downstream): it calls ledger-db (kind=db), pricing-engine (http), exchange-gateway (http) and produces to kafka (queue-produce); pricing-engine in turn calls market-data-service (http) and redis-cache (cache), and exchange-gateway calls external exchange-api (has_telemetry=false). No derivatives-specific microservice appears anywhere in the closure — the F&O distinction is internal to order-service code, not a separate dependency.
**Contradicting evidence**
  - `F1` ✔ order-service ERROR logs on the order path begin at 2026-03-03T12:12:31Z, 10 minutes before the 12:22 report; 426 ERROR entries matched between 12:12:31Z and 13:58:50Z with only two top patterns, both the same NPE.
  - `F2` ✔ Dominant error signature: NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) with reason 'marginProfile is null', affecting orderType=FNO orders — 224 occurrences of the validator NPE and 202 of the 'order rejected with 500' variant.
  - `F18` ✔ The derivatives-specific path is not a separate service or dependency: the only F&O/derivatives marker anywhere in the change or dependency data is the order-service v2.14.0 change text 'add margin check for F&O orders' plus the removal of 'legacy OrderRules.validateMargin()'. Combined with T1's error being orderType=FNO with marginProfile null in MarginCheckValidator, the derivatives concentration maps to an in-process validation branch, not to a shared component.
**Assumptions**
  - An external-provider outage would typically show timeout/connectivity errors or 502/504, whereas the observed signature is a validation NPE inside order-service
  - Historical precedent HIST-103 is an UNVERIFIED/lead-only match and a different signature (504, not NPE)

### H4: Capacity/resource saturation (connection or thread pool, rate limit) on the order path caused by a traffic shift
A recent traffic shift saturated order-service's connection/thread pool or a rate limit on the order path, so a subset of order placements (heavier F&O requests) failed with 500s. This would show resource trends and a plateau without a code change.

**Causal chain**
1. A traffic shift increased load on the order path around 12:12Z (assumed; no traffic/resource metrics were retrieved) *(assumed)*
2. Pool/rate-limit saturation causes a subset of order placements to fail *(assumed)*
3. Failures surface as 500s and error rate plateaus at ~18.5% [F3]

**Supporting evidence**
  - `F3` ✔ order-service error_rate jumped from a stable ~0.2% baseline (11:30–12:05, max 0.32) to 18.49% at the 12:12 bucket and then plateaued: 12:19=18.11, 12:33=18.64, 13:15=18.97, 13:57=18.39, last=18.33 — flat, not rising or recovering.
**Contradicting evidence**
  - `F1` ✔ order-service ERROR logs on the order path begin at 2026-03-03T12:12:31Z, 10 minutes before the 12:22 report; 426 ERROR entries matched between 12:12:31Z and 13:58:50Z with only two top patterns, both the same NPE.
  - `F2` ✔ Dominant error signature: NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) with reason 'marginProfile is null', affecting orderType=FNO orders — 224 occurrences of the validator NPE and 202 of the 'order rejected with 500' variant.
  - `F8` ✔ The order-service v2.14.0 deployment at 12:10Z is the only change that touches order-service and it precedes the incident report at 12:22Z by ~12 minutes; it is an F&O (derivatives) order-validation change, matching the reported symptom that failures are 'mostly derivatives orders'.
  - `F16` ✔ Deployment timeline around onset is confirmed: across all services for 2026-03-01→2026-03-03T12:10Z only two deployments exist — portfolio-service v4.2.1 at 09:36Z (ci-bot, 'bump grpc 1.62 -> 1.63', status live, rolled_back_at null) and order-service v2.14.0 at 12:10Z (r.iyer, 'Refactor order validation; add margin check for F&O orders', changes ['new MarginCheckValidator in validation chain','remove legacy OrderRules.validateMargin()'], status live, rolled_back_at null). Neither is rolled back; the F&O margin-check deployment is the only change touching the failing code path and precedes the 12:12:31Z first error.
**Assumptions**
  - Resource saturation would produce pool-exhaustion/timeout errors or WARN signals, but only two log patterns were seen, both NPE (logs truncated to 40 of 426)
  - error_rate was flat at baseline through 12:05 and jumped coincident with the 12:10Z deploy, not with any observed load change

## Facts (verified observations)
- order-service ERROR logs on the order path begin at 2026-03-03T12:12:31Z, 10 minutes before the 12:22Z report; 426 ERROR entries were matched between 12:12:31Z and 13:58:50Z with only two top patterns, both the same NPE. [F1]
- The dominant error signature is NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) with reason 'marginProfile is null', on orderType=FNO orders (224 validator-NPE occurrences and 202 'order rejected with 500' variants). [F2]
- order-service error_rate jumped from a stable ~0.2% baseline (11:30–12:05, max 0.32) to 18.49% at the 12:12 bucket and then plateaued flat at roughly 18.1–19.0% through 13:57. [F3]
- Only two deployments occurred across all services in the 09:00–13:59Z window: portfolio-service v4.2.1 at 09:36Z (dep-5088, grpc bump, status live) and order-service v2.14.0 at 12:10Z (dep-5101, 'Refactor order validation; add margin check for F&O orders', changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()', status live); neither is recorded as rolled back. [F7, F16]
- The only config/feature-flag change in the whole window (scope 'all services') was notification-service key email.batch_size 200 -> 250 at 11:21Z (cfg-2101), unrelated to the order placement path. [F9]
- No config changes were found for shared components ledger-db, kafka or redis-cache, nor for order-service, in 2026-03-01 → 2026-03-03T13:59Z (count 0 for each scope). [F10]
- order-service's full order-placement dependency closure (depth 2) is ledger-db (db), pricing-engine (http), exchange-gateway (http) and kafka (queue-produce); no derivatives-specific microservice appears anywhere in the closure. [F11, F13]
- ledger-db, redis-cache and kafka are flagged shared=true and sit on the failing path; ledger-db and redis-cache are reachable from multiple branches (order-service→ledger-db and portfolio-service→ledger-db; redis-cache used by pricing-engine, portfolio-service and market-data-service). [F14]
- market-data-service stayed at baseline throughout the incident (error_rate max 0.32%, last 0.11%), so it is not failing alongside order-service. [F5]
- The report's service names 'pricing-service', 'risk-service', 'ledger-service' and 'derivatives-service' do not exist in monitoring; the known service set includes pricing-engine, ledger-db, portfolio-service, payment-service, settlement-service, exchange-gateway and auth-service. [F6]

## Assumptions (not verified)
- marginProfile is null because of an internal data/state condition in order-service (missing profile for the affected FNO accounts) rather than a change in an upstream data source; telemetry does not show what populates marginProfile.
- The legacy OrderRules.validateMargin() previously tolerated the missing marginProfile (or defaulted it), so removing that path is what exposes the null.
- No other deployment or config change in the wider window touched the order path; deployment lookups for pricing-engine, exchange-gateway, market-data-service and auth-service were not completed, and config lookups beyond order-service/the shared datastores were not run.
- The 426 ERROR entries and the flat ~18.5% error rate reflect the same single failure mode (marginProfile-null NPE on FNO orders); log truncation (40 of 426 returned) is assumed not to hide a materially different second failure mode.
- The ~18.5% plateau rather than ~100% failure is consistent with the check running only on the derivatives branch, i.e. FNO orders are a subset of order volume.
- The external exchange-api (has_telemetry=false) was healthy during 12:12–13:59Z; this is assumed rather than verified.
- (demoted from facts: no verified observation backs it) The order-service v2.14.0 deployment at 12:10Z is the only change touching order-service and precedes the 12:22Z report by ~12 minutes; it is an F&O derivatives order-validation change, matching the reported symptom that failures are 'mostly derivatives orders'.
- (demoted from facts: no verified observation backs it) api-gateway error_rate rose in the same window from 0.2% at 12:00 to 1.79% at 12:08 and stabilized at ~3.4% (12:16–13:52, max 3.66), far below order-service's ~18.5%, consistent with downstream collateral rather than an independent gateway fault.
- (demoted from facts: no verified observation backs it) The derivatives concentration maps to an in-process validation branch, not a shared component: the only F&O/derivatives marker in the change or dependency data is the v2.14.0 change text 'add margin check for F&O orders' plus the removal of legacy OrderRules.validateMargin().
- (demoted from facts: no verified observation backs it) Past-incident precedent search for the current signature (margin/validation NPE, F&O, 500 on order placement) returned no matching incident; closest hits (HIST-110 ledger-db IOPS throttling, HIST-115 connection-pool config, HIST-103 external exchange outage → 504) are infrastructure/dependency failures, and prior deployment-regression precedents (HIST-109, HIST-101) are different services and causes.

## Timeline
- `2026-03-03T09:36:00Z` portfolio-service v4.2.1 deployed (dep-5088, ci-bot, grpc bump, status live) — the only other deployment in the window; shares ledger-db and redis-cache with the order path. [F7, F15, F16]
- `2026-03-03T11:21:00Z` Only config/feature-flag change of the window: notification-service email.batch_size 200 -> 250 (cfg-2101, config-bot) — unrelated to order placement. [F9]
- `2026-至 2026-03-03T12:05:00Z` order-service error_rate stable at ~0.2% baseline (max 0.32). [F3]
- `2026-03-03T12:08:00Z` api-gateway error_rate begins rising from 0.2% (12:00) to 1.79%. [F4]
- `2026-03-03T12:10:00Z` order-service v2.14.0 deployed (dep-5101, r.iyer): 'Refactor order validation; add margin check for F&O orders'; adds new MarginCheckValidator in validation chain, removes legacy OrderRules.validateMargin(); status live, not rolled back. [F7, F8, F16]
- `2026-03-03T12:12:00Z` order-service error_rate jumps to 18.49% at the 12:12 bucket. [F3]
- `2026-03-03T12:12:31Z` First order-service ERROR log on the order path; NPE at MarginCheckValidator.validate(MarginCheckValidator.java:88), 'marginProfile is null', orderType=FNO. [F1, F2]
- `2026-03-03T12:16:00Z–13:52:00Z` api-gateway error_rate stabilizes at ~3.4% (max 3.66), consistent with collateral from failing order calls. [F4]
- `2026-03-03T12:22:00Z` Incident INC-001 [SEV-1] reported by on-call SRE: spike in failed order placements, HTTP 500 on POST /v1/orders. []
- `2026-03-03T13:57:00Z` order-service error_rate still ~18.33–18.39% — flat plateau, not recovering; 426 ERROR entries logged since 12:12:31Z, only two patterns, both the same NPE. [F1, F3]

## Recommended actions
- **[immediate]** Roll back the order-service deployment dep-5101 (v2.14.0, deployed 12:10Z) to the previous version that retains the legacy OrderRules.validateMargin() path, then confirm order-service error_rate returns to the ~0.2% baseline and that FNO POST /v1/orders stops returning 500. -- v2.14.0 is the only change touching order-service and lands ~2 minutes before the first error; the error is an in-process NPE at MarginCheckValidator.java:88 not present before this deploy. Removal of the legacy validateMargin() is the only other change and is part of the same trigger. [F1, F2, F3, F7, F8, F16]
- **[immediate]** If rollback is not immediately available, add a temporary null guard / feature-flag kill switch on the F&O margin check in the validation chain (bypass MarginCheckValidator on the derivatives branch) to stop the 500s while preserving the deployment. -- The failing branch is F&O-only (H1); disabling only that branch restores FNO order placement without reverting the rest of the release, based on the FNO-concentrated NPE signature. [F2, F18]
- **[short_term]** Fix the regression in code: add a null guard / safe default for marginProfile at MarginCheckValidator.java:88 and restore a defaulting behaviour equivalent to legacy validateMargin() before the F&O margin check runs; ship as a patch release. -- The NPE is caused by dereferencing an unset marginProfile; the legacy validateMargin() path that tolerated or defaulted the missing profile was removed by the same deployment. [F2, F7, F18]
- **[short_term]** Verify after the fix by replaying/placing FNO test orders with marginProfile absent and confirming no NPE and 200/expected validation errors, and by watching order-service error_rate return to baseline for at least the plateau duration observed. -- Confirms the fix addresses the exact failing input (orderType=FNO with marginProfile null) rather than just reducing error volume; the observed failure was a flat 18.5% plateau with no self-recovery. [F2, F3]
- **[short_term]** Add a required-nullability assertion / unit and integration test for absent marginProfile on the F&O validation branch, and make the validation chain fail with a proper 4xx validation error instead of an unhandled NullPointerException. -- The incident surfaced as an HTTP 500 from an in-process NPE; a missing profile is a data/validation condition that should be a controlled rejection, not a 500. [F2, F18]
- **[short_term]** Retrieve the missing telemetry to close the residual gaps: error/resource metrics for pricing-engine, ledger-db, portfolio-service, payment-service, settlement-service, auth-service and exchange-gateway; deployments for auth-service, pricing-engine, exchange-gateway and market-data-service in 2026-03-01→13:59Z; and shared-datastore health metrics (ledger-db IOPS/connections, redis-cache, kafka lag) for 12:00–13:59Z. -- These queries were never executed or returned not_found/budget_exhausted; they are the discriminators that would confirm the blast radius and fully exclude the shared-datastore (H2) and neighbour-deployment branches. [F10, F11, F14, F15]
- **[long_term]** Gate F&O/validation-chain changes behind a canary with automatic error-rate rollback (rollback on error_rate deviation from baseline within minutes) and require error-budget-based rollout for order-service. -- A 12:10Z deploy produced a step change at 12:12Z and was still live at 13:59Z; automated canary rollback would have limited the 100+ minute impact. [F3, F7, F16]
- **[long_term]** Correct the incident's service naming: the report references pricing-service, risk-service, ledger-service and derivatives-service, which do not exist in monitoring; use the known service set (pricing-engine, ledger-db, portfolio-service, payment-service, settlement-service, exchange-gateway, auth-service). -- Four observability queries failed with not_found because of these non-existent names, wasting investigation budget and leaving the blast radius only partially established. [F6]

## Data gaps
- Error/resource metrics were never retrieved for pricing-engine, ledger-db, portfolio-service, payment-service, settlement-service, auth-service or exchange-gateway; the first queries used non-existent service names and budget ran out before retrying, so blast radius is only partially established (confirmed flat only for market-data-service, elevated for api-gateway).
- No logs were queried for any service other than order-service, so the exact nature of api-gateway's ~3.4% 5xx (propagated order-service 500s vs. a gateway-specific fault) is undetermined.
- order-service logs were returned truncated (40 of 426) and only two patterns appeared; WARN/DEBUG context and pre-12:12 warnings were not examined, so earlier low-volume signals cannot be ruled out (though error_rate was at baseline through 12:05).
- Deployment lookups for auth-service, pricing-engine, exchange-gateway and market-data-service (2026-03-01 → 2026-03-03T13:59Z) returned budget_exhausted and were not executed, so a deployment on an upstream/downstream neighbour cannot be excluded.
- Config/feature-flag lookups covered order-service and the three shared datastores plus one 'all services' query over 09:00–13:59Z only; a flag/auth/config-service change outside those scopes cannot be excluded.
- No telemetry-level health check (connection saturation, IOPS, consumer lag) was run for ledger-db, redis-cache or kafka in 12:00–13:59Z; only the absence of config changes was confirmed, so a shared-datastore fault cannot be independently excluded.
- No historical incident matched this exact margin/validation-NPE-on-F&O failure mode, so duration/expected-resolution cannot be cited from precedent (only the flat plateau).
- exchange-api is external with has_telemetry=false, so an external exchange-provider fault cannot be fully excluded via telemetry, though the NPE-on-FNO signature does not point there.

## What would change this conclusion
- order-service error_rate does not fall back toward the ~0.2% baseline after rolling back dep-5101 / disabling the F&O margin-check branch — that would falsify H1 as the cause of the 500s.
- New logs from order-service (untruncated) or from ledger-db/redis-cache/kafka showing datastore timeouts, connection errors or statement failures on the order path around 12:12Z would support H2 (shared datastore fault).
- Telemetry showing degradation (error rate, IOPS throttle, connection saturation, consumer lag, timeouts) on ledger-db, redis-cache or kafka at ~12:12Z would support H2.
- Evidence of exchange-gateway connectivity failures or 502/504 responses to exchange-api during 12:12–13:59Z would support H3 (external provider degradation, HIST-103 class).
- Resource metrics showing order-service connection/thread-pool exhaustion or rate-limit rejections, or a traffic shift coinciding with the 12:12Z error jump, would support H4 (capacity saturation).
- Discovery of any other deployment or config/feature-flag change touching order-service or its order-path dependencies in the wider 2026-03-01→13:59Z window would introduce an alternative trigger.
- Evidence that marginProfile was being populated differently by an upstream source at ~12:12Z (rather than being an internal null-handling regression) would shift the root cause out of the order-service code change.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: The order-service v2.14.0 deployment at 12:10Z is the only change touching order
- fact demoted to assumption: api-gateway error_rate rose in the same window from 0.2% at 12:00 to 1.79% at 12
- fact demoted to assumption: The derivatives concentration maps to an in-process validation branch, not a sha
- fact demoted to assumption: Past-incident precedent search for the current signature (margin/validation NPE,

## Investigation stats
- latency 113.38s, LLM calls 12, tokens 69673 (in 53535 / out 16138)
- tool calls 23 (4 failed, 0 partial, 0 recovered by retry), findings 17/18 verified
