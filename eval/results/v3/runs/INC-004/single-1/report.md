# Incident investigation report: INC-004
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
notification-service is OOMKilling roughly every 24h due to a memory leak introduced by dep-5402 (v3.8.0, deployed 2026-03-21T00:00Z), which added an unbounded in-memory ConcurrentHashMap TemplateCache. The cache log shows size growing 9000->18000 entries with evictions=0; memory_pct climbs linearly from ~48% to 95-98% then resets on each restart (counter 0->1->2->3 at 03-21T22, 03-22T22, 03-23T20). CPU stays flat at ~25%, ruling out load saturation. The reported Kafka-maintenance/replay theory is contradicted: identical consumer-lag spikes occurred on 03-21 and 03-22 with no maintenance, and memory growth began ~60h before the 03-23T18:00Z retention change. Delayed trade-confirmation emails are a downstream effect of each OOM/GC-thrash episode.

## Probable root cause
**Unbounded in-memory TemplateCache added in dep-5402 leaks memory, causing periodic OOM** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded in-memory TemplateCache added in dep-5402 leaks memory, causing periodic OOM | bad_deployment | notification-service | dep-5402 | 0.90 (high) | active |
| 2 | H2: Kafka maintenance (retention change) triggered consumer replay that OOMs the consumer | config_change | kafka | cfg-2410 | 0.08 (low) | rejected |
| 3 | H3: External email-provider or infrastructure fault causing backlog/OOM | dependency_failure | email-provider | - | 0.05 (low) | rejected |

### H1: Unbounded in-memory TemplateCache added in dep-5402 leaks memory, causing periodic OOM
Deployment dep-5402 (v3.8.0, 2026-03-21T00:00Z) added a ConcurrentHashMap TemplateCache with no eviction/expiry. Entries accumulate monotonically (~9000/hr, evictions=0), growing the old-gen heap until the container exceeds its limit and is OOMKilled ~every 24h; the restart clears the heap and the cycle repeats. Consumer lag spikes and delayed emails are downstream effects of each OOM/GC-thrashing episode.

**Causal chain**
1. dep-5402 adds unbounded ConcurrentHashMap template cache on 03-21T00:00Z [F3]
2. cache grows ~9000 entries/hr with 0 evictions [F4]
3. memory_pct climbs linearly from ~48% starting 03-21T00:00Z [F1, F2]
4. heap saturation -> full-GC thrash WARNs -> OOMKilled exit 137 [F6]
5. restart resets memory -> sawtooth repeats ~24h (restarts 0->1->2->3) [F1, F5]
6. consumer lag/5xx spikes during each episode -> delayed emails [F8, F10]

**Supporting evidence**
  - `F1` ✔ notification-service memory_pct shows a repeated sawtooth: rising linearly from ~48% to 95-98% over ~24h then dropping back to ~48% (restart). Baseline before 03-21T00:00 was flat at ~47-48%.
  - `F2` ✔ The memory climb starts exactly at 2026-03-21T00:00Z; the 5-minute-resolution series shows flat ~48% through 03-21T01:00 then steady growth (49.52 -> 72.64 by 11:50).
  - `F3` ✔ Deployment dep-5402 shipped to notification-service v3.8.0 at 2026-03-21T00:00:00Z with change 'Add in-memory template cache for email rendering' (ConcurrentHashMap keyed by template+locale+userSegment); status live, never rolled back.
  - `F4` ✔ TemplateCache stats logs show unbounded growth with zero evictions: size=9000 entries at 21:00:44 and size=18000 entries at 22:00:34, both with evictions=0 (~9000 entries/hour).
  - `F5` ✔ restarts counter (cumulative) went 0 -> 1 at 03-21T22:00 bucket, 1 -> 2 at 03-22T22:00 bucket, 2 -> 3 at 03-23T20:00 bucket.
  - `F6` ✔ Three OOMKilled ERROR log lines: 03-21T22:59:50Z, 03-22T21:59:47Z, 03-23T19:59:21Z, each 'container notification-service-7d9f OOMKilled (exit code 137), restarting', each preceded by a burst of 'heap usage ... over 90 (old gen), full GC took' WARNs.
  - `F7` ✔ CPU is essentially flat at ~25% (min 21.78, max 28.5) across the whole window, with no rise during memory growth.
  - `F8` ✔ consumer_lag spikes (avg 810-2273, max 14375-18137) occur in three buckets: 03-21T22:00 (2273/17161), 03-22T20:00-22:00 (790/17611 and 1589/14375), 03-23T18:00-20:00 (810/18137, 1542/14213). Two of these spikes are NOT after any Kafka maintenance.
  - `F10` ✔ error_rate (HTTP 5xx) is ~0.2% normally but rises to 1.36%/1.29% avg (max 13.96%) only in the 03-23T19:00 and 20:00 buckets, coinciding with the third OOM, then returns to ~0.2%.
