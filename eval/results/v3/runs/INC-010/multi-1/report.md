# Incident investigation report: INC-010
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The settlement stall is caused by a schema-breaking order-service release: dep-6010 (order-service v2.16.0, deployed 2026-05-12T07:40:00Z) renamed the OrderExecuted field qty -> quantity and stamped a v2 schema header, while settlement-service remained on schema v1 and was never upgraded (F1, F33, F34). The first affected record at orders-3@8841207 fails to deserialize with the explicit v1/v2 mismatch error ~2m14s after the deploy (F8, F14), is retried as a poison message in a tight loop tens of thousands of times, blocks partition orders-3 so no offsets commit, and drives lag monotonically from ~1.5k to ~59k with zero settlements posted (F7, F9, F10). Competing hypotheses are contradicted by evidence: the poll-config change cfg-3004 cannot produce a schema error (F18), broker telemetry is flat and the other broker consumer is unaffected (F11, F12, F29), and ledger-db shows no contention through onset (F28). Confidence is medium (0.70) rather than high because the same-broker hop is verified but the same-topic/producer-id hop is permanently unverifiable and the log-signature set is truncated.

## Probable root cause
**order-service v2.16.0 schema-breaking deploy (qty->quantity) poisons settlement consumer** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.70, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 schema-breaking deploy (qty->quantity) poisons settlement consumer | bad_deployment | order-service | dep-6010 | 0.70 (medium) | active |
| 2 | H5: Schema mismatch originates from an unverified producer / registry event, not necessarily order-service v2.16.0 | data_issue | kafka (shared broker / schema path; exact producer unconfirmed) | null | 0.12 (low) | active |
| 3 | H3: Shared kafka broker / partition rebalance stalled settlement consumer | infrastructure_failure | kafka | null | 0.05 (low) | rejected |
| 4 | H4: Shared ledger-db contention stalls settlement commits | dependency_failure | ledger-db | null | 0.05 (low) | rejected |
| 5 | H2: settlement-service poll-config change (cfg-3004) caused the stall | config_change | settlement-service | cfg-3004 | 0.03 (low) | rejected |

### H1: order-service v2.16.0 schema-breaking deploy (qty->quantity) poisons settlement consumer
order-service deploy dep-6010 (07:40:00Z) renamed the OrderExecuted field qty -> quantity and stamped schema header v2 without keeping the legacy field. settlement-service, still on schema v1 (never upgraded; F34), fails to deserialize the first affected OrderExecuted record at orders-3@8841207 ('missing required field qty (schema v1 expected, header says v2)', 07:42:14Z). The record is treated as a poison message and retried in a tight loop (tens of thousands of attempts), blocking partition orders-3, so no offsets commit, lag grows monotonically (~1.5k -> ~59k) and zero settlements post. The origin of the fault is the schema-breaking code release, not the consumer's poll settings.

**Causal chain**
1. dep-6010 deploys order-service v2.16.0 renaming OrderExecuted qty->quantity, adding field venue, and adding a v2 schema header; it is the only deployment across all services between 06:00-09:59Z and is still live with no rollback [F1, F13, F33]
2. The only other change near onset is the settlement-service poll-config change cfg-3004 (07:35Z), which reduces batch size 500->200 and cannot produce a schema error; no broker/order-service/kafka config or flag change exists in the window, so the schema change reaches the broker only through the code deploy [F2, F3, F16, F35, F18]
3. order-service has a queue-produce edge to the same shared kafka component that settlement-service consumes from (queue-consume), and both write the shared ledger-db; the dependency graph only resolves to the 'kafka' component node, not per-topic nodes, so order-service and settlement-service are confirmed to share the broker but not confirmed to share the exact topic orders-3 [F5, F15, F19, F20, F23]
4. settlement-service's consumer receives an OrderExecuted record with header v2 but no qty field and fails deserialization at 07:42:14Z, ~2m14s after the deploy and ~7m14s after the poll change; settlement-service was never upgraded to schema v2 [F8, F14, F34]
5. The record becomes a poison message retried in a tight loop on partition orders-3@8841207 (attempt counters in the tens of thousands), blocking the partition and preventing offset commits [F8, F9]
6. No offsets commit -> consumer lag rises monotonically from ~1572 to ~59267 (max 61605) and zero settlements post [F7]
7. settlement-service CPU doubles from ~28% to ~70% at the same onset, consistent with the retry loop rather than an external load spike [F10]

