# Incident investigation report: INC-006
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The incident is best explained by H1 (confidence 0.55, medium): degradation of the shared redis-cache caused market-data-service to rebuild quotes from the exchange feed snapshot on every request, driving its CPU to ~87% and p99 latency to ~1.8s, which blew pricing-engine's downstream deadline and surfaced 2-3s quote loads. The evidence is consistent and strongly directional: pricing-engine's 192 errors all cite the downstream market-data-service call (F3), the latency and CPU degradation sits on market-data-service, not pricing-engine (F4-F5, F16), and market-data-service's WARN logs are 289/290 'cache miss storm ... not in redis-cache' with rebuilds of 1-8.7s (F6). No change touched the affected path (F7-F10, F14), so the single auth-service deploy is ruled out on both timing and blast radius (F15). The conclusion is nevertheless limited by a decisive unverified link: redis-cache's own metric trend at onset was never retrieved (latency_p99_ms not_found; budget exhausted for hit-ratio/eviction trends), so the cache-side trigger (eviction/memory pressure vs. a market-data-service-side load surge) is inferred rather than observed, and the upstream exchange feed (H2, 0.25) cannot be fully excluded.

## Probable root cause
**Shared redis-cache degradation (cold cache / eviction storm) propagates through market-data-service to pricing-engine** -- infrastructure_failure in `redis-cache`, triggered by `null` (confidence 0.55, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Shared redis-cache degradation (cold cache / eviction storm) propagates through market-data-service to pricing-engine | infrastructure_failure | redis-cache | null | 0.55 (medium) | active |
| 2 | H2: Upstream exchange feed / exchange-gateway slowdown makes cache rebuilds slow, cascading to market-data-service and pricing-engine | dependency_failure | exchange-gateway / external exchange-api | null | 0.25 (low) | active |
| 3 | H3: Market-data-service self-saturation from load growth (capacity saturation) drives the cascade | capacity_saturation | market-data-service | null | 0.20 (low) | active |
| 4 | H4: auth-service v7.2.0 deployment (11:30Z) caused the latency (reporter/orchestrator's 'today's changes elsewhere' theory) | bad_deployment | auth-service | dep-5604 | 0.03 (low) | rejected |

### H1: Shared redis-cache degradation (cold cache / eviction storm) propagates through market-data-service to pricing-engine
The shared redis-cache lost or evicted quote entries, so market-data-service began rebuilding quotes from the exchange feed snapshot on every request (cache miss storm). Each rebuild takes 1-8.7s, which pushed market-data-service's CPU to ~87% and latency to ~1.8s, causing pricing-engine's downstream market-data-service call to blow its deadline and surface 2-3s quote loads on trading screens.

**Causal chain**
1. redis-cache (shared:true, single shared datastore on the quote path) degrades — cold cache / eviction or failover event [F11, F12, F17]
2. market-data-service begins a cache miss storm on redis-cache, rebuilding quotes from exchange feed snapshot in 1-8.7s [F6, F18]
3. Rebuild load drives market-data-service CPU from 44.8% to ~87% (max 94.5%) and its p99 from ~40ms to ~1.8s [F4, F5, F16]
4. pricing-engine's market-data-service call exceeds its deadline (5171ms, 8456ms) [F3]
5. pricing-engine p99 jumps 60ms→1216ms→2097ms and errors rise 0.2%→4.2%; slow quotes visible on trading screens [F1, F2]

**Supporting evidence**
  - `F1` ✔ pricing-engine p99 latency was flat at ~60ms through the 13:00 bucket, then jumped to avg 1216.56ms / max 2309.91ms in the 13:15 bucket and peaked at avg 2097.96ms (max 2238.06ms) in the 13:30 bucket, remaining elevated through 14:45 (avg 1191.68ms).
  - `F2` ✔ pricing-engine error_rate was stable at ~0.2% until the 13:00 bucket, then rose to avg 2.11% (max 4.37%) in the 13:15 bucket and held ~4.0-4.2% through 14:45.
  - `F3` ✔ All 192 pricing-engine ERROR logs 13:23:57Z-14:58:47Z share a single signature: 'quote computation deadline exceeded: market-data-service took <n>ms' (e.g. 5171ms, 8456ms) -- i.e. deadline exceeded on the downstream market-data-service call, not 5xx or local resource errors.
  - `F4` ✔ market-data-service shows nearly the same latency degradation as pricing-engine: p99 flat at ~40ms through 13:00, then avg 1037.16ms / max 1979.98ms at 13:15 and avg 1791.27ms at 13:30, tracking pricing-engine closely.
  - `F5` ✔ market-data-service CPU jumped from ~44.8% (13:00 bucket) to avg 69.65% / max 94.51% at 13:15 and avg 87.54% at 13:30, while pricing-engine CPU stayed flat at ~39-40% across the whole window (13:15=39.77%, 13:30=39.57%).
  - `F6` ✔ market-data-service WARN logs 13:22:25Z-14:58:22Z are dominated (289 of 290) by 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)', with rebuilds taking 1-8.7s (e.g. 6864ms, 8748ms) -- pointing at the redis-cache dependency.
  - `F11` ✔ pricing-engine dependency graph: outbound edges to market-data-service (http) and redis-cache (cache); inbound edge from order-service (http), with api-gateway → order-service upstream. market-data-service itself calls redis-cache (cache) and exchange-gateway (http). redis-cache is the only node with "shared":true (type redis, owner platform); all other nodes are shared:false.
  - `F12` ✔ redis-cache is a shared component sitting on the pricing-engine → market-data-service → exchange-gateway quote path, i.e. a neighbour (market-data-service) can degrade pricing-engine through that shared cache; the highest-scoring historical lead is HIST-107 'Cold cache storm after redis failover' (services redis-cache, market-data-service; symptoms 'cache miss storm','quote latency'; root_cause 'Redis primary failure promoted an empty replica', match_score 3).
  - `F16` ✔ Blast radius of the latency matches market-data-service, not pricing-engine: market-data-service p99 rose ~40ms→avg 1791.27ms and its CPU 44.8%→avg 87.54% at 13:30 with max 94.51%, while pricing-engine CPU stayed flat at 39.77%/39.57% across the same buckets. pricing-engine shows no local saturation to explain its own 2s+ latencies.
  - `F17` ✔ The shared component architecture permits a neighbour to break pricing-engine: an independent depth-2 query on market-data-service returns the same shared node set -- redis-cache (type redis, owner platform, shared:true) called by both pricing-engine and market-data-service, plus market-data-service → exchange-gateway → external exchange-api (has_telemetry:false) and additional callers api-gateway and portfolio-service. So market-data-service is a fan-out dependency of at least four services while redis-cache is the single shared datastore.
  - `F18` ✔ The cache-miss/rebuild signature starts at 13:22:25Z against redis-cache, after the 13:15Z metric-bucket latency rise, and its rebuild timings (1-8.7s, e.g. 6864ms, 8748ms) exceed pricing-engine's own deadline budgets (errors cite market-data-service taking 5171ms, 8456ms) -- consistent with a degraded shared cache/rebuild path rather than pricing-engine compute.
  - `F19` ✔ Historical leads, treated as leads only: HIST-107 'Cold cache storm after redis failover' (redis-cache/market-data-service, match_score 3) matches this symptom pattern, while HIST-113 (pricing-engine 99% CPU) implies self-saturation that the flat ~40% pricing-engine CPU here does NOT show, and the same-day-release precedent HIST-101 is a different service/symptom (auth-service login, rolled back).
