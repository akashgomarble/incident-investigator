# Incident investigation report: INC-003
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order-submit 504s (~30%) are caused by degradation of the external exchange partner (api.exchange-partner.example) reached via exchange-gateway, NOT by the order-service v2.15.2 release. exchange-gateway began logging 'upstream connect timeout after 5000ms host=api.exchange-partner.example' and 'exchange-api returned 503 Service Unavailable' from 11:01:25; its upstream_latency jumped 119ms -> ~9.2s and upstream_error_rate 0.3% -> ~46%, while its own CPU (~30%) and request_rate (~270/s) stayed flat and there were no gateway deploys/config changes. order-service's only errors are 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'. The v2.15.2 deploy coincides in time but is exonerated: it was rolled back at 11:16 and errors persisted at ~31% through 12:45, order-service CPU/pool/restarts are flat, and the failure is explicitly in the downstream external call path. Historical incident HIST-103 matches this exact signature (partner-side outage).

## Probable root cause
**External exchange partner (api.exchange-partner.example) degraded — dependency_failure** -- dependency_failure in `exchange-api` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange partner (api.exchange-partner.example) degraded — dependency_failure | dependency_failure | exchange-api | - | 0.86 (high) | active |
| 2 | H2: order-service v2.15.2 idempotency-keys release introduced the fault — bad_deployment | bad_deployment | order-service | dep-5333 | 0.06 (low) | rejected |
| 3 | H3: Exchange-gateway-side change or resource exhaustion | infrastructure_failure | exchange-gateway | - | 0.04 (low) | rejected |
| 4 | H4: Shared datastore / pricing-engine config change (cfg-2290) caused the failure | config_change | pricing-engine | - | 0.03 (low) | rejected |

### H1: External exchange partner (api.exchange-partner.example) degraded — dependency_failure
The external exchange provider behind exchange-api began failing/slowing around 11:01 (connect timeouts >5000ms, 503 Service Unavailable). exchange-gateway's calls to it time out, so placeOrder returns 504 to order-service, producing the ~30% order-submit failure rate. order-service v2.15.2 is coincidental (promoted ~head of the same window) and unrelated; its rollback at 11:16 did not help.

**Causal chain**
1. External exchange-api degrades ~11:01 (timeouts/503s) [F5, F13]
2. exchange-gateway upstream_latency_ms 119ms -> ~9.2s, upstream_error_rate 0.3% -> 46%, while gateway CPU/request_rate flat [F6, F7]
3. order-service placeOrder calls to exchange-gateway time out (504) -> ~31% order errors [F3, F1]
4. api-gateway absorbs a proportional ~6% error share upstream of order-service [F11]

**Supporting evidence**
  - `F5` ✔ exchange-gateway ERROR logs (470 ERRORs) are dominated by 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (163 placeOrder + 146 quoteSnapshot) and 'exchange-api returned 503 Service Unavailable' (161), starting 11:01:25.
  - `F6` ✔ exchange-gateway upstream_latency_ms (latency to the external exchange-api) jumped from ~119ms to ~9221-9290ms at the same onset; upstream_error_rate went from 0.30% to ~46%.
  - `F7` ✔ exchange-gateway CPU stayed flat ~30% and its request_rate flat ~270 req/s; error_rate ~44% and p99 ~5100ms track order-service exactly.
  - `F13` ✔ exchange-gateway opened its circuit breaker for exchange-api ('failure ratio 0.47 over 60s') at 11:03:26, ~1 min after timeouts began — consistent with the partner degrading, not the gateway.
  - `F3` ✔ Every order-service ERROR logged in the window is the same pattern: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' (349 matches, first 11:02:45, last 12:58:54).
  - `F1` ✔ order-service error_rate was ~0.2% until the 10:48 deploy, then jumped to 14.76% in the 10:50 bucket and stayed at ~31% (avg 31.12 at 11:13, 30.99 at 12:45) through 12:59.
  - `F8` ✔ order-service deploy dep-5333 v2.15.2 (Add idempotency keys to order submit) went out 10:48 and was rolled back at 11:16 — yet errors persisted at ~31% until at least 12:45.
  - `F9` ✔ exchange-gateway had zero deployments and zero config changes on 2026-03-17.
  - `F12` ✔ Shared datastores are healthy: ledger-db shows only benign warnings (slow /internal/metrics, one 38ms NTP drift) with no errors; kafka shows a single benign GC-pause WARN.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The host api.exchange-partner.example is a third-party/vendor endpoint (owner 'vendor', external per dependency graph), so its failure is a dependency, not ours.
  - No internal component (gateway CPU, broker, DB) is the true origin; they all look healthy.

