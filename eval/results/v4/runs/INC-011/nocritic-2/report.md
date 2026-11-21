# Incident investigation report: INC-011
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The order-service quote timeouts (INC-011) are caused by pricing-engine CPU saturation induced by the cfg-3120 feature-flag ramp: flags.pricing.greeks_v2.rollout_pct was pushed from 5 to 100 at 12:04:00Z, after which all quote traffic ran through the expensive GreeksV2 Monte-Carlo calculator (paths=20000). CPU rose from ~40% to 98-99.5%, p99 jumped from ~60ms to ~2.6s, and quote budget overruns (2000ms) propagated to order-service's 2500ms client timeout. The reporter's suspicion of the market-data-service protobuf deploy is not supported: that service is flat/healthy across the whole window and its deploy at 11:56 precedes the 12:04 onset by 8 minutes. Confidence is medium (0.60) and the top hypothesis is clearly ahead of the alternatives, but sub-minute ordering of the flag change vs. the latency jump and the absence of GC/thread telemetry leave residual uncertainty.

## Probable root cause
**GreeksV2 flag ramp to 100% made pricing-engine compute-bound (CPU saturation) → quote timeouts** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: GreeksV2 flag ramp to 100% made pricing-engine compute-bound (CPU saturation) → quote timeouts | config_change | pricing-engine | cfg-3120 | 0.60 (medium) | active |
| 2 | H2: market-data-service protobuf serialization deploy (v5.2.0) introduced a costly/large quote payload that pricing-engine can't deserialize efficiently | bad_deployment | market-data-service | dep-6111 | 0.10 (low) | active |
| 3 | H4: Organic market-open/load-driven capacity saturation on pricing-engine (independent of any change) | capacity_saturation | pricing-engine | null | 0.08 (low) | active |
| 4 | H3: Shared-resource contention (redis-cache / ledger-db / kafka / shared node) degraded pricing-engine independently of the flag | resource_exhaustion | shared infrastructure (redis-cache / ledger-db / kafka / node) | null | 0.05 (low) | active |

### H1: GreeksV2 flag ramp to 100% made pricing-engine compute-bound (CPU saturation) → quote timeouts
cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04:00Z, pushing all quote traffic through the expensive GreeksV2 Monte-Carlo calculator (paths=20000). The added compute load saturated pricing-engine CPU (~98-99.5% from 12:06 onward), slow calculations blew the 2000ms quote budget, p99 rose to ~2.6s, and order-service's 2500ms client timeout was exceeded, producing quote timeouts. Origin: pricing-engine config change (config_change), not the market-data-service serialization deploy.

**Causal chain**
1. cfg-3120 ramps greeks_v2.rollout_pct 5 -> 100 at 12:04:00Z, sending essentially all quote traffic through the GreeksV2 calculator [F8, F7]
2. GreeksV2 Monte-Carlo computations (paths=20000) take 6.5-8.9s, appearing first at 12:05:21Z [F4]
3. Compute load saturates pricing-engine CPU to a flat ~98-99.5% (max 100.0) from 12:06 onward [F3, F4]
4. p99 latency jumps to 1705ms in the 12:04 bucket and holds ~2532-2746ms through 13:58 [F1, F3]
5. Requests blow the 2000ms quote budget: 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' from 12:06:41Z, ~12.5% 5xx [F4, F2]
6. order-service's quote call times out after 2500ms, first error 12:06:10Z, 225 errors through 13:58 [F5]

