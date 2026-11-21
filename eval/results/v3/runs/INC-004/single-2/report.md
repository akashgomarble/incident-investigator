# Incident investigation report: INC-004
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
notification-service is being OOMKilled by an unbounded in-memory template cache (TemplateCache, ConcurrentHashMap keyed by template+locale+userSegment) introduced by deployment dep-5402 (v3.8.0) on 2026-03-21T00:00:14Z. The cache grows with zero evictions (evictions=0; 9000 entries -> 18000 entries in one hour; 188100 entries at the peak), and memory_pct climbs in a linear sawtooth from ~48% to ~98% before each restart — a stable ~24h cycle producing restarts at Mar 21 22:00, Mar 22 22:00 and Mar 23 20:00 (the third restart, exit code 137 at 19:59:21Z). CPU (~25%), request_rate (~140 req/s) and latency (~110ms) are all flat, ruling out load/capacity causes. The reported Kafka maintenance (cfg-2410, retention reduction at 18:00 Mar 23) is coincidental: the identical OOM cycle occurred on Mar 21 and Mar 22 with no config change, kafka shows 0 under-replicated partitions and no rebalance/error logs, and consumer_lag only spikes during the restart windows (lag is a consequence of the consumer being down, not the trigger). Root cause: bad deployment (unbounded cache) in notification-service.

## Probable root cause
**Unbounded in-memory TemplateCache introduced by dep-5402 leaks heap until OOM** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded in-memory TemplateCache introduced by dep-5402 leaks heap until OOM | bad_deployment | notification-service | dep-5402 | 0.90 (high) | active |
| 2 | H2: Kafka maintenance (cfg-2410 retention change) triggered a replay that overran the consumer | config_change | kafka | cfg-2410 | 0.08 (low) | rejected |
| 3 | H3: Consumer replay/lag is the primary fault with no faulty change | capacity_saturation | notification-service | - | 0.05 (low) | rejected |

### H1: Unbounded in-memory TemplateCache introduced by dep-5402 leaks heap until OOM
Deploy dep-5402 (v3.8.0) added an in-memory ConcurrentHashMap template cache with no eviction. Under steady traffic the cache grows without bound (~9000 entries/hour, evictions=0), linearly consuming heap (old gen) until the container hits its memory limit and is OOMKilled, restarting ~every 24h. The reported Kafka replay/lag is a downstream symptom of the restart, not the cause.

**Causal chain**
1. dep-5402 deploys in-memory template cache without eviction [F6]
2. Cache grows unbounded: evictions=0, 9000->18000 entries/hour [F5]
3. Heap memory climbs linearly from ~48% to ~98% and resets on restart (24h sawtooth) [F1, F2]
4. Container OOMKilled (exit 137), restart count increments once per cycle [F4, F3]
5. Consumer lag spikes only during restart windows; kafka broker itself healthy [F13, F12]

