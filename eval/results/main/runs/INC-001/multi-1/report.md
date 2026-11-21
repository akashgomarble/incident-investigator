# Incident investigation report: INC-001
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The incident is a deploy-triggered failure: order-service v2.14.0 (dep-5101) landed at 12:10:00Z, and ~2 minutes later the order-service error rate broke from ~0.2% to ~18% (17.95% at the 12:12 bucket), remaining flat at ~18-19.6% through 13:59Z with no rollback. Every logged NPE names MarginCheckValidator.java:88 with "marginProfile is null", and the failures are scoped to orderType=FNO — matching the deploy summary's "add margin check for F&O orders" and the removal of legacy OrderRules.validateMargin() in the same deploy. The mechanism is a local fail-fast validation NPE with no resource saturation and no downstream dependency named; the shared-store hypothesis is contradicted by flat metrics at onset for pricing-engine, market-data-service, ledger-db and redis-cache, by zero config/failover events in the onset window, and by the absence of a graph-legal depth-1 path from order-service to redis-cache. Confidence is medium (0.55), as the reviewed ranking caps it: the discriminator between "new unguarded dereference" (H1, 0.55) and "pre-existing null marginProfile re-exposed" (H3, 0.45) — the source of MarginCheckValidator.java:88 and the removed validateMargin() — was never obtainable, and both predict identical telemetry; this affects the remediation narrative (code fix vs data-contract fix) but not the trigger, timing, or immediate operator action of rolling back/fixing dep-5101.

## Probable root cause
**order-service v2.14.0 introduced an unguarded null-dereference in the new MarginCheckValidator (derivatives path)** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.55, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.14.0 introduced an unguarded null-dereference in the new MarginCheckValidator (derivatives path) | bad_deployment | order-service | dep-5101 | 0.55 (medium) | active |
| 2 | H3: Pre-existing absent/null FNO marginProfile data re-exposed by the new validator (data-condition origin, not a code regression) | data_issue | order-service | dep-5101 | 0.45 (low) | active |
| 3 | H2: A shared platform store (redis-cache / ledger-db / user-db) degraded at ~12:12Z and returned null/absent FNO margin data | infrastructure_failure | redis-cache | null | 0.18 (low) | active |
| 4 | H5: A change elsewhere (portfolio-service grpc bump, notification-service config) or an external exchange-api outage broke the FNO path | config_change | portfolio-service | dep-5088 | 0.03 (low) | rejected |
| 5 | H4: Resource saturation (DB pool / thread pool) on order-service causing 500s | resource_exhaustion | order-service | null | 0.02 (low) | rejected |

### H1: order-service v2.14.0 introduced an unguarded null-dereference in the new MarginCheckValidator (derivatives path)
Deploy dep-5101 (v2.14.0) at 12:10Z added a new MarginCheckValidator to the F&O validation chain and simultaneously removed the legacy OrderRules.validateMargin(). The new validator dereferences marginProfile for orderType=FNO without a null/absence guard, throwing NullPointerException at MarginCheckValidator.java:88, which order-service surfaces as HTTP 500 on POST /v1/orders. Because only the derivatives path runs this validator, failures are concentrated on FNO orders. Confidence held at 0.55 (not 0.82) because the load-bearing hop — that the NEW code itself lacks the guard — is unverified: no diff, deploy content, or source for MarginCheckValidator.java:88 was obtainable [F19, F23], and on available evidence H1 and H3 produce identical telemetry and cannot be separated.

**Causal chain**
1. order-service is deployed at 12:10Z (dep-5101, v2.14.0) with 'new MarginCheckValidator in validation chain' and removal of legacy OrderRules.validateMargin(); the change summary explicitly scopes it to F&O orders; both the addition of the new path and the deletion of the legacy path occur at the same instant [F6, F13, F14, F20, F21, F32]
2. ~2 minutes later, at 12:12Z, order-service error_rate breaks from ~0.2% to ~18% and stays flat through 13:59Z; no rollback occurred, so the fault, if deploy-caused, remained live [F2, F16, F9]
3. The new validator dereferences marginProfile for FNO orders which is null, raising NPE at MarginCheckValidator.java:88 — i.e. the new code path lacks the guard the legacy path may have had *(assumed)*
4. The failure is a local fail-fast validation NPE (marginProfile is null), not a downstream call failure, timeout, or pool event; no dependency is named in any error line, and there is no resource saturation on order-service [F1, F5, F3]
5. Failures are scoped to the derivatives path (every logged NPE carries orderType=FNO), matching the deploy summary's F&O scope [F4, F21]
6. The NPEs are surfaced to the caller as HTTP 500 on POST /v1/orders, producing the reported order-placement failures [F1]

