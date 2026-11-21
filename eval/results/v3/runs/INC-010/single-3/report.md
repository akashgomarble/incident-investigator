# Incident investigation report: INC-010
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Settlement postings stopped because order-service deploy dep-6010 (v2.16.0, 07:40:00Z) renamed the OrderExecuted field qty->quantity and bumped the event schema to v2, while settlement-service still expects schema v1. Every OrderExecuted event now fails deserialization ('schema v1 expected, header says v2'), the consumer recycles the same offset as a poison message and blocks the partition, so throughput collapses to ~0.4 req/s, settlements post zero, and consumer lag grows from ~47 to 61.6k. The reported consumer poll config change (cfg-3004, 07:35:37) is a red herring: it only shrank batch size and predates the error onset. Kafka is healthy and settlement-service had no deployment and zero restarts. The fault originates in the order-service release; confidence 0.9. Fix: roll back dep-6010 or ship a v2-compatible deserializer in settlement-service, then reset the poisoned consumer offset (or DLQ the stuck message) to unblock the partition.

## Probable root cause
**order-service v2 OrderExecuted schema change breaks settlement-service consumer** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2 OrderExecuted schema change breaks settlement-service consumer | bad_deployment | order-service | dep-6010 | 0.90 (high) | active |
| 2 | H2: settlement-service config change (consumer.max.poll.records) caused the stall | config_change | settlement-service | cfg-3004 | 0.05 (low) | rejected |
| 3 | H3: Kafka partition reassignment / broker rebalance stalled the consumer (cf. HIST-112) | infrastructure_failure | kafka | - | 0.03 (low) | rejected |

### H1: order-service v2 OrderExecuted schema change breaks settlement-service consumer
order-service deploy dep-6010 (v2.16.0) renamed OrderExecuted field 'qty'->'quantity' and bumped the schema header to v2. settlement-service still deserializes as schema v1, so every OrderExecuted event fails to deserialize; the consumer retries the same offset as a poison message, the partition is blocked, zero settlements post, and consumer lag grows unboundedly. The fault ORIGINATES in the order-service release (contract-breaking producer change), not in settlement-service.

**Causal chain**
1. dep-6010 deploys and renames qty->quantity, bumping event schema to v2 [F1]
2. settlement-service (schema v1) fails to deserialize OrderExecuted [F2]
3. consumer enters poison-message retry loop and blocks the partition offset [F3]
4. consumption stops (request_rate ~0) so settlements process zero [F5, F12]
5. consumer_lag grows monotonically [F4]
6. retry loop drives CPU from ~28% to ~70% [F6]

**Supporting evidence**
  - `F1` ✔ order-service was deployed to v2.16.0 (dep-6010) at 07:40:00Z, renaming the OrderExecuted field 'qty' -> 'quantity' and bumping the event schema header to v2.
  - `F2` ✔ settlement-service logs show repeated deserialization failures on OrderExecuted events, first at 07:42:14Z, stating the consumer expects schema v1 but the header says v2.
  - `F3` ✔ settlement-service logs show poison-message retry loops blocking the partition on the same offset, with attempt counters in the tens of thousands (e.g. attempt 92490).
  - `F4` ✔ settlement-service consumer_lag rose from ~47 messages at 07:00-07:24 to 61,605 by 09:48, growing ~5000 per 12-min bucket.
  - `F5` ✔ settlement-service request_rate collapsed from ~62 req/s to 0.4 req/s in the 07:36-07:48 window and stayed at 0.4 through 09:48.
  - `F6` ✔ settlement-service CPU rose from ~28% to ~70% at the 07:36 bucket, consistent with a tight retry loop.
  - `F8` ✔ There were zero settlement-service deployments in the window.
  - `F14` ✔ First deserialize error (07:42:14Z) follows the order-service deploy (07:40:00Z rollout complete 07:40:01Z) and the config change (07:35:37Z); the error onset aligns with the deploy, but the config change only reduced poll batch size and cannot cause a schema mismatch.
**Contradicting evidence**
  - (none)
**Assumptions**
  - settlement-service has no v2 deserializer; the consumer is the only OrderExecuted consumer affected.
  - All settlement-service partitions fail equally (logs sample partition orders-3).

### H2: settlement-service config change (consumer.max.poll.records) caused the stall
cfg-3004 reduced consumer.max.poll.records from 500 to 200 at 07:35, starving the consumer and causing lag to grow.

**Causal chain**
1. cfg-3004 applied at 07:35:37Z [F7]
2. smaller poll batches reduce throughput *(assumed)*

**Supporting evidence**
  - `F7` ✔ The config change cfg-3004 (consumer.max.poll.records 500->200) was applied at 07:35:37Z, and the settlement-service log line records it as applied.
**Contradicting evidence**
  - `F2` ✔ settlement-service logs show repeated deserialization failures on OrderExecuted events, first at 07:42:14Z, stating the consumer expects schema v1 but the header says v2.
  - `F5` ✔ settlement-service request_rate collapsed from ~62 req/s to 0.4 req/s in the 07:36-07:48 window and stayed at 0.4 through 09:48.
  - `F8` ✔ There were zero settlement-service deployments in the window.
  - `F14` ✔ First deserialize error (07:42:14Z) follows the order-service deploy (07:40:00Z rollout complete 07:40:01Z) and the config change (07:35:37Z); the error onset aligns with the deploy, but the config change only reduced poll batch size and cannot cause a schema mismatch.
**Assumptions**
  - Poll batch size alone can reduce throughput to zero.
