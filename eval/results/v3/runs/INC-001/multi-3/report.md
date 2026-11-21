# Incident investigation report: INC-001
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Order placement 500s are caused by the order-service v2.14.0 release (dep-5101, deployed 12:10:00Z): the newly introduced MarginCheckValidator dereferences a null marginProfile with no null guard, throwing NullPointerException on F&O orders 2 minutes after deploy and stepping order-service error_rate from ~0.20% to 18.49% at 12:12Z. All 426 errors carry the MarginCheckValidator NPE / "marginProfile is null" signature and are filtered to orderType=FNO, exactly the change's stated scope ("add margin check for F&O orders"). Competing origins are contradicted: dep-5088 predates onset by ~2h36m and only bumps gRPC, no config change touched order-service or its dependencies, and all four derivatives-adjacent peers (market-data, exchange-gateway, pricing-engine, portfolio-service) stayed flat at ~0.2% error_rate throughout, which falsifies a shared-infrastructure path and removes the only mechanism by which upstream data could be blamed. Confidence 0.85 (high), capped for the unexecuted narrow-window change confirmation and the missing order-service resource-saturation cross-check; the recommended fix is to roll back dep-5101 (or patch the validator with a null guard / explicit marginProfile loading).

## Probable root cause
**dep-5101 (order-service v2.14.0) introduced a null-marginProfile NPE in the new F&O margin validation path** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: dep-5101 (order-service v2.14.0) introduced a null-marginProfile NPE in the new F&O margin validation path | bad_deployment | order-service | dep-5101 | 0.85 (high) | active |
| 2 | H2: Null marginProfile originates from an upstream data source (portfolio-service / ledger-db), and the new validator merely surfaces pre-existing bad data | data_issue | portfolio-service (margin/profile data owner) or ledger-db | dep-5088 | 0.12 (low) | active |
| 3 | H3: Shared datastore/middleware (ledger-db, redis-cache, kafka) degraded at onset, causing order-service failures over a shared path | infrastructure_failure | ledger-db / redis-cache / kafka (shared platform components) | null | 0.06 (low) | active |
| 4 | H4: Adjacent service change (cfg-2101 on notification-service / other adjacent change) rejecting derivative orders | config_change | notification-service (cfg-2101) or other adjacent service | cfg-2101 | 0.03 (low) | rejected |

### H1: dep-5101 (order-service v2.14.0) introduced a null-marginProfile NPE in the new F&O margin validation path
The v2.14.0 refactor of order-service replaced the legacy OrderRules.validateMargin() with a new MarginCheckValidator. The new validator dereferences marginProfile without a null guard (or fails to populate/load it), so for F&O (derivatives) orders where marginProfile is null the validator throws NullPointerException, the request fails, and POST /v1/orders returns HTTP 500. All 426 errors are confined to orderType=FNO, exactly the order class the deploy summary says the change targets.

**Causal chain**
1. dep-5101 deploys order-service v2.14.0 at 12:10Z, adding MarginCheckValidator and removing legacy OrderRules.validateMargin() [F1, F14]
2. The new validator is inserted into the validation chain for F&O orders only (per deploy summary 'add margin check for F&O orders') [F1]
3. Two minutes later, first error at 12:12:31Z is NullPointerException in MarginCheckValidator.validate(MarginCheckValidator.java:88) for orderType=FNO [F8, F14, F15]
4. The same class the deploy introduced is the faulting frame; 224 NPE-at-validator + 202 'marginProfile is null' errors, all filtered to FNO [F9, F15]
5. Validator throws -> order-service returns 500 on POST /v1/orders -> error_rate steps from 0.20% (12:05Z) to 18.49% (12:12Z) and stays 17.9-19.0% through 13:57Z [F10, F8]
6. Peer/downstream services (market-data, exchange-gateway, pricing-engine, portfolio-service) all remain flat at ~0.2% error_rate, so nothing downstream is generating this failure [F11, F12, F13, F19]

