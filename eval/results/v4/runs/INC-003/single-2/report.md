# Incident investigation report: INC-003
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order submit failures (~31%) began at ~11:00-11:02 and persist through the latest data (12:58), caused by the external exchange provider (api.exchange-partner.example) degrading. exchange-gateway logs show upstream connect timeouts (5000ms) and 503 Service Unavailable from the partner beginning 11:01:25, and exchange-gateway upstream_latency jumped from ~120ms to ~9200ms while its own CPU/memory/request-rate stayed flat. order-service errors are purely propagated ("exchange-gateway call failed: 504 Gateway Timeout"). The order-service v2.15.2 deploy (10:48) was rolled back at 11:16 with NO recovery, and order-service failures started after exchange-gateway's — so the deploy is not the cause. This matches historical dependency_failure incident HIST-103. Root cause originates at the external exchange partner.

## Probable root cause
**External exchange partner API degradation** -- dependency_failure in `exchange-api` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange partner API degradation | dependency_failure | exchange-api | - | 0.90 (high) | active |
| 2 | H4: resource_exhaustion in exchange-gateway | resource_exhaustion | exchange-gateway | - | 0.05 (low) | rejected |
| 3 | H2: order-service v2.15.2 release introduced the fault | bad_deployment | order-service | dep-5333 | 0.04 (low) | rejected |
| 4 | H3: config_change: pricing-engine cache TTL | config_change | pricing-engine | cfg-2290 | 0.03 (low) | rejected |

### H1: External exchange partner API degradation
The third-party exchange provider (api.exchange-partner.example) started timing out/returning 503 at ~11:01. exchange-gateway's calls to it hang ~5s and fail ~46%, so placeOrder requests fail as 504s at order-service. exchange-gateway itself is healthy, so the fault originates at the external dependency.

**Causal chain**
1. External exchange provider starts timing out / returning 503 (~11:01) [F3]
2. exchange-gateway upstream latency jumps 120ms -> ~9200ms and upstream error rate -> ~46% [F4, F5]
3. order-service placeOrder calls to exchange-gateway exceed timeout and return 504 [F1]
4. order-service error rate -> ~31%; api-gateway and market-data-service also degrade [F2, F9]

**Supporting evidence**
  - `F3` ✔ exchange-gateway errors begin at 11:01:25 (earlier than order-service) and are upstream failures: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' and 'exchange-api returned 503 Service Unavailable'.
  - `F4` ✔ exchange-gateway upstream_latency_ms jumped from ~120ms to ~8248ms at 11:00 and remained ~9200ms through 12:50.
  - `F5` ✔ exchange-gateway upstream_error_rate rose from ~0.3% to ~46% at 11:00 and stayed elevated.
  - `F6` ✔ exchange-gateway's own resources were flat/healthy: cpu_pct ~30% and request_rate ~272 req/s unchanged across the incident window.
  - `F1` ✔ order-service first error appears at 11:02:45 and the only error pattern is 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' (349 matches).
  - `F2` ✔ order-service error_rate was ~0.2% until 10:50, then jumped to 24.64% at 11:00 and stayed ~31% through 12:50.
  - `F9` ✔ api-gateway error_rate rose from ~0.2% to ~6% at 11:00 and stayed elevated, and market-data-service logged 'stale quotes: exchange feed lag ... via exchange-gateway' from 11:02:59 — showing the impact spans multiple exchange-gateway consumers.
  - `F11` ✔ Historical incident HIST-103 (2025-10-21) is the same pattern: order-service/exchange-gateway 504 on order submit, root cause 'External exchange API outage on the partner side', category dependency_failure.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Logs mentioning host=api.exchange-partner.example and 'exchange-api returned 503' reflect genuine partner-side failures, not a local network/DNS fault misattributed to the partner
  - exchange-api has no telemetry (has_telemetry=false), so partner-side evidence is only visible indirectly via exchange-gateway

### H4: resource_exhaustion in exchange-gateway
exchange-gateway's own resources (CPU/memory/connections) were exhausted, causing timeouts.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ exchange-gateway's own resources were flat/healthy: cpu_pct ~30% and request_rate ~272 req/s unchanged across the incident window.
  - `F3` ✔ exchange-gateway errors begin at 11:01:25 (earlier than order-service) and are upstream failures: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' and 'exchange-api returned 503 Service Unavailable'.
  - `F4` ✔ exchange-gateway upstream_latency_ms jumped from ~120ms to ~8248ms at 11:00 and remained ~9200ms through 12:50.
**Rejected because:** exchange-gateway CPU (~30%), memory (~44%) and request rate (~272 req/s) were flat across the incident; failures are explicitly upstream to api.exchange-partner.example.

### H2: order-service v2.15.2 release introduced the fault
The idempotency-key change in order-service v2.15.2 caused submit failures.

**Causal chain**
(none given)

**Supporting evidence**
  - `F7` ✔ order-service deployment dep-5333 (v2.15.2, 'Add idempotency keys to order submit') went out 10:48 and was rolled back at 11:16.
**Contradicting evidence**
  - `F3` ✔ exchange-gateway errors begin at 11:01:25 (earlier than order-service) and are upstream failures: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' and 'exchange-api returned 503 Service Unavailable'.
  - `F8` ✔ After the 11:16 rollback, exchange-gateway errors continued unchanged through 12:58 (412 ERROR matches), and order-service error_rate stayed ~31% — no recovery.
  - `F1` ✔ order-service first error appears at 11:02:45 and the only error pattern is 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' (349 matches).
**Rejected because:** Errors began at exchange-gateway (11:01:25) before order-service's first error (11:02:45); order-service errors are purely propagated exchange-gateway 504s; and although v2.15.2 was rolled back at 11:16, failures continued unchanged through 12:58 with no recovery. A deploy-caused fault would have stopped at rollback.

