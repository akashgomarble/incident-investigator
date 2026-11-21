# Incident investigation report: INC-010
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Settlement processing stalled at ~07:42Z because order-service v2.16.0 (deployed 07:40Z, the only deploy in the window) renamed the OrderExecuted field qty->quantity and added a v2 schema header, while settlement-service's consumer still expects schema v1; the first v2 message at orders-3@8841207 fails deserialization ('missing required field qty', header v2) and is retried forever, blocking that partition and freezing all settlement progress (confidence 0.72, medium). The onset is tightly aligned: deploy 07:40:00, lag rise 07:37-07:44, throughput collapse 07:42:00, first bad log 07:42:14. Alternatives were rejected on mechanical grounds rather than mere absence of evidence: the reporter's cfg-3004 poll change cannot block an offset (the consumer is actively polling and retrying), the shared ledger-db is healthy (flat db_pool_in_use and 5xx), and a broker-wide event is contradicted by the healthy sibling consumer notification-service on the same shared broker (flat lag ~60, normal throughput, zero under-replication). The remaining weakness is that no per-partition or producer-side telemetry directly proves offset 8841207 was emitted by order-service, and no rollback/restart was attempted to serve as a natural experiment.

## Probable root cause
**order-service v2.16.0 schema rename (qty->quantity, header v2) produces a poison message that blocks partition orders-3** -- bad_deployment in `order-service`, triggered by `order-service v2.16.0 (2026-05-12T07:40:00Z)` (confidence 0.72, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 schema rename (qty->quantity, header v2) produces a poison message that blocks partition orders-3 | bad_deployment | order-service | order-service v2.16.0 (2026-05-12T07:40:00Z) | 0.72 (medium) | active |
| 2 | H3: Broker-side partition reassignment / infra event on the shared kafka broker caused the stall (historical analogy) | infrastructure_failure | kafka | null | 0.06 (low) | rejected |
| 3 | H2: settlement-service cfg-3004 poll-settings change caused the consumer to stop fetching (reporter's suspicion) | config_change | settlement-service | cfg-3004 | 0.05 (low) | rejected |
| 4 | H5: Poison message from a source other than order-service's v2 schema blocks the partition | data_issue | unknown (message producer) | null | 0.05 (low) | rejected |
| 5 | H4: Shared ledger-db degradation back-pressures settlement processing | dependency_failure | ledger-db | null | 0.04 (low) | rejected |

### H1: order-service v2.16.0 schema rename (qty->quantity, header v2) produces a poison message that blocks partition orders-3
order-service v2.16.0 (deployed 07:40) changed the OrderExecuted event schema: field qty renamed to quantity and a v2 schema header added. settlement-service's consumer (built for schema v1) cannot deserialize the first v2 message at orders-3@8841207, throws 'missing required field qty', and retries that offset indefinitely, blocking the partition. No offsets advance, so lag grows monotonically and zero settlements post.

**Causal chain**
1. order-service v2.16.0 deploys at 07:40:00Z, the single deployment in the fleet window, renaming OrderExecuted field qty->quantity, adding field venue, and adding a v2 schema version header; status live, not rolled back [F1, F32, F33]
2. order-service is a declared producer ('queue-produce') to the shared kafka broker that settlement-service consumes, so it is a legitimate writer to the orders topic [F3, F5, F36]
3. settlement-service's consumer expects schema v1 and cannot deserialize the v2 message: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field qty (schema v1 expected, header says v2)' [F10, F28, F32]
4. The error header version (v2) and the missing field name (qty) match order-service v2.16.0's declared changes exactly, linking the poison message content to that release [F32, F10]
5. The single poison message at orders-3@8841207 blocks the partition and is retried without advancing the offset ('poison message: retrying orders-3@8841207, partition blocked') [F10, F29, F13]
6. No offsets advance, so settlement throughput collapses (~65 -> ~0.4 req/s) and consumer lag grows monotonically (~50 -> 60k+) [F8, F9, F30, F13]

**Supporting evidence**
  - `F1` ✔ Only ONE deployment occurred across all services in 07:30-09:59: order-service v2.16.0 at 2026-05-12T07:40:00Z ('Order events v2'), which renames event field qty -> quantity, adds field venue, and adds event schema version header v2. Status live, not rolled back.
  - `F32` ✔ order-service producer-side change content matches the poison-message signature exactly: dep-6010 v2.16.0 at 07:40:00Z lists 'OrderExecuted event: rename field qty -> quantity', 'add field venue', 'event schema version header v2' — the same field name the consumer says is missing and the same header version the consumer reports.
  - `F33` ✔ No rollback of order-service v2.16.0 occurred in 07:30-09:59: the single deployment in the whole fleet window is dep-6010 with status 'live' and rolled_back_at null.
  - `F10` ✔ The first bad log entry is at 07:42:14Z: a deserialization failure on a single message. Dominant signature = poison message on partition orders-3 stuck at offset 8841207: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences, first_seen 07:42:14) and 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first_seen 07:44:51).
  - `F28` ✔ The deserialization-failure signature is confined to a single message/partition: 213 occurrences, ALL at 'orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)', first_seen 07:42:14, last 09:58:00. No deserialization errors on any other partition or topic are present.
  - `F29` ✔ The 'partition blocked' retry storm is likewise (216 aggregated) all on orders-3@8841207: the poison-message pattern counts 196 with first_seen 07:44:51, all quoting 'retrying orders-3@8841207 ... partition blocked'. No partition besides orders-3 is named.
  - `F8` ✔ Settlement throughput (request_rate) collapsed from ~62-67 req/s to ~0.4 req/s between the 07:41 bucket (avg 65.3) and the 07:42 bucket (avg 0.39), and stayed at ~0.4 req/s through 09:55. This is the throughput-drop onset: 07:42:00Z.
  - `F9` ✔ Consumer lag was flat/baseline (~36-56 messages, 5-min buckets) through 07:35, then rose sharply: 07:40 avg 515.03 (max 1306.47), 07:45 avg 2629.17, and grew monotonically to avg 60220.76 / max 61605.53 at 09:55. Lag starts rising in the 07:40-07:45 bucket, i.e. concurrent with the throughput collapse at 07:42.
  - `F30` ✔ Aggregate consumer_lag rose from a flat ~47 avg (07:30 bucket) to 202.21 (07:37) and 2616.82 (07:44), continuing monotonically to 60713.43 avg / 61605.53 max at 09:57. The rise begins in the 07:37-07:44 region, i.e. within one 7-min bucket of both the deployment (07:40) and the first deserialize error (07:42:14); no metric evidence shows lag growing on a specific partition independently.
  - `F13` ✔ Ordering: first error log (07:42:14Z) coincides with the first zero-throughput minute (07:42 bucket avg 0.39) and the 07:40-07:45 lag rise, so all three signals onset together at ~07:42 RUNNING. A single poison message on orders-3 offset 8841207 is blocking the partition, which is consistent with (but does not alone prove) the throughput drop and lag growth being consumer-side rather than downstream.
  - `F3` ✔ kafka is a SHARED broker: order-service and payment-service produce to it, while settlement-service AND notification-service both consume from it. So any broker-side disruption affects two consumers.
  - `F5` ✔ settlement-service's declared dependencies are exactly two: it CONSUMES the shared kafka queue and calls the shared ledger-db. Direct upstream producers feeding the queue are order-service and payment-service; no dedicated 'posting/ledger' downstream service is declared beyond ledger-db.
  - `F36` ✔ order-service's only queue edge is a produce edge to the shared kafka broker ('queue-produce'), making it a legitimate writer to the topic settlement-service consumes; its other dependencies are ledger-db, pricing-engine and exchange-gateway (plus api-gateway calling in), none of which is a plausible source of the v2 event header.
  - `F16` ✔ No historical incident matches today's actual signature: searching 'poison message deserialization failure partition blocked offset' returned no poison-message/deserialization precedent -- top hits were HIST-104 (score 1), HIST-107 redis cache storm (score 1), HIST-112 (score 1). Likewise, searching for an upstream producer schema/rename breaking a consumer returned no such record (only unrelated HIST-103/104/112/116, all score 1). So there is NO historical precedent for the schema-header v2 / missing-field 'qty' poison-message pattern seen in INC-010.
  - `F25` ✔ Pattern consistent with H1 and against H3: the kafka broker is shared by both consumers (F3), yet only settlement-service shows lag rise/throughput collapse while notification-service stays healthy on lag and throughput and the one available broker health metric is flat — a broker-wide event affecting both consumers and the broker is not visible in the data.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The v2 message at orders-3@8841207 was produced by order-service specifically (no per-partition producer telemetry exists; linkage rests on deploy-content + error-text match + timing)
  - settlement-service's consumer was not updated in lockstep with the v2 schema (no settlement-service deployment in window per F1/F35)
  - The poll-config change cfg-3004 is coincidental/ineffective as a stall cause (no poll/rebalance/timeout log evidence, F11)
  - The poison message blocks partition orders-3 only; errors on other partitions of the orders topic are not visible in the retrieved (truncated) logs
