# Incident investigation report: INC-003
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order submit failures (~31% error rate, 504 Gateway Timeout) originate from the external exchange partner, not from order-service v2.15.2. Fault onset is 11:01-11:02Z, coinciding with exchange-gateway logging connect timeouts to api.exchange-partner.example and 503 Service Unavailable responses from exchange-api. exchange-gateway's upstream latency rose from ~120ms to ~9300ms and upstream error rate from ~0.3% to ~45%, while exchange-gateway itself and order-service show no local resource anomaly (CPU/memory/pool/request-rate flat, no restarts). The reported order-service deploy at 10:48 was rolled back at 11:16 but the error rate never recovered (~31% through 12:45), ruling it out as the cause. Root cause: external exchange-api dependency failure.", "facts": [{"statement": "order-service v2.15.2 deployed 10:48Z, rolled back 11:16Z with no recovery of the ~31% error rate.", "finding_ids": ["F1", "F3"]}, {"statement": "order-service error rate jumped from ~0.2% to ~31% at the 11:00Z bucket and stayed there through 12:45Z.", "finding_ids": ["F2"]}, {"statement": "All order-service errors are 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' beginning 11:02:45Z.", "finding_ids": ["F4"]}, {"statement": "exchange-gateway began logging upstream connect timeouts to api.exchange-partner.example at 11:01:25Z and partner 503 responses at 11:02:20Z.", "finding_ids": ["F5", "F6"]}, {"statement": "exchange-gateway upstream latency rose ~120ms -> ~9300ms and upstream error rate ~0.3% -> ~45%.", "finding_ids": ["F7"]}, {"statement": "exchange-gateway local health unchanged: CPU ~30%, 0 restarts, request_rate ~270 r/s.", "finding_ids": ["F8"]}, {"statement": "order-service local metrics unchanged: CPU ~40%, db_pool ~14, request_rate ~230 r/s.", "finding_ids": ["F9"]}, {"statement": "pricing-engine error rate flat ~0.2%; its 09:20 cache TTL config change had no effect.", "finding_ids": ["F11"]}, {"statement": "exchange-gateway had no deployments in the incident window; order-service depends on exchange-gateway which depends on the external exchange-api.", "finding_ids": ["F13", "F12"]}]

## Probable root cause
**External exchange partner (exchange-api) outage causes exchange-gateway timeouts and order submit failures** -- dependency_failure in `exchange-api (external partner)` (confidence 0.82, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange partner (exchange-api) outage causes exchange-gateway timeouts and order submit failures | dependency_failure | exchange-api (external partner) | - | 0.82 (high) | active |
| 2 | H2: order-service v2.15.2 release introduced the failures | bad_deployment | order-service | dep-5333 | 0.05 (low) | rejected |
| 3 | H3: exchange-gateway local resource exhaustion or process failure | resource_exhaustion | exchange-gateway | - | 0.03 (low) | rejected |
| 4 | H4: pricing-engine cache TTL config change degraded order flow | config_change | pricing-engine | cfg-2290 | 0.02 (low) | rejected |

### H1: External exchange partner (exchange-api) outage causes exchange-gateway timeouts and order submit failures
The external provider api.exchange-partner.example began failing/timing out at ~11:01Z (connect timeouts after 5000ms and 503 Service Unavailable responses). exchange-gateway, which has no local resource issue, surfaces 504s to order-service, whose placeOrder path fails ~30%. The order-service release is coincidental and its rollback had no effect because the fault is external.

**Causal chain**
1. External exchange-api degrades: connect timeouts + 503s [F5, F6]
2. exchange-gateway upstream latency 120ms->9300ms and upstream_error_rate 0.3%->45% [F7]
3. order-service placeOrder to exchange-gateway returns 504 Gateway Timeout [F4]
4. order-service error rate ~31%, latency p99 ->5s [F2, F10]
5. Rollback of order-service v2.15.2 did not restore service, confirming the fault is not in order-service [F3, F1]

**Supporting evidence**
  - `F5` ✔ exchange-gateway upstream connect timeouts and 503s begin at 11:01:25Z (upstream connect timeout host=api.exchange-partner.example op=placeOrder), about 13 minutes after the order-service deploy and about 2 minutes before order-service's first 504.
  - `F6` ✔ exchange-gateway logs show the external partner returning 503 Service Unavailable with x-partner-request-id values (161 occurrences) plus 163 placeOrder + 146 quoteSnapshot connect timeouts.
  - `F7` ✔ exchange-gateway's upstream to the partner degraded sharply: upstream_latency_ms went from ~120ms to ~8550ms at the 11:00 bucket, and upstream_error_rate from ~0.3% to ~42%, both sustained through 12:45.
  - `F8` ✔ exchange-gateway itself shows no local resource problem: CPU stayed ~30%, memory ~44%, restarts 0, and request_rate flat ~270 r/s across the whole window.
  - `F4` ✔ All 349 order-service ERROR entries share a single pattern: 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)', first seen 11:02:45Z, still occurring at 12:58:54Z.
  - `F3` ✔ order-service error rate did NOT recover after the 11:16 rollback: it remains ~31% in every bucket from 11:15 through 12:45.
  - `F9` ✔ order-service local metrics are unchanged across the incident: CPU ~40%, db_pool_in_use ~14 connections, request_rate ~230 r/s.
  - `F13` ✔ exchange-gateway had no code deployments in the 06:00-12:59 window.