**Supporting evidence**
  - `F6` ✔ order-service deployed v2.14.0 at 2026-03-03T12:10:00Z, 12 minutes before the incident report (12:22Z); status live, rolled_back_at null. Changes: 'new MarginCheckValidator in validation chain', 'remove legacy OrderRules.validateMargin()'.
  - `F13` ✔ order-service deploy dep-5101 (v2.14.0) landed at 2026-03-03T12:10:00Z, status live, rolled_back_at null, with changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'. This is 2 minutes before the onset (~12:12Z) and directly names the validator class appearing in the NPE stack traces.
  - `F14` ✔ Across ALL services in the 24h window (2026-03-02T12:00Z-2026-03-03T13:59Z) there are exactly 2 deployments: portfolio-service dep-5088 v4.2.1 at 09:36Z ('bump grpc 1.62 -> 1.63') and order-service dep-5101 v2.14.0 at 12:10Z. No other order-service deployment exists in the window.
  - `F20` ✔ The deploy metadata does confirm the two facts the task needs as its frame: (1) a NEW initialisation of MarginCheckValidator entered the validation chain, and (2) the LEGACY OrderRules.validateMargin() was REMOVED — both in the same deploy dep-5101 at 12:10:00Z, i.e. the old code path was deleted at the same instant the new one went live. This supports the 'new path replaced legacy path' framing but says nothing about the presence/absence of null guards on either side.
  - `F21` ✔ The deploy summary explicitly scopes the new check to the derivatives path ('add margin check for F&O orders'), matching F4's observation that every logged NPE carries orderType=FNO. So the changed code is on exactly the code path that is failing; however the summary does not state what the new code does when marginProfile is absent.
  - `F32` ✔ In the exact onset window 12:05-12:15Z there was exactly one deployment across ALL services: order-service dep-5101 v2.14.0 at 2026-03-03T12:10:00Z ('Refactor order validation; add margin check for F&O orders'), status live, rolled_back_at null. No deployment for pricing-engine or redis-cache occurred.
  - `F2` ✔ order-service error_rate was flat ~0.2% through the 12:10 bucket (~0.17 avg) and jumped in the next 2-minute bucket to 17.95% at 12:12, staying ~18-19.6% through 13:59Z. The onset is therefore ~12:12Z, ~10 minutes before reported_at 12:22Z.
  - `F16` ✔ No rollback of v2.14.0 occurred (rolled_back_at null), so if the deploy is the cause the fault has remained live and unexplained by any mitigation, consistent with T1's finding that the ~18% error rate stayed flat through 13:59Z.
  - `F1` ✔ First order-service ERROR appears at 2026-03-03T12:12:31Z with two recurring signatures: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId=...' (36 occurrences) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account ...' (34 occurrences). All 70 matched ERRORs fall between 12:12:31Z and 12:29:51Z in this window.
  - `F4` ✔ The failed orders are exclusively derivatives: every logged NPE carries 'orderType=FNO' and the top pattern is scoped to 'orderType=FNO'; no other order type appears in the error stream, consistent with the derivatives-only ticket skew.
  - `F5` ✔ The failure is a fail-fast validation NPE (marginProfile null for an account) raised inside order-service's own MarginCheckValidator, not a downstream call failure, timeout, or pool-exhaustion event; no downstream dependency is named in any error line.
  - `F3` ✔ The spike is not preceded or accompanied by any resource saturation on order-service: db_pool_in_use stays 12.79-15.47 connections (avg ~14) flat across the whole window, latency_p99_ms stays 134-165ms flat, and no pool-exhaustion or timeout logs appear -- only GC-pause WARNs at 11:02:15Z and 11:21:12Z.
  - `F17` ✔ order-service's dependencies (depth 2) are ledger-db, pricing-engine -> market-data-service/redis-cache, exchange-gateway -> exchange-api (external, no telemetry), and kafka. None of these had any deployment or config change in the window, so a change-driven fault in a dependency is not supported; only order-service itself changed.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The FNO null marginProfile results from the new validator's own implementation lacking a null guard (needs source/diff of MarginCheckValidator.java:88) — no code artefact was retrievable [F19, F23].
  - The removed legacy OrderRules.validateMargin() tolerated the same null marginProfile (the regression direction is untested; no historical incident establishes the legacy null-handling contract [F23]).
  - 'Isolated to order-service' is assumed: no cross-service error-rate comparison was run, so blast radius is unmeasured.
  - The 'derivatives-only' skew rests on error-log lines only; no error-rate-by-order-type denominator exists, so non-FNO failures are not positively excluded.
**Rejected because:** null

### H3: Pre-existing absent/null FNO marginProfile data re-exposed by the new validator (data-condition origin, not a code regression)
Some FNO accounts/orders legitimately have an absent or null marginProfile as a long-standing data condition that the legacy validation tolerated. Deploy dep-5101 replaced the tolerant legacy path with a stricter (or unguarded) new MarginCheckValidator, which now dereferences the absent profile and NPEs. The origin is the data contract, with the deploy acting only as the trigger that made the latent condition observable. Raised from 0.22 to 0.45: it is mechanistically indistinguishable from H1 on all available evidence, and the one observation that would separate them (did the legacy path tolerate a null marginProfile?) was never tested and has no historical precedent [F23]. The near-symmetric confidence with H1 reflects genuine ambiguity, not a preference.

