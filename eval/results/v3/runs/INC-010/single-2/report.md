# Incident investigation report: INC-010
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Settlement processing stopped because order-service's deploy dep-6010 (v2.16.0, 07:40Z) shipped a breaking change to the OrderExecuted event schema (renamed qty->quantity, header bumped to v2) while settlement-service's deserializer still requires the v1 'qty' field. From 07:42:14Z settlement-service logged repeated 'missing required field qty (schema v1 expected, header says v2)' errors; the first semantically-new record (orders-3@8841207) became a poison message retried infinitely, blocking partition 3. With the partition blocked, request_rate fell from ~62/s to ~0.4/s (zero settlements) and consumer lag climbed monotonically to ~61.6k. The settlement-side config change cfg-3004 (07:35, poll batch 500->200) is a coincidental red herring: it applied cleanly, is not referenced by any error, and cannot cause a schema mismatch. Kafka and ledger-db infrastructure are healthy (zero Kafka errors; flat db_pool ~6 and memory ~50%); elevated CPU (~70%) is retry churn, not a cause. Root cause originates in order-service (bad_deployment).

## Probable root cause
**Incompatible OrderExecuted schema deploy by order-service breaks settlement-service deserialization** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Incompatible OrderExecuted schema deploy by order-service breaks settlement-service deserialization | bad_deployment | order-service | dep-6010 | 0.90 (high) | active |
| 2 | H2: Config change cfg-3004 (poll settings) caused the stall | config_change | settlement-service | cfg-3004 | 0.08 (low) | rejected |
| 3 | H3: Kafka broker / partition reassignment infrastructure fault stalled the consumer | infrastructure_failure | kafka | - | 0.03 (low) | rejected |
| 4 | H4: Resource exhaustion on settlement-service (CPU/pool/memory) halted processing | resource_exhaustion | settlement-service | - | 0.02 (low) | rejected |

### H1: Incompatible OrderExecuted schema deploy by order-service breaks settlement-service deserialization
order-service's dep-6010 (v2.16.0, 07:40Z) renamed the OrderExecuted field qty->quantity and emitted a v2 schema header, but settlement-service still expects schema v1 with 'qty'. Every new order event then fails deserialization; the first such record (orders-3@8841207) becomes a poison message that blocks partition 3, halting all settlement consumption and growing consumer lag.

**Causal chain**
1. dep-6010 renamed qty->quantity and set schema header v2 on OrderExecuted [F1]
2. settlement-service deserializer expects v1/qty and rejects v2 events [F2]
3. the first unconsumable record is retried forever as a poison message, blocking partition 3 [F3]
4. partition blocked -> request_rate collapses to ~0.4/s, zero settlements [F4]
5. consumer_lag accumulates unbounded (to 61k) [F5]

**Supporting evidence**
  - `F1` ✔ order-service deployed v2.16.0 (dep-6010) at 07:40:00Z, changing the OrderExecuted event schema: renaming field 'qty' -> 'quantity', adding field 'venue', and adding an event schema version header v2.
  - `F2` ✔ settlement-service's first ERROR appears at 07:42:14Z (about 2 minutes after the order-service deploy): 'failed to deserialize OrderExecuted ... missing required field 'qty' (schema v1 expected, header says v2)'.
  - `F3` ✔ A single Kafka record orders-3@8841207 of the new v2 schema is a poison message: it repeatedly fails deserialization and blocks partition 3, with retry attempt counts in the tens of thousands, persisting from 07:44:51Z through at least 09:58:31Z.
  - `F4` ✔ settlement-service request_rate collapsed from ~62-63 req/s (07:15) to ~0.4 req/s from 07:45 onward — effectively zero settlements processed.
  - `F5` ✔ settlement-service consumer_lag was stable ~40-64 messages until 07:15, first rose at 07:30 (avg 203), then climbed monotonically to last=61605.53 messages by 09:45-09:59.
  - `F6` ✔ Config change cfg-3004 was applied to settlement-service at 07:35:37Z, changing consumer.max.poll.records from 500 to 200 ('Smaller poll batches to smooth DB writes'). No error or warning log references this config, and it succeeded cleanly.
  - `F7` ✔ settlement-service ledger-db pool usage stayed flat (~6 connections) and memory_pct stayed flat (~50%) throughout the incident; neither shows exhaustion.
  - `F8` ✔ settlement-service CPU doubled from ~28% (06:00-07:24) to ~70% starting at 07:36 and stayed elevated — consistent with continuous poison-message retry churn rather than a cause.
  - `F9` ✔ Kafka produced zero ERROR/WARN logs across 04:00-09:59Z, and order-service's own logs show rollout complete with no errors, so neither broker nor producer application is failing.
  - `F10` ✔ settlement-service's dependency graph shows it consumes from kafka and writes to ledger-db; the message contract with order-service (producer) is the schema-coupled edge.
