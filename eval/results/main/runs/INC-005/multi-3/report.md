# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause identified with medium confidence (0.65): payment-service's mTLS client certificate (CN=payments-client.orangeinvest.internal) for bank-partner-api expired at notAfter=2026-04-02T12:15:00Z — exactly the true onset minute — so every outbound call to the bank failed at the TLS handshake (171 "tls: expired certificate" errors), transfers were parked as PENDING_RETRY (140 log lines), and bank_api_error_rate went to a sustained 100%. The "green" dashboard is a measurement blind spot rather than evidence of health: the error-rate panel tracks only HTTP 5xx (flat 0.16–0.32%), while the business success metric collapsed from 99.68% to 2.18% in one minute and latency dropped (~330ms→~140ms) because calls now fail fast at the handshake. Alternatives are properly excluded: the only in-window change (cfg-2502, timeout 3000→5000ms at 11:20Z) precedes onset by 55 minutes and lengthens rather than truncates a handshake; the settlement-service v1.9.0 deploy precedes onset by 105 minutes and is off the request path; and shared dependencies (ledger-db, kafka) measure flat/normal with zero error logs. The one residual soft spot is that bank-side telemetry is permanently unavailable (has_telemetry=false), but the evidence shows outbound-only credential use and an org-managed cert precedent, so H2 (bank-side trust change) retains no positive evidence.

## Probable root cause
**Expired mTLS client certificate for payment-service -> bank-partner-api** -- credential_expiry in `payment-service (client certificate consumed against bank-partner-api)`, triggered by `null` (confidence 0.65, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired mTLS client certificate for payment-service -> bank-partner-api | credential_expiry | payment-service (client certificate consumed against bank-partner-api) | null | 0.65 (medium) | active |
| 2 | H2: Bank-partner-side trust/certificate change or degradation | dependency_failure | bank-partner-api (external PSP, owner=vendor) | null | 0.12 (low) | active |
| 3 | H6: Silent-failure / instrumentation blind spot as the primary fault (reporting defect) | unknown | payment-service observability/instrumentation | null | 0.08 (low) | active |
| 4 | H4: Shared dependency (ledger-db / kafka) failure | infrastructure_failure | ledger-db or kafka (shared) | null | 0.05 (low) | rejected |
| 5 | H3: bank.api.timeout_ms raise 3000->5000 (cfg-2502) | config_change | payment-service | cfg-2502 | 0.03 (low) | rejected |
| 6 | H5: settlement-service v1.9.0 deployment side-effect | bad_deployment | settlement-service | settlement-service v1.9.0 | 0.02 (low) | rejected |

### H1: Expired mTLS client certificate for payment-service -> bank-partner-api
payment-service's mTLS client certificate (CN=payments-client.orangeinvest.internal) reached notAfter=2026-04-02T12:15:00Z, exactly the true onset minute. Every outbound call to bank-partner-api then failed at the TLS handshake, transfers were parked as PENDING_RETRY, and because the failure surface only touches the external bank call path the HTTP 5xx error-rate panel stayed green while the business success metric collapsed.

**Causal chain**
1. payment-service's client cert exists and is used for outbound mTLS to bank-partner-api; the error text reports its notAfter [F9, F23]
2. The cert expires at notAfter=2026-04-02T12:15:00Z, with no renewal automation (org pattern from HIST-106) [F9, F18, F20]
3. payment_success_rate collapses 99.68% -> 2.18% in the 12:15 minute, i.e. onset is exactly the expiry minute [F8]
4. Repeated handshake rejections from 12:15:27Z; 171 'tls: expired certificate' + 140 'marked PENDING_RETRY: mTLS handshake ... rejected' lines [F9, F10]
5. All bank calls fail after expiry: bank_api_error_rate goes to sustained 100% while request_rate stays flat (~90-110 req/s) [F11]
6. Failures fast-fail at the handshake: p99 latency DROPS ~330ms -> ~140ms, so no latency alert fires [F12]
7. Failures are absorbed as PENDING_RETRY on the HTTP-200 path, so the 5xx-only error_rate panel stays flat at 0.16-0.32% [F7, F10]
8. No deploy or config change is temporally aligned with onset: 0 deploys and exactly 1 payment-service config key (cfg-2502) in the window, and that key is a timeout increase [F14, F15, F21, F22, F24]
9. No renewal/rotation artifact exists for the cert in 12:15-14:00Z, consistent with a missed rotation rather than a change event [F24, F19]

**Supporting evidence**
  - `F8` ✔ payment_success_rate collapsed from ~99% to ~2.5% in one minute: 12:14 = 99.68%, 12:15 = 2.18%, and stayed depressed for the rest of the window (12:52 = 2.14%). This is the true onset — the business-success metric broke exactly at 12:15:00Z while error_rate did not move.
  - `F9` ✔ The first error in logs is 12:15:27Z 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'; the diagnosing line appears at 12:16:44Z: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. The cert expiry timestamp equals the onset minute.
  - `F10` ✔ Error volume is large and sustained: 313 matched ERROR/WARN log lines for payment-service (311 ERROR) from 12:04:22Z to 13:58:39Z; dominant patterns are 'bank-partner-api call failed: ... tls: expired certificate' (171) and 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140).
  - `F11` ✔ bank_api_error_rate jumped from 0.2% (12:00) to 12.68% avg/max 100% (12:08) to a sustained 100% from 12:16 onward, confirming all calls to the external bank dependency failed after cert expiry; request_rate stayed flat (~90-110 req/s) so traffic was not reduced.
  - `F12` ✔ payment-service latency p99 DROPPED (not rose) after onset: 316-341ms before 12:15, then ~135-147ms for the rest of the window — consistent with calls failing fast at the TLS handshake rather than hanging, so a latency-based alert would also have missed this.
  - `F18` ✔ Historical precedents: HIST-106 (2025-06-30, settlement-service mTLS client certificate expired, 'no renewal automation', resolution 'Issued new cert; added expiry alert') is the closest mechanism match (credential_expiry), while HIST-105 (2025-11-19) and HIST-103 (2025-10-21) are partner-side dependency failures (5xx / API outage). None of the 5 returned incidents documents 'payment failures with a green error-rate dashboard', and none is a deploy-config change aligned to a payment onset.
  - `F20` ✔ The closest internal precedent to this CN's lifecycle is HIST-106 (2025-06-30, settlement-service mTLS certificate expired): root_cause 'Client certificate expired; no renewal automation', resolution 'Issued new cert; added expiry alert' — i.e. an org-issued, org-managed client certificate for an internal service.
  - `F22` ✔ In 2026-04-02T11:00-14:00Z and the whole 2026-03-01 onward period the ONLY payment-service config change is cfg-2502 bank.api.timeout_ms 3000->5000 at 11:20:00Z by p.nair ('Raise bank API timeout after partner advisory'); there is no cert-rotation, secret-bump, or trust-store config key anywhere, and no config change in the 12:15-14:00 secret/credential domain.
  - `F24` ✔ No renewal/rotation attempt for this CN was recorded in 12:15-14:00Z: deployments count 0 for payment-service and 1 (settlement-service, 10:30Z) globally, and config changes for all services in 11:00-14:00Z count 1 (cfg-2502 only) — so nothing in the incident window touched the certificate.
  - `F23` ✔ payment-service has no calls from bank-partner-api: the dependency graph is payment-service -> bank-partner-api (kind external, has_telemetry=false, owner vendor), with bank-partner-api shared=false and having no other callers, so the bank side cannot push a trust/credential change to our cert — the direction of credential use is outbound from payment-service only.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Credential lifecycle is not a field in deployments/config_changes, so expiry cannot be confirmed as a change artifact; it is inferred from runtime error text only (documented gap).
  - The CN 'payments-client.orangeinvest.internal' implies internally issued/managed, but no registry entry was retrievable (F19); external (partner-provisioned) issuance is not formally excluded.
  - Whether the bank side independently changed trust is unobservable (has_telemetry=false, F16/F23).

