# Incident investigation report: INC-003
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
Order submit failures (~30% error rate, 504s) began at ~11:01Z and are best explained by the top-ranked hypothesis H1 (confidence 0.55, medium): an independent failure of the external exchange partner api.exchange-partner.example, which made exchange-gateway's placeOrder/quoteSnapshot calls time out, opened its circuit breaker at 11:03:26, and surfaced 504 Gateways back through order-service. The evidence that supports this is strong on the symptom chain — exchange-gateway upstream timeouts at 11:01:25Z precede order-service's first 504 at 11:02:45Z, and the only error signature order-service emits is 'exchange-gateway call failed: Gateway Timeout (placeOrder)'. However, the conclusion is not adequately supported for a root_cause_identified call: the external partner has no telemetry (has_telemetry=false), so the partner's own failure cannot be directly observed or shown to be independent of the order-service v2.15.2 deploy that fell in the same 10:48Z–11:16Z window, and H2 (bad deployment, 0.12) was not excluded by direct data. Given the uncorroborated external dependency and the residual coincidence-with-deploy question, I report inconclusive with safe mitigations and the specific observations that would discriminate between H1 and H2. The rollback already performed (11:16Z) should be evaluated for whether it actually reduced the failure rate.

## Probable root cause
**Independent failure of the external exchange partner (api.exchange-partner.example)** -- dependency_failure in `api.exchange-partner.example (external exchange API)`, triggered by `null` (confidence 0.55, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Independent failure of the external exchange partner (api.exchange-partner.example) | dependency_failure | api.exchange-partner.example (external exchange API) | null | 0.55 (medium) | active |
| 2 | H2: order-service v2.15.2 release introduced a regression that drives the 504s | bad_deployment | order-service | dep-5333 | 0.12 (low) | active |
| 3 | H4: Shared platform component (ledger-db / redis-cache / kafka) saturates and propagates to order-service | resource_exhaustion | ledger-db / redis-cache / kafka (shared platform components) | null | 0.05 (low) | active |
| 4 | H3: pricing-engine cache.ttl_seconds config change (09:20Z) degrades a dependency chain reaching order-service | config_change | pricing-engine | null | 0.03 (low) | active |

### H1: Independent failure of the external exchange partner (api.exchange-partner.example)
The external trading partner endpoint api.exchange-partner.example began failing (~503 / connect timeouts) at ~11:01Z, causing exchange-gateway's upstream calls (placeOrder, quoteSnapshot) to time out, opening its circuit breaker at 11:03:26 and pushing 504 Gateway Timeout responses back to order-service, which surfaces them as ~30% order submit failures. The order-service v2.15.2 deploy is coincidental in time.

**Causal chain**
1. External exchange partner api.exchange-partner.example degrades / returns 503 at ~11:01Z (origin; no telemetry available) *(assumed)*
2. exchange-gateway sees connect timeouts + 503 'Service Unavailable' from api.exchange-partner.example starting 11:01:25Z on both placeOrder and quoteSnapshot [F10]
3. exchange-gateway latency p99 jumps ~40x (128ms -> ~4.6s) in the same 11:00 bucket and error_rate jumps 0.2% -> ~39% [F13, F14]
4. exchange-gateway circuit breaker opens at 11:03:26 (failure ratio 0.47 over 60s), further failing fast [F10]
5. order-service's calls to exchange-gateway return 504 Gateway Timeout; all 349 ERROR logs share the single signature 'exchange-gateway call failed: Gateway Timeout (placeOrder)' with no code exceptions or stack traces [F9, F12]
6. order-service error_rate jumps 0.2% -> ~21-32% and p99 latency jumps ~35x (150ms -> ~4.3-5.4s) from the 11:00-11:01 bucket onward [F7, F8]
7. Reported symptom: ~30% order submit failure rate with 504s [F7]

**Supporting evidence**
  - `F10` ✔ exchange-gateway shows upstream timeouts to host=api.exchange-partner.example (ops placeOrder AND quoteSnapshot) starting 2026-03-17T11:01:25Z — ~80s BEFORE order-service's first 504 — plus 503 'Service Unavailable' from exchange-api and a circuit breaker opening at 11:03:26 ('failure ratio 0.47 over 60s').
  - `F13` ✔ exchange-gateway error_rate was flat at ~0.2% through the 10:50 bucket, then jumped to avg 39.33% (max 45.46) in the 11:00 bucket and stayed 42–45% through 12:50 — the same 10-min bucket in which order-service's error rate rose.
  - `F14` ✔ exchange-gateway p99 latency was ~128–131ms through 10:50, then jumped to avg 4625.9ms (max 5287.98) in the 11:00 bucket and ~4900–5200ms thereafter — a ~40x rise coincident with order-service's latency jump.
  - `F9` ✔ All 349 order-service ERROR logs share one signature: 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)', first_seen 2026-03-17T11:02:45Z and continuing to 12:58:54Z — there are no stack traces or code-exception signatures.
  - `F12` ✔ The failure pattern is a downstream dependency timeout fronted by an external partner: order-service only ever reports 504 to exchange-gateway, while exchange-gateway reports connect timeouts/503 from api.exchange-partner.example; no order-service-internal code error, stack trace, or resource-saturation metric accompanies the onset.
  - `F8` ✔ order-service p99 latency was ~150ms (146–166ms max) until the 10:50 bucket, then rose to avg 4285.85ms in the 11:00 bucket and ~5200–5400ms afterward — a ~35x latency jump coincident with the error onset.
  - `F7` ✔ order-service error_rate sat at ~0.2% (avg 0.13–0.25) from 09:00 through the 10:58 bucket, then jumped to avg 21.04% in the 11:01 bucket and ~30–32% thereafter through 12:58.
  - `F5` ✔ Historical precedent with the same symptom pair is HIST-103 (2025-10-21, order-service + exchange-gateway, '504 on order submit', 'upstream timeouts', category dependency_failure, root cause external exchange API outage, match_score 4) — the highest-scoring match; other matches include ledger-db failover/IOPS (HIST-102, HIST-110) and api-gateway envoy upgrade (HIST-116, '504 at gateway'). These are leads only, explicitly 'not evidence about the current incident'.
  - `F4` ✔ order-service's dependency graph contains three SHARED platform components that are not owned by trading and are reachable by other services: ledger-db (postgres, shared), redis-cache (redis, shared), kafka (kafka, shared). Callers/callees: api-gateway -> order-service (http) upstream; order-service -> pricing-engine, exchange-gateway (http), -> kafka (queue-produce), -> ledger-db (db); pricing-engine -> market-data-service (http) and -> redis-cache (cache); exchange-gateway -> exchange-api (external, has_telemetry=false).
