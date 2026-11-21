# Incident investigation report: INC-005
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Customer payment failures are caused by the expiry at 2026-04-02T12:15:00Z of payment-service's mTLS client certificate (CN=payments-client.orangeinvest.internal) to the external bank-partner-api, with no automated renewal (cert-watcher warned at 10:20 and 11:50). From 12:15:27 mTLS handshakes were rejected (TLS alert 45), payments were marked PENDING_RETRY, bank_api_error_rate hit 100% and payment_success_rate collapsed to ~2.5%. The HTTP error_rate panel stayed green (~0.2%) because failed transfers are async/retryable and not surfaced as 5xx, which is why the dashboard was misleading. Root-cause category: credential_expiry, originating in payment-service. All alternatives (partner outage, timeout config change, deploy, resource exhaustion) are contradicted by evidence.", "facts": ["payment-service's bank client cert CN=payments-client.orangeinvest.internal had notAfter=2026-04-02T12:15:00Z (F1)", "mTLS handshakes with bank-partner-api were rejected as 'expired certificate (alert 45)' from 12:16:44 (F2, F3)", "bank_api_error_rate rose from 0.2% to 100% at the 12:16 bucket (F4)", "payment_success_rate fell from 99.45% to ~2.55% at 12:16 (F5)", "HTTP error_rate stayed flat ~0.2% throughout, i.e., the dashboard was a false green (F6)", "request_rate steady, CPU ~30% flat, restarts 0 -> not a resource/capacity fault (F7)", "Only change was cfg-2502 timeout 3000->5000 at 11:20; only deploy dep-5510 settlement v1.9.0 at 10:30 (F8)", "bank-partner-api is external with no telemetry (F9)", "Historical HIST-106 was an identical mTLS cert expiry with no renewal automation (F10)"]

## Probable root cause
**Expired payment-service mTLS client certificate to bank-partner-api** -- credential_expiry in `payment-service` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired payment-service mTLS client certificate to bank-partner-api | credential_expiry | payment-service | - | 0.90 (high) | active |
| 2 | H2: Bank-partner-api (external dependency) outage/degradation | dependency_failure | bank-partner-api | - | 0.05 (low) | rejected |
| 3 | H3: Config change (bank API timeout) or unrelated deployment caused failures | config_change | payment-service | - | 0.03 (low) | rejected |

### H1: Expired payment-service mTLS client certificate to bank-partner-api
The payment-service client certificate CN=payments-client.orangeinvest.internal expired at 2026-04-02T12:15:00Z with no automated renewal, so all mTLS handshakes to the external bank-partner-api are rejected (TLS alert 45). Payments are marked PENDING_RETRY and effectively fail, while the HTTP 5xx-based error_rate panel stays flat because these failures are not surfaced as 5xx.

**Causal chain**
1. Client cert notAfter=2026-04-02T12:15:00Z; cert-watcher warned at 10:20 and 11:50 with no renewal [F1]
2. Cert expires -> mTLS handshakes to bank-partner-api rejected (TLS alert 45) starting 12:15:27 [F2, F3, F11]
3. payments marked PENDING_RETRY -> payment_success_rate collapses to ~2.5%, bank_api_error_rate 100% [F4, F5]
4. Failures not returned as HTTP 5xx -> error_rate dashboard stays green (~0.2%) [F6, F12]

**Supporting evidence**
  - `F1` ✔ payment-service logged a cert-watcher warning that its bank client certificate CN=payments-client.orangeinvest.internal would expire at notAfter=2026-04-02T12:15:00Z, at 10:20:44 (115 min before) and 11:50:26 (25 min before).
  - `F2` ✔ From 12:15:27 payment-service began logging ERRORs that mTLS handshakes with bank-partner-api were rejected and payments were marked PENDING_RETRY; top patterns show 140 such entries first seen 12:15:27.
  - `F3` ✔ payment-service logged 171 entries of 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', first seen 12:16:44.
  - `F4` ✔ payment-service bank_api_error_rate jumped from 0.2% at 12:00 to 100% from the 12:16 bucket through 13:52.
  - `F5` ✔ payment-service payment_success_rate collapsed from 99.45% at 12:00 to ~2.55% at 12:16 and stayed near 2-3% through 13:52.
  - `F6` ✔ payment-service HTTP error_rate stayed flat ~0.16-0.26% across the whole 12:00-13:52 window (the 'green' dashboard), contradicting a 5xx-visible failure.
  - `F9` ✔ bank-partner-api is an external vendor dependency with has_telemetry=false; payment-service calls it (kind external).
  - `F10` ✔ Historical incident HIST-106 (2025-06-30) was a settlement-service mTLS client certificate expiry with category credential_expiry and note 'no renewal automation'.
  - `F11` ✔ The error is on the client side: the rejected certificate is our own payments-client.orangeinvest.internal (alert 45, remote error), i.e., payment-service presented an expired client cert to the bank.
  - `F12` ✔ The green HTTP error_rate is a false negative because failed bank transactions are marked PENDING_RETRY (async/retryable) rather than returning HTTP 5xx, so the 5xx-based panel misses them.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The bank-partner-api itself is healthy and only rejects our expired client cert (vendor has no telemetry).
  - No server-side cert of the bank is involved; error text explicitly attributes the expiry to 'our client cert'.

### H2: Bank-partner-api (external dependency) outage/degradation
The external bank partner is failing, causing payment failures independent of our certificate.

**Causal chain**
1. External provider degrades -> payments fail [F4, F5]