**Contradicting evidence**
  - (none)
**Assumptions**
  - redis-cache's own hit-ratio/eviction/memory trend at 13:15 was never retrieved (metric query failed not_found) — the cache-side cause is inferred, not observed
  - The cache-miss storm begins at 13:22:25Z, ~7 minutes AFTER the 13:15Z metro bucket latency rise; a pre-bucket Redis event must be assumed to explain the earlier rise
  - The 1-8.7s rebuild times are attributed to cache/rebuild path, not to a slow exchange feed upstream (exchange-gateway/exchange-api telemetry not retrieved)

### H2: Upstream exchange feed / exchange-gateway slowdown makes cache rebuilds slow, cascading to market-data-service and pricing-engine
The external exchange (or exchange-gateway) slowed down. market-data-service's cache rebuilds, which pull from the exchange feed snapshot, therefore took 1-8.7s, saturating market-data-service CPU and latency and blowing pricing-engine's deadline. The cache miss storm is a symptom of slow rebuilds rather than a cold cache.

**Causal chain**
1. External exchange-api (has_telemetry:false, vendor-owned) or exchange-gateway degrades [F17]
2. market-data-service cache rebuilds from exchange feed snapshot take 1-8.7s [F6]
3. Sustained rebuild work saturates market-data-service CPU (~87%) and latency (~1.8s p99) [F4, F5]
4. pricing-engine's market-data-service call exceeds deadline; quote latency 2-3s [F3, F1]

