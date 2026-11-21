# Incident investigation report: INC-011
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The most probable root cause is the pricing-engine config change cfg-3120, which at 2026-05-20T12:04:16Z ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100, forcing all quote traffic onto the compute-heavy GreeksV2 Monte-Carlo calculator path (20000 paths, 6465ms observed) — a ~40x p99 jump on pricing-engine that then trips order-service's 2500ms quote timeout. This is supported by a consistent causal chain of verified observations: the flag flip is the only change in the ~5 minutes before the first slow calc (F14), the first GreeksV2 slow-calc log immediately follows it (F7), pricing-engine p99 was flat until that bucket (F9), pricing-engine's own errors name calculator=GreeksV2 exceeding its 2000ms budget (F12), and order-service timeouts begin after the pricing-engine latency jump (F8, F11). Confidence is medium (0.60) rather than high: no canary latency history for the GreeksV2 path and no resource metrics were obtained, and the reporter's suspected market-data-service protobuf deploy (dep-6111) is plausible but contradicted by that service being healthy and not the earliest-affected component (F10). Mitigation should be to roll the flag back and verify pricing-engine p99 recovers.

## Probable root cause
**GreeksV2 flag ramp (cfg-3120) made an expensive calculator serve all traffic, saturating pricing-engine** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: GreeksV2 flag ramp (cfg-3120) made an expensive calculator serve all traffic, saturating pricing-engine | config_change | pricing-engine | cfg-3120 | 0.60 (medium) | active |
| 2 | H2: market-data-service protobuf deploy dep-6111 (reporter's suspicion) broke quote payloads downstream | bad_deployment | market-data-service | dep-6111 | 0.20 (low) | active |
| 3 | H3: Shared redis-cache fault degraded independently, propagating to pricing-engine and market-data-service | infrastructure_failure | redis-cache | null | 0.10 (low) | active |
| 4 | H4: Resource saturation (CPU/memory/thread-pool) on pricing-engine or order-service | capacity_saturation | pricing-engine | null | 0.10 (low) | active |

### H1: GreeksV2 flag ramp (cfg-3120) made an expensive calculator serve all traffic, saturating pricing-engine
At 12:04:16Z cfg-3120 ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100, moving all pricing-engine quote traffic onto the GreeksV2 Monte-Carlo calculator code path. That path is compute-heavy (20000 Monte-Carlo paths, 6465ms observed) and exceeds the 2000ms internal deadline, driving pricing-engine p99 from ~60ms to ~2500ms and its 'request deadline exceeded ... calculator=GreeksV2' errors. order-service, which calls pricing-engine over http with a 2500ms timeout, then fails the quote step.

**Causal chain**
1. cfg-3120 ramps greeks_v2.rollout_pct 5 -> 100 at 2026-05-20T12:04:16Z, sending all quote traffic through the GreeksV2 path [F2, F7, F14]
2. GreeksV2 Monte-Carlo calculator is compute-heavy and slow: 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms' first at 12:05:21Z, ~65s after the flag flip [F7]
3. pricing-engine p99 jumps from flat ~58-62ms to ~2489ms in the 12:05:00Z bucket and stays elevated through 13:56Z [F9]
4. pricing-engine emits 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' from 12:06:41Z [F12]
5. order-service quote calls to pricing-engine exceed the 2500ms downstream threshold, producing 225 'pricing-engine quote call timed out' errors starting 12:06:10Z [F8, F12]
6. order-service error_rate jumps from ~0.2% baseline to 6-8.4% in the bucket starting 12:04:00Z [F11]

**Supporting evidence**
  - `F2` ✔ Two config changes in the window (full, non-partial retry): cfg-3118 api-gateway 'ratelimit.burst' 200→250 at 2026-05-20T10:30:00Z (config-bot), and cfg-3120 pricing-engine 'flags.pricing.greeks_v2.rollout_pct' 5→100 at 2026-05-20T12:04:00Z by 'pm-experiments' ('Ramp Greeks v2 experiment to all traffic'). The pricing-engine flag ramp at 12:04Z is ~8 minutes after the protobuf deploy.
  - `F7` ✔ The earliest anomaly is on pricing-engine: first slow-calc log 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms' at 2026-05-20T12:05:21Z, immediately preceded by config change 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100' at 12:04:16Z.
  - `F9` ✔ pricing-engine p99 latency was flat ~58-62ms all morning through the 11:56-11:59 buckets, then jumped to avg 2489ms in the bucket starting 12:05:00Z and stayed ~2,500-2,850ms through 13:56Z — a new regression coinciding with the 12:04:16 config change, not pre-existing.
  - `F11` ✔ order-service error_rate jumped from a ~0.17-0.25% baseline (11:00-11:56) to 6.08% in the bucket starting 12:04:00Z and ~8.1-8.4% thereafter, a blast-radius confirmation at the same onset window as pricing-engine.
  - `F12` ✔ pricing-engine's own error signature is 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (276 occurrences, first at 12:06:41Z), i.e. the GreeksV2 calculator exceeds its internal budget, while order-service's downstream timeout threshold is 2500ms — the pricing-engine failure precedes and causes the order-service timeouts.
  - `F14` ✔ A tight-window config query (11:40–12:10Z) returned exactly one change: cfg-3120, pricing-engine, key flags.pricing.greeks_v2.rollout_pct old_value 5 → new_value 100, changed_at 2026-05-20T12:04:00Z, author pm-experiments, 'Ramp Greeks v2 experiment to all traffic'. This is the only change in the ~5 minutes before the 12:05:21Z first slow calc, and it directly alters the pricing-engine calculator named in the slow-calc log.
  - `F18` ✔ Historical precedent search gives mixed leads, none matching a serialization change: HIST-107 (2025-12-09) red-cache/market-data-service cold-cache storm (redis primary failure promoted an empty replica) and two bad_deployment precedents HIST-101 (bcrypt cost 10→14) and HIST-109 (JWT clock-skew default) where a config/code value change released at rollout caused the latency — the latter pattern (HIST-101) structurally resembles a flag ramp that makes an expensive code path serve all traffic.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The GreeksV2 path was not already slow at 5% traffic (no canary latency history for cfg-3120 was obtained) — if it was, blame shifts from the ramp to the code path itself
  - No untracked pricing-engine deploy occurred near onset; deployment queries found none (F13)
  - No resource-saturation metrics were captured, so a coincident CPU/memory fault cannot be formally excluded

### H2: market-data-service protobuf deploy dep-6111 (reporter's suspicion) broke quote payloads downstream
At 11:56:45Z market-data-service shipped dep-6111 v5.2.0 'Switch quote serialization to protobuf'. The protobuf payloads are valid but incompatible with what pricing-engine (or an intermediate consumer) can parse, so pricing-engine wastes time on parse retries/fallbacks, inflating its latency and eventually causing order-service quote timeouts.

**Causal chain**
1. dep-6111 (market-data-service v5.2.0, protobuf quote serialization) deploys at 11:56:45Z, the only deployment in the window [F1, F10, F13]
2. pricing-engine fetches quotes from market-data-service over http and would be the consumer of the new protobuf payloads [F3, F15]
3. protobuf payload incompatibility causes downstream parse retries/fallbacks in pricing-engine, inflating latency *(assumed)*
4. inflated pricing-engine latency pushes quote calls past order-service's 2500ms timeout, producing quote timeouts [F8, F12]

**Supporting evidence**
  - `F1` ✔ Only one deployment in 10:00–13:59Z across ALL services: dep-6111, market-data-service v5.2.0, deployed 2026-05-20T11:56:00Z by v.rao, 'Switch quote serialization to protobuf' ('protobuf quote payloads for internal callers'), status 'live', rolled_back_at null. No deployment on order-service, pricing-engine, or any other service in the window.
  - `F3` ✔ order-service's quote path (dependency edges, caller→callee) is: api-gateway → order-service → {pricing-engine (http), ledger-db (db), exchange-gateway (http), kafka (queue-produce)}; pricing-engine → {market-data-service (http), redis-cache (cache)}. So order-service does NOT call market-data-service directly — it goes through pricing-engine. market-data-service → redis-cache (cache) and → exchange-gateway (http).
  - `F13` ✔ Full re-query of deployments 10:00–13:59Z across all services returned exactly one: dep-6111 market-data-service v5.2.0 at 11:56:00Z, status live, rolled_back_at null. A second, narrower query scoped to service=pricing-engine for 11:30–12:06:30Z returned count 0 deployments, so nothing was deployed to pricing-engine (or its own stack) near the 12:05 onset.
  - `F15` ✔ Dependency re-query confirms order-service reaches market-data-service only indirectly: order-service → pricing-engine (http) → market-data-service (http). market-data-service and pricing-engine both have edges to redis-cache (kind 'cache'), and redis-cache is flagged type 'redis', owner platform, shared=true. So redis-cache is the single shared component (other than the direct http edge) through which market-data-service could affect pricing-engine.
**Contradicting evidence**
  - `F7` ✔ The earliest anomaly is on pricing-engine: first slow-calc log 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms' at 2026-05-20T12:05:21Z, immediately preceded by config change 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100' at 12:04:16Z.
  - `F9` ✔ pricing-engine p99 latency was flat ~58-62ms all morning through the 11:56-11:59 buckets, then jumped to avg 2489ms in the bucket starting 12:05:00Z and stayed ~2,500-2,850ms through 13:56Z — a new regression coinciding with the 12:04:16 config change, not pre-existing.
  - `F10` ✔ market-data-service deployed dep-6111 (v5.2.0) at 2026-05-20T11:56:45Z but shows no errors and stays responsive afterward ('served batch of requests, p50=393ms' at 12:01:29Z); its only WARN is an unrelated 'deprecated TLS cipher offered by client' at 11:43:53Z — it is not the earliest-affected component.
  - `F14` ✔ A tight-window config query (11:40–12:10Z) returned exactly one change: cfg-3120, pricing-engine, key flags.pricing.greeks_v2.rollout_pct old_value 5 → new_value 100, changed_at 2026-05-20T12:04:00Z, author pm-experiments, 'Ramp Greeks v2 experiment to all traffic'. This is the only change in the ~5 minutes before the 12:05:21Z first slow calc, and it directly alters the pricing-engine calculator named in the slow-calc log.
**Assumptions**
  - The protobuf change produced payloads that pricing-engine cannot fully parse (untested)
  - Any parse cost would have to account for a ~40x p99 jump on its own

### H3: Shared redis-cache fault degraded independently, propagating to pricing-engine and market-data-service
A shared dependency of pricing-engine and market-data-service — redis-cache (the only shared component besides the direct http edge) — degraded at ~12:04-12:05Z, causing cache-miss storms/retries that inflated pricing-engine latency and eventually order-service quote timeouts, independently of both the protobuf deploy and the flag ramp.

**Causal chain**
1. redis-cache is the only shared component linking pricing-engine and market-data-service, and is shared=true [F4, F15]
2. redis-cache degrades (failover/empty replica/eviction) at onset, causing a cold-cache miss storm *(assumed)*
3. cache misses inflate pricing-engine quote computation latency to ~2.5s *(assumed)*
4. order-service quote calls to pricing-engine exceed 2500ms and time out [F8]

**Supporting evidence**
  - `F4` ✔ redis-cache is the only shared component connecting pricing-engine and market-data-service: both have edges to redis-cache (kind 'cache'), and redis-cache is flagged shared=true, owner platform. So a shared cache is a possible cross-service path from the market-data serialization change to pricing-engine latency, independent of the direct pricing-engine→market-data-service http edge.
  - `F5` ✔ Other shared platform components in the graph: ledger-db (postgres, shared=true, used by order-service) and kafka (kafka, shared=true, order-service produces to it). These — plus redis-cache — are shared components that could transmit faults from a neighbour into order-service or pricing-engine.
  - `F15` ✔ Dependency re-query confirms order-service reaches market-data-service only indirectly: order-service → pricing-engine (http) → market-data-service (http). market-data-service and pricing-engine both have edges to redis-cache (kind 'cache'), and redis-cache is flagged type 'redis', owner platform, shared=true. So redis-cache is the single shared component (other than the direct http edge) through which market-data-service could affect pricing-engine.
  - `F6` ✔ Historical leads (search 'quote timeout serialization change'): HIST-107 (2025-12-09) 'Cold cache storm after redis failover', services redis-cache + market-data-service, symptoms cache miss storm / quote latency, root cause 'Redis primary failure promoted an empty replica', resolution warmed cache + fixed replica sync checks; and HIST-113 (2026-01-28) 'pricing-engine CPU saturation at market open', symptom quote timeouts + cpu 99%, root cause capacity saturation, resolution raised replica count. Both are leads only, not evidence about the current incident; note neither prior incident was caused by a serialization/deploy change, and both implicate redis-cache or pricing-engine capacity rather than market-data's protobuf change.
  - `F17` ✔ redis-cache is shared by a wider set than pricing-engine/market-data-service: auth-service and portfolio-service also have cache edges to it, and api-gateway and portfolio-service call market-data-service over http. If the shared cache were the fault vector, those extra callers are additional exposed neighbours that were not checked in this task.
**Contradicting evidence**
  - `F7` ✔ The earliest anomaly is on pricing-engine: first slow-calc log 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms' at 2026-05-20T12:05:21Z, immediately preceded by config change 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100' at 12:04:16Z.
  - `F9` ✔ pricing-engine p99 latency was flat ~58-62ms all morning through the 11:56-11:59 buckets, then jumped to avg 2489ms in the bucket starting 12:05:00Z and stayed ~2,500-2,850ms through 13:56Z — a new regression coinciding with the 12:04:16 config change, not pre-existing.
  - `F12` ✔ pricing-engine's own error signature is 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (276 occurrences, first at 12:06:41Z), i.e. the GreeksV2 calculator exceeds its internal budget, while order-service's downstream timeout threshold is 2500ms — the pricing-engine failure precedes and causes the order-service timeouts.
  - `F16` ✔ config-change queries scoped to redis-cache and to market-data-service for 11:00–13:59Z each returned count 0: no config change touched the shared cache or market-data-service in the window. Hence no tracked change landed on the shared dependency (redis-cache) of pricing-engine at any point around onset.
**Assumptions**
  - An untracked redis-cache event (no config/deploy record) occurred at onset
  - The error signature would look like cache misses rather than the observed GreeksV2 deadline-exceeded messages

### H4: Resource saturation (CPU/memory/thread-pool) on pricing-engine or order-service
pricing-engine or order-service hit CPU/memory/GC/connection-pool saturation around 12:04Z, causing the latency spike and timeouts; the flag ramp and protobuf deploy are coincidental. Structurally resembles HIST-113 (pricing-engine CPU saturation at market open).

**Causal chain**
1. pricing-engine CPU/memory or thread pool saturates near 12:04Z *(assumed)*
2. saturation inflates quote computation latency to ~2.5s *(assumed)*
3. order-service quote calls exceed 2500ms and time out [F8]

**Supporting evidence**
  - `F6` ✔ Historical leads (search 'quote timeout serialization change'): HIST-107 (2025-12-09) 'Cold cache storm after redis failover', services redis-cache + market-data-service, symptoms cache miss storm / quote latency, root cause 'Redis primary failure promoted an empty replica', resolution warmed cache + fixed replica sync checks; and HIST-113 (2026-01-28) 'pricing-engine CPU saturation at market open', symptom quote timeouts + cpu 99%, root cause capacity saturation, resolution raised replica count. Both are leads only, not evidence about the current incident; note neither prior incident was caused by a serialization/deploy change, and both implicate redis-cache or pricing-engine capacity rather than market-data's protobuf change.
**Contradicting evidence**
  - `F7` ✔ The earliest anomaly is on pricing-engine: first slow-calc log 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms' at 2026-05-20T12:05:21Z, immediately preceded by config change 'config change cfg-3120 applied: flags.pricing.greeks_v2.rollout_pct=100' at 12:04:16Z.
  - `F12` ✔ pricing-engine's own error signature is 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (276 occurrences, first at 12:06:41Z), i.e. the GreeksV2 calculator exceeds its internal budget, while order-service's downstream timeout threshold is 2500ms — the pricing-engine failure precedes and causes the order-service timeouts.
**Assumptions**
  - Resource metrics would show saturation (unqueried — budget exhausted)
  - The compute-heavy GreeksV2 path is not itself sufficient to explain the latency

## Facts (verified observations)
- cfg-3120 changed pricing-engine key flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 2026-05-20T12:04:16Z, authored by pm-experiments, and was the only change in the 11:40–12:10Z window. [F2, F14]
- The first pricing-engine slow-calc log, 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms', occurred at 2026-05-20T12:05:21Z, immediately preceded by the cfg-3120 config application at 12:04:16Z. [F7]
- pricing-engine p99 was flat ~58–62ms through the 11:56–11:59 buckets, then jumped to ~2489ms in the bucket starting 12:05:00Z and stayed ~2500–2850ms through 13:56Z — a new regression, not pre-existing. [F9]
- pricing-engine's error signature is 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (276 occurrences, first at 12:06:41Z), i.e. the GreeksV2 calculator exceeds its internal budget. [F12]
- order-service quote timeouts begin at 2026-05-20T12:06:10Z, after pricing-engine's first slow calc at 12:05:21Z, with 225 ERROR lines of 'pricing-engine quote call timed out after <n>ms'. [F8]
- order-service error_rate jumped from a ~0.17–0.25% baseline to 6.08% in the bucket starting 12:04:00Z and ~8.1–8.4% thereafter. [F11]
- Only one deployment occurred across all services in 10:00–13:59Z: dep-6111, market-data-service v5.2.0, at 11:56:00Z ('Switch quote serialization to protobuf'), status live, not rolled back; a pricing-engine-scoped deploy query for 11:30–12:06:30Z returned zero. [F1, F13]
- market-data-service shows no errors after dep-6111 and stayed responsive ('served batch of requests, p50=393ms' at 12:01:29Z); it is not the earliest-affected component. [F10]
- order-service reaches market-data-service only indirectly via pricing-engine (http); pricing-engine and market-data-service both hold 'cache' edges to redis-cache, flagged shared=true. [F3, F15]
- Config-change queries scoped to redis-cache and market-data-service for 11:00–13:59Z each returned count 0; no tracked change landed on the shared cache or market-data-service around onset. [F16]
- Historical precedents do not match a serialization change; HIST-107 (redis cold-cache storm) and HIST-113 (pricing-engine CPU saturation) implicate redis-cache or pricing-engine capacity, and HIST-101 (bcrypt cost config change) structurally resembles a flag ramp making an expensive code path serve all traffic. [F5, F6]

## Assumptions (not verified)
- The GreeksV2 code path was not already slow at 5% traffic before cfg-3120: no canary latency history for the flag was obtained, so if it was already slow the blame shifts from the ramp to the code path itself.
- No untracked pricing-engine deploy occurred near onset: deployment queries found none, but 'no deploy record' is not the same as 'no deploy occurred'.
- No resource-saturation fault (CPU/memory/GC/thread-pool) or shared redis-cache fault occurred at onset: no resource or cache metrics were captured, so a coincident fault cannot be formally excluded.
- order-service's 2500ms pricing-engine timeout and pricing-engine's 2000ms internal deadline are as stated in the observation findings and are the mechanism transferring pricing-engine latency to order-service quote failures.
- The deployment and config record set returned by the tools is complete for the window, modulo the noted partial config-audit shard 2/3.

## Timeline
- `2026-05-20T11:56:00Z` Only deployment in the window: dep-6111, market-data-service v5.2.0 'Switch quote serialization to protobuf' by v.rao, status live (not rolled back). [F1, F13]
- `2026-05-20T12:01:29Z` market-data-service post-deploy shows no errors and stays responsive ('served batch of requests, p50=393ms'). [F10]
- `2026-05-20T12:04:00Z` order-service error_rate bucket jumps to 6.08% (baseline ~0.17–0.25%). [F11]
- `2026-05-20T12:04:16Z` Config change cfg-3120 applied: pricing-engine flags.pricing.greeks_v2.rollout_pct 5→100 ('Ramp Greeks v2 experiment to all traffic'). [F7, F14, F2]
- `2026-05-20T12:05:21Z` First pricing-engine slow calc: 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms'. [F7]
- `2026-05-20T12:06:10Z` order-service quote timeouts begin: 'pricing-engine quote call timed out after <n>ms' (225 ERROR lines). [F8]
- `2026-05-20T12:06:41Z` pricing-engine error 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (first of 276). [F12]
- `2026-05-20T12:16:00Z` Incident INC-011 reported as SEV-2, order quotes timing out. []
- `2026-05-20T13:56:00Z` pricing-engine p99 remains elevated at ~2500–2850ms through the last bucket, i.e. sustained, not self-healing. [F9]

## Recommended actions
- **[immediate]** Roll back cfg-3120: set pricing-engine flags.pricing.greeks_v2.rollout_pct back from 100 to 5 (or 0) to remove the GreeksV2 Monte-Carlo path from the bulk of quote traffic. -- cfg-3120 is the only change in the ~5 minutes before the first slow calc and directly alters the calculator named in pricing-engine's slow-calc log and deadline-exceeded errors; reverting it is the fastest way to restore pricing-engine p99 and order-service quote success. [F2, F7, F12, F14]
- **[immediate]** If the flag cannot be rolled back quickly, mitigate at the boundary: raise order-service's pricing-engine call timeout (currently 2500ms) and/or shed load, but treat this only as a stopgap since it does not fix the underlying 6465ms compute cost. -- order-service fails because its 2500ms timeout is exceeded by pricing-engine's 2000ms-budget breach and ~2.5s p99; raising the timeout may reduce failures but will not address the saturated GreeksV2 path. [F12, F9]
- **[short_term]** Confirm recovery after rollback: watch pricing-engine p99 return toward the ~58–62ms baseline and order-service error_rate return to ~0.2% and quote timeouts cease. -- Pre-change baselines are known (p99 ~60ms; order-service error_rate ~0.17–0.25%), giving clear success criteria to verify the mitigation rather than assume it. [F9, F11, F8]
- **[short_term]** Do NOT roll back dep-6111 (market-data-service protobuf v5.2.0) as the first action; if rollback risk is low and cheap, it may be tested only after the flag rollback to discriminate H2, since market-data-service is healthy and not the earliest-affected component. -- The protobuf deploy is the reporter's suspicion but is contradicted by market-data-service showing no errors and staying responsive, and by order-service reaching it only through pricing-engine; rolling it back first risks churn without evidence. [F10, F1, F3, F15]
- **[short_term]** Capture pricing-engine CPU/memory/GC/thread-pool metrics (and redis-cache miss/latency) around 12:04–12:06Z in a follow-up to close the H3/H4 resource-saturation and shared-cache questions. -- No resource or cache telemetry was obtained; confirming the GreeksV2 compute path (rather than generic CPU saturation or a redis-cache fault) is the sole cause strengthens the conclusion and rules out the remaining hypotheses. [F4, F16]
- **[long_term]** Establish canary latency history / SLO gating for the GreeksV2 path before any future ramp, and cap rollout_pct increases behind latency thresholds. -- The ramp moved 100% of traffic onto a path never validated for production-scale latency; a canary history would have caught the ~40x p99 jump at 5% and prevented the all-traffic ramp. [F7, F9, F14, F18]

## Data gaps
- No resource/telemetry metrics (CPU, memory, GC, connection/thread-pool) were captured for pricing-engine or order-service (query_metrics returned 'budget_exhausted'), so H4 resource saturation cannot be confirmed or excluded.
- No redis-cache telemetry (miss rate, evictions, latency, connection saturation) was queried, so H3 shared-cache fault rests only on topology, with no supporting data.
- Blast radius to other services calling pricing-engine or market-data-service was not checked, so the full impact scope is unestablished.
- No canary/rollout history for cfg-3120 at 5% traffic, so it is unknown whether the GreeksV2 path was already slow before the 100% ramp.
- The first get_config_changes call (tc-005) was partial ('config-audit shard 2/3 did not respond'); residual risk that a config change on order-service or market-data-service is unlisted.
- No query for untracked cluster/infra events (node restarts, autoscaling, maintenance) that could have contributed without a change record.
- market-data-service's edge to exchange-gateway and the external exchange-api (has_telemetry=false) was not evaluated for latency contribution.
- No precedent specific to a protobuf/serialization change breaking downstream callers was found (historical search budget not exhausted before tool limits).

## What would change this conclusion
- Tracing the onset to 11:56Z (the dep-6111 deploy) rather than 12:04–12:05Z would shift support toward H2 (market-data-service protobuf).
- Metrics showing the GreeksV2 path was already slow at 5% traffic before cfg-3120 would move blame from the ramp to the code path itself (a property of the GreeksV2 implementation rather than the rollout).
- Evidence of CPU/memory/GC/thread-pool saturation on pricing-engine independent of the GreeksV2 compute cost would support H4 (resource saturation) over H1.
- Cache-miss/latency/eviction anomalies on redis-cache at ~12:04–12:06Z (with no corresponding config/deploy change) would support H3 (shared-cache fault).
- Failure of pricing-engine p99 and order-service errors to recover after rolling back cfg-3120 would falsify H1 as the sufficient cause and redirect to H2/H3/H4.
- Discovery of an additional unlisted config or deploy change (e.g. from config-audit shard 2/3) affecting pricing-engine or order-service around onset would re-open the causal attribution.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6

## Investigation stats
- latency 108.92s, LLM calls 13, tokens 88224 (in 71832 / out 16392)
- tool calls 24 (0 failed, 1 partial, 0 recovered by retry), findings 18/18 verified
