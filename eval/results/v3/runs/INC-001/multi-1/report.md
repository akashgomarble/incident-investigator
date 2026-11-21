# Incident investigation report: INC-001
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Order placement 500s are caused by dep-5101, order-service v2.14.0 (deployed 2026-03-03T12:10:00Z), which added a new MarginCheckValidator that throws NullPointerException ("marginProfile is null") for futures & options orders, returning HTTP 500 on POST /v1/orders while equity orders are unaffected. The deploy is the only order-service change in the window and the latest change fleet-wide, landing ~2.5 minutes before the first matching error log at 12:12:31Z and the first elevated error bucket at 12:12. Confidence is high (0.88, capped by the Critic). The strongest rival explanation — a shared-platform fault (ledger-db/kafka/redis) — is positively discriminated: all three other ledger-db consumers (portfolio-, payment-, settlement-service) stayed flat at ~0.21% error rate through the onset buckets, and order-service latency/pool metrics were flat, indicating a fail-fast code defect rather than saturation. Residual uncertainty concerns secondary mechanism detail (in-process vs. outbound null source) and the absence-of-evidence basis for non-FNO unaffectedness, not which origin to fix.

## Probable root cause
**dep-5101 (order-service v2.14.0) added MarginCheckValidator which NPEs on F&O orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.88, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: dep-5101 (order-service v2.14.0) added MarginCheckValidator which NPEs on F&O orders | bad_deployment | order-service | dep-5101 | 0.88 (high) | active |
| 2 | H2: Shared platform dependency (ledger-db / kafka / redis-cache) faults around 12:10-12:22Z and surfaces as order-service 500s | infrastructure_failure | ledger-db | null | 0.03 (low) | rejected |
| 3 | H3: Off-path config change / misconfiguration (cfg-2101 or an order-service profile override) caused the failures | config_change | notification-service | cfg-2101 | 0.02 (low) | rejected |
| 4 | H4: Overload / resource exhaustion in order-service or a shared component caused intermittent null-profile handling and 500s | resource_exhaustion | order-service | null | 0.02 (low) | rejected |

### H1: dep-5101 (order-service v2.14.0) added MarginCheckValidator which NPEs on F&O orders
The only fleet-wide deployment, order-service v2.14.0 (dep-5101, 12:10:00Z), replaced legacy OrderRules.validateMargin() with a new MarginCheckValidator. The new validator dereferences a margin profile that is null for futures & options accounts (the legacy path tolerated or derived it), so every F&O order reaching validation throws NullPointerException at MarginCheckValidator.java:88 and POST /v1/orders returns 500 for derivatives while equity orders stay healthy.

**Causal chain**
1. dep-5101 deploys order-service v2.14.0 at 12:10:00Z: 'Refactor order validation; add margin check for F&O orders' — adds new MarginCheckValidator, removes legacy OrderRules.validateMargin() [F1, F14, F2, F3]
2. The new validator executes for F&O (orderType=FNO) orders and dereferences a marginProfile that is null for those accounts, throwing NPE at MarginCheckValidator.java:88 [F9, F10, F14]
3. The unhandled NPE is mapped to HTTP 500 by the order API ('order rejected with 500: marginProfile is null for account') [F10, F9]
4. Because the new code only governs the derivatives/F&O validation branch, failures are scoped to orderType=FNO; all 224 logged errors are FNO and no non-FNO order type appears [F11, F29, F14]
5. Onset ordering: deploy 12:10:00Z -> first NPE log 12:12:31Z -> error rate jumps in the 12:12 bucket; healthy (0.13-0.23%) in the 12:08-12:11 buckets [F8, F9, F15, F26]
6. Failure is fail-fast, not a latency/dependency/timeout pattern: latency_p99 flat ~150ms and db_pool_in_use flat ~14 across the window [F12, F24]
7. Elevated rate is sustained (17.97% -> 18.85% at 13:58) and request volume did not change, so it is a rate change not a traffic change [F27, F28]
8. Blast radius attenuates correctly upstream: api-gateway error_rate rises only to ~3.3-3.5%, consistent with just the order route failing [F23, F31]