### H2: Bank-partner-side trust/certificate change or degradation
The bank/PSP partner changed its server-side trust store or rotated its own certificate at ~12:15, so the handshake between payment-service and bank-partner-api was rejected; the fault originates outside our perimeter and payment-service's own credential is healthy.

**Causal chain**
1. The bank side rotates/alters its certificate or trust config at ~12:15 *(assumed)*
2. The handshake is rejected and the transfer is parked [F9, F10]
3. All bank calls fail from 12:16 onward (bank_api_error_rate 100%) [F11]

**Supporting evidence**
  - `F6` ✘ unverified Historical precedent closest in signature to 'payment failures with external dependency involved': HIST-105 (2025-11-19, payment failures / bank api errors, bank partner API returned 5xx for 40 minutes, queued retries) and HIST-103 (external exchange API outage, upstream timeouts). Both are dependency_failure on a partner side. None of the five returned incidents documents a case where the payment-service error-rate dashboard stayed green.
  - `F16` ✔ The change is isolated to payment-service (one config key on payment-service, zero deploys everywhere), NOT shared. payment-service's dependencies: calls ledger-db (postgres, shared=true), bank-partner-api (external, shared=false, has_telemetry=false), produces to kafka (shared=true); is called by api-gateway. bank-partner-api has no other callers and no telemetry, so its health is only observable indirectly through payment-service.
