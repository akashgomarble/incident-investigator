# Incident investigation report: INC-011
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order quote timeouts originate in pricing-engine, not order-service or market-data-service. At 12:04 the feature-flag change cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100, routing all quotes through the GreeksV2 Monte-Carlo calculator (20000 paths, 3-9s per computation). pricing-engine CPU pinned at ~99%, quote p99 rose from ~60ms to ~2.6s, exceeding the 2000ms compute budget (94 'deadline exceeded' errors), which surfaced as 2500ms quote-call timeouts and ~8% errors in order-service from 12:06. The market-data-service protobuf deploy (dep-6111, 11:56) is exonerated: its latency and error rate never moved, and pricing-engine had no deployment. Root cause category: config_change. Recommend reverting cfg-3120 (roll greeks_v2 back to 5%) immediately.

## Probable root cause
**greeks_v2 flag ramp made quote computation CPU-bound, saturating pricing-engine** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: greeks_v2 flag ramp made quote computation CPU-bound, saturating pricing-engine | config_change | pricing-engine | cfg-3120 | 0.90 (high) | active |
| 2 | H2: market-data-service protobuf deploy corrupted quote payloads | bad_deployment | market-data-service | dep-6111 | 0.05 (low) | rejected |
| 3 | H3: Load outgrew pricing-engine capacity | capacity_saturation | pricing-engine | - | 0.03 (low) | rejected |

### H1: greeks_v2 flag ramp made quote computation CPU-bound, saturating pricing-engine
Config change cfg-3120 ramped pricing-engine's flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04, routing all quotes through the expensive GreeksV2 Monte-Carlo calculator (20000 paths, 3-9s each). This drove pricing-engine CPU to ~99%, pushing quote latency p99 to ~2.6s and exceeding the 2000ms compute budget, which propagated as 2500ms quote timeouts and ~8% errors in order-service. Fault originates in pricing-engine (triggered by a config/feature-flag change, not a release).

**Causal chain**
1. cfg-3120 ramps greeks_v2 flag 5->100 at 12:04 [F1]
2. All quotes now use GreeksV2 Monte-Carlo (20000 paths, 3-9s) [F4]
3. pricing-engine CPU saturates to ~99% [F3]
4. Quote p99 jumps to ~2.6s, exceeding 2000ms budget [F2, F5]
5. order-service 2500ms quote calls time out, ~8% errors [F6, F7]

**Supporting evidence**
  - `F1` ✔ Config change cfg-3120 ramped pricing-engine feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z, by author pm-experiments.
  - `F2` ✔ pricing-engine p99 latency was ~60ms until the 11:50 bucket, then jumped to avg 1325ms/max 2849ms in the 12:00 bucket and stayed ~2600ms through 13:50.
  - `F3` ✔ pricing-engine CPU was ~40% until the 11:50 bucket, then rose to avg 69.4%/max 100% in the 12:00 bucket and pinned at ~99% thereafter.
  - `F4` ✔ pricing-engine logs show GreeksV2Calculator Monte-Carlo runs of 20000 paths taking 2964-8942ms, first seen 2026-05-20T12:05:21Z.
  - `F5` ✔ pricing-engine logs show 94 ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' from 12:06:41Z.
  - `F6` ✔ order-service logs show 76 ERROR 'pricing-engine quote call timed out after 2500ms' starting 2026-05-20T12:06:10Z.
  - `F7` ✔ order-service error rate was ~0.2% until the 11:50 bucket, then rose to avg 3.38%/max 8.37% in the 12:00 bucket and held ~8% thereafter.
  - `F8` ✔ pricing-engine request_rate stayed flat across the incident window (~631 req/s pre-onset, ~644 req/s after), i.e. no traffic surge.
  - `F13` ✔ pricing-engine had no code deployments in the window (count 0), so its fault was not introduced by a release.
  - `F14` ✔ pricing-engine error rate rose from ~0.2% to ~12.5% starting the 12:00 bucket, coincident with the greeks_v2 ramp.
  - `F15` ✔ First sign of trouble (order-service quote timeout) at 12:06:10Z, ~2 min after the 12:04 config change and ~10 min after the 11:56 MDS deploy.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 2000ms compute budget is a fixed config unrelated to the flag; the calculator choice (GreeksV2) is what changed

### H2: market-data-service protobuf deploy corrupted quote payloads
dep-6111 (market-data-service v5.2.0 protobuf serialization, 11:56) caused quote failures by changing payload format for internal callers.

**Causal chain**
1. dep-6111 ships protobuf quotes at 11:56 [F11]
2. Downstream quote handling degrades *(assumed)*

**Supporting evidence**
  - `F11` ✔ Only code deployment in the window is dep-6111: market-data-service v5.2.0 'Switch quote serialization to protobuf' deployed at 2026-05-20T11:56:00Z, status live, not rolled back.
  - `F15` ✔ First sign of trouble (order-service quote timeout) at 12:06:10Z, ~2 min after the 12:04 config change and ~10 min after the 11:56 MDS deploy.
**Contradicting evidence**
  - `F9` ✔ market-data-service latency_p99 stayed flat ~40ms across the entire window including after its 11:56 deploy.
  - `F10` ✔ market-data-service error rate stayed flat ~0.2% across the window.
  - `F2` ✔ pricing-engine p99 latency was ~60ms until the 11:50 bucket, then jumped to avg 1325ms/max 2849ms in the 12:00 bucket and stayed ~2600ms through 13:50.
  - `F13` ✔ pricing-engine had no code deployments in the window (count 0), so its fault was not introduced by a release.
  - `F4` ✔ pricing-engine logs show GreeksV2Calculator Monte-Carlo runs of 20000 paths taking 2964-8942ms, first seen 2026-05-20T12:05:21Z.
  - `F5` ✔ pricing-engine logs show 94 ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' from 12:06:41Z.