**Supporting evidence**
  - `F6` ✔ market-data-service WARN logs 13:22:25Z-14:58:22Z are dominated (289 of 290) by 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)', with rebuilds taking 1-8.7s (e.g. 6864ms, 8748ms) -- pointing at the redis-cache dependency.
  - `F17` ✔ The shared component architecture permits a neighbour to break pricing-engine: an independent depth-2 query on market-data-service returns the same shared node set -- redis-cache (type redis, owner platform, shared:true) called by both pricing-engine and market-data-service, plus market-data-service → exchange-gateway → external exchange-api (has_telemetry:false) and additional callers api-gateway and portfolio-service. So market-data-service is a fan-out dependency of at least four services while redis-cache is the single shared datastore.
  - `F18` ✔ The cache-miss/rebuild signature starts at 13:22:25Z against redis-cache, after the 13:15Z metric-bucket latency rise, and its rebuild timings (1-8.7s, e.g. 6864ms, 8748ms) exceed pricing-engine's own deadline budgets (errors cite market-data-service taking 5171ms, 8456ms) -- consistent with a degraded shared cache/rebuild path rather than pricing-engine compute.
**Contradicting evidence**
  - (none)
**Assumptions**
  - exchange-gateway metrics were never queried and exchange-api has no telemetry — the upstream slowdown is entirely unverified
  - The cache-miss signature ('rebuilding from exchange feed snapshot') implies the rebuild path touches the exchange, but does not by itself prove the exchange is slow

### H3: Market-data-service self-saturation from load growth (capacity saturation) drives the cascade
market-data-service's inbound request volume grew to the point where its provisioned capacity was exhausted, driving CPU to ~94% and latency to ~1.8s, which in turn blew pricing-engine's deadline. The cache-miss storm is a secondary effect of the load, not a cause.

**Causal chain**
1. Request load on market-data-service rises beyond provisioned capacity (request_rate never retrieved — budget_exhausted) *(assumed)*
2. market-data-service CPU saturates (44.8%→avg 87.5%, max 94.5%) and p99 rises to ~1.8s [F5, F4]
3. Cache miss storm appears as rebuilds slow under load (13:22:25Z onward) [F6]
4. pricing-engine deadline exceeded on market-data-service call; p99 2s+, error rate 4% [F3, F1, F2]

**Supporting evidence**
  - `F4` ✔ market-data-service shows nearly the same latency degradation as pricing-engine: p99 flat at ~40ms through 13:00, then avg 1037.16ms / max 1979.98ms at 13:15 and avg 1791.27ms at 13:30, tracking pricing-engine closely.
  - `F5` ✔ market-data-service CPU jumped from ~44.8% (13:00 bucket) to avg 69.65% / max 94.51% at 13:15 and avg 87.54% at 13:30, while pricing-engine CPU stayed flat at ~39-40% across the whole window (13:15=39.77%, 13:30=39.57%).
  - `F6` ✔ market-data-service WARN logs 13:22:25Z-14:58:22Z are dominated (289 of 290) by 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)', with rebuilds taking 1-8.7s (e.g. 6864ms, 8748ms) -- pointing at the redis-cache dependency.
