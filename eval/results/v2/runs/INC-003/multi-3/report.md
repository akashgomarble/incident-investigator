# Incident investigation report: INC-003
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause: the vendor-owned external exchange partner API (api.exchange-partner.example), reached only via exchange-gateway, began failing around 11:00–11:01Z — exchange-gateway's outbound calls hit 5s connect timeouts and 503s, its circuit breaker opened at 11:03:26Z, and its error rate (~40%) and p99 latency (~5s) spiked. order-service, which fans every placeOrder through exchange-gateway, then received 504 Gateway Timeouts on ~31% of submits; all 349 of its errors are outbound 'exchange-gateway call failed: 504', with no app-side 5xx, DB, or idempotency-key signature. Confidence 0.74 (medium, capped because the partner's own telemetry does not exist and the initiating hop is inferred from exchange-gateway's outbound view). The order-service v2.15.2 release (dep-5333) and the pricing-engine config change (cfg-2290) are both contradicted: failures persisted ~1h40m after the 11:16Z rollback, and cfg-2290 landed 09:20Z with a ~1.5h quiet gap before onset. A shared-infra/datastore cause (H4) is refuted for its distinguishing mechanism — the other exchange-gateway caller, market-data-service, shows no shared onset or latency rise.

## Probable root cause
**External exchange partner API (exchange-api) failure/outage drives 504s propagating up through exchange-gateway to order-service** -- dependency_failure in `exchange-api (external partner API, reached via exchange-gateway)`, triggered by `null` (confidence 0.74, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External exchange partner API (exchange-api) failure/outage drives 504s propagating up through exchange-gateway to order-service | dependency_failure | exchange-api (external partner API, reached via exchange-gateway) | null | 0.74 (medium) | active |
| 2 | H4: Shared infrastructure/network (mesh, DNS, egress) or shared datastore/cache (ledger-db/kafka/redis-cache) degradation at ~11:00Z affects exchange-gateway's connectivity | infrastructure_failure | shared platform component (mesh/DNS/network serving exchange-gateway) or shared datastore/cache (ledger-db/kafka/redis-cache) | null | 0.08 (low) | active |
| 3 | H2: order-service v2.15.2 ('idempotency keys on order submit') introduced the regression | bad_deployment | order-service | dep-5333 | 0.05 (low) | rejected |
| 4 | H3: Shared config change cfg-2290 (pricing-engine cache TTL 5->4s) degraded downstream and cascaded | config_change | pricing-engine | cfg-2290 | 0.03 (low) | rejected |
| 5 | H5: Resource exhaustion / capacity saturation on a downstream dependency drives the timeouts | resource_exhaustion | order-service / ledger-db pool | null | 0.03 (low) | rejected |

### H1: External exchange partner API (exchange-api) failure/outage drives 504s propagating up through exchange-gateway to order-service
The vendor-owned external exchange API (api.exchange-partner.example) began failing/slowing around 11:00–11:01Z. exchange-gateway's outbound calls to it hit 5s connect timeouts and 503s, its circuit breaker opened, and its own error rate and upstream latency spiked. Every order-service placeOrder that fans through exchange-gateway then received a 504 Gateway Timeout. order-service is the loudest victim of a partner-side dependency failure, not a fault of its own code or of a shared platform component.

**Causal chain**
1. External exchange-api (has_telemetry=false, vendor-owned, zero recorded changes) begins refusing/slowing outbound requests at ~11:00–11:01Z [F17, F11, F27]
2. exchange-gateway's outbound calls to host=api.exchange-partner.example fail: 'upstream connect timeout after 5000ms' and 'exchange-api returned 503'; the failure hits BOTH operations (placeOrder 23, quoteSnapshot 17), i.e. host-level not caller-specific [F11, F23]
3. exchange-gateway's first connect timeout (11:01:25Z) precedes order-service's first 504 (11:02:45Z) — deepest visible error precedes the symptom it explains [F21]
4. exchange-gateway circuit breaker opens (failure ratio 0.47/60s at 11:03:26Z), amplifying/sustaining rejections [F11]
5. exchange-gateway error_rate 0.2%->40.44% and upstream_latency 120ms->8550ms in the 11:00Z bucket; its p99 125ms->5152ms [F10, F20]
6. order-service's placeOrder calls to exchange-gateway return 504: 100% of its 349 ERROR logs are 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; error_rate 0.2%->~31%, p99 150ms->~5300ms [F8, F22, F7, F9]
7. Failures persist ~1h40m after the 11:16Z rollback of dep-5333 because the external partner is still degraded [F14, F7]

**Supporting evidence**
  - `F11` ✔ exchange-gateway logs show the deepest visible fault: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163), 'exchange-api returned 503 Service Unavailable' (161), and 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26Z — pointing to the external exchange partner/API as the origin, with circuit-breaker amplification in exchange-gateway.
  - `F17` ✔ exchange-gateway's only outbound edge is to exchange-api, kind 'external' with has_telemetry=false (vendor-owned); its callers are order-service and market-data-service (both via http). This makes the external exchange-api the deepest node with no telemetry, matching the T1 finding of connect timeouts/503s against api.exchange-partner.example.
  - `F27` ✔ exchange-api, the external partner node in the dependency path, is registered as a service name in the change tools but has zero recorded deployments and zero config changes across 2026-03-17 (00:00–12:59Z deploys; 06:00–12:59Z config) — consistent with vendor-owned, untracked infra rather than a tracked change.
  - `F23` ✔ exchange-gateway's upstream connect-timeout logs hit BOTH of its operations (placeOrder 23 occurrences, quoteSnapshot 17 occurrences), i.e. the partner connect failure affects all outbound traffic to api.exchange-partner.example, not just the order path — consistent with a host-level partner connectivity fault rather than an order-service-specific caller defect.
  - `F21` ✔ exchange-gateway's first 'upstream connect timeout ... host=api.exchange-partner.example' (op=placeOrder) is at 11:01:25Z — BEFORE order-service's first 504 at 11:02:45Z, i.e. the deepest/furthest-downstream visible error precedes the order-service symptom it would have to explain.
  - `F10` ✔ The fault is also present at exchange-gateway, one hop downstream of order-service: its error_rate jumped from ~0.2% to avg 40.44% (max 45.46) in the same 11:00Z bucket, and its upstream_latency_ms jumped from ~120ms to avg 8550.82ms — so the shared symptom is at exchange-gateway and beyond, not local to order-service.
  - `F20` ✔ exchange-gateway p99 latency (latency_p99_ms) shows the same 11:00Z onset: flat ~125-135ms through 10:58Z, then avg 2645.0 (max 5152.47) at 11:00Z and ~5000-5200ms thereafter. market-data-service p99 latency is completely flat across the whole window (avg ~38-41ms, e.g. 38.78 at 11:00Z), i.e. the latency blow-up is confined to the exchange path and is NOT shared with the other exchange-gateway caller.
  - `F8` ✔ order-service error signature is exclusively outbound call timeouts: all 349 ERROR logs are 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', first at 11:02:45Z — i.e. 504/gateway-timeout downstream, not an app 5xx in order-service itself.
  - `F22` ✔ order-service's ERROR population over 10:30-12:59Z is 100% exchange-gateway 504s: total_matched 349, counts_by_level {ERROR:349}, and the single top pattern is 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder) orderId=<n>' with count 349 (first 11:02:45Z, last 12:58:54Z). No app-side 5xx, DB error or idempotency-key error pattern appears in the result.
  - `F9` ✔ order-service p99 latency leapt from ~150ms baseline to avg 4648.8ms (max 5665) in the 11:00Z bucket (≈31x), confirming the degradation started at 11:00Z, and stayed ~5300ms through 12:45Z.
  - `F7` ✔ order-service error_rate was flat at ~0.2% (max ~0.3) from 07:00 to 10:52Z, then jumped to avg 23.12% (max 31.9) in the 11:00Z bucket and stayed ~31% through 12:52Z. Onset is in the 11:00–11:08Z window, matching the 11:10Z report, not earlier.
  - `F14` ✔ The order-service deploy (10:48Z) was rolled back at 11:16Z, but the incident continued ~1h40m beyond that: order-service error_rate stayed ~31% through 12:52Z (T1/F7) — a rollback that fails to clear the symptom argues against the release being the initiating cause.
  - `F3` ✔ order-service's dependency graph: outbound edges to ledger-db (kind db), pricing-engine (http), exchange-gateway (http), kafka (queue-produce); inbound api-gateway -> order-service. Transitive path order-service -> pricing-engine -> {market-data-service (http), redis-cache (cache)} and exchange-gateway -> exchange-api (external, has_telemetry false).
  - `F19` ✔ exchange-gateway and market-data-service (the two exchange-gateway callers) do NOT share the same onset: exchange-gateway error_rate is flat ~0.2% through 10:58Z then jumps in the 11:00Z bucket (avg 22.79, max 45.46), while market-data-service error_rate is unchanged at 0.22% in the same 11:00Z bucket and only rises at 11:02Z (avg 6.5). Both are far larger than baselines, and market-data-service's rise is ~2 min later and much smaller (6.5 vs 42-45).
  - `F24` ✔ The shared datastores could not be evaluated with an error_rate query: ledger-db exposes only [connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min] and kafka only [cpu_pct, messages_in_per_s, under_replicated_partitions] — neither has an error_rate metric, so their saturation/health could not be compared against the 11:00Z onset in this round.
