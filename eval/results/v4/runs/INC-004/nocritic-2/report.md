# Incident investigation report: INC-004
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The evidence points to an unbounded in-memory template cache introduced by notification-service v3.8.0 (dep-5402, 2026-03-21) as the root cause of the recurring OOMKilled restarts, at medium confidence (0.60, top hypothesis H1 and clearly ahead of the alternatives at ≤0.10). Memory_pct climbs monotonically in a ~24h sawtooth from ~48% to ~95-98% and resets at each of the three daily OOMs, with a classic old-gen GC-pressure log signature leading each kill; lag spikes coincide with (and recover right after) each restart rather than preceding it, and there is no throughput or lag response at the Kafka retention change. The reporter's replay theory is unsupported: lowering log.retention.ms truncates segments rather than forcing a replay, no offset reset or topic/partition change is recorded, and the ramp and restarts began ~2 days before the maintenance. Remaining uncertainty comes from the absence of byte-level heap attribution, service-scoped config history, and node/sizing change visibility.

## Probable root cause
**Unbounded in-memory template cache in notification-service v3.8.0 (memory leak)** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded in-memory template cache in notification-service v3.8.0 (memory leak) | bad_deployment | notification-service | dep-5402 | 0.60 (medium) | active |
| 2 | H4: Shared-component resource contention reduced memory available to notification-service | resource_exhaustion | shared infrastructure / node | null | 0.10 (low) | active |
| 3 | H5: Email-provider / downstream slowdown caused unbounded in-memory queueing of unsent notifications | dependency_failure | email-provider | null | 0.08 (low) | active |
| 4 | H3: Consumer-group rebalance / partition reassignment storm after broker maintenance concentrated load per instance | infrastructure_failure | kafka | tc-004 | 0.10 (low) | rejected |
| 5 | H2: Kafka retention change (7d→3d) triggered a replay/backlog that the consumer buffered in memory | config_change | kafka | tc-004 | 0.05 (low) | rejected |

### H1: Unbounded in-memory template cache in notification-service v3.8.0 (memory leak)
notification-service v3.8.0 (dep-5402, 2026-03-21) added a ConcurrentHashMap template cache keyed by template+locale+userSegment with no eviction/bound. The cache grows as new key combinations are rendered, so old-gen heap climbs monotonically after each restart until it saturates and the container is OOMKilled (exit 137). Each cycle takes ~24h; restarts are the sawtooth resets. Kafka lag spikes are a downstream consequence of the restart, not the cause.

**Causal chain**
1. dep-5402 (v3.8.0) adds an unbounded ConcurrentHashMap template cache in notification-service, the only recent deploy touching the service [F2, F6]
2. Cache grows with each distinct template+locale+userSegment rendering, so old-gen heap rises monotonically instead of steady-state *(assumed)*
3. memory_pct shows a monotonic sawtooth: ~48% after reset climbing ~5%/2h to 95-98% before the next drop — the leak signature [F7, F14]
4. Old-gen saturates and full-GC pauses escalate (4152→6488ms) until the container is OOMKilled (exit 137) [F15, F8]
5. Restart counter steps exactly once per ~24h cycle (Mar 21 22:00, Mar 22 22:00, Mar 23 20:00), consistent with a fixed-size leak, not an external trigger [F9]
6. Consumer lag is flat all day and spikes only in the OOM bucket, then returns to baseline in the same bucket — downstream of the restart [F10, F12, F13]
7. Delayed trade-confirmation emails follow from the consumer being frozen/down during the OOM-restart window [F12]

