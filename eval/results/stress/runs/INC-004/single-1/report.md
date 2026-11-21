# Incident investigation report: INC-004
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
notification-service OOMKills are caused by an unbounded in-memory template cache introduced in deployment dep-5402 (v3.8.0, 2026-03-21T00:00Z). memory_pct ramps linearly ~5.4%/hour starting 2026-03-21T00:10Z (flat ~48% before), reaching ~97% and triggering a restart roughly every 24h (03-21T21:55, 03-22T19:40, 03-23T19:50). request_rate (~140/s), cpu_pct (~25%) and latency_p99 (~110ms) are all flat across the period, ruling out traffic/replay/load and downstream degradation. The reported Kafka maintenance (cfg-2410, retention change at 2026-03-23T18:00Z) postdates memory-onset by ~2 days, and consumer_lag spikes occur only in the same buckets as the restarts (restart catch-up), so Kafka is an effect, not the cause.

## Probable root cause
**Unbounded in-memory template cache (dep-5402) leaks memory -> OOMKill** -- bad_deployment in `notification-service`, triggered by `dep-5402` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Unbounded in-memory template cache (dep-5402) leaks memory -> OOMKill | bad_deployment | notification-service | dep-5402 | 0.86 (high) | active |
| 2 | H2: Kafka maintenance/replay causes consumer to choke on messages | dependency_failure | kafka | cfg-2410 | 0.07 (low) | rejected |
| 3 | H3: External email-provider degradation backs up retries | dependency_failure | email-provider | - | 0.04 (low) | rejected |

### H1: Unbounded in-memory template cache (dep-5402) leaks memory -> OOMKill
Deployment dep-5402 introduced a ConcurrentHashMap TemplateCache keyed by template+locale+userSegment with no eviction bound; as renders accumulate unique keys, heap grows ~5.4%/hr until the container is OOMKilled roughly every 24h, causing restarts, brief unavailability (error_rate spikes) and consumer catch-up lag spikes.

**Causal chain**
1. dep-5402 adds unbounded in-memory TemplateCache [F1]
2. cache growth -> linear memory ramp starting 2026-03-21T00:10Z [F2]
3. ramp hits ~97% -> OOMKill restart ~every 24h [F3, F4]
4. restart causes brief 5xx and consumer catch-up lag spikes [F7, F10]

**Supporting evidence**
  - `F1` ✔ Deployment dep-5402 for notification-service (v3.8.0, deployed 2026-03-21T00:00:00Z) added an in-memory template cache: 'TemplateCache (ConcurrentHashMap) keyed by template+locale+userSegment'.
  - `F2` ✔ notification-service memory_pct was flat (~47-49%) through 2026-03-20 and until 2026-03-21T00:10Z, then began a steady climb (~54% at 02:35) reaching ~92-98% peaks.
  - `F3` ✔ memory_pct shows a repeating sawtooth: ramp to ~97%, drop to ~50%, repeat every ~24h, with max values 97.37, 97.98 and 97.56 on successive days.
  - `F4` ✔ restarts counter increments by 1 at ~2026-03-21T21:55Z, ~2026-03-22T19:40Z and ~2026-03-23T19:50Z (cumulative 0->1->2->3), each coinciding with the memory sawtooth resets.
  - `F5` ✔ request_rate is flat across the whole window (mean 140.17 req/s, range 115-172), showing no traffic increase coincident with the memory ramp.
  - `F6` ✔ cpu_pct is flat (~25%) across the entire window including the memory-ramp period.
  - `F9` ✔ latency_p99_ms is flat (~110ms mean) across the whole window, showing no downstream/dependency latency degradation.
  - `F10` ✔ error_rate is ~0.2% baseline but spikes to max 13.78/14.73/13.96 in the buckets containing the restarts (2026-03-21T20:00Z, 03-22T20:00Z, 03-23T16:00Z).
  - `F13` ✔ No notification-service deployment made before the incident window (2026-03-01 to 2026-03-20), so the only code change in the window is dep-5402.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The cache has no size/TTL eviction (not documented in the deploy summary)
  - render key cardinality grows over time (per userSegment/locale) so entries are effectively never reused/reclaimed

### H2: Kafka maintenance/replay causes consumer to choke on messages
The Kafka retention change cfg-2410 and/or a consumer-group replay floods notification-service, driving memory growth and OOMKills as reported.

**Causal chain**
(none given)

**Supporting evidence**
  - `F7` ✔ consumer_lag is low (~57-65) on 2026-03-20 and 2026-03-21 up to ~17:05Z, then spikes to avg 1890 / max 17161 at 2026-03-21T21:55Z — the same bucket as the first restart — and again ~17611 at 2026-03-22T19:40Z and ~18137 at 2026-03-23T19:50Z.
  - `F8` ✔ Kafka config change cfg-2410 reduced log.retention.ms from 604800000 to 259200000 at 2026-03-23T18:00:00Z, i.e. ~2 days after memory growth began.
**Contradicting evidence**
  - `F2` ✔ notification-service memory_pct was flat (~47-49%) through 2026-03-20 and until 2026-03-21T00:10Z, then began a steady climb (~54% at 02:35) reaching ~92-98% peaks.
  - `F5` ✔ request_rate is flat across the whole window (mean 140.17 req/s, range 115-172), showing no traffic increase coincident with the memory ramp.
  - `F6` ✔ cpu_pct is flat (~25%) across the entire window including the memory-ramp period.
  - `F9` ✔ latency_p99_ms is flat (~110ms mean) across the whole window, showing no downstream/dependency latency degradation.