**Supporting evidence**
  - `F1` ✔ Exactly one deployment occurred across all services in 2026-05-12T06:00Z–09:59Z: dep-6010, order-service v2.16.0, deployed 2026-05-12T07:40:00Z by a.khan, with changes 'OrderExecuted event: rename field qty -> quantity', 'add field venue', 'event schema version header v2'; status live, rolled_back_at null.
  - `F13` ✔ Only two changes occurred near the ~07:40-07:42Z onset: order-service deployment dep-6010 (v2.16.0, 07:40:00Z, a.khan) renaming OrderExecuted qty->quantity and adding a schema v2 header, and settlement-service config cfg-3004 (07:35:00Z, m.das) consumer.max.poll.records 500->200. These are the only entries in their respective windows across all services.
  - `F14` ✔ Temporal proximity: the order-service v2.16.0 deploy at 07:40:00Z is ~2m14s before the first settlement error (deserialization failure at 07:42:14Z), whereas the poll-config change at 07:35:00Z precedes that error by ~7m14s.
  - `F5` ✔ order-service writes to the same shared kafka (edge queue-produce to kafka) and also to shared ledger-db, so the 07:40Z order-service v2.16.0 event-schema change touches components shared with settlement-service's consumption path and DB.
  - `F15` ✔ The order-service deploy touches a component shared with settlement-service's path: order-service has a queue-produce edge to the same shared kafka that settlement-service consumes (queue-consume), and both services write to shared ledger-db (owner platform, shared=true). So the schema change is on the shared broker carrying the events settlement-service must deserialize.
  - `F16` ✔ No order-service or kafka/broker config/flag change occurred in 06:00-07:45Z (count 0), so the only order-service-side change is the 07:40Z code deployment; the schema/field rename reaches the broker only through dep-6010, not through a config flag.
  - `F8` ✔ Earliest settlement-service error is a deserialization failure at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. All 409 error logs in the window are ERROR level; the two dominant patterns are this deserialize failure (213 occurrences) and 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first seen 07:44:51Z).
  - `F9` ✔ The poison-message retries all target the same stuck offset orders-3@8841207 with attempt counters in the tens of thousands (e.g. attempt 92490, 82363, 89594), indicating a single partition is blocked and being retried in a tight loop rather than advancing.
  - `F7` ✔ settlement-service consumer_lag is flat and healthy (~38-48 messages) until 07:30Z, then jumps in the 07:40-07:50 bucket (avg 1572 -> 5930) and rises monotonically to avg 59267 / max 61605 by 09:50Z. Onset is between 07:30Z and 07:40Z.
  - `F10` ✔ settlement-service CPU more than doubles at the same onset: avg ~27-28% throughout 06:00-07:30Z, then ~60.8% in the 07:40 bucket and steady ~69-71% through 09:50Z, consistent with the retry loop burning CPU rather than an external load spike.
  - `F11` ✔ The shared Kafka broker looks healthy and NOT overloaded: kafka cpu_pct stays flat at ~34-35% across the entire 06:00-09:50Z window (no change at the 07:40 onset), and available broker metrics show only cpu_pct, messages_in_per_s, under_replicated_partitions. This argues against a shared broker capacity fault.
  - `F12` ✔ notification-service, another consumer, shows NO lag growth over the same window: its consumer_lag stays in a flat ~51-67 band the whole time (07:30 avg 51.92, 07:40 avg 64.06, 09:50 avg 56.45), so the fault is not shared across all consumers on the broker family as far as this consumer is concerned.
  - `F33` ✔ dep-6010 is still live: the order-service deployment query returns exactly one entry, dep-6010 v2.16.0 deployed 2026-05-12T07:40:00Z by a.khan, with "status":"live" and "rolled_back_at":null.
  - `F34` ✔ settlement-service was never upgraded/bootstrapped to schema v2: get_deployments scoped to settlement-service over 07:00–09:59Z returns count 0 with an empty list.
  - `F35` ✔ The only settlement-service config change in 07:00–09:59Z remains cfg-3004 (consumer.max.poll.records 500->200, changed_at 2026-05-12T07:35:00Z, author m.das) — there is no later entry reverting it or introducing a schema-v2 consumer setting.
  - `F25` ✔ Probe 1: In 2026-05-12T07:30Z-08:10Z, settlement-service produced 81 matched WARN+ logs and ALL 81 were ERROR level (counts_by_level shows ERROR:81, no WARN entries at all). The returned page (40 of 81, truncated) contains exactly TWO signature patterns: 'failed to deserialize OrderExecuted ... missing required field 'qty' (schema v1 expected, header says v2)' (43 of page) first seen 07:42:14Z, and 'poison message: retrying ... (attempt <n>), partition blocked' (38 of page) first seen 07:44:51Z. No other signature family appears in this window.
**Contradicting evidence**
  - `F2` ✔ The reported settlement-service consumer poll config changed exactly once: cfg-3004, key consumer.max.poll.records, old_value 500 -> new_value 200, changed_at 2026-05-12T07:35:00Z, author m.das, description 'Smaller poll batches to smooth DB writes'.
  - `F26` ✔ Caveat on the completeness claim: the 07:30Z-08:10Z settlement-service log result is truncated=true (returned 40 of 81 total, first_match 07:42:14Z, last_match 08:08:53Z), so the deserialization + poison-message patterns are the only ones present IN THE RETURNED PAGE, not proven to be the only ones in the full window. The specific targeted searches for rebalance/assignment-loss ('rebalance') and offset-commit ('commit') signatures could NOT be executed (budget exhausted), so absence of a rebalance/commit-failure signature is NOT established by the data collected.