**Supporting evidence**
  - `F1` ✔ notification-service memory_pct was flat at ~48% through 2026-03-20 (pre-deploy), then began a steady linear climb from 2026-03-21T02:00Z (~52.7%) onward.
  - `F2` ✔ notification-service memory shows a repeating ~24h sawtooth: climbing to ~98% then resetting to ~48% (resets at Mar 21 22:00, Mar 22 22:00, Mar 23 20:00), consistent with OOM restarts, not a load spike.
  - `F3` ✔ notification-service restarts were 0 for the entire pre-deploy day, then incremented to 1 (Mar 21 22:00), 2 (Mar 22 22:00) and 3 (Mar 23 20:00) — one restart per ~24h.
  - `F4` ✔ notification-service OOMKilled with exit code 137 was logged at 2026-03-23T19:59:21Z.
  - `F5` ✔ TemplateCache stats show an unbounded cache with zero evictions: size=188100 entries, evictions=0 at 18:54; after restart size=9000 entries, evictions=0 at 21:00; then size=18000 entries, evictions=0 at 22:00 — growing ~9000 entries/hour with no eviction.
  - `F6` ✔ Deployment dep-5402 (v3.8.0, 'Add in-memory template cache for email rendering', keyed by template+locale+userSegment) is live and completed rollout at 2026-03-21T00:00:14Z.
  - `F7` ✔ The memory climb began ~2h after dep-5402 rollout: first GC-pause warning 'GC pause 253ms exceeded 200ms target' at 2026-03-21T03:00:44Z, with repeated old-gen 'heap usage ... over 90 (old gen)' warnings from 18:34Z on Mar 23.
  - `F8` ✔ notification-service CPU was flat at ~25% throughout (min 21.78, max 28.5) with no rise during the memory climb, ruling out CPU saturation.
  - `F9` ✔ notification-service request_rate was flat ~140 req/s across the whole window (min 115.35, max 172.03) with no increase at onset, so the memory growth is not traffic-driven.
  - `F10` ✔ notification-service latency_p99 was flat ~110ms throughout (min 96.63, max 124.49), so latency did not degrade before the OOM.
  - `F12` ✔ kafka showed 0 under-replicated partitions for every 30-min bucket from 16:00 to 23:30 on Mar 23, and its message intake rate was steady ~2400 msg/s across the window.
  - `F13` ✔ notification-service consumer_lag spikes (e.g. avg 2273 at Mar 21 22:00, avg 810 at Mar 23 18:00, avg 1542 at Mar 23 20:00) coincide with the restart windows, i.e. lag accumulates while the consumer is restarting, not before the fault.
  - `F15` ✔ No deployment occurred on any service between 2026-03-23T16:00Z and 23:59Z.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The cache is not bounded by any TTL/size config elsewhere (evictions=0 in logs supports this)
  - ~9000 entries/hour at steady ~140 req/s is consistent with the observed memory slope

### H2: Kafka maintenance (cfg-2410 retention change) triggered a replay that overran the consumer
The Kafka maintenance at 2026-03-23T18:00Z that reduced log.retention.ms drove a consumer replay/rebalance, spiking lag and causing notification-service to choke and OOM.

**Causal chain**
1. kafka retention reduced to 3 days at 18:00 [F11]
2. consumer lag spiked on notification-service [F13]
3. OOM followed [F4]

**Supporting evidence**
  - `F11` ✔ The only config change in the window was cfg-2410 on kafka (log.retention.ms 604800000 -> 259200000, 'Kafka maintenance: reduce retention to 3 days') at 2026-03-23T18:00:00Z.
  - `F13` ✔ notification-service consumer_lag spikes (e.g. avg 2273 at Mar 21 22:00, avg 810 at Mar 23 18:00, avg 1542 at Mar 23 20:00) coincide with the restart windows, i.e. lag accumulates while the consumer is restarting, not before the fault.
**Contradicting evidence**
  - `F1` ✔ notification-service memory_pct was flat at ~48% through 2026-03-20 (pre-deploy), then began a steady linear climb from 2026-03-21T02:00Z (~52.7%) onward.
  - `F2` ✔ notification-service memory shows a repeating ~24h sawtooth: climbing to ~98% then resetting to ~48% (resets at Mar 21 22:00, Mar 22 22:00, Mar 23 20:00), consistent with OOM restarts, not a load spike.
  - `F3` ✔ notification-service restarts were 0 for the entire pre-deploy day, then incremented to 1 (Mar 21 22:00), 2 (Mar 22 22:00) and 3 (Mar 23 20:00) — one restart per ~24h.
  - `F5` ✔ TemplateCache stats show an unbounded cache with zero evictions: size=188100 entries, evictions=0 at 18:54; after restart size=9000 entries, evictions=0 at 21:00; then size=18000 entries, evictions=0 at 22:00 — growing ~9000 entries/hour with no eviction.
  - `F12` ✔ kafka showed 0 under-replicated partitions for every 30-min bucket from 16:00 to 23:30 on Mar 23, and its message intake rate was steady ~2400 msg/s across the window.
  - `F14` ✔ kafka logs during the maintenance window show only one benign WARN ('deprecated TLS cipher offered by client'), with no rebalance/reassignment/error entries.
  - `F15` ✔ No deployment occurred on any service between 2026-03-23T16:00Z and 23:59Z.
  - `F8` ✔ notification-service CPU was flat at ~25% throughout (min 21.78, max 28.5) with no rise during the memory climb, ruling out CPU saturation.
  - `F9` ✔ notification-service request_rate was flat ~140 req/s across the whole window (min 115.35, max 172.03) with no increase at onset, so the memory growth is not traffic-driven.
