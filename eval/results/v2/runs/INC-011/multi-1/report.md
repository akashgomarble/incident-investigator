# Incident investigation report: INC-011
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause (confidence 0.85, high): the cfg-3120 feature-flag ramp on pricing-engine (flags.pricing.greeks_v2.rollout_pct 5 -> 100) at 2026-05-20T12:04:00Z put the CPU-heavy Greeks v2 Monte-Carlo path (20000 paths) on all quote traffic, driving pricing-engine CPU from ~40% to ~99% and per-request latency from ~60ms to ~2.6s, which exceeded order-service's 2500ms quote-call deadline and produced the order-service quote timeouts. The mechanism is a pure per-request CPU-cost step: pricing-engine request_rate is flat across the onset (625.14 req/s at the 12:04Z bucket vs 667.24 at 12:02Z) while cpu_pct jumps 40%->99% in the same bucket, refuting the organic-volume/capacity alternative (H4). The reporter's suspected market-data-service protobuf deploy (dep-6111, 11:56Z) is refuted: it precedes onset by 8 minutes, market-data-service shows no telemetry anomaly, and order-service's errors are timeouts, not serialization errors. The shared redis-cache/exchange-gateway degradation hypothesis is positively contradicted by flat cache CPU/clients/hit-ratio/evictions and flat exchange-gateway latency with zero component errors.