**Assumptions**
  - The events settlement-service consumes are produced by order-service: the graph only resolves to the shared 'kafka' component, and get_service_dependencies for settlement-service reached depth 2 with no producer edge enumerated, so the producer of the consumed topic is not directly verified.
  - The v2-schema record at orders-3@8841207 was actually produced by order-service v2.16.0; no producer-id / record-header metadata for that offset was retrieved (tc-047 budget_exhausted) and no schema-registry event evidences the v2 header's origin.
  - A single undeserializable record is sufficient to block partition orders-3 and prevent commits for all later records, matching the observed tight retry loop.
  - The absence of rebalance/assignment-loss or offset-commit-failure signatures is inferred from a truncated result (40 of 81 returned, 40 of 409 matched ERROR entries) with a WARN+ filter that could suppress consumer-group WARN signatures; competing signatures are not positively excluded.

### H5: Schema mismatch originates from an unverified producer / registry event, not necessarily order-service v2.16.0
The observed failure is a schema-version mismatch: a record on the consumed path carries header v2 while settlement-service deserializes as v1. dep-6010 is only the most plausible source of that v2 header. An alternate origin exists: the v2-format record could be injected by a different producer (e.g. payment-service or another producer on the shared kafka), or a schema-registry/topic-level change not captured by the deployment/config queries could have re-labelled records. The fix would then be on the producer/registry side or a consumer schema upgrade, not rolling back dep-6010.

**Causal chain**
1. A record with schema header v2 and no qty field reaches settlement-service's consumed path [F8]
2. settlement-service is still on schema v1 and cannot deserialize it, blocking the partition and growing lag [F8, F9, F7, F34]
3. The v2 header's origin is not verified: the graph exposes only the 'kafka' component (no per-topic nodes), the producer-id/header metadata for orders-3@8841207 was never retrieved (budget exhausted), and no schema-registry change record exists in the window, so any producer on the shared broker could in principle be the source [F21, F3, F19, F20]

**Supporting evidence**
  - `F8` ✔ Earliest settlement-service error is a deserialization failure at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. All 409 error logs in the window are ERROR level; the two dominant patterns are this deserialize failure (213 occurrences) and 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first seen 07:44:51Z).
  - `F9` ✔ The poison-message retries all target the same stuck offset orders-3@8841207 with attempt counters in the tens of thousands (e.g. attempt 92490, 82363, 89594), indicating a single partition is blocked and being retried in a tight loop rather than advancing.
  - `F34` ✔ settlement-service was never upgraded/bootstrapped to schema v2: get_deployments scoped to settlement-service over 07:00–09:59Z returns count 0 with an empty list.
  - `F21` ✘ unverified The graph exposes only the component 'kafka', not per-topic nodes: get_service_dependencies with service='orders-3' returned ok:false budget_exhausted before it could be evaluated, so no topic-level node (e.g. orders-3) and no producer-id / record-header metadata for orders-3@8841207 was retrieved.
  - `F3` ✔ Config-change queries scoped to kafka and to order-service for 06:00–09:59Z both returned count 0, i.e. no broker/middleware or order-service config/flag changes in the window.
**Contradicting evidence**
  - `F1` ✔ Exactly one deployment occurred across all services in 2026-05-12T06:00Z–09:59Z: dep-6010, order-service v2.16.0, deployed 2026-05-12T07:40:00Z by a.khan, with changes 'OrderExecuted event: rename field qty -> quantity', 'add field venue', 'event schema version header v2'; status live, rolled_back_at null.
  - `F14` ✔ Temporal proximity: the order-service v2.16.0 deploy at 07:40:00Z is ~2m14s before the first settlement error (deserialization failure at 07:42:14Z), whereas the poll-config change at 07:35:00Z precedes that error by ~7m14s.
  - `F33` ✔ dep-6010 is still live: the order-service deployment query returns exactly one entry, dep-6010 v2.16.0 deployed 2026-05-12T07:40:00Z by a.khan, with "status":"live" and "rolled_back_at":null.
**Assumptions**
  - An unidentified producer or registry action produced the v2 record; no direct evidence of such a producer exists, only the absence of a verified producer edge for orders-3.
  - The registry/credential config-change gap (no query was run for schema-registry components) could hide a registry event.

### H3: Shared kafka broker / partition rebalance stalled settlement consumer
A broker-side event (rebalance, partition reassignment, maintenance) on the shared kafka cluster caused settlement-service to lose its partition assignment, stalling consumption and growing lag, mirroring HIST-112.

**Causal chain**
1. settlement-service consumes from the shared kafka component [F4]
2. a broker rebalance/reassignment stalls the consumer and it stops advancing offsets *(assumed)*
3. lag grows monotonically and zero settlements post [F7]