**Rejected because:** null

### H3: Broker-side partition reassignment / infra event on the shared kafka broker caused the stall (historical analogy)
A broker-side disruption (partition reassignment, rebalance, broker maintenance) on the shared kafka broker stalled the settlement consumer, matching HIST-112/HIST-104. The poison message and v2 deploy are incidental.

**Causal chain**
1. A broker-side reassignment/rebalance occurs on the shared kafka broker (unrecorded; no broker rebalance/reassignment metric exists to observe it) *(assumed)*
2. settlement-service loses/regains partition assignment and stops advancing offsets *(assumed)*
3. Lag grows and settlements stall [F9, F8]

**Supporting evidence**
  - `F7` ✔ Historical precedent leads (not evidence for this incident): HIST-112 (2026-02-03) 'Settlement consumer stalled after partition reassignment' with symptoms 'consumer lag growing','settlements stuck' matched settlement-service+kafka with match_score 5; HIST-104 matched notification-service+kafka lag after broker rebalance. Both point to partition-reassignment/infra causes, unlike the config/event-schema changes seen today.
  - `F14` ✔ Historical precedent (broker/rebalance cause): HIST-112 (2026-02-03) 'Settlement consumer stalled after partition reassignment', services settlement-service+kafka, symptoms 'consumer lag growing','settlements stuck', root_cause 'Consumer lost partition assignment during broker maintenance', category infrastructure_failure, resolution 'Restarted consumer group', match_score 4-5 (highest match in every query). This is the single closest prior event to today's settlement stall.
  - `F15` ✔ Historical precedent (second consumer-side storm): HIST-104 (2025-11-05) 'notification-service lag after Kafka broker rebalance', services notification-service+kafka, symptoms 'consumer lag spike','delayed emails', root_cause 'Partition reassignment caused consumer group rebalancing storm', category infrastructure_failure, resolution 'Throttled reassignment'. Same shared broker and the other kafka consumer as today.
  - `F19` ✔ Detection signature across the historical broker/rebalance stalls: all report a lag-RISE symptom ('consumer lag growing' / 'consumer lag spike') plus a processing-side symptom ('settlements stuck' / 'delayed emails'), and all resolve via a consumer-group / reassignment action ('Restarted consumer group', 'Throttled reassignment'), i.e. detection was lag-growth + stalled processing -- the same surface symptoms as INC-010, even though today's logs show a poison message rather than a rebalance.
  - `F3` ✔ kafka is a SHARED broker: order-service and payment-service produce to it, while settlement-service AND notification-service both consume from it. So any broker-side disruption affects two consumers.
