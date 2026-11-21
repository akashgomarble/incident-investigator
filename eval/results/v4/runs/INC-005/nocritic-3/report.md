# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
Root cause (top hypothesis H1, confidence 0.60, medium): payment-service's mTLS client certificate CN=payments-client.orangeinvest.internal expired at 2026-04-02T12:15:00Z and was not renewed, so every call to bank-partner-api failed at the TLS handshake and fund transfers could not be executed. payment-service returned success-shaped (non-5xx) responses to callers, which is why the HTTP 5xx error-rate panel stayed flat (0.19-0.27%) while payment_success_rate collapsed from ~99% to ~2.5% and bank_api_error_rate went to 100%. The failure is localized to our own client-side certificate by 134 log lines naming the expired cert, pre-figured by a cert-watcher WARN at 11:50Z. Competing hypotheses (external bank outage H2 0.12, settlement deploy H3 0.06, timeout config H4 0.05, reporting artifact H5 0.02) are all contradicted or unsupported.

## Probable root cause
**payment-service's client mTLS certificate to bank-partner-api expired at 12:15Z, breaking all bank calls** -- credential_expiry in `payment-service`, triggered by `null` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: payment-service's client mTLS certificate to bank-partner-api expired at 12:15Z, breaking all bank calls | credential_expiry | payment-service | null | 0.60 (medium) | active |
| 2 | H2: Bank partner / PSP (bank-partner-api) degraded externally, breaking transfers | dependency_failure | bank-partner-api | null | 0.12 (low) | active |
| 3 | H3: settlement-service v1.9.0 CSV exporter deployment (dep-5510) overloaded shared components, degrading payments | bad_deployment | settlement-service | dep-5510 | 0.06 (low) | active |
| 4 | H4: Config change cfg-2502 (bank.api.timeout_ms 3000->5000) broke or masked bank calls | config_change | payment-service | cfg-2502 | 0.05 (low) | rejected |
| 5 | H5: Failures are external/client-side or a reporting artifact with no server-side anomaly | unknown | unknown | null | 0.02 (low) | rejected |

### H1: payment-service's client mTLS certificate to bank-partner-api expired at 12:15Z, breaking all bank calls
The mTLS client certificate CN=payments-client.orangeinvest.internal (owned by payment-service) reached its notAfter=2026-04-02T12:15:00Z and was not renewed. From 12:14-12:15Z onward every call to bank-partner-api failed at the TLS handshake ('tls: expired certificate (alert 45)'), so fund transfers could not be executed. payment-service short-circuited these calls and returned success-shaped (non-5xx) responses to callers, which is why the HTTP 5xx error-rate panel stayed green while payment_success_rate collapsed to ~2.5%. Category: credential_expiry — a time-based lifecycle event, not a deployment.

**Causal chain**
1. The client certificate CN=payments-client.orangeinvest.internal was scheduled to expire at 2026-04-02T12:15:00Z and no renewal/rotation occurred in the change feeds. [F11, F12, F15]
2. Cert expiry at 12:15Z -> mTLS handshake with bank-partner-api rejected; bank_api_error_rate jumps 0.2% -> 100% at 12:14Z. [F8, F11]
3. Rejected handshakes -> transfers marked PENDING_RETRY / never settle; payment_success_rate collapses 99% -> ~2.5% from 12:20Z. [F9, F11]
4. payment-service returns non-5xx to callers for these failed transfers, so HTTP error_rate stays flat at 0.19-0.27% (green dashboard) while absolute failures only surface as support tickets. [F10]
5. Failed calls fail fast rather than hang: p99 latency drops from ~320ms to ~140ms and request_rate stays ~100 req/s, matching short-circuited TLS failures. [F13]

