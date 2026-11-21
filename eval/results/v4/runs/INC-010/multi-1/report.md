# Incident investigation report: INC-010
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The settlement stall is best explained by a schema-contract break introduced by order-service v2.16.0 (dep-6010, 07:40Z), which renamed the OrderExecuted field qty→quantity and emitted a schema v2 header while settlement-service's consumer still expects schema v1; the first v2-shaped message on orders-3 (@8841207) failed to deserialize at 07:42:14Z, was retried as a poison message, and permanently blocked the partition, so lag climbed monotonically to 60k+ and throughput collapsed. Confidence is medium (0.72 per the reviewed ranking): the failing log line, the 07:40Z deploy, and the lag/request_rate onset are tightly coincident, and the only alternative with a real change (the 07:35Z poll-batch setting cfg-3004) cannot mechanically produce schema errors. The residual uncertainty is confined to a hop no available tool can verify — no explicit topic→partition mapping and no produce-side timestamp — so the message's producer identity is inferred from the exact field-rename match and the deploy→first-error ordering rather than proven. Recommended path: keep the poison offset from blocking the partition (DLQ/skip or roll the producer back), then align the consumer contract to schema v2.

## Probable root cause
**order-service v2.16.0 schema rename (qty→quantity) breaks settlement-service deserialization; poison message blocks orders-3** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.72, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 schema rename (qty→quantity) breaks settlement-service deserialization; poison message blocks orders-3 | bad_deployment | order-service | dep-6010 | 0.72 (medium) | active |
| 2 | H2: settlement-service consumer poll-settings change (max.poll.records 500→200) stalled consumption | config_change | settlement-service | cfg-3004 | 0.10 (low) | rejected |
| 3 | H3: Shared kafka broker partition-assignment loss / rebalancing storm stalls settlement consumer | infrastructure_failure | kafka broker | null | 0.08 (low) | rejected |
| 4 | H4: Shared ledger-db or broker contention throttles settlement-service | resource_exhaustion | ledger-db | null | 0.05 (low) | rejected |

### H1: order-service v2.16.0 schema rename (qty→quantity) breaks settlement-service deserialization; poison message blocks orders-3
The order-service v2.16.0 deploy (dep-6010, 07:40Z) changed the OrderExecuted event contract: it renamed field `qty` to `quantity` and began emitting a schema v2 header. settlement-service's consumer still expects schema v1 with a required `qty` field (its code/config was frozen since before March — no deploys), so the first v2-shaped OrderExecuted message on orders-3 (offset 8841207) fails to deserialize at 07:42:14Z; it is treated as a poison message and retried indefinitely, permanently blocking partition orders-3. Consumption stalls (request_rate 62→0.4 req/s), lag climbs monotonically to 60k+ while order-service keeps producing, and zero settlements post. The exact topic->partition mapping is not exposed by tooling, but the message content (v2 header + missing qty, exactly the field dep-6010 renamed) and the 07:40Z→07:42:14Z ordering make the dep-6010 producer the only credible origin.

**Causal chain**
1. order-service v2.16.0 deployed 07:40Z, renaming OrderExecuted field qty->quantity and setting event schema version header v2 (dep-6010) [F1, F10, F23]
2. order-service is a producer on the shared kafka broker; settlement-service consumes from that same broker, so order-service's OrderExecuted stream reaches settlement-service's consumer [F5, F11, F20]
3. settlement-service's consumer contract was not changed in this window (zero deploys since 2026-03-01), so it still expects schema v1 with required field qty [F24]
4. First message to carry the v2 shape on orders-3 (offset 8841207) fails deserialization: 'missing required field qty (schema v1 expected, header says v2)' — the payload is v2-shaped, hitting a v1-expecting deserializer [F14, F25, F26, F21]
5. The failed message is retried as a poison message, blocking partition orders-3 (196 poison-retry ERRORs, first 07:44:51Z, same offset through 09:58Z) [F14, F27, F25]
6. Consumption stalls: request_rate collapses 62->0.4 req/s and consumer_lag climbs monotonically from ~26 at 07:41 to 60220 avg by 09:55Z; onset 07:42 is exactly when the poison offset is first consumed [F13, F15, F28]
7. Symptom: zero settlements posted (not a 5xx wave — error_rate flat ~0.1-0.32%); lag keeps growing as order-service continues producing v2 events [F16, F15]

