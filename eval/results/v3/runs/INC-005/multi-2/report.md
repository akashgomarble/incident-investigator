# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The payment failures are caused by the expiry of payment-service's bank-facing mTLS client certificate (CN=payments-client.orangeinvest.internal, notAfter=2026-04-02T12:15:00Z), which banks' TLS layer now rejects with alert 45 (expired certificate), so outbound fund transfers can no longer reach bank-partner-api. Confidence is high (0.78, the critic's cap), anchored on two independent signals in the same 12:12-12:19Z window: a second-precision log onset (first mTLS rejection 12:15:27Z, 27s after notAfter) and a collapse of bank_api_error_rate to 100% and payment_success_rate to ~2.4%. The "green" error-rate dashboard is literally correct but blind, because failed transfers are marked PENDING_RETRY on a non-HTTP settlement/retry path rather than returned as 5xx. Both in-window changes (cfg-2502 at 11:20Z, dep-5510 at 10:30Z) are time- and mechanism-inconsistent with onset, and no competing explanation names a different origin.

## Probable root cause
**payment-service bank-facing mTLS client cert expired at 12:15:00Z** -- credential_expiry in `payment-service (bank-facing mTLS client cert)`, triggered by `null` (confidence 0.78, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: payment-service bank-facing mTLS client cert expired at 12:15:00Z | credential_expiry | payment-service (bank-facing mTLS client cert) | null | 0.78 (high) | active |
| 2 | H4: External bank/PSP partner-side outage or rejection | dependency_failure | bank-partner-api (external bank/PSP) | null | 0.04 (low) | rejected |
| 3 | H2: Bad deployment: settlement-service v1.9.0 CSV exporter regression | bad_deployment | settlement-service | dep-5510 | 0.03 (low) | rejected |
| 4 | H3: Config change cfg-2502 (bank.api.timeout_ms 3000->5000) caused the failure | config_change | payment-service | cfg-2502 | 0.03 (low) | rejected |

### H1: payment-service bank-facing mTLS client cert expired at 12:15:00Z
The client certificate 'CN=payments-client.orangeinvest.internal' used by payment-service to authenticate to the external bank-partner-api expired at notAfter=2026-04-02T12:15:00Z. Immediately after expiry (first rejected handshake 12:15:27Z), every mTLS handshake to the bank was rejected, so outbound fund transfers could no longer reach the partner. Failed transfers were marked PENDING_RETRY inside the settlement/retry path rather than returned as HTTP 5xx, so the HTTP error-rate panel stayed flat (~0.22%) while payment_success_rate collapsed to ~2.4%. Confidence is set at the critic's cap (0.78) rather than higher because the key linkage hop — that the expired credential is specifically the one bound to the payment-service->bank-partner-api edge — is inferred from log-line wording, not confirmed against a cert subject/key binding or a cert-lifecycle source.

**Causal chain**
1. Client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z reaches expiry at 12:15Z [F4, F20, F21]
2. All mTLS handshakes from payment-service to bank-partner-api are rejected from 12:15:27Z onward (first NEW error, 27s after notAfter) [F4, F21, F25]
3. bank_api_error_rate jumps to 57.23% in the 12:12Z bucket then 100% from 12:19Z; payment_success_rate falls to ~2.4% [F2, F3]
4. Failed transfers are marked PENDING_RETRY (a non-HTTP settlement/retry path), so the HTTP error-rate panel remains flat at ~0.22% (75 ERROR vs 1 WARN) [F5, F1]
5. Customers observe ~40 failed fund transfers; success rate ~2.4% corroborates them; the error persists unchanged through 13:58Z (no rotation) [F3, F24, F23]

**Supporting evidence**
  - `F4` ✔ Low-level error signature onset: the earliest NEW error is at 12:15:27Z ('payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'); the first TLS cert-expiry line is at 12:16:44Z naming our client cert 'CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. 33+33+... PENDING_RETRY/TLS failures accumulate; only 1 WARN (a retry) precedes them at 12:04:22Z.
  - `F2` ✔ The bank-facing dependency metric shows a real collapse: payment-service bank_api_error_rate was ~0.2% baseline (11:30-12:05Z), then jumped to 57.23% in the 12:12Z bucket and hit 100% from 12:19Z onward (100% max for every bucket through 13:57Z). Onset is in the 12:12-12:19Z bucket.
  - `F3` ✔ payment_success_rate dropped from ~99% baseline to avg 44.15% in the 12:12Z bucket, then to ~2.4-2.7% for 12:19Z onward (all buckets through 13:57Z), i.e. fund transfers effectively stopped succeeding. This contradicts the green error_rate panel and corroborates the ~40 customer tickets.
  - `F1` ✔ payment-service HTTP error_rate stayed flat/healthy (~0.16-0.32% of requests, mean 0.22) across the entire window 11:30-13:57Z with no rise at any bucket, confirming the reported 'error-rate dashboard is green' is literally true for the HTTP 5xx panel.
  - `F5` ✔ The error path is a non-HTTP/settlement-side path: failed transfers are marked 'PENDING_RETRY' (not returned as 5xx), so they never surface in the HTTP error-rate metric. 75 ERROR logs vs 1 WARN in payment-service 12:04-12:39Z. This is the mechanism by which the HTTP error-rate panel is blind to the failure.
  - `F18` ✔ Precedent with the closest signature: HIST-106 (2025-06-30) 'Settlement mTLS certificate expired', services [settlement-service], symptoms ['tls handshake failures','expired certificate'], root_cause 'Client certificate expired; no renewal automation', category 'credential_expiry', resolution 'Issued new cert; added expiry alert' — matched on mTLS/cert-expiry keywords in two independent searches. Caveat (lead only, different service and no green-dashboard symptom): the current failure is on payment-service's bank-facing cert, not settlement-service's.
  - `F20` ✔ The REJECTED certificate is explicitly named as OUR client cert: all 5 TLS errors in 12:15-12:20Z read verbatim 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', matching the CN=payments-client.orangeinvest.internal / notAfter=2026-04-02T12:15:00Z given in the task.
  - `F21` ✔ Second-precision onset vs notAfter: the first mTLS handshake rejection is at 12:15:27Z ('payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'), 27 seconds after the cert notAfter=2026-04-02T12:15:00Z; the descriptive expired-certificate ERROR line arrives 77s later at 12:16:44Z. 7 PENDING_RETRY and 5 expired-cert lines all fall inside 12:15:00-12:20:00Z.
  - `F23` ✔ NO cert reload/renewal/'new certificate loaded' event appears for payment-service's bank-facing cert: a 'reload' search over 12:15-12:20Z returned 0 matches, and a 'renew' search over 12:00-13:59Z returned 0 matches.
  - `F24` ✔ The expired-cert error persists (no recovery/rotation) well past the report: searching 'certificate' in 12:14-13:59Z returns 171 ERROR matches, all the same pattern 'our client cert CN=payments-...', first seen 12:16:44Z and still logging at 'last_match':'2026-04-02T13:58:39Z'.
  - `F25` ✔ Direction of the cert check: the log phrase is 'remote error: tls: expired certificate (alert 45)' combined with 'our client cert ... notAfter=2026-04-02T12:15:00Z' — i.e. the peer rejected OUR client certificate as expired; the notAfter timestamp equals the reported expiry second and precedes the first rejection by 27s, which pins onset to the client-cert expiry rather than a still-valid-cert/truststore mismatch.
  - `F29` ✔ Historical cert precedent HIST-106 (2025-06-30, 'Settlement mTLS certificate expired', settlement-service) records root_cause 'Client certificate expired; no renewal automation' and resolution 'Issued new cert; added expiry alert', i.e. this environment has a prior credential_expiry incident whose remediation explicitly states renewal automation was absent — consistent with the current expiry happening without an automated rotation.
**Contradicting evidence**
  - `F14` ✔ Time-consistency check fails for both changes: the only config change in the whole window is cfg-2502 on payment-service (bank.api.timeout_ms 3000->5000) at 2026-04-02T11:20:00Z, i.e. ~52 min before the 12:12Z bucket and ~55 min before the first concrete error at 12:15:27Z. The only deployment (dep-5510, settlement-service v1.9.0) is at 10:30Z, ~102 min before onset. Neither change coincides with the 12:12-12:19Z onset.
  - `F15` ✔ No additional or rollback change appeared after the report either: re-sweeping the full range 2026-04-01T00:00Z-2026-04-02T13:59Z again returns exactly one config change (count 1, cfg-2502) and one deployment (count 1, dep-5510) with rolled_back_at null, so there is no compensating/rollback event to explain or resolve the outage.
**Assumptions**
  - The expired cert is the credential on the payment-service->bank-partner-api edge — supported by the revoked-cert line naming OUR client cert (F20) and the rejection direction (alert 45 to us, F25), but not confirmed by a cert registry (none exists, F26) nor by a renewal/reload event (F23).
  - A still-valid cert being rejected by the partner, or a renewed cert loaded against a stale partner truststore, would produce a similar log signature; no partner-side telemetry exists to rule this out (F22, F28).
  - The failure is confined to payment-service: bank-partner-api is external/shared=false with no other visible callers (F16, F17); unmeasured co-tenant degradation of ledger-db/kafka would widen blast radius but cannot change the origin.
  - Metric onset in the 12:12Z bucket (57.23%) marginally precedes notAfter=12:15:00Z; attributed to bucket-boundary/clock skew, not reconciled.

### H4: External bank/PSP partner-side outage or rejection
The bank-partner-api (external PSP) itself degraded or began rejecting traffic, so payment-service transfers fail. The mTLS/TLS cert-expiry error lines are the partner's response to our calls rather than our own cert having expired, and no internal service is at fault.

**Causal chain**
1. External bank-partner-api begins rejecting/erroring on payment-service calls from ~12:12Z [F2]
2. Outbound transfer calls fail; bank_api_error_rate rises to 100% and success rate falls to ~2.4% [F2, F3]
3. Failures are marked PENDING_RETRY, so the HTTP panel stays green [F5, F1]

**Supporting evidence**
  - `F2` ✔ The bank-facing dependency metric shows a real collapse: payment-service bank_api_error_rate was ~0.2% baseline (11:30-12:05Z), then jumped to 57.23% in the 12:12Z bucket and hit 100% from 12:19Z onward (100% max for every bucket through 13:57Z). Onset is in the 12:12-12:19Z bucket.
  - `F12` ✘ unverified Historical precedents for payment-side failures (leads only, not evidence): HIST-105 (2025-11-19) 'Payment failures: bank partner API outage', category dependency_failure, resolved by waiting for the partner and queued retries; HIST-103 (2025-10-21) external exchange partner outage; HIST-106 (2025-06-30) settlement-service expired mTLS cert; HIST-109 (2025-07-11) auth-service token-validation regression after a library upgrade (bad_deployment). None of these listed a green payment-service error dashboard as a symptom.
  - `F16` ✔ Shared-dependency impact is narrow: bank-partner-api is an external node with shared=false and has_telemetry=false, and its only edge is payment-service -> bank-partner-api. So no other internal service consumes the affected component, and no telemetry exists on the bank/PSP side to confirm or rule out a partner-side cause.
**Contradicting evidence**
  - `F4` ✔ Low-level error signature onset: the earliest NEW error is at 12:15:27Z ('payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'); the first TLS cert-expiry line is at 12:16:44Z naming our client cert 'CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. 33+33+... PENDING_RETRY/TLS failures accumulate; only 1 WARN (a retry) precedes them at 12:04:22Z.
  - `F20` ✔ The REJECTED certificate is explicitly named as OUR client cert: all 5 TLS errors in 12:15-12:20Z read verbatim 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', matching the CN=payments-client.orangeinvest.internal / notAfter=2026-04-02T12:15:00Z given in the task.
  - `F25` ✔ Direction of the cert check: the log phrase is 'remote error: tls: expired certificate (alert 45)' combined with 'our client cert ... notAfter=2026-04-02T12:15:00Z' — i.e. the peer rejected OUR client certificate as expired; the notAfter timestamp equals the reported expiry second and precedes the first rejection by 27s, which pins onset to the client-cert expiry rather than a still-valid-cert/truststore mismatch.
**Assumptions**
  - The TLS cert-expiry log line refers to the partner's presented certificate rather than our client cert; but F20 explicitly names OUR client cert CN=payments-client.orangeinvest.internal with notAfter 12:15:00Z, and F25 establishes the direction as the peer rejecting our cert (alert 45 to us).
  - No internal change is responsible (consistent with F7/F9/F14/F15).
**Rejected because:** Directly contradicted by F20/F25: the rejected certificate is explicitly named as OUR client cert (CN=payments-client.orangeinvest.internal, notAfter=2026-04-02T12:15:00Z) and the TLS alert 45 is received by us, i.e. the peer rejected our expired client cert rather than the partner's own outage causing the signature. No partner-side telemetry (F22/F28) supports a partner-origin fault.

### H2: Bad deployment: settlement-service v1.9.0 CSV exporter regression
The lone deployment in the window (settlement-service v1.9.0, 'Batch settlement report export', new CSV exporter, dep-5510 at 10:30Z) introduced the fault, disturbing settlement/batch processing payment-service depends on or a shared component (ledger-db/kafka), so transfers stopped succeeding while the request/response error panel stayed green.

**Causal chain**
1. settlement-service v1.9.0 deployed 10:30Z with a new CSV exporter [F7]
2. The new exporter disturbs settlement/batch processing *(assumed)*
3. Disturbance propagates to payment-service fund transfers (via shared ledger-db/kafka or the settlement path) *(assumed)*
4. Transfers fail silently as PENDING_RETRY, success rate collapses, HTTP panel stays green [F5, F3, F1]

**Supporting evidence**
  - `F7` ✔ Only ONE deployment across all services in 2026-04-01T12:00Z–2026-04-02T12:32Z: settlement-service v1.9.0 deployed 2026-04-02T10:30:00Z by ci-bot, summary 'Batch settlement report export', changes ['new CSV exporter'], status 'live', rolled_back_at null — i.e. not rolled back. No deployment of payment-service or any of its direct dependencies appears in the window.
**Contradicting evidence**
  - `F14` ✔ Time-consistency check fails for both changes: the only config change in the whole window is cfg-2502 on payment-service (bank.api.timeout_ms 3000->5000) at 2026-04-02T11:20:00Z, i.e. ~52 min before the 12:12Z bucket and ~55 min before the first concrete error at 12:15:27Z. The only deployment (dep-5510, settlement-service v1.9.0) is at 10:30Z, ~102 min before onset. Neither change coincides with the 12:12-12:19Z onset.
  - `F13` ✔ Of the two changes found, only cfg-2502 (payment-service bank.api.timeout_ms 3000→5000, 11:20Z) touches payment-service's dependency path: it alters payment-service's own outbound call timeout toward the downstream bank-partner-api edge. dep-5510 is on settlement-service, which is NOT in payment-service's graph (tc-005/tc-013); any shared-component interaction with ledger-db/kafka is not visible in the returned edges.
  - `F4` ✔ Low-level error signature onset: the earliest NEW error is at 12:15:27Z ('payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'); the first TLS cert-expiry line is at 12:16:44Z naming our client cert 'CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. 33+33+... PENDING_RETRY/TLS failures accumulate; only 1 WARN (a retry) precedes them at 12:04:22Z.
  - `F2` ✔ The bank-facing dependency metric shows a real collapse: payment-service bank_api_error_rate was ~0.2% baseline (11:30-12:05Z), then jumped to 57.23% in the 12:12Z bucket and hit 100% from 12:19Z onward (100% max for every bucket through 13:57Z). Onset is in the 12:12-12:19Z bucket.
  - `F17` ✔ The genuinely shared components in payment-service's graph are ledger-db (postgres, owner platform, shared=true) and kafka (kafka, owner platform, shared=true), but neither is on the failing path — the failure is on the external/mTLS edge to bank-partner-api, and the graph shows no other service attached to bank-partner-api, so degradation is not propagating to co-tenants of ledger-db/kafka via this fault.
  - `F19` ✔ Secondary precedents diverge in cause and do NOT match the current signature: HIST-105 (dependency_failure, bank partner API returned 5xx on the partner side, resolved by waiting + queued retries) and HIST-109 (bad_deployment, JWT clock-skew regression, rolled back). Neither is a client-side cert expiry, so they are leads to rule out rather than support.
**Assumptions**
  - settlement-service shares a component or batch dependency with payment-service (not shown in the payment-service graph, F13).
  - The CSV-export change affects live transfer processing rather than only report exports.
**Rejected because:** Time-inconsistent (deployed 10:30Z, ~102 min before onset, F14) and mechanism-inconsistent: settlement-service is absent from payment-service's graph (F13), and the actual error signature is an mTLS cert rejection naming OUR client cert (F4/F20), which a CSV exporter change cannot produce. No unsupported-hop path survives.

### H3: Config change cfg-2502 (bank.api.timeout_ms 3000->5000) caused the failure
The only payment-service change in the window, cfg-2502 raising bank.api.timeout_ms from 3000 to 5000ms at 11:20Z, altered outbound-call behavior toward bank-partner-api and caused the transfer failures and collapse in bank_api_error_rate/payment_success_rate.

**Causal chain**
1. cfg-2502 changes bank.api.timeout_ms 3000->5000 on payment-service at 11:20Z [F8, F13]
2. Longer timeout shifts outbound calls to a broken/hanging path *(assumed)*
3. bank_api_error_rate jumps and transfers fail silently [F2, F3, F5]

**Supporting evidence**
  - `F8` ✔ Only ONE config change across all services in the window, and it is on payment-service itself: cfg-2502 key 'bank.api.timeout_ms' changed 3000 → 5000 ms at 2026-04-02T11:20:00Z by author p.nair, description 'Raise bank API timeout after partner advisory'. This is ~72 minutes before the incident was reported (12:32Z).
  - `F13` ✔ Of the two changes found, only cfg-2502 (payment-service bank.api.timeout_ms 3000→5000, 11:20Z) touches payment-service's dependency path: it alters payment-service's own outbound call timeout toward the downstream bank-partner-api edge. dep-5510 is on settlement-service, which is NOT in payment-service's graph (tc-005/tc-013); any shared-component interaction with ledger-db/kafka is not visible in the returned edges.
**Contradicting evidence**
  - `F14` ✔ Time-consistency check fails for both changes: the only config change in the whole window is cfg-2502 on payment-service (bank.api.timeout_ms 3000->5000) at 2026-04-02T11:20:00Z, i.e. ~52 min before the 12:12Z bucket and ~55 min before the first concrete error at 12:15:27Z. The only deployment (dep-5510, settlement-service v1.9.0) is at 10:30Z, ~102 min before onset. Neither change coincides with the 12:12-12:19Z onset.
  - `F4` ✔ Low-level error signature onset: the earliest NEW error is at 12:15:27Z ('payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'); the first TLS cert-expiry line is at 12:16:44Z naming our client cert 'CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. 33+33+... PENDING_RETRY/TLS failures accumulate; only 1 WARN (a retry) precedes them at 12:04:22Z.
**Assumptions**
  - A timeout increase could produce 100% failure and mTLS handshake rejections, which no mechanism in the evidence establishes.
**Rejected because:** Time-inconsistent (changed 11:20Z, ~52 min before the 12:12Z bucket and ~55 min before the first concrete error at 12:15:27Z, F14) and mechanism-inconsistent: the observed errors are mTLS handshake rejections of an expired client cert (F4/F21), not timeout-driven failures.

## Facts (verified observations)
- The earliest NEW error is at 12:15:27Z: 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'; the first TLS cert-expiry line follows at 12:16:44Z naming our client cert 'CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z', with 7 PENDING_RETRY and 5 expired-cert lines inside 12:15:00-12:20:00Z. [F4, F21]
- The rejected certificate is explicitly named in the logs as OUR client cert, and the TLS alert 45 direction shows the peer rejected our cert: 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. [F20]
- payment-service bank_api_error_rate went from ~0.2% baseline to 57.23% in the 12:12Z bucket and 100% from 12:19Z onward (all buckets through 13:57Z), while payment_success_rate fell from ~99% to ~2.4-2.7% from 12:19Z — the same 12:12-12:19Z onset as the cert expiry. [F2, F3]
- The HTTP 5xx error_rate panel stayed flat/healthy at ~0.16-0.32% (mean 0.22) across 11:30-13:57Z, so the reported green dashboard is literally true for that metric. [F1]
- No cert reload/renewal event exists: a 'reload' search over 12:15-12:20Z returned 0 matches and a 'renew' search over 12:00-13:59Z returned 0 matches; the expired-cert error was still logging at 13:58:39Z (171 ERROR matches in 12:14-13:59Z, all 'our client cert CN=payments-...'). [F23, F24]
- No line anywhere describes the PARTNER's presented server certificate — no partner cert CN, issuer or notAfter exists in the matched output; the only partner-side information is the received TLS alert 45. [F22]
- Both in-window changes are time-inconsistent with onset: cfg-2502 (payment-service bank.api.timeout_ms 3000->5000, 11:20Z) is ~52 min before the 12:12Z bucket and ~55 min before the first concrete error, and dep-5510 (settlement-service v1.9.0, 10:30Z) is ~102 min before onset. [F8, F7, F14]
- Only one deployment (dep-5510, rolled_back_at null) and one config change (cfg-2502) exist in the entire window, and re-sweeping through 13:59Z returns the same counts with no compensating/rollback event; a payment-service-scoped config sweep for 12:00-13:59Z returns 0. [F9, F15, F27]
- bank-partner-api is an external node with shared=false and has_telemetry=false whose only edge is payment-service -> bank-partner-api, so no other internal service consumes the affected component and no vendor-side telemetry exists; ledger-db and kafka are the shared components but are not on the failing path. [F16, F10]
- No certificate-inventory or lifecycle tool exists in this environment: queries for 'cert-inventory' and 'cert-lifecycle' returned not_found, and the known_services list contains no cert-management component. [F26]
- A prior credential_expiry precedent exists in this environment: HIST-106 (2025-06-30, settlement-service mTLS cert expired) records root_cause 'Client certificate expired; no renewal automation' and resolution 'Issued new cert; added expiry alert'. [F18, F29]
- api-gateway, the upstream caller of payment-service, registered 0 WARN/ERROR logs in 12:00-12:40Z, and payment-service's only upstream edge is api-gateway -> payment-service (http). [F6, F11]

## Assumptions (not verified)
- Assumption: the expired certificate (notAfter=2026-04-02T12:15:00Z) is specifically the credential bound to the payment-service->bank-partner-api edge. This is supported by the log line naming OUR client cert (F20) and the alert-45 rejection direction (F25), but is not confirmed against a cert subject/key binding or any cert registry, because none exists (F26).
- Assumption: a still-valid client cert being rejected by the partner, or a renewed cert loaded against a stale partner truststore, would produce a similar log signature; no partner-side telemetry exists to rule this out (F22, F28). No positive evidence for this alternative was found (F22, F23, F24).
- Assumption: the failure is confined to payment-service. bank-partner-api is external with shared=false and a single internal caller (F16, F17); unmeasured co-tenant degradation of ledger-db/kafka could widen blast radius but cannot change the origin.
- Assumption: the metric onset in the 12:12Z bucket (57.23%) marginally precedes notAfter=12:15:00Z; this is attributed to bucket-boundary/clock skew and was not reconciled to the second.
- Assumption: absence of a cert lifecycle event from the deployment/config feeds means no automated rotation occurred; cert expiry is not represented in those change feeds, so change-correlation alone cannot identify the fault locus.
- (demoted from facts: no verified observation backs it) The mechanism for the blind dashboard is a non-HTTP path: failed transfers are marked PENDING_RETRY (not returned as 5xx) and never surface in the HTTP error-rate metric; 75 ERROR logs vs 1 WARN in payment-service 12:04-12:39Z.

## Timeline
- `2026-04-02T10:30:00Z` Only deployment in the window: settlement-service v1.9.0 ('Batch settlement report export', new CSV exporter, dep-5510, ci-bot) goes live; not rolled back. Later ruled out as time- and mechanism-inconsistent. [F7, F14]
- `2026-04-02T11:20:00Z` Only config change in the window: cfg-2502 on payment-service, bank.api.timeout_ms 3000->5000, by p.nair. ~52 min before onset; ruled out as time- and mechanism-inconsistent. [F8, F13, F14]
- `2026-04-02T12:12:00Z` Onset bucket: bank_api_error_rate jumps to 57.23% and payment_success_rate falls to avg 44.15%, while the HTTP error_rate panel remains flat. [F2, F3, F1]
- `2026-04-02T12:15:00Z` Client certificate CN=payments-client.orangeinvest.internal reaches notAfter (expiry). [F4, F20]
- `2026-04-02T12:15:27Z` First mTLS handshake rejection: 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' — 27 seconds after cert expiry. [F4, F21]
- `2026-04-02T12:16:44Z` First descriptive ERROR naming the cert: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. [F20, F21]
- `2026-04-02T12:19:00Z` bank_api_error_rate reaches 100% and payment_success_rate drops to ~2.4-2.7%, sustained for every bucket through 13:57Z. [F2, F3]
- `2026-04-02T12:32:00Z` Incident reported by support lead (SEV-2): ~40 tickets about failed fund transfers while the payment-service error-rate panel shows nothing unusual. [F1, F3]
- `2026-04-02T13:58:39Z` Expired-cert ERROR still logging (171 matches in 12:14-13:59Z, all 'our client cert CN=payments-...'); no reload or renew event found in the window. [F24, F23]
- `2026-04-02T13:59:00Z` Full-range re-sweep confirms still exactly one deployment and one config change, with no rollback or compensating event. [F9, F15]

## Recommended actions
- **[immediate]** Immediately rotate/renew the payment-service client certificate used on the bank-partner-api edge (CN=payments-client.orangeinvest.internal) — issue a new cert from the same CA/issuer, deploy it into payment-service's mTLS keystore, and reload/restart payment-service so the handshake uses the new material. -- The handshake is rejected as 'expired certificate (alert 45)' naming our client cert with notAfter=2026-04-02T12:15:00Z; every outbound transfer since 12:15:27Z has failed and the error persists through 13:58:39Z with no reload/renew event (F20, F25, F21, F23, F24). This is the direct mitigation. [F20, F25, F21, F23, F24]
- **[immediate]** After reload, verify recovery on payment-service's bank-facing metrics: confirm bank_api_error_rate returns to ~0.2% baseline and payment_success_rate returns to ~99% in the next metric buckets, and confirm the 'expired certificate' ERROR lines stop. -- These two metrics are the diagnostic pair that detected the failure while the HTTP panel stayed green; success_rate at ~2.4% and bank_api_error_rate at 100% are the pre-fix state to reverse (F2, F3). [F2, F3, F24]
- **[immediate]** Triage the PENDING_RETRY backlog: identify transfers marked PENDING_RETRY from 12:15Z onward and replay them once the mTLS handshake succeeds, confirming with settlement that no transfers were double-posted or lost. -- Failed transfers were not returned as 5xx but marked PENDING_RETRY on the settlement/retry path (7 PENDING_RETRY events in 12:15-12:20Z), so customer funds are sitting in a retry queue rather than having failed cleanly (F4, F5). [F4, F5]
- **[short_term]** Add payment_success_rate and bank_api_error_rate to the primary customer-facing payment dashboard, and alert on payment_success_rate dropping below its ~99% baseline — the HTTP error_rate panel must not be the only payment health signal. -- The HTTP 5xx error_rate panel stayed flat at ~0.22% while success_rate collapsed, because failures take a non-HTTP PENDING_RETRY path; the dashboard is structurally blind to this failure mode (F1, F3, F5). [F1, F3, F5]
- **[short_term]** Add an explicit mTLS-handshake-failure / TLS-alert counter and error classifier for the bank-partner-api edge so cert-expiry rejections surface as a monitored signal instead of only as settlement-path ERROR logs. -- The only earlier evidence of the failure was low-level ERROR log lines (75 ERROR vs 1 WARN in 12:04-12:39Z) on a path that never increments the HTTP error metric (F4, F5, F24). [F4, F5, F24]
- **[long_term]** Stand up certificate expiry monitoring and automated renewal for all service-to-service and bank/PSP mTLS client certs (alert at e.g. 30/14/7 days before notAfter), starting with the payment-service -> bank-partner-api edge. -- No cert inventory/lifecycle tooling exists in this environment (no 'cert-inventory' or 'cert-lifecycle' service), no renewal automation ran before the 12:15Z expiry, and the prior HIST-106 incident had the identical root cause 'Client certificate expired; no renewal automation' with remediation 'Issued new cert; added expiry alert' (F26, F23, F27, F29). [F26, F23, F27, F29]
- **[long_term]** Confirm the exact key/subject binding of the expired credential on the payment-service -> bank-partner-api edge and record it in an inventory once one exists; independently confirm with the bank partner that its truststore accepts the renewed cert. -- The cert-to-edge binding currently rests on a log-line parse, and no partner-side telemetry or cert registry exists to confirm it (F20, F22, F26, F28). [F20, F22, F26, F28]

## Data gaps
- bank-partner-api could not be queried directly (tool not_found; external with has_telemetry=false), so the bank's own view of the TLS failure and any partner-side rotation/alert cannot be confirmed — only observed indirectly through payment-service logs and metrics (F10, F16, F28).
- No logs were retrieved for order-service, settlement-service, notification-service, kafka or ledger-db, so a wider blast radius cannot be confirmed or excluded; ledger-db and kafka are shared components not on the failing path (F17).
- The api-gateway log query returned 0 matches in the window; a broader INFO-level query was not run, so 'no errors' could not be distinguished from 'no logs at all' on the client-facing path.
- No certificate inventory, issuance or renewal record store is reachable, so the cert-subject/key binding and the fleet-wide question (single cert vs many expiring certs) cannot be resolved from cert metadata; only the log-line notAfter remains as evidence (F26).
- No log line describes the partner's presented server certificate, and the partner has no telemetry, so the alternative 'still-valid cert rejected / stale partner truststore' cannot be positively excluded — it is only left without positive evidence (F22).
- Cert lifecycle events are not represented in the deployment/config-change feeds, so change-correlation alone cannot pinpoint the fault locus; the only two in-window changes are not time-consistent with onset (F14, F15).
- No historical incident matched the exact symptom 'payment failures with a normal payment-service error dashboard', so this silent-failure pattern is not confirmed as a known precedent.

## What would change this conclusion
- Evidence that the rejected certificate is NOT the one bound to the payment-service -> bank-partner-api edge (e.g. a cert registry or key-binding record showing a different subject/key for that edge), which would invalidate the core linkage of H1.
- A log line or partner-side confirmation naming an expired certificate presented by bank-partner-api (partner server cert CN/issuer/notAfter), which would shift the root cause toward partner-side expiry.
- Evidence that the partner was rejecting a still-valid client cert (e.g. handshake failure persisting after a verified renewal-and-reload, or partner truststore staleness), which would make the renewal action ineffective.
- Discovery of a configuration, feature-flag, WAF or auth-service change at 12:12-12:19Z (outside the deployment/config feeds surveyed) that could produce mTLS handshake rejections, which would reopen the change-correlation hypotheses.
- Evidence that shared ledger-db or kafka co-tenants degraded in the same window in a way that reaches payment-service, which would widen blast radius (though not change the origin or fix of the top hypothesis).
- Recovery of transfers without any cert rotation (or a matching bank-side change that accepts the expired cert), which would falsify cert expiry as the necessary cause.

## Integrity warnings (set by code)
- fact demoted to assumption: The mechanism for the blind dashboard is a non-HTTP path: failed transfers are m

## Investigation stats
- latency 143.82s, LLM calls 23, tokens 164899 (in 137247 / out 27652)
- tool calls 37 (4 failed, 0 partial, 0 recovered by retry), findings 29/30 verified
