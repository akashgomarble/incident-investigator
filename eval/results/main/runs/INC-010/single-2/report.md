# Incident investigation report: INC-010
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Root cause is the order-service deployment dep-6010 (v2.16.0) at 07:40, which renamed the OrderExecuted field qty -> quantity and set the event header to schema v2. settlement-service still deserializes with the v1 schema expecting 'qty', so the first v2 event (orders-3@8841207) fails to deserialize at 07:42:14, becomes a poison message, and repeatedly blocks partition orders-3, halting settlements and driving consumer_lag from ~48 to 60k+. The reported config change cfg-3004 (poll records 500->200 at 07:35) is a red herring: no errors or lag rise occurred in the 7 minutes after it applied, and it cannot explain a schema-deserialization failure. CPU rose to ~70% from the retry loop but DB pool stayed flat ~6, ruling out DB/resource exhaustion. The main fix is a rollback/canary of order-service dep-6010 (or a settlement-side v2 schema/backward-compatible consumer), not touching the poll config.

## Probable root cause
**order-service v2.16.0 event schema change breaks settlement deserialization** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.88, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 event schema change breaks settlement deserialization | bad_deployment | order-service | dep-6010 | 0.88 (high) | active |
| 2 | H2: Config change cfg-3004 (poll batch size) caused the stall | config_change | settlement-service | cfg-3004 | 0.05 (low) | rejected |
| 3 | H3: Kafka partition reassignment / broker issue (per HIST-112 pattern) | infrastructure_failure | kafka | - | 0.04 (low) | rejected |

### H1: order-service v2.16.0 event schema change breaks settlement deserialization
order-service deploy dep-6010 (v2.16.0, 07:40) renamed the OrderExecuted field qty -> quantity and bumped the schema header to v2, but settlement-service still deserializes with the v1 schema expecting 'qty'. The renamed event (orders-3@8841207) fails to deserialize, becomes a poison message, and repeatedly blocks partition orders-3, so settlement-service stops posting settlements from that partition and consumer lag grows unbounded.

**Causal chain**
1. order-service dep-6010 at 07:40 renames qty->quantity and sets event header v2 [F4, F5]
2. settlement-service consumer still uses v1 schema expecting 'qty' and fails to deserialize the first v2 event at 07:42:14 [F2]
3. the un-deserializable message becomes a poison message that blocks partition orders-3 (retried tens of thousands of times) [F3]
4. partition 3 stays blocked, no settlements posted, consumer_lag climbs from ~48 to 60k+ [F1, F9]
5. retry-loop spinning raises CPU to ~70% while DB pool stays flat, ruling out DB saturation [F8, F10]

**Supporting evidence**
  - `F1` ✔ settlement-service consumer_lag was flat at ~40-65 messages from 06:00 through the 07:30 bucket (avg 47.78), then jumped to avg 1572.1 (max 3512) in the 07:40 bucket and grew monotonically to avg 59267.74 (max 61605.53) by 09:50.
  - `F2` ✔ settlement-service first ERROR is at 07:42:14: "failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)". This exact pattern is the top error (213 occurrences) and persists through 09:58.
  - `F3` ✔ settlement-service logs a poison message on the same offset repeatedly: "poison message: retrying orders-3@8841207 (attempt ...), partition blocked" (196 occurrences, first_seen 07:44:51), with attempt counters in the tens of thousands.
  - `F4` ✔ order-service deployed dep-6010 v2.16.0 at 2026-05-12T07:40:00Z with summary "Order events v2" and changes: "OrderExecuted event: rename field qty -> quantity", "add field venue", "event schema version header v2". Status live, not rolled back.
  - `F5` ✔ order-service logged "deployment dep-6010 (v2.16.0) rollout complete, all pods ready" at 2026-05-12T07:40:01Z, immediately before settlement errors began.
  - `F8` ✔ settlement-service CPU jumped from ~27.5% (07:30 bucket) to ~60.8% (07:40) and held ~70% thereafter, while db_pool_in_use stayed flat at ~6 connections for the entire window.
  - `F9` ✔ settlement-service consumer_lag rise is confined to partition orders-3 (all errors reference orders-3@8841207), and HTTP error_rate for settlement-service stayed flat at ~0.2% throughout.
  - `F10` ✔ settlement-service logs show healthy DB pool ("connection pool stats: healthy") and successful scheduled-job heartbeats throughout, with no restart or crash lines.