## Probable root cause
**Greeks v2 rollout flag ramp to 100% saturates pricing-engine CPU, stalling quote computation** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Greeks v2 rollout flag ramp to 100% saturates pricing-engine CPU, stalling quote computation | config_change | pricing-engine | cfg-3120 | 0.85 (high) | active |
| 2 | H3: Shared redis-cache / exchange-gateway degradation on the shared quote path | infrastructure_failure | redis-cache | null | 0.04 (low) | rejected |
| 3 | H2: market-data-service protobuf serialization change (reporter's suspicion) broke/slowed the quote path | bad_deployment | market-data-service | dep-6111 | 0.03 (low) | rejected |
| 4 | H4: Capacity saturation: market-open/volume-driven load exceeding pricing-engine provisioned capacity | capacity_saturation | pricing-engine | null | 0.03 (low) | rejected |

### H1: Greeks v2 rollout flag ramp to 100% saturates pricing-engine CPU, stalling quote computation
pricing-engine's GreeksV2 calculator (Monte-Carlo, 20000 paths) is CPU-heavy. Config change cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z, putting the expensive path on all quote traffic. pricing-engine CPU jumped from ~40% to ~99% at exactly the same 12:04Z bucket with request_rate flat — a pure per-request cost step. Per-request latency rose to ~2.6s, exceeding order-service's 2500ms quote-call deadline, producing order-service quote timeouts. Origin is a feature-flag ramp (config_change); no service code shipped in this window.

**Causal chain**
1. cfg-3120 ramps flags.pricing.greeks_v2.rollout_pct from 5 to 100 on pricing-engine at 12:04:00Z [F8, F13, F16]
2. Heavy GreeksV2 Monte-Carlo (20000 paths) path now exercised for all quote requests, driving pricing-engine CPU from ~40% to ~99% while memory stays flat (not memory/GC) and request_rate stays flat (not volume) [F4, F2, F16, F19, F20, F21, F23]
3. CPU saturation raises pricing-engine per-request latency to ~2.6s p99 in the same 12:04Z bucket as the CPU jump [F3, F4, F21]
4. pricing-engine's own first WARN cites 'GreeksV2Calculator: Monte-Carlo paths=20000 took ~3000-8900ms' and its own errors are GreeksV2 deadline-exceeded [F2]
5. order-service's http call to pricing-engine exceeds its 2500ms deadline, producing the identical 'pricing-engine quote call timed out after 2500ms' errors [F1]
6. Reported symptom: order-service quote timeouts at ~8% error rate from 12:04Z onward [F5]

**Supporting evidence**
  - `F8` ✔ Exactly one config/flag change in the window: cfg-3120 on pricing-engine, key flags.pricing.greeks_v2.rollout_pct, 5 -> 100, changed_at 2026-05-20T12:04:00Z, author pm-experiments ('Ramp Greeks v2 experiment to all traffic'). Note this lands 8 minutes AFTER the market-data deploy and is the closest change to the 12:16Z report.
  - `F13` ✔ Ordering of the two changes vs onset: only deployment dep-6111 (market-data-service v5.2.0 'Switch quote serialization to protobuf') at 2026-05-20T11:56:00Z, and only config change cfg-3120 (pricing-engine flags.pricing.greeks_v2.rollout_pct 5->100) at 2026-05-20T12:04:00Z. Narrow re-query of 11:40-12:20Z and 11:50-12:10Z returns the same single items, both partial:false, so the set is complete for this window.
  - `F16` ✔ The pricing-engine flag ramp cfg-3120 is the only change whose timestamp coincides exactly with the onset bucket: changed_at 12:04:00Z equals the 12:04Z bucket in which T1/F3 found pricing-engine latency_p99 jump ~59-61ms -> 2291ms and F4 found cpu_pct 39-41% -> 91.5-100%, with pricing-engine's own first WARN at 12:05:21Z ('GreeksV2Calculator: Monte-Carlo paths=20000 took ~3000-8900ms'). The flag author/description flags it as the Greeks v2 ramp, matching the calculator named in the warnings.
  - `F4` ✔ pricing-engine's latency is CPU-driven, not memory/GC-driven: cpu_pct sat at ~39-41% through 11:56Z and jumped to 91.5% (max 100.0%) at 12:04Z, holding ~98.7-99.3% for the rest of the window, while memory_pct stayed flat at ~47.7-51.0% (min 44.65, max 54.11) with no rise at onset.
  - `F3` ✔ pricing-engine latency_p99_ms jumped in the SAME 12:04Z bucket as order-service: baseline ~59-61ms through 11:56Z, then 2291ms at 12:04Z, settling at ~2560-2706ms (max 2849ms) thereafter — matching the reported '~2.6s'. order-service latency_p99 likewise jumped in the 12:04Z bucket from ~149-154ms to 2149ms then ~2877ms.
  - `F2` ✔ pricing-engine's earliest in-window signal is 12:05:21Z: 342 WARN 'GreeksV2Calculator: Monte-Carlo paths=20000 took ~3000-8900ms' events, and its own first ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' at 12:06:41Z. pricing-engine's own errors are deadline-exceeded on its GreeksV2 calculator, not serialization errors.
  - `F1` ✔ order-service first ERROR in the 11:00-14:00Z window is at 12:06:10Z and every error carries the identical signature 'pricing-engine quote call timed out after 2500ms' — 225 such errors, all one pattern, no 5xx or serialization errors. So the failing downstream call is order-service -> pricing-engine.
  - `F5` ✔ order-service error_rate confirms the same onset: flat ~0.17-0.25% through 11:56Z, then 6.08% at 12:04Z and ~8.1-8.4% thereafter (max 8.87%). This is an order-of-magnitude step at exactly the same bucket as the pricing-engine latency/CPU jump.
  - `F17` ✔ Call-path placement: on the quote path order-service -> pricing-engine (http) -> market-data-service (http), pricing-engine is the direct callee of order-service and the caller of market-data-service; pricing-engine and market-data-service share redis-cache. cfg-3120 sits squarely on the failing path (pricing-engine's quote/Greeks computation); dep-6111 sits one hop further downstream and only affects the pricing-engine->market-data-service edge.
  - `F19` ✔ pricing-engine request_rate did NOT spike at the 12:04Z onset bucket: at 120s resolution it reads 623.65 (11:56Z), 606.28 (11:58Z), 647.20 (12:00Z), 667.24 (12:02Z), 625.14 (12:04Z), 666.82 (12:06Z) req/s — i.e. the 12:04Z bucket (625.14) is BELOW the preceding 12:02Z bucket (667.24) and within the baseline noise band.
  - `F20` ✔ pricing-engine request_rate is flat across the whole 11:50–13:59Z window: 180s buckets range only 582.43–741.12 req/s, mean 646.11, with no step at 12:04Z (12:02Z avg 653.76, 12:05Z avg 652.37, 12:08Z 634.18); the post-onset level equals the pre-onset level.
  - `F21` ✔ In the SAME 12:04Z bucket where pricing-engine request_rate was flat (625.14), cpu_pct jumped from a 38-41% baseline to avg 68.44 / max 98.65, then 98.67-99.56 for the rest of the window — a pure CPU-cost step with no corresponding request volume change.
  - `F22` ✔ order-service request_rate over the same 11:50–13:59Z window is flat and shows no step at 12:04Z: the 12:02Z bucket avg 239.82 vs the 12:05Z bucket avg 231.73, and the overall range is only 216.83–256.97 req/s (mean 233.06) — no capacity ramp on the caller side either.
  - `F23` ✔ Discriminator verdict: a flat request rate coinciding with a 40%→99% CPU jump confirms H1 (fixed code-path cost per request from the Greeks v2 rollout) and is inconsistent with H4 (organic volume / market-open capacity saturation), which requires a request-volume spike that is absent from both pricing-engine and order-service.
**Contradicting evidence**
  - `F6` ✔ market-data-service (the service the report blames, which 'shipped a serialization change') shows NO error/latency anomaly in logs in the 11:00-12:20Z window: only a single unrelated WARN 'deprecated TLS cipher offered by client' at 11:43:53Z, zero ERROR entries.
  - `F15` ✔ Contradicting evidence against the market-data-service hypothesis: market-data-service (which sits downstream of pricing-engine on the quote path) shows NO error/latency anomaly in 11:00-12:20Z (only an unrelated TLS WARN 11:43:53Z, zero ERRORs), and order-service's failures are all 'pricing-engine quote call timed out', not serialization/deserialization errors — so the failing downstream edge is order-service -> pricing-engine, not the serialization boundary at pricing-engine -> market-data-service.
**Assumptions**
  - The flag ramp actually enabled/expanded the heavy 20000-path Monte-Carlo path (flag→CPU-cost link is inferred from timing plus F2's GreeksV2 warnings, not from direct code inspection)
  - The flag took effect within the 12:04–12:05 bucket (sub-minute ordering flag-apply vs CPU jump unproven, both in same 480s bucket)

### H3: Shared redis-cache / exchange-gateway degradation on the shared quote path
A shared component (redis-cache, shared by pricing-engine and market-data-service; or exchange-gateway) degraded in the 12:04Z bucket, inflating pricing-engine latency and cascading to order-service quote timeouts.

**Causal chain**
1. A shared dependency (redis-cache or exchange-gateway) begins failing/latency-spiking at onset *(assumed)*
2. pricing-engine calls block on the shared dependency, raising request latency *(assumed)*
3. order-service quote calls time out [F1]

**Supporting evidence**
  - `F10` ✔ Quote path graph: order-service -> pricing-engine (http) -> market-data-service (http); pricing-engine also -> redis-cache (cache) and market-data-service -> redis-cache (cache) and -> exchange-gateway (http). redis-cache is the shared component between pricing-engine and market-data-service (both edges present in the same result).
  - `F11` ✔ order-service's own downstreams are ledger-db (db, shared), pricing-engine (http), exchange-gateway (http), kafka (queue-produce); 'shared' datastores flagged are ledger-db, redis-cache, kafka. No shared component directly between order-service and market-data-service: they connect only via pricing-engine.
**Contradicting evidence**
  - `F4` ✔ pricing-engine's latency is CPU-driven, not memory/GC-driven: cpu_pct sat at ~39-41% through 11:56Z and jumped to 91.5% (max 100.0%) at 12:04Z, holding ~98.7-99.3% for the rest of the window, while memory_pct stayed flat at ~47.7-51.0% (min 44.65, max 54.11) with no rise at onset.
  - `F8` ✔ Exactly one config/flag change in the window: cfg-3120 on pricing-engine, key flags.pricing.greeks_v2.rollout_pct, 5 -> 100, changed_at 2026-05-20T12:04:00Z, author pm-experiments ('Ramp Greeks v2 experiment to all traffic'). Note this lands 8 minutes AFTER the market-data deploy and is the closest change to the 12:16Z report.
  - `F16` ✔ The pricing-engine flag ramp cfg-3120 is the only change whose timestamp coincides exactly with the onset bucket: changed_at 12:04:00Z equals the 12:04Z bucket in which T1/F3 found pricing-engine latency_p99 jump ~59-61ms -> 2291ms and F4 found cpu_pct 39-41% -> 91.5-100%, with pricing-engine's own first WARN at 12:05:21Z ('GreeksV2Calculator: Monte-Carlo paths=20000 took ~3000-8900ms'). The flag author/description flags it as the Greeks v2 ramp, matching the calculator named in the warnings.
  - `F3` ✔ pricing-engine latency_p99_ms jumped in the SAME 12:04Z bucket as order-service: baseline ~59-61ms through 11:56Z, then 2291ms at 12:04Z, settling at ~2560-2706ms (max 2849ms) thereafter — matching the reported '~2.6s'. order-service latency_p99 likewise jumped in the 12:04Z bucket from ~149-154ms to 2149ms then ~2877ms.
  - `F25` ✔ redis-cache CPU is flat and low across the whole 11:50-13:59Z window, showing no deviation at 12:04Z onset: mean 21.98%, min 20.4%, max 24.13%; the 12:02Z bucket is 22.0% avg and 12:08Z is 22.36% avg, i.e. the onset bucket is indistinguishable from the 11:50Z baseline (22.31%).
  - `F26` ✔ redis-cache connection saturation is absent: connected_clients stayed in a narrow band with no step at onset — min 283.29, max 348.57, mean 311.89; 12:02Z avg 310.12 vs 12:08Z avg 319.2, both within the 11:50Z (314.14) / 11:56Z (311.24) baseline range.
  - `F27` ✔ redis-cache hit ratio and evictions show no degradation at onset: hit_ratio_pct held 93.83-98.46% (mean 96.05) with 12:02Z=96.39 and 12:08Z=96.02; evictions_per_min held 0.78-4.81 (mean 2.99) with 12:02Z=3.32 and 12:08Z=2.98 — both bucketed identically to the pre-onset 11:50-11:56Z values.
  - `F28` ✔ exchange-gateway latency is flat, not degraded, across the window: latency_p99_ms ranges 117.54-142.55ms with mean 130.43ms; the 12:02Z bucket is 128.87ms and 12:08Z is 133.78ms, entirely within the 11:50Z (131.94) and 11:56Z (130.49) baseline. No step at 12:04Z onset.
  - `F29` ✔ No error/critical log evidence from either shared dependency in 11:50-13:59Z: redis-cache returned exactly 1 WARN total ('deprecated TLS cipher offered by client 46e36f97' at 13:28:54Z) and exchange-gateway exactly 1 WARN ('clock drift 38ms detected, resyncing with NTP' at 12:54:21Z), both truncated:false and neither at or near the 12:04Z onset.
  - `F21` ✔ In the SAME 12:04Z bucket where pricing-engine request_rate was flat (625.14), cpu_pct jumped from a 38-41% baseline to avg 68.44 / max 98.65, then 98.67-99.56 for the rest of the window — a pure CPU-cost step with no corresponding request volume change.
  - `F23` ✔ Discriminator verdict: a flat request rate coinciding with a 40%→99% CPU jump confirms H1 (fixed code-path cost per request from the Greeks v2 rollout) and is inconsistent with H4 (organic volume / market-open capacity saturation), which requires a request-volume spike that is absent from both pricing-engine and order-service.
**Assumptions**
  - The shared dependency degraded without any recorded change
  - pricing-engine latency is dominated by the shared dependency rather than its own CPU
**Rejected because:** Shared components were positively cleared: redis-cache CPU flat 20.4–24.13% with no step at onset (F25), connected_clients 283–349 with no step (F26), hit_ratio 93.8–98.5% and evictions 0.78–4.81 bucketed identically pre/post onset (F27), and zero ERROR logs (F29). exchange-gateway latency_p99 flat 117.5–142.6ms with no step (F28) and only one unrelated clock-drift WARN (F29). Crucially, pricing-engine's latency is CPU-bound with flat memory and flat request rate (F4, F21, F23) — an I/O-blocking shared dependency would not raise its own CPU to 99% while volume is flat.

### H2: market-data-service protobuf serialization change (reporter's suspicion) broke/slowed the quote path
dep-6111 (market-data-service v5.2.0, 'Switch quote serialization to protobuf', 11:56:00Z) introduced new serialization that pricing-engine mis-handles, inflating quote computation time and cascading into order-service timeouts.

**Causal chain**
1. dep-6111 ships protobuf quote serialization at 11:56:00Z [F7]
2. pricing-engine deserialization of new format is slow/buggy, inflating quote latency *(assumed)*
3. order-service quote calls exceed 2500ms deadline [F1]

**Supporting evidence**
  - `F7` ✔ Single deployment across ALL services in 11:00–14:00Z: dep-6111 market-data-service v5.2.0 at 2026-05-20T11:56:00Z by v.rao, 'Switch quote serialization to protobuf' / 'protobuf quote payloads for internal callers', status live, rolled_back_at null.
  - `F10` ✔ Quote path graph: order-service -> pricing-engine (http) -> market-data-service (http); pricing-engine also -> redis-cache (cache) and market-data-service -> redis-cache (cache) and -> exchange-gateway (http). redis-cache is the shared component between pricing-engine and market-data-service (both edges present in the same result).
**Contradicting evidence**
  - `F6` ✔ market-data-service (the service the report blames, which 'shipped a serialization change') shows NO error/latency anomaly in logs in the 11:00-12:20Z window: only a single unrelated WARN 'deprecated TLS cipher offered by client' at 11:43:53Z, zero ERROR entries.
  - `F14` ✔ Test of reporter's suspicion: the market-data-service serialization change (dep-6111, 11:56:00Z) precedes the 12:04:00Z onset by exactly 8 minutes and does NOT align with onset — T1 established the first anomalies in the 12:04Z bucket (pricing-engine WARN 12:05:21Z, order-service first timeout 12:06:10Z). A change 8 min before onset with no market-data-service telemetry anomaly is not temporally consistent with being the trigger.
  - `F15` ✔ Contradicting evidence against the market-data-service hypothesis: market-data-service (which sits downstream of pricing-engine on the quote path) shows NO error/latency anomaly in 11:00-12:20Z (only an unrelated TLS WARN 11:43:53Z, zero ERRORs), and order-service's failures are all 'pricing-engine quote call timed out', not serialization/deserialization errors — so the failing downstream edge is order-service -> pricing-engine, not the serialization boundary at pricing-engine -> market-data-service.
  - `F1` ✔ order-service first ERROR in the 11:00-14:00Z window is at 12:06:10Z and every error carries the identical signature 'pricing-engine quote call timed out after 2500ms' — 225 such errors, all one pattern, no 5xx or serialization errors. So the failing downstream call is order-service -> pricing-engine.
  - `F2` ✔ pricing-engine's earliest in-window signal is 12:05:21Z: 342 WARN 'GreeksV2Calculator: Monte-Carlo paths=20000 took ~3000-8900ms' events, and its own first ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' at 12:06:41Z. pricing-engine's own errors are deadline-exceeded on its GreeksV2 calculator, not serialization errors.
  - `F4` ✔ pricing-engine's latency is CPU-driven, not memory/GC-driven: cpu_pct sat at ~39-41% through 11:56Z and jumped to 91.5% (max 100.0%) at 12:04Z, holding ~98.7-99.3% for the rest of the window, while memory_pct stayed flat at ~47.7-51.0% (min 44.65, max 54.11) with no rise at onset.
  - `F12` ✔ No deployment in the 2026-05-19T00:00Z–2026-05-20T11:00Z baseline window (count 0), and no order-service, ledger-db or kafka change appears in the 11:00–14:00Z results — i.e. no change attributed to order-service or its datastores.
**Assumptions**
  - Assumed pricing-engine incurs serialization cost not visible in its logs
**Rejected because:** dep-6111 precedes onset by 8 minutes with no market-data-service latency/error anomaly (F6, F15); order-service errors are timeouts, not serialization errors (F1); pricing-engine's own error is a GreeksV2 deadline exceeded, not a serialization error (F2); pricing-engine latency is CPU saturation, not I/O or serialization (F4).

### H4: Capacity saturation: market-open/volume-driven load exceeding pricing-engine provisioned capacity
pricing-engine CPU saturation reflects organic quote volume growth (market open) exceeding provisioned capacity, matching historical precedent HIST-113, with order-service timeouts as a downstream symptom. No faulty change required.

**Causal chain**
1. Quote traffic volume rises past pricing-engine provisioned CPU capacity at 12:04Z *(assumed)*
2. pricing-engine CPU saturates and latency rises to ~2.6s [F4, F3]
3. order-service quote calls exceed 2500ms deadline [F1]

**Supporting evidence**
  - `F18` ✔ Historical precedent HIST-113 (2026-01-28, pricing-engine CPU saturation at market open, symptoms 'quote timeouts','cpu 99%', root cause record market-open volume exceeding capacity) is the closest match by symptom to this incident — but it is a lead, not evidence: HIST-113 attributes the CPU saturation to market-open volume, whereas here the saturation coincides with a Greeks v2 rollout flag, not with a market-open time.
  - `F4` ✔ pricing-engine's latency is CPU-driven, not memory/GC-driven: cpu_pct sat at ~39-41% through 11:56Z and jumped to 91.5% (max 100.0%) at 12:04Z, holding ~98.7-99.3% for the rest of the window, while memory_pct stayed flat at ~47.7-51.0% (min 44.65, max 54.11) with no rise at onset.
  - `F3` ✔ pricing-engine latency_p99_ms jumped in the SAME 12:04Z bucket as order-service: baseline ~59-61ms through 11:56Z, then 2291ms at 12:04Z, settling at ~2560-2706ms (max 2849ms) thereafter — matching the reported '~2.6s'. order-service latency_p99 likewise jumped in the 12:04Z bucket from ~149-154ms to 2149ms then ~2877ms.
**Contradicting evidence**
  - `F19` ✔ pricing-engine request_rate did NOT spike at the 12:04Z onset bucket: at 120s resolution it reads 623.65 (11:56Z), 606.28 (11:58Z), 647.20 (12:00Z), 667.24 (12:02Z), 625.14 (12:04Z), 666.82 (12:06Z) req/s — i.e. the 12:04Z bucket (625.14) is BELOW the preceding 12:02Z bucket (667.24) and within the baseline noise band.
  - `F20` ✔ pricing-engine request_rate is flat across the whole 11:50–13:59Z window: 180s buckets range only 582.43–741.12 req/s, mean 646.11, with no step at 12:04Z (12:02Z avg 653.76, 12:05Z avg 652.37, 12:08Z 634.18); the post-onset level equals the pre-onset level.
  - `F21` ✔ In the SAME 12:04Z bucket where pricing-engine request_rate was flat (625.14), cpu_pct jumped from a 38-41% baseline to avg 68.44 / max 98.65, then 98.67-99.56 for the rest of the window — a pure CPU-cost step with no corresponding request volume change.
  - `F22` ✔ order-service request_rate over the same 11:50–13:59Z window is flat and shows no step at 12:04Z: the 12:02Z bucket avg 239.82 vs the 12:05Z bucket avg 231.73, and the overall range is only 216.83–256.97 req/s (mean 233.06) — no capacity ramp on the caller side either.
  - `F23` ✔ Discriminator verdict: a flat request rate coinciding with a 40%→99% CPU jump confirms H1 (fixed code-path cost per request from the Greeks v2 rollout) and is inconsistent with H4 (organic volume / market-open capacity saturation), which requires a request-volume spike that is absent from both pricing-engine and order-service.
  - `F8` ✔ Exactly one config/flag change in the window: cfg-3120 on pricing-engine, key flags.pricing.greeks_v2.rollout_pct, 5 -> 100, changed_at 2026-05-20T12:04:00Z, author pm-experiments ('Ramp Greeks v2 experiment to all traffic'). Note this lands 8 minutes AFTER the market-data deploy and is the closest change to the 12:16Z report.
  - `F16` ✔ The pricing-engine flag ramp cfg-3120 is the only change whose timestamp coincides exactly with the onset bucket: changed_at 12:04:00Z equals the 12:04Z bucket in which T1/F3 found pricing-engine latency_p99 jump ~59-61ms -> 2291ms and F4 found cpu_pct 39-41% -> 91.5-100%, with pricing-engine's own first WARN at 12:05:21Z ('GreeksV2Calculator: Monte-Carlo paths=20000 took ~3000-8900ms'). The flag author/description flags it as the Greeks v2 ramp, matching the calculator named in the warnings.
  - `F13` ✔ Ordering of the two changes vs onset: only deployment dep-6111 (market-data-service v5.2.0 'Switch quote serialization to protobuf') at 2026-05-20T11:56:00Z, and only config change cfg-3120 (pricing-engine flags.pricing.greeks_v2.rollout_pct 5->100) at 2026-05-20T12:04:00Z. Narrow re-query of 11:40-12:20Z and 11:50-12:10Z returns the same single items, both partial:false, so the set is complete for this window.
**Assumptions**
  - None retained: the required volume spike does not exist in the data
**Rejected because:** The decisive discriminator was measured and refutes it: pricing-engine request_rate is flat across 11:50–13:59Z with no step at 12:04Z (F20: 180s buckets 582–741 req/s, mean 646; the 12:04Z bucket 625.14 is BELOW the preceding 12:02Z 667.24) while CPU jumped 38–41%→~99% in the same bucket (F21). order-service request_rate is likewise flat (F22: 12:02Z 239.82 vs 12:05Z 231.73). A CPU step with no volume step is a code-path cost increase, not organic load; its only support (HIST-113 symptom match, F18) is weak precedent and HIST-113 attributes saturation to market-open volume, absent here.

## Facts (verified observations)
- All 225 order-service errors in the window carry one identical signature, 'pricing-engine quote call timed out after 2500ms', with no 5xx or serialization errors; the first is at 12:06:10Z — so the failing edge is order-service -> pricing-engine. [F1]
- pricing-engine's earliest in-window signal is 12:05:21Z: 342 WARN 'GreeksV2Calculator: Monte-Carlo paths=20000 took ~3000-8900ms', and its first ERROR at 12:06:41Z is 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' — its own errors are GreeksV2 deadline-exceeded, not serialization errors. [F2]
- pricing-engine latency_p99_ms jumped in the same 12:04Z bucket as order-service: ~59-61ms baseline through 11:56Z, 2291ms at 12:04Z, then ~2560-2706ms (max 2849ms); order-service latency_p99 jumped in the same bucket from ~149-154ms to 2149ms then ~2877ms, matching the reported ~2.6s. [F3]
- pricing-engine latency is CPU-driven, not memory/GC-driven: cpu_pct sat ~39-41% through 11:56Z, jumped to 91.5% (max 100.0%) at 12:04Z and held ~98.7-99.3% thereafter, while memory_pct stayed flat at ~47.7-51.0% with no rise at onset. [F4]
- order-service error_rate shows the same onset: flat ~0.17-0.25% through 11:56Z, then 6.08% at 12:04Z and ~8.1-8.4% thereafter (max 8.87%). [F5]
- market-data-service, the service the report blames, shows no error/latency anomaly in logs in 11:00-12:20Z: only one unrelated WARN ('deprecated TLS cipher offered by client', 11:43:53Z) and zero ERROR entries. [F6]
- Exactly one deployment occurred across all services in 11:00-14:00Z: dep-6111 market-data-service v5.2.0 at 11:56:00Z ('Switch quote serialization to protobuf'), status live, rolled_back_at null. [F7]
- Exactly one config/flag change occurred in the window: cfg-3120 on pricing-engine, key flags.pricing.greeks_v2.rollout_pct, 5 -> 100, changed_at 2026-05-20T12:04:00Z, author pm-experiments ('Ramp Greeks v2 experiment to all traffic'). [F8]
- The change list is complete for the onset window: narrow re-queries of 11:40-12:20Z and 11:50-12:10Z returned the same single deployment and single config change, both partial:false. [F13]
- pricing-engine request_rate did not spike at onset: at 120s resolution 623.65 (11:56Z), 647.20 (12:00Z), 667.24 (12:02Z), 625.14 (12:04Z), 666.82 (12:06Z) req/s — the onset bucket is below the preceding bucket and within baseline noise. [F19]
- pricing-engine request_rate is flat across 11:50-13:59Z: 180s buckets range 582.43-741.12 req/s, mean 646.11, with no step at 12:04Z; post-onset level equals pre-onset level. [F20]
- In the same 12:04Z bucket where pricing-engine request_rate was flat (625.14 req/s), cpu_pct jumped from a 38-41% baseline to avg 68.44 / max 98.65, then 98.67-99.56 for the rest of the window — a pure CPU-cost step with no request-volume change. [F21]
- order-service request_rate is likewise flat with no step at 12:04Z (12:02Z avg 239.82 vs 12:05Z avg 231.73; overall range 216.83-256.97 req/s, mean 233.06). [F22]
- On the quote path order-service -> pricing-engine (http) -> market-data-service (http), cfg-3120 sits on pricing-engine's quote/Greeks computation, while dep-6111 sits one hop further downstream and only affects the pricing-engine -> market-data-service edge. [F17]
- redis-cache CPU is flat and low across 11:50-13:59Z with no deviation at onset (mean 21.98%, min 20.4%, max 24.13%; 12:02Z 22.0% vs 12:08Z 22.36%). [F25]
- redis-cache connection saturation is absent: connected_clients stayed 283.29-348.57 (mean 311.89) with no step at onset. [F26]
- redis-cache hit ratio and evictions show no degradation at onset: hit_ratio_pct 93.83-98.46% (mean 96.05), evictions_per_min 0.78-4.81 (mean 2.99), bucketed identically to pre-onset values. [F27]
- exchange-gateway latency is flat, not degraded, across the window: latency_p99_ms 117.54-142.55ms (mean 130.43ms) with no step at 12:04Z. [F28]
- No error/critical logs from either shared dependency in 11:50-13:59Z: redis-cache had exactly 1 WARN (TLS cipher, 13:28:54Z) and exchange-gateway exactly 1 WARN (clock drift 38ms, 12:54:21Z), neither at or near onset. [F29]
- Quote-path topology: redis-cache is the shared component between pricing-engine and market-data-service, while order-service and market-data-service connect only via pricing-engine — there is no shared component directly between them. [F10, F11]
- No deployment occurred in the 2026-05-19T00:00Z-2026-05-20T11:00Z baseline window, and no order-service, ledger-db or kafka change appears in the 11:00-14:00Z results. [F12]
- redis-cache exposes no latency_p99_ms and no error-rate metric at all, so its health can only be judged from CPU/clients/hit-ratio/evictions saturation proxies. [F24]

## Assumptions (not verified)
- The flag ramp actually enabled/expanded the heavy 20000-path Monte-Carlo GreeksV2 path; the flag->CPU-cost link is inferred from timing plus the GreeksV2 warnings (F2, F16), not from direct code inspection.
- The flag took effect within the 12:04-12:05 bucket; sub-minute ordering between flag-apply and the CPU jump is unproven because both fall inside the same 480s bucket.
- exchange-gateway is treated as healthy on the basis of flat latency_p99 and a clean WARN/ERROR log scan; its error_rate/CPU metrics were never fetched (budget exhausted), so positive exclusion rests on latency plus logs rather than on its own error metric.
- redis-cache is treated as healthy on the basis of flat CPU/clients/hit-ratio/evictions because it exposes no latency or error-rate metric.
- No config change hidden on config-audit shard 2/3 (which failed on the first attempt) affected order-service, ledger-db or kafka; only one retry was possible.
- (demoted from facts: no verified observation backs it) A flat request rate coinciding with a 40%->99% CPU jump confirms H1 (fixed per-request code-path cost from the Greeks v2 rollout) and is inconsistent with organic volume / market-open capacity saturation, which requires a volume spike absent from both pricing-engine and order-service.
- (demoted from facts: no verified observation backs it) The market-data-service serialization change (11:56:00Z) precedes the 12:04:00Z onset by exactly 8 minutes and does not align with onset (first anomalies: pricing-engine WARN 12:05:21Z, order-service first timeout 12:06:10Z), and order-service's failures are timeouts rather than serialization errors.
- (demoted from facts: no verified observation backs it) Historical precedent HIST-113 (2026-01-28, pricing-engine CPU saturation at market open with 'quote timeouts'/'cpu 99%') is the closest symptom match but attributes saturation to market-open volume, whereas here the saturation coincides with a Greeks v2 rollout flag and not a market-open time.

## Timeline
- `2026-05-20T11:43:53Z` market-data-service logs its only in-window WARN, an unrelated 'deprecated TLS cipher offered by client'; no ERROR entries (F6). [F6]
- `2026-05-20T11:56:00Z` Deployment dep-6111: market-data-service v5.2.0 'Switch quote serialization to protobuf' by v.rao goes live — 8 minutes before onset and with no subsequent market-data-service anomaly (F7, F14). [F7, F14]
- `2026-05-20T12:02:00Z` Last pre-onset bucket: pricing-engine request_rate 667.24 req/s and cpu_pct 38-41% baseline; order-service latency_p99 ~149-154ms and error_rate ~0.17-0.25% (F19, F21, F3, F5). [F19, F21, F3, F5]
- `2026-05-20T12:04:00Z` Config change cfg-3120 applied on pricing-engine: flags.pricing.greeks_v2.rollout_pct 5 -> 100 ('Ramp Greeks v2 experiment to all traffic') — the only change landing exactly in the onset bucket (F8, F16). [F8, F16]
- `2026-05-20T12:04:00Z` In the same bucket: pricing-engine cpu_pct jumps 40% -> 91.5% (max 100%), latency_p99 59-61ms -> 2291ms; order-service latency_p99 149-154ms -> 2149ms and error_rate 0.25% -> 6.08%; pricing-engine request_rate is flat (625.14 req/s) and order-service request_rate flat (231.73-239.82 req/s) (F3, F4, F5, F19, F21, F22). [F3, F4, F5, F19, F21, F22]
- `2026-05-20T12:05:21Z` pricing-engine emits 342 WARNs: 'GreeksV2Calculator: Monte-Carlo paths=20000 took ~3000-8900ms' (F2). [F2]
- `2026-05-20T12:06:10Z` First order-service ERROR in the window: 'pricing-engine quote call timed out after 2500ms' — eventually 225 identical errors (F1). [F1]
- `2026-05-20T12:06:41Z` pricing-engine's own first ERROR: 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (F2). [F2]
- `2026-05-20T12:16:00Z` Incident reported by on-call SRE: order-service quote timeouts, pricing-engine p99 ~2.6s. []
- `2026-05-20T12:54:21Z` exchange-gateway logs a single unrelated WARN ('clock drift 38ms detected, resyncing with NTP'); latency_p99 stays flat ~130ms throughout (F28, F29). [F28, F29]
- `2026-05-20T13:28:54Z` redis-cache logs its only in-window WARN (TLS cipher); CPU/clients/hit-ratio/evictions remain flat and normal (F25-F27, F29). [F25, F26, F27, F29]
- `2026-05-20T13:59:00Z` End of data: pricing-engine cpu_pct continues at ~98.7-99.3% with latency ~2.6s; no rollback of cfg-3120 or dep-6111 recorded in the window (F4, F7). [F4, F7]

## Recommended actions
- **[immediate]** Roll back config change cfg-3120 on pricing-engine: set flags.pricing.greeks_v2.rollout_pct from 100 back to 5 (its pre-12:04Z value), and confirm the apply in the config audit. -- cfg-3120 is the only change coinciding exactly with the 12:04Z onset bucket and it directly gates the GreeksV2 calculator that is emitting the 20000-path Monte-Carlo warnings and the deadline-exceeded errors (F8, F16, F2, F4). This is the same mitigation under any flag->CPU mechanism, so it is safe. [F8, F16, F2, F4]
- **[immediate]** Verify recovery in the 12:04Z-bucket metrics after the flag rollback: pricing-engine cpu_pct should return to ~40% and latency_p99 to ~60ms, and order-service error_rate should return from ~8% to ~0.2% with quote timeouts stopping. -- The onset is measurable in exactly these series (F3, F4, F5), so their return to baseline is a direct, observable confirmation that the flag was the cause rather than a coincidental correlation. [F3, F4, F5]
- **[short_term]** Do not roll back dep-6111 (market-data-service v5.2.0 protobuf) as a first step; leave it live while the flag mitigation is tested. -- The serialization deploy precedes onset by 8 minutes, market-data-service shows no telemetry anomaly, and order-service errors are timeouts rather than serialization errors (F6, F7, F14, F15) — rolling it back would confound the flag experiment and is unsupported by the evidence. [F6, F7, F14, F15]
- **[short_term]** Before re-enabling the Greeks v2 ramp, size the 20000-path Monte-Carlo path: load-test pricing-engine quote traffic at 100% rollout to measure per-request CPU cost, and either reduce the path count, cache/precompute Greeks, or add capacity headroom so per-request latency stays comfortably under order-service's 2500ms deadline. -- The data shows a pure per-request CPU-cost step (request_rate flat, cpu 40%->99%; F19-F23) and the observed Monte-Carlo cost of ~3000-8900ms per calculation (F2); the fix must lower per-request cost, not add volume capacity. [F2, F19, F21, F22, F23]
- **[long_term]** Add a staged rollout guard for flags.pricing.greeks_v2.rollout_pct: ramp in small increments with automatic rollback if pricing-engine cpu_pct exceeds a threshold (e.g. 85%) or latency_p99 exceeds a fraction of the 2500ms quote deadline, and surface the flag state on the pricing-engine dashboard. -- A 5 -> 100 single-step ramp put the heavy path on all traffic within one 480s bucket (F8, F16); incremental ramp with CPU/latency gating would have caught the cost step before order-service timeouts (F1, F5). [F8, F16, F1, F5]
- **[long_term]** Instrument redis-cache with latency_p99_ms and an error-rate metric, and ensure exchange-gateway error_rate/CPU metrics are queryable, so shared-dependency hypotheses can be positively tested rather than inferred from proxy metrics. -- Redis exposes no latency or error-rate metric at all (F24), and exchange-gateway error metrics were never retrievable (budget/gap), leaving H3 exclusions resting on CPU/clients/hit-ratio/evictions proxies and flat latency (F25-F29). [F24, F25, F26, F27, F28, F29]
- **[long_term]** Add an alert on the pricing-engine quote path when cpu_pct steps abruptly (e.g. >30 percentage points within one bucket) while request_rate is flat, since that pattern indicates a code-path cost change rather than organic load and preceded order-service impact. -- The decisive discriminator was a CPU step with flat request rate (F19, F21, F23); alerting on it would have detected the regression at 12:04Z rather than at the 12:16Z report. [F19, F21, F23]

## Data gaps
- Could not enumerate all services for error-rate/latency anomalies in 11:00-14:00Z (no service-catalog/topology lookup tool); only order-service, pricing-engine and market-data-service were checked, so the blast radius may be larger than established.
- pricing-engine exposes no GC metric (only cpu_pct, error_rate, latency_p99_ms, memory_pct, request_rate, restarts), so GC pauses vs pure CPU saturation cannot be fully separated; only flat memory_pct plus CPU saturation are evidenced.
- No code inspection or deployment/config data confirms mechanically that flipping the flag to 100% is what enabled/exercised the 20000-path Monte-Carlo path; the flag->CPU-cost link is a strong inference from timing plus F2/F16.
- Config-audit shard 2/3 did not respond on the first query (partial:true) and only one retry was possible, so the change list for order-service/ledger-db/kafka cannot be guaranteed exhaustive.
- The 12:04Z bucket is 480s wide and the config change is minuted at 12:04:00Z while pricing-engine's first WARN is 12:05:21Z, so sub-minute ordering between flag-apply and the CPU jump cannot be proven.
- exchange-gateway error_rate, upstream_error_rate, cpu_pct, upstream_latency_ms, request_rate and restarts were never fetched (budget_exhausted), so its health rests on flat latency_p99 plus a clean 1-WARN log scan rather than on its error metric.
- redis-cache has no latency_p99_ms or error-rate metric instrumented (latency query returned not_found), so redis slowness cannot be measured directly.
- No rollback of cfg-3120 or dep-6111 is recorded in the window, so the mitigation state at the current time (13:59Z) is unknown.

## What would change this conclusion
- A measured request-volume spike at 12:04Z on pricing-engine or order-service (contradicting the flat request_rate in F19-F22) would point back to organic capacity/market-open saturation (H4) rather than a code-path cost increase.
- Evidence that pricing-engine CPU returned to ~40% and latency to ~60ms without rolling back cfg-3120 (e.g. a spontaneous recovery or a different change taking effect) would break the flag->CPU causal link and force a re-ranking.
- Finding an error/latency or serialization anomaly in market-data-service or on the pricing-engine -> market-data-service edge at 11:56-12:06Z would revive the protobuf serialization hypothesis (H2).
- Evidence of redis-cache or exchange-gateway degradation at 12:04Z (e.g. redis latency/error metrics or exchange-gateway error/CPU saturation, which were never fetched) would revive the shared-component hypothesis (H3).
- Logs or code showing the GreeksV2 calculator was already running at 20000 paths (or on all traffic) before 12:04Z would decouple cfg-3120 from the CPU step and undermine the top hypothesis.

## Integrity warnings (set by code)
- fact demoted to assumption: A flat request rate coinciding with a 40%->99% CPU jump confirms H1 (fixed per-r
- fact demoted to assumption: The market-data-service serialization change (11:56:00Z) precedes the 12:04:00Z 
- fact demoted to assumption: Historical precedent HIST-113 (2026-01-28, pricing-engine CPU saturation at mark

## Investigation stats
- latency 163.09s, LLM calls 21, tokens 155806 (in 126988 / out 28818)
- tool calls 32 (1 failed, 1 partial, 0 recovered by retry), findings 30/30 verified
