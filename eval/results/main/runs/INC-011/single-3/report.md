# Incident investigation report: INC-011
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Root cause is a feature-flag config change on pricing-engine: cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5% to 100% at 12:04:00Z. Within ~1 minute pricing-engine CPU went from ~40% to ~99% and p99 latency from ~60ms to ~2600ms while request_rate stayed flat (~640 rps), indicating the CPU-heavy GreeksV2 Monte-Carlo calculator (20k paths, 3–9s per call) now ran on all traffic and saturated the service. Quote computation exceeded its 2000ms budget, producing "request deadline exceeded computing quote" errors, which propagated to order-service as "pricing-engine quote call timed out after 2500ms" from 12:06:10Z (order error_rate 0.2%→8.2%). The market-data-service protobuf deploy (dep-6111, 11:56Z) is a red herring: market-data CPU, latency and error rate stayed flat. Fault originates at pricing-engine (config_change).

## Probable root cause
**GreeksV2 feature-flag ramp saturates pricing-engine CPU, causing quote deadline failures** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: GreeksV2 feature-flag ramp saturates pricing-engine CPU, causing quote deadline failures | config_change | pricing-engine | cfg-3120 | 0.90 (high) | active |
| 2 | H2: market-data-service protobuf serialization deploy causes slow/failed quotes | bad_deployment | market-data-service | dep-6111 | 0.05 (low) | rejected |
| 3 | H3: Capacity saturation from market/traffic surge on pricing-engine | capacity_saturation | pricing-engine | - | 0.03 (low) | rejected |

### H1: GreeksV2 feature-flag ramp saturates pricing-engine CPU, causing quote deadline failures
Config change cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5% to 100% at 12:04:00Z on pricing-engine. The CPU-heavy GreeksV2Calculator (Monte-Carlo, 20k paths) then ran on essentially all quote requests, driving pricing-engine CPU from ~40% to ~99% and p99 latency from ~60ms to ~2600ms while request rate stayed flat. Quote computation exceeded the 2000ms budget, and order-service quote calls timed out at 2500ms, raising order-service error rate to ~8%.

**Causal chain**
1. cfg-3120 ramps greeks_v2.rollout_pct 5->100 at 12:04Z [F1, F2]
2. GreeksV2 Monte-Carlo (20k paths, 3-9s) now runs on all quote traffic [F6]
3. pricing-engine CPU 40%->99% and p99 60ms->2600ms with flat request rate [F3, F4, F5]
4. quote computation exceeds 2000ms budget -> deadline errors [F6]
5. order-service quote calls time out at 2500ms -> error_rate ~8% [F7, F8, F9]

**Supporting evidence**
  - `F1` ✔ Config change cfg-3120 on pricing-engine changed flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z, authored by pm-experiments, described as 'Ramp Greeks v2 experiment to all traffic'.
  - `F2` ✔ pricing-engine logged the config change taking effect: 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100' at 12:04:16Z.
  - `F3` ✔ pricing-engine latency_p99_ms jumped from <65ms (11:58 bucket) to 2609.71ms avg at the 12:05:00Z bucket and stayed elevated (~2600ms) through 13:57Z.
  - `F4` ✔ pricing-engine cpu_pct jumped from ~40% (11:58 bucket 39.89) to ~99% (12:05 bucket avg 99.11, max 100.0) and stayed near 99% through the window.
  - `F5` ✔ pricing-engine request_rate was flat at ~640 req/s across the entire incident window (629 rps at 11:30 through 643 rps at 13:57), so no traffic surge occurred.
  - `F6` ✔ pricing-engine logs show 'GreeksV2Calculator: Monte-Carlo paths=20000 took ...ms' warnings from 12:05:21Z (e.g. 6465ms, 8361ms, 8710ms) and 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors from 12:06:41Z.
  - `F7` ✔ order-service logged 'pricing-engine quote call timed out after 2500ms' errors starting 12:06:10Z (225 ERROR entries, last 13:58:24Z).
  - `F8` ✘ unverified order-service error_rate jumped from ~0.2% (11:58 bucket 0.2) to 6.93% at the 12:05:00Z bucket and ~8.2% thereafter.
  - `F9` ✔ order-service latency_p99_ms jumped from ~150ms to 2433ms at the 12:05:00Z bucket and ~2900ms thereafter.