**Supporting evidence**
  - `F1` ✔ The only deployment to order-service in the 24h (and broader 2026-02-25→03-03) window is dep-5101, v2.14.0, deployed 2026-03-03T12:10:00Z by r.iyer, summary 'Refactor order validation; add margin check for F&O orders', changes ['new MarginCheckValidator in validation chain','remove legacy OrderRules.validateMargin()'], status 'live', rolled_back_at null.
  - `F2` ✔ The order-service deploy at 12:10:00Z precedes the 12:22:00Z incident report by ~12 minutes; it is the latest change in the whole fleet and no deployment or config change is shown as rolled back.
  - `F3` ✔ Across ALL services the 24h deployment inventory contains only 2 deployments: dep-5088 portfolio-service v4.2.1 at 2026-03-03T09:36:00Z (summary 'Dependency bumps', change 'bump grpc 1.62 -> 1.63', status live) and dep-5101 order-service v2.14.0.
  - `F8` ✔ order-service error_rate baseline was ~0.2-0.3% through 12:06, then jumped to avg 9.06% (max 17.97) in the 12:10 bucket and stayed elevated at 18-19% (avg 18.85, max 19.38) in the final 13:58 bucket. The rise begins in the 12:10-12:14 window.
  - `F9` ✔ First order-service ERROR log of the incident is at 2026-03-03T12:12:31Z; 426 ERROR entries matched from 12:12:31Z to 13:58:50Z, all of two patterns involving MarginCheckValidator NPE and 'marginProfile is null'.
  - `F10` ✔ Dominant error signature is a NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) on orderType=FNO (224 occurrences) and the paired 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account' (202 occurrences). The 500s are attributable to a null marginProfile during margin validation.
  - `F11` ✔ Every logged failing order in the window carries orderType=FNO (futures & options / derivatives); no non-derivatives (e.g. equity) order type appears in the returned error entries, consistent with the support report that failures are mostly derivatives orders.
  - `F12` ✔ order-service showed no resource saturation or latency change accompanying the error spike: latency_p99_ms stayed flat at ~147-153ms across the whole window (mean 150.01ms), and db_pool_in_use stayed flat at ~13.9-14.4 connections (mean 14.04).
  - `F14` ✔ The only order-service deployment in the whole 2026-02-24→03-03 window is dep-5101 v2.14.0 at 2026-03-03T12:10:00Z by r.iyer, summary 'Refactor order validation; add margin check for F&O orders', changes ['new MarginCheckValidator in validation chain','remove legacy OrderRules.validateMargin()'], status 'live', rolled_back_at null. This is the only change whose content matches the derivatives (F&O) scoping and the MarginCheckValidator NPE signature.
  - `F15` ✔ The deploy at 12:10:00Z precedes the first incident error log at 12:12:31Z (T1/F9) and the first elevated error bucket at 12:10-12:14Z (T1/F8), i.e. the only deployment in the fleet lands ~2.5 minutes before the first NPE and ~12 minutes before the 12:22 report; nothing was rolled back.
  - `F16` ✔ The ONLY config change across all services in the 11:00-13:59 window is cfg-2101 on notification-service (email.batch_size 200→250 at 11:21Z by config-bot) — notification-service is off the order path, so this config change cannot explain order-service 500s and is ruled out on blast radius.
  - `F17` ✔ order-service had NO config changes in the 2026-02-24→03-03 window (count 0), ruling out a config/profile override (e.g. the historical pool-misconfiguration pattern HIST-115) as the explanation for this incident.
  - `F19` ✔ Historical precedents for order-500s are leads only and none matches this incident's signature: HIST-103 (exchange partner outage, 504/upstream timeouts), HIST-110 (ledger-db IOPS throttling, latency/statement timeouts), HIST-115 (order-service conn-pool config_change, 'pool exhausted'), HIST-109 (token validation regression after deployment, auth-service 401s). The closest categorical precedent is HIST-109 — a regression introduced by a code deployment rather than a dependency outage.
  - `F24` ✔ order-service latency_p99_ms stayed flat across the whole 11:30-13:59Z window (mean 150.01ms, 12:12 bucket avg 149.5ms, 12:19 153.06ms, max 164.98ms), confirming the order-service failure mode is an immediate error (fail-fast on validation), not a latency/dependency-timeout pattern.
  - `F26` ✔ At 60-second bucket resolution, order-service error_rate was flat and healthy through the deploy minute itself: 12:08 avg 0.22, 12:09 avg 0.23, 12:10 avg 0.13, 12:11 avg 0.20 — then the 12:12:00Z bucket jumps to avg 17.97 (max 17.97), i.e. the onset is the minute immediately AFTER the 12:10:00Z dep-5101 deploy, not during it.
  - `F27` ✔ The elevated error rate is sustained from onset, not a transient blip: 12:12 avg 17.97 -> 12:13 avg 17.93 -> 12:14 avg 19.63, and in the 120s series it remains avg 18.4 at 12:20 and avg 18.85 (max 19.38) at 13:58.
  - `F28` ✔ Request volume did not change at onset, so the 5xx jump is a rate change not a traffic change: request_rate stays ~224-236 req/s across 12:00-12:20 (12:10 avg 230.01, 12:12 avg 232.61, 12:14 avg 235.57).
  - `F29` ✔ Order-type scoping: a full-range scan of order-service ERROR logs for 'orderType=' returned total_matched 224, all ERROR, all of the single pattern 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:<n>) orderType=FNO orderId=<n>' — zero occurrences of any non-FNO orderType in the error stream.
  - `F31` ✔ Blast radius reaches the caller but attenuated: api-gateway error_rate is 0.19-0.20% in the 12:00 and 12:06 buckets, then 3.37% at 12:12 and stays ~3.3-3.5% through 13:48 — consistent with a subset of gateway traffic (the order route) failing rather than the gateway itself. Sparse 360s buckets here (available_metrics for api-gateway has no db_pool_in_use), so gateway onset is bounded to 12:06-12:12, not sub-minute.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The null marginProfile originates in-process from the new validator's own handling (missing/absent account margin snapshot for FNO) rather than from an outbound lookup that started returning null; no trace/span data resolved the failing span.
  - No code-level diff/version history is available beyond the deploy summary; the exact line 88 semantics are inferred from the matched log pattern.
  - No direct telemetry was obtained for ledger-db/kafka/redis/pricing-engine/exchange-gateway; their health at 12:10Z is accepted based on the absence of any recorded change plus the unaffected co-consumers (F20-F22).

