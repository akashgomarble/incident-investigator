# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The top hypothesis H1 (confidence 0.60, medium) is adequately supported and clearly ahead of the rejected alternatives: payment-service's client mTLS certificate CN=payments-client.orangeinvest.internal expired at 2026-04-02T12:15:00Z, and from 12:15:27Z every bank-partner-api call is rejected at the TLS handshake with 'tls: expired certificate (alert 45)', so payments are accepted (HTTP 2xx, flat error_rate) but marked PENDING_RETRY and never complete. What the customers see as "payments fail while the dashboard is green" is explained by the dashboard tracking only HTTP 5xx while the failure is a non-HTTP egress-handshake failure: bank_api_error_rate hit 100% and payment_success_rate fell to ~2.5% at the same 12:15Z bucket, and p99 latency dropped (fail-fast), so no HTTP-layer panel showed anything. The bank-side and change-window theories (H2/H3/H4) are contradicted by the to-the-second alignment with our own client cert's notAfter and by 55–105 minutes of quiet between the only deploy/config changes and onset. Residual uncertainty is moderate rather than low, because the partner side has no telemetry and no certificate/DNS change feed was queryable, so an independent co-occurring bank-side outage cannot be formally excluded.

## Probable root cause
**Expired client mTLS certificate on payment-service breaks bank-partner-api calls (silent, non-HTTP failure path)** -- credential_expiry in `payment-service (client certificate / mTLS egress credential)`, triggered by `null` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired client mTLS certificate on payment-service breaks bank-partner-api calls (silent, non-HTTP failure path) | credential_expiry | payment-service (client certificate / mTLS egress credential) | null | 0.60 (medium) | active |
| 2 | H2: External bank-partner-api outage / partner-side certificate change causes the failures (reporter's 'bank side' theory) | dependency_failure | bank-partner-api (external partner) | null | 0.08 (low) | rejected |
| 3 | H3: Config change cfg-2502 (bank.api.timeout_ms 3000→5000) masks/hides bank errors and delays settlement | config_change | payment-service (configuration bank.api.timeout_ms) | cfg-2502 | 0.03 (low) | rejected |
| 4 | H4: settlement-service deploy dep-5510 (new CSV exporter) exfiltrates resources / breaks shared kafka+ledger-db path used by payment settlement | bad_deployment | settlement-service | dep-5510 | 0.02 (low) | rejected |

### H1: Expired client mTLS certificate on payment-service breaks bank-partner-api calls (silent, non-HTTP failure path)
The client certificate CN=payments-client.orangeinvest.internal used by payment-service to call bank-partner-api expired at 2026-04-02T12:15:00Z. From 12:15:27Z every bank call is rejected at the TLS handshake, so payments are accepted (HTTP 2xx, hence flat payment-service error_rate) but marked PENDING_RETRY and never complete. This is a credential_expiry fault on payment-service's own egress credential, not a change in the deployment/config window.

**Causal chain**
1. payment-service holds a client certificate CN=payments-client.orangeinvest.internal with notAfter=2026-04-02T12:15:00Z; a cert-watcher warns at 11:50:26Z that it expires in 25 minutes. [F16]
2. At 12:15:00Z the certificate expires; the first mTLS handshake rejection to bank-partner-api occurs at 12:15:27Z, matching expiry to the second. [F15, F16]
3. bank-partner-api rejects the handshake; payment-service logs 'remote error: tls: expired certificate (alert 45)' (171x) and 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140x). [F15]
4. Because the failure is thrown on the bank-call path before/outside the HTTP request handling, payment-service still returns 2xx to api-gateway: HTTP error_rate stays flat at 0.1–0.32% throughout (arithmetically real green panel). [F17, F18]
5. The correct signal does move: bank_api_error_rate steps to 100.0% at the 12:15:00Z bucket and payment_success_rate collapses from ~99.3% to 2.51% and stays ~2-3%. [F18]
6. Requests are still accepted at ~100 req/s so this is not a surge; latency_p99 falls (323ms→~140ms) because calls fail fast on handshake rejection rather than hanging. [F19, F20]
7. Result: customers see failed fund transfers while the payment-service HTTP error-rate dashboard stays green. [F17, F18]

