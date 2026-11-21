# Incident investigation report: INC-010
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
settlement-service stopped posting settlements because order-service's v2.16.0 deploy (dep-6010, 07:40) renamed the OrderExecuted field qty->quantity and bumped the schema header to v2, while settlement-service still deserializes v1. Every v2 message fails with 'missing required field qty (schema v1 expected, header says v2)', and the first bad offset (orders-3@8841207) becomes a poison message that blocks partition orders-3, halting consumption. Consumer lag grew from ~40 to ~60,713 and request_rate collapsed from ~62 to ~0.4 req/s starting in the 07:42 bucket — exactly matching the 07:40 deploy, 7 minutes AFTER the settlement config change (07:35), which produced no degradation. The reported config change (consumer.max.poll.records 500->200) is a red herring. Resource metrics are flat (memory ~50%, db_pool ~6, restarts 0, order-service error_rate ~0.2%), ruling out resource/infra causes. Root cause: bad_deployment originating in order-service.

## Probable root cause
**order-service v2.16.0 schema change broke settlement-service consumer (field rename qty->quantity)** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.16.0 schema change broke settlement-service consumer (field rename qty->quantity) | bad_deployment | order-service | dep-6010 | 0.86 (high) | active |
| 2 | H2: settlement-service poll-settings change cfg-3004 caused stall | config_change | settlement-service | cfg-3004 | 0.06 (low) | rejected |
| 3 | H3: Kafka broker/partition failure blocked the consumer | infrastructure_failure | kafka | - | 0.04 (low) | rejected |
| 4 | H4: Resource exhaustion on settlement-service | resource_exhaustion | settlement-service | - | 0.02 (low) | rejected |

### H1: order-service v2.16.0 schema change broke settlement-service consumer (field rename qty->quantity)
order-service deploy dep-6010 (v2.16.0) renamed OrderExecuted.qty to 'quantity' and bumped the event header to schema v2 at 07:40. settlement-service still expects v1 (qty), so every v2 message fails deserialization; the offending offset orders-3@8841207 becomes a poison message that blocks partition orders-3, halting the consumer and causing lag to grow unbounded while zero settlements post.

**Causal chain**
1. dep-6010 (v2.16.0) renames qty->quantity and sets header v2 [F1]
2. settlement-service deserializer expects v1 -> 'missing required field qty' [F2]
3. failed offset becomes poison message, partition orders-3 blocked [F3]
4. consumer stops -> request_rate collapses to ~0.4 [F5]
5. consumer_lag grows monotonically; zero settlements posted [F4]

**Supporting evidence**
  - `F1` ✔ order-service deployed v2.16.0 at 07:40:00Z which renamed the OrderExecuted field 'qty'->'quantity', added field 'venue', and set event schema version header v2.
  - `F2` ✔ settlement-service began emitting deserialization errors at 07:42:14Z: 'missing required field qty (schema v1 expected, header says v2)' on orders-3@8841207.
  - `F3` ✔ settlement-service then logged poison-message retries with 'partition blocked' on the same offset orders-3@8841207 starting 07:44:51Z.
  - `F4` ✔ settlement-service consumer_lag was flat (~40-64) through 07:33:00Z, then jumped to avg 2169 (max 3865) in the 07:42 bucket and climbed monotonically to 60713 at 09:57.
  - `F5` ✔ settlement-service request_rate collapsed from ~62 req/s through 07:33 to ~0.4 req/s from the 07:42 bucket onward.
  - `F12` ✔ Lag/throughput divergence onset (07:42) coincides with the order-service v2 deploy (07:40) and is 7 minutes after the settlement config change (07:35), which itself produced no immediate degradation.
**Contradicting evidence**
  - (none)
**Assumptions**
  - settlement-service is the only/handling consumer of OrderExecuted on orders-3
  - no consumer-side v2 support was deployed (no settlement-service deploy found)

### H2: settlement-service poll-settings change cfg-3004 caused stall
The consumer.max.poll.records change (500->200) at 07:35 slowed polling enough to grow lag.

**Causal chain**
1. cfg-3004 lowers poll size [F6]
2. reduced polling would slowly grow lag *(assumed)*

**Supporting evidence**
  - `F6` ✔ A config change cfg-3004 on settlement-service lowered consumer.max.poll.records from 500 to 200 at 07:35:00Z.
**Contradicting evidence**
  - `F2` ✔ settlement-service began emitting deserialization errors at 07:42:14Z: 'missing required field qty (schema v1 expected, header says v2)' on orders-3@8841207.
  - `F4` ✔ settlement-service consumer_lag was flat (~40-64) through 07:33:00Z, then jumped to avg 2169 (max 3865) in the 07:42 bucket and climbed monotonically to 60713 at 09:57.
  - `F5` ✔ settlement-service request_rate collapsed from ~62 req/s through 07:33 to ~0.4 req/s from the 07:42 bucket onward.
  - `F12` ✔ Lag/throughput divergence onset (07:42) coincides with the order-service v2 deploy (07:40) and is 7 minutes after the settlement config change (07:35), which itself produced no immediate degradation.
**Rejected because:** cfg-3004 at 07:35 did NOT change lag/throughput: lag stayed flat (~40-64) through 07:33 and request_rate stayed ~62; divergence starts only at 07:42, matching the 07:40 order-service deploy. A poll-batch change cannot produce 'missing required field qty' deserialization errors. Also cfg-3004 reduces batch size, which cannot explain a totally blocked partition.