**Assumptions**
  - A retention reduction could force a replay
**Rejected because:** Memory climb and OOM restarts began on 2026-03-21, two days before the 18:00 Mar 23 Kafka change; the same ~24h sawtooth repeats on Mar 21 and Mar 22 with no config change. kafka shows 0 under-replicated partitions and no rebalance/error logs during the maintenance window, and a retention reduction (deleting old segments) does not cause consumers to re-read data.

### H3: Consumer replay/lag is the primary fault with no faulty change
notification-service is simply overwhelmed by message volume/replay, saturating memory via in-flight buffering; lag is the cause rather than a symptom.

**Causal chain**
1. message volume causes lag and memory pressure [F13]

**Supporting evidence**
  - `F13` ✔ notification-service consumer_lag spikes (e.g. avg 2273 at Mar 21 22:00, avg 810 at Mar 23 18:00, avg 1542 at Mar 23 20:00) coincide with the restart windows, i.e. lag accumulates while the consumer is restarting, not before the fault.
**Contradicting evidence**
  - `F8` ✔ notification-service CPU was flat at ~25% throughout (min 21.78, max 28.5) with no rise during the memory climb, ruling out CPU saturation.
  - `F9` ✔ notification-service request_rate was flat ~140 req/s across the whole window (min 115.35, max 172.03) with no increase at onset, so the memory growth is not traffic-driven.
  - `F10` ✔ notification-service latency_p99 was flat ~110ms throughout (min 96.63, max 124.49), so latency did not degrade before the OOM.
  - `F12` ✔ kafka showed 0 under-replicated partitions for every 30-min bucket from 16:00 to 23:30 on Mar 23, and its message intake rate was steady ~2400 msg/s across the window.
  - `F5` ✔ TemplateCache stats show an unbounded cache with zero evictions: size=188100 entries, evictions=0 at 18:54; after restart size=9000 entries, evictions=0 at 21:00; then size=18000 entries, evictions=0 at 22:00 — growing ~9000 entries/hour with no eviction.
  - `F2` ✔ notification-service memory shows a repeating ~24h sawtooth: climbing to ~98% then resetting to ~48% (resets at Mar 21 22:00, Mar 22 22:00, Mar 23 20:00), consistent with OOM restarts, not a load spike.
**Assumptions**
  - Lag could buffer unboundedly in memory
**Rejected because:** request_rate, CPU and latency are all flat; memory consumption tracks cache entry count not message rate; kafka message rate is steady ~2400/s; lag spikes only during restart windows.

## Facts (verified observations)
- notification-service memory was flat ~48% pre-deploy and began a linear climb from Mar 21 02:00 onward. [F1]
- Memory follows a ~24h sawtooth resetting on restart, matching OOM restarts rather than load. [F2]
- Restart count went 0 -> 1 -> 2 -> 3 at Mar 21 22:00, Mar 22 22:00, Mar 23 20:00 — one per cycle. [F3]
- An OOMKilled (exit code 137) event was logged at 2026-03-23T19:59:21Z. [F4]
- TemplateCache logs show evictions=0 with size growing 9000->18000 entries/hour and 188100 at peak. [F5]
- dep-5402 (v3.8.0) added an in-memory template cache and rolled out at 2026-03-21T00:00:14Z. [F6]
- First GC-pause warning appeared 2026-03-21T03:00:44Z, ~3h after dep-5402 rollout. [F7]
- CPU stayed flat ~25% with no rise during the memory climb. [F8]
- request_rate stayed flat ~140 req/s across the window. [F9]
- latency_p99 stayed flat ~110ms across the window. [F10]
- The only config change was kafka log.retention.ms reduced to 3 days at 18:00 Mar 23. [F11]
- kafka had 0 under-replicated partitions and steady ~2400 msg/s intake. [F12]
- notification-service consumer_lag spikes coincide with restart windows. [F13]
- kafka logs during maintenance show only a benign TLS-cipher warning, no rebalance/errors. [F14]
- No deployments occurred on any service after 2026-03-23T16:00Z. [F15]

