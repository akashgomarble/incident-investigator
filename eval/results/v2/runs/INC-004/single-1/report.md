# Incident investigation report: INC-004
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
notification-service has been OOMKilling repeatedly since 2026-03-21T00:00, when deployment dep-5402 (v3.8.0, "Add in-memory template cache for email rendering") introduced an unbounded ConcurrentHashMap in the render path. memory_pct shows a textbook sawtooth: flat ~48% before the deploy, then a steady linear ramp reaching ~95-98% each day, OOMKill, restart, ramp again — cycling roughly once per day (03-21 22:59, 03-22 21:59, 03-23 19:59). The reported "right after Kafka maintenance" timing is coincidental: the Kafka retention change (cfg-2410) landed 2026-03-23T18:00, AFTER the first two OOMs, and Kafka itself stayed healthy throughout (cpu ~35%, 0 under-replicated partitions). CPU (~25%), request_rate (~140/s) and latency_p99 (~110ms) are flat, ruling out load-driven capacity saturation. Consumer-lag and error-rate spikes co-occur with each restart and are consequences, not causes. Root cause: bad_deployment — an unbounded in-memory cache.

## Probable root cause
**Unbounded in-memory template cache (dep-5402) leaks heap, causing OOMKills** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded in-memory template cache (dep-5402) leaks heap, causing OOMKills | bad_deployment | notification-service | dep-5402 | 0.86 (high) | active |
| 2 | H2: Kafka maintenance / retention change triggers replay that chokes the consumer | config_change | kafka | cfg-2410 | 0.05 (low) | rejected |
| 3 | H3: Capacity saturation — load outgrew provisioned memory | capacity_saturation | notification-service | - | 0.03 (low) | rejected |

### H1: Unbounded in-memory template cache (dep-5402) leaks heap, causing OOMKills
Deployment dep-5402 (notification-service v3.8.0, deployed 2026-03-21T00:00) added an in-memory TemplateCache (ConcurrentHashMap keyed by template+locale+userSegment) on the email render path. Because entries appear never to be evicted, resident heap grows monotonically under steady traffic until the container hits its memory limit and is OOMKilled (exit 137). Each restart clears the cache, so the heap ramps again to the same ceiling ~24h later — producing the daily OOM cycle since 03-21.

**Causal chain**
1. dep-5402 adds ConcurrentHashMap template cache at 2026-03-21T00:00 [F1, F12]
2. memory_pct shifts from flat ~48% to steady linear ramp immediately after deploy [F2]
3. heap exceeds limit -> OOMKill (exit 137), restart count increments to 3 [F4, F5]
4. restart clears cache, memory resets to ~48% and ramps again next day (sawtooth) [F3]
5. consumer lag and error-rate spikes are transient consequences of each restart [F9, F10]

**Supporting evidence**
  - `F1` ✔ notification-service was deployed with v3.8.0 (dep-5402) at 2026-03-21T00:00:00Z, adding an in-memory template cache: 'TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment', status live, not rolled back.
  - `F2` ✔ memory_pct for notification-service was flat ~47–48% through 2026-03-20 and up to 2026-03-21T00:00, then began a steady linear climb starting 2026-03-21T02:00 (avg 52.71, max 55.43) reaching avg 92.91 / max 95.09 at 2026-03-21T20:00.
  - `F3` ✔ memory_pct shows a repeated sawtooth/reset cycle: it climbs to ~95-98% then drops back to ~48-50% at each restart, on 03-21T22:00 (avg 71.52, max 97.37), 03-22T20:00 (avg 94.91, max 97.98), and 03-23T18:00 (avg 95.05, max 98.44, then 03-23T20:00 avg 48.33).
  - `F4` ✔ notification-service logs show 'heap usage ... over 90 (old gen)' full-GC warnings with growing GC times starting 2026-03-21T21:37 and OOMKilled events (exit code 137) at 2026-03-21T22:59:50, 2026-03-22T21:59:47 and 2026-03-23T19:59:21 — three OOMs in three days.
  - `F5` ✔ The restarts metric (cumulative) for notification-service is 0 until 2026-03-21T20:00, becomes 1 at 2026-03-21T22:00, 2 at 2026-03-22T22:00, and 3 at 2026-03-23T20:00 — matching exactly three OOM restarts after the deploy.
  - `F12` ✔ The memory ramp and OOM cycle began at exactly 2026-03-21T00:00, the same time as deployment dep-5402 (v3.8.0), and there was no other deployment to notification-service in the two weeks up to the incident.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The cache has no eviction/size bound and grows with distinct template+locale+userSegment keys under normal traffic
  - Traffic composition (distinct keys) is roughly constant day to day, which is what makes each ramp look identical