**Supporting evidence**
  - `F2` ✔ notification-service itself was deployed on 2026-03-21T00:00:00Z (dep-5402, v3.8.0, author s.menon) with change 'Add in-memory template cache for email rendering' — a ConcurrentHashMap keyed by template+locale+userSegment — and it is still live, not rolled back. This is the only recent deploy touching notification-service.
  - `F6` ✔ The Kafka maintenance change (retention 7d→3d) does not by itself force a data replay — lowering retention truncates old segments rather than re-reading them; the reported 'consumer choking on a replay' is not supported by any change I found (no consumer-group offset reset, no topic/partition change recorded). The only change that adds unbounded in-memory growth is the notification-service v3.8.0 template cache deployed 2 days before onset.
  - `F7` ✔ notification-service memory_pct rises gradually and monotonically within each ~24h cycle, then resets — a sawtooth, not an abrupt jump. 7-day series starts ~48% after each reset and climbs in ~5%/2h steps to ~95-98% before the next drop.
  - `F8` ✔ Three OOMKilled restarts of the same container notification-service-7d9f (exit code 137) occurred, timed at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z — one per day, each ~1h earlier than the previous.
  - `F9` ✔ Cumulative restart counter confirms exactly 3 restarts, stepping 0→1 at the 22:00 bucket of Mar 21, 1→2 at 22:00 of Mar 22, and 2→3 at 20:00 of Mar 23; it was a flat 0 for all of Mar 20 and most of Mar 21.
  - `F10` ✔ Consumer lag is flat/low (~56-65 msgs) throughout each day and spikes only in the same 2h bucket as each OOM: max 17161.92 at 2026-03-21T22:00Z, max 17611.66 at 2026-03-22T20:00Z, max 18137.93 at 2026-03-23T18:00Z — i.e. lag spikes coincide with the memory-exhaustion/OOM window, not before the ~24h ramp.
  - `F12` ✔ The lag spike is short-lived relative to the memory ramp: after each OOM the lag returns to baseline in the same 2h bucket that the restart froze (e.g. 2026-03-23T20:00Z avg 1542.68 falls back to 57.89 by 2026-03-23T22:00Z). This is consistent with lag being a consequence of the restart, not a leading trigger.
  - `F13` ✔ In the post-maintenance window (2026-03-23T16:00–23:59Z), notification-service consumer_lag stays flat at ~52–67 msgs throughout the flat period and spikes ONLY at the OOM: 2026-03-23T19:30Z avg 3069.83/max 18137.93, 20:00Z avg 6004.47/max 14213.61, then back to avg 57.18 at 20:30Z. Importantly the 18:00Z bucket (right at the Kafka retention change, 18:00:00Z) shows avg 64.6/max 75.74 — no lag response to the maintenance itself. Lag rises only in the ~30 min bucket containing the 19:59:21Z OOM, i.e. after/with the memory exhaustion, not at the change time.
  - `F14` ✔ notification-service memory_pct was already in a monotonic ramp well BEFORE the 18:00Z maintenance: 2026-03-23T14:00Z avg 83.82, climbing continuously (16:30Z 89.12, 17:45Z 93.37, 19:00Z 96.17, max 98.44 at 19:25Z) and only dropping at the 19:50Z bucket (avg 66.53) into 20:15Z (avg 47.2) — the restart reset. So the last ~4h pre-OOM is a steady climb with no inflection at 18:00Z.
  - `F15` ✔ notification-service logs in the maintenance window (18:00–21:00Z) show a GC/heap-pressure signature leading the OOM, NOT rebalance or partition-assignment activity: 10 WARN 'heap usage N% over 90 (old gen), full GC took Nms' lines from 18:34:46Z (4152ms) escalating to 19:49:28Z (6488ms), then one ERROR 'container notification-service-7d9f OOMKilled (exit code 137), restarting' at 19:59:21Z. Full-GC pause duration grows monotonically (4152→6488ms) toward the kill.
  - `F17` ✔ notification-service request_rate (consumer throughput proxy) is flat with no replay surge: ~130–142 req/s across 14:00–23:59Z, dipping slightly to avg 128.67 at 21:20Z (post-restart) and never exceeding max 152.2. There is no throughput spike that would indicate an unusually large replay volume crossing the maintenance or the OOM.
  - `F18` ✔ Interpretation: the OOM correlates with heap/old-gen saturation (an internal memory ramp, already at ~84% by 14:00Z and rising to ~98% by 19:25Z) reaching a limit, not with replay volume or a traffic/rebalance shift. Lag spikes at 19:30–20:00Z are temporally downstream of near-full heap rather than at the 18:00Z retention change, and throughput is flat — consistent with the 'choking on a replay' theory being unsupported in this window.
**Contradicting evidence**
  - `F1` ✔ The only config change on shared infrastructure in the 48h window (2026-03-21→23) is a Kafka maintenance change: kafka key log.retention.ms reduced from 604800000 to 259200000 (7 days → 3 days), changed at 2026-03-23T18:00:00Z by platform-team, described as 'Kafka maintenance: reduce retention to 3 days'. No topic/partition/consumer-group/rebalance setting was altered in this change.
  - `F5` ✔ Historical precedent (lead only): HIST-104 (2025-11-05) 'notification-service lag after Kafka broker rebalance' had identical symptoms (consumer lag spike, delayed emails) but root cause 'Partition reassignment caused consumer group rebalancing storm' — an infrastructure/rebalance cause, not a retention change; resolution was 'Throttled reassignment'. A second lead HIST-112 (2026-02-03, settlement-service) is 'Consumer lost partition assignment during broker maintenance'.
**Assumptions**
  - The template cache actually accumulates entries at a rate matching a ~24h hour-to-OOM cycle; no byte-level heap attribution exists (only memory_pct %).
  - There is no eviction/weak-reference bound in the cache implementation — supported by the change being 'Add in-memory template cache' with no noted bound, but unverified.
  - No earlier notification-service deploy or the pre-existing code could equally explain the ramp; deployment history for the service is not retrievable (budget_exhausted gap).

### H4: Shared-component resource contention reduced memory available to notification-service
A change to node sizing, Kafka broker container limits, or JVM limits in the same maintenance window reduced the memory ceiling available to notification-service (or raised its concurrency), so a constant working set now hits the limit. Would affect co-tenant consumers too.

**Causal chain**
1. Node/container/JVM memory limit or sizing was reduced for notification-service's pod or its node *(assumed)*
2. The effective memory ceiling drops, so a previously-tolerable working set exceeds the limit *(assumed)*
3. Container is OOMKilled (exit 137) once the ramp touches the new lower ceiling [F8]

