# Incident investigation report: INC-003
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order submit failures (30% error rate, 504s) started at 2026-03-17T11:01:25Z and persist unchanged through the latest data (12:59Z), i.e. ~1h42m AFTER the order-service v2.15.2 rollback at 11:16Z. The 504s are order-service calls to exchange-gateway (placeOrder), which in turn is timing out against its external upstream api.exchange-partner.example (connect timeouts at 5000ms; upstream latency p99 ~9.4s vs ~120ms baseline). exchange-gateway's own resources are healthy (CPU flat ~30%, request rate flat ~270 req/s), and pricing-engine (and its 09:20 cache-TTL config change) is entirely unaffected. The evidence points to the external exchange partner API degrading — a dependency_failure — not the order-service release. The reported rollback is a red herring. Outcome: root cause identified (dependency_failure at the external exchange provider), with the caveat that the vendor has no telemetry, so the partner-side fault can only be inferred from the gateway's upstream metrics and error messages.

## Probable root cause
**External exchange partner API degraded (dependency_failure)** -- dependency_failure in `exchange-api (external vendor) / exchange-gateway` (confidence 0.82, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange partner API degraded (dependency_failure) | dependency_failure | exchange-api (external vendor) / exchange-gateway | - | 0.82 (high) | active |
| 2 | H2: order-service v2.15.2 release caused the failures (bad_deployment) | bad_deployment | order-service | dep-5333 | 0.05 (low) | rejected |
| 3 | H4: exchange-gateway or its infrastructure exhausted resources (resource/capacity/infrastructure) | resource_exhaustion | exchange-gateway | - | 0.05 (low) | rejected |
| 4 | H3: pricing-engine cache.ttl_seconds config change caused the incident (config_change) | config_change | pricing-engine | cfg-2290 | 0.03 (low) | rejected |

### H1: External exchange partner API degraded (dependency_failure)
The third-party exchange provider at api.exchange-partner.example began failing/hanging around 11:01Z, causing 5s connect timeouts and 503s at exchange-gateway, which returned 504s to order-service (placeOrder and quoteSnapshot both affected). order-service's release is unrelated.

**Causal chain**
1. External provider api.exchange-partner.example starts failing ~11:01Z [F5, F12]
2. connect timeouts (5000ms) and 503s raise exchange-gateway upstream_error_rate to ~45% and upstream latency to ~9.3s [F6, F7]
3. exchange-gateway returns 504 to order-service placeOrder calls [F1, F14]
4. order-service submit failure rate reaches ~30% [F2]

**Supporting evidence**
  - `F5` ✔ exchange-gateway emits errors against its external upstream: 163 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' from 11:01:25Z and 146 'quoteSnapshot' timeouts from 11:01:43Z, plus 161 'exchange-api returned 503' entries and a circuit-breaker OPEN warning.
  - `F6` ✔ exchange-gateway upstream_error_rate jumps from ~0.3% (flat 08:00-10:36) to 45.46% at 11:02 and stays ~44-47% through 12:59.
  - `F7` ✔ exchange-gateway upstream_latency_ms rises from ~120ms baseline to ~9151ms at 11:02 (max ~10,042ms) and stays ~9.2-9.4s through 12:59.
  - `F11` ✔ The dependency graph confirms exchange-gateway -> exchange-api is an 'external' node owned by 'vendor' with has_telemetry:false, and order-service -> exchange-gateway is http.
  - `F12` ✔ No exchange-partner timeouts were logged before 11:01:25Z; a targeted query for 'timeout' on exchange-gateway from 09:00-11:05 returns only 10 entries, earliest at 11:01:25Z.
  - `F13` ✔ A prior incident HIST-103 (2025-10-21) has the same symptom profile — '504 on order submit', 'upstream timeouts', services order-service/exchange-gateway — with root cause 'External exchange API outage on the partner side'.
  - `F14` ✔ The fault propagates outward-in: order-service 504s are a downstream symptom of exchange-gateway's upstream timeouts, whose target host is the external provider (api.exchange-partner.example).
  - `F8` ✔ exchange-gateway's own resource usage is flat and healthy: cpu_pct ~30% and request_rate ~270 req/s are unchanged before and after onset.
**Contradicting evidence**
  - (none)
**Assumptions**
  - api.exchange-partner.example has no telemetry, so the partner-side fault is inferred from gateway upstream metrics, not directly observed
  - quoteSnapshot timeouts (via pricing-engine path) are the same partner degradation, not a separate cause

### H2: order-service v2.15.2 release caused the failures (bad_deployment)
The order-service v2.15.2 idempotency-key release introduced a fault causing order submit failures, which is why on-call is rolling it back.

**Causal chain**
1. deploy dep-5333 changes submit handler [F3]
2. rollback should restore service [F4]

**Supporting evidence**
  - `F3` ✔ The only deployment in the window is order-service v2.15.2 (Add idempotency keys to order submit), deployed 10:48:00Z and rolled back at 11:16:00Z.
**Contradicting evidence**
  - `F4` ✔ order-service errors continue after the 11:16 rollback: 310 ERROR entries between 11:16:05Z and 12:58:54Z, all '504 Gateway Timeout (placeOrder)'. The rollback did not restore service.
  - `F5` ✔ exchange-gateway emits errors against its external upstream: 163 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' from 11:01:25Z and 146 'quoteSnapshot' timeouts from 11:01:43Z, plus 161 'exchange-api returned 503' entries and a circuit-breaker OPEN warning.
  - `F7` ✔ exchange-gateway upstream_latency_ms rises from ~120ms baseline to ~9151ms at 11:02 (max ~10,042ms) and stays ~9.2-9.4s through 12:59.
  - `F14` ✔ The fault propagates outward-in: order-service 504s are a downstream symptom of exchange-gateway's upstream timeouts, whose target host is the external provider (api.exchange-partner.example).
**Rejected because:** Rollback at 11:16Z did NOT restore service: 310 identical 504 errors continue through 12:58:54Z (F4). The failing call is to exchange-gateway, whose upstream to the external partner is timing out (F5-F7); order-service's own error rate onset (11:02, F2) is driven entirely by that external timeout pattern, not by submit-handler code.

### H4: exchange-gateway or its infrastructure exhausted resources (resource/capacity/infrastructure)
exchange-gateway pods ran out of CPU/memory/connections or its node failed, causing the timeouts.

**Causal chain**
1. gateway resources saturated [F8]

**Supporting evidence**
  - `F7` ✔ exchange-gateway upstream_latency_ms rises from ~120ms baseline to ~9151ms at 11:02 (max ~10,042ms) and stays ~9.2-9.4s through 12:59.
**Contradicting evidence**
  - `F8` ✔ exchange-gateway's own resource usage is flat and healthy: cpu_pct ~30% and request_rate ~270 req/s are unchanged before and after onset.
**Rejected because:** exchange-gateway cpu_pct (~30%) and request_rate (~270 req/s) are flat across onset (F8), and its latency increase is entirely in upstream_latency_ms to the external host (F7), indicating the failure is external to the gateway box.

### H3: pricing-engine cache.ttl_seconds config change caused the incident (config_change)
The 09:20Z pricing-engine cache TTL reduction from 5s to 4s increased downstream load and caused timeouts.

**Causal chain**
1. TTL 5->4 raises pricing-engine load [F10]

**Supporting evidence**
  - `F10` ✔ The only config change is pricing-engine cache.ttl_seconds 5->4 at 09:20:00Z (config-bot), which touches pricing-engine and is 1h41m before onset.
**Contradicting evidence**
  - `F9` ✔ pricing-engine is unaffected: latency_p99_ms stays flat at ~59-62ms across the whole window and it has zero WARN/ERROR logs.
  - `F5` ✔ exchange-gateway emits errors against its external upstream: 163 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' from 11:01:25Z and 146 'quoteSnapshot' timeouts from 11:01:43Z, plus 161 'exchange-api returned 503' entries and a circuit-breaker OPEN warning.
**Rejected because:** pricing-engine latency is flat (~60ms) and it has zero errors (F9); the change is 1h41m before onset (F10); the failing host is the external exchange partner, not pricing-engine or redis-cache.

## Facts (verified observations)
- order-service 504 errors began 11:02:45Z and continued to 12:58:54Z, all of the form 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'. [F1, F4]
- order-service error_rate rose from ~0.2% to ~30.8% at 11:02 and stayed ~30% through 12:59. [F2]
- The only deployment was order-service v2.15.2 at 10:48Z, rolled back at 11:16Z. [F3]
- exchange-gateway logged upstream connect timeouts at 5000ms to host=api.exchange-partner.example, plus 503s and a circuit-breaker OPEN warning. [F5]
- exchange-gateway upstream_error_rate rose to ~45% and upstream_latency_ms to ~9.3s at onset. [F6, F7]
- exchange-gateway's own cpu_pct (~30%) and request_rate (~270 req/s) are flat across the onset. [F8]
- pricing-engine is unaffected (flat ~60ms latency, zero errors) and the only config change was its 09:20Z cache TTL 5->4. [F9, F10]
- exchange-api is an external vendor node with no telemetry, reachable only via exchange-gateway. [F11]
- No exchange-partner timeout errors appear before 11:01:25Z. [F12]
- Historical incident HIST-103 matches the current symptom profile with root cause 'External exchange API outage on the partner side'. [F13]

## Assumptions (not verified)
- The 5000ms connect timeouts to api.exchange-partner.example reflect genuine partner-side slowness/unavailability rather than a local network/DNS fault on the gateway's egress path.
- quoteSnapshot and placeOrder failures share the same partner root cause rather than two independent faults.
- order-service v2.15.2 did not itself introduce the external timeout behavior (its placeOrder path always calls exchange-gateway).

## Timeline
- `2026-03-17T09:20:00Z` pricing-engine config change cfg-2290: cache.ttl_seconds 5->4 (unrelated; pricing-engine unaffected) [F10]
- `2026-03-17T10:48:00Z` order-service v2.15.2 deployed (dep-5333, idempotency keys) [F3]
- `2026-03-17T10:49:00Z` exchange-gateway upstream_error_rate/latency begin rising (3.7%, 817ms avg) within this bucket [F6, F7]
- `2026-03-17T11:01:25Z` First exchange-gateway 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' [F5, F12]
- `2026-03-17T11:02:00Z` order-service error_rate jumps to ~30.8%; exchange-gateway upstream_error_rate ~45%, upstream_latency ~9.2s [F2, F6, F7]
- `2026-03-17T11:02:45Z` First order-service '504 Gateway Timeout (placeOrder)' error [F1]
- `2026-03-17T11:03:26Z` Circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s) [F5]
- `2026-03-17T11:16:00Z` order-service v2.15.2 rolled back [F3]
- `2026-03-17T11:16:05Z` order-service errors continue unchanged after rollback (310 more through 12:58:54Z) [F4]
- `2026-03-17T12:59:00Z` Latest data: order-service error_rate 30.4%, exchange-gateway upstream_latency ~9.4s — still failing [F2, F7]

