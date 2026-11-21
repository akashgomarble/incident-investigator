# Incident investigation report: INC-003
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order submit failures (~30%) and 504s began at ~10:50 UTC, coinciding with order-service v2.15.2 (dep-5333) but NOT caused by it. The error chain is: order-service -> exchange-gateway -> external exchange partner (api.exchange-partner.example). exchange-gateway's logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example' and 'exchange-api returned 503 Service Unavailable', with its upstream_error_rate jumping 0.3%->~46% and upstream_latency 120ms->~9.3s. exchange-gateway itself is healthy (no deploy, no config change, CPU ~30%, memory ~44%, 0 restarts, request_rate flat ~270/s), so the fault originates at the external partner. Critically, market-data-service -- an independent caller of exchange-gateway with no relation to order-service -- also degraded at the same 10:50 onset (0.2%->6.6%), and error rates persisted well after the order-service rollback at 11:16. The rollback of dep-5333 is therefore ineffective; the root cause is the external exchange partner dependency failure (matching historical HIST-103).

## Probable root cause
**External exchange partner API degradation (dependency_failure)** -- dependency_failure in `exchange-api` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange partner API degradation (dependency_failure) | dependency_failure | exchange-api | - | 0.90 (high) | active |
| 2 | H2: order-service v2.15.2 deploy caused failures (bad_deployment) | bad_deployment | order-service | dep-5333 | 0.05 (low) | rejected |
| 3 | H3: Exchange-gateway resource exhaustion or saturation | resource_exhaustion | exchange-gateway | - | 0.03 (low) | rejected |

### H1: External exchange partner API degradation (dependency_failure)
The external exchange provider api.exchange-partner.example began failing at ~11:01 UTC (connect timeouts after 5000ms and 503 Service Unavailable), causing exchange-gateway upstream errors (~46%), which propagated as 504s to exchange-gateway's callers order-service and market-data-service, and onward to api-gateway. The order-service v2.15.2 deploy is coincidental.

**Causal chain**
1. external exchange partner api.exchange-partner.example starts timing out (5000ms) and returning 503 at ~11:01 [F6]
2. exchange-gateway upstream_error_rate 0.3%->46% and upstream_latency 120ms->9.3s; circuit breaker opens [F7, F15]
3. order-service emits 'exchange-gateway call failed: 504' and error_rate 0.2%->31%, latency p99 ->5.3s [F3, F1, F12]
4. independent caller market-data-service and upstream api-gateway degrade at the same 10:50 onset, confirming a shared downstream fault [F10, F11]

**Supporting evidence**
  - `F1` ✔ order-service error_rate was ~0.2% until 10:32, then jumped to 14.76% avg in the 10:50 bucket and stayed ~31% through 12:45.
  - `F3` ✔ order-service's only new ERROR pattern is 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' (349 occurrences), first seen 11:02:45Z, continuing to 12:58:54Z.
  - `F5` ✔ exchange-gateway error_rate sat at ~0.2% until 10:27, then jumped to 22.77% in the 10:50 bucket and ~44% through 12:45.
  - `F6` ✔ exchange-gateway logs show upstream connect timeouts to the external partner: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' and 'exchange-api returned 503 Service Unavailable', first seen 11:01:25Z.
  - `F7` ✔ exchange-gateway upstream_error_rate rose from ~0.3% to ~46% and upstream_latency_ms from ~120ms to ~9.3s at the 10:50 onset.
  - `F8` ✔ exchange-gateway itself is internally healthy: CPU ~30% (flat), memory ~44% (flat), restarts 0, and request_rate flat ~270 req/s across the whole window.
  - `F9` ✔ exchange-gateway had no deployments and no config changes in the incident window.
  - `F10` ✔ market-data-service, an independent caller of exchange-gateway with no relation to order-service, also degraded at the same 10:50 onset (error_rate 0.2% -> ~6.6%).
  - `F11` ✔ api-gateway (upstream caller of order-service) error_rate also rose from ~0.2% to ~6.2% at the 10:50 onset.
  - `F12` ✔ order-service latency_p99 rose from ~150ms to ~5.3s at the 10:50 onset, consistent with waiting on 5000ms exchange-gateway timeouts.
  - `F14` ✔ Historical incident HIST-103 matches: 'Order failures during exchange partner outage' with symptoms '504 on order submit','upstream timeouts', root_cause 'External exchange API outage on the partner side', category dependency_failure.
  - `F15` ✔ exchange-gateway logged a circuit breaker opening for exchange-api: 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26Z.