**Supporting evidence**
  - `F8` ✔ payment-service's bank_api_error_rate jumped from ~0.2% to 100% at 12:14Z (bucket 12:14 avg=50.1, max=100) and stayed at 100% through 12:30Z, versus a flat ~0.20% baseline from 11:00-12:12Z.
  - `F9` ✔ payment-service's payment_success_rate collapsed from ~99% to ~2.5%: 12:12Z avg=38.93, then 12:20Z avg=2.72 / max=3.3, and remained ~2.1-2.7% through 13:56Z (summary min 1.44, mean 42.74).
  - `F10` ✔ payment-service error_rate (HTTP 5xx) stayed flat at baseline the entire window: avg 0.19-0.27%, max 0.32% from 11:00 through 13:56Z, with no deviation at the 12:12-12:28 onset buckets (avg 0.23 / 0.19 / 0.24).
  - `F11` ✔ payment-service logs show the failure signature is an expired client certificate at the bank boundary: 80x 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (first 12:16:44Z) and 54x 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (first 12:15:27Z).
  - `F12` ✔ A cert-watcher WARN at 11:50:26Z pre-dated the failure: 'client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)'; a single 'retrying idempotent request (attempt 2) after connection reset' WARN appeared at 12:04:22Z, before the 12:15 expiry.
  - `F13` ✔ payment-service request_rate and latency_p99_ms show no overload signature: request_rate held steady ~90-110 req/s across the incident (mean 100.21), and p99 latency actually DROPPED from ~320ms baseline to ~138-144ms after 12:20Z, consistent with failed calls short-circuiting rather than hanging.
  - `F14` ✔ bank-partner-api has no directly observable telemetry: querying it returns not_found with 'this is an external dependency: we have no logs/metrics for it. Observe it through its callers.' So the bank side can only be assessed via payment-service's bank_api_error_rate.
  - `F15` ✔ Re-queried all-services changes for 09:00-13:59Z: exactly one config change (cfg-2502, payment-service, bank.api.timeout_ms 3000->5000, 11:20:00Z, p.nair) and one deployment (dep-5510, settlement-service v1.9.0, 10:30:00Z). No certificate/credential/secret change or payment-service deployment appears anywhere in the window.
  - `F16` ✔ Change-to-onset alignment: the latest change before onset is cfg-2502 at 11:20Z, ~54-55 minutes BEFORE the 12:14-12:15Z onset; the settlement deploy is ~104 min before. Neither is at or just before onset, so no internal change aligns tightly with the failure onset.
  - `F17` ✔ Reporter's external-only theory is contradicted at the boundary description but not fully: payment-service's failure is at its OWN client certificate (CN=payments-client.orangeinvest.internal, notAfter=2026-04-02T12:15:00Z), i.e. an expired credential on the CLIENT side of the mTLS handshake, not a bank-side 5xx. HIST-105 (external bank 5xx) does NOT match; HIST-106 (settlement mTLS certificate expired, credential_expiry, higher match_score 4) is the closer precedent.
  - `F19` ✔ External-only cause cannot be confirmed from outside: bank-partner-api has no telemetry (has_telemetry:false, type external), so no independent signal exists to attribute the failure to the bank. The only evidence localizes the failure to payment-service's client cert.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The cert-watcher WARN at 11:50Z (F12) and the 12:15:27Z PENDING_RETRY log (F11) are the same certificate named in the TLS alert; the log evidence is treated as one coherent signature.
  - Non-5xx status returned to callers for failed transfers is inferred from the flat HTTP error_rate (F10) plus collapsed success rate (F9); no direct status-code finding exists.
  - The absence of any credential/cert change in the config and deploy feeds (F15) reflects a genuine lack of renewal rather than monitoring gaps in those feeds.

### H2: Bank partner / PSP (bank-partner-api) degraded externally, breaking transfers
bank-partner-api, the external bank/PSP connector, degraded on its own side (5xx / handshake-side failure), so payment-service's bank calls failed and transfers did not complete — matching the only exact historical precedent HIST-105 ('Payment failures: bank partner API outage', 2025-11-19). payment-service's HTTP error-rate panel stayed green because the failure occurred on outbound dependency calls, not inbound requests.

