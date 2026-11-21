# Incident investigation report: INC-011
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order quote timeouts originate in pricing-engine, not in the market-data-service deploy the report points to. At 12:04:00Z config change cfg-3120 ramped the feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100, moving all quote traffic onto the GreeksV2 Monte-Carlo calculator (paths=20000, 3-9s per computation). Because request_rate stayed flat (~640 req/s), CPU went from ~40% to 99-100% and each quote blew its 2000ms internal budget, producing "request deadline exceeded computing quote (2000ms budget)" errors; order-service's 2500ms client timeout then fired. market-data-service (deploy dep-6111 at 11:56) is healthy throughout (p99 ~40ms, CPU ~45%, error_rate ~0.2%), ruling out the reported suspect. Fault category: config_change (feature-flag ramp).

## Probable root cause
**Feature-flag ramp of GreeksV2 Monte-Carlo calculator saturated pricing-engine CPU, causing quote deadline breaches** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.88, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Feature-flag ramp of GreeksV2 Monte-Carlo calculator saturated pricing-engine CPU, causing quote deadline breaches | config_change | pricing-engine | cfg-3120 | 0.88 (high) | active |
| 2 | H2: market-data-service protobuf serialization deploy caused quote latency | bad_deployment | market-data-service | dep-6111 | 0.05 (low) | rejected |
| 3 | H3: Capacity saturation from market-open volume (recurrence of HIST-113) | capacity_saturation | pricing-engine | - | 0.03 (low) | rejected |

### H1: Feature-flag ramp of GreeksV2 Monte-Carlo calculator saturated pricing-engine CPU, causing quote deadline breaches
Config change cfg-3120 (12:04Z) ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 on pricing-engine. The GreeksV2 calculator runs a 20000-path Monte-Carlo costing 3-9s of CPU per quote, ~100x the previous path. With request_rate unchanged (~640 req/s), running it for all traffic saturated CPU to 100%, so each quote exceeded its 2000ms budget ('request deadline exceeded'), and order-service's 2500ms client timeout fired, failing orders at the quote step.

**Causal chain**
1. cfg-3120 ramps greeks_v2 rollout 5->100 at 12:04Z [F2]
2. All quote traffic now uses the expensive GreeksV2 Monte-Carlo (20000 paths, 3-9s) [F3]
3. pricing-engine CPU saturates to 99-100% despite flat request_rate [F6, F7]
4. Quotes breach the 2000ms internal budget -> deadline exceeded errors [F4, F8]
5. order-service 2500ms client timeout fires; orders fail at quote step [F1, F9]

**Supporting evidence**
  - `F2` ✔ A single config change occurred in the window: cfg-3120 on pricing-engine, key flags.pricing.greeks_v2.rollout_pct, old 5 -> new 100, at 2026-05-20T12:04:00Z by pm-experiments.
  - `F3` ✔ pricing-engine logs show the GreeksV2 Monte-Carlo calculator taking 3-9s per symbol ('Monte-Carlo paths=20000 took 6465ms') starting 12:05:21Z, separate from the service's normal behaviour.
  - `F4` ✔ pricing-engine logged 276 ERRORs 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' starting 12:06:41Z, indicating the expensive calculator breached the 2000ms quote budget.
  - `F5` ✔ pricing-engine p99 latency jumped from ~60ms (11:58 bucket avg 58.97) to ~2609ms (12:05 bucket avg 2609.71) and stayed ~2600ms; the jump is exactly at the 12:05 bucket.
  - `F6` ✔ pricing-engine CPU jumped from ~40% (11:58 avg 39.89) to 99-100% (12:05 avg 99.11, max 100.0) and stayed saturated; memory stayed flat ~49%.
  - `F7` ✔ pricing-engine request_rate was flat across the incident (~629-662 req/s, 11:58 avg 638.32 vs 12:05 avg 640.29), so load did not increase at onset.
  - `F8` ✘ unverified pricing-engine error_rate rose from ~0.19% (11:58) to 11-12% (12:05 avg 11.1), matching the CPU saturation window.
  - `F9` ✘ unverified order-service error_rate rose from ~0.2% to ~8% (12:05 avg 6.93) at the same onset.
  - `F14` ✔ The first config-audit query returned partial:true (shard 2/3 missing); a retry returned the complete single change cfg-3120, so no other config changes exist in the window.
  - `F15` ✔ The onset aligns with cfg-3120 (12:04) plus ~1-2 min propagation, not with dep-6111 (11:56, 10 min earlier): all pricing-engine/order-service metrics change exactly at the 12:05 bucket while market-data-service (the deploy target) is unaffected.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The greeks_v2 flag directly gates which calculator handles a quote (supported by the metric onset coinciding with the flag ramp).
  - The 12:05 metric bucket boundary is consistent with a 12:04 change taking effect within seconds.

### H2: market-data-service protobuf serialization deploy caused quote latency
Deploy dep-6111 (market-data-service v5.2.0, protobuf quote serialization, 11:56Z) degraded the quote path, propagating to pricing-engine and order-service timeouts.

**Causal chain**
(none given)

**Supporting evidence**
  - `F10` ✔ The only code deploy in the window was dep-6111 to market-data-service v5.2.0 at 11:56:00Z ('Switch quote serialization to protobuf'); no deploy to pricing-engine or order-service.
  - `F13` ✔ Dependency graph: order-service calls pricing-engine (http); pricing-engine calls market-data-service (http) and redis-cache. Edge direction is caller -> callee.
