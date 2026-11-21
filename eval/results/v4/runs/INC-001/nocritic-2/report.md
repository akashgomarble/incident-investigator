# Incident investigation report: INC-001
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
Root cause identified with medium confidence (0.60): order-service deployment dep-5101 (v2.14.0, rolled out 12:10:08Z, 2026-03-03) introduced the new MarginCheckValidator, which dereferences marginProfile without a null guard (MarginCheckValidator.java:88) and throws NullPointerException that is returned as HTTP 500 for FNO (derivatives) orders only. This matches the exact change set of the deploy, the FNO-only scope of all 426 error entries, the error onset 2m23s after rollout completion, and the absence of any other deployment or config change across all services in the window. Competing hypotheses (bad margin data, shared dependency degradation, deploy-independent order-service bug) are markedly weaker and largely contradicted by the strict FNO scope, the flat p99 latency, and the absence of any other change in the window. The one material gap is that no dependency telemetry was queried, so a silent shared-component degradation cannot be positively excluded from metrics — the exclusion rests on change-list evidence only.

## Probable root cause
**order-service v2.14.0 bad deployment: MarginCheckValidator NPE on FNO orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.14.0 bad deployment: MarginCheckValidator NPE on FNO orders | bad_deployment | order-service | dep-5101 | 0.60 (medium) | active |
| 2 | H2: FNO margin data missing/null in backing store (data_issue) - new validator exposes missing marginProfile | data_issue | ledger-db / margin data source | null | 0.20 (low) | active |
| 3 | H3: Shared dependency degradation returns null margin data (dependency_failure on ledger-db/redis-cache) | dependency_failure | ledger-db or redis-cache | null | 0.12 (low) | active |
| 4 | H4: Reporter's premise: order-service itself regressed independent of the deploy (generic order-service bug/regression) | unknown | order-service | null | 0.05 (low) | rejected |

### H1: order-service v2.14.0 bad deployment: MarginCheckValidator NPE on FNO orders
order-service deploy dep-5101 (v2.14.0, 12:10:08Z) replaced legacy OrderRules.validateMargin() with a new MarginCheckValidator in the validation chain. The new validator dereferences marginProfile without a null guard (MarginCheckValidator.java:88), throwing NullPointerException for FNO (derivatives) orders. The NPE is uncaught upstream, so POST /v1/orders returns HTTP 500. Because the new validator only runs on F&O orders, only derivatives orders fail, matching the ticket skew.

**Causal chain**
1. order-service deploys dep-5101 v2.14.0 at 12:10:08Z, introducing new MarginCheckValidator and removing legacy OrderRules.validateMargin() [F13, F14, F15, F2]
2. New validator code path runs during F&O order validation; marginProfile from account/lookup is null for these accounts [F2, F8, F9]
3. MarginCheckValidator.validate (line 88) dereferences marginProfile without a null guard -> NullPointerException [F8, F9]
4. NPE propagates as order rejection -> HTTP 500 'order rejected with 500' on POST /v1/orders [F7, F8, F12]
5. Only F&O orders traverse the new validator, so failures are confined to orderType=FNO [F9, F10]

**Supporting evidence**
  - `F2` ✔ The order-service v2.14.0 change explicitly touches order validation and adds a margin check for F&O (derivatives) orders: changes are ["new MarginCheckValidator in validation chain","remove legacy OrderRules.validateMargin()"], status live, rolled_back_at null. This is on the code path for the derivatives orders named in the ticket description.
  - `F7` ✔ order-service error_rate jumped from a ~0.2% baseline to 14.74% in the 12:10–12:20 bucket and stayed elevated ~18-20% through the end of data (13:50 bucket avg 18.76%, max 19.82%); baseline buckets 10:00–12:00 were all 0.17–0.25% avg.
  - `F8` ✔ First order-service error in the window is at 12:12:31Z: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId=9326cc69'; the two dominant patterns are that NPE (224 occurrences) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202 occurrences).
  - `F9` ✔ The 500 error signature is java.lang.NullPointerException: marginProfile is null, thrown inside MarginCheckValidator.validate (line 88), returning HTTP 500 'order rejected with 500'; every logged error instance carries orderType=FNO (derivatives/futures & options).
  - `F10` ✔ Only FNO (derivatives) orders appear in the error log: all 426 ERROR entries carry orderType=FNO; the filtered query for errors after 12:22 also returned exclusively 'orderType=FNO' NPE patterns (204 matches). No non-FNO order type appears among failures.
  - `F13` ✔ An order-service deployment completed just before onset: INFO 'deployment dep-5101 (v2.14.0) rollout complete, all pods ready' at 2026-03-03T12:10:08Z, ~2m23s before the first error at 12:12:31Z (an earlier 'config reloaded (no changes)' at 12:07:51Z and 'health check ok' at 11:56:19Z).
  - `F14` ✔ In the 11:00Z–13:59Z window exactly one deployment occurred across all services: order-service dep-5101 v2.14.0 at 2026-03-03T12:10:00Z (author r.iyer), status live, rolled_back_at null. No other service deployed.
  - `F15` ✔ dep-5101's change set is precisely on the derivatives validation path: changes are ["new MarginCheckValidator in validation chain","remove legacy OrderRules.validateMargin()"], summary "Refactor order validation; add margin check for F&O orders". This is the exact code path named in the ticket (derivatives orders).
  - `F12` ✔ api-gateway error_rate also rose in the same window, from ~0.2% baseline to avg 2.66% at the 12:09 bucket and steady ~3.3-3.45% thereafter — consistent with propagating order-service 500s but at a much smaller magnitude (~3.4% vs order-service ~18%).
  - `F11` ✔ order-service p99 latency is flat across the incident: 12:10 bucket avg 151.76ms, 12:00 bucket avg 150.32ms, 13:50 bucket avg 150.55ms — no latency degradation accompanies the error spike.
