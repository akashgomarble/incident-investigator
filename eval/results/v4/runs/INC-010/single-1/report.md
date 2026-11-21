# Incident investigation report: INC-010
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Settlements stopped because order-service deployment dep-6010 (v2.16.0, rolled out 07:40:01Z) renamed the OrderExecuted field qty->quantity and set the event schema header to v2, while settlement-service's consumer still deserializes v1. The first v2 message (orders-3@8841207) failed at 07:42:14Z ('missing required field qty, schema v1 expected, header says v2'), was retried as a poison message, and blocked the partition. settlement._lag climbed from ~40 to 60,124 by 09:54 while request_rate fell to 0.4 req/s. The poll-record config change cfg-3004 (07:35, 500->200) is a coincidence/misattribution: a batch-size setting cannot cause a per-message schema deserialize failure, and the error began 2 minutes after the v2 deploy whose change notes name the exact renamed field. Kafka shows no rebalance and resources (DB pool flat, no restarts) rule out exhaustion. Fix: roll back dep-6010 or make settlement-service handle schema v2, then unblock/skip the poisoned offset.

## Probable root cause
**order-service v2.16.0 schema change breaks settlement-service consumer** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 schema change breaks settlement-service consumer | bad_deployment | order-service | dep-6010 | 0.86 (high) | active |
| 2 | H2: cfg-3004 poll-record reduction caused the stall | config_change | settlement-service | cfg-3004 | 0.06 (low) | rejected |
| 3 | H3: Kafka partition reassignment / broker issue stalled the consumer | infrastructure_failure | kafka | - | 0.05 (low) | rejected |
| 4 | H4: Resource exhaustion on settlement-service | resource_exhaustion | settlement-service | - | 0.03 (low) | rejected |

### H1: order-service v2.16.0 schema change breaks settlement-service consumer
Deployment dep-6010 (order-service v2.16.0, 07:40) renamed the OrderExecuted field qty->quantity and bumped the schema header to v2. settlement-service's consumer still parses v1, so the first v2 message (orders-3@8841207) fails to deserialize, is retried as a poison message, and blocks that partition. No settlements post, the partition stalls, and consumer lag grows unboundedly.

**Causal chain**
1. dep-6010 renames qty->quantity and sets event schema header v2 [F2]
2. settlement consumer expects v1 and fails to deserialize the v2 message [F1, F10]
3. failed message is retried as a poison message and blocks the partition [F1]
4. partition blocked -> zero settlements processed, consumer_lag grows to 60k [F4]

**Supporting evidence**
  - `F1` ✔ settlement-service logged deserialization failures on OrderExecuted starting at 07:42:14Z: 'missing required field qty (schema v1 expected, header says v2)' at orders-3@8841207, then poison-message retries blocking the partition.
  - `F2` ✔ order-service deployment dep-6010 (v2.16.0) completed rollout at 07:40:01Z and its change summary is 'OrderExecuted event: rename field qty -> quantity' plus 'event schema version header v2'.
  - `F4` ✔ settlement-service consumer_lag was ~40 messages until 07:36, then rose from 1506 (07:42 bucket) to 60124 by 09:54, and request_rate collapsed from ~62 req/s to 0.4 req/s at 07:48.
  - `F10` ✔ The first settlement deserialize error (07:42:14) occurs ~2 minutes after the order-service v2 deployment completed (07:40:01) and ~7 minutes after the poll config change (07:35); the error names a schema version mismatch, not a batch/poll issue.
  - `F5` ✔ settlement-service CPU rose from ~28% to ~70% after 07:36 and stayed there; DB pool in use stayed flat at ~6 connections; no restarts occurred.
**Contradicting evidence**
  - `F6` ✔ settlement-service error_rate (HTTP 5xx) stayed flat at ~0.2% throughout, and order-service itself logged only INFO/WARN with no errors in the window.
  - `F7` ✔ kafka logged only two benign WARN 'retrying idempotent request after connection reset' messages (07:27:08 and 09:51:04) with no broker rebalance or partition-reassignment events.
**Assumptions**
  - settlement-service has not been updated to handle OrderExecuted schema v2
  - no rollback of dep-6010 or schema-registry fix was applied before the data window ends

### H2: cfg-3004 poll-record reduction caused the stall
The reported suspect: config change cfg-3004 (consumer.max.poll.records 500->200, 07:35) on settlement-service caused the consumer to stall and lag to grow.

