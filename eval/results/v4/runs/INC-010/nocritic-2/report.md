# Incident investigation report: INC-010
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The settlement stall on INC-010 is most likely caused by the order-service v2.16.0 schema release at 07:40Z, which renamed the OrderExecuted field qty→quantity and bumped the schema header to v2, producing messages that settlement-service's v1 deserializer cannot parse (top hypothesis H1, confidence 0.60, medium). The signature is unambiguous on the consumer side: a repeated "missing required field 'qty' (schema v1 expected, header says v2)" error on the same offset orders-3@8841207 from 07:42:14Z, escalating into a poison-message retry loop that blocks partition 3, with lag climbing from ~38 to ~60k and throughput collapsing to ~0.4 req/s. Competing explanations were rejected: no rebalance/connection/DB-timeout errors exist in the log stream, resource metrics show no exhaustion and zero restarts, and the other consumer (notification-service) and producer (payment-service) on the shared broker are healthy, ruling out shared broker/DB faults. The reporter's suspicion of the 07:35Z poll-setting change is not supported — it produces no signal at 07:35 and cannot mechanistically block a partition. Confidence is capped at medium because producer-side emission of v2 records was not directly observed and broker/queue metrics were never queried.

## Probable root cause
**order-service v2.16.0 schema change (qty→quantity) produced unconsumable OrderExecuted events** -- bad_deployment in `order-service`, triggered by `tc-005` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 schema change (qty→quantity) produced unconsumable OrderExecuted events | bad_deployment | order-service | tc-005 | 0.60 (medium) | active |
| 2 | H2: Consumer poll-settings change on settlement-service degraded consumption (reporter's suspicion) | config_change | settlement-service | tc-006 | 0.07 (low) | rejected |
| 3 | H3: Shared broker or shared ledger-db dependency failing (settlement as victim) | infrastructure_failure | kafka broker / ledger-db | null | 0.05 (low) | rejected |
| 4 | H4: Resource exhaustion / broker-side fault driven by a change (CPU, GC, connections, partition reassignment) | resource_exhaustion | settlement-service / kafka broker | null | 0.04 (low) | rejected |

### H1: order-service v2.16.0 schema change (qty→quantity) produced unconsumable OrderExecuted events
order-service v2.16.0 (deployed 07:40Z) renamed the OrderExecuted field qty→quantity and bumped the schema version header to v2. settlement-service's avro/deserializer still expects v1 and requires 'qty', so the first v2 OrderExecuted message to land on orders-3@8841207 (07:42:14Z) fails to deserialize; because the offset is never committed, the partition is blocked, retries accumulate as a poison-message loop, and consumer lag grows without bound while throughput drops to ~zero. The fault originates in the producer's schema release, not in settlement-service.

**Causal chain**
1. order-service v2.16.0 deployed at 07:40Z, renaming OrderExecuted qty->quantity and bumping event schema header to v2 [F2]
2. The deployer begins emitting v2 OrderExecuted records onto the shared orders topic consumed by settlement-service *(assumed)*
3. Settlement lag departs from its flat ~38-55 baseline at the 07:41 bucket and grows monotonically to ~60k [F9]
4. Settlement throughput collapses from ~61-63 req/s to ~0.41 by the 07:48 bucket [F10]
5. First logged error at 07:42:14Z is a deserialization failure on orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2), repeated 213x [F11]
6. From 07:44:51Z the same offset becomes a poison message; retries in the tens of thousands block partition 3 [F12]
7. The only error stream is schema/poison-message; no fetch, rebalance, connection, DB-timeout or RPC signatures [F13]
8. Downstream latency and pod resources are flat/exhaustion-free, so nothing else is blocking the consumer [F14, F18]

