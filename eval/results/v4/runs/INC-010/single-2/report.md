# Incident investigation report: INC-010
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
settlement-service stopped consuming because order-service deploy dep-6010 (v2.16.0) renamed the OrderExecuted field `qty`→`quantity` and bumped the schema header to v2, while settlement-service's deserializer still expects schema v1. Every message on orders-3 (offset 8841207) fails deserialization; the consumer enters a poison-message retry loop and the partition is blocked, so zero settlements post and consumer_lag grows monotonically (~45 → 61,605 messages). The config change to max.poll.records (500→200) and the errors are coincidental/adjacent, not causal: a smaller poll batch cannot produce a `missing required field 'qty'` deserialization failure. Resource/infra causes are contradicted by flat db_pool (~6), zero restarts, and flat HTTP error_rate. Fix is to restore schema compatibility (roll back dep-6010 or make settlement-service tolerate v2 / skip the poison offset).

## Probable root cause
**order-service v2 schema deploy broke settlement-service deserializer** -- bad_deployment in `order-service`, triggered by `dep-6010` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2 schema deploy broke settlement-service deserializer | bad_deployment | order-service | dep-6010 | 0.90 (high) | active |
| 2 | H2: Consumer poll-settings config change throttled consumption | config_change | settlement-service | cfg-3004 | 0.08 (low) | rejected |
| 3 | H3: Kafka broker rebalance / partition reassignment (per historical pattern) | infrastructure_failure | kafka | - | 0.05 (low) | rejected |

### H1: order-service v2 schema deploy broke settlement-service deserializer
Deploy dep-6010 (order-service v2.16.0) renamed OrderExecuted field qty->quantity and stamped header v2; settlement-service still expects schema v1, so every message fails to deserialize, the consumer loops on a poison offset and blocks partition 3, halting all settlements and growing lag.

**Causal chain**
1. dep-6010 renames qty->quantity and sets header v2 at 07:40 [F1]
2. settlement-service deserializer expects v1 -> missing required field 'qty' errors from 07:42:14 [F2]
3. consumer retries the same offset, blocking partition 3 (poison message) [F3, F11]
4. zero settlements posted and consumer_lag grows monotonically [F4]

**Supporting evidence**
  - `F1` ✔ order-service deploy dep-6010 (v2.16.0) landed at 07:40:00Z and renamed the OrderExecuted field qty->quantity with an event schema version header v2.
  - `F2` ✔ The first settlement-service deserialization ERROR appears at 07:42:14Z, ~2 min after dep-6010, matching 'failed to deserialize OrderExecuted ... missing required field qty (schema v1 expected, header says v2)' (213 occurrences).
  - `F3` ✔ settlement-service then enters a poison-message retry loop on the same offset, blocking the partition: 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences).
  - `F4` ✔ consumer_lag was flat ~40-65 messages until the 07:36 bucket, then grew from 774.71 to 61,605.53 by 09:48, i.e. onset in the 07:36-07:48 window.
  - `F9` ✔ order-service emitted no relevant errors in this window (only a single unrelated slow-request WARN at 09:14:44Z), consistent with the broker/consumer side being where the failure surfaces.
  - `F11` ✔ The poison-message loop is the mechanism that converts a per-message deserialization failure into a full partition block (zero settlements) and unbounded lag growth.
**Contradicting evidence**
  - (none)
**Assumptions**
  - settlement-service deserializer was not updated to accept v2 in the same window (no such deploy/config observed)
  - orders-3@8841207 is the first failed offset and blocks the partition head

### H2: Consumer poll-settings config change throttled consumption
cfg-3004 lowered consumer.max.poll.records 500->200 on settlement-service at 07:35, reducing throughput enough to cause lag and stalls.

**Causal chain**
1. max.poll.records reduced 500->200 at 07:35 [F5]
2. reduced batch size throttles consumption *(assumed)*
3. lag grows and settlements stall [F4]

**Supporting evidence**
  - `F5` ✔ The only config change was on settlement-service at 07:35:00Z: consumer.max.poll.records 500 -> 200 ('Smaller poll batches to smooth DB writes').
  - `F4` ✔ consumer_lag was flat ~40-65 messages until the 07:36 bucket, then grew from 774.71 to 61,605.53 by 09:48, i.e. onset in the 07:36-07:48 window.
**Contradicting evidence**
  - `F2` ✔ The first settlement-service deserialization ERROR appears at 07:42:14Z, ~2 min after dep-6010, matching 'failed to deserialize OrderExecuted ... missing required field qty (schema v1 expected, header says v2)' (213 occurrences).
  - `F3` ✔ settlement-service then enters a poison-message retry loop on the same offset, blocking the partition: 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences).
**Assumptions**
  - throughput reduction alone could stall settlements
**Rejected because:** A smaller poll batch cannot produce 'missing required field qty (schema v1 expected, header says v2)' deserialization errors or block a partition; lag growth is fully explained by the poison-message loop after the 07:40 deploy.

### H3: Kafka broker rebalance / partition reassignment (per historical pattern)
A broker-side rebalance caused settlement-service to lose its partition assignment, letting lag grow and settlements stall, as in HIST-104/HIST-112.