**Contradicting evidence**
  - (none)
**Assumptions**
  - exchange-api has no telemetry, so its own degradation is inferred from exchange-gateway's outbound symptoms and cannot be measured directly
  - No local mesh/DNS/egress fault contributed to the connect timeouts (unsupported but plausible; the fault is exchange-path-specific per F19/F20)
  - Partner degradation onset is ~11:00–11:01Z; only bucket-level resolution is available, so exact sub-minute ordering vs dep-5333 (10:48Z) is not independently pinned
  - The vendor has not confirmed the outage; no 2026-03-17 external record exists (F28)

### H4: Shared infrastructure/network (mesh, DNS, egress) or shared datastore/cache (ledger-db/kafka/redis-cache) degradation at ~11:00Z affects exchange-gateway's connectivity
A shared platform component not captured by the deploy/config tools (service mesh/envoy, DNS, network egress, or a shared datastore such as ledger-db, kafka or redis-cache) degraded at ~11:00Z and caused the exchange-path timeouts, making order-service the loudest visible victim. Its core mechanism — a shared component degrading multiple consumers at the same onset — is now refuted: exchange-gateway's other caller (market-data-service) shows no shared onset or latency rise, and exchange-path latency is not shared. H4 survives only in a narrowed form: a shared component that selectively affects exchange connectivity while leaving market-data-service's exchange calls untouched, which is now hard to construct from the evidence.

