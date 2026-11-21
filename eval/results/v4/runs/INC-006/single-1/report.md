# Incident investigation report: INC-006
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Slow quotes in the trading app are a symptom of a redis-cache infrastructure failure. At 13:22:10 the redis-cache primary (redis-cache-2) failed its health check (3 consecutive PING timeouts) and was marked down; at 13:22:52 an automatic failover promoted replica redis-cache-3 to primary, but its dataset was EMPTY because the replica was still resyncing. This wiped the cached quotes: redis-cache hit_ratio collapsed 96%->13% and memory 70%->7%. market-data-service was forced into a 'cache miss storm', rebuilding every quote from the exchange feed snapshot, which drove its CPU to ~94% and p99 from ~40ms to ~1900ms. pricing-engine's downstream calls to market-data-service then exceeded their deadline, raising pricing-engine p99 from ~60ms to ~2230ms and error_rate from ~0.2% to ~4.2%, which users saw as 2-3s quote loads. Alternatives were ruled out: exchange-gateway is healthy (flat ~130ms, no errors), there were no deploys/config changes to pricing, market-data, or redis (only an unrelated auth-service deploy 2h earlier), and redis CPU/evictions were normal so this was not memory-pressure eviction or CPU saturation. Latency is slowly recovering as the cache re-warms (p99 2230ms->1153ms). This matches HIST-107 'Cold cache storm after redis failover'. Root cause originates in redis-cache (infrastructure_failure); category = infrastructure_failure.

## Probable root cause
**redis-cache primary node failure with failover to an empty replica triggered a cold-cache storm** -- infrastructure_failure in `redis-cache` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: redis-cache primary node failure with failover to an empty replica triggered a cold-cache storm | infrastructure_failure | redis-cache | - | 0.85 (high) | active |
| 2 | H2: market-data-service CPU/resource saturation drove the downstream latency | resource_exhaustion | market-data-service | - | 0.15 (low) | rejected |
| 3 | H3: exchange-gateway / external exchange provider degradation | dependency_failure | exchange-gateway | - | 0.05 (low) | rejected |
| 4 | H4: Bad deployment or config change to pricing/market-data | bad_deployment | auth-service | dep-5604 | 0.05 (low) | rejected |

### H1: redis-cache primary node failure with failover to an empty replica triggered a cold-cache storm
The redis-cache primary (redis-cache-2) stopped responding to PINGs at ~13:22, triggering an automatic failover to replica redis-cache-3 whose dataset was still empty (it was mid-resync). This wiped the cached quotes, collapsing hit ratio from ~96% to ~13%. market-data-service then had to rebuild every quote from the exchange feed snapshot, saturating its CPU (~94%) and driving its p99 from ~40ms to ~1900ms. pricing-engine's downstream calls to market-data-service exceeded their deadline, raising pricing-engine p99 to ~2230ms and error rate to ~4.2%, surfacing as slow quotes in the trading app.

**Causal chain**
1. redis-cache-2 primary fails health check (PING timeouts) at 13:22:10 [F4]
2. failover promotes redis-cache-3 with an empty dataset at 13:22:52 [F4]
3. cache is emptied: hit ratio 96%->13%, memory 70%->7% [F5]
4. market-data-service cache-miss storm rebuilds quotes from exchange feed, CPU 45%->94%, p99 40ms->1900ms [F6, F7]
5. pricing-engine downstream deadline exceeded, p99 -> ~2230ms, error_rate -> ~4.2% [F1, F2, F3]

**Supporting evidence**
  - `F1` ✔ pricing-engine p99 latency was ~60ms through 13:12, jumped at 13:18 (avg 781ms), peaked ~2230ms at 13:24, and has been slowly declining to ~1153ms by 14:54.
  - `F2` ✔ pricing-engine error_rate rose from ~0.2% to ~4.2% at the same onset (~13:18-13:24) and stayed elevated.
  - `F3` ✔ pricing-engine ERROR logs (192 matches) are all 'quote computation deadline exceeded: market-data-service took <n>ms', first seen 13:23:57, indicating the latency originates downstream at market-data-service.
  - `F4` ✔ redis-cache logs show the primary node redis-cache-2 failed health check with 3 consecutive PING timeouts at 13:22:10, and at 13:22:52 a failover promoted replica redis-cache-3 to primary with an EMPTY dataset because the replica was still resyncing.
  - `F5` ✔ redis-cache memory_pct collapsed from ~70% to ~7% between 13:18 and 13:24, and hit_ratio_pct collapsed from ~96% to ~13% over the same window (subsequently recovering gradually to ~47%).
  - `F6` ✔ market-data-service WARN logs show 289 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' events, first seen 13:22:25, coincident with the redis failover.
  - `F7` ✔ market-data-service p99 latency rose from ~40ms to ~1900ms at 13:18-13:24, and its CPU rose from ~45% to ~94% over the same window.
  - `F9` ✔ redis-cache CPU stayed flat at ~22% and evictions_per_min stayed flat at ~3/min throughout, so the cache emptying was not caused by CPU saturation or memory-pressure eviction.
  - `F10` ✔ The only deployment in the window is auth-service dep-5604 v7.2.0 at 11:30:00 (Session cookie hardening), and there were zero config/feature-flag changes; market-data-service had zero deployments.
  - `F11` ✔ market-data-service restarts metric is 0 across the whole window (no pod restart) even though its CPU and latency spiked, and request_rate stayed flat at ~955 req/s.
  - `F12` ✔ The failure propagates: redis-cache primary failure/empty-replica failover -> cache hit ratio collapse -> market-data-service cache-miss storm rebuilding from exchange feed (CPU+latsity spike) -> pricing-engine downstream deadline exceeded -> slow quotes on trading screens. Onset times are ordered and consistent.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The redis-cache failover logs describe a real node failure (health-check timeouts), not a monitoring artifact.
  - The gradual recovery in pricing-engine p99 reflects the cache warming back up rather than a manual mitigation not visible in the tooling.

