# Incident investigation report: INC-004
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The three OOMKilled restarts of notification-service this week are best explained by dep-5402 (v3.8.0, 2026-03-21T00:00Z), which added an in-memory ConcurrentHashMap template cache on the email render path and is the only notification-service change in the window; a steady, unbounded-looking memory ramp begins ~2h after it and repeats on a ~24h cadence to the ~95-98% ceiling, each time collapsing into an OOMKill (F1, F13, F10, F9, F7, F8, F12). The reporter's "consumer choking on a replay" theory is not supported: consumer lag spikes follow each crash rather than precede it, the ramps predate the 2026-03-23T18:00Z Kafka maintenance, and retention is a stored-data-window setting rather than a capacity knob (F11, F15, F7, F9). Confidence is medium (0.60) because attribution to the cache is by timing and elimination only — no heap/absolute-memory metric or runtime profile confirms the cache is the allocator, and broker-side rebalance events are not queryable. The immediate priority is to roll back dep-5402 and confirm the sawtooth stops; a replay/rebalance cause remains the principal runner-up and should be discriminated by heap profiling and broker-side consumer-group checks.

## Probable root cause
**dep-5402 in-memory template cache leaks/unbounds memory** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: dep-5402 in-memory template cache leaks/unbounds memory | bad_deployment | notification-service | dep-5402 | 0.60 (medium) | active |
| 2 | H2: Broker-side Kafka rebalance/replay storm (HIST-104 class) | infrastructure_failure | kafka | cfg-2410 | 0.12 (low) | rejected |
| 3 | H5: Cyclical traffic peak (end-of-day trade confirmations) with no faulty change | capacity_saturation | notification-service | null | 0.08 (low) | rejected |
| 4 | H3: Kafka retention reduction (cfg-2410) increased broker memory/backpressure | config_change | kafka | cfg-2410 | 0.05 (low) | rejected |
| 5 | H4: Resource-limit/heap config was lowered, narrowing OOM margin | config_change | notification-service | null | 0.05 (low) | rejected |

### H1: dep-5402 in-memory template cache leaks/unbounds memory
The code release dep-5402 v3.8.0 on 2026-03-21T00:00Z added an in-memory ConcurrentHashMap template cache keyed by template+locale+userSegment on the email render path. The key space grows unbounded (or entries are never evicted), so resident memory ramps steadily ~24h until the container hits its limit and is OOMKilled; the daily cadence reflects the time to fill the heap from reset, and the consumer lag spikes/email delays are consequences of each crash (in-flight work lost, redelivery), not causes.

**Causal chain**
1. dep-5402 v3.8.0 (in-memory template cache) deployed to notification-service at 03-21T00:00Z [F1, F13]
2. Memory begins a steady ramp from ~48% baseline starting 03-21T02:00, ~2h after the deploy [F10, F9]
3. Heap climbs to >90-98%, full-GC warnings, then sawtooth crash to ~48% at each OOM [F9, F12]
4. OOMKilled exits (137) recur ~24h apart as the cache refills [F7, F8]
5. Consumer lag spikes and email delays appear AFTER each crash, not before [F11, F12]