**Supporting evidence**
  - `F1` ✔ Deployment dep-5101 to order-service v2.14.0 occurred at 2026-03-03T12:10:00Z, 12 minutes before the 12:22Z report; summary 'Refactor order validation; add margin check for F&O orders', changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'; status live, rolled_back_at null.
  - `F8` ✔ First order-service ERROR in the window is at 2026-03-03T12:12:31Z: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId=9326cc69'. 426 ERROR lines match between 12:12:31Z and 13:58:50Z.
  - `F9` ✔ The error signature is a NullPointerException on a null marginProfile, surfacing as the client-facing 500: two dominant patterns cover all 426 errors -- 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:<n>) orderType=FNO' (224) and 'order rejected with <n>: java.lang.NullPointerException: marginProfile is null for account <n>' (202). Both patterns are filtered to orderType=FNO, the derivatives order type.
  - `F10` ✔ order-service error_rate shows a step change between the 12:05Z and 12:12Z buckets: 0.20% avg at 12:05:00Z, then 18.49% at 12:12:00Z, and it stays elevated (17.93-18.97% avg) in every bucket through 13:57:00Z. Baseline 11:30-12:05Z was 0.18-0.27% avg.
  - `F14` ✔ order-service deployment dep-5101 to v2.14.0 landed at 2026-03-03T12:10:00Z, 2 minutes before the first 500 hitting POST /v1/orders at 12:12:31Z; it is the only deployment in the 11:00-14:00Z window and its summary matches the failure mode exactly: 'Refactor order validation; add margin check for F&O orders', changes 'new MarginCheckValidator in validation chain' + 'remove legacy OrderRules.validateMargin()'. Status is still 'live' with rolled_back_at null.
  - `F15` ✔ The NPE stack trace is inside the exact class the deploy introduced: T1/F8-9 report 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88)' with 'marginProfile is null', filtered to orderType=FNO. So the faulting code path is the newly deployed validator, not a pre-existing path.
  - `F7` ✔ Temporal proximity makes dep-5101 the change closest to symptom onset (order-service itself, 12 min before 12:22Z), while dep-5088 (09:36Z) and cfg-2101 (11:21Z) are further from onset and touch services without a direct functional link to the F&O order path.
  - `F16` ✔ No config change touched order-service or any dependency in the 11:00-14:00Z window: the only change is cfg-2101 on notification-service (email.batch_size 200->250 at 11:21Z), which has no functional link to the F&O order path.
  - `F17` ✔ Historically, no precedent matches the NPE/null-marginProfile signature. The closest order-service precedents are HIST-115 connection-pool exhaustion, HIST-110 ledger-db IOPS throttling (both latency/resource signatures, not null-pointer validation errors) and HIST-103 external exchange outage (504/timeouts). None involves a null field from a validation refactor.
  - `F18` ✔ Alternative candidate dep-5088 (portfolio-service v4.2.1, grpc 1.62->1.63) at 2026-03-03T09:36:00Z is 2h36m before onset and touches only a gRPC version; portfolio-service error_rate stayed flat (~0.2%) throughout, and it can only reach order-service indirectly via the shared ledger-db/redis-cache. Its temporal and architectural distance makes it a much weaker fit than dep-5101.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 12:05->12:12Z step is due to the validator path and not a coincident resource exhaustion; order-service latency/pool metrics were not retrieved (gap).
  - No unrecorded feature-flag/infra change landed within 2 minutes of onset (change inventory possibly incomplete - gap).

### H2: Null marginProfile originates from an upstream data source (portfolio-service / ledger-db), and the new validator merely surfaces pre-existing bad data
marginProfile is derived margin data owned downstream of order-service (portfolio-service writes ledger-db, a shared store). If that data path began returning/omitting marginProfile for F&O accounts, the newly deployed validator would NPE on data it was never defensive against. On this reading the trigger is a data_issue at the margin-data owner, not the order-service release itself.

**Causal chain**
1. marginProfile is margin data not owned by order-service; portfolio-service reads/writes the shared ledger-db and is the plausible owner [F4, F5]
2. portfolio-service v4.2.1 (dep-5088) deployed at 09:36Z, a gRPC bump that could alter data shape served to consumers [F2, F5]
3. A missing/blank marginProfile for FNO accounts flows into order-service's validation chain (no guard in new code) *(assumed)*
4. Validator dereferences null marginProfile -> NPE -> 500 for F&O orders [F8, F9]