**Contradicting evidence**
  - `F2` ✔ order-service v2.15.2 (dep-5333, 'Add idempotency keys to order submit') deployed at 10:48:00Z and was rolled back at 11:16:00Z.
**Assumptions**
  - exchange-api has no telemetry (vendor external, has_telemetry=false), so partner-side evidence is inferred from exchange-gateway's upstream metrics/logs
  - no infra change on the network path to the partner occurred

### H2: order-service v2.15.2 deploy caused failures (bad_deployment)
The dep-5333 order-service release introduced a fault that caused order submit 504s.

**Causal chain**
1. deploy dep-5333 at 10:48 introduced the failure [F2]
2. errors appear shortly after at 10:50 [F1]

**Supporting evidence**
  - `F2` ✔ order-service v2.15.2 (dep-5333, 'Add idempotency keys to order submit') deployed at 10:48:00Z and was rolled back at 11:16:00Z.
  - `F1` ✔ order-service error_rate was ~0.2% until 10:32, then jumped to 14.76% avg in the 10:50 bucket and stayed ~31% through 12:45.
**Contradicting evidence**
  - `F3` ✔ order-service's only new ERROR pattern is 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' (349 occurrences), first seen 11:02:45Z, continuing to 12:58:54Z.
  - `F6` ✔ exchange-gateway logs show upstream connect timeouts to the external partner: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' and 'exchange-api returned 503 Service Unavailable', first seen 11:01:25Z.
  - `F9` ✔ exchange-gateway had no deployments and no config changes in the incident window.
  - `F10` ✔ market-data-service, an independent caller of exchange-gateway with no relation to order-service, also degraded at the same 10:50 onset (error_rate 0.2% -> ~6.6%).
  - `F11` ✔ api-gateway (upstream caller of order-service) error_rate also rose from ~0.2% to ~6.2% at the 10:50 onset.
  - `F8` ✔ exchange-gateway itself is internally healthy: CPU ~30% (flat), memory ~44% (flat), restarts 0, and request_rate flat ~270 req/s across the whole window.
**Rejected because:** Rollback at 11:16 did not restore service (errors persist to 12:58); the errors are explicit upstream timeouts to the external partner, not order-service code paths; the independent caller market-data-service degraded at the same onset; and exchange-gateway shows the partner fault with no deploy of its own. Timing coincidence only.

### H3: Exchange-gateway resource exhaustion or saturation
exchange-gateway ran out of CPU/memory/connections or saturated its capacity, causing timeouts.

**Causal chain**
1. exchange-gateway resource metrics rise *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ exchange-gateway itself is internally healthy: CPU ~30% (flat), memory ~44% (flat), restarts 0, and request_rate flat ~270 req/s across the whole window.
  - `F9` ✔ exchange-gateway had no deployments and no config changes in the incident window.
**Rejected because:** CPU flat ~30%, memory flat ~44%, restarts 0, request_rate flat ~270 req/s; errors are upstream-attributed (upstream_error_rate/upstream_latency), not local resource signals.