**Supporting evidence**
  - `F1` ✔ Only one notification-service deployment in the 7 days before the incident: dep-5402 v3.8.0 at 2026-03-21T00:00:00Z adding an in-memory template cache (ConcurrentHashMap keyed by template+locale+userSegment on the email render path); status live, rolled_back_t null. It landed ~2.5 days before the report.
  - `F13` ✔ Across 2026-03-16..24 the ONLY notification-service deployment is dep-5402 v3.8.0 'Add in-memory template cache for email rendering' at 2026-03-21T00:00:00Z (status live, rolled_back_at null) — it is the only notification-service change preceding the first OOM at 2026-03-21T22:59:50Z (~23h earlier).
  - `F10` ✔ The first anomaly in the week is NOT the OOM: log warnings begin 2026-03-21T03:00:44Z with 'GC pause 253ms exceeded 200ms target', and the memory_pct ramp clearly begins in the 2026-03-21T02:00 bucket (avg 52.71) after a flat ~48% baseline on 03-20; heap-over-90% 'full GC took' warnings start at 2026-03-21T21:37:01Z, just before the first OOM.
  - `F9` ✔ memory_pct shows a sawtooth ramp-then-crash pattern preceding every OOM: it climbs steadily (~48% -> 95%+) and resets to ~48% at each restart. First OOM (03-21T22:59) was preceded by a ramp from 48.23% at 03-21T00:00 to avg 92.91/max 95.09 at 03-21T20:00 and max 97.37 at 03-21T22:00. Second (03-22T21:59) ramped 50.33 -> avg 94.91/max 97.98 at 03-22T20:00. Third (03-23T19:59) ramped 52.99 -> avg 95.05/max 98.44 at 03-23T18:00 with 20-min buckets reaching avg 96.73/max 98.44 at 19:40, then resetting to 46.72 at 20:00.
  - `F7` ✔ Exactly three OOMKilled restarts (exit code 137) of notification-service-7d9f occurred in this week's window, at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z, and 2026-03-23T19:59:21Z — closely spaced near 22:00/20:00 daily, roughly 24h apart.
  - `F8` ✔ The restarts metric (cumulative) confirms three restarts: it steps from 0 to 1 in the 2026-03-21T22:00 bucket, to 2 in the 2026-03-22T22:00 bucket, and to 3 in the 2026-03-23T20:00 bucket.
  - `F11` ✔ consumer_lag spikes immediately AFTER each memory crash/OOM, not before: during the ramps lag stays in its normal ~50-100 band, then jumps within the same/next bucket — 03-21T22:00 avg 2273.11/max 17161.92; 03-22T20:00 avg 790.21/max 17611.66 and 22:00 avg 1589.77/max 14375.74; 03-23 the 19:40 bucket avg 4578.25/max 18137.93 while memory is already at 96.73%.
  - `F12` ✔ The third OOM is a repeat of the same pattern, not a deviation: memory ramps to the same ~95-98% ceiling and collapses, identical sawtooth shape as the 03-21 and 03-22 events, and the first heap-over-90% warning in each ramp comes just minutes before the OOM (e.g. 03-23 ramp warnings from 18:34:46Z, OOM at 19:59:21Z). The reported causal link to 'today's Kafka maintenance' is not supported by the data: the lag spike follows the crash, and the ramp began the prior day.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The template cache grows unbounded / lacks eviction, and it is the dominant allocator of the growing heap
  - No tracked heap/limit change accompanied dep-5402 (GAPS)
**Rejected because:** null

### H2: Broker-side Kafka rebalance/replay storm (HIST-104 class)
A Kafka broker-side event (partition reassignment/rebalancing storm, as in HIST-104) causes consumer-group churn and a replay/backlog flood to notification-service, driving unbounded in-memory buffering and OOM. The reporter's 'consumer choking on a replay after maintenance' is a variant of this.

**Causal chain**
1. Kafka maintenance/broker-side rebalance changes consumer-group state [F3, F15, F18]
2. Consumer receives burst of stale/replayed records and buffers them in memory *(assumed)*
3. Memory ramps to limit and container is OOMKilled [F9, F7]
4. Consumer lag spikes as backlog grows [F11]

**Supporting evidence**
  - `F6` ✔ Prior incident HIST-104 (2025-11-05) matches the same symptom pair — consumer lag spike + delayed emails on notification-service/kafka — with root cause 'Partition reassignment caused consumer group rebalancing storm' (category infrastructure_failure, throttled reassignment). HIST-107 (redis cold-cache storm) matched only weakly (score 1). These are leads about similar symptoms, not evidence about this incident's cause.
  - `F18` ✔ The only historical precedent matching the symptom pair (lag spike + delayed emails) is HIST-104 (2025-11-05), attributed to a Kafka broker rebalance/partition-reassignment storm — a lead, and notably a broker-side event with NO deployment/config change, i.e. a different class of cause than a code change. No prior OOMKilled/memory-ramp incident for notification-service was found.
  - `F3` ✔ Exactly one config change in the window and it is the Kafka maintenance: kafka log.retention.ms 604800000 -> 259200000 (7d -> 3d) at 2026-03-23T18:00:00Z by platform-team, i.e. ~2h20m before the 20:20Z incident report. No notification-service consumer/JVM/heap/resource-limit config change appears in the results.
