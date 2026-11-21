# Incident investigation report: INC-003
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order-submit failures (~31% error rate, 504s) are caused by the EXTERNAL exchange partner API (api.exchange-partner.example) degrading: exchange-gateway logs upstream connect timeouts after 5000ms and 503 Service Unavailable from the partner starting 11:01:25, and upstream_latency jumped from ~118ms to ~8500ms. order-service simply propagates those 504/503s on placeOrder. Root cause category is dependency_failure originating at the external exchange-api. The order-service v2.15.2 release (idempotency keys, deployed 10:48) is coincidental timing: its rollback at 11:16 did NOT restore service (error rate still ~31% through 12:45), order-service CPU/memory/restarts are normal, and no idempotency-related errors appear in any logs. The incident was still ongoing at 12:58 (latest data). Note: the reported "rolling back order-service" action will not fix this; a secondary exchange route / partner failover is needed.

## Probable root cause
**External exchange partner API degrading (connect timeouts / 503)** -- dependency_failure in `exchange-api` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange partner API degrading (connect timeouts / 503) | dependency_failure | exchange-api | - | 0.85 (high) | active |
| 2 | H2: order-service v2.15.2 release introduced the fault | bad_deployment | order-service | dep-5333 | 0.08 (low) | rejected |
| 3 | H4: exchange-gateway resource exhaustion / saturation | resource_exhaustion | exchange-gateway | - | 0.05 (low) | rejected |
| 4 | H3: pricing-engine cache TTL config change caused failures | config_change | pricing-engine | cfg-2290 | 0.03 (low) | rejected |

### H1: External exchange partner API degrading (connect timeouts / 503)
The external exchange provider api.exchange-partner.example began timing out connections (5s) and returning 503 Service Unavailable around 11:01. exchange-gateway's calls to it fail, producing 504s that order-service surfaces on placeOrder (~31% error rate). exchange-gateway and order-service are victims, not causes.

**Causal chain**
1. exchange-api (external partner) starts refusing/timing out connections [F3, F10]
2. exchange-gateway upstream_latency rises to ~8.5s and upstream_error_rate to ~42-46% [F4, F3]
3. exchange-gateway returns 504/503 to order-service on placeOrder [F3]
4. order-service propagates 504s -> ~31% error rate and p99 ~5.3s [F1, F2, F5]

**Supporting evidence**
  - `F1` ✔ order-service error_rate jumped from a ~0.2% baseline to 26.73% at the 11:00-11:15 bucket and stayed ~31% (still 30.99 at 12:45).
  - `F2` ✔ All 174 order-service ERROR entries in 10:30-12:00 match one pattern: 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)' (504).
  - `F3` ✔ exchange-gateway logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' first seen 11:01:25, plus 'exchange-api returned 503 Service Unavailable' and a circuit breaker opening at 11:03:26.
  - `F4` ✔ exchange-gateway upstream_latency_ms rose from ~118ms baseline to 8550.82ms at the 11:00 bucket and remained ~9200-9300ms through 12:45.
  - `F5` ✔ order-service latency_p99_ms rose from ~150ms to ~5300ms at the 11:00 bucket and stayed elevated (5301.2 at 12:45), consistent with the 5000ms upstream timeout.
  - `F10` ✔ exchange-gateway was still logging upstream connect timeouts to api.exchange-partner.example at 12:58, i.e. the incident was ongoing at latest data.
  - `F11` ✔ The fault originates at the external exchange provider (exchange-api); exchange-gateway and order-service are downstream victims propagating the partner's timeouts/503s.
  - `F9` ✔ search_historical_incidents returned HIST-103 'Order failures during exchange partner outage' with root_cause 'External exchange API outage on the partner side', category dependency_failure.
**Contradicting evidence**
  - (none)
**Assumptions**
  - exchange-api has no telemetry (vendor), so the fault can only be inferred from gateway-side symptoms
  - the external partner's degradation is the true origin rather than a network path shared only by our gateway

### H2: order-service v2.15.2 release introduced the fault
The idempotency-keys release v2.15.2 broke order submit, causing ~31% failures.

