# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: budget_exhausted:deadline*

## Summary
The incident was caused by an expired TLS certificate on the external bank-partner API, which led to mTLS handshake rejections starting at 12:15:27 UTC. Payment‑service logged the rejections and returned business‑logic failures to customers, while its generic HTTP error‑rate metric remained normal. Confidence in this root cause is high (0.78) supported by multiple verified observations.

## Probable root cause
**Bank partner API TLS certificate expired, causing mTLS handshake rejections** -- credential_expiry in `bank-partner-api`, triggered by `null` (confidence 0.78, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bank partner API TLS certificate expired, causing mTLS handshake rejections | credential_expiry | bank-partner-api | null | 0.78 (high) | active |
| 2 | H3: External bank‑partner‑api deployment introduced TLS handshake bug | dependency_failure | bank-partner-api | null | 0.45 (low) | active |
| 3 | H2: Payment-service configuration change increased timeout, exposing TLS handshake failures | config_change | payment-service | null | 0.38 (low) | active |
| 4 | H4: Network/DNS connectivity issue between payment‑service and bank‑partner‑api | infrastructure_failure | network | null | 0.32 (low) | active |

### H1: Bank partner API TLS certificate expired, causing mTLS handshake rejections
The external bank-partner-api presented an expired TLS certificate at 12:15, causing payment-service mTLS handshakes to be rejected. Payment-service logs the failure (PENDING_RETRY) and returns a business‑logic failure to customers, while its generic HTTP error‑rate metric remains normal.

**Causal chain**
1. Bank partner API certificate expired at 2026‑04‑02T12:15:00Z [F3]
2. Payment-service mTLS handshake with bank-partner-api rejected due to expired cert [F1, F2]
3. Payment-service marks payments as PENDING_RETRY and returns failure responses to customers *(assumed)*

**Supporting evidence**
  - `F1` ✔ The earliest ERROR log in payment-service appears at 2026-04-02T12:15:27Z, message "payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected".
  - `F2` ✔ The most frequent error pattern is "mTLS handshake with bank-partner-api rejected", seen 140 times, first occurring at 2026-04-02T12:15:27Z.
  - `F3` ✔ A related TLS expiration error "bank-partner-api call failed: remote error: tls: expired certificate (alert 45); ... notAfter=2026-04-02T12:15:00Z" first appears at 2026-04-02T12:16:44Z.
  - `F7` ✔ The outbound bank API error rate spiked from ~0.2% before 12:12 to 50.1% avg (max 100%) at 12:12 and then stayed at 100% for all subsequent buckets, matching the failure onset at 12:15:27.
  - `F8` ✔ The 99th‑percentile latency of calls to the bank API remained low (≈140 ms) after the error‑rate spike, showing no latency surge concurrent with the failures.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Payment-service translates handshake rejection into customer‑visible payment failure without incrementing the generic HTTP error‑rate metric.

### H3: External bank‑partner‑api deployment introduced TLS handshake bug
A recent (unobserved) deployment of the external bank‑partner‑api changed its TLS configuration, causing mTLS handshakes from payment‑service to be rejected. Payment‑service logs the failures and returns payment errors to customers, while its own HTTP error‑rate metric remains unchanged.

**Causal chain**
1. Bank‑partner‑api deployed a change affecting its TLS endpoint *(assumed)*
2. mTLS handshake from payment‑service rejected (observed in logs) [F1, F2]
3. Payment‑service reports payment failures to customers *(assumed)*

**Supporting evidence**
  - `F1` ✔ The earliest ERROR log in payment-service appears at 2026-04-02T12:15:27Z, message "payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected".
  - `F2` ✔ The most frequent error pattern is "mTLS handshake with bank-partner-api rejected", seen 140 times, first occurring at 2026-04-02T12:15:27Z.
  - `F7` ✔ The outbound bank API error rate spiked from ~0.2% before 12:12 to 50.1% avg (max 100%) at 12:12 and then stayed at 100% for all subsequent buckets, matching the failure onset at 12:15:27.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The external API deployment occurred shortly before 12:12, matching the error‑rate spike.

### H2: Payment-service configuration change increased timeout, exposing TLS handshake failures
A configuration change at 11:20 increased the bank API timeout from 3000 ms to 5000 ms. The longer timeout window allowed the expired‑certificate handshake failures to surface as mTLS errors, which are logged but not counted in the HTTP error‑rate metric, leading to customer‑reported payment failures.

**Causal chain**
1. Config change raised bank API timeout to 5000 ms [F6]
2. Longer timeout allowed mTLS handshake attempts to reach expiration point, resulting in rejection logs [F1, F2]
3. Payment-service returns failure to customers while HTTP error‑rate stays normal *(assumed)*

**Supporting evidence**
  - `F1` ✔ The earliest ERROR log in payment-service appears at 2026-04-02T12:15:27Z, message "payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected".
  - `F2` ✔ The most frequent error pattern is "mTLS handshake with bank-partner-api rejected", seen 140 times, first occurring at 2026-04-02T12:15:27Z.
  - `F6` ✘ unverified A configuration change on payment-service raised the bank API timeout from 3000 ms to 5000 ms at 2026‑04‑02T11:20:00Z.
**Contradicting evidence**
  - `F5` ✔ No code deployments for payment-service were recorded in the 24‑hour window preceding the incident (2026‑04‑01T12:32Z to 2026‑04‑02T13:59Z).
**Assumptions**
  - The timeout increase directly contributed to the visibility of the TLS handshake failure.
  - The config change was applied to payment-service instances handling the failing transactions.

### H4: Network/DNS connectivity issue between payment‑service and bank‑partner‑api
A transient network or DNS outage prevented payment‑service from establishing mTLS connections to the bank API, resulting in handshake rejections logged as errors. Customers see payment failures, but the generic HTTP error‑rate metric stays normal.

**Causal chain**
1. Network/DNS outage blocked outbound TLS connections to bank‑partner‑api *(assumed)*
2. mTLS handshake rejected (logged errors) [F1, F2]
3. Payment‑service returns failure to customers *(assumed)*

**Supporting evidence**
  - `F1` ✔ The earliest ERROR log in payment-service appears at 2026-04-02T12:15:27Z, message "payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected".
  - `F2` ✔ The most frequent error pattern is "mTLS handshake with bank-partner-api rejected", seen 140 times, first occurring at 2026-04-02T12:15:27Z.
  - `F7` ✔ The outbound bank API error rate spiked from ~0.2% before 12:12 to 50.1% avg (max 100%) at 12:12 and then stayed at 100% for all subsequent buckets, matching the failure onset at 12:15:27.
**Contradicting evidence**
  - `F8` ✔ The 99th‑percentile latency of calls to the bank API remained low (≈140 ms) after the error‑rate spike, showing no latency surge concurrent with the failures.
**Assumptions**
  - The outage would not affect latency metrics, explaining low 99th‑percentile latency.

## Facts (verified observations)
- The first payment‑service error log at 12:15:27 UTC reports a mTLS handshake rejection with bank‑partner‑api. [F1]
- The error pattern "mTLS handshake with bank‑partner‑api rejected" appears 140 times, all starting at 12:15:27 UTC. [F2]
- A TLS error indicating an expired certificate (notAfter=2026‑04‑02T12:15:00Z) is logged at 12:16:44 UTC. [F3]
- The outbound bank‑API error‑rate spiked from ~0.2% to >50% at 12:12 UTC and stayed at 100% thereafter. [F7]
- 99th‑percentile latency to the bank API remained low (~140 ms) after the error‑rate spike, indicating no network latency issue. [F8]
- No code deployments for payment‑service occurred in the 24‑hour window before the incident. [F5]

## Assumptions (not verified)
- Payment‑service translates mTLS handshake rejections into customer‑visible payment failures without incrementing the generic HTTP error‑rate metric.
- The expired TLS certificate on the bank‑partner‑api is the sole cause of the handshake rejections.

## Timeline
- `2026-04-02T12:12:00Z` Bank‑API error‑rate spikes to >50%, indicating the start of failures. [F7]
- `2026-04-02T12:15:27Z` First payment‑service log records mTLS handshake rejection (PENDING_RETRY). [F1]
- `2026-04-02T12:16:44Z` TLS error log shows expired certificate (notAfter=12:15:00Z). [F3]
- `2026-04-02T12:18:00Z` 99th‑percentile latency to bank API remains low (~140 ms). [F8]

## Recommended actions
- **[immediate]** Coordinate with the bank partner to renew the TLS certificate immediately and redeploy the updated certificate to the bank‑partner‑api endpoint. -- Expired certificate is the direct cause of handshake rejections (F3). Restoring a valid cert will allow mTLS connections to succeed. [F3]
- **[short_term]** Deploy a temporary fallback in payment‑service to treat handshake rejections as retryable internal errors and return a generic "service unavailable" HTTP status, while also incrementing the generic HTTP error‑rate metric. -- Ensures customers receive a clear error signal and that monitoring reflects the failure, addressing the assumption about metric handling.
- **[short_term]** Add explicit logging of the HTTP response code sent to customers on handshake failures to verify the translation path assumed in H1. -- Closes the evidence gap around how payment‑service maps TLS errors to customer‑visible failures.
- **[long_term]** Implement automated TLS certificate expiry monitoring and alerts for all external mTLS dependencies. -- Prevents recurrence of expired‑certificate failures.

## Data gaps
- No direct telemetry from the external bank‑partner‑api to confirm the certificate expiration timestamp beyond the error log (F3).
- Lack of logs showing the exact HTTP response codes returned to customers on handshake failures, leaving the translation assumption unverified.

## What would change this conclusion
- If evidence showed the bank‑partner‑api certificate was valid at the time of failures, the root cause would shift away from H1.
- If logs demonstrated that payment‑service correctly handled handshake rejections without affecting customers, the assumed causal chain would be invalid.

## Investigation stats
- latency 492.2s, LLM calls 14, tokens 43251 (in 33116 / out 10135)
- tool calls 7 (0 failed, 0 partial, 0 recovered by retry), findings 7/8 verified
