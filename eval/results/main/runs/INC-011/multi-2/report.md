# Incident investigation report: INC-011
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause identified with high confidence (0.82, capped at 0.88 by the Critic): the pricing-engine config/flag change cfg-3120 at 12:04:16Z ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100, enabling the compute-heavy GreeksV2 Monte-Carlo path (paths=20000, 2964-8942ms per calculation) for all traffic, pinning pricing-engine CPU at ~99% and saturating p99 latency at ~2.5s, which broke the internal 2000ms compute budget and caused order-service's 2500ms quote calls to time out. The chain is tight and complete: CPU (38-42%) and p99 (57-62ms) are flat baseline through the 12:04Z bucket and step simultaneously in the 12:05 bucket, one bucket after the flag apply (F25-F30), with pricing-engine-local GreeksV2 log signatures confirming the mechanism (F11, F28). Alternatives H2 (market-data-service protobuf deploy), H4 (traffic surge) and H5 (config change elsewhere) are contradicted by flat metrics across the fleet, and H3 (redis-cache saturation) is refuted by flat redis counters and no error logs. Remaining gaps (per-pod CPU split, sub-minute resolution, ledger-db and blast-radius queries) are non-blocking and cannot change the ranking.

## Probable root cause
**Greeks v2 flag ramp to 100% overloaded pricing-engine CPU** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.82, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Greeks v2 flag ramp to 100% overloaded pricing-engine CPU | config_change | pricing-engine | cfg-3120 | 0.82 (high) | active |
| 2 | H2: market-data-service protobuf serialization change cascaded into pricing-engine | bad_deployment | market-data-service | dep-6111 | 0.04 (low) | rejected |
| 3 | H3: Shared downstream dependency (redis-cache / datastore) saturated, slowing pricing-engine | dependency_failure | redis-cache | null | 0.03 (low) | rejected |
| 4 | H4: Traffic surge at the quote step exceeded pricing-engine capacity | capacity_saturation | pricing-engine | null | 0.03 (low) | rejected |
| 5 | H5: Config change on an unqueried service (redis-cache / kafka / gateway) degraded the quote path | config_change | unknown | null | 0.02 (low) | rejected |

### H1: Greeks v2 flag ramp to 100% overloaded pricing-engine CPU
The pricing-engine config/flag change cfg-3120 at 12:04:16Z ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100, enabling the computationally expensive GreeksV2 Monte-Carlo path (paths=20000, 2964-8942ms each) for all traffic. At 1-minute resolution pricing-engine CPU and latency are flat baseline through the 12:04 bucket (CPU 38.22-40.18%, p99 56.82-60.53ms) and then step to ~98.65% CPU and ~2534.9ms p99 in the 12:05 bucket, exactly one bucket after the flag apply. This pinned CPU, saturated latency, blew the internal 2000ms compute budget, and caused order-service's 2500ms quote calls to time out. Origin is the config change at pricing-engine.

**Causal chain**
1. cfg-3120 changes flags.pricing.greeks_v2.rollout_pct 5->100 at 12:04:16Z ('config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100') [F2, F20, F28]
2. GreeksV2 Monte-Carlo (paths=20000, each call 2964-8942ms) becomes the hot path for all traffic; first WARN at 12:05:21Z [F11, F28]
3. pricing-engine CPU steps from flat ~38-40% (through 12:04) to ~98.6-99.4% in the 12:05 bucket and stays pinned [F10, F25, F27, F30]
4. pricing-engine p99 latency steps from flat ~57-61ms (through 12:04) to ~2534.9ms in the same 12:05 bucket; CPU and latency step in the SAME bucket [F9, F26, F27, F30]
5. pricing-engine exceeds its 2000ms compute budget, logging 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' [F11]
6. order-service's 2500ms client calls to pricing-engine time out, producing 225 identical 'pricing-engine quote call timed out after 2500ms' errors and error_rate jump to ~8% [F7, F8]

