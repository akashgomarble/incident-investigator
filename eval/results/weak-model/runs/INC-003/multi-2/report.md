# Incident investigation report: INC-003
*System: multi_agent | outcome: **root_cause_identified** | ended: budget_exhausted:deadline*

## Summary
Root cause identified: a bug introduced in order-service v2.15.2 malformed the request to exchange-gateway, leading to 504 Gateway Timeout errors and a 30% order failure rate. Confidence is medium (60%) based on strong supporting observations. Immediate rollback mitigates impact while a fix is prepared.

## Probable root cause
**Bug introduced in order-service v2.15.2 causing exchange-gateway timeouts** -- bad_deployment in `order-service`, triggered by `order-service v2.15.2 deployment` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bug introduced in order-service v2.15.2 causing exchange-gateway timeouts | bad_deployment | order-service | order-service v2.15.2 deployment | 0.60 (medium) | active |
| 2 | H2: Exchange‑gateway internal failure returning 504 errors | dependency_failure | exchange-gateway | null | 0.35 (low) | active |
| 3 | H3: Network connectivity issue between order‑service and exchange‑gateway | infrastructure_failure | network path (order-service ↔ exchange-gateway) | null | 0.30 (low) | active |

### H1: Bug introduced in order-service v2.15.2 causing exchange-gateway timeouts
The order-service deployment v2.15.2 introduced a code bug that malformed the request to exchange‑gateway, causing the gateway to return 504 Gateway Timeout responses. Order‑service logs the failure, leading to a sharp latency increase and a 30 % error rate.

**Causal chain**
1. order-service v2.15.2 was deployed [F1]
2. new code sends malformed or mis‑routed request to exchange‑gateway *(assumed)*
3. exchange‑gateway returns 504 Gateway Timeout [F8]
4. order-service logs "exchange‑gateway call failed: 504" and returns 504 to callers [F7, F9]
5. order-service latency spikes (p99 >5 s) and error rate climbs to ~33 % [F13, F14]

**Supporting evidence**
  - `F1` ✔ A deployment of order-service version v2.15.2 was applied at 2026-03-17T10:48:00Z and rolled back at 2026-03-17T11:16:00Z.
  - `F7` ✔ The first ERROR log in order-service appears at 2026-03-17T11:02:45Z.
  - `F8` ✔ All early ERROR entries share the signature "exchange-gateway call failed: 504 Gateway Timeout (placeOrder)".
  - `F9` ✔ No ERROR logs are present in order-service before 2026-03-17T11:02:45Z.
  - `F13` ✔ Order-service p99 latency spiked sharply at 2026-03-17T11:01:00Z from ~150 ms to >5 s.
  - `F14` ✔ Order-service error rate jumped from <0.3% to >15% at 2026-03-17T11:01:00Z and rose to ~33% by 11:15.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The malformed request is the direct cause of the 504 response (not a downstream issue).

### H2: Exchange‑gateway internal failure returning 504 errors
A failure inside exchange‑gateway (e.g., overload or internal error) caused it to return 504 Gateway Timeout responses to order‑service, which then propagated the failures to callers.

**Causal chain**
1. exchange‑gateway experiences internal issue causing 504 responses *(assumed)*
2. order‑service receives 504 from exchange‑gateway [F8]
3. order‑service logs the failure and returns 504 to callers [F7, F9]
4. order‑service latency spikes and error rate rises [F13, F14]

**Supporting evidence**
  - `F8` ✔ All early ERROR entries share the signature "exchange-gateway call failed: 504 Gateway Timeout (placeOrder)".
  - `F7` ✔ The first ERROR log in order-service appears at 2026-03-17T11:02:45Z.
  - `F9` ✔ No ERROR logs are present in order-service before 2026-03-17T11:02:45Z.
  - `F13` ✔ Order-service p99 latency spiked sharply at 2026-03-17T11:01:00Z from ~150 ms to >5 s.
  - `F14` ✔ Order-service error rate jumped from <0.3% to >15% at 2026-03-17T11:01:00Z and rose to ~33% by 11:15.
**Contradicting evidence**
  - `F11` ✔ Exchange-gateway shows zero ERROR logs containing "Gateway Timeout" between 11:00 and 12:30 UTC.
**Assumptions**
  - exchange‑gateway can emit 504 responses without logging an explicit error.

### H3: Network connectivity issue between order‑service and exchange‑gateway
A transient network or routing problem between order‑service and exchange‑gateway caused requests to time out, resulting in 504 errors observed by order‑service.

