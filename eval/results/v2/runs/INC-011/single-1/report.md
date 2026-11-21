# Incident investigation report: INC-011
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
order-service quote timeouts are downstream of a pricing-engine CPU saturation caused by a config/feature-flag change, not the market-data-service deploy. At 12:04:00, config change cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5% to 100% ('Ramp Greeks v2 experiment to all traffic'). Within ~1 minute pricing-engine CPU rose from ~40% to ~99%, p99 latency from ~60ms to ~2600ms, and error_rate from ~0.2% to ~12%, with 276 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors and GreeksV2 Monte-Carlo warnings of 3-9s per call. The 5%->100% ramp exposed an expensive Monte-Carlo calculator (20k paths) to all quote traffic, pushing per-request compute past deadlines and beyond order-service's 2500ms client timeout (order errors begin 12:06:10). market-data-service (deployed 11:56, v5.2.0 protobuf) is healthy — flat ~40ms latency and ~45% CPU — and request_rate is flat ~645 req/s, ruling out the deploy and load growth. Restarts=0, memory flat, redis flat. Recommended: revert cfg-3120 to 5% or 0% to restore service immediately.

## Probable root cause
**Greeks v2 feature-flag ramp saturated pricing-engine CPU** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Greeks v2 feature-flag ramp saturated pricing-engine CPU | config_change | pricing-engine | cfg-3120 | 0.90 (high) | active |
| 2 | H2: market-data-service protobuf deploy broke quote serialization | bad_deployment | market-data-service | dep-6111 | 0.05 (low) | rejected |
| 3 | H3: Load-driven capacity saturation of pricing-engine | capacity_saturation | pricing-engine | - | 0.03 (low) | rejected |

### H1: Greeks v2 feature-flag ramp saturated pricing-engine CPU
Config change cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5% to 100% at 12:04, exposing the expensive GreeksV2 Monte-Carlo calculator (20k paths, ~3-9s per call) to all quote traffic. The added CPU work drove pricing-engine to ~99% CPU, inflating quote latency to ~2.6s past the 2000ms budget and beyond order-service's 2500ms client timeout, causing order quote timeouts.

**Causal chain**
1. cfg-3120 ramps greeks_v2 rollout 5%->100% at 12:04:00 [F4]
2. GreeksV2 Monte-Carlo warnings begin at 12:05:21 and config applied log at 12:04:16 [F5]
3. pricing-engine CPU jumps from ~40% to ~99% at 12:05 [F1]
4. pricing-engine p99 jumps from ~60ms to ~2600ms and error_rate to ~12% [F2, F3, F14]
5. order-service quote calls to pricing-engine time out at 2500ms starting 12:06 [F7]

**Supporting evidence**
  - `F1` ✔ pricing-engine cpu_pct was stable at ~40% until the 12:00 bucket, then jumped to ~99% (max 100.0) from the 12:05 bucket onward and stayed there.
  - `F2` ✔ pricing-engine latency_p99_ms was ~60ms until 12:00, then jumped to ~2600ms from the 12:05 bucket (avg 2591.16, max 2849.25).
  - `F3` ✔ pricing-engine error_rate rose from ~0.2% before 12:00 to ~10-13% from the 12:05 bucket (avg 10.37, max 13.61).
  - `F4` ✔ A config change cfg-3120 on pricing-engine changed flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z (author pm-experiments, 'Ramp Greeks v2 experiment to all traffic').
  - `F5` ✔ pricing-engine logs show the config being applied at 12:04:16 and GreeksV2 Monte-Carlo warnings beginning at 12:05:21 (e.g. 'took 6465ms').
  - `F6` ✔ In the 8 hours before 12:05, pricing-engine emitted only 2 WARN entries (TLS cipher, slow /internal/metrics) and no GreeksV2 Monte-Carlo warnings, so the expensive calculation was not running broadly before the ramp.
  - `F10` ✔ pricing-engine request_rate stayed flat ~645 req/s (min 571.46, max 741.12) with no surge across the window.
  - `F14` ✔ pricing-engine logs show 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' as the dominant error pattern (276 occurrences).
**Contradicting evidence**
  - `F13` ✔ The only deployment in the incident window was market-data-service v5.2.0 'Switch quote serialization to protobuf' at 11:56:00Z, and it was not rolled back; pricing-engine had no deployments in the window.
**Assumptions**
  - The GreeksV2 calculator is markedly more CPU-intensive than the default Greeks path (inferred from 3-9s compute times and the CPU jump coinciding with the ramp).

### H2: market-data-service protobuf deploy broke quote serialization
market-data-service v5.2.0 'Switch quote serialization to protobuf' at 11:56 introduced a serialization fault that slowed decoding on pricing-engine, causing quote timeouts.

**Causal chain**
1. dep-6111 shipped at 11:56 [F13]
2. quote timeouts appear shortly after [F7]

**Supporting evidence**
  - `F13` ✔ The only deployment in the incident window was market-data-service v5.2.0 'Switch quote serialization to protobuf' at 11:56:00Z, and it was not rolled back; pricing-engine had no deployments in the window.
**Contradicting evidence**
  - `F8` ✔ market-data-service latency_p99_ms stayed flat ~39-41ms across the entire window, including after its v5.2.0 deploy at 11:56.
  - `F9` ✔ market-data-service cpu_pct stayed flat ~45% across the window (min 39.91, max 49.97).
  - `F1` ✔ pricing-engine cpu_pct was stable at ~40% until the 12:00 bucket, then jumped to ~99% (max 100.0) from the 12:05 bucket onward and stayed there.
  - `F4` ✔ A config change cfg-3120 on pricing-engine changed flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z (author pm-experiments, 'Ramp Greeks v2 experiment to all traffic').
  - `F5` ✔ pricing-engine logs show the config being applied at 12:04:16 and GreeksV2 Monte-Carlo warnings beginning at 12:05:21 (e.g. 'took 6465ms').