### H2: Shared platform dependency (ledger-db / kafka / redis-cache) faults around 12:10-12:22Z and surfaces as order-service 500s
A fault in a shared component on the order path (ledger-db postgres, kafka, or redis-cache reachable via pricing-engine) degrades or returns errors/nulls to order-service, which surfaces this as marginProfile-is-null NPEs and HTTP 500s. If true, the same failure should appear in the other consumers of the shared component.

**Causal chain**
1. order-service writes directly to ledger-db, reaches redis-cache via pricing-engine, and produces to kafka; all three are shared platform components [F5, F18]
2. ledger-db is shared by order-service, portfolio-service, payment-service and settlement-service, so a ledger-db fault would surface as 500s across all four consumers [F6]
3. Blast-radius test FAILS: all three other ledger-db consumers are flat through 11:30-13:59Z (portfolio-service mean 0.21%, payment-service mean 0.21%, settlement-service mean 0.21%, with the 12:12/12:19 onset buckets at 0.20-0.26%) — no simultaneous degradation [F20, F21, F22]
4. Fault must originate in order-service, not the shared datastore [F20, F21, F22]

**Supporting evidence**
  - `F5` ✔ order-service dependency graph: order-service → ledger-db (kind db), → pricing-engine (http), → exchange-gateway (http), → kafka (queue-produce); pricing-engine → market-data-service (http) and → redis-cache (cache); exchange-gateway → exchange-api (external); api-gateway → order-service (http). Shared components (shared:true) are ledger-db (postgres, platform), redis-cache (redis, platform), kafka (kafka, platform).
  - `F6` ✔ ledger-db is shared by four services: order-service, portfolio-service, payment-service and settlement-service all write to it (kind db), so a ledger-db fault could surface as order-service 500s while every one of its four consumers is affected.
  - `F18` ✔ Shared dependency components reachable from order-service are ledger-db (postgres, shared:true), redis-cache (redis, shared:true) and kafka (kafka, shared:true); order-service writes to ledger-db directly and reaches redis only via pricing-engine, and produce kafka. Any of these is a theoretical alternative fault path for order 500s, but none has a change recorded in the change inventory.
