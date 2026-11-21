# Incident investigation report: INC-004
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The OOMKilled restarts of notification-service are most consistent with a memory leak introduced by the notification-service v3.8.0 deploy dep-5402 (2026-03-21, "Add in-memory template cache" — TemplateCache ConcurrentHashMap), which accumulates heap over ~22h and triggers daily OOMKills — the reviewed top hypothesis H1 at confidence 0.60 (medium). The reported Kafka maintenance (cfg-2410, 2026-03-23T18:00Z) is coincidence, not cause: the memory sawtooth and the first OOM restart began 2026-03-21T22:59:50Z, roughly two days earlier, and the lag spikes recur around each restart rather than tracking the config change. Confidence is medium rather than high because there is no per-pod heap/cache telemetry proving the ConcurrentHashMap is the consumer of the memory, and the email-provider/kafka-side telemetry gaps were never closed. Immediate action is to roll the deploy back (or cap the cache), then verify the sawtooth disappears.

## Probable root cause
**notification-service v3.8.0 in-memory TemplateCache leaks, causing daily sawtooth OOMs** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: notification-service v3.8.0 in-memory TemplateCache leaks, causing daily sawtooth OOMs | bad_deployment | notification-service | dep-5402 | 0.60 (medium) | active |
| 2 | H3: Downstream email-provider dependency slow/hanging, accumulating in-flight messages and back-pressuring memory | dependency_failure | email-provider | null | 0.15 (low) | active |
| 3 | H4: Producer-side volume/duplicate change inflated payloads and lag independent of maintenance | capacity_saturation | order-service | null | 0.08 (low) | active |
| 4 | H5: Shared kafka broker maintenance/rebalance stalls the consumer (historical precedent shape) | infrastructure_failure | kafka | cfg-2410 | 0.08 (low) | active |
| 5 | H2: Kafka log.retention.ms reduction (cfg-2410) triggered a replay/backlog the consumer buffered in memory | config_change | kafka | cfg-2410 | 0.05 (low) | rejected |

### H1: notification-service v3.8.0 in-memory TemplateCache leaks, causing daily sawtooth OOMs
The notification-service v3.8.0 deploy (dep-5402, 2026-03-21) added an in-memory template cache (ConcurrentHashMap) that grows unbounded / is never evicted, so the heap fills over ~22h and the container is OOMKilled, restarting roughly daily. The Kafka maintenance is coincidence; the lag spikes are a consequence of each restart, not the cause.

**Causal chain**
1. Deploy dep-5402 v3.8.0 introduces in-memory template cache (ConcurrentHashMap) at 2026-03-21T00:00Z [F2, F13]
2. Cache grows/never evicted -> heap climbs steadily across the process lifetime *(assumed)*
3. memory_pct sawtooth ~48%->~98% over ~22h, resetting on restart; cycles start 21T00, 22T00, 22T22, 23T20 [F7]
4. Container hits limit -> OOMKilled (exit 137) and restarts at 21T22:59, 22T21:59, 23T19:59 [F8, F9, F11]
5. During restart/backlog drain, consumer_lag spikes to ~17-18k while baseline is ~55-65 [F10]
6. Backlog draining delays trade-confirmation email sends ~25 min *(assumed)*