**Supporting evidence**
  - `F8` ✔ The only config/flag change in the window is in pricing-engine: cfg-3120 key 'flags.pricing.greeks_v2.rollout_pct' changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'). Its timestamp is the exact same bucket/minute as the p99 latency jump (F1) and the CPU jump (F3), and matches the GreeksV2 calculator in pricing-engine's errors (F4).
  - `F4` ✔ pricing-engine logs show the earliest anomaly at 12:05:21Z: GreekV2 Monte-Carlo computations (paths=20000) taking 6.5-8.9s, escalating into 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors from 12:06:41Z; 342 slow-calc WARNs and 276 deadline-exceeded ERRORs total. Note the calculator in the error is GreeksV2, not a serialization path.
  - `F3` ✔ pricing-engine CPU saturation coincides with the latency jump: ~40% baseline through the 12:02 bucket, then 68.44% (max 98.65) in the 12:04 bucket and a flat ~98-99.5% (max 100.0) from 12:06 onward.
  - `F1` ✔ pricing-engine p99 latency jumped from a stable ~58-61ms baseline to 1705ms in the 12:04 bucket (bucket spans 12:04-12:07) and stayed at ~2532-2746ms for the entire rest of the window through 13:58 (~2.6s ongoing, not recovered).
  - `F2` ✔ pricing-engine HTTP 5xx error_rate rose from ~0.2% baseline to 4.47% in the same 12:04 bucket and to ~12-13% from 12:07 onward, holding ~12.5% through 13:58 (ongoing).
  - `F5` ✔ order-service's first ERROR is at 12:06:10Z, all 225 errors being the same signature 'pricing-engine quote call timed out after 2500ms' (plus 2 unrelated WARNs), spanning through 13:58:24Z; error volume concentrated, no serialization/deserialization or 5xx errors logged by order-service itself.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 12:04:00Z flag-change timestamp precedes (or is simultaneous with) the 12:04 p99/CPU jump; sub-minute ordering is unresolved (minute-granular flag timestamp vs bucket granularity).
  - The ~98-99.5% CPU is caused by GreeksV2 compute rather than GC pauses or thread saturation; no GC/thread/pool metrics were available (gap).
  - The remaining 5% of traffic pre-ramp plus 100% post-ramp is enough to raise CPU ~40% -> ~99% (i.e. GreeksV2 is dramatically more expensive than the default calculator).
  - cfg-3120 is the only relevant change to pricing-engine's behavior; no shared-library/infra change outside the deployment/config APIs coincided (dependency gap).

### H2: market-data-service protobuf serialization deploy (v5.2.0) introduced a costly/large quote payload that pricing-engine can't deserialize efficiently
market-data-service v5.2.0 'Switch quote serialization to protobuf' deployed 11:56:00Z changed the quote payload format/size; pricing-engine's handling of that payload became slow or partially failing, degrading p99 and driving CPU on the pricing-engine path. Origin: market-data-service deployment (bad_deployment). This is the reporter's suspicion.

**Causal chain**
1. market-data-service v5.2.0 switches quote serialization to protobuf at 11:56:00Z, 8 minutes before onset [F7]
2. pricing-engine consumes changed quote payloads on the market-data-service HTTP call path [F10, F7]
3. Deserialization/parsing of the new payload is slow or fails, raising pricing-engine latency and CPU *(assumed)*
4. quote budget (2000ms) exceeded and order-service 2500ms timeout tripped -> quote timeouts [F5]

**Supporting evidence**
  - `F7` ✔ The only deployment across ALL services in the window 11:30-13:59Z is market-data-service v5.2.0, 'Switch quote serialization to protobuf', deployed at 11:56:00Z by v.rao, status live, rolled_back_at null (never rolled back). No deployment exists for pricing-engine or order-service.
  - `F10` ✔ Dependency graph for order-service: it calls ledger-db (db, SHARED), pricing-engine (http), exchange-gateway (http) and produces to kafka (SHARED); api-gateway calls order-service. pricing-engine in turn calls market-data-service (http) and redis-cache (cache, SHARED). Shared components in the blast radius are ledger-db, redis-cache and kafka.
**Contradicting evidence**
  - `F6` ✔ market-data-service is flat/healthy across the incident: p99 steady ~38-43ms for the whole 11:40-13:58 window (min 35.28, max 43.75, mean 39.9), and CPU steady ~44-47% (max 49.97); its only log line 11:40-13:59 is a single unrelated 'deprecated TLS cipher' WARN at 11:43:53Z. It shows no latency or CPU anomaly.
  - `F4` ✔ pricing-engine logs show the earliest anomaly at 12:05:21Z: GreekV2 Monte-Carlo computations (paths=20000) taking 6.5-8.9s, escalating into 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors from 12:06:41Z; 342 slow-calc WARNs and 276 deadline-exceeded ERRORs total. Note the calculator in the error is GreeksV2, not a serialization path.
  - `F8` ✔ The only config/flag change in the window is in pricing-engine: cfg-3120 key 'flags.pricing.greeks_v2.rollout_pct' changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'). Its timestamp is the exact same bucket/minute as the p99 latency jump (F1) and the CPU jump (F3), and matches the GreeksV2 calculator in pricing-engine's errors (F4).
  - `F1` ✔ pricing-engine p99 latency jumped from a stable ~58-61ms baseline to 1705ms in the 12:04 bucket (bucket spans 12:04-12:07) and stayed at ~2532-2746ms for the entire rest of the window through 13:58 (~2.6s ongoing, not recovered).
  - `F3` ✔ pricing-engine CPU saturation coincides with the latency jump: ~40% baseline through the 12:02 bucket, then 68.44% (max 98.65) in the 12:04 bucket and a flat ~98-99.5% (max 100.0) from 12:06 onward.