**Contradicting evidence**
  - `F12` ✔ order-service showed no resource saturation or latency change accompanying the error spike: latency_p99_ms stayed flat at ~147-153ms across the whole window (mean 150.01ms), and db_pool_in_use stayed flat at ~13.9-14.4 connections (mean 14.04).
  - `F18` ✔ Shared dependency components reachable from order-service are ledger-db (postgres, shared:true), redis-cache (redis, shared:true) and kafka (kafka, shared:true); order-service writes to ledger-db directly and reaches redis only via pricing-engine, and produce kafka. Any of these is a theoretical alternative fault path for order 500s, but none has a change recorded in the change inventory.
  - `F19` ✔ Historical precedents for order-500s are leads only and none matches this incident's signature: HIST-103 (exchange partner outage, 504/upstream timeouts), HIST-110 (ledger-db IOPS throttling, latency/statement timeouts), HIST-115 (order-service conn-pool config_change, 'pool exhausted'), HIST-109 (token validation regression after deployment, auth-service 401s). The closest categorical precedent is HIST-109 — a regression introduced by a code deployment rather than a dependency outage.
  - `F20` ✔ portfolio-service (a ledger-db consumer) shows no error-rate degradation over 11:30-13:59Z: flat ~0.19-0.26% average on every 7-minute bucket, mean 0.21%, min 0.1% max 0.32%, and the 12:12/12:19 buckets (coincident with order-service onset) are 0.22 and 0.21 avg.
  - `F21` ✔ payment-service (a ledger-db consumer) shows no error-rate degradation over 11:30-13:59Z: every bucket 0.17-0.26% average, mean 0.21%, with 12:12 and 12:19 buckets at 0.2 and 0.26 avg; "missing_points":0, total_points 150.
  - `F22` ✔ settlement-service (a ledger-db consumer) shows no error-rate degradation over 11:30-13:59Z: buckets stay 0.18-0.27% average, mean 0.21%, 12:12/12:19 buckets 0.2 and 0.21 avg; the last bucket even drops to 0.14 avg / 0.15 max.
**Rejected because:** Ruled out by blast radius, the exact discriminator the critique identified: portfolio-service, payment-service and settlement-service — the three other ledger-db consumers — show flat ~0.21% error rates across the whole 11:30-13:59Z window including the 12:12/12:19 onset buckets (F20, F21, F22). A ledger-db origin would have failed all four consumers simultaneously. Additionally, the observed failure is a deterministic code NPE on a derivatives-only branch introduced by dep-5101 (F10, F29), not a datastore/cache null-return pattern; no shared component has any recorded change (F18); order-service latency/pool metrics are flat (F12); and no precedent matches (F19).

### H3: Off-path config change / misconfiguration (cfg-2101 or an order-service profile override) caused the failures
A configuration change rather than code introduced the failure — e.g. cfg-2101 raising notification-service email.batch_size, or an order-service config/profile override in the historical HIST-115 connection-pool pattern. This is only viable if a config on the order path had changed.

**Causal chain**
1. cfg-2101 changed notification-service email.batch_size 200→250 at 11:21Z — the only config change in the fleet in the window [F4, F16]
2. notification-service is not on the order path, so its config change cannot produce order-service 500s (blast radius excludes it) [F16, F5]
3. order-service had zero config changes in the 24h window, ruling out a profile/flag override (HIST-115 pattern) [F17]

**Supporting evidence**
  - `F4` ✔ Across ALL services the only config change in the window is cfg-2101, notification-service key email.batch_size 200→250 at 2026-03-03T11:21:00Z by config-bot — not on the order path.
**Contradicting evidence**
  - `F16` ✔ The ONLY config change across all services in the 11:00-13:59 window is cfg-2101 on notification-service (email.batch_size 200→250 at 11:21Z by config-bot) — notification-service is off the order path, so this config change cannot explain order-service 500s and is ruled out on blast radius.
  - `F17` ✔ order-service had NO config changes in the 2026-02-24→03-03 window (count 0), ruling out a config/profile override (e.g. the historical pool-misconfiguration pattern HIST-115) as the explanation for this incident.
  - `F9` ✔ First order-service ERROR log of the incident is at 2026-03-03T12:12:31Z; 426 ERROR entries matched from 12:12:31Z to 13:58:50Z, all of two patterns involving MarginCheckValidator NPE and 'marginProfile is null'.
  - `F10` ✔ Dominant error signature is a NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) on orderType=FNO (224 occurrences) and the paired 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account' (202 occurrences). The 500s are attributable to a null marginProfile during margin validation.
