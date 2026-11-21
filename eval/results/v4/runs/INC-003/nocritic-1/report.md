# Incident investigation report: INC-003
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The order-submit failures (error rate stepping to ~30% at ~10:56–11:00Z) are caused by a degradation of the external exchange partner (api.exchange-partner.example), which broke exchange-gateway's placeOrder/quoteSnapshot calls and propagated 504s up into order-service. Confidence in this root cause is medium (0.60) and it is clearly ahead of the alternatives: order-service v2.15.2 (0.05), the pricing-engine TTL change cfg-2290 (0.04), and an unenumerated shared-infrastructure event (0.03) are all rejected by mechanism and timing evidence. The v2.15.2 deploy at 10:48Z is coincidental — order-service's only error signature is 'exchange-gateway call failed: 504 Gateway Timeout', its resource usage is flat across deploy and onset, its ~5.2s p99 plateau equals the 5000ms upstream connect timeout, exchange-gateway started erroring ~80s earlier, and the downstream failure signature persisted ~1h42m after the 11:16Z rollback. This matches historical precedent HIST-103 (order failures during an external exchange partner outage, not a release).

## Probable root cause
**External exchange partner outage (api.exchange-partner.example) degraded exchange-gateway, propagating 504s to order-service** -- dependency_failure in `exchange-api (api.exchange-partner.example)`, triggered by `null` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange partner outage (api.exchange-partner.example) degraded exchange-gateway, propagating 504s to order-service | dependency_failure | exchange-api (api.exchange-partner.example) | null | 0.60 (medium) | active |
| 2 | H2: order-service v2.15.2 regression (idempotency-key code) caused order submit failures | bad_deployment | order-service | dep-5333 | 0.05 (low) | rejected |
| 3 | H3: pricing-engine cache.ttl_seconds config change (cfg-2290) degraded a shared path feeding order-service | config_change | pricing-engine | cfg-2290 | 0.04 (low) | rejected |
| 4 | H4: Shared infrastructure component (redis-cache failover or kafka rebalance) degraded at onset, indirectly breaking the order path | infrastructure_failure | unknown (redis-cache or kafka) | null | 0.03 (low) | rejected |

### H1: External exchange partner outage (api.exchange-partner.example) degraded exchange-gateway, propagating 504s to order-service
The external exchange partner API (api.exchange-partner.example, has_telemetry=false) began failing/timing out around 11:00Z. exchange-gateway's calls to it blocked on the 5000ms connect timeout and returned 503/504, which surfaced in order-service as 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'. order-service v2.15.2 is incidental (coincidental timing) and the rollback did not clear the symptom.

**Causal chain**
1. External exchange partner (api.exchange-partner.example) starts failing/timing out ~11:00Z *(assumed)*
2. exchange-gateway's upstream calls to the partner block on a 5000ms connect timeout and receive 503s [F10]
3. exchange-gateway error_rate steps from ~0.21% to 39.89% (peak 48.42%) at 11:00 and p99 jumps 130ms -> ~5085ms [F9, F10]
4. order-service's exchange-gateway calls inherit the failure and return 504 Gateway Timeout on placeOrder, 80s after gateway onset [F8, F12]
5. order-service error rate steps sharply from ~0.2% to ~30% and stays flat there; p99 plateaus at ~5.2s matching the upstream 5000ms timeout [F7, F11, F14]
6. The fault persists unchanged ~1h42m after the order-service rollback, and neither service shows local resource saturation [F16, F13, F15, F17]
7. Historical precedent HIST-103 matches: 504 on order submit from an external exchange API outage, not a release; resolved by failover to a secondary route [F5]

