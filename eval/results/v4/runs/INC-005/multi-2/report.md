# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
payment-service's outbound mTLS client certificate (CN=payments-client.orangeinvest.internal) expired at 2026-04-02T12:15:00Z, so bank-partner-api rejected every outbound transfer handshake; this is the top-ranked hypothesis H1 at 0.82 confidence (high), clearly ahead of all rejected alternatives. The customer-visible payment failures never touch payment-service's inbound HTTP 5xx metric, which is structurally blind to outbound/asynchronous adapter failures, explaining the green error_rate dashboard. Evidence is tight and non-coincidental: cert-watcher WARN at 10:20:44Z predicting expiry in 115 minutes, first ERROR at 12:15:27Z naming our own client cert with 'tls: expired certificate (alert 45)', bank_api_error_rate jumping 0.2%->100% and payment_success_rate collapsing to ~2.5% in exactly the 12:15Z bucket, and p99 latency dropping 320ms->140ms (calls fail fast, not hang). Deploy- and config-change alternatives were tested and excluded (no payment-service deploy in 08:00-13:59Z; the only config change is a timeout raise that cannot produce alert 45 and has a 55-minute timing gap), and the partner-side outage hypothesis is contradicted by the log naming our expired credential and zero partner config changes. Remaining uncertainty is limited to the partner's server-side trust state and the renewal/recovery timeline, which were not observable.

