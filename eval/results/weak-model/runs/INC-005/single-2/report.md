# Incident investigation report: INC-005
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Payments failed because the client TLS certificate used by payment-service to call the external bank-partner-api expired at 12:15:00Z, causing mTLS handshake rejections. The internal error_rate metric stayed low because the failures occurred in downstream calls, not in HTTP responses to clients.

## Probable root cause
**Client TLS certificate expired** -- credential_expiry in `payment-service`, triggered by `N/A` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Client TLS certificate expired | credential_expiry | payment-service | N/A | 0.86 (high) | active |
| 2 | H2: External bank-partner-api TLS rejection (dependency failure) | dependency_failure | bank-partner-api | N/A | 0.45 (low) | rejected |

### H1: Client TLS certificate expired
The client certificate used by payment-service to authenticate to bank-partner-api expired at 2026-04-02T12:15:00Z, causing mTLS handshake rejections and payment failures.

**Causal chain**
1. cert expiry -> mTLS handshake rejected -> payment marked PENDING_RETRY -> customer sees failure [F2, F3, F1]

**Supporting evidence**
  - `F2` ✔ First error log at 2026-04-02T12:15:27Z: "payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected"
  - `F3` ✔ Repeated log entries show TLS failure: "bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z"
  - `F1` ✔ payment-service depends on external bank-partner-api (no telemetry) and on ledger-db and kafka; upstream caller is api-gateway.
**Contradicting evidence**
  - (none)

### H2: External bank-partner-api TLS rejection (dependency failure)
Bank-partner-api rejected connections due to TLS handshake issues, possibly a provider-side certificate problem, leading to payment failures.

**Causal chain**
1. bank-partner-api TLS rejection -> payment-service errors [F2, F3]

**Supporting evidence**
  - `F2` ✔ First error log at 2026-04-02T12:15:27Z: "payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected"
  - `F3` ✔ Repeated log entries show TLS failure: "bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z"
**Contradicting evidence**
  - `F3` ✔ Repeated log entries show TLS failure: "bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z"
**Assumptions**
  - The error message refers to the remote side, not the client cert.
**Rejected because:** Log explicitly states the client certificate expired ("our client cert ... notAfter=2026-04-02T12:15:00Z"), pointing to credential expiry rather than external provider failure.

## Facts (verified observations)
- TLS handshake failures originated from an expired client certificate. [F3]
- No recent code deployment could have introduced a bug. [F4]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-04-02T12:15:00Z` Client certificate expiration (notAfter timestamp). [F3]
- `2026-04-02T12:15:27Z` First observed mTLS handshake rejection logged. [F2]
- `2026-04-02T12:32:00Z` Support tickets start reporting payment failures. []

## Recommended actions
- **[immediate]** Rotate/renew the client TLS certificate for payment-service immediately. -- Restores ability to authenticate to bank-partner-api and stops payment failures. [F3]
- **[short_term]** Add monitoring for certificate expiry (e.g., alert 24h before notAfter). -- Prevents future outages caused by expired credentials.
- **[short_term]** Verify that downstream error metrics (e.g., bank_api_error_rate) are surfaced on dashboards. -- Allows operators to see downstream failures earlier. [F5]

## Data gaps
- Exact source and rotation schedule of the expired client certificate (e.g., secret store version) was not queried.
- No direct metric for bank-partner-api call failures; adding one would improve observability.

## Investigation stats
- latency 204.99s, LLM calls 6, tokens 26658 (in 22828 / out 3830)
- tool calls 5 (0 failed, 0 partial, 0 recovered by retry), findings 6/6 verified
