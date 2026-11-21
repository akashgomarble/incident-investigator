# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Payments are failing because the outbound mTLS client certificate (CN=payments-client.orangeinvest.internal) that payment-service uses to call bank-partner-api expired at 2026-04-02T12:15:00Z and was not rotated; from 12:15:27Z every handshake is rejected with 'tls: expired certificate (alert 45)'. Confidence is high (H1 at 0.90, all alternatives rejected at ≤0.06). The HTTP error-rate panel stays green because payment-service records these as business-level PENDING_RETRY, not HTTP 5xx: error_rate is flat at ~0.2% while bank_api_error_rate hits 100% and payment_success_rate collapses to ~2.5%. The failure was predicted by the cert-watcher 25 minutes in advance and is independent of the only deployment (settlement dep-5510) and config change (bank.api.timeout_ms) in the window.

## Probable root cause
**Client mTLS certificate for payment-service → bank-partner-api expired at 12:15Z** -- credential_expiry in `payment-service (client cert for bank-partner-api)` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Client mTLS certificate for payment-service → bank-partner-api expired at 12:15Z | credential_expiry | payment-service (client cert for bank-partner-api) | - | 0.90 (high) | active |
| 2 | H4: Bank/acquirer side (external) failure causing the rejections | dependency_failure | bank-partner-api | - | 0.06 (low) | rejected |
| 3 | H3: settlement-service dep-5510 CSV-exporter deploy degraded shared ledger-db, causing payment failures | bad_deployment | settlement-service → ledger-db | dep-5510 | 0.04 (low) | rejected |
| 4 | H2: bank.api.timeout_ms config change (3000->5000 at 11:20Z) caused/coincided with the failure | config_change | payment-service (bank API client config) | bank.api.timeout_ms 3000->5000 @ 11:20Z | 0.03 (low) | rejected |

### H1: Client mTLS certificate for payment-service → bank-partner-api expired at 12:15Z
The outbound mTLS client certificate CN=payments-client.orangeinvest.internal used by payment-service to call bank-partner-api reached its notAfter=2026-04-02T12:15:00Z and was not rotated. From 12:15:27Z every handshake is rejected by the bank gateway with 'tls: expired certificate (alert 45)'; payment-service marks affected transfers PENDING_RETRY at business level without emitting HTTP errors, so transfers fail (100% bank_api_error_rate, ~2.5% success) while the HTTP error-rate panel stays green.

**Causal chain**
1. payment-service's client cert CN=payments-client.orangeinvest.internal has a hard notAfter=2026-04-02T12:15:00Z (a fixed, schedulable property, not caused by any change in the window) [F2, F14]
2. cert-watcher logs a WARN 25 min before expiry (11:50:26Z) naming the exact notAfter, giving an independent timeline [F14]
3. At/after 12:15:00Z the cert expires; first TLS expiry ERROR at 12:15:27Z and continuing through 13:58:39Z [F1, F2]
4. bank-partner-api rejects the mTLS handshake: 'remote error: tls: expired certificate (alert 45)' [F1, F2, F6]
5. payment-service records the failure as a business-level PENDING_RETRY rather than an HTTP 5xx, so the HTTP error_rate panel does not move [F7, F1]
6. Resulting user-visible payment failures: bank_api_error_rate 0.2% -> 83-100% from the 12:14Z bucket; payment_success_rate ~99% -> 2.4-2.7% [F3, F4]
7. The failure is on the only anomalous dependency path (bank-partner-api, observed solely via payment-service); all other neighbors (ledger-db, kafka) show no coincident anomaly [F6, F11, F15, F16, F17]

