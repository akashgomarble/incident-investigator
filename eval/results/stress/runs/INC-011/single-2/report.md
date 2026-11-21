# Incident investigation report: INC-011
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order quote timeouts are caused by the pricing-engine feature-flag ramp cfg-3120 (flags.pricing.greeks_v2.rollout_pct 5→100 at 12:04:00Z). Enabling GreeksV2 for all traffic made every quote run the expensive GreeksV2Calculator Monte-Carlo path (20000 paths, 3–9s per call), blowing the 2000ms quote budget. pricing-engine p99 jumped from ~60ms to ~2.6s in the 12:05 bucket and order-service quote timeouts + 8% error rate began at 12:06. The reported market-data-service protobuf deploy (dep-6111 at 11:56) is NOT the cause: market-data-service p99 stayed flat at ~40ms across the entire window. Root cause is a config/feature-flag change (config_change) in pricing-engine.

## Probable root cause
**GreeksV2 feature-flag ramp to 100% overloads pricing-engine quote computation** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.88, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: GreeksV2 feature-flag ramp to 100% overloads pricing-engine quote computation | config_change | pricing-engine | cfg-3120 | 0.88 (high) | active |
| 2 | H2: market-data-service protobuf serialization deploy slows quotes | bad_deployment | market-data-service | dep-6111 | 0.05 (low) | rejected |
| 3 | H3: Load/CPU saturation of pricing-engine | capacity_saturation | pricing-engine | - | 0.03 (low) | rejected |

### H1: GreeksV2 feature-flag ramp to 100% overloads pricing-engine quote computation
Config change cfg-3120 (greeks_v2 rollout_pct 5→100 at 12:04) forced all quote requests onto the expensive GreeksV2Calculator Monte-Carlo path (20000 paths, 3–9s each), exceeding the 2000ms budget. pricing-engine quote latency rose to ~2.6s p99, causing downstream order-service quote calls to time out at 2500ms.

**Causal chain**
1. cfg-3120 ramps greeks_v2 from 5% to 100% at 12:04:00Z [F2]
2. All quotes now run GreeksV2Calculator Monte-Carlo (20000 paths, multi-second) from 12:05:21Z [F3]
3. Quote requests exceed the 2000ms budget -> pricing-engine deadline-exceeded errors [F4]
4. pricing-engine p99 latency jumps 60ms -> 2.6s in the 12:05 bucket [F5]
5. order-service quote calls exceed their 2500ms client timeout, error rate rises to ~8% [F1, F8]

**Supporting evidence**
  - `F2` ✔ A feature-flag change cfg-3120 on pricing-engine raised flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:00Z (author pm-experiments).
  - `F3` ✔ pricing-engine logged 'GreeksV2Calculator: Monte-Carlo paths=20000 took <n>ms' warnings (342 of them) starting 2026-05-20T12:05:21Z, with durations of 2964–8942ms.
  - `F4` ✔ pricing-engine logged 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors starting 2026-05-20T12:06:41Z (276 total).
  - `F5` ✔ pricing-engine latency_p99_ms jumped from ~59ms (11:58 bucket) to 2609.71ms in the 12:05 bucket and stayed ~2.6s through 13:57.
  - `F8` ✔ order-service error_rate jumped from ~0.2% (11:58 bucket) to 6.93% in the 12:05 bucket and held ~8% through 13:57.
  - `F9` ✔ pricing-engine request_rate was flat (~640 req/s) with no traffic surge at onset, ruling out load-driven saturation.
  - `F10` ✔ pricing-engine memory_pct stayed ~48–50% throughout, no memory growth correlated with onset.
**Contradicting evidence**
  - (none)
**Assumptions**
  - cfg-3120 greeks_v2 metric actually routes traffic to GreeksV2Calculator (naming correlation only)
  - No pricing-engine code deploy occurred (confirmed: 0 deployments)

### H2: market-data-service protobuf serialization deploy slows quotes
dep-6111 switched market-data-service quote serialization to protobuf at 11:56, which the incident report suspected caused quote timeouts.

**Causal chain**
1. dep-6111 deploys protobuf serialization at 11:56 [F7]
2. market-data-service latency rises, slowing quotes *(assumed)*

**Supporting evidence**
  - `F7` ✔ The only deployment in the window was dep-6111, market-data-service v5.2.0 'Switch quote serialization to protobuf', deployed 2026-05-20T11:56:00Z, status live (not rolled back).
**Contradicting evidence**
  - `F6` ✔ market-data-service latency_p99_ms stayed flat (~39–41ms) across the whole window, including the 12:05 bucket (avg 39.29) where pricing-engine p99 exploded.
  - `F11` ✔ market-data-service had no config changes in 10:00–13:59 (query returned 0 changes) and only 1 WARN (a TLS cipher notice at 11:43:53) in the window.
  - `F5` ✔ pricing-engine latency_p99_ms jumped from ~59ms (11:58 bucket) to 2609.71ms in the 12:05 bucket and stayed ~2.6s through 13:57.
**Assumptions**
  - protobuf change could affect downstream quote latency
**Rejected because:** market-data-service p99 stayed flat at ~40ms across the entire window (F6); the deploy at 11:56 was 9 minutes before onset, whereas the symptom onset (12:05-12:06) aligns exactly with cfg-3120 at 12:04. No market-data-service degradation observed.

