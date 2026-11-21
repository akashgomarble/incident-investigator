# Incident investigation report: INC-006
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The quote latency degradation on trading screens is best explained by loss of the quote:<n> keys in the shared redis-cache, which triggered a cache-miss storm at market-data-service and cascaded deadline-exceeded errors into pricing-engine (H1, confidence 0.60 — above threshold and clearly ahead of H2 0.30 and H3 0.22). The chain is directly observed at both ends: market-data-service began emitting "cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot" WARNs at 13:22:25Z, coincident with pricing-engine's p99 step from ~61ms to ~2225ms at 13:22Z, and pricing-engine's 192 errors all attribute the timeout to market-data-service with no accompanying CPU/memory/load change on pricing-engine itself. The change inventory is empty at every scope checked (no pricing-engine, redis-cache, market-data-service, org-wide, or config/flag change in or around the window), ruling out the deployment/config hypotheses. Confidence is medium rather than high because the cache-side mechanism (eviction vs TTL expiry vs silent failover) could not be observed — no redis-cache telemetry was retrieved — and market-data-service's own p99 could not be measured; H3, a silent infra event on the redis host, remains a live alternative with the same visible signature.

## Probable root cause
**redis-cache key loss (eviction/TTL/failover) triggers cache-miss storm at market-data-service, cascading deadline-exceeded into pricing-engine** -- infrastructure_failure in `redis-cache`, triggered by `null` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: redis-cache key loss (eviction/TTL/failover) triggers cache-miss storm at market-data-service, cascading deadline-exceeded into pricing-engine | infrastructure_failure | redis-cache | null | 0.60 (medium) | active |
| 2 | H2: Reporter's framing is misplaced: the fault is upstream of pricing-engine entirely, in the market-data-service -> redis-cache path | dependency_failure | market-data-service | null | 0.30 (low) | active |
| 3 | H3: Silent infrastructure event on the shared platform/node hosting redis-cache (failover, IOPS throttling, restart) at ~13:22Z | infrastructure_failure | redis-cache | null | 0.22 (low) | active |
| 4 | H4: Traffic-driven capacity saturation on the shared cache / pricing path (historical recurrence) | capacity_saturation | redis-cache | null | 0.06 (low) | rejected |
| 5 | H7: Reporter's suspicion: pricing-engine itself is the origin (latency in its own compute or app path) | unknown | pricing-engine | null | 0.05 (low) | rejected |
| 6 | H5: auth-service dep-5604 (Session cookie hardening, 11:30Z) degrades the shared redis-cache for all callers | bad_deployment | auth-service | dep-5604 | 0.04 (low) | rejected |
| 7 | H6: A code/config change on the quote path is the trigger | config_change | unknown | null | 0.03 (low) | rejected |

### H1: redis-cache key loss (eviction/TTL/failover) triggers cache-miss storm at market-data-service, cascading deadline-exceeded into pricing-engine
The shared redis-cache lost the quote:<n> keys (eviction under maxmemory, TTL expiry, or a silent primary failover promoting an empty replica). market-data-service then had to rebuild every quote from the exchange feed snapshot, its per-call latency rose to seconds, and pricing-engine's synchronous http call to market-data-service exceeded its deadline, producing 2-3s quote loads on trading screens. No code or config change was involved; the origin is the cache layer.

**Causal chain**
1. redis-cache loses quote:<n> keys (eviction / TTL expiry / replica promoted empty) *(assumed)*
2. market-data-service sees 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot', starting 13:22:25Z, 289 WARN lines [F11, F17]
3. Rebuilds from the exchange feed are slow, so market-data-service call latencies rise (earliest quote slowdown 6864ms) [F11, F10]
4. pricing-engine's synchronous call to market-data-service breaches its deadline: 192 ERRORs 'quote computation deadline exceeded: market-data-service took <n>ms', per-call 827-8456ms, first at 13:23:57Z [F10, F13]
5. pricing-engine p99 steps from ~60ms to >2000ms at the 13:20-13:22Z boundary and never recovers [F8, F9]
6. Trading screens (via order-service/api-gateway) show 2-3s slow quotes [F18, F1]