**Contradicting evidence**
  - (none)
**Assumptions**
  - marginProfile is expected to be non-null for some accounts and the legacy validateMargin() tolerated/read it differently; no field-by-field diff of old vs new code was available
  - the NPE is not caught by an outer handler and converted to 500 (verified by error signature 'order rejected with 500')

### H2: FNO margin data missing/null in backing store (data_issue) - new validator exposes missing marginProfile
The origin of the fault is the data: accounts placing F&O orders have no marginProfile record (or an unpopulated one) in the upstream margin/account data. The new MarginCheckValidator is merely the first code to strictly require it, so it NPEs. Fix would be data-side (populate/repair marginProfile) rather than code rollback.

**Causal chain**
1. F&O-trading accounts lack a marginProfile row (or the field is null) in the margin source *(assumed)*
2. Prior code (legacy OrderRules.validateMargin) tolerated the missing profile, masking the gap [F2]
3. New MarginCheckValidator requires the field and dereferences it -> NPE at line 88 [F8, F9]
4. NPE -> HTTP 500 for FNO orders only [F9, F10]

**Supporting evidence**
  - `F8` ✔ First order-service error in the window is at 12:12:31Z: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId=9326cc69'; the two dominant patterns are that NPE (224 occurrences) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202 occurrences).
  - `F9` ✔ The 500 error signature is java.lang.NullPointerException: marginProfile is null, thrown inside MarginCheckValidator.validate (line 88), returning HTTP 500 'order rejected with 500'; every logged error instance carries orderType=FNO (derivatives/futures & options).
  - `F2` ✔ The order-service v2.14.0 change explicitly touches order validation and adds a margin check for F&O (derivatives) orders: changes are ["new MarginCheckValidator in validation chain","remove legacy OrderRules.validateMargin()"], status live, rolled_back_at null. This is on the code path for the derivatives orders named in the ticket description.
**Contradicting evidence**
  - `F1` ✔ In window 2026-03-03T06:00Z–13:59Z there were 2 deployments across all services: portfolio-service v4.2.1 at 2026-03-03T09:36:00Z ("bump grpc 1.62 -> 1.63", live, not rolled back) and order-service v2.14.0 at 2026-03-03T12:10:00Z by r.iyer ("Refactor order validation; add margin check for F&O orders", live, rolled_back_at null). No other service deployed in the window.
  - `F3` ✔ Only one config change occurred across all services in the window: notification-service key email.batch_size changed 200 -> 250 at 2026-03-03T11:21:00Z by config-bot ("Tune email batching"). A check of the prior week (2026-02-25T00:00Z–2026-03-03T06:00Z) returned count 0 for both deployments and config changes, so nothing changed in the preceding days.
  - `F17` ✔ No config change at all was recorded for order-service (or any service) in the extended window 2026-02-20T00:00Z–2026-03-03T13:59Z except cfg-2101: order-service config query returned count 0; the all-services query over the same extended window returned count 1 (cfg-2101). So there is no config-based explanation for the onset.
  - `F18` ✔ order-service's dependency set in the window is the same as previously mapped and includes three shared components: ledger-db (postgres, shared:true), redis-cache (redis, shared:true), kafka (kafka, shared:true), plus direct http deps pricing-engine, exchange-gateway and queue-produce kafka; api-gateway -> order-service. None of these components had a deployment or config change in the window (only the order-service deploy), so no shared-component change coincides with the onset.