**Causal chain**
1. bank-partner-api degrades externally (no internal change in the 09:00-13:59Z window). [F1, F15, F6]
2. Degradation -> payment-service bank calls fail; bank_api_error_rate jumps 0.2% -> 100% at 12:14Z. [F8]
3. Failed bank calls -> transfers do not complete; payment_success_rate collapses to ~2.5%. [F9]
4. Outbound dependency failure is invisible to the inbound HTTP 5xx error-rate panel, explaining the green dashboard. [F10]

**Supporting evidence**
  - `F6` ✔ Historical precedents matching 'payment failures without payment-service errors': HIST-105 (2025-11-19) 'Payment failures: bank partner API outage' on payment-service, root cause 'Bank partner API returned 5xx for 40 minutes (their incident)', category dependency_failure, match_score 2 — the only exact symptom match. Related leads: HIST-106 (settlement mTLS certificate expired) and HIST-111 (reporting job full table scans slowed ledger-db, services ledger-db+settlement-service).
  - `F8` ✔ payment-service's bank_api_error_rate jumped from ~0.2% to 100% at 12:14Z (bucket 12:14 avg=50.1, max=100) and stayed at 100% through 12:30Z, versus a flat ~0.20% baseline from 11:00-12:12Z.
  - `F9` ✔ payment-service's payment_success_rate collapsed from ~99% to ~2.5%: 12:12Z avg=38.93, then 12:20Z avg=2.72 / max=3.3, and remained ~2.1-2.7% through 13:56Z (summary min 1.44, mean 42.74).
  - `F10` ✔ payment-service error_rate (HTTP 5xx) stayed flat at baseline the entire window: avg 0.19-0.27%, max 0.32% from 11:00 through 13:56Z, with no deviation at the 12:12-12:28 onset buckets (avg 0.23 / 0.19 / 0.24).
  - `F3` ✔ payment-service direct dependency edges: -> ledger-db (db), -> bank-partner-api (external), -> kafka (queue-produce); inbound: api-gateway -> payment-service (http). bank-partner-api is type external, owner vendor, has_telemetry:false.
**Contradicting evidence**
  - `F11` ✔ payment-service logs show the failure signature is an expired client certificate at the bank boundary: 80x 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (first 12:16:44Z) and 54x 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (first 12:15:27Z).
  - `F17` ✔ Reporter's external-only theory is contradicted at the boundary description but not fully: payment-service's failure is at its OWN client certificate (CN=payments-client.orangeinvest.internal, notAfter=2026-04-02T12:15:00Z), i.e. an expired credential on the CLIENT side of the mTLS handshake, not a bank-side 5xx. HIST-105 (external bank 5xx) does NOT match; HIST-106 (settlement mTLS certificate expired, credential_expiry, higher match_score 4) is the closer precedent.
  - `F19` ✔ External-only cause cannot be confirmed from outside: bank-partner-api has no telemetry (has_telemetry:false, type external), so no independent signal exists to attribute the failure to the bank. The only evidence localizes the failure to payment-service's client cert.
  - `F14` ✔ bank-partner-api has no directly observable telemetry: querying it returns not_found with 'this is an external dependency: we have no logs/metrics for it. Observe it through its callers.' So the bank side can only be assessed via payment-service's bank_api_error_rate.
  - `F12` ✔ A cert-watcher WARN at 11:50:26Z pre-dated the failure: 'client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)'; a single 'retrying idempotent request (attempt 2) after connection reset' WARN appeared at 12:04:22Z, before the 12:15 expiry.
**Assumptions**
  - The TLS 'expired certificate' errors observed could be attributed to the partner side if the alert were a server-certificate rejection, though the log text names OUR client cert and CN.
  - That bank-partner-api has an independent outage despite zero telemetry to confirm it.

