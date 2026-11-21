# Incident investigation report: INC-010
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
Settlement posting stalled at ~07:42Z because the order-service deploy dep-6010 (v2.16.0, 07:40Z) broke the OrderExecuted payload contract: the event renamed field qty->quantity, added venue, and added an event schema version header v2, while settlement-service's consumer still validates against schema v1. The consumer cannot deserialize offset orders-3@8841207 ('missing required field qty, schema v1 expected, header says v2'), treats it as a poison message, and blocks partition orders-3, so lag grows monotonically to ~60k with no settlements posted. Confidence in H1 is medium (0.60), clearly ahead of the alternatives, and it is supported by an exact verbatim match between dep-6010's declared change set and the first ERROR, by the ~2-minute deploy-to-failure timing, and by the absence of any rebalance/pool evidence that would support the suspected consumer poll-settings change (cfg-3004). Residual uncertainty is that no producer-side or schema-registry telemetry directly proves dep-6010 emitted the v2 message at that offset.

## Probable root cause
**order-service dep-6010 emitted v2 OrderExecuted schema that settlement-service cannot deserialize, blocking a partition** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service dep-6010 emitted v2 OrderExecuted schema that settlement-service cannot deserialize, blocking a partition | bad_deployment | order-service | dep-6010 | 0.60 (medium) | active |
| 2 | H2: settlement-service consumer.max.poll.records change (cfg-3004) caused processing/rebalance failure | config_change | settlement-service | cfg-3004 | 0.05 (low) | rejected |
| 3 | H3: Kafka broker degradation / partition reassignment starves the consumer independent of any service change | infrastructure_failure | kafka | null | 0.04 (low) | rejected |
| 4 | H4: Downstream backpressure (ledger-db) or resource saturation on settlement-service stalls processing | resource_exhaustion | settlement-service | null | 0.03 (low) | rejected |

### H1: order-service dep-6010 emitted v2 OrderExecuted schema that settlement-service cannot deserialize, blocking a partition
The order-service deploy dep-6010 (v2.16.0, 07:40Z) changed the OrderExecuted event schema (renamed qty->quantity, added venue, added schema-version header v2) while settlement-service's consumer still validates against schema v1. The consumer fails to deserialize offset orders-3@8841207 ('missing required field qty'), treats it as a poison message, and blocks partition orders-3, so no further settlements are posted and lag grows monotonically. The fault originates in the order-service producer contract break.

**Causal chain**
1. dep-6010 deployed to order-service at 07:40Z changes OrderExecuted schema: qty->quantity, add venue, header v2 [F3, F17]
2. order-service begins producing v2 OrderExecuted messages onto topic 'orders' (partition orders-3) [F3]
3. settlement-service consumer validates against v1, cannot deserialize the v2 record at offset 8841207 ('missing required field qty, schema v1 expected, header says v2') [F8, F17]
4. the un-deserializable record becomes a poison message retried indefinitely on orders-3, blocking the partition [F9]
5. partition blocked -> consumer commits nothing -> lag grows monotonically (603 at 07:42 to ~60k at 09:55) and zero settlements post [F7, F10, F16]