**Supporting evidence**
  - `F1` ✔ payment-service ERROR logs begin at 2026-04-02T12:15:27Z with 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' and continue through 13:58:39Z (311 ERROR matches, 140 PENDING_RETRY + 171 cert-error). Onset is ~12:15-12:16Z.
  - `F2` ✔ The exact error signature is a TLS client-certificate expiry: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' — first_seen 2026-04-02T12:16:44Z, 171 occurrences.
  - `F3` ✔ payment-service bank_api_error_rate jumps from a flat 0.20% baseline through 12:08Z to 83.37% avg (max 100) in the 12:14Z bucket and 100.0% for every bucket from 12:20Z onward.
  - `F4` ✔ payment-service payment_success_rate collapses from ~99.04-99.81% (11:50-12:08Z) to 18.65% avg in the 12:14Z bucket, then ~2.4-2.7% for all buckets from 12:20Z through 13:56Z.
  - `F5` ✔ The HTTP error_rate panel is genuinely flat: error_rate stays in 0.16-0.26% across the whole 11:50-13:56Z window (min 0.1, max 0.32, mean 0.22), while request_rate (~100 req/s) is also unchanged and latency_p99_ms actually DROPS from ~317-330ms to ~139ms at 12:14Z onward.
  - `F6` ✔ bank-partner-api is an external dependency with no logs/metrics of its own; it must be observed through payment-service, its only caller.
  - `F7` ✔ payment-service does not emit an HTTP error for these failures (error_rate stays ~0.2%) while business-level bank_api_error_rate hits 100% — so the green error-rate panel does not reflect real payment outcomes; the failures are business-level/functional, not HTTP 5xx.
  - `F14` ✔ The cert-watcher WARN log predates the failure and warns of the exact expiry: 'cert-watcher: client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)' at 2026-04-02T11:50:26Z, 25 minutes before expiry at 12:15:00Z. This timing is independent of either candidate change.
  - `F15` ✔ ledger-db cpu_pct is flat across the entire 11:30-13:00Z window (mean 29.98%, min 27.41, max 32.82, 0 missing points) with no rise in the 12:15Z bucket (avg 29.17) where payment failures begin — no shared-datastore resource spike aligning with onset.
  - `F16` ✔ ledger-db slow_queries_per_min rises to 9.02 avg / 9.63 max in the 12:00Z bucket and 4.25 in 11:55Z, but is back to baseline by the 12:10Z bucket (0.91) and stays ~1.0 through 12:15Z (1.11) and 12:20Z (0.75) — the blip is ~15-20 min BEFORE the 12:15Z payment onset, not coincident with it.
  - `F17` ✔ ledger-db replication_lag_ms (mean 39.86, min 35.27, max 44.34) and connections_active (mean 62.1, min 55.91, max 69.24) are stable across 11:30-13:00Z with no anomaly in the 12:15Z bucket. Also, ledger-db has NO p99-latency metric ('service ledger-db has no metric latency_p99_ms'; available: connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min).
  - `F18` ✔ settlement-service emitted ZERO ERROR logs in 10:30-12:30Z (total_matched 0), so the dep-5510 CSV-exporter deployment shows no error signature around the payment onset. No ledger-db logs either (total_matched 0).
  - `F19` ✔ Across the onset window, the 80+ payment-service errors are 100% TLS client-cert expiry against bank-partner-api ('remote error: tls: expired certificate (alert 45)'), and there is no evidence tying either the dep-5510 deploy (10:30Z) or the bank.api.timeout_ms change (11:20Z) to the cert expiry or to a downstream resource/latency trend at 12:14-12:15Z. The only downstream dependency showing an anomaly is bank-partner-api, whose rejection (via expired client cert) is observed entirely within payment-service logs/metrics.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The bank gateway is the party enforcing client-cert validity and returning alert 45 (strongly supported by F2 wording, but the bank side has no telemetry to confirm independently).
  - No prior renewal/config change was scheduled that should have rotated this cert before 12:15Z (cert-watcher fired, so no rotation was in flight).

### H4: Bank/acquirer side (external) failure causing the rejections
The reporter's alternative suspicion: the failure originates on the bank/acquirer side (external provider degrading), not in our platform. Partially true in that the rejection happens at the bank end, but the origin is our own expired client cert, so this is a symptom-side attribution rather than the root cause.

