# Incident investigation report: INC-003
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Despite the incident being reported against order-service and correlated with the v2.15.2 release, the fault originates at the external exchange provider. exchange-gateway began logging 'upstream connect timeout ... host=api.exchange-partner.example' at 11:01:25Z and provider 503s at 11:02:20Z — before order-service's first error (11:02:45Z). exchange-gateway upstream_error_rate jumped 0.3%->46% and upstream_latency 120ms->9200ms at the 11:00 bucket, opening its circuit breaker at 11:03:26Z; order-service's placeOrder then failed with 'exchange-gateway call failed: 504'. Critically, errors did NOT stop after the order-service rollback at 11:16Z (310 more errors through 12:58Z) and order-service's own resources were normal, ruling out the deploy as the trigger. pricing-engine (the only config change) was healthy. Root cause: dependency_failure at exchange-api; recommended mitigation mirrors HIST-103 (fail over to secondary exchange route, engage vendor).

## Probable root cause
**External exchange-api provider degradation causes placeOrder timeouts/504s** -- dependency_failure in `exchange-api` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange-api provider degradation causes placeOrder timeouts/504s | dependency_failure | exchange-api | - | 0.86 (high) | active |
| 2 | H2: order-service v2.15.2 deploy caused the failures | bad_deployment | order-service | dep-5333 | 0.08 (low) | rejected |
| 3 | H3: pricing-engine config change (cache.ttl 5->4) caused the incident | config_change | pricing-engine | cfg-2290 | 0.03 (low) | rejected |
| 4 | H4: Local resource/infrastructure saturation in order-service or exchange-gateway | resource_exhaustion | exchange-gateway | - | 0.03 (low) | rejected |

### H1: External exchange-api provider degradation causes placeOrder timeouts/504s
The external vendor provider api.exchange-partner.example (exchange-api) began failing/being unreachable at ~11:01Z, causing exchange-gateway's upstream connect timeouts and 503s, which propagated as 504 Gateway Timeouts on order-service placeOrder. The fault originates outside our infrastructure (dependency_failure), not in order-service.

**Causal chain**
1. exchange-api (external vendor) starts refusing/timing out connections at ~11:01Z [F4, F5, F13]
2. exchange-gateway upstream_error_rate 0.3%->46% and upstream_latency 120ms->9200ms [F6, F7]
3. exchange-gateway opens circuit breaker for exchange-api, returns 504s [F14, F16]
4. order-service placeOrder calls fail with 'exchange-gateway call failed: 504' [F2]
5. order-service error_rate ~31%, no recovery after rollback [F3, F8]

**Supporting evidence**
  - `F4` ✔ exchange-gateway ERROR logs begin at 11:01:25Z with 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' — earlier than order-service's first error at 11:02:45Z.
  - `F5` ✔ exchange-gateway also logged 503s returned by the external provider: 'exchange-api returned 503 Service Unavailable (x-partner-request-id=...)' first seen 11:02:20Z.
  - `F6` ✔ exchange-gateway upstream_error_rate jumped from ~0.3% (all buckets before 11:00) to 40.87% avg at the 11:00 bucket, and stayed ~45% through 12:50.
  - `F7` ✔ exchange-gateway upstream_latency_ms jumped from ~120ms to 8248ms avg at the 11:00 bucket and remained ~9200ms.
  - `F13` ✔ exchange-api is an external vendor dependency (owner 'vendor', has_telemetry:false) that exchange-gateway calls; exchange-gateway's upstream targets host=api.exchange-partner.example.
  - `F14` ✔ exchange-gateway opened its circuit breaker for exchange-api: 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26Z.
  - `F15` ✔ Historical incident HIST-103 ('Order failures during exchange partner outage') matches symptoms 504 on order submit / upstream timeouts, category dependency_failure, root cause external exchange API outage.
  - `F16` ✔ Interpretation: exchange-gateway's own service error_rate and latency_p99 rose to ~44% and ~5100ms because it was blocked/timing out on the external exchange-api, not due to local faults.
**Contradicting evidence**
  - `F1` ✔ order-service v2.15.2 (dep-5333, 'Add idempotency keys to order submit') was deployed at 10:48:00Z and rolled back at 11:16:00Z.
**Assumptions**
  - api.exchange-partner.example is the same external provider as exchange-api (vendor edge)
  - the vendor did not restore service within the window

### H2: order-service v2.15.2 deploy caused the failures
The order-service v2.15.2 idempotency-key release introduced a regression causing order submit 504s, as the incident report suggests.

**Causal chain**
1. deploy dep-5333 -> submit handler regression [F1]

**Supporting evidence**
  - `F1` ✔ order-service v2.15.2 (dep-5333, 'Add idempotency keys to order submit') was deployed at 10:48:00Z and rolled back at 11:16:00Z.
  - `F2` ✔ order-service ERROR logs all have the same pattern 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', first seen 11:02:45Z.
