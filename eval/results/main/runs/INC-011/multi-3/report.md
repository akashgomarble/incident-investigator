# Incident investigation report: INC-011
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause identified at medium-to-high confidence (top hypothesis H1, confidence 0.70 per the reviewed ranking): the cfg-3120 config change ramping pricing-engine's flags.pricing.greeks_v2.rollout_pct from 5% to 100% at 2026-05-20T12:04:00Z put the expensive GreeksV2 Monte-Carlo quote path (paths=20000) on essentially all quote requests, driving pricing-engine CPU from ~40% to ~99-100% and p99 from ~60ms to ~2.5-2.9s at statistically flat traffic; order-service's 2500ms pricing-engine quote call then timed out, failing orders at the quote step. The reporter's suspicion (market-data-service dep-6111 protobuf serialization change, H2) is rejected: it landed 8 minutes before onset with a quiet interval, market-data-service shows zero errors, there is no direct order-service->market-data-service edge, and no deserialization error signature exists. Rival hypotheses (GC/resource exhaustion, redis-cache shared-dependency failure, broader blast radius) are ranked low and contradicted or untestable, though some residual uncertainty remains because no intervention/revert test was run and the config-audit shard 2/3 response was partial. The incident is still active and unresolved through 13:59Z.

## Probable root cause
**Greeks v2 full-traffic flag ramp saturates pricing-engine CPU → quote timeouts** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.70, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Greeks v2 full-traffic flag ramp saturates pricing-engine CPU → quote timeouts | config_change | pricing-engine | cfg-3120 | 0.70 (medium) | active |
| 2 | H5: Broader blast radius: multiple pricing-engine clients degraded (shared-component framing) | capacity_saturation | pricing-engine | null | 0.15 (low) | active |
| 3 | H4: Resource exhaustion / GC thrash in pricing-engine independent of any change | resource_exhaustion | pricing-engine | null | 0.10 (low) | active |
| 4 | H3: Shared redis-cache dependency failure on both hops of the quote path | dependency_failure | redis-cache | null | 0.07 (low) | active |
| 5 | H2: Reporter's suspicion: market-data-service protobuf serialization change broke quote responses | bad_deployment | market-data-service | dep-6111 | 0.03 (low) | rejected |

### H1: Greeks v2 full-traffic flag ramp saturates pricing-engine CPU → quote timeouts
The cfg-3120 config change ramping pricing-engine's flags.pricing.greeks_v2.rollout_pct from 5% to 100% at 12:04:00Z put the expensive GreeksV2 Monte-Carlo calculator (paths=20000) on essentially all quote requests. This drove pricing-engine CPU from ~40% to 99-100% and p99 from ~60ms to ~2.5-2.9s at constant traffic, causing order-service's 2500ms pricing-engine quote call to time out and orders to fail at the quote step.

**Causal chain**
1. cfg-3120 ramps flags.pricing.greeks_v2.rollout_pct 5 -> 100 on pricing-engine at 12:04:00Z; it is the ONLY pricing-engine config change between 2026-05-01 and 12:04Z [F2, F15, F27]
2. The config-applied log lands at 12:04:16Z and the GreeksV2 path is essentially absent from the log stream pre-ramp, then saturates it post-ramp [F35]
3. GreeksV2 Monte-Carlo computation (paths=20000) is the slow component, taking 6465ms+ and exceeding the 2000ms quote budget [F12, F18]
4. CPU steps ~40% -> 99-100% in the 12:05 bucket at statistically identical request_rate (mean ~641 vs ~641 req/s), so the step is not traffic-driven [F23, F33, F34]
5. p99 latency tracks the flagged-path share: flat ~57-63ms through the 12:04 bucket, then jumps to ~2.4-2.8s from the 12:05 bucket at a ~1.0x request_rate ratio [F36, F8, F9]
6. error_rate steps ~0.15-0.29% -> ~13% with no preceding gradual rise, matching the CPU step; failure is compute-induced not load-induced [F24, F11]
7. Memory is flat (46-53%) across the transition and restarts remain 0, so the CPU step is not GC/heap pressure or a restart [F22, F37, F25]
8. pricing-engine's own errors are deadline-exceeded on the GreeksV2 calculator (2000ms budget) [F12]
9. order-service's only error signature is a 2500ms timeout on its pricing-engine quote call, first at 12:06:10Z, ~2 min after the pricing-engine latency jump [F7, F14]
10. Orders fail at the quote step, surfacing as INC-011 [F7]