**Supporting evidence**
  - `F2` ✔ The only deployment across all services in the window was order-service v2.16.0 at 2026-05-12T07:40:00Z (author a.khan), which renamed the OrderExecuted event field qty -> quantity, added field 'venue', and added an event schema version header v2; status live, not rolled back.
  - `F9` ✔ Consumer lag on settlement-service switches from a flat baseline of ~38-55 messages to sustained growth at the 07:41 bucket: 07:39 bucket avg 37.78/max 39.7 → 07:41 avg 206.44/max 386.63 → 07:43 avg 1063.21 → 07:49 avg 3688.8/max 3865.43, reaching avg 60412.45/max 61605.53 by 09:56. This is a hard departure from baseline, not a gradual pre-existing trend.
  - `F10` ✔ Throughput stopped essentially in the same bucket as the lag onset: request_rate was ~61-63 req/s through the 07:32 bucket, fell to avg 16.32 in the 07:40 bucket, and was down to avg 0.41/max 0.44 by the 07:48 bucket where it stayed flat (~0.40-0.41) for the rest of the window — i.e. zero settlements processed within ~2 minutes of the first deserialization error, not a long slow ramp.
  - `F11` ✔ Dominant error signature in settlement-service logs is a deserialization/schema failure, first at 2026-05-12T07:42:14Z and repeated 213 times through 09:58:31Z: "failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)" — the same offset orders-3@8841207 recurs in every occurrence.
  - `F12` ✔ The error escalates into a poison-message retry loop on the same partition from 2026-05-12T07:44:51Z (196 occurrences, attempts in the tens of thousands, e.g. attempt 92490): "poison message: retrying orders-3@8841207 (attempt 92490), partition blocked" — i.e. the stuck offset blocks partition 3 consumption.
  - `F13` ✔ All 409 matched log records in 07:00-09:59 were ERROR level, and the only two patterns are the deserialization failure and the poison-message/partition-blocked retry. No fetch/publish errors, rebalance storms, connection resets, DB timeouts or RPC failures appear in the settlement-service error stream.
  - `F14` ✔ settlement-service pod/resource metrics show no exhaustion signal: cpu_pct went 27.48 (07:32 bucket) to ~58.26 (07:40) then held ~68-71 flat to the end (max 78.28); memory_pct stayed ~49-51 flat; db_pool_in_use ~5.8-6.2 flat (max 6.78); restarts 0.0 for the entire window.
  - `F18` ✔ Downstream call latency from settlement-service did NOT degrade at onset: latency_p99_ms is flat across the stall (07:36 avg 243.6, 07:48 avg 238.79, 08:00 avg 239.16, 09:48 avg 237.22; mean 238.27, missing_points 0) — no DB-timeout/RPC-failure latency signature, so downstream latency is not the first mover.
  - `F8` ✔ Timeline note: the settlement-service poll-settings change (07:35Z) precedes the order-service v2 schema deployment (07:40Z) by 5 minutes; both sit ~47 min before the 08:22Z report, so neither is immediately adjacent to the reported onset.
**Contradicting evidence**
  - `F1` ✔ The only config change across all services in the window was on settlement-service: consumer.max.poll.records reduced from 500 to 200 at 2026-05-12T07:35:00Z (author m.das, reason 'Smaller poll batches to smooth DB writes').
**Assumptions**
  - order-service actually began publishing v2 OrderExecuted records at 07:40Z — not directly confirmed (F21: no order-service log lines containing 'quantity'; producer-side emission unverified)
  - orders-3 is the partition carrying the newly-emitted v2 records
  - The 07:41 lag-onset bucket and the 07:42:14Z first error are within the same onset event given ~11-minute bucket resolution

### H2: Consumer poll-settings change on settlement-service degraded consumption (reporter's suspicion)
The settlement-service config change at 07:35Z reducing consumer.max.poll.records from 500 to 200 caused rebalances / inefficient polling such that the consumer stopped making progress, driving lag growth and zero settlements posted. Under this reading the schema error is a symptom of the same degraded consumer (slow processing exposes/exacerbates the failing offset) rather than the cause.

**Causal chain**
1. consumer.max.poll.records reduced 500->200 on settlement-service at 07:35Z [F1]
2. Smaller poll batches cause session-timeout-driven rebalances / repeated reprocessing, stalling the consumer group *(assumed)*
3. Consumption stalls, zero settlements posted, lag climbs to ~60k [F9, F10]

**Supporting evidence**
  - `F1` ✔ The only config change across all services in the window was on settlement-service: consumer.max.poll.records reduced from 500 to 200 at 2026-05-12T07:35:00Z (author m.das, reason 'Smaller poll batches to smooth DB writes').