**Rejected because:** market-data-service latency (~40ms) and error rate (~0.2%) never moved (F9, F10); pricing-engine had no deployment (F13); the failures are compute-bound GreeksV2 Monte-Carlo runs (F4, F5) starting after the 12:04 config change, not payload/deserialization errors.

### H3: Load outgrew pricing-engine capacity
A market-open traffic surge exceeded provisioned pricing-engine capacity, causing CPU saturation and timeouts (as in HIST-113).

**Causal chain**
1. Traffic surge saturates pricing-engine *(assumed)*

**Supporting evidence**
  - `F3` ✔ pricing-engine CPU was ~40% until the 11:50 bucket, then rose to avg 69.4%/max 100% in the 12:00 bucket and pinned at ~99% thereafter.
**Contradicting evidence**
  - `F8` ✔ pricing-engine request_rate stayed flat across the incident window (~631 req/s pre-onset, ~644 req/s after), i.e. no traffic surge.
  - `F1` ✔ Config change cfg-3120 ramped pricing-engine feature flag flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z, by author pm-experiments.
**Rejected because:** pricing-engine request_rate stayed flat (~631->644 req/s, F8); demand never rose. What changed was per-request compute cost, driven by the greeks_v2 flag (F1), not load.

## Facts (verified observations)
- cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z. [F1]
- pricing-engine p99 latency jumped from ~60ms to ~2600ms at the 12:00 bucket. [F2]
- pricing-engine CPU jumped from ~40% to ~99% at the 12:00 bucket. [F3]
- pricing-engine logs show GreeksV2Calculator Monte-Carlo runs of 20000 paths taking up to 8942ms. [F4]
- pricing-engine logged 94 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors. [F5]
- order-service logged 76 'pricing-engine quote call timed out after 2500ms' errors starting 12:06:10Z. [F6]
- order-service error rate jumped from ~0.2% to ~8% at the 12:00 bucket. [F7]
- pricing-engine request_rate remained flat (~631-644 req/s); no traffic surge. [F8]
- market-data-service latency (~40ms) and error rate (~0.2%) never changed across the window. [F9, F10]
- The only deployment in the window is market-data-service dep-6111 (protobuf) at 11:56; pricing-engine had zero deployments. [F11, F13]
- pricing-engine memory stayed flat ~49%, so no memory leak/GC cause. [F12]
- pricing-engine error rate rose to ~12.5% starting the 12:00 bucket. [F14]

## Assumptions (not verified)
- The 2000ms compute budget is fixed service config independent of the flag, so the calculator (GreeksV2) choice is the changed variable.
- Order-service/quote traffic and exchange-gateway/external providers were healthy; no evidence of exchange-api degradation was found (its telemetry is unavailable).

## Timeline
- `2026-05-20T11:56:00Z` market-data-service v5.2.0 (protobuf quotes) deployed (dep-6111). [F11]
- `2026-05-20T12:00:00Z` pricing-engine CPU jumps to ~99% and p99 latency to ~2.6s (bucket). [F2, F3]
- `2026-05-20T12:04:00Z` Config change cfg-3120 ramps greeks_v2 rollout from 5% to 100%. [F1]
- `2026-05-20T12:05:21Z` First GreeksV2Calculator Monte-Carlo 20000-path WARN (6465ms). [F4]
- `2026-05-20T12:06:10Z` First order-service 'quote call timed out after 2500ms' error. [F6]
- `2026-05-20T12:06:41Z` First 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2'. [F5]
- `2026-05-20T12:16:00Z` INC-011 reported (SEV-2). []

## Recommended actions
- **[immediate]** Revert config cfg-3120: roll flags.pricing.greeks_v2.rollout_pct back to 5 (or 0) to restore fast quote computation. -- The flag ramp at 12:04 is the trigger; reverting removes the expensive Monte-Carlo path and should restore pricing-engine CPU/latency. [F1, F4, F2]
- **[immediate]** Verify no other service deploys/config changes are pending; treat dep-6111 (market-data-service) as a red herring. -- MDS telemetry was flat, so rollback of dep-6111 is unnecessary and would be a distraction. [F9, F10, F11]
- **[short_term]** Before re-enabling greeks_v2, add a cost/latency guard: cap Monte-Carlo paths or add a fast-fail + load test so a 5->100 ramp cannot pin CPU. -- The v2 calculator at 20000 paths exceeds the 2000ms budget even at normal request rates, so any full ramp will recur. [F4, F5, F8]
- **[long_term]** Add gating/auto-rollback on feature-flag ramps tied to pricing-engine CPU and quote p99 SLOs. -- A gradual canary on the compute-heavy flag would have caught this before full rollout. [F2, F3, F1]

## Data gaps
- One config-audit shard (2/3) did not respond on the first call (tc-003, partial); a pricing-engine-scoped retry (tc-004) succeeded, but other services' config changes cannot be fully ruled out.
- exchange-api (external vendor) has has_telemetry=false, so external-provider health could not be checked directly and remains an unverified alternative.

## Investigation stats
- latency 79.46s, LLM calls 7, tokens 82810 (in 72551 / out 10259)
- tool calls 17 (0 failed, 1 partial, 0 recovered by retry), findings 15/15 verified
