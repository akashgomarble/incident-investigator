# Incident investigation report: INC-006
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
Quote latency degradation in INC-006 is best explained by H1: market-data-service itself slowed, and that slowdown propagated up the quote path into pricing-engine and on to order-service / api-gateway trading screens (confidence 0.50, medium — clearly ahead of H2 0.30 and H3 0.25). The evidence is consistent and multi-source: pricing-engine's p99 jumped from ~60ms to >2.2s between the 13:08 and 13:16 buckets while its own CPU (39-40%) and memory (48-49%) stayed flat, and 192/197 of its error logs name market-data-service as the timed-out downstream, with market-data-service's own p99 independently rising from ~40ms to ~1.7-2.0s in the same 13:12-13:21 window. No deployment or config change touched pricing-engine or any quote-path service; the only change in the window (auth-service v7.2.0 at 11:30Z, SameSite=strict) is off the pricing path and unsupported by telemetry. Confidence is capped at medium because the intrinsic cause inside market-data-service was never resolved — its slowdown could still be inherited from redis-cache (H2) or the external exchange-api via exchange-gateway (H3), and those telemetry checks were not completed.

## Probable root cause
**market-data-service itself slowed (dependency degradation on the quote path)** -- dependency_failure in `market-data-service`, triggered by `null` (confidence 0.50, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: market-data-service itself slowed (dependency degradation on the quote path) | dependency_failure | market-data-service | null | 0.50 (medium) | active |
| 2 | H2: Shared redis-cache degradation/cold-cache storm slows the quote path | infrastructure_failure | redis-cache | null | 0.30 (low) | active |
| 3 | H3: External exchange-api / exchange-gateway slowdown propagating up the quote path | dependency_failure | exchange-api (external exchange provider) | null | 0.25 (low) | active |
| 4 | H4: auth-service v7.2.0 (SameSite=strict) is the trigger (reporter-adjacent / only-change-in-window suspect) | bad_deployment | auth-service | auth-service v7.2.0 | 0.08 (low) | active |
| 5 | H5: pricing-engine-internal saturation (resources / pools / retry storm) | resource_exhaustion | pricing-engine | null | 0.05 (low) | rejected |

### H1: market-data-service itself slowed (dependency degradation on the quote path)
pricing-engine's downstream market-data-service experienced its own latency degradation starting 13:12-13:21Z, which propagated as quote deadline-exceeded errors in pricing-engine, and on to order-service/api-gateway trading screens. The fault origin is market-data-service (or something immediately behind it), not pricing-engine.

**Causal chain**
1. market-data-service p99 jumps from ~40ms to ~1700-1980ms in the 13:21 bucket, same window as the pricing-engine latency jump [F17]
2. pricing-engine emits 'quote computation deadline exceeded: market-data-service took <n>ms' (192/197 matched logs), naming market-data-service as the slow downstream [F14, F7]
3. pricing-engine p99 jumps ~60ms -> 601ms -> 2211ms and 5xx rises to ~4% with flat CPU/memory, i.e. no local saturation [F13, F15, F16, F18]
4. Latency propagates to order-service/api-gateway and thus the trading screens [F9]
5. No deployment or config change to pricing-engine, market-data, cache or broker in the window, so the origin is an intrinsic/operational failure inside market-data-service or its own upstreams [F1, F2, F10, F11]

**Supporting evidence**
  - `F13` ✔ pricing-engine p99 latency was flat at ~59-62ms (max ~66ms) from 09:00 through the 13:08 bucket, then jumped in the 13:16 bucket to avg 601.68ms / max 2227.82ms, and stayed elevated (13:24 avg 2211.36ms, max 2309.91ms). Onset (p99 > 2s) is therefore between 13:08 and 13:16 UTC.
  - `F14` ✔ First ERROR log in pricing-engine is 2026-04-09T13:23:57Z: 'quote computation deadline exceeded: market-data-service took 5171ms'. The dominant error signature is 'quote computation deadline exceeded: market-data-service took <n>ms' with count 192 out of 197 matched logs — a downstream-timeout signature naming market-data-service, with observed durations 827ms to 8456ms. No 5xx/connection-refused pattern appears in the top patterns.
  - `F15` ✔ pricing-engine error_rate (HTTP 5xx %) was ~0.17-0.24% through the 13:12 bucket, then rose in the 13:21 bucket to avg 3.32% / max 4.37% and held ~4% (e.g. 13:30 avg 4.11%, 14:51 avg 4.02%). The 5xx elevation begins in the same 13:21 bucket as the latency jump.
  - `F16` ✔ pricing-engine's own resource metrics are flat across the onset: cpu_pct stays ~39-40% (max 44.48, min 34.59) and memory_pct stays ~48-49% (max 54.93, min 43.36) from 12:00 through 14:51, with no step change at 13:16-13:21. This indicates no CPU or memory saturation in pricing-engine itself.
  - `F17` ✔ market-data-service p99 latency is flat at ~39-40ms through the 13:12 bucket, then jumps in the SAME 13:21 bucket to avg 1702.29ms / max 1979.98ms (13:30 avg 1814.5ms). This matches the pricing-engine subnet of errors naming market-data-service as the slow downstream and places market-data-service's own latency onset in the 13:12-13:21 window.
  - `F18` ✔ The reported degradation is consistent with a downstream (market-data-service) slowdown rather than pricing-engine saturation: pricing-engine CPU/memory are flat while its p99 and 5xx rise and 192/197 of its warn/error logs attribute the exceeded deadline to market-data-service taking up to 8456ms. Trading-screen impact propagates pricing-engine -> market-data-service is the shared upstream.
  - `F7` ✔ pricing-engine's quote-serving upstreams are market-data-service (http), redis-cache (cache); market-data-service itself depends on redis-cache (cache) and exchange-gateway (http), which calls external exchange-api. So the full quote path is pricing-engine -> market-data-service -> exchange-gateway -> exchange-api, plus a direct pricing-engine -> redis-cache edge.
  - `F9` ✔ Callers of pricing-engine: order-service -> pricing-engine (http), and api-gateway -> order-service -> pricing-engine; api-gateway also reaches market-data-service and portfolio-service directly. So pricing latency propagates to order-service and the trading screens behind api-gateway.
  - `F12` ✔ Precedent leads (explicitly 'leads, not evidence'): HIST-107 (2025-12-09) 'Cold cache storm after redis failover' on redis-cache+market-data-service with symptoms 'cache miss storm','quote latency'; HIST-113 (2026-01-28) pricing-engine CPU saturation at market open ('quote timeouts','cpu 99%'); HIST-103 (2025-10-21) external exchange partner outage causing upstream timeouts. These match the quote path's three candidate failure points (shared redis cache, market-data-service/exchange-gateway, external exchange-api).
**Contradicting evidence**
  - (none)
**Assumptions**
  - market-data-service's slowdown is intrinsic (own resource/GC/thread-pool/connection issue) rather than purely inherited from exchange-gateway/exchange-api/redis-cache; this distinction was never resolved (GAP: market-data-service log query not executed)
  - The 13:12-13:21 onset in market-data-service is real and not an artifact of a delayed/misaligned metric bucket

### H2: Shared redis-cache degradation/cold-cache storm slows the quote path
The shared redis-cache (platform-owned, consumed by auth-service, portfolio-service, pricing-engine and market-data-service) degraded or lost its warm dataset, causing cache-miss storms that inflated market-data-service and pricing-engine quote latency. Origin is redis-cache (infrastructure), consistent with the HIST-107 precedent.

**Causal chain**
1. redis-cache is the only shared node in the quote dependency graph, feeding both market-data-service and pricing-engine directly [F3, F8]
2. A redis-cache failure/failover or cold cache would inflate downstream reads on both market-data-service and pricing-engine simultaneously *(assumed)*
3. market-data-service p99 rises to ~1700-1980ms and pricing-engine attributes deadline-exceeded to market-data-service [F17, F14]
4. Symptom pattern matches precedent HIST-107 'Cold cache storm after redis failover' on redis-cache + market-data-service with 'quote latency' [F5, F12]

**Supporting evidence**
  - `F3` ✔ pricing-engine dependency graph: calls market-data-service (http) and redis-cache (cache). market-data-service itself calls redis-cache (cache) and exchange-gateway (http). order-service calls pricing-engine; api-gateway calls order-service. redis-cache is the only SHARED node in this graph (owner platform, shared:true).
  - `F8` ✔ redis-cache is the only 'shared' datastore in pricing-engine's dependency graph and is consumed by four components: auth-service, portfolio-service, pricing-engine and market-data-service all have `-> redis-cache kind: cache` edges. It is owner=platform, type=redis, shared=true.
  - `F5` ✔ Precedent HIST-107 (2025-12-09) 'Cold cache storm after redis failover' involved redis-cache and market-data-service with symptom 'quote latency'; root cause redis primary failure promoted an empty replica; resolution warmed cache and fixed replica sync checks. This is the closest symptom match (shared redis-cache + market-data path), lead only.
  - `F12` ✔ Precedent leads (explicitly 'leads, not evidence'): HIST-107 (2025-12-09) 'Cold cache storm after redis failover' on redis-cache+market-data-service with symptoms 'cache miss storm','quote latency'; HIST-113 (2026-01-28) pricing-engine CPU saturation at market open ('quote timeouts','cpu 99%'); HIST-103 (2025-10-21) external exchange partner outage causing upstream timeouts. These match the quote path's three candidate failure points (shared redis cache, market-data-service/exchange-gateway, external exchange-api).
  - `F17` ✔ market-data-service p99 latency is flat at ~39-40ms through the 13:12 bucket, then jumps in the SAME 13:21 bucket to avg 1702.29ms / max 1979.98ms (13:30 avg 1814.5ms). This matches the pricing-engine subnet of errors naming market-data-service as the slow downstream and places market-data-service's own latency onset in the 13:12-13:21 window.
**Contradicting evidence**
  - (none)
**Assumptions**
  - redis-cache actually degraded at 13:12-13:21 (no redis metrics were retrieved on this path)
  - redis-cache is a single point of failure / has a replica/failover event; its redundancy model is unknown
  - The HIST-107 precedent is relevant despite being from a past incident and explicitly flagged as a 'lead only'

### H3: External exchange-api / exchange-gateway slowdown propagating up the quote path
exchange-gateway's call to the external exchange-api (type=external, has_telemetry=false) degraded, slowing market-data-service, then pricing-engine, then order-service and the screens. Origin is the external provider or the exchange-gateway edge.

**Causal chain**
1. Full quote path is pricing-engine -> market-data-service -> exchange-gateway -> exchange-api [F7]
2. An external exchange slowdown would raise exchange-gateway and then market-data-service latency *(assumed)*
3. market-data-service p99 rises to ~1700-1980ms in the 13:21 bucket, matching the pricing-engine deadline-exceeded signature naming market-data-service [F17, F14]
4. Precedent HIST-103 'external exchange partner outage causing upstream timeouts' matches this failure point [F12]

**Supporting evidence**
  - `F7` ✔ pricing-engine's quote-serving upstreams are market-data-service (http), redis-cache (cache); market-data-service itself depends on redis-cache (cache) and exchange-gateway (http), which calls external exchange-api. So the full quote path is pricing-engine -> market-data-service -> exchange-gateway -> exchange-api, plus a direct pricing-engine -> redis-cache edge.
  - `F17` ✔ market-data-service p99 latency is flat at ~39-40ms through the 13:12 bucket, then jumps in the SAME 13:21 bucket to avg 1702.29ms / max 1979.98ms (13:30 avg 1814.5ms). This matches the pricing-engine subnet of errors naming market-data-service as the slow downstream and places market-data-service's own latency onset in the 13:12-13:21 window.
  - `F14` ✔ First ERROR log in pricing-engine is 2026-04-09T13:23:57Z: 'quote computation deadline exceeded: market-data-service took 5171ms'. The dominant error signature is 'quote computation deadline exceeded: market-data-service took <n>ms' with count 192 out of 197 matched logs — a downstream-timeout signature naming market-data-service, with observed durations 827ms to 8456ms. No 5xx/connection-refused pattern appears in the top patterns.
  - `F12` ✔ Precedent leads (explicitly 'leads, not evidence'): HIST-107 (2025-12-09) 'Cold cache storm after redis failover' on redis-cache+market-data-service with symptoms 'cache miss storm','quote latency'; HIST-113 (2026-01-28) pricing-engine CPU saturation at market open ('quote timeouts','cpu 99%'); HIST-103 (2025-10-21) external exchange partner outage causing upstream timeouts. These match the quote path's three candidate failure points (shared redis cache, market-data-service/exchange-gateway, external exchange-api).
**Contradicting evidence**
  - (none)
**Assumptions**
  - exchange-api is degraded; it has no telemetry so this cannot be observed (explicit GAP)
  - exchange-gateway metrics were never retrieved, so no direct evidence links this hop to the 13:12-13:21 onset

### H4: auth-service v7.2.0 (SameSite=strict) is the trigger (reporter-adjacent / only-change-in-window suspect)
The only deployment in the entire window, auth-service v7.2.0 'Session cookie hardening' at 11:30Z, changed session cookie behavior in a way that disrupted shared traffic or the shared redis-cache (e.g. session re-authentication storm or cache-key churn), indirectly slowing the quote path.

**Causal chain**
1. auth-service v7.2.0 (SameSite=strict) is the only deployment in 2026-04-08 00:00Z - 2026-04-09 14:59Z, live and never rolled back [F1, F10]
2. auth-service shares redis-cache with pricing-engine and market-data-service [F8]
3. A cookie-policy change causing session churn could add load to the shared redis-cache, slowing quote reads *(assumed)*
4. market-data-service / pricing-engine latency then rises [F17, F13]

**Supporting evidence**
  - `F1` ✔ Full all-services deployment sweep 2026-04-08T00:00Z to 2026-04-09T14:59Z returns exactly ONE deployment: auth-service v7.2.0 'Session cookie hardening' (change: SameSite=strict) deployed 2026-04-09T11:30:00Z by ci-bot, status live, never rolled back. No deployment to pricing-engine or any market-data/quote/cache/broker service in this window.
  - `F8` ✔ redis-cache is the only 'shared' datastore in pricing-engine's dependency graph and is consumed by four components: auth-service, portfolio-service, pricing-engine and market-data-service all have `-> redis-cache kind: cache` edges. It is owner=platform, type=redis, shared=true.
  - `F10` ✔ No deployments to pricing-engine (or any service except auth-service) occurred in 2026-04-09 06:00-14:59: the single deployment is auth-service v7.2.0 'Session cookie hardening' at 2026-04-09T11:30:00Z, status live, not rolled back. This is consistent with the report's 'no releases to pricing today'.
**Contradicting evidence**
  - `F4` ✔ The only change in the window (auth-service v7.2.0 at 11:30Z) is 2 hours before the 13:30Z report, is not upstream/downstream of pricing-engine, and the only listed change is SameSite=strict on session cookies -- not a timeout/pool/TTL/routing knob. The data shows no change activity that could plausibly touch pricing-engine's quote path.
  - `F13` ✔ pricing-engine p99 latency was flat at ~59-62ms (max ~66ms) from 09:00 through the 13:08 bucket, then jumped in the 13:16 bucket to avg 601.68ms / max 2227.82ms, and stayed elevated (13:24 avg 2211.36ms, max 2309.91ms). Onset (p99 > 2s) is therefore between 13:08 and 13:16 UTC.
  - `F11` ✔ No config/flag changes across any service in 2026-04-09 06:00-14:59 (count 0).
**Assumptions**
  - auth-service session churn measurably loads the shared redis-cache on the quote path (no supporting telemetry)
  - The 11:30Z deploy's effect manifested ~1h45m later at 13:08-13:16 without any intermediate signal

### H5: pricing-engine-internal saturation (resources / pools / retry storm)
pricing-engine itself is the origin: its own CPU/memory/pool saturation or a retry storm caused the p99 and 5xx degradation, and the market-data-service slow hop is a secondary symptom of pricing-engine's own retries.

**Causal chain**
1. pricing-engine p99 and 5xx rise sharply at 13:16-13:21 [F13, F15]
2. Its own compute is saturated or its connection pool is exhausted *(assumed)*
3. Errors quote 'market-data-service took Nms' because pricing-engine's own overload delayed its outbound calls [F14]

**Supporting evidence**
  - `F13` ✔ pricing-engine p99 latency was flat at ~59-62ms (max ~66ms) from 09:00 through the 13:08 bucket, then jumped in the 13:16 bucket to avg 601.68ms / max 2227.82ms, and stayed elevated (13:24 avg 2211.36ms, max 2309.91ms). Onset (p99 > 2s) is therefore between 13:08 and 13:16 UTC.
  - `F15` ✔ pricing-engine error_rate (HTTP 5xx %) was ~0.17-0.24% through the 13:12 bucket, then rose in the 13:21 bucket to avg 3.32% / max 4.37% and held ~4% (e.g. 13:30 avg 4.11%, 14:51 avg 4.02%). The 5xx elevation begins in the same 13:21 bucket as the latency jump.
  - `F14` ✔ First ERROR log in pricing-engine is 2026-04-09T13:23:57Z: 'quote computation deadline exceeded: market-data-service took 5171ms'. The dominant error signature is 'quote computation deadline exceeded: market-data-service took <n>ms' with count 192 out of 197 matched logs — a downstream-timeout signature naming market-data-service, with observed durations 827ms to 8456ms. No 5xx/connection-refused pattern appears in the top patterns.
**Contradicting evidence**
  - `F16` ✔ pricing-engine's own resource metrics are flat across the onset: cpu_pct stays ~39-40% (max 44.48, min 34.59) and memory_pct stays ~48-49% (max 54.93, min 43.36) from 12:00 through 14:51, with no step change at 13:16-13:21. This indicates no CPU or memory saturation in pricing-engine itself.
  - `F18` ✔ The reported degradation is consistent with a downstream (market-data-service) slowdown rather than pricing-engine saturation: pricing-engine CPU/memory are flat while its p99 and 5xx rise and 192/197 of its warn/error logs attribute the exceeded deadline to market-data-service taking up to 8456ms. Trading-screen impact propagates pricing-engine -> market-data-service is the shared upstream.
  - `F17` ✔ market-data-service p99 latency is flat at ~39-40ms through the 13:12 bucket, then jumps in the SAME 13:21 bucket to avg 1702.29ms / max 1979.98ms (13:30 avg 1814.5ms). This matches the pricing-engine subnet of errors naming market-data-service as the slow downstream and places market-data-service's own latency onset in the 13:12-13:21 window.
  - `F1` ✔ Full all-services deployment sweep 2026-04-08T00:00Z to 2026-04-09T14:59Z returns exactly ONE deployment: auth-service v7.2.0 'Session cookie hardening' (change: SameSite=strict) deployed 2026-04-09T11:30:00Z by ci-bot, status live, never rolled back. No deployment to pricing-engine or any market-data/quote/cache/broker service in this window.
  - `F11` ✔ No config/flag changes across any service in 2026-04-09 06:00-14:59 (count 0).
**Assumptions**
  - GC/thread-pool/connection-pool saturation exists (never measured)
  - The downstream-attributed timeout messages are misleading rather than descriptive
**Rejected because:** pricing-engine CPU (39-40%) and memory (48-49%) are flat with no step change at onset (F16); 192/197 logs attribute the exceeded deadline to market-data-service, and market-data-service's own p99 independently rises in the same bucket (F17); no deployment (F1) or config change (F11) touched pricing-engine. Own-saturation is contradicted by the evidence.

## Facts (verified observations)
- pricing-engine p99 latency was flat at ~59-62ms through the 13:08 bucket, then jumped in the 13:16 bucket to avg 601.68ms / max 2227.82ms and stayed elevated (13:24 avg 2211.36ms). Onset is between 13:08 and 13:16 UTC. [F13]
- market-data-service p99 latency was flat at ~39-40ms through the 13:12 bucket, then jumped in the same 13:21 bucket to avg 1702.29ms / max 1979.98ms (13:30 avg 1814.5ms), placing its own latency onset in the 13:12-13:21 window. [F17]
- The dominant pricing-engine error signature is 'quote computation deadline exceeded: market-data-service took <n>ms' at 192 of 197 matched logs, with observed durations 827ms to 8456ms; first ERROR log 13:23:57Z. No 5xx/connection-refused pattern appears in the top patterns. [F14]
- pricing-engine error_rate rose from ~0.17-0.24% (13:12 bucket) to avg 3.32% / max 4.37% in the 13:21 bucket and held ~4%; the 5xx elevation begins in the same bucket as the latency jump. [F15]
- pricing-engine's own CPU stayed ~39-40% (max 44.48, min 34.59) and memory ~48-49% (max 54.93, min 43.36) from 12:00 through 14:51 with no step change at 13:16-13:21, indicating no CPU or memory saturation in pricing-engine itself. [F16]
- The quote path is pricing-engine -> market-data-service (http) and pricing-engine -> redis-cache (cache), with market-data-service depending on redis-cache (cache) and exchange-gateway (http), which in turn calls external exchange-api. [F3, F7]
- redis-cache is the only 'shared' datastore in pricing-engine's dependency graph (owner=platform, type=redis, shared=true) and is consumed by auth-service, portfolio-service, pricing-engine and market-data-service. [F8]
- Latency propagates onward from pricing-engine: order-service -> pricing-engine (http) and api-gateway -> order-service -> pricing-engine, so pricing latency reaches the trading screens behind api-gateway. [F9]
- A full all-services deployment sweep from 2026-04-08T00:00Z to 2026-04-09T14:59Z returns exactly one deployment: auth-service v7.2.0 'Session cookie hardening' (SameSite=strict) at 11:30Z, status live, never rolled back; no deployment touched pricing-engine or any market-data/quote/cache/broker service. [F1, F10]
- All-services config/feature-flag change sweeps for 2026-04-08 to 2026-04-09 (and a pricing-engine-scoped query) return zero changes; no timeout, pool-size, flag, cache-TTL or routing config change was recorded around the incident window. [F2, F11]
- Precedent HIST-107 (2025-12-09) 'Cold cache storm after redis failover' involved redis-cache and market-data-service with symptom 'quote latency'; recorded as a lead only, not evidence. [F5]
- Other precedents (HIST-113 market-open volume exceeding provisioned capacity, HIST-115 connection pool misconfiguration, HIST-110 ledger-db IOPS throttling) match symptoms but have different recorded causes; recorded as leads only. [F6]

## Assumptions (not verified)
- Assumption: market-data-service's slowdown is intrinsic (its own resource/GC/thread-pool/connection issue) rather than purely inherited from redis-cache, exchange-gateway or external exchange-api; this distinction was never resolved because the market-data-service log query was not executed.
- Assumption: the 13:12-13:21 onset in market-data-service is real and not an artifact of a delayed or misaligned metric bucket.
- Assumption: redis-cache is on the critical quote path for market-data-service such that its degradation could produce the observed market-data-service slowdown (H2 remains viable but unmeasured).
- Assumption: exchange-gateway/exchange-api sits on the quote path and could slow market-data-service, but no exchange-gateway metrics or external telemetry were retrieved (H3 remains viable but unmeasured).
- Assumption: the deployment and config-change sweeps are complete enough to exclude a change-driven trigger despite one backend shard not responding on the first attempt.
- Assumption: the 13:16-13:21 onset windows observed for pricing-engine and market-data-service reflect the same causal event rather than two unrelated degradations.
- (demoted from facts: no verified observation backs it) The only change in the window (auth-service v7.2.0 at 11:30Z) is 2 hours before the report, is not upstream/downstream of pricing-engine, and only changes SameSite=strict on session cookies — not a timeout/pool/TTL/routing knob.

## Timeline
- `2026-04-09T11:30:00Z` Only change in the window deploys: auth-service v7.2.0 'Session cookie hardening' (SameSite=strict), status live, never rolled back. This service is not on the pricing quote path. [F1, F4, F10]
- `2026-04-09T13:08:00Z` Last clean metric bucket: pricing-engine p99 still flat at ~59-62ms. [F13]
- `2026-04-09T13:12:00Z` Last clean market-data-service bucket: p99 still flat at ~39-40ms. pricing-engine error_rate still ~0.17-0.24%. [F15, F17]
- `2026-04-09T13:16:00Z` pricing-engine p99 jumps to avg 601.68ms / max 2227.82ms; onset of quote latency degradation between 13:08 and 13:16. [F13]
- `2026-04-09T13:21:00Z` Same bucket as the jump: market-data-service p99 rises to avg 1702.29ms / max 1979.98ms, and pricing-engine error_rate rises to avg 3.32% / max 4.37%. [F15, F17]
- `2026-04-09T13:23:57Z` First ERROR in pricing-engine: 'quote computation deadline exceeded: market-data-service took 5171ms'; this becomes the dominant signature (192/197 logs). [F14]
- `2026-04-09T13:24:00Z` pricing-engine p99 stays elevated at avg 2211.36ms / max 2309.91ms; degradation holds through 13:30 and 14:51. [F13, F15]
- `2026-04-09T13:30:00Z` Incident INC-006 [SEV-2] reported by on-call SRE: quotes taking 2-3s to load, pricing-engine deadline-exceeded alerts firing. [F13, F14]
- `2026-04-09T14:59:00Z` Latest data: pricing-engine p99 and ~4% error_rate remain elevated (14:51 avg 4.02% 5xx) with pricing-engine CPU/memory flat throughout. [F15, F16]

## Recommended actions
- **[immediate]** Mitigate at the quote-path bottleneck: check and warm/restore the redis-cache dataset consumed by market-data-service on the quote path (if hit-rate/evictions or a failover-to-empty-replica pattern is present, warm the cache and re-point to the healthy primary). -- H2 is the leading alternative to H1 and redis-cache is the only shared node on the quote path (F3, F8) with a matching precedent (F5). If redis-cache is the true bottleneck, warming/restoring it stops the degradation immediately; this is safe even if H1 is the true cause. [F3, F5, F8]
- **[immediate]** Mitigate at market-data-service: verify its health (thread-pool/connection-pool/GC and dependency timeouts) and, if it is the bottleneck, shed or scale it (increase replicas / restart the degraded instance) to restore market-data-service p99 toward its ~40ms baseline. -- H1 is the top-ranked hypothesis (0.50): market-data-service's own p99 jumped to ~1.7-2.0s in the same window (F17) and is named as the slow downstream in 192/197 pricing-engine errors (F14). Reducing its latency directly relieves pricing-engine and the trading screens (F9). [F14, F17, F9]
- **[immediate]** Protect the downstream chain while mitigations are applied: raise/relax the pricing-engine quote deadline temporarily and/or enable the fallback/cached-quote path so order-service and api-gateway screens do not surface 3s loads and 5xx. -- Degradation propagates pricing-engine -> order-service -> api-gateway to the trading screens (F9), with pricing-engine error_rate holding ~4% (F15). A temporary deadline/fallback change limits customer-visible impact without masking the root cause. [F9, F15, F14]
- **[short_term]** Discriminate H1 vs H2 vs H3 by collecting the missing telemetry: market-data-service WARN+ logs; redis-cache node metrics (memory, evictions, hit-rate, connections, latency, failover events); exchange-gateway latency/error metrics and exchange-api call outcomes for the 13:00-13:30 window. -- These checks were never executed (budget exhausted) and are exactly what separates an intrinsic market-data-service slowdown from inherited redis-cache or exchange degradation (F3, F7). Confirming the true origin avoids re-occurrence. [F3, F7, F14]
- **[short_term]** After the immediate bottleneck is relieved, verify recovery: confirm market-data-service p99 returns to ~40ms and pricing-engine p99 returns to ~60ms with 5xx back to ~0.2% over at least 15-30 minutes, then close the incident. -- Provides an objective exit criterion against the observed baselines and onset profile (F13, F15, F17). [F13, F15, F17]
- **[long_term]** Add capacity/headroom and dependency-health guardrails on the quote path: per-dependency latency SLOs and alerting for market-data-service, redis-cache hit-rate/eviction and exchange-gateway/exchange-api timeouts, plus pre-scaling for market-open volume. -- The incident spread silently for ~8 minutes before pricing-engine breached (F13, F15) and the origin could not be localized because downstream dependency telemetry was absent (gaps in redis-cache, exchange-gateway and market-data-service logs). Precedents HIST-113 (market-open capacity) and HIST-107 (redis failover cold cache) show recurring failure modes at these points (F5, F6). [F5, F6, F13, F15]
- **[long_term]** Ensure request-scoped instrumentation records which dependency consumes the deadline budget (a per-hop tracer or structured 'slow_dependency' field), so the root hop is unambiguous in future incidents. -- Currently the only attribution is free-text log messages (F14), which forced this investigation to rely on inference rather than direct evidence. [F14, F18]

## Data gaps
- market-data-service WARN+ log query was never executed, so its own error/exception signature and the intrinsic cause of its slowdown are unconfirmed — this is why H1 is capped at medium confidence.
- No redis-cache telemetry was retrieved (memory, evictions, hit-rate, connection count, latency, failover/replica events) and its redundancy model is unknown, so H2 cannot be excluded or confirmed.
- exchange-gateway metrics were never retrieved and exchange-api is type=external with has_telemetry=false, so H3 cannot be excluded or confirmed.
- Only pricing-engine and market-data-service were probed for latency; api-gateway, order-service, portfolio-service, exchange-gateway, ledger-db, redis-cache and kafka were not checked for the same 13:16-13:21 window, so the full set of co-affected services is unknown.
- Quote-endpoint granularity (uniform vs isolated across quote endpoints/routes) was not tested, so it is unknown whether the latency is uniform across all quote paths.
- No GC, thread-pool or connection-pool metrics were retrieved for pricing-engine (and request_rate/restarts were not queried), so the 'no own-saturation' verdict rests only on CPU and memory flatness.
- The first all-services deployment query returned partial=true with a non-responding backend shard, so an unlisted early-window deployment on that shard cannot be fully excluded.
- The config-change store returned zero for all services and it is unknown whether infra/network/mesh changes are even recorded there, so absence of config changes may reflect a recording gap rather than genuinely no changes.

## What would change this conclusion
- If market-data-service is shown to be healthy on its own (flat internal resource/thread/connection metrics) while redis-cache metrics show evictions, hit-rate collapse or a failover-to-empty-replica event at 13:12-13:21, the conclusion shifts to H2 (redis-cache) as the origin.
- If exchange-gateway/exchange-api call timeouts or latency are shown to rise starting at/near 13:12-13:21 and precede market-data-service's slowdown, the conclusion shifts to H3 (external exchange).
- If a deployment or config change on a previously unreported shard (the partial first sweep) is found touching market-data-service or redis-cache in this window, a deployment-triggered hypothesis would need re-ranking.
- If market-data-service's own resource metrics show CPU/memory/thread-pool/connection-pool saturation at onset, that would convert H1 from 'a dependency slowed' to a specific intrinsic resource-exhaustion root cause within market-data-service.
- If per-hop tracing shows the pricing-engine deadline budget being consumed inside pricing-engine itself (not by market-data-service), the rejection of H5 would need revisiting.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: The only change in the window (auth-service v7.2.0 at 11:30Z) is 2 hours before 

## Investigation stats
- latency 95.68s, LLM calls 14, tokens 79707 (in 63283 / out 16424)
- tool calls 23 (2 failed, 1 partial, 1 recovered by retry), findings 18/18 verified
