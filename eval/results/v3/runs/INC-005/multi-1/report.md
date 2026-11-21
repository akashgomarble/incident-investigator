# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The incident is caused by the expiry of payment-service's outbound mTLS client certificate CN=payments-client.orangeinvest.internal at 2026-04-02T12:15:00Z (H1, confidence 0.86). The cert-watcher warned 25 minutes in advance (F11), the first hard failure occurred 27 seconds after notAfter (F12), and both bank_api_error_rate (100%) and payment_success_rate (2.51%) broke at exactly the 12:15Z bucket (F9, F10). Because failing bank calls were absorbed as PENDING_RETRY instead of HTTP 5xx, payment-service's aggregate HTTP error_rate stayed flat at ~0.22% (F8), which is why the dashboard looked green while customers saw ~97% transfer failures. Traffic volume was unchanged and latency dropped (fast failures, no restarts), excluding load, retry-storm, timeout-config, and shared-dependency mechanisms (F13 vs F2/F3). The exact TLS error side (peer rejecting our cert vs. our client rejecting the peer) and the absence of any renewal are asserted from caller-side logs only, which caps confidence at 0.86 but does not change the fix: reissue/renew and hot-reload the client certificate.

## Probable root cause
**Expired mTLS client certificate causes bank-partner-api calls to fail (silent partial failure)** -- credential_expiry in `payment-service (client certificate / credential lifecycle)`, triggered by `null` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired mTLS client certificate causes bank-partner-api calls to fail (silent partial failure) | credential_expiry | payment-service (client certificate / credential lifecycle) | null | 0.86 (high) | active |
| 2 | H2: bank-partner-api (external dependency) outage or policy change rejects payment-service calls | dependency_failure | bank-partner-api (external partner) | null | 0.12 (low) | active |
| 3 | H3: Timeout config change (bank.api.timeout_ms 3000->5000) altered retry/absorption behavior on the bank path | config_change | payment-service (configuration) | tc-002 | 0.08 (low) | active |
| 4 | H4: Shared downstream (ledger-db or kafka) degradation causes transfers to fail silently | infrastructure_failure | ledger-db or kafka | null | 0.05 (low) | active |
| 5 | H5: Client-app/bank-side reporting artifact only; payment-service genuinely healthy | unknown | client app / bank partner (external, not payment-service) | null | 0.02 (low) | rejected |

### H1: Expired mTLS client certificate causes bank-partner-api calls to fail (silent partial failure)
payment-service's outbound client certificate CN=payments-client.orangeinvest.internal expired at 12:15Z, so the mTLS handshake to bank-partner-api is rejected; failing transfers are absorbed as PENDING_RETRY instead of HTTP 5xx, keeping the error-rate panel green while customers see ~97% transfer failure.

**Causal chain**
1. payment-service mTLS client cert CN=payments-client.orangeinvest.internal reached notAfter=2026-04-02T12:15:00Z; cert-watcher warned 25 min early at 11:50:26Z [F11]
2. At exactly 12:15:27Z the first hard failure occurs: bank-partner-api rejects the mTLS handshake with 'tls: expired certificate (alert 45)' [F12]
3. payment-service absorbs the failed bank call as PENDING_RETRY rather than returning 5xx, so HTTP error_rate stays flat (~0.22%) and the dashboard stays green [F8, F12]
4. bank_api_error_rate jumps from 0.20% to exactly 100.0% at the 12:15Z bucket; payment_success_rate collapses from ~99.25% to 2.51% [F9, F10]
5. Customers experience failed fund transfers (~40 tickets); request volume and latency profile confirm fast failures at the bank-call step, not a load or client-side issue [F13]

