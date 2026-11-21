# Incident investigation report: INC-011
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order quote timeouts are caused by pricing-engine CPU saturation triggered by the feature-flag ramp cfg-3120 (flags.pricing.greeks_v2.rollout_pct 5->100 at 12:04), which moved (almost) all quotes onto the expensive GreeksV2 Monte-Carlo calculator (20000 paths, 3-9s each). pricing-engine CPU went 40%->99%, p99 went 60ms->~2600ms against a 2000ms quote budget, and order-service quote calls began timing out at 2500ms from 12:06. The market-data-service protobuf deploy (dep-6111 at 11:56) named in the report is NOT the cause: its error_rate and latency are perfectly flat. Request rate is flat, ruling out a capacity/traffic surge. Fault originates in pricing-engine configuration => config_change; the fix is to roll back cfg-3120.

## Probable root cause
**Feature-flag ramp of CPU-heavy Greeks v2 calculator saturated pricing-engine CPU** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Feature-flag ramp of CPU-heavy Greeks v2 calculator saturated pricing-engine CPU | config_change | pricing-engine | cfg-3120 | 0.86 (high) | active |
| 2 | H2: market-data-service protobuf serialization deploy broke pricing-engine quote path | bad_deployment | market-data-service | dep-6111 | 0.08 (low) | rejected |
| 3 | H3: Capacity saturation from a traffic surge (cf. HIST-113) | capacity_saturation | pricing-engine | - | 0.05 (low) | rejected |

### H1: Feature-flag ramp of CPU-heavy Greeks v2 calculator saturated pricing-engine CPU
Config change cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04, switching virtually all quote computations to the expensive GreeksV2 Monte-Carlo calculator (20000 paths, 3-9s each). This pegged pricing-engine CPU at ~99%, pushed p99 above the 2000ms budget, and caused the downstream order-service quote-call timeouts. The fault originates in pricing-engine's configuration, not in market-data-service.

**Causal chain**
1. cfg-3120 ramps greeks_v2 rollout from 5% to 100% at 12:04 [F3, F4]
2. GreeksV2 Monte-Carlo calculator consumes 3-9s CPU per quote (20000 paths) [F5]
3. pricing-engine CPU saturates at ~99% with flat request rate [F2]
4. p99 exceeds the 2000ms quote budget -> deadline-exceeded errors [F1, F5]
5. order-service quote calls exceed 2500ms and time out [F6, F10]

**Supporting evidence**
  - `F1` ✔ pricing-engine latency_p99_ms jumped from ~60ms to ~1500-2850ms starting in the 12:00-12:12 bucket and stayed elevated (~2600ms avg) through 13:59.
  - `F2` ✔ pricing-engine CPU rose from ~40% to ~99% (max 100%) starting at the 12:00 bucket, while request_rate stayed flat at ~640 req/s throughout.
  - `F3` ✔ A config change cfg-3120 on pricing-engine set flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z, description 'Ramp Greeks v2 experiment to all traffic'.
  - `F4` ✔ pricing-engine log confirms the flag was applied at 12:04:16: 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100'.
  - `F5` ✔ pricing-engine emits repeated warnings 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms...' and errors 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2', first seen 12:05:21.
  - `F6` ✔ order-service logged 225 ERRORs 'pricing-engine quote call timed out after 2500ms', first at 12:06:10 — i.e. its quote step times out waiting on pricing-engine.
  - `F9` ✔ pricing-engine had no code deployments in the window (count 0) and zero restarts.
  - `F10` ✔ order-service latency_p99 jumped from ~150ms to ~2900ms concurrently with the pricing-engine onset, but order-service CPU stayed flat at ~40%.
**Contradicting evidence**
  - `F7` ✔ market-data-service error_rate stayed flat at ~0.2% and latency_p99 flat at ~40ms across the entire window, including after its 11:56 protobuf deploy.
  - `F8` ✔ The only deployment in the window was dep-6111 to market-data-service v5.2.0 ('Switch quote serialization to protobuf') at 11:56:00Z, status live, not rolled back.
**Assumptions**
  - greeks_v2.rollout_pct=100 means all/almost all quote requests use the GreeksV2 calculator
  - CPU saturation is the mechanism (not a separate fault) causing the latency

### H2: market-data-service protobuf serialization deploy broke pricing-engine quote path
The report's guess: market-data-service v5.2.0 (dep-6111, protobuf serialization switch at 11:56) caused downstream quote slowness. This is contradicted — market-data-service error_rate and latency remained flat, and the pricing-engine fault is CPU-bound inside its own GreeksV2 calculator, not in serialization/deserialization.

**Causal chain**
1. dep-6111 deployed at 11:56 [F8]
2. No market-data error/latency degradation appeared (contradicts) [F7]

**Supporting evidence**
  - `F8` ✔ The only deployment in the window was dep-6111 to market-data-service v5.2.0 ('Switch quote serialization to protobuf') at 11:56:00Z, status live, not rolled back.
**Contradicting evidence**
  - `F7` ✔ market-data-service error_rate stayed flat at ~0.2% and latency_p99 flat at ~40ms across the entire window, including after its 11:56 protobuf deploy.
  - `F5` ✔ pricing-engine emits repeated warnings 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms...' and errors 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2', first seen 12:05:21.
  - `F2` ✔ pricing-engine CPU rose from ~40% to ~99% (max 100%) starting at the 12:00 bucket, while request_rate stayed flat at ~640 req/s throughout.
  - `F9` ✔ pricing-engine had no code deployments in the window (count 0) and zero restarts.
**Rejected because:** market-data-service telemetry is completely flat (error_rate ~0.2%, p99 ~40ms) and the failing subsystem is pricing-engine's internal GreeksV2 calculator; no serialization/deserialization error appears anywhere.