**Causal chain**
1. bank-partner-api is an external dependency with no telemetry, observable only via payment-service [F6, F11]
2. payment-service calls to the bank fail and customers report transfer failures [F1, F4]
3. The bank side is degraded/changed and rejecting our calls *(assumed)*

**Supporting evidence**
  - `F6` ✔ bank-partner-api is an external dependency with no logs/metrics of its own; it must be observed through payment-service, its only caller.
  - `F11` ✔ payment-service's dependency graph: edges payment-service -> ledger-db (db), payment-service -> bank-partner-api (external), payment-service -> kafka (queue-produce), and api-gateway -> payment-service (http). ledger-db and kafka are marked shared:true; bank-partner-api has has_telemetry:false.
  - `F1` ✔ payment-service ERROR logs begin at 2026-04-02T12:15:27Z with 'payment 45f233b6 marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' and continue through 13:58:39Z (311 ERROR matches, 140 PENDING_RETRY + 171 cert-error). Onset is ~12:15-12:16Z.
**Contradicting evidence**
  - `F2` ✔ The exact error signature is a TLS client-certificate expiry: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' — first_seen 2026-04-02T12:16:44Z, 171 occurrences.
  - `F14` ✔ The cert-watcher WARN log predates the failure and warns of the exact expiry: 'cert-watcher: client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)' at 2026-04-02T11:50:26Z, 25 minutes before expiry at 12:15:00Z. This timing is independent of either candidate change.
  - `F19` ✔ Across the onset window, the 80+ payment-service errors are 100% TLS client-cert expiry against bank-partner-api ('remote error: tls: expired certificate (alert 45)'), and there is no evidence tying either the dep-5510 deploy (10:30Z) or the bank.api.timeout_ms change (11:20Z) to the cert expiry or to a downstream resource/latency trend at 12:14-12:15Z. The only downstream dependency showing an anomaly is bank-partner-api, whose rejection (via expired client cert) is observed entirely within payment-service logs/metrics.
**Assumptions**
  - That the bank independently changed behavior at 12:15Z, coincident with our cert expiry (an improbable coincidence).
**Rejected because:** The error text is explicit and self-incriminating: 'our client cert ... notAfter=2026-04-02T12:15:00Z' (F2), independently predicted by cert-watcher 25 min earlier (F14). The bank is the party enforcing the check, but the fault originates in our expired certificate, not bank degradation; no bank-side change is evidenced (F19).

### H3: settlement-service dep-5510 CSV-exporter deploy degraded shared ledger-db, causing payment failures
The orchestrator's top suspect: settlement-service v1.9.0 'new CSV exporter' (dep-5510, 10:30Z) wrote to the shared ledger-db, whose degradation then broke payment-service's transfer writes. This is supported by topology (ledger-db shared by four services, F12) and a historical precedent pattern (HIST-111, F13), but the direct telemetry contradicts it.

**Causal chain**
1. settlement-service v1.9.0 CSV-exporter (dep-5510) deployed 10:30Z, the only deploy in the window [F8]
2. ledger-db is shared by order/portfolio/payment/settlement services, so a settlement-side write pattern reaches a datastore payment-service uses [F11, F12]
3. The new exporter ran full/heavy scans and degraded ledger-db (historical analogue HIST-111) [F13]
4. Degraded ledger-db caused payment-service transfers to fail *(assumed)*
5. Users see payment failures and a green HTTP panel [F5]

**Supporting evidence**
  - `F8` ✔ Exactly one deployment occurred across all services in 10:00-13:00Z: settlement-service v1.9.0 (dep-5510), 'Batch settlement report export', deployed 2026-04-02T10:30:00Z by ci-bot, change 'new CSV exporter', status live with rolled_back_at null.
  - `F12` ✔ ledger-db is shared by four services — order-service, portfolio-service, payment-service and settlement-service all hold db edges into it — so the settlement-service deployment lands on a datastore that payment-service also writes to.
  - `F13` ✔ Historical precedent matching the 'users report failures / downstream degradation' pattern: HIST-111 (2025-05-22) 'Reporting job full table scans slowed ledger-db', services ledger-db + settlement-service, root_cause 'New report query without index did sequential scans', category bad_deployment. Also relevant: HIST-105 payment failures from bank partner API 5xx (dependency_failure). These are leads only, not evidence about the current incident.