**Causal chain**
1. A shared mesh/DNS/network or datastore component degrades around 11:00Z without a recorded rollout (change tools do not cover mesh/DNS/node/volume) [F30, F25]
2. exchange-gateway's outbound connections to exchange-api time out at 5s (connectivity-level failure) [F11]
3. exchange-gateway errors (40%) and latency spike; circuit breaker opens [F10, F11]
4. order-service's exchange-gateway calls return 504, producing ~30% submit failures [F8, F7]
5. NOTE: this mechanism predicts a shared onset across exchange-gateway consumers; market-data-service (the other caller) instead stays flat at 0.22% error / ~38ms p99 at 11:00Z, refuting the shared-component reading [F19, F20]

**Supporting evidence**
  - `F30` ✔ Because the only change sources queried are per-service deploy/config registries with no mesh, DNS, node or volume coverage, the observed silence about infra changes in 10:30–11:10Z cannot be used to dismiss H4 (mesh/DNS/egress or shared-datastore degradation) — the evidence base for that dismissal is invalid, though H4 is also not positively supported.
  - `F25` ✔ The change-tool service registry enumerates exactly 18 named services and contains no envoy/service-mesh, DNS, node or volume entity: get_deployments and get_config_changes for service 'envoy' both fail with not_found, listing known_services as only api-gateway, auth-service, order-service, portfolio-service, pricing-engine, market-data-service, payment-service, settlement-service, notification-service, exchange-gateway, ledger-db, user-db, redis-cache, kafka, exchange-api, bank-partner-api, kyc-provider, email-provider. So these tools structurally DO NOT cover mesh/DNS/node/volume change sources.
  - `F26` ✔ What the change tools DO cover: per-service deployment records and per-service config/flag records for the named services. An explicit api-gateway deployment query for the infra window 10:30–11:10Z returned count 0, and an all-services config-change query for the same infra-only window also returned count 0. Absence here only excludes deploys/config for the named services in that window; it says nothing about mesh/DNS/node/volume.
**Contradicting evidence**
  - `F19` ✔ exchange-gateway and market-data-service (the two exchange-gateway callers) do NOT share the same onset: exchange-gateway error_rate is flat ~0.2% through 10:58Z then jumps in the 11:00Z bucket (avg 22.79, max 45.46), while market-data-service error_rate is unchanged at 0.22% in the same 11:00Z bucket and only rises at 11:02Z (avg 6.5). Both are far larger than baselines, and market-data-service's rise is ~2 min later and much smaller (6.5 vs 42-45).
  - `F20` ✔ exchange-gateway p99 latency (latency_p99_ms) shows the same 11:00Z onset: flat ~125-135ms through 10:58Z, then avg 2645.0 (max 5152.47) at 11:00Z and ~5000-5200ms thereafter. market-data-service p99 latency is completely flat across the whole window (avg ~38-41ms, e.g. 38.78 at 11:00Z), i.e. the latency blow-up is confined to the exchange path and is NOT shared with the other exchange-gateway caller.
  - `F23` ✔ exchange-gateway's upstream connect-timeout logs hit BOTH of its operations (placeOrder 23 occurrences, quoteSnapshot 17 occurrences), i.e. the partner connect failure affects all outbound traffic to api.exchange-partner.example, not just the order path — consistent with a host-level partner connectivity fault rather than an order-service-specific caller defect.
  - `F17` ✔ exchange-gateway's only outbound edge is to exchange-api, kind 'external' with has_telemetry=false (vendor-owned); its callers are order-service and market-data-service (both via http). This makes the external exchange-api the deepest node with no telemetry, matching the T1 finding of connect timeouts/503s against api.exchange-partner.example.
  - `F12` ✔ Compute resources on both affected services are flat across onset: order-service cpu_pct ~39–40% and exchange-gateway cpu_pct ~29–30% show no spike at 11:00Z, so the 504/timeout storm is not caused by local CPU saturation on either service. (Interpretation: resource-exhaustion on order-service or exchange-gateway is ruled out by CPU; downstream/partner-side latency is the driver.)
  - `F14` ✔ The order-service deploy (10:48Z) was rolled back at 11:16Z, but the incident continued ~1h40m beyond that: order-service error_rate stayed ~31% through 12:52Z (T1/F7) — a rollback that fails to clear the symptom argues against the release being the initiating cause.
**Assumptions**
  - A shared component could selectively degrade only the exchange path while leaving market-data-service's exchange calls and latency untouched (no evidence this is constructible)
  - Absence of a recorded infra change is not proof of no infra fault — but absence of any runtime corroboration is also not support