**Assumptions**
  - A serialization change can inflate pricing-engine compute/latency without changing market-data-service's own latency or CPU.
  - The absence of any serialization/deserialization error in pricing-engine logs is due to such errors not being logged verbatim.

### H4: Organic market-open/load-driven capacity saturation on pricing-engine (independent of any change)
Incoming quote volume on pricing-engine outgrew provisioned compute capacity, driving CPU to ~99% and p99 to ~2.6s, so order-service quote calls time out. Origin: pricing-engine capacity saturation with no faulty triggering change — matches historical precedent HIST-113. Origin: capacity_saturation.

**Causal chain**
1. Quote request volume rises around 12:04 (market activity), approaching provisioned pricing-engine capacity *(assumed)*
2. pricing-engine CPU saturates to ~98-99.5% (flat) from 12:06 onward [F3]
3. p99 latency rises to ~2.6s and stays there (no recovery) [F1]
4. order-service 2500ms quote calls time out, 225 errors 12:06-13:58 [F5]

**Supporting evidence**
  - `F11` ✔ Historical precedent: HIST-113 (2026-01-28) 'pricing-engine CPU saturation at market open' has symptoms 'quote timeouts' and 'cpu 99%' on pricing-engine, category capacity_saturation, root cause 'Record market-open volume exceeded provisioned capacity'. This is the same symptom pair as the current p99 2.6s / ~98-99.5% CPU picture, but its stated cause (organic volume) differs from a flag ramp.
  - `F1` ✔ pricing-engine p99 latency jumped from a stable ~58-61ms baseline to 1705ms in the 12:04 bucket (bucket spans 12:04-12:07) and stayed at ~2532-2746ms for the entire rest of the window through 13:58 (~2.6s ongoing, not recovered).
  - `F3` ✔ pricing-engine CPU saturation coincides with the latency jump: ~40% baseline through the 12:02 bucket, then 68.44% (max 98.65) in the 12:04 bucket and a flat ~98-99.5% (max 100.0) from 12:06 onward.
  - `F5` ✔ order-service's first ERROR is at 12:06:10Z, all 225 errors being the same signature 'pricing-engine quote call timed out after 2500ms' (plus 2 unrelated WARNs), spanning through 13:58:24Z; error volume concentrated, no serialization/deserialization or 5xx errors logged by order-service itself.
**Contradicting evidence**
  - `F8` ✔ The only config/flag change in the window is in pricing-engine: cfg-3120 key 'flags.pricing.greeks_v2.rollout_pct' changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'). Its timestamp is the exact same bucket/minute as the p99 latency jump (F1) and the CPU jump (F3), and matches the GreeksV2 calculator in pricing-engine's errors (F4).
  - `F4` ✔ pricing-engine logs show the earliest anomaly at 12:05:21Z: GreekV2 Monte-Carlo computations (paths=20000) taking 6.5-8.9s, escalating into 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors from 12:06:41Z; 342 slow-calc WARNs and 276 deadline-exceeded ERRORs total. Note the calculator in the error is GreeksV2, not a serialization path.
**Assumptions**
  - Request volume increased organically; no request_rate data was retrieved to confirm a traffic surge.
  - The sustained ~99% CPU floor for ~2 hours is organic load rather than a change-induced constant-cost increase.

### H3: Shared-resource contention (redis-cache / ledger-db / kafka / shared node) degraded pricing-engine independently of the flag
A shared component in the blast radius (redis-cache accessed by pricing-engine, ledger-db or kafka shared with order-service, or CPU contention on a shared node/namespace) degraded, pushing pricing-engine p99 over SLO and causing quote timeouts. Origin: shared dependency/infrastructure degradation rather than pricing-engine code or config.

**Causal chain**
1. order-service and pricing-engine share ledger-db, kafka and redis-cache [F10]
2. A shared component (e.g. redis-cache cold-cache storm, or DB/broker pressure) begins degrading around 12:04 *(assumed)*
3. pricing-engine quote computations stall on the shared dependency, raising p99 and CPU *(assumed)*
4. order-service quote calls exceed 2500ms -> quote timeouts [F5]

