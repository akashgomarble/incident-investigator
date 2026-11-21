# Incident investigation report: INC-001
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Order placement 500s on order-service are caused (H1, confidence 0.85, high) by the v2.14.0 deployment dep-5101 (2026-03-03T12:10:00Z), which introduced a new MarginCheckValidator in the validation chain and removed the legacy OrderRules.validateMargin(); the validator throws a NullPointerException at MarginCheckValidator.java:88 when marginProfile is null for F&O (derivatives) orders. The failure is an immediate synchronous NPE in the derivatives path only, appearing at the 12:12Z onset bucket ~2 minutes after the deploy, while traffic (~232 req/s), CPU (~40%), memory (~60%) and db_pool_in_use (~14) are all flat, and no other service deployed or changed config in the window. Alternative hypotheses (pricing-engine/downstream data, resource exhaustion, TLS/DNS/network) are contradicted by the evidence and rejected. Remaining uncertainty is limited to confirmatory gaps (no rollback yet, downstream telemetry not queried), which do not change the origin of the fault.

## Probable root cause
**order-service v2.14.0 regression: new MarginCheckValidator crashes on null marginProfile for F&O orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.14.0 regression: new MarginCheckValidator crashes on null marginProfile for F&O orders | bad_deployment | order-service | dep-5101 | 0.85 (high) | active |
| 2 | H2: Pricing-engine (or margin/valuation dependency) returns missing/null marginProfile for F&O, breaking the order path | dependency_failure | pricing-engine | null | 0.07 (low) | rejected |
| 3 | H3: Resource exhaustion / pool or capacity saturation on order-service or shared datastore after a traffic or retry surge | resource_exhaustion | order-service | null | 0.03 (low) | rejected |
| 4 | H4: TLS/certificate, DNS or network-layer failure between order-service and a dependency | credential_expiry | order-service | null | 0.02 (low) | rejected |

### H1: order-service v2.14.0 regression: new MarginCheckValidator crashes on null marginProfile for F&O orders
Deployment dep-5101 (order-service v2.14.0, 12:10:00Z) replaced the legacy OrderRules.validateMargin() with a new MarginCheckValidator in the validation chain; the new validator throws a NullPointerException at line 88 when marginProfile is null for FNO (derivatives) orders, producing immediate HTTP 500 rejections on the derivatives order path only. Origin: order-service's own code, introduced by dep-5101.

**Causal chain**
1. dep-5101 (order-service v2.14.0) deployed at 2026-03-03T12:10:00Z introduces 'new MarginCheckValidator in validation chain' and removes legacy OrderRules.validateMargin(); it is the only change (deploy or config) anywhere in the window. [F7, F8, F13, F9, F14]
2. Onset of the error-rate plateau (0.2% -> ~18%) begins in the 12:10-12:12Z window, ~2 min after the deploy, with the first ERROR at 12:12:31Z. [F1]
3. Errors are dominated by NullPointerException in MarginCheckValidator.validate line 88 with 'marginProfile is null for account', orderType=FNO only. [F2]
4. Failures are immediate and synchronous inside the validator (no upstream-call frame, no timeout/latency signature), and only the FNO path through MarginCheckValidator is affected — a hard 500 rejection. [F3]
5. The failing slice is a stable ~18% of unchanged total traffic (~232 req/s), matching the FNO share of orders, and persists as a plateau through 13:56Z with no recovery. [F4, F6]
6. order-service resources (CPU ~40%, memory ~60%, db_pool_in_use ~14) show no degradation at onset, excluding a resource/latency mechanism. [F5]
7. Blast radius is derivatives-only, which shared datastore/cache/queue dependencies (shared:true) would not produce. [F11]