**Rejected because:** cfg-2101 is on notification-service, which is off the order path, so it cannot produce order-service 500s (F16); order-service itself had zero config changes in the 24h window (F17), removing the profile/flag-override HIST-115 pattern; and the error signature is a code-level NPE in a validator introduced by a deployment (F10), not a pool-exhaustion signature.

### H4: Overload / resource exhaustion in order-service or a shared component caused intermittent null-profile handling and 500s
The error spike is a symptom of saturation (pool exhausted, throttled IOPS, cold-cache storm) rather than a code defect: under pressure a margin/portfolio lookup times out or returns incomplete state, so order-service raises null marginProfile errors. Historical precedents HIST-110 (ledger-db IOPS throttling) and HIST-107 (cold cache storm) are the anchors.

**Causal chain**
1. Historical precedents exist for order failures driven by ledger-db IOPS throttling and by redis cold-cache storms after failover [F7, F19]
2. Under such pressure a margin/profile lookup would return null or time out, producing the NPE/500 — speculative; no such error signature in those precedents, and latency/pool metrics are flat *(assumed)*
3. order-service latency_p99 stayed flat ~147-153ms and db_pool_in_use flat ~14 connections across the window [F12, F24]

**Supporting evidence**
  - `F7` ✔ Historical precedents for order-500 / order-failure symptoms (leads only, not evidence for this incident): HIST-103 'Order failures during exchange partner outage' (exchange-gateway, 504/upstream timeouts, external exchange API outage); HIST-110 'Slow orders from ledger-db IOPS throttling' (order latency, statement timeouts, IOPS throttled); HIST-115 'order-service connection pool misconfiguration' (pool exhausted, wrong env profile, config_change); HIST-107 'Cold cache storm after redis failover' (redis-cache/market-data-service).
**Contradicting evidence**
  - `F12` ✔ order-service showed no resource saturation or latency change accompanying the error spike: latency_p99_ms stayed flat at ~147-153ms across the whole window (mean 150.01ms), and db_pool_in_use stayed flat at ~13.9-14.4 connections (mean 14.04).
  - `F10` ✔ Dominant error signature is a NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) on orderType=FNO (224 occurrences) and the paired 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account' (202 occurrences). The 500s are attributable to a null marginProfile during margin validation.
  - `F19` ✔ Historical precedents for order-500s are leads only and none matches this incident's signature: HIST-103 (exchange partner outage, 504/upstream timeouts), HIST-110 (ledger-db IOPS throttling, latency/statement timeouts), HIST-115 (order-service conn-pool config_change, 'pool exhausted'), HIST-109 (token validation regression after deployment, auth-service 401s). The closest categorical precedent is HIST-109 — a regression introduced by a code deployment rather than a dependency outage.
  - `F24` ✔ order-service latency_p99_ms stayed flat across the whole 11:30-13:59Z window (mean 150.01ms, 12:12 bucket avg 149.5ms, 12:19 153.06ms, max 164.98ms), confirming the order-service failure mode is an immediate error (fail-fast on validation), not a latency/dependency-timeout pattern.
  - `F20` ✔ portfolio-service (a ledger-db consumer) shows no error-rate degradation over 11:30-13:59Z: flat ~0.19-0.26% average on every 7-minute bucket, mean 0.21%, min 0.1% max 0.32%, and the 12:12/12:19 buckets (coincident with order-service onset) are 0.22 and 0.21 avg.
  - `F21` ✔ payment-service (a ledger-db consumer) shows no error-rate degradation over 11:30-13:59Z: every bucket 0.17-0.26% average, mean 0.21%, with 12:12 and 12:19 buckets at 0.2 and 0.26 avg; "missing_points":0, total_points 150.
  - `F22` ✔ settlement-service (a ledger-db consumer) shows no error-rate degradation over 11:30-13:59Z: buckets stay 0.18-0.27% average, mean 0.21%, 12:12/12:19 buckets 0.2 and 0.21 avg; the last bucket even drops to 0.14 avg / 0.15 max.
**Assumptions**
  - Saturation could exist in a component whose error metrics were never queried, and could manifest as null rather than timeout errors.
**Rejected because:** order-service latency_p99 stayed flat at ~147-153ms and db_pool_in_use flat at ~14 connections across the window (F12, F24), i.e. no saturation signal; the failure mode is fail-fast (latency flat while errors spike), the opposite of saturation; the dominant signature is a deterministic code NPE in a validator introduced by dep-5101 (F9, F10); no shared component's consumers show degradation (F20-F22); and the cited precedents are leads only, none matching this signature (F19).

