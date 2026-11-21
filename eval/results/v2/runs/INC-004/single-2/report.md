# Incident investigation report: INC-004
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
notification-service suffers a repeating ~24h memory-leak cycle: heap climbs linearly from ~48% to ~97% then OOMKills (exit 137) and restarts, resetting memory. This started immediately after deployment dep-5402 (v3.8.0, 2026-03-21T00:00), which added an in-memory template cache (ConcurrentHashMap keyed by template+locale+userSegment). CPU (~25%), request_rate (~140/s), and latency (~110ms) are all flat, ruling out load growth, capacity saturation, and dependency degradation. The consumer-lag and error-rate spikes are consequences of GC thrash/lost throughput at the end of each leak cycle and coincide with the three OOM restarts (03-21 22:59, 03-22, 03-23 19:59) - they also occurred on 03-21 and 03-22, before today's 18:00 Kafka retention change. The reported Kafka-replay hypothesis is contradicted by timing (leak began two days before maintenance) and by clean Kafka logs. Best-supported root cause: bad_deployment (unbounded template cache in dep-5402) causing resource_exhaustion (heap OOM).

## Probable root cause
**Unbounded in-memory template cache (dep-5402) leaks heap -> periodic OOMKill; lag spikes are a consequence, not the cause** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.88, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded in-memory template cache (dep-5402) leaks heap -> periodic OOMKill; lag spikes are a consequence, not the cause | bad_deployment | notification-service | dep-5402 | 0.88 (high) | active |
| 2 | H2: Kafka maintenance / replay causes consumer lag and heap spike | infrastructure_failure | kafka | cfg-2410 | 0.07 (low) | rejected |
| 3 | H3: Traffic growth / capacity saturation | capacity_saturation | notification-service | - | 0.03 (low) | rejected |
| 4 | H4: email-provider / external dependency degradation | dependency_failure | email-provider | - | 0.02 (low) | rejected |

### H1: Unbounded in-memory template cache (dep-5402) leaks heap -> periodic OOMKill; lag spikes are a consequence, not the cause
notification-service v3.8.0 (dep-5402, 2026-03-21T00:00) introduced a ConcurrentHashMap template cache keyed by template+locale+userSegment with no eviction bound. Entries accumulate continuously under steady ~140 req/s load, leaking old-gen heap linearly (~48% -> ~97% over ~24h). GC thrash and loss of consumer throughput near the end of each cycle cause the observed consumer-lag and error-rate spikes, ending in OOMKilled (exit 137) and restart, which resets memory and restarts the cycle. Three cycles = three OOMs this week.

**Causal chain**
1. dep-5402 adds in-memory template cache with no eviction bound [F1]
2. Cache grows unbounded under steady load -> linear old-gen heap growth from ~48% at 03-21T00:35 [F2, F3]
3. Heap nears 100%, GC thrash reduces consumer throughput -> consumer lag + error-rate spikes [F9, F12, F5]
4. Container exceeds memory limit -> OOMKilled (exit 137) restart at 03-21 22:59, 03-22, 03-23 19:59 [F5, F6, F4]
5. Restart resets heap; cycle repeats every ~24h [F2, F8]

**Supporting evidence**
  - `F1` ✔ notification-service deployed v3.8.0 (dep-5402) on 2026-03-21T00:00:00Z, change summary 'Add in-memory template cache for email rendering' with changes 'TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment' and 'render path uses cache'; status live, not rolled back.
  - `F2` ✔ Memory usage for notification-service was flat at ~48% throughout 2026-03-20 (e.g. 47.86, 47.92, 48.12, 48.24, 47.89) and then ramped linearly from ~48% to ~97% over each subsequent ~24h period, sawtoothing down only on OOM restarts.
  - `F3` ✔ At fine resolution the memory climb begins right after 2026-03-21T00:00: memory is 46.84 at 00:10 and 47.79 at 00:35, then rises steadily 48.93 (01:00), 49.7 (01:25), 50.35 (01:50), 51.83 (02:15).
  - `F4` ✔ The restarts counter is 0 through 2026-03-20 and 2026-03-21 up to 16:00, then becomes 1 at 2026-03-22T00:00, 2 at 2026-03-23T00:00, and 3 at 2026-03-23T20:00.
  - `F5` ✔ notification-service logs show 3 OOMKilled ERROR events over the window, first_seen 2026-03-21T22:59:50Z, each preceded by repeated 'heap usage N% over 90 (old gen), full GC took Nms' warnings and a slow /internal/metrics request.
  - `F6` ✔ The latest OOMKilled (exit code 137) for container notification-service-7d9f occurred at 2026-03-23T19:59:21Z, immediately after heap warnings peaked with 'full GC took 6488ms'.
  - `F7` ✔ CPU for notification-service is flat at ~25% across the entire window (min 21.78, max 28.5, mean 25.01), and request_rate is flat at ~140 req/s (min 115.35, max 172.03, mean 140.17) with no upward trend.
  - `F8` ✔ Latency p99 is flat at ~110ms across the window (mean 110.02, range 96.63-124.49) with no degradation trend.
  - `F9` ✔ Consumer lag is normally ~60 with brief spikes to 80-120; large spikes appear only in the 22:00 bucket on 03-21 (avg 2273.11, max 17161.92), 20:00-22:00 on 03-22 (avg 790.21/1589.77), and 18:00-20:00 on 03-23 (avg 810.33/1542.68), returning to ~60 at 22:00 on 03-23.
  - `F12` ✔ Error rate is normally ~0.2% and only flickers to ~13-14% max briefly in the same buckets as the lag spikes (03-21T20:00, 03-22T20:00, 03-23T16:00 and 20:00).