**Rejected because:** Memory ramp began 2026-03-21T00:10Z, ~2 days BEFORE cfg-2410 (2026-03-23T18:00Z). consumer_lag spikes occur only in the same buckets as the restarts (catch-up after restart), not before them, and cpu_pct/request_rate/latency are flat — inconsistent with a message flood.

### H3: External email-provider degradation backs up retries
The external email-provider degrades, causing retry buffering and memory growth in notification-service.

**Causal chain**
(none given)

**Supporting evidence**
  - `F11` ✔ notification-service consumes from a shared kafka queue and calls an external email-provider; email-provider has no telemetry.
**Contradicting evidence**
  - `F9` ✔ latency_p99_ms is flat (~110ms mean) across the whole window, showing no downstream/dependency latency degradation.
  - `F10` ✔ error_rate is ~0.2% baseline but spikes to max 13.78/14.73/13.96 in the buckets containing the restarts (2026-03-21T20:00Z, 03-22T20:00Z, 03-23T16:00Z).
  - `F2` ✔ notification-service memory_pct was flat (~47-49%) through 2026-03-20 and until 2026-03-21T00:10Z, then began a steady climb (~54% at 02:35) reaching ~92-98% peaks.
**Rejected because:** latency_p99_ms is flat (~110ms) and error_rate stays at baseline ~0.2% except during restart buckets; no provider-side telemetry but no signal of dependency degradation. Memory ramp starts exactly at the deploy, not at any provider event.

## Facts (verified observations)
- dep-5402 (v3.8.0, 2026-03-21T00:00Z) added an in-memory ConcurrentHashMap template cache to notification-service. [F1]
- notification-service memory_pct was flat ~47-49% before 2026-03-21T00:10Z, then ramped steadily to ~92-98%. [F2]
- memory_pct shows a ~24h sawtooth reaching ~97% before resetting to ~50%. [F3]
- restarts incremented at ~2026-03-21T21:55Z, ~2026-03-22T19:40Z and ~2026-03-23T19:50Z (cumulative 0->3). [F4]
- request_rate (mean 140/s) and cpu_pct (mean 25%) are flat across the window; no traffic/load increase. [F5, F6]
- consumer_lag spikes (max 17161, 17611, 18137) occur only in the same buckets as the restarts. [F7]
- Kafka cfg-2410 changed retention at 2026-03-23T18:00Z, ~2 days after memory growth began. [F8]
- latency_p99_ms remains flat ~110ms; no dependency latency degradation. [F9]
- error_rate spikes to ~13-14% only in restart buckets vs ~0.2% baseline. [F10]
- No notification-service deployment occurred before 2026-03-20; dep-5402 is the only code change in-window. [F13]

## Assumptions (not verified)
- The TemplateCache in dep-5402 has no eviction/TTL bound (inferred from the deploy summary; not directly shown in logs since logs were unavailable).
- The render key space (template+locale+userSegment) accumulates unique entries over time rather than being small and reused.
- The restarts counter reflects OOMKill restarts (consistent with the memory sawtooth reaching ~97%).

## Timeline
- `2026-03-21T00:00:00Z` Deployment dep-5402 (v3.8.0) adds in-memory template cache to notification-service. [F1]
- `2026-03-21T00:10:00Z` memory_pct stops being flat (~48%) and begins linear ramp. [F2]
- `2026-03-21T21:55:00Z` First restart (cumulative=1); memory ~97%, consumer_lag spike 17161, error_rate spike. [F4, F7, F10]
- `2026-03-22T19:40:00Z` Second restart (cumulative=2); consumer_lag spike 17611. [F4, F7]
- `2026-03-23T18:00:00Z` Kafka cfg-2410 reduces log retention (reported maintenance); postdates leak onset by ~2 days. [F8]
- `2026-03-23T19:50:00Z` Third restart (cumulative=3); consumer_lag spike 18137; reported incident. [F4, F7]

## Recommended actions
- **[immediate]** Roll back notification-service to the version before dep-5402 (v3.8.0) to stop the leak immediately. -- Memory ramp begins exactly at the deploy and the only in-window code change is dep-5402's cache; rollback should halt the sawtooth. [F1, F2, F13]
- **[short_term]** If rollback is not feasible, bound the TemplateCache (max size / TTL / LRU eviction) via config or hotfix. -- The cache keyed by template+locale+userSegment grows unbounded; an eviction bound caps heap growth. [F1]
- **[short_term]** Add heap-usage and cache-entry-count alerts and raise the container memory limit pending the fix. -- Early-warning on the ~5.4%/hr ramp prevents silent OOM cycles. [F2, F3]
- **[short_term]** Do NOT pursue a Kafka replay fix for this incident; treat consumer_lag spikes as restart side-effects. -- Lag spikes coincide with restarts and postdate memory onset; Kafka config change postdates onset by 2 days. [F7, F8]
- **[long_term]** Restore log pipeline access (query_logs circuit-open) and review OOM/GC logs to confirm eviction behaviour post-fix. -- Logs were unavailable during the incident, limiting direct confirmation of the leak. [F12]

## Data gaps
- query_logs for notification-service was unavailable throughout (503 then circuit-open) — no application logs, stack traces, OOM/GC messages, or cache-size instrumentation could be inspected to directly confirm the cache leak or its eviction policy.
- email-provider (external) has no telemetry, so dependency-side health could not be verified directly; ruled out only via flat latency/error metrics.
- Exact per-restart timestamps are inferred from 8700s metric buckets, so restart times are approximate.
- No visibility into the TemplateCache implementation detail (eviction, key cardinality) beyond the deploy change summary.

## Investigation stats
- latency 57.55s, LLM calls 6, tokens 62768 (in 55789 / out 6979)
- tool calls 18 (5 failed, 0 partial, 5 recovered by retry), findings 13/13 verified