**Causal chain**
1. broker rebalance loses assignment *(assumed)*
2. consumer stops advancing -> lag grows [F4]

**Supporting evidence**
  - `F10` ✔ Prior similar incidents (HIST-104, HIST-112) were Kafka broker rebalance/partition-reassignment failures, not schema mismatches - similar symptoms but a different proposed cause.
**Contradicting evidence**
  - `F2` ✔ The first settlement-service deserialization ERROR appears at 07:42:14Z, ~2 min after dep-6010, matching 'failed to deserialize OrderExecuted ... missing required field qty (schema v1 expected, header says v2)' (213 occurrences).
  - `F3` ✔ settlement-service then enters a poison-message retry loop on the same offset, blocking the partition: 'poison message: retrying orders-3@8841207 (attempt <n>), partition blocked' (196 occurrences).
  - `F7` ✔ settlement-service restarts metric was 0 across every bucket - no process/broker-driven restart.
**Assumptions**
  - a rebalance occurred but was not logged
**Rejected because:** No rebalance/reassignment evidence; restarts=0, and the consumer is actively processing (thousands of retry attempts on a specific offset), which is inconsistent with a lost assignment.

## Facts (verified observations)
- order-service deploy dep-6010 (v2.16.0) at 07:40:00Z renamed the OrderExecuted field qty->quantity and set event schema header v2. [F1]
- settlement-service began emitting 'missing required field qty (schema v1 expected, header says v2)' deserialization errors at 07:42:14Z, ~2 minutes after dep-6010. [F2]
- settlement-service entered a poison-message retry loop that blocked partition 3 (orders-3@8841207). [F3]
- settlement-service consumer_lag grew from ~45 to 61,605.53 messages, with onset in the 07:36-07:48 window. [F4]
- The only config change was consumer.max.poll.records 500->200 on settlement-service at 07:35:00Z. [F5]
- settlement-service db_pool_in_use stayed flat at ~6 connections, restarts=0, and HTTP error_rate stayed ~0.2% throughout, ruling out DB exhaustion, restarts, and HTTP-level saturation. [F6, F7, F8]
- order-service logged no relevant errors in the window (one unrelated slow-request WARN). [F9]
- Prior similar incidents were attributed to Kafka rebalance/partition reassignment, a different cause than the schema mismatch observed here. [F10]

## Assumptions (not verified)
- settlement-service was not simultaneously updated to accept schema v2 (no such deploy/config appeared in the window).
- orders-3@8841207 is the head/blocking poison offset on partition 3.
- Kafka broker health was normal; no rebalance was captured in the searched windows.

## Timeline
- `2026-05-12T07:35:00Z` Config change cfg-3004: settlement-service consumer.max.poll.records 500->200. [F5]
- `2026-05-12T07:40:00Z` Deploy dep-6010: order-service v2.16.0 renames OrderExecuted field qty->quantity, header v2. [F1]
- `2026-05-12T07:42:14Z` First settlement-service deserialization error (missing required field 'qty'). [F2]
- `2026-05-12T07:44:51Z` Poison-message retry loop begins; partition orders-3 blocked. [F3]
- `2026-05-12T07:36:00Z` consumer_lag begins climbing (774.71 -> 61,605.53 by 09:48). [F4]
- `2026-05-12T08:22:00Z` Incident INC-010 reported (no settlements for ~40 min, lag >60k). [F4]

## Recommended actions
- **[immediate]** Roll back order-service dep-6010 to the previous version (or emit v1-compatible OrderExecuted with qty) to stop producing v2 messages. -- The v2 schema change is the trigger; halting new incompatible events stops the error stream. [F1, F2]
- **[immediate]** Make settlement-service skip or dead-letter the poison offset orders-3@8841207 to unblock partition 3, then replay after the schema fix. -- The retry loop on a blocked partition is why zero settlements post; unblocking restores consumption. [F3, F11]
- **[short_term]** Add schema-compatibility (backward-compatible) deserialization or a v2 handler for OrderExecuted in settlement-service. -- Prevents producer schema evolution from wedging the consumer. [F2]
- **[short_term]** Add a bounded retry + dead-letter path so a single unparseable message cannot block a whole partition. -- The poison-message partition block amplified one bad message into a full outage. [F3, F11]
- **[long_term]** Introduce contract/schema-registry gating in CI for order-service event schema changes. -- Prevents incompatible field renames from reaching consumers. [F1]

## Data gaps
- No direct Kafka broker-side telemetry (rebalance/partition-assignment events) was queried; broker health is inferred from absence of rebalance evidence and restarts=0.
- The settlement-service logs were truncated (409 total matches, 40 returned), so the full set of distinct failing offsets/patterns was not enumerated - all sampled errors are the same qty/v2 pattern.
- No deployment of settlement-service itself was observed, so it is assumed its deserializer was unchanged.

## Investigation stats
- latency 33.74s, LLM calls 4, tokens 35302 (in 29988 / out 5314)
- tool calls 12 (0 failed, 0 partial, 0 recovered by retry), findings 11/11 verified