### H2: order-service v2.15.2 ('idempotency keys on order submit') introduced the regression
The reporter's suspicion: dep-5333 changed the order submit path (adding idempotency keys), producing 504s on ~30% of submits. Under this reading the timeouts are an order-service-side defect.

**Causal chain**
1. dep-5333 deploys order-service v2.15.2 with new idempotency-key logic in order submit [F1]
2. The new submit path produces slow/failed outbound calls on a fraction of requests *(assumed)*
3. Error rate and p99 rise on order-service [F7, F9]

**Supporting evidence**
  - `F1` ✔ The only deployment recorded across ALL services between 06:00–12:59Z is dep-5333: order-service v2.15.2, deployed 2026-03-17T10:48:00Z by a.khan, summary 'Add idempotency keys to order submit', status 'rolled_back' with rolled_back_at 2026-03-17T11:16:00Z. The same single record was returned on a wider 00:00–12:59Z re-query.
**Contradicting evidence**
  - `F8` ✔ order-service error signature is exclusively outbound call timeouts: all 349 ERROR logs are 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', first at 11:02:45Z — i.e. 504/gateway-timeout downstream, not an app 5xx in order-service itself.
  - `F22` ✔ order-service's ERROR population over 10:30-12:59Z is 100% exchange-gateway 504s: total_matched 349, counts_by_level {ERROR:349}, and the single top pattern is 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder) orderId=<n>' with count 349 (first 11:02:45Z, last 12:58:54Z). No app-side 5xx, DB error or idempotency-key error pattern appears in the result.
  - `F10` ✔ The fault is also present at exchange-gateway, one hop downstream of order-service: its error_rate jumped from ~0.2% to avg 40.44% (max 45.46) in the same 11:00Z bucket, and its upstream_latency_ms jumped from ~120ms to avg 8550.82ms — so the shared symptom is at exchange-gateway and beyond, not local to order-service.
  - `F14` ✔ The order-service deploy (10:48Z) was rolled back at 11:16Z, but the incident continued ~1h40m beyond that: order-service error_rate stayed ~31% through 12:52Z (T1/F7) — a rollback that fails to clear the symptom argues against the release being the initiating cause.
  - `F11` ✔ exchange-gateway logs show the deepest visible fault: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163), 'exchange-api returned 503 Service Unavailable' (161), and 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26Z — pointing to the external exchange partner/API as the origin, with circuit-breaker amplification in exchange-gateway.
  - `F21` ✔ exchange-gateway's first 'upstream connect timeout ... host=api.exchange-partner.example' (op=placeOrder) is at 11:01:25Z — BEFORE order-service's first 504 at 11:02:45Z, i.e. the deepest/furthest-downstream visible error precedes the order-service symptom it would have to explain.
  - `F12` ✔ Compute resources on both affected services are flat across onset: order-service cpu_pct ~39–40% and exchange-gateway cpu_pct ~29–30% show no spike at 11:00Z, so the 504/timeout storm is not caused by local CPU saturation on either service. (Interpretation: resource-exhaustion on order-service or exchange-gateway is ruled out by CPU; downstream/partner-side latency is the driver.)
**Assumptions**
  - The v2.15.2 code path itself is faulty
  - An order-service defect could manifest as outbound 504s rather than app-side 5xx
**Rejected because:** F8/F22 show all 349 order-service errors are 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', i.e. downstream, with no app-side 5xx, DB or idempotency-key error pattern. F10 shows the same fault one hop downstream at exchange-gateway, and F11/F21 localize the deepest/source error to the external partner (and it precedes the order-service symptom). F14 shows error rate stayed ~31% for ~1h40m AFTER the 11:16Z rollback of dep-5333. No deployment exists on exchange-gateway (F13), so the shared symptom there cannot come from order-service code.

### H3: Shared config change cfg-2290 (pricing-engine cache TTL 5->4s) degraded downstream and cascaded
The only config change in the window, cfg-2290 reducing pricing-engine cache.ttl_seconds from 5 to 4, increased cache misses on pricing-engine, driving load onto market-data-service/redis-cache and degrading the path into order-service's submit failures.

**Causal chain**
1. cfg-2290 lowers pricing-engine cache TTL 5->4s at 09:20Z, raising miss rate [F2, F15]
2. Higher miss rate increases pricing-engine load on market-data-service and redis-cache *(assumed)*
3. Degraded pricing path slows order-service traffic and surfaces as 504s on submit *(assumed)*

**Supporting evidence**
  - `F2` ✔ The only config change recorded across ALL services between 06:00–12:59Z is cfg-2290: pricing-engine cache.ttl_seconds changed 5 -> 4 at 2026-03-17T09:20:00Z by config-bot. No feature-flag record was returned by the config-change query.
  - `F3` ✔ order-service's dependency graph: outbound edges to ledger-db (kind db), pricing-engine (http), exchange-gateway (http), kafka (queue-produce); inbound api-gateway -> order-service. Transitive path order-service -> pricing-engine -> {market-data-service (http), redis-cache (cache)} and exchange-gateway -> exchange-api (external, has_telemetry false).
