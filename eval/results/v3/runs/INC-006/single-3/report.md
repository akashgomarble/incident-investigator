# Incident investigation report: INC-006
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Quote latency degradation was not caused by pricing-engine. At 13:22:10 the redis-cache primary node (redis-cache-2) failed health checks (3 consecutive PING timeouts) and was marked down; at 13:22:52 failover promoted replica redis-cache-3, whose dataset was empty because it was still resyncing. This wiped the shared cache (hit ratio 96%→16.8%, memory 70%→9.9%, no evictions). market-data-service, which prices quotes out of redis-cache, entered a "cache miss storm", rebuilding each quote from the exchange feed snapshot (2–8s), which raised its p99 from ~40ms to ~1790ms and CPU from ~45% to ~87%. pricing-engine, which calls market-data-service synchronously, then hit "quote computation deadline exceeded", making quotes take 2-3s on trading screens. There were zero deployments and zero config changes across all services, ruling out bad_deployment/config_change. Root cause is an infrastructure failure of the redis-cache primary plus promotion of an unsynced (empty) replica.

## Probable root cause
**redis-cache primary failure promoted an unsynced empty replica, wiping the shared quote cache (infrastructure_failure)** -- infrastructure_failure in `redis-cache` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: redis-cache primary failure promoted an unsynced empty replica, wiping the shared quote cache (infrastructure_failure) | infrastructure_failure | redis-cache | - | 0.86 (high) | active |
| 2 | H2: Pricing-engine computes every quote from scratch (code/logic regression) (rejected) | bad_deployment | pricing-engine | - | 0.05 (low) | rejected |
| 3 | H3: exchange-gateway / external feed degradation slowed quote rebuilds (rejected) | dependency_failure | exchange-gateway | - | 0.05 (low) | rejected |
| 4 | H4: Redis memory-exhaustion/eviction pressure (rejected) | resource_exhaustion | redis-cache | - | 0.04 (low) | rejected |

### H1: redis-cache primary failure promoted an unsynced empty replica, wiping the shared quote cache (infrastructure_failure)
At 13:22:10 the redis-cache primary node redis-cache-2 failed health checks and was marked down; at 13:22:52 failover promoted replica redis-cache-3, whose dataset was empty (still resyncing). This emptied the shared quote cache (hit ratio 96%->16.8%, memory 70%->9.9%). market-data-service then rebuilt every quote from the exchange feed snapshot (2-8s per miss), raising its latency/CPU/error_rate; pricing-engine's synchronous calls to market-data-service hit deadline-exceeded, producing 2-3s quote loads.

**Causal chain**
1. redis-cache-2 primary fails health check, marked down [F3]
2. failover promotes redis-cache-3 with empty dataset (was resyncing) [F4]
3. shared cache wiped: hit_ratio 96%->16.8%, memory 70%->9.9%, no evictions [F5, F6, F7]
4. market-data-service cache-miss storm rebuilds quotes from exchange feed (2-8s) [F8]
5. market-data-service p99 40ms->1791ms, cpu 45%->87%, error_rate 0.2%->3.2% [F9, F10]
6. pricing-engine deadline-exceeded on market-data-service -> 2-3s quote loads [F2, F1]