**Contradicting evidence**
  - `F11` ✔ consumer_lag spikes immediately AFTER each memory crash/OOM, not before: during the ramps lag stays in its normal ~50-100 band, then jumps within the same/next bucket — 03-21T22:00 avg 2273.11/max 17161.92; 03-22T20:00 avg 790.21/max 17611.66 and 22:00 avg 1589.77/max 14375.74; 03-23 the 19:40 bucket avg 4578.25/max 18137.93 while memory is already at 96.73%.
  - `F12` ✔ The third OOM is a repeat of the same pattern, not a deviation: memory ramps to the same ~95-98% ceiling and collapses, identical sawtooth shape as the 03-21 and 03-22 events, and the first heap-over-90% warning in each ramp comes just minutes before the OOM (e.g. 03-23 ramp warnings from 18:34:46Z, OOM at 19:59:21Z). The reported causal link to 'today's Kafka maintenance' is not supported by the data: the lag spike follows the crash, and the ramp began the prior day.
  - `F15` ✔ The Kafka maintenance cfg-2410 (log.retention.ms 604800000 -> 259200000, i.e. 7d -> 3d, platform-team) is timestamped 2026-03-23T18:00:00Z — ~2h before the third OOM (19:59:21Z) and ~19h after the memory ramp for that cycle already began (~03-22T21:00). Retention is a stored-data window change, not a consumer/broker-capacity change.
  - `F7` ✔ Exactly three OOMKilled restarts (exit code 137) of notification-service-7d9f occurred in this week's window, at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z, and 2026-03-23T19:59:21Z — closely spaced near 22:00/20:00 daily, roughly 24h apart.
  - `F9` ✔ memory_pct shows a sawtooth ramp-then-crash pattern preceding every OOM: it climbs steadily (~48% -> 95%+) and resets to ~48% at each restart. First OOM (03-21T22:59) was preceded by a ramp from 48.23% at 03-21T00:00 to avg 92.91/max 95.09 at 03-21T20:00 and max 97.37 at 03-21T22:00. Second (03-22T21:59) ramped 50.33 -> avg 94.91/max 97.98 at 03-22T20:00. Third (03-23T19:59) ramped 52.99 -> avg 95.05/max 98.44 at 03-23T18:00 with 20-min buckets reaching avg 96.73/max 98.44 at 19:40, then resetting to 46.72 at 20:00.
**Assumptions**
  - A broker-side rebalance/replay occurred (not queryable via config/deploy tools)
  - Replay volume exceeds consumer memory
**Rejected because:** Directly contradicted: consumer lag spikes FOLLOW the memory crash, not before (F11), so backlog cannot be the cause of the ramp. The ramp begins ~2h after the deploy on 03-21 and repeats ~24h apart predating today's maintenance (F7/F9/F12). Kafka maintenance is at 03-23T18:00Z, after the second and third ramps already began (F15). Mem still ~48% at start and ramps steadily, not stepwise from a replay burst.

### H5: Cyclical traffic peak (end-of-day trade confirmations) with no faulty change
The OOMs are a regular traffic-driven cycle tied to daily end-of-day trade-confirmation volume; the sawtooth reflects periodic traffic peaks filling the heap, unrelated to Kafka maintenance or any code change.

**Causal chain**
1. Daily EOD trade-confirmation burst raises memory use *(assumed)*
2. Memory peaks to limit and container OOMKilled ~24h apart [F7, F8, F9]
3. Delayed emails follow [F11]

**Supporting evidence**
  - `F7` ✔ Exactly three OOMKilled restarts (exit code 137) of notification-service-7d9f occurred in this week's window, at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z, and 2026-03-23T19:59:21Z — closely spaced near 22:00/20:00 daily, roughly 24h apart.
  - `F8` ✔ The restarts metric (cumulative) confirms three restarts: it steps from 0 to 1 in the 2026-03-21T22:00 bucket, to 2 in the 2026-03-22T22:00 bucket, and to 3 in the 2026-03-23T20:00 bucket.