**Supporting evidence**
  - `F1` ✔ pricing-engine's dependency edges: caller order-service -> pricing-engine (http); pricing-engine -> market-data-service (http) and -> redis-cache (cache); market-data-service -> redis-cache (cache) and -> exchange-gateway (http). redis-cache is the only node flagged shared:true.
  - `F2` ✔ redis-cache is on both the pricing-engine and market-data-service code paths, and is the only shared component in the mapped subgraph; a redis-level fault could therefore degrade both quote pricing and market data simultaneously, matching 'multiple slow services'.
  - `F8` ✔ pricing-engine p99 latency was flat at ~58-66ms until the 13:20-13:22Z bucket, then jumped to >2000ms: 5-min buckets show 60.88ms avg at 13:15Z then 1370.73ms avg (max 2281.51) at 13:20Z and 2218.07ms at 13:25Z. This is a step change, not a ramp.
  - `F9` ✔ 2-minute resolution pins onset between 13:20Z (60.87ms avg, max 61.34) and 13:22Z (2225.2ms avg, max 2227.82): last normal bucket 13:20Z, first elevated bucket 13:22Z. After peaking ~2280ms at 13:24Z the p99 decays monotonically to 1151.82ms by 14:55Z but stays >1s, i.e. it never recovered within the window.
  - `F10` ✔ Error signature: 192 ERROR lines in pricing-engine, all one pattern 'quote computation deadline exceeded: market-data-service took <n>ms', first seen 13:23:57Z ('took 5171ms') and still firing at 14:58:47Z. The timing-out downstream call is market-data-service; per-call durations range 827ms-8456ms (e.g. 13:28:32Z took 8456ms).
  - `F11` ✔ market-data-service emits 290 WARN lines, 289 of them the single pattern 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)', first at 13:22:25Z and continuing to 14:58:22Z. Earliest quote slowdowns reported are 6864ms at 13:22:25Z. This starts ~1.5 min before pricing-engine's first ERROR and is coincident with the 13:22Z p99 step.
  - `F12` ✔ pricing-engine's own resources are NOT saturated: cpu_pct holds ~38-41% across 12:00-14:54Z (mean 39.83, max 44.48) with no change at onset; memory_pct holds ~48-52% (mean 49.07, max 54.93); request_rate is flat ~640 req/s (min 570.37, max 712.45) with no drop or spike. So latency rose ~2000ms with no accompanying CPU/memory/load change on pricing-engine.
  - `F13` ✔ Blast-radius note: the reported symptom originates in the market-data-service -> redis-cache path (cache miss storm messages name 'redis-cache' as the missing store), and all 192 pricing-engine errors attribute the timeout to market-data-service; no evidence collected of other services degrading in the window.
  - `F14` ✔ No deployment anywhere in the quote path or org-wide in 12:00-14:00Z on 2026-04-09: get_deployments (all services) returned count 0, partial:false, and get_config_changes (all services) returned count 0, partial:false. Independently reproduced this turn, closing the partial:true caveat from the earlier all-services query.
  - `F15` ✔ No change on the quote path even on a widened window: deployments scoped to redis-cache for 06:00-15:00Z = count 0, and config changes scoped to redis-cache (06:00-15:00Z) and market-data-service (08:00-14:00Z) both = count 0. So the change inventory contains no candidate at all for the affected component.
  - `F16` ✔ redis-cache is the only shared node in the subgraph (shared:true, owner platform) and has four direct cache callers: auth-service, portfolio-service, pricing-engine and market-data-service. The only deployment anywhere near the window (auth-service dep-5604 v7.2.0 11:30Z) is itself a redis-cache caller, but it landed 1h52m before the 13:22Z step and was not rolled back; no second deployment accompanies the onset.
  - `F17` ✔ Since the change inventory is empty, no recorded change timestamp aligns with the 13:22Z onset. The observed event that does align to the second is a cache-miss storm at the shared component: market-data-service WARN 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot' starting 13:22:25Z (289 lines), with an earliest quote slowdown of 6864ms — coincident with the 13:22Z p99 step and ~1.5 min before pricing-engine's first deadline-exceeded ERROR at 13:23:57Z.
  - `F18` ✔ Dependency path from the shared component to the reported symptom: redis-cache (keys quote:<n> missing) -> market-data-service rebuilds snapshot -> pricing-engine -> market-data-service (http) times out -> order-service/api-gateway trading screens show slow quotes. market-data-service also calls exchange-gateway (http), which is a downstream fallback/target of the rebuild rather than a shared component.
  - `F19` ✔ Precedent leads (not evidence): HIST-107 (2025-12-09) 'Cold cache storm after redis failover', services redis-cache + market-data-service, symptoms 'cache miss storm','quote latency', root cause 'Redis primary failure promoted an empty replica', match_score 5 — matching both the component and the exact symptom wording of the live WARNs. HIST-110 (2026-01-15) shows a silent infrastructure cause (IOPS throttling) with no deployment, i.e. an empty change inventory is not proof of no infra fault.
**Contradicting evidence**
  - `F3` ✔ Deployments scoped to pricing-engine for 10:00-14:00Z on 2026-04-09: count 0, empty list, partial:false. No pricing-engine release in the window.
  - `F4` ✔ Deployments for redis-cache and for market-data-service in the same 10:00-14:00Z window were both count 0, empty, partial:false -- no release on either dependency.
  - `F6` ✔ Config/feature-flag changes across ALL services: 0 changes for 11:00-14:00Z (partial:false) and 0 changes for the wider 08:00-15:00Z (partial:false), so no config or flag change is documented around onset.