**Supporting evidence**
  - `F4` ✔ settlement-service has only two outgoing edges: queue-consume to kafka and db to ledger-db; both kafka and ledger-db are marked shared=true (owner platform), while settlement-service itself is shared=false.
  - `F6` ✔ Historical incidents include HIST-112 (2026-02-03, 'Settlement consumer stalled after partition reassignment', services settlement-service+kafka, symptoms 'consumer lag growing','settlements stuck', root cause consumer lost partition assignment during broker maintenance, resolution 'Restarted consumer group', match_score 5) and HIST-106 (2025-06-30, settlement-service mTLS certificate expired), plus HIST-111 (reporting job full table scans slowed leder-db, services ledger-db+settlement-service) — the same symptoms have previously had an infrastructure/rebalance cause, not a poll-config cause.
  - `F17` ✔ Historical precedents for these exact symptoms point away from a poll-config cause: HIST-112 (settlement consumer stalled, root cause 'Consumer lost partition assignment during broker maintenance') and HIST-111 (ledger-db slowdown affecting settlement-service) are leads only, and the top matches for 'consumer max.poll.records poll config change settlement' are the same infrastructure/rebalance and shared-DB incidents rather than a poll-tuning incident.
**Contradicting evidence**
  - `F11` ✔ The shared Kafka broker looks healthy and NOT overloaded: kafka cpu_pct stays flat at ~34-35% across the entire 06:00-09:50Z window (no change at the 07:40 onset), and available broker metrics show only cpu_pct, messages_in_per_s, under_replicated_partitions. This argues against a shared broker capacity fault.
  - `F12` ✔ notification-service, another consumer, shows NO lag growth over the same window: its consumer_lag stays in a flat ~51-67 band the whole time (07:30 avg 51.92, 07:40 avg 64.06, 09:50 avg 56.45), so the fault is not shared across all consumers on the broker family as far as this consumer is concerned.
  - `F8` ✔ Earliest settlement-service error is a deserialization failure at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. All 409 error logs in the window are ERROR level; the two dominant patterns are this deserialize failure (213 occurrences) and 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first seen 07:44:51Z).
  - `F9` ✔ The poison-message retries all target the same stuck offset orders-3@8841207 with attempt counters in the tens of thousands (e.g. attempt 92490, 82363, 89594), indicating a single partition is blocked and being retried in a tight loop rather than advancing.
  - `F10` ✔ settlement-service CPU more than doubles at the same onset: avg ~27-28% throughout 06:00-07:30Z, then ~60.8% in the 07:40 bucket and steady ~69-71% through 09:50Z, consistent with the retry loop burning CPU rather than an external load spike.
  - `F29` ✔ Broker-side degradation is absent across 07:30-08:00Z: kafka under_replicated_partitions is 0.0 for every one of the 31 points (min 0.0, max 0.0, mean 0.0, missing_points 0) with no change at 07:40Z; and messages_in_per_s stays steady in a 2226-2734 msg/s band (07:40 avg 2529.26, 07:42 avg 2605.53, mean 2527.24) with no dip at onset.
  - `F30` ✔ Both probes' shared-infrastructure legs return flat, healthy telemetry through the 07:40Z onset (kafka under_replicated_partitions=0, messages_in_per_s stable; ledger-db slow_queries/connections/cpu flat), which does NOT support H4 as an active co-factor via broker replication/throughput or DB contention in this window.
**Assumptions**
  - A rebalance occurred near 07:40Z despite no telemetry confirming it; the actual error signature would be assignment/commit errors, not deserialization.
**Rejected because:** No rebalance/assignment-loss or offset-commit error signature appears in the window, and the only observed signatures are deserialization failure and poison-message retries on one specific offset (F8, F9), which a rebalance would not produce. The broker is healthy: cpu_pct flat ~34-35% with no onset change (F11), under_replicated_partitions 0.0 for all 31 points and messages_in_per_s steady (F29), and notification-service on the same broker family shows no lag growth (F12). HIST-112 is a past-incident lead only and cannot carry a live hypothesis. Partially tempered by the critique: the absence of a rebalance signature is only weakly established (truncated log page, WARN+ filter, F26), but the positive, mechanism-matching deserialization evidence outweighs it.

### H4: Shared ledger-db contention stalls settlement commits
The shared ledger-db (four writer services including order-service) became slow/contended, so settlement-service commits block and lag grows, with settlement as a symptom of a shared dependency bottleneck.

**Causal chain**
1. settlement-service writes to the shared ledger-db, which is also written by order-service, portfolio-service and payment-service [F4, F5, F23]
2. ledger-db becomes slow/contended and settlement commits block *(assumed)*
3. lag grows monotonically and zero settlements post [F7]

**Supporting evidence**
  - `F4` ✔ settlement-service has only two outgoing edges: queue-consume to kafka and db to ledger-db; both kafka and ledger-db are marked shared=true (owner platform), while settlement-service itself is shared=false.
  - `F5` ✔ order-service writes to the same shared kafka (edge queue-produce to kafka) and also to shared ledger-db, so the 07:40Z order-service v2.16.0 event-schema change touches components shared with settlement-service's consumption path and DB.
  - `F23` ✔ ledger-db (owner platform, shared=true) has exactly four writers: order-service, portfolio-service, payment-service and settlement-service (all 'db' edges), so order-service and settlement-service share ledger-db in addition to the shared kafka.
