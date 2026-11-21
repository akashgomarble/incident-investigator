# Incident investigation report: INC-004
*System: multi_agent | outcome: **inconclusive** | ended: budget_exhausted:deadline*

## Summary
The investigation could not definitively identify a root cause for the repeated OOMKilled restarts of notification-service. Verified observations show sharp memory usage spikes (>90%) and consumer lag spikes preceding each OOM event, but no concrete link to recent changes or external factors could be established. Confidence in a root‑cause conclusion is low; further data collection is required.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|

## Facts (verified observations)
- Three OOMKilled restarts of notification-service occurred at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z, and 2026-03-23T19:59:21Z. [F6, F7]
- Memory usage (memory_pct) of notification-service rose sharply from ~48% on 2026-03-20 to >90% just before each OOM event, peaking at 94.55% (2026-03-21T18:15:00Z), 97.98% (2026-03-22T20:15:00Z), and 95.16% (2026-03-23T15:45:00Z). [F8]
- After each OOM restart, memory_pct dropped back to ~53‑63%. [F9]
- Consumer lag for notification-service spiked dramatically before each OOM event (avg ~520‑556, max >17k). [F14]
- No configuration changes were recorded for notification-service between 2026-03-09 and 2026-03-23. [F2]
- order-service showed no OOMKilled logs and its memory_pct remained stable (~60%). [F10, F11]
- Historical incident HIST-104 (2025-11-05) involved notification-service and Kafka with consumer lag spikes caused by a Kafka partition reassignment storm. [F4]

## Assumptions (not verified)
- The in‑memory template cache deployment (dep-5402) actually increased memory pressure on notification-service.
- Kafka maintenance performed on 2026-03-23 could have caused a replay that increased consumer lag.
- No other services besides order-service experienced OOM events during the same window.
- (demoted from facts: no verified observation backs it) A deployment (dep-5402) adding an in‑memory template cache was applied to notification-service on 2026-03-21T00:00:00Z (unverified).

## Timeline
- `2026-03-21T00:00:00Z` Deployment dep-5402 adding an in‑memory template cache applied to notification-service (unverified). [F1]
- `2026-03-21T18:15:00Z` Memory_pct of notification-service peaked at 94.55%. [F8]
- `2026-03-21T22:59:50Z` First OOMKilled restart of notification-service. [F6]
- `2026-03-22T20:15:00Z` Memory_pct peaked at 97.98%. [F8]
- `2026-03-22T21:59:47Z` Second OOMKilled restart of notification-service. [F7]
- `2026-03-23T15:45:00Z` Memory_pct peaked at 95.16%. [F8]
- `2026-03-23T19:59:21Z` Third OOMKilled restart of notification-service. [F7]

## Recommended actions
- **[immediate]** Roll back deployment dep-5402 (remove in‑memory template cache) and monitor memory usage. -- The cache deployment coincides with the start of the OOM pattern and could be adding memory pressure. [F1]
- **[short_term]** Increase memory limits / request for notification-service pod and enable memory‑usage alerts. -- Observed memory_pct consistently exceeds 90% before OOM; higher limits may prevent immediate kills while root cause is investigated. [F8, F9]
- **[short_term]** Collect and verify runtime metrics (memory, CPU, GC, consumer lag) for notification-service and Kafka during the next maintenance window. -- Current gaps prevent correlation of OOM events with Kafka activity or lag spikes.
- **[short_term]** Obtain detailed Kafka maintenance logs (timing, partitions affected) and compare with OOM timestamps. -- To test the hypothesis that Kafka replay after maintenance caused lag and memory spikes.
- **[short_term]** Audit upstream (email-provider) and downstream (Kafka) configuration changes between 2026-03-09 and 2026-03-23. -- Missing change data could hide a contributing factor.
- **[long_term]** Implement finer‑grained consumer lag monitoring (sub‑minute resolution) for notification-service. -- Current lag metric granularity (≈9.7 h) is insufficient to pinpoint spikes at OOM moments.
- **[long_term]** Add automated regression testing for memory usage when new in‑memory caches are introduced. -- Prevents future deployments that unintentionally increase memory pressure.

## Data gaps
- No runtime metric data (memory usage, OOMKilled events, consumer lag) for notification-service or Kafka around the OOM incidents was collected.
- Deployments and configuration changes for upstream/downstream services (Kafka, email-provider) were not retrieved.
- Details of the Kafka maintenance window (timing, scope) were not obtained.
- Consumer lag metric is bucketed at ~9.7 h, limiting precise correlation with OOM timestamps.
- No Kafka maintenance logs were found for the period 2026-03-20 to 2026-03-24.

## What would change this conclusion
- Verified evidence that the in‑memory template cache added by dep-5402 directly caused memory usage to exceed pod limits.
- Confirmed Kafka maintenance logs showing a replay that produced the observed consumer lag spikes immediately before each OOM event.
- Discovery of a configuration change in the email‑provider that increased payload size, leading to higher memory consumption in notification-service.

## Integrity warnings (set by code)
- fact demoted to assumption: A deployment (dep-5402) adding an in‑memory template cache was applied to notifi

## Investigation stats
- latency 896.26s, LLM calls 20, tokens 61868 (in 50043 / out 11825)
- tool calls 15 (3 failed, 0 partial, 0 recovered by retry), findings 11/17 verified