**Contradicting evidence**
  - `F15` ✔ ledger-db cpu_pct is flat across the entire 11:30-13:00Z window (mean 29.98%, min 27.41, max 32.82, 0 missing points) with no rise in the 12:15Z bucket (avg 29.17) where payment failures begin — no shared-datastore resource spike aligning with onset.
  - `F16` ✔ ledger-db slow_queries_per_min rises to 9.02 avg / 9.63 max in the 12:00Z bucket and 4.25 in 11:55Z, but is back to baseline by the 12:10Z bucket (0.91) and stays ~1.0 through 12:15Z (1.11) and 12:20Z (0.75) — the blip is ~15-20 min BEFORE the 12:15Z payment onset, not coincident with it.
  - `F17` ✔ ledger-db replication_lag_ms (mean 39.86, min 35.27, max 44.34) and connections_active (mean 62.1, min 55.91, max 69.24) are stable across 11:30-13:00Z with no anomaly in the 12:15Z bucket. Also, ledger-db has NO p99-latency metric ('service ledger-db has no metric latency_p99_ms'; available: connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min).
  - `F18` ✔ settlement-service emitted ZERO ERROR logs in 10:30-12:30Z (total_matched 0), so the dep-5510 CSV-exporter deployment shows no error signature around the payment onset. No ledger-db logs either (total_matched 0).
  - `F19` ✔ Across the onset window, the 80+ payment-service errors are 100% TLS client-cert expiry against bank-partner-api ('remote error: tls: expired certificate (alert 45)'), and there is no evidence tying either the dep-5510 deploy (10:30Z) or the bank.api.timeout_ms change (11:20Z) to the cert expiry or to a downstream resource/latency trend at 12:14-12:15Z. The only downstream dependency showing an anomaly is bank-partner-api, whose rejection (via expired client cert) is observed entirely within payment-service logs/metrics.
  - `F2` ✔ The exact error signature is a TLS client-certificate expiry: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' — first_seen 2026-04-02T12:16:44Z, 171 occurrences.
  - `F14` ✔ The cert-watcher WARN log predates the failure and warns of the exact expiry: 'cert-watcher: client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)' at 2026-04-02T11:50:26Z, 25 minutes before expiry at 12:15:00Z. This timing is independent of either candidate change.
**Assumptions**
  - That ledger-db (which emits no logs and no latency_p99 metric) degraded at 12:15Z despite flat cpu/lag/connections.
  - That the new exporter generates heavy queries immediately and continuously from 10:30Z.
**Rejected because:** Direct evidence contradicts it: ledger-db cpu_pct, replication_lag_ms and connections_active are flat with no 12:15Z anomaly (F15, F17); the slow-query blip peaks ~12:00Z and is back to baseline by 12:10Z, ~15-20 min BEFORE onset (F16); settlement-service emitted ZERO errors 10:30-12:30Z (F18); and the actual failure signature is a TLS cert expiry, unrelated to datastore latency (F2, F19). A ~105-minute quiet gap between deploy and onset also contradicts causation.

### H2: bank.api.timeout_ms config change (3000->5000 at 11:20Z) caused/coincided with the failure
The only config change in the window (F9: bank.api.timeout_ms raised to 5000ms at 11:20Z, 'after partner advisory') is a candidate cause: a longer timeout could delay error surfacing or alter handshake behavior, and the reporter/orchestrator flagged a possible config origin. On inspection this is a weak lead.

**Causal chain**
1. Config change raised bank.api.timeout_ms 3000->5000 at 11:20Z [F9]
2. This changed the outbound bank-call behavior and led to the reported transfer failures *(assumed)*
3. Failures appear ~55 min later at 12:15Z with a TLS cert-expiry signature [F1, F2]