**Contradicting evidence**
  - (none)
**Assumptions**
  - exchange-api has no telemetry in our stack, so its outage is inferred from exchange-gateway's upstream metrics and partner error bodies
  - the 503s with x-partner-request-id genuinely originate at the partner rather than a shared network egress layer

### H2: order-service v2.15.2 release introduced the failures
The idempotency-key/retry-safe submit change in v2.15.2 caused order submit failures and the rollback should have recovered the service.

**Causal chain**
(none given)

**Supporting evidence**
  - `F1` ✔ order-service deployed v2.15.2 at 2026-03-17T10:48:00Z (change 'Add idempotency keys to order submit'); it was rolled back at 11:16:00Z.
**Contradicting evidence**
  - `F3` ✔ order-service error rate did NOT recover after the 11:16 rollback: it remains ~31% in every bucket from 11:15 through 12:45.
  - `F4` ✔ All 349 order-service ERROR entries share a single pattern: 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)', first seen 11:02:45Z, still occurring at 12:58:54Z.
  - `F5` ✔ exchange-gateway upstream connect timeouts and 503s begin at 11:01:25Z (upstream connect timeout host=api.exchange-partner.example op=placeOrder), about 13 minutes after the order-service deploy and about 2 minutes before order-service's first 504.
  - `F9` ✔ order-service local metrics are unchanged across the incident: CPU ~40%, db_pool_in_use ~14 connections, request_rate ~230 r/s.
**Rejected because:** Rollback at 11:16 did not recover (error rate stayed ~31% through 12:45: F3); error onset at 11:02 postdates the 10:48 deploy by ~14 min and coincides with partner-side failures starting 11:01 (F5); all errors are exchange-gateway 504s, not order-service internal errors (F4); order-service CPU/pool/request-rate are normal (F9).

### H3: exchange-gateway local resource exhaustion or process failure
exchange-gateway ran out of CPU/memory or crashed, causing timeouts to order-service.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ exchange-gateway itself shows no local resource problem: CPU stayed ~30%, memory ~44%, restarts 0, and request_rate flat ~270 r/s across the whole window.
  - `F7` ✔ exchange-gateway's upstream to the partner degraded sharply: upstream_latency_ms went from ~120ms to ~8550ms at the 11:00 bucket, and upstream_error_rate from ~0.3% to ~42%, both sustained through 12:45.