**Contradicting evidence**
  - `F6` ✔ Config change cfg-3004 on settlement-service changed consumer.max.poll.records from 500 to 200 at 2026-05-12T07:35:00Z (applied per log at 07:35:37). This is the only config change in the window.
  - `F7` ✔ No ERROR-level entries appear between the cfg-3004 application (07:35:37) and 07:42:14; the two logs in that gap are INFO "scheduled job heartbeat ok".
**Assumptions**
  - The renamed 'quantity' field is not also emitted under a 'qty' alias, so v1 consumers cannot read it
  - orders-3 is the partition carrying the affected event stream and the blocked partition is a real availability impact, not an isolated bad record

### H2: Config change cfg-3004 (poll batch size) caused the stall
Lowering consumer.max.poll.records from 500 to 200 on settlement-service at 07:35 reduced throughput enough to stall the consumer and grow lag.

**Causal chain**
1. cfg-3004 applied at 07:35:37 [F6]
2. lag would start rising immediately after 07:35 *(assumed)*

**Supporting evidence**
  - `F6` ✔ Config change cfg-3004 on settlement-service changed consumer.max.poll.records from 500 to 200 at 2026-05-12T07:35:00Z (applied per log at 07:35:37). This is the only config change in the window.
**Contradicting evidence**
  - `F2` ✔ settlement-service first ERROR is at 07:42:14: "failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)". This exact pattern is the top error (213 occurrences) and persists through 09:58.
  - `F7` ✔ No ERROR-level entries appear between the cfg-3004 application (07:35:37) and 07:42:14; the two logs in that gap are INFO "scheduled job heartbeat ok".
  - `F1` ✔ settlement-service consumer_lag was flat at ~40-65 messages from 06:00 through the 07:30 bucket (avg 47.78), then jumped to avg 1572.1 (max 3512) in the 07:40 bucket and grew monotonically to avg 59267.74 (max 61605.53) by 09:50.
**Assumptions**
  - none — contradicted by timing
**Rejected because:** No errors or lag rise in the 7 minutes after cfg-3004 applied; the first deserialize error (07:42:14) and the lag spike (07:40 bucket) follow the order-service deploy at 07:40, not the config change. A smaller poll batch alone cannot produce a schema-deserialization poison message.

### H3: Kafka partition reassignment / broker issue (per HIST-112 pattern)
A Kafka broker rebalance or partition reassignment caused settlement-service to lose its assignment and stall.

**Causal chain**
1. partition reassignment causes lag and stuck settlements [F11]

**Supporting evidence**
  - `F11` ✔ Historical post-mortem HIST-112 (settlement-service, 2026-02-03) described the same lag/settlements-stuck symptom but attributed it to partition reassignment during broker maintenance — a different root cause than the current schema mismatch.
**Contradicting evidence**
  - `F2` ✔ settlement-service first ERROR is at 07:42:14: "failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)". This exact pattern is the top error (213 occurrences) and persists through 09:58.
  - `F3` ✔ settlement-service logs a poison message on the same offset repeatedly: "poison message: retrying orders-3@8841207 (attempt ...), partition blocked" (196 occurrences, first_seen 07:44:51), with attempt counters in the tens of thousands.
  - `F4` ✔ order-service deployed dep-6010 v2.16.0 at 2026-05-12T07:40:00Z with summary "Order events v2" and changes: "OrderExecuted event: rename field qty -> quantity", "add field venue", "event schema version header v2". Status live, not rolled back.
**Assumptions**
  - none — contradicted by logs
**Rejected because:** Logs show a specific deserialization failure and poison-message retry on offset orders-3@8841207 tied to an event schema/field rename, which partition reassignment would not produce. The historical match is a symptom-level lead only.