## Recommended actions
- **[immediate]** Stop/complete the order-service rollback as a fix — it is not the cause; keep v2.15.2 (or revert only if independent reasons) and redirect effort to the exchange integration. -- Errors persisted 1h42m past rollback (F4); rolling back further wastes time and risks unnecessary churn. [F4]
- **[immediate]** Engage the exchange partner / vendor support with the x-partner-request-id values (e.g. 46ed2490) and the 5000ms connect-timeout / 503 evidence; check partner status page. -- The failing host api.exchange-partner.example returned 503s and hung; partner-side confirmation is the fastest path. [F5, F11]
- **[immediate]** Fail over to a secondary exchange route / enable degraded mode (e.g. queue placeOrder retries) as was done in HIST-103. -- Historically resolved the same symptom profile by failing over to a secondary exchange route. [F13]
- **[short_term]** Tune exchange-gateway resilience: shorten connect timeout/tighten circuit-breaker thresholds so partner slowness sheds load faster instead of holding 5s+ connections. -- 5000ms connect timeouts dominate and a 60s-window breaker opened only after ~2 min of degradation. [F5, F7]
- **[long_term]** Add telemetry/synthetic monitoring for the external exchange API so partner-side degradation is observable directly (it currently has has_telemetry:false). -- Partner has no telemetry, forcing inference from gateway metrics only. [F11]

## Data gaps
- api.exchange-partner.example has has_telemetry:false — the partner-side root cause cannot be directly observed; it is inferred from exchange-gateway upstream metrics and error messages only.
- No vendor status/incident data was available to independently confirm the partner outage.
- Logs for exchange-gateway and order-service are truncated (truncated:true, 40 of 349/473 returned); absence of other error patterns is not proven, though the top-pattern counts and metrics corroborate the single failure mode.

## Investigation stats
- latency 45.09s, LLM calls 5, tokens 63277 (in 55607 / out 7670)
- tool calls 16 (0 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