**Contradicting evidence**
  - `F9` ✔ The first error in logs is 12:15:27Z 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'; the diagnosing line appears at 12:16:44Z: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. The cert expiry timestamp equals the onset minute.
  - `F10` ✔ Error volume is large and sustained: 313 matched ERROR/WARN log lines for payment-service (311 ERROR) from 12:04:22Z to 13:58:39Z; dominant patterns are 'bank-partner-api call failed: ... tls: expired certificate' (171) and 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140).
  - `F12` ✔ payment-service latency p99 DROPPED (not rose) after onset: 316-341ms before 12:15, then ~135-147ms for the rest of the window — consistent with calls failing fast at the TLS handshake rather than hanging, so a latency-based alert would also have missed this.
**Assumptions**
  - External telemetry is permanently unavailable (has_telemetry=false), so a partner-side change can be neither confirmed nor fully excluded.
  - The criterion the TLS client applies to reject is OUR cert's notAfter; if the rejection were trust-side the alert text would more typically surface the peer/issuer, not our notAfter matching the onset.
**Rejected because:** null

### H6: Silent-failure / instrumentation blind spot as the primary fault (reporting defect)
The reported symptom is really two separate facts: transfers fail AND the dashboard is green. An independent contributing cause is that payment-service's error-rate panel measures only HTTP 5xx while the bank call path fails on the HTTP-200/PENDING_RETRY branch, so the failure is structurally invisible; on its own this explains the green panel but not the transfer failures.

**Causal chain**
1. payment-service returns HTTP 200 on the retry-park path, so 5xx error_rate is blind to bank-path failures [F7, F10]
2. Latency also fails to flag it (p99 drops on fast handshake rejection) [F12]
3. Only the business metric payment_success_rate reveals the outage [F8]

**Supporting evidence**
  - `F7` ✔ payment-service HTTP error_rate (5xx) stayed flat and normal across the whole window, staying between ~0.16% and ~0.32% (mean 0.22, max 0.32) with no step at 12:15 — the 'green' panel reflects only HTTP 5xx.
  - `F8` ✔ payment_success_rate collapsed from ~99% to ~2.5% in one minute: 12:14 = 99.68%, 12:15 = 2.18%, and stayed depressed for the rest of the window (12:52 = 2.14%). This is the true onset — the business-success metric broke exactly at 12:15:00Z while error_rate did not move.
  - `F12` ✔ payment-service latency p99 DROPPED (not rose) after onset: 316-341ms before 12:15, then ~135-147ms for the rest of the window — consistent with calls failing fast at the TLS handshake rather than hanging, so a latency-based alert would also have missed this.
**Contradicting evidence**
  - `F8` ✔ payment_success_rate collapsed from ~99% to ~2.5% in one minute: 12:14 = 99.68%, 12:15 = 2.18%, and stayed depressed for the rest of the window (12:52 = 2.14%). This is the true onset — the business-success metric broke exactly at 12:15:00Z while error_rate did not move.
**Assumptions**
  - Treated as a co-condition of observability rather than the origin of the transfer failures; its existence does not explain why the cert expired.

### H4: Shared dependency (ledger-db / kafka) failure
A shared component (ledger-db with 4 callers, or kafka) degraded around 12:15, breaking payment-service and potentially neighbours, with payment-service noticed first.

**Causal chain**
1. Neighbour stress or internal failure degrades shared ledger-db/kafka *(assumed)*
2. payment-service bank path fails as a result *(assumed)*

**Supporting evidence**
  - `F2` ✔ ledger-db and kafka are marked shared=true and are used by multiple services; ledger-db has four callers: order-service, portfolio-service, payment-service and settlement-service.