**Contradicting evidence**
  - `F10` ✔ The first anomaly in the week is NOT the OOM: log warnings begin 2026-03-21T03:00:44Z with 'GC pause 253ms exceeded 200ms target', and the memory_pct ramp clearly begins in the 2026-03-21T02:00 bucket (avg 52.71) after a flat ~48% baseline on 03-20; heap-over-90% 'full GC took' warnings start at 2026-03-21T21:37:01Z, just before the first OOM.
  - `F13` ✔ Across 2026-03-16..24 the ONLY notification-service deployment is dep-5402 v3.8.0 'Add in-memory template cache for email rendering' at 2026-03-21T00:00:00Z (status live, rolled_back_at null) — it is the only notification-service change preceding the first OOM at 2026-03-21T22:59:50Z (~23h earlier).
  - `F9` ✔ memory_pct shows a sawtooth ramp-then-crash pattern preceding every OOM: it climbs steadily (~48% -> 95%+) and resets to ~48% at each restart. First OOM (03-21T22:59) was preceded by a ramp from 48.23% at 03-21T00:00 to avg 92.91/max 95.09 at 03-21T20:00 and max 97.37 at 03-21T22:00. Second (03-22T21:59) ramped 50.33 -> avg 94.91/max 97.98 at 03-22T20:00. Third (03-23T19:59) ramped 52.99 -> avg 95.05/max 98.44 at 03-23T18:00 with 20-min buckets reaching avg 96.73/max 98.44 at 19:40, then resetting to 46.72 at 20:00.
**Assumptions**
  - Traffic alone can fill heap to 95-98% (no traffic metric pulled - GAPS)
**Rejected because:** The pattern does not predate dep-5402: baseline was flat ~48% before 03-21 (F10), and the first ramp/deploy align within ~2h (F13/F10). A load-driven cycle would have shown the same pattern earlier and would not correlate with the single code change adding an in-memory cache.

### H3: Kafka retention reduction (cfg-2410) increased broker memory/backpressure
The Kafka log.retention.ms reduction (7d->3d) at 03-23T18:00Z changed broker behavior (e.g., compaction/segment churn or backpressure), increasing notification-service memory pressure and causing the third OOM.

**Causal chain**
1. cfg-2410 reduces retention 7d->3d at 03-23T18:00Z [F3, F15]
2. Reduced retention triggers segment deletion/compaction pressure on broker *(assumed)*
3. Increased consumer memory pressure -> OOMKilled *(assumed)*

**Supporting evidence**
  - `F3` ✔ Exactly one config change in the window and it is the Kafka maintenance: kafka log.retention.ms 604800000 -> 259200000 (7d -> 3d) at 2026-03-23T18:00:00Z by platform-team, i.e. ~2h20m before the 20:20Z incident report. No notification-service consumer/JVM/heap/resource-limit config change appears in the results.
  - `F15` ✔ The Kafka maintenance cfg-2410 (log.retention.ms 604800000 -> 259200000, i.e. 7d -> 3d, platform-team) is timestamped 2026-03-23T18:00:00Z — ~2h before the third OOM (19:59:21Z) and ~19h after the memory ramp for that cycle already began (~03-22T21:00). Retention is a stored-data window change, not a consumer/broker-capacity change.
**Contradicting evidence**
  - `F7` ✔ Exactly three OOMKilled restarts (exit code 137) of notification-service-7d9f occurred in this week's window, at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z, and 2026-03-23T19:59:21Z — closely spaced near 22:00/20:00 daily, roughly 24h apart.
  - `F8` ✔ The restarts metric (cumulative) confirms three restarts: it steps from 0 to 1 in the 2026-03-21T22:00 bucket, to 2 in the 2026-03-22T22:00 bucket, and to 3 in the 2026-03-23T20:00 bucket.
  - `F9` ✔ memory_pct shows a sawtooth ramp-then-crash pattern preceding every OOM: it climbs steadily (~48% -> 95%+) and resets to ~48% at each restart. First OOM (03-21T22:59) was preceded by a ramp from 48.23% at 03-21T00:00 to avg 92.91/max 95.09 at 03-21T20:00 and max 97.37 at 03-21T22:00. Second (03-22T21:59) ramped 50.33 -> avg 94.91/max 97.98 at 03-22T20:00. Third (03-23T19:59) ramped 52.99 -> avg 95.05/max 98.44 at 03-23T18:00 with 20-min buckets reaching avg 96.73/max 98.44 at 19:40, then resetting to 46.72 at 20:00.
  - `F16` ✔ No code deployment at all occurred on 2026-03-23 (12:00-24:00 window, all services: count=0, not partial), so nothing was shipped in the ~12h before the third OOM; the last deployment of any kind was the unrelated auth-service jackson bump on 03-22T13:00Z.
  - `F15` ✔ The Kafka maintenance cfg-2410 (log.retention.ms 604800000 -> 259200000, i.e. 7d -> 3d, platform-team) is timestamped 2026-03-23T18:00:00Z — ~2h before the third OOM (19:59:21Z) and ~19h after the memory ramp for that cycle already began (~03-22T21:00). Retention is a stored-data window change, not a consumer/broker-capacity change.
  - `F14` ✔ There is NOT a single configuration/flag change on notification-service in the window (scope=notification-service returns count=0, not partial); the sole config change anywhere is the Kafka maintenance.