**Contradicting evidence**
  - (none)
**Assumptions**
  - settlement-service was rolled out to require v1/qty before 07:40 and no coordinated consumer deploy landed
  - the poison record orders-3@8841207 is one of the new v2-schema events

### H2: Config change cfg-3004 (poll settings) caused the stall
The consumer.max.poll.records change at 07:35 (500->200) somehow stalled the consumer, and lag growth is a consequence of that misconfiguration rather than a schema mismatch.

**Causal chain**
1. cfg-3004 applied at 07:35 [F6]
2. consumer stops processing after the config change *(assumed)*

**Supporting evidence**
  - `F6` ✔ Config change cfg-3004 was applied to settlement-service at 07:35:37Z, changing consumer.max.poll.records from 500 to 200 ('Smaller poll batches to smooth DB writes'). No error or warning log references this config, and it succeeded cleanly.
**Contradicting evidence**
  - `F2` ✔ settlement-service's first ERROR appears at 07:42:14Z (about 2 minutes after the order-service deploy): 'failed to deserialize OrderExecuted ... missing required field 'qty' (schema v1 expected, header says v2)'.
  - `F3` ✔ A single Kafka record orders-3@8841207 of the new v2 schema is a poison message: it repeatedly fails deserialization and blocks partition 3, with retry attempt counts in the tens of thousands, persisting from 07:44:51Z through at least 09:58:31Z.
  - `F9` ✔ Kafka produced zero ERROR/WARN logs across 04:00-09:59Z, and order-service's own logs show rollout complete with no errors, so neither broker nor producer application is failing.
**Assumptions**
  - none verified
**Rejected because:** The logs attribute the stall to deserialization failures of a v2-schema message ('missing required field qty', 'header says v2'), not to poll settings; the config change logged success with no error, and errors began only after the order-service deploy at 07:40. A smaller poll batch cannot cause a schema mismatch error. Timing correlation is coincidental.

### H3: Kafka broker / partition reassignment infrastructure fault stalled the consumer
A Kafka-side failure (broker or partition reassignment) blocked the consumer, causing the lag; past incidents HIST-104/HIST-112 had similar symptoms.

**Causal chain**
1. Kafka broker/rebalance impairs consumption *(assumed)*
2. consumer lag grows [F5]

**Supporting evidence**
  - `F5` ✔ settlement-service consumer_lag was stable ~40-64 messages until 07:15, first rose at 07:30 (avg 203), then climbed monotonically to last=61605.53 messages by 09:45-09:59.
**Contradicting evidence**
  - `F3` ✔ A single Kafka record orders-3@8841207 of the new v2 schema is a poison message: it repeatedly fails deserialization and blocks partition 3, with retry attempt counts in the tens of thousands, persisting from 07:44:51Z through at least 09:58:31Z.
  - `F9` ✔ Kafka produced zero ERROR/WARN logs across 04:00-09:59Z, and order-service's own logs show rollout complete with no errors, so neither broker nor producer application is failing.
**Assumptions**
  - none verified
**Rejected because:** Kafka emitted zero ERROR/WARN logs across the window (tc-014) and the client logs show a deterministic application-level deserialization error tied to a specific record and schema version, not a transport/rebalance symptom. Historical matches are symptom similarity only.

### H4: Resource exhaustion on settlement-service (CPU/pool/memory) halted processing
settlement-service ran out of CPU, DB connections, or memory, stalling the consumer.

**Causal chain**
1. resource saturation stalls the consumer *(assumed)*
2. lag grows [F5]

**Supporting evidence**
  - `F5` ✔ settlement-service consumer_lag was stable ~40-64 messages until 07:15, first rose at 07:30 (avg 203), then climbed monotonically to last=61605.53 messages by 09:45-09:59.
  - `F8` ✔ settlement-service CPU doubled from ~28% (06:00-07:24) to ~70% starting at 07:36 and stayed elevated — consistent with continuous poison-message retry churn rather than a cause.
**Contradicting evidence**
  - `F7` ✔ settlement-service ledger-db pool usage stayed flat (~6 connections) and memory_pct stayed flat (~50%) throughout the incident; neither shows exhaustion.
  - `F3` ✔ A single Kafka record orders-3@8841207 of the new v2 schema is a poison message: it repeatedly fails deserialization and blocks partition 3, with retry attempt counts in the tens of thousands, persisting from 07:44:51Z through at least 09:58:31Z.