### H2: Kafka maintenance / retention change triggers replay that chokes the consumer
The Kafka retention change cfg-2410 (2026-03-23T18:00) or underlying broker maintenance caused a consumer replay, overwhelming notification-service and causing heap exhaustion.

**Causal chain**
1. Kafka maintenance at 2026-03-23T18:00 reduces retention, forcing consumer replay [F6]
2. replay floods consumer and blows heap *(assumed)*

**Supporting evidence**
  - `F9` ✔ consumer_lag was flat ~56-65 messages for most of the window but spiked massively only at the same buckets as the OOMs: 2026-03-21T22:00 max 17161.92, 2026-03-22T20:00 max 17611.66, 2026-03-23T18:00 max 18137.93.
**Contradicting evidence**
  - `F6` ✔ The only config/flag change in the window is cfg-2410 on service 'kafka': log.retention.ms reduced 604800000 -> 259200000, changed_at 2026-03-23T18:00:00Z — i.e. after the first two OOMKills.
  - `F5` ✔ The restarts metric (cumulative) for notification-service is 0 until 2026-03-21T20:00, becomes 1 at 2026-03-21T22:00, 2 at 2026-03-22T22:00, and 3 at 2026-03-23T20:00 — matching exactly three OOM restarts after the deploy.
  - `F2` ✔ memory_pct for notification-service was flat ~47–48% through 2026-03-20 and up to 2026-03-21T00:00, then began a steady linear climb starting 2026-03-21T02:00 (avg 52.71, max 55.43) reaching avg 92.91 / max 95.09 at 2026-03-21T20:00.
  - `F7` ✔ Kafka metrics were healthy across the whole window: cpu_pct flat ~35% (min 29.77, max 38.87) and under_replicated_partitions = 0 at every sampled point.
  - `F8` ✔ notification-service cpu_pct stayed flat ~25% (min 21.78, max 28.5) across the entire window including through all OOM events, and request_rate stayed ~140/s (min 115.35, max 172.03).
**Assumptions**
  - A replay actually occurred
**Rejected because:** Timing does not fit: the first two OOMKills (2026-03-21T22:59, 2026-03-22T21:59) and the start of the memory ramp (2026-03-21T02:00) all precede cfg-2410 (2026-03-23T18:00). Kafka metrics were healthy (cpu ~35%, 0 under-replicated partitions). Lag spikes coincide with restarts rather than preceding them.

### H3: Capacity saturation — load outgrew provisioned memory
Steady growth in notification traffic exhausted provisioned memory, causing OOMKills independent of any code change.

**Causal chain**
1. rising request volume raises heap use *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ notification-service cpu_pct stayed flat ~25% (min 21.78, max 28.5) across the entire window including through all OOM events, and request_rate stayed ~140/s (min 115.35, max 172.03).
  - `F2` ✔ memory_pct for notification-service was flat ~47–48% through 2026-03-20 and up to 2026-03-21T00:00, then began a steady linear climb starting 2026-03-21T02:00 (avg 52.71, max 55.43) reaching avg 92.91 / max 95.09 at 2026-03-21T20:00.
  - `F3` ✔ memory_pct shows a repeated sawtooth/reset cycle: it climbs to ~95-98% then drops back to ~48-50% at each restart, on 03-21T22:00 (avg 71.52, max 97.37), 03-22T20:00 (avg 94.91, max 97.98), and 03-23T18:00 (avg 95.05, max 98.44, then 03-23T20:00 avg 48.33).
**Rejected because:** request_rate (~140/s), cpu_pct (~25%) and latency_p99 (~110ms) are flat across the window, and memory was flat ~48% before the deploy — no load growth coincident with onset.