**Contradicting evidence**
  - `F8` ✔ Timeline note: the settlement-service poll-settings change (07:35Z) precedes the order-service v2 schema deployment (07:40Z) by 5 minutes; both sit ~47 min before the 08:22Z report, so neither is immediately adjacent to the reported onset.
  - `F11` ✔ Dominant error signature in settlement-service logs is a deserialization/schema failure, first at 2026-05-12T07:42:14Z and repeated 213 times through 09:58:31Z: "failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)" — the same offset orders-3@8841207 recurs in every occurrence.
  - `F13` ✔ All 409 matched log records in 07:00-09:59 were ERROR level, and the only two patterns are the deserialization failure and the poison-message/partition-blocked retry. No fetch/publish errors, rebalance storms, connection resets, DB timeouts or RPC failures appear in the settlement-service error stream.
  - `F15` ✔ The lag onset (~07:41 bucket) follows the reported consumer poll-setting change and precedes the first logged error (07:42:14Z) by about a minute; the first error is the schema mismatch on orders-3@8841207, and the poison/partition-blocked loop starts 07:44:51Z. The stall is upstream-inbound (an unprocessable inbound orders topic record), not a resource or broker-side fault visible on settlement-service.
  - `F16` ✔ Coarse 11-minute buckets make the lag onset and the order-service deploy look simultaneous, but at this resolution the poll-settings change at 07:35Z has no accompanying signal: order-service request_rate is flat across the change (07:36 bucket avg 230.97, 07:47 avg 231.53, vs 06:30 avg 225.64; window mean 231.5, min 197.21 max 256.25, missing_points 0), and settlement throughput collapse appears only in the 07:40-07:48 buckets — consistent with the 07:40Z deploy, not the 07:35Z config change.
  - `F9` ✔ Consumer lag on settlement-service switches from a flat baseline of ~38-55 messages to sustained growth at the 07:41 bucket: 07:39 bucket avg 37.78/max 39.7 → 07:41 avg 206.44/max 386.63 → 07:43 avg 1063.21 → 07:49 avg 3688.8/max 3865.43, reaching avg 60412.45/max 61605.53 by 09:56. This is a hard departure from baseline, not a gradual pre-existing trend.
  - `F10` ✔ Throughput stopped essentially in the same bucket as the lag onset: request_rate was ~61-63 req/s through the 07:32 bucket, fell to avg 16.32 in the 07:40 bucket, and was down to avg 0.41/max 0.44 by the 07:48 bucket where it stayed flat (~0.40-0.41) for the rest of the window — i.e. zero settlements processed within ~2 minutes of the first deserialization error, not a long slow ramp.
**Assumptions**
  - max.poll.records=200 alone can trigger rebalance/partition-loss on this consumer group
  - The consumer group never logged a rebalance warning (none were retrievable, F21)
**Rejected because:** Mechanistically contradicted: the error stream contains zero rebalance/session-timeout/fetch errors (F13) and the dominant signature is a schema deserialization failure (F11) which is unrelated to poll batch size. Timing also fails: order-service and its request_rate are flat across 07:35 while the collapse aligns with the 07:40Z signal (F16, F8), and lag/throughput onset (~07:41/07:40) is better explained by the deploy than the config change. Smaller poll batches would reduce throughput per poll, not produce a hard zero with a blocked partition.

### H3: Shared broker or shared ledger-db dependency failing (settlement as victim)
The shared kafka broker (queue-consume) or shared ledger-db (postgres, four writers) degraded — broker rebalance/ISR/leadership loss, or DB slowness from another writer — blocking settlement-service mid-processing so lag accumulates.

**Causal chain**
1. Shared broker or shared DB degrades *(assumed)*
2. Settlement-service consumers stall or block on I/O *(assumed)*
3. Lag grows and zero settlements post [F9, F10]

