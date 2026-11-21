# Incident investigation report: INC-006
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Quote latency across trading screens is a downstream symptom of an infrastructure failure at the shared redis-cache. At 13:22:10 the redis-cache primary node (redis-cache-2) failed health checks (3 consecutive PING timeouts) and was marked down; at 13:22:52 the failover controller promoted replica redis-cache-3, whose dataset was EMPTY because it was mid-resync. The empty primary collapsed the cache hit ratio from ~96% to ~16%, causing market-data-service to enter a "cache miss storm", rebuilding every quote from the exchange feed snapshot (1-8s per key). Those slow lookups propagated up into pricing-engine as "quote computation deadline exceeded", pushing pricing-engine p99 from ~60ms to ~2100ms. There is no code deployment or config change on pricing-engine, market-data-service, or redis-cache; exchange-gateway latency is flat and redis CPU/evictions are flat, ruling out dependency, capacity, and resource-exhaustion causes. The service is self-recovering as the cache re-warms (p99 falling from 2100ms toward 1170ms). Confidence in the root cause is high given a directly logged causal chain.

## Probable root cause
**redis-cache primary node failure with empty-replica promotion (infrastructure failure)** -- infrastructure_failure in `redis-cache` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: redis-cache primary node failure with empty-replica promotion (infrastructure failure) | infrastructure_failure | redis-cache | - | 0.86 (high) | active |
| 2 | H2: Capacity saturation: market-open load overwhelmed redis/cache capacity | capacity_saturation | redis-cache | - | 0.05 (low) | rejected |
| 3 | H3: Bad deployment to market-data-service/pricing-engine | bad_deployment | market-data-service | - | 0.05 (low) | rejected |
| 4 | H4: Dependency failure at exchange-gateway/exchange feed | dependency_failure | exchange-gateway | - | 0.05 (low) | rejected |

### H1: redis-cache primary node failure with empty-replica promotion (infrastructure failure)
The redis-cache primary (redis-cache-2) failed health checks (3 consecutive PING timeouts) and was marked down at 13:22:10; the failover controller promoted replica redis-cache-3, whose dataset was empty because it was mid-resync. The resulting cold cache collapsed hit ratio to ~16% and forced market-data-service to rebuild every quote from the exchange feed (1-8s per key), which propagated into pricing-engine as 'quote computation deadline exceeded' and elevated p99 to ~2s. Fault origin: redis-cache node/infrastructure.

**Causal chain**
1. redis-cache-2 primary fails health checks (3 PING timeouts) and is marked down at 13:22:10 [F3]
2. Failover promotes redis-cache-3, whose dataset is empty (was resyncing) at 13:22:52 [F4]
3. Cache hit ratio collapses from ~96% to ~16% [F5, F10]
4. market-data-service enters cache miss storm, rebuilding quotes from exchange feed snapshot in 1-8s [F6, F7]
5. pricing-engine quote computation deadline exceeded; p99 rises to ~2100ms [F1, F2]

**Supporting evidence**
  - `F3` ✔ redis-cache logged an ERROR at 13:22:10: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down'.
  - `F4` ✔ redis-cache logged at 13:22:52 a failover promoting an EMPTY replica: 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)'.
  - `F5` ✔ redis-cache hit ratio dropped from ~95.8% (13:10 bucket) to 29.06% avg (13:20 bucket) and 15.9% (13:30 bucket), recovering gradually to 46.16% by 14:50.
  - `F6` ✔ market-data-service WARN logs are dominated (289) by 'cache miss storm: quote:<id> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)', first seen 13:22:25, with rebuild times of 1-8.7s.
  - `F7` ✔ market-data-service p99 latency was flat ~39ms until 13:10, then jumped in the 13:20 bucket to avg 1535.72ms (max 1979.98ms), tracking the pricing-engine degradation.
  - `F10` ✔ redis-cache memory_pct FELL during the incident rather than rising: 70.34% at 13:00, down to avg 9.32% at 13:30, i.e. the datastore emptied out (consistent with an empty promoted replica, not memory pressure).
  - `F13` ✔ redis-cache logged client reconnect storms after the failover: 15048 new connections in 10s at 13:30:20 and 92851 at 13:40:15, indicating clients thundering back to the new primary.
  - `F14` ✔ The failure originated in redis-cache infrastructure (primary node health-check failure forcing promotion of an unsynced/empty replica), not in pricing-engine or market-data-service; those services only reflected the resulting cache-miss latency.