**Contradicting evidence**
  - `F7` ✔ order-service error_rate was flat at ~0.2% (max ~0.3) from 07:00 to 10:52Z, then jumped to avg 23.12% (max 31.9) in the 11:00Z bucket and stayed ~31% through 12:52Z. Onset is in the 11:00–11:08Z window, matching the 11:10Z report, not earlier.
  - `F8` ✔ order-service error signature is exclusively outbound call timeouts: all 349 ERROR logs are 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', first at 11:02:45Z — i.e. 504/gateway-timeout downstream, not an app 5xx in order-service itself.
  - `F10` ✔ The fault is also present at exchange-gateway, one hop downstream of order-service: its error_rate jumped from ~0.2% to avg 40.44% (max 45.46) in the same 11:00Z bucket, and its upstream_latency_ms jumped from ~120ms to avg 8550.82ms — so the shared symptom is at exchange-gateway and beyond, not local to order-service.
  - `F11` ✔ exchange-gateway logs show the deepest visible fault: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163), 'exchange-api returned 503 Service Unavailable' (161), and 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26Z — pointing to the external exchange partner/API as the origin, with circuit-breaker amplification in exchange-gateway.
  - `F15` ✔ The only config change in 08:00–12:59Z is cfg-2290: pricing-engine cache.ttl_seconds 5 -> 4, changed_at 2026-03-17T09:20:00Z; no exchange-gateway config change exists (count 0). The config change precedes the release and onset by ~1.5h with no reported deviation at 09:20Z.
  - `F20` ✔ exchange-gateway p99 latency (latency_p99_ms) shows the same 11:00Z onset: flat ~125-135ms through 10:58Z, then avg 2645.0 (max 5152.47) at 11:00Z and ~5000-5200ms thereafter. market-data-service p99 latency is completely flat across the whole window (avg ~38-41ms, e.g. 38.78 at 11:00Z), i.e. the latency blow-up is confined to the exchange path and is NOT shared with the other exchange-gateway caller.
**Assumptions**
  - A 1-second TTL reduction could materially change miss rate and downstream load
  - pricing-engine degradation would produce 504s specifically on exchange-gateway calls
**Rejected because:** Ordering is wrong: cfg-2290 lands 09:20Z but error rate stays flat ~0.2% until 10:52Z (F7), a ~1.5h quiet gap (F15). The error signature points at exchange-gateway/exchange-api (F8, F11), not on the pricing-engine path (F3); market-data-service (shared by pricing-engine and exchange-gateway) shows flat error and p99 latency at onset (F19, F20), so no pricing-path degradation is visible.

### H5: Resource exhaustion / capacity saturation on a downstream dependency drives the timeouts
A resource or capacity problem (DB connection pool exhaustion at order-service, ledger-db saturation, or exchange-gateway connection/thread pool saturation) causes calls to hang and time out, producing the 504 storm independent of any release.

**Causal chain**
1. Connection pool or datastore resources exhaust under normal traffic near 11:00Z *(assumed)*
2. Calls queue and exceed timeouts, surfacing as 504s on submit [F9, F7]
3. No spike in local compute accompanies onset (CPU flat on both affected services) [F12]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ order-service error signature is exclusively outbound call timeouts: all 349 ERROR logs are 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)', first at 11:02:45Z — i.e. 504/gateway-timeout downstream, not an app 5xx in order-service itself.
  - `F22` ✔ order-service's ERROR population over 10:30-12:59Z is 100% exchange-gateway 504s: total_matched 349, counts_by_level {ERROR:349}, and the single top pattern is 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder) orderId=<n>' with count 349 (first 11:02:45Z, last 12:58:54Z). No app-side 5xx, DB error or idempotency-key error pattern appears in the result.
  - `F12` ✔ Compute resources on both affected services are flat across onset: order-service cpu_pct ~39–40% and exchange-gateway cpu_pct ~29–30% show no spike at 11:00Z, so the 504/timeout storm is not caused by local CPU saturation on either service. (Interpretation: resource-exhaustion on order-service or exchange-gateway is ruled out by CPU; downstream/partner-side latency is the driver.)
  - `F10` ✔ The fault is also present at exchange-gateway, one hop downstream of order-service: its error_rate jumped from ~0.2% to avg 40.44% (max 45.46) in the same 11:00Z bucket, and its upstream_latency_ms jumped from ~120ms to avg 8550.82ms — so the shared symptom is at exchange-gateway and beyond, not local to order-service.
  - `F11` ✔ exchange-gateway logs show the deepest visible fault: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163), 'exchange-api returned 503 Service Unavailable' (161), and 'circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s)' at 11:03:26Z — pointing to the external exchange partner/API as the origin, with circuit-breaker amplification in exchange-gateway.
**Assumptions**
  - A pool/datastore exhaustion could manifest specifically as exchange-gateway-call 504s rather than DB errors
  - The unexecuted db_pool_in_use query would have shown saturation
**Rejected because:** F12 shows CPU flat on both affected services (~39-40% and ~29-30%) — no local saturation. F8/F22 show the error signature is exclusively outbound exchange-gateway 504s, not DB/latency errors, and F11 localizes the deepest fault to the external partner host. No metric shows a pool or datastore exhausting. Residual: db_pool_in_use and shared-datastore metrics were never queried (F24), so DB/pool exhaustion is contradicted rather than formally excluded, but the error signature (F8/F22) argues against it.

