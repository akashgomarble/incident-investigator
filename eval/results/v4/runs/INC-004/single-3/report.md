# Incident investigation report: INC-004
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The OOMKilled restarts of notification-service are caused by a memory leak introduced by deployment dep-5402 (v3.8.0, 2026-03-21T00:00), which added an unbounded in-memory template cache (ConcurrentHashMap) with zero evictions. Memory was flat at ~48% before the deploy, then climbed monotonically to ~98% over ~22h (heap log: 'size=51750 entries, evictions=0'), triggering OOMKilled (exit 137) restarts and consumer-lag spikes about every 24h — including the third restart at 2026-03-23T19:59. The reported Kafka maintenance (cfg-2410, retention 7d->3d at 18:00) is coincidental: leaks occurred on 03-21 and 03-22 before it, and CPU (~25%), request_rate (~140 req/s) and p99 (~110ms) are all flat, with no Kafka rebalance/errors — so a replay-driven cause is ruled out. The 'consumer choking on a replay' theory is contradicted by the data.

## Probable root cause
**Memory leak from unbounded TemplateCache in dep-5402 causes OOMKilled** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.88, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Memory leak from unbounded TemplateCache in dep-5402 causes OOMKilled | bad_deployment | notification-service | dep-5402 | 0.88 (high) | active |
| 2 | H2: Kafka retention change / replay overwhelms the consumer | dependency_failure | kafka | cfg-2410 | 0.07 (low) | rejected |
| 3 | H3: Off-cluster independent memory leak / capacity limit too low | capacity_saturation | notification-service | - | 0.05 (low) | rejected |

### H1: Memory leak from unbounded TemplateCache in dep-5402 causes OOMKilled
notification-service v3.8.0 (dep-5402, 2026-03-21T00:00) added an unbounded in-memory template cache (ConcurrentHashMap keyed by template+locale+userSegment) with no eviction. Entries accumulate (size=51750, evictions=0 within hours), steadily growing old-gen heap to the container limit over ~22h, causing OOMKilled (exit 137) restarts and given-up consumer lag while restarting. Fault originates in the notification-service code release.

**Causal chain**
1. dep-5402 deploys unbounded ConcurrentHashMap template cache [F1]
2. cache grows without eviction (size=51750, evictions=0) [F4]
3. old-gen heap climbs monotonically from ~48% to ~98% in ~22h [F2, F3, F6]
4. container hits memory limit -> OOMKilled exit 137, restarts accumulate [F5]
5. consumer stalls during restart -> consumer_lag spikes and emails delayed [F10]

**Supporting evidence**
  - `F1` ✔ notification-service was deployed as v3.8.0 (dep-5402) at 2026-03-21T00:00:00Z, adding an in-memory template cache: 'Add in-memory template cache for email rendering', change 'TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment'.
  - `F2` ✔ notification-service memory_pct was flat at ~48% before the deploy (2026-03-20 all buckets avg 47.5-48.9) and began a monotonic climb immediately after 2026-03-21T00:00, reaching 52.01 at 01:45, 56.4 at 03:40, 60.43 at 05:35, ... 94.81 at 20:55.
  - `F3` ✔ notification-service memory shows a repeating sawtooth: climbs to ~95-98% then resets to ~48% roughly every 22 hours (peaks 2026-03-21T20:00 max 95.09, 2026-03-22T20:00 max 97.98, 2026-03-23T18:00 max 98.44).
  - `F4` ✔ notification-service logged the template cache growing unbounded with zero evictions: 'TemplateCache stats: size=51750 entries, evictions=0' at 2026-03-21T05:45:44Z.
  - `F5` ✔ notification-service restarts (cumulative) stayed 0 until 2026-03-21T22:00, then 1, 2 (2026-03-22T22:00), and 3 (2026-03-23T20:00); three OOMKilled ERROR events logged: 'container notification-service-7d9f OOMKilled (exit code 137), restarting' at 2026-03-21T22:59:50, 2026-03-22T21:59:47, and 2026-03-23T19:59:21.
  - `F6` ✔ notification-service heap/old-gen warnings begin 2026-03-21T21:37:01 ('heap usage 8% over 90 (old gen), full GC took 4317ms'), 35 occurrences total, always old-gen over-90% with multi-second full GC.
  - `F14` ✔ A memory leak in the template cache introduced by dep-5402 explains the observed pattern: unbounded ConcurrentHashMap with evictions=0, monotonic old-gen growth, ~22h to OOM, and reset on restart.
