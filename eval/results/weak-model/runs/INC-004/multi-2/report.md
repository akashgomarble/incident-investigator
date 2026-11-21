# Incident investigation report: INC-004
*System: multi_agent | outcome: **inconclusive** | ended: budget_exhausted:deadline*

## Summary
The investigation could not definitively identify a root cause for the repeated OOMKilled restarts of notification-service. Verified observations confirm three OOM events and the service's dependencies, but key memory and node metrics are missing, and the hypothesized memory spikes and consumer lag remain unverified. Confidence in any single cause is low; further data collection is required.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|

## Facts (verified observations)
- notification-service calls two downstream components: a shared Kafka queue (named 'kafka') and an external email provider service. [F1]
- The Kafka component is shared and used by multiple services. [F2]
- No upstream services were found that call notification-service (upstream graph empty). [F3]
- notification-service experienced OOMKilled restarts at 2026-03-21T22:59:50Z, 2026-03-22T21:59:47Z, and 2026-03-23T19:59:21Z. [F4]
- No log entries referencing the host node were found for notification-service in the queried window. [F8]

## Assumptions (not verified)
- Memory usage of notification-service rose to near‑100% in the two‑hour buckets preceding each OOM event (based on unverified observation F5).
- Consumer lag spiked dramatically in the same periods (based on unverified observation F6).
- After each OOM restart, memory usage dropped back to ~50% (based on unverified observation F7).
- Kafka maintenance performed on 2026-03-23 may have caused a replay that increased load on the consumer.

## Timeline
- `2026-03-21T22:59:50Z` notification-service OOMKilled restart (first observed event). [F4]
- `2026-03-22T21:59:47Z` notification-service OOMKilled restart (second observed event). [F4]
- `2026-03-23T19:59:21Z` notification-service OOMKilled restart (third observed event). [F4]

## Recommended actions
- **[immediate]** Increase the memory request and limit for the notification-service pod (e.g., raise limit from 512Mi to 1Gi). -- Verified OOM events indicate memory pressure; raising limits can prevent immediate restarts while further data is gathered. [F4]
- **[immediate]** Deploy a temporary replica of notification-service with reduced consumer batch size to lower memory consumption. -- If consumer lag is causing memory growth, smaller batches reduce per‑poll memory usage.
- **[short_term]** Collect node‑level resource metrics (memory, CPU, OOM events) for the host running notification-service. -- Data gap GAPS prevents confirming whether node pressure contributed to OOM; node metrics will clarify.
- **[short_term]** Enable and verify detailed memory usage and consumer lag metrics for notification-service (e.g., Prometheus exporters, JVM/Go heap profiling). -- Unverified observations F5 and F6 need confirmation to assess their role in OOM events.
- **[short_term]** Review Kafka maintenance logs and replay configuration; if replay caused a surge, add rate‑limiting or back‑pressure to the consumer. -- The incident description suggests Kafka replay may be a trigger; confirming and throttling can mitigate spikes.
- **[long_term]** Implement alerts for notification-service memory usage >80% and consumer lag >10,000 messages. -- Proactive monitoring will surface similar issues before OOM occurs.
- **[long_term]** Architecturally isolate the shared Kafka queue usage (e.g., dedicated topic or consumer group for notification-service) to reduce cross‑service impact. -- Shared Kafka component (F2) means load spikes from other services could affect this consumer; isolation improves resilience. [F2]

## Data gaps
- Node‑level resource metrics (memory, CPU, OOM events) for the host running notification-service were not retrieved.
- Memory usage and consumer lag observations (F5, F6, F7) are unverified and lack supporting metric data.
- No logs referencing the host node were found, limiting insight into node‑side failures.

## What would change this conclusion
- Verified memory usage spikes correlating precisely with each OOM event.
- Node‑level memory pressure or OOM events occurring concurrently with the service restarts.
- Evidence that Kafka replay or a specific upstream message pattern caused a sudden increase in consumer load.

## Investigation stats
- latency 543.19s, LLM calls 11, tokens 31941 (in 25135 / out 6806)
- tool calls 7 (1 failed, 0 partial, 0 recovered by retry), findings 5/8 verified