**Assumptions**
  - marginProfile data is stored in the backing store and was already null before 12:10Z
  - no code path in the new deploy could itself produce a null marginProfile (e.g. a lookup key change)

### H3: Shared dependency degradation returns null margin data (dependency_failure on ledger-db/redis-cache)
A shared component order-service calls (ledger-db, or redis-cache holding margin profiles) degraded in the onset window, returning null/empty account-margin data. The new MarginCheckValidator surfaces this as an NPE. Blast radius would extend to other order-service operations, but only the new strict validator fails hard.

**Causal chain**
1. ledger-db or redis-cache degrades around 12:10-12:12Z *(assumed)*
2. Margin profile lookup returns null instead of a profile *(assumed)*
3. MarginCheckValidator NPEs on the null profile [F8, F9]
4. 500 on POST /v1/orders for the affected orders [F7, F9]

**Supporting evidence**
  - `F4` ✔ order-service direct dependencies (caller->callee edges): order-service -> ledger-db (db), -> pricing-engine (http), -> exchange-gateway (http), -> kafka (queue-produce); api-gateway -> order-service. Transitive: pricing-engine -> market-data-service (http) and -> redis-cache (cache); exchange-gateway -> exchange-api (external, has_telemetry false). Shared components order-service relies on: ledger-db (postgres), redis-cache (redis), kafka (kafka) — all marked "shared":true; each is a possible cross-service blast path.
  - `F6` ✔ Sharable-component incident precedents exist for two of order-service's shared dependencies: HIST-112 (2026-02-03) "Settlement consumer stalled after partition reassignment" on kafka, and HIST-104 (2025-11-05) "notification-service lag after Kafka broker rebalance" on kafka; HIST-107 (2025-12-09) "Cold cache storm after redis failover" on redis-cache + market-data-service.
**Contradicting evidence**
  - `F17` ✔ No config change at all was recorded for order-service (or any service) in the extended window 2026-02-20T00:00Z–2026-03-03T13:59Z except cfg-2101: order-service config query returned count 0; the all-services query over the same extended window returned count 1 (cfg-2101). So there is no config-based explanation for the onset.
  - `F18` ✔ order-service's dependency set in the window is the same as previously mapped and includes three shared components: ledger-db (postgres, shared:true), redis-cache (redis, shared:true), kafka (kafka, shared:true), plus direct http deps pricing-engine, exchange-gateway and queue-produce kafka; api-gateway -> order-service. None of these components had a deployment or config change in the window (only the order-service deploy), so no shared-component change coincides with the onset.
  - `F11` ✔ order-service p99 latency is flat across the incident: 12:10 bucket avg 151.76ms, 12:00 bucket avg 150.32ms, 13:50 bucket avg 150.55ms — no latency degradation accompanies the error spike.
  - `F14` ✔ In the 11:00Z–13:59Z window exactly one deployment occurred across all services: order-service dep-5101 v2.14.0 at 2026-03-03T12:10:00Z (author r.iyer), status live, rolled_back_at null. No other service deployed.
  - `F16` ✔ Only one config change occurred anywhere in the 11:00Z–13:59Z window: notification-service email.batch_size 200->250 at 2026-03-03T11:21:00Z (cfg-2101, config-bot). It does not touch order-service or any of its dependencies, and predates onset by ~51 min.
**Assumptions**
  - a dependency returned null rather than erroring/timing out
  - the degradation left no deploy/config trace (it is a silent degradation, not a change)

### H4: Reporter's premise: order-service itself regressed independent of the deploy (generic order-service bug/regression)
order-service developed an internal defect (its own code/logic) causing 500s on POST /v1/orders, not tied to dep-5101's specific change to margin validation, and the deploy is coincidental. Would predict failures across order types and/or attributable to a pre-existing code path.

**Causal chain**
1. order-service has an intrinsic defect in order placement *(assumed)*
2. POST /v1/orders returns HTTP 500 [F7]

**Supporting evidence**
  - `F7` ✔ order-service error_rate jumped from a ~0.2% baseline to 14.74% in the 12:10–12:20 bucket and stayed elevated ~18-20% through the end of data (13:50 bucket avg 18.76%, max 19.82%); baseline buckets 10:00–12:00 were all 0.17–0.25% avg.