**Contradicting evidence**
  - `F1` ✔ pricing-engine p99 latency was flat at ~60ms through the 13:00 bucket, then jumped to avg 1216.56ms / max 2309.91ms in the 13:15 bucket and peaked at avg 2097.96ms (max 2238.06ms) in the 13:30 bucket, remaining elevated through 14:45 (avg 1191.68ms).
**Assumptions**
  - market-data-service request_rate was never obtained (budget_exhausted), so load-driven onset is unproven
  - No deployment or config change explains a load shift (F9/F14) — a traffic surge would have to be organic market activity

### H4: auth-service v7.2.0 deployment (11:30Z) caused the latency (reporter/orchestrator's 'today's changes elsewhere' theory)
The only change in the window — auth-service v7.2.0 'Session cookie hardening' (SameSite=strict) at 11:30Z — altered session/traffic behavior and somehow degraded the quote path.

**Causal chain**
1. auth-service v7.2.0 deployed at 11:30Z with SameSite=strict cookie change [F7]
2. Session/cookie change affects request patterns or auth on the trading path *(assumed)*
3. pricing-engine quote latency degrades ~13:15Z [F1]

**Supporting evidence**
  - `F7` ✔ Only one deployment occurred across ALL services in the window 2026-04-08T00:00Z–2026-04-09T14:59Z: dep-5604, auth-service, version v7.2.0, deployed_at 2026-04-09T11:30:00Z, summary 'Session cookie hardening', changes ["SameSite=strict"], status 'live', rolled_back_at null. This was confirmed by a non-partial retry (attempts: 2 total).
**Contradicting evidence**
  - `F15` ✔ Timing mismatch rules out the single deployment as cause: dep-5604 auth-service v7.2.0 went live at 2026-04-09T11:30:00Z and was never rolled back ('status': 'live', rolled_back_at null), ~1h45m before pricing-engine latency jumped in the 13:15Z bucket and ~1h53m before the first deadline-exceeded error at 13:23:57Z. An auth/login-path cookie change (SameSite=strict) has no blast radius matching a quote-path latency event.
  - `F8` ✔ No deployment to pricing-engine or any trading-path service (market-data-service, order-service, exchange-gateway, api-gateway) appears in the window; the only change at or before the ~13:30Z onset is the auth-service deploy ~2h earlier (11:30Z).
  - `F14` ✔ No change of any kind touched the affected path or the shared component: redis-cache had 0 config changes (tc-017), market-data-service had 0 deployments (tc-018) and 0 config changes (tc-019) across 2026-04-08/09, matching the earlier all-services result of exactly one deployment (auth-service v7.2.0, 11:30Z) and 0 config changes. So the change timeline contains nothing at or near the ~13:15Z onset.
  - `F7` ✔ Only one deployment occurred across ALL services in the window 2026-04-08T00:00Z–2026-04-09T14:59Z: dep-5604, auth-service, version v7.2.0, deployed_at 2026-04-09T11:30:00Z, summary 'Session cookie hardening', changes ["SameSite=strict"], status 'live', rolled_back_at null. This was confirmed by a non-partial retry (attempts: 2 total).
**Assumptions**
  - No mechanism linking a login-path cookie hardening to a quote-path cache rebuild has been identified
  - An unmeasured side effect of the deploy on traffic routing would have to exist
**Rejected because:** Timing and blast radius both fail: dep-5604 went live ~1h45m before onset (13:15Z) and ~1h53m before the first deadline error (13:23:57Z), was never rolled back, and an auth/login cookie change has no plausible blast radius to the quote path. Every change query (F8, F14) confirms no change touched the affected path or shared component.