**Contradicting evidence**
  - (none)
**Assumptions**
  - The external partner's own degradation at ~11:01Z is real and independent of order-service (no telemetry exists for api.exchange-partner.example, has_telemetry=false)
  - exchange-gateway's upstream timeout at 11:01:25Z genuinely precedes order-service's first 504 at 11:02:45Z (log timestamps, not metric buckets)

### H2: order-service v2.15.2 release introduced a regression that drives the 504s
The v2.15.2 release ('Add idempotency keys to order submit', dep-5333) introduced a defect in the order submit path that manifests as timeouts/504s toward its downstream exchange-gateway call, producing ~30% submit failures during the 10:48Z-11:16Z exposure window.

**Causal chain**
1. order-service v2.15.2 (dep-5333) deployed at 10:48:00Z adding idempotency keys to order submit [F1]
2. The change introduces a defect on the order submit path (origin; no stack traces or code-exception signatures were observed, so the mechanism is unproven) *(assumed)*
3. order-service error_rate jumps from 0.2% to ~21-32% and p99 latency rises ~35x from the 11:01 bucket [F7, F8]
4. order-service logs show only 'exchange-gateway call failed: Gateway Timeout' — the deploy's failure would have to express itself purely as downstream 504s [F9]
5. Reported symptom: ~30% order submit failure rate [F7]