**Causal chain**
1. v2.14.0 installed a validator on the F&O path that assumes a non-null marginProfile (legacy path removed in the same deploy) [F6, F13, F20, F21]
2. Some FNO accounts/orders have genuinely absent margin profiles — a data condition predating the incident — which the legacy code path tolerated *(assumed)*
3. On validating such an FNO order the new validator dereferences the absent profile and raises the NPE at MarginCheckValidator.java:88 [F1, F5, F4]
4. The NPE is surfaced as HTTP 500, and the ~18% flat error rate persists only on the derivatives path [F1, F2, F16, F4]

**Supporting evidence**
  - `F1` ✔ First order-service ERROR appears at 2026-03-03T12:12:31Z with two recurring signatures: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId=...' (36 occurrences) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account ...' (34 occurrences). All 70 matched ERRORs fall between 12:12:31Z and 12:29:51Z in this window.
  - `F4` ✔ The failed orders are exclusively derivatives: every logged NPE carries 'orderType=FNO' and the top pattern is scoped to 'orderType=FNO'; no other order type appears in the error stream, consistent with the derivatives-only ticket skew.
  - `F5` ✔ The failure is a fail-fast validation NPE (marginProfile null for an account) raised inside order-service's own MarginCheckValidator, not a downstream call failure, timeout, or pool-exhaustion event; no downstream dependency is named in any error line.
  - `F6` ✔ order-service deployed v2.14.0 at 2026-03-03T12:10:00Z, 12 minutes before the incident report (12:22Z); status live, rolled_back_at null. Changes: 'new MarginCheckValidator in validation chain', 'remove legacy OrderRules.validateMargin()'.
  - `F13` ✔ order-service deploy dep-5101 (v2.14.0) landed at 2026-03-03T12:10:00Z, status live, rolled_back_at null, with changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'. This is 2 minutes before the onset (~12:12Z) and directly names the validator class appearing in the NPE stack traces.
  - `F20` ✔ The deploy metadata does confirm the two facts the task needs as its frame: (1) a NEW initialisation of MarginCheckValidator entered the validation chain, and (2) the LEGACY OrderRules.validateMargin() was REMOVED — both in the same deploy dep-5101 at 12:10:00Z, i.e. the old code path was deleted at the same instant the new one went live. This supports the 'new path replaced legacy path' framing but says nothing about the presence/absence of null guards on either side.
**Contradicting evidence**
  - `F23` ✔ Historical search for a code-regression / unguarded-dereference precedent returned no order-service margin/NPE match: results were HIST-103 (external exchange-api outage, dependency_failure), HIST-110 (ledger-db IOPS throttling, infrastructure_failure) and HIST-115 (connection pool config, config_change), all match_score 0.5. No prior incident establishes whether OrderRules.validateMargin() historically guarded a null marginProfile, so the question of 'pre-existing data condition re-exposed' vs 'new unguarded deref' has no prior for comparison.
**Assumptions**
  - Absent FNO margin profiles predate the incident and are not caused by the deploy or by a live degradation of any store.
  - The legacy OrderRules.validateMargin() tolerated a null marginProfile — never verified; no historical incident documents its contract [F23].
**Rejected because:** null

### H2: A shared platform store (redis-cache / ledger-db / user-db) degraded at ~12:12Z and returned null/absent FNO margin data
marginProfile (or the account/margin record behind it) is sourced from a shared platform store. That store began returning null/absent margin profiles for FNO accounts at ~12:12Z, so order-service's margin check dereferences the null and NPEs regardless of whether the new validator is itself buggy. This is the orchestrator's/reporter's 'order-service merely surfaces an upstream failure' theory. Held at 0.18 rather than near-zero: the critique correctly notes F5/F3/F17 only show the SYMPTOM is local and that no dependency CHANGED — they do not show any shared store is HEALTHY. However, it is weakened by positive health observations at onset: pricing-engine, market-data-service, ledger-db (connections, replication lag) and redis-cache (hit_ratio) are all flat at ~12:12Z [F24, F25, F26, F27], and there is no graph-legal depth-1 path from order-service to redis-cache/market-data-service [F30, F31].

**Causal chain**
1. order-service's direct down-edges are ledger-db (db), pricing-engine (http), exchange-gateway (http), kafka; pricing-engine/market-data-service sit at depth 2+ and terminate at the shared redis-cache [F10, F30, F31]
2. A shared store (redis-cache, ledger-db or user-db) loses or starts returning null FNO margin records at ~12:12Z, yielding a null marginProfile *(assumed)*
3. order-service's margin check dereferences the null profile and raises the NPE, surfacing as HTTP 500 [F1, F5]
4. Only the derivatives path pulls this specific margin data, producing the FNO skew [F4]