**Supporting evidence**
  - `F5` ✔ Historical precedent with matching symptoms: HIST-103 (2025-10-21) 'Order failures during exchange partner outage', services [order-service, exchange-gateway], symptoms ['504 on order submit','upstream timeouts']; its root cause was NOT a release: 'External exchange API outage on the partner side' (category dependency_failure), resolved by failing over to a secondary exchange route. Other 'failure after release' precedents: HIST-101 auth-service (bcrypt cost 10->14), HIST-109 auth-service (JWT library clock-skew default) — both bad_deployment resolved by rollback.
  - `F7` ✔ order-service error rate is flat ~0.17–0.25% from 08:00 through 10:48, then steps to 8.02% at 10:56 and settles at ~30–31.5% (30.44 at 11:04) and stays there through 12:56 — a sharp step change, not a ramp.
  - `F8` ✔ All 349 order-service ERROR logs in 11:02:45–12:58:54 carry a single pattern: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; the earliest is orderId=07fcb25a at 11:02:45.
  - `F9` ✔ exchange-gateway shows the anomaly earlier than order-service: its first ERROR is at 11:01:25 and error_rate jumps from ~0.21% baseline to 39.89% in the 11:00 bucket (peak 48.42%); p99 latency jumps from ~130ms to 5085ms avg at 11:04.
  - `F10` ✔ exchange-gateway's own upstream call is the failing dependency: logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163) and op=quoteSnapshot (146), plus 'exchange-api returned 503 Service Unavailable' (161); upstream_latency_ms jumps from ~118ms to ~8408ms avg at 11:00 (max 10042ms).
  - `F11` ✔ The 30% failure is ~uniform across time, not spiky: order-service error_rate buckets stay tightly in 30.44–31.64% for every bucket from 11:04 to 12:56 (max never above ~35.6%).
  - `F12` ✔ order-service's own error log onset (11:02:45) lags exchange-gateway's (11:01:25) by ~80s, consistent with the failure originating downstream at the exchange-gateway/partner boundary rather than inside order-service; this ordering is a data observation, the causal direction is inference.
  - `F13` ✔ order-service shows NO local resource saturation at or after onset: cpu_pct is flat ~39-40% avg (max 45.44%) across all buckets 09:00-12:56 (e.g. 39.91 avg at 10:50, 39.78 at 11:00, 40.08 at 11:10), and memory_pct is flat ~58-62% avg (max 67.93%) with no step at 11:00. db_pool_in_use is flat ~14 connections (13.85 avg at 11:00, 14.04 at 11:10; max 15.49 over the whole window) — no pool exhaustion.
  - `F14` ✔ order-service latency p99 steps abruptly at the 11:00 bucket from a flat ~147-153ms baseline (150.67 avg at 10:40, 149.93 at 10:50) to 4285.85ms avg (max 5631.42) and stays ~5200-5400ms through 12:50. The ~5.2s plateau matches the 5000ms upstream connect timeout, i.e. requests are blocking on a downstream dependency, not on local CPU/memory/DB.
  - `F15` ✔ exchange-gateway, the failing dependency, also shows no resource driving the fault: cpu_pct flat ~29-30% avg (max 33.58%) and memory_pct flat ~44% avg (max 48.99%) across 09:00-12:56, unchanged through onset. So the gateway's degradation is not CPU/memory-driven.
  - `F16` ✔ exchange-gateway's failures continue unchanged long after the order-service rollback (11:16Z): logs from 11:20:03Z to 12:58:32Z still show 396 ERROR entries with the same three patterns — 'exchange-api returned 503 Service Unavailable' (140), 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (133), and '... op=quoteSnapshot' (123). The downstream failure signature persists ~1h42m past the rollback.
  - `F17` ✔ The fault is NOT inside order-service v2.15.2 and is not resource-driven: order-service's own resources (CPU, memory, DB pool) are all flat across the deploy and onset, while its only error signature is the downstream 504 (per T1/F8) and its p99 plateau equals the downstream 5000ms timeout. The downstream exchange-gateway to external api.exchange-partner.example (has_telemetry=false) boundary is where 503s/timeouts originate, and it keeps failing after the rollback. This mirrors HIST-103 (external exchange API outage, not a release).
**Contradicting evidence**
  - `F1` ✔ The only deployment in the window 2026-03-16T12:00Z to 2026-03-17T12:59Z across ALL services is order-service v2.15.2 (dep-5333), deployed 2026-03-17T10:48:00Z by a.khan, and it was already rolled back at 2026-03-17T11:16:00Z (status 'rolled_back'). Summary: 'Add idempotency keys to order submit'; changes: 'Idempotency-Key header persisted in orders table', 'retry-safe submit handler'.
**Assumptions**
  - The partner's own degradation is the origin rather than an intermediate hop (partner telemetry is unavailable, so only gateway-side symptoms were observed)
  - The 'upstream connect timeout after 5000ms host=api.exchange-partner.example' messages are truly partner-side rather than DNS/network in the path to the partner
  - F18's inference that the rollback did not restore success rate is correct (the fine-grained post-rollback error-rate query was never executed)

