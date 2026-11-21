# Incident investigation report: INC-003
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The order-submit failures are caused by degradation of the external exchange partner api.exchange-partner.example, not by the order-service v2.15.2 release (H2, confidence 0.75, high). Starting 11:01:25Z, exchange-gateway's outbound calls to that host began failing with 5s connect timeouts and application-layer 503s carrying the partner's own request id, pushing exchange-gateway's error rate to ~44% and p99 to ~4.8s; order-service, one hop downstream, shows ~30% submits failing with the identical error 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' and a pure downstream-wait latency profile with flat local CPU. The reporter's suspicion — the v2.15.2 release — is decisively rebutted: the rate was already at full ~30% magnitude before the 11:16Z rollback, was flat across the rollback second, and is still ~30-31% at 12:59Z, 1h43m later. Alternatives H3 (pricing-engine TTL change) and H4 (ledger-db/kafka shared-infra) are tested and contradicted by clean multi-metric evidence and cannot produce an exchange-gateway-scoped error signature. The remaining uncertainty is internal to H2 (partner-side outage vs. a DNS/egress/NAT/TLS path fault between exchange-gateway and the partner), which does not change the ranking or the remediation.

## Probable root cause
**External exchange partner degradation (api.exchange-partner.example) timing out exchange-gateway, propagating to order-service** -- dependency_failure in `exchange-gateway (external exchange-api upstream)`, triggered by `null` (confidence 0.75, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: External exchange partner degradation (api.exchange-partner.example) timing out exchange-gateway, propagating to order-service | dependency_failure | exchange-gateway (external exchange-api upstream) | null | 0.75 (high) | active |
| 2 | H1: order-service v2.15.2 regression (reporter's suspicion) | bad_deployment | order-service | dep-5333 | 0.03 (low) | rejected |
| 3 | H3: Pricing-engine cache TTL config change (5s -> 4s) degrading the quote path | config_change | pricing-engine | tc-004 | 0.02 (low) | rejected |
| 4 | H4: Shared-infra / neighbour fault (ledger-db or kafka) independent of the release | infrastructure_failure | ledger-db or kafka | null | 0.02 (low) | rejected |

### H2: External exchange partner degradation (api.exchange-partner.example) timing out exchange-gateway, propagating to order-service
The external exchange API behind exchange-gateway stopped responding/timed out (connect timeouts after 5s, then application-layer 503s with the partner's own request id) starting 11:01:25Z. exchange-gateway's placeOrder/quoteSnapshot calls exceed the 5s timeout and a circuit breaker opens; this surfaces to order-service as 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', giving ~30% order submit failures. order-service v2.15.2 (dep-5333) is coincidental: all order-service errors are the identical downstream signature, its local CPU is flat, and the failure rate is unchanged across the 11:16Z rollback.

**Causal chain**
1. api.exchange-partner.example begins failing exchange-gateway's upstream calls: 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (op=placeOrder/quoteSnapshot) first_seen 11:01:25Z, plus application-layer 'exchange-api returned 503 Service Unavailable' with x-partner-request-id first_seen 11:02:20Z [F11, F34, F35]
2. Circuit breaker for exchange-api opens at 11:03:26Z (failure ratio 0.47 over 60s) [F11]
3. exchange-gateway error_rate ~0.2% -> ~44% and p99 130ms -> 4811ms in the same 11:00Z bucket, with flat request_rate ~265-280 req/s (not traffic-driven) and flat cpu_pct (~30%) / memory_pct (~44%) — no local saturation [F10, F18, F31, F32]
4. order-service's placeOrder calls to exchange-gateway fail with 504 Gateway Timeout (349/350 errors identical); its p99 jumps to 4648.8ms while local CPU stays ~40% (pure downstream-wait) [F8, F9]
5. order-service error_rate reaches ~30% and api-gateway propagates a smaller ~6% failure upstream; the partner-side error surface (connect timeouts + 503s) persists continuously to latest data 12:58Z [F7, F17, F33, F35]

**Supporting evidence**
  - `F11` ✔ exchange-gateway's errors are upstream-call failures naming the external host: 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (op=placeOrder 163x, op=quoteSnapshot 146x, first_seen 11:01:25Z) and 'exchange-api returned 503 Service Unavailable' (161x, first_seen 11:02:20Z). A circuit breaker opened for exchange-api at 11:03:26Z (failure ratio 0.47 over 60s).
  - `F35` ✔ The partner is also returning application-layer 503s the whole time: 'exchange-api returned 503 Service Unavailable (x-partner-request-id=...)' matched 161 ERROR records, first_match 11:02:20Z and last_match 2026-03-17T12:58:32Z, e.g. 12:58:32Z 'x-partner-request-id=b62798d4'. This is a real partner-side error surface (not just a network connect timeout) and it is still present at latest data.
  - `F33` ✔ The api.exchange-partner.example upstream failure path is still broken at latest data, not a transient blip: exchange-gateway connect-timeout errors to that host run continuously from first_match 11:01:25Z to last_match 12:58:30Z (309 ERROR records in 10:30-12:59Z), with the most recent entry '2026-03-17T12:58:30Z ... upstream connect timeout after 5000ms host=api.exchange-partner.example op=quoteSnapshot'. Bucket-level counts confirm continuity: 11:00-11:15Z = 40 matched, 12:00-12:15Z = 31 matched (first 12:00:32Z), 12:45-12:59Z = 39 matched (first 12:45:33Z).
  - `F34` ✔ The same two upstream failure signatures persist across the requested buckets with matching per-bucket counts: 11:00-11:15Z showed 23x op=placeOrder (first_seen 11:01:25Z) and 17x op=quoteSnapshot (first_seen 11:01:43Z); 12:00-12:15Z 17x placeOrder / 14x quoteSnapshot; 12:45-12:59Z 20x quoteSnapshot / 19x placeOrder. So the failure rate to the partner is steady (~2-3 errors/min) rather than decaying to zero.
  - `F10` ✔ exchange-gateway shows the same onset and higher magnitude: its error_rate went from ~0.2% through 10:45Z to avg 40.44 / max 45.46 in the 11:00Z bucket (~44% sustained), and latency_p99_ms went from ~130ms to 4811ms avg in the same bucket.
  - `F18` ✔ exchange-gateway's request_rate was flat at ~265-280 req/s across the entire window (10:00Z onward, including after 11:00Z), so its failure is not traffic-driven; the break is confined to its upstream call to the external exchange, matching the reported upstream-derived error/error-latency jump (T1 F10/F11) rather than any load or local-resource change.
  - `F31` ✔ exchange-gateway cpu_pct was flat across the whole 10:30-12:59Z window, including before and after the 11:00Z onset: min 26.73%, max 32.52%, mean 29.88% over 150 points with missing_points 0; the 10:58Z bucket (just before onset) avg 29.93 / max 31.52, the 11:05Z bucket avg 30.15 / max 32.52, and the latest 12:57Z bucket avg 30.49 / max 31.63. No CPU rise at or after onset.
  - `F32` ✔ exchange-gateway memory_pct was likewise flat across 10:30-12:59Z: min 39.41%, max 48.55%, mean 44.11%, missing_points 0; 10:58Z avg 44.5 / max 47.07, 11:05Z avg 44.75 / max 48.01, and latest 12:57Z avg 43.08 / max 44.38. No memory pressure or growth coinciding with the onset, i.e. no local resource saturation.
  - `F8` ✔ Every order-service ERROR in the window (349 of 350 matched records) is the identical signature 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; the only non-error log is a single GC WARN at 07:03:32Z. First occurrence 11:02:45Z; the single error pattern's first_seen is 11:02:45Z.
  - `F9` ✔ order-service's own resources were stable across the onset: cpu_pct held ~39-42% in every 15-min bucket from 07:00Z through 12:45Z (11:00Z avg 40.08), and latency_p99_ms was flat ~148-154ms through 10:45Z before jumping to 4648.8ms at 11:00Z — a downstream-wait signature, not local saturation.
  - `F12` ✔ The exchange-gateway upstream failure (connect timeouts to api.exchange-partner.example) starts at 11:01:25Z, i.e. order-service's reported failures (first log 11:02:45Z, first saturated bucket 11:00-10:58Z) are consequent to the gateway's upstream calls failing, consistent with a fast 5s timeout propagating to 504s.
  - `F7` ✔ order-service error_rate was flat at ~0.2% (max ~0.32) through the 10:45Z bucket, then jumped to avg 26.73 / max 33.2 in the 11:00Z bucket, staying ~30-31% through 12:45Z. A zoomed query (540s buckets) puts the onset in the 10:58Z bucket (avg 17.26, max 31.9 vs 0.21 at 10:49Z).
  - `F17` ✔ api-gateway (the only caller of order-service) shows the same 11:00Z onset: error_rate went from flat ~0.19-0.23% through 10:45Z to avg 5.42 / max 6.56 in the 11:00Z bucket and held ~6.1% through 12:45Z — a smaller magnitude than order-service (~30%), consistent with it merely propagating downstream order-submit failures, not originating them.
  - `F4` ✔ order-service dependency map: callers -> order-service: api-gateway (http). order-service -> callees: ledger-db (db), pricing-engine (http), exchange-gateway (http), kafka (queue-produce). pricing-engine further depends on market-data-service (http) and redis-cache (cache); exchange-gateway depends on external exchange-api (no telemetry).
**Contradicting evidence**
  - `F1` ✔ In the fleet-wide change inventory for 06:00-12:59Z there was exactly ONE deployment: order-service v2.15.2 (dep-5333), deployed 2026-03-17T10:48:00Z by a.khan, summary 'Add idempotency keys to order submit', changes ['Idempotency-Key header persisted in orders table','retry-safe submit handler'], status rolled_back with rolled_back_at 2026-03-17T11:16:00Z.
**Assumptions**
  - The origin is the partner service/network itself rather than a DNS/egress/NAT/cert path fault between exchange-gateway and api.exchange-partner.example — no DNS/egress-change source and no partner telemetry exist (F4); the application-layer 503s carrying x-partner-request-id (F35) lean toward a real partner-side error surface but do not fully exclude a path fault.
  - Live traffic to the exchange was succeeding normally before 11:01Z (no pre-onset partner success metric retrieved).

### H1: order-service v2.15.2 regression (reporter's suspicion)
The idempotency-key code release dep-5333 introduced a fault in order-service's submit path causing ~30% of submits to fail with 504s.

**Causal chain**
1. dep-5333 (order-service v2.15.2, 'Add idempotency keys to order submit') deployed 10:48Z [F1]
2. New idempotency/retry-safe submit code path causes submits to stall or error *(assumed)*
3. order-service error_rate jumps ~0.2% -> ~30% at onset ~10:58-11:00Z [F7]
4. Failures surface as 504 Gateway Timeout on order submit [F8]

**Supporting evidence**
  - `F1` ✔ In the fleet-wide change inventory for 06:00-12:59Z there was exactly ONE deployment: order-service v2.15.2 (dep-5333), deployed 2026-03-17T10:48:00Z by a.khan, summary 'Add idempotency keys to order submit', changes ['Idempotency-Key header persisted in orders table','retry-safe submit handler'], status rolled_back with rolled_back_at 2026-03-17T11:16:00Z.
  - `F7` ✔ order-service error_rate was flat at ~0.2% (max ~0.32) through the 10:45Z bucket, then jumped to avg 26.73 / max 33.2 in the 11:00Z bucket, staying ~30-31% through 12:45Z. A zoomed query (540s buckets) puts the onset in the 10:58Z bucket (avg 17.26, max 31.9 vs 0.21 at 10:49Z).
**Contradicting evidence**
  - `F8` ✔ Every order-service ERROR in the window (349 of 350 matched records) is the identical signature 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; the only non-error log is a single GC WARN at 07:03:32Z. First occurrence 11:02:45Z; the single error pattern's first_seen is 11:02:45Z.
  - `F9` ✔ order-service's own resources were stable across the onset: cpu_pct held ~39-42% in every 15-min bucket from 07:00Z through 12:45Z (11:00Z avg 40.08), and latency_p99_ms was flat ~148-154ms through 10:45Z before jumping to 4648.8ms at 11:00Z — a downstream-wait signature, not local saturation.
  - `F10` ✔ exchange-gateway shows the same onset and higher magnitude: its error_rate went from ~0.2% through 10:45Z to avg 40.44 / max 45.46 in the 11:00Z bucket (~44% sustained), and latency_p99_ms went from ~130ms to 4811ms avg in the same bucket.
  - `F11` ✔ exchange-gateway's errors are upstream-call failures naming the external host: 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (op=placeOrder 163x, op=quoteSnapshot 146x, first_seen 11:01:25Z) and 'exchange-api returned 503 Service Unavailable' (161x, first_seen 11:02:20Z). A circuit breaker opened for exchange-api at 11:03:26Z (failure ratio 0.47 over 60s).
  - `F12` ✔ The exchange-gateway upstream failure (connect timeouts to api.exchange-partner.example) starts at 11:01:25Z, i.e. order-service's reported failures (first log 11:02:45Z, first saturated bucket 11:00-10:58Z) are consequent to the gateway's upstream calls failing, consistent with a fast 5s timeout propagating to 504s.
  - `F17` ✔ api-gateway (the only caller of order-service) shows the same 11:00Z onset: error_rate went from flat ~0.19-0.23% through 10:45Z to avg 5.42 / max 6.56 in the 11:00Z bucket and held ~6.1% through 12:45Z — a smaller magnitude than order-service (~30%), consistent with it merely propagating downstream order-submit failures, not originating them.
  - `F20` ✔ Post-rollback window 11:15-12:00Z (45 minutes after the 11:16Z rollback) at 120s buckets: every one of 23 points is elevated, min 28.43, max 33.32, mean 31.05 — no dip at or after the rollback instant.
  - `F21` ✔ Boundary zoom at 60s buckets straddling the 11:16Z rollback: 11:15Z avg 32.87, 11:16Z avg 31.45, 11:17Z avg 31.73, 11:18Z avg 30.37 — the rate is flat across the rollback second (range 29.88-33.2, mean 31.31); there is no recovery step.
  - `F22` ✔ Post-rollback window 12:00-12:45Z at 120s buckets remains elevated: mean 31.05, range 28.38-35.57, no downward trend across the 30 points (12:00Z 31.26 ... 12:44Z 30.3).
  - `F23` ✔ Through the latest data at 12:59Z the rate is still ~30-31%: 12:57Z avg 31.49 (max 32.44), 12:59Z avg 30.39 — 1h43m after the rollback the incident is unresolved.
  - `F31` ✔ exchange-gateway cpu_pct was flat across the whole 10:30-12:59Z window, including before and after the 11:00Z onset: min 26.73%, max 32.52%, mean 29.88% over 150 points with missing_points 0; the 10:58Z bucket (just before onset) avg 29.93 / max 31.52, the 11:05Z bucket avg 30.15 / max 32.52, and the latest 12:57Z bucket avg 30.49 / max 31.63. No CPU rise at or after onset.
  - `F32` ✔ exchange-gateway memory_pct was likewise flat across 10:30-12:59Z: min 39.41%, max 48.55%, mean 44.11%, missing_points 0; 10:58Z avg 44.5 / max 47.07, 11:05Z avg 44.75 / max 48.01, and latest 12:57Z avg 43.08 / max 44.38. No memory pressure or growth coinciding with the onset, i.e. no local resource saturation.
**Assumptions**
  - order-service error text originates in order-service code rather than being a downstream failure it merely reports
  - the deploy precedes the onset (onset-vs-deploy ordering not proven from telemetry)
**Rejected because:** Every order-service error (349/350) is the identical downstream signature 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' (F8), and order-service's own CPU stayed flat ~40% with a pure downstream-wait latency profile (F9). The identical onset appears one hop further out at exchange-gateway (F10) with error text naming the external host (F11) before order-service logged anything (F12). Decisively, the 11:16Z rollback (F1) produced no recovery: the rate is flat across the rollback second (F21) and held ~30-31% through 12:45Z (F20/F22) and to 12:59Z (F23), and exchange-gateway's resources were flat (F31/F32) — incompatible with the release being the cause.

### H3: Pricing-engine cache TTL config change (5s -> 4s) degrading the quote path
The only fleet-wide config change in the window (pricing-engine cache.ttl_seconds 5->4 at 09:20Z) caused pricing-engine to misbehave, and order-service 504s are timeouts waiting on pricing-engine/market-data.

**Causal chain**
1. config-bot shortens pricing-engine cache TTL 5s -> 4s at 09:20Z [F2]
2. Reduced cache hit rate increases pricing-engine latency / downstream load *(assumed)*
3. order-service submit calls pricing-engine and times out with 504 *(assumed)*
4. order-service error_rate rises to ~30% [F7]

**Supporting evidence**
  - `F2` ✔ Only ONE config change fleet-wide in 06:00-12:59Z: pricing-engine cache.ttl_seconds changed 5 -> 4 by config-bot at 2026-03-17T09:20:00Z ('Shorter quote cache TTL').
**Contradicting evidence**
  - `F16` ✔ pricing-engine (the other HTTP dependency, and the only config change fleet-wide) shows NO degradation: error_rate stayed flat ~0.18-0.23% in every 15-min bucket from 09:00Z through 12:45Z (11:00Z avg 0.21, max 0.30), and it logged zero WARN+ records in 10:30-12:00Z.
  - `F8` ✔ Every order-service ERROR in the window (349 of 350 matched records) is the identical signature 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; the only non-error log is a single GC WARN at 07:03:32Z. First occurrence 11:02:45Z; the single error pattern's first_seen is 11:02:45Z.
  - `F11` ✔ exchange-gateway's errors are upstream-call failures naming the external host: 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (op=placeOrder 163x, op=quoteSnapshot 146x, first_seen 11:01:25Z) and 'exchange-api returned 503 Service Unavailable' (161x, first_seen 11:02:20Z). A circuit breaker opened for exchange-api at 11:03:26Z (failure ratio 0.47 over 60s).
**Assumptions**
  - the TTL change produces a delayed impact ~100 minutes later
  - order-service's failing dependency is pricing-engine despite the log text naming exchange-gateway
**Rejected because:** pricing-engine shows zero degradation: error_rate flat ~0.18-0.23% through 12:45Z and no WARN+ logs in 10:30-12:00Z (F16). The error signature names exchange-gateway, not pricing-engine (F8), and exchange-gateway's error text explicitly names the external host (F11).

### H4: Shared-infra / neighbour fault (ledger-db or kafka) independent of the release
A shared component in order-service's neighbourhood (shared postgres ledger-db or shared kafka) degraded around 11:00Z, causing order-service submit timeouts and 504s independent of v2.15.2.

**Causal chain**
1. ledger-db / kafka is shared between order-service and portfolio/payment/settlement services [F5]
2. Shared component degrades around 11:00Z (IOPS throttling, connection exhaustion, broker issue) *(assumed)*
3. order-service writes/swaps on the shared DB or produces to kafka and blocks *(assumed)*
4. order-service error_rate rises to ~30% with 504s on submit [F7, F8]

**Supporting evidence**
  - `F5` ✔ Shared components in order-service's neighbourhood: ledger-db (shared postgres) is written by order-service, portfolio-service, payment-service and settlement-service; kafka (shared) is produced to by order-service and payment-service and consumed by settlement-service and notification-service - so these neighbours could affect order-service via shared infra.
  - `F6` ✔ Historical incidents matching 'order submit failure / 504' had varied root causes: HIST-103 (2025-10-21, '504 on order submit') = external exchange API outage; HIST-116 (2025-12-18, '504 at gateway') = api-gateway envoy upgrade (bad_deployment); HIST-110 (2026-01-15, order latency) = ledger-db IOPS throttling; HIST-115 (2025-09-25, order-service) = datasource pool config override. These are leads, not evidence about the current incident.
**Contradicting evidence**
  - `F13` ✔ In the pre-onset baseline ledger-db was healthy: slow_queries_per_min stayed ~0.9-1.2/min across every 15-min bucket from 09:00Z to 12:45Z (11:00Z avg 0.93, max 1.36), i.e. no IOPS/slow-query degradation at the onset.
  - `F14` ✔ ledger-db connections_active was flat across the onset: ~61-63 avg in every bucket (11:00Z avg 61.62, 11:15Z avg 60.73), with no connection-pool exhaustion or spike, so the shared DB did not degrade before or after 11:00Z.
  - `F15` ✔ ledger-db emits only a single WARN in 07:00-12:59Z, a benign 'clock drift 38ms detected, resyncing with NTP' at 11:26:22Z; no errors, lock or slow-query warnings around the onset.
  - `F8` ✔ Every order-service ERROR in the window (349 of 350 matched records) is the identical signature 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; the only non-error log is a single GC WARN at 07:03:32Z. First occurrence 11:02:45Z; the single error pattern's first_seen is 11:02:45Z.
  - `F11` ✔ exchange-gateway's errors are upstream-call failures naming the external host: 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (op=placeOrder 163x, op=quoteSnapshot 146x, first_seen 11:01:25Z) and 'exchange-api returned 503 Service Unavailable' (161x, first_seen 11:02:20Z). A circuit breaker opened for exchange-api at 11:03:26Z (failure ratio 0.47 over 60s).
  - `F12` ✔ The exchange-gateway upstream failure (connect timeouts to api.exchange-partner.example) starts at 11:01:25Z, i.e. order-service's reported failures (first log 11:02:45Z, first saturated bucket 11:00-10:58Z) are consequent to the gateway's upstream calls failing, consistent with a fast 5s timeout propagating to 504s.
**Assumptions**
  - a shared-infra fault exists that left no trace in the queried ledger-db metrics but would still produce an exchange-gateway-scoped error message
  - kafka metrics (unqueried) show a fault
**Rejected because:** ledger-db is demonstrably healthy across the onset: slow_queries flat ~0.9-1.2/min (F13), connections_active flat ~61-63 (F14), only a benign 38ms NTP clock-drift WARN with no lock/slow-query warnings (F15). The failure is scoped to the external exchange call (F11) and precedes order-service errors (F12), which shared-infra cannot explain.

## Facts (verified observations)
- exchange-gateway's errors are upstream-call failures explicitly naming the external host: 'upstream connect timeout after 5000ms host=api.exchange-partner.example' (op=placeOrder 163x, op=quoteSnapshot 146x, first_seen 11:01:25Z) and 'exchange-api returned 503 Service Unavailable' (161x, first_seen 11:02:20Z); a circuit breaker opened for exchange-api at 11:03:26Z with failure ratio 0.47. [F11]
- exchange-gateway showed the same onset as order-service and higher magnitude: error_rate rose from ~0.2% through 10:45Z to avg 40.44 / max 45.46 in the 11:00Z bucket (~44% sustained), with latency_p99_ms going from ~130ms to 4811ms avg in the same bucket. [F10]
- Every order-service ERROR in the window (349 of 350 matched records) is the identical signature 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; first occurrence 11:02:45Z. [F8]
- order-service's own resources were stable across the onset: cpu_pct held ~39-42% in every 15-minute bucket from 07:00Z through 12:45Z (11:00Z avg 40.08), while latency_p99_ms jumped from flat ~148-154ms to 4648.8ms at 11:00Z — a downstream-wait signature, not local saturation. [F9]
- exchange-gateway cpu_pct (min 26.73%, max 32.52%, mean 29.88%) and memory_pct (min 39.41%, max 48.55%, mean 44.11%) were flat across 10:30-12:59Z with no rise at or after onset, i.e. no local resource saturation. [F31, F32]
- order-service error_rate was flat at ~0.2% through the 10:45Z bucket, then jumped to avg 26.73 / max 33.2 in the 11:00Z bucket and stayed ~30-31% through 12:45Z; a 540s-bucket zoom puts onset in the 10:58Z bucket. [F7]
- Pre-rollback, order-service error_rate was already at full magnitude: it jumped to ~30% within ~2 minutes (11:02Z 31.43, then 30.91, 29.95, 30.69, 30.21, 31.85, 31.66) well BEFORE the 11:16Z rollback. [F19]
- There is no recovery at the rollback: 23 of 23 two-minute buckets in 11:15-12:00Z are elevated (min 28.43, max 33.32, mean 31.05), and a 60s zoom straddling the rollback second shows 11:15Z avg 32.87, 11:16Z 31.45, 11:17Z 31.73, 11:18Z 30.37 — flat, no recovery step. [F20, F21]
- The elevated rate persisted through 12:00-12:45Z (mean 31.05, range 28.38-35.57, no downward trend) and was still ~30-31% at latest data (12:57Z avg 31.49; 12:59Z avg 30.39), 1h43m after the rollback. [F22, F23]
- order-service request_rate was flat ~226-237 req/s across the whole period, so the ~30% is a fixed fraction of an unchanged workload, not a load surge. [F24]
- In the fleet-wide change inventory for 06:00-12:59Z there was exactly ONE deployment: order-service v2.15.2 (dep-5333) at 10:48:00Z, status rolled_back with rolled_back_at 11:16:00Z; and exactly ONE config change fleet-wide: pricing-engine cache.ttl_seconds 5->4 at 09:20:00Z. [F1, F2]
- pricing-engine shows no degradation: error_rate flat ~0.18-0.23% in every 15-minute bucket from 09:00Z through 12:45Z, and zero WARN+ records in 10:30-12:00Z. [F16]
- ledger-db was healthy across the onset on multiple metrics: slow_queries_per_min flat ~0.9-1.2/min from 09:00Z to 12:45Z, connections_active flat ~61-63, and only a single benign 'clock drift 38ms' NTP WARN at 11:26:22Z with no errors, lock or slow-query warnings. [F13, F14, F15]
- api-gateway (the only caller of order-service) shows the same 11:00Z onset but at ~6.1% sustained, a smaller magnitude than order-service's ~30%, consistent with it merely propagating downstream order-submit failures rather than originating them. [F17]
- The partner failure path is not a transient blip: exchange-gateway connect-timeout errors to api.exchange-partner.example run continuously from 11:01:25Z to 12:58:30Z (309 ERROR records), and application-layer 503s with x-partner-request-id run from 11:02:20Z to 12:58:32Z (161 records), with steady per-bucket counts (~2-3 errors/min) rather than decaying to zero. [F33, F34, F35]
- exchange-gateway is called by two independent in-fleet callers (order-service and market-data-service), and the external endpoint exchange-api is reachable solely via exchange-gateway; no deployment or config change was recorded for exchange-gateway or market-data-service in the window. [F25, F26, F27, F28]
- Historical precedent matches the current symptom set: HIST-103 (2025-10-21, 'Order failures during exchange partner outage', services order-service + exchange-gateway, symptoms '504 on order submit','upstream timeouts') was an external exchange API outage on the partner side. [F29]
- order-service's dependency map shows it calls ledger-db, pricing-engine, exchange-gateway and kafka; exchange-gateway depends on external exchange-api which has no telemetry. [F4]

## Assumptions (not verified)
- The ultimate origin is the partner service/network itself rather than a DNS/egress/NAT/certificate path fault between exchange-gateway and api.exchange-partner.example — no DNS/egress-change source exists and no partner telemetry exists (F4); the application-layer 503s carrying x-partner-request-id lean toward a real partner-side error surface but do not fully exclude a path fault.
- Live traffic to the exchange was succeeding normally before 11:01Z; no pre-onset partner success metric was retrieved.
- The rollback of dep-5333 actually executed at 11:16:00Z as recorded in the deployment record (F1); an explicit restart-count step at that timestamp was not checked, though the error_rate shows no marker there regardless.
- The 11:16Z rollback did not fix the incident because the cause lies outside order-service, rather than because the rollback itself was incomplete; no per-pod rollout timing for dep-5333 was available.
- (demoted from facts: no verified observation backs it) exchange-gateway's request_rate was flat at ~265-280 req/s across the entire window including after 11:00Z, so its failure is not traffic-driven.
- (demoted from facts: no verified observation backs it) The exchange-gateway upstream failure starts at 11:01:25Z, i.e. order-service's reported failures (first log 11:02:45Z) are consequent to the gateway's upstream calls failing, consistent with a fast 5s timeout propagating to 504s.

## Timeline
- `2026-03-16T12:00:00Z` Baseline check: no deployments and no config changes in the prior 24h before onset (get_deployments count 0, get_config_changes count 0). [F3]
- `2026-03-17T09:20:00Z` Only fleet-wide config change in the window: pricing-engine cache.ttl_seconds changed 5 -> 4 by config-bot ('Shorter quote cache TTL'). [F2]
- `2026-03-17T10:45:00Z` Last clean bucket: order-service error_rate flat ~0.2% and p99 ~148-154ms; exchange-gateway error_rate ~0.2% and p99 ~130ms. [F7, F9, F10]
- `2026-03-17T10:48:00Z` The only deployment in the window: order-service v2.15.2 (dep-5333) by a.khan, 'Add idempotency keys to order submit'. [F1]
- `2026-03-17T11:01:25Z` First exchange-gateway upstream failure to api.exchange-partner.example: 'upstream connect timeout after 5000ms' (earliest error on the whole chain). [F11, F12]
- `2026-03-17T11:02:20Z` Partner-side application-layer errors appear: 'exchange-api returned 503 Service Unavailable' carrying x-partner-request-id. [F11, F35]
- `2026-03-17T11:02:45Z` First order-service ERROR: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' — one hop and ~80 seconds after the gateway's upstream break. [F8, F12]
- `2026-03-17T11:03:26Z` Circuit breaker opens for exchange-api at exchange-gateway (failure ratio 0.47 over 60s). [F11]
- `2026-03-17T11:10:00Z` Incident INC-003 reported (SEV-1): order submit failure rate ~30%, 504s on order submit; on-call begins rolling back v2.15.2. []
- `2026-03-17T11:15:00Z` 60s zoom straddling the rollback shows the rate already flat at ~30%: 11:15Z 32.87, 11:16Z 31.45, 11:17Z 31.73, 11:18Z 30.37. [F21]
- `2026-03-17T11:16:00Z` dep-5333 marked rolled_back — no recovery in error rate at or after this instant. [F1, F20, F21]
- `2026-03-17T11:26:22Z` Only ledger-db WARN in the window: benign 'clock drift 38ms detected, resyncing with NTP' — no lock/slow-query warnings around the onset. [F15]
- `2026-03-17T12:00:00Z` 12:00-12:45Z remains elevated: mean 31.05%, range 28.38-35.57, no downward trend across 30 buckets. [F22]
- `2026-03-17T12:58:30Z` Latest exchange-gateway upstream failure record still shows 'connect timeout after 5000ms host=api.exchange-partner.example op=quoteSnapshot'; partner 503 with x-partner-request-id last seen 12:58:32Z. [F33, F35]
- `2026-03-17T12:59:00Z` Incident unresolved at latest data: order-service error_rate still ~30-31% (12:57Z avg 31.49; 12:59Z avg 30.39), 1h43m after the rollback. [F23]

## Recommended actions
- **[immediate]** Stop treating the rollback as the fix and re-announce the incident as external-dependency-driven: confirm the rollback of dep-5333 is complete/stable, then freeze further order-service changes — the rate was already at full ~30% magnitude before the 11:16Z rollback and is flat across it (F19, F20, F21), and is still ~30-31% at 12:59Z (F23), so the rollback cannot resolve the incident. -- H1 (release regression) is decisively rebutted by the unchanged rate across the rollback boundary; diverting effort to further rollback/redeploy work delays the real mitigation. [F1, F19, F20, F21, F23]
- **[immediate]** Mitigate the exchange-gateway upstream path: raise or disable the 5s upstream connect timeout and relax/open the circuit breaker for exchange-api so calls degrade instead of hard-failing, and apply fail-fast/degraded-mode handling to exchange-dependent order submit paths in order-service (queue/retry with backoff rather than returning 504). -- The 5s connect timeout to api.exchange-partner.example is the immediate mechanism converting partner slowness into ~44% exchange-gateway errors and ~30% order-submit 504s; the breaker is already open (failure ratio 0.47). [F11, F8, F10, F18]
- **[immediate]** Escalate to the exchange partner via the vendor relationship and open a partner ticket with timestamps and the captured x-partner-request-id values (first 11:02:20Z, latest 12:58:32Z), asking for their status for 11:01Z onward. -- The partner is returning its own application-layer 503s continuously, which is a partner-side error surface the vendor must answer for; this is the fastest path to recovery if the fault is truly partner-side. [F35, F33, F11]
- **[short_term]** Run the discriminating check between partner-side outage and an egress-path fault on our side: query market-data-service error_rate/latency_p99 around 11:00-11:05Z (the second, independent caller of exchange-gateway), and check exchange-gateway upstream_error_rate/restarts plus DNS/egress/NAT/TLS-cert change feeds for the exchange-gateway -> api.exchange-partner.example path. -- If market-data-service also broke at ~11:01Z, the fault is partner-side rather than exchange-gateway-local; if only order-service traffic is affected, an egress-path fault is more likely. This resolves the one remaining open element inside H2 and determines who owns the fix. [F25, F26, F30, F27, F28]
- **[short_term]** Confirm recovery against the same signal that defined the fault: verify exchange-gateway's connect-timeout/503 counts to api.exchange-partner.example return to zero and order-service error_rate returns to ~0.2% (its flat pre-onset baseline), and only then close the incident. -- The failure is continuous rather than decaying (309 timeout records and 161 partner 503s through 12:58Z), so closure must be tied to the upstream signature disappearing, not to time elapsed. [F33, F34, F35, F7, F23]
- **[long_term]** Add upstream-partner health to alerting and add an order-submit degradation path that does not convert partner slowness into user-visible 504s (explicit timeout budget per downstream hop, request hedging or queue-and-confirm for exchange-dependent submits). -- This incident reproduced a known historical pattern (HIST-103: external exchange API outage producing 504s on order submit), so recurrence prevention should target blast-radius reduction at the exchange-gateway boundary. [F29, F11, F8]
- **[long_term]** Tighten release-vs-incident discrimination in the on-call runbook: before rolling back a same-day deploy, check whether the error signature is entirely a downstream dependency message and whether the rate is flat across the deploy/rollback boundary — and align the v2.15.2 deploy timestamp (only 10:48Z is recorded) against a second-level onset series so future incidents do not spend ~40 minutes on a coincidental release. -- The same-day dep-5333 deploy (10:48Z) was coincidental with the 11:01Z partner onset, and ~100 minutes of rollback effort were spent on it despite a pure downstream-wait signature and flat local CPU. [F1, F8, F9, F12]

## Data gaps
- No telemetry exists for the external exchange endpoint api.exchange-partner.example (has_telemetry: false, out of fleet scope), so the ultimate origin — partner-side service outage vs. a network/DNS/egress/NAT/TLS path fault between exchange-gateway and the partner — cannot be confirmed from internal data. This bounds H2's confidence at 0.75.
- market-data-service error_rate/latency metrics around the 11:00Z onset were never retrieved (budget exhausted), so the decisive test for whether the second independent caller of exchange-gateway also broke — which would mark the fault as partner-side rather than gateway-local — is unanswered (F30).
- exchange-gateway upstream_error_rate and restart counts were not retrieved (query budget exhausted), so the upstream failure rate is only derivable from log counts (309 timeout records; ~2-3/min) and it is unverified whether exchange-gateway restarted, which could otherwise explain a resource change.
- No DNS / egress-firewall / NAT / TLS-certificate change source was available or queried for the exchange-gateway -> partner path; only deployment and config inventories were checked (both returned zero entries). An infrastructure-layer change on the egress path would not appear in those feeds.
- order-service v2.15.2's exact deploy time is not present in retrieved data (only the 10:48Z record from the deployment inventory), and no per-pod rollout timing/canary percentage was available, so the onset-vs-deploy ordering cannot be strictly proven from telemetry — only that onset (11:01Z) precedes the rollback (11:16Z).
- No sub-minute comparison of api-gateway vs. exchange-gateway vs. order-service onset buckets was obtained; the ordering relies on log timestamps (exchange-gateway first error 11:01:25Z) rather than metric buckets, which all straddle the onset at 11:00Z.
- Kafka/queue metrics and metrics+logs for the shared-infra neighbours portfolio-service, payment-service and settlement-service were not queried, so a host-level shared-infra contribution reaching order-service via neighbours cannot be fully excluded (though ledger-db itself is clean on multiple metrics).
- The order-service log query was truncated (40 of 350 records returned), so additional distinct error signatures between 11:02 and 12:58 could exist, though none appeared in top_patterns.
- No explicit restart-count step for order-service at 11:16Z was checked; the rollback time is taken from the deployment record, not re-derived. This does not affect the exoneration conclusion, since the error rate shows no marker at that timestamp.
- Tool reliability: one query (query_metrics ledger-db latency_p99_ms) failed with not_found, and two calls returned budget_exhausted, limiting coverage of exchange-gateway and neighbouring services.

## What would change this conclusion
- Market-data-service (the second, independent caller of exchange-gateway) showing NO error/latency degradation at ~11:01Z while only order-service traffic fails — that would point to an exchange-gateway-local fault or an order-service-specific path/credential issue rather than a partner-wide outage.
- Discovery of a DNS record, firewall/egress or NAT rule, or TLS certificate change on the exchange-gateway -> api.exchange-partner.example path at ~11:00Z, which would move the origin from the partner to our egress path.
- Partner confirmation that their service was healthy at 11:01Z onward despite the 503s, or evidence that the 503s carrying x-partner-request-id are generated by an intermediary, which would invalidate the partner-side error-surface reading.
- Evidence that order-service's error rate recovered or dropped at any point after the 11:16Z rollback (it did not through 12:59Z) — a genuine recovery step at the rollback boundary would reopen H1 (v2.15.2 regression).
- A distinct order-service error signature (not the downstream 'exchange-gateway call failed: 504') appearing on the submit path, or a targeted code-level read showing dep-5333's idempotency-key change can itself produce submit failures, which would re-raise H1.
- New degradation evidence for pricing-engine (error_rate or WARN+ logs) or for ledger-db/kafka on metrics not yet queried (slow queries, connections, kafka produce path), which would reopen H3 or H4.

## Integrity warnings (set by code)
- fact demoted to assumption: exchange-gateway's request_rate was flat at ~265-280 req/s across the entire win
- fact demoted to assumption: The exchange-gateway upstream failure starts at 11:01:25Z, i.e. order-service's 

## Investigation stats
- latency 251.86s, LLM calls 27, tokens 208103 (in 174039 / out 34064)
- tool calls 46 (1 failed, 0 partial, 0 recovered by retry), findings 35/35 verified
