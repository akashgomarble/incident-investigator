# Incident investigation report: INC-006
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Reported symptom (slow quotes, pricing-engine p99 >2s) originates in the shared redis-cache. At 13:22:10Z redis-cache-2 failed its health check and was marked primary-down; the failover at 13:22:52Z promoted replica redis-cache-3 whose dataset was empty because it was still resyncing. redis hit_ratio then collapsed from ~96% to 14% and memory from ~70% to ~8% while evictions stayed flat (~3/min) and CPU stayed flat (~22%) — i.e. the dataset vanished, it was not evicted or CPU-bound. The resulting cold cache triggered a cache-miss storm in market-data-service (204 log events), which rebuilt each quote from the exchange feed snapshot taking seconds, driving market-data-service CPU to ~90% and p99 to ~2s, which in turn caused pricing-engine quote computations to exceed deadline (p99 peaking 2309ms, ~4% errors) and slow quotes in the app. No deploys or config changes occurred for any of these services; pricing-engine and market-data-service container CPU/memory of their own stayed flat, and exchange-gateway/exchange-api were healthy — ruling out bad_deployment, config_change, dependency_failure and capacity_saturation. This matches historical incident HIST-107 exactly. Category: infrastructure_failure, root cause component: redis-cache.

## Probable root cause
**redis-cache primary failure with empty-replica promotion caused a cold cache storm in market-data-service** -- infrastructure_failure in `redis-cache` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: redis-cache primary failure with empty-replica promotion caused a cold cache storm in market-data-service | infrastructure_failure | redis-cache | - | 0.86 (high) | active |
| 2 | H2: market-data-service capacity saturation from rising request load | capacity_saturation | market-data-service | - | 0.12 (low) | rejected |
| 3 | H3: External exchange feed / exchange-gateway degradation | dependency_failure | exchange-gateway | - | 0.05 (low) | rejected |
| 4 | H4: Bad deployment or config change in pricing-engine | bad_deployment | pricing-engine | - | 0.03 (low) | rejected |

### H1: redis-cache primary failure with empty-replica promotion caused a cold cache storm in market-data-service
At 13:22Z the redis-cache primary (redis-cache-2) failed its health check and was marked down; the failover promoted redis-cache-3, whose dataset was empty because it was resyncing. The resulting cold cache triggered a cache-miss storm in market-data-service, which must rebuild each quote from the exchange feed snapshot (multi-second rebuilds). This saturated market-data-service CPU (~90%) and pushed its p99 to ~2s, causing pricing-engine quote computations to exceed deadline, degrading quotes in the app.

**Causal chain**
1. redis-cache-2 failed health check / marked primary down at 13:22:10Z [F6]
2. Failover promoted redis-cache-3 with an EMPTY dataset (replica was resyncing) [F6]
3. redis hit_ratio collapsed 96%->14% and memory 70%->8% with evictions flat (dataset emptied, not evicted) [F4, F5]
4. market-data-service cache-miss storm: rebuild each quote from exchange feed snapshot (seconds each) [F7]
5. market-data-service CPU saturated ~90% and p99 -> ~2s [F3]
6. pricing-engine quote computation exceeds deadline waiting on market-data-service [F2]
7. User-visible quote latency 2-3s and pricing-engine p99 > 2s [F1]

**Supporting evidence**
  - `F4` ✔ redis-cache hit_ratio collapses from ~96% to 14.02% in the 13:24 bucket and only slowly recovers, while redis CPU stays flat at ~22%.
  - `F5` ✔ redis-cache memory_pct collapses from ~70% to 8.04% in the 13:24 bucket while evictions_per_min stay flat at ~3/min (min 1.29, max 4.87).
  - `F6` ✔ redis-cache log shows the primary node failed a health check and a failover promoted a replica whose dataset was empty: at 13:22:10Z 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' and at 13:22:52Z 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)'.
  - `F7` ✔ market-data-service logs a 'cache miss storm' (204 occurrences, first seen 13:22:25Z): 'quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' with rebuild times of seconds, and the storm continues past 14:58Z.
  - `F3` ✔ market-data-service p99 latency also jumps from ~40ms to a peak of 1979.98ms starting in the 13:12 bucket, and its CPU rises from ~44% to ~90% peak.
  - `F2` ✔ pricing-engine errors cite downstream market-data-service: pattern 'quote computation deadline exceeded: market-data-service took <n>ms' (72 occurrences, first seen 13:23:57Z) with values up to 8456ms.
  - `F1` ✔ pricing-engine p99 latency jumps from ~60ms to a peak of 2309.91ms beginning in the 13:12-13:24 UTC bucket, with error_rate rising from ~0.2% to ~4%.
  - `F12` ✔ Historical incident HIST-107 (2025-12-09, redis-cache + market-data-service) has the same symptom set ('cache miss storm','quote latency') and root cause 'Redis primary failure promoted an empty replica'.
  - `F13` ✔ redis-cache logs also show client reconnect storms (up to '92851 new connections in 10s') during the incident, consistent with many clients reconnecting to the failed-over cache.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The redis-cache failover event is the trigger for the miss storm (timing matches within ~1 min) rather than a coincidental event.
  - The empty replica dataset was not pre-existing before 13:22Z.

