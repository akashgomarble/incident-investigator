# Incident investigation report: INC-011
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause identified with high confidence (0.78-0.84): config change cfg-3120 on pricing-engine ramped feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04Z, routing all quote traffic through the GreeksV2 Monte-Carlo calculator (20000 paths), which is a CPU-bound compute path far over the 2000ms per-request budget. pricing-engine CPU stepped from ~40% to ~99% pinned, p99 rose to ~2.6s and 5xx from ~0.2% to ~12.4%, so a fraction of quote calls exceeded order-service's 2500ms client timeout and surfaced as order-service quote timeouts. The failing path is positively tagged ('request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2'), with zero downstream timeout/refusal signatures, and the reporter's stated suspect (market-data-service serialization change) is actively contradicted by flat latency/error/throughput on that service and by redis-cache being flat. The flag remains live and was never reverted.

## Probable root cause
**Greeks v2 feature-flag ramp (5%→100%) routes all quote traffic onto a CPU-bound Monte-Carlo compute path that pins pricing-engine CPU and blows the 2s/2.5s budgets** -- config_change in `pricing-engine`, triggered by `tc-011` (confidence 0.78, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Greeks v2 feature-flag ramp (5%→100%) routes all quote traffic onto a CPU-bound Monte-Carlo compute path that pins pricing-engine CPU and blows the 2s/2.5s budgets | config_change | pricing-engine | tc-011 | 0.78 (high) | active |
| 2 | H5: An unenumerated config change on pricing-engine/order-service (or an uncaptured pricing-engine code deploy) is the true trigger | config_change | pricing-engine | null | 0.05 (low) | active |
| 3 | H4: Coincidental/pre-existing pricing-engine capacity saturation at a daily peak (no faulty change on the quote path) | capacity_saturation | pricing-engine | null | 0.04 (low) | active |
| 4 | H2: market-data-service v5.2.0 protobuf serialization change bloats/destabilizes quote payloads consumed by pricing-engine | bad_deployment | market-data-service | tc-001 | 0.03 (low) | active |
| 5 | H3: Shared redis-cache degradation (cache-miss storm) stalls pricing-engine and market-data-service | infrastructure_failure | redis-cache | null | 0.03 (low) | active |

### H1: Greeks v2 feature-flag ramp (5%→100%) routes all quote traffic onto a CPU-bound Monte-Carlo compute path that pins pricing-engine CPU and blows the 2s/2.5s budgets
At 12:04Z config change cfg-3120 (tc-011) ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100, routing all quote requests through the new GreeksV2 calculator. That calculator runs a 20000-path Monte-Carlo whose per-request CPU cost is far above budget: in-process compute takes 6.5-8.9s per call against a 2000ms application budget, pinning pricing-engine CPU at ~99% and raising p99 to ~2.6s, so a fraction of quote calls exceed order-service's 2500ms client timeout and surface as order-service quote timeouts and ~8% 5xx. The mechanism is now positively corroborated by the failure semantics, not just timing: the 5xx text is 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2', the slow-log is a literal Monte-Carlo compute-duration line, and there are zero downstream timeout/refusal signatures. The critic's alternative mechanism (thread-pool/queue/connection-pool exhaustion or blocking on a dependency) is contradicted by F28 and is a competing reading of the SAME origin (the flag), so it is carried as an open detail under assumptions rather than as a separate hypothesis.

**Causal chain**
1. cfg-3120 ramps flags.pricing.greeks_v2.rollout_pct 5->100 on pricing-engine at 12:04Z; it is the only config change on any service in the window, and it is applied on pricing-engine itself at 12:04:16Z. [F2, F19, F24, F29, F22]
2. Within 65s of the flag applying, the first GreeksV2Calculator slow-compute WARN appears (12:05:21Z) reporting Monte-Carlo paths=20000 completing in 6465-8942ms against a 2000ms budget. [F29, F27, F26]
3. In the same 12:04-12:07Z bucket pricing-engine CPU_pct steps ~40% -> sustained 99-100% plateau and latency_p99 steps ~58ms -> ~2.6s, while inbound request_rate stays flat (~640 req/s) and memory stays flat (~48-50%) with zero restarts. [F13, F14, F15, F8, F11, F18]
4. CPU-bound compute makes a fraction of quote requests exceed budgets: pricing-engine 5xx steps ~0.2% -> ~12.4% with the single error signature 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (56 occurrences, first 12:06:41Z); no downstream timeout/refused signatures exist. [F25, F26, F16, F30, F28]
5. The over-budget quote calls breach order-service's 2500ms client timeout, producing 225 of 227 identical 'pricing-engine quote call timed out after 2500ms' ERROR lines from 12:06:10Z and order-service error_rate stepping to ~8.2%. [F7, F9]
6. market-data-service (the reporter's suspect) and the shared redis-cache show no error, latency, throughput or CPU deviation at any point, so extra cost is internal to pricing-engine's compute path, not payload- or dependency-driven. [F10, F12, F17]

**Supporting evidence**
  - `F2` ✔ Config change on pricing-engine: feature flag flags.pricing.greeks_v2.rollout_pct ramped from 5 to 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic') -- 8 minutes after the market-data-service deploy and ~12 min before the 12:16Z report. (First query returned partial:true, 0 changes; retry on full shards returned this.)
  - `F19` ✔ Across all services the ONLY config change in 11:30-13:59Z is cfg-3120: pricing-engine flags.pricing.greeks_v2.rollout_pct ramped 5->100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'). No second config/flag change exists on any service, so no OTHER flag landed at 12:04-12:05Z.
  - `F24` ✔ The pricing-engine config-change list (scope pricing-engine, 11:30-13:59Z) contains exactly one entry, cfg-3120, and is complete (partial:false) — the previously partial/empty audit shard is now retrieved.
  - `F29` ✔ The Greeks v2 flag ramp lands as an INFO config line on pricing-engine itself: 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100' at 2026-05-20T12:04:16Z — 65 seconds before the first GreeksV2Calculator slow-compute WARN (12:05:21Z) and matching F2's 12:04Z ramp to 100.
  - `F27` ✔ The WARN signature shows the mechanism as slow in-process compute, not a blocked call: 'GreeksV2Calculator: Monte-Carlo paths=20000 took <n>ms' with durations far over the 2000ms budget — e.g. 6465ms, 6966ms, 8361ms, 8710ms, 8942ms — 75 such WARN lines in 12:04-12:30Z, first at 12:05:21Z (earliest log signal, ~80s before the first ERROR at 12:06:41Z).
  - `F26` ✔ The failing code path is explicitly tagged: every ERROR line carries calculator=GreeksV2, and every WARN line in the same window is 'GreeksV2Calculator: Monte-Carlo paths=20000 took <n>ms for symbol opt-<n>'. The structured tag is a literal 'calculator=GreeksV2' field on the failures (no legacy/GreeksV1 tag appears among failures), so the failing requests are the Greeks v2 path.
  - `F25` ✔ pricing-engine's ERROR log in 12:04-12:30Z has exactly ONE signature, seen 56 times (all ERROR): 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2', first at 12:06:41Z, last at 12:29:37Z. It is an application-level deadline exceeded while COMPUTING the quote (2s budget), not an exception stack trace and not a downstream-call timeout/refusal.
  - `F13` ✔ pricing-engine CPU_pct steps from ~40% to full saturation in the same 12:05Z bucket as its latency p99 rise: 39.89 (11:58Z) -> 99.11 avg / 100.0 max (12:05Z), then stays 98.6-99.3 avg through 13:57Z. It is a sustained pinned-CPU plateau, not a spike.
  - `F14` ✔ pricing-engine inbound request_rate is FLAT across the onset: 638.32 req/s (11:58Z) -> 640.29 (12:05Z) -> ~631-662 for the rest of the window (min 571.46, max 741.12). There is no load/traffic surge driving the CPU saturation — CPU rose ~2.5x while request volume stayed constant.
  - `F15` ✔ pricing-engine memory_pct is FLAT at ~48-50% throughout (11:30Z avg 47.91 -> 13:57Z avg 48.54, min 44.65, max 54.11) with no step at onset, and restarts stays 0.0 for all 22 buckets. So the saturation is CPU-bound, not memory pressure or crash-loop.
  - `F8` ✔ pricing-engine latency_p99_ms was flat at ~56-64ms through the 12:01Z bucket (avg 57.99, max 58.97), then jumped in the 12:04Z bucket (avg 1705.61, max 2534.9) and reached full impact at 12:07Z (avg 2633.16, max 2849.25), staying ~2600ms for the rest of the window. The 420s-bucket view put the step at '12:05:00Z', but the 180s zoom shows the rise begins inside the 12:04-12:07 bucket.
  - `F11` ✔ pricing-engine p99 rise and order-service quote timeouts appear simultaneous at the resolution available: both step in the 12:04-12:07Z window, with the first order-service timeout at 12:06:10Z and the pricing-engine 12:04Z bucket already partially elevated (avg 1705.61 vs 57.99 at 12:01Z). No pre-12:04 elevation is visible in either service, so neither signal precedes the other beyond bucket resolution.
  - `F18` ✔ The pricing-engine onset (CPU pin, p99 2.6s, 5xx step, all in the 12:04-12:07Z window per F8/F11) coincides to within bucket resolution with the flags.pricing.greeks_v2.rollout_pct 5->100 ramp at 12:04Z (F2), whereas the market-data-service serialization deploy was 8 min earlier at 11:56Z (F1) and market-data-service telemetry is flat (F10). This favors a pricing-engine-internal compute change over an upstream payload/latency cause.
  - `F16` ✔ pricing-engine error_rate (HTTP 5xx) steps from ~0.2% to ~12.4% at the same 12:05Z bucket (0.19 -> 11.1 avg, max 13.61) and holds ~12.3-12.7% through 13:57Z. Roughly one in eight calls fails, i.e. it is mostly slow (p99 2.6s) rather than uniformly erroring.
  - `F30` ✔ pricing-engine error_rate (unit: % of requests (HTTP 5xx)) steps in the 12:04Z bucket: avg 6.46 / max 12.98 in 12:04-12:08, then avg 13.04 (max 13.61) at 12:08Z and holds ~12.0-12.9 avg through 12:28Z — consistent with F16's ~0.2%->~12.4% step.
  - `F28` ✔ There are NO downstream/latency/refusal signatures on pricing-engine: a contains='timeout' search over WARN+ returned 0 matches and contains='refused' returned 0 matches in 12:04-12:30Z (full shards, partial=false, data_range present). The only slowdowns are internal Compute-classed mechanics; no redis-cache / market-data-service / DB call timeout or connection-refused appears.
  - `F7` ✔ order-service quote timeouts began at 2026-05-20T12:06:10Z: the earliest ERROR log is 'pricing-engine quote call timed out after 2500ms orderId=4bd44879'; 225 of 227 matched log lines are this identical timeout pattern, all between 12:06:10Z and 13:58:24Z. There are no 5xx or connection-refused signatures in the WARN+ log set.
  - `F9` ✔ order-service error_rate shows the same step at the same coarse bucket: 0.18-0.22% through 11:58Z, then 6.93% at 12:05Z rising to ~8.1-8.3% and holding there through 13:57Z (max 8.87%). Error rate is described as '% of requests (HTTP 5xx)'.
  - `F10` ✔ market-data-service is healthy across the whole window: latency_p99_ms stays 39-41ms (12:00Z avg 39.46, 12:10Z avg 39.34), error_rate stays 0.16-0.25% with no step (12:00Z avg 0.2, 12:10Z avg 0.18), and request_rate stays ~930-985 req/s with no throughput dip. The only WARN in its log window is a single 'deprecated TLS cipher offered by client e0942b8a' at 11:43:53Z, unrelated to the incident.
  - `F12` ✔ The incident report's claim that market-data-service 'shipped a serialization change a few minutes before' is not corroborated by any telemetry: market-data-service shows no error, latency or throughput deviation at any point in 11:30-13:59Z, and its request_rate entering the incident window (12:00Z avg 938.08) is unchanged from baseline (11:30Z avg 929.09).
  - `F17` ✔ Shared downstream redis-cache (used by both pricing-engine and market-data-service, F4) is healthy across the window: cpu_pct flat ~21.5-22.5% (11:58Z avg 22.36 -> 12:05Z avg 22.45 -> 13:57Z avg 22.07, min 19.53, max 24.13). It exposes only connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct — it has no latency metric, so I could not measure cache response latency directly.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 5%->100 ramp actually changed the code path taken by the request flow at onset (the flag was not already effectively 100%, and is evaluated per request). Log evidence is indirect: no per-request flag-arm/rollout_pct tag is attached to individual request lines, so the pre-12:04 vs post-12:04 routing split cannot be quantified from logs.
  - Even with the flag's application and the calculator tag, pricing-engine exposes no thread-pool/queue/connection-pool metric, so a blocking mechanism producing the same CPU pin cannot be excluded by measurement — only by the absence of pool/downstream rejection signatures (F28). This does not change the fix locus (revert/repair the Greeks v2 path).
  - No revert of the flag was attempted or observed (F21), so 'rollback without recovery' could not be used as a discriminating test; the flag remains live, which is consistent with but does not prove causality.
  - The compute is CPU-bound at request-serving time rather than an additive cheap path; supported by the Monte-Carlo compute-duration logs and flat request rate, but not by a direct CPU-attribution metric.

### H5: An unenumerated config change on pricing-engine/order-service (or an uncaptured pricing-engine code deploy) is the true trigger
A second config/flag change landing at ~12:04-12:05Z on pricing-engine or order-service — captured in the audit shard that never returned — or a pricing-engine code deploy missed by the deployment query, caused the CPU saturation, separate from the greeks_v2 ramp.

**Causal chain**
1. The first all-services config audit returned partial (shard 2/3 did not respond), so changes could not be fully enumerated. [F2]
2. ASPECT UNVERIFIED: a second pricing-engine/order-service config change or an uncaptured code deploy landed at ~12:04-12:05Z. *(assumed)*
3. That change raised pricing-engine compute cost and pinned CPU, producing the latency/timeout step. *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F19` ✔ Across all services the ONLY config change in 11:30-13:59Z is cfg-3120: pricing-engine flags.pricing.greeks_v2.rollout_pct ramped 5->100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'). No second config/flag change exists on any service, so no OTHER flag landed at 12:04-12:05Z.
  - `F24` ✔ The pricing-engine config-change list (scope pricing-engine, 11:30-13:59Z) contains exactly one entry, cfg-3120, and is complete (partial:false) — the previously partial/empty audit shard is now retrieved.
  - `F22` ✔ No config change of any kind occurred after 12:04:30Z in the window: all-services query from 12:04:30Z-13:59Z returns count:0, confirming the 5->100 ramp at 12:04:00Z is the last config event before the incident window ends.
  - `F23` ✔ No code deploy landed on pricing-engine or order-service in 11:30-13:59Z (both return count:0), so H5's 'pricing-engine code deploy' alternative has no supporting event; the only deploy in the window remains market-data-service v5.2.0 at 11:56Z (F1).
**Assumptions**
  - The retried, now-complete audit shards (F24 pricing-engine scope partial:false, F19 all-services single change, F22 no change after 12:04:30Z) could still have missed a change on a service whose shard was never retrieved.
  - A pricing-engine code deploy is essentially excluded: the all-services deployment query (F1/F23) shows exactly one deploy (market-data-service) in the window.

### H4: Coincidental/pre-existing pricing-engine capacity saturation at a daily peak (no faulty change on the quote path)
pricing-engine tipped into CPU saturation at ~12:05Z independently of the 11:56Z deploy and the 12:04Z flag ramp — e.g. a daily-peak load event like HIST-113 — and the near-simultaneous onset with cfg-3120 is coincidence.

**Causal chain**
1. pricing-engine has a historical precedent of CPU saturation producing quote timeouts (HIST-113, market open, cpu 99%). [F6]
2. ASPECT UNVERIFIED: pricing-engine had a pre-existing CPU/capacity trend before 12:04Z. *(assumed)*
3. CPU pins at ~99% and p99 rises to 2.6s, causing order-service quote timeouts. [F13, F8, F7]

**Supporting evidence**
  - `F6` ✔ Historical precedents for 'quote timeout'/pricing latency: HIST-113 (2026-01-28, pricing-engine CPU saturation at market open; quote timeouts, cpu 99%; root cause capacity_saturation), HIST-107 (2025-12-09, cold cache storm after redis failover; redis-cache+market-data-service; 'cache miss storm','quote latency'; infrastructure_failure), HIST-110 (2026-01-15, order latency/statement timeouts via ledger-db IOPS throttling). No past incident is recorded as caused by a serialization/payload-format change.
**Contradicting evidence**
  - `F14` ✔ pricing-engine inbound request_rate is FLAT across the onset: 638.32 req/s (11:58Z) -> 640.29 (12:05Z) -> ~631-662 for the rest of the window (min 571.46, max 741.12). There is no load/traffic surge driving the CPU saturation — CPU rose ~2.5x while request volume stayed constant.
  - `F8` ✔ pricing-engine latency_p99_ms was flat at ~56-64ms through the 12:01Z bucket (avg 57.99, max 58.97), then jumped in the 12:04Z bucket (avg 1705.61, max 2534.9) and reached full impact at 12:07Z (avg 2633.16, max 2849.25), staying ~2600ms for the rest of the window. The 420s-bucket view put the step at '12:05:00Z', but the 180s zoom shows the rise begins inside the 12:04-12:07 bucket.
  - `F18` ✔ The pricing-engine onset (CPU pin, p99 2.6s, 5xx step, all in the 12:04-12:07Z window per F8/F11) coincides to within bucket resolution with the flags.pricing.greeks_v2.rollout_pct 5->100 ramp at 12:04Z (F2), whereas the market-data-service serialization deploy was 8 min earlier at 11:56Z (F1) and market-data-service telemetry is flat (F10). This favors a pricing-engine-internal compute change over an upstream payload/latency cause.
  - `F2` ✔ Config change on pricing-engine: feature flag flags.pricing.greeks_v2.rollout_pct ramped from 5 to 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic') -- 8 minutes after the market-data-service deploy and ~12 min before the 12:16Z report. (First query returned partial:true, 0 changes; retry on full shards returned this.)
**Assumptions**
  - Would require pricing-engine CPU to have been creeping up before 12:04Z; it is flat ~40% until the 12:04-12:07Z step, and request_rate is flat (F14), so there is no capacity/load driver.

### H2: market-data-service v5.2.0 protobuf serialization change bloats/destabilizes quote payloads consumed by pricing-engine
The 11:56Z deploy tc-001 (market-data-service v5.2.0, 'Switch quote serialization to protobuf') changed the wire format/size of quote payloads; downstream pricing-engine deserializes or processes the new format inefficiently, driving CPU high and quote calls past the 2500ms timeout.

**Causal chain**
1. market-data-service v5.2.0 'Switch quote serialization to protobuf' deployed at 11:56Z, the only deployment in the 11:30-13:59Z window, 8 min before onset. [F1, F23]
2. The quote path traverses order-service -> pricing-engine -> market-data-service, so market-data-service's payload format is on the quote path and could affect pricing-engine's deserialization cost. [F3]
3. ASPECT UNVERIFIED: the new protobuf payloads are larger or costlier to parse, raising pricing-engine CPU and latency. *(assumed)*
4. Resulting pricing-engine slowness pushes order-service quote calls past 2500ms. [F7]

**Supporting evidence**
  - `F1` ✔ Across ALL services in 11:30Z-13:59Z there was exactly ONE deployment: market-data-service v5.2.0 'Switch quote serialization to protobuf' at 2026-05-20T11:56:00Z by v.rao, status 'live', rolled_back_at null (no rollback).
  - `F3` ✘ unverified Quote path topology: api-gateway -> order-service -> pricing-engine (http) -> market-data-service (http); order-service also calls ledger-db (db), exchange-gateway (http) and kafka (queue-produce). So order-service's quote step traverses order-service -> pricing-engine -> market-data-service to reach quotes.
**Contradicting evidence**
  - `F10` ✔ market-data-service is healthy across the whole window: latency_p99_ms stays 39-41ms (12:00Z avg 39.46, 12:10Z avg 39.34), error_rate stays 0.16-0.25% with no step (12:00Z avg 0.2, 12:10Z avg 0.18), and request_rate stays ~930-985 req/s with no throughput dip. The only WARN in its log window is a single 'deprecated TLS cipher offered by client e0942b8a' at 11:43:53Z, unrelated to the incident.
  - `F12` ✔ The incident report's claim that market-data-service 'shipped a serialization change a few minutes before' is not corroborated by any telemetry: market-data-service shows no error, latency or throughput deviation at any point in 11:30-13:59Z, and its request_rate entering the incident window (12:00Z avg 938.08) is unchanged from baseline (11:30Z avg 929.09).
  - `F8` ✔ pricing-engine latency_p99_ms was flat at ~56-64ms through the 12:01Z bucket (avg 57.99, max 58.97), then jumped in the 12:04Z bucket (avg 1705.61, max 2534.9) and reached full impact at 12:07Z (avg 2633.16, max 2849.25), staying ~2600ms for the rest of the window. The 420s-bucket view put the step at '12:05:00Z', but the 180s zoom shows the rise begins inside the 12:04-12:07 bucket.
  - `F13` ✔ pricing-engine CPU_pct steps from ~40% to full saturation in the same 12:05Z bucket as its latency p99 rise: 39.89 (11:58Z) -> 99.11 avg / 100.0 max (12:05Z), then stays 98.6-99.3 avg through 13:57Z. It is a sustained pinned-CPU plateau, not a spike.
  - `F14` ✔ pricing-engine inbound request_rate is FLAT across the onset: 638.32 req/s (11:58Z) -> 640.29 (12:05Z) -> ~631-662 for the rest of the window (min 571.46, max 741.12). There is no load/traffic surge driving the CPU saturation — CPU rose ~2.5x while request volume stayed constant.
  - `F18` ✔ The pricing-engine onset (CPU pin, p99 2.6s, 5xx step, all in the 12:04-12:07Z window per F8/F11) coincides to within bucket resolution with the flags.pricing.greeks_v2.rollout_pct 5->100 ramp at 12:04Z (F2), whereas the market-data-service serialization deploy was 8 min earlier at 11:56Z (F1) and market-data-service telemetry is flat (F10). This favors a pricing-engine-internal compute change over an upstream payload/latency cause.
  - `F11` ✔ pricing-engine p99 rise and order-service quote timeouts appear simultaneous at the resolution available: both step in the 12:04-12:07Z window, with the first order-service timeout at 12:06:10Z and the pricing-engine 12:04Z bucket already partially elevated (avg 1705.61 vs 57.99 at 12:01Z). No pre-12:04 elevation is visible in either service, so neither signal precedes the other beyond bucket resolution.
  - `F25` ✔ pricing-engine's ERROR log in 12:04-12:30Z has exactly ONE signature, seen 56 times (all ERROR): 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2', first at 12:06:41Z, last at 12:29:37Z. It is an application-level deadline exceeded while COMPUTING the quote (2s budget), not an exception stack trace and not a downstream-call timeout/refusal.
  - `F28` ✔ There are NO downstream/latency/refusal signatures on pricing-engine: a contains='timeout' search over WARN+ returned 0 matches and contains='refused' returned 0 matches in 12:04-12:30Z (full shards, partial=false, data_range present). The only slowdowns are internal Compute-classed mechanics; no redis-cache / market-data-service / DB call timeout or connection-refused appears.
  - `F29` ✔ The Greeks v2 flag ramp lands as an INFO config line on pricing-engine itself: 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100' at 2026-05-20T12:04:16Z — 65 seconds before the first GreeksV2Calculator slow-compute WARN (12:05:21Z) and matching F2's 12:04Z ramp to 100.
**Assumptions**
  - No payload-size/bytes metric exists to check the bloat claim, and no consumer-side inbound-bytes evidence was obtained.
  - A serialization-parse problem would be expected to show downstream-call latency/refusal signatures on pricing-engine; none exist (F28), and the failure text is an in-process compute deadline, not a deserialization error (F25).

### H3: Shared redis-cache degradation (cache-miss storm) stalls pricing-engine and market-data-service
The shared redis-cache (shared=true, used by both pricing-engine and market-data-service) degraded — e.g. elevated miss rate or latency — so pricing-engine blocks or recomputes, exhausting CPU and pushing latency to 2.6s, while the same cache backs market-data-service.

**Causal chain**
1. pricing-engine and market-data-service both depend on the shared redis-cache (owner platform). [F4]
2. ASPECT UNVERIFIED: redis-cache latency or hit ratio degraded at ~12:04-12:05Z. *(assumed)*
3. Cache stalls force pricing-engine to recompute or block, pinning CPU and raising p99 to ~2.6s, yielding order-service quote timeouts. [F13, F8, F7]

**Supporting evidence**
  - `F4` ✔ pricing-engine additionally depends on shared redis-cache (kind cache), and market-data-service depends on redis-cache (cache) AND exchange-gateway (http). redis-cache is marked shared=true (owner platform), so market-data-service and pricing-engine share that cache component.
  - `F6` ✔ Historical precedents for 'quote timeout'/pricing latency: HIST-113 (2026-01-28, pricing-engine CPU saturation at market open; quote timeouts, cpu 99%; root cause capacity_saturation), HIST-107 (2025-12-09, cold cache storm after redis failover; redis-cache+market-data-service; 'cache miss storm','quote latency'; infrastructure_failure), HIST-110 (2026-01-15, order latency/statement timeouts via ledger-db IOPS throttling). No past incident is recorded as caused by a serialization/payload-format change.
**Contradicting evidence**
  - `F17` ✔ Shared downstream redis-cache (used by both pricing-engine and market-data-service, F4) is healthy across the window: cpu_pct flat ~21.5-22.5% (11:58Z avg 22.36 -> 12:05Z avg 22.45 -> 13:57Z avg 22.07, min 19.53, max 24.13). It exposes only connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct — it has no latency metric, so I could not measure cache response latency directly.
  - `F10` ✔ market-data-service is healthy across the whole window: latency_p99_ms stays 39-41ms (12:00Z avg 39.46, 12:10Z avg 39.34), error_rate stays 0.16-0.25% with no step (12:00Z avg 0.2, 12:10Z avg 0.18), and request_rate stays ~930-985 req/s with no throughput dip. The only WARN in its log window is a single 'deprecated TLS cipher offered by client e0942b8a' at 11:43:53Z, unrelated to the incident.
  - `F14` ✔ pricing-engine inbound request_rate is FLAT across the onset: 638.32 req/s (11:58Z) -> 640.29 (12:05Z) -> ~631-662 for the rest of the window (min 571.46, max 741.12). There is no load/traffic surge driving the CPU saturation — CPU rose ~2.5x while request volume stayed constant.
  - `F25` ✔ pricing-engine's ERROR log in 12:04-12:30Z has exactly ONE signature, seen 56 times (all ERROR): 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2', first at 12:06:41Z, last at 12:29:37Z. It is an application-level deadline exceeded while COMPUTING the quote (2s budget), not an exception stack trace and not a downstream-call timeout/refusal.
  - `F28` ✔ There are NO downstream/latency/refusal signatures on pricing-engine: a contains='timeout' search over WARN+ returned 0 matches and contains='refused' returned 0 matches in 12:04-12:30Z (full shards, partial=false, data_range present). The only slowdowns are internal Compute-classed mechanics; no redis-cache / market-data-service / DB call timeout or connection-refused appears.
  - `F29` ✔ The Greeks v2 flag ramp lands as an INFO config line on pricing-engine itself: 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100' at 2026-05-20T12:04:16Z — 65 seconds before the first GreeksV2Calculator slow-compute WARN (12:05:21Z) and matching F2's 12:04Z ramp to 100.
**Assumptions**
  - redis-cache exposes no latency metric and hit_ratio_pct/evictions_per_min were never queried (tool reliability: tc-024 latency query FAILED not_found), so a network/backend-latency cache stall at flat CPU cannot be fully excluded. This is under-tested but has zero positive evidence.
  - HIST-107 (cold cache storm) is structural similarity only, not evidence about this event.
  - A cache-driven stall would produce downstream blocking/timeout signatures on pricing-engine; there are none, and the only error is an in-process compute deadline (F25/F28).

## Facts (verified observations)
- The only deployment across all services in 11:30Z-13:59Z was market-data-service v5.2.0 'Switch quote serialization to protobuf' at 11:56:00Z, with no rollback. [F1]
- The only config change across all services in 11:30Z-13:59Z was cfg-3120: pricing-engine flags.pricing.greeks_v2.rollout_pct ramped 5->100 at 12:04:00Z; the pricing-engine audit scope is complete and contains exactly this one entry, and there were zero changes on order-service. [F2, F19, F20, F24]
- No config change of any kind occurred after 12:04:30Z, and there are no pricing-engine or order-service code deploys in the window. [F22, F23]
- The greeks_v2 flag was never ramped down or reverted after the 12:04Z ramp. [F21]
- pricing-engine logged the config application on itself: 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100' at 12:04:16Z, 65s before the first slow-compute WARN. [F29]
- pricing-engine's only ERROR signature in 12:04-12:30Z is 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (56 occurrences, all ERROR), an in-process compute deadline rather than a downstream-call timeout or exception trace. [F25, F26]
- pricing-engine WARN logs show the mechanism as slow in-process compute: 'GreeksV2Calculator: Monte-Carlo paths=20000 took <n>ms' with durations of 6465-8942ms, first at 12:05:21Z, ~80s before the first ERROR. [F27]
- There are zero downstream timeout or connection-refused signatures on pricing-engine in 12:04-12:30Z (full shards), so no dependency/pool rejection is observed. [F28]
- pricing-engine CPU_pct stepped from ~39.89% to 99.11% avg / 100% max in the 12:05Z bucket and stayed pinned (98.6-99.3% avg) through 13:57Z. [F13]
- pricing-engine inbound request_rate stayed flat across onset (638.32 req/s at 11:58Z -> 640.29 at 12:05Z), so no load surge drove the CPU saturation. [F14]
- pricing-engine memory_pct stayed flat ~48-50% and restarts stayed 0.0 for all 22 buckets, indicating a CPU-bound rather than memory-pressure or crash-loop failure. [F15]
- pricing-engine latency_p99_ms was flat at ~56-64ms through 12:01Z, jumped in the 12:04Z bucket (avg 1705.61, max 2534.9) and reached ~2600ms by 12:07Z, holding there for the rest of the window. [F8]
- pricing-engine error_rate stepped from ~0.2% to ~12.4% at the 12:05Z bucket and held ~12.0-12.9% through the window, i.e. calls are mostly slow and roughly one in eight fails. [F16, F30]
- order-service quote timeouts began at 12:06:10Z with 'pricing-engine quote call timed out after 2500ms orderId=4bd44879'; 225 of 227 matched log lines are this identical timeout pattern between 12:06:10Z and 13:58:24Z, with no 5xx or connection-refused signatures in the WARN+ set. [F7]
- order-service error_rate (HTTP 5xx % of requests) stepped from ~0.18-0.22% to 6.93% at 12:05Z and held ~8.1-8.3% through 13:57Z. [F9]
- market-data-service is healthy across the whole window: p99 39-41ms, error_rate 0.16-0.25% with no step, request_rate ~930-985 req/s with no throughput dip. [F10]
- The report's claim that market-data-service 'shipped a serialization change a few minutes before' is not corroborated by telemetry; its request_rate entering the incident window is unchanged from baseline. [F10, F1]
- The shared redis-cache (used by both pricing-engine and market-data-service) is healthy across the window: cpu_pct flat ~21.5-22.5%. [F17, F4]

## Assumptions (not verified)
- The 5%->100 ramp actually changed the code path taken per request (the flag was not already effectively 100% and is evaluated per request). No per-request flag-arm/rollout_pct tag is attached to request lines, so the pre-12:04 vs post-12:04 routing split cannot be quantified from logs - the evidence is indirect (config INFO line plus GreeksV2Calculator WARNs appearing only in this window).
- The compute is CPU-bound at request-serving time rather than an additive cheap path; supported by the Monte-Carlo compute-duration logs, flat request rate and flat memory, but not by a direct per-request CPU-attribution metric.
- A blocking mechanism (thread-pool/queue/connection-pool exhaustion or dependency blocking) producing the same CPU pin cannot be excluded by measurement, since pricing-engine exposes no pool/queue/dependency metrics; it is excluded only by the absence of pool/downstream rejection signatures and by the explicit compute-deadline error text.
- No revert of the flag was attempted or observed, so 'rollback without recovery' could not be used as a discriminating test; the flag staying live is consistent with, but does not by itself prove, causality.
- Config/flag enumeration completeness relies on the retried audit shards; one shard (2/3) was initially never retrieved, though the retries returned complete results for pricing-engine and all services.
- (demoted from facts: no verified observation backs it) pricing-engine p99 rise and order-service quote timeouts step in the same 12:04-12:07Z bucket, with no pre-12:04 elevation in either, so the pricing-engine onset coincides with the 12:04Z flag ramp while the 11:56Z market-data-service deploy precedes it by 8 minutes.
- (demoted from facts: no verified observation backs it) The quote path is api-gateway -> order-service -> pricing-engine -> market-data-service, so order-service's quote step traverses pricing-engine.

## Timeline
- `2026-05-20T11:56:00Z` Only deploy in the window: market-data-service v5.2.0 'Switch quote serialization to protobuf' (tc-001), status live, never rolled back. [F1]
- `2026-05-20T12:04:00Z` Config change cfg-3120 (tc-011) ramps pricing-engine flags.pricing.greeks_v2.rollout_pct from 5 to 100 by pm-experiments. [F2, F19]
- `2026-05-20T12:04:16Z` pricing-engine logs 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100'. [F29]
- `2026-05-20T12:04:00Z-12:07:00Z` pricing-engine latency p99 steps from ~58ms to 1705.61 avg (max 2534.9) in the 12:04Z bucket and reaches ~2600ms by 12:07Z; CPU steps from ~40% to ~99% in the 12:05Z bucket; error_rate steps to ~12.4% while request_rate stays flat. [F8, F13, F14, F16, F30, F11]
- `2026-05-20T12:05:21Z` First GreeksV2Calculator slow-compute WARN: 'Monte-Carlo paths=20000 took <n>ms' (6465-8942ms range); earliest log signal of the incident. [F27]
- `2026-05-20T12:05:00Z` order-service error_rate steps from ~0.2% to 6.93%, rising to ~8.1-8.3%. [F9]
- `2026-05-20T12:06:10Z` First order-service quote timeout: 'pricing-engine quote call timed out after 2500ms orderId=4bd44879'. [F7]
- `2026-05-20T12:06:41Z` First pricing-engine ERROR: 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2'. [F25, F26]
- `2026-05-20T12:16:00Z` Incident INC-011 reported as SEV-2, order-service quote timeouts, reported service order-service. [F7, F9]
- `2026-05-20T12:30:00Z` Verification pass: zero downstream timeout or refused signatures on pricing-engine; market-data-service and redis-cache flat; no further config change after 12:04:30Z; flag never reverted. [F28, F10, F17, F22, F21]

## Recommended actions
- **[immediate]** Immediately ramp the flag back down: apply a pricing-engine config change reverting flags.pricing.greeks_v2.rollout_pct from 100 to 5 (or 0), i.e. roll back cfg-3120 (tc-011). -- The onset coincides within bucket resolution with the 12:04Z 5->100 ramp and the failures are explicitly tagged calculator=GreeksV2; the flag is still live and was never reverted, so reverting removes the CPU-bound Monte-Carlo path from the quote request flow. [F2, F19, F21, F25, F26, F27, F8, F13]
- **[immediate]** After reverting the flag, verify recovery on the leading indicators: pricing-engine CPU_pct should fall from ~99% back toward ~40% and latency_p99_ms from ~2600ms toward ~60ms, pricing-engine error_rate from ~12.4% to ~0.2%, and order-service quote-timeout logs and 5xx (currently ~8.1-8.3%) should stop. -- These specific metrics are the observed step-change signals; their return to pre-12:04 baselines confirms the flag is the operative cause and bounds the mitigation. [F13, F8, F16, F30, F9, F7]
- **[immediate]** Do NOT roll back or hotfix market-data-service v5.2.0 (tc-001) on the basis of this incident; if it is rolled back for other reasons, that must not be treated as the fix. -- market-data-service is flat on latency, error and throughput across the whole window and redis-cache is flat, contradicting the serialization-payload hypothesis that the reporter named. [F10, F12, F17, F1]
- **[short_term]** Short term: right-size or reimplement the GreeksV2 calculator before any re-ramp - reduce Monte-Carlo path count (currently 20000 paths, 6465-8942ms observed) or move it off the request-serving path (async/precompute/cache), then re-run a canary at low rollout_pct with the 2000ms budget and per-request flag-arm logging enabled. -- The measured per-request compute cost (6.5-8.9s) is far over the 2000ms application budget; the path must be made to fit budget or moved off the synchronous quote call before traffic is routed to it again. [F27, F25, F26]
- **[short_term]** Short term: add per-request flag-evaluation/experiment-arm tags (calculator + rollout_pct) to pricing-engine request and error logs, and a mechanism to gate a flag ramp on live budget/latency health. -- The 5%->100 routing split could not be quantified from logs and the ramp to 100% was applied with no evident latency guard, which is what allowed a budget-blowing path to take 100% of traffic. [F2, F29, F26]
- **[short_term]** Short term: add thread-pool/queue/connection-pool and per-dependency metrics to pricing-engine so pool-exhaustion mechanisms can be measured rather than inferred from log absence. -- pricing-engine exposes only cpu_pct, error_rate, latency_p99_ms, memory_pct, request_rate and restarts, so blocking/pool mechanisms can only be excluded by absence of rejection signatures. [F28, F15, F17]
- **[long_term]** Long term: require load/perf budgeting of any new compute-heavy code path and staged flag ramps (e.g. 5%->25%->50%->100% with automatic abort on latency budget breach) for experiments touching the quote request path. -- Prevents recurrence of a single unguarded flag ramp routing all quote traffic onto a path whose measured cost (6.5-8.9s) exceeds the 2s/2.5s budgets. [F2, F27, F8, F9]
- **[long_term]** Long term: close the config-audit coverage gap (the 2/3 shard that initially did not respond) and carry the reverse dependency mapping for shared redis-cache so future investigations can enumerate all changes and blast radius deterministically. -- Both gaps limited confidence in the enumeration of config changes and in excluding a second change or a shared-component failure during this incident. [F24, F19, F4]

## Data gaps
- Config-change audit for 11:30Z-13:59Z initially returned partial ('shard 2/3 did not respond'); the retried shards were complete for pricing-engine and all-services (only cfg-3120), but the initially missing shard means an additional config/flag change on other services cannot be fully excluded from the first pass.
- Reverse/upstream dependency query for redis-cache failed (budget_exhausted), so the full set of services co-using the shared cache beyond the two known edges is unconfirmed.
- pricing-engine exposes only six metrics (cpu_pct, error_rate, latency_p99_ms, memory_pct, request_rate, restarts) - no GC, heap, thread-pool, queue-depth, connection-pool or per-dependency metrics - so pool/queue exhaustion is excluded only by absence of rejection log signatures, not measured directly.
- redis-cache exposes no latency metric (latency_p99_ms query returned not_found) and hit_ratio_pct/evictions_per_min were never queried, so a network/backend-latency cache stall cannot be fully excluded, though cpu_pct is flat and there is zero positive evidence for it.
- order-service logs returned only the earliest 30 of 227 matches (truncated), so the full error mix and the tail of the incident could not be characterised beyond the dominant 2500ms-timeout signature.
- No payload-size/bytes-per-request metric exists, so the 'payload-size shift from market-data-service' correlation could not be tested directly.
- No separate experiment-arm/rollout_pct tag is attached to individual request lines, so the pre-12:04 (5%) vs post-12:04 (100%) routing split cannot be quantified from logs.
- No revert was attempted, so 'rollback restores CPU' was not available as a discriminating test of the flag's causal role.
- Blast radius beyond order-service (other pricing-engine or market-data-service consumers) was not enumerated or queried.

## What would change this conclusion
- Reverting flags.pricing.greeks_v2.rollout_pct to 5 (or 0) without pricing-engine CPU_pct / latency_p99_ms / error_rate recovering would falsify H1 as the operative cause.
- Evidence that the flag was already effectively at 100% before 12:04Z (so the ramp changed nothing on the request path) would remove the trigger and reopen H5/H4.
- Finding a second config change or pricing-engine code deploy at ~12:04-12:05Z in a previously unretrieved audit shard would promote H5.
- Direct evidence of pricing-engine thread-pool/queue/connection-pool exhaustion or a blocked dependency call at onset (e.g. pool-wait metrics or rejection signatures) would shift the mechanism away from a CPU-bound compute path.
- Evidence that pre-12:04 (5%) traffic was also routed to GreeksV2 with the same slow compute (e.g. GreeksV2Calculator WARNs before 12:04) would break the cause-before-effect ordering.
- Emergence of a downstream timeout/refusal signature on pricing-engine or degradation of redis-cache/market-data-service during onset would revive H2/H3.

## Integrity warnings (set by code)
- fact demoted to assumption: pricing-engine p99 rise and order-service quote timeouts step in the same 12:04-
- fact demoted to assumption: The quote path is api-gateway -> order-service -> pricing-engine -> market-data-

## Investigation stats
- latency 172.28s, LLM calls 23, tokens 174370 (in 143615 / out 30755)
- tool calls 38 (1 failed, 1 partial, 0 recovered by retry), findings 29/30 verified