### H2: order-service v2.15.2 regression (idempotency-key code) caused order submit failures
The reporter's suspicion: order-service v2.15.2 introduced a regression in the retry-safe submit handler / idempotency-key persistence that broke order submission, and the ~30% failure rate is a rollout-wide artifact of that code.

**Causal chain**
1. order-service v2.15.2 (dep-5333) deployed 10:48Z introduces a submit-handler/idempotency regression [F1]
2. ~8 minutes later order-service error rate steps from ~0.2% to 8% then ~30% [F7]
3. Requests fail with 504s on order submit *(assumed)*

**Supporting evidence**
  - `F1` ✔ The only deployment in the window 2026-03-16T12:00Z to 2026-03-17T12:59Z across ALL services is order-service v2.15.2 (dep-5333), deployed 2026-03-17T10:48:00Z by a.khan, and it was already rolled back at 2026-03-17T11:16:00Z (status 'rolled_back'). Summary: 'Add idempotency keys to order submit'; changes: 'Idempotency-Key header persisted in orders table', 'retry-safe submit handler'.
  - `F7` ✔ order-service error rate is flat ~0.17–0.25% from 08:00 through 10:48, then steps to 8.02% at 10:56 and settles at ~30–31.5% (30.44 at 11:04) and stays there through 12:56 — a sharp step change, not a ramp.
**Contradicting evidence**
  - `F5` ✔ Historical precedent with matching symptoms: HIST-103 (2025-10-21) 'Order failures during exchange partner outage', services [order-service, exchange-gateway], symptoms ['504 on order submit','upstream timeouts']; its root cause was NOT a release: 'External exchange API outage on the partner side' (category dependency_failure), resolved by failing over to a secondary exchange route. Other 'failure after release' precedents: HIST-101 auth-service (bcrypt cost 10->14), HIST-109 auth-service (JWT library clock-skew default) — both bad_deployment resolved by rollback.
  - `F8` ✔ All 349 order-service ERROR logs in 11:02:45–12:58:54 carry a single pattern: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; the earliest is orderId=07fcb25a at 11:02:45.
  - `F9` ✔ exchange-gateway shows the anomaly earlier than order-service: its first ERROR is at 11:01:25 and error_rate jumps from ~0.21% baseline to 39.89% in the 11:00 bucket (peak 48.42%); p99 latency jumps from ~130ms to 5085ms avg at 11:04.
  - `F10` ✔ exchange-gateway's own upstream call is the failing dependency: logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163) and op=quoteSnapshot (146), plus 'exchange-api returned 503 Service Unavailable' (161); upstream_latency_ms jumps from ~118ms to ~8408ms avg at 11:00 (max 10042ms).
  - `F12` ✔ order-service's own error log onset (11:02:45) lags exchange-gateway's (11:01:25) by ~80s, consistent with the failure originating downstream at the exchange-gateway/partner boundary rather than inside order-service; this ordering is a data observation, the causal direction is inference.
  - `F13` ✔ order-service shows NO local resource saturation at or after onset: cpu_pct is flat ~39-40% avg (max 45.44%) across all buckets 09:00-12:56 (e.g. 39.91 avg at 10:50, 39.78 at 11:00, 40.08 at 11:10), and memory_pct is flat ~58-62% avg (max 67.93%) with no step at 11:00. db_pool_in_use is flat ~14 connections (13.85 avg at 11:00, 14.04 at 11:10; max 15.49 over the whole window) — no pool exhaustion.
  - `F14` ✔ order-service latency p99 steps abruptly at the 11:00 bucket from a flat ~147-153ms baseline (150.67 avg at 10:40, 149.93 at 10:50) to 4285.85ms avg (max 5631.42) and stays ~5200-5400ms through 12:50. The ~5.2s plateau matches the 5000ms upstream connect timeout, i.e. requests are blocking on a downstream dependency, not on local CPU/memory/DB.
  - `F15` ✔ exchange-gateway, the failing dependency, also shows no resource driving the fault: cpu_pct flat ~29-30% avg (max 33.58%) and memory_pct flat ~44% avg (max 48.99%) across 09:00-12:56, unchanged through onset. So the gateway's degradation is not CPU/memory-driven.
  - `F16` ✔ exchange-gateway's failures continue unchanged long after the order-service rollback (11:16Z): logs from 11:20:03Z to 12:58:32Z still show 396 ERROR entries with the same three patterns — 'exchange-api returned 503 Service Unavailable' (140), 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (133), and '... op=quoteSnapshot' (123). The downstream failure signature persists ~1h42m past the rollback.
  - `F17` ✔ The fault is NOT inside order-service v2.15.2 and is not resource-driven: order-service's own resources (CPU, memory, DB pool) are all flat across the deploy and onset, while its only error signature is the downstream 504 (per T1/F8) and its p99 plateau equals the downstream 5000ms timeout. The downstream exchange-gateway to external api.exchange-partner.example (has_telemetry=false) boundary is where 503s/timeouts originate, and it keeps failing after the rollback. This mirrors HIST-103 (external exchange API outage, not a release).
  - `F18` ✔ Could not directly verify whether the 11:16Z rollback restored order-service success rate: the error_rate query restricted to 11:00-12:59 for order-service was not executed (tool-call budget exhausted). Contextual evidence from T1/F7/F11 and tc-020 is that error rate stayed ~30-31% through 12:56 and exchange-gateway kept erroring through 12:58, i.e. the rollback did not clear the symptom.
