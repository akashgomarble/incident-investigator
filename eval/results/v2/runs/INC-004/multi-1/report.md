# Incident investigation report: INC-004
*System: multi_agent | outcome: **root_cause_identified** | ended: no_new_evidence_paths*

## Summary
notification-service is OOMKilled once per day at ~22:00Z by a monotonic old-generation heap ramp that reaches the container cgroup limit, and the only growing, never-evicting collection in the logs is the application's own TemplateCache (evictions=0) introduced by v3.8.0. The mechanical failure chain is verified to a high standard — smooth near-linear memory ramp to ~98% then reset to ~47%, multi-second full-GC old-gen bursts immediately pre-kill, exit 137, and zero queue/batch/deserialization signatures — and the Kafka retention change (cfg-2410) is decisively ruled out on timing, since identical daily OOMs occurred on 03-21 and 03-22, before it existed. The top hypothesis (H1, unbounded TemplateCache leak from v3.8.0) leads clearly and is the best-supported explanation, but it is NOT adequate for a confident root cause: its first hop — that the cache is unbounded AND is the origin of the leak — rests on changelog silence rather than code, and the single decisive test separating H1 from H4 (identical daily OOMs predating the 03-21 deploy) is unanswerable because all telemetry bottoms out at the 2026-03-20 retention floor. With confidences fixed at 0.50/0.18/0.05 and the origin axis unmovable on available data, I report inconclusive with the mitigation and the one discriminating measurement to run.

## Probable root cause
**Unbounded template-cache memory leak introduced by notification-service v3.8.0** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.50, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded template-cache memory leak introduced by notification-service v3.8.0 | bad_deployment | notification-service | dep-5402 | 0.50 (medium) | active |
| 2 | H4: Pre-existing unbounded in-memory structure in notification-service predating v3.8.0 | bad_deployment | notification-service | null | 0.18 (low) | active |
| 3 | H3: Node-pool / shared-infrastructure memory contention on the container's host | infrastructure_failure | node pool hosting notification-service | null | 0.05 (low) | active |
| 4 | H2: Kafka retention reduction (cfg-2410) triggered a consumer replay buffered in memory | config_change | shared Kafka cluster (platform-team config) | cfg-2410 | 0.02 (low) | rejected |

### H1: Unbounded template-cache memory leak introduced by notification-service v3.8.0
notification-service v3.8.0 (dep-5402, 2026-03-21T00:00:00Z) added an in-memory TemplateCache (ConcurrentHashMap keyed by template+locale+userSegment) with no size cap, TTL or eviction. It grows monotonically, retaining heap in the JVM old generation until the container cgroup limit is reached, producing one OOMKill (~22:00 UTC) per day. The Kafka retention change is coincidental.

**Causal chain**
1. v3.8.0 deploys an in-memory template cache for email rendering on 2026-03-21T00:00:00Z; the changelog names key structure but no size cap, TTL or eviction policy [F1, F20]
2. That cache is observed growing monotonically with ZERO evictions ever recorded (~45k entries appended per ~5h45m, reaching 188k-197k entries pre-OOM), and it is the only growing collection in the logs [F27, F28, F30]
3. Cache size resets with each restart and re-climbs from zero on the same daily cadence as the OOM, and keeps growing at the same rate after the 18:00Z Kafka change as before it [F29]
4. The retained set saturates the old generation: 'heap usage over 90 (old gen)' warnings with multi-second full-GC pauses burst immediately before each kill [F14, F15, F18, F31]
5. Heap reaches ~98% of the container limit and the cgroup OOMKills the JVM (exit 137), then memory resets to ~47% [F12, F13, F10]
6. Restart loop causes consumer lag spikes (lag follows the restart, not precedes it) and delayed trade-confirmation emails [F11]