**Contradicting evidence**
  - `F11` ✔ All 409 WARN+ log entries in 07:30-09:59 are level ERROR; there are no rebalance-loop, session-timeout, or poll-timeout messages, and no DB/ledger/posting/5xx downstream-failure messages. The only patterns present are the two above.
  - `F6` ✔ No config changes were found for kafka (scope=kafka returned count 0) in 07:30-09:59, so the broker/cluster infra shows no recorded config change in the window.
  - `F10` ✔ The first bad log entry is at 07:42:14Z: a deserialization failure on a single message. Dominant signature = poison message on partition orders-3 stuck at offset 8841207: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences, first_seen 07:42:14) and 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first_seen 07:44:51).
  - `F20` ✔ notification-service consumer_lag is flat/baseline throughout 06:30-09:57 with no rise at 07:42 or later: mean 60.11 messages, every 9-min bucket avg between ~52 and ~66 (e.g. 07:42 avg 62.1, 09:57 avg 52.57). No lag growth concurrent with the settlement stall.
  - `F21` ✔ notification-service request_rate (throughput) is normal and flat across the whole window: min 132.42, max 166.73, mean 147.31 req/s; 07:42 avg 151.53, 09:57 avg 143.7 — no drop or spike, i.e. the other consumer kept processing normally while settlement-service stalled.
  - `F22` ✔ The only WARN+ log from notification-service in 07:30-09:59 is a single unrelated NTP message: 'clock drift 38ms detected, resyncing with NTP' at 08:39:18Z (total_matched 1, truncated false). No rebalance, session-timeout, or poll-timeout symptom appears.
  - `F23` ✔ Broker metric under_replicated_partitions is exactly 0.0 for every 9-min bucket from 06:30 to 09:57 (min 0.0, max 0.0, missing_points 0) — no under-replication activity across the stall window.
  - `F25` ✔ Pattern consistent with H1 and against H3: the kafka broker is shared by both consumers (F3), yet only settlement-service shows lag rise/throughput collapse while notification-service stays healthy on lag and throughput and the one available broker health metric is flat — a broker-wide event affecting both consumers and the broker is not visible in the data.