**Assumptions**
  - The redis-cache key loss is real and unexplained by any recorded change; no telemetry from redis-cache (evictions, expired_keys, maxmemory, hit/miss ratio, failover) was retrieved, so the mechanism behind the miss storm is inferred not observed.
  - market-data-service's own latency step matches 13:22Z (query_metrics for its p99 failed, tc-013), so its degradation is inferred from log payloads rather than measured.
  - The miss storm is not merely a symptom of an even earlier upstream (exchange-gateway) failure -- exchange-gateway is only reached during the rebuild fallback, but its own health in the window is unverified.

### H2: Reporter's framing is misplaced: the fault is upstream of pricing-engine entirely, in the market-data-service -> redis-cache path
pricing-engine is a victim, not a cause. Its own CPU/memory/request-rate are flat and its only errors attribute the timeout to market-data-service. The origin should be localised to the market-data-service/redis-cache path; the fix would be there (cache warm/repair or market-data-service fallback), not in pricing-engine.

**Causal chain**
1. pricing-engine's own resources are flat across onset (cpu ~40%, mem ~49%, ~640 req/s) [F12]
2. pricing-engine's only error signature blames the downstream: 'quote computation deadline exceeded: market-data-service took <n>ms' [F10]
3. market-data-service is the first service to emit degradation messages (13:22:25Z), ~1.5 min before pricing-engine's first ERROR [F11, F17]
4. Its messages name redis-cache as the missing store, so the fault sits on the market-data-service -> redis-cache edge [F11, F13, F18]
5. Slow quotes surface to trading screens through order-service/api-gateway [F18, F1]

**Supporting evidence**
  - `F10` ✔ Error signature: 192 ERROR lines in pricing-engine, all one pattern 'quote computation deadline exceeded: market-data-service took <n>ms', first seen 13:23:57Z ('took 5171ms') and still firing at 14:58:47Z. The timing-out downstream call is market-data-service; per-call durations range 827ms-8456ms (e.g. 13:28:32Z took 8456ms).
  - `F11` ✔ market-data-service emits 290 WARN lines, 289 of them the single pattern 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)', first at 13:22:25Z and continuing to 14:58:22Z. Earliest quote slowdowns reported are 6864ms at 13:22:25Z. This starts ~1.5 min before pricing-engine's first ERROR and is coincident with the 13:22Z p99 step.
  - `F12` ✔ pricing-engine's own resources are NOT saturated: cpu_pct holds ~38-41% across 12:00-14:54Z (mean 39.83, max 44.48) with no change at onset; memory_pct holds ~48-52% (mean 49.07, max 54.93); request_rate is flat ~640 req/s (min 570.37, max 712.45) with no drop or spike. So latency rose ~2000ms with no accompanying CPU/memory/load change on pricing-engine.
  - `F13` ✔ Blast-radius note: the reported symptom originates in the market-data-service -> redis-cache path (cache miss storm messages name 'redis-cache' as the missing store), and all 192 pricing-engine errors attribute the timeout to market-data-service; no evidence collected of other services degrading in the window.
  - `F17` ✔ Since the change inventory is empty, no recorded change timestamp aligns with the 13:22Z onset. The observed event that does align to the second is a cache-miss storm at the shared component: market-data-service WARN 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot' starting 13:22:25Z (289 lines), with an earliest quote slowdown of 6864ms — coincident with the 13:22Z p99 step and ~1.5 min before pricing-engine's first deadline-exceeded ERROR at 13:23:57Z.
  - `F18` ✔ Dependency path from the shared component to the reported symptom: redis-cache (keys quote:<n> missing) -> market-data-service rebuilds snapshot -> pricing-engine -> market-data-service (http) times out -> order-service/api-gateway trading screens show slow quotes. market-data-service also calls exchange-gateway (http), which is a downstream fallback/target of the rebuild rather than a shared component.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Same underlying mechanism as H1; H2 differs only in which component is judged the fault origin (the data-serving service rather than the cache).
  - market-data-service's rebuild-from-snapshot path is the specific slow code path, not merely the client of the cache.

### H3: Silent infrastructure event on the shared platform/node hosting redis-cache (failover, IOPS throttling, restart) at ~13:22Z
An unrecorded infrastructure action on the node/platform serving redis-cache -- primary failover promoting an empty replica (HIST-107), IOPS throttling or a restart (HIST-110 pattern) -- wiped or made unreachable the quote keys at ~13:22Z. This is consistent with a completely empty change inventory and with a step change rather than a ramp. Origin is infrastructure; the cache-miss storm is the visible first symptom.

**Causal chain**
1. Infrastructure event (failover / throttling / restart) at ~13:22Z on redis-cache's host *(assumed)*
2. Capacity/keys lost -> quote:<n> lookups miss *(assumed)*
3. market-data-service cache-miss storm begins 13:22:25Z [F11]
4. Downstream deadline-exceeded in pricing-engine and slow quotes [F10, F9, F18]

