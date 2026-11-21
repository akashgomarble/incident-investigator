# Incident investigation report: INC-010
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Settlement processing stopped because order-service deploy dep-6010 (v2.16.0, 07:40Z) shipped an incompatible OrderExecuted v2 schema (renamed qty->quantity, header v2). settlement-service's deserializer still expects schema v1 field 'qty', so the record at orders-3@8841207 failed to deserialize starting 07:42:14Z, became a poison message, and blocked partition orders-3 at 07:44:51Z. Consumption collapsed (request_rate ~62 -> 0.4 req/s) and consumer_lag climbed to 61605 by 09:45Z. The 07:35 config change cfg-3004 (max.poll.records 500->200) is a red herring: it cannot explain deserialize failures, a near-total stop, or flat error_rate/db-pool. Fix: roll back/deploy a v1-compatible producer or add an upcast/dual-read in settlement-service, then clear the blocked partition (skip/retry the poison offset).

## Probable root cause
**order-service v2.16.0 schema change (qty->quantity, header v2) broke settlement-service deserialization, blocking the partition** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.88, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 schema change (qty->quantity, header v2) broke settlement-service deserialization, blocking the partition | bad_deployment | order-service | dep-6010 | 0.88 (high) | active |
| 2 | H2: Config change cfg-3004 (max.poll.records 500->200) caused lag/consumer stall | config_change | settlement-service | cfg-3004 | 0.07 (low) | rejected |
| 3 | H3: Kafka broker/partition reassignment stalled settlement consumer (infrastructure) | infrastructure_failure | kafka | - | 0.05 (low) | rejected |

### H1: order-service v2.16.0 schema change (qty->quantity, header v2) broke settlement-service deserialization, blocking the partition
order-service deploy dep-6010 (v2.16.0, 07:40) renamed OrderExecuted field qty->quantity and set schema header v2 without a compatible consumer. settlement-service still expects schema v1 (field 'qty'), so every OrderExecuted on orders-3 fails to deserialize. The failed record becomes a poison message that blocks partition orders-3, halting all settlement consumption around 07:42-07:44.

**Causal chain**
1. dep-6010 ships OrderExecuted v2 with field renamed qty->quantity and header v2 [F1, F2]
2. settlement-service deserializer expects schema v1 field 'qty', so v2 messages fail [F3, F13]
3. the un-deserializable record is retried forever as a poison message and blocks partition orders-3 [F4]
4. blocked partition halts consumption: request_rate collapses to 0.4 req/s and consumer_lag grows to 61605 [F5, F6]

**Supporting evidence**
  - `F1` ✔ order-service was deployed at 07:40:00Z (dep-6010, v2.16.0) with change summary 'Order events v2', renaming field qty -> quantity and adding an event schema version header v2.
  - `F2` ✔ order-service logged 'deployment dep-6010 (v2.16.0) rollout complete, all pods ready' at 07:40:01Z.
  - `F3` ✔ settlement-service began emitting deserialization errors at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' — 213 occurrences.
  - `F4` ✔ settlement-service logged 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' starting 07:44:51Z, 196 occurrences.
  - `F5` ✔ settlement-service request_rate collapsed from ~62 req/s (07:15-07:30) to 0.4 req/s by the 07:45 bucket and stayed ~0.4 through 09:45.
  - `F6` ✔ settlement-service consumer_lag grew monotonically from ~40 messages before 07:30 to 61605 by 09:45; the ramp begins in the 07:30 bucket (avg 203.53, max 1306.47).
  - `F9` ✔ settlement-service cpu_pct rose from ~28% to ~70% starting at the 07:30 bucket and stayed ~70%, consistent with hot retry looping rather than load.
  - `F13` ✔ The deserialization errors span from 07:42:14 onward continuously (last_match 09:58:31) with 213 of 442 log entries matching that pattern and 196 matching the poison-message pattern.
**Contradicting evidence**
  - `F7` ✔ A config change cfg-3004 on settlement-service ('consumer.max.poll.records' 500 -> 200) was applied at 07:35:00Z, described as 'Smaller poll batches to smooth DB writes'.
**Assumptions**
  - order-service is the producer of the orders-3 topic consumed by settlement-service (both connect to shared kafka; edge kinds queue-produce/queue-consume support this but topic ownership is not directly shown)
  - no consumer-side schema registry was configured to handle v2

### H2: Config change cfg-3004 (max.poll.records 500->200) caused lag/consumer stall
The 07:35 config change reduced batch size on settlement-service, which could slow consumption enough to grow lag. This is the incident report's leading guess.

**Causal chain**
1. smaller poll batch reduces throughput [F7]
2. lag grows [F6]

**Supporting evidence**
  - `F7` ✔ A config change cfg-3004 on settlement-service ('consumer.max.poll.records' 500 -> 200) was applied at 07:35:00Z, described as 'Smaller poll batches to smooth DB writes'.
  - `F6` ✔ settlement-service consumer_lag grew monotonically from ~40 messages before 07:30 to 61605 by 09:45; the ramp begins in the 07:30 bucket (avg 203.53, max 1306.47).
**Contradicting evidence**
  - `F3` ✔ settlement-service began emitting deserialization errors at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' — 213 occurrences.
  - `F4` ✔ settlement-service logged 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' starting 07:44:51Z, 196 occurrences.
  - `F5` ✔ settlement-service request_rate collapsed from ~62 req/s (07:15-07:30) to 0.4 req/s by the 07:45 bucket and stayed ~0.4 through 09:45.
  - `F8` ✔ settlement-service error_rate stayed flat at ~0.2% throughout (06:00-09:45), and db_pool_in_use stayed flat at ~6 connections.
  - `F13` ✔ The deserialization errors span from 07:42:14 onward continuously (last_match 09:58:31) with 213 of 442 log entries matching that pattern and 196 matching the poison-message pattern.
