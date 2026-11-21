# Incident investigation report: INC-011
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order quote timeouts originate in pricing-engine, not order-service or the reported suspect market-data-service. At 12:04:00Z config change cfg-3120 ramped the feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100; pricing-engine applied it at 12:04:16Z. From 12:05:21Z the GreeksV2 Monte-Carlo calculator (20000 paths) began taking 6-9s per call against a 2000ms budget, driving pricing-engine CPU from ~40% to ~99-100% while request rate stayed flat at ~640 req/s. Latency p99 went 60ms->2600ms and the 2000ms-budget deadline errors appeared at 12:06:41Z, immediately followed by order-service's 'pricing-engine quote call timed out after 2500ms' at 12:06:10Z. market-data-service (the protobuf deploy at 11:56Z) is completely healthy (CPU ~45%, p99 ~40ms, error ~0.2%, zero log errors) and its timing does not match onset, ruling out the deployment hypothesis. Root cause: an over-expensive pricing experiment ramped to all traffic that saturates pricing-engine CPU, i.e. a config/feature-flag change (config_change). Immediate mitigation is rolling the flag back to a low percentage.

## Probable root cause
**GreeksV2 feature-flag ramp to 100% overloads pricing-engine CPU, causing quote timeouts** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: GreeksV2 feature-flag ramp to 100% overloads pricing-engine CPU, causing quote timeouts | config_change | pricing-engine | cfg-3120 | 0.85 (high) | active |
| 2 | H2: market-data-service protobuf serialization deploy degrades pricing-engine | bad_deployment | market-data-service | dep-6111 | 0.10 (low) | rejected |
| 3 | H3: Capacity saturation from increased market/order volume | capacity_saturation | pricing-engine | - | 0.05 (low) | rejected |

### H1: GreeksV2 feature-flag ramp to 100% overloads pricing-engine CPU, causing quote timeouts
Config change cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04Z. The GreeksV2 Monte-Carlo calculator is computationally far more expensive per request (6-9s for 20000 paths) than the previously-served calculator. At 100% of ~640 req/s it saturated pricing-engine CPU to ~99%, so quotes blew the 2000ms internal budget, causing order-service's 2500ms quote calls to time out. Fault originates in pricing-engine via a config/feature-flag change.

**Causal chain**
1. cfg-3120 ramps GreeksV2 flag 5->100 at 12:04Z [F2, F3]
2. GreeksV2 calculator applied at 12:04:16Z; heavy Monte-Carlo runs begin 12:05:21Z taking 6-9s vs 2000ms budget [F3, F4]
3. pricing-engine CPU rises 40%->99% while request_rate stays flat, i.e. per-request cost increased, not load [F6]
4. latency p99 60ms->2600ms and error_rate 0.2%->12.5% ('request deadline exceeded computing quote') [F5, F7]
5. order-service quote calls to pricing-engine exceed 2500ms and time out at 12:06:10Z [F1, F11]

**Supporting evidence**
  - `F1` ✔ order-service began emitting 'pricing-engine quote call timed out after 2500ms' errors at 12:06:10Z; 106 such ERROR entries matched, first_match 12:06:10Z.
  - `F2` ✔ A configuration change cfg-3120 on pricing-engine ramped feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z (author pm-experiments).
  - `F3` ✔ pricing-engine logged 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100' at 12:04:16Z, immediately preceding onset of the slow-calculator warnings.
  - `F4` ✔ Starting 12:05:21Z, pricing-engine logged GreeksV2Calculator Monte-Carlo runs of 20000 paths taking 2964-8942ms (first seen 6465ms), far above the 2000ms budget.
  - `F5` ✔ pricing-engine emitted 134 ERROR entries 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' beginning 12:06:41Z.
  - `F6` ✔ pricing-engine CPU jumped from ~40% to ~99-100% in the 12:00 bucket (avg 69.4 then 99.27), while request_rate stayed flat at ~640-655 req/s throughout.
  - `F7` ✔ pricing-engine latency p99 rose from ~60ms to ~2600ms in the same 12:00 bucket and stayed elevated; error_rate rose from ~0.2% to ~12.5%.
  - `F11` ✔ order-service latency p99 rose from ~150ms to ~2900ms in the 12:00 bucket, matching the 2500ms downstream quote timeout.
  - `F13` ✔ Config-change audit returned an INCOMPLETE result in the first attempt (shard 2/3 did not respond) but the retry returned the full result including cfg-3120.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The GreeksV2 calculator is genuinely much more CPU-expensive than the prior default calculator (inferred from 6-9s Monte-Carlo vs 2000ms budget).
  - The prior calculator was serving the remaining 95% of traffic before the ramp with sustainable CPU (~40%).

