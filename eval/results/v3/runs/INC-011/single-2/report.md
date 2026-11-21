# Incident investigation report: INC-011
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order quote timeouts originate in pricing-engine, not the reported order-service or market-data-service. A config/feature-flag change cfg-3120 at 12:04Z ramped flags.pricing.greeks_v2.rollout_pct from 5% to 100%, routing all quote traffic through the expensive GreeksV2 Monte-Carlo calculator (2.9-8.9s per quote). pricing-engine CPU jumped from ~40% to ~99% (with request rate flat), quote deadlines (2000ms budget) were exceeded, p99 rose to ~2.6s and 5xx to ~12.5%, and order-service quote calls timed out at 2500ms starting 12:06:10Z. The market-data-service protobuf serialization deploy (dep-6111, 11:56Z) is a temporal red herring: its latency/CPU stayed flat and the onset follows the flag ramp, not the deploy. Root cause category: config_change.

## Probable root cause
**greeks_v2 flag ramp to 100% saturated pricing-engine CPU, causing quote deadline misses** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.88, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: greeks_v2 flag ramp to 100% saturated pricing-engine CPU, causing quote deadline misses | config_change | pricing-engine | cfg-3120 | 0.88 (high) | active |
| 2 | H2: market-data-service protobuf serialization deploy degraded quote path | bad_deployment | market-data-service | dep-6111 | 0.10 (low) | rejected |
| 3 | H3: Capacity saturation from market-open load | capacity_saturation | pricing-engine | - | 0.05 (low) | rejected |

### H1: greeks_v2 flag ramp to 100% saturated pricing-engine CPU, causing quote deadline misses
Config change cfg-3120 at 12:04Z ramped flags.pricing.greeks_v2.rollout_pct from 5% to 100%, so the expensive GreeksV2 Monte-Carlo calculator (2.9-8.9s per quote) ran for all traffic, pinning pricing-engine CPU at ~99%. Quotes exceeded the 2000ms budget and pricing-engine returned latency/errors, so order-service quote calls hit their 2500ms timeout. Fault originates at pricing-engine (config_change).

**Causal chain**
1. cfg-3120 ramps greeks_v2 to 100% at 12:04Z [F2]
2. GreeksV2 Monte-Carlo runs for all traffic, taking multi-second per quote [F3]
3. pricing-engine CPU saturates ~99% with flat request rate [F5]
4. quote deadline (2000ms) exceeded -> p99 ~2.6s, 5xx ~12.5% [F4, F6]
5. order-service quote calls time out at 2500ms [F1, F11]

**Supporting evidence**
  - `F1` ✔ order-service began emitting 'pricing-engine quote call timed out after 2500ms' errors at 12:06:10Z, sustained through 13:58Z (225 such errors).
  - `F2` ✔ Config change cfg-3120 on pricing-engine ramped feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z.
  - `F3` ✔ pricing-engine logs show GreeksV2Calculator Monte-Carlo computations taking 2964-8942ms (paths=20000), and 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors, starting at 12:05:21Z.
  - `F4` ✔ pricing-engine latency_p99_ms jumped from ~60ms (11:30-11:50) to 1325ms avg in the 12:00 bucket and ~2600-2700ms sustained thereafter, max 2849ms.
  - `F5` ✔ pricing-engine cpu_pct rose from ~40% (11:30-11:50) to avg 99.27% at 12:10 and stayed ~99% through 13:59 (max 100%), while request_rate remained flat ~635-661 req/s.
  - `F6` ✔ pricing-engine error_rate (HTTP 5xx) rose from ~0.2% to 12.55% avg at 12:10 and stayed ~12.4-12.7% through 13:59.
  - `F10` ✔ order-service depends on pricing-engine via HTTP; pricing-engine depends on market-data-service via HTTP.
  - `F11` ✔ Inference: the fault propagates order-service -> pricing-engine, where the CPU-bound GreeksV2 Monte-Carlo calculator exceeds the 2000ms quote deadline, so order-service quote calls hit their 2500ms timeout.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 5%->100% ramp is what newly routed the expensive GreeksV2 path; the calculator itself was already deployed but only sampled.

### H2: market-data-service protobuf serialization deploy degraded quote path
Deployment dep-6111 (market-data-service v5.2.0, protobuf quote serialization) at 11:56Z caused pricing-engine quote slowdowns leading to order-service timeouts.

**Causal chain**
1. dep-6111 deploys protobuf serialization at 11:56Z [F8]
2. market-data-service would show latency degradation *(assumed)*
3. pricing-engine quote latency rises [F4]

**Supporting evidence**
  - `F8` ✔ The only deployment in window is dep-6111 (market-data-service v5.2.0, 'Switch quote serialization to protobuf') deployed at 11:56:00Z, not rolled back.