**Contradicting evidence**
  - `F8` ✔ Earliest settlement-service error is a deserialization failure at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. All 409 error logs in the window are ERROR level; the two dominant patterns are this deserialize failure (213 occurrences) and 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first seen 07:44:51Z).
  - `F9` ✔ The poison-message retries all target the same stuck offset orders-3@8841207 with attempt counters in the tens of thousands (e.g. attempt 92490, 82363, 89594), indicating a single partition is blocked and being retried in a tight loop rather than advancing.
  - `F10` ✔ settlement-service CPU more than doubles at the same onset: avg ~27-28% throughout 06:00-07:30Z, then ~60.8% in the 07:40 bucket and steady ~69-71% through 09:50Z, consistent with the retry loop burning CPU rather than an external load spike.
  - `F12` ✔ notification-service, another consumer, shows NO lag growth over the same window: its consumer_lag stays in a flat ~51-67 band the whole time (07:30 avg 51.92, 07:40 avg 64.06, 09:50 avg 56.45), so the fault is not shared across all consumers on the broker family as far as this consumer is concerned.
  - `F28` ✔ ledger-db shows NO contention at the 07:40Z onset (07:30-08:00Z): slow_queries_per_min stays flat in a 0.4-1.56 band (07:30 avg 0.97, 07:40 avg 1.02, 08:00 avg 1.01; mean 0.98, min 0.4, max 1.56, missing_points 0); connections_active is flat ~58-64 (07:30 avg 61.2, 07:40 avg 62.41, mean 61.3, max 66.84); and cpu_pct is flat ~28-32 (07:30 avg 30.03, 07:42 avg 30.95, mean 29.9). No step change coincides with 07:40Z.
  - `F27` ✔ ledger-db exposes NO latency_p99_ms and NO error_rate metric; its available_metrics are only ['connections_active','cpu_pct','disk_used_pct','replication_lag_ms','slow_queries_per_min']. (Queried latency_p99_ms and error_rate -> not_found.)
**Assumptions**
  - A latency-only DB symptom would be invisible because ledger-db exposes no latency_p99_ms or error_rate metric (F27); DB contention is possible but unobserved.
**Rejected because:** The visible failure is a schema/deserialization error on a specific offset (F8) and a tight poison-message retry loop (F9, F10), not commit failures or DB latency errors. ledger-db telemetry that IS available is flat through the 07:40Z onset: slow_queries_per_min 0.4-1.56 with no step change, connections_active ~58-64, cpu_pct ~28-32 (F28), and notification-service (not a DB writer on this path) is unaffected (F12). Residual caveat per critique: ledger-db exposes no latency_p99_ms or error_rate (F27) and replication_lag_ms/disk_used_pct were never queried, so a purely latency-only DB co-factor is not positively excluded — but it cannot explain the schema-specific error text.

### H2: settlement-service poll-config change (cfg-3004) caused the stall
The 07:35:00Z change to consumer.max.poll.records 500->200 on settlement-service altered consumer behavior in a way that led to the stuck partition / lag growth the reporter attributes to poll settings.

**Causal chain**
1. cfg-3004 changes settlement-service consumer.max.poll.records 500->200 at 07:35:00Z [F2, F13]
2. the changed poll behavior causes the consumer to stop committing offsets / block a partition *(assumed)*
3. lag grows monotonically and zero settlements post [F7]

**Supporting evidence**
  - `F2` ✔ The reported settlement-service consumer poll config changed exactly once: cfg-3004, key consumer.max.poll.records, old_value 500 -> new_value 200, changed_at 2026-05-12T07:35:00Z, author m.das, description 'Smaller poll batches to smooth DB writes'.
  - `F13` ✔ Only two changes occurred near the ~07:40-07:42Z onset: order-service deployment dep-6010 (v2.16.0, 07:40:00Z, a.khan) renaming OrderExecuted qty->quantity and adding a schema v2 header, and settlement-service config cfg-3004 (07:35:00Z, m.das) consumer.max.poll.records 500->200. These are the only entries in their respective windows across all services.