**Contradicting evidence**
  - `F10` ✔ Error volume is large and sustained: 313 matched ERROR/WARN log lines for payment-service (311 ERROR) from 12:04:22Z to 13:58:39Z; dominant patterns are 'bank-partner-api call failed: ... tls: expired certificate' (171) and 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140).
  - `F11` ✔ bank_api_error_rate jumped from 0.2% (12:00) to 12.68% avg/max 100% (12:08) to a sustained 100% from 12:16 onward, confirming all calls to the external bank dependency failed after cert expiry; request_rate stayed flat (~90-110 req/s) so traffic was not reduced.
  - `F16` ✔ The change is isolated to payment-service (one config key on payment-service, zero deploys everywhere), NOT shared. payment-service's dependencies: calls ledger-db (postgres, shared=true), bank-partner-api (external, shared=false, has_telemetry=false), produces to kafka (shared=true); is called by api-gateway. bank-partner-api has no other callers and no telemetry, so its health is only observable indirectly through payment-service.
  - `F17` ✔ Shared components ledger-db (shared, 4 callers: order/portfolio/payment/settlement-service) and kafka (shared) show no associated deploy or config change in the window, so there is no change-based evidence that a neighbour damaged payment-service via a shared datastore/queue; the only shared-component-side event, settlement-service v1.9.0, is 105 min pre-onset and unrelated in function.
  - `F26` ✔ ledger-db connections_active is FLAT and normal across 12:00-13:55Z: min 55.32, max 69.24, mean 62.31, 0 missing points, 120/120 points present; the 12:15 bucket is avg 62.64 / max 66.88, indistinguishable from pre-onset buckets (12:10 avg 62.33, 12:00 avg 61.79). No connection-failure surge or pool exhaustion at the onset minute.
  - `F27` ✔ ledger-db slow_queries_per_min actually IMPROVED before and stayed low through onset: 12:00 avg 9.02 -> 12:10 avg 0.91 -> 12:15 avg 1.11 -> rest of window ~0.7-1.2 (mean 1.46, 120/120 points, 0 missing). There is no latency/query degradation step at 12:15:00Z; if anything the DB was quieter at onset.
  - `F29` ✔ kafka is healthy across 12:00-13:55Z: messages_in_per_s stayed flat at min 2332.2 / max 2784.18 / mean 2531.6 (120/120 points, 0 missing), with the 12:15 bucket at avg 2582.77 / max 2693.16 (no dip or spike at onset); and under_replicated_partitions is a hard 0.0 avg and 0.0 max for every one of the 24 buckets. No produce-side degradation or broker-partition loss at 12:15.
  - `F30` ✔ No error/warn telemetry exists from either shared infrastructure component or from api-gateway at all in 12:00-14:00Z: ledger-db returned total_matched 0 WARN+ lines and api-gateway returned total_matched 0 ERROR lines, each over an intact data_range 2026-04-02T08:00:00Z -> 2026-04-02T13:59:00Z with truncated=false. So ledger-db emitted no errors and api-gateway logged no errors during the entire incident window.
**Assumptions**
  - Absence of change events touching shared components is treated as weak evidence; direct health metrics are now available and are flat.
**Rejected because:** Now contradicted by measured shared-component health, not just absence of change events: ledger-db connections_active flat/normal (55-69) with no step at 12:15 and slow_queries improving (F26/F27); kafka messages_in_per_s flat (2332-2784) with under_replicated_partitions hard 0 (F29); ledger-db emitted 0 WARN+ lines and api-gateway 0 ERROR lines over an intact window (F30). The error signature is exclusively TLS-handshake-to-bank, with no DB/queue errors anywhere. A shared-dependency failure cannot produce a client-cert notAfter equal to the onset minute.

### H3: bank.api.timeout_ms raise 3000->5000 (cfg-2502)
The 11:20 config change cfg-2502 raising the bank API timeout to 5s altered bank-call behaviour and caused silent transfer failures.

**Causal chain**
1. cfg-2502 raises timeout 3000->5000 ms at 11:20 [F3, F14]
2. The longer timeout changes bank-call failure mode *(assumed)*
3. Transfers fail silently *(assumed)*

**Supporting evidence**
  - `F3` ✔ Exactly one config change in the incident window: payment-service bank.api.timeout_ms raised from 3000 to 5000 ms at 2026-04-02T11:20:00Z by p.nair ("Raise bank API timeout after partner advisory").
  - `F14` ✔ Exactly one config change exists across ALL services in 11:00-14:00Z: cfg-2502, payment-service bank.api.timeout_ms raised 3000->5000 ms at 2026-04-02T11:20:00Z by p.nair ('Raise bank API timeout after partner advisory'). No config change occurred in the 08:00-11:00Z window (count 0), so this is the only change preceding onset with any proximity to it.
**Contradicting evidence**
  - `F15` ✔ The only change plausibly coincident with onset is the timeout raise, but it precedes the 12:15 onset by 55 minutes and is a 3s->5s increase, which would lengthen rather than truncate a handshake; the observed failure is a TLS 'expired certificate' handshake rejection (notAfter=2026-04-02T12:15:00Z) whose expiry timestamp exactly equals the onset minute. No deploy/config event is temporally aligned with onset.
  - `F5` ✔ The only change directly affecting payment-service in the window precedes the reported incident (~12:17 onset) by about an hour: a bank API timeout increase from 3s to 5s, described as a response to a partner advisory.
  - `F9` ✔ The first error in logs is 12:15:27Z 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'; the diagnosing line appears at 12:16:44Z: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. The cert expiry timestamp equals the onset minute.
  - `F12` ✔ payment-service latency p99 DROPPED (not rose) after onset: 316-341ms before 12:15, then ~135-147ms for the rest of the window — consistent with calls failing fast at the TLS handshake rather than hanging, so a latency-based alert would also have missed this.
**Assumptions**
  - None verified; rests on temporal proximity alone.