**Contradicting evidence**
  - `F8` ✔ exchange-gateway p99 latency stayed flat at ~127-133ms across the entire window including the incident period (e.g. 13:20 avg 132.17, 13:30 avg 131.23), so the upstream exchange feed itself was not slow.
  - `F9` ✔ redis-cache cpu_pct stayed flat ~22% through the incident (13:20 avg 21.85, 13:30 avg 21.68) and evictions_per_min stayed flat ~3/min (13:20 avg 2.91, 13:30 avg 3.03).
**Assumptions**
  - The 'MARKET DATA' rebuild path actually calls the exchange feed via the normal path (no separate fast path)
  - No earlier undetected replica lag issue beyond what redis logs show

### H2: Capacity saturation: market-open load overwhelmed redis/cache capacity
Record load could have exceeded provisioned cache capacity, causing evictions and misses. Rejected: redis evictions/min stayed flat ~3/min and cpu_pct flat ~22%, request_rate to market-data-service stayed flat ~950 req/s, and memory FELL rather than rising — no saturation signature.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ redis-cache cpu_pct stayed flat ~22% through the incident (13:20 avg 21.85, 13:30 avg 21.68) and evictions_per_min stayed flat ~3/min (13:20 avg 2.91, 13:30 avg 3.03).
  - `F10` ✔ redis-cache memory_pct FELL during the incident rather than rising: 70.34% at 13:00, down to avg 9.32% at 13:30, i.e. the datastore emptied out (consistent with an empty promoted replica, not memory pressure).
**Rejected because:** No rise in evictions, CPU, request rate, or memory; the cache emptied (memory fell) which is inconsistent with load-driven saturation. Also onset is a step change at 13:22, not a gradual load ramp.

### H3: Bad deployment to market-data-service/pricing-engine
A release could have introduced slow code. Rejected: no deployments exist for market-data-service (count 0) or pricing-engine. Rejected also because the timing of the fault traces to redis-cache failover, and past incident HIST-101 (bcrypt) is a different mechanism.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ The only deployment across all services in the window is auth-service v7.2.0 ('Session cookie hardening') at 11:30:00Z; no deployments exist for market-data-service or redis-cache (count 0 each), and the listing was marked partial.
**Rejected because:** No deployment for the implicated services; get_deployments returned count 0 for market-data-service and redis-cache. Fault coincides exactly with redis-cache failover, not a release.

### H4: Dependency failure at exchange-gateway/exchange feed
The external exchange feed could be slow. Rejected: exchange-gateway p99 latency stayed ~127-133ms flat across the incident. The 'rebuilding from exchange feed snapshot' is triggered by cache misses, not by a slow feed.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ exchange-gateway p99 latency stayed flat at ~127-133ms across the entire window including the incident period (e.g. 13:20 avg 132.17, 13:30 avg 131.23), so the upstream exchange feed itself was not slow.
**Rejected because:** exchange-gateway latency was flat; the rebuild latency is from cache misses, not a degraded external provider.

## Facts (verified observations)
- pricing-engine p99 latency jumped from ~60ms to ~1800ms avg starting in the 13:20 bucket and is slowly recovering. [F1]
- pricing-engine errors are all 'quote computation deadline exceeded: market-data-service took <n>ms' (192 occurrences, up to 8456ms). [F2]
- redis-cache primary node redis-cache-2 was marked down at 13:22:10 after 3 consecutive PING timeouts. [F3]
- Failover at 13:22:52 promoted replica redis-cache-3 whose dataset was empty (replica was resyncing). [F4]
- redis-cache hit ratio collapsed from ~96% to ~16% during the incident and is recovering. [F5]
- market-data-service logged a cache miss storm rebuilding quotes from the exchange feed snapshot in 1-8s, starting 13:22:25. [F6]
- market-data-service p99 latency jumped from ~39ms to ~1536ms in the 13:20 bucket. [F7]
- exchange-gateway p99 stayed flat ~130ms throughout, ruling out the external feed as the slow component. [F8]
- redis-cache CPU (~22%) and evictions (~3/min) stayed flat, ruling out CPU/eviction saturation. [F9]
- redis-cache memory dropped from ~70% to ~9% (datastore emptied), consistent with an empty promoted replica rather than memory pressure. [F10]
- No deployments exist for market-data-service or redis-cache; only an unrelated auth-service v7.2.0 deploy at 11:30. [F11]
- No configuration or feature-flag changes were recorded for any service in the window. [F12]
- redis-cache saw client reconnect storms post-failover (15048 and 92851 new connections in 10s). [F13]