**Supporting evidence**
  - `F15` ✔ payment-service logged 311 ERROR events in 11:45–13:59Z; the dominant signatures are 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' (171x, first_seen 12:16:44Z) and 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140x, first_seen 12:15:27Z).
  - `F16` ✔ A cert-watcher WARN at 11:50:26Z predicted the exact failure onset: 'client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)'; the first mTLS rejection followed at 12:15:27Z, matching expiry to the second.
  - `F17` ✔ payment-service raw HTTP error_rate stayed flat across the whole window (min 0.1 / max 0.32%, unit '% of requests (HTTP 5xx)'), including every bucket from 12:15Z onward — the reported green dashboard is arithmetically real for that metric.
  - `F18` ✔ The same service's OTHER paths are not green: bank_api_error_rate steps from ~0.2% to exactly 100.0 at the 12:15:00Z bucket and stays at 100.0 through 13:57Z, while payment_success_rate drops from ~99.3% (12:09Z) to 2.51% at 12:15Z and remains ~2-3% thereafter.
  - `F19` ✔ Request volume to payment-service did not change at onset: request_rate is ~100 req/s throughout (12:15Z avg 101.27, 13:57Z avg 99.55), so the failures are not a traffic surge — inbound requests keep being accepted while downstream settlement fails.
  - `F20` ✔ payment-service latency_p99 DROPS rather than rising at onset (323.5ms at 12:00Z → 140.0ms avg at 12:18Z bucket, staying ~135-145ms), consistent with failing fast on handshake rejection instead of hanging on the bank call — so the panel shows no latency anomaly either.
  - `F4` ✔ payment-service's dependency edges are: payment-service → ledger-db (db), payment-service → bank-partner-api (external), payment-service → kafka (queue-produce); api-gateway → payment-service (http). ledger-db and kafka are marked shared=true; bank-partner-api is external and has_telemetry=false.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The cert-watcher entry and the 311 log events are the true onset and not a coincidental second fault
  - No independent bank-side outage also exists (bank-partner-api has no telemetry: F4/F11, tc-022 failed)

### H2: External bank-partner-api outage / partner-side certificate change causes the failures (reporter's 'bank side' theory)
The fund-transfer failures originate outside our stack: the bank/acquirer side is failing or its server-side TLS configuration became incompatible. The green payment-service error-rate panel is correct because payment-service itself is healthy, but the failures surface via the bank call path.

**Causal chain**
1. payment-service calls bank-partner-api (external, has_telemetry=false) to complete transfers. [F4, F11]
2. The partner degrades (5xx or TLS incompatibility), so transfers fail even though payment-service returns 2xx on the request path. *(assumed)*
3. Failure surfaces only on the bank_api_error_rate path, not the HTTP error_rate panel. [F17, F18]
4. Historical precedent HIST-105 (2025-11-19) is the same symptom pair from a bank-partner API outage. [F6]

**Supporting evidence**
  - `F6` ✔ Historical precedent HIST-105 (2025-11-19) is titled 'Payment failures: bank partner API outage' with the same symptom pair ('payment failures', 'bank api errors'), root cause 'Bank partner API returned 5xx for 40 minutes (their incident)'; this is a lead only, not evidence about INC-005.
  - `F18` ✔ The same service's OTHER paths are not green: bank_api_error_rate steps from ~0.2% to exactly 100.0 at the 12:15:00Z bucket and stays at 100.0 through 13:57Z, while payment_success_rate drops from ~99.3% (12:09Z) to 2.51% at 12:15Z and remains ~2-3% thereafter.
**Contradicting evidence**
  - `F15` ✔ payment-service logged 311 ERROR events in 11:45–13:59Z; the dominant signatures are 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' (171x, first_seen 12:16:44Z) and 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140x, first_seen 12:15:27Z).
  - `F16` ✔ A cert-watcher WARN at 11:50:26Z predicted the exact failure onset: 'client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)'; the first mTLS rejection followed at 12:15:27Z, matching expiry to the second.
**Assumptions**
  - The client-side 'tls: expired certificate (alert 45)' could be produced by a server-side certificate problem rather than our own client cert expiry
  - The cert-watcher WARN and the to-the-second alignment with first rejection could be coincidence
**Rejected because:** F15/F16 show the error is a client-credential expiry: the cert-watcher states CN=payments-client.orangeinvest.internal (our client cert) expires at 12:15:00Z and the first mTLS rejection lands at 12:15:27Z. A partner-side outage would not be predicted to the minute by our own certificate's notAfter, nor produce 'alert 45 expired certificate' naming our client CN. HIST-105 is only a lead (F6) and its symptom pair differs from the observed TLS alert.

