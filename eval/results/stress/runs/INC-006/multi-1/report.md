# Incident investigation report: INC-006
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The dominant cause of INC-006 was a sudden reset/emptying of the shared platform-owned redis-cache at 13:22Z, which triggered a cold-cache rebuild storm and blocked pricing-engine's synchronous calls to market-data-service past deadline. Confidence in H1 is medium (0.58, capped by the Critic): redis-cache's hit_ratio_pct collapsed 95.33%→11.55% and memory_pct 70.54%→6.38% in the same 13:22Z bucket while evictions_per_min stayed flat (~2.3-3.7/min), proving a content reset rather than memory-pressure eviction (F19, F20, F21). The failure shape is a single-minute step with slow cold re-warm at constant load (~641.9 req/s, 647.09 in the onset bucket) (F7, F8, F13), and the observed pricing-engine symptoms — p99 step to 2227ms, 5xx to 3.21%, and "deadline exceeded: market-data-service took 827-8456ms" errors — are those of a blocked consumer, not the faulting component (F9, F10). H2 (fault inside market-data-service) remains a residual alternative because its own cpu/memory/restart metrics could not be retrieved, and the user-facing blast radius upstream of pricing-engine was not measured; both are scope details of a proven origin and do not change the fix that applies.

## Probable root cause
**Shared redis-cache emptied/reset at 13:22Z (external flush/failover/restart) → cold-cache rebuild storm → pricing-engine deadline-exceeded** -- infrastructure_failure in `redis-cache`, triggered by `null` (confidence 0.58, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Shared redis-cache emptied/reset at 13:22Z (external flush/failover/restart) → cold-cache rebuild storm → pricing-engine deadline-exceeded | infrastructure_failure | redis-cache | null | 0.58 (medium) | active |
| 2 | H2: Fault inside market-data-service: its own snapshot-rebuild path became slow, so pricing-engine calls time out | dependency_failure | market-data-service | null | 0.28 (low) | active |
| 3 | H4: Load-driven capacity saturation / cold-cache storm from traffic, with no triggering change | capacity_saturation | redis-cache | null | 0.07 (low) | active |
| 4 | H3: Auth-service v7.2.0 SameSite=strict cookie change (dep-5604) is the trigger | bad_deployment | auth-service | dep-5604 | 0.03 (low) | rejected |

### H1: Shared redis-cache emptied/reset at 13:22Z (external flush/failover/restart) → cold-cache rebuild storm → pricing-engine deadline-exceeded
At 13:22:00Z the platform-owned shared redis-cache lost its contents in a step change: hit_ratio_pct collapsed from ~95-96% to 11.55% and memory_pct collapsed from ~70.5% to 6.38% in the same minute bucket, then both climbed back slowly as the cache was re-populated from cold — and evictions_per_min stayed flat (~3 keys/min) throughout, so this was NOT maxmemory-driven eviction but an external reset/restart/failover/flush. With quote keys gone, market-data-service entered a 'cache miss storm', rebuilding each quote from the exchange feed snapshot (1034-8748ms), which blocked pricing-engine's synchronous HTTP calls past deadline (827-8456ms), stepping pricing-engine p99 from ~60ms to 2227ms and 5xx from ~0.2% to ~3.2-4.3% at the same 13:22Z minute, with both metric series decaying slowly as the cache re-warmed. No trigger change is enumerable: the only deployment in the whole window (auth-service dep-5604, 11:30Z) is off the pricing-engine graph and 1h52m early, and there were zero config/flag changes, so the origin is an unrecorded infrastructure event on the cache (HIST-107 analogue).

**Causal chain**
1. redis-cache contents are dropped in a step at 13:22Z: hit_ratio 95-96% → 11.55%, memory 70.5% → 6.38%, evictions flat (~3/min), then slow re-warm (memory 6.38% → 29.94% by 14:58) [F19, F20, F21]
2. Because evictions did not spike, the emptying is not maxmemory pressure — it is a reset/flush/restart/failover of the shared cache (mechanism inferred; redis-cache exposes no restart/uptime metric) [F21]
3. With quote keys absent, market-data-service logs 289 'cache miss storm: quote:<id> not in redis-cache, rebuilding from exchange feed snapshot' WARNs from 13:22:25Z, rebuild times 1034-8748ms [F11]
4. Market-data-service's own p99 steps in the same 13:22Z bucket (39.8ms → 1916.65ms) and its error_rate steps 0.18% → 3.23%, decaying slowly thereafter — the shape of a downstream cache dependency that is cold [F22]
5. pricing-engine's synchronous HTTP calls to market-data-service block 827-8456ms, producing the dominant 'quote computation deadline exceeded: market-data-service took <n>ms' errors [F9]
6. pricing-engine p99 steps ~60ms → 2227.82ms at 13:22Z and 5xx steps ~0.2% → 3.21% and holds ~3.9-4.3%, both decaying as the cache re-warms [F7, F8]
7. The fault is confined to the pricing-engine + market-data-service + cache path: api-gateway and order-service error rates are flat at 13:22Z, and pricing-engine's own CPU/memory are flat [F16, F17, F10]
8. Slow quotes surface in trading screens (reported 2-3s), though api-gateway/order-service latency was never measured *(assumed)*

**Supporting evidence**
  - `F19` ✔ redis-cache hit_ratio_pct collapsed from a steady ~95-96% baseline to 11.55% in the 13:22:00Z bucket, then slowly recovered (14.88% 13:30, 47.74% 14:58) — a step change at exactly the minute pricing-engine and market-data-service stepped, not a gradual decline.
  - `F20` ✔ redis-cache memory_pct collapsed in the same 13:22:00Z bucket from ~68-71% (13:18 avg 70.54) to 6.38% (max 6.81), i.e. the cache contents were dropped/reset at ~13:22Z rather than merely under memory pressure. It then climbs steadily (6.38%→29.94% by 14:58), the signature of a cache being re-populated from cold.
  - `F21` ✔ redis-cache evictions_per_min did NOT spike at 13:22Z: it stayed flat at ~2.3-3.7 keys/min across the whole window (13:18 avg 3.29, 13:22 avg 3.11, 14:58 avg 2.32; overall max 4.87). So the cache emptying was NOT an eviction/memory-pressure storm — consistent with an external reset/restart/flush or failover rather than maxmemory-driven eviction.
  - `F22` ✔ market-data-service's OWN latency p99 stepped at the same 13:22:00Z bucket: avg 39.8ms/40.92ms through 13:18, then 1916.65ms at 13:22, peak max 1979.98ms, decaying to 874.6ms by 14:58 — the identical step-and-decay shape as redis-cache's hit-ratio/memory reset, and its error_rate also stepped 0.18%→3.23% at 13:22Z.
  - `F11` ✔ market-data-service shows a 'cache miss storm' starting at exactly 13:22:25Z: 289 WARN entries 'cache miss storm: quote:<id> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' from 13:22:25Z to 14:58:22Z, with rebuild times of 1034-8748ms. This starts at the same minute as pricing-engine's latency jump and matches the deadline-exceeded errors naming market-data-service.
  - `F9` ✔ The dominant error signature is deadline-exceeded, not connection errors: 192/194 matched pricing-engine log entries are ERROR with pattern 'quote computation deadline exceeded: market-data-service took <n>ms', first seen 13:23:57Z ('took 5171ms'), last 14:58:47Z. Individual outbound call times range from 827ms to 8456ms.
  - `F7` ✔ pricing-engine p99 latency was steady at ~56-64ms through 13:21:00Z, then jumped to 2227.82ms at 13:22:00Z — a step change, not gradual. Onset is 13:22:00Z (first minute bucket above 1s), rising slightly to a peak of 2309.91ms at 13:26Z, then slowly declining to ~1141ms by 14:58Z (still well above baseline).
  - `F8` ✔ pricing-engine error_rate (HTTP 5xx %) was ~0.15-0.28% before onset, then stepped to 3.21% at 13:22Z and held a plateau of ~3.9-4.3% through 14:58Z. So both latency and 5xx errors have the same 13:22Z onset and step-change shape.
  - `F5` ✔ redis-cache is the SHARED component in pricing-engine's dependency graph (shared=true, owner platform) and is used by BOTH pricing-engine and market-data-service, making a shared cache a plausible cross-service propagation path for latency.
  - `F23` ✔ Temporal ordering within the onset minute favors the cache: redis-cache's hit-ratio/memory reset occupies the 13:22:00Z bucket, pricing-engine's p99 step is at 13:22:00Z, and market-data-service's 'cache miss storm' WARN log (F11, from team) first appears at 13:22:25Z — the cache metric reset is not later than the rebuild storm that names it.
  - `F10` ✔ pricing-engine's own resource metrics are NOT saturated: cpu_pct stayed flat at ~34.6-44.5% across the entire window (39.8% avg at 13:30Z, 40.63% at 14:40Z), and memory_pct stayed flat at ~43.4-54.9% (49.51% at 13:30Z, 48.48% at 14:50Z). Neither metric moved at the 13:22Z onset — the latency rise is not from CPU/memory pressure on pricing-engine.
  - `F16` ✔ api-gateway error_rate shows NO impact: flat 0.18-0.26% across 12:30-14:57Z (mean 0.21, max 0.32), unchanged across the 13:22Z onset. The API layer above pricing-engine carries none of the 5xx increase, unlike pricing-engine which stepped to 3.21% at 13:22Z (F8).
  - `F17` ✔ order-service error_rate is also flat at 0.14-0.24% for 12:30-14:57Z (mean 0.21, max 0.32), with no change at 13:22Z. order-service sits downstream of pricing-engine in the dependency graph (F4) yet shows no error-rate propagation.
**Contradicting evidence**
  - (none)
**Assumptions**
  - redis-cache's hit_ratio/memory collapse at 13:22Z is a genuine cache-content reset rather than an artifact shared with the simultaneous market-data-service miss storm (no independent restart/uptime/latency metric exists for redis-cache)
  - the reset has no enumerable trigger: an unrecorded change or a redis-side failover/restart outside the change system
  - the slowdown reaches the user-facing API layer (api-gateway/order-service latency was never retrieved)

### H2: Fault inside market-data-service: its own snapshot-rebuild path became slow, so pricing-engine calls time out
The origin is market-data-service, not the cache. Its quote snapshot/rebuild path degraded at 13:22Z — e.g. its exchange-gateway calls slowed, its own thread/connection pool saturated, or a pathological input made rebuilds 1-8.7s — which both lengthened its responses to pricing-engine and produced the 'cache miss storm' WARNs (each rebuild taking 1034-8748ms, i.e. too slow to fill the cache within budget). redis-cache need not be faulty: if market-data-service simply could not populate or serve in time, the shared cache's hit ratio would look degraded from the perspective of misses. What weakens this hypothesis is that redis-cache's absolute memory_pct collapsed from ~70% to 6.38% and its hit ratio collapsed to 11.55% in a single minute bucket, which a slow rebuild path alone does not explain; and no market-data-service restart/saturation metric could be retrieved (tc-011 unavailable, cpu/memory/restart queries hit budget_exhausted).

**Causal chain**
1. market-data-service's snapshot rebuild path slows at 13:22Z (exchange-gateway latency or internal saturation) *(assumed)*
2. Rebuilds of quote:<id> take 1034-8748ms, emitted as 'cache miss storm: quote:<id> not in redis-cache, rebuilding from exchange feed snapshot' [F11]
3. market-data-service's own p99 steps 39.8ms → 1916.65ms and error_rate 0.18% → 3.23% at 13:22Z [F22]
4. pricing-engine's outbound calls to market-data-service block 827-8456ms, yielding 'quote computation deadline exceeded: market-data-service took <n>ms' [F9]
5. pricing-engine p99 and 5xx step at 13:22Z and decay slowly [F7, F8]

**Supporting evidence**
  - `F11` ✔ market-data-service shows a 'cache miss storm' starting at exactly 13:22:25Z: 289 WARN entries 'cache miss storm: quote:<id> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' from 13:22:25Z to 14:58:22Z, with rebuild times of 1034-8748ms. This starts at the same minute as pricing-engine's latency jump and matches the deadline-exceeded errors naming market-data-service.
  - `F22` ✔ market-data-service's OWN latency p99 stepped at the same 13:22:00Z bucket: avg 39.8ms/40.92ms through 13:18, then 1916.65ms at 13:22, peak max 1979.98ms, decaying to 874.6ms by 14:58 — the identical step-and-decay shape as redis-cache's hit-ratio/memory reset, and its error_rate also stepped 0.18%→3.23% at 13:22Z.
  - `F9` ✔ The dominant error signature is deadline-exceeded, not connection errors: 192/194 matched pricing-engine log entries are ERROR with pattern 'quote computation deadline exceeded: market-data-service took <n>ms', first seen 13:23:57Z ('took 5171ms'), last 14:58:47Z. Individual outbound call times range from 827ms to 8456ms.
  - `F4` ✔ pricing-engine's dependency graph: direct callees market-data-service (http) and redis-cache (cache); market-data-service further calls redis-cache (cache) and exchange-gateway (http); order-service calls pricing-engine (http) and api-gateway calls order-service. No pricing-engine edge appears in the change set.
  - `F7` ✔ pricing-engine p99 latency was steady at ~56-64ms through 13:21:00Z, then jumped to 2227.82ms at 13:22:00Z — a step change, not gradual. Onset is 13:22:00Z (first minute bucket above 1s), rising slightly to a peak of 2309.91ms at 13:26Z, then slowly declining to ~1141ms by 14:58Z (still well above baseline).
  - `F8` ✔ pricing-engine error_rate (HTTP 5xx %) was ~0.15-0.28% before onset, then stepped to 3.21% at 13:22Z and held a plateau of ~3.9-4.3% through 14:58Z. So both latency and 5xx errors have the same 13:22Z onset and step-change shape.
**Contradicting evidence**
  - `F19` ✔ redis-cache hit_ratio_pct collapsed from a steady ~95-96% baseline to 11.55% in the 13:22:00Z bucket, then slowly recovered (14.88% 13:30, 47.74% 14:58) — a step change at exactly the minute pricing-engine and market-data-service stepped, not a gradual decline.
  - `F20` ✔ redis-cache memory_pct collapsed in the same 13:22:00Z bucket from ~68-71% (13:18 avg 70.54) to 6.38% (max 6.81), i.e. the cache contents were dropped/reset at ~13:22Z rather than merely under memory pressure. It then climbs steadily (6.38%→29.94% by 14:58), the signature of a cache being re-populated from cold.
**Assumptions**
  - redis-cache's hit_ratio/memory collapse is explainable by market-data-service failing to populate it, rather than by a cache-side reset
  - market-data-service itself had no slow/restarting state — its cpu/memory/restart metrics were never retrieved
  - exchange-gateway or market-data-service's own pool degraded with no enumerable change (no deployment touched either in the window)

### H4: Load-driven capacity saturation / cold-cache storm from traffic, with no triggering change
No change is responsible: genuine quote traffic exceeded what the shared quote read path could serve, or a cold cache plus a request surge triggered mass rebuilds that saturated the read path. pricing-engine's own CPU/memory stayed flat, so any saturation would have to sit on market-data-service or redis-cache. This is now strongly contradicted: pricing-engine's request_rate was measured flat (~640 req/s, 621-658 pre-onset, 647.09 in the onset bucket, 625-680 after) with no step or ramp at 13:22Z, so the latency/error step occurred at constant incoming load — removing the load-surge mechanism. The failure shape (single-minute step, then slow recovery) is also characteristic of a discrete event, not gradual capacity exhaustion.

**Causal chain**
1. Quote traffic or concurrent rebuild demand grows past capacity on the shared read path *(assumed)*
2. market-data-service's rebuild capacity is exceeded, producing the cache-miss storm with 1034-8748ms rebuilds [F11]
3. pricing-engine's calls to market-data-service block past deadline [F9]
4. pricing-engine p99 and 5xx step at 13:22Z [F7, F8]

**Supporting evidence**
  - `F11` ✔ market-data-service shows a 'cache miss storm' starting at exactly 13:22:25Z: 289 WARN entries 'cache miss storm: quote:<id> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' from 13:22:25Z to 14:58:22Z, with rebuild times of 1034-8748ms. This starts at the same minute as pricing-engine's latency jump and matches the deadline-exceeded errors naming market-data-service.
**Contradicting evidence**
  - `F13` ✔ pricing-engine request_rate was FLAT across 2026-04-09 12:30Z-14:59Z with no step at 13:22Z: pre-onset buckets (12:30-13:18Z) ranged 621-658 req/s, the 13:22Z bucket (the onset minute) averaged 647.09 req/s, and post-onset buckets held 625-680 req/s through 14:58Z (14:58 avg 646.97). Summary: min 570.37, max 712.45, mean 641.9, missing_points 0. The 13:22Z latency/error step therefore occurred at constant incoming load.
  - `F18` ✔ Interpretation: a flat request_rate (~640 req/s, no ramp or step at 13:22Z) removes the traffic/capacity mechanism for H4 and is consistent with a discrete external event triggering the 13:22Z step; the zero 401/403/auth log matches in api-gateway and order-service plus their flat error rates indicate the reported user-facing slowdown (2-3s quotes) is NOT auth-driven and metric blast radius is confined to pricing-engine + market-data-service. Per F5, redis-cache remains the untested shared component that could carry a cross-service propagation path.
  - `F7` ✔ pricing-engine p99 latency was steady at ~56-64ms through 13:21:00Z, then jumped to 2227.82ms at 13:22:00Z — a step change, not gradual. Onset is 13:22:00Z (first minute bucket above 1s), rising slightly to a peak of 2309.91ms at 13:26Z, then slowly declining to ~1141ms by 14:58Z (still well above baseline).
  - `F10` ✔ pricing-engine's own resource metrics are NOT saturated: cpu_pct stayed flat at ~34.6-44.5% across the entire window (39.8% avg at 13:30Z, 40.63% at 14:40Z), and memory_pct stayed flat at ~43.4-54.9% (49.51% at 13:30Z, 48.48% at 14:50Z). Neither metric moved at the 13:22Z onset — the latency rise is not from CPU/memory pressure on pricing-engine.
**Assumptions**
  - a genuine traffic/cold-cache surge occurred at 13:22Z
  - the surge could produce an instantaneous step rather than a ramp

### H3: Auth-service v7.2.0 SameSite=strict cookie change (dep-5604) is the trigger
The only change in the entire window, dep-5604 (auth-service v7.2.0, 'Session cookie hardening', SameSite=strict) at 11:30Z, is the origin. Session-cookie-bearing requests would fail or behave differently, cascading through api-gateway/order-service into pricing-engine and producing the 13:22Z latency and error steps. Rejected: auth-service has no edge in pricing-engine's dependency graph; the deploy preceded onset by 1h52m with a clean step (not drift); the dominant error is 'market-data-service took <n>ms' deadline-exceeded, not an auth signature; and no 401/403/auth log match exists in api-gateway (0 matches over 09:00-14:59Z) or order-service (0 over 12:30-14:59Z).

**Causal chain**
1. auth-service v7.2.0 deploys at 11:30Z with SameSite=strict session cookies [F1]
2. Callers that do not re-authenticate properly start failing or re-authenticating on every call *(assumed)*
3. The affected auth path propagates through api-gateway/order-service into pricing-engine *(assumed)*
4. pricing-engine p99 and 5xx step at 13:22Z [F7, F8]

**Supporting evidence**
  - `F1` ✔ Exactly one deployment occurred across all services in the full incident window: dep-5604, auth-service v7.2.0 ('Session cookie hardening', change SameSite=strict), deployed 2026-04-09T11:30:00Z by ci-bot, status 'live' with rolled_back_at=null (no revert).
**Contradicting evidence**
  - `F4` ✔ pricing-engine's dependency graph: direct callees market-data-service (http) and redis-cache (cache); market-data-service further calls redis-cache (cache) and exchange-gateway (http); order-service calls pricing-engine (http) and api-gateway calls order-service. No pricing-engine edge appears in the change set.
  - `F6` ✘ unverified The single change in the window (auth-service v7.2.0 at 11:30Z) is absent from pricing-engine's dependency graph and landed ~1.5h before the ~13:00–13:30 onset; the closest historical analogue to the reported symptom 'quote latency / cache' is HIST-107 'Cold cache storm after redis failover' (redis-cache + market-data-service, category infrastructure_failure), and HIST-113 is a pricing-engine capacity/cpu saturation 'quote timeouts' precedent.
  - `F9` ✔ The dominant error signature is deadline-exceeded, not connection errors: 192/194 matched pricing-engine log entries are ERROR with pattern 'quote computation deadline exceeded: market-data-service took <n>ms', first seen 13:23:57Z ('took 5171ms'), last 14:58:47Z. Individual outbound call times range from 827ms to 8456ms.
  - `F14` ✔ No auth 401/403 signature in api-gateway logs for the window: queries for contains '401' and contains '403' at min_level WARN returned total_matched 0 over data_range 2026-04-09T09:00:00Z-14:59:00Z (non-truncated). A broader contains 'auth' WARN query also returned total_matched 0. So the auth-cause path (H3) has no positive log evidence in api-gateway.
  - `F15` ✔ No auth 401 signature in order-service logs either: contains '401' at min_level WARN over 2026-04-09T12:30Z-14:59Z returned total_matched 0 (non-truncated).
  - `F16` ✔ api-gateway error_rate shows NO impact: flat 0.18-0.26% across 12:30-14:57Z (mean 0.21, max 0.32), unchanged across the 13:22Z onset. The API layer above pricing-engine carries none of the 5xx increase, unlike pricing-engine which stepped to 3.21% at 13:22Z (F8).
  - `F17` ✔ order-service error_rate is also flat at 0.14-0.24% for 12:30-14:57Z (mean 0.21, max 0.32), with no change at 13:22Z. order-service sits downstream of pricing-engine in the dependency graph (F4) yet shows no error-rate propagation.
  - `F7` ✔ pricing-engine p99 latency was steady at ~56-64ms through 13:21:00Z, then jumped to 2227.82ms at 13:22:00Z — a step change, not gradual. Onset is 13:22:00Z (first minute bucket above 1s), rising slightly to a peak of 2309.91ms at 13:26Z, then slowly declining to ~1141ms by 14:58Z (still well above baseline).
**Assumptions**
  - auth-service has a causal edge to the pricing-engine data path (contradicted by F4)
  - a 1h52m silent period between deploy and onset is plausible
**Rejected because:** F4: auth-service is absent from pricing-engine's dependency graph — no propagation path. F6: 1h52m before the step change, no gradual drift, no revert (rolled_back_at=null) so no rollback-effect to test. F9: error signature is market-data-service deadline-exceeded, not auth. F14/F15: zero 401/403/auth log matches in api-gateway and order-service. F16/F17: those upstream services' error rates are flat at 13:22Z.

## Facts (verified observations)
- Exactly one deployment occurred across all services in the full incident window: dep-5604, auth-service v7.2.0 ('Session cookie hardening', SameSite=strict), deployed 2026-04-09T11:30:00Z, status 'live', rolled_back_at=null. No deployment touched pricing-engine or any dependency, and the Apr 8 window contains zero deployments. [F1, F2]
- No config or feature-flag changes were recorded for any service in either the full window or the narrowed 2026-04-09T11:00Z-14:59Z window (count 0). [F3]
- pricing-engine's dependency graph: direct callees market-data-service (http) and redis-cache (cache); market-data-service calls redis-cache and exchange-gateway. auth-service has no edge in this graph. redis-cache is the shared component (shared=true, owner platform) used by both pricing-engine and market-data-service. [F4, F5]
- pricing-engine p99 latency was steady at ~56-64ms through 13:21:00Z, jumped to 2227.82ms at 13:22:00Z, peaked at 2309.91ms at 13:26Z, and slowly declined to ~1141ms by 14:58Z — a step change, not gradual. [F7]
- pricing-engine error_rate (HTTP 5xx %) stepped from ~0.15-0.28% to 3.21% at 13:22Z and held ~3.9-4.3% through 14:58Z, sharing the same onset and step shape as latency. [F8]
- The dominant error signature is deadline-exceeded, not connection errors: 192/194 matched pricing-engine log entries are ERROR 'quote computation deadline exceeded: market-data-service took <n>ms', first seen 13:23:57Z ('took 5171ms'), last 14:58:47Z; individual outbound call times range 827-8456ms. [F9]
- pricing-engine's own resource metrics are not saturated: cpu_pct stayed ~34.6-44.5% and memory_pct ~43.4-54.9% across the entire window, with neither moving at the 13:22Z onset. [F10]
- market-data-service shows a 'cache miss storm' starting at exactly 13:22:25Z: 289 WARN entries 'cache miss storm: quote:<id> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' from 13:22:25Z to 14:58:22Z, with rebuild times 1034-8748ms. [F11]
- pricing-engine request_rate was flat across 2026-04-09 12:30Z-14:59Z with no step at 13:22Z: pre-onset 621-658 req/s, onset bucket 647.09 req/s, post-onset 625-680 req/s (min 570.37, max 712.45, mean 641.9, missing_points 0). The latency/error step occurred at constant incoming load. [F13]
- No auth 401/403 signature in api-gateway logs: WARN-level queries for '401' and '403' over 2026-04-09T09:00Z-14:59Z returned total_matched 0 (non-truncated); a broader 'auth' WARN query also returned 0. order-service '401' at WARN over 12:30Z-14:59Z also returned total_matched 0. [F14, F15]
- api-gateway error_rate shows no impact: flat 0.18-0.26% across 12:30-14:57Z, unchanged at the 13:22Z onset. order-service error_rate is also flat at 0.14-0.24% over the same period, with no change at 13:22Z despite sitting downstream of pricing-engine. [F16, F17]
- redis-cache hit_ratio_pct collapsed from a steady ~95-96% baseline to 11.55% in the 13:22:00Z bucket, then slowly recovered (14.88% at 13:30, 47.74% at 14:58) — a step change at exactly the minute pricing-engine and market-data-service stepped. [F19]
- redis-cache memory_pct collapsed in the same 13:22:00Z bucket from ~68-71% (13:18 avg 70.54) to 6.38% (max 6.81), then climbed steadily to 29.94% by 14:58 — the signature of a cache being re-populated from cold. [F20]
- redis-cache evictions_per_min did NOT spike at 13:22Z: flat ~2.3-3.7 keys/min across the whole window (13:18 avg 3.29, 13:22 avg 3.11, 14:58 avg 2.32; overall max 4.87), so the emptying was not eviction/memory-pressure driven. [F21]
- market-data-service's own latency p99 stepped in the same 13:22:00Z bucket: avg ~39.8-40.92ms through 13:18, then 1916.65ms at 13:22 (peak max 1979.98ms), decaying to 874.6ms by 14:58; its error_rate also stepped 0.18%→3.23% at 13:22Z. [F22]

## Assumptions (not verified)
- redis-cache's hit_ratio/memory collapse at 13:22Z is a genuine cache-content reset rather than an artifact shared with the simultaneous market-data-service miss storm (redis-cache exposes no independent restart/uptime/latency metric).
- The reset has no enumerable trigger — it is an unrecorded change or a redis-side failover/restart outside the change system (the only window change, dep-5604, is off-graph and 1h52m early; zero config/flag changes exist).
- The slowdown reaches the user-facing API layer (quote screens); api-gateway and order-service latency were never retrieved, so the final hop of the causal chain is inferred rather than measured.
- A concurrent market-data-service-side restart/saturation at 13:22Z is assumed absent; its cpu/memory/restart metrics were never retrieved (budget_exhausted), so a concurrent internal fault is not conclusively excluded.
- (demoted from facts: no verified observation backs it) Temporal ordering within the onset minute favors the cache: the redis-cache hit-ratio/memory reset occupies the 13:22:00Z bucket, pricing-engine's p99 step is at 13:22:00Z, and market-data-service's cache-miss-storm WARN log first appears at 13:22:25Z — the cache metric reset is not later than the rebuild storm that names it.

## Timeline
- `2026-04-08T00:00:00Z` Start of the Apr 8 portion of the window: zero deployments across all services. [F2]
- `2026-04-09T11:30:00Z` Only deployment in the full window: dep-5604, auth-service v7.2.0 ('Session cookie hardening', SameSite=strict), status 'live', rolled_back_at=null. auth-service is off pricing-engine's dependency graph. [F1, F4, F6]
- `2026-04-09T13:18:00Z` Last pre-onset steady state: redis-cache memory_pct avg 70.54, evictions_per_min avg 3.29; redis-cache hit_ratio ~95-96%; market-data-service p99 ~40ms. [F19, F20, F21, F22]
- `2026-04-09T13:22:00Z` Shared redis-cache contents reset: hit_ratio_pct collapses 95.33%→11.55% and memory_pct 70.54%→6.38% in the same minute bucket, while evictions_per_min stays flat (~3.11) — an external reset/restart/flush, not eviction. [F19, F20, F21]
- `2026-04-09T13:22:00Z` Same minute: pricing-engine p99 steps from ~56-64ms to 2227.82ms and 5xx steps from ~0.15-0.28% to 3.21%; market-data-service p99 simultaneously steps to 1916.65ms with error_rate 0.18%→3.23%. Incoming load is flat (~647.09 req/s). [F7, F8, F22, F13]
- `2026-04-09T13:22:25Z` market-data-service 'cache miss storm' WARN logs begin: quote keys not in redis-cache, rebuilding from exchange feed snapshot (1034-8748ms per rebuild). [F11, F23]
- `2026-04-09T13:23:57Z` First pricing-engine deadline-exceeded ERROR: 'quote computation deadline exceeded: market-data-service took 5171ms'; outbound call times 827-8456ms. [F9]
- `2026-04-09T14:58:00Z` Slow cold re-warm continues: redis-cache hit_ratio 47.74%, memory_pct 29.94%, evictions flat 2.32/min; pricing-engine p99 still ~1141ms and 5xx ~3.9-4.3%; last deadline-exceeded error 14:58:47Z. [F19, F20, F21, F7, F8, F9]

## Recommended actions
- **[immediate]** Treat the shared redis-cache reset as the incident origin: confirm with the platform/Redis owners whether a failover, restart, manual FLUSH, or infrastructure event (e.g., node failover, upgrade, or scheduled maintenance) hit the shared redis-cache at 2026-04-09T13:22Z, and capture the failover/restart timestamp from the Redis control plane (not available in the metric set). -- H1 attributes the whole chain to an external reset of the shared cache (F19, F20, F21) with no enumerable trigger (F1, F3). Confirming the cache-side event closes the single unverified origin hop of the leading hypothesis and isolates whether a change-system gap allowed it. [F19, F20, F21, F1, F3]
- **[immediate]** If the cache reset is confirmed as a Redis-side restart/failover, restore warm cache state deliberately: pre-warm the shared redis-cache from market-data-service's snapshot path before shifting full quote traffic back, or enable gradual cache warming to avoid a second cold-start amplification under production load. -- The observed failure is a cold-cache rebuild storm with slow recovery (F11, F19, F20); controlled pre-warming removes the rebuild-storm mechanism that blocked pricing-engine calls past deadline (F9). [F11, F19, F20]
- **[short_term]** Add or enable a shared-redis-cache health signal — restart/uptime, latency/response-time, and connected_clients — and alert on a simultaneous hit_ratio + memory step (the exact F19/F20 signature) so a silent content reset pages immediately instead of surfacing as pricing-engine latency. -- redis-cache exposes no restart/uptime/latency metric (only connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct), so the reset was only inferable after the fact from simultaneous hit_ratio and memory collapse (F19, F20, F21). [F19, F20, F21]
- **[short_term]** Verify the user-facing blast radius by measuring api-gateway and order-service latency_p99 for 12:30-14:59Z, which was never retrieved; confirm whether the reported 2-3s quote load time reproduces at the API layer. -- The final hop of the causal chain (trading-screen latency) is inferred, not measured: api-gateway and order-service latency were not retrieved, though their error rates are flat (F16, F17). Confirming or refuting propagation completes the impact picture without changing the fix. [F12, F16, F17]
- **[short_term]** Close the H1-vs-H2 residual by retrieving market-data-service cpu_pct, memory_pct, request_rate, and restarts for 12:30-14:59Z (previously budget_exhausted) and correlating the 1034-8748ms rebuild times against redis-cache wait vs exchange-gateway wait. -- H2 (fault inside market-data-service) remains live only because its own restart/saturation metrics were never retrieved, so a concurrent internal fault at 13:22Z is not excluded (F22; assumptions). If market-data-service shows no independent step/restart in that bucket, H2 is conclusively refuted. [F22, F11, F20]
- **[long_term]** Eliminate the change-system blind spot: ensure platform-owned infrastructure components (shared redis-cache) have auditable events (failover, restart, flush, maintenance) recorded in the same change/event store as service deployments, since the 13:22Z reset has no enumerable trigger despite complete deployment (F1, F2) and config (F3) enumeration. -- The only recorded change in the window is off-graph and 1h52m early (F1, F4, F6); the reset origin is unrecorded, matching the HIST-107 'cold cache storm after redis failover' analogue. [F1, F2, F3, F6]
- **[long_term]** Harden pricing-engine against dependency stalls: add a bounded cache-miss/rebuild budget or circuit-breaker on the market-data-service call path, and consider serving last-known-good quotes under deadline pressure, so a downstream cold-cache rebuild cannot drive p99 to 2+ seconds and 4% 5xx. -- pricing-engine's own CPU/memory were flat (F10); the failure was purely a blocked synchronous call 'market-data-service took 827-8456ms' (F9), so resilience belongs at the call boundary. [F9, F10]

## Data gaps
- redis-cache exposes no restart-count, uptime, or latency/response-time metric (available: connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct), and its connected_clients/cpu_pct values were not retrieved — so no direct restart/failover/promotion event for the cache is confirmed; the reset is inferred from the simultaneous hit_ratio + memory collapse (F19, F20, F21).
- market-data-service's own cpu_pct, memory_pct, request_rate, and restarts metrics for 12:30-14:59Z returned budget_exhausted, so the decisive sub-test 'did market-data-service restart/saturate independently of the cache at 13:22Z' could not be executed — H2 is not conclusively refuted.
- api-gateway and order-service latency_p99_ms were not retrieved (only error_rate), so downstream latency propagation to the user-facing API layer (the reported 2-3s quote load) is inferred only from pricing-engine p99, not measured.
- Outbound call latency from market-data-service to exchange-gateway vs to redis-cache (wait-time correlation of the 1034-8748ms rebuilds) was not retrievable, so it cannot be determined whether the slow rebuilds correlate with cache wait time or exchange-gateway wait time.
- api-gateway latency/error metric query and the broader INFO-level '401' log search were not completed (budget_exhausted / timeout), so auth 401/403 absence is confirmed at WARN only, not across all log levels.
- pricing-engine thread/connection-pool and GC metrics do not exist in the available metric list, and deployment enumeration was completed only by splitting into two partial-free windows after a partial first query and a timed-out retry, so a deployment in a shard that failed only during the unsplit query cannot be fully excluded.
- pricing-engine and market-data-service log results were truncated (194 and 290 matched vs 40 and 25 returned, ascending order), so late-window signatures beyond ~13:42Z are not fully characterized.

## What would change this conclusion
- Retrieving market-data-service cpu_pct/memory_pct/restarts showing its own independent restart or resource saturation beginning at 13:22Z would elevate H2 (fault inside market-data-service) over H1, since the cache miss storm could then be a symptom of market-data-service failing to serve rather than the cache being reset.
- Evidence that redis-cache's hit_ratio/memory collapse is an artifact of the miss storm rather than a genuine content reset (e.g., a Redis restart/uptime metric showing no reset, or a counter that explains the memory drop without a flush) would break the core premise of H1.
- Discovery of a recorded change or config/flag update on redis-cache or market-data-service near 13:22Z (within the unverified Apr 8 config slice or a shard that failed during the unsplit deployments query) would reinterpret the origin as a bad/triggered change rather than an unrecorded infrastructure event.
- Measurement showing pricing-engine request_rate or a downstream queue actually ramped at 13:22Z (contradicting the flat ~641.9 req/s) would revive the load-driven capacity mechanism of H4.
- Evidence that the 1034-8748ms rebuilds correlate with exchange-gateway wait time rather than cache wait time would shift the origin downstream of the cache, favoring a dependency/exchange-gateway fault path.

## Integrity warnings (set by code)
- fact demoted to assumption: Temporal ordering within the onset minute favors the cache: the redis-cache hit-

## Investigation stats
- latency 173.09s, LLM calls 22, tokens 156997 (in 128167 / out 28830)
- tool calls 32 (6 failed, 1 partial, 7 recovered by retry), findings 21/23 verified