## Facts (verified observations)
- dep-5101 (order-service v2.14.0) was deployed at 2026-03-03T12:10:00Z by r.iyer with summary 'Refactor order validation; add margin check for F&O orders' and changes ['new MarginCheckValidator in validation chain','remove legacy OrderRules.validateMargin()'], status 'live', rolled_back_at null. [F1, F14]
- dep-5101 is the only order-service deployment in the 24h (and 2026-02-24→03-03) window and the latest change in the whole fleet; the only other fleet deployment was dep-5088 portfolio-service v4.2.1 (grpc bump) at 09:36:00Z. [F1, F3, F14]
- order-service error rate was ~0.2-0.3% through 12:06, then jumped to avg 9.06% (max 17.97) in the 12:10 bucket and stayed elevated at 18-19% (avg 18.85, max 19.38) in the 13:58 bucket. [F8]
- At 60-second resolution, order-service error rate was healthy through the deploy minute (12:08 avg 0.22, 12:09 avg 0.23, 12:10 avg 0.13, 12:11 avg 0.20) and jumped in the 12:12:00Z bucket to avg 17.97, i.e. onset is the minute immediately after the 12:10:00Z deploy. [F26]
- The first order-service ERROR log of the incident is at 2026-03-03T12:12:31Z (426 ERROR entries through 13:58:50Z); the deploy precedes the first NPE log by ~2.5 minutes and the 12:22 report by ~12 minutes. [F9]
- The dominant error signature is a NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) on orderType=FNO (224 occurrences) paired with 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account' (202 occurrences). [F10, F29]
- Every logged failing order in the window carries orderType=FNO; a full-range scan of order-service ERROR logs for 'orderType=' returned 224 matches, all of the single FNO MarginCheckValidator NPE pattern, with zero non-FNO order types. [F11, F29]
- The elevated error rate is sustained, not a transient blip: 12:12 avg 17.97 → 12:13 avg 17.93 → 12:14 avg 19.63, remaining avg 18.4 at 12:20 and avg 18.85 (max 19.38) at 13:58. [F27]
- Request volume did not change at onset (request_rate ~224-236 req/s across 12:00-12:20), so the 5xx jump is a rate change, not a traffic change. [F28]
- order-service showed no resource saturation or latency change accompanying the error spike: latency_p99_ms stayed flat at ~147-153ms (mean 150.01ms) and db_pool_in_use flat at ~13.9-14.4 connections (mean 14.04), consistent with a fail-fast error rather than a latency/dependency-timeout pattern. [F12, F24]
- All three other ledger-db consumers — portfolio-service, payment-service and settlement-service — show flat ~0.21% error rates across 11:30-13:59Z including the 12:12/12:19 onset buckets (0.22/0.21, 0.2/0.26, 0.2/0.21 avg respectively), so a shared ledger-db fault would have failed all four consumers simultaneously and did not. [F20, F21, F22, F6]
- The only other degradation is at the caller: api-gateway error_rate went from ~0.2% (12:00/12:06 buckets) to ~3.37% at 12:12 and stayed ~3.3-3.5% through 13:48 — consistent with a subset of gateway traffic (the order route) failing rather than an independent origin. [F23, F31]
- The only config change across all services in the window is cfg-2101 on notification-service (email.batch_size 200→250 at 11:21Z), which is off the order path and cannot produce order-service 500s; order-service itself had zero config changes in the 2026-02-24→03-03 window, ruling out the HIST-115 pool/profile-override pattern. [F4, F16, F17]
- No margin-service, account-service or risk-service exists in the environment, so margin validation must live inside an existing component (e.g. order-service or portfolio-service). [F13]
- No historical precedent matches this incident's signature: HIST-103 (exchange partner outage, 504/upstream timeouts), HIST-110 (ledger-db IOPS throttling, latency/statement timeouts), HIST-115 (conn-pool config_change), HIST-109 (token validation regression after deployment, auth-service 401s); the closest categorical precedent is HIST-109, a regression introduced by a code deployment. [F19, F7]
- Shared components reachable from order-service are ledger-db (postgres), redis-cache (redis, reached only via pricing-engine) and kafka; none has a change recorded in the change inventory. [F18, F5]
- The shared platform dependencies do not expose error/latency series (ledger-db: connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min; kafka: cpu_pct, messages_in_per_s, under_replicated_partitions; redis-cache: connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct), so no direct error-rate comparison is possible for them. [F25]
- fleet-wide 24h deployment inventory contains only 2 deployments; no deployment or config change is shown as rolled back. [F3]

