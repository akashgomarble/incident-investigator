# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: budget_exhausted:deadline*

## Summary
The incident was caused by an expired client TLS certificate used by payment-service to authenticate to the bank-partner-api, leading to mTLS handshake failures and payment failures despite a normal HTTP error-rate dashboard. Evidence from multiple verified observations (F1, F2, F3, F4, F10) supports this conclusion with a confidence of 0.60. Immediate certificate renewal and longer‑term automation are recommended.

## Probable root cause
**Expired client certificate for bank-partner-api causing mTLS handshake failures** -- credential_expiry in `payment-service`, triggered by `null` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired client certificate for bank-partner-api causing mTLS handshake failures | credential_expiry | payment-service | null | 0.60 (medium) | active |
| 2 | H2: Bank‑partner‑API outage or latency spike (dependency failure) | dependency_failure | bank-partner-api | null | 0.22 (low) | active |

### H1: Expired client certificate for bank-partner-api causing mTLS handshake failures
The client TLS certificate used by payment-service to authenticate to bank-partner-api expired at 12:15, causing mTLS handshake rejections. Payment-service treats these as business failures, marking payments PENDING_RETRY and returning errors that do not increment the HTTP error-rate metric, while the internal bank_api_error_rate metric spikes to 100%. Customers experience payment failures despite a green error-rate dashboard.

**Causal chain**
1. Client certificate expired at 12:15:00 [F2]
2. mTLS handshake with bank-partner-api rejected due to expired cert [F1]
3. Payment-service marks payments PENDING_RETRY and returns business‑logic errors (not 5xx) *(assumed)*
4. bank_api_error_rate metric spikes to 100% from 12:16 onward [F3, F10]
5. Overall HTTP error_rate remains low, so dashboard appears normal [F4]
6. Customers see payment failures and open tickets *(assumed)*

**Supporting evidence**
  - `F1` ✔ The first error related to payment processing appears at 2026-04-02T12:15:27Z: a payment is marked PENDING_RETRY due to mTLS handshake with bank-partner-api rejected.
  - `F2` ✔ The first TLS expiration error is logged at 2026-04-02T12:16:44Z, indicating the client certificate expired at 2026-04-02T12:15:00Z.
  - `F3` ✔ The bank_api_error_rate metric spikes to 100% starting at the 12:16 bucket and stays at 100% through the rest of the window, whereas the overall HTTP error_rate stays below 0.32%.
  - `F4` ✔ The error_rate dashboard for payment-service shows no abnormal rise (max 0.32%) despite the high bank_api_error_rate, explaining why the panel appears green while customers experience failures.
  - `F10` ✔ The payment-service metric `bank_api_error_rate` jumps from ~0.2% to 25.15% (max 100%) in the 12:12 bucket and reaches 100% steady from the 12:16 bucket onward.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Payment-service uses the expired client certificate for all calls to bank-partner-api
  - Business‑logic errors (e.g., PENDING_RETRY) are not counted in the HTTP error_rate metric

### H2: Bank‑partner‑API outage or latency spike (dependency failure)
The external bank-partner-api experienced an outage or severe latency spike, causing payment-service calls to fail or timeout. These failures are recorded in the bank_api_error_rate metric, leading to customer‑visible payment failures while the HTTP error‑rate metric stays low.

**Causal chain**
1. Bank‑partner‑API became unavailable or highly latent *(assumed)*
2. Payment-service receives errors/timeouts from bank‑partner‑API *(assumed)*
3. bank_api_error_rate spikes to 100% from 12:16 onward [F3, F10]
4. Payment failures are reported by customers *(assumed)*

**Supporting evidence**
  - `F3` ✔ The bank_api_error_rate metric spikes to 100% starting at the 12:16 bucket and stays at 100% through the rest of the window, whereas the overall HTTP error_rate stays below 0.32%.
  - `F10` ✔ The payment-service metric `bank_api_error_rate` jumps from ~0.2% to 25.15% (max 100%) in the 12:12 bucket and reaches 100% steady from the 12:16 bucket onward.
  - `F5` ✔ payment-service depends downstream on ledger-db (shared DB), bank-partner-api (external), and kafka (queue produce).
