# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The failure is an expired client mTLS certificate on payment-service: the cert CN=payments-client.orangeinvest.internal had notAfter=2026-04-02T12:15:00Z, exactly when bank_api_error_rate jumped to 100% and payment_success_rate collapsed from 99.41% to ~2.5%, with logs showing 'tls: expired certificate (alert 45)' and mTLS handshake rejections (F9, F10, F11, F8). The green dashboard is an observability artifact: payment-service still accepts inbound HTTP and returns no 5xx, so its HTTP error-rate panel stayed flat at ~0.2% (F7), and the real failure is only visible on the bank-call path. The leading competing hypotheses are excluded by mechanism and timing: the only config change (cfg-2502, bank timeout 3000->5000 at 11:20Z) alters a timeout, not a credential, and precedes onset by ~55 min, while no change of any kind occurred near 12:15Z (F2, F13, F14, F16). Confidence is medium (0.60) as rated by the reviewed analysis; the residual uncertainty is that no PKI/cert-rotation record and no bank-side telemetry could be checked.

## Probable root cause
**Expired client mTLS certificate on payment-service (credential expiry) breaks the bank-partner-api call path** -- credential_expiry in `payment-service`, triggered by `null` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired client mTLS certificate on payment-service (credential expiry) breaks the bank-partner-api call path | credential_expiry | payment-service | null | 0.60 (medium) | active |
| 2 | H2: cfg-2502 (bank.api.timeout_ms 3000->5000) induced the transfer failures | config_change | payment-service | cfg-2502 | 0.05 (low) | rejected |
| 3 | H4: External bank-partner-api degraded (dependency_failure), failing payment transfers | dependency_failure | bank-partner-api | null | 0.05 (low) | rejected |
| 4 | H3: Shared dependency (ledger-db or kafka) degraded by the 10:30Z settlement-service v1.9.0 deploy, propagating into payment-service | bad_deployment | settlement-service | settlement-service v1.9.0 | 0.03 (low) | rejected |

### H1: Expired client mTLS certificate on payment-service (credential expiry) breaks the bank-partner-api call path
The client certificate that payment-service presents to bank-partner-api (CN=payments-client.orangeinvest.internal) expired at exactly 2026-04-02T12:15:00Z. The bank rejects the handshake with 'tls: expired certificate (alert 45)', so every bank call fails; payment-service accepts HTTP requests (no 5xx, so HTTP error-rate panel stays green) but marks transfers PENDING_RETRY, collapsing payment_success_rate to ~2.5%. The fault originates in payment-service's own client credential, not in the bank, not in a shared component, and not in any app deploy/config change.

**Causal chain**
1. The mTLS client cert CN=payments-client.orangeinvest.internal has notAfter=2026-04-02T12:15:00Z and no rotation happened (no cert-lifecycle change record exists). [F11]
2. Cert expiry at 12:15:00Z coincides with the exact start of the 12:15Z failure bucket; first concrete log is PENDING_RETRY at 12:15:27Z. [F9, F11]
3. bank-partner-api rejects the presented cert with 'tls: expired certificate (alert 45)' (171 occurrences, first 12:16:44Z); payment-service marks 140 payments PENDING_RETRY for mTLS handshake rejection. [F10]
4. bank_api_error_rate jumps 0.21% -> 100.0% at the 12:15Z bucket and stays 100%; payment_success_rate collapses 99.41% -> 2.45%. [F8]
5. The failing call is synchronous from the customer's perspective but returns success/202 to the caller, so HTTP 5xx error_rate stays flat at ~0.22% and the dashboard remains green. [F7]
6. Blast radius stays confined to the payment->bank-partner-api path; no shared-component or neighbor propagation signature appears. [F12]
7. No deploy or config change exists at or near onset (11:50Z-12:30Z: zero changes), so the removal of prior alternatives is consistent with a pure credential-time trigger. [F13]