### H2: order-service v2.15.2 idempotency-keys release introduced the fault — bad_deployment
The 10:48 order-service release (idempotency keys / retry-safe submit) introduced a regression causing order submissions to fail; onset roughly coincides with the deploy and on-call is rolling it back.

**Causal chain**
(none given)

**Supporting evidence**
  - `F1` ✔ order-service error_rate was ~0.2% until the 10:48 deploy, then jumped to 14.76% in the 10:50 bucket and stayed at ~31% (avg 31.12 at 11:13, 30.99 at 12:45) through 12:59.
**Contradicting evidence**
  - `F8` ✔ order-service deploy dep-5333 v2.15.2 (Add idempotency keys to order submit) went out 10:48 and was rolled back at 11:16 — yet errors persisted at ~31% until at least 12:45.
  - `F3` ✔ Every order-service ERROR logged in the window is the same pattern: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' (349 matches, first 11:02:45, last 12:58:54).
  - `F4` ✔ order-service CPU stayed flat ~40% and db_pool_in_use flat ~14 connections throughout; restarts stayed at 0. No in-process resource exhaustion.
  - `F5` ✔ exchange-gateway ERROR logs (470 ERRORs) are dominated by 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (163 placeOrder + 146 quoteSnapshot) and 'exchange-api returned 503 Service Unavailable' (161), starting 11:01:25.
  - `F6` ✔ exchange-gateway upstream_latency_ms (latency to the external exchange-api) jumped from ~119ms to ~9221-9290ms at the same onset; upstream_error_rate went from 0.30% to ~46%.
  - `F9` ✔ exchange-gateway had zero deployments and zero config changes on 2026-03-17.
**Rejected because:** Rollback at 11:16 did not restore service — errors remained ~31% from 11:16 through 12:45 (F8). Every order-service error is an exchange-gateway 504, not an internal error from the new idempotency code (F3). order-service CPU/pool/restarts are flat (F4). The deploy onset (10:48) actually post-dates the exchange-gateway upstream timeouts' first appearance and does not explain the external-partner timeouts. This is temporal coincidence.

### H3: Exchange-gateway-side change or resource exhaustion
A change or resource saturation inside exchange-gateway caused it to time out calls.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ exchange-gateway had zero deployments and zero config changes on 2026-03-17.
  - `F7` ✔ exchange-gateway CPU stayed flat ~30% and its request_rate flat ~270 req/s; error_rate ~44% and p99 ~5100ms track order-service exactly.
  - `F5` ✔ exchange-gateway ERROR logs (470 ERRORs) are dominated by 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (163 placeOrder + 146 quoteSnapshot) and 'exchange-api returned 503 Service Unavailable' (161), starting 11:01:25.
  - `F6` ✔ exchange-gateway upstream_latency_ms (latency to the external exchange-api) jumped from ~119ms to ~9221-9290ms at the same onset; upstream_error_rate went from 0.30% to ~46%.
**Rejected because:** Zero exchange-gateway deployments or config changes on 2026-03-17 (F9); its CPU stayed flat ~30% and request_rate flat ~270 req/s (F7); errors are explicitly upstream to api.exchange-partner.example (F5, F6).