**Supporting evidence**
  - `F8` ✔ payment-service HTTP error_rate is flat/normal across 11:45–13:59Z (mean 0.22%, max 0.32%), which explains the green dashboard panel — this metric only counts HTTP 5xx returned to the caller, and failing bank calls were absorbed as PENDING_RETRY rather than surfaced as 5xx.
  - `F9` ✔ payment-service bank_api_error_rate jumped from a steady 0.20% baseline (11:45–12:09Z) to exactly 100.0% starting with the 12:15Z bucket and stayed at 100.0% through 13:57Z.
  - `F10` ✔ payment_service success rate collapsed from ~99.25% (12:09Z) to 2.51% (12:15Z) and remained ~2–2.8% through 13:57Z — i.e. the overwhelming majority of fund transfers stopped completing, matching the ~40 support tickets.
  - `F11` ✔ The earliest observable anomaly in the window is a cert-watcher WARN at 11:50:26Z: the payment-service client certificate CN=payments-client.orangeinvest.internal was due to expire at 2026-04-02T12:15:00Z. This is a 25-minute early warning that precedes any customer impact.
  - `F12` ✔ First hard failure: at 12:15:27Z payment 45f233b6 was marked PENDING_RETRY after the mTLS handshake to bank-partner-api was rejected; the dominant ERROR patterns are 'payment <n> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140 occurrences, first_seen 12:15:27Z) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (171 occurrences, first_seen 12:16:44Z). 311 of 314 WARN+ERROR log lines are ERROR-level.
  - `F13` ✔ payment-service request volume did NOT change (steady ~100 req/s, 90.2–109.6 across the whole window; 12:15Z bucket 101.27) — so this is not a traffic surge or a client-retry storm; the request mix simply fails at the bank-call step. Latency actually DROPPED from ~320ms p99 to ~140ms at the same 12:15Z boundary (fast failures instead of completed bank round-trips), and there were no restarts.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The client certificate was not renewed before/at expiry anywhere in the path (no evidence of renewal; cert-watcher only warned)
  - No other cause simultaneously broke the bank handshake — the dominant ERROR text is unambiguously the expired cert
  - PENDING_RETRY did not drain and complete later, i.e. transfers genuinely failed

### H2: bank-partner-api (external dependency) outage or policy change rejects payment-service calls
The bank partner's API degraded or changed its TLS/trust policy around 12:15Z, so payment-service's calls fail at the handshake; the 100% bank_api_error_rate and 2.5% payment_success_rate reflect a partner-side failure rather than payment-service's own credential.

**Causal chain**
1. bank-partner-api is an external dependency with no telemetry of its own; its health can only be inferred from caller-side signals [F5]
2. At 12:15Z payment-service bank_api_error_rate goes to 100% and success_rate collapses [F9, F10]
3. Handshake failures were logged as mTLS rejections with a remote 'expired certificate' alert at 12:15:27Z [F12]

**Supporting evidence**
  - `F5` ✔ Shared components on the payment path: ledger-db is marked "shared": true and is written/read by order-service, portfolio-service, payment-service and settlement-service; kafka is also shared. bank-partner-api is external with no telemetry (has_telemetry false).
  - `F9` ✔ payment-service bank_api_error_rate jumped from a steady 0.20% baseline (11:45–12:09Z) to exactly 100.0% starting with the 12:15Z bucket and stayed at 100.0% through 13:57Z.
  - `F10` ✔ payment_service success rate collapsed from ~99.25% (12:09Z) to 2.51% (12:15Z) and remained ~2–2.8% through 13:57Z — i.e. the overwhelming majority of fund transfers stopped completing, matching the ~40 support tickets.
**Contradicting evidence**
  - `F12` ✔ First hard failure: at 12:15:27Z payment 45f233b6 was marked PENDING_RETRY after the mTLS handshake to bank-partner-api was rejected; the dominant ERROR patterns are 'payment <n> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140 occurrences, first_seen 12:15:27Z) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (171 occurrences, first_seen 12:16:44Z). 311 of 314 WARN+ERROR log lines are ERROR-level.
  - `F11` ✔ The earliest observable anomaly in the window is a cert-watcher WARN at 11:50:26Z: the payment-service client certificate CN=payments-client.orangeinvest.internal was due to expire at 2026-04-02T12:15:00Z. This is a 25-minute early warning that precedes any customer impact.
**Assumptions**
  - The partner-side service, not our client cert, is what broke — despite the log explicitly naming OUR cert's notAfter=12:15:00Z
  - cert-watcher's expiry warning is coincidental to the onset

