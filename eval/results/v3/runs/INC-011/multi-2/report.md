# Incident investigation report: INC-011
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
H1 (confidence 0.86, high) is the root cause: the cfg-3120 flag ramp on pricing-engine (flags.pricing.greeks_v2.rollout_pct 5 → 100 at 2026-05-20T12:04:00Z) put all pricing-engine traffic onto the CPU-bound Greeks v2 Monte-Carlo path, saturating CPU and inflating p99 from ~59ms to ~2.6s while request rate stayed flat — which broke order-service's 2500ms client timeout on its direct pricing-engine quote call. The mechanism is directly witnessed in pricing-engine's own logs (flag applied 12:04:16Z, then "GreeksV2Calculator Monte-Carlo paths=20000 took 6465ms" from 12:05:21Z and "request deadline exceeded ... calculator=GreeksV2" errors), and the ordering/scope/signature all line up: pricing-engine's CPU, p99 and 5xx jump together in the 12:04 bucket before order-service degrades in the 12:06 bucket, with every order-service error carrying the single downstream signature "pricing-engine quote call timed out after 2500ms". The competing capacity/saturation recurrence (H2) is contradicted by flat RPS at constant load plus zero restarts and zero scaling events, and the shared redis-cache origin (H3) is contradicted by flat hit ratio, evictions and client count across the onset with no cache WARN+ logs. The reporter's market-data-service protobuf deploy suspicion is cleanly rejected: its p99 stayed flat at ~35-44ms throughout, there is an 8-minute quiet gap after that deploy, and no serialization-change precedent exists.

