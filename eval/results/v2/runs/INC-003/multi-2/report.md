# Incident investigation report: INC-003
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The ~30% order-submit failure rate is caused by degradation of the external exchange partner api.exchange-partner.example: order-service is a victim of a downstream dependency failure, not of its own v2.15.2 release. Confidence is medium (0.55, top-ranked H2) — the causal chain order-service → exchange-gateway → partner is verified hop by hop, but no telemetry exists for the partner itself (has_telemetry=false), so the deepest link is inferred from gateway outbound failures. The order-service v2.15.2 hypothesis (H1) is correctly rejected at 0.05: the failing component sits downstream of order-service (gateway first error 11:01:25Z precedes order-service's 11:02:45Z), the deploy ran clean for ~13 minutes, and 100% of order-service errors are downstream 504s on the exchange-gateway placeOrder call. Immediate action: the already-initiated rollback of dep-5333 (which will not fix the problem) should be completed for hygiene, but the actual mitigation is at the partner/connectivity layer — engage the partner and treat the circuit breaker as the protective stopgap.

## Probable root cause
**External exchange partner (api.exchange-partner.example) degraded — order-service is a victim of a dependency failure** -- dependency_failure in `exchange-partner (api.exchange-partner.example)`, triggered by `null` (confidence 0.55, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: External exchange partner (api.exchange-partner.example) degraded — order-service is a victim of a dependency failure | dependency_failure | exchange-partner (api.exchange-partner.example) | null | 0.55 (medium) | active |
| 2 | H3: Shared gateway/mesh or platform networking egress path between exchange-gateway and the partner failed | infrastructure_failure | shared network/mesh egress path (exchange-gateway <-> external partner) | null | 0.32 (low) | active |
| 3 | H1: order-service v2.15.2 introduced the fault directly | bad_deployment | order-service | dep-5333 | 0.05 (low) | rejected |
| 4 | H4: Traffic surge or resource exhaustion (connection pool / thread saturation) causing timeouts independent of the release | resource_exhaustion | order-service / exchange-gateway runtime | null | 0.03 (low) | rejected |
| 5 | H5: pricing-engine cache TTL change (cfg-2290, 09:20Z) contributed to degraded submit path | config_change | pricing-engine | cfg-2290 | 0.02 (low) | rejected |

### H2: External exchange partner (api.exchange-partner.example) degraded — order-service is a victim of a dependency failure
The external exchange partner endpoint api.exchange-partner.example stopped accepting/answering connections around 11:01Z; exchange-gateway's outbound 5s connect timeouts (placeOrder and quoteSnapshot) escalate into gateway 5xx, which propagate up to order-service's placeOrder path and produce ~30% submit failures.

**Causal chain**
1. api.exchange-partner.example stops accepting/answering connections: gateway logs 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (placeOrder + quoteSnapshot) plus 'exchange-api returned 503'; earliest 11:01:25Z [F10, F17, F25, F26]
2. The delay is localized to the gateway's outbound leg: exchange-gateway upstream_latency_ms 121ms -> ~8248/9200ms while its inbound error onset and flat request_rate (~269-281 req/s) show no local overload [F11, F9, F27]
3. Gateway circuit breaker opens for exchange-api at 11:03:26Z and gateway error_rate goes 0.2% -> ~32-44% [F10, F17, F9]
4. order-service's placeOrder calls hang ~5s and return 504, raising its p99 to ~4.1-5.4s and error_rate 0.2% -> ~31%; 100% of its errors are the downstream 504 signature [F7, F12, F14, F16, F8, F31]
5. Other non-order-service callers of the gateway degrade in the same bucket, consistent with a shared failing dependency rather than an order-service fault [F28, F29]
6. Reported symptom: order submit failure rate ~30% with 504s [F13]

**Supporting evidence**
  - `F7` ✔ Every error logged by order-service in the window is a downstream timeout to exchange-gateway, not an internal failure: 349/349 ERROR entries are the single pattern 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', first at 11:02:45Z and last at 12:58:54Z.
  - `F8` ✔ order-service error_rate was flat at ~0.2% through the 10:50 bucket (avg 0.22) then jumped to 24.64% in the 11:00 bucket and held ~31% afterward (12:50 avg 31.28).
  - `F9` ✔ exchange-gateway's own latency spiked at the same onset: p99 avg 130.93ms in the 10:50 bucket → 4625.9ms in the 11:00 bucket → ~5,000ms+ sustained; its error_rate went 0.22% → 39.33% (11:00) → ~44% sustained, with request_rate flat (~269–281 req/s) across the whole window.
  - `F10` ✔ The stall is in exchange-gateway's upstream call to api.exchange-partner.example: gateway logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163 occurrences) and op=quoteSnapshot (146), plus 161 'exchange-api returned 503 Service Unavailable', earliest such log at 11:01:25Z, and a WARN 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26Z.
  - `F11` ✔ exchange-gateway's upstream-facing latency (upstream_latency_ms) exploded from ~121ms in the 10:40–10:50 buckets to 8248.65ms avg (max 9644.67) in the 11:00 bucket and ~9,200ms sustained, i.e. the delay is on the exchange-gateway→partner leg, well beyond its own inbound p99.
  - `F12` ✔ order-service's own p99 rose in lockstep with the gateway timeouts, from ~150ms (10:50) to 4285.85ms avg / 5631.42ms max in the 11:00 bucket, consistent with the 5000ms upstream connect timeout rather than an order-service-internal latency source.
  - `F13` ✔ order-service error_rate was at baseline ~0.2% through the 11:00Z bucket and jumped to 31.43% in the 11:02Z bucket (2-minute resolution), placing onset between 11:00 and 11:02Z — before the 11:10 report.
  - `F14` ✔ The earliest order-service ERROR log is 2026-03-17T11:02:45Z and 100% of its 174 ERROR logs in 09:30–12:00 share one signature: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' — the failure is specific to the placeOrder path calling exchange-gateway, not a generic application error.
  - `F16` ✔ order-service latency_p99_ms was ~146–152ms before 11:00 and jumped to ~4136–5427ms from the 11:00Z bucket onward (5747ms max), i.e. requests now hang ~5s — consistent with waiting on an outbound call, starting in the same window as the error onset.
  - `F17` ✔ The downstream dependency exchange-gateway shows its OWN errors starting EARLIER (first at 2026-03-17T11:01:25Z), with 121 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder', 111 quoteSnapshot timeouts, 122 'exchange-api returned 503', and a circuit breaker opening at 11:03:26 (failure ratio 0.47 over 60s). Its error_rate rose from ~0.2% to 31.95% in the 10:58Z bucket.
  - `F18` ✔ exchange-gateway's failures originate at an external host, api.exchange-partner.example, with 5s connect timeouts and 503s — the deepest visible failing component; order-service's 504s (placeOrder) attribute the same failure to that call path. This points to order-service waiting on a downstream/external dependency rather than failing internally.
  - `F25` ✔ exchange-gateway logged 236 WARN+ entries in 10:50–12:00Z, and ALL of them are against a single destination host: two timeout patterns naming 'host=api.exchange-partner.example' (85 op=placeOrder timeouts first at 11:01:25Z, 77 op=quoteSnapshot timeouts first at 11:01:43Z) plus 73 'exchange-api returned 503 Service Unavailable (x-partner-request-id=...)'. No timeout/error naming any other outbound host appears.
  - `F26` ✔ The failure is not confined to the order-submit path: exchange-gateway's own errors cover TWO distinct upstream operations against api.exchange-partner.example, op=placeOrder AND op=quoteSnapshot, i.e. a second route/op beyond order-service's placeOrder call times out to the same partner host.
  - `F27` ✔ exchange-gateway's inbound error_rate jumped from a 0.26%/0.19% baseline (10:50/10:55 buckets) to 34.8% in the 11:00Z bucket and sustained 42–47% through 12:00Z, while its upstream_latency_ms went 120.93ms (10:50) → 7450.9ms avg / 9620.91ms max (11:00) → ~9,000–9,600ms sustained: the degradation is on the gateway's upstream-facing leg, not its inbound side.
  - `F28` ✔ A NON-order-service caller shows elevated failures at the same onset: market-data-service error_rate was ~0.24–0.26% in 10:50/10:55 buckets then jumped to 4.01% (11:00) and ~6.4–6.7% sustained, i.e. it degrades in the same 11:00Z bucket as exchange-gateway/order-service.
  - `F29` ✔ The shared ingress layer shows the same onset: api-gateway error_rate went from 0.19%/0.26% (10:50/10:55) to 3.85% (11:00) and 6.09–6.32% sustained, so the degradation propagated up to the front door, not only on order-service's path. api-gateway emitted no WARN/ERROR logs of its own in 10:50–12:00Z (total_matched=0), so its errors are counted at ingress, not self-logged.
  - `F31` ✔ order-service's own errors in 10:50–12:00Z are exclusively one signature, 174/174 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', first at 11:02:45Z — i.e. order-service's failure is a downstream projection of the gateway's partner timeouts, and its first error (11:02:45Z) lags the gateway's first partner timeout (11:01:25Z).
  - `F32` ✔ The egress faults are isolated to the single partner host and do not span multiple outbound destinations: every timeout signature in the window carries 'host=api.exchange-partner.example', and no error names any other outbound host — consistent with a partner-specific failure rather than a shared mesh/egress path fault affecting several destinations.
  - `F22` ✔ Historical precedent search explicitly for external-partner degradation returns HIST-103 (2025-10-21, match_score 4) whose root cause is 'External exchange API outage on the partner side' with symptoms '504 on order submit' / 'upstream timeouts' — the same service pair (order-service, exchange-gateway) as the current incident.
  - `F24` ✔ Prior external-partner outage precedent (HIST-103, HIST-105 bank-partner-api 5xx for 40 minutes) shows the vendor-side-failure pattern resolves on the partner side ('partner fixed', 'Waited for partner'), not by any internal infra change — corroborating that partner degradation, not a mesh change, is the recurring mechanism for this symptom class.
**Contradicting evidence**
  - `F1` ✔ The only deployment across all services in 2026-03-17T08:00-12:59Z is order-service v2.15.2 (dep-5333, author a.khan, 'Add idempotency keys to order submit') deployed 10:48:00Z, status rolled_back at 11:16:00Z. It is ~22 min before the 11:10Z report and inside the flagged 10:30-11:10 onset window.
**Assumptions**
  - The partner hostname in gateway logs is a genuine external dependency (has_telemetry=false) and the connectivity failure is on the partner or inter-network path, not inside exchange-gateway's own networking stack.
  - quoteSnapshot and placeOrder share the same partner endpoint, so the failure is broad rather than op-specific.
  - F28/F29 count other callers of the gateway (market-data-service, api-gateway), consistent with a shared downstream dependency — though these services' dependency edges to *this* gateway were not fully enumerated (blast-radius gap).
**Rejected because:** null

### H3: Shared gateway/mesh or platform networking egress path between exchange-gateway and the partner failed
The exchange-gateway→external-host connectivity failure is caused by a shared infrastructure component (service mesh/egress proxy/DNS/NAT/network path) failing to establish egress connections, rather than the external partner itself; the partner may be healthy and unreachable only from this path.

**Causal chain**
1. Connect timeouts (connection-establishment failure, not read timeouts) after exactly 5000ms to the partner host are equally consistent with an egress/mesh path fault as with a partner fault [F10, F11]
2. Simultaneous onset across multiple services and the front door (~11:00-11:03Z) is what a shared infrastructure path fault would produce [F9, F17, F13, F28, F29]
3. Failure occurs despite no deployment/config change for exchange-gateway or order-service's dependencies, consistent with a non-service-attributed network change [F1, F3, F20, F19]
4. No platform/network object exists in the change APIs, so a mesh/egress change would be invisible in the records — absence of a change is not evidence of no change [F23, F19]

**Supporting evidence**
  - `F10` ✔ The stall is in exchange-gateway's upstream call to api.exchange-partner.example: gateway logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163 occurrences) and op=quoteSnapshot (146), plus 161 'exchange-api returned 503 Service Unavailable', earliest such log at 11:01:25Z, and a WARN 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26Z.
  - `F11` ✔ exchange-gateway's upstream-facing latency (upstream_latency_ms) exploded from ~121ms in the 10:40–10:50 buckets to 8248.65ms avg (max 9644.67) in the 11:00 bucket and ~9,200ms sustained, i.e. the delay is on the exchange-gateway→partner leg, well beyond its own inbound p99.
  - `F19` ✔ No non-service-attributed infrastructure/network change (service-mesh config, egress proxy/firewall, DNS, VPC/NAT/gateway, load-balancer or route change) is recorded anywhere in the infra/change APIs for the 10:00-11:05Z window: fleet-wide get_deployments returned exactly 1 deployment (order-service v2.15.2, dep-5333, 10:48Z) and fleet-wide get_config_changes returned count 0.
  - `F20` ✔ The shared gateway components show no recorded config change in 08:00-12:59Z: api-gateway config changes count=0 and exchange-gateway config changes count=0, so no mesh/route/LB change is attributed to either gateway in the window.
  - `F23` ✔ The 'infra' pseudo-service name is not a valid scope in the deployment API (known_services lists only application services, DBs and external APIs, no mesh/firewall/DNS/VPC/NAT/LB entity), so there is no service-level object in which a non-service-attributed network change could even be recorded; unknown service 'infra'.
**Contradicting evidence**
  - `F25` ✔ exchange-gateway logged 236 WARN+ entries in 10:50–12:00Z, and ALL of them are against a single destination host: two timeout patterns naming 'host=api.exchange-partner.example' (85 op=placeOrder timeouts first at 11:01:25Z, 77 op=quoteSnapshot timeouts first at 11:01:43Z) plus 73 'exchange-api returned 503 Service Unavailable (x-partner-request-id=...)'. No timeout/error naming any other outbound host appears.
  - `F26` ✔ The failure is not confined to the order-submit path: exchange-gateway's own errors cover TWO distinct upstream operations against api.exchange-partner.example, op=placeOrder AND op=quoteSnapshot, i.e. a second route/op beyond order-service's placeOrder call times out to the same partner host.
  - `F32` ✔ The egress faults are isolated to the single partner host and do not span multiple outbound destinations: every timeout signature in the window carries 'host=api.exchange-partner.example', and no error names any other outbound host — consistent with a partner-specific failure rather than a shared mesh/egress path fault affecting several destinations.
**Assumptions**
  - A mesh/egress/network change occurred but is unrepresentable in the deployment/config APIs (coverage gap noted).
  - The fault would have to be selective to the exchange-partner destination while other egress from the same path stayed healthy — no per-destination metric confirms this.
**Rejected because:** null

### H1: order-service v2.15.2 introduced the fault directly
The v2.15.2 release ('Add idempotency keys to order submit') introduced a defect in order-submit that manifests as 504s and ~30% error rate.

**Causal chain**
1. dep-5333 deployed to order-service at 10:48Z [F1]
2. orders begin failing shortly after, at ~11:02Z [F8, F13]
3. errors are outbound placeOrder 504s, not internal application errors [F7, F14]

**Supporting evidence**
  - `F1` ✔ The only deployment across all services in 2026-03-17T08:00-12:59Z is order-service v2.15.2 (dep-5333, author a.khan, 'Add idempotency keys to order submit') deployed 10:48:00Z, status rolled_back at 11:16:00Z. It is ~22 min before the 11:10Z report and inside the flagged 10:30-11:10 onset window.
  - `F8` ✔ order-service error_rate was flat at ~0.2% through the 10:50 bucket (avg 0.22) then jumped to 24.64% in the 11:00 bucket and held ~31% afterward (12:50 avg 31.28).
  - `F13` ✔ order-service error_rate was at baseline ~0.2% through the 11:00Z bucket and jumped to 31.43% in the 11:02Z bucket (2-minute resolution), placing onset between 11:00 and 11:02Z — before the 11:10 report.
**Contradicting evidence**
  - `F7` ✔ Every error logged by order-service in the window is a downstream timeout to exchange-gateway, not an internal failure: 349/349 ERROR entries are the single pattern 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', first at 11:02:45Z and last at 12:58:54Z.
  - `F10` ✔ The stall is in exchange-gateway's upstream call to api.exchange-partner.example: gateway logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163 occurrences) and op=quoteSnapshot (146), plus 161 'exchange-api returned 503 Service Unavailable', earliest such log at 11:01:25Z, and a WARN 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26Z.
  - `F12` ✔ order-service's own p99 rose in lockstep with the gateway timeouts, from ~150ms (10:50) to 4285.85ms avg / 5631.42ms max in the 11:00 bucket, consistent with the 5000ms upstream connect timeout rather than an order-service-internal latency source.
  - `F14` ✔ The earliest order-service ERROR log is 2026-03-17T11:02:45Z and 100% of its 174 ERROR logs in 09:30–12:00 share one signature: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' — the failure is specific to the placeOrder path calling exchange-gateway, not a generic application error.
  - `F15` ✔ order-service's own CPU is completely flat across the incident: ~39–41% in every bucket from 09:30 to 12:57 (e.g. 39.59% at 11:00, 40.3% at 11:09, 38.96% at 11:18). No resource spike accompanies the failure.
  - `F16` ✔ order-service latency_p99_ms was ~146–152ms before 11:00 and jumped to ~4136–5427ms from the 11:00Z bucket onward (5747ms max), i.e. requests now hang ~5s — consistent with waiting on an outbound call, starting in the same window as the error onset.
  - `F17` ✔ The downstream dependency exchange-gateway shows its OWN errors starting EARLIER (first at 2026-03-17T11:01:25Z), with 121 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder', 111 quoteSnapshot timeouts, 122 'exchange-api returned 503', and a circuit breaker opening at 11:03:26 (failure ratio 0.47 over 60s). Its error_rate rose from ~0.2% to 31.95% in the 10:58Z bucket.
  - `F18` ✔ exchange-gateway's failures originate at an external host, api.exchange-partner.example, with 5s connect timeouts and 503s — the deepest visible failing component; order-service's 504s (placeOrder) attribute the same failure to that call path. This points to order-service waiting on a downstream/external dependency rather than failing internally.
  - `F31` ✔ order-service's own errors in 10:50–12:00Z are exclusively one signature, 174/174 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', first at 11:02:45Z — i.e. order-service's failure is a downstream projection of the gateway's partner timeouts, and its first error (11:02:45Z) lags the gateway's first partner timeout (11:01:25Z).
**Rejected because:** Contradicted by mechanism and ordering. 100% of order-service errors are 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' (F7, F14, F31), i.e. an outbound wait; order-service CPU is flat ~39-41% (F15) and its p99 ~4-5s matches the 5000ms upstream connect timeout (F12, F16). Decisively, exchange-gateway's own upstream leg to api.exchange-partner.example fails from 11:01:25Z, EARLIER than order-service's first error at 11:02:45Z (F10, F17, F31), so the failing component sits downstream of order-service. The 10:48Z deploy produced no failures for ~13 minutes and exchange-gateway received no deployment (F1, F3).

### H4: Traffic surge or resource exhaustion (connection pool / thread saturation) causing timeouts independent of the release
A traffic surge or resource exhaustion (order-service or exchange-gateway connection/thread pool, or shared ledger-db saturation) caused 504s independent of the release.

**Causal chain**
1. No load or resource spike accompanies the failure [F9, F15]
2. The failing leg is exchange-gateway's outbound call to a single external host, not any inbound or local saturation [F10, F11, F32]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ exchange-gateway's own latency spiked at the same onset: p99 avg 130.93ms in the 10:50 bucket → 4625.9ms in the 11:00 bucket → ~5,000ms+ sustained; its error_rate went 0.22% → 39.33% (11:00) → ~44% sustained, with request_rate flat (~269–281 req/s) across the whole window.
  - `F10` ✔ The stall is in exchange-gateway's upstream call to api.exchange-partner.example: gateway logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163 occurrences) and op=quoteSnapshot (146), plus 161 'exchange-api returned 503 Service Unavailable', earliest such log at 11:01:25Z, and a WARN 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26Z.
  - `F11` ✔ exchange-gateway's upstream-facing latency (upstream_latency_ms) exploded from ~121ms in the 10:40–10:50 buckets to 8248.65ms avg (max 9644.67) in the 11:00 bucket and ~9,200ms sustained, i.e. the delay is on the exchange-gateway→partner leg, well beyond its own inbound p99.
  - `F15` ✔ order-service's own CPU is completely flat across the incident: ~39–41% in every bucket from 09:30 to 12:57 (e.g. 39.59% at 11:00, 40.3% at 11:09, 38.96% at 11:18). No resource spike accompanies the failure.
  - `F32` ✔ The egress faults are isolated to the single partner host and do not span multiple outbound destinations: every timeout signature in the window carries 'host=api.exchange-partner.example', and no error names any other outbound host — consistent with a partner-specific failure rather than a shared mesh/egress path fault affecting several destinations.
**Rejected because:** exchange-gateway request_rate is flat (~269-281 req/s) and order-service CPU is flat ~39-41% (F9, F15); the failing leg is specifically the gateway's outbound call to one external host (F10, F11, F32). No load spike or resource metric supports saturation, and onset is simultaneous across services, matching a shared external dependency rather than per-service resource pressure.

### H5: pricing-engine cache TTL change (cfg-2290, 09:20Z) contributed to degraded submit path
The pricing-engine cache.ttl_seconds 5->4 change at 09:20Z increased pricing-engine load/latency and, because pricing-engine sits on order-service's submit path, contributes to order-submit failures.

**Causal chain**
1. cfg-2290 precedes onset by ~88 minutes with no failure during that quiet gap [F6, F8, F13]
2. pricing-engine's own error_rate stays flat ~0.17-0.28% across the incident — no degradation on its leg [F30]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ The pricing-engine TTL change (09:20Z) precedes order-service's deploy (10:48Z) by ~88 min and precedes the reported onset window, and pricing-engine sits on order-service's submit path (order-service→pricing-engine http, pricing-engine→redis-cache cache, pricing-engine→market-data-service). It is a candidate contributor on the request path despite being outside the tight 10:30-11:10 window.
  - `F8` ✔ order-service error_rate was flat at ~0.2% through the 10:50 bucket (avg 0.22) then jumped to 24.64% in the 11:00 bucket and held ~31% afterward (12:50 avg 31.28).
  - `F13` ✔ order-service error_rate was at baseline ~0.2% through the 11:00Z bucket and jumped to 31.43% in the 11:02Z bucket (2-minute resolution), placing onset between 11:00 and 11:02Z — before the 11:10 report.
  - `F30` ✔ pricing-engine is a counter-control on the same submit path: its error_rate stayed flat at ~0.17–0.28% across 10:50–12:00Z (e.g. 0.2 in the 11:00 bucket, 0.19 at 12:00), so the 11:00Z degradation is specific to the exchange-partner egress leg and not a fleet-wide/request-path-wide failure.
  - `F7` ✔ Every error logged by order-service in the window is a downstream timeout to exchange-gateway, not an internal failure: 349/349 ERROR entries are the single pattern 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', first at 11:02:45Z and last at 12:58:54Z.
  - `F14` ✔ The earliest order-service ERROR log is 2026-03-17T11:02:45Z and 100% of its 174 ERROR logs in 09:30–12:00 share one signature: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' — the failure is specific to the placeOrder path calling exchange-gateway, not a generic application error.
**Rejected because:** Timing and mechanism both rule it out: the change precedes onset by ~88 minutes with a quiet gap (F6, F8, F13), the observed signature is exclusively a 504 on the exchange-gateway placeOrder path with a 5000ms upstream connect timeout (F7, F14, F16), and pricing-engine's error_rate stays flat ~0.17-0.28% throughout (F30), so the degradation is not on the pricing leg.

## Facts (verified observations)
- Every error logged by order-service during the incident (349/349, first 11:02:45Z) is the single downstream pattern 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' — an outbound wait, not an internal application failure. [F7, F14, F31]
- order-service error_rate was flat ~0.2% through the 10:50 bucket, jumped to 24.64% in the 11:00 bucket and held ~31% (12:50 avg 31.28%); its p99 rose from ~150ms to 4285.85ms avg / 5631.42ms max, consistent with a 5000ms upstream connect timeout, while its CPU stayed flat at ~39–41%. [F8, F12, F15, F16]
- exchange-gateway's degradation originates at a single external host: its errors name only api.exchange-partner.example, with 5s upstream connect timeouts on op=placeOrder (163) and op=quoteSnapshot (146) plus 161 'exchange-api returned 503', earliest at 11:01:25Z; a circuit breaker opened at 11:03:26Z (failure ratio 0.47 over 60s). [F10, F17, F25]
- exchange-gateway's upstream-facing latency exploded from ~121ms to 8248.65ms avg (max 9644.67) in the 11:00 bucket and ~9,200ms sustained, while its inbound request_rate stayed flat (~269–281 req/s) — the delay is on the egress leg, not its inbound side. [F9, F11, F27]
- Temporal ordering is clean: exchange-gateway's first partner timeout is 11:01:25Z, earlier than order-service's first error at 11:02:45Z — the failing component sits downstream of order-service and the failure propagates up from gateway to order-service. [F17, F31, F13]
- Blast radius is broader than order-service: market-data-service (0.25% → 4.01% → ~6.4–6.7%) and api-gateway (0.19%/0.26% → 3.85% → ~6.1–6.3%) both degrade in the same 11:00Z bucket, and exchange-gateway fails on two distinct ops (placeOrder and quoteSnapshot) against the same partner host. [F28, F29, F26]
- pricing-engine is a counter-control on the same submit path: its error_rate stayed flat ~0.17–0.28% throughout 10:50–12:00Z, so the 11:00Z degradation is specific to the exchange-partner egress leg, not fleet-wide. [F30]
- exchange-gateway is a non-shared service (shared:false) whose only downstream is exchange-api (external, has_telemetry:false); the failing leg exchange-gateway→exchange-api is a single dedicated external edge, not a shared mesh component. [F21, F4]
- The egress faults are isolated to that single partner host — every timeout signature in the window carries host=api.exchange-partner.example and no error names any other outbound host. [F25]
- Historical precedent for the same service pair: HIST-103 (2025-10-21, match_score 4) is an external exchange API outage on the partner side presenting as '504 on order submit' / upstream timeouts across order-service and exchange-gateway; other partner-outage precedents resolved partner-side ('partner fixed', 'Waited for partner'). [F22, F5]
- No deployment or config change is recorded for exchange-gateway, api-gateway or ledger-db in the window; the only deployment fleet-wide in 08:00–12:59Z is order-service v2.15.2 (dep-5333, 10:48Z, rolled_back 11:16Z), and the only config change is pricing-engine cache.ttl_seconds 5→4 at 09:20Z (cfg-2290). [F1, F2, F3, F19, F20]
- The pricing-engine TTL change precedes onset by ~88 minutes with no corresponding signature: pricing-engine's error_rate stayed flat and the observed failure is exclusively a 504 on the exchange-gateway placeOrder path. [F30, F7]

## Assumptions (not verified)
- Assumption: the partner hostname api.exchange-partner.example in gateway logs is a genuine external dependency and the connectivity failure is on the partner or the inter-network path, not inside exchange-gateway's own networking stack (H2 assumption; exchange-api has_telemetry=false, so the partner's own health cannot be observed).
- Assumption: quoteSnapshot and placeOrder share the same partner endpoint, so the failure is broad rather than op-specific (supported by F26 naming the same host for both ops, but the partner-side configuration is not observable).
- Assumption: F28/F29 count other callers of the gateway (market-data-service, api-gateway) consistent with a shared downstream dependency; these services' dependency edges to this gateway were not fully enumerated, so blast-radius completeness is assumed.
- Assumption: absence of any recorded mesh/egress/DNS/NAT/route change in the window (F19, F20, F23) reflects absence of a causative change rather than an unrepresentable change in the change APIs.
- Assumption: error-log absence is treated as evidence that no other outbound destination from exchange-gateway was failing; no per-destination metric was retrieved.
- (demoted from facts: no verified observation backs it) The deployment API has no representable 'infra' (mesh/firewall/DNS/VPC/NAT/LB) service scope — get_deployments(service=infra) fails not_found and known_services contains no such entity — so absence of a recorded network change is not proof that none occurred.

## Timeline
- `2026-03-17T09:20:00Z` Only fleet-wide config change in the window: pricing-engine cache.ttl_seconds 5→4 (cfg-2290). No order-service config/flag change recorded. [F2, F6]
- `2026-03-17T10:48:00Z` order-service v2.15.2 (dep-5333, 'Add idempotency keys to order submit') deployed — the only deployment fleet-wide in 08:00–12:59Z. No failures follow for ~13 minutes. [F1, F8]
- `2026-03-17T11:01:25Z` First exchange-gateway upstream connect timeout to api.exchange-partner.example (op=placeOrder, 5000ms). This is the earliest visible failure and precedes all order-service errors. [F10, F17]
- `2026-03-17T11:01:43Z` First exchange-gateway op=quoteSnapshot timeout to the same partner host — the failure spans two distinct upstream operations. [F26, F25]
- `2026-03-17T11:02:00Z` order-service error_rate at 2-minute resolution jumps to 31.43% (from ~0.2%), placing onset between 11:00 and 11:02Z, before the 11:10 report. [F13]
- `2026-03-17T11:02:45Z` Earliest order-service ERROR log: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' — downstream projection of the gateway's partner timeouts. [F14, F31]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s). [F10]
- `2026-03-17T11:10:00Z` Incident INC-003 [SEV-1] reported by on-call SRE; rollback of v2.15.2 initiated. [F1]
- `2026-03-17T11:16:00Z` order-service v2.15.2 (dep-5333) marked rolled_back — errors continue afterward (31.28% avg at 12:50, last error 12:58:54Z), inconsistent with the release being the cause. [F1, F8, F7]
- `2026-03-17T12:50:00Z` order-service error_rate still ~31% and exchange-gateway upstream latency ~9s sustained across market-data-service and api-gateway as well. [F8, F11, F28, F29]

