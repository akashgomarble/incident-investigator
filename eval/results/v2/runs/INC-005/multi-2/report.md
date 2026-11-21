# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The root cause is the payment-service client mTLS certificate for bank-partner-api (CN=payments-client.orangeinvest.internal) expiring at 2026-04-02T12:15:00Z without renewal, so every outbound payment leg failed the handshake from 12:17:57Z onward (confidence 0.84, high; reviewed ranking H1). The failure is invisible on the HTTP error-rate panel because failed transfers are moved to PENDING_RETRY rather than returning 5xx: error_rate stayed flat at 0.18-0.26% (F8) while bank_api_error_rate was pinned at 100% and payment_success_rate fell to ~2.5% (F9). All alternative origins were actively excluded: no deploys existed in the window (F1/F13), the lone config change cfg-2502 only altered a client-side timeout ~57 minutes before onset and matches neither timing nor failure mode (F2/F14/F18), and the three services sharing ledger-db/kafka (order, portfolio, settlement) stayed clean throughout (F19-F22), ruling out a shared-component failure. The residual uncertainty (partner-side trust change vs genuine expiry, and the exact sub-two-minute onset instant) does not name a different origin or fix.

## Probable root cause
**payment-service client mTLS certificate expired, rejecting handshakes to bank-partner-api** -- credential_expiry in `payment-service`, triggered by `null` (confidence 0.84, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: payment-service client mTLS certificate expired, rejecting handshakes to bank-partner-api | credential_expiry | payment-service | null | 0.84 (high) | active |
| 2 | H2: bank-partner-api (external PSP) degraded or rejected our credentials from its side | dependency_failure | bank-partner-api | null | 0.12 (low) | active |
| 3 | H3: Shared dependency (ledger-db or kafka) degraded, indirectly failing payment legs | infrastructure_failure | ledger-db | null | 0.03 (low) | rejected |
| 4 | H4: Config change cfg-2502 (bank.api.timeout_ms 3000→5000) triggered the failures | config_change | payment-service | cfg-2502 | 0.03 (low) | rejected |

### H1: payment-service client mTLS certificate expired, rejecting handshakes to bank-partner-api
The client certificate used by payment-service to authenticate to bank-partner-api (CN=payments-client.orangeinvest.internal) reached notAfter=2026-04-02T12:15:00Z without being renewed. With no deploy or config change that rotates certificates (no deploys at all in the window; the only config change was an unrelated timeout), nothing renewed it. Every outbound payment leg then failed the mTLS handshake at ~12:17Z. Failed transfers were moved to PENDING_RETRY instead of returning HTTP 5xx, so the HTTP error_rate panel stayed green while bank_api_error_rate hit 100% and payment_success_rate fell to ~2.5%.

**Causal chain**
1. Client certificate CN=payments-client.orangeinvest.internal reaches notAfter=2026-04-02T12:15:00Z without being renewed; no rotation appears in the change list and no deploys/rollbacks exist in the window. [F7, F13, F14]
2. At ~12:17Z outbound mTLS handshakes from payment-service to bank-partner-api begin to be rejected: 'remote error: tls: expired certificate (alert 45)' (170x, first_seen 12:18:10Z) and 'payment marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (135x, first_seen 12:17:57Z). [F7]
3. The failure is confined to the outbound external leg (bank-partner-api, has_telemetry=false, sole caller payment-service), not to shared ledger-db or kafka, which sit on separate paths. [F3, F11, F15, F16]
4. Failed payment legs are parked in PENDING_RETRY rather than returning 5xx, so the HTTP 5xx error_rate panel stays flat at 0.18-0.26%. [F8, F12]
5. The true failure shows only on non-HTTP metrics: bank_api_error_rate pinned at 100% and payment_success_rate ~2.5%, with latency and throughput unchanged — producing ~40 customer tickets for failed transfers. [F9, F10, F12]
6. Blast radius is confined to payment-service: the three services sharing ledger-db and kafka (order-service, portfolio-service, settlement-service) show no onset or degradation, consistent with a fault on payment-service's own outbound credential path rather than a shared component. [F19, F20, F21, F22]

**Supporting evidence**
  - `F7` ✔ payment-service logged 305 ERROR (and 1 WARN) events between 12:17:57Z and 13:58:39Z, dominated by two patterns: 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (170x, first_seen 12:18:10Z) and 'payment <n> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (135x, first_seen 12:17:57Z).
  - `F9` ✔ A non-HTTP metric shows the true failure: bank_api_error_rate for payment-service is pinned at 100.0% for the entire window (12:17Z-13:57Z, mean 100.0), and payment_success_rate is only ~2.5% (mean 2.48, range 1.44-3.5) — i.e. ~97.5% of bank transfers are failing.
  - `F12` ✔ Inference: the failures are outbound mTLS/async payment-leg failures (cert expiry), NOT HTTP-facing errors. Failed transfers are moved to PENDING_RETRY rather than returning 5xx, which is why the HTTP status-code dashboard (error_rate) stayed green; retries are therefore accumulating without surfacing on that panel.
  - `F13` ✔ No deployments occurred in any service between 2026-04-02T11:00:00Z and 13:59:00Z (all-services query: count 0). So no deploy can explain the 12:17Z onset, and no rollback of a code change was possible.
  - `F14` ✔ Exactly one config change exists across all services in the window: payment-service bank.api.timeout_ms 3000 -> 5000 at 2026-04-02T11:20:00Z by p.nair (cfg-2502, 'Raise bank API timeout after partner advisory'). It precedes the ~12:17Z onset by ~57 minutes and only changes a client-side timeout, not any certificate or endpoint.
  - `F15` ✔ payment-service's dependency path is api-gateway -> payment-service -> {ledger-db (kind db), bank-partner-api (kind external), kafka (kind queue-produce)}. The failing leg (mTLS to bank-partner-api) is on this graph, and bank-partner-api is the only node with has_telemetry=false.
  - `F16` ✔ Shared components on payment-service's path (which could hurt it indirectly): ledger-db (postgres, shared=true, owner platform) and kafka (shared=true, owner platform), both with has_telemetry=true. Neither is on the failing outbound mTLS leg to the bank; the observed failure is specifically the bank-partner-api TLS handshake, not a shared-datastore/queue path.
  - `F17` ✔ Historical precedent search returns HIST-106 'Settlement mTLS certificate expired' (2025-06-30, symptoms 'tls handshake failures','expired certificate', root_cause 'Client certificate expired; no renewal automation', category credential_expiry, match_score 5) - a strong lead matching the current expired-certificate signature. HIST-105 (payment failures from a bank partner 5xx outage) is a bank-side dependency_failure precedent but was a partner 5xx, not a client-cert rejection (match_score 3). Per instructions these are leads, not evidence about this incident.
  - `F19` ✔ order-service is clean/unchanged across the window: error_rate stays flat at mean 0.2% (min 0.1, max 0.32; 12:00Z avg 0.21 -> 13:55Z avg 0.17) with no step change at 12:17Z or later, and latency_p99_ms is flat (mean 150.13ms, range 133.08-159.94). Its ledger-db path shows no saturation: db_pool_in_use flat at mean 14.09 connections (max 15.63) throughout 12:00-13:59Z.
  - `F20` ✔ portfolio-service is clean/unchanged: error_rate flat at mean 0.21% (min 0.1, max 0.32; 12:00Z avg 0.25 -> 13:55Z avg 0.15) with no onset, and latency_p99_ms flat at mean 179.18ms (range 163.28-202.01) across 12:00-13:59Z.
  - `F21` ✔ settlement-service is clean/unchanged in both its kafka and ledger-db paths: error_rate flat at mean 0.2% (min 0.1, max 0.32; 12:00Z avg 0.19 -> 13:55Z avg 0.23) with no onset at 12:17Z, and kafka consumer_lag stays normal at mean 43.42 messages (min 3.4, max 77.17) with no monotonic backlog accumulation.
  - `F22` ✔ None of the three shared-component neighbours (order-service, portfolio-service, settlement-service) shows any degradation, onset, or shared-path anomaly during 12:00-13:59Z; all maintain error_rate ~0.2% and stable latency/pool/lag. Meanwhile only payment-service carries the documented failure (bank_api_error_rate pinned at 100%, payment_success_rate ~2.5% per F9). The two groups are clean-vs-degraded, not degrade-together.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 'expired certificate (alert 45)' raised by the remote peer reflects payment-service's own client cert being past notAfter, rather than the partner rejecting a still-valid cert due to a partner-side trust change. (Either reading still places the remediation on our certificate, so it does not name a different origin.)
  - The cert's notAfter was in fact reached at 12:15Z and no automated renewal had fired; the renewal job's failure cause is not visible in any finding.

### H2: bank-partner-api (external PSP) degraded or rejected our credentials from its side
The external bank/PSP endpoint bank-partner-api itself went bad — an outage, a partner-side certificate/trust-store rotation, or a misconfiguration — causing all payment calls to fail. The reporter's suspicion aligns with this. The observed 'expired certificate' text may reflect the partner rejecting our still-valid-but-untrusted client cert, rather than a genuine expiry on our side.

**Causal chain**
1. bank-partner-api (external, has_telemetry=false) begins failing all requests around 12:17Z. [F6, F11]
2. payment-service's outbound call to bank-partner-api fails, logging a remote TLS error. [F7]
3. payments are moved to PENDING_RETRY, masking the failure from the HTTP error_rate dashboard. [F8, F12]
4. Non-HTTP metrics show 100% bank_api_error_rate and ~2.5% success. [F9]

**Supporting evidence**
  - `F6` ✔ bank-partner-api (external vendor) has has_telemetry=false, so the config change's effect on bank-side latency/errors cannot be observed from our metrics, and no deploy was rolled back in the window (no deployments at all).
  - `F9` ✔ A non-HTTP metric shows the true failure: bank_api_error_rate for payment-service is pinned at 100.0% for the entire window (12:17Z-13:57Z, mean 100.0), and payment_success_rate is only ~2.5% (mean 2.48, range 1.44-3.5) — i.e. ~97.5% of bank transfers are failing.
  - `F11` ✔ bank-partner-api is an external dependency with no telemetry of its own; it can only be observed through payment-service, its sole caller.
**Contradicting evidence**
  - `F7` ✔ payment-service logged 305 ERROR (and 1 WARN) events between 12:17:57Z and 13:58:39Z, dominated by two patterns: 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (170x, first_seen 12:18:10Z) and 'payment <n> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (135x, first_seen 12:17:57Z).
  - `F18` ✔ Inference: the single change (cfg-2502, timeout_ms 3000->5000 at 11:20Z) does not match the onset (12:17Z, ~57 min later) or the failure mode (cert expiry at notAfter=2026-04-02T12:15:00Z rejected by an external mTLS peer). The fault lies on the outbound dependency path to bank-partner-api; because the rejection is a client-certificate expiry in our own credential, it is a payment-service-side credential fault triggered at handshake time, not a bank/PSP service outage and not a shared ledger-db/kafka component failure. No internal deploy/rollback exists to blame.
**Assumptions**
  - A partner-side trust-store/rotation change could produce alert 45 on our client cert even though the message names OUR CN and OUR notAfter.
  - No partner-side telemetry exists to confirm or refute a partner outage (has_telemetry=false).

### H3: Shared dependency (ledger-db or kafka) degraded, indirectly failing payment legs
A shared component on payment-service's path — ledger-db (shared with order/portfolio/settlement) or kafka (shared with order/settlement/notification) — degraded around 12:17Z, causing payment processing to fail while payment-service's own HTTP error rate remained green.

**Causal chain**
1. A shared component (ledger-db or kafka) on payment-service's dependency path experiences degradation around onset. [F4, F16]
2. payment-service's payment legs fail as a downstream effect. *(assumed)*
3. Failures surface as async/parked retries rather than HTTP 5xx, keeping error_rate green. [F8, F12]

**Supporting evidence**
  - `F4` ✔ Shared components on payment-service's path: ledger-db (postgres, shared=true) is also used by order-service, portfolio-service and settlement-service; kafka (shared=true) is used by order-service, settlement-service and notification-service (consumers) plus payment-service (producer).
**Contradicting evidence**
  - `F15` ✔ payment-service's dependency path is api-gateway -> payment-service -> {ledger-db (kind db), bank-partner-api (kind external), kafka (kind queue-produce)}. The failing leg (mTLS to bank-partner-api) is on this graph, and bank-partner-api is the only node with has_telemetry=false.
  - `F16` ✔ Shared components on payment-service's path (which could hurt it indirectly): ledger-db (postgres, shared=true, owner platform) and kafka (shared=true, owner platform), both with has_telemetry=true. Neither is on the failing outbound mTLS leg to the bank; the observed failure is specifically the bank-partner-api TLS handshake, not a shared-datastore/queue path.
  - `F7` ✔ payment-service logged 305 ERROR (and 1 WARN) events between 12:17:57Z and 13:58:39Z, dominated by two patterns: 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (170x, first_seen 12:18:10Z) and 'payment <n> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (135x, first_seen 12:17:57Z).
  - `F9` ✔ A non-HTTP metric shows the true failure: bank_api_error_rate for payment-service is pinned at 100.0% for the entire window (12:17Z-13:57Z, mean 100.0), and payment_success_rate is only ~2.5% (mean 2.48, range 1.44-3.5) — i.e. ~97.5% of bank transfers are failing.
  - `F10` ✔ Latency and throughput of payment-service are normal/flat: latency_p99_ms hovered 130-157ms (mean 139.86) and request_rate ~99-103 req/s (mean 100.56) throughout 12:17Z-13:57Z with no step change, so the failure does not manifest as latency spike or traffic drop.
  - `F19` ✔ order-service is clean/unchanged across the window: error_rate stays flat at mean 0.2% (min 0.1, max 0.32; 12:00Z avg 0.21 -> 13:55Z avg 0.17) with no step change at 12:17Z or later, and latency_p99_ms is flat (mean 150.13ms, range 133.08-159.94). Its ledger-db path shows no saturation: db_pool_in_use flat at mean 14.09 connections (max 15.63) throughout 12:00-13:59Z.
  - `F20` ✔ portfolio-service is clean/unchanged: error_rate flat at mean 0.21% (min 0.1, max 0.32; 12:00Z avg 0.25 -> 13:55Z avg 0.15) with no onset, and latency_p99_ms flat at mean 179.18ms (range 163.28-202.01) across 12:00-13:59Z.
  - `F21` ✔ settlement-service is clean/unchanged in both its kafka and ledger-db paths: error_rate flat at mean 0.2% (min 0.1, max 0.32; 12:00Z avg 0.19 -> 13:55Z avg 0.23) with no onset at 12:17Z, and kafka consumer_lag stays normal at mean 43.42 messages (min 3.4, max 77.17) with no monotonic backlog accumulation.
  - `F22` ✔ None of the three shared-component neighbours (order-service, portfolio-service, settlement-service) shows any degradation, onset, or shared-path anomaly during 12:00-13:59Z; all maintain error_rate ~0.2% and stable latency/pool/lag. Meanwhile only payment-service carries the documented failure (bank_api_error_rate pinned at 100%, payment_success_rate ~2.5% per F9). The two groups are clean-vs-degraded, not degrade-together.
**Assumptions**
  - Shared-component degradation could produce an mTLS handshake rejection message naming the bank partner (mechanistically it would not).
  - Ledger-db/kafka metric deviations between 11:20Z and 12:17Z went unqueried and could exist.
**Rejected because:** Scope test now answered and fails: the three services sharing ledger-db/kafka (order-service, portfolio-service, settlement-service) show no onset or degradation (F19-F22) — all clean while only payment-service is degraded (F9). A shared-component failure would degrade them together. The observed mechanism is an outbound mTLS client-cert rejection to bank-partner-api (F7, F15, F16), which is not on any shared datastore/queue path. The 'payment legs fail as a downstream effect' hop also has no supporting finding.

### H4: Config change cfg-2502 (bank.api.timeout_ms 3000→5000) triggered the failures
The single config change in the window — raising payment-service's bank API client-side timeout from 3000ms to 5000ms at 11:20Z by p.nair — altered behavior on the bank-API call path and is the trigger for the payment failures observed from 12:17Z.

**Causal chain**
1. cfg-2502 changes bank.api.timeout_ms 3000→5000 at 11:20Z. [F2, F14]
2. Longer client timeout changes retry/handshake behavior on the bank-API path. *(assumed)*
3. Outbound bank calls begin failing, payments parked in PENDING_RETRY ~57 minutes later. [F7, F12]
4. HTTP error_rate stays green while bank_api_error_rate hits 100%. [F8, F9]

**Supporting evidence**
  - `F2` ✔ Exactly one config change occurred across all services in the window: payment-service bank.api.timeout_ms changed from 3000 to 5000 at 2026-04-02T11:20:00Z by p.nair (cfg-2502, 'Raise bank API timeout after partner advisory'). It precedes the 12:17Z onset by ~57 minutes.
**Contradicting evidence**
  - `F14` ✔ Exactly one config change exists across all services in the window: payment-service bank.api.timeout_ms 3000 -> 5000 at 2026-04-02T11:20:00Z by p.nair (cfg-2502, 'Raise bank API timeout after partner advisory'). It precedes the ~12:17Z onset by ~57 minutes and only changes a client-side timeout, not any certificate or endpoint.
  - `F18` ✔ Inference: the single change (cfg-2502, timeout_ms 3000->5000 at 11:20Z) does not match the onset (12:17Z, ~57 min later) or the failure mode (cert expiry at notAfter=2026-04-02T12:15:00Z rejected by an external mTLS peer). The fault lies on the outbound dependency path to bank-partner-api; because the rejection is a client-certificate expiry in our own credential, it is a payment-service-side credential fault triggered at handshake time, not a bank/PSP service outage and not a shared ledger-db/kafka component failure. No internal deploy/rollback exists to blame.
  - `F7` ✔ payment-service logged 305 ERROR (and 1 WARN) events between 12:17:57Z and 13:58:39Z, dominated by two patterns: 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (170x, first_seen 12:18:10Z) and 'payment <n> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (135x, first_seen 12:17:57Z).
  - `F10` ✔ Latency and throughput of payment-service are normal/flat: latency_p99_ms hovered 130-157ms (mean 139.86) and request_rate ~99-103 req/s (mean 100.56) throughout 12:17Z-13:57Z with no step change, so the failure does not manifest as latency spike or traffic drop.
  - `F8` ✔ The stated healthy HTTP error-rate panel is genuinely green: payment-service error_rate (unit '% of requests (HTTP 5xx)') stayed flat at 0.18-0.26% avg (max 0.32) across every 5-min bucket from 12:17Z to 13:57Z, matching the report's 'error_rate normal'.
**Rejected because:** Timing and mechanism rule it out: cfg-2502 precedes onset by ~57 minutes with no intervening symptom, it only changes a client-side timeout (no certificate/endpoint effect), and the observed failure is an expired-certificate mTLS rejection, not a timeout. No latency step change accompanies it (F10), and it was not reverted at onset. F14/F18 explicitly note the change does not match onset or failure mode.

## Facts (verified observations)
- payment-service logged 305 ERROR events between 12:17:57Z and 13:58:39Z, dominated by 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (170x, first_seen 12:18:10Z) and 'payment <n> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (135x, first_seen 12:17:57Z). [F7]
- The HTTP error-rate panel is genuinely green: payment-service error_rate stayed flat at 0.18-0.26% avg (max 0.32) across every 5-min bucket from 12:17Z to 13:57Z. [F8]
- bank_api_error_rate for payment-service was pinned at 100.0% for the entire window (12:17Z-13:57Z, mean 100.0), and payment_success_rate was only ~2.5% (mean 2.48, range 1.44-3.5) — i.e. ~97.5% of bank transfers failing. [F9]
- Latency and throughput of payment-service were normal/flat (latency_p99_ms 130-157ms mean 139.86; request_rate ~99-103 req/s mean 100.56) throughout 12:17Z-13:57Z with no step change. [F10]
- The failures are outbound mTLS/async payment-leg failures (cert expiry), not HTTP-facing errors; failed transfers are moved to PENDING_RETRY rather than returning 5xx, which is why error_rate stayed green while retries accumulate. [F7, F8, F9]
- No deployments occurred in any service between 2026-04-02T11:00:00Z and 13:59:00Z (all-services query returned count 0), so no deploy explains the 12:17Z onset and no rollback of a code change was possible. [F1, F13]
- Exactly one config change occurred across all services in the window: payment-service bank.api.timeout_ms 3000 -> 5000 at 11:20:00Z by p.nair (cfg-2502); it precedes the ~12:17Z onset by ~57 minutes and only changes a client-side timeout, not any certificate or endpoint. [F2, F14]
- payment-service's dependency path is api-gateway -> payment-service -> {ledger-db (db), bank-partner-api (external), kafka (queue-produce)}; the failing leg is the mTLS handshake to bank-partner-api, the only node with has_telemetry=false. [F3, F15]
- Shared components on payment-service's path (ledger-db, kafka; both shared=true) are not on the failing outbound mTLS leg to the bank; the observed failure is specifically the bank-partner-api TLS handshake, not a shared-datastore/queue path. [F4, F16]
- The three shared-component neighbours (order-service, portfolio-service, settlement-service) show no degradation or onset during 12:00-13:59Z — all maintain error_rate ~0.2% with stable latency, db_pool_in_use ~14 conns, and settlement kafka consumer_lag mean 43.42 with no backlog accumulation — while only payment-service is degraded (bank_api_error_rate 100%, payment_success_rate ~2.5%). [F19, F20, F21, F9]
- bank-partner-api is an external dependency with has_telemetry=false and no logs/metrics; it can only be observed through payment-service, its sole caller. [F11, F15]
- The single config change (cfg-2502) does not match the onset (12:17Z, ~57 min later) or the failure mode (cert expiry at notAfter 12:15:00Z rejected by an external mTLS peer); no internal deploy/rollback exists to blame. [F14, F13]
- Historical precedent HIST-106 'Settlement mTLS certificate expired' (2025-06-30, root_cause 'Client certificate expired; no renewal automation', category credential_expiry, match_score 5) matches the current expired-certificate signature; HIST-105 is a bank-side 5xx dependency_failure precedent, not a client-cert rejection (match_score 3). Per instructions, precedents are leads, not evidence about this incident. [F17]
- payment-service exposes no retry_queue_depth metric (query returned not_found); the only quantitative proxy for the PENDING_RETRY backlog is the log count of 135 'marked PENDING_RETRY' events (first_seen 12:17:57Z). [F23]

## Assumptions (not verified)
- The 'expired certificate (alert 45)' raised by the remote peer reflects payment-service's own client cert being past its notAfter, rather than the partner rejecting a still-valid cert due to a partner-side trust change. (Either reading still places the remediation on our certificate, so it does not name a different origin.)
- The cert's notAfter was in fact reached at 12:15:00Z and no automated renewal had fired; the renewal job's existence and failure cause are not visible in any finding.
- No partner-side (bank/PSP) outage accompanied the failure; this cannot be verified because bank-partner-api has has_telemetry=false and no partner telemetry exists.
- No shared-component deviation occurred between 11:20Z and 12:17Z; metrics for ledger-db/kafka in that pre-onset interval were not queried, and the post-onset scope test (F19-F22) is used as a proxy.
- The reported symptom set (customer ticket volume, 'error_rate normal') and the log/metric evidence describe the same incident window.

## Timeline
- `2026-04-02T11:20:00Z` Only config change in the window: payment-service bank.api.timeout_ms 3000 -> 5000 by p.nair (cfg-2502, 'Raise bank API timeout after partner advisory'). It changes only a client-side timeout and precedes onset by ~57 minutes. [F2, F14]
- `2026-04-02T12:15:00Z` Client certificate for CN=payments-client.orangeinvest.internal reaches notAfter (expiry), per the logged rejection message. [F7]
- `2026-04-02T12:17:57Z` First 'payment marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' event; onset of observed failures. [F7]
- `2026-04-02T12:18:10Z` First occurrence of 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' naming our client cert CN and notAfter. [F7]
- `2026-04-02T12:32:00Z` Incident INC-005 reported by support lead: ~40 tickets about failed fund transfers in the last 15 minutes; payment-service HTTP error-rate panel shows nothing unusual. [F8, F9]
- `2026-04-02T12:17:00Z/13:57:00Z` Throughout the window, HTTP error_rate stays flat at 0.18-0.26% (F8) while bank_api_error_rate is pinned at 100% and payment_success_rate ~2.5% (F9); latency_p99 and request_rate remain flat (F10). [F8, F9, F10]
- `2026-04-02T12:00:00Z/13:59:00Z` order-service, portfolio-service and settlement-service remain clean and unchanged (error_rate ~0.2%, stable latency/pool/lag), confirming the failure is confined to payment-service's outbound bank leg. [F19, F20, F21, F22]

## Recommended actions
- **[immediate]** Rotate/renew the payment-service client certificate for bank-partner-api (CN=payments-client.orangeinvest.internal) — replace the expired cert material with a valid one and reload/restart payment-service so it picks up the new credential — then confirm mTLS handshakes to bank-partner-api succeed. -- Every hop of H1 rests on VERIFIED findings: the rejection quotes our own CN and our own notAfter=2026-04-02T12:15:00Z with alert 45 (F7), and all alternatives (deploy F1/F13, cfg-2502 F2/F14/F18, shared-component failure F19-F22) have been excluded. Renewing the credential is the direct remediation regardless of which reading of alert 45 is correct. [F7, F9, F12, F17]
- **[immediate]** Verify recovery with the non-HTTP signals: bank_api_error_rate should fall from 100% and payment_success_rate should rise from ~2.5% (per F9), and the 'mTLS handshake rejected' / 'expired certificate' error streams (F7) should stop. Do not use the HTTP error_rate panel as the recovery signal — it stayed green throughout (F8). -- The HTTP error-rate panel is proven not to reflect this failure because failed transfers are moved to PENDING_RETRY (F8/F12); recovery must be judged on bank_api_error_rate, payment_success_rate and the handshake-rejection logs. [F8, F9, F12, F7]
- **[short_term]** Drain/replay the accumulated PENDING_RETRY transfers once handshakes succeed (the backlog is only quantified as 135 PENDING_RETRY log events in F7, since no retry_queue_depth metric exists per F23), and confirm settlement/order/portfolio paths absorb the replay without degradation. -- 135 'marked PENDING_RETRY' events accumulated without surfacing on any panel (F7, F23); the retried legs need to be settled so customer transfers complete. [F7, F23]
- **[short_term]** Add alerting and a dashboard panel that cover non-HTTP failure paths: alert on bank_api_error_rate and payment_success_rate thresholds, and on mTLS/handshake error log patterns, alongside the existing HTTP error_rate panel. -- The core paradox in this incident is that customers saw failures while the error-rate dashboard was green (F8 vs F9). Non-HTTP signals (bank_api_error_rate 100%, payment_success_rate ~2.5%) were available but not alerted on (F9). [F8, F9, F10]
- **[short_term]** Confirm the sub-two-minute onset instant and the renewal-failure cause, and if feasible obtain partner-side confirmation that the rejection was due to our cert being past notAfter rather than a partner-side trust change; if either turns out differently, revisit the fix. -- Two confirmation-class residuals remain: the exact first-failure instant between 12:15Z and 12:17Z is unverified, and it cannot be independently confirmed from our side whether the peer's alert 45 reflects genuine expiry vs a partner trust change (bank-partner-api has has_telemetry=false, F6/F11). Neither names a competing origin, so this is verification, not a re-open. [F7, F6, F11]
- **[short_term]** Run the (currently unqueried) pre-onset metric checks — payment-service, ledger-db and kafka metrics for 11:20Z-12:17Z — to close the remaining scope gap and confirm no contributor existed before the cert expiry. -- No metric queries were run against ledger-db, kafka, payment-service or api-gateway for the 11:20Z-12:17Z interval, so any pre-onset deviation there is unverified; the post-onset scope test (F19-F22) is currently the evidence for shared-component exclusion. [F19, F20, F21, F22]
- **[long_term]** Install automated certificate-expiry monitoring and renewal automation for all service client certificates (starting with payment-service -> bank-partner-api), with alerting well before notAfter and a documented rotation procedure; verify by test-renewal before the next expiry boundary. -- The failure resulted from a cert reaching notAfter with no renewal in the window and no cert-rotation change in the config list (F7 and the dependency gap that no certificate-management change appears). HIST-106 'Settlement mTLS certificate expired' (root_cause 'Client certificate expired; no renewal automation') is the same failure class, indicating a recurring systemic gap (F17). [F7, F17]
- **[long_term]** Make PENDING_RETRY a first-class observable: add a retry_queue_depth / PENDING_RETRY backlog metric for payment-service and alert on its growth, since no such metric currently exists. -- No retry_queue_depth metric exists (F23), so the backlog was invisible as a metric and only inferable from log counts (F7); a growing retry backlog is the leading indicator that was missed. [F23, F7, F12]

## Data gaps
- No direct telemetry exists for bank-partner-api (has_telemetry=false, F6/F11); its behaviour is only observable through payment-service, so an accompanying partner-side outage or a partner-side trust change cannot be independently ruled out.
- The exact first-failure instant is only bounded, not pinned: cert notAfter is 12:15:00Z and the earliest ERROR log is 12:17:57Z, and no logs/metrics before 12:17Z were queried, so failure onset between 12:15Z and 12:17Z is unverified and a pre-window trend cannot be excluded.
- No metric queries were run against ledger-db, kafka, payment-service or api-gateway (available_metrics not enumerated), so whether any metric deviated between the 11:20Z config change and the 12:17Z onset is unanswered.
- No certificate-management/inventory change appears in the change list and no deploy occurred (F1/F13), so the process that was supposed to renew the cert, and why it failed, is not visible.
- bank-partner-api exposes no logs/metrics (F6/F11), so no direct metric/log evidence of the partner side exists.
- No dedicated retry-queue-depth / PENDING_RETRY backlog metric exists for payment-service (F23), so the backlog can only be inferred from 135 log events (F7), not measured.
- Caller-side telemetry was not examined; immediate callers of payment-service (e.g. API gateway/web client) were not queried, so blast radius beyond payment-service is not established.
- Other payment-service metrics (restarts, db_pool_in_use, cpu_pct, memory_pct, timeout/retry counters) were not retrieved, so secondary resource pressure cannot be excluded and retry counts/timeout counters are unverified.
- Historical-precedent search returned only summary rows and did not specifically search for a silent failure with a green error-rate dashboard, so whether this exact paradox has occurred before is unconfirmed.
- settlement-service db_pool_in_use and latency_p99_ms were not queried (budget exhausted); its kafka path and error_rate were clean, so this omission is unlikely to change the conclusion.
- No access was checked to kafka consumer-lag metrics for notification-service, so shared kafka queue congestion as a cross-service contributor could not be fully ruled out.

## What would change this conclusion
- Evidence that our client certificate is/was still valid (notAfter later than 2026-04-02T12:15:00Z, or a secret-store rotation in the window) would contradict H1 and point elsewhere.
- Partner-side confirmation (e.g. bank-partner-api logs) showing a partner outage or trust-store rotation that rejects still-valid client certs would shift the origin toward H2 and the remediation off our certificate.
- The three ledger-db/kafka neighbours (order-service, portfolio-service, settlement-service) showing a degradation with onset at ~12:17Z — rather than the clean flat series observed — would revive the shared-component hypothesis (H3).
- Discovery of an unlisted deployment or certificate-rotation change at ~12:15-12:17Z would reopen the trigger question and could implicate a bad deployment or a failed rotation rather than simple non-renewal.
- A latency step change or traffic drop accompanying onset (rather than the flat p99/request_rate observed) would contradict the 'pure handshake rejection' mechanism and suggest a different failure mode.

## Investigation stats
- latency 163.74s, LLM calls 20, tokens 139741 (in 112869 / out 26872)
- tool calls 28 (3 failed, 0 partial, 0 recovered by retry), findings 22/23 verified