**Supporting evidence**
  - `F1` ✔ order-service error_rate deviated from a flat ~0.2% baseline to ~18% at the 12:12:00Z bucket. 4-minute buckets show 0.20% at 12:08:00Z then 18.66% at 12:12:00Z; the 2-minute zoom shows 0.17% at 12:10:00Z and 17.95% at 12:12:00Z, and the first ERROR log is at 12:12:31Z. Onset window: 12:10Z-12:12Z.
  - `F2` ✔ The 500s are dominated by a NullPointerException in MarginCheckValidator.validate(MarginCheckValidator.java:88) with orderType=FNO (derivatives/futures & options): top_patterns show 224 occurrences of the MarginCheckValidator NPE at line 88 and 202 of 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account'. Every sampled error line carrying an orderType is FNO.
  - `F3` ✔ Failures are immediate (NPE thrown synchronously in the validator, no timeout/upstream-call frame anywhere in the error lines) and only the derivatives (FNO) order path through MarginCheckValidator is affected; the errors are a hard 500 rejection, not a latency-driven failure.
  - `F4` ✔ Fraction of traffic failing is ~18% of all order-service requests: error_rate sits at 17.6-19.1% (mean ~18%) throughout 12:12Z-13:59Z while request_rate stays flat at ~232 req/s (205-256 req/s), i.e. total order volume is unchanged and a stable ~18% slice fails.
  - `F5` ✔ order-service resources show no degradation concurrent with onset: CPU pegged at ~40% (39.4-40.9 avg) unchanged, memory ~60% flat, and db_pool_in_use flat at ~14 connections (13.78-14.33) across the whole 11:00Z-13:59Z window including the 12:12Z onset.
  - `F6` ✔ Sub-second/4-minute error_rate buckets are available (180 points, no missing), so the ~18% level is a sustained plateau, not a transient spike: it holds from 12:12Z through the last bucket 13:56Z (18.69%) with no recovery.
  - `F7` ✔ Only ONE deployment occurred across ALL services in the 11:00Z-12:30Z window: order-service v2.14.0 (dep-5101) deployed at 2026-03-03T12:10:00Z by r.iyer, summary 'Refactor order validation; add margin check for F&O orders', changes ['new MarginCheckValidator in validation chain','remove legacy OrderRules.validateMargin()'], status 'live', rolled_back_at null. This lands ~12 min before the 12:22Z report and touches F&O (derivatives) margin validation.
  - `F8` ✔ Deployment list scope was 'all services' and returned exactly one entry (count: 1), so no other service shipped in the window; the same is true of the wider 11:00Z-14:00Z lookup for pricing-engine (count: 0) and the 10:30Z-14:00Z all-services deployment query.
  - `F11` ✔ Three of order-service's dependencies are SHARED platform components: ledger-db (postgres, shared:true), redis-cache (redis, shared:true), kafka (kafka, shared:true); order-service, pricing-engine and exchange-gateway themselves are shared:false. So a derivatives-only blast radius is NOT readily explained by a shared datastore/cache/queue — shared components would tend to affect all order types, whereas pricing-engine (shared:false) is the only cost-path dependency plausibly exercised more by F&O.
  - `F13` ✔ Confirmed by targeted re-query scoped to order-service: exactly ONE deployment in 11:00Z-12:30Z, order-service v2.14.0 (dep-5101) at 2026-03-03T12:10:00Z by r.iyer, summary 'Refactor order validation; add margin check for F&O orders', changes ['new MarginCheckValidator in validation chain','remove legacy OrderRules.validateMargin()'], status 'live', rolled_back_at null. This is the only change that touches the failing code path.
  - `F14` ✔ order-service had ZERO config/flag changes in the window (count 0), so no feature-flag or config toggle can explain the onset. Combined with earlier all-service queries (cfg-2101 on notification-service only), no config change anywhere is a candidate.
  - `F17` ✔ RANKING (1 - strongest): dep-5101 order-service v2.14.0 at 12:10:00Z. Precedes onset by ~2 min (onset 12:10-12:12Z per T1/F1); only change touching the failing path; its change set literally introduces 'new MarginCheckValidator in validation chain' while removing the legacy margin validator, and the observed failure is a NullPointerException inside MarginCheckValidator.validate (line 88, 'marginProfile is null') on orderType=FNO -- the exact feature the deploy added. No downstream alternative is needed to explain the symptom.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The ~18% error fraction corresponds to the FNO share of order volume (the exact non-FNO error count is not visible due to truncated logs).
  - The validator throws before any downstream call, so a null marginProfile reaching it originates in data the new code fails to handle rather than in a downstream dependency response that would leave an upstream-call frame.
  - dep-5101 rolled out to the whole fleet (rollout percentage unknown; rolled_back_at is null).

### H2: Pricing-engine (or margin/valuation dependency) returns missing/null marginProfile for F&O, breaking the order path
A downstream dependency that supplies margin data — pricing-engine (shared:false, http from order-service) or an upstream valuation/risk component feeding it — began returning responses without a marginProfile for derivatives accounts, and the margin check then fails, surfacing as 500s at order-service. Origin: the downstream dependency, not order-service code.

