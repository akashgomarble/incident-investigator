# Incident investigation report: INC-005
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Customers' failed fund transfers are caused by payment-service's mTLS client certificate (CN=payments-client.orangeinvest.internal) expiring at exactly 2026-04-02T12:15:00Z. From 12:15:27Z onward, every call to bank-partner-api fails the TLS handshake (alert 45, expired certificate) and payments are marked PENDING_RETRY instead of completing. The HTTP error-rate dashboard is green because these queued/acknowledged payments are not surfaced as 5xx; the true signals are bank_api_error_rate jumping 0.2%->100% and payment_success_rate collapsing 99%->~2.5% at the same moment. Resource metrics (CPU, memory, DB pool, restarts) and request throughput are normal, ruling out resource/capacity causes. The earlier timeout config change (11:20Z) and the settlement-service deploy (10:30Z) both predate onset and do not fit a TLS-cert error. This matches historical HIST-106 (client cert expiry, no renewal automation). Fix: issue/reload a valid client cert for payment-service and drain the PENDING_RETRY backlog; add certificate-expiry monitoring/automated rotation and an alert on bank_api_error_rate / payment_success_rate.

## Probable root cause
**Expired mTLS client certificate in payment-service breaks bank-partner-api calls** -- credential_expiry in `payment-service` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired mTLS client certificate in payment-service breaks bank-partner-api calls | credential_expiry | payment-service | - | 0.90 (high) | active |
| 2 | H2: Bank partner API outage (dependency failure) | dependency_failure | bank-partner-api | - | 0.05 (low) | rejected |
| 3 | H3: Config change (bank.api.timeout_ms 3000->5000) triggered failures | config_change | payment-service | cfg-2502 | 0.03 (low) | rejected |
| 4 | H4: settlement-service deploy v1.9.0 caused the incident | bad_deployment | settlement-service | dep-5510 | 0.02 (low) | rejected |

### H1: Expired mTLS client certificate in payment-service breaks bank-partner-api calls
payment-service's client certificate CN=payments-client.orangeinvest.internal expired at 2026-04-02T12:15:00Z. All outbound mTLS calls to bank-partner-api now fail the handshake (TLS alert 45), so payments are acknowledged and marked PENDING_RETRY instead of completing. The HTTP error_rate panel stays green because these are handled/queued, not 5xx.

**Causal chain**
1. client cert payments-client.orangeinvest.internal reaches notAfter=2026-04-02T12:15:00Z [F3, F11]
2. cert expiry -> mTLS handshake to bank-partner-api rejected (alert 45) [F1]
3. rejections -> payments marked PENDING_RETRY rather than 5xx [F2, F12]
4. PENDING_RETRY path bypasses HTTP 5xx metric -> error_rate panel stays green [F4, F12]
5. bank_api_error_rate -> 100% and payment_success_rate -> ~2.5% [F5, F6]

**Supporting evidence**
  - `F1` ✔ payment-service ERROR logs from 12:15:27Z onward show mTLS handshake rejections to bank-partner-api and an expired client certificate.
  - `F2` ✔ The two dominant ERROR patterns (171 cert-expiry + 140 PENDING_RETRY) account for 311 of 313 WARN/ERROR entries; failures persist through the latest data at 13:58:39Z.
  - `F3` ✔ The expired client cert's notAfter is exactly 2026-04-02T12:15:00Z; failures begin immediately after (12:15:27Z first PENDING_RETRY).
  - `F4` ✔ payment-service HTTP error_rate metric stays flat (~0.18-0.32%) across the whole window, explaining the green dashboard.
  - `F5` ✔ bank_api_error_rate jumps from ~0.2% to 100% starting in the 12:10-12:20 bucket and stays at 100% through 13:50.
  - `F6` ✔ payment_success_rate collapses from ~99% to ~2.5% at the same onset, corroborating real customer-facing payment failures.
  - `F11` ✔ The failure is a client-side certificate expiry: the alert is raised against OUR client cert named payments-client.orangeinvest.internal, not a server/partner-side cert.
  - `F12` ✔ payments are marked PENDING_RETRY rather than surfaced as HTTP 5xx, which is why the HTTP error_rate panel (5xx-based) stayed green while actual payment success dropped.
  - `F13` ✔ Historical incident HIST-106 is the same class of fault: a settlement-service client certificate expiry without renewal automation, resolved by issuing a new cert and adding an expiry alert.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Renewing/replacing the client cert will restore bank-partner-api connectivity
  - bank-partner-api accepts a renewed cert from the same CA/CN

### H2: Bank partner API outage (dependency failure)
bank-partner-api is itself failing, causing payment calls to fail.

**Causal chain**
(none given)

**Supporting evidence**
  - `F10` ✔ bank-partner-api is an external dependency with no telemetry; payment-service is its only caller and observes it via its own logs.
**Contradicting evidence**
  - `F1` ✔ payment-service ERROR logs from 12:15:27Z onward show mTLS handshake rejections to bank-partner-api and an expired client certificate.
  - `F3` ✔ The expired client cert's notAfter is exactly 2026-04-02T12:15:00Z; failures begin immediately after (12:15:27Z first PENDING_RETRY).
  - `F11` ✔ The failure is a client-side certificate expiry: the alert is raised against OUR client cert named payments-client.orangeinvest.internal, not a server/partner-side cert.
**Rejected because:** Logs explicitly attribute the failure to OUR expired client certificate (alert 45, our client cert CN=... notAfter=...), and a server-side outage would not produce a client-cert expiry error with an exact notAfter matching the onset. We cannot see partner telemetry (F10) but the local error is self-explanatory.