## Assumptions (not verified)
- The template cache has no TTL/size cap configured elsewhere; evictions=0 in logs strongly supports this.
- The ~9000 entries/hour growth rate is consistent with the steady request rate and the observed ~1.5-2%/hour memory slope.
- The 48%->98% memory cycle is heap/anonymous memory growth from the cache rather than native/off-heap leaks not visible in cache stats.

## Timeline
- `2026-03-20T00:00:00Z` Baseline: memory_pct flat ~48%, restarts=0, CPU ~25%, stable. [F1, F3]
- `2026-03-21T00:00:14Z` Deployment dep-5402 (v3.8.0) 'Add in-memory template cache for email rendering' rollout complete. [F6]
- `2026-03-21T02:00:00Z` memory_pct begins steady linear climb (~52.7% and rising). [F1]
- `2026-03-21T03:00:44Z` First GC-pause warning: 'GC pause 253ms exceeded 200ms target'. [F7]
- `2026-03-21T22:00:00Z` First OOM restart (restarts=1); memory resets to ~48%; consumer_lag spikes to avg 2273. [F3, F13]
- `2026-03-22T22:00:00Z` Second OOM restart (restarts=2); memory resets; consumer_lag spikes (avg 790). [F3, F13]
- `2026-03-23T18:00:00Z` Kafka maintenance cfg-2410: log.retention.ms reduced to 3 days. [F11]
- `2026-03-23T18:54:31Z` TemplateCache stats: size=188100 entries, evictions=0. [F5]
- `2026-03-23T19:59:21Z` Third OOMKilled (exit code 137), container restarting. [F4]
- `2026-03-23T20:00:00Z` restarts=3; memory resets to ~48%; consumer_lag spikes (avg 1542); emails delayed ~25 min. [F3, F13]
- `2026-03-23T21:00:44Z` Post-restart TemplateCache stats: size=9000 entries, evictions=0 (cache refilling immediately). [F5]
- `2026-03-23T22:00:34Z` TemplateCache stats: size=18000 entries, evictions=0 — linear unbounded growth continues. [F5]

## Recommended actions
- **[immediate]** Roll back notification-service to the pre-dep-5402 version (or disable the TemplateCache code path), then confirm the memory sawtooth disappears. -- The memory climb maps exactly to the unbounded TemplateCache introduced by dep-5402; rollback removes the leaking component and restores the flat ~48% baseline. [F1, F2, F5, F6]
- **[short_term]** Add a bounded cache (maximumSize/TTL, e.g. Caffeine with eviction) to the template cache and emit eviction metrics. -- The root cause is an unbounded map; a size/TTL cap with eviction prevents unbounded heap growth. [F5]
- **[short_term]** Add an alert on old-gen heap trend / restarts and on TemplateCache entry count so growth is caught before OOM. -- The leak ran for 24h before each OOM; a heap-growth alert would have caught it on Mar 21. [F1, F2]
- **[short_term]** Treat the Kafka maintenance theory as a red herring; do not pursue a replay/throttling remediation for this incident. -- Identical OOM cycles predate the kafka change and kafka was healthy (0 under-replicated partitions, no rebalance logs); lag only spikes during restarts. [F11, F12, F13, F14, F15]

## Data gaps
- No heap-dump / per-object memory allocation breakdown to confirm the TemplateCache is the dominant heap consumer (inferred from cache stats + timing, not directly measured).
- TemplateCache logs appear only on Mar 23; cache-size history for Mar 21-22 is not available to directly tie entry count to the earlier climbs.
- No email-provider telemetry (external node has has_telemetry=false), so its contribution to email delays cannot be assessed.

## Investigation stats
- latency 46.76s, LLM calls 5, tokens 69361 (in 61128 / out 8233)
- tool calls 19 (1 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