**Supporting evidence**
  - `F2` ✔ Only two deployments occurred fleet-wide in the 7-day window: notification-service v3.8.0 on 2026-03-21 ("Add in-memory template cache for email rendering", TemplateCache ConcurrentHashMap) and auth-service v7.1.4 on 2026-03-22 (jackson bump); both are status live, neither rolled back.
  - `F7` ✔ notification-service memory_pct shows a repeating sawtooth across the full window: it climbs steadily from ~48% to near 98% over roughly 22h then resets to ~48%, with identical cycles starting 21T00:00, 22T00:00, 22T22:00 and 23T20:00. This is a multi-day recurring pattern, not a single step at today's maintenance.
  - `F8` ✔ The first OOMKilled restart this week was at 2026-03-21T22:59:50Z, with two more at 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z — so OOMs began ~2 days before the incident report and recur roughly daily, not only after today's Kafka maintenance.
  - `F9` ✔ The error signature is exclusively 'OOMKilled (exit code 137), restarting' — 3 ERROR entries total, count-by-level all ERROR, no other error types. No GC/heap warning lines appear in the returned ERROR set.
  - `F10` ✔ notification-service consumer_lag spikes to extreme values only around the OOM windows: max 17161.92 at 21T22:00, 17611.66 at 22T20:00, 18137.93 at 23T18:00; baseline buckets are ~55-65 msgs (e.g. 56.8 at 20T00:00, 57.89 at 23T22:00). Lag spikes correlate with each restart, not a continuous maintenance-driven increase.
  - `F11` ✔ notification-service restarts (cumulative) was 0 through 2026-03-21T20:00, became 1 at 22:00 on 03-21, 2 at 22:00 on 03-22, and 3 at 20:00 on 03-23 — three restarts spread across three days, matching the log OOMs.
  - `F12` ✔ Isolation check: settlement-service memory_pct stays flat ~49-50% (min 44.44, max 56.06) and order-service memory_pct stays flat ~60% (min 51.57, max 66.74) across the entire 7-day window, with no sawtooth. The memory anomaly is not present on these two services, indicating it is isolated to notification-service (other services unverified due to budget).
  - `F13` ✔ Rank 1 candidate: notification-service deploy dep-5402 v3.8.0 at 2026-03-21T00:00:00Z introduced in-memory state ('Add in-memory template cache for email rendering', TemplateCache ConcurrentHashMap). It is the only change touching notification-service, is still status 'live' (never rolled back), and precedes the first OOM by ~23h — but it predicts a gradual daily-accumulating leak, matching the T1 sawtooth (~48%→~98% per ~22h) rather than a step at the maintenance.
  - `F15` ✔ No change on the reported service coincides with onset: fleet-wide deploy query over the window returned exactly 2 deployments (neither on 03-23) and the fleet-wide config query returned exactly 1 change, on kafka. notification-service itself had zero deploys/config changes on the incident day.
**Contradicting evidence**
  - `F14` ✔ Rank 2 candidate: kafka config change cfg-2410 (log.retention.ms 604800000→259200000) at 2026-03-23T18:00:00Z is the only change near today's onset, ~2h20m before the report, but T1 shows the memory sawtooth and OOMs began 2026-03-21, two days earlier — so the retention change cannot be the initiating cause; at most it is a coincidence or a contributor to the final cycle's lag spike.
**Assumptions**
  - The ConcurrentHashMap cache-like structure is the actual memory consumer; no per-pod heap/cache telemetry exists to prove it (gap acknowledged)
  - The ~23h gap between deploy and first OOM is explained by slow accumulation rather than refuting causation
  - The ~25 min email delay is caused by the restart/backlog drain
**Rejected because:** null

### H3: Downstream email-provider dependency slow/hanging, accumulating in-flight messages and back-pressuring memory
The external email-provider (or an unmapped template/DB dependency) is slow or hanging, so the notification consumer accumulates in-flight/unacked messages and grows memory, causing OOMKills and delayed emails.

**Causal chain**
1. notification-service depends on external email-provider for send path [F1, F16]
2. email-provider becomes slow/hanging, so sends do not complete *(assumed)*
3. In-flight/unacked messages accumulate in consumer memory *(assumed)*
4. Heap fills -> OOMKilled restart, and emails are delayed [F9]

**Supporting evidence**
  - `F1` ✔ notification-service depends on shared kafka (queue-consume) and external email-provider; kafka is shared and also consumed by settlement-service and produced to by order-service and payment-service, so those are the coupling points through which a neighbouring service could hurt it.
  - `F16` ✔ Dependency map confirms notification-service's coupling points are shared kafka (queue-consume) and external email-provider; kafka is flagged shared=true, so neighbours (order-service, payment-service, settlement-service) can affect it only through that shared queue, not through a shared DB/cache.