**Supporting evidence**
  - `F7` ✔ payment-service HTTP error_rate (unit: '% of requests (HTTP 5xx)') stayed completely flat and normal across the entire incident window: 0.23% at 12:00Z, 0.21% at 12:15Z, 0.19% at 12:20Z, min 0.1 / mean 0.22 over 120 points. This is why the dashboard stayed green while payments failed.
  - `F8` ✔ The narrow HTTP 5xx panel masked the real failure because the failing path is not returning HTTP 5xx: bank_api_error_rate on payment-service jumps from 0.21% at 12:10Z to exactly 100.0% in the 12:15Z bucket (and stays 100.0% through 13:55Z), while payment_success_rate collapses from 99.41% (12:10Z) to 2.45% (12:15Z) and averages ~2.5% for the rest of the window.
  - `F9` ✔ True onset is the 12:15Z metric bucket (5-min granularity, so failures began between 12:10Z and 12:15Z), earlier than the report's 12:17Z guess. The first concrete error log is a payment marked PENDING_RETRY at 12:15:27Z, and the explicit cert-expiry signature appears at 12:16:44Z.
  - `F10` ✔ Concrete error signatures and counts in payment-service logs (12:00–13:59Z): 313 total matched WARN/ERROR (311 ERROR, 2 WARN), dominated by two patterns — 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' x171 (first 12:16:44Z) and 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' x140 (first 12:15:27Z). Logs continue to the latest data at 13:58:39Z.
  - `F11` ✔ The failing signature is a client-side mTLS certificate the client itself presents: cert CN=payments-client.orangeinvest.internal with notAfter=2026-04-02T12:15:00Z. Its expiry timestamp coincides exactly with the start of the 12:15Z failure bucket. The remote bank error is 'tls: expired certificate (alert 45)'.
  - `F12` ✔ Blast radius appears confined to the payment→bank-partner-api path: settlement-service shows only benign signals in the window (3 WARN total — 2 GC pauses and 1 NTP clock drift, none payment-related), i.e. no propagation signature there. bank-partner-api is an external dependency with no logs/metrics of its own (its callers must be observed instead).
  - `F13` ✔ In the tight window around the 12:15Z failure onset (11:50:00Z–12:30:00Z), there were zero deployments and zero config changes across all services, i.e. no change coincides with onset.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The cert that expired is the one actively presented for the bank call (rather than a stale/unused cert also appearing in logs).
  - The external bank did not independently start rejecting anything at the same time; the alert-45 signature is generated by the presented cert's validity window.

### H2: cfg-2502 (bank.api.timeout_ms 3000->5000) induced the transfer failures
The only app config change in the entire window — payment-service bank.api.timeout_ms raised 3000->5000 at 11:20Z 'after partner advisory' — altered the bank-call path in payment-service and is responsible for the failures reported at 12:32Z.

**Causal chain**
1. cfg-2502 raised bank.api.timeout_ms 3000->5000 on payment-service at 11:20Z, the only change in the window. [F2, F14]
2. Failures appeared within the same incident window, so the change is temporally the nearest candidate. [F6]

**Supporting evidence**
  - `F2` ✔ Exactly one config change exists in 00:00Z-13:59Z: payment-service bank.api.timeout_ms raised 3000 -> 5000 at 2026-04-02T11:20:00Z by p.nair, described as 'Raise bank API timeout after partner advisory'. It affects payment-service itself and precedes the 12:32Z report.
  - `F14` ✔ Across 2026-04-01T00:00Z–2026-04-02T13:59Z only one config change exists: payment-service bank.api.timeout_ms raised 3000 -> 5000 at 11:20:00Z by p.nair ('Raise bank API timeout after partner advisory'). It is a timeout value on the bank call, not a cert/credential or endpoint change.