**Contradicting evidence**
  - `F8` ✔ Earliest settlement-service error is a deserialization failure at 07:42:14Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. All 409 error logs in the window are ERROR level; the two dominant patterns are this deserialize failure (213 occurrences) and 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences, first seen 07:44:51Z).
  - `F9` ✔ The poison-message retries all target the same stuck offset orders-3@8841207 with attempt counters in the tens of thousands (e.g. attempt 92490, 82363, 89594), indicating a single partition is blocked and being retried in a tight loop rather than advancing.
  - `F18` ✔ The poll-config change alone does not fit the observed failure shape: cfg-3004 only reduces batch size (500->200), which cannot produce a schema/deserialization error or a single blocked offset retried tens of thousands of times; and it is settlement-service-only, so it cannot produce any cross-service pattern.
  - `F14` ✔ Temporal proximity: the order-service v2.16.0 deploy at 07:40:00Z is ~2m14s before the first settlement error (deserialization failure at 07:42:14Z), whereas the poll-config change at 07:35:00Z precedes that error by ~7m14s.
  - `F12` ✔ notification-service, another consumer, shows NO lag growth over the same window: its consumer_lag stays in a flat ~51-67 band the whole time (07:30 avg 51.92, 07:40 avg 64.06, 09:50 avg 56.45), so the fault is not shared across all consumers on the broker family as far as this consumer is concerned.
  - `F17` ✔ Historical precedents for these exact symptoms point away from a poll-config cause: HIST-112 (settlement consumer stalled, root cause 'Consumer lost partition assignment during broker maintenance') and HIST-111 (ledger-db slowdown affecting settlement-service) are leads only, and the top matches for 'consumer max.poll.records poll config change settlement' are the same infrastructure/rebalance and shared-DB incidents rather than a poll-tuning incident.
  - `F25` ✔ Probe 1: In 2026-05-12T07:30Z-08:10Z, settlement-service produced 81 matched WARN+ logs and ALL 81 were ERROR level (counts_by_level shows ERROR:81, no WARN entries at all). The returned page (40 of 81, truncated) contains exactly TWO signature patterns: 'failed to deserialize OrderExecuted ... missing required field 'qty' (schema v1 expected, header says v2)' (43 of page) first seen 07:42:14Z, and 'poison message: retrying ... (attempt <n>), partition blocked' (38 of page) first seen 07:44:51Z. No other signature family appears in this window.
  - `F10` ✔ settlement-service CPU more than doubles at the same onset: avg ~27-28% throughout 06:00-07:30Z, then ~60.8% in the 07:40 bucket and steady ~69-71% through 09:50Z, consistent with the retry loop burning CPU rather than an external load spike.
**Assumptions**
  - A smaller max.poll.records could stall a consumer; not supported by any error signature.
**Rejected because:** cfg-3004 only reduces batch size (500->200); it cannot produce a schema/deserialization error or a single offset retried tens of thousands of times (F18), and it is settlement-service-only so it cannot produce any cross-service artifact. Every observed error signature (deserialization + poison-message, F25) belongs to a schema-mismatch mechanism rather than batching. The poll change precedes the first error by ~7m14s vs the deploy's ~2m14s, but this timing argument is only weak corroboration: the stall onset was never independently measured and the onset bucket is coarse (F7 onset between 07:30 and 07:40Z).

## Facts (verified observations)
- Exactly one deployment occurred across all services in 06:00-09:59Z: dep-6010, order-service v2.16.0, deployed 2026-05-12T07:40:00Z, with changes 'OrderExecuted event: rename field qty -> quantity', 'add field venue', 'event schema version header v2'. [F1, F13]
- dep-6010 remains live with rolled_back_at null, and no deployment or config change of any kind occurred after 07:42Z in the incident window (no rollback, no mitigation release). [F33, F31, F32]
- settlement-service was never upgraded to schema v2: deployments scoped to settlement-service over 07:00-09:59Z return count 0. [F34]
- The earliest settlement-service error at 07:42:14Z is a deserialization failure on orders-3@8841207: "missing required field 'qty' (schema v1 expected, header says v2)". [F8, F25]
- The deploy at 07:40:00Z precedes the first error by ~2m14s, versus ~7m14s for the poll-config change at 07:35:00Z. [F14, F1, F2]
- Poison-message retries all target the same offset orders-3@8841207 with attempt counters in the tens of thousands, indicating a single blocked partition retried in a tight loop rather than advancing. [F9, F8]
- settlement-service consumer_lag was healthy (~38-48) until 07:30Z, rose from the 07:40-07:50 bucket onward and climbed monotonically to avg 59267 / max 61605 by 09:50Z; onset falls between 07:30Z and 07:40Z. [F7]
- settlement-service CPU more than doubled at the same onset (avg ~27-28% through 06:00-07:30Z to ~69-71% by 09:50Z), consistent with a retry loop burning CPU rather than external load. [F10]
- The only other settlement-service config change in the window is cfg-3004 (consumer.max.poll.records 500->200, 07:35:00Z); it was never reverted and no schema-v2 consumer setting was introduced. [F2, F35]
- The poll-config change cannot produce the observed failure shape: it only reduces batch size and is settlement-service-only, so it cannot generate a schema/deserialization error or a cross-service artifact. [F2, F25]
- order-service produces to the same shared kafka that settlement-service consumes, and both write to shared ledger-db; only two changes exist near onset, dep-6010 and cfg-3004. [F5, F15, F19, F20, F13]
- The broker looks healthy and not overloaded through onset: cpu_pct flat ~34-35%, under_replicated_partitions 0.0 for all 31 points in 07:30-08:00Z, messages_in_per_s steady (~2226-2734 msg/s). [F11, F29]
- notification-service, the other consumer on the shared broker, shows no lag growth over the same window (flat ~51-67 band), so the fault is not shared across all consumers. [F12, F22]
- ledger-db shows no contention at the 07:40Z onset: slow_queries_per_min flat in a 0.4-1.56 band, connections_active ~58-64, cpu_pct ~28-32, with no step change. [F28]
- No kafka/broker or order-service config/flag change occurred in the window (count 0 for 06:00-07:45Z and for kafka scope), so the schema change reaches the broker only through code deployment dep-6010. [F3, F16]
- The 07:30-08:10Z settlement-service log page contains exactly two signature patterns (deserialization failure and poison-message retries), but is truncated (40 of 81 returned), so exclusivity of the signature set is not proven. [F25, F26]
- ledger-db exposes no latency_p99_ms and no error_rate metric, and kafka exposes only cpu_pct, messages_in_per_s and under_replicated_partitions. [F27, F11]
- Historical incidents with identical symptoms (HIST-112 consumer lost partition assignment during broker maintenance; HIST-106 mTLS cert expiry; HIST-111 ledger-db slowdown affecting settlement) are leads only and no evidence in this window matches their mechanisms. [F6]