**Supporting evidence**
  - `F4` ✔ order-service (trading, not shared) directly depends on ledger-db (postgres, shared=true), pricing-engine (http), exchange-gateway (http) and kafka (queue-produce); pricing-engine in turn depends on market-data-service (http) and redis-cache (cache, shared=true), so order-service inherits redis-cache and market-data-service paths. It is called by api-gateway.
  - `F5` ✔ ledger-db and redis-cache are shared components: portfolio-service also writes/reads ledger-db and redis-cache, so a change or load from portfolio-service (including its 09:36Z v4.2.1 deploy) could reach order-service through those shared datastores; both are flagged shared=true with owner platform.
  - `F2` ✔ The only other deployment in the 2026-03-02T12:00Z–2026-03-03T13:59Z window was dep-5088 to portfolio-service v4.2.1 at 2026-03-03T09:36:00Z ('bump grpc 1.62 -> 1.63'), status live, rolled_back_at null — ~2h46m before symptom onset.
**Contradicting evidence**
  - `F13` ✔ Neither derivatives-adjacent peer shows the order-service symptom: pricing-engine error_rate is flat (min 0.1%, max 0.32%, mean 0.22%, 12:12Z bucket 0.19%) and portfolio-service error_rate is flat (min 0.1%, max 0.32%, mean 0.21%, 12:12Z bucket 0.22%). Both remain at ~0.2% for the entire incident.
  - `F18` ✔ Alternative candidate dep-5088 (portfolio-service v4.2.1, grpc 1.62->1.63) at 2026-03-03T09:36:00Z is 2h36m before onset and touches only a gRPC version; portfolio-service error_rate stayed flat (~0.2%) throughout, and it can only reach order-service indirectly via the shared ledger-db/redis-cache. Its temporal and architectural distance makes it a much weaker fit than dep-5101.
  - `F8` ✔ First order-service ERROR in the window is at 2026-03-03T12:12:31Z: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId=9326cc69'. 426 ERROR lines match between 12:12:31Z and 13:58:50Z.
  - `F9` ✔ The error signature is a NullPointerException on a null marginProfile, surfacing as the client-facing 500: two dominant patterns cover all 426 errors -- 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:<n>) orderType=FNO' (224) and 'order rejected with <n>: java.lang.NullPointerException: marginProfile is null for account <n>' (202). Both patterns are filtered to orderType=FNO, the derivatives order type.
  - `F1` ✔ Deployment dep-5101 to order-service v2.14.0 occurred at 2026-03-03T12:10:00Z, 12 minutes before the 12:22Z report; summary 'Refactor order validation; add margin check for F&O orders', changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'; status live, rolled_back_at null.
**Assumptions**
  - portfolio-service marginProfile output actually changed and is the source of the null.
  - The NPE only manifests because order-service code lacks a null guard.

### H3: Shared datastore/middleware (ledger-db, redis-cache, kafka) degraded at onset, causing order-service failures over a shared path
order-service inherits shared components (ledger-db, redis-cache via pricing-engine, kafka) that also serve portfolio-service. If one of these saturated or degraded around 12:10-12:22Z, order-service requests could fail. The 500s would then be a downstream/shared-infrastructure symptom rather than a local code defect.

**Causal chain**
1. order-service depends on shared ledger-db, redis-cache and kafka; portfolio-service shares ledger-db and redis-cache [F4, F5, F19]
2. A shared component degrades/saturates near 12:10-12:22Z *(assumed)*
3. order-service calls fail and surface as 500 on POST /v1/orders [F10]

**Supporting evidence**
  - `F4` ✔ order-service (trading, not shared) directly depends on ledger-db (postgres, shared=true), pricing-engine (http), exchange-gateway (http) and kafka (queue-produce); pricing-engine in turn depends on market-data-service (http) and redis-cache (cache, shared=true), so order-service inherits redis-cache and market-data-service paths. It is called by api-gateway.
  - `F5` ✔ ledger-db and redis-cache are shared components: portfolio-service also writes/reads ledger-db and redis-cache, so a change or load from portfolio-service (including its 09:36Z v4.2.1 deploy) could reach order-service through those shared datastores; both are flagged shared=true with owner platform.
  - `F19` ✔ order-service inherits shared upstream paths through ledger-db (shared=true), redis-cache (shared=true, via pricing-engine), kafka and market-data-service — so a shared-component fault could in principle produce the symptom; but all four derivatives-adjacent peers stayed at flat ~0.2% error rate in the same window, which argues against a shared-datastore causation here.
  - `F6` ✔ Historical precedents returned as leads only (not evidence for this incident): HIST-115 (2025-09-25) order-service 'connection pool misconfiguration' with 'pool exhausted' symptoms, HIST-110 (2026-01-15) 'Slow orders from ledger-db IOPS throttling' involving order-service+ledger-db, HIST-103 (2025-10-21) order failures from external exchange API outage; a separate search returned HIST-113 (2026-01-28) pricing-engine CPU saturation.
