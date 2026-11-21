# Incident investigation report: INC-003
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The 30% order-submit failure rate is best explained by an outage/degradation of the external exchange partner behind exchange-gateway (H1, confidence 0.60, medium), not by the order-service v2.15.2 release. The failure signal originates upstream at exchange-gateway (first upstream connect timeout to api.exchange-partner.example at 11:01:25Z, before order-service's first 504 at 11:02:45Z), the error signature is dominated by "exchange-gateway call failed: 504 Gateway Timeout (placeOrder)", and order-service's latency (~5300ms) matches the 5000ms exchange upstream timeout with flat CPU and db_pool. The deploy's timing (10:48Z, ~13 min before onset) and blast radius (exchange-gateway is not shared and not part of the changed code path) both mismatch, and the error rate stayed ~30-32% after the 11:16Z rollback. Residual uncertainty remains because the failing hop (exchange-api) has no telemetry and the rollback's execution could not be independently confirmed. Mitigation should be exchange-side (fail-fast/breaker/retry policy, partner engagement); the rollback is not expected to fix the symptom.

## Probable root cause
**External exchange partner (exchange-api) outage propagating to order-service** -- dependency_failure in `exchange-api (external partner, behind exchange-gateway)`, triggered by `null` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange partner (exchange-api) outage propagating to order-service | dependency_failure | exchange-api (external partner, behind exchange-gateway) | null | 0.60 (medium) | active |
| 2 | H2: order-service v2.15.2 'idempotency keys' defect causing order submit 504s (reporter's suspicion) | bad_deployment | order-service | dep-5333 | 0.05 (low) | rejected |
| 3 | H4: exchange-gateway's own resource saturation / capacity limit causing upstream timeouts | capacity_saturation | exchange-gateway | null | 0.05 (low) | rejected |
| 4 | H3: pricing-engine cache.ttl_seconds 5→4 config change (cfg-2290) degrading the quote branch | config_change | pricing-engine | cfg-2290 | 0.03 (low) | rejected |

### H1: External exchange partner (exchange-api) outage propagating to order-service
The external exchange partner's API (api.exchange-partner.example) began failing/degrading around 11:01Z, causing exchange-gateway connect timeouts and upstream 503s, which surfaced as 504s on the exchange-dependent subset of order-service's order-submit calls. order-service v2.15.2 is a coincidence in timing and was unrelated.

**Causal chain**
1. External exchange-api at api.exchange-partner.example begins failing (connect timeouts / 503s) around 11:01Z [F10]
2. exchange-gateway's error_rate jumps in the 10:58 bucket to ~25% and sustains ~44% from 11:05; first upstream connect timeout at 11:01:25Z, upstream 503s from exchange-api from 11:02:20Z, circuit breaker opens at 11:03:26Z (failure ratio 0.47) [F10, F11]
3. order-service calls exchange-gateway for the exchange-dependent subset of placeOrder calls and times out at the 5000ms upstream timeout, producing 504s [F5, F9, F12]
4. order-service error_rate jumps to ~30% in the 11:00 bucket (onset 10:55-11:05), latency_p99 rises to ~3264ms then ~5300ms matching the 5s exchange timeout, while CPU and db_pool_in_use stay flat [F8, F12]
5. order-service 504s persist through 12:55 despite the 11:16Z rollback of dep-5333, because the fault is external [F1, F8, F10]

**Supporting evidence**
  - `F10` ✔ exchange-gateway is the origin of the failure signal: first upstream connect timeout to the external partner at 11:01:25Z (earlier than order-service's first 504 at 11:02:45Z), plus upstream 503s from exchange-api starting 11:02:20Z, and a circuit breaker opening for exchange-api at 11:03:26Z (failure ratio 0.47 over 60s). Patterns: 163x 'upstream connect timeout ... host=api.exchange-partner.example op=placeOrder', 146x 'op=quoteSnapshot', 161x 'exchange-api returned 503 Service Unavailable'.
  - `F11` ✔ exchange-gateway error_rate also jumped: flat ~0.17-0.23% through the 10:51 bucket, then 24.93% in the 10:58 bucket and ~43-45% sustained from 11:05 onward — a larger error rate than order-service's ~30%, consistent with order-service failing only the exchange-dependent subset of calls.
  - `F9` ✔ order-service logged 349 ERROR entries between 11:02:45Z and 12:58:54Z, with a single top pattern: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'. First occurrence 2026-03-17T11:02:45Z.
  - `F12` ✔ order-service latency_p99_ms jumped from ~149ms baseline (10:30-10:55 buckets) to 3264.85 avg in the 11:00 bucket and ~5300ms sustained from 11:05 — consistent with 504s timing out at the 5000ms exchange-gateway upstream timeout, not with internal slowness. CPU (~38-41%) and db_pool_in_use (~13.6-14.5 connections) on order-service stayed flat across onset, so no local resource saturation.
  - `F5` ✔ order-service dependency structure: inbound edge api-gateway → order-service (http); outbound edges order-service → ledger-db (db), → pricing-engine (http), → exchange-gateway (http), → kafka (queue-produce); pricing-engine → market-data-service (http) and → redis-cache (cache); exchange-gateway → exchange-api (external, has_telemetry:false).
  - `F16` ✔ BLAST RADIUS MISMATCH: the origin of the failure signal is exchange-gateway -> external exchange-api (api.exchange-partner.example), and exchange-gateway is NOT shared ("shared":false) and is not a dependency of the deployed service's changed code path -- order-service only calls it for the exchange-dependent subset. exchange-gateway's error_rate (~44%) exceeds order-service's (~30%), consistent with order-service failing only a subset of calls.
  - `F17` ✔ Change graph of the deployed service: order-service's own downstream paths were untouched by dep-5333's scope (only the order-service submit handler changed); order-service's dependencies are ledger-db (shared:true), pricing-engine, exchange-gateway (shared:false), kafka (shared:true). exchange-api is external with has_telemetry:false, so the failing hop has NO telemetry of its own.
  - `F19` ✔ Precedent leads (not evidence): HIST-111 (2025-05-22, category bad_deployment, ledger-db/settlement-service) and HIST-115 (2025-09-25, category config_change, order-service 'pool exhausted') both returned under the 'rollback did not fix error rate' query; the close-match incident for the current signature is HIST-103 2025-10-21 'Order failures during exchange partner outage' (order-service + exchange-gateway, '504 on order submit', 'upstream timeouts', root cause 'External exchange API outage on the partner side', category dependency_failure).
  - `F1` ✔ Across ALL services in 2026-03-17T06:00Z–13:00Z there was exactly one deployment: order-service v2.15.2 (dep-5333), deployed 2026-03-17T10:48:00Z by a.khan, summary "Add idempotency keys to order submit", already status "rolled_back" with rolled_back_at 2026-03-17T11:16:00Z.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The external partner genuinely had an incident at ~11:01Z (has_telemetry:false, so unobservable in-platform)
  - order-service's error set is dominated by the exchange-dependent subset, so its ~30% vs gateway's ~44% gap is fully explained
  - The rollback at 11:16Z was actually executed and error rate stayed flat through 12:55 as F8/F10/F11 show

### H2: order-service v2.15.2 'idempotency keys' defect causing order submit 504s (reporter's suspicion)
The v2.15.2 change to the order-submit handler introduced a defect (e.g. retry/duplicate-submission behaviour or a blocking call) that caused 504s on order submit; the exchange-gateway errors are a downstream symptom or unrelated noise.

**Causal chain**
1. v2.15.2 'Add idempotency keys to order submit' (dep-5333) deployed 10:48Z changes the submit handler [F1]
2. The changed code path produces slow/failing submit requests that time out as 504s *(assumed)*
3. order-service error_rate jumps to ~30% and latency_p99 to ~5300ms [F8, F12]

**Supporting evidence**
  - `F1` ✔ Across ALL services in 2026-03-17T06:00Z–13:00Z there was exactly one deployment: order-service v2.15.2 (dep-5333), deployed 2026-03-17T10:48:00Z by a.khan, summary "Add idempotency keys to order submit", already status "rolled_back" with rolled_back_at 2026-03-17T11:16:00Z.
**Contradicting evidence**
  - `F8` ✔ order-service error_rate was flat ~0.16-0.23% through the 10:55 bucket (avg 0.21), then jumped to avg 19.02% (max 31.9) in the 11:00 bucket and stayed ~30-32% through 12:55. 5-min buckets, so true onset is between 10:55 and 11:05.
  - `F15` ✔ TIMING MISMATCH: dep-5333 deployed at 10:48:00Z, but the failure signal did not begin for another ~13 minutes -- first exchange-gateway upstream connect timeout at 11:01:25Z, order-service error_rate jumping in the 11:00 bucket (between 10:55 and 11:05), first order-service 504 at 11:02:45Z. During those first ~7 minutes after the deploy (10:48-10:55) order-service error_rate was still flat ~0.16-0.23%.
  - `F9` ✔ order-service logged 349 ERROR entries between 11:02:45Z and 12:58:54Z, with a single top pattern: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'. First occurrence 2026-03-17T11:02:45Z.
  - `F10` ✔ exchange-gateway is the origin of the failure signal: first upstream connect timeout to the external partner at 11:01:25Z (earlier than order-service's first 504 at 11:02:45Z), plus upstream 503s from exchange-api starting 11:02:20Z, and a circuit breaker opening for exchange-api at 11:03:26Z (failure ratio 0.47 over 60s). Patterns: 163x 'upstream connect timeout ... host=api.exchange-partner.example op=placeOrder', 146x 'op=quoteSnapshot', 161x 'exchange-api returned 503 Service Unavailable'.
  - `F11` ✔ exchange-gateway error_rate also jumped: flat ~0.17-0.23% through the 10:51 bucket, then 24.93% in the 10:58 bucket and ~43-45% sustained from 11:05 onward — a larger error rate than order-service's ~30%, consistent with order-service failing only the exchange-dependent subset of calls.
  - `F12` ✔ order-service latency_p99_ms jumped from ~149ms baseline (10:30-10:55 buckets) to 3264.85 avg in the 11:00 bucket and ~5300ms sustained from 11:05 — consistent with 504s timing out at the 5000ms exchange-gateway upstream timeout, not with internal slowness. CPU (~38-41%) and db_pool_in_use (~13.6-14.5 connections) on order-service stayed flat across onset, so no local resource saturation.
  - `F16` ✔ BLAST RADIUS MISMATCH: the origin of the failure signal is exchange-gateway -> external exchange-api (api.exchange-partner.example), and exchange-gateway is NOT shared ("shared":false) and is not a dependency of the deployed service's changed code path -- order-service only calls it for the exchange-dependent subset. exchange-gateway's error_rate (~44%) exceeds order-service's (~30%), consistent with order-service failing only a subset of calls.
  - `F17` ✔ Change graph of the deployed service: order-service's own downstream paths were untouched by dep-5333's scope (only the order-service submit handler changed); order-service's dependencies are ledger-db (shared:true), pricing-engine, exchange-gateway (shared:false), kafka (shared:true). exchange-api is external with has_telemetry:false, so the failing hop has NO telemetry of its own.
**Assumptions**
  - The changed handler has a defect that manifests only under live traffic ~13 minutes after deploy (warm-up, backfill, or first-triggered path)
  - The dominant error 'exchange-gateway call failed: 504 Gateway Timeout' is a secondary effect of the local defect rather than the true origin
**Rejected because:** Timing mismatch: deploy at 10:48Z but error_rate stayed flat ~0.16-0.23% through 10:55 and onset was between 10:55 and 11:05 (first order-service 504 at 11:02:45Z, first failure signal upstream at exchange-gateway 11:01:25Z) — the failure signal originates at exchange-gateway, which is NOT shared and is not part of the changed code path (F15, F16, F10, F11). The single dominant error is a 504 on exchange-gateway, and order-service CPU/db_pool stayed flat (no local resource symptom, F12), while latency matches the 5s exchange timeout. Also, the failure persisted after the 11:16Z rollback (F2, F8).

### H4: exchange-gateway's own resource saturation / capacity limit causing upstream timeouts
exchange-gateway itself (not the external partner) exhausted a local resource (connections, threads, CPU) so its calls to the external partner timed out; the connect timeouts are a symptom of gateway-local saturation.

**Causal chain**
1. exchange-gateway hits an internal resource/connection limit around 11:01Z *(assumed)*
2. Outbound calls to api.exchange-partner.example time out at connect stage [F10]
3. order-service's exchange-dependent calls 504 [F9, F12]

**Supporting evidence**
  - `F10` ✔ exchange-gateway is the origin of the failure signal: first upstream connect timeout to the external partner at 11:01:25Z (earlier than order-service's first 504 at 11:02:45Z), plus upstream 503s from exchange-api starting 11:02:20Z, and a circuit breaker opening for exchange-api at 11:03:26Z (failure ratio 0.47 over 60s). Patterns: 163x 'upstream connect timeout ... host=api.exchange-partner.example op=placeOrder', 146x 'op=quoteSnapshot', 161x 'exchange-api returned 503 Service Unavailable'.
  - `F11` ✔ exchange-gateway error_rate also jumped: flat ~0.17-0.23% through the 10:51 bucket, then 24.93% in the 10:58 bucket and ~43-45% sustained from 11:05 onward — a larger error rate than order-service's ~30%, consistent with order-service failing only the exchange-dependent subset of calls.
**Contradicting evidence**
  - `F10` ✔ exchange-gateway is the origin of the failure signal: first upstream connect timeout to the external partner at 11:01:25Z (earlier than order-service's first 504 at 11:02:45Z), plus upstream 503s from exchange-api starting 11:02:20Z, and a circuit breaker opening for exchange-api at 11:03:26Z (failure ratio 0.47 over 60s). Patterns: 163x 'upstream connect timeout ... host=api.exchange-partner.example op=placeOrder', 146x 'op=quoteSnapshot', 161x 'exchange-api returned 503 Service Unavailable'.
  - `F16` ✔ BLAST RADIUS MISMATCH: the origin of the failure signal is exchange-gateway -> external exchange-api (api.exchange-partner.example), and exchange-gateway is NOT shared ("shared":false) and is not a dependency of the deployed service's changed code path -- order-service only calls it for the exchange-dependent subset. exchange-gateway's error_rate (~44%) exceeds order-service's (~30%), consistent with order-service failing only a subset of calls.
**Assumptions**
  - exchange-gateway has a saturation mode that shows as 'upstream connect timeout' rather than a local capacity error
  - No gateway config/limit change occurred (none was found in the window, F3/F4)
**Rejected because:** The error text is explicitly 'upstream connect timeout ... host=api.exchange-partner.example' plus 'exchange-api returned 503 Service Unavailable' and a circuit breaker on exchange-api — a signature of the peer failing, not of gateway-local saturation (F10). exchange-gateway is not shared and received no config/deploy change in the window (F3, F4, F16). The gateway's own resource metrics were never queried (gap), but the mechanism evidence points at the external peer.

### H3: pricing-engine cache.ttl_seconds 5→4 config change (cfg-2290) degrading the quote branch
cfg-2290 (pricing-engine cache.ttl_seconds 5→4) reduced quote cache lifetime, causing more upstream market-data calls, latency/error propagation through pricing-engine into order-service's quote-dependent submit path.

**Causal chain**
1. cfg-2290 reduces pricing-engine cache TTL 5→4 at 09:20Z [F3]
2. Shorter TTL increases market-data-service / redis-cache load and pricing-engine latency *(assumed)*
3. order-service submit path calling pricing-engine slows and times out *(assumed)*

**Supporting evidence**
  - `F3` ✔ Across ALL services the only config change in the window was cfg-2290: pricing-engine cache.ttl_seconds changed 5 → 4 at 2026-03-17T09:20:00Z by config-bot ("Shorter quote cache TTL") — no feature-flag, infra/network or dependency-version change appears.
  - `F5` ✔ order-service dependency structure: inbound edge api-gateway → order-service (http); outbound edges order-service → ledger-db (db), → pricing-engine (http), → exchange-gateway (http), → kafka (queue-produce); pricing-engine → market-data-service (http) and → redis-cache (cache); exchange-gateway → exchange-api (external, has_telemetry:false).
**Contradicting evidence**
  - `F9` ✔ order-service logged 349 ERROR entries between 11:02:45Z and 12:58:54Z, with a single top pattern: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'. First occurrence 2026-03-17T11:02:45Z.
  - `F10` ✔ exchange-gateway is the origin of the failure signal: first upstream connect timeout to the external partner at 11:01:25Z (earlier than order-service's first 504 at 11:02:45Z), plus upstream 503s from exchange-api starting 11:02:20Z, and a circuit breaker opening for exchange-api at 11:03:26Z (failure ratio 0.47 over 60s). Patterns: 163x 'upstream connect timeout ... host=api.exchange-partner.example op=placeOrder', 146x 'op=quoteSnapshot', 161x 'exchange-api returned 503 Service Unavailable'.
  - `F11` ✔ exchange-gateway error_rate also jumped: flat ~0.17-0.23% through the 10:51 bucket, then 24.93% in the 10:58 bucket and ~43-45% sustained from 11:05 onward — a larger error rate than order-service's ~30%, consistent with order-service failing only the exchange-dependent subset of calls.
  - `F12` ✔ order-service latency_p99_ms jumped from ~149ms baseline (10:30-10:55 buckets) to 3264.85 avg in the 11:00 bucket and ~5300ms sustained from 11:05 — consistent with 504s timing out at the 5000ms exchange-gateway upstream timeout, not with internal slowness. CPU (~38-41%) and db_pool_in_use (~13.6-14.5 connections) on order-service stayed flat across onset, so no local resource saturation.
  - `F18` ✔ The other in-window change, cfg-2290 (pricing-engine cache.ttl_seconds 5 -> 4 at 2026-03-17T09:20:00Z), sits upstream of a different branch of order-service's graph (order-service -> pricing-engine -> market-data-service/redis-cache) and is >1h41m before onset; no config change exists on order-service itself (query scoped to order-service returned count 0).
  - `F8` ✔ order-service error_rate was flat ~0.16-0.23% through the 10:55 bucket (avg 0.21), then jumped to avg 19.02% (max 31.9) in the 11:00 bucket and stayed ~30-32% through 12:55. 5-min buckets, so true onset is between 10:55 and 11:05.
**Assumptions**
  - A 1-second TTL reduction is enough to cause failures
  - The 100-minute gap between the change and onset is a delayed effect rather than a coincidence
**Rejected because:** cfg-2290 is on a different branch of the graph (order-service → pricing-engine → market-data/redis) than the failing hop (order-service → exchange-gateway → exchange-api) and occurred >1h41m before onset (F18, F3). The observed error signature names exchange-gateway, not pricing-engine, and order-service latency matches the 5s exchange timeout (F9, F10, F12).

## Facts (verified observations)
- Across all services in 2026-03-17T06:00Z-13:00Z there was exactly one deployment: order-service v2.15.2 (dep-5333), deployed 2026-03-17T10:48:00Z, summary 'Add idempotency keys to order submit', status 'rolled_back' at 2026-03-17T11:16:00Z. [F1, F14]
- The only config change in the window was cfg-2290: pricing-engine cache.ttl_seconds 5 -> 4 at 2026-03-17T09:20:00Z; no feature-flag, infra/network or dependency-version change appears. [F3, F4]
- order-service error_rate was flat ~0.16-0.23% through the 10:55 bucket, then jumped to avg 19.02% (max 31.9) in the 11:00 bucket and stayed ~30-32% through 12:55, placing true onset between 10:55 and 11:05. [F8]
- order-service logged 349 ERROR entries from 11:02:45Z to 12:58:54Z with a single dominant pattern: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' (first occurrence 11:02:45Z). [F9]
- exchange-gateway is the origin of the failure signal: first upstream connect timeout to api.exchange-partner.example at 11:01:25Z (before order-service's first 504 at 11:02:45Z), exchange-api 503s from 11:02:20Z, and a circuit breaker opening for exchange-api at 11:03:26Z (failure ratio 0.47 over 60s). [F10]
- exchange-gateway error_rate jumped from ~0.17-0.23% (through 10:51) to 24.93% in the 10:58 bucket and ~43-45% sustained from 11:05, exceeding order-service's ~30%, consistent with order-service failing only the exchange-dependent subset of calls. [F11]
- order-service latency_p99 jumped from ~149ms baseline to 3264.85ms in the 11:00 bucket and ~5300ms sustained from 11:05, consistent with the 5000ms exchange-gateway upstream timeout; order-service CPU (~38-41%) and db_pool_in_use (~13.6-14.5) stayed flat across onset. [F12]
- order-service's dependency graph runs order-service -> exchange-gateway -> exchange-api (external, has_telemetry:false); exchange-gateway is shared:false, and exchange-api has no telemetry of its own, so the failing hop is not observable in-platform. [F5, F17]
- BLAST RADIUS MISMATCH: the failure signal originates at exchange-gateway -> external exchange-api, and the gateway is not shared with other services and is not part of the changed code path; the deployed change only altered the order-service submit handler. [F17]
- cfg-2290 sits on a different branch of order-service's graph (order-service -> pricing-engine -> market-data-service/redis-cache), >1h41m before onset, and no config change exists on order-service itself. [F18]
- payment-service error_rate stayed flat ~0.17-0.26% across the entire 10:30-12:57 window with no onset at 11:00, so the blast radius in error rate does not include payment-service. [F13]
- Historical precedent HIST-103 (2025-10-21) 'Order failures during exchange partner outage' shares the current signature (order-service + exchange-gateway, '504 on order submit', 'upstream timeouts', root cause 'External exchange API outage on the partner side', category dependency_failure); HIST-115 (order-service 'pool exhausted', config_change) and HIST-111 (bad_deployment) are the rollback-blind precedents. [F7, F19]
- A second deployment query restricted to the post-rollback window 11:10-13:00Z returned 0 deployments. [F14]

## Assumptions (not verified)
- The external exchange partner genuinely had an outage/degradation beginning ~11:01Z; this is unobservable in-platform (exchange-api has_telemetry:false), so it is assumed rather than verified.
- order-service's error set is dominated by the exchange-dependent subset of order-submit calls, which explains the gap between order-service's ~30% and exchange-gateway's ~44% error rate.
- The 11:16Z rollback was actually executed; only the 'rolled_back' status and rolled_back_at field are available, with no rollback deploy record, revert commit/PR, or rollback event id.
- The error-rate series through 12:55 (~30-32%) reliably reflects the post-rollback period, since no independent order-service error_rate query was run specifically for 11:16-13:00Z.
- The 'single top error pattern is a 504 on exchange-gateway' log characterization reflects the aggregate error set and not a single-sampled error line.
- The absence of a config/network/dependency-version change is inferred from the change-scoped queries performed, not from a full audit of all change types.
- (demoted from facts: no verified observation backs it) TIMING MISMATCH: dep-5333 deployed 10:48:00Z but the failure signal began ~13 minutes later (first exchange-gateway timeout 11:01:25Z; order-service error_rate still flat ~0.16-0.23% through 10:55).

## Timeline
- `2026-03-17T06:00:00Z` Start of the investigated window; no deployments or config changes occurred in the preceding 18 hours (2026-03-16T12:00Z-2026-03-17T06:00Z). [F4]
- `2026-03-17T09:20:00Z` cfg-2290 applied: pricing-engine cache.ttl_seconds 5 -> 4 by config-bot (on a different branch of order-service's graph, >1h41m before onset). [F3, F18]
- `2026-03-17T10:48:00Z` order-service v2.15.2 (dep-5333) 'Add idempotency keys to order submit' deployed by a.khan - the only deployment anywhere in the window. [F1]
- `2026-03-17T10:51:00Z` exchange-gateway error_rate last flat bucket (~0.17-0.23%) before onset. [F11]
- `2026-03-17T10:55:00Z` order-service error_rate still flat ~0.16-0.23% through this bucket (~7 minutes after the deploy); no symptom yet. [F8, F15]
- `2026-03-17T10:58:00Z` exchange-gateway error_rate jumps to 24.93% - earliest bucket showing degradation. [F11]
- `2026-03-17T11:01:25Z` First exchange-gateway upstream connect timeout to api.exchange-partner.example (op=placeOrder) - origin of the failure signal. [F10, F15]
- `2026-03-17T11:02:20Z` exchange-api begins returning 503 Service Unavailable to exchange-gateway. [F10]
- `2026-03-17T11:02:45Z` First order-service ERROR 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; error_rate jumps in the 11:00 bucket (avg 19.02%, max 31.9). [F8, F9]
- `2026-03-17T11:03:26Z` Circuit breaker opens for exchange-api at exchange-gateway (failure ratio 0.47 over 60s). [F10]
- `2026-03-17T11:05:00Z` exchange-gateway error_rate ~43-45% sustained; order-service latency_p99 ~5300ms sustained (matching the 5s exchange timeout). [F11, F12]
- `2026-03-17T11:10:00Z` Incident INC-003 reported by on-call SRE (order submit failure rate ~30%, 504s). [F2]
- `2026-03-17T11:16:00Z` order-service v2.15.2 marked rolled_back (rolled_back_at) - ~6 min after report, ~28 min after deploy; the rollback was not part of the reported trigger chain. [F1, F2]
- `2026-03-17T11:16:00Z-2026-03-17T12:55:00Z` Post-rollback period: order-service error_rate stayed ~30-32% through 12:55 and no further deployments occurred in 11:10-13:00Z - the rollback did not restore service. [F8, F10, F11, F14]
- `2026-03-17T12:57:00Z-2026-03-17T12:58:54Z` payment-service error_rate still flat ~0.17-0.26% (no onset); last logged order-service error at 12:58:54Z. [F13, F9]

## Recommended actions
- **[immediate]** Do NOT rely on the dep-5333 rollback as the mitigation: the error rate held ~30-32% well after the 11:16Z rollback. Treat the rollback as complete and keep order-service on the pre-v2.15.2 revision rather than roll-forward, then mitigate at the exchange hop instead. -- The rollback (~11:16Z) did not change the symptom: error_rate stayed ~30-32% through 12:55, and timing/blast-radius evidence places the origin upstream of the changed code path. [F8, F14, F15, F16]
- **[immediate]** Fail fast on the exchange-dependent path: at exchange-gateway, set/verify the exchange-api circuit breaker to open on error (currently opened late at 11:03:26Z, ~2 min after the first connect timeout) and shorten the connect timeout so order-submit returns a fast, well-formed error instead of hanging to the 5000ms upstream timeout; make the order-service submit path degrade gracefully (skip/queue the exchange-dependent step) rather than 504. -- Latency_p99 on order-service (~5300ms) matches the 5000ms exchange upstream timeout, and the breaker only opened ~2 minutes into the failure, so users see 504s for the full timeout window. [F9, F10, F12]
- **[immediate]** Engage the external exchange partner and confirm/obtain partner-side status for api.exchange-partner.example for the window starting ~11:01Z, since the failing hop has no in-platform telemetry; escalate per the previous exchange-partner-outage playbook. -- The only failure signals available are gateway-side connect timeouts, upstream 503s and a breaker opening for exchange-api; exchange-api is external with has_telemetry:false so the partner must confirm the outage. [F10, F16, F17]
- **[short_term]** Verify recovery with the specific series that carried the evidence: exchange-gateway error_rate (was ~43-45%), order-service error_rate (was ~30-32%), order-service latency_p99 (~5300ms back toward ~149ms baseline), and the disappearance of the 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' pattern. -- These are the exact series/patterns that defined onset and can confirm whether the partner has recovered; success is a return of all four to pre-onset baselines. [F8, F9, F10, F11, F12]
- **[short_term]** Retire mutating rollbacks as the default first response for this signature: add a pre-check that inspects the dominant error pattern (exchange-gateway 504/upstream timeout vs a code-path error) before rolling back a release, so an exchange-partner outage does not trigger an unnecessary rollback. -- H2 (the reporter's 'idempotency keys' suspicion) was rejected precisely because the dominant error names exchange-gateway and the rollback did not change the error rate - a pattern check would have avoided the rollback. [F8, F9, F10, F11]
- **[long_term]** Add health/telemetry for the exchange hop: a synthetic placeOrder/quoteSnapshot probe plus partner error-budget and latency SLOs for api.exchange-partner.example, surfaced in the same view as exchange-gateway, and an alert on upstream connect-timeout rate and breaker state. -- The failing hop is currently blind (has_telemetry:false) and was only detected indirectly through exchange-gateway, which delayed attribution during this incident. [F10, F16, F17]
- **[long_term]** Add an automated discriminating check to the incident playbook: on order-submit 504s, compare exchange-gateway error_rate against order-service error_rate (subset test) and check order-service CPU/db_pool (local-saturation test) before declaring root cause, codifying the errors from the rejected H2/H3/H4 branches. -- The three rejected hypotheses were each eliminated by a specific observable margin (subset/super-set error rates, flat local resources, unrelated dependency branch) that can be automated into triage. [F8, F11, F12, F16, F18]

## Data gaps
- No telemetry exists for the failing hop itself: exchange-api is external with has_telemetry:false, so the partner-side outage is inferred from gateway-side symptoms (connect timeouts, 503s, breaker) and cannot be directly observed in-platform.
- No partner-side status page, error budget, or incident record for api.exchange-partner.example was queried, so the external event behind the 11:01Z onset is unconfirmed.
- No independent order-service error_rate query was run specifically for the 11:16-13:00Z post-rollback period; the 'rollback did not fix it' conclusion rests on the existing F8/F10/F11 series rather than an independent pull, so a transient change at exactly 11:16Z could have been missed.
- get_deployments returned only a rolled_back_at field for dep-5333 - no rollback deploy record, revert commit/PR id, or rollback event id - so whether the rollback was actually executed and completed is unverified.
- exchange-gateway's own cpu_pct/memory_pct/latency_p99 were never queried, so exchange-gateway-internal resource saturation as a contributor to the upstream timeouts cannot be fully excluded (only dismissed on mechanism evidence: the error text names the external host).
- inventory-service error_rate query returned budget_exhausted and never executed, so the blast radius is incomplete; other order-service dependents were not checked either.
- No database or queue component metrics (order DB, exchange-gateway DB) were inspected around onset, so local saturation at those components is not addressed.
- The order-service INFO/WARN log query (including any v2.15.2 deploy/startup line) returned budget_exhausted, so the exact deploy timestamp could not be confirmed directly and onset-vs-deploy correlation remains indirect.
- No metric queries were run for ledger-db/kafka/redis-cache saturation in this window; the ruling-out of shared-component contribution comes from the absence of local saturation on order-service, not from direct evidence at those components.
- The historical-incident search for pricing-engine/quote-cache-TTL/deployment-regression precedent was never executed (budget exhausted), so there is no precedent evidence on whether a cache.ttl_seconds reduction causes order-submit failures.
- Historical incident results provided only id/title/root_cause summaries with no resolution timelines, so precedents remain leads only and cannot be compared quantitatively to the current resolution window.

## What would change this conclusion
- Partner-side confirmation that the external exchange API was healthy from 11:01Z onward, or partner status showing no incident, would remove the external cause and push the investigation back toward H2/H3/H4.
- Discovery that the dep-5333 rollback never actually executed (e.g. the 'rolled_back' status was stale and v2.15.2 still served traffic) would invalidate the key 'rollback did not help' argument and could re-open H2.
- An order-service error_rate query showing a sharp return to ~0.2% at exactly 11:16Z would show the rollback did fix the symptom and point to the deployment after all.
- Evidence that the dominant errors are code-path errors rather than exchange-gateway 504s (e.g. a local order-submit stack trace, NPE, or duplicate-idempotency-key error dominating at 11:02+) would contradict the external-cause signature.
- Exchange-gateway-internal saturation at onset (very high CPU/thread/connection usage inside the gateway itself, with the external peer healthy) would move the cause to H4 (gateway capacity) rather than the external partner.
- Evidence that other ledger-db/kafka/redis-cache sharers (portfolio-service, settlement-service, notification-service) degraded at the same time would raise a shared-component cause not currently represented in the ranking.
- A retroactive config/flag/network change on exchange-gateway or order-service found in the 11:00-11:01Z window would displace the external-outage explanation.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: TIMING MISMATCH: dep-5333 deployed 10:48:00Z but the failure signal began ~13 mi

## Investigation stats
- latency 144.21s, LLM calls 13, tokens 87905 (in 69979 / out 17926)
- tool calls 24 (0 failed, 0 partial, 0 recovered by retry), findings 19/19 verified