### H3: Timeout config change (bank.api.timeout_ms 3000->5000) altered retry/absorption behavior on the bank path
The 11:20Z payment-service config change 'bank.api.timeout_ms' 3000->5000ms changed how bank calls fail, converting bank-call problems into silently absorbed PENDING_RETRY rather than surfaced errors; combined with a partner advisory this produced the green-dashboard-but-failed-transfers symptom.

**Causal chain**
1. payment-service key bank.api.timeout_ms changed 3000->5000ms at 11:20Z, described as raised after a partner advisory [F2]
2. The change is the nearest preceding change to the 12:00-12:30Z onset window (40+ min earlier) [F3]
3. Failed bank calls were absorbed as PENDING_RETRY instead of 5xx, hiding failures from the error-rate panel [F8]
4. This produces customer-visible transfer failures with a green dashboard [F10]

**Supporting evidence**
  - `F2` ✔ Only one config change occurred across all services in the window: payment-service key 'bank.api.timeout_ms' changed 3000 -> 5000 ms at 2026-04-02T11:20:00Z by p.nair, described as 'Raise bank API timeout after partner advisory'.
  - `F3` ✔ Neither recorded change is timestamped inside the reported 12:00-12:30Z onset window; the nearest preceding change to that window is the payment-service timeout change at 11:20Z (40+ min before onset), and the settlement deployment is at 10:30Z.
  - `F8` ✔ payment-service HTTP error_rate is flat/normal across 11:45–13:59Z (mean 0.22%, max 0.32%), which explains the green dashboard panel — this metric only counts HTTP 5xx returned to the caller, and failing bank calls were absorbed as PENDING_RETRY rather than surfaced as 5xx.
**Contradicting evidence**
  - `F12` ✔ First hard failure: at 12:15:27Z payment 45f233b6 was marked PENDING_RETRY after the mTLS handshake to bank-partner-api was rejected; the dominant ERROR patterns are 'payment <n> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140 occurrences, first_seen 12:15:27Z) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (171 occurrences, first_seen 12:16:44Z). 311 of 314 WARN+ERROR log lines are ERROR-level.
  - `F11` ✔ The earliest observable anomaly in the window is a cert-watcher WARN at 11:50:26Z: the payment-service client certificate CN=payments-client.orangeinvest.internal was due to expire at 2026-04-02T12:15:00Z. This is a 25-minute early warning that precedes any customer impact.
  - `F9` ✔ payment-service bank_api_error_rate jumped from a steady 0.20% baseline (11:45–12:09Z) to exactly 100.0% starting with the 12:15Z bucket and stayed at 100.0% through 13:57Z.
  - `F13` ✔ payment-service request volume did NOT change (steady ~100 req/s, 90.2–109.6 across the whole window; 12:15Z bucket 101.27) — so this is not a traffic surge or a client-retry storm; the request mix simply fails at the bank-call step. Latency actually DROPPED from ~320ms p99 to ~140ms at the same 12:15Z boundary (fast failures instead of completed bank round-trips), and there were no restarts.
**Assumptions**
  - Longer timeout changed failure classification — no log evidence shows timeouts; the dominant errors are TLS/mTLS rejections, not timeouts
  - The 40+ min gap between the change and onset is causally bridged

### H4: Shared downstream (ledger-db or kafka) degradation causes transfers to fail silently
A shared component on the payment path (ledger-db IOPS/disk, or kafka consumer/producer lag) degraded at onset, so transfers stop settling and only surface as customer tickets while payment-service HTTP errors stay green — echoing precedents HIST-110/HIST-111/HIST-114 or HIST-112/HIST-104.

**Causal chain**
1. ledger-db is marked shared:true and is read/written by order, portfolio, payment and settlement services; kafka is also shared [F5]
2. Historical precedents show slow/failing payment and settlement paths traced to ledger-db IOPS throttling, full-table scans and disk-full WAL, plus a settlement consumer stall after partition reassignment [F6, F7]
3. Payment failures could accordingly be absorbed silently as PENDING_RETRY with no HTTP 5xx [F8]