**Assumptions**
  - The identical downstream 504 signature is somehow generated by the new code rather than by the dependency
  - The rollback at 11:16Z failed to take effect
**Rejected because:** Contradicted by mechanism and intervention evidence: every order-service error is an 'exchange-gateway call failed: 504 Gateway Timeout' (F8), exchange-gateway errored ~80s before order-service (F9, F12), the failing upstream is api.exchange-partner.example (F10), order-service resources were flat across deploy and onset (F13), and the downstream signature persisted ~1h42m after the v2.15.2 rollback (F16, F18). The deployment is not the evidenced origin.

### H3: pricing-engine cache.ttl_seconds config change (cfg-2290) degraded a shared path feeding order-service
cfg-2290 lowered pricing-engine cache.ttl_seconds 5->4 at 09:20Z, ~1h28m before onset. A shorter TTL could increase origin load / backend pressure and, if it affected a shared cache path used by exchange-gateway or the partner-facing chain, could cause the observed timeouts.

**Causal chain**
1. pricing-engine cache TTL lowered 5->4 at 09:20Z by config-bot [F2]
2. Shorter TTL increases downstream/origin fetch load, degrading a shared component (e.g. redis-cache, market-data-service) [F3, F4]
3. The degraded shared component propagates timeouts toward order-service *(assumed)*

**Supporting evidence**
  - `F2` ✔ The only config/feature-flag change in the window is pricing-engine cache.ttl_seconds lowered 5 -> 4 (cfg-2290) at 2026-03-17T09:20:00Z by config-bot, i.e. ~1h28m BEFORE the order-service v2.15.2 deploy. pricing-engine is a direct HTTP dependency of order-service.
  - `F3` ✔ order-service dependency map (depth 3, downstream): directly depends on ledger-db (db), pricing-engine (http), exchange-gateway (http), kafka (queue-produce). Transitively: pricing-engine -> market-data-service (http) and -> redis-cache; market-data-service -> redis-cache and -> exchange-gateway; exchange-gateway -> exchange-api (external, has_telemetry=false).
**Contradicting evidence**
  - `F4` ✔ Shared components in order-service's chain are used by other services too: ledger-db (postgres, shared=true) is used by portfolio-service, payment-service and settlement-service; redis-cache (shared=true) by auth-service, portfolio-service, pricing-engine and market-data-service; kafka (shared=true) by payment-service, settlement-service and notification-service. api-gateway (edge) is the sole upstream caller of order-service (depth 2 found only api-gateway->order-service).
  - `F10` ✔ exchange-gateway's own upstream call is the failing dependency: logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163) and op=quoteSnapshot (146), plus 'exchange-api returned 503 Service Unavailable' (161); upstream_latency_ms jumps from ~118ms to ~8408ms avg at 11:00 (max 10042ms).
  - `F15` ✔ exchange-gateway, the failing dependency, also shows no resource driving the fault: cpu_pct flat ~29-30% avg (max 33.58%) and memory_pct flat ~44% avg (max 48.99%) across 09:00-12:56, unchanged through onset. So the gateway's degradation is not CPU/memory-driven.
  - `F16` ✔ exchange-gateway's failures continue unchanged long after the order-service rollback (11:16Z): logs from 11:20:03Z to 12:58:32Z still show 396 ERROR entries with the same three patterns — 'exchange-api returned 503 Service Unavailable' (140), 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (133), and '... op=quoteSnapshot' (123). The downstream failure signature persists ~1h42m past the rollback.