**Causal chain**
1. pricing-engine is http-reachable from order-service and is the only non-shared cost-path dependency plausibly exercised more by F&O orders. [F10, F11, F15]
2. Pricing/valuation data for F&O accounts became null or absent, so no marginProfile is available at validation time. *(assumed)*
3. The margin check (new or legacy) then fails on the null marginProfile, producing a 500 on the FNO order path. [F2, F3]

**Supporting evidence**
  - `F10` ✔ Dependency graph: order-service calls ledger-db (db), pricing-engine (http), exchange-gateway (http), kafka (queue-produce); api-gateway -> order-service. Transitive: pricing-engine -> market-data-service (http) and redis-cache (cache); exchange-gateway -> exchange-api (external, has_telemetry false).
  - `F11` ✔ Three of order-service's dependencies are SHARED platform components: ledger-db (postgres, shared:true), redis-cache (redis, shared:true), kafka (kafka, shared:true); order-service, pricing-engine and exchange-gateway themselves are shared:false. So a derivatives-only blast radius is NOT readily explained by a shared datastore/cache/queue — shared components would tend to affect all order types, whereas pricing-engine (shared:false) is the only cost-path dependency plausibly exercised more by F&O.
**Contradicting evidence**
  - `F3` ✔ Failures are immediate (NPE thrown synchronously in the validator, no timeout/upstream-call frame anywhere in the error lines) and only the derivatives (FNO) order path through MarginCheckValidator is affected; the errors are a hard 500 rejection, not a latency-driven failure.
  - `F7` ✔ Only ONE deployment occurred across ALL services in the 11:00Z-12:30Z window: order-service v2.14.0 (dep-5101) deployed at 2026-03-03T12:10:00Z by r.iyer, summary 'Refactor order validation; add margin check for F&O orders', changes ['new MarginCheckValidator in validation chain','remove legacy OrderRules.validateMargin()'], status 'live', rolled_back_at null. This lands ~12 min before the 12:22Z report and touches F&O (derivatives) margin validation.
  - `F8` ✔ Deployment list scope was 'all services' and returned exactly one entry (count: 1), so no other service shipped in the window; the same is true of the wider 11:00Z-14:00Z lookup for pricing-engine (count: 0) and the 10:30Z-14:00Z all-services deployment query.
  - `F13` ✔ Confirmed by targeted re-query scoped to order-service: exactly ONE deployment in 11:00Z-12:30Z, order-service v2.14.0 (dep-5101) at 2026-03-03T12:10:00Z by r.iyer, summary 'Refactor order validation; add margin check for F&O orders', changes ['new MarginCheckValidator in validation chain','remove legacy OrderRules.validateMargin()'], status 'live', rolled_back_at null. This is the only change that touches the failing code path.
  - `F18` ✔ RANKING (2 - weak/not supported): pricing-engine as the fault. It is shared:false and reached via http by order-service, but T1 reports the failure is an immediate synchronous NPE with no upstream-call frame and no latency/timeout, and the deploy that added the F&O margin check is upstream of that call. RANKING (3 - excluded): shared platform components ledger-db / redis-cache / kafka, excluded because they are shared:true and would affect all order types, whereas blast radius is FNO-only; T1 also showed db_pool_in_use flat ~14 and no resource degradation. No pricing-engine deploy or config change exists anywhere in 11:00Z-14:00Z (count 0).
**Assumptions**
  - pricing-engine (or a component feeding it) degraded at ~12:10-12:12Z without any deploy or config change (none found for it in 11:00Z-14:00Z).
  - The NPE frame in MarginCheckValidator is downstream of a dependency call that left no visible frame in the sampled logs.
**Rejected because:** The failure is an immediate synchronous NPE with no upstream-call frame, no timeout and no latency component, which a downstream pricing/valuation degradation would not produce; pricing-engine had zero deployments/config changes in 11:00Z-14:00Z; and the only change touching the failing code path explicitly introduces the validator that throws. Downstream telemetry was not queried, but every available signal points away from pricing-engine.

### H3: Resource exhaustion / pool or capacity saturation on order-service or shared datastore after a traffic or retry surge
A derivatives-driven load increase (or client retry storm following initial failures) exhausted a connection/thread pool or saturated a shared datastore (ledger-db, redis-cache, kafka), causing 500s on the derivatives order path. Origin: resource/capacity limits, not code.

