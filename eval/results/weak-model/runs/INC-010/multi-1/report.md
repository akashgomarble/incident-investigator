# Incident investigation report: INC-010
*System: multi_agent | outcome: **root_cause_identified** | ended: budget_exhausted:deadline*

## Summary
The investigation found strong evidence of malformed OrderExecuted messages causing deserialization failures and poison‑message handling, leading to consumer lag. However, the source of these malformed messages (order-service deployment) remains unverified, and resource or broker issues have not been fully ruled out. Therefore, the root cause cannot be definitively confirmed at this time.

## Probable root cause
**Producer emitted malformed messages causing deserialization failures and consumer lag** -- dependency_failure in `order-service`, triggered by `unknown` (confidence 0.62, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Producer emitted malformed messages causing deserialization failures and consumer lag | dependency_failure | order-service | unknown | 0.62 (medium) | active |
| 2 | H2: Configuration change reduced max.poll.records causing consumer to stall | config_change | settlement-service | config_change_2026-05-12T07:35:00Z | 0.38 (low) | active |
| 3 | H3: Settlement-service experienced resource exhaustion leading to consumer stall | resource_exhaustion | settlement-service | null | 0.18 (low) | active |
| 4 | H4: Kafka partition leader or connectivity issue blocked settlement-service consumer | infrastructure_failure | kafka-broker | null | 0.12 (low) | active |

### H1: Producer emitted malformed messages causing deserialization failures and consumer lag
A recent change in the upstream producer (likely order-service) started emitting OrderExecuted messages with schema version v2 missing the required field 'qty'. Settlement-service consumer attempts to deserialize these messages, encounters errors (F6), treats them as poison messages (F7), and blocks the partition, causing rapid consumer lag growth (F5, F9) and zero settlements processed.

**Causal chain**
1. order-service change introduced schema v2 messages missing 'qty' *(assumed)*
2. settlement-service consumer receives malformed messages and fails to deserialize [F6]
3. consumer treats failures as poison messages, blocks partition [F7]
4. partition block leads to rapid increase in consumer lag and processing halt [F5, F9]

**Supporting evidence**
  - `F6` ✔ The first error log entry appears at 2026-05-12T07:42:14Z: "failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)".
  - `F7` ✔ Poison‑message errors start at 2026-05-12T07:44:51Z, e.g., "poison message: retrying orders-3@8841207 (attempt 92490), partition blocked".
  - `F5` ✔ Consumer lag began a rapid increase at 2026-05-12T07:42:00Z, where the avg lag jumped to 386.63 messages (previous minute avg 26.25).
  - `F9` ✔ Consumer lag metric shows a steep rise from 35.87 messages at 07:40 to 1306.47 messages at 07:44, confirming the rapid escalation.
  - `F4` ✘ unverified A deployment of order-service (v2.16.0) occurred at 2026-05-12T07:40:00Z, unrelated to settlement-service.
**Contradicting evidence**
  - (none)
**Assumptions**
  - order-service deployed new version at 07:40 (F4) introduced schema change
  - order-service is the source of OrderExecuted messages

### H2: Configuration change reduced max.poll.records causing consumer to stall
The configuration change on settlement-service at 07:35 reduced consumer.max.poll.records from 500 to 200 (F2). This change interacted with the consumer's processing loop, causing it to poll fewer messages and become unable to keep up, leading to rapid lag increase (F5, F9) and eventual processing halt.

**Causal chain**
1. max.poll.records reduced to 200 [F2]
2. consumer poll loop processes fewer messages per poll, increasing backlog *(assumed)*
3. backlog manifests as rapid consumer lag increase [F5, F9]

**Supporting evidence**
  - `F2` ✔ A configuration change on settlement-service reduced consumer.max.poll.records from 500 to 200 at 2026-05-12T07:35:00Z.
  - `F5` ✔ Consumer lag began a rapid increase at 2026-05-12T07:42:00Z, where the avg lag jumped to 386.63 messages (previous minute avg 26.25).
  - `F9` ✔ Consumer lag metric shows a steep rise from 35.87 messages at 07:40 to 1306.47 messages at 07:44, confirming the rapid escalation.
**Contradicting evidence**
  - `F6` ✔ The first error log entry appears at 2026-05-12T07:42:14Z: "failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)".
  - `F7` ✔ Poison‑message errors start at 2026-05-12T07:44:51Z, e.g., "poison message: retrying orders-3@8841207 (attempt 92490), partition blocked".
**Assumptions**
  - lower poll size directly caused processing slowdown sufficient to generate observed lag
  - no other factor (e.g., poison messages) contributed significantly

### H3: Settlement-service experienced resource exhaustion leading to consumer stall
At around 07:42 settlement-service hit CPU or memory limits, causing its consumer threads to stall. This resulted in rapid consumer lag growth (F5, F9) and halted settlement processing.

**Causal chain**
1. settlement-service resource usage spiked (CPU/memory/thread pool) *(assumed)*
2. consumer threads unable to process messages, leading to lag [F5, F9]

**Supporting evidence**
  - `F5` ✔ Consumer lag began a rapid increase at 2026-05-12T07:42:00Z, where the avg lag jumped to 386.63 messages (previous minute avg 26.25).
  - `F9` ✔ Consumer lag metric shows a steep rise from 35.87 messages at 07:40 to 1306.47 messages at 07:44, confirming the rapid escalation.
**Contradicting evidence**
  - (none)
**Assumptions**
  - resource metrics would show spike at 07:42
  - resource exhaustion can cause consumer to stop without raising HTTP errors

### H4: Kafka partition leader or connectivity issue blocked settlement-service consumer
A broker-side issue (e.g., partition leader loss or network partition) prevented settlement-service from fetching messages, causing consumer lag to rise rapidly.

**Causal chain**
1. Kafka partition leader became unavailable or network issue arose *(assumed)*
2. settlement-service consumer could not fetch messages, lag increased [F5, F9]

**Supporting evidence**
  - `F5` ✔ Consumer lag began a rapid increase at 2026-05-12T07:42:00Z, where the avg lag jumped to 386.63 messages (previous minute avg 26.25).
  - `F9` ✔ Consumer lag metric shows a steep rise from 35.87 messages at 07:40 to 1306.47 messages at 07:44, confirming the rapid escalation.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Kafka broker issue occurred at 07:42 without being logged in findings

## Facts (verified observations)
- No deployments were recorded for settlement-service between 07:00 and 09:59 on 2026-05-12. [F1]
- A configuration change on settlement-service reduced consumer.max.poll.records from 500 to 200 at 07:35. [F2]
- Consumer lag began a rapid increase at 07:42, jumping from ~35 to >1300 messages by 07:44. [F5, F9]
- The first deserialization error log appears at 07:42:14: missing required field 'qty' (schema v1 expected, header says v2). [F6]
- Poison‑message errors start at 07:44:51, indicating partition blocking due to repeated deserialization failures. [F7]
- HTTP error rate remains low and flat (0.11‑0.31 %) throughout the incident window. [F8]
- Settlement-service depends on the shared Kafka queue and the shared ledger‑db PostgreSQL database. [F3]

## Assumptions (not verified)
- The order‑service deployment at 07:40 introduced a schema version v2 for OrderExecuted messages that omitted the required field 'qty'.
- No significant CPU, memory, or DB pool utilization spikes occurred in settlement‑service during the incident.
- Kafka brokers and network connectivity were healthy; no leader election or partition loss events occurred.
- The configuration change to max.poll.records did not directly cause the consumer to stall; the stall is driven by poison‑message handling.

## Timeline
- `2026-05-12T07:35:00Z` Configuration change on settlement-service reduced consumer.max.poll.records from 500 to 200. [F2]
- `2026-05-12T07:40:00Z` Deployment of order‑service (v2.16.0) occurred (unverified). [F4]
- `2026-05-12T07:42:00Z` Consumer lag begins rapid increase (avg 386.63 msgs). [F5]
- `2026-05-12T07:42:14Z` First deserialization error logged: missing required field 'qty'. [F6]
- `2026-05-12T07:44:51Z` Poison‑message errors start; partition blocked. [F7]
- `2026-05-12T07:44:00Z` Consumer lag peaks at ~1300 messages. [F9]
- `2026-05-12T07:41-07:45Z` HTTP error rate remains low and flat. [F8]

## Recommended actions
- **[immediate]** Pause the settlement‑service consumer for the affected partition and move the poison messages to a dead‑letter topic. -- Stops further blocking and allows the consumer to resume processing good messages. [F7]
- **[short_term]** Revert the consumer.max.poll.records setting back to 500. -- Restores original poll capacity; while not the primary cause, it reduces risk of future stalls. [F2]
- **[short_term]** Verify and correct the OrderExecuted schema in order‑service; ensure the 'qty' field is present in version v2 and redeploy. -- Eliminates the malformed messages that triggered deserialization failures.
- **[short_term]** Collect and analyze CPU, memory, and DB pool utilization metrics for settlement‑service during the incident window. -- Rule out resource exhaustion (H3) as a contributing factor.
- **[short_term]** Review Kafka broker logs for leader election or network partition events around 07:42. -- Exclude infrastructure failure (H4) as a cause.
- **[long_term]** Add automated schema compatibility checks in the CI pipeline for all producer services. -- Prevents future schema mismatches from reaching production.
- **[long_term]** Enable monitoring alerts for deserialization errors and poison‑message counts. -- Provides early detection of similar issues before lag escalates.

## Data gaps
- Verification of the order‑service deployment's schema change (no direct evidence that it omitted 'qty').
- Resource usage metrics (CPU, memory, DB pool) for settlement‑service during the incident.
- Kafka broker logs for possible leader or connectivity issues in the incident window.

## What would change this conclusion
- If logs or a schema diff confirm that order‑service indeed emitted OrderExecuted messages missing the 'qty' field, H1 would be confirmed as the root cause.
- If resource usage metrics show a spike coinciding with the lag increase, H3 would become a stronger candidate.
- If broker logs reveal a partition leader loss or network partition at the same time, H4 would need to be reconsidered.

## Integrity warnings (set by code)
- H3: dropped citations to non-existent findings ['F8 (low HTTP error rate suggests service still responsive)']
- H4: dropped citations to non-existent findings ['F3 (no config/deployment changes for Kafka observed)', 'F8 (HTTP errors low, suggesting other services still communicating)']
- reporter wrote outcome=inconclusive; overridden to root_cause_identified by rule (top hypothesis is above threshold and clearly ahead)

## Investigation stats
- latency 579.21s, LLM calls 18, tokens 60892 (in 48457 / out 12435)
- tool calls 10 (0 failed, 0 partial, 0 recovered by retry), findings 8/14 verified