**Contradicting evidence**
  - `F8` ✔ First order-service error in the window is at 12:12:31Z: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId=9326cc69'; the two dominant patterns are that NPE (224 occurrences) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202 occurrences).
  - `F9` ✔ The 500 error signature is java.lang.NullPointerException: marginProfile is null, thrown inside MarginCheckValidator.validate (line 88), returning HTTP 500 'order rejected with 500'; every logged error instance carries orderType=FNO (derivatives/futures & options).
  - `F10` ✔ Only FNO (derivatives) orders appear in the error log: all 426 ERROR entries carry orderType=FNO; the filtered query for errors after 12:22 also returned exclusively 'orderType=FNO' NPE patterns (204 matches). No non-FNO order type appears among failures.
  - `F13` ✔ An order-service deployment completed just before onset: INFO 'deployment dep-5101 (v2.14.0) rollout complete, all pods ready' at 2026-03-03T12:10:08Z, ~2m23s before the first error at 12:12:31Z (an earlier 'config reloaded (no changes)' at 12:07:51Z and 'health check ok' at 11:56:19Z).
  - `F15` ✔ dep-5101's change set is precisely on the derivatives validation path: changes are ["new MarginCheckValidator in validation chain","remove legacy OrderRules.validateMargin()"], summary "Refactor order validation; add margin check for F&O orders". This is the exact code path named in the ticket (derivatives orders).
  - `F7` ✔ order-service error_rate jumped from a ~0.2% baseline to 14.74% in the 12:10–12:20 bucket and stayed elevated ~18-20% through the end of data (13:50 bucket avg 18.76%, max 19.82%); baseline buckets 10:00–12:00 were all 0.17–0.25% avg.
**Assumptions**
  - the error onset at 12:12:31Z is not causally related to the 12:10:08Z deploy of the F&O margin refactor
**Rejected because:** The first and dominant error is a NullPointerException inside MarginCheckValidator (new in dep-5101), exclusively on orderType=FNO - the exact code path and order type the deploy targeted. Onset 2m23s after rollout completion, no other service changed. A deploy-independent bug cannot explain the strict FNO-only scope and the validator-specific stack frame.

## Facts (verified observations)
- Only two deployments occurred across all services between 06:00Z and 13:59Z: portfolio-service v4.2.1 at 09:36Z and order-service v2.14.0 (dep-5101, author r.iyer) at 12:10Z; no other service deployed in the window. [F1, F14]
- dep-5101's change set is exactly on the derivatives validation path: 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()', summary 'Refactor order validation; add margin check for F&O orders'; status live with rolled_back_at null at fetch time. [F2, F15]
- The first order-service error in the window is a NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) with orderType=FNO, orderId=9326cc69; dominant patterns are that NPE (224 occurrences) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202 occurrences). [F8]
- The 500 signature is java.lang.NullPointerException: marginProfile is null, thrown inside MarginCheckValidator.validate at line 88, surfacing as HTTP 500 'order rejected with 500'. [F9]
- All 426 ERROR log entries carry orderType=FNO; no non-FNO order type appears among failures, and a post-12:22 filtered query returned exclusively FNO NPE patterns (204 matches). [F10]
- order-service error_rate jumped from a ~0.2% baseline to 14.74% in the 12:10-12:20 bucket and stayed at ~18-20% through the end of data (13:50 bucket avg 18.76%, max 19.82%). [F7]
- order-service p99 latency is flat across the incident (12:10 bucket 151.76ms, 12:00 bucket 150.32ms, 13:50 bucket 150.55ms) — no latency degradation accompanies the error spike. [F11]
- api-gateway error_rate rose in the same window from ~0.2% baseline to ~3.3-3.45%, consistent with propagating order-service 500s at a much smaller magnitude. [F12]
- The only config change anywhere in the window was notification-service email.batch_size 200->250 at 11:21Z (cfg-2101), which touches neither order-service nor its dependencies and predates onset by ~51 minutes; order-service had zero config changes in the extended window 2026-02-20 to 2026-03-03T13:59Z. [F3, F16, F17]
- order-service's dependency set includes shared components ledger-db (postgres), redis-cache (redis) and kafka (kafka), plus http deps pricing-engine and exchange-gateway and queue-produce kafka; none of these had a deployment or config change in the window. [F4]
- Historical precedent for this exact signature (500 on POST /v1/orders for derivatives orders, margin-validator NPE) was not found; nearest precedents involve different mechanisms (connection pool misconfiguration, ledger-db IOPS throttling, external exchange 504, JWT library upgrade). [F5, F19]