**Supporting evidence**
  - `F3` ✔ settlement-service consumes from the shared kafka broker (queue-consume) and reads/writes the shared ledger-db (postgres, owner platform); both are shared components used by multiple services.
  - `F4` ✔ Upstream producers feeding the shared kafka broker are order-service (queue-produce) and payment-service (queue-produce); settlement-service and notification-service both consume from the same broker, so a broker-side rebalance would affect both consumers.
  - `F5` ✔ ledger-db is shared by four writers: order-service, portfolio-service, payment-service and settlement-service all have db edges to ledger-db, so a change or load in any of those can affect settlement posting through the shared DB.
**Contradicting evidence**
  - `F6` ✔ No config changes were found for the shared kafka broker in the window (count 0).
  - `F13` ✔ All 409 matched log records in 07:00-09:59 were ERROR level, and the only two patterns are the deserialization failure and the poison-message/partition-blocked retry. No fetch/publish errors, rebalance storms, connection resets, DB timeouts or RPC failures appear in the settlement-service error stream.
  - `F18` ✔ Downstream call latency from settlement-service did NOT degrade at onset: latency_p99_ms is flat across the stall (07:36 avg 243.6, 07:48 avg 238.79, 08:00 avg 239.16, 09:48 avg 237.22; mean 238.27, missing_points 0) — no DB-timeout/RPC-failure latency signature, so downstream latency is not the first mover.
  - `F19` ✔ The other upstream producer to the shared broker, payment-service, is healthy at onset: request_rate flat (07:40 avg 99.78, 07:54 avg 99.36, mean 99.96, missing_points 0) and latency_p99_ms flat (07:40 avg 320.82, 07:54 avg 322.63, mean 319.73). payment-service therefore did not change its publish behaviour at 07:40Z.
  - `F20` ✔ The other consumer of the same shared broker, notification-service, shows no symptom at onset — a broker-side rebalance/common-component cause would be expected to affect it too, but its error_rate is flat through the window (07:26 avg 0.23, 07:40 avg 0.19, 07:54 avg 0.2, mean 0.21, missing_points 0), so broker-wide consumer disruption is not supported.
  - `F14` ✔ settlement-service pod/resource metrics show no exhaustion signal: cpu_pct went 27.48 (07:32 bucket) to ~58.26 (07:40) then held ~68-71 flat to the end (max 78.28); memory_pct stayed ~49-51 flat; db_pool_in_use ~5.8-6.2 flat (max 6.78); restarts 0.0 for the entire window.
**Assumptions**
  - A shared-component degradation occurred without any config change on the broker (F6 found none)
**Rejected because:** notification-service, the other consumer of the same broker, shows no error at onset (F20); payment-service, the other producer, is flat (F19); downstream call latency is flat with no DB-timeout signature (F18); error stream has no connection/reset/fetch/DB-timeout lines (F13); no broker config change in window (F6). No positive evidence for a shared-component failure.

### H4: Resource exhaustion / broker-side fault driven by a change (CPU, GC, connections, partition reassignment)
Load or a change drove settlement-service into CPU/memory/thread/connection exhaustion, or a broker-side partition reassignment removed the consumer's assignment (as in the HIST-112 precedent), stalling consumption while lag grows.

**Causal chain**
1. Resource or partition-assignment loss occurs *(assumed)*
2. Consumer cannot process, lag grows unbounded [F9]

**Supporting evidence**
  - `F7` ✘ unverified Prior settlement-lag precedent: HIST-112 (2026-02-03) 'Settlement consumer stalled after partition reassignment' — lag growing, settlements stuck, root cause 'Consumer lost partition assignment during broker maintenance', resolved by restarting the consumer group; HIST-104 describes a Kafka broker rebalance storm causing consumer lag.
