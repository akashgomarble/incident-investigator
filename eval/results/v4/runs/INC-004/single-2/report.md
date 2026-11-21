# Incident investigation report: INC-004
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
notification-service is being OOMKilled ~every 22h by a steady heap leak, not by a Kafka replay. Deploy dep-5402 (v3.8.0, 2026-03-21T00:00, 'Add in-memory template cache for email rendering') added a ConcurrentHashMap TemplateCache keyed by template+locale+userSegment with no eviction bound. Immediately after the deploy, memory_pct switched from flat ~48% (stable for days) to a monotonic linear climb, producing OOMKilled restarts at 03-21T22:59, 03-22T21:59 and 03-23T19:59; the cache itself logs 'size=188100 entries, evictions=0' (then reset to 9000 after restart). The reported Kafka-replay theory is contradicted: the retention config change (cfg-2410) happened 2026-03-23T18:00, ~21h after the climb began and after two prior OOMs; consumer_lag is normal (~50-90) until the restart instant, so lag/delayed emails are a downstream symptom of the restart. CPU (~25%) and request_rate (~140 req/s) are flat, ruling out load/capacity. Root cause: bad_deployment in notification-service.

## Probable root cause
**Unbounded in-memory TemplateCache leak introduced by deploy v3.8.0 causes OOMKill** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.88, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded in-memory TemplateCache leak introduced by deploy v3.8.0 causes OOMKill | bad_deployment | notification-service | dep-5402 | 0.88 (high) | active |
| 2 | H2: Kafka replay/retention change after maintenance overloads consumer | dependency_failure | kafka | cfg-2410 | 0.05 (low) | rejected |
| 3 | H3: Load/capacity growth or resource exhaustion without a triggering change | capacity_saturation | notification-service | - | 0.03 (low) | rejected |

### H1: Unbounded in-memory TemplateCache leak introduced by deploy v3.8.0 causes OOMKill
Deploy dep-5402 (v3.8.0) added a TemplateCache (ConcurrentHashMap keyed by template+locale+userSegment) with no eviction bound. The cache grows monotonically (~100 entries/min), steadily consuming heap until the container hits its memory limit and is OOMKilled roughly every 22h; the resulting restart causes the ~25 min email delay and transient consumer-lag/5xx spikes. Fault originates in notification-service code.

**Causal chain**
1. dep-5402 deployed at 2026-03-21T00:00 adds unbounded TemplateCache [F1]
2. memory_pct inflects from flat ~48% to steady linear climb at 03-21T01:20 [F2]
3. cache reported growing with evictions=0 (188100 -> reset to 9000 after restart) [F4]
4. heap crosses limit -> OOMKilled every ~22h (03-21T22:59, 03-22T21:59, 03-23T19:59) [F3, F10]
5. restart causes consumer-lag and 5xx spikes and delayed emails [F6, F9]

**Supporting evidence**
  - `F1` ✔ notification-service deployed v3.8.0 'Add in-memory template cache for email rendering' using 'TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment' on 2026-03-21T00:00:00Z by s.menon; status live, not rolled back.
  - `F2` ✔ notification-service memory_pct was flat ~47-49% from 2026-03-18 through 2026-03-21T00:00 (e.g. 48.14 avg), then inflected to a steady linear climb starting ~2026-03-21T01:20 (50.75 -> 54.24 -> 57.82 -> 61.21 -> 68.47 -> 71.42).
  - `F3` ✔ memory_pct shows a repeating ~22h sawtooth: climbs from ~47% to ~95-98% then resets, with OOMKilled restarts at 2026-03-21T22:59, 2026-03-22T21:59 and 2026-03-23T19:59; restarts metric increments 0->1->2->3 at exactly those times.
  - `F4` ✔ TemplateCache reports unbounded growth with no eviction: 'size=188100 entries, evictions=0' at 18:54 before the OOM, then after restart 'size=9000 entries, evictions=0' at 21:00 and 'size=18000 entries, evictions=0' at 22:00.
  - `F5` ✔ CPU is flat ~25% and request_rate is flat ~140 req/s across 2026-03-19 to 2026-03-23, showing no load increase accompanying the memory climb.
  - `F6` ✔ consumer_lag for notification-service stays ~50-90 messages normally and spikes only during 2026-03-22T21:00-22:00 (avg 1525, max 17611) and 2026-03-23T19:00-20:00 (avg 1562, max 18137) — i.e. exactly during the OOM/restart windows, not before them.
  - `F10` ✔ notification-service emits repeated 'heap usage N% over 90 (old gen), full GC took ...ms' WARNs from 2026-03-21T21:37 onward, consistent with a heap filling toward OOM.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Heap is dominated by the new cache (no direct heap-dump/heap-by-object telemetry available)
  - The memory limit was unchanged (no config change for notification-service in window)