**Assumptions**
  - pricing-engine sits on the failing path at all — it is not an ancestor of exchange-gateway or of the partner call
  - A 1-second TTL delta is large enough to saturate a shared component
  - exchange-gateway's degradation is not upstream of pricing-engine
**Rejected because:** Timing and mechanism both fail: the change precedes onset by ~1h28m with no intermediate degradation, order-service's error signature points exclusively at exchange-gateway/api.exchange-partner.example (F8, F10, F12), and exchange-gateway shows no resource pressure (F15) while the small TTL delta produces no evidence of load impact. cfg-2290 is not on the degraded path.

### H4: Shared infrastructure component (redis-cache failover or kafka rebalance) degraded at onset, indirectly breaking the order path
An unenumerated infrastructure event (redis-cache primary failover like HIST-107, or a kafka broker rebalance like HIST-104) affected a shared component in order-service's chain, producing the ~30% order failures and downstream timeouts.

**Causal chain**
1. An infra event (redis failover / kafka rebalance) occurred near 11:00Z; no dedicated infra-change feed was queried so it cannot be excluded *(assumed)*
2. The affected shared component degrades consumers, but the observed failing hop is api.exchange-partner.example via exchange-gateway [F8, F10]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ All 349 order-service ERROR logs in 11:02:45–12:58:54 carry a single pattern: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; the earliest is orderId=07fcb25a at 11:02:45.
  - `F10` ✔ exchange-gateway's own upstream call is the failing dependency: logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163) and op=quoteSnapshot (146), plus 'exchange-api returned 503 Service Unavailable' (161); upstream_latency_ms jumps from ~118ms to ~8408ms avg at 11:00 (max 10042ms).
  - `F13` ✔ order-service shows NO local resource saturation at or after onset: cpu_pct is flat ~39-40% avg (max 45.44%) across all buckets 09:00-12:56 (e.g. 39.91 avg at 10:50, 39.78 at 11:00, 40.08 at 11:10), and memory_pct is flat ~58-62% avg (max 67.93%) with no step at 11:00. db_pool_in_use is flat ~14 connections (13.85 avg at 11:00, 14.04 at 11:10; max 15.49 over the whole window) — no pool exhaustion.
  - `F15` ✔ exchange-gateway, the failing dependency, also shows no resource driving the fault: cpu_pct flat ~29-30% avg (max 33.58%) and memory_pct flat ~44% avg (max 48.99%) across 09:00-12:56, unchanged through onset. So the gateway's degradation is not CPU/memory-driven.
  - `F16` ✔ exchange-gateway's failures continue unchanged long after the order-service rollback (11:16Z): logs from 11:20:03Z to 12:58:32Z still show 396 ERROR entries with the same three patterns — 'exchange-api returned 503 Service Unavailable' (140), 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (133), and '... op=quoteSnapshot' (123). The downstream failure signature persists ~1h42m past the rollback.
  - `F17` ✔ The fault is NOT inside order-service v2.15.2 and is not resource-driven: order-service's own resources (CPU, memory, DB pool) are all flat across the deploy and onset, while its only error signature is the downstream 504 (per T1/F8) and its p99 plateau equals the downstream 5000ms timeout. The downstream exchange-gateway to external api.exchange-partner.example (has_telemetry=false) boundary is where 503s/timeouts originate, and it keeps failing after the rollback. This mirrors HIST-103 (external exchange API outage, not a release).
**Assumptions**
  - A redis failover or kafka rebalance coincided with the 11:00 onset
  - It would manifest as exchange-partner connect timeouts and 503s rather than redis/kafka-specific errors
**Rejected because:** No infra change was enumerated in the window (F6 only lists the deploy and the TTL change), and the observed failure signature is exclusively api.exchange-partner.example 503s/connect timeouts (F10, F16) with no redis or kafka error evidence; order-service db pool and resources are flat (F13). Speculative and unsupported.