**Supporting evidence**
  - `F1` ✔ The only deployment across all services in 06:00Z-12:59Z is order-service v2.15.2 (dep-5333), deployed 2026-03-17T10:48:00Z by a.khan, summary 'Add idempotency keys to order submit', status 'rolled_back' with rolled_back_at 2026-03-17T11:16:00Z — i.e. a 28-minute exposure window before rollback.
  - `F6` ✔ Only one change (dep-5333) falls inside the order-service failure window; the pricing-engine TTL config change at 09:20Z precedes the reported onset and the deploy, and the config-change and per-service deployment sweeps were complete (partial:false) for the services queried.
  - `F7` ✔ order-service error_rate sat at ~0.2% (avg 0.13–0.25) from 09:00 through the 10:58 bucket, then jumped to avg 21.04% in the 11:01 bucket and ~30–32% thereafter through 12:58.
  - `F8` ✔ order-service p99 latency was ~150ms (146–166ms max) until the 10:50 bucket, then rose to avg 4285.85ms in the 11:00 bucket and ~5200–5400ms afterward — a ~35x latency jump coincident with the error onset.
**Contradicting evidence**
  - `F10` ✔ exchange-gateway shows upstream timeouts to host=api.exchange-partner.example (ops placeOrder AND quoteSnapshot) starting 2026-03-17T11:01:25Z — ~80s BEFORE order-service's first 504 — plus 503 'Service Unavailable' from exchange-api and a circuit breaker opening at 11:03:26 ('failure ratio 0.47 over 60s').
  - `F13` ✔ exchange-gateway error_rate was flat at ~0.2% through the 10:50 bucket, then jumped to avg 39.33% (max 45.46) in the 11:00 bucket and stayed 42–45% through 12:50 — the same 10-min bucket in which order-service's error rate rose.
  - `F14` ✔ exchange-gateway p99 latency was ~128–131ms through 10:50, then jumped to avg 4625.9ms (max 5287.98) in the 11:00 bucket and ~4900–5200ms thereafter — a ~40x rise coincident with order-service's latency jump.
  - `F9` ✔ All 349 order-service ERROR logs share one signature: 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)', first_seen 2026-03-17T11:02:45Z and continuing to 12:58:54Z — there are no stack traces or code-exception signatures.
  - `F12` ✔ The failure pattern is a downstream dependency timeout fronted by an external partner: order-service only ever reports 504 to exchange-gateway, while exchange-gateway reports connect timeouts/503 from api.exchange-partner.example; no order-service-internal code error, stack trace, or resource-saturation metric accompanies the onset.
**Assumptions**
  - A code change in order-service can cause exchange-gateway itself to report upstream 503/timeouts from the external partner
  - The 13-minute gap between deploy (10:48) and onset (~11:01) reflects request mix/pool warming rather than a lack of causal link

### H4: Shared platform component (ledger-db / redis-cache / kafka) saturates and propagates to order-service
A shared platform component (ledger-db postgres, redis-cache, or kafka) degraded at ~11:00Z — e.g. connection pool exhaustion or broker stall — and the resulting backpressure surfaced as order submit timeouts in order-service.

**Causal chain**
1. A shared component (ledger-db, redis-cache, kafka) fails or saturates at ~11:00Z (origin; not verifiable, no deployments rows or telemetry for datastores) *(assumed)*
2. order-service experiences timeouts on submit path due to backpressure from the shared component *(assumed)*
3. order-service error_rate and latency jump from the 11:00-11:01 bucket [F7, F8]
4. Reported symptom: ~30% order submit failure rate [F7]

**Supporting evidence**
  - `F4` ✔ order-service's dependency graph contains three SHARED platform components that are not owned by trading and are reachable by other services: ledger-db (postgres, shared), redis-cache (redis, shared), kafka (kafka, shared). Callers/callees: api-gateway -> order-service (http) upstream; order-service -> pricing-engine, exchange-gateway (http), -> kafka (queue-produce), -> ledger-db (db); pricing-engine -> market-data-service (http) and -> redis-cache (cache); exchange-gateway -> exchange-api (external, has_telemetry=false).