### H2: Kafka replay/retention change after maintenance overloads consumer
The reported theory: Kafka maintenance + reduced retention (cfg-2410) triggers a replay that the consumer chokes on, causing OOM. Fault would originate in/depend on Kafka.

**Causal chain**
(none given)

**Supporting evidence**
  - `F12` ✔ Historical incidents HIST-104 and HIST-112 both involve consumer lag spikes after Kafka broker maintenance/reassignment on notification-service/kafka.
  - `F6` ✔ consumer_lag for notification-service stays ~50-90 messages normally and spikes only during 2026-03-22T21:00-22:00 (avg 1525, max 17611) and 2026-03-23T19:00-20:00 (avg 1562, max 18137) — i.e. exactly during the OOM/restart windows, not before them.
  - `F7` ✔ The only config change was kafka 'log.retention.ms' from 604800000 to 259200000 (Kafka maintenance) at 2026-03-23T18:00:00Z — about 21 hours AFTER the memory climb began at 2026-03-21T01:20 and after two prior OOMs.
**Contradicting evidence**
  - `F2` ✔ notification-service memory_pct was flat ~47-49% from 2026-03-18 through 2026-03-21T00:00 (e.g. 48.14 avg), then inflected to a steady linear climb starting ~2026-03-21T01:20 (50.75 -> 54.24 -> 57.82 -> 61.21 -> 68.47 -> 71.42).
  - `F7` ✔ The only config change was kafka 'log.retention.ms' from 604800000 to 259200000 (Kafka maintenance) at 2026-03-23T18:00:00Z — about 21 hours AFTER the memory climb began at 2026-03-21T01:20 and after two prior OOMs.
  - `F8` ✔ kafka CPU is flat ~35% and no kafka WARN/ERROR related to the incident exists; kafka only logged one 'deprecated TLS cipher offered by client' at 17:17:15.
  - `F6` ✔ consumer_lag for notification-service stays ~50-90 messages normally and spikes only during 2026-03-22T21:00-22:00 (avg 1525, max 17611) and 2026-03-23T19:00-20:00 (avg 1562, max 18137) — i.e. exactly during the OOM/restart windows, not before them.
**Rejected because:** The cfg-2410 retention change landed 2026-03-23T18:00, ~21h after the memory climb began (03-21T01:20) and after two earlier OOMs (03-21T22:59, 03-22T21:59). consumer_lag stays normal (~50-90) until the OOM moment itself, then spikes — lag is a consequence of the restart, not the trigger. kafka CPU flat ~35%, no kafka errors.

### H3: Load/capacity growth or resource exhaustion without a triggering change
Traffic grew beyond provisioned capacity, or the container simply ran out of memory/resource with no faulty change.

**Causal chain**
(none given)

**Supporting evidence**
  - `F5` ✔ CPU is flat ~25% and request_rate is flat ~140 req/s across 2026-03-19 to 2026-03-23, showing no load increase accompanying the memory climb.
**Contradicting evidence**
  - `F2` ✔ notification-service memory_pct was flat ~47-49% from 2026-03-18 through 2026-03-21T00:00 (e.g. 48.14 avg), then inflected to a steady linear climb starting ~2026-03-21T01:20 (50.75 -> 54.24 -> 57.82 -> 61.21 -> 68.47 -> 71.42).
  - `F3` ✔ memory_pct shows a repeating ~22h sawtooth: climbs from ~47% to ~95-98% then resets, with OOMKilled restarts at 2026-03-21T22:59, 2026-03-22T21:59 and 2026-03-23T19:59; restarts metric increments 0->1->2->3 at exactly those times.
  - `F4` ✔ TemplateCache reports unbounded growth with no eviction: 'size=188100 entries, evictions=0' at 18:54 before the OOM, then after restart 'size=9000 entries, evictions=0' at 21:00 and 'size=18000 entries, evictions=0' at 22:00.
  - `F5` ✔ CPU is flat ~25% and request_rate is flat ~140 req/s across 2026-03-19 to 2026-03-23, showing no load increase accompanying the memory climb.
**Rejected because:** request_rate (~140 req/s) and CPU (~25%) are flat across the whole window; capacity did not change. Memory growth is a deterministic ~22h sawtooth starting exactly at the deploy, which points to a code leak (bad_deployment) rather than load outgrowing capacity.