**Assumptions**
  - None material.
**Rejected because:** The deploy preceded the fault by ~10 minutes while CPU/latency/errors stayed normal until 12:04-12:05; market-data-service latency and CPU are completely flat (F8,F9); and the fault onsets exactly with the cfg-3120 ramp (F4,F5). Serialization on market-data would also not inflate pricing-engine CPU to 99%.

### H3: Load-driven capacity saturation of pricing-engine
A surge in quote request volume exceeded provisioned pricing-engine capacity, saturating CPU and causing timeouts.

**Causal chain**
1. traffic surge saturates CPU *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ pricing-engine request_rate stayed flat ~645 req/s (min 571.46, max 741.12) with no surge across the window.
  - `F4` ✔ A config change cfg-3120 on pricing-engine changed flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z (author pm-experiments, 'Ramp Greeks v2 experiment to all traffic').
  - `F5` ✔ pricing-engine logs show the config being applied at 12:04:16 and GreeksV2 Monte-Carlo warnings beginning at 12:05:21 (e.g. 'took 6465ms').
**Assumptions**
  - None material.
**Rejected because:** request_rate is flat ~645 req/s with no surge (F10); the CPU jump is coincident with the feature-flag ramp, not a volume change.

## Facts (verified observations)
- pricing-engine CPU jumped from ~40% to ~99% at the 12:05 bucket and stayed there. [F1]
- pricing-engine p99 latency jumped from ~60ms to ~2600ms at the 12:05 bucket. [F2]
- pricing-engine error_rate jumped from ~0.2% to ~12% at the 12:05 bucket. [F3]
- Config change cfg-3120 set greeks_v2.rollout_pct 5->100 at 12:04:00. [F4]
- The config was applied at 12:04:16 and GreeksV2 Monte-Carlo warnings began at 12:05:21. [F5]
- order-service quote timeouts (2500ms) begin at 12:06:10. [F7]
- market-data-service latency and CPU are flat across the window despite its 11:56 deploy. [F8, F9]
- pricing-engine request_rate is flat ~645 req/s with no surge. [F10]
- pricing-engine had 0 restarts and flat memory; redis-cache CPU flat. [F11, F12]
- The only deployment in the window was market-data-service v5.2.0 at 11:56 (not rolled back); pricing-engine had no deployments. [F13]

## Assumptions (not verified)
- The GreeksV2 calculator is substantially more CPU-intensive than the previously-dominant (v1/default) path; inferred from its 3-9s per-call times and the CPU jump coinciding with the flag ramp.
- The config-audit gap (shard 2/3) did not hide another relevant change; pricing-engine-specific config query returned only cfg-3120.
- order-service timeouts are entirely attributable to pricing-engine latency exceeding the 2500ms client timeout.

## Timeline
- `2026-05-20T11:56:00Z` market-data-service v5.2.0 (protobuf quote serialization) deployed; no degradation follows. [F13, F8]
- `2026-05-20T12:04:00Z` Config change cfg-3120 ramps flags.pricing.greeks_v2.rollout_pct 5 -> 100. [F4]
- `2026-05-20T12:04:16Z` pricing-engine logs 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100'. [F5]
- `2026-05-20T12:05:00Z` pricing-engine CPU ~99%, p99 ~2600ms, error_rate ~12%. [F1, F2, F3]
- `2026-05-20T12:05:21Z` First GreeksV2 Monte-Carlo warning (6465ms). [F5]
- `2026-05-20T12:06:10Z` First order-service 'pricing-engine quote call timed out after 2500ms' error. [F7]
- `2026-05-20T13:59:00Z` Conditions persist unchanged (CPU ~99%, p99 ~2600ms); no recovery observed. [F1, F2]

## Recommended actions
- **[immediate]** Revert cfg-3120: set flags.pricing.greeks_v2.rollout_pct back to 5 (or 0) immediately. -- The ramp is the trigger; reverting removes the CPU-heavy Monte-Carlo path from the bulk of traffic and should restore quote latency. [F1, F4, F5]
- **[immediate]** Confirm recovery by watching pricing-engine cpu_pct, latency_p99_ms and error_rate for several minutes after revert. -- CPU dropped instantly from ~40% to ~99% at the ramp, so it should return promptly; sustained ~99% would indicate a different cause. [F1, F2, F3]
- **[short_term]** Do not roll back market-data-service v5.2.0 for this symptom; it is healthy. -- market-data latency/CPU are flat and its deploy precedes the onset by ~10 minutes. [F8, F9, F13]
- **[short_term]** Gate the GreeksV2 flag behind a performance/canary review: benchmark the Monte-Carlo calculator cost and ramp gradually with CPU/latency SLOs. -- A 5%->100% jump exposed a path ~40x more CPU-expensive than the default; gradual ramp with SLO gates prevents recurrence. [F4, F5]
- **[long_term]** Add alerting on pricing-engine CPU saturation and quote latency SLO before feature-flag ramps. -- Saturation reached 99% with ~2.6s p99 before being reported. [F1, F2]

## Data gaps
- The first all-service config_changes query (tc-003) was partial: config-audit shard 2/3 did not respond, so an unrelated config change elsewhere cannot be fully excluded (mitigated by the pricing-engine-scoped query returning only cfg-3120).
- No direct profile breakdown proving the GreeksV2 calculator consumes the majority of pricing-engine CPU; this is inferred from timing correlation rather than a CPU-by-function metric.

## Investigation stats
- latency 45.68s, LLM calls 6, tokens 89253 (in 81242 / out 8011)
- tool calls 24 (0 failed, 1 partial, 0 recovered by retry), findings 14/14 verified
