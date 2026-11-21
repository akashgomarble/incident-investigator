# Incident investigation report: INC-004
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
notification-service is OOMKilling roughly every 24h due to a memory leak introduced by deployment dep-5402 (v3.8.0, 2026-03-21T00:00Z) which added an unbounded in-memory template cache (ConcurrentHashMap keyed by template+locale+userSegment). Memory was flat at ~48% before the deploy, then climbed monotonically to ~98% and reset via OOMKill at 03-21T22:59, 03-22T21:59 and 03-23T19:59 (restarts counter 0->1->2->3), accompanied by old-gen full-GC thrash warnings. Consumer-lag spikes to thousands are a consequence of the consumer being down during each OOM restart, which is what delayed trade-confirmation emails. The reported Kafka maintenance (retention reduction at 03-23T18:00) is coincidental: two of the three OOMs predate it, so Kafka is not the origin. Recommended fix: roll back dep-5402 or bound/evict the template cache (size/TTL), then replay the lagged messages.

## Probable root cause
**Unbounded in-memory template cache (dep-5402) leaks heap -> 24h OOMKill cycle -> consumer lag/email delay** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded in-memory template cache (dep-5402) leaks heap -> 24h OOMKill cycle -> consumer lag/email delay | bad_deployment | notification-service | dep-5402 | 0.85 (high) | active |
| 2 | H2: Kafka maintenance / replays cause the consumer to choke and OOM | infrastructure_failure | kafka | cfg-2410 | 0.05 (low) | rejected |
| 3 | H3: Capacity saturation: trading load outgrew provisioned memory | capacity_saturation | notification-service | - | 0.03 (low) | rejected |

### H1: Unbounded in-memory template cache (dep-5402) leaks heap -> 24h OOMKill cycle -> consumer lag/email delay
notification-service v3.8.0 (dep-5402, deployed 2026-03-21T00:00Z) added a ConcurrentHashMap template cache keyed by template+locale+userSegment with no bound/eviction. Entries accumulate over ~24h, old-gen fills to ~98%, GC goes into full-GC thrash and the container is OOMKilled (exit 137); while the consumer is down its Kafka consumer lag spikes and trade-confirmation emails are delayed. The Kafka retention change is incidental.

**Causal chain**
1. dep-5402 introduces unbounded TemplateCache (ConcurrentHashMap) [F1]
2. cache entries grow monotonically, memory_pct climbs from ~48% to ~98% over ~22h [F2, F3, F12]
3. old-gen full-GC thrash then OOMKill (exit 137), restart resets memory to ~48% [F4, F5, F9]
4. consumer downtime during OOM restart spikes consumer lag and delays emails [F6, F11]
5. cycle repeats every ~24h producing the third OOM [F4, F5]