**Contradicting evidence**
  - (none)
**Assumptions**
  - GreeksV2Calculator is the dominant CPU consumer during the incident (implied by the WARN logs but not directly profiled)
  - No other workload change occurred at 12:04-12:05Z (config-audit and metrics do not show one)

### H2: market-data-service protobuf serialization deploy causes slow/failed quotes
Deployment dep-6111 switched market-data-service quote serialization to protobuf at 11:56Z, which the incident report suggested as the cause of order quote timeouts.

**Causal chain**
1. dep-6111 switches quote payloads to protobuf at 11:56Z [F12]
2. market-data-service degrades -> pricing-engine quote calls slow *(assumed)*

**Supporting evidence**
  - `F12` ✔ Deployment dep-6111 on market-data-service v5.2.0 'Switch quote serialization to protobuf' was deployed at 11:56:00Z, still live with no rollback.
  - `F13` ✔ Dependency graph: order-service calls pricing-engine over http; pricing-engine calls market-data-service and redis-cache; no deployment was made to pricing-engine or order-service in the window.
**Contradicting evidence**
  - `F3` ✔ pricing-engine latency_p99_ms jumped from <65ms (11:58 bucket) to 2609.71ms avg at the 12:05:00Z bucket and stayed elevated (~2600ms) through 13:57Z.
  - `F4` ✔ pricing-engine cpu_pct jumped from ~40% (11:58 bucket 39.89) to ~99% (12:05 bucket avg 99.11, max 100.0) and stayed near 99% through the window.
  - `F10` ✔ market-data-service latency_p99_ms stayed flat ~40ms across the whole window (min 35.28, max 43.75), unaffected by the 11:56Z protobuf deploy.
  - `F11` ✔ market-data-service cpu_pct stayed flat ~45% (mean 45.37) and error_rate flat ~0.2% (mean 0.21) across the whole window.
**Assumptions**
  - none that hold
**Rejected because:** market-data-service shows no degradation: latency flat ~40ms, CPU flat ~45%, error rate flat ~0.2% across the window; the onset (12:05Z) does not match the deploy (11:56Z) and does match the GreeksV2 flag ramp instead.

### H3: Capacity saturation from market/traffic surge on pricing-engine
Pricing-engine CPU saturation was caused by a surge in quote request volume exceeding provisioned capacity (as in historical incident HIST-113).

**Causal chain**
1. request volume surges -> CPU saturates *(assumed)*

**Supporting evidence**
  - `F4` ✔ pricing-engine cpu_pct jumped from ~40% (11:58 bucket 39.89) to ~99% (12:05 bucket avg 99.11, max 100.0) and stayed near 99% through the window.
**Contradicting evidence**
  - `F5` ✔ pricing-engine request_rate was flat at ~640 req/s across the entire incident window (629 rps at 11:30 through 643 rps at 13:57), so no traffic surge occurred.
**Assumptions**
  - none that hold
**Rejected because:** request_rate is flat at ~640 req/s throughout; the CPU jump is not driven by traffic volume but by a per-request compute cost increase from the flag ramp.