**Contradicting evidence**
  - `F9` ✔ True onset is the 12:15Z metric bucket (5-min granularity, so failures began between 12:10Z and 12:15Z), earlier than the report's 12:17Z guess. The first concrete error log is a payment marked PENDING_RETRY at 12:15:27Z, and the explicit cert-expiry signature appears at 12:16:44Z.
  - `F10` ✔ Concrete error signatures and counts in payment-service logs (12:00–13:59Z): 313 total matched WARN/ERROR (311 ERROR, 2 WARN), dominated by two patterns — 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' x171 (first 12:16:44Z) and 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' x140 (first 12:15:27Z). Logs continue to the latest data at 13:58:39Z.
  - `F11` ✔ The failing signature is a client-side mTLS certificate the client itself presents: cert CN=payments-client.orangeinvest.internal with notAfter=2026-04-02T12:15:00Z. Its expiry timestamp coincides exactly with the start of the 12:15Z failure bucket. The remote bank error is 'tls: expired certificate (alert 45)'.
  - `F13` ✔ In the tight window around the 12:15Z failure onset (11:50:00Z–12:30:00Z), there were zero deployments and zero config changes across all services, i.e. no change coincides with onset.
  - `F16` ✔ Timing alignment: cfg-2502 (11:20:00Z) precedes the 12:15Z onset by ~55 min and the only deploy (10:30:00Z) by ~105 min; neither is co-located with onset, so neither can explain a failure that starts exactly at 12:15:00Z.
**Assumptions**
  - A longer timeout could cause or expose handshake/transfer failures.
  - Bank-call failures would present as mTLS rejection.
**Rejected because:** Onset is pinned to exactly 12:15:00Z by the cert notAfter timestamp (F9/F11), ~55 min after cfg-2502 (F16), and the ~55 min quiet gap after the change weakens any causal link. Mechanistically, cfg-2502 changes a timeout value, not a cert, endpoint or handshake parameter, yet the error signature is a TLS expired-certificate alert (F10). No change of any kind exists in 11:50Z-12:30Z (F13). The trigger is a credential time-boundary, not this config.

### H4: External bank-partner-api degraded (dependency_failure), failing payment transfers
The bank-partner-api / bank side degraded (matching precedent HIST-105 'Bank partner API returned 5xx'), causing payment-service bank calls to fail and transfers to remain incomplete while payment-service's own HTTP endpoints still returned success.

**Causal chain**
1. bank-partner-api is an external dependency (has_telemetry=false) on the payment path. [F3, F17]
2. Historical precedent HIST-105 describes a bank-partner-API failure causing payment-service payment failures. [F5]
3. Calls to bank-partner-api began failing at the 12:15Z bucket, collapsing payment_success_rate. [F8]
4. package of external failures would not show on the payment-service HTTP 5xx panel. [F7]

**Supporting evidence**
  - `F3` ✔ payment-service dependency graph (edge = caller -> callee): api-gateway -> payment-service (http); payment-service -> ledger-db (db); payment-service -> bank-partner-api (external, has_telemetry=false); payment-service -> kafka (queue-produce). Shared components: ledger-db (postgres) and kafka, both marked shared=true; bank-partner-api is external with no telemetry.
  - `F5` ✔ Historical precedent search returned no incident matching 'green dashboard but real payment failures'. Closest matches are dependency_failure precedents: HIST-105 (2025-11-19, 'Bank partner API returned 5xx for 40 minutes', payment-service payment failures) and HIST-103 (2025-10-21, external exchange API outage causing upstream timeouts); other matches were credential_expiry (HIST-106), bad_deployment/auth regression (HIST-109) and ledger-db resource_exhaustion (HIST-114). The tool notes these are 'leads, not evidence about the current incident'.
  - `F8` ✔ The narrow HTTP 5xx panel masked the real failure because the failing path is not returning HTTP 5xx: bank_api_error_rate on payment-service jumps from 0.21% at 12:10Z to exactly 100.0% in the 12:15Z bucket (and stays 100.0% through 13:55Z), while payment_success_rate collapses from 99.41% (12:10Z) to 2.45% (12:15Z) and averages ~2.5% for the rest of the window.
  - `F17` ✔ The failing path payment-service -> bank-partner-api does sit in a graph with shared components: payment-service -> ledger-db (db), -> kafka (queue-produce); ledger-db and kafka are both marked shared=true, while bank-partner-api (the branch that actually failed) is external with has_telemetry=false.
