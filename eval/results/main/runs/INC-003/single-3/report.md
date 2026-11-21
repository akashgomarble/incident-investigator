# Incident investigation report: INC-003
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order submit failures (~31% error rate, 504s) are caused by the EXTERNAL exchange partner degrading, not by order-service v2.15.2. Onset is 11:01:25 (first "upstream connect timeout after 5000ms host=api.exchange-partner.example" in exchange-gateway), ~13 min AFTER the 10:48 deploy. exchange-gateway's upstream_latency_ms jumped 120ms→~9250ms and upstream_error_rate 0.3%→~46%; it returned 503s from exchange-api and its circuit breaker opened at 11:03:26. order-service's errors are all "exchange-gateway call failed: 504 Gateway Timeout (placeOrder)". order-service and exchange-gateway show no CPU/memory/restart/pool anomalies (CPU flat 40%/30%, db_pool flat 14, 0 restarts), ruling out resource/capacity causes. Critically, the v2.15.2 rollback at 11:16 did NOT restore the error rate (still ~31% at 12:59, 1h43m post-rollback), ruling out the deployment as root cause. Category: dependency_failure, originating at the external exchange-api.

## Probable root cause
**External exchange partner (exchange-api) outage degrades order submits** -- dependency_failure in `exchange-api` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange partner (exchange-api) outage degrades order submits | dependency_failure | exchange-api | - | 0.85 (high) | active |
| 2 | H2: bad_deployment: order-service v2.15.2 retry-safe submit handler overloads exchange path | bad_deployment | order-service | dep-5333 | 0.05 (low) | rejected |
| 3 | H3: Resource/capacity exhaustion in order-service or exchange-gateway | capacity_saturation | order-service | - | 0.03 (low) | rejected |

### H1: External exchange partner (exchange-api) outage degrades order submits
The external exchange provider api.exchange-partner.example / exchange-api began timing out and returning 503 at ~11:01. exchange-gateway's calls to it hit the 5s connect timeout, returning 504s to order-service's placeOrder path, which surfaced as ~31% order submit failures. The fault ORIGINATES outside our services at the third-party exchange API.

**Causal chain**
1. External exchange-api degrades: connect timeouts + 503 at ~11:01 [F5, F12]
2. exchange-gateway upstream_latency 120ms->9250ms and upstream_error_rate 0.3%->46% [F6]
3. exchange-gateway circuit breaker opens for exchange-api at 11:03:26 [F7]
4. order-service placeOrder gets 504 Gateway Timeout from exchange-gateway [F2]
5. order-service error rate jumps to ~31% and stays there [F1, F3]

**Supporting evidence**
  - `F5` ✔ exchange-gateway errors are 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (163 placeOrder + 146 quoteSnapshot) and 'exchange-api returned 503 Service Unavailable' (161); first at 11:01:25.
  - `F6` ✔ exchange-gateway upstream_latency_ms jumped from ~120ms to ~9250ms and upstream_error_rate from 0.3% to ~46%, measured on the call to the external partner.
  - `F7` ✔ exchange-gateway circuit breaker opened for exchange-api: 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26.
  - `F2` ✔ The only order-service error pattern is 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' (349 matches), first at 11:02:45.
  - `F1` ✔ order-service error rate was ~0.2% until the 10:49 bucket, then jumped to ~31% from the 11:02 bucket and remained ~30-31% through 12:59.
  - `F3` ✔ order-service latency_p99 jumped from ~150ms to ~5300ms at the 11:02 bucket, consistent with a 5s downstream timeout.
  - `F12` ✔ The dependency graph shows exchange-api is an external node (owner vendor, has_telemetry:false) reached via exchange-gateway, and order-service calls exchange-gateway over HTTP.
  - `F13` ✔ A prior incident HIST-103 (2025-10-21) matches: 'Order failures during exchange partner outage', services order-service+exchange-gateway, symptoms '504 on order submit', root cause 'External exchange API outage on the partner side', category dependency_failure.
  - `F8` ✔ exchange-gateway is internally healthy: 0 restarts, cpu_pct flat ~30%, memory_pct flat ~44%, request_rate flat ~271 req/s, with INFO 'health check ok' and 'connection pool stats: healthy'.
**Contradicting evidence**
  - (none)
**Assumptions**
  - exchange-api has no telemetry of its own (has_telemetry:false), so partner-side degradation is inferred from exchange-gateway's upstream metrics and error text.
  - No internal change to exchange-gateway or the network path to the partner occurred at 11:01.

### H2: bad_deployment: order-service v2.15.2 retry-safe submit handler overloads exchange path
Dep-5333 (10:48) added idempotency keys and a retry-safe submit handler; retries could hammer exchange-gateway and the partner, causing timeouts. REJECTED: the rollback at 11:16 did not recover the error rate (still ~31% at 12:59), exchange-gateway request_rate is flat (~271 req/s) with no load spike, and onset (11:01) is 13 min after the deploy while order-service's own CPU/pool are unremarkable.

**Causal chain**
1. deploy dep-5333 -> retry storm -> partner timeouts *(assumed)*

**Supporting evidence**
  - `F9` ✔ The order-service v2.15.2 deploy (dep-5333) occurred at 10:48:00 and was rolled back at 11:16:00.