## Assumptions (not verified)
- The null marginProfile originates in-process from the new validator's own handling of a missing/absent FNO account margin snapshot, rather than from an outbound lookup that started returning null; no trace/span data resolved the failing span.
- No code-level diff or version history beyond the deploy summary is available, so the exact semantics of MarginCheckValidator.java:88 are inferred from the matched log pattern.
- No direct telemetry was obtained for ledger-db/kafka/redis/pricing-engine/exchange-gateway; their health at 12:10Z is accepted on the basis of no recorded change plus the unaffected co-consumers (F20-F22).
- Non-FNO (equity) orders are assumed unaffected because no non-FNO order type appears in the error stream; this is absence of evidence rather than a measured per-order-type failure rate.

## Timeline
- `2026-03-03T09:36:00Z` Only other fleet deployment in the window: dep-5088 portfolio-service v4.2.1 (grpc 1.62→1.63 bump), off the failing path. [F3]
- `2026-03-03T11:21:00Z` Only config change in the window: cfg-2101 notification-service email.batch_size 200→250 (off the order path). [F4, F16]
- `2026-03-03T12:08:00Z` order-service error rate still healthy: 12:08 avg 0.22, 12:09 avg 0.23. [F26]
- `2026-03-03T12:10:00Z` dep-5101 order-service v2.14.0 deployed by r.iyer: new MarginCheckValidator added, legacy OrderRules.validateMargin() removed. Error rate in the deploy minute is still healthy (12:10 avg 0.13, 12:11 avg 0.20). [F1, F14, F26]
- `2026-03-03T12:12:00Z` First full minute after the deploy: order-service error_rate jumps to avg 17.97 (max 17.97); api-gateway error rate simultaneously rises to ~3.37%; all other ledger-db consumers stay flat at ~0.21%. [F8, F26, F23, F20, F21, F22]
- `2026-03-03T12:12:31Z` First order-service ERROR log of the incident: NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) on orderType=FNO, 'marginProfile is null'. [F9, F10]
- `2026-03-03T12:22:00Z` Incident INC-001 reported: spike in failed order placements, HTTP 500 on POST /v1/orders, mostly derivatives orders per support tickets. [F2, F11]
- `2026-03-03T12:14:00Z` Error rate remains elevated and sustained (12:13 avg 17.93, 12:14 avg 19.63). [F27]
- `2026-03-03T13:58:00Z` Error rate still elevated at avg 18.85 (max 19.38); 426 ERROR entries logged across the window; deploy still live with rolled_back_at null. [F8, F9, F27, F1]

## Recommended actions
- **[immediate]** Roll back dep-5101 (order-service v2.14.0) to the previous version and confirm the error rate returns to baseline; since the deploy removed legacy OrderRules.validateMargin() and added MarginCheckValidator, rollback restores the legacy margin path that tolerated/derived the absent FNO margin profile. -- dep-5101 is the only order-service change and the latest fleet change, landing ~2.5 minutes before the first matching NPE; the failure is fail-fast and sustained, and rollback is the lowest-risk mitigation. [F1, F14, F15, F26, F8, F27]
- **[immediate]** If rollback is not immediately possible, disable/feature-flag the MarginCheckValidator F&O branch in the order-service validation chain so FNO orders fall back to the prior margin handling. -- The NPE is deterministic on the new validator at MarginCheckValidator.java:88 for orderType=FNO, so bypassing that branch should stop the 500s for derivatives orders while a proper fix is prepared. [F10, F29, F11]
- **[short_term]** After rollback/disablement, verify recovery on order-service error_rate (expect return to ~0.2-0.3%) and confirm api-gateway error rate falls back toward ~0.2%, while portfolio-/payment-/settlement-service remain flat. -- Recovery after removing dep-5101's new validator is the decisive confirmation of H1; the current data contains no rollback event to validate against (rolled_back_at null). [F8, F23, F20, F21, F22]
- **[short_term]** Fix MarginCheckValidator to null-safe the marginProfile path for FNO accounts (derive or default the margin profile as the legacy OrderRules.validateMargin() path did) and add unit/integration coverage for FNO accounts with an absent margin snapshot before redeploying. -- The root defect is a null dereference on the FNO branch; the legacy path tolerated or derived the null margin profile, so the replacement must preserve that behaviour. [F10, F14, F1]
- **[long_term]** Add per-order-type / per-endpoint error-rate and request-rate dimensions to order-service metrics so non-FNO (equity) health can be positively measured rather than inferred from absence of error logs. -- order-service exposes only service-wide metrics and logs carry no non-FNO order lines, so non-FNO unaffectedness currently rests on absence-of-evidence rather than a computed failure rate — this is the main residual cap on confidence. [F30, F29, F11]
- **[long_term]** Add a canary/staged rollout with automatic error-rate abort for order-service deploys, and expand shared-component telemetry (ledger-db, kafka, redis-cache) to expose error/latency series for faster future blast-radius discrimination. -- The deploy's impact was visible within one minute of going live, but no canary gate stopped it; and the shared components expose no error series, which forced H2 to be excluded indirectly via consumer error rates. [F26, F25, F20, F21, F22]