**Supporting evidence**
  - `F1` ✔ Only one deploy occurred across ALL services in the window 06:22–09:59Z: order-service v2.16.0 'Order events v2' at 2026-05-12T07:40:00Z, status live, not rolled back; changes include 'OrderExecuted event: rename field qty -> quantity' and 'event schema version header v2'.
  - `F10` ✔ A deployment landed on the shared upstream producer 5 minutes after the config change: dep-6010 order-service v2.16.0 at 2026-05-12T07:40:00Z ('OrderExecuted event: rename field qty -> quantity', schema version header v2), status live, not rolled back. order-service feeds the same kafka queue settlement-service consumes.
  - `F23` ✔ The order-service deploy dep-6010 v2.16.0 (07:40Z) is the only deployment touching the shared kafka producer path, and its changes explicitly rename the OrderExecuted field ('rename field qty -> quantity') and set 'event schema version header v2' consistent with the settlement-service error text ('header says v2', missing 'qty').
  - `F5` ✔ Producers of messages to the shared kafka broker include order-service and payment-service (queue-produce); other consumers are notification-service; api-gateway calls order-service and payment-service over http. So order-service (deployed 07:40Z) is a producer feeding the topics settlement-service consumes.
  - `F11` ✔ settlement-service consumes from kafka (queue-consume) and writes to ledger-db (db); both kafka and ledger-db are shared components ('shared':true, owner platform). Its own definition is shared=false, so a neighbour can affect it via the shared broker or DB.
  - `F20` ✔ order-service produces to the same shared 'kafka' broker via a queue-produce edge (no topic names exposed); its dependency metadata also lists no topic/partition identifiers.
  - `F24` ✔ settlement-service had zero deployments in 2026-03-01..2026-05-12, so its consumer code/schema contract was NOT changed in this window; the v1-pin ('schema v1 expected') is pre-existing configuration, not a fresh change.
  - `F14` ✔ settlement-service produced ERROR logs starting exactly 07:42:14Z, dominated by two patterns: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences) and 'poison message: retrying orders-3@8841207 (attempt ...), partition blocked' (196 occurrences). All 409 matched entries were ERROR level; first_match 07:42:14Z.
  - `F25` ✔ The first failing offset on orders-3 is orders-3@8841207 and its first ERROR appears at 2026-05-12T07:42:14Z, i.e. AFTER 07:40Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. The same offset remains stuck for the whole window (last_match 09:58:31Z).
  - `F26` ✔ The failing payload DOES carry the schema v2 header: every deserialize error states the consumer expected schema v1 but 'header says v2', and the required v1 field 'qty' is missing — which is exactly the field renamed qty->quantity by order-service v2.16.0. So the message is a v2-shaped payload (quantity) hitting a v1-expecting deserializer.
  - `F21` ✔ The literal partition/topic identifier 'orders-3' appears only in settlement-service's runtime error log ('failed to deserialize OrderExecuted at orders-3@8841207 ... schema v1 expected, header says v2'), confirming settlement-service consumes the OrdersExecuted stream on a topic whose partition 3 is named/tracked as 'orders-3'. The dependency/topic-metadata tools do NOT expose an explicit topic->partition mapping, so I could not independently list settlement-service's consumed topics.
  - `F27` ✔ Only orders-3@8841207 and its partition are implicated; there are two error signatures, both on orders-3@8841207: 213 deserialize failures plus 196 'poison message: retrying orders-3@8841207 (attempt ...), partition blocked' (first poison-retry at 07:44:51Z), all ERROR level, first_seen 07:42:14Z.
  - `F13` ✔ settlement-service consumer_lag was flat at 21-64 messages through 07:40 (07:40 bucket avg 31.06, max 35.87) and then jumped in the very next 2-minute bucket to avg 603.29 at 07:42:00Z, then climbed monotonically (1516 at 07:44, 2399 at 07:46, 6028 at 07:54) and reached avg 60220.76 / max 61605.53 by 09:55:00Z. Onset of lag growth is 07:40-07:42Z (between the 07:40 and 07:42 buckets).
  - `F15` ✔ settlement-service request_rate collapsed mid-stream: steady ~62 req/s from 06:30 through the 07:33 bucket (avg 62.23), then the 07:42 bucket drops to avg 0.4 req/s and stays at ~0.4 for the rest of the window. This is a throughput stall, not a 5xx error spike.
  - `F28` ✔ The failure onset precedes/coincides with the lag onset: settlement-service consumer_lag was 26.25 at 07:41, jumped to 386.63 at 07:42 and climbed monotonically (1306 at 07:44, 3865 at 07:50, 8264 at 08:00), confirming the poison offset at 07:42:14Z blocked the partition and lag accumulated from then on.
  - `F16` ✔ settlement-service error_rate (HTTP 5xx % of requests) stayed flat at ~0.1-0.32% across the entire window including after onset (0.17 at 07:42, 0.21 at 08:00, 0.2 at 09:57), i.e. the 'zero settlements' symptom is NOT reflected as an HTTP 5xx error increase. 'Zero processed' is substantiated by the request_rate collapse and the partition-block errors, not by 5xx.
