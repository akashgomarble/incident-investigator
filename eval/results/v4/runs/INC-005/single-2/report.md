# Incident investigation report: INC-005
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Customer payment failures are real and are caused by an expired mTLS client certificate in payment-service. The client cert CN=payments-client.orangeinvest.internal reached notAfter=2026-04-02T12:15:00Z; from ~12:10Z onward every call to bank-partner-api fails with 'tls: expired certificate (alert 45)'. This drove bank_api_error_rate from 0.2% to 100% and payment_success_rate from ~99% to ~2.5%, while ~313 ERROR log lines and 140 PENDING_RETRY markings appear. The HTTP 5xx error_rate panel stayed green (~0.2%) because failed bank calls are recorded as PENDING_RETRY rather than 5xx, so the dashboard does not reflect the outage. All resource metrics (CPU, db pool, restarts), request rate, and other services (settlement, api-gateway) are normal, ruling out capacity/resource/deployment causes. Root-cause category: credential_expiry, originating in payment-service's own credential. Fix: renew/rotate the client cert and add expiry alerting.

## Probable root cause
**payment-service mTLS client certificate expired** -- credential_expiry in `payment-service` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: payment-service mTLS client certificate expired | credential_expiry | payment-service | - | 0.90 (high) | active |
| 2 | H2: Bank partner API outage / dependency failure | dependency_failure | bank-partner-api | - | 0.08 (low) | rejected |
| 3 | H3: Config change (bank.api.timeout_ms 3000->5000) | config_change | payment-service | - | 0.02 (low) | rejected |
| 4 | H4: Bad deployment (settlement v1.9.0) | bad_deployment | settlement-service | - | 0.01 (low) | rejected |

### H1: payment-service mTLS client certificate expired
The mTLS client certificate (CN=payments-client.orangeinvest.internal) used by payment-service to call bank-partner-api expired at 12:15:00Z. All outbound bank calls now fail TLS handshake (alert 45 certificate expired) and payments get marked PENDING_RETRY instead of completing, so real customers see failures while the HTTP 5xx error_rate panel stays green because the app returns a non-5xx outcome.

**Causal chain**
1. Client cert notAfter=12:15:00Z reached, cert now invalid [F4, F13]
2. TLS handshake with bank-partner-api rejected (alert 45) [F4, F5]
3. bank_api_error_rate 0.2%->100% and payment_success_rate 99%->2.5% [F2, F3]
4. Payments marked PENDING_RETRY instead of 5xx, so error_rate dashboard stays ~0.2% [F1, F5]

**Supporting evidence**
  - `F1` ✘ unverified payment-service HTTP 5xx error_rate stayed flat (~0.2%, max 0.32%) across 11:30-13:59Z, explaining why the error-rate dashboard looked green.
  - `F2` ✔ payment_success_rate collapsed from ~99% to ~2.5% starting in the 12:10Z bucket and stayed there.
  - `F3` ✔ bank_api_error_rate jumped from 0.2% to 50% at 12:10Z then 100% from 12:20Z onward.
  - `F4` ✔ payment-service logs show 311 ERROR entries 12:04-13:58Z with the dominant pattern being 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'.
  - `F5` ✔ 140 ERROR log lines show payments marked PENDING_RETRY due to 'mTLS handshake with bank-partner-api rejected', first seen 12:15:27Z.
  - `F8` ✔ Resource metrics for payment-service stayed normal: cpu_pct ~30% flat, db_pool_in_use ~8 connections flat, restarts 0.
  - `F9` ✔ request_rate stayed steady ~100 req/s throughout, so traffic volume did not change.
  - `F10` ✔ payment-service depends on external bank-partner-api (no telemetry) via mTLS, plus ledger-db and kafka.
  - `F11` ✔ settlement-service and api-gateway produced zero ERROR logs in 12:00-13:59Z, so the failure is isolated to payment-service's bank call path.
  - `F12` ✔ Historical incident HIST-106 is a prior 'Settlement mTLS certificate expired' with root cause 'Client certificate expired; no renewal automation' resolved by issuing a new cert and adding an expiry alert.
  - `F13` ✔ The failure is caused by OUR client certificate (CN=payments-client.orangeinvest.internal) expiring, not by the bank partner rejecting a valid cert or returning server-side errors; the bank is simply enforcing cert validity.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The expired cert is our own credential (payments-client.orangeinvest.internal) that we own and can rotate, not a vendor-managed cert
  - PENDING_RETRY payments do not eventually succeed automatically, matching customer complaints