## Facts (verified observations)
- The deepest visible fault is at exchange-gateway's external dependency: logs show 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163) and 'exchange-api returned 503 Service Unavailable' (161), with the circuit breaker opening for exchange-api (failure ratio 0.47 over 60s) at 11:03:26Z. [F11, F17]
- The partner connect failure affects ALL outbound traffic to api.exchange-partner.example, not just the order path: timeouts hit both exchange-gateway operations placeOrder (23) and quoteSnapshot (17). [F23]
- exchange-gateway's first upstream connect timeout (11:01:25Z) precedes order-service's first 504 (11:02:45Z), i.e. the deepest visible error precedes the symptom it would explain. [F21]
- exchange-gateway itself shows the fault one hop downstream of order-service: error_rate jumped from ~0.2% to avg 40.44% (max 45.46) and upstream_latency_ms from ~120ms to avg 8550.82ms in the same 11:00Z bucket; p99 latency went from ~125–135ms to avg 2645ms (max 5152ms) then ~5000–5200ms. [F10, F20]
- 100% of order-service's error population over 10:30–12:59Z is outbound exchange-gateway 504s: 349 ERROR logs, single top pattern 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder) orderId=<n>', first at 11:02:45Z and last at 12:58:54Z; no app-side 5xx, DB error, or idempotency-key error pattern appears. [F8, F22]
- order-service error_rate was flat at ~0.2% from 07:00 to 10:52Z, then jumped to avg 23.12% (max 31.9) in the 11:00Z bucket and stayed ~31% through 12:52Z; p99 latency leapt from ~150ms to avg 4648.8ms (≈31x) in the 11:00Z bucket. [F7, F9]
- The order-service v2.15.2 deploy (dep-5333, 10:48Z) was rolled back at 11:16Z, but the incident continued ~1h40m beyond: order-service error_rate stayed ~31% through 12:52Z. [F1]
- No deployment exists on exchange-gateway (count 0) in 00:00–12:59Z, so the shared symptom at exchange-gateway cannot originate from order-service code. [F13]
- The two exchange-gateway callers do not share the same onset and latency profile: market-data-service error_rate is unchanged at 0.22% in the 11:00Z bucket (vs exchange-gateway avg 22.79/max 45.46) and rises only ~2 min later and much smaller (6.5); market-data-service p99 latency is flat ~38–41ms across the whole window. [F19, F20]
- The only deployment recorded across all services between 06:00–12:59Z (confirmed on a wider 00:00–12:59Z re-query) is dep-5333: order-service v2.15.2, deployed 10:48Z by a.khan, 'Add idempotency keys to order submit', status rolled_back. [F1, F13]
- The only config change recorded across all services between 06:00–12:59Z is cfg-2290: pricing-engine cache.ttl_seconds 5 -> 4 at 09:20Z by config-bot; no exchange-gateway config change exists (count 0), and the change precedes onset by ~1.5h with no deviation reported at 09:20Z. [F2, F15]
- exchange-api is an external node with has_telemetry=false, and exchange-gateway's only outbound edge is to it; exchange-api has zero recorded deployments and zero config changes across 2026-03-17, consistent with vendor-owned untracked infra. [F17, F27]
- The shared datastores could not be evaluated via error_rate: ledger-db exposes only [connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min] and kafka only [cpu_pct, messages_in_per_s, under_replicated_partitions], so their saturation could not be compared against the 11:00Z onset. [F24]
- The change-tool service registry enumerates exactly 18 named services with no mesh/DNS/node/volume entity (envoy queries fail with not_found), so absence of recorded infra changes in 10:30–11:10Z cannot be used to dismiss a mesh/DNS/egress or shared-datastore cause. [F25, F26]
- No tool returns vendor/partner status corroboration: the only external-facing channel is search_historical_incidents, which is explicitly labeled leads not evidence, and the sole partner-outage precedent (HIST-103) is dated 2025-10-21, with no 2026-03-17-dated external outage record in any returned result. [F28]
- order-service's dependency graph places shared components on the path — ledger-db (db), kafka (queue-produce), and redis-cache transitively via pricing-engine — and none of these has any recorded change in the incident window, so the fault path must be inspected at runtime rather than via change records. [F3, F4, F16]

## Assumptions (not verified)
- exchange-api has no telemetry, so its own degradation is inferred from exchange-gateway's outbound symptoms (connect timeouts, 503s) and cannot be measured directly.
- The partner degradation onset is ~11:00–11:01Z; only bucket-level resolution is available, so exact sub-minute ordering versus dep-5333 (10:48Z) is not independently pinned.
- No local mesh/DNS/egress fault contributed to the connect timeouts; the fault is exchange-path-specific per F19/F20, though this is unsupported rather than proven (the change tools do not cover mesh/DNS/egress).
- The partner has not confirmed the outage externally; no 2026-03-17 external record exists, so the partner-side origin rests on in-band telemetry/log evidence only.
- The 'no app-side/DB/idempotency error' claim rests on full-population log pattern counts (single pattern, count 349) rather than an exhaustive multi-level WARN/INFO search.
- (demoted from facts: no verified observation backs it) Compute is flat on both affected services across onset — order-service cpu_pct ~39–40%, exchange-gateway cpu_pct ~29–30% — so the 504/timeout storm is not caused by local CPU saturation.