## Probable root cause
**pricing-engine Greeks v2 feature-flag ramp to 100% causes CPU saturation and quote timeouts** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: pricing-engine Greeks v2 feature-flag ramp to 100% causes CPU saturation and quote timeouts | config_change | pricing-engine | cfg-3120 | 0.86 (high) | active |
| 2 | H2: pricing-engine CPU saturation from load outgrowing provisioned capacity (recurrence of HIST-113) | capacity_saturation | pricing-engine | null | 0.10 (low) | active |
| 3 | H3: Shared redis-cache degradation starves pricing-engine (dependency/infrastructure failure on a shared datastore) | infrastructure_failure | redis-cache | null | 0.08 (low) | active |
| 4 | H4: market-data-service protobuf serialization deploy (dep-6111) degrades its callers, cascading to order-service (reporter's suspicion) | bad_deployment | market-data-service | dep-6111 | 0.04 (low) | rejected |
| 5 | H5: order-service-local fault (deploy/config/pool exhaustion) causes quote timeouts | resource_exhaustion | order-service | null | 0.03 (low) | rejected |

### H1: pricing-engine Greeks v2 feature-flag ramp to 100% causes CPU saturation and quote timeouts
At 12:04:00Z the config change cfg-3120 (flags.pricing.greeks_v2.rollout_pct 5 → 100) ramped the Greeks v2 Monte-Carlo computation to all pricing-engine traffic. That code path is CPU-bound and expensive (per-request Monte-Carlo with paths=20000), driving pricing-engine cpu_pct from ~40% to ~98-100% and p99 from ~59ms to ~2.6s in the same 12:04 bucket — at CONSTANT request rate (~606-687 req/s, no step). order-service's quote path calls pricing-engine directly (F9), so its quote calls exceeded the 2500ms client timeout, producing the observed 'pricing-engine quote call timed out after 2500ms' errors.

**Causal chain**
1. cfg-3120 ramps flags.pricing.greeks_v2.rollout_pct 5 → 100 at 12:04:00Z on pricing-engine [F8]
2. The Greeks v2 code path is executed for all traffic and pricing-engine logs show the flag applied at 12:04:16Z, with GreeksV2Calculator Monte-Carlo slowness immediately after (first WARN 12:05:21Z 'paths=20000 took 6465ms'; 44 WARNs + 32 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' ERRORs) [F24]
3. pricing-engine CPU rises at constant load: RPS FLAT at ~606-687 req/s across 11:50-12:20Z with no step at 12:04Z (F19), while cpu_pct goes ~39-41% flat through 12:02Z → avg 68.44/max 98.65 in the 12:04 bucket → pinned ~98.7-99.6% from 12:06Z (F20). CPU rose at constant RPS. [F19, F20, F5]
4. pricing-engine p99 jumps from ~59ms to avg 1297.72 / max 2534.9ms in the same 12:04 bucket, reaching ~2.6-2.7s thereafter [F2]
5. pricing-engine itself returns ~12% 5xx server-side (0.2% → 9.75% avg in the 12:04 bucket, holding ~12.3-12.75%) [F6]
6. pricing-engine degrades in the 12:04 bucket, before order-service degrades in the 12:06 bucket [F3]
7. order-service quote calls to pricing-engine exceed the 2500ms client timeout, yielding 225 ERROR entries with signature 'pricing-engine quote call timed out after 2500ms' from 12:06:10Z onward [F1, F9]

**Supporting evidence**
  - `F8` ✔ The only config/flag change in the window is on pricing-engine: cfg-3120 key flags.pricing.greeks_v2.rollout_pct changed 5 → 100 at 2026-05-20T12:04:00Z by author pm-experiments ('Ramp Greeks v2 experiment to all traffic'). No order-service config change was recorded.
  - `F24` ✔ pricing-engine's own logs show the flag applied at 12:04:16Z and GreeksV2 Monte-Carlo slowness beginning immediately after: first WARN at 12:05:21Z 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms', 44 such WARNs plus 32 ERRORs 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (first error 12:06:41Z). First match 12:04:16Z, last 12:19:59Z (truncated=true, 30 of 77 returned).
  - `F19` ✔ pricing-engine request_rate was FLAT across the whole 11:50-12:20Z window: values oscillate ~606-687 req/s with no step at 12:04Z (11:58=606.28, 12:00=647.2, 12:02=667.24, 12:04=625.14, 12:06=666.82, 12:08=646.04, 12:10=616.42). Summary min 590.66 / max 687.48 / mean 642.59. Throughput did not increase at the flag ramp.
  - `F20` ✔ pricing-engine cpu_pct at the same 2-min resolution was ~39-41% flat through 12:02Z, then in the 12:04 bucket avg 68.44 / max 98.65 (ramping mid-bucket) and pinned at ~98.7-99.6% from 12:06Z onward (12:06=98.67, 12:08=99.34, max 100.0). The CPU ramp coincides with the 12:04Z flag change while RPS is unchanged -> CPU rose at constant load.
  - `F5` ✔ pricing-engine CPU saturates at onset: cpu_pct baseline ~39-41% through 11:56Z, then jumps to avg 91.5% / max 100.0% in the 12:04 bucket and stays pinned at ~98-99% for the rest of the window. This resource saturation coincides with the latency jump.
  - `F2` ✔ pricing-engine p99 latency was ~59ms at 12:00 and 12:02, then jumped in the 12:04 bucket to avg 1297.72 / max 2534.9 ms, reaching 2466 ms by 12:06 and ~2.7s thereafter. Baseline before onset was ~58-63ms, so 2.6s is a new regression starting between 12:02 and 12:04Z, not pre-existing.
  - `F6` ✔ pricing-engine's own server-side error_rate jumped from ~0.2% baseline to avg 9.75% (max 13.61%) in the 12:04 bucket and held ~12.3-12.75% thereafter, i.e. pricing-engine was itself returning ~12% 5xx during the incident.
  - `F3` ✔ order-service p99 was normal (~152ms) through the 12:04 bucket and only rose in the 12:06 bucket (avg 2887.47 / max 3011.61 ms). Using the 2-minute resolution, pricing-engine degraded (12:04 bucket) before order-service (12:06 bucket), consistent with order-service timeouts being a consequence of pricing-engine slowness rather than the origin.
  - `F1` ✔ order-service's first quote-timeout error is at 2026-05-20T12:06:10Z; all 225 ERROR entries share the single signature 'pricing-engine quote call timed out after 2500ms', i.e. a downstream-call timeout, not a connection-refused or serialization/parse error. Errors persist through 13:58:24Z (227 matched, 225 ERROR / 2 WARN, result truncated).
  - `F9` ✔ order-service's quote path does not call market-data-service directly: the dependency graph shows edges order-service→pricing-engine (kind http) and pricing-engine→market-data-service (kind http). market-data-service's only inbound edge from the trade path is pricing-engine.
  - `F22` ✔ pricing-engine restarts (cumulative) stayed exactly 0.0 for every 2-min bucket from 11:50Z through 12:20Z (min 0.0, max 0.0, missing_points 0), i.e. no pod restart/replacement occurred during onset, and memory_pct stayed flat ~48-51% throughout — memory pressure is not the saturation driver.
**Contradicting evidence**
  - (none)
**Assumptions**
  - No other change in the partial config-audit shard 2/3 contributed (shard 2/3 never independently confirmed)
  - The 2-minute bucket resolution cannot pin the sub-minute ordering of flag-apply vs CPU ramp; the flag-applied log line (12:04:16Z) and the CPU ramp are both within the 12:04 bucket
  - Greeks v2's specific CPU cost is inferred from its own 6465ms Monte-Carlo WARN logs rather than from a CPU profile
**Rejected because:** null

### H2: pricing-engine CPU saturation from load outgrowing provisioned capacity (recurrence of HIST-113)
pricing-engine's CPU saturation and 2.6s p99 are driven by request volume outgrowing provisioned capacity at a market-open-style traffic peak, not by any faulty change. Consistent with HIST-113 ('Record market-open volume exceeded provisioned capacity', quote timeouts + cpu 99%, resolved by raising replica count), order-service timeouts are the downstream consequence of a saturated pricing-engine that cannot absorb the load.

**Causal chain**
1. pricing-engine request volume/load exceeds provisioned capacity around 12:04Z *(assumed)*
2. pricing-engine CPU pins at ~98-100% from the 12:04 bucket onward [F5, F20]
3. pricing-engine p99 rises from ~59ms to ~2.6s and error_rate to ~12% [F2, F6]
4. Downstream order-service quote calls time out at 2500ms, producing the observed ERROR signature [F1, F3, F9]

**Supporting evidence**
  - `F13` ✔ Precedent HIST-113 (2026-01-28, 'pricing-engine CPU saturation at market open') is the closest historical match on BOTH symptom and resource signature: services=['pricing-engine'], symptoms=['quote timeouts','cpu 99%'], root_cause='Record market-open volume exceeded provisioned capacity', category=capacity_saturation, resolution='Raised replica count; pre-scale before open' (match_score=6). This mirrors the current pricing-engine symptoms (quote timeouts downstream) plus CPU saturation ~99%.
  - `F12` ✔ Historical precedents (leads only, not evidence) with similar symptoms: HIST-113 2026-01-28 'pricing-engine CPU saturation at market open' with symptoms 'quote timeouts' and root cause capacity_saturation; HIST-107 2025-12-09 'Cold cache storm after redis failover' affecting redis-cache and market-data-service with 'quote latency'. Both match the current symptom set but have different causes than a serialization change.
  - `F5` ✔ pricing-engine CPU saturates at onset: cpu_pct baseline ~39-41% through 11:56Z, then jumps to avg 91.5% / max 100.0% in the 12:04 bucket and stays pinned at ~98-99% for the rest of the window. This resource saturation coincides with the latency jump.
  - `F2` ✔ pricing-engine p99 latency was ~59ms at 12:00 and 12:02, then jumped in the 12:04 bucket to avg 1297.72 / max 2534.9 ms, reaching 2466 ms by 12:06 and ~2.7s thereafter. Baseline before onset was ~58-63ms, so 2.6s is a new regression starting between 12:02 and 12:04Z, not pre-existing.
  - `F6` ✔ pricing-engine's own server-side error_rate jumped from ~0.2% baseline to avg 9.75% (max 13.61%) in the 12:04 bucket and held ~12.3-12.75% thereafter, i.e. pricing-engine was itself returning ~12% 5xx during the incident.
**Contradicting evidence**
  - `F19` ✔ pricing-engine request_rate was FLAT across the whole 11:50-12:20Z window: values oscillate ~606-687 req/s with no step at 12:04Z (11:58=606.28, 12:00=647.2, 12:02=667.24, 12:04=625.14, 12:06=666.82, 12:08=646.04, 12:10=616.42). Summary min 590.66 / max 687.48 / mean 642.59. Throughput did not increase at the flag ramp.
  - `F20` ✔ pricing-engine cpu_pct at the same 2-min resolution was ~39-41% flat through 12:02Z, then in the 12:04 bucket avg 68.44 / max 98.65 (ramping mid-bucket) and pinned at ~98.7-99.6% from 12:06Z onward (12:06=98.67, 12:08=99.34, max 100.0). The CPU ramp coincides with the 12:04Z flag change while RPS is unchanged -> CPU rose at constant load.
  - `F8` ✔ The only config/flag change in the window is on pricing-engine: cfg-3120 key flags.pricing.greeks_v2.rollout_pct changed 5 → 100 at 2026-05-20T12:04:00Z by author pm-experiments ('Ramp Greeks v2 experiment to all traffic'). No order-service config change was recorded.
  - `F24` ✔ pricing-engine's own logs show the flag applied at 12:04:16Z and GreeksV2 Monte-Carlo slowness beginning immediately after: first WARN at 12:05:21Z 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms', 44 such WARNs plus 32 ERRORs 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (first error 12:06:41Z). First match 12:04:16Z, last 12:19:59Z (truncated=true, 30 of 77 returned).
  - `F3` ✔ order-service p99 was normal (~152ms) through the 12:04 bucket and only rose in the 12:06 bucket (avg 2887.47 / max 3011.61 ms). Using the 2-minute resolution, pricing-engine degraded (12:04 bucket) before order-service (12:06 bucket), consistent with order-service timeouts being a consequence of pricing-engine slowness rather than the origin.
  - `F22` ✔ pricing-engine restarts (cumulative) stayed exactly 0.0 for every 2-min bucket from 11:50Z through 12:20Z (min 0.0, max 0.0, missing_points 0), i.e. no pod restart/replacement occurred during onset, and memory_pct stayed flat ~48-51% throughout — memory pressure is not the saturation driver.
  - `F23` ✔ No autoscaling-scale log lines exist for pricing-engine in 11:45-12:20Z (contains='scal' returned 0 matches vs a data range 08:00-13:59Z), so there is no evidence of any scale-up/scale-down event in the window.
**Assumptions**
  - Load on pricing-engine rose independently of the 12:04 config ramp (contradicted: RPS is flat)
**Rejected because:** null

### H3: Shared redis-cache degradation starves pricing-engine (dependency/infrastructure failure on a shared datastore)
The platform-owned, shared=true redis-cache sits on pricing-engine's quote path. A redis-cache degradation or cold-cache/contention event (as in HIST-107, 'Cold cache storm after redis failover') would make pricing-engine block on cache I/O, producing CPU-busy latency inflation, elevated 5xx, and downstream order-service timeouts. Under this hypothesis the fault originates in shared infrastructure, not in pricing-engine's own code or config.

**Causal chain**
1. redis-cache (platform-owned, shared=true) is on the pricing-engine quote path [F11, F16]
2. redis-cache degrades (failover/eviction/cold-cache storm or contention) around 12:04Z — NOT observed: all three queried facets are flat through onset (hit ratio ~97% F25, evictions ~3/min F26, connected_clients ~300-335 F27), and zero WARN+ redis-cache logs in 08:00-13:59Z (F28) [F25, F26, F27, F28, F30]
3. pricing-engine blocks on cache calls, inflating CPU-busy latency to ~2.6s p99 and error_rate to ~12% *(assumed)*
4. order-service quote calls to pricing-engine exceed 2500ms and time out [F1, F9]

**Supporting evidence**
  - `F11` ✔ Shared (platform-owned, shared=true) components sit on the quote path: order-service→ledger-db (db), order-service→kafka (queue-produce), pricing-engine→redis-cache (cache). All three datastores are owned by 'platform' and marked "shared":true, so other services can affect them.
  - `F16` ✔ Dependency mapping of the current quote path confirms: pricing-engine (owner 'trading', shared=false) calls market-data-service (http) and redis-cache (cache); market-data-service in turn calls redis-cache and exchange-gateway (http). order-service calls pricing-engine (http), and api-gateway calls order-service. market-data-service and redis-cache are the only shared nodes (market-data-service shared=false per node list; redis-cache shared=true), so pricing-engine's own saturation cannot be attributed to a shared datastore contention unless redis-cache itself is degraded.
  - `F15` ✔ Second precedent lead HIST-107 (2025-12-09, 'Cold cache storm after redis failover') affected services redis-cache and market-data-service with symptom 'quote latency', root cause 'Redis primary failure promoted an empty replica', resolved by warming the cache and fixing replica sync checks. This is relevant because the shared, platform-owned redis-cache sits on the quote path: the current dependency graph shows edges pricing-engine→redis-cache (cache) and market-data-service→redis-cache (cache).
  - `F12` ✔ Historical precedents (leads only, not evidence) with similar symptoms: HIST-113 2026-01-28 'pricing-engine CPU saturation at market open' with symptoms 'quote timeouts' and root cause capacity_saturation; HIST-107 2025-12-09 'Cold cache storm after redis failover' affecting redis-cache and market-data-service with 'quote latency'. Both match the current symptom set but have different causes than a serialization change.
**Contradicting evidence**
  - `F25` ✔ redis-cache hit ratio is flat through the onset: baseline 11:50-12:02 averages ~94.7-97.2%, and the onset buckets are 12:04 avg 97.11 / max 98.46 and 12:06 avg 96.44, i.e. no cache-miss storm or drop in hit rate at 12:02-12:06Z (window min 93.83, max 98.46, mean 96.19).
  - `F26` ✔ redis-cache evictions_per_min show no eviction storm at onset: the series stays in a narrow ~2-4 keys/min band across 11:50-12:20Z (12:04 avg 2.75/max 3.35, 12:06 avg 3.75/max 3.76; window min 2.15, max 4.31, mean 3.15), with no jump at 12:02-12:06Z.
  - `F27` ✔ redis-cache connected_clients is flat through the onset with no connection spike or collapse: 314-336 clients around 12:00-12:02 (12:00 avg 324.72, 12:02 avg 326.45), 306.24 at 12:04 and 297.69 at 12:06; window range 288.17-335.37, mean 311.86.
  - `F28` ✔ There are ZERO redis-cache log entries at WARN or above for the whole 08:00-13:59Z data range (total_matched 0, returned 0, not truncated), so no redis-cache failover/restart/error event was logged in the incident window or the hours before it.
  - `F30` ✔ Across the three queried redis-cache facets (hit ratio, evictions, client count) there is no metric step at the 12:02-12:06Z onset that matches the pricing-engine latency/CPU step at 12:04Z; redis-cache is flat across the step, which is a distinctly different signature from the CPU-saturation and 5xx step seen on pricing-engine.
  - `F24` ✔ pricing-engine's own logs show the flag applied at 12:04:16Z and GreeksV2 Monte-Carlo slowness beginning immediately after: first WARN at 12:05:21Z 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms', 44 such WARNs plus 32 ERRORs 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (first error 12:06:41Z). First match 12:04:16Z, last 12:19:59Z (truncated=true, 30 of 77 returned).
**Assumptions**
  - redis-cache latency/error/failover cannot be measured from its exposed metrics (F29), so the 'healthy cache' conclusion rests on hit ratio, evictions and client count only
**Rejected because:** null

### H4: market-data-service protobuf serialization deploy (dep-6111) degrades its callers, cascading to order-service (reporter's suspicion)
market-data-service dep-6111 v5.2.0 'Switch quote serialization to protobuf' at 11:56:00Z introduced a slow/blocking encode path. Because pricing-engine calls market-data-service (F9/F16), the slow encode path stalls pricing-engine's quote handler, which in turn makes order-service quote calls time out.

**Causal chain**
1. dep-6111 v5.2.0 switches market-data-service quote serialization to protobuf at 11:56:00Z and is not rolled back [F7]
2. The protobuf encode path is slow/blocking for the quote payloads market-data-service serves *(assumed)*
3. market-data-service latency rises at onset *(assumed)*
4. pricing-engine, which calls market-data-service, blocks on those calls and its p99 rises to ~2.6s [F9, F16]
5. order-service quote calls to pricing-engine exceed 2500ms and time out [F1, F9]

**Supporting evidence**
  - `F7` ✔ Only one deployment occurred across ALL services in the 11:30–12:30Z window and in the wider 11:00–14:00Z window: market-data-service dep-6111 v5.2.0 'Switch quote serialization to protobuf' at 2026-05-20T11:56:00Z by author v.rao; its status is 'live' and 'rolled_back_at':null, i.e. it has NOT been rolled back.
  - `F9` ✔ order-service's quote path does not call market-data-service directly: the dependency graph shows edges order-service→pricing-engine (kind http) and pricing-engine→market-data-service (kind http). market-data-service's only inbound edge from the trade path is pricing-engine.
  - `F16` ✔ Dependency mapping of the current quote path confirms: pricing-engine (owner 'trading', shared=false) calls market-data-service (http) and redis-cache (cache); market-data-service in turn calls redis-cache and exchange-gateway (http). order-service calls pricing-engine (http), and api-gateway calls order-service. market-data-service and redis-cache are the only shared nodes (market-data-service shared=false per node list; redis-cache shared=true), so pricing-engine's own saturation cannot be attributed to a shared datastore contention unless redis-cache itself is degraded.
**Contradicting evidence**
  - `F4` ✔ market-data-service p99 latency stayed flat at ~35-44ms across the entire 11:00-13:59Z window (mean 39.99, min 35.28, max 43.75), showing no rise at onset (12:04 bucket = 39.15ms, 12:06-ish buckets ~39-40ms). It is NOT part of the latency blast radius despite the report's suggestion of a serialization change there.
  - `F2` ✔ pricing-engine p99 latency was ~59ms at 12:00 and 12:02, then jumped in the 12:04 bucket to avg 1297.72 / max 2534.9 ms, reaching 2466 ms by 12:06 and ~2.7s thereafter. Baseline before onset was ~58-63ms, so 2.6s is a new regression starting between 12:02 and 12:04Z, not pre-existing.
  - `F3` ✔ order-service p99 was normal (~152ms) through the 12:04 bucket and only rose in the 12:06 bucket (avg 2887.47 / max 3011.61 ms). Using the 2-minute resolution, pricing-engine degraded (12:04 bucket) before order-service (12:06 bucket), consistent with order-service timeouts being a consequence of pricing-engine slowness rather than the origin.
  - `F14` ✔ No historical incident in the corpus was caused by a market-data-service serialization/encoding change. The query 'serialization encoding change protobuf market-data-service' returned only 2 incidents, both unrelated to serialization: HIST-107 (redis failover) and HIST-113 (capacity saturation). The nearest encoding-change analogues are HIST-101 (bcrypt cost factor raised in a release) and HIST-109 (JWT library upgrade changed clock-skew default) — both root-caused as bad_deployment and resolved by rolling back the release.
  - `F24` ✔ pricing-engine's own logs show the flag applied at 12:04:16Z and GreeksV2 Monte-Carlo slowness beginning immediately after: first WARN at 12:05:21Z 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms', 44 such WARNs plus 32 ERRORs 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (first error 12:06:41Z). First match 12:04:16Z, last 12:19:59Z (truncated=true, 30 of 77 returned).
**Assumptions**
  - An encode-path regression would show up in market-data-service's own p99 (contradicted: flat ~35-44ms)
  - The 8-minute gap between the 11:56 deploy and the 12:04 onset reflects delayed activation rather than irrelevance
**Rejected because:** market-data-service p99 stayed flat at ~35-44ms across the entire 11:00-13:59Z window including the onset buckets (F4), so it is not in the latency blast radius; there is an 8-minute quiet gap between the 11:56 deploy and the 12:04 pricing-engine jump (F2/F7); no historical precedent exists for a market-data-service serialization change causing timeouts (F14); and pricing-engine's own logs directly attribute the slowness to GreeksV2 Monte-Carlo, not to a downstream call (F24). One caveat: market-data-service error_rate/CPU were never queried, so this rejection rests mainly on the p99 evidence.

### H5: order-service-local fault (deploy/config/pool exhaustion) causes quote timeouts
An order-service-local change or resource exhaustion (thread pool, db connection pool to shared ledger-db, or timeout configuration) shortly before onset causes quote-request timeouts regardless of downstream latency; the market-data-service correlation is spurious.

**Causal chain**
1. order-service thread/connection pool exhausts around 12:06Z *(assumed)*
2. Quote requests queue and time out at the 2500ms client timeout [F1]
3. order-service p99 rises to ~2.9s while its own downstream is slow [F3]

**Supporting evidence**
  - `F17` ✔ A config/flag-change precedent pattern exists alongside the deployment-rollback pattern: HIST-115 (2025-09-25, 'order-service connection pool misconfiguration') has symptoms 'connection is not available','request timed out','pool exhausted', root cause 'Datasource pool size overridden by a wrong environment profile', category config_change, resolution 'Reverted config; added validation'. This shows config/flag reverts (not just code deploys) are an established mitigation for timeout incidents on this platform.
  - `F11` ✔ Shared (platform-owned, shared=true) components sit on the quote path: order-service→ledger-db (db), order-service→kafka (queue-produce), pricing-engine→redis-cache (cache). All three datastores are owned by 'platform' and marked "shared":true, so other services can affect them.
**Contradicting evidence**
  - `F3` ✔ order-service p99 was normal (~152ms) through the 12:04 bucket and only rose in the 12:06 bucket (avg 2887.47 / max 3011.61 ms). Using the 2-minute resolution, pricing-engine degraded (12:04 bucket) before order-service (12:06 bucket), consistent with order-service timeouts being a consequence of pricing-engine slowness rather than the origin.
  - `F8` ✔ The only config/flag change in the window is on pricing-engine: cfg-3120 key flags.pricing.greeks_v2.rollout_pct changed 5 → 100 at 2026-05-20T12:04:00Z by author pm-experiments ('Ramp Greeks v2 experiment to all traffic'). No order-service config change was recorded.
  - `F1` ✔ order-service's first quote-timeout error is at 2026-05-20T12:06:10Z; all 225 ERROR entries share the single signature 'pricing-engine quote call timed out after 2500ms', i.e. a downstream-call timeout, not a connection-refused or serialization/parse error. Errors persist through 13:58:24Z (227 matched, 225 ERROR / 2 WARN, result truncated).
  - `F7` ✔ Only one deployment occurred across ALL services in the 11:30–12:30Z window and in the wider 11:00–14:00Z window: market-data-service dep-6111 v5.2.0 'Switch quote serialization to protobuf' at 2026-05-20T11:56:00Z by author v.rao; its status is 'live' and 'rolled_back_at':null, i.e. it has NOT been rolled back.
  - `F24` ✔ pricing-engine's own logs show the flag applied at 12:04:16Z and GreeksV2 Monte-Carlo slowness beginning immediately after: first WARN at 12:05:21Z 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms', 44 such WARNs plus 32 ERRORs 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (first error 12:06:41Z). First match 12:04:16Z, last 12:19:59Z (truncated=true, 30 of 77 returned).
**Assumptions**
  - An unrecorded order-service config change exists in the partial config-audit shard 2/3
  - order-service's own resources (not queried) are saturated
**Rejected because:** No order-service deploy or config change was recorded in the window (F7, F8; only cfg-3120 on pricing-engine), the error signature is explicitly a downstream-call timeout to pricing-engine rather than a local pool/connection error (F1 vs HIST-115's 'connection is not available'), pricing-engine degraded before order-service (F3), and pricing-engine logs show its own internal GreeksV2 computation exceeding the deadline (F24), placing the origin downstream of order-service.

## Facts (verified observations)
- order-service's first quote-timeout error is at 2026-05-20T12:06:10Z; all 225 ERROR entries share the single signature 'pricing-engine quote call timed out after 2500ms' — a downstream-call timeout, not a connection or serialization error — persisting through 13:58:24Z. [F1]
- pricing-engine p99 latency was ~59ms at 12:00–12:02Z, then jumped in the 12:04 bucket to avg 1297.72 / max 2534.9 ms, reaching 2466 ms by 12:06 and ~2.7s thereafter (baseline ~58–63ms). [F2]
- order-service p99 stayed normal (~152ms) through the 12:04 bucket and only rose in the 12:06 bucket (avg 2887.47 / max 3011.61 ms), i.e. pricing-engine degraded before order-service. [F3]
- market-data-service p99 stayed flat at ~35-44ms across the entire 11:00–13:59Z window (12:04 bucket = 39.15ms), so it is not in the latency blast radius. [F4]
- pricing-engine cpu_pct baseline was ~39-41% through 11:56Z, then jumped to avg 91.5% / max 100.0% in the 12:04 bucket and stayed pinned at ~98-99% for the rest of the window. [F5]
- pricing-engine's own server-side error_rate jumped from ~0.2% baseline to avg 9.75% (max 13.61%) in the 12:04 bucket and held ~12.3-12.75% thereafter. [F6]
- Only one deployment occurred across all services in 11:00–14:00Z: market-data-service dep-6111 v5.2.0 'Switch quote serialization to protobuf' at 11:56:00Z by v.rao, still live and never rolled back. [F7]
- The only config/flag change in the window is on pricing-engine: cfg-3120 key flags.pricing.greeks_v2.rollout_pct changed 5 → 100 at 12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'); no order-service config change was recorded. [F8]
- order-service's quote path calls pricing-engine directly (http edge order-service→pricing-engine) and does not call market-data-service directly; pricing-engine→market-data-service is the only other edge. [F9]
- pricing-engine request_rate was flat at ~606-687 req/s across 11:50–12:20Z with no step at 12:04Z (12:02=667.24, 12:04=625.14, 12:06=666.82); throughput did not increase at the flag ramp. [F19]
- pricing-engine cpu_pct at 2-min resolution was ~39-41% flat through 12:02Z, then in the 12:04 bucket avg 68.44 / max 98.65 and pinned at ~98.7-99.6% from 12:06Z onward — CPU rose at constant load. [F20]
- pricing-engine restarts (cumulative) stayed exactly 0.0 for every 2-min bucket 11:50–12:20Z and memory_pct stayed flat ~48-51%, so restart or memory pressure is not the saturation driver. [F22]
- No autoscaling/scale log lines exist for pricing-engine in 11:45–12:20Z (contains='scal' returned 0 matches against a data range of 08:00–13:59Z). [F23]
- pricing-engine logs show the flag applied at 12:04:16Z with GreeksV2 slowness immediately after: first WARN at 12:05:21Z 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms', 44 such WARNs plus 32 ERRORs 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (first error 12:06:41Z). [F24]
- redis-cache hit ratio is flat through onset (12:04 avg 97.11, 12:06 avg 96.44; window mean 96.19), with no cache-miss storm. [F25]
- redis-cache evictions_per_min stay in a narrow ~2-4 keys/min band across 11:50–12:20Z with no jump at 12:02–12:06Z. [F26]
- redis-cache connected_clients is flat through onset (306.24 at 12:04, 297.69 at 12:06; window mean 311.86) with no connection spike or collapse. [F27]
- There are zero redis-cache log entries at WARN or above for the whole 08:00–13:59Z range (total_matched 0), so no failover/restart/error event was logged. [F28]
- redis-cache exposes no latency, connection-error or failover/restart metric (only connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct); the instrumented facets are consistent with a healthy cache. [F29]
- No historical incident in the corpus was caused by a market-data-service serialization/encoding change; the 'serialization' query returned only HIST-107 (redis failover) and HIST-113 (capacity saturation). [F14]
- HIST-113 (2026-01-28, 'pricing-engine CPU saturation at market open') is the closest historical match on both symptom and resource signature (quote timeouts + cpu 99%, root cause 'market-open volume exceeded provisioned capacity', resolved by raising replica count). [F13]
- A config/flag-revert precedent exists on this platform: HIST-115 (2025-09-25, order-service connection-pool misconfiguration, config_change category) was resolved by reverting config. [F17]
- pricing-engine exposes no replica_count and no queue_depth/in-flight metric — only cpu_pct, error_rate, latency_p99_ms, memory_pct, request_rate, restarts. [F21]

## Assumptions (not verified)
- No other change in the partial config-audit shard 2/3 contributed — shard 2/3 never independently confirmed (F8 query was partial on first attempt).
- The 2-minute bucket resolution cannot pin the sub-minute ordering of flag-apply vs CPU ramp; the flag-applied log line (12:04:16Z) and the CPU ramp both fall in the 12:04 bucket (F24, F20).
- Greeks v2's specific CPU cost is inferred from its own 6465ms Monte-Carlo WARN logs rather than from a CPU profile (F24).
- The stated confidences and ranking are taken as fixed from the reviewed analysis; this report does not re-rank hypotheses.
- (demoted from facts: no verified observation backs it) Across the three queried redis-cache facets (hit ratio, evictions, client count) there is no metric step at the 12:02–12:06Z onset matching the pricing-engine step at 12:04Z.

## Timeline
- `2026-05-20T11:56:00Z` Only deployment in the window: market-data-service dep-6111 v5.2.0 'Switch quote serialization to protobuf' goes live (never rolled back). [F7]
- `2026-05-20T12:02:00Z` Last clean pricing-engine bucket: p99 ~59ms, cpu_pct ~39-41%, request_rate ~667 req/s, order-service p99 normal. [F2, F19, F20, F3]
- `2026-05-20T12:04:00Z` Config change cfg-3120 ramps flags.pricing.greeks_v2.rollout_pct 5 → 100 on pricing-engine. [F8]
- `2026-05-20T12:04:00Z` Same 12:04 bucket: pricing-engine p99 jumps to avg 1297.72 / max 2534.9 ms, cpu_pct ramps to avg 68.44 / max 98.65, error_rate rises to avg 9.75% (max 13.61%) — all at flat request rate (~625 req/s). [F2, F5, F6, F20, F19]
- `2026-05-20T12:04:16Z` pricing-engine logs show the Greeks v2 flag applied; first GreeksV2 log match. [F24]
- `2026-05-20T12:05:21Z` First pricing-engine WARN: 'GreeksV2Calculator: Monte-Carlo paths=20000 took 6465ms' (44 such WARNs follow). [F24]
- `2026-05-20T12:06:00Z` pricing-engine CPU pinned at ~98.7-99.6% and stays there; order-service p99 rises to avg 2887.47 / max 3011.61 ms (its first degraded bucket). [F20, F3]
- `2026-05-20T12:06:10Z` First order-service quote-timeout error: 'pricing-engine quote call timed out after 2500ms'. [F1]
- `2026-05-20T12:06:41Z` First pricing-engine ERROR 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' (32 such errors). [F24]
- `2026-05-20T12:16:00Z` Incident INC-011 [SEV-2] reported; order-service quote timeouts ongoing, pricing-engine p99 2.6s. [F1]
- `2026-05-20T13:58:24Z` Latest order-service quote-timeout error in the retrieved sample (errors persist through the window). [F1]

## Recommended actions
- **[immediate]** Roll back the pricing-engine flag: revert cfg-3120 flags.pricing.greeks_v2.rollout_pct from 100 to 5 (the pre-incident value), then watch pricing-engine cpu_pct, latency_p99_ms and error_rate and order-service quote errors return to baseline. -- cfg-3120 is the only change on pricing-engine and the CPU/p99/5xx step lands in the same 12:04 bucket as the flag apply, at constant request rate (F8, F2, F5, F6, F19, F20); GreeksV2 Monte-Carlo slowness in logs begins immediately after the flag applies (F24). A config/flag revert is an established mitigation for this platform (F17). [F8, F24, F2, F5, F6, F19, F20, F17]
- **[immediate]** Confirm recovery against explicit criteria for at least 15 minutes after the revert: pricing-engine cpu_pct back to ~39-41%, p99 back to ~60ms, error_rate back to ~0.2%, and zero new order-service 'pricing-engine quote call timed out after 2500ms' errors. -- Establishes that the fix is causal rather than coincidental; baseline values are measured (F2, F5, F6) and the failure signature is unambiguous (F1). [F2, F5, F6, F1]
- **[immediate]** While mitigation is in flight, pull the discriminating telemetry that was never queried: order-service cpu_pct / memory_pct / db_pool_in_use / thread-pool, market-data-service cpu_pct / error_rate, and redis-cache cpu_pct / memory_pct (plus ledger-db and kafka series if available). -- These facets were not retrieved (budget exhausted) and are the cheapest way to close the remaining blast-radius and shared-infrastructure alternatives if the revert does not fully restore service. [F11, F29]
- **[short_term]** Do not roll back market-data-service dep-6111 on the basis of this incident; keep the protobuf serialization change deployed and monitor its p99 separately. -- market-data-service p99 stayed flat at ~35-44ms through the onset (F4), there is an 8-minute quiet gap after its 11:56 deploy (F7, F2), no serialization-change precedent exists (F14), and pricing-engine's logs attribute the slowness to its own GreeksV2 computation (F24). [F4, F7, F14, F24]
- **[short_term]** Re-ramp Greeks v2 cautiously: cap rollout_pct at the pre-incident value and step it up in small increments (e.g. 5 → 10 → 25) with CPU and p99 gates between steps, validating that the CPU cost per request stays inside budget. -- The change itself is a legitimate experiment; the harm came from putting 100% of CPU-bound Monte-Carlo traffic (paths=20000) on all instances at once (F5, F20, F24). [F5, F20, F24]
- **[long_term]** Add CPU/headroom-based protection for the Greeks v2 path: a rollout kill-switch bound to pricing-engine cpu_pct (auto-disable or throttle above a threshold) and an autoscaling/HPA policy keyed on cpu_pct for pricing-engine. -- CPU went to 98-100% with zero restarts and zero scaling events in the window (F5, F20, F22, F23), so nothing in the platform stopped the saturation; a guard rail plus scaling would bound blast radius. Note replica/queue metrics are not exposed (F21), so scaling signals must come from cpu_pct or the orchestrator. [F5, F20, F22, F23, F21]
- **[long_term]** Reduce Greeks v2 unit cost or add capacity planning for the Monte-Carlo path (lower paths, caching, or dedicated capacity) so a full-traffic ramp cannot exhaust the pod CPU budget. -- The witnessed cost is a 6465ms Monte-Carlo call against a 2000ms deadline (F24); the per-request cost is what makes the 100% ramp catastrophic at constant load (F19, F20). [F24, F19, F20]
- **[long_term]** Fix the config-audit tooling so shard 2/3 responds, and re-run the 11:30–12:30Z config-change query to confirm cfg-3120 was the only change in the window. -- The first config-change query was partial (shard 2/3 did not respond), so an additional order-service or shared-infra flag change in that window cannot be fully excluded (F8). [F8]
- **[long_term]** Extend redis-cache instrumentation with latency/connection-error/failover metrics, and add baseline noise-floor alerting on pricing-engine cpu_pct / p99 and order-service downstream-timeout rate so a step like this pages earlier than a reported SEV-2. -- redis-cache has no latency/error/failover metric, so the healthy-cache conclusion rests only on hit ratio, evictions and client count (F29, F25, F26, F27, F28); and the pricing-engine CPU/p99 step was detectable purely from existing metrics (F2, F5, F20) yet reached a human report. [F29, F25, F26, F27, F28, F2, F5, F20]

## Data gaps
- order-service cpu_pct/memory_pct/db_pool_in_use/thread-pool and market-data-service cpu_pct/error_rate were never queried, and no other services were enumerated for degradation in 12:00–13:59Z — blast-radius completeness and the order-service/market-data-service resource picture remain unverified.
- order-service ERROR log results were truncated and not re-queried for a second error signature; only the timeout pattern was observed in the returned sample, so additional error signatures cannot be fully excluded.
- pricing-engine's latency jump is bounded to between 12:02 and 12:04Z, not pinned to an exact second, and the first slow request sub-minute timing is unresolved.
- The first config-change query returned partial=true (config-audit shard 2/3 did not respond); the retry returned only cfg-3120 but shard 2/3 content was never independently confirmed, so additional config/flag changes in 11:30–12:30Z cannot be fully ruled out.
- No telemetry was queried for ledger-db, kafka or exchange-gateway, and no shared-component change (DB/cache/queue/infra) was surfaced — those shared components cannot be ruled in or out.
- redis-cache exposes no latency, connection-error or failover/restart metric, so cache latency/errors at onset cannot be observed; the flat-cache conclusion rests on hit ratio, evictions and client count only.
- pricing-engine exposes no replica_count/queue_depth metric and no autoscaling event source was queried, so 'no scale-up' rests on zero restarts plus zero 'scal' log matches.
- Per-instance breakdown of cpu_pct/latency/error_rate across pricing-engine replicas was not obtainable (single aggregated series), so 'all instances saturated at once' vs progressive propagation is unresolved.
- The contains='greeks' log query was truncated (30 of 77 returned), so the full per-instance/time distribution of GreeksV2 events was not enumerable.
- Historical precedents are summaries only, with no numeric timelines or metric values, so precedent matching is qualitative (symptom/category) only.
- A precedent lookup for shared redis-cache contention / connection-pool exhaustion was never executed (budget exhausted).

## What would change this conclusion
- Reverting cfg-3120 (rollout_pct 100 → 5) fails to bring pricing-engine CPU, p99 and error_rate back to baseline — that would refute H1 and point back at an unmeasured origin (H2 capacity, H3 shared infrastructure, or an unrecorded change in the config-audit shard 2/3).
- pricing-engine request_rate or concurrent load is found to have stepped up at 12:04Z in higher-resolution or per-instance data (contradicting flat ~590-687 req/s), which would revive the H2/HIST-113 capacity-saturation mechanism.
- redis-cache latency, connection errors or a failover event are observed at 12:02–12:06Z through a source not covered by its current metrics (e.g. orchestrator/node telemetry), which would revive H3.
- Evidence emerges that market-data-service's protobuf encode path blocks on its callers (e.g. pricing-engine thread dumps showing time blocked in the market-data-service client around 12:04–12:06Z), which would revive H4.
- Additional config/flag changes are found in the non-responding config-audit shard 2/3 covering 11:30–12:30Z that plausibly explain the pricing-engine CPU step without the Greeks v2 ramp.
- GreeksV2 logs are shown to precede the 12:04:16Z flag-apply line (e.g. Monte-Carlo WARNs before the flag change), which would break the flag-apply → slow-computation causal ordering.

## Integrity warnings (set by code)
- fact demoted to assumption: Across the three queried redis-cache facets (hit ratio, evictions, client count)

## Investigation stats
- latency 174.81s, LLM calls 22, tokens 171080 (in 138570 / out 32510)
- tool calls 38 (6 failed, 1 partial, 0 recovered by retry), findings 30/30 verified