**Supporting evidence**
  - `F10` ✔ Dependency graph for order-service: it calls ledger-db (db, SHARED), pricing-engine (http), exchange-gateway (http) and produces to kafka (SHARED); api-gateway calls order-service. pricing-engine in turn calls market-data-service (http) and redis-cache (cache, SHARED). Shared components in the blast radius are ledger-db, redis-cache and kafka.
  - `F12` ✔ Second relevant precedent is HIST-101 (auth-service, v6.4.0): a config-like change inside a release (bcrypt cost 10->14) produced high CPU + p99/503s and was resolved by rolling back the release; HIST-107 shows the shared redis-cache can cause quote latency via a cold-cache storm. Both are leads only, not evidence of the current cause.
**Contradicting evidence**
  - `F8` ✔ The only config/flag change in the window is in pricing-engine: cfg-3120 key 'flags.pricing.greeks_v2.rollout_pct' changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'). Its timestamp is the exact same bucket/minute as the p99 latency jump (F1) and the CPU jump (F3), and matches the GreeksV2 calculator in pricing-engine's errors (F4).
  - `F9` ✔ No config changes were found for order-service or redis-cache in 11:30-13:59Z (both counts 0), so no timeout/pool/TLS/DNS/resource-limit change is recorded for the callee chain or the shared cache.
  - `F1` ✔ pricing-engine p99 latency jumped from a stable ~58-61ms baseline to 1705ms in the 12:04 bucket (bucket spans 12:04-12:07) and stayed at ~2532-2746ms for the entire rest of the window through 13:58 (~2.6s ongoing, not recovered).
  - `F3` ✔ pricing-engine CPU saturation coincides with the latency jump: ~40% baseline through the 12:02 bucket, then 68.44% (max 98.65) in the 12:04 bucket and a flat ~98-99.5% (max 100.0) from 12:06 onward.
**Assumptions**
  - A shared-dependency stall can raise pricing-engine own CPU to ~99% rather than idle-waiting.
  - No config-change record exists because the triggering event is organic degradation or infra-level.

## Facts (verified observations)
- pricing-engine p99 latency jumped from a stable ~58-61ms baseline to 1705ms in the 12:04 bucket and stayed at ~2532-2746ms through 13:58, i.e. ~2.6s ongoing and unrecovered. [F1]
- pricing-engine HTTP 5xx error_rate rose from ~0.2% baseline to 4.47% in the 12:04 bucket and to ~12-13% from 12:07, holding ~12.5% through 13:58. [F2]
- pricing-engine CPU saturation coincides with the latency jump: ~40% baseline, 68.44% (max 98.65) in the 12:04 bucket, then flat ~98-99.5% (max 100.0) from 12:06 onward. [F3]
- pricing-engine logs show the earliest anomaly at 12:05:21Z with GreeksV2 Monte-Carlo computations (paths=20000) taking 6.5-8.9s, escalating into 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors from 12:06:41Z (342 slow-calc WARNs, 276 deadline-exceeded ERRORs total). The calculator in the error is GreeksV2, not a serialization path. [F4]
- order-service's first ERROR is at 12:06:10Z; all 225 errors share the signature 'pricing-engine quote call timed out after 2500ms' and span through 13:58:24Z, with no serialization/deserialization or 5xx errors logged by order-service itself. [F5]
- market-data-service is flat/healthy across the incident: p99 steady ~38-43ms for the whole 11:40-13:58 window and CPU steady ~44-47% (max 49.97); its only log line in the window is an unrelated TLS-cipher WARN at 11:43:53Z. [F6]
- The only deployment across all services in 11:30-13:59Z is market-data-service v5.2.0, 'Switch quote serialization to protobuf', deployed at 11:56:00Z (status live, never rolled back); there is no deployment for pricing-engine or order-service. [F7]
- The only config/flag change in the window is pricing-engine cfg-3120, key 'flags.pricing.greeks_v2.rollout_pct', changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'), timestamped in the exact same bucket as the p99 and CPU jumps and matching the GreeksV2 calculator in pricing-engine's errors. [F8]
- No config changes were found for order-service or redis-cache in 11:30-13:59Z, so no timeout/pool/TLS/DNS/resource-limit change is recorded for the callee chain or the shared cache. [F9]
- order-service's dependency graph: it calls ledger-db (shared), pricing-engine (http), exchange-gateway (http) and produces to kafka (shared); pricing-engine calls market-data-service (http) and redis-cache (shared). Shared components in the blast radius are ledger-db, redis-cache and kafka. [F10]
- Historical precedent HIST-113 (2026-01-28) 'pricing-engine CPU saturation at market open' had the same symptom pair (quote timeouts, cpu 99%) but was attributed to organic record market-open volume exceeding capacity. [F11]
- Precedents HIST-101 (auth-service bcrypt cost 10->14 inside a release caused high CPU + p99/503s, resolved by rollback) and HIST-107 (redis-cache cold-cache storm caused quote latency) are leads only, not evidence of the current cause. [F12]