## Timeline
- `2026-03-17T09:20:00Z` cfg-2290 applied: pricing-engine cache.ttl_seconds 5 -> 4 (config-bot). The only config change in the window; precedes onset by ~1.5h with no reported deviation. [F2, F15]
- `2026-03-17T10:48:00Z` dep-5333 deployed: order-service v2.15.2 'Add idempotency keys to order submit' (a.khan) — the only deployment across all services in the window; no exchange-gateway deployment exists. [F1, F13]
- `2026-03-17T10:52:00Z` order-service error_rate is still flat at ~0.2% (baseline), confirming no deviation in the ~1.5h after cfg-2290 and 4 min after the deploy. [F7]
- `2026-03-17T11:00:00Z` Onset in the 11:00Z bucket: order-service error_rate jumps to avg 23.12% (max 31.9) and p99 leaps from ~150ms to avg 4648.8ms; exchange-gateway error_rate jumps to avg 40.44% and p99 to avg 2645ms; market-data-service stays flat (error 0.22%, p99 ~38.78ms). [F7, F9, F10, F20, F19]
- `2026-03-17T11:01:25Z` exchange-gateway's first 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' — the deepest visible error, preceding order-service's first 504. [F21, F23]
- `2026-03-17T11:02:45Z` order-service's first error: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' — the start of the 349-error population, all of which are outbound exchange-gateway 504s. [F8, F22]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker OPEN for exchange-api (failure ratio 0.47 over 60s); 503s from exchange-api (161 occurrences) and connect timeouts (163) across both operations. [F11, F23]
- `2026-03-17T11:10:00Z` Incident INC-003 reported by on-call SRE: ~30% order submit failure rate, 504s on order submit; on-call rolling back order-service. [F7]
- `2026-03-17T11:16:00Z` dep-5333 (order-service v2.15.2) rolled back. [F1, F13, F14]
- `2026-03-17T11:16:00Z-12:52:00Z` Failures persist ~1h40m after rollback: order-service error_rate stays ~31% through 12:52Z, p99 ~5300ms through 12:45Z, last error at 12:58:54Z — arguing against the release as the initiating cause. [F14, F7, F9, F22]
- `2026-03-17T12:59:00Z` Latest data: incident still ongoing; exchange-api has zero recorded deployments/config changes all day, consistent with vendor-owned untracked infra. [F27]

## Recommended actions
- **[immediate]** Do NOT expect the 11:16Z rollback of dep-5333 (order-service v2.15.2) to clear the incident; treat the rollback as a precautionary measure only and refocus mitigation on the exchange-gateway → exchange-api path. -- F14/F7/F22 show error_rate stayed ~31% for ~1h40m after the rollback and all 349 errors are outbound exchange-gateway 504s; the release is contradicted as the initiating cause. [F14, F7, F22]
- **[immediate]** On exchange-gateway, keep the circuit breaker to exchange-api protective rather than forcing retries: reject fast (circuit OPEN) instead of allowing 5s connect-timeout pile-up, and confirm the breaker is tripping on the same failure-ratio threshold (0.47 over 60s) rather than being bypassed. -- F11 shows the breaker opened at 11:03:26Z after 5s connect timeouts and 503s; the timeout pile-up is what propagates as 504s into order-service (F8/F22, 5.3s p99 in F9). [F11, F8, F22, F9]
- **[immediate]** Engage the exchange partner (api.exchange-partner.example) via the vendor escalation channel and request their incident/status for 11:00Z onward, citing the specific failing operations placeOrder and quoteSnapshot. -- No in-tool channel exists for partner corroboration (F28) but the partner host is the deepest fault (F11/F17/F21) and the failure is host-level, affecting both operations (F23). [F11, F17, F21, F23, F28]
- **[short_term]** Add a graceful-degradation path for placeOrder when exchange-gateway returns 504/timeout: return a distinguishable, non-5xx response or queue-and-retry with bounded backoff so order-service error rate is decoupled from partner availability. -- F8/F22 show order-service's entire error population is unhandled propagation of exchange-gateway 504s; this is the mechanism by which a partner outage becomes a 30% submit failure rate (F7). [F8, F22, F7]
- **[short_term]** Install partner-facing SLO monitoring and alerting on exchange-gateway's outbound exchange-api signals (connect-timeout count, 503 count, breaker state, outbound p99) with alerting ahead of the customer-visible order-service error rate. -- F11/F20/F23 show the partner symptom is fully visible one hop up at exchange-gateway ~1–2 min before order-service fails (F21 precedes F8); alerting there gives earlier detection than the 11:10Z report. [F11, F20, F23, F21, F8]
- **[short_term]** Run and record the missing discriminating queries to close residual uncertainty: (a) exchange-path vs non-exchange-path latency/error on exchange-gateway's callers, (b) ledger-db (connections_active, slow_queries_per_min, replication_lag_ms), kafka (messages_in_per_s, under_replicated_partitions) and redis-cache metrics over 10:30–12:30Z, and (c) order-service db_pool_in_use. -- F24 and the gaps show these were never queried; they are the only remaining tests that could positively implicate a shared datastore (H4) or DB pool exhaustion rather than the partner (H1). [F24, F4, F16]
- **[long_term]** Add coverage for infra-only change sources (service mesh/envoy, DNS, node/volume) to the change registry or pair this investigation flow with an infra change feed. -- F25/F26/F30 show the change tools structurally cover only 18 named services, so mesh/DNS/node/volume changes at 10:30–11:10Z can be neither validated nor excluded; HIST-116 (envoy HTTP/2) and HIST-110 (volume IOPS) precedents show these sources matter. [F25, F26, F30]
- **[long_term]** Establish a vendor-integration dependency register that records, for every external partner (exchange-api, bank-partner-api, kyc-provider, email-provider), the partner's status/incident channel and a synthetic probe, so partner-side outages are directly observable rather than inferred. -- F17/F27/F28 show exchange-api has has_telemetry=false and no status tool exists, so the root cause of this exact signature must always be inferred from exchange-gateway's outbound view. [F17, F27, F28]
- **[long_term]** Review the change-management requirements for order-service submits, and explicitly retain/codify the rollback procedure validated here, but do not attribute this incident to dep-5333 in the postmortem without further evidence. -- F1/F13 show dep-5333 was the only deployment and was rolled back at 11:16Z, yet failures persisted (F14); HIST-103 (F18) shows the same signature was previously a partner-side failure. [F1, F13, F14, F18]