### H3: settlement-service v1.9.0 CSV exporter deployment (dep-5510) overloaded shared components, degrading payments
settlement-service v1.9.0 'Batch settlement report export / new CSV exporter' deployed at 10:30Z overloaded a shared component in payment-service's graph (ledger-db, a shared postgres written by four services, or kafka, produced/consumed by four services), degrading payment-service's ability to complete transfers while leaving its inbound HTTP error rate flat. Precedent HIST-111 (a reporting job's full table scans slowed ledger-db).

**Causal chain**
1. dep-5510 (settlement-service v1.9.0, new CSV exporter) deploys at 10:30Z. [F1]
2. The exporter performs heavy reads/writes against the shared ledger-db and/or produces to shared kafka, saturating a fan-in point used by payment-service. [F5]
3. Shared-component contention -> payment-service transfers slow or stall -> payment_success_rate collapses. [F9]
4. Failure is at a dependency hop, so payment-service's inbound 5xx error_rate stays flat. [F10]

**Supporting evidence**
  - `F1` ✔ Only one deployment occurred across all services in the 09:00–13:59Z window: settlement-service v1.9.0 (dep-5510) at 2026-04-02T10:30:00Z, summary 'Batch settlement report export' / 'new CSV exporter', status live with rolled_back_at null. No payment-service deployment exists in this window.
  - `F4` ✔ Two shared components are in payment-service's graph and are shared=true: ledger-db (postgres) and kafka — the note states "'shared' datastores are used by several services".
  - `F5` ✔ ledger-db is written by four services (order-service, portfolio-service, payment-service, settlement-service) and kafka is produced/consumed by four services (order-service, payment-service, settlement-service, notification-service) — so both are cross-team fan-in points that could affect payment-service from outside.
  - `F6` ✔ Historical precedents matching 'payment failures without payment-service errors': HIST-105 (2025-11-19) 'Payment failures: bank partner API outage' on payment-service, root cause 'Bank partner API returned 5xx for 40 minutes (their incident)', category dependency_failure, match_score 2 — the only exact symptom match. Related leads: HIST-106 (settlement mTLS certificate expired) and HIST-111 (reporting job full table scans slowed ledger-db, services ledger-db+settlement-service).
**Contradicting evidence**
  - `F11` ✔ payment-service logs show the failure signature is an expired client certificate at the bank boundary: 80x 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (first 12:16:44Z) and 54x 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (first 12:15:27Z).
  - `F8` ✔ payment-service's bank_api_error_rate jumped from ~0.2% to 100% at 12:14Z (bucket 12:14 avg=50.1, max=100) and stayed at 100% through 12:30Z, versus a flat ~0.20% baseline from 11:00-12:12Z.
  - `F13` ✔ payment-service request_rate and latency_p99_ms show no overload signature: request_rate held steady ~90-110 req/s across the incident (mean 100.21), and p99 latency actually DROPPED from ~320ms baseline to ~138-144ms after 12:20Z, consistent with failed calls short-circuiting rather than hanging.
  - `F16` ✔ Change-to-onset alignment: the latest change before onset is cfg-2502 at 11:20Z, ~54-55 minutes BEFORE the 12:14-12:15Z onset; the settlement deploy is ~104 min before. Neither is at or just before onset, so no internal change aligns tightly with the failure onset.
  - `F18` ✔ cfg-2502 is a timeout INCREASE (3000->5000ms) justified by a 'partner advisory', not a credential rotation. It can only mask hangs (make calls wait longer), and T1's F13 shows latency actually DROPPED, so this change is not the failure mechanism; at most it is unrelated context (a partner advisory predating the cert expiry).
**Assumptions**
  - The CSV exporter actually exercises shared components at volumes that affect other tenants.
  - No telemetry (db_pool_in_use, kafka lag, ledger-db latency) contradicting this was collected — resource/backlog metrics were never pulled (gap noted).

### H4: Config change cfg-2502 (bank.api.timeout_ms 3000->5000) broke or masked bank calls
The 11:20Z payment-service config change cfg-2502 raised bank.api.timeout_ms from 3000 to 5000 after a 'partner advisory', and this change is the origin of the payment failures — either by letting calls hang longer or by interacting with a partner-side change. The 'partner advisory' hints an external condition coincided with the change window.

**Causal chain**
1. cfg-2502 changes bank.api.timeout_ms 3000->5000 at 11:20Z. [F2, F15]
2. A timeout increase could only lengthen waits for a slow/hanging partner, potentially converting failures into hangs or masking them. [F18]
3. This manifests as payment failures with normal HTTP error rate. [F9, F10]

**Supporting evidence**
  - `F2` ✔ Exactly one config change in the window: payment-service cfg-2502 changed bank.api.timeout_ms from 3000 to 5000 at 2026-04-02T11:20:00Z by p.nair, described as 'Raise bank API timeout after partner advisory'.
  - `F7` ✔ Timeline alignment: the settlement-service deploy (10:30Z) and the payment-service bank timeout config change (11:20Z) both precede the 12:32Z report, and the config change is explicitly connected to a 'partner advisory' about the bank API; the timeout was raised (3s→5s), not lowered or rolled back.
**Contradicting evidence**
  - `F13` ✔ payment-service request_rate and latency_p99_ms show no overload signature: request_rate held steady ~90-110 req/s across the incident (mean 100.21), and p99 latency actually DROPPED from ~320ms baseline to ~138-144ms after 12:20Z, consistent with failed calls short-circuiting rather than hanging.
  - `F16` ✔ Change-to-onset alignment: the latest change before onset is cfg-2502 at 11:20Z, ~54-55 minutes BEFORE the 12:14-12:15Z onset; the settlement deploy is ~104 min before. Neither is at or just before onset, so no internal change aligns tightly with the failure onset.
  - `F18` ✔ cfg-2502 is a timeout INCREASE (3000->5000ms) justified by a 'partner advisory', not a credential rotation. It can only mask hangs (make calls wait longer), and T1's F13 shows latency actually DROPPED, so this change is not the failure mechanism; at most it is unrelated context (a partner advisory predating the cert expiry).
  - `F11` ✔ payment-service logs show the failure signature is an expired client certificate at the bank boundary: 80x 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (first 12:16:44Z) and 54x 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (first 12:15:27Z).
**Assumptions**
  - That a pure timeout increase can cause (not merely mask) failed transfers.
**Rejected because:** cfg-2502 is a timeout INCREASE (3s->5s), which can only make hung calls wait longer, yet latency actually DROPPED (~320ms -> ~140ms), consistent with fast short-circuits (F13, F18). It occurred 54-55 min before onset, no internal change aligns with the 12:14-12:15Z onset (F16), and the failure signature is an expired client certificate (F11), which no timeout value can produce.

### H5: Failures are external/client-side or a reporting artifact with no server-side anomaly
The support lead's theory: the client app or the bank side is genuinely at fault, and there is no server-side anomaly — the ~40 tickets represent an absolute-volume blip masked by the error-rate denominator, or external failures not visible to server telemetry.

**Causal chain**
1. Client app or bank side fails externally. *(assumed)*
2. No server-side anomaly occurs, so payment-service's HTTP error-rate panel is correctly green. [F10]

**Supporting evidence**
  - `F10` ✔ payment-service error_rate (HTTP 5xx) stayed flat at baseline the entire window: avg 0.19-0.27%, max 0.32% from 11:00 through 13:56Z, with no deviation at the 12:12-12:28 onset buckets (avg 0.23 / 0.19 / 0.24).
**Contradicting evidence**
  - `F8` ✔ payment-service's bank_api_error_rate jumped from ~0.2% to 100% at 12:14Z (bucket 12:14 avg=50.1, max=100) and stayed at 100% through 12:30Z, versus a flat ~0.20% baseline from 11:00-12:12Z.
  - `F9` ✔ payment-service's payment_success_rate collapsed from ~99% to ~2.5%: 12:12Z avg=38.93, then 12:20Z avg=2.72 / max=3.3, and remained ~2.1-2.7% through 13:56Z (summary min 1.44, mean 42.74).
  - `F11` ✔ payment-service logs show the failure signature is an expired client certificate at the bank boundary: 80x 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (first 12:16:44Z) and 54x 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (first 12:15:27Z).
  - `F12` ✔ A cert-watcher WARN at 11:50:26Z pre-dated the failure: 'client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)'; a single 'retrying idempotent request (attempt 2) after connection reset' WARN appeared at 12:04:22Z, before the 12:15 expiry.
  - `F13` ✔ payment-service request_rate and latency_p99_ms show no overload signature: request_rate held steady ~90-110 req/s across the incident (mean 100.21), and p99 latency actually DROPPED from ~320ms baseline to ~138-144ms after 12:20Z, consistent with failed calls short-circuiting rather than hanging.
  - `F17` ✔ Reporter's external-only theory is contradicted at the boundary description but not fully: payment-service's failure is at its OWN client certificate (CN=payments-client.orangeinvest.internal, notAfter=2026-04-02T12:15:00Z), i.e. an expired credential on the CLIENT side of the mTLS handshake, not a bank-side 5xx. HIST-105 (external bank 5xx) does NOT match; HIST-106 (settlement mTLS certificate expired, credential_expiry, higher match_score 4) is the closer precedent.
**Assumptions**
  - That externally observed failures leave no server-side signature.
**Rejected because:** Server-side telemetry shows a dramatic real anomaly: bank_api_error_rate 0.2% -> 100% (F8), payment_success_rate 99% -> 2.5% (F9), and 134 log lines naming an expired client certificate at the bank boundary (F11), pre-figured by a cert-watcher WARN at 11:50Z (F12). This is not an artifact and not purely external.

## Facts (verified observations)
- payment-service's client-side failure signature is an expired mTLS client certificate: 80x 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' and 54x 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (first 12:15:27Z). [F11]
- A cert-watcher WARN at 2026-04-02T11:50:26Z pre-dated the failure, stating the client certificate CN=payments-client.orangeinvest.internal would expire in 25 minutes at notAfter=2026-04-02T12:15:00Z. [F12]
- payment-service's bank_api_error_rate jumped from ~0.2% to 100% at 12:14Z and stayed at 100% through 12:30Z, versus a flat ~0.20% baseline from 11:00-12:12Z. [F8]
- payment-service's payment_success_rate collapsed from ~99% to ~2.5% at 12:20Z and remained ~2.1-2.7% through 13:56Z. [F9]
- payment-service HTTP error_rate (5xx) stayed flat at baseline (avg 0.19-0.27%, max 0.32%) for the entire window including the 12:12-12:28 onset buckets, explaining the green error-rate dashboard. [F10]
- payment-service request_rate held steady (~90-110 req/s) and p99 latency actually DROPPED from ~320ms to ~138-144ms after 12:20Z, consistent with failed calls short-circuiting rather than hanging or saturating. [F13]
- Exactly one deployment occurred across all services in 09:00-13:59Z: settlement-service v1.9.0 (dep-5510) at 10:30Z; no payment-service deployment exists in the window. [F1, F15]
- Exactly one config change occurred in the window: payment-service cfg-2502 raised bank.api.timeout_ms from 3000 to 5000 at 11:20:00Z. [F2, F15]
- No certificate/credential/secret change or payment-service deployment appears anywhere in the 09:00-13:59Z config/deploy feeds. [F15]
- cfg-2502 is a timeout increase (3000->5000ms), which can only mask hangs and cannot produce an expired-certificate failure; latency dropping rather than rising further excludes it as the mechanism. [F13]
- bank-partner-api has no directly observable telemetry (type external, has_telemetry:false); it can only be observed through its callers, so no independent signal exists to attribute the failure to the bank side. [F3, F14, F19]
- The only exact historical symptom match (HIST-105, external bank 5xx) does not match; the closer precedent is HIST-106 (mTLS certificate expired, credential_expiry). [F6]

## Assumptions (not verified)
- The cert-watcher WARN at 11:50Z (F12) and the 12:15:27Z PENDING_RETRY/TLS-alert logs (F11) refer to the same certificate named in the TLS alert; the log evidence is treated as one coherent signature.
- The non-5xx status returned to callers for failed transfers is inferred from the flat HTTP error_rate (F10) plus the collapsed success rate (F9); no direct status-code finding exists.
- The absence of any credential/cert change in the config and deploy feeds (F15) reflects a genuine lack of renewal rather than monitoring gaps in those feeds.
- bank-partner-api was concurrently healthy; this cannot be verified directly because the partner exposes no telemetry (F14, F19).
- (demoted from facts: no verified observation backs it) No internal change aligns tightly with the 12:14-12:15Z onset: cfg-2502 at 11:20Z is ~54-55 minutes before onset and dep-5510 is ~104 minutes before.

## Timeline
- `2026-04-02T10:30:00Z` Only deployment in the window: settlement-service v1.9.0 (dep-5510, 'Batch settlement report export' / new CSV exporter); no payment-service deployment. [F1, F15]
- `2026-04-02T11:20:00Z` Only config change in the window: payment-service cfg-2502 raises bank.api.timeout_ms 3000->5000 'after partner advisory'. [F2, F15]
- `2026-04-02T11:50:26Z` cert-watcher WARN: client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z). [F12]
- `2026-04-02T12:04:22Z` Single WARN 'retrying idempotent request (attempt 2) after connection reset' appears, before the 12:15 expiry. [F12]
- `2026-04-02T12:14:00Z` payment-service bank_api_error_rate jumps from ~0.2% to 100% and stays at 100% through 12:30Z. [F8]
- `2026-04-02T12:15:00Z` Client certificate CN=payments-client.orangeinvest.internal reaches notAfter=2026-04-02T12:15:00Z. [F11, F12]
- `2026-04-02T12:15:27Z` First 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' log line. [F11]
- `2026-04-02T12:16:44Z` First 'tls: expired certificate (alert 45)' log line naming our client cert. [F11]
- `2026-04-02T12:20:00Z` payment_success_rate collapses to ~2.5% (avg 2.72, max 3.3) and remains ~2.1-2.7% through 13:56Z; p99 latency drops to ~138-144ms; HTTP 5xx error_rate unchanged. [F9, F13, F10]
- `2026-04-02T12:32:00Z` Incident INC-005 reported by support lead: ~40 tickets for failed fund transfers while the payment-service error-rate panel is green. [F10, F9]
- `2026-04-02T13:59:00Z` Latest data: payment_success_rate still ~2.1-2.7%; failure persists at end of window. [F9]

