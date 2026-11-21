# Incident investigation report: INC-011
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order quote timeouts (onset 12:06:10Z) are caused by pricing-engine, not market-data-service. Config change cfg-3120 ramped feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z (applied 12:04:16Z). Within ~1 minute pricing-engine began running the CPU-heavy GreeksV2 Monte-Carlo path (20000 paths, 3-9s per call); CPU jumped from ~40% to ~99%, quote p99 rose from ~60ms to ~2600ms, exceeding the 2000ms internal quote budget and the 2500ms order-service timeout, yielding 12%+ 5xx and order-service quote timeouts. Request rate was flat (no load surge), market-data-service latency/errors were flat (ruling out the protobuf deploy dep-6111), and there were no restarts or memory pressure. The flag was never rolled back and the incident was still fully active at 13:59Z. Root cause category: config_change, origin in pricing-engine's Greeks v2 code path.

## Probable root cause
**Feature-flag ramp of Greeks v2 (config_change) saturated pricing-engine CPU, causing quote deadline misses and order timeouts** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Feature-flag ramp of Greeks v2 (config_change) saturated pricing-engine CPU, causing quote deadline misses and order timeouts | config_change | pricing-engine | cfg-3120 | 0.86 (high) | active |
| 2 | H2: market-data-service protobuf serialization deploy (dep-6111) broke pricing-engine quote parsing | bad_deployment | market-data-service | dep-6111 | 0.05 (low) | rejected |
| 3 | H3: Capacity saturation due to increased request load | capacity_saturation | pricing-engine | - | 0.04 (low) | rejected |

### H1: Feature-flag ramp of Greeks v2 (config_change) saturated pricing-engine CPU, causing quote deadline misses and order timeouts
Config change cfg-3120 (flags.pricing.greeks_v2.rollout_pct 5->100) at 12:04:00Z enabled the CPU-heavy GreeksV2 Monte-Carlo path (20000 paths, 3-9s each) for all traffic. Pricing-engine CPU jumped from ~40% to ~99%, quote p99 went from ~60ms to ~2600ms, exceeding the 2000ms internal budget and the 2500ms order-service timeout, producing quote timeouts and elevated 5xx. The fault originates in pricing-engine (the newly-fully-enabled Greeks v2 code path) but is triggered by the flag rollout.

**Causal chain**
1. cfg-3120 ramps greeks_v2 rollout 5->100 at 12:04:00Z [F2, F3]
2. GreeksV2Calculator Monte-Carlo path (20000 paths) becomes active at full traffic and takes 3-9s per call [F4]
3. pricing-engine CPU saturates ~40%->~99% [F7]
4. quote p99 rises 60ms->2600ms, exceeding 2000ms internal budget [F6, F5]
5. order-service 2500ms pricing-engine quote calls time out [F1, F15]

**Supporting evidence**
  - `F2` ✔ A pricing-engine config change cfg-3120 ramped feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z.
  - `F3` ✔ pricing-engine logged the config change as applied at 12:04:16Z: 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100'.
  - `F4` ✔ pricing-engine began logging slow GreeksV2Calculator Monte-Carlo runs (paths=20000) taking 2964-8942ms at 12:05:21Z (342 such WARN entries).
  - `F5` ✔ pricing-engine logged 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' from 12:06:41Z (276 ERROR entries).
  - `F6` ✔ pricing-engine latency_p99_ms jumped from ~60ms (bucket 11:58) to ~2609ms avg (bucket 12:05) and stayed ~2600ms through 13:57.
  - `F7` ✔ pricing-engine cpu_pct jumped from ~40% (bucket 11:58, avg 39.89) to ~99% (bucket 12:05, avg 99.11, max 100.0) and stayed pinned near 99-100%.
  - `F8` ✔ pricing-engine error_rate rose from ~0.19% (bucket 11:58) to ~12.5% (bucket 12:12 onward, avg 12.34) and stayed elevated.
  - `F9` ✔ pricing-engine request_rate was flat/normal throughout (~629-662 req/s, no surge) across the incident.
  - `F10` ✔ market-data-service latency_p99_ms stayed flat at ~39-42ms across the whole window (no degradation), and its error_rate stayed ~0.2%.
  - `F15` ✔ order-service latency_p99_ms jumped from ~150ms (bucket 11:58) to ~2433ms (bucket 12:05), consistent with waiting on pricing-engine quotes.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The greeks_v2 flag gates the GreeksV2Calculator path (strongly implied by identical naming and timing, not directly stated in a single log line)
  - No other change between 11:56 and 12:05 affected pricing-engine

### H2: market-data-service protobuf serialization deploy (dep-6111) broke pricing-engine quote parsing
Deploy dep-6111 switched market-data-service quote serialization to protobuf at 11:56:00Z, allegedly causing pricing-engine quote timeouts.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ market-data-service latency_p99_ms stayed flat at ~39-42ms across the whole window (no degradation), and its error_rate stayed ~0.2%.
  - `F11` ✔ market-data-service error_rate remained ~0.17-0.28% throughout the incident window with no spike.
  - `F12` ✔ The only deployment in the window is dep-6111 on market-data-service (protobuf quote serialization) at 11:56:00Z, still 'live' with no rollback; no pricing-engine deployments occurred.
  - `F4` ✔ pricing-engine began logging slow GreeksV2Calculator Monte-Carlo runs (paths=20000) taking 2964-8942ms at 12:05:21Z (342 such WARN entries).
  - `F5` ✔ pricing-engine logged 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' from 12:06:41Z (276 ERROR entries).
  - `F2` ✔ A pricing-engine config change cfg-3120 ramped feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z.
