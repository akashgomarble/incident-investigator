# Incident investigation report: INC-010
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
Settlement processing stalled because the order-service v2.16.0 deploy (dep-6010, 07:40Z) changed the OrderExecuted event schema (qty->quantity, v2 header) on the shared Kafka topic, while settlement-service still expects schema v1; the poison message at orders-3@8841207 fails deserialization, is retried indefinitely with "partition blocked", so partition 3 never advances and lag grows monotonically. Timeline evidence is tight: throughput collapsed inside the 07:40Z bucket (F1, F2) — the same bucket as the deploy — and the first deserialization error appears at 07:42:14Z pinned to that single offset (F4, F5, F9). The competing config-change hypothesis (cfg-3004, max.poll.records 500->200) is weaker: it predates the first error and the mechanism (a smaller batch size causing rebalances) is counter-intuitive, and broker/dependency evidence is clean (under_replicated_partitions 0, DB pool flat, HTTP error rate flat). Confidence is medium (0.60 per the reviewed ranking), capped by an unverified producer side and one single-partition sample.

## Probable root cause
**order-service v2.16.0 schema change broke settlement-service deserialization on partition 3** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 schema change broke settlement-service deserialization on partition 3 | bad_deployment | order-service | dep-6010 | 0.60 (medium) | active |
| 2 | H2: cfg-3004 consumer.max.poll.records=200 reduction causes group instability / rebalances that stall processing | config_change | settlement-service | cfg-3004 | 0.12 (low) | active |
| 3 | H3: Kafka broker-side partition reassignment / infrastructure failure stalls the settlement consumer group | infrastructure_failure | kafka | null | 0.06 (low) | active |
| 4 | H4: Downstream dependency (ledger-db) degradation blocks consumer mid-processing, lag grows independently of changes | dependency_failure | ledger-db | null | 0.05 (low) | active |

### H1: order-service v2.16.0 schema change broke settlement-service deserialization on partition 3
dep-6010 on order-service v2.16.0 (07:40Z) renamed qty->quantity and bumped the event schema header to v2 on OrderExecuted events published to the shared kafka topic. settlement-service still expects schema v1 and fails to deserialize ('missing required field qty (schema v1 expected, header says v2)'). The first failing offset (orders-3@8841207) blocks partition 3 permanently ('partition blocked'), so the consumer stops committing and processing that partition entirely — zero settlements, lag grows monotonically. Other partitions/consumers are unaffected in the sample, matching a single-poison-message stall rather than a systemic throughput reduction.

**Causal chain**
1. order-service deploys v2.16.0 at 07:40:00Z, renaming qty->quantity and adding a v2 schema version header to OrderExecuted events [F14, F18]
2. Settlement-service throughput collapses and lag begins rising in the 07:40 bucket, immediately after the deploy [F1, F2, F7]
3. Consumer logs deserialization failure 'missing required field qty (schema v1 expected, header says v2)' and then 'poison message ... partition blocked' on the same offset [F4, F8, F9]
4. The poisoned offset orders-3@8841207 never advances, permanently blocking partition 3 and preventing any settlement from that partition [F5, F8, F9]
5. Because partition 3 is never processed but the producer keeps publishing, consumer lag grows monotonically to ~60k [F2, F7]
6. No downstream dependency failure explains the stall: DB pool flat, HTTP error rate flat, broker under-replicated partitions 0 [F6, F10, F11, F12]