**Contradicting evidence**
  - `F10` ✔ After the rollback at 11:16, order-service error rate stayed ~30-31% (11:28 30.95, 12:59 30.39) and exchange-gateway error_rate stayed ~44-47%, so the rollback did not restore service.
  - `F8` ✔ exchange-gateway is internally healthy: 0 restarts, cpu_pct flat ~30%, memory_pct flat ~44%, request_rate flat ~271 req/s, with INFO 'health check ok' and 'connection pool stats: healthy'.
  - `F1` ✔ order-service error rate was ~0.2% until the 10:49 bucket, then jumped to ~31% from the 11:02 bucket and remained ~30-31% through 12:59.
  - `F5` ✔ exchange-gateway errors are 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (163 placeOrder + 146 quoteSnapshot) and 'exchange-api returned 503 Service Unavailable' (161); first at 11:01:25.
**Rejected because:** Rollback at 11:16 did not restore service (error rate still ~31% at 12:59); exchange-gateway request_rate stayed flat ~271 req/s; errors are explicitly upstream partner timeouts/503s, not order-service-internal failures.

### H3: Resource/capacity exhaustion in order-service or exchange-gateway
CPU, memory, DB pool, or request load exhaustion caused the 504s. REJECTED: order-service cpu flat ~40% and db_pool_in_use flat ~14; exchange-gateway cpu flat ~30%, memory flat ~44%, 0 restarts, request_rate flat ~271 req/s.

**Causal chain**
1. resource exhaustion -> timeouts *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F4` ✔ order-service db_pool_in_use stayed flat (~14 connections) and cpu_pct stayed flat (~40%) throughout the incident, showing no resource exhaustion.
  - `F8` ✔ exchange-gateway is internally healthy: 0 restarts, cpu_pct flat ~30%, memory_pct flat ~44%, request_rate flat ~271 req/s, with INFO 'health check ok' and 'connection pool stats: healthy'.
**Rejected because:** All resource metrics on order-service and exchange-gateway are flat and well below saturation during the incident.

## Facts (verified observations)
- order-service error rate rose from ~0.2% to ~31% starting the 11:02 bucket and remained elevated through 12:59. [F1]
- All order-service errors are 504 Gateway Timeouts on exchange-gateway placeOrder calls. [F2]
- exchange-gateway logged connect timeouts and 503s against the external host api.exchange-partner.example starting 11:01:25. [F5, F12]
- exchange-gateway upstream latency/error rates to the partner jumped to ~9250ms / ~46%, and its circuit breaker opened at 11:03:26. [F6, F7]
- order-service and exchange-gateway resource metrics (CPU, memory, DB pool, restarts, request rate) stayed flat during the incident. [F4, F8]
- The order-service v2.15.2 deploy was rolled back at 11:16, yet the error rate was still ~31% at 12:59. [F9, F10]
- pricing-engine error rate stayed flat, so the 09:20 cache TTL change had no impact. [F11]

## Assumptions (not verified)
- The external exchange-api partner is genuinely degraded rather than the network path between us and it.
- No internal change to exchange-gateway or the network path to the partner occurred at 11:01.
- The 504s reported by order-service are the propagated exchange-gateway 504s, not an independent order-service fault.

## Timeline
- `2026-03-17T09:20:00Z` Config change cfg-2290: pricing-engine cache.ttl_seconds 5->4 (unrelated; pricing-engine error rate flat). [F11]
- `2026-03-17T10:48:00Z` order-service v2.15.2 (dep-5333) deployed. [F9]
- `2026-03-17T11:01:25Z` First exchange-gateway upstream connect timeout to api.exchange-partner.example (op=placeOrder). [F5]
- `2026-03-17T11:02:20Z` First 'exchange-api returned 503 Service Unavailable' in exchange-gateway. [F5]
- `2026-03-17T11:02:45Z` First order-service order submit 504 error. [F2]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker OPEN for exchange-api. [F7]
- `2026-03-17T11:10:00Z` Incident reported (INC-003). []
- `2026-03-17T11:16:00Z` order-service v2.15.2 rolled back. [F9]
- `2026-03-17T12:59:00Z` Error rate still ~31% (order-service) and ~47% (exchange-gateway) 1h43m after rollback; partner degradation ongoing. [F10]

## Recommended actions
- **[immediate]** Stop treating the rollback as the fix; the rollback is already done and did not recover service. Escalate to the exchange partner and ask for their incident status on api.exchange-partner.example. -- Error rate remained ~31% at 12:59, 1h43m after rollback, and the failing calls are upstream timeouts/503s from the external partner. [F10, F5, F6]
- **[immediate]** Fail over to a secondary exchange route / enable partner fallback if available. -- HIST-103 resolution was exactly this ('Failed over to secondary exchange route; partner fixed'). [F13]
- **[short_term]** Confirm the exchange-gateway circuit breaker and timeouts are limiting blast radius; tune 5s connect timeout/retry budget if partner is slow-but-alive. -- Circuit breaker opened at 11:03:26; connect timeouts at 5000ms drive the 504s. [F7, F5]
- **[long_term]** Add telemetry/alerting on exchange-api (currently has_telemetry:false) so partner degradation is detected as the origin, not the symptom. -- The external node has no telemetry, forcing inference from exchange-gateway metrics. [F12]

## Data gaps
- No telemetry from the external exchange-api (has_telemetry:false), so the partner-side cause is inferred from exchange-gateway's upstream metrics and error text rather than directly observed.
- market-data-service shows a rise to ~6.5% error rate at the same 11:02 bucket but produced 0 ERROR logs in the queried window; its role (if any) was not resolved and it does not appear on order-service's critical path.
- order-service and exchange-gateway logs earlier than the query windows were not exhaustively checked for an earlier onset; onset is bounded by the first observed errors at ~11:01.

## Investigation stats
- latency 51.17s, LLM calls 7, tokens 111602 (in 103023 / out 8579)
- tool calls 24 (0 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