**Supporting evidence**
  - `F1` ✔ Deployment history for the last 7 days (all services) contains exactly 2 deployments: notification-service v3.8.0 on 2026-03-21T00:00:00Z ('Add in-memory template cache for email rendering', author s.menon, status live, rolled_back_at null) and auth-service v7.1.4 on 2026-03-22T13:00:00Z ('bump jackson', status live).
  - `F20` ✔ dep-5402 (notification-service v3.8.0, 'Add in-memory template cache for email rendering', author s.menon, deployed 2026-03-21T00:00:00Z) records exactly two change entries: 'TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment' and 'render path uses cache'. The changelog names no maximum size, no TTL/expiry and no eviction policy for the cache, so the only field it is described as retaining is an unbounded map keyed by template+locale+userSegment.
  - `F22` ✔ No configuration or feature-flag key that bounds the template cache exists anywhere in the last ~60 days: get_config_changes over 2026-01-20T00:00:00Z–2026-03-23T23:59:00Z for ALL services returned count 1 and it was only the Kafka change 'log.retention.ms' (cfg-2410). There is no cache-size, cache-TTL, cache-eviction or feature-flag entry for notification-service.
  - `F24` ✔ Because no flag/config gates the cache and the changelog exposes no TTL/size/eviction control, dep-5402 is not reversible by configuration toggle — the only documented reversal path is a redeploy/rollback of v3.8.0, and no such rollback is recorded (rolled_back_at null at latest data 2026-03-23T23:59:00Z).
  - `F27` ✔ TemplateCache growth is logged and is monotonically growing with zero evictions across both requested pre-OOM windows. Full window shows sizes 51750 -> 103500 -> 155250 -> 196650 entries (evictions=0) on 03-21/03-22 and 49500 -> 99000 -> 148500 -> 188100 (evictions=0) on 03-23, i.e. ~45000 entries appended per ~5h45m interval with no eviction ever recorded.
  - `F28` ✘ unverified In the 2026-03-22T21:34-21:59Z pre-OOM window, TemplateCache stood at 196650 entries (evictions=0) at 21:51:17Z - 8 minutes before the 21:59:47Z OOMKill - and in the 2026-03-23T19:33-19:59Z window the last pre-OOM cache sample was 188100 entries at 18:54:31Z before the cache reset to 9000 at 21:00:44Z. The retained set in both windows is thus dominated by a template cache that only grows and never evicts.
  - `F29` ✘ unverified The cache size resets with each restart and re-climbs from zero on the same daily cadence as the OOM: after the 03-22 OOM the next sample is 51750 entries at 04:45:31Z, and after the 03-23 OOM it is 9000 entries at 21:00:44Z / 18000 at 22:00:34Z. Growth continues after the Kafka retention change at 18:00Z (188100 entries at 18:54:31Z) at the same rate as before it, so the cache growth is not gated on the Kafka replay.
  - `F30` ✔ No message/queue/batch/deserialization-shaped structures appear in notification-service logs: a search for 'queue' across 2026-03-16 to 2026-03-23 returned total_matched 0 (truncated false), consistent with the team's earlier 'deserial' total_matched 0. The only growing collection in the logs is the template cache.
  - `F31` ✔ The pre-OOM heap pressure in both requested windows is old-generation and escalates with the cache growth: 2026-03-23T19:33-19:59Z shows 'heap usage 1% over 90 (old gen), full GC took 6388ms' (19:33:14Z) and 6488ms at 19:49:28Z, and 2026-03-22T21:34-21:59Z shows 1050ms at 21:34:52Z and 7735ms at 21:56:37Z, ending in the exit-137 OOMKilled line. This confirms the retained set being exhausted is the old generation, matching the template/cache-shaped object pattern rather than a young-gen batch burst.
  - `F10` ✔ Memory growth is a steady near-linear ramp, not a sharp spike: from the post-restart baseline ~47-48% it climbs ~4.5-5% per 2h bucket (e.g. 2026-03-23: 00:00 53%, 08:00 71.5%, 16:00 90.25%) and peaks at ~98% right before the OOM reset. The 20-minute-resolution view of 2026-03-23 shows the same smooth climb (16:00 88.1% → 19:40 96.7%) then a drop to 46.72% at 20:00.
  - `F12` ✔ The ramp reaches ~98% of memory before each OOM and resets to ~47% after, which is the classic container-cgroup-limit signature; no node-level metric is available (kafka node metric query could not execute), so node-level pressure is not directly excluded.
  - `F13` ✔ Each OOM terminates the same identical log line: the container notification-service-7d9f is OOMKilled with exit code 137, at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z respectively.
  - `F14` ✔ The pre-OOM error signature is old-gen heap exhaustion with multi-second full GCs: warnings of the form 'heap usage N% over 90 (old gen), full GC took N ms' with pauses of 1050-8220 ms occur in every OOM window (03-21 from 22:32:06Z: 6262/2571/7457 ms; 03-22 from 21:34:52Z: 1050/7735 ms; 03-23 from 19:33:14Z: 6388/6488 ms).
  - `F15` ✔ The GC-thrash warnings form a dense burst only shortly before each OOM: for 2026-03-22 all 9 'heap usage ... over 90 (old gen)' warnings span 20:35:10Z-21:56:37Z, immediately preceding the 21:59:47Z kill, while for 2026-03-21 the burst runs from 21:37:01Z onward ahead of the 22:59:50Z kill.
  - `F18` ✔ The GC-thrash burst is event-correlated, not a constant condition: in the 2026-03-21 window the 13 heap-over-90 warnings have first_seen 21:37:01Z (~1 h 22 min before the OOM), while earlier hours of 03-21 contain only unrelated single events (clock drift, TLS cipher, one GC pause 'exceeded 200ms target').
  - `F16` ✔ No message-volume/replay/deserialization signature exists in notification-service logs for the 7-day window: a search for 'deserial' returned total_matched 0, and each OOM-window warn/error set contains only heap/GC warnings plus the OOMKilled line (03-21: 4 matches; 03-23: 3 matches) with no batch, backlog or parse errors.
  - `F19` ✔ Interpretation: the identical OOM signature across all three days (steady heap ramp to >90% old gen, escalating multi-second full-GC pauses, exit 137, and absent deserialization/backlog/batch errors) matches a memory leak/retention in the JVM old generation rather than a Kafka replay burst; a replay would leave batch, deserialization or back-pressure log lines, which are absent.
  - `F7` ✔ Exactly three OOMKilled events occurred in the last 7 days, one per day at ~22:00 UTC: 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z, 2026-03-23T19:59:21Z. All three are the same container notification-service-7d9f, exit code 137.
  - `F36` ✔ The cumulative restart series starts stepping only on the deploy day: it is 0 through the '2026-03-21T20:00:00Z' bucket and first reaches max 1 at '2026-03-21T22:00:00Z' (avg 0.5), i.e. the earliest retained OOMKilled restart (2026-03-21T22:59:50Z, container notification-service-7d9f, exit 137, per F7/F8) falls ~23 h after the 2026-03-21T00:00:00Z v3.8.0 deploy. No bucket before 2026-03-21T22:00 exceeds 0.