**Contradicting evidence**
  - `F14` ✔ settlement-service pod/resource metrics show no exhaustion signal: cpu_pct went 27.48 (07:32 bucket) to ~58.26 (07:40) then held ~68-71 flat to the end (max 78.28); memory_pct stayed ~49-51 flat; db_pool_in_use ~5.8-6.2 flat (max 6.78); restarts 0.0 for the entire window.
  - `F13` ✔ All 409 matched log records in 07:00-09:59 were ERROR level, and the only two patterns are the deserialization failure and the poison-message/partition-blocked retry. No fetch/publish errors, rebalance storms, connection resets, DB timeouts or RPC failures appear in the settlement-service error stream.
  - `F20` ✔ The other consumer of the same shared broker, notification-service, shows no symptom at onset — a broker-side rebalance/common-component cause would be expected to affect it too, but its error_rate is flat through the window (07:26 avg 0.23, 07:40 avg 0.19, 07:54 avg 0.2, mean 0.21, missing_points 0), so broker-wide consumer disruption is not supported.
  - `F10` ✔ Throughput stopped essentially in the same bucket as the lag onset: request_rate was ~61-63 req/s through the 07:32 bucket, fell to avg 16.32 in the 07:40 bucket, and was down to avg 0.41/max 0.44 by the 07:48 bucket where it stayed flat (~0.40-0.41) for the rest of the window — i.e. zero settlements processed within ~2 minutes of the first deserialization error, not a long slow ramp.
**Assumptions**
  - A partition reassignment happened at ~07:40 despite no broker config change (F6)
**Rejected because:** settlement-service pod metrics show no exhaustion: cpu flat ~68-71%, memory flat ~49-51%, db_pool_in_use flat ~6, zero restarts (F14). No rebalance/partition-assignment errors in the log stream (F13), and the other consumer on the broker is unaffected (F20). The only support is an UNVERIFIED prior-incident precedent (F7), which is weak evidence.

## Facts (verified observations)
- The only deployment across all services in the window was order-service v2.16.0 at 2026-05-12T07:40:00Z (author a.khan), which renamed the OrderExecuted event field qty -> quantity, added field 'venue', and added event schema version header v2; it is live and was not rolled back. [F2]
- The only config change across all services in the window was on settlement-service: consumer.max.poll.records reduced from 500 to 200 at 2026-05-12T07:35:00Z. [F1]
- The dominant settlement-service error signature is a deserialization/schema failure, first at 2026-05-12T07:42:14Z and repeated 213 times through 09:58:31Z: "failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)" — the same offset orders-3@8841207 recurs in every occurrence. [F11]
- The error escalated into a poison-message retry loop on the same partition from 2026-05-12T07:44:51Z (196 occurrences, attempts in the tens of thousands, e.g. attempt 92490), reporting 'partition blocked' on orders-3. [F12]
- All 409 matched settlement-service log records in 07:00-09:59 were ERROR level, and the only two patterns are the deserialization failure and the poison-message/partition-blocked retry; no fetch/publish errors, rebalance storms, connection resets, DB timeouts or RPC failures appear. [F13]
- Consumer lag on settlement-service switched from a flat baseline of ~38-55 messages to sustained growth at the 07:41 bucket (avg 206 -> 1063 -> 3688), reaching avg 60412/max 61605 by 09:56 — a hard departure from baseline. [F9]
- Throughput collapsed in the same bucket as the lag onset: request_rate fell from ~61-63 req/s (07:32 bucket) to avg 16.32 (07:40) and to a flat ~0.40-0.41 for the remainder of the window. [F10]
- settlement-service pod/resource metrics show no exhaustion: cpu_pct rose to ~68-71 and held flat, memory_pct ~49-51 flat, db_pool_in_use ~5.8-6.2 flat, restarts 0.0 for the entire window. [F14]
- Downstream call latency from settlement-service did not degrade at onset: latency_p99_ms flat across the stall (07:36 avg 243.6, 07:48 avg 238.79, 09:48 avg 237.22). [F18]
- The other upstream producer to the shared broker, payment-service, is healthy at onset: request_rate flat (mean 99.96) and latency_p99_ms flat (mean 319.73). [F19]
- The other consumer of the same shared broker, notification-service, shows no symptom at onset: error_rate flat through the window (mean 0.21), so a broker-side rebalance/common-component cause is not supported. [F20]
- Upstream producer order-service shows no degradation at onset: error_rate flat and negligible for the whole window (mean 0.22), with no produce-error or publish-failure signature. [F17]
- No config changes were found for the shared kafka broker in the window (count 0). [F6]
- No settlement-service WARN/ERROR log lines with 'lag' and no order-service log lines containing 'quantity' were retrievable in the onset window (both queries returned 0 matches), so producer-side emission and consumer-rebalance warnings could not be directly confirmed. [F21]
- settlement-service consumes from the shared kafka broker (queue-consume) and reads/writes the shared ledger-db; order-service (queue-produce) and payment-service (queue-produce) produce to that broker, and settlement-service and notification-service both consume from it. [F3, F4]
- ledger-db is shared by four writers (order-service, portfolio-service, payment-service, settlement-service), so a change or load in any of them could affect settlement posting through the shared DB. [F5]