**Supporting evidence**
  - `F14` ✔ The only deployment in the window (and across 06:00-09:59Z) was dep-6010 on order-service v2.16.0 at 2026-05-12T07:40:00Z, changing the OrderExecuted event schema: rename qty -> quantity, add venue, and a v2 event schema version header. Status 'live', not rolled back.
  - `F18` ✔ The two concurrent changes are ~5 minutes apart (cfg-3004 at 07:35:00Z, dep-6010 at 07:40:00Z), both shortly before the ~08:22Z report of a ~40-minute settlement gap. The order-service schema change (qty->quantity rename, new v2 header) alters the event format consumed by settlement-service on kafka, co-occurring with the settlement-service poll-batch reduction.
  - `F4` ✔ The earliest error is a deserialization failure, not a rebalance/commit/pool error: at 07:42:14Z "failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)", repeated 213 times (first_seen 07:42:14Z). A second signature, "poison message: retrying orders-3@8841207 (attempt <n>), partition blocked", appears from 07:44:51Z with 196 occurrences.
  - `F5` ✔ All error signatures are pinned to a single partition: every one of the 40 earliest error lines references offset 'orders-3@8841207' (partition 3). No other partition id appears in the sample, and the log explicitly says 'partition blocked'.
  - `F8` ✔ settlement-service logged only ERROR-level entries (409 matched, all ERROR) starting 07:42:14Z, in two patterns: deserialize failure 'missing required field qty (schema v1 expected, header says v2)' on orders-3@8841207 (213x) and 'poison message: retrying ... partition blocked' on the same offset (196x), still occurring at 09:58:31Z.
  - `F9` ✔ Deserialization/poison errors first appeared at 07:42:14Z and 07:44:51Z, i.e. BEFORE lag reached 1572 (07:40 bucket) and well before the reported ~08:22Z onset; the first failed offset is pinned to orders-3@8841207 and never advances.
  - `F1` ✔ Settlement throughput collapsed to zero inside the 07:40:00Z bucket: request_rate fell from a stable ~60-63 req/s (07:00-07:35) to avg 25.87 at 07:40 and avg 0.41 at 07:45, then stayed flat at ~0.40 req/s through 09:55 — i.e. zero real throughput persists for the whole window, ~42 min before the 08:22 report.
  - `F2` ✔ Consumer lag was flat/baseline (~39-50 msgs) through 07:35, then jumped in the same 07:40 bucket to avg 515.03 / max 1306.47, and climbed monotonically every 5-min bucket thereafter to avg 60220.76 at 09:55 (summary max 61605.53). Lag growth therefore begins at the 07:40 bucket, not at 08:22.
  - `F7` ✔ settlement-service consumer lag was flat/near-zero (~48 msgs) at 07:30Z, then rose monotonically every 10-min bucket: 1572 at 07:40, 5930 at 07:50, 10371 at 08:00, climbing to 59267 (max 61605) at 09:50Z. No recovery in the window.
  - `F6` ✔ Settlement-service shows no DB/connection-pool pressure and no HTTP 5xx spike: db_pool_in_use flat ~5.8-6.2 connections across 07:00-09:55, and error_rate flat ~0.17-0.27% throughout. A 'connection pool stats: healthy' INFO line appears at 07:53:07Z. So the failure is not pool exhaustion or DB timeouts.
  - `F10` ✔ settlement-service DB connection pool is NOT saturated: db_pool_in_use stays flat at ~5.2-6.8 connections across 07:30-09:59Z (first 6.0, last 5.91, max 6.78), with no rise at lag onset.
  - `F11` ✔ settlement-service HTTP error_rate stays flat ~0.18-0.26% throughout (min 0.1, max 0.32) with no spike at lag onset; consumer-side failures are not reflected as HTTP 5xx.
  - `F12` ✔ Kafka broker health looks clean: under_replicated_partitions is 0.0 for every 10-min bucket from 07:30 to 09:50Z (min/max/mean all 0.0). Kafka's only exposed metrics are cpu_pct, messages_in_per_s, under_replicated_partitions.
**Contradicting evidence**
  - `F3` ✔ A config change was applied at 07:35:37Z (cfg-3004, consumer.max.poll.records=200) — ~4 min before the first error and the lag jump. It is the only config-change log line in the 07:30-08:30 window.
  - `F13` ✔ The only config change across all services in the incident window was cfg-3004 on settlement-service: consumer.max.poll.records changed from 500 to 200 at 2026-05-12T07:35:00Z by m.das ("Smaller poll batches to smooth DB writes"). A wider 06:00-09:59Z query returned the same single change.