**Contradicting evidence**
  - `F9` ✔ The only config change in the window is cfg-2410: kafka log.retention.ms reduced from 604800000 to 259200000 at 2026-03-23T18:00:00Z ('Kafka maintenance: reduce retention to 3 days').
  - `F10` ✔ notification-service consumer_lag spikes occur at 2026-03-21T22:00 (max 17161.92), 2026-03-22T20:00-22:00 (max 17611.66/14375.74) and 2026-03-23T18:00-20:00 (max 18137.93/14213.61), i.e. clustered around the OOM/restart events rather than after the 18:00 maintenance.
**Assumptions**
  - The template cache keys are high-cardinality (userSegment) so entries are effectively never reused and never expire
  - No separate memory limit change occurred in the window

### H2: Kafka retention change / replay overwhelms the consumer
Kafka maintenance at 2026-03-23T18:00 (cfg-2410, retention 7d->3d) triggered a replay/rebalance that flooded notification-service's consumer, causing lag and OOM. This is the human-reported theory.

**Causal chain**
1. cfg-2410 retention reduction triggers partition replay [F9]
2. consumer processes burst -> memory pressure and lag *(assumed)*

**Supporting evidence**
  - `F9` ✔ The only config change in the window is cfg-2410: kafka log.retention.ms reduced from 604800000 to 259200000 at 2026-03-23T18:00:00Z ('Kafka maintenance: reduce retention to 3 days').
  - `F10` ✔ notification-service consumer_lag spikes occur at 2026-03-21T22:00 (max 17161.92), 2026-03-22T20:00-22:00 (max 17611.66/14375.74) and 2026-03-23T18:00-20:00 (max 18137.93/14213.61), i.e. clustered around the OOM/restart events rather than after the 18:00 maintenance.
**Contradicting evidence**
  - `F2` ✔ notification-service memory_pct was flat at ~48% before the deploy (2026-03-20 all buckets avg 47.5-48.9) and began a monotonic climb immediately after 2026-03-21T00:00, reaching 52.01 at 01:45, 56.4 at 03:40, 60.43 at 05:35, ... 94.81 at 20:55.
  - `F3` ✔ notification-service memory shows a repeating sawtooth: climbs to ~95-98% then resets to ~48% roughly every 22 hours (peaks 2026-03-21T20:00 max 95.09, 2026-03-22T20:00 max 97.98, 2026-03-23T18:00 max 98.44).
  - `F5` ✔ notification-service restarts (cumulative) stayed 0 until 2026-03-21T22:00, then 1, 2 (2026-03-22T22:00), and 3 (2026-03-23T20:00); three OOMKilled ERROR events logged: 'container notification-service-7d9f OOMKilled (exit code 137), restarting' at 2026-03-21T22:59:50, 2026-03-22T21:59:47, and 2026-03-23T19:59:21.
  - `F7` ✔ notification-service CPU stayed flat at ~25% (min 21.78, max 28.5, mean 25.01) across the entire 2026-03-20 to 2026-03-23 window, showing no CPU surge.
  - `F8` ✔ notification-service request_rate stayed flat at ~140 req/s (min 115.35, max 172.03, mean 140.24) throughout, with no traffic surge after the Kafka maintenance.
  - `F11` ✔ notification-service latency_p99_ms stayed flat at ~110ms (min 96.63, max 124.49, mean 110.06) across the window, so the service was not CPU/latency-bound.
  - `F12` ✔ Kafka logs in the maintenance window show no broker errors or rebalance events; the only WARN is 'deprecated TLS cipher offered by client 23ad0a46' at 2026-03-23T17:17:15Z.
**Assumptions**
  - Replay would produce a CPU/throughput surge
**Rejected because:** Leaks/OOMs occurred on 03-21 and 03-22, before the 03-23T18:00 Kafka change; CPU (F7), request_rate (F8) and latency (F11) are flat, so no replay surge occurred; Kafka logs show no rebalance/errors (F12).

### H3: Off-cluster independent memory leak / capacity limit too low
The service's memory limit is simply too low for its steady-state workload, so normal operation OOMs it periodically.

**Causal chain**
1. provisioned memory below needed steady-state *(assumed)*

**Supporting evidence**
  - `F3` ✔ notification-service memory shows a repeating sawtooth: climbs to ~95-98% then resets to ~48% roughly every 22 hours (peaks 2026-03-21T20:00 max 95.09, 2026-03-22T20:00 max 97.98, 2026-03-23T18:00 max 98.44).
