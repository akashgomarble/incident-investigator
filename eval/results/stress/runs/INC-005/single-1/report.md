# Incident investigation report: INC-005
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Fund-transfer failures are caused by an expired client mTLS certificate on payment-service (CN=payments-client.orangeinvest.internal, notAfter=2026-04-02T12:15:00Z). From 12:15:27Z every call to bank-partner-api is rejected (TLS alert 45), so payments are set to PENDING_RETRY rather than returned as HTTP 5xx. That is why the HTTP error-rate panel stays ~0.2% (green) while the real signals diverge sharply: bank_api_error_rate rose to 100% and payment_success_rate collapsed to ~2.5%. The metric that would have caught this (bank_api_error_rate / payment_success_rate) simply wasn't the one on the dashboard. No payment-service deployment occurred and the only config change (timeout 3000->5000 at 11:20) predates onset and is unrelated; request_rate and CPU were flat and latency fell, ruling out load/saturation. This is credential_expiry, matching historical incident HIST-106 (same failure mode, no renewal automation).

## Probable root cause
**Expired payment-service client mTLS certificate breaks bank-partner-api calls** -- credential_expiry in `payment-service` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired payment-service client mTLS certificate breaks bank-partner-api calls | credential_expiry | payment-service | - | 0.90 (high) | active |
| 2 | H2: Bank partner API outage (dependency failure) | dependency_failure | bank-partner-api | - | 0.05 (low) | rejected |
| 3 | H3: Config change (bank.api.timeout_ms raise) caused failures | config_change | payment-service | cfg-2502 | 0.03 (low) | rejected |

### H1: Expired payment-service client mTLS certificate breaks bank-partner-api calls
The client certificate CN=payments-client.orangeinvest.internal used by payment-service for mTLS to bank-partner-api expired at 2026-04-02T12:15:00Z. bank-partner-api rejects the handshake (TLS alert 45), so all outbound payment/fund-transfer calls fail and are marked PENDING_RETRY. Because these are not surfaced as HTTP 5xx, the HTTP error_rate panel stays green while bank_api_error_rate hits 100% and payment_success_rate collapses to ~2.5%.

**Causal chain**
1. client cert expiry at 12:15:00Z [F2]
2. cert expiry -> mTLS handshake rejected by bank-partner-api (alert 45) [F1, F2]
3. handshake failure -> payments marked PENDING_RETRY, not 5xx [F6]
4. no 5xx -> HTTP error_rate flat while bank_api_error_rate=100% and success_rate~2.5% [F3, F4, F5]
5. customer-visible fund-transfer failures [F4]

**Supporting evidence**
  - `F1` ✔ payment-service logs show mTLS handshake rejections with bank-partner-api beginning 2026-04-02T12:15:27Z.
  - `F2` ✔ The client certificate used by payment-service to call bank-partner-api expired at 2026-04-02T12:15:00Z, causing TLS alert 45 (certificate expired).
  - `F3` ✔ payment-service bank_api_error_rate jumped from ~0.2% to 100% in the 12:12-12:20 buckets and stayed 100% through 13:56.
  - `F4` ✔ payment-service payment_success_rate collapsed from ~99% to ~2-3% in the 12:12-12:20 buckets and remained ~2-3% through 13:56.
  - `F5` ✔ payment-service HTTP error_rate remained flat ~0.2% across the entire window (11:00-13:56), explaining the green dashboard.
  - `F6` ✔ Failed payments are recorded as PENDING_RETRY rather than surfaced as HTTP 5xx, so the HTTP error-rate panel does not reflect them.
  - `F7` ✔ payment-service request_rate was stable ~100 req/s and latency_p99 DROPPED from ~320ms to ~140ms at onset (no load or saturation spike).
  - `F10` ✔ A prior incident HIST-106 (settlement-service, 2025-06-30) had the same pattern: client certificate expired, no renewal automation.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 'our client cert' referenced is the credential payment-service presents to bank-partner-api.
  - bank-partner-api enforces mTLS and rejects the expired client cert (consistent with alert 45 in logs).

### H2: Bank partner API outage (dependency failure)
bank-partner-api is degraded/unavailable (as in HIST-105), causing payment failures independent of our certificate.

**Causal chain**
1. partner outage -> payment failures *(assumed)*

**Supporting evidence**
  - `F3` ✔ payment-service bank_api_error_rate jumped from ~0.2% to 100% in the 12:12-12:20 buckets and stayed 100% through 13:56.
  - `F4` ✔ payment-service payment_success_rate collapsed from ~99% to ~2-3% in the 12:12-12:20 buckets and remained ~2-3% through 13:56.
**Contradicting evidence**
  - `F2` ✔ The client certificate used by payment-service to call bank-partner-api expired at 2026-04-02T12:15:00Z, causing TLS alert 45 (certificate expired).
  - `F1` ✔ payment-service logs show mTLS handshake rejections with bank-partner-api beginning 2026-04-02T12:15:27Z.