**Supporting evidence**
  - `F4` ✔ Kafka is a SHARED component used by several services: order-service (queue-produce), payment-service (queue-produce), settlement-service (queue-consume) and notification-service (queue-consume). notification-service also depends on email-provider (external, no telemetry). So notification-service competes for the shared Kafka cluster with order-, payment- and settlement-services.
  - `F8` ✔ Three OOMKilled restarts of the same container notification-service-7d9f (exit code 137) occurred, timed at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z — one per day, each ~1h earlier than the previous.
**Contradicting evidence**
  - `F1` ✔ The only config change on shared infrastructure in the 48h window (2026-03-21→23) is a Kafka maintenance change: kafka key log.retention.ms reduced from 604800000 to 259200000 (7 days → 3 days), changed at 2026-03-23T18:00:00Z by platform-team, described as 'Kafka maintenance: reduce retention to 3 days'. No topic/partition/consumer-group/rebalance setting was altered in this change.
  - `F11` ✔ order-service memory_pct is flat and stable across the whole window (avg ~59.5-60.5, max ~66), showing no concurrent memory growth like notification-service's ramp.
  - `F16` ✔ No rebalance or partition-assignment events exist in notification-service logs for the whole 2026-03-23T14:00–23:59Z window: searches for 'rebalance' and 'partition' both returned total_matched 0 (returned 0, truncated false). The same 'rebalance' search on the co-sharer settlement-service also returned 0 matches. There is no log evidence of a rebalance storm or partition reassignment around the maintenance/OOM.
  - `F7` ✔ notification-service memory_pct rises gradually and monotonically within each ~24h cycle, then resets — a sawtooth, not an abrupt jump. 7-day series starts ~48% after each reset and climbs in ~5%/2h steps to ~95-98% before the next drop.
  - `F14` ✔ notification-service memory_pct was already in a monotonic ramp well BEFORE the 18:00Z maintenance: 2026-03-23T14:00Z avg 83.82, climbing continuously (16:30Z 89.12, 17:45Z 93.37, 19:00Z 96.17, max 98.44 at 19:25Z) and only dropping at the 19:50Z bucket (avg 66.53) into 20:15Z (avg 47.2) — the restart reset. So the last ~4h pre-OOM is a steady climb with no inflection at 18:00Z.
  - `F9` ✔ Cumulative restart counter confirms exactly 3 restarts, stepping 0→1 at the 22:00 bucket of Mar 21, 1→2 at 22:00 of Mar 22, and 2→3 at 20:00 of Mar 23; it was a flat 0 for all of Mar 20 and most of Mar 21.
**Assumptions**
  - A sizing/limit change occurred — the gaps state node instance-type and broker-count changes may be invisible in the deploy feed, so this cannot be fully excluded.
  - The effect would be notification-specific despite shared Kafka, since order-service is flat.

### H5: Email-provider / downstream slowdown caused unbounded in-memory queueing of unsent notifications
The external email-provider degraded, so rendered-but-unsent notifications accumulated in an in-memory queue inside notification-service, driving heap to exhaustion independent of Kafka volume. Emails were delayed because the provider is slow, and the OOM followed from the internal queue.

**Causal chain**
1. email-provider (external, no telemetry) slowed or errored *(assumed)*
2. notification-service queues unsent messages in memory without backpressure *(assumed)*
3. Heap saturates → OOMKilled (exit 137) and emails delayed ~25 min [F8]

**Supporting evidence**
  - `F4` ✔ Kafka is a SHARED component used by several services: order-service (queue-produce), payment-service (queue-produce), settlement-service (queue-consume) and notification-service (queue-consume). notification-service also depends on email-provider (external, no telemetry). So notification-service competes for the shared Kafka cluster with order-, payment- and settlement-services.
**Contradicting evidence**
  - `F10` ✔ Consumer lag is flat/low (~56-65 msgs) throughout each day and spikes only in the same 2h bucket as each OOM: max 17161.92 at 2026-03-21T22:00Z, max 17611.66 at 2026-03-22T20:00Z, max 18137.93 at 2026-03-23T18:00Z — i.e. lag spikes coincide with the memory-exhaustion/OOM window, not before the ~24h ramp.
  - `F17` ✔ notification-service request_rate (consumer throughput proxy) is flat with no replay surge: ~130–142 req/s across 14:00–23:59Z, dipping slightly to avg 128.67 at 21:20Z (post-restart) and never exceeding max 152.2. There is no throughput spike that would indicate an unusually large replay volume crossing the maintenance or the OOM.
  - `F12` ✔ The lag spike is short-lived relative to the memory ramp: after each OOM the lag returns to baseline in the same 2h bucket that the restart froze (e.g. 2026-03-23T20:00Z avg 1542.68 falls back to 57.89 by 2026-03-23T22:00Z). This is consistent with lag being a consequence of the restart, not a leading trigger.
  - `F13` ✔ In the post-maintenance window (2026-03-23T16:00–23:59Z), notification-service consumer_lag stays flat at ~52–67 msgs throughout the flat period and spikes ONLY at the OOM: 2026-03-23T19:30Z avg 3069.83/max 18137.93, 20:00Z avg 6004.47/max 14213.61, then back to avg 57.18 at 20:30Z. Importantly the 18:00Z bucket (right at the Kafka retention change, 18:00:00Z) shows avg 64.6/max 75.74 — no lag response to the maintenance itself. Lag rises only in the ~30 min bucket containing the 19:59:21Z OOM, i.e. after/with the memory exhaustion, not at the change time.
  - `F9` ✔ Cumulative restart counter confirms exactly 3 restarts, stepping 0→1 at the 22:00 bucket of Mar 21, 1→2 at 22:00 of Mar 22, and 2→3 at 20:00 of Mar 23; it was a flat 0 for all of Mar 20 and most of Mar 21.