## Assumptions (not verified)
- The redis-cache failover decision and replica resync state are genuine and not an artifact of the logging pipeline (logs are internally consistent with the metric collapse).
- market-data-service rebuilds directly from the exchange feed snapshot on a cache miss, so any cold cache causes the observed 1-8s rebuilds.
- No config change to redis-cache occurred (get_config_changes returned empty), so the primary failure was not caused by a configuration edit.
- get_deployments for all services was partial, but the implicated services (market-data-service, redis-cache) returned full count-0 results, so absence of deploys there is reliable.

## Timeline
- `2026-04-09T11:30:00Z` Unrelated auth-service v7.2.0 deploy (session cookie hardening) — not temporally linked to onset. [F11]
- `2026-04-09T13:10:00Z` Last healthy bucket: pricing-engine p99 ~60ms, market-data-service p99 ~40ms, redis hit ratio ~95.8%. [F1, F7, F5]
- `2026-04-09T13:22:10Z` redis-cache-2 primary fails health check (3 consecutive PING timeouts) and is marked down. [F3]
- `2026-04-09T13:22:25Z` market-data-service begins logging 'cache miss storm ... not in redis-cache, rebuilding from exchange feed snapshot'. [F6]
- `2026-04-09T13:22:52Z` Failover promotes replica redis-cache-3, whose dataset is empty (was resyncing). [F4]
- `2026-04-09T13:23:57Z` pricing-engine begins logging 'quote computation deadline exceeded: market-data-service took 5171ms'. [F2]
- `2026-04-09T13:30:20Z` redis-cache client reconnect storm (15048 new connections in 10s); 92851 at 13:40:15. [F13]
- `2026-04-09T13:30:00Z` Incident reported (SEV-2); pricing-engine p99 ~2124ms, hit ratio trough ~16%. [F1, F5]
- `2026-04-09T14:50:00Z` Partial recovery: pricing-engine p99 down to ~1172ms, redis hit ratio back to ~46%. [F1, F5]

## Recommended actions
- **[immediate]** Warm/repopulate the redis-cache quotes keyspace (or trigger market-data-service to re-prime the cache) to accelerate recovery of hit ratio and quote latency. -- The system is recovering only as the cache re-warms (hit ratio 16% -> 46%); forcing a warm-up will cut time-to-recovery. [F5, F6]
- **[immediate]** Investigate why redis-cache-2 (primary) failed health checks / became unreachable (host, network, or process) — inspect node-level and infra metrics. -- The trigger is the primary node losing PING responses; without finding why, it may recur. [F3]
- **[short_term]** Fix the replication/failover configuration so promotion never selects an unsynced/empty replica (e.g. min-replicas-to-write, replica-serve-stale-data, or block promotion when replica sync is incomplete). -- The empty promoted replica is what turned a node blip into a full cold-cache outage; preventing it removes the amplification. [F4, F10]
- **[short_term]** Add alerting on redis-cache cache hit ratio and replica sync state so an empty-primary condition is detected immediately. -- Hit ratio collapse was the direct driver of the miss storm but there was no early signal before customer-visible latency. [F5, F6]
- **[short_term]** Add replica-sync health checks before failover (prevent promotion of resyncing replicas) — mirrors HIST-107 resolution. -- HIST-107 (Cold cache storm after redis failover) had the same empty-replica root cause; the prior fix was not applied here. [F4]
- **[long_term]** Implement negative-cache / request coalescing for quote lookups in market-data-service to cap rebuild fan-out during a cold cache. -- Every miss triggers a 1-8s exchange-feed rebuild; coalescing would bound the impact of any future cold cache. [F6]

## Data gaps
- Node/infrastructure telemetry for redis-cache-2 (the failed primary) is not available — the root trigger of its PING timeouts (host failure, network partition, OOM kill, process crash) cannot be confirmed.
- get_deployments for all services was marked partial (one backend shard did not respond), though per-service queries for market-data-service and redis-cache returned complete count-0 results.
- The full set of redis-cache logs is truncated (35 matched, 20 returned); additional failure-mode log lines (e.g. OOM, connection errors) may exist beyond those retrieved.
- No metric directly showing redis-cache replica replication lag/offset before 13:22 — only the failover log line states the replica was resyncing.

## Investigation stats
- latency 67.8s, LLM calls 6, tokens 88005 (in 79043 / out 8962)
- tool calls 25 (1 failed, 1 partial, 2 recovered by retry), findings 14/14 verified
