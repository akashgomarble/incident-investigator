# Incident investigation report: INC-005
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
payment-service fund transfers began failing at 2026-04-02T12:15 when its client mTLS certificate (CN=payments-client.orangeinvest.internal) expired (notAfter=2026-04-02T12:15:00Z) and was not renewed. bank-partner-api correctly rejects the handshake with 'tls: expired certificate (alert 45)'; every outbound bank call now fails, so bank_api_error_rate goes to 100% and payment_success_rate drops from ~99% to ~2.5%. Because payments are marked PENDING_RETRY (a business-level outcome) rather than returning HTTP 5xx, the HTTP error_rate panel stays flat at ~0.22% and api-gateway logs nothing — this is why the dashboard stayed green despite the outage. The fault originates on our side (certificate lifecycle), not the bank: the error names our client cert. The unrelated cfg-2502 timeout change (11:20) and settlement-service deploy (10:30) are ruled out by timing and mechanism. Root cause category: credential_expiry.

## Probable root cause
**Expired payment-service client mTLS certificate breaks bank-partner-api calls** -- credential_expiry in `payment-service` (confidence 0.92, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired payment-service client mTLS certificate breaks bank-partner-api calls | credential_expiry | payment-service | - | 0.92 (high) | active |
| 2 | H2: bank-partner-api (external vendor) outage / dependency failure | dependency_failure | bank-partner-api | - | 0.04 (low) | rejected |
| 3 | H3: Config change cfg-2502 (bank API timeout 3000->5000ms) caused payment failures | config_change | payment-service | cfg-2502 | 0.02 (low) | rejected |
| 4 | H4: Resource exhaustion / capacity saturation on payment-service | resource_exhaustion | payment-service | - | 0.02 (low) | rejected |
| 5 | H5: settlement-service deployment dep-5510 caused the failures | bad_deployment | settlement-service | dep-5510 | 0.02 (low) | rejected |

### H1: Expired payment-service client mTLS certificate breaks bank-partner-api calls
payment-service's client certificate (CN=payments-client.orangeinvest.internal) expired at 2026-04-02T12:15:00Z and was not renewed. Its mTLS handshake to bank-partner-api is now rejected (TLS alert 45 'expired certificate'), so all fund transfers fail; payments are marked PENDING_RETRY rather than returning HTTP 5xx, which is why the HTTP error-rate dashboard stayed green.

**Causal chain**
1. Client cert CN=payments-client.orangeinvest.internal reached notAfter=2026-04-02T12:15:00Z and was not renewed [F5, F3]
2. Expired client cert -> mTLS handshake rejected by bank-partner-api (tls alert 45) [F3, F4]
3. bank_api_error_rate 0.2% -> 100%; payment_success_rate 99% -> ~2.5% [F2, F1]
4. Failures surface as PENDING_RETRY (business-level), not HTTP 5xx, so HTTP error_rate stays flat and the dashboard is green [F4, F1, F6]

**Supporting evidence**
  - `F1` ✔ payment-service 'payment_success_rate' collapses from ~99% to ~2.5% starting in the 2026-04-02T12:12 bucket (avg 26.82), while 'error_rate' stays flat at ~0.22% throughout.
  - `F2` ✔ payment-service 'bank_api_error_rate' jumps from ~0.2% to 100% at the 2026-04-02T12:12 bucket (avg 75.05, then 100.0).
  - `F3` ✔ payment-service logs show 171 occurrences of bank-partner-api call failure due to an expired client certificate: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', first seen 2026-04-02T12:16:44Z.
  - `F4` ✔ payment-service logs show 140 occurrences of 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected', first seen 2026-04-02T12:15:27Z.
  - `F5` ✔ A cert-watcher warning logged at 2026-04-02T11:50:26Z states the client certificate would expire: 'expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)'.
  - `F12` ✔ The error text explicitly frames the failure as OUR client side: 'our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' being rejected (alert 45) by the bank's server during handshake; the bank_partner returned a TLS alert, not a 5xx.
  - `F14` ✔ Failure onset (12:12-12:15) is ~55 minutes after the cfg-2502 timeout change (11:20) and ~1h45m after the unrelated settlement deploy (10:30), and 1 minute after the cert's stated notAfter time (12:15:00), aligning the failure with cert expiry rather than those changes.
  - `F6` ✔ api-gateway logged zero WARN/ERROR entries between 2026-04-02T12:00:00Z and 13:59:00Z (total_matched 0).
  - `F7` ✔ payment-service request_rate is unchanged across the incident (~100 req/s, 98.97-102.11 avg), i.e. traffic volume did not spike or collapse.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The cert CN=payments-client.orangeinvest.internal belongs to payment-service and is not shared with another service (no evidence of another owner).
  - No certificate auto-renewal occurred between 12:15 and 13:59 (the continuing 100% bank_api_error_rate implies the cert stayed expired).

### H2: bank-partner-api (external vendor) outage / dependency failure
The external bank-partner-api degraded, causing payment failures. The bank-side fault would originate outside our systems.

**Causal chain**
1. bank-partner-api failing -> payment calls error [F2]

**Supporting evidence**
  - `F2` ✔ payment-service 'bank_api_error_rate' jumps from ~0.2% to 100% at the 2026-04-02T12:12 bucket (avg 75.05, then 100.0).
  - `F11` ✔ payment-service depends on bank-partner-api (external, has_telemetry=false) and shared datastores ledger-db and kafka.
**Contradicting evidence**
  - `F3` ✔ payment-service logs show 171 occurrences of bank-partner-api call failure due to an expired client certificate: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', first seen 2026-04-02T12:16:44Z.
  - `F12` ✔ The error text explicitly frames the failure as OUR client side: 'our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' being rejected (alert 45) by the bank's server during handshake; the bank_partner returned a TLS alert, not a 5xx.
  - `F13` ✔ Historical incident HIST-105 (payment failures, bank partner API outage) was a dependency_failure with bank returning 5xx; the current incident differs in that the bank returned a TLS alert about our certificate, and HIST-106 (settlement mTLS cert expired, category credential_expiry) is the closer precedent.
**Assumptions**
  - None credible: the error text attributes the rejected cert to us, not to a partner-side error.
**Rejected because:** Logs show the bank returned a TLS alert about OUR client certificate (our client cert ... notAfter=...), not a partner-side 5xx. The bank is correctly rejecting an expired credential. This matches HIST-106 (our cert expiry), not HIST-105 (partner 5xx outage).

### H3: Config change cfg-2502 (bank API timeout 3000->5000ms) caused payment failures
Raising bank.api.timeout_ms at 11:20 caused the failure.

**Causal chain**
1. cfg-2502 at 11:20 -> failures [F10]

**Supporting evidence**
  - `F10` ✔ One config change occurred: cfg-2502 on payment-service, bank.api.timeout_ms 3000 -> 5000 at 2026-04-02T11:20:00Z (after partner advisory).
**Contradicting evidence**
  - `F3` ✔ payment-service logs show 171 occurrences of bank-partner-api call failure due to an expired client certificate: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', first seen 2026-04-02T12:16:44Z.
  - `F14` ✔ Failure onset (12:12-12:15) is ~55 minutes after the cfg-2502 timeout change (11:20) and ~1h45m after the unrelated settlement deploy (10:30), and 1 minute after the cert's stated notAfter time (12:15:00), aligning the failure with cert expiry rather than those changes.
  - `F7` ✔ payment-service request_rate is unchanged across the incident (~100 req/s, 98.97-102.11 avg), i.e. traffic volume did not spike or collapse.
**Assumptions**
  - None: a timeout value cannot cause a TLS certificate-expiry rejection.
**Rejected because:** Onset is 12:15, 55 min after the 11:20 change; the error is an mTLS cert-expiry alert (alert 45), which a timeout setting cannot produce. Request_rate and latency show no timeout-related behavior.

### H4: Resource exhaustion / capacity saturation on payment-service
Resource exhaustion (connections/memory/CPU) or load growth caused the failures.

**Causal chain**
1. Resource pressure -> payment failures *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ payment-service request_rate is unchanged across the incident (~100 req/s, 98.97-102.11 avg), i.e. traffic volume did not spike or collapse.
  - `F8` ✔ payment-service cpu_pct stays flat at ~30% and restarts stays at 0 across the whole window; db_pool_in_use ~8 connections with no ramp.
**Assumptions**
  - None.
**Rejected because:** cpu_pct flat ~30%, db_pool_in_use flat ~8, restarts 0, request_rate unchanged; and the error explicitly names an expired TLS certificate.

### H5: settlement-service deployment dep-5510 caused the failures
The 10:30 settlement-service v1.9.0 deploy broke payments.

**Causal chain**
1. settlement deploy -> payment failures [F9]

**Supporting evidence**
  - `F9` ✔ Only one code deployment occurred in the window: settlement-service v1.9.0 (dep-5510) at 2026-04-02T10:30:00Z ('new CSV exporter'); no payment-service deployment.
**Contradicting evidence**
  - `F3` ✔ payment-service logs show 171 occurrences of bank-partner-api call failure due to an expired client certificate: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', first seen 2026-04-02T12:16:44Z.
  - `F6` ✔ api-gateway logged zero WARN/ERROR entries between 2026-04-02T12:00:00Z and 13:59:00Z (total_matched 0).
**Assumptions**
  - None.
**Rejected because:** Settlement deploy was 1h45m before onset, only added a CSV exporter, and payment-service has no deploy; the failure mechanism (client cert expiry) is unrelated to settlement.

## Facts (verified observations)
- payment_success_rate fell from ~99% to ~2.5% at the 12:12 bucket; HTTP error_rate stayed ~0.22%. [F1]
- bank_api_error_rate rose from ~0.2% to 100% at 12:12. [F2]
- Logs report a TLS expired-certificate alert 45 citing our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z. [F3]
- payments are marked PENDING_RETRY on mTLS handshake rejection starting 12:15:27. [F4]
- A cert-watcher warned at 11:50:26 that the client certificate expired in 25 minutes (notAfter 12:15). [F5]
- api-gateway logged no WARN/ERROR entries in the window. [F6]
- request_rate, cpu_pct, db_pool_in_use and restarts were flat/unremarkable across the incident. [F7, F8]
- No payment-service deployment occurred; the only deploy was settlement-service v1.9.0 at 10:30. [F9]
- The only config change was bank.api.timeout_ms 3000->5000 at 11:20. [F10]

## Assumptions (not verified)
- CN=payments-client.orangeinvest.internal is payment-service's own client credential and not shared with another service.
- The certificate remained expired through 13:59 (consistent with bank_api_error_rate staying at 100%).
- bank-partner-api has no telemetry, so partner-side state is inferred from our logs only.

## Timeline
- `2026-04-02T10:30:00Z` settlement-service v1.9.0 (dep-5510, CSV exporter) deployed — unrelated [F9]
- `2026-04-02T11:20:00Z` payment-service config cfg-2502: bank.api.timeout_ms 3000 -> 5000 — unrelated to TLS [F10]
- `2026-04-02T11:50:26Z` cert-watcher warns client cert expires in 25 minutes (notAfter 12:15:00) [F5]
- `2026-04-02T12:15:00Z` Client mTLS certificate CN=payments-client.orangeinvest.internal expires [F3, F5]
- `2026-04-02T12:15:27Z` First 'PENDING_RETRY: mTLS handshake with bank-partner-api rejected' error [F4]
- `2026-04-02T12:16:44Z` First 'tls: expired certificate (alert 45)' error [F3]
- `2026-04-02T12:24:00Z` bank_api_error_rate saturates at 100%, payment_success_rate ~2.5% [F2, F1]
- `2026-04-02T12:32:00Z` Support reports ~40 failed-transfer tickets (incident reported) []

## Recommended actions
- **[immediate]** Renew/rotate the payment-service client certificate CN=payments-client.orangeinvest.internal and reload it (hot reload or rollout) so mTLS to bank-partner-api succeeds. -- The expired cert is the direct cause; bank_api_error_rate is 100% and all transfers fail until it is replaced. [F3, F4, F5]
- **[immediate]** Drain/replay the PENDING_RETRY payment backlog once mTLS is restored, and verify idempotency. -- 140+ payments were marked PENDING_RETRY and need reconcoliation once the bank channel is restored. [F4]
- **[short_term]** Add an alert on bank_api_error_rate and payment_success_rate (and on the cert-watcher expiry warning) so business-level failures page before customers escalate. -- The HTTP error-rate panel stayed green because failures surface as PENDING_RETRY; success-rate/bank-error metrics are the correct SLIs. [F1, F2, F6]
- **[long_term]** Implement automated certificate renewal/rotation with pre-expiry alerting (per HIST-106 precedent). -- This is a repeat class of incident (HIST-106: settlement mTLS cert expired; no renewal automation). [F5]

## Data gaps
- bank-partner-api has no telemetry (has_telemetry=false): we cannot directly confirm the partner's view or whether a renewed cert would be accepted.
- No deployment or config record of certificate-issuance/renewal tooling exists in the window, so we cannot see why renewal did not occur (process gap vs. failure).
- payment-service logs are truncated (returned 40 of 316); later entries (12:26-13:58) were not individually inspected, though the aggregated patterns cover them.

## Investigation stats
- latency 42.42s, LLM calls 5, tokens 52752 (in 45060 / out 7692)
- tool calls 18 (1 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