**Contradicting evidence**
  - `F1` ✔ Deployment history for the last 7 days (all services) contains exactly 2 deployments: notification-service v3.8.0 on 2026-03-21T00:00:00Z ('Add in-memory template cache for email rendering', author s.menon, status live, rolled_back_at null) and auth-service v7.1.4 on 2026-03-22T13:00:00Z ('bump jackson', status live).
  - `F7` ✔ Exactly three OOMKilled events occurred in the last 7 days, one per day at ~22:00 UTC: 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z, 2026-03-23T19:59:21Z. All three are the same container notification-service-7d9f, exit code 137.
  - `F36` ✔ The cumulative restart series starts stepping only on the deploy day: it is 0 through the '2026-03-21T20:00:00Z' bucket and first reaches max 1 at '2026-03-21T22:00:00Z' (avg 0.5), i.e. the earliest retained OOMKilled restart (2026-03-21T22:59:50Z, container notification-service-7d9f, exit 137, per F7/F8) falls ~23 h after the 2026-03-21T00:00:00Z v3.8.0 deploy. No bucket before 2026-03-21T22:00 exceeds 0.
  - `F37` ✔ The decisive H1-vs-H4 test is unanswerable with the available data and must be recorded as a gap, not as evidence: because the metric/log floor is 2026-03-20T00:00:00Z, the observation that restarts begin on the deploy day is equally consistent with (a) H1, the v3.8.0 cache introducing the leak, and (b) H4, a pre-existing leak whose earlier daily OOMs simply predate the retained window. No pre-2026-03-20 restart timestamp or exit code can be produced.
**Assumptions**
  - The changelog's silence on size/TTL/eviction genuinely means unbounded (no code diff, heap dump or class histogram is available; F26/F37)
  - The cache is the dominant contributor to the old-gen retained set, not merely correlated growth; no object-level (dominator/histogram) telemetry exists
  - Onset attribution: first retained OOM is ~23h after the deploy (F36); no pre-deploy control period exists (F37), so 'introduced the leak' cannot be proven causally, only shown consistent
**Rejected because:** null

### H4: Pre-existing unbounded in-memory structure in notification-service predating v3.8.0
notification-service carries a long-standing memory-retention defect (queue/batch/other structure) that predates the 7-day window; the daily OOM pattern is old, and v3.8.0 and the Kafka change are unrelated. The observed old-gen ramp reflects that pre-existing defect, not the new cache.

**Causal chain**
1. A pre-existing unbounded queue/batch/collection accumulates retained heap during normal email throughput, causing the same steady old-gen ramp *(assumed)*
2. Heap ramps smoothly to >90% old gen with escalating full-GC pauses and is OOMKilled daily (exit 137) [F10, F14, F12, F13]
3. Restart causes lag spike and delayed emails [F11]

**Supporting evidence**
  - `F10` ✔ Memory growth is a steady near-linear ramp, not a sharp spike: from the post-restart baseline ~47-48% it climbs ~4.5-5% per 2h bucket (e.g. 2026-03-23: 00:00 53%, 08:00 71.5%, 16:00 90.25%) and peaks at ~98% right before the OOM reset. The 20-minute-resolution view of 2026-03-23 shows the same smooth climb (16:00 88.1% → 19:40 96.7%) then a drop to 46.72% at 20:00.
  - `F14` ✔ The pre-OOM error signature is old-gen heap exhaustion with multi-second full GCs: warnings of the form 'heap usage N% over 90 (old gen), full GC took N ms' with pauses of 1050-8220 ms occur in every OOM window (03-21 from 22:32:06Z: 6262/2571/7457 ms; 03-22 from 21:34:52Z: 1050/7735 ms; 03-23 from 19:33:14Z: 6388/6488 ms).
  - `F37` ✔ The decisive H1-vs-H4 test is unanswerable with the available data and must be recorded as a gap, not as evidence: because the metric/log floor is 2026-03-20T00:00:00Z, the observation that restarts begin on the deploy day is equally consistent with (a) H1, the v3.8.0 cache introducing the leak, and (b) H4, a pre-existing leak whose earlier daily OOMs simply predate the retained window. No pre-2026-03-20 restart timestamp or exit code can be produced.
