# Incident investigation report: INC-006
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Quote latency degradation across trading screens (pricing-engine p99 >2s) starting ~13:20 originates in the shared redis-cache, NOT in pricing-engine or the external exchange feed. At 13:22:10 the redis-cache primary (redis-cache-2) failed its health check and was marked down; failover promoted replica redis-cache-3 whose dataset was empty because it was still resyncing. The quote cache was therefore wiped (hit_ratio 96%→16%, memory_pct 70%→9%, evictions flat). market-data-service, which serves quotes to pricing-engine, then had to rebuild every quote from the exchange feed snapshot ('cache miss storm'), spiking its CPU to ~94%, p99 to ~1.8s and error_rate to ~3%. pricing-engine's synchronous calls to market-data-service exceeded their deadline (192 'deadline exceeded' errors), producing the visible 2–3s load times. No deployments or config changes to the involved services; exchange-gateway (external feed) metrics flat throughout. This matches historical incident HIST-107 (cold cache storm after redis failover). Confidence 0.9.

## Probable root cause
**redis-cache primary failure → failover to empty replica → cold cache miss storm → market-data-service overload → pricing-engine quote deadline exceeded** -- infrastructure_failure in `redis-cache` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: redis-cache primary failure → failover to empty replica → cold cache miss storm → market-data-service overload → pricing-engine quote deadline exceeded | infrastructure_failure | redis-cache | - | 0.90 (high) | active |
| 2 | H2: External exchange feed / exchange-gateway degradation slowed quote rebuilds | dependency_failure | exchange-gateway | - | 0.05 (low) | rejected |
| 3 | H3: A bad deployment introduced the fault | bad_deployment | pricing-engine | - | 0.03 (low) | rejected |
| 4 | H4: Redis resource exhaustion (memory pressure / evictions) | resource_exhaustion | redis-cache | - | 0.03 (low) | rejected |

### H1: redis-cache primary failure → failover to empty replica → cold cache miss storm → market-data-service overload → pricing-engine quote deadline exceeded
At 13:22:10 the redis-cache primary (redis-cache-2) failed health checks (3 consecutive PING timeouts) and was marked down. Failover promoted replica redis-cache-3, whose dataset was empty because it was still resyncing. All cached quote keys vanished (hit_ratio 96%→16%, memory 70%→9%). market-data-service then had to rebuild every quote from the exchange feed snapshot (rebuilds of 1–8s), driving its CPU to ~94%, p99 to ~1.8s and error_rate to ~3%. pricing-engine, which calls market-data-service synchronously for quotes, exceeded its deadline (192 'deadline exceeded' errors), producing the 2–3s quote latency. The fault originates in the redis-cache infrastructure, not in pricing-engine.

**Causal chain**
1. redis-cache-2 primary fails health check at 13:22:10 [F7]
2. failover promotes empty replica at 13:22:52; dataset lost [F7, F5, F6]
3. market-data-service cache misses force rebuild from exchange feed (cache miss storm from 13:22:25) [F4]
4. market-data-service CPU/p99/error spike [F3, F11]
5. pricing-engine quote calls to market-data-service exceed deadline [F2]
6. user-visible 2-3s quote latency on trading screens [F1]

**Supporting evidence**
  - `F1` ✔ pricing-engine p99 latency jumped from ~60ms to 1794ms in the 13:20 bucket, peaked 2309ms, and was still elevated (1171ms) at 14:50.
  - `F2` ✔ pricing-engine ERROR logs blame the downstream market-data-service: 192 occurrences of 'quote computation deadline exceeded: market-data-service took <n>ms', first seen 13:23:57.
  - `F3` ✔ market-data-service p99 latency simultaneously jumped from ~40ms to 1535ms in the 13:20 bucket, and its CPU rose from ~45% to a peak of 94.5% in the 13:20 bucket.
  - `F4` ✔ market-data-service logs a 'cache miss storm' (289 occurrences) rebuilding quotes from the exchange feed snapshot because keys are 'not in redis-cache', first seen 13:22:25.
  - `F5` ✔ redis-cache hit_ratio_pct collapsed from ~96% to 15.9% in the 13:30 bucket (29.06 avg in the 13:20 bucket), recovering only gradually to 46% by 14:50.
  - `F6` ✔ redis-cache memory_pct dropped from ~70% to 9.3% in the 13:30 bucket, consistent with the dataset being emptied (lost keys), while CPU stayed flat ~22% and evictions_per_min stayed flat ~3/min.
  - `F7` ✔ redis-cache logs show a primary failure and failover to an empty replica: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' at 13:22:10 followed by 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' at 13:22:52.
  - `F8` ✔ redis-cache logged client reconnect storms after the failover: '15048 new connections in 10s' at 13:30:20 up to '92851 new connections in 10s' at 13:40:15, and connected_clients dipped to a 37.15 min in the 13:20 bucket.
  - `F11` ✔ market-data-service error_rate rose from ~0.2% to ~3.2% at 13:20, and market-data-service request_rate stayed flat (~950 req/s), ruling out a traffic surge.
  - `F12` ✔ A prior incident HIST-107 ('Cold cache storm after redis failover', 2025-12-09) had the same services and symptoms with root cause 'Redis primary failure promoted an empty replica'.