## Assumptions (not verified)
- Assumption: the 12:04:00Z flag-change timestamp precedes or is simultaneous with the 12:04 p99/CPU jump; sub-minute ordering is unresolved (minute-granular flag timestamp vs. bucket granularity).
- Assumption: the ~98-99.5% CPU is caused by GreeksV2 compute rather than GC pauses or thread saturation; no GC/thread/pool metrics were available.
- Assumption: the traffic mix shift from 5% to 100% GreeksV2 is sufficient to raise CPU from ~40% to ~99%, i.e. GreeksV2 is dramatically more expensive than the default calculator.
- Assumption: cfg-3120 is the only relevant change to pricing-engine behavior; no shared-library or infra change outside the deployment/config APIs coincided with the 12:04 onset.
- Assumption: pricing-engine is on the critical path for every order quote such that its degradation alone explains the order-service 2500ms timeouts (its errors are the same signature).

## Timeline
- `2026-05-20T11:43:53Z` market-data-service emits its only log line in the window: an unrelated deprecated TLS cipher WARN (no anomaly). [F6]
- `2026-05-20T11:56:00Z` market-data-service v5.2.0 'Switch quote serialization to protobuf' deployed by v.rao (only deployment in window; never rolled back). This is the reporter's suspicion. [F7]
- `2026-05-20T12:04:00Z` Config change cfg-3120: flags.pricing.greeks_v2.rollout_pct ramped 5 -> 100 by pm-experiments ('Ramp Greeks v2 experiment to all traffic'). [F8]
- `2026-05-20T12:04:00Z` pricing-engine p99 latency jumps from ~58-61ms baseline to 1705ms; HTTP 5xx error_rate rises from ~0.2% to 4.47%; CPU rises from ~40% to 68.44% (max 98.65). [F1, F2, F3]
- `2026-05-20T12:05:21Z` Earliest pricing-engine log anomaly: GreeksV2 Monte-Carlo computations (paths=20000) taking 6.5-8.9s. [F4]
- `2026-05-20T12:06:00Z` pricing-engine CPU enters a flat ~98-99.5% (max 100.0) regime that persists for the rest of the window. [F3]
- `2026-05-20T12:06:10Z` order-service's first ERROR: 'pricing-engine quote call timed out after 2500ms'; first of 225 identical errors spanning through 13:58:24Z. [F5]
- `2026-05-20T12:06:41Z` pricing-engine 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' errors begin; 276 total. [F4]
- `2026-05-20T12:07:00Z` pricing-engine 5xx error_rate settles at ~12-13% and p99 at ~2532-2746ms; both hold through 13:58 (ongoing, not recovered). [F1, F2]
- `2026-05-20T13:58:00Z` Incident still ongoing at latest data: pricing-engine p99 ~2.6s, errors ~12.5%, order-service quote timeouts continuing. [F1, F2, F5]