**Contradicting evidence**
  - `F16` ✔ No message-volume/replay/deserialization signature exists in notification-service logs for the 7-day window: a search for 'deserial' returned total_matched 0, and each OOM-window warn/error set contains only heap/GC warnings plus the OOMKilled line (03-21: 4 matches; 03-23: 3 matches) with no batch, backlog or parse errors.
  - `F30` ✔ No message/queue/batch/deserialization-shaped structures appear in notification-service logs: a search for 'queue' across 2026-03-16 to 2026-03-23 returned total_matched 0 (truncated false), consistent with the team's earlier 'deserial' total_matched 0. The only growing collection in the logs is the template cache.
  - `F27` ✔ TemplateCache growth is logged and is monotonically growing with zero evictions across both requested pre-OOM windows. Full window shows sizes 51750 -> 103500 -> 155250 -> 196650 entries (evictions=0) on 03-21/03-22 and 49500 -> 99000 -> 148500 -> 188100 (evictions=0) on 03-23, i.e. ~45000 entries appended per ~5h45m interval with no eviction ever recorded.
  - `F28` ✘ unverified In the 2026-03-22T21:34-21:59Z pre-OOM window, TemplateCache stood at 196650 entries (evictions=0) at 21:51:17Z - 8 minutes before the 21:59:47Z OOMKill - and in the 2026-03-23T19:33-19:59Z window the last pre-OOM cache sample was 188100 entries at 18:54:31Z before the cache reset to 9000 at 21:00:44Z. The retained set in both windows is thus dominated by a template cache that only grows and never evicts.
  - `F29` ✘ unverified The cache size resets with each restart and re-climbs from zero on the same daily cadence as the OOM: after the 03-22 OOM the next sample is 51750 entries at 04:45:31Z, and after the 03-23 OOM it is 9000 entries at 21:00:44Z / 18000 at 22:00:34Z. Growth continues after the Kafka retention change at 18:00Z (188100 entries at 18:54:31Z) at the same rate as before it, so the cache growth is not gated on the Kafka replay.
  - `F20` ✔ dep-5402 (notification-service v3.8.0, 'Add in-memory template cache for email rendering', author s.menon, deployed 2026-03-21T00:00:00Z) records exactly two change entries: 'TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment' and 'render path uses cache'. The changelog names no maximum size, no TTL/expiry and no eviction policy for the cache, so the only field it is described as retaining is an unbounded map keyed by template+locale+userSegment.
  - `F35` ✔ Within the retained window the restart counter is 0 for the entire pre-deploy day 2026-03-20: a zoomed query (bucket_seconds 7500) over 2026-03-20T00:00:00Z-2026-03-21T00:00:00Z shows avg 0.0/max 0.0 at every bucket up to '2026-03-20T22:55:00Z' (summary min 0.0, max 0.0). A restart already existed on 2026-03-20 would have made max >= 1.
**Assumptions**
  - The defect exists in pre-v3.8.0 code (unverifiable: log/metric retention floor is 2026-03-20, so no pre-deploy behavior can be observed)
  - The assumed queue/batch structure exists despite no queue/batch/deserialization log lines anywhere in the retained window
**Rejected because:** null

### H3: Node-pool / shared-infrastructure memory contention on the container's host
notification-service is a victim of node-level memory pressure or a noisy neighbor; its cgroup hits its limit because the host is starved. The OOM is an infrastructure symptom, not an application leak.

**Causal chain**
1. Node-level memory pressure or a co-tenant workload reduces memory available to the notification-service cgroup *(assumed)*
2. Container memory reaches ~98% of its limit and is OOMKilled (exit 137) [F12, F13]
3. Repeated restarts cause consumer lag spikes and email delay [F11]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ Memory growth is a steady near-linear ramp, not a sharp spike: from the post-restart baseline ~47-48% it climbs ~4.5-5% per 2h bucket (e.g. 2026-03-23: 00:00 53%, 08:00 71.5%, 16:00 90.25%) and peaks at ~98% right before the OOM reset. The 20-minute-resolution view of 2026-03-23 shows the same smooth climb (16:00 88.1% → 19:40 96.7%) then a drop to 46.72% at 20:00.
  - `F14` ✔ The pre-OOM error signature is old-gen heap exhaustion with multi-second full GCs: warnings of the form 'heap usage N% over 90 (old gen), full GC took N ms' with pauses of 1050-8220 ms occur in every OOM window (03-21 from 22:32:06Z: 6262/2571/7457 ms; 03-22 from 21:34:52Z: 1050/7735 ms; 03-23 from 19:33:14Z: 6388/6488 ms).
  - `F15` ✔ The GC-thrash warnings form a dense burst only shortly before each OOM: for 2026-03-22 all 9 'heap usage ... over 90 (old gen)' warnings span 20:35:10Z-21:56:37Z, immediately preceding the 21:59:47Z kill, while for 2026-03-21 the burst runs from 21:37:01Z onward ahead of the 22:59:50Z kill.
  - `F16` ✔ No message-volume/replay/deserialization signature exists in notification-service logs for the 7-day window: a search for 'deserial' returned total_matched 0, and each OOM-window warn/error set contains only heap/GC warnings plus the OOMKilled line (03-21: 4 matches; 03-23: 3 matches) with no batch, backlog or parse errors.
  - `F28` ✘ unverified In the 2026-03-22T21:34-21:59Z pre-OOM window, TemplateCache stood at 196650 entries (evictions=0) at 21:51:17Z - 8 minutes before the 21:59:47Z OOMKill - and in the 2026-03-23T19:33-19:59Z window the last pre-OOM cache sample was 188100 entries at 18:54:31Z before the cache reset to 9000 at 21:00:44Z. The retained set in both windows is thus dominated by a template cache that only grows and never evicts.
  - `F29` ✘ unverified The cache size resets with each restart and re-climbs from zero on the same daily cadence as the OOM: after the 03-22 OOM the next sample is 51750 entries at 04:45:31Z, and after the 03-23 OOM it is 9000 entries at 21:00:44Z / 18000 at 22:00:34Z. Growth continues after the Kafka retention change at 18:00Z (188100 entries at 18:54:31Z) at the same rate as before it, so the cache growth is not gated on the Kafka replay.
  - `F31` ✔ The pre-OOM heap pressure in both requested windows is old-generation and escalates with the cache growth: 2026-03-23T19:33-19:59Z shows 'heap usage 1% over 90 (old gen), full GC took 6388ms' (19:33:14Z) and 6488ms at 19:49:28Z, and 2026-03-22T21:34-21:59Z shows 1050ms at 21:34:52Z and 7735ms at 21:56:37Z, ending in the exit-137 OOMKilled line. This confirms the retained set being exhausted is the old generation, matching the template/cache-shaped object pattern rather than a young-gen batch burst.