**Rejected because:** Precedes onset by 55 minutes (no quiet-gap alignment), is a timeout INCREASE which lengthens rather than truncates a handshake, and cannot produce a TLS alert carrying our cert's notAfter know, exactly equal to the onset minute; the fast-fail latency DROP (F12) contradicts a timeout-driven mechanism.

### H5: settlement-service v1.9.0 deployment side-effect
The 10:30 settlement-service batch-CSV-exporter deploy introduced a fault that reduced payment throughput without HTTP 5xx.

**Causal chain**
1. settlement v1.9.0 deploys 10:30, not rolled back [F4, F13]
2. It interferes with the payment path *(assumed)*
3. Transfers fail *(assumed)*

**Supporting evidence**
  - `F4` ✔ No deployments at all in 2026-04-02T11:00-14:00 (count 0), and only one config change in 2026-04-02T09:00-11:00 window was none (count 0). The single deployment on 2026-04-02 before 11:00 was settlement-service v1.9.0 at 10:30:00Z (batch CSV exporter), status live, not rolled back.
  - `F13` ✔ No deployments occurred on any service in 2026-04-02T11:00-14:00Z (count 0); the only deployment that day was settlement-service v1.9.0 at 10:30:00Z ('Batch settlement report export', status live, rolled_back_at null) — 105 minutes before the 12:15 true onset, and settlement-service is not on payment-service's request path.
**Contradicting evidence**
  - `F13` ✔ No deployments occurred on any service in 2026-04-02T11:00-14:00Z (count 0); the only deployment that day was settlement-service v1.9.0 at 10:30:00Z ('Batch settlement report export', status live, rolled_back_at null) — 105 minutes before the 12:15 true onset, and settlement-service is not on payment-service's request path.
  - `F17` ✔ Shared components ledger-db (shared, 4 callers: order/portfolio/payment/settlement-service) and kafka (shared) show no associated deploy or config change in the window, so there is no change-based evidence that a neighbour damaged payment-service via a shared datastore/queue; the only shared-component-side event, settlement-service v1.9.0, is 105 min pre-onset and unrelated in function.
  - `F21` ✔ Across the entire 2026-04-01 to 2026-04-02 window the only deployment on any service was settlement-service v1.9.0 ('Batch settlement report export') at 10:30:00Z; payment-service itself has zero deployments in the whole period from 2026-03-01 onward (count 0).
**Assumptions**
  - None verified.
**Rejected because:** Precedes onset by 105 minutes, settlement-service is not on payment-service's request path, it is the only deployment and is live/not rolled back, and it cannot explain a TLS client-cert expiry aligned to the exact onset minute.

## Facts (verified observations)
- payment-service's true business-metric onset was 2026-04-02T12:15:00Z: payment_success_rate fell from 99.68% (12:14) to 2.18% (12:15) in one minute and stayed depressed (2.14% at 12:52), while HTTP error_rate stayed flat between ~0.16% and ~0.32%. [F7, F8]
- The first error is 12:15:27Z 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected', and at 12:16:44Z the diagnostic line reads 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' — the cert expiry timestamp equals the onset minute. [F9]
- Error volume is large and sustained: 313 matched ERROR/WARN lines for payment-service (311 ERROR) from 12:04:22Z to 13:58:39Z, dominated by 'bank-partner-api call failed: ... tls: expired certificate' (171) and 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140). [F10]
- bank_api_error_rate jumped from 0.2% (12:00) to 12.68% avg/max 100% (12:08) and sustained 100% from 12:16 onward, with request_rate flat (~90-110 req/s), confirming all calls to the external bank dependency failed after cert expiry without traffic reduction. [F11]
- Only one config change exists across all services in the window (cfg-2502): payment-service bank.api.timeout_ms raised 3000->5000 ms at 2026-04-02T11:20:00Z by p.nair ('Raise bank API timeout after partner advisory'); it precedes onset by 55 minutes and is a timeout increase, which would lengthen rather than truncate a handshake, and cannot produce a TLS alert carrying our cert's notAfter. [F3, F14]
- No deployments occurred on any service in 2026-04-02T11:00-14:00Z (count 0); the only deployment that day was settlement-service v1.9.0 ('Batch settlement report export') at 10:30:00Z, status live and not rolled back — 105 minutes pre-onset and off payment-service's request path. payment-service has zero deployments in the whole period from 2026-03-01 onward. [F4, F13, F21]
- No cert-rotation, secret-bump, or trust-store config key exists anywhere in the observed period, and no renewal/rotation attempt for this CN was recorded in 12:15-14:00Z (0 payment-service deployments; 1 config change total = cfg-2502 only) — nothing in the incident window touched the certificate. [F22, F24]
- Shared dependencies measured healthy: ledger-db connections_active flat and normal (min 55.32, max 69.24, mean 62.31; 12:15 bucket avg 62.64) with slow_queries_per_min flat-to-improving (12:00 avg 9.02 -> 12:10 avg 0.91 -> 12:15 avg 1.11); kafka messages_in_per_s flat (min 2332.2, max 2784.18, mean 2531.6) with under_replicated_partitions a hard 0.0 across all 24 buckets. [F26, F27, F29]
- No error/warn telemetry exists from shared infrastructure or api-gateway in 12:00-14:00Z: ledger-db returned 0 WARN+ lines and api-gateway 0 ERROR lines, each over an intact non-truncated data range from 2026-04-02T08:00:00Z to 13:59:00Z. [F30]
- Credential use is outbound-only: the dependency graph is payment-service -> bank-partner-api (external, has_telemetry=false, owner=vendor, shared=false, no other callers), so the bank side cannot push a trust/credential change to our cert. [F23, F16]
- The change is isolated to payment-service (one config key on payment-service, zero deploys everywhere) and not shared; ledger-db and kafka are shared and show no associated deploy or config change in the window beyond the unrelated settlement-service v1.9.0 event 105 minutes pre-onset. [F16]
- The closest internal precedent for this mechanism is HIST-106 (2025-06-30, settlement-service mTLS certificate expired): root cause 'Client certificate expired; no renewal automation', resolution 'Issued new cert; added expiry alert' — i.e. an org-issued, org-managed client certificate for an internal service. [F20]
- No certificate/credential registry, secret-store entry, or renewal record for CN=payments-client.orangeinvest.internal was retrievable from any tool; searching incidents for CN/secret-store/ownership terms returned only HIST-106 and HIST-108, not a registry or ownership record. [F19]
- Structural telemetry limits: ledger-db exposes no write_errors/read_errors metric (available: connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min) and kafka exposes no error_rate metric (available: cpu_pct, messages_in_per_s, under_replicated_partitions), so component health could only be asserted via proxies. [F25, F28]