**Supporting evidence**
  - `F1` ✔ Exactly two changes occurred in 2026-05-12T07:00Z–09:59Z across ALL services: a settlement-service config change of consumer.max.poll.records from 500 to 200 at 07:35:00Z (author m.das), and an order-service deploy dep-6010 v2.16.0 at 07:40:00Z. No other service changed anything.
  - `F2` ✔ The settlement-service consumer poll-settings change (cfg-3004, 07:35:00Z) preceded the order-service deploy (dep-6010, 07:40:00Z) by 5 minutes, and both preceded the 08:22Z incident report. The poll change is the earliest change in the window.
  - `F3` ✔ The order-service deploy changed the OrderExecuted event schema: field renamed qty -> quantity, added field 'venue', added an 'event schema version header v2'. order-service is an upstream producer of the events settlement-service consumes; dep-6010 status is 'live' and rolled_back_at is null.
  - `F7` ✔ Settlement consumer lag was at baseline (~26-55 messages, avg 31.06 at 07:40:00Z) through the 07:40Z bucket, then jumped to avg 603.29 / max 819.95 in the 07:42:00Z bucket — onset between 07:40 and 07:42.
  - `F8` ✔ The first ERROR on settlement-service in the window is a deserialization failure at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. This pattern fired 213 times (first_seen 07:42:14Z).
  - `F9` ✔ A second error pattern follows: 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first_seen 07:44:51Z) — the same offset 8841207 on partition orders-3 is retried indefinitely, which is the mechanism that stalls the partition.
  - `F10` ✔ Settlement lag grew monotonically and did not recover: from 603 (07:42) through 20,525 (08:25:34 avg, max 22062), 60,220.76 avg / max 61,605.53 at 09:55 — approximately 60k and climbing as reported. All 409 matched log lines in 07:00-09:59 were ERROR level.
  - `F15` ✔ Re-query of ALL services 2026-05-12T07:00Z-09:59Z returns exactly one config change and one deployment: cfg-3004 settlement-service consumer.max.poll.records 500->200 at 07:35:00Z (author m.das, 'Smaller poll batches to smooth DB writes') and dep-6010 order-service v2.16.0 at 07:40:00Z. No other service changed anything; both precede the 08:22Z report.
  - `F16` ✔ Temporal alignment: the last normal lag bucket is 07:40Z (avg 31.06) and the first elevated bucket is 07:42Z (avg 603.29); dep-6010 deployed 07:40:00Z sits inside that gap, and the first deserialization ERROR is 07:42:14Z — i.e. the order-service deploy precedes the first failed offset by ~2 minutes, whereas the poll change precedes it by ~7 minutes.
  - `F17` ✔ The deploy's declared change set matches the error signature verbatim: dep-6010 changes are 'OrderExecuted event: rename field qty -> quantity', 'add field venue', 'event schema version header v2', and the first ERROR is 'missing required field qty (schema v1 expected, header says v2)' at offset orders-3@8841207. This is a genuine payload-contract break on the exact field/header named.
  - `F19` ✔ Shared-component check: settlement-service depends on kafka (queue-consume) and ledger-db (db); both are shared:true and owned by 'platform', and neither had any deploy or config change on 2026-05-12 (broker/DB change counts zero). No change to the broker or shared DB competes as an explanation for the stall.
  - `F20` ✔ Historical leads do not match this incident's mechanism: HIST-112 (settlement-service 2026-02-03, 'Consumer lost partition assignment during broker maintenance') and HIST-104 (notification-service, 'Partition reassignment caused consumer group rebalancing storm') both cite kafka partition reassignment/rebalance, but T1 found zero rebalance events; a search for deserialization/schema-mismatch precedents returned 0 incidents. Both HIST hits are match_score-only leads, not evidence about the current incident.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The v2 messages on orders-3 were produced by order-service and not by a different producer (F3 change set matches, but no producer-side telemetry confirms the specific record)
  - settlement-service has no forward-compatible/v2 schema handling
  - The 'zero settlements processed' symptom follows from the blocked partition (no direct settlement counter metric)

### H2: settlement-service consumer.max.poll.records change (cfg-3004) caused processing/rebalance failure
The settlement-service config change cfg-3004 (07:35Z) reducing max.poll.records 500->200 caused the consumer to fail to process or to be rebalanced, so lag grows. This is the reporter's suspicion.

**Causal chain**
1. cfg-3004 changed settlement-service consumer.max.poll.records 500->200 at 07:35Z [F1, F2, F15]
2. the reduced batch size disrupts the consumer group / causes rebalance, stalling processing (assumed mechanism) *(assumed)*
3. consumer stops committing, lag grows to ~60k, zero settlements [F10]

