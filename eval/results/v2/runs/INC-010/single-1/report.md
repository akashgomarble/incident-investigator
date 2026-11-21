# Incident investigation report: INC-010
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Settlement posting stopped at ~07:42 because a poison message (orders-3@8841207) repeatedly fails deserialization and blocks its partition, stalling the consumer and collapsing settlement throughput to ~0 req/s while lag grows to ~60k. The fault originates in order-service deployment dep-6010 (v2.16.0, 07:40:00Z), which renamed the OrderExecuted field 'qty'->'quantity' and bumped the event schema to v2. settlement-service still expects schema v1 ('missing required field qty, schema v1 expected, header says v2') and has no schema-compatibility handling, so every v2 event fails and is retried forever as a poison message. The reported consumer poll config change (cfg-3004, max.poll.records 500->200, 07:35) is NOT the root cause: no errors occurred in the 6+ minutes after it, and heartbeats continued. Kafka and ledger-db are healthy. Fix: roll back/forward-compat settlement-service deserializer to accept v2 (or roll back order-service dep-6010), plus add poison-message/DLQ handling and bound the retry loop.

## Probable root cause
**Incompatible OrderExecuted schema v2 from order-service dep-6010 blocks settlement consumer** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.87, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Incompatible OrderExecuted schema v2 from order-service dep-6010 blocks settlement consumer | bad_deployment | order-service | dep-6010 | 0.87 (high) | active |
| 2 | H2: settlement-service consumer.max.poll.records change cfg-3004 stalls consumer | config_change | settlement-service | cfg-3004 | 0.05 (low) | rejected |
| 3 | H3: Kafka broker/partition reassignment failure (per historical incidents) | infrastructure_failure | kafka | - | 0.05 (low) | rejected |
| 4 | H4: Database/resource exhaustion on settlement-service or ledger-db | resource_exhaustion | settlement-service | - | 0.03 (low) | rejected |

### H1: Incompatible OrderExecuted schema v2 from order-service dep-6010 blocks settlement consumer
order-service deployment dep-6010 (v2.16.0, 07:40:00Z) introduced an event schema v2 that renamed 'qty' to 'quantity'. settlement-service's deserializer only supports schema v1, so the message at orders-3@8841207 fails with 'missing required field qty (schema v1 expected, header says v2)'. The consumer retries the same offset forever as a poison message, blocking the partition, so no settlements post and lag grows to ~60k.

**Causal chain**
1. dep-6010 renamed qty->quantity and bumped header to v2 [F1]
2. producer begins emitting v2 OrderExecuted events [F1, F3]
3. settlement-service deserializer expects v1, fails on v2 message orders-3@8841207 [F3, F14]
4. failed offset is retried forever as poison message, blocking the partition [F4, F5]
5. partition blocked -> zero settlements, request_rate collapses, lag grows [F6, F7]

**Supporting evidence**
  - `F1` ✔ Deployment dep-6010 for order-service (v2.16.0) went live at 2026-05-12T07:40:00Z with changes 'OrderExecuted event: rename field qty -> quantity' and 'event schema version header v2'; status live, not rolled back.
  - `F3` ✔ The first settlement-service error at 2026-05-12T07:42:14Z is 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field qty (schema v1 expected, header says v2)' -- a producer/consumer schema mismatch.
  - `F4` ✔ From 07:44:51Z the same offset orders-3@8841207 is retried as a poison message blocking the partition, with attempt counters in the tens of thousands (e.g. attempt 92490, 94650), i.e. an unbounded retry loop stuck on one message.
  - `F5` ✔ settlement-service logs in 07:00-09:59 are dominated by 213 'failed to deserialize OrderExecuted' and 196 'poison message ... partition blocked' entries; 409 of 442 entries are ERROR level.
  - `F6` ✔ settlement-service request_rate fell from ~62 req/s (07:00-07:36) to ~0.4 req/s and stayed there through 09:48 (first 60.87, last 0.38), matching the loss of settlement processing.
  - `F7` ✔ settlement-service consumer_lag rose steadily from ~47 to 61605 (max) over the window, e.g. 774 at the 07:36 bucket to 58846 avg / 61605 max at 09:48.
  - `F14` ✔ The 'schema v1 expected, header says v2' mismatch shows settlement-service's deserializer was compiled for schema v1 while the producer now emits v2 -- i.e. the consumer was not deployed with v2 support, the deployment only happened on order-service.
  - `F13` ✔ settlement-service consumes from Kafka (queue-consume) and writes to shared ledger-db, so message deserialization sits on the critical path of the consumer.
**Contradicting evidence**
  - (none)
**Assumptions**
  - settlement-service was not separately deployed with v2 support (no settlement-service deployment appears in the window)
  - the single stuck offset explains the whole-partition stall (only one partition is observed in logs)