## Assumptions (not verified)
- The events settlement-service consumes are produced by order-service. The graph only resolves to the shared 'kafka' component and no producer edge for the consumed topic was enumerated, so the producer relationship is inferred from order-service's queue-produce edge and settlement-service's queue-consume edge landing on the same shared broker node (F19, F20) rather than directly verified.
- The v2-schema record at orders-3@8841207 was actually produced by order-service v2.16.0. No producer-id or record-header metadata for that offset was retrieved (F21) and no schema-registry event evidences the v2 header's origin.
- A single undeserializable record is sufficient to block partition orders-3 and prevent offset commits for all later records, matching the observed tight retry loop.
- The absence of rebalance/assignment-loss and offset-commit-failure signatures is inferred from a truncated result (40 of 81 returned) with a WARN+ filter; competing signatures are not positively excluded (F26).
- order-service and settlement-service touch the same topic (orders-3), not merely the same Kafka cluster; topic-level topology was permanently unavailable (F21).
- No out-of-band mitigation (manual consumer restart, DLQ skip, offset reset) occurred, since no deployment or config change was recorded after 07:42Z (F31, F32); such an action would be invisible to the deployment/config queries.
- The stall onset is between 07:30Z and 07:40Z per lag bucketing (F7); a finer onset time was not independently measured.
- (demoted from facts: no verified observation backs it) The dependency graph exposes a single node named 'kafka' with no per-topic nodes, and the topic-level query for orders-3 returned budget_exhausted, so no producer-id or record-header metadata for orders-3@8841207 was retrieved.

## Timeline
- `2026-05-12T06:00:00Z` Baseline healthy: settlement-service consumer_lag ~38-48, CPU ~27-28%, kafka cpu flat ~34-35%, ledger-db slow_queries/connections/cpu flat. [F7, F10, F11, F28]
- `2026-05-12T07:30:00Z` Last healthy lag bucket; onset falls between this bucket and the next (07:40-07:50). [F7]
- `2026-05-12T07:35:00Z` settlement-service config change cfg-3004 applied: consumer.max.poll.records 500 -> 200 by m.das (the only settlement-service config change; never reverted). [F2, F35]
- `2026-05-12T07:40:00Z` order-service deployment dep-6010 (v2.16.0) by a.khan goes live: OrderExecuted field renamed qty -> quantity, venue field added, event schema version header v2. Only deployment across all services in the window. [F1, F13, F33]
- `2026-05-12T07:40:00Z` Same onset: settlement-service consumer_lag jumps (avg 1572 -> 5930), CPU doubles to ~60.8%; kafka and ledger-db telemetry remain flat. [F7, F10, F11, F29, F28]
- `2026-05-12T07:42:14Z` First settlement-service error: deserialization failure at orders-3@8841207 - "missing required field 'qty' (schema v1 expected, header says v2)". [F8, F25]
- `2026-05-12T07:44:51Z` Poison-message retry loop begins on the same offset: 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked', attempt counters reach tens of thousands. [F9, F25]
- `2026-05-12T07:42:00Z` Through 09:59Z: no deployment and no config change of any kind across all services - dep-6010 not rolled back, cfg-3004 not reverted, no broker action. [F31, F32, F36, F35]
- `2026-05-12T09:50:00Z` settlement-service lag at avg 59267 / max 61605; CPU steady ~69-71%; zero settlements posted; notification-service lag flat ~51-67 throughout. [F7, F10, F12]