**Assumptions**
  - Node-level memory metrics would show pressure in each pre-OOM window (no node/host metric is exposed; the kafka node metric query could not execute)
  - No co-tenant OOMs or restarts are reported, which a shared-node cause would tend to produce
**Rejected because:** null

### H2: Kafka retention reduction (cfg-2410) triggered a consumer replay buffered in memory
cfg-2410 reduced Kafka log retention 7d->3d at 2026-03-23T18:00:00Z, forcing notification-service's consumer to replay/reseek into a backlog that it buffered in memory, driving the JVM to its limit and OOMKilling it.

**Causal chain**
1. cfg-2410 changes Kafka log.retention.ms 7d->3d at 2026-03-23T18:00:00Z [F2]
2. Retention truncation forces notification-service's consumer to replay backlog *(assumed)*
3. Replayed messages are buffered/deserialized in memory, pushing the heap to the container limit *(assumed)*
4. Container is OOMKilled at 19:59:21Z and emails are delayed [F13, F9]

**Supporting evidence**
  - `F2` ✔ The only config change in the last 7 days is on the Kafka cluster: cfg-2410 changed log.retention.ms from 604800000 (7 days) to 259200000 (3 days) at 2026-03-23T18:00:00Z, authored by platform-team, described as 'Kafka maintenance: reduce retention to 3 days'.
  - `F4` ✔ notification-service depends on the shared Kafka cluster ('has_telemetry':true,'shared':true) via a 'queue-consume' edge; its only other dependency is the external email-provider (has_telemetry false). Kafka is also consumed by settlement-service and produced to by order-service and payment-service, so a Kafka-side change can affect several services at once.
  - `F6` ✔ The Kafka retention reduction at 2026-03-23T18:00:00Z and the notification-service cache deploy at 2026-03-21T00:00:00Z are both change candidates, but the cache deploy predates the maintenance by ~2 days while the report describes OOMs occurring across the week; only the Kafka config change is same-day as the latest restart. No rollback is recorded for either change.
  - `F9` ✔ There is a mismatch between the reported event time (20:20:00Z) and the actual OOM log timestamp (19:59:21Z); the container OOMKilled ~21 minutes before the report time and memory already reset to ~47% by the 20:00 bucket.
**Contradicting evidence**
  - `F7` ✔ Exactly three OOMKilled events occurred in the last 7 days, one per day at ~22:00 UTC: 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z, 2026-03-23T19:59:21Z. All three are the same container notification-service-7d9f, exit code 137.
  - `F8` ✔ The restarts metric is cumulative and steps 0→1 at the 2026-03-21T22:00 bucket, 1→2 at 2026-03-22T22:00, and 2→3 at 2026-03-23T20:00, confirming exactly 3 restarts and that the 20:20 event is the third of the recurring daily pattern.
  - `F10` ✔ Memory growth is a steady near-linear ramp, not a sharp spike: from the post-restart baseline ~47-48% it climbs ~4.5-5% per 2h bucket (e.g. 2026-03-23: 00:00 53%, 08:00 71.5%, 16:00 90.25%) and peaks at ~98% right before the OOM reset. The 20-minute-resolution view of 2026-03-23 shows the same smooth climb (16:00 88.1% → 19:40 96.7%) then a drop to 46.72% at 20:00.
  - `F11` ✔ Consumer lag does NOT precede the OOM/restart: baseline lag is ~55-65 messages, and the only spikes (avg 790-2273, max ~14k-18k) fall in the SAME 2h bucket as, or the bucket immediately after, each restart (2026-03-21T22:00 max 17161; 2026-03-22T20:00 max 17611 and 22:00 avg 1589; 2026-03-23T18:00 max 18137 and 20:00 avg 1542), returning to ~57 by the next bucket.
  - `F15` ✔ The GC-thrash warnings form a dense burst only shortly before each OOM: for 2026-03-22 all 9 'heap usage ... over 90 (old gen)' warnings span 20:35:10Z-21:56:37Z, immediately preceding the 21:59:47Z kill, while for 2026-03-21 the burst runs from 21:37:01Z onward ahead of the 22:59:50Z kill.
  - `F16` ✔ No message-volume/replay/deserialization signature exists in notification-service logs for the 7-day window: a search for 'deserial' returned total_matched 0, and each OOM-window warn/error set contains only heap/GC warnings plus the OOMKilled line (03-21: 4 matches; 03-23: 3 matches) with no batch, backlog or parse errors.
  - `F17` ✔ The instrumented shared-Kafka services show no warnings in the 2026-03-23 18:00-21:00Z window coinciding with the notification-service OOM: settlement-service returned total_matched 0 and order-service returned total_matched 0 (both truncated:false).
  - `F29` ✘ unverified The cache size resets with each restart and re-climbs from zero on the same daily cadence as the OOM: after the 03-22 OOM the next sample is 51750 entries at 04:45:31Z, and after the 03-23 OOM it is 9000 entries at 21:00:44Z / 18000 at 22:00:34Z. Growth continues after the Kafka retention change at 18:00Z (188100 entries at 18:54:31Z) at the same rate as before it, so the cache growth is not gated on the Kafka replay.
  - `F30` ✔ No message/queue/batch/deserialization-shaped structures appear in notification-service logs: a search for 'queue' across 2026-03-16 to 2026-03-23 returned total_matched 0 (truncated false), consistent with the team's earlier 'deserial' total_matched 0. The only growing collection in the logs is the template cache.
  - `F35` ✔ Within the retained window the restart counter is 0 for the entire pre-deploy day 2026-03-20: a zoomed query (bucket_seconds 7500) over 2026-03-20T00:00:00Z-2026-03-21T00:00:00Z shows avg 0.0/max 0.0 at every bucket up to '2026-03-20T22:55:00Z' (summary min 0.0, max 0.0). A restart already existed on 2026-03-20 would have made max >= 1.
