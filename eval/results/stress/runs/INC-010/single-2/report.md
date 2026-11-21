# Incident investigation report: INC-010
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Settlements stalled because order-service deployment dep-6010 (v2.16.0, 07:40:01Z) renamed the OrderExecuted field 'qty' to 'quantity' and added a schema v2 header, while settlement-service still expects schema v1. Starting 07:42:14Z settlement-service logged 409 deserialization errors ('missing required field qty (schema v1 expected, header says v2)') and poison-message retries that block partition orders-3@8841207. Consumption halted (request_rate 62->0.4/s) and consumer lag climbed to 61,605. The config change cfg-3004 (max.poll.records 500->200) applied at 07:35:37 is a red herring: it preceded onset by ~7 minutes and cannot produce field-level schema errors. No restarts, memory/pool flat, HTTP error_rate flat and zero ledger-db errors rule out resource/infra/dependency causes. The visible CPU rise (~70%) is the busy retry loop, not the origin. Root cause category: bad_deployment originating in order-service.

## Probable root cause
**order-service v2.16.0 event schema change (qty->quantity) makes new OrderExecuted events undeserializable in settlement-service, poisoning partition orders-3 and stalling the consumer** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 event schema change (qty->quantity) makes new OrderExecuted events undeserializable in settlement-service, poisoning partition orders-3 and stalling the consumer | bad_deployment | order-service | dep-6010 | 0.90 (high) | active |
| 2 | H2: Config change cfg-3004 (consumer.max.poll.records 500->200) throttled settlement-service enough to stall consumption | config_change | settlement-service | cfg-3004 | 0.10 (low) | rejected |
| 3 | H3: Kafka infrastructure failure (partition reassignment / broker issue) stalled the settlement consumer | infrastructure_failure | kafka | - | 0.05 (low) | rejected |

### H1: order-service v2.16.0 event schema change (qty->quantity) makes new OrderExecuted events undeserializable in settlement-service, poisoning partition orders-3 and stalling the consumer
order-service deploy dep-6010 (v2.16.0) renamed the OrderExecuted field 'qty' to 'quantity' and stamped events with schema header v2. settlement-service still expects schema v1 with a required 'qty' field, so every new v2 event on orders-3 fails deserialization; the offset never advances (poison message), blocking the partition. Because the consumer cannot make progress, all downstream settlement processing stops and consumer lag grows unboundedly. The CPU rise is the busy retry loop, not the cause.

**Causal chain**
1. dep-6010 ships OrderExecuted v2 renaming qty->quantity with header v2 [F1]
2. settlement-service, still on schema v1, fails to deserialize: "missing required field 'qty' (schema v1 expected, header says v2)" [F3]
3. The bad record at orders-3@8841207 is retried as a poison message and the partition is blocked [F3, F11]
4. Consumer progress halts -> request_rate drops 62->0.4/s and consumer_lag grows to 61,605 [F5, F6]
5. Hot retry loop drives CPU to ~70% without memory/pool/restart changes [F7, F8]

**Supporting evidence**
  - `F1` ✔ order-service deployment dep-6010 (v2.16.0) rolled out at 2026-05-12T07:40:00Z; summary includes renaming the OrderExecuted event field 'qty' -> 'quantity' and adding a schema v2 header.
  - `F3` ✔ settlement-service logs show 409 ERROR entries starting 2026-05-12T07:42:14Z, dominated by 'failed to deserialize OrderExecuted ... missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences) and 'poison message: retrying orders-3@8841207 ... partition blocked' (196 occurrences).
  - `F4` ✔ The config-change log 'config change cfg-3004 applied: consumer.max.poll.records=200' was emitted at 2026-05-12T07:35:37Z, ~7 minutes BEFORE the first deserialize error at 07:42:14Z.
  - `F5` ✔ settlement-service request_rate collapsed from ~62 req/s (07:15 bucket) to 0.4 req/s at the 07:45 bucket and stayed flat at ~0.4 for the remainder of the window.
  - `F6` ✔ settlement-service consumer_lag rose from ~40-60 (07:15 bucket) to 61,605.53 by the 09:45 bucket, monotonically increasing.
  - `F7` ✔ settlement-service CPU rose from ~28% (07:15) to ~70% (07:45 onward), while memory_pct stayed flat ~50% and db_pool_in_use stayed flat ~6 connections.
  - `F11` ✔ The poison-message retry attempt counters on partition orders-3@8841207 are wildly inconsistent and enormous (92490, 25391, 56347, 4411...), consistent with a tight hot retry loop on a single blocked message rather than normal processing.
**Contradicting evidence**
  - (none)
**Assumptions**
  - settlement-service did not receive a matching schema-v2 code change (no settlement deployment appears in the window)
  - the partition-3 events after 07:40 are in fact v2 payloads emitted by dep-6010