## Data gaps
- No time-series metrics could be obtained for ledger-db, kafka, redis-cache, pricing-engine or exchange-gateway (their metric namespaces lack error_rate; tc-031/032/033 failed not_found), so a shared-component origin cannot be excluded from direct infrastructure telemetry — it is excluded by the unaffected ledger-db consumer blast radius (F20-F22) and flat order-service latency/pool metrics (F12, F24) instead.
- No deployment/config history was retrieved for the shared components themselves or for exchange-api/bank-partner-api (external components marked has_telemetry:false), so an infrastructure-side change coinciding with the 12:10 onset cannot be positively ruled out.
- No trace/span query was run, so which downstream call (if any) produced the null marginProfile in the failing span was not resolved.
- No per-order-type request/denominator metric exists for order-service and no non-FNO order logging exists, so a true non-FNO failure rate could not be computed; non-FNO unaffectedness rests on absence from the error stream and logs.
- No rollback exists in the data (rolled_back_at null) and current time 13:59Z precedes any remediation, so recovery-after-rollback could not be observed to confirm the hypothesis.
- Sub-minute deploy→onset ordering is bounded to a 60s bucket: the deploy is timestamped 12:10:00Z, the first elevated bucket is 12:12:00Z and the first error log 12:12:31Z; the exact seconds between deploy and first 500 are unknown.
- Latency_p99_ms series were not fetched for portfolio-, payment- or settlement-service (only error_rate), so a purely latency-only degradation of those consumers cannot be strictly excluded, though their error rates were flat to 0.02%.
- Blast radius beyond order-service/api-gateway is only partially confirmed; other services' error/latency/saturation metrics in the 11:30-13:59Z window were not obtained.
- The 12:00-12:20 WARN log scan returned only 31 ERROR entries with no validation-rejection lines, so no pre-12:12 benign validation behaviour by non-FNO orders was observed.

## What would change this conclusion
- Rollback of dep-5101 not reducing order-service error rate (or the NPE continuing after rollback) would overturn H1 and refocus attention on a shared-component or another origin.
- Simultaneous error-rate degradation in portfolio-service, payment-service and/or settlement-service at 12:12/12:19 would revive H2 (shared ledger-db/platform fault) and contradict H1's blast-radius discrimination.
- Evidence that FNO orders reached MarginCheckValidator before dep-5101 (e.g. pre-deploy NPEs or an earlier FNO margin check path) would break the temporal/causal chain.
- Evidence of a null-returning outbound lookup (e.g. traces showing a margin/portfolio dependency returning null, or a change to such a dependency) would shift the mechanism away from in-process null handling and possibly the origin/fix.
- A feature-flag/canary showing dep-5101's MarginCheckValidator was not actually active on the failing traffic path would break the causal chain.
- Evidence that equity (non-FNO) orders were also failing at a comparable rate would contradict the derivatives-only scoping that the mechanism relies on.
- Latency/saturation degradation in order-service or a shared component (e.g. db_pool_in_use exhaustion, redis evictions, kafka under-replicated partitions) would support H4 and undermine the fail-fast code-defect conclusion.

## Investigation stats
- latency 159.69s, LLM calls 24, tokens 180451 (in 149725 / out 30726)
- tool calls 40 (6 failed, 0 partial, 0 recovered by retry), findings 31/31 verified