## Facts (verified observations)
- pricing-engine p99 latency was flat at ~60ms through the 13:00 bucket, jumped to avg 1216.56ms / max 2309.91ms in the 13:15 bucket, peaked at avg 2097.96ms (max 2238.06ms) at 13:30, and remained elevated through 14:45 (avg 1191.68ms). [F1]
- pricing-engine error_rate rose from a stable ~0.2% (through 13:00) to avg 2.11% / max 4.37% at 13:15 and held ~4.0-4.2% through 14:45. [F2]
- All 192 pricing-engine ERROR logs from 13:23:57Z to 14:58:47Z share one signature: 'quote computation deadline exceeded: market-data-service took <n>ms' (e.g. 5171ms, 8456ms) — a downstream deadline failure on market-data-service, not a 5xx or local resource error. [F3]
- market-data-service shows nearly the same latency degradation as pricing-engine: p99 flat ~40ms through 13:00, then avg 1037.16ms / max 1979.98ms at 13:15 and avg 1791.27ms at 13:30. [F4]
- market-data-service CPU jumped from ~44.8% (13:00) to avg 69.65% / max 94.51% at 13:15 and avg 87.54% at 13:30, while pricing-engine CPU stayed flat at ~39-40% throughout (39.77% at 13:15, 39.57% at 13:30). [F5]
- market-data-service WARN logs from 13:22:25Z to 14:58:22Z are dominated (289 of 290) by 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)', with rebuilds taking 1-8.7s (e.g. 6864ms, 8748ms). [F6]
- Only one deployment occurred across all services in 2026-04-08T00:00Z–2026-04-09T14:59Z: dep-5604, auth-service v7.2.0, deployed 11:30Z, 'Session cookie hardening' (SameSite=strict), status live, never rolled back; confirmed by a non-partial retry. [F7]
- No deployment to pricing-engine or any trading-path service (market-data-service, order-service, exchange-gateway, api-gateway) appears in the window. [F8]
- Zero config or feature-flag changes were recorded for all services in the window (count 0, partial false), and zero specifically for pricing-engine. [F9, F10]
- pricing-engine's dependency graph shows outbound edges to market-data-service (http) and redis-cache (cache); market-data-service calls redis-cache (cache) and exchange-gateway (http). redis-cache is the only node with shared:true (type redis, owner platform). [F11]
- Historical incidents are leads only: HIST-113 (capacity saturation at market open, pricing-engine cpu 99%), HIST-107 (redis/market-data cache-miss storm), HIST-101 (same-day release caused auth-service login latency — different service/symptom). [F13]
- No change of any kind touched the affected path or the shared component: redis-cache 0 config changes; market-data-service 0 deployments and 0 config changes across 2026-04-08/09, matching the all-services result of exactly one deployment (auth-service, 11:30Z) and 0 config changes. [F14]
- The blast radius matches market-data-service, not pricing-engine: market-data-service p99 ~40ms→avg 1791.27ms and CPU 44.8%→avg 87.54% (max 94.51%) at 13:30, while pricing-engine CPU stayed flat at 39.77%/39.57%; pricing-engine shows no local saturation to explain its own 2s+ latencies. [F16]
- An independent depth-2 query confirms the shared node set: redis-cache (type redis, owner platform, shared:true) called by both pricing-engine and market-data-service, plus market-data-service → exchange-gateway → external exchange-api (has_telemetry:false); market-data-service is a fan-out dependency of at least four services (api-gateway, portfolio-service, and others) while redis-cache is the single shared datastore. [F17]
- The cache-miss/rebuild signature starts at 13:22:25Z against redis-cache, after the 13:15Z metric-bucket latency rise, and its rebuild timings (1-8.7s) exceed pricing-engine's own deadline budgets (errors cite 5171ms, 8456ms), consistent with a degraded shared cache/rebuild path rather than pricing-engine compute. [F18]