**Contradicting evidence**
  - `F7` ✔ market-data-service latency_p99_ms stayed flat ~40ms and cpu_pct flat ~45% across 11:30-13:59 (no degradation).
  - `F3` ✔ pricing-engine logs show GreeksV2Calculator Monte-Carlo computations taking 2964-8942ms (paths=20000), and 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors, starting at 12:05:21Z.
  - `F2` ✔ Config change cfg-3120 on pricing-engine ramped feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z.
**Assumptions**
  - Serialization overhead could be CPU-bound in pricing-engine even without market-data-service degradation.
**Rejected because:** market-data-service latency (~40ms) and CPU (~45%) are flat across the window (F7); onset is 12:05-12:06 (F3), ~10 min after the 11:56 deploy and ~immediately after the 12:04 flag ramp (F2). The deploy is a temporal red herring.

### H3: Capacity saturation from market-open load
pricing-engine was overwhelmed by organic request-volume growth (as in HIST-113), exhausting provisioned capacity.

**Causal chain**
1. request volume grew, saturating pricing-engine *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F5` ✔ pricing-engine cpu_pct rose from ~40% (11:30-11:50) to avg 99.27% at 12:10 and stayed ~99% through 13:59 (max 100%), while request_rate remained flat ~635-661 req/s.
**Rejected because:** request_rate is flat ~635-661 req/s before and after onset (F5/tc-013); the CPU rise coincides with the flag ramp, not with a traffic increase.

## Facts (verified observations)
- order-service quote timeouts started 12:06:10Z (225 errors of 'pricing-engine quote call timed out after 2500ms'). [F1]
- Config change cfg-3120 ramped greeks_v2.rollout_pct 5->100 at 12:04:00Z. [F2]
- GreeksV2 Monte-Carlo computations took 2.9-8.9s and quote deadlines (2000ms budget) began being exceeded at 12:05:21Z. [F3]
- pricing-engine p99 latency rose from ~60ms to ~2600-2800ms and error_rate to ~12.5%, while request_rate stayed flat ~640 req/s. [F4, F5, F6]
- market-data-service latency and CPU stayed flat across the incident window. [F7]
- The only deployment was market-data-service dep-6111 at 11:56Z (protobuf serialization), preceding symptoms by ~10 minutes. [F8]
- pricing-engine memory stayed ~49% and restarts stayed 0, indicating no node/memory failure. [F9]

## Assumptions (not verified)
- The greeks_v2 flag controls whether quotes use the GreeksV2 calculator; at 5% only a fraction of traffic paid the cost, so CPU stayed ~40%.
- The 2000ms pricing-engine deadline and 2500ms order-service timeout are as logged.

## Timeline
- `2026-05-20T11:56:00Z` market-data-service dep-6111 (protobuf serialization) deployed [F8]
- `2026-05-20T12:04:00Z` cfg-3120 ramps greeks_v2 rollout_pct 5->100 on pricing-engine [F2]
- `2026-05-20T12:05:21Z` First GreeksV2 Monte-Carlo slow log (6465ms) on pricing-engine [F3]
- `2026-05-20T12:06:10Z` First order-service quote timeout (2500ms) [F1]
- `2026-05-20T12:10:00Z` pricing-engine CPU ~99%, p99 ~2.6s, 5xx ~12.5% sustained [F4, F5, F6]

## Recommended actions
- **[immediate]** Roll back cfg-3120 (set flags.pricing.greeks_v2.rollout_pct back toward 5%) to restore pricing-engine CPU and quote latency. -- Onset coincides exactly with the 5%->100% ramp; reverting should remove the CPU saturation. [F2, F4, F5]
- **[immediate]** If rollback is not possible, shed/limit GreeksV2 traffic or scale pricing-engine replicas temporarily. -- CPU is pinned at ~99%; adding capacity or reducing load restores headroom. [F5]
- **[short_term]** Optimize or bound the GreeksV2 Monte-Carlo calculator (reduce paths or cache) before re-ramping. -- 20000-path Monte-Carlo takes multi-second per quote, far above the 2000ms budget. [F3]
- **[long_term]** Add a canary/gradual-ramp guard with CPU and p99 SLO gates for pricing feature-flag rollouts. -- A 5%->100% single-step ramp on a CPU-heavy path bypassed any staged validation. [F2, F5]

## Data gaps
- config-audit shard 2/3 did not respond on first query (tc-004 partial); a second query returned the single cfg-3120 change, but a config change in the missing shard cannot be fully excluded.
- No pre-12:04 log lines explicitly stating the greeks_v2 effective rollout percentage beyond the single applied-change line (tc-016).

## Investigation stats
- latency 38.95s, LLM calls 5, tokens 59418 (in 52938 / out 6480)
- tool calls 19 (0 failed, 1 partial, 0 recovered by retry), findings 11/11 verified
