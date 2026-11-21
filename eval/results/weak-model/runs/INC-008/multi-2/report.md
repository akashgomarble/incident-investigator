# Incident investigation report: INC-008
*System: multi_agent | outcome: **inconclusive** | ended: budget_exhausted:deadline*

## Summary
The investigation did not identify a definitive root cause; the incident aligns with a sudden request-rate surge that saturated CPU and triggered accept‑queue‑full errors, but without evidence of a code or configuration change driving the surge. Confidence in this assessment is moderate, limited by missing dependency deployment data and lack of thread‑pool metrics.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|

## Facts (verified observations)
- Auth-service had no deployments in the 36‑hour window before the incident. [F1]
- Api-gateway also had no deployments in the same window. [F3]
- Auth-service log level was changed from INFO to WARN at 2026‑04‑22T03:40:00Z. [F2]
- Auth-service p99 latency jumped from ~90 ms to >3700 ms starting at 06:54Z. [F5]
- Auth-service error rate spiked from ~0.2 % to >12 % starting at 06:54Z. [F6]
- First ERROR log indicating 503 responses appears at 06:55:42Z with message "request rejected: accept queue full, returning 503 (POST /v1/auth/login)". [F7]
- All ERROR logs between 06:45‑07:15 share the same accept‑queue‑full pattern. [F8]
- No ERROR logs were recorded before 06:55:42Z. [F9]
- Latency and error rate remained elevated through the latest data point at 08:51Z. [F10]
- Auth-service CPU usage rose sharply to ~96 % at 06:54Z and stayed high through 07:30Z. [F11]
- Auth-service memory usage stayed roughly constant (50‑55 %). [F12]
- Request rate surged from ~390 req/s to >1150 req/s at 06:54Z, peaking at ~1640 req/s at 06:57Z and staying >1400 req/s through 07:30Z. [F13]
- Rate‑limited requests per minute jumped from ~2 to >600 at 06:54Z and stayed around 900‑950 req/min thereafter. [F14]

## Assumptions (not verified)
- The sudden request‑rate surge is external (e.g., client traffic spike) rather than caused by a change in auth-service logic.
- Accept‑queue‑full errors are a symptom of thread‑pool or worker saturation, inferred from CPU saturation and rate‑limit spikes.
- No code or configuration changes in dependent services (user-db, redis-cache, kyc‑provider) occurred that could have introduced latency, given the lack of deployment data.
- (demoted from facts: no verified observation backs it) The timing of CPU saturation, request‑rate surge, and rate‑limit spikes aligns with the onset of high latency and 503 errors.

## Timeline
- `2026-04-22T03:40:00Z` Auth-service log level changed from INFO to WARN. [F2]
- `2026-04-22T06:48:00Z` Baseline request rate ~390 req/s; CPU ~38 % avg. [F11, F13]
- `2026-04-22T06:54:00Z` Request rate surge >1150 req/s; CPU spikes to ~96 %; latency jumps >3.7 s; error rate >12 %; rate‑limit spikes >600 req/min. [F5, F6, F11, F13, F14]
- `2026-04-22T06:55:42Z` First ERROR log: accept queue full, returning 503. [F7]
- `2026-04-22T07:00:00Z` Sustained high latency and error rate; CPU remains >96 %; request rate stays >1400 req/s. [F10, F11, F13]
- `2026-04-22T08:51:00Z` Latency and error rate still elevated (≈3‑4 s, 13‑15 % error). [F10]

## Recommended actions
- **[immediate]** Temporarily throttle incoming login requests at the load balancer or api‑gateway to bring request rate below 800 req/s. -- Reduces pressure on auth-service, allowing CPU to drop and queue to clear; directly addresses the observed request‑rate surge (F13) and accept‑queue‑full errors (F7,F8). [F13, F7, F8]
- **[short_term]** Scale auth-service horizontally (add additional instances) and/or increase CPU limits for existing pods. -- Provides additional processing capacity to handle the high request volume while root cause is investigated; aligns with observed CPU saturation (F11). [F11, F13]
- **[short_term]** Enable detailed thread‑pool and queue‑length metrics for auth-service and set alerts for >80 % queue utilization. -- Current lack of thread‑pool metrics (GAP) prevents confirming that accept‑queue‑full errors are due to pool exhaustion; this metric will validate the inference (F15).
- **[short_term]** Audit deployment and configuration history for user-db, redis-cache, and kyc‑provider, focusing on bcrypt cost parameters and schema changes. -- Missing dependency deployment data (GAP) could hide a change that increased auth latency; confirming no such change will narrow the cause space.
- **[long_term]** Implement finer‑grained (per‑minute) latency, error‑rate, and request‑rate metrics for auth-service. -- Current 9‑minute buckets limit precise onset detection (GAP); finer granularity will improve future incident triage.
- **[long_term]** Review and adjust rate‑limit thresholds for auth-service to ensure they are appropriate for peak traffic patterns. -- Rate‑limit spikes coincided with the incident (F14); proper thresholds can prevent excessive throttling that leads to 503 errors. [F14]
- **[long_term]** Add automated alerts for CPU usage >80 % sustained over 5 minutes on auth-service. -- Early warning of CPU saturation could trigger scaling or throttling before queue saturation occurs. [F11]

## Data gaps
- No deployment or configuration change data for user-db, redis-cache, or kyc-provider; cannot rule out external dependency changes.
- Missing bcrypt cost parameter history; a change could increase auth latency.
- Metrics are aggregated in 9‑minute intervals, limiting precise onset timing.
- No thread‑pool or worker‑queue length metrics for auth-service, preventing direct verification of accept‑queue‑full errors.
- Log query limited to 20 entries; later logs may contain additional patterns.

## What would change this conclusion
- Discovery of a recent code or configuration change (e.g., increased bcrypt cost) in auth-service or a dependent service that aligns with the latency spike would shift the root cause to a software regression.
- Evidence that the request‑rate surge was caused by a client bug or internal loop rather than external traffic would change mitigation focus.
- Thread‑pool metrics showing saturation at the exact time of accept‑queue‑full errors would confirm the queue‑saturation hypothesis as the primary cause.

## Integrity warnings (set by code)
- fact demoted to assumption: The timing of CPU saturation, request‑rate surge, and rate‑limit spikes aligns w

## Investigation stats
- latency 685.14s, LLM calls 19, tokens 63497 (in 52812 / out 10685)
- tool calls 14 (1 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