**Supporting evidence**
  - `F1` ✔ Exactly two changes occurred in 2026-05-12T07:00Z–09:59Z across ALL services: a settlement-service config change of consumer.max.poll.records from 500 to 200 at 07:35:00Z (author m.das), and an order-service deploy dep-6010 v2.16.0 at 07:40:00Z. No other service changed anything.
  - `F2` ✔ The settlement-service consumer poll-settings change (cfg-3004, 07:35:00Z) preceded the order-service deploy (dep-6010, 07:40:00Z) by 5 minutes, and both preceded the 08:22Z incident report. The poll change is the earliest change in the window.
  - `F15` ✔ Re-query of ALL services 2026-05-12T07:00Z-09:59Z returns exactly one config change and one deployment: cfg-3004 settlement-service consumer.max.poll.records 500->200 at 07:35:00Z (author m.das, 'Smaller poll batches to smooth DB writes') and dep-6010 order-service v2.16.0 at 07:40:00Z. No other service changed anything; both precede the 08:22Z report.
**Contradicting evidence**
  - `F8` ✔ The first ERROR on settlement-service in the window is a deserialization failure at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. This pattern fired 213 times (first_seen 07:42:14Z).
  - `F9` ✔ A second error pattern follows: 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first_seen 07:44:51Z) — the same offset 8841207 on partition orders-3 is retried indefinitely, which is the mechanism that stalls the partition.
  - `F12` ✔ No rebalance or connection-pool log events were found on settlement-service in 07:00-09:59: both the 'rebalance' and 'pool' searches returned total_matched 0. No consumer-group rebalance evidence in the log store.
  - `F18` ✔ The consumer poll-settings change (cfg-3004, 500->200 max.poll.records) has no corresponding failure signature: T1 found no consumer-group rebalance and no pool events on settlement-service, and db_pool_in_use stayed flat ~5.85-6.17. A batch-size reduction cannot itself produce a schema/deserialization failure, so its 07:35Z position is timing-only, not mechanism.
**Assumptions**
  - A max.poll.records reduction can produce a rebalance or hard consumer failure — contradicted by the absence of any rebalance/pool event (F12)
**Rejected because:** Mechanism mismatch and no supporting signature: the only error is a schema/deserialization failure keyed to dep-6010's declared change (F8/F17), and logs show zero rebalance or pool events on settlement-service (F12). A batch-size reduction cannot itself cause a 'missing required field qty (header says v2)' error (F18). Timing is also looser (7 min vs 2 min gap).

### H3: Kafka broker degradation / partition reassignment starves the consumer independent of any service change
Broker-side trouble (partition reassignment, rebalance storm, or produce-side starvation) stalls settlement-service's consumer independently of settlement-service config. Supported by two historical incidents citing kafka partition reassignment/rebalance.

**Causal chain**
1. kafka broker undergoes partition reassignment / rebalance affecting orders-3 (assumed) *(assumed)*
2. settlement consumer loses assignment or is starved, lag grows [F10]
3. zero settlements posted [F10]

**Supporting evidence**
  - `F4` ✔ settlement-service dependencies: consumes from kafka (queue-consume) and writes to ledger-db (db). Both kafka and ledger-db are marked shared:true and are owned by 'platform', not by the payments-owned settlement-service.
  - `F6` ✔ Two prior 'consumer lag growing / settlements stuck' incidents both cite kafka partition reassignment/rebalance as root cause (HIST-112 settlement-service 2026-02-03 'Consumer lost partition assignment during broker maintenance'; HIST-104 notification-service 2025-11-05 'Partition reassignment caused consumer group rebalancing storm') — these are leads only, not evidence about the current incident.
  - `F20` ✔ Historical leads do not match this incident's mechanism: HIST-112 (settlement-service 2026-02-03, 'Consumer lost partition assignment during broker maintenance') and HIST-104 (notification-service, 'Partition reassignment caused consumer group rebalancing storm') both cite kafka partition reassignment/rebalance, but T1 found zero rebalance events; a search for deserialization/schema-mismatch precedents returned 0 incidents. Both HIST hits are match_score-only leads, not evidence about the current incident.