### H3: Config change cfg-2502 (bank.api.timeout_ms 3000→5000) masks/hides bank errors and delays settlement
The 11:20Z config change raising bank.api.timeout_ms from 3000 to 5000 on payment-service altered bank-call behaviour on the payment path, causing transfers to hang/delay and failures to be hidden from the HTTP error panel.

**Causal chain**
1. cfg-2502 changed bank.api.timeout_ms 3000→5000 at 11:20Z on payment-service. [F2, F10]
2. Longer timeout keeps bank calls open longer, delaying settlement and shifting failures off the counted surface. [F14]
3. Result: transfers fail without payment-service HTTP error_rate moving. [F17]

**Supporting evidence**
  - `F2` ✔ Exactly one config change occurred: cfg-2502 on payment-service, key bank.api.timeout_ms, changed 3000 → 5000 at 2026-04-02T11:20:00Z by p.nair, description 'Raise bank API timeout after partner advisory'.
  - `F14` ✔ Inference: the settlement-service deployment dep-5510 (10:30Z) and the shared kafka/ledger-db dependencies are the change candidates that could affect settlement while leaving payment-service's own HTTP error rate green; the 11:20Z timeout raise (3s->5s) would delay, not surface, bank-side errors on payment-service's panel.
**Contradicting evidence**
  - `F3` ✔ Both changes precede the 12:32Z report and the ~12:32 symptom window by over an hour (deploy 10:30, config 11:20); neither was rolled back.
  - `F15` ✔ payment-service logged 311 ERROR events in 11:45–13:59Z; the dominant signatures are 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' (171x, first_seen 12:16:44Z) and 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140x, first_seen 12:15:27Z).
  - `F16` ✔ A cert-watcher WARN at 11:50:26Z predicted the exact failure onset: 'client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)'; the first mTLS rejection followed at 12:15:27Z, matching expiry to the second.
  - `F20` ✔ payment-service latency_p99 DROPS rather than rising at onset (323.5ms at 12:00Z → 140.0ms avg at 12:18Z bucket, staying ~135-145ms), consistent with failing fast on handshake rejection instead of hanging on the bank call — so the panel shows no latency anomaly either.
**Assumptions**
  - A timeout change alone can produce the observed TLS handshake rejections
**Rejected because:** A timeout raise would delay calls, not produce 'tls: expired certificate (alert 45)' handshake rejections (F15). Timing also fails: the change was at 11:20Z, ~55 minutes of quiet before the 12:15:27Z onset (F3); and observed latency fell rather than rose (F20, 323ms→~140ms), opposite to what a longer timeout would create.

### H4: settlement-service deploy dep-5510 (new CSV exporter) exfiltrates resources / breaks shared kafka+ledger-db path used by payment settlement
The 10:30Z settlement-service v1.9.0 deploy (new CSV exporter) over-consumes the shared kafka/ledger-db resources it shares with payment-service, causing settlement of payments to fail while payment-service's own HTTP error rate stays green.

**Causal chain**
1. dep-5510 deployed settlement-service v1.9.0 (batch settlement CSV exporter) at 10:30Z, live and not rolled back. [F1, F9]
2. settlement-service shares kafka and ledger-db with payment-service, so a settlement-side fault could reach payment's settlement path. [F12, F11]
3. The exporter saturates a shared component, so payments are accepted but settlement never completes while payment-service HTTP responses remain 2xx. *(assumed)*
4. Result: customer-visible transfer failures with a green payment-service error panel. *(assumed)*

**Supporting evidence**
  - `F1` ✔ Across all services, exactly one deployment occurred in the 2026-04-01→2026-04-02T13:59 window: dep-5510, settlement-service v1.9.0 at 2026-04-02T10:30:00Z (author ci-bot, 'new CSV exporter'), status live, not rolled back.
  - `F12` ✔ Shared-component sharers: kafka is produced by order-service and payment-service, and consumed by settlement-service and notification-service; ledger-db is used by order-service, portfolio-service, payment-service and settlement-service. So settlement-service and payment-service both touch kafka and ledger-db.
  - `F14` ✔ Inference: the settlement-service deployment dep-5510 (10:30Z) and the shared kafka/ledger-db dependencies are the change candidates that could affect settlement while leaving payment-service's own HTTP error rate green; the 11:20Z timeout raise (3s->5s) would delay, not surface, bank-side errors on payment-service's panel.