**Assumptions**
  - A provider slowdown of sufficient duration and magnitude to fill heap over a ~24h cycle — unsupported; no telemetry exists for email-provider.
  - Consumer lag would stay flat while an internal send queue fills — not directly checked but consistent with F17's flat request_rate.

### H3: Consumer-group rebalance / partition reassignment storm after broker maintenance concentrated load per instance
Per historical precedent HIST-104/HIST-112, broker maintenance induces a consumer-group rebalancing storm or partition reassignment that concentrates partitions on fewer instances, spiking per-instance memory and starving notification-service of its assignment until it is OOMKilled. The Kafka maintenance (tc-004) is the origin even though it only changed retention.

**Causal chain**
1. Kafka maintenance triggers a broker-side rebalance/partition reassignment (as in HIST-104) [F5]
2. Partitions concentrate on fewer notification-service instances, raising per-instance memory and consumer lag *(assumed)*
3. Per-instance heap saturates → OOMKilled at 19:59:21Z and prior daily restarts [F8]

**Supporting evidence**
  - `F5` ✔ Historical precedent (lead only): HIST-104 (2025-11-05) 'notification-service lag after Kafka broker rebalance' had identical symptoms (consumer lag spike, delayed emails) but root cause 'Partition reassignment caused consumer group rebalancing storm' — an infrastructure/rebalance cause, not a retention change; resolution was 'Throttled reassignment'. A second lead HIST-112 (2026-02-03, settlement-service) is 'Consumer lost partition assignment during broker maintenance'.
  - `F1` ✔ The only config change on shared infrastructure in the 48h window (2026-03-21→23) is a Kafka maintenance change: kafka key log.retention.ms reduced from 604800000 to 259200000 (7 days → 3 days), changed at 2026-03-23T18:00:00Z by platform-team, described as 'Kafka maintenance: reduce retention to 3 days'. No topic/partition/consumer-group/rebalance setting was altered in this change.
**Contradicting evidence**
  - `F16` ✔ No rebalance or partition-assignment events exist in notification-service logs for the whole 2026-03-23T14:00–23:59Z window: searches for 'rebalance' and 'partition' both returned total_matched 0 (returned 0, truncated false). The same 'rebalance' search on the co-sharer settlement-service also returned 0 matches. There is no log evidence of a rebalance storm or partition reassignment around the maintenance/OOM.
  - `F13` ✔ In the post-maintenance window (2026-03-23T16:00–23:59Z), notification-service consumer_lag stays flat at ~52–67 msgs throughout the flat period and spikes ONLY at the OOM: 2026-03-23T19:30Z avg 3069.83/max 18137.93, 20:00Z avg 6004.47/max 14213.61, then back to avg 57.18 at 20:30Z. Importantly the 18:00Z bucket (right at the Kafka retention change, 18:00:00Z) shows avg 64.6/max 75.74 — no lag response to the maintenance itself. Lag rises only in the ~30 min bucket containing the 19:59:21Z OOM, i.e. after/with the memory exhaustion, not at the change time.
  - `F14` ✔ notification-service memory_pct was already in a monotonic ramp well BEFORE the 18:00Z maintenance: 2026-03-23T14:00Z avg 83.82, climbing continuously (16:30Z 89.12, 17:45Z 93.37, 19:00Z 96.17, max 98.44 at 19:25Z) and only dropping at the 19:50Z bucket (avg 66.53) into 20:15Z (avg 47.2) — the restart reset. So the last ~4h pre-OOM is a steady climb with no inflection at 18:00Z.
  - `F9` ✔ Cumulative restart counter confirms exactly 3 restarts, stepping 0→1 at the 22:00 bucket of Mar 21, 1→2 at 22:00 of Mar 22, and 2→3 at 20:00 of Mar 23; it was a flat 0 for all of Mar 20 and most of Mar 21.
  - `F10` ✔ Consumer lag is flat/low (~56-65 msgs) throughout each day and spikes only in the same 2h bucket as each OOM: max 17161.92 at 2026-03-21T22:00Z, max 17611.66 at 2026-03-22T20:00Z, max 18137.93 at 2026-03-23T18:00Z — i.e. lag spikes coincide with the memory-exhaustion/OOM window, not before the ~24h ramp.
  - `F17` ✔ notification-service request_rate (consumer throughput proxy) is flat with no replay surge: ~130–142 req/s across 14:00–23:59Z, dipping slightly to avg 128.67 at 21:20Z (post-restart) and never exceeding max 152.2. There is no throughput spike that would indicate an unusually large replay volume crossing the maintenance or the OOM.
  - `F18` ✔ Interpretation: the OOM correlates with heap/old-gen saturation (an internal memory ramp, already at ~84% by 14:00Z and rising to ~98% by 19:25Z) reaching a limit, not with replay volume or a traffic/rebalance shift. Lag spikes at 19:30–20:00Z are temporally downstream of near-full heap rather than at the 18:00Z retention change, and throughput is flat — consistent with the 'choking on a replay' theory being unsupported in this window.
  - `F7` ✔ notification-service memory_pct rises gradually and monotonically within each ~24h cycle, then resets — a sawtooth, not an abrupt jump. 7-day series starts ~48% after each reset and climbs in ~5%/2h steps to ~95-98% before the next drop.
  - `F11` ✔ order-service memory_pct is flat and stable across the whole window (avg ~59.5-60.5, max ~66), showing no concurrent memory growth like notification-service's ramp.