**Contradicting evidence**
  - `F18` ✔ order-service (the apparent producer of the 'orders-3' topic) showed no anomaly in the queried signals: 0 ERROR log entries for 07:00-09:59Z, and error_rate stayed flat at ~0.1-0.32% across 06:30-09:59Z. Blast-radius testing of order-service is only partial (see gaps).
**Assumptions**
  - settlement-service consumes specifically the topic/partition that order-service produces; tooling exposes no topic->partition mapping (only the literal 'orders-3' in the consumer's own error log), so this hop is inferred, not verified.
  - The poison message's payload originates from the post-07:40Z order-service v2.16.0 producer: no produce-side timestamp exists (F29), so this rests on the 6.5-min deploy->first-error gap plus the exact field-rename match, not on a message-level producer identity.
  - The v1 schema pin is a pre-existing consumer contract (only visible in runtime error text, not declared in config metadata), and settlement-service was never updated to accept v2/quantity.
  - order-service had no throughput drop at 07:40Z (its error_rate and logs are clean; throughput unverified due to budget exhaustion — F18).

### H2: settlement-service consumer poll-settings change (max.poll.records 500→200) stalled consumption
The config change cfg-3004 shrinking max.poll.records to 200 on settlement-service (07:35:37Z, 'smaller poll batches to smooth DB writes') altered poll pacing or commit behavior, slowing or stalling the consumer so settlements stopped posting and lag accrued. This is the reporter's suspicion.

**Causal chain**
1. max.poll.records changed 500->200 on settlement-service at 07:35:37Z by m.das [F2, F9, F17]
2. Smaller poll batches slow processing and/or alter offset commit timing, possibly triggering rebalance-timeout stalls *(assumed)*
3. Consumption throughput collapses and lag grows from ~07:42Z [F13, F15]

**Supporting evidence**
  - `F2` ✔ Exactly one config change in the window across ALL services: settlement-service key consumer.max.poll.records changed 500→200 at 2026-05-12T07:35:00Z by m.das ('Smaller poll batches to smooth DB writes').
  - `F9` ✔ The only settlement-service config change in the incident window was cfg-3004: consumer.max.poll.records 500 -> 200 at 2026-05-12T07:35:00Z by m.das ('Smaller poll batches to smooth DB writes'). This is the poll-settings change the reporter mentioned.
  - `F17` ✔ A config change was applied on settlement-service at 07:35:37Z, ~6.5 minutes before the first error and lag onset: 'config change cfg-3004 applied: consumer.max.poll.records=200'. This matches the reporter's note about changing consumer poll settings shortly before the incident.
**Contradicting evidence**
  - `F14` ✔ settlement-service produced ERROR logs starting exactly 07:42:14Z, dominated by two patterns: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences) and 'poison message: retrying orders-3@8841207 (attempt ...), partition blocked' (196 occurrences). All 409 matched entries were ERROR level; first_match 07:42:14Z.
  - `F25` ✔ The first failing offset on orders-3 is orders-3@8841207 and its first ERROR appears at 2026-05-12T07:42:14Z, i.e. AFTER 07:40Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. The same offset remains stuck for the whole window (last_match 09:58:31Z).
  - `F26` ✔ The failing payload DOES carry the schema v2 header: every deserialize error states the consumer expected schema v1 but 'header says v2', and the required v1 field 'qty' is missing — which is exactly the field renamed qty->quantity by order-service v2.16.0. So the message is a v2-shaped payload (quantity) hitting a v1-expecting deserializer.
  - `F27` ✔ Only orders-3@8841207 and its partition are implicated; there are two error signatures, both on orders-3@8841207: 213 deserialize failures plus 196 'poison message: retrying orders-3@8841207 (attempt ...), partition blocked' (first poison-retry at 07:44:51Z), all ERROR level, first_seen 07:42:14Z.
  - `F16` ✔ settlement-service error_rate (HTTP 5xx % of requests) stayed flat at ~0.1-0.32% across the entire window including after onset (0.17 at 07:42, 0.21 at 08:00, 0.2 at 09:57), i.e. the 'zero settlements' symptom is NOT reflected as an HTTP 5xx error increase. 'Zero processed' is substantiated by the request_rate collapse and the partition-block errors, not by 5xx.
  - `F28` ✔ The failure onset precedes/coincides with the lag onset: settlement-service consumer_lag was 26.25 at 07:41, jumped to 386.63 at 07:42 and climbed monotonically (1306 at 07:44, 3865 at 07:50, 8264 at 08:00), confirming the poison offset at 07:42:14Z blocked the partition and lag accumulated from then on.
