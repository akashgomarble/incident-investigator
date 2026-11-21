# Incident investigation report: INC-004
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The recurring OOMKills of notification-service are a heap leak introduced by deployment dep-5402 (v3.8.0, 2026-03-21T00:00Z), which added an in-memory TemplateCache (ConcurrentHashMap keyed by template+locale+userSegment) without eviction. Starting at the deploy, memory_pct climbed monotonically from a flat ~48% to ~98% over ~20h, with escalating old-gen GC warnings, then OOMKilled (exit 137) at ~22:59 (03-21), ~21:59 (03-22) and ~19:59 (03-23). The sawtooth repeats on a ~24h cycle. The reported Kafka maintenance (cfg-2410, 2026-03-23T18:00Z retention reduction) is NOT the cause: it happened two days after the first OOMs, it reduces (deletes) rather than replays data, and consumer-lag spikes follow the restarts rather than preceding them. CPU, request rate, latency and the external dependency were all stable, ruling out load, saturation and dependency failure. Recommended fix: revert/rollback dep-5402 or bound the TemplateCache with an eviction policy, and raise the memory limit as a temporary mitigation.

## Probable root cause
**Unbounded in-memory template cache from v3.8.0 leaks heap, causing periodic OOMKills** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.82, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded in-memory template cache from v3.8.0 leaks heap, causing periodic OOMKills | bad_deployment | notification-service | dep-5402 | 0.82 (high) | active |
| 2 | H2: Kafka maintenance / retention change triggered a consumer replay that OOMs the consumer | config_change | kafka | cfg-2410 | 0.08 (low) | rejected |
| 3 | H3: Load / external dependency saturation caused the restarts | capacity_saturation | notification-service | - | 0.05 (low) | rejected |

### H1: Unbounded in-memory template cache from v3.8.0 leaks heap, causing periodic OOMKills
Deployment dep-5402 (v3.8.0, 2026-03-21T00:00Z) introduced an in-memory TemplateCache (ConcurrentHashMap keyed by template+locale+userSegment) that grows without eviction. Heap fills monotonically over ~20h until the container exceeds its memory limit and is OOMKilled; each restart is followed by a Kafka consumer-lag spike as the consumer re-reads/re-processes, which is why the trade-confirmation emails were delayed. This is the origin of the fault (a bad code release), not the Kafka maintenance.

**Causal chain**
1. dep-5402 adds in-memory TemplateCache with no eviction [F2, F8]
2. memory_pct ramps monotonically from the deploy onward [F1]
3. old-gen heap pressure causes long GCs then OOMKill [F7, F3]
4. restart produces a consumer-lag spike / delayed emails [F5]

**Supporting evidence**
  - `F1` ✔ notification-service memory_pct was flat at ~48% through 2026-03-21T00:00Z, then began a monotonic ramp (+~2.2%/hr) reaching 95-98% by 2026-03-21T18:00-22:00Z; the identical ramp-then-crash sawtooth repeats every ~24h.
  - `F2` ✔ The memory ramp onset coincides with deployment dep-5402 (v3.8.0) which added an in-memory template cache, deployed 2026-03-21T00:00:00Z.
  - `F3` ✔ OOMKilled restarts occurred at ~22:59 on 03-21, ~21:59 on 03-22, and ~19:59 on 03-23 (three restarts this week), matching the sawtooth cycle; the restarts counter is cumulative 0->1->2->3.
  - `F7` ✔ Logs show escalating 'heap usage N% over 90 (old gen), full GC took Nms' warnings starting 2026-03-21T21:37Z and continuing through 03-23, with GC pauses growing to 6-8s, consistent with old-gen pressure from retained objects.
  - `F8` ✔ The dep-5402 rollout is recorded complete in notification-service logs at 2026-03-21T00:00:14Z, confirming the deploy actually went live in this service.
**Contradicting evidence**
  - (none)
**Assumptions**
  - TemplateCache has no eviction/size bound (summary does not state an eviction policy)
  - no other change in the same window caused the leak

### H2: Kafka maintenance / retention change triggered a consumer replay that OOMs the consumer
cfg-2410 (kafka log.retention.ms reduced at 2026-03-23T18:00Z) caused a replay/lag storm that the consumer choked on, exhausting memory.

**Causal chain**
1. retention change at kafka triggers replay [F4]
2. consumer buffers replayed messages and OOMs [F5]

**Supporting evidence**
  - `F5` ✔ Consumer lag was normal (~60 messages) throughout and only spiked to 2273/790 around the OOM windows (03-21T22:00, 03-22T20:00, 03-23T18:00-20:00), i.e. lag spikes coincide with and follow the restarts rather than preceding them.
**Contradicting evidence**
  - `F1` ✔ notification-service memory_pct was flat at ~48% through 2026-03-21T00:00Z, then began a monotonic ramp (+~2.2%/hr) reaching 95-98% by 2026-03-21T18:00-22:00Z; the identical ramp-then-crash sawtooth repeats every ~24h.
  - `F3` ✔ OOMKilled restarts occurred at ~22:59 on 03-21, ~21:59 on 03-22, and ~19:59 on 03-23 (three restarts this week), matching the sawtooth cycle; the restarts counter is cumulative 0->1->2->3.
  - `F4` ✔ The recurring OOMs predate the Kafka retention change (cfg-2410 at 2026-03-23T18:00:00Z) by two days; OOMs occurred on 03-21 and 03-22 before any Kafka change.