**Contradicting evidence**
  - `F2` ✔ notification-service memory_pct was flat at ~48% before the deploy (2026-03-20 all buckets avg 47.5-48.9) and began a monotonic climb immediately after 2026-03-21T00:00, reaching 52.01 at 01:45, 56.4 at 03:40, 60.43 at 05:35, ... 94.81 at 20:55.
  - `F8` ✔ notification-service request_rate stayed flat at ~140 req/s (min 115.35, max 172.03, mean 140.24) throughout, with no traffic surge after the Kafka maintenance.
  - `F11` ✔ notification-service latency_p99_ms stayed flat at ~110ms (min 96.63, max 124.49, mean 110.06) across the window, so the service was not CPU/latency-bound.
**Assumptions**
  - Traffic is the driver of memory
**Rejected because:** Memory was stable at ~48% for the entire day before dep-5402 (F2) and request_rate is flat (F8); the climb is monotonic and eviction-free, i.e. a leak, not steady-state load.

## Facts (verified observations)
- notification-service v3.8.0 (dep-5402) deployed 2026-03-21T00:00 added an in-memory template cache keyed by template+locale+userSegment. [F1]
- Memory was flat ~48% before the deploy and climbed monotonically starting immediately after it. [F2]
- Memory follows a ~22h sawtooth from ~48% to ~98% then resets. [F3]
- The template cache grew to size=51750 entries with evictions=0. [F4]
- Three OOMKilled (exit 137) restarts occurred, the latest at 2026-03-23T19:59:21Z. [F5]
- Old-gen heap over-90% full-GC warnings began 2026-03-21T21:37. [F6]
- CPU stayed flat ~25%, request_rate ~140 req/s, and p99 ~110ms across the window. [F7, F8, F11]
- The Kafka retention config change is dated 2026-03-23T18:00, after the 03-21 and 03-22 leaks. [F9]
- consumer_lag spikes cluster around the OOM/restart events, not after the Kafka maintenance. [F10]
- Kafka logs show no broker errors or rebalance in the maintenance window. [F12]

## Assumptions (not verified)
- Template cache keys (template+locale+userSegment) are high-cardinality enough that entries are never reused, so the map only grows.
- No independent memory-limit or JVM-flag change occurred in the window (none appeared in get_config_changes).
- The pre-dep-5402 version did not exhibit the sawtooth (inferred from flat ~48% baseline on 03-20).

## Timeline
- `2026-03-20T00:00:00Z` Baseline: notification-service memory flat ~48%, restarts 0. [F2, F5]
- `2026-03-21T00:00:00Z` Deploy dep-5402 (v3.8.0) adds in-memory template cache. [F1]
- `2026-03-21T01:45:00Z` Memory begins monotonic climb (52%), never returning to baseline. [F2]
- `2026-03-21T05:45:44Z` TemplateCache logs size=51750 entries, evictions=0. [F4]
- `2026-03-21T21:37:01Z` First old-gen >90% full-GC warnings. [F6]
- `2026-03-21T22:59:50Z` First OOMKilled (exit 137) restart; consumer_lag spike to ~17k. [F5, F10]
- `2026-03-22T21:59:47Z` Second OOMKilled restart after second ~22h sawtooth. [F5, F3]
- `2026-03-23T18:00:00Z` Kafka maintenance cfg-2410 reduces retention 7d->3d (coincidental). [F9]
- `2026-03-23T19:59:21Z` Third OOMKilled restart; reported incident. [F5]

## Recommended actions
- **[immediate]** Roll back notification-service to the pre-dep-5402 version (or disable the template cache) and monitor whether the memory sawtooth disappears. -- The leak onset coincides exactly with dep-5402 and the cache has evictions=0; rollback is the fastest way to stop repeated OOMKills. [F1, F2, F4, F5]
- **[short_term]** Bound the TemplateCache: add a maximum size / LRU or TTL eviction and a bounded key cardinality. -- Unbounded ConcurrentHashMap keyed by template+locale+userSegment will always grow without eviction. [F1, F4]
- **[short_term]** Add a memory-leak alert on steady memory growth / container memory-utilization slope and on restart count, rather than waiting for OOMKilled. -- The leak grew for ~22h before each OOM; earlier detection avoids the multi-restart pattern. [F3, F5]
- **[long_term]** Add a unit/soak test that asserts template-cache size stays bounded under high-cardinality keys. -- Prevents regressions of this class in email rendering paths. [F4]

## Data gaps
- email-provider has no telemetry; its availability/latency could not be observed directly (only via notification-service, which shows flat latency).
- No pod memory-limit configuration data was available to confirm the exact container ceiling; the limit is inferred from the ~98% sawtooth peak.
- No JVM heap dump or detailed heap-composition metric exists to attribute per-object retention 100% to the template cache; attribution rests on the deploy correlation and the cache log line.

## Investigation stats
- latency 60.19s, LLM calls 5, tokens 62244 (in 54598 / out 7646)
- tool calls 16 (1 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