**Assumptions**
  - That the malformed v2 message actually originated from order-service's deploy rather than an out-of-band producer (gap: producer side unverified; no producer publish-rate metric retrieved)
  - That 'partition blocked' halts all settlement processing rather than just that one partition's records — the report says zero settlements, implying either a single-partition topic or a global commit halt
  - That settlement-service did not already tolerate v2 schema (no evidence of a consumer-side schema upgrade)

### H2: cfg-3004 consumer.max.poll.records=200 reduction causes group instability / rebalances that stall processing
The only settlement-service config change in the window (cfg-3004, 07:35:37Z, max.poll.records 500->200) altered poll behaviour such that consumer heartbeats/processing exceed max.poll.interval.ms or the group churns through rebalances, so partitions are repeatedly revoked and never processed; lag grows because partitions never complete a processing cycle.

**Causal chain**
1. cfg-3004 applied at 07:35:37Z reducing max.poll.records from 500 to 200 [F3, F13]
2. Reduced poll batch size / altered poll cadence triggers rebalance or heartbeat timeout in the consumer group *(assumed)*
3. Partitions are repeatedly revoked, so no partition is ever processed to completion and throughput falls to zero [F1]
4. Lag accumulates monotonically because the producer keeps publishing while the consumer makes no progress [F2, F7]

**Supporting evidence**
  - `F3` ✔ A config change was applied at 07:35:37Z (cfg-3004, consumer.max.poll.records=200) — ~4 min before the first error and the lag jump. It is the only config-change log line in the 07:30-08:30 window.
  - `F13` ✔ The only config change across all services in the incident window was cfg-3004 on settlement-service: consumer.max.poll.records changed from 500 to 200 at 2026-05-12T07:35:00Z by m.das ("Smaller poll batches to smooth DB writes"). A wider 06:00-09:59Z query returned the same single change.
  - `F1` ✔ Settlement throughput collapsed to zero inside the 07:40:00Z bucket: request_rate fell from a stable ~60-63 req/s (07:00-07:35) to avg 25.87 at 07:40 and avg 0.41 at 07:45, then stayed flat at ~0.40 req/s through 09:55 — i.e. zero real throughput persists for the whole window, ~42 min before the 08:22 report.
  - `F2` ✔ Consumer lag was flat/baseline (~39-50 msgs) through 07:35, then jumped in the same 07:40 bucket to avg 515.03 / max 1306.47, and climbed monotonically every 5-min bucket thereafter to avg 60220.76 at 09:55 (summary max 61605.53). Lag growth therefore begins at the 07:40 bucket, not at 08:22.
  - `F7` ✔ settlement-service consumer lag was flat/near-zero (~48 msgs) at 07:30Z, then rose monotonically every 10-min bucket: 1572 at 07:40, 5930 at 07:50, 10371 at 08:00, climbing to 59267 (max 61605) at 09:50Z. No recovery in the window.
**Contradicting evidence**
  - `F4` ✔ The earliest error is a deserialization failure, not a rebalance/commit/pool error: at 07:42:14Z "failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)", repeated 213 times (first_seen 07:42:14Z). A second signature, "poison message: retrying orders-3@8841207 (attempt <n>), partition blocked", appears from 07:44:51Z with 196 occurrences.
  - `F5` ✔ All error signatures are pinned to a single partition: every one of the 40 earliest error lines references offset 'orders-3@8841207' (partition 3). No other partition id appears in the sample, and the log explicitly says 'partition blocked'.
  - `F8` ✔ settlement-service logged only ERROR-level entries (409 matched, all ERROR) starting 07:42:14Z, in two patterns: deserialize failure 'missing required field qty (schema v1 expected, header says v2)' on orders-3@8841207 (213x) and 'poison message: retrying ... partition blocked' on the same offset (196x), still occurring at 09:58:31Z.
  - `F9` ✔ Deserialization/poison errors first appeared at 07:42:14Z and 07:44:51Z, i.e. BEFORE lag reached 1572 (07:40 bucket) and well before the reported ~08:22Z onset; the first failed offset is pinned to orders-3@8841207 and never advances.