**Supporting evidence**
  - `F9` ✔ Exactly one config change occurred across all services in 10:00-13:00Z: payment-service key bank.api.timeout_ms changed 3000 -> 5000 at 2026-04-02T11:20:00Z (author p.nair, 'Raise bank API timeout after partner advisory').
**Contradicting evidence**
  - `F2` ✔ The exact error signature is a TLS client-certificate expiry: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' — first_seen 2026-04-02T12:16:44Z, 171 occurrences.
  - `F14` ✔ The cert-watcher WARN log predates the failure and warns of the exact expiry: 'cert-watcher: client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)' at 2026-04-02T11:50:26Z, 25 minutes before expiry at 12:15:00Z. This timing is independent of either candidate change.
  - `F19` ✔ Across the onset window, the 80+ payment-service errors are 100% TLS client-cert expiry against bank-partner-api ('remote error: tls: expired certificate (alert 45)'), and there is no evidence tying either the dep-5510 deploy (10:30Z) or the bank.api.timeout_ms change (11:20Z) to the cert expiry or to a downstream resource/latency trend at 12:14-12:15Z. The only downstream dependency showing an anomaly is bank-partner-api, whose rejection (via expired client cert) is observed entirely within payment-service logs/metrics.
  - `F9` ✔ Exactly one config change occurred across all services in 10:00-13:00Z: payment-service key bank.api.timeout_ms changed 3000 -> 5000 at 2026-04-02T11:20:00Z (author p.nair, 'Raise bank API timeout after partner advisory').
**Assumptions**
  - That a timeout change could produce a TLS expired-certificate alert (no plausible mechanism; F2 shows a fixed notAfter).
**Rejected because:** No mechanism: an HTTP/socket timeout change cannot expire or reject a client certificate. The failure is a hard TLS alert 45 with a fixed notAfter=12:15:00Z (F2, F14) that is independent of timing (F14). The change at 11:20Z precedes onset by ~55 min and shows no alignment with the 12:15Z cert expiry; F19 explicitly ties no change to the cert expiry.

## Facts (verified observations)
- The exact failure signature is a TLS client-certificate expiry: 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' — first_seen 2026-04-02T12:16:44Z, 171 occurrences. [F2]
- payment-service ERROR logs begin at 2026-04-02T12:15:27Z with 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' and continue through 13:58:39Z (311 ERROR matches, 140 PENDING_RETRY + 171 cert-error). [F1]
- The cert-watcher logged at 2026-04-02T11:50:26Z, 25 minutes before expiry, 'client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)' — an independent pre-onset prediction of the exact expiry timestamp. [F14]
- payment-service bank_api_error_rate jumped from a 0.20% flat baseline to 83.37% avg (max 100) in the 12:14Z bucket and 100.0% for every bucket from 12:20Z onward. [F3]
- payment_success_rate collapsed from ~99.04-99.81% (11:50-12:08Z) to 18.65% avg in the 12:14Z bucket, then ~2.4-2.7% for all buckets from 12:20Z through 13:56Z. [F4]
- The HTTP error_rate panel is genuinely flat (0.16-0.26% across 11:50-13:56Z) with request_rate unchanged (~100 req/s) and latency_p99_ms dropping from ~317-330ms to ~139ms at 12:14Z onward. [F5]
- bank-partner-api is an external dependency with no logs or metrics of its own and must be observed through payment-service, its only caller; bank-partner-api has has_telemetry:false. [F6, F11]
- Exactly one deployment occurred across all services in 10:00-13:00Z: settlement-service v1.9.0 (dep-5510, 'new CSV exporter'), deployed 2026-04-02T10:30:00Z, status live, rolled_back_at null. [F8]
- Exactly one config change occurred across all services in 10:00-13:00Z: payment-service bank.api.timeout_ms 3000 -> 5000 at 2026-04-02T11:20:00Z ('Raise bank API timeout after partner advisory'). [F9]
- payment-service had zero deployments in 08:00-14:00Z and settlement-service had zero config changes in the same window. [F10]
- ledger-db cpu_pct is flat across 11:30-13:00Z (mean 29.98%, min 27.41, max 32.82) with no rise in the 12:15Z bucket (avg 29.17) where payment failures begin. [F15]
- ledger-db slow_queries_per_min peaked at 9.02 avg / 9.63 max in the 12:00Z bucket, returned to baseline by 12:10Z (0.91), and stayed ~1.0 through 12:15Z (1.11) and 12:20Z (0.75) — the blip is ~15-20 min before the 12:15Z onset. [F16]
- ledger-db replication_lag_ms (mean 39.86) and connections_active (mean 62.1) are stable across 11:30-13:00Z with no anomaly in the 12:15Z bucket; ledger-db exposes no latency_p99_ms metric. [F17]
- settlement-service emitted zero ERROR logs in 10:30-12:30Z, and ledger-db emitted no logs at all, so the dep-5510 deployment shows no error signature around the payment onset. [F18]
- ledger-db is shared by four services (order-service, portfolio-service, payment-service, settlement-service), so the settlement-service deployment lands on a datastore payment-service also writes to. [F12]