## Probable root cause
**Expired client mTLS certificate on payment-service's bank-partner-api path** -- credential_expiry in `payment-service (client certificate for bank-partner-api)`, triggered by `null` (confidence 0.82, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired client mTLS certificate on payment-service's bank-partner-api path | credential_expiry | payment-service (client certificate for bank-partner-api) | null | 0.82 (high) | active |
| 2 | H5: Metric/scrape artifact: payment-service error_rate panel misleading (green because blind to the failing path) | unknown | payment-service observability (metric definition) | null | 0.85 (high) | rejected |
| 3 | H2: Bank/PSP side outage or trust-policy change (reporter's suspicion) | dependency_failure | bank-partner-api (external vendor) | null | 0.06 (low) | rejected |
| 4 | H3: cfg-2502: bank API timeout raised 3000->5000ms converting bank errors into silent stalls | config_change | payment-service (config cfg-2502) | cfg-2502 | 0.04 (low) | rejected |
| 5 | H4: dep-5510 settlement-service deploy via shared ledger-db/kafka degradation | bad_deployment | settlement-service (deploy dep-5510) via shared ledger-db/kafka | dep-5510 | 0.02 (low) | rejected |

### H1: Expired client mTLS certificate on payment-service's bank-partner-api path
payment-service's outbound client certificate CN=payments-client.orangeinvest.internal expired at 2026-04-02T12:15:00Z, causing bank-partner-api to reject every mTLS handshake. All outbound transfer calls fail at the TLS layer, payments are marked PENDING_RETRY, and the customer-visible failure never touches payment-service's inbound HTTP 5xx metric, so the error_rate dashboard stays green.

**Causal chain**
1. A client certificate used by payment-service for outbound mTLS to bank-partner-api had notAfter=2026-04-02T12:15:00Z and was never renewed (no renewal automation on this path). [F5, F4]
2. A cert-watcher WARN at 10:20:44Z pre-announced the expiry; no remediation followed. [F5]
3. At 12:15Z the certificate expires; bank-partner-api begins rejecting the TLS handshake with 'remote error: tls: expired certificate (alert 45)'. [F3, F4, F13]
4. Every outbound transfer call fails fast at the handshake: bank_api_error_rate jumps 0.2%->100.0% and payment_success_rate collapses ~99.25%->~2.5% in the 12:15Z bucket, with first ERROR at 12:15:27Z. [F2, F3]
5. Failures are marked PENDING_RETRY (payments 45f233b6, 7b4c31a9, 62f775af) on an async/adapter path, producing customer-visible fund-transfer failures. [F4]
6. Because the failure is on the outbound dependency call, not the inbound HTTP edge, the 'HTTP 5xx' error_rate panel never moves and stays ~0.2%. [F1, F2]

**Supporting evidence**
  - `F2` ✔ payment-service's real business-failure signal collapses at the 12:15 bucket: bank_api_error_rate jumps from 0.2% to 100.0% and payment_success_rate drops from ~99.25% to ~2.5%, both staying flat at the failed level through 13:57Z. A narrow 120s-bucket query pins the transition between 12:13Z (0.21) and 12:15Z (100.0).
  - `F3` ✔ payment-service ERROR logs onset at 12:15:27Z (first 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected') and continue to 13:58:39Z, total_matched 311, truncated. The two dominant patterns are mTLS handshake rejections (140) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' (171) naming our client cert CN=payments-client.orangeinvest.internal.
  - `F4` ✔ The failure is an outbound call to bank-partner-api (adapter/processor path), not inbound client HTTP: log text names 'bank-partner-api call failed' and 'mTLS handshake with bank-partner-api rejected', and affected payments are marked PENDING_RETRY (e.g. payments 45f233b6, 7b4c31a9, 62f775af). The cert notAfter timestamp is 2026-04-02T12:15:00Z, matching the metric onset bucket exactly.
  - `F5` ✔ A cert-watcher WARN at 10:20:44Z pre-signalled the expiry: 'client certificate CN=payments-client.orangeinvest.internal expires in 115 minutes (notAfter=2026-04-02T12:15:00Z)' -- i.e. ~115 min before the 12:15Z failure onset. Logs before 11:45Z show only 3 WARN entries, no ERRORs.
  - `F13` ✘ unverified The onset coincidence is exact: payment-service's outbound mTLS client certificate CN=payments-client.orangeinvest.internal has notAfter=2026-04-02T12:15:00Z, and bank_api_error_rate jumps to 100.0% in the 12:15Z bucket with the first ERROR at 12:15:27Z. The failure is our own credential expiring on our side of the TLS handshake, not an inbound HTTP fault.
  - `F14` ✔ Historical precedent HIST-106 (2025-06-30, match_score 5) is literally 'Settlement mTLS certificate expired' on settlement-service: symptoms 'tls handshake failures','expired certificate'; root_cause 'Client certificate expired; no renewal automation'; category credential_expiry; resolution 'Issued new cert; added expiry alert'. This is the same failure mechanism as the current symptoms (cert_expiry), unlike HIST-105 which was a partner 5xx (match_score 1).
  - `F15` ✔ The reporter's 'bank/PSP side' hypothesis is weakened: the bank-partner-api is an external node of type 'external' with owner 'vendor' and has_telemetry=false, and there are ZERO config changes scoped to bank-partner-api in 10:00Z-13:59Z. The error log text names OUR cert CN explicitly ('remote error: tls: expired certificate (alert 45)', 'mTLS handshake with bank-partner-api rejected'), i.e. the partner is rejecting our expired credential, not serving 5xx.
  - `F16` ✔ The alternative 'fault is inside our stack via a deploy or config change' has no temporal match: ZERO deployments scoped to payment-service in 08:00Z-13:59Z (tc-018) and the only payment-service config change in 2026-04-01..13:59Z is cfg-2502 bank.api.timeout_ms 3000->5000 at 11:20Z (tc-019, a timeout raise, not a cert change). No change exists that could mint/expire a client cert.
  - `F17` ✔ The dependency graph confirms the failing path is reachable and isolated: payment-service's only external edge is bank-partner-api, distinct from the shared multi-tenant neighbours ledger-db (shared:true) and kafka (shared:true). No shared-component change is required to explain the failure, and shared neighbours would also show order-service/portfolio-service impact.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The cert expiry timestamp quoted in logs/telemetry is the actual cause and not coincidental with a partner-side policy change (bank-partner-api has no telemetry: tc-017/tc-021 could not confirm server-side trust config).
  - The client cert is provisioned/renewed outside the deployment and config-change systems, which is why no deploy or config change explains it (F16).
  - HIST-106 is treated as analogy, not evidence about the current incident.

### H5: Metric/scrape artifact: payment-service error_rate panel misleading (green because blind to the failing path)
The 'error_rate' panel is not stale or broken but structurally blind: it counts only inbound HTTP 5xx, so it cannot represent asynchronous transfer / outbound adapter failures. The green panel is an absence of relevant signal, not absence of errors, which masked a real internal fault.

**Causal chain**
1. The error_rate metric is explicitly '% of requests (HTTP 5xx)' and did not move during the incident (stays ~0.2%). [F1]
2. The real failures occur on an outbound/async path (bank-partner-api call, PENDING_RETRY), invisible to that panel. [F2, F4]
3. Therefore the green dashboard coexists with real payment failures. [F1, F2]

**Supporting evidence**
  - `F1` ✔ The HTTP error_rate metric for payment-service never moves during the incident: it stays ~0.2% (avg) across 11:45Z-13:59Z, max 0.32%, with summary min 0.1 / mean 0.22. The unit is explicitly '% of requests (HTTP 5xx)', so this panel only counts edge 5xx and does not reflect downstream/async payment failures.
  - `F2` ✔ payment-service's real business-failure signal collapses at the 12:15 bucket: bank_api_error_rate jumps from 0.2% to 100.0% and payment_success_rate drops from ~99.25% to ~2.5%, both staying flat at the failed level through 13:57Z. A narrow 120s-bucket query pins the transition between 12:13Z (0.21) and 12:15Z (100.0).
  - `F4` ✔ The failure is an outbound call to bank-partner-api (adapter/processor path), not inbound client HTTP: log text names 'bank-partner-api call failed' and 'mTLS handshake with bank-partner-api rejected', and affected payments are marked PENDING_RETRY (e.g. payments 45f233b6, 7b4c31a9, 62f775af). The cert notAfter timestamp is 2026-04-02T12:15:00Z, matching the metric onset bucket exactly.
**Contradicting evidence**
  - (none)
**Assumptions**
  - None about the origin -- this hypothesis describes the masking condition, not the fault origin.
**Rejected because:** This is a contributing condition (monitoring blind spot), not the fault origin: it explains why the dashboard stayed green but not why payments failed. The origin evidence points to the expired client certificate (H1). Retained in notes rather than as a competing root cause.

### H2: Bank/PSP side outage or trust-policy change (reporter's suspicion)
The bank-partner-api (external vendor) degraded or changed its TLS trust policy (e.g. stricter CRL/OCSP enforcement, rotated server cert, server-side misconfiguration) around 12:15Z, rejecting payment-service handshakes. Customer-visible failures originate at the partner, and the partner has no telemetry into our stack, so payment-service's error_rate panel stays green.

**Causal chain**
1. bank-partner-api is external, owner 'vendor', has_telemetry=false, so a partner-side fault would not surface on payment-service's edge metrics. [F9, F15]
2. Partner-side degradation at ~12:15Z would reject outgoing mTLS calls, producing the same failure signature as observed. [F2, F3]
3. payment-service marks affected payments PENDING_RETRY and customers see failed transfers while the HTTP 5xx panel is unchanged. [F4, F1]

**Supporting evidence**
  - `F9` ✔ payment-service has exactly four dependency edges: to ledger-db (db), to bank-partner-api (external, owner 'vendor', has_telemetry false), to kafka (queue-produce), and inbound from api-gateway (http).
  - `F15` ✔ The reporter's 'bank/PSP side' hypothesis is weakened: the bank-partner-api is an external node of type 'external' with owner 'vendor' and has_telemetry=false, and there are ZERO config changes scoped to bank-partner-api in 10:00Z-13:59Z. The error log text names OUR cert CN explicitly ('remote error: tls: expired certificate (alert 45)', 'mTLS handshake with bank-partner-api rejected'), i.e. the partner is rejecting our expired credential, not serving 5xx.
**Contradicting evidence**
  - `F3` ✔ payment-service ERROR logs onset at 12:15:27Z (first 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected') and continue to 13:58:39Z, total_matched 311, truncated. The two dominant patterns are mTLS handshake rejections (140) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' (171) naming our client cert CN=payments-client.orangeinvest.internal.
  - `F13` ✘ unverified The onset coincidence is exact: payment-service's outbound mTLS client certificate CN=payments-client.orangeinvest.internal has notAfter=2026-04-02T12:15:00Z, and bank_api_error_rate jumps to 100.0% in the 12:15Z bucket with the first ERROR at 12:15:27Z. The failure is our own credential expiring on our side of the TLS handshake, not an inbound HTTP fault.
  - `F4` ✔ The failure is an outbound call to bank-partner-api (adapter/processor path), not inbound client HTTP: log text names 'bank-partner-api call failed' and 'mTLS handshake with bank-partner-api rejected', and affected payments are marked PENDING_RETRY (e.g. payments 45f233b6, 7b4c31a9, 62f775af). The cert notAfter timestamp is 2026-04-02T12:15:00Z, matching the metric onset bucket exactly.
  - `F16` ✔ The alternative 'fault is inside our stack via a deploy or config change' has no temporal match: ZERO deployments scoped to payment-service in 08:00Z-13:59Z (tc-018) and the only payment-service config change in 2026-04-01..13:59Z is cfg-2502 bank.api.timeout_ms 3000->5000 at 11:20Z (tc-019, a timeout raise, not a cert change). No change exists that could mint/expire a client cert.
**Assumptions**
  - That the observed TLS rejection is caused by server-side trust-config change rather than our credential's notAfter timestamp.
  - That the exact notAfter=12:15:00Z coincidence with onset is incidental.
**Rejected because:** The error text explicitly names OUR client cert and the reason code 'tls: expired certificate (alert 45)' (F3/F4), and the onset matches our cert's notAfter=2026-04-02T12:15:00Z to the second (F13). 'alert 45' = certificate_expired, which is a statement about the client certificate we presented, not a 5xx from the partner. HIST-105 (partner 5xx) is a poor match (score 1) whereas HIST-106 (our cert expired) matches the mechanism (F14). No config/telemetry evidence points to a partner change.

### H3: cfg-2502: bank API timeout raised 3000->5000ms converting bank errors into silent stalls
The config change cfg-2502 on payment-service raised bank.api.timeout_ms from 3000 to 5000 at 11:20Z, changing the behavior of the external bank call path so that failures are no longer surfaced on edge metrics and payments silently stall while customers see failures.

**Causal chain**
1. cfg-2502 (bank.api.timeout_ms 3000->5000) was applied on payment-service at 11:20Z, the only config change scoped to the affected service, and was never rolled back. [F8]
2. Raising an outbound timeout can turn fast bank errors into long hangs/silent stalls that never raise payment-service's own error_rate. [F12]
3. Customers therefore observe failed/stalled transfers while the error-rate panel is green. [F1]

**Supporting evidence**
  - `F8` ✔ The only config change in the window was cfg-2502 on payment-service: key bank.api.timeout_ms changed from 3000 to 5000 at 2026-04-02T11:20:00Z by p.nair ('Raise bank API timeout after partner advisory'). It was the sole change scoped to payment-service and no rollback is recorded.
  - `F12` ✔ Ranking by timing fit to a ~12:15-12:30 onset: (1) cfg-2502 payment-service bank.api.timeout_ms 3000->5000 at 11:20Z - the only change on the affected service and the only one touching the external bank call path, and raising a timeout can convert fast bank errors into long hangs/silent stalls that never raise payment-service's own error_rate; (2) bank-partner-api - external, has_telemetry:false, so a partner-side failure would NOT surface in payment-service's error-rate panel (consistent with 'dashboard green but payments failing'); (3) dep-5510 settlement-service at 10:30Z - precedes onset but is an unrelated CSV export and settlement is not an edge of payment-service (only plausibly linked via shared ledger-db/kafka); (4) shared ledger-db / kafka as multi-tenant neighbours (failures there would also hit order-service/portfolio-service). This is an ordering of candidates, not a root-cause conclusion.
**Contradicting evidence**
  - `F2` ✔ payment-service's real business-failure signal collapses at the 12:15 bucket: bank_api_error_rate jumps from 0.2% to 100.0% and payment_success_rate drops from ~99.25% to ~2.5%, both staying flat at the failed level through 13:57Z. A narrow 120s-bucket query pins the transition between 12:13Z (0.21) and 12:15Z (100.0).
  - `F3` ✔ payment-service ERROR logs onset at 12:15:27Z (first 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected') and continue to 13:58:39Z, total_matched 311, truncated. The two dominant patterns are mTLS handshake rejections (140) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' (171) naming our client cert CN=payments-client.orangeinvest.internal.
  - `F6` ✔ Payment traffic volume did not change at onset (request_rate stays ~100 req/s, avg 100.51 across the window), so the success collapse is not a traffic artifact. latency_p99_ms actually DROPS from ~320ms to ~140ms at the 12:15 bucket -- consistent with calls failing fast at the failed TLS handshake rather than hanging.
  - `F13` ✘ unverified The onset coincidence is exact: payment-service's outbound mTLS client certificate CN=payments-client.orangeinvest.internal has notAfter=2026-04-02T12:15:00Z, and bank_api_error_rate jumps to 100.0% in the 12:15Z bucket with the first ERROR at 12:15:27Z. The failure is our own credential expiring on our side of the TLS handshake, not an inbound HTTP fault.
**Assumptions**
  - That a timeout raise could produce a 100% bank_api_error_rate and a TLS-level rejection message.
  - That the 11:20Z change is causally linked to the 12:15Z onset despite a ~55 minute quiet gap.
**Rejected because:** Mechanism contradicts the evidence: the observed errors are TLS handshake rejections with 'expired certificate (alert 45)' (F3/F13), which a timeout change cannot produce, and a longer timeout would raise latency while p99_ms actually DROPS from ~320ms to ~140ms (F6) -- calls fail fast, not hang. Timing is also poor: a 55-minute quiet gap between 11:20Z and the 12:15Z onset, and the config touches no certificate material (F16).

### H4: dep-5510 settlement-service deploy via shared ledger-db/kafka degradation
The settlement-service v1.9.0 deploy (dep-5510, new CSV exporter) at 10:30Z degraded a shared neighbour (ledger-db or kafka) or settlement processing, and payment-service's transfers failed as a side effect on a shared path, unseen by its edge error-rate metric.

**Causal chain**
1. dep-5510 deployed settlement-service v1.9.0 at 10:30Z with a new CSV exporter. [F7]
2. settlement-service shares ledger-db/kafka with payment-service, so a resource/load impact there could propagate. [F10]
3. That shared impact causes payment transfers to fail while edge metrics stay green. *(assumed)*

**Supporting evidence**
  - `F7` ✔ In the window 2026-04-02T10:00Z-13:59Z, the only deployment across all services was dep-5510: settlement-service version v1.9.0 deployed at 2026-04-02T10:30:00Z by ci-bot ('Batch settlement report export', 'new CSV exporter'), status live, rolled_back_at null.
  - `F10` ✔ ledger-db and kafka are marked shared:true and are each used by more than one service (ledger-db by payment-service, order-service and portfolio-service; kafka by payment-service and order-service), so those neighbours can affect payment-service through a shared datastore/broker. redis-cache is also shared:true but its consumers are auth-service, portfolio-service and market-data-service - payment-service has no redis edge.
**Contradicting evidence**
  - `F2` ✔ payment-service's real business-failure signal collapses at the 12:15 bucket: bank_api_error_rate jumps from 0.2% to 100.0% and payment_success_rate drops from ~99.25% to ~2.5%, both staying flat at the failed level through 13:57Z. A narrow 120s-bucket query pins the transition between 12:13Z (0.21) and 12:15Z (100.0).
  - `F3` ✔ payment-service ERROR logs onset at 12:15:27Z (first 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected') and continue to 13:58:39Z, total_matched 311, truncated. The two dominant patterns are mTLS handshake rejections (140) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' (171) naming our client cert CN=payments-client.orangeinvest.internal.
  - `F4` ✔ The failure is an outbound call to bank-partner-api (adapter/processor path), not inbound client HTTP: log text names 'bank-partner-api call failed' and 'mTLS handshake with bank-partner-api rejected', and affected payments are marked PENDING_RETRY (e.g. payments 45f233b6, 7b4c31a9, 62f775af). The cert notAfter timestamp is 2026-04-02T12:15:00Z, matching the metric onset bucket exactly.
  - `F10` ✔ ledger-db and kafka are marked shared:true and are each used by more than one service (ledger-db by payment-service, order-service and portfolio-service; kafka by payment-service and order-service), so those neighbours can affect payment-service through a shared datastore/broker. redis-cache is also shared:true but its consumers are auth-service, portfolio-service and market-data-service - payment-service has no redis edge.
  - `F17` ✔ The dependency graph confirms the failing path is reachable and isolated: payment-service's only external edge is bank-partner-api, distinct from the shared multi-tenant neighbours ledger-db (shared:true) and kafka (shared:true). No shared-component change is required to explain the failure, and shared neighbours would also show order-service/portfolio-service impact.
**Assumptions**
  - That a CSV-export deploy could degrade ledger-db/kafka stress near 12:15Z, ~105 minutes after deploy.
  - That shared-neighbour impact would manifest as a TLS certificate error.
**Rejected because:** The failure signature is a TLS handshake rejection naming our client cert (F3/F4), which shared DB/broker degradation cannot produce; the onset is ~105 minutes after the deploy with no evidence of shared-resource saturation, and shared neighbours would also visibly impact order-service/portfolio-service (F17). Settlement-service is not even an edge of payment-service.

## Facts (verified observations)
- The payment-service HTTP error_rate panel stayed ~0.2% (max 0.32%) across 11:45Z-13:59Z; its unit is explicitly '% of requests (HTTP 5xx)', so it counts only inbound edge 5xx and is blind to downstream/async failures. [F1]
- payment-service's business-failure signal collapsed at the 12:15 bucket: bank_api_error_rate went from 0.2% to 100.0% and payment_success_rate from ~99.25% to ~2.5%, both holding at failed levels through 13:57Z; a narrow 120s-bucket query pins the transition between 12:13Z (0.21) and 12:15Z (100.0). [F2]
- ERROR logs begin at 12:15:27Z ('payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected') and continue to 13:58:39Z with 311 matched lines; dominant patterns are mTLS handshake rejections (140) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' (171) naming CN=payments-client.orangeinvest.internal. [F3]
- The failure is on the outbound bank-partner-api adapter path, not inbound client HTTP: logs name 'bank-partner-api call failed' and 'mTLS handshake with bank-partner-api rejected', affected payments are marked PENDING_RETRY, and the cert notAfter timestamp 2026-04-02T12:15:00Z matches the metric onset bucket exactly. [F4]
- A cert-watcher WARN at 10:20:44Z pre-signalled the expiry: 'client certificate CN=payments-client.orangeinvest.internal expires in 115 minutes (notAfter=2026-04-02T12:15:00Z)'; logs before 11:45Z show only 3 WARN entries and no ERRORs. [F5]
- Traffic volume did not change at onset (request_rate ~100 req/s, avg 100.51), so the success collapse is not a traffic artifact; latency_p99_ms dropped from ~320ms to ~140ms at the 12:15 bucket, consistent with fast failure at the TLS handshake rather than hanging. [F6]
- In 10:00Z-13:59Z the only deployment across all services was dep-5510 (settlement-service v1.9.0, 10:30Z, CSV exporter, not rolled back) - unrelated to payment-service's outbound bank path. [F7]
- The only config change in the window was cfg-2502 on payment-service: bank.api.timeout_ms changed 3000->5000 at 11:20Z; it was the sole payment-service-scoped change and no rollback is recorded. [F8]
- payment-service has exactly four dependency edges: ledger-db (db), bank-partner-api (external, owner 'vendor', has_telemetry false), kafka (queue-produce), and inbound from api-gateway (http). [F9]
- ledger-db and kafka are shared:true multi-tenant neighbours (ledger-db used by payment-service, order-service, portfolio-service; kafka by payment-service and order-service), so degradation there would also impact other services. [F10]
- Precedent HIST-105 (2025-11-19) was a bank-partner-API 5xx outage ('their incident') resolved by waiting for the partner and queuing retries; it is a lead only, not evidence about the current incident. [F11]
- Historical precedent HIST-106 (2025-06-30, match_score 5) is 'Settlement mTLS certificate expired': root cause 'Client certificate expired; no renewal automation', category credential_expiry, resolved by issuing a new cert and adding an expiry alert - the same mechanism as the current symptoms, unlike HIST-105 (match_score 1). [F14]
- The bank side shows no supporting fault evidence: bank-partner-api is external with has_telemetry=false, there are ZERO config changes scoped to bank-partner-api in 10:00Z-13:59Z, and the error text names OUR cert ('remote error: tls: expired certificate (alert 45)'), i.e. the partner is rejecting our expired credential, not serving 5xx. [F15]
- No deploy or config change explains the failure: ZERO deployments scoped to payment-service in 08:00Z-13:59Z, and the only payment-service config change is cfg-2502 (a timeout raise, not a cert change) - no change exists that could mint or expire a client certificate. [F16]

## Assumptions (not verified)
- The cert expiry timestamp quoted in logs/telemetry is the actual cause and not coincidental with a partner-side TLS trust-policy change; bank-partner-api has no telemetry (tc-017/tc-021 could not confirm its server-side trust config).
- The client certificate is provisioned/renewed outside the deployment and config-change systems, which is why no deploy or config change explains it (F16).
- HIST-106 is treated as analogy supporting the mechanism, not as evidence about the current incident (as is HIST-105 for the rejected partner hypothesis).
- The 140 ms latency drop is attributable to fast TLS handshake failure rather than to an unrelated performance change; it was not correlated against a separate timeout-change metric.
- The 40-of-311 truncated ERROR sample is representative of the whole set; no unobserved, different error variant drives the incident.
- (demoted from facts: no verified observation backs it) Timing-fit ranking put cfg-2502 (11:20Z, sole change on the affected service) first and the external bank-partner-api second, with dep-5510 and shared ledger-db/kafka after; this is an ordering of candidates, not a root-cause conclusion.
- (demoted from facts: no verified observation backs it) The dependency graph confirms the failing path is reachable and isolated: payment-service's only external edge is bank-partner-api, distinct from the shared multi-tenant neighbours ledger-db and kafka, so no shared-component change is required to explain the failure.

## Timeline
- `2026-04-02T10:20:44Z` Cert-watcher WARN: client certificate CN=payments-client.orangeinvest.internal expires in 115 minutes (notAfter=2026-04-02T12:15:00Z). [F5]
- `2026-04-02T10:30:00Z` Only in-window deployment: dep-5510, settlement-service v1.9.0 (Batch settlement report export / new CSV exporter), via ci-bot; not rolled back. [F7]
- `2026-04-02T11:20:00Z` Only in-window config change: cfg-2502 on payment-service, bank.api.timeout_ms 3000->5000 by p.nair ('Raise bank API timeout after partner advisory'); no rollback recorded. [F8]
- `2026-04-02T12:15:00Z` Outbound client certificate notAfter timestamp reached; bank_api_error_rate jumps 0.2%->100.0% and payment_success_rate drops ~99.25%->~2.5% in the 12:15Z bucket (transition pinned between 12:13Z and 12:15Z). [F2, F4]
- `2026-04-02T12:15:00Z` latency_p99_ms drops from ~320ms to ~140ms while request_rate stays ~100 req/s - calls fail fast at the TLS handshake. [F6]
- `2026-04-02T12:15:27Z` First ERROR log: 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'; cert error text names our CN with 'tls: expired certificate (alert 45)'. [F3]
- `2026-04-02T12:32:00Z` Incident INC-005 reported (SEV-2) by support lead: ~40 tickets about failed fund transfers in the prior 15 minutes; payment-service HTTP error-rate panel shows nothing unusual. []
- `2026-04-02T12:15:00Z-13:59:00Z` HTTP error_rate panel stays ~0.2% (max 0.32%) throughout - structurally blind to the failing outbound path. [F1]
- `2026-04-02T13:57:00Z-13:58:39Z` bank_api_error_rate and payment_success_rate remain at failed levels through 13:57Z; ERROR logs continue to 13:58:39Z (311 matched lines, truncated sample). [F2, F3]

## Recommended actions
- **[immediate]** Immediately renew/reissue the payment-service outbound client certificate for bank-partner-api (CN=payments-client.orangeinvest.internal) and install it on the payment-service bank-partner-api client path, then restart/reload the adapter to pick up the new credential. -- H1: the cert notAfter=2026-04-02T12:15:00Z exactly matches the failure onset, and bank-partner-api rejects with 'tls: expired certificate (alert 45)' naming our cert; a fresh credential is the direct mitigation. [F2, F3, F4, F13]
- **[immediate]** Reprocess the queued payments stuck in PENDING_RETRY (and any dead-lettered items on the bank-partner-api adapter path) once the new certificate is live, and reconcile against the ledger to confirm settlement. -- H1: affected payments were marked PENDING_RETRY rather than failing terminally; the customer-visible failure is the missing completion, so retries must be drained after the credential fix. [F3, F4]
- **[short_term]** Verify recovery by confirming bank_api_error_rate returns to ~0.2% and payment_success_rate returns to ~99%+ after the cert replacement, and confirm ERROR log volume on the bank-partner-api path ceases. -- The pre-incident baseline is bank_api_error_rate 0.2% / success ~99.25%, so these metrics are the discriminating post-fix checks. [F2]
- **[short_term]** Add a dashboard/alert on bank_api_error_rate and payment_success_rate (business-failure signals), in addition to the inbound HTTP 5xx error_rate panel. -- H5 masked the incident: the green panel counts only inbound HTTP 5xx and cannot represent outbound/async transfer failures (F1/F4). Adding business-failure signals closes the detection gap. [F1, F2]
- **[short_term]** No rollback of cfg-2502 (bank.api.timeout_ms 3000->5000) is required as an incident mitigation; treat it as out of scope for the root cause but evaluate separately whether the raised timeout should be retained. -- The timeout raise cannot produce a TLS 'expired certificate (alert 45)' rejection, and p99 latency dropped (fast fail) rather than rising (hang), so it is not the cause (F3/F6/F8). [F3, F6, F8]
- **[long_term]** Implement automated certificate lifecycle management: alert at a defined lead time (e.g. 7/30 days) on client-certificate notAfter for all outbound mTLS credentials, plus auto-renewal or a tracked renewal runbook. -- HIST-106 is the identical mechanism ('Client certificate expired; no renewal automation') and was resolved with a new cert plus an expiry alert; the existing watcher WARN (115-min lead) fired but did not trigger action. [F5, F14]
- **[long_term]** Route the 115-minute cert-expiry WARN to a paging/actionable alert path (not just logs) so pre-expiry warnings trigger a renewal task before customer impact. -- The watcher warned at 10:20:44Z yet the failure still occurred at 12:15Z, indicating a warning-without-action gap. [F5]
- **[long_term]** Add synthetic end-to-end transfer probes (client path through bank-partner-api adapter) to catch outbound-path failures that edge metrics miss. -- The incident was only detected via customer tickets because the edge error-rate panel stayed ~0.2% (F1/F2). [F1, F2]

## Data gaps
- bank-partner-api logs / server-side view of the rejected handshake could not be queried (budget exhausted after tc-017) - limits confirmation that only our expired client cert, and not also a partner-side trust-config change, caused the rejection.
- Retry/DLQ/queue-depth checks were not executed (tc-018 not run) - the number of payments stuck in PENDING_RETRY or dead-lettered, and the total impacted transaction count, is unknown beyond 311 ERROR lines and the ~2.5% success rate.
- The ERROR log result was truncated (40 of 311 lines) and the timeout metric was not queried separately - a rarer error signature could be unobserved.
- No prior incident exactly matched 'dashboard green but customer payments failing error rate normal' (count 0); precedent support is by analogy only (match_score <=4 with looser keywords).
- Infrastructure changes (load balancer, DNS, secrets/keys, node/infra events) could not be queried - an infra change near 12:15Z cannot be ruled in or out.
- Component-level config for kafka, ledger-db, api-gateway routes, redis-cache and the bank-partner-api connector was not queried - cannot fully exclude a shared-infrastructure config/flag change.
- Metric-level data to correlate cfg-2502 (11:20Z) with any latency/error shift was not obtained - the timing link remains an ordering argument, not a measured correlation.
- No telemetry or config data exists for bank-partner-api (has_telemetry=false; config-change count 0), so a simultaneous partner-side certificate-policy change cannot be fully excluded as a co-factor.
- The log query was truncated, so errors after 13:58:39Z were not counted - recovery time and whether the cert was renewed in-window are undetermined.

## What would change this conclusion
- Partner-side evidence (bank-partner-api logs or vendor confirmation) showing the handshake was rejected for a reason other than our certificate's expiry - e.g. a server-side trust-policy/CRL/OCSP change or server misconfiguration taking effect at ~12:15Z. This would move H2 back into contention.
- Evidence that the client certificate was successfully renewed/rotated and reloaded at or before 12:15Z (in which case the notAfter timestamp could not be the operative cause).
- New error-log variants from the untruncated 311-line set showing a different failure signature (e.g. connection refused, 5xx from the partner, timeout) rather than 'expired certificate (alert 45)'.
- A config/secret/deploy change scoped to payment-service or its bank-partner-api connector (or to shared ledger-db/kafka/gateway) at approximately 12:13-12:15Z that was not visible in the deploy and config queries.
- Demonstration that bank_api_error_rate and payment_success_rate recovered (or the ERROR logs ceased) before any certificate change - indicating an alternative explanation for the ~12:15Z onset and later recovery.

## Integrity warnings (set by code)
- fact demoted to assumption: Timing-fit ranking put cfg-2502 (11:20Z, sole change on the affected service) fi
- fact demoted to assumption: The dependency graph confirms the failing path is reachable and isolated: paymen

## Investigation stats
- latency 136.1s, LLM calls 13, tokens 89811 (in 71536 / out 18275)
- tool calls 21 (0 failed, 0 partial, 0 recovered by retry), findings 16/17 verified