**Contradicting evidence**
  - `F5` ✔ No config change or deploy was recorded for kafka or ledger-db on 2026-05-12 (kafka config count 0, kafka deploys count 0, ledger-db deploys count 0) — the broker and shared DB had no declared change in the window despite being shared components.
  - `F12` ✔ No rebalance or connection-pool log events were found on settlement-service in 07:00-09:59: both the 'rebalance' and 'pool' searches returned total_matched 0. No consumer-group rebalance evidence in the log store.
  - `F19` ✔ Shared-component check: settlement-service depends on kafka (queue-consume) and ledger-db (db); both are shared:true and owned by 'platform', and neither had any deploy or config change on 2026-05-12 (broker/DB change counts zero). No change to the broker or shared DB competes as an explanation for the stall.
  - `F8` ✔ The first ERROR on settlement-service in the window is a deserialization failure at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. This pattern fired 213 times (first_seen 07:42:14Z).
  - `F9` ✔ A second error pattern follows: 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first_seen 07:44:51Z) — the same offset 8841207 on partition orders-3 is retried indefinitely, which is the mechanism that stalls the partition.
  - `F20` ✔ Historical leads do not match this incident's mechanism: HIST-112 (settlement-service 2026-02-03, 'Consumer lost partition assignment during broker maintenance') and HIST-104 (notification-service, 'Partition reassignment caused consumer group rebalancing storm') both cite kafka partition reassignment/rebalance, but T1 found zero rebalance events; a search for deserialization/schema-mismatch precedents returned 0 incidents. Both HIST hits are match_score-only leads, not evidence about the current incident.
**Assumptions**
  - A broker event occurred without any recorded config/deploy change (F5 shows none)
  - The historical rebalance-pattern leads (F6) apply to this incident
**Rejected because:** Zero rebalance events on settlement-service (F12) directly falsifies the rebalance mechanism; no kafka change recorded (F5/F19); the observed error is a deterministic poison-message block at a specific offset with a schema mismatch, not starvation; historical leads are match_score-only and contradict this incident's mechanism (F20).

### H4: Downstream backpressure (ledger-db) or resource saturation on settlement-service stalls processing
Backpressure from ledger-db or CPU/DB-connection saturation on settlement-service stalls the consumer so messages are fetched but never committed. Lag grows without upstream involvement.

**Causal chain**
1. ledger-db write latency or settlement-service resource saturation slows processing *(assumed)*
2. consumer falls behind and commits nothing, lag grows [F10]
3. zero settlements posted [F10]

**Supporting evidence**
  - `F11` ✔ No resource saturation or DB pool exhaustion on settlement-service: db_pool_in_use stayed flat at ~5.85-6.17 avg (max 6.78) across 160 points from 07:20 to 09:52, and cpu_pct rose from 28.68% avg (07:20) to ~69-71% avg (max 78.28%) and stayed there — elevated but not saturated.
**Contradicting evidence**
  - `F11` ✔ No resource saturation or DB pool exhaustion on settlement-service: db_pool_in_use stayed flat at ~5.85-6.17 avg (max 6.78) across 160 points from 07:20 to 09:52, and cpu_pct rose from 28.68% avg (07:20) to ~69-71% avg (max 78.28%) and stayed there — elevated but not saturated.
  - `F5` ✔ No config change or deploy was recorded for kafka or ledger-db on 2026-05-12 (kafka config count 0, kafka deploys count 0, ledger-db deploys count 0) — the broker and shared DB had no declared change in the window despite being shared components.
  - `F8` ✔ The first ERROR on settlement-service in the window is a deserialization failure at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. This pattern fired 213 times (first_seen 07:42:14Z).
  - `F9` ✔ A second error pattern follows: 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first_seen 07:44:51Z) — the same offset 8841207 on partition orders-3 is retried indefinitely, which is the mechanism that stalls the partition.
  - `F19` ✔ Shared-component check: settlement-service depends on kafka (queue-consume) and ledger-db (db); both are shared:true and owned by 'platform', and neither had any deploy or config change on 2026-05-12 (broker/DB change counts zero). No change to the broker or shared DB competes as an explanation for the stall.