**Causal chain**
1. smaller poll batches change consumer throughput [F3]
2. consumer stalls and lag grows [F4]

**Supporting evidence**
  - `F3` ✔ The only config change in the window was cfg-3004 on settlement-service changing consumer.max.poll.records from 500 to 200 at 07:35:00Z ('Smaller poll batches to smooth DB writes').
  - `F4` ✔ settlement-service consumer_lag was ~40 messages until 07:36, then rose from 1506 (07:42 bucket) to 60124 by 09:54, and request_rate collapsed from ~62 req/s to 0.4 req/s at 07:48.
**Contradicting evidence**
  - `F1` ✔ settlement-service logged deserialization failures on OrderExecuted starting at 07:42:14Z: 'missing required field qty (schema v1 expected, header says v2)' at orders-3@8841207, then poison-message retries blocking the partition.
  - `F2` ✔ order-service deployment dep-6010 (v2.16.0) completed rollout at 07:40:01Z and its change summary is 'OrderExecuted event: rename field qty -> quantity' plus 'event schema version header v2'.
  - `F10` ✔ The first settlement deserialize error (07:42:14) occurs ~2 minutes after the order-service v2 deployment completed (07:40:01) and ~7 minutes after the poll config change (07:35); the error names a schema version mismatch, not a batch/poll issue.
**Assumptions**
  - poll batch size could produce a hard per-message deserialize failure
**Rejected because:** The failure mode is a per-message schema deserialization error on a single message id (orders-3@8841207 'missing required field qty'), which poll batch size cannot cause. It also began 2 minutes after the v2 deployment whose change summary explicitly renames the missing field.

### H3: Kafka partition reassignment / broker issue stalled the consumer
Infrastructure-level Kafka rebalance or partition loss (as in HIST-104/HIST-112) stalled settlement-service's consumer.

**Causal chain**
1. broker rebalance reassigns partitions *(assumed)*
2. consumer loses assignment and lag grows [F4]

**Supporting evidence**
  - `F4` ✔ settlement-service consumer_lag was ~40 messages until 07:36, then rose from 1506 (07:42 bucket) to 60124 by 09:54, and request_rate collapsed from ~62 req/s to 0.4 req/s at 07:48.
  - `F9` ✔ Historical incidents HIST-104 and HIST-112 describe settlement/notification lag from Kafka broker rebalance and partition reassignment, not schema mismatch.
**Contradicting evidence**
  - `F1` ✔ settlement-service logged deserialization failures on OrderExecuted starting at 07:42:14Z: 'missing required field qty (schema v1 expected, header says v2)' at orders-3@8841207, then poison-message retries blocking the partition.
  - `F7` ✔ kafka logged only two benign WARN 'retrying idempotent request after connection reset' messages (07:27:08 and 09:51:04) with no broker rebalance or partition-reassignment events.
  - `F10` ✔ The first settlement deserialize error (07:42:14) occurs ~2 minutes after the order-service v2 deployment completed (07:40:01) and ~7 minutes after the poll config change (07:35); the error names a schema version mismatch, not a batch/poll issue.
**Assumptions**
  - kafka rebalance occurred but was not logged
**Rejected because:** kafka logs show no rebalance/reassignment events (only two benign connection-reset retries), and the settlement errors are explicit schema v1-vs-v2 deserialization failures, not assignment loss.

### H4: Resource exhaustion on settlement-service
CPU/memory/DB connections exhaustion caused settlement-service to stop consuming.

**Causal chain**
1. resource saturation stops the consumer [F5]
2. lag grows [F4]

**Supporting evidence**
  - `F5` ✔ settlement-service CPU rose from ~28% to ~70% after 07:36 and stayed there; DB pool in use stayed flat at ~6 connections; no restarts occurred.
  - `F4` ✔ settlement-service consumer_lag was ~40 messages until 07:36, then rose from 1506 (07:42 bucket) to 60124 by 09:54, and request_rate collapsed from ~62 req/s to 0.4 req/s at 07:48.
**Contradicting evidence**
  - `F1` ✔ settlement-service logged deserialization failures on OrderExecuted starting at 07:42:14Z: 'missing required field qty (schema v1 expected, header says v2)' at orders-3@8841207, then poison-message retries blocking the partition.
**Assumptions**
  - none
**Rejected because:** DB pool flat at ~6 connections and no restarts; elevated CPU (~70%) is a consequence of busy-looping poison retries, and request_rate collapse is downstream of the blocked partition rather than a cause.

