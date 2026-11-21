# Incident investigation report: INC-005
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Payment failures began at ~12:15Z on 2026-04-02 and are caused by the expiry of payment-service's client mTLS certificate CN=payments-client.orangeinvest.internal (notAfter=2026-04-02T12:15:00Z). After expiry, all calls to bank-partner-api fail the TLS handshake with 'remote error: tls: expired certificate (alert 45)' and payments are queued as PENDING_RETRY. payment_success_rate collapsed from ~99% to ~2.5% and bank_api_error_rate rose to 100%, but the HTTP error_rate panel stayed flat (~0.2%) because no 5xx is returned — successfully hiding the incident from the dashboard. Resource metrics, traffic, deployments and the unrelated timeout config change were all ruled out. This matches historical incident HIST-106 (credential_expiry, no renewal automation). Remediation: renew/rotate the client certificate and add expiry-aware rotation automation.

## Probable root cause
**payment-service client mTLS certificate expired, breaking bank-partner-api calls** -- credential_expiry in `payment-service`, triggered by `null` (confidence 0.95, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: payment-service client mTLS certificate expired, breaking bank-partner-api calls | credential_expiry | payment-service | null | 0.95 (high) | active |
| 2 | H2: Bank partner API-side outage (dependency_failure) | dependency_failure | bank-partner-api | - | 0.10 (low) | rejected |
| 3 | H3: Resource exhaustion / capacity saturation on payment-service | resource_exhaustion | payment-service | - | 0.05 (low) | rejected |
| 4 | H4: Config change (bank.api.timeout_ms 3000->5000) caused the fault | config_change | payment-service | cfg-2502 | 0.03 (low) | rejected |

### H1: payment-service client mTLS certificate expired, breaking bank-partner-api calls
The client certificate CN=payments-client.orangeinvest.internal used by payment-service to authenticate to bank-partner-api expired at 2026-04-02T12:15:00Z. All outbound bank calls then failed the mTLS handshake, so transfers were marked PENDING_RETRY and payment_success_rate fell to ~2.5% while bank_api_error_rate hit 100%. The HTTP error_rate panel stayed green because payment-service queues retries instead of returning 5xx. Fault originates in payment-service credential management.

**Causal chain**
1. Client cert CN=payments-client.orangeinvest.internal had notAfter=2026-04-02T12:15:00Z [F4]
2. At 12:15 the cert expired; bank-partner-api mTLS handshakes were rejected with alert 45 [F5]
3. Failed bank calls -> payments marked PENDING_RETRY (no HTTP 5xx) [F6]
4. payment_success_rate collapsed to ~2.5% and bank_api_error_rate hit 100%, while error_rate stayed ~0.2% [F2, F3, F1]

**Supporting evidence**
  - `F1` ✔ payment-service HTTP error_rate remained flat (~0.2-0.32%) across the whole window including onset, so the error-rate panel is genuinely green.
  - `F2` ✔ payment_success_rate collapsed from ~99% to ~2.5% starting at the 12:10-12:20 bucket and stayed there.
  - `F3` ✔ bank_api_error_rate jumped from ~0.2% to 100% at the 12:10-12:20 bucket and remained 100%.
  - `F4` ✔ cert-watcher warned at 11:50:26Z that the client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z).
  - `F5` ✔ From 12:15-12:16 onward, payment-service logs show bank-partner-api calls failing with 'remote error: tls: expired certificate (alert 45)' referencing notAfter=2026-04-02T12:15:00Z.
  - `F6` ✔ Failed payments are marked 'PENDING_RETRY' due to rejected mTLS handshake (140 occurrences), explaining why no HTTP 5xx are emitted.
  - `F7` ✔ payment-service latency_p99_ms DECREASED at onset (from ~320ms to ~140ms), consistent with requests failing fast at the mTLS handshake rather than timing out.
  - `F13` ✔ Prior HIST-106 (settlement mTLS certificate expired, 2025-06-30) had the same category 'credential_expiry' with root cause 'Client certificate expired; no renewal automation'.
