# Incident investigation report: INC-005
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Root cause is credential_expiry: payment-service's own mTLS client certificate (CN=payments-client.orangeinvest.internal, notAfter=2026-04-02T12:15:00Z) expired. cert-watcher warned from 08:20Z, but no renewal occurred. From ~12:15Z every bank-partner-api call fails the TLS handshake ('tls: expired certificate (alert 45)'), so fund transfers cannot be submitted and payments are parked as PENDING_RETRY. The HTTP error_rate panel stayed green because those failures are returned as successful (non-5xx) responses, while bank_api_error_rate rose to 100% and payment_success_rate fell from ~99% to ~2.5% at 12:12Z. Deployments (none for payment-service), the unrelated cfg-2502 timeout change, settlement-service dep-5510, bank-side outage, and resource/capacity saturation were all ruled out.

## Probable root cause
**payment-service mTLS client certificate expired, breaking all bank-partner-api calls** -- credential_expiry in `payment-service` (confidence 0.93, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: payment-service mTLS client certificate expired, breaking all bank-partner-api calls | credential_expiry | payment-service | - | 0.93 (high) | active |
| 2 | H2: Bank partner API outage (external dependency failure) | dependency_failure | bank-partner-api | - | 0.05 (low) | rejected |
| 3 | H3: Config change cfg-2502 (bank API timeout 3s->5s) caused payment failures | config_change | payment-service | - | 0.03 (low) | rejected |
| 4 | H4: Resource/capacity saturation in payment-service | capacity_saturation | payment-service | - | 0.02 (low) | rejected |

### H1: payment-service mTLS client certificate expired, breaking all bank-partner-api calls
payment-service's mTLS client certificate (CN=payments-client.orangeinvest.internal) expired at 2026-04-02T12:15:00Z. After expiry every call to bank-partner-api fails the TLS handshake (alert 45 expired certificate), so fund transfers cannot be submitted. Failures are swallowed as PENDING_RETRY (HTTP success), leaving the HTTP 5xx error_rate panel green while bank_api_error_rate hits 100% and payment_success_rate collapses.

**Causal chain**
1. Client cert notAfter=2026-04-02T12:15:00Z reached; cert-watcher warned since 08:20Z with no renewal [F1]
2. mTLS handshake to bank-partner-api rejected, error 'tls: expired certificate (alert 45)' [F2]
3. Payments marked PENDING_RETRY instead of failing HTTP -> error_rate (5xx) stays flat [F3, F10]
4. bank_api_error_rate rises to 100% and payment_success_rate falls to ~2.5% at 12:12Z [F4]
5. Customer-visible fund-transfer failures (~40 tickets) [F4]

**Supporting evidence**
  - `F1` ✔ payment-service's own mTLS client certificate CN=payments-client.orangeinvest.internal expired at 2026-04-02T12:15:00Z; cert-watcher had been warning about this expiry since 08:20:09Z.
  - `F2` ✔ payment-service logs show 171 bank-partner-api call failures with 'tls: expired certificate (alert 45)' referencing our own client cert notAfter=2026-04-02T12:15:00Z, plus 140 payments marked PENDING_RETRY due to rejected mTLS handshakes.
  - `F3` ✔ The HTTP dashboard metric error_rate (unit: % of requests (HTTP 5xx)) stayed flat and normal (~0.18-0.32%) throughout the incident window, never rising.
  - `F4` ✔ The bank-facing and business metrics did go red: bank_api_error_rate jumped from ~0.2% to 100% starting 12:12Z, and payment_success_rate collapsed from ~99% to ~2.5%.
  - `F10` ✔ Because failed bank calls are caught and converted to PENDING_RETRY (HTTP success), the HTTP 5xx error_rate panel cannot see this failure mode, while bank_api_error_rate and payment_success_rate can — explaining 'customers fail but dashboard is green'.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The cert was not renewed/rotated by any process after cert-watcher fired.
  - bank-partner-api itself is healthy; the failure is on our side of the mTLS handshake.

### H2: Bank partner API outage (external dependency failure)
bank-partner-api is degraded/down, causing all payment calls to fail regardless of our certificate.

**Causal chain**
(none given)

**Supporting evidence**
  - `F9` ✔ Historical incident HIST-106 (Settlement mTLS certificate expired, category credential_expiry) is a close precedent, and HIST-105 is a dependency_failure case for comparison; these are leads only.
**Contradicting evidence**
  - `F1` ✔ payment-service's own mTLS client certificate CN=payments-client.orangeinvest.internal expired at 2026-04-02T12:15:00Z; cert-watcher had been warning about this expiry since 08:20:09Z.
  - `F2` ✔ payment-service logs show 171 bank-partner-api call failures with 'tls: expired certificate (alert 45)' referencing our own client cert notAfter=2026-04-02T12:15:00Z, plus 140 payments marked PENDING_RETRY due to rejected mTLS handshakes.
**Rejected because:** The error is explicitly a TLS handshake rejection citing OUR client certificate ('our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z') with alert 45 'expired certificate', and cert-watcher pre-warned of the same expiry. A partner-side outage would not produce an expired-our-cert message nor a predictable onset exactly at our cert's notAfter.

### H3: Config change cfg-2502 (bank API timeout 3s->5s) caused payment failures
Raising bank.api.timeout_ms caused bank calls to fail/hang, producing the failures.

**Causal chain**
(none given)

**Supporting evidence**
  - `F6` ✔ The only payment-service config change before onset was cfg-2502 raising bank.api.timeout_ms from 3000 to 5000 at 11:20Z — unrelated to TLS certificate validity.
**Contradicting evidence**
  - `F2` ✔ payment-service logs show 171 bank-partner-api call failures with 'tls: expired certificate (alert 45)' referencing our own client cert notAfter=2026-04-02T12:15:00Z, plus 140 payments marked PENDING_RETRY due to rejected mTLS handshakes.
  - `F4` ✔ The bank-facing and business metrics did go red: bank_api_error_rate jumped from ~0.2% to 100% starting 12:12Z, and payment_success_rate collapsed from ~99% to ~2.5%.
**Rejected because:** cfg-2502 at 11:20Z precedes both the cert expiry (12:15Z) and the metric inflection (12:12Z), and a timeout change cannot produce 'tls: expired certificate (alert 45)' referencing a specific cert notAfter. bank_api_error_rate stayed normal (~0.2%) until 12:12Z, i.e. after the config change.

### H4: Resource/capacity saturation in payment-service
CPU, DB connection pool, or traffic saturation caused the failures.

**Causal chain**
(none given)

**Supporting evidence**
  - `F7` ✔ Saturation/resource metrics were flat and normal: cpu_pct ~30%, db_pool_in_use ~8 connections, request_rate ~100 req/s — no resource exhaustion.
**Contradicting evidence**
  - `F2` ✔ payment-service logs show 171 bank-partner-api call failures with 'tls: expired certificate (alert 45)' referencing our own client cert notAfter=2026-04-02T12:15:00Z, plus 140 payments marked PENDING_RETRY due to rejected mTLS handshakes.
**Rejected because:** cpu_pct (~30%), db_pool_in_use (~8), and request_rate (~100 req/s) are all flat and normal across the window; no saturation. Failure mode is TLS-specific, not load-related.

## Facts (verified observations)
- payment-service client cert expired at 2026-04-02T12:15:00Z with advance cert-watcher warnings from 08:20Z. [F1]
- bank-partner-api calls failed with TLS 'expired certificate (alert 45)' citing our client cert, and 140 payments went to PENDING_RETRY. [F2]
- HTTP 5xx error_rate panel stayed flat ~0.2% throughout, masking the incident. [F3]
- bank_api_error_rate reached 100% and payment_success_rate fell to ~2.5% from 12:12Z. [F4]
- No payment-service code deployment occurred on 2026-04-02. [F5]
- Only prior payment-service config change was cfg-2502 (timeout 3000->5000) at 11:20Z, unrelated to TLS cert validity. [F6]
- CPU, DB pool, and request rate were flat/normal; no resource saturation. [F7]
- settlement-service logged no errors, so its 10:30Z deploy is not implicated. [F8]

## Assumptions (not verified)
- The client cert was not silently renewed/rotated after the last cert-watcher warning (no evidence of renewal found).
- bank-partner-api is otherwise healthy; the handshake failure is attributable to our expired client cert.
- PENDING_RETRY payments are recoverable via replay once mTLS works.

## Timeline
- `2026-04-02T08:20:09Z` cert-watcher warns payment-service client cert expires in 235 minutes (notAfter 12:15:00Z) [F1]
- `2026-04-02T10:30:00Z` settlement-service v1.9.0 deployed (dep-5510); not implicated [F5, F8]
- `2026-04-02T11:20:00Z` cfg-2502 changes bank.api.timeout_ms 3000->5000 [F6]
- `2026-04-02T11:50:26Z` cert-watcher final warning: cert expires in 25 minutes [F1]
- `2026-04-02T12:12:00Z` bank_api_error_rate jumps to ~75% then 100%; payment_success_rate collapses toward ~2.5% [F4]
- `2026-04-02T12:15:00Z` client certificate notAfter reached; mTLS handshakes begin to fail [F1]
- `2026-04-02T12:15:27Z` First payments marked PENDING_RETRY: mTLS handshake rejected [F2]
- `2026-04-02T12:32:00Z` Incident reported by support lead (payment failures, dashboard green) [F3]

## Recommended actions
- **[immediate]** Renew/rotate the payment-service client certificate for CN=payments-client.orangeinvest.internal and reload it immediately; confirm bank_api_error_rate returns to ~0 and payment_success_rate recovers. -- Expired cert is the direct cause of all bank call failures; renewal restores the mTLS handshake. [F1, F2, F4]
- **[immediate]** Drain/replay the PENDING_RETRY payment backlog once mTLS is restored. -- 140+ transfers are parked awaiting retry and must be completed for customers. [F2]
- **[short_term]** Add or fix alerting on bank_api_error_rate and payment_success_rate (business-level SLIs), not just the HTTP 5xx error_rate panel, so caught-and-retried failures are visible. -- The green 5xx dashboard masked a full outage; bank/success metrics revealed it. [F3, F4, F10]
- **[long_term]** Automate certificate renewal and page on cert-watcher expiry warnings; enforce an expiry-buffer policy. -- cert-watcher warned for hours and nothing acted; the same class recurred in HIST-106. [F1, F9]

## Data gaps
- No direct telemetry for bank-partner-api (external, has_telemetry=false), so partner-side health could not be independently confirmed; conclusion rests on our-side TLS error text.
- No explicit cert-renewal/rotation audit log was available to prove no renewal attempt was made.
- payment-service logs returned truncated=true (40 of 314 rows shown), though top patterns and first-seen times were captured; total counts are consistent.

## Investigation stats
- latency 36.72s, LLM calls 4, tokens 44560 (in 38343 / out 6217)
- tool calls 16 (0 failed, 0 partial, 0 recovered by retry), findings 10/10 verified