## Recommended actions
- **[immediate]** Roll back dep-6010 (order-service v2.16.0) to the previous order-service release that emits the legacy 'qty' field, or re-emit OrderExecuted with both 'qty' (legacy) and 'quantity' plus a compatible schema header, so the v1 settlement-service consumer can deserialize the blocked record. -- dep-6010 is the only change in the window that can produce the observed v1/v2 schema error (F1, F13, F16), it is still live with no rollback (F33, F31), and the mechanism is the positively observed deserialization mismatch on orders-3@8841207 (F8). [F1, F33, F8, F31, F34]
- **[immediate]** After the producer fix, confirm recovery by verifying that offset orders-3@8841207 deserializes, the poison-message retry loop stops, offsets commit, consumer_lag returns to baseline (~40-70) and settlement CPU drops back to ~27-28%. If the blocked record cannot be replayed, reset/skip that offset under change control as a one-off. -- Recovery is only proven if the blocked offset clears and lag drains; the current state is lag ~59k with the offset retried tens of thousands of times and no commits (F9, F7, F10). [F9, F7, F10]
- **[immediate]** Do NOT revert cfg-3004 (consumer.max.poll.records) as a remediation; it is not the cause and reverting it adds an unrelated variable during recovery. -- cfg-3004 only reduces batch size and cannot produce a schema deserialization error or a single blocked offset retried tens of thousands of times (F18, F25). [F18, F25, F2]
- **[short_term]** Upgrade settlement-service to the v2 schema (support 'quantity'/venue and the v2 header) and add forward/backward-compatible handling: unknown-field tolerance and a dead-letter path for undeserializable records instead of a tight infinite retry loop on one offset. -- settlement-service was never upgraded to v2 (F34) and the absence of a DLQ/skip path is what turned one bad record into a full partition block and multi-hour outage (F8, F9). [F34, F8, F9]
- **[long_term]** Release-coordinate producer schema changes: require backward-compatible evolution (keep legacy fields through at least one consumer upgrade cycle) and gated rollout of schema-version headers, with a pre-deploy check that all consumers of the shared kafka are on the new schema version. -- A single schema-breaking release (qty->quantity + v2 header) with no consumer upgrade path took down settlement with zero settlements for over two hours (F1, F34, F7). [F1, F34, F7]
- **[long_term]** Add alerting that distinguishes poison-message/deserialization failures and per-partition offset commit stalls from generic lag, and alert on repeated retries of the same offset. -- The failure mode (one offset retried tens of thousands of times, partition blocked) was visible in logs at 07:42-07:44Z but the incident ran ~1h40m before the report (F9, F8, F25). [F9, F8, F25]
- **[long_term]** Instrument schema-registry/header provenance and per-topic producer metadata in the topology and observability tooling so a v2 header's origin can be attributed directly rather than inferred from the deploy summary. -- The causal chain's producer/topic hop could not be verified: the graph exposes only a 'kafka' node and the orders-3 metadata query failed with budget_exhausted (F21, F19, F20). [F21, F19, F20]

## Data gaps
- Same-topic confirmation is unavailable: the dependency graph exposes only a single 'kafka' component node with no per-topic nodes, and the orders-3 topic query returned budget_exhausted, so it cannot be confirmed that order-service and settlement-service touch the exact same topic rather than the same cluster (F21).
- No producer-id or record-header metadata for orders-3@8841207 was retrieved, so the v2 header's producer is inferred from dep-6010's summary, not directly observed.
- Log-signature exclusivity is unproven: the 07:30-08:10Z log result was truncated (40 of 81 returned) with a WARN+ filter, and targeted searches for 'rebalance' and 'commit' signatures failed on budget, so rebalance/assignment-loss or commit-failure signatures in the unreturned entries are not positively excluded (F26).
- No schema-registry, consumer-group assignment state, or topic-partition tool exists in the toolkit; config-change queries for registry/credential components were never run, so an out-of-band registry or credential change near 07:35-07:40Z cannot be ruled in or out.
- ledger-db exposes no latency_p99_ms or error_rate and replication_lag_ms/disk_used_pct were never queried, so a latency-only DB co-factor remains unverified (not excluded); likewise settlement-service memory and other broker metrics were unqueried.
- No deployment or config record can reveal an out-of-band mitigation (manual consumer restart, offset reset, DLQ skip); the claim that nothing cleared rests on the absence of recorded interventions rather than a fresh post-08:00Z error-count query.

## What would change this conclusion
- Evidence that the v2-header record at orders-3@8841207 was produced by a service other than order-service, or that decoupling order-service's schema change (e.g. rolling back dep-6010) leaves the deserialization failure and lag growth unchanged.
- Observation that the events consumed by settlement-service are produced by a different producer on the shared broker (e.g. payment-service) rather than order-service, which would replace H1 with H5 as the causal origin.
- Recovery without any producer-side change: if the stall cleared with dep-6010 still live and settlement on schema v1, the deserialization-mismatch mechanism would not explain the outage.
- A consumer-group rebalance/partition-assignment-loss or offset-commit-failure signature found in the unreturned settlement-service logs, or a broker-side rebalance/partition event near 07:40Z, which would support H3 and weaken the poison-record explanation.
- Evidence of ledger-db latency or error-rate degradation coinciding with onset (metrics not currently exposed) that would make H4 an active co-factor.
- Proof that cfg-3004 (max.poll.records 500->200) alone, without any schema mismatch, blocks a partition or blocks offset commits, which would raise H2.
- Evidence that the affected record never actually carried the legacy 'qty' field mismatch - i.e. that the error text was misattributed and the failure arises from a different mechanism.

## Integrity warnings (set by code)
- fact demoted to assumption: The dependency graph exposes a single node named 'kafka' with no per-topic nodes

## Investigation stats
- latency 176.62s, LLM calls 27, tokens 194606 (in 160105 / out 34501)
- tool calls 47 (6 failed, 0 partial, 0 recovered by retry), findings 35/36 verified