## Assumptions (not verified)
- The bank gateway is the party enforcing client-certificate validity and returning TLS alert 45; the bank side has no telemetry in our platform, so this can only be inferred from the caller-side error text (F2), which is nonetheless internally corroborated by the independent cert-watcher prediction of the same notAfter (F14).
- No prior renewal or config change was scheduled that should have rotated this certificate before 12:15Z — the cert-watcher fired a WARN, implying no rotation was in flight.
- The 12:15:27Z log onset and 12:14Z metric bucket onset reflect the same event; the metric bucket is 360s wide so it cannot pin the transition second, and the log timestamp is treated as the tightest bound.
- The unscoped all-services deployment/config listings returned a complete change set for 10:00-13:00Z (i.e. dep-5510 and the timeout change are genuinely the only changes), an assumption required to state that no other change caused or masked the failure.
- The green HTTP error-rate panel is explained by payment-service classifying these failures as business-level PENDING_RETRY rather than HTTP 5xx; the exact internal mechanism (e.g. async transfer workers) was not directly instrumented in the evidence reviewed.
- (demoted from facts: no verified observation backs it) All payment-service errors across the onset window are 100% TLS client-cert expiry against bank-partner-api; no evidence ties either the dep-5510 deploy (10:30Z) or the bank.api.timeout_ms change (11:20Z) to the cert expiry or to a downstream resource/latency trend at 12:14-12:15Z.
- (demoted from facts: no verified observation backs it) payment-service emits no HTTP error for these failures while business-level bank_api_error_rate reaches 100% — the green error-rate panel does not reflect real payment outcomes because failures are business-level/functional, not HTTP 5xx.
- (demoted from facts: no verified observation backs it) Historical precedents exist for the 'users report failures / downstream degradation' pattern: HIST-111 (2025-05-22) reporting job full table scans slowed ledger-db (bad_deployment) and HIST-105 payment failures from bank partner API 5xx (dependency_failure); these are leads only, not evidence about the current incident.