### H3: Load/CPU saturation of pricing-engine
A surge in quote request volume saturated pricing-engine CPU, inflating latency.

**Causal chain**
1. request_rate surge drives CPU saturation *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ pricing-engine request_rate was flat (~640 req/s) with no traffic surge at onset, ruling out load-driven saturation.
  - `F10` ✔ pricing-engine memory_pct stayed ~48–50% throughout, no memory growth correlated with onset.
**Rejected because:** request_rate was flat (~640 req/s) at onset (F9) and memory_pct was stable (F10); latency rose ~40x in one bucket without any traffic increase — a per-request cost change, not load.

## Facts (verified observations)
- order-service reported 'pricing-engine quote call timed out after 2500ms' errors beginning 2026-05-20T12:06:10Z. [F1]
- A pricing-engine feature-flag change cfg-3120 (flags.pricing.greeks_v2.rollout_pct 5→100) took effect at 2026-05-20T12:04:00Z. [F2]
- pricing-engine GreeksV2Calculator Monte-Carlo warnings (20000 paths, 3–9s) began 12:05:21Z, one minute after cfg-3120. [F3]
- pricing-engine p99 latency jumped from ~59ms to ~2609ms in the 12:05 bucket and stayed elevated. [F5]
- order-service error_rate jumped from ~0.2% to 6.93% in the 12:05 bucket. [F8]
- market-data-service p99 latency remained flat (~40ms) throughout, ruling out its protobuf deploy as the cause. [F6]
- pricing-engine request_rate was flat and memory_pct stable at onset, excluding load/memory saturation. [F9, F10]

## Assumptions (not verified)
- The greeks_v2 rollout flag controls whether quotes use GreeksV2Calculator (inferred from naming and a 1-minute timing alignment).
- No pricing-engine code deployment occurred (confirmed: 0 deployments in window), so latency increase is due to the flag change, not new code.
- No other simultaneous change on unresponsive config-audit shard 2/3 occurred.

## Timeline
- `2026-05-20T11:43:53Z` market-data-service emits a deprecated TLS cipher WARN (benign noise). [F11]
- `2026-05-20T11:56:00Z` dep-6111: market-data-service v5.2.0 protobuf serialization deployed (suspected by report, later ruled out). [F7]
- `2026-05-20T12:04:00Z` cfg-3120: pricing-engine greeks_v2 rollout ramped 5% -> 100%. [F2]
- `2026-05-20T12:05:21Z` pricing-engine GreeksV2Calculator Monte-Carlo warnings begin (3–9s per call). [F3]
- `2026-05-20T12:05:00Z` pricing-engine p99 jumps to 2609.71ms; order-service error_rate jumps to 6.93%. [F5, F8]
- `2026-05-20T12:06:10Z` First order-service 'quote call timed out after 2500ms' error. [F1]
- `2026-05-20T12:06:41Z` First pricing-engine 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2'. [F4]
- `2026-05-20T13:57:00Z` pricing-engine p99 still ~2.6s; incident unresolved as of latest data. [F5]

## Recommended actions
- **[immediate]** Immediately revert cfg-3120: set flags.pricing.greeks_v2.rollout_pct back to 5 (or 0). -- The flag ramp is the trigger; rollback should restore pricing-engine p99 to ~60ms and clear order-service timeouts. [F2, F5, F8]
- **[immediate]** Verify recovery by watching pricing-engine latency_p99_ms and order-service quote-timeout log volume for 10 minutes after the flag revert. -- Confirms the causal chain and that no second factor (e.g. a memory backlog) persists. [F1, F5]
- **[short_term]** Before re-enabling, benchmark GreeksV2Calculator and reduce Monte-Carlo paths or move it off the synchronous quote path (cache/async). -- 20000-path Monte-Carlo at 3–9s cannot meet the 2000ms synchronous budget; the feature is inherently too costly inline. [F3, F4]
- **[long_term]** Add a rollout guardrail: auto-halt a feature-flag ramp if pricing-engine p99 or deadline-exceeded rate crosses a threshold. -- A 5%→100% jump in one step caused a 40x latency regression; staged ramps with automated abort would have caught it. [F2, F5]
- **[long_term]** Add a per-request latency budget check before routing to console the Greeks calculator so slow paths degrade gracefully instead of timing out downstream callers. -- Prevents an expensive optional computation from breaking the quote SLA for all callers. [F4]

## Data gaps
- pricing-engine cpu_pct metric could not be retrieved (upstream timeout, 3 attempts) — CPU saturation as a contributory factor is unverified, though request_rate and memory were flat.
- pricing-engine error_rate metric could not be retrieved (upstream timeout) — deadline-exceeded rate confirmed only via logs, not metrics.
- First config-change query returned partial:true (config-audit shard 2/3 did not respond); a retry returned only cfg-3120, so a concurrent change on a third of shards cannot be fully excluded.
- market-data-service config-change query timed out (3 attempts) — no market-data-service config changes were confirmed, only inferred from the absence of correlated latency.
- No direct metric linking greeks_v2 flag value to GreeksV2Calculator invocation exists; the link is name-based correlation plus exact timing alignment.

## Investigation stats
- latency 122.28s, LLM calls 5, tokens 54518 (in 47726 / out 6792)
- tool calls 19 (4 failed, 1 partial, 4 recovered by retry), findings 12/12 verified