**Supporting evidence**
  - `F3` ✔ redis-cache primary node redis-cache-2 failed its health check (3 consecutive PING timeouts) and was marked down at 13:22:10.
  - `F4` ✔ redis-cache failover at 13:22:52 promoted replica redis-cache-3 to primary with an EMPTY dataset because it was still resyncing.
  - `F5` ✔ redis-cache hit_ratio_pct collapsed from ~96% to 16.79% at 13:30 (max 97.11 at 13:15 bucket).
  - `F6` ✔ redis-cache memory_pct dropped from ~70% to 9.9% at 13:30, indicating the dataset was emptied (not filled).
  - `F7` ✔ redis-cache evictions_per_min stayed flat at ~3/min across the incident and redis cpu_pct stayed ~22%; neither eviction pressure nor redis CPU explains the hit-ratio collapse.
  - `F8` ✔ market-data-service logged 204 WARN 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' entries starting 13:22:25, with rebuilds up to 8748ms.
  - `F9` ✔ market-data-service p99 rose from ~40ms to avg 1791ms at 13:30 and CPU from ~45% to avg 87.5%, while its request_rate stayed flat at ~950 req/s.
  - `F10` ✔ market-data-service error_rate rose from ~0.2% to ~3.2% at 13:30 while its pod restarts stayed at 0 (no crash/restart).
  - `F11` ✔ exchange-gateway p99 latency stayed flat at ~130ms throughout the incident window, so the exchange feed itself was not slow.
  - `F12` ✔ No deployments occurred for pricing-engine, market-data-service, redis-cache, or any service in the 06:00-15:00 window.
  - `F13` ✔ No configuration or feature-flag changes occurred for any service in the 04-08 to 04-09 window.
  - `F14` ✔ Historical incident HIST-107 (2025-12-09) matches: 'Cold cache storm after redis failover' - root cause 'Redis primary failure promoted an empty replica', services redis-cache and market-data-service.
  - `F15` ✔ Dependency graph confirms pricing-engine calls market-data-service (http) and both share redis-cache; market-data-service calls exchange-gateway (http).
  - `F16` ✔ Inference: The empty-replica promotion wiped the shared quote cache, and market-data-service's forced per-request rebuilds from the exchange feed (2-8s) are the proximate cause of the downstream 2-3s quote loads; pricing-engine is only the symptom holder.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The redis-cache node failure was not itself triggered by an upstream config/deploy change (none observed).
  - market-data-service has no local fallback for cache misses other than rebuilding from the exchange feed.

### H2: Pricing-engine computes every quote from scratch (code/logic regression) (rejected)
A release or logic regression in pricing-engine made quote computation slow. Rejected: no pricing-engine deployment exists (F12), pricing-engine p50 stayed ~200-330ms in its own INFO logs, and its ERRORs explicitly attribute the delay to market-data-service, not itself.

**Causal chain**
1. pricing-engine change -> slow quotes *(assumed)*

**Supporting evidence**
  - `F2` ✔ pricing-engine logged 72 ERROR entries 'quote computation deadline exceeded: market-data-service took <n>ms' (up to 8456ms) starting 13:23:57.
**Contradicting evidence**
  - `F12` ✔ No deployments occurred for pricing-engine, market-data-service, redis-cache, or any service in the 06:00-15:00 window.
  - `F13` ✔ No configuration or feature-flag changes occurred for any service in the 04-08 to 04-09 window.
  - `F5` ✔ redis-cache hit_ratio_pct collapsed from ~96% to 16.79% at 13:30 (max 97.11 at 13:15 bucket).
  - `F6` ✔ redis-cache memory_pct dropped from ~70% to 9.9% at 13:30, indicating the dataset was emptied (not filled).
**Rejected because:** No deployments or config changes for any service (F12, F13); pricing-engine's own errors blame market-data-service latency (F2).

### H3: exchange-gateway / external feed degradation slowed quote rebuilds (rejected)
The exchange feed became slow, so quote rebuilds took 2-8s. Rejected: exchange-gateway p99 stayed flat at ~130ms across the whole window (F11); also its only WARN was a benign 38ms NTP clock-drift message at 13:19:38. The rebuild slowness is explained by cache misses forcing the rebuild path, not by a slow feed.

**Causal chain**
1. exchange feed slows -> slow rebuild *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ exchange-gateway p99 latency stayed flat at ~130ms throughout the incident window, so the exchange feed itself was not slow.
**Rejected because:** exchange-gateway latency unchanged (F11); no upstream error/latency anomaly.

### H4: Redis memory-exhaustion/eviction pressure (rejected)
Redis ran out of memory or evicted keys, causing misses. Rejected: evictions_per_min stayed ~3/min, redis cpu ~22%, and memory FELL from 70% to 9.9% at onset (the dataset shrank, it was not over-filled). The miss cause is an empty promoted replica, not eviction.