**Contradicting evidence**
  - `F9` ✔ All 349 order-service ERROR logs share one signature: 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)', first_seen 2026-03-17T11:02:45Z and continuing to 12:58:54Z — there are no stack traces or code-exception signatures.
  - `F10` ✔ exchange-gateway shows upstream timeouts to host=api.exchange-partner.example (ops placeOrder AND quoteSnapshot) starting 2026-03-17T11:01:25Z — ~80s BEFORE order-service's first 504 — plus 503 'Service Unavailable' from exchange-api and a circuit breaker opening at 11:03:26 ('failure ratio 0.47 over 60s').
  - `F13` ✔ exchange-gateway error_rate was flat at ~0.2% through the 10:50 bucket, then jumped to avg 39.33% (max 45.46) in the 11:00 bucket and stayed 42–45% through 12:50 — the same 10-min bucket in which order-service's error rate rose.
  - `F14` ✔ exchange-gateway p99 latency was ~128–131ms through 10:50, then jumped to avg 4625.9ms (max 5287.98) in the 11:00 bucket and ~4900–5200ms thereafter — a ~40x rise coincident with order-service's latency jump.
**Assumptions**
  - A shared-datastore/broker change or failure occurred despite no config/deployment evidence
  - The observed error signature (exchange-gateway 504) would coexist with or mask a DB-path failure

### H3: pricing-engine cache.ttl_seconds config change (09:20Z) degrades a dependency chain reaching order-service
The pricing-engine cache.ttl_seconds 5->4 change (config-bot, 09:20Z) reduced quote caching, pushing extra load onto market-data-service and/or redis-cache, and the resulting slower quote path contributed to order-service timeouts.

**Causal chain**
1. pricing-engine cache.ttl_seconds reduced 5 -> 4 at 09:20Z by config-bot [F2]
2. Shorter TTL increases cache misses and downstream load on pricing-engine -> market-data-service / redis-cache (assumed; no evidence of load shift observed) *(assumed)*
3. order-service's pricing-engine dependency slows, contributing to submit timeouts *(assumed)*
4. Reported symptom: ~30% order submit failure rate [F7]

**Supporting evidence**
  - `F2` ✔ The only config change in 06:00Z-12:59Z is pricing-engine cache.ttl_seconds changed 5 -> 4 at 2026-03-17T09:20:00Z by config-bot ('Shorter quote cache TTL') — 88 minutes BEFORE the order-service deploy, and on a dependency of order-service (order-service -> pricing-engine, http).
**Contradicting evidence**
  - `F18` ✔ pricing-engine p99 latency stayed flat across the onset (~59–62ms, avg 60.71 in 10:50 and 62.18 in 11:00), so the pricing-engine cache.ttl_seconds config change at 09:20Z produced no measurable latency degradation at this granularity.
  - `F2` ✔ The only config change in 06:00Z-12:59Z is pricing-engine cache.ttl_seconds changed 5 -> 4 at 2026-03-17T09:20:00Z by config-bot ('Shorter quote cache TTL') — 88 minutes BEFORE the order-service deploy, and on a dependency of order-service (order-service -> pricing-engine, http).
  - `F7` ✔ order-service error_rate sat at ~0.2% (avg 0.13–0.25) from 09:00 through the 10:58 bucket, then jumped to avg 21.04% in the 11:01 bucket and ~30–32% thereafter through 12:58.
  - `F8` ✔ order-service p99 latency was ~150ms (146–166ms max) until the 10:50 bucket, then rose to avg 4285.85ms in the 11:00 bucket and ~5200–5400ms afterward — a ~35x latency jump coincident with the error onset.
  - `F9` ✔ All 349 order-service ERROR logs share one signature: 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)', first_seen 2026-03-17T11:02:45Z and continuing to 12:58:54Z — there are no stack traces or code-exception signatures.
  - `F10` ✔ exchange-gateway shows upstream timeouts to host=api.exchange-partner.example (ops placeOrder AND quoteSnapshot) starting 2026-03-17T11:01:25Z — ~80s BEFORE order-service's first 504 — plus 503 'Service Unavailable' from exchange-api and a circuit breaker opening at 11:03:26 ('failure ratio 0.47 over 60s').