### H2: market-data-service capacity saturation from rising request load
Market-data-service CPU reached ~90%, suggesting it ran out of provisioned capacity, causing slow quote rebuilds and downstream pricing-engine timeouts.

**Causal chain**
1. market-data-service CPU ~90% [F3]
2. high CPU slows quote building *(assumed)*
3. pricing-engine times out waiting [F2]

**Supporting evidence**
  - `F3` ✔ market-data-service p99 latency also jumps from ~40ms to a peak of 1979.98ms starting in the 13:12 bucket, and its CPU rises from ~44% to ~90% peak.
**Contradicting evidence**
  - `F7` ✔ market-data-service logs a 'cache miss storm' (204 occurrences, first seen 13:22:25Z): 'quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' with rebuild times of seconds, and the storm continues past 14:58Z.
  - `F8` ✔ exchange-gateway (and its external exchange-api upstream) show no degradation: p99 flat ~130ms, upstream_latency_ms flat ~120ms, upstream_error_rate flat ~0.3%, CPU ~30%, error_rate ~0.2% across the incident.
  - `F4` ✔ redis-cache hit_ratio collapses from ~96% to 14.02% in the 13:24 bucket and only slowly recovers, while redis CPU stays flat at ~22%.
  - `F5` ✔ redis-cache memory_pct collapses from ~70% to 8.04% in the 13:24 bucket while evictions_per_min stay flat at ~3/min (min 1.29, max 4.87).
  - `F9` ✔ No deployments occurred for pricing-engine, market-data-service or redis-cache on 2026-04-09; only deployment in the window is auth-service v7.2.0 at 11:30Z (unrelated). No config changes were recorded for any service.
  - `F10` ✔ pricing-engine CPU (~40%) and memory (~49%) stay flat through the incident; no container restarts for pricing-engine or market-data-service (restarts=0).
**Assumptions**
  - Request load rose organically.
**Rejected because:** market-data-service request_rate is flat (~950 req/s, min 858 max 1049) across the whole window (tc-028), so load did not increase. The CPU rise is a consequence of the cache-miss rebuild storm, not its cause. Logs explicitly name the cache as the source (F7).

### H3: External exchange feed / exchange-gateway degradation
The upstream exchange provider or exchange-gateway slowed, delaying quote rebuilds and quote reads.

**Causal chain**
1. exchange-gateway/exchange-api slow [F8]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ exchange-gateway (and its external exchange-api upstream) show no degradation: p99 flat ~130ms, upstream_latency_ms flat ~120ms, upstream_error_rate flat ~0.3%, CPU ~30%, error_rate ~0.2% across the incident.
  - `F6` ✔ redis-cache log shows the primary node failed a health check and a failover promoted a replica whose dataset was empty: at 13:22:10Z 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down' and at 13:22:52Z 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)'.
**Rejected because:** exchange-gateway p99 (~130ms), upstream_latency_ms (~120ms), upstream_error_rate (~0.3%) and CPU (~30%) are all flat across the window (tc-023, tc-026, tc-027, tc-035). The failure is internal to redis-cache.

### H4: Bad deployment or config change in pricing-engine
A recent release or flag change in pricing-engine introduced the latency regression.

**Causal chain**
1. deploy/config change to pricing-engine *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ No deployments occurred for pricing-engine, market-data-service or redis-cache on 2026-04-09; only deployment in the window is auth-service v7.2.0 at 11:30Z (unrelated). No config changes were recorded for any service.
  - `F10` ✔ pricing-engine CPU (~40%) and memory (~49%) stay flat through the incident; no container restarts for pricing-engine or market-data-service (restarts=0).
  - `F1` ✔ pricing-engine p99 latency jumps from ~60ms to a peak of 2309.91ms beginning in the 13:12-13:24 UTC bucket, with error_rate rising from ~0.2% to ~4%.
**Rejected because:** No deployments or config changes for pricing-engine in the window (tc-006, tc-005); pricing-engine CPU/memory flat and restarts=0 (tc-018, tc-017, tc-038). The latency is externally caused (downstream market-data-service, tc-011).