**Assumptions**
  - none verified
**Rejected because:** db_pool (~6) and memory (~50%) are flat and healthy; CPU rose only after the deserialize errors began and is a consequence of retry churn (F8), not a trigger. The stall is explained by a blocked partition, not saturation.

## Facts (verified observations)
- order-service dep-6010 v2.16.0 deployed 07:40Z renamed OrderExecuted field qty->quantity and added schema version header v2. [F1]
- settlement-service first deserialization ERROR at 07:42:14Z: missing required field 'qty' (schema v1 expected, header says v2). [F2]
- A single record orders-3@8841207 is retried endlessly as a poison message blocking partition 3, from 07:44Z to at least 09:58Z. [F3]
- settlement-service throughput collapsed from ~62/s to ~0.4 req/s at 07:45Z, i.e., no settlements processed. [F4]
- consumer_lag climbed from ~50 to 61605 messages over the incident. [F5]
- Config change cfg-3004 (poll records 500->200) applied cleanly at 07:35:37Z with no associated errors. [F6]
- ledger-db pool (~6 conns) and memory (~50%) stayed flat; Kafka logged no errors; order-service rollout reported complete. [F7, F9]
- settlement-service CPU rose from ~28% to ~70% starting 07:36, after which processing had already begun to fail — consistent with retry churn. [F8]

## Assumptions (not verified)
- settlement-service's running code expects schema v1 with field 'qty' and had not been updated for the v2 contract
- the poison record orders-3@8841207 is a new v2-schema OrderExecuted event produced post-deploy

## Timeline
- `2026-05-12T07:35:37Z` Config change cfg-3004 applied to settlement-service (consumer.max.poll.records 500->200), logged success. [F6]
- `2026-05-12T07:40:00Z` order-service deploys dep-6010 v2.16.0: OrderExecuted qty->quantity, +venue, schema header v2. [F1]
- `2026-05-12T07:42:14Z` First settlement-service deserialization ERROR: missing required field 'qty' (schema v1 expected, header says v2). [F2]
- `2026-05-12T07:44:51Z` Poison message: record orders-3@8841207 retried, partition blocked; retries continue for hours. [F3]
- `2026-05-12T07:45:00Z` settlement-service request_rate collapses to ~0.4 req/s; consumer_lag begins climbing steeply. [F4, F5]
- `2026-05-12T09:45:00Z` consumer_lag reaches ~61,605 messages; still climbing, zero settlements. [F5]
- `2026-05-12T09:58:31Z` Same poison record still blocking partition 3 (attempt 61790). [F3]

## Recommended actions
- **[immediate]** Immediate mitigation: stop the infinite retry loop — either roll back order-service dep-6010 to the previous version (restoring the v1 qty field) or deploy a settlement-service fix that tolerates/handles the v2 schema, then reset the stuck consumer offset past orders-3@8841207 or route it to a dead-letter queue. -- The blocked partition is the direct cause of zero settlements and growing lag; the poison record cannot be processed by the current v1 deserializer. [F1, F2, F3, F4]
- **[short_term]** Drain/rewind the blocked partition and re-process the backlog after the schema fix is live, monitoring consumer_lag return to baseline (~<100). -- 61k+ records accumulated behind the blocked partition must be replayed once consumers can deserialize them. [F5]
- **[short_term]** Do NOT treat cfg-3004 (poll batch size) as the cause; leave it as-is unless separately justified. -- Evidence attributes the stall to schema deserialization, not poll settings; changing it back would not help. [F6]
- **[long_term]** Adopt backward-compatible schema evolution and a contract test between order-service (producer) and settlement-service (consumer): register schemas, enforce compatibility on deploy, and add DLQ + max-retry caps for poison messages. -- A field rename with a version bump bypassed consumer compatibility and an unbounded retry loop amplified it into a full outage. [F1, F2, F3]

## Data gaps
- No direct evidence of settlement-service's deployed schema version/config before 07:40 (its own deploys in the window were not listed) — assumed it requires v1 'qty'; a coordination deploy cannot be fully excluded.
- Only order-service's INFO logs were sampled; the exact per-record payload of orders-3@8841207 is inferred from the error text, not seen directly.
- Kafka-side consumer group state / partition assignment metrics were not directly available (only absence of Kafka error logs).

## Investigation stats
- latency 41.47s, LLM calls 5, tokens 53092 (in 46772 / out 6320)
- tool calls 14 (0 failed, 0 partial, 0 recovered by retry), findings 10/10 verified