**Assumptions**
  - A smaller max.poll.records would actually trigger rebalances — mechanism is counter-intuitive (smaller batches ease max.poll.interval pressure, they don't worsen it)
  - That the observed error signatures are coincidental rather than causal

### H3: Kafka broker-side partition reassignment / infrastructure failure stalls the settlement consumer group
A broker-side event (partition reassignment, ISR shrink, or broker overload) around 07:40-07:42Z caused the settlement consumer group to stall on partition 3, producing growing lag and zero settlements, independent of the two code/config changes. This mirrors historical incidents HIST-112 and HIST-104 with infrastructure_failure root causes.

**Causal chain**
1. A broker event (reassignment/rebalance) occurs around 07:40Z, coinciding with lag onset *(assumed)*
2. The settlement consumer group loses/stalls partition 3, halting settlement processing [F5, F1]
3. Lag grows monotonically as the producer continues publishing [F2, F7]

**Supporting evidence**
  - `F17` ✘ unverified Two historical incidents with matching 'settlements stuck / consumer lag growing' symptoms (HIST-112, HIST-104) had infrastructure_failure root causes from Kafka partition reassignment, not from config or schema changes. HIST-111 shows a past ledger-db slowdown affecting settlement-service. Stated by the tool as leads, not evidence about the current incident.
**Contradicting evidence**
  - `F12` ✔ Kafka broker health looks clean: under_replicated_partitions is 0.0 for every 10-min bucket from 07:30 to 09:50Z (min/max/mean all 0.0). Kafka's only exposed metrics are cpu_pct, messages_in_per_s, under_replicated_partitions.
  - `F4` ✔ The earliest error is a deserialization failure, not a rebalance/commit/pool error: at 07:42:14Z "failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)", repeated 213 times (first_seen 07:42:14Z). A second signature, "poison message: retrying orders-3@8841207 (attempt <n>), partition blocked", appears from 07:44:51Z with 196 occurrences.
  - `F5` ✔ All error signatures are pinned to a single partition: every one of the 40 earliest error lines references offset 'orders-3@8841207' (partition 3). No other partition id appears in the sample, and the log explicitly says 'partition blocked'.
  - `F8` ✔ settlement-service logged only ERROR-level entries (409 matched, all ERROR) starting 07:42:14Z, in two patterns: deserialize failure 'missing required field qty (schema v1 expected, header says v2)' on orders-3@8841207 (213x) and 'poison message: retrying ... partition blocked' on the same offset (196x), still occurring at 09:58:31Z.
  - `F9` ✔ Deserialization/poison errors first appeared at 07:42:14Z and 07:44:51Z, i.e. BEFORE lag reached 1572 (07:40 bucket) and well before the reported ~08:22Z onset; the first failed offset is pinned to orders-3@8841207 and never advances.
**Assumptions**
  - A broker-side rebalance happened but left no trace in under_replicated_partitions or cpu metrics
  - Only rests on UNVERIFIED historical incident leads (F17), which are explicitly not evidence about this incident

### H4: Downstream dependency (ledger-db) degradation blocks consumer mid-processing, lag grows independently of changes
A downstream dependency settlement-service writes to (ledger-db) is degraded, so the consumer blocks mid-processing on the failing offset and partitions stop advancing; lag grows because processing cannot complete. This is the 'stuck consumer independent of config' theory, aligned with HIST-111 (past ledger-db slowdown affecting settlement-service).

**Causal chain**
1. ledger-db (shared, platform-owned) becomes slow or unresponsive around 07:40Z *(assumed)*
2. Settlement consumer blocks on the DB write and cannot commit offsets *(assumed)*
3. Lag grows monotonically and settlements are not posted [F2, F7]

**Supporting evidence**
  - `F17` ✘ unverified Two historical incidents with matching 'settlements stuck / consumer lag growing' symptoms (HIST-112, HIST-104) had infrastructure_failure root causes from Kafka partition reassignment, not from config or schema changes. HIST-111 shows a past ledger-db slowdown affecting settlement-service. Stated by the tool as leads, not evidence about the current incident.
**Contradicting evidence**
  - `F6` ✔ Settlement-service shows no DB/connection-pool pressure and no HTTP 5xx spike: db_pool_in_use flat ~5.8-6.2 connections across 07:00-09:55, and error_rate flat ~0.17-0.27% throughout. A 'connection pool stats: healthy' INFO line appears at 07:53:07Z. So the failure is not pool exhaustion or DB timeouts.
  - `F10` ✔ settlement-service DB connection pool is NOT saturated: db_pool_in_use stays flat at ~5.2-6.8 connections across 07:30-09:59Z (first 6.0, last 5.91, max 6.78), with no rise at lag onset.
  - `F11` ✔ settlement-service HTTP error_rate stays flat ~0.18-0.26% throughout (min 0.1, max 0.32) with no spike at lag onset; consumer-side failures are not reflected as HTTP 5xx.
**Assumptions**
  - ledger-db slowness exists despite no evidence; DB pool_in_use flat and connection-pool 'healthy' line argue against it
  - Error messages would be DB/timeout errors, but observed errors are deserialization/poison-message — mechanism mismatch

## Facts (verified observations)
- Settlement throughput collapsed to zero inside the 07:40:00Z bucket: request_rate fell from ~60-63 req/s (07:00-07:35) to avg 25.87 at 07:40 and avg 0.41 at 07:45, staying at ~0.40 req/s through 09:55. [F1]
- Consumer lag was baseline (~39-50 msgs) through 07:35, jumped in the 07:40 bucket to avg 515.03 / max 1306.47, and climbed monotonically to avg 60220.76 by 09:55 (summary max 61605.53). [F2, F7]
- The only deployment across 06:00-09:59Z was dep-6010 on order-service v2.16.0 at 07:40:00Z, changing the OrderExecuted event schema: qty->quantity rename, added venue, and a v2 event schema version header; status 'live', not rolled back. [F14]
- The earliest error is a deserialization failure: at 07:42:14Z 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field qty (schema v1 expected, header says v2)', repeated 213 times; a second signature 'poison message: retrying orders-3@8841207 (attempt n), partition blocked' appears from 07:44:51Z with 196 occurrences and is still occurring at 09:58:31Z. [F4, F8]
- All error signatures in the sample are pinned to a single offset on partition 3 (orders-3@8841207); no other partition id appears and the log explicitly states 'partition blocked'. [F5]
- The first failing offset never advances, and the errors began at 07:42:14Z / 07:44:51Z, i.e. before the reported ~08:22Z onset. [F9]
- The only config change in the incident window (and across 06:00-09:59Z) was cfg-3004 on settlement-service: consumer.max.poll.records changed 500->200 at 07:35:00Z by m.das. [F3, F13]
- Kafka broker health is clean: under_replicated_partitions is 0.0 for every 10-min bucket from 07:30 to 09:50Z. [F12]
- settlement-service shows no DB/connection-pool pressure and no HTTP 5xx spike: db_pool_in_use flat ~5.2-6.8 connections across 07:30-09:59Z, HTTP error_rate flat ~0.18-0.26%, and a 'connection pool stats: healthy' INFO line at 07:53:07Z. [F6, F10, F11]
- order-service is the upstream producer into the shared Kafka topic that settlement-service consumes, and both Kafka and ledger-db are shared platform-owned components. [F15, F16]

## Assumptions (not verified)
- That the malformed v2 message actually originated from order-service's dep-6010 deploy rather than an out-of-band producer — the producer side was not verified and no producer publish-rate metric was retrieved.
- That 'partition blocked' halts all settlement processing rather than only partition 3's records — the report says zero settlements, implying either a single-partition topic or a global commit halt; the exact mechanism was not confirmed.
- That settlement-service did not already tolerate the v2 schema (no evidence of a consumer-side schema upgrade was checked).
- That the config change cfg-3004 (max.poll.records 500->200) is coincidental rather than causal, based on timing (4 minutes before the first error) and on the counter-intuitive assumed mechanism.
- That the error-log sample (40 earliest lines / 409 ERROR entries) is representative of the ongoing failure across the whole stall window.
- That the deployment/config inventory (06:00-09:59Z) was complete enough to exclude other tracked changes.

## Timeline
- `2026-05-12T07:30:00Z` settlement-service consumer lag flat/near-zero (~48 msgs); DB pool flat ~5-6 connections; pre-change steady state. [F7, F10]
- `2026-05-12T07:35:00Z` cfg-3004 applied to settlement-service: consumer.max.poll.records changed 500->200 by m.das; only config change in the window. [F3, F13]
- `2026-05-12T07:40:00Z` dep-6010 goes live on order-service v2.16.0: OrderExecuted schema change (qty->quantity, add venue, v2 header) on the shared Kafka topic. [F14, F18]
- `2026-05-12T07:40:00Z` Settlement throughput collapses inside this bucket (request_rate 60-63 req/s -> 25.87 -> 0.41) and consumer lag jumps from baseline to avg 515.03 / max 1306.47. [F1, F2]
- `2026-05-12T07:42:14Z` First deserialization error: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field qty (schema v1 expected, header says v2)', eventually 213 occurrences. [F4, F5, F8]
- `2026-05-12T07:44:51Z` 'poison message: retrying orders-3@8841207 (attempt n), partition blocked' begins (196 occurrences), confirming the offset never advances. [F4, F5, F9]
- `2026-05-12T07:53:07Z` settlement-service logs 'connection pool stats: healthy'; DB pool remains flat, HTTP error_rate flat — rules out pool exhaustion / DB-timeout mechanism. [F6, F10, F11]
- `2026-05-12T08:22:00Z` Incident INC-010 reported by on-call SRE: no settlements for ~40 minutes, consumer lag growing, poll settings changed shortly before. [F1, F2]
- `2026-05-12T09:55:00Z` Consumer lag at avg 60220.76 (max 61605.53); throughput still ~0.40 req/s; no recovery in window. Kafka under_replicated_partitions 0.0 throughout 07:30-09:50Z. [F2, F7, F12]
- `2026-05-12T09:58:31Z` Same deserialization/poison errors on orders-3@8841207 still being logged — the stall is ongoing and unmitigated. [F8]

## Recommended actions
- **[immediate]** Immediately roll back or revert dep-6010 on order-service (v2.16.0 -> prior version) so the producer emits schema v1 OrderExecuted events again, then confirm the settlement consumer resumes past orders-3@8841207 and lag begins to fall. -- The first failing offset (orders-3@8841207) matches the v2 schema change in the same 07:40Z bucket as the throughput collapse; reverting the producer removes the poison message at source without touching consumer state. [F14, F4, F5, F1, F2, F9]
- **[immediate]** If a full order-service rollback is not possible quickly, make settlement-service backward-compatible for the v2 schema (accept 'quantity' as alias for 'qty' and accept the v2 header) as a stop-gap, then skip/dead-letter the single poison offset orders-3@8841207 so partition 3 can advance. -- The stall is a single blocked offset; allowing the consumer to tolerate v2 or to skip that one record unblocks partition 3 and stops the growing lag immediately. [F4, F5, F9, F8]
- **[immediate]** Do NOT revert cfg-3004 (consumer.max.poll.records 500->200) as the primary mitigation, and avoid re-applying further poll-setting changes as a first response. -- cfg-3004 predates the first error by ~4 minutes and the error signatures are deserialization/poison-message, not rebalance/commit errors; changing it back would add churn without addressing the blocking offset. [F3, F13, F4, F5]
- **[short_term]** Replay/reprocess the order-executed records published between 07:40Z and the fix, after confirming the producer emits v1 (or after the consumer tolerates v2), and reconcile settlements for the gap window against ledger-db. -- Throughput was ~0 for the whole window (F1), so a backlog of unprocessed settlements exists and must be posted; ledger-db is a shared downstream that receives both order-service and settlement-service writes. [F1, F7, F15, F16]
- **[short_term]** Verify recovery by confirming: partition 3 advances past offset 8841207, consumer lag returns to baseline (~50 msgs), request_rate returns to ~60 req/s, and the deserialize/poison error signatures stop appearing in settlement-service logs. -- These are the exact signals that inverted at onset, so their inversion confirms the root cause and closure. [F1, F2, F4, F5, F7, F8]
- **[long_term]** Add contract/schema compatibility checks (producer/consumer schema registry or CI contract tests) gating any event-schema change on the shared Kafka topic, so a producer rename like qty->quantity cannot go live while consumers expect v1. -- The failure was a cross-service schema contract break with no gate; the shared-topic dependency was visible in the topology but not enforced. [F14, F16]
- **[long_term]** Add a consumer-side poison-message dead-letter path (bounded retries then DLQ + partition advance) and an alert on lag with a per-partition blocked-offset detector, so a single unprocessable record cannot silently stall settlement for 40+ minutes. -- A single offset blocked partition 3 indefinitely with no automated escape and the incident was only noticed ~40 minutes later at 08:22Z, despite errors starting at 07:42:14Z. [F4, F5, F8, F2]

## Data gaps
- Producer side unverified: no order-service logs (WARN) or producer publish-rate metric was retrieved, so it is not directly confirmed that order-service's dep-6010 emitted the malformed v2 message rather than an out-of-band producer.
- No Kafka broker/partition logs or rebalance metrics were obtained (broker exposes only cpu_pct, messages_in_per_s, under_replicated_partitions), so a broker-side reassignment (H3) cannot be positively excluded from metrics alone.
- Error-log sample is limited (40 earliest lines, 409 ERROR entries) and only shows partition 3; it is not confirmed whether the topic has multiple partitions and whether other partitions/consumers were truly unaffected or merely absent from the sample.
- Downstream ledger/posting health could not be checked: ledger-service and posting-service do not exist in this environment (query_metrics returned not_found), and order-service/ledger log queries were cut off by budget, so blast radius on downstream ledger/posting is unconfirmed.
- The exact mechanism by which 'partition blocked' halts ALL settlement processing (single-partition topic vs global commit halt) was not established, so the path from one blocked offset to zero settlements relies on the reported symptom.
- No consumer-group/offset-commit telemetry was available to show directly that commits stopped at orders-3@8841207.

## What would change this conclusion
- Direct evidence that the v2/quantity messages on orders-3 were published by a producer other than order-service dep-6010 (out-of-band producer), which would break the causal link to the deploy.
- Finding settlement-service already accepts schema v2, or that the failed offset was a one-off malformed record unrelated to the schema rollout.
- Evidence of a Kafka broker partition reassignment, ISR shrink, or broker overload coinciding with the 07:40Z lag onset (e.g. rebalance counts, broker logs), which would support H3 (infrastructure_failure) over H1.
- Evidence that the lag/zero-throughput stall occurs on multiple partitions or is driven by consumer-group rebalances rather than a single blocked offset, which would support H2 (cfg-3004) over H1.
- Evidence of ledger-db latency/timeouts or DB errors on settlement-service mid-processing at 07:40Z, which would support H4 (dependency failure) — currently argued against by the flat DB pool and healthy pool log line.
- Logs showing rebalance/commit/heartbeat errors (rather than deserialization/poison-message errors) at onset, which would reframe the failure as a poll-setting/group-instability problem.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6

## Investigation stats
- latency 87.63s, LLM calls 14, tokens 91377 (in 74171 / out 17206)
- tool calls 24 (4 failed, 0 partial, 0 recovered by retry), findings 17/18 verified