## Facts (verified observations)
- pricing-engine p99 jumped from ~60ms to a peak of 2309.91ms starting in the 13:12 bucket, with error_rate rising to ~4%. [F1]
- pricing-engine errors named market-data-service as the slow downstream call ('quote computation deadline exceeded: market-data-service took 5171ms'). [F2]
- market-data-service p99 rose to ~2s and its CPU reached ~90%. [F3]
- redis-cache hit_ratio collapsed from ~96% to 14.02% and memory_pct from ~70% to 8.04% while evictions stayed flat (~3/min) and CPU stayed flat (~22%). [F4, F5]
- redis-cache logs show the primary node redis-cache-2 marked down (13:22:10Z) and a failover promoting redis-cache-3 with an empty dataset (13:22:52Z). [F6]
- market-data-service logged a 'cache miss storm' (204 events) rebuilding quotes from the exchange feed snapshot in multi-second times. [F7]
- exchange-gateway and its external exchange-api upstream were flat (p99 ~130ms, upstream error ~0.3%). [F8]
- No deployments or config changes occurred for pricing-engine, market-data-service, or redis-cache in the window. [F9]
- pricing-engine CPU/memory stayed flat and there were no restarts for pricing-engine or market-data-service. [F10]
- order-service p99 stayed flat at ~150ms, so impact was confined to quote reads. [F11]
- Historical incident HIST-107 has identical symptoms and root cause ('Redis primary failure promoted an empty replica'). [F12]

## Assumptions (not verified)
- The redis failover event (13:22Z) is the cause of the miss storm, not merely coincident — supported by matching onset and by HIST-107.
- The empty replica dataset did not pre-exist before the failover.
- redis-cache has no dedicated restarts metric exposed; the failover is inferred from application-level redis logs.

## Timeline
- `2026-04-09T13:12:00Z` Latency onset: pricing-engine p99 rises from ~60ms (bucket avg 421ms, max 2227ms) and market-data-service p99 rises from ~40ms (bucket avg 352ms, max 1979ms); redis memory starts dipping (bucket avg 59%). [F1, F3, F5]
- `2026-04-09T13:22:10Z` redis-cache-2 fails health check (3 consecutive PING timeouts); marked primary as down. [F6]
- `2026-04-09T13:22:25Z` First market-data-service 'cache miss storm' log; quotes rebuilt from exchange feed snapshot in seconds. [F7]
- `2026-04-09T13:22:52Z` Failover promotes replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing). [F6]
- `2026-04-09T13:23:57Z` First pricing-engine 'quote computation deadline exceeded: market-data-service took 5171ms' error. [F2]
- `2026-04-09T13:24:00Z` Peak impact: redis hit_ratio 14.02%/memory 8.04%; market-data-service CPU ~90%; pricing-engine error_rate ~4.16%. [F4, F5, F3, F2]
- `2026-04-09T13:28:42Z` Multiple client reconnect storms to redis (up to 92851 new connections in 10s) as clients re-find the new primary. [F13]
- `2026-04-09T14:48:00Z` Partial recovery only: pricing-engine p99 still ~1170ms, redis hit_ratio only ~46% — cache still cold ~1.5h later. [F1, F4]

## Recommended actions
- **[immediate]** Warm/repopulate the redis-cache quote keyspace now (bulk pre-fetch of hot quotes from the exchange feed) to stop the ongoing miss storm. -- Cache is still cold ~1.5h later (hit_ratio only ~46%, misses still logged at 14:58Z); warming restores hit_ratio and clears the market-data-service rebuild CPU pressure. [F4, F7, F3]
- **[short_term]** Investigate and fix redis replica synchronization so promoted replicas are not empty (e.g. ensure replica is synced before it can be promoted / add warm-standby). -- The root fault is that the failover promoted a replica mid-resync with an empty dataset (F6); this is the same defect as HIST-107. [F6, F12]
- **[short_term]** Add alerting on redis hit_ratio drop and on empty-dataset-after-failover, and a circuit-breaker/limit on market-data-service snapshot rebuilds to avoid thundering-herd CPU saturation. -- Impact propagated because a cold cache caused unbounded parallel rebuilds that saturated market-data-service and cascaded into pricing-engine timeouts. [F4, F3, F7]
- **[long_term]** Review redis health-check/PING-timeout thresholds and failover policy to reduce false or premature primary-down markings. -- The trigger was 3 consecutive PING timeouts marking the primary down; tuning avoids unnecessary failovers to unsynced replicas. [F6]

## Data gaps
- No dedicated redis-cache 'restarts' or failover-count metric is exposed; the failover was established only from application-level redis logs (call tc-033).
- redis-cache has no latency_p99_ms metric (tc-010 returned not_found), so cache-level latency could not be measured directly.
- Root cause of the redis-cache-2 primary health-check failure (PING timeouts) is not visible — no host/node-level metrics for the redis node itself.
- The initial all-services deployments query returned partial:true (tc-004); it was retried and returned complete (tc-032), but if any shard remained silent a late deploy could be missed.

## Investigation stats
- latency 61.83s, LLM calls 8, tokens 130583 (in 120289 / out 10294)
- tool calls 38 (2 failed, 1 partial, 2 recovered by retry), findings 13/13 verified