**Contradicting evidence**
  - `F11` ✔ market-data-service is healthy across the whole incident: p99 flat ~40ms, CPU flat ~45%, error_rate flat ~0.2% with no change at 12:05.
  - `F12` ✔ market-data-service logged only 1 WARN in the window ('deprecated TLS cipher offered by client') at 11:43:53Z and nothing at onset.
  - `F15` ✔ The onset aligns with cfg-3120 (12:04) plus ~1-2 min propagation, not with dep-6111 (11:56, 10 min earlier): all pricing-engine/order-service metrics change exactly at the 12:05 bucket while market-data-service (the deploy target) is unaffected.
  - `F3` ✔ pricing-engine logs show the GreeksV2 Monte-Carlo calculator taking 3-9s per symbol ('Monte-Carlo paths=20000 took 6465ms') starting 12:05:21Z, separate from the service's normal behaviour.
**Rejected because:** market-data-service is healthy throughout (p99 ~40ms, CPU ~45%, error_rate ~0.2%, no logs at onset). The slow component is pricing-engine's local GreeksV2Calculator, not the market-data call. The deploy precedes onset by 10 minutes with no matching step change.

### H3: Capacity saturation from market-open volume (recurrence of HIST-113)
Traffic growth exceeded provisioned pricing-engine capacity, saturating CPU and causing quote timeouts.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ pricing-engine request_rate was flat across the incident (~629-662 req/s, 11:58 avg 638.32 vs 12:05 avg 640.29), so load did not increase at onset.
**Rejected because:** request_rate is flat (~640 req/s) across onset; there is no load increase. CPU rose with identical traffic, which points to a per-request cost increase (the flag ramp), not demand.

## Facts (verified observations)
- order-service quote-timeout ERRORs begin 12:06:10Z, matching a pricing-engine degradation. [F1]
- The only change in the window is config cfg-3120 ramping greeks_v2 rollout 5 -> 100 at 12:04:00Z. [F2, F14]
- pricing-engine's GreeksV2 calculator executes 20000-path Monte-Carlos taking 3-9s per symbol. [F3]
- pricing-engine CPU went ~40% -> 99-100% and p99 ~60ms -> ~2600ms exactly at the 12:05 bucket. [F5, F6]
- pricing-engine request_rate is flat across onset, ruling out a load-driven cause. [F7]
- market-data-service stayed healthy (p99 ~40ms, CPU ~45%, error_rate ~0.2%) with only a TLS-cipher WARN, ruling out dep-6111 as the cause. [F11, F12]

## Assumptions (not verified)
- The greeks_v2 feature flag selects which quote calculator executes, so a 5->100 ramp moved essentially all traffic onto GreeksV2.
- The 12:04 flag change becomes effective within the 12:05 metric bucket (sub-minute propagation).
- Order-service's 2500ms timeout is the client side of pricing-engine's 2000ms internal quote budget.
- (demoted from facts: no verified observation backs it) pricing-engine error_rate rose to ~11-12% and order-service error_rate to ~8% at onset.

## Timeline
- `2026-05-20T11:43:53Z` market-data-service logs a single benign 'deprecated TLS cipher' WARN (last notable event before onset) [F12]
- `2026-05-20T11:56:00Z` deploy dep-6111 to market-data-service v5.2.0 (protobuf serialization) — reported suspect [F10]
- `2026-05-20T12:04:00Z` cfg-3120 ramps pricing-engine flags.pricing.greeks_v2.rollout_pct 5 -> 100 [F2]
- `2026-05-20T12:05:00Z` pricing-engine CPU jumps to 99-100%, p99 to ~2600ms, error_rate to ~11%; order-service error_rate to ~8% [F5, F6, F8, F9]
- `2026-05-20T12:05:21Z` pricing-engine begins logging slow GreeksV2 Monte-Carlo computations (6-8s) [F3]
- `2026-05-20T12:06:10Z` order-service begins logging 2500ms pricing-engine quote timeouts [F1]
- `2026-05-20T12:06:41Z` pricing-engine logs 'request deadline exceeded computing quote (2000ms budget)' [F4]
- `2026-05-20T12:16:00Z` INC-011 reported []

## Recommended actions
- **[immediate]** Immediately roll back cfg-3120: set flags.pricing.greeks_v2.rollout_pct back to 5 (or 0). -- This is the trigger; reverting the flag should restore pricing-engine CPU/latency and clear the quote timeouts without touching market-data-service. [F2, F6, F5]
- **[immediate]** Do NOT roll back dep-6111 (market-data-service); it is not implicated. -- market-data-service is healthy; a rollback would add risk without addressing the fault. [F11, F12]
- **[short_term]** Before re-ramping greeks_v2, benchmark its CPU cost and gate the rollout on headroom. -- GreeksV2 costs ~100x CPU per quote; a 100% rollout saturates capacity at normal traffic. [F3, F6, F7]
- **[long_term]** Add per-calculator CPU/latency metrics and an alert on quote-budget breaches, plus progressive-rollout canary guardrails for flags touching hot paths. -- Detect expensive-calculator regressions and stop flag ramps automatically before they saturate CPU. [F4, F5]

## Data gaps
- No metric series for pricing-engine restarts was inspected; restart-related effects not fully excluded (CPU saturation pattern makes restarts unlikely to be causal).
- No direct measurement of GreeksV1 vs GreeksV2 per-request CPU cost; the ~100x figure is inferred from log timings (3-9s Monte-Carlo) and CPU jump at flat request rate.
- Config-audit shard 2/3 was initially unreachable; after retry only cfg-3120 appeared, so no other config changes are expected but a shard outage could in principle have hidden changes.

## Integrity warnings (set by code)
- fact demoted to assumption: pricing-engine error_rate rose to ~11-12% and order-service error_rate to ~8% at

## Investigation stats
- latency 41.23s, LLM calls 5, tokens 59452 (in 52394 / out 7058)
- tool calls 20 (1 failed, 1 partial, 0 recovered by retry), findings 13/15 verified