**Assumptions**
  - reducing retention could cause a replay (implausible: reducing retention deletes data rather than replaying it)
**Rejected because:** The OOMs occurred on 03-21 and 03-22, two days before cfg-2410; retention was reduced (data deleted), not replayed; and lag spikes follow the restarts rather than preceding them.

### H3: Load / external dependency saturation caused the restarts
A traffic surge or email-provider/external degradation exhausted memory.

**Causal chain**
1. traffic or dependency pressure drove memory growth *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ CPU, request rate, and p99 latency stayed flat across the whole period (cpu ~25%, request_rate ~140 req/s, latency ~110ms), so no load surge, CPU saturation, or external-dependency degradation drove the OOMs.
  - `F1` ✔ notification-service memory_pct was flat at ~48% through 2026-03-21T00:00Z, then began a monotonic ramp (+~2.2%/hr) reaching 95-98% by 2026-03-21T18:00-22:00Z; the identical ramp-then-crash sawtooth repeats every ~24h.
**Rejected because:** request_rate, cpu_pct and latency are flat across the period; growth is strictly monotonic from the deploy, not demand-linked.

## Facts (verified observations)
- notification-service memory_pct was flat ~48% through 2026-03-21T00:00Z then ramped monotonically to ~98% before crashing, on a repeating ~24h cycle. [F1]
- Deployment dep-5402 (v3.8.0) added an in-memory template cache and was deployed 2026-03-21T00:00:00Z, coinciding with the memory ramp onset. [F2, F8]
- OOMKilled (exit 137) restarts occurred on 03-21, 03-22 and 03-23 (three this week), with the cumulative restarts counter reaching 3. [F3]
- The OOMs predate the Kafka retention change cfg-2410 (2026-03-23T18:00Z) by two days. [F4]
- Consumer lag was ~60 messages normally and only spiked around/after the OOM restarts, not before them. [F5]
- CPU (~25%), request_rate (~140 req/s) and p99 latency (~110ms) were flat throughout, ruling out load and dependency causes. [F6]
- Old-gen heap warnings and GC pauses of 6-8s escalated from 2026-03-21T21:37Z onward. [F7]

## Assumptions (not verified)
- The Summary of dep-5402 does not state an eviction policy, so the cache is assumed unbounded (leak source).
- No other unlisted change to notification-service in the same window contributed to the leak.
- The template cache is rendered per request so a template+locale+userSegment-keyed map grows with distinct user segments.

## Timeline
- `2026-03-21T00:00:00Z` dep-5402 (v3.8.0) 'Add in-memory template cache' deployed; rollout complete 00:00:14Z [F2, F8]
- `2026-03-21T02:00:00Z` memory_pct begins monotonic ramp from flat ~48% baseline [F1]
- `2026-03-21T21:37:01Z` Escalating old-gen heap-over-90% / multi-second full-GC warnings begin [F7]
- `2026-03-21T22:59:50Z` First OOMKilled (exit 137) restart [F3]
- `2026-03-22T21:59:47Z` Second OOMKilled restart [F3]
- `2026-03-23T18:00:00Z` Kafka cfg-2410: log.retention.ms reduced 7d->3d (occurs AFTER two prior OOMs) [F4]
- `2026-03-23T19:59:21Z` Third OOMKilled restart; trade-confirmation emails delayed ~25 min [F3]

## Recommended actions
- **[immediate]** Roll back dep-5402 / revert v3.8.0, or hotfix the TemplateCache with a bounded eviction policy (size/TTL/LRU). -- The cache introduced at the deploy matches the monotonic memory ramp that began immediately after it; removing or bounding it should stop the leak. [F1, F2, F8]
- **[immediate]** As a stop-gap, raise the container memory limit for notification-service to reduce OOM frequency while the fix is prepared. -- Buys headroom; not a fix since the leak continues. [F1, F3]
- **[short_term]** Verify the cache's key cardinality and add cache-size/heap metrics and alerts on old-gen usage trend. -- Distinct template+locale+userSegment keys grow unboundedly; metrics would catch recurrence early. [F2, F7]
- **[short_term]** Do not attribute this to the Kafka retention change; document that cfg-2410 is a red herring so the platform team does not revert a correct change. -- OOMs predate cfg-2410 and lag spikes follow restarts. [F4, F5]
- **[long_term]** Add a bounded-memory / cache-growth unit test or static check for in-memory caches in the service. -- Prevents recurrence of unbounded caches. [F2]

## Data gaps
- request_rate query (tc-008) returned 503 backend unavailable; retried as tc-011 with a wider bucket and succeeded, so the gap was closed.
- An ERROR-level log query for 2026-03-20T12:00Z-2026-03-21T06:00Z (tc-017) timed out and could not confirm absence of pre-deploy errors; however memory_pct data already shows a flat baseline before the deploy.
- No metrics/telemetry for the external email-provider (has_telemetry:false), so its health could not be directly inspected; ruled out indirectly via flat latency.
- The eviction policy of the TemplateCache is not stated in the deployment summary; unbounded growth is inferred from the metric pattern rather than the code.

## Investigation stats
- latency 64.52s, LLM calls 5, tokens 63313 (in 56086 / out 7227)
- tool calls 17 (2 failed, 0 partial, 3 recovered by retry), findings 9/9 verified