**Contradicting evidence**
  - (none)
**Assumptions**
  - TemplateCache entries are retained per unique template+locale+segment with no bound (log shows evictions=0)
  - no other memory consumer was introduced in the same window

### H2: Kafka maintenance (retention change) triggered consumer replay that OOMs the consumer
The reported theory: cfg-2410 (retention 7d->3d) at 03-23T18:00Z caused a replay/lag spike that made the consumer choke and OOM. Only weakly supported: the 03-23 18:00 lag spike coincides with the maintenance, but identical spikes occurred on 03-21 and 03-22 without any maintenance, and the memory sawtooth predates the maintenance by two days.

**Causal chain**
(none given)

**Supporting evidence**
  - `F8` ✔ consumer_lag spikes (avg 810-2273, max 14375-18137) occur in three buckets: 03-21T22:00 (2273/17161), 03-22T20:00-22:00 (790/17611 and 1589/14375), 03-23T18:00-20:00 (810/18137, 1542/14213). Two of these spikes are NOT after any Kafka maintenance.
  - `F9` ✔ The only config change in the window is cfg-2410 on kafka (log.retention.ms 604800000 -> 259200000, i.e. 7d->3d retention) at 2026-03-23T18:00:00Z by platform-team.
**Contradicting evidence**
  - `F1` ✔ notification-service memory_pct shows a repeated sawtooth: rising linearly from ~48% to 95-98% over ~24h then dropping back to ~48% (restart). Baseline before 03-21T00:00 was flat at ~47-48%.
  - `F2` ✔ The memory climb starts exactly at 2026-03-21T00:00Z; the 5-minute-resolution series shows flat ~48% through 03-21T01:00 then steady growth (49.52 -> 72.64 by 11:50).
  - `F5` ✔ restarts counter (cumulative) went 0 -> 1 at 03-21T22:00 bucket, 1 -> 2 at 03-22T22:00 bucket, 2 -> 3 at 03-23T20:00 bucket.
  - `F8` ✔ consumer_lag spikes (avg 810-2273, max 14375-18137) occur in three buckets: 03-21T22:00 (2273/17161), 03-22T20:00-22:00 (790/17611 and 1589/14375), 03-23T18:00-20:00 (810/18137, 1542/14213). Two of these spikes are NOT after any Kafka maintenance.
**Rejected because:** Memory growth began 2026-03-21T00:00Z, 60+ hours before the 03-23T18:00Z maintenance; the same lag-spike pattern repeats on 03-21/03-22 with no maintenance; and reducing retention removes data rather than forcing a replay. Timing and scope contradict this theory.

### H3: External email-provider or infrastructure fault causing backlog/OOM
A failing external email-provider (no telemetry) or broker fault could stall the consumer and build backlog. Not supported: lag spikes track the OOM/restart cycle, not provider behaviour; kafka logs are clean; no email-provider error evidence exists.

**Causal chain**
(none given)

**Supporting evidence**
  - `F8` ✔ consumer_lag spikes (avg 810-2273, max 14375-18137) occur in three buckets: 03-21T22:00 (2273/17161), 03-22T20:00-22:00 (790/17611 and 1589/14375), 03-23T18:00-20:00 (810/18137, 1542/14213). Two of these spikes are NOT after any Kafka maintenance.
**Contradicting evidence**
  - `F7` ✔ CPU is essentially flat at ~25% (min 21.78, max 28.5) across the whole window, with no rise during memory growth.
  - `F12` ✔ kafka logs contain only one WARN (deprecated TLS cipher at 03-23T17:17:15Z) and no error/incident-level entries in 17:00-23:59 window.
**Rejected because:** No email-provider errors in logs, kafka logs clean, and lag spikes align with the OOM cycle rather than any external event.