## Assumptions (not verified)
- ASSUMPTION: order-service actually began publishing v2 OrderExecuted records at 07:40Z — producer-side emission was not directly confirmed (F21 returned no order-service log lines containing 'quantity').
- ASSUMPTION: orders-3 is the partition carrying the newly-emitted v2 records and the failing offset orders-3@8841207 corresponds to a v2 message.
- ASSUMPTION: the 07:41 lag-onset bucket and the 07:42:14Z first error fall within the same onset event, given ~11-minute bucket resolution.
- ASSUMPTION: no other consumer of the orders topic failed in the same way outside the observed scope, i.e. the blast radius is limited to what was queried.
- ASSUMPTION: the truncated log retrieval (40 of 409 matches) did not hide additional error signatures that would change the diagnosis.
- (demoted from facts: no verified observation backs it) order-service request_rate is flat across the 07:35Z poll-settings change (07:36 bucket avg 230.97, 07:47 avg 231.53, window mean 231.5), and settlement throughput collapse appears only in the 07:40-07:48 buckets.

## Timeline
- `2026-05-12T07:35:00Z` settlement-service config change: consumer.max.poll.records reduced from 500 to 200 (only config change across all services; no signal accompanies it). [F1, F16]
- `2026-05-12T07:40:00Z` order-service v2.16.0 deployed (only deployment in window): OrderExecuted field qty renamed to quantity, venue added, schema version header bumped to v2. [F2, F8]
- `2026-05-12T07:40:00Z-07:48:00Z` settlement-service throughput collapses from ~61-63 req/s to a flat ~0.40-0.41 req/s. [F10]
- `2026-05-12T07:41:00Z` Consumer lag departs baseline (~38-55 msgs) and begins sustained growth, reaching avg ~60k by 09:56. [F9]
- `2026-05-12T07:42:14Z` First deserialization failure logged: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field qty (schema v1 expected, header says v2)'. [F11]
- `2026-05-12T07:44:51Z` Error escalates into a poison-message retry loop; partition 3 becomes blocked on the same offset orders-3@8841207. [F12]
- `2026-05-12T08:22:00Z` Incident INC-010 reported: no settlements posted for ~40 min, consumer lag growing. []
- `2026-05-12T09:56:00Z-09:58:31Z` Lag plateaus at avg 60412/max 61605; the same schema error continues to repeat (213 occurrences total). [F9, F11]