**Assumptions**
  - Logs would show 5xx/timeout rather than an explicit cert-expiry error.
**Rejected because:** Logs explicitly show a TLS 'expired certificate (alert 45)' error naming OUR client cert CN=payments-client.orangeinvest.internal, not partner-side 5xx/timeouts; endpoint is our credential failure.

### H3: Config change (bank.api.timeout_ms raise) caused failures
Raising bank.api.timeout_ms 3000->5000 at 11:20 caused payment failures.

**Causal chain**
1. timeout change -> failures *(assumed)*

**Supporting evidence**
  - `F8` ✔ The only config change in window was payment-service bank.api.timeout_ms 3000->5000 at 2026-04-02T11:20:00Z, ~55 min before onset.
**Contradicting evidence**
  - `F2` ✔ The client certificate used by payment-service to call bank-partner-api expired at 2026-04-02T12:15:00Z, causing TLS alert 45 (certificate expired).
  - `F1` ✔ payment-service logs show mTLS handshake rejections with bank-partner-api beginning 2026-04-02T12:15:27Z.
  - `F7` ✔ payment-service request_rate was stable ~100 req/s and latency_p99 DROPPED from ~320ms to ~140ms at onset (no load or saturation spike).
**Assumptions**
  - A timeout change could somehow produce an explicit cert-expiry error.
**Rejected because:** Change predates onset by ~55 min; latency did not increase (dropped), and error cause is an explicit cert expiry, unrelated to timeout value.

## Facts (verified observations)
- payment-service began logging mTLS handshake rejections to bank-partner-api at 12:15:27Z, with the client cert expired at 12:15:00Z. [F1, F2]
- bank_api_error_rate went to 100% and payment_success_rate fell to ~2.5% while HTTP error_rate stayed ~0.2%. [F3, F4, F5]
- Failures are recorded as PENDING_RETRY, not HTTP 5xx, hiding them from the error-rate panel. [F6]
- No payment-service deployment occurred; only a settlement-service deploy and an unrelated 11:20 timeout config change. [F8, F9]
- Traffic and resource metrics were normal/improving, ruling out load or saturation. [F7]
- A prior near-identical incident (HIST-106) was an expired client certificate with no renewal automation. [F10]

## Assumptions (not verified)
- The 'our client cert' in the log is the credential payment-service presents to bank-partner-api.
- bank-partner-api enforces mTLS and rejects the expired cert (consistent with alert 45).
- bank-partner-api has no telemetry (has_telemetry=false), so its side could not be directly queried.

## Timeline
- `2026-04-02T11:20:00Z` Config change cfg-2502: payment-service bank.api.timeout_ms 3000->5000 (unrelated). [F8]
- `2026-04-02T12:15:00Z` payment-service client mTLS cert CN=payments-client.orangeinvest.internal expires (notAfter). [F2]
- `2026-04-02T12:15:27Z` First payment marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected. [F1]
- `2026-04-02T12:16:44Z` First explicit 'tls: expired certificate (alert 45)' log; 171 such errors accumulate. [F1]
- `2026-04-02T12:12:00Z` bank_api_error_rate begins climbing (62.58 avg) toward 100%; payment_success_rate starts dropping. [F3, F4]
- `2026-04-02T12:32:00Z` Incident reported by support (reported service payment-service). []
- `2026-04-02T13:56:00Z` bank_api_error_rate still 100%, payment_success_rate still ~2.2%; incident ongoing. [F3, F4]

## Recommended actions
- **[immediate]** Rotate/renew the payment-service client certificate for bank-partner-api and force an immediate reload/restart of the mTLS client so handshakes succeed. -- The expired cert (notAfter 12:15:00Z) is the direct cause; renewal restores bank-partner-api calls. [F1, F2]
- **[immediate]** After renewal, replay/clear the PENDING_RETRY backlog of stranded fund transfers. -- 40+ customer transfers are stuck in PENDING_RETRY and not yet completed. [F6]
- **[short_term]** Add certificate-expiry alerting (e.g. <14 days to notAfter) and renewal automation, as was done after HIST-106. -- HIST-106 was the same failure mode; no renewal automation/expiry alert exists. [F10]
- **[short_term]** Fix dashboards/alerting to surface bank_api_error_rate and payment_success_rate (business-level signals), not just HTTP 5xx error_rate. -- PENDING_RETRY failures bypass the HTTP 5xx panel, giving a false-green dashboard. [F5, F6]

## Data gaps
- bank-partner-api is external with has_telemetry=false, so its own status/errors could not be independently confirmed (mitigated by explicit client-side cert-expiry error).
- No direct metric for the specific client certificate's validity/expiry was available; expiry is inferred from the explicit log line.
- PENDING_RETRY backlog size and whether retries eventually succeed after renewal were not measurable from available data.

## Investigation stats
- latency 38.03s, LLM calls 4, tokens 44801 (in 39047 / out 5754)
- tool calls 14 (0 failed, 0 partial, 4 recovered by retry), findings 11/11 verified
