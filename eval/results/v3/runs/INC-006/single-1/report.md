# Incident investigation report: INC-006
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Quote latency degradation originated in redis-cache, not pricing-engine. At 13:22:10Z the redis-cache primary (redis-cache-2) failed PING health checks and was marked down; failover at 13:22:52Z promoted replica redis-cache-3, whose dataset was empty because it was still resyncing. The shared cache emptied (memory 70%->9%, hit_ratio 96%->16%), so market-data-service missed on nearly every quote lookup and had to rebuild each quote from the exchange feed, driving its CPU to ~94% and latency to ~2s (cache-miss-storm warnings started 13:22:25Z). pricing-engine, which calls market-data-service, then blew its quote-computation deadline (2-8s), producing p99 ~2.3s and the slow quotes in the app. Ruled out: pricing-engine capacity saturation (its CPU stayed ~40%), external exchange degradation (exchange-gateway flat ~130ms/0.2%), an eviction/OOM storm (evictions ~3/min, redis CPU flat ~22%), and any deployment/config change (none for redis/market-data/pricing; only an unrelated auth-service deploy at 11:30). This matches historical incident HIST-107.

## Probable root cause
**redis-cache primary failure promoted an empty replica, causing a cold-cache storm that overloaded market-data-service and cascaded into pricing-engine** -- infrastructure_failure in `redis-cache` (confidence 0.82, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: redis-cache primary failure promoted an empty replica, causing a cold-cache storm that overloaded market-data-service and cascaded into pricing-engine | infrastructure_failure | redis-cache | - | 0.82 (high) | active |
| 2 | H2: market-data-service resource exhaustion / self-induced overload (CPU saturation) as the root fault | resource_exhaustion | market-data-service | - | 0.12 (low) | rejected |
| 3 | H3: pricing-engine capacity saturation at market activity spike | capacity_saturation | pricing-engine | - | 0.05 (low) | rejected |
| 4 | H4: External exchange provider / dependency degradation | dependency_failure | exchange-gateway | - | 0.05 (low) | rejected |

### H1: redis-cache primary failure promoted an empty replica, causing a cold-cache storm that overloaded market-data-service and cascaded into pricing-engine
At 13:22:10Z the redis-cache primary (redis-cache-2) failed its PING health checks and was marked down. Automatic failover at 13:22:52Z promoted replica redis-cache-3, but that replica's dataset was empty because it was still resyncing. With the shared cache emptied (memory 70%->9%, hit ratio 96%->16%), market-data-service missed on essentially every quote lookup and had to rebuild each quote from the exchange feed snapshot, driving its CPU to ~94% and its latency to ~2s. pricing-engine, which calls market-data-service, then exceeded its quote-computation deadline (2-8s), surfacing as 2-3s slow quotes in the app.

**Causal chain**
1. redis-cache-2 primary fails PING health checks at 13:22:10Z and is marked down [F4]
2. Failover promotes empty, still-resyncing replica redis-cache-3 at 13:22:52Z [F5]
3. Cache dataset emptied: memory 70%->9%, hit_ratio 96%->16% [F6, F7]
4. market-data-service cache-miss storm begins 13:22:25Z, rebuilding quotes from exchange feed [F8]
5. market-data-service CPU 45%->94% and latency 40ms->2s, error_rate ->3.2% [F9, F10]
6. pricing-engine deadline-exceeded on market-data-service calls; p99 60ms->2.3s [F2, F1]

**Supporting evidence**
  - `F1` ✔ pricing-engine p99 latency jumped from ~60ms (13:10 bucket) to ~2.1s at the 13:30 bucket, an onset in the 13:20 bucket, and stayed elevated (gradually declining) for the rest of the window.
  - `F2` ✔ pricing-engine logs show 192 errors of the pattern 'quote computation deadline exceeded: market-data-service took <n>ms', first seen 13:23:57Z, with values ranging hundreds to ~8456ms.
  - `F4` ✔ redis-cache-2 failed health checks ('3 consecutive PING timeouts') and was marked primary-down at 13:22:10Z.
  - `F5` ✔ At 13:22:52Z redis failover promoted replica redis-cache-3 to primary, and the promoted node's dataset was empty because it was still resyncing.
  - `F6` ✔ redis-cache memory_pct collapsed from ~70% (13:10 bucket, max 74.39) to ~9-10% at the 13:30 bucket, consistent with an emptied dataset after the empty-replica promotion.
  - `F7` ✔ redis-cache hit_ratio_pct fell from ~96% (13:10 bucket) to 15.9% at the 13:30 bucket, confirming the cache emptied and was serving misses.
  - `F8` ✔ market-data-service logs show 289 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot' warnings, first seen 13:22:25Z — starting ~15s after the redis failover.
  - `F9` ✔ market-data-service CPU rose from ~45% (13:10 bucket) to ~82-88% (peaking max 94.51) at the 13:20 bucket onward, and its latency_p99 rose from ~40ms to ~1.5-1.8s over the same interval.
  - `F10` ✔ market-data-service error_rate rose from ~0.2% (13:10 bucket) to ~3.2% at 13:20 onward, while request_rate stayed flat (~950 req/s) and restarts stayed 0.
  - `F14` ✔ redis-cache logged a client reconnect storm ('29623 new connections in 10s') at 13:35:30Z, consistent with clients (market-data-service) reconnecting after the failover.
  - `F15` ✔ Historical incident HIST-107 ('Cold cache storm after redis failover') matches this signature exactly: redis primary failure promoted an empty replica, causing cache miss storm and quote latency.
**Contradicting evidence**
  - `F3` ✔ pricing-engine's own CPU was flat ~40% throughout (13:20 bucket avg 40.01, 13:30 bucket avg 39.8), and it logged healthy connection pools and health checks, so pricing-engine itself is not saturated.
  - `F11` ✔ exchange-gateway (market-data-service's downstream market feed) stayed healthy: latency_p99 flat ~130ms and error_rate flat ~0.2% across the whole window.
  - `F12` ✔ redis-cache evictions_per_min stayed flat at ~3/min (13:20 bucket avg 2.91) and redis-cache cpu_pct stayed flat ~22%, ruling out an eviction storm or redis CPU saturation.
  - `F13` ✔ No deployments or config changes occurred for redis-cache, market-data-service, or pricing-engine in the 06:00-14:59 window; the only deploy anywhere was auth-service v7.2.0 at 11:30Z.
**Assumptions**
  - redis-cache-2's failure was an unplanned node/infra fault rather than a planned maintenance action
  - market-data-service reads quotes from redis-cache on the hot path such that a cache miss forces a slow exchange-feed rebuild
  - the timing coincidence (redis failover 13:22:52 vs market-data cache-miss onset 13:22:25) reflects the failover making the primary unreachable slightly before the promotion completed

### H2: market-data-service resource exhaustion / self-induced overload (CPU saturation) as the root fault
market-data-service itself saturated (CPU peaking 94%), throttling its own processing and causing both the cache-miss backlog and the downstream quote deadlines, with redis-cache emptiness being a secondary symptom.

**Causal chain**
1. market-data-service CPU climbs to 94% [F9]
2. CPU saturation slows request handling, producing cache-miss rebuild lag and 2s latency [F8, F9]
3. pricing-engine deadline exceeded on the slow downstream [F2]

**Supporting evidence**
  - `F8` ✔ market-data-service logs show 289 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot' warnings, first seen 13:22:25Z — starting ~15s after the redis failover.
  - `F9` ✔ market-data-service CPU rose from ~45% (13:10 bucket) to ~82-88% (peaking max 94.51) at the 13:20 bucket onward, and its latency_p99 rose from ~40ms to ~1.5-1.8s over the same interval.
  - `F10` ✔ market-data-service error_rate rose from ~0.2% (13:10 bucket) to ~3.2% at 13:20 onward, while request_rate stayed flat (~950 req/s) and restarts stayed 0.
**Contradicting evidence**
  - `F4` ✔ redis-cache-2 failed health checks ('3 consecutive PING timeouts') and was marked primary-down at 13:22:10Z.
  - `F5` ✔ At 13:22:52Z redis failover promoted replica redis-cache-3 to primary, and the promoted node's dataset was empty because it was still resyncing.
  - `F6` ✔ redis-cache memory_pct collapsed from ~70% (13:10 bucket, max 74.39) to ~9-10% at the 13:30 bucket, consistent with an emptied dataset after the empty-replica promotion.
  - `F7` ✔ redis-cache hit_ratio_pct fell from ~96% (13:10 bucket) to 15.9% at the 13:30 bucket, confirming the cache emptied and was serving misses.
  - `F12` ✔ redis-cache evictions_per_min stayed flat at ~3/min (13:20 bucket avg 2.91) and redis-cache cpu_pct stayed flat ~22%, ruling out an eviction storm or redis CPU saturation.
**Assumptions**
  - CPU saturation could be primary rather than a consequence of rebuilding an emptied cache
**Rejected because:** market-data-service CPU/error rise and cache-miss storm both begin at ~13:22, immediately AFTER the redis primary failure (13:22:10) and empty-replica promotion (13:22:52), and the log message explicitly names redis-cache as the missing store. The elevated work is rebuilding quotes the emptied cache can no longer serve, so CPU saturation is an effect, not the origin.

### H3: pricing-engine capacity saturation at market activity spike
A surge in quote demand overwhelmed provisioned pricing-engine capacity, causing its p99 latency and deadline-exceeded alerts.

**Causal chain**
1. traffic surge overruns pricing-engine *(assumed)*
2. p99 exceeds 2s [F1]

**Supporting evidence**
  - `F1` ✔ pricing-engine p99 latency jumped from ~60ms (13:10 bucket) to ~2.1s at the 13:30 bucket, an onset in the 13:20 bucket, and stayed elevated (gradually declining) for the rest of the window.
**Contradicting evidence**
  - `F3` ✔ pricing-engine's own CPU was flat ~40% throughout (13:20 bucket avg 40.01, 13:30 bucket avg 39.8), and it logged healthy connection pools and health checks, so pricing-engine itself is not saturated.
  - `F2` ✔ pricing-engine logs show 192 errors of the pattern 'quote computation deadline exceeded: market-data-service took <n>ms', first seen 13:23:57Z, with values ranging hundreds to ~8456ms.
**Assumptions**
  - request volume rose materially at onset
**Rejected because:** pricing-engine CPU stayed flat at ~40% and it served healthy batches; its errors explicitly blame the downstream market-data-service call, not its own load. market-data-service request_rate was also flat (~950 req/s).

### H4: External exchange provider / dependency degradation
exchange-gateway's upstream market feed degraded, slowing quote rebuilds and cascading to pricing-engine.

**Causal chain**
1. exchange feed slows *(assumed)*
2. quote rebuilds slow, cache misses expensive [F8]

**Supporting evidence**
  - `F8` ✔ market-data-service logs show 289 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot' warnings, first seen 13:22:25Z — starting ~15s after the redis failover.
**Contradicting evidence**
  - `F11` ✔ exchange-gateway (market-data-service's downstream market feed) stayed healthy: latency_p99 flat ~130ms and error_rate flat ~0.2% across the whole window.
  - `F4` ✔ redis-cache-2 failed health checks ('3 consecutive PING timeouts') and was marked primary-down at 13:22:10Z.
  - `F5` ✔ At 13:22:52Z redis failover promoted replica redis-cache-3 to primary, and the promoted node's dataset was empty because it was still resyncing.
**Assumptions**
  - upstream feed latency could contribute
**Rejected because:** exchange-gateway latency (~130ms) and error_rate (~0.2%) are flat across the entire window; the fault window aligns instead with the redis failover.

## Facts (verified observations)
- redis-cache primary redis-cache-2 failed health checks and was marked down at 13:22:10Z, then failover promoted an empty, still-resyncing replica at 13:22:52Z. [F4, F5]
- redis-cache dataset emptied at onset: memory fell ~70%->9% and hit ratio ~96%->16%. [F6, F7]
- market-data-service emitted a cache-miss storm (289 warnings, rebuilding quotes from exchange feed) beginning 13:22:25Z, with CPU rising to ~94% and latency to ~2s. [F8, F9]
- pricing-engine p99 rose from ~60ms to ~2.3s and logged 192 'quote computation deadline exceeded: market-data-service took Nms' errors first at 13:23:57Z, while its own CPU stayed ~40%. [F1, F2, F3]
- exchange-gateway (the market feed) remained healthy with flat ~130ms latency and ~0.2% error rate; redis evictions and CPU stayed flat, ruling out those faults. [F11, F12]
- No deployments or config changes touched redis-cache, market-data-service, or pricing-engine in the window; the only deploy was auth-service v7.2.0 at 11:30Z. [F13]
- Past incident HIST-107 documents the identical signature (redis primary failure promoting an empty replica causing a cache-miss storm and quote latency). [F15]

## Assumptions (not verified)
- redis-cache-2's failure was an unplanned node/infrastructure fault, not a deliberate maintenance drain (no maintenance/deploy record found).
- market-data-service reads quotes from redis-cache on the hot path, so an emptied cache forces slow exchange-feed rebuilds.
- The ~27s gap between the cache-miss onset (13:22:25) and the promotion completion (13:22:52) reflects the primary being unreachable before the new primary finished taking over.

## Timeline
- `2026-04-09T11:30:00Z` Unrelated deploy: auth-service v7.2.0 (session cookie hardening) — no bearing on quote path. [F13]
- `2026-04-09T13:22:10Z` redis-cache-2 primary fails PING health checks (3 consecutive timeouts); marked down. [F4]
- `2026-04-09T13:22:25Z` market-data-service begins cache-miss storm, rebuilding quotes from exchange feed snapshot. [F8]
- `2026-04-09T13:22:52Z` Failover promotes empty replica redis-cache-3 to primary. [F5]
- `2026-04-09T13:23:57Z` First pricing-engine 'quote computation deadline exceeded' error (market-data-service took 5171ms). [F2]
- `2026-04-09T13:28:42Z` MASTER<->REPLICA full resync begins for redis-cache-2 as it rejoins. [F5]
- `2026-04-09T13:30:20Z` redis-cache client reconnect storm (15048 new connections in 10s); later 29623 in 10s at 13:35:30Z. [F14]
- `2026-04-09T13:30:00Z` pricing-engine p99 peaks ~2.1-2.3s; market-data-service CPU ~88%, latency ~1.8s. [F1, F9]
- `2026-04-09T13:30:00Z` Incident reported by on-call SRE. []
- `2026-04-09T14:50:00Z` Partial recovery under way: pricing-engine p99 ~1.17s, redis memory ~30%, hit ratio ~46% as cache warms. [F1, F6, F7]

## Recommended actions
- **[immediate]** Warm the redis-cache (preload hot quotes) and confirm full replication sync has completed so the cluster returns to steady state. -- hit_ratio recovered only to ~46% and memory to ~30% by 14:50, so cache warmth is still incomplete; warming restores latency faster. [F6, F7, F1]
- **[short_term]** Fix redis replica-sync / failover checks so a replica that is still full-resyncing cannot be promoted to primary (e.g. promote only fully-synced replicas, or add a sync-completion gate). -- The promotion of an empty, resyncing replica is the direct fault; HIST-107's listed resolution was exactly 'fixed replica sync checks'. [F5, F15]
- **[short_term]** Add protection/fallback for market-data-service so an emptied shared cache does not translate into a rebuild storm (request coalescing, local per-node cache, and/or rate-limited exchange-feed rebuilds). -- The cache-miss storm drove market-data-service CPU to ~94% and rebuilt every quote from the exchange feed, amplifying impact far beyond the cache outage itself. [F8, F9]
- **[long_term]** Add alerting on redis-cache hit_ratio and memory collapse plus on failover events, to detect empty-replica promotions before quote latency is user-visible. -- The failure was only noticed after pricing-engine p99 breached 2s; a direct cache-health signal would catch it at 13:22. [F6, F7, F1]
- **[long_term]** Determine why redis-cache-2 primary failed its PING health checks (node/host-level investigation) to prevent recurrence. -- Root trigger is the primary's unplanned failure; the fault origin is redis-cache infrastructure and its underlying cause is not visible in the available telemetry. [F4]

## Data gaps
- No node/host-level telemetry for redis-cache-2 to explain WHY the primary's PING checks timed out (disk, network, OOM at the host, or process crash) — the immediate trigger cause is inferred only from symptom.
- redis-cache available metrics did not include replication lag / sync status, so the exact resync state of redis-cache-3 at promotion time is only stated in the log line, not corroborated by a metric.
- No trace-level data was available to count how many pricing-engine quote requests failed vs. merely slowed, so the user-facing error magnitude is approximated from log error counts.
- The all-services deployment list was initially marked partial (tc-003); after retry it returned a single auth-service deploy, but if a deployment shard remained unresponsive a redis/market-data change could theoretically be unobserved (config-change query was complete and empty, partially mitigating this).

## Investigation stats
- latency 63.32s, LLM calls 6, tokens 89524 (in 79617 / out 9907)
- tool calls 29 (0 failed, 1 partial, 4 recovered by retry), findings 15/15 verified