**Supporting evidence**
  - `F5` ✔ Shared components on the payment path: ledger-db is marked "shared": true and is written/read by order-service, portfolio-service, payment-service and settlement-service; kafka is also shared. bank-partner-api is external with no telemetry (has_telemetry false).
  - `F6` ✔ Historical precedents involving payment-service or its shared components: HIST-105 'Payment failures: bank partner API outage' (2025-11-19, payment failures with bank api errors, dependency_failure); HIST-110 'Slow orders from ledger-db IOPS throttling' (2026-01-15, statement timeouts/slow queries, match_score 4); HIST-111 'Reporting job full table scans slowed ledger-db' (2025-05-22, tied to settlement-service, match_score 4); HIST-109 token validation regression with 401 invalid_token (2025-07-11); HIST-114 ledger-db disk full from retained WAL (2025-04-18, write failures).
  - `F7` ✔ Several matched precedents are cases where the failure was silent at the HTTP layer or caused by shared components: HIST-109 (auth library upgrade producing 401 invalid_token, found only via token validation), HIST-115 'connection pool misconfiguration' with 'request timed out'/'pool exhausted' (config_change), HIST-112 settlement consumer stalled after partition reassignment (consumer lag growing, settlements stuck), HIST-104 Kafka broker rebalance lag.
  - `F8` ✔ payment-service HTTP error_rate is flat/normal across 11:45–13:59Z (mean 0.22%, max 0.32%), which explains the green dashboard panel — this metric only counts HTTP 5xx returned to the caller, and failing bank calls were absorbed as PENDING_RETRY rather than surfaced as 5xx.
**Contradicting evidence**
  - `F9` ✔ payment-service bank_api_error_rate jumped from a steady 0.20% baseline (11:45–12:09Z) to exactly 100.0% starting with the 12:15Z bucket and stayed at 100.0% through 13:57Z.
  - `F12` ✔ First hard failure: at 12:15:27Z payment 45f233b6 was marked PENDING_RETRY after the mTLS handshake to bank-partner-api was rejected; the dominant ERROR patterns are 'payment <n> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140 occurrences, first_seen 12:15:27Z) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (171 occurrences, first_seen 12:16:44Z). 311 of 314 WARN+ERROR log lines are ERROR-level.
  - `F13` ✔ payment-service request volume did NOT change (steady ~100 req/s, 90.2–109.6 across the whole window; 12:15Z bucket 101.27) — so this is not a traffic surge or a client-retry storm; the request mix simply fails at the bank-call step. Latency actually DROPPED from ~320ms p99 to ~140ms at the same 12:15Z boundary (fast failures instead of completed bank round-trips), and there were no restarts.
**Assumptions**
  - Shared-component degradation coincides exactly at 12:15Z — no ledger/kafka telemetry was retrieved (explicit gap)
  - ledger/kafka failures would not log as mTLS/TLS certificate errors (they do not)
  - Precedent-based plausibility substitutes for direct evidence, which is absent

### H5: Client-app/bank-side reporting artifact only; payment-service genuinely healthy
The 40 support tickets reflect a client-app or bank-side issue, and payment-service is healthy — the flat HTTP error_rate accurately represents service health.

**Causal chain**
1. HTTP error_rate stayed flat (~0.22%) across the whole window [F8]
2. Customers reported failures anyway *(assumed)*

**Supporting evidence**
  - `F8` ✔ payment-service HTTP error_rate is flat/normal across 11:45–13:59Z (mean 0.22%, max 0.32%), which explains the green dashboard panel — this metric only counts HTTP 5xx returned to the caller, and failing bank calls were absorbed as PENDING_RETRY rather than surfaced as 5xx.
