# Incident investigation report: INC-005
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
payment-service's outbound mTLS client certificate (CN=payments-client.orangeinvest.internal) expired at 2026-04-02T12:15:00Z, causing every call to the external bank-partner-api to be rejected at the TLS handshake ('tls: expired certificate (alert 45)'). Onset is unambiguous: bank_api_error_rate jumped from ~0.2% to 100% and payment_success_rate from ~99% to ~2.5% starting in the 12:10–12:20 buckets, and a cert-watcher warned at 11:50. The HTTP error-rate dashboard stayed green because failed transfers are recorded asynchronously as PENDING_RETRY (ERROR logs) rather than as synchronous 5xx, so the 5xx-based panel sees nothing. No deployments occurred; the only config change (11:20 timeout bump) and flat request rate/p99 rule out deployment, config, capacity and partner-side-outage hypotheses. Root cause category: credential_expiry. Fix: renew/rotate the client certificate and restore bank calls; the PENDING_RETRY queue should be replayed.

## Probable root cause
**Expired mTLS client certificate breaks bank-partner-api calls** -- credential_expiry in `payment-service` (confidence 0.93, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired mTLS client certificate breaks bank-partner-api calls | credential_expiry | payment-service | - | 0.93 (high) | active |
| 2 | H2: Bank partner API outage / dependency failure | dependency_failure | bank-partner-api | - | 0.05 (low) | rejected |
| 3 | H3: Timeout config change (bad deployment/config_change) | config_change | payment-service | cfg-2502 | 0.02 (low) | rejected |
| 4 | H4: Traffic surge / capacity saturation | capacity_saturation | payment-service | - | 0.01 (low) | rejected |

### H1: Expired mTLS client certificate breaks bank-partner-api calls
payment-service's outbound mTLS client certificate CN=payments-client.orangeinvest.internal expired at 2026-04-02T12:15:00Z. From that moment every call to the external bank-partner-api is rejected at the TLS handshake (alert 45), so fund transfers fail. Because failed transfers are recorded asynchronously as PENDING_RETRY rather than a synchronous 5xx, the HTTP error_rate panel stays green while bank_api_error_rate hits 100% and payment_success_rate drops to ~2.5%.

**Causal chain**
1. Client cert reaches notAfter=2026-04-02T12:15:00Z without renewal (cert-watcher warned at 11:50) [F2]
2. Expiry -> mTLS handshake rejected with 'tls: expired certificate (alert 45)' [F1]
3. Handshake failure -> bank_api_error_rate 0.2%->100% and payment_success_rate 99%->~2.5% [F3, F4]
4. Failed payments queued asynchronously as PENDING_RETRY (ERROR logs), so HTTP 5xx error_rate stays ~0.2% [F5, F6]

**Supporting evidence**
  - `F1` ✔ payment-service logs show mTLS handshake failures with bank-partner-api: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', 171 occurrences, first seen 2026-04-02T12:16:44Z.
  - `F2` ✔ A cert-watcher WARN at 11:50:26Z gave 25 minutes notice: client certificate CN=payments-client.orangeinvest.internal expires at notAfter=2026-04-02T12:15:00Z.
  - `F3` ✔ bank_api_error_rate jumped from ~0.2% to 50% in the 12:10 bucket and 100% from the 12:20 bucket onward, remaining at 100% through 13:50.
  - `F4` ✔ payment_success_rate collapsed from ~99% to ~2.6% in the same 12:10/12:20 buckets and stayed near 2.5% (min 1.44) through 13:50, i.e. roughly 97% of payments failing.
  - `F5` ✔ The HTTP error_rate metric (5xx % of requests) stayed flat at ~0.2% across the whole window, never rising above max 0.32 — this is the green dashboard the report mentions.
  - `F6` ✔ 140 ERROR logs show payments absorbed asynchronously: 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected', first seen 12:15:27Z.
  - `F7` ✔ No code deployments occurred for any service in 2026-04-02T11:00–13:59Z (count 0).
  - `F10` ✔ Payment request_rate stayed steady ~100 req/s before and during the incident (min 90.22, max 109.57), so the failure is not a traffic surge.
  - `F11` ✔ p99 latency dropped (320ms -> ~140ms) at onset rather than rising — consistent with requests failing fast at handshake instead of hanging.
  - `F12` ✔ No WARN/ERROR logs from api-gateway or ledger-db in the window, so upstream edge and shared database were not the source of the failure.
  - `F13` ✔ A prior incident HIST-106 (settlement-service, 2025-06-30) had the same signature: 'Client certificate expired; no renewal automation', category credential_expiry, resolution 'Issued new cert; added expiry alert'.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Certificate renewal is manual; no automated rotation occurred before expiry.
  - The 'expired certificate' remote error is caused by our own client cert (log explicitly names our CN and notAfter), not by a partner-side cert change.

### H2: Bank partner API outage / dependency failure
The external bank-partner-api is degraded or returning errors, causing transfers to fail (as in HIST-105).

**Causal chain**
1. Partner API fails -> partner calls error -> payment failures *(assumed)*

**Supporting evidence**
  - `F3` ✔ bank_api_error_rate jumped from ~0.2% to 50% in the 12:10 bucket and 100% from the 12:20 bucket onward, remaining at 100% through 13:50.
**Contradicting evidence**
  - `F1` ✔ payment-service logs show mTLS handshake failures with bank-partner-api: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', 171 occurrences, first seen 2026-04-02T12:16:44Z.
  - `F2` ✔ A cert-watcher WARN at 11:50:26Z gave 25 minutes notice: client certificate CN=payments-client.orangeinvest.internal expires at notAfter=2026-04-02T12:15:00Z.
  - `F5` ✔ The HTTP error_rate metric (5xx % of requests) stayed flat at ~0.2% across the whole window, never rising above max 0.32 — this is the green dashboard the report mentions.
  - `F6` ✔ 140 ERROR logs show payments absorbed asynchronously: 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected', first seen 12:15:27Z.
**Assumptions**
  - Would require partner-side change, but no partner telemetry exists to confirm.
**Rejected because:** Error text names OUR client cert as expired (notAfter given), and a local cert-watcher warned 25 minutes before onset. The failure is at our TLS handshake (alert 45), not a partner 5xx; partner has no telemetry but the evidence points to our credential.

### H3: Timeout config change (bad deployment/config_change)
The 11:20 bank.api.timeout_ms change (3000->5000) caused the failures.

**Causal chain**
1. cfg-2502 raises timeout -> ... -> payment failures [F8]

**Supporting evidence**
  - `F8` ✔ The only config change in the window was payment-service bank.api.timeout_ms raised from 3000 to 5000 at 11:20:00Z, ~55 minutes before onset.
**Contradicting evidence**
  - `F1` ✔ payment-service logs show mTLS handshake failures with bank-partner-api: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', 171 occurrences, first seen 2026-04-02T12:16:44Z.
  - `F2` ✔ A cert-watcher WARN at 11:50:26Z gave 25 minutes notice: client certificate CN=payments-client.orangeinvest.internal expires at notAfter=2026-04-02T12:15:00Z.
  - `F3` ✔ bank_api_error_rate jumped from ~0.2% to 50% in the 12:10 bucket and 100% from the 12:20 bucket onward, remaining at 100% through 13:50.
**Assumptions**
  - A timeout increase could manifest as handshake cert errors (implausible).
**Rejected because:** Change landed 55 min before onset (11:20 vs 12:15) and the symptom is a TLS cert expiry matching notAfter exactly; p99 latency fell rather than rose, inconsistent with a timeout effect. No deployments occurred at all.

### H4: Traffic surge / capacity saturation
A load spike saturated payment-service or its downstream and caused failures.

**Causal chain**
1. Load up -> saturation -> failures *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ Payment request_rate stayed steady ~100 req/s before and during the incident (min 90.22, max 109.57), so the failure is not a traffic surge.
  - `F11` ✔ p99 latency dropped (320ms -> ~140ms) at onset rather than rising — consistent with requests failing fast at handshake instead of hanging.
**Rejected because:** request_rate was flat ~100 req/s and p99 latency decreased at onset; error is a deterministic TLS handshake rejection, not saturation.

## Facts (verified observations)
- payment-service logs show mTLS handshake failures naming our expired client cert: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. [F1]
- A cert-watcher warned at 11:50:26Z that the certificate would expire at 12:15:00Z. [F2]
- bank_api_error_rate rose from ~0.2% to 100% starting in the 12:10–12:20 buckets. [F3]
- payment_success_rate fell from ~99% to ~2.5% over the same buckets and remained there. [F4]
- The HTTP 5xx error_rate metric stayed flat at ~0.2%, which is why the dashboard looked green. [F5]
- Failed payments were recorded asynchronously as PENDING_RETRY errors (140 occurrences). [F6]
- No code deployments occurred for any service in the incident window. [F7]
- The only config change was bank.api.timeout_ms 3000->5000 at 11:20Z, 55 minutes before onset. [F8]
- bank-partner-api is external with no telemetry; api-gateway and ledger-db logged no warnings/errors. [F9, F12]
- Request rate was flat (~100 req/s) and p99 latency fell at onset, ruling out load/saturation. [F10, F11]
- A past incident (HIST-106) had the identical 'client certificate expired; no renewal automation' signature. [F13]

## Assumptions (not verified)
- The 'expired certificate (alert 45)' remote error is triggered by our client certificate (the log names our CN and notAfter), not a partner-side certificate.
- PENDING_RETRY payments are recoverable via retry/replay and were not permanently lost.

## Timeline
- `2026-04-02T11:20:00Z` Config change cfg-2502: bank.api.timeout_ms 3000->5000 (unrelated to onset) [F8]
- `2026-04-02T11:50:26Z` cert-watcher warns client cert expires in 25 minutes (notAfter 12:15:00Z) [F2]
- `2026-04-02T12:15:00Z` Client certificate expires; mTLS handshakes to bank-partner-api begin failing [F2, F1]
- `2026-04-02T12:15:27Z` First payment marked PENDING_RETRY due to rejected handshake [F6]
- `2026-04-02T12:16:44Z` First 'tls: expired certificate (alert 45)' error logged [F1]
- `2026-04-02T12:20:00Z` bank_api_error_rate reaches 100%, payment_success_rate drops to ~2.5% [F3, F4]
- `2026-04-02T12:32:00Z` Support reports ~40 payment-failure tickets; HTTP error-rate panel still green [F5]

## Recommended actions
- **[immediate]** Renew/rotate the payments-client client certificate for bank-partner-api and reload it in payment-service to restore mTLS handshakes. -- The expired cert (notAfter 2026-04-02T12:15:00Z) is the direct cause of 100% bank API failures. [F1, F2, F3]
- **[immediate]** Replay the queued PENDING_RETRY payments once connectivity is restored and reconcile against the bank. -- 140 payments were absorbed into a retry queue and may need explicit reprocessing. [F6]
- **[short_term]** Add an alerting SLO on payment_success_rate and bank_api_error_rate (not just HTTP 5xx error_rate) so async-absorbed failures page on call. -- The 5xx error_rate panel stayed flat while ~97% of payments failed, hiding the incident. [F5, F4]
- **[long_term]** Implement automated certificate rotation with expiry alerting (as done after HIST-106) and make the cert-watcher warning actionable/paging. -- Same root cause recurred; renewal is manual and the 25-minute warning did not trigger action. [F2, F13]

## Data gaps
- bank-partner-api has no telemetry, so partner-side health cannot be independently confirmed; the conclusion rests on our own logs naming our expired client cert.
- The first INFO-level payment-service log query (call tc-011) failed with invalid_arguments (missing start); a follow-up at 11:45-12:16 (tc-015, not truncated) showed no additional INFO events, but full INFO history was not exhaustively reviewed.
- No data on when the certificate was last renewed or which secret store holds it, so the renewal-ownership gap is inferred from HIST-106 rather than observed.

## Investigation stats
- latency 61.71s, LLM calls 4, tokens 39037 (in 32141 / out 6896)
- tool calls 15 (1 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
