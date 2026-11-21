# Incident investigation report: INC-004
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
notification-service is being OOMKilled by unbounded heap growth from the new in-memory TemplateCache introduced in dep-5402 (v3.8.0, deployed 2026-03-21T00:00Z). Memory_pct climbs monotonically from ~47% to ~98% over ~22h and then resets, repeating daily on 03-21, 03-22 and 03-23 — a daily restart cadence matching the cumulative restarts counter (1, 2, 3) and the 03-23T19:59:21Z OOMKilled (exit 137) log. TemplateCache logs confirm the map grows to 196,650 entries with 0 evictions. CPU (~25%) and latency (~110ms) are flat; Kafka is healthy (cpu ~35%, ~2300 msg/s). The consumer_lag spike (~18k) occurs only after the 19:59 restart (and similarly after the 03-22 restart), consistent with post-restart replay, not a cause. The 03-23 18:00 Kafka retention change (cfg-2410) is a red herring: OOMs predate it and no throughput surge accompanied it. Recommended fix: roll back dep-5402 or add a bounded cache with eviction/size cap.

## Probable root cause
**Unbounded TemplateCache (dep-5402) causes gradual heap growth and OOM** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.82, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded TemplateCache (dep-5402) causes gradual heap growth and OOM | bad_deployment | notification-service | dep-5402 | 0.82 (high) | active |
| 2 | H2: Kafka maintenance/replay causes consumer to choke and OOM | config_change | kafka | cfg-2410 | 0.12 (low) | active |
| 3 | H3: Load/throughput growth (capacity saturation) | capacity_saturation | notification-service | - | 0.05 (low) | rejected |

### H1: Unbounded TemplateCache (dep-5402) causes gradual heap growth and OOM
dep-5402 (notification-service v3.8.0, 03-21) introduced an in-memory ConcurrentHashMap template cache with no eviction bound. Heap grows monotonically at render traffic until the container exceeds its memory limit and is OOMKilled (~daily), producing the repeated restarts and delayed emails. The post-restart consumer lag spike is the replay backlog draining, not the trigger.

**Causal chain**
1. dep-5402 deploys unbounded in-memory template cache on render path [F3]
2. Cache grows without bound (evictions=0, size 49.5k->196k) [F2]
3. Heap/memory_pct climbs monotonically ~47%->98% over ~22h [F1]
4. Container exceeds memory limit -> OOMKilled restart [F4, F5]
5. During restart, consumer group re-reads/replays -> lag spike and delayed emails [F8]

**Supporting evidence**
  - `F1` ✔ notification-service memory_pct shows a daily sawtooth: monotonic growth from ~47% to ~98% over ~22h followed by a reset to ~49%, repeating on 03-21, 03-22 and 03-23.
  - `F2` ✔ TemplateCache grows unbounded with zero evictions: size climbs 49500 -> 99000 -> 148500 (03-23) and reached 196650 entries with evictions=0 on 03-21/03-22.
  - `F3` ✔ Deployment dep-5402 (notification-service v3.8.0, deployed 2026-03-21T00:00:00Z) added an in-memory template cache 'TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment' on the render path; it is live and not rolled back.
  - `F4` ✔ notification-service experienced OOMKilled restarts: log shows 'container notification-service-7d9f OOMKilled (exit code 137), restarting' at 2026-03-23T19:59:21Z, preceded by heap-over-threshold/full GC warnings starting 2026-03-23T18:34:46Z.
  - `F5` ✔ Cumulative restart count for notification-service rose to 1 around 03-21 21:00-24:00, to 2 around 03-22 21:00, and to 3 around 03-23 18:00-21:00.
  - `F6` ✔ notification-service CPU is flat ~25% and latency_p99 is flat ~110ms throughout 03-23, with no surge coincident with the OOM.
  - `F11` ✔ The first OOM period (memory sawtooth restart) began on 03-21, before the 03-23 Kafka maintenance; GC/memory pressure warnings on 03-23 start at 18:34, i.e., only ~34 min after the 18:00 Kafka change but part of a pattern already present for two days.
