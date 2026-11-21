# Incident investigation report: INC-005
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Customer payment failures began at ~12:15Z because payment-service's mTLS client certificate (CN=payments-client.orangeinvest.internal) expired at 2026-04-02T12:15:00Z, so every outbound mTLS handshake to bank-partner-api was rejected with TLS alert 45. bank_api_error_rate jumped from 0.2% to 100% and payment_success_rate fell from ~99% to ~2.5%. The HTTP error-rate dashboard stayed green because failures are recorded as payment PENDING_RETRY rather than 5xx responses (error_rate flat at 0.22%, latency actually dropped as calls failed fast). A config change (timeout 3000->5000 at 11:20Z, 55 min before onset), a settlement-service deploy (10:30Z, unrelated service), and resource exhaustion were all ruled out. This is a credential_expiry incident on payment-service; precedent exists (HIST-106, settlement mTLS cert expiry with no renewal automation).

## Probable root cause
**Expired mTLS client certificate to bank-partner-api** -- credential_expiry in `payment-service` (confidence 0.92, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired mTLS client certificate to bank-partner-api | credential_expiry | payment-service | - | 0.92 (high) | active |
| 2 | H2: Bank partner API outage (partner-side fault) | dependency_failure | bank-partner-api | - | 0.05 (low) | rejected |
| 3 | H3: bank.api.timeout_ms config change (3000->5000) caused the failures | config_change | payment-service | cfg-2502 | 0.03 (low) | rejected |
| 4 | H5: Resource exhaustion in payment-service | resource_exhaustion | payment-service | - | 0.03 (low) | rejected |
| 5 | H4: settlement-service deploy dep-5510 caused it | bad_deployment | settlement-service | dep-5510 | 0.02 (low) | rejected |

### H1: Expired mTLS client certificate to bank-partner-api
payment-service's client certificate CN=payments-client.orangeinvest.internal expired at 2026-04-02T12:15:00Z, causing every mTLS handshake to bank-partner-api to be rejected (TLS alert 45). All bank calls failed, fund transfers were marked PENDING_RETRY, and customer payments failed. The fault originates in credential lifecycle management for payment-service's outbound cert, not in the code or the partner.

**Causal chain**
1. Certificate notAfter=2026-04-02T12:15:00Z; cert-watcher warned 25 min earlier [F4]
2. At 12:15:27Z mTLS handshakes to bank-partner-api begin being rejected (alert 45 expired certificate) [F5, F6]
3. bank_api_error_rate jumps 0.2% -> 75% (12:12) -> 100% (12:24); payment_success_rate collapses 99% -> 2.5% [F2, F3]
4. Failures surface as PENDING_RETRY instead of HTTP 5xx, so the error_rate dashboard stays green [F1, F16]

**Supporting evidence**
  - `F2` ✔ payment-service bank_api_error_rate was ~0.2% until 12:00Z, then rose to 75% at 12:12Z and 100% from 12:24Z onward.
  - `F3` ✔ payment-service payment_success_rate collapsed from ~99% to 26.8% at 12:12Z and ~2.5% from 12:24Z onward.
  - `F4` ✔ cert-watcher logged at 11:50:26Z that client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes at notAfter=2026-04-02T12:15:00Z.
  - `F5` ✔ From 12:15:27Z payment-service logged 140 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' errors, first-seen 12:15:27Z.
  - `F6` ✔ payment-service logged 171 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' errors, first-seen 12:16:44Z.
  - `F15` ✔ The failure mode is distinct from a partner-side outage: the log identifies OUR client cert (CN=payments-client.orangeinvest.internal) as the expired element, with the server responding 'alert 45' (certificate_expired).
  - `F16` ✔ payment 5xx counter is green because payment-service absorbs bank failures as PENDING_RETRY rather than returning HTTP errors, so the dashboard metric never fires.
**Contradicting evidence**
  - `F14` ✔ bank-partner-api is an external dependency with has_telemetry=false, so no direct vendor-side telemetry is available.
**Assumptions**
  - The cert chain is served by payment-service itself (CN prefix 'payments-') and not by an intermediate CA that also expired
  - No automatic renewal ran after 12:15Z (errors continue to 13:58Z, so consistent)

### H2: Bank partner API outage (partner-side fault)
bank-partner-api, the external vendor, returned errors for the whole window, failing fund transfers as in HIST-105. This would be a dependency_failure originating at the vendor.

**Causal chain**
(none given)

**Supporting evidence**
  - `F14` ✔ bank-partner-api is an external dependency with has_telemetry=false, so no direct vendor-side telemetry is available.
**Contradicting evidence**
  - `F4` ✔ cert-watcher logged at 11:50:26Z that client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes at notAfter=2026-04-02T12:15:00Z.
  - `F6` ✔ payment-service logged 171 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' errors, first-seen 12:16:44Z.
  - `F15` ✔ The failure mode is distinct from a partner-side outage: the log identifies OUR client cert (CN=payments-client.orangeinvest.internal) as the expired element, with the server responding 'alert 45' (certificate_expired).
**Rejected because:** The error message explicitly names OUR client cert as expired ('our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z') and the server returns TLS alert 45 (certificate_expired). A partner-side outage would not produce an expired-certificate alert about our own CN, and the onset matches our cert's notAfter to the second (12:15Z).

### H3: bank.api.timeout_ms config change (3000->5000) caused the failures
Raising the bank API timeout at 11:20Z induced slow/failed bank calls.

**Causal chain**
(none given)

**Supporting evidence**
  - `F12` ✔ The only config change was cfg-2502 on payment-service: bank.api.timeout_ms raised 3000->5000 at 11:20Z, ~55 min before onset.
**Contradicting evidence**
  - `F4` ✔ cert-watcher logged at 11:50:26Z that client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes at notAfter=2026-04-02T12:15:00Z.
  - `F6` ✔ payment-service logged 171 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' errors, first-seen 12:16:44Z.
  - `F7` ✔ payment-service latency_p99_ms actually DROPPED from ~320ms to ~140ms at 12:12Z (failing fast on TLS rejection), not increased.
**Rejected because:** The change landed at 11:20Z but onset is 12:15Z (55 min later, matching cert expiry exactly, not the change). Failures are TLS handshake rejections, unrelated to timeouts, and latency actually fell rather than rose.

### H5: Resource exhaustion in payment-service
CPU/memory/connection pool exhaustion caused the payment failures.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ payment-service cpu_pct was flat at ~30% and restarts were 0 across the window; db_pool_in_use was flat at ~8 connections.
**Rejected because:** cpu_pct flat ~30%, restarts 0, db_pool_in_use flat ~8. No resource pressure at all.

### H4: settlement-service deploy dep-5510 caused it
The v1.9.0 settlement-service deploy at 10:30Z introduced the fault.

**Causal chain**
(none given)

**Supporting evidence**
  - `F11` ✔ No payment-service code deployment occurred; the only deploy in the window was settlement-service dep-5510 v1.9.0 at 10:30Z, which completed rollout with all pods ready.
**Contradicting evidence**
  - `F13` ✔ settlement-service showed only GC-pause warnings (273ms/391ms) and normal INFO heartbeats; no failures aligned with the 12:15Z onset.
  - `F4` ✔ cert-watcher logged at 11:50:26Z that client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes at notAfter=2026-04-02T12:15:00Z.
**Rejected because:** Deploy completed cleanly at 10:30Z, 1h45m before onset, and settlement-service logs show only GC warnings. The failing component is payment-service's outbound cert to bank-partner-api.

## Facts (verified observations)
- payment-service HTTP error_rate was flat at ~0.22% during the outage (dashboard green). [F1]
- bank_api_error_rate went from ~0.2% to 100% starting 12:12-12:24Z. [F2]
- payment_success_rate collapsed from ~99% to ~2.5% starting 12:12-12:24Z. [F3]
- The client cert CN=payments-client.orangeinvest.internal had notAfter=2026-04-02T12:15:00Z, warned 25 min in advance by cert-watcher. [F4]
- payment-service logged mTLS handshake rejections and 'tls: expired certificate (alert 45)' naming its own client cert. [F5, F6]
- Latency fell and CPU/restarts/DB pool were flat, ruling out resource exhaustion and saturation. [F7, F10]
- No payment-service deployment occurred; only an unrelated settlement-service deploy and a bank.api.timeout_ms config change 55 min before onset. [F11, F12]
- api-gateway metrics were entirely flat, so the symptom was invisible at the edge. [F9]

## Assumptions (not verified)
- The cert chain served by payment-service is the only expired element (no expired intermediate CA).
- No automatic cert rotation succeeded after 12:15Z, consistent with errors persisting to 13:58Z.
- PENDING_RETRY payments are recoverable via replay once mTLS works.

## Timeline
- `2026-04-02T10:30:00Z` settlement-service dep-5510 v1.9.0 deployed and rolled out cleanly (unrelated). [F11]
- `2026-04-02T11:20:00Z` cfg-2502 bank.api.timeout_ms raised 3000->5000 on payment-service (ruled out). [F12]
- `2026-04-02T11:50:26Z` cert-watcher warns client cert expires at 12:15:00Z. [F4]
- `2026-04-02T12:15:00Z` Client certificate CN=payments-client.orangeinvest.internal expires. [F4, F6]
- `2026-04-02T12:15:27Z` First mTLS handshake rejection; payments marked PENDING_RETRY. [F5]
- `2026-04-02T12:12:00Z` bank_api_error_rate 75% and payment_success_rate 26.8% (bucket boundary). [F2, F3]
- `2026-04-02T12:24:00Z` bank_api_error_rate 100%, payment_success_rate ~2.5%. [F2, F3]
- `2026-04-02T12:32:00Z` Support reports ~40 failed-transfer tickets. []
- `2026-04-02T13:58:39Z` Errors still ongoing at latest data. [F6]

## Recommended actions
- **[immediate]** Immediately issue/renew the payment-service mTLS client certificate for bank-partner-api and roll it out, then verify mTLS handshakes succeed and payment_success_rate recovers. -- The expired cert is the confirmed cause; failures continue at latest data. [F4, F5, F6]
- **[immediate]** Replay/requeue the PENDING_RETRY payments accumulated since 12:15Z once the cert is restored. -- Fund transfers were deferred, not lost; customers need settlement. [F5]
- **[short_term]** Fix monitoring: alert on bank_api_error_rate and payment_success_rate (not just HTTP error_rate), since HTTP 5xx stayed green while payments failed. -- The dashboard masked a total payment outage for >1.5h. [F1, F2, F3]
- **[long_term]** Add certificate-expiry alerting and automated renewal for all mTLS client certs (recurrence of HIST-106). -- cert-watcher warned but no automated action taken; same class of incident happened before. [F4, F14]
- **[long_term]** Review whether payment failures should surface as elevated error rate instead of silently becoming PENDING_RETRY. -- Silent conversion to retry hid the impact from error-rate SLOs. [F16]

## Data gaps
- payment-service memory_pct query failed (backend 503) - memory exhaustion not directly confirmed, though CPU/restarts were flat.
- bank-partner-api has no telemetry (has_telemetry=false), so we cannot directly confirm the partner was healthy; the TLS alert content is the only signal from that side.
- No cert inventory/renewal-automation data available to confirm why the cert was not rotated before expiry.

## Investigation stats
- latency 52.7s, LLM calls 5, tokens 60447 (in 53302 / out 7145)
- tool calls 18 (1 failed, 0 partial, 2 recovered by retry), findings 16/16 verified