**Contradicting evidence**
  - `F7` ✔ notification-service memory_pct shows a repeating sawtooth across the full window: it climbs steadily from ~48% to near 98% over roughly 22h then resets to ~48%, with identical cycles starting 21T00:00, 22T00:00, 22T22:00 and 23T20:00. This is a multi-day recurring pattern, not a single step at today's maintenance.
  - `F8` ✔ The first OOMKilled restart this week was at 2026-03-21T22:59:50Z, with two more at 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z — so OOMs began ~2 days before the incident report and recur roughly daily, not only after today's Kafka maintenance.
  - `F12` ✔ Isolation check: settlement-service memory_pct stays flat ~49-50% (min 44.44, max 56.06) and order-service memory_pct stays flat ~60% (min 51.57, max 66.74) across the entire 7-day window, with no sawtooth. The memory anomaly is not present on these two services, indicating it is isolated to notification-service (other services unverified due to budget).
**Assumptions**
  - email-provider has a memory/availability anomaly (never queried; email-service not_found tc-009/tc-010; gap acknowledged)
  - Accumulation mechanism would be bounded by consumer prefetch limits rather than growing monotonically to 98%
**Rejected because:** null

### H4: Producer-side volume/duplicate change inflated payloads and lag independent of maintenance
An upstream producer change (order-service/payment-service) increased event rate or message size, inflating notification-service consumer lag and memory independent of both the deploy and the Kafka maintenance.

**Causal chain**
1. order-service and payment-service produce to the shared kafka topic consumed by notification-service [F1, F16]
2. A producer-side change raised event volume/size *(assumed)*
3. notification-service lags and buffers more messages, growing memory *(assumed)*
4. Heap fills -> OOMKilled, emails delayed [F9]

**Supporting evidence**
  - `F1` ✔ notification-service depends on shared kafka (queue-consume) and external email-provider; kafka is shared and also consumed by settlement-service and produced to by order-service and payment-service, so those are the coupling points through which a neighbouring service could hurt it.
  - `F16` ✔ Dependency map confirms notification-service's coupling points are shared kafka (queue-consume) and external email-provider; kafka is flagged shared=true, so neighbours (order-service, payment-service, settlement-service) can affect it only through that shared queue, not through a shared DB/cache.
**Contradicting evidence**
  - `F4` ✔ No deployments and no notification-service config changes occurred on 2026-03-23: the all-services deploy query for that day returned count 0 and the notification-service config query for the full week returned count 0, so nothing on the reported service changed on the incident day.
  - `F7` ✔ notification-service memory_pct shows a repeating sawtooth across the full window: it climbs steadily from ~48% to near 98% over roughly 22h then resets to ~48%, with identical cycles starting 21T00:00, 22T00:00, 22T22:00 and 23T20:00. This is a multi-day recurring pattern, not a single step at today's maintenance.
  - `F8` ✔ The first OOMKilled restart this week was at 2026-03-21T22:59:50Z, with two more at 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z — so OOMs began ~2 days before the incident report and recur roughly daily, not only after today's Kafka maintenance.
  - `F15` ✔ No change on the reported service coincides with onset: fleet-wide deploy query over the window returned exactly 2 deployments (neither on 03-23) and the fleet-wide config query returned exactly 1 change, on kafka. notification-service itself had zero deploys/config changes on the incident day.
**Assumptions**
  - No producer deploy exists in the window (F2 says only 2 fleet-wide deploys, neither a producer); a config-only producer change was not searched (gap acknowledged)
**Rejected because:** null

### H5: Shared kafka broker maintenance/rebalance stalls the consumer (historical precedent shape)
A Kafka broker/partition reassignment or rebalance stalls notification-service's consumer, spiking lag and delaying emails — mirroring HIST-104/HIST-112.