## Recommended actions
- **[immediate]** Complete the in-flight rollback of dep-5333 (order-service v2.15.2) to closure for hygiene, but do NOT expect it to restore service — monitor for no recovery after 11:16Z rollback completion. -- The rollback was already initiated; F1 shows it completed at 11:16Z yet errors sustained ~31% (F8) with a last error at 12:58:54Z (F7), confirming the deploy is not the cause. Completing it removes a confounder from the incident picture rather than mitigating the outage. [F1, F8, F7]
- **[immediate]** Engage the external exchange partner (api.exchange-partner.example) directly — open a partner/vendor incident, request partner-side status and health for the placeOrder and quoteSnapshot endpoints, and reference the x-partner-request-id values captured in gateway logs. This is the primary mitigation path. -- H2 (0.55, accepted) identifies partner degradation as the root cause; all gateway timeouts and 503s name that single host (F10, F25, F32), and the precedent HIST-103 for the same service pair was an external exchange API outage resolved partner-side (F22, F24). [F10, F17, F25, F32, F22, F24]
- **[immediate]** Treat the exchange-gateway circuit breaker as the protective stopgap: confirm it remains open/tuned (it opened at 11:03:26Z, failure ratio 0.47 over 60s) and consider lowering upstream connect timeouts / enabling fast-fail so order-service returns a fast, bounded error instead of hanging ~5s per request. -- The 5000ms upstream connect timeout is what turns the partner stall into 4–5s hangs at order-service (F12, F16); fast-fail reduces user-facing latency and load while the partner is engaged. [F10, F12, F16]
- **[short_term]** Run the discriminating evidence that H3 still needs: (a) a reachability/connectivity probe of api.exchange-partner.example from a host/path OTHER than exchange-gateway, and (b) reconciliation of the full caller set of exchange-gateway for the 11:00–12:59Z window to confirm the blast radius (F28/F29 sampled market-data-service and api-gateway; portfolio-service was not enumerated). If the partner is reachable from other paths and other callers are healthy, escalate H3. -- The Critic's residual for H2 vs H3 is exactly partner reachability from another path plus complete blast-radius enumeration; these separate external-partner outage from a shared mesh/egress fault and are currently uncollected gaps. [F28, F29, F21, F25]
- **[short_term]** Verify partner recovery and confirm service restoration: watch exchange-gateway upstream_latency_ms (baseline ~121ms) and error_rate return to baseline, and order-service error_rate return to ~0.2%, before declaring INC-003 resolved. -- exchange-gateway's upstream latency went ~121ms → ~9,200ms and order-service error_rate ~0.2% → ~31%; these are the objective recovery signals (F8, F11, F27). [F8, F11, F27]
- **[long_term]** Add observability for the external partner edge: synthetic outbound probes / connectivity health checks to api.exchange-partner.example, an upstream_error_rate metric and per-destination egress metrics, and a partner status feed — because exchange-api has_telemetry=false today. -- The deepest link in H2 was inferable only from gateway logs; lack of partner telemetry and per-destination egress metrics is what capped confidence at 0.55 and left H3 unfalsifiable (F25, F32, F21). [F21, F25, F32]
- **[long_term]** Make mesh/egress/DNS/NAT/route changes representable in the change APIs (currently get_deployments(service=infra) returns not_found and no such entity exists in known_services), so future infra-path hypotheses can be positively confirmed or excluded. -- Absence of a recorded network change cannot currently be treated as proof none occurred (F23), which is why H3 can only be excluded by absence-of-record argument rather than positive evidence. [F23, F19, F20]