## Assumptions (not verified)
- Assumed the CN=payments-client.orangeinvest.internal certificate is internally issued and org-managed rather than partner-provisioned. Credential lifecycle is not a field in deployments/config_changes, so expiry cannot be confirmed as a change artifact and ownership is inferred from the runtime error text plus the org-run HIST-106 precedent (F19/F20) rather than a verbatim registry record.
- Assumed the cert expiry was the result of a missed renewal (operational) rather than any change event, inferred from the total absence of deploy/config events aligned to onset (F22/F24/F15).
- Assumed that 'impact confined to payment-service's bank-dependent path' follows from the structural dependency edges (bank-partner-api's only caller) plus flat measured shared-component health and zero api-gateway errors, since neighbour-service metric values at 12:15 were not retrieved.
- Assumed that payment-service's non-bank paths (ledger writes, kafka produces) are unaffected, inferred from flat ledger-db/kafka health and the bank-only error signature rather than direct payment-service-side non-bank-path metrics.
- Assumed the external bank's own trust store / CRL state did not change; bank-partner-api telemetry is permanently unavailable (has_telemetry=false), so a partner-side change can be neither confirmed nor formally excluded — only argued against from the outbound-only credential direction (F23).
- (demoted from facts: no verified observation backs it) payment-service latency p99 DROPPED after onset (316-341ms before 12:15 to ~135-147ms after), consistent with fast-fail TLS handshake rejection rather than hanging — so a latency-based alert would also have missed this.

## Timeline
- `2026-04-02T10:30:00Z` Only deployment of the day: settlement-service v1.9.0 ('Batch settlement report export'), status live, not rolled back — 105 minutes before true onset and not on payment-service's request path. [F4, F13, F21]
- `2026-04-02T11:20:00Z` Only in-window config change: cfg-2502 raises payment-service bank.api.timeout_ms from 3000 to 5000 ms ('Raise bank API timeout after partner advisory') — precedes onset by 55 minutes. [F3, F14, F22]
- `2026-04-02T12:00:00Z` Pre-onset baselines: payment-service HTTP 5xx error_rate flat ~0.16-0.32%; bank_api_error_rate 0.2%; ledger-db connections_active avg 61.79 and slow_queries 9.02; kafka throughput normal. [F7, F11, F26, F27, F29]
- `2026-04-02T12:14:00Z` Last healthy business-metric minute: payment_success_rate 99.68%. [F8]
- `2026-04-02T12:15:00Z` credential expiry: the mTLS client certificate CN=payments-client.orangeinvest.internal reaches notAfter=2026-04-02T12:15:00Z. In the same minute payment_success_rate collapses to 2.18% — the true onset. Shared-component metrics in this bucket remain normal (ledger-db connections 62.64, slow_queries 1.11; kafka 2582.77 msg/s, 0 under-replicated partitions). [F8, F9, F26, F27, F29]
- `2026-04-02T12:15:27Z` First error in logs: 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected'. [F9, F10]
- `2026-04-02T12:16:00Z` bank_api_error_rate reaches a sustained 100% while request_rate stays flat (~90-110 req/s); payment-service latency p99 drops from 316-341ms to ~135-147ms (fast-fail handshake). [F11, F12]
- `2026-04-02T12:16:44Z` Diagnostic log line pinning the cause: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. [F9]
- `2026-04-02T12:32:00Z` Incident INC-005 [SEV-2] reported by support lead: ~40 tickets about failed fund transfers while the payment-service HTTP error-rate panel shows nothing unusual. [F1]
- `2026-04-02T13:58:39Z` Sustained failure through end of window: 313 matched ERROR/WARN lines (311 ERROR) with 171 'tls: expired certificate' and 140 PENDING_RETRY entries; HTTP 5xx error_rate never moved. [F10, F7]
- `2026-04-02T13:59:00Z` Latest data: no renewal/rotation attempt recorded for this CN anywhere in the incident window; shared components still measure healthy with zero error lines from ledger-db and api-gateway. [F24, F26, F29, F30]