**Contradicting evidence**
  - `F9` ✔ payment-service bank_api_error_rate jumped from a steady 0.20% baseline (11:45–12:09Z) to exactly 100.0% starting with the 12:15Z bucket and stayed at 100.0% through 13:57Z.
  - `F10` ✔ payment_service success rate collapsed from ~99.25% (12:09Z) to 2.51% (12:15Z) and remained ~2–2.8% through 13:57Z — i.e. the overwhelming majority of fund transfers stopped completing, matching the ~40 support tickets.
  - `F11` ✔ The earliest observable anomaly in the window is a cert-watcher WARN at 11:50:26Z: the payment-service client certificate CN=payments-client.orangeinvest.internal was due to expire at 2026-04-02T12:15:00Z. This is a 25-minute early warning that precedes any customer impact.
  - `F12` ✔ First hard failure: at 12:15:27Z payment 45f233b6 was marked PENDING_RETRY after the mTLS handshake to bank-partner-api was rejected; the dominant ERROR patterns are 'payment <n> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140 occurrences, first_seen 12:15:27Z) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (171 occurrences, first_seen 12:16:44Z). 311 of 314 WARN+ERROR log lines are ERROR-level.
  - `F13` ✔ payment-service request volume did NOT change (steady ~100 req/s, 90.2–109.6 across the whole window; 12:15Z bucket 101.27) — so this is not a traffic surge or a client-retry storm; the request mix simply fails at the bank-call step. Latency actually DROPPED from ~320ms p99 to ~140ms at the same 12:15Z boundary (fast failures instead of completed bank round-trips), and there were no restarts.
**Assumptions**
  - Aggregate HTTP error_rate alone is sufficient to declare payment-service healthy — contradicted by the collapse in payment_success_rate to 2.51%
**Rejected because:** payment_success_rate collapsed to ~2.5% (F10) and bank_api_error_rate hit 100% (F9) with 311 ERROR-level lines naming an expired client certificate (F12); payment-service is demonstrably failing at the bank-call step, not healthy.

## Facts (verified observations)
- payment-service HTTP error_rate remained flat/normal across 11:45-13:59Z (mean 0.22%, max 0.32%), explaining the green dashboard panel; this metric only counts HTTP 5xx returned to the caller, while failing bank calls were absorbed as PENDING_RETRY. [F8]
- payment-service bank_api_error_rate jumped from a 0.20% baseline (11:45-12:09Z) to exactly 100.0% starting with the 12:15Z bucket and stayed at 100.0% through 13:57Z. [F9]
- payment_service success rate collapsed from ~99.25% (12:09Z) to 2.51% (12:15Z) and remained ~2-2.8% through 13:57Z, matching the ~40 support tickets. [F10]
- A cert-watcher WARN at 11:50:26Z flagged that the payment-service client certificate CN=payments-client.orangeinvest.internal was due to expire at 2026-04-02T12:15:00Z, 25 minutes before any customer impact. [F11]
- The first hard failure was at 12:15:27Z when payment 45f233b6 was marked PENDING_RETRY after the mTLS handshake to bank-partner-api was rejected; dominant ERROR patterns are 'marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' (140 occurrences, first_seen 12:15:27Z) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (171 occurrences, first_seen 12:16:44Z). 311 of 314 WARN+ERROR lines are ERROR-level. [F12]
- payment-service request volume was steady (~100 req/s, 90.2-109.6; 12:15Z bucket 101.27) with no restarts, and p99 latency dropped from ~320ms to ~140ms at the 12:15Z boundary (fast failures rather than completed bank round-trips), ruling out traffic surge and client-retry storm. [F13]
- Only one deployment occurred in any service between 09:00Z and 13:59Z on 2026-04-02 (settlement-service v1.9.0 at 10:30:00Z) and only one config change occurred across all services (payment-service 'bank.api.timeout_ms' 3000->5000 ms at 11:20:00Z); neither is timestamped inside the reported 12:00-12:30Z onset window. [F1, F2]
- payment-service's dependency map includes outbound edges to ledger-db (db, shared), bank-partner-api (external), and kafka (queue-produce, shared); bank-partner-api has no telemetry of its own (has_telemetry false). [F4, F5]
- Matched historical precedents involving payment-service or its shared components include HIST-105 (bank partner API outage), HIST-110/HIST-111 (ledger-db IOPS throttling and full table scans), HIST-114 (ledger-db disk full), HIST-109/HIST-115 (silent auth/pool failures), HIST-104/HIST-112 (kafka rebalance/consumer lag). [F6, F7]