## Facts (verified observations)
- order-service error rate was flat ~0.17–0.25% from 08:00 to 10:48Z, stepped to 8.02% at 10:56Z, and settled at ~30–31.5% (30.44% at 11:04Z), holding through 12:56Z — a sharp step, not a ramp. [F7]
- All 349 order-service ERROR logs between 11:02:45Z and 12:58:54Z carry a single pattern: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'. [F8]
- exchange-gateway degraded earlier than order-service: first ERROR at 11:01:25Z, error rate jumping from ~0.21% baseline to 39.89% in the 11:00 bucket (peak 48.42%), p99 latency from ~130ms to 5085ms avg at 11:04Z. [F9]
- exchange-gateway's failing upstream dependency is the external partner: logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163), op=quoteSnapshot (146), and 'exchange-api returned 503 Service Unavailable' (161); upstream latency jumped from ~118ms to ~8408ms avg at 11:00Z (max 10042ms). [F10]
- The ~30% failure is time-uniform: order-service error-rate buckets stay tightly within 30.44–31.64% for every bucket from 11:04Z to 12:56Z. [F11]
- order-service shows no local resource saturation at or after onset: cpu_pct flat ~39–40% avg (max 45.44%), memory_pct flat ~58–62% avg (max 67.93%), db_pool_in_use flat ~14 connections (max 15.49) across 09:00–12:56Z. [F13]
- order-service p99 latency stepped at the 11:00Z bucket from a flat ~147–153ms baseline to 4285.85ms avg and held at a ~5200–5400ms plateau through 12:50Z — matching the 5000ms upstream connect timeout, i.e. requests blocking on a downstream dependency. [F14]
- exchange-gateway, the failing dependency, also shows no resource driving the fault: cpu_pct flat ~29–30% avg (max 33.58%) and memory_pct flat ~44% avg (max 48.99%) across 09:00–12:56Z, unchanged through onset. [F15]
- exchange-gateway's failures continued unchanged ~1h42m after the order-service rollback (11:16Z): 396 ERROR entries from 11:20:03Z to 12:58:32Z with the same three patterns (503s, op=placeOrder connect timeouts, op=quoteSnapshot connect timeouts). [F16]
- order-service v2.15.2 (dep-5333) was deployed at 10:48:00Z and was already rolled back at 11:16:00Z; it is the only deployment in the window across all services; contextual evidence is that error rate stayed ~30–31% through 12:56Z and exchange-gateway kept erroring through 12:58Z, i.e. the rollback did not clear the symptom. [F1]
- The only config change in the window is pricing-engine cache.ttl_seconds 5 -> 4 (cfg-2290) at 09:20:00Z, ~1h28m before the deploy — timing and mechanism both fail to explain the 11:00Z onset. [F2]
- order-service transitively depends on exchange-gateway -> exchange-api (external, has_telemetry=false); api-gateway is the sole upstream caller of order-service. [F3, F4]
- Historical precedent HIST-103 (2025-10-21) 'Order failures during exchange partner outage' had matching symptoms (504 on order submit, upstream timeouts) and was caused by an external exchange API outage, not a release. [F5]