## Timeline
- `2026-04-02T10:30:00Z` Only deployment in the window: settlement-service v1.9.0 (dep-5510, 'new CSV exporter') goes live via ci-bot; no errors follow it. [F8, F18]
- `2026-04-02T11:20:00Z` Only config change in the window: payment-service bank.api.timeout_ms raised 3000 -> 5000 ('after partner advisory'). No mechanism links this to a TLS certificate alert. [F9, F19]
- `2026-04-02T11:50:26Z` cert-watcher WARNs that CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z) — independent pre-onset prediction of the exact expiry. [F14]
- `2026-04-02T11:55:00Z-12:10:00Z` ledger-db slow_queries_per_min blips (peaks ~12:00Z at 9.02 avg) then returns to baseline (0.91) by 12:10Z — ~15-20 minutes BEFORE payment onset, with cpu/lag/connections flat throughout. [F16, F15, F17]
- `2026-04-02T12:15:00Z` Client certificate CN=payments-client.orangeinvest.internal reaches notAfter; handshakes to bank-partner-api begin to be rejected. [F2, F14]
- `2026-04-02T12:15:27Z` First payment-service ERROR: transfer 45f233b6 marked PENDING_RETRY, mTLS handshake with bank-partner-api rejected; errors continue through 13:58:39Z. [F1]
- `2026-04-02T12:16:44Z` Exact error signature first recorded: 'remote error: tls: expired certificate (alert 45); our client cert ... notAfter=2026-04-02T12:15:00Z' (171 occurrences). [F2]
- `2026-04-02T12:14:00Z` First affected 6-minute metric bucket: bank_api_error_rate 83.37% avg (max 100), payment_success_rate 18.65% avg, while HTTP error_rate stays ~0.2% and latency_p99 drops to ~139ms. [F3, F4, F5]
- `2026-04-02T12:20:00Z` bank_api_error_rate is 100% for every bucket from here onward; payment_success_rate sits at ~2.4-2.7% through 13:56Z. [F3, F4]
- `2026-04-02T12:32:00Z` Incident INC-005 reported (SEV-2) by support lead: ~40 tickets about failed fund transfers; payment-service HTTP error-rate panel shows nothing unusual. [F5, F7]

## Recommended actions
- **[immediate]** Mitigate now: rotate/renew the payment-service client certificate CN=payments-client.orangeinvest.internal and reload it into payment-service's outbound TLS config (and restart/reload the pod so the new cert is picked up), then confirm handshakes to bank-partner-api succeed again. -- The failure is a hard TLS alert 45 on a certificate whose notAfter=2026-04-02T12:15:00Z has passed; restoring a valid client cert is the only fix that addresses the observed error signature. [F1, F2, F14]
- **[immediate]** After rotation, replay the PENDING_RETRY transfers (the 140 PENDING_RETRY records logged between 12:15:27Z and 13:58:39Z) and verify payment_success_rate returns to ~99% and bank_api_error_rate to baseline. -- Transfers were parked at business level, not failed with HTTP 5xx, so customer impact persists until retries succeed; success-rate recovery is the verification signal. [F1, F3, F4]
- **[short_term]** Add/extend a business-level alert on payment-service bank_api_error_rate and payment_success_rate (and on PENDING_RETRY rate), so a green HTTP error_rate panel can no longer mask functional payment failures. -- HTTP error_rate stayed 0.16-0.26% while bank_api_error_rate hit 100% and success rate fell to ~2.5%, which is why the incident was only found via tickets. [F3, F4, F5, F7]
- **[short_term]** Verify the cert-watcher path end-to-end: confirm the 11:50:26Z WARN was delivered and ticketed, and that automatic renewal/rotation actively re-issues and hot-reloads client certs with enough lead time (not merely warning). -- The expiry was predicted 25 minutes in advance but the cert was not rotated, so detection existed without remediation. [F14]
- **[short_term]** Close out the rejected leads explicitly: confirm no relation between dep-5510 (settlement CSV exporter) and ledger-db health (cpu/lag/connections flat, slow-query blip pre-onset) and note that the bank.api.timeout_ms 3000->5000 change at 11:20Z is unrelated to cert expiry — do not roll back either change on this basis alone. -- The only deploy and only config change in the window are affirmatively contradicted as causes; rolling them back would add risk without addressing the failure. [F8, F9, F15, F16, F17, F18, F19]
- **[long_term]** Add a pre-expiry rotation gate/policy for all outbound mTLS client certs: enforce automated rotation well before notAfter with hard fail rather than WARN, plus expiry monitoring across every service-to-partner credential. -- The root cause is a missing rotation of a schedulable, known-expiry credential; the same class of failure can recur on any other partner integration. [F2, F14]
- **[long_term]** Improve observability of the bank path: surface bank-partner-api call outcomes (success/error/retry rates, TLS handshake failures) as first-class dashboards/alerts, and capture the bank gateway's own response/telemetry where the contract allows. -- bank-partner-api has has_telemetry:false and is only observable through payment-service, which delayed independent confirmation and forces reliance on caller-side logs. [F6, F11, F19]