**Causal chain**
1. notification-service consumes from shared kafka broker [F1, F16]
2. Broker rebalance/reassignment during maintenance stalls the consumer [F6, F17]
3. consumer_lag spikes and emails are delayed [F10]
4. Stall leads to buffered messages and memory exhaustion -> OOMKilled *(assumed)*

**Supporting evidence**
  - `F6` ✔ Historical precedents with the same symptom shape are Kafka broker/partition-maintenance issues, not memory leaks: HIST-104 (2025-11-05) notification-service consumer lag after Kafka broker rebalance, and HIST-112 (2026-02-03) settlement-service consumer stalled after partition reassignment — both root-caused to partition reassignment/rebalancing, and both are leads only.
  - `F17` ✔ Historical precedents with the same SYMPTOMS (consumer lag spike + delayed emails) are Kafka broker-maintenance/rebalance issues, not memory leaks: HIST-104 (2025-11-05, notification-service lag after broker rebalance) and HIST-112 (2026-02-03, settlement consumer stalled after partition reassignment). These are leads only; neither is an OOM/memory-exhaustion root cause, so they do not support the replay theory as the OOM mechanism.
  - `F10` ✔ notification-service consumer_lag spikes to extreme values only around the OOM windows: max 17161.92 at 21T22:00, 17611.66 at 22T20:00, 18137.93 at 23T18:00; baseline buckets are ~55-65 msgs (e.g. 56.8 at 20T00:00, 57.89 at 23T22:00). Lag spikes correlate with each restart, not a continuous maintenance-driven increase.
**Contradicting evidence**
  - `F7` ✔ notification-service memory_pct shows a repeating sawtooth across the full window: it climbs steadily from ~48% to near 98% over roughly 22h then resets to ~48%, with identical cycles starting 21T00:00, 22T00:00, 22T22:00 and 23T20:00. This is a multi-day recurring pattern, not a single step at today's maintenance.
  - `F8` ✔ The first OOMKilled restart this week was at 2026-03-21T22:59:50Z, with two more at 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z — so OOMs began ~2 days before the incident report and recur roughly daily, not only after today's Kafka maintenance.
  - `F11` ✔ notification-service restarts (cumulative) was 0 through 2026-03-21T20:00, became 1 at 22:00 on 03-21, 2 at 22:00 on 03-22, and 3 at 20:00 on 03-23 — three restarts spread across three days, matching the log OOMs.
  - `F12` ✔ Isolation check: settlement-service memory_pct stays flat ~49-50% (min 44.44, max 56.06) and order-service memory_pct stays flat ~60% (min 51.57, max 66.74) across the entire 7-day window, with no sawtooth. The memory anomaly is not present on these two services, indicating it is isolated to notification-service (other services unverified due to budget).
**Assumptions**
  - A stall mechanism producing steady heap growth to 98% (rather than transient lag) exists — precedents show lag stalls, not OOMs
**Rejected because:** null

### H2: Kafka log.retention.ms reduction (cfg-2410) triggered a replay/backlog the consumer buffered in memory
The Kafka maintenance config change cfg-2410 (retention 7d -> 3d, 2026-03-23T18:00Z) caused retention/compaction churn or a broker-side event that made notification-service re-read or buffer a large backlog, exhausting memory.

**Causal chain**
1. cfg-2410 reduces log.retention.ms from 7d to 3d at 2026-03-23T18:00Z [F3]
2. Retention shortening causes segment deletion/churn or a replay the consumer must reprocess *(assumed)*
3. Consumer buffers the backlog in memory and grows the heap *(assumed)*
4. OOM reported 2h20m later at 20:20Z [F5]