**Contradicting evidence**
  - `F9` ✔ The error signature is a NullPointerException on a null marginProfile, surfacing as the client-facing 500: two dominant patterns cover all 426 errors -- 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:<n>) orderType=FNO' (224) and 'order rejected with <n>: java.lang.NullPointerException: marginProfile is null for account <n>' (202). Both patterns are filtered to orderType=FNO, the derivatives order type.
  - `F15` ✔ The NPE stack trace is inside the exact class the deploy introduced: T1/F8-9 report 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88)' with 'marginProfile is null', filtered to orderType=FNO. So the faulting code path is the newly deployed validator, not a pre-existing path.
  - `F13` ✔ Neither derivatives-adjacent peer shows the order-service symptom: pricing-engine error_rate is flat (min 0.1%, max 0.32%, mean 0.22%, 12:12Z bucket 0.19%) and portfolio-service error_rate is flat (min 0.1%, max 0.32%, mean 0.21%, 12:12Z bucket 0.22%). Both remain at ~0.2% for the entire incident.
  - `F16` ✔ No config change touched order-service or any dependency in the 11:00-14:00Z window: the only change is cfg-2101 on notification-service (email.batch_size 200->250 at 11:21Z), which has no functional link to the F&O order path.
  - `F17` ✔ Historically, no precedent matches the NPE/null-marginProfile signature. The closest order-service precedents are HIST-115 connection-pool exhaustion, HIST-110 ledger-db IOPS throttling (both latency/resource signatures, not null-pointer validation errors) and HIST-103 external exchange outage (504/timeouts). None involves a null field from a validation refactor.
**Assumptions**
  - A shared component actually degraded; no telemetry for ledger-db/redis-cache/kafka was retrieved.
  - Shared-component failure could produce a validator-level NullPointerException rather than a connection/timeout error.

### H4: Adjacent service change (cfg-2101 on notification-service / other adjacent change) rejecting derivative orders
An adjacent change near onset — the only recorded config change, cfg-2101 on notification-service (email.batch_size 200->250 at 11:21Z) — or some other adjacent-service rollout is rejecting derivative order requests, causing order-service 500s without order-service code changing.

**Causal chain**
1. cfg-2101 changes notification-service email batching at 11:21Z, ~1h before onset [F3, F16]
2. This change reaches order-service's F&O order path *(assumed)*
3. Derivative orders get rejected -> 500s [F10]

**Supporting evidence**
  - `F3` ✔ The only config change in the window was cfg-2101 on notification-service: key email.batch_size changed 200 -> 250 at 2026-03-03T11:21:00Z by config-bot ('Tune email batching'). No config change touched order-service or any of its dependencies.
**Contradicting evidence**
  - `F16` ✔ No config change touched order-service or any dependency in the 11:00-14:00Z window: the only change is cfg-2101 on notification-service (email.batch_size 200->250 at 11:21Z), which has no functional link to the F&O order path.
  - `F8` ✔ First order-service ERROR in the window is at 2026-03-03T12:12:31Z: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId=9326cc69'. 426 ERROR lines match between 12:12:31Z and 13:58:50Z.
  - `F9` ✔ The error signature is a NullPointerException on a null marginProfile, surfacing as the client-facing 500: two dominant patterns cover all 426 errors -- 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:<n>) orderType=FNO' (224) and 'order rejected with <n>: java.lang.NullPointerException: marginProfile is null for account <n>' (202). Both patterns are filtered to orderType=FNO, the derivatives order type.
  - `F13` ✔ Neither derivatives-adjacent peer shows the order-service symptom: pricing-engine error_rate is flat (min 0.1%, max 0.32%, mean 0.22%, 12:12Z bucket 0.19%) and portfolio-service error_rate is flat (min 0.1%, max 0.32%, mean 0.21%, 12:12Z bucket 0.22%). Both remain at ~0.2% for the entire incident.
  - `F15` ✔ The NPE stack trace is inside the exact class the deploy introduced: T1/F8-9 report 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88)' with 'marginProfile is null', filtered to orderType=FNO. So the faulting code path is the newly deployed validator, not a pre-existing path.
