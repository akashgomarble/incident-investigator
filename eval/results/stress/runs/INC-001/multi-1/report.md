# Incident investigation report: INC-001
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause identified: the order-service deployment dep-5101 (v2.14.0, live 2026-03-03T12:10:00Z) added a new MarginCheckValidator to the validation chain and removed the legacy OrderRules.validateMargin(); the new validator dereferences a null marginProfile for F&O (derivatives) orders, throwing java.lang.NullPointerException at MarginCheckValidator.java:88 and returning HTTP 500. Confidence is medium (H1 = 0.74, capped by the Critic after two review rounds), well ahead of the residual alternatives (H2 0.08, H3 0.06, H4 0.02 rejected). The causal chain is tight: dep-5101 is the only change to any component in the window, the error onset (12:10–12:12Z bucket, first error 12:12:31Z) follows it immediately, and all 426 ERROR logs in the window are NullPointerException/FNO marginProfile-null with zero equity-order failures. No downstream, shared-dependency, or capacity signal co-precides the onset; order-service is the only service that steps up in error rate. Mitigation is to revert or roll back dep-5101.

## Probable root cause
**dep-5101 in order-service: new MarginCheckValidator mishandles null marginProfile for F&O orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.74, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: dep-5101 in order-service: new MarginCheckValidator mishandles null marginProfile for F&O orders | bad_deployment | order-service | dep-5101 | 0.74 (medium) | active |
| 2 | H2: Unseen marginProfile data source degrading, with dep-5101 only exposing the gap | dependency_failure | unidentified margin/portfolio profile source (candidate: portfolio-service, or a margin datastore) | null | 0.08 (low) | active |
| 3 | H3: Silent change or degradation on a shared component (ledger-db / redis-cache / kafka) invisible to the standard feeds | infrastructure_failure | ledger-db / redis-cache / kafka (shared components) | null | 0.06 (low) | active |
| 4 | H4: Capacity/resource saturation in order-service on the derivatives path | capacity_saturation | order-service | null | 0.02 (low) | rejected |

### H1: dep-5101 in order-service: new MarginCheckValidator mishandles null marginProfile for F&O orders
The v2.14.0 deployment dep-5101 to order-service (12:10:00Z) added a new MarginCheckValidator into the validation chain and removed the legacy OrderRules.validateMargin(). The new validator dereferences the margin profile it retrieves without a null guard, and for F&O (derivatives) orders that profile is absent, so validation throws java.lang.NullPointerException at MarginCheckValidator.java:88 and the request fails with HTTP 500. The fault originates in the order-service code change itself; whether the null profile comes from a missing data record or from the new code failing to populate it is one and the same origin (the new validation path) and is noted as an open detail.

**Causal chain**
1. dep-5101 deploys order-service v2.14.0 at 12:10:00Z, adding new MarginCheckValidator and removing legacy OrderRules.validateMargin() [F1, F2]
2. This is the only deployment across the entire fleet in the 11:00-13:59Z window; the only config change is unrelated (notification-service email.batch_size), and the standard deploy/config feeds are empty for ledger-db, redis-cache, kafka, portfolio-service [F3, F4, F20, F21, F24]
3. Error onset falls in the 12:10Z bucket, error rate stepping from 0.22% baseline to 14.74% at 12:10Z and holding ~18.3-18.8%, with the first error log at 12:12:31Z — immediately after the deployment goes live [F8, F14, F10]
4. The new validation path runs strictly on the derivatives (F&O) path; every one of the 426 ERROR logs is an NPE at MarginCheckValidator.java:88 with orderType=FNO or 'marginProfile is null for account', and zero equity/non-FNO error logs exist [F9, F31, F32, F33, F34]
5. The null dereference throws an immediate application exception rather than a latency/resource symptom: latency_p99 flat ~150ms, cpu_pct flat ~40%, db_pool_in_use flat ~14, memory flat ~60%, ledger-db connections flat ~62 [F11, F18, F17]
6. Only order-service shows an error step; pricing-engine, exchange-gateway, payment-service, portfolio-service and ledger-db all stay flat at ~0.2% error rate with no step at the 12:10-12:12Z onset [F13, F15, F16, F19, F26, F27]
7. The api-gateway's smaller concurrent error increase is the same 5xx response surfacing at the caller, not an independent fault [F12]