**Supporting evidence**
  - `F3` ✔ The only config change fleet-wide in the window is on kafka: cfg-2410, log.retention.ms reduced from 604800000 (7d) to 259200000 (3d) at 2026-03-23T18:00:00Z, described as "Kafka maintenance: reduce retention to 3 days" — this is the change just before the 20:20Z OOM report.
  - `F5` ✔ The gap between the Kafka retention config change (18:00Z) and the OOM report (20:20Z) is ~2h20m, and the notification-service template-cache deploy (2026-03-21) precedes onset by ~2.8 days; the notification-service deploy introduced in-memory state (a ConcurrentHashMap cache) which is directly relevant to a memory-exhaustion symptom.
**Contradicting evidence**
  - `F7` ✔ notification-service memory_pct shows a repeating sawtooth across the full window: it climbs steadily from ~48% to near 98% over roughly 22h then resets to ~48%, with identical cycles starting 21T00:00, 22T00:00, 22T22:00 and 23T20:00. This is a multi-day recurring pattern, not a single step at today's maintenance.
  - `F8` ✔ The first OOMKilled restart this week was at 2026-03-21T22:59:50Z, with two more at 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z — so OOMs began ~2 days before the incident report and recur roughly daily, not only after today's Kafka maintenance.
  - `F10` ✔ notification-service consumer_lag spikes to extreme values only around the OOM windows: max 17161.92 at 21T22:00, 17611.66 at 22T20:00, 18137.93 at 23T18:00; baseline buckets are ~55-65 msgs (e.g. 56.8 at 20T00:00, 57.89 at 23T22:00). Lag spikes correlate with each restart, not a continuous maintenance-driven increase.
  - `F11` ✔ notification-service restarts (cumulative) was 0 through 2026-03-21T20:00, became 1 at 22:00 on 03-21, 2 at 22:00 on 03-22, and 3 at 20:00 on 03-23 — three restarts spread across three days, matching the log OOMs.
  - `F14` ✔ Rank 2 candidate: kafka config change cfg-2410 (log.retention.ms 604800000→259200000) at 2026-03-23T18:00:00Z is the only change near today's onset, ~2h20m before the report, but T1 shows the memory sawtooth and OOMs began 2026-03-21, two days earlier — so the retention change cannot be the initiating cause; at most it is a coincidence or a contributor to the final cycle's lag spike.
**Assumptions**
  - Retention reduction produces a replay at all (no broker metrics retrieved; gap acknowledged)
**Rejected because:** Timing refutes it as the initiating cause: the memory sawtooth and OOM restarts began 2026-03-21T22:59Z (F7, F8, F11), ~2 days BEFORE cfg-2410 (F3). Retention shortening deletes old segments; it does not lengthen retention, so there is no mechanism for re-consuming old data. Lag spikes correlate with each restart rather than with the 18:00Z change (F10).

## Facts (verified observations)
- The only notification-service change in the 7-day window is deploy dep-5402, v3.8.0 on 2026-03-21, described as 'Add in-memory template cache for email rendering' with a TemplateCache ConcurrentHashMap; it is still live and was never rolled back. [F2]
- notification-service had zero deployments and zero config changes on the incident day (2026-03-23); fleet-wide there were exactly 2 deployments and exactly 1 config change (the Kafka retention change). [F4, F15]
- notification-service memory_pct follows a repeating sawtooth, climbing from ~48% to near 98% over roughly 22h then resetting to ~48%, with identical cycles starting 03-21T00:00, 03-22T00:00, 03-22T22:00 and 03-23T20:00. [F7]
- The first OOMKilled restart occurred at 2026-03-21T22:59:50Z, with two more at 2026-03-22T21:59:47Z and 2026-03-23T19:59:21Z — roughly daily, and beginning ~2 days before the reported incident and before cfg-2410. [F8, F11]
- The only errors logged are 'OOMKilled (exit code 137), restarting' (3 ERROR entries, all ERROR, none other); no GC/heap warnings were returned. [F9]
- notification-service consumer_lag spikes to extreme values only around the OOM windows (max 17161.92 at 03-21T22:00, 17611.66 at 03-22T20:00, 18137.93 at 03-23T18:00) while baseline buckets are ~55-65 msgs, so lag correlates with each restart rather than with a continuous maintenance-driven increase. [F10]
- The Kafka config change cfg-2410 (log.retention.ms 604800000 -> 259200000, 7d -> 3d) occurred at 2026-03-23T18:00:00Z, about 2h20m before the OOM report, but the memory sawtooth and OOMs began 2026-03-21, two days earlier, so it cannot be the initiating cause. [F3]
- The memory anomaly is isolated to notification-service: settlement-service memory_pct stays ~49-50% and order-service ~60% flat across the whole 7-day window with no sawtooth. [F12]
- notification-service's only coupling points are shared kafka (queue-consume) and the external email-provider; kafka is shared with settlement-service (consumer) and order-service/payment-service (producers). [F1, F16]
- Historical precedents (HIST-104, HIST-112) with the same symptom shape (consumer lag spike + delayed emails) were Kafka broker/partition-rebalance issues, not memory-exhaustion/OOM root causes. [F6, F17]

