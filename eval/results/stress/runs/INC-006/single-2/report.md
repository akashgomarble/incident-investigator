# Incident investigation report: INC-006
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Quote latency degradation (p99 ~2-3s) in pricing-engine beginning ~13:20Z originated in redis-cache, not in pricing-engine. At 13:22:10Z the redis-cache primary (redis-cache-2) failed its health check ('3 consecutive PING timeouts; marking primary as down') and failover promoted an empty, resyncing replica (redis-cache-3). The dataset loss is corroborated by redis memory_pct dropping from ~70% to ~8.6% and hit_ratio collapsing from 96% to ~15% while redis CPU (~22%) and connected_clients (~310) stayed flat and evictions stayed at ~3/min. With the cache cold, market-data-service entered a 'cache miss storm', rebuilding every quote from the exchange feed (68 log events, rebuilds up to 8748ms), which is CPU-bound: market-data-service CPU rose from ~45% to ~90% and its p99 from ~40ms to ~2s, with request_rate flat at ~950 req/s. pricing-engine then exceeded its compute deadline waiting on market-data-service ('quote computation deadline exceeded: market-data-service took 5171ms', up to 8456ms), producing the user-visible 2-3s quote loads. The cache is slowly re-warming (hit_ratio recovering 15%->47%, latency declining), consistent with a refilling cold cache. No deployments or config changes to the quote path explain the event; the same redis-failover signature matches historical incident HIST-107.

## Probable root cause
**Redis primary node failed and failed over to an empty (resyncing) replica, causing a cold-cache storm that CPU-saturated market-data-service** -- infrastructure_failure in `redis-cache` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Redis primary node failed and failed over to an empty (resyncing) replica, causing a cold-cache storm that CPU-saturated market-data-service | infrastructure_failure | redis-cache | - | 0.86 (high) | active |
| 2 | H2: Capacity saturation: organic load outgrew market-data-service provisioned capacity | capacity_saturation | market-data-service | - | 0.05 (low) | rejected |
| 3 | H3: Bad deployment / config change to pricing-engine or market-data-service | bad_deployment | pricing-engine | - | 0.04 (low) | rejected |

### H1: Redis primary node failed and failed over to an empty (resyncing) replica, causing a cold-cache storm that CPU-saturated market-data-service
The redis-cache primary redis-cache-2 failed its health check at 13:22:10Z; orchestration promoted replica redis-cache-3 whose dataset was empty. All cached quote keys vanished, so market-data-service suffered a cache-miss storm and rebuilt every quote from the exchange feed. That rebuild work is CPU-bound, driving market-data-service CPU to ~90% and its p99 from ~40ms to ~2s. pricing-engine, which calls market-data-service for quote data, then hit its compute deadline ('market-data-service took 5171ms', up to 8456ms), producing 2-3s quote latency across trading screens.

**Causal chain**
1. redis-cache-2 primary fails health check (3 PING timeouts) -> marked down [F8]
2. failover promotes redis-cache-3, whose dataset is empty because it was resyncing [F9, F7]
3. hit ratio collapses 96% -> 15% (cold cache) while redis CPU/clients steady -> dataset lost, not overload [F6, F7]
4. market-data-service cache-miss storm: every quote rebuilt from exchange feed [F5]
5. rebuild work is CPU-bound -> market-data-service CPU 45% -> ~90%, p99 40ms -> ~2s [F4, F3, F10]
6. pricing-engine quote computation exceeds deadline waiting on market-data-service -> 2-3s quote latency [F2, F1, F11]