**Supporting evidence**
  - `F1` ✔ order-service was deployed to v2.14.0 (dep-5101) at 2026-03-03T12:10:00Z, 12 minutes before the reported onset (12:22Z); it is still 'live' with no rollback. Deployment summary: 'Refactor order validation; add margin check for F&O orders', changes include 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'.
  - `F2` ✔ The dep-5101 change targets exactly the reported symptom scope: it adds an F&O (futures & options / derivatives) margin check and removes the legacy OrderRules.validateMargin() method, while tickets report mostly derivatives orders failing — a validation-stage change on the derivatives path is a plausible producer of HTTP 500s.
  - `F3` ✔ Across the whole fleet, get_deployments(scope all services, 10:00–13:59Z) returned exactly one deployment: dep-5101 on order-service. No other service deployed in the window.
  - `F8` ✔ order-service error rate jumped from a baseline of ~0.17-0.29% to ~18.5% between the 12:09Z and 12:12Z buckets; onset of the sustained error-rate alert is 2026-03-03T12:12Z (first elevated bucket avg=18.51%, and errors present continuously thereafter).
  - `F9` ✔ All 426 order-service ERROR logs in the window are NullPointerException in MarginCheckValidator.validate at MarginCheckValidator.java:88, and every stack-trace line reads orderType=FNO (derivatives); the two top patterns are 'NullPointerException at MarginCheckValidator.validate' (224) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account' (202).
  - `F10` ✔ First error log at 2026-03-03T12:12:31Z matches the 12:12 metric bucket; error logs run continuously to 13:58:50Z, so the failure is ongoing, not transient.
  - `F11` ✔ The 500s are not a latency/timeout symptom inside order-service: latency_p99_ms stayed flat ~145-165ms across the whole window (mean 149.99ms) and cpu_pct stayed flat ~36-43% (mean 40.16%), so the failures are immediate application exceptions, not downstream slowness or resource exhaustion on order-service.
  - `F14` ✔ order-service error_rate rises from a 12:00Z baseline bucket of avg=0.22 (max 0.32) to avg=14.74 at the 12:10Z bucket and then holds ~18.3-18.8% through 13:50Z — the onset is contained in the 12:10Z bucket, i.e. errors begin ~12:10-12:12Z, the same time the dep-5101 change went live.
  - `F17` ✔ The shared ledger-db shows NO degradation on connections: connections_active is flat ~60-63 (mean 62.02, max 67.0) across 11:30-13:50Z, so no connection-pool/DB saturation accompanies the onset.
  - `F18` ✔ order-service itself has NO resource saturation at onset: db_pool_in_use is flat ~13.8-14.3 (mean 14.04, max 15.22) and memory_pct is flat ~59-61% (mean 60.08, max 65.92) across 11:30-13:50Z, spanning the 12:10Z onset — consistent with an immediate application exception rather than pool/CPU/memory exhaustion.
  - `F19` ✔ No downstream/shared component's error signal precedes or co-precides order-service's: order-service is the only service with an error-rate step (to ~18%), payment-service (F13) and pricing-engine/exchange-gateway stay at ~0.2%; the dep-5101 deployment that added the F&O margin check landed at 12:10:00Z, matching the 12:10Z/12:12Z onset bucket, and no deployment touched any dependency (F3, F6). This localises the fault to order-service.
  - `F24` ✔ Combining the standard-feed results with the team's F1-F19, the only change on ANY component at or before the 12:10-12:12Z onset, across every feed I could reach, is the order-service deployment dep-5101 at 12:10:00Z (F1/F3). No config/flag or deployment change on ledger-db, redis-cache, kafka or portfolio-service is present at or shortly before 12:10-12:12Z in the feeds I reached.
  - `F31` ✔ Full error-log distribution for order-service 11:00-13:59Z sums to exactly 426 ERROR logs, all accounted for by two patterns, neither of which involves any non-FNO order type: 'NullPointerException at MarginCheckValidator.validate(...) orderType=FNO' = 224 and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account' = 202 (224+202=426).
  - `F32` ✔ Pagination confirms the two-pattern composition holds across the whole window. Segment 12:22:20-13:00Z: total 151, patterns NPE orderType=FNO=82 and marginProfile-is-null=69 (82+69=151). Segment 13:00-13:59Z: total 235, patterns NPE orderType=FNO=121 and marginProfile-is-null=114 (121+114=235). Trend over 12:12-13:59: NPE count rises from 224 (11:00-13:59 window, tc-040) to the full 12:12-13:59 count, but no equity/other order-type pattern ever appears in any segment.
  - `F33` ✔ Every single ERROR entry returned across all paginated pages (tc-031, tc-038, tc-039, tc-040) is either 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId=...' or 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account ...'. No entry carries an orderType other than FNO. This directly replaces the earlier truncated 40/426 sample (tc-002) with the complete 426/426 population.
  - `F34` ✔ A targeted search for the substring 'equity' in order-service ERROR logs over 11:00-13:59Z returned zero matches ('total_matched':0), so no equity-order failure log exists in the window; equity orders were not logging 500s.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 'marginProfile is null' condition is produced by the new code path itself (missing/incorrect handling of F&O accounts) rather than by a broken external data source that the new code merely exposed; the null's provenance is not directly traced.
  - No rollback was performed during the window, so no recovery evidence exists for or against this hypothesis.
  - The deploy/config feeds can be trusted as complete for shared datastores — the dependency agent flagged that infra-level changes might not surface there, though they did return the empty result for the named components.

