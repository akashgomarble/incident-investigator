# Incident investigation report: INC-006
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause identified with high confidence (0.82): the shared redis-cache primary redis-cache-2 failed its PING health check at 13:22:10Z and the replica promoted in its place was mid-resync and therefore empty, producing a cache-miss storm that forced market-data-service to synchronously rebuild quotes from the exchange-feed snapshot (up to 8748ms), saturating its CPU and pushing its p99 past pricing-engine's computation deadline, which surfaced as 2-3s quote loads on trading screens. The origin hop is verified by cache-layer telemetry: hit_ratio_pct collapsed 95.9% -> 10.82% at 13:22Z while memory_pct fell 71% -> 6.1% and connected_clients fell ~295 -> 40 (an empty/new-primary signature), with evictions flat, plus an explicit failover log naming redis-cache-3 as promoted with an empty dataset. Competing origins were actively falsified: request rates at market-data-service and pricing-engine were flat across onset (no load spike), exchange-gateway and its upstream feed were flat, and no deployment or config change exists anywhere in the blast radius. Why redis-cache-2 failed its health check is not determined and remains a residual uncertainty, but it sits above the identified origin and does not change the mitigation.

## Probable root cause
**redis-cache primary failed and an empty replica was promoted, causing a cache-miss storm that forces market-data-service rebuilds and propagates as pricing-engine quote latency** -- infrastructure_failure in `redis-cache`, triggered by `null` (confidence 0.82, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: redis-cache primary failed and an empty replica was promoted, causing a cache-miss storm that forces market-data-service rebuilds and propagates as pricing-engine quote latency | infrastructure_failure | redis-cache | null | 0.82 (high) | active |
| 2 | H2: Load-driven saturation at market-data-service (rebuild path) exceeds provisioned capacity — no change needed | capacity_saturation | market-data-service | null | 0.06 (low) | rejected |
| 3 | H4: Unenumerated change on an unqueried trading-path service (order-service / exchange-gateway / api-gateway) or on pricing-engine itself | bad_deployment | unknown | null | 0.03 (low) | rejected |
| 4 | H3: auth-service dep-5604 (SameSite=strict session cookie hardening) is the trigger | bad_deployment | auth-service | dep-5604 | 0.02 (low) | rejected |
| 5 | H5: Genuine resource exhaustion (memory) on pricing-engine itself | resource_exhaustion | pricing-engine | null | 0.02 (low) | rejected |

### H1: redis-cache primary failed and an empty replica was promoted, causing a cache-miss storm that forces market-data-service rebuilds and propagates as pricing-engine quote latency
The fault originates at the shared redis-cache layer: the primary node redis-cache-2 failed its health check (3 consecutive PING timeouts) and was marked down at 13:22:10Z; the replica promoted in its place was mid-resync and therefore EMPTY (explicit WARN 'dataset on new primary is empty'). The promoted node's dataset loss is confirmed by telemetry: memory_pct dropped 71% -> 6.1% and connected_clients fell ~295 -> 40 simultaneously, with hit_ratio_pct collapsing ~95% -> 10.82% at exactly 13:22Z. market-data-service therefore misses quote:<n> keys and synchronously rebuilds each quote from the exchange-feed snapshot (up to 8748ms), saturating its CPU (~90%) and blowing its p99, which makes pricing-engine's market-data call exceed its computation deadline ('quote computation deadline exceeded: market-data-service took <n>ms'), producing 2-3s quote loads on trading screens. This is the HIST-107 mechanism ('Redis primary failure promoted an empty replica') reproduced exactly, but with no recorded deployment or config change triggering it — it is an unrecorded infrastructure event.

**Causal chain**
1. redis-cache primary redis-cache-2 fails health check (3 consecutive PING timeouts) and is marked down at 13:22:10Z; the promoted replica redis-cache-3 is mid-resync and EMPTY ('dataset on new primary is empty') [F22]
2. The empty promoted dataset is confirmed at the metric layer: hit_ratio_pct collapses from ~94.7-97% (through the 13:20Z bucket) to 10.82% at 13:22Z, memory_pct drops 71% -> 6.1%, connected_clients collapses ~295 -> 40 — an empty/new-primary signature, not load or eviction [F19, F21, F23]
3. The cache-loss is NOT eviction-driven: evictions_per_min stays flat ~2.0-4.9 keys/min across 12:50-14:29Z with no burst at onset, so the hit-ratio collapse is key loss, not memory pressure [F20]
4. market-data-service now misses quote:<n> keys and logs 'cache miss storm ... rebuilding from exchange feed snapshot', 289 WARNs from 13:22:25Z, rebuilds up to 8748ms [F11, F23]
5. The synchronous rebuild path saturates market-data-service CPU (~45% -> ~90% sustained) and its p99 jumps to ~1161ms avg / 1980ms max from ~40ms baseline in the same 13:20Z onset bucket [F10]
6. pricing-engine's slow call is market-data-service: 'quote computation deadline exceeded: market-data-service took <n>ms' (192 errors, first_seen 13:23:57Z) [F8]
7. pricing-engine p99 jumps from ~58-62ms to 1370-2218ms with error_rate rising to ~4% in the 13:15-13:20Z onset window [F7, F9]
8. blended client effects: client reconnect storm (15048 new connections in 10s, 13:30:20Z) and contractile tables hold the elevated latency [F22]
9. Sales/app trading screens report 2-3s quote loads *(assumed)*

**Supporting evidence**
  - `F22` ✔ redis-cache logs show an explicit FAILOVER at 13:22:10Z: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' (ERROR 13:22:10Z), then 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' (WARN 13:22:52Z), followed by full resync at 13:28:42Z and a 'client reconnect storm: 15048 new connections in 10s' at 13:30:20Z.
  - `F19` ✔ redis-cache hit_ratio_pct was stable at ~94.7-97% through the 13:20:00Z bucket, then COLLAPSED to 10.82% at 13:22:00Z and stayed low (12.28% at 13:24Z, 14.26% at 13:29Z). The 13:00/13:15/13:17/13:20 buckets were all normal (~95-96%).
  - `F21` ✔ redis-cache memory_pct dropped abruptly from ~71% at 13:20Z to 6.1% at 13:22Z and connected_clients fell from ~295 (13:17Z) to 40.24 at 13:23Z and then recovered to ~310 by 13:26Z. A simultaneous collapse of memory footprint and clients is consistent with an EMPTY dataset/new primary, not a load or eviction event.
  - `F20` ✔ redis-cache evictions_per_min did NOT spike at onset: it stayed flat/steady ~2.0-4.9 keys/min across 12:50-14:29Z (13:17Z 3.25, 13:20Z 2.98, 13:23Z 3.49, 13:26Z 2.26), i.e. NO eviction burst. The hit-ratio collapse was not caused by eviction pressure.
  - `F23` ✔ The redis-cache hit-ratio collapse (13:22:00Z bucket) sits in the SAME onset window as pricing-engine p99 jump (13:20Z bucket, F7) and market-data-service 'cache miss storm' WARN burst (first_seen 13:22:25Z, F11) — the cache-layer event is tightly coincident with (within ~2 min of) the application-layer symptoms.
  - `F11` ✔ market-data-service logs show a 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' WARN burst — 289 occurrences, first_seen 13:22:25Z — with rebuild times up to 8748ms, indicating cache misses forcing expensive snapshot rebuilds.
  - `F10` ✔ market-data-service p99 latency jumped in the same 13:20Z bucket (avg 1160.98ms, max 1979.98ms from ~40ms baseline) and its CPU rose from ~45% (13:15Z) to 72.72% avg / 93.99% max at 13:20Z, then ~90% sustained — the same onset window as pricing-engine.
  - `F8` ✔ pricing-engine's error signature is dominated by 'quote computation deadline exceeded: market-data-service took <n>ms' — 192 ERROR occurrences, first_seen 13:23:57Z — naming market-data-service as the slow call, not generic timeouts or connection errors.
  - `F7` ✔ pricing-engine p99 latency was flat ~58-62ms through 13:15Z, then jumped in the 13:20Z bucket to avg 1370.73ms / max 2281.51ms and stayed elevated (13:25Z avg 2218.07ms), i.e. onset between 13:15Z and 13:20Z.
  - `F9` ✔ pricing-engine error_rate rose from ~0.18-0.3% before 13:15Z to 1.85% (13:20Z) and ~4.0-4.2% sustained from 13:25Z onward, consistent with the latency onset window.
  - `F24` ✔ redis-cache exposes exactly these metrics: connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct. There is NO 'latency_p99_ms' metric for redis-cache — cache latency is not directly observable; cpu_pct (F12: flat ~21-22%) is the closest host-health signal.
  - `F1` ✔ pricing-engine's dependency graph: outgoing http to market-data-service, cache to redis-cache; incoming http from order-service (which is called by api-gateway). market-data-service also uses redis-cache (cache) and exchange-gateway (http). redis-cache is flagged shared=true (owner platform) — a shared component used by both pricing-engine and market-data-service.
**Contradicting evidence**
  - `F26` ✔ Zoomed-in 120s buckets for market-data-service confirm no spike at onset: 13:16Z avg 946.42, 13:18Z avg 932.99 (local trough), 13:20Z avg 992.73; the 13:20Z value is within the day's normal band (summary min 909.87, max 1025.69).
  - `F25` ✔ market-data-service inbound request_rate was flat across the whole 13:00-14:30Z window: bucket avgs 948.16 (13:00Z), 975.14 (13:12Z), 939.71 (13:16Z), 979.01 (13:20Z), 942.07 (13:24Z), 984.80 (14:24Z); summary min 858.72, max 1040.42, mean 956.36, missing_points 0. No rise at 13:15-13:20Z.
  - `F27` ✔ pricing-engine request_rate was likewise flat across 13:00-14:30Z: 643.07 (13:00Z), 658.26 (13:12Z), 638.65 (13:16Z), 630.65 (13:20Z), 633.70 (13:24Z); summary min 589.80, max 707.39, mean 643.84, missing_points 0. No inbound surge at 13:15-13:20Z.
**Assumptions**
  - The trigger for the redis-cache-2 PING health-check failure (host, network, or resource) is not determined — redis-cache logs begin at the 13:22:10Z health-check failure.
  - redis-cache exposes no latency_p99_ms metric (F24), so cache-side latency is not directly measurable; the failover -> miss -> rebuild chain is inferred from logs + hit-ratio/memory/client telemetry rather than measured cache latency.
  - The p99 step is observed in the 13:20Z metric bucket and the failover log at 13:22:10Z — bucket granularity means the app-layer bucket may aggregate some pre-failover sub-intervals; the cache-miss WARN burst (13:22:25Z) and hit-ratio collapse (13:22Z) bracket the failover tightly and are causally downstream of it.

### H2: Load-driven saturation at market-data-service (rebuild path) exceeds provisioned capacity — no change needed
The origin is capacity/resource saturation in market-data-service: synchronous snapshot rebuilds from the exchange feed are CPU-expensive and at market-open-scale load the rebuild path saturates market-data-service CPU (~90% sustained), so its p99 rises and pricing-engine's deadline is exceeded. Redis misses appear because market-data-service is hammering a cold/under-sized cache under this load, not because redis failed.

**Causal chain**
1. Load on the quote/rebuild path exceeds market-data-service provisioned capacity at market open *(assumed)*
2. market-data-service inbound request_rate rises at onset, driving the rebuild path *(assumed)*
3. market-data-service CPU saturates (~90% sustained from 13:20Z) and p99 rises to ~1161ms avg [F10]
4. Cache-miss warnings + slow snapshot rebuilds indicate the expensive rebuild path is being exercised heavily [F11]
5. pricing-engine's market-data-service call exceeds deadline; p99 ~2s, error_rate ~4% [F8, F7, F9]

**Supporting evidence**
  - `F10` ✔ market-data-service p99 latency jumped in the same 13:20Z bucket (avg 1160.98ms, max 1979.98ms from ~40ms baseline) and its CPU rose from ~45% (13:15Z) to 72.72% avg / 93.99% max at 13:20Z, then ~90% sustained — the same onset window as pricing-engine.
**Contradicting evidence**
  - `F25` ✔ market-data-service inbound request_rate was flat across the whole 13:00-14:30Z window: bucket avgs 948.16 (13:00Z), 975.14 (13:12Z), 939.71 (13:16Z), 979.01 (13:20Z), 942.07 (13:24Z), 984.80 (14:24Z); summary min 858.72, max 1040.42, mean 956.36, missing_points 0. No rise at 13:15-13:20Z.
  - `F26` ✔ Zoomed-in 120s buckets for market-data-service confirm no spike at onset: 13:16Z avg 946.42, 13:18Z avg 932.99 (local trough), 13:20Z avg 992.73; the 13:20Z value is within the day's normal band (summary min 909.87, max 1025.69).
  - `F27` ✔ pricing-engine request_rate was likewise flat across 13:00-14:30Z: 643.07 (13:00Z), 658.26 (13:12Z), 638.65 (13:16Z), 630.65 (13:20Z), 633.70 (13:24Z); summary min 589.80, max 707.39, mean 643.84, missing_points 0. No inbound surge at 13:15-13:20Z.
  - `F19` ✔ redis-cache hit_ratio_pct was stable at ~94.7-97% through the 13:20:00Z bucket, then COLLAPSED to 10.82% at 13:22:00Z and stayed low (12.28% at 13:24Z, 14.26% at 13:29Z). The 13:00/13:15/13:17/13:20 buckets were all normal (~95-96%).
  - `F20` ✔ redis-cache evictions_per_min did NOT spike at onset: it stayed flat/steady ~2.0-4.9 keys/min across 12:50-14:29Z (13:17Z 3.25, 13:20Z 2.98, 13:23Z 3.49, 13:26Z 2.26), i.e. NO eviction burst. The hit-ratio collapse was not caused by eviction pressure.
  - `F21` ✔ redis-cache memory_pct dropped abruptly from ~71% at 13:20Z to 6.1% at 13:22Z and connected_clients fell from ~295 (13:17Z) to 40.24 at 13:23Z and then recovered to ~310 by 13:26Z. A simultaneous collapse of memory footprint and clients is consistent with an EMPTY dataset/new primary, not a load or eviction event.
  - `F22` ✔ redis-cache logs show an explicit FAILOVER at 13:22:10Z: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' (ERROR 13:22:10Z), then 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' (WARN 13:22:52Z), followed by full resync at 13:28:42Z and a 'client reconnect storm: 15048 new connections in 10s' at 13:30:20Z.
  - `F12` ✔ redis-cache CPU stayed flat at ~21-22% across 13:00-14:55Z (min 20.34, max 24.37), so the cache host itself shows no saturation trend; pricing-engine CPU likewise stayed ~38-41% (39.7 mean), i.e. neither the cache nor pricing-engine compute looks saturated.
  - `F28` ✔ exchange-gateway latency_p99_ms shows no movement at or before onset: 130.38 avg (13:16Z), 130.46 avg (13:20Z), 136.05 avg (13:24Z), against a window summary of min 119.96 / max 143.91 / mean 130.80 — i.e. the 13:20Z bucket sits inside the baseline band.
  - `F29` ✔ exchange-gateway error_rate stayed at baseline ~0.1-0.32% throughout (13:16Z avg 0.21, 13:20Z avg 0.21, 13:24Z avg 0.22; summary min 0.1, max 0.32, mean 0.22), so exchange-gateway itself surfaced no error onset.
  - `F30` ✔ exchange-gateway's upstream (exchange feed) side is also flat: upstream_latency_ms ~114.95-130.39 (13:16Z avg 116.97, 13:20Z avg 117.98) and upstream_error_rate ~0.28-0.33 across the window (13:20Z avg 0.29). No missing points (91/91).
  - `F5` ✘ unverified Historical precedent: HIST-107 (2025-12-09) 'Cold cache storm after redis failover' affected redis-cache + market-data-service with symptoms 'cache miss storm', 'quote latency' — matching the current quote-latency symptom and touching the same shared redis-cache dependency. HIST-113 (2026-01-28) 'pricing-engine CPU saturation at market open' showed 'quote timeouts', 'cpu 99%' but was a capacity issue, not a change.
  - `F6` ✔ Historical incidents are explicitly leads only, not evidence about this incident ('past incidents are leads, not evidence about the current incident'), and prior pricing-engine quote-latency events had different root causes (capacity saturation vs. redis failover vs. bad deployment), so no precedent should be treated as the cause.
**Assumptions**
  - A traffic spike occurred at onset.
  - Cache misses are a symptom of high traffic rather than a cause.
**Rejected because:** Directly contradicted by load telemetry: market-data-service inbound request_rate is flat across onset (13:16Z avg 946.42, 13:18Z trough 932.99, 13:20Z 992.73 — within the day's normal 909-1025 band) (F25, F26), and pricing-engine request_rate is likewise flat (F27). Load did not rise. The cache-layer evidence points the other way from this hypothesis: the hit-ratio collapse is accompanied by a simultaneous memory/client footprint collapse and an EXPLICIT, logged failover to an empty replica (F19, F21, F22), and evictions did NOT spike (F20) — i.e. the misses are key loss, not a hammered under-sized cache. Redis CPU stayed flat (F12) and exchange-side remained healthy (F28-F30), so no load-failure origin is supported.

### H4: Unenumerated change on an unqueried trading-path service (order-service / exchange-gateway / api-gateway) or on pricing-engine itself
The fault originates in a component of the trading path whose change history was never enumerated: order-service, exchange-gateway or api-gateway, or pricing-engine itself, deployed or reconfigured near 13:15-13:20Z. Per-channel change coverage was incomplete, so a change there could be hiding.

**Causal chain**
1. An unenumerated deploy/config change lands on order-service, exchange-gateway, api-gateway or pricing-engine near 13:15-13:20Z *(assumed)*
2. That component adds latency on the market-data call path (exchange-gateway backs market-data-service) [F1]
3. market-data-service p99 + CPU spike and a cache-miss storm appear [F10, F11]
4. pricing-engine deadline-exceeded and screen latency [F8, F7]

**Supporting evidence**
  - `F1` ✔ pricing-engine's dependency graph: outgoing http to market-data-service, cache to redis-cache; incoming http from order-service (which is called by api-gateway). market-data-service also uses redis-cache (cache) and exchange-gateway (http). redis-cache is flagged shared=true (owner platform) — a shared component used by both pricing-engine and market-data-service.
**Contradicting evidence**
  - `F4` ✔ No deployments were found for market-data-service (count 0) or redis-cache (count 0) in 2026-04-07T13:00Z -> 2026-04-09T15:00Z, and no config changes were found for redis-cache (count 0) or across all services (count 0).
  - `F14` ✔ Zero config/feature-flag changes across all services in 2026-04-09T09:00Z-15:00Z (count 0), so no flag flip can be correlated with the 13:15-13:20Z onset.
  - `F15` ✔ No deployments exist for pricing-engine (count 0, 2026-04-08T00:00Z-15:00Z), matching the report's claim 'No releases to pricing today'; no deployments for market-data-service (count 0) or config changes for redis-cache (count 0) in the incident window.
  - `F18` ✔ Ranked shortlist: (1) NO change is temporally tight AND scope-matched — the change record is empty for every component in the blast radius, so no correlating change exists in the data. (2) auth-service dep-5604 is the closest in time (11:30Z) but is a coincidental/low-plausibility candidate: it precedes onset by ~1h45m, auth-service is not a dependency of pricing-engine or market-data-service, and HIST-101-type auth breakage manifests as login failures, not quote latency. (3) The redis-cache cold-cache mechanism (HIST-107 pattern) is a dependency-side risk with NO corresponding recorded change, so it would be an unrecorded infrastructure event, not a deployment.
  - `F22` ✔ redis-cache logs show an explicit FAILOVER at 13:22:10Z: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' (ERROR 13:22:10Z), then 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' (WARN 13:22:52Z), followed by full resync at 13:28:42Z and a 'client reconnect storm: 15048 new connections in 10s' at 13:30:20Z.
  - `F28` ✔ exchange-gateway latency_p99_ms shows no movement at or before onset: 130.38 avg (13:16Z), 130.46 avg (13:20Z), 136.05 avg (13:24Z), against a window summary of min 119.96 / max 143.91 / mean 130.80 — i.e. the 13:20Z bucket sits inside the baseline band.
  - `F29` ✔ exchange-gateway error_rate stayed at baseline ~0.1-0.32% throughout (13:16Z avg 0.21, 13:20Z avg 0.21, 13:24Z avg 0.22; summary min 0.1, max 0.32, mean 0.22), so exchange-gateway itself surfaced no error onset.
  - `F30` ✔ exchange-gateway's upstream (exchange feed) side is also flat: upstream_latency_ms ~114.95-130.39 (13:16Z avg 116.97, 13:20Z avg 117.98) and upstream_error_rate ~0.28-0.33 across the window (13:20Z avg 0.29). No missing points (91/91).
**Assumptions**
  - A change exists but was not captured by the queried change inventory.
  - Such a change could produce an empty-replica redis failover and cache-miss storm, which no code/config change mechanism would naturally cause.
**Rejected because:** Change inventory is empty for every component in the blast radius and zero config/flag changes exist anywhere in the window (F4, F14, F15, F18). Moreover the observed mechanism (empty replica promoted after a PING health-check failure, memory/client footprint collapse) is an infrastructure event with no plausible code/config-change pathway (F22, F21), and exchange-gateway — the adjacent service this hypothesis most depends on — shows flat latency/error and flat upstream feed health across onset (F28, F29, F30).

### H3: auth-service dep-5604 (SameSite=strict session cookie hardening) is the trigger
The only deployment in the 48h window — auth-service v7.2.0 'Session cookie hardening' (SameSite=strict) at 11:30Z — caused the quote-latency degradation via session re-validation or a shared edge/gateway effect.

**Causal chain**
1. auth-service v7.2.0 SameSite=strict deployed 11:30Z [F2, F13]
2. cookie hardening changes session behaviour on the app/edge path *(assumed)*
3. quote rendering path slows *(assumed)*
4. pricing-engine p99 >2s at 13:20Z [F7]

**Supporting evidence**
  - `F2` ✔ Only one deployment exists in the entire 2026-04-07T13:00Z -> 2026-04-09T15:00Z window across ALL services: auth-service v7.2.0 (dep-5604), deployed 2026-04-09T11:30:00Z by ci-bot, summary 'Session cookie hardening', change 'SameSite=strict', status live, never rolled back. The first (partial) query returned the same single record; a retry returned partial:false, confirming completeness.
  - `F13` ✔ The only deployment in the 24-48h window across ALL services is auth-service v7.2.0 (dep-5604), deployed_at 2026-04-09T11:30:00Z, summary 'Session cookie hardening', change 'SameSite=strict', status live, rolled_back_at null. Its timestamp PRECEDES the p99 onset of 13:15-13:20Z by ~1h45-1h50m; it was never rolled back.
**Contradicting evidence**
  - `F3` ✔ auth-service is NOT a dependency of pricing-engine (it does not appear in pricing-engine's dependency graph), so dep-5604 is not directly upstream or downstream of the affected service — but it is the closest change in time to the incident window (2h before the 13:30Z report).
  - `F7` ✔ pricing-engine p99 latency was flat ~58-62ms through 13:15Z, then jumped in the 13:20Z bucket to avg 1370.73ms / max 2281.51ms and stayed elevated (13:25Z avg 2218.07ms), i.e. onset between 13:15Z and 13:20Z.
  - `F8` ✔ pricing-engine's error signature is dominated by 'quote computation deadline exceeded: market-data-service took <n>ms' — 192 ERROR occurrences, first_seen 13:23:57Z — naming market-data-service as the slow call, not generic timeouts or connection errors.
  - `F11` ✔ market-data-service logs show a 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' WARN burst — 289 occurrences, first_seen 13:22:25Z — with rebuild times up to 8748ms, indicating cache misses forcing expensive snapshot rebuilds.
  - `F16` ✔ SCOPE MISMATCH: dep-5604 touches auth-service, which is absent from pricing-engine's dependency graph (outgoing http market-data-service, cache redis-cache; incoming order-service), and market-data-service's dependencies are redis-cache + exchange-gateway. Nothing in the observed blast radius (pricing-engine, market-data-service, redis-cache) changed.
  - `F15` ✔ No deployments exist for pricing-engine (count 0, 2026-04-08T00:00Z-15:00Z), matching the report's claim 'No releases to pricing today'; no deployments for market-data-service (count 0) or config changes for redis-cache (count 0) in the incident window.
  - `F17` ✔ Historical precedent matching the symptom EXACTLY: HIST-107 (2025-12-09) 'Cold cache storm after redis failover' on redis-cache + market-data-service, symptoms 'cache miss storm','quote latency', root cause 'Redis primary failure promoted an empty replica' (match_score 7) — same shared dependency and same cache-miss-storm signature seen at 13:22:25Z. HIST-101 shows auth-service releases CAN cause latency (bad_deployment, rolled back), but that incident's symptoms were login-side. These are leads only, not evidence about INC-006.
  - `F22` ✔ redis-cache logs show an explicit FAILOVER at 13:22:10Z: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' (ERROR 13:22:10Z), then 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' (WARN 13:22:52Z), followed by full resync at 13:28:42Z and a 'client reconnect storm: 15048 new connections in 10s' at 13:30:20Z.
**Rejected because:** Decisively contradicted: (a) SCOPE — auth-service is absent from pricing-engine's and market-data-service's dependency graphs, and every component in the observed blast radius has zero recorded changes (F3, F16, F15); (b) TIMING — precedes onset by ~1h45-1h50m with a flat 58-62ms p99 through 13:15Z (F7, F13); (c) MECHANISM — errors name market-data-service as the slow callee and a redis cache-miss storm, nothing login/session-related (F8, F11), and the cache-layer evidence now shows an explicit redis failover (F22) with no connection to auth.

### H5: Genuine resource exhaustion (memory) on pricing-engine itself
pricing-engine's own memory/resource pressure caused the deadline-exceeded errors and slow quotes, independent of market-data-service.

**Causal chain**
1. pricing-engine memory/CPU pressure *(assumed)*
2. pricing-engine p99 >2s [F7]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ pricing-engine's error signature is dominated by 'quote computation deadline exceeded: market-data-service took <n>ms' — 192 ERROR occurrences, first_seen 13:23:57Z — naming market-data-service as the slow call, not generic timeouts or connection errors.
  - `F10` ✔ market-data-service p99 latency jumped in the same 13:20Z bucket (avg 1160.98ms, max 1979.98ms from ~40ms baseline) and its CPU rose from ~45% (13:15Z) to 72.72% avg / 93.99% max at 13:20Z, then ~90% sustained — the same onset window as pricing-engine.
  - `F11` ✔ market-data-service logs show a 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' WARN burst — 289 occurrences, first_seen 13:22:25Z — with rebuild times up to 8748ms, indicating cache misses forcing expensive snapshot rebuilds.
  - `F12` ✔ redis-cache CPU stayed flat at ~21-22% across 13:00-14:55Z (min 20.34, max 24.37), so the cache host itself shows no saturation trend; pricing-engine CPU likewise stayed ~38-41% (39.7 mean), i.e. neither the cache nor pricing-engine compute looks saturated.
  - `F22` ✔ redis-cache logs show an explicit FAILOVER at 13:22:10Z: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' (ERROR 13:22:10Z), then 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' (WARN 13:22:52Z), followed by full resync at 13:28:42Z and a 'client reconnect storm: 15048 new connections in 10s' at 13:30:20Z.
**Rejected because:** Contradicted by mechanism and metrics: pricing-engine CPU stayed flat ~38-41% with no saturation trend (F12); the error signature explicitly names market-data-service as the slow callee (F8); the same-onset p99/CPU jump and cache-miss storm appear in market-data-service (F10, F11); and an explicit redis failover is logged (F22). A pricing-engine-internal resource cause cannot explain the concurrent redis failover, cache-miss storm, and market-data-service CPU saturation.

## Facts (verified observations)
- redis-cache is a shared component (shared=true) used as cache by both pricing-engine and market-data-service; pricing-engine calls market-data-service over http and market-data-service calls exchange-gateway over http. [F1]
- pricing-engine p99 latency was flat ~58-62ms through 13:15Z, then jumped in the 13:20Z bucket to avg 1370.73ms / max 2281.51ms and stayed elevated (13:25Z avg 2218.07ms). [F7]
- pricing-engine error_rate rose from ~0.18-0.3% before 13:15Z to 1.85% at 13:20Z and ~4.0-4.2% sustained from 13:25Z onward. [F9]
- pricing-engine's dominant error signature is 'quote computation deadline exceeded: market-data-service took <n>ms' (192 ERRORs, first_seen 13:23:57Z), naming market-data-service as the slow callee rather than generic timeouts. [F8]
- market-data-service p99 jumped in the same 13:20Z bucket (avg 1160.98ms, max 1979.98ms from ~40ms baseline) and its CPU rose from ~45% (13:15Z) to 72.72% avg / 93.99% max at 13:20Z, then ~90% sustained. [F10]
- market-data-service logs show a 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' WARN burst (289 occurrences, first_seen 13:22:25Z) with rebuild times up to 8748ms. [F11]
- redis-cache hit_ratio_pct was stable ~94.7-97% through the 13:20Z bucket, then collapsed to 10.82% at 13:22:00Z and stayed low (12.28% at 13:24Z, 14.26% at 13:29Z). [F19]
- redis-cache evictions_per_min did not spike at onset: flat/steady ~2.0-4.9 keys/min across 12:50-14:29Z (13:17Z 3.25, 13:20Z 2.98, 13:23Z 3.49, 13:26Z 2.26), ruling out eviction pressure as the cause of the hit-ratio collapse. [F20]
- redis-cache memory_pct dropped abruptly from ~71% at 13:20Z to 6.1% at 13:22Z and connected_clients fell from ~295 (13:17Z) to 40.24 at 13:23Z, then recovered to ~310 by 13:26Z — an empty-dataset/new-primary signature rather than a load or eviction event. [F21]
- redis-cache logs show an explicit failover: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' (ERROR 13:22:10Z), then 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' (WARN 13:22:52Z), full resync at 13:28:42Z, and a 'client reconnect storm: 15048 new connections in 10s' at 13:30:20Z. [F22]
- market-data-service inbound request_rate was flat across 13:00-14:30Z (bucket avgs 948.16, 975.14, 939.71, 979.01, 942.07, 984.80; min 858.72, max 1040.42, mean 956.36, no missing points) — no rise at onset. [F25]
- Zoomed 120s buckets confirm no market-data-service request spike at onset: 13:16Z avg 946.42, 13:18Z avg 932.99, 13:20Z avg 992.73, the last within the day's normal band (min 909.87, max 1025.69). [F26]
- pricing-engine request_rate was likewise flat across 13:00-14:30Z (643.07, 658.26, 638.65, 630.65, 633.70; min 589.80, max 707.39, mean 643.84) — no inbound surge at onset. [F27]
- exchange-gateway latency_p99_ms showed no movement at or before onset (13:16Z avg 130.38, 13:20Z avg 130.46, 13:24Z avg 136.05; window min 119.96 / max 143.91 / mean 130.80). [F28]
- exchange-gateway error_rate stayed at baseline ~0.1-0.32% throughout (13:16Z 0.21, 13:20Z 0.21, 13:24Z 0.22). [F29]
- exchange-gateway's upstream exchange-feed side is flat: upstream_latency_ms ~114.95-130.39 (13:16Z avg 116.97, 13:20Z avg 117.98) and upstream_error_rate ~0.28-0.33 (13:20Z avg 0.29), 91/91 points present. [F30]
- redis-cache CPU stayed flat at ~21-22% across 13:00-14:55Z (min 20.34, max 24.37) and pricing-engine CPU stayed ~38-41% (mean 39.7); neither the cache host nor pricing-engine compute shows saturation. [F12]
- redis-cache exposes only connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct — there is no latency_p99_ms metric, so cache-side latency is not directly observable. [F24]
- Across the entire 2026-04-07T13:00Z -> 2026-04-09T15:00Z window there is exactly one deployment across ALL services: auth-service v7.2.0 (dep-5604, 'Session cookie hardening', SameSite=strict) at 2026-04-09T11:30:00Z, never rolled back. [F2, F13]
- No deployments exist for market-data-service (count 0) or redis-cache (count 0) and no config changes exist for redis-cache (count 0) or across all services (count 0) in the incident window; no deployments exist for pricing-engine in 2026-04-08T00:00Z-15:00Z (count 0), matching the report that there were no pricing releases today. [F4, F14, F15]

## Assumptions (not verified)
- ASSUMPTION: The trigger for the redis-cache-2 PING health-check failure (host, network, or resource) is not determined; redis-cache logs begin only at the 13:22:10Z health-check failure.
- ASSUMPTION: Because redis-cache exposes no latency_p99_ms metric (F24), cache-side latency is not directly measurable; the failover -> miss -> rebuild -> CPU-saturation -> pricing-engine-deadline chain is inferred from logs plus hit-ratio/memory/client telemetry rather than from measured cache latency.
- ASSUMPTION: The p99 step is observed in the 13:20Z metric bucket while the failover log is at 13:22:10Z; bucket granularity means the app-layer bucket may aggregate some pre-failover sub-intervals. The cache-miss WARN burst (13:22:25Z) and hit-ratio collapse (13:22Z) bracket the failover tightly and are causally downstream of it.
- ASSUMPTION: No unrecorded deployment or config change occurred on order-service, exchange-gateway, api-gateway or pricing-engine; only aggregate plus two per-service change queries were completed, so this is not fully excluded by direct per-service query.
- ASSUMPTION: market-data-service's synchronous rebuild path has no effective stampede/single-flight protection that would have prevented a miss storm from saturating it.
- (demoted from facts: no verified observation backs it) The redis-cache hit-ratio collapse (13:22:00Z bucket) sits in the same onset window as the pricing-engine p99 jump (13:20Z bucket) and the market-data-service cache-miss-storm WARN burst (first_seen 13:22:25Z) — within ~2 minutes.
- (demoted from facts: no verified observation backs it) auth-service is not a dependency of pricing-engine (absent from its dependency graph), and its 11:30Z deployment precedes the 13:15-13:20Z onset by ~1h45-1h50m.

## Timeline
- `2026-04-09T11:30:00Z` Only deployment in the 48h window across all services: auth-service v7.2.0 (dep-5604, 'Session cookie hardening', SameSite=strict), never rolled back; precedes onset by ~1h45m and is not in pricing-engine's dependency graph. [F2, F13, F3]
- `2026-04-09T13:15:00Z` Baseline: pricing-engine p99 flat ~58-62ms; error_rate ~0.18-0.3%; redis-cache hit_ratio ~95% and memory_pct ~71%. [F7, F9, F19, F21]
- `2026-04-09T13:20:00Z` Onset bucket: pricing-engine p99 jumps to avg 1370.73ms / max 2281.51ms; market-data-service p99 jumps to avg 1160.98ms / max 1979.98ms from ~40ms baseline with CPU 45% -> 72.72% avg / 93.99% max; request rates at both services remain flat. [F7, F10, F25, F26, F27]
- `2026-04-09T13:22:00Z` redis-cache hit_ratio_pct collapses 95.9% -> 10.82%; memory_pct drops ~71% -> 6.1%; evictions_per_min stays flat ~2-5/min (not an eviction event). [F19, F21, F20]
- `2026-04-09T13:22:10Z` redis-cache-2 fails its health check (3 consecutive PING timeouts) and is marked down as primary. [F22]
- `2026-04-09T13:22:25Z` market-data-service emits 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot' WARN burst (289 occurrences) with rebuilds up to 8748ms. [F11]
- `2026-04-09T13:22:52Z` Failover promotes replica redis-cache-3 to primary with 'dataset on new primary is empty (replica was resyncing)'. [F22]
- `2026-04-09T13:23:57Z` pricing-engine errors: 'quote computation deadline exceeded: market-data-service took <n>ms', first_seen 13:23:57Z, 192 occurrences. [F8]
- `2026-04-09T13:25:00Z` Sustained degradation: pricing-engine p99 avg 2218.07ms and error_rate ~4.0-4.2%; market-data-service CPU ~90% sustained. exchange-gateway latency/error and upstream feed remain at baseline throughout. [F7, F9, F10, F28, F29, F30]
- `2026-04-09T13:28:42Z` redis-cache full resync of the new primary completes. [F22]
- `2026-04-09T13:30:00Z` Incident reported (INC-006, SEV-2): quotes taking 2-3s across trading screens; pricing-engine deadline-exceeded alerts firing. Redis client reconnect storm (15048 new connections in 10s) logged at 13:30:20Z. [F22]
- `2026-04-09T13:30:00Z` Incident reported by on-call SRE: quotes taking 2-3s across trading screens; pricing-engine deadline-exceeded alerts firing. Redis client reconnect storm (15048 new connections in 10s) logged 20s later at 13:30:20Z. [F22]

## Recommended actions
- **[immediate]** Verify the redis-cache failover and its blast radius now: confirm redis-cache-3 is primary and fully resynced, confirm hit_ratio_pct has recovered to ~95%+ and memory_pct/connected_clients are back to baseline, and confirm market-data-service CPU has returned to ~45% and its cache-miss WARN burst has stopped. If hit ratio is still low, force a controlled full resync/warm-up of the promoted primary before declaring mitigation. -- The identified mechanism is an empty promoted replica (F22) causing misses (F19, F20, F21) and rebuild saturation (F10, F11); mitigation is complete only when the dataset is restored and misses stop. Recovery activity was already partly visible (full resync 13:28:42Z, client reconnect storm 13:30:20Z, F22). [F22, F19, F21, F10, F11]
- **[immediate]** Add stampede/single-flight protection on the market-data-service redis-miss rebuild path (e.g. per-quote-key lock or request coalescing, plus bounded concurrency on exchange-feed snapshot rebuilds) so a cold cache cannot drive synchronous rebuilds to saturate market-data-service. -- The verified failure mode is synchronous per-quote rebuilds (up to 8748ms) driving market-data-service CPU to ~90% and blowing pricing-engine's deadline (F11, F10, F8). Coalescing would decouple cache-miss volume from downstream CPU even while the cache is cold. [F11, F10, F8]
- **[short_term]** Instrument and alarm on redis-cache dataset/failover health: alert on any failover promotion with an empty or non-resynced dataset (the 'dataset on new primary is empty' WARN), and on hit_ratio_pct dropping below a threshold (e.g. <80% for >1 min). Note that redis-cache currently exposes no latency metric, so hit_ratio/memory/clients are the available leading indicators. -- The degradation was detected via downstream p99 and deadline-exceeded errors rather than cache-layer health; the explicit empty-dataset WARN at 13:22:52Z (F22) and hit-ratio collapse to 10.82% (F19) were the earliest unambiguous signals but had no alert. redis-cache has no latency_p99_ms metric (F24). [F22, F19, F21, F24]
- **[short_term]** Harden redis-cache failover semantics: refuse to promote a replica that is mid-resync / has an empty dataset, or require the promoted node to complete resync (or be repopulated from a warm source) before accepting client traffic; review persistence (AOF/RDB) configuration on redis-cache-2/3. -- The proximate fault is promotion of a replica whose dataset was empty because it was resyncing (F22); preventing empty promotion removes the root mechanism entirely rather than mitigating its symptoms. [F22, F21]
- **[short_term]** Determine why redis-cache-2 failed 3 consecutive PING health checks and capture the failing host's node-level metrics/logs (host, network, resource); this is the unexplained trigger above the identified origin. -- Logs begin at the 13:22:10Z health-check failure (F22); the trigger of the primary failure is not determined and is the remaining uncertainty on this root cause. [F22]
- **[short_term]** Close the change-inventory blind spot by enumerating deployments/config changes for order-service, exchange-gateway, api-gateway and pricing-engine individually, and confirm the aggregate 'count 1' result remains stable. -- The rejection of the change-based hypotheses rests on an aggregate all-services query plus two per-service queries, with a first attempt returning partial:true (F2, F4); direct per-service confirmation is needed to close this gap. [F2, F4, F18]
- **[short_term]** Add a downstream-caller observability check (trading screen/app tier and gateway) to quantify the user-visible impact and confirm only the quote path was affected. -- The symptom was reported 'across trading screens' but no downstream caller metrics/logs were retrieved; blast radius beyond pricing-engine and market-data-service is unverified. [F1, F7]
- **[long_term]** Rehearse/test the redis failover path with a mid-resync replica in a game-day, and ensure warm-up/repopulation of quote keys (or a secondary cache tier) is part of the runbook so a failover cannot present an empty dataset to market-data-service. -- This is a reproduction of HIST-107 ('Redis primary failure promoted an empty replica') on the same shared redis-cache dependency (F17, F5), so without a structural fix the same class of incident is expected to recur. [F17, F5, F22]

## Data gaps
- The trigger of the redis-cache-2 PING health-check failure is unknown: redis-cache logs begin at the 13:22:10Z failure, so host/network/resource cause of the primary failure could not be determined (limits conclusions about preventing the next occurrence, not about the mechanism).
- redis-cache exposes no latency_p99_ms metric (only connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct), so cache-side latency during the failover could not be quantified; the miss -> rebuild chain is established from logs and hit-ratio/memory/client telemetry rather than measured cache latency.
- Deployments/config changes were not enumerated per-service for order-service, exchange-gateway, api-gateway or pricing-engine individually (only an aggregate all-services query, which returned partial:true on first attempt but partial:false with count 1 on retry, plus two per-service queries). A change on an unqueried trading-path service therefore cannot be fully ruled out.
- pricing-engine's own memory_pct and request_rate queries returned budget_exhausted, so a coincident pricing-engine-internal memory or traffic anomaly is not directly excluded (though F12 shows flat pricing-engine CPU and F27 shows flat request rate from a separate query).
- No logs or metrics were pulled for downstream callers of pricing-engine (trading screens / app tier / gateway), so the full blast radius of the slow quotes is unverified.
- exchange-gateway request_rate was only available at 5-minute bucket granularity, so a sub-5-minute dip or stall in calls into exchange-gateway around 13:15-13:20Z could have been smoothed away; no cache hit-ratio or upstream-freshness metric exists for exchange-gateway or market-data-service.

## What would change this conclusion
- Evidence that market-data-service's cache-miss storm was caused by key loss from eviction or expiry rather than an empty promoted dataset: e.g. a redis-cache eviction/expiry burst at 13:20-13:22Z (currently falsified — evictions stayed flat ~2-5/min, F20), or an explicit expire/TTL purge log rather than the 'dataset on new primary is empty' failover log (F22).
- Direct measurement showing redis-cache remained serving the quote keys during 13:20-13:30Z (e.g. hit_ratio_pct staying ~95% at 13:22Z), which would implicate a market-data-service-internal or pricing-engine-internal cause instead.
- A verified deployment or config change on a component in the observed blast radius (pricing-engine, market-data-service, redis-cache) or on the trading path timestamped 13:15-13:20Z, which would re-open the change-triggered hypotheses (H3/H4).
- A documented request-rate surge at market-data-service or pricing-engine at 13:15-13:20Z, which would support the load/capacity origin (H2) instead of the cache-origin (currently falsified by flat request_rate, F25/F26/F27).
- Evidence of an exchange-feed or exchange-gateway stall around onset (rising upstream latency/error or a request-rate dip), which would move the origin upstream of redis-cache (currently falsified by flat exchange-gateway and upstream metrics, F28/F29/F30).

## Integrity warnings (set by code)
- fact demoted to assumption: The redis-cache hit-ratio collapse (13:22:00Z bucket) sits in the same onset win
- fact demoted to assumption: auth-service is not a dependency of pricing-engine (absent from its dependency g

## Investigation stats
- latency 175.51s, LLM calls 23, tokens 183260 (in 150922 / out 32338)
- tool calls 38 (0 failed, 1 partial, 3 recovered by retry), findings 29/30 verified