**Assumptions**
  - notification-service has a functional link to the F&O order path (no evidence for this).
**Rejected because:** notification-service has no functional link to the order path (F16); the failure is an in-process NullPointerException in order-service's own newly deployed MarginCheckValidator (F8, F9, F15), not an external rejection; and all adjacent peers are flat (F13).

## Facts (verified observations)
- Dep-5101 deployed order-service v2.14.0 at 2026-03-03T12:10:00Z with summary 'Refactor order validation; add margin check for F&O orders' and changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'; status live, rolled_back_at null. [F1, F14]
- The first order-service ERROR appeared at 2026-03-03T12:12:31Z — 2 minutes after dep-5101 landed — as 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId=9326cc69', with 426 matching ERROR lines through 13:58:50Z. [F8, F14]
- The error signature is a null marginProfile NPE surfacing as the client-facing 500: 224 occurrences of 'NullPointerException at MarginCheckValidator.validate(...) orderType=FNO' and 202 of 'order rejected with <n>: java.lang.NullPointerException: marginProfile is null for account <n>', both filtered to orderType=FNO. [F9]
- order-service error_rate stepped between the 12:05Z and 12:12Z buckets from 0.20% to 18.49%, remaining elevated (17.93-18.97%) in every bucket through 13:57Z against a 11:30-12:05Z baseline of 0.18-0.27%. [F10]
- The faulting stack frame is inside the exact class the deploy introduced, so the failing code path is the newly deployed validator rather than a pre-existing path. [F15]
- No config change touched order-service or any of its dependencies in the 11:00-14:00Z window; the only change was cfg-2101 on notification-service (email.batch_size 200->250 at 11:21Z), which has no functional link to the F&O order path. [F16, F3]
- All four derivatives-adjacent peers stayed flat over the incident: market-data-service (min 0.1%, max 0.32%, 12:12Z bucket 0.22%), exchange-gateway (12:12Z bucket 0.17%), pricing-engine (12:12Z bucket 0.19%) and portfolio-service (12:12Z bucket 0.22%). [F11, F12, F13]
- The only other deployment in the 24h window, dep-5088 to portfolio-service v4.2.1 ('bump grpc 1.62 -> 1.63'), landed at 2026-03-03T09:36:00Z — ~2h36m before onset — touches only a gRPC version, and can reach order-service only indirectly via shared ledger-db/redis-cache. [F2]
- order-service inherits shared upstream paths (ledger-db shared=true, redis-cache shared=true via pricing-engine, kafka, market-data-service), so a shared-component fault could in principle produce the symptom; the flat peer error rates argue against it here. [F4, F5]
- No historical precedent matches the null-marginProfile NPE signature; the closest order-service precedents are connection-pool exhaustion (HIST-115), ledger-db IOPS throttling (HIST-110) and an external exchange outage (HIST-103), all latency/resource or timeout signatures. [F6]

## Assumptions (not verified)
- The 12:05Z -> 12:12Z step is attributable to the new validator code path and not to a coincident resource-exhaustion event: order-service latency, request_rate, cpu_pct and db_pool_in_use were not retrieved, so a resource-pressure contпоribution to the 500s is assumed absent rather than verified.
- No unrecorded infrastructure-level or feature-flag change landed within ~2 minutes of onset: the change inventory (2 deployments, 1 config change over 24h) is assumed complete; an infra/cloud change feed and a feature-flag service were not queried.
- The reviewed hypothesis ranking and confidences (H1 0.85, H2 0.12, H3 0.06, H4 rejected 0.03) are taken as fixed from shared state and are not re-derived here.
- For H1's mechanism to produce the observed client-facing 500, the NPE thrown in MarginCheckValidator is assumed to be uncaught on the request path and translated into an HTTP 500 by order-service's error handling (the 224 'order rejected with <n>: ... marginProfile is null' lines are consistent with this but the handler itself was not inspected).
- The 426 ERROR lines are assumed to be the full population of failed requests producing the ~18% error_rate elevation (the log query was truncated to 40 of 426 matches), i.e. no distinct error family is hidden in the unreturned portion.