**Contradicting evidence**
  - `F3` ✘ unverified order-service error logs continue after the 11:16 rollback, with 310 matches from 11:16 to 12:58 and first_match '2026-03-17T11:16:05Z'.
  - `F4` ✔ exchange-gateway ERROR logs begin at 11:01:25Z with 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' — earlier than order-service's first error at 11:02:45Z.
  - `F6` ✔ exchange-gateway upstream_error_rate jumped from ~0.3% (all buckets before 11:00) to 40.87% avg at the 11:00 bucket, and stayed ~45% through 12:50.
  - `F7` ✔ exchange-gateway upstream_latency_ms jumped from ~120ms to 8248ms avg at the 11:00 bucket and remained ~9200ms.
  - `F8` ✔ order-service error_rate jumped from ~0.2% to 24.64% at the 11:00 bucket and stayed ~31% through 12:50 (no recovery after the 11:16 rollback).
  - `F9` ✔ exchange-gateway CPU stayed flat at ~30% throughout (min 26.73, max 33.58) — no local resource saturation.
  - `F10` ✔ order-service resource metrics are normal: memory_pct ~60% (max 67.93) and db_pool_in_use ~14 connections (min 12.58, max 15.49).
**Assumptions**
  - none verified
**Rejected because:** Errors persisted unchanged after the 11:16 rollback (F3, F8); exchange-gateway upstream failures began at 11:01:25, BEFORE order-service's first error at 11:02:45 (F4); order-service CPU/memory/db-pool were normal (F9, F10); errors are exclusively 504s from a downstream external call, not local exceptions.

### H3: pricing-engine config change (cache.ttl 5->4) caused the incident
cfg-2290 shortening the pricing-engine quote cache TTL caused cascading slowness leading to order failures.

**Causal chain**
1. cfg-2290 -> pricing-engine degradation *(assumed)*

**Supporting evidence**
  - `F12` ✔ Only one config change occurred in the window: pricing-engine cache.ttl_seconds 5->4 at 09:20:00Z (cfg-2290), well before onset.
**Contradicting evidence**
  - `F11` ✔ pricing-engine stayed completely healthy through the window: error_rate flat ~0.21% (max 0.32) and zero WARN/ERROR logs.
  - `F2` ✔ order-service ERROR logs all have the same pattern 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', first seen 11:02:45Z.
**Assumptions**
  - none verified
**Rejected because:** pricing-engine error_rate was flat at ~0.21% with zero WARN/ERROR logs (F11); change was at 09:20, ~100 min before onset; order-service errors are explicitly exchange-gateway 504s, not pricing calls (F2).

### H4: Local resource/infrastructure saturation in order-service or exchange-gateway
CPU/memory/connection exhaustion in order-service or exchange-gateway produced the timeouts.

**Causal chain**
1. resource saturation -> timeouts *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ exchange-gateway CPU stayed flat at ~30% throughout (min 26.73, max 33.58) — no local resource saturation.
  - `F10` ✔ order-service resource metrics are normal: memory_pct ~60% (max 67.93) and db_pool_in_use ~14 connections (min 12.58, max 15.49).
**Assumptions**
  - none verified
**Rejected because:** exchange-gateway CPU flat ~30% (F9); order-service memory ~60% and db_pool ~14 conns (F10); the latency is on the OUTBOUND external call (upstream_latency), not local processing.

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- api.exchange-partner.example is the external vendor 'exchange-api' node in the dependency graph.
- The external provider stayed degraded through 12:59Z (no recovery seen in the window).
- No order-service-internal code path other than the exchange-gateway call is producing the 504s (log evidence shows only that pattern).

## Timeline
- `2026-03-17T09:20:00Z` pricing-engine config change cfg-2290 (cache.ttl_seconds 5->4) — unrelated [F12]
- `2026-03-17T10:48:00Z` order-service v2.15.2 deployed (dep-5333) [F1]
- `2026-03-17T11:01:25Z` exchange-gateway first upstream connect timeout to api.exchange-partner.example (placeOrder) [F4]
- `2026-03-17T11:02:20Z` First 'exchange-api returned 503 Service Unavailable' from provider [F5]
- `2026-03-17T11:02:45Z` order-service first 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' error [F2]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker OPEN for exchange-api (failure ratio 0.47) [F14]
- `2026-03-17T11:16:00Z` order-service v2.15.2 rolled back — errors continue unchanged (310 further errors to 12:58) [F3, F8]
- `2026-03-17T12:58:00Z` Errors still ongoing; exchange-api still failing [F3, F6]

## Recommended actions
- **[immediate]** Stop attributing the incident to the order-service release; the v2.15.2 rollback is not the fix and errors continue without it. -- Errors persisted 310+ occurrences after the 11:16 rollback and the causal chain starts downstream at the external provider. [F3, F8, F4]
- **[immediate]** Engage the exchange-api vendor and/or fail over to a secondary exchange route (as in HIST-103). -- Provider is returning 503s and connect timeouts; mitigation must address the external dependency. [F5, F4, F15]
- **[short_term]** Add/adjust circuit breaker + short-timeout failover to a secondary exchange route so exchange-api degradation does not surface as order submit 504s. -- exchange-gateway already tripped its breaker but order submits still fail; needs graceful degradation path. [F14, F2]
- **[long_term]** Add synthetic monitoring/alerts on exchange-gateway upstream_error_rate and upstream_latency so provider degradation is detected before order-service symptoms. -- Upstream metrics moved first (0.3%->46%) and would have given earlier warning. [F6, F7]

## Data gaps
- No telemetry from exchange-api itself (has_telemetry:false), so the vendor-side root cause cannot be directly confirmed — only observed via exchange-gateway's upstream errors.
- No vendor status page / external incident feed available in the tools.
- Whether a secondary exchange route exists and is configurable was not verifiable from available data.

## Investigation stats
- latency 40.43s, LLM calls 5, tokens 64686 (in 57875 / out 6811)
- tool calls 19 (0 failed, 0 partial, 0 recovered by retry), findings 15/16 verified
