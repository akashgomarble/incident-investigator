# Incident investigation report: INC-003
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order-submit failures (~31% 5xx) starting ~11:01 are caused by the EXTERNAL exchange provider (api.exchange-partner.example) degrading: exchange-gateway sees upstream connect timeouts after 5000ms and 503 Service Unavailable from the partner, its circuit breaker opened at 11:03, and those errors propagate up as 504 Gateway Timeouts on order-service placeOrder (order-service hangs on exchange-gateway). The order-service v2.15.2 deploy (dep-5333, 10:48) coincides in time but is NOT the root cause: it was rolled back at 11:16 and the error rate did NOT recover (still ~31% at 12:45), exchange-gateway's request_rate/cpu/memory/restarts are flat (no local resource or capacity problem), and the failing signatures are all partner-facing. Same pattern as historical HIST-103 (external exchange partner outage). Category: dependency_failure.

## Probable root cause
**External exchange provider (exchange-api) outage/degradation** -- dependency_failure in `exchange-api` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange provider (exchange-api) outage/degradation | dependency_failure | exchange-api | - | 0.86 (high) | active |
| 2 | H2: order-service v2.15.2 deploy introduced the fault | bad_deployment | order-service | dep-5333 | 0.08 (low) | rejected |
| 3 | H3: Capacity saturation / resource exhaustion on our services | capacity_saturation | exchange-gateway | - | 0.05 (low) | rejected |

### H1: External exchange provider (exchange-api) outage/degradation
The third-party exchange API (api.exchange-partner.example, reached via exchange-gateway) began timing out on connect (5000ms) and returning 503s around 11:01, tripping exchange-gateway's circuit breaker and causing placeOrder/quoteSnapshot to fail. order-service blocks on these calls, producing 504 Gateway Timeouts on order submit (~31% of requests). The fault originates outside our infra.

**Causal chain**
1. External exchange-api degrades at ~11:01 (connect timeouts + 503s) [F3, F5, F6]
2. exchange-gateway's calls to the partner fail; circuit breaker opens [F4]
3. order-service placeOrder hangs on exchange-gateway (latency 150ms->5.3s) and returns 504s [F7, F2]
4. order submit 5xx reaches ~31% and persists [F1]
5. not our deploy: rollback at 11:16 gave no recovery; exchange-gateway resources flat [F12, F10, F11]

**Supporting evidence**
  - `F2` ✔ order-service errors are exclusively 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', 349 matches from 11:02:45 onward.
  - `F3` ✔ exchange-gateway logs are dominated by partner-facing failures: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder/quoteSnapshot' and 'exchange-api returned 503 Service Unavailable', first appearing 11:01:25.
  - `F4` ✔ exchange-gateway circuit breaker opened for exchange-api: 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26.
  - `F5` ✔ exchange-gateway upstream_error_rate (~0.3% baseline) jumped to 42.39% avg at the 11:00 bucket and stayed ~45% through 12:45.
  - `F6` ✔ exchange-gateway upstream_latency_ms (~120ms baseline) jumped to 8550ms avg at the 11:00 bucket and stayed ~9200ms through 12:45.
  - `F7` ✔ order-service latency_p99 jumped from ~150ms to 4648.8ms at the 11:00 bucket and stayed ~5300ms — consistent with hanging on the downstream call.
  - `F10` ✔ exchange-gateway request_rate stayed flat ~272 req/s (no traffic surge) and restarts stayed 0 across the window.
  - `F11` ✔ exchange-gateway memory_pct stayed flat ~44% across the window.
  - `F14` ✔ Historical incident HIST-103 (2025-10-21) had the same symptom set (504 on order submit, upstream timeouts) and root cause 'External exchange API outage on the partner side' (category dependency_failure).
  - `F15` ✔ exchange-gateway partner errors persist to the latest data: 116 ERRORs between 12:30 and 12:58, still 'exchange-api returned 503' at 12:58:32.
**Contradicting evidence**
  - `F1` ✔ order-service error_rate was ~0.2% until the 10:51 bucket, then jumped to avg 13.07% and stayed ~31% through 12:45 (last 30.39%).
**Assumptions**
  - exchange-gateway errored telemetry (upstream_error_rate/upstream_latency_ms) measures the external partner, not an internal hop
  - the partner outage is not itself caused by a change on our side (e.g. an auth/cert change)

### H2: order-service v2.15.2 deploy introduced the fault
Deployment dep-5333 (10:48, idempotency-key/retry-safe submit handler) broke order submit, so the 30% failure rate is a bad release.

**Causal chain**
(none given)

**Supporting evidence**
  - `F1` ✔ order-service error_rate was ~0.2% until the 10:51 bucket, then jumped to avg 13.07% and stayed ~31% through 12:45 (last 30.39%).
  - `F12` ✔ The only order-service deployment in the window is dep-5333 (v2.15.2, 10:48, 'Add idempotency keys to order submit'), status rolled_back at 11:16.