## Timeline
- `2026-03-03T09:36:00Z` dep-5088 deployed portfolio-service v4.2.1 (gRPC 1.62 -> 1.63); the only other deployment in the window, ~2h36m before onset. [F2, F18]
- `2026-03-03T11:21:00Z` cfg-2101 applied to notification-service (email.batch_size 200 -> 250) by config-bot; the only config change in the window and unrelated to the order path. [F3, F16]
- `2026-03-03T11:30:00Z` Baseline period: order-service error_rate 0.18-0.27% avg. [F10]
- `2026-03-03T12:05:00Z` Last pre-incident bucket: order-service error_rate 0.20% avg. [F10]
- `2026-03-03T12:10:00Z` dep-5101 deploys order-service v2.14.0 ('Refactor order validation; add margin check for F&O orders'; new MarginCheckValidator, legacy OrderRules.validateMargin() removed). Status live. [F1, F7, F14]
- `2026-03-03T12:12:00Z` order-service error_rate steps to 18.49% avg and stays elevated (17.93-18.97%) through 13:57Z; peers market-data, exchange-gateway, pricing-engine and portfolio-service remain flat ~0.2%. [F10, F11, F12, F13]
- `2026-03-03T12:12:31Z` First order-service ERROR: NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88), orderType=FNO, orderId=9326cc69. [F8, F15]
- `2026-03-03T12:12:44Z` Both dominant error patterns established (MarginCheckValidator NPE and 'marginProfile is null for account <n>'), all filtered to orderType=FNO; 426 matching ERROR lines total. [F9]
- `2026-03-03T12:22:00Z` Incident INC-001 reported by on-call SRE: HTTP 500 on POST /v1/orders, order-service error-rate alert firing. []
- `2026-03-03T13:58:50Z` Last matching ERROR line in the observed window. [F8]
- `2026-03-03T13:59:00Z` End of data window: dep-5101 still live, rolled_back_at null; error_rate still ~18% (not recovered). [F1, F10]

## Recommended actions
- **[immediate]** Immediately roll back dep-5101 (order-service v2.14.0) to the last known-good release, or, if rollback is not feasible in the incident window, hotfix MarginCheckValidator to null-guard / explicitly load marginProfile before validation and redeploy. -- dep-5101 is the change that introduced the faulting class and predates the first 500 by 2 minutes; the error signature (MarginCheckValidator NPE 'marginProfile is null', orderType=FNO only) is exactly the change's stated scope, and error_rate has been stuck at ~18% with error volume flat since 12:12Z — the change is still live and the symptom is ongoing. [F1, F8, F9, F10, F14, F15]
- **[immediate]** During the incident window, if rollback is delayed, discriminate the remaining low-confidence alternatives cheaply: query order-service latency_p99/request_rate/cpu_pct/db_pool_in_use and the shared components' (ledger-db, redis-cache, kafka) health for 12:10-12:22Z; a pure code-path failure should show no local saturation. Note that a shared-component or upstream-data origin is already disfavored because market-data, exchange-gateway, pricing-engine and portfolio-service error_rate stayed flat at ~0.2%. -- Resource-saturation metrics for order-service and shared components were never retrieved, so H2/H3 cannot be formally closed; this check separates a pure code-path NPE from resource-pressure or shared-datastore causation without slowing the rollback of the confirmed suspect. [F11, F12, F13, F19]
- **[short_term]** Verify mitigation by confirming order-service error_rate returns to the 0.18-0.27% baseline and that MarginCheckValidator NullPointerException / 'marginProfile is null' log lines cease after the rollback or hotfix; also confirm rolled_back_at is populated for dep-5101. -- The elevated error_rate (18.49% step at 12:12Z, still 17.93-18.97% through 13:57Z) is the discriminating signal; confirming it recedes closes the loop on the rollback and distinguishes this failure from any residual data-side cause. [F10, F8, F1]
- **[short_term]** Add a null-marginProfile guard plus explicit marginProfile population/load path in MarginCheckValidator, with F&O unit/integration coverage asserting the validator never dereferences a null profile and returns a typed business error rather than an uncaught NPE. -- The root cause is a missing null guard on a newly added validation path (uncaught NPE produced the client-facing 500); a typed rejection instead of a 500 is the correct product behavior for missing margin data. [F9, F15]
- **[long_term]** Instrument and alert on validation-path-specific error metrics for order-service (per-validator / per-orderType error counters) so a refactor-induced NPE surface is caught in the 12:05-12:12Z style step by an alert rather than by support tickets. -- The 0.20%->18.49% step at 12:12Z was visible in error_rate, but detection came from a support-reported spike 10 minutes later at 12:22Z; finer-grained signal shortens time-to-detect for similar local code-path failures. [F10, F8]
- **[long_term]** Require canary/staged rollout with mandatory rollback criteria for order-service validation and margin-path changes, and enforce a null-safety review checklist on refactors that remove legacy validators (legacy OrderRules.validateMargin() -> MarginCheckValidator). -- dep-5101 was fully live with no rollback, and the change removed a legacy validator in favor of a new one without a null guard; staged rollout plus review gates would have contained the F&O-only blast radius. [F1, F14, F15]