### H2: market-data-service CPU/resource saturation drove the downstream latency
market-data-service's CPU spiked to ~94% and its p99 to ~1900ms, which could independently be the root cause if it was resource-exhausted (e.g., by a load surge). However request_rate stayed flat at ~955 req/s and CPU only spiked in lockstep with the cache miss storm, so the CPU saturation is a consequence, not the origin.

**Causal chain**
1. market-data-service CPU spikes to 94% and p99 to 1900ms [F7]
2. pricing-engine calls to it exceed deadline [F3]

**Supporting evidence**
  - `F7` ✔ market-data-service p99 latency rose from ~40ms to ~1900ms at 13:18-13:24, and its CPU rose from ~45% to ~94% over the same window.
**Contradicting evidence**
  - `F6` ✔ market-data-service WARN logs show 289 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' events, first seen 13:22:25, coincident with the redis failover.
  - `F11` ✔ market-data-service restarts metric is 0 across the whole window (no pod restart) even though its CPU and latency spiked, and request_rate stayed flat at ~955 req/s.
**Assumptions**
  - None required to state the contradiction.
**Rejected because:** CPU spike coincides exactly with the cache miss storm (F6) and request_rate was flat (F11/F20-tc020), so the saturation is a downstream effect of the redis cache emptying, not an independent load-driven saturation.

### H3: exchange-gateway / external exchange provider degradation
Slow quotes could originate from the external exchange feed. This is ruled out: exchange-gateway latency stayed flat (~130ms) with no errors, and the market-data-service rebuilds were slow because of cache misses, not because the exchange feed itself was slow.

**Causal chain**
1. exchange feed slow -> quote rebuilds slow *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ exchange-gateway latency_p99_ms stayed flat at ~110-144ms across the entire window, with no errors (only one clock-drift WARN at 13:19:38), ruling out the external exchange provider as the fault origin.
  - `F6` ✔ market-data-service WARN logs show 289 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' events, first seen 13:22:25, coincident with the redis failover.
**Assumptions**
  - None.
**Rejected because:** exchange-gateway p99 flat at ~130ms with no errors (F8); the slow path is quote rebuilds after cache misses (F6), not exchange provider latency.

### H4: Bad deployment or config change to pricing/market-data
A release or flag change could have introduced the latency. Ruled out: no deployments or config changes touched pricing-engine, market-data-service, or redis-cache in the window; the only deploy was auth-service v7.2.0 at 11:30, ~2h before onset and unrelated to quote data.

**Causal chain**
1. dep-5604 auth-service change -> quote latency *(assumed)*

**Supporting evidence**
  - `F10` ✔ The only deployment in the window is auth-service dep-5604 v7.2.0 at 11:30:00 (Session cookie hardening), and there were zero config/feature-flag changes; market-data-service had zero deployments.
**Contradicting evidence**
  - `F10` ✔ The only deployment in the window is auth-service dep-5604 v7.2.0 at 11:30:00 (Session cookie hardening), and there were zero config/feature-flag changes; market-data-service had zero deployments.
  - `F4` ✔ redis-cache logs show the primary node redis-cache-2 failed health check with 3 consecutive PING timeouts at 13:22:10, and at 13:22:52 a failover promoted replica redis-cache-3 to primary with an EMPTY dataset because the replica was still resyncing.
**Assumptions**
  - None.
**Rejected because:** Only deployment is auth-service dep-5604 at 11:30 (2h before onset, unrelated service); no config changes; no deploys to pricing or market-data (F10).