**Supporting evidence**
  - `F14` ✔ No deployment anywhere in the quote path or org-wide in 12:00-14:00Z on 2026-04-09: get_deployments (all services) returned count 0, partial:false, and get_config_changes (all services) returned count 0, partial:false. Independently reproduced this turn, closing the partial:true caveat from the earlier all-services query.
  - `F15` ✔ No change on the quote path even on a widened window: deployments scoped to redis-cache for 06:00-15:00Z = count 0, and config changes scoped to redis-cache (06:00-15:00Z) and market-data-service (08:00-14:00Z) both = count 0. So the change inventory contains no candidate at all for the affected component.
  - `F16` ✔ redis-cache is the only shared node in the subgraph (shared:true, owner platform) and has four direct cache callers: auth-service, portfolio-service, pricing-engine and market-data-service. The only deployment anywhere near the window (auth-service dep-5604 v7.2.0 11:30Z) is itself a redis-cache caller, but it landed 1h52m before the 13:22Z step and was not rolled back; no second deployment accompanies the onset.
  - `F19` ✔ Precedent leads (not evidence): HIST-107 (2025-12-09) 'Cold cache storm after redis failover', services redis-cache + market-data-service, symptoms 'cache miss storm','quote latency', root cause 'Redis primary failure promoted an empty replica', match_score 5 — matching both the component and the exact symptom wording of the live WARNs. HIST-110 (2026-01-15) shows a silent infrastructure cause (IOPS throttling) with no deployment, i.e. an empty change inventory is not proof of no infra fault.
  - `F11` ✔ market-data-service emits 290 WARN lines, 289 of them the single pattern 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)', first at 13:22:25Z and continuing to 14:58:22Z. Earliest quote slowdowns reported are 6864ms at 13:22:25Z. This starts ~1.5 min before pricing-engine's first ERROR and is coincident with the 13:22Z p99 step.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The change/deployment inventories do not cover platform-level infra events, so an unrecorded event is possible but entirely unverified.
  - No redis-cache telemetry, failover flag, node event or restart marker was ever retrieved.

### H4: Traffic-driven capacity saturation on the shared cache / pricing path (historical recurrence)
Load outgrew provisioned cache or rebuild capacity, reproducing the HIST-113 'pricing-engine CPU saturation at market open' pattern: at sustained ~640 req/s the quote path saturates and the cache cannot keep entries hot, producing a miss storm and multi-second rebuilds. Origin is capacity rather than a change.

**Causal chain**
1. Request volume crosses a capacity threshold on the shared quote path *(assumed)*
2. Cache entry pressure -> evictions -> miss storm at 13:22:25Z [F11]
3. Rebuild cost pushes market-data-service beyond pricing-engine's deadline [F10]
4. Slow quotes on trading screens [F9, F18]

**Supporting evidence**
  - `F7` ✔ Historical precedent (leads only): HIST-113 (2026-01-28) 'pricing-engine CPU saturation at market open' with 'quote timeouts','cpu 99%', root cause capacity_saturation, match_score 3; HIST-107 (2025-12-09) 'Cold cache storm after redis failover' on redis-cache/market-data-service with 'cache miss storm','quote latency', match_score 2.
  - `F11` ✔ market-data-service emits 290 WARN lines, 289 of them the single pattern 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)', first at 13:22:25Z and continuing to 14:58:22Z. Earliest quote slowdowns reported are 6864ms at 13:22:25Z. This starts ~1.5 min before pricing-engine's first ERROR and is coincident with the 13:22Z p99 step.
  - `F1` ✔ pricing-engine's dependency edges: caller order-service -> pricing-engine (http); pricing-engine -> market-data-service (http) and -> redis-cache (cache); market-data-service -> redis-cache (cache) and -> exchange-gateway (http). redis-cache is the only node flagged shared:true.
**Contradicting evidence**
  - `F8` ✔ pricing-engine p99 latency was flat at ~58-66ms until the 13:20-13:22Z bucket, then jumped to >2000ms: 5-min buckets show 60.88ms avg at 13:15Z then 1370.73ms avg (max 2281.51) at 13:20Z and 2218.07ms at 13:25Z. This is a step change, not a ramp.
  - `F9` ✔ 2-minute resolution pins onset between 13:20Z (60.87ms avg, max 61.34) and 13:22Z (2225.2ms avg, max 2227.82): last normal bucket 13:20Z, first elevated bucket 13:22Z. After peaking ~2280ms at 13:24Z the p99 decays monotonically to 1151.82ms by 14:55Z but stays >1s, i.e. it never recovered within the window.
  - `F12` ✔ pricing-engine's own resources are NOT saturated: cpu_pct holds ~38-41% across 12:00-14:54Z (mean 39.83, max 44.48) with no change at onset; memory_pct holds ~48-52% (mean 49.07, max 54.93); request_rate is flat ~640 req/s (min 570.37, max 712.45) with no drop or spike. So latency rose ~2000ms with no accompanying CPU/memory/load change on pricing-engine.
  - `F19` ✔ Precedent leads (not evidence): HIST-107 (2025-12-09) 'Cold cache storm after redis failover', services redis-cache + market-data-service, symptoms 'cache miss storm','quote latency', root cause 'Redis primary failure promoted an empty replica', match_score 5 — matching both the component and the exact symptom wording of the live WARNs. HIST-110 (2026-01-15) shows a silent infrastructure cause (IOPS throttling) with no deployment, i.e. an empty change inventory is not proof of no infra fault.