**Assumptions**
  - An unrecorded broker event occurred despite no kafka config change and no under-replication activity
**Rejected because:** Rejected on direct sibling-consumer evidence, not merely absence of broker telemetry. The broker is shared by TWO consumers (F3, F25), yet the other consumer notification-service shows flat baseline lag (mean ~60, no rise at 07:42 or later — F20), fully normal flat throughput (132-167 req/s — F21), and no rebalance/session-timeout/poll-timeout logs (only one unrelated NTP message — F22). under_replicated_partitions is exactly 0.0 for every bucket (F23). A broker-wide reassignment affecting the shared broker would degrade both consumers; the impact is confined to settlement-service, and the observed error is a content-specific deserialization failure at a named offset (F10), not an assignment loss. Historical analogy (HIST-112/104) carries no dependency/partition telemetry (F14/F15) and no poison-message precedent exists in the corpus (F16).

### H2: settlement-service cfg-3004 poll-settings change caused the consumer to stop fetching (reporter's suspicion)
The settlement-service config change cfg-3004 at 07:35 (consumer.max.poll.records 500->200) triggered the stall — e.g. a mis-set poll parameter caused the consumer to stop advancing. This is the reporter's stated suspicion.

**Causal chain**
1. cfg-3004 changes consumer.max.poll.records 500->200 at 07:35, preceding the stall [F2]
2. Mis-set poll parameter causes consumer to stop fetching/deserializing, so no offsets advance *(assumed)*
3. Throughput collapses and lag grows [F8, F9]

**Supporting evidence**
  - `F2` ✔ Only ONE config change occurred across all services in the window: settlement-service cfg-3004 at 2026-05-12T07:35:00Z, key consumer.max.poll.records changed from 500 to 200 by m.das ('Smaller poll batches to smooth DB writes'). This precedes the stall.
**Contradicting evidence**
  - `F11` ✔ All 409 WARN+ log entries in 07:30-09:59 are level ERROR; there are no rebalance-loop, session-timeout, or poll-timeout messages, and no DB/ledger/posting/5xx downstream-failure messages. The only patterns present are the two above.
  - `F10` ✔ The first bad log entry is at 07:42:14Z: a deserialization failure on a single message. Dominant signature = poison message on partition orders-3 stuck at offset 8841207: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences, first_seen 07:42:14) and 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first_seen 07:44:51).
  - `F13` ✔ Ordering: first error log (07:42:14Z) coincides with the first zero-throughput minute (07:42 bucket avg 0.39) and the 07:40-07:45 lag rise, so all three signals onset together at ~07:42 RUNNING. A single poison message on orders-3 offset 8841207 is blocking the partition, which is consistent with (but does not alone prove) the throughput drop and lag growth being consumer-side rather than downstream.
  - `F17` ✔ Consumer-side CONFIG-change precedents exist but are a different mechanism: HIST-115 (2025-09-25) 'order-service connection pool misconfiguration', symptoms 'connection is not available','request timed out','pool exhausted', category config_change, resolution 'Reverted config; added validation'. The only config-change-classified lag-adjacent precedent involving settlement-service is HIST-111 (2025-05-22) 'Reporting job full table scans slowed ledger-db' (services ledger-db,settlement-service), whose cause was a bad query on the shared ledger-db -- not a poll-settings change. No precedent was found where a max.poll.records-type consumer setting caused a stall.
  - `F28` ✔ The deserialization-failure signature is confined to a single message/partition: 213 occurrences, ALL at 'orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)', first_seen 07:42:14, last 09:58:00. No deserialization errors on any other partition or topic are present.
**Assumptions**
  - max.poll.records=200 is small enough to throttle fetch to a full stall (implausible — 200 is a normal batch size)
**Rejected because:** Mechanism mismatch: reducing max.poll.records to 200 slows batch size, it does not block an offset or stop all progress, and the consumer demonstrably IS polling and actively retrying offset 8841207 (F10/F28). No rebalance-loop, session-timeout, or poll-timeout logs exist — the only error signatures are the deserialization/poison patterns (F11). No precedent exists in the corpus for a max.poll.records-type setting causing a stall (F17). The actual failure is a content-specific deserialization error naming the v2 header and missing qty field (F10), which poll settings cannot produce.

### H5: Poison message from a source other than order-service's v2 schema blocks the partition
A malformed/poison message (from a non-order-service source, or a pre-existing corrupt event) sits at orders-3@8841207 and blocks the partition, with order-service v2.16.0 merely coincident in time. settlement-service is victim, not cause.