**Rejected because:** market-data-service latency and error_rate stayed flat/normal throughout; the onset (~12:05) follows the pricing-engine flag change (12:04), not the 11:56 deploy; the failing code path is a local GreeksV2 CPU computation, not serialization; and the timing gap (9 min) doesn't match. This is the report's human guess, unsupported by telemetry.

### H3: Capacity saturation due to increased request load
Traffic growth pushed pricing-engine past provisioned capacity, causing CPU saturation and timeouts.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ pricing-engine request_rate was flat/normal throughout (~629-662 req/s, no surge) across the incident.
  - `F2` ✔ A pricing-engine config change cfg-3120 ramped feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z.
  - `F3` ✔ pricing-engine logged the config change as applied at 12:04:16Z: 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100'.
  - `F4` ✔ pricing-engine began logging slow GreeksV2Calculator Monte-Carlo runs (paths=20000) taking 2964-8942ms at 12:05:21Z (342 such WARN entries).
**Rejected because:** request_rate was flat (~630-660 req/s) across the onset; no load surge. CPU rose because a far heavier computation (GreeksV2) was enabled by the flag, not because of more traffic.

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- The greeks_v2 rollout flag gates the GreeksV2Calculator execution path (inferred from identical naming and 1-minute onset alignment; no single log line states this explicitly)
- No pricing-engine deployment or other config change occurred between 11:56 and 12:05 that could independently raise CPU (deployments list shows none)
- Downstream effects (order timeouts) are caused by pricing-engine latency, consistent with the dependency graph order-service->pricing-engine->market-data-service

## Timeline
- `2026-05-20T11:56:00Z` market-data-service deploy dep-6111 (protobuf quote serialization) goes live [F12]
- `2026-05-20T12:04:00Z` Config change cfg-3120: flags.pricing.greeks_v2.rollout_pct 5->100 [F2]
- `2026-05-20T12:04:16Z` pricing-engine logs cfg-3120 applied (rollout_pct=100) [F3]
- `2026-05-20T12:05:00Z` pricing-engine CPU jumps ~40%->~99%, latency_p99 ~60ms->~2609ms [F6, F7]
- `2026-05-20T12:05:21Z` Slow GreeksV2Calculator Monte-Carlo warnings begin (3-9s per call) [F4]
- `2026-05-20T12:06:10Z` order-service begins logging pricing-engine quote call timeouts (2500ms) [F1]
- `2026-05-20T12:06:41Z` pricing-engine logs request deadline exceeded (2000ms budget) calculator=GreeksV2 [F5]
- `2026-05-20T12:12:00Z` pricing-engine error_rate elevated to ~12.5% [F8]
- `2026-05-20T13:59:00Z` Incident still active; cfg-3120 never rolled back [F16]

## Recommended actions
- **[immediate]** Immediately roll back cfg-3120 (set flags.pricing.greeks_v2.rollout_pct back to 5 or 0) and confirm pricing-engine CPU and quote p99 recover -- This is the direct trigger; reverting the flag should immediately stop the CPU-heavy GreeksV2 path and restore latency. Flag was never rolled back and incident is ongoing. [F2, F3, F6, F7, F16]
- **[short_term]** Add a target of reducing GreeksV2 Monte-Carlo path count / optimizing the calculator before re-enabling at scale -- 20000-path Monte-Carlo runs taking 3-9s and pinning CPU at 99% is not viable at full traffic; needs optimization or a sampled/async design. [F4, F7]
- **[short_term]** Require load/capacity validation and staged ramp with automated rollback for feature-flag ramps on latency-critical services -- A 5->100 jump in one step bypassed gradual exposure; canarying would have caught the CPU blowup before full ramp. [F2, F7]
- **[long_term]** Add alerting on pricing-engine CPU saturation and GreeksV2 calculator duration budget breaches -- CPU pinned at 99% and 3-9s calculator runs were only visible as slow logs; proactive alerting would shorten detection. [F4, F6, F7]

## Data gaps
- The initial all-services config-change query (tc-004) was partial (config-audit shard 2/3 did not respond); however the retried full query (tc-019) returned the complete set of 2 changes, so config completeness is confirmed.
- No log/metric directly proves the greeks_v2 flag is the on/off switch for the GreeksV2Calculator code path; this linkage is inferred from naming and timing.
- Root cause of why GreeksV2 is CPU-heavy (code regression vs. inherently expensive algorithm) cannot be determined from telemetry alone; no pricing-engine deployment in window means the code predates the window.

## Investigation stats
- latency 63.59s, LLM calls 6, tokens 87778 (in 79859 / out 7919)
- tool calls 25 (0 failed, 1 partial, 0 recovered by retry), findings 17/17 verified