## Recommended actions
- **[immediate]** Renew/rotate the payment-service mTLS client certificate CN=payments-client.orangeinvest.internal (notAfter=2026-04-02T12:15:00Z) via the credential/cert provisioning path, then restart/reload payment-service so the new cert is loaded, and verify bank_api_error_rate returns to its ~0.2% baseline and payment_success_rate recovers to ~99%. -- This is the specific expired credential identified at the failure boundary; every bank call fails at the TLS handshake until it is replaced. [F11, F12, F8, F9]
- **[immediate]** While the renewal is in flight, treat the bank-call failures as intentional fail-fast: confirm payment-service is not silently returning success-shaped responses to customers for failed transfers, and switch it to an explicit error/queued-retry path so failed transfers are visible rather than masked. -- The success-shaped non-5xx responses kept the 5xx error-rate panel green and hid the outage from monitoring. [F10, F9]
- **[short_term]** Add a direct alert on payment_success_rate (and bank_api_error_rate) with a threshold well above the ~2.5% collapse, so a success-rate drop pages even when HTTP 5xx error_rate is flat. -- The existing error-rate-only panel is blind to outbound-dependency short-circuits; success rate is the metric that actually moved. [F9, F10, F8]
- **[short_term]** Verify the fix end-to-end after renewal: re-check bank_api_error_rate returns to ~0.2%, payment_success_rate to ~99%, and PENDING_RETRY log volume drops to zero over a full window (e.g. 30-60 minutes). -- Confirms the expiry was the sole mechanism and that no residual handshake failures remain. [F8, F9, F11]
- **[long_term]** Implement certificate lifecycle automation for payment-service: enforce alerting and auto-renewal well before notAfter (the cert-watcher fired only 25 minutes before expiry at 11:50Z), and gate renewals through change management so an unmanaged expiry cannot recur. -- No credential/secret change appeared in the change feeds and the 25-minute warning was not acted on; the gap is in renewal management, not in a deployment. [F12, F15]
- **[long_term]** Add a synthetic/external probe that exercises the full bank transfer path (including the mTLS handshake to bank-partner-api) and alerts on failure, since bank-partner-api exposes no telemetry and can only be observed through its callers. -- The partner has no directly observable metrics/logs, so detection must come from caller-side end-to-end checks. [F14, F19, F3]