## Recommended actions
- **[immediate]** Immediate mitigation: revert cfg-3120 by lowering flags.pricing.greeks_v2.rollout_pct from 100 back to 5 (or 0) for pricing-engine, and watch pricing-engine CPU, p99 and order-service quote timeouts over the next 10-15 minutes. -- cfg-3120 is the only change coinciding exactly with the p99/CPU jump, and pricing-engine's own errors name calculator=GreeksV2 (F4, F8, F1, F3). HIST-101 shows a config-like cost regression inside a release was resolved by rollback (F12). [F8, F4, F1, F3, F12]
- **[immediate]** Do NOT roll back market-data-service v5.2.0 (dep-6111) as the first action; if a rollback is demanded for reassurance, note it is a low-probability lever and its deploy at 11:56 precedes onset by 8 minutes. -- market-data-service shows zero latency, CPU or error anomaly across the window, and the flag change is a far better temporal and log-content match (F6, F7, F4). [F6, F7, F4]
- **[short_term]** Confirm the reversal: verify pricing-engine CPU returns toward ~40% baseline, p99 toward ~60ms and order-service quote timeouts cease; then close the incident and re-ramp the flag in graded steps (e.g. 5 -> 25 -> 50) with an explicit cost/latency gate. -- The whole incident signature is change-induced and ongoing (F1, F2, F3, F5); recovery on revert both confirms H1 and provides the fix path. [F1, F2, F3, F5]
- **[short_term]** If discriminating is still needed, compare pricing-engine request_rate at 12:04 against the 11:30-13:59 baseline: organic-load saturation (H4) would show a volume surge, while a flag ramp should show roughly flat volume with a step change in per-request cost. -- Separates the change-induced compute hypothesis from HIST-113-style organic market-open capacity saturation, which shares the symptom pair (F11). [F11, F3, F1]
- **[short_term]** Also check market-data-service error_rate and pricing-engine memory_pct for the onset window to close the outstanding telemetry gaps, and confirm whether order-service recorded any signal before 12:06:10Z. -- These queries were never executed due to call-budget exhaustion, limiting the strength of the alternative-hypothesis exclusions (data gaps; F5, F6). [F5, F6]
- **[long_term]** Add a pre-ramp capacity/cost gate for pricing-engine flag experiments: estimate per-request compute for GreeksV2 (paths=20000) before ramping, and block ramps that would exceed headroom; alert on pricing-engine CPU > ~80% and quote-budget overruns. -- The root cause is a config ramp that ran the expensive calculator on 100% of traffic without a compute budget check; the same class of event (HIST-101) has occurred before (F4, F8, F12). [F4, F8, F12]
- **[long_term]** Instrument pricing-engine with GC, thread-saturation and connection-pool metrics, and enrich the p99/CPU alerts so a compute-bound regression is distinguishable from GC or pool exhaustion. -- The CPU-saturation attribution rests on indirect evidence because no GC/thread/pool telemetry was available; this gap makes the conclusion harder to defend at second resolution (data gap; F3, F4). [F3, F4]

## Data gaps
- market-data-service error_rate, order-service error_rate and pricing-engine memory_pct queries were never executed (call-budget exhausted), so order-service's own 5xx rate, market-data-service's error rate and pricing-engine's memory/GC trend around onset are unconfirmed.
- No GC, thread-saturation or connection-pool metrics exist among the available metrics (cpu_pct, error_rate, latency_p99_ms, memory_pct, request_rate, restarts), so the ~98-99.5% CPU cannot be definitively attributed to GreeksV2 compute rather than GC pauses or thread saturation.
- market-data-service INFO logs between 11:55 and 12:07 were not retrieved, so the reported serialization change could not be correlated to any log/metric event; whether it produced any telemetry is unverified.
- order-service logs below WARN earlier than 12:06 and order-service metrics were not retrieved, so an earlier order-service signal before 12:06:10Z cannot be fully excluded.
- No second historical search for a feature-flag-ramp CPU/latency precedent was possible (budget_exhausted), so the precedent set is limited to five incidents.
- No shared-library/adapter artifacts, k8s resource-limit changes or CI/CD pipelines were inspected, so a change outside get_deployments/get_config_changes coinciding with the 12:04 onset is not fully excluded.
- The exact sub-minute ordering of the 12:04:00Z flag change vs. the p99/CPU jump is unresolved (minute-granular timestamps).
- One tool call (tc-002 get_config_changes by dependency) returned a PARTIAL result, so config-change coverage may be incomplete.

## What would change this conclusion
- pricing-engine request_rate showing a sharp, step-like surge at 12:04 with flat per-request cost would support H4 (organic capacity saturation, HIST-113 pattern) over the flag ramp.
- Evidence that pricing-engine CPU/p99 remained elevated after reverting flags.pricing.greeks_v2.rollout_pct to 5 would falsify H1 and reopen the investigation.
- GC-pause or thread-saturation/pool-exhaustion evidence (e.g. long GC pauses, blocked thread counts) coinciding with the CPU jump would move the cause away from GreeksV2 compute.
- Detection of a serialization/deserialization error, payload-size jump or latency change on the market-data-service protobuf path (or a market-data-service-side anomaly) would revive H2.
- Discovery of a change outside the deployment/config APIs (shared library publish, k8s resource-limit change) timestamped at or just before 12:04 would reopen the space of triggering changes.
- A confirmed, second-resolution ordering showing the p99/CPU jump strictly preceded the 12:04:00Z flag change would weaken the causal claim.

## Integrity warnings (set by code)
- H1: dropped citations to non-existent findings ['7']
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6

## Investigation stats
- latency 128.54s, LLM calls 14, tokens 95716 (in 75369 / out 20347)
- tool calls 24 (0 failed, 1 partial, 0 recovered by retry), findings 12/12 verified