**Contradicting evidence**
  - `F15` ✔ payment-service logged 311 ERROR events in 11:45–13:59Z; the dominant signatures are 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' (171x, first_seen 12:16:44Z) and 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140x, first_seen 12:15:27Z).
  - `F16` ✔ A cert-watcher WARN at 11:50:26Z predicted the exact failure onset: 'client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)'; the first mTLS rejection followed at 12:15:27Z, matching expiry to the second.
  - `F18` ✔ The same service's OTHER paths are not green: bank_api_error_rate steps from ~0.2% to exactly 100.0 at the 12:15:00Z bucket and stays at 100.0 through 13:57Z, while payment_success_rate drops from ~99.3% (12:09Z) to 2.51% at 12:15Z and remains ~2-3% thereafter.
  - `F19` ✔ Request volume to payment-service did not change at onset: request_rate is ~100 req/s throughout (12:15Z avg 101.27, 13:57Z avg 99.55), so the failures are not a traffic surge — inbound requests keep being accepted while downstream settlement fails.
  - `F3` ✔ Both changes precede the 12:32Z report and the ~12:32 symptom window by over an hour (deploy 10:30, config 11:20); neither was rolled back.
**Assumptions**
  - The CSV exporter can exhaust or lock the shared kafka/ledger-db path badly enough to fail payments
  - The 1h45m quiet gap between deploy and onset is a plausible incubation delay
**Rejected because:** No evidence of shared-component saturation was ever pulled (gap: kafka/ledger-db metrics unqueried). The observed failure mode is not a resource/datastore error but an mTLS handshake rejection to bank-partner-api (F15), and the onset is predicted to the second by an unrelated certificate's notAfter (F16) with no connection to dep-5510. A 1h45m quiet gap after the deploy also weighs against it (F3).

## Facts (verified observations)
- payment-service's egress credential to bank-partner-api is an mTLS client certificate CN=payments-client.orangeinvest.internal; a cert-watcher WARN at 11:50:26Z predicted expiry in 25 minutes with notAfter=2026-04-02T12:15:00Z. [F16]
- The first mTLS rejection followed at 2026-04-02T12:15:27Z, matching the predicted certificate expiry to the second; the dominant log signatures are 'tls: expired certificate (alert 45)' (171x) and 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140x), 311 ERROR events in 11:45–13:59Z. [F15, F16]
- payment-service raw HTTP error_rate stayed flat across the entire window (min 0.1 / max 0.32% of HTTP 5xx), including every bucket from 12:15Z onward, so the green dashboard is arithmetically real for that metric. [F17]
- Other paths for the same service are not green: bank_api_error_rate steps from ~0.2% to exactly 100.0 at the 12:15:00Z bucket and stays at 100.0 through 13:57Z, while payment_success_rate drops from ~99.3% (12:09Z) to 2.51% at 12:15Z and remains ~2–3%. [F18]
- Request volume to payment-service did not change at onset (request_rate ~100 req/s throughout; 12:15Z avg 101.27, 13:57Z avg 99.55), so the failures are not a traffic surge; inbound requests keep being accepted while the downstream bank call fails. [F19]
- payment-service's dependency edges are payment-service → ledger-db (db, shared=true), → bank-partner-api (external, has_telemetry=false), → kafka (queue-produce, shared=true), with api-gateway → payment-service; the partner has no telemetry and cannot be observed independently. [F4, F11]
- Exactly one deployment occurred in the 2026-04-01→2026-04-02T13:59 window (and in the widened 08:00–14:00 search): dep-5510, settlement-service v1.9.0 at 2026-04-02T10:30:00Z, 'new CSV exporter', status live, not rolled back. [F1, F9]
- Exactly one config change occurred (and in the widened search): cfg-2502 on payment-service, bank.api.timeout_ms 3000 → 5000 at 2026-04-02T11:20:00Z by p.nair, description 'Raise bank API timeout after partner advisory'. [F2, F10]
- Both changes precede the 12:32Z report and the 12:15Z onset by over 50 minutes (deploy 10:30Z, config 11:20Z, onset 12:15:27Z) and neither was rolled back; no deployments or config/feature-flag changes occurred in 11:45Z–13:30Z (count=0). [F3, F7, F8]
- Historical precedent HIST-106 (2025-06-30) 'Settlement mTLS certificate expired' on settlement-service had symptoms 'tls handshake failures / expired certificate' and was resolved by issuing a new cert and adding an expiry alert; HIST-105 is a same-symptom-pair lead only and is not evidence about INC-005. [F13]
- kafka is produced by order-service and payment-service and consumed by settlement-service and notification-service; ledger-db is used by order-service, portfolio-service, payment-service and settlement-service, so settlement-service and payment-service share both kafka and ledger-db. [F12]
- payment-service's only upstream caller is api-gateway; no client-app/mobile component appears anywhere in payment-service's dependency graph, and api-gateway logs for 12:00–13:59Z matched zero entries. [F5]