**Supporting evidence**
  - `F10` ✔ order-service direct dependencies: ledger-db (db), pricing-engine (http), exchange-gateway (http), kafka (queue-produce); it is called by api-gateway. Transitive: pricing-engine -> market-data-service and redis-cache; market-data-service -> redis-cache and exchange-gateway; exchange-gateway -> exchange-api (external, no telemetry).
  - `F11` ✔ Shared components common to multiple services on this path (notably a shared redis-cache reached from BOTH pricing-engine and market-data-service): ledger-db (postgres, platform, shared:true), redis-cache (redis, platform, shared:true), kafka (platform, shared:true). Because prices/risk and validation flow through these shared platform stores, a fault in one can surface as 500s on order placement without any order-service change.
  - `F4` ✔ The failed orders are exclusively derivatives: every logged NPE carries 'orderType=FNO' and the top pattern is scoped to 'orderType=FNO'; no other order type appears in the error stream, consistent with the derivatives-only ticket skew.
**Contradicting evidence**
  - `F3` ✔ The spike is not preceded or accompanied by any resource saturation on order-service: db_pool_in_use stays 12.79-15.47 connections (avg ~14) flat across the whole window, latency_p99_ms stays 134-165ms flat, and no pool-exhaustion or timeout logs appear -- only GC-pause WARNs at 11:02:15Z and 11:21:12Z.
  - `F5` ✔ The failure is a fail-fast validation NPE (marginProfile null for an account) raised inside order-service's own MarginCheckValidator, not a downstream call failure, timeout, or pool-exhaustion event; no downstream dependency is named in any error line.
  - `F17` ✔ order-service's dependencies (depth 2) are ledger-db, pricing-engine -> market-data-service/redis-cache, exchange-gateway -> exchange-api (external, no telemetry), and kafka. None of these had any deployment or config change in the window, so a change-driven fault in a dependency is not supported; only order-service itself changed.
  - `F24` ✔ pricing-engine, a direct HTTP dependency of order-service, showed no error-rate anomaly around onset: flat ~0.17% at 12:10Z and 12:12Z, max 0.32% across 12:00-12:30Z (mean 0.21%). No break at ~12:12Z.
  - `F25` ✔ market-data-service (transitive dependency via pricing-engine) showed no error-rate anomaly: 0.26% at 12:10Z, 0.24% at 12:12Z, range 0.11-0.32% across 12:00-12:30Z. No break at onset.
  - `F26` ✔ ledger-db (shared postgres store on the order path) is flat around onset across its available metrics: connections_active 61.97 at 12:10Z and 61.97 at 12:12Z (range 58.62-66.84, mean 62.34), and replication_lag_ms 39.12 at 12:10Z, 41.27 at 12:12Z (range 37.46-42.77, mean 39.95). No connection or replication anomaly at ~12:12Z.
  - `F27` ✔ redis-cache (shared platform store reached by both pricing-engine and market-data-service) is flat around onset: hit_ratio_pct 95.82 at 12:10Z, 96.05 at 12:12Z, range 94.05-97.03%, mean 95.63%. No cache degradation at ~12:12Z.
  - `F29` ✔ Inference: across the four path dependencies/shared stores measurable in the onset window, no shared component shows a degradation, new error signature, or latency break at ~12:12Z, so H2's shared-store fault (null/absent margin data for FNO) is not supported by these metrics; the signal at onset remains localized to order-service (F2/F3).
  - `F30` ✔ order-service has NO direct dependency on pricing-engine/market-data-service/redis-cache for a 'margin' pathway spanning the graph: its direct down-edges are ledger-db (db), pricing-engine (http), exchange-gateway (http), kafka (queue-produce). pricing-engine and market-data-service sit at depth 2+ and both terminate at the shared redis-cache; there is no edge from order-service to redis-cache or market-data-service at depth 1.
**Assumptions**
  - A shared store actually holds marginProfile for FNO accounts and began returning nulls at onset — no telemetry for the FNO margin records themselves was retrieved [F28].
  - A degradation subtle enough to escape the sampled metrics (connections/lag/hit-ratio/error_rate) occurred at exactly 12:12Z; the un-sampled metrics (slow_queries, cpu_pct, disk_used, evictions, memory_pct) could hide it.
**Rejected because:** null

### H5: A change elsewhere (portfolio-service grpc bump, notification-service config) or an external exchange-api outage broke the FNO path
A recent change to a component shared across services — portfolio-service v4.2.1 grpc 1.62->1.63 or notification-service email.batch_size — or an external exchange-api outage degraded order placement, and order-service merely surfaced 500s.

**Causal chain**
1. portfolio-service v4.2.1 deployed at 09:36Z with a grpc version bump; notification-service config changed at 11:21Z [F7, F14, F15, F8]
2. The grpc bump perturbs the shared path used by order placement *(assumed)*
3. order-service margin check fails -> NPE -> HTTP 500 [F1]

**Supporting evidence**
  - `F7` ✔ Only two deployments across ALL services in the 24h window before onset: order-service v2.14.0 (12:10Z) and portfolio-service v4.2.1 at 2026-03-03T09:36:00Z ('bump grpc 1.62 -> 1.63', ci-bot, live, not rolled back).
  - `F14` ✔ Across ALL services in the 24h window (2026-03-02T12:00Z-2026-03-03T13:59Z) there are exactly 2 deployments: portfolio-service dep-5088 v4.2.1 at 09:36Z ('bump grpc 1.62 -> 1.63') and order-service dep-5101 v2.14.0 at 12:10Z. No other order-service deployment exists in the window.
  - `F15` ✔ Exactly one config/flag change exists across ALL services in the window: notification-service cfg-2101 email.batch_size 200 -> 250 at 2026-03-03T11:21:00Z. It touches notification-service (unrelated to the FNO order path) and predates onset by 51 minutes.