### H2: settlement-service consumer.max.poll.records change cfg-3004 stalls consumer
The reported change cfg-3004 (consumer.max.poll.records 500->200 at 07:35) reduced poll batch size and stalled settlement processing. Rejected: no errors occurred in the 6+ minutes after it was applied, heartbeats continued, and the failure signature is a deserialization/schema error, not a poll-throttling one.

**Causal chain**
1. cfg-3004 applied at 07:35:37Z [F2]
2. processing continues normally for 6+ min with heartbeats, no errors [F12]
3. first failure at 07:42:14Z is a schema deserialize error, not poll-related [F3]

**Supporting evidence**
  - `F2` ✔ Config change cfg-3004 on settlement-service changed consumer.max.poll.records from 500 to 200 at 2026-05-12T07:35:00Z.
**Contradicting evidence**
  - `F12` ✔ Between the config apply at 07:35:37Z and the first error at 07:42:14Z, settlement-service logged ongoing normal heartbeats (07:36:26Z, 07:41:06Z) and no errors; the config change at 07:35 did not immediately break processing.
  - `F3` ✔ The first settlement-service error at 2026-05-12T07:42:14Z is 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field qty (schema v1 expected, header says v2)' -- a producer/consumer schema mismatch.
  - `F4` ✔ From 07:44:51Z the same offset orders-3@8841207 is retried as a poison message blocking the partition, with attempt counters in the tens of thousands (e.g. attempt 92490, 94650), i.e. an unbounded retry loop stuck on one message.
**Rejected because:** Timing and error signature contradict it: no failures for 6+ minutes after the config apply, and the observed error is a schema mismatch produced by the order-service deploy, not reduced batch size.

### H3: Kafka broker/partition reassignment failure (per historical incidents)
As in HIST-112/HIST-104, a Kafka partition reassignment/broker issue caused the settlement consumer to lose its assignment and stall. Rejected: kafka under_replicated_partitions was 0 for all buckets and the logs show a specific poisoned offset, not a rebalance/assignment loss.

**Causal chain**
1. historical pattern: partition reassignment stalls consumer [F15]
2. current data shows no broker health issue [F9]
3. stall is a single poison offset, not assignment loss [F4]

**Supporting evidence**
  - `F15` ✔ Historical incidents HIST-112 and HIST-104 describe settlement/notification consumer lag from Kafka partition reassignment, but are not supported here because kafka under_replicated_partitions stayed 0 and no reassignment indicator appears in the logs.
**Contradicting evidence**
  - `F9` ✔ Kafka under_replicated_partitions was 0 for every bucket across the window (min 0.0, max 0.0), showing no broker/partition-health problem.
  - `F4` ✔ From 07:44:51Z the same offset orders-3@8841207 is retried as a poison message blocking the partition, with attempt counters in the tens of thousands (e.g. attempt 92490, 94650), i.e. an unbounded retry loop stuck on one message.
**Rejected because:** No supporting telemetry: under_replicated_partitions stayed 0 and the stall is localized to one failing offset rather than a reassignment.

### H4: Database/resource exhaustion on settlement-service or ledger-db
ledger-db or settlement-service resource saturation caused zero settlements. Rejected: ledger-db connections and slow queries were flat, and settlement-service cpu rise is a consequence of the retry loop, with no restarts and flat HTTP error rate.

**Causal chain**
1. cpu_pct rose at onset [F11]
2. ledger-db stayed healthy [F10]
3. no restarts and flat error_rate argue against exhaustion [F8]

**Supporting evidence**
  - `F11` ✔ settlement-service cpu_pct rose from ~28% to ~70% coincident with error onset (07:36 bucket onward), consistent with a hot retry loop rather than a capacity shortage.
**Contradicting evidence**
  - `F10` ✔ ledger-db was healthy throughout: connections_active ~61 (min 54.8, max 68.58) and slow_queries_per_min ~1 (min 0.4, max 1.62), with no step change at onset.
  - `F8` ✔ settlement-service HTTP error_rate stayed flat (~0.16 to 0.18, mean 0.21) and restarts stayed 0 for the entire window; the failure is not surfacing as HTTP 5xx or process crash.
**Rejected because:** Datastore metrics flat; CPU rise is explained by the infinite retry of the poison message, not by resource exhaustion.