### H2: Unseen marginProfile data source degrading, with dep-5101 only exposing the gap
The NPE is a symptom rather than the fault: the component that owns or computes margin profiles for accounts degraded or returned incomplete data, so marginProfile was null. The dep-5101 change is at most the event that revealed a pre-existing data gap, and the fix would lie outside order-service in that data source.

**Causal chain**
1. Some margin/portfolio profile source stops returning valid marginProfile for F&O accounts at ~12:10-12:12Z *(assumed)*
2. order-service's newly active MarginCheckValidator dereferences the missing value [F1, F2]
3. NPE at MarginCheckValidator.java:88 surfaces as HTTP 500 on derivatives orders [F9]

**Supporting evidence**
  - `F9` ✔ All 426 order-service ERROR logs in the window are NullPointerException in MarginCheckValidator.validate at MarginCheckValidator.java:88, and every stack-trace line reads orderType=FNO (derivatives); the two top patterns are 'NullPointerException at MarginCheckValidator.validate' (224) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account' (202).
**Contradicting evidence**
  - `F3` ✔ Across the whole fleet, get_deployments(scope all services, 10:00–13:59Z) returned exactly one deployment: dep-5101 on order-service. No other service deployed in the window.
  - `F6` ✔ pricing-engine and exchange-gateway (order-service's main downstream callees on the derivatives path) had ZERO deployments in the 10:00–13:59Z window, so neither can explain the onset via a code release.
  - `F13` ✔ payment-service shows NO concurrent anomaly: error_rate stayed flat ~0.17-0.32% (mean 0.21%) throughout 12:00-13:59Z.
  - `F15` ✔ pricing-engine (order-service's derivatives-path downstream callee) shows NO error anomaly: error_rate is flat across the whole window, 0.18-0.25% every 10-min bucket (min 0.1, max 0.32, mean 0.21), including the 12:10Z bucket where order-service spiked.
  - `F16` ✔ exchange-gateway also shows NO error anomaly: error_rate flat 0.18-0.26% all window (mean 0.21) including the 12:10Z and subsequent buckets, so neither of order-service's HTTP downstream callees degrades first.
  - `F17` ✔ The shared ledger-db shows NO degradation on connections: connections_active is flat ~60-63 (mean 62.02, max 67.0) across 11:30-13:50Z, so no connection-pool/DB saturation accompanies the onset.
  - `F26` ✔ The only known candidate owner of the marginProfile lookup, portfolio-service, shows a completely flat error_rate across 11:30–13:59Z: every 10-min bucket is 0.18–0.23% avg (min 0.1, max 0.32, mean 0.21), including the 12:10Z bucket (avg 0.22) — no step at 12:10–12:12Z and no elevation matching order-service's 18.5%.
  - `F27` ✔ portfolio-service latency is also flat across the window: latency_p99_ms averages 161.89–206.25 (mean 181.02) with the 12:10Z bucket at avg 177.98 (below the 12:00Z bucket's 183.23) — no latency step at onset, consistent with a non-degraded profile source.
  - `F28` ✔ portfolio-service logs show no error or null-return signal in 11:30–13:59Z: 26 total lines, counts_by_level INFO=24 / WARN=2, top patterns are routine ('connection pool stats: healthy', 'config reloaded (no changes)', 'served batch of requests'), and the only WARNs are a slow /internal/metrics request (865ms, 11:41Z) and a deprecated TLS cipher — neither at 12:10–12:12Z.
  - `F29` ✔ Zoomed to the onset window (12:05–12:16Z), portfolio-service logged only two routine INFO lines — 'scheduled job heartbeat ok' at 12:08:25Z and 'connection pool stats: healthy' at 12:15:33Z — with nothing anomalous straddling 12:10–12:12Z.
  - `F30` ✔ Interpretation for H2: the candidate marginProfile source (portfolio-service, the only plausible owner) shows no error, latency, or log degradation and no step at 12:10–12:12Z, so the available telemetry does not support an upstream profile-source failure returning null; the null marginProfile observed in order-service (F9) is thus not explained by an observable degradation of its source. A margin-service the prior queries targeted simply does not exist, so those failures were not evidence of the owner being unhealthy.
**Assumptions**
  - An unidentified margin/profile source exists and was degraded at exactly the onset.
  - Its degradation is invisible in error_rate, latency, logs and resource metrics because it returns null without erroring — this is unfalsifiable with the available tools since no null-return/data-completeness metric exists.
  - The onset's coincidence with dep-5101 within one to two minutes is accidental.

### H3: Silent change or degradation on a shared component (ledger-db / redis-cache / kafka) invisible to the standard feeds
A shared component on order-service's dependency graph degraded or received an infra-level change that does not surface in the service-scoped deploy/config endpoints — e.g. ledger-db IOPS throttling, redis-cache saturation, or a kafka change — causing the derivatives order path to fail with 500s. This follows the HIST-110 (ledger-db statement timeouts) and HIST-115 (order-service pool misconfiguration) precedents.

**Causal chain**
1. A shared dependency degrades or is changed but is invisible in the service-scoped deploy/config history [F5, F22, F23]
2. order-service's order path (especially derivatives) begins failing as a result [F8]
3. Failures surface as HTTP 500 on POST /v1/orders [F8]

**Supporting evidence**
  - `F5` ✔ order-service's dependency graph: upstream caller api-gateway; downstream ledger-db (postgres, SHARED), pricing-engine (http), exchange-gateway (http), kafka (queue-produce, SHARED); pricing-engine further depends on market-data-service and redis-cache (SHARED); exchange-gateway calls external exchange-api (no telemetry). Shared components (ledger-db, redis-cache, kafka) are used by several services and can carry faults from neighbours.
  - `F7` ✔ Historical precedents for order-500 / order-failure symptoms have DIFFERENT root causes: HIST-103 (2025-10-21) external exchange API outage causing '504 on order submit'; HIST-110 (2026-01-15) ledger-db IOPS throttling ('statement timeouts'); HIST-115 (2025-09-25) order-service connection-pool misconfiguration ('pool exhausted') fixed by reverting config. None is a derivatives-margin-check regression, so these are leads only — similar symptoms, different causes.
  - `F22` ✔ The attempt to query a non-standard/margin-related service scope FAILED with an upstream timeout (retryable=true, gave up after 3 attempts), so the standard config feed could not be confirmed or refuted for that scope. This is the exact class of infra/margin datastore change the task suspected might be invisible to the standard endpoint.
  - `F23` ✔ The service inventory returned by an unknown-scope error does NOT contain any margin/profile/margin-datastore service: known_services are api-gateway, auth-service, order-service, portfolio-service, pricing-engine, market-data-service, payment-service, settlement-service, notification-service, exchange-gateway, ledger-db, user-db, redis-cache, kafka, exchange-api, bank-partner-api, kyc-provider, email-provider. There is no separately queryable 'margin-service' or 'profile-store' entity in the deployment/config tool's namespace, so a margin/profile datastore config change would not appear as its own feed entry even if one existed.
**Contradicting evidence**
  - `F9` ✔ All 426 order-service ERROR logs in the window are NullPointerException in MarginCheckValidator.validate at MarginCheckValidator.java:88, and every stack-trace line reads orderType=FNO (derivatives); the two top patterns are 'NullPointerException at MarginCheckValidator.validate' (224) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account' (202).
  - `F11` ✔ The 500s are not a latency/timeout symptom inside order-service: latency_p99_ms stayed flat ~145-165ms across the whole window (mean 149.99ms) and cpu_pct stayed flat ~36-43% (mean 40.16%), so the failures are immediate application exceptions, not downstream slowness or resource exhaustion on order-service.
  - `F17` ✔ The shared ledger-db shows NO degradation on connections: connections_active is flat ~60-63 (mean 62.02, max 67.0) across 11:30-13:50Z, so no connection-pool/DB saturation accompanies the onset.
  - `F18` ✔ order-service itself has NO resource saturation at onset: db_pool_in_use is flat ~13.8-14.3 (mean 14.04, max 15.22) and memory_pct is flat ~59-61% (mean 60.08, max 65.92) across 11:30-13:50Z, spanning the 12:10Z onset — consistent with an immediate application exception rather than pool/CPU/memory exhaustion.
  - `F15` ✔ pricing-engine (order-service's derivatives-path downstream callee) shows NO error anomaly: error_rate is flat across the whole window, 0.18-0.25% every 10-min bucket (min 0.1, max 0.32, mean 0.21), including the 12:10Z bucket where order-service spiked.
  - `F16` ✔ exchange-gateway also shows NO error anomaly: error_rate flat 0.18-0.26% all window (mean 0.21) including the 12:10Z and subsequent buckets, so neither of order-service's HTTP downstream callees degrades first.
  - `F20` ✔ The standard get_config_changes endpoint returns ZERO config changes on the shared dependencies in 11:00-13:59Z: ledger-db (count 0), redis-cache (count 0), kafka (count 0), portfolio-service (count 0). No config/flag change is attributable to any of these components via the standard feed at or before the 12:10-12:12Z onset.
  - `F21` ✔ The standard get_deployments endpoint returns ZERO deployments on the shared dependencies in 11:00-13:59Z: ledger-db (count 0) and portfolio-service (count 0). No deployment touched these components in the window.
  - `F36` ✔ Total request volume to order-service is flat across the incident (request_rate ~230-235 req/s mean 232.19 every ~23-min bucket from 11:00 to 13:41Z, min 205.56 max 255.8), so the ~18.5% error_rate is not an artifact of traffic surge; only the error fraction changed.
**Assumptions**
  - The shared component's degradation is real but unmeasured: slow_queries_per_min for ledger-db, redis hit_ratio/memory/evictions and kafka metrics were never retrieved (budget exhausted).
  - The precise one-to-two-minute coincidence with dep-5101 is accidental.
  - The failure mechanism would still have to produce an immediate NPE inside a brand-new validator, which no shared-infra degradation explains directly.

### H4: Capacity/resource saturation in order-service on the derivatives path
A traffic or load surge on the derivatives path saturated order-service resources (DB pool, thread pool, CPU), producing 500s — consistent with the HIST-115 precedent of connection-pool exhaustion.

**Causal chain**
1. Derivatives order volume rises and saturates a pooled resource in order-service *(assumed)*
2. Requests begin failing with 500s [F8]

**Supporting evidence**
  - `F7` ✔ Historical precedents for order-500 / order-failure symptoms have DIFFERENT root causes: HIST-103 (2025-10-21) external exchange API outage causing '504 on order submit'; HIST-110 (2026-01-15) ledger-db IOPS throttling ('statement timeouts'); HIST-115 (2025-09-25) order-service connection-pool misconfiguration ('pool exhausted') fixed by reverting config. None is a derivatives-margin-check regression, so these are leads only — similar symptoms, different causes.
**Contradicting evidence**
  - `F9` ✔ All 426 order-service ERROR logs in the window are NullPointerException in MarginCheckValidator.validate at MarginCheckValidator.java:88, and every stack-trace line reads orderType=FNO (derivatives); the two top patterns are 'NullPointerException at MarginCheckValidator.validate' (224) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account' (202).
  - `F11` ✔ The 500s are not a latency/timeout symptom inside order-service: latency_p99_ms stayed flat ~145-165ms across the whole window (mean 149.99ms) and cpu_pct stayed flat ~36-43% (mean 40.16%), so the failures are immediate application exceptions, not downstream slowness or resource exhaustion on order-service.
  - `F17` ✔ The shared ledger-db shows NO degradation on connections: connections_active is flat ~60-63 (mean 62.02, max 67.0) across 11:30-13:50Z, so no connection-pool/DB saturation accompanies the onset.
  - `F18` ✔ order-service itself has NO resource saturation at onset: db_pool_in_use is flat ~13.8-14.3 (mean 14.04, max 15.22) and memory_pct is flat ~59-61% (mean 60.08, max 65.92) across 11:30-13:50Z, spanning the 12:10Z onset — consistent with an immediate application exception rather than pool/CPU/memory exhaustion.
  - `F36` ✔ Total request volume to order-service is flat across the incident (request_rate ~230-235 req/s mean 232.19 every ~23-min bucket from 11:00 to 13:41Z, min 205.56 max 255.8), so the ~18.5% error_rate is not an artifact of traffic surge; only the error fraction changed.
**Assumptions**
  - A load surge occurred but was not observable in the queried metrics (now positively refuted by F36).
**Rejected because:** Resource metrics directly refute saturation: latency_p99 flat ~150ms, cpu_pct flat ~40% (F11), db_pool_in_use flat ~14 max 15.22, memory flat ~60% (F18), and ledger-db connections_active flat ~62 (F17). Request volume is flat at ~230-235 req/s across the whole incident (F36), so there is no traffic surge either. The errors are immediate NullPointerExceptions in a specific validator (F9), not pool/timeout errors.

## Facts (verified observations)
- order-service was deployed to v2.14.0 (dep-5101) at 2026-03-03T12:10:00Z, 12 minutes before the reported onset; the deployment is still live with no rollback. Its summary is 'Refactor order validation; add margin check for F&O orders', with changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'. [F1]
- A fleet-wide deployment query (all services, 10:00–13:59Z) returned exactly one deployment: dep-5101 on order-service. No other service deployed in the window. [F3]
- The only config change across all services in the window is cfg-2101 on notification-service (email.batch_size 200 -> 250 at 11:21:00Z), unrelated to order placement or derivatives orders. [F4]
- order-service's downstream HTTP callees on the derivatives path (pricing-engine, exchange-gateway) had zero deployments in the window, so neither can explain the onset via a code release. [F6]
- order-service error rate jumped from a baseline of ~0.17–0.29% to ~18.5% between the 12:09Z and 12:12Z buckets; the first elevated bucket averaged 18.51% and errors are present continuously thereafter. [F8, F14]
- All 426 order-service ERROR logs in the window are NullPointerException in MarginCheckValidator.validate at MarginCheckValidator.java:88, and every stack-trace line reads orderType=FNO (derivatives); the two patterns are 'NullPointerException at MarginCheckValidator.validate' (224) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account' (202), summing exactly to 426. [F9, F31, F33]
- The full 426/426 ERROR population was assembled by pagination and pattern counts sum exactly to each sub-window total (12:22:20–13:00Z: 82+69=151; 13:00–13:59Z: 121+114=235); no non-FNO order-type pattern appears in any segment. A targeted search for the substring 'equity' in order-service ERROR logs over 11:00–13:59Z returned zero matches. [F32, F34]
- The first error log is at 2026-03-03T12:12:31Z, matching the 12:12Z metric bucket, and error logs run continuously to 13:58:50Z, so the failure is ongoing, not transient. [F10]
- The 500s are not a latency/timeout symptom: latency_p99_ms stayed flat ~145–165ms (mean 149.99ms) and cpu_pct flat ~36–43% (mean 40.16%) across the whole window. [F11]
- order-service shows no resource saturation at onset: db_pool_in_use flat ~13.8–14.3 (mean 14.04, max 15.22) and memory_pct flat ~59–61% (mean 60.08) spanning 11:30–13:50Z. [F18]
- Total request volume to order-service is flat across the incident (request_rate ~230–235 req/s, mean 232.19, min 205.56, max 255.8), so the ~18.5% error rate is not an artifact of a traffic surge; only the error fraction changed. [F36]
- No downstream or shared component degrades first: pricing-engine error rate is flat 0.18–0.25%, exchange-gateway flat 0.18–0.26%, payment-service flat ~0.17–0.32%, and ledger-db connections_active flat ~60–63 across the window. [F15, F16, F13, F17]
- api-gateway shows a concurrent smaller error increase (~0.18–0.23% rising to ~2.06% at 12:10Z and settling ~3.3–3.5%), co-timed with and slightly preceding the order-service spike, consistent with the gateway surfacing the same 5xx responses from POST /v1/orders. [F12]
- The standard config-change endpoint returned zero config changes on ledger-db, redis-cache, kafka and portfolio-service for 11:00–13:59Z, and the standard deployment endpoint returned zero deployments on ledger-db and portfolio-service in that window. [F20, F21]
- No separately queryable margin/profile/margin-datastore service exists in the fleet: metrics and logs queries for 'margin-service' returned not_found against the known_services list, which contains no margin-service or profile-store entity. [F25, F23]
- portfolio-service, the only plausible owner of the marginProfile lookup, is completely flat across 11:30–13:59Z: error_rate every bucket 0.18–0.23% (12:10Z bucket avg 0.22), latency_p99 averaging 161.89–206.25ms with the 12:10Z bucket below the 12:00Z bucket, and logs showing only 24 INFO / 2 WARN routine lines with nothing anomalous straddling 12:10–12:12Z. [F26, F27, F28, F29]
- Historical precedents for order-500 symptoms have different root causes (HIST-103 external exchange API outage, HIST-110 ledger-db IOPS throttling, HIST-115 connection-pool misconfiguration) and therefore serve as leads only, not as this incident's cause. [F7]
- order-service exposes no order-type/route dimension in its metrics (available_metrics: cpu_pct, db_pool_in_use, error_rate, latency_p99_ms, memory_pct, request_rate, restarts), so order-type exclusivity is evidenced by logs, not metrics. [F35]

## Assumptions (not verified)
- The 'marginProfile is null' condition is produced by the new code path itself (missing/incorrect handling of F&O accounts) rather than by a pre-existing broken/sparse external data record that the new code merely exposed; the null's provenance was not directly traced.
- The deploy/config feeds can be trusted as sufficiently complete for shared datastores; the dependency agent flagged that infra-level changes might not surface there, and no separate infra/ops/maintenance feed exists in the tool namespace, so any such change is not queriably observable.
- No rollback was performed during the observation window, so no recovery evidence exists for or against H1.
- portfolio-service is the owner of the marginProfile lookup; the dependency graph does not list a margin/profile dependency, so ownership is inferred rather than directly traced.
- The failure is exclusively on the F&O (derivatives) order path; this is inferred from logs because order-service metrics carry no order-type dimension.
- (demoted from facts: no verified observation backs it) The dep-5101 change targets exactly the reported symptom scope: it adds an F&O (derivatives) margin check and removes the legacy margin validation, while tickets report mostly derivatives orders failing.

## Timeline
- `2026-03-03T11:21:00Z` Only unrelated config change in the fleet: cfg-2101 on notification-service (email.batch_size 200 -> 250). Not related to order placement. [F4]
- `2026-03-03T12:10:00Z` order-service deployed to v2.14.0 (dep-5101): new MarginCheckValidator added to the validation chain, legacy OrderRules.validateMargin() removed. This is the only deployment on any service in the 10:00–13:59Z window. [F1, F3]
- `2026-03-03T12:10:00Z` order-service error rate enters the onset bucket (avg 14.74, up from a 12:00Z baseline avg of 0.22); api-gateway error rate also rises to ~2.06% and peaks at 12:10Z. [F14, F12]
- `2026-03-03T12:12:00Z` First sustained order-service error bucket (avg 18.51%), holding ~18.3–18.8% through 13:50Z. [F8]
- `2026-03-03T12:12:31Z` First order-service ERROR log: NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) with orderType=FNO. Errors continue to 13:58:50Z. [F9, F10]
- `2026-03-03T12:22:00Z` Incident INC-001 [SEV-1] reported by on-call SRE; spike in failed order placements, mostly derivatives orders per support tickets. [F2]
- `2026-03-03T13:00:00Z` Probe of a non-standard margin-related service scope fails with upstream timeout after 3 attempts; no separate margin-datastore entity exists in known_services. [F22, F25, F23]
- `2026-03-03T13:59:00Z` End of observation window: dep-5101 still live with no rollback; 426/426 ERROR logs are NullPointerException/FNO marginProfile-null with zero equity-order failures; all downstream and shared components remain flat. [F1, F31, F34, F15, F16, F17]

## Recommended actions
- **[immediate]** Roll back order-service from v2.14.0 to the prior release (revert deployment dep-5101) as the immediate mitigation, and confirm POST /v1/orders 500s stop and order-service error_rate returns to its ~0.2% baseline. -- dep-5101 is the sole change on any component at or before the 12:10–12:12Z onset (F1, F3, F24) and is still live; the failure is an immediate NPE inside the validator it added (F9), with the error onset contained in the 12:10Z bucket after it (F14, F8). [F1, F3, F8, F9, F14, F24]
- **[immediate]** If a full rollback is not feasible, disable/remove the new MarginCheckValidator from the validation chain (or restore OrderRules.validateMargin()) via the order-service configuration/feature flag, then verify errors cease. -- All 426 ERROR logs originate at MarginCheckValidator.java:88 and the legacy margin validation was removed in the same change, so reverting just this validation path should restore behaviour (F1, F9). [F1, F9, F31]
- **[short_term]** Add a null guard for marginProfile in MarginCheckValidator (handle absent F&O margin profiles explicitly, e.g. fall back to the legacy validation behaviour or return a deterministic 4xx rather than an NPE-driven 500), then re-enable the F&O margin check. -- The NPE is the direct failure mechanism ('marginProfile is null for account') and removing the legacy check eliminated any fallback for F&O accounts (F9, F1). [F9, F1]
- **[short_term]** Verify the fix under F&O order traffic: confirm order-service error_rate returns to ~0.2%, no NullPointerException at MarginCheckValidator.java:88 appears in logs, and api-gateway error rate returns to baseline. -- The pre-incident baselines are known (order-service ~0.17–0.29%, api-gateway ~0.18–0.23%), giving a clear verification target (F8, F12). [F8, F12, F31]
- **[short_term]** Add a request-path/order-type dimension (e.g. orderType or route label) to order-service metrics so failures can be attributed by product line without relying on log sampling. -- order-service exposes no order-type dimension (available_metrics list excludes it), so F&O-vs-equity exclusivity could only be established from logs (F35, F34). [F35, F34]
- **[long_term]** Extend deployment/change gating (canary or staged rollout) for validation-path changes to order-service, plus a pre-deploy null/missing-data test for the marginProfile lookup on F&O accounts. -- The regression shipped and ran for ~1h49m before remediation, and the failure was specific to a data-shape condition (absent F&O margin profile) that a canary on the derivatives path would have caught (F1, F9, F10). [F1, F9, F10]
- **[long_term]** Instrument the marginProfile lookup source with a null-return / lookup-miss counter and an explicit error signal, so a missing-profile condition is observable at the source in future. -- No null-return/data-completeness metric exists anywhere in the fleet, which is why the null's provenance could only be inferred and why portfolio-service health had to be judged from generic error_rate/latency/logs (F26, F28, F29). [F26, F28, F29]
- **[long_term]** Add or document an infra/ops change feed covering shared datastores (ledger-db, redis-cache, kafka), since the standard deploy/config endpoints are service-scoped and returned empty for them. -- The dependency agent flagged that infra-level changes on shared components may not surface in the standard feeds, which is the residual alternative path that could not be fully ruled out (F20, F21, F23). [F20, F21, F23]

## Data gaps
- No infra/ops/maintenance change feed or margin-datastore feed exists among the exposed tools; only service-scoped get_deployments and get_config_changes are available, and those returned empty for ledger-db, redis-cache, kafka and portfolio-service. A deploy-free infra-level change on a shared component at 12:10–12:12Z therefore cannot be directly confirmed or excluded (F20, F21, F23).
- The provenance of the null marginProfile is untraced: it is not established whether the new code path fails to retrieve/populate the profile or whether a pre-existing sparse data record is simply dereferenced without a guard. This detail does not change the origin or the fix (both lie in the new validation path from dep-5101) but it lowers confidence.
- No component in the fleet is named margin-service or profile-store, so the exact owner of the marginProfile lookup could not be confirmed; portfolio-service is inferred as the candidate, and it shows no degradation, but ownership is not directly proven (F23, F25, F30).
- portfolio-service logs are sparse (26 lines in the window, ~1 request-batch sample per 6–10 minutes), so the absence of a null/error log line at 12:10–12:12Z is weak evidence against a brief null-return burst (F29).
- order-service metrics have no per-order-type/route dimension and the requests_by_order_type query failed; F&O exclusivity of the failures rests on logs alone, so it could not be independently corroborated by a success-rate-by-type metric (F35, F34).
- Every error-log page remained truncated within its sub-window, so the full 426-entry population was assembled from pattern counts that reconcile to each sub-window total rather than from inspecting all lines; a rare non-FNO entry in an unreturned page middle is possible in principle, though the counts argue against it (F32, F33).
- Several intended probes were cut off by the tool-call budget: ledger-db slow_queries_per_min (the HIST-110 precedent), redis-cache cpu/memory/hit_ratio/evictions, kafka metrics, exchange-gateway upstream_error_rate/upstream_latency_ms, and a null-filtered portfolio-service log query. No positive dependency signal exists, but silent ledger-db throttling or redis saturation is not mechanically excluded (F17).
- No rollback or other remediation occurred within the observation window (dep-5101 rolled_back_at is null), so there is no recovery evidence that would confirm causality by reversibility (F1).

## What would change this conclusion
- A fleet-wide or infra-level change (e.g. datastore maintenance/throttling, or a config/deploy on ledger-db, redis-cache, kafka or a margin datastore) timestamped at 12:08–12:12Z, discovered via a feed not queried here, would challenge H1 in favour of H3 or H2.
- Evidence that marginProfile was already null or missing for F&O accounts before 12:10Z (e.g. pre-deployment traces or data showing the lookup failing beforehand) would shift the origin toward H2 — a pre-existing data gap merely exposed by dep-5101 — and change the recommended fix.
- Non-FNO order types also failing with 500s, or errors appearing in a validator stage that runs before/independently of order type, would show the defect is broader than the F&O margin path and would change the scope of the fix.
- A rollback of dep-5101 that fails to stop the 500s (persisting NPEs or errors after revert) would falsify H1 as the sufficient cause.
- A degraded-but-non-erroring marginProfile source (e.g. portfolio-service returning nulls while showing flat error_rate/latency, visible only via a null-return counter) would revive H2 and relocate the fix outside order-service.
- Evidence that the 12:10Z deploy and 12:12Z onset are coincidental — e.g. error logs beginning before 12:10Z or the deployment being a no-op — would weaken the temporal causal chain that anchors H1.

## Integrity warnings (set by code)
- fact demoted to assumption: The dep-5101 change targets exactly the reported symptom scope: it adds an F&O (

## Investigation stats
- latency 195.58s, LLM calls 28, tokens 224519 (in 190653 / out 33866)
- tool calls 48 (11 failed, 0 partial, 9 recovered by retry), findings 36/36 verified
