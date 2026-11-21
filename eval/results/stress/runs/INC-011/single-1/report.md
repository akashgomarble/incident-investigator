# Incident investigation report: INC-011
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order-service quote timeouts are caused by pricing-engine CPU saturation triggered by config change cfg-3120, which at 12:04:00Z ramped feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100. With Greeks v2 active on all traffic, the GreeksV2Calculator Monte-Carlo (20000 paths) takes 3-9s per quote against a 2000ms deadline budget, driving pricing-engine p99 from ~60ms to ~2600ms, CPU from ~40% to 99%, and error rate from ~0.2% to ~12.5%; order-service then logs ~106 quote timeouts. Traffic is flat (~644 req/s), memory is flat (~49%), and market-data-service is completely healthy (p99 ~40ms, cpu ~45%, error ~0.2%), so the reported market-data-service protobuf serialization deploy dep-6111 is a red herring.

## Probable root cause
**Feature-flag rollout of Greeks v2 to 100% saturated pricing-engine CPU, stalling quotes** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Feature-flag rollout of Greeks v2 to 100% saturated pricing-engine CPU, stalling quotes | config_change | pricing-engine | cfg-3120 | 0.90 (high) | active |
| 2 | H2: market-data-service protobuf serialization change caused quote timeouts | bad_deployment | market-data-service | dep-6111 | 0.05 (low) | rejected |
| 3 | H3: Traffic volume outgrew pricing-engine capacity | capacity_saturation | pricing-engine | - | 0.05 (low) | rejected |

### H1: Feature-flag rollout of Greeks v2 to 100% saturated pricing-engine CPU, stalling quotes
Config change cfg-3120 (pricing-engine flags.pricing.greeks_v2.rollout_pct 5->100 at 12:04Z) activated the expensive GreeksV2 Monte-Carlo path on all traffic. Each quote runs 20000-path Monte-Carlo taking 3-9s against a 2000ms budget, saturating pricing-engine CPU at ~99% and pushing p99 to ~2600ms; order-service quote calls then exceed their 2500ms timeout and fail.

**Causal chain**
1. cfg-3120 ramps greeks_v2 flag 5->100 at 12:04Z [F3]
2. flag activation enables GreeksV2Calculator on all quotes [F4]
3. Monte-Carlo takes 3-9s vs 2000ms budget -> deadline exceeded errors [F4]
4. CPU saturates ~99%, p99 rises to ~2600ms, error_rate to ~12.5% [F1, F2, F5]
5. order-service quote calls exceed 2500ms and log timeouts [F7]

**Supporting evidence**
  - `F1` ✔ pricing-engine latency_p99_ms jumps from ~60ms to ~2609ms in the bucket starting 2026-05-20T12:05:00Z and stays ~2600ms through 13:57Z.
  - `F2` ✔ pricing-engine cpu_pct jumps from ~40% to ~99-100% at the same 12:05Z bucket and remains saturated.
  - `F3` ✔ Config change cfg-3120 on pricing-engine set flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z, described as 'Ramp Greeks v2 experiment to all traffic'.
  - `F4` ✔ pricing-engine logs show GreeksV2Calculator Monte-Carlo runs (paths=20000) taking 2964-8942ms, and 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors; first such WARN appears 2026-05-20T12:05:21Z.
  - `F5` ✔ pricing-engine error_rate rises from ~0.2% to 6.57% in the 12:02Z bucket and ~12.5% thereafter.
  - `F7` ✔ order-service logs 106 ERROR 'pricing-engine quote call timed out after 2500ms' entries starting 2026-05-20T12:06:10Z.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The flag value 5->100 means 5% to 100% of traffic uses Greeks v2
  - GreeksV2 is the new/expensive calculator activated by the flag

### H2: market-data-service protobuf serialization change caused quote timeouts
dep-6111 switched market-data-service quote serialization to protobuf, degrading the pricing-engine -> market-data-service call and causing quote timeouts.

**Causal chain**
1. dep-6111 deployed 11:56Z [F11]
2. protobuf change degrades market-data-service latency *(assumed)*
3. degradation propagates to pricing-engine quotes *(assumed)*

**Supporting evidence**
  - `F11` ✔ Deployment dep-6111 (market-data-service v5.2.0, 'Switch quote serialization to protobuf') was deployed at 2026-05-20T11:56:00Z and was not rolled back.
**Contradicting evidence**
  - `F8` ✔ market-data-service latency_p99_ms stays flat at ~39-41ms across the whole window, unaffected at onset.
  - `F9` ✔ market-data-service cpu_pct stays flat at ~45% and error_rate flat at ~0.2% throughout the window.
  - `F10` ✔ The only market-data-service log in the window is a single 'deprecated TLS cipher offered by client' WARN at 11:43:53Z; no errors.
  - `F1` ✔ pricing-engine latency_p99_ms jumps from ~60ms to ~2609ms in the bucket starting 2026-05-20T12:05:00Z and stays ~2600ms through 13:57Z.
**Rejected because:** market-data-service p99 (~40ms), cpu (~45%) and error_rate (~0.2%) are flat across the window; the change would have degraded its own telemetry. Onset (12:05Z) is 9 minutes after the 11:56Z deploy and coincides exactly with cfg-3120 at 12:04Z. The fault is CPU-bound compute inside pricing-engine, not a serialization/dependency issue.