**Contradicting evidence**
  - (none)
**Assumptions**
  - The ConcurrentHashMap cache has no size/TTL eviction (deploy summary lists a bounded key but no eviction)
  - Restart resets heap so each sawtooth cycle is one leak period
  - No other memory-consuming change was deployed to notification-service in the window

### H2: Kafka maintenance / replay causes consumer lag and heap spike
The 2026-03-23T18:00 Kafka retention change (cfg-2410) triggered a replay/consumer backlog that spiked heap in notification-service and caused the OOM.

**Causal chain**
(none given)

**Supporting evidence**
  - `F9` ✔ Consumer lag is normally ~60 with brief spikes to 80-120; large spikes appear only in the 22:00 bucket on 03-21 (avg 2273.11, max 17161.92), 20:00-22:00 on 03-22 (avg 790.21/1589.77), and 18:00-20:00 on 03-23 (avg 810.33/1542.68), returning to ~60 at 22:00 on 03-23.
  - `F10` ✔ The only cross-service config change in the window is cfg-2410 on kafka: log.retention.ms reduced 604800000 -> 259200000 ('reduce retention to 3 days') at 2026-03-23T18:00:00Z.
  - `F14` ✔ Historical incidents HIST-104 and HIST-112 both attribute lag spikes to Kafka broker maintenance/partition reassignment, but those are past incidents cited as leads, not evidence about this incident.
**Contradicting evidence**
  - `F2` ✔ Memory usage for notification-service was flat at ~48% throughout 2026-03-20 (e.g. 47.86, 47.92, 48.12, 48.24, 47.89) and then ramped linearly from ~48% to ~97% over each subsequent ~24h period, sawtoothing down only on OOM restarts.
  - `F3` ✔ At fine resolution the memory climb begins right after 2026-03-21T00:00: memory is 46.84 at 00:10 and 47.79 at 00:35, then rises steadily 48.93 (01:00), 49.7 (01:25), 50.35 (01:50), 51.83 (02:15).
  - `F4` ✔ The restarts counter is 0 through 2026-03-20 and 2026-03-21 up to 16:00, then becomes 1 at 2026-03-22T00:00, 2 at 2026-03-23T00:00, and 3 at 2026-03-23T20:00.
  - `F7` ✔ CPU for notification-service is flat at ~25% across the entire window (min 21.78, max 28.5, mean 25.01), and request_rate is flat at ~140 req/s (min 115.35, max 172.03, mean 140.17) with no upward trend.
  - `F8` ✔ Latency p99 is flat at ~110ms across the window (mean 110.02, range 96.63-124.49) with no degradation trend.
  - `F13` ✔ kafka logs in 03-23T17:00-23:59 contain only one WARN ('deprecated TLS cipher offered by client 23ad0a46') and no error, restart, reassignment, or outage events.
**Rejected because:** Timing contradicts causation: memory was already at ~95% and climbing since 2026-03-21T00:35, two days before the 2026-03-23T18:00 maintenance; identical lag spikes and OOMs occurred on 03-21 and 03-22 before any maintenance. Kafka logs for 03-23 show only a benign TLS-cipher warning with no reassignment/outage (F13).

### H3: Traffic growth / capacity saturation
Growing request load saturated notification-service memory/capacity and caused OOM.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ CPU for notification-service is flat at ~25% across the entire window (min 21.78, max 28.5, mean 25.01), and request_rate is flat at ~140 req/s (min 115.35, max 172.03, mean 140.17) with no upward trend.
  - `F8` ✔ Latency p99 is flat at ~110ms across the window (mean 110.02, range 96.63-124.49) with no degradation trend.
**Rejected because:** request_rate is flat ~140 req/s (mean 140.17) and CPU flat ~25% across the whole window; memory leaks even at constant load, so no load growth occurred.

