# Incident investigation report: INC-006
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Onset ~13:20 UTC. pricing-engine p99 jumped 60ms -> ~2.1s and market-data-service p99 40ms -> ~1.85s, both in lockstep, with no deploy to either service. The fault originates in the shared redis-cache: at 13:22:10 node redis-cache-2 failed health checks and was marked down; at 13:22:52 a replica (redis-cache-3) was promoted whose dataset was EMPTY. redis-cache memory_pct collapsed 70%->8.6% and hit_ratio_pct 96%->15% at 13:20, confirming the cache contents were lost. market-data-service, now getting ~15% cache hits, rebuilds each quote from the exchange feed snapshot (2-8.7s each), which drives its CPU to 79-94% and propagates multi-second quote latency up through pricing-engine to trading screens. exchange-gateway and redis-cache CPU remain flat/noisy-healthy; no config or code change involved. Best hypothesis: infrastructure_failure at redis-cache (empty-replica failover), matching HIST-107.

## Probable root cause
**redis-cache primary failure promoted an empty replica; cold-cache storm overwhelms market-data-service** -- infrastructure_failure in `redis-cache` (confidence 0.82, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: redis-cache primary failure promoted an empty replica; cold-cache storm overwhelms market-data-service | infrastructure_failure | redis-cache | - | 0.82 (high) | active |
| 2 | H2: market-data-service capacity saturation from its own resource limits | capacity_saturation | market-data-service | - | 0.10 (low) | rejected |
| 3 | H3: exchange-gateway / external exchange feed degradation | dependency_failure | exchange-gateway | - | 0.05 (low) | rejected |

### H1: redis-cache primary failure promoted an empty replica; cold-cache storm overwhelms market-data-service
The redis primary (redis-cache-2) failed health checks and was marked down at ~13:22; the promoted replica (redis-cache-3) had an empty dataset (was mid-resync). All cached quotes were lost, so market-data-service fell back to rebuilding every quote from the exchange feed snapshot in 2-8.7s, saturating its CPU (79-94%) and raising quote latency, which propagated to pricing-engine and trading screens.

**Causal chain**
1. redis-cache primary fails health checks and is marked down [F7]
2. replica promoted with an empty dataset -> hit ratio 96%->15%, memory 70%->8.6% [F5, F6]
3. market-data-service catches cache-miss storm, rebuilds quotes from exchange feed (2-8.7s each) [F4]
4. market-data-service CPU rises to 79-94% and p99 to ~1.85s [F9, F2]
5. pricing-engine quote computation deadline exceeded, p99 ~2.1s, visible on trading screens [F3, F1]

**Supporting evidence**
  - `F5` ✔ redis-cache hit_ratio_pct collapsed from ~96% to ~15% at the 13:20 bucket and stayed low, recovering only slowly.
  - `F6` ✔ redis-cache memory_pct collapsed from ~70% to ~8.6% at the 13:20 bucket, indicating the cached dataset was emptied, not merely evicted under pressure.
  - `F7` ✔ redis-cache logs show the primary node failed health checks and an EMPTY replica was promoted: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' at 13:22:10 and 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' at 13:22:52.
  - `F8` ✔ redis-cache evictions_per_min stayed flat around 3/min through the event (no eviction-driven key loss), and redis-cache cpu_pct stayed ~22%.
  - `F4` ✔ market-data-service logs show 289 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' warnings starting 13:22:25, with rebuilds taking up to ~8748ms.
  - `F2` ✔ market-data-service p99 latency was flat ~40ms until 13:12, then jumped to 1433.5ms at the 13:20 bucket, matching the pricing-engine onset.
  - `F9` ✔ market-data-service CPU jumped from ~45% to 79-94% at the 13:20 bucket, consistent with CPU spent rebuilding quotes after cache misses.
  - `F3` ✔ pricing-engine logs are dominated by 192 ERROR lines 'quote computation deadline exceeded: market-data-service took <n>ms' starting 13:23:57, pinpointing market-data-service as the slow downstream hop.
  - `F1` ✔ pricing-engine p99 latency was flat ~59-61ms until 13:12, then jumped to 1697ms at the 13:20 bucket and peaked at 2309.91ms.
  - `F13` ✔ redis-cache logs show 'client reconnect storm: 15048 new connections in 10s' at 13:30:20 (later 92851) and full resync events, consistent with clients reconnecting after the failover.
  - `F15` ✔ Historical post-mortem HIST-107 (2025-12-09) records 'Cold cache storm after redis failover' with root cause 'Redis primary failure promoted an empty replica', same services and symptoms.
  - `F16` ✔ The simultaneous onset (13:20) of redis-cache hit-ratio/memory collapse, market-data-service latency/CPU spike, and pricing-engine latency spike, combined with the empty-replica failover log at 13:22, establishes an infrastructure failure at redis-cache as the origin, with market-data-service and pricing-engine as downstream victims.
**Contradicting evidence**
  - `F10` ✔ exchange-gateway (market-data's upstream exchange provider) is healthy and flat: latency_p99 ~130ms and upstream_latency_ms ~120ms unchanged across the onset.
  - `F12` ✔ market-data-service request_rate stayed flat ~950 req/s across the onset, so the failure is not a traffic/load surge.
**Assumptions**
  - redis-cache is the shared cache both market-data-service and pricing-engine depend on (confirmed by dependency graph)
  - the health-check failure was an operational fault rather than an externally-triggered event

### H2: market-data-service capacity saturation from its own resource limits
market-data-service hit a capacity/resource ceiling (CPU saturation) causing quote rebuilds to queue and slow down, independent of redis.

**Causal chain**
(none given)

**Supporting evidence**
  - `F9` ✔ market-data-service CPU jumped from ~45% to 79-94% at the 13:20 bucket, consistent with CPU spent rebuilding quotes after cache misses.
**Contradicting evidence**
  - `F12` ✔ market-data-service request_rate stayed flat ~950 req/s across the onset, so the failure is not a traffic/load surge.
  - `F5` ✔ redis-cache hit_ratio_pct collapsed from ~96% to ~15% at the 13:20 bucket and stayed low, recovering only slowly.
  - `F6` ✔ redis-cache memory_pct collapsed from ~70% to ~8.6% at the 13:20 bucket, indicating the cached dataset was emptied, not merely evicted under pressure.
  - `F7` ✔ redis-cache logs show the primary node failed health checks and an EMPTY replica was promoted: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' at 13:22:10 and 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' at 13:22:52.
  - `F4` ✔ market-data-service logs show 289 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' warnings starting 13:22:25, with rebuilds taking up to ~8748ms.
**Rejected because:** request_rate is flat ~950 req/s (F12) so load did not grow; the CPU spike is fully explained by cold-cache rebuilds (F4/F7); and the cache hit-ratio/memory collapse plus empty-replica failover is an upstream cause, not a consequence of market-data saturation.

### H3: exchange-gateway / external exchange feed degradation
The upstream exchange feed provider degraded, slowing quote rebuilds at market-data-service.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ exchange-gateway (market-data's upstream exchange provider) is healthy and flat: latency_p99 ~130ms and upstream_latency_ms ~120ms unchanged across the onset.
  - `F4` ✔ market-data-service logs show 289 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' warnings starting 13:22:25, with rebuilds taking up to ~8748ms.
**Rejected because:** exchange-gateway latency_p99 (~130ms) and upstream_latency_ms (~120ms) are flat across onset (F10); the rebuild slowness only appears because of cache misses, not a slow feed.

## Facts (verified observations)
- pricing-engine p99 rose from ~60ms to ~2.1s at 13:20 UTC. [F1]
- market-data-service p99 rose from ~40ms to ~1.85s at the same 13:20 bucket. [F2]
- pricing-engine errors attribute the slowness to market-data-service (deadline exceeded, 'market-data-service took ...ms'). [F3]
- market-data-service logged a cache-miss storm rebuilding quotes from the exchange feed for 2-8.7s. [F4]
- redis-cache hit_ratio_pct fell from ~96% to ~15%. [F5]
- redis-cache memory_pct fell from ~70% to ~8.6%, indicating the dataset was emptied. [F6]
- redis-cache raised an empty-replica failover: primary marked down, replica with empty dataset promoted. [F7]
- redis-cache evictions stayed ~3/min and CPU ~22% (no eviction/reaper involvement). [F8]
- market-data-service CPU rose to 79-94% during the event. [F9]
- exchange-gateway latency and upstream latency stayed flat (~130ms / ~120ms). [F10]
- No deployments or config changes on 2026-04-09 for pricing-engine, market-data-service, or redis-cache. [F11]
- market-data-service request_rate stayed flat ~950 req/s. [F12]
- market-data-service restarts stayed 0 and memory flat (~62%). [F14]
- Historical incident HIST-107 had identical symptoms and root cause (redis failover promoted empty replica). [F15]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-04-09T13:12:00Z` Last normal bucket: pricing-engine p99 ~60ms, market-data p99 ~40ms, redis hit ratio ~96%. [F1, F2, F5]
- `2026-04-09T13:19:38Z` exchange-gateway logs a trivial 38ms clock drift / NTP resync. []
- `2026-04-09T13:20:00Z` redis-cache hit_ratio collapses to ~15% and memory_pct to ~8.6%; market-data p99 jumps to 1433ms and CPU to 79%; pricing-engine p99 jumps to 1697ms. [F1, F2, F5, F6, F9]
- `2026-04-09T13:22:10Z` redis-cache-2 fails health check (3 PING timeouts) and is marked primary-down. [F7]
- `2026-04-09T13:22:25Z` market-data-service begins logging cache miss storm rebuilds from exchange feed. [F4]
- `2026-04-09T13:22:52Z` Replica redis-cache-3 promoted to primary with an EMPTY dataset (was resyncing). [F7]
- `2026-04-09T13:23:57Z` pricing-engine errors 'quote computation deadline exceeded' begin. [F3]
- `2026-04-09T13:30:20Z` redis-cache client reconnect storm (15048 new connections/10s) as clients reconnect post-failover. [F13]
- `2026-04-09T14:56:00Z` Still degraded; pricing-engine p99 ~1164ms, market-data p99 ~912ms, redis hit ratio ~47% (slowly warming). [F1, F2, F5]

## Recommended actions
- **[immediate]** Trigger/verify a cache warm-up (pre-populate hot quotes in redis-cache) to restore hit ratio and drain the rebuild backlog instead of waiting for organic warming. -- Current state is a slow self-healing cold-cache storm; active warming restores quotes fastest. [F4, F5, F6]
- **[immediate]** Confirm the correct/full replica to promote or resync redis-cache from a non-empty source; verify redis-cache-3 was in fact empty and check replication/sync configuration. -- The trigger was promotion of an empty replica; ensure no further failovers occur and data is not served from an empty store. [F7, F13]
- **[short_term]** Investigate why redis-cache-2 failed health checks (3 consecutive PING timeouts) - host/network saturation, blocked event loop, or persistence (background save) stalls. -- Root trigger is the primary node failing health checks; background saves were active and memory had been ~70%. [F7, F8]
- **[long_term]** Add replica-sync readiness gating so a replica mid-resync is never promoted to primary with an empty dataset. -- Prevents recurrence of the exact HIST-107 failure mode (cold cache storm after empty-replica failover). [F7, F15]
- **[long_term]** Decouple quote serving from cache-fill: add request coalescing / single-flight and backpressure when rebuilding from the exchange feed, so misses cannot saturate CPU. -- The amplification came from every miss independently rebuilding (2-8.7s) and saturating market-data CPU. [F4, F9]

## Data gaps
- First query_metrics attempt for market-data-service latency returned 503 (tc-007); a retry succeeded (tc-012) so coverage is complete, but this hints at transient telemetry backend instability.
- The very first get_deployments call was partial (tc-003); the retried full query (tc-006) showed only the unrelated auth-service deploy, so no deploy evidence is missing.
- No infra/node-level telemetry for the redis-cache host (only redis app metrics) is available, so the underlying reason redis-cache-2 stopped answering PINGs cannot be confirmed from these tools.
- No metric explains advertised pricing-engine 'deadline exceeded' threshold value; taken from logs only.

## Investigation stats
- latency 55.9s, LLM calls 7, tokens 125222 (in 115539 / out 9683)
- tool calls 34 (2 failed, 1 partial, 2 recovered by retry), findings 16/16 verified