**Rejected because:** The change only shrinks poll batch size (500->200) and was applied at 07:35:37, before the 07:42:14 first error; it cannot produce a schema-v1-vs-v2 deserialization failure. Throughput fell to ~0.4 req/s due to deserialize failures, not batch sizing.

### H3: Kafka partition reassignment / broker rebalance stalled the consumer (cf. HIST-112)
A Kafka reassignment stranded settlement-service's consumer group, matching historical incident HIST-112.

**Causal chain**
1. Kafka rebalance strands consumer *(assumed)*
2. lag grows while consumer idle [F4]

**Supporting evidence**
  - `F13` ✔ Historical incident HIST-112 (settlement consumer stalled after partition reassignment, categorized infrastructure_failure) is a superficially similar lead, but its signature (reassignment/rebalance) is not present here.
**Contradicting evidence**
  - `F2` ✔ settlement-service logs show repeated deserialization failures on OrderExecuted events, first at 07:42:14Z, stating the consumer expects schema v1 but the header says v2.
  - `F3` ✔ settlement-service logs show poison-message retry loops blocking the partition on the same offset, with attempt counters in the tens of thousands (e.g. attempt 92490).
  - `F9` ✔ settlement-service restarts metric is 0 for every bucket, ruling out a restart/rebalance loop.
  - `F11` ✔ Kafka logs show only two benign idempotent-retry connection resets (07:27 and 09:51), no broker failure or rebalance storm.
**Assumptions**
  - A rebalance occurred around 07:40.
**Rejected because:** Kafka logs are clean (only two benign connection resets), restarts=0, and the settled failure mode is an explicit schema-version deserialize error with a poison-message loop, not a lost assignment.

## Facts (verified observations)
- order-service deploy dep-6010 (v2.16.0) at 07:40:00Z renamed OrderExecuted qty->quantity and bumped schema to v2. [F1]
- settlement-service fails to deserialize OrderExecuted: 'missing required field qty (schema v1 expected, header says v2)', first at 07:42:14Z. [F2]
- settlement-service re-enters a poison-message retry loop ('partition blocked') on the same offset. [F3]
- settlement-service consumer_lag grew from ~47 to 61,605 messages. [F4]
- settlement-service request_rate collapsed from ~62 to 0.4 req/s, i.e. settlements stopped processing. [F5]
- settlement-service CPU spiked from ~28% to ~70% consistent with the retry loop. [F6]
- The config change cfg-3004 (max.poll.records 500->200) was applied at 07:35:37Z, before the first error. [F7]
- There were zero settlement-service deployments and zero restarts in the window. [F8, F9]
- Kafka logs and settlement-service db_pool were normal, ruling out broker failure and DB exhaustion. [F10, F11]
- settlement-service HTTP error_rate stayed flat ~0.2%, so the failure is in the consumer path. [F12]

## Assumptions (not verified)
- settlement-service has no v2 deserializer and is the sole consumer of OrderExecuted.
- The lag growth (~5000 msgs per 12 min) reflects unprocessed backlog, consistent with a single consumer group on the orders topic.
- dep-6010 is still live (status 'live', not rolled back).

## Timeline
- `2026-05-12T07:35:37Z` settlement-service config cfg-3004 applied (max.poll.records 500->200) [F7]
- `2026-05-12T07:40:00Z` order-service deploy dep-6010 (v2.16.0) with OrderExecuted qty->quantity and schema v2 [F1]
- `2026-05-12T07:40:01Z` order-service rollout complete, all pods ready [F1]
- `2026-05-12T07:42:14Z` First settlement-service deserialize error: schema v1 expected, header says v2 [F2]
- `2026-05-12T07:44:51Z` Poison-message retry loop begins, partition blocked [F3]
- `2026-05-12T07:36:00Z` consumer_lag begins climbing; request_rate collapses toward 0.4 req/s [F4, F5]
- `2026-05-12T09:48:00Z` consumer_lag reaches 61,605 and still climbing [F4]

## Recommended actions
- **[immediate]** Roll back order-service dep-6010 to the previous version (or restore the qty field / v1 header) to stop producing v2 events the consumer cannot parse. -- The producer schema change is the origin of the fault; reverting halts new malformed events. [F1, F2]
- **[immediate]** Ship a v2-compatible OrderExecuted deserializer (accept 'quantity'/v2) in settlement-service, then reset the poisoned consumer offset or move message orders-3@8841207 to a DLQ to unblock the partition. -- Unblocks the poison message and drains the 61k backlog once the schema is understood. [F2, F3, F4]
- **[short_term]** Add producer/consumer schema-compatibility checks (contract tests / schema registry) to the order-service release pipeline before future OrderExecuted changes. -- Prevents a producer field rename from silently breaking downstream consumers. [F1, F2]
- **[short_term]** Add alerting on consumer_lag and poison-message retry loops with offset-level detail, and add a DLQ policy for undeserializable events. -- Lag took ~40 min to be noticed and the poison offset blocked all partitions. [F3, F4]

## Data gaps
- No explicit metric for order-service event schema version or per-partition lag; partition-blocking inferred from log text.
- Settlement-service logs sampled only partition orders-3; whether all partitions are blocked is inferred, not directly observed.
- No consumer-group / offset-commit metrics to confirm the exact reset point needed for remediation.

## Investigation stats
- latency 39.53s, LLM calls 5, tokens 48369 (in 42058 / out 6311)
- tool calls 15 (0 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