## Facts (verified observations)
- Memory sawtooths from ~48% to 95-98% repeatedly since 03-21T00:00Z, resetting on each restart. [F1, F2]
- dep-5402 added an in-memory template cache (ConcurrentHashMap) to notification-service at 2026-03-21T00:00Z and is still live, never rolled back. [F3]
- TemplateCache grows ~9000 entries/hour with evictions=0, indicating no bound/expiry. [F4]
- Three OOMKilled (exit 137) restarts at 03-21T22:59Z, 03-22T21:59Z, 03-23T19:59Z; restarts counter 0->3. [F5, F6]
- CPU is flat at ~25% throughout, so the OOM is not driven by CPU/load. [F7]
- Consumer-lag spikes also occurred on 03-21 and 03-22 without any Kafka maintenance. [F8]
- The only config change was the kafka retention reduction (7d->3d) at 03-23T18:00Z, ~60h after memory growth began. [F9]
- 5xx error_rate spiked (avg 1.36%, max 13.96%) only during the 03-23T19:00-20:00 OOM episode. [F10]
- notification-service consumes from shared kafka and calls external email-provider (no telemetry); kafka logs are clean. [F11, F12]

## Assumptions (not verified)
- TemplateCache retains entries keyed by template+locale+userSegment with no eviction or size bound (evictions=0 in logs confirms none occurring).
- No other memory consumers were added to notification-service in the same window.
- Restart-to-memory-reset mapping is inferred from the sawtooth aligning with each OOM log line.

## Timeline
- `2026-03-21T00:00:00Z` dep-5402 deploys v3.8.0 adding in-memory TemplateCache; memory_pct begins climbing from ~48%. [F2, F3]
- `2026-03-21T22:59:50Z` First OOMKilled (exit 137) restart; restart counter 0->1. [F5, F6]
- `2026-03-22T21:59:47Z` Second OOMKilled restart; counter 1->2. [F5, F6]
- `2026-03-23T18:00:00Z` Kafka maintenance: cfg-2410 reduces retention 7d->3d. [F9]
- `2026-03-23T18:00:00Z` Consumer lag spike (avg 810, max 18138) in this bucket. [F8]
- `2026-03-23T19:59:21Z` Third OOMKilled restart; counter 2->3; 5xx error_rate peaks at 13.96%. [F5, F6, F10]
- `2026-03-23T20:20:00Z` Incident INC-004 reported; delayed trade-confirmation emails (~25 min). []
- `2026-03-23T22:00:34Z` Post-restart TemplateCache stats: size=18000 entries, evictions=0 (already regrown). [F4]

## Recommended actions
- **[immediate]** Roll back notification-service from v3.8.0 (dep-5402) to the prior version to stop the leak immediately. -- The leak was introduced by dep-5402 and is the verified root cause; rollback restores stable ~48% memory. [F1, F3, F4]
- **[short_term]** If rollback is not possible, bound the TemplateCache (max size / TTL / Caffeine with eviction) and ship as a hotfix. -- The ConcurrentHashMap has zero evictions and grows unbounded; a bounded cache caps memory. [F4]
- **[short_term]** Add alerting on memory_pct slope / heap old-gen saturation and a cache-size metric, and raise/verify the container memory limit. -- The sawtooth was visible for two days before the third restart; slope alerting would catch it earlier. [F1, F5]
- **[short_term]** De-prioritise the Kafka replay theory; verify lag spikes resolve on their own after each restart before considering consumer-side changes. -- Lag spikes predate and recur independently of the maintenance, so they are an effect, not the cause. [F8, F9]
- **[long_term]** Add memory/eviction regression tests and coverage for new in-memory caches in the notification-service pipeline. -- Prevents recurrence of unbounded caches reaching production. [F3, F4]

## Data gaps
- No pod/container memory-limit metric was inspected directly; OOM is inferred from exit code 137 and memory_pct reaching ~98%.
- email-provider has no telemetry, so it cannot be fully excluded as a secondary factor, though no evidence implicates it.
- Only notification-service and kafka logs were searched; no direct TemplateCache entry-key cardinality data beyond size/evictions counts.
- No heap-dump or GC-logs beyond the WARN summaries were available to pinpoint the exact retaining object.

## Investigation stats
- latency 39.66s, LLM calls 5, tokens 56178 (in 49411 / out 6767)
- tool calls 16 (1 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