## Data gaps
- No telemetry at all for the external partner api.exchange-partner.example (has_telemetry=false): the partner's own health cannot be observed, so 'partner degraded' vs 'network path to the partner failed' cannot be separated from inside this environment.
- No external status page, synthetic probe or third-party health endpoint was queried for api.exchange-partner.example (tool budget exhausted), so the H2 claim rests entirely on internal telemetry (F10, F11, F17, F18).
- No API exposes mesh/egress-proxy/firewall/DNS/VPC/NAT/route objects; get_deployments(service=infra) fails not_found, so H3 cannot be positively excluded from change records — the absence of a recorded network change is not proof none occurred.
- Blast radius is incomplete: the full caller set of exchange-gateway/the partner was not enumerated (portfolio-service query returned budget_exhausted). Only market-data-service, api-gateway and pricing-engine were sampled (of which the first two degrade, the third does not).
- No per-destination egress metric for exchange-gateway: isolation to the partner host is inferred from the absence of any other host in its error logs (F25), not from a metric showing other destinations healthy.
- No metric-level evidence for order-service, ledger-db, redis-cache, kafka or pricing-engine in the onset window; order-service db_pool_in_use and exchange-gateway upstream_error_rate queries were not executed. A secondary DB-pool or resource bottleneck inside order-service cannot be fully excluded.
- No ingress/gateway-layer metrics for POST order submit specifically were retrieved, so independent errors at the edge/login ingress layer cannot be stated.
- Metric buckets are 600s (2-minute resolution only for the error-rate series), so the exact ordering/lead-lag is resolved only from logs (gateway 11:01:25Z vs order-service 11:02:45Z), not from metrics.
- A second historical-incident search on the idempotency-key/orders-table write path introduced by v2.15.2 returned budget_exhausted, so that precedent question is only partially answered.
- Tool reliability: tc-035 get_deployments(service=infra) by dependency FAILED not_found.