**Assumptions**
  - Some unmeasured backpressure exists despite flat db_pool_in_use
**Rejected because:** db_pool_in_use stayed flat ~5.85-6.17 and CPU elevated but not saturated (F11); no ledger-db change (F5/F19); the stall is explained by a specific deserialization/poison-message block (F8/F9), not throughput degradation.

## Facts (verified observations)
- Exactly two changes occurred across ALL services in 07:00Z-09:59Z on 2026-05-12: a settlement-service config change consumer.max.poll.records 500->200 at 07:35:00Z (author m.das) and the order-service deploy dep-6010 v2.16.0 at 07:40:00Z; both preceded the 08:22Z report. [F1, F15, F2]
- dep-6010 (order-service, v2.16.0, 07:40Z, status live, rolled_back_at null) changed the OrderExecuted event schema: field rename qty -> quantity, added field 'venue', and added an event schema version header v2; order-service is an upstream producer of the events settlement-service consumes. [F3, F17]
- Settlement consumer lag was at baseline through the 07:40Z bucket (avg 31.06) and jumped to avg 603.29 / max 819.95 in the 07:42:00Z bucket, placing onset between 07:40 and 07:42. [F7, F16]
- The first ERROR on settlement-service in the window is a deserialization failure at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field qty (schema v1 expected, header says v2)'; the pattern fired 213 times. [F8]
- A second error pattern follows: 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first_seen 07:44:51Z) — the same offset on orders-3 is retried indefinitely, stalling the partition. [F9]
- The deploy's declared change set matches the error signature verbatim: dep-6010 names 'rename field qty -> quantity', 'add field venue', 'event schema version header v2', and the first ERROR names exactly 'missing required field qty (schema v1 expected, header says v2)' at orders-3@8841207. [F17]
- Timing favors the deploy over the poll change: dep-6010 at 07:40:00Z precedes the first failed offset by ~2 minutes, whereas cfg-3004 at 07:35:00Z precedes it by ~7 minutes. [F16, F2]
- Settlement lag grew monotonically and did not recover: 603 (07:42), 20,525 avg / 22,062 max (08:25:34), 60,220.76 avg / 61,605.53 max (09:55); all 409 matched log lines in 07:00-09:59 were ERROR level. [F10]
- No rebalance or connection-pool log events were found on settlement-service in 07:00-09:59 ('rebalance' and 'pool' searches both returned total_matched 0). [F12]
- No resource saturation or DB pool exhaustion on settlement-service: db_pool_in_use stayed flat ~5.85-6.17 avg (max 6.78) over 160 points 07:20-09:52; cpu_pct rose from ~28.68% avg to ~69-71% avg (max 78.28%) — elevated but not saturated. [F11]
- order-service shows no error signal correlated with settlement onset: only 2 WARN lines in 07:00-09:59 (connection-reset retry 07:22:50Z, slow /internal/metrics 09:14:44Z), neither coinciding with the 07:42 onset. [F13]
- settlement-service depends on kafka (queue-consume) and ledger-db (db); both are shared:true and owned by 'platform', and neither had any deploy or config change recorded on 2026-05-12. [F4, F5, F19]
- The consumer poll-settings change cfg-3004 has no corresponding failure signature: no consumer-group rebalance or pool events on settlement-service, and a batch-size reduction cannot itself produce a schema/deserialization error, so its 07:35Z position is timing-only. [F12, F11]
- Historical leads do not match this incident's mechanism: HIST-112 and HIST-104 both cite kafka partition reassignment/rebalance, but zero rebalance events were found, and a search for deserialization/schema-mismatch precedents returned 0 incidents. [F6, F20]