## Facts (verified observations)
- order-service error rate jumped from ~0.2% to ~31% at the 10:50 bucket and persisted. [F1]
- order-service v2.15.2 (dep-5333) deployed 10:48 and rolled back 11:16; errors did not stop. [F2, F3]
- order-service ERROR logs are exclusively 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'. [F3]
- order-service db_pool_in_use stayed flat ~14 connections. [F4]
- exchange-gateway error_rate jumped from ~0.2% to ~44% at the same 10:50 onset. [F5]
- exchange-gateway logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example' and 'exchange-api returned 503 Service Unavailable'. [F6]
- exchange-gateway upstream_error_rate ~46% and upstream_latency ~9.3s at onset. [F7]
- exchange-gateway internal resources healthy: CPU ~30%, memory ~44%, restarts 0, request_rate flat. [F8]
- exchange-gateway had zero deployments and zero config changes in the window. [F9]
- market-data-service, an independent exchange-gateway caller, degraded at the same 10:50 onset. [F10]
- pricing-engine stayed healthy (error rate ~0.2%) despite cfg-2290. [F13]
- Historical HIST-103 is a partner-side exchange outage causing the same symptoms. [F14]

## Assumptions (not verified)
- exchange-api has no telemetry (has_telemetry=false), so the partner-side fault is inferred from exchange-gateway's upstream metrics and logs.
- No infrastructure change occurred on the network path between exchange-gateway and the external partner.
- The 10:50 metric bucket onset (~11:01 from logs) reflects the true start of the partner degradation.

## Timeline
- `2026-03-17T09:20:00Z` pricing-engine config cfg-2290 (cache.ttl_seconds 5->4); no impact observed [F13]
- `2026-03-17T10:48:00Z` order-service v2.15.2 (dep-5333) deployed [F2]
- `2026-03-17T10:50:00Z` Simultaneous error onset: order-service 0.2%->14.8%, exchange-gateway 0.2%->22.8%, market-data-service 0.2%->3.2%, api-gateway 0.2%->3.1% [F1, F5, F10, F11]
- `2026-03-17T11:01:25Z` exchange-gateway first logs upstream connect timeout to api.exchange-partner.example [F6]
- `2026-03-17T11:02:45Z` order-service first logs 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' [F3]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker opens for exchange-api (failure ratio 0.47/60s) [F15]
- `2026-03-17T11:10:00Z` Incident reported; order-service latency p99 ~5.3s [F12]
- `2026-03-17T11:16:00Z` order-service dep-5333 rolled back; errors persist [F2]
- `2026-03-17T12:58:54Z` order-service still logging 504 exchange-gateway failures [F3]

## Recommended actions
- **[immediate]** Stop treating the order-service rollback as the fix and check status/status page of external exchange partner api.exchange-partner.example; engage the vendor. -- Errors persist after rollback and originate from partner connect timeouts/503s. [F2, F6]
- **[immediate]** Fail over exchange traffic to the secondary exchange route (as done in HIST-103) if available. -- Removes dependence on the degraded partner. [F14]
- **[short_term]** Tune exchange-gateway circuit breaker/timeout behavior and queue/shed order submissions to protect callers during partner degradation. -- Circuit breaker opened but failures still propagated as 504s to callers. [F15, F3]
- **[short_term]** Do not roll back further versions of order-service; verify with market-data-service correlation before attributing to order-service releases. -- The deploy was coincidental; rolling back order-service had no effect. [F2, F10]
- **[long_term]** Add telemetry/alerting on the external partner dependency and reduce the 5000ms connect timeout so degradation is detected earlier. -- exchange-api has no telemetry; degradation was only visible indirectly. [F6, F7]

## Data gaps
- exchange-api (external vendor) has no telemetry (has_telemetry=false), so the partner-side fault cannot be directly observed; only inferred from exchange-gateway upstream metrics/logs.
- No network/infra telemetry for the path between exchange-gateway and api.exchange-partner.example to distinguish partner outage from network path failure.
- order-service log query returned truncated=true (40 of 349); however the single top pattern covers all 349 errors, so no additional error classes were missed within the queried level.
- No vendor status/communication data available in tool outputs.

## Investigation stats
- latency 72.68s, LLM calls 7, tokens 114328 (in 101361 / out 12967)
- tool calls 25 (0 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