## Assumptions (not verified)
- redis-cache's own hit-ratio/eviction/memory trend at onset was never retrieved (latency_p99_ms query returned not_found; budget exhausted for other cache metrics), so the cache-side cause is INFERRED from the downstream cache-miss signature, not directly observed.
- A pre-bucket redis-cache event is assumed to explain the 13:15Z metric-bucket latency rise, because the explicit cache-miss storm signature only begins at 13:22:25Z, ~7 minutes later.
- The 1-8.7s rebuild times are assumed to originate in the cache/rebuild path rather than a slow upstream exchange feed, since exchange-gateway metrics were not queried and exchange-api has no telemetry.
- The 'no change on the affected path' conclusion assumes the change inventory is complete; get_deployments was partial on the first call and only non-partial after retry, and out-of-band infrastructure actions (e.g. a redis node restart, eviction policy or memory-limit change) would not appear in deployment/config change APIs at all.
- H1's causal chain (cache degradation → rebuild storm → market-data-service saturation → pricing-engine deadline) is assumed to be the direction of causality; the alternative that a load surge caused the cache misses is not fully excluded because market-data-service request_rate was never obtained.
- pricing-engine's flat ~40% CPU and lack of local resource errors are assumed to be representative of its compute state across the whole window, not just the sampled buckets.
- (demoted from facts: no verified observation backs it) redis-cache is a shared component on the pricing-engine → market-data-service → exchange-gateway quote path, so a neighbour can degrade pricing-engine through the shared cache; the top historical lead is HIST-107 'Cold cache storm after redis failover' (redis-cache/market-data-service; symptoms 'cache miss storm','quote latency'; match_score 3).
- (demoted from facts: no verified observation backs it) dep-5604 auth-service v7.2.0 went live at 11:30Z and was never rolled back, ~1h45m before the 13:15Z latency jump and ~1h53m before the first deadline-exceeded error at 13:23:57Z; an auth/login-path cookie change has no blast radius matching a quote-path latency event.
- (demoted from facts: no verified observation backs it) Historical leads treated as leads only: HIST-107 matches this symptom pattern, while HIST-113 (pricing-engine 99% CPU self-saturation) is inconsistent with the flat ~40% pricing-engine CPU observed here.

## Timeline
- `2026-04-09T11:30:00Z` Only deployment in the window goes live: dep-5604 auth-service v7.2.0 'Session cookie hardening' (SameSite=strict), status live, never rolled back. This is the reporter's 'today's changes elsewhere' candidate. [F7, F8]
- `2026-04-09T13:00:00Z` Last healthy baseline bucket: pricing-engine p99 flat ~60ms and error_rate ~0.2%; market-data-service p99 flat ~40ms, CPU ~44.8%. [F1, F2, F4, F5]
- `2026-04-09T13:15:00Z` Onset bucket: pricing-engine p99 jumps to avg 1216.56ms / max 2309.91ms and error_rate to avg 2.11%; market-data-service p99 jumps to avg 1037.16ms / max 1979.98ms with CPU avg 69.65% / max 94.51%. pricing-engine CPU stays flat at 39.77%. [F1, F2, F4, F5, F16]
- `2026-04-09T13:22:25Z` First market-data-service WARN 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot' — the explicit cache-miss/rebuild signature, with rebuilds of 1-8.7s (e.g. 6864ms, 8748ms). [F6, F18]
- `2026-04-09T13:23:57Z` First pricing-engine ERROR of the incident: 'quote computation deadline exceeded: market-data-service took 5171ms'. [F3]
- `2026-04-09T13:30:00Z` Peak degradation: pricing-engine p99 avg 2097.96ms (max 2238.06ms); market-data-service p99 avg 1791.27ms with CPU avg 87.54% (max 94.51%); pricing-engine CPU still flat at 39.57%. Incident reported to on-call SRE at 13:30:00Z. [F1, F4, F5, F16]
- `2026-04-09T14:45:00Z` Conditions remain elevated rather than resolved: pricing-engine p99 avg 1191.68ms and error_rate ~4.0-4.2%. [F1, F2]
- `2026-04-09T14:58:47Z` Latest pricing-engine ERROR in the window; all 192 errors across 13:23:57Z-14:58:47Z share the single downstream-market-data-service deadline signature. market-data-service cache-miss WARNs continue to 14:58:22Z (289 of 290). [F3, F6]