**Supporting evidence**
  - `F4` ✔ payment-service bank_api_error_rate jumped from 0.2% at 12:00 to 100% from the 12:16 bucket through 13:52.
  - `F5` ✔ payment-service payment_success_rate collapsed from 99.45% at 12:00 to ~2.55% at 12:16 and stayed near 2-3% through 13:52.
  - `F9` ✔ bank-partner-api is an external vendor dependency with has_telemetry=false; payment-service calls it (kind external).
**Contradicting evidence**
  - `F1` ✔ payment-service logged a cert-watcher warning that its bank client certificate CN=payments-client.orangeinvest.internal would expire at notAfter=2026-04-02T12:15:00Z, at 10:20:44 (115 min before) and 11:50:26 (25 min before).
  - `F3` ✔ payment-service logged 171 entries of 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', first seen 12:16:44.
  - `F11` ✔ The error is on the client side: the rejected certificate is our own payments-client.orangeinvest.internal (alert 45, remote error), i.e., payment-service presented an expired client cert to the bank.
**Assumptions**
  - The partner could be rejecting valid certs.
**Rejected because:** Logs explicitly say the failure is 'our client cert ... notAfter=2026-04-02T12:15:00Z' expired (alert 45), a client-side credential problem, and the cert-watcher had already flagged the impending expiry; onset coincides exactly with 12:15 expiry.

### H3: Config change (bank API timeout) or unrelated deployment caused failures
The 11:20 bank.api.timeout_ms increase or the 10:30 settlement-service deploy caused the payment failures.

**Causal chain**
1. Config/deploy change -> payment failures [F8]

**Supporting evidence**
  - `F8` ✔ The only config change in the window was payment-service bank.api.timeout_ms 3000->5000 at 11:20:00; the only deployment was settlement-service v1.9.0 at 10:30:00. No rollback recorded.
**Contradicting evidence**
  - `F1` ✔ payment-service logged a cert-watcher warning that its bank client certificate CN=payments-client.orangeinvest.internal would expire at notAfter=2026-04-02T12:15:00Z, at 10:20:44 (115 min before) and 11:50:26 (25 min before).
  - `F2` ✔ From 12:15:27 payment-service began logging ERRORs that mTLS handshakes with bank-partner-api were rejected and payments were marked PENDING_RETRY; top patterns show 140 such entries first seen 12:15:27.
  - `F3` ✔ payment-service logged 171 entries of 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', first seen 12:16:44.
  - `F4` ✔ payment-service bank_api_error_rate jumped from 0.2% at 12:00 to 100% from the 12:16 bucket through 13:52.
  - `F6` ✔ payment-service HTTP error_rate stayed flat ~0.16-0.26% across the whole 12:00-13:52 window (the 'green' dashboard), contradicting a 5xx-visible failure.
  - `F7` ✔ payment-service request_rate stayed steady ~98-102 req/s and latency_p99 actually fell from ~317ms to ~140ms after onset; CPU ~30% flat; restarts all 0.
**Rejected because:** The change was 55 min before onset and raised a timeout (would not cause TLS rejections); failures align to the 12:15 cert expiry, and error messages are TLS certificate errors, not timeouts. settlement-service GC/clock warnings are unrelated.

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- The bank-partner-api is otherwise healthy and is rejecting requests solely due to our expired client certificate.
- HTTP error_rate is defined on 5xx responses, so async/retryable payment failures (PENDING_RETRY) do not appear in it.

## Timeline
- `2026-04-02T10:20:44Z` cert-watcher warns bank client cert expires in 115 min (notAfter 12:15:00) [F1]
- `2026-04-02T11:20:00Z` config change cfg-2502 raises bank.api.timeout_ms 3000->5000 (separate, not the cause) [F8]
- `2026-04-02T11:50:26Z` cert-watcher warns cert expires in 25 min; no renewal occurs [F1]
- `2026-04-02T12:15:00Z` Client certificate expires (notAfter) [F1, F3]
- `2026-04-02T12:15:27Z` First mTLS handshake rejection; payments marked PENDING_RETRY [F2]
- `2026-04-02T12:16:00Z` bank_api_error_rate hits 100%; payment_success_rate collapses to ~2.55% [F4, F5]
- `2026-04-02T12:32:00Z` Support reports ~40 failed-transfer tickets; HTTP error-rate panel still green [F6]

## Recommended actions
- **[immediate]** Rotate/renew the payment-service client certificate for bank-partner-api (CN=payments-client.orangeinvest.internal) and reload it, then replay the PENDING_RETRY payments -- The expired client cert is the direct cause; renewing it restores mTLS and clears the PENDING_RETRY backlog. [F1, F2, F3, F5]
- **[short_term]** Add/verify automated certificate renewal (cert-manager/ACME) and a hard expiry alert well before notAfter for payment-service -- cert-watcher only warned and no renewal happened, matching the prior HIST-106 failure. [F1, F10]
- **[short_term]** Fix monitoring: alert on bank_api_error_rate and payment_success_rate (business metrics), not just the HTTP 5xx error_rate panel -- The 5xx-based error_rate stayed green while 97% of payments actually failed, hiding the incident. [F6, F12]
- **[long_term]** Add a dead-letter/alerting path for payments stuck in PENDING_RETRY so async failures page on-call -- Masked failures produced customer tickets before any internal alert. [F2, F12]

## Data gaps
- bank-partner-api has no telemetry, so we cannot independently confirm the partner side is healthy from inside our systems (relied on the explicit 'our client cert ... expired' error text).
- No explicit audit log of the certificate renewal process was available to show why renewal did not run before expiry.

## Investigation stats
- latency 37.15s, LLM calls 5, tokens 57257 (in 50714 / out 6543)
- tool calls 19 (0 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