**Contradicting evidence**
  - `F9` ✔ The only config change was cfg-2410 on service 'kafka', log.retention.ms reduced to 259200000 at 2026-03-23T18:00:00Z ('Kafka maintenance: reduce retention to 3 days'); no config change on notification-service.
**Assumptions**
  - The rendered-template cardinality (template+locale+userSegment) is effectively unbounded so the map never stops growing
  - The leaked TemplateCache lives in the same heap measured by memory_pct
  - Heap plateau at ~98% then reset corresponds to the OOM restart rather than a GC reclaim

### H2: Kafka maintenance/replay causes consumer to choke and OOM
The 03-23 18:00 Kafka retention change (cfg-2410) and/or broker maintenance triggered a consumer replay storm; the consumer re-fetched a large backlog, the in-memory processing buffer grew, and the container was OOMKilled at 19:59. This is the on-call's stated hypothesis.

**Causal chain**
1. cfg-2410 reduces Kafka retention at 18:00 on 03-23 [F9]
2. Replay backlog is re-consumed by notification-service [F8]
3. Buffer growth during replay drives OOM [F4]

**Supporting evidence**
  - `F8` ✔ consumer_lag for notification-service spiked to avg ~3000 / max ~18000 only in the 19:30-21:00 bucket on 03-23 (after the 19:59 restart), and similarly at the ~21:00 bucket on 03-22; baseline lag is ~50-70.
  - `F9` ✔ The only config change was cfg-2410 on service 'kafka', log.retention.ms reduced to 259200000 at 2026-03-23T18:00:00Z ('Kafka maintenance: reduce retention to 3 days'); no config change on notification-service.
  - `F12` ✔ Historical incidents with similar 'consumer lag spike / delayed emails' symptoms cite Kafka partition reassignment/rebalance (HIST-104, HIST-112) as root cause.
**Contradicting evidence**
  - `F1` ✔ notification-service memory_pct shows a daily sawtooth: monotonic growth from ~47% to ~98% over ~22h followed by a reset to ~49%, repeating on 03-21, 03-22 and 03-23.
  - `F5` ✔ Cumulative restart count for notification-service rose to 1 around 03-21 21:00-24:00, to 2 around 03-22 21:00, and to 3 around 03-23 18:00-21:00.
  - `F6` ✔ notification-service CPU is flat ~25% and latency_p99 is flat ~110ms throughout 03-23, with no surge coincident with the OOM.
  - `F7` ✔ Kafka is healthy on 03-23: cpu_pct flat ~35% and messages_in_per_s steady ~2300 with max 2532.9, no throughput surge.
  - `F11` ✔ The first OOM period (memory sawtooth restart) began on 03-21, before the 03-23 Kafka maintenance; GC/memory pressure warnings on 03-23 start at 18:34, i.e., only ~34 min after the 18:00 Kafka change but part of a pattern already present for two days.
**Assumptions**
  - Retention reduction would actually force a re-fetch (retention changes typically delete old segments, not force replay)
  - OOMs on 03-21 and 03-22 were caused by the same mechanism

### H3: Load/throughput growth (capacity saturation)
Notification render traffic outgrew the provisioned memory, causing OOM.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ notification-service CPU is flat ~25% and latency_p99 is flat ~110ms throughout 03-23, with no surge coincident with the OOM.
  - `F7` ✔ Kafka is healthy on 03-23: cpu_pct flat ~35% and messages_in_per_s steady ~2300 with max 2532.9, no throughput surge.
  - `F1` ✔ notification-service memory_pct shows a daily sawtooth: monotonic growth from ~47% to ~98% over ~22h followed by a reset to ~49%, repeating on 03-21, 03-22 and 03-23.
**Rejected because:** CPU flat ~25%, Kafka messages/s flat ~2300, latency flat — no load surge; memory grows as a bounded sawtooth independent of traffic.

