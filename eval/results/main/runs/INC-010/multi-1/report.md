# Incident investigation report: INC-010
*System: multi_agent | outcome: **inconclusive** | ended: critic_inconclusive*

## Summary
Settlements stopped because settlement-service's consumer is permanently blocked on a single poison record, orders-3@8841207, which it cannot deserialize ("missing required field 'qty'; schema v1 expected, header says v2") — the mechanism, onset and scope are well established. The onset is tightly correlated with the window's only deployment, order-service v2.16.0 at 07:40:00Z: lag began rising and request_rate collapsed at ~07:42Z, with the first deserialization error at 07:42:14Z and explicit "poison message ... partition blocked" retries from 07:44:51Z. The top hypothesis H1 (order-service v2.16.0 emitted a v2-schema record the settlement v1 reader cannot parse) is the best-supported explanation, but confidence is capped at 0.50 because its decisive causal hop — that this specific record was produced by v2.16.0 after 07:40Z, and whether the fault is a malformed producer payload (H1) or a stale v1 consumer reader (H5, 0.42) — rests on record provenance and reader-version data that no available surface exposes. Alternatives H3 (broker reassignment) and H2 (poll-config change) are weakly supported or untestable, and H4 (ledger-db) is excluded by flat telemetry. Outcome: inconclusive, with a safe mitigation and two discriminating checks below.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 schema change produced a record the settlement consumer cannot deserialize, poisoning partition 3 | bad_deployment | order-service | order-service v2.16.0 (2026-05-12T07:40:00Z) | 0.50 (medium) | active |
| 2 | H5: Settlement-service's own stale v1 consumer reader is the owning fault; the producer is healthy and spec-compliant (version skew in the consumer) | config_change | settlement-service | order-service v2.16.0 (2026-05-12T07:40:00Z) | 0.42 (low) | active |
| 3 | H3: Kafka partition-3 reassignment/leader change stalled the consumer (historical pattern HIST-104/HIST-112) | infrastructure_failure | kafka (broker) | null | 0.05 (low) | active |
| 4 | H2: settlement-service consumer poll-config change caused a rebalance/stall loop (reporter's suspicion) | config_change | settlement-service | settlement-service max.poll.records 500->200 (2026-05-12T07:35:00Z) | 0.03 (low) | active |
| 5 | H4: Shared ledger-db degradation blocked the consumer; schema/config changes are coincidental | dependency_failure | ledger-db | null | 0.03 (low) | rejected |

### H1: order-service v2.16.0 schema change produced a record the settlement consumer cannot deserialize, poisoning partition 3
order-service v2.16.0 (deployed 07:40:00Z) changed the OrderExecuted event schema ('qty'->'quantity', added 'venue', schema-version header v2). The first such record landed at orders-3@8841207 ~07:42Z; settlement-service's consumer failed to deserialize it ('missing required field qty', schema v1 expected, header says v2), and because the offset can never be committed it permanently blocks partition 3. No further offsets are consumed, so request_rate collapses to ~0.4 req/s, consumer_lag climbs 603->61605, and CPU stays elevated in the tight retry loop.

**Causal chain**
1. order-service v2.16.0 deployed at 07:40:00Z changes the OrderExecuted schema: qty->quantity, +venue, header schema version v2; it is the only deploy in the window [F1, F23]
2. order-service produces the new-schema event to the shared kafka topic, landing on partition orders-3 and reaching settlement-service within ~2 min [F4, F7]
3. settlement-service consumer reads offset orders-3@8841207 with a v1 reader and fails: missing required field 'qty' (schema v1 expected, header says v2) [F9, F28]
4. Failure is not transient: the same offset is retried tens of thousands of times and blocks partition 3 [F10, F11, F29, F30]
5. Partition blocked -> no commits/processing -> request_rate collapses ~61->~0.4 req/s and consumer_lag climbs 603->61605 [F8, F7]
6. CPU holds ~70% as the retry/deserialize loop spins [F13]

**Supporting evidence**
  - `F1` ✔ Only one deployment occurred across all services in 07:00-09:59Z: order-service v2.16.0 deployed at 2026-05-12T07:40:00Z (author a.khan), status live, not rolled back; it changes the OrderExecuted Kafka event schema ('rename field qty -> quantity', 'add field venue', 'event schema version header v2').
  - `F4` ✔ order-service (upstream of settlement-service via the shared kafka topic) also produces to the same shared kafka ('queue-produce') and uses the same shared ledger-db, so any order-service change propagates to settlement-service through two shared components.
  - `F7` ✔ settlement-service consumer_lag was flat/baseline (~26-64 messages) through 07:40Z, then began rising at 07:42Z: bucket 07:42Z avg 603.29 max 819.95, and climbed to 61605.53 by 09:56Z. So lag growth started at ~07:42Z, NOT at the 08:22Z report time.
  - `F8` ✔ settlement-service request_rate collapsed at the same instant: ~61-67 req/s through 07:38Z, then bucket 07:42Z avg 0.39 max 0.41 and stayed ~0.4 req/s afterwards. This is the signature of the consumer ceasing to process/commit, consistent with 'zero settlements processed'.
  - `F9` ✔ The earliest ERROR in settlement-service is 2026-05-12T07:42:14Z, a deserialization failure: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. This is the first error signature and coincides with the lag/request-rate onset.
  - `F10` ✔ The consumer IS actively polling and retrying, not idle. A second error pattern appears from 07:44:51Z: 'poison message: retrying orders-3@8841207 (attempt 92490), partition blocked' -- the same offset 8841207 is retried repeatedly (attempt counters in the tens of thousands) and the partition is BLOCKED by that poison message, so no further offsets are consumed.
  - `F11` ✔ The error load spans 07:42:14Z to 09:58:31Z: 409 ERROR log lines total in that window, dominated by exactly two patterns ('failed to deserialize OrderExecuted...' x213 and 'poison message: retrying orders-3@8841207... partition blocked' x196). The single poison offset is orders-3@8841207.
  - `F28` ✔ The settlement-service ERROR log identifies the poison record by exact coordinates orders-3@8841207 (topic 'orders', partition 3, offset 8841207) and states the consumer-side deserialization failure is 'missing required field 'qty' (schema v1 expected, header says v2)'. This means the reader is a v1 schema reader (expecting 'qty') while the record's schema-version header declares v2 — the failure is the header/value version mismatch on this specific record, first seen 2026-05-12T07:42:14Z.
  - `F29` ✔ The same offset 8841207 is retried indefinitely as a poison message and blocks partition 3: 'poison message: retrying orders-3@8841207 (attempt 92490), partition blocked' from 07:44:51Z onward, with attempt counters in the thousands to tens of thousands (92490, 25391, 56347, 14422, 74563, ...). The partition cannot advance past that offset, which explains zero settlements processed and the growing lag.
  - `F30` ✔ Across the whole window the ERROR traffic is exactly two patterns tied to the single offset 8841207: 'failed to deserialize OrderExecuted...' x213 and 'poison message: retrying orders-3@8841207... partition blocked' x196 = 409 ERROR lines, first 07:42:14Z, last 09:58:31Z. Only one poison offset exists in the observed data, and no other offsets are named.
  - `F23` ✔ No deployment occurred on any other service in 07:30-08:30Z; the only one is order-service v2.16.0 at 07:40:00Z (status live, not rolled back). This bounds the config/deploy-driven rebalance question: nothing was deployed to a broker or a second consumer in the window.
**Contradicting evidence**
  - `F12` ✔ No degradation seen on order-service (also on the same broker): only 1 WARN in 07:30-09:59Z ('slow request 2414ms on /internal/metrics' at 09:14:44Z), and order-service does not even expose consumer_lag. This suggests the fault is specific to settlement-service's consumer of the orders topic, not a broker-wide outage.
  - `F14` ✔ settlement-service memory_pct is flat across the incident: ~49-51% avg throughout (min 44.33, max 57.8), showing no memory-pressure ramp at onset.
  - `F15` ✔ settlement-service request latency p99 is flat and unchanging across onset: avg ~233-243 ms in every bucket from 07:15Z to 09:49Z (min 214.76, max 266.35, mean 238.72). No latency degradation accompanies the stall.
  - `F16` ✔ settlement-service error_rate is flat at ~0.19-0.25% HTTP 5xx before and after onset (mean 0.21, max 0.32) - no rise in server error rate at 07:42Z, so the failure is not surfacing as HTTP 5xx errors.
  - `F17` ✔ settlement-service db_pool_in_use is flat at ~6 connections (mean 5.99, min 5.21, max 6.78) across the whole window including onset, and restarts is 0 for every bucket from 07:15Z to 09:49Z. No DB connection-pool exhaustion and no pod restarts / crash-loop, so the consumer is not dying on a dependency outage.
  - `F22` ✔ None of the three requested ledger-db metrics (slow_queries_per_min, replication_lag_ms, connections_active) deviates at or before the 07:42Z onset; together with the already-established flat cpu_pct ~30%, ledger-db shows no measurable degradation coinciding with the settlement stall, weighing against H4.
**Assumptions**
  - The poison offset 8841207 was produced by order-service AFTER the 07:40Z deploy (no produced-at timestamp observed; F31/F32/F41 show producer-side and payload surfaces are unavailable).
  - The record's value actually carries 'quantity'+'venue' under a v2 header (vs. a malformed/legacy value with a mis-set v2 header); unreadable per F32/F41.
  - order-service is the producer of this record on the orders topic to which settlement-service subscribes (topology not directly proven at record level).

### H5: Settlement-service's own stale v1 consumer reader is the owning fault; the producer is healthy and spec-compliant (version skew in the consumer)
The order-service v2.16.0 schema change is a legitimate, backward-incompatible-at-v1 change AND the settlement-service consumer was never upgraded to a v2-aware reader. The fault ORIGINATES in settlement-service's consumer configuration/version skew (its v1 reader), not in the producer. The producer emits well-formed v2 records; only the settlement consumer, still pinned to a v1 schema, chokes and blocks its own partition. Fix belongs to settlement-service (upgrade reader / add schema-registry compatibility), not order-service.

**Causal chain**
1. order-service v2.16.0 legitimately begins emitting v2-headered OrderExecuted records at ~07:40Z [F1]
2. settlement-service's consumer reader remains a v1 reader and has not been redeployed/upgraded in the window (only order-service deployed) [F1, F23]
3. The two error patterns on settlement-service are incompatible-schema/poison errors localized to a single consumer's partition [F9, F10, F11, F28, F29, F30]
4. The only other consumer of the orders topic (notification-service) shows NO lag growth, NO deserialize errors, and healthy throughput at onset [F33, F34, F35, F36]
5. Absence of the signal on the other consumer points to consumer-side ownership (settlement's reader) rather than a producer/broker-wide fault [F37]
6. Settlement-consumer partition blocked -> request_rate collapse and lag climb confined to settlement-service [F8, F7, F34]

**Supporting evidence**
  - `F25` ✔ Topology of the shared kafka: only two queue-consume edges exist on kafka -- settlement-service and notification-service. Therefore notification-service is the ONLY other consumer of the orders topic that would experience lag/deserialization errors if the poison event affected all consumers.
  - `F33` ✔ notification-service consumer_lag shows NO lag growth across 07:15Z-08:30Z: it oscillates in a flat baseline band of roughly 40-70 messages (min 18.27, max 94.61, mean 59.96) with no upward trend at or after the 07:42Z onset — pre-onset buckets 07:35Z avg 67.44 and 07:39Z avg 64.17 vs post-onset 07:43Z avg 57.52 and 07:47Z avg 59.73. This is the flat baseline pattern, in stark contrast to settlement-service's climb to 61605.53.
  - `F34` ✔ notification-service consumer_lag stays flat for the whole incident through 09:55Z (08:30Z-09:55Z buckets avg 52.31-68.19, min 21.96, max 94.98, mean 61.2) — no sustained climb 2.5 hours after onset. The lag-growth symptom is confined to settlement-service.
  - `F35` ✔ notification-service shows NO deserialization or schema error signature at any level 07:15Z-09:59Z: the 'deserialize' filter returned total_matched=0 (tc-046), the ERROR-level query for 07:15Z-08:30Z returned total_matched=0 (tc-045), and the only WARN in 07:15Z-09:59Z is a single unrelated 'clock drift 38ms detected, resyncing with NTP' at 08:39:18Z. So none of 'missing required field qty', 'header says v2', 'OrderExecuted' or 'poison message' appears on notification-service.
  - `F36` ✔ notification-service is functionally healthy at onset on two independent metrics besides lag: request_rate is flat at ~147 req/s throughout (07:15Z-08:27Z avg 143.31-150.41, mean 147.4, min 133.71, max 166.73) with no collapse at 07:42Z — unlike settlement-service whose request_rate fell to ~0.4 req/s; and cpu_pct is flat at ~25% (mean 24.98, min 23.09, max 27.46) the entire window. notification-service never stopped processing.
  - `F37` ✔ notification-service (the ONLY other consumer of the orders topic on the shared kafka, per F25) shows neither the lag growth nor the identical deserialization signature that settlement-service shows. A record-level schema/poison event on the shared topic would poison every consumer of that partition; a broker/producer-side fault affecting settlement-service's partition 3 would also hit notification-service (also on kafka). Its clean state therefore points away from a topic-wide producer/broker fault and toward something specific to settlement-service's consumer (its v1 schema reader) — i.e. consumer-side ownership.
  - `F9` ✔ The earliest ERROR in settlement-service is 2026-05-12T07:42:14Z, a deserialization failure: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. This is the first error signature and coincides with the lag/request-rate onset.
  - `F28` ✔ The settlement-service ERROR log identifies the poison record by exact coordinates orders-3@8841207 (topic 'orders', partition 3, offset 8841207) and states the consumer-side deserialization failure is 'missing required field 'qty' (schema v1 expected, header says v2)'. This means the reader is a v1 schema reader (expecting 'qty') while the record's schema-version header declares v2 — the failure is the header/value version mismatch on this specific record, first seen 2026-05-12T07:42:14Z.
**Contradicting evidence**
  - `F30` ✔ Across the whole window the ERROR traffic is exactly two patterns tied to the single offset 8841207: 'failed to deserialize OrderExecuted...' x213 and 'poison message: retrying orders-3@8841207... partition blocked' x196 = 409 ERROR lines, first 07:42:14Z, last 09:58:31Z. Only one poison offset exists in the observed data, and no other offsets are named.
  - `F41` ✔ The only place offset 8841207 appears in settlement-service logs is ERROR-level: an INFO-level query for '8841207' (tc-057) returned total_matched=409, counts_by_level {"ERROR": 409} — i.e. zero INFO lines. No payload dump, no schema header value beyond the error text, and no produced-at timestamp is logged for the poison record; the sole provenance facts are coordinates orders-3@8841207 and the message text 'schema v1 expected, header says v2'.
**Assumptions**
  - notification-service consumes the orders topic but is NOT assigned partition 3 (its clean state could reflect partition coverage, not schema tolerance); partition map is unexposed per F26/F37 gaps.
  - The producer's v2 payload is well-formed (cannot be read; error text is the only evidence of the mismatch side).
  - settlement-service's consumer code version at 07:42Z was unchanged/stale (its last deploy was not directly read; bounded only by F1/F23 showing no settlement deploy in the window).

### H3: Kafka partition-3 reassignment/leader change stalled the consumer (historical pattern HIST-104/HIST-112)
A broker-side event on the shared kafka cluster (partition reassignment, leader change, rebalance storm as in HIST-112/HIST-104) around 07:42Z stalled settlement-service's consumer group on partition 3, while the schema/config changes are coincidental.

**Causal chain**
1. A partition-level reassignment/leader change occurs on orders partition 3 at ~07:42Z *(assumed)*
2. Settlement-service consumer group is left blocked on partition 3, so no offsets are consumed [F7, F8]
3. Lag climbs and request_rate collapses as a result [F7, F8]
4. Historical precedent exists for exactly this pattern [F5, F27]

**Supporting evidence**
  - `F5` ✔ Two historical incidents match the reported symptom pattern: HIST-112 (2026-02-03, settlement-service + kafka) 'Settlement consumer stalled after partition reassignment' with 'consumer lag growing','settlements stuck', and HIST-104 (2025-11-05, notification-service + kafka) lag spike after Kafka broker rebalance; both root causes were partition reassignment, not consumer config or schema changes -- leads only.
  - `F7` ✔ settlement-service consumer_lag was flat/baseline (~26-64 messages) through 07:40Z, then began rising at 07:42Z: bucket 07:42Z avg 603.29 max 819.95, and climbed to 61605.53 by 09:56Z. So lag growth started at ~07:42Z, NOT at the 08:22Z report time.
  - `F8` ✔ settlement-service request_rate collapsed at the same instant: ~61-67 req/s through 07:38Z, then bucket 07:42Z avg 0.39 max 0.41 and stayed ~0.4 req/s afterwards. This is the signature of the consumer ceasing to process/commit, consistent with 'zero settlements processed'.
**Contradicting evidence**
  - `F9` ✔ The earliest ERROR in settlement-service is 2026-05-12T07:42:14Z, a deserialization failure: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. This is the first error signature and coincides with the lag/request-rate onset.
  - `F10` ✔ The consumer IS actively polling and retrying, not idle. A second error pattern appears from 07:44:51Z: 'poison message: retrying orders-3@8841207 (attempt 92490), partition blocked' -- the same offset 8841207 is retried repeatedly (attempt counters in the tens of thousands) and the partition is BLOCKED by that poison message, so no further offsets are consumed.
  - `F11` ✔ The error load spans 07:42:14Z to 09:58:31Z: 409 ERROR log lines total in that window, dominated by exactly two patterns ('failed to deserialize OrderExecuted...' x213 and 'poison message: retrying orders-3@8841207... partition blocked' x196). The single poison offset is orders-3@8841207.
  - `F28` ✔ The settlement-service ERROR log identifies the poison record by exact coordinates orders-3@8841207 (topic 'orders', partition 3, offset 8841207) and states the consumer-side deserialization failure is 'missing required field 'qty' (schema v1 expected, header says v2)'. This means the reader is a v1 schema reader (expecting 'qty') while the record's schema-version header declares v2 — the failure is the header/value version mismatch on this specific record, first seen 2026-05-12T07:42:14Z.
  - `F29` ✔ The same offset 8841207 is retried indefinitely as a poison message and blocks partition 3: 'poison message: retrying orders-3@8841207 (attempt 92490), partition blocked' from 07:44:51Z onward, with attempt counters in the thousands to tens of thousands (92490, 25391, 56347, 14422, 74563, ...). The partition cannot advance past that offset, which explains zero settlements processed and the growing lag.
  - `F30` ✔ Across the whole window the ERROR traffic is exactly two patterns tied to the single offset 8841207: 'failed to deserialize OrderExecuted...' x213 and 'poison message: retrying orders-3@8841207... partition blocked' x196 = 409 ERROR lines, first 07:42:14Z, last 09:58:31Z. Only one poison offset exists in the observed data, and no other offsets are named.
  - `F12` ✔ No degradation seen on order-service (also on the same broker): only 1 WARN in 07:30-09:59Z ('slow request 2414ms on /internal/metrics' at 09:14:44Z), and order-service does not even expose consumer_lag. This suggests the fault is specific to settlement-service's consumer of the orders topic, not a broker-wide outage.
  - `F24` ✔ No config change occurred on kafka in 07:30-07:45Z (scope kafka returned count 0), so there is no evidence of a broker-side config/flag action coincident with the 07:42Z onset.
  - `F33` ✔ notification-service consumer_lag shows NO lag growth across 07:15Z-08:30Z: it oscillates in a flat baseline band of roughly 40-70 messages (min 18.27, max 94.61, mean 59.96) with no upward trend at or after the 07:42Z onset — pre-onset buckets 07:35Z avg 67.44 and 07:39Z avg 64.17 vs post-onset 07:43Z avg 57.52 and 07:47Z avg 59.73. This is the flat baseline pattern, in stark contrast to settlement-service's climb to 61605.53.
  - `F34` ✔ notification-service consumer_lag stays flat for the whole incident through 09:55Z (08:30Z-09:55Z buckets avg 52.31-68.19, min 21.96, max 94.98, mean 61.2) — no sustained climb 2.5 hours after onset. The lag-growth symptom is confined to settlement-service.
  - `F36` ✔ notification-service is functionally healthy at onset on two independent metrics besides lag: request_rate is flat at ~147 req/s throughout (07:15Z-08:27Z avg 143.31-150.41, mean 147.4, min 133.71, max 166.73) with no collapse at 07:42Z — unlike settlement-service whose request_rate fell to ~0.4 req/s; and cpu_pct is flat at ~25% (mean 24.98, min 23.09, max 27.46) the entire window. notification-service never stopped processing.
**Assumptions**
  - A broker event actually occurred at onset (never queried; no broker event surface exists).
  - A partition-3-only event would leave the poison-offset error as the dominant symptom rather than a distinct rebalance/assignment message.
  - order-service/notification-service on the same broker would still be healthy under a partition-3-only event (F12/F33/F36 are consistent with this).

### H2: settlement-service consumer poll-config change caused a rebalance/stall loop (reporter's suspicion)
The 07:35Z settlement-service config change (max.poll.records 500->200, the only config change in the window) caused the consumer to stall or rebalance-loop so nothing processes.

**Causal chain**
1. settlement-service max.poll.records changed 500->200 at 07:35Z [F2]
2. Smaller poll batches induce commit/session-timeout or rebalance churn that halts processing *(assumed)*
3. Zero settlements processed and lag climbs [F7, F8]

**Supporting evidence**
  - `F2` ✔ The only config change across all services in 07:00-09:59Z was on settlement-service: consumer.max.poll.records changed from 500 to 200 at 2026-05-12T07:35:00Z by m.das ('Smaller poll batches to smooth DB writes').
  - `F7` ✔ settlement-service consumer_lag was flat/baseline (~26-64 messages) through 07:40Z, then began rising at 07:42Z: bucket 07:42Z avg 603.29 max 819.95, and climbed to 61605.53 by 09:56Z. So lag growth started at ~07:42Z, NOT at the 08:22Z report time.
  - `F8` ✔ settlement-service request_rate collapsed at the same instant: ~61-67 req/s through 07:38Z, then bucket 07:42Z avg 0.39 max 0.41 and stayed ~0.4 req/s afterwards. This is the signature of the consumer ceasing to process/commit, consistent with 'zero settlements processed'.
**Contradicting evidence**
  - `F9` ✔ The earliest ERROR in settlement-service is 2026-05-12T07:42:14Z, a deserialization failure: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. This is the first error signature and coincides with the lag/request-rate onset.
  - `F10` ✔ The consumer IS actively polling and retrying, not idle. A second error pattern appears from 07:44:51Z: 'poison message: retrying orders-3@8841207 (attempt 92490), partition blocked' -- the same offset 8841207 is retried repeatedly (attempt counters in the tens of thousands) and the partition is BLOCKED by that poison message, so no further offsets are consumed.
  - `F11` ✔ The error load spans 07:42:14Z to 09:58:31Z: 409 ERROR log lines total in that window, dominated by exactly two patterns ('failed to deserialize OrderExecuted...' x213 and 'poison message: retrying orders-3@8841207... partition blocked' x196). The single poison offset is orders-3@8841207.
  - `F13` ✔ settlement-service CPU climbed from a flat ~27-28% baseline (07:15Z/07:26Z buckets) to 49.98 avg / 71.68 max in the 07:37Z bucket, then held ~70% (70.47-71.2 avg) for the rest of the window through 09:49Z. The CPU rise begins at the 07:37Z bucket, i.e. at or just before the 07:42Z onset.
  - `F15` ✔ settlement-service request latency p99 is flat and unchanging across onset: avg ~233-243 ms in every bucket from 07:15Z to 09:49Z (min 214.76, max 266.35, mean 238.72). No latency degradation accompanies the stall.
  - `F17` ✔ settlement-service db_pool_in_use is flat at ~6 connections (mean 5.99, min 5.21, max 6.78) across the whole window including onset, and restarts is 0 for every bucket from 07:15Z to 09:49Z. No DB connection-pool exhaustion and no pod restarts / crash-loop, so the consumer is not dying on a dependency outage.
  - `F28` ✔ The settlement-service ERROR log identifies the poison record by exact coordinates orders-3@8841207 (topic 'orders', partition 3, offset 8841207) and states the consumer-side deserialization failure is 'missing required field 'qty' (schema v1 expected, header says v2)'. This means the reader is a v1 schema reader (expecting 'qty') while the record's schema-version header declares v2 — the failure is the header/value version mismatch on this specific record, first seen 2026-05-12T07:42:14Z.
  - `F29` ✔ The same offset 8841207 is retried indefinitely as a poison message and blocks partition 3: 'poison message: retrying orders-3@8841207 (attempt 92490), partition blocked' from 07:44:51Z onward, with attempt counters in the thousands to tens of thousands (92490, 25391, 56347, 14422, 74563, ...). The partition cannot advance past that offset, which explains zero settlements processed and the growing lag.
  - `F30` ✔ Across the whole window the ERROR traffic is exactly two patterns tied to the single offset 8841207: 'failed to deserialize OrderExecuted...' x213 and 'poison message: retrying orders-3@8841207... partition blocked' x196 = 409 ERROR lines, first 07:42:14Z, last 09:58:31Z. Only one poison offset exists in the observed data, and no other offsets are named.
**Assumptions**
  - A reduced max.poll.records alone can block a partition indefinitely.
  - No explicit rebalance/session-timeout log exists at INFO level (never checked).

### H4: Shared ledger-db degradation blocked the consumer; schema/config changes are coincidental
The shared ledger-db (used by both order-service and settlement-service) degraded around onset, causing the consumer to block on DB calls so it fetches but cannot commit, growing lag.

**Causal chain**
1. ledger-db slows at onset *(assumed)*
2. Consumer blocks on ledger-db writes and stops committing [F7, F8]
3. Historical precedent that ledger-db can stall downstream consumers [F6]

**Supporting evidence**
  - `F6` ✔ Past ledger-db problems also reached settlement-service before: HIST-111 (2025-05-22) 'Reporting job full table scans slowed ledger-db' involving ledger-db and settlement-service, and HIST-110 (2026-01-15) ledger-db IOPS throttling affected order-service -- precedent that the shared ledger-db can stall downstream consumers.
**Contradicting evidence**
  - `F9` ✔ The earliest ERROR in settlement-service is 2026-05-12T07:42:14Z, a deserialization failure: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. This is the first error signature and coincides with the lag/request-rate onset.
  - `F10` ✔ The consumer IS actively polling and retrying, not idle. A second error pattern appears from 07:44:51Z: 'poison message: retrying orders-3@8841207 (attempt 92490), partition blocked' -- the same offset 8841207 is retried repeatedly (attempt counters in the tens of thousands) and the partition is BLOCKED by that poison message, so no further offsets are consumed.
  - `F11` ✔ The error load spans 07:42:14Z to 09:58:31Z: 409 ERROR log lines total in that window, dominated by exactly two patterns ('failed to deserialize OrderExecuted...' x213 and 'poison message: retrying orders-3@8841207... partition blocked' x196). The single poison offset is orders-3@8841207.
  - `F15` ✔ settlement-service request latency p99 is flat and unchanging across onset: avg ~233-243 ms in every bucket from 07:15Z to 09:49Z (min 214.76, max 266.35, mean 238.72). No latency degradation accompanies the stall.
  - `F17` ✔ settlement-service db_pool_in_use is flat at ~6 connections (mean 5.99, min 5.21, max 6.78) across the whole window including onset, and restarts is 0 for every bucket from 07:15Z to 09:49Z. No DB connection-pool exhaustion and no pod restarts / crash-loop, so the consumer is not dying on a dependency outage.
  - `F18` ✔ ledger-db shows no degradation at onset: cpu_pct flat at ~30% (min 27.12, max 33.45, mean 30.06) across 07:15Z-09:49Z. Its exposed metrics are connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min (no latency metric available).
  - `F19` ✔ ledger-db slow_queries_per_min is flat across the 07:15Z-08:30Z window (min 0.4, max 1.61, mean 1.0 queries/min) with no elevation at the 07:42Z onset: buckets 07:39Z=0.83, 07:41Z=1.04, 07:43Z=1.12, all within pre-onset baseline noise.
  - `F20` ✔ ledger-db replication_lag_ms is flat across 07:15Z-08:30Z (min 36.24, max 42.5, mean 39.89 ms); pre-onset 07:41Z avg 40.39 and post-onset 07:43Z avg 39.73 are indistinguishable, so no replication stall at/before onset.
  - `F21` ✔ ledger-db connections_active shows no pile-up or drop around onset: min 55.62, max 68.58, mean 61.53 connections; 07:41Z avg 62.36 and 07:43Z avg 61.91 match the surrounding baseline.
  - `F22` ✔ None of the three requested ledger-db metrics (slow_queries_per_min, replication_lag_ms, connections_active) deviates at or before the 07:42Z onset; together with the already-established flat cpu_pct ~30%, ledger-db shows no measurable degradation coinciding with the settlement stall, weighing against H4.
**Assumptions**
  - ledger-db had query-latency/lock contention at 07:42Z; every available metric is flat, but it exposes no latency metric so a sub-2-minute blip cannot be fully resolved.
  - A DB stall would produce a deserialization error and a single repeating poison offset (it would not).
**Rejected because:** All four ledger-db metric families that exist (cpu_pct F18, slow_queries_per_min F19, replication_lag_ms F20, connections_active F21) are flat across the 07:15Z-08:30Z window including the 07:41/07:43Z buckets, and settlement-service db_pool_in_use is flat at ~6 with zero restarts (F17). A DB-side block would not produce a deserialization error or a single repeating poison offset (F9, F10/F29). Mechanism mismatch plus zero corroborating telemetry.

## Facts (verified observations)
- Only one deployment occurred across all services in 07:00-09:59Z: order-service v2.16.0 at 2026-05-12T07:40:00Z (author a.khan, status live, not rolled back), and it changes the OrderExecuted Kafka event schema (rename qty->quantity, add venue, schema version header v2). [F1, F23]
- The only config change in the window was on settlement-service: consumer.max.poll.records 500->200 at 2026-05-12T07:35:00Z by m.das. [F2]
- settlement-service consumer_lag was flat at baseline (~26-64) through 07:40Z, rose starting 07:42Z (bucket avg 603.29) and climbed to 61605.53 by 09:56Z; lag growth began ~07:42Z, not at the 08:22Z report time. [F7]
- settlement-service request_rate collapsed at the same instant: ~61-67 req/s through 07:38Z, then ~0.39-0.41 req/s from the 07:42Z bucket onward, consistent with the consumer ceasing to process/commit and with zero settlements processed. [F8]
- The earliest settlement-service ERROR is at 2026-05-12T07:42:14Z: a deserialization failure 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' — the consumer-side reader expects v1 while the record header declares v2. [F9, F28]
- The consumer is actively polling and retrying, not idle: from 07:44:51Z onward the same offset is retried indefinitely ('poison message: retrying orders-3@8841207 (attempt 92490), partition blocked') with attempt counters in the thousands to tens of thousands, so partition 3 cannot advance past that offset. [F10, F29]
- Across the whole window ERROR traffic is exactly two patterns tied to the single offset 8841207: 'failed to deserialize OrderExecuted...' x213 and 'poison message: retrying orders-3@8841207... partition blocked' x196 = 409 ERROR lines, first 07:42:14Z, last 09:58:31Z; no other offsets are named. [F11, F30]
- settlement-service CPU climbed from a flat ~27-28% baseline to 49.98 avg / 71.68 max in the 07:37Z bucket and held ~70% thereafter, consistent with a tight retry loop rather than a dependency stall. [F13]
- settlement-service memory_pct (~49-51%), latency_p99_ms (~233-243 ms), error_rate (~0.21% HTTP 5xx), and db_pool_in_use (~6) are all flat across onset, and restarts is 0 in every bucket 07:15Z-09:49Z — no memory ramp, no latency degradation, no connection-pool exhaustion and no pod crash-loop. [F14, F15, F16, F17]
- ledger-db shows no degradation coinciding with the stall: cpu_pct flat ~30%, slow_queries_per_min flat (07:41Z=1.04, 07:43Z=1.12), replication_lag_ms flat (~40 ms), and connections_active flat (~62), so no measured DB-side event at or before onset. [F18, F19, F20, F21]
- order-service (on the same broker) shows no degradation: only 1 WARN in 07:30-09:59Z and it does not expose consumer_lag, suggesting the fault is specific to settlement-service's consumption of the orders topic rather than a broker-wide outage. [F12]
- In 07:30-07:45Z no config change occurred on kafka (count 0) and no deployment occurred on any other service in 07:30-08:30Z besides order-service v2.16.0. [F24, F23]
- notification-service is the ONLY other queue-consume consumer of kafka besides settlement-service; it shows no lag growth (flat ~40-70 messages through 09:55Z), zero deserialization/schema errors, flat request_rate ~147 req/s and flat cpu_pct ~25% throughout. [F25, F33, F34, F35, F36]
- notification-service had zero deployments and zero config changes in 07:00-09:59Z, so its consumer behaviour was not altered by any deploy/config in the window. [F26]
- The only place offset 8841207 appears in settlement-service logs is ERROR-level (409 lines, zero INFO); no payload dump, no schema header value beyond the error text, and no produced-at timestamp is logged for the poison record. [F41, F32]
- Producer-side and record-metadata surfaces do not exist anywhere queried: order-service log searches for 'schema', 'quantity', 'OrderExecuted', 'produced' and '8841207' all returned 0 matches; kafka metrics are limited to [cpu_pct, messages_in_per_s, under_replicated_partitions] with no record_metadata/producer_id; kafka logs are empty; settlement-service exposes no schema_version or record_timestamp metric. [F31, F38, F39, F40]

## Assumptions (not verified)
- The poison offset 8841207 was produced by order-service AFTER the 07:40Z deploy; no produced-at timestamp was observed on any surface (F31/F32/F41).
- The record's value actually carries 'quantity'+'venue' under a v2 header rather than a malformed/legacy value with a mis-set v2 header; the raw payload is unreadable (F32/F41).
- order-service is the producer of this record on the orders topic to which settlement-service subscribes; the record-level topology is not directly proven.
- settlement-service's consumer code version at 07:42Z was stale (still a v1 reader); its last deploy was not directly read, only bounded by F1/F23 showing no settlement deploy in the window.
- notification-service's clean state reflects consumer-specific behaviour rather than merely not being assigned partition 3; the topic/partition subscription map per consumer is not exposed by any available tool.
- Whether a genuine produced_at field exists, and whether it is after 07:40Z, is unverifiable from the exposed telemetry and should be treated as permanently unobservable.
- (demoted from facts: no verified observation backs it) The poison record's first-seen time 07:42:14Z is after the 07:40:00Z order-service v2.16.0 deploy, but this ordering is inferred from deploy-vs-first-error times, not from record metadata.
- (demoted from facts: no verified observation backs it) Two historical incidents matched the symptom pattern (HIST-112 partition reassignment on settlement-service + kafka; HIST-104 notification-service lag after Kafka broker rebalance) but match scores are low (3 and 2) and the repo records no broker event for the current case.

## Timeline
- `2026-05-12T07:35:00Z` settlement-service config change: consumer.max.poll.records 500->200 by m.das ('Smaller poll batches to smooth DB writes'); the only config change across all services in the window. [F2]
- `2026-05-12T07:37:00Z` settlement-service CPU rises from a flat ~27-28% baseline to 49.98 avg / 71.68 max, remaining ~70% thereafter. [F13]
- `2026-05-12T07:40:00Z` order-service v2.16.0 deployed (author a.khan, status live, not rolled back); changes the OrderExecuted event schema (qty->quantity, add venue, header v2). Only deployment in 07:00-09:59Z. [F1]
- `2026-05-12T07:42:00Z` settlement-service onset: consumer_lag breaks from baseline (~26-64) to bucket avg 603.29 and request_rate collapses from ~61-67 req/s to ~0.39-0.41 req/s; both symptoms begin here, not at the 08:22Z report time. [F7, F8]
- `2026-05-12T07:42:14Z` First settlement-service ERROR: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. [F9, F28]
- `2026-05-12T07:44:51Z` Second error pattern begins: 'poison message: retrying orders-3@8841207 (attempt 92490), partition blocked' — the same offset is retried indefinitely, blocking partition 3 so no further offsets are consumed. [F10, F29]
- `2026-05-12T08:22:00Z` Incident reported by on-call SRE (SEV-2): no settlements posted for ~40 minutes, settlement consumer lag growing. []
- `2026-05-12T08:39:18Z` notification-service's only WARN in the window: an unrelated 'clock drift 38ms detected, resyncing with NTP'; no deserialization or schema errors at any level. [F35]
- `2026-05-12T09:14:44Z` order-service's only WARN in 07:30-09:59Z: 'slow request 2414ms on /internal/metrics'; no broker-wide degradation observed. [F12]
- `2026-05-12T09:56:00Z` settlement-service consumer_lag reaches 61605.53; notification-service lag remains flat (~40-70) through 09:55Z. [F7, F34]
- `2026-05-12T09:58:31Z` Last ERROR in the window: cumulative 409 ERROR lines, all tied to the single offset orders-3@8841207 (213 deserialize +196 poison-message/partition-blocked). [F11, F30]

## Recommended actions
- **[immediate]** Unblock the consumer without waiting for root-cause attribution: skip/park the poison offset orders-3@8841207 for the settlement-service consumer group (offset advance / dead-letter the record), so partition 3 can resume. This is the fastest path to restoring settlements. -- The partition is provably blocked on one offset and cannot advance (F10/F11/F29/F30); moving past it removes the outage regardless of whether the fault is producer- or consumer-side. [F10, F11, F29, F30]
- **[immediate]** Do NOT roll back order-service v2.16.0 on its own as the first action; if a rollback is considered, verify with the order-service owners first, because the v2 schema change is the leading trigger but the fault could equally be the stale v1 reader (H5). -- Both H1 (0.50) and H5 (0.42) depend on the same v2 schema change; H5 fixes ownership to settlement-service. A unilateral rollback could be the wrong service change. [F1, F28, F37]
- **[short_term]** Discriminate H1 vs H5 by fetching the raw value of orders-3@8841207 (and its schema-version header) from the Kafka topic/consumer-group tooling, and the settlement-service consumer image version / schema-registry compatibility setting in effect at 07:42Z. -- This is the single decisive check: if the payload carries 'quantity'+'venue' under a v2 header while settlement-service runs a v1 reader, ownership moves to settlement-service (H5); if the producer emitted a malformed or legacy payload with a v2 header, H1 stands. Neither surface was reachable in the investigation (F31/F32/F38/F39/F40/F41). [F28, F32, F38, F39, F41]
- **[short_term]** Add a schema-compatibility guard on both sides: order-service should register/validate the OrderExecuted v2 schema in the schema registry with a backward-compatibility check before deploy, and settlement-service should add a dead-letter path plus a schema-registry-aware deserializer so no single record can block a partition. -- The outage mechanism is a permanent offset block on one undeserializable record (F10/F29); DLQ + schema-registry-aware deserialization makes the failure mode non-blocking. [F9, F10, F29, F30]
- **[short_term]** Verify the fix on two independent metrics: settlement-service consumer_lag returning to the ~26-64 baseline and request_rate recovering from ~0.4 req/s to ~61-67 req/s. -- These two metrics define the onset (F7/F8) and are the fastest objective recovery signal. [F7, F8]
- **[short_term]** If the raw record shows the fault is producer-side malformed output rather than consumer version skew, gate v2.16.0 and roll it back or patch it; if it shows consumer-side v1 skew, upgrade settlement-service's reader instead and leave the producer. -- Ownership fork is the unresolved discriminator between the two live hypotheses (H1/H5); the action depends on the record-level check above. [F1, F28, F37]
- **[short_term]** If the record-level check is also inconclusive, close out H3 by querying Kafka broker/consumer-group state for 'orders' (partition 3 reassignment/leader-change/rebalance history) in 07:30-08:30Z. H3 is low-probability (0.05) but untestable with the current tool set and shares the historical pattern HIST-104/HIST-112. -- H3's exclusion rests on the broker never being queried, not on a negative finding (F24 shows no kafka config change; F12/F33/F36 show no topic-wide impact). Confirming no broker event occurred closes the loop. [F5, F24, F12, F33]
- **[long_term]** Prevent recurrence of the schema-compatibility class: enforce a schema-registry compatibility contract in CI for both order-service and settlement-service, add per-consumer consumer_lag alerts on the orders topic, and document the producer-consumer ownership of the OrderExecuted schema. -- The window had a legitimate backward-incompatible-at-v1 deploy and a consumer that failed to advance; a compatibility gate plus lag alerting would have caught the mismatch at deploy time and given settlement-service advance warning. [F1, F7, F28]
- **[long_term]** Add a nonexistent-resolution log/metric surface for record provenance (producer id/version, produced_at, raw payload one-line dump) at ERROR level in consumers so future poison-record incidents are attributable without log archaeology. -- The investigation could not read the poison record's provenance from any surface (F31/F32/F38/F39/F40/F41), which is precisely what left the outcome inconclusive. [F32, F41, F38, F39, F40]
- **[long_term]** Reconsider the settlement-service consumer.max.poll.records 500->200 change (07:35Z) after the incident is resolved; it is weakly implicated (H2, 0.03) and is a reversible config change that was not shown to help. -- H2 is contradicted by a single-offset poison mechanism and flat p99/pool/restarts, but the config change is the reporter's stated suspicion and the only change on the affected service (F2/F15/F17). [F2, F15, F17]

## Data gaps
- No Kafka broker, topic or consumer-group state was queryable: broker/partition reassignment, leader change, rebalance history and consumer-group assignment are all unavailable, so H3 cannot be confirmed or excluded (F24 only shows no kafka config change; F12/F33/F36 show no topic-wide impact).
- The poison record's provenance is not obtainable: producer id/version and produced_at are absent from every queried surface (order-service log searches returned 0 matches; kafka has no record_metadata/producer_id metric; kafka logs are empty; settlement-service exposes no schema_version/record_timestamp metric).
- The raw value of orders-3@8841207 (actual field names 'qty' vs 'quantity', presence of 'venue', and the schema-version header value beyond the error text) and the running reader's schema version could not be read, so the H1 vs H5 ownership fork cannot be resolved.
- No runtime telemetry was obtained for notification-service (the only other orders consumer) — only its flat lag, flat request_rate/cpu and absence of errors, and only zero config/deployment changes. Whether it is genuinely schema-tolerant or simply not assigned partition 3 is unknown because the per-consumer partition map is not exposed.
- settlement-service's last deployment timestamp was not read, so its v1 reader could not be bounded as stale-but-legitimate versus actively broken.
- ledger-db exposes no query-latency or lock/wait metric and no bucket boundary (07:41Z/07:43Z) shows deviation, but metric buckets are 120s, so a sub-2-minute ledger-db blip exactly at onset cannot be fully excluded.
- No JVM/GC or per-dependency downstream latency metrics exist in settlement-service, so whether the CPU rise is retry-loop-driven versus GC-driven is inferred rather than measured (memory_pct is flat).
- Only WARN/ERROR levels were examined for settlement-service; a rebalance or poll-timeout message at INFO level would not have been seen.
- The 07:42Z onset bucket is a 120s aggregate, so the exact sub-minute rollover was not resolved and the lag-vs-first-error ordering is only bounded to ~2 minutes.

## What would change this conclusion
- Reading the raw value of orders-3@8841207: if it carries 'quantity' + 'venue' under a v2 header, this confirms the v2-schema trigger and points to consumer-side version skew (H5); if the payload is malformed or legacy with a v2 header, H1's producer-side attribution is strengthened.
- Any record metadata showing the poison record's produced_at timestamp is after 2026-05-12T07:40:00Z, which would lock H1 to the order-service v2.16.0 deployment.
- Evidence that settlement-service already ran a v2-aware reader at 07:42Z (e.g. its last deploy/schema-registry config), which would shift ownership to order-service and break the H5 mechanism.
- A Kafka broker/consumer-group event on partition 3 of the orders topic coinciding with 07:42Z (partition reassignment, leader change, or rebalance storm), which would revive H3 and make the schema/poison record coincidental.
- Proof that notification-service consumes partition 3 of the orders topic yet stayed healthy, which would rule out a topic-wide producer/payload fault and strongly confirm the consumer-side (H5) reading.
- Evidence that the 07:35Z max.poll.records change or a Kafka session-timeout/rebalance loop produced the same poison-offset signature, which would reorder H2 upward.
- Discovery of a ledger-db latency/lock spike shorter than the 120s metric bucket at exactly 07:42Z, which would partially revive H4.

## Integrity warnings (set by code)
- fact demoted to assumption: The poison record's first-seen time 07:42:14Z is after the 07:40:00Z order-servi
- fact demoted to assumption: Two historical incidents matched the symptom pattern (HIST-112 partition reassig

## Investigation stats
- latency 211.31s, LLM calls 36, tokens 280160 (in 237098 / out 43062)
- tool calls 58 (7 failed, 0 partial, 0 recovered by retry), findings 42/42 verified
