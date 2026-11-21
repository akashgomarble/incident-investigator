# Incident investigation report: INC-010
*System: multi_agent | outcome: **inconclusive** | ended: budget_exhausted:deadline*

## Summary
The investigation did not identify a definitive root cause. Evidence shows a configuration change to the consumer poll setting coincided with a spike in CPU usage, request rate collapse, and deserialization errors leading to poison messages, but gaps in data (ledger-db changes, later logs, dependent service metrics) prevent a conclusive attribution. Immediate mitigations to stop processing poison messages and revert the poll setting are recommended, followed by short‑term fixes to the message schema and long‑term safeguards against similar failures.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|

## Facts (verified observations)
- A configuration change on settlement-service adjusted the consumer poll setting 'consumer.max.poll.records' from 500 to 200 at 2026-05-12T07:35:00Z. [F1]
- No deployments were recorded for settlement-service or Kafka between 2026-05-11T08:00:00Z and 2026-05-12T10:00:00Z. [F2, F3]
- No configuration changes were recorded for Kafka in the same window. [F4]
- Consumer lag was stable (~40‑50 msgs/min) until 07:42, then spiked sharply, reaching >12k messages by 08:09. [F5, F6, F12]
- ERROR logs began at 07:42:14Z with messages "failed to deserialize OrderExecuted ... missing required field 'qty'" and later "poison message: retrying ... partition blocked". [F8, F9]
- CPU usage spiked from ~27% to ~69% at 07:42 and stayed high. [F10]
- Request rate dropped from ~62 req/s to ~0.4 req/s at 07:42 and remained near zero. [F11]
- Memory usage and DB connection pool usage remained stable during the incident. [F13, F14]

## Assumptions (not verified)
- The deserialization errors are caused by malformed messages (e.g., missing 'qty') rather than a bug in the consumer code.
- The reduction of max.poll.records increased poll frequency, potentially amplifying the impact of poison messages on CPU usage.
- No other unseen deployments or config changes (e.g., ledger-db) contributed to the observed behavior.
- (demoted from facts: no verified observation backs it) The service became CPU‑bound, throttling request processing while other resources stayed sufficient.

## Timeline
- `2026-05-12T07:35:00Z` Consumer poll setting changed from 500 to 200 (F1). [F1]
- `2026-05-12T07:42:00Z` CPU usage spikes, request rate collapses, consumer lag surges (F10, F11, F12). [F10, F11, F12]
- `2026-05-12T07:42:14Z` First deserialization error logged (F8). [F8]
- `2026-05-12T07:44:51Z` Poison‑message pattern appears, indicating repeated failures (F9). [F9]
- `2026-05-12T07:45:00Z` Consumer lag exceeds 1,700 messages (F6). [F6]
- `2026-05-12T08:09:00Z` Consumer lag >12k messages (F12). [F12]

## Recommended actions
- **[immediate]** Pause the settlement-service consumer (e.g., stop the pod or set max.poll.interval.ms to a high value) to stop processing poison messages. -- Stops the CPU‑bound loop caused by repeated deserialization failures, preventing further lag growth. [F8, F9, F10, F11]
- **[immediate]** Revert the consumer.max.poll.records setting back to 500 (or a value validated in staging). -- If the reduced batch size contributed to higher poll frequency and amplified the poison‑message impact, restoring the prior value may reduce CPU pressure. [F1]
- **[short_term]** Investigate and fix the schema mismatch causing the missing 'qty' field (e.g., update producer to include the field or adjust consumer deserializer). Deploy the fix and restart the consumer. -- Eliminates the root source of poison messages, allowing normal processing to resume. [F8]
- **[short_term]** Implement a dead‑letter queue (DLQ) for messages that fail deserialization after a configurable retry count. -- Prevents a single malformed message from blocking the entire partition and consuming CPU.
- **[short_term]** Query and audit configuration and deployment history for ledger-db to rule out database‑side changes. -- Addresses data gap G1 and G2 which could hide additional contributors.
- **[short_term]** Collect and analyze logs and metrics from 07:49:33Z onward, as well as from dependent services (Kafka brokers, downstream databases). -- Fills gaps G3 and G4 to confirm whether errors persisted and whether other services were impacted.
- **[long_term]** Add monitoring for deserialization error rates and set alerts for sudden spikes. -- Provides early detection of similar poison‑message scenarios.
- **[long_term]** Establish a change‑impact review process for consumer configuration tweaks, including load‑testing for CPU impact. -- Prevents future incidents caused by seemingly minor config changes.

## Data gaps
- Missing configuration and deployment history for ledger-db (G1, G2) – could hide database‑related causes.
- Logs after 2026-05-12T07:49:33Z were not retrieved (G3) – prevents confirming the duration of the error pattern.
- No metrics/logs from dependent services (Kafka brokers, downstream databases) (G4) – cannot rule out broader system issues.
- Thread‑pool, I/O, GC pause, and latency metrics were not collected (G5) – limits ability to fully exclude other resource throttling mechanisms.

## What would change this conclusion
- If reverting the consumer.max.poll.records setting alone restores normal CPU usage and clears the lag, the config change would be identified as the primary root cause.
- If fixing the deserialization schema (e.g., adding the missing 'qty' field) eliminates the poison‑message errors and restores processing, the schema mismatch would be the root cause.
- Discovery of a recent ledger-db configuration or deployment change that aligns with the incident timeline would shift the root cause toward the database layer.

## Integrity warnings (set by code)
- fact demoted to assumption: The service became CPU‑bound, throttling request processing while other resource

## Investigation stats
- latency 533.66s, LLM calls 20, tokens 67770 (in 57305 / out 10465)
- tool calls 15 (1 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