**Supporting evidence**
  - `F1` ✔ pricing-engine p99 latency was ~60ms until 13:12 and jumped to ~1700-2300ms from the 13:20 bucket onward, then slowly declined.
  - `F2` ✔ pricing-engine ERROR logs are dominated by 'quote computation deadline exceeded: market-data-service took <n>ms' (72 of 73 matches), with reported downstream times up to 8456ms; first match 13:23:57Z.
  - `F3` ✔ market-data-service p99 latency rose from ~40ms to ~1433-1980ms in the same 13:20 bucket, tracking pricing-engine's onset.
  - `F4` ✔ market-data-service CPU jumped from ~45% to ~79-94.5% at the 13:20 bucket while memory stayed flat at ~62%, indicating CPU-bound work rather than a memory leak.
  - `F5` ✔ market-data-service logs show a 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' pattern, 68 occurrences starting 13:22:25Z with rebuild durations up to 8748ms.
  - `F6` ✔ redis-cache hit_ratio collapsed from ~96% to 14.8% in the 13:28 bucket while cpu_pct (~22%) and connected_clients (~310) were unchanged, ruling out CPU overload of redis.
  - `F7` ✔ redis-cache memory_pct dropped abruptly from ~70% to ~8.6% (max 74.63 at 13:20; avg 8.56 at 13:28) with evictions_per_min flat at ~3/min, indicating the dataset was lost/emptied rather than evicted under pressure.
  - `F8` ✔ redis-cache log at 13:22:10Z: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down'.
  - `F9` ✔ redis-cache log at 13:22:52Z: 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)'.
  - `F10` ✔ market-data-service request_rate was stable at ~950-975 req/s across the incident, so the CPU/latency rise is not explained by increased inbound load.
  - `F11` ✔ market-data-service and pricing-engine error rates rose from ~0.2% to ~3% and ~4% respectively starting at the 13:20 bucket.
  - `F12` ✔ No deployments to pricing-engine or market-data-service on 2026-04-09; the only deployment found was auth-service v7.2.0 at 11:30Z (unrelated to the quote path).
  - `F13` ✔ No config/feature-flag changes for any service (0 changes) and none specifically for redis-cache in the incident window.
  - `F15` ✔ Historical incident HIST-107 ('Cold cache storm after redis failover') on the same services (redis-cache, market-data-service) had root cause 'Redis primary failure promoted an empty replica' with symptom 'cache miss storm'.
  - `F16` ✔ A redis-cache 'client reconnect storm' pattern appears (15048 new connections in 10s at 13:30, 92851 in 10s at 13:40), consistent with clients re-establishing connections after the failover.
**Contradicting evidence**
  - (none)
**Assumptions**
  - redis-cache-2 was the actual primary serving market-data-service's quote keys
  - market-data-service is the sole/primary consumer that rebuilds quotes on cache miss
  - no additional control-plane change (deployment) to redis occurred that the deployment API failed to return

### H2: Capacity saturation: organic load outgrew market-data-service provisioned capacity
A surge of quote traffic above provisioned capacity saturated market-data-service CPU, slowing quotes.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ market-data-service request_rate was stable at ~950-975 req/s across the incident, so the CPU/latency rise is not explained by increased inbound load.
  - `F16` ✔ A redis-cache 'client reconnect storm' pattern appears (15048 new connections in 10s at 13:30, 92851 in 10s at 13:40), consistent with clients re-establishing connections after the failover.
  - `F6` ✔ redis-cache hit_ratio collapsed from ~96% to 14.8% in the 13:28 bucket while cpu_pct (~22%) and connected_clients (~310) were unchanged, ruling out CPU overload of redis.
**Rejected because:** market-data-service request_rate was flat at ~950-975 req/s throughout (F10) and pricing-engine request_rate was also flat; there was no traffic increase. CPU rose with unchanged load, which points to a change in work per request (cache-miss rebuilds), not more requests.