## Facts (verified observations)
- notification-service v3.8.0 added a ConcurrentHashMap TemplateCache keyed by template+locale+userSegment, deployed 2026-03-21T00:00, not rolled back. [F1]
- Memory was flat ~48% for days then began a steady linear climb beginning ~2026-03-21T01:20, right after the v3.8.0 deploy. [F2]
- Memory follows a ~22h sawtooth with OOMKilled restarts at 03-21T22:59, 03-22T21:59, 03-23T19:59 (restarts counter 1/2/3). [F3, F10]
- TemplateCache logs unbounded size with evictions=0 (188100 entries pre-OOM; reset to 9000 post-restart). [F4]
- CPU (~25%) and request_rate (~140 req/s) are flat throughout, so load/capacity did not change. [F5]
- consumer_lag is normal (~50-90) and spikes only during the OOM/restart windows, indicating lag is a consequence of the restart. [F6]
- The Kafka retention config change (cfg-2410) occurred at 2026-03-23T18:00, ~21h after the leak began and after two earlier OOMs. [F7]
- kafka shows flat CPU ~35% and no incident-related errors. [F8]
- 5xx error_rate spikes (max ~13-14%) occur only in restart buckets; baseline ~0.2%. [F9]
- Historical incidents link consumer-lag spikes to Kafka broker maintenance/reassignment. [F12]

## Assumptions (not verified)
- The new TemplateCache dominates heap growth; no object-level heap dump telemetry is available to prove allocation source.
- container memory limit is unchanged (no notification-service config change observed in window).
- The cache is keyed by template+locale+userSegment whose cardinality is effectively unbounded, so entries accumulate without hits.

## Timeline
- `2026-03-18T00:00:00Z` Memory stable flat ~48% (pre-deploy baseline) [F2]
- `2026-03-21T00:00:00Z` Deploy dep-5402 v3.8.0 'Add in-memory template cache' goes live [F1]
- `2026-03-21T01:20:00Z` Memory begins steady linear climb (leak onset) [F2]
- `2026-03-21T21:37:01Z` Old-gen heap-over-90% GC WARNs begin [F10]
- `2026-03-21T22:59:50Z` First OOMKilled restart (restarts=1) [F3]
- `2026-03-22T21:59:47Z` Second OOMKilled restart (restarts=2) [F3]
- `2026-03-23T18:00:00Z` Kafka retention reduced to 3 days (cfg-2410) [F7]
- `2026-03-23T18:54:31Z` TemplateCache size=188100 entries, evictions=0 [F4]
- `2026-03-23T19:59:21Z` Third OOMKilled restart (restarts=3); consumer lag spike window follows [F3, F6]
- `2026-03-23T20:20:00Z` Incident reported (SEV-3), ~25 min email delay []

## Recommended actions
- **[immediate]** Roll back / revert dep-5402 (v3.8.0) or hotfix the TemplateCache to add a bounded eviction policy (size/size+TTL) and disable the cache path. -- The leak starts exactly when this deploy goes live and the cache is proven unbounded with evictions=0; removing it stops the ~22h OOM cycle. [F1, F2, F4]
- **[immediate]** Temporarily raise the container memory limit / increase restart guard to buy time and reduce customer-visible email delays until the fix ships. -- Mitigates OOM-driven restarts and the resulting ~25 min email delay and 5xx spikes. [F3, F9]
- **[short_term]** Add heap/object histograms and cache-size metrics with eviction counts so unbounded growth is detectable before OOM. -- Current telemetry needed a manual log read of 'evictions=0' to confirm the leak; proactive metrics would catch recurrence. [F4, F10]
- **[long_term]** Review TemplateCache key cardinality (template+locale+userSegment) and cap per-segment entries; add a load test asserting bounded heap over a 24h soak. -- Unbounded key space is the underlying design flaw that let the leak grow for 22h undetected. [F1, F4]

## Data gaps
- No heap-dump/object-allocation telemetry to positively attribute heap growth to TemplateCache entries.
- kafka service exposes no consumer_lag metric (only cpu_pct, messages_in_per_s, under_replicated_partitions), so the Kafka-side replay volume during 03-23T18:00-20:00 could not be directly measured.
- No notification-service config change logs exist for the window, so container memory-limit changes cannot be confirmed (assumed unchanged).
- email-provider has no telemetry, so external delay contribution cannot be measured (though not the observed failure mode).

## Investigation stats
- latency 74.17s, LLM calls 6, tokens 87118 (in 78973 / out 8145)
- tool calls 21 (1 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
