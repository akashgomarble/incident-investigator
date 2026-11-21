# Incident investigation report: INC-003
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order submit failures (~30% 5xx) beginning ~11:01 UTC are NOT caused by the order-service v2.15.2 release. The fault originates at the external exchange partner API (api.exchange-partner.example), which started returning connect timeouts (5000ms) and 503 Service Unavailable, causing exchange-gateway upstream errors to spike to ~45%, which order-service surfaces as 504s on placeOrder. The order-service deploy at 10:48 was rolled back at 11:16 but error rate stayed flat at ~31% through 12:45, ruling out the release as the trigger. After the partner degraded, exchange-gateway's circuit breaker opened (11:03:26), converting timeouts into immediate failures. Category: dependency_failure; root-cause component is the external exchange partner.

## Probable root cause
**External exchange partner API outage** -- dependency_failure in `exchange-api` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange partner API outage | dependency_failure | exchange-api | - | 0.85 (high) | active |
| 2 | H2: order-service v2.15.2 release caused the failures | bad_deployment | order-service | dep-5333 | 0.05 (low) | rejected |
| 3 | H3: Resource/capacity exhaustion on exchange-gateway or order-service | resource_exhaustion | exchange-gateway | - | 0.05 (low) | rejected |

### H1: External exchange partner API outage
The third-party exchange partner API (api.exchange-partner.example) degraded around 11:01 UTC, returning connect timeouts and 503s. exchange-gateway (which calls it for placeOrder and quoteSnapshot) accumulated upstream errors (~45%), tripped its circuit breaker, and returned errors/504s to order-service, which surfaced them on order submit.

**Causal chain**
1. External partner API begins timing out / returning 503 [F2, F9]
2. exchange-gateway upstream_error_rate climbs to ~45% and circuit breaker opens [F3, F4, F10]
3. order-service placeOrder calls to exchange-gateway fail with 504 [F5, F8]
4. order-service error rate rises to ~30% even after rollback [F1, F6]

**Supporting evidence**
  - `F2` ✔ exchange-gateway logs show upstream connect timeouts to host=api.exchange-partner.example and 'exchange-api returned 503 Service Unavailable', first appearing at 11:01:25.
  - `F3` ✔ exchange-gateway error_rate rose from ~0.2% to ~44% starting in the 10:51 bucket and stayed elevated (last 46.67%) through 12:45.
  - `F4` ✔ exchange-gateway upstream_error_rate (calls to the partner) rose from ~0.3% to ~46% and upstream_latency_ms rose from ~120ms to ~9,200ms starting at the 10:51 bucket.
  - `F7` ✔ exchange-gateway logs include a WARN 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26.
  - `F8` ✔ order-service depends on exchange-gateway (http) and exchange-gateway depends on external exchange-api (vendor, has_telemetry:false).
  - `F9` ✔ exchange-gateway CPU (~30%), memory (~44%), restarts (0) and request_rate (~270 req/s) all stayed flat across the incident onset; no local resource anomaly.
  - `F10` ✔ order-service db_pool_in_use stayed flat at ~14 connections and restarts stayed 0 throughout.
**Contradicting evidence**
  - (none)
**Assumptions**
  - exchange-api is genuinely an external vendor dependency whose degradation is not caused by our own traffic (request rate was flat, CPU/mem normal on our side)
  - The 504s reported at order-service are the same failures observed at exchange-gateway