### H3: Config change (bank.api.timeout_ms 3000->5000) triggered failures
The 11:20Z timeout increase caused the payment failures.

**Causal chain**
(none given)

**Supporting evidence**
  - `F8` ✔ Only one config change near onset: payment-service bank.api.timeout_ms 3000->5000 at 11:20:00Z, ~55 min BEFORE the failure onset.
**Contradicting evidence**
  - `F3` ✔ The expired client cert's notAfter is exactly 2026-04-02T12:15:00Z; failures begin immediately after (12:15:27Z first PENDING_RETRY).
  - `F8` ✔ Only one config change near onset: payment-service bank.api.timeout_ms 3000->5000 at 11:20:00Z, ~55 min BEFORE the failure onset.
**Rejected because:** The config change occurred ~55 minutes before onset; onset at 12:15Z coincides exactly with the cert notAfter. The failures are TLS certificate errors, not timeout errors, so the timeout change is not on the causal path.

### H4: settlement-service deploy v1.9.0 caused the incident
The settlement CSV exporter deploy degraded payments.

**Causal chain**
(none given)

**Supporting evidence**
  - `F9` ✔ Only deployment in scope is settlement-service v1.9.0 at 10:30:00Z (new CSV exporter); no payment-service deploy and no settlement-service config change.
**Contradicting evidence**
  - `F1` ✔ payment-service ERROR logs from 12:15:27Z onward show mTLS handshake rejections to bank-partner-api and an expired client certificate.
  - `F3` ✔ The expired client cert's notAfter is exactly 2026-04-02T12:15:00Z; failures begin immediately after (12:15:27Z first PENDING_RETRY).
**Rejected because:** Deploy was at 10:30Z (~2h before onset) and unrelated to payment-service's outbound mTLS to bank-partner-api; symptoms are cert-expiry specific.

## Facts (verified observations)
- mTLS handshakes from payment-service to bank-partner-api began being rejected at 12:15:27Z. [F1, F3]
- The expired client certificate's notAfter timestamp (2026-04-02T12:15:00Z) coincides exactly with the failure onset. [F3]
- bank_api_error_rate rose from ~0.2% to 100% at onset while the HTTP error_rate panel stayed flat (~0.2%). [F4, F5]
- payment_success_rate fell from ~99% to ~2.5% at the same onset. [F6]
- Resource metrics (cpu ~30%, memory ~55%, db_pool ~8, restarts 0) were stable; request_rate ~100 req/s. [F7]
- Failures persisted to the latest data (13:58:39Z); 311 of 313 WARN/ERROR entries relate to the cert/PENDING_RETRY issue. [F2]
- The only config change was bank.api.timeout_ms 3000->5000 at 11:20Z; the only deploy was settlement-service v1.9.0 at 10:30Z; neither aligns with onset. [F8, F9]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-04-02T10:30:00Z` settlement-service v1.9.0 deployed (CSV exporter) - unrelated to onset [F9]
- `2026-04-02T11:20:00Z` payment-service config change bank.api.timeout_ms 3000->5000 - precedes onset by ~55 min [F8]
- `2026-04-02T12:15:00Z` mTLS client cert CN=payments-client.orangeinvest.internal reaches notAfter / expires [F3]
- `2026-04-02T12:15:27Z` First payments marked PENDING_RETRY (mTLS handshake rejected) [F3, F1]
- `2026-04-02T12:16:44Z` First explicit 'tls: expired certificate (alert 45)' log line [F1]
- `2026-04-02T12:20:00Z` bank_api_error_rate reaches 100%; payment_success_rate falls to ~2.5% [F5, F6]
- `2026-04-02T12:32:00Z` Incident reported by support (payments failing, error_rate dashboard green) [F4]
- `2026-04-02T13:58:39Z` Failures still ongoing at latest data [F2]

## Recommended actions
- **[immediate]** Issue/renew the payment-service mTLS client certificate and reload it into the service (trusted by bank-partner-api). -- The expired cert (notAfter 2026-04-02T12:15:00Z) is blocking all bank-partner-api calls; renewal is the direct fix. [F1, F3, F11]
- **[immediate]** After cert reload, drain/replay the backlog of payments marked PENDING_RETRY (171+ cert failures, 140 PENDING_RETRY). -- These queued payments represent customer fund transfers that never reached the partner. [F2, F6]
- **[short_term]** Add certificate-expiry monitoring and automated rotation for payment-service's outbound mTLS certs. -- Expiry was silent until it broke; HIST-106 had the same 'no renewal automation' root cause. [F13, F3]
- **[short_term]** Alert on bank_api_error_rate and payment_success_rate (not just HTTP 5xx error_rate), since queued/PENDING_RETRY failures bypass the 5xx panel. -- The green 5xx dashboard masked a 100% partner API failure and ~2.5% payment success. [F4, F5, F12]
- **[long_term]** Review the 11:20Z timeout change and 10:30Z settlement deploy as coincidental, not causal; no rollback needed for them. -- Neither aligns with onset; ruling them out avoids unnecessary changes. [F8, F9]

## Data gaps
- bank-partner-api has no telemetry; its status is only inferable through payment-service callers (log query for it returned not_found). Partner-side confirmation of cert rejection is unavailable.
- No cert-management/rotation telemetry was available to confirm the literal renewal state or whether any renewal job exists for payments-client.orangeinvest.internal.
- payment-service WARN/ERROR log results were truncated (313 matched, 40 returned); the full population was summarized via top_patterns counts rather than read in full.

## Investigation stats
- latency 49.5s, LLM calls 4, tokens 44421 (in 38077 / out 6344)
- tool calls 18 (1 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