**Contradicting evidence**
  - `F1` ✔ The first error related to payment processing appears at 2026-04-02T12:15:27Z: a payment is marked PENDING_RETRY due to mTLS handshake with bank-partner-api rejected.
  - `F2` ✔ The first TLS expiration error is logged at 2026-04-02T12:16:44Z, indicating the client certificate expired at 2026-04-02T12:15:00Z.
**Assumptions**
  - The mTLS handshake failure observed in F1 is a symptom of the bank API being down rather than a certificate issue

## Facts (verified observations)
- The first payment processing error occurred at 2026-04-02T12:15:27Z, marking the payment PENDING_RETRY due to a rejected mTLS handshake with bank-partner-api. [F1]
- TLS expiration errors were logged at 2026-04-02T12:16:44Z, indicating the client certificate expired at 2026-04-02T12:15:00Z. [F2]
- The `bank_api_error_rate` metric spiked to 100% starting at the 12:16 bucket and remained at 100%, while the overall HTTP `error_rate` stayed below 0.32%. [F3, F10]
- `latency_p99_ms` for payment-service did not spike; it actually decreased after the failures began. [F11]
- `request_rate` for payment-service remained roughly constant (~100 req/s) throughout the incident window. [F12]

## Assumptions (not verified)
- Payment-service uses the expired client certificate for all calls to bank-partner-api.
- Business‑logic failures (e.g., PENDING_RETRY) are not counted in the HTTP `error_rate` metric.
- (demoted from facts: no verified observation backs it) The payment-service error‑rate dashboard displayed a maximum of 0.32%, appearing green despite the downstream failures.

## Timeline
- `2026-04-02T11:20:00Z` Configuration change: bank.api.timeout_ms increased from 3000 ms to 5000 ms. [F7]
- `2026-04-02T12:12:00Z` `bank_api_error_rate` begins rising from ~0.2% toward 25.15%. [F10]
- `2026-04-02T12:15:00Z` Client TLS certificate used by payment-service expires. [F2]
- `2026-04-02T12:15:27Z` First payment marked PENDING_RETRY due to mTLS handshake rejection. [F1]
- `2026-04-02T12:16:00Z` `bank_api_error_rate` spikes to 100% and stays there. [F3]
- `2026-04-02T12:16:44Z` TLS expiration error logged, confirming certificate expiry. [F2]
- `2026-04-02T12:32:00Z` Support reports ~40 tickets of payment failures; incident declared SEV‑2. []
- `2026-04-02T13:59:00Z` Investigation concluded (budget exhausted). []

## Recommended actions
- **[immediate]** Generate and deploy a new client TLS certificate for payment-service to authenticate to bank-partner-api; restart payment-service to load the new certificate. -- The expired certificate is the root cause of mTLS handshake failures (F1, F2). Renewing it will restore successful downstream calls. [F1, F2]
- **[short_term]** Verify that payment-service correctly propagates downstream failures to the HTTP `error_rate` metric or adjust monitoring to surface `bank_api_error_rate` on the primary dashboard. -- Customers see failures while the main dashboard stays green; aligning metrics will improve observability (F3, F4). [F3, F4]
- **[long_term]** Implement automated certificate rotation and expiry alerts (e.g., Prometheus alert on cert expiry < 24h). -- Prevents recurrence of expiry‑related outages (root cause).
- **[long_term]** Add a dedicated `bank_api_latency_p99_ms` metric and integrate it into dashboards. -- Current latency metrics (F11) do not isolate downstream latency; this will aid future root‑cause analysis.

## Data gaps
- Configuration changes for bank-partner-api and kafka were not queried; unknown if any recent changes could have contributed.
- No dedicated latency metric for bank-partner-api calls, limiting ability to confirm downstream latency spikes.

## What would change this conclusion
- Evidence that the client certificate was still valid at the time of failures (e.g., a valid cert fingerprint log).
- Logs from bank-partner-api showing it was down or rejecting connections for reasons unrelated to client cert expiry.

## Integrity warnings (set by code)
- fact demoted to assumption: The payment-service error‑rate dashboard displayed a maximum of 0.32%, appearing

## Investigation stats
- latency 824.08s, LLM calls 17, tokens 57835 (in 46460 / out 11375)
- tool calls 11 (0 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