## Recommended actions
- **[immediate]** IMMEDIATE: Rotate/reissue the mTLS client certificate for CN=payments-client.orangeinvest.internal against bank-partner-api (renew with the issuing CA per the HIST-106 runbook) and reload it into payment-service, then drain the PENDING_RETRY backlog. -- The cert reached notAfter=2026-04-02T12:15:00Z, exactly the onset minute; every handshake to bank-partner-api is rejected with 'tls: expired certificate', bank_api_error_rate is a sustained 100%, and ~140 transfers are parked as PENDING_RETRY. Refresh+reload is the direct mitigation; retries cannot succeed until a valid cert is presented. [F9, F10, F11, F20]
- **[immediate]** IMMEDIATE: Repoint or add alerting on the business-success metric (payment_success_rate) and on bank_api_error_rate — do not rely on the payment-service HTTP 5xx error-rate panel during this incident. -- The HTTP 5xx panel stayed flat at 0.16-0.32% for the entire outage and latency p99 dropped (330ms->140ms), so both the error-rate and any latency alert are structurally blind to this failure mode; only payment_success_rate (99.68%->2.18%) and bank_api_error_rate (100%) captured it. Incident detection depended on customer tickets. [F7, F8, F11, F12]
- **[short_term]** SHORT_TERM: After rotation, verify recovery explicitly: confirm bank_api_error_rate returns to ~0.2%, payment_success_rate returns to ~99%, the PENDING_RETRY queue drains, and 'tls: expired certificate' lines stop appearing in payment-service logs. -- These are the exact metrics that moved at onset, so they are the correct verification set; the fix is only proven when the business-success metric and bank error rate both normalize and no new handshake rejections appear. [F8, F9, F10, F11]
- **[short_term]** SHORT_TERM: Add automated certificate-expiry monitoring with alerting at a defined lead time (e.g. 30/14/7 days) for all service client certificates, starting with CN=payments-client.orangeinvest.internal, and test the renewal path in staging. -- HIST-106 (the closest internal precedent, same class of org-managed mTLS client cert) had root cause 'Client certificate expired; no renewal automation' and was resolved with 'Issued new cert; added expiry alert' — the same remediation was evidently not generalized to this CN, allowing a recurrence. [F18, F19, F20, F22]
- **[short_term]** SHORT_TERM: Register every service client certificate and its owner/renewal process in a credential registry (CN, issuer, notAfter, owner, renewal automation), since none currently exists and no renewal/rotation record was retrievable for this CN. -- Credential lifecycle is not a field in deployments/config_changes and no cert-store/secret-rotation endpoint or ownership record was available, which is exactly why ownership had to be inferred from precedent rather than read verbatim. Closing this gap converts future expiry events from inference into signal. [F19, F22, F24]
- **[short_term]** SHORT_TERM: Invite bank-partner-api (vendor) to confirm no concurrent server-side trust/rotation change on their side during 12:15-14:00Z, and record their statement in the incident. -- bank-partner-api has_telemetry=false and owner=vendor, so their view is the only way to close out H2. Current evidence argues against it (credential use is outbound-only, no push path) but cannot positively exclude it from our telemetry. [F16, F23, F6]
- **[long_term]** LONG_TERM: Instrument the external-dependency call path end-to-end, including bank_api_error_rate and payment_success_rate as first-class SLOs and alerting on the HTTP-200/PENDING_RETRY branch so silent failures are not invisible. -- F7/F8 show the failure occurred entirely on a branch the error-rate panel does not measure; making the business-success and dependency-error signals alertable prevents a repeat of a SEV-2 detected only via ~40 customer tickets. [F7, F8, F11, F12]
- **[long_term]** LONG_TERM: Add a pre-expiry handshake canary against bank-partner-api that exercises the real mTLS path and fails loudly before notAfter, and reconsider the 5s bank timeout (cfg-2502) once the real failure mode is fixed. -- A canary would have caught this before the expiry minute, and preventive monitoring on the actual TLS path is stronger than static expiry alerts alone. cfg-2502 is not the cause (55 min pre-onset, timeout increase) but remains an unrelated change worth revisiting with normal change control. [F9, F10, F12, F3, F15]
- **[long_term]** LONG_TERM: Ensure TLS client code surfaces peer/issuer identity (not only our cert's notAfter) in handshake-rejection errors, so future incidents can discriminate client-cert expiry from peer trust rejection without ambiguity. -- The surviving discriminator between H1 and H2 is that the alert text reports OUR cert's notAfter; richer TLS error context would make the client-vs-peer attribution unambiguous in future occurrences. [F9, F23]

## Data gaps
- No certificate/credential registry, secret-store entry, or renewal record for CN=payments-client.orangeinvest.internal was retrievable from any tool (F19), so cert issuance entity (internal CA vs partner-provisioned) and ownership are inferred from the HIST-106 org-managed precedent rather than read verbatim.
- bank-partner-api has_telemetry=false, owner=vendor: no metrics, logs, deployments, config, or certificate-lifecycle data exist for the bank side, so a partner-side trust/rotation change (H2) can be neither confirmed nor fully excluded from our telemetry.
- Neighbour-service metrics at 12:15 (api-gateway, order-service, portfolio-service, settlement-service error/latency/success) were never retrieved (queries returned budget_exhausted), so the 'impact confined to payment-service's bank path' statement rests on structural dependency edges plus flat shared-component health and 0 api-gateway error lines, not on measured neighbour values.
- ledger-db has no write_errors/read_errors metric and kafka no error_rate metric (F25/F28), so shared-component health is asserted only via proxies (connections_active, slow_queries_per_min, messages_in_per_s, under_replicated_partitions) and absence of log errors — literal write/produce error counts are unavailable.
- payment-service's non-bank internal path metrics (ledger-write latency/success, kafka-produce success, timeout/retry counters) were not enumerated or queried, so the claim that non-bank paths are unaffected is inferred from flat downstream health and the bank-only error signature.
- No historical incident documents the exact 'payment failures with a green error-rate dashboard' signature (the targeted search exhausted the tool budget), so the dashboard blind-spot pattern is not corroborated by precedent for this system; HIST-105/HIST-103 are mechanism-level leads only.
- A cert rotation at ~12:15 cannot be confirmed as a change event because certificate lifecycle is not a field in deployments or config_changes — the expiry is visible only indirectly via the runtime 'expired certificate' error text.

## What would change this conclusion
- Bank-side evidence (vendor statement, partner logs, or an observed concurrent rotation on bank-partner-api at ~12:15) showing the rejection originated from a partner-side trust-store/certificate change would shift the conclusion toward H2 and away from our expired client cert.
- TLS error text or a protected trace showing the handshake rejected on peer/issuer trust criteria rather than our cert's notAfter would contradict H1's mechanism.
- Discovery that CN=payments-client.orangeinvest.internal is partner-provisioned external issuance (e.g. a bank-issued cert without an internal renewal record) would move ownership to the vendor boundary and change the remediation path, though it would not change the immediate rotation mitigation.
- Evidence of a renewal/rotation attempt for this CN inside 12:15-14:00Z (a deployment or config event touching the credential) would contradict the 'missed renewal' interpretation and point to a failed renewal as a distinct root cause.
- A measured anomaly at 12:15 in a shared dependency or neighbour service — e.g. ledger-db connection failures or write errors, kafka produce errors or partition loss, or api-gateway/order/portfolio/settlement success-metric collapse — would reopen H4 and raise the possibility of a shared-dependency contributor to the transfer failures.
- Demonstration that cfg-2502 (the 3000->5000ms timeout raise) actually truncated the handshake, or alignment of any deploy/config event with the 12:15 onset minute, would revive H3 or H5.
- A fix that rotates/reloads the certificate and does NOT restore payment_success_rate and bank_api_error_rate would falsify H1 and require re-investigation.

## Integrity warnings (set by code)
- fact demoted to assumption: payment-service latency p99 DROPPED after onset (316-341ms before 12:15 to ~135-

## Investigation stats
- latency 151.73s, LLM calls 23, tokens 159539 (in 130404 / out 29135)
- tool calls 39 (3 failed, 0 partial, 0 recovered by retry), findings 29/30 verified