## Data gaps
- bank-partner-api exposes no telemetry (type external, has_telemetry:false; a direct metrics query returned not_found), so the partner's own health and certificate state cannot be measured and can only be inferred through payment-service's client-side bank_api_error_rate.
- No queries were possible for network changes, feature-flag flips, or credential/secret rotations in 09:00-13:59Z; absence of such changes is unverified rather than proven. The mechanism by which the cert reached expiry without renewal is not visible in the change record.
- Could not query sibling/caller services (e.g. api-gateway) or bank-partner-api logs due to budget_exhausted, so the blast radius beyond payment-service is unknown and propagated errors/latency in callers are unverified.
- Did not retrieve timeout counts, queue/backlog depth, db_pool_in_use, cpu_pct/memory_pct, or restart counts before budget exhaustion; saturation/hang/backlog hypotheses (H3) are not fully ruled out by resource metrics, though flat request_rate and falling latency argue against them.
- Historical precedent search returned only 6 incidents (limit 6) and was not flagged truncated; other credential-expiry or config-change cases for payment-service may exist beyond the returned set.

## What would change this conclusion
- Evidence that the TLS 'expired certificate' alert referred to the bank-partner-api's server certificate rather than our client certificate, or that bank-partner-api was independently failing (e.g. partner-side telemetry showing 5xx or its own cert expiry), which would shift the conclusion toward H2.
- A credential/secret rotation into the bank connector appearing in the change feeds at or near 12:14-12:15Z, which would indicate an internal change mechanism rather than an unmanaged time-based expiry.
- Persistence of bank_api_error_rate at 100% and payment_success_rate at ~2.5% after the client certificate is renewed and reloaded — this would show the expired cert is not the (sole) cause and re-open external/dependency hypotheses.
- Resource telemetry (db_pool_in_use saturation, kafka lag, ledger-db latency spikes) temporally aligned with 12:14-12:15Z, which would support the shared-component/deployment hypothesis H3.
- Direct status-code evidence showing payment-service returned 5xx (rather than success-shaped responses) during the outage, which would weaken the masking explanation for the green error-rate panel.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: No internal change aligns tightly with the 12:14-12:15Z onset: cfg-2502 at 11:20

## Investigation stats
- latency 122.79s, LLM calls 12, tokens 75979 (in 59980 / out 15999)
- tool calls 20 (1 failed, 0 partial, 0 recovered by retry), findings 19/19 verified
