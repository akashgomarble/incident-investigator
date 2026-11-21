# Incident investigation report: INC-004
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The OOMKilled restarts of notification-service are caused by the unbounded in-memory template cache shipped in notification-service v3.8.0 (deployed 2026-03-21T00:00Z): the cache grows monotonically with zero evictions (51,750 → 196,650 entries, evictions=0 throughout), driving a slow old-gen heap climb that reaches the container limit roughly every ~24h. All three exit-137 restarts (03-21 22:59:50Z, 03-22 21:59:47Z, 03-23 19:59:21Z) follow that cadence, and the escalating full-GC "heap usage over 90 (old gen)" warnings begin 03-21T21:37Z — hours after the deploy and before any Kafka change. The reported Kafka retention change (03-23T18:00Z) is coincidental: two of the three restarts predate it, consumer lag was normal (<100) through 19:40Z and only spiked in the same bucket as the 19:59Z crash, and throughput/latency stayed flat across the maintenance and onset. Confidence is high (0.78), capped by the absence of a pre-03-21 baseline, no RSS/heap split, and possible gaps in config-change coverage.

## Probable root cause
**Unbounded in-memory template cache introduced by notification-service v3.8.0 causes old-gen heap exhaustion and OOMKilled restarts** -- bad_deployment in `notification-service`, triggered by `notification-service v3.8.0 (2026-03-21T00:00:00Z)` (confidence 0.78, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded in-memory template cache introduced by notification-service v3.8.0 causes old-gen heap exhaustion and OOMKilled restarts | bad_deployment | notification-service | notification-service v3.8.0 (2026-03-21T00:00:00Z) | 0.78 (high) | active |
| 2 | H4: Memory is a pre-existing resource-exhaustion / unrecorded limit change rather than a new fault | resource_exhaustion | notification-service | null | 0.15 (low) | active |
| 3 | H3: Shared dependency (email-provider, Kafka broker, or template/rendering service) degradation causes notification-service to buffer retries in memory | dependency_failure | email-provider | null | 0.10 (low) | active |
| 4 | H2: Kafka retention reduction to 3 days triggered a replay that notification-service choked on | config_change | kafka | kafka log.retention.ms 604800000 → 259200000 at 2026-03-23T18:00:00Z | 0.08 (low) | rejected |

### H1: Unbounded in-memory template cache introduced by notification-service v3.8.0 causes old-gen heap exhaustion and OOMKilled restarts
notification-service v3.8.0 (deployed 2026-03-21T00:00Z) added an in-memory template cache with no eviction policy. The cache grows monotonically with distinct template content (size=51750 → 196650 entries, evictions=0 over the whole window). This drives a slow steady heap (old-gen) climb that reaches the container limit roughly every ~24h, producing OOMKilled restarts on 03-21 22:59Z, 03-22 21:59Z, and 03-23 19:59Z. Consumer lag spikes are a consequence of each restart (consumer stops polling, then rebalances/catches up), not the cause. The Kafka retention change and 'replay' are coincidental — the 03-23 restart's lag spike occurs co-incident with the crash, and the earlier two restarts predate the retention change entirely.

**Causal chain**
1. Deploy v3.8.0 adds an in-memory template cache to notification-service [F1]
2. Cache accumulates without any eviction: entries grow to 196650 with evictions=0 [F15]
3. Heap grows slowly and steadily, driven by content accumulation rather than message volume [F9, F17, F15]
4. Old-generation heap exhaustion with escalating full-GC pause times appears hours after the deploy (first heap warning 03-21T21:37Z) [F16]
5. Container hits memory limit → OOMKilled (exit 137) restarts on 03-21, 03-22, 03-23 [F7, F8]
6. Consumer stops polling during restart → lag spikes to 18137 then decays as it catches up [F10, F12, F13]
7. Emails delayed ~25 min while consumer catches up post-restart [F10]

**Supporting evidence**
  - `F1` ✔ Across ALL services, only two deployments occurred in the week before the incident: notification-service v3.8.0 at 2026-03-21T00:00:00Z ('Add in-memory template cache for email rendering', status live, not rolled back) and auth-service v7.1.4 at 2026-03-22T13:00:00Z (patch, bump jackson). No notification-service deployment after 2026-03-21.
  - `F15` ✔ An in-memory template cache grows monotonically and has never evicted an entry: TemplateCache stats show size=51750 entries, evictions=0 (03-21T05:45:44Z) → 103500 entries, evictions=0 (03-21T11:30:16Z) → 155250 entries, evictions=0 (03-21T17:15:45Z) → 196650 entries, evictions=0 (03-21T21:51:13Z) → 188100 entries, evictions=0 (03-23T18:54:31Z). This is unbounded, content-driven accumulation, not message-volume-driven.
  - `F16` ✔ The memory pressure is old-generation heap exhaustion, not a throughput/heap-transient pattern: repeated 'heap usage N% over 90 (old gen)' full-GC warnings with escalating pause times (4152ms → 5072ms → 6388ms → 6488ms) in the hours before the 19:59:21Z OOMKilled restart. The heap warnings first appear 2026-03-21T21:37:01Z, hours after the v3.8.0 template-cache deployment (03-21T00:00:14Z) and before any Kafka maintenance.
  - `F9` ✔ Memory (memory_pct) is a slow steady climb, not a step: on 03-23 it rose from ~46% at 20:00 (post-restart) to 55.48% by 23:50, a gain of ~1 point per ~10 min, and pre-restart it sat pinned near 96-98% (max 98.44 at 19:40). Post-restart the container resumes climbing immediately.
  - `F17` ✔ memory_pct confirms content/leak-style growth independent of the restart: 93.73% at 18:00Z climbing to 96.78% (max 98.44) by 19:50Z, then a drop to 46.45% at 20:00Z immediately after the 19:59:21Z OOMKilled restart, after which it resumes climbing (47.87% at 21:00Z) at a steady ~1 point/10 min.
  - `F7` ✔ notification-service OOMKilled (exit code 137) occurred exactly three times this week, at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z — all three predate/are unrelated to a single maintenance event, and the 03-23 restart happened BEFORE the 20:20Z report.
  - `F8` ✔ The cumulative restarts metric steps 0→1 in the 2026-03-21T22:00 bucket, 1→2 in the 2026-03-22T22:00 bucket, and 2→3 in the 2026-03-23T20:00 bucket (bucket_seconds=7200), matching three distinct restart events.
  - `F10` ✔ Consumer lag spikes occur in the SAME/first bucket as the restart and are ~50-70 messages immediately before, then jump to 18137.93 at 19:55Z and decay afterwards (14213 at 20:00, 10374 at 20:05, 3600 at 20:15, back to 50.65 at 20:25) — i.e. the lag spike coincides with/after the OOM, not before it; lag is a consequence of the restart, not a leading cause.
  - `F12` ✔ The week-22 lag spikes are not unique to 03-23: consumer_lag also spiked in the 2026-03-21T22:00 bucket (avg 2273.11, max 17161.92) and 2026-03-22T20:00/22:00 buckets (avg 790.21/1589.77, max 17611.66/14375.74), each aligned with an OOM restart, showing the same restart→lag pattern repeats.
  - `F13` ✔ notification-service consumer_lag was NORMAL (well under 100 messages) throughout 18:00-19:40Z and only spiked to 18137.93 max in the 19:50Z bucket, the same bucket as the 19:59:21Z OOMKilled restart; it then decayed (14213.61 max at 20:00, 7499.81 at 20:10, 286.99 at 20:20, 71.58 at 20:30). No lag buildup precedes the crash.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 196650-entry cache is the dominant contributor to the ~50% memory_pct baseline and the per-cycle climb; no RSS/heap split is available to confirm the exact share.
  - The ~24h cadence (one OOM per day) matches the cache growth rate reaching the limit, but no pre-03-20 baseline exists to prove growth is new.
  - No unrecorded memory-limit reduction (disproven by absence of config changes, F3/F6, but config coverage may be incomplete).

### H4: Memory is a pre-existing resource-exhaustion / unrecorded limit change rather than a new fault
The OOMKilled restarts stem from notification-service running at/above its provisioned memory (container limit or an unrecorded limit/HPA reduction) with stale, pre-existing growth, so the incident is resource exhaustion on an under-provisioned service rather than a fault introduced this week. Lag spikes and the Kafka maintenance are incidental.

**Causal chain**
1. Service runs near its memory limit with slow persistent growth [F9, F17]
2. No deployment or config change explains the growth (Kafka retention change is the only change after the deploy) [F1, F3, F6]
3. Container repeatedly exceeds the limit → OOMKilled [F7, F8]

**Supporting evidence**
  - `F3` ✔ A dedicated query for config changes across all services in 2026-03-19 to 2026-03-21 returned count 0, so there were no config/flag changes (e.g. HPA, memory limits, consumer-group or topic settings) registered in the days immediately preceding the deployment.
  - `F6` ✔ No deployment or config change is recorded on or after the 2026-03-23 18:00Z Kafka maintenance, nor between the 2026-03-21 template-cache deploy and the maintenance — the change surface between the last code change (2026-03-21) and the incident is only the kafka retention reduction.
  - `F9` ✔ Memory (memory_pct) is a slow steady climb, not a step: on 03-23 it rose from ~46% at 20:00 (post-restart) to 55.48% by 23:50, a gain of ~1 point per ~10 min, and pre-restart it sat pinned near 96-98% (max 98.44 at 19:40). Post-restart the container resumes climbing immediately.
**Contradicting evidence**
  - `F1` ✔ Across ALL services, only two deployments occurred in the week before the incident: notification-service v3.8.0 at 2026-03-21T00:00:00Z ('Add in-memory template cache for email rendering', status live, not rolled back) and auth-service v7.1.4 at 2026-03-22T13:00:00Z (patch, bump jackson). No notification-service deployment after 2026-03-21.
  - `F15` ✔ An in-memory template cache grows monotonically and has never evicted an entry: TemplateCache stats show size=51750 entries, evictions=0 (03-21T05:45:44Z) → 103500 entries, evictions=0 (03-21T11:30:16Z) → 155250 entries, evictions=0 (03-21T17:15:45Z) → 196650 entries, evictions=0 (03-21T21:51:13Z) → 188100 entries, evictions=0 (03-23T18:54:31Z). This is unbounded, content-driven accumulation, not message-volume-driven.
  - `F16` ✔ The memory pressure is old-generation heap exhaustion, not a throughput/heap-transient pattern: repeated 'heap usage N% over 90 (old gen)' full-GC warnings with escalating pause times (4152ms → 5072ms → 6388ms → 6488ms) in the hours before the 19:59:21Z OOMKilled restart. The heap warnings first appear 2026-03-21T21:37:01Z, hours after the v3.8.0 template-cache deployment (03-21T00:00:14Z) and before any Kafka maintenance.
**Assumptions**
  - Growth could predate 03-21; no pre-03-20 data exists.
  - Memory limits were unchanged (not confirmed — config coverage may miss limit keys).
**Rejected because:** null

### H3: Shared dependency (email-provider, Kafka broker, or template/rendering service) degradation causes notification-service to buffer retries in memory
A downstream shared dependency — most plausibly the vendor email-provider (has_telemetry=false, so unmonitored) or the Kafka broker — degraded, causing notification-service to accumulate unsent messages/retries in memory, driving the OOMKilled restarts and delayed emails.

**Causal chain**
1. Email-provider or broker degrades and slows notifications *(assumed)*
2. notification-service buffers unsent messages/retries in memory *(assumed)*
3. Buffer grows unbounded → OOMKilled restart [F7]
4. Emails delayed [F7]

**Supporting evidence**
  - `F4` ✔ Dependency graph of notification-service: it has edges to kafka (kind 'queue-consume') and to email-provider (kind 'external', vendor-owned, has_telemetry=false). kafka is marked shared=true (owner platform); notification-service itself is has_telemetry=true, shared=false. No upstream edges into notification-service were returned (empty edge set), so no producing service is registered as a caller.
**Contradicting evidence**
  - `F15` ✔ An in-memory template cache grows monotonically and has never evicted an entry: TemplateCache stats show size=51750 entries, evictions=0 (03-21T05:45:44Z) → 103500 entries, evictions=0 (03-21T11:30:16Z) → 155250 entries, evictions=0 (03-21T17:15:45Z) → 196650 entries, evictions=0 (03-21T21:51:13Z) → 188100 entries, evictions=0 (03-23T18:54:31Z). This is unbounded, content-driven accumulation, not message-volume-driven.
  - `F16` ✔ The memory pressure is old-generation heap exhaustion, not a throughput/heap-transient pattern: repeated 'heap usage N% over 90 (old gen)' full-GC warnings with escalating pause times (4152ms → 5072ms → 6388ms → 6488ms) in the hours before the 19:59:21Z OOMKilled restart. The heap warnings first appear 2026-03-21T21:37:01Z, hours after the v3.8.0 template-cache deployment (03-21T00:00:14Z) and before any Kafka maintenance.
  - `F11` ✔ Request throughput was flat and normal across the onset (~118-142 req/s throughout 18:30-21:30Z, including the 19:55 spike window), so the memory climb is NOT throughput-correlated — the OOM is not driven by a traffic/load surge.
  - `F14` ✔ Message/request throughput did NOT surge around the onset: request_rate stayed flat 117.85-142.43 req/s across 18:00-21:00Z (141.2 avg / 141.93 max in the 19:50 bucket itself), and latency_p99_ms stayed flat 101.97-117.13 ms. So neither a traffic burst nor a processing-rate change is present.
  - `F9` ✔ Memory (memory_pct) is a slow steady climb, not a step: on 03-23 it rose from ~46% at 20:00 (post-restart) to 55.48% by 23:50, a gain of ~1 point per ~10 min, and pre-restart it sat pinned near 96-98% (max 98.44 at 19:40). Post-restart the container resumes climbing immediately.
  - `F13` ✔ notification-service consumer_lag was NORMAL (well under 100 messages) throughout 18:00-19:40Z and only spiked to 18137.93 max in the 19:50Z bucket, the same bucket as the 19:59:21Z OOMKilled restart; it then decayed (14213.61 max at 20:00, 7499.81 at 20:10, 286.99 at 20:20, 71.58 at 20:30). No lag buildup precedes the crash.
**Assumptions**
  - A dependency could fail silently because email-provider has no telemetry.
  - Memory growth tracks accumulated retries rather than cached content.
**Rejected because:** null

### H2: Kafka retention reduction to 3 days triggered a replay that notification-service choked on
As the reporter suspects, the 2026-03-23T18:00Z Kafka log.retention.ms reduction (7d→3d) caused brokers to drop/expire segments in a way that forced consumers to replay or re-read, and the resulting burst of messages overwhelmed notification-service's buffering/cache, causing memory to grow and the consumer to OOMKilled.

**Causal chain**
1. Platform reduces kafka retention to 3 days [F2]
2. Retention/segment change triggers consumer replay and a lag burst reaching notification-service *(assumed)*
3. Replay burst drives unbounded memory growth in notification-service *(assumed)*
4. notification-service OOMKilled and emails delayed [F7]

**Supporting evidence**
  - `F2` ✔ The only config change anywhere in the window was on kafka: log.retention.ms reduced from 604800000 (7 days) to 259200000 (3 days) at 2026-03-23T18:00:00Z by platform-team, described as 'Kafka maintenance: reduce retention to 3 days'. This is the 'today's Kafka maintenance' referenced in the report and is ~2h20m before the 20:20Z report.
  - `F5` ✔ Two historical precedent incidents match the symptom pattern: HIST-104 (2025-11-05, notification-service + kafka, consumer lag spike/delayed emails, root cause 'Partition reassignment caused consumer group rebalancing storm') and HIST-112 (2026-02-03, settlement-service + kafka, consumer lag growing, root cause 'Consumer lost partition assignment during broker maintenance'). Neither precedes today's reported OOMKilled restarts directly, and the search notes past incidents are leads, not evidence about the current incident.
**Contradicting evidence**
  - `F7` ✔ notification-service OOMKilled (exit code 137) occurred exactly three times this week, at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z — all three predate/are unrelated to a single maintenance event, and the 03-23 restart happened BEFORE the 20:20Z report.
  - `F8` ✔ The cumulative restarts metric steps 0→1 in the 2026-03-21T22:00 bucket, 1→2 in the 2026-03-22T22:00 bucket, and 2→3 in the 2026-03-23T20:00 bucket (bucket_seconds=7200), matching three distinct restart events.
  - `F13` ✔ notification-service consumer_lag was NORMAL (well under 100 messages) throughout 18:00-19:40Z and only spiked to 18137.93 max in the 19:50Z bucket, the same bucket as the 19:59:21Z OOMKilled restart; it then decayed (14213.61 max at 20:00, 7499.81 at 20:10, 286.99 at 20:20, 71.58 at 20:30). No lag buildup precedes the crash.
  - `F10` ✔ Consumer lag spikes occur in the SAME/first bucket as the restart and are ~50-70 messages immediately before, then jump to 18137.93 at 19:55Z and decay afterwards (14213 at 20:00, 10374 at 20:05, 3600 at 20:15, back to 50.65 at 20:25) — i.e. the lag spike coincides with/after the OOM, not before it; lag is a consequence of the restart, not a leading cause.
  - `F18` ✔ The same restart→lag pattern (lag spike only coincident with the crash, no pre-crash buildup) is consistent with T1's finding that all three OOMKilled restarts predate the single 2026-03-23T18:00Z Kafka retention change, so the maintenance cannot be the trigger for the earlier two crashes.
  - `F9` ✔ Memory (memory_pct) is a slow steady climb, not a step: on 03-23 it rose from ~46% at 20:00 (post-restart) to 55.48% by 23:50, a gain of ~1 point per ~10 min, and pre-restart it sat pinned near 96-98% (max 98.44 at 19:40). Post-restart the container resumes climbing immediately.
  - `F11` ✔ Request throughput was flat and normal across the onset (~118-142 req/s throughout 18:30-21:30Z, including the 19:55 spike window), so the memory climb is NOT throughput-correlated — the OOM is not driven by a traffic/load surge.
  - `F14` ✔ Message/request throughput did NOT surge around the onset: request_rate stayed flat 117.85-142.43 req/s across 18:00-21:00Z (141.2 avg / 141.93 max in the 19:50 bucket itself), and latency_p99_ms stayed flat 101.97-117.13 ms. So neither a traffic burst nor a processing-rate change is present.
**Assumptions**
  - A retention reduction on a topic whose data is still being consumed can trigger large re-reads.
  - Broker-side telemetry (not queried) would show a replay burst at ~18:00-19:50Z.
**Rejected because:** Timing and mechanism both fail. Two of the three OOMKilled restarts (03-21 22:59Z, 03-22 21:59Z) precede the 18:00Z retention change entirely (F7, F8, F18). On 03-23, consumer lag is normal (<100) through 19:40Z — i.e. 100 minutes after the maintenance — and only spikes in the SAME bucket as the 19:59Z crash, decaying afterward (F10, F13). No lag buildup precedes the OOM, so lag cannot be the leading cause. Throughput and latency are flat across the maintenance and onset (F11, F14), so no replay burst reached the consumer. Memory rises as a slow steady pre-existing climb (F9, F17) independent of any burst.

## Facts (verified observations)
- The only deployment on notification-service in the week before the incident was v3.8.0 at 2026-03-21T00:00:00Z, described as 'Add in-memory template cache for email rendering', status live and not rolled back. No notification-service deployment occurred after 2026-03-21. [F1]
- The in-memory template cache grows monotonically and has never evicted an entry: size 51750 (03-21T05:45Z) → 103500 → 155250 → 196650 (03-21T21:51Z) → 188100 (03-23T18:54Z), with evictions=0 at every sample; accumulation is content-driven, not message-volume-driven. [F15]
- notification-service was OOMKilled (exit code 137) exactly three times this week: 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z. [F7]
- The cumulative restarts metric steps 0→1 in the 2026-03-21T22:00 bucket, 1→2 in the 2026-03-22T22:00 bucket, and 2→3 in the 2026-03-23T20:00 bucket (bucket_seconds=7200), matching three distinct restart events and a ~24h cadence. [F8]
- memory_pct shows a slow steady climb rather than a step: on 03-23 it rose from ~46% post-restart at 20:00 to 55.48% by 23:50 (~1 point per ~10 min), was pinned near 96-98% before the crash (max 98.44 at 19:40), and resumes climbing immediately after each restart. [F9]
- consumer_lag was normal (well under 100 messages) throughout 18:00-19:40Z on 03-23 and only spiked to 18137.93 max in the 19:50Z bucket — the same bucket as the 19:59:21Z OOM restart — decaying afterwards (14213.61 at 20:00, 7499.81 at 20:10, 286.99 at 20:20, 71.58 at 20:30). No lag buildup precedes the crash. [F13]
- Request throughput was flat and normal across the onset (~117.85-142.43 req/s throughout 18:00-21:00Z, including the 19:50 spike window) and latency_p99_ms stayed flat at 101.97-117.13 ms, so no traffic surge or processing-rate change is present. [F11, F14]
- The only config change anywhere in the window was kafka log.retention.ms reduced from 604800000 (7 days) to 259200000 (3 days) at 2026-03-23T18:00:00Z; a dedicated query for config changes across all services 2026-03-19 to 2026-03-21 returned count 0. [F2, F3]
- The same restart→lag pattern repeats across all three crashes: consumer_lag also spiked in the 2026-03-21T22:00 bucket (avg 2273.11, max 17161.92) and the 03-22 20:00/22:00 buckets (avg 790.21/1589.77, max 17611.66/14375.74), each aligned with an OOM restart — so the lag spike coincides with or follows the crashes rather than preceding them. [F12, F10]
- notification-service depends on kafka (queue-consume) and on email-provider (external, vendor-owned, has_telemetry=false); no upstream edge into notification-service is registered, and notification-service itself is has_telemetry=true and shared=false. [F4]
- Historical precedents HIST-104 (2025-11-05, notification-service + kafka, consumer lag spike/delayed emails) and HIST-112 (2026-02-03, settlement-service + kafka, consumer lag growth during broker maintenance) match the symptom pattern but are leads, not evidence about the current incident. [F5]

## Assumptions (not verified)
- Assumption: the 196650-entry cache is the dominant contributor to the ~50% memory_pct baseline and the per-cycle climb; no RSS/heap split is available to confirm the exact share.
- Assumption: the ~24h OOM cadence reflects the cache growth rate reaching the container limit; no pre-03-20 baseline exists to prove the growth pattern is new.
- Assumption: no unrecorded memory-limit or HPA reduction occurred for notification-service (no such change was found, but config-change coverage may only cover a limited set of keys).
- Assumption: the consumer lag spikes are a consequence of each restart (consumer stops polling, then rebalances/catches up) rather than a cause of the memory growth.
- Assumption: no broker-level replay burst reached the consumer, inferred from flat consumer-side throughput/latency and the absence of pre-crash lag buildup.
- Assumption: the 'Kafka maintenance' consisted only of the recorded log.retention.ms change; broker-level maintenance actions are not covered by available tooling.
- (demoted from facts: no verified observation backs it) Old-generation heap exhaustion is present, not a throughput transient: repeated 'heap usage N% over 90 (old gen)' full-GC warnings with escalating pause times (4152ms → 5072ms → 6388ms → 6488ms) in the hours before the 19:59:21Z OOM; the first heap warnings appear 2026-03-21T21:37:01Z, hours after the v3.8.0 deploy and before any Kafka maintenance.
- (demoted from facts: no verified observation backs it) No deployment or config change is recorded on or after the 2026-03-23T18:00Z Kafka maintenance, nor between the 2026-03-21 template-cache deploy and the maintenance; the only change surface between the last code change and the incident is the kafka retention reduction.
- (demoted from facts: no verified observation backs it) All three OOMKilled restarts predate or are unrelated to the single 2026-03-23T18:00Z Kafka retention change, so the maintenance cannot have triggered the two earlier crashes.

## Timeline
- `2026-03-21T00:00:00Z` notification-service v3.8.0 deployed, described as 'Add in-memory template cache for email rendering'; status live, not rolled back. Only notification-service deploy in the week. [F1]
- `2026-03-21T05:45:44Z` TemplateCache stats: size=51750 entries, evictions=0 (earliest cache sample). [F15]
- `2026-03-21T21:37:01Z` First 'heap usage N% over 90 (old gen)' full-GC warning recorded, ~21.5h after the v3.8.0 deploy and before any Kafka change. [F16]
- `2026-03-21T21:51:13Z` TemplateCache size reaches 196650 entries with evictions=0 (peak); later sample 188100 entries, evictions=0. [F15]
- `2026-03-21T22:59:50Z` notification-service OOMKilled (exit code 137), first restart; cumulative restarts metric steps 0→1 in the 03-21T22:00 bucket. Consumer lag spikes in the same bucket (avg 2273.11, max 17161.92). [F7, F8, F12]
- `2026-03-22T21:59:47Z` Second OOMKilled restart (exit 137); restarts metric steps 1→2 in the 03-22T22:00 bucket, with consumer lag spikes in the 03-22 20:00/22:00 buckets (max 17611.66/14375.74). [F7, F8, F12]
- `2026-03-23T18:00:00Z` Kafka maintenance: log.retention.ms reduced from 604800000 (7d) to 259200000 (3d) by platform-team — the only config change anywhere in the window and the last change before the incident. [F2, F6]
- `2026-03-23T18:00:00Z` memory_pct 93.73% and climbing; consumer lag remains normal (<100) through 19:40Z, ~100 minutes after the maintenance. [F17, F13]
- `2026-03-23T18:54:31Z` TemplateCache sample: 188100 entries, evictions=0 — continued unbounded, content-driven accumulation. [F15]
- `2026-03-23T19:40:00Z` memory_pct max 98.44% (pinned 96-98% pre-crash); repeated old-gen full-GC warnings with escalating pauses (4152ms → 5072ms → 6388ms → 6488ms). [F9, F17, F16]
- `2026-03-23T19:50:00Z` Consumer lag spikes to 18137.93 max in the 19:50Z bucket — the same bucket as the crash; request_rate flat (141.2 avg / 141.93 max) and latency_p99_ms flat 101.97-117.13 ms. [F13, F10, F14, F11]
- `2026-03-23T19:59:21Z` Third OOMKilled restart (exit 137); restarts metric steps 2→3 in the 03-23T20:00 bucket. [F7, F8, F10]
- `2026-03-23T20:00:00Z` memory_pct drops to 46.45% immediately after the restart, then resumes climbing (47.87% at 21:00Z); lag decays (14213.61 at 20:00, 7499.81 at 20:10, 286.99 at 20:20, 71.58 at 20:30). [F17, F13]
- `2026-03-23T20:20:00Z` Incident INC-004 reported by on-call SRE (delayed trade-confirmation emails ~25 min), after the 19:59:21Z restart. [F7]
- `2026-03-23T23:50:00Z` memory_pct at 55.48%, ~1 point per ~10 minutes after the restart — the same steady climb recurring toward the next OOM cycle. [F9]

## Recommended actions
- **[immediate]** Mitigate now by disabling or bounding the v3.8.0 in-memory template cache in notification-service — roll back to the pre-v3.8.0 build or disable the cache feature flag and set an explicit max-entries/eviction (LRU) bound — then restart to reset the pod and confirm memory_pct plateaus instead of climbing ~1 point/10 min. -- The cache is the only unbounded, content-driven memory consumer introduced this week (F1, F15) and is the leading driver of the old-gen heap climb and repeated OOMs (F16, F9, F17). Bounding/removing it breaks the ~24h OOM cadence; the next OOM would otherwise be expected around 03-24T20:00Z by cadence (F7, F8). [F1, F15, F16, F9, F17, F7, F8]
- **[immediate]** Do NOT treat the Kafka retention change as the cause and do not revert log.retention.ms as a mitigation; if platform-team proposes reverting it, require evidence of a broker-side replay burst first, since consumer-side lag was normal (<100) through 19:40Z and throughput/latency were flat across the maintenance window. -- The retention change cannot explain the two earlier restarts (F18, F7) and no replay burst reached the consumer (F13, F10, F11, F14); reverting it would not stop the OOMs and would add unnecessary change to the system. [F2, F18, F7, F13, F10, F11, F14]
- **[short_term]** Verify the fix by watching TemplateCache size/evictions and memory_pct after the change for at least one full ~24h OOM cadence: confirm evictions > 0 and cache size plateaus, memory_pct stays well below the pre-crash 96-98% band, and no exit-137 restart occurs in the 03-24T20:00Z bucket. -- The ~24h cadence (F7, F8) means one cycle of clean telemetry after the change is the cheapest available discrimination between a fixed and an unfixed condition; pre-crash memory was 96-98% with escalating old-gen GC pauses (F9, F16, F17). [F7, F8, F9, F16, F17, F15]
- **[short_term]** Instrument the cache and heap: expose eviction/hit-ratio metrics, add a heap-vs-RSS split (currently only memory_pct exists), and alert on old-gen GC pause escalation and on memory_pct rise rate (e.g. >1 point per 10 min sustained) rather than only on restart counts. -- The investigation was limited to memory_pct with no RSS/heap breakdown and had to infer the leak shape from GC warnings (F16) and cache entry counts (F15); direct instrumentation makes the next occurrence trivially diagnosable. [F15, F16, F9]
- **[long_term]** Add a pre-merge review gate/lint for in-memory caches in notification-service requiring a bounded size and eviction policy, and add a load/soak test that asserts stable memory under sustained distinct-template traffic before release. -- The root fault is an evictionless cache introduced by a single deploy (F1, F15); a gate prevents recurrence of the same class of defect rather than this instance. [F1, F15]
- **[long_term]** Close the config-coverage gap by extending the config-change feed to cover notification-service memory limits, HPA, and consumer-group settings (max.poll.records, fetch sizes) so that resource-exhaustion alternatives can be excluded rather than assumed away next time. -- The remaining uncertainty about an unrecorded limit/HPA change (H4) exists only because coverage may miss those keys; no config change was found in the window (F3, F6) and F15/F16 argue growth is new and deploy-tied. [F3, F6, F15, F16]
- **[long_term]** Add telemetry/health signaling for the email-provider dependency (currently has_telemetry=false) so the retry-buffering alternative (H3) can be tested directly rather than inferred, and so delayed-email symptoms can be attributed precisely. -- H3 could not be tested because the vendor email-provider is unmonitored (F4); the alternative was only dismissed indirectly via flat lag/throughput and content-driven cache growth (F11, F14, F15). [F4, F11, F14, F15]

## Data gaps
- No memory-limit/request or HPA config change for notification-service could be found, and get_config_changes(kafka) returned only log.retention.ms; config-change scope may cover only a limited set of keys, so an unrecorded memory-limit/HPA/consumer-group change cannot be excluded as a co-factor (H4).
- get_service_dependencies(notification-service, direction=upstream) and (kafka, downstream) both returned empty edge sets, so the producing service(s) that write the trade-confirmation topic could not be identified and a producer-side change cannot be assessed.
- No Kafka broker-level telemetry (broker heap, replication, under-replicated partitions, partition reassignment events, produce/replay volume) was available, so the 'maintenance replay' hypothesis (H2) could not be tested against broker-side data, only against consumer-side lag/throughput.
- No pre-2026-03-20 data was retrieved; the restarts series starts 2026-03-20, so the claim that growth is new is bounded by data availability rather than by an explicit zero baseline before 03-20.
- Metrics provide only memory_pct with no heap-vs-RSS breakdown, so a true application heap leak versus container RSS growth could not be distinguished.
- Neighbor services' metrics (order-service, payment-service, trade-service, Kafka broker) were not queried; only order-service logs were checked (0 matched entries, no ERROR/WARN for 03-23 18:00-23:59Z), so blast radius beyond notification-service is unverified.
- The 03-21/03-22 log window query returned truncated=true (40 of 151 matched), so full heap-GC and TemplateCache history for the first two crashes was not enumerated minute-by-minute.
- Per-partition skew and message byte-size metrics are not exposed, so content-size correlation is inferred indirectly from unbounded TemplateCache entry counts rather than measured.

## What would change this conclusion
- TemplateCache size and evictions showing a bounded/plateauing pattern after 03-21T00:00Z (evictions > 0 or size stable), which would show the cache is not unbounded and break the H1 mechanism.
- A template-cache rollback/disable that fails to stop the ~24h OOM cycle — memory_pct continuing to climb to 96-98% and an exit-137 restart recurring around 03-24T20:00Z — which would point away from the cache toward another memory consumer or an unrecorded limit change.
- Evidence of a Kafka broker-side replay or partition-reassignment burst at ~18:00-19:50Z on 03-23 with consumer-side lag buildup BEFORE the 19:59:21Z crash, which would revive the retention/replay mechanism (H2).
- A recorded memory-limit or HPA reduction on notification-service within the window, which would support pre-existing resource exhaustion (H4) instead of a new fault.
- Dependency telemetry showing email-provider or Kafka degradation with retry-buffering in notification-service memory (e.g. growing queued-retry counts tracking memory growth), which would support H3.
- Any deployment or config change on notification-service between 2026-03-21T00:00Z and the incident other than v3.8.0 — or a producer-side change on the trade-confirmation topic — which would introduce an alternative trigger the current evidence does not cover.

## Integrity warnings (set by code)
- fact demoted to assumption: Old-generation heap exhaustion is present, not a throughput transient: repeated 
- fact demoted to assumption: No deployment or config change is recorded on or after the 2026-03-23T18:00Z Kaf
- fact demoted to assumption: All three OOMKilled restarts predate or are unrelated to the single 2026-03-23T1

## Investigation stats
- latency 160.72s, LLM calls 14, tokens 94499 (in 75398 / out 19101)
- tool calls 24 (2 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