## Assumptions (not verified)
- Assumption: the cert-watcher entry and the 311 log events mark the true onset of this incident, i.e. there is no coincidental second fault starting at the same time.
- Assumption: no independent bank-side outage or partner-side TLS change also occurred; bank-partner-api has no telemetry (F4/F11) and the tc-022 log query for it failed with not_found, so the partner side cannot be observed directly.
- Assumption: the flat HTTP error_rate (F17) is defined as % of HTTP 5xx responses, so a non-HTTP egress handshake failure would legitimately not move it.
- Assumption: the expired client certificate was not renewed/rotated at or shortly before 12:15Z by any mechanism not covered by the deployment and config-change data (no certificate/DNS change API was available to query).
- Assumption: PENDING_RETRY payments will drain after the credential is renewed rather than being permanently lost; no queue-depth/retry-count metric was inspected.
- (demoted from facts: no verified observation backs it) payment-service latency_p99 DROPS at onset (323.5ms at 12:00Z → 140.0ms avg at the 12:18Z bucket, staying ~135–145ms), consistent with failing fast on handshake rejection rather than hanging on the bank call, so no latency anomaly appears on the panel.

## Timeline
- `2026-04-02T10:30:00Z` Only deployment in the window: dep-5510, settlement-service v1.9.0 ('new CSV exporter'), live, not rolled back. No deploy or config change occurs afterward until onset. [F1, F9, F7]
- `2026-04-02T11:20:00Z` Only config change in the window: cfg-2502 on payment-service, bank.api.timeout_ms 3000 → 5000 (p.nair, 'Raise bank API timeout after partner advisory'). No config/feature-flag change occurs afterward until onset. [F2, F10, F8]
- `2026-04-02T11:50:26Z` cert-watcher WARN: client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z). [F16]
- `2026-04-02T12:15:00Z` Certificate notAfter passes; bank_api_error_rate steps from ~0.2% to exactly 100.0 and payment_success_rate falls from ~99.3% to 2.51%, both remaining anomalous through 13:57Z, while payment-service HTTP error_rate stays flat (0.1–0.32%). [F16, F18, F17]
- `2026-04-02T12:15:27Z` First mTLS rejection to bank-partner-api; payments marked PENDING_RETRY. Latency_p99 drops from 323.5ms to ~140ms (fail-fast), with request_rate unchanged at ~100 req/s. [F15, F16, F20, F19]
- `2026-04-02T12:16:44Z` First 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' log entry (171x by 13:59Z; PENDING_RETRY signature 140x). [F15]
- `2026-04-02T12:32:00Z` Support lead reports INC-005 (SEV-2): ~40 tickets about failed fund transfers in the last 15 minutes; payment-service HTTP error-rate panel shows nothing unusual. [F3]
- `2026-04-02T13:57:00Z` Latest data: payment_success_rate still ~2–3%, bank_api_error_rate still 100%, no deploy or config change in 11:45Z–13:30Z, api-gateway logs for the period show zero matches, bank-partner-api log query failed (not_found). [F18, F7, F8, F5]