**Causal chain**
1. A malformed message is produced to orders-3 (source and timing unknown) *(assumed)*
2. consumer cannot deserialize and retries offset 8841207 indefinitely, blocking the partition [F10]
3. Offsets stop advancing; throughput collapses and lag grows [F8, F9, F13]

**Supporting evidence**
  - `F10` ✔ The first bad log entry is at 07:42:14Z: a deserialization failure on a single message. Dominant signature = poison message on partition orders-3 stuck at offset 8841207: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences, first_seen 07:42:14) and 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first_seen 07:44:51).
  - `F13` ✔ Ordering: first error log (07:42:14Z) coincides with the first zero-throughput minute (07:42 bucket avg 0.39) and the 07:40-07:45 lag rise, so all three signals onset together at ~07:42 RUNNING. A single poison message on orders-3 offset 8841207 is blocking the partition, which is consistent with (but does not alone prove) the throughput drop and lag growth being consumer-side rather than downstream.
**Contradicting evidence**
  - `F1` ✔ Only ONE deployment occurred across all services in 07:30-09:59: order-service v2.16.0 at 2026-05-12T07:40:00Z ('Order events v2'), which renames event field qty -> quantity, adds field venue, and adds event schema version header v2. Status live, not rolled back.
  - `F10` ✔ The first bad log entry is at 07:42:14Z: a deserialization failure on a single message. Dominant signature = poison message on partition orders-3 stuck at offset 8841207: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences, first_seen 07:42:14) and 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first_seen 07:44:51).
  - `F32` ✔ order-service producer-side change content matches the poison-message signature exactly: dep-6010 v2.16.0 at 07:40:00Z lists 'OrderExecuted event: rename field qty -> quantity', 'add field venue', 'event schema version header v2' — the same field name the consumer says is missing and the same header version the consumer reports.
**Assumptions**
  - A malformed message could originate independently of the v2 schema change
**Rejected because:** The error text explicitly names the v2 schema: 'missing required field qty (schema v1 expected, header says v2)' (F10). The v2 header exactly matches order-service v2.16.0's added v2 schema version header, and 'qty' is exactly the field that release renamed to 'quantity' (F1/F32). This is the new schema, not random corruption; timing (deploy 07:40, first bad message 07:42:14) also aligns (F13).

### H4: Shared ledger-db degradation back-pressures settlement processing
The shared ledger-db (used by four services) degraded, causing settlement-service's calls to hang and back-pressure the consumer, stalling settlements. Because the broker is shared, other services would surface it too.

**Causal chain**
1. Shared ledger-db degrades (failover, IOPS throttling, disk full, slow scan — all precedented) [F4, F18]
2. settlement-service DB calls slow/fail, holding the consumer *(assumed)*
3. Throughput collapses and offsets stop advancing [F8, F9]

**Supporting evidence**
  - `F4` ✔ ledger-db is a SHARED postgres used by four services: order-service, portfolio-service, payment-service and settlement-service. settlement-service's only datastore edge is to this shared ledger-db.
  - `F18` ✔ Shared-component precedents matching concurrent multi-service impact: the SHARED ledger-db has four separate incident precedents affecting more than one consumer service at once -- HIST-102 (2025-09-02) 'Portfolio timeouts during ledger-db failover' (services portfolio-service,ledger-db, '6 min of refused connections'), HIST-110 (2026-01-15) 'Slow orders from ledger-db IOPS throttling' (order-service,ledger-db), HIST-111 (2025-05-22, ledger-db+settlement-service) and HIST-114 (2025-04-18) 'ledger-db disk full from retained WAL' ('write failures'). All four are ledger-db-caused multi-service impacts.
**Contradicting evidence**
  - `F12` ✔ Downstream/DB health indicators stayed flat across the stall, arguing against downstream call failure or connection-pool exhaustion: db_pool_in_use held ~5.8-6.5 connections throughout (06:30-09:57), and error_rate (HTTP 5xx) stayed ~0.17-0.26% with no step change.
  - `F11` ✔ All 409 WARN+ log entries in 07:30-09:59 are level ERROR; there are no rebalance-loop, session-timeout, or poll-timeout messages, and no DB/ledger/posting/5xx downstream-failure messages. The only patterns present are the two above.
  - `F10` ✔ The first bad log entry is at 07:42:14Z: a deserialization failure on a single message. Dominant signature = poison message on partition orders-3 stuck at offset 8841207: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences, first_seen 07:42:14) and 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first_seen 07:44:51).
**Assumptions**
  - ledger-db degradation can stall the consumer at the message-deserialization stage (it cannot — deserialization is pre-DB)