**Assumptions**
  - That log verbosity simply did not emit rebalance events (the gap explicitly flags absence of rebalance logs as weak evidence) — this is the only way H3 survives F16.
  - That a broker maintenance limited to a retention key can induce reassignment with no topic/partition setting change (F1 says none were altered).
**Rejected because:** HIST-104/HIST-112 are historical leads, not current-incident evidence, and are weak by policy. F16: zero rebalance/partition-assignment log lines for notification-service AND co-sharer settlement-service in the incident window. F1: the maintenance change altered only log.retention.ms — no topic/partition/consumer-group/rebalance setting. F13/F14/F17: no lag or throughput response at 18:00Z and the memory ramp predates the change. F9/F8: restarts began 2 days before the maintenance, so this trigger cannot cover the full symptom set.

### H2: Kafka retention change (7d→3d) triggered a replay/backlog that the consumer buffered in memory
The Kafka maintenance config change (log.retention.ms 604800000→259200000, tc-004) at 2026-03-23T18:00Z caused notification-service to choke on a replay/backlog that it buffered unboundedly in memory, causing the 19:59:21Z OOM. This is the reporter's theory.

**Causal chain**
1. tc-004 reduces Kafka log.retention.ms from 7d to 3d on shared infra at 18:00Z [F1]
2. The retention change somehow forces a consumer-group replay of already-retained messages *(assumed)*
3. notification-service buffers the replay backlog in memory *(assumed)*
4. Heap saturates and the container is OOMKilled at 19:59:21Z [F8]

**Supporting evidence**
  - `F1` ✔ The only config change on shared infrastructure in the 48h window (2026-03-21→23) is a Kafka maintenance change: kafka key log.retention.ms reduced from 604800000 to 259200000 (7 days → 3 days), changed at 2026-03-23T18:00:00Z by platform-team, described as 'Kafka maintenance: reduce retention to 3 days'. No topic/partition/consumer-group/rebalance setting was altered in this change.
  - `F5` ✔ Historical precedent (lead only): HIST-104 (2025-11-05) 'notification-service lag after Kafka broker rebalance' had identical symptoms (consumer lag spike, delayed emails) but root cause 'Partition reassignment caused consumer group rebalancing storm' — an infrastructure/rebalance cause, not a retention change; resolution was 'Throttled reassignment'. A second lead HIST-112 (2026-02-03, settlement-service) is 'Consumer lost partition assignment during broker maintenance'.
**Contradicting evidence**
  - `F6` ✔ The Kafka maintenance change (retention 7d→3d) does not by itself force a data replay — lowering retention truncates old segments rather than re-reading them; the reported 'consumer choking on a replay' is not supported by any change I found (no consumer-group offset reset, no topic/partition change recorded). The only change that adds unbounded in-memory growth is the notification-service v3.8.0 template cache deployed 2 days before onset.
  - `F13` ✔ In the post-maintenance window (2026-03-23T16:00–23:59Z), notification-service consumer_lag stays flat at ~52–67 msgs throughout the flat period and spikes ONLY at the OOM: 2026-03-23T19:30Z avg 3069.83/max 18137.93, 20:00Z avg 6004.47/max 14213.61, then back to avg 57.18 at 20:30Z. Importantly the 18:00Z bucket (right at the Kafka retention change, 18:00:00Z) shows avg 64.6/max 75.74 — no lag response to the maintenance itself. Lag rises only in the ~30 min bucket containing the 19:59:21Z OOM, i.e. after/with the memory exhaustion, not at the change time.
  - `F14` ✔ notification-service memory_pct was already in a monotonic ramp well BEFORE the 18:00Z maintenance: 2026-03-23T14:00Z avg 83.82, climbing continuously (16:30Z 89.12, 17:45Z 93.37, 19:00Z 96.17, max 98.44 at 19:25Z) and only dropping at the 19:50Z bucket (avg 66.53) into 20:15Z (avg 47.2) — the restart reset. So the last ~4h pre-OOM is a steady climb with no inflection at 18:00Z.
  - `F16` ✔ No rebalance or partition-assignment events exist in notification-service logs for the whole 2026-03-23T14:00–23:59Z window: searches for 'rebalance' and 'partition' both returned total_matched 0 (returned 0, truncated false). The same 'rebalance' search on the co-sharer settlement-service also returned 0 matches. There is no log evidence of a rebalance storm or partition reassignment around the maintenance/OOM.
  - `F17` ✔ notification-service request_rate (consumer throughput proxy) is flat with no replay surge: ~130–142 req/s across 14:00–23:59Z, dipping slightly to avg 128.67 at 21:20Z (post-restart) and never exceeding max 152.2. There is no throughput spike that would indicate an unusually large replay volume crossing the maintenance or the OOM.
  - `F18` ✔ Interpretation: the OOM correlates with heap/old-gen saturation (an internal memory ramp, already at ~84% by 14:00Z and rising to ~98% by 19:25Z) reaching a limit, not with replay volume or a traffic/rebalance shift. Lag spikes at 19:30–20:00Z are temporally downstream of near-full heap rather than at the 18:00Z retention change, and throughput is flat — consistent with the 'choking on a replay' theory being unsupported in this window.
  - `F7` ✔ notification-service memory_pct rises gradually and monotonically within each ~24h cycle, then resets — a sawtooth, not an abrupt jump. 7-day series starts ~48% after each reset and climbs in ~5%/2h steps to ~95-98% before the next drop.
  - `F9` ✔ Cumulative restart counter confirms exactly 3 restarts, stepping 0→1 at the 22:00 bucket of Mar 21, 1→2 at 22:00 of Mar 22, and 2→3 at 20:00 of Mar 23; it was a flat 0 for all of Mar 20 and most of Mar 21.