**Assumptions**
  - Retention reduction affected broker memory/backpressure in ways that reach the consumer
  - The change preceded symptom onset meaningfully
**Rejected because:** Retention is a stored-data-window setting, not a consumer/broker-capacity knob (F15). The change is ~2h before the third OOM, but the same OOM pattern occurred on 03-21 and 03-22 BEFORE any Kafka change existed (F7/F8), so it cannot explain the series. No notification-service config change exists (F14).

### H4: Resource-limit/heap config was lowered, narrowing OOM margin
A JVM heap (Xmx) or container memory-limit config change narrowed the headroom so the steady ramp reaches the limit sooner, producing the OOMKilled events.

**Causal chain**
1. Heap or container memory limit was reduced *(assumed)*
2. memory_pct ramp hits ceiling faster -> OOMKilled [F9, F7]

**Supporting evidence**
  - `F9` ✔ memory_pct shows a sawtooth ramp-then-crash pattern preceding every OOM: it climbs steadily (~48% -> 95%+) and resets to ~48% at each restart. First OOM (03-21T22:59) was preceded by a ramp from 48.23% at 03-21T00:00 to avg 92.91/max 95.09 at 03-21T20:00 and max 97.37 at 03-21T22:00. Second (03-22T21:59) ramped 50.33 -> avg 94.91/max 97.98 at 03-22T20:00. Third (03-23T19:59) ramped 52.99 -> avg 95.05/max 98.44 at 03-23T18:00 with 20-min buckets reaching avg 96.73/max 98.44 at 19:40, then resetting to 46.72 at 20:00.
**Contradicting evidence**
  - `F14` ✔ There is NOT a single configuration/flag change on notification-service in the window (scope=notification-service returns count=0, not partial); the sole config change anywhere is the Kafka maintenance.
  - `F3` ✔ Exactly one config change in the window and it is the Kafka maintenance: kafka log.retention.ms 604800000 -> 259200000 (7d -> 3d) at 2026-03-23T18:00:00Z by platform-team, i.e. ~2h20m before the 20:20Z incident report. No notification-service consumer/JVM/heap/resource-limit config change appears in the results.
  - `F1` ✔ Only one notification-service deployment in the 7 days before the incident: dep-5402 v3.8.0 at 2026-03-21T00:00:00Z adding an in-memory template cache (ConcurrentHashMap keyed by template+locale+userSegment on the email render path); status live, rolled_back_t null. It landed ~2.5 days before the report.
**Assumptions**
  - Such a limit is tracked but was missed (GAPS)
**Rejected because:** get_config_changes with scope=notification-service returned count=0, not partial (F14); the only config change anywhere is the Kafka retention (F3). No evidence a limit change occurred. Behavior is explained by memory growth, not a lowered ceiling.