## Assumptions (not verified)
- Assumption: marginProfile is legitimately non-null for a subset of accounts and the legacy OrderRules.validateMargin() tolerated or handled a null marginProfile differently; no field-by-field diff of old vs new code was available.
- Assumption: the NPE from MarginCheckValidator is not caught by an outer handler that could convert it to something other than a 500; verified only via the error signature 'order rejected with 500'.
- Assumption: the strict FNO-only scope of failures (based on 426 pattern-aggregated error entries and returned samples) is representative and holds exhaustively — log results were truncated:true.
- Assumption: the absence of deploy/config entries for ledger-db, redis-cache, kafka, pricing-engine and exchange-gateway in the window implies they did not change; this is not equivalent to them being healthy (no dependency telemetry was queried).
- Assumption: no in-flight or partial rollback of dep-5101 occurred after 13:59Z; the deployments list showed rolled_back_at null at fetch time only.
- (demoted from facts: no verified observation backs it) The order-service v2.14.0 rollout completed at 2026-03-03T12:10:08Z ('all pods ready'), approximately 2m23s before the first error at 12:12:31Z.

## Timeline
- `2026-03-03T09:36:00Z` portfolio-service v4.2.1 deployed (grpc 1.62->1.63); the only other deploy in the 06:00Z-13:59Z window, ~2.5h before onset. [F1]
- `2026-03-03T11:21:00Z` notification-service config change cfg-2101 (email.batch_size 200->250) — the only config change in the window; unrelated to order-service, predates onset by ~51 min. [F3, F16]
- `2026-03-03T12:10:00Z` order-service deploy dep-5101 (v2.14.0, author r.iyer) begins/lives: adds MarginCheckValidator, removes legacy OrderRules.validateMargin() — changes on the F&O validation path. [F1, F2, F14, F15]
- `2026-03-03T12:10:08Z` dep-5101 rollout complete, all pods ready (INFO). [F13]
- `2026-03-03T12:10:00Z` order-service error_rate bucket jumps from ~0.2% baseline to 14.74% (12:10-12:20 bucket). [F7]
- `2026-03-03T12:12:31Z` First order-service error: NullPointerException at MarginCheckValidator.validate(line 88), orderType=FNO, orderId=9326cc69 — 2m23s after rollout completion. [F8, F13]
- `2026-03-03T12:22:00Z` Incident INC-001 reported: spike in failed order placements, mostly derivatives orders; HTTP 500 on POST /v1/orders. []
- `2026-03-03T12:22:00Z` api-gateway error_rate elevated to ~2.66% at the 12:09 bucket and ~3.3-3.45% thereafter, consistent with propagating order-service 500s. [F12]
- `2026-03-03T13:50:00Z` order-service error_rate remains elevated: 13:50 bucket avg 18.76%, max 19.82%; p99 latency still flat at ~150ms. [F7, F11]
- `2026-03-03T13:59:00Z` Latest data: dep-5101 still live, rolled_back_at null; all 426 error entries remain exclusively orderType=FNO. [F10, F2]