**Assumptions**
  - Traffic rose enough to evict entries, but no redis-cache metrics (evictions, ops/s, memory) exist to show it.
**Rejected because:** pricing-engine request_rate is flat ~640 req/s with no spike or drop across onset (F12), the degradation is a step change not a ramp (F8, F9), and pricing-engine's own CPU/memory are unsaturated (F12) -- directly contradicting the HIST-113 saturation mechanism. The live signature matches HIST-107 (cache miss storm on redis-cache/market-data-service, match_score 5), not HIST-113.

### H7: Reporter's suspicion: pricing-engine itself is the origin (latency in its own compute or app path)
pricing-engine's own code/compute is slow, or the 2-3s load times originate in the app/client/gateway, making the pricing-engine alert a red herring.

**Causal chain**
1. pricing-engine's own compute degrades *(assumed)*
2. p99 step at 13:22Z [F8, F9]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ Error signature: 192 ERROR lines in pricing-engine, all one pattern 'quote computation deadline exceeded: market-data-service took <n>ms', first seen 13:23:57Z ('took 5171ms') and still firing at 14:58:47Z. The timing-out downstream call is market-data-service; per-call durations range 827ms-8456ms (e.g. 13:28:32Z took 8456ms).
  - `F12` ✔ pricing-engine's own resources are NOT saturated: cpu_pct holds ~38-41% across 12:00-14:54Z (mean 39.83, max 44.48) with no change at onset; memory_pct holds ~48-52% (mean 49.07, max 54.93); request_rate is flat ~640 req/s (min 570.37, max 712.45) with no drop or spike. So latency rose ~2000ms with no accompanying CPU/memory/load change on pricing-engine.
  - `F13` ✔ Blast-radius note: the reported symptom originates in the market-data-service -> redis-cache path (cache miss storm messages name 'redis-cache' as the missing store), and all 192 pricing-engine errors attribute the timeout to market-data-service; no evidence collected of other services degrading in the window.
**Assumptions**
  - That pricing-engine's flat CPU/memory and its downstream-blaming errors mask a self-inflicted slowdown.
**Rejected because:** pricing-engine's CPU (~40%), memory (~49%) and request rate (~640 req/s) are flat through onset (F12), and all 192 of its errors explicitly attribute the timeout to market-data-service (F10, F13). The only measured degradation is downstream of it.

### H5: auth-service dep-5604 (Session cookie hardening, 11:30Z) degrades the shared redis-cache for all callers
The only deployment anywhere near the window, auth-service v7.2.0 at 11:30Z, is itself a redis-cache caller; a change in its session-key access pattern could load the shared cache and degrade quote keys. Origin is the auth-service release.

**Causal chain**
1. dep-5604 ships at 11:30Z changing session cookie handling [F5, F16]
2. auth-service's new access pattern loads redis-cache *(assumed)*
3. redis-cache pressure causes quote:<n> misses *(assumed)*
4. market-data-service miss storm -> pricing-engine deadline exceeded -> slow quotes [F11, F10, F9]