**Contradicting evidence**
  - (none)
**Assumptions**
  - No cert renewal occurred between the 11:50 warning and expiry
  - bank-partner-api correctly rejects an expired client cert (alert 45 is standard TLS expired-certificate alert)

### H2: Bank partner API-side outage (dependency_failure)
bank-partner-api itself was failing, causing payment failures. Contradicted because logs explicitly report OUR client cert expired (alert 45, notAfter=2026-04-02T12:15:00Z) — the rejection is triggered by our expired credential, and onset exactly matches our cert's notAfter.

**Causal chain**
1. bank-partner-api returned errors to payment-service [F3]

**Supporting evidence**
  - `F3` ✔ bank_api_error_rate jumped from ~0.2% to 100% at the 12:10-12:20 bucket and remained 100%.
**Contradicting evidence**
  - `F5` ✔ From 12:15-12:16 onward, payment-service logs show bank-partner-api calls failing with 'remote error: tls: expired certificate (alert 45)' referencing notAfter=2026-04-02T12:15:00Z.
  - `F4` ✔ cert-watcher warned at 11:50:26Z that the client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z).
**Rejected because:** Error message is a TLS expired-certificate alert naming our own client cert with notAfter equal to the onset; not a partner 5xx. HIST-105 (partner 5xx) doesn't fit the message.

### H3: Resource exhaustion / capacity saturation on payment-service
CPU, memory, DB connection pool or traffic overload caused the failures. Contradicted: cpu flat ~30%, db_pool_in_use flat ~8, restarts 0, request_rate steady ~100 req/s, and latency DECREASED (fast handshake failures, not saturation).

**Causal chain**
1. Resource pressure -> failed payments [F8]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ payment-service cpu_pct stayed flat at ~30% and db_pool_in_use flat at ~8 connections; restarts remained 0 throughout.
  - `F11` ✔ payment-service request_rate stayed steady at ~100 req/s through the incident — traffic did not surge (rules out capacity saturation).
  - `F7` ✔ payment-service latency_p99_ms DECREASED at onset (from ~320ms to ~140ms), consistent with requests failing fast at the mTLS handshake rather than timing out.
**Rejected because:** All saturation indicators flat or improving; latency fell rather than rising.

### H4: Config change (bank.api.timeout_ms 3000->5000) caused the fault
The 11:20 timeout config change broke payments. Contradicted: it predates onset by 55 minutes, changes only a timeout (not TLS trust/renewal), and onset aligns exactly with cert notAfter=12:15. Also a timeout increase could not produce an 'expired certificate' alert.

**Causal chain**
1. cfg-2502 at 11:20 -> failures at 12:15 [F10]

**Supporting evidence**
  - `F10` ✔ The only payment-service config change was bank.api.timeout_ms 3000->5000 at 11:20:00Z — 55 minutes before the 12:15 onset and unrelated to TLS.
**Contradicting evidence**
  - `F5` ✔ From 12:15-12:16 onward, payment-service logs show bank-partner-api calls failing with 'remote error: tls: expired certificate (alert 45)' referencing notAfter=2026-04-02T12:15:00Z.
  - `F4` ✔ cert-watcher warned at 11:50:26Z that the client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z).
  - `F10` ✔ The only payment-service config change was bank.api.timeout_ms 3000->5000 at 11:20:00Z — 55 minutes before the 12:15 onset and unrelated to TLS.
**Rejected because:** Timing mismatch (55 min gap) and semantically unrelated to mTLS certificate errors.