## Facts (verified observations)
- cfg-3120 changed pricing-engine flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z. [F1]
- pricing-engine applied cfg-3120 (greeks_v2.rollout_pct=100) at 12:04:16Z. [F2]
- pricing-engine cpu_pct rose from ~40% to ~99% at the 12:05:00Z bucket. [F4]
- pricing-engine latency_p99_ms rose from ~60ms to ~2600ms at the 12:05:00Z bucket. [F3]
- pricing-engine request_rate was flat (~640 req/s) across the window. [F5]
- pricing-engine logged GreeksV2 Monte-Carlo calls taking 3-9s and quote deadline-exceeded errors (2000ms budget). [F6]
- order-service logged quote timeouts (2500ms) starting 12:06:10Z; error_rate rose to ~8%. [F7]
- market-data-service latency, CPU and error rate remained flat throughout. [F10, F11]

## Assumptions (not verified)
- GreeksV2Calculator is the dominant CPU consumer in pricing-engine during the incident (strongly implied by WARN logs but not profiled directly).
- No other unlogged change occurred at 12:04-12:05Z; the config-audit tool returned partial data on the all-services query, so a hidden change cannot be fully excluded.
- The 11:56Z market-data deploy is genuinely unrelated because its telemetry is unchanged.

## Timeline
- `2026-05-20T11:56:00Z` market-data-service v5.2.0 (dep-6111, protobuf serialization) deployed [F12]
- `2026-05-20T12:04:00Z` cfg-3120 changes pricing-engine greeks_v2.rollout_pct 5 -> 100 [F1]
- `2026-05-20T12:04:16Z` pricing-engine applies cfg-3120 [F2]
- `2026-05-20T12:05:00Z` pricing-engine CPU ~40% -> ~99%, p99 ~60ms -> ~2600ms [F3, F4]
- `2026-05-20T12:05:21Z` GreeksV2 Monte-Carlo (20k paths) taking 3-9s logged [F6]
- `2026-05-20T12:06:10Z` order-service quote call timeouts begin; error_rate rises to ~8% [F7, F8]
- `2026-05-20T12:16:00Z` Incident reported by on-call SRE []
- `2026-05-20T13:58:24Z` Quote timeout errors still occurring (no recovery) [F7]

## Recommended actions
- **[immediate]** Roll back cfg-3120 (set flags.pricing.greeks_v2.rollout_pct back to 5) to immediately relieve pricing-engine CPU and restore quotes. -- The flag ramp is the trigger; reverting it should drop GreeksV2 Monte-Carlo load and restore CPU/latency. [F1, F2, F4]
- **[immediate]** Verify recovery by watching pricing-engine cpu_pct/latency_p99 and order-service error_rate after the flag revert. -- Confirms the causal link and that no secondary fault exists. [F3, F4, F8]
- **[short_term]** Add a CPU-cost/latency guardrail and staged canary gating to feature-flag ramps for compute-heavy calculators (GreeksV2). -- A jump from 5% to 100% put 20k-path Monte-Carlo on all traffic with no canary brake. [F1, F6]
- **[long_term]** Scale pricing-engine horizontally or reduce Monte-Carlo path count / add caching so the GreeksV2 calculator fits within the 2000ms quote budget at full rollout. -- Even at 100% rollout the per-request compute must meet the deadline. [F6, F9]
- **[long_term]** Avoid attributing future quote incidents to the market-data protobuf deploy; add quote-serialization health signals only if needed. -- market-data-service telemetry is flat; the deploy was a red herring. [F10, F11, F12]

## Data gaps
- All-services config-change query (tc-003) returned partial:true (config-audit shard 2/3 did not respond); although the pricing-engine-scoped query (tc-012) gave a complete result, a change in another service/shard cannot be fully excluded.
- No CPU profiling to prove GreeksV2Calculator consumes the ~60% extra CPU; inference is from logs plus the flag timing.
- No explicit rollback record for cfg-3120: the later pricing-engine config query (tc-021) returned count 0, suggesting no rollback applied (consistent with errors persisting to 13:58Z), but this was not separately confirmed.

## Investigation stats
- latency 45.86s, LLM calls 6, tokens 78675 (in 70756 / out 7919)
- tool calls 22 (0 failed, 1 partial, 0 recovered by retry), findings 13/14 verified
