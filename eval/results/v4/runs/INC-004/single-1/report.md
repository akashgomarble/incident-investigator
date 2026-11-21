# Incident investigation report: INC-004
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The third OOMKill of notification-service (2026-03-23T19:59:21Z, exit 137) is the latest in a deterministic ~24-hour sawtooth caused by deployment dep-5402 (v3.8.0, 2026-03-21T00:00Z), which added an unbounded in-memory template cache (ConcurrentHashMap keyed by template+locale+userSegment). Memory was flat ~48% before the deploy, then rose ~5%/hour to ~95-98% and was OOMKilled three times in a row (03-21T22:59, 03-22T21:59, 03-23T19:59), with the restarts counter incrementing 0->1->2->3 in lockstep. Consumer-lag spikes (to ~18k at 19:45-20:15 on 03-23) and ~25-min email delays are downstream effects of the thrashing/OOMing consumer. The incident report's Kafka-replay theory is contradicted: the only Kafka change (cfg-2410, 03-23T18:00) REDUCED retention (7d->3d, not a replay), arrived when memory was already 94.63% mid-cycle, and the two earlier OOMKills occurred with no Kafka change at all. CPU (~25%), request_rate (~140/s) and latency (~110ms) were all flat, ruling out load/capacity causes.

## Probable root cause
**Unbounded in-memory template cache leak from dep-5402 causes cyclic OOMKills** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded in-memory template cache leak from dep-5402 causes cyclic OOMKills | bad_deployment | notification-service | dep-5402 | 0.85 (high) | active |
| 2 | H2: Kafka retention maintenance triggered a replay that overloaded the consumer | dependency_failure | kafka | cfg-2410 | 0.05 (low) | rejected |
| 3 | H3: Traffic/capacity saturation overwhelmed the consumer | capacity_saturation | notification-service | - | 0.03 (low) | rejected |

### H1: Unbounded in-memory template cache leak from dep-5402 causes cyclic OOMKills
notification-service v3.8.0 (dep-5402) added a ConcurrentHashMap template cache keyed by template+locale+userSegment with no eviction/size bound. Retained cache entries grow the old-gen heap ~5%/hour, reaching ~95-98% in ~24h then triggering an OOMKill (exit 137), after which the process restarts, cache is empty, and the cycle repeats. Consumer-lag spikes and delayed emails are downstream effects of the thrashing/OOMing consumer, not the cause.

**Causal chain**
1. dep-5402 (v3.8.0) introduces unbounded ConcurrentHashMap template cache [F1]
2. Suspected cache retention drives old-gen heap up ~5%/hr from deploy [F2, F3, F4, F13]
3. Heap reaches ~95-98% and container is OOMKilled (exit 137) [F5, F13]
4. Restart clears cache; cycle repeats ~every 24h (3 restarts) [F6, F2]
5. Consumer lag spikes and email delivery stalls during thrash/OOM [F10]

**Supporting evidence**
  - `F1` ✔ notification-service was deployed with v3.8.0 (dep-5402) at 2026-03-21T00:00:00Z, adding an in-memory template cache: 'Add in-memory template cache for email rendering' / 'TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment'.
  - `F2` ✔ notification-service memory_pct shows a repeating sawtooth: it climbs ~5%/hour from ~48% up to ~95-98% then resets, three times. Baselines just before each climb are ~47-49%.
  - `F3` ✔ Before the deploy, memory was flat/stable: 03-20 buckets all ~47.5-48.9% avg with max never above 55%.
  - `F4` ✔ The leak onset coincides exactly with dep-5402: memory is 47.68% at 2026-03-21T00:00 (deploy time) and already 51.01% at 01:30, rising monotonically thereafter.
  - `F5` ✔ Three OOMKills occurred, one per ~24h cycle, each right after memory reached ~95-98%: 2026-03-21T22:59:50, 2026-03-22T21:59:47, 2026-03-23T19:59:21 (all 'OOMKilled (exit code 137)').
  - `F6` ✔ The restarts counter is 0 through 2026-03-21T18:00 and increments to 1 (~03-21T21-24), 2 (~03-22T21-24) and 3 (~03-23T18-21), matching the three OOM events.
  - `F10` ✔ notification-service consumer_lag was normal (~40-90) through 19:30 on 03-23 and only spiked to 6082.94 avg / 18137.93 max at 19:45-20:15, AFTER memory had already been ~94-98% and the container was thrashing prior to the 19:59 OOM.
  - `F11` ✔ request_rate was flat (~131-151 req/s) across 03-22 and 03-23 including the incident window, with no load surge.
  - `F12` ✔ CPU stayed flat ~25% throughout (min 21.78, max 28.5) and latency_p99 stayed flat ~104-124ms, i.e. no CPU saturation or latency blowup preceding the OOM.
  - `F13` ✔ notification-service logs show repeated 'heap usage N% over 90 (old gen), full GC took Nms' warnings escalating from 2026-03-21T21:37 onward, with GC pauses growing to multi-second, consistent with heap exhaustion from retained objects.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The template cache is the object retaining heap (no heap-dump/object profiler data was available to name the retaining class)
  - No hidden memory-limit reduction or unrelated leak source exists