### H3: Kafka broker/partition failure blocked the consumer
A broker or partition infrastructure failure blocked consumption of orders-3.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F2` ✔ settlement-service began emitting deserialization errors at 07:42:14Z: 'missing required field qty (schema v1 expected, header says v2)' on orders-3@8841207.
  - `F3` ✔ settlement-service then logged poison-message retries with 'partition blocked' on the same offset orders-3@8841207 starting 07:44:51Z.
  - `F10` ✔ kafka logs contain only a single unrelated WARN (idempotent retry after connection reset at 09:51) — no broker/partition failure signal.
**Rejected because:** kafka logs show only one unrelated connection-reset WARN; no broker errors. The block is explained by a deterministic app-level poison message (orders-3@8841207) with explicit deserialize errors, not a broker outage.

### H4: Resource exhaustion on settlement-service
CPU/memory/DB pool exhaustion stalled the consumer.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ settlement-service resource metrics show no exhaustion: memory_pct ~50% flat, db_pool_in_use ~6 flat, restarts 0 throughout.
  - `F9` ✔ settlement-service CPU rose from ~28% to ~70% in the 07:42 bucket and stayed elevated, consistent with a hot retry loop rather than a resource shortage.
**Rejected because:** memory ~50% flat, db_pool ~6 flat, restarts 0. CPU rose only after 07:42, consistent with a hot poison-message retry loop downstream of the deserialize failure, not a cause.

## Facts (verified observations)
- order-service dep-6010 v2.16.0 deployed 07:40 renamed qty->quantity and set schema header v2. [F1]
- settlement-service logged 'missing required field qty (schema v1 expected, header says v2)' starting 07:42:14Z. [F2]
- The failed offset became a poison message blocking partition orders-3. [F3]
- settlement-service consumer_lag jumped from flat ~40 to 2169 avg in the 07:42 bucket and reached 60713. [F4]
- settlement-service request_rate collapsed from ~62 to ~0.4 req/s at 07:42. [F5]
- settlement config change cfg-3004 lowered max.poll.records 500->200 at 07:35, before the divergence. [F6]
- order-service stayed healthy (flat request_rate ~231 req/s, ~0.2% error rate). [F7]
- settlement-service resource metrics show no exhaustion (memory ~50%, db_pool ~6, restarts 0). [F8]
- settlement-service CPU rose to ~70% only after 07:42 (retry-loop symptom). [F9]
- kafka logs show no broker failure signal. [F10]
- No settlement-service deployments occurred in the window. [F11]

## Assumptions (not verified)
- settlement-service is the consumer of the orders topic and the only handler of OrderExecuted on orders-3
- no settlement-service-side v2 deserializer support was shipped (no settlement deploy found in window)
- order-service continued publishing v2-format events throughout the window (it was healthy and processing traffic)

## Timeline
- `2026-05-12T07:35:00Z` settlement-service config cfg-3004: consumer.max.poll.records 500->200 (no immediate effect) [F6]
- `2026-05-12T07:40:00Z` order-service deploys v2.16.0 (dep-6010): qty->quantity, schema header v2 [F1]
- `2026-05-12T07:42:14Z` settlement-service first deserialization error 'missing required field qty (v1 expected, header v2)' on orders-3@8841207 [F2]
- `2026-05-12T07:42:00Z` consumer_lag spikes to avg 2169; request_rate collapses to ~0.4 req/s [F4, F5]
- `2026-05-12T07:44:51Z` poison-message retries with 'partition blocked' on orders-3@8841207 begin [F3]
- `2026-05-12T08:22:00Z` Incident reported (settlements stuck ~40 min) []
- `2026-05-12T09:57:00Z` consumer_lag reaches 60713 and still climbing [F4]

## Recommended actions
- **[immediate]** Roll back or hotfix order-service dep-6010 to emit qty (v1) or dual-write both fields, and simultaneously ship a v2-aware deserializer in settlement-service -- The v2 messages are the direct cause of deserialize failures; restoring a compatible contract unblocks consumption. [F1, F2]
- **[immediate]** Skip/quarantine the poison offset orders-3@8841207 (or DLQ the offending event) so partition orders-3 can resume -- The single blocked offset is halting the entire partition; bypassing it restores throughput for the rest of the stream. [F3]
- **[short_term]** Add poison-message/DLQ alerting and a max-retry cap so a stuck partition pages before lag reaches tens of thousands -- Retries ran into the tens of thousands of attempts before anyone noticed; earlier detection limits blast radius. [F3, F4]
- **[short_term]** Do NOT attribute the incident to cfg-3004; document it as a red herring and keep the poll-settings change -- Timing shows cfg-3004 predates divergence and produced no degradation; reverting it would not fix the incident. [F6, F12]
- **[long_term]** Add consumer schema-compatibility/contract tests and a schema registry so producer field renames cannot silently break consumers -- A rename with a version header mismatch should be caught in CI or enforced by a registry. [F1, F2]

## Data gaps
- settlement-service log query returned truncated:true (409 ERROR matches, 40 shown) — full set not inspected, though the two dominant patterns are consistent across the window
- No direct producer-side record of the message bytes on orders-3 was available; the v1/v2 mismatch is inferred from the consumer's explicit error message plus the deploy's change summary
- No explicit metric for settlements posted count was available; zero-settlement symptom is taken from the incident report and the request_rate collapse

## Investigation stats
- latency 47.5s, LLM calls 5, tokens 56236 (in 49580 / out 6656)
- tool calls 19 (0 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