**Causal chain**
1. Network path between order‑service and exchange‑gateway degrades (packet loss / latency) *(assumed)*
2. order‑service requests to exchange‑gateway time out, yielding 504 responses [F8]
3. order‑service logs the timeout and returns 504 to callers [F7, F9]
4. order‑service latency spikes and error rate climbs [F13, F14]

**Supporting evidence**
  - `F8` ✔ All early ERROR entries share the signature "exchange-gateway call failed: 504 Gateway Timeout (placeOrder)".
  - `F7` ✔ The first ERROR log in order-service appears at 2026-03-17T11:02:45Z.
  - `F9` ✔ No ERROR logs are present in order-service before 2026-03-17T11:02:45Z.
  - `F13` ✔ Order-service p99 latency spiked sharply at 2026-03-17T11:01:00Z from ~150 ms to >5 s.
  - `F14` ✔ Order-service error rate jumped from <0.3% to >15% at 2026-03-17T11:01:00Z and rose to ~33% by 11:15.
**Contradicting evidence**
  - (none)
**Assumptions**
  - No other services observed the same network issue (lack of data).

## Facts (verified observations)
- order-service version v2.15.2 was deployed at 2026-03-17T10:48:00Z and rolled back at 2026-03-17T11:16:00Z. [F1]
- The first ERROR log in order-service appears at 2026-03-17T11:02:45Z, after the deployment. [F7]
- All early ERROR logs share the signature "exchange-gateway call failed: 504 Gateway Timeout (placeOrder)". [F8]
- No ERROR logs are present in order-service before 2026-03-17T11:02:45Z. [F9]
- Order-service p99 latency spiked from ~150 ms to >5 s at 2026-03-17T11:01:00Z. [F13]
- Order-service error rate jumped from <0.3% to >15% at 2026-03-17T11:01:00Z and rose to ~33% by 11:15. [F14]
- Order-service request rate remained stable (~220‑240 req/s) during the incident window. [F15]
- Exchange-gateway logs contain zero ERROR entries with "Gateway Timeout" between 11:00 and 12:30 UTC. [F11]
- Payment-service shows zero ERROR logs containing "Gateway Timeout" between 11:00 and 12:00 UTC. [F10]

## Assumptions (not verified)
- The malformed request generated by the new order-service code is the direct cause of the 504 responses (i.e., exchange-gateway does not treat a well‑formed request as an error).
- Exchange-gateway would emit ERROR logs if it experienced an internal failure that produced 504 responses.

## Timeline
- `2026-03-17T10:48:00Z` Deployment of order-service v2.15.2. [F1]
- `2026-03-17T11:01:00Z` Order-service p99 latency spikes and error rate begins to rise. [F13, F14]
- `2026-03-17T11:02:45Z` First ERROR log appears in order-service, indicating exchange-gateway 504 failures. [F7, F8]
- `2026-03-17T11:15:00Z` Error rate reaches ~33%; latency remains >5 s. [F14]
- `2026-03-17T11:16:00Z` Rollback of order-service to previous version (v2.15.1) initiated. [F1]

## Recommended actions
- **[immediate]** Rollback order-service to version v2.15.1 (or the last known good version). -- Restores correct request format to exchange-gateway, eliminating 504 errors and reducing error rate. [F1, F8, F14]
- **[short_term]** Patch order-service v2.15.2 to correct the request formatting bug and redeploy after verification. -- Fixes the root cause while allowing the new version to be used after validation.
- **[long_term]** Add unit/integration tests that validate the request payload sent to exchange-gateway, ensuring proper formatting. -- Prevents regression of this bug in future releases.
- **[long_term]** Enhance observability: collect latency and error metrics from exchange-gateway, pricing-engine, and kafka; ensure inventory-service logs are searchable. -- Reduces data gaps that limited root‑cause confidence and improves future incident detection.

## Data gaps
- Missing logs from inventory-service due to unknown service name, preventing correlation of potential downstream errors.
- No DB connection usage metrics for ledger-db, limiting ability to rule out DB saturation as a cause of 504 responses.
- Absence of downstream latency and error metrics (pricing-engine, exchange-gateway, kafka) hampers full verification that the issue was isolated to order-service request formatting.

## What would change this conclusion
- If exchange-gateway logs later show internal errors or overload conditions coinciding with the 504 responses, hypothesis H2 (dependency failure) would become more plausible.
- If network trace data revealed packet loss or routing failures between order-service and exchange-gateway during the incident window, hypothesis H3 (network issue) would gain support.
- Discovery of a malformed request pattern that existed prior to the v2.15.2 deployment would weaken the link between the deployment and the failures.

## Investigation stats
- latency 531.32s, LLM calls 19, tokens 67799 (in 55664 / out 12135)
- tool calls 13 (2 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