**Contradicting evidence**
  - `F2` ✔ order-service error_rate was flat ~0.2% through the 12:10 bucket (~0.17 avg) and jumped in the next 2-minute bucket to 17.95% at 12:12, staying ~18-19.6% through 13:59Z. The onset is therefore ~12:12Z, ~10 minutes before reported_at 12:22Z.
  - `F4` ✔ The failed orders are exclusively derivatives: every logged NPE carries 'orderType=FNO' and the top pattern is scoped to 'orderType=FNO'; no other order type appears in the error stream, consistent with the derivatives-only ticket skew.
  - `F5` ✔ The failure is a fail-fast validation NPE (marginProfile null for an account) raised inside order-service's own MarginCheckValidator, not a downstream call failure, timeout, or pool-exhaustion event; no downstream dependency is named in any error line.
  - `F13` ✔ order-service deploy dep-5101 (v2.14.0) landed at 2026-03-03T12:10:00Z, status live, rolled_back_at null, with changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'. This is 2 minutes before the onset (~12:12Z) and directly names the validator class appearing in the NPE stack traces.
  - `F17` ✔ order-service's dependencies (depth 2) are ledger-db, pricing-engine -> market-data-service/redis-cache, exchange-gateway -> exchange-api (external, no telemetry), and kafka. None of these had any deployment or config change in the window, so a change-driven fault in a dependency is not supported; only order-service itself changed.
  - `F32` ✔ In the exact onset window 12:05-12:15Z there was exactly one deployment across ALL services: order-service dep-5101 v2.14.0 at 2026-03-03T12:10:00Z ('Refactor order validation; add margin check for F&O orders'), status live, rolled_back_at null. No deployment for pricing-engine or redis-cache occurred.
  - `F33` ✔ There were ZERO config/flag changes across ALL services in the 12:00-12:20Z window (count 0, empty list). So no failover, eviction-policy, TTL, or cache/margin configuration event occurred for redis-cache, ledger-db, pricing-engine or market-data-service in the onset window.
  - `F21` ✔ The deploy summary explicitly scopes the new check to the derivatives path ('add margin check for F&O orders'), matching F4's observation that every logged NPE carries orderType=FNO. So the changed code is on exactly the code path that is failing; however the summary does not state what the new code does when marginProfile is absent.
**Assumptions**
  - A 09:36Z deploy caused a symptom 2h36m later, or a notification-service config unrelated to the FNO path caused it 51 min later.
**Rejected because:** Rejected on timing and scope: both candidate changes are scope-disjoint from the FNO order path (notification-service config is unrelated; portfolio-service predates onset by 2h36m), none of order-service's actual dependencies were changed in the window [F17], and the onset window 12:05-12:15Z contains exactly one change across all services — the order-service deploy [F32, F33]. The NPE is a local fail-fast validator error naming MarginCheckValidator, not a dependency failure [F5]. Note the external exchange-api limb remains dismissed only by absence of tooling (has_telemetry:false), not by absence of a fault; however the observed failure mechanism (local NPE on marginProfile) is not the shape an exchange-api outage would produce (cf. HIST-103's 504s), so the limb is strongly disfavoured.

### H4: Resource saturation (DB pool / thread pool) on order-service causing 500s
order-service ran out of DB connections or threads, causing request failures surfaced as 500s; matches HIST-115's connection-pool precedent.

**Causal chain**
1. Connection/thread pool exhausts on order-service *(assumed)*
2. Requests fail/time out and return 500 *(assumed)*
3. Derivatives orders are hit hardest as the heaviest path *(assumed)*

**Supporting evidence**
  - `F12` ✔ Historical leads only (not evidence for this incident): HIST-115 (2025-09-25) order-service 'connection pool misconfiguration' -> 'connection is not available'/'pool exhausted' (config_change); HIST-110 (2026-01-15) ledger-db IOPS throttling causing slow orders/statement timeouts; HIST-103 (2025-10-21) order 504s from external exchange-api outage; HIST-104 (2025-11-05) notification-service Kafka consumer lag after broker rebalance.
**Contradicting evidence**
  - `F3` ✔ The spike is not preceded or accompanied by any resource saturation on order-service: db_pool_in_use stays 12.79-15.47 connections (avg ~14) flat across the whole window, latency_p99_ms stays 134-165ms flat, and no pool-exhaustion or timeout logs appear -- only GC-pause WARNs at 11:02:15Z and 11:21:12Z.
  - `F5` ✔ The failure is a fail-fast validation NPE (marginProfile null for an account) raised inside order-service's own MarginCheckValidator, not a downstream call failure, timeout, or pool-exhaustion event; no downstream dependency is named in any error line.
  - `F1` ✔ First order-service ERROR appears at 2026-03-03T12:12:31Z with two recurring signatures: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId=...' (36 occurrences) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account ...' (34 occurrences). All 70 matched ERRORs fall between 12:12:31Z and 12:29:51Z in this window.
  - `F2` ✔ order-service error_rate was flat ~0.2% through the 12:10 bucket (~0.17 avg) and jumped in the next 2-minute bucket to 17.95% at 12:12, staying ~18-19.6% through 13:59Z. The onset is therefore ~12:12Z, ~10 minutes before reported_at 12:22Z.