**Rejected because:** Mechanism mismatch: the failing action is message DESERIALIZATION, which occurs before any DB call can fail, so a DB fault cannot produce 'missing required field qty (schema v1 expected, header says v2)'. Metrics also contradict it: db_pool_in_use flat (~5.8-6.5) and HTTP 5xx error_rate flat (~0.17-0.26%) with no step change (F12), and no DB/ledger/posting/5xx error logs exist (F11). Only weak precedent (F18) supports it.

## Facts (verified observations)
- Exactly one deployment occurred across all services in 07:30-09:59: order-service v2.16.0 at 2026-05-12T07:40:00Z ('Order events v2'), which renames event field qty -> quantity, adds field venue, and adds event schema version header v2; status live, not rolled back. [F1]
- Exactly one config change occurred in the window: settlement-service cfg-3004 at 07:35:00Z changing consumer.max.poll.records from 500 to 200. [F2]
- The first bad log entry is at 07:42:14Z: deserialization failure 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences, first_seen 07:42:14) plus 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first_seen 07:44:51). [F10]
- order-service's producer-side change content matches the consumer's poison-message signature exactly: dep-6010 lists 'OrderExecuted event: rename field qty -> quantity', 'add field venue', 'event schema version header v2' — the same missing field name and the same header version the consumer reports. [F32, F10]
- Settlement throughput collapsed from ~62-67 req/s to ~0.4 req/s between the 07:41 and 07:42 buckets and stayed at ~0.4 req/s through 09:55 (onset 07:42:00Z). [F8]
- Consumer lag was flat/baseline (~36-56 messages) through 07:35, rose sharply from the 07:40-07:45 bucket (07:40 avg 515.03, 07:45 avg 2629.17), and grew monotonically to avg 60220.76 / max 61605.53 at 09:55. [F9, F30]
- The first error log (07:42:14Z) coincides with the first zero-throughput minute (07:42 bucket avg 0.39) and the lag rise in the 07:37-07:44 region, so all three signals onset together at ~07:42. [F8, F30]
- All 409 WARN+ entries in 07:30-09:59 are level ERROR and consist solely of the two orders-3 patterns; there are no rebalance-loop, session-timeout, poll-timeout, or DB/ledger/posting/5xx downstream-failure messages. [F11]
- Every WARN+ log entry names ONLY partition orders-3 at offset 8841207; targeted searches for 'orders-0' and 'orders-1' returned 0 matches, and no other topic name appears in settlement-service logs. All 213 deserialization errors are at orders-3@8841207. [F27, F28, F29]
- kafka is a shared broker with TWO consumers (settlement-service and notification-service), so a broker-wide disruption would affect both. [F3]
- notification-service consumer lag is flat/baseline throughout 06:30-09:57 (mean ~60.11, no rise at 07:42 or later) and its throughput is normal and flat (min 132.42, max 166.73 req/s); its only WARN+ entry is an unrelated NTP clock-drift message at 08:39:18Z. [F20, F21, F22]
- Broker metric under_replicated_partitions is exactly 0.0 for every 9-min bucket from 06:30 to 09:57 (no missing points). [F23]
- Downstream/DB health indicators stayed flat across the stall: db_pool_in_use held ~5.8-6.5 connections (06:30-09:57) and HTTP 5xx error_rate stayed ~0.17-0.26% with no step change, arguing against a downstream call failure, connection-pool exhaustion, or ledger-db fault. [F12]
- No rollback of order-service v2.16.0 occurred (dep-6010 is live, rolled_back_at null) and no settlement-service deployment/restart event exists in 07:30-09:59, so no event could have reset the blocked offset. [F33, F35]
- order-service has a produce edge to the shared kafka broker, making it a legitimate writer to the topic settlement-service consumes. [F36, F5]
- No historical incident in the searchable corpus matches today's poison-message/deserialization or producer-schema-rename signature, nor any case of a max.poll.records-type setting causing a stall. [F16, F17]

## Assumptions (not verified)
- The v2 message at orders-3@8841207 was produced by order-service specifically. No per-partition or per-producer telemetry exists to link that offset to a producer (F26, F24); the linkage rests on deploy-content match (F32), the error text (F10), the produce edge from order-service to the shared broker (F36), and timing (F13).
- settlement-service's consumer was not updated in lockstep with the v2 schema; the only deploy in the window is order-service v2.16.0 and there is no settlement-service deployment (F1, F35).
- The cfg-3004 poll-settings change is coincidental/ineffective as a stall cause; there is no poll/rebalance/timeout log evidence and the consumer is actively polling and retrying the bad offset (F2, F10, F11).
- The poison message blocks partition orders-3 only; the retrieved logs are truncated, so errors on other partitions of the orders topic may exist but are not visible (F27, F28, F31).
- The retrieved log sample, though truncated, is assumed representative enough that no additional distinct error pattern (DB/posting/5xx) exists after 07:59; the pattern aggregate lists only the two orders-3 consumer-side patterns (F11, F31).
- Historical precedent (HIST-112/HIST-104) is assumed non-diagnostic here: those broker/rebalance records carry no poison-message, partition, or offset telemetry and no producer-schema precedent exists in the corpus (F14, F15, F16, F19).