## Facts (verified observations)
- settlement-service consumer_lag exploded from ~48 (07:30 bucket) to 1572 (07:40) to 60k+ (09:50), starting exactly at the order-service deploy time. [F1, F4]
- settlement-service error stream shows a v1-vs-v2 schema mismatch: "missing required field 'qty' (schema v1 expected, header says v2)", first at 07:42:14. [F2]
- The un-deserializable offset orders-3@8841207 is retried as a poison message, blocking partition orders-3. [F3]
- order-service dep-6010 v2.16.0 renamed OrderExecuted qty -> quantity and bumped the schema header to v2 at 07:40. [F4, F5]
- The config change cfg-3004 (poll records 500->200 at 07:35) preceded no errors and no lag rise, and does not explain a deserialization failure. [F6, F7]
- settlement-service CPU rose to ~70% from the retry loop while db_pool_in_use stayed flat ~6 and pool was reported healthy. [F8, F10]
- HTTP error_rate on settlement-service stayed flat ~0.2%, so the impact is consumer-side, not request-serving. [F9]

## Assumptions (not verified)
- The renamed 'quantity' field has no 'qty' alias, so v1 consumers cannot read v2 events.
- The recurring orders-3@8841207 poison message is representative of the blocking condition (a real bad record), not merely a noisy retry counter.
- order-service is the sole producer of OrderExecuted events consumed by settlement-service.

## Timeline
- `2026-05-12T07:35:00Z` Config change cfg-3004 applied on settlement-service: consumer.max.poll.records 500->200 (log confirms at 07:35:37) [F6]
- `2026-05-12T07:40:00Z` order-service deploys dep-6010 v2.16.0 (OrderExecuted qty->quantity, schema header v2); rollout completes 07:40:01 [F4, F5]
- `2026-05-12T07:40:00Z` settlement-service consumer_lag jumps from ~48 to avg 1572 in this bucket [F1]
- `2026-05-12T07:42:14Z` First deserialization error: missing required field 'qty' (schema v1 expected, header says v2) [F2]
- `2026-05-12T07:44:51Z` First poison-message line: orders-3@8841207 retried, partition blocked [F3]
- `2026-05-12T08:22:00Z` Incident reported (settlements stuck ~40 min, lag 60k and climbing) []
- `2026-05-12T09:50:00Z` consumer_lag reaches avg 59267.74 / max 61605.53; errors still ongoing [F1, F2]

## Recommended actions
- **[immediate]** Roll back or pause order-service dep-6010 v2.16.0 (or re-emit OrderExecuted with a backward-compatible 'qty' alias / keep schema v1) to stop producing v2 events the consumer cannot read -- The schema change is the identified trigger; stopping v2 events removes the poison-message source so partition orders-3 can drain. [F2, F3, F4]
- **[immediate]** After the producer is fixed, skip/dead-letter the stuck offset orders-3@8841207 so partition 3 unblocks and the consumer catches up -- A single poison message is blocking the whole partition; it must be skipped or DLQ'd to restore posting. [F3]
- **[short_term]** Add a schema-registry compatibility gate (backward-compatible evolution, e.g. additive fields) so producers cannot emit a schema version consumers lack -- A rename + header bump was shipped without consumer readiness, breaking a live consumer. [F4, F2]
- **[short_term]** Make the consumer forward-tolerant: honor the event schema header and/or dead-letter un-deserializable records instead of blocking the partition indefinitely -- Poison-message handling turned one bad record into a full partition stall. [F3]
- **[long_term]** Do NOT revert cfg-3004 as the primary fix; if desired, re-evaluate the poll batch size separately after recovery -- The config change is not the cause; reverting it would not restore service and could confuse the fix. [F6, F7]

## Data gaps
- No order-service producer-side log confirming the exact payload/schema of offset orders-3@8841207 was retrieved; the qty->quantity mapping is inferred from the deploy change summary plus the consumer error text.
- No Kafka broker/partition telemetry was queried, so an independent broker-side contribution (e.g. rebalance) cannot be fully excluded — though logs point clearly to a schema failure.
- Consumer_lag metric is aggregate; per-partition lag was not directly retrieved, so the claim that only orders-3 is affected rests on the uniform orders-3@8841207 error reference.

## Investigation stats
- latency 40.4s, LLM calls 4, tokens 42326 (in 35980 / out 6346)
- tool calls 12 (0 failed, 0 partial, 0 recovered by retry), findings 11/11 verified
