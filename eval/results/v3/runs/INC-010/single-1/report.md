# Incident investigation report: INC-010
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Root cause is a breaking event-schema change deployed by order-service. Deploy dep-6010 (order-service v2.16.0, 2026-05-12T07:40Z) renamed the OrderExecuted field qty->quantity and bumped the schema to v2. settlement-service, which expects schema v1 with 'qty', began failing to deserialize every OrderExecuted message at 07:42:14Z ('missing required field qty (schema v1 expected, header says v2)'), then treated the message as a poison message and retried it endlessly, blocking partition orders-3. As a result zero settlements posted and consumer_lag climbed from ~45 to 61605 by 09:45; the retry storm drove CPU to ~70%. The reported consumer poll-config change (cfg-3004, 07:35) is a coincidence/red herring: it cannot produce schema-mismatch errors, and the error onset tracks the order-service deploy. kafka and ledger-db are healthy, ruling out infrastructure/dependency causes. Fix: roll back or fix order-service v2.16.0 to emit schema v1-compatible events (or add settlement-service compatibility for v2), and skip/unblock the poisoned offset.

## Probable root cause
**order-service v2.16.0 schema change broke settlement-service deserialization** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 schema change broke settlement-service deserialization | bad_deployment | order-service | dep-6010 | 0.90 (high) | active |
| 2 | H2: settlement-service consumer poll config change degraded throughput | config_change | settlement-service | cfg-3004 | 0.10 (low) | rejected |
| 3 | H3: Infrastructure failure: Kafka partition reassignment / broker issue | infrastructure_failure | kafka | - | 0.05 (low) | rejected |

### H1: order-service v2.16.0 schema change broke settlement-service deserialization
Deploy dep-6010 (order-service v2.16.0, 07:40) renamed the OrderExecuted field qty->quantity and bumped the event schema to v2. settlement-service still expects schema v1 with 'qty', so every OrderExecuted message on partition orders-3 fails to deserialize; the consumer treats it as a poison message, retries endlessly and blocks the partition, so no settlements are posted and consumer lag grows unbounded.

**Causal chain**
1. dep-6010 renames qty->quantity and sets schema header v2 on OrderExecuted [F1]
2. settlement-service expects schema v1 with 'qty' and fails deserialization [F3]
3. Poison message retried forever, blocking partition orders-3 [F3]
4. No settlements posted; consumer lag climbs to 61605 by 09:45 [F4]
5. Retry storm drives cpu_pct to ~70% [F5]

**Supporting evidence**
  - `F1` ✔ order-service was deployed at 2026-05-12T07:40:00Z as version v2.16.0, changing the OrderExecuted event: 'rename field qty -> quantity' and adding an event schema version header v2.
  - `F3` ✔ settlement-service emitted 409 ERROR log entries starting 2026-05-12T07:42:14Z, dominated by 'failed to deserialize OrderExecuted ... missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences) and 'poison message: retrying ... partition blocked' (196 occurrences).
  - `F4` ✘ unverified settlement-service consumer_lag was flat (~40-64) until 07:30, then rose steeply each 15-min bucket to max 61605.53 by 09:45 (e.g. 1306 at 07:30, 14401 at 08:00, 61605 at 09:45).
  - `F5` ✔ settlement-service cpu_pct rose from ~28% before 07:30 to ~70% from 07:45 onward, while memory_pct stayed flat (~50%) and db_pool_in_use stayed flat (~6).
  - `F9` ✔ The deserialization errors start at 07:42:14Z, ~2 minutes after the 07:40 order-service v2.16.0 deploy, and the consumer_lag inflection occurs in the 07:30-07:45 bucket — timing matches the order-service deploy, not the 07:35 config change.
  - `F8` ✔ settlement-service had no code deployments in the window (count 0 since 2026-05-01), so the service's own code was unchanged.
**Contradicting evidence**
  - `F2` ✔ settlement-service consumer config consumer.max.poll.records was changed from 500 to 200 at 2026-05-12T07:35:00Z.
**Assumptions**
  - The 07:42 first error is caused by the first v2 OrderExecuted message after the 07:40 deploy
  - No consumer-side compatibility shim exists for schema v2

### H2: settlement-service consumer poll config change degraded throughput
cfg-3004 lowered consumer.max.poll.records from 500 to 200 at 07:35, reducing batch throughput and letting lag accumulate. This is the change the reporter suspected.

**Causal chain**
(none given)

**Supporting evidence**
  - `F2` ✔ settlement-service consumer config consumer.max.poll.records was changed from 500 to 200 at 2026-05-12T07:35:00Z.
**Contradicting evidence**
  - `F3` ✔ settlement-service emitted 409 ERROR log entries starting 2026-05-12T07:42:14Z, dominated by 'failed to deserialize OrderExecuted ... missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences) and 'poison message: retrying ... partition blocked' (196 occurrences).
  - `F9` ✔ The deserialization errors start at 07:42:14Z, ~2 minutes after the 07:40 order-service v2.16.0 deploy, and the consumer_lag inflection occurs in the 07:30-07:45 bucket — timing matches the order-service deploy, not the 07:35 config change.
  - `F4` ✘ unverified settlement-service consumer_lag was flat (~40-64) until 07:30, then rose steeply each 15-min bucket to max 61605.53 by 09:45 (e.g. 1306 at 07:30, 14401 at 08:00, 61605 at 09:45).