**Supporting evidence**
  - `F2` ✔ A pricing-engine config/flag change occurred at 2026-05-20T12:04:00Z: cfg-3120 changed flags.pricing.greeks_v2.rollout_pct from 5 to 100 ('Ramp Greeks v2 experiment to all traffic'), author pm-experiments. This is 8 minutes after the market-data-service deploy and ~12 minutes before the 12:16Z report.
  - `F7` ✔ order-service ERROR logs contain a single pattern only: 'pricing-engine quote call timed out after 2500ms'. 225 ERROR entries, first_seen 2026-05-20T12:06:10Z, all identical 2500ms timeout — i.e. the timeouts are client-side waiting on the pricing-engine dependency, not internal CPU/GC/pool errors.
  - `F8` ✔ order-service error_rate was flat ~0.14-0.23% through 11:30-12:02Z, then jumped to 8.1% avg in the 12:06 bucket and stayed ~8.0-8.4% through 13:58Z. Onset therefore in the 12:02-12:06 window.
  - `F9` ✔ pricing-engine latency_p99_ms rose one bucket earlier than order-service errors: 59.33ms at 11:58Z → avg 677.61ms (max 2534.9ms) at 12:02Z → ~2600ms saturated from 12:06Z onward. This places pricing-engine as the first-degrading service.
  - `F10` ✔ pricing-engine CPU_pct was ~40% baseline through 11:58Z, then spiked to avg 54.28%/max 98.65% in the 12:02 bucket and pinned at ~98.6-99.4% (max 100.0) from 12:06Z onward — resource saturation local to pricing-engine coinciding with the latency jump.
  - `F11` ✔ pricing-engine's own logs show the internal cause signature: WARN 'GreeksV2Calculator: Monte-Carlo paths=20000 took <n>ms' (342 occurrences, first 12:05:21Z, durations 2964-8942ms) and ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (276 occurrences, first 12:06:41Z). The 2000ms compute budget matches the 2500ms client timeout seen by order-service.
  - `F17` ✔ pricing-engine's own request_rate is flat across the onset (11:54 avg 632.35, 12:01 avg 648.32, 12:08 avg 645.77, 12:15 avg 647.98 req/s; summary mean 645.07) and memory_pct is flat ~48-50% (12:01 avg 49.05, 12:08 avg 48.86; summary min 44.65 / max 54.11). So pricing-engine's degradation at 12:02 is NOT explained by a traffic or memory/payload change: throughput held constant while CPU pinned and latency saturated.
  - `F20` ✔ Over ALL services in 11:30-13:59Z the complete (partial=false) query returns exactly ONE config/flag change: cfg-3120, pricing-engine, flags.pricing.greeks_v2.rollout_pct, 5->100, changed_at 2026-05-20T12:04:00Z, author pm-experiments, 'Ramp Greeks v2 experiment to all traffic'. No other service has any in-window config change.
  - `F25` ✔ pricing-engine cpu_pct at 1-minute buckets from 11:54Z-12:11Z is flat baseline 38.03-41.86% for every bucket through 12:04Z (11:56 avg/max 40.22, 11:58 41.86, 12:00 41.45, 12:02 40.08, 12:03 40.18, 12:04 38.22) and then steps to 98.65% in the 12:05 bucket, staying 98.09-100.0% thereafter (12:05 98.65, 12:06 99.25, 12:08 100.0, 12:10 100.0). No bucket in 11:54-12:04 shows any elevated or rising CPU.
  - `F26` ✔ pricing-engine latency_p99_ms at 1-minute buckets is flat 56.82-61.75ms in every bucket from 11:54Z through 12:04Z (11:56 56.86, 11:58 60.41, 12:00 60.06, 12:02 58.17, 12:03 56.82, 12:04 60.53), then steps to 2534.9ms in the 12:05 bucket and stays 2410.97-2849.25ms through 12:11 (12:08 2849.25 max, summary max 2849.25).
  - `F27` ✔ CPU and latency step simultaneously in the SAME bucket (12:05), not one before the other, and there is NO sub-bucket evidence of a pre-12:05 rise: the last baseline buckets 12:02 (CPU 40.08, p99 58.17), 12:03 (40.18 / 56.82) and 12:04 (38.22 / 60.53) are indistinguishable from the 11:54-12:00 baseline. This refutes a step beginning at/before 12:02Z.
  - `F28` ✔ The cfg-3120 flag ramp applied at 12:04:16Z (INFO log 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100'), and the first GreeksV2Calculator Monte-Carlo WARN appears at 12:05:21Z (6465ms), i.e. the metric step bucket (12:05) immediately follows the flag application — ordering is flag-change THEN step, matching a step exactly at the 12:04Z ramp rather than preceding it.
  - `F29` ✔ Achieved granularity for pricing-engine is genuine 1-minute buckets: both queries returned bucket_seconds=60 with missing_points=0, partial=false, warnings=[] and no truncation, so sub-bucket (per-minute) onset resolution IS possible and the 12:04→12:05 boundary is resolved to one minute. No timestamp-skew or bucket-alignment artifact is evident — the flagged change log (12:04:16Z) and the metric step (12:05 bucket) are consistent with a bucket-ending-vs-bucket-starting convention rather than skew.
  - `F30` ✔ Because a 60s bucket average would already smear any intra-bucket onset, note the 12:05 bucket is fully saturated (CPU avg=max=98.65, p99 avg=max=2534.9) — the step is a full-bucket jump with no partial ramp visible at 1-minute resolution, so the true onset is bounded to 12:04:00-12:05:00Z (after the 12:04:16Z flag apply), one bucket after the pre-12:05 flat line.
**Contradicting evidence**
  - (none)
**Assumptions**
  - That the CPU/latency step is attributable to the flag ramp rather than an unlogged simultaneous change; supported by the 12:04:16Z apply timestamp preceding the 12:05 step and the GreeksV2 log signature, but no causal experiment was run
  - That the flag had negligible effect at 5% rollout (no baseline for the 5% window exists)

### H2: market-data-service protobuf serialization change cascaded into pricing-engine
The market-data-service v5.2.0 protobuf serialization deploy at 11:56Z introduced a change that cascaded through pricing-engine into order-service quote timeouts. Every market-data-service signal (server-side p99 ~39-40ms, request_rate ~940 req/s, cpu ~45%, error_rate ~0.2%) is flat across the deploy and onset, and pricing-engine shows no downstream-call signature; the release is excluded as the origin.

**Causal chain**
1. market-data-service deployed v5.2.0 'Switch quote serialization to protobuf' at 11:56Z [F1]
2. The serialization change increased latency or bloat in market-data-service responses *(assumed)*
3. pricing-engine's calls to market-data-service slowed, cascading into pricing-engine latency *(assumed)*
4. order-service quote calls timed out [F7]

**Supporting evidence**
  - `F1` ✔ Only one deployment occurred in 11:30-13:59 across ALL services: dep-6111, market-data-service v5.2.0, deployed at 2026-05-20T11:56:00Z by v.rao, summary 'Switch quote serialization to protobuf'; status 'live' with rolled_back_at=null (not rolled back as of latest data 13:59Z).
**Contradicting evidence**
  - `F12` ✔ market-data-service (the service that shipped the serialization change) shows NO degradation: latency_p99_ms stayed flat ~39-41ms for the entire 11:30-13:59Z window (min 35.28, max 43.75, no bucket above 43.75ms), and its only log is a single unrelated TLS-cipher WARN. Its healthy status argues against it being the source of the pricing-engine timeouts.
  - `F13` ✔ market-data-service latency_p99_ms did NOT shift at the 11:56Z serialization deploy or the 12:04Z flag change: it is flat across the whole 11:30-13:59Z window, 11:54 bucket avg 39.37ms, 11:58 avg 39.06ms, 12:02 avg 39.84ms, 12:06 avg 39.43ms, with summary min 35.28 / max 43.75 / mean 39.91. No bucket exceeds 43.75ms. This directly answers the objective: pricing-engine's calls to market-data-service did not get slower after the protobuf change.
  - `F14` ✔ market-data-service request rate is flat through the deploy and onset: 11:54 avg 938.99, 11:58 avg 941.71, 12:02 avg 936.4, 12:06 avg 944.97 req/s; summary min 858.07 / max 1063.86 / mean 952.39. There is no traffic surge or step change at 11:56Z or 12:04Z (the mild drift upward ~976-1000 req/s at 12:22-12:58 is small and unrelated to onset).
  - `F15` ✔ market-data-service cpu_pct stayed ~44-47% with no step at either change: 11:54 avg 45.73, 11:58 avg 45.36, 12:02 avg 45.27, 12:06 avg 45.4; summary min 39.91 / max 49.97 / mean 45.37. No CPU spike co-located with the deploy or with onset.
  - `F16` ✔ market-data-service error_rate was flat ~0.15-0.27% for the entire window including the onset buckets (11:58 avg 0.22, 12:02 avg 0.16, 12:06 avg 0.21; summary min 0.1 / max 0.32 / mean 0.21). No error elevation at the deploy or at onset, so market-data-service shows no functional or resource degradation at the time pricing-engine saturated.
  - `F18` ✔ Timing precludes the 11:56Z market-data-service deploy as the trigger of the 12:02Z degradation: every market-data metric is unchanged in the buckets straddling 11:56Z (latency ~39-40ms, request_rate ~940 req/s, cpu ~45%), i.e. no post-deploy step at any bucket from 11:58 to 13:58. The only signal that moves at onset is pricing-engine-local (CPU spike/latency saturation per T1); the pricing-engine-local flag change cfg-3120 at 12:04Z (greeks_v2.rollout_pct 5->100) is the change closer to onset, though CPU already began rising in the 12:02 bucket.
  - `F11` ✔ pricing-engine's own logs show the internal cause signature: WARN 'GreeksV2Calculator: Monte-Carlo paths=20000 took <n>ms' (342 occurrences, first 12:05:21Z, durations 2964-8942ms) and ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (276 occurrences, first 12:06:41Z). The 2000ms compute budget matches the 2500ms client timeout seen by order-service.
  - `F27` ✔ CPU and latency step simultaneously in the SAME bucket (12:05), not one before the other, and there is NO sub-bucket evidence of a pre-12:05 rise: the last baseline buckets 12:02 (CPU 40.08, p99 58.17), 12:03 (40.18 / 56.82) and 12:04 (38.22 / 60.53) are indistinguishable from the 11:54-12:00 baseline. This refutes a step beginning at/before 12:02Z.
  - `F28` ✔ The cfg-3120 flag ramp applied at 12:04:16Z (INFO log 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100'), and the first GreeksV2Calculator Monte-Carlo WARN appears at 12:05:21Z (6465ms), i.e. the metric step bucket (12:05) immediately follows the flag application — ordering is flag-change THEN step, matching a step exactly at the 12:04Z ramp rather than preceding it.
**Assumptions**
  - That a mechanism exists by which flat server-side mds metrics still slow pricing-engine
**Rejected because:** Every market-data-service metric is flat across the deploy and onset (F12-F16, F18): server-side p99 ~39-40ms, request_rate ~940 req/s, cpu ~45%, error_rate ~0.2%, no step at 11:56Z or 12:04Z. The 11:56Z deploy precedes onset by ~9 minutes with no sub-bucket step, and pricing-engine's own bottleneck signature is GreeksV2Calculator CPU (F11, F28), not a downstream call. Residual caveat: payload size has no direct metric and is excluded only by proxy (latency/request_rate/cpu/memory) - minor because all proxies are uniformly flat.

### H3: Shared downstream dependency (redis-cache / datastore) saturated, slowing pricing-engine
A shared downstream dependency of pricing-engine (redis-cache, reached via F4) became resource-saturated around onset, slowing pricing-engine p99 and cascading to order-service timeouts. This was previously untested due to budget exhaustion. It is now directly tested and refuted: every redis-cache signal (cpu_pct, hit_ratio_pct, memory_pct, connected_clients, evictions_per_min) is flat across 11:30-13:59Z with no change coinciding with onset, and redis-cache emits no relevant error/warn logs.

**Causal chain**
1. redis-cache degrades or saturates around 12:04-12:05Z *(assumed)*
2. pricing-engine cache lookups slow, raising its latency and CPU (retries/serialization) [F9, F10]
3. order-service quote calls hit the 2500ms timeout [F7]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F31` ✔ redis-cache cpu_pct is completely flat across the entire 11:30-13:59Z window, with no elevation at the 12:00-12:06Z onset: 11:58 bucket avg 22.7/max 22.87, 12:02 avg 21.98/max 22.57, 12:06 avg 22.41/max 23.09; summary min 19.53 / max 24.13 / mean 21.94 % over 150 points, missing_points 0. No saturation of redis-cache CPU coincides with pricing-engine's 12:02-12:06 CPU pin at ~98.6-99.4% (F10).
  - `F32` ✔ redis-cache hit_ratio_pct is flat and healthy (95-98%) with no dip at onset: 11:58 avg 95.93, 12:02 avg 96.36, 12:06 avg 96.73; summary min 93.83 / max 98.46 / mean 96.02 %. A shared-cache saturation or cold-cache/miss storm (H3) would drive hit ratio down and/or misses up; neither occurs.
  - `F33` ✔ redis-cache memory_pct is flat ~69-72% with no growth step at onset: 11:58 avg 70.83, 12:02 avg 70.2, 12:06 avg 69.66; summary min 63.12 / max 79.59 / mean 70.12 % over 150 points, missing_points 0. No memory pressure/eviction pressure developed when pricing-engine degraded.
  - `F34` ✔ redis-cache connected_clients is flat ~296-325, no connection pile-up at onset: 11:58 avg 314.43/max 335.37, 12:02 avg 316.34/max 335.28, 12:06 avg 309.61/max 322.35; summary min 282.36 / max 348.57 / mean 311.36 clients. Also evictions_per_min stayed flat ~3/min (12:02 avg 3.11, 12:06 avg 3.27; summary min 0.78 / max 5.04). No client exhaustion or eviction storm at the 12:02-12:06 onset.
  - `F35` ✔ redis-cache emits no error or warning logs during the incident: only 1 WARN in 11:30-13:59Z, 'deprecated TLS cipher offered by client 46e36f97' at 13:28:54Z (unrelated to onset). No timeout, eviction, OOM, or slow-command messages. This is consistent with the flat metrics: redis-cache shows no functional or resource distress.
  - `F36` ✔ All five metrics exposed by redis-cache (cpu_pct, latency replaced by hit_ratio, memory_pct, connected_clients, evictions_per_min) are flat across 11:30-13:59Z with no change coinciding with pricing-engine's 12:02-12:06 saturation. redis-cache exposes no latency_p99_ms and no generic 'connections' metric (available_metrics: connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct), so cache-side latency itself could not be measured directly; the four measurable signals that a saturated shared cache would move (CPU, hit ratio, memory, clients/evictions) all remain at baseline.
  - `F11` ✔ pricing-engine's own logs show the internal cause signature: WARN 'GreeksV2Calculator: Monte-Carlo paths=20000 took <n>ms' (342 occurrences, first 12:05:21Z, durations 2964-8942ms) and ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (276 occurrences, first 12:06:41Z). The 2000ms compute budget matches the 2500ms client timeout seen by order-service.
  - `F17` ✔ pricing-engine's own request_rate is flat across the onset (11:54 avg 632.35, 12:01 avg 648.32, 12:08 avg 645.77, 12:15 avg 647.98 req/s; summary mean 645.07) and memory_pct is flat ~48-50% (12:01 avg 49.05, 12:08 avg 48.86; summary min 44.65 / max 54.11). So pricing-engine's degradation at 12:02 is NOT explained by a traffic or memory/payload change: throughput held constant while CPU pinned and latency saturated.
  - `F25` ✔ pricing-engine cpu_pct at 1-minute buckets from 11:54Z-12:11Z is flat baseline 38.03-41.86% for every bucket through 12:04Z (11:56 avg/max 40.22, 11:58 41.86, 12:00 41.45, 12:02 40.08, 12:03 40.18, 12:04 38.22) and then steps to 98.65% in the 12:05 bucket, staying 98.09-100.0% thereafter (12:05 98.65, 12:06 99.25, 12:08 100.0, 12:10 100.0). No bucket in 11:54-12:04 shows any elevated or rising CPU.
  - `F28` ✔ The cfg-3120 flag ramp applied at 12:04:16Z (INFO log 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100'), and the first GreeksV2Calculator Monte-Carlo WARN appears at 12:05:21Z (6465ms), i.e. the metric step bucket (12:05) immediately follows the flag application — ordering is flag-change THEN step, matching a step exactly at the 12:04Z ramp rather than preceding it.
**Assumptions**
  - redis-cache exposes no latency metric (F36), so cache-side latency is not directly measurable; H3 is falsified on the four measurable saturation signals (CPU, hit ratio, memory, clients/evictions) plus absence of error logs, all flat
**Rejected because:** redis-cache cpu_pct flat (F31), hit_ratio flat 95-98% with no miss storm (F32), memory_pct flat (F33), connected_clients and evictions flat (F34), no error/warn logs during the window (F35). A saturated shared cache would move these counters; none move. Additionally pricing-engine CPU is flat through 12:04 and steps to 98.65% only in the 12:05 bucket, immediately after the 12:04:16Z flag apply, with a pricing-engine-local GreeksV2 signature (F28) rather than a cache-lookup pattern.

### H4: Traffic surge at the quote step exceeded pricing-engine capacity
A traffic surge at the quote step increased load beyond provisioned capacity, saturating pricing-engine CPU and causing order-service timeouts, with no code/flag change responsible.

**Causal chain**
1. Quote-step request volume rises beyond capacity at ~12:04Z *(assumed)*
2. pricing-engine CPU pins at ~99% under the load [F10, F25]
3. pricing-engine latency saturates and order-service times out [F9, F26, F7]

**Supporting evidence**
  - `F5` ✔ Historical precedents for quote-timeout / latency symptoms exist with causes different from a serialization change: HIST-113 (2026-01-28) pricing-engine CPU saturation at market open caused 'quote timeouts' (capacity_saturation); HIST-107 (2025-12-09) cold cache storm after redis failover caused 'quote latency' (infrastructure_failure); HIST-110 (2026-01-15) ledger-db IOPS throttling caused order latency/statement timeouts; HIST-101 (2025-08-14) is the closest deployment-induced latency case (auth-service bad release, bcrypt cost 10->14, rolled back).
**Contradicting evidence**
  - `F17` ✔ pricing-engine's own request_rate is flat across the onset (11:54 avg 632.35, 12:01 avg 648.32, 12:08 avg 645.77, 12:15 avg 647.98 req/s; summary mean 645.07) and memory_pct is flat ~48-50% (12:01 avg 49.05, 12:08 avg 48.86; summary min 44.65 / max 54.11). So pricing-engine's degradation at 12:02 is NOT explained by a traffic or memory/payload change: throughput held constant while CPU pinned and latency saturated.
  - `F14` ✔ market-data-service request rate is flat through the deploy and onset: 11:54 avg 938.99, 11:58 avg 941.71, 12:02 avg 936.4, 12:06 avg 944.97 req/s; summary min 858.07 / max 1063.86 / mean 952.39. There is no traffic surge or step change at 11:56Z or 12:04Z (the mild drift upward ~976-1000 req/s at 12:22-12:58 is small and unrelated to onset).
  - `F11` ✔ pricing-engine's own logs show the internal cause signature: WARN 'GreeksV2Calculator: Monte-Carlo paths=20000 took <n>ms' (342 occurrences, first 12:05:21Z, durations 2964-8942ms) and ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (276 occurrences, first 12:06:41Z). The 2000ms compute budget matches the 2500ms client timeout seen by order-service.
  - `F25` ✔ pricing-engine cpu_pct at 1-minute buckets from 11:54Z-12:11Z is flat baseline 38.03-41.86% for every bucket through 12:04Z (11:56 avg/max 40.22, 11:58 41.86, 12:00 41.45, 12:02 40.08, 12:03 40.18, 12:04 38.22) and then steps to 98.65% in the 12:05 bucket, staying 98.09-100.0% thereafter (12:05 98.65, 12:06 99.25, 12:08 100.0, 12:10 100.0). No bucket in 11:54-12:04 shows any elevated or rising CPU.
  - `F26` ✔ pricing-engine latency_p99_ms at 1-minute buckets is flat 56.82-61.75ms in every bucket from 11:54Z through 12:04Z (11:56 56.86, 11:58 60.41, 12:00 60.06, 12:02 58.17, 12:03 56.82, 12:04 60.53), then steps to 2534.9ms in the 12:05 bucket and stays 2410.97-2849.25ms through 12:11 (12:08 2849.25 max, summary max 2849.25).
  - `F28` ✔ The cfg-3120 flag ramp applied at 12:04:16Z (INFO log 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100'), and the first GreeksV2Calculator Monte-Carlo WARN appears at 12:05:21Z (6465ms), i.e. the metric step bucket (12:05) immediately follows the flag application — ordering is flag-change THEN step, matching a step exactly at the 12:04Z ramp rather than preceding it.
**Assumptions**
  - That a traffic surge exists (directly contradicted)
**Rejected because:** pricing-engine request_rate is flat across onset (F17) and market-data-service request rate is flat (F14). No traffic step exists. The CPU and latency step occur in the same 12:05 bucket, one minute after a flag apply that enables a compute-heavy code path (F25, F26, F28), i.e. the saturation is compute-driven, not load-driven.

### H5: Config change on an unqueried service (redis-cache / kafka / gateway) degraded the quote path
A config/feature-flag change on a service other than pricing-engine degraded the quote path around onset. This was originally untestable because the global config audit returned partial=true. That gap is now closed: the retried complete global query (partial=false) returns exactly ONE config change anywhere in 11:30-13:59Z (cfg-3120 on pricing-engine), and every service named in H5 (redis-cache, ledger-db, kafka, api-gateway, exchange-gateway) is confirmed to have zero in-window config changes.

**Causal chain**
1. A config change on an unqueried service occurred near onset *(assumed)*
2. That change slowed a shared dependency used by the quote path *(assumed)*
3. pricing-engine/order-service latency rose and quotes timed out [F8, F9]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F19` ✔ The retried global get_config_changes over 2026-05-20T11:30:00Z-13:59:00Z now succeeds completely: partial=false, warnings=[] (vs the earlier tc-002 run that returned partial=true 'config-audit shard 2/3 did not respond'). The config-audit shard 2/3 is responsive again, so H5 is testable rather than untestable.
  - `F20` ✔ Over ALL services in 11:30-13:59Z the complete (partial=false) query returns exactly ONE config/flag change: cfg-3120, pricing-engine, flags.pricing.greeks_v2.rollout_pct, 5->100, changed_at 2026-05-20T12:04:00Z, author pm-experiments, 'Ramp Greeks v2 experiment to all traffic'. No other service has any in-window config change.
  - `F21` ✔ redis-cache had ZERO config/flag changes in 11:30-13:59Z (count=0, complete result, no partial flag).
  - `F22` ✔ kafka had ZERO config/flag changes in 11:30-13:59Z (count=0, complete result).
  - `F23` ✔ api-gateway had ZERO config/flag changes in 11:30-13:59Z (count=0, complete). A separate pre-window query confirms its only nearby change was cfg-3118 ratelimit.burst 200->250 at 2026-05-20T10:30:00Z by config-bot — 1h26m before onset, outside the incident window.
  - `F24` ✔ ledger-db and exchange-gateway each had ZERO config/flag changes in 11:30-13:59Z (count=0, complete results). With these, every service named in H5 (redis-cache, ledger-db, kafka, api-gateway, exchange-gateway) is confirmed to have no in-window config change, and the complete global query confirms cfg-3120 is the only one anywhere.
  - `F28` ✔ The cfg-3120 flag ramp applied at 12:04:16Z (INFO log 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100'), and the first GreeksV2Calculator Monte-Carlo WARN appears at 12:05:21Z (6465ms), i.e. the metric step bucket (12:05) immediately follows the flag application — ordering is flag-change THEN step, matching a step exactly at the 12:04Z ramp rather than preceding it.
  - `F11` ✔ pricing-engine's own logs show the internal cause signature: WARN 'GreeksV2Calculator: Monte-Carlo paths=20000 took <n>ms' (342 occurrences, first 12:05:21Z, durations 2964-8942ms) and ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (276 occurrences, first 12:06:41Z). The 2000ms compute budget matches the 2500ms client timeout seen by order-service.
**Rejected because:** The complete (partial=false) global config query now returns exactly one in-window change, cfg-3120 on pricing-engine (F20); redis-cache, kafka, api-gateway, ledger-db and exchange-gateway are each confirmed with zero in-window config changes (F21-F24). The incomplete-audit premise (F6) is retired by F19. Moreover no mechanism links an unqueried-service flag to the pricing-engine-local GreeksV2 compute signature (F11, F28).

## Facts (verified observations)
- A pricing-engine config/flag change occurred: cfg-3120 changed flags.pricing.greeks_v2.rollout_pct from 5 to 100 ('Ramp Greeks v2 experiment to all traffic'), author pm-experiments, with the apply logged at 12:04:16Z. [F2, F28]
- The complete (partial=false) global config-change query over 11:30-13:59Z returns exactly ONE change across all services: cfg-3120 on pricing-engine; redis-cache, kafka, api-gateway, ledger-db and exchange-gateway each have zero in-window config changes. [F19, F20, F21, F22, F23, F24]
- pricing-engine CPU_pct and latency_p99_ms are flat and indistinguishable from baseline in every 1-minute bucket through 12:04Z inclusive (cpu 38.03-41.86%; p99 56.82-61.75ms), then step simultaneously in the 12:05 bucket to 98.65% CPU and 2534.9ms p99, staying pinned thereafter. [F25, F26, F27, F29]
- pricing-engine's own logs show the internal cause signature: WARN 'GreeksV2Calculator: Monte-Carlo paths=20000 took <n>ms' (342 occurrences, first 12:05:21Z, 2964-8942ms) and ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (276 occurrences, first 12:06:41Z), matching order-service's 2500ms client timeout. [F11]
- pricing-engine request_rate (~645 req/s) and memory_pct (~48-50%) are flat across onset, so the saturation is not explained by traffic or payload/memory growth. [F17]
- order-service ERROR logs contain a single pattern only: 'pricing-engine quote call timed out after 2500ms' (225 entries, first_seen 12:06:10Z), i.e. client-side timeouts waiting on the pricing-engine dependency. [F7]
- order-service error_rate jumped from ~0.14-0.23% to ~8.1% in the 12:06 bucket and stayed ~8.0-8.4% through 13:58Z, placing onset in the 12:02-12:06Z window. [F8]
- market-data-service, the service that shipped the protobuf serialization deploy, shows no degradation: latency_p99 flat ~39-41ms, request_rate flat ~940 req/s, cpu flat ~45%, error_rate flat ~0.2% across both the 11:56Z deploy and the onset. [F12, F13, F14, F15, F16]
- Only one deployment occurred in 11:30-13:59Z across all services: dep-6111, market-data-service v5.2.0 at 11:56Z, not rolled back; no deployments to pricing-engine and no config changes for order-service or market-data-service. [F1, F3]
- redis-cache resource and health signals are flat across 11:30-13:59Z (cpu ~22%, hit_ratio 95-98%, memory ~70%, connected_clients ~296-325, evictions ~3/min) with no error/warn logs, refuting shared-cache saturation at onset. [F31, F32, F33, F34, F35]
- Dependency path is order-service -> pricing-engine (http) -> market-data-service (http), with pricing-engine also depending on redis-cache; market-data-service and order-service do not share a datastore directly. [F4]

## Assumptions (not verified)
- That the CPU/latency step is attributable to the flag ramp rather than an unlogged simultaneous change; supported by the 12:04:16Z apply timestamp preceding the 12:05 step and the GreeksV2 log signature, but no causal experiment (controlled revert/canary) was run.
- That the flag had negligible effect at 5% rollout; no baseline measurement exists for the 5% window.
- That sub-minute resolution would not reveal a CPU rise before 12:04:16Z; the finest available bucket is 60s, so the onset is bounded to 12:04:16-12:05:00Z.
- That the pricing-engine CPU metric (service-level aggregate) reflects the replicas that received the flag; no per-pod breakdown was available.

## Timeline
- `2026-05-20T11:56:00Z` dep-6111 deployed: market-data-service v5.2.0, 'Switch quote serialization to protobuf' (not rolled back). [F1]
- `2026-05-20T11:56:00Z` No post-deploy step in any market-data-service metric (latency ~39ms, request_rate ~940 req/s, cpu ~45%), excluding the deploy as trigger. [F13, F14, F15, F16, F18]
- `2026-05-20T12:04:16Z` cfg-3120 applied on pricing-engine: flags.pricing.greeks_v2.rollout_pct 5 -> 100 ('Ramp Greeks v2 experiment to all traffic'). [F2, F28, F20]
- `2026-05-20T12:05:00Z` pricing-engine CPU and latency step simultaneously in the 12:05 1-minute bucket: CPU 98.65% (from 38.22% at 12:04) and p99 2534.9ms (from 60.53ms at 12:04). [F25, F26, F27, F29, F30]
- `2026-05-20T12:05:21Z` First pricing-engine WARN 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms'. [F11, F28]
- `2026-05-20T12:06:00Z` order-service error_rate jumps to ~8.1% and pins ~8.0-8.4% through 13:58Z. [F8]
- `2026-05-20T12:06:10Z` First order-service ERROR 'pricing-engine quote call timed out after 2500ms' (225 total). [F7]
- `2026-05-20T12:06:41Z` First pricing-engine ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (276 total). [F11]
- `2026-05-20T12:16:00Z` Incident INC-011 reported by on-call SRE. []
- `2026-05-20T13:59:00Z` Latest data: pricing-engine remains pinned at ~99-100% CPU and ~2.5s p99; order-service error rate remains ~8%; dep-6111 not rolled back. [F1, F25, F26, F8]

## Recommended actions
- **[immediate]** Revert cfg-3120: set flags.pricing.greeks_v2.rollout_pct back to 5 (or 0) on pricing-engine, restoring the pre-incident flags state as of the 12:04:16Z apply. -- The flag ramp is the only in-window change fleet-wide and immediately precedes the CPU/latency step; reverting is the direct mitigation and simultaneously serves as the causal test. [F2, F20, F25, F26, F28]
- **[immediate]** After reverting, verify pricing-engine CPU returns to ~38-42% and latency_p99 to ~57-62ms within 1-2 minutes, and confirm order-service error_rate returns from ~8% to ~0.2% and the 'quote call timed out after 2500ms' errors stop. -- Confirms the mitigation worked and closes the loop on the causal chain from flag to compute saturation to quote timeouts. [F25, F26, F8, F7, F11]
- **[short_term]** Do NOT roll back dep-6111 (market-data-service v5.2.0 protobuf change) as the primary fix; if operations insist, treat it as a separate canary, since its signals are flat. -- market-data-service metrics are flat across the deploy and onset, so a rollback would not address the saturation and risks unrelated churn. [F12, F13, F14, F15, F16, F18]
- **[short_term]** Re-ramp the Greeks v2 rollout only behind a staged canary (e.g. 5% -> 25% -> 50% -> 100%) with an automated guard on pricing-engine CPU and p99 compute budget, and pre-measure the 5% baseline before the next ramp. -- The mechanism is a per-request compute-cost explosion from GreeksV2 Monte-Carlo (paths=20000, 2964-8942ms) against a 2000ms budget, so capacity headroom must be established before enabling all traffic. [F11, F25, F26, F20]
- **[long_term]** Add per-dependency/outbound-call latency and per-pod CPU metrics for pricing-engine, and a cache-latency metric for redis-cache, so compute saturation versus downstream-call degradation can be discriminated in future incidents. -- Current aggregates (service-level cpu_pct, aggregate latency_p99) could not separate the saturated pod(s) from the fleet or isolate pricing-engine->market-data-service call latency, extending time-to-diagnosis. [F36, F17, F12]
- **[long_term]** Require pre-approval and capacity review for feature-flag ramps that enable new compute-heavy code paths, with an automatic kill-switch on pricing-engine CPU > ~85% or p99 > the 2000ms compute budget. -- The incident arose from a full 5->100 ramp of an expensive Monte-Carlo path; an automated guard would have contained the blast radius to the 12:04-12:05 window. [F2, F25, F26, F11]

## Data gaps
- No per-pod CPU breakdown (cpu_pct is a service-level aggregate): cannot distinguish 'flag ramped all replicas' from 'single hot pod' as the mechanism of the CPU step.
- Sub-minute resolution is unavailable (finest bucket is 60s), so a CPU rise a few seconds before 12:05:00 cannot be separated from the 12:04:16Z flag apply; onset is bounded to 12:04:16-12:05:00Z, which still postdates 12:02Z.
- No controlled revert/canary experiment was run, so the causal link is established by correlation plus pricing-engine-local log signature rather than by intervention.
- redis-cache exposes no latency metric, so cache-side response latency could not be measured directly; H3 is refuted on flat resource/health counters (cpu, hit_ratio, memory, clients, evictions) plus absence of error logs rather than on measured cache latency.
- ledger-db (shared=true, reached by order-service) was not queried in this investigation, and market-data-service blast-radius/other-caller metrics were not retrieved; the full blast radius beyond order-service/pricing-engine is unconfirmed.
- order-service's ERROR log list was truncated (30 of 225 entries returned), so a non-timeout error pattern below the truncation cannot be fully excluded.
- No payload-size/bytes-per-request metric exists for market-data-service, so the serialization-change payload-size question was addressed only via flat proxy metrics (latency, request_rate, cpu, memory).

## What would change this conclusion
- Reverting cfg-3120 (greeks_v2.rollout_pct back to 5) without any reduction in pricing-engine CPU (~98-100%) or p99 (~2.5s) would falsify H1 as the driver of the saturation.
- A verified config/flag change on another service (e.g. redis-cache, ledger-db, kafka, api-gateway, exchange-gateway) inside 11:30-13:59Z would reopen H5; the complete global query currently shows none.
- Evidence that pricing-engine CPU/latency began rising before 12:04:16Z at sub-minute resolution would break the cause-precedes-effect ordering.
- Evidence of a traffic surge (a request_rate step on pricing-engine or market-data-service coinciding with onset) would revive H4.
- A redis-cache latency or saturation signal coinciding with the 12:04-12:05 step (latency is currently unmeasurable) would revive H3.
- A newly surfaced deployment or code change to pricing-engine in the window (currently count=0) would introduce an alternative origin.

## Investigation stats
- latency 158.89s, LLM calls 26, tokens 205615 (in 172182 / out 33433)
- tool calls 44 (4 failed, 1 partial, 0 recovered by retry), findings 36/36 verified