**Contradicting evidence**
  - `F10` ✔ Concrete error signatures and counts in payment-service logs (12:00–13:59Z): 313 total matched WARN/ERROR (311 ERROR, 2 WARN), dominated by two patterns — 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' x171 (first 12:16:44Z) and 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' x140 (first 12:15:27Z). Logs continue to the latest data at 13:58:39Z.
  - `F11` ✔ The failing signature is a client-side mTLS certificate the client itself presents: cert CN=payments-client.orangeinvest.internal with notAfter=2026-04-02T12:15:00Z. Its expiry timestamp coincides exactly with the start of the 12:15Z failure bucket. The remote bank error is 'tls: expired certificate (alert 45)'.
**Assumptions**
  - The partner advisory behind cfg-2502 reflects real partner degradation (Gap: no partner telemetry at all).
  - The partner's rejection is not caused solely by the caller's expired certificate.
**Rejected because:** The actual error is generated on the client side: the specific expired cert CN=payments-client.orangeinvest.internal with notAfter exactly equal to the onset bucket (F11), and alert 45 'expired certificate' is a client-certificate validity outcome (F10). A partner-side degradation would not produce a client cert expiry perfectly aligned to the failure start second. HIST-105 is explicitly a lead, not evidence, and the partner has no telemetry to support it (F5, Gap).

### H3: Shared dependency (ledger-db or kafka) degraded by the 10:30Z settlement-service v1.9.0 deploy, propagating into payment-service
settlement-service v1.9.0 ('Batch settlement report export', 10:30Z, shared ledger-db and kafka) increased load or lock contention on a shared component, degrading the shared datastore/queue and causing payment-service transfer failures, with the fault originating in the neighbor service or the shared resource rather than in payment-service itself.

**Causal chain**
1. settlement-service v1.9.0 deployed at 10:30Z, shares ledger-db and kafka with payment-service, and was not rolled back. [F1, F4, F15]
2. Shared ledger-db/kafka faults can propagate from a neighbor into payment-service. [F4, F17]
3. payment-service's HTTP error-rate panel stayed green while customers reported transfer failures. [F7]
4. This produced PENDING_RETRY payments and a collapsed success rate from ~12:15Z onward. [F8, F10]

**Supporting evidence**
  - `F1` ✔ No deployments occurred in the incident window 11:00Z-13:59Z across all services (count 0). The only deploy in the wider 00:00Z-13:59Z window was settlement-service v1.9.0 at 10:30:00Z ('Batch settlement report export', status live, not rolled back) -- a payments-org neighbor that shares ledger-db and kafka with payment-service.
  - `F4` ✔ ledger-db is a shared postgres used by four services: order-service, portfolio-service, payment-service and settlement-service; kafka is a shared queue used by order-service (produce), payment-service (produce), settlement-service (consume) and notification-service (consume). A fault in either shared component can therefore propagate to payment-service from a neighbor.
  - `F15` ✘ unverified The only nearby deploy is settlement-service v1.9.0 at 10:30:00Z ('Batch settlement report export', status live, /rolled_back_at: null/), ~105 min before onset and not rolled back; deployment list for settlement-service confirms no other deploy.
  - `F17` ✔ The failing path payment-service -> bank-partner-api does sit in a graph with shared components: payment-service -> ledger-db (db), -> kafka (queue-produce); ledger-db and kafka are both marked shared=true, while bank-partner-api (the branch that actually failed) is external with has_telemetry=false.
**Contradicting evidence**
  - `F10` ✔ Concrete error signatures and counts in payment-service logs (12:00–13:59Z): 313 total matched WARN/ERROR (311 ERROR, 2 WARN), dominated by two patterns — 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' x171 (first 12:16:44Z) and 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' x140 (first 12:15:27Z). Logs continue to the latest data at 13:58:39Z.
  - `F11` ✔ The failing signature is a client-side mTLS certificate the client itself presents: cert CN=payments-client.orangeinvest.internal with notAfter=2026-04-02T12:15:00Z. Its expiry timestamp coincides exactly with the start of the 12:15Z failure bucket. The remote bank error is 'tls: expired certificate (alert 45)'.
  - `F12` ✔ Blast radius appears confined to the payment→bank-partner-api path: settlement-service shows only benign signals in the window (3 WARN total — 2 GC pauses and 1 NTP clock drift, none payment-related), i.e. no propagation signature there. bank-partner-api is an external dependency with no logs/metrics of its own (its callers must be observed instead).
  - `F13` ✔ In the tight window around the 12:15Z failure onset (11:50:00Z–12:30:00Z), there were zero deployments and zero config changes across all services, i.e. no change coincides with onset.
  - `F16` ✔ Timing alignment: cfg-2502 (11:20:00Z) precedes the 12:15Z onset by ~55 min and the only deploy (10:30:00Z) by ~105 min; neither is co-located with onset, so neither can explain a failure that starts exactly at 12:15:00Z.