## Assumptions (not verified)
- The ConcurrentHashMap TemplateCache introduced by dep-5402 is the actual memory consumer; no per-pod heap/cache-size telemetry exists to prove it, so the link is by timing plus change content.
- The ~23h gap between the deploy and the first OOM is explained by slow accumulation rather than by refuting causation.
- The ~25 min trade-confirmation email delay is caused by the restart/backlog drain rather than by an independent downstream email-provider failure.
- The 2h metric bucket size means the exact onset minute within a bucket is approximate; the onset is inferred to be within the bucket containing each restart.
- No undiscovered change exists on a mail/SMTP gateway or template/DB dependency that the dependency graph fails to expose.

## Timeline
- `2026-03-21T00:00:00Z` Deploy dep-5402: notification-service v3.8.0 ships 'Add in-memory template cache for email rendering' (TemplateCache ConcurrentHashMap); still live, never rolled back. [F2, F13]
- `2026-03-21T22:59:50Z` First OOMKilled restart of notification-service (exit code 137) this week; restart counter goes 0 -> 1. Happens ~2 days before the Kafka retention change. [F8, F11]
- `2026-03-22T21:59:47Z` Second OOMKilled restart, ~23h after the first, consistent with the ~22h sawtooth cycle. [F8, F11]
- `2026-03-23T18:00:00Z` Kafka config change cfg-2410 reduces log.retention.ms from 7d to 3d as 'Kafka maintenance' — the change just before the reported OOM. [F3]
- `2026-03-23T19:59:21Z` Third OOMKilled restart; memory had climbed back to near 98% and consumer lag peaked around this window (~18137 msgs at the 18:00Z bucket). [F8, F10, F11]
- `2026-03-23T20:20:00Z` Incident INC-004 reported (SEV-3): third OOM restart this week, trade-confirmation emails delayed ~25 min; initial suspicion was a Kafka replay after maintenance. []