## Assumptions (not verified)
- Assumed (consistent with H1's stated assumptions): the partner's own degradation is the origin rather than an intermediate hop; because the partner has has_telemetry=false, only gateway-side symptoms were observed and the partner-side origin is inferred, not directly measured.
- Assumed: the 'upstream connect timeout after 5000ms host=api.exchange-partner.example' messages reflect the partner side rather than DNS or network in the path to the partner.
- Assumed (per F18's inference): the 11:16Z rollback did not restore order-service success rate — the fine-grained post-rollback error-rate query was never executed, so this rests on contextual evidence rather than a direct measurement.
- Assumed: the ~30% aggregate failure rate is fairly distributed rather than concentrated in a subset of instances/hosts — no per-instance breakdown was retrieved.
- Assumed: no non-deployment infrastructure change (e.g. redis failover, kafka rebalance) occurred in the window; no dedicated infra change feed was queried, so this is inferred from change enumeration showing only the deploy and the TTL change.
- (demoted from facts: no verified observation backs it) order-service's error-log onset (11:02:45Z) lags exchange-gateway's (11:01:25Z) by ~80s, consistent with the failure originating at the downstream exchange-gateway/partner boundary.
- (demoted from facts: no verified observation backs it) The fault is not inside order-service v2.15.2 and is not resource-driven: order-service resources are flat across deploy and onset, its only error signature is the downstream 504, and its p99 plateau equals the downstream 5000ms timeout — mirroring HIST-103 (external exchange API outage, not a release).

## Timeline
- `2026-03-17T09:20:00Z` Only config change in the window: pricing-engine cache.ttl_seconds lowered 5 -> 4 (cfg-2290) by config-bot (~1h28m before onset). [F2]
- `2026-03-17T10:48:00Z` order-service v2.15.2 (dep-5333) deployed by a.khan — the only deployment in the window; order-service error rate still flat ~0.17–0.25% at this point. [F1, F7]
- `2026-03-17T10:56:00Z` order-service error rate steps from flat baseline to 8.02%, the first visible break in the flat series. [F7]
- `2026-03-17T11:00:00Z` exchange-gateway error rate jumps from ~0.21% to 39.89% (peak 48.42%) and upstream latency to api.exchange-partner.example jumps ~118ms -> ~8408ms avg (max 10042ms); order-service p99 steps from ~150ms to 4285.85ms. [F9, F10, F14]
- `2026-03-17T11:01:25Z` First exchange-gateway ERROR — earlier than order-service's first error, indicating downstream origin. [F9, F12]
- `2026-03-17T11:02:45Z` First order-service ERROR ('exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', orderId=07fcb25a) — ~80s after exchange-gateway's first error. [F8, F12]
- `2026-03-17T11:10:00Z` Incident INC-003 reported by on-call SRE; order-service submit failure rate ~30%, 504s on submit; rollback of v2.15.2 initiated. []
- `2026-03-17T11:16:00Z` order-service v2.15.2 rollback completed (status 'rolled_back'). [F1, F18]
- `2026-03-17T11:20:03Z–12:58:32Z` exchange-gateway continues failing unchanged with the same three error patterns (396 ERRORs) — downstream failure persists ~1h42m past the rollback. [F16]
- `2026-03-17T12:56:00Z` order-service error rate still ~30–31% and p99 ~5200–5400ms; the rollback did not clear the symptom. [F7, F11, F14, F18]

## Recommended actions
- **[immediate]** Stop the rollback of order-service v2.15.2 as the mitigation path (it is already complete, status 'rolled_back' at 11:16Z) and treat the exchange path as the active fault: fail over exchange-gateway's exchange-api route to the secondary exchange route, as was done to resolve HIST-103. -- The failure signature persisted ~1h42m after the rollback (F16, F18) and order-service's only error is the downstream 504 (F8); HIST-103 was resolved precisely by failing over to a secondary exchange route (F5). [F1, F8, F16, F18, F5]
- **[immediate]** Contact/confirm the external exchange partner (api.exchange-partner.example) outage status and reduce exchange-gateway's exposure to it (e.g. lower connect timeout below 5000ms, enable circuit-breaking on the placeOrder/quoteSnapshot calls) to shed blocked requests while the partner recovers. -- The failing dependency is the partner boundary: 163 op=placeOrder and 146 op=quoteSnapshot 5000ms connect timeouts plus 161 'exchange-api returned 503' (F10), and the order-service 5.2s p99 plateau exactly matches that connect timeout (F14). [F10, F14]
- **[short_term]** Run the fine-grained order-service error-rate query for 11:00–12:59Z (the unexecuted query behind F18) to confirm that the rollback produced no change, closing the last measurement gap in the causal chain. -- The rollback's non-effect is currently supported by contextual evidence only (F18); a direct post-rollback curve removes the last ambiguity in attributing the incident away from dep-5333. [F18, F7, F11]
- **[short_term]** Keep exchange-gateway's 503/504 semantics from masquerading as an order-service fault: verify that order-service surfaces dependency-specific error codes/retry hints rather than generic 504s, and reconcile the ~30% failure rate with the ~40–48% exchange-gateway error rate. -- order-service emits only 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' (F8) while exchange-gateway error rate peaked at 48.42% (F9) yet order-service settled at ~30% (F11); clarifying the mapping helps triage speed next time. [F8, F9, F11]
- **[long_term]** Add an explicit health/degradation signal (or partner-side telemetry/SLO alert) for the external exchange partner (api.exchange-partner.example, currently has_telemetry=false), so partner degradation is detected before it propagates to exchange-gateway and order-service. -- The partner has no telemetry, so the outage could only be inferred from gateway-side symptoms (F10, F17); this is the same blind spot that HIST-103 exposed. [F10, F17, F5]
- **[long_term]** Introduce a release-vs-dependency discriminator into incident runbooks (compare onset ordering between a service and its downstream, check resource flatness, and verify whether rollback changes the symptom) so coincidental deploys are not chased. -- The v2.15.2 deploy was coincidental yet consumed the first response: error signature is purely downstream (F8), resources flat across deploy and onset (F13, F15), and downstream failures persisted post-rollback (F16, F18). [F8, F13, F15, F16, F18]

## Data gaps
- No dedicated infra-change / rollback / restart feed was queried beyond get_deployments and get_config_changes (both count=1), so a non-deployment infrastructure change (redis failover, kafka rebalance like HIST-104/107) coinciding with the 11:00Z onset cannot be fully ruled out.
- api.exchange-partner.example has has_telemetry=false and query_logs against it failed (tc-016 not_found): the partner's own latency/error metrics could not be measured directly, so the partner-side origin is inferred from gateway-side 503/timeout signatures only.
- The order-service error_rate query restricted to 11:00–12:59Z (to confirm the rollback's effect at fine granularity) was never executed (tool-call budget exhausted) — the rollback's non-effect rests on contextual evidence (F18).
- Other services on the order path (api-gateway, payment-service, settlement-service, pricing-engine, market-data-service, ledger-db, redis-cache, kafka) were not checked for simultaneous 09:00–13:00 anomalies, so blast radius beyond order-service + exchange-gateway + partner is not established.
- Only aggregate service-level metrics were retrieved; no per-instance/per-host or per-endpoint breakdown exists, so instance concentration cannot be confirmed or excluded.
- ledger-db error_rate query returned not_found (tc-024) and only connections_active/cpu/disk/replication/slow-query metrics are exposed, so shared-Postgres health was not directly assessed (order-service db_pool_in_use was flat, though).
- No deployment-marker/restart metric was queried for order-service, so whether the v2.15.2 rollback even took effect on serving instances cannot be quantified.
- Historical search returned only 6 incidents and none matched '30% failure after release' on order-service itself; the closest match was HIST-103 (external dependency failure).

## What would change this conclusion
- Direct partner-side telemetry showing api.exchange-partner.example was healthy at 11:00Z would break the H1 mechanism and push the investigation toward an intermediate hop (DNS/network path to the partner) or another root cause.
- A confirmed non-deployment infrastructure event (e.g. redis-cache failover, kafka rebalance) coinciding with the 11:00Z onset would rehabilitate H4, since no dedicated infra change feed was queried.
- A fine-grained post-rollback order-service error-rate curve showing the error rate dropped after 11:16Z would contradict the rollback-ineffectiveness evidence (F18) and reinstate H2 (order-service v2.15.2 regression).
- Evidence that order-service's error signature (exchange-gateway 504s) is generated by new code paths in v2.15.2 rather than by blocked dependency calls would overturn the 'deploy is incidental' conclusion; the ~5.2s p99 plateau matching the 5000ms connect timeout currently argues strongly against this.
- Per-instance or per-host data showing failures concentrated on instances running v2.15.2 (rather than uniformly across instances) would weaken H1 in favor of a deployment- or instance-specific cause.
- Finding that exchange-gateway itself is the faulty component (e.g. its connection pool or retry logic failing locally) rather than its partner calls timing out would move the root cause one hop closer, from the external partner to exchange-gateway.
- Evidence that the 09:20Z pricing-engine TTL change (cfg-2290) sits on a shared path that feeds the partner-facing chain would revive H3, though the ~1h28m gap before onset and the absence of intermediate degradation argue against it.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: order-service's error-log onset (11:02:45Z) lags exchange-gateway's (11:01:25Z) 
- fact demoted to assumption: The fault is not inside order-service v2.15.2 and is not resource-driven: order-

## Investigation stats
- latency 137.08s, LLM calls 13, tokens 93649 (in 76078 / out 17571)
- tool calls 24 (2 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