**Supporting evidence**
  - `F1` ✔ notification-service v3.8.0 (dep-5402) was deployed 2026-03-21T00:00:00Z adding an in-memory template cache: 'Add in-memory template cache for email rendering' with 'TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment'.
  - `F2` ✔ Before the deploy notification-service memory_pct was flat at ~48% (03-20 all buckets avg 47.5-48.9); immediately after the 03-21T00:00 deploy it begins a monotonic climb ~4.5pp per 2h bucket (48.23 at 00:00 -> 92.91 avg, 95.09 max at 20:00).
  - `F3` ✔ memory_pct shows a repeating sawtooth: ramp to ~94-98% then reset to ~48%. Resets occur at 03-21T22:00 (avg 71.52 after max 97.37), 03-22T22:00 (avg 48.25 after max 97.98), 03-23T20:00 (avg 48.33 after max 98.44).
  - `F4` ✔ Three OOMKilled events (exit code 137) occurred, one per ~24h: 2026-03-21T22:59:50, 2026-03-22T21:59:47, 2026-03-23T19:59:21.
  - `F5` ✔ The cumulative restarts counter goes 0 (through 03-21T20:00) then 1 (03-21T22:00), 2 (03-22T22:00), 3 (03-23T20:00), matching the three OOM events.
  - `F6` ✔ consumer_lag is ~55-65 messages at baseline but spikes to thousands exactly in the same 2h buckets as the OOM restarts: 2273.11 (03-21T22:00, max 17161.92), 790.21 (03-22T20:00, max 17611.66), 810.33 (03-23T18:00, max 18137.93), returning to ~57 in the next idle bucket.
  - `F7` ✔ cpu_pct for notification-service is flat at ~25% (mean 25.02, max 28.01) across 03-21T22:00 through 03-23T23:59, i.e. no CPU saturation accompanying the memory growth.
  - `F9` ✔ The WARN log pattern 'heap usage <n>% over 90 (old gen), full GC took <n>ms' repeatedly precedes each OOM (first seen 2026-03-21T21:37:01, and 18:34:46-19:49:28 on 03-23), with full GC times growing to 6-8s.
  - `F11` ✔ Inference: the lag spikes are a consequence of the OOM restarts (consumer stops consuming during/after the crash) rather than their cause, because lag rises only in the same buckets as the restarts and the restarts at 03-21T22:59 and 03-22T21:59 predate the 03-23T18:00 Kafka change.
  - `F12` ✔ Inference: the flat ~48% memory before 03-21T00:00 and the monotonic climb after it, resetting only on OOMKill, is the signature of an unbounded memory leak introduced at 03-21T00:00 (matching dep-5402's new in-memory template cache); the ~24h fill-to-OOM periodicity is consistent with steady allocation into a cache that is never bounded or evicted.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The reported 'third OOM this week' corresponds to the three events at 03-21T22:59, 03-22T21:59 and 03-23T19:59 (no earlier, pre-deploy OOMs exist).
  - The template cache keys grow with distinct template/locale/userSegment combinations so the map is effectively unbounded.
  - No eviction/TTL exists in the cache code (not directly observable from provided data).

### H2: Kafka maintenance / replays cause the consumer to choke and OOM
The Kafka retention change (cfg-2410, 03-23T18:00Z) / broker maintenance triggered a consumer replay whose backlog consumed heap, causing the OOM restart and lag spike.

**Causal chain**
(none given)

**Supporting evidence**
  - `F6` ✔ consumer_lag is ~55-65 messages at baseline but spikes to thousands exactly in the same 2h buckets as the OOM restarts: 2273.11 (03-21T22:00, max 17161.92), 790.21 (03-22T20:00, max 17611.66), 810.33 (03-23T18:00, max 18137.93), returning to ~57 in the next idle bucket.
  - `F8` ✔ The only config change in the window is cfg-2410 on kafka: log.retention.ms reduced from 604800000 to 259200000 at 2026-03-23T18:00:00Z ('Kafka maintenance: reduce retention to 3 days').
  - `F10` ✔ Past post-mortems HIST-104 (2025-11-05, notification-service 'consumer lag spike','delayed emails' after Kafka broker rebalance) and HIST-112 (2026-02-03, settlement consumer stalled after partition reassignment) show lag-after-Kafka-shaped symptoms, but both are different services/dates and are leads only.
**Contradicting evidence**
  - `F2` ✔ Before the deploy notification-service memory_pct was flat at ~48% (03-20 all buckets avg 47.5-48.9); immediately after the 03-21T00:00 deploy it begins a monotonic climb ~4.5pp per 2h bucket (48.23 at 00:00 -> 92.91 avg, 95.09 max at 20:00).
  - `F3` ✔ memory_pct shows a repeating sawtooth: ramp to ~94-98% then reset to ~48%. Resets occur at 03-21T22:00 (avg 71.52 after max 97.37), 03-22T22:00 (avg 48.25 after max 97.98), 03-23T20:00 (avg 48.33 after max 98.44).
  - `F4` ✔ Three OOMKilled events (exit code 137) occurred, one per ~24h: 2026-03-21T22:59:50, 2026-03-22T21:59:47, 2026-03-23T19:59:21.
  - `F5` ✔ The cumulative restarts counter goes 0 (through 03-21T20:00) then 1 (03-21T22:00), 2 (03-22T22:00), 3 (03-23T20:00), matching the three OOM events.
  - `F7` ✔ cpu_pct for notification-service is flat at ~25% (mean 25.02, max 28.01) across 03-21T22:00 through 03-23T23:59, i.e. no CPU saturation accompanying the memory growth.
**Rejected because:** OOM restarts #1 (03-21T22:59) and #2 (03-22T21:59) occurred BEFORE the 03-23T18:00 Kafka change, and memory was already ramping from 03-21T00:00 (deploy time). Lag spikes align with the restarts (consequence, not cause), CPU is flat and no broker error/telemetry anomaly was found in kafka logs. Timing disproves Kafka as origin.

### H3: Capacity saturation: trading load outgrew provisioned memory
Steadily rising traffic/message volume simply exceeded the provisioned heap, causing OOM without any faulty change.

**Causal chain**
(none given)

**Supporting evidence**
  - `F2` ✔ Before the deploy notification-service memory_pct was flat at ~48% (03-20 all buckets avg 47.5-48.9); immediately after the 03-21T00:00 deploy it begins a monotonic climb ~4.5pp per 2h bucket (48.23 at 00:00 -> 92.91 avg, 95.09 max at 20:00).
**Contradicting evidence**
  - `F2` ✔ Before the deploy notification-service memory_pct was flat at ~48% (03-20 all buckets avg 47.5-48.9); immediately after the 03-21T00:00 deploy it begins a monotonic climb ~4.5pp per 2h bucket (48.23 at 00:00 -> 92.91 avg, 95.09 max at 20:00).
  - `F3` ✔ memory_pct shows a repeating sawtooth: ramp to ~94-98% then reset to ~48%. Resets occur at 03-21T22:00 (avg 71.52 after max 97.37), 03-22T22:00 (avg 48.25 after max 97.98), 03-23T20:00 (avg 48.33 after max 98.44).
  - `F4` ✔ Three OOMKilled events (exit code 137) occurred, one per ~24h: 2026-03-21T22:59:50, 2026-03-22T21:59:47, 2026-03-23T19:59:21.
  - `F12` ✔ Inference: the flat ~48% memory before 03-21T00:00 and the monotonic climb after it, resetting only on OOMKill, is the signature of an unbounded memory leak introduced at 03-21T00:00 (matching dep-5402's new in-memory template cache); the ~24h fill-to-OOM periodicity is consistent with steady allocation into a cache that is never bounded or evicted.
**Rejected because:** Memory was perfectly flat at ~48% for the entire pre-deploy window and began climbing at the exact deploy timestamp; a pure capacity issue would not produce a sudden post-deploy regression nor a clean resettable 24h sawtooth.

## Facts (verified observations)
- notification-service v3.8.0 dep-5402 (2026-03-21T00:00Z) added an in-memory template cache keyed by template+locale+userSegment. [F1]
- Memory was flat ~48% before the deploy and began a monotonic climb immediately after it, reaching ~92-95% by 03-21T20:00. [F2]
- memory_pct follows a ~24h sawtooth resetting to ~48% at each OOM. [F3]
- Three OOMKilled (exit 137) events occurred, ~24h apart: 03-21T22:59:50, 03-22T21:59:47, 03-23T19:59:21. [F4]
- Cumulative restarts counter rose 0 -> 1 -> 2 -> 3 matching the three OOM events. [F5]
- consumer_lag spikes only in the same buckets as the OOM restarts (2273/790/810 vs baseline ~57). [F6]
- cpu_pct stayed flat ~25%, ruling out CPU saturation. [F7]
- The only config change was kafka cfg-2410 (retention 7d->3d) at 03-23T18:00Z, after two of the three OOMs. [F8]
- Heap-over-90% / long full-GC warnings precede each OOM. [F9]
- Past incidents HIST-104/HIST-112 are Kafka-lag-shaped but for different services and dates (leads only). [F10]

## Assumptions (not verified)
- The three observed OOM events correspond to the reported 'third OOM this week' (no pre-deploy OOMs).
- The TemplateCache has no size bound or TTL/eviction (code not directly observable).
- Cache keys are diverse enough that the map grows unbounded over a day.

## Timeline
- `2026-03-20T00:00:00Z` Baseline: memory_pct flat ~48%, restarts=0, consumer_lag baseline ~57. [F2, F5, F6]
- `2026-03-21T00:00:00Z` dep-5402 deployed: notification-service v3.8.0 adds in-memory template cache. [F1]
- `2026-03-21T02:00:00Z` memory_pct begins steady climb (52.71 -> ...). [F2]
- `2026-03-21T21:37:01Z` First 'heap usage N% over 90 (old gen)' full-GC warnings appear. [F9]
- `2026-03-21T22:59:50Z` OOMKill #1 (exit 137); memory resets to ~48%; consumer_lag spikes to max 17161. [F4, F3, F6]
- `2026-03-22T21:59:47Z` OOMKill #2; memory resets; restarts counter -> 2. [F4, F5]
- `2026-03-23T18:00:00Z` Kafka cfg-2410 retention 7d->3d (incidental to symptom onset). [F8]
- `2026-03-23T19:59:21Z` OOMKill #3; restarts counter -> 3; lag spike; emails delayed ~25 min. [F4, F5, F6]

## Recommended actions
- **[immediate]** Roll back dep-5402 (notification-service v3.8.0) to the last version without the template cache, or immediately disable the TemplateCache via feature flag. -- Memory growth began exactly at the deploy and resets only on OOMKill; rollback removes the leak source. [F1, F2, F4]
- **[short_term]** Bound the template cache (max entries + TTL/eviction) or move it to an external bounded cache (e.g. Redis) before re-enabling. -- The ConcurrentHashMap keyed by template+locale+userSegment is effectively unbounded and fills heap over ~24h. [F1, F12]
- **[short_term]** After restoring memory stability, allow the consumer to drain and replay the lagged trade-confirmation messages. -- consumer_lag spiked to thousands and emails were delayed; the backlog must be cleared. [F6]
- **[long_term]** Add heap-usage alerting (old-gen %, full-GC time) with a forecast-to-OOM warning instead of waiting for the Killed event. -- The heap-over-90%/long-GC warnings preceded each OOM by hours and were not acted on early. [F9]

## Data gaps
- No heap-used / old-gen byte-level metric is exposed (heap_used_mb not_found); growth is inferred from memory_pct only.
- No notification-service code/diff or line-level detail of the TemplateCache bounds to confirm absence of eviction.
- No request_rate / template-cardinality series to quantify cache key growth.
- No kafka broker-side logs beyond a TLS-cipher warning, so broker telemetry during the OOM windows is not directly observable.
- Whether any pre-2026-03-21 OOMs existed is not covered by the queried window (restarts counter starts at 0 on 03-20).

## Investigation stats
- latency 45.85s, LLM calls 5, tokens 54062 (in 46630 / out 7432)
- tool calls 15 (1 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