## Recommended actions
- **[immediate]** Roll back order-service dep-5101 (v2.14.0) to the last known-good version now — this is the single change that coincides with onset and touches the exact failing path. -- dep-5101 is still live with rolled_back_at null; it added the MarginCheckValidator that throws the NPE and removed the legacy OrderRules.validateMargin() that presumably tolerated null marginProfile. Rollback is the direct mitigation for the FNO-only 500s. [F1, F2, F8, F9, F13, F14, F15]
- **[immediate]** After rollback, verify recovery: confirm order-service error_rate returns to the ~0.2% baseline and that POST /v1/orders for orderType=FNO succeeds, then confirm api-gateway error_rate returns to baseline. -- The error_rate jump (0.2% -> ~19%) and api-gateway propagation are the observed signals; recovery should mirror them. If error_rate stays elevated after rollback, H2/H3 (data or dependency cause) remain live and must be pursued. [F7, F11, F12]
- **[short_term]** Fix MarginCheckValidator.java:88 to handle a null marginProfile (explicit null guard or default/fallback to legacy behavior) and ship in a follow-up v2.14.x release; add a unit test for FNO orders with null marginProfile before re-deploying the F&O margin check. -- The NPE is a missing null guard at a specific line; the fix must preserve the intended margin check for F&O orders without 500-ing on null profiles. [F2, F8, F9, F15]
- **[short_term]** Query order-service dependency telemetry around onset (ledger-db error rate/latency/IOPS, redis-cache hit rate/evictions, kafka broker/consumer lag, pricing-engine and exchange-gateway call status codes) to close the remaining gap on H3 and confirm no silent shared-component degradation contributed. -- The exclusion of shared-dependency degradation currently rests only on absence of deploy/config entries, not on dependency health metrics — this data was never queried. [F4, F18, F6]
- **[short_term]** Verify the marginProfile data path against failing accounts: check whether marginProfile records for the affected accounts are null in the backing store and whether the new validator's lookup key differs from legacy OrderRules.validateMargin(), to confirm the fault is code-side rather than data-side. -- H2 (null margin data in the store) remains formally possible and would require a data-side repair instead of / in addition to the code fix; currently supported only indirectly. [F8, F9, F2]
- **[long_term]** Add pre-deployment integration coverage that exercises the F&O validation path with null/absent marginProfile, and add a deploy-correlated error-rate canary (auto-alert or auto-rollback on FNO-only 500 spike within minutes of rollout). -- The incident was detectable 2m23s after rollout via a clear NPE signature and a 2-order-of-magnitude error-rate jump; gating deploys on this path and alerting on deploy-correlated error-rate deltas prevents recurrence. [F7, F8, F10, F13, F15]
- **[long_term]** Fall back to the change-diff discipline: require a field-by-field behavioral comparison of removed legacy code (OrderRules.validateMargin) versus its replacement before merging refactors on the order validation chain. -- The refactor removed legacy behavior that presumably tolerated null marginProfile; no field-by-field old-vs-new diff was available during this investigation, which slowed confirmation. [F2, F15]

## Data gaps
- No dependency telemetry (error rate, latency, connection pool, broker/consumer lag, cache hit rate) was queried for order-service, ledger-db, kafka, redis-cache, pricing-engine or exchange-gateway, so a silent shared-component or downstream degradation in the onset window cannot be positively excluded from metrics — only from change-list evidence.
- No direct query of dependency call status codes around onset, so it cannot be confirmed from telemetry that the NPE is self-contained in order-service rather than triggered by a dependency returning null marginProfile.
- Tool-call budget was exhausted before querying error_rate/latency for portfolio-service and pricing-engine, so the set of genuinely unaffected services is not fully established.
- Error log results were truncated (truncated:true) and top patterns were computed over matched sets, so the claim that failures are strictly limited to FNO rests on pattern aggregates and returned samples rather than an exhaustive per-entry scan.
- Deployment lookup covered only two windows and did not explicitly check rollback activity after 12:22Z/13:59Z beyond rolled_back_at being null at fetch time.
- Historical incident search (max match_score 2) found no precedent for the exact '500 + POST /v1/orders + derivatives/F&O margin' signature, so precedent neither corroborates nor contradicts the top hypothesis; the index may not cover all past incidents.
- margin-service and order-gateway do not exist in the known_services inventory (queries returned not_found), so a dedicated margin component's health could not be assessed; margin validation appears to live inside order-service.

## What would change this conclusion
- Error rate for order-service (and specifically FNO order 500s) does not recover after rolling back dep-5101 — this would refute H1 and point to H2 (data-side null marginProfile) or H3 (shared dependency degradation).
- Evidence that marginProfile records for the affected accounts are missing/null in the backing store independently of the new code (e.g. a pre-12:10Z data state), or that the new validator's lookup key changed and returns null where legacy code found data — shifts root cause toward H2.
- Dependency telemetry showing ledger-db, redis-cache, kafka, pricing-engine or exchange-gateway erroring/timing out or returning empty account-margin data in the 12:10-12:12Z onset window — would elevate H3 and widen blast radius.
- Discovery of non-FNO order types among the 500s (beyond truncated samples), or of a pre-existing code path failing, would weaken the FNO-scoped deploy-exclusive explanation (H4).
- Evidence of a config change or rollout of order-service (or a hot reload) that the deploy/config queries missed, which would reopen a config-based explanation for onset (F17 currently shows count 0).
- A finding that the legacy OrderRules.validateMargin() also dereferenced marginProfile and the failing path was pre-existing but only now invoked — would complicate the attribution to dep-5101 specifically.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: The order-service v2.14.0 rollout completed at 2026-03-03T12:10:08Z ('all pods r

## Investigation stats
- latency 114.69s, LLM calls 13, tokens 80930 (in 65181 / out 15749)
- tool calls 24 (2 failed, 0 partial, 0 recovered by retry), findings 19/19 verified