**Assumptions**
  - The 4-second vs 5-second TTL difference produces material downstream pressure
  - order-service's failing path involves pricing-engine

## Facts (verified observations)
- The only deployment across all services in 06:00Z-12:59Z was order-service v2.15.2 (dep-5333) at 2026-03-17T10:48:00Z by a.khan, 'Add idempotency keys to order submit', status 'rolled_back' at 2026-03-17T11:16:00Z (28-minute exposure). [F1]
- The only config change in the window was pricing-engine cache.ttl_seconds 5->4 at 09:20:00Z by config-bot — 88 minutes before the order-service deploy — but pricing-engine p99 latency stayed flat across onset (~59-62ms), so it produced no measurable latency degradation at this granularity. [F2, F18]
- No deployments occurred for pricing-engine, exchange-gateway, api-gateway or market-data-service in 06:00Z-12:59Z. [F3]
- order-service's dependency graph includes shared platform components ledger-db, redis-cache and kafka reachable by other services, and exchange-gateway depends on the external exchange-api (has_telemetry=false). [F4]
- order-service error_rate was ~0.2% through the 10:58 bucket, then jumped to avg 21.04% in the 11:01 bucket and ~30-32% thereafter through 12:58. [F7]
- order-service p99 latency rose from ~150ms (until the 10:50 bucket) to avg 4285.85ms in the 11:00 bucket and ~5200-5400ms afterward (~35x). [F8]
- All 349 order-service ERROR logs share one signature, 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)', first_seen 2026-03-17T11:02:45Z and continuing to 12:58:54Z, with no stack traces or code-exception signatures. [F9]
- exchange-gateway shows upstream timeouts to host=api.exchange-partner.example (placeOrder AND quoteSnapshot) starting 2026-03-17T11:01:25Z — ~80s before order-service's first 504 — plus 503 Service Unavailable from exchange-api and a circuit breaker opening at 11:03:26 (failure ratio 0.47 over 60s). [F10]
- exchange-gateway error_rate jumped from ~0.2% (through the 10:50 bucket) to avg 39.33% in the 11:00 bucket and 42-45% thereafter, and its p99 latency rose from ~128-131ms to ~4625.9ms avg / ~4900-5200ms — coincident with order-service's error and latency rises. [F13, F14]
- order-service CPU showed no saturation (avg 39.9% in the 10:50 bucket, 39.8% in the 11:00 bucket, max 45.44 across the window). [F11, F15]
- order-service memory_pct was flat across onset (avg 59.96% at 10:50 -> 60.9% at 11:00, range 52.04-67.93), with no memory growth or pressure. [F16]
- order-service request_rate was unchanged across onset (~224.92 req/s at 10:50, 229.3 at 11:00, 231.4 at 12:50), so the failure is not driven by a traffic surge. [F17]
- Historical precedent HIST-103 (2025-10-21, order-service + exchange-gateway, '504 on order submit' / 'upstream timeouts', category dependency_failure, external exchange API outage, match_score 4) is the highest-scoring match, but matches are explicitly leads only and not evidence about the current incident. [F5]

## Assumptions (not verified)
- The external partner's own degradation at ~11:01Z is real and independent of order-service v2.15.2; this cannot be proven because api.exchange-partner.example has no telemetry (has_telemetry=false).
- The ordering claim rests on log timestamps (exchange-gateway upstream timeout 11:01:25Z vs order-service first 504 11:02:45Z), not on independently verified metric buckets; onset is only resolvable to the 10-minute bucket level.
- The 13-minute gap between the 10:48Z deploy and ~11:01Z onset is treated as coincidence, which is an assumption, not a verified fact — the deploy timestamp was never directly observed in logs/metrics and no deploy event was queried.
- Shared components ledger-db/redis-cache/kafka are assumed not to have been the co-factor, based on the absence of a config/deployment change for them in the platform-wide sweep, but the sweep's coverage of datastore-level changes is itself unverified.
- The platform-wide unscoped config-change sweep at 09:20Z and the all-services deployment sweep capture all relevant changes in 06:00Z-12:59Z, so the per-service calls that returned budget_exhausted were redundant.
- (demoted from facts: no verified observation backs it) The failure pattern is a downstream dependency timeout fronted by an external partner: order-service only reports 504 to exchange-gateway, while exchange-gateway reports connect timeouts/503 from api.exchange-partner.example; no order-service-internal code error or resource-saturation metric accompanies the onset.