**Assumptions**
  - Retention reduction actually forced a replay or offset reset for the consumer group (no offset/topic-state evidence found)
**Rejected because:** Decisive timing and mechanism contradictions: identical daily ~22:00 OOMs occur on 03-21 and 03-22, one and two days BEFORE cfg-2410 existed (F7,F8,F35), so the maintenance cannot be the origin; lag spikes follow the restarts rather than precede them (F11); the cache keeps growing at the same rate after 18:00Z as before it, showing growth is not replay-gated (F29); zero deserialization/batch/queue/backlog log lines exist, which a replay would produce (F16,F30); co-consumers of the same cluster were clean in the incident window (F17); and the ramp is smooth, not a replay burst (F10).

## Facts (verified observations)
- Exactly three OOMKilled events occurred in the retained window, one per day at ~22:00Z, all on the same container notification-service-7d9f with exit code 137: 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z, 2026-03-23T19:59:21Z. [F7, F8, F13]
- The restart counter is 0 for the entire pre-deploy day 2026-03-20 and first steps to 1 at the 2026-03-21T22:00Z bucket, ~23h after the 2026-03-21T00:00:00Z v3.8.0 deploy. [F35, F36]
- Memory growth is a steady near-linear ramp, not a spike: ~4.5-5% per 2h bucket, peaking at ~98% just before each OOM and resetting to ~47% after — the classic container-cgroup-limit signature. [F10]
- The pre-OOM error signature is old-generation heap exhaustion with multi-second full GCs ('heap usage N% over 90 (old gen), full GC took 1050-8220 ms'), forming a dense burst only shortly before each kill. [F14, F15, F18, F31]
- No message-volume/replay/deserialization/queue/batch signature exists in notification-service logs for the 7-day window ('deserial' and 'queue' searches both returned total_matched 0); the only growing collection logged is the template cache. [F16, F30]
- TemplateCache is logged as monotonically growing with zero evictions across the pre-OOM windows: e.g. 51750 -> 103500 -> 155250 -> 196650 entries on 03-21/03-22 and 49500 -> 99000 -> 148500 -> 188100 on 03-23, ~45000 entries per ~5h45m, evictions=0 throughout. [F27]
- dep-5402 (notification-service v3.8.0, deployed 2026-03-21T00:00:00Z, 'Add in-memory template cache for email rendering') describes a ConcurrentHashMap keyed by template+locale+userSegment with no maximum size, TTL or eviction mentioned; it is still live and never rolled back. [F20, F21, F1]
- No configuration, feature flag or resource-limit entry bounding the template cache exists in ~60 days of change data; the only config change is the Kafka log.retention.ms change (cfg-2410), and dep-5402 has no config-gated reversal path. [F22]
- The Kafka retention reduction (cfg-2410, 7d->3d at 2026-03-23T18:00:00Z) postdates the first two identical OOMs by one and two days, and consumer lag spikes follow the restarts rather than precede them, so it cannot be the origin of the restart pattern. [F2, F7, F8, F11]
- The instrumented shared-Kafka co-consumers (settlement-service, order-service) showed zero warnings in the 2026-03-23 18:00-21:00Z window coinciding with the OOM, and no replay-driven log evidence exists. [F17]
- notification-service exposes no heap/generation composition metric (heap_used_bytes not_found); available metrics are only consumer_lag, cpu_pct, error_rate, latency_p99_ms, memory_pct, request_rate, restarts. [F26]
- All telemetry bottoms out at a 2026-03-20T00:00:00Z retention floor, confirmed independently by the restarts metric, memory_pct and OOMKilled/ERROR log searches, so the decisive pre-deploy test (identical daily OOMs before 2026-03-21T00:00Z) cannot be answered. [F32, F33, F34]
- Historical notification-service incidents are lag/rebalance incidents (HIST-104, HIST-112), not OOM; no precedent matching an unbounded in-memory cache leak was found (HIST-107 and HIST-115 are unrelated). [F5, F25]

## Assumptions (not verified)
- That the dep-5402 changelog's silence on size/TTL/eviction genuinely means the cache is unbounded. No code diff, heap dump or class histogram is available (F20, F26); 'unbounded' is inferred from absence of any bound being named, not read from code.
- That the TemplateCache is the dominant contributor to the old-generation retained set, not merely correlated with it. There is no object-level (dominator/histogram) telemetry to confirm retained-set composition; the inference rests on application log lines (F27 vs F16/F30).
- That the ~23h onset (F36) reflects the deploy introducing the leak, rather than a pre-existing daily OOM pattern that simply predates the retained window (F37). This is the H1-vs-H4 axis and is assumed, not proven.
- That no container memory request/limit reduction occurred outside the tracked deployment/config pipeline; absence of a limit change in recorded data (F23) does not fully prove the limit is unchanged.
- That the reported event time (20:20:00Z) maps to the actual OOM log at 19:59:21Z, leaving a 21-minute discrepancy unexplained (F9).
- That the trade-confirmation email delay (~25 min) is attributable to notification-service restart/lag behavior; the external email-provider has no telemetry, so the mail-side delay cannot be corroborated.
- That node-level memory pressure was not a contributing cause; no node/host metric was exposed, so this is assumed from the container-scoped 98%->47% reset pattern (F12).
- (demoted from facts: no verified observation backs it) No node-level memory/pressure metric is available (the kafka node metric query could not execute), so node-level contention is not directly excluded.