### H2: Kafka retention maintenance triggered a replay that overloaded the consumer
The 2026-03-23T18:00 Kafka retention change (cfg-2410) caused a message replay/backlog that the consumer could not absorb, driving memory and lag until OOMKill.

**Causal chain**
1. Kafka retention change at 18:00 alters log availability [F7]
2. Consumer sees replay/backlog and grows memory *(assumed)*

**Supporting evidence**
  - `F10` ✔ notification-service consumer_lag was normal (~40-90) through 19:30 on 03-23 and only spiked to 6082.94 avg / 18137.93 max at 19:45-20:15, AFTER memory had already been ~94-98% and the container was thrashing prior to the 19:59 OOM.
**Contradicting evidence**
  - `F8` ✔ At the Kafka change time (2026-03-23T18:00) notification-service memory was already 94.63% avg (95.05% in the 2h view) — well into the third leak cycle that began ~2026-03-22T22.
  - `F9` ✔ Two earlier OOMKills (2026-03-21T22:59:50 and 2026-03-22T21:59:47) occurred with no Kafka maintenance in the window; the Kafka retention change on 03-23T18:00 was the only Kafka config change across 03-20 to 03-23.
  - `F2` ✔ notification-service memory_pct shows a repeating sawtooth: it climbs ~5%/hour from ~48% up to ~95-98% then resets, three times. Baselines just before each climb are ~47-49%.
  - `F3` ✔ Before the deploy, memory was flat/stable: 03-20 buckets all ~47.5-48.9% avg with max never above 55%.
  - `F7` ✔ The only Kafka change was at 2026-03-23T18:00:00Z (cfg-2410), reducing log.retention.ms from 604800000 to 259200000 (7d -> 3d) — a retention REDUCTION, described as 'reduce retention to 3 days'.
**Assumptions**
  - A retention reduction (7d->3d) could produce a replay, which is contrary to normal Kafka semantics
**Rejected because:** Kafka change reduces retention (deletes data, does not replay); memory was already 94.63% at 18:00 and climbing since 03-22T22; the two prior OOMKills (03-21, 03-22) had no Kafka change; leak started precisely at the 03-21 deploy, ~2 days earlier.

### H3: Traffic/capacity saturation overwhelmed the consumer
A surge in load or under-provisioned capacity caused the consumer to saturate memory and be OOMKilled.

**Causal chain**
1. Load rises above provisioned capacity *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ request_rate was flat (~131-151 req/s) across 03-22 and 03-23 including the incident window, with no load surge.
  - `F12` ✔ CPU stayed flat ~25% throughout (min 21.78, max 28.5) and latency_p99 stayed flat ~104-124ms, i.e. no CPU saturation or latency blowup preceding the OOM.
**Assumptions**
  - Request rate or CPU would show a surge
**Rejected because:** request_rate was flat ~131-151 req/s and CPU flat ~25% with flat latency; memory climbed linearly regardless of traffic, inconsistent with load-driven saturation.

