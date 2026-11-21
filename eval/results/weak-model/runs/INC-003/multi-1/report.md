# Incident investigation report: INC-003
*System: multi_agent | outcome: **root_cause_identified** | ended: budget_exhausted:deadline*

## Summary
The investigation could not definitively identify a root cause; while the leading hypothesis (a bug in order-service v2.15.2 causing exchange-gateway timeouts) has the highest confidence, it lacks direct evidence. The observed simultaneous spikes in exchange-gateway latency and error rate (F17, F18) and the timing of order-service errors (F8, F9, F11) are consistent with multiple plausible causes. Further data collection is required to discriminate between a deployment bug, exchange-gateway degradation, or a network issue.

## Probable root cause
**Bug introduced in order-service v2.15.2 causing exchange-gateway timeouts** -- bad_deployment in `order-service`, triggered by `F1` (confidence 0.50, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bug introduced in order-service v2.15.2 causing exchange-gateway timeouts | bad_deployment | order-service | F1 | 0.50 (medium) | active |
| 2 | H2: Exchange-gateway degradation causing order-service timeouts | dependency_failure | exchange-gateway | null | 0.48 (low) | active |
| 3 | H3: Network connectivity issue between order-service and exchange-gateway | infrastructure_failure | network | null | 0.35 (low) | active |

### H1: Bug introduced in order-service v2.15.2 causing exchange-gateway timeouts
The deployment of order-service version v2.15.2 introduced a code defect that sends malformed or delayed requests to exchange-gateway, which responds with 504 Gateway Timeout, leading to a surge in order-service error rate.

**Causal chain**
1. order-service v2.15.2 was deployed [F1]
2. code defect causes exchange-gateway calls to fail with 504 *(assumed)*
3. exchange-gateway returns 504 for placeOrder calls [F8]
4. order-service error rate spikes to ~30% and latency >5s [F9, F11]

**Supporting evidence**
  - `F1` ✘ unverified A deployment of order-service version v2.15.2 was released at 2026-03-17T10:48:00Z and rolled back at 2026-03-17T11:16:00Z.
  - `F7` ✔ The first error log from order-service appears at 2026-03-17T11:02:45Z.
  - `F8` ✔ All early errors share the signature "exchange-gateway call failed: 504 Gateway Timeout (placeOrder)" indicating the placeOrder endpoint is failing.
  - `F9` ✔ order-service error_rate was ~0.2% before 11:00 UTC and jumped to 19.02% (avg) at 11:00 UTC, then stabilized around 30% thereafter.
  - `F11` ✔ p99 latency was ~150 ms before 11:00 UTC, then spiked to 3264 ms (avg) at 11:00 UTC and stayed above 5 s for the rest of the period, with the latest avg 5305 ms at 12:55 UTC.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The code change in v2.15.2 modifies the request to exchange-gateway in a way that triggers timeouts.

### H2: Exchange-gateway degradation causing order-service timeouts
A performance degradation or outage in the exchange-gateway service caused it to return 504 Gateway Timeout for placeOrder calls, which manifested as a high error rate in order-service.

**Causal chain**
1. exchange-gateway experiences latency or failure resulting in 504 responses *(assumed)*
2. order-service receives 504 from exchange-gateway [F8]
3. order-service error rate spikes to ~30% and latency >5s [F9, F11]

**Supporting evidence**
  - `F8` ✔ All early errors share the signature "exchange-gateway call failed: 504 Gateway Timeout (placeOrder)" indicating the placeOrder endpoint is failing.
  - `F9` ✔ order-service error_rate was ~0.2% before 11:00 UTC and jumped to 19.02% (avg) at 11:00 UTC, then stabilized around 30% thereafter.
  - `F11` ✔ p99 latency was ~150 ms before 11:00 UTC, then spiked to 3264 ms (avg) at 11:00 UTC and stayed above 5 s for the rest of the period, with the latest avg 5305 ms at 12:55 UTC.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The exchange-gateway degradation began around 11:00 UTC, coincident with the observed error spike.

### H3: Network connectivity issue between order-service and exchange-gateway
A network partition or increased packet loss between order-service and exchange-gateway caused HTTP requests to time out, resulting in 504 errors and elevated order-service failure rates.

**Causal chain**
1. Network issues (latency, loss) affect traffic to exchange-gateway *(assumed)*
2. order-service receives 504 Gateway Timeout from exchange-gateway [F8]
3. order-service error rate spikes to ~30% and latency >5s [F9, F11]

**Supporting evidence**
  - `F8` ✔ All early errors share the signature "exchange-gateway call failed: 504 Gateway Timeout (placeOrder)" indicating the placeOrder endpoint is failing.
  - `F9` ✔ order-service error_rate was ~0.2% before 11:00 UTC and jumped to 19.02% (avg) at 11:00 UTC, then stabilized around 30% thereafter.
  - `F11` ✔ p99 latency was ~150 ms before 11:00 UTC, then spiked to 3264 ms (avg) at 11:00 UTC and stayed above 5 s for the rest of the period, with the latest avg 5305 ms at 12:55 UTC.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Network metrics would show degradation starting around 11:00 UTC.

## Facts (verified observations)
- No other service deployments occurred in the last 24 hours [F2]
- order-service directly calls ledger-db, pricing-engine, exchange-gateway, and kafka [F4]
- The first order-service error log appears at 2026-03-17T11:02:45Z [F7]
- All early order-service errors share the signature "exchange-gateway call failed: 504 Gateway Timeout (placeOrder)" [F8]
- order-service error_rate jumped from ~0.2% to ~19% at 11:00 UTC and stabilized around 30% thereafter [F9, F10]
- p99 latency for order-service spiked from ~150 ms to >5 s after 11:00 UTC [F11]
- The deployment added an Idempotency-Key header and a retry‑safe submit handler, the only documented changes affecting the exchange‑gateway client [F13]
- exchange-gateway first timeout log appears at 2026-03-17T11:01:25Z [F15]
- All exchange-gateway timeout logs share the pattern "upstream connect timeout after <n>ms host=api.exchange-partner.example op=placeOrder" [F16]
- exchange-gateway p99 latency jumped from ~130 ms to >5 s at 11:00 UTC [F17]
- exchange-gateway error_rate spiked from ~0.2% to ~30% at 11:00 UTC and later reached 40‑47% [F18]
- CPU usage of order-service remained around 40% with no spike [F12]

## Assumptions (not verified)
- The code change in v2.15.2 modifies the request to exchange-gateway in a way that triggers timeouts (H1).
- The exchange-gateway degradation began around 11:00 UTC, coincident with the observed error spike (H2).
- Network metrics would show degradation starting around 11:00 UTC if a network issue were present (H3).
- (demoted from facts: no verified observation backs it) order-service v2.15.2 was deployed at 2026-03-17T10:48:00Z and rolled back at 2026-03-17T11:16:00Z

## Timeline
- `2026-03-17T09:20:00Z` Pricing-engine cache TTL changed from 5 to 4 seconds [F3]
- `2026-03-17T10:48:00Z` order-service v2.15.2 deployed [F1, F13, F14]
- `2026-03-17T11:00:00Z` order-service error_rate and latency begin to spike; exchange-gateway latency and error_rate also spike [F9, F11, F17, F18]
- `2026-03-17T11:01:25Z` First exchange-gateway timeout log for placeOrder appears [F15]
- `2026-03-17T11:02:45Z` First order-service error log appears [F7]
- `2026-03-17T11:16:00Z` order-service v2.15.2 rolled back [F1]
- `2026-03-17T12:55:00Z` order-service error_rate ~31% and p99 latency >5 s [F10, F11]

## Recommended actions
- **[immediate]** Confirm rollback of order-service v2.15.2 is complete and monitor for error_rate reduction -- Rollback removes the only recent code change; if the bug is the cause, error metrics should improve. [F1]
- **[short_term]** Collect detailed code diff for v2.15.2, focusing on request payload, timeout settings, and retry logic for exchange-gateway client -- Direct evidence is needed to confirm or refute H1's assumption that the new header/retry logic triggers timeouts.
- **[short_term]** Obtain exchange-gateway internal metrics (CPU, memory, thread pools) and logs around 11:00 UTC -- If exchange-gateway itself degraded, its own resource usage or error logs would show anomalies, supporting H2.
- **[short_term]** Gather network path health data (packet loss, latency, jitter) between order-service and exchange-gateway for the incident window -- Network degradation would manifest in packet loss or increased RTT, addressing the gap for H3.
- **[long_term]** Implement monitoring for packet loss and connection health for all critical service-to-service links -- Prevents future blind spots similar to the current data gaps.
- **[long_term]** Add automated regression tests that validate exchange-gateway request format and timeout behavior after deployments -- Ensures future code changes that affect downstream calls are caught before release.

## Data gaps
- Missing memory usage and DB connection pool metrics for order-service (cannot assess resource pressure).
- No detailed code diff or configuration values for the exchange-gateway client in v2.15.2 (cannot confirm payload or timeout changes).
- No packet loss or other network health metrics for the order-service ↔ exchange-gateway path (cannot evaluate network hypothesis).
- No internal metrics or logs from exchange-gateway to determine if it experienced its own degradation.

## What would change this conclusion
- If the code diff shows that the Idempotency-Key header or retry logic modifies request timing or size in a way that triggers exchange-gateway timeouts, H1 would become strongly supported.
- If exchange-gateway internal logs reveal a spike in resource usage, thread pool exhaustion, or a separate failure at 11:00 UTC, H2 would be confirmed.
- If network telemetry shows a sudden increase in packet loss or latency between the two services at the incident onset, H3 would be validated.

## Integrity warnings (set by code)
- reporter wrote outcome=inconclusive; overridden to root_cause_identified by rule (top hypothesis is above threshold and clearly ahead)
- fact demoted to assumption: order-service v2.15.2 was deployed at 2026-03-17T10:48:00Z and rolled back at 20

## Investigation stats
- latency 517.67s, LLM calls 30, tokens 109743 (in 92582 / out 17161)
- tool calls 20 (5 failed, 0 partial, 0 recovered by retry), findings 17/19 verified
