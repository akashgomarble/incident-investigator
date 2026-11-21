# Incident investigation report: INC-008
*System: multi_agent | outcome: **root_cause_identified** | ended: budget_exhausted:deadline*

## Summary
The incident was caused by an abrupt external traffic spike that saturated auth-service CPU and filled its accept queue, leading to high latency and 503 errors. This conclusion is supported by multiple verified observations of request rate, CPU usage, and accept‑queue errors. Confidence is medium (0.60) and the evidence clearly favors the traffic‑spike hypothesis over alternatives.

## Probable root cause
**Traffic spike caused CPU saturation and accept queue full in auth-service** -- capacity_saturation in `auth-service`, triggered by `null` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Traffic spike caused CPU saturation and accept queue full in auth-service | capacity_saturation | auth-service | null | 0.60 (medium) | active |
| 2 | H2: Undetected bcrypt cost increase (code/config change) raised CPU per request | bad_deployment | auth-service | null | 0.32 (low) | active |
| 3 | H3: Downstream user‑db latency degraded auth‑service request handling | dependency_failure | user-db | null | 0.22 (low) | active |

### H1: Traffic spike caused CPU saturation and accept queue full in auth-service
An external surge in request volume increased auth-service load, driving CPU usage near 100%, which saturated the accept queue, resulting in high latency and 503 errors.

**Causal chain**
1. Request rate jumped from ~395 req/s to >1,100 req/s at 06:54 UTC [F12]
2. CPU usage surged to ~97 % and stayed high [F10]
3. Accept queue became full, producing 503 errors [F3, F4]
4. Latency p99 rose to >3.7 s and error rate climbed to ~14 % [F1, F2, F14]
5. Rate‑limiting exploded, indicating internal throttling response [F13]
6. Overall pattern points to resource saturation from traffic overload [F15]

**Supporting evidence**
  - `F1` ✔ auth-service p99 latency jumped from ~90 ms to >3.7 s starting at the 06:54 UTC bucket
  - `F2` ✔ auth-service error_rate rose from ~0.2 % to ~13 % in the same 06:54 UTC bucket
  - `F3` ✔ First logged 503 error message appears at 06:55:42 UTC: "request rejected: accept queue full, returning 503 (POST /v1/auth/login)"
  - `F4` ✔ All error logs in the 06:45‑07:15 window share the same pattern: "request rejected: accept queue full, returning 503 (POST /v1/auth/login)" (59 occurrences)
  - `F10` ✔ CPU usage of auth-service surged to near‑full capacity, averaging 96.86 % (max 97.33 %) at 06:54 UTC and remaining above 96 % through 07:15 UTC.
  - `F12` ✔ Request rate jumped from ~395 req/s (06:48 UTC) to 1,154 req/s (max 1,275 req/s) at 06:54 UTC and peaked at 1,642 req/s (max 1,814 req/s) at 06:57 UTC.
  - `F13` ✔ Rate‑limited requests per minute exploded from ~2 req/min before 06:51 UTC to 607 req/min (max 919 req/min) at 06:54 UTC and stayed above 850 req/min thereafter.
  - `F14` ✔ Error rate rose sharply from <0.3 % to 9.25 % (max 14.33 %) at 06:54 UTC and remained around 14 % through 07:15 UTC.
  - `F15` ✔ The onset of high CPU, request volume, rate‑limiting and error rate aligns with the first logged "accept queue full" 503 error at 06:55:42 UTC, indicating resource saturation as a likely contributor to the latency spike.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The traffic increase originated externally and was not triggered by a recent code or config change in auth‑service.

### H2: Undetected bcrypt cost increase (code/config change) raised CPU per request
A recent, undocumented change to the bcrypt work factor (or related code) increased CPU consumption per authentication request, leading to CPU saturation, accept‑queue overflow and the observed latency and 503 errors.

**Causal chain**
1. Bcrypt cost or related authentication code was increased *(assumed)*
2. Higher per‑request CPU caused overall CPU usage to near 100 % [F10]
3. Accept queue filled, generating 503 errors [F3, F4]
4. Latency and error rate spiked as a result [F1, F2, F14]

**Supporting evidence**
  - `F10` ✔ CPU usage of auth-service surged to near‑full capacity, averaging 96.86 % (max 97.33 %) at 06:54 UTC and remaining above 96 % through 07:15 UTC.
  - `F1` ✔ auth-service p99 latency jumped from ~90 ms to >3.7 s starting at the 06:54 UTC bucket
  - `F2` ✔ auth-service error_rate rose from ~0.2 % to ~13 % in the same 06:54 UTC bucket
  - `F3` ✔ First logged 503 error message appears at 06:55:42 UTC: "request rejected: accept queue full, returning 503 (POST /v1/auth/login)"
  - `F4` ✔ All error logs in the 06:45‑07:15 window share the same pattern: "request rejected: accept queue full, returning 503 (POST /v1/auth/login)" (59 occurrences)
  - `F14` ✔ Error rate rose sharply from <0.3 % to 9.25 % (max 14.33 %) at 06:54 UTC and remained around 14 % through 07:15 UTC.
**Contradicting evidence**
  - `F6` ✔ No deployments of auth-service were recorded between 2026-04-21T00:00:00Z and the incident onset at 2026-04-22T07:06:00Z.
**Assumptions**
  - A deployment or config change affecting bcrypt cost occurred but was not captured in the deployment logs.
  - The change preceded the incident onset.

### H3: Downstream user‑db latency degraded auth‑service request handling
A slowdown or failure in the user database caused auth‑service request threads to block, filling the accept queue, exhausting CPU, and producing high latency and 503 errors.

