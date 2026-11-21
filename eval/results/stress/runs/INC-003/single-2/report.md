# Incident investigation report: INC-003
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order submit failures (~31% error rate, 504s) are caused by degradation of the external exchange partner API (api.exchange-partner.example), surfaced through exchange-gateway. exchange-gateway shows upstream connect timeouts and 503s from exchange-api starting 11:01, driving 40-48% upstream error rate and ~9s upstream latency. order-service merely propagates these as "exchange-gateway call failed: 504 Gateway Timeout". The onset aligns with exchange-gateway's upstream failures, NOT with the order-service v2.15.2 deploy (10:48) — and critically the deploy was already rolled back at 11:16:38 yet errors continue unchanged through 12:58. order-service CPU/memory/db-pool are flat with zero restarts, ruling out resource exhaustion; pricing-engine is healthy, ruling out the config change at 09:20. Root cause: dependency_failure at the external exchange provider.

## Probable root cause
**External exchange provider degradation (dependency_failure)** -- dependency_failure in `exchange-api` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange provider degradation (dependency_failure) | dependency_failure | exchange-api | - | 0.86 (high) | active |
| 2 | H2: order-service v2.15.2 release introduced the faults | bad_deployment | order-service | dep-5333 | 0.06 (low) | rejected |
| 3 | H3: pricing-engine cache TTL config change caused the failures | config_change | pricing-engine | cfg-2290 | 0.04 (low) | rejected |
| 4 | H4: order-service resource exhaustion (CPU/memory/DB pool) | resource_exhaustion | order-service | - | 0.03 (low) | rejected |

### H1: External exchange provider degradation (dependency_failure)
The external exchange partner API (api.exchange-partner.example, fronted by exchange-api) began timing out and returning 503s at ~11:01. exchange-gateway's upstream calls hit connect timeouts (~9s upstream latency) and its circuit breaker opened, propagating 504s to order-service, which reports ~31% order submit failures.

**Causal chain**
1. External exchange partner degrades; api.exchange-partner.example connect timeouts and 503s appear at 11:01 [F4, F13]
2. exchange-gateway upstream_error_rate rises to ~46% and upstream_latency to ~9s; circuit breaker opens [F5, F6]
3. exchange-gateway returns 504 to order-service [F3]
4. order-service order submit error_rate reaches ~31% and p99 ~5.3s [F1, F2]

**Supporting evidence**
  - `F4` ✔ exchange-gateway logs show upstream connect timeouts to api.exchange-partner.example and 503 Service Unavailable from exchange-api, first at 11:01:25.
  - `F5` ✔ exchange-gateway error_rate rose from ~0.2% to policies ~44% and upstream_error_rate to ~46% at the 11:00 bucket, and upstream_latency_ms rose from ~120ms to ~9200ms.
  - `F6` ✔ exchange-gateway circuit breaker opened for exchange-api with failure ratio 0.47 over 60s at 11:03:26.
  - `F3` ✔ All 349 order-service ERROR logs are the single pattern 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)', first seen 11:02:45.
  - `F1` ✔ order-service error_rate jumped from ~0.2% to ~31% at the 11:00 bucket and stayed elevated through 12:45 (last 30.39).
  - `F13` ✔ exchange-api is an external node (owner 'vendor') with has_telemetry false; exchange-gateway depends on it.
  - `F10` ✔ exchange-gateway had zero restarts and flat CPU ~30% during the incident.
**Contradicting evidence**
  - `F7` ✔ The order-service deploy dep-5333 (v2.15.2 'Add idempotency keys to order submit') was deployed at 10:48 and rolled back at 11:16.
  - `F8` ✔ The rollback of dep-5333 completed at 11:16:38 ('rollback of dep-5333 complete, previous version restored'), yet errors continued unchanged (315+ errors between 11:20 and 12:58).
**Assumptions**
  - exchange-api is a thin proxy to the external partner and the fault is on the partner side, not on our edge

### H2: order-service v2.15.2 release introduced the faults
The order-service v2.15.2 deploy (idempotency keys in order submit) at 10:48 caused the 30% submit failure rate.

**Causal chain**
(none given)

**Supporting evidence**
  - `F7` ✔ The order-service deploy dep-5333 (v2.15.2 'Add idempotency keys to order submit') was deployed at 10:48 and rolled back at 11:16.
**Contradicting evidence**
  - `F8` ✔ The rollback of dep-5333 completed at 11:16:38 ('rollback of dep-5333 complete, previous version restored'), yet errors continued unchanged (315+ errors between 11:20 and 12:58).
  - `F3` ✔ All 349 order-service ERROR logs are the single pattern 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)', first seen 11:02:45.
  - `F4` ✔ exchange-gateway logs show upstream connect timeouts to api.exchange-partner.example and 503 Service Unavailable from exchange-api, first at 11:01:25.
  - `F10` ✔ exchange-gateway had zero restarts and flat CPU ~30% during the incident.
**Rejected because:** Deploy was rolled back at 11:16:38 but errors continued unchanged through 12:58; the error signature ('exchange-gateway call failed: 504') points to a downstream failure, not order-service code; exchange-gateway shows an independent upstream failure starting 11:01 with no deploy of its own.