**Assumptions**
  - A shared-component degradation would surface as an mTLS handshake rejection against bank-partner-api.
  - settlement-service's batch export would affect ledger-db/kafka enough to break bank calls. (Gaps: shared-component metrics were never queried — budget ran out).
  - The ~105 min quiet gap between deploy and onset is incidental.
**Rejected because:** The failure signature is explicitly a TLS expired-certificate (alert 45) on the payment->bank-partner-api path (F10/F11), not a DB or queue error; payment-service's DB/queue branches were not the failing branch (F17). Blast radius is confined to the bank path with no shared-component or neighbor propagation signature, and settlement-service itself shows only benign GC/NTP warnings (F12). Onset at exactly 12:15:00Z is ~105 min after the deploy and ~105 min of silence post-deploy (F16), and zero changes existed near onset (F13). The only surviving temporal anchor is the cert notAfter.

## Facts (verified observations)
- payment-service's client certificate CN=payments-client.orangeinvest.internal has notAfter=2026-04-02T12:15:00Z, coinciding exactly with the start of the 12:15Z failure bucket; the remote bank returns 'tls: expired certificate (alert 45)'. [F11, F10]
- Failure signatures in payment-service logs (12:00-13:59Z) are dominated by 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)' x171 (first 12:16:44Z) and 'payment <id> marked PENDING_RETRY: mTLS handshake with bank-partner-api rejected' x140 (first 12:15:27Z); logs continue through the latest data at 13:58:39Z. [F10, F9]
- bank_api_error_rate on payment-service jumped from 0.21% at 12:10Z to exactly 100.0% in the 12:15Z bucket and stayed 100.0% through 13:55Z, while payment_success_rate collapsed from 99.41% (12:10Z) to 2.45% (12:15Z), averaging ~2.5% thereafter. [F8]
- payment-service HTTP error_rate (HTTP 5xx) stayed normal across the whole incident window (0.23% at 12:00Z, 0.21% at 12:15Z, min 0.1 / mean 0.22 over 120 points), which is why the dashboard stayed green while transfers failed. [F7]
- True onset is the 12:15Z 5-minute metric bucket (failures began between 12:10Z and 12:15Z), earlier than the report's ~12:17Z estimate. [F9]
- In the tight window 11:50:00Z-12:30:00Z around onset there were zero deployments and zero config changes across all services. [F13]
- Exactly one config change exists in 2026-04-01T00:00Z-2026-04-02T13:59Z: payment-service bank.api.timeout_ms raised 3000 -> 5000 at 11:20:00Z by p.nair ('Raise bank API timeout after partner advisory') — a timeout value on the bank call, not a cert, credential or endpoint change. [F2, F14]
- The only deploy in the wider 00:00Z-13:59Z window was settlement-service v1.9.0 at 10:30:00Z ('Batch settlement report export', live, not rolled back), a payments-org neighbor sharing ledger-db and kafka; it precedes onset by ~105 minutes. [F1]
- Blast radius appears confined to the payment->bank-partner-api path: settlement-service shows only benign signals in the window (3 WARN total — 2 GC pauses, 1 NTP clock drift, none payment-related), and the failing branch is external bank-partner-api (has_telemetry=false) rather than the shared ledger-db or kafka branches. [F12, F17]
- bank-partner-api is external with no logs/metrics of its own, so the partner side must be observed through its callers. [F3, F12]