**Rejected because:** A smaller poll batch would slow consumption but cannot produce 'failed to deserialize ... missing required field qty (schema v1 expected, header says v2)' errors or poison-message partition blocking. The errors are the direct cause of zero settlements, and they begin 2 min after the order-service deploy, not the 07:35 config change. The config change alone is insufficient to explain the hard stop.

### H3: Infrastructure failure: Kafka partition reassignment / broker issue
As in HIST-112, the consumer could have lost its partition assignment or hit a broker problem, causing lag to grow and settlements to stall.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F3` ✔ settlement-service emitted 409 ERROR log entries starting 2026-05-12T07:42:14Z, dominated by 'failed to deserialize OrderExecuted ... missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences) and 'poison message: retrying ... partition blocked' (196 occurrences).
  - `F7` ✔ kafka and ledger-db logs show no errors during the incident window (kafka only 2 benign 'connection reset' WARNs; ledger-db 0 matches).
**Rejected because:** kafka logs show no errors (only 2 benign connection-reset WARNs) and no rebalance/reassignment events. The errors are explicit deserialization failures tied to a schema version mismatch, which a broker problem cannot produce.

## Facts (verified observations)
- order-service v2.16.0 (dep-6010) deployed 07:40 renamed qty->quantity and added schema header v2 for OrderExecuted. [F1]
- settlement-service began logging deserialization failures for OrderExecuted at 07:42:14Z citing missing 'qty' and schema v1 vs header v2. [F3]
- The consumer then treated the message as poison and blocked partition orders-3, retrying endlessly. [F3]
- CPU rose to ~70% while memory and DB pool stayed flat, consistent with a retry storm rather than resource exhaustion. [F5]
- kafka and ledger-db showed no errors during the window. [F7]
- settlement-service itself had no code deployments, so the fault originates upstream at order-service. [F8]

## Assumptions (not verified)
- The first v2 OrderExecuted message on orders-3 arrived shortly after the 07:40 deploy, producing the 07:42:14 error.
- settlement-service has no runtime schema-v2 compatibility path.
- order-service v2.16.0 was not rolled back by the time of this analysis (status 'live').
- (demoted from facts: no verified observation backs it) settlement-service consumer_lag inflected in the 07:30-07:45 bucket and climbed to 61605 by 09:45.

## Timeline
- `2026-05-12T07:35:00Z` settlement-service config change cfg-3004: consumer.max.poll.records 500->200 [F2]
- `2026-05-12T07:40:00Z` order-service v2.16.0 (dep-6010) deployed: OrderExecuted qty->quantity, schema header v2 [F1]
- `2026-05-12T07:42:14Z` First deserialization error in settlement-service ('missing required field qty ... header says v2') [F3]
- `2026-05-12T07:44:51Z` Poison-message retries begin; partition orders-3 blocked [F3]
- `2026-05-12T07:45:00Z` consumer_lag bucket jumps (avg 4829) and CPU rises to ~70% [F4, F5]
- `2026-05-12T09:45:00Z` consumer_lag reaches max 61605.53 [F4]

## Recommended actions
- **[immediate]** Roll back order-service dep-6010 (v2.16.0) to the previous version, or hotfix it to emit OrderExecuted in schema v1 (field 'qty') or dual-write compatible events. -- The v2 schema is incompatible with settlement-service's v1 deserializer and is the source of all failures. [F1, F3]
- **[immediate]** Unblock the poisoned offset: skip/seek past orders-3@8841207 or deploy a settlement-service fix that tolerates v2, then restart the consumer group. -- The retry loop on a single poison message is blocking the whole partition and prevents any settlement posting. [F3]
- **[short_term]** Do not treat cfg-3004 (poll batch 500->200) as the cause; consider reverting only if throughput concerns remain after the schema fix. -- Evidence shows the config change cannot explain the schema-mismatch errors or the hard stop. [F2, F3]
- **[long_term]** Add schema-compatibility enforcement (contract tests / schema registry) between order-service producers and settlement-service consumers before future deploys. -- A field rename with a schema-version bump broke a downstream consumer; a registry would have caught this pre-deploy. [F1, F3]

## Data gaps
- No direct consumer-group/partition-assignment telemetry was available to confirm the partition-blocked state beyond the app log messages.
- Could not verify whether OrderExecuted messages after the deploy carry only the schema-version header without the renamed field in all partitions or only orders-3.
- No producer-side (order-service) metrics for event publish volume per schema version were available to size the backlog of v2 messages.

## Integrity warnings (set by code)
- fact demoted to assumption: settlement-service consumer_lag inflected in the 07:30-07:45 bucket and climbed 

## Investigation stats
- latency 32.03s, LLM calls 4, tokens 35879 (in 30629 / out 5250)
- tool calls 14 (0 failed, 0 partial, 0 recovered by retry), findings 8/10 verified