## Assumptions (not verified)
- The v2-header OrderExecuted records on partition orders-3 were produced by order-service dep-6010; the deploy's declared change set matches the error verbatim, but no producer-side telemetry or schema-registry record confirms the specific message at offset 8841207.
- settlement-service has no forward-compatible / v2 schema handling, so a v2 payload is a hard deserialization failure for its consumer.
- The 'zero settlements processed' symptom follows from the blocked partition; there is no directly measured processed-settlements counter among available metrics.
- order-service is the only upstream producer of the OrderExecuted events consumed by settlement-service on topic 'orders'; a full producer enumeration for kafka could not be completed.

## Timeline
- `2026-05-12T07:35:00Z` settlement-service config change cfg-3004: consumer.max.poll.records 500->200 by m.das ('Smaller poll batches to smooth DB writes') — the reporter's suspected cause. [F1, F2, F15]
- `2026-05-12T07:40:00Z` order-service deploy dep-6010 v2.16.0 goes live, changing the OrderExecuted event schema (qty->quantity, added venue, added schema-version header v2). [F3, F16, F15]
- `2026-05-12T07:40:00Z` Last normal settlement lag bucket: avg 31.06. [F7, F16]
- `2026-05-12T07:42:00Z` First elevated lag bucket: avg 603.29 / max 819.95 — onset between 07:40 and 07:42. [F7, F16]
- `2026-05-12T07:42:14Z` First ERROR on settlement-service: deserialization failure at orders-3@8841207, 'missing required field qty (schema v1 expected, header says v2)'; 213 occurrences. [F8, F17]
- `2026-05-12T07:44:51Z` Second error pattern: 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences) — partition orders-3 blocked. [F9]
- `2026-05-12T08:22:00Z` Incident INC-010 reported by on-call SRE: no settlements posted ~40 minutes, settlement consumer lag growing. [F1]
- `2026-05-12T08:25:34Z` Settlement lag reaches avg 20,525 / max 22,062. [F10]
- `2026-05-12T09:52:00Z` Resource check: db_pool_in_use flat ~5.85-6.17 (max 6.78) over 160 points; cpu_pct elevated ~69-71% avg (max 78.28%), not saturated. [F11]
- `2026-05-12T09:55:00Z` Settlement lag ~60,220.76 avg / 61,605.53 max — approximately 60k and climbing, no recovery. [F10]

## Recommended actions
- **[immediate]** Roll back order-service deploy dep-6010 (v2.16.0) to the last v1-schema-producing version so order-service resumes emitting OrderExecuted with field 'qty' and no v2 schema-version header, unblocking partition orders-3. -- dep-6010's declared change set (qty->quantity, venue, v2 header) matches the settlement-service deserialization error verbatim at orders-3@8841207, and it deployed ~2 minutes before onset (F3, F7, F8, F16, F17). dep-6010 is still 'live' with rolled_back_at null (F3), so a rollback is available and is the fastest way to stop producing v2 payloads. [F3, F7, F8, F16, F17]
- **[immediate]** After rollback, unblock/skip the poison offset on partition orders-3 (reset consumer offset past orders-3@8841207 or acked-skip the poison record) via settlement-service's consumer tooling. -- The consumer retries the same offset indefinitely ('poison message: retrying orders-3@8841207 ... partition blocked', 196 occurrences), so the partition stays blocked even after the producer is fixed unless the stuck record is bypassed (F9, F14). [F9, F14]
- **[immediate]** Do NOT revert the settlement-service poll-settings change cfg-3004 as the primary mitigation, and do not treat it as the cause. -- cfg-3004 has no failure signature: zero rebalance/pool events on settlement-service, flat db_pool_in_use, and a batch-size reduction cannot produce a schema/deserialization error; its 07:35Z position is timing-only (F11, F12, F18). [F11, F12, F18]
- **[short_term]** Verify recovery: confirm settlement consumer lag returns to baseline (~26-55) and drains, the 'missing required field qty' and 'poison message ... partition blocked' error patterns stop, and CPU settles below the elevated ~69-71% plateau. -- Lag grew monotonically to ~60k with 409 ERROR lines and no recovery, so restoration must be demonstrated by lag drain plus cessation of both error patterns and CPU normalization (F8, F9, F10, F11). [F8, F9, F10, F11]
- **[short_term]** Add forward-compatible schema handling on settlement-service (accept/ignore unknown fields, tolerate a v2 schema-version header) and add a schema-compatibility gate so producer contract changes are caught before rollout. -- The consumer discarded the whole partition on a single unknown field, a hard failure mode for any future additive producer change; forward compatibility would have made qty->quantity plus an added field non-breaking (F8, F17). [F8, F17]
- **[long_term]** Adopt a producer/consumer contract-test or schema-registry compatibility check in the order-service and settlement-service release pipeline, and add a dead-letter-queue path so a poison message does not block an entire partition. -- The incident was caused by an unreviewed producer contract break (dep-6010) plus a consumer design that blocks the partition on one bad record; both need pipeline and consumer-level prevention to avoid recurrence (F3, F9, F17). [F3, F9, F17]
- **[long_term]** Alert on poison-message / deserialization error rate and partition-blocked conditions (not only on lag), so a blocked partition pages immediately instead of being discovered via a downstream lag report. -- The two ERROR patterns fired at 07:42:14Z and 07:44:51Z but the incident was only reported at 08:22Z (~40 minutes later) — earlier detection of these signatures shortens exposure (F8, F9, F10). [F8, F9, F10]