**Contradicting evidence**
  - `F2` ✔ order-service errors are exclusively 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', 349 matches from 11:02:45 onward.
  - `F3` ✔ exchange-gateway logs are dominated by partner-facing failures: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder/quoteSnapshot' and 'exchange-api returned 503 Service Unavailable', first appearing 11:01:25.
  - `F5` ✔ exchange-gateway upstream_error_rate (~0.3% baseline) jumped to 42.39% avg at the 11:00 bucket and stayed ~45% through 12:45.
  - `F6` ✔ exchange-gateway upstream_latency_ms (~120ms baseline) jumped to 8550ms avg at the 11:00 bucket and stayed ~9200ms through 12:45.
  - `F15` ✔ exchange-gateway partner errors persist to the latest data: 116 ERRORs between 12:30 and 12:58, still 'exchange-api returned 503' at 12:58:32.
**Rejected because:** The deploy was rolled back at 11:16 yet the error rate did NOT recover (still ~31% at 12:45), and the failing error signatures are all partner-facing (exchange-gateway 504 / upstream connect timeout host=api.exchange-partner.example), not local to order-service code. Timing is coincidental.

### H3: Capacity saturation / resource exhaustion on our services
Traffic surged beyond provisioned capacity, exhausting exchange-gateway or order-service resources, producing timeouts.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ order-service db_pool_in_use stayed flat (~14 connections) across the whole window; no pool exhaustion.
  - `F9` ✔ order-service cpu_pct stayed flat ~40% across the window; no local CPU saturation.
  - `F10` ✔ exchange-gateway request_rate stayed flat ~272 req/s (no traffic surge) and restarts stayed 0 across the window.
  - `F11` ✔ exchange-gateway memory_pct stayed flat ~44% across the window.
**Rejected because:** request_rate flat ~272 req/s (no surge), cpu ~40% (order-service), memory ~44% (exchange-gateway), restarts 0, db_pool_in_use flat ~14. No resource near limits.

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- exchange-gateway's upstream_error_rate and upstream_latency_ms refer to the external partner api.exchange-partner.example (not an internal dependency).
- The partner outage is externally caused and not a downstream effect of a change on our side (e.g. credential/cert change) — no evidence of a credential event was found, but the partner side has no telemetry.

## Timeline
- `2026-03-17T09:20:00Z` pricing-engine config change cfg-2290 (cache.ttl_seconds 5->4) — before onset, no related errors [F13]
- `2026-03-17T10:48:00Z` order-service v2.15.2 (dep-5333, idempotency keys) deployed [F12]
- `2026-03-17T11:01:25Z` First exchange-gateway partner error: upstream connect timeout to api.exchange-partner.example [F3]
- `2026-03-17T11:02:45Z` First order-service 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' [F2]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker OPEN for exchange-api (failure ratio 0.47) [F4]
- `2026-03-17T11:16:00Z` order-service v2.15.2 rolled back — error rate does NOT recover [F12, F1]
- `2026-03-17T12:45:00Z` order-service error_rate still ~31%; exchange-gateway partner errors persist to 12:58 [F1, F15]

## Recommended actions
- **[immediate]** Stop treating the order-service rollback as the fix; engage the exchange partner (api.exchange-partner.example) / vendor to confirm and resolve their outage, and request status. -- Errors continue after rollback and all failing signatures point at the external partner. [F3, F4, F15]
- **[immediate]** Fail over to the secondary exchange route (as in HIST-103) or apply graceful degradation/fallback for placeOrder/quoteSnapshot. -- Removes customer-facing 504s while the partner is degraded. [F14]
- **[short_term]** Tune exchange-gateway resilience: lower connect/read timeouts and circuit-breaker thresholds so external degradation sheds fast instead of blocking order-service threads for 5s. -- order-service latency rose to ~5.3s, matching the 5000ms upstream connect timeout. [F6, F7]
- **[short_term]** Add synthetic/health probing and alerting on exchange-api partner error rate & latency so partner outages are detected before they surface as order 5xx. -- Partner errors began 11:01 but only surfaced as an order-service SEV-1. [F3, F5]
- **[long_term]** Improve deploy/incident correlation so coincidental releases are not rolled back without checking whether the rollback restores the symptom. -- The coincidental 10:48 deploy led to a rollback that had no effect on the incident. [F12, F1]

## Data gaps
- exchange-api (external vendor) has no telemetry (has_telemetry:false), so its internal failure mode cannot be observed directly — partner confirmation required.
- Several tool calls failed after retries (tc-005 order-service logs, tc-011 pricing-engine latency, tc-018 exchange-gateway logs 10:40-11:20, tc-023 exchange-gateway cpu), so those specific series were not obtained; restarts/memory/request_rate cover the resource question.
- Whether a credential/cert change (the 10:04:50 'deprecated TLS cipher offered by client' WARN in exchange-gateway) contributed to the partner rejection is unresolved.

## Investigation stats
- latency 106.55s, LLM calls 9, tokens 143732 (in 130729 / out 13003)
- tool calls 24 (4 failed, 0 partial, 1 recovered by retry), findings 15/15 verified