### H3: Bad deployment / config change to pricing-engine or market-data-service
A code or config release introduced slow quote computation or cache-miss behavior.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F12` ✔ No deployments to pricing-engine or market-data-service on 2026-04-09; the only deployment found was auth-service v7.2.0 at 11:30Z (unrelated to the quote path).
  - `F13` ✔ No config/feature-flag changes for any service (0 changes) and none specifically for redis-cache in the incident window.
**Rejected because:** No deployments to pricing-engine or market-data-service on 2026-04-09 (F12) and zero config/feature-flag changes (F13). The only deploy (auth-service, 11:30Z) is off the quote path and precedes onset by ~2h.

## Facts (verified observations)
- pricing-engine p99 jumped from ~60ms to ~1700-2300ms at the 13:20 bucket and is slowly recovering. [F1]
- pricing-engine errors are 'quote computation deadline exceeded: market-data-service took <n>ms' with downstream times up to 8456ms. [F2]
- market-data-service p99 rose from ~40ms to ~1980ms at the same 13:20 onset. [F3]
- market-data-service CPU rose from ~45% to ~90% (max 94.51%) while memory stayed ~62%. [F4]
- market-data-service logged a cache-miss storm rebuilding quotes from the exchange feed (up to 8748ms per rebuild). [F5]
- redis-cache hit_ratio collapsed from ~96% to 14.8% with CPU and client counts unchanged. [F6]
- redis-cache memory_pct fell from ~70% to ~8.6% with evictions flat at ~3/min, indicating dataset loss not eviction. [F7]
- redis-cache logged its primary redis-cache-2 failing health checks at 13:22:10Z. [F8]
- redis-cache failed over to an empty replica (redis-cache-3) that was still resyncing. [F9]
- market-data-service and pricing-engine request rates were flat across the incident. [F10]
- market-data-service and pricing-engine error rates rose from ~0.2% to ~3-4% at onset. [F11]
- No deployments or config changes to pricing-engine, market-data-service, or redis-cache; only an unrelated auth-service deploy at 11:30Z. [F12, F13]
- market-data-service restart count stayed 0 throughout. [F14]
- Historical incident HIST-107 matches: redis primary failure promoted an empty replica causing a cache miss storm. [F15]

## Assumptions (not verified)
- redis-cache-2 was the primary node backing market-data-service's quote cache.
- market-data-service is the primary participant in the quote cache-miss rebuild path.
- No control-plane change to redis occurred that a successful deployment query would have surfaced (the redis deployment query timed out).

## Timeline
- `2026-04-09T12:00:00Z` Baseline: pricing-engine p99 ~60ms, market-data-service p99 ~40ms, redis hit_ratio ~96%, redis memory ~70%. [F1, F3, F6, F7]
- `2026-04-09T13:22:10Z` redis-cache primary redis-cache-2 fails health check (3 PING timeouts) and is marked down. [F8]
- `2026-04-09T13:22:25Z` market-data-service begins logging cache-miss storm, rebuilding quotes from exchange feed. [F5]
- `2026-04-09T13:22:52Z` Failover promotes empty replica redis-cache-3 to primary; dataset empty. [F9, F7]
- `2026-04-09T13:23:57Z` pricing-engine begins logging quote computation deadline-exceeded against market-data-service. [F2]
- `2026-04-09T13:30:20Z` redis client reconnect storm (15048 new connections in 10s); market-data-service CPU ~90%. [F16, F4]
- `2026-04-09T13:30:00Z` Incident reported (IN-006). []
- `2026-04-09T14:56:00Z` Latency recovering: pricing-engine p99 ~1164ms, redis hit_ratio ~47% as cache rewarm proceeds. [F1, F6]

## Recommended actions
- **[immediate]** Confirm redis-cache failover root cause and complete the topology repair: verify redis-cache-2 is back in sync, and if the promoted redis-cache-3 primary is stable, no further action; otherwise fail back to a fully-synced node. -- The failover promoted an empty replica; the system is still re-warming and exposed to a repeat loss. [F8, F9, F7]
- **[immediate]** Warm the quote cache deliberately (pre-populate hot quote keys) rather than relying on organic rebuilds, to end the CPU saturation in market-data-service faster. -- Full-market-data CPU saturation from rebuilds is the active cause of customer-visible latency. [F5, F4, F2]
- **[short_term]** Add replica sync/pre-promotion checks so failover never promotes a replica whose dataset is empty or still resyncing. -- HIST-107 was fixed by exactly this ('fixed replica sync checks'); the same gap recurred. [F9, F15]
- **[short_term]** Add a rate limiter / circuit breaker on cold-cache rebuilds in market-data-service so a cache flush cannot drive CPU to saturation and cascade to pricing-engine deadlines. -- Rebuild storms are CPU-bound and propagate as quote timeouts; bounding them contains blast radius. [F5, F4, F2]
- **[long_term]** Add alerting on redis hit_ratio collapse and memory_pct drop (not just CPU), since the failure was invisible in CPU/clients metrics. -- CPU and client metrics were flat while hit_ratio and memory collapsed; detection depended on a slow customer report. [F6, F7]

## Data gaps
- The all-services and redis-cache deployment queries timed out (tc-002/tc-025/tc-032), so an operator-initiated redis change (e.g., manual failover or config push) cannot be fully excluded from the deployment/config APIs; service-scoped and window-scoped config queries returned 0 changes.
- The redis-cache node failure reason (why redis-cache-2 stopped answering PINGs) is not shown in the logs; only the health-check failure and failover are recorded.
- No redis-cache node-level infra metrics (node health, replication lag) were available via the metrics API to independently confirm the resyncing-replica state.

## Investigation stats
- latency 67.16s, LLM calls 8, tokens 121525 (in 111986 / out 9539)
- tool calls 33 (5 failed, 1 partial, 6 recovered by retry), findings 16/16 verified
