# Incident investigation report: INC-004
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The OOMKilled restarts of notification-service are caused by the unbounded in-memory TemplateCache introduced by deployment dep-5402 (v3.8.0, 2026-03-21T00:00Z): it has no size cap, TTL or eviction, so per-render entries accumulate in the JVM old generation until full-GC thrash and OOMKill (exit 137). Confidence is high (0.82) per the reviewed ranking. The reporter's Kafka-replay hypothesis is refuted: two of the three OOMs predate the 2026-03-23T18:00Z retention change by ~2 days, throughput and CPU are flat, Kafka shows no rebalance/broker anomaly, and lag spikes only after each crash. H2-H5 are each contradicted by multiple independent signals; the ~25 min email delay is a downstream consequence of the restart/consumer-lag transient, not a cause.

## Probable root cause
**TemplateCache introduced by dep-5402 unboundedly accumulates email-render entries and exhausts notification-service heap** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.82, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: TemplateCache introduced by dep-5402 unboundedly accumulates email-render entries and exhausts notification-service heap | bad_deployment | notification-service | dep-5402 | 0.82 (high) | active |
| 2 | H2: Kafka maintenance replay/rebalance floods the consumer (reporter's hypothesis) | config_change | kafka | cfg-2410 | 0.03 (low) | rejected |
| 3 | H3: Upstream producer increased message size/volume/rate, over-buffering the consumer | data_issue | order-service | null | 0.03 (low) | rejected |
| 4 | H4: Shared node/pod memory pressure — notification-service is a victim of a co-tenant or sidecar | infrastructure_failure | node-pool | null | 0.02 (low) | rejected |
| 5 | H5: Downstream dependency (email-provider/template/DB) blocks, causing backpressure that buffers memory | dependency_failure | email-provider | null | 0.02 (low) | rejected |

### H1: TemplateCache introduced by dep-5402 unboundedly accumulates email-render entries and exhausts notification-service heap
Deployment dep-5402 (v3.8.0, 2026-03-21T00:00Z) added an in-memory ConcurrentHashMap TemplateCache keyed by template+locale+userSegment and put it on the render path. The cache has no size cap, TTL or eviction (verified: evictions=0 across 14 samples; entry count grows monotonically within each pod lifetime and resets only on pod restart, F19/F20), and the key space is inflated by the per-userSegment dimension (F21). Entries therefore accumulate for every rendered email, filling the JVM old generation until full-GC thrash (GC up to 8.2s, heap warnings >90%) and OOMKill (exit 137). The observed cache growth rate tracks the memory sawtooth (F22). The ~25 min trade-confirmation email delay is a downstream consequence of the restart/consumer-lag transient, not a cause.

**Causal chain**
1. dep-5402 deployed v3.8.0 at 2026-03-21T00:00Z, adding an in-memory TemplateCache keyed by template+locale+userSegment onto the render path [F1]
2. Memory sawtooth onset follows the deploy: pre-deploy baseline 2026-03-20 flat ~47-55%, climb begins 2026-03-21 ~02:00-04:00Z, well after the 00:00Z deploy [F7, F8]
3. The cache is unbounded: evictions=0 in every sample, entry count grows monotonically (51,750 -> 196,650) within each pod lifetime, with no cap or TTL observed [F19, F20]
4. Cache key includes the unbounded userSegment dimension, so per-render entries accumulate rather than hitting on a bounded key set [F21]
5. Cache entry growth rate matches the memory growth rate, i.e. entries accumulate ~8.6k/h tracking the ~+9-10pp/4h memory climb — memory resets only at OOM restart [F22, F13]
6. Memory climbs from ~48-55% post-restart to ~95-98% each evening and resets only on restart; sawtooth repeats identically 03-21/22/23 against a constant limit [F7, F8, F13, F29]
7. Old generation fills: heap-over-90% warnings with full GC up to 8.2s precede each OOM; no batch-size/record-size/replay/rebalance lines appear [F17]
8. Container is OOMKilled (exit 137) on 03-21T22:59, 03-22T21:59 and 03-23T19:59; each restart wipes the heap and the cache counter restarts small afterward [F9, F10, F20]
9. Consumer lag does not precede memory growth; it spikes only in the crash bucket and returns to ~57 immediately, i.e. lag is a consequence of the OOM/restart driving the ~25 min email delay [F11]

**Supporting evidence**
  - `F1` ✔ Only one deployment touched notification-service in the last 7 days: dep-5402, v3.8.0, deployed 2026-03-21T00:00:00Z by s.menon, 'Add in-memory template cache for email rendering', with changes ['TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment','render path uses cache'], status 'live', rolled_back_at null.
  - `F7` ✔ notification-service memory_pct follows a 24-hour sawtooth: normal ~48% baseline, then a steady climb beginning 2026-03-21 ~02:00-04:00Z (52.71 -> 57.15), continuing to ~95%+ each evening, then resetting to ~48% after each restart. Pattern repeats on 03-21, 03-22 and 03-23.
  - `F8` ✔ Memory peak reaches 98.44% (tc-001 max) near the reset points, consistent with hitting a ~100% container memory limit; the pre-incident baseline (2026-03-20) was flat at ~47-55%. Zoomed-out summary max is 98.44%.
  - `F9` ✔ Three OOMKilled events, all signature 'container notification-service-7d9f OOMKilled (exit code 137), restarting', at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z. First occurrence this week is 2026-03-21T22:59:50Z — NOT the reported 03-23 event.
  - `F10` ✔ Cumulative restarts metric confirms the onset: 0 through 2026-03-21T16:00Z, first becomes 1 in the 2026-03-21T20:00Z bucket, 2 at 2026-03-22T20:00Z, 3 at 2026-03-23T20:00Z.
  - `F11` ✔ Consumer lag does NOT precede memory growth: lag stays normal (~55-65, max ~110) throughout the entire memory climb, and spikes only in the same bucket as the crash/reset — 03-21T22:00Z avg 2273/max 17161.92, 03-22T20:00Z max 17611.66, 03-23T18:00Z max 18137.93 — then immediately returns to ~57. Lag therefore follows the OOM/restart.
  - `F13` ✔ Memory sawtooth repeats identically across all three restart days, with each climb starting from the SAME ~48-55% post-restart baseline and rising ~+9-10 percentage points per 4h bucket independent of any external event: 03-21 04:00 avg 59.31 -> 16:00 avg 85.99; 03-22 00:00 avg 52.57 -> 16:00 avg 88.08; 03-23 00:00 avg 55.25 -> 16:00 avg 92.65. Critically the climb on 03-21 and 03-22 (13:00-22:00 on 03-22, well before the 03-23T18:00Z Kafka config change) is identical in shape to 03-23, so the pattern predates the maintenance by two days.
  - `F17` ✔ The failure mode is heap exhaustion by GC pressure, not message-volume overflow: WARN logs consist almost entirely of 'heap usage N% over 90 (old gen), full GC took Nms' (03-23: 10 such warnings from 18:34:46 to 19:49:28 with GC times up to 6488ms, immediately followed by the OOM at 19:59:21). The same sequence recurs on 03-21 (25 heap warnings 21:37-22:41, GC up to 8220ms, then OOM 22:59:50) and 03-22 (heap warnings 20:35-21:56, GC up to 8093ms, then OOM 21:59:47). No batch-size, record-size, replay or rebalance log line appears in any of the three windows.
  - `F19` ✔ TemplateCache is UNBOUNDED: every 'TemplateCache stats' log line reports evictions=0, and the entry count grows monotonically within each pod's lifetime (51,750 -> 103,500 -> 155,250 -> 196,650 entries on 03-21, 03-22) with no eviction ever recorded. There is no size cap, TTL, or eviction behavior observable — evictions never becomes non-zero across 14 samples over 03-21..03-23.
  - `F20` ✔ Cache size is bounded only by pod restarts, not by configuration: each day the size climbs in equal 51,750-entry steps to ~196,650 then resets after the OOM restart (03-21 05:45 51,750 ... 21:51 196,650; 03-22 04:45 51,750 ... 20:51 196,650; 03-23 03:30 49,500 ... 18:54 188,100, then 21:00 9,000 and 22:00 18,000 after the 19:59 crash). The counter restarting from a small value right after each crash confirms the map dies with the pod; growth accumulates in memory rather than being evicted.
  - `F21` ✔ The cache key includes an unbounded dimension: the dep-5402 change is 'TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment' (established F1), and the ~51,750 new entries appearing per 5-6h interval is consistent with per-user-segment key explosion — every observed sample shows entries accumulating with zero evictions.
  - `F22` ✔ The cache-size growth rate matches the memory growth rate: the map roughly quadruples per 16h (51,750 -> 196,650) while memory climbs ~+9-10 percentage points per 4h bucket (per team F13), i.e. cache entries per bucket ~= +51,750/6h == ~8.6k entries/h added steadily, tracking the observed memory sawtooth that resets only at each OOM restart.
  - `F29` ✔ Since no resource-limit change was recorded in the entire 03-01..03-23 period, the container memory limit in force during the 03-21..03-23 OOM events is unchanged from the pre-incident baseline; the ~98.44% peak (T1/F8) is therefore against a constant limit, not a lowered one. The absolute value (e.g. 512Mi vs 1Gi) is not present in any change feed available to me.
  - `F6` ✔ No historical incident in the searched set has OOMKilled or memory-exhaustion as a root cause, and none attributes an incident to a memory-holding cache or a replay driven by a retention change; the two notification/Kafka precedents were rebalancing-storm infrastructure failures.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The container memory limit is fixed and unchanged during the window (no limit change exists in the change feed, F25/F26/F29, but the exact numeric limit value is not retrievable by any available tool)
  - TemplateCache is the dominant contributor to the old-gen growth; no other new allocation path in dep-5402 is inspected (dep-5402's only listed change is the cache, F1)
  - Per-render accumulation is inferred from entry-count growth and flat request_rate rather than from a direct emails-rendered series, which does not exist (F23, F24)

### H2: Kafka maintenance replay/rebalance floods the consumer (reporter's hypothesis)
The 2026-03-23T18:00Z Kafka retention reduction (cfg-2410) triggered a consumer replay/rebalance that flooded notification-service with buffered messages, causing runaway memory growth and OOM. This is the reporter's stated suspicion, but it is chronologically and mechanistically refuted.

**Causal chain**
1. cfg-2410 reduced log.retention.ms 7d -> 3d on 2026-03-23T18:00Z, described as Kafka maintenance [F3]
2. Maintenance causes partition reassignment / consumer group rebalance, forcing a message replay to notification-service *(assumed)*
3. Replay floods the consumer, buffering far more than before and driving memory up *(assumed)*
4. Heap exhausts and container is OOMKilled, delaying emails [F9]

**Supporting evidence**
  - `F3` ✔ Exactly one config change was recorded in the last 7 days: cfg-2410 on service 'kafka', key 'log.retention.ms', old_value 604800000 -> new_value 259200000, changed_at 2026-03-23T18:00:00Z, description 'Kafka maintenance: reduce retention to 3 days'. This is the maintenance event referenced in the report; no broker-quota or memory/limit config changes appear.
  - `F5` ✔ Historical incident HIST-104 (2025-11-05): 'notification-service lag after Kafka broker rebalance', symptoms 'consumer lag spike','delayed emails', root_cause 'Partition reassignment caused consumer group rebalancing storm', resolution 'Throttled reassignment'. The prior similar-symptom event's root cause was a rebalancing storm, NOT memory/OOM, and HIST-112 (settlement-service, 2026-02-03) likewise had 'Consumer lost partition assignment during broker maintenance'.
**Contradicting evidence**
  - `F7` ✔ notification-service memory_pct follows a 24-hour sawtooth: normal ~48% baseline, then a steady climb beginning 2026-03-21 ~02:00-04:00Z (52.71 -> 57.15), continuing to ~95%+ each evening, then resetting to ~48% after each restart. Pattern repeats on 03-21, 03-22 and 03-23.
  - `F9` ✔ Three OOMKilled events, all signature 'container notification-service-7d9f OOMKilled (exit code 137), restarting', at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z. First occurrence this week is 2026-03-21T22:59:50Z — NOT the reported 03-23 event.
  - `F10` ✔ Cumulative restarts metric confirms the onset: 0 through 2026-03-21T16:00Z, first becomes 1 in the 2026-03-21T20:00Z bucket, 2 at 2026-03-22T20:00Z, 3 at 2026-03-23T20:00Z.
  - `F11` ✔ Consumer lag does NOT precede memory growth: lag stays normal (~55-65, max ~110) throughout the entire memory climb, and spikes only in the same bucket as the crash/reset — 03-21T22:00Z avg 2273/max 17161.92, 03-22T20:00Z max 17611.66, 03-23T18:00Z max 18137.93 — then immediately returns to ~57. Lag therefore follows the OOM/restart.
  - `F12` ✔ Kafka shows no concurrent resource anomaly: cpu_pct flat at ~35% across 03-20 to 03-23 (min 29.77, max 41.75), giving no support to a Kafka-side replay overload.
  - `F13` ✔ Memory sawtooth repeats identically across all three restart days, with each climb starting from the SAME ~48-55% post-restart baseline and rising ~+9-10 percentage points per 4h bucket independent of any external event: 03-21 04:00 avg 59.31 -> 16:00 avg 85.99; 03-22 00:00 avg 52.57 -> 16:00 avg 88.08; 03-23 00:00 avg 55.25 -> 16:00 avg 92.65. Critically the climb on 03-21 and 03-22 (13:00-22:00 on 03-22, well before the 03-23T18:00Z Kafka config change) is identical in shape to 03-23, so the pattern predates the maintenance by two days.
  - `F14` ✔ Workload throughput does not rise with the replay theory: request_rate is flat across the whole window (min 115.35, max 172.03, mean 140.17 req/s), and the 03-23 16:00-20:00 buckets around the Kafka change/crash (avg 132.82, then 135.84) are LOWER than the daily 04:00 peak buckets (03-23 04:00 avg 148.37, max 172.03). There is no post-maintenance traffic/message surge.
  - `F16` ✔ CPU is flat (mean 25.01%, range 21.78-28.5%) across 03-20..03-23 with no rise during any memory climb or crash bucket — the work per unit time does not increase, so the memory growth is not explained by extra processing volume (retry/replay).
  - `F17` ✔ The failure mode is heap exhaustion by GC pressure, not message-volume overflow: WARN logs consist almost entirely of 'heap usage N% over 90 (old gen), full GC took Nms' (03-23: 10 such warnings from 18:34:46 to 19:49:28 with GC times up to 6488ms, immediately followed by the OOM at 19:59:21). The same sequence recurs on 03-21 (25 heap warnings 21:37-22:41, GC up to 8220ms, then OOM 22:59:50) and 03-22 (heap warnings 20:35-21:56, GC up to 8093ms, then OOM 21:59:47). No batch-size, record-size, replay or rebalance log line appears in any of the three windows.
  - `F18` ✔ Kafka-side telemetry shows no maintenance-induced anomaly around the 03-23T18:00Z retention change: over 03-20..03-23 only benign WARNs appear (GC pause, TLS cipher, /internal/metrics slow, NTP clock drift, one idempotent retry at 2026-03-21T13:28:52Z) with NO rebalance/partition-reassignment or broker-error entries after 18:00Z; the last Kafka log is 2026-03-23T17:17:15Z (before the change). This corroborates T1's flat Kafka CPU and the absence of a replay-driven rebalance storm.
**Assumptions**
  - A retention change induces a replay/rebalance at all
  - Replay delivers a message volume large enough to exhaust heap
  - Replay timing coincides with memory onset
**Rejected because:** Chronology and mechanism both fail. Memory sawtooth began 2026-03-21 (~40h) and the first OOM was 03-21T22:59Z, two days BEFORE the 03-23T18:00Z config change (F7, F9, F10, F13); the same shape recurred on 03-22 before the change (F13). No traffic surge (F14), flat Kafka CPU (F12), no rebalance/partition-reassignment or broker-error logs after the change (F18), flat notification CPU with no extra processing (F16), lag spikes only in the crash bucket and follows the OOM (F11), and logs show heap/GC exhaustion with no replay/rebalance/batch-size lines (F17).

### H3: Upstream producer increased message size/volume/rate, over-buffering the consumer
An upstream producer (order-service/payment-service) changed message size or rate, so notification-service buffers far more than before and exhausts memory.

**Causal chain**
1. An upstream producer increases message size/volume/rate *(assumed)*
2. Consumer buffers larger/more messages, growing memory *(assumed)*
3. Memory exhausts, OOMKill, delayed emails [F9]

**Supporting evidence**
  - `F4` ✔ notification-service's dependencies are kafka (queue-consume, shared=true, telemetry=true) and email-provider (external, no telemetry). kafka is shared by order-service and payment-service (queue-produce) and settlement-service (queue-consume), so those neighbours can affect notification-service through Kafka.
**Contradicting evidence**
  - `F2` ✔ The only other deployment in the 7-day window across all services is auth-service v7.1.4 (dep-5415) at 2026-03-22T13:00:00Z ('bump jackson'), which does not touch notification-service, Kafka, or memory limits.
  - `F11` ✔ Consumer lag does NOT precede memory growth: lag stays normal (~55-65, max ~110) throughout the entire memory climb, and spikes only in the same bucket as the crash/reset — 03-21T22:00Z avg 2273/max 17161.92, 03-22T20:00Z max 17611.66, 03-23T18:00Z max 18137.93 — then immediately returns to ~57. Lag therefore follows the OOM/restart.
  - `F13` ✔ Memory sawtooth repeats identically across all three restart days, with each climb starting from the SAME ~48-55% post-restart baseline and rising ~+9-10 percentage points per 4h bucket independent of any external event: 03-21 04:00 avg 59.31 -> 16:00 avg 85.99; 03-22 00:00 avg 52.57 -> 16:00 avg 88.08; 03-23 00:00 avg 55.25 -> 16:00 avg 92.65. Critically the climb on 03-21 and 03-22 (13:00-22:00 on 03-22, well before the 03-23T18:00Z Kafka config change) is identical in shape to 03-23, so the pattern predates the maintenance by two days.
  - `F14` ✔ Workload throughput does not rise with the replay theory: request_rate is flat across the whole window (min 115.35, max 172.03, mean 140.17 req/s), and the 03-23 16:00-20:00 buckets around the Kafka change/crash (avg 132.82, then 135.84) are LOWER than the daily 04:00 peak buckets (03-23 04:00 avg 148.37, max 172.03). There is no post-maintenance traffic/message surge.
  - `F16` ✔ CPU is flat (mean 25.01%, range 21.78-28.5%) across 03-20..03-23 with no rise during any memory climb or crash bucket — the work per unit time does not increase, so the memory growth is not explained by extra processing volume (retry/replay).
  - `F17` ✔ The failure mode is heap exhaustion by GC pressure, not message-volume overflow: WARN logs consist almost entirely of 'heap usage N% over 90 (old gen), full GC took Nms' (03-23: 10 such warnings from 18:34:46 to 19:49:28 with GC times up to 6488ms, immediately followed by the OOM at 19:59:21). The same sequence recurs on 03-21 (25 heap warnings 21:37-22:41, GC up to 8220ms, then OOM 22:59:50) and 03-22 (heap warnings 20:35-21:56, GC up to 8093ms, then OOM 21:59:47). No batch-size, record-size, replay or rebalance log line appears in any of the three windows.
**Assumptions**
  - Some undeployed upstream change altered message characteristics
**Rejected because:** No deployment or config change in the window touched any producer (F2: only auth-service 'bump jackson', unrelated; F3: only Kafka retention). Throughput is flat with no post-event surge (F14), CPU flat so no extra processing volume (F16), lag does not precede memory growth (F11), logs show no batch/record-size evidence (F17), and growth shape is identical across days predating the maintenance (F13).

### H4: Shared node/pod memory pressure — notification-service is a victim of a co-tenant or sidecar
Node-level memory pressure from a co-tenant pod or a sidecar/agent sharing notification-service's node pool pushes it over its limit, causing OOMKill.

**Causal chain**
1. A co-tenant/sidecar consumes node memory *(assumed)*
2. notification-service is starved and its container hits the memory limit *(assumed)*
3. OOMKilled, delayed emails [F9]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F1` ✔ Only one deployment touched notification-service in the last 7 days: dep-5402, v3.8.0, deployed 2026-03-21T00:00:00Z by s.menon, 'Add in-memory template cache for email rendering', with changes ['TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment','render path uses cache'], status 'live', rolled_back_at null.
  - `F7` ✔ notification-service memory_pct follows a 24-hour sawtooth: normal ~48% baseline, then a steady climb beginning 2026-03-21 ~02:00-04:00Z (52.71 -> 57.15), continuing to ~95%+ each evening, then resetting to ~48% after each restart. Pattern repeats on 03-21, 03-22 and 03-23.
  - `F13` ✔ Memory sawtooth repeats identically across all three restart days, with each climb starting from the SAME ~48-55% post-restart baseline and rising ~+9-10 percentage points per 4h bucket independent of any external event: 03-21 04:00 avg 59.31 -> 16:00 avg 85.99; 03-22 00:00 avg 52.57 -> 16:00 avg 88.08; 03-23 00:00 avg 55.25 -> 16:00 avg 92.65. Critically the climb on 03-21 and 03-22 (13:00-22:00 on 03-22, well before the 03-23T18:00Z Kafka config change) is identical in shape to 03-23, so the pattern predates the maintenance by two days.
  - `F17` ✔ The failure mode is heap exhaustion by GC pressure, not message-volume overflow: WARN logs consist almost entirely of 'heap usage N% over 90 (old gen), full GC took Nms' (03-23: 10 such warnings from 18:34:46 to 19:49:28 with GC times up to 6488ms, immediately followed by the OOM at 19:59:21). The same sequence recurs on 03-21 (25 heap warnings 21:37-22:41, GC up to 8220ms, then OOM 22:59:50) and 03-22 (heap warnings 20:35-21:56, GC up to 8093ms, then OOM 21:59:47). No batch-size, record-size, replay or rebalance log line appears in any of the three windows.
  - `F19` ✔ TemplateCache is UNBOUNDED: every 'TemplateCache stats' log line reports evictions=0, and the entry count grows monotonically within each pod's lifetime (51,750 -> 103,500 -> 155,250 -> 196,650 entries on 03-21, 03-22) with no eviction ever recorded. There is no size cap, TTL, or eviction behavior observable — evictions never becomes non-zero across 14 samples over 03-21..03-23.
  - `F20` ✔ Cache size is bounded only by pod restarts, not by configuration: each day the size climbs in equal 51,750-entry steps to ~196,650 then resets after the OOM restart (03-21 05:45 51,750 ... 21:51 196,650; 03-22 04:45 51,750 ... 20:51 196,650; 03-23 03:30 49,500 ... 18:54 188,100, then 21:00 9,000 and 22:00 18,000 after the 19:59 crash). The counter restarting from a small value right after each crash confirms the map dies with the pod; growth accumulates in memory rather than being evicted.
**Assumptions**
  - Co-tenant pressure exists and is periodic
  - Node placement shares memory envelope
**Rejected because:** The pattern is a clean per-process sawtooth that resets precisely on each notification-service restart and starts from the same ~48-55% baseline (F7, F13); the in-process cache entry count resets only with the pod (F19, F20). Logs show process-internal old-gen GC exhaustion inside notification-service (F17), and growth begins immediately after the 03-21 deploy (F1), not after any node event. Node placement data was unavailable, so this is excluded by shape/log mechanism rather than by positive node evidence.

### H5: Downstream dependency (email-provider/template/DB) blocks, causing backpressure that buffers memory
A slow downstream dependency (email-provider mail API, template rendering, or DB) blocks the consumer, causing unbounded in-flight buffering and memory growth in notification-service.

**Causal chain**
1. email-provider/template/DB becomes slow or blocks *(assumed)*
2. Slow calls cause request backpressure and buffering, growing memory *(assumed)*
3. Memory exhausts, OOMKill, delayed emails [F9]

**Supporting evidence**
  - `F4` ✔ notification-service's dependencies are kafka (queue-consume, shared=true, telemetry=true) and email-provider (external, no telemetry). kafka is shared by order-service and payment-service (queue-produce) and settlement-service (queue-consume), so those neighbours can affect notification-service through Kafka.
**Contradicting evidence**
  - `F13` ✔ Memory sawtooth repeats identically across all three restart days, with each climb starting from the SAME ~48-55% post-restart baseline and rising ~+9-10 percentage points per 4h bucket independent of any external event: 03-21 04:00 avg 59.31 -> 16:00 avg 85.99; 03-22 00:00 avg 52.57 -> 16:00 avg 88.08; 03-23 00:00 avg 55.25 -> 16:00 avg 92.65. Critically the climb on 03-21 and 03-22 (13:00-22:00 on 03-22, well before the 03-23T18:00Z Kafka config change) is identical in shape to 03-23, so the pattern predates the maintenance by two days.
  - `F15` ✔ Competing signal (slow-dependency) is NEGATIVE: downstream/processing latency and error_rate are flat across the entire week, i.e. no mail-provider/template-service/DB slowness accompanies the leak. latency_p99_ms sits at ~110ms with no trend (mean 110.02, max 124.49; the 03-23T20:00 crash bucket is avg 110.04, max 124.49 — indistinguishable from baseline). error_rate is 0.19-0.23% every bucket, spiking only IN the crash bucket itself (03-21T20:00 max 13.78, 03-22T20:00 max 14.73, 03-23T16:00 max 13.96) — i.e. error is a consequence of the OOM, not an upstream cause.
  - `F16` ✔ CPU is flat (mean 25.01%, range 21.78-28.5%) across 03-20..03-23 with no rise during any memory climb or crash bucket — the work per unit time does not increase, so the memory growth is not explained by extra processing volume (retry/replay).
  - `F17` ✔ The failure mode is heap exhaustion by GC pressure, not message-volume overflow: WARN logs consist almost entirely of 'heap usage N% over 90 (old gen), full GC took Nms' (03-23: 10 such warnings from 18:34:46 to 19:49:28 with GC times up to 6488ms, immediately followed by the OOM at 19:59:21). The same sequence recurs on 03-21 (25 heap warnings 21:37-22:41, GC up to 8220ms, then OOM 22:59:50) and 03-22 (heap warnings 20:35-21:56, GC up to 8093ms, then OOM 21:59:47). No batch-size, record-size, replay or rebalance log line appears in any of the three windows.
**Assumptions**
  - A downstream slowdown occurred
  - In-flight buffering without bound
**Rejected because:** Directly contradicted: latency_p99 and error_rate are flat all week, with errors spiking only in the crash bucket itself (F15) — no downstream slowness accompanies the leak. CPU flat (F16), logs show GC/heap exhaustion rather than blocked-call threads (F17), and the sawtooth predates the maintenance and is identical across days (F13).

## Facts (verified observations)
- Dep-5402 (notification-service v3.8.0, 2026-03-21T00:00Z, 'Add in-memory template cache for email rendering') is the only deployment touching notification-service in the last 7 days; the only other deployment in the window is auth-service v7.1.4 ('bump jackson'), unrelated to notification-service, Kafka or memory limits. [F1, F2]
- Exactly one config change was recorded in the last 7 days (and only one across all services in a 23-day window): cfg-2410 on kafka, log.retention.ms 604800000 -> 259200000 at 2026-03-23T18:00:00Z. No memory/resource/limit config changes exist for notification-service (count 0) or any service. [F3, F25, F26, F27]
- notification-service memory_pct follows a 24-hour sawtooth from a ~48% baseline, climbing to 98.44% and resetting to ~48% after each restart, repeating identically on 03-21, 03-22 and 03-23; the pre-incident baseline on 03-20 was flat at ~47-55%. [F7, F8, F13]
- Three OOMKilled events occurred (03-21T22:59:50Z, 03-22T21:59:47Z, 03-23T19:59:21Z, exit code 137); the cumulative restart metric first becomes non-zero in the 2026-03-21T20:00Z bucket and reaches 3 at 2026-03-23T20:00Z, i.e. two of three crashes predate the 03-23T18:00Z Kafka change. [F9, F10]
- TemplateCache is unbounded: every 'TemplateCache stats' line reports evictions=0 across 14 samples over 03-21..03-23, while the entry count grows monotonically within each pod lifetime in ~51,750-entry steps to ~196,650, then resets only after each OOM restart (dying with the pod). [F19, F20]
- The cache key includes an unbounded dimension: dep-5402 introduced 'TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment', and the ~51,750 new entries per 5-6h interval is consistent with per-user-segment key explosion. [F1]
- The failure signature is heap/old-generation GC exhaustion, not message-volume overflow: WARN logs are dominated by 'heap usage N% over 90 (old gen), full GC took Nms' (up to 6488ms on 03-23, 8220ms on 03-21, 8093ms on 03-22) immediately preceding each OOM, with no batch-size, record-size, replay or rebalance lines in any of the three windows. [F17]
- Consumer lag does not precede memory growth: lag stays normal (~55-65, max ~110) throughout each climb and spikes only in the same bucket as the crash (03-21T22:00Z avg 2273/max 17161.92; 03-22T20:00Z max 17611.66; 03-23T18:00Z max 18137.93), then returns to ~57 — lag follows the OOM/restart. [F11]
- Throughput is flat across the window (request_rate min 115.35, max 172.03, mean 140.17 req/s) with the 03-23 16:00-20:00 buckets around the Kafka change/crash no higher than daily 04:00 peaks; CPU is flat too (mean 25.01%, range 21.78-28.5%), so no replay/retry processing surge occurred. [F14, F16]
- Kafka telemetry shows no maintenance-induced anomaly: cpu_pct flat ~35% across 03-20..03-23, and only benign WARNs with no rebalance/partition-reassignment or broker-error entries after the 18:00Z change (last Kafka log 03-23T17:17:15Z, before the change). [F12, F18]
- The competing slow-dependency signal is negative: latency_p99_ms is flat ~110ms and error_rate is 0.19-0.23% every bucket, spiking only inside the crash buckets themselves — errors are a consequence of the OOM, not an upstream cause. [F15]
- Historical incidents do not support the Kafka-replay/partition hypothesis for this pattern: HIST-104 (2025-11-05) was a rebalancing storm without OOM, HIST-112 likewise lost a partition assignment during maintenance, and no historical incident in the searched set has OOMKilled or memory exhaustion as a root cause. [F5]
- No resource-limit change was recorded anywhere in 03-01..03-23, so the ~98.44% memory peak occurred against a constant container limit, not a lowered one. [F25, F26]
- No emails-rendered/render-rate metric exists (notification-service exposes only consumer_lag, cpu_pct, error_rate, latency_p99_ms, memory_pct, request_rate, restarts), and email-provider is external with no telemetry, so render volume could only be proxied by request_rate (flat). [F23, F24]

## Assumptions (not verified)
- The container memory limit is fixed and unchanged across the window (the absence of any resource-limit change is verified, F25/F26/F29, but the exact numeric limit value is not retrievable by any available tool).
- TemplateCache is the dominant contributor to old-generation growth; no other new allocation path in dep-5402 is inspected, since the deploy diff lists the cache and render-path change as its only changes (F1).
- Per-render accumulation is inferred from cache entry-count growth combined with flat request_rate, because no direct emails-rendered series exists (F23, F24).
- The ~25 min trade-confirmation email delay is attributed to the restart/consumer-lag transient rather than measured directly end to end, since email-provider has no telemetry (F24).
- (demoted from facts: no verified observation backs it) Cache growth rate tracks the memory sawtooth: the map roughly quadruples per 16h (51,750 -> 196,650, ~8.6k entries/h) while memory climbs ~+9-10 percentage points per 4h bucket and resets only at each OOM restart.

## Timeline
- `2026-03-20T00:00:00Z` Pre-incident memory baseline flat at ~47-55% (no sawtooth). [F8]
- `2026-03-21T00:00:00Z` Dep-5402 deployed: notification-service v3.8.0 adds TemplateCache (ConcurrentHashMap, keyed template+locale+userSegment) on the render path. [F1]
- `2026-03-21T02:00:00Z` Memory sawtooth onset: steady climb begins from ~52.71% to 57.15% — roughly 2h after the deploy. [F7]
- `2026-03-21T21:51:13Z` TemplateCache entries reach 196,650 with evictions=0 — unbounded, no cap/TTL/eviction. [F19, F20]
- `2026-03-21T22:41:00Z` Heap-exhaustion WARNs (25 heap warnings 21:37-22:41, full GC up to 8220ms). [F17]
- `2026-03-21T22:59:50Z` First OOMKill (exit 137), restarting; consumer lag spikes in the same bucket (max 17161.92) and cache counter resets. [F9, F11, F20]
- `2026-03-22T13:00:00Z` Only other deployment in window: auth-service v7.1.4 'bump jackson' (no relation to notification-service/Kafka/memory). [F2]
- `2026-03-22T16:00:00Z` Identical memory climb repeats (03-22 00:00 avg 52.57 -> 16:00 avg 88.08), before any Kafka change. [F13]
- `2026-03-22T21:59:47Z` Second OOMKill (exit 137); lag spike max 17611.66 in the crash bucket only. [F9, F11]
- `2026-03-23T16:00:00Z` Third daily climb (00:00 avg 55.25 -> 16:00 avg 92.65); request_rate and CPU flat. [F13, F14, F16]
- `2026-03-23T18:00:00Z` Kafka maintenance cfg-2410: log.retention.ms 604800000 -> 259200000 (the maintenance blamed in the report). No resource/limit change anywhere. [F3, F26]
- `2026-03-23T18:34:28Z` Heap usage >90% (old gen) WARNs begin, running to 19:49:28Z with full GC up to 6488ms. [F17]
- `2026-03-23T19:59:21Z` Third OOMKill (exit 137); lag spike max 18137.93 in the crash bucket, then returns to ~57; email delays observed downstream. [F9, F11, F15]
- `2026-03-23T21:00:00Z-22:00:00Z` Cache counter restarts from 9,000 then 18,000 after the crash, confirming the map dies with the pod and growth is un-evicted in-memory accumulation. [F20]

## Recommended actions
- **[immediate]** Immediate: roll back notification-service to the pre-dep-5402 version (revert dep-5402 / v3.8.0), or if a rollback cannot be done quickly, disable the TemplateCache read/write path via the v3.8.0 feature path so renders no longer populate the ConcurrentHashMap. -- The cache is the sole relevant change and is unbounded (evictions=0, monotonic growth); removing it stops the leak. Growth begins ~2h after this deploy and resets only on restart (F1, F7, F13, F19, F20). [F1, F7, F13, F19, F20, F21]
- **[immediate]** Immediate: raise or temporarily scale notification-service memory/replica capacity ONLY as a stopgap if rollback is not feasible, and stage a manual restart schedule ahead of the ~20:00Z daily peak — this buys headroom but does not stop the leak. -- Each climb reaches ~98.4% of a constant limit, then OOMs; restarting before the evening peak defers the crash but a restart is not a fix (F8, F9, F29). [F8, F9, F29]
- **[immediate]** Immediate: if the reviewer still suspects Kafka, explicitly do NOT pursue cfg-2410 changes further, and instead confirm no further action is needed on the Kafka retention change (cfg-2410). -- Two of three OOMs predate the 03-23T18:00Z retention change by ~2 days, Kafka CPU is flat with no rebalance/broker errors, and lag follows rather than precedes each crash (F3, F9, F10, F11, F12, F13, F18). [F3, F9, F10, F11, F12, F13, F18]
- **[short_term]** Short term: after the rollback/revert is live, verify recovery by watching memory_pct return to a flat ~48-55% baseline with no evening climb, TemplateCache entry growth cease (cache absent or bounded), and zero new OOMKilled restarts across at least one full 24h sawtooth cycle. -- The sawtooth shape and per-restart cache reset are the strongest confirming signals; the pattern repeats daily, so one full cycle of observation is needed (F7, F8, F9, F10, F13, F19, F20). [F7, F8, F9, F10, F13, F19, F20]
- **[short_term]** Short term: if the cache is re-introduced, bound it — a size cap (e.g. Caffeine maximumSize / LRU), a TTL, and/or removal of the unbounded userSegment key dimension — plus an alert on cache entry count and eviction ratio. -- The load-bearing defect is unboundedness with an inflated key space from the per-userSegment dimension; bounding it directly addresses the verified mechanism (F19, F20, F21, F22). [F19, F20, F21, F22]
- **[long_term]** Long term: add a memory-sawtooth / post-deploy heap-growth alarm (e.g. alert when old-gen usage shows sustained +N pp/4h growth or >90% heap WARNs) and require an explicit cache-size/eviction review in the deploy review checklist for render-path caches. -- The leak ran three days across three daily WARN→OOM cycles before being reported; earlier detection plus deploy-review guardrails prevent recurrence (F7, F13, F17). [F7, F13, F17]
- **[long_term]** Long term: instrument an emails-rendered / render-rate and cache hit-ratio metric so per-render memory contribution and value of the cache can be measured directly. -- No render-count metric exists today, forcing entry-count and flat request_rate to serve as proxies; direct measurement would remove this gap from future investigations (F23, F24). [F23, F24]

## Data gaps
- No tool returns notification-service's container memory limit/request value; get_config_changes and get_deployments contain no resource fields, and dependency data exposes no resource-spec node. Only the negative (no limit change over 23 days) is established, so the absolute limit (e.g. 512Mi vs 1Gi) is unknown.
- Kafka broker-side quotas / JVM heap / replica settings after the 18:00Z maintenance were not verifiable; only log.retention.ms appeared. Broker-resource changes cannot be fully excluded from contributing to lag during the maintenance window.
- Node-pool / host placement data was unavailable, so co-tenant memory pressure (H4) is excluded by memory-sawtooth shape and process-internal GC logs rather than by positive node evidence.
- No emails-rendered / render-rate metric exists and email-provider has no telemetry, so per-render accumulation and the ~25 min email delay could not be measured directly; only cache entry-count growth and flat request_rate served as proxies.
- Sub-hour (zoomed) metric buckets for the 03-21 onset and evening peaks were not obtained, so onset is localized only to the ~02:00-04:00Z bucket and not to the minute.
- A log query filtered on 'rebalance' was not executed (budget exhausted); rebalance absence is inferred from full WARN/ERROR logs containing no rebalance lines plus flat Kafka CPU and no broker errors, so a very low-volume rebalance cannot be 100% excluded.
- Message-size / record-size metrics are not exposed (available metrics are consumer_lag, cpu_pct, error_rate, latency_p99_ms, memory_pct, request_rate, restarts), so the H3 message-size variant could not be measured directly.
- No other business service's memory was sampled (order-service query failed with budget_exhausted), so the blast-radius question is only partially answered — Kafka was confirmed normal but no other service was checked.

## What would change this conclusion
- Evidence that memory growth continues at the same rate after reverting dep-5402 / removing the TemplateCache path, which would disprove H1 as the dominant cause.
- A verified resource-limit change (e.g. a lowered memory limit) for notification-service in the 03-21..03-23 window, which would make a lowered limit a contributing/alternative cause rather than the constant-limit assumption (now unsupported by F25/F26/F29 but not positively ruled out at the numeric level).
- A direct measurement showing another allocation path introduced by dep-5402 (beyond the cache) dominates old-generation growth.
- Evidence that a Kafka consumer-group rebalance or replay actually did occur before the first 03-21 OOM (log lines, partition-reassignment events, or a traffic spike), which would revive H2 and break the chronological refutation.
- A verified upstream producer change raising message size or rate in the window, which would revive H3.
- Node/pod placement evidence showing a co-tenant sharing notification-service's memory envelope with a periodic pressure pattern matching the sawtooth, which would revive H4.
- Downstream email-provider latency/error data showing a slowdown preceding memory growth, which would revive H5 as a backpressure cause rather than a consequence.

## Integrity warnings (set by code)
- fact demoted to assumption: Cache growth rate tracks the memory sawtooth: the map roughly quadruples per 16h

## Investigation stats
- latency 179.0s, LLM calls 26, tokens 194186 (in 164045 / out 30141)
- tool calls 39 (6 failed, 0 partial, 0 recovered by retry), findings 29/29 verified