## Data gaps
- The originating hop (exchange-api / api.exchange-partner.example) has has_telemetry=false and no vendor status-page tool exists (F17, F28), so the partner's own degradation can only be inferred from exchange-gateway's outbound symptoms — this is the main reason confidence is capped at 0.74 rather than higher.
- Sub-bucket ordering between the 10:48Z dep-5333 deploy and exchange-gateway's first 11:01:25Z timeout is not resolvable (only 11:00Z bucket-level metrics, F7/F9), so the exact causal ordering of deploy vs partner onset is not pinned; note the release is separately contradicted by the post-rollback persistence (F14).
- Shared datastores/queue/cache were not evaluated: ledger-db and kafka expose no error_rate metric (F24) and their correct metrics plus redis-cache metrics were never queried, so H4 cannot be positively excluded on datastore telemetry — only on the exchange-path-specific evidence (F19/F20/F23).
- No mesh/DNS/egress/node/volume change source is exposed by the change tools (F25/F26), so whether an infra-only change occurred in the 10:30–11:10Z window remains unverified; absence of a recorded change cannot be read as absence of a fault (F30).
- order-service db_pool_in_use was never queried (budget exhausted), so DB connection-pool exhaustion as a contributing factor is unverified either way (though contradicted by the error signature in F8/F22).
- Only ERROR-level logs were searched (349 total, output truncated to 40); a WARN/INFO sweep for DB or idempotency-key anomalies was not run, so the 'no app-side defect' claim rests on full-population pattern counts rather than multi-level search.
- Blast radius beyond the order-service → exchange-gateway → exchange-api path was not fully mapped; whether unrelated services show elevated errors at 11:00Z is unknown.
- No external corroboration of the partner outage: search_historical_incidents is explicitly leads-not-evidence and HIST-103 is dated 2025-10-21, with no 2026-03-17-dated external record in any returned result (F28).
- Tool-call budget was exhausted before the final planned searches, so no exhaustive sweep of gateway/envoy/mesh 504 precedents beyond HIST-116 was completed.

## What would change this conclusion
- Direct evidence that exchange-gateway's outbound calls to api.exchange-partner.example were succeeding normally at 11:00–11:05Z (e.g. partner-side logs or a synthetic probe showing healthy 200s) would refute H1 and shift weight to a local gateway/host fault.
- Discovery of a mesh/DNS/egress fault or a change covering those sources with an ~11:00Z onset would promote H4: F30 establishes that the tools cannot exclude this today.
- A shared-component signal with the same 11:00Z onset affecting market-data-service or the shared datastores/cache (ledger-db, kafka, redis-cache) would move H4 up, since F19/F20 currently show market-data-service flat and no datastore telemetry was gathered (F24).
- Evidence that a specific order-service v2.15.2 code path fails only on outbound exchange-gateway calls (e.g. an app-side 5xx or idempotency-key error pattern in logs, or a staged replay of the release reproducing the failure without the partner fault) would revive H2 — though this is contradicted by F14 (post-rollback persistence) and F22 (no app-side errors).
- A confirmed exchange-partner incident report for 2026-03-17 with onset ~11:00Z would raise confidence in H1 above the current cap.
- Discovery that the exchange-api 503s/timeouts correlate with a specific exchange-gateway-side condition (deployment, config, or connection-pool exhaustion at the gateway) rather than a host-level partner fault would indicate the fault is on our side of the boundary, shifting the root cause to exchange-gateway itself.

## Integrity warnings (set by code)
- fact demoted to assumption: Compute is flat on both affected services across onset — order-service cpu_pct ~

## Investigation stats
- latency 178.13s, LLM calls 22, tokens 167907 (in 135708 / out 32199)
- tool calls 40 (4 failed, 0 partial, 0 recovered by retry), findings 29/30 verified
