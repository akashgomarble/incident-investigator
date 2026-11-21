# Incident investigation report: INC-005
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The customer-visible payment failures are caused by the expiry of payment-service's outbound mTLS client certificate (CN=payments-client.orangeinvest.internal, notAfter=2026-04-02T12:15:00Z) used to authenticate to the external bank-partner-api (H1, confidence 0.88/high). payment-service logs explicitly record "remote error: tls: expired certificate (alert 45)" naming our own client certificate, an independent cert-watcher warned 25 minutes in advance, and the first failure landed ~27 seconds after expiry (F11, F12). Because the failed transfers are recorded as PENDING_RETRY rather than HTTP 5xx, the 5xx-only error-rate panel stayed green (~0.22% average) while transfers effectively stopped succeeding (payment_success_rate fell to ~2.1-2.7%) and bank_api_error_rate jumped to 100% (F8, F9, F10). Competing hypotheses (bank-side outage, settlement-service deploy, timeout config change) are positively contradicted: none can produce a TLS alert 45 naming our certificate, and H3/H4 cannot explain TLS-layer rejections 45–105 minutes after their trigger events.

## Probable root cause
**Expired client certificate for bank-partner-api mTLS (our side)** -- credential_expiry in `payment-service (outbound bank connector credential)`, triggered by `null` (confidence 0.88, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Expired client certificate for bank-partner-api mTLS (our side) | credential_expiry | payment-service (outbound bank connector credential) | null | 0.88 (high) | active |
| 2 | H2: Bank/acquirer-side outage (HIST-105 pattern) | dependency_failure | bank-partner-api (external provider) | null | 0.12 (low) | active |
| 3 | H3: settlement-service v1.9.0 CSV exporter saturates shared ledger-db / kafka | bad_deployment | settlement-service | tc-004 | 0.05 (low) | active |
| 4 | H4: bank.api.timeout_ms raise (3000->5000) at 11:20Z causes the failures | config_change | payment-service (config: bank.api.timeout_ms) | tc-005 | 0.04 (low) | active |

### H1: Expired client certificate for bank-partner-api mTLS (our side)
The client certificate CN=payments-client.orangeinvest.internal used by payment-service to authenticate to the external bank-partner-api expired at 2026-04-02T12:15:00Z. With no renewal/rotation having occurred, every outbound transfer call fails the mTLS handshake, payments are marked PENDING_RETRY rather than 5xx, so the HTTP error-rate panel stays green while customers see failed transfers.

**Causal chain**
1. Client certificate CN=payments-client.orangeinvest.internal has notAfter=2026-04-02T12:15:00Z and no rotation occurs [F11, F12]
2. Cert-watcher warns at 11:50:26Z that the cert expires in 25 minutes [F12]
3. At 12:15:00Z the cert expires; first transfer failure at 12:15:27Z (~27s later) [F12, F11]
4. payment-service -> bank-partner-api mTLS handshakes are rejected: 'remote error: tls: expired certificate (alert 45)' (171 occ) and 'mTLS handshake rejected' (140 occ) [F11]
5. bank_api_error_rate jumps 0.20% -> 50.1% (12:14Z) -> 100% (12:16Z onward) [F9]
6. Transfers fail fast at the handshake: payment_success_rate collapses to ~2% and latency_p99 DROPS to ~132-144ms with flat CPU [F10, F13]
7. Failures surface as async PENDING_RETRY, never as HTTP 5xx, so error_rate stays flat ~0.22% and the dashboard looks green [F8, F11]

**Supporting evidence**
  - `F8` ✔ payment-service error_rate (HTTP 5xx) stayed flat/normal throughout 11:00-14:00Z, avg ~0.22% (min 0.1, max 0.32), showing NO incident — this is why the dashboard looked green. The panel only measures 'HTTP 5xx' and the failures are async PENDING_RETRY, so they never register as 5xx.
  - `F9` ✔ The transfer-specific signal bank_api_error_rate jumped from a stable ~0.2% to 100%: 0.20% at 12:12Z, 50.1% at the 12:14Z bucket, then 100% from 12:16Z onward through 13:56Z (mean 58.42). This is the real, hidden failure signal.
  - `F10` ✔ payment_success_rate collapsed in the same window: ~99.17% at 12:04Z, partial 38.93% at 12:12Z bucket, then ~2.1-2.7% from 12:20Z onward (min 1.44, last 2.13). Customer transfers essentially stopped succeeding.
  - `F11` ✔ payment-service ERROR logs begin at 12:15:27Z and are dominated by two signatures: 'mTLS handshake with bank-partner-api rejected' (140 occurrences, first 12:15:27Z) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (171 occurrences, first 12:16:44Z). Payments are marked PENDING_RETRY rather than failed.
  - `F12` ✔ A cert-watcher warning at 11:50:26Z foreshadowed the expiry: 'client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)'. The first transfer failure (12:15:27Z) follows the 12:15:00Z cert expiry by ~27 seconds.
  - `F13` ✔ payment-service latency_p99_ms DROPPED rather than rose during the incident: ~320-327ms before 12:04Z, falling to ~132-144ms from 12:20Z onward. Resource metric cpu_pct was flat at ~30% (27.41-34.25) across 11:00-14:00Z, showing no resource saturation — consistent with fast TLS-handshake rejection rather than overload.
  - `F14` ✔ bank-partner-api is an external dependency with NO logs/metrics of its own; it can only be observed through its callers (payment-service). This means any 'bank side' claim must be inferred from payment-service's outbound call metrics.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The certificate was not rotated before 12:15Z (no rotation event appears in the config-change results, but no credential-rotation tool was available to confirm)
  - No bank-side change coincided; bank-partner-api has no telemetry so its own health cannot be observed

### H2: Bank/acquirer-side outage (HIST-105 pattern)
The external bank-partner-api itself is failing (5xx/timeout/its own TLS or infrastructure problem) starting ~12:14Z, so our transfers fail at the partner leg. Because bank-partner-api has no telemetry, the failure is only visible indirectly as payment-service's outbound errors, and support's 'bank side' suspicion is right in substance.

**Causal chain**
1. bank-partner-api degrades/fails around 12:14Z (unobservable directly) [F14]
2. payment-service outbound calls to bank-partner-api fail [F9]
3. bank_api_error_rate rises to 100% and payment_success_rate collapses [F9, F10]
4. External leg has no telemetry, so the dashboard cannot show it [F3, F14]
5. Historic precedent HIST-105 matches: bank partner API 5xx for 40 min, resolved by waiting [F5]

**Supporting evidence**
  - `F5` ✔ Precedent HIST-105 (2025-11-19, payment-service): 'Payment failures: bank partner API outage', root cause 'Bank partner API returned 5xx for 40 minutes (their incident)', category dependency_failure, resolution 'Waited for partner; queued retries' — the closest prior match to today's symptom of customer-visible payment failures from the bank/acquirer leg. HIST-103 (order/exchange-gateway, 2025-10-21) is the same external-partner-outage pattern.
  - `F9` ✔ The transfer-specific signal bank_api_error_rate jumped from a stable ~0.2% to 100%: 0.20% at 12:12Z, 50.1% at the 12:14Z bucket, then 100% from 12:16Z onward through 13:56Z (mean 58.42). This is the real, hidden failure signal.
  - `F10` ✔ payment_success_rate collapsed in the same window: ~99.17% at 12:04Z, partial 38.93% at 12:12Z bucket, then ~2.1-2.7% from 12:20Z onward (min 1.44, last 2.13). Customer transfers essentially stopped succeeding.
  - `F14` ✔ bank-partner-api is an external dependency with NO logs/metrics of its own; it can only be observed through its callers (payment-service). This means any 'bank side' claim must be inferred from payment-service's outbound call metrics.
**Contradicting evidence**
  - `F11` ✔ payment-service ERROR logs begin at 12:15:27Z and are dominated by two signatures: 'mTLS handshake with bank-partner-api rejected' (140 occurrences, first 12:15:27Z) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (171 occurrences, first 12:16:44Z). Payments are marked PENDING_RETRY rather than failed.
  - `F12` ✔ A cert-watcher warning at 11:50:26Z foreshadowed the expiry: 'client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)'. The first transfer failure (12:15:27Z) follows the 12:15:00Z cert expiry by ~27 seconds.
  - `F13` ✔ payment-service latency_p99_ms DROPPED rather than rose during the incident: ~320-327ms before 12:04Z, falling to ~132-144ms from 12:20Z onward. Resource metric cpu_pct was flat at ~30% (27.41-34.25) across 11:00-14:00Z, showing no resource saturation — consistent with fast TLS-handshake rejection rather than overload.
**Assumptions**
  - The error strings would be different if truly bank-side; this hypothesis must explain away the explicit 'expired certificate' and 'alert 45' messages
**Rejected because:** null

### H3: settlement-service v1.9.0 CSV exporter saturates shared ledger-db / kafka
settlement-service was deployed at 10:30Z with a new CSV exporter that writes the shared ledger-db (and consumes kafka), the same two shared components payment-service uses. The exporter's write/scan load degrades ledger-db or kafka, causing payment-service transfers to fail while its own 5xx panel stays green.

**Causal chain**
1. settlement-service v1.9.0 deployed 10:30Z ('new CSV exporter'), live, not rolled back [F1]
2. Its exporter writes shared ledger-db; settlement-service consumes kafka and writes ledger-db, sitting on both shared components payment-service uses [F4]
3. Shared ledger-db/kafka contention degrades payment-service transfers *(assumed)*
4. Customer transfers fail while payment-service HTTP 5xx stays flat [F8, F10]

**Supporting evidence**
  - `F1` ✔ Across ALL services in 10:00-13:59Z only one deploy occurred: settlement-service v1.9.0 at 2026-04-02T10:30:00Z ('new CSV exporter'), status live, not rolled back. payment-service itself had no deploy in the window.
  - `F4` ✔ ledger-db and kafka are flagged shared=true. ledger-db has four writer services (order-service, portfolio-service, payment-service, settlement-service), and settlement-service consumes kafka while also writing ledger-db — so settlement-service sits on both shared components payment-service uses.
**Contradicting evidence**
  - `F11` ✔ payment-service ERROR logs begin at 12:15:27Z and are dominated by two signatures: 'mTLS handshake with bank-partner-api rejected' (140 occurrences, first 12:15:27Z) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (171 occurrences, first 12:16:44Z). Payments are marked PENDING_RETRY rather than failed.
  - `F12` ✔ A cert-watcher warning at 11:50:26Z foreshadowed the expiry: 'client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)'. The first transfer failure (12:15:27Z) follows the 12:15:00Z cert expiry by ~27 seconds.
  - `F13` ✔ payment-service latency_p99_ms DROPPED rather than rose during the incident: ~320-327ms before 12:04Z, falling to ~132-144ms from 12:20Z onward. Resource metric cpu_pct was flat at ~30% (27.41-34.25) across 11:00-14:00Z, showing no resource saturation — consistent with fast TLS-handshake rejection rather than overload.
  - `F10` ✔ payment_success_rate collapsed in the same window: ~99.17% at 12:04Z, partial 38.93% at 12:12Z bucket, then ~2.1-2.7% from 12:20Z onward (min 1.44, last 2.13). Customer transfers essentially stopped succeeding.
  - `F9` ✔ The transfer-specific signal bank_api_error_rate jumped from a stable ~0.2% to 100%: 0.20% at 12:12Z, 50.1% at the 12:14Z bucket, then 100% from 12:16Z onward through 13:56Z (mean 58.42). This is the real, hidden failure signal.
**Assumptions**
  - That ledger-db/kafka contention would produce TLS handshake rejections on the bank leg (it would not)
  - That a ~2h quiet gap between deploy (10:30Z) and symptom onset (12:14Z) reflects delayed resource exhaustion
  - That no other service writing ledger-db shows degradation

### H4: bank.api.timeout_ms raise (3000->5000) at 11:20Z causes the failures
The config change raising the bank API timeout from 3000ms to 5000ms at 11:20Z lets more calls hang into the failure zone, converting borderline partner calls into timeouts/PENDING_RETRY, producing customer-visible failures that never register as HTTP 5xx.

**Causal chain**
1. bank.api.timeout_ms raised 3000->5000 at 11:20Z [F2]
2. Longer timeout lets more calls hit the failing partner leg *(assumed)*
3. Transfers move to PENDING_RETRY, keeping HTTP 5xx flat [F8, F11]
4. Customer-visible failure with a green dashboard [F8, F10]

**Supporting evidence**
  - `F2` ✔ Exactly one config change in the window: payment-service 'bank.api.timeout_ms' raised 3000 -> 5000 at 2026-04-02T11:20:00Z by p.nair ('Raise bank API timeout after partner advisory') — i.e. the bank/acquirer call timeout was changed roughly 72 min before the incident was reported (12:32Z).
  - `F8` ✔ payment-service error_rate (HTTP 5xx) stayed flat/normal throughout 11:00-14:00Z, avg ~0.22% (min 0.1, max 0.32), showing NO incident — this is why the dashboard looked green. The panel only measures 'HTTP 5xx' and the failures are async PENDING_RETRY, so they never register as 5xx.
**Contradicting evidence**
  - `F11` ✔ payment-service ERROR logs begin at 12:15:27Z and are dominated by two signatures: 'mTLS handshake with bank-partner-api rejected' (140 occurrences, first 12:15:27Z) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (171 occurrences, first 12:16:44Z). Payments are marked PENDING_RETRY rather than failed.
  - `F12` ✔ A cert-watcher warning at 11:50:26Z foreshadowed the expiry: 'client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z)'. The first transfer failure (12:15:27Z) follows the 12:15:00Z cert expiry by ~27 seconds.
  - `F9` ✔ The transfer-specific signal bank_api_error_rate jumped from a stable ~0.2% to 100%: 0.20% at 12:12Z, 50.1% at the 12:14Z bucket, then 100% from 12:16Z onward through 13:56Z (mean 58.42). This is the real, hidden failure signal.
  - `F13` ✔ payment-service latency_p99_ms DROPPED rather than rose during the incident: ~320-327ms before 12:04Z, falling to ~132-144ms from 12:20Z onward. Resource metric cpu_pct was flat at ~30% (27.41-34.25) across 11:00-14:00Z, showing no resource saturation — consistent with fast TLS-handshake rejection rather than overload.
**Assumptions**
  - That a timeout raise alone can produce an abrupt 100% bank_api_error_rate 55 minutes later
  - That it would produce TLS-layer rejections rather than timeouts

## Facts (verified observations)
- payment-service ERROR logs begin at 2026-04-02T12:15:27Z and are dominated by 'mTLS handshake with bank-partner-api rejected' (140 occurrences) and 'bank-partner-api call failed: remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z' (171 occurrences, first 12:16:44Z); payments are marked PENDING_RETRY rather than failed. [F11]
- An independent cert-watcher warned at 11:50:26Z that the client certificate CN=payments-client.orangeinvest.internal would expire in 25 minutes (notAfter=2026-04-02T12:15:00Z); the first transfer failure at 12:15:27Z followed expiry by ~27 seconds. [F12]
- The payment-service HTTP 5xx error_rate panel stayed flat and normal across 11:00-14:00Z (avg ~0.22%, range 0.1-0.32%), because the panel measures only HTTP 5xx and these failures are async PENDING_RETRY events. [F8]
- The transfer-specific signal bank_api_error_rate jumped from a stable ~0.2% to 50.1% at the 12:14Z bucket and 100% from 12:16Z onward through 13:56Z, and payment_success_rate collapsed from ~99.17% (12:04Z) to ~2.1-2.7% from 12:20Z onward. [F9, F10]
- payment-service latency_p99_ms DROPPED during the incident (from ~320-327ms before 12:04Z to ~132-144ms from 12:20Z) while cpu_pct stayed flat at ~30%, consistent with fast TLS-handshake rejection rather than resource saturation or call-timeout overload. [F13]
- payment-service had no deploy in the incident window; the only deploy across all services was settlement-service v1.9.0 at 10:30Z, and the only config change was payment-service bank.api.timeout_ms raised from 3000 to 5000 at 11:20Z. [F1, F2]
- bank-partner-api is an external dependency with has_telemetry=false and no logs/metrics of its own, so the bank leg can only be observed through its caller payment-service. [F3, F14]
- The payment-service transfer path is api-gateway -> payment-service (http), then payment-service -> ledger-db (db), -> bank-partner-api (external), -> kafka (queue-produce); ledger-db and kafka are shared components. [F3, F4]
- Precedent HIST-105 (2025-11-19) was a payment-service incident caused by a bank partner API returning 5xx for 40 minutes; it is a surface-level symptom match but did not involve TLS client-certificate expiry on our side. [F5]

## Assumptions (not verified)
- Assumption: the client certificate was not rotated before 12:15Z. This is inferred from the absence of any rotation event in the config-change results, but no credential-rotation tool was available to confirm it.
- Assumption: no bank-side change coincided with the failure. bank-partner-api has no telemetry, so its own health cannot be observed; this is inferred from the alert 45 error text naming our own certificate and CN rather than being verified against bank-side data.
- Assumption: PENDING_RETRY payments can be replayed successfully once the certificate is renewed, i.e. the queued work is recoverable and no payments were lost.
- Assumption: the certificate expiry is the sole cause of the 100% bank_api_error_rate, rather than one of several concurrent issues on the bank leg.
- Assumption: no other caller of bank-partner-api uses the same expired certificate; the credential is scoped to payment-service.

## Timeline
- `2026-04-02T10:30:00Z` Only deploy in the window: settlement-service v1.9.0 ('new CSV exporter') goes live, writing the shared ledger-db and consuming kafka. Not rolled back. (F1, F4) [F1, F4]
- `2026-04-02T11:20:00Z` Only config change in the window: payment-service bank.api.timeout_ms raised 3000 -> 5000 by p.nair, 72 minutes before the incident was reported. (F2) [F2]
- `2026-04-02T11:50:26Z` Cert-watcher warning: client certificate CN=payments-client.orangeinvest.internal expires in 25 minutes (notAfter=2026-04-02T12:15:00Z). (F12) [F12]
- `2026-04-02T12:04:00Z` Last normal payment_success_rate reading, ~99.17%. (F10) [F10]
- `2026-04-02T12:12:00Z` bank_api_error_rate still ~0.20%; payment_success_rate bucket partially degraded to 38.93%. Onset of the hidden failure. (F9, F10) [F9, F10]
- `2026-04-02T12:14:00Z` bank_api_error_rate bucket jumps to 50.1%; from 12:16Z it reaches 100% and stays there through 13:56Z. (F9) [F9]
- `2026-04-02T12:15:00Z` The payment-service mTLS client certificate expires (notAfter=2026-04-02T12:15:00Z). (F11, F12) [F11, F12]
- `2026-04-02T12:15:27Z` First payment-service ERROR: 'mTLS handshake with bank-partner-api rejected' — ~27 seconds after certificate expiry. (F11, F12) [F11, F12]
- `2026-04-02T12:16:44Z` First occurrence of 'remote error: tls: expired certificate (alert 45); our client cert CN=payments-client.orangeinvest.internal notAfter=2026-04-02T12:15:00Z'. (F11) [F11]
- `2026-04-02T12:20:00Z` payment_success_rate stabilizes at ~2.1-2.7% and latency_p99_ms drops to ~132-144ms as fast TLS rejections replace normal calls. (F10, F13) [F10, F13]
- `2026-04-02T12:32:00Z` Support lead reports INC-005 (SEV-2): ~40 tickets about failed fund transfers; payment-service HTTP error-rate panel still shows nothing unusual. (F8) [F8]

## Recommended actions
- **[immediate]** Rotate/renew the payment-service mTLS client certificate for bank-partner-api: issue a new client cert for CN=payments-client.orangeinvest.internal (or its successor CN) with a valid notAfter, load it into the payment-service bank connector credential (secret/config entry used for the outbound bank-partner-api mTLS handshake), and restart/reload payment-service so the new key material is picked up. -- H1 (confidence 0.88) is grounded in logs explicitly naming the expired certificate and alert 45; the certificate expired at 12:15:00Z and every outbound bank call since then fails the handshake, so restoring valid key material is the direct mitigation. (F11, F12, F9) [F11, F12, F9]
- **[immediate]** After the credential is reloaded, verify recovery on the transfer-specific signals rather than the 5xx panel: confirm bank_api_error_rate returns to ~0.2% and payment_success_rate recovers toward ~99% over the 12:20Z+ pattern reversal, and confirm the 'mTLS handshake rejected'/alert 45 log signatures stop appearing. -- bank_api_error_rate and payment_success_rate are the metrics that actually exposed the incident (100% and ~2%), whereas HTTP 5xx error_rate never moved; verification must use the signal that failed. (F9, F10, F8) [F9, F10, F8]
- **[immediate]** Confirm the disposition and replay of payments marked PENDING_RETRY during 12:15Z-now: check whether the queued/async retries are re-attempting successfully against the bank leg post-renewal, and whether any payments need manual replay or customer communication. -- The failure mode marks payments PENDING_RETRY rather than failing them (F11), so customer-visible status may lag actual recovery; the blast radius is ~40 tickets and ~98% of transfers during the window. (F11, F10) [F11, F10]
- **[short_term]** Replace or supplement the HTTP-5xx-only payment error-rate panel with a transfer-outcome panel driven by bank_api_error_rate and payment_success_rate (and an alert on mTLS handshake rejections), since the green 5xx panel actively masked this SEV-2 for ~17+ minutes. -- The dashboard stayed green (avg ~0.22% 5xx) because failures surface as async PENDING_RETRY, not 5xx (F8); the discriminating signals existed but were not on the panel (F9, F10). [F8, F9, F10]
- **[short_term]** Verify the settlement-service v1.9.0 deploy (10:30Z) and the 11:20Z bank.api.timeout_ms raise are not contributing: revert the timeout back to 3000ms or confirm it is irrelevant, and confirm ledger-db/kafka show no contention from the new CSV exporter. -- H3 (0.05) and H4 (0.04) are contradicted by the TLS-layer evidence, but they are the only other changes in the window and clearing them prevents recurrence from a confounder. (F1, F2, F4, F13) [F1, F2, F4, F13]
- **[long_term]** Add automated certificate-expiry alerting and pre-expiry auto-renewal for all outbound mTLS client certs (particularly the bank connector), with alerting thresholds well ahead of expiry (e.g. 30/14/7 days and a final <1h warning), so an expiry cannot again become a customer-visible outage. -- The cert-watcher fired only 25 minutes before expiry at 11:50:26Z and had no automated renewal hook, so a foreseeable expiry became an outage (F12); HIST-106 shows the same class of failure occurred before (F6). [F12, F6]
- **[long_term]** Treat bank-partner-api as a monitored dependency: add synthetic/active probing of the mTLS handshake to the bank leg from our side, and alert on handshake failures, so a partner-leg credential or TLS problem is detectable without bank-side telemetry. -- bank-partner-api has has_telemetry=false and cannot be queried directly (tc-015 not_found), so this failure was only visible indirectly through payment-service's outbound metrics (F14, F3). [F14, F3]

## Data gaps
- No metrics, traces, or service logs for payment-service or ledger-db across 10:00-13:59Z were queried (tool budget exhausted), so whether the 11:20Z timeout change or the 10:30Z settlement-service deploy produced any measurable behaviour change remains unanswered.
- bank-partner-api is external with has_telemetry=false and its error_rate query failed with not_found (tc-015), so the health of the bank leg itself cannot be verified and a coincident bank-side problem cannot be fully excluded from our telemetry.
- No credential-rotation or feature-flag tool was available, so credential and feature-flag state for the payment path is unchecked; the claim that the certificate was never rotated is inferred from absence, not verified.
- Could not retrieve payment-service resource trends (db_pool_in_use, request_rate, memory_pct) or queue-depth/retry-count/callback signals, so a concurrent connection-pool, queue-depth, or retry-storm condition is unquantified.
- No metric named for outbound connection timeouts, retry counts, queue depth, or async callback failure rate was verified as existing; only bank_api_error_rate and payment_success_rate characterize the failure.
- Blast radius beyond payment-service was not checked: whether api-gateway, other callers of bank-partner-api, or neighbouring services show the same symptom was not verified.
- Historical search returned no incident whose title explicitly matches the 'dashboard green but customers failing' monitoring gap, so no direct precedent supports the blind-spot failure mode.

## What would change this conclusion
- Evidence that the client certificate CN=payments-client.orangeinvest.internal was actually rotated or renewed before 12:15:00Z (a rotation event, a new notAfter, or a credential reload) would invalidate the expiry mechanism and shift support toward H2 or H4.
- Bank-partner-api telemetry or partner-side logs showing its own TLS/infrastructure failure beginning ~12:14Z would support H2 (bank-side outage) instead.
- Evidence that the mTLS handshake failures persist after a valid renewed certificate is loaded into payment-service would falsify H1 and point to a client-side TLS/trust-store or partner trust-store problem instead.
- Log or metric evidence showing the error text is not attributable to our client cert (e.g. failures occurring before 12:15:00Z, or a different CN in the failing handshakes) would overturn the temporal and scope match.
- Demonstration that ledger-db/kafka contention from settlement-service v1.9.0 can produce TLS handshake rejections on the bank leg would revive H3; conversely, post-renewal failure persistence with residual ledger-db degradation would raise H3/H4.
- Evidence that the timeout raise to 5000ms alone converts calls into the observed failure pattern (and that failures stop if it is reverted while the certificate remains expired) would raise H4.

## Investigation stats
- latency 82.65s, LLM calls 11, tokens 70970 (in 55862 / out 15108)
- tool calls 16 (1 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