## Timeline
- `2026-03-17T09:20:00Z` pricing-engine cache.ttl_seconds changed 5 -> 4 by config-bot ('Shorter quote cache TTL') — before order-service onset and deploy; pricing-engine latency later shows no measurable impact. [F2, F18]
- `2026-03-17T10:48:00Z` order-service v2.15.2 (dep-5333, 'Add idempotency keys to order submit') deployed by a.khan — the only deployment in the window, beginning a 28-minute exposure until rollback. [F1, F6]
- `2026-03-17T11:00:00Z` order-service p99 latency jumps to avg 4285.85ms; exchange-gateway error_rate jumps to avg 39.33% and p99 to ~4625.9ms, coincident with order-service onset. [F8, F13, F14]
- `2026-03-17T11:01:00Z` order-service error_rate jumps from ~0.2% to avg 21.04% in the 11:01 bucket and ~30-32% thereafter. [F7]
- `2026-03-17T11:01:25Z` exchange-gateway begins showing upstream timeouts to host=api.exchange-partner.example (placeOrder AND quoteSnapshot), ~80s before order-service's first 504. [F10]
- `2026-03-17T11:02:45Z` First order-service ERROR log, 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)' — the sole error signature, continuing to 12:58:54Z. [F9]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker opens ('failure ratio 0.47 over 60s') while reporting 503 Service Unavailable from exchange-api. [F10]
- `2026-03-17T11:16:00Z` order-service v2.15.2 rolled back (rolled_back_at), ending the 28-minute deployment exposure window. [F1]
- `2026-03-17T12:58:00Z` order-service error_rate remains ~30-32% through the 12:58 bucket; order-service CPU, memory_pct and request_rate remain flat, showing no resource saturation or traffic surge. [F7, F11, F15, F16, F17]

## Recommended actions
- **[immediate]** Verify whether the 11:16Z rollback of order-service v2.15.2 (dep-5333) actually reduced the order-submit failure rate; if the ~30% failure rate persists after rollback, H1 (external partner failure) is strongly favored and the deploy is exonerated. -- The deploy fell in the failure window (F1) but the error signature is purely an exchange-gateway dependency timeout (F9, F12); rollback outcome is the cleanest discriminator between H1 and H2 and should govern whether further release work is warranted. [F1, F9, F12]
- **[immediate]** Have exchange-gateway on-call confirm the api.exchange-partner.example upstream timeouts/503s directly (exchange-gateway log-observed at 11:01:25Z, circuit breaker at 11:03:26Z) and, if confirmed, engage the partner and apply exchange-gateway-side mitigations (extend the upstream timeout budget / keep the circuit breaker open / serve 503 with backoff) — no change to order-service code. -- F10 and F14 place the fault at exchange-gateway's upstream call to the external partner with 503s and a breaker trip; this is the strongest evidence-backed mitigation and avoids masking or compounding the issue with an order-service change. [F10, F13, F14]
- **[short_term]** If the failure rate dropped only after rollback and the partner reports no issue, re-examine order-service v2.15.2's submit path (and its downstream timeout/retry handling) before re-releasing; specifically confirm whether the deploy event precedes or follows onset, since deploy/onset ordering was never directly observed. -- H2 was not excluded by direct data (deploy/onset ordering is unconfirmed) even though it is contradicted by the dependency-timeout signature (F9, F10, F12, F13); a rollback-correlated recovery would raise H2 and warrant a code-level review. [F1, F9, F10]
- **[short_term]** Add monitoring on the exchange-gateway -> exchange-api upstream path (upstream_error_rate, upstream_latency_ms) and, if contractually possible, request partner-side health telemetry, since api.exchange-partner.example currently has has_telemetry=false. -- The external dependency is unobservable today (F4), which is exactly why the top hypothesis cannot be confirmed; exchange-gateway error_rate/latency (F13, F14) provide only an indirect mirror of partner health. [F4, F13, F14]
- **[short_term]** In parallel, confirm no shared-platform co-factor by checking ledger-db connections_active, redis-cache and kafka metrics/config before acceptance of H1. -- ledger-db/redis-cache/kafka are shared and reachable by other services (F4) and datastore deployments/config changes are not exposed by the sweeps; H4 (0.05) is low but not formally closed and its discriminating metrics were never retrieved (budget_exhausted). [F4]
- **[long_term]** Introduce dependency-timeout alerting that fires on exchange-gateway upstream errors/breaker trips before they cascade into order-service 504s, plus a documented fallback/degraded-mode for order submit when the exchange partner is unavailable. -- exchange-gateway upstream failures at 11:01:25Z propagated to ~30% order failures and lasted to 12:58Z (F10, F7); catching this at the gateway layer would shorten blast radius without touching order-service. [F10, F7, F12]
- **[long_term]** Add synthetic or periodic active health probes against the external partner's placeOrder/quoteSnapshot endpoints with alerting, so partner degradation is detected at the source rather than inferred from downstream 504s. -- With has_telemetry=false on the external endpoint (F4), every prior occurrence of this symptom pair (HIST-103) can only be diagnosed indirectly; active probing makes the dependency observable. [F4, F5]