**Causal chain**
1. User‑db began responding slowly or intermittently *(assumed)*
2. Auth‑service request handling blocked, increasing CPU and queue length [F10]
3. Accept queue filled, leading to 503 errors [F3, F4]
4. Observed latency and error spikes [F1, F2, F14]

**Supporting evidence**
  - `F10` ✔ CPU usage of auth-service surged to near‑full capacity, averaging 96.86 % (max 97.33 %) at 06:54 UTC and remaining above 96 % through 07:15 UTC.
  - `F3` ✔ First logged 503 error message appears at 06:55:42 UTC: "request rejected: accept queue full, returning 503 (POST /v1/auth/login)"
  - `F4` ✔ All error logs in the 06:45‑07:15 window share the same pattern: "request rejected: accept queue full, returning 503 (POST /v1/auth/login)" (59 occurrences)
  - `F1` ✔ auth-service p99 latency jumped from ~90 ms to >3.7 s starting at the 06:54 UTC bucket
  - `F2` ✔ auth-service error_rate rose from ~0.2 % to ~13 % in the same 06:54 UTC bucket
  - `F14` ✔ Error rate rose sharply from <0.3 % to 9.25 % (max 14.33 %) at 06:54 UTC and remained around 14 % through 07:15 UTC.
**Contradicting evidence**
  - (none)
**Assumptions**
  - User‑db experienced a latency regression at ~06:54 UTC (not captured in the current data).
  - No deployment or config change in auth‑service contributed directly.

## Facts (verified observations)
- Auth-service latency jumped from ~90 ms to >3.7 s at 06:54 UTC. [F1]
- Auth-service error rate rose from ~0.2 % to ~13 % at 06:54 UTC. [F2]
- First "accept queue full" 503 error logged at 06:55:42 UTC. [F3]
- All 503 errors in the 06:45‑07:15 window share the same "accept queue full" pattern (59 occurrences). [F4]
- CPU usage of auth-service surged to ~97 % starting at 06:54 UTC and stayed above 96 % through 07:15 UTC. [F10]
- Memory usage remained stable (~52 %) during the incident. [F11]
- Request rate jumped from ~395 req/s to >1,600 req/s between 06:54 UTC and 06:57 UTC. [F12]
- Rate‑limited requests exploded from ~2 req/min to >600 req/min at 06:54 UTC. [F13]
- Error rate rose sharply to ~14 % after 06:54 UTC. [F14]
- No auth-service deployments occurred between 2026‑04‑21T00:00:00Z and incident onset. [F6]
- A log level change from INFO to WARN was applied at 03:40 UTC, unrelated to performance. [F9]

## Assumptions (not verified)
- The traffic increase originated externally and was not triggered by a recent code or configuration change in auth‑service.
- No hidden deployments or config changes (e.g., bcrypt cost) occurred that were not captured in the available logs.

## Timeline
- `2026-04-22T06:48:00Z` Baseline request rate ~395 req/s. [F12]
- `2026-04-22T06:54:00Z` Request rate spikes to >1,600 req/s; CPU usage jumps to ~97 %; latency and error rate begin to rise; rate‑limited requests surge. [F10, F12, F13, F14, F1, F2]
- `2026-04-22T06:55:42Z` First "accept queue full" 503 error logged. [F3]
- `2026-04-22T06:55:42Z – 07:15:00Z` Continued high CPU, sustained request surge, persistent 503 errors (59 total). [F4, F10, F12, F13, F14]
- `2026-04-22T07:06:00Z` Incident reported by on‑call SRE. []

## Recommended actions
- **[immediate]** Enable temporary traffic throttling or rate‑limit inbound login requests to bring request rate below 1,000 req/s. -- Reduces load on auth-service, allowing CPU to recover and accept queue to drain. [F12, F10, F4]
- **[short_term]** Scale auth-service horizontally (add additional pod instances) and verify autoscaling policies trigger at lower thresholds. -- Additional capacity will absorb traffic spikes and prevent CPU saturation. [F10, F12]
- **[short_term]** Review and tighten rate‑limiting configuration (e.g., increase limit burst size, adjust back‑off) to avoid accept‑queue overflow. -- Current rate‑limit spikes correlate with queue saturation; better limits can protect the service. [F13, F4]
- **[long_term]** Implement finer‑grained (1‑minute) monitoring for latency, error rate, CPU, and request volume. -- Current 9‑minute buckets obscure precise onset; higher resolution will improve detection and root‑cause speed.
- **[long_term]** Add thread‑pool usage metrics and alerts for auth-service. -- Missing thread‑pool data limits ability to confirm queue‑full causes; future incidents can be diagnosed more precisely.
- **[long_term]** Conduct a post‑mortem review of traffic forecasting and capacity planning processes. -- To ensure anticipated traffic spikes are covered by existing capacity and autoscaling rules.

## Data gaps
- Metric buckets are 9‑minute intervals; lack of 1‑minute granularity limits pinpointing the exact minute of onset.
- No deployment or configuration data for user‑db, bcrypt cost parameters, or external kyc‑provider service; cannot fully rule out dependency‑related causes.
- Thread‑pool size/usage metrics for auth‑service were unavailable, preventing direct verification of thread‑pool exhaustion.

## What would change this conclusion
- Discovery of a recent bcrypt cost increase or related code change that raises per‑request CPU usage.
- Evidence of a latency regression or schema change in the user‑db occurring at the incident onset.
- Identification of a hidden deployment or configuration change in auth‑service that coincides with the spike.

## Investigation stats
- latency 910.26s, LLM calls 22, tokens 73871 (in 61166 / out 12705)
- tool calls 15 (1 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