## Facts (verified observations)
- The only notification-service deployment in the window (2026-03-16..24) is dep-5402 v3.8.0 at 2026-03-21T00:00:00Z, 'Add in-memory template cache for email rendering' (ConcurrentHashMap keyed by template+locale+userSegment on the email render path), status live with rolled_back_at null — the sole notification-service change preceding the first OOM at 2026-03-21T22:59:50Z. [F1, F13]
- Exactly three OOMKilled restarts (exit code 137) of notification-service-7d9f occurred, at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z, roughly 24h apart; the cumulative restarts metric steps 0->1->2->3 in the matching hourly buckets. [F7, F8]
- memory_pct shows a sawtooth ramp-then-crash before every OOM: from a ~48% reset it climbs to 95%+ (max 97.37% on 03-21, 97.98% on 03-22, 98.44% on 03-23) and resets to ~46-48% at each restart. [F9]
- Baseline memory was flat ~48% before 03-21; the ramp clearly begins in the 2026-03-21T02:00 bucket (~2h after dep-5402), with the first log anomaly being 'GC pause 253ms exceeded 200ms target' at 2026-03-21T03:00:44Z and heap-over-90% 'full GC took' warnings starting 2026-03-21T21:37:01Z, just before the first OOM. [F10]
- consumer_lag spikes immediately AFTER each memory crash, not before: lag stays in its normal ~50-100 band during the ramps, then jumps (03-21T22:00 avg 2273.11/max 17161.92; 03-22 20:00 and 22:00 buckets; 03-23 19:40 avg 4578.25/max 18137.93 while memory is already at 96.73%). [F11]
- The Kafka maintenance cfg-2410 (log.retention.ms 604800000 -> 259200000, 7d -> 3d, platform-team) is timestamped 2026-03-23T18:00:00Z — ~2h before the third OOM and ~19h after that cycle's memory ramp had already begun; retention is a stored-data-window setting, not a consumer/broker-capacity knob. [F15]
- There is no configuration/flag change on notification-service in the window (scope=notification-service returns count=0, not partial); the sole config change anywhere is the Kafka retention change. [F14, F3]
- No code deployment at all occurred on 2026-03-23 (12:00-24:00, all services: count=0, not partial); the last deployment of any kind was the unrelated auth-service v7.1.4 jackson bump on 2026-03-22T13:00Z, and the only other deployment in the window across all services is that auth-service patch. [F16, F2]
- notification-service has no incoming service edges (no caller); its load arrives only by consuming from the shared Kafka topic. Its direct dependencies are kafka (queue-consume) and email-provider (external, no telemetry). [F5, F4]
- Kafka is a shared component (shared: true, owner platform) with settlement-service as a peer consumer, so a shared-Kafka change could reach settlement-service — but the retention reduction is the only Kafka change and neither settlement-service nor the other peers (order-service, payment-service) is reported with OOM symptoms. [F17, F4]

## Assumptions (not verified)
- The dep-5402 template cache (ConcurrentHashMap keyed by template+locale+userSegment) grows without effective eviction and is the dominant allocator of the growing heap; this is inferred from timing and elimination, not from runtime attribution, since no heap/absolute-memory metric or profile was available.
- No notification-service heap (Xmx) or container memory-limit change accompanied dep-5402 that would narrow headroom; the config-change query returned no notification-service entries, but such settings may simply not be tracked there.
- no unlogged Kafka broker-side rebalance/replay occurred around 03-21..03-23; broker-side events are not queryable via the config/deployment tools, so absence of a config record does not exclude one.
- The ~24h cadence reflects the time needed to fill the heap from reset under steady traffic, rather than a daily traffic or maintenance trigger; no traffic metric was pulled to confirm this.
- (demoted from facts: no verified observation backs it) The third OOM repeats the identical sawtooth shape of the 03-21 and 03-22 events, with heap-over-90% warnings minutes before the kill (03-23 ramp warnings from 18:34:46Z, OOM at 19:59:21Z); the reported link to today's Kafka maintenance is not supported because the ramp began the prior day.
- (demoted from facts: no verified observation backs it) The only historical incident matching the symptom pair (lag spike + delayed emails) is HIST-104 (2025-11-05), attributed to a Kafka broker rebalance/partition-reassignment storm — a broker-side event class with no deployment/config change; no prior OOMKilled/memory-ramp incident for notification-service was found.