## Recommended actions
- **[immediate]** Verify and, if confirmed, mitigate the redis-cache side: immediately query redis-cache memory_pct, evictions_per_min, hit_ratio_pct and connected_clients for 12:45Z-15:00Z (note: latency_p99_ms does not exist for redis-cache — do not re-issue it). If memory_pct is near cap and/or evictions_per_min is spiking at/just before 13:15Z, treat H1's cache-side trigger as confirmed and act on the cache (raise memory limit / eviction headroom, or review eviction policy) rather than on market-data-service. -- This is the single decisive unverified link in H1 (0.55): the cache-miss storm is observed (F6, F18) but redis-cache's own state at onset was never retrieved. It simultaneously confirms H1 and discriminates H1 from H3 (market-data-service load-driven saturation), whose request_rate metric was also never obtained. [F6, F18, F11, F17]
- **[immediate]** Apply immediate load protection on the cache-miss/rebuild path: enable short-TTL negative/backfill caching or single-flight (request coalescing) on the market-data-service 'rebuild from exchange feed snapshot' path so concurrent misses for the same quote do not each trigger a 1-8.7s rebuild, and/or temporarily increase the pricing-engine deadline for the market-data-service call. -- Every one of pricing-engine's 192 errors is a downstream deadline exceeded on market-data-service, citing 5171-8456ms (F3), against rebuilds of 1-8.7s (F6). Cutting the rebuild amplification or widening the deadline directly relieves the observed failure mode, which persists as of 14:58Z (F3, F6). [F3, F6, F18]
- **[immediate]** Do NOT roll back or modify auth-service v7.2.0 (dep-5604) as a remediation: it went live 11:30Z, was never rolled back, and is ~1h45m-1h53m before onset with no plausible quote-path blast radius. Direct the responder's attention to redis-cache/market-data-service instead. -- H4 is already rejected (0.03) on timing and blast radius (F7, F8, F14, F15); acting on it would burn mitigation time on a non-cause while the real degradation continues. [F7, F8, F14, F15]
- **[short_term]** Run the discriminating query set that the budget cut off: market-data-service request_rate and memory_pct (for 13:00-14:00) to test H3, plus api-gateway latency to confirm blast radius; and, to test H2, retrieve exchange-gateway metrics. If request_rate is flat while CPU and cache misses rose, H3 is excluded and H1/H2 remain; if request_rate rose proportionally, H3 gains weight. -- H3's key assumption is explicitly that request_rate was never obtained (budget_exhausted), and H2's is that exchange-gateway was never queried with exchange-api untelimetried (has_telemetry:false, F17). These are the exact data gaps separating H1 (0.55) from H2 (0.25) and H3 (0.20). [F4, F5, F6, F17]
- **[short_term]** Fix and verify: after the cache-side change, confirm recovery against the same signals that degraded — pricing-engine p99 back toward ~60ms and error_rate back toward ~0.2%, market-data-service p99 toward ~40ms and CPU toward ~45%, and market-data-service 'cache miss storm' WARNs ceasing (currently 289 of 290 WARNs). -- These are the verified pre-incident baselines (F1, F2, F4, F5) and the dominant WARN signature (F6); returning to them is the objective verification that the rebuild storm, not just the symptom, has ended. [F1, F2, F4, F5, F6]
- **[long_term]** Add the missing telemetry and safeguards long term: instrument redis-cache with a latency/health metric (latency_p99_ms returned not_found, tc-016) and alert on evictions_per_min / memory_pct / hit_ratio_pct; add a cache-miss-storm alert on market-data-service WARN rate; and add a documented pre-approved cache-restore/backfill runbook, since the top historical lead is HIST-107 'Cold cache storm after redis failover'. -- The root cause could not be observed on the cache side precisely because redis-cache exposes no usable latency metric and its trend was not retrieved (gaps + tc-016), and the matching historical precedent HIST-107 (match_score 3) shows this failure class recurs (F12, F19). [F12, F18, F19]
- **[long_term]** Close the change-inventory blind spot: instrument/record out-of-band infrastructure actions (redis node restarts, eviction-policy or memory-limit changes, failovers) into the same change feed as deployments and config changes, and fix the get_deployments partial-shard condition (tc-001 returned partial:true before the clean retry). -- The change timeline contains nothing at or near the 13:15Z onset (F7-F10, F14), yet a cache-side inflicting event must exist under H1; today such an infrastructure action would be invisible to the deployment/config APIs, leaving the trigger unprovable. [F7, F9, F14]