**Assumptions**
  - A pool would saturate without showing in db_pool_in_use and without emitting pool/timeout logs.
**Rejected because:** Rejected: db_pool_in_use flat at ~12.8-15.5 connections, latency_p99 flat 134-165ms, no pool-exhaustion or timeout logs [F3]; the failure is a local fail-fast NPE, not a saturation/timeout mechanism [F5]. F12 is a historical lead (HIST-115), not evidence for this incident.

## Facts (verified observations)
- order-service deployed v2.14.0 (dep-5101) at 2026-03-03T12:10:00Z, status live, rolled_back_at null, with change summary 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'. [F6, F13]
- In the exact onset window 12:05-12:15Z there was exactly one deployment across ALL services: order-service dep-5101 v2.14.0 at 12:10:00Z ('Refactor order validation; add margin check for F&O orders'); no deployment for pricing-engine or redis-cache occurred, and there were ZERO config/flag changes in 12:00-12:20Z. [F32, F33]
- Across the full 24h pre-onset window (2026-03-02T12:00Z-2026-03-03T13:59Z) there were exactly two deployments — portfolio-service dep-5088 v4.2.1 at 09:36Z and order-service dep-5101 at 12:10Z — and exactly one config change, notification-service cfg-2101 email.batch_size 200->250 at 11:21Z (unrelated to the FNO order path). [F7, F14, F8, F15]
- order-service error_rate was flat ~0.2% through the 12:10 bucket (~0.17 avg) and broke to 17.95% at the 12:12 bucket, staying ~18-19.6% through 13:59Z; onset is therefore ~12:12Z, ~10 minutes before the 12:22Z report. [F2]
- The first order-service ERROR appears at 2026-03-03T12:12:31Z with recurring signatures 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO' (36 occurrences) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account ...' (34 occurrences). [F1]
- All failed orders in the error stream are derivatives: every logged NPE carries orderType=FNO and no other order type appears, consistent with the derivatives-only ticket skew. [F4]
- The spike is not accompanied by any resource saturation on order-service: db_pool_in_use flat at 12.79-15.47 connections, latency_p99_ms flat at 134-165ms, and no pool-exhaustion or timeout logs — only GC-pause WARNs at 11:02:15Z and 11:21:12Z. [F3]
- Shared dependencies measurable at onset were flat: pricing-engine error rate ~0.17% at 12:10Z and 12:12Z (max 0.32% across 12:00-12:30Z); market-data-service 0.26% at 12:10Z and 0.24% at 12:12Z; ledger-db connections_active 61.97 at both 12:10Z and 12:12Z with replication_lag_ms 39.12/41.27; redis-cache hit_ratio_pct 95.82 at 12:10Z and 96.05 at 12:12Z. [F24, F25, F26, F27]
- None of order-service's depth-2 dependencies (ledger-db, pricing-engine, market-data-service/redis-cache, exchange-gateway, kafka) had any deployment or config change in the window, so a change-driven fault in a dependency is not supported; only order-service itself changed. [F17]
- order-service has no direct (depth-1) dependency on redis-cache or market-data-service; its direct down-edges are ledger-db, pricing-engine, exchange-gateway and kafka, and redis-cache is reached from both pricing-engine and market-data-service at depth 2+. [F30]
- No pricing-engine or redis-cache deployment exists in the pre-onset baseline (both count 0) and no order-service config/flag change exists in the window (count 0), so the margin-validator change can only have arrived via dep-5101's code. [F34, F22]
- No rollback of v2.14.0 occurred (rolled_back_at null) and no further order-service deployment appeared between 12:10Z and 13:59Z, consistent with the ~18% error rate remaining flat through the end of data. [F9]
- The deploy metadata confirms (1) a new initialisation of MarginCheckValidator entered the validation chain and (2) the legacy OrderRules.validateMargin() was removed, both in dep-5101 at 12:10:00Z, and the change summary explicitly scopes the check to the derivatives path ('add margin check for F&O orders'). [F20]
- No source diff, code body, or line-level content for dep-5101 is exposed by any available tool; the deploy record shows the same 2-line textual summary in two independent query windows. [F19]
- Historical search for the exact NPE/marginProfile signature returned no order-service match: results were HIST-103 (external exchange-api outage), HIST-110 (ledger-db IOPS throttling) and HIST-115 (connection pool config), all match_score 0.5, so no prior incident establishes the legacy validateMargin() null-handling contract. [F23, F18]
- Failure orders were 504s in HIST-103 (external exchange-api outage), a different failure shape from the observed local NPE on marginProfile. [F12]
- across the four path dependencies/shared stores measurable in the onset window, no shared component shows a degradation, new error signature, or latency break at ~12:12Z; the signal at onset remains localized to order-service. [F2, F3]