### H4: email-provider / external dependency degradation
The external email-provider degraded, backing up the consumer and inflating memory.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ Latency p99 is flat at ~110ms across the window (mean 110.02, range 96.63-124.49) with no degradation trend.
  - `F12` ✔ Error rate is normally ~0.2% and only flickers to ~13-14% max briefly in the same buckets as the lag spikes (03-21T20:00, 03-22T20:00, 03-23T16:00 and 20:00).
**Rejected because:** email-provider has no telemetry and no error/evidence points to it; latency p99 stayed flat at ~110ms and error rate stayed ~0.2% baseline, inconsistent with an external provider fault.

## Facts (verified observations)
- notification-service deployed v3.8.0 (dep-5402) at 2026-03-21T00:00, adding an in-memory ConcurrentHashMap template cache. [F1]
- Memory was flat ~48% on 03-20 and then ramped linearly to ~97% over ~24h periods after 2026-03-21T00:00, sawtoothing down on restarts. [F2, F3]
- Restart count went 0 -> 1 (03-22) -> 2 (03-23) -> 3 (03-23T20:00), matching three OOMKilled events. [F4, F5]
- Latest OOMKilled was 2026-03-23T19:59:21Z on container notification-service-7d9f (exit code 137). [F6]
- CPU (~25%), request_rate (~140/s) and latency p99 (~110ms) are flat with no trend, excluding load/capacity/dependency causes. [F7, F8]
- Large consumer-lag spikes occur only at ~the OOM restart buckets (03-21 22:00, 03-22 20:00-22:00, 03-23 18:00-20:00), returning to ~60 by 03-23T22:00. [F9]
- The only config change is kafka cfg-2410 (log.retention.ms 604800000 -> 259200000) at 2026-03-23T18:00, two days after the leak began. [F10]
- kafka logs on 03-23 17:00-23:59 show no errors/reassignments, only a benign deprecated-TLS-cipher warning. [F13]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-03-20T00:00:00Z` Memory flat ~48% all day; restarts=0; no incidents. [F2, F4]
- `2026-03-21T00:00:00Z` dep-5402 v3.8.0 deployed: in-memory template cache added to notification-service. [F1]
- `2026-03-21T00:35:00Z` Memory begins steady linear climb from ~48%. [F3]
- `2026-03-21T22:59:50Z` First OOMKilled (exit 137); consumer-lag spike in 22:00 bucket (avg 2273). [F5, F9, F4]
- `2026-03-22T20:00:00Z` Second OOMKilled; consumer-lag spike (avg 790-1589). [F4, F9]
- `2026-03-23T18:00:00Z` Kafka maintenance cfg-2410 (retention 7d -> 3d). [F10]
- `2026-03-23T19:59:21Z` Third OOMKilled (exit 137) on notification-service-7d9f; lag spike in 18:00-20:00 buckets; lag returns to ~60 by 22:00. [F6, F9, F4]

## Recommended actions
- **[immediate]** Roll back or disable the template cache introduced in dep-5402 (feature-flag off / revert to v3.7.x) and confirm memory stops climbing. -- The leak onset exactly coincides with dep-5402 (2026-03-21T00:00); removing the change should halt the ~24h OOM cycle. [F1, F2, F3]
- **[immediate]** Do NOT attribute this to the 2026-03-23 Kafka retention change; keep the Kafka maintenance out of the remediation path. -- The leak and two prior OOMs predate the maintenance, and Kafka logs are clean; acting on Kafka would not fix the issue. [F2, F10, F13]
- **[short_term]** Bound the ConcurrentHashMap template cache: add max-size/TTL eviction (e.g. capped size + LRU, or use a bounded Caffeine/Guava cache) instead of an unbounded keyed map. -- A cache keyed by template+locale+userSegment with no eviction grows unbounded under steady traffic. [F1]
- **[short_term]** Raise an alert on old-gen heap >90% trending and on notification-service restarts counter to catch leak onset before the 3rd OOM. -- The leak ran ~24h per cycle over three days before being escalated; earlier heap-trend alerting would have flagged it on 03-21. [F2, F4, F5]

## Data gaps
- No heap-dump or cache-size/size-metric was available to directly prove the ConcurrentHashMap template cache is the leaking object (leak attribution is inferred from deploy timing + unbounded keying, not a measured cache size).
- email-provider has has_telemetry=false, so external-provider health could not be directly confirmed (though flat latency/error-rate argue against it).
- The 03-21T17:00-19:30 window before the first OOM had only 1 WARN log returned in tc-016 due to truncation (truncated:true), so pre-21:37 log detail for 03-21 is partial.

## Investigation stats
- latency 43.57s, LLM calls 4, tokens 48051 (in 40837 / out 7214)
- tool calls 16 (0 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