## Data gaps
- api.exchange-partner.example is an external dependency with has_telemetry=false, so its own state at ~11:01Z cannot be observed directly; this is the central gap limiting the conclusion and prevents confirming H1 as the root cause.
- No direct observation of the v2.15.2 deploy event in logs/metrics was made (no deploy event queried), so whether onset preceded or postdated the release is unconfirmed — code-change causality is unestablished.
- order-service db_pool_in_use, exchange-gateway upstream_error_rate/upstream_latency_ms, and ledger-db connections_active could not be retrieved (budget_exhausted), so connection-pool vs dependency-timeout discrimination rests on indirect evidence.
- No telemetry or change history for the vendor exchange endpoint, so an upstream exchange-side failure cannot be confirmed as independent of the order-service deploy.
- Shared components ledger-db, redis-cache and kafka could not be verified for config/flag/persistence or infrastructure-level changes, since datastore deployments are not exposed by get_deployments.
- Onset is only resolvable to the 10-minute metric bucket level, so the exact sub-bucket ordering of the exchange-gateway vs order-service rise could not be refined.
- No baseline comparison before 06:00Z was performed, so change cadence before the window is unknown.
- Blast radius across sibling/dependent services (e.g. payment-service) is unverified beyond order-service and exchange-gateway.

## What would change this conclusion
- Direct confirmation that api.exchange-partner.example experienced an independent degradation at ~11:01Z (partner status page, partner telemetry, or an active probe) would upgrade the conclusion toward root_cause_identified for H1.
- If the ~30% order-submit failure rate persists unchanged after the 11:16Z rollback, H1 (external partner failure) is strongly favored and H2 (bad deployment) would be effectively excluded.
- If the failure rate dropped only after rollback, or if order-service logs showed internal code exceptions/stack traces, H2 (v2.15.2 regression) would become the leading hypothesis.
- Evidence that order-service actually began failing before exchange-gateway's 11:01:25Z upstream timeout would undermine the H1 timing chain and re-open H2/H4.
- Discovery of a config/flag/infrastructure change on a shared component (ledger-db, redis-cache, or kafka) around 11:00Z would revive H4 (resource_exhaustion).
- A measurable latency or error impact traceable to the pricing-engine cache.ttl_seconds change at 09:20Z would revive H3, though F18 currently shows no measurable degradation at this granularity.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- reporter wrote outcome=inconclusive; overridden to root_cause_identified by rule (top hypothesis is above threshold and clearly ahead)
- fact demoted to assumption: The failure pattern is a downstream dependency timeout fronted by an external pa

## Investigation stats
- latency 126.71s, LLM calls 13, tokens 91690 (in 74409 / out 17281)
- tool calls 24 (3 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