## Data gaps
- Tool-call budget was exhausted before the final two parallel queries (get_service_dependencies for kafka and a second historical-incident search), so the full set of upstream producers feeding settlement-service's kafka topic beyond order-service is unconfirmed, and no precedent linking a producer schema change to consumer stalls was found.
- get_config_changes/get_deployments were only queried per-service for kafka, order-service, and ledger-db plus one all-services call starting at 07:00Z; the window 00:00Z-07:00Z for other services was not fully enumerated, so a pre-07:00Z change by a third service affecting settlement-service could have been missed.
- Broker/topic metrics (produce rate, partition lag distribution, bytes in/out) could not be queried and no kafka-broker available_metrics listing was obtained, so upstream starvation vs consumer-side stall cannot be separated from broker-side metrics.
- kafka-broker and trade-service log searches were not executed, so no broker-side or alternate-consumer error signatures could be gathered.
- No producer-side log, deploy telemetry, or schema-registry record was retrieved to identify the actor that began emitting v2 OrderExecuted events, so the identity of the schema-changing producer (inferred to be order-service dep-6010) is not independently confirmed.
- No metric-based evidence of 'zero settlements processed' exists among available metrics (consumer_lag, cpu_pct, db_pool_in_use, error_rate, latency_p99_ms, memory_pct, request_rate, restarts), so that symptom is inferred from the blocked-partition lag shape rather than directly measured.
- No producer-side or schema-registry telemetry confirms the specific record at offset orders-3@8841207 was produced by dep-6010; the link rests on the deploy's declared change set matching the error text exactly.

## What would change this conclusion
- Producer-side or schema-registry evidence showing the v2-header OrderExecuted records on orders-3 were emitted by a service other than order-service (or by a producer unchanged by dep-6010).
- Rolling back dep-6010 without any other change yet finding the partition still blocked and the 'missing required field qty' errors persisting, which would point away from the producer contract break.
- Recovery of settlement lag with no rollback of dep-6010 (e.g. after only cfg-3004 reverted or a consumer restart), which would support the poll-settings change (H2) or a transient/broker cause instead.
- Discovery of consumer-group rebalance, partition-reassignment, or pool-exhaustion events on settlement-service in a fuller log/metric query, which would revive H2 or H3.
- Evidence of a broker-side event (partition reassignment, produce starvation) or a ledger-db change on 2026-05-12 that was outside the queried per-service windows, which would support an infrastructure cause (H3/H4).
- A finding that settlement-service actually does have forward-compatible v2 handling, which would break the deserialization mechanism underlying H1.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6

## Investigation stats
- latency 188.49s, LLM calls 13, tokens 79334 (in 62448 / out 16886)
- tool calls 22 (0 failed, 0 partial, 0 recovered by retry), findings 20/20 verified