## Timeline
- `2026-05-12T07:35:00Z` settlement-service config change cfg-3004: consumer.max.poll.records 500->200 by m.das (only config change in the window). [F2]
- `2026-05-12T07:37:00Z` Aggregate consumer_lag begins rising from a flat ~47 avg (07:30 bucket) to 202.21 (07:37) and 2616.82 (07:44). [F30]
- `2026-05-12T07:40:00Z` order-service v2.16.0 deployed ('Order events v2'): renames OrderExecuted field qty->quantity, adds venue, adds v2 schema header. Only deployment in the window; remains live. [F1, F32, F33]
- `2026-05-12T07:41:00Z` Last normal settlement throughput bucket (avg ~65.3 req/s). [F8]
- `2026-05-12T07:42:00Z` Settlement throughput collapses to ~0.4 req/s (07:42 bucket) and stays there through 09:55. [F8]
- `2026-05-12T07:42:14Z` First deserialization error: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' — onset of the poison-message loop. [F10, F13]
- `2026-05-12T07:44:51Z` First 'poison message: retrying orders-3@8841207 (attempt n), partition blocked' log — the retry storm begins. [F10, F29]
- `2026-05-12T08:22:00Z` Incident INC-010 reported by on-call SRE: no settlements posted ~40 minutes, consumer lag 60k and climbing. [F9]
- `2026-05-12T08:39:18Z` Only notification-service WARN+ entry in the window: unrelated NTP clock-drift resync message. [F22]
- `2026-05-12T09:57:00Z` Lag plateaus at avg 60713.43 / max 61605.53; under_replicated_partitions still 0.0; db_pool_in_use and 5xx error_rate still flat. [F30, F23, F12]
- `2026-05-12T09:58:00Z` Last deserialization error logged; partition orders-3 still blocked with no rollback or consumer restart applied. [F28, F35, F37]

## Recommended actions
- **[immediate]** Roll back order-service to the pre-v2.16.0 producer contract (or set order-service to emit schema v1 / stop emitting the v2 header) so that new OrderExecuted events on orders-3 are again v1-compatible. This is the direct mitigation of the identified cause (the only deploy in-window, F1/F32/F33). -- The poison message is the v2-schema OrderExecuted event that settlement-service cannot deserialize (F10/F32); restoring v1 production stops new poison messages from being produced at the head of the partition. [F1, F32, F33, F10]
- **[immediate]** Unblock partition orders-3 for settlement-service: skip/quarantine the single poison record at orders-3@8841207 (e.g. via an offset-skip/seek-past-bad-record admin action or a dead-letter/poison-message handler), then verify offsets advance and lag declines. -- The consumer is stuck retrying the same offset indefinitely (213 deserialize errors, 196 blocked-retry entries, all at orders-3@8841207 — F10/F28/F29/F27); only clearing that record restores forward progress. No offset-skip/rollback mechanism is currently recorded (F34) and no restart has been applied (F35). [F10, F28, F29, F27, F34, F35]
- **[immediate]** Do NOT treat cfg-3004 (consumer.max.poll.records 500->200) as the cause and do not roll it back as the primary fix; keep it or revert at leisure. The consumer is demonstrably polling and actively retrying the bad offset, which a smaller batch size cannot produce. -- cfg-3004 is coincidental: the failure is a content-specific deserialization error naming the v2 header and missing qty field, a mechanism poll settings cannot produce (F10, F11, F17). [F2, F10, F11, F17]
- **[short_term]** Confirm recovery after offset skip / rollback: verify settlement throughput returns toward ~62-67 req/s and consumer_lag declines monotonically from ~60k, and confirm the settlement-service ERROR patterns (orders-3@8841207) cease. -- Recovery is defined by the same signals that showed the fault: throughput collapse onset at 07:42 (F8), lag rise to ~60k (F9/F30), and the two orders-3 error patterns (F10/F31). [F8, F9, F30, F10, F31]
- **[short_term]** Add a poison-message / deserialization-failure dead-letter path with an alert that fires when a partition is blocked retrying a single offset, so a single unreadable record cannot silently halt an entire partition again. -- All 409 ERROR entries are retries of one offset on one partition (F10/F27/F28/F29); the existing tooling exposes no offset-skip or block-detection mechanism (F34), and the block persisted unnoticed from 07:42 to the 08:22 report. [F10, F27, F28, F29, F34]
- **[long_term]** Introduce and enforce consumer-driven schema compatibility: versioned event contracts with backward compatibility, a schema registry / consumer contract test in CI, and required lockstep consumer updates when a producer bumps the event schema version. -- This is the first recorded schema-rename poisoning in the corpus (F16) and there is no precedent tying producer schema version bumps to consumer deserialization failures (F32 showed the exact schema change); a registry/contract gate prevents recurrence. [F16, F32, F10, F1]
- **[long_term]** Add per-partition lag/offset-progress and producer-side schema-version telemetry (plus the missing broker rebalance/reassignment/error metrics) so the causal hop — which producer emitted the blocking offset — can be proven directly next time. -- Metrics expose only aggregate consumer_lag; partition_lag, rebalance_count, partition_reassignment_count, broker_error_rate and offline_partitions all returned not_found (F26, F24), which is why the offset-to-producer link remains an assumption and could not be confirmed this time. [F26, F24]
- **[long_term]** Do not pursue broker-side partition reassignment remediation (e.g. restarting the consumer group as in HIST-112): the shared broker's other consumer is unaffected and under-replication is zero, so a reassignment theory is contradicted by evidence. -- notification-service lag is flat (~60) and throughput normal (132-167 req/s) on the same shared broker, its only WARN+ entry is unrelated NTP, and under_replicated_partitions is 0.0 for every bucket (F20/F21/F22/F23/F25). [F20, F21, F22, F23, F25]