**Supporting evidence**
  - `F5` ✔ The only deployment recorded anywhere in 11:00-14:00Z was dep-5604 auth-service v7.2.0 at 2026-04-09T11:30:00Z ('Session cookie hardening', SameSite=strict), status live, not rolled back -- it is not on the quote path (auth-service is not in pricing-engine's dependency graph).
  - `F16` ✔ redis-cache is the only shared node in the subgraph (shared:true, owner platform) and has four direct cache callers: auth-service, portfolio-service, pricing-engine and market-data-service. The only deployment anywhere near the window (auth-service dep-5604 v7.2.0 11:30Z) is itself a redis-cache caller, but it landed 1h52m before the 13:22Z step and was not rolled back; no second deployment accompanies the onset.
**Contradicting evidence**
  - `F5` ✔ The only deployment recorded anywhere in 11:00-14:00Z was dep-5604 auth-service v7.2.0 at 2026-04-09T11:30:00Z ('Session cookie hardening', SameSite=strict), status live, not rolled back -- it is not on the quote path (auth-service is not in pricing-engine's dependency graph).
  - `F16` ✔ redis-cache is the only shared node in the subgraph (shared:true, owner platform) and has four direct cache callers: auth-service, portfolio-service, pricing-engine and market-data-service. The only deployment anywhere near the window (auth-service dep-5604 v7.2.0 11:30Z) is itself a redis-cache caller, but it landed 1h52m before the 13:22Z step and was not rolled back; no second deployment accompanies the onset.
  - `F8` ✔ pricing-engine p99 latency was flat at ~58-66ms until the 13:20-13:22Z bucket, then jumped to >2000ms: 5-min buckets show 60.88ms avg at 13:15Z then 1370.73ms avg (max 2281.51) at 13:20Z and 2218.07ms at 13:25Z. This is a step change, not a ramp.
  - `F9` ✔ 2-minute resolution pins onset between 13:20Z (60.87ms avg, max 61.34) and 13:22Z (2225.2ms avg, max 2227.82): last normal bucket 13:20Z, first elevated bucket 13:22Z. After peaking ~2280ms at 13:24Z the p99 decays monotonically to 1151.82ms by 14:55Z but stays >1s, i.e. it never recovered within the window.
  - `F13` ✔ Blast-radius note: the reported symptom originates in the market-data-service -> redis-cache path (cache miss storm messages name 'redis-cache' as the missing store), and all 192 pricing-engine errors attribute the timeout to market-data-service; no evidence collected of other services degrading in the window.
**Assumptions**
  - auth-service's session traffic actually shares key space or resource with quote:<n> keys -- asserted nowhere in the findings.
**Rejected because:** Timing kills it: dep-5604 landed 11:30Z, 1h52m before the 13:22Z step change, with no second event at onset (F5, F16). auth-service is not in pricing-engine's dependency graph and no auth-service symptoms are reported (F5, F13, F16). A ~2-hour quiet gap is inconsistent with a deployment trigger of a step change.

### H6: A code/config change on the quote path is the trigger
A release or feature-flag flip on pricing-engine, redis-cache or market-data-service caused the latency step.

**Causal chain**
1. A change ships on the quote path just before 13:22Z *(assumed)*
2. Latency step at 13:22Z [F8, F9]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F3` ✔ Deployments scoped to pricing-engine for 10:00-14:00Z on 2026-04-09: count 0, empty list, partial:false. No pricing-engine release in the window.
  - `F4` ✔ Deployments for redis-cache and for market-data-service in the same 10:00-14:00Z window were both count 0, empty, partial:false -- no release on either dependency.
  - `F6` ✔ Config/feature-flag changes across ALL services: 0 changes for 11:00-14:00Z (partial:false) and 0 changes for the wider 08:00-15:00Z (partial:false), so no config or flag change is documented around onset.
  - `F14` ✔ No deployment anywhere in the quote path or org-wide in 12:00-14:00Z on 2026-04-09: get_deployments (all services) returned count 0, partial:false, and get_config_changes (all services) returned count 0, partial:false. Independently reproduced this turn, closing the partial:true caveat from the earlier all-services query.
  - `F15` ✔ No change on the quote path even on a widened window: deployments scoped to redis-cache for 06:00-15:00Z = count 0, and config changes scoped to redis-cache (06:00-15:00Z) and market-data-service (08:00-14:00Z) both = count 0. So the change inventory contains no candidate at all for the affected component.
**Assumptions**
  - That the change inventory is incomplete despite repeated partial:false confirmations.
**Rejected because:** Not one change candidate exists: zero deployments for pricing-engine (F3), redis-cache and market-data-service (F4), zero org-wide in 12:00-14:00Z (F14), zero config/flag changes org-wide 08:00-15:00Z (F6), and zero redis/market-data changes on a widened 06:00-15:00Z window (F15).

## Facts (verified observations)
- redis-cache is the only shared component in pricing-engine's mapped dependency subgraph and sits on both the pricing-engine and market-data-service code paths. [F1, F16]
- pricing-engine p99 latency was flat at ~58-66ms and then stepped to >2000ms at 13:22Z (last normal 2-min bucket 13:20Z at 60.87ms, first elevated 13:22Z at 2225.2ms), a step change not a ramp, with no recovery within the window (decaying to 1151.82ms by 14:55Z). [F8, F9]
- market-data-service emitted 290 WARN lines starting 13:22:25Z, 289 of them the single pattern 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot', continuing to 14:58:22Z; the earliest reported quote slowdown was 6864ms. [F11]
- pricing-engine emitted 192 ERROR lines, all of the pattern 'quote computation deadline exceeded: market-data-service took <n>ms', first at 13:23:57Z and still firing at 14:58:47Z; every error attributes the timeout to market-data-service. [F10]
- pricing-engine's own resources are not saturated: CPU ~38-41%, memory ~48-52%, request_rate flat ~640 req/s across the onset, with no change at 13:22Z. [F12]
- No deployment or config/feature-flag change exists on the quote path or org-wide in the window: 0 for pricing-engine (10:00-14:00Z), 0 for redis-cache and market-data-service (10:00-14:00Z), 0 org-wide deployments and config changes (12:00-14:00Z), 0 config/flag changes (08:00-15:00Z), and 0 on a widened 06:00-15:00Z window for redis-cache and market-data-service. [F3, F4, F6, F14, F15]
- The only deployment near the window was auth-service dep-5604 v7.2.0 at 11:30Z (Session cookie hardening), 1h52m before onset, not on the quote path, and not rolled back. [F5, F16]
- Historical precedent HIST-107 'Cold cache storm after redis failover' (2025-12-09) matches the live component (redis-cache + market-data-service) and the exact symptom wording ('cache miss storm', 'quote latency'), match_score 5; HIST-110 shows a silent infrastructure cause producing no deployment record. [F19, F7]

## Assumptions (not verified)
- Assumption: the loss of the quote:<n> keys in redis-cache is real and not explained by any recorded change. No redis-cache telemetry (evictions, expired_keys, maxmemory, hit/miss ratio, failover) was ever retrieved, so the mechanism behind the miss storm is inferred from market-data-service WARN payloads, not directly observed.
- Assumption: market-data-service's rebuild-from-snapshot path is the slow code path and its own latency step matches the 13:22Z onset — its p99 metric query failed (tc-013, 503), so this is inferred from log payloads rather than measured.
- Assumption: the miss storm is not merely a symptom of an earlier exchange-gateway failure. exchange-gateway is only reached during the rebuild fallback, and its own health in the window was never verified.
- Assumption: the change/deployment inventories do not cover platform-level infrastructure events, so a silent redis failover or node-level action at ~13:22Z cannot be excluded even though the inventories are empty.
- Assumption: mapping was limited to depth<=2 from pricing-engine and depth 1 from market-data-service, so a degraded shared component outside this subgraph could exist without appearing in the evidence.
- (demoted from facts: no verified observation backs it) The only event aligned to the second with the 13:22Z step is the market-data-service cache-miss storm at the shared redis-cache, starting ~1.5 minutes before pricing-engine's first deadline-exceeded ERROR.
- (demoted from facts: no verified observation backs it) The dependency path from the missing shared keys to the reported symptom is: redis-cache quote:<n> keys missing -> market-data-service rebuilds from exchange feed snapshot -> pricing-engine http call to market-data-service times out -> trading screens show slow quotes.

## Timeline
- `2026-04-09T11:30:00Z` auth-service v7.2.0 dep-5604 ('Session cookie hardening') deployed — the only change anywhere near the window; not on the quote path; 1h52m before onset; not rolled back. [F5, F16]
- `2026-04-09T13:20:00Z` Last normal bucket: pricing-engine p99 60.87ms avg (max 61.34ms). [F9]
- `2026-04-09T13:22:00Z` Step change: pricing-engine p99 jumps to 2225.2ms avg (max 2227.82ms); 5-min bucket average rises from 60.88ms (13:15Z) to 1370.73ms (13:20Z) to 2218.07ms (13:25Z). No corresponding change in pricing-engine CPU, memory or request rate. [F8, F9, F12]
- `2026-04-09T13:22:25Z` market-data-service begins emitting 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot' WARNs; earliest reported quote slowdown 6864ms. Coincident with the p99 step and ~1.5 min before pricing-engine's first error. [F11, F17]
- `2026-04-09T13:23:57Z` pricing-engine first ERROR: 'quote computation deadline exceeded: market-data-service took 5171ms'. [F10]
- `2026-04-09T13:24:00Z` p99 peaks ~2280ms, then decays monotonically but never recovers below 1s through end of window (1151.82ms at 14:55Z). [F9]
- `2026-04-09T14:58:22Z` market-data-service cache-miss WARNs still firing (289 lines total through 14:58:22Z). [F11]
- `2026-04-09T14:58:47Z` pricing-engine deadline-exceeded ERRORs still firing (192 lines total). [F10]

## Recommended actions
- **[immediate]** Immediately retrieve redis-cache telemetry for 13:00-14:00Z on 2026-04-09 — evicted_keys, expired_keys, keyspace hit/miss ratio, used_memory vs maxmemory, connected clients, ops/s, and any failover/role-change marker — to determine whether the quote:<n> keys were lost to eviction, TTL expiry, or a primary failover. This is the single highest-value missing measurement. -- H1 and H3 share the same visible signature (miss storm at a shared cache) and are discriminated only by the redis-side mechanism, which was never observed (GAPS: no redis-cache telemetry retrieved). [F11, F17, F19]
- **[immediate]** Warm/repair the quote:<n> keys in redis-cache (bulk re-seed from the authoritative quote source or by replaying the exchange feed snapshot) and confirm the market-data-service cache-miss WARN rate and pricing-engine p99 (>2000ms since 13:22Z) begin to fall. -- The miss storm at 13:22:25Z is the observed event aligned to the second with the p99 step; restoring key presence attacks the observed mechanism directly and is reversible. [F11, F9, F17]
- **[immediate]** Check pricing-engine-provider market-data-service and order-service/api-gateway dashboards for the same 13:20-13:24Z step, and re-run query_metrics(service=market-data-service, metric=latency_p99_ms) now that the earlier call failed with 503 (tc-013). -- Confirms the shared-component blast radius and replaces inference-from-logs with a measured market-data-service onset; also determines whether other trading-screen-facing services degraded simultaneously (unverified per GAPS). [F10, F13, F18]
- **[short_term]** Check for a platform/infrastructure event on the redis-cache host at ~13:22Z (node restart, sentinel/primary promotion, IOPS throttling, reshard, sidecar or memory-limit change) via platform event feeds, since get_deployments/get_config_changes do not expose infra actions. -- H3 (0.22) is the live alternative to H1 and HIST-107 is directly on point ('Redis primary failure promoted an empty replica', match_score 5); an empty change inventory is not proof of no infra fault (HIST-110). [F19, F15, F16]
- **[long_term]** Add redis-cache alerting and guardrails: hit/miss ratio and evicted_keys alerts, monitor maxmemory headroom, and prefer noeviction/volatile policies or a dedicated keyspace for quote:<n> so eviction cannot silently drop quote keys. -- Whatever the mechanism (eviction, TTL, failover), the shared cache losing hot keys produced a multi-second customer-visible outage; the cache layer had no distinct alert and the miss storm had to be discovered via downstream errors. [F2, F11, F16]
- **[long_term]** Make the market-data-service rebuild path survivable: bound the rebuild (cached snapshot, bounded staleness, rate limiting/queueing), and set pricing-engine's http deadline to market-data-service with a fail-fast/fallback rather than a 2-3s screen load. -- The rebuild-from-exchange-feed snapshot is what converted a cache-miss into 827ms-8456ms downstream calls and 2-3s quote loads; hardening this removes the single-point amplification. [F10, F11, F18]
- **[long_term]** When redis-cache is deliberately failed over, restarted, or memory-limit changed, require a mandatory cache warm/backfill runbook step before returning the component to service. -- HIST-107 is the same incident class (empty replica promoted after redis failover producing a cold cache storm); the runbook gap is a recurrence risk. [F19, F7]

## Data gaps
- No redis-cache telemetry was retrieved (memory, evictions, expired_keys, hit/miss ratio, ops/s, latency, failover flag) — the query budget was exhausted before querying the redis component's available metrics. This is the main limit on the conclusion: it cannot be stated from data whether the miss storm came from eviction/maxmemory pressure, TTL expiry, or a failover.
- query_metrics for market-data-service latency_p99_ms failed with 'backend unavailable (503), gave up after 3 attempts' (tc-013), so market-data-service's own latency step and its exact onset could not be measured — only inferred from log payloads.
- The first all-services deployment query for 11:00-14:00Z returned partial:true (one or more backend shards did not respond). Retries covered only pricing-engine, redis-cache and market-data-service, so deployments on api-gateway, order-service, exchange-gateway and platform/infra were never fully enumerated (tc-002 PARTIAL), leaving a change on an unmapped neighbouring service unruled-out.
- No separate feature-flag endpoint was queried; flag toggles not tracked by the config-change API remain an unanswered possibility on the redis/quote path.
- no infrastructure/maintenance-change or failover event was checked for redis-cache in the window; platform-level infra actions do not appear in get_deployments/get_config_changes.
- Log results were truncated (25 of 290 market-data-service lines; 40 of 192 pricing-engine ERROR lines) and redis-cache's own metrics were never queried, so absence of other signatures outside the sampled windows is not proven.
- Blast radius was not enumerated: other trading-screen-facing services (quote-gateway, trading UI backends) and redis-cache's own degradation were not sampled, so simultaneous impact elsewhere in the 13:22Z window is unverified.
- Dependency mapping was depth<=2 from pricing-engine and depth 1 from market-data-service; deeper redis topology (sentinel/replica nodes) and other datastores were not enumerated, so a shared component outside this subgraph could be missed.

## What would change this conclusion
- redis-cache telemetry showing no key loss, healthy hit ratio, and no evictions/expiries at 13:20-13:24Z, with the quote:<n> keys still present — this would falsify H1/H3 and point to market-data-service's own code path or to exchange-gateway (H2 in its narrowed form).
- Market-data-service p99 measured flat through 13:22Z (e.g. if tc-013 were retried successfully) — that would break the asserted pricing-engine -> market-data-service timeout chain and require re-ranking.
- Evidence of a deployment, config/feature-flag flip, or platform infra event on the quote path or on redis-cache spanning 13:00-13:30Z — the empty change inventory would be overturned and H6 (or a change-driven variant) revived.
- redis-cache logs or a failover/role-change marker showing a primary promotion or restart at ~13:22Z — this would promote H3 over H1 as the named origin, though the customer-visible mitigation path is unchanged.
- Evidence that a client-side, gateway, or order-service change rather than pricing-engine -> market-data-service is responsible for the 2-3s screen loads, which would relocate the symptom origin away from the current dependency path.
- Evidence that exchange-gateway was already failing before 13:22Z — the rebuild fallback would then be a consequence, not the cause, moving the origin upstream of the cache miss.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: The only event aligned to the second with the 13:22Z step is the market-data-ser
- fact demoted to assumption: The dependency path from the missing shared keys to the reported symptom is: red

## Investigation stats
- latency 126.06s, LLM calls 13, tokens 81046 (in 64034 / out 17012)
- tool calls 24 (1 failed, 1 partial, 0 recovered by retry), findings 19/19 verified