## Assumptions (not verified)
- The FNO null marginProfile results from the new validator's own implementation lacking a null guard at MarginCheckValidator.java:88 — no code artefact was retrievable to confirm this [F19, F23].
- The removed legacy OrderRules.validateMargin() tolerated the same null marginProfile, i.e. the regression direction is real — this legacy null-handling contract was never verified and has no historical precedent [F23].
- 'Isolated to order-service' is assumed: no cross-service error-rate comparison was completed, so blast radius is unmeasured.
- The 'derivatives-only' skew rests on error-log lines only; no error-rate-by-order-type denominator exists, so non-FNO failures are not positively excluded.
- If H3 is true, absent/null FNO margin profiles predate the incident and are a standing data condition rather than a live store degradation.
- No metrics beyond error_rate were available for pricing-engine/market-data-service and only a subset for ledger-db/redis-cache, so 'dependencies healthy' rests on the metrics that were sampled.
- (demoted from facts: no verified observation backs it) The failure is a fail-fast validation NPE raised inside order-service's own MarginCheckValidator, not a downstream call failure, timeout, or pool-exhaustion event; no downstream dependency is named in any error line.

## Timeline
- `2026-03-03T09:36:00Z` portfolio-service v4.2.1 (dep-5088) deployed, 'bump grpc 1.62 -> 1.63' — the only other deployment in the 24h window; scope-disjoint from the FNO order path. [F7, F14]
- `2026-03-03T11:02:15Z` order-service emits a GC-pause WARN; no resource saturation signature appears around the incident. [F3]
- `2026-03-03T11:21:00Z` notification-service config change cfg-2101 email.batch_size 200 -> 250 — the only config change in the window, on an unrelated service, 51 minutes before onset. [F8, F15]
- `2026-03-03T12:10:00Z` order-service v2.14.0 (dep-5101) deployed by r.iyer: new MarginCheckValidator added to the validation chain, legacy OrderRules.validateMargin() removed. The only deployment across all services in 12:05-12:15Z; zero config changes in 12:00-12:20Z. [F6, F13, F20, F21, F32, F33]
- `2026-03-03T12:10:00Z` order-service error_rate still flat at ~0.17%; pricing-engine, market-data-service, ledger-db and redis-cache all show no anomaly at this bucket. [F2, F24, F25, F26, F27]
- `2026-03-03T12:12:00Z` order-service error_rate breaks to 17.95% (from ~0.2% in the prior bucket); shared dependencies remain flat. [F2, F29]
- `2026-03-03T12:12:31Z` First order-service ERROR logged: NullPointerException at MarginCheckValidator.java:88 with marginProfile null for orderType=FNO. [F1]
- `2026-03-03T12:22:00Z` Incident INC-001 reported by on-call SRE (SEV-1), ~10 minutes after actual onset. [F2]
- `2026-03-03T12:29:51Z` Last ERROR in the sampled window; all 70 matched ERRORs fall between 12:12:31Z and 12:29:51Z, with the error rate persisting ~18-19.6% regardless. [F1, F2]
- `2026-03-03T13:59:00Z` Latest data: error rate still ~18-19.6%, no rollback of dep-5101 (rolled_back_at null), no further order-service deployment. [F2, F9, F16]

## Recommended actions
- **[immediate]** Immediately roll back order-service dep-5101 (v2.14.0) to the previous version, restoring the pre-deploy code path. This is the single change in the onset window and the error rate remains flat at ~18-19.6% while it is live. -- dep-5101 landed at 12:10Z and onset followed at 12:12Z; it is the only deployment across all services in 12:05-12:15Z and the only candidate matching the NPE's code path. No rollback has occurred, so the fault has remained live. [F6, F13, F32, F33, F16, F9, F2]
- **[immediate]** If rollback is not feasible, mitigate the FNO path specifically: revert the F&O margin check to the removed legacy OrderRules.validateMargin() behavior or put the new MarginCheckValidator behind a disable switch until the null-guard is reviewed. Note: no order-service feature flag exists today (0 config changes), so this requires a code/config redeploy. -- Failures are scoped entirely to orderType=FNO, matching the deploy's 'add margin check for F&O orders' scope; disabling just that validator restores service for derivatives orders with minimal blast radius. [F4, F21, F22]
- **[short_term]** After rollback, confirm recovery by re-checking order-service error_rate (expect a return to ~0.2%), FNO 500s, and the absence of the MarginCheckValidator.java:88 NPE in logs; keep the watcher open through the next 30-60 minutes. -- The onset and continuation were pinned by these exact signals, so the same series is the verification instrument. [F2, F1, F4, F16]
- **[short_term]** Obtain and review the source of MarginCheckValidator.java:88 and the removed OrderRules.validateMargin() from the repository/git history to determine which side tolerated a null marginProfile. This is the single discriminator between the code-regression and data-condition explanations and determines whether the permanent fix is a null guard or a data-contract fix. -- The load-bearing hop — that the NEW code itself lacks the guard — is unverified; no diff, deploy content or source was retrievable from any tool. [F19, F23, F20]
- **[short_term]** Complete a cheap explicit check of the shared stores feeding marginProfile at 12:10-12:15Z (ledger-db slow_queries_per_min/cpu_pct/disk_used_pct and redis-cache cpu_pct/memory_pct/evictions_per_min) to close the residual shared-store doubt. -- These specific metrics were never queried; if flat, the shared-store hypothesis is fully falsified and the conclusion tightens to the deploy. [F24, F25, F26, F27, F28, F29]
- **[long_term]** Once the guard/tolerance question is answered, land the fix with a regression test covering orderType=FNO with an absent/null marginProfile, and add a null-safe default so a missing profile cannot NPE a request. -- Both remaining hypotheses converge on the same null marginProfile at the FNO validation boundary; an explicit, tested contract eliminates the failure mode regardless of origin. [F1, F5, F20, F21]
- **[long_term]** Add an error-rate-by-order-type (FNO vs non-FNO) metric and service-metric coverage for the shared stores (ledger-db, redis-cache) so blast radius and the derivatives-only scope can be measured directly next time rather than inferred from error logs. -- The derivatives-only claim rests on error logs without a denominator, and no shared dependency was sampled for blast radius; both were identified gaps. [F4, F28]

