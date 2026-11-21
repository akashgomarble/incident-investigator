# Incident investigation report: INC-005
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The reported symptom (payment failures with a green error-rate dashboard) is caused by an expired mTLS client certificate on payment-service. The client cert CN=payments-client.orangeinvest.internal expired at 2026-04-02T12:15:00Z (predicted by a cert-watcher WARN at 11:50:26Z). From 12:15:27Z payment-service logs mTLS handshake rejections and 'remote error: tls: expired certificate (alert 45)', bank_api_error_rate rises to 100%, and payment_success_rate collapses from ~99% to ~2.5%. The HTTP error-rate panel stayed flat (~0.2%) because failed payments are accepted and marked PENDING_RETRY rather than returning 5xx. Alternatives (partner-side outage, the cfg-2502 timeout change at 11:20, resource/capacity saturation, the settlement-service deploy) do not fit the timing or the error messages.

## Probable root cause
**Expired payment-service mTLS client certificate to bank-partner-api** -- credential_expiry in `payment-service` (confidence 0.92, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired payment-service mTLS client certificate to bank-partner-api | credential_expiry | payment-service | - | 0.92 (high) | active |
| 2 | H2: Bank-partner-api side outage / partner dependency failure | dependency_failure | bank-partner-api | - | 0.08 (low) | active |
| 3 | H3: Config change cfg-2502 (bank.api.timeout_ms 3000->5000) caused payment failures | config_change | payment-service | cfg-2502 | 0.03 (low) | rejected |
| 4 | H4: Resource exhaustion / capacity saturation in payment-service | resource_exhaustion | payment-service | - | 0.02 (low) | rejected |

### H1: Expired payment-service mTLS client certificate to bank-partner-api
The payment-service client certificate CN=payments-client.orangeinvest.internal expired at 2026-04-02T12:15:00Z. Once expired, mTLS handshakes to the bank-partner-api are rejected (TLS alert 45), so nearly all fund transfers fail; payments are accepted and marked PENDING_RETRY, so the HTTP 5xx error-rate panel stayed flat while ~97.5% of payments fail.

**Causal chain**
1. payment-service client cert reaches notAfter=2026-04-02T12:15:00Z [F2]
2. mTLS handshake with bank-partner-api rejected with 'tls: expired certificate (alert 45)' [F3, F4]
3. bank_api_error_rate -> 100%, payment_success_rate -> ~2.5% from 12:24 [F5, F6]
4. Failed payments marked PENDING_RETRY instead of returning 5xx, so error_rate stays green [F1, F14]

**Supporting evidence**
  - `F2` ✔ A cert-watcher WARN at 11:50:26Z reported the payment-service client certificate CN=payments-client.orangeinvest.internal expires at notAfter=2026-04-02T12:15:00Z, 25 minutes later.
  - `F3` ✔ From 12:15:27Z payment-service logged mTLS handshake rejections with bank-partner-api, e.g. 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'; the pattern repeats 140 times.
  - `F4` ✔ payment-service logs report the remote error explicitly: 'tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', 171 occurrences.
  - `F5` ✔ payment-success-rate collapsed from ~99% (12:00 bucket) to 26.82% at 12:12 and ~2.5% from the 12:24 bucket onward, staying at ~2.5% through 13:48.
  - `F6` ✔ bank_api_error_rate for payment-service jumped from ~0.2% to 75.05% at the 12:12 bucket and 100% from 12:24 onward.
  - `F12` ✔ bank-partner-api is an external dependency with has_telemetry=false, so the fault is on the payment-service side using an expired client credential, not telemetry from the partner.
  - `F13` ✔ Prior incident HIST-106 (2025-06-30, settlement-service) had the same pattern: 'Client certificate expired; no renewal automation', category credential_expiry.
  - `F14` ✔ Only ~2.5% of payments complete successfully, consistent with customer reports of ~40 failed fund transfers; failures occur while HTTP 5xx stays low because requests are accepted then marked PENDING_RETRY.
**Contradicting evidence**
  - `F9` ✔ A config change cfg-2502 on payment-service raised bank.api.timeout_ms from 3000 to 5000 at 11:20:00Z; latency_p99_ms actually DECREASED from ~325ms to ~140ms after 12:12, so the timeout change does not explain the failure.
**Assumptions**
  - cert-watcher notAfter value is authoritative for the cert actually presented in the TLS handshake
  - bank-partner-api rejects (rather than silently accepts) an expired client cert

### H2: Bank-partner-api side outage / partner dependency failure
The external bank-partner-api began returning errors/failing, causing payment failures on the partner side.

**Causal chain**
1. bank-partner-api degrades *(assumed)*
2. payment calls fail [F5, F6]

**Supporting evidence**
  - `F5` ✔ payment-success-rate collapsed from ~99% (12:00 bucket) to 26.82% at 12:12 and ~2.5% from the 12:24 bucket onward, staying at ~2.5% through 13:48.
  - `F6` ✔ bank_api_error_rate for payment-service jumped from ~0.2% to 75.05% at the 12:12 bucket and 100% from 12:24 onward.
**Contradicting evidence**
  - `F3` ✔ From 12:15:27Z payment-service logged mTLS handshake rejections with bank-partner-api, e.g. 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'; the pattern repeats 140 times.
  - `F4` ✔ payment-service logs report the remote error explicitly: 'tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', 171 occurrences.
  - `F2` ✔ A cert-watcher WARN at 11:50:26Z reported the payment-service client certificate CN=payments-client.orangeinvest.internal expires at notAfter=2026-04-02T12:15:00Z, 25 minutes later.
**Assumptions**
  - Partner outage coinciding with our cert expiry
**Rejected because:** Logs name OUR expired certificate (CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z) as the cause, not partner-side errors; onset matches cert expiry to the minute. Partner has no telemetry and there is no partner-side 5xx evidence.

### H3: Config change cfg-2502 (bank.api.timeout_ms 3000->5000) caused payment failures
Raising the bank API timeout at 11:20 caused payments to fail.

**Causal chain**
1. timeout raised [F9]
2. payments fail *(assumed)*

**Supporting evidence**
  - `F9` ✔ A config change cfg-2502 on payment-service raised bank.api.timeout_ms from 3000 to 5000 at 11:20:00Z; latency_p99_ms actually DECREASED from ~325ms to ~140ms after 12:12, so the timeout change does not explain the failure.
**Contradicting evidence**
  - `F2` ✔ A cert-watcher WARN at 11:50:26Z reported the payment-service client certificate CN=payments-client.orangeinvest.internal expires at notAfter=2026-04-02T12:15:00Z, 25 minutes later.
  - `F3` ✔ From 12:15:27Z payment-service logged mTLS handshake rejections with bank-partner-api, e.g. 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'; the pattern repeats 140 times.
  - `F4` ✔ payment-service logs report the remote error explicitly: 'tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', 171 occurrences.
  - `F9` ✔ A config change cfg-2502 on payment-service raised bank.api.timeout_ms from 3000 to 5000 at 11:20:00Z; latency_p99_ms actually DECREASED from ~325ms to ~140ms after 12:12, so the timeout change does not explain the failure.
  - `F7` ✔ request_rate for payment-service stayed flat at ~100 req/s across the window, so there was no traffic surge triggering the failure.
**Assumptions**
  - Timeout change affects TLS handshake
**Rejected because:** Failure onset (12:15) is 55 min after cfg-2502 and coincides exactly with the cert notAfter time; a longer timeout cannot cause TLS 'expired certificate' rejections, and latency actually fell rather than rose.

### H4: Resource exhaustion / capacity saturation in payment-service
CPU, memory, DB pool or traffic overload caused the failures.

**Causal chain**
1. resource saturation [F1]
2. payments fail *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ request_rate for payment-service stayed flat at ~100 req/s across the window, so there was no traffic surge triggering the failure.
  - `F8` ✔ payment-service CPU (~30%), memory (~55%), db_pool_in_use (~8 conns) and restarts (0) were all flat and healthy throughout - no resource exhaustion or restart.
**Rejected because:** CPU ~30%, memory ~55%, db_pool ~8 conns, restarts 0 and request_rate flat; no resource pressure at all.

## Facts (verified observations)
- payment-service HTTP error_rate stayed flat ~0.22% (max 0.32%) the whole window, so the panel is green. [F1]
- A cert-watcher warned at 11:50:26Z that CN=payments-client.orangeinvest.internal expires at notAfter=2026-04-02T12:15:00Z. [F2]
- mTLS handshake rejections to bank-partner-api began at 12:15:27Z (140 occurrences of 'marked PENDING_RETRY'). [F3]
- 171 log lines report 'tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. [F4]
- payment_success_rate fell from ~99% to 2.47% by the 12:24 bucket and stayed ~2.5%. [F5]
- bank_api_error_rate rose to 100% from the 12:24 bucket onward. [F6]
- request_rate stayed flat ~100 req/s; CPU/memory/db_pool/restarts all healthy. [F7, F8]
- The only config change was cfg-2502 raising bank.api.timeout_ms 3000->5000 at 11:20; latency fell, not rose. [F9]
- The only deployment was settlement-service dep-5510 (v1.9.0, CSV exporter) at 10:30, unrelated to the payment path. [F10]
- api-gateway error_rate stayed flat ~0.21%, showing no client-facing 5xx spike. [F11]
- bank-partner-api is external with no telemetry. [F12]
- Past incident HIST-106 was a settlement-service client certificate expiry with no renewal automation. [F13]

## Assumptions (not verified)
- cert-watcher's notAfter value reflects the certificate actually presented in the TLS handshake.
- bank-partner-api rejects (does not silently accept) an expired client certificate.
- PENDING_RETRY payments are not counted as HTTP 5xx by the dashboard, explaining the green panel.

## Timeline
- `2026-04-02T11:20:00Z` cfg-2502 raised bank.api.timeout_ms 3000->5000 (red herring) [F9]
- `2026-04-02T11:50:26Z` cert-watcher warns client cert expires in 25 minutes [F2]
- `2026-04-02T12:15:00Z` Client certificate CN=payments-client.orangeinvest.internal reaches notAfter (expires) [F2, F4]
- `2026-04-02T12:15:27Z` First mTLS handshake rejection to bank-partner-api; payments marked PENDING_RETRY [F3]
- `2026-04-02T12:16:44Z` First explicit 'tls: expired certificate (alert 45)' log [F4]
- `2026-04-02T12:24:00Z` bank_api_error_rate 100%, payment_success_rate ~2.5% [F5, F6]
- `2026-04-02T12:32:00Z` Support lead reports incident (still ongoing) []
- `2026-04-02T13:59:00Z` Latest data; success rate still ~2.5%, cert not yet rotated [F5]

## Recommended actions
- **[immediate]** Renew/rotate the payment-service client certificate (CN=payments-client.orangeinvest.internal) and reload it immediately to restore mTLS to bank-partner-api. -- The cert expired at 12:15:00Z and is the direct cause of 100% bank API error rate. [F2, F4]
- **[immediate]** Replay the payments currently queued as PENDING_RETRY once mTLS is restored, and reconcile ledger state. -- Failed transfers are held as PENDING_RETRY and must be retried/reconciled for customers. [F3, F14]
- **[short_term]** Add a payment-success-rate / bank_api_error_rate alert and fix the error-rate panel to count PENDING_RETRY business failures, not just HTTP 5xx. -- The HTTP 5xx panel stayed green while 97.5% of payments failed; the monitoring gap delayed detection. [F1, F5, F6]
- **[long_term]** Automate certificate expiry monitoring with paging alerts well before notAfter and implement automated cert renewal (e.g., cert-manager). -- cert-watcher only emitted a WARN 25 min before expiry and HIST-106 was the identical cert-expiry pattern. [F2, F13]

## Data gaps
- No direct telemetry from bank-partner-api (has_telemetry=false), so partner-side acceptance of retried payments is unverified.
- The PENDING_RETRY queue depth / Kafka produce metrics were not inspected, so the size of the backlog awaiting replay is unknown.
- Exact error-code mapping of PENDING_RETRY to customer-visible failure is inferred from success-rate and logs, not from an explicit API response sample.
- The payment-service logs were truncated (316 matched, 40 returned); the full set was not exhaustively reviewed, though the dominant patterns are consistent.

## Investigation stats
- latency 42.1s, LLM calls 5, tokens 58191 (in 50840 / out 7351)
- tool calls 19 (0 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