**Assumptions**
  - Reducing poll batch size can produce a total, sustained stall rather than merely slower processing.
**Rejected because:** Mechanism mismatch: the observed failures are deserialization errors ('missing required field qty', 'schema v1 expected, header says v2') plus explicit 'poison message ... partition blocked' errors pinned to a single offset (F14, F25, F26, F27). A poll batch-size change cannot produce schema errors, a renamed-field failure, or a hard partition block. Timing is also non-specific: the config landed 07:35Z but lag/errors start only at 07:42Z — right after the 07:40Z deploy. No historical precedent links poll settings to lag (F12), though that absence rests on limited keyword searches.

### H3: Shared kafka broker partition-assignment loss / rebalancing storm stalls settlement consumer
A broker-side event (partition reassignment / rebalancing storm) on the shared kafka broker caused settlement-service's consumer to lose its partition assignment, halting consumption and accruing lag — the HIST-112/HIST-104 pattern.

**Causal chain**
1. Shared kafka broker suffers a partition reassignment / rebalance storm *(assumed)*
2. settlement-service consumer loses its partition assignment *(assumed)*
3. Consumption stops and lag grows from ~07:42Z [F13, F15]

**Supporting evidence**
  - `F6` ✔ ledger-db is a SHARED postgres used by four services (order-service, portfolio-service, payment-service, settlement-service); kafka is a SHARED broker with two producers and two consumers. Shared components are contention paths into settlement-service.
  - `F7` ✔ HIST-112 (2026-02-03) is the closest historical match: settlement-service + kafka, symptoms 'consumer lag growing' / 'settlements stuck'; root cause was 'Consumer lost partition assignment during broker maintenance' and the fix was 'Restarted consumer group'.
  - `F8` ✔ A second comparable precedent, HIST-104 (2025-11-05, notification-service + kafka), also had broker-side causality: 'Partition reassignment caused consumer group rebalancing storm', resolved by 'Throttled reassignment'. Both historical lag incidents are infrastructure/broker categories, not config.
  - `F11` ✔ settlement-service consumes from kafka (queue-consume) and writes to ledger-db (db); both kafka and ledger-db are shared components ('shared':true, owner platform). Its own definition is shared=false, so a neighbour can affect it via the shared broker or DB.
**Contradicting evidence**
  - `F14` ✔ settlement-service produced ERROR logs starting exactly 07:42:14Z, dominated by two patterns: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences) and 'poison message: retrying orders-3@8841207 (attempt ...), partition blocked' (196 occurrences). All 409 matched entries were ERROR level; first_match 07:42:14Z.
  - `F25` ✔ The first failing offset on orders-3 is orders-3@8841207 and its first ERROR appears at 2026-05-12T07:42:14Z, i.e. AFTER 07:40Z: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)'. The same offset remains stuck for the whole window (last_match 09:58:31Z).
  - `F26` ✔ The failing payload DOES carry the schema v2 header: every deserialize error states the consumer expected schema v1 but 'header says v2', and the required v1 field 'qty' is missing — which is exactly the field renamed qty->quantity by order-service v2.16.0. So the message is a v2-shaped payload (quantity) hitting a v1-expecting deserializer.
  - `F27` ✔ Only orders-3@8841207 and its partition are implicated; there are two error signatures, both on orders-3@8841207: 213 deserialize failures plus 196 'poison message: retrying orders-3@8841207 (attempt ...), partition blocked' (first poison-retry at 07:44:51Z), all ERROR level, first_seen 07:42:14Z.
  - `F13` ✔ settlement-service consumer_lag was flat at 21-64 messages through 07:40 (07:40 bucket avg 31.06, max 35.87) and then jumped in the very next 2-minute bucket to avg 603.29 at 07:42:00Z, then climbed monotonically (1516 at 07:44, 2399 at 07:46, 6028 at 07:54) and reached avg 60220.76 / max 61605.53 by 09:55:00Z. Onset of lag growth is 07:40-07:42Z (between the 07:40 and 07:42 buckets).