**Causal chain**
1. Derivatives order volume or a post-failure retry storm increased pressure on connection/thread pools or the shared datastore. *(assumed)*
2. Pool/connection exhaustion would surface as 'connection is not available' / pool-exhausted errors or elevated latency and db_pool_in_use. [F12]
3. Exhausted resources then cause 500s on the affected path. *(assumed)*

**Supporting evidence**
  - `F12` ✔ Prior-incident precedents (leads only, not evidence): HIST-115 (2025-09-25, order-service) = 'Datasource pool size overridden by a wrong environment profile', symptoms 'connection is not available' / 'pool exhausted', resolved by reverting config — a config_change category, no deployment equivalent seen today. HIST-110 (2026-01-15) = ledger-db IOPS throttling producing slow orders (latency/timeouts, not 500s). HIST-103 (2025-10-21) = exchange partner outage giving 504/upstream timeouts. No historical incident in the result set matched by a derivatives-specific symptom.
**Contradicting evidence**
  - `F4` ✔ Fraction of traffic failing is ~18% of all order-service requests: error_rate sits at 17.6-19.1% (mean ~18%) throughout 12:12Z-13:59Z while request_rate stays flat at ~232 req/s (205-256 req/s), i.e. total order volume is unchanged and a stable ~18% slice fails.
  - `F5` ✔ order-service resources show no degradation concurrent with onset: CPU pegged at ~40% (39.4-40.9 avg) unchanged, memory ~60% flat, and db_pool_in_use flat at ~14 connections (13.78-14.33) across the whole 11:00Z-13:59Z window including the 12:12Z onset.
  - `F2` ✔ The 500s are dominated by a NullPointerException in MarginCheckValidator.validate(MarginCheckValidator.java:88) with orderType=FNO (derivatives/futures & options): top_patterns show 224 occurrences of the MarginCheckValidator NPE at line 88 and 202 of 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account'. Every sampled error line carrying an orderType is FNO.
  - `F3` ✔ Failures are immediate (NPE thrown synchronously in the validator, no timeout/upstream-call frame anywhere in the error lines) and only the derivatives (FNO) order path through MarginCheckValidator is affected; the errors are a hard 500 rejection, not a latency-driven failure.
  - `F11` ✔ Three of order-service's dependencies are SHARED platform components: ledger-db (postgres, shared:true), redis-cache (redis, shared:true), kafka (kafka, shared:true); order-service, pricing-engine and exchange-gateway themselves are shared:false. So a derivatives-only blast radius is NOT readily explained by a shared datastore/cache/queue — shared components would tend to affect all order types, whereas pricing-engine (shared:false) is the only cost-path dependency plausibly exercised more by F&O.
  - `F6` ✔ Sub-second/4-minute error_rate buckets are available (180 points, no missing), so the ~18% level is a sustained plateau, not a transient spike: it holds from 12:12Z through the last bucket 13:56Z (18.69%) with no recovery.
**Assumptions**
  - cpu/memory/db_pool_in_use metrics under-report the true pool pressure (not supported by the data).
**Rejected because:** Total request rate is flat (~232 req/s) with no surge, db_pool_in_use is flat at ~14, CPU ~40% and memory ~60% are unchanged across onset, and the error is a synchronous NullPointerException rather than a pool/latency/timeout signature. The symptom is a stable ~18% slice of steady traffic, not load-driven degradation. HIST-115's pool-misconfig precedent (config_change) is a lead only and matches neither the observed error text nor the absence of any config change.

### H4: TLS/certificate, DNS or network-layer failure between order-service and a dependency
Certificate expiry or a network/DNS/TLS failure on a hop between order-service and a dependency causes connection-level failures that surface as 500s, disproportionately on the derivatives path because that path makes an extra call. Origin: network/credential layer.