**Assumptions**
  - smaller batches alone could not explain a total stall
**Rejected because:** request_rate dropped to 0.4 req/s (near-total stop, not a slower rate), and error logs show explicit schema-mismatch deserialization failures and a blocked partition starting ~2 min after the order-service deploy. A batch-size reduction would slow consumption, not produce deserialize failures or a poison-message block. Lag ramp onset matches the deploy (07:42), not the config change.

### H3: Kafka broker/partition reassignment stalled settlement consumer (infrastructure)
Similar to historic incidents HIST-104/HIST-112, a Kafka broker rebalance could have stalled the settlement consumer group.

**Causal chain**
1. broker maintenance causes partition reassignment *(assumed)*
2. consumer loses partitions and lag grows [F6]

**Supporting evidence**
  - `F6` ✔ settlement-service consumer_lag grew monotonically from ~40 messages before 07:30 to 61605 by 09:45; the ramp begins in the 07:30 bucket (avg 203.53, max 1306.47).
**Contradicting evidence**
  - `F3` ✔ settlement-service began emitting deserialization errors at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' — 213 occurrences.
  - `F4` ✔ settlement-service logged 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' starting 07:44:51Z, 196 occurrences.
  - `F13` ✔ The deserialization errors span from 07:42:14 onward continuously (last_match 09:58:31) with 213 of 442 log entries matching that pattern and 196 matching the poison-message pattern.
**Assumptions**
  - broker health data would show reassignment
**Rejected because:** The logs show a specific per-record deserialization failure ('missing required field qty, schema v1 expected header says v2') tied to a single offset on orders-3, not a rebalance message. Broker-side logs could not be retrieved (F12), but the application-layer error precisely matches the order-service schema change.

## Facts (verified observations)
- order-service deploy dep-6010 (v2.16.0) at 07:40Z renamed OrderExecuted field qty->quantity and added schema header v2. [F1, F2]
- settlement-service began failing to deserialize OrderExecuted at 07:42:14Z with 'missing required field qty (schema v1 expected, header says v2)'. [F3, F13]
- The failed record was retried as a poison message with 'partition blocked' starting 07:44:51Z on orders-3@8841207. [F4]
- Settlement consumption collapsed from ~62 req/s to 0.4 req/s while consumer_lag grew to 61605 by 09:45Z. [F5, F6]
- settlement-service error_rate (~0.2%) and db_pool_in_use (~6) stayed flat; only cpu_pct rose with retry looping. [F8, F9]
- A config change cfg-3004 (max.poll.records 500->200) was applied at 07:35Z. [F7]
- kafka broker-side logs could not be retrieved (503), leaving broker health unverified. [F12]

## Assumptions (not verified)
- order-service is the producer of the orders-3 topic that settlement-service consumes (both link to shared kafka with queue-produce/queue-consume, but topic-level ownership is not directly shown).
- No schema registry / backward-compatibility handling exists between the v2 producer and v1 consumer.

## Timeline
- `2026-05-12T07:35:00Z` Config change cfg-3004 applied to settlement-service (consumer.max.poll.records 500->200). [F7]
- `2026-05-12T07:40:00Z` order-service deploy dep-6010 v2.16.0 (Order events v2, qty->quantity, header v2). [F1, F2]
- `2026-05-12T07:42:14Z` First deserialization error in settlement-service: missing 'qty', schema v1 expected header says v2. [F3]
- `2026-05-12T07:44:51Z` Poison message: partition orders-3 blocked, retrying orders-3@8841207. [F4]
- `2026-05-12T07:45:00Z` settlement-service request_rate collapses to ~0.4 req/s; consumer_lag begins steep climb. [F5, F6]
- `2026-05-12T08:22:00Z` Incident reported by on-call SRE (settlements stuck ~40 min). []
- `2026-05-12T09:45:00Z` consumer_lag reaches 61605 messages. [F6]

## Recommended actions
- **[immediate]** Roll back order-service to the pre-dep-6010 version (or have it emit schema v1 / keep field 'qty') to stop producing v2 OrderExecuted events. -- The v2 schema is incompatible with the live settlement-service deserializer and is the source of the poison messages. [F1, F3]
- **[immediate]** Unblock partition orders-3: skip/dead-letter the poison offset at orders-3@8841207 and reset the consumer to resume from the next valid offset. -- The retry loop blocks the entire partition, so settlement stays at zero until the poison record is bypassed. [F4, F5]
- **[short_term]** Deploy forward-compatible deserialization in settlement-service (accept v2 'quantity' as alias for 'qty' / upcast v1<->v2) so mixed-version traffic is tolerated. -- Prevents recurrence and lets the backlog of v2 events be processed after recovery. [F3, F13]
- **[long_term]** Add a schema registry / CI compatibility check on the order events topic and alert on poison-message partition blocks. -- The change passed review but was incompatible at the consumer boundary; automated contract checks and a poison-message alert would catch it earlier. [F1, F4]

## Data gaps
- kafka broker-side logs could not be fetched (503), so partition-reassignment/broker health for 06:00-10:00 could not be directly excluded (though application logs point elsewhere).
- The exact producer-to-topic binding (which service owns orders-3) is inferred from dependency graph edge kinds, not shown directly.
- get_deployments for settlement-service timed out on first attempt; only order-service and config-change data were confirmed.
- The rate of incoming order-event production (input throughput) was not directly measured, so lag growth is inferred from the consumer stall plus continuing production.

## Investigation stats
- latency 41.46s, LLM calls 5, tokens 48243 (in 42022 / out 6221)
- tool calls 16 (3 failed, 0 partial, 6 recovered by retry), findings 13/13 verified