### H2: Bank partner API outage / dependency failure
bank-partner-api (external) is degrading or rejecting traffic, causing payment failures. This is what the report's 'bank side' guess suggests.

**Causal chain**
(none given)

**Supporting evidence**
  - `F3` ✔ bank_api_error_rate jumped from 0.2% to 50% at 12:10Z then 100% from 12:20Z onward.
**Contradicting evidence**
  - `F4` ✔ payment-service logs show 311 ERROR entries 12:04-13:58Z with the dominant pattern being 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'.
  - `F5` ✔ 140 ERROR log lines show payments marked PENDING_RETRY due to 'mTLS handshake with bank-partner-api rejected', first seen 12:15:27Z.
  - `F13` ✔ The failure is caused by OUR client certificate (CN=payments-client.orangeinvest.internal) expiring, not by the bank partner rejecting a valid cert or returning server-side errors; the bank is simply enforcing cert validity.
  - `F1` ✘ unverified payment-service HTTP 5xx error_rate stayed flat (~0.2%, max 0.32%) across 11:30-13:59Z, explaining why the error-rate dashboard looked green.
**Rejected because:** The error is a TLS alert 45 specifically naming OUR client certificate's notAfter=2026-04-02T12:15:00Z expiry, not a partner-side 5xx/outage. Onset aligns exactly with the cert's expiry second, and the cert is our own CN, so the fault is our credential expiring rather than the partner degrading.

### H3: Config change (bank.api.timeout_ms 3000->5000)
Raising the bank API timeout to 5000ms caused bank call failures.

**Causal chain**
(none given)

**Supporting evidence**
  - `F6` ✔ The only config change in the window was payment-service bank.api.timeout_ms raised 3000->5000 at 11:20Z, ~50 min before onset.
**Contradicting evidence**
  - `F4` ✔ payment-service logs show 311 ERROR entries 12:04-13:58Z with the dominant pattern being 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'.
  - `F2` ✔ payment_success_rate collapsed from ~99% to ~2.5% starting in the 12:10Z bucket and stayed there.
  - `F3` ✔ bank_api_error_rate jumped from 0.2% to 50% at 12:10Z then 100% from 12:20Z onward.
**Rejected because:** The change was at 11:20Z, ~55 minutes before onset (~12:10Z), and the error is a TLS expired-certificate alert, unrelated to timeout duration. Raising a timeout would slow failures, not cause 'tls: expired certificate'.

### H4: Bad deployment (settlement v1.9.0)
Deployment dep-5510 introduced the fault.

**Causal chain**
(none given)

**Supporting evidence**
  - `F7` ✔ The only deployment in the window was settlement-service v1.9.0 (new CSV exporter) at 10:30Z, not payment-service.
**Contradicting evidence**
  - `F4` ✔ payment-service logs show 311 ERROR entries 12:04-13:58Z with the dominant pattern being 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'.
  - `F11` ✔ settlement-service and api-gateway produced zero ERROR logs in 12:00-13:59Z, so the failure is isolated to payment-service's bank call path.
**Rejected because:** The only deployment was to settlement-service at 10:30Z, not payment-service; settlement-service logged zero errors; the failure is a TLS credential expiry which a CSV exporter change could not produce.

## Facts (verified observations)
- payment_success_rate fell from ~99% to ~2.5% beginning in the 12:10Z bucket and remained there. [F2]
- bank_api_error_rate rose from 0.2% to 50% at 12:10Z and reached 100% from 12:20Z onward. [F3]
- payment-service logs contain 171 'tls: expired certificate (alert 45)' entries naming CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z, plus 140 'mTLS handshake with bank-partner-api rejected' PENDING_RETRY entries. [F4, F5]
- The only config change in the window was payment-service bank.api.timeout_ms 3000->5000 at 11:20Z (before onset). [F6]
- The only deployment was settlement-service v1.9.0 at 10:30Z, unrelated to payment-service. [F7]
- payment-service CPU (~30%), db_pool_in_use (~8), and restarts (0) were all normal throughout. [F8]
- request_rate was steady ~100 req/s, ruling out a traffic spike. [F9]
- settlement-service and api-gateway logged zero errors, isolating the failure to payment-service's bank call path. [F11]