## Timeline
- `2026-03-20T00:00:00Z` Telemetry retention floor: no metrics or logs exist before this time, limiting all historical comparisons (F32, F33, F34, F37). [F32, F33, F34, F37]
- `2026-03-20T00:00:00Z-2026-03-21T00:00:00Z` Pre-deploy day: restart counter is 0 at every bucket (min 0.0, max 0.0), no restart before the deploy day in retained data (F35). [F35]
- `2026-03-21T00:00:00Z` Deployment dep-5402: notification-service v3.8.0 'Add in-memory template cache for email rendering' goes live (never rolled back) — adds an unbounded ConcurrentHashMap template cache per the changelog (F1, F20, F21). [F1, F20, F21]
- `2026-03-21T21:37:01Z` GC-thrash burst begins: dense 'heap usage over 90 (old gen)' warnings ahead of the first kill (F18, F15). [F18, F15]
- `2026-03-21T22:59:50Z` First OOMKill of container notification-service-7d9f, exit code 137 (~23h after deploy); memory had ramped near-linearly to ~98% (F7, F13, F36). [F7, F13, F36]
- `2026-03-22T13:00:00Z` Second deployment in window: auth-service v7.1.4 (bump jackson), unrelated to notification-service (F1). [F1]
- `2026-03-22T20:35:10Z-2026-03-22T21:56:37Z` Dense GC-thrash burst (9 old-gen warnings, pauses up to 7735 ms) immediately precedes the second kill; TemplateCache at 196650 entries, evictions=0 (F15, F14, F28). [F15, F14, F28]
- `2026-03-22T21:59:47Z` Second OOMKill, same container, exit 137 — identical signature to 03-21 (F7, F13, F8). [F7, F13, F8]
- `2026-03-23T18:00:00Z` Kafka config change cfg-2410 reduces log.retention.ms 7d->3d (platform-team maintenance) — same day as, but after, the first two identical OOMs (F2, F7, F8). [F2, F7, F8]
- `2026-03-23T18:54:31Z` TemplateCache at 188100 entries; growth continues at the same rate after the Kafka change, showing growth is not replay-gated (F29, F27). [F29, F27]
- `2026-03-23T19:33:14Z-2026-03-23T19:49:28Z` Pre-OOM old-gen exhaustion warnings with full GCs of 6388 ms and 6488 ms; memory ramps 88.1% -> 96.7% (F31, F10). [F31, F10]
- `2026-03-23T19:59:21Z` Third OOMKill, same container, exit 137; memory resets to ~46.72% by the 20:00 bucket (F7, F9, F10). [F7, F9, F10]
- `2026-03-23T20:20:00Z` Incident INC-004 reported (20:20:00Z) — ~21 minutes after the actual OOM log timestamp (F9). [F9]
- `2026-03-23T21:00:44Z` Post-restart TemplateCache resets and re-climbs from 9000 -> 18000 entries, beginning the next daily cycle (F29). [F29]
- `2026-03-23T23:59:00Z` Latest data: dep-5402 still live, rolled_back_at null; no rollback or cache-bounding config has been applied (F21, F24). [F21, F24]