**Rejected because:** exchange-gateway CPU ~30%, memory ~44%, 0 restarts, request_rate flat ~270 r/s (F8); the only degradation is on its upstream to the external provider (F7).

### H4: pricing-engine cache TTL config change degraded order flow
The cache.ttl_seconds 5->4 change at 09:20 caused downstream failures.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ pricing-engine error rate stayed flat at ~0.2% throughout, and it has no shared dependency with order-service other than being a sibling; the only config change (pricing-engine cache.ttl_seconds 5->4 at 09:20) produced no measurable effect.
  - `F4` ✔ All 349 order-service ERROR entries share a single pattern: 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)', first seen 11:02:45Z, still occurring at 12:58:54Z.
**Rejected because:** pricing-engine error rate stayed flat ~0.2% (F11), change occurred 100+ min before onset, and no order-service error references pricing-engine (F4).

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- exchange-api external provider has no telemetry in our stack; its fault is inferred from exchange-gateway upstream metrics and partner error payloads
- the observed 503/x-partner-request-id responses genuinely originate at the partner and not at a shared egress/network layer
- metric buckets are 900s aligned, so 1-2 min onset precision is limited

## Timeline
- `2026-03-17T09:20:00Z` pricing-engine config change cfg-2290 (cache.ttl_seconds 5->4) [F11]
- `2026-03-17T10:48:00Z` order-service v2.15.2 deployed (idempotency keys) [F1]
- `2026-03-17T11:01:25Z` exchange-gateway first upstream connect timeout to api.exchange-partner.example [F5]
- `2026-03-17T11:02:20Z` exchange-api returns 503 Service Unavailable with x-partner-request-id [F6]
- `2026-03-17T11:02:45Z` order-service first 504 Gateway Timeout on placeOrder [F4]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker OPEN for exchange-api (failure ratio 0.47) [F6]
- `2026-03-17T11:16:00Z` order-service v2.15.2 rolled back - error rate does not recover [F3]
- `2026-03-17T12:58:54Z` Last exchange-gateway 504 error logged on order-service [F4]

## Recommended actions
- **[immediate]** Stop treating this as order-service and engage the exchange partner / check partner status page; open a vendor incident for exchange-api. -- Partner returns 503 with x-partner-request-id and connect timeouts; fault is external. [F5, F6]
- **[immediate]** Fail over to a secondary exchange route or degrade gracefully (queue retries) instead of returning 504 on order submit. -- No secondary route exists in the dependency graph; HIST-103 resolution used exactly this. [F12]
- **[short_term]** Verify post-rollback state and confirm keeping v2.15.2 is safe; the rollback was based on a false correlation. -- Rollback produced no recovery; re-deploying or re-investigating costs availability. [F3, F1]
- **[short_term]** Add telemetry/health checks for the external exchange-api and alert on exchange-gateway upstream_error_rate. -- exchange-api has_telemetry=false and the upstream degradation was only visible via exchange-gateway. [F12, F7]
- **[long_term]** Tune exchange-gateway circuit breaker/timeouts so order-service fails fast rather than piling up 5s waits. -- Client p99 rose to ~5s while 5000ms connect timeouts were hit repeatedly. [F10, F5]

## Data gaps
- No direct telemetry from exchange-api (external, has_telemetry=false), so the partner-side root condition cannot be confirmed from our data - only inferred from exchange-gateway's upstream metrics and error bodies.
- No packet-level/DNS/TLS egress data to fully rule out a shared network path fault between exchange-gateway and the partner; the single WARN 'deprecated TLS cipher offered by client' at 10:04:50Z is unexplained and not obviously causal.
- order-service logs are truncated (349 matches, 40 returned); only the dominant 504 pattern was seen, so rarer error variants cannot be excluded.

## Investigation stats
- latency 48.53s, LLM calls 6, tokens 93958 (in 85769 / out 8189)
- tool calls 24 (0 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
