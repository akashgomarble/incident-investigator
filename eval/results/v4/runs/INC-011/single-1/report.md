# Incident investigation report: INC-011
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order quote timeouts (INC-011) originate in pricing-engine, not market-data-service. At 12:04:00Z the feature flag flags.pricing.greeks_v2.rollout_pct was ramped from 5 to 100 (cfg-3120, author pm-experiments). GreeksV2's Monte-Carlo calculator (20000 paths, 3-9s per quote) then ran on 100% of traffic, saturating pricing-engine CPU at ~99-100% (up from ~40%). Quote computation exceeded pricing-engine's 2000ms budget, pushing pricing-engine p99 from ~60ms to ~2530ms — beyond order-service's 2500ms client timeout — so order-service quotes began failing ("pricing-engine quote call timed out after 2500ms", first at 12:06:10Z). The market-data-service protobuf deploy (dep-6111, 11:56Z) named in the report is a red herring: market-data-service latency (~40ms), request_rate (~950/s) and error_rate (~0.2%) are all flat through the incident window and it logged no warnings. Root cause category: config_change, originating in pricing-engine.

## Probable root cause
**GreeksV2 feature-flag ramp to 100% saturated pricing-engine CPU** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: GreeksV2 feature-flag ramp to 100% saturated pricing-engine CPU | config_change | pricing-engine | cfg-3120 | 0.86 (high) | active |
| 2 | H2: market-data-service protobuf deploy (dep-6111) broke quote serialization | bad_deployment | market-data-service | dep-6111 | 0.08 (low) | rejected |
| 3 | H3: Traffic/volume growth outgrew pricing-engine capacity | capacity_saturation | pricing-engine | - | 0.05 (low) | rejected |

### H1: GreeksV2 feature-flag ramp to 100% saturated pricing-engine CPU
cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct 5->100 at 12:04Z, so the CPU-heavy GreeksV2 Monte-Carlo calculator (20000 paths, 3-9s/quote) ran on all traffic, saturating pricing-engine CPU (~40%->~100%). Quote computation exceeded the 2000ms deadline and p99 rose to ~2530ms, past order-service's 2500ms timeout, causing quote timeouts.

**Causal chain**
1. cfg-3120 ramps greeks_v2 rollout 5%->100% at 12:04Z [F2]
2. GreeksV2 Monte-Carlo runs on 100% of traffic, each taking 3-9s [F3, F4]
3. pricing-engine CPU saturates to ~99-100% [F5]
4. quote p99 rises from ~60ms to ~2530ms, exceeding 2000ms budget [F6]
5. order-service 2500ms client timeout trips -> quote timeouts [F1, F11]

**Supporting evidence**
  - `F2` ✔ A pricing-engine config change cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z (author pm-experiments).
  - `F3` ✔ pricing-engine logs show GreeksV2Calculator Monte-Carlo runs taking 3499-8942ms (paths=20000) plus 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors beginning 12:05:21Z/12:06:41Z.
  - `F4` ✔ No GreeksV2 warnings appear before the flag ramp: in 11:00-12:06 only 3 GreeksV2 matches exist and all are at >=12:05:21Z.
  - `F5` ✔ pricing-engine CPU jumped from ~40% (flat through 11:54) to 49.66% avg/98.65% max in the 12:00 bucket and ~99-100% thereafter.
  - `F6` ✔ pricing-engine latency p99 was ~60ms through the 12:00 bucket then jumped to avg 2528.15ms at the 12:05 bucket.
  - `F7` ✔ pricing-engine request_rate stayed flat (~630-660 req/s) across the incident, so the CPU/latency rise was not driven by a traffic surge.
  - `F11` ✔ order-service error_rate rose from ~0.2% to ~8% starting the 12:06 bucket, consistent with quote timeouts.
  - `F12` ✔ pricing-engine error_rate rose from ~0.2% to ~12-13% starting the 12:06 bucket; no pricing-engine deployment exists and restarts stayed 0.
  - `F14` ✔ The 12:04 feature-flag ramp of GreeksV2 to 100% is the trigger: it moved the expensive Monte-Carlo calculator (3-9s/quote) from 5% to 100% of traffic, saturating pricing-engine CPU and pushing quote latency past the 2500ms order-service timeout.
**Contradicting evidence**
  - (none)
**Assumptions**
  - GreeksV2 is the dominant CPU consumer in pricing-engine (no other component shows a matching change).
  - The 5% pre-ramp cohort was a small enough share that it did not previously saturate CPU.

### H2: market-data-service protobuf deploy (dep-6111) broke quote serialization
The dep-6111 protobuf serialization change at 11:56Z degraded quote handling for internal callers, slowing pricing-engine quotes.

**Causal chain**
1. dep-6111 changes quote serialization to protobuf [F10]
2. pricing-engine quote latency spikes [F6]

**Supporting evidence**
  - `F10` ✔ The only deployment in the window is dep-6111: market-data-service v5.2.0 'Switch quote serialization to protobuf' at 11:56:00Z; it is still status 'live' (not rolled back).
**Contradicting evidence**
  - `F8` ✔ market-data-service shows no degradation: latency_p99 flat ~39-43ms, request_rate flat ~935-960/s, error_rate flat ~0.2% across the whole window.
  - `F9` ✔ market-data-service emitted zero WARN/ERROR log entries in 11:50-12:30 (total_matched 0).
  - `F15` ✔ The market-data-service protobuf deploy (dep-6111) is not the cause; its timing (11:56) precedes the observed degradation (12:04-12:06) and market-data-service telemetry is entirely healthy, ruling out a downstream payload/serialization fault.
  - `F4` ✔ No GreeksV2 warnings appear before the flag ramp: in 11:00-12:06 only 3 GreeksV2 matches exist and all are at >=12:05:21Z.