## Data gaps
- Order-service latency_p99_ms, request_rate, cpu_pct and db_pool_in_use were never retrieved — cannot confirm whether the 500s coincide with local latency/saturation (i.e. pure code-path failure vs. resource-pressure-driven).
- No telemetry was queried for shared components (ledger-db, redis-cache, kafka) or pricing-engine for 12:10-12:22Z, so the shared-infrastructure path (H3) cannot be formally closed by observation, only disfavored by the flat peer error rates.
- portfolio-service error logs and ledger-db/user-db queries were not run (tool budget exhausted), so the origin of the null marginProfile data (H2: upstream data vs. local mapping/loading) was not inspected.
- The narrowed-window config-change re-query (11:00Z-13:00Z) was not executed (budget_exhausted); the wider 24h query returned count=1 with partial=false, so an unrecorded config change on a shared component close to onset cannot be fully excluded.
- Deployment/config inventory completeness is unverified: only 2 deployments and 1 config change over 24h, with no broader infra/cloud change feed or feature-flag source queried.
- Dependency map is limited to depth 2 from order-service/portfolio-service; transitive owners upstream of ledger-db/kafka are not inventoried.
- exchange-gateway's upstream_error_rate and upstream_latency_ms were not queried, so a downstream dependency of exchange-gateway being impacted cannot be fully ruled out (only its own error_rate is known flat).
- The order-service ERROR log result was truncated (40 of 426 matches returned), so per-instance/per-endpoint distribution of the 500s is unconfirmed; onset time and top patterns are reliable but not exhaustive.
- No historical incident matches the null-marginProfile NPE signature, so it is unconfirmed whether this is a recurring known pattern.
- Whether dep-5101 was rolled back after 13:59Z is unknown (window ends at latest data; rolled_back_at null as of 13:59Z), so mitigation effectiveness is unverified.
- One tool failure (tc-011: query_metrics error_rate for pricing-service, not_found) — immaterial to the conclusion since pricing-engine error_rate was obtained via F13.

## What would change this conclusion
- A subsequent observation that the MarginCheckValidator null guard / marginProfile loading path already existed and the NPE arose from a source other than the validator dereference (e.g. an unpopulated field injected upstream) would shift the origin toward H2 (data/source owner).
- Evidence that order-service was already erroring on F&O orders before 12:10:00Z, or that error_rate was already elevated in the 12:05Z bucket, would break the deploy-to-onset causal link and reopen the alternative hypotheses.
- A fast, complete recovery after some other change (e.g. an unrecorded config/feature-flag flip around 12:10-12:22Z that was reverted) rather than after rollback of dep-5101 would indicate the real trigger lay outside the deployment.
- Detection of simultaneous degradation in a shared component (ledger-db, redis-cache, kafka) or in pricing-engine within 12:10-12:22Z, correlated with order-service failures, would revive H3 (shared infrastructure) as the origin.
- Proof that portfolio-service or ledger-db began emitting/omitting null marginProfile data for F&O accounts at or shortly before 12:10Z — with the new validator merely exposing pre-existing bad data — would make H2 the better-ranked origin.
- Any indication that the errors are not confined to orderType=FNO (e.g. the truncated log sample hides non-FNO failures) would weaken the scope match between dep-5101's stated change and the blast radius.
- A material resource-saturation signal on order-service (pool exhaustion, CPU saturation, latency spike) coincident with onset would suggest a resource-driven failure mode rather than the deterministic null-dereference, changing the recommended immediate mitigation toward capacity/pool remediation.

## Investigation stats
- latency 94.15s, LLM calls 14, tokens 88620 (in 71392 / out 17228)
- tool calls 20 (1 failed, 0 partial, 0 recovered by retry), findings 19/19 verified
