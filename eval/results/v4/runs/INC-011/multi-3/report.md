# Incident investigation report: INC-011
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause (confidence: medium, 0.62): the pricing-engine experiment flag cfg-3120 — flags.pricing.greeks_v2.rollout_pct ramped 5 -> 100 at 2026-05-20T12:04:00Z — enabled a CPU-heavy code path for all traffic, driving pricing-engine CPU from ~38-40% to 98-100% and p99 latency from ~60ms to ~2534ms in the very next 60s bucket, which pushed order-service quote calls past their 2500ms timeout from 12:06:10Z onward. The fault originates in pricing-engine's compute cost under the fully-rolled-out flagged code path; the fix target is reverting/disengaging cfg-3120 or fixing that path. Alternatives are excluded by positive evidence, not dismissal: H2 (market-data-service protobuf deploy at 11:56Z) by pricing-engine CPU/latency being flat at baseline for the 15 minutes spanning that deploy and stepping only at 12:05 plus market-data-service emitting zero WARN+ logs; H3 (redis-cache) by healthy/flat hit-ratio, CPU, evictions and clients at onset; H4 (capacity/organic load) by flat request_rate across onset. The one unproven hop is the mechanism detail that the Greeks v2 path is materially more CPU-intensive and behaves near-on/off at full rollout — this caps confidence, and the ~2h no-recovery/no-revert signature is treated as part of the same origin rather than a separate one.

## Probable root cause
**Greeks v2 feature-flag ramp to 100% saturates pricing-engine CPU (with persistent-cost mechanism folded in)** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.62, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Greeks v2 feature-flag ramp to 100% saturates pricing-engine CPU (with persistent-cost mechanism folded in) | config_change | pricing-engine | cfg-3120 | 0.62 (medium) | active |
| 2 | H5: Persistent post-ramp state: flag-ramp change but no self-recovery (mechanically incomplete H1) | config_change | pricing-engine | cfg-3120 | 0.34 (low) | active |
| 3 | H2: market-data-service protobuf change (dep-6111) slowed the pricing path | bad_deployment | market-data-service | dep-6111 | 0.10 (low) | active |
| 4 | H3: Shared redis-cache degradation stalls the quote path | infrastructure_failure | redis-cache | null | 0.02 (low) | rejected |
| 5 | H4: pricing-engine CPU saturation from organic market-open load (capacity) | capacity_saturation | pricing-engine | null | 0.02 (low) | rejected |

### H1: Greeks v2 feature-flag ramp to 100% saturates pricing-engine CPU (with persistent-cost mechanism folded in)
Config change cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 on pricing-engine at 2026-05-20T12:04:00Z, enabling the CPU-heavy Greeks v2 computation for all traffic. pricing-engine CPU steps 38%->98.65% and p99 steps ~60ms->2534ms in the very next 60s bucket (12:05:00Z). Order quotes served by pricing-engine then exceed order-service's 2500ms timeout, producing continuous quote timeouts from 12:06:10Z. Fault originates in pricing-engine's compute cost under the newly fully-rolled-out code path, triggered by the flag change. The persistence detail (flat 98-100% CPU and flat ~2.6s p99 with no downslope and no visible revert through 13:59Z) is treated as an assumption about the flagged code path's cost profile (self-sustaining/near-on-off at full rollout), not as a separate origin: same origin, same fix target (revert/disengage cfg-3120 or fix the flagged path), so it is not split into a competing hypothesis.