**Assumptions**
  - Lowering log.retention.ms deletes old segments; re-reading them requires an offset reset that no change records — this is the weakest link.
  - The replay would have to start within the 18:00–19:59Z window and produce lag/throughput responses that are in fact absent.
**Rejected because:** F6 (inference): lowering retention truncates old segments, it does not force a replay; no offset reset or consumer-group change recorded. F13: the 18:00Z bucket shows avg lag 64.6/max 75.74 — no lag response at the change time; lag rises only in the bucket containing the OOM. F17: request_rate is flat ~130-142 req/s with no replay surge. F14: the memory ramp was already continuous from 14:00Z (83.82%) with no inflection at 18:00Z, i.e. the ramp predates the change by hours. F9/F7/F8 show restarts began Mar 21 22:59Z, ~2 days before the maintenance, so the change cannot explain the earlier restarts at all.

## Facts (verified observations)
- notification-service v3.8.0 was deployed on 2026-03-21T00:00:00Z (dep-5402, author s.menon) with change 'Add in-memory template cache for email rendering' — a ConcurrentHashMap keyed by template+locale+userSegment — and it is still live, not rolled back; it is the only recent deploy touching notification-service. [F2]
- The only config change on shared infrastructure in the 48h window (2026-03-21→23) is the Kafka maintenance change reducing log.retention.ms from 604800000 to 259200000 (7d→3d) at 2026-03-23T18:00:00Z; no topic/partition/consumer-group/rebalance setting was altered. [F1]
- notification-service memory_pct rises gradually and monotonically within each ~24h cycle and then resets — a sawtooth, starting ~48% after each reset and climbing in ~5%/2h steps to ~95-98% before the next drop. [F7]
- Three OOMKilled restarts of the same container notification-service-7d9f (exit code 137) occurred at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z — one per day, each ~1h earlier than the previous; the cumulative restart counter steps 0→1→2→3 at the corresponding 2h buckets and was flat 0 for all of Mar 20 and most of Mar 21. [F8, F9]
- Consumer lag is flat/low (~56-65 msgs) throughout each day and spikes only in the same 2h bucket as each OOM (max 17161.92 at 2026-03-21T22:00Z, 17611.66 at 2026-03-22T20:00Z, 18137.93 at 2026-03-23T18:00Z), and returns to baseline in the very next bucket after the restart (e.g. avg 1542.68 at 20:00Z falls back to 57.89 by 22:00Z). [F10]
- In the post-maintenance window, notification-service consumer_lag stays flat at ~52-67 msgs and spikes only at the OOM (19:30Z avg 3069.83/max 18137.93; 20:00Z avg 6004.47/max 14213.61; back to avg 57.18 at 20:30Z); the 18:00Z bucket at the retention change shows avg 64.6/max 75.74 — no lag response to the maintenance itself. [F13]
- notification-service memory_pct was already in a monotonic ramp well before the 18:00Z maintenance (14:00Z avg 83.82, 16:30Z 89.12, 17:45Z 93.37, 19:00Z 96.17, max 98.44 at 19:25Z) with no inflection at 18:00Z; it only dropped at the 19:50Z bucket (avg 66.53) into 20:15Z (avg 47.2), the restart reset. [F14]
- notification-service logs in the 18:00-21:00Z window show a GC/heap-pressure signature leading the OOM, not rebalance or partition-assignment activity: 10 WARN 'heap usage N% over 90 (old gen), full GC took Nms' lines from 18:34:46Z (4152ms) escalating to 19:49:28Z (6488ms), then one ERROR 'container notification-service-7d9f OOMKilled (exit code 137), restarting' at 19:59:21Z. [F15]
- No rebalance or partition-assignment events exist in notification-service logs for the whole 2026-03-23T14:00-23:59Z window (searches for 'rebalance' and 'partition' both returned total_matched 0), and the same 'rebalance' search on the co-sharer settlement-service also returned 0 matches. [F16]
- notification-service request_rate (consumer throughput proxy) is flat with no replay surge: ~130-142 req/s across 14:00-23:59Z, dipping slightly to avg 128.67 at 21:20Z post-restart, never exceeding max 152.2. [F17]
- order-service memory_pct is flat and stable across the whole window (avg ~59.5-60.5, max ~66), showing no concurrent memory growth like notification-service's ramp. [F11]
- Kafka is a shared component used by order-service and payment-service (queue-produce) and settlement-service and notification-service (queue-consume); notification-service also depends on email-provider (external, no telemetry). [F4]
- Historical leads HIST-104 (2025-11-05, notification-service lag after Kafka broker rebalance) and HIST-112 (2026-02-03, settlement-service lost partition assignment during broker maintenance) describe rebalance-related causes, but are historical leads only, not current-incident evidence. [F5]