## Data gaps
- No per-producer or per-partition telemetry exists to prove offset 8841207 was emitted by order-service: partition_lag returned not_found for settlement-service (only aggregate consumer_lag is exposed, F26), the kafka broker lacks rebalance/reassignment/error/offline metrics (F24), and order-service producer-metric queries were not completed (budget exhausted). The central causal hop therefore rests on deploy-content plus timing plus error-text match.
- No consumer-group offset/commit view is exposed by any tool used, so it cannot be independently confirmed that the orders-3@8841207 record was produced after 07:40 rather than being an older record whose first parse attempt happened at 07:42.
- Broker CPU and messages_in_per_s for the window were not queried (budget exhausted), leaving one remaining broker-health signal unchecked, though under_replicated_partitions=0.0 and both consumers' behavior indicate no broker-side disturbance.
- Both log queries were truncated (409 and 52 total matches, 40 returned), so the complete absence of later (07:59-09:59) downstream/DB error signatures in the raw sample cannot be fully asserted, though the top_patterns aggregate lists only the two consumer-side patterns.
- The producer-side schema version history for order-service and whether any other service emits to the orders topic could not be enumerated, so the assumption that orders-3's poison record is order-service v2 output is not directly verified at the source.
- No historical precedent exists for a poison-message/deserialization stall or for a max.poll.records-type setting causing a stall, so neither today's signature nor the poll-config theory can be corroborated or refuted from precedent.

## What would change this conclusion
- Per-partition offset/commit telemetry or producer-side metrics showing that orders-3@8841207 was produced by a service other than order-service (e.g. payment-service), or that the blocking record predates the 07:40 deploy — this would move the conclusion toward H5 (non-order-service poison message).
- Evidence that notification-service (the other consumer on the shared broker) also shows lag rise, throughput drop, rebalance, or the same deserialization/blocked symptom — this would revive H3 (broker-side event) and undercut the confinement argument.
- Discovery of rebalance/session-timeout/poll-timeout log entries or non-zero under_replicated_partitions / partition-reassignment activity in the window — this would support H3 against H1.
- Proof that the v2 poison record is readable by another consumer (or that offsets on orders-3 advanced while orders-0/orders-1 lag rose), which would indicate a settlement-service-specific consumer defect rather than a producer schema break.
- Evidence that cfg-3004 did in fact cause fetch to stop (e.g. poll-timeout logs, a rebalance loop, or offset advance resuming on config revert before any schema fix) — this would point back to H2.
- A ledger-db fault or connection-pool exhaustion signature timed to the onset, or a DB/post/5xx error in the truncated log region, which would revive H4 despite the currently flat DB counters.
- A completed natural experiment: if rolling back order-service v2.16.0 (or skipping the poison offset) restores throughput and lag declines, H1 is confirmed; if not, H1 is falsified.

## Investigation stats
- latency 163.34s, LLM calls 26, tokens 210012 (in 179044 / out 30968)
- tool calls 46 (5 failed, 0 partial, 0 recovered by retry), findings 37/37 verified