## Recommended actions
- **[immediate]** Capture a heap dominator/histogram (or a jmap/live heap dump) from the running notification-service pod — ideally during a pre-OOM high-memory window — and report the top retained classes/sizes. -- This is the one decisive, obtainable measurement the Critic flagged as still unrun. It directly shows whether ConcurrentHashMap/TemplateCache dominates the old-gen retained set (H1 mechanism) or whether a queue/batch structure does (H4 mechanism), and it does NOT require pre-deploy history that bottoms out at the 2026-03-20 floor (F26, F16, F30, F37). [F26, F16, F30, F37]
- **[immediate]** Apply a safe immediate mitigation while the discriminating test runs: raise the notification-service container memory limit AND/OR lower the OOM frequency by reducing the cache footprint via a rolling restart cadence before the daily ~22:00Z saturation — do NOT change Kafka config (cfg-2410 is already ruled out and further fiddling risks unrelated consumers). -- The container saturates its cgroup limit once per day on a monotonic ramp (F10, F12); a limit increase buys runway and keeps trade-confirmation emails flowing while the origin question (H1 vs H4) is settled. Kafka-side changes are not indicated (H2 rejected: F7, F8, F11). [F10, F12, F7, F8, F11]
- **[short_term]** Correlate the reported 21-minute discrepancy and the latest restart against the Kafka maintenance timeline, and check notification-service consumer offset/topic state around 2026-03-23T18:00Z to definitively close out H2. -- cfg-2410 is already decisively rejected on timing (identical OOMs on 03-21/03-22 predate it) and mechanism (no replay/deserial evidence, co-consumers clean), but recording the offset state removes the last ambiguity and prevents re-litigating the maintenance hypothesis (F2, F7, F8, F16, F17). [F2, F7, F8, F16, F17]
- **[short_term]** Prepare a rollback of dep-5402 (v3.8.0) to the prior image as a controlled test if the dominator capture confirms the cache dominates the retained set, then observe whether the daily ~22:00Z OOM pattern disappears; if the pattern persists after rollback, H4 (pre-existing retention defect) is implicated instead. -- The Critic recommends a rolled-back-as-test path when the origin split cannot be resolved from history alone (F37). With no config/flag to bound the cache, redeploy is the only documented reversal path (F22, F24); rolling back is the direct experimental test of H1 vs H4. [F22, F24, F37, F21]
- **[short_term]** Correct the incident timeline in the postmortem: the third OOM was at 2026-03-23T19:59:21Z (exit 137), not 20:20:00Z, and the restart counter reached 3 at the 2026-03-23T20:00Z bucket. -- The 21-minute report/log discrepancy is unexplained and could mislead future 'correlate with maintenance' reasoning (F9). [F9]
- **[long_term]** Extend and instrument telemetry for notification-service before the next OOM cycle: retain history beyond the 2026-03-20 floor, add heap-by-generation and top-retained-class metrics, and export node/host memory pressure. -- The retention floor is exactly what makes H1 vs H4 (and H3) unanswerable; heap composition and node metrics would convert today's inferences into direct evidence and would also have excluded node contention (F12, F26, F32, F33, F34). [F12, F26, F32, F33, F34]
- **[long_term]** If rollback confirms the cache as the leak source, ship a bounded TemplateCache (max size + TTL + eviction, or a size-capped cache) and add a regression test/alarm on retained-cache size growth. -- Closes the defect pattern (unbounded map with evictions=0) and prevents recurrence; also addresses that the cache has no config-gated bound today (F20, F27). [F20, F27]

## Data gaps
- The decisive H1-vs-H4 test — whether identical daily ~22:00Z OOMKilled restarts predate the 2026-03-21T00:00Z v3.8.0 deploy — is unanswerable: all metrics and logs bottom out at a 2026-03-20T00:00:00Z retention floor, confirmed by three independent probes (F32, F33, F34, F37).
- No object-level retained-set evidence: heap_used_bytes is not_found, no heap-by-generation metric, no dominator/histogram, no heap/thread dump artifact. The 'cache dominates the old gen' inference rests only on application log lines (F26, F16, F30).
- The dep-5402 changelog exposes only a 2-line summary — no code diff, class source or cache-parameter values — so whether the cache is truly unbounded is inferred from the absence of any bound being named, not read from code (F20).
- Container memory request/limit values are not exposed by any queried tool; a limit change applied outside the tracked pipeline would not appear, so 'the limit is unchanged' is only ruled out for recorded change data (F23).
- No node/host memory metric was exposed and the kafka node metric query could not execute, so node-level pressure (H3) is not directly excluded; only container-scoped evidence is available (F12).
- The shared Kafka cluster logs for 2026-03-23 18:00-21:00Z and payment-service logs in the same window could not be queried (budget_exhausted), so cluster-side degradation and producer-side impact are unverified (F17 only covers settlement/order).
- Searches for 'batch' and 'dump' were never executed (budget exhausted), so absence of batch-shaped structures and of a dump artifact is not positively confirmed; only 'queue' (0) and 'deserial' (0) support it (F16, F30).
- No container restart/exit-code events for any predecessor revision exist; only container notification-service-7d9f appears, so restarts cannot be attributed to a pre-v3.8.0 image (F7).
- The external email-provider has no logs/metrics, so the ~25-minute trade-confirmation email delay cannot be corroborated from the mail side (GAP).

## What would change this conclusion
- A heap dominator/histogram or live dump showing ConcurrentHashMap/TemplateCache dominating the old-gen retained set would move H1 to adequately supported and support root_cause_identified.
- Conversely, a dominator capture showing a queue/batch/other structure dominating the retained set would favor H4 (pre-existing unbounded structure) and overturn H1 as the mechanism.
- Discovery of retained pre-2026-03-20 restart/OOM history showing identical daily ~22:00Z exit-137 kills before the deploy would refute H1's onset claim and support H4 (leak predates v3.8.0).
- Discovery of pre-deploy absence of any restart on the retained pre-deploy day (already seen for 03-20, F35) plus a longer retained pre-deploy baseline with zero OOMs would strengthen H1.
- A successful rollback of v3.8.0 that eliminates the daily OOM pattern would confirm H1; persistence of OOMs after rollback would implicate H4.
- Evidence of a container memory-limit reduction outside the tracked pipeline would shift the explanation from leak-origin toward resource-limit causation (reopens part of F23).
- Node-level or host memory metrics showing pressure in each pre-OOM window would raise H3 (node contention) from its unsupported state.
- Any replay/deserialization/offset-reset evidence for the notification-service consumer group tied to cfg-2410 would revive H2, which is currently rejected on timing and mechanism.

## Integrity warnings (set by code)
- reporter wrote outcome=inconclusive; overridden to root_cause_identified by rule (top hypothesis is above threshold and clearly ahead)
- fact demoted to assumption: No node-level memory/pressure metric is available (the kafka node metric query c

## Investigation stats
- latency 205.13s, LLM calls 32, tokens 247663 (in 209109 / out 38554)
- tool calls 45 (3 failed, 0 partial, 0 recovered by retry), findings 35/37 verified