### H2: order-service v2.15.2 release caused the failures
The v2.15.2 deploy (idempotency keys / retry-safe submit) introduced a defect causing order submit failures; rollback should fix it.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F1` ✔ order-service error_rate jumped from ~0.2% to ~31% starting in the 10:51 bucket and remained ~31% (last 30.39%, max 35.57%) through 12:45.
  - `F5` ✔ order-service ERROR logs are exclusively 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', 349 total matches from 11:02:45 to 12:58:54.
  - `F6` ✔ order-service v2.15.2 was deployed 10:48 and rolled back at 11:16, yet error rate stayed ~31% until at least 12:45 (no recovery after rollback).
**Rejected because:** Errors attributed to the exchange-gateway upstream (504 on placeOrder), not the new idempotency code. Onset of errors (~11:01) lags deploy (10:48) by >13 min. Most decisively, the release was rolled back at 11:16 yet error rate stayed at ~31% through 12:45 with no recovery - the release is not the cause.

### H3: Resource/capacity exhaustion on exchange-gateway or order-service
CPU/memory/db-pool exhaustion on the request path caused timeouts and 5xx.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ exchange-gateway CPU (~30%), memory (~44%), restarts (0) and request_rate (~270 req/s) all stayed flat across the incident onset; no local resource anomaly.
  - `F10` ✔ order-service db_pool_in_use stayed flat at ~14 connections and restarts stayed 0 throughout.
  - `F11` ✔ pricing-engine error_rate (~0.2%) and latency_p99_ms (~60ms) stayed flat throughout, and its only config change was a cache TTL 5->4s at 09:20.
  - `F12` ✔ ledger-db slow_queries_per_min stayed flat (~1/min) throughout, showing no database-side degradation.
**Rejected because:** exchange-gateway CPU stayed ~30%, memory ~44%, request_rate flat ~270 req/s, restarts 0; order-service db_pool_in_use flat ~14, restarts 0. No resource saturation on our side; the timeouts are on outbound calls to the external host.

## Facts (verified observations)
- order-service error rate rose from ~0.2% to ~31% beginning in the 10:51 UTC bucket and did not recover after the 11:16 rollback. [F1, F6]
- exchange-gateway's errors are outbound to the external host api.exchange-partner.example (connect timeouts and 503 Service Unavailable). [F2, F8]
- exchange-gateway error_rate and upstream_error_rate rose to ~44-46% at the same 10:51 onset bucket. [F3, F4]
- order-service's only ERROR logs are 504 Gateway Timeout on exchange-gateway placeOrder calls. [F5]
- exchange-gateway's circuit breaker for exchange-api opened at 11:03:26. [F7]
- exchange-gateway CPU, memory, restarts and request_rate showed no anomaly; order-service db_pool and restarts also flat. [F9, F10]
- pricing-engine config change (cache TTL 5->4) and metrics are clean; ledger-db slow queries flat. [F11, F12]
- The external exchange-api dependency has no telemetry. [F14]

## Assumptions (not verified)
- api.exchange-partner.example is a genuinely external third-party dependency, not an internal service mislabeled 'vendor'.
- The 10:51 bucket spike represents the true onset; bucket granularity (~19 min) prevents finer timing.
- order-service and exchange-gateway are the only relevant consumers; no other caller of exchange-gateway changed behavior.

## Timeline
- `2026-03-17T10:48:00Z` order-service v2.15.2 deployed (idempotency keys / retry-safe submit). [F6]
- `2026-03-17T10:51:00Z` error_rate and exchange-gateway upstream metrics jump in this 5-minute bucket (first bucket showing spiking averages). [F1, F3, F4]
- `2026-03-17T11:01:25Z` First exchange-gateway ERROR: upstream connect timeout to api.exchange-partner.example. [F2]
- `2026-03-17T11:02:45Z` First order-service ERROR: exchange-gateway call failed 504. [F5]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker OPEN for exchange-api. [F7]
- `2026-03-17T11:16:00Z` order-service v2.15.2 rolled back; error rate does not recover. [F6]
- `2026-03-17T12:45:00Z` order-service error rate still ~31%; incident ongoing. [F1]

## Recommended actions
- **[immediate]** Engage the exchange partner / vendor support and request status of api.exchange-partner.example; do not roll back order-service again (it is not the cause). -- Errors are outbound to the external partner host; the vendor owns the fault. [F2, F14]
- **[immediate]** Fail over to the secondary exchange route (as done in HIST-103) or shed/queue placeOrder traffic to protect downstream systems while the partner recovers. -- Past partner-outage incident was mitigated by failing over to a secondary exchange route. [F13]
- **[short_term]** Review exchange-gateway circuit-breaker and timeout thresholds so a partner outage degrades gracefully (fast-fail with clear error) instead of surfacing 504s upstream. -- Circuit breaker only opened after errors were already propagating; order-service surfaced raw 504s. [F7, F5]
- **[long_term]** Request partner-side telemetry / SLA visibility, since exchange-api has no telemetry on our side. -- Inability to observe the external dependency slowed diagnosis. [F14]

## Data gaps
- exchange-api (external partner) has no telemetry, so the partner-side failure is inferred from gateway-side symptoms only and cannot be directly confirmed.
- No exact second-level onset of the partner failure is available; the 10:51 metric bucket is the earliest signal, while first logs are 11:01:25 - the ~10-minute gap is unexplained.
- kafka (queue-produce dependency of order-service) metrics were not examined; assume healthy but unverified.

## Investigation stats
- latency 64.13s, LLM calls 6, tokens 80333 (in 72852 / out 7481)
- tool calls 26 (1 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