## Data gaps
- No tool exposes deploy content, diff, or source, so MarginCheckValidator.java:88 and OrderRules.validateMargin() could not be reviewed; this is the discriminator between H1 (code regression, 0.55) and H3 (pre-existing data condition re-exposed, 0.45), which produce identical telemetry.
- No source-repository, static-analysis, or git-diff tool is available and historical search found no incident documenting the legacy validateMargin() null-handling contract, so the legacy behaviour cannot be reconstructed.
- Blast radius was not established: completed dependency/candidate service queries were limited (the services 'margin-service', 'account-service' and 'risk-service' returned not_found and a follow-up query hit budget exhaustion), so it is unknown whether other services show the same ~18% spike at 12:12Z.
- No error-rate-by-order-type denominator exists for POST /v1/orders, and no log line enumerates non-FNO order attempts, so the 'derivatives-only' claim rests on error logs alone and non-FNO failures are not positively excluded.
- Some shared-store metrics were never sampled (ledger-db slow_queries_per_min/cpu_pct/disk_used_pct, redis-cache cpu_pct/memory_pct/evictions_per_min, user-db entirely), so a subtler degradation in a shared store not captured by connections/lag/hit-ratio cannot be excluded.
- Error-rate and latency data for pricing-engine/market-data-service beyond error_rate were not retrieved (metrics budget exhausted), and ERROR-level logs for those services in 12:05-12:30Z were not run.
- exchange-api is external with no telemetry, so an upstream vendor outage on the derivatives path cannot be confirmed or excluded — though the observed local NPE is not the shape an exchange outage produces (cf. HIST-103's 504s).
- api-gateway returned zero upstream edges, so gateway-side or edge-layer changes affecting POST /v1/orders could not be ruled out.
- No telemetry was sampled for portfolio-service or notification-service error rates, so their non-involvement rests on scope and timing rather than demonstrated health.

## What would change this conclusion
- Source review showing the removed OrderRules.validateMargin() already dereferenced a null marginProfile without a guard, or that the new MarginCheckValidator does guard it — this would collapse H1 into H3 (data-contract origin) and change the fix from a code guard to a data-contract fix.
- Evidence that absent/null FNO marginProfile is a long-standing data condition with a documented legacy contract would move the root cause from the deploy's code to the marginProfile data path.
- A metric or log showing degradation of a shared store that actually holds marginProfile/account margin data at 12:10-12:15Z (e.g. ledger-db or user-db slow queries, CPU/disk, or redis-cache memory/evictions) would revive H2 despite the flat connections/lag/hit-ratio evidence.
- Discovery that order-service reaches marginProfile via a graph-legal path different from the topology recorded (e.g. a direct redis-cache read or an undocumented service) would invalidate the 'no depth-1 path' argument against H2.
- Evidence of an order-service feature-flag or runtime config flip specific to the F&O margin check (a dedicated feature-flag tool was never invoked) would indicate a config-change mechanism rather than a code deploy.
- Error-rate-by-order-type data showing non-FNO order types also failing would broaden the scope beyond the FNO validator and undercut the current deploy-plus-FNO-path explanation.
- A confirmed external exchange-api outage or gateway-side change affecting the derivatives order path would reopen H5's external limb, which is currently dismissed only for lack of tooling.

## Integrity warnings (set by code)
- fact demoted to assumption: The failure is a fail-fast validation NPE raised inside order-service's own Marg

## Investigation stats
- latency 169.88s, LLM calls 27, tokens 184745 (in 150923 / out 33822)
- tool calls 42 (5 failed, 0 partial, 0 recovered by retry), findings 35/35 verified