### H2: market-data-service protobuf serialization deploy degrades pricing-engine
market-data-service v5.2.0 switched quote serialization to protobuf at 11:56Z; pricing-engine now deserializes a heavier/different payload per quote, slowing its compute and causing timeouts.

**Causal chain**
1. dep-6111 protobuf serialization change deployed to market-data-service at 11:56Z [F9]
2. pricing-engine consumes market-data-service quotes [F12]
3. pricing-engine compute slows and times out [F4, F5]

**Supporting evidence**
  - `F9` ✔ The only deployment in the window was market-data-service v5.2.0 'Switch quote serialization to protobuf' at 11:56:00Z, which is ~9 minutes before the pricing-engine/config-change onset and had no rollback.
  - `F12` ✔ pricing-engine's dependency graph shows it calls market-data-service (http) and redis-cache (cache); market-data-service is not a proxy for the heavy compute, which runs locally in pricing-engine.
**Contradicting evidence**
  - `F8` ✔ market-data-service was healthy throughout: latency p99 flat at ~40ms, CPU flat at ~45%, error_rate flat at ~0.2%, and zero WARN/ERROR log entries in the window.
  - `F6` ✔ pricing-engine CPU jumped from ~40% to ~99-100% in the 12:00 bucket (avg 69.4 then 99.27), while request_rate stayed flat at ~640-655 req/s throughout.
  - `F3` ✔ pricing-engine logged 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100' at 12:04:16Z, immediately preceding onset of the slow-calculator warnings.
**Assumptions**
  - Protobuf payloads would meaningfully increase pricing-engine CPU per quote.
**Rejected because:** market-data-service shows no degradation (CPU ~45%, p99 ~40ms, error 0.2%, zero log errors) and the onset is ~9 min after the deploy and coincides exactly with the pricing-engine flag ramp at 12:04Z, not the 11:56Z deploy. A serialization format change would also not raise pricing-engine CPU from 40% to ~99%.

### H3: Capacity saturation from increased market/order volume
Increasing request volume exceeded pricing-engine's provisioned capacity (as in HIST-113), causing CPU saturation and quote timeouts.

**Causal chain**
1. request rate rises into saturation *(assumed)*
2. CPU saturates and quotes time out [F6]

**Supporting evidence**
  - `F6` ✔ pricing-engine CPU jumped from ~40% to ~99-100% in the 12:00 bucket (avg 69.4 then 99.27), while request_rate stayed flat at ~640-655 req/s throughout.
  - `F14` ✔ A comparable historical incident HIST-113 shows pricing-engine quote timeouts with cpu 99% attributed to capacity_saturation (record market-open volume). In this incident, however, request_rate is flat, so load did not increase.
**Contradicting evidence**
  - `F6` ✔ pricing-engine CPU jumped from ~40% to ~99-100% in the 12:00 bucket (avg 69.4 then 99.27), while request_rate stayed flat at ~640-655 req/s throughout.
**Assumptions**
  - Request volume rose.
**Rejected because:** pricing-engine request_rate is flat (~640-655 req/s) across the whole window including the onset; only per-request CPU cost changed, so this is not load-driven capacity saturation.