**Contradicting evidence**
  - (none)
**Assumptions**
  - redis-cache-2 was the primary serving the quote: keys used by market-data-service
  - the promoted replica's empty dataset is why hit ratio collapsed rather than eviction

### H2: External exchange feed / exchange-gateway degradation slowed quote rebuilds
The exchange feed provider behind exchange-gateway degraded, slowing the snapshot rebuilds market-data-service performs on cache misses and thus causing the quote latency.

**Causal chain**
1. exchange feed latency rises *(assumed)*
2. market-data-service rebuilds slow [F4]
3. pricing-engine deadline exceeded [F2]

**Supporting evidence**
  - `F4` ✔ market-data-service logs a 'cache miss storm' (289 occurrences) rebuilding quotes from the exchange feed snapshot because keys are 'not in redis-cache', first seen 13:22:25.
**Contradicting evidence**
  - `F9` ✔ exchange-gateway (the external exchange feed provider) showed NO degradation: p99 flat ~130ms and upstream_latency_ms flat ~120ms across the whole window, with error_rate flat ~0.21%.
  - `F4` ✔ market-data-service logs a 'cache miss storm' (289 occurrences) rebuilding quotes from the exchange feed snapshot because keys are 'not in redis-cache', first seen 13:22:25.
  - `F7` ✔ redis-cache logs show a primary failure and failover to an empty replica: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' at 13:22:10 followed by 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' at 13:22:52.
**Assumptions**
  - the exchange feed would show latency growth on exchange-gateway metrics
**Rejected because:** exchange-gateway p99 (~130ms), upstream_latency_ms (~120ms) and error_rate (~0.2%) are flat across the entire window (F9); the feed provider did not degrade. The rebuild slowness is explained by the redis cache being emptied (F7), not by the feed.

### H3: A bad deployment introduced the fault
A code release to one of the involved services caused the latency regression.

**Causal chain**
1. release introduces slow code *(assumed)*
2. quote latency rises [F1]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ No code deployments to pricing-engine, market-data-service, or redis-cache in the incident window; the only deployment found (dep-5604, auth-service v7.2.0, 11:30) is unrelated. No config/flag changes were found for any service.
  - `F7` ✔ redis-cache logs show a primary failure and failover to an empty replica: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' at 13:22:10 followed by 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' at 13:22:52.
**Assumptions**
  - a relevant release exists
**Rejected because:** No deployments to pricing-engine, market-data-service or redis-cache were found in the window (F10); onset coincides with the redis primary failure instead (F7).

### H4: Redis resource exhaustion (memory pressure / evictions)
redis-cache ran out of memory and evicted the quote keys, causing the miss storm.

**Causal chain**
1. memory pressure evicts keys *(assumed)*
2. hit ratio collapses [F5]

**Supporting evidence**
  - `F5` ✔ redis-cache hit_ratio_pct collapsed from ~96% to 15.9% in the 13:30 bucket (29.06 avg in the 13:20 bucket), recovering only gradually to 46% by 14:50.
  - `F6` ✔ redis-cache memory_pct dropped from ~70% to 9.3% in the 13:30 bucket, consistent with the dataset being emptied (lost keys), while CPU stayed flat ~22% and evictions_per_min stayed flat ~3/min.
**Contradicting evidence**
  - `F6` ✔ redis-cache memory_pct dropped from ~70% to 9.3% in the 13:30 bucket, consistent with the dataset being emptied (lost keys), while CPU stayed flat ~22% and evictions_per_min stayed flat ~3/min.
  - `F7` ✔ redis-cache logs show a primary failure and failover to an empty replica: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' at 13:22:10 followed by 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' at 13:22:52.
**Assumptions**
  - evictions would rise before a hit-ratio collapse
**Rejected because:** evictions_per_min stayed flat ~3/min and memory DROPPED (70%→9%) rather than rising (F6); the logs directly show a primary failure and empty-replica failover (F7), not eviction pressure.