**Causal chain**
1. cfg-3120 ramps flags.pricing.greeks_v2.rollout_pct 5->100 on pricing-engine at 12:04:00Z, 12 min before the report [F2, F8]
2. Greeks v2 path becomes active for 100% of traffic and is materially more CPU-expensive (and cost does not scale linearly back down with partial rollout; near-on/off at full rollout) *(assumed)*
3. pricing-engine CPU saturates 38%->98.65% in the 12:05:00Z bucket, one 60s bucket after the ramp [F16, F32]
4. pricing-engine p99 steps from ~60ms to ~2500-2850ms at 12:05:00Z, one minute before the first order-service timeout [F15, F33]
5. Saturation is CPU-bound: memory flat ~46-53% [F17]
6. order-service -> pricing-engine (http) quote call exceeds the 2500ms timeout; first error 12:06:10Z, step-change not ramp, continuous to 13:58:24Z [F13, F14, F3, F10]
7. Saturation persists with no recovery and no revert and restarts=0 through 13:59Z (consistent with flag remaining applied; the flagged path's cost is sustained) [F25, F26, F27, F28, F30]

**Supporting evidence**
  - `F2` ✔ Config audit (full, retried) shows cfg-3120 on pricing-engine: key 'flags.pricing.greeks_v2.rollout_pct' changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic') - 12 minutes before the incident onset.
  - `F8` ✔ Exactly one config change occurred across all services in the window: cfg-3120 on pricing-engine, flag flags.pricing.greeks_v2.rollout_pct ramped from 5 to 100 by pm-experiments at 2026-05-20T12:04:00Z ('Ramp Greeks v2 experiment to all traffic') — 12 minutes before the 12:16Z report.
  - `F15` ✔ pricing-engine p99 latency steps from ~57-62ms to ~2500-2850ms at 12:05:00Z, one minute before the first order-service timeout log. 60s buckets: 12:00=60.06, 12:01=58.97, 12:02=58.17, 12:03=56.82, 12:04=60.53, then 12:05=2534.9 and sustained ~2410-2849 through 12:20+.
  - `F16` ✔ pricing-engine CPU saturates at the same instant as the latency jump: cpu_pct is 38-42% through 12:04 (12:04=38.22), then 12:05=98.65, 12:06=99.25, 12:08=100.0, and stays 98-100% through 12:20. This is a hard saturation coincident with onset, not a pre-existing trend.
  - `F17` ✔ pricing-engine memory_pct stays flat ~46-53% across the onset (11:55-12:20, min 46.04, max 52.54), so the pricing-engine failure is CPU-bound, not a memory/leak symptom.
  - `F13` ✔ order-service quote timeouts begin at 12:05-12:06:10Z: the first ERROR log is 'pricing-engine quote call timed out after 2500ms orderId=4bd44879' at 2026-05-20T12:06:10Z, and all 225 matched ERROR entries are the same pattern 'pricing-engine quote call timed out after <n>ms orderId=<n>'. The error onset is step-change, not ramped: errors run continuously from 12:06:10 to 13:58:24.
  - `F14` ✔ order-service error_rate jumps abruptly in the 12:00 10-min bucket: baseline 11:30-11:50 buckets are 0.2, 0.21, 0.17 avg (max <=0.32), then 12:00 bucket avg 3.38/max 8.37 and 12:10 onward plateaus at ~8.1-8.3 avg (max ~8.87) through 13:50. The failure is a step change with no gradual ramp.
  - `F3` ✔ order-service's only path to the changed quote serialization is order-service -> pricing-engine (http) -> market-data-service (http); pricing-engine is a direct, non-shared dependency of order-service and market-data-service is not a direct dependency of order-service.
  - `F10` ✔ Quote-path dependency chain (edge direction caller -> callee): order-service -> pricing-engine (http) -> market-data-service (http); market-data-service -> exchange-gateway (http) -> exchange-api (external). order-service also calls ledger-db (db), exchange-gateway (http) and kafka (queue-produce).
  - `F5` ✔ Precedents for quote-timeout/pricing-path symptoms: HIST-113 (pricing-engine CPU saturation at market open, symptoms 'quote timeouts','cpu 99%', category capacity_saturation); HIST-107 (redis failover, services redis-cache+market-data-service, symptoms 'cache miss storm','quote latency'); HIST-110 (order-service+ledger-db IOPS throttling, 'order latency','slow queries').
  - `F32` ✔ pricing-engine cpu_pct is flat at baseline from 11:50Z through 12:04Z with no climb around the 11:56 deploy: 11:50=40.64, 11:51=41.74, 11:52=39.26, 11:53=38.7, 11:54=39.7, 11:55=39.03, 11:56=40.22, 11:57=40.35, 11:58=41.86, 11:59=38.03, 12:00=41.45, 12:01=39.39, 12:02=40.08, 12:03=40.18, 12:04=38.22 — every value in the 38-42% band. Then a hard step: 12:05=98.65, 12:06=99.25.
  - `F33` ✔ pricing-engine latency_p99_ms is flat at ~57-63ms from 11:50Z through 12:04Z with no climb at 11:56: 11:56=56.86, 11:57=60.52, 11:58=60.41, 11:59=57.86, 12:00=60.06, 12:01=58.97, 12:02=58.17, 12:03=56.82, 12:04=60.53. Then a hard step: 12:05=2534.9, 12:06=2521.4.
  - `F34` ✔ pricing-engine error_rate stays at baseline (~0.13-0.29%) from 11:54Z through 12:05Z, including the entire post-deploy stretch 11:56-12:05 (11:56=0.21, 11:57=0.22, 11:58=0.14, 11:59=0.13, 12:00=0.18, 12:01=0.2, 12:02=0.17, 12:03=0.25, 12:04=0.29, 12:05=0.15), then jumps to 12.98 at 12:06.
  - `F36` ✔ VERDICT: flat-then-step at 12:05, NOT a climb from 11:56. CPU and latency are flat at baseline for 15 consecutive minutes spanning the 11:56 market-data-service deploy and step only at 12:05; because no earlier degradation begins near 11:56, this confirms H1 trigger timing (the 12:04 greeks_v2 ramp) and further undermines H2 instead of re-opening it. The sub-minute caveat applies: the true onset could sit anywhere inside the 12:04-12:05 or 12:05-12:06 60s bucket, but no climb is visible at 11:56 or later.
  - `F25` ✔ pricing-engine cpu_pct never recovered in 12:16-13:59Z: 180s buckets stay at ~98-100% throughout, summary min 97.76, max 100.0, mean 98.96; first bucket 12:16=99.42 and last 13:58=99.2. This remains the saturating level seen at onset (12:05), versus the ~40% baseline.
  - `F26` ✔ pricing-engine latency_p99_ms also never recovered: every 180s bucket from 12:16 to 13:58 is ~2391-2844ms (mean 2633.34), e.g. 12:16=2629.75 and 13:58=2620.47, versus ~57-62ms baseline. The p99 is flat across the whole window with no downslope.
  - `F28` ✔ pricing-engine restarts metric stayed 0 for the entire 12:16-13:59Z window (all points avg 0.0, summary min 0.0 max 0.0, missing_points 0), so no process restart/redeploy forced a rollback effect or cleared the saturation.
**Contradicting evidence**
  - `F25` ✔ pricing-engine cpu_pct never recovered in 12:16-13:59Z: 180s buckets stay at ~98-100% throughout, summary min 97.76, max 100.0, mean 98.96; first bucket 12:16=99.42 and last 13:58=99.2. This remains the saturating level seen at onset (12:05), versus the ~40% baseline.
  - `F26` ✔ pricing-engine latency_p99_ms also never recovered: every 180s bucket from 12:16 to 13:58 is ~2391-2844ms (mean 2633.34), e.g. 12:16=2629.75 and 13:58=2620.47, versus ~57-62ms baseline. The p99 is flat across the whole window with no downslope.
  - `F30` ✔ Because CPU and latency are flat-saturated and request_rate is flat, pricing-engine shows no recovery toward baseline in 12:16-13:59Z and no revert evidence exists in its logs — the H1-vs-H2 alignment test yields NO revert and NO recovery, i.e. no flag-revert/recovery alignment to implicate H1.
**Assumptions**
  - The newly enabled Greeks v2 code path is materially more CPU-intensive than Greeks v1
  - Its cost does not scale proportionally down with rollout_pct, or it engages a self-sustaining/hot loop at full rollout, which is why the flat 98-100% CPU shows no downslope (mechanism detail, not a separate origin)
  - The 12:04:00Z flag write propagated to pricing-engine within ~1 minute and drove the 12:05 CPU/latency step; sub-minute resolution is unavailable (F31), so this is inference-by-alignment at one 60s bucket
  - The flag remains applied (no revert) so the CPU cost stays applied continuously through 13:59Z; the absence of a revert record is inferred from zero pricing-engine log matches (F27) and restarts=0 (F28), not from the config-audit store

### H5: Persistent post-ramp state: flag-ramp change but no self-recovery (mechanically incomplete H1)
Same origin as H1 (cfg-3120 flag ramp at 12:04Z enabling an expensive code path) but acknowledging that the saturation shows NO recovery and NO revert for ~2 hours (F25/F26/F30, restarts=0 F28). For an ordinary feature-flag toggle whose cost scales with rollout_pct, restoring the flag would restore CPU; the flat 98-100% CPU and flat 2.6s p99 with no downslope indicates the new code path either introduced a self-sustaining state (a runaway/loop or a hot path that cannot complete) or is genuinely order-of-magnitude more expensive and gated purely as on/off at the 100% boundary. This framing treats the no-recovery signature as part of the originating defect triggered by the flag change rather than a discrepancy.

**Causal chain**
1. cfg-3120 ramps the greeks_v2 flag 5->100 at 12:04:00Z [F2, F8]
2. The newly 100%-active code path holds pricing-engine CPU at 98-100% indefinitely (no ramp-down), latent until full rollout [F16, F25]
3. p99 stays flat at ~2.4-2.85s with no downslope through 13:58Z; CPU mean 98.96, p99 mean 2633ms [F25, F26]
4. No feature-flag revert is visible in pricing-engine logs over 12:16-13:59Z and restarts=0 [F27, F28]
5. order-service quote timeouts continue from 12:06:10Z to 13:58:24Z [F13]

**Supporting evidence**
  - `F2` ✔ Config audit (full, retried) shows cfg-3120 on pricing-engine: key 'flags.pricing.greeks_v2.rollout_pct' changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic') - 12 minutes before the incident onset.
  - `F8` ✔ Exactly one config change occurred across all services in the window: cfg-3120 on pricing-engine, flag flags.pricing.greeks_v2.rollout_pct ramped from 5 to 100 by pm-experiments at 2026-05-20T12:04:00Z ('Ramp Greeks v2 experiment to all traffic') — 12 minutes before the 12:16Z report.
  - `F15` ✔ pricing-engine p99 latency steps from ~57-62ms to ~2500-2850ms at 12:05:00Z, one minute before the first order-service timeout log. 60s buckets: 12:00=60.06, 12:01=58.97, 12:02=58.17, 12:03=56.82, 12:04=60.53, then 12:05=2534.9 and sustained ~2410-2849 through 12:20+.
  - `F16` ✔ pricing-engine CPU saturates at the same instant as the latency jump: cpu_pct is 38-42% through 12:04 (12:04=38.22), then 12:05=98.65, 12:06=99.25, 12:08=100.0, and stays 98-100% through 12:20. This is a hard saturation coincident with onset, not a pre-existing trend.
  - `F25` ✔ pricing-engine cpu_pct never recovered in 12:16-13:59Z: 180s buckets stay at ~98-100% throughout, summary min 97.76, max 100.0, mean 98.96; first bucket 12:16=99.42 and last 13:58=99.2. This remains the saturating level seen at onset (12:05), versus the ~40% baseline.
  - `F26` ✔ pricing-engine latency_p99_ms also never recovered: every 180s bucket from 12:16 to 13:58 is ~2391-2844ms (mean 2633.34), e.g. 12:16=2629.75 and 13:58=2620.47, versus ~57-62ms baseline. The p99 is flat across the whole window with no downslope.
  - `F27` ✔ No revert/rollback signal for cfg-3120 or the greeks_v2 flag exists in pricing-engine logs: separate queries for 'flag', 'cfg-3120', 'greeks_v2', and 'rollback' over 12:16-13:59Z each returned total_matched 0, in a service whose log data range covers 08:00-13:59Z.
  - `F28` ✔ pricing-engine restarts metric stayed 0 for the entire 12:16-13:59Z window (all points avg 0.0, summary min 0.0 max 0.0, missing_points 0), so no process restart/redeploy forced a rollback effect or cleared the saturation.
  - `F13` ✔ order-service quote timeouts begin at 12:05-12:06:10Z: the first ERROR log is 'pricing-engine quote call timed out after 2500ms orderId=4bd44879' at 2026-05-20T12:06:10Z, and all 225 matched ERROR entries are the same pattern 'pricing-engine quote call timed out after <n>ms orderId=<n>'. The error onset is step-change, not ramped: errors run continuously from 12:06:10 to 13:58:24.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The absence of recovery is attributable to the flag remaining applied (no revert) rather than to the flag being irrelevant
  - The new code path is a persistent consume, not a bursty one

### H2: market-data-service protobuf change (dep-6111) slowed the pricing path
Deployment dep-6111 switched market-data-service quote serialization to protobuf at 11:56:00Z. The reporter suspects the changed serialization raised payload/CPU cost for a downstream consumer (pricing-engine), driving pricing-engine CPU/latency and thus order quote timeouts. Fault originates in market-data-service's v5.2.0 release; it stays applied (live, not rolled back), which would be consistent with the ~2h flat, non-recovering saturation. Kept active because its mechanism (protobuf slower for the downstream parser) was never directly tested — it is contradicted only indirectly.

**Causal chain**
1. dep-6111 market-data-service v5.2.0 'Switch quote serialization to protobuf' deployed 11:56:00Z, status live, rolled_back_at null [F1, F7]
2. protobuf serialization is more CPU-expensive for a downstream parser (pricing-engine) *(assumed)*
3. pricing-engine, downstream of market-data-service on the quote path, sees CPU and latency rise [F16, F15, F3, F10]
4. order-service quote timeouts follow from 12:06:10Z [F13, F14]

**Supporting evidence**
  - `F1` ✔ Only one deployment in the 10:00Z-13:59Z window across all services: dep-6111, market-data-service v5.2.0, 'Switch quote serialization to protobuf', deployed 2026-05-20T11:56:00Z by v.rao, status live, rolled_back_at null.
  - `F7` ✔ Exactly one deployment occurred across all services in 2026-05-20T11:00Z-13:59Z: dep-6111, market-data-service v5.2.0 'Switch quote serialization to protobuf' by v.rao at 2026-05-20T11:56:00Z, status 'live' with rolled_back_at null (20 minutes before the 12:16Z report).
  - `F3` ✔ order-service's only path to the changed quote serialization is order-service -> pricing-engine (http) -> market-data-service (http); pricing-engine is a direct, non-shared dependency of order-service and market-data-service is not a direct dependency of order-service.
  - `F10` ✔ Quote-path dependency chain (edge direction caller -> callee): order-service -> pricing-engine (http) -> market-data-service (http); market-data-service -> exchange-gateway (http) -> exchange-api (external). order-service also calls ledger-db (db), exchange-gateway (http) and kafka (queue-produce).
  - `F25` ✔ pricing-engine cpu_pct never recovered in 12:16-13:59Z: 180s buckets stay at ~98-100% throughout, summary min 97.76, max 100.0, mean 98.96; first bucket 12:16=99.42 and last 13:58=99.2. This remains the saturating level seen at onset (12:05), versus the ~40% baseline.
  - `F26` ✔ pricing-engine latency_p99_ms also never recovered: every 180s bucket from 12:16 to 13:58 is ~2391-2844ms (mean 2633.34), e.g. 12:16=2629.75 and 13:58=2620.47, versus ~57-62ms baseline. The p99 is flat across the whole window with no downslope.
**Contradicting evidence**
  - `F18` ✔ market-data-service emitted zero WARN-or-higher logs in the window 11:45-13:59Z (total_matched 0), despite the report attributing the change to it. Its data range covers 08:00-13:59Z, so this is not an out-of-range artifact.
  - `F15` ✔ pricing-engine p99 latency steps from ~57-62ms to ~2500-2850ms at 12:05:00Z, one minute before the first order-service timeout log. 60s buckets: 12:00=60.06, 12:01=58.97, 12:02=58.17, 12:03=56.82, 12:04=60.53, then 12:05=2534.9 and sustained ~2410-2849 through 12:20+.
  - `F16` ✔ pricing-engine CPU saturates at the same instant as the latency jump: cpu_pct is 38-42% through 12:04 (12:04=38.22), then 12:05=98.65, 12:06=99.25, 12:08=100.0, and stays 98-100% through 12:20. This is a hard saturation coincident with onset, not a pre-existing trend.
  - `F13` ✔ order-service quote timeouts begin at 12:05-12:06:10Z: the first ERROR log is 'pricing-engine quote call timed out after 2500ms orderId=4bd44879' at 2026-05-20T12:06:10Z, and all 225 matched ERROR entries are the same pattern 'pricing-engine quote call timed out after <n>ms orderId=<n>'. The error onset is step-change, not ramped: errors run continuously from 12:06:10 to 13:58:24.
  - `F2` ✔ Config audit (full, retried) shows cfg-3120 on pricing-engine: key 'flags.pricing.greeks_v2.rollout_pct' changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic') - 12 minutes before the incident onset.
  - `F8` ✔ Exactly one config change occurred across all services in the window: cfg-3120 on pricing-engine, flag flags.pricing.greeks_v2.rollout_pct ramped from 5 to 100 by pm-experiments at 2026-05-20T12:04:00Z ('Ramp Greeks v2 experiment to all traffic') — 12 minutes before the 12:16Z report.
  - `F32` ✔ pricing-engine cpu_pct is flat at baseline from 11:50Z through 12:04Z with no climb around the 11:56 deploy: 11:50=40.64, 11:51=41.74, 11:52=39.26, 11:53=38.7, 11:54=39.7, 11:55=39.03, 11:56=40.22, 11:57=40.35, 11:58=41.86, 11:59=38.03, 12:00=41.45, 12:01=39.39, 12:02=40.08, 12:03=40.18, 12:04=38.22 — every value in the 38-42% band. Then a hard step: 12:05=98.65, 12:06=99.25.
  - `F33` ✔ pricing-engine latency_p99_ms is flat at ~57-63ms from 11:50Z through 12:04Z with no climb at 11:56: 11:56=56.86, 11:57=60.52, 11:58=60.41, 11:59=57.86, 12:00=60.06, 12:01=58.97, 12:02=58.17, 12:03=56.82, 12:04=60.53. Then a hard step: 12:05=2534.9, 12:06=2521.4.
  - `F34` ✔ pricing-engine error_rate stays at baseline (~0.13-0.29%) from 11:54Z through 12:05Z, including the entire post-deploy stretch 11:56-12:05 (11:56=0.21, 11:57=0.22, 11:58=0.14, 11:59=0.13, 12:00=0.18, 12:01=0.2, 12:02=0.17, 12:03=0.25, 12:04=0.29, 12:05=0.15), then jumps to 12.98 at 12:06.
  - `F36` ✔ VERDICT: flat-then-step at 12:05, NOT a climb from 11:56. CPU and latency are flat at baseline for 15 consecutive minutes spanning the 11:56 market-data-service deploy and step only at 12:05; because no earlier degradation begins near 11:56, this confirms H1 trigger timing (the 12:04 greeks_v2 ramp) and further undermines H2 instead of re-opening it. The sub-minute caveat applies: the true onset could sit anywhere inside the 12:04-12:05 or 12:05-12:06 60s bucket, but no climb is visible at 11:56 or later.
**Assumptions**
  - The serialization change alone raises CPU cost enough to saturate pricing-engine
  - A 9-minute quiet gap (11:56 deploy vs 12:05 onset) is plausible for a format-effect that builds over time
  - The mechanism is untested: no parser-cost/latency measurement for the new protobuf path exists in the evidence

### H3: Shared redis-cache degradation stalls the quote path
redis-cache is shared and an inbound dependency of both pricing-engine and market-data-service on the quote path, so a failover/cold-cache/saturation could stall pricing-engine and thus order quotes. This origin is REJECTED: direct cache-health metrics contradict every degradation signature at onset.

**Causal chain**
1. redis-cache is shared, inbound dependency of both pricing-engine and market-data-service on the quote path [F11, F4]
2. redis-cache degrades at ~12:05Z *(assumed)*
3. Contrary: hit_ratio_pct peaks (98.46), cpu 21-23% flat, evictions below baseline, clients/memory stable [F20, F21, F22, F23]
4. pricing-engine latency -> ~2.5s and order-service timeouts follow [F15, F13, F14]

**Supporting evidence**
  - `F11` ✔ redis-cache is the shared component common to the quote path: it is marked shared=true (owner platform) and is an inbound dependency of BOTH pricing-engine and market-data-service. kafka and ledger-db are shared=true but attach only to order-service in this graph; no shared serialization library node appears in any dependency result.
  - `F4` ✔ order-service also depends on three SHARED components (ledger-db postgres, redis-cache redis, kafka queue) that other services use, so a neighbour could degrade quotes through a shared datastore/cache/queue as well.
**Contradicting evidence**
  - `F20` ✔ redis-cache hit_ratio_pct stays high and actually peaks at onset, not degrades: 12:04=95.76, 12:05=98.46, 12:06=96.69, range 93.83-98.46 across 12:00-12:10. A cold-cache/miss-storm (HIST-107 pattern) would show a hit-ratio drop; none occurs.
  - `F21` ✔ redis-cache cpu_pct is flat ~21-23% with NO saturation at onset: 12:04=21.16, 12:05=22.19, 12:06=22.76, 12:08=23.09 (min 21.16, max 23.09). The degraded component (pricing-engine) is at 98-100% while the shared cache is idle-healthy — no shared-cache CPU bottleneck.
  - `F22` ✔ redis-cache evictions_per_min show no eviction storm at onset: 12:04=2.15, 12:05=3.35, 12:06=3.76, staying 2.15-3.76/min throughout 12:00-12:10 (below the 12:00-12:03 baseline of 3.33-3.66). No memory-pressure eviction spike coincides with 12:05Z.
  - `F23` ✔ redis-cache connected_clients is stable across onset (12:04=299.99, 12:05=312.48, 12:06=296.5; range 296.5-335.37) and memory_pct is normal/steady (12:04=68.64, 12:05=72.72, 12:06=70.18; range 66.95-72.72). No client-connection drop, churn or memory exhaustion signals a redis failover/degradation at 12:05Z — consistent with excluding H3.
  - `F16` ✔ pricing-engine CPU saturates at the same instant as the latency jump: cpu_pct is 38-42% through 12:04 (12:04=38.22), then 12:05=98.65, 12:06=99.25, 12:08=100.0, and stays 98-100% through 12:20. This is a hard saturation coincident with onset, not a pre-existing trend.
  - `F17` ✔ pricing-engine memory_pct stays flat ~46-53% across the onset (11:55-12:20, min 46.04, max 52.54), so the pricing-engine failure is CPU-bound, not a memory/leak symptom.
**Rejected because:** Direct redis-cache health metrics show no degradation at onset: hit_ratio high/peaking (F20), CPU flat 21-23% (F21), no eviction storm (F22), stable clients/memory (F23, all missing_points 0 per F24). The degraded component (pricing-engine) is CPU-saturated (F16/F17) while the shared cache is idle-healthy, so a shared-cache bottleneck is excluded by positive evidence, not mere inference. Residual caveat: redis-cache exposes no failover/error counter (F24), so an instantaneous failover cannot be read directly.

### H4: pricing-engine CPU saturation from organic market-open load (capacity)
Precedent HIST-113 shows pricing-engine CPU saturating at market open causing quote timeouts; if this were the same recurring capacity pattern, a load surge would saturate pricing-engine CPU at 12:05Z independent of any change. REJECTED: pricing-engine request_rate is flat across onset and the whole window, so no load surge coincided with the 12:05 CPU step.

**Causal chain**
1. HIST-113 precedent: pricing-engine CPU saturation at market open -> quote timeouts/cpu 99% [F5, F12]
2. An organic load surge saturates pricing-engine CPU at 12:05Z *(assumed)*
3. Contrary: request_rate is flat at onset and across the window, no surge [F19, F29]

**Supporting evidence**
  - `F5` ✔ Precedents for quote-timeout/pricing-path symptoms: HIST-113 (pricing-engine CPU saturation at market open, symptoms 'quote timeouts','cpu 99%', category capacity_saturation); HIST-107 (redis failover, services redis-cache+market-data-service, symptoms 'cache miss storm','quote latency'); HIST-110 (order-service+ledger-db IOPS throttling, 'order latency','slow queries').
  - `F12` ✔ Precedent leads (not evidence): HIST-113 (2026-01-28) pricing-engine CPU saturation, symptoms 'quote timeouts'/cpu 99%, cause capacity_saturation; HIST-107 (2025-12-09) cold cache storm after redis failover affecting redis-cache and market-data-service, symptom quote latency, cause infrastructure_failure.
**Contradicting evidence**
  - `F19` ✔ pricing-engine request_rate shows NO load surge at 12:05Z; 60s buckets 11:50-12:10 are flat. Baseline 11:50-12:04 (~590.66-682.85 req/s), then 12:05=623.49, 12:06=646.15, 12:07=687.48, 12:08=641.54, 12:09=650.55, 12:10=610.44. Summary min 590.66 / max 687.48 / mean 640.69, missing_points 0. => H4 organic market-open surge is EXCLUDED: throughput did not rise at onset.
  - `F29` ✔ pricing-engine request_rate is flat at ~571-741 req/s across the whole window including before onset (11:30-12:19 avg ~629-648) and during saturation (13:29 avg 662.09), so the sustained ~99% CPU is not explained by a traffic increase in this window.
**Rejected because:** pricing-engine request_rate is flat at and around onset (F19: 12:04=623.49, 12:05=623.49, min 590.66 max 687.48) and flat across the full window (F29), so there is no load surge to attribute the CPU step to. The capacity_saturation precedent HIST-113 is not matched.

## Facts (verified observations)
- cfg-3120 on pricing-engine ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'); this was the only config change across all services in the window, from a full retried config-audit query. [F2, F8]
- Only one deployment occurred across all services in the 11:00-13:59Z window: dep-6111, market-data-service v5.2.0 'Switch quote serialization to protobuf', deployed 2026-05-20T11:56:00Z, status live with rolled_back_at null. [F1, F7]
- pricing-engine CPU_pct was flat at 38-42% from 11:50Z through 12:04Z (including every minute after the 11:56Z deploy) and then stepped to 98.65% at 12:05:00Z and 99.25% at 12:06:00Z, coincident with the latency jump. [F16, F32]
- pricing-engine p99 latency was flat at ~57-62ms from 11:50Z through 12:04Z (including 11:56Z) and stepped to 2534.9ms at 12:05:00Z and 2521.4ms at 12:06:00Z. [F15, F33]
- order-service quote timeouts began at 12:06:10Z with the first ERROR log 'pricing-engine quote call timed out after 2500ms orderId=4bd44879'; all 225 matched ERROR entries are the same timeout pattern and errors run continuously through 13:58:24Z. [F13]
- order-service error_rate stepped abruptly in the 12:00 10-min bucket (baseline 0.17-0.21 avg, <=0.32 max) to 3.38 avg / 8.37 max and then plateaued at ~8.1-8.3 avg through 13:50 — a step change with no gradual ramp. [F14]
- pricing-engine memory_pct stayed flat ~46-53% across onset, so the failure is CPU-bound, not a memory/leak symptom. [F17]
- pricing-engine request_rate showed no load surge at 12:05Z (12:04=623.49, 12:05=623.49, min 590.66 / max 687.48) and remained flat ~571-741 req/s across the whole window, excluding an organic market-open load surge. [F19, F29]
- redis-cache health was normal/healthy at onset: hit_ratio_pct 12:04=95.76, 12:05=98.46 (peaking, not degrading); cpu_pct flat 21.16-23.09%; evictions_per_min 2.15-3.76 (below the 12:00-12:03 baseline); connected_clients stable 296.5-335.37 and memory_pct 66.95-72.72%. [F20, F21, F22, F23]
- market-data-service emitted zero WARN-or-higher logs in 11:45-13:59Z (total_matched 0) with data range covering 08:00-13:59Z, despite the report attributing the change to it. [F18]
- pricing-engine error_rate stayed at baseline ~0.13-0.29% through 12:05Z (including the entire post-11:56 deploy stretch) then jumped to 12.98 at 12:06. [F34]
- pricing-engine cpu_pct never recovered in 12:16-13:59Z (180s buckets, min 97.76, max 100.0, mean 98.96) and latency_p99_ms stayed ~2391-2844ms (mean 2633.34) with no downslope. [F25, F26]
- No revert/rollback signal for cfg-3120 or the greeks_v2 flag appears in pricing-engine logs (queries for 'flag', 'cfg-3120', 'greeks_v2', 'rollback' over 12:16-13:59Z each returned 0 matches) and pricing-engine restarts stayed 0 for the entire 12:16-13:59Z window. [F27, F28]
- All pricing-engine metric readings used here have missing_points 0, so the flat-baseline and step readings are real data, not MISSING/null placeholders. [F35]
- Sub-minute resolution is unavailable for pricing-engine: cpu_pct at max_points=32 and 48 and error_rate at max_points=40 all returned bucket_seconds=60, so onset cannot be pinned below one 60s bucket. [F31]
- Quote-path topology: order-service -> pricing-engine (http) -> market-data-service (http); pricing-engine is a direct, non-shared dependency of order-service, and redis-cache is the shared component common to both pricing-engine and market-data-service. [F3, F10, F11]
- No deployments or config changes were found scoped to order-service (market-data-scoped deploy query returned only dep-6111; order-service config query returned count 0). [F9]

## Assumptions (not verified)
- The newly enabled Greeks v2 code path is materially more CPU-intensive than Greeks v1 — the mechanism hop that is inferred, not directly measured.
- Its cost does not scale proportionally down with rollout_pct, or it engages a self-sustaining/hot loop at full rollout, which is why flat 98-100% CPU shows no downslope through 13:59Z (mechanism detail, not a separate origin).
- The 12:04:00Z flag write propagated to pricing-engine within ~1 minute and drove the 12:05 step; sub-minute resolution is unavailable, so this is inference-by-alignment at one 60s bucket.
- The flag remains applied with no revert, inferred from zero pricing-engine log matches for flag/cfg-3120/rollback and restarts=0, rather than from an audit-store revert record.
- The absence of recovery is attributable to the flag remaining applied rather than to the flag being irrelevant to the saturation.
- pricing-engine is the component producing order-service's quote timeouts, based on the timeout log text and the quote-path topology, since no end-to-end trace was available.

## Timeline
- `2026-05-20T11:56:00Z` dep-6111 deployed: market-data-service v5.2.0 'Switch quote serialization to protobuf' (only deployment in window; still live). [F1, F7]
- `2026-05-20T11:50:00Z-12:04:00Z` pricing-engine CPU flat at 38-42% and p99 flat at ~57-62ms for 15 consecutive minutes, spanning the 11:56Z deploy — no climb attributable to the protobuf change. [F32, F33]
- `2026-05-20T12:04:00Z` cfg-3120 ramps flags.pricing.greeks_v2.rollout_pct 5 -> 100 on pricing-engine (only config change in window). [F2, F8]
- `2026-05-20T12:05:00Z` pricing-engine CPU steps 38.22% -> 98.65% and p99 steps 60.53ms -> 2534.9ms; redis-cache health remains normal/peaking and request_rate is flat. [F15, F16, F19, F20, F21, F22, F23]
- `2026-05-20T12:06:00Z` pricing-engine error_rate jumps 0.15 -> 12.98. [F34]
- `2026-05-20T12:06:10Z` First order-service quote timeout: 'pricing-engine quote call timed out after 2500ms orderId=4bd44879'. [F13]
- `2026-05-20T12:10:00Z-13:50:00Z` order-service error_rate plateaus at ~8.1-8.3 avg (max ~8.87); errors continue continuously to 13:58:24Z. [F13, F14]
- `2026-05-20T12:16:00Z` Incident reported (SEV-2) by on-call SRE. [F27]
- `2026-05-20T12:16:00Z-13:59:00Z` pricing-engine remains flat-saturated: CPU ~98-100%, p99 ~2.4-2.8s, request_rate flat, restarts 0, and no flag/cfg-3120/rollback log signal. [F25, F26, F27, F28, F29, F30]

## Recommended actions
- **[immediate]** Revert / disengage cfg-3120 on pricing-engine: set flags.pricing.greeks_v2.rollout_pct back to its pre-incident value (5) or disable the greeks_v2 flag for pricing-engine, and confirm the change landed in the config-audit store. -- cfg-3120 is the only pricing-engine-relevant change and its 12:04Z write is immediately followed by the CPU step to 98.65% and p99 step to 2534.9ms; the flagged code path is the identified compute-cost origin, so removing it is the direct mitigation. [F2, F8, F15, F16]
- **[immediate]** Verify recovery after the flag change: watch pricing-engine cpu_pct and latency_p99_ms return toward the ~40% / ~60ms baseline and confirm order-service quote-timeout ERROR logs stop and error_rate returns to ~0.2; note that if CPU/latency stay flat at 98-100%/~2.6s after the flag write lands, escalate to restarting pricing-engine as the next mitigation. -- The window shows no spontaneous recovery or revert (flat 98-100% CPU and ~2.6s p99 through 13:59Z, restarts 0, no revert log), so recovery must be actively confirmed; a non-response to revert would distinguish the persistent-state variant H5 from a simple flag-cost revert. [F25, F26, F27, F28, F13, F14]
- **[immediate]** As a safety net if revert does not restore pricing-engine within a few minutes, restart the pricing-engine process (restarts were 0 throughout, so no clear has been attempted) and confirm CPU/p99 normalization. -- The saturation is a hard CPU-bound state with no downslope and no process restart in the window; if the flagged path left a self-sustaining state, a process cycle is the cheap way to clear it. [F25, F26, F28]
- **[short_term]** Roll back dep-6111 (market-data-service v5.2.0 protobuf quote serialization) only if the flag revert does not restore pricing-engine CPU/latency; keep it as a held-down candidate rather than the first action. -- H2 remains the only live alternative with an untested mechanism (protobuf slower for the downstream parser) and the deploy remains live with rolled_back_at null; the flat-then-step shape spanning 11:56Z and market-data-service's silent logs make it unlikely, but a rollback is the decisive test if revert fails. [F1, F7, F18, F32, F33, F36]
- **[short_term]** Measure the CPU/latency cost of the Greeks v2 code path directly (bench or load test at rollout_pct 0 vs 100) and fix the expensive path or gate it behind capacity-based rollout rather than a binary full-rollout default. -- The single unproven hop in the conclusion is the mechanism — that Greeks v2 is materially more CPU-intensive and behaves near-on/off at full rollout; measuring it converts an assumption into evidence and confirms the fix. [F2, F8, F16, F15]
- **[long_term]** Add guardrails for experiment/config ramps: staged rollout with automatic rollback driven by pricing-engine CPU/p99 SLOs (e.g. halt and revert if cpu_pct > 90% or p99 > 500ms for >1 minute after a ramp), plus alerting on experiment-ramp writes to CPU-bound services. -- A 5 -> 100 single-step ramp on a CPU-heavy path took pricing-engine to full saturation within one 60s bucket with no automatic detection or revert for ~2 hours (no revert signals, restarts 0). [F2, F8, F16, F25, F26, F27, F28]
- **[long_term]** Increase pricing-engine metric resolution and add order-service/quote-path SLO alerting tied to pricing-engine p99, so sub-minute onset attribution and fast detection are possible in future. -- Sub-minute resolution is unavailable (60s buckets floor even at high max_points), which is the main reason onset attribution cannot be tightened below one minute, and the incident ran ~2h before the report. [F31, F13, F14]

## Data gaps
- First config-audit call was partial (shard 2/3 did not respond); changes between 10:00-11:00Z and shard-2-only entries remain unverified, so an additional earlier config change cannot be fully excluded.
- No live trace/end-to-end tool was available, so the order-service quote timeout could not be attributed to a specific span and the deploy-vs-ramp onset could not be discriminated below 60s resolution.
- The config-audit store was not re-queried for a 12:16-13:59Z revert record; revert-freeness rests on pricing-engine log queries (0 matches) and restarts=0, which could miss a change recorded only in the audit store.
- order-service cpu_pct / memory_pct / db_pool_in_use at the onset minute were not queried, so it is not confirmed whether order-service was purely a downstream victim or had local saturation.
- Only the top 40 of 225 order-service ERROR logs were returned (truncated), so a second, later error signature after 12:25:54Z cannot be ruled out.
- No latency/error metrics were pulled for market-data-service or other quote-path hops besides pricing-engine and order-service, so 'localized vs systemic' rests on partial coverage.
- redis-cache exposes no failover/error-counter metric, so an instantaneous failover cannot be read directly; the H3 exclusion relies on the four healthy health metrics.
- No shared library node appears in the dependency graph, so it cannot be confirmed at runtime whether order-service, pricing-engine and market-data-service share a protobuf/serialization library.
- No historical precedent was retrieved specifically for feature-flag-ramp-induced latency regressions, so that precedent class is unverified.
- The order-service error_rate query for 12:16-13:59Z was not executed, so ongoing order-service failure to the end of the window is corroborated only by pricing-engine saturation and the continuous ERROR logs.

## What would change this conclusion
- pricing-engine CPU and p99 returning to baseline promptly after reverting/disengaging cfg-3120 would confirm H1 as the root cause (and rule out a self-sustaining state).
- pricing-engine CPU/latency restoring to baseline only after rolling back dep-6111 (with the flag revert not helping) would shift the root cause to H2 (protobuf serialization change).
- Evidence of an earlier or additional config/deploy change in the unread shard-2 audit window (or a change type other than deployment/config) aligning with the 12:05Z step would reopen the origin question.
- A metric showing a load surge or a request-rate/complexity increase on pricing-engine at 12:05Z would revive H4 (capacity/organic load).
- A redis-cache failover/error event at 12:04-12:06Z (from a source exposing failover counters) would revive H3.
- Evidence that the cessation of order-service timeouts coincides with something other than pricing-engine recovery (e.g. an order-service timeout/config change) would challenge the causal chain.
- Failure to reproduce the CPU step in a controlled run of the Greeks v2 path at rollout_pct=100 would falsify the assumed mechanism and re-open the origin attribution.

## Integrity warnings (set by code)
- reasoning did not revisit active hypotheses ['H5'] in round 3; their confidence predates the latest evidence

## Investigation stats
- latency 465.76s, LLM calls 31, tokens 241341 (in 200178 / out 41163)
- tool calls 43 (1 failed, 1 partial, 0 recovered by retry), findings 36/36 verified