## Assumptions (not verified)
- The template cache actually accumulates entries at a rate matching a ~24h hour-to-OOM cycle; no byte-level heap attribution exists (only memory_pct %).
- There is no eviction/weak-reference bound in the cache implementation — supported by the change being 'Add in-memory template cache' with no noted bound, but unverified.
- No earlier notification-service deploy or pre-existing code could equally explain the ramp; deployment history for the service is not retrievable (budget_exhausted gap).
- The absence of rebalance log lines is meaningful evidence rather than a log-verbosity artifact; the gap explicitly flags that the log stream may simply not emit rebalance events.
- (demoted from facts: no verified observation backs it) Lowering Kafka retention truncates old segments rather than re-reading them; no consumer-group offset reset or topic/partition change was found that would force a replay, and the only change that adds unbounded in-memory growth is the notification-service v3.8.0 template cache deployed 2 days before onset.
- (demoted from facts: no verified observation backs it) The Kafka maintenance change and the OOM correlate with near-full heap (already ~84% by 14:00Z rising to ~98% by 19:25Z) rather than with replay volume or a traffic/rebalance shift; lag spikes at 19:30-20:00Z are temporally downstream of near-full heap, and throughput is flat.

## Timeline
- `2026-03-21T00:00:00Z` notification-service v3.8.0 deployed (dep-5402, author s.menon) with 'Add in-memory template cache for email rendering' — ConcurrentHashMap keyed by template+locale+userSegment; no rollback recorded. [F2]
- `2026-03-21T22:59:50Z` First OOMKilled restart of notification-service-7d9f (exit 137); restart counter steps 0→1 in the 22:00 bucket; lag spikes to max 17161.92 in the same 2h bucket. [F8, F9, F10]
- `2026-03-22T21:59:47Z` Second OOMKilled restart (exit 137), ~1h earlier than the previous; restart counter steps 1→2 in the 22:00 bucket; lag max 17611.66. [F8, F9, F10]
- `2026-03-23T14:00:00Z` Memory ramp already underway: memory_pct avg 83.82 with continuous climb (16:30Z 89.12, 17:45Z 93.37) — hours before the Kafka maintenance. [F14]
- `2026-03-23T18:00:00Z` Kafka maintenance change applied: log.retention.ms 604800000→259200000 (7d→3d) by platform-team; no lag response in this bucket (avg 64.6/max 75.74) and no throughput surge (request_rate flat ~130-142 req/s). [F1, F13, F17]
- `2026-03-23T18:34:46Z` First full-GC WARN under heap pressure (4152ms) appears in notification-service logs; old-gen heap usage over 90%. [F15]
- `2026-03-23T19:25:00Z` memory_pct reaches max 98.44. [F14]
- `2026-03-23T19:49:28Z` Full-GC pause escalates to 6488ms; GC pause duration grew monotonically toward the kill. [F15]
- `2026-03-23T19:59:21Z` Third OOMKilled restart (exit 137) of notification-service-7d9f logged as ERROR; restart counter steps 2→3; lag spikes (19:30Z avg 3069.83/max 18137.93; 20:00Z avg 6004.47/max 14213.61). [F8, F9, F13, F15]
- `2026-03-23T20:15:00Z` Memory reset to avg 47.2 after the restart (sawtooth reset); lag returns to baseline (avg 57.18 by 20:30Z), confirming lag as a downstream consequence. [F14, F13, F12]