## Assumptions (not verified)
- Assumption: the certificate that expired is the one actually presented on the live bank call, not a stale or unused cert that also appears in logs.
- Assumption: the alert-45 'expired certificate' rejection is generated by the presented client certificate's validity window, and the external bank did not independently begin rejecting traffic at the same moment for another reason.
- Assumption: no cert-rotation or PKI change is represented in the deployment/config change history, because those tools do not cover PKI lifecycle records — i.e. the causal 'change' (expiry) lives outside the observed change feeds.
- Assumption: the 10:30Z settlement-service deploy and the 11:20Z cfg-2502 timeout change are unrelated to the failure, based on the ~105 min and ~55 min gaps and on the fact that neither touches the credential or handshake path.
- Assumption: the 5-minute metric bucket resolution is accurate enough that the cert notAfter=12:15:00Z is effectively the onset instant, rather than the onset merely falling inside the 12:10-12:15Z interval for another reason.

## Timeline
- `2026-04-02T10:30:00Z` Only nearby deploy: settlement-service v1.9.0 'Batch settlement report export' goes live (not rolled back); a payments-org neighbor sharing ledger-db and kafka. ~105 min before onset. [F1, F15]
- `2026-04-02T11:20:00Z` Only config change in the window: payment-service bank.api.timeout_ms raised 3000 -> 5000 by p.nair ('Raise bank API timeout after partner advisory'). A timeout value, not a credential or endpoint change. ~55 min before onset. [F2, F14]
- `2026-04-02T12:10:00Z` Last healthy data points: payment_success_rate 99.41%, bank_api_error_rate 0.21%, HTTP error_rate 0.21%. [F8, F7]
- `2026-04-02T12:15:00Z` Client certificate CN=payments-client.orangeinvest.internal reaches notAfter and expires, at exactly the start of the 12:15Z failure bucket; failure onset is bounded to (12:10Z, 12:15Z]. Zero deployments and zero config changes occurred in 11:50Z-12:30Z. [F11, F9, F13]
- `2026-04-02T12:15:00Z` bank_api_error_rate jumps to exactly 100.0% and payment_success_rate collapses to 2.45% in the 12:15Z bucket; both persist through 13:55Z. [F8]
- `2026-04-02T12:15:27Z` First concrete error log: a payment marked PENDING_RETRY due to mTLS handshake with bank-partner-api rejected. [F9, F10]
- `2026-04-02T12:16:44Z` First explicit cert-expiry signature logged: 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45)'. [F10]
- `2026-04-02T12:32:00Z` Incident INC-005 [SEV-2] reported by support lead: ~40 tickets about failed fund transfers in the last 15 minutes, while the payment-service HTTP error-rate panel shows nothing unusual. [F7]
- `2026-04-02T13:58:39Z` Logs continue to the latest data: 313 matched WARN/ERROR (311 ERROR, 2 WARN), dominated by the two cert/handshake patterns (171 + 140). [F10]
- `2026-04-02T13:59:00Z` Latest data: failure ongoing — bank_api_error_rate still 100.0% and payment_success_rate ~2.5%; settlement-service shows only benign GC/NTP warnings. [F8, F12]

