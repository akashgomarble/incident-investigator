# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The payment failures reported by customers are caused by an expired mTLS client certificate on the payment-service -> bank-partner-api hop (H1, confidence 0.68): the certificate presented by payment-service reached notAfter=2026-04-02T12:15:00Z, and from the first outbound call at 12:15:27Z every bank call is rejected at the TLS handshake, driving bank_api_error_rate to exactly 100% and payment_success_rate from ~99% to ~2.5% while the 5xx error_rate panel stays flat (failures are returned as PENDING_RETRY, not errors). The failure mode is unambiguous — decomposition shows 0 timeout and 0 5xx signatures and only TLS-handshake/expired-certificate errors — which rules out the timeout config change (H3) and the partner-5xx/degradation reading (H2), and ledger-db, db_pool, kafka-facing and api-gateway signals are all flat at onset, ruling out H4/H5. Two premises remain unverifiable and cap confidence: whether CN=payments-client.orangeinvest.internal is our own credential or partner-issued (no cert/secret store is queryable — the root-cause statement should therefore not claim ownership), and whether the ~40 support tickets map to this exact failure (no ticket telemetry exists). The defect and its impact are identical under either ownership reading, and the crit

## Probable root cause
**Expired client mTLS certificate on the payment-service -> bank-partner-api hop** -- credential_expiry in `payment-service` (confidence 0.68, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired client mTLS certificate on the payment-service -> bank-partner-api hop | credential_expiry | payment-service | - | 0.68 (medium) | active |
| 2 | H2: Bank partner came to reject our mTLS handshake (partner-side certificate/trust failure) — reporter's external-boundary suspicion, refined | dependency_failure | bank-partner-api | - | 0.16 (low) | active |
| 3 | H3: bank.api.timeout_ms config change (3000->5000) at 11:20Z caused the failures | config_change | payment-service | cfg-2502 (bank.api.timeout_ms 3000->5000 @ 2026-04-02T11:20:00Z) | 0.03 (low) | rejected |
| 4 | H4: settlement-service v1.9.0 (10:30Z) co-tenant effect via shared ledger-db or shared kafka backlog | bad_deployment | settlement-service | settlement-service v1.9.0 @ 2026-04-02T10:30:00Z | 0.03 (low) | rejected |
| 5 | H5: Ledger-db / DB connection-pool exhaustion on the transfer path | resource_exhaustion | ledger-db | - | 0.01 (low) | rejected |

### H1: Expired client mTLS certificate on the payment-service -> bank-partner-api hop
The mTLS client certificate presented by payment-service on calls to bank-partner-api (CN=payments-client.orangeinvest.internal, notAfter=2026-04-02T12:15:00Z) became invalid at 12:15:00Z. From the next outbound call at 12:15:27Z every bank call is rejected at the TLS handshake ('remote error: tls: expired certificate (alert 45)'), so transfers are marked PENDING_RETRY and returned as non-error responses — payment_success_rate collapses to ~2.5%, bank_api_error_rate hits exactly 100%, latency falls (fast handshake reject), while the 5xx error_rate panel stays flat. Framing caveat per critique: whether this cert is minted/rotated BY us or is a partner-issued credential is NOT verified; the defect is an expired credential on the handshake, and the failure mechanism/impact is the same either way.

**Causal chain**
1. The client certificate used by payment-service to call bank-partner-api reaches notAfter=2026-04-02T12:15:00Z; a cert-watcher already warned 'expires in 25 minutes' at 11:50:26Z [F7, F30]
2. The next outbound call at 12:15:27Z is rejected: 'mTLS handshake with bank-partner-api rejected' / 'remote error: tls: expired certificate (alert 45)', with no timeout or 5xx signature anywhere [F7, F27, F28]
3. All outbound bank calls now fail, so bank_api_error_rate steps from ~0.2% to exactly 100% and payment_success_rate collapses ~97 points, both first deviating in the 12:15 bucket [F9, F8, F25, F29]
4. Failures are caught and recorded as 'marked PENDING_RETRY' (non-error responses), so the 5xx error_rate panel never moves and latency_p99 DROPS as handshakes fail fast [F12, F10, F11, F20]
5. The failure is continuous and customer-scale through the window: ~140 PENDING_RETRY entries at ~1.4/min from 12:15:27Z to 13:57:46Z, consistent with the ~40 tickets reported in the last 15 minutes at 12:32Z [F20, F21, F22]
6. The reporter's symptom (payment failures, green error dashboard) is the direct consequence of steps 2-5 on the transfer route *(assumed)*

**Supporting evidence**
  - `F7` ✔ payment-service received ERROR logs on the transfer path starting 2026-04-02T12:15:27Z: 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected', then 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. 313 ERROR-level entries matched, first 12:15:27Z, last 13:58:39Z.
  - `F8` ✔ The metric that shows the failure is payment_success_rate, which collapses from ~99% (avg 98.62-99.81 across 11:45-12:09) to ~2.5% at the 12:15 bucket (avg 2.51, max 2.99) and stays ~2-2.8% through 13:57 — a sustained ~97 point drop coincident with the 12:15:27Z log onset.
  - `F9` ✔ bank_api_error_rate on payment-service jumps from ~0.2% (flat 11:45-12:09) to exactly 100.0 for every bucket from 12:15 onward through 13:57, confirming the bank call path is entirely failing.
  - `F10` ✔ The aggregate HTTP error_rate panel (5xx % of requests) stays flat and low throughout, between 0.17 and 0.32% (max 0.32) across all buckets 11:45-13:57, with no change at 12:15 — confirming the incident is invisible to the 5xx error-rate panel even though transfers are failing 100%.
  - `F11` ✔ latency_p99_ms on payment-service DROPS rather than rises at onset — from ~318-341ms (11:45-12:09) to ~135-150ms (12:15-13:57) — consistent with bank calls failing fast (TLS handshake reject) instead of completing, so latency signals cannot be used to detect this incident.
  - `F12` ✔ Failures on the transfer path are logged at ERROR level (not silent) with two dominant signature patterns, but the app classifies them as successful responses ('marked PENDING_RETRY'), which explains why the 5xx error_rate panel stays green.
  - `F18` ✔ payment-service exposes only 9 metrics (bank_api_error_rate, cpu_pct, db_pool_in_use, error_rate, latency_p99_ms, memory_pct, payment_success_rate, request_rate, restarts): there is NO timeout_rate, NO connection_errors, and NO DNS/TLS metric, so the outbound bank-rail hop can only be characterised through bank_api_error_rate (already 100%) and the mTLS/expired-certificate log signature — granular timeout vs TLS-failure breakdown is unavailable.
  - `F20` ✔ The failing transfer path emits exactly one dominant ERROR signature, 140 matches of 'payment <n> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' between first_match 2026-04-02T12:15:27Z and last_match 2026-04-02T13:57:46Z (truncated=true, so 140 is a floor), each carrying a distinct payment id (e.g. 45f233b6 at 12:15:27Z, 7b4c31a9 at 12:16:13Z, 03ba104b at 12:17:57Z). This is the 'pending retry' state referenced in the task, produced continuously through the window.
  - `F21` ✔ PENDING_RETRY failures are frequent and customer-scale in the ticket window: 140 recorded in the trailing ~102 minutes from 12:15:27Z, i.e. roughly 1.4 per minute sustained, and they continue well past the 12:32Z report (last at 13:57:46Z) — consistent with ~40 support tickets in the last ~15 minutes at report time rather than a short burst.
  - `F22` ✔ The failing route traffic volume is unchanged: payment-service request_rate is flat at ~90-110 req/s across 12:00-13:59 (bucket avgs 98.48-101.67, overall mean 100.62, missing_points 0), so requests keep arriving and are failing on the bank leg rather than the endpoint being called less.
  - `F25` ✔ payment_success_rate and bank_api_error_rate both stay at baseline through the 12:14 bucket and step to failure in the 12:15 bucket, i.e. onset is between 12:14 and 12:15Z. In 60s buckets: payment_success_rate = 99.68 (12:14) then 2.18 (12:15), 2.99 (12:16); bank_api_error_rate = 0.21 (12:14) then exactly 100.0 for every bucket 12:15-12:30. Neither series shows any change at 11:20Z (success 98.76 avg at 11:20 bucket; bank error 0.2) nor at 10:30Z.
  - `F27` ✔ Failure-mode decomposition of bank_api_error_rate=100%: in 2026-04-02T11:00:00Z-12:30:00Z payment-service has 44 ERROR entries, all in exactly two patterns — 'mTLS handshake with bank-partner-api rejected' (21) first seen 12:15:27Z and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (23). There is no timeout or 5xx signature among them.
  - `F28` ✔ Timeout-mode contribution is exactly zero: a log search on 'timeout' for payment-service over 2026-04-02T10:00:00Z-13:59:00Z matches 0 entries (total_matched 0, truncated false), and a search on '5xx' over the same window also matches 0 entries. Neither raised-timeout symptoms (expected if the 3000->5000ms change at 11:20Z were the cause) nor partner 5xx rejections are present, so the 100% bank_api_error_rate is TLS-handshake rejection, not timeouts and not partner 5xx.
  - `F29` ✔ The first deviating bucket (12:15) is preceded by a flat pre-onset baseline in finer data, so the onset is NOT an artifact of post-onset-only data: 12:08-12:14 buckets exist and read 99.21/98.47/98.63/99.22/100.0/99.52/99.68 (success) and 0.19-0.22 (bank error), and the 240s series extends back to 10:00Z with success 98.65-99.73 and bank error 0.18-0.22 for every bucket 10:00-12:08.
  - `F30` ✔ On the pre-onset transfer path the only bank-related warnings are benign: 3 WARN entries in 11:15-12:15Z, two 'retrying idempotent request (attempt 2) after connection reset' at 11:24:11Z and 12:04:22Z, and one 'cert-watcher: client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)' at 11:50:26Z. No elevated bank-error or timeout warnings appear between the 11:20Z config change and the 12:15Z collapse.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The notAfter=2026-04-02T12:15:00Z certificate is a credential on the payment-service call path (client-side of our identity as logged); whether we issue/rotate it or the partner does is UNVERIFIED — no cert/secret store was queryable. H1's framing must not claim ownership; H1 and H2 differ mainly on this unresolved premise.
  - The ~40 support tickets map to this exact bank-call failure rather than a distinct client-app issue — the ticket system has no telemetry (F19), so the link rests on matching rate/timing, not ticket content.

### H2: Bank partner came to reject our mTLS handshake (partner-side certificate/trust failure) — reporter's external-boundary suspicion, refined
The failure sits on the bank partner side: because bank-partner-api has no telemetry, a partner-side certificate/trust change (e.g. their CA rotating or the credentials they accept for CN=payments-client... changing) would surface to us exactly as a TLS handshake rejection and 100% bank_api_error_rate, with payment-service degrading gracefully into PENDING_RETRY — the same symptom shape as HIST-105. This differs from the reporter's raw 'bank outage' framing only in mechanism (handshake/cert trust, not 5xx/timeout).

**Causal chain**
1. bank-partner-api (external, has_telemetry=false) begins rejecting our mTLS handshake or otherwise failing around 12:15Z — unobservable directly *(assumed)*
2. payment-service outbound bank calls fail, driving bank_api_error_rate to exactly 100% and payment_success_rate to ~2.5% [F9, F8]
3. Graceful degradation marks transfers PENDING_RETRY, keeping the 5xx error_rate green while success collapses [F12, F10]
4. Precedent HIST-105 (2025-11-19, bank partner 5xx for 40 min) shows this symptom shape from a partner-side cause [F6, F34]

**Supporting evidence**
  - `F3` ✔ payment-service dependency graph: outbound edges are payment-service -> ledger-db (kind db), payment-service -> bank-partner-api (kind external, has_telemetry=false), payment-service -> kafka (kind queue-produce); inbound edge api-gateway -> payment-service (http). The external bank rail adapter has NO telemetry.
  - `F9` ✔ bank_api_error_rate on payment-service jumps from ~0.2% (flat 11:45-12:09) to exactly 100.0 for every bucket from 12:15 onward through 13:57, confirming the bank call path is entirely failing.
  - `F6` ✔ Historical precedent for the "payment failures with normal/failed dashboard" shape: HIST-105 (2025-11-19) 'Payment failures: bank partner API outage' on payment-service — root cause 'Bank partner API returned 5xx for 40 minutes (their incident)', category dependency_failure, resolution 'Waited for partner; queued retries'. This is a lead, not evidence about the current incident.
  - `F34` ✔ HIST-105 (2025-11-19) 'Payment failures: bank partner API outage' on payment-service is a dependency_failure on the partner side, i.e. the same symptom (payment failures, bank api errors) with a different, external cause - which is why an expired-our-own-certificate cause in 2026 cannot be inferred from symptom similarity alone.
**Contradicting evidence**
  - `F7` ✔ payment-service received ERROR logs on the transfer path starting 2026-04-02T12:15:27Z: 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected', then 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. 313 ERROR-level entries matched, first 12:15:27Z, last 13:58:39Z.
  - `F11` ✔ latency_p99_ms on payment-service DROPS rather than rises at onset — from ~318-341ms (11:45-12:09) to ~135-150ms (12:15-13:57) — consistent with bank calls failing fast (TLS handshake reject) instead of completing, so latency signals cannot be used to detect this incident.
  - `F27` ✔ Failure-mode decomposition of bank_api_error_rate=100%: in 2026-04-02T11:00:00Z-12:30:00Z payment-service has 44 ERROR entries, all in exactly two patterns — 'mTLS handshake with bank-partner-api rejected' (21) first seen 12:15:27Z and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (23). There is no timeout or 5xx signature among them.
  - `F28` ✔ Timeout-mode contribution is exactly zero: a log search on 'timeout' for payment-service over 2026-04-02T10:00:00Z-13:59:00Z matches 0 entries (total_matched 0, truncated false), and a search on '5xx' over the same window also matches 0 entries. Neither raised-timeout symptoms (expected if the 3000->5000ms change at 11:20Z were the cause) nor partner 5xx rejections are present, so the 100% bank_api_error_rate is TLS-handshake rejection, not timeouts and not partner 5xx.
**Assumptions**
  - The 'expired certificate (alert 45)' is generated by our client rejecting the server/its own expired cert, which is what alert 45 typically means — this makes a pure partner-side cause hard to sustain
  - A partner-side change would produce a TLS cert-expiry alert rather than timeouts/5xx (not the usual signature of a partner degradation)

### H3: bank.api.timeout_ms config change (3000->5000) at 11:20Z caused the failures
The only config change in the window — payment-service bank.api.timeout_ms 3000->5000 ms at 11:20Z — introduced the bank-call failures with a delayed 12:15Z onset.

**Causal chain**
1. Config change raises bank API timeout 3000->5000 ms on payment-service at 11:20Z [F2, F31]
2. The changed timeout causes bank calls to fail/hang, producing transfer failures *(assumed)*
3. Failures surface as bank_api_error_rate 100% and payment_success_rate collapse [F9, F8]

**Supporting evidence**
  - `F2` ✔ Only one config change occurred across all services in the window: payment-service key bank.api.timeout_ms changed from 3000 to 5000 ms at 2026-04-02T11:20:00Z by author p.nair, described as "Raise bank API timeout after partner advisory". This is a payment-service change ~72 minutes before the 12:32Z report.
  - `F31` ✔ The only config change across all services in 2026-04-01T00:00Z-2026-04-02T13:59Z is cfg-2502: payment-service key bank.api.timeout_ms 3000->5000 at 2026-04-02T11:20:00Z by p.nair ('Raise bank API timeout after partner advisory'). No config entry references a certificate, secret, keystore or rotation key for the payment-service->bank-partner-api mTLS path; extending the search back to 2026-01-01 and querying bank-partner-api still returns only cfg-2502 and an empty set respectively.
**Contradicting evidence**
  - `F7` ✔ payment-service received ERROR logs on the transfer path starting 2026-04-02T12:15:27Z: 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected', then 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. 313 ERROR-level entries matched, first 12:15:27Z, last 13:58:39Z.
  - `F11` ✔ latency_p99_ms on payment-service DROPS rather than rises at onset — from ~318-341ms (11:45-12:09) to ~135-150ms (12:15-13:57) — consistent with bank calls failing fast (TLS handshake reject) instead of completing, so latency signals cannot be used to detect this incident.
  - `F25` ✔ payment_success_rate and bank_api_error_rate both stay at baseline through the 12:14 bucket and step to failure in the 12:15 bucket, i.e. onset is between 12:14 and 12:15Z. In 60s buckets: payment_success_rate = 99.68 (12:14) then 2.18 (12:15), 2.99 (12:16); bank_api_error_rate = 0.21 (12:14) then exactly 100.0 for every bucket 12:15-12:30. Neither series shows any change at 11:20Z (success 98.76 avg at 11:20 bucket; bank error 0.2) nor at 10:30Z.
  - `F28` ✔ Timeout-mode contribution is exactly zero: a log search on 'timeout' for payment-service over 2026-04-02T10:00:00Z-13:59:00Z matches 0 entries (total_matched 0, truncated false), and a search on '5xx' over the same window also matches 0 entries. Neither raised-timeout symptoms (expected if the 3000->5000ms change at 11:20Z were the cause) nor partner 5xx rejections are present, so the 100% bank_api_error_rate is TLS-handshake rejection, not timeouts and not partner 5xx.
  - `F30` ✔ On the pre-onset transfer path the only bank-related warnings are benign: 3 WARN entries in 11:15-12:15Z, two 'retrying idempotent request (attempt 2) after connection reset' at 11:24:11Z and 12:04:22Z, and one 'cert-watcher: client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)' at 11:50:26Z. No elevated bank-error or timeout warnings appear between the 11:20Z config change and the 12:15Z collapse.
**Assumptions**
  - None surviving — no mechanism links a timeout increase to a TLS cert-expiry alert
**Rejected because:** Mechanically impossible for a timeout increase to produce TLS 'expired certificate (alert 45)' rejections (F27/F28: 0 timeout and 0 5xx signature matches). The 55-minute gap is now shown real, not a masking artifact: metrics were flat in finer 60s buckets through 11:20 and up to 12:14 (success 98.76 avg at the 11:20 bucket; bank error 0.20; fully normal 12:12-12:14), with no bank-error or timeout warnings between the 11:20Z change and the 12:15Z collapse (F25, F29, F30). A timeout change would surface as slow calls/timeouts and/or a latency RISE; latency instead DROPPED (F11).

### H4: settlement-service v1.9.0 (10:30Z) co-tenant effect via shared ledger-db or shared kafka backlog
settlement-service v1.9.0 (new CSV exporter, 10:30Z) loaded the shared ledger-db or backed up shared kafka, degrading the payment transfer path while payment-service's own error rate stayed green.

**Causal chain**
1. settlement-service v1.9.0 deploys at 10:30Z with a new CSV exporter [F1]
2. The exporter consumes shared ledger-db capacity and/or backs up the shared kafka queue [F4, F5]
3. Payment transfer path degrades through the shared resources, failing transfers while HTTP errors stay green [F10]

**Supporting evidence**
  - `F1` ✔ Only one deployment occurred across all services in the window 2026-04-01T00:00Z through 2026-04-02T13:59Z: settlement-service v1.9.0 at 2026-04-02T10:30:00Z by ci-bot ("Batch settlement report export", change "new CSV exporter"), status live, not rolled back. No deployment touched payment-service in the window.
  - `F4` ✔ ledger-db is a SHARED postgres used by four services: order-service, portfolio-service, payment-service and settlement-service all write/read it (edges to ledger-db kind db). Settlement-service is thus a co-tenant of the ledger that payment-service depends on.
  - `F5` ✔ kafka is a SHARED queue: payment-service and order-service produce (queue-produce) while settlement-service and notification-service consume (queue-consume). A consumer slowdown or backlog on settlement-service can therefore affect the transfer flow without showing in payment-service's own error rate.
**Contradicting evidence**
  - `F7` ✔ payment-service received ERROR logs on the transfer path starting 2026-04-02T12:15:27Z: 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected', then 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. 313 ERROR-level entries matched, first 12:15:27Z, last 13:58:39Z.
  - `F9` ✔ bank_api_error_rate on payment-service jumps from ~0.2% (flat 11:45-12:09) to exactly 100.0 for every bucket from 12:15 onward through 13:57, confirming the bank call path is entirely failing.
  - `F13` ✔ ledger-db shows no degradation at or after the 12:15Z onset: slow_queries_per_min is flat (~0.77-1.15 avg per bucket for every bucket from 12:12 onward), and its only spike (avg 5.4, max 9.63) is at 11:54Z, ~21 minutes BEFORE the incident began, so the DB is not failing during the incident window.
  - `F14` ✔ ledger-db replication_lag_ms is completely stable across the whole window (min 35.27, max 44.34, mean 39.7 ms; bucket avgs 38.85-40.59 for 11:45-13:51), showing no ledger write/replication stall coincident with the 12:15Z failure onset.
  - `F15` ✔ payment-service db_pool_in_use is flat at ~8 connections (mean 7.99, min 7.19, max 8.95) for every bucket from 11:45 through 13:51, with no step at 12:15 — the service did not run out of DB connections and there is no DB-saturation signature on the transfer path.
  - `F25` ✔ payment_success_rate and bank_api_error_rate both stay at baseline through the 12:14 bucket and step to failure in the 12:15 bucket, i.e. onset is between 12:14 and 12:15Z. In 60s buckets: payment_success_rate = 99.68 (12:14) then 2.18 (12:15), 2.99 (12:16); bank_api_error_rate = 0.21 (12:14) then exactly 100.0 for every bucket 12:15-12:30. Neither series shows any change at 11:20Z (success 98.76 avg at 11:20 bucket; bank error 0.2) nor at 10:30Z.
  - `F29` ✔ The first deviating bucket (12:15) is preceded by a flat pre-onset baseline in finer data, so the onset is NOT an artifact of post-onset-only data: 12:08-12:14 buckets exist and read 99.21/98.47/98.63/99.22/100.0/99.52/99.68 (success) and 0.19-0.22 (bank error), and the 240s series extends back to 10:00Z with success 98.65-99.73 and bank error 0.18-0.22 for every bucket 10:00-12:08.
**Assumptions**
  - settlement-service v1.9.0 increased load on ledger-db or kafka (no metric evidence obtained)
**Rejected because:** Contradicted on mechanism and scope: the observed failure localises to the payment-service -> bank-partner-api TLS handshake (F7/F9/F28), which a DB/queue contention problem cannot produce (it would not yield an exact 100% bank_api_error_rate or a 'tls: expired certificate' alert). ledger-db shows no degradation (F13 slow_queries flat through the incident, only spike 21 min BEFORE onset; F14 replication_lag stable 35-44ms; F15 db_pool flat ~8). Onset would track the 10:30Z deploy, but both series are flat through 10:30 and 11:20 and only deviate at 12:15 (F25, F29). Kafka consumer-lag was never enumerated, so a co-tenant contribution cannot be fully excluded, but no positive co-tenant signal exists.

### H5: Ledger-db / DB connection-pool exhaustion on the transfer path
The transfer path fails because ledger-db is slow or the payment-service DB pool is exhausted, causing ledger writes to fail or hang while the aggregate HTTP error panel stays green.

**Causal chain**
1. ledger-db slow queries or replication stall begins around 12:15Z *(assumed)*
2. payment-service DB pool saturates, blocking transfer writes *(assumed)*
3. Transfers fail in a way not surfacing as 5xx [F10]

**Supporting evidence**
  - `F4` ✔ ledger-db is a SHARED postgres used by four services: order-service, portfolio-service, payment-service and settlement-service all write/read it (edges to ledger-db kind db). Settlement-service is thus a co-tenant of the ledger that payment-service depends on.
**Contradicting evidence**
  - `F13` ✔ ledger-db shows no degradation at or after the 12:15Z onset: slow_queries_per_min is flat (~0.77-1.15 avg per bucket for every bucket from 12:12 onward), and its only spike (avg 5.4, max 9.63) is at 11:54Z, ~21 minutes BEFORE the incident began, so the DB is not failing during the incident window.
  - `F14` ✔ ledger-db replication_lag_ms is completely stable across the whole window (min 35.27, max 44.34, mean 39.7 ms; bucket avgs 38.85-40.59 for 11:45-13:51), showing no ledger write/replication stall coincident with the 12:15Z failure onset.
  - `F15` ✔ payment-service db_pool_in_use is flat at ~8 connections (mean 7.99, min 7.19, max 8.95) for every bucket from 11:45 through 13:51, with no step at 12:15 — the service did not run out of DB connections and there is no DB-saturation signature on the transfer path.
  - `F7` ✔ payment-service received ERROR logs on the transfer path starting 2026-04-02T12:15:27Z: 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected', then 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. 313 ERROR-level entries matched, first 12:15:27Z, last 13:58:39Z.
  - `F9` ✔ bank_api_error_rate on payment-service jumps from ~0.2% (flat 11:45-12:09) to exactly 100.0 for every bucket from 12:15 onward through 13:57, confirming the bank call path is entirely failing.
**Rejected because:** Directly contradicted by F13 (ledger-db slow_queries flat ~0.77-1.15 through the incident; only spike at 11:54Z, 21 min BEFORE onset), F14 (replication_lag stable 35-44ms), F15 (payment-service db_pool_in_use flat ~8, no step at 12:15), and by F7/F9 which localise the failure to the mTLS/TLS handshake to bank-partner-api, not the DB.

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- (none)

## Timeline
- (none)

## Recommended actions

## Data gaps
- No telemetry/metric queries were made against payment-service, settlement-service, ledger-db, kafka or bank-partner-api in this round (budget spent on change history and dependency mapping), so I cannot say whether any metric moved at the 11:20Z config change or the 10:30Z deployment.
- bank-partner-api is external with has_telemetry=false, so no metrics or logs exist for the bank rail itself.
- Two historical-incident searches for 'payments fail for customers but dashboard green' and 'silent failure missing telemetry dashboard green payment' returned count=0; only the bank-rail query matched.
- Could not query logs or metrics for bank-partner-api: the tool returned not_found, stating "'bank-partner-api' is an external dependency: we have no logs/metrics for it. Observe it through its callers" (callers: payment-service).
- Tool-call budget was exhausted before I could run per-endpoint/per-route error rates, 5xx-vs-4xx breakdown, timeout/retry counters, and a request_rate check; the only retry evidence captured is one WARN 'retrying idempotent request (attempt 2) after connection reset' at 12:04:22Z.
- No metrics were checked for other services in the transfer flow (e.g. ledger/account/wallet services) beyond payment-service and the external bank-partner-api.
- payment-service exposes no timeout_rate, connection_errors, or DNS/TLS-specific metric (query returned not_found listing the 9 available metrics); the granular per-failure-mode breakdown of the bank rail (timeouts vs cert rejection vs connection refused) could not be measured and had to be inferred from bank_api_error_rate=100% plus the ERROR log signature.
- No transfer-queue consumer-lag or queue-depth metric could be enumerated for kafka/settlement-service/notification-service within the remaining budget; the queue hop's health during 12:15-13:59 is unverified.
- The active client certificate CN=payments-client.orangeinvest.internal and its notAfter=2026-04-02T12:15:00Z could only be seen in payment-service ERROR logs; no certificate-validity or secret/config telemetry was queryable.
- No support/helpdesk/ticketing service exists in telemetry (query_logs for 'support-service' and 'helpdesk-service' both returned not_found; known_services contains no CRM/ticket component).
- email-provider (customer-facing failure-notification fan-out) is an external dependency with no logs or metrics; notification-service shows 0 WARN+ entries so its fan-out could not be confirmed either.
- PENDING_RETRY query was truncated (returned 20 of total_matched 140), so the 140 count is a floor and the full per-minute distribution of failing payments was not enumerated.
- payment-service exposes no timeout/connection/TLS failure-counter metric (only bank_api_error_rate plus generic ones), so the timeout-vs-TLS-vs-5xx split cannot be computed from a counter or histogram; the decomposition rests on log-signature counts (0 timeout, 0 5xx, 44 TLS entries) rather than on metric values.
- bank-partner-api (external bank rail) has has_telemetry=false, so no partner-side telemetry (their 5xx rate, timeout rate, response codes) could be queried directly; partner health is inferred only from the absence of '5xx' matches in payment-service logs.
- Log queries were run per-pattern ('timeout', '5xx') with a contains filter over a single service; I did not enumerate the full unfiltered WARN/ERROR pattern inventory of other services (settlement-service, kafka consumers) in 10:30-12:20Z, so a co-tenant signal invisible in payment-service cannot be excluded.
- No certificate-store / secret-manager / keystore tool was available: get_config_changes only exposes application config keys, and queries scoped to bank-partner-api and payment-service returned only the timeout_ms key. The entry for CN=payments-client.orangeinvest.internal could not be read from any store.
- No scheduled-job / cron / rotation-run history tool was available, so the question 'did the payment-service->bank-partner-api rotation run, fail, or get skipped around 12:15Z' could not be answered directly; the empty config-change record is only weak negative evidence.
- The final depth-3 get_service_dependencies call for payment-service returned {"ok": false, "error": {"type": "budget_exhausted"}} and was never executed (only the depth-2 graph was already known from the team).

## Investigation stats
- latency 146.43s, LLM calls 28, tokens 215718 (in 183531 / out 32187)
- tool calls 48 (8 failed, 0 partial, 0 recovered by retry), findings 35/35 verified