## Recommended actions
- **[immediate]** Roll back or disable the v3.8.0 template cache: revert deploy dep-5402 on notification-service to the pre-3.8.0 image, or (faster, if rollback is slow) gate the TemplateCache behind a kill-switch and restart the pods to reset the heap. -- H1 is the only change touching notification-service and is the single artefact matching a gradual heap-filling sawtooth; removing it directly tests and, if correct, resolves the OOMs. [F2, F13, F7, F8]
- **[immediate]** Do NOT roll back the Kafka retention change cfg-2410 as the OOM mitigation; leave or restore log.retention.ms = 604800000 (7d) only if a separate retention requirement calls for it. -- cfg-2410 post-dates onset by ~2 days and retention shortening deletes old segments rather than causing replay, so it is not the cause and changing it will not stop the restarts. [F3, F14]
- **[short_term]** Add per-pod heap/GC and TemplateCache size (entry count / approximate byte size) telemetry to notification-service, and add a bounded cache with eviction (max size + TTL) so the cache cannot grow monotonically. -- Closes the key gap: it proves whether the ConcurrentHashMap is the memory consumer and prevents recurrence if the mechanism is confirmed. [F2, F13, F7]
- **[short_term]** Verify the fix by watching notification-service memory_pct for at least 48h after rollback/fix: the ~48% -> ~98% sawtooth with a reset every ~22h and the daily OOMKilled restarts must both disappear. -- The sawtooth over roughly 22h with daily restarts is the distinguishing signature of H1; its disappearance confirms the fix and contradicts alternatives. [F7, F8, F11]
- **[short_term]** If OOMs continue after rollback: capture heap dumps at ~90% memory on the affected pod, and close the two low-confidence branches — check kafka-side broker metrics (group lag cluster-wide, partition reassignment events) and email-provider latency/availability — to discriminate H3 and H5 from H1. -- These are the two remaining live hypotheses (H3 dependency back-pressure, H5 broker maintenance) that the current data can only weakly exclude; cluster-wide lag and email-provider latency would separate them from an isolated consumer-side leak. [F1, F10, F12, F16]
- **[long_term]** Add OOMKill/restart-rate and memory-sawtooth alerting on notification-service (and consumers generally) so a daily restart cycle is caught on the first recurrence rather than the third, and enforce a cache-ownership review in the deploy checklist for services adding unbounded in-memory state. -- Three restarts over three days went unreported until the third; earlier detection plus a review gate prevents this class of leak from reaching production and from being misattributed to coincident infrastructure maintenance. [F8, F11, F9]

## Data gaps
- No per-pod heap/cache-size telemetry for the v3.8.0 TemplateCache — only coarse 2h-bucketed memory_pct — so the leak's precise mechanism is unproven (ranked by timing and change content only).
- No kafka-side metrics were queried (broker memory, disk/retention churn, replay volume, cluster-wide consumer lag), so the retention change producing any replay cannot be fully excluded from data — only contradicted by timing.
- email-provider (and the nonexistent 'email-service', tc-009/tc-010 not_found) was never successfully queried for memory/latency, so a downstream email-path anomaly as a contributor to the delayed emails cannot be ruled out.
- The dependency graph exposes only kafka queue-consume and email-provider; a mail/SMTP gateway or template/DB dependency is unmapped, so changes there are unchecked.
- No deployment/metric data for 2026-03-16..2026-03-22 on kafka or producers (order-service, payment-service); producer-side config/volume changes cannot be excluded.
- No WARN-level GC/heap-warning logs were returned (ERROR-level query only), so heap/GC pressure signatures immediately preceding restarts are uncharacterized.
- No prior OOMKilled/memory-leak incident for notification-service was found in history, so there is no precedent confirming the in-memory-cache leak pattern.

## What would change this conclusion
- A heap dump or per-pod heap/cache telemetry showing the TemplateCache ConcurrentHashMap is not the dominant memory consumer would undermine H1 and shift weight to H3 (downstream dependency back-pressure) or another cause.
- Evidence that memory grows on notification-service only when a downstream dependency (email-provider) is slow — e.g. in-flight/unacked message count tracking heap growth — would support H3 over a self-contained cache leak.
- Observation of cluster-wide consumer lag spikes affecting settlement-service and other kafka consumers at the same times would support H5 (broker/partition maintenance) over an isolated notification-service leak.
- Discovery of a producer-side volume/message-size change or a mail/SMTP/template-DB config change in the window would introduce an alternative explanation competing with H1.
- A successful rollback/revert of dep-5402 that does NOT stop the sawtooth and restarts would falsify H1 and reopen the remaining hypotheses.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6

## Investigation stats
- latency 132.61s, LLM calls 12, tokens 68229 (in 53456 / out 14773)
- tool calls 20 (2 failed, 0 partial, 0 recovered by retry), findings 17/17 verified