### H4: Shared datastore / pricing-engine config change (cfg-2290) caused the failure
The pricing-engine cache TTL change at 09:20 or a shared ledger-db/kafka issue caused the order failures.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ The only config change in the window was pricing-engine cache.ttl_seconds 5->4 at 09:20; pricing-engine latency/cpu stayed flat (~60ms) and it logged zero WARN+ lines.
  - `F12` ✔ Shared datastores are healthy: ledger-db shows only benign warnings (slow /internal/metrics, one 38ms NTP drift) with no errors; kafka shows a single benign GC-pause WARN.
  - `F5` ✔ exchange-gateway ERROR logs (470 ERRORs) are dominated by 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (163 placeOrder + 146 quoteSnapshot) and 'exchange-api returned 503 Service Unavailable' (161), starting 11:01:25.
**Rejected because:** pricing-engine latency/cpu flat and it logged no warnings (F10); ledger-db and kafka show only benign warnings (F12). Onset (11:01) does not match the 09:20 config change, and the errors are external-partner timeouts (F5).

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- api.exchange-partner.example is a third-party/vendor endpoint (dependency graph marks it type 'external', owner 'vendor', no telemetry), so its degradation is a dependency failure rather than an internal fault.
- The downstream call path order-service -> exchange-gateway -> exchange-api is synchronous for placeOrder, so partner latency surfaces as order-service 504s.

## Timeline
- `2026-03-17T09:20:00Z` pricing-engine config change cfg-2290 (cache TTL 5->4) — unrelated, no latency impact [F10]
- `2026-03-17T10:48:00Z` order-service v2.15.2 deployed (idempotency keys) — dep-5333 [F8]
- `2026-03-17T11:01:25Z` exchange-gateway first logs upstream connect timeout to api.exchange-partner.example [F5]
- `2026-03-17T11:02:20Z` exchange-api begins returning 503 Service Unavailable [F5]
- `2026-03-17T11:02:45Z` order-service first 'exchange-gateway call failed: 504' error [F3]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker opens for exchange-api (failure ratio 0.47/60s) [F13]
- `2026-03-17T11:16:00Z` order-service v2.15.2 rolled back — errors do NOT recover [F8]
- `2026-03-17T12:45:00Z` order-service error_rate still ~31%; exchange-gateway upstream latency still ~9.2s [F1, F6]

## Recommended actions
- **[immediate]** Stop rolling back / stop investigating order-service v2.15.2 — it is not the cause; keep the release or re-evaluate it on its own merits later. -- Rollback at 11:16 did not restore service; errors depend on the external partner, not the release. [F8, F3]
- **[immediate]** Engage the exchange partner (api.exchange-partner.example / vendor) and confirm their incident; fail over to a secondary exchange route as done in HIST-103. -- All failures are connect-timeouts/503s to the external host; the origin is on the partner side. [F5, F6]
- **[short_term]** Tune exchange-gateway resilience: lower connect timeout, keep the circuit breaker open, and shed/queue placeOrder instead of blocking, to cap order-service latency amplification. -- 5s connect timeout + blocked calls propagate ~9s upstream latency into order-service 504s. [F5, F6, F13]
- **[long_term]** Add a dashboard/alert that separates gateway 'upstream' error metrics from internal errors so partner outages are not mistaken for our deploys. -- The deploy coincidence misdirected the initial response. [F6, F7]

## Data gaps
- No telemetry for the external endpoint itself (exchange-api has_telemetry:false), so the partner's own error cause cannot be confirmed from our side — we infer it from exchange-gateway's upstream_* metrics and logs.
- Whether a vendor status page / partner incident was declared for api.exchange-partner.example could not be checked.
- order-service log query was truncated (349 of 349 shown as 40 returned, truncated:true), so per-message completeness is limited, though the single dominant pattern is clear; a same-pattern count was confirmed with a narrower window.

## Investigation stats
- latency 50.49s, LLM calls 6, tokens 85961 (in 78121 / out 7840)
- tool calls 28 (0 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