### H2: Config change cfg-3004 (consumer.max.poll.records 500->200) throttled settlement-service enough to stall consumption
The reported guess: smaller poll batches reduced throughput and caused the settlement consumer to fall behind and stop posting. This is the natural suspect because it is on the reported service and was changed shortly before onset.

**Causal chain**
1. cfg-3004 applied 07:35:37 lowered max.poll.records to 200 [F2, F4]
2. Reduced batch size slows consumption and lag grows [F6]

**Supporting evidence**
  - `F2` ✔ settlement-service consumer config change cfg-3004 lowered consumer.max.poll.records from 500 to 200, applied at 2026-05-12T07:35:00Z.
  - `F4` ✔ The config-change log 'config change cfg-3004 applied: consumer.max.poll.records=200' was emitted at 2026-05-12T07:35:37Z, ~7 minutes BEFORE the first deserialize error at 07:42:14Z.
**Contradicting evidence**
  - `F3` ✔ settlement-service logs show 409 ERROR entries starting 2026-05-12T07:42:14Z, dominated by 'failed to deserialize OrderExecuted ... missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences) and 'poison message: retrying orders-3@8841207 ... partition blocked' (196 occurrences).
  - `F4` ✔ The config-change log 'config change cfg-3004 applied: consumer.max.poll.records=200' was emitted at 2026-05-12T07:35:37Z, ~7 minutes BEFORE the first deserialize error at 07:42:14Z.
  - `F5` ✔ settlement-service request_rate collapsed from ~62 req/s (07:15 bucket) to 0.4 req/s at the 07:45 bucket and stayed flat at ~0.4 for the remainder of the window.
  - `F8` ✔ settlement-service restarts stayed 0 throughout (cumulative), and its HTTP error_rate stayed flat at ~0.2% with no change at onset.
**Assumptions**
  - a poll-size reduction alone is sufficient to halt consumption entirely
**Rejected because:** The errors are field-specific deserialization failures ('missing required field qty ... header says v2'), not throughput symptoms; and they began 07:42:14, ~7 min after the config was applied, matching the dep-6010 rollout (07:40:01) far better. A smaller poll batch would slow processing gradually, not produce a partition-blocking poison message or a hard drop to ~0.4 req/s with a schema-mismatch error. F8 shows no restart/error-rate change consistent with pure throttling.

### H3: Kafka infrastructure failure (partition reassignment / broker issue) stalled the settlement consumer
Echoing historical incidents HIST-112/HIST-104, a broker-side partition reassignment or outage could have removed the consumer's assignment and stalled settlements.

**Causal chain**
1. Broker/partition event stalls the consumer group [F12]

**Supporting evidence**
  - `F12` ✔ Historical incident HIST-112 (settlement consumer stalled, growing lag, stuck settlements) was attributed to a Kafka infrastructure partition-assignment issue — a superficially similar symptom but different mechanism.
**Contradicting evidence**
  - `F3` ✔ settlement-service logs show 409 ERROR entries starting 2026-05-12T07:42:14Z, dominated by 'failed to deserialize OrderExecuted ... missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences) and 'poison message: retrying orders-3@8841207 ... partition blocked' (196 occurrences).
  - `F5` ✔ settlement-service request_rate collapsed from ~62 req/s (07:15 bucket) to 0.4 req/s at the 07:45 bucket and stayed flat at ~0.4 for the remainder of the window.
  - `F8` ✔ settlement-service restarts stayed 0 throughout (cumulative), and its HTTP error_rate stayed flat at ~0.2% with no change at onset.
  - `F11` ✔ The poison-message retry attempt counters on partition orders-3@8841207 are wildly inconsistent and enormous (92490, 25391, 56347, 4411...), consistent with a tight hot retry loop on a single blocked message rather than normal processing.
**Assumptions**
  - a broker event coincided with onset
**Rejected because:** The consumer is clearly still assigned and actively retrying a specific offset on orders-3@8841207 (poison message/partition blocked), i.e. it is running and failing on message content, not disconnected. The failure signature is deserialization of event schema, which a broker event cannot explain. Kafka logs could not be retrieved to fully confirm, but the on-service evidence already contradicts this.