## Facts (verified observations)
- order-service deployment dep-6010 v2.16.0 went live at 07:40:00Z and renamed the OrderExecuted field qty -> quantity, adding schema version header v2. [F1]
- settlement-service config cfg-3004 (consumer.max.poll.records 500 -> 200) was applied at 07:35:00Z / 07:35:37Z. [F2]
- The first settlement-service error is a deserialization failure at 07:42:14Z: 'missing required field qty (schema v1 expected, header says v2)' on message orders-3@8841207. [F3]
- From 07:44:51Z onward the same message orders-3@8841207 is repeatedly retried as a 'poison message' with 'partition blocked' and unbounded attempt counts (up to 92490). [F4]
- settlement-service error logs are dominated by 213 deserialize failures and 196 poison-message retries in the 07:00-09:59 window, with error level 409 of 442 entries. [F5]
- settlement-service request_rate collapsed from ~62 req/s to ~0.4 req/s from the 07:36-07:48 bucket onward (first 60.87, last 0.38). [F6]
- settlement-service consumer_lag grew from ~47 messages to 61605 (max) across the window, with max 2544 at the 07:36 bucket and steady climb thereafter. [F7]
- settlement-service error_rate (HTTP 5xx) stayed flat at ~0.21% and restarts stayed 0 throughout, indicating failure is inside the consumer loop, not the HTTP layer or process crashes. [F8]
- Kafka under_replicated_partitions was 0 across the entire window, so no broker/partition-reassignment failure. [F9]
- ledger-db connections_active stayed ~61 and slow_queries_per_min ~1 throughout, so the shared datastore was healthy. [F10]
- settlement-service cpu_pct rose from ~28% to ~70% starting at the 07:36 bucket, consistent with a hot retry loop rather than a pre-existing load problem. [F11]
- No settlement-service log entries between 07:41:06Z and the first 07:42:14Z error show processing or errors; heartbeats were still ok at 07:36:26Z and 07:41:06Z. [F12]

## Assumptions (not verified)
- settlement-service was not independently deployed with schema-v2 support, since no settlement-service deployment appears in the queried window.
- The stuck offset orders-3@8841207 is the direct cause of the partition stall rather than a symptom of a broader Kafka outage.
- The observed metric buckets (720s) are fine-grained enough to attribute onset to the deploy at 07:40 rather than the config at 07:35.

## Timeline
- `2026-05-12T07:35:37Z` settlement-service applies config cfg-3004 (consumer.max.poll.records 500->200) [F2, F12]
- `2026-05-12T07:40:00Z` order-service deployment dep-6010 v2.16.0 goes live: renames OrderExecuted qty->quantity, schema header v2 [F1]
- `2026-05-12T07:41:06Z` settlement-service heartbeat ok, still healthy [F12]
- `2026-05-12T07:42:14Z` First deserialization error: orders-3@8841207 missing required field qty (schema v1 expected, header says v2) [F3]
- `2026-05-12T07:44:51Z` Same offset becomes a poison message, retried with partition blocked [F4]
- `2026-05-12T07:48:00Z` settlement-service request_rate collapses to ~0.4 req/s; consumer_lag begins sustained climb [F6, F7]
- `2026-05-12T09:48:00Z` consumer_lag reaches ~58846 avg / 61605 max, settlements still not posting [F7]

## Recommended actions
- **[immediate]** Roll back order-service dep-6010 to v2.15 (or its qty field naming) OR deploy a forward-compatible settlement-service that accepts schema v2 (quantity field). -- The producer now emits schema v2 while the consumer only understands v1; either side must be made compatible to clear the stall. [F1, F3, F14]
- **[immediate]** Drain/skip the poisoned offset orders-3@8841207 (or move to a DLQ) and reset the consumer to unblock the partition. -- A single message with unbounded retries is blocking the whole partition; quarantining it restores throughput. [F4, F5]
- **[short_term]** Bound consumer retry attempts and route undeserializable messages to a dead-letter queue instead of infinite in-place retry. -- Unbounded retry (attempt counts up to 94650) turned one bad message into a full partition outage. [F4]
- **[short_term]** Add alerting on consumer_lag and poison-message retry rate so a stuck partition is detected within minutes instead of ~40. -- Lag grew for ~40 minutes before being reported; a poison-message metric would have caught it immediately. [F5, F7]
- **[long_term]** Add schema-registry compatibility checks / consumer-driven contract tests between order-service and settlement-service before rolling out event schema changes. -- The qty->quantity rename broke a downstream consumer; schema evolution needs enforced compatibility. [F1, F3]

## Data gaps
- No settlement-service deployment appears in the window, so it is assumed (not proven) the consumer was not separately updated for schema v2.
- Only partition orders-3 is visible in logs; other partitions' state is not shown, so whether all partitions or only one is blocked is unconfirmed.
- The full contents of the v2 OrderExecuted message were not retrievable (logs only show the deserialize error), so the exact field set of v2 is inferred from the deploy summary.
- No consumer group / offset-commit or Kafka broker logs were available to independently confirm the partition-blocked interpretation.

## Investigation stats
- latency 48.77s, LLM calls 5, tokens 57861 (in 49708 / out 8153)
- tool calls 19 (2 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