## What would change this conclusion
- A connectivity probe showing api.exchange-partner.example is reachable from another host/path while exchange-gateway alone cannot reach it — this would move the conclusion from H2 (external partner degraded) to H3 (shared mesh/egress/network path fault).
- Confirmation from the partner/vendor or an external status source that the partner was healthy during 11:01–12:59Z — would falsify H2 and promote H3.
- Complete blast-radius enumeration showing other independent callers of exchange-gateway/partner healthy at 11:01–11:03Z while only one egress path fails — would favor H3 over H2.
- Discovery of a recordable (or externally confirmed) mesh/egress/DNS/NAT/route change at ~11:01Z, currently unrepresentable in the change APIs — would promote H3.
- Evidence that other outbound destinations from exchange-gateway were also timing out in the same window (per-destination egress metrics) — would indicate a path-wide egress fault rather than a partner-specific one.
- Emergence of a non-placeOrder/non-partner error signature in order-service, or a resource spike (CPU/DB pool) at 11:00Z, which would reopen H1 or H4.
- A matching historical incident on the v2.15.2 idempotency-key/orders-table write path (could not be searched) that explains the 504s — would reopen H1.

## Integrity warnings (set by code)
- fact demoted to assumption: The deployment API has no representable 'infra' (mesh/firewall/DNS/VPC/NAT/LB) s

## Investigation stats
- latency 144.36s, LLM calls 25, tokens 198307 (in 166446 / out 31861)
- tool calls 40 (1 failed, 0 partial, 0 recovered by retry), findings 32/32 verified