## Data gaps
- redis-cache's own metrics for the onset window were never retrieved: latency_p99_ms does not exist for redis-cache (tc-016 FAILED not_found), and hit_ratio_pct / evictions_per_min / memory_pct were never queried (budget exhausted). This is the decisive missing evidence for H1 — the cache-side cause is inferred from downstream symptoms (F6, F18) rather than observed directly.
- market-data-service request_rate and memory_pct were not obtained (budget_exhausted), so H3 (load-driven capacity saturation) cannot be confirmed or excluded, and H1's alternative framing (load surge caused the misses) remains open.
- api-gateway latency was not queried, so it is unverified whether front-end gateways/trading screens share the same symptom (blast-radius confirmation incomplete).
- exchange-gateway metrics were never queried and exchange-api has has_telemetry:false (vendor-owned, F17), so H2 (upstream exchange feed slowdown making rebuilds slow) cannot be excluded; the 1-8.7s rebuild times have no attributed upstream cause.
- No per-service flag/config inventory was returned (get_config_changes returned count 0 with no breakdown), so 'no flag changed' rests on an empty result rather than a list of unchanged flags; a silently absent config shard cannot be ruled out.
- get_deployments returned partial:true on the first call and partial:false only after retry (tc-001, tc-008), and a per-service deployment cross-check for trading services was not performed, leaving residual (low) uncertainty about a deployment in a low-ownership/platform shard.
- Change coverage is limited to 2026-04-08T00:00Z–2026-04-09T14:59Z and excludes out-of-band infrastructure actions (redis node restart, eviction-policy or memory-limit change, failover), which would not appear in get_deployments/get_config_changes yet could be the trigger underlying H1.
- Timing gap unresolved: the metric-bucket latency rise begins at 13:15Z but the explicit cache-miss storm signature begins at 13:22:25Z, so a pre-bucket cache event must be assumed rather than demonstrated.

## What would change this conclusion
- If redis-cache hit_ratio_pct / evictions_per_min / memory_pct turn out to be healthy and flat across 13:00-15:00, H1's cache-side trigger is falsified; the miss storm would then most likely reflect a market-data-service-side or upstream-feed-side cause, shifting weight to H2/H3.
- If market-data-service request_rate is shown to have risen proportionally with CPU and cache misses at 13:15Z, H3 (load-driven saturation) becomes the better explanation and the cache misses become a symptom of the load rather than its cause.
- If exchange-gateway latency/metrics or exchange feed timings show degradation beginning at or before 13:15Z, H2 (upstream exchange feed slowdown making rebuilds slow) would move ahead of H1, since the 1-8.7s rebuild times would then be attributed upstream.
- If a redis-cache failover, node restart, or eviction-policy/memory-limit change is discovered in out-of-band infrastructure records around 13:15Z, that would confirm and sharpen H1's mechanism (matching HIST-107); absence of such an event would weaken the failover variant of H1.
- If pricing-engine CPU or memory is found to have saturated locally in buckets not sampled here (contradicting F5/F16's flat ~40%), the premise that pricing-engine is purely a downstream victim would fail and a pricing-engine-local cause would need re-ranking.
- If a deployment or config change to a trading-path service (or to redis-cache/platform ownership) were found outside the retrieved shard set — i.e. the partial-then-clean deployment result (tc-001/tc-008) concealed a real change near 13:15Z — a bad-deployment hypothesis would need to be reopened, though the timing/mechanism burden would still apply.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: redis-cache is a shared component on the pricing-engine → market-data-service → 
- fact demoted to assumption: dep-5604 auth-service v7.2.0 went live at 11:30Z and was never rolled back, ~1h4
- fact demoted to assumption: Historical leads treated as leads only: HIST-107 matches this symptom pattern, w

## Investigation stats
- latency 120.77s, LLM calls 12, tokens 76030 (in 58858 / out 17172)
- tool calls 20 (1 failed, 1 partial, 2 recovered by retry), findings 19/19 verified