**Causal chain**
1. redis OOM -> evictions -> misses *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F14` ✔ Historical incident HIST-107 (2025-12-09) matches: 'Cold cache storm after redis failover' - root cause 'Redis primary failure promoted an empty replica', services redis-cache and market-data-service.
  - `F6` ✔ redis-cache memory_pct dropped from ~70% to 9.9% at 13:30, indicating the dataset was emptied (not filled).
  - `F7` ✔ redis-cache evictions_per_min stayed flat at ~3/min across the incident and redis cpu_pct stayed ~22%; neither eviction pressure nor redis CPU explains the hit-ratio collapse.
**Rejected because:** Evictions flat (F7) and memory dropped rather than saturated (F6).

## Facts (verified observations)
- pricing-engine p99 jumped from ~60ms to avg 1216ms at 13:15 and 2097ms at 13:30. [F1]
- pricing-engine emitted 72 'quote computation deadline exceeded: market-data-service took <n>ms' errors (max 8456ms) starting 13:23:57. [F2]
- redis-cache-2 primary failed health check (3 PING timeouts) at 13:22:10 and failover promoted an empty replica redis-cache-3 at 13:22:52. [F3, F4]
- redis-cache hit_ratio fell from ~96% to 16.79% and memory from ~70% to 9.9% at 13:30, while evictions (~3/min) and CPU (~22%) stayed flat. [F5, F6, F7]
- market-data-service logged 204 cache-miss-storm warnings (rebuilds up to 8748ms) starting 13:22:25; p99 rose 40ms->1791ms and CPU 45%->87.5% at 13:30 with flat request_rate and 0 restarts. [F8, F9, F10]
- exchange-gateway p99 stayed flat at ~130ms. [F11]
- No deployments and no config/feature-flag changes across all services in the incident window. [F12, F13]
- Past incident HIST-107 (2025-12-09) was a cold cache storm after a redis failover that promoted an empty replica. [F14]

## Assumptions (not verified)
- No upstream deploy/config change triggered the redis node failure (none observed in deployment or config-change data).
- market-data-service has no cheaper fallback than rebuilding a quote from the exchange feed on a cache miss.
- The redis-cache cluster is the shared cache used by both market-data-service and pricing-engine per the dependency graph.

## Timeline
- `2026-04-09T13:15:00Z` pricing-engine p99 rises from ~60ms to avg 1216ms [F1]
- `2026-04-09T13:22:10Z` redis-cache-2 primary fails health check (3 PING timeouts), marked down [F3]
- `2026-04-09T13:22:25Z` market-data-service begins 'cache miss storm' warnings [F8]
- `2026-04-09T13:22:52Z` failover promotes replica redis-cache-3 with empty dataset [F4]
- `2026-04-09T13:23:57Z` pricing-engine begins deadline-exceeded errors against market-data-service [F2]
- `2026-04-09T13:30:00Z` redis hit_ratio 16.79%, memory 9.9%; market-data-service p99 1791ms, cpu 87.5%, error_rate 3.2% [F5, F6, F9, F10]
- `2026-04-09T13:30:20Z` redis client reconnect storm (15048 new connections in 10s) [F4]
- `2026-04-09T14:45:00Z` pricing-engine p99 still elevated at avg 1191ms; recovery ongoing [F1]

## Recommended actions
- **[immediate]** Warm/rebuild the redis-cache quote keyspace and verify replica sync is healthy before relying on it -- The promoted primary has an empty dataset; misses force expensive rebuilds. Restoring the cache will remove the cache miss storm. [F4, F6, F8]
- **[short_term]** Add a guard so failover never promotes an unsynced/empty replica (block promotion if replica offset lags), and health-check PING timeouts trigger alerts -- The fault origin is promoting a resyncing empty replica; HIST-107 had the same cause. [F4, F14]
- **[short_term]** Add a local negative-cache/rate-limit or fallback for market-data-service quote rebuilds so a cold cache cannot stampede the exchange feed -- Each cache miss triggers a 2-8s synchronous rebuild; bounding this protects pricing-engine SLAs. [F8, F9]
- **[long_term]** Raise alerting on redis-cache hit_ratio_pct and memory_pct drops so a wiped cache is caught before downstream deadline-exceeded errors -- hit_ratio fell from 96% to 16.8% and memory 70% to 9.9% but was only noticed via pricing-engine symptoms. [F5, F6]

## Data gaps
- Root cause of the redis-cache-2 primary health-check failure (node/infra event) is not directly observable from the available telemetry - only the downstream failure is.
- redis-cache exposes no 'restarts' metric, so we cannot confirm whether the primary node crashed vs. was network-partitioned.
- The all-services deployment list returned partial=true on first query; it was re-run (tc-026) and came back complete with 0 deployments, so deployment absence is confirmed for the window queried (06:00-15:00).
- order-service (also a redis-cache/pricing-engine consumer) was not individually checked for impact.

## Investigation stats
- latency 54.24s, LLM calls 7, tokens 91628 (in 82547 / out 9081)
- tool calls 30 (2 failed, 1 partial, 2 recovered by retry), findings 16/16 verified