**Causal chain**
1. dep-5333 changes order submit [F7]
2. submit handler fails *(assumed)*
3. rollback should restore service but did not [F7, F1]

**Supporting evidence**
  - `F7` ✔ order-service v2.15.2 (summary 'Add idempotency keys to order submit') deployed 10:48 and rolled back at 11:16, but error_rate remained ~31% after rollback.
**Contradicting evidence**
  - `F2` ✔ All 174 order-service ERROR entries in 10:30-12:00 match one pattern: 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)' (504).
  - `F3` ✔ exchange-gateway logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' first seen 11:01:25, plus 'exchange-api returned 503 Service Unavailable' and a circuit breaker opening at 11:03:26.
  - `F7` ✔ order-service v2.15.2 (summary 'Add idempotency keys to order submit') deployed 10:48 and rolled back at 11:16, but error_rate remained ~31% after rollback.
  - `F8` ✔ No order-service logs matched 'idempot' in 10:40-11:20, and no exchange-gateway deployments exist in 06:00-12:59.
  - `F1` ✔ order-service error_rate jumped from a ~0.2% baseline to 26.73% at the 11:00-11:15 bucket and stayed ~31% (still 30.99 at 12:45).
**Rejected because:** Rolling back v2.15.2 at 11:16 did not restore service (error rate stayed ~31% through 12:45). All errors are exchange-gateway 504s, unrelated to idempotency; no 'idempot' log lines appear; order-service CPU/memory/restarts are normal.

### H4: exchange-gateway resource exhaustion / saturation
exchange-gateway ran out of CPU/connections and timed out its upstream calls.

**Causal chain**
1. gateway saturated *(assumed)*
2. metrics show flat cpu/request rate [F6]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ exchange-gateway request_rate stayed flat (~270-279 req/s) and cpu_pct flat (~30%) across the window; order-service cpu_pct (~40%), memory_pct (~60%) and db_pool_in_use (~14 conns) were flat, and restarts stayed 0.
  - `F3` ✔ exchange-gateway logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' first seen 11:01:25, plus 'exchange-api returned 503 Service Unavailable' and a circuit breaker opening at 11:03:26.
  - `F4` ✔ exchange-gateway upstream_latency_ms rose from ~118ms baseline to 8550.82ms at the 11:00 bucket and remained ~9200-9300ms through 12:45.
**Rejected because:** exchange-gateway cpu_pct flat ~30% and request_rate flat ~273 req/s; the timeouts are on the outbound call to the external partner, not local resource pressure.

### H3: pricing-engine cache TTL config change caused failures
cfg-2290 (cache.ttl_seconds 5->4) degraded pricing-engine and cascaded to order-service.

**Causal chain**
1. cfg-2290 changed pricing cache TTL *(assumed)*
2. no pricing-engine error/latency impact observed *(assumed)*

**Supporting evidence**
  - `F1` ✔ order-service error_rate jumped from a ~0.2% baseline to 26.73% at the 11:00-11:15 bucket and stayed ~31% (still 30.99 at 12:45).
**Contradicting evidence**
  - `F2` ✔ All 174 order-service ERROR entries in 10:30-12:00 match one pattern: 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)' (504).
  - `F3` ✔ exchange-gateway logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' first seen 11:01:25, plus 'exchange-api returned 503 Service Unavailable' and a circuit breaker opening at 11:03:26.
**Rejected because:** pricing-engine error_rate stayed ~0.2% and latency_p99 ~60ms throughout, and order-service errors are all exchange-gateway 504s, not pricing-engine calls.

## Facts (verified observations)
- order-service error_rate rose from ~0.2% to ~31% at the 11:00 bucket and remained ~31% through 12:45. [F1]
- order-service error logs are exclusively 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' (174 matches). [F2]
- exchange-gateway logged upstream connect timeouts after 5000ms to host=api.exchange-partner.example and 503 Service Unavailable from exchange-api, beginning 11:01:25. [F3]
- exchange-gateway upstream_latency_ms rose from ~118ms to ~8500-9300ms at the 11:00 bucket and stayed elevated through 12:45. [F4]
- order-service latency_p99_ms rose from ~150ms to ~5300ms coincident with the failures. [F5]
- exchange-gateway request_rate, cpu_pct (~30%) and order-service cpu_pct (~40%), memory_pct (~60%) all remained flat, and order-service restarts stayed 0. [F6]
- order-service v2.15.2 (idempotency keys) was deployed 10:48 and rolled back 11:16, but the error rate stayed ~31% after the rollback. [F7]
- No logs matching 'idempot' exist in order-service during 10:40-11:20, and no exchange-gateway deployments occurred in the window. [F8]
- Historical incident HIST-103 was an external exchange partner API outage producing 504s on order submit via exchange-gateway (category dependency_failure). [F9]
- The exchange partner failures were still occurring at 12:58 (latest data). [F10]