## Assumptions (not verified)
- The client certificate was not renewed before or at expiry anywhere in the path; no renewal evidence was found and the cert-watcher only warned.
- No other cause simultaneously broke the bank handshake; the dominant ERROR text unambiguously names our expired client certificate.
- PENDING_RETRY transactions did not later drain and complete, i.e. the transfers genuinely failed from the customer's perspective.
- The exact TLS error side (the bank peer rejecting our expired client cert, versus our client rejecting the peer's certificate) is inferred from caller-side logs only and was not independently confirmed.
- The two change-listing tools cover infra-level changes (volume/IOPS, broker maintenance, certificate renewal, gateway config); if they do not, a coincident infra change in 11:50-12:15Z could be invisible.
- Absence of a recorded renewal is a true absence rather than a coverage limit of the change/config listing tools.

## Timeline
- `2026-04-02T10:30:00Z` Only deployment in the window: settlement-service v1.9.0 (CSV exporter), status live, not rolled back. [F1]
- `2026-04-02T11:20:00Z` Only config change in the window: payment-service 'bank.api.timeout_ms' 3000 -> 5000 ms by p.nair ('Raise bank API timeout after partner advisory'). [F2]
- `2026-04-02T11:45:00Z` payment-service HTTP error_rate begins a flat/normal run (mean 0.22%, max 0.32%) that continues to 13:59Z; bank_api_error_rate baseline 0.20% through 12:09Z. [F8, F9]
- `2026-04-02T11:50:26Z` Earliest anomaly: cert-watcher WARN that payment-service client cert CN=payments-client.orangeinvest.internal expires at 2026-04-02T12:15:00Z (25-minute early warning). [F11]
- `2026-04-02T12:15:00Z` Certificate notAfter reached. bank_api_error_rate jumps to exactly 100.0% and payment_service success rate collapses from ~99.25% to 2.51% at this bucket; request volume unchanged (~101 req/s) and p99 latency drops ~320ms -> ~140ms with no restarts. [F9, F10, F13]
- `2026-04-02T12:15:27Z` First hard failure: payment 45f233b6 marked PENDING_RETRY after the mTLS handshake with bank-partner-api was rejected. [F12]
- `2026-04-02T12:16:44Z` ERROR log 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' first seen; 311 of 314 WARN+ERROR lines are ERROR-level. [F12]
- `2026-04-02T12:32:00Z` Incident INC-005 [SEV-2] reported by support lead: ~40 customer tickets about failed fund transfers; payment-service error-rate panel green. []
- `2026-04-02T13:57:00Z` Last observed data: bank_api_error_rate still 100%, success rate still ~2%, HTTP error_rate still ~0.22% - failure persists unmitigated to the end of the window. [F8, F9, F10]

## Recommended actions
- **[immediate]** Immediately renew/reissue and hot-reload the payment-service outbound mTLS client certificate CN=payments-client.orangeinvest.internal (or roll the payment-service pod/credential mount to load a valid cert), then confirm bank-partner-api calls succeed. -- The expired client cert is the identified root cause (H1): the cert hit notAfter=2026-04-02T12:15:00Z, the first handshake rejection followed 27 seconds later, and bank_api_error_rate has been pinned at 100% with success rate ~2.5% ever since. Reloading a valid credential is the only change that can restore the bank leg. [F11, F12, F9, F10]
- **[immediate]** Replay/drain the PENDING_RETRY backlog for payments failed since 12:15Z and verify each transfer settles against the ledger, rather than attempting retries against the broken handshake. -- 'Drain vs genuinely failed' is an explicit assumption behind H1; the ~97% failed transfers absorbed as PENDING_RETRY must be reconciled so customers either get settled transfers or correct outcomes. [F12, F10]
- **[short_term]** Confirm the recovery objectively: watch bank_api_error_rate return to ~0.2% and payment_success_rate return to ~99% in the 12:15Z-style per-minute buckets, and confirm the 'tls: expired certificate (alert 45)' ERROR pattern stops. -- F9/F10 give the exact pre-incident baselines (0.20% bank error rate, ~99.25% success) and F12 the exact error signature; these are the acceptance criteria for a verified fix. [F9, F10, F12]
- **[short_term]** Replace the cert-watcher's WARN-only behavior with an automated pre-expiry renewal/alerts that page an on-call owner when an mTLS client credential on the bank path will expire within a defined lead time, and add a hard-fail guard so an expired credential cannot silently degrade into PENDING_RETRY. -- F11 shows the system had a 25-minute warning it did not act on; the mechanism only stayed silent because bank-call failures were absorbed instead of surfaced. [F11, F8, F12]
- **[short_term]** If the certificate reload does not restore the bank leg, pivot to discriminating observations: check whether a renewal or infra change occurred between 11:50Z and 12:15Z (coverage limit of the config/deployment tools per F1-F3), and obtain any bank-partner-side confirmation to distinguish an expired-cert rejection from a partner-side policy change (H2). -- H2 remains at 0.12 and bank-partner-api has no telemetry, so a residual alternative cannot be excluded by caller-side data alone; this action bounds that uncertainty. [F1, F2, F3, F5]
- **[long_term]** Add bank-leg health monitoring that alerts on bank_api_error_rate and payment_success_rate (per-partner where possible) instead of relying on the aggregate HTTP 5xx error_rate panel. -- F8 shows the aggregate error_rate metric is structurally blind to absorbed bank-call failures, which is the exact reason a SEV-2 customer-visible outage presented as a green dashboard. [F8, F9, F10]

## Data gaps
- No ledger-db, kafka or bank-partner-api telemetry was retrieved for 12:00-12:30Z, so a co-occurring shared-component degradation could not be directly excluded; bank-partner-api exposes no telemetry at all (F5), so its health can only be inferred from payment-service caller-side signals.
- The change/config listing tools each returned only a single record and it is unknown whether they cover infra-level changes (volume/IOPS, broker maintenance, certificate renewal, gateway/envoy config), so absence of a 12:00-12:30Z change may be a tool coverage limit rather than a true absence.
- No exact historical precedent for 'customer-reported payment failures while the HTTP error-rate dashboard was green' was found; closest matches (HIST-105, HIST-109, HIST-115) have match_score <= 2, so the mechanism is supported by analogy only.
- Per-endpoint/per-partner/per-status-code error breakdowns and 4xx vs 5xx vs timeout splits were not obtainable, so it cannot be stated whether one bank partner or client segment was more affected.
- Resource metrics (cpu_pct, memory_pct, db_pool_in_use, restarts) for payment-service and shared dependencies, plus queue depth / thread pool metrics, were never retrieved, so co-occurring resource saturation or an unbounded PENDING_RETRY backlog cannot be ruled out.
- Logs for transfer/settlement/ledger components were not reviewed, so it is unconfirmed whether PENDING_RETRY transactions were drained, dropped, or accumulated downstream.
- Observed metrics were unavailable: query_metrics(metric=error_rate, service=bank-partner-api) failed with not_found (tc-016), limiting independent partner-side verification.

## What would change this conclusion
- Bank-partner-side confirmation that our client certificate was still trusted and that the partner itself changed TLS policy or trust store at ~12:15Z would move the conclusion toward H2 (partner-side failure) rather than our expired credential.
- Evidence that the client certificate had actually been renewed before 12:15Z (or that a valid, newer cert was loaded and still rejected) would falsify H1 and implicate a partner-side or trust-chain cause.
- Discovery of an infra or certificate-renewal change between 11:50:26Z and 12:15:00Z invisible to the current change-listing tools would open a mechanism competing with the expiry itself and lower confidence in H1.
- Finding that failing bank calls were caused by timeouts or connection-pool exhaustion (rather than 'tls: expired certificate, alert 45') would support H3 or H4 and contradict the TLS-expiry mechanism.
- ledger-db or kafka telemetry showing an outage, IOPS throttling, or consumer lag beginning at 12:15Z would raise H4 and challenge H1 as the origin of the customer-visible failure.
- Evidence that the PENDING_RETRY backlog drained and the transfers eventually completed would invalidate the assumption that customers' transfers genuinely failed and lower severity/relevance of the root cause.

## Investigation stats
- latency 73.98s, LLM calls 10, tokens 60037 (in 45919 / out 14118)
- tool calls 16 (1 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