**Supporting evidence**
  - `F2` ✔ Config change cfg-3120 on pricing-engine: key 'flags.pricing.greeks_v2.rollout_pct' changed 5 -> 100 by pm-experiments at 2026-05-20T12:04:00Z (full-traffic ramp). No config change in the window touches market-data-service or order-service.
  - `F15` ✔ The full-traffic ramp of pricing-engine's Greeks v2 experiment (cfg-3120, flags.pricing.greeks_v2.rollout_pct 5->100 by pm-experiments) is timestamped 2026-05-20T12:04:00Z — the same minute bucket in which pricing-engine p99 jumped from ~60ms to 1297ms avg (F8) and CPU began pinning at 99-100% (F10), and 2 minutes before order-service's first quote timeout at 12:06:10Z. This is the only change whose timestamp coincides with the measured onset.
  - `F27` ✔ pricing-engine's ONLY config change between 2026-05-01 and 2026-05-20T12:04:00Z is cfg-3120: 'flags.pricing.greeks_v2.rollout_pct' old_value 5 -> new_value 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'). No earlier partial ramp of that flag (or any other pricing-engine flag) appears in the history.
  - `F35` ✔ GreeksV2 path is essentially absent from the log stream in the pre-ramp window and saturates it post-ramp: the earliest Greeks/config match is the INFO 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100' at 12:04:16Z, then 30 WARN 'GreeksV2Calculator: Monte-Carlo paths=20000 took <n>ms' (first 12:05:21Z, e.g. 6465ms, 8361ms, 8942ms) and 21 ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (first 12:06:41Z). No GreeksV2 WARN/ERROR appears before 12:04Z — consistent with a 5% flag producing few/no logged slow-path hits pre-ramp.
  - `F12` ✔ pricing-engine's earliest internal signal is a WARN at 2026-05-20T12:05:21Z ('GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms'), followed by its own ERROR signature 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' first at 12:06:41Z. The Monte-Carlo computation is the slow component.
  - `F18` ✔ Greeks v2 flag scope matches the observed mechanism: the flag ramped is pricing-engine's own Greeks calculator, and pricing-engine's earliest and only internal signatures are GreeksV2Calculator Monte-Carlo warnings/errors ('GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms' at 12:05:21Z, 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' at 12:06:41Z). A 5%->100% ramp of that calculator plausibly explains the CPU pin (F10) and flat request_rate (F11: ~627-660 req/s).
  - `F23` ✔ pricing-engine cpu_pct shows a sharp step at the 12:05 bucket: stable ~37-44% through the 12:04 bucket (12:00 41.45, 12:04 38.22), then 98.65 at 12:05, 99.25 at 12:06, 100.0 at 12:08 and pinned 98-100% every minute thereafter through 12:15. The CPU pin begins one bucket AFTER the 12:04Z config ramp and coincides with the error_rate step.
  - `F33` ✔ pricing-engine CPU steps from ~40% to ~99% between the two comparable-traffic windows: pre-ramp 11:50–12:03Z buckets avg 38.03–41.86% (mean ~39.97%), post-ramp 12:05–12:15Z buckets avg 98.09–100.0% (mean ~99.17%). The 12:04 bucket is a transition bucket still at 38.22%, so the true step is in the 12:05 bucket.
  - `F34` ✔ request_rate is statistically identical in the two windows: pre-ramp 11:50–12:03Z avg 590.66–682.85 req/s (mean 641.22), post-ramp 12:05–12:15Z avg 599.28–687.48 req/s — i.e. CPU rises ~2.5x at constant traffic, so the step is not traffic-driven.
  - `F36` ✔ Latency tracks the flagged-path share, not traffic: p99 stays flat ~56.8–63.1ms across the entire pre-ramp window (5% flag) including the 12:04 bucket (60.53ms), then jumps to 2534.9ms in the 12:05 bucket and 2410–2849ms thereafter — a ~42x jump at a ~1.0x request_rate ratio.
  - `F8` ✔ pricing-engine p99 latency was stable ~57-61ms from 11:50 through the 12:02 bucket, then jumped in the 12:04 bucket (avg 1297.72ms, max 2534.9ms) and reached ~2849ms max by 12:08 — so the latency onset is ~12:04Z, about 2 minutes before the first order-service timeout.
  - `F9` ✔ pricing-engine p99 stayed elevated for the whole window (12:04Z through the 13:56 bucket avg 2580.86ms), i.e. the latency did not recover through 13:59Z.
  - `F24` ✔ pricing-engine error_rate steps from baseline ~0.1-0.3% (0.18 at 11:40, 0.29 at 12:04, 0.15 at 12:05) to 12.98% in the 12:06 bucket and stays ~11.9-13.6% through 12:15 — a ~50x jump with no preceding gradual rise, matching the CPU step.
  - `F11` ✔ pricing-engine error_rate rose from ~0.2% (through 11:56) to 9.75% in the 12:04 bucket and ~12.5% from 12:12 onward, while request_rate stayed flat (~627-660 req/s) — so the failure is not traffic-driven.
  - `F22` ✔ pricing-engine memory_pct is FLAT and steady across the whole window: it never exceeds 52.78% and stays in a 46.04–52.78% band with no step change at the 12:04Z ramp — 12:03Z 48.26, 12:04Z 49.49, 12:05Z 52.10, 12:06Z 50.34, 12:08Z 48.94, 12:15Z 50.25. There is no heap/memory pressure accompanying the CPU pin. This argues against resource exhaustion / heap-driven GC thrash (H4).
  - `F37` ✔ Memory is flat across the transition (mean 49.12%, range 46.04–52.78%, pre-ramp ~47–52% and post-ramp ~46–52%), so the CPU step is not accompanied by a memory/GC-pressure signature.
  - `F25` ✔ pricing-engine restarts (cumulative count) is flat 0.0 for the entire 11:40-12:13 window (every bucket 11:40 through 12:13 avg/max 0.0) — there is NO restart/uptime discontinuity at or before the 12:04Z ramp, ruling out a restart as the mechanism for the CPU pin and latency jump.
  - `F7` ✔ order-service quote-timeout errors (225 total) all share a single signature — 'pricing-engine quote call timed out after 2500ms' — with the earliest at 2026-05-20T12:06:10Z and continuing to 13:58:24Z. There are no connection-refused or deserialization signatures in the error set.
  - `F14` ✔ Inference: the order-service symptom is a downstream shadow of pricing-engine latency — pricing-engine p99/CPU/error rate degrade ~2 min BEFORE order-service's first quote timeout, order-service's only signature is a 2500ms timeout on a pricing-engine call, and no other error type appears. This is consistent with pricing-engine being the origin and order-service merely timing out on it, not a distinct fault in order-service.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The latency/CPU step at 12:04-12:05 is caused by the flag ramp rather than a coincident unlogged event; sub-minute onset within the 12:04 bucket cannot be resolved (config-applied log 12:04:16Z, first clear degraded bucket 12:05).
  - The ~2.5x CPU step for a ~20x increase in flagged share is directionally consistent with the flagged Monte-Carlo path dominating CPU only when applied to most requests (F38 inference), but no per-flag request-count metric exists to measure the flagged share directly.
  - No pricing-engine GC metric or GC log line exists, so GC thrash is excluded only indirectly via flat memory_pct (F22/F37), not by direct GC telemetry.

### H5: Broader blast radius: multiple pricing-engine clients degraded (shared-component framing)
The fault is a shared-component failure of pricing-engine affecting all its consumers, with order-service merely the first/most visible symptom at the quote step; other pricing-engine clients would show the same degradation.

**Causal chain**
1. pricing-engine is on the quote path (order-service -> pricing-engine -> market-data-service) [F4]
2. pricing-engine degrades internally (p99 ~2.6-2.9s, CPU 99%, error_rate ~12-13%) [F8, F10, F11]
3. All pricing-engine consumers experience the same latency, not just order-service *(assumed)*

**Supporting evidence**
  - `F4` ✔ order-service's quote path is a 2-hop service chain with no direct edge to market-data-service: edges include order-service -> pricing-engine (kind http) and pricing-engine -> market-data-service (kind http); order-service also calls ledger-db (db), exchange-gateway (http) and kafka (queue-produce).
  - `F8` ✔ pricing-engine p99 latency was stable ~57-61ms from 11:50 through the 12:02 bucket, then jumped in the 12:04 bucket (avg 1297.72ms, max 2534.9ms) and reached ~2849ms max by 12:08 — so the latency onset is ~12:04Z, about 2 minutes before the first order-service timeout.
  - `F10` ✔ pricing-engine CPU went from ~40% (11:40-11:54) to 99-100% starting in the 12:01 bucket, and stayed pinned at ~99% through 13:53Z, indicating CPU saturation coincident with the latency jump.
  - `F11` ✔ pricing-engine error_rate rose from ~0.2% (through 11:56) to 9.75% in the 12:04 bucket and ~12.5% from 12:12 onward, while request_rate stayed flat (~627-660 req/s) — so the failure is not traffic-driven.
  - `F28` ✔ pricing-engine's upstream/caller set has exactly ONE caller: order-service (edge order-service -> pricing-engine, kind http). api-gateway appears only as api-gateway -> order-service, i.e. upstream of the caller, not a direct pricing-engine client. No other service calls pricing-engine.
  - `F29` ✔ The both-direction, depth-3 dependency graph around pricing-engine confirms the single caller and lists its downstream shared components: pricing-engine -> market-data-service (http), pricing-engine -> redis-cache (cache), market-data-service -> redis-cache (cache), market-data-service -> exchange-gateway -> exchange-api (external, has_telemetry false). redis-cache is the only 'shared': true node.
**Contradicting evidence**
  - `F30` ✔ H5 (broader blast radius: multiple pricing-engine clients degraded) cannot be supported or tested via other callers: the service graph gives pricing-engine a single caller (order-service), so there is no second client whose latency/error/timeout trend could be pulled for 11:50-12:15Z.
  - `F11` ✔ pricing-engine error_rate rose from ~0.2% (through 11:56) to 9.75% in the 12:04 bucket and ~12.5% from 12:12 onward, while request_rate stayed flat (~627-660 req/s) — so the failure is not traffic-driven.
**Assumptions**
  - pricing-engine has exactly ONE caller (order-service) in the service graph, so there is no second client whose telemetry could be pulled; the broader-blast-radius hop is untestable and modeled as an assumption.

### H4: Resource exhaustion / GC thrash in pricing-engine independent of any change
pricing-engine reached CPU saturation (~99-100%) through resource exhaustion (GC thrash / memory pressure / thread-pool saturation) unrelated to the flag ramp or the market-data change, producing latency inflation and upstream quote timeouts.

**Causal chain**
1. pricing-engine CPU pins at 99-100% from the 12:05 bucket onward [F10, F23, F33]
2. CPU exhaustion inflates latency and produces request deadline-exceeded errors [F8, F12]
3. order-service quote calls time out at 2500ms [F7]

**Supporting evidence**
  - `F10` ✔ pricing-engine CPU went from ~40% (11:40-11:54) to 99-100% starting in the 12:01 bucket, and stayed pinned at ~99% through 13:53Z, indicating CPU saturation coincident with the latency jump.
  - `F23` ✔ pricing-engine cpu_pct shows a sharp step at the 12:05 bucket: stable ~37-44% through the 12:04 bucket (12:00 41.45, 12:04 38.22), then 98.65 at 12:05, 99.25 at 12:06, 100.0 at 12:08 and pinned 98-100% every minute thereafter through 12:15. The CPU pin begins one bucket AFTER the 12:04Z config ramp and coincides with the error_rate step.
  - `F8` ✔ pricing-engine p99 latency was stable ~57-61ms from 11:50 through the 12:02 bucket, then jumped in the 12:04 bucket (avg 1297.72ms, max 2534.9ms) and reached ~2849ms max by 12:08 — so the latency onset is ~12:04Z, about 2 minutes before the first order-service timeout.
  - `F9` ✔ pricing-engine p99 stayed elevated for the whole window (12:04Z through the 13:56 bucket avg 2580.86ms), i.e. the latency did not recover through 13:59Z.
**Contradicting evidence**
  - `F15` ✔ The full-traffic ramp of pricing-engine's Greeks v2 experiment (cfg-3120, flags.pricing.greeks_v2.rollout_pct 5->100 by pm-experiments) is timestamped 2026-05-20T12:04:00Z — the same minute bucket in which pricing-engine p99 jumped from ~60ms to 1297ms avg (F8) and CPU began pinning at 99-100% (F10), and 2 minutes before order-service's first quote timeout at 12:06:10Z. This is the only change whose timestamp coincides with the measured onset.
  - `F27` ✔ pricing-engine's ONLY config change between 2026-05-01 and 2026-05-20T12:04:00Z is cfg-3120: 'flags.pricing.greeks_v2.rollout_pct' old_value 5 -> new_value 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'). No earlier partial ramp of that flag (or any other pricing-engine flag) appears in the history.
  - `F11` ✔ pricing-engine error_rate rose from ~0.2% (through 11:56) to 9.75% in the 12:04 bucket and ~12.5% from 12:12 onward, while request_rate stayed flat (~627-660 req/s) — so the failure is not traffic-driven.
  - `F12` ✔ pricing-engine's earliest internal signal is a WARN at 2026-05-20T12:05:21Z ('GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms'), followed by its own ERROR signature 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' first at 12:06:41Z. The Monte-Carlo computation is the slow component.
  - `F18` ✔ Greeks v2 flag scope matches the observed mechanism: the flag ramped is pricing-engine's own Greeks calculator, and pricing-engine's earliest and only internal signatures are GreeksV2Calculator Monte-Carlo warnings/errors ('GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms' at 12:05:21Z, 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' at 12:06:41Z). A 5%->100% ramp of that calculator plausibly explains the CPU pin (F10) and flat request_rate (F11: ~627-660 req/s).
  - `F22` ✔ pricing-engine memory_pct is FLAT and steady across the whole window: it never exceeds 52.78% and stays in a 46.04–52.78% band with no step change at the 12:04Z ramp — 12:03Z 48.26, 12:04Z 49.49, 12:05Z 52.10, 12:06Z 50.34, 12:08Z 48.94, 12:15Z 50.25. There is no heap/memory pressure accompanying the CPU pin. This argues against resource exhaustion / heap-driven GC thrash (H4).
  - `F37` ✔ Memory is flat across the transition (mean 49.12%, range 46.04–52.78%, pre-ramp ~47–52% and post-ramp ~46–52%), so the CPU step is not accompanied by a memory/GC-pressure signature.
  - `F25` ✔ pricing-engine restarts (cumulative count) is flat 0.0 for the entire 11:40-12:13 window (every bucket 11:40 through 12:13 avg/max 0.0) — there is NO restart/uptime discontinuity at or before the 12:04Z ramp, ruling out a restart as the mechanism for the CPU pin and latency jump.
  - `F34` ✔ request_rate is statistically identical in the two windows: pre-ramp 11:50–12:03Z avg 590.66–682.85 req/s (mean 641.22), post-ramp 12:05–12:15Z avg 599.28–687.48 req/s — i.e. CPU rises ~2.5x at constant traffic, so the step is not traffic-driven.
  - `F33` ✔ pricing-engine CPU steps from ~40% to ~99% between the two comparable-traffic windows: pre-ramp 11:50–12:03Z buckets avg 38.03–41.86% (mean ~39.97%), post-ramp 12:05–12:15Z buckets avg 98.09–100.0% (mean ~99.17%). The 12:04 bucket is a transition bucket still at 38.22%, so the true step is in the 12:05 bucket.
  - `F36` ✔ Latency tracks the flagged-path share, not traffic: p99 stays flat ~56.8–63.1ms across the entire pre-ramp window (5% flag) including the 12:04 bucket (60.53ms), then jumps to 2534.9ms in the 12:05 bucket and 2410–2849ms thereafter — a ~42x jump at a ~1.0x request_rate ratio.
**Assumptions**
  - No GC-pause/resolution telemetry exists (F21, F26), so GC thrash cannot be directly measured or fully excluded.
  - A resource-exhaustion driver would need to arise at exactly the 12:04-12:05 minute with no accompanying change, which is asserted but unevidenced.

### H3: Shared redis-cache dependency failure on both hops of the quote path
redis-cache is shared by BOTH pricing-engine and market-data-service on the quote path; an eviction/cache-miss storm or redis-side degradation slowed one or both hops, driving pricing-engine latency and downstream quote timeouts independent of the serialization change and the flag ramp.

**Causal chain**
1. redis-cache is shared: true on both pricing-engine -> redis-cache and market-data-service -> redis-cache edges (the only shared node on the path) [F5, F29]
2. A cache-side event (eviction/miss storm) reduces hit rate, increasing pricing-engine compute/latency *(assumed)*
3. pricing-engine latency rises, CPU pins, order-service quote call times out [F8, F10, F7]

**Supporting evidence**
  - `F5` ✔ redis-cache is a 'shared': true component appearing on multiple critical paths: pricing-engine -> redis-cache (cache) AND market-data-service -> redis-cache (cache), so both hops of the order-service quote path depend on the same cache; ledger-db (shared, postgres), kafka (shared) and exchange-api (external, has_telemetry false) are the other shared/external nodes in the graph.
  - `F6` ✔ Closest historical precedents (leads only, not evidence): HIST-113 'pricing-engine CPU saturation at market open' with symptoms 'quote timeouts','cpu 99%' (category capacity_saturation, 2026-01-28) and HIST-116 'HTTP/2 resets after gateway envoy upgrade' (category bad_deployment, 2025-12-18) — matching the current 'quote timeouts' symptom and a change-induced failure; HIST-107 links redis-cache + market-data-service to 'cache miss storm / quote latency'.
  - `F29` ✔ The both-direction, depth-3 dependency graph around pricing-engine confirms the single caller and lists its downstream shared components: pricing-engine -> market-data-service (http), pricing-engine -> redis-cache (cache), market-data-service -> redis-cache (cache), market-data-service -> exchange-gateway -> exchange-api (external, has_telemetry false). redis-cache is the only 'shared': true node.
**Contradicting evidence**
  - `F20` ✔ redis-cache is a shared cache used by BOTH hops pricing-engine -> redis-cache and market-data-service -> redis-cache, so a cache-side change/eviction on either hop could in principle explain onset; however no config or deployment change in 10:00-13:59Z touches redis-cache, and there is no redis telemetry evidence in the retrieved change timeline. Retained as an unexcluded shared-dependency alternative, not a supported trigger.
  - `F15` ✔ The full-traffic ramp of pricing-engine's Greeks v2 experiment (cfg-3120, flags.pricing.greeks_v2.rollout_pct 5->100 by pm-experiments) is timestamped 2026-05-20T12:04:00Z — the same minute bucket in which pricing-engine p99 jumped from ~60ms to 1297ms avg (F8) and CPU began pinning at 99-100% (F10), and 2 minutes before order-service's first quote timeout at 12:06:10Z. This is the only change whose timestamp coincides with the measured onset.
  - `F27` ✔ pricing-engine's ONLY config change between 2026-05-01 and 2026-05-20T12:04:00Z is cfg-3120: 'flags.pricing.greeks_v2.rollout_pct' old_value 5 -> new_value 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'). No earlier partial ramp of that flag (or any other pricing-engine flag) appears in the history.
  - `F12` ✔ pricing-engine's earliest internal signal is a WARN at 2026-05-20T12:05:21Z ('GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms'), followed by its own ERROR signature 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' first at 12:06:41Z. The Monte-Carlo computation is the slow component.
  - `F18` ✔ Greeks v2 flag scope matches the observed mechanism: the flag ramped is pricing-engine's own Greeks calculator, and pricing-engine's earliest and only internal signatures are GreeksV2Calculator Monte-Carlo warnings/errors ('GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms' at 12:05:21Z, 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' at 12:06:41Z). A 5%->100% ramp of that calculator plausibly explains the CPU pin (F10) and flat request_rate (F11: ~627-660 req/s).
  - `F22` ✔ pricing-engine memory_pct is FLAT and steady across the whole window: it never exceeds 52.78% and stays in a 46.04–52.78% band with no step change at the 12:04Z ramp — 12:03Z 48.26, 12:04Z 49.49, 12:05Z 52.10, 12:06Z 50.34, 12:08Z 48.94, 12:15Z 50.25. There is no heap/memory pressure accompanying the CPU pin. This argues against resource exhaustion / heap-driven GC thrash (H4).
  - `F37` ✔ Memory is flat across the transition (mean 49.12%, range 46.04–52.78%, pre-ramp ~47–52% and post-ramp ~46–52%), so the CPU step is not accompanied by a memory/GC-pressure signature.
**Assumptions**
  - A redis-cache event occurred with no accompanying change record; no redis telemetry was pulled (gap) so this is unverified.
  - A cache-miss storm would manifest specifically as the GreeksV2 Monte-Carlo CPU cost observed, which is not established (F12/F18 tie the cost to the flag-gated calculator, not the cache).

### H2: Reporter's suspicion: market-data-service protobuf serialization change broke quote responses
dep-6111 (market-data-service v5.2.0, 'Switch quote serialization to protobuf') at 11:56:00Z changed payload/format handling on the pricing-engine->market-data edge, degrading or breaking quote responses consumed via pricing-engine and causing order-service quote timeouts.

**Causal chain**
1. dep-6111 switches market-data-service quote serialization to protobuf at 11:56:00Z (status live, not rolled back) [F1]
2. The format change degrades responses on the pricing-engine->market-data edge, slowing pricing-engine quote computation *(assumed)*
3. pricing-engine p99 rises and order-service's 2500ms call times out [F8, F7]

**Supporting evidence**
  - `F1` ✔ Across all services, the only deployment in 2026-05-20T10:00Z-13:59Z window is dep-6111: market-data-service v5.2.0, 'Switch quote serialization to protobuf' by v.rao, deployed_at 2026-05-20T11:56:00Z, status 'live', rolled_back_at null — ~20 min before the 12:16Z report.
**Contradicting evidence**
  - `F13` ✔ market-data-service — the service the report blames for a serialization change — shows no error/degradation signal in the window: only 1 WARN at 11:43:53Z (a deprecated TLS cipher notice), zero ERRORs.
  - `F16` ✘ unverified The blamed market-data-service deployment dep-6111 (v5.2.0, 'Switch quote serialization to protobuf') landed at 2026-05-20T11:56:00Z, 8 minutes BEFORE the 12:04Z pricing-engine onset and 10 minutes before the first order-service timeout at 12:06:10Z, with no pricing-engine degradation signal in those 8 minutes; it is status 'live', rolled_back_at null. market-data-service itself also shows no error/degradation in the window (F13: 1 WARN deprecated TLS cipher at 11:43:53Z, zero ERRORs).
  - `F17` ✔ The failing call path does not give order-service any path through the changed serializer: order-service -> pricing-engine (http) -> market-data-service (http). The market-data deployment changes a payload format on the pricing-engine->market-data edge, one hop below pricing-engine, while the measured fault (Monte-Carlo GreeksV2 computation, 6465ms, deadline exceeded 2000ms) is inside pricing-engine itself — so a serialization/deserialization error at the order-service boundary is not the mechanism.
  - `F8` ✔ pricing-engine p99 latency was stable ~57-61ms from 11:50 through the 12:02 bucket, then jumped in the 12:04 bucket (avg 1297.72ms, max 2534.9ms) and reached ~2849ms max by 12:08 — so the latency onset is ~12:04Z, about 2 minutes before the first order-service timeout.
  - `F15` ✔ The full-traffic ramp of pricing-engine's Greeks v2 experiment (cfg-3120, flags.pricing.greeks_v2.rollout_pct 5->100 by pm-experiments) is timestamped 2026-05-20T12:04:00Z — the same minute bucket in which pricing-engine p99 jumped from ~60ms to 1297ms avg (F8) and CPU began pinning at 99-100% (F10), and 2 minutes before order-service's first quote timeout at 12:06:10Z. This is the only change whose timestamp coincides with the measured onset.
  - `F7` ✔ order-service quote-timeout errors (225 total) all share a single signature — 'pricing-engine quote call timed out after 2500ms' — with the earliest at 2026-05-20T12:06:10Z and continuing to 13:58:24Z. There are no connection-refused or deserialization signatures in the error set.
  - `F18` ✔ Greeks v2 flag scope matches the observed mechanism: the flag ramped is pricing-engine's own Greeks calculator, and pricing-engine's earliest and only internal signatures are GreeksV2Calculator Monte-Carlo warnings/errors ('GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms' at 12:05:21Z, 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' at 12:06:41Z). A 5%->100% ramp of that calculator plausibly explains the CPU pin (F10) and flat request_rate (F11: ~627-660 req/s).
**Assumptions**
  - That a serialization change could induce latency (rather than error) on a hop where no errors or deserialization signatures are observed.
  - That the change is the dominant factor despite an 8-minute quiet period with no degradation signal.
**Rejected because:** Ruled out on all four critic tests: (a) timing — dep-6111 landed 11:56Z but pricing-engine degradation begins 12:04Z (8-minute quiet gap, F16), while cfg-3120 is timestamped exactly in the onset minute (F15); (b) mechanism — the only error signature is a 2500ms timeout with zero deserialization/connection-refused errors (F7), no direct edge from order-service to market-data-service (F17), and the measured slow component is pricing-engine's internal flag-gated GreeksV2 Monte-Carlo computation (F12, F18); (c) scope — market-data-service itself shows zero ERRORs and no degradation in the window (F13).

## Facts (verified observations)
- Config change cfg-3120 on pricing-engine ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z by pm-experiments; no config change in the window touched market-data-service or order-service. [F2]
- cfg-3120 is the ONLY pricing-engine config change between 2026-05-01 and 2026-05-20T12:04:00Z, and the window-wide config query for 11:50-13:59Z returns only cfg-3120. [F27, F31]
- pricing-engine p99 latency was stable ~57-61ms through the 12:02 bucket, jumped in the 12:04 bucket (avg 1297.72ms, max 2534.9ms) and reached ~2849ms max by 12:08; the onset coincides with the cfg-3120 timestamp. [F8, F15]
- pricing-engine CPU stepped from ~38-42% pre-ramp (11:50-12:03Z, mean ~39.97%) to 98-100% post-ramp (12:05-12:15Z, mean ~99.17%), pinned through 13:53Z. [F10, F23, F33]
- request_rate was statistically identical across the transition (pre-ramp mean 641.22 req/s, post-ramp comparable), so the CPU and latency step is not traffic-driven. [F11, F34]
- pricing-engine error_rate stepped from baseline ~0.1-0.3% to ~12-13% at the same time as the CPU step, with no preceding gradual rise. [F11, F24]
- pricing-engine's earliest and only internal signatures are GreeksV2Calculator Monte-Carlo warnings/errors: WARN 'Monte-Carlo paths=20000 took 6465ms' at 12:05:21Z and ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' from 12:06:41Z; the Monte-Carlo computation is the slow component. [F12]
- The GreeksV2 path is essentially absent from the log stream pre-ramp and saturates it post-ramp: no GreeksV2 WARN/ERROR appears before 12:04Z, then 30 WARNs and 21 ERRORs follow. [F35]
- pricing-engine latency tracks flagged-path share, not traffic: p99 flat ~56.8-63.1ms across the entire 5%-flag window, then ~2410-2849ms after the ramp, a ~42x jump at ~1.0x request_rate ratio. [F36]
- order-service's 225 quote-timeout errors all share a single signature, 'pricing-engine quote call timed out after 2500ms', first at 2026-05-20T12:06:10Z and continuing through 13:58:24Z; there are no connection-refused or deserialization signatures. [F7]
- The order-service quote path is a 2-hop chain with no direct edge to market-data-service: order-service -> pricing-engine (http) -> market-data-service (http). [F4]
- pricing-engine has exactly ONE caller, order-service, so order-service is the only client that can be used to test blast radius; api-gateway sits upstream of order-service, not directly in front of pricing-engine. [F28, F29]
- The blamed deployment dep-6111 (market-data-service v5.2.0, 'Switch quote serialization to protobuf') landed at 2026-05-20T11:56:00Z, 8 minutes before the 12:04Z pricing-engine onset, with no degradation signal in those 8 minutes. [F1]
- market-data-service shows no error/degradation signal in the window: only 1 WARN at 11:43:53Z (deprecated TLS cipher), zero ERRORs. [F13]
- pricing-engine memory_pct is flat across the window (never above 52.78%, band 46.04-52.78%), with no step change at the 12:04Z ramp and no memory pressure accompanying the CPU pin. [F22, F37]
- pricing-engine restarts (cumulative) is flat 0.0 for the entire 11:40-12:13Z window, ruling out a restart as the mechanism for the CPU pin and latency jump. [F25]
- pricing-engine exposes only cpu_pct, error_rate, latency_p99_ms, memory_pct, request_rate and restarts; there are no GC pause/frequency, heap or thread-pool saturation metrics, and no GC log lines exist in 11:40-12:15Z. [F21, F26]
- The competing change cfg-3118 (api-gateway ratelimit.burst 200->250, 10:30:00Z) sits 1h34m before onset and changes an admission threshold, not compute cost; no deployment or config change in the window touches order-service. [F19]
- redis-cache is a shared cache used by both hops (pricing-engine -> redis-cache and market-data-service -> redis-cache), with no config or deployment change touching it in 10:00-13:59Z. [F5]
- pricing-engine latency remained elevated through the 13:56 bucket (avg 2580.86ms) and order-service timeouts continued to 13:58:24Z, i.e. the incident had not recovered by the latest data at 13:59Z. [F9, F7]

## Assumptions (not verified)
- Assumption: the latency/CPU step at 12:04-12:05 is caused by the flag ramp rather than a coincident unlogged event; sub-minute onset within the 12:04 bucket cannot be resolved (config-applied log 12:04:16Z, first clear degraded bucket 12:05).
- Assumption: the ~2.5x CPU step for a ~20x increase in flagged share is directionally consistent with the flagged Monte-Carlo path dominating CPU only when applied to most requests; no per-flag request-count metric exists to measure the flagged share directly.
- Assumption: GC thrash is excluded only indirectly via flat memory_pct, restarts=0 and absent GC telemetry, not by direct GC telemetry.
- Assumption: pricing-engine has exactly one caller (order-service) in the service graph, so the broader-blast-radius hypothesis (H5) is untestable and modeled as an assumption.
- Assumption: the relative ranking and confidences (H1 0.70, H5 0.15, H4 0.10, H3 0.07, H2 0.03) are those of the reviewed analysis and are not re-derived here.
- Assumption: no unobserved config/deployment change hidden in the partial config-audit response meaningfully supersedes cfg-3120, since cfg-3120 targets the exact observed slow path inside the degrading service.
- (demoted from facts: no verified observation backs it) pricing-engine p99/CPU/error-rate degrade ~2 minutes before order-service's first quote timeout, and order-service's only error signature is a timeout on the pricing-engine call, consistent with order-service being a downstream symptom rather than a distinct fault.

## Timeline
- `2026-05-20T10:30:00Z` Config change cfg-3118 on api-gateway: ratelimit.burst 200 -> 250 (upstream of order-service; later ruled out as the trigger). [F3, F19]
- `2026-05-20T11:43:53Z` market-data-service emits its only log line in the window: a WARN about a deprecated TLS cipher (zero ERRORs overall). [F13]
- `2026-05-20T11:50:00Z` Pre-ramp baseline window begins: pricing-engine p99 flat ~57-61ms and CPU ~38-42% at stable request_rate (~641 req/s). [F8, F33, F34]
- `2026-05-20T11:56:00Z` Deployment dep-6111 lands: market-data-service v5.2.0, 'Switch quote serialization to protobuf' (8 minutes of quiet follow with no pricing-engine degradation). [F1, F16]
- `2026-05-20T12:04:00Z` Config change cfg-3120 on pricing-engine ramps flags.pricing.greeks_v2.rollout_pct 5 -> 100; the 12:04 bucket shows p99 avg 1297.72ms / max 2534.9ms and CPU still transitional at 38.22%. [F2, F15, F8, F23]
- `2026-05-20T12:04:16Z` Log line: 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100'. [F35]
- `2026-05-20T12:05:00Z` Clear degradation bucket: pricing-engine CPU steps to ~98.65-100% and p99 jumps to ~2534.9ms at flat request_rate; memory stays flat. [F23, F33, F36, F22]
- `2026-05-20T12:05:21Z` First pricing-engine internal warning: 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms'. [F12, F35]
- `2026-05-20T12:06:10Z` First order-service quote timeout: 'pricing-engine quote call timed out after 2500ms'. [F7]
- `2026-05-20T12:06:41Z` First pricing-engine ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2'; error_rate steps to ~12.98% in the 12:06 bucket. [F12, F24]
- `2026-05-20T12:16:00Z` Incident INC-011 reported by on-call SRE: order quotes timing out, pricing-engine p99 2.6s, market-data-service serialization change suspected. [F7, F8]
- `2026-05-20T13:56:00Z` pricing-engine p99 still elevated in the 13:56 bucket (avg 2580.86ms); CPU pinned ~99% through 13:53Z — no recovery through the data window. [F9, F10]
- `2026-05-20T13:58:24Z` Latest order-service quote timeout observed; incident ongoing at latest data (13:59Z). [F7]

## Recommended actions
- **[immediate]** Immediately roll back pricing-engine config change cfg-3120: set flags.pricing.greeks_v2.rollout_pct back to 5 (or 0) and land it now; this is the highest-confidence mitigation and targets the exact change whose timestamp coincides with the onset. -- cfg-3120 is the only pricing-engine config change since 2026-05-01 and lands in the exact bucket where pricing-engine p99 jumped 60ms->2534.9ms and CPU stepped ~40%->99%, activating the GreeksV2 Monte-Carlo path that is the observed slow component. [F2, F27, F8, F33, F12, F18]
- **[immediate]** As a secondary measure, if quote failures persist after the flag rollback, shed or throttle GreeksV2 computation (e.g. reduce Monte-Carlo paths from 20000, or disable the calculator path) before considering changes to order-service, which has no fault of its own. -- order-service's only error signature is a 2500ms timeout on its pricing-engine call, ~2 minutes after pricing-engine degraded; order-service itself is a symptom, not the origin. [F7, F14, F12]
- **[immediate]** Do not roll back dep-6111 (market-data-service v5.2.0) as a mitigation; it landed 8 minutes before onset with no degradation signal and market-data-service shows zero errors. -- The market-data-service serialization hypothesis (H2) is rejected on timing (8-minute quiet gap), error signature (no deserialization errors), path topology (no direct order-service->market-data-service edge) and zero market-data-service errors. [F16, F13, F17, F7]
- **[short_term]** Verify recovery by confirming pricing-engine p99 returns to ~60ms, CPU returns to ~40%, and error_rate returns to <1% within two 1-minute buckets after the rollback, and that order-service quote timeouts stop. -- The pre-ramp baseline was p99 ~57-61ms and CPU ~38-42% at flat request_rate; a matching post-rollback profile would serve as the missing intervention positive-control test for the causal chain. [F8, F33, F24, F7, F34]
- **[short_term]** Repair the config-audit query path (shard 2/3 returned partial in tc-006) so future incident change timelines are complete, and re-run the window-wide config/deploy query after the fix to confirm no unseen change is relevant. -- The partial config-audit response leaves open the possibility of an unseen config change on order-service, market-data-service or a shared dependency that could bear on the conclusion. [F31]
- **[short_term]** While validating recovery, keep the redis-cache (shared by both quote-path hops) and exchange-gateway/exchange-api dependencies under observation, and pull their telemetry if the flag rollback does not restore p99. -- redis-cache is the only shared:true node on the quote path, and no redis-side telemetry was pulled; it is an unexcluded alternative (H3) should the primary mitigation fail. [F5, F20, F32, F29]
- **[long_term]** Add per-flag / per-calculator request-count and cost telemetry (flagged-share and GreeksV2 time budget) so the flagged-path share can be measured directly rather than inferred, and add GC/heap/thread-pool metrics for pricing-engine. -- pricing-engine exposes only cpu_pct, error_rate, latency_p99_ms, memory_pct, request_rate and restarts; the 5%-vs-100% flagged split and any GC/thrash mechanism can currently only be inferred, and GC/thread-pool absence blocked direct exclusion of H4/H3-style alternatives. [F21, F26, F36, F37]
- **[long_term]** Introduce a staged canary with automatic guardrails for experiments that ramp expensive compute paths: ramp in small steps (e.g. 5->25->50->100) with automatic halt/rollback if p99 or CPU crosses a threshold, instead of a single-step full-traffic ramp. -- cfg-3120 moved the flag 5 -> 100 in a single step at 12:04Z, directly preceding a ~42x p99 jump and CPU pin; a guarded staged rollout would have capped blast radius and auto-reverted. [F2, F27, F36, F33]

## Data gaps
- The first config-change query (tc-006) returned partial:true ('config-audit shard 2/3 did not respond; changes may be missing'); the retry returned 2 changes but it is unverified whether shard 2/3 content was restored — an unseen config/flag change on order-service, market-data-service or a shared dependency could exist.
- No deploy/config change on order-service itself was observed in the window, so it cannot be confirmed from change history that order-service's own code/config was untouched before onset.
- No telemetry/error rates were pulled for redis-cache, kafka or ledger-db; exchange-api is external with has_telemetry:false — a shared-dependency event with no accompanying change record cannot be fully ruled out on the quote path.
- pricing-engine has no GC pause/frequency metric and zero GC log lines in the window; H4 (GC thrash) can only be argued indirectly from flat memory_pct and restarts=0, not refuted by direct GC telemetry.
- pricing-engine has no thread-pool saturation or queue-depth metric, so thread-pool saturation cannot be assessed.
- No per-flag or per-endpoint request-count metric exists, so the exact flagged-path share (5% vs 100%) is inferred from the latency transition rather than measured directly.
- No explicit uptime/start-time metric exists (only restarts, flat 0), so a silent in-place restart that did not increment the counter cannot be fully excluded.
- Only a percentage memory metric exists (no heap-used/heap-max bytes or allocation rate), so heap pressure inside a sub-minute window could be missed.
- No intervention/revert positive-control test was run: cfg-3120's rollback status through 13:59Z is unknown, so causation is supported by temporal and mechanistic alignment rather than by a demonstrated revert-and-recover.
- pricing-engine's only caller is order-service, so the broader-blast-radius question (H5) cannot be tested against a second client; the incident is confirmed only in order-service.
- Direct redis-cache consumers could not be enumerated (get_service_dependencies returned an empty edge list despite redis-cache being shared with telemetry), limiting a redis-side blast-radius check.
- Sub-minute onset timing within the 12:04 bucket cannot be resolved below 60s bucket granularity (config-applied log 12:04:16Z, first clear degraded bucket 12:05).

## What would change this conclusion
- An intervention test: if rolling back cfg-3120 (flags.pricing.greeks_v2.rollout_pct back to 5/0) does NOT restore pricing-engine p99 to ~60ms and CPU to ~40%, the flag-ramp causation would be substantially weakened.
- Evidence of a pricing-engine CPU/latency contributor that is independent of the flag: e.g. GC pause/frequency telemetry showing heavy GC thrash, a heap pressure spike, or thread-pool queue saturation coincident with the 12:04-12:05 onset, would elevate H4.
- Discovery — after the config-audit shard 2/3 gap is closed — of a missed config or deployment change landing at ~12:04-12:06Z on pricing-engine or a shared dependency (e.g. redis-cache) that better explains the onset.
- Telemetry showing redis-cache degradation or a cache-miss storm at ~12:04Z on either quote-path hop, which would support H3.
- Evidence that the slow Monte-Carlo path was already running at ~100% of requests before 12:04Z (e.g. per-flag request counts), which would decouple the flag ramp from the CPU/latency step and break H1's mechanism.
- Confirmation that other pricing-engine clients (if any exist outside the modeled graph) show the same degradation, which would support a shared-component framing over the flag-ramp explanation.

## Integrity warnings (set by code)
- fact demoted to assumption: pricing-engine p99/CPU/error-rate degrade ~2 minutes before order-service's firs

## Investigation stats
- latency 144.02s, LLM calls 25, tokens 191247 (in 157863 / out 33384)
- tool calls 41 (3 failed, 1 partial, 0 recovered by retry), findings 37/38 verified