### H3: pricing-engine cache TTL config change caused the failures
The cfg-2290 cache.ttl_seconds 5->4 change at 09:20 degraded pricing and cascaded to orders.

**Causal chain**
(none given)

**Supporting evidence**
  - `F12` ✔ The only config change in the window was pricing-engine cache.ttl_seconds 5->4 at 09:20, 1h40m before onset.
**Contradicting evidence**
  - `F11` ✔ pricing-engine error_rate stayed flat ~0.2% throughout — unaffected by the incident.
  - `F1` ✔ order-service error_rate jumped from ~0.2% to ~31% at the 11:00 bucket and stayed elevated through 12:45 (last 30.39).
**Rejected because:** pricing-engine error_rate is flat ~0.2% and the change predates onset by 1h40m; order errors are all exchange-gateway 504s, unrelated to pricing.

### H4: order-service resource exhaustion (CPU/memory/DB pool)
order-service exhausted CPU, memory or DB connections, causing latency and failures.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ order-service CPU stayed ~40% and db_pool_in_use stayed ~14 throughout the incident; request_rate stayed flat ~230 req/s.
  - `F2` ✔ order-service latency_p99_ms jumped from ~150ms to ~5300ms at the 11:00 bucket and remained high.
**Rejected because:** CPU ~40%, db_pool ~14 and request_rate flat throughout; no restarts. Slow latency is explained by the downstream 504 wait.

## Facts (verified observations)
- order-service error_rate rose from ~0.2% to ~31% at the 11:00 bucket and remained elevated through 12:45. [F1]
- order-service latency_p99 rose from ~150ms to ~5300ms at 11:00. [F2]
- All order-service ERROR logs are 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'. [F3]
- exchange-gateway logged upstream connect timeouts to api.exchange-partner.example and 503s from exchange-api starting 11:01:25. [F4]
- exchange-gateway error_rate ~44%, upstream_error_rate ~46%, upstream_latency ~9.2s during the incident. [F5]
- exchange-gateway circuit breaker opened for exchange-api at 11:03:26. [F6]
- order-service v2.15.2 (dep-5333) was deployed 10:48 and rolled back at 11:16. [F7]
- Rollback completed at 11:16:38 but errors continued unchanged through 12:58. [F8]
- order-service CPU (~40%), db_pool (~14) and request_rate (~230/s) were flat during the incident. [F9]
- exchange-gateway had zero restarts and flat CPU (~30%). [F10]
- pricing-engine error_rate stayed flat ~0.2%. [F11]
- Only config change was pricing-engine cache TTL 5->4 at 09:20. [F12]
- exchange-api is an external vendor node without telemetry. [F13]
- No exchange-gateway deployments in the window. [F14]
- api-gateway logged no WARN/ERROR entries during the incident. [F15]

## Assumptions (not verified)
- exchange-api (external vendor node without telemetry) is a proxy for api.exchange-partner.example, so the outage is on the partner side rather than in our own edge.
- No other relevant change occurred outside the queried windows.

## Timeline
- `2026-03-17T09:20:00Z` pricing-engine cache.ttl_seconds changed 5->4 (cfg-2290) [F12]
- `2026-03-17T10:48:00Z` order-service v2.15.2 (dep-5333) deployed [F7]
- `2026-03-17T11:01:25Z` exchange-gateway first upstream connect timeout to api.exchange-partner.example [F4]
- `2026-03-17T11:02:20Z` exchange-api returns 503 Service Unavailable [F4]
- `2026-03-17T11:02:45Z` order-service first 504 'exchange-gateway call failed' error [F3]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker opens for exchange-api [F6]
- `2026-03-17T11:10:00Z` Incident reported (INC-003) []
- `2026-03-17T11:16:38Z` order-service rollback of dep-5333 complete; errors persist unchanged [F8]
- `2026-03-17T12:58:54Z` Latest order-service 504 error; incident ongoing [F8]

## Recommended actions
- **[immediate]** Stop treating the order-service rollback as the fix; engage the exchange partner / vendor to restore api.exchange-partner.example, and fail over to a secondary exchange route if available. -- Fault originates at the external provider; rollback did not help and errors persist. [F4, F8]
- **[immediate]** Verify whether exchange-api is a proxy to the partner vs. our own component, and check its health/credentials to confirm the fault boundary. -- exchange-api has no telemetry, so the exact boundary between our gateway and the partner is unconfirmed. [F13]
- **[short_term]** Tune exchange-gateway circuit breaker / add fast-fail and secondary-route failover so external partition degrades gracefully instead of 30% order failures. -- Circuit breaker opened but requests still propagated 504s to orders for ~2h. [F6, F1]
- **[long_term]** Add monitoring/telemetry on exchange-api and partner connectivity to detect external degradation at onset. -- The external node has no telemetry, delaying detection. [F13]

## Data gaps
- exchange-api/external provider has no telemetry (has_telemetry false), so the partner-side health is inferred only from exchange-gateway logs.
- exchange-gateway request_rate query timed out (tc-019), so its own request volume during the incident is unconfirmed.
- No vendor status page or partner-side incident record was available to independently confirm the external outage.

## Investigation stats
- latency 64.86s, LLM calls 6, tokens 72923 (in 65211 / out 7712)
- tool calls 23 (1 failed, 0 partial, 6 recovered by retry), findings 15/15 verified