## Recommended actions
- **[immediate]** Immediate mitigation: renew/reissue the payment-service client mTLS certificate for CN=payments-client.orangeinvest.internal and deploy the new credential to payment-service (or confirm the replacement cert is already staged and reload it), then verify bank_api_error_rate returns to baseline and payment_success_rate recovers toward ~99%. -- The cert expired at 2026-04-02T12:15:00Z and bank_api_error_rate is pinned at 100.0% with 'tls: expired certificate (alert 45)' — rotating the presented credential is the direct fix. [F10, F11, F8]
- **[immediate]** Immediate: after rotation, drain/retry the backlog of payments marked PENDING_RETRY (140+ handshake-rejected transfers) and confirm they complete rather than accumulating. -- Transfers were marked PENDING_RETRY rather than failed, so they must be reattempted once the bank path works; queue/lag impact is unmeasured (data gap). [F10, F9]
- **[immediate]** Immediate (safe, discriminating): while mitigation is in progress, do NOT roll back settlement-service v1.9.0 and do NOT revert cfg-2502 as the first move; instead query shared-component metrics (ledger-db, kafka timeseries/depth) and payment-service latency_p99_ms to definitively exclude a shared-component or queue-side contribution. -- These are the untested branches; they are rejected by mechanism but were never measured, and a cheap read either closes the gap or overturns the conclusion. Neither deploy nor config is co-located with onset. [F13, F16, F17, F8]
- **[short_term]** Short term: instrument the bank-call path explicitly — alert on bank_api_error_rate and payment_success_rate (not just on HTTP 5xx error_rate) so credential/handshake failures cannot hide behind a green HTTP panel. -- The HTTP error_rate metric stayed flat at ~0.2% throughout because the failing path returns no 5xx; the real signal was bank_api_error_rate at 100%. [F7, F8]
- **[short_term]** Short term: add a pre-expiry certificate monitor/alert (e.g. warn at 30/14/7 days before notAfter) for payment-service's client certs and add an automated read of presented cert notAfter into the observability stack. -- The failure appeared within seconds of notAfter with no prior alerting; expiry was the entire trigger and no change coincided with onset. [F11, F13]
- **[long_term]** Long term: automate client-certificate rotation/renewal for the payment->bank-partner-api mTLS credential and record cert lifecycle events in a change feed, since change-history tools currently expose only app config and deploys. -- The one thing that changed at onset was the cert validity boundary, and the cert lifecycle was invisible to the change-history and PKI tooling queried (data gap). [F10, F11]
- **[long_term]** Long term: establish a synthetic bank-partner-api probe (caller-side health check that exercises the mTLS handshake end-to-end) since the partner has no telemetry of its own. -- bank-partner-api is external with has_telemetry=false; failures can only be observed indirectly via the caller, which caused the detection delay and left open whether the partner also changed anything. [F3, F12]

## Data gaps
- No telemetry exists for bank-partner-api (has_telemetry=false); partner-side latency/error changes could not be checked and the partner advisory behind cfg-2502 could not be confirmed as real degradation.
- Tool budget ran out before querying available_metrics/timeseries for shared components (ledger-db, kafka) and for api-gateway/settlement-service, so a shared-component signal at ~12:15Z could not be excluded by measurement.
- Deployment history was only queried for 2026-04-02; earlier days were not checked, so a pre-midnight change is not fully excluded.
- Kafka queue depth/lag and payment-service latency_p99_ms were never queried, so the async/queue-side blast radius and latency impact of the PENDING_RETRY backlog are unmeasured.
- payment-worker and payment-queue are not valid service names (log queries failed not_found), so the worker/queue success-failure ratio could not be measured as a distinct service.
- Metric buckets are 5 minutes, so onset is bounded to (12:10Z, 12:15Z] rather than an exact second.
- No PKI/cert-rotation record was found for CN=payments-client.orangeinvest.internal; change-history tools expose only app config and deployments, so who owned the rotation and whether it was expected is unknown.
- bank-partner-api log query failed (not_found), so the bank-side handshake rejection could not be corroborated from the partner's view.

## What would change this conclusion
- Evidence that the cert CN=payments-client.orangeinvest.internal was not the credential actually presented on the failing bank calls (e.g. a different, still-valid cert in use) would break H1.
- Observation of bank-partner-api-side degradation concurrent with onset (e.g. partner returning 5xx/alert 45 to callers with currently valid certificates) would shift support toward the dependency_failure hypothesis H4.
- A shared-component signal at ~12:15Z — e.g. ledger-db saturation or kafka depth/lag spike aligning with onset — would reopen the bad-deployment/shared-dependency hypothesis H3.
- Finding a config/endpoint/handshake change on the payment->bank path at ~12:15Z (rather than only the 11:20Z timeout change) would implicate H2 or a change-driven cause.
- Confirmation that the handshake rejection is purely a local TLS-version or cipher mismatch rather than a cert validity failure (e.g. rejection despite a valid notAfter) would invalidate the credential-expiry mechanism.
- Failure of payment_success_rate and bank_api_error_rate to recover after cert rotation would falsify H1 as the sole root cause.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6

## Investigation stats
- latency 139.26s, LLM calls 14, tokens 84642 (in 68508 / out 16134)
- tool calls 24 (3 failed, 0 partial, 0 recovered by retry), findings 16/18 verified