## Facts (verified observations)
- pricing-engine p99 jumped from ~60ms to ~2230ms at 13:18-13:24 and was slowly recovering to ~1153ms by 14:54. [F1]
- pricing-engine error_rate rose from ~0.2% to ~4.2% at onset. [F2]
- All pricing-engine errors are 'quote computation deadline exceeded: market-data-service took <n>ms', first seen 13:23:57. [F3]
- redis-cache primary redis-cache-2 failed health check with 3 consecutive PING timeouts at 13:22:10; at 13:22:52 failover promoted replica redis-cache-3 with an empty dataset (still resyncing). [F4]
- redis-cache hit_ratio collapsed 96%->13% and memory_pct 70%->7% at 13:18-13:24. [F5]
- market-data-service logged 289 cache-miss-storm events rebuilding quotes from the exchange feed snapshot starting 13:22:25. [F6]
- market-data-service p99 rose from ~40ms to ~1900ms and CPU from ~45% to ~94% at the same onset. [F7]
- exchange-gateway p99 stayed flat ~110-144ms with no errors, ruling out the external exchange provider. [F8]
- redis-cache CPU (~22%) and evictions (~3/min) stayed flat, so the cache emptying was not CPU saturation or memory-pressure eviction. [F9]
- The only deployment was auth-service dep-5604 v7.2.0 at 11:30; no config changes; market-data-service had zero deployments. [F10]
- market-data-service restarts = 0 and request_rate stayed flat ~955 req/s during the incident. [F11]

## Assumptions (not verified)
- The redis-cache node failure (PING timeouts) is a genuine broker/node fault rather than a monitoring false positive; no deeper infra telemetry (e.g., host-level or network-layer data) was available to confirm the node's actual state.
- The recovery trend is natural cache re-warming; no manual mitigation actions are visible in the provided logs/metrics.

## Timeline
- `2026-04-09T11:30:00Z` auth-service v7.2.0 (dep-5604) deployed - unrelated to quote path, 2h before onset [F10]
- `2026-04-09T13:18:00Z` pricing-engine and market-data-service latency begin rising; redis-cache hit_ratio and memory begin collapsing [F1, F5, F7]
- `2026-04-09T13:22:10Z` redis-cache-2 primary fails health check (3 consecutive PING timeouts), marked down [F4]
- `2026-04-09T13:22:25Z` market-data-service begins cache miss storm rebuilding quotes from exchange feed [F6]
- `2026-04-09T13:22:52Z` failover promotes replica redis-cache-3 to primary with an empty dataset (replica resyncing) [F4]
- `2026-04-09T13:23:57Z` pricing-engine begins emitting 'quote computation deadline exceeded: market-data-service' errors [F3]
- `2026-04-09T13:24:00Z` Peak impact: pricing-engine p99 ~2230ms, error_rate ~4.2%, market-data-service CPU ~94%, redis hit ratio ~13% [F1, F2, F5, F7]
- `2026-04-09T13:30:20Z` redis-cache client reconnect storm (15048 new connections in 10s) as clients re-establish connections [F4]
- `2026-04-09T14:54:00Z` Slow recovery: pricing-engine p99 down to ~1153ms as cache re-warms [F1]

## Recommended actions
- **[immediate]** Pre-warm / manually repopulate the redis-cache quote keyspace (or trigger market-data-service to rebuild hot quotes) to accelerate recovery -- The cache was emptied by the failover and is only re-warming gradually; forcing a warm will restore hit ratio and cut quote latency. [F5, F6, F1]
- **[immediate]** Fail over back to / restore redis-cache-2 replica in a controlled manner once it is healthy and verify the promoted primary has data -- An empty primary serving production is a latent risk; ensure the topology is stable and data-bearing. [F4]
- **[short_term]** Add a guard so failover refuses to promote a replica whose dataset is empty/incomplete, and require replicas to complete sync before being promotion-eligible -- The failover promoted an empty replica, which directly caused the cache-miss storm - the core defect. [F4, F5]
- **[short_term]** Add cache-warming and request-coalescing/throttling in market-data-service so a cold cache does not stampede the exchange feed and saturate CPU -- The miss storm drove CPU to 94% and p99 to 1900ms; protecting the rebuild path would contain blast radius. [F6, F7]
- **[long_term]** Improve redis-cache monitoring: alert on hit_ratio drop and memory_pct drop, and on primary health-check failures, with runbook pointing at HIST-107 -- Detection was by downstream symptom; direct cache health signals existed but did not page early. [F5, F4]

## Data gaps
- No host/node-level telemetry for redis-cache-2 to confirm why PINGs timed out (network, process halt, OOM at host level) - the root trigger of the node failure is unobserved.
- The initial get_deployments (all services) returned partial:true (one backend shard did not respond), so a deployment on some service in the window cannot be fully excluded; however market-data-service, redis-cache, and pricing-engine deploys were confirmed absent.
- No redis-cache 'role' or 'replication lag' metric was available, so the replica's resync state at failover time is only known from the log message.
- No direct telemetry on the trading-screen/frontend layer; impact is inferred from pricing-engine p99 and error logs.

## Investigation stats
- latency 85.48s, LLM calls 6, tokens 97580 (in 88166 / out 9414)
- tool calls 27 (2 failed, 1 partial, 2 recovered by retry), findings 12/12 verified