## Facts (verified observations)
- notification-service v3.8.0 (dep-5402) deployed 2026-03-21T00:00 adding an in-memory ConcurrentHashMap template cache; live, not rolled back. [F1, F12]
- memory_pct was flat ~48% before 2026-03-21T00:00 then ramped linearly to ~95-98% each day with resets to ~48-50% at each restart (sawtooth). [F2, F3]
- Three OOMKilled events (exit code 137): 2026-03-21T22:59:50, 2026-03-22T21:59:47, 2026-03-23T19:59:21; restarts counter reached 3. [F4, F5]
- The only config change was Kafka log.retention.ms reduction at 2026-03-23T18:00, after the first two OOMs. [F6]
- Kafka stayed healthy: cpu ~35%, under_replicated_partitions = 0 throughout. [F7]
- notification-service cpu_pct (~25%) and request_rate (~140/s) were flat across the window. [F8]
- consumer_lag spikes (max ~17k-18k) and error_rate spikes (max ~14%) occur only in the same buckets as the OOM/restart events. [F9, F10]

## Assumptions (not verified)
- The TemplateCache has no eviction/size bound and accumulates entries over time.
- Distinct template+locale+userSegment key cardinality under normal traffic is stable day-to-day, producing identical ~24h ramps.
- No other code or infra change to notification-service occurred in the window (only dep-5402 was listed).

## Timeline
- `2026-03-20T00:00:00Z` Baseline: notification-service memory_pct flat ~48%, restarts=0, consumer_lag ~60. [F2, F5, F9]
- `2026-03-21T00:00:00Z` Deployment dep-5402 (v3.8.0) ships in-memory template cache to notification-service. [F1, F12]
- `2026-03-21T02:00:00Z` memory_pct begins steady linear ramp (avg 52.71 -> rising each bucket). [F2]
- `2026-03-21T21:37:00Z` Heap >90% old-gen full-GC warnings begin. [F4]
- `2026-03-21T22:59:50Z` First OOMKill (exit 137), restart; restarts counter -> 1; memory resets to ~48%. [F4, F5, F3]
- `2026-03-22T21:59:47Z` Second OOMKill; restarts -> 2; memory resets to ~48%. [F4, F5, F3]
- `2026-03-23T18:00:00Z` Kafka config change cfg-2410 (retention 7d -> 3d) — after the first two OOMs. [F6]
- `2026-03-23T19:59:21Z` Third OOMKill; restarts -> 3; trade-confirmation emails delayed ~25 min. [F4, F5, F9]

## Recommended actions
- **[immediate]** Roll back notification-service to the pre-dep-5402 build (v3.7.x) or immediately disable the TemplateCache feature path (feature-flag / config) to stop the heap leak. -- The memory ramp begins exactly at dep-5402 and the cache is the only candidate leak; rollback is the fastest mitigation. [F1, F2, F12]
- **[short_term]** If rollback is not possible, bound the cache with a size/weight limit and TTL, or replace with an off-heap/bounded cache (e.g. Caffeine with maximumSize + expireAfterWrite). -- ConcurrentHashMap keyed by template+locale+userSegment has no eviction, so it grows unbounded. [F1]
- **[short_term]** Add a memory/heap alert on notification-service OOM/restart and a post-restart cache-warm check to break the daily sawtooth until the fix lands. -- Three OOMs occurred over three days before escalation; earlier alerting would have shortened impact. [F4, F5]
- **[short_term]** Correct the incident narrative: Kafka maintenance is coincidental, not causal — do not schedule further Kafka rollbacks/replays for this issue. -- cfg-2410 postdates the first two OOMs and Kafka telemetry is healthy; chasing Kafka would waste effort. [F6, F7]

## Data gaps
- No heap-dump or JVM-side cache-size telemetry was available to directly prove TemplateCache unbounded growth; the leak is inferred from the deploy-correlated sawtooth.
- email-provider has no telemetry, so its contribution to delivery delays could not be excluded, though it cannot explain the heap ramp.
- No deployment/config change history is available for kafka beyond cfg-2410, so we cannot fully audit the 'Kafka maintenance' activities mentioned in the report.

## Investigation stats
- latency 41.46s, LLM calls 5, tokens 63427 (in 55905 / out 7522)
- tool calls 17 (1 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