## Timeline
- `2026-03-20T00:00:00Z` Memory baseline is flat at ~48% before any change (pre-incident reference). [F10, F9]
- `2026-03-21T00:00:00Z` dep-5402 v3.8.0 deployed to notification-service — the only notification-service deployment in the window — adding an in-memory template cache (keyed by template+locale+userSegment) on the email render path; status live, not rolled back. [F1, F13]
- `2026-03-21T02:00:00Z` Memory_pct ramp begins (avg 52.71 in the 02:00 bucket) after the flat ~48% baseline. [F10, F9]
- `2026-03-21T03:00:44Z` First log anomaly: 'GC pause 253ms exceeded 200ms target'. [F10]
- `2026-03-21T21:37:01Z` Heap-over-90% 'full GC took' warnings begin, just before the first OOM. [F10]
- `2026-03-21T22:59:50Z` First OOMKilled restart (exit 137) of notification-service-7d9f after memory reached max 97.37%; consumer lag spikes immediately after (22:00 bucket avg 2273.11/max 17161.92), not before. [F7, F9, F11, F8]
- `2026-03-22T13:00:00Z` Only other deployment in the window: unrelated auth-service v7.1.4 jackson patch bump. [F2]
- `2026-03-22T21:59:47Z` Second OOMKilled restart (exit 137) after a ramp from 50.33% to avg 94.91/max 97.98%; consumer lag spikes follow the crash. [F7, F9, F11, F8]
- `2026-03-23T18:00:00Z` Kafka maintenance cfg-2410: log.retention.ms 604800000 -> 259200000 (7d -> 3d) by platform-team — ~2h before the third OOM, and ~19h after that cycle's ramp already began. [F3, F15]
- `2026-03-23T18:34:46Z` Heap-over-90% 'full GC took' warnings begin for the third ramp (memory already ~95-98%). [F12, F9]
- `2026-03-23T19:40:00Z` Memory reaches avg 96.73/max 98.44% while consumer lag jumps to avg 4578.25/max 18137.93 — lag rising with memory already saturated, i.e. consequentially. [F9, F11]
- `2026-03-23T19:59:21Z` Third OOMKilled restart (exit 137); memory resets to 46.72% at 20:00. No code deployment occurred anywhere on 03-23. [F7, F9, F16, F12]
- `2026-03-23T20:20:00Z` Incident INC-004 reported: third OOM restart this week, trade-confirmation emails delayed ~25 min; reporter hypothesises consumer choke on a Kafka replay. [F7, F4]

## Recommended actions
- **[immediate]** Roll back dep-5402 (notification-service v3.8.0 in-memory template cache) to the previous release, or disable the template-cache code path via its flag/fallback, to stop the memory ramp. -- dep-5402 is the only notification-service change in the window and the ramp begins ~2h after it, with three ~24h OOM cycles and no notification-service config change to explain them (F1, F13, F10, F9, F14). Backing it out is the direct mitigation and also the discriminating test. [F1, F13, F10, F9, F14]
- **[immediate]** Verify the rollback by watching memory_pct and restarts on notification-service-7d9f through at least the next ~24h cycle window: memory should stay near the ~48% baseline and restarts should stay flat (no 0->1 step). -- The failure signature is a clear sawtooth to 95-98% with a ~24h fill time; holding flat at baseline through one full cycle confirms the mitigation (F9, F7, F8). [F9, F7, F8]
- **[short_term]** While rolled back or immediately after, capture a heap dump / bound the cache: confirm the ConcurrentHashMap keyed by template+locale+userSegment is the dominant retained set and add an eviction policy (size cap and/or TTL) or switch to a bounded cache. -- Attribution to the cache is currently by timing and elimination only; a heap profile converts the medium-confidence inference into direct evidence and closes the main gap (F1, F13). [F1, F13]
- **[short_term]** Run the discriminating check for the runner-up hypothesis: inspect Kafka consumer-group state and broker-side partition reassignment/rebalance activity for the notification-service group (and settlement-service as peer consumer) across 03-21..03-23, and confirm whether any replay volume burst preceded a ramp. -- H2 (broker rebalance/replay) is the rejected-but-plausible alternative and broker-side events are not queryable via config/deploy tools; lag increases after crashes and ramps predate the maintenance, but an unlogged rebalance cannot be excluded without this check (F11, F15, F6, F18, F17). [F11, F15, F6, F18, F17]
- **[short_term]** Pull the adjacent-service anomaly sweep that was budget-exhausted (kafka broker and trade-service/order/payment/settlement) for OOM, lag and latency anomalies in the three OOM windows to confirm only notification-service is affected. -- The claim that only notification-service is affected is currently unverified, and a shared-Kafka effect on peers would indicate a different root cause (F4, F17). [F4, F17]
- **[long_term]** Add heap/absolute memory (used vs limit) metrics and JVM heap-size tracking for notification-service, plus alerting on the memory-ramp slope rather than only on OOM restarts. -- Only memory_pct was available, so heap vs working-set could not be quantified and the ramp was only detected at crash; slope alerting converts this class of leak into an early warning (F9). [F9]
- **[long_term]** Add CI guardrails requiring bounded/eviction-configured in-memory caches and a memory-leak soak test gating deploys on the email render path, and track JVM/heap and consumer settings in get_config_changes so headroom changes are auditable. -- The unbounded in-memory cache reached production unremarked and no heap/limit change was trackable, which is why attribution rested on timing and elimination (F1, F13, F14). [F1, F13, F14]