### H3: Traffic volume outgrew pricing-engine capacity
A surge in quote requests exceeded pricing-engine provisioned capacity, causing CPU saturation and timeouts.

**Causal chain**
1. request rate rises *(assumed)*
2. CPU saturates [F2]

**Supporting evidence**
  - `F2` ✔ pricing-engine cpu_pct jumps from ~40% to ~99-100% at the same 12:05Z bucket and remains saturated.
**Contradicting evidence**
  - `F6` ✔ pricing-engine request_rate stays flat (~628-662 req/s) across the entire window; no traffic surge.
**Rejected because:** pricing-engine request_rate is flat (~644 req/s, min 571 to max 741) across the entire window; there is no load increase to explain the CPU jump. CPU rose only after the flag ramp.

## Facts (verified observations)
- pricing-engine p99 rose from ~60ms to ~2600ms starting at the 12:05Z bucket and remained elevated. [F1]
- pricing-engine CPU rose from ~40% to ~99% at the same 12:05Z bucket. [F2]
- Config change cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z, one minute before onset. [F3]
- pricing-engine logged GreeksV2 Monte-Carlo runs of 3-9s and 2000ms-budget deadline-exceeded errors from 12:05:21Z. [F4]
- pricing-engine error_rate climbed to ~12.5% after 12:02Z. [F5]
- pricing-engine request_rate was flat (~644 req/s), so no traffic surge occurred. [F6]
- order-service logged 106 quote-call timeouts against pricing-engine from 12:06:10Z. [F7]
- market-data-service p99, CPU, and error_rate were flat across the window. [F8, F9]
- market-data-service deploy dep-6111 (protobuf serialization) occurred at 11:56Z, 9 minutes before onset, with no telemetry impact. [F11, F8]
- pricing-engine had no code deployments in the window. [F12]
- pricing-engine memory was flat at ~49%, ruling out memory exhaustion. [F13]

## Assumptions (not verified)
- flags.pricing.greeks_v2.rollout_pct value is a percentage of traffic, so 5->100 means the GreeksV2 calculator now serves all quotes.
- GreeksV2Calculator is the code path gated by the greeks_v2 flag.
- The pricing-engine CPU saturation is caused by the Monte-Carlo compute (not an external dependency), consistent with flat request rate and flat downstream telemetry.

## Timeline
- `2026-05-20T11:43:53Z` Only market-data-service log in window: a single unrelated deprecated-TLS-cipher WARN. [F10]
- `2026-05-20T11:56:00Z` market-data-service deploys v5.2.0 (dep-6111, protobuf serialization); no telemetry impact. [F11]
- `2026-05-20T12:04:00Z` cfg-3120 ramps pricing-engine greeks_v2 flag 5->100. [F3]
- `2026-05-20T12:05:21Z` First GreeksV2 Monte-Carlo WARN (6465ms) in pricing-engine; p99 and CPU jump at 12:05Z. [F4, F1, F2]
- `2026-05-20T12:06:10Z` order-service begins logging pricing-engine quote call timeouts. [F7]
- `2026-05-20T12:16:00Z` Incident INC-011 reported. []
- `2026-05-20T13:57:00Z` pricing-engine p99 and CPU still elevated (ongoing, no rollback). [F1, F2]

## Recommended actions
- **[immediate]** Roll back cfg-3120: set flags.pricing.greeks_v2.rollout_pct back to 5 (or 0). -- Onset coincides within one minute of the 5->100 ramp; reverting the flag should immediately remove the expensive Monte-Carlo path from most traffic and relieve CPU saturation. [F3, F1, F2]
- **[immediate]** Verify recovery via pricing-engine p99 (<100ms), CPU (<50%), error_rate (~0.2%) and cessation of order-service quote timeouts after the flag revert. -- Confirms H1 causal chain and rules out confounders. [F1, F2, F5, F7]
- **[short_term]** Do NOT roll back dep-6111; instead verify market-data-service is exonerated before the incident call. -- market-data-service telemetry is flat; an unnecessary rollback adds risk and would not address the fault. [F8, F9]
- **[short_term]** Re-size Greeks v2: reduce Monte-Carlo path count / add caching or async compute, and ramp the flag gradually with CPU/latency SLO gates. -- GreeksV2 at 20000 paths takes 3-9s per quote far exceeding the 2000ms budget; the experiment must fit the request budget before full rollout. [F4]
- **[long_term]** Add an automatic guard that trips a feature-flag ramp when pricing-engine CPU exceeds ~85% or p99 exceeds the quote budget. -- Prevents a flag ramp (which bypasses code deploy gates) from silently saturating production. [F3, F2, F1]

## Data gaps
- config-audit shard 2/3 did not respond on the first all-services config query (tc-002, partial:true), so other services' config changes in the window cannot be fully excluded; the pricing-engine-scoped query (tc-009) returned cfg-3120 cleanly.
- The exact baseline behavior at 5% Greeks v2 traffic and any prior history of the flag is not directly listed; assumed from old_value=5.
- No root-cause confirmation from a successful flag rollback yet (the change is still live at 100%), so the causal chain is inferred from timing plus mechanism rather than a verified recovery.
- market-data-service internal serialization correctness (protobuf decode on pricing-engine side) was not directly probed; it is inferred safe from flat downstream telemetry.

## Investigation stats
- latency 134.4s, LLM calls 5, tokens 55456 (in 48383 / out 7073)
- tool calls 18 (0 failed, 1 partial, 5 recovered by retry), findings 13/13 verified