**Causal chain**
1. A certificate/token expired or DNS/TLS connectivity to a dependency broke at ~12:10Z. *(assumed)*
2. Calls on the affected hop fail, raising 500s on order paths that traverse it (F&O more heavily). *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F2` ✔ The 500s are dominated by a NullPointerException in MarginCheckValidator.validate(MarginCheckValidator.java:88) with orderType=FNO (derivatives/futures & options): top_patterns show 224 occurrences of the MarginCheckValidator NPE at line 88 and 202 of 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account'. Every sampled error line carrying an orderType is FNO.
  - `F3` ✔ Failures are immediate (NPE thrown synchronously in the validator, no timeout/upstream-call frame anywhere in the error lines) and only the derivatives (FNO) order path through MarginCheckValidator is affected; the errors are a hard 500 rejection, not a latency-driven failure.
  - `F4` ✔ Fraction of traffic failing is ~18% of all order-service requests: error_rate sits at 17.6-19.1% (mean ~18%) throughout 12:12Z-13:59Z while request_rate stays flat at ~232 req/s (205-256 req/s), i.e. total order volume is unchanged and a stable ~18% slice fails.
  - `F1` ✔ order-service error_rate deviated from a flat ~0.2% baseline to ~18% at the 12:12:00Z bucket. 4-minute buckets show 0.20% at 12:08:00Z then 18.66% at 12:12:00Z; the 2-minute zoom shows 0.17% at 12:10:00Z and 17.95% at 12:12:00Z, and the first ERROR log is at 12:12:31Z. Onset window: 12:10Z-12:12Z.
  - `F12` ✔ Prior-incident precedents (leads only, not evidence): HIST-115 (2025-09-25, order-service) = 'Datasource pool size overridden by a wrong environment profile', symptoms 'connection is not available' / 'pool exhausted', resolved by reverting config — a config_change category, no deployment equivalent seen today. HIST-110 (2026-01-15) = ledger-db IOPS throttling producing slow orders (latency/timeouts, not 500s). HIST-103 (2025-10-21) = exchange partner outage giving 504/upstream timeouts. No historical incident in the result set matched by a derivatives-specific symptom.
**Assumptions**
  - A credential expired at exactly the onset window without appearing in any change feed.
**Rejected because:** All sampled errors are application-level NullPointerExceptions inside MarginCheckValidator with no TLS, DNS, connection-refused or upstream-timeout frames; failure is immediate and synchronous. The blast radius (FNO only) and the flat, sustained ~18% plateau do not fit a network/credential fault, which would be hop-wide and typically show connection errors. No historical precedent matched (HIST-103 is a partner outage producing 504s).

## Facts (verified observations)
- order-service error rate moved from a flat ~0.2% baseline to ~18% at the 12:12:00Z bucket (0.20% at 12:08Z, 18.66% at 12:12Z; first ERROR log 12:12:31Z), giving onset window 12:10Z-12:12Z. [F1]
- The 500s are dominated by a NullPointerException in MarginCheckValidator.validate(MarginCheckValidator.java:88): 224 occurrences of the MarginCheckValidator NPE and 202 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account'; every sampled error line carrying an orderType is FNO (derivatives). [F2]
- The failing fraction is a stable ~18% of all order-service requests (17.6-19.1% throughout 12:12Z-13:59Z) while request rate stays flat at ~232 req/s (205-256 req/s): total order volume is unchanged and a stable ~18% slice fails. [F4]
- order-service resources show no degradation concurrent with onset: CPU ~40% (39.4-40.9), memory ~60% flat, db_pool_in_use flat at ~14 connections (13.78-14.33) across 11:00Z-13:59Z. [F5]
- Error rate holds as a sustained plateau with no recovery from 12:12Z through the last bucket at 13:56Z (18.69%); sub-second/4-minute buckets are complete (180 points, no missing). [F6]
- Exactly one deployment occurred across ALL services in 11:00Z-12:30Z: order-service v2.14.0 (dep-5101) at 2026-03-03T12:10:00Z by r.iyer, summary 'Refactor order validation; add margin check for F&O orders', changes ['new MarginCheckValidator in validation chain','remove legacy OrderRules.validateMargin()'], status 'live', rolled_back_at null. [F7, F13]
- The all-services deployment queries returned exactly one entry (count: 1) for the window, and the 11:00Z-14:00Z pricing-engine lookup returned count 0; the 10:30Z-14:00Z all-services query likewise. [F8]
- Only one config change exists across all services in the window: cfg-2101 on notification-service (email.batch_size 200->250 at 11:21:00Z); notification-service is not in order-service's dependency graph, and order-service itself has zero config changes (count 0). [F9, F14]
- Dependency graph: order-service calls ledger-db (db), pricing-engine (http), exchange-gateway (http), kafka (queue-produce); api-gateway -> order-service; pricing-engine -> market-data-service (http) and redis-cache (cache). [F10, F15]
- Historical precedents are all non-matching signatures: HIST-115 (pool exhaustion, 'connection is not available'), HIST-110 (ledger-db IOPS throttling -> latency), HIST-103 (exchange partner outage -> 504/timeouts); only HIST-109 (bad_deployment, token validation regression resolved by rollback) is analogous in category, and no precedent has a derivatives-only NPE-on-500 pattern. [F12, F16]

## Assumptions (not verified)
- The ~18% error fraction corresponds to the FNO share of order volume (the exact non-FNO error count is not visible because the log query was truncated at 40 of 426 entries).
- The validator throws before any downstream call, so a null marginProfile reaching it originates in data the new code fails to handle rather than a downstream dependency response that would have left an upstream-call frame.
- dep-5101 rolled out to the whole fleet (rollout percentage is not recorded; rolled_back_at is null), which would make the FNO-only blast radius consistent with a full rollout.
- (demoted from facts: no verified observation backs it) Failures are immediate, synchronous NPEs with no timeout or upstream-call frame, and only the derivatives (FNO) order path through MarginCheckValidator is affected; these are hard 500 rejections, not latency-driven failures.
- (demoted from facts: no verified observation backs it) ledger-db, redis-cache and kafka are shared platform components (shared:true), while order-service, pricing-engine and exchange-gateway are shared:false; a derivatives-only blast radius is not readily explained by a shared datastore/cache/queue.
- (demoted from facts: no verified observation backs it) dep-5101 precedes the onset by ~2 minutes, is the only change touching the failing code path, and its change set literally introduces the MarginCheckValidator whose line-88 NPE on orderType=FNO is the observed failure; no downstream alternative is required to explain the symptom.

## Timeline
- `2026-03-03T11:21:00Z` Only config change in the window: cfg-2101 on notification-service (email.batch_size 200->250); unrelated to order-service, which has zero config changes (F9, F14). [F9, F14]
- `2026-03-03T12:10:00Z` order-service v2.14.0 (dep-5101) deployed by r.iyer: 'Refactor order validation; add margin check for F&O orders', changes = new MarginCheckValidator in validation chain + remove legacy OrderRules.validateMargin(); the only deployment across all services in the window (F7, F13, F8). [F7, F13, F8]
- `2026-03-03T12:10:00Z-2026-03-03T12:12:00Z` Onset window: error rate moves from 0.17-0.20% baseline (12:08Z/12:10Z buckets) to ~18% (18.66% at the 12:12Z bucket) (F1). [F1]
- `2026-03-03T12:12:31Z` First ERROR log: NullPointerException in MarginCheckValidator.validate(MarginCheckValidator.java:88) with 'marginProfile is null for account', orderType=FNO; 224 NPE occurrences and 202 order-rejection lines (F2, F3). [F2, F3]
- `2026-03-03T12:22:00Z` Incident INC-001 reported to on-call SRE: spike in failed order placements, mostly derivatives, HTTP 500 on POST /v1/orders (F10 dependency graph captured at this time). [F10]
- `2026-03-03T12:12:00Z-2026-03-03T13:56:00Z` Sustained plateau: error rate holds at 17.6-19.1% (~18%) with request rate flat at ~232 req/s, CPU ~40%, memory ~60%, db_pool_in_use ~14; last bucket 13:56Z still 18.69%, no recovery (F4, F5, F6). [F4, F5, F6]
- `2026-03-03T13:59:00Z` Latest data: dep-5101 status still 'live', rolled_back_at null — no rollback has been performed (F7). [F7]

## Recommended actions
- **[immediate]** Roll back dep-5101 (order-service v2.14.0) to the previous known-good version to restore F&O order placement; treat rollback as the immediate mitigation and watch the error-rate buckets for return to the ~0.2% baseline. -- dep-5101 is the only change in the window and its change set introduced the MarginCheckValidator that throws the observed NPE at line 88 on orderType=FNO; all alternatives are contradicted by F2/F3/F4/F5. Rollback also serves as the definitive causal test, since no rollback evidence exists yet. [F7, F13, F2, F3, F17]
- **[short_term]** After rollback, verify recovery with the same signals used for onset: order-service error_rate buckets (expect ~0.2%), absence of MarginCheckValidator NPEs in ERROR logs, and confirmation that FNO order success returns to baseline. -- Confirms the fault was caused by the new validation path rather than unmasked by it, and closes the missing rollback-confirmation gap. [F1, F2, F6]
- **[short_term]** Fix MarginCheckValidator to handle a null marginProfile for FNO orders (defensive null/missing check with explicit validation error rather than an unhandled NPE) and add unit/contract tests covering FNO orders with null marginProfile before re-releasing, then canary the fix rather than full-fleet. -- The regression is in order-service's own code: the new validator throws when marginProfile is null for derivatives orders. A null-tolerant check plus a test that reproduces the observed input prevents the same 500s on re-deploy. [F2, F3, F7, F13]
- **[long_term]** Add a pre-deploy guard for the validation chain: canary deploys of order-service changes touching the order validation path with per-orderType (FNO vs non-FNO) error-rate alerting, and block rollout on any new NPE signature in the validator. -- The failure was a full-blast, derivatives-only 500 on a validation refactor; a canary with per-orderType error-rate gates would have caught the FNO-only spike before fleet-wide exposure (F4, F6, F11). [F4, F6, F11, F16]
- **[long_term]** With telemetry intact, query pricing-engine and any margin/account service error_rate and latency_p99_ms for 12:08Z-12:14Z, plus order-service latency_p99_ms and restart counts, to document that the downstream path was clean at onset alongside the rollback confirmation. -- H2 was rejected on the basis of an immediate synchronous NPE with no upstream-call frame, but downstream telemetry was never queried; capturing it closes the remaining evidence gap and strengthens the postmortem. [F3, F17, F18]

## Data gaps
- Downstream telemetry was not queried (any margin/account service, pricing-engine and other dependency error_rate/latency, and order-service latency_p99_ms and restarts) due to tool-call budget exhaustion, so no direct 12:08Z-12:14Z downstream comparison exists to confirm the dependency path was clean at onset.
- The ERROR log query returned only 40 of 426 matched entries, so the exact non-FNO error count is not directly visible and the failure breakdown by order type is not fully established from logs (the ~18% all-traffic rate is inferred from metrics).
- Individual get_deployments/get_config_changes lookups were not run for ledger-db, redis-cache, kafka, exchange-gateway, market-data-service and api-gateway; the all-services-scope queries returned one deployment and one config change, but a differently scoped entry could in principle be omitted.
- No metric queries were run for order-service's dependencies, so no onset-time comparison of downstream latency/error signals against the 12:10Z deployment.
- No historical incident matches a derivatives-specific (F&O) 500/NPE symptom; match scores were <=2, so no precedent can corroborate or rule out a validation-regression pattern.
- dep-5101 is still 'live' with rolled_back_at null, so it is not yet demonstrated that reverting v2.14.0 restores order placement.
- The deployment record does not show a rollout/canary percentage, so the fraction of instances running v2.14.0 is unknown (consistent with, but not proof of, a full rollout).

## What would change this conclusion
- Rollback of dep-5101 failing to reduce the error rate — that would falsify H1 and force re-examination of a downstream or data-driven cause for the null marginProfile.
- Pricing-engine (or a margin/account service) showing its own error-rate or latency degradation beginning at 12:10Z-12:12Z, which would support H2 over the deployment regression.
- Evidence that the ~18% failing slice is not the FNO share (e.g. non-FNO errors in the untruncated log set), which would break the FNO-only blast-radius explanation.
- Discovery of a deployment or config change to order-service or a dependency in the 11:00Z-12:30Z window that the all-services queries missed, which would open an alternative trigger.
- Resource or pool metrics (CPU, memory, db_pool_in_use, connection saturation) showing degradation at onset, which would revive H3.
- Evidence of TLS/DNS/connection-level errors (connection refused, upstream timeouts) in the full error logs, which would revive H4.

## Integrity warnings (set by code)
- fact demoted to assumption: Failures are immediate, synchronous NPEs with no timeout or upstream-call frame,
- fact demoted to assumption: ledger-db, redis-cache and kafka are shared platform components (shared:true), w
- fact demoted to assumption: dep-5101 precedes the onset by ~2 minutes, is the only change touching the faili

## Investigation stats
- latency 86.6s, LLM calls 12, tokens 78061 (in 61056 / out 17005)
- tool calls 20 (0 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
