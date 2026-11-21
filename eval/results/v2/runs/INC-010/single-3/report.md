# Incident investigation report: INC-010
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Settlements stopped because order-service deploy dep-6010 (v2.16.0, 07:40Z) changed the OrderExecuted event schema (renamed qty->quantity, header bumped to v2). settlement-service still expects schema v1 and has been failing to deserialize since 07:42Z ('missing required field qty ... schema v1 expected, header says v2'), entering a poison-message retry loop that blocked partition orders-3. Consumption stopped (request_rate 62 -> 0.4 req/s), zero settlements posted, and consumer lag climbed to ~61.6k. The consumer poll config change cfg-3004 (07:35Z) blamed in the report is a red herring: it preceded onset and cannot produce a schema deserialize failure. Root cause originates in the order-service release (bad_deployment); fix is to make settlement-service understand schema v2 (or roll back order-service), then reset the stuck offset.

## Probable root cause
**order-service v2.16.0 schema change produces records settlement-service cannot deserialize, blocking the partition** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 schema change produces records settlement-service cannot deserialize, blocking the partition | bad_deployment | order-service | dep-6010 | 0.90 (high) | active |
| 2 | H2: settlement-service consumer config change (cfg-3004) caused the stall | config_change | settlement-service | cfg-3004 | 0.12 (low) | rejected |
| 3 | H3: Kafka/infrastructure fault (partition reassignment or broker issue) stalled the consumer | infrastructure_failure | kafka | - | 0.08 (low) | rejected |

### H1: order-service v2.16.0 schema change produces records settlement-service cannot deserialize, blocking the partition
order-service deploy dep-6010 (07:40Z) renamed OrderExecuted.qty to quantity and bumped the schema header to v2. settlement-service still expects v1 and fails to deserialize the first v2 record on partition orders-3 at 07:42Z; the consumer enters a poison-message retry loop and the partition is blocked, so zero settlements post and consumer lag grows unbounded. The fault originates in the order-service release; the consumer poll config change is coincidental.

**Causal chain**
1. order-service v2.16.0 deployed at 07:40Z renaming qty->quantity and header v2 [F1]
2. settlement-service cannot deserialize v2 OrderExecuted ('missing required field qty, schema v1 expected, header says v2') from 07:42Z [F2]
3. consumer stuck retrying same offset, partition blocked from 07:44Z [F3]
4. consumption stops: request_rate 62->0.4 req/s and consumer_lag climbs to 61605 [F4]
5. CPU spikes to ~70% from busy retry loop; no restarts, HTTP error_rate normal [F6, F8]

**Supporting evidence**
  - `F1` ✔ order-service deployed v2.16.0 (dep-6010) at 07:40:00Z, which renamed the OrderExecuted field qty -> quantity and set event schema version header to v2.
  - `F2` ✔ settlement-service began emitting deserialize errors at 07:42:14Z: 'missing required field qty (schema v1 expected, header says v2)' on partition orders-3@8841207 — ~2 minutes after the order-service deploy.
  - `F3` ✔ From 07:44:51Z settlement-service logged a repeating poison-message loop on the same offset with 'partition blocked', showing the consumer group cannot progress past the bad record.
  - `F4` ✔ settlement-service request_rate collapsed from ~62 req/s to ~0.4 req/s in the 07:45 bucket and stayed flat, while consumer_lag rose from ~42 (07:15) to 61605 (09:45).
  - `F8` ✔ settlement-service CPU rose from ~28% to ~70% starting in the 07:30-07:45 buckets, consistent with the consumer busy-spinning retrying the same poison record.
  - `F9` ✔ settlement-service consumes from shared Kafka (also consumed by notification-service and produced by order-service/payment-service), so the order-service schema change propagates to settlement-service over Kafka.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The v2 records in orders-3@8841207 were produced by the newly deployed order-service v2.16.0.
  - settlement-service has not yet been updated to understand schema v2.

### H2: settlement-service consumer config change (cfg-3004) caused the stall
cfg-3004 reduced consumer.max.poll.records from 500 to 200 at 07:35Z, which the report blames for the stall. But the change preceded both the first deserialize error (07:42Z) and the order-service deploy (07:40Z) by several minutes, and the observed failure mode (schema deserialize error + partition block) is unrelated to poll batch size. This hypothesis is rejected.

**Causal chain**
1. cfg-3004 applied at 07:35Z reducing poll batch size [F5]
2. No error appears until 07:42Z, after the order-service deploy, so the config change alone does not explain onset [F2]
3. Error is a schema deserialize failure, not a batch-size effect [F2]

**Supporting evidence**
  - `F5` ✔ The config change cfg-3004 on settlement-service (consumer.max.poll.records 500 -> 200) was applied at 07:35:00Z, ~7 minutes BEFORE the first deserialize error and ~5 minutes before the order-service deploy.
**Contradicting evidence**
  - `F1` ✔ order-service deployed v2.16.0 (dep-6010) at 07:40:00Z, which renamed the OrderExecuted field qty -> quantity and set event schema version header to v2.
  - `F2` ✔ settlement-service began emitting deserialize errors at 07:42:14Z: 'missing required field qty (schema v1 expected, header says v2)' on partition orders-3@8841207 — ~2 minutes after the order-service deploy.
  - `F3` ✔ From 07:44:51Z settlement-service logged a repeating poison-message loop on the same offset with 'partition blocked', showing the consumer group cannot progress past the bad record.
**Rejected because:** Timing does not fit onset (7 min early) and the failure signature (schema deserialize conflict) is unrelated to poll batch size; error began only after the order-service v2 deploy.