**Assumptions**
  - Broker-side reassignment occurred at ~07:40-07:42Z; no broker telemetry was available to observe it.
  - The Feb/Nov historical pattern recurs with no triggering deploy.
**Rejected because:** Rests only on historical precedent (HIST-112/HIST-104, F7/F8 — past incidents, weak evidence) with no positive telemetry: no broker partition-assignment/reassignment metrics or group-state events were queried, and the dismissal is by inference rather than direct falsification (critic's minor note acknowledged). Critically, the observed signatures are deserialization/poison errors on one specific offset with content that exactly matches the 07:40Z deploy's field rename (F14, F25, F26, F27) — a schema-contract failure, not an assignment loss. A partition-assignment loss would not produce 'missing required field qty ... header says v2'.

### H4: Shared ledger-db or broker contention throttles settlement-service
Contention on the shared ledger-db (four services) or the shared broker throttled settlement-service's consumption/writes, stalling settlements; settlement is merely where the symptom surfaced.

**Causal chain**
1. ledger-db (shared by order-, portfolio-, payment-, settlement-service) becomes contended [F6, F11]
2. settlement-service writes to ledger-db stall, blocking processing *(assumed)*
3. Consumption blocked, lag grows from ~07:42Z [F13, F15]

**Supporting evidence**
  - `F6` ✔ ledger-db is a SHARED postgres used by four services (order-service, portfolio-service, payment-service, settlement-service); kafka is a SHARED broker with two producers and two consumers. Shared components are contention paths into settlement-service.
  - `F11` ✔ settlement-service consumes from kafka (queue-consume) and writes to ledger-db (db); both kafka and ledger-db are shared components ('shared':true, owner platform). Its own definition is shared=false, so a neighbour can affect it via the shared broker or DB.
**Contradicting evidence**
  - `F14` ✔ settlement-service produced ERROR logs starting exactly 07:42:14Z, dominated by two patterns: 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field 'qty' (schema v1 expected, header says v2)' (213 occurrences) and 'poison message: retrying orders-3@8841207 (attempt ...), partition blocked' (196 occurrences). All 409 matched entries were ERROR level; first_match 07:42:14Z.
  - `F16` ✔ settlement-service error_rate (HTTP 5xx % of requests) stayed flat at ~0.1-0.32% across the entire window including after onset (0.17 at 07:42, 0.21 at 08:00, 0.2 at 09:57), i.e. the 'zero settlements' symptom is NOT reflected as an HTTP 5xx error increase. 'Zero processed' is substantiated by the request_rate collapse and the partition-block errors, not by 5xx.
  - `F26` ✔ The failing payload DOES carry the schema v2 header: every deserialize error states the consumer expected schema v1 but 'header says v2', and the required v1 field 'qty' is missing — which is exactly the field renamed qty->quantity by order-service v2.16.0. So the message is a v2-shaped payload (quantity) hitting a v1-expecting deserializer.
  - `F27` ✔ Only orders-3@8841207 and its partition are implicated; there are two error signatures, both on orders-3@8841207: 213 deserialize failures plus 196 'poison message: retrying orders-3@8841207 (attempt ...), partition blocked' (first poison-retry at 07:44:51Z), all ERROR level, first_seen 07:42:14Z.
**Assumptions**
  - DB or broker contention began at exactly 07:40-07:42Z with no triggering change.
**Rejected because:** No telemetry shows DB or broker saturation (cpu_pct, db_pool_in_use, latency queries were budget-exhausted; nothing positive was found). The observed errors are schema/deserialization errors unrelated to write contention, and F14/F26/F27 explain the hard, single-offset block directly without invoking shared-resource throttling. Note the reporter's own config rationale ('smoothing DB writes') hints at DB pressure, but no evidence supports contention as the origin.

## Facts (verified observations)
- Only one deployment occurred across all services in the incident window (06:22–09:59Z): order-service v2.16.0 'Order events v2' at 2026-05-12T07:40:00Z, status live, not rolled back, with changes 'OrderExecuted event: rename field qty -> quantity' and 'event schema version header v2'. [F1, F10, F23]
- Exactly one config change occurred in the window: settlement-service consumer.max.poll.records 500→200 at 2026-05-12T07:35:00Z (cfg-3004, applied 07:35:37Z, 'Smaller poll batches to smooth DB writes'). [F2, F9, F17]
- settlement-service produced ERROR logs starting exactly 07:42:14Z, dominated by 'failed to deserialize OrderExecuted at orders-3@8841207: missing required field qty (schema v1 expected, header says v2)' (213 occurrences) and 'poison message: retrying orders-3@8841207 (attempt ...), partition blocked' (196 occurrences). [F14, F25, F27]
- The failing payload carries the schema v2 header and is missing the v1-required field qty — exactly the field renamed qty→quantity by order-service v2.16.0; i.e. a v2-shaped payload hitting a v1-expecting deserializer. [F26]
- settlement-service consumer_lag was flat at 21–64 messages through 07:40Z, then jumped to avg 603.29 in the 07:42Z bucket and climbed monotonically to avg 60220.76 / max 61605.53 by 09:55Z; lag was 26.25 at 07:41 and 386.63 at 07:42. [F13, F28]
- settlement-service request_rate collapsed from ~62 req/s (steady through the 07:33 bucket) to ~0.4 req/s in the 07:42 bucket and stayed there — a throughput stall, not a 5xx error spike. [F15, F16]
- The first failing offset on orders-3 is @8841207, first failing at 07:42:14Z (after the 07:40Z deploy) and remaining stuck for the whole window (last_match 09:58:31Z); only that one offset/partition is implicated. [F25, F27]
- settlement-service consumes from the shared kafka broker (queue-consume) and writes to the shared ledger-db (db); order-service is a queue-produce producer on the same shared broker, and the shared broker/DB are contention paths into settlement-service. [F4, F5, F6, F11]
- settlement-service had zero deployments between 2026-03-01 and 2026-05-12, so its consumer code/schema contract was not changed in this window; the v1-pin is pre-existing. [F24]
- The literal identifier 'orders-3' appears only in settlement-service's runtime error log, confirming it consumes an OrderExecuted stream whose partition 3 is tracked as 'orders-3'; dependency/topic metadata exposes no explicit topic→partition mapping. [F19, F20]
- No produce-side (ingest) timestamp exists for offset orders-3@8841207: INFO-level offset searches returned 0 matches on settlement-service, and order-service INFO searches for 'quantity'/'produced' returned 0 matches over 04:00–09:59Z. [F29]
- order-service showed no anomaly in the queried signals: 0 ERROR log entries for 07:00–09:59Z and error_rate flat at ~0.1–0.32% across 06:30–09:59Z (throughput not verifiable — budget exhausted). [F18]
- Historical lag incidents ins this service area were broker/infrastructure-caused (HIST-112: 'consumer lost partition assignment during broker maintenance', fixed by restarting the consumer group; HIST-104: partition reassignment rebalancing storm). [F7, F8, F22]
- No deploys occurred in the 2-hour baseline before the window (04:00–06:22Z). [F3]

## Assumptions (not verified)
- settlement-service consumes specifically the topic/partition that order-service produces; tooling exposes no topic→partition mapping (only the literal 'orders-3' in the consumer's own error log), so this producer→consumer hop is inferred, not verified.
- The poison message's payload originates from the post-07:40Z order-service v2.16.0 producer: no produce-side timestamp exists, so this rests on the 6.5-minute deploy→first-error gap plus the exact field-rename/schema-header match, not on message-level producer identity.
- The v1 schema pin is a pre-existing consumer contract, visible only in runtime error text ('schema v1 expected, header says v2'), not declared in config metadata.
- A poll batch-size reduction cannot produce schema/deserialization errors or a hard partition block, so cfg-3004 is not the origin of the stall (mechanism-based exclusion, supported by the absence of precedent in F12).
- Broker-side partition-assignment loss (HIST-112 pattern) did not occur at 07:40–07:42Z: no broker telemetry was available, so this is inferred from the explicit deserialize/poison error signatures rather than affirmatively falsified.
- The 'zero settlements processed' symptom is real; it was assessed indirectly via request_rate collapse and poison/partition-block errors rather than a directly queried settled-count metric.
- (demoted from facts: no verified observation backs it) No past incident was found attributed to a consumer poll/max.poll.records setting; the only config-flavoured near-hit (HIST-116) was a gateway default change.

## Timeline
- `2026-05-12T07:35:00Z` Config change cfg-3004 applied on settlement-service: consumer.max.poll.records 500→200 (by m.das, 'smaller poll batches to smooth DB writes'). [F2, F9, F17]
- `2026-05-12T07:40:00Z` Only deploy of the window: order-service v2.16.0 ('Order events v2') goes live, renaming OrderExecuted field qty→quantity and adding event schema header v2. Not rolled back. [F1, F10, F23]
- `2026-05-12T07:41:00Z` settlement-service consumer_lag still low (26.25) — last pre-onset bucket. [F28]
- `2026-05-12T07:42:00Z` Lag onset: consumer_lag jumps to 386.63 (07:42 bucket avg 603.29 vs 31.06 at 07:40); request_rate collapses from ~62 req/s to ~0.4 req/s and stays there. [F13, F15, F28]
- `2026-05-12T07:42:14Z` First deserialize ERROR on orders-3@8841207: 'missing required field qty (schema v1 expected, header says v2)' — a v2-shaped message blocking a v1-expecting consumer. [F14, F25, F26]
- `2026-05-12T07:44:00Z` Lag continues climbing (1516 at 07:44); first poison-message retry logged at 07:44:51Z ('retrying orders-3@8841207 ..., partition blocked'). [F13, F27]
- `2026-05-12T07:54:00Z` Lag reaches 6028 and continues rising monotonically as order-service keeps producing. [F13]
- `2026-05-12T08:22:00Z` Incident INC-010 reported by on-call SRE: no settlements posted for ~40 minutes, settlement consumer lag growing. []
- `2026-05-12T09:55:00Z` Lag reaches avg 60220.76 / max 61605.53; HTTP error_rate remains flat at ~0.2%, confirming the symptom is a throughput stall not a 5xx spike. [F13, F16]
- `2026-05-12T09:58:31Z` Offset orders-3@8841207 still failing/blocked (last_match) — the poison message has not cleared. [F25]

## Recommended actions
- **[immediate]** Unblock the poison offset on partition orders-3 (offset 8841207) on settlement-service: route the failing message to a dead-letter topic / skip it using the consumer's poison-message handling (or, if DLQ is not configurable, raise the consumer's max retries and skip-on-deserialize-failure), then restart the settlement-service consumer group so consumption resumes on orders-3. -- The partition is hard-blocked by a single retried offset (F14, F25, F27) and lag is climbing monotonically to 60k+ (F13, F28); clearing the blocked offset is the direct mitigation and matches the HIST-112 fix pattern for stalled consumers (F7). [F14, F25, F27, F13, F28, F7]
- **[immediate]** In parallel, roll back order-service to the pre-dep-6010 version (stop emitting schema v2 / quantity OrderExecuted events) or pause the v2.16.0 producer path, to halt the injection of further messages the settlement consumer cannot deserialize. -- dep-6010 is the only deploy in the window and it changed exactly the contract that is failing (F1, F10, F23, F26); rolling back removes the source of incompatible messages while the consumer is fixed. [F1, F10, F23, F26]
- **[immediate]** Do NOT revert cfg-3004 (consumer.max.poll.records=200) as the first action; leave it at 200 pending the schema fix. -- A poll batch-size change cannot produce schema/deserialization errors or a hard partition block (F14, F25, F26, F27), the config landed 6.5 minutes before onset while lag/errors start only after the 07:40Z deploy (F2, F9, F17, F13), and there is no historical precedent for a poll-setting-caused lag incident (F12). Reverting it wastes mitigation time. [F2, F9, F17, F13, F12]
- **[short_term]** Confirm recovery: verify consumer_lag on settlement-service returns to the pre-incident baseline (tens of messages) and request_rate recovers to ~62 req/s after the offset is cleared, and confirm settlements begin posting. -- Onset was measured as lag 26→386 in the 07:41/07:42 buckets with request_rate 62→0.4 (F13, F15, F28); the inverse movement is the acceptance signal for the fix. [F13, F15, F28]
- **[short_term]** Update settlement-service's consumer contract to schema v2: accept the renamed field quantity (v2 header) while remaining backward-compatible with v1 (qty), and add a config-declared schema version so the pin is explicit rather than implicit in runtime behavior. -- The consumer was never updated for v2 — zero settlement-service deploys since March (F24) — while the producer now emits v2 (F1, F23, F26); the contract mismatch is the root cause mechanism. [F24, F1, F23, F26]
- **[long_term]** Add schema-compatibility gating to the deploy pipeline: any producer change to an event contract (field rename, header/version bump) must be validated against registered consumers' declared schemas before rollout, and consumers must handle unknown/future schema versions gracefully (skip-to-DLQ instead of blocking a partition). -- The incident's failure mode is a producer-side contract change unilaterally breaking a downstream consumer with no gating and no poison-message escape hatch (F1, F14, F23, F26, F27). [F1, F14, F23, F26, F27]
- **[long_term]** Alert on consumer-lag onset rate and on any 'poison message / partition blocked' log pattern for settlement-service, so a single stuck offset pages within minutes rather than ~40 minutes. -- Lag went from 26 to 386 within one 2-minute bucket and kept climbing for ~2.5 hours before being reported (F13, F28); the existing alerting relied on an HTTP error_rate that stayed flat at ~0.2% and never fired (F16). [F13, F28, F16]

## Data gaps
- No Kafka topic catalog or explicit topic→partition mapping is exposed by any available tool; get_service_dependencies returns only a service-level edge to the shared 'kafka' broker (F19, F20). The literal 'orders-3' is known only from the consumer's own error log (F21), so the claim that settlement-service consumes the partition order-service produces is inferred, not verified. This is the main limit on the conclusion.
- No produce-side timestamp for offset orders-3@8841207 exists (F29, F30): INFO searches returned 0 matches for offsets on settlement-service and for 'quantity'/'produced' on order-service. Whether the poison message was PRODUCED after 07:40Z is therefore inferred from the deploy→first-error gap, not proven.
- order-service throughput (request_rate) was never queried, and settlement-service cpu_pct/restarts/db_pool_in_use/latency_p99 returned 'budget_exhausted'; blast-radius testing of order-service is only partial (error_rate + logs clean, throughput unverified) (F18).
- The settled/posted count was not directly queried; 'zero settlements' was assessed indirectly via request_rate collapse and poison/partition-block errors, so 'zero posted' vs 'merely slow' is not directly measured.
- No broker telemetry (partition-assignment/reassignment metrics, consumer group state, broker events) was queried, so the HIST-112 broker-side pattern (F7, F8) is dismissed on the strength of the explicit deserialize/poison error text rather than affirmatively falsified.
- Pre-window config changes (04:00–06:22Z) were not baselined (budget exhausted), though baseline deploys were confirmed zero (F3); no kafka-side broker config change could be verified in or before the window.
- No other candidate services (other consumers/producers of the orders topic, schema registry) were checked around 07:35–07:42Z, so whether settlement-service is the sole affected service remains partly open.
- No evidence retrieves a declared config artifact pinning the consumer to schema v1; the v1-pin is known only from runtime error text (assumption, not verified config).
- order-service config changes were not retrieved, so whether the producer's topic/partition routing or keying changed alongside the schema rename is unconfirmed.

## What would change this conclusion
- Broker telemetry showing a partition reassignment, consumer-group rebalance, or loss of partition assignment on orders-3 at ~07:40–07:42Z (the HIST-112 pattern) would move the origin from the schema-contract break to H3 (infrastructure/broker cause).
- A produce-side timestamp or Kafka message metadata showing offset orders-3@8841207 was produced BEFORE the 07:40Z dep-6010 deploy (i.e. carrying a v1/qty payload from the old producer) would break the link between the failing message and the v2.16.0 schema rename.
- Evidence that settlement-service actually consumes a different topic/partition than order-service produces would invalidate the order-service → settlement-service hop and reduce H1 to unsupported.
- A known poll-setting mechanism that can cause sustained consumer stalls (or a precedent incident attributing lag to max.poll.records) would revive H2 as a co-cause or primary cause.
- DB or broker saturation telemetry (cpu_pct, db_pool_in_use, latency, kafka throttling) coinciding with the 07:40–07:42Z onset would support H4 (shared-resource contention) as the origin.
- A settlement-service deploy or config change that updated its consumer to expect schema v2 (contradicting F24) would remove the contract mismatch as the mechanism.
- Evidence that the failing messages are dropped instead of blocking the partition (i.e. no true poison-message partition block) would require re-explaining the lag and request_rate collapse on another basis.

## Integrity warnings (set by code)
- fact demoted to assumption: No past incident was found attributed to a consumer poll/max.poll.records settin

## Investigation stats
- latency 148.41s, LLM calls 25, tokens 178434 (in 150740 / out 27694)
- tool calls 40 (0 failed, 0 partial, 0 recovered by retry), findings 30/30 verified