## Recommended actions
- **[immediate]** Mitigate the immediate recurrence risk by rolling back notification-service from v3.8.0 (dep-5402) to the prior build, or by disabling the in-memory template cache (feature flag / config) if a rollback is not feasible — this is the operative change id dep-5402. -- The only change touching notification-service in the window and the only change adding unbounded in-memory growth is dep-5402; the memory ramp, GC-pressure logs and daily ~24h OOM cycle all begin after it, and it was never rolled back. [F2, F6, F7, F8, F15]
- **[immediate]** If rollback must be staged, set an interim container memory limit increase / raise the JVM max heap on notification-service as a stopgap, and enable alerting on memory_pct > 85% and on full-GC pause > 3s to catch the next ramp before the ~24h OOM. -- Memory climbs in a predictable sawtooth from ~48% to ~95-98% over roughly 24h with escalating full-GC pauses, so a higher ceiling and earlier alerting buys time while the fix is prepared; this is mitigation, not a root-cause fix. [F7, F14, F15]
- **[immediate]** Do NOT act on the Kafka retention change (tc-004) or pursue a replay mitigation: revert nothing in Kafka for this incident, and correct the incident narrative that the maintenance/replay caused the OOM. -- The retention change only truncated segments (no forced replay, no offset reset), produced no lag or throughput response at 18:00Z, and both the memory ramp and the first two restarts predate it by ~2 days. [F1, F6, F13, F14, F17]
- **[short_term]** Fix the cache properly in a new notification-service release: bound the template cache with a size limit plus eviction (e.g. Caffeine with maximumSize/expireAfterAccess or weak keys), and add a per-process heap/old-gen metric so cache growth is attributable. -- The root-cause mechanism is a ConcurrentHashMap keyed by template+locale+userSegment with no eviction or bound, and no byte-level heap attribution exists today (only memory_pct %), so the fix and its verification both require a bounded cache and better instrumentation. [F2, F7, F15]
- **[short_term]** Verify the fix by watching notification-service for at least 48-72h after deploy: confirm memory_pct no longer shows the monotonic ~24h sawtooth, restart count stays flat at its current value, full-GC WARN lines stop recurring, and consumer lag no longer spikes in the OOM bucket. -- The signature to disprove is the daily sawtooth to ~95-98% with an OOM in the same 2h bucket; a full 72h observation covers more than the three ~24h cycles observed so far. [F7, F8, F9, F10, F15]
- **[short_term]** Close out the alternatives: check node/instance sizing and Kafka broker container limits for the 2026-03-21→23 window (not visible in the deploy feed), and spot-check other co-tenant consumers (settlement-, payment-) for a similar memory ramp to exclude a shared-component ceiling change. -- H4 (shared-component contention, 0.10) cannot be fully excluded because node instance-type and broker-count changes may be invisible in the deploy feed, and only order-service was checked (flat at ~59.5-60.5%). [F4, F11]
- **[long_term]** Add a memory-growth/leak gate to CI for notification-service (heap-ramp soak test or cache-size bound check) and require bounded-collection review for caches added to long-lived services. -- A single PR ('Add in-memory template cache', dep-5402) introduced an unbounded collection that took ~2 days to manifest as three SEV-3 restarts; a soak/bound check would catch this class of regression pre-merge. [F2, F6, F7]

## Data gaps
- No JVM heap/RSS byte-level gauges exist for notification-service — only a memory_pct percentage — so the ramp cannot be attributed to the template cache specifically, only that old-gen pressure grows toward the kill (GC WARN lines).
- Config-change history for notification-service itself was not retrievable (budget_exhausted), so notification-specific JVM heap / container memory limits or Kafka consumer-group/offset settings cannot be confirmed unchanged.
- Node/instance sizing and Kafka broker-count changes may be invisible in the deploy feed (get_deployments returned only 2 deploys across all services for the 7-day window), limiting the 'what changed' answer to deploys plus config keys.
- Kafka metrics (cpu_pct/messages_in_per_s/under_replicated_partitions) and kafka memory were not queried (and kafka exposes no memory_pct), so broker-side maintenance activity cannot be correlated with the start of the ramp.
- No broker-side/consumer-group telemetry exists (no per-consumer fetch, partition-assignment, or rebalance metrics), so a broker-side rebalance at 18:00Z cannot be confirmed or excluded — only that no such event is logged service-side, and the log stream may simply not emit rebalance events.
- No telemetry for the external email-provider, so the reported ~25 min email delay could not be measured directly — it is observed only via notification-service lag and restarts.
- Only order-service was checked for co-tenant memory behavior; settlement-service and payment-service memory was not compared, so the scope of any shared-component effect remains unverified.
- Historical incident search for shared-node memory pressure / Kafka broker container limits returned budget_exhausted, so no prior pattern could be checked.

## What would change this conclusion
- A service-scoped config or deployment history showing notification-service JVM heap or container memory limit was reduced (or its consumer concurrency raised) in the window would shift weight toward H4/shared-component contention.
- Evidence of a concurrent memory ramp in sibling consumers (settlement-service, payment-service) or a node instance-type/broker-count change in the window would indicate a shared-component cause rather than an application-level cache leak.
- Log evidence or broker-side telemetry showing a consumer-group rebalance or partition reassignment storm at or near 18:00Z — or a demonstration that the log stream suppresses rebalance events — would revive H3.
- A recorded consumer-group offset reset or forced re-read of retained segments at 18:00Z would support the reporter's replay theory (H2).
- Inspection of the v3.8.0 source showing the template cache is already bounded/evicting, or heap-profiler data attributing the old-gen growth to something other than the cache, would overturn H1.
- An email-provider degradation of sufficient duration and magnitude, with a flat consumer lag but a growing internal send queue, would support H5.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: Lowering Kafka retention truncates old segments rather than re-reading them; no 
- fact demoted to assumption: The Kafka maintenance change and the OOM correlate with near-full heap (already 

## Investigation stats
- latency 144.43s, LLM calls 14, tokens 87764 (in 69500 / out 18264)
- tool calls 23 (3 failed, 0 partial, 0 recovered by retry), findings 17/18 verified