### H3: config_change: pricing-engine cache TTL
Shorter pricing quote cache TTL (cfg-2290) caused failures.

**Causal chain**
(none given)

**Supporting evidence**
  - `F10` ✔ Only other change in window: pricing-engine config cfg-2290 cache.ttl_seconds 5->4 at 09:20; pricing-engine produced zero WARN/ERROR logs and no degrade.
**Contradicting evidence**
  - `F3` ✔ exchange-gateway errors begin at 11:01:25 (earlier than order-service) and are upstream failures: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' and 'exchange-api returned 503 Service Unavailable'.
  - `F4` ✔ exchange-gateway upstream_latency_ms jumped from ~120ms to ~8248ms at 11:00 and remained ~9200ms through 12:50.
**Rejected because:** The change was at 09:20, ~100 min before onset; pricing-engine logged zero errors/warnings; order-service errors are tied to the exchange path, not pricing; and the ordering of timings does not fit.

## Facts (verified observations)
- order-service's only error pattern is propagated exchange-gateway 504 Gateway Timeout on placeOrder, starting 11:02:45. [F1]
- order-service error rate jumped to ~31% at 11:00 and stayed elevated through 12:50. [F2]
- exchange-gateway upstream failures (connect timeout to api.exchange-partner.example and 503 from exchange-api) began at 11:01:25, before order-service errors. [F3]
- exchange-gateway upstream latency rose from ~120ms to ~9200ms at 11:00 and stayed there. [F4]
- exchange-gateway upstream error rate rose from ~0.3% to ~46%. [F5]
- exchange-gateway's own CPU (~30%) and request rate (~272 req/s) were flat throughout, ruling out local overload. [F6]
- order-service v2.15.2 (dep-5333) was deployed at 10:48 and rolled back at 11:16. [F7]
- Failures continued unchanged for ~1h40m after the rollback (through 12:58), so the rollback did not restore service. [F8]
- The impact spans multiple exchange-gateway consumers: api-gateway error_rate ~6% and market-data-service stale-quote warnings. [F9]
- The only other change (pricing-engine cfg-2290, cache TTL 5->4s at 09:20) had no correlating errors (pricing-engine logged zero WARN/ERROR). [F10]
- A prior incident (HIST-103) with identical symptoms was attributed to an external exchange partner outage (dependency_failure). [F11]

## Assumptions (not verified)
- exchange-api (external) has no direct telemetry (has_telemetry=false), so partner-side health is inferred solely from exchange-gateway logs/metrics
- The 'api.exchange-partner.example' host and 'exchange-api returned 503' messages genuinely indicate a partner-side failure rather than a local network/DNS/certificate fault on our side
- No other unrecorded change occurred on the external network path

## Timeline
- `2026-03-17T09:20:00Z` pricing-engine config cfg-2290 (cache.ttl_seconds 5->4) applied (unrelated) [F10]
- `2026-03-17T10:48:00Z` order-service v2.15.2 deployed (idempotency keys) [F7]
- `2026-03-17T11:00:00Z` order-service, exchange-gateway, api-gateway error rates and exchange-gateway upstream latency spike [F2, F4, F5, F9]
- `2026-03-17T11:01:25Z` First exchange-gateway upstream connect timeout to api.exchange-partner.example [F3]
- `2026-03-17T11:02:45Z` First order-service 'exchange-gateway call failed: 504' error [F1]
- `2026-03-17T11:02:59Z` market-data-service warns of stale quotes via exchange-gateway [F9]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker OPEN for exchange-api [F3]
- `2026-03-17T11:16:00Z` order-service v2.15.2 rolled back — no recovery [F7, F8]
- `2026-03-17T12:58:32Z` exchange-gateway still returning 503s from exchange-api (ongoing) [F3, F8]

## Recommended actions
- **[immediate]** Engage the exchange provider / vendor and confirm the api.exchange-partner.example outage; request status and ETA -- Failures are entirely on the partner-side path; only the partner can fix the root cause. [F3, F4, F5]
- **[immediate]** Fail over to a secondary exchange route / enable circuit-breaker fallback to route around the degraded partner (as in HIST-103) -- Removes customer-facing impact while the partner recovers. [F11, F3]
- **[short_term]** Do NOT roll back or chase the order-service deploy as the cause; keep v2.15.2 or re-deploy if desired since rollback showed no benefit -- Rollback at 11:16 produced no recovery; the deploy is not the fault. [F7, F8]
- **[short_term]** Add telemetry/alerting on exchange-gateway upstream_error_rate and upstream_latency_ms to detect partner degradation sooner -- exchange-api has no telemetry; the partner outage was only visible indirectly. [F5, F4]
- **[long_term]** Tune timeouts/retries and add graceful degradation for order submit when the exchange partner is down (queue or reject fast with clear messaging) -- Reduces blast radius of future partner outages across order-service, api-gateway and market-data-service. [F1, F9]

## Data gaps
- No direct telemetry or logs from the external exchange provider (exchange-api) — partner-side root cause is inferred, not observed.
- No network/DNS-level data for the host api.exchange-partner.example (e.g. TLS handshake failures or DNS resolution) to fully exclude a local connectivity fault masquerading as a partner outage.
- No deployment history found for exchange-gateway (count=0), so we cannot fully exclude an unrecorded gateway-side configuration/network change on our side.

## Investigation stats
- latency 63.33s, LLM calls 6, tokens 82912 (in 75584 / out 7328)
- tool calls 20 (0 failed, 0 partial, 0 recovered by retry), findings 11/11 verified