### H3: Kafka/infrastructure fault (partition reassignment or broker issue) stalled the consumer
Historical incidents HIST-104/HIST-112 involved consumer stalls after Kafka broker/partition events, which look similar. However kafka logs show only isolated connection-reset retries and no reassignment/error events, notification-service (same broker) is healthy, and the settlement-service errors are explicit schema deserialize failures on a specific record — pointing at a data/schema cause, not infrastructure.

**Causal chain**
1. No broker errors or partition reassignment in kafka logs [F7]
2. Other Kafka consumer (notification-service) unaffected [F7]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F2` ✔ settlement-service began emitting deserialize errors at 07:42:14Z: 'missing required field qty (schema v1 expected, header says v2)' on partition orders-3@8841207 — ~2 minutes after the order-service deploy.
  - `F3` ✔ From 07:44:51Z settlement-service logged a repeating poison-message loop on the same offset with 'partition blocked', showing the consumer group cannot progress past the bad record.
  - `F7` ✔ Kafka, notification-service and payment-service show no error-level events in the window; only isolated 'connection reset' retries and one clock-drift warning. No shared-infrastructure fault is evident.
**Rejected because:** No supporting infra telemetry; failure signature is a schema deserialize error, not a broker/rebalance event.

## Facts (verified observations)
- order-service v2.16.0 (dep-6010) deployed at 07:40Z renamed OrderExecuted qty->quantity and set header v2. [F1]
- settlement-service began failing to deserialize OrderExecuted at 07:42:14Z with 'missing required field qty (schema v1 expected, header says v2)'. [F2]
- From 07:44:51Z settlement-service repeatedly logged 'poison message: retrying orders-3@8841207 ... partition blocked'. [F3]
- settlement-service request_rate dropped from ~62 to ~0.4 req/s at 07:45Z and consumer_lag rose to 61605 by 09:45Z. [F4]
- cfg-3004 changed consumer.max.poll.records 500->200 at 07:35Z, before both the first error and the order-service deploy. [F5]
- settlement-service had zero restarts and normal HTTP error_rate throughout. [F6]
- kafka, notification-service and payment-service showed no error-level events; only isolated connection-reset retries and a clock-drift warning. [F7]
- settlement-service CPU rose from ~28% to ~70% during the incident window. [F8]
- settlement-service and notification-service both consume shared Kafka; order-service produces to it. [F9]

## Assumptions (not verified)
- The v2 record at orders-3@8841207 was produced by order-service v2.16.0 after its 07:40Z rollout.
- settlement-service has not been updated for schema v2 (its deserializer still expects v1).
- No other producer wrote a malformed record to orders-3; the schema mismatch fully explains the deserialize failure.

## Timeline
- `2026-05-12T07:35:00Z` cfg-3004 applied on settlement-service: consumer.max.poll.records 500->200 [F5]
- `2026-05-12T07:40:00Z` order-service v2.16.0 (dep-6010) deployed: OrderExecuted qty->quantity, header v2 [F1]
- `2026-05-12T07:42:14Z` settlement-service first deserialize failure on orders-3@8841207 (schema v1 expected, header says v2) [F2]
- `2026-05-12T07:44:51Z` settlement-service poison-message retry loop begins; partition orders-3 blocked [F3]
- `2026-05-12T07:45:00Z` settlement-service request_rate collapses 62 -> 0.4 req/s; CPU rises to ~70% [F4, F8]
- `2026-05-12T08:22:00Z` Incident INC-010 reported (settlements stuck ~40 min, lag 60k) []
- `2026-05-12T09:45:00Z` consumer_lag reaches 61605 and still climbing [F4]

## Recommended actions
- **[immediate]** Make settlement-service able to consume OrderExecuted schema v2 (add quantity field / v2 deserializer) and redeploy; until then, treat the record as skippable rather than blocking the partition. -- The consumer cannot progress past the v2 record; only schema compatibility unblocks the partition. [F2, F3]
- **[immediate]** If a prompt schema-compatible consumer cannot be shipped, roll back order-service dep-6010 to v2.16.0's predecessor while coordinating the forward fix, and confirm new records revert to a parseable schema. -- Stops new incompatible records from being produced so the consumer can catch up. [F1, F2]
- **[immediate]** After the fix, reset/seek the stuck offset on orders-3 past 8841207 or DLQ the poison record so the blocked partition resumes. -- The partition is stuck at a single offset; it will not advance on its own. [F3]
- **[short_term]** Revert or re-evaluate cfg-3004 (poll records 200) only if it is shown to matter; it is not the cause and should not be the primary remediation. -- Avoid misdirected remediation; the timing and failure signature rule it out as the root cause. [F5]
- **[long_term]** Add a schema-compatibility check / canary in the order-service CI-CD pipeline and consumer-side poison-message dead-lettering instead of infinite retry, to prevent a schema change from blocking a partition. -- Prevents recurrence of producer/consumer schema mismatch causing a full consumer stall. [F1, F3]

## Data gaps
- No direct log from order-service confirming it emitted the specific record orders-3@8841207; the link is inferred from the 07:40Z schema-changing deploy and the v2 header in the error.
- Settlement lag/partition metrics for other partitions are not shown, so it is unverified whether only orders-3 is blocked or the whole consumer group is affected.
- No evidence of whether settlement-service was ever intended to be updated to v2 in the same release train (deploy metadata only lists order-service).
- No confirmation of whether a rollback of dep-6010 was attempted; dep-6010 shows status 'live', rolled_back_at null.

## Investigation stats
- latency 40.48s, LLM calls 5, tokens 57251 (in 50742 / out 6509)
- tool calls 16 (0 failed, 0 partial, 0 recovered by retry), findings 9/9 verified