**Assumptions**
  - Serialization change could add CPU cost on the caller side.
**Rejected because:** market-data-service telemetry is fully healthy (latency ~40ms flat, request_rate flat, error_rate ~0.2%, zero logs), the deploy precedes onset by ~8min without a matching step change, and the observed slow path is an in-process Monte-Carlo computation, not serialization.

### H3: Traffic/volume growth outgrew pricing-engine capacity
A surge in quote request volume saturated pricing-engine CPU.

**Causal chain**
1. request volume rises *(assumed)*
2. CPU saturates [F5]

**Supporting evidence**
  - `F5` ✔ pricing-engine CPU jumped from ~40% (flat through 11:54) to 49.66% avg/98.65% max in the 12:00 bucket and ~99-100% thereafter.
**Contradicting evidence**
  - `F7` ✔ pricing-engine request_rate stayed flat (~630-660 req/s) across the incident, so the CPU/latency rise was not driven by a traffic surge.
  - `F12` ✔ pricing-engine error_rate rose from ~0.2% to ~12-13% starting the 12:06 bucket; no pricing-engine deployment exists and restarts stayed 0.
**Assumptions**
  - Request volume could have risen without showing in request_rate.
**Rejected because:** pricing-engine request_rate is flat (~630-660/s) across the incident; the CPU rise coincides exactly with the 12:04 flag ramp, not with load.

## Facts (verified observations)
- order-service quote timeout errors ('pricing-engine quote call timed out after 2500ms') start at 12:06:10Z. [F1]
- cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z. [F2]
- pricing-engine GreeksV2 Monte-Carlo runs took 3.5-8.9s and exceeded a 2000ms quote budget. [F3]
- GreeksV2 warnings first appear at 12:05:21Z; none before the ramp. [F4]
- pricing-engine CPU rose from ~40% to ~99-100% at ~12:00-12:06. [F5]
- pricing-engine p99 rose from ~60ms to ~2528ms in the 12:05 bucket. [F6]
- pricing-engine request_rate stayed flat ~630-660 req/s. [F7]
- market-data-service latency, request_rate and error_rate stayed flat/healthy. [F8]
- market-data-service logged zero WARN/ERROR entries in the window. [F9]
- dep-6111 (market-data-service protobuf serialization) at 11:56Z is the only deployment and remains live. [F10]
- order-service error_rate rose from ~0.2% to ~8%; pricing-engine error_rate rose to ~12-13%; pricing-engine restarts stayed 0. [F11, F12]
- order-service calls pricing-engine, which calls market-data-service and redis-cache. [F13]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-05-20T11:56:00Z` market-data-service v5.2.0 (protobuf quote serialization) deployed (dep-6111), remains live. [F10]
- `2026-05-20T12:04:00Z` cfg-3120 ramps flags.pricing.greeks_v2.rollout_pct 5 -> 100. [F2]
- `2026-05-20T12:05:21Z` First GreeksV2 Monte-Carlo slow-run warnings in pricing-engine. [F4]
- `2026-05-20T12:05:00Z` pricing-engine p99 jumps to ~2528ms; CPU reaches ~100%. [F6, F5]
- `2026-05-20T12:06:10Z` First order-service quote timeout error. [F1]
- `2026-05-20T12:06:41Z` First pricing-engine 'request deadline exceeded computing quote (2000ms budget)' error. [F3]
- `2026-05-20T12:16:00Z` Incident INC-011 reported by on-call SRE. []
- `2026-05-20T13:59:00Z` Latest data; degradation persists (pricing-engine still ~99% CPU, elevated error rates). [F5, F12]

## Recommended actions
- **[immediate]** Roll back cfg-3120: set flags.pricing.greeks_v2.rollout_pct back to 5 (or 0). -- The CPU saturation and quote latency step-change coincide exactly with the 5->100 ramp; reverting should restore CPU and latency. [F2, F5, F6]
- **[immediate]** Verify recovery: watch pricing-engine CPU/p99 and order-service error_rate after the flag revert. -- Confirms the causal chain and that no second fault is masking recovery. [F1, F5, F11]
- **[short_term]** Profile/optimize the GreeksV2 Monte-Carlo calculator (path count, vectorization, caching) before any future full rollout. -- 20000-path Monte-Carlo at 3-9s/quote cannot run at 100% traffic on current capacity; the calculator itself is too expensive. [F3, F7]
- **[short_term]** Do NOT roll back dep-6111; instead exclude market-data-service as the cause. -- Its telemetry is healthy and timing does not match; a rollback would be an unfounded change. [F8, F9, F10]
- **[long_term]** Require load/CPU-capacity validation and a canary stage for feature-flag ramps affecting CPU-heavy pricing code. -- Prevents a single flag ramp from saturating a service and cascading to callers. [F2, F5]

## Data gaps
- Initial get_config_changes (all services) returned partial:true because 'config-audit shard 2/3 did not respond'; a retry for pricing-engine returned cfg-3120 and the all-services retry returned only cfg-3118 and cfg-3120, but a scoped re-query for other services (e.g., order-service) was not exhaustively re-tried, so an unrelated config change cannot be 100% excluded.
- No direct metric isolating GreeksV2 CPU share vs other pricing-engine code; the attribution of CPU saturation to GreeksV2 rests on log evidence and exact timing, not a per-component CPU metric.
- No trace/correlation IDs linking individual order-service timeouts to individual GreeksV2 calculator runs, so the hop-by-hop linkage is inferred from matching onset times rather than a single traced request.

## Investigation stats
- latency 170.09s, LLM calls 6, tokens 73835 (in 65567 / out 8268)
- tool calls 24 (0 failed, 1 partial, 0 recovered by retry), findings 15/15 verified