## Assumptions (not verified)
- exchange-api (external partner) has no telemetry available, so its failure is inferred from exchange-gateway observations rather than directly measured.
- order-service deploy timing (10:48) is coincidental to the 11:01 exchange-api onset; we cannot fully exclude a shared network-path cause between our gateway and the partner.
- Latency/error metric buckets are 15 min, so exact onset is bounded to ~11:00-11:01.

## Timeline
- `2026-03-17T09:20:00Z` Config change cfg-2290: pricing-engine cache.ttl_seconds 5->4 (no observed impact). []
- `2026-03-17T10:48:00Z` order-service v2.15.2 (idempotency keys) deployed. [F7]
- `2026-03-17T11:01:25Z` First exchange-gateway upstream connect timeout to api.exchange-partner.example (placeOrder). [F3]
- `2026-03-17T11:02:20Z` exchange-api begins returning 503 Service Unavailable. [F3]
- `2026-03-17T11:02:45Z` First order-service ERROR: exchange-gateway call failed 504 on placeOrder. [F2]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker OPEN for exchange-api. [F3]
- `2026-03-17T11:10:00Z` Incident reported (order error ~30%). [F1]
- `2026-03-17T11:16:00Z` order-service v2.15.2 rolled back - service does NOT recover. [F7]
- `2026-03-17T12:45:00Z` order-service error_rate still ~31%; exchange-gateway upstream_latency still ~9.2s. [F1, F4]
- `2026-03-17T12:58:30Z` exchange-gateway still logging upstream connect timeouts (incident ongoing). [F10]

## Recommended actions
- **[immediate]** Stop / do not rely on the order-service rollback to fix this - the rollback already occurred (11:16) without recovery; restore v2.15.2 if it is otherwise fine. -- The error rate stayed ~31% after rollback and the fault is external, so the rollback does not address the cause. [F7, F1]
- **[immediate]** Engage the exchange partner and/or fail over to the secondary exchange route; verify exchange-api health directly with the vendor. -- All failures trace to api.exchange-partner.example connect timeouts/503s; the fix is on the vendor side or via failover. [F3, F4, F10]
- **[short_term]** Tune exchange-gateway resilience: shorter connect timeout, keep circuit breaker open with graceful degradation, and queue/retry idempotent placeOrder calls for when the partner recovers. -- 5000ms timeouts propagate as order-submit latency/504s (~5.3s p99); fast-fail would reduce blast radius. [F3, F5]
- **[long_term]** Add partner-side synthetics/alerting on exchange-api upstream_error_rate and latency so vendor degradation is detected before it manifests as order failures. -- The vendor has no telemetry (exchange-api external, has_telemetry false) and detection lagged the 11:01 onset. [F3]

## Data gaps
- No direct telemetry for exchange-api (external vendor, has_telemetry: false) - vendor-side cause (their outage vs. our network egress path) cannot be distinguished from our data.
- order-service log query for 'idempot' returned 0 in 10:40-11:20, but this narrow window does not cover the full deploy duration; idempotency-specific failures can't be fully excluded, though all observed order errors are exchange-gateway 504s.
- 15-minute metric buckets bound onset precision to roughly 11:00-11:01; exact second of exchange-api degradation unknown.
- No packets/network telemetry to confirm whether the failure is the partner's service or connectivity between our egress and the partner.

## Investigation stats
- latency 65.3s, LLM calls 6, tokens 78676 (in 70516 / out 8160)
- tool calls 24 (0 failed, 0 partial, 0 recovered by retry), findings 11/11 verified