## Facts (verified observations)
- payment-service HTTP error_rate stayed flat (~0.2-0.32%) through the incident. [F1]
- payment_success_rate collapsed from ~99% to ~2.5% starting at 12:10-12:20Z. [F2]
- bank_api_error_rate rose to 100% at 12:10-12:20Z. [F3]
- cert-watcher warned the payments-client certificate would expire at 2026-04-02T12:15:00Z. [F4]
- bank-partner-api calls failed with 'tls: expired certificate (alert 45)' from 12:16Z. [F5]
- Failed payments were marked PENDING_RETRY due to rejected mTLS handshake. [F6]
- Latency decreased at onset (fast handshake failures, not timeouts). [F7]
- CPU, DB pool and restarts were flat; no resource exhaustion. [F8]
- No payment-service deployment; only an unrelated settlement-service deploy at 10:30Z. [F9]
- Only config change was bank.api.timeout_ms at 11:20Z, unrelated to TLS and 55 min before onset. [F10]
- Request rate was steady ~100 req/s; no traffic surge. [F11]
- api-gateway logged no errors. [F12]
- HIST-106 was a prior credential_expiry incident (client cert expired, no renewal automation). [F13]

## Assumptions (not verified)
- cert-watcher's notAfter=2026-04-02T12:15:00Z is authoritative for the client cert's expiry.
- TLS alert 45 denotes certificate_expired and is emitted because our client cert is expired, not the partner's.
- No certificate rotation occurred between the 11:50 warning and expiry.

## Timeline
- `2026-04-02T10:30:00Z` settlement-service v1.9.0 deployed (CSV exporter) — unrelated [F9]
- `2026-04-02T11:20:00Z` payment-service config change bank.api.timeout_ms 3000->5000 — unrelated to TLS [F10]
- `2026-04-02T11:50:26Z` cert-watcher warns payments-client certificate expires in 25 min (notAfter 12:15Z) [F4]
- `2026-04-02T12:10:00Z` payment_success_rate begins to fall; bank_api_error_rate begins to rise (partial bucket) [F2, F3]
- `2026-04-02T12:15:00Z` client certificate expires; mTLS handshakes to bank-partner-api rejected [F4, F5]
- `2026-04-02T12:15:27Z` first payments marked PENDING_RETRY (mTLS rejected) [F6]
- `2026-04-02T12:16:44Z` first 'tls: expired certificate (alert 45)' errors from bank-partner-api calls [F5]
- `2026-04-02T12:20:00Z` payment_success_rate steady at ~2.5%, bank_api_error_rate 100% [F2, F3]
- `2026-04-02T12:32:00Z` support lead reports ~40 failed-transfer tickets; error-rate panel still green [F1]

## Recommended actions
- **[immediate]** Renew and install a valid client certificate for CN=payments-client.orangeinvest.internal and restart/reload payment-service TLS config -- The expired cert (notAfter 12:15Z) is the direct cause of all bank-partner-api handshake failures. [F4, F5]
- **[immediate]** Reprocess/retry the payments queued as PENDING_RETRY and verify success_rate recovers to ~99% -- Fund transfers are stuck; customers reported ~40 failures. [F6, F2]
- **[short_term]** Fix alerting: add a firing alarm on payment_success_rate and bank_api_error_rate (not just HTTP error_rate), since PENDING_RETRY masks failures from the 5xx panel -- The green error_rate dashboard hid a full payment outage. [F1, F2, F3]
- **[long_term]** Implement automated certificate rotation with expiry alerting at multiple lead times satisfying the cert-watcher warning -- HIST-106 was the same credential_expiry pattern; recurrence shows the renewal gap was never fixed. [F13, F4]

## Data gaps
- bank-partner-api has no telemetry (has_telemetry:false), so we cannot independently confirm the partner side behaved normally; H2 (partner outage) is rejected on the strength of our own error messages rather than partner-side evidence.
- No direct metric for certificate validity/expiry time-series; the expiry is confirmed only via the cert-watcher warning log and the error text (notAfter=2026-04-02T12:15:00Z), not an observable metric.
- Root-cause of why the cert was not renewed (owner/process failure) is not visible in the available data — no cert-management or secret-store telemetry was queried.

## Investigation stats
- latency 39.79s, LLM calls 5, tokens 53802 (in 46936 / out 6866)
- tool calls 18 (0 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