## Facts (verified observations)
- order-service dep-6010 v2.16.0 rolled out at 07:40:00Z renaming OrderExecuted field qty->quantity and adding schema v2 header. [F1]
- settlement-service cfg-3004 lowered consumer.max.poll.records 500->200 at 07:35:00Z. [F2]
- settlement-service emitted 409 ERROR log entries from 07:42:14Z, chiefly deserialization failures on missing 'qty' with header v2 and poison-message partition-blocked retries. [F3]
- The cfg-3004 application log (07:35:37Z) precedes the first deserialize error (07:42:14Z) by ~7 minutes. [F4]
- settlement-service request_rate dropped from ~62 req/s to 0.4 req/s from the 07:45 bucket onward. [F5]
- settlement-service consumer_lag grew to 61,605.53 by 09:45. [F6]
- settlement-service CPU reached ~70% while memory (~50%) and db_pool_in_use (~6) stayed flat. [F7]
- settlement-service restarts stayed 0 and HTTP error_rate stayed ~0.2%. [F8]
- order-service logs after its rollout were all healthy INFO entries. [F9]
- ledger-db produced zero ERROR logs in the window. [F10]
- Poison-message retry counters on orders-3@8841207 are enormous and inconsistent, indicating a hot retry loop. [F11]

## Assumptions (not verified)
- settlement-service received no code deployment in the window matching schema v2 (only order-service dep-6010 appears), so its v1 deserializer is unchanged.
- The v2 payloads on orders-3 after 07:40 were produced by dep-6010's OrderExecuted change.
- Tool result incompleteness: settlement-service log query tc-005/tc-018 returned truncated=true, and ledger-db cpu_pct (tc-008) and settlement latency_p99 (tc-020) failed; these gaps do not affect the schema-mismatch conclusion but mean the full tail of logs was not reviewed.

## Timeline
- `2026-05-12T07:35:00Z` Config change cfg-3004 applied to settlement-service (consumer.max.poll.records 500->200). [F2, F4]
- `2026-05-12T07:40:00Z` order-service deployment dep-6010 (v2.16.0) completes rollout; renames OrderExecuted qty->quantity, adds schema v2 header. [F1, F9]
- `2026-05-12T07:42:14Z` First settlement-service deserialization error: missing required field 'qty' (schema v1 expected, header says v2) at orders-3@8841207. [F3]
- `2026-05-12T07:44:51Z` First poison-message entry: retrying orders-3@8841207, partition blocked. [F3, F11]
- `2026-05-12T07:45:00Z` settlement-service request_rate collapses to 0.4 req/s; consumer_lag begins monotonic climb; CPU rises to ~70%. [F5, F6, F7]
- `2026-05-12T08:22:00Z` Incident INC-010 reported (settlements stuck ~40 min, lag ~60k). []
- `2026-05-12T09:45:00Z` consumer_lag reaches 61,605.53. [F6]

## Recommended actions
- **[immediate]** Roll back order-service dep-6010 (v2.16.0) to restore v1 OrderExecuted events, or hotfix order-service to keep emitting the 'qty' field. -- The fault originates in the schema-changing release; reverting stops new incompatible v2 events from being produced. [F1, F3]
- **[immediate]** After fixing the producer, reset/skip the blocked offset for orders-3@8841207 (or deploy a settlement-service consumer that tolerates v2) to unblock the partition; never replay the poison record as-is. -- The partition is stuck retrying a single undeserializable record; consumption cannot resume until it is handled. [F3, F11]
- **[short_term]** Do NOT rely on the cfg-3004 poll-size change as the cause; keep it under observation but do not treat it as the fix. -- cfg-3004 preceded onset by 7 min and cannot explain field-level schema errors; misattribution would waste the recovery window. [F2, F4, F8]
- **[short_term]** Alert on poison-message/partition-blocked events and on consumer request_rate deviation, not just lag, so a stuck consumer is caught within minutes. -- Lag growth was the visible symptom but the root signal (deserialization failure + request_rate collapse) appeared at 07:42 and detection lagged. [F3, F5]
- **[long_term]** Add CI/compatibility gating and a shared event schema registry with backward-compatibility checks (and consumer-side default handling) for OrderExecuted-style events. -- A field rename without schema-registry enforcement caused cross-service breakage; prevention requires the contract to be enforced before deploy. [F1, F3]

## Data gaps
- ledger-db cpu_pct metric could not be retrieved (upstream timeout, tc-008/tc-009) - could not directly confirm ledger-db was not resource-stressed, though zero ledger-db error logs argue against it.
- settlement-service latency_p99_ms query failed (backend 503, tc-020) - latency trend unverified.
- kafka broker/service logs could not be retrieved (timeout, tc-017), so broker-side partition-reassignment health was not directly confirmed; inference relies on the consumer still being assigned and retrying a specific offset.
- settlement-service error logs were truncated (tc-005 returned 40 of 409; tc-018 returned 30 of 59) - full log tail not reviewed.
- No order-service event payload sample was inspected to 100% confirm the emitted schema is v2 (inferred from the schema header in settlement-service errors and dep-6010's change summary).

## Investigation stats
- latency 181.11s, LLM calls 6, tokens 70194 (in 62202 / out 7992)
- tool calls 22 (5 failed, 0 partial, 5 recovered by retry), findings 12/12 verified
