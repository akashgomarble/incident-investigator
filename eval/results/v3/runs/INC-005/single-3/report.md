# Incident investigation report: INC-005
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
payment-service's mTLS client certificate (CN=payments-client.orangeinvest.internal) expired at 2026-04-02T12:15:00Z without renewal. From 12:15:27Z every bank-partner-api call fails with 'tls: expired certificate (alert 45)', bank_api_error_rate goes to 100% and payment_success_rate collapses to ~2%, matching ~40 customer tickets about failed transfers. The HTTP error-rate panel stayed green (~0.2%) because affected payments are marked PENDING_RETRY rather than returned as 5xx, so the symptom was invisible on the standard error dashboard. Category: credential_expiry. Fix is to issue/install a renewed client certificate and re-process PENDING_RETRY payments; add renewal automation (same failure mode as historical HIST-106).

## Probable root cause
**payment-service mTLS client certificate expired, breaking bank-partner-api calls** -- credential_expiry in `payment-service` (confidence 0.92, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: payment-service mTLS client certificate expired, breaking bank-partner-api calls | credential_expiry | payment-service | - | 0.92 (high) | active |
| 2 | H2: Bank partner API outage on the vendor side (dependency_failure) | dependency_failure | bank-partner-api | - | 0.05 (low) | rejected |
| 3 | H3: payment-service config change (bank.api.timeout_ms 3s->5s) caused failures | config_change | payment-service | cfg-2502 | 0.03 (low) | rejected |
| 4 | H4: settlement-service v1.9.0 deploy broke payment flow | bad_deployment | settlement-service | dep-5510 | 0.02 (low) | rejected |

### H1: payment-service mTLS client certificate expired, breaking bank-partner-api calls
The client certificate CN=payments-client.orangeinvest.internal used by payment-service to authenticate to bank-partner-api expired at 2026-04-02T12:15:00Z and was not renewed. All bank-partner-api calls then fail at the TLS handshake, payments cannot be submitted to the bank, and payments are marked PENDING_RETRY instead of returning errors, so the HTTP error-rate panel stays green while real payment success collapses to ~2%.

**Causal chain**
1. client cert notAfter=2026-04-02T12:15:00Z (cert-watcher warned 25 min earlier) [F1]
2. cert expires -> mTLS handshake with bank-partner-api rejected from 12:15:27Z [F2, F3]
3. bank_api_error_rate goes to 100% and payment_success_rate to ~2% [F4, F5]
4. payments marked PENDING_RETRY instead of HTTP 5xx -> error_rate panel stays ~0.2% [F6, F13]

**Supporting evidence**
  - `F1` ✔ payment-service logs show a cert-watcher warning at 11:50:26Z that its client certificate CN=payments-client.orangeinvest.internal expires at notAfter=2026-04-02T12:15:00Z.
  - `F2` ✔ From 12:15:27Z onward payment-service logs mTLS handshake rejections from bank-partner-api: 140 occurrences of 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'.
  - `F3` ✔ payment-service logs 171 occurrences of 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', confirming the failure is local cert expiry, not a partner-side fault.
  - `F4` ✔ bank_api_error_rate for payment-service jumps from ~0.2% (through 12:00Z) to avg 75.05% at the 12:12Z bucket and then flat 100% from 12:24Z onward.
  - `F5` ✔ payment_success_rate for payment-service collapses from ~99% before 12:12Z to avg 26.82% at 12:12Z and ~2.5% thereafter, confirming customers' payments are broadly failing.
  - `F6` ✔ The HTTP error_rate metric stayed flat at ~0.2% throughout the incident (avg 0.22, max 0.32), explaining why the error-rate dashboard looked green despite mass payment failures.
  - `F7` ✔ payment-service latency_p99 dropped from ~320ms to ~140ms after 12:12Z, consistent with bank calls failing fast at the TLS handshake rather than hanging.
  - `F11` ✔ Historical incident HIST-106 (2025-06-30, settlement-service) was 'Settlement mTLS certificate expired' with root cause 'Client certificate expired; no renewal automation', category credential_expiry.
  - `F13` ✔ Inference: affected payments are marked PENDING_RETRY rather than returned as HTTP 5xx, so the client-facing HTTP status stays 200 and only the bank_api_error_rate/payment_success_rate metrics reveal the failure.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The CN=payment cert expiry timestamp in logs is the actual applied credential (not a stale config reference).
  - No renewed cert was deployed between 12:15Z and 13:59Z (error logs continue to the latest data).

### H2: Bank partner API outage on the vendor side (dependency_failure)
The bank partner's API is down/returning errors, causing our bank calls to fail, as in historical incident HIST-105.

**Causal chain**
1. bank-partner-api degrades *(assumed)*
2. payment-service bank calls fail [F4]

**Supporting evidence**
  - `F4` ✔ bank_api_error_rate for payment-service jumps from ~0.2% (through 12:00Z) to avg 75.05% at the 12:12Z bucket and then flat 100% from 12:24Z onward.
**Contradicting evidence**
  - `F1` ✔ payment-service logs show a cert-watcher warning at 11:50:26Z that its client certificate CN=payments-client.orangeinvest.internal expires at notAfter=2026-04-02T12:15:00Z.
  - `F2` ✔ From 12:15:27Z onward payment-service logs mTLS handshake rejections from bank-partner-api: 140 occurrences of 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'.
  - `F3` ✔ payment-service logs 171 occurrences of 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', confirming the failure is local cert expiry, not a partner-side fault.
  - `F10` ✔ bank-partner-api is an external node with has_telemetry=false; no partner-side telemetry is available, but the error text attributes the failure to our client cert.
**Rejected because:** Error text explicitly says 'remote error: tls: expired certificate (alert 45); our client cert ... notAfter=2026-04-02T12:15:00Z' and cert-watcher flagged our own cert expiring at exactly 12:15:00Z. The fault is our credential, not the partner's availability.

### H3: payment-service config change (bank.api.timeout_ms 3s->5s) caused failures
cfg-2502 raising the bank API timeout to 5000ms caused payment failures.

**Causal chain**
1. timeout change at 11:20Z [F8]
2. payments fail [F5]

**Supporting evidence**
  - `F8` ✔ The only config change in the window is cfg-2502 on payment-service: bank.api.timeout_ms raised 3000->5000 at 11:20:00Z, before the 12:15Z cert expiry.
**Contradicting evidence**
  - `F1` ✔ payment-service logs show a cert-watcher warning at 11:50:26Z that its client certificate CN=payments-client.orangeinvest.internal expires at notAfter=2026-04-02T12:15:00Z.
  - `F2` ✔ From 12:15:27Z onward payment-service logs mTLS handshake rejections from bank-partner-api: 140 occurrences of 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'.
  - `F3` ✔ payment-service logs 171 occurrences of 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', confirming the failure is local cert expiry, not a partner-side fault.
  - `F7` ✔ payment-service latency_p99 dropped from ~320ms to ~140ms after 12:12Z, consistent with bank calls failing fast at the TLS handshake rather than hanging.
**Rejected because:** The config change was 55 minutes before onset, changed only a timeout, and latency actually DECREASED (~320ms->140ms) post-onset; failures are TLS handshake errors tied to cert notAfter=12:15:00Z.

### H4: settlement-service v1.9.0 deploy broke payment flow
The 10:30Z settlement-service deployment (CSV exporter) introduced a fault that surfaces in payments.

**Causal chain**
1. dep-5510 deployed 10:30Z [F9]
2. payment failures at 12:15Z [F5]

**Supporting evidence**
  - `F9` ✔ Only one deployment occurred in the window: settlement-service v1.9.0 (dep-5510, 'Batch settlement report export') at 10:30Z; no payment-service deployment.
**Contradicting evidence**
  - `F1` ✔ payment-service logs show a cert-watcher warning at 11:50:26Z that its client certificate CN=payments-client.orangeinvest.internal expires at notAfter=2026-04-02T12:15:00Z.
  - `F2` ✔ From 12:15:27Z onward payment-service logs mTLS handshake rejections from bank-partner-api: 140 occurrences of 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'.
  - `F3` ✔ payment-service logs 171 occurrences of 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', confirming the failure is local cert expiry, not a partner-side fault.
  - `F12` ✔ settlement-service shows no error-level logs in the window (only 5 WARNs: GC pauses, one connection reset, NTP drift), so the settlement deploy is not producing failures.
**Rejected because:** settlement-service shows no error logs; onset 12:15Z coincides exactly with the payment cert expiry, not the 10:30Z deploy.

## Facts (verified observations)
- payment-service's client cert CN=payments-client.orangeinvest.internal had notAfter=2026-04-02T12:15:00Z and triggered a cert-watcher warning 25 minutes prior. [F1]
- mTLS handshakes with bank-partner-api were rejected from 12:15:27Z and continued through the latest data (13:58Z). [F2, F3]
- bank_api_error_rate rose from ~0.2% to 75% then 100%; payment_success_rate fell from ~99% to ~2.5%. [F4, F5]
- The HTTP error_rate metric stayed flat at ~0.22% mean throughout, so the standard dashboard was green. [F6]
- Only config change was cfg-2502 (bank.api.timeout_ms 3000->5000 at 11:20Z) and only deployment was settlement-service dep-5510 at 10:30Z; neither aligns with the 12:15Z onset. [F8, F9]
- bank-partner-api is external with no telemetry, and the error text attributes failure to our own client cert, not partner availability. [F3, F10]

## Assumptions (not verified)
- The logged notAfter=2026-04-02T12:15:00Z reflects the actual in-use credential, not a stale config reference.
- PENDING_RETRY payments are recoverable via retry after cert renewal (not permanently dropped).

## Timeline
- `2026-04-02T10:30:00Z` settlement-service v1.9.0 (dep-5510) deployed - unrelated, no error logs [F9, F12]
- `2026-04-02T11:20:00Z` cfg-2502 raises payment-service bank.api.timeout_ms 3000->5000 [F8]
- `2026-04-02T11:50:26Z` cert-watcher warns payment client cert expires in 25 minutes (notAfter 12:15:00Z) [F1]
- `2026-04-02T12:15:00Z` Client certificate CN=payments-client.orangeinvest.internal expires [F1, F3]
- `2026-04-02T12:15:27Z` First payments marked PENDING_RETRY due to rejected mTLS handshake [F2]
- `2026-04-02T12:16:44Z` First 'tls: expired certificate (alert 45)' bank-partner-api call failure [F3]
- `2026-04-02T12:24:00Z` bank_api_error_rate reaches 100%, payment_success_rate falls to ~2.5% [F4, F5]
- `2026-04-02T12:32:00Z` Incident INC-005 reported by support lead []
- `2026-04-02T13:59:00Z` Latest data: errors still ongoing, error_rate panel still green [F6]

## Recommended actions
- **[immediate]** Issue and install a renewed client certificate for CN=payments-client.orangeinvest.internal (or roll a valid one) and confirm bank-partner-api mTLS handshakes succeed again. -- The expired cert (notAfter 12:15:00Z) is the direct cause of 100% bank_api_error_rate and ~2% payment success. [F1, F3, F4, F5]
- **[immediate]** Reprocess/retry the payments accumulated in PENDING_RETRY once mTLS is restored. -- Payments were deferred rather than lost; customers need the transfers completed. [F2, F13]
- **[short_term]** Add an alert on bank_api_error_rate / payment_success_rate and on cert expiry lead time (not just HTTP error_rate), since the standard 5xx panel stayed green. -- The failure was invisible on the HTTP error-rate dashboard; PENDING_RETRY masking makes this a recurring blind spot. [F6, F13]
- **[long_term]** Automate certificate renewal/rotation with proactive expiry alerting for all services. -- Matches root cause of historical incident HIST-106 ('no renewal automation'); prevents recurrence. [F11]

## Data gaps
- No telemetry for bank-partner-api (has_telemetry=false); could not directly confirm partner-side health, though error text attributes failure to our client cert.
- No certificate-management/secret-store tool was available to confirm whether a renewal was attempted or failed between 12:15Z and 13:59Z.
- Log results were truncated (truncated=true) in several queries; counts are lower bounds but the dominant patterns are consistent across queries.

## Integrity warnings (set by code)
- timeline: removed citations to non-existent findings ['unknown:F14']

## Investigation stats
- latency 34.25s, LLM calls 4, tokens 43649 (in 37493 / out 6156)
- tool calls 14 (0 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