## Data gaps
- bank-partner-api has no logs or metrics (has_telemetry:false), so the bank/acquirer side of the mTLS rejection could not be inspected directly; only the caller's view is available.
- bank-partner-api is external with no telemetry and no outbound bank-call trace query was run, so it cannot be determined whether the bank gateway contributed anything beyond enforcing cert validity, nor whether the 5000ms timeout change was a reaction or a cause.
- Within the 12:14Z bucket, bank_api_error_rate already averaged 83.37% (max 100) and success rate 18.65%, but 360s bucket granularity prevents pinning the exact second of transition; the log-based 12:15:27Z onset is the tightest bound obtained.
- ledger-db exposes no latency_p99_ms metric and emits no logs; it was excluded as a cause mainly via flat cpu_pct/replication_lag_ms/connections_active and the pre-onset slow-query blip rather than a negative latency measurement, and disk_used_pct was not queried.
- No payment-service latency/queue-depth/pending-transfer metrics were retrieved, so the precise mechanism by which failures stay invisible to the HTTP error-rate panel is inferred (business-level PENDING_RETRY) rather than directly measured.
- Deployment/config listings for payment-adjacent services (transfer/ledger workers, kafka, api-gateway) were not individually queried; only the unscoped all-services call was used, so a change in a worker service could have been missed if that listing is filtered or capped.
- api-gateway WARN/ERROR query for 12:00-13:59Z returned zero matches, leaving it unverified whether the anomaly surfaces in neighbouring services.
- No config/service inventory for cert-watcher was retrieved, so it could not be definitively ruled out that a config change altered a cert renewal path before the 12:15Z expiry (though no such change appears in the 10:00-13:00Z change set).
- Tool reliability: query_logs(service=bank-partner-api) and query_metrics(metric=latency_p99_ms, service=ledger-db) both FAILED with not_found, capping the direct evidence available for those components.

## What would change this conclusion
- Evidence that the client certificate CN=payments-client.orangeinvest.internal was actually valid (or was successfully renewed and reloaded) at or after 2026-04-02T12:15:00Z — e.g. a rotation/reload record — which would break the direct causal link between the notAfter and the onset.
- Evidence that bank-partner-api independently changed behaviour at ~12:15Z (e.g. bank-side advisory, gateway config change, or its own error telemetry) coincident with our cert expiry, which would resurrect the external-dependency hypothesis H4.
- Telemetry showing ledger-db degradation at 12:15Z (e.g. a latency or lock/contention spike, or a disk_used_pct anomaly) coinciding with onset, which would revive the shared-datastore hypothesis H3 despite the flat cpu/lag/connections and pre-onset slow-query blip.
- Discovery of an additional change (a service, config, or certificate-rotation path) in the 10:00-13:00Z window that was not returned by the unscoped deployment/config listing, which could indicate a missed trigger.
- Evidence that the TLS alert-45 handshake failures are being raised or reported by a different client identity/channel than the one whose expiry F14 predicted, which would mean the attached certificate path is not the one in failure.

## Integrity warnings (set by code)
- fact demoted to assumption: All payment-service errors across the onset window are 100% TLS client-cert expi
- fact demoted to assumption: payment-service emits no HTTP error for these failures while business-level bank
- fact demoted to assumption: Historical precedents exist for the 'users report failures / downstream degradat

## Investigation stats
- latency 98.86s, LLM calls 13, tokens 84050 (in 66234 / out 17816)
- tool calls 24 (2 failed, 0 partial, 0 recovered by retry), findings 19/19 verified