## Recommended actions
- **[immediate]** Renew and deploy the payment-service egress client certificate for CN=payments-client.orangeinvest.internal (mTLS credential used for calls to bank-partner-api), then confirm the new notAfter and reload/restart payment-service so the handshake picks up the new cert. -- The first mTLS rejection at 12:15:27Z matches the cert's notAfter of 12:15:00Z to the second, and 171 'tls: expired certificate (alert 45)' plus 140 'PENDING_RETRY' events trace to this credential; this is the direct mitigation and the HIST-106 precedent resolution ('Issued new cert'). [F15, F16, F13]
- **[immediate]** Alert the on-call engineer and incident commander, and add a temporary panel/alert on bank_api_error_rate and payment_success_rate (not just HTTP 5xx error_rate) so the ongoing failure is visible while the renewal is applied. -- bank_api_error_rate is at 100% and payment_success_rate at ~2.5% while the reported dashboard (HTTP 5xx error_rate) is flat, which is why the incident was reported by customers rather than by monitoring. [F17, F18, F15]
- **[short_term]** After renewal, verify recovery: bank_api_error_rate returns to baseline, payment_success_rate returns to ~99%, 'tls: expired certificate' and PENDING_RETRY log signatures stop, and drain/replay the PENDING_RETRY payments (check retry/queue depth and confirm no permanently stuck transfers). -- Confirming the causal chain requires the metric and log signatures to clear on cert renewal; retry/queue-depth metrics were never inspected, so backlog draining is unverified. [F18, F15, F20]
- **[short_term]** Add monitoring/alerts for non-HTTP failure paths on payment-service, especially a hard alert on mTLS/TLS handshake failures to bank-partner-api and on payment_success_rate degradation, so a flat HTTP error_rate no longer masks a payment-path outage. -- This failure was silent to the HTTP 5xx panel for 1h+; HIST-106's resolution included adding an expiry alert, and the same hiding mechanism applies here. [F17, F18, F13]
- **[long_term]** Add proactive certificate-expiry alerting/automation with renewal well ahead of notAfter for all egress mTLS credentials (payment-service to bank-partner-api and analogous settlement-service credentials), and treat cert-watcher WARNs as actionable tickets rather than log noise. -- The cert-watcher predicted the outage exactly, yet no automated remediation fired; HIST-106 shows the same class of fault recurring. [F16, F13]
- **[long_term]** Re-run the unexecuted checks to close the residual uncertainty: query kafka and ledger-db available_metrics for 12:00–13:30Z, and settlement-service and notification-service logs, to confirm the shared-component and downstream paths are healthy and that the only anomaly is the mTLS rejection. -- H4 (shared kafka/ledger-db saturation from dep-5510) was rejected on absence of evidence rather than on evidence of absence, since those metrics were never pulled; this bounds confidence in H1. [F12, F14]

## Data gaps
- bank-partner-api is external with has_telemetry=false and tc-022 query_logs failed (not_found); no metrics, logs or status feed exist for the partner, so its health at 12:15–13:59Z is unobservable and a co-occurring partner-side problem cannot be formally excluded.
- No certificate or DNS change API/feed was available, so certificate/DNS changes in the window can be neither confirmed nor excluded from change data; the cert-watcher entry (F16) is the only certificate-side evidence.
- No observability true-onset/blast-radius output (T1) was available, so the exact onset was anchored via the cert-watcher and log timestamps (F15/F16) rather than a dedicated onset determination.
- kafka and ledger-db are shared=true and have telemetry, but no metrics were pulled for 12:00–13:30Z, so shared-datastore/queue saturation as a contributor (the H4 path) is unverified rather than disproven.
- settlement-service and notification-service logs were never queried (budget exhausted), so an additional downstream anomaly or queue backlog cannot be confirmed.
- payment-service queue-depth / retry-count / db_pool_in_use saturation metrics were not inspected, so whether PENDING_RETRY payments will drain after renewal or remain stuck is undetermined.
- api-gateway logs for 12:00–13:59Z matched zero entries, which may mean either no gateway-side errors or that the gateway does not log this failure mode; upstream blast radius is unconfirmed.

## What would change this conclusion
- Evidence of the payment-service client certificate having been renewed before 12:15Z (i.e. it did not actually expire), which would break the notAfter-to-onset alignment in F16 and undercut H1.
- Independent evidence of a bank-partner-api-side outage or partner TLS/configuration change at ~12:15Z, or a partner log showing its own server-side certificate problem rather than rejecting our client credential, which would favour H2.
- A confirmed anomaly in a shared dependency (kafka lag or ledger-db write errors) or in settlement-service/notification-service starting at ~12:15Z, which would revive the H4 shared-component path.
- A deployment or config/certificate change inside 11:45Z–13:30Z that the current change data missed, which would reopen H3/the change-window theories.
- Recovery of bank_api_error_rate and payment_success_rate without any certificate renewal (e.g. spontaneous bank-side recovery), which would imply the failures were not caused by our credential expiry.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: payment-service latency_p99 DROPS at onset (323.5ms at 12:00Z → 140.0ms avg at t

## Investigation stats
- latency 109.29s, LLM calls 13, tokens 72962 (in 57418 / out 15544)
- tool calls 24 (1 failed, 0 partial, 0 recovered by retry), findings 20/20 verified