## Facts (verified observations)
- settlement-service first logged OrderExecuted deserialize failures at 07:42:14Z on orders-3@8841207 with 'missing required field qty (schema v1 expected, header says v2)', followed by poison-message retries marking the partition blocked. [F1]
- order-service dep-6010 (v2.16.0) completed rollout at 07:40:01Z with changes renaming OrderExecuted field qty->quantity and adding schema header v2. [F2]
- The only config change was cfg-3004 on settlement-service setting consumer.max.poll.records 500->200 at 07:35:00Z. [F3]
- settlement-service consumer_lag grew from ~40 to 60124 and request_rate fell from ~62 req/s to 0.4 req/s. [F4]
- settlement-service CPU rose to ~70% while DB pool stayed ~6 connections and restarts remained 0, and error_rate stayed flat at ~0.2%. [F5, F6]
- kafka logged no rebalance, only two benign connection-reset retries. [F7]
- Historical similar incidents (HIST-104, HIST-112) were partition-reassignment causes, not schema mismatch. [F9]

## Assumptions (not verified)
- settlement-service has not been independently updated to parse OrderExecuted schema v2.
- No rollback of dep-6010 or schema-registry migration was applied within the observed window (dep-6010 status is 'live', rolled_back_at is null).
- The v2 messages are continuously produced by order-service, so the blocked partition cannot self-recover.

## Timeline
- `2026-05-12T07:35:00Z` Config change cfg-3004 applied to settlement-service: consumer.max.poll.records 500->200 (reporter's suspected cause). [F3]
- `2026-05-12T07:40:01Z` order-service deployment dep-6010 (v2.16.0) rollout complete: OrderExecuted qty renamed to quantity, schema header v2. [F2]
- `2026-05-12T07:42:14Z` settlement-service first deserialize error on orders-3@8841207 (schema v1 expected, header says v2). [F1]
- `2026-05-12T07:44:51Z` Poison-message retries begin; partition blocked. [F1]
- `2026-05-12T07:48:00Z` consumer_lag ~4167 and request_rate collapsed to 0.4 req/s; CPU elevated to ~70%. [F4, F5]
- `2026-05-12T08:22:00Z` Incident reported (SEV-2): settlements stuck ~40 min. []
- `2026-05-12T09:54:00Z` consumer_lag reaches 60124 and still climbing. [F4]

## Recommended actions
- **[immediate]** Roll back order-service dep-6010 to v2.15.x (or publish a v1-compatible OrderExecuted) to stop producing schema-v2 events. -- The v2 schema header and qty->quantity rename are the direct trigger of the deserialize failures. [F1, F2]
- **[immediate]** Make settlement-service tolerate schema v2 (accept 'quantity' as alias for 'qty') and skip/dead-letter the poisoned offset orders-3@8841207 to unblock the partition. -- The partition stays blocked on the poison message, so lag cannot drain until it is consumed or skipped. [F1, F4]
- **[short_term]** Add a schema-compatibility gate (consumer-driven contract / schema registry check) to the order-service release pipeline. -- A consumer-breaking field rename shipped with no compatibility check, causing a cross-service outage. [F2, F1]
- **[short_term]** Add alerting on consumer_lag growth and poison-message/partition-blocked errors rather than relying on HTTP 5xx. -- error_rate stayed flat at ~0.2% while lag grew to 60k, so existing signals did not page. [F6, F4]
- **[short_term]** Do not revert cfg-3004 as the primary mitigation (optionally revert for cleanliness); it is not the root cause. -- Poll batch size cannot cause a per-message schema deserialize failure. [F3, F1]

## Data gaps
- No settlement-service logs were checked after 10:00Z to confirm whether lag eventually drained or a rollback/unblock occurred; later-window logs and metrics were not queried.
- No schema-registry or order-service publish-side telemetry was available to confirm how many v2 messages were produced or whether a compatibility check exists.
- Root-cause is inferred from the deployment change summary (qty->quantity + header v2) matching the consumer's error; the schema-registry diff itself was not directly observed.
- The exact upstream message offset/consumer group configuration of orders-3@8841207 was not retrieved from kafka telemetry.

## Investigation stats
- latency 37.69s, LLM calls 5, tokens 56158 (in 49628 / out 6530)
- tool calls 16 (0 failed, 0 partial, 0 recovered by retry), findings 10/10 verified