## Facts (verified observations)
- notification-service v3.8.0 (dep-5402, 2026-03-21T00:00Z) added an in-memory template cache (ConcurrentHashMap keyed by template+locale+userSegment). [F1]
- Memory was flat ~47.5-48.9% before the deploy and began a monotonic ~5%/hour climb at the deploy time. [F2, F3, F4]
- Memory peaked ~95-98% and was OOMKilled (exit 137) three times ~24h apart: 2026-03-21T22:59:50, 2026-03-22T21:59:47, 2026-03-23T19:59:21. [F5, F2]
- The restarts counter went 0 -> 1 -> 2 -> 3 matching the three OOM events. [F6]
- The only Kafka change was cfg-2410 at 2026-03-23T18:00 reducing log.retention.ms 7d->3d; at that time memory was already 94.63%. [F7, F8]
- The two earlier OOMKills happened with no Kafka config change in the window. [F9]
- Consumer lag was normal until 19:45-20:15 on 03-23 (spike to max 18137.93) after memory was already ~94-98%. [F10]
- request_rate (~131-151/s), CPU (~25%) and latency_p99 (~104-124ms) were all flat; no load surge. [F11, F12]
- Logs show escalating 'heap usage N over 90 (old gen)' full-GC warnings with multi-second pauses before each OOM. [F13]

## Assumptions (not verified)
- The template cache is the object retaining heap; no heap-dump/object profiler data was available to name the retaining class, so the attribution rests on timing and the deploy change description.
- No concurrent hidden memory-limit reduction or unrelated leak source contributed; the sawtooth pre-existed any Kafka change.
- The 'delayed emails ~25 min' symptom aligns with the OOM/thrash window (reported, not directly measured in logs).

## Timeline
- `2026-03-20T00:00:00Z` notification-service memory flat and stable ~48%; restarts counter 0. [F3, F6]
- `2026-03-21T00:00:00Z` Deploy dep-5402 v3.8.0 adds in-memory template cache; memory begins climbing from 47.68%. [F1, F4]
- `2026-03-21T21:37:00Z` First 'heap usage over 90 (old gen)' full-GC warnings appear. [F13]
- `2026-03-21T22:59:50Z` First OOMKill (exit 137); restart #1. [F5, F6]
- `2026-03-22T21:59:47Z` Second OOMKill (exit 137); restart #2 (no Kafka change involved). [F5, F6, F9]
- `2026-03-23T18:00:00Z` Kafka maintenance cfg-2410 reduces log.retention.ms 7d->3d; notification memory already 94.63%. [F7, F8]
- `2026-03-23T19:45:00Z` Consumer lag spikes to avg 6082.94 / max 18137.93 as consumer thrashes. [F10]
- `2026-03-23T19:59:21Z` Third OOMKill (exit 137); restart #3; trade-confirmation emails delayed ~25 min. Reported 20:20. [F5, F6]

## Recommended actions
- **[immediate]** Roll back notification-service from v3.8.0 (dep-5402) to the prior version immediately; confirm memory returns to the flat ~48% baseline. -- The leak began exactly at the deploy and every ~24h cycle reproduces it; rollback removes the faulty cache code. [F1, F2, F4]
- **[short_term]** Bound the template cache: add a maximum size and an eviction policy (e.g. Caffeine/Guava with TTL + maxEntries), or key only by template (not per userSegment which is unbounded). -- An unbounded ConcurrentHashMap keyed by userSegment grows without limit; bounding it stops heap growth. [F1, F2]
- **[short_term]** Raise memory limits and add a heap-usage alarm near 85% with periodic JVM heap dumps so leaks are caught before OOMKill. -- Each cycle took ~24h to OOM undetected; earlier alerting and heap dumps would confirm the retaining class. [F2, F13]
- **[short_term]** Do NOT treat the Kafka retention change as the trigger; verify broker health (under_replicated_partitions) separately before any Kafka remediation. -- Evidence contradicts the replay theory: retention was reduced and memory was already climbing 2 days before the Kafka change. [F7, F8, F9]
- **[long_term]** Add a regression test / load soak that runs notification-service for >24h and asserts heap stays bounded with the template cache enabled. -- Prevents reintroduction of an unbounded-cache leak that only manifests on ~24h timescales. [F2, F5]

## Data gaps
- No heap dump or object-allocation profile was available to conclusively identify the retaining object (the ConcurrentHashMap cache is inferred from dep-5402's change summary and timing, not directly observed as the heap retainer).
- email-provider has no telemetry (has_telemetry:false), so its health during the delay could not be assessed.
- kafka exposes no consumer_lag metric and no broker-side broker/under_replicated_partitions series was pulled, so a broker-side contribution cannot be fully excluded even though it is strongly contradicted.
- No exact per-minute memory series immediately around each OOMKill was fetched; buckets are 90-120min, though the resets and restart counter align tightly.

## Investigation stats
- latency 74.68s, LLM calls 5, tokens 58268 (in 49776 / out 8492)
- tool calls 15 (1 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