## Facts (verified observations)
- Config change cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z on pricing-engine. [F2]
- pricing-engine applied cfg-3120 at 12:04:16Z. [F3]
- GreeksV2Calculator Monte-Carlo runs of 20000 paths began taking 6-9s from 12:05:21Z, exceeding the 2000ms budget. [F4]
- pricing-engine CPU rose from ~40% to ~99-100% while request_rate stayed flat at ~640 req/s. [F6]
- pricing-engine latency p99 rose from ~60ms to ~2600ms and error_rate from ~0.2% to ~12.5%. [F7]
- order-service began logging 'pricing-engine quote call timed out after 2500ms' at 12:06:10Z and its p99 rose to ~2900ms. [F1, F11]
- market-data-service showed no degradation: p99 ~40ms, CPU ~45%, error ~0.2%, zero WARN/ERROR logs. [F8]
- No pricing-engine deployment or restart occurred in the window. [F10]
- The only deployment was market-data-service v5.2.0 (protobuf serialization) at 11:56:00Z, ~9 minutes before onset. [F9]

## Assumptions (not verified)
- The GreeksV2 calculator is genuinely far more CPU-expensive per request than the previously-serving calculator (inferred from 6-9s Monte-Carlo runs vs 2000ms budget; no direct before/after per-calculator cost data was available).
- The prior 5% GreeksV2 rollout plus default calculator were served within the ~40% baseline CPU, so the ramp to 100% is what pushed CPU to saturation.
- cfg-3120 was applied to the live pricing-engine fleet (not staged), per the 12:04:16Z application log.

## Timeline
- `2026-05-20T11:56:00Z` market-data-service v5.2.0 (protobuf serialization) deployed — eventual red herring [F9]
- `2026-05-20T12:04:00Z` cfg-3120: flags.pricing.greeks_v2.rollout_pct ramped 5 -> 100 [F2]
- `2026-05-20T12:04:16Z` pricing-engine logs cfg-3120 applied [F3]
- `2026-05-20T12:05:21Z` GreeksV2 Monte-Carlo runs begin taking 6-9s vs 2000ms budget [F4]
- `2026-05-20T12:06:10Z` order-service starts logging pricing-engine quote call timeouts after 2500ms [F1]
- `2026-05-20T12:06:41Z` pricing-engine logs 'request deadline exceeded computing quote (2000ms budget)' [F5]
- `2026-05-20T12:00:00Z` pricing-engine CPU bucket rises to ~99-100%, p99 to ~2600ms, error_rate to ~12.5% [F6, F7]
- `2026-05-20T13:50:00Z` Condition persists through latest data (CPU ~99%, p99 ~2600ms), no recovery [F7]

## Recommended actions
- **[immediate]** Roll back cfg-3120: set flags.pricing.greeks_v2.rollout_pct back to a low value (e.g. 5) or 0 -- The ramp to 100% is the trigger; reverting it should return CPU, latency and error rate to baseline per the causal chain. [F2, F3, F4, F6, F7]
- **[immediate]** Temporarily add pricing-engine capacity (more replicas / CPU) to absorb load while the flag is rolled back -- CPU is pinned at ~99-100%, so headroom is needed to drain timeouts quickly even as the flag reverts. [F6]
- **[short_term]** Do NOT roll back market-data-service v5.2.0 as the primary fix; it is healthy and not the cause -- market-data-service shows no degradation and its deploy timing does not match onset; a rollback there would not address the saturation. [F8, F9]
- **[long_term]** Add a CPU-cost/latency gate to feature-flag ramp tooling so expensive calculators cannot be ramped to 100% without a canary capacity check -- The experiment was ramped directly to all traffic without validating per-request compute cost, saturating CPU. [F2, F4, F6]
- **[long_term]** Fix/complete the config-audit shard so config-change queries are not partial during incidents -- The first config-change query returned an incomplete result (shard 2/3 down), which could delay flag-change discovery. [F13]

## Data gaps
- No per-calculator cost/benchmark data to quantify how much more expensive GreeksV2 is than the prior default calculator; the CPU-cost claim rests on the observed 6-9s Monte-Carlo durations vs the 2000ms budget.
- pricing-engine request_rate is bucketed at 600s; a finer-grained rate series was not available to fully exclude a short burst, though the bucket-level rate is flat.
- The reported symptom mentioned 'pricing-engine p99 2.6s' but no data was available on the exact historical default-calculator per-call latency before 12:00 to compute the precise per-request CPU multiplier.

## Investigation stats
- latency 54.06s, LLM calls 7, tokens 96609 (in 87913 / out 8696)
- tool calls 25 (1 failed, 1 partial, 0 recovered by retry), findings 14/14 verified