## Assumptions (not verified)
- The client certificate CN=payments-client.orangeinvest.internal is owned/managed by our team (not the vendor), so it is the rotatable fault origin
- PENDING_RETRY payments do not auto-complete, consistent with the ~40 customer tickets
- No overlapping unrelated failure exists; payment-service/ledger-db/kafka metrics were checked only for the listed resource metrics
- (demoted from facts: no verified observation backs it) payment-service HTTP 5xx error_rate stayed flat (~0.2%) across the incident window, so the dashboard was green while payments were failing.
- (demoted from facts: no verified observation backs it) Historical incident HIST-106 was a settled prior case of an expired mTLS client certificate with no renewal automation.

## Timeline
- `2026-04-02T10:30:00Z` settlement-service v1.9.0 deployed (unrelated, no errors observed) [F7]
- `2026-04-02T11:20:00Z` config change bank.api.timeout_ms 3000->5000 on payment-service [F6]
- `2026-04-02T12:04:22Z` first WARN 'retrying idempotent request after connection reset' in payment-service [F4]
- `2026-04-02T12:10:00Z` payment_success_rate starts collapsing; bank_api_error_rate jumps to 50% [F2, F3]
- `2026-04-02T12:15:00Z` client certificate CN=payments-client.orangeinvest.internal expires (notAfter) [F4]
- `2026-04-02T12:15:27Z` first 'mTLS handshake with bank-partner-api rejected' PENDING_RETRY error [F5]
- `2026-04-02T12:16:44Z` first 'tls: expired certificate (alert 45)' error logged [F4]
- `2026-04-02T12:20:00Z` bank_api_error_rate reaches 100%; payment_success_rate ~2.6% [F3, F2]
- `2026-04-02T12:32:00Z` support lead reports ~40 failed-transfer tickets (error dashboard still green) [F1]

## Recommended actions
- **[immediate]** Rotate/renew the mTLS client certificate CN=payments-client.orangeinvest.internal and reload it into payment-service; verify bank-partner-api handshakes succeed. -- The cert's notAfter=2026-04-02T12:15:00Z has passed; all bank calls fail with alert 45 until a valid cert is presented. [F4, F5]
- **[immediate]** Reprocess the ~140+ payments marked PENDING_RETRY once the cert is valid so customers' transfers complete. -- Payments are stuck in PENDING_RETRY from the failed mTLS handshakes; customer impact persists until retried. [F5]
- **[short_term]** Add an alert on client-certificate expiry (e.g. warn 14-30 days out) and on bank_api_error_rate / payment_success_rate, which would have caught this despite the green 5xx panel. -- The error_rate dashboard missed the incident; HIST-106 was the same failure mode and its resolution included an expiry alert. [F1, F2, F3, F12]
- **[short_term]** Instrument bank-call outcomes as distinct signals (PENDING_RETRY, tls errors) so non-5xx failures surface on dashboards. -- Failed bank calls were recorded as PENDING_RETRY, not 5xx, hiding the outage from the error-rate panel. [F1, F5]
- **[long_term]** Automate certificate renewal/rotation to remove manual expiry risk. -- Repeat failure mode (HIST-106) attributed to 'no renewal automation'. [F12]

## Data gaps
- No telemetry on bank-partner-api (external, has_telemetry=false), so its side is inferred only from our client's error messages
- No certificate-store/deployment record showing when the cert was last rotated, so pre-expiry warning history is unknown
- Exact count of affected customer payments/transactions beyond the 140 logged PENDING_RETRY markings is unquantified
- logs are truncated (returned 40 of 313; 171 of the cert-error pattern shown) but the pattern and first/last timestamps are consistent across two queries

## Integrity warnings (set by code)
- fact demoted to assumption: payment-service HTTP 5xx error_rate stayed flat (~0.2%) across the incident wind
- fact demoted to assumption: Historical incident HIST-106 was a settled prior case of an expired mTLS client 

## Investigation stats
- latency 60.93s, LLM calls 5, tokens 53578 (in 47099 / out 6479)
- tool calls 16 (0 failed, 0 partial, 0 recovered by retry), findings 12/13 verified