## Recommended actions
- **[immediate]** Immediately roll back order-service from v2.16.0 to the prior release (revert a.khan's 07:40Z deploy) so it stops emitting v2 OrderExecuted records with the renamed qty→quantity field, then restart the settlement-service consumer group to release the blocked partition offset orders-3@8841207. -- H1 is the leading root cause (confidence 0.60): the fault originates in the producer schema release, and the poison-message loop on orders-3 blocks partition consumption. Rollback plus consumer restart is the fail-fast mitigation that clears the stuck offset and restores throughput. Note: the poll-settings change (F1) is not believed causal, so do not rely on reverting it alone to restore service. [F2, F11, F12, F1]
- **[immediate]** Skip or dead-letter the poison offset orders-3@8841207 on the orders topic (or reset the settlement-service consumer group offset past it) to unblock partition 3 immediately if rollback cannot proceed within the SLO. -- The partition is blocked solely on a single unparseable offset (F12); bypassing that offset clears the block even before the producer is fixed, allowing other settlements to drain. [F12, F11]
- **[short_term]** Confirm the fix: verify order-service no longer publishes v2 qty-renamed OrderExecuted records, confirm settlement consumer lag trends back toward the ~38-55 baseline, request_rate recovers to ~61 req/s, and the deserialization error stops appearing in settlement-service logs. -- Closes the loop on the H1 mechanism by observing that throughput returns to the pre-incident baseline (F9, F10) and the schema error signature disappears (F11). [F9, F10, F11]
- **[short_term]** Do NOT revert the 07:35Z settlement-service poll-settings change as a fix; keep it or revert independently only after the schema issue is resolved, then re-measure throughput. -- H2 was rejected: the poll-settings change has no accompanying signal at 07:35 (F16) and cannot mechanistically block a partition; reverting it risks confusing the recovery signal. [F16, F13, F1]
- **[long_term]** Add a producer/consumer schema contract gate (CI check or registry compatibility test) that fails order-service builds when an OrderExecuted field is renamed or a schema version header is bumped without a negotiated backward-compatible consumer upgrade. -- Prevents recurrence of the exact failure mode in H1: an unconsumable v2 message blocking a partition (F2, F11). [F2, F11]
- **[long_term]** Add consumer-side alerting on partition-blocked / poison-message events and a consumer-lag SLO alert so a single blocked offset surfaces within minutes rather than as a 40-minute silent stall. -- The stall was detected only via reported symptoms; lag and the poison loop grew for ~40 minutes (F9, F12). [F9, F12]

## Data gaps
- Broker/queue health metrics (topic partition status, broker CPU/IO, under-replicated partitions, ISR, request errors, consumer group rebalance count) were never queried due to tool-call budget exhaustion; broker/queue instability cannot be ruled in or out from the available data.
- No log query at INFO level for the pre-onset window (07:35-07:50) completed (budget_exhausted), so what changed at 07:40-07:42 (config reload, consumer poll-setting change event) could not be confirmed from logs.
- Producer-side confirmation that order-service v2.16.0 actually began emitting qty→quantity v2 OrderExecuted records is unverified: order-service logs returned 0 matches for 'quantity' (F21).
- Neighbouring services (other consumers of the orders topic) were not queried, so the blast radius beyond settlement-service is undetermined.
- Settlement-service log retrieval was truncated (40 of 409 matches returned), so rare additional error signatures could be hidden in the unreturned records.
- Metric bucket resolution for order-service (660s) and payment-service/notification-service (840s) is coarser than the minute-level ordering needed, so the exact minute at which upstream behaviour changed cannot be pinned below the ~11-minute bucket.
- Feature-flag changes across other services were not separately enumerated, so any flag-specific changes in the window may be missing.
- Historical incident search returned only 5 results and did not surface precedents tied to poll-settings or producer schema-version changes; precedent set may be incomplete.

## What would change this conclusion
- Observation that order-service v2.16.0 did NOT actually emit any v2 OrderExecuted records with the qty→quantity rename (e.g. producer-side logs or topic payload inspection showing v1 messages only) would remove the only mechanism linking the deploy to the failing offset.
- Identification of a different, non-schema cause for the offset orders-3@8841207 deserialization failure — e.g. a corrupt or malformed message from another producer, or a schema-registry/deserializer change on the consumer side — would redirect the root cause away from H1.
- Broker-side metrics showing a partition reassignment, ISR/leadership loss, or rebalance at ~07:40Z (as in the HIST-112 precedent, F7) that also affected notification-service would shift suspicion toward a shared broker fault (H3).
- Evidence of consumer-group rebalance/session-timeout events on settlement-service coincident with the 07:35Z poll-settings change, or evidence that max.poll.records=200 alone caused partition loss, would resurrect H2.
- Evidence that log truncation hid a dominant error signature other than the deserialization failure would weaken the consumer-side schema-failure diagnosis.
- Confirmation that other consumers of the orders topic also failed on the same v2 messages would broaden (but not overturn) the H1 diagnosis; absence of such failures beyond settlement-service would narrow the blast radius.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: order-service request_rate is flat across the 07:35Z poll-settings change (07:36

## Investigation stats
- latency 174.18s, LLM calls 12, tokens 78362 (in 61831 / out 16531)
- tool calls 24 (0 failed, 0 partial, 0 recovered by retry), findings 20/21 verified