## Data gaps
- No heap/working-set absolute byte metric was available for notification-service (only memory_pct), so heap vs working-set breakdown and absolute memory limits could not be quantified and attribution of the growth to the template cache is by timing/elimination, not runtime profiling.
- No notification-service consumer/Kafka-consumer-group, JVM/heap (Xmx) or container resource-limit config change appears in get_config_changes (count=1 overall, not partial); cannot confirm whether headroom tuning accompanied dep-5402 or such settings are simply untracked.
- Kafka broker-side events (partition reassignment/rebalance, as in HIST-104) are not queryable via the config/deployment tools, so absence of a config record does not exclude an unlogged broker-side rebalance around 03-21..03-23.
- email-provider has has_telemetry=false and no time-series metrics were pulled for notification-service/kafka, so exact lag values and short-window onset order of lag vs memory could not be established from raw series.
- Neighbouring services (kafka, trade-service) could not be queried for OOM/lag/latency anomalies in the same windows (budget exhausted), so the claim that only notification-service is affected is unverified, and it is unknown whether the Kafka maintenance/replay reached the broker.
- The notification-service log query returned 40 of 48 matches (truncated: true); the final 8 entries 2026-03-23T18:49-19:59 were not retrieved, though a separate ERROR-only query captured all three OOM events.
- Historical search returned only HIST-104 and HIST-107; no prior HIST entry with literal 'OOMKilled' for notification-service was retrievable, so the 'third this week' history and any prior replay-triggered OOM precedent could not be independently established.
- No change is recorded between 2026-03-21T00:00Z and 03-23T12:00Z besides the Kafka retention change, so the ~24h cadence of the three restarts has no attributable triggering change other than the shared dep-5402 code path.

## What would change this conclusion
- A heap dump or profile showing the retained memory is NOT dominated by dep-5402's template cache (e.g. dominated by consumer buffers or deserialized messages) would refute H1 and move the rebalance/replay class (H2) to the top.
- The memory ramp persisting at the same ~24h cadence and reaching the same 95-98% ceiling after dep-5402 is rolled back would falsify H1 as the cause of the series.
- A broker-side partition reassignment/rebalance or replay-volume burst confirmed (via consumer-group and broker telemetry) to precede the ramps — rather than follow the crashes — would restore H2 despite the config-tool blind spot.
- Discovery of a tracked, timestamped notification-service heap (Xmx) or container memory-limit reduction narrowing headroom would support the rejected H4 and change the mitigation to a config revert.
- Evidence that settlement-service and/or other Kafka peers (order-service, payment-service) show OOM or lag anomalies in the same windows would point to a shared-Kafka cause rather than a notification-service code change.
- Evidence that memory growth is traffic-volume-coupled (a traffic metric showing end-of-day trade-confirmation peaks driving the ramp and the same sawtooth predating dep-5402) would support the rejected H5 cyclical-traffic explanation.
- A deployment/config change artifact not visible in the reviewed queries (e.g. rollback of dep-5402 having actually occurred, or a dep-5402 tag mismatch) that breaks the alignment between dep-5402 and the first ramp would weaken H1's central timing evidence.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: The third OOM repeats the identical sawtooth shape of the 03-21 and 03-22 events
- fact demoted to assumption: The only historical incident matching the symptom pair (lag spike + delayed emai

## Investigation stats
- latency 118.91s, LLM calls 13, tokens 83430 (in 66895 / out 16535)
- tool calls 23 (0 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