## Facts (verified observations)
- notification-service memory follows a daily sawtooth rising from ~47% to ~98% then resetting to ~49% on each of 03-21, 03-22 and 03-23. [F1]
- TemplateCache grows unbounded with evictions=0, reaching 196,650 entries. [F2]
- dep-5402 (v3.8.0, 03-21T00:00Z) added the TemplateCache on the render path and remains live. [F3]
- An OOMKilled restart (exit 137) occurred at 2026-03-23T19:59:21Z after heap-over-threshold GC warnings from 18:34:46Z. [F4]
- Cumulative restarts rose 1 (03-21) -> 2 (03-22) -> 3 (03-23), one per day. [F5]
- CPU (~25%) and latency_p99 (~110ms) were flat on 03-23; Kafka CPU (~35%) and messages_in_per_s (~2300) were flat. [F6, F7]
- consumer_lag spiked to ~18k only after the 19:59 restart on 03-23 (and after the ~21:00 restart on 03-22), vs baseline ~50-70. [F8]
- The only config change was the 03-23 18:00 Kafka retention reduction (cfg-2410); nothing changed on notification-service. [F9]
- notification-service depends on Kafka (consume) and an external email-provider. [F10]
- The daily OOM pattern began 03-21, preceding the 03-23 Kafka maintenance by two days. [F11]

## Assumptions (not verified)
- The leaked TemplateCache resides in the same heap measured by memory_pct (no separate native/off-heap accounting).
- The memory plateau-then-reset at ~98% corresponds to the OOMKilled restart, and the daily reset period equals the observed restart cadence.
- Rendered-template key cardinality (template+locale+userSegment) is effectively unbounded so the cache never stops growing.

## Timeline
- `2026-03-21T00:00:00Z` dep-5402 v3.8.0 deployed to notification-service adding in-memory TemplateCache [F3]
- `2026-03-21T21:00:00Z` First OOM restart (restart count -> 1); memory had climbed to ~97% [F1, F5]
- `2026-03-22T21:00:00Z` Second OOM restart (restart count -> 2) with same memory sawtooth [F1, F5]
- `2026-03-23T18:00:00Z` Kafka retention reduced (cfg-2410) as maintenance [F9]
- `2026-03-23T18:34:46Z` Heap-over-90% old-gen GC warnings begin on notification-service [F4]
- `2026-03-23T19:59:21Z` Third OOMKilled restart (exit 137); restart count -> 3 [F4, F5]
- `2026-03-23T20:00:00Z` consumer_lag spikes to ~18k as post-restart backlog replays; emails delayed ~25 min [F8]

## Recommended actions
- **[immediate]** Roll back / disable dep-5402 (TemplateCache) on notification-service, or set a bounded cache (max size + eviction, e.g. Caffeine with maximumSize/TTL). -- Cache is the leak source: unbounded growth with evictions=0 matching the daily OOM cadence. [F2, F3]
- **[short_term]** Raise the container memory limit only as a temporary buffer and/or alert on memory_pct slope, not just threshold. -- Buys time while the leak fix ships; a slope alert catches the ramp before OOM. [F1]
- **[short_term]** Do NOT act on the Kafka maintenance as the cause; keep a post-restart replay drain watch instead. -- Lag spike is a downstream consequence of the restart, and OOMs predate the config change. [F8, F11, F9]
- **[long_term]** Add a cache-size/eviction metric and a unit test asserting the cache is bounded under high-cardinality keys. -- Prevents recurrence of unbounded in-memory caches on the render path. [F2]

## Data gaps
- No heap dump / per-component memory breakdown to prove TemplateCache is the exact allocation dominating the heap (inferred from size growth + evictions=0 + timing).
- Kafka consumer_lag metric is not exposed on the kafka service itself (only cpu_pct, messages_in_per_s, under_replicated_partitions), so partition-reassignment activity cannot be directly confirmed or excluded.
- email-provider has no telemetry, so its contribution to delayed emails cannot be measured.
- No data before 2026-03-21, so it is not directly verifiable that no OOM occurred prior to dep-5402.

## Investigation stats
- latency 41.29s, LLM calls 5, tokens 53217 (in 46201 / out 7016)
- tool calls 16 (1 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