## Facts (verified observations)
- pricing-engine p99 jumped from ~60ms to 1794ms at 13:20 and peaked at 2309ms. [F1]
- pricing-engine errors attribute the timeout to market-data-service ('quote computation deadline exceeded: market-data-service took ...'). [F2]
- market-data-service p99 and CPU both spiked at 13:20 (p99 1535ms, CPU peak 94.5%). [F3]
- market-data-service logged a cache miss storm (keys not in redis-cache) rebuilding from the exchange snapshot from 13:22:25. [F4]
- redis-cache hit_ratio collapsed from ~96% to 15.9%, and memory_pct dropped from ~70% to 9.3%, while evictions stayed flat ~3/min. [F5, F6]
- redis-cache logs record a primary health-check failure at 13:22:10 and a failover to an empty replica ('dataset on new primary is empty') at 13:22:52. [F7]
- exchange-gateway (external feed) latency, upstream latency and error rate stayed flat across the incident window. [F9]
- No deployments to pricing-engine, market-data-service or redis-cache, and no config changes, occurred in the window. [F10]
- market-data-service request_rate stayed flat ~950 req/s, ruling out a traffic surge. [F11]

## Assumptions (not verified)
- The quote: keys in redis-cache are the ones market-data-service reads for pricing-engine quotes (the log lines name those keys and the dependency graph shows both services use redis-cache).
- The 13:20 metric bucket boundary obscures whether the failure preceded the latency spike by seconds; both are within the same bucket.

## Timeline
- `2026-04-09T13:20:00Z` pricing-engine and market-data-service p99 latency spike; redis-cache memory/hit-ratio start collapsing [F1, F3, F5, F6]
- `2026-04-09T13:22:10Z` redis-cache-2 primary fails health check (3 consecutive PING timeouts), marked down [F7]
- `2026-04-09T13:22:25Z` market-data-service begins 'cache miss storm', rebuilding quotes not found in redis-cache [F4]
- `2026-04-09T13:22:52Z` redis-cache failover promotes redis-cache-3; new primary dataset empty [F7]
- `2026-04-09T13:23:57Z` pricing-engine begins emitting 'quote computation deadline exceeded: market-data-service took ...' errors [F2]
- `2026-04-09T13:30:20Z` redis-cache client reconnect storm (15048 new connections in 10s), escalating to 92851 by 13:40:15 [F8]
- `2026-04-09T13:30:00Z` Incident reported (INC-006) as quote latency degraded [F1]
- `2026-04-09T14:50:00Z` Latency still elevated (pricing-engine p99 ~1171ms); redis hit ratio recovering slowly to ~46%; cache still cold [F1, F5]

## Recommended actions
- **[immediate]** Trigger/accelerate cache warming of quote keys on the new redis primary, or fail back to a properly synced primary once replication is caught up. -- The new primary is empty; rebuild-on-miss is what keeps latency high. Warming restores hit ratio and clears the miss storm. [F4, F5, F7]
- **[short_term]** Verify redis-cache-2's failure cause and replication health; confirm full resync of redis-cache-2 completes and replica sync checks are fixed. -- The failover promoted a replica that was mid-resync; the same HIST-107 pattern recurs, so replica sync checks need fixing. [F7, F12]
- **[short_term]** Add alerting on redis hit_ratio collapse and on market-data-service cache-miss rebuild rate to detect cold-cache events earlier. -- The onset was detectable via hit_ratio 96%→16% and the miss-storm log pattern before the user-facing threshold was crossed. [F4, F5]
- **[long_term]** Add a pre-failover guard / replica-sync check so failover does not promote an empty/mid-resync replica; add cache-warming on failover. -- Prevents cold-cache storms from empty-replica promotion, the documented root cause of HIST-107 and this incident. [F7, F12]
- **[long_term]** Consider bounding quote rebuild concurrency / serving stale quotes during cache-cold periods to protect market-data-service from CPU saturation. -- Rebuild fan-out drove CPU to ~94% and amplified latency; a stale-serve or concurrency cap would dampen the storm. [F3, F4]

## Data gaps
- get_deployments for all services returned partial=true (one backend shard did not respond); a relevant deployment outside the missing shard cannot be fully excluded, though the redis-cache logs give a direct non-deployment cause.
- redis-cache exposes no 'restarts' metric or restart log lines, so whether the primary redis-cache-2 process crashed vs. was partitioned is not directly observable; only PING timeouts and an empty-replica failover are logged.
- No metric for cache-miss rebuild rate is available, so the exact number of affected quotes is estimated from log counts (289 miss-storm lines vs. 192 pricing errors).

## Investigation stats
- latency 71.35s, LLM calls 6, tokens 95649 (in 86969 / out 8680)
- tool calls 28 (2 failed, 1 partial, 4 recovered by retry), findings 12/12 verified
