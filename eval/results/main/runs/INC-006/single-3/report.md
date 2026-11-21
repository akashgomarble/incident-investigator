# Incident investigation report: INC-006
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
pricing-engine quote latency (p99 60ms -> >2s, deadline-exceeded) is a downstream symptom. At ~13:22 the redis-cache primary node (redis-cache-2) failed health checks and failover promoted redis-cache-3, which was mid-resync with an EMPTY dataset. redis-cache memory fell 70%->8% and hit ratio 96%->14%. The cold cache triggered market-data-service 'cache miss storm' events, rebuilding each quote from the exchange feed snapshot in 1-8s, raising market-data-service p99 from ~40ms to ~1980ms and cascading into pricing-engine's quote computation. No trading-service deploys or config changes occurred; request rate was flat and exchange-gateway was healthy, ruling out capacity saturation, bad deployment and external-provider failure. Root cause category: infrastructure_failure at redis-cache. Matches historical incident HIST-107 (empty-replica-promotion cold cache storm).

## Probable root cause
**redis-cache primary failure promoted an empty (resyncing) replica, cold-cache storm on market-data-service** -- infrastructure_failure in `redis-cache` (confidence 0.87, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: redis-cache primary failure promoted an empty (resyncing) replica, cold-cache storm on market-data-service | infrastructure_failure | redis-cache | - | 0.87 (high) | active |
| 2 | H2: Capacity saturation of market-data-service / redis under load | capacity_saturation | market-data-service | - | 0.05 (low) | rejected |
| 3 | H3: Faulty code/config release or external provider failure | bad_deployment | pricing-engine | - | 0.05 (low) | rejected |

### H1: redis-cache primary failure promoted an empty (resyncing) replica, cold-cache storm on market-data-service
At 13:22 the redis-cache primary node redis-cache-2 failed health checks; automatic failover promoted replica redis-cache-3, which was mid-resync and had an empty dataset. The resulting cold cache caused market-data-service cache-miss storms (rebuilding quotes from the exchange feed, taking seconds), slowing market-data-service and cascading through pricing-engine quote computation to produce 2-3s quote latency.

**Causal chain**
1. redis-cache primary redis-cache-2 failed health checks (3 PING timeouts) [F2]
2. failover promoted an empty resyncing replica -> cache dataset lost (memory 70%->8%, hit ratio 96%->14%) [F2, F3]
3. market-data-service cache-miss storm: rebuild quotes from exchange feed, 1-8s each [F4]
4. market-data-service p99 rises ~40ms->1980ms [F6]
5. pricing-engine quote computation deadline exceeded calling market-data-service; p99 ~60ms->2227ms [F5, F1]

**Supporting evidence**
  - `F1` ✔ pricing-engine p99 latency rose from ~60ms (up to 13:00) to 2227ms max in the 13:12 bucket, staying elevated through 14:48.
  - `F2` ✔ redis-cache logged a failed health check marking primary down, then a failover promoting a replica whose dataset was empty because the replica was resyncing.
  - `F3` ✔ redis-cache memory_pct collapsed from ~70% to ~8% (bucket 13:24 avg 8.04) and hit_ratio_pct collapsed from ~96% to ~14% (bucket 13:24 avg 14.02), indicating the dataset was lost, not merely under memory pressure.
  - `F4` ✔ market-data-service logged 204 'cache miss storm' warnings, e.g. quote keys 'not in redis-cache, rebuilding from exchange feed snapshot' taking seconds, starting 13:22:25.
  - `F5` ✔ pricing-engine logged 72 'quote computation deadline exceeded: market-data-service took <n>ms' errors (up to 8456ms), i.e. the slow dependency was market-data-service.
  - `F6` ✔ market-data-service p99 rose in lockstep with pricing-engine, from ~40ms to 1979.98ms max in the 13:12 bucket.
  - `F10` ✔ redis-cache evictions_per_min (~3) and cpu_pct (~22%) were flat across the window, inconsistent with eviction/CPU-driven cache loss.
  - `F11` ✔ redis-cache logged client reconnect storms (e.g. 92851 new connections in 10s at 13:40:15) and full resyncs of redis-cache-2, consistent with clients reconnecting after the failover.
  - `F12` ✔ Historical incident HIST-107 describes 'Cold cache storm after redis failover' with root cause 'Redis primary failure promoted an empty replica' affecting redis-cache and market-data-service.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The empty dataset was the cause of the miss storm, not a coincidental eviction (evictions/CPU flat support this).
  - No other dependency of market-data-service degraded simultaneously (exchange-gateway healthy).

### H2: Capacity saturation of market-data-service / redis under load
Quote load outgrew provisioned cache/processing capacity, exhausting resources and causing slow quote rebuilds.

**Causal chain**
1. load surge overwhelms capacity *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ market-data-service request_rate was flat at ~950 req/s throughout (min 858.72, max 1049.4), showing no traffic surge.
  - `F10` ✔ redis-cache evictions_per_min (~3) and cpu_pct (~22%) were flat across the window, inconsistent with eviction/CPU-driven cache loss.
  - `F3` ✔ redis-cache memory_pct collapsed from ~70% to ~8% (bucket 13:24 avg 8.04) and hit_ratio_pct collapsed from ~96% to ~14% (bucket 13:24 avg 14.02), indicating the dataset was lost, not merely under memory pressure.
**Assumptions**
  - A traffic surge occurred
**Rejected because:** market-data-service request_rate flat at ~950 req/s (F7); redis evictions and CPU flat (F10); redis memory and hit ratio fell rather than saturated (F3).

### H3: Faulty code/config release or external provider failure
A deployment or config change to a trading service, or exchange-gateway degradation, introduced the latency.

**Causal chain**
1. faulty change deployed to a trading service *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ No deployments to pricing-engine, market-data-service or redis-cache; the only deployment was auth-service v7.2.0 (Session cookie hardening) and there were zero config/flag changes.
  - `F8` ✔ exchange-gateway showed no errors and flat p99 (~110-144ms) across the incident window.
**Assumptions**
  - An undocumented release occurred
**Rejected because:** No deployments/config changes to any trading service (only unrelated auth-service v7.2.0); exchange-gateway p99 flat ~130ms with zero errors.

## Facts (verified observations)
- pricing-engine p99 rose from ~60ms to a 2227.82ms max in the 13:12 bucket and stayed elevated through 14:48. [F1]
- redis-cache primary redis-cache-2 failed its health check and failover promoted an empty, resyncing replica redis-cache-3. [F2]
- redis-cache memory_pct fell to ~8% and hit_ratio_pct to ~14% in the 13:24 bucket. [F3]
- market-data-service emitted 204 'cache miss storm' warnings rebuilding quotes from the exchange feed snapshot in 1-8s. [F4]
- pricing-engine's dominant error was 'quote computation deadline exceeded: market-data-service took <n>ms' (72 occurrences, up to 8456ms). [F5]
- market-data-service p99 rose in lockstep to 1979.98ms max in the 13:12 bucket. [F6]
- market-data-service request_rate stayed flat (~950 req/s), and redis-cache evictions (~3/min) and cpu (~22%) stayed flat. [F7, F10]
- exchange-gateway had no errors and flat p99 (~130ms). [F8]
- No deployments or config changes affected any trading service; only auth-service v7.2.0 was deployed at 11:30. [F9]
- A prior incident (HIST-107) was an empty-replica-promotion cold cache storm affecting the same services. [F12]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-04-09T11:30:00Z` Unrelated auth-service v7.2.0 deploy (session cookie hardening). [F9]
- `2026-04-09T13:12:00Z` pricing-engine and market-data-service p99 begin climbing (60ms->421ms / 40ms->353ms). [F1, F6]
- `2026-04-09T13:22:10Z` redis-cache-2 fails health check (3 PING timeouts), marked primary down. [F2]
- `2026-04-09T13:22:25Z` First market-data-service 'cache miss storm' warning. [F4]
- `2026-04-09T13:22:52Z` Failover promotes empty resyncing replica redis-cache-3 to primary. [F2]
- `2026-04-09T13:24:00Z` redis-cache memory 8.04%, hit ratio 14.02% (dataset lost). [F3]
- `2026-04-09T13:23:57Z` First pricing-engine 'quote computation deadline exceeded' error. [F5]
- `2026-04-09T13:30:00Z` Incident reported (SEV-2). []
- `2026-04-09T13:30:20Z` redis-cache client reconnect storm begins (15048 new connections/10s). [F11]
- `2026-04-09T14:48:00Z` Latency still recovering (pricing-engine p99 avg 1170ms; redis hit ratio 45.78%). [F1, F3]

## Recommended actions
- **[immediate]** Warm/repopulate redis-cache immediately (preload hot quote keys from market-data-service) to cut the ongoing miss storm and restore hit ratio. -- Hit ratio is still only ~46% at 14:48 and latency remains elevated; warming the cache is the direct mitigation. [F3, F4]
- **[short_term]** Investigate why redis-cache-2 failed health checks (PING timeouts) and why redis-cache-3 was mid-resync at failover time. -- The failover promoted a replica with an empty dataset; root fix is ensuring replicas are fully synced/healthy before promotion. [F2]
- **[short_term]** Add replica-sync verification/guard to the failover procedure so promotion is refused or delayed when the target replica's dataset is empty or resyncing. -- Prevents repeating the empty-replica promotion that caused this incident (same pattern as HIST-107). [F2, F12]
- **[long_term]** Add cache-miss-storm alerting on redis-cache hit_ratio and on market-data-service miss-storm log rate to catch cold-cache onset before it cascades. -- Detection lagged: miss storm began 13:22 but p99 alarm/report came later; early detection shortens impact. [F4, F10]

## Data gaps
- redis-cache has no 'restarts' metric (tc-019 not_found), so process restart count for the failed primary could not be confirmed directly.
- No infra/host telemetry for the redis-cache nodes (only cache-level metrics); the root cause of the primary's PING timeouts (host failure, network partition, OOM-kill) is not observable here.
- market-data-service log query returned truncated=true and archives rows only from 09:00 onward; message-level coverage is incomplete though the top-pattern count (204) is consistent.
- Deployment listing returned only one entry and initially partial (shard timeout); cannot fully exclude an undocumented deploy, though config-change list was complete and empty.

## Investigation stats
- latency 48.1s, LLM calls 7, tokens 83180 (in 75667 / out 7513)
- tool calls 25 (3 failed, 1 partial, 2 recovered by retry), findings 12/12 verified