### H3: Capacity saturation from a traffic surge (cf. HIST-113)
pricing-engine CPU hit 99% due to a genuine traffic/load surge exceeding provisioned capacity, as in HIST-113. Contradicted: request_rate stayed flat at ~640 req/s across the entire window, so load did not grow — the CPU rise is from a per-request cost increase, not volume.

**Causal chain**
1. CPU rose to ~99% [F2]
2. but request_rate did not increase (contradicts surge) [F2]

**Supporting evidence**
  - `F2` ✔ pricing-engine CPU rose from ~40% to ~99% (max 100%) starting at the 12:00 bucket, while request_rate stayed flat at ~640 req/s throughout.
**Contradicting evidence**
  - `F2` ✔ pricing-engine CPU rose from ~40% to ~99% (max 100%) starting at the 12:00 bucket, while request_rate stayed flat at ~640 req/s throughout.
  - `F3` ✔ A config change cfg-3120 on pricing-engine set flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z, description 'Ramp Greeks v2 experiment to all traffic'.
  - `F5` ✔ pricing-engine emits repeated warnings 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms...' and errors 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2', first seen 12:05:21.
**Rejected because:** Request rate is flat (~640 req/s) before and after onset; the CPU jump is explained by per-request GreeksV2 cost, not extra volume.

## Facts (verified observations)
- pricing-engine p99 latency rose from ~60ms to ~1500-2850ms beginning at the 12:00 bucket and stayed elevated through 13:59. [F1]
- pricing-engine CPU rose from ~40% to ~99% at 12:00 while request_rate remained flat at ~640 req/s. [F2]
- Config change cfg-3120 set flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z. [F3]
- pricing-engine logged the cfg-3120 application at 12:04:16. [F4]
- pricing-engine logged GreeksV2 Monte-Carlo computations of 3-9s and 2000ms-budget deadline-exceeded errors starting 12:05:21. [F5]
- order-service logged 225 'pricing-engine quote call timed out after 2500ms' errors starting 12:06:10. [F6]
- market-data-service error_rate (~0.2%) and p99 (~40ms) were flat across the whole window. [F7]
- The only deployment was market-data-service v5.2.0 (protobuf) at 11:56, live and not rolled back. [F8]
- pricing-engine had zero deployments and zero restarts in the window. [F9]
- order-service p99 rose to ~2900ms while its CPU stayed flat at ~40%. [F10]

## Assumptions (not verified)
- greeks_v2.rollout_pct=100 routes essentially all quote computations to the GreeksV2 Monte-Carlo calculator
- CPU saturation in pricing-engine is the mechanism translating the flag ramp into latency; no separate hardware fault is present
- the flag value is the operative control knob and was not already overridden elsewhere

## Timeline
- `2026-05-20T11:56:00Z` market-data-service v5.2.0 (dep-6111, protobuf serialization) deployed — no telemetry impact follows. [F8, F7]
- `2026-05-20T12:04:00Z` Config change cfg-3120 ramps flags.pricing.greeks_v2.rollout_pct 5->100. [F3]
- `2026-05-20T12:04:16Z` pricing-engine logs 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100'. [F4]
- `2026-05-20T12:05:21Z` First GreeksV2 Monte-Carlo slow warnings (>6s for 20000 paths) and 2000ms budget deadline-exceeded errors. [F5]
- `2026-05-20T12:06:10Z` order-service begins logging 'pricing-engine quote call timed out after 2500ms'. [F6]
- `2026-05-20T12:16:00Z` Incident INC-011 reported (SEV-2). []
- `2026-05-20T13:59:00Z` Latest data: pricing-engine still ~99% CPU, order-service still timing out; no recovery. [F1, F2]

## Recommended actions
- **[immediate]** Roll back cfg-3120: set flags.pricing.greeks_v2.rollout_pct back to 5 (or lower) to shed the heavy calculator load. -- Onset tracks the flag ramp exactly; reverting should restore CPU/latency since the calculator is the CPU consumer. [F3, F4, F2, F1]
- **[immediate]** Confirm recovery by watching pricing-engine CPU (return toward ~40%) and p99 (return toward ~60ms), plus cessation of order-service quote timeouts. -- Validates the causal chain; if unaffected, H1 is wrong and hypothesis space reopens. [F2, F1, F6]
- **[short_term]** Temporarily raise pricing-engine replica count / CPU limits to absorb GreeksV2 cost while the flag is re-tuned. -- Buys headroom for the experiment without a full rollback. [F2]
- **[short_term]** Re-tune the GreeksV2 calculator (reduce Monte-Carlo paths, cache results, or lower rollout %) before re-ramping. -- 20000 paths taking 3-9s per quote exceeds the 2000ms budget by design. [F5]
- **[long_term]** Add a pre-ramp load/CPU guardrail and canary stage for feature-flag rollouts on CPU-heavy code paths. -- A 5->100 jump masks whether the new path fits the latency budget. [F3, F5]

## Data gaps
- First get_config_changes call was partial (config-audit shard 2/3 did not respond); a retry returned only cfg-3120, so a second concurrent config change could in principle exist but was not seen.
- No per-calculator request split metric exists, so the exact fraction of quotes hitting GreeksV2 (pre/post ramp) is inferred from the flag, not directly measured.
- No pricing-engine replica count / HPA behavior metric available to fully separate config_change from an autoscaling limitation.

## Investigation stats
- latency 43.26s, LLM calls 5, tokens 66583 (in 59195 / out 7388)
- tool calls 22 (0 failed, 1 partial, 0 recovered by retry), findings 12/12 verified
