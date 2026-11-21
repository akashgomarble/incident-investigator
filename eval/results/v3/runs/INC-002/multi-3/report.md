# Incident investigation report: INC-002
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause is cfg-2207: the cost-optimisation profile 'small' applied at 2026-03-10T09:52:00Z cut portfolio-service db.pool.max_size from 50 to 5, exhausting the Hikari pool and stalling /v1/portfolio/*, which surfaced at api-gateway as upstream-timeout 504s (confidence 0.78, high). The chain is directly observed: pool usage pinned at 5.0 (active=5=total, idle=0, waiting=43) with explicit 'could not acquire JDBC connection within 3000ms' errors, simultaneous with portfolio-service p99 jumping from ~180ms to ~4.2s at 09:52-09:54 and gateway p99 stepping from ~200ms to ~4.3s. The reported suspect — the 08:00Z envoy 1.30 / HTTP-2-to-upstream deploy — is effectively excluded: it preceded onset by 1h48m with no latent step, and order-service, the other h2 upstream from the same deploy, stayed flat (p99 ~149ms, error ~0.2%) throughout. The gateway is the propagation surface, not the origin; shared components ledger-db and redis-cache show no degradation signal at onset.

## Probable root cause
**portfolio-service DB connection pool cut to 5 (cfg-2207) exhausts the pool, stalling /v1/portfolio/* and surfacing as gateway 504s** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.78, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service DB connection pool cut to 5 (cfg-2207) exhausts the pool, stalling /v1/portfolio/* and surfacing as gateway 504s | config_change | portfolio-service | cfg-2207 | 0.78 (high) | active |
| 2 | H2: Gateway envoy 1.30 / HTTP-2-to-upstream upgrade (dep-5220) causes upstream stalls on portfolio-service, producing p99 step change and 504s | bad_deployment | api-gateway | dep-5220 | 0.08 (low) | active |
| 3 | H4: Capacity saturation: gateway/upstream throughput trend outgrows provisioned capacity, no single triggering change | capacity_saturation | api-gateway | null | 0.02 (low) | active |
| 4 | H3: Shared-component fault: ledger-db failover/saturation (or redis-cache) on the portfolio path stalls portfolio-service then the gateway | infrastructure_failure | ledger-db | null | 0.03 (low) | rejected |

### H1: portfolio-service DB connection pool cut to 5 (cfg-2207) exhausts the pool, stalling /v1/portfolio/* and surfacing as gateway 504s
A cost-optimisation profile 'small' applied at 09:52Z reduced portfolio-service db.pool.max_size from 50 to 5 (cfg-2207). The pool (HikariPool-1) had been running at ~12 connections against a 50-slot pool with zero saturation, and never saturated on the old pool. Immediately after the cut it pins at the new ceiling of 5 (active=5=total, idle=0, waiting=43), requests block on JDBC acquisition and exceed the 3000ms timeout, portfolio-service returns 503/slow responses, and api-gateway's upstream calls to portfolio-service time out, producing the p99 step change and 504s on /v1/portfolio/*. The gateway is the propagation surface, not the origin.

**Causal chain**
1. infra-automation applies profile 'small' to portfolio-service datasource: db.pool.max_size 50 -> 5 at 09:52:00Z [F2, F13]
2. Pre-cut the pool was healthy: db_pool_in_use steady ~11.5-12.7 on a 50-slot pool through 09:52, no pre-cut elevation, no saturation (active never near 50) [F26, F27]
3. At 1-min granularity the pool metric steps 11.89 -> 5.0 between 09:52 and 09:53, i.e. the ceiling change takes effect exactly at the config apply [F27, F30]
4. Immediately after, the pool pins at the new ceiling: total=5, active=5, idle=0, waiting=43; waiting counts collapse onto the 5-slot pool [F12]
5. Threads block on connection acquisition and hit 'could not acquire JDBC connection within 3000ms' / 'Connection is not available... after 3000ms'; first ERROR 09:53:19Z [F12]
6. portfolio-service's own latency step and 5xx onset follow within ~1-2 min (p99 elevated in the 09:52 bucket; error_rate first elevated 09:54 bucket, ~22%) [F28, F29, F30, F22]
7. Gateway calls to the slow portfolio-service exceed upstream timeout -> single signature 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' (first 09:53:53Z) [F11, F19]
8. Gateway p99 steps from ~207ms to ~3.1-4.3s and error_rate from ~0.2% to ~7.2%; users see portfolio page timeouts and 504s [F7, F9, F23]
9. Same failure shape (wrong profile overriding pool size -> pool exhausted -> timeouts, no reset/refused signature) as HIST-115 [F15, F5]

**Supporting evidence**
  - `F13` ✔ The only change near onset is portfolio-service config cfg-2207: db.pool.max_size 50 -> 5, changed_at 2026-03-10T09:52:00Z by infra-automation, description 'Apply cost-optimisation profile small to service datasource'. It is the closest change in time to the 09:48-09:51Z onset and to the 09:53:19Z first pool-exhaustion ERROR (~1-2 min after).
  - `F12` ✔ portfolio-service is degraded with DB connection-pool exhaustion at the same time: 503 ERRORs, first match 09:53:19Z. Signatures: 'could not acquire JDBC connection within 3000ms' (270) and 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' (233, first_seen 09:54:29Z). Pool is fully saturated (active=5=total, idle=0) with dozens of waiters.
  - `F11` ✔ The gateway error signature is a single pattern: 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504'. 252 WARN occurrences, first seen 2026-03-10T09:53:53Z, continuing to 11:58:44Z. This is a timeout to the upstream, not connection-refused.
  - `F7` ✔ api-gateway p99 latency shows a step change from a ~200-215ms baseline to multi-second values. 10-min buckets: 09:40=206.74ms, 09:50=3119.97ms (max 4624.18), 10:00=4328.65ms; remains ~4300ms through 11:50 (4296.33ms).
  - `F8` ✔ Zoomed to 3-min buckets, gateway p99 is normal through 09:48 (206.71ms) and first jumps in the 09:51 bucket (avg 1606.91ms, max 4418.76ms), then 09:54=4304.85ms. Onset is therefore between 09:48 and 09:51, not before.
  - `F9` ✔ Gateway error_rate steps in the same window: baseline ~0.17-0.25% through 09:40, then 09:50=4.44% (max 7.74) and steady ~7.1-7.4% from 10:00 onward through 11:50 (7.22%).
  - `F5` ✔ Competing precedent HIST-115 (2025-09-25): 'order-service connection pool misconfiguration', symptoms 'connection is not available', 'request timed out', 'pool exhausted', root_cause 'Datasource pool size overridden by a wrong environment profile', category config_change, resolution 'Reverted config' -- matches the 09:52Z portfolio-service db.pool.max_size 50->5 'profile small' change in shape. HIST-102 (2025-09-02) also shows portfolio page timeouts + 504 at gateway caused by ledger-db failover, a shared component on this path.
  - `F15` ✔ The gateway failure signature is an upstream timeout, not a connection reset: 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' (first 09:53:53Z). This matches HIST-115's 'connection is not available'/'pool exhausted' shape (config_change via wrong profile) rather than HIST-116's HTTP/2 resets from an envoy upgrade — no reset/refused signature was observed on the gateway.
  - `F17` ✔ Widening the portfolio-service baseline to 2026-03-01T00:00Z-2026-03-10T11:59Z still returns only one config change (cfg-2207) and zero deployments — the pool cut is the sole portfolio-service change in ten days, so no earlier same-service change can explain onset.
  - `F18` ✔ The 07:00Z order-service change (log.sampling_rate 0.1->0.2) is logging-only and on a different service off the portfolio path; the pre-window baseline 2026-03-09T18:00Z-06:00Z had zero changes, so the window contains exactly three changes total. Change history alone cannot rule out dep-5220, but no second change aligns with the 09:51Z onset as closely as cfg-2207.
  - `F26` ✔ portfolio-service db_pool_in_use was STEADY at ~11.5-12.7 connections (pool max_size was still 50 pre-cut) from 08:00Z through 09:52Z — it was NOT 'active << 5' before the cut. 240s buckets: 08:00=12.04, 09:00=11.56, 09:32=12.36, 09:48=11.71, 09:52=11.89. It then steps DOWN to the new ceiling of 5.0 and stays there.
  - `F27` ✔ At 1-min granularity the pool step is sharp and coincides with the config cut: 09:51=11.75, 09:52=11.89 (still on the old pool), then 09:53=5.0, 09:54=5.0 and flat 5.0 through 10:10. There is ZERO pre-cut elevation — the pool never saturates on the old 50-connection pool.
  - `F28` ✔ portfolio-service latency_p99 was flat/normal (~173-189ms) from 08:00Z all the way to 09:50Z, with no elevation whatsoever. First elevated bucket is 09:52 (avg 2211.8ms, max 4250.91ms at 120s buckets; 240s bucket 09:52 avg 3261.31, max 4546.3), then 09:54=4310.82ms steady ~4-4.5s through 10:28 (4166.23).
  - `F29` ✔ portfolio-service error_rate was normal (~0.14-0.28%) from 08:00Z through 09:52Z. The 09:52 120s bucket is still 0.25% (normal); the first elevated bucket is 09:54=22.3%, then steady ~21.5-23.2% through 10:28 (21.86). So portfolio-service's own 5xx onset is 09:54Z.
  - `F30` ✔ Ordering within the 09:52-09:54 window (all 1-2 min resolution): pool metric switch 11.89->5.0 at 09:53; portfolio-service p99 already spurking in the 09:52 bucket (avg 2211.8ms); portfolio-service error_rate first elevated in 09:54 bucket. The pool ceiling change and the portfolio-service latency step are effectively simultaneous (same 09:52-09:53 minute band), consistent with the config applying at 09:52:00Z; the service's 5xx appear ~1-2 min later.
  - `F22` ✔ portfolio-service mirrors the gateway degradation, not just the gateway: portfolio-service error_rate steps from 5.04% avg / 22.3 max in the 09:40 bucket (and baseline min 0.13, first 0.31) to 22.46% avg/24.02 max at 09:58 and holds 21.6-22.5% through 11:46; latency_p99_ms goes 1291.3 (max 4546.3) at 09:40 to 4213.59 at 09:58 and ~4100-4200ms thereafter.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Config changed_at 09:52:00Z reflects runtime apply within ~1 min; the pool metric step (F27) and the simultaneous p99 step (F30) support this despite F8's coarse 3-min onset window placing onset in 09:48-09:51.
  - A 5-connection pool is structurally insufficient for portfolio-service's normal load, which the waiting=43 / total=5 evidence implies.
  - The ~12-connection pre-cut level is normal-but-under-high-concurrency (per-request connection holding time means ~12 in use at any instant corresponds to far higher concurrent request demand), so cutting the ceiling to 5 strangles it even though instantaneous in-use was only ~12.

### H2: Gateway envoy 1.30 / HTTP-2-to-upstream upgrade (dep-5220) causes upstream stalls on portfolio-service, producing p99 step change and 504s
The 08:00Z gateway release dep-5220 changed the envoy filter chain and enabled HTTP/2 to both the portfolio-service and order-service upstreams (envoy 1.29->1.30). Multiplexing / max-concurrent-streams behaviour with the portfolio upstream stalls requests, so gateway p99 rises through the roof and /v1/portfolio/* returns 504s, echoing HIST-116. The best evidence against this is scope: order-service, which also received h2 from the same deploy, shows no deviation at all.

**Causal chain**
1. platform-team deploys dep-5220 at 08:00Z: envoy 1.29->1.30, upstream protocol h2 enabled for portfolio-service AND order-service [F1]
2. HTTP/2 multiplexing/max-streams behaviour degrades upstream connections to portfolio-service (analogue of HIST-116) [F4]
3. Requests stall on the gateway->portfolio-service hop, gateway p99 climbs and /v1/portfolio/holdings requests time out to 504 [F7, F11]
4. Users see portfolio page spinning and timing out *(assumed)*

**Supporting evidence**
  - `F1` ✔ Only one deployment across all services in 2026-03-10T08:00Z / window 05:00-11:59Z: dep-5220 on api-gateway, version v9.3.0, deployed_at 2026-03-10T08:00:00Z by platform-team, summary 'Upgrade envoy filter chain; enable HTTP/2 to upstreams' (envoy 1.29 -> 1.30; upstream protocol h2 for portfolio-service and order-service); status 'live', rolled_back_at null.
  - `F4` ✔ Historical precedent HIST-116 (2025-12-18): 'HTTP/2 resets after gateway envoy upgrade', services [api-gateway], symptoms '504 at gateway', 'upstream resets', 'timeouts', root_cause 'Envoy upgrade changed HTTP/2 max concurrent streams default', resolution 'Rolled back gateway', match_score 5 -- i.e. the reported hypothesis has a known past analogue.
**Contradicting evidence**
  - `F14` ✔ The suspect gateway upgrade dep-5220 (v9.3.0, envoy 1.29->1.30, HTTP/2 to portfolio/order upstreams) was deployed at 2026-03-10T08:00:00Z, ~1h48m BEFORE the first gateway latency jump at 09:51Z, and remains live (rolled_back_at null). No intermediate gateway change exists between 08:00Z and the 09:48-09:51Z onset.
  - `F8` ✔ Zoomed to 3-min buckets, gateway p99 is normal through 09:48 (206.71ms) and first jumps in the 09:51 bucket (avg 1606.91ms, max 4418.76ms), then 09:54=4304.85ms. Onset is therefore between 09:48 and 09:51, not before.
  - `F10` ✔ Gateway CPU and request rate stayed flat across the onset, ruling out gateway CPU saturation or a traffic surge: cpu_pct ~41.8-42.6% at 09:30-09:50 and ~41-43% later; request_rate ~1232-1293 req/s before and after (09:40=1241.59, 09:50=1254.24, 11:00=1293.92).
  - `F11` ✔ The gateway error signature is a single pattern: 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504'. 252 WARN occurrences, first seen 2026-03-10T09:53:53Z, continuing to 11:58:44Z. This is a timeout to the upstream, not connection-refused.
  - `F15` ✔ The gateway failure signature is an upstream timeout, not a connection reset: 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' (first 09:53:53Z). This matches HIST-115's 'connection is not available'/'pool exhausted' shape (config_change via wrong profile) rather than HIST-116's HTTP/2 resets from an envoy upgrade — no reset/refused signature was observed on the gateway.
  - `F20` ✔ order-service was NOT affected: error_rate stayed at 0.18-0.23% (max 0.32) in every bucket from 08:00Z through 11:46Z, identical to the 08:00-09:31Z pre-onset baseline (avg 0.2-0.23%), so the onset at ~09:51Z produced no step in order-service.
  - `F21` ✔ order-service latency_p99_ms is flat across the whole incident: 149.14 / 149.09 / 151.79 / 149.66 / 149.69 / 151.21 / 149.23 / 147.57 ms for the 8 buckets 09:40-11:46Z (summary min 133.22, max 164.76, mean 149.73). No multi-second latency on the order-service route through the gateway.
  - `F24` ✔ The 08:00Z gateway H2-to-upstream deploy (dep-5220, which enables h2 to BOTH portfolio-service and order-service) is not supported by the route scope: order-service shows no latency or error deviation at any point from 08:00Z through 11:59Z, while only portfolio routes degrade. A gateway-level/HTTP-2 fault shared by both h2 upstreams would be expected to touch order-service as well.
  - `F26` ✔ portfolio-service db_pool_in_use was STEADY at ~11.5-12.7 connections (pool max_size was still 50 pre-cut) from 08:00Z through 09:52Z — it was NOT 'active << 5' before the cut. 240s buckets: 08:00=12.04, 09:00=11.56, 09:32=12.36, 09:48=11.71, 09:52=11.89. It then steps DOWN to the new ceiling of 5.0 and stays there.
  - `F27` ✔ At 1-min granularity the pool step is sharp and coincides with the config cut: 09:51=11.75, 09:52=11.89 (still on the old pool), then 09:53=5.0, 09:54=5.0 and flat 5.0 through 10:10. There is ZERO pre-cut elevation — the pool never saturates on the old 50-connection pool.
  - `F28` ✔ portfolio-service latency_p99 was flat/normal (~173-189ms) from 08:00Z all the way to 09:50Z, with no elevation whatsoever. First elevated bucket is 09:52 (avg 2211.8ms, max 4250.91ms at 120s buckets; 240s bucket 09:52 avg 3261.31, max 4546.3), then 09:54=4310.82ms steady ~4-4.5s through 10:28 (4166.23).
**Assumptions**
  - Envoy 1.30 vs portfolio-service h2 negotiation can produce stalls for this workload and the effect could be latent (~1h48m) rather than immediate — untested because no data covers 08:00-09:48Z gateway/portfolio latency.
  - A h2 fault on the shared upstream protocol could somehow affect only the portfolio-service h2 upstream and not the order-service h2 upstream, despite both being enabled by the same deploy.

### H4: Capacity saturation: gateway/upstream throughput trend outgrows provisioned capacity, no single triggering change
Gradual traffic growth or accumulated connection churn saturates the request path until request handling exceeds timeout budgets, producing the p99 step and 504s. No single change is the trigger; load simply outgrew provisioned capacity.

**Causal chain**
1. Traffic/connection demand grows on the /v1/portfolio/* path *(assumed)*
2. Request path saturates and requests exceed timeout budgets *(assumed)*
3. Gateway p99 steps and 504s appear [F7, F11]

**Supporting evidence**
  - `F7` ✔ api-gateway p99 latency shows a step change from a ~200-215ms baseline to multi-second values. 10-min buckets: 09:40=206.74ms, 09:50=3119.97ms (max 4624.18), 10:00=4328.65ms; remains ~4300ms through 11:50 (4296.33ms).
**Contradicting evidence**
  - `F10` ✔ Gateway CPU and request rate stayed flat across the onset, ruling out gateway CPU saturation or a traffic surge: cpu_pct ~41.8-42.6% at 09:30-09:50 and ~41-43% later; request_rate ~1232-1293 req/s before and after (09:40=1241.59, 09:50=1254.24, 11:00=1293.92).
  - `F8` ✔ Zoomed to 3-min buckets, gateway p99 is normal through 09:48 (206.71ms) and first jumps in the 09:51 bucket (avg 1606.91ms, max 4418.76ms), then 09:54=4304.85ms. Onset is therefore between 09:48 and 09:51, not before.
  - `F9` ✔ Gateway error_rate steps in the same window: baseline ~0.17-0.25% through 09:40, then 09:50=4.44% (max 7.74) and steady ~7.1-7.4% from 10:00 onward through 11:50 (7.22%).
  - `F27` ✔ At 1-min granularity the pool step is sharp and coincides with the config cut: 09:51=11.75, 09:52=11.89 (still on the old pool), then 09:53=5.0, 09:54=5.0 and flat 5.0 through 10:10. There is ZERO pre-cut elevation — the pool never saturates on the old 50-connection pool.
  - `F28` ✔ portfolio-service latency_p99 was flat/normal (~173-189ms) from 08:00Z all the way to 09:50Z, with no elevation whatsoever. First elevated bucket is 09:52 (avg 2211.8ms, max 4250.91ms at 120s buckets; 240s bucket 09:52 avg 3261.31, max 4546.3), then 09:54=4310.82ms steady ~4-4.5s through 10:28 (4166.23).
**Assumptions**
  - Assumes load grew, contrary to the flat request_rate (~1232-1294 req/s) and flat CPU (F10); and contrary to the flat portfolio latency up to 09:50Z (F28).

### H3: Shared-component fault: ledger-db failover/saturation (or redis-cache) on the portfolio path stalls portfolio-service then the gateway
A shared (multi-tenant) component on the /v1/portfolio/* path — ledger-db (also used by order-service and payment-service) or redis-cache (also used by auth-service and market-data-service) — degraded near 09:50Z, slowing portfolio-service's DB/cache calls until its connection pool saturated, and the gateway 504s are the propagation surface. This is now strongly contradicted: ledger-db shows no failover/saturation/latency/CPU signal, replication lag and CPU flat, slow queries flat, and its connections_active actually drops across onset.

**Causal chain**
1. ledger-db or redis-cache on the portfolio path degrades/fails over near 09:50Z [F3]
2. portfolio-service DB/cache calls slow, holding connections longer until its pool saturates [F12]
3. portfolio-service responses exceed gateway upstream timeout; gateway p99 steps and 504s appear [F7, F11]

**Supporting evidence**
  - `F3` ✔ Dependency graph for the /v1/portfolio/* path: api-gateway -> portfolio-service (http); portfolio-service -> ledger-db (db), market-data-service (http), redis-cache (cache); market-data-service -> redis-cache (cache) and exchange-gateway (http). Shared (multi-tenant) components on this path are ledger-db (shared=true, also used by order-service and payment-service) and redis-cache (shared=true, also used by auth-service and market-data-service).
  - `F12` ✔ portfolio-service is degraded with DB connection-pool exhaustion at the same time: 503 ERRORs, first match 09:53:19Z. Signatures: 'could not acquire JDBC connection within 3000ms' (270) and 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' (233, first_seen 09:54:29Z). Pool is fully saturated (active=5=total, idle=0) with dozens of waiters.
**Contradicting evidence**
  - `F31` ✔ ledger-db shows NO failover, saturation, latency, or CPU signal around the 09:50Z onset. Over 06:00-11:59Z, replication_lag_ms stays flat (avg 39.4-40.7ms, max 44.8ms at 08:00Z; 09:45=40.21, 10:00=39.54) and cpu_pct stays flat (~29.4-30.6% throughout, 09:45=30.22, 10:00=29.95). Neither metric shows any step or spike at 09:48-09:51Z.
  - `F32` ✔ ledger-db query health is unchanged across onset: slow_queries_per_min stays ~0.94-1.19/min for the whole window (09:45=1.01, 10:00=1.19, max 1.62 at 10:15) and connections_active actually DROPS slightly after 09:45 (09:45 avg 57.95, 10:00=55.1, 10:30=54.49 vs a stable ~61-62 before), i.e. fewer connections in use, not a saturation queue. No elevated query times or connection pile-up near 09:50Z.
  - `F33` ✔ ledger-db WARN logs for 06:00-11:59Z contain exactly one entry, at 07:15:46Z, and it is unrelated to the incident: pattern 'slow request <n>ms on /internal/metrics', single occurrence 'slow request 2439ms on /internal/metrics'. No DB errors, connection errors, or failover messages anywhere in the window.
  - `F34` ✔ redis-cache WARN logs for 06:00-11:59Z contain only 3 entries, all the same benign '/internal/metrics' pattern, spread across the window (06:06:35Z 7940ms, 08:04:42Z 3732ms, 09:58:57Z 3013ms). No error-level entries, no eviction/connection-failure/replication messages; nothing clusters at 09:50Z.
  - `F16` ✔ Shared components on the /v1/portfolio/* path are ledger-db (shared=true) and redis-cache (shared=true); portfolio-service also calls market-data-service, which reaches redis-cache and exchange-gateway. Neither shared component has any config change or deployment in the 06:00-11:59Z window.
  - `F13` ✔ The only change near onset is portfolio-service config cfg-2207: db.pool.max_size 50 -> 5, changed_at 2026-03-10T09:52:00Z by infra-automation, description 'Apply cost-optimisation profile small to service datasource'. It is the closest change in time to the 09:48-09:51Z onset and to the 09:53:19Z first pool-exhaustion ERROR (~1-2 min after).
  - `F27` ✔ At 1-min granularity the pool step is sharp and coincides with the config cut: 09:51=11.75, 09:52=11.89 (still on the old pool), then 09:53=5.0, 09:54=5.0 and flat 5.0 through 10:10. There is ZERO pre-cut elevation — the pool never saturates on the old 50-connection pool.
  - `F28` ✔ portfolio-service latency_p99 was flat/normal (~173-189ms) from 08:00Z all the way to 09:50Z, with no elevation whatsoever. First elevated bucket is 09:52 (avg 2211.8ms, max 4250.91ms at 120s buckets; 240s bucket 09:52 avg 3261.31, max 4546.3), then 09:54=4310.82ms steady ~4-4.5s through 10:28 (4166.23).
**Assumptions**
  - No change/deploy on shared components in window (F16) does not preclude spontaneous failover; but ledger-db telemetry (F31/F32/F33) now actively contradicts it.
  - redis-cache saturation remains only unsupported by absence of error logs — its health metrics were never retrieved (F35).
**Rejected because:** Superseded by telemetry: ledger-db shows no failover, saturation, latency or CPU signal at onset (F31: replication_lag and cpu flat; F32: slow_queries flat and connections_active actually DROPS after 09:45; F33: single unrelated WARN at 07:15Z), and redis-cache shows only benign /internal/metrics WARNs with nothing clustering at 09:50Z (F34). Also, the pool failure appears only AFTER the 09:52 ceiling cut, with zero pre-cut saturation (F27), inconsistent with a backend slowdown driving pool exhaustion.

## Facts (verified observations)
- Portfolio-service config cfg-2207 reduced db.pool.max_size from 50 to 5 at 2026-03-10T09:52:00Z, applied by infra-automation with description 'Apply cost-optimisation profile small to service datasource'; it is the closest change in time to the 09:48-09:51Z gateway onset and to the 09:53:19Z first pool-exhaustion error. [F13, F2]
- portfolio-service shows DB connection-pool exhaustion concurrent with the incident: 503 errors first matching at 09:53:19Z with signatures 'could not acquire JDBC connection within 3000ms' (270 occurrences) and 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' (233 occurrences, first_seen 09:54:29Z). [F12]
- portfolio-service db_pool_in_use was steady at ~11.5-12.7 connections on the 50-slot pool from 08:00Z through 09:52Z, then stepped down to exactly 5.0 at 09:53 and stayed there; at 1-minute granularity there is zero pre-cut pool elevation (09:51=11.75, 09:52=11.89, 09:53=5.0). [F26, F27]
- portfolio-service latency_p99 was flat/normal ~173-189ms from 08:00Z to 09:50Z with no elevation; the first elevated bucket is 09:52 (avg 2211.8ms, max 4250.91ms) and then steady ~4-4.5s. Its error_rate was normal ~0.14-0.28% through 09:52Z and first elevated at 09:54 (22.3%). [F28, F29]
- api-gateway p99 latency shows a step change from a ~200-215ms baseline to multi-second values: 09:40=206.74ms, 09:50=3119.97ms, 10:00=4328.65ms, holding ~4300ms through 11:50; gateway error_rate steps from ~0.17-0.25% to 4.44% at 09:50 and steady ~7.1-7.4% from 10:00 onward. [F7, F9, F23]
- The gateway error signature is a single pattern, 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' (252 WARN occurrences, first 09:53:53Z, last 11:58:44Z); no order-service, payment-service or other upstream route appears — this is a timeout, not connection-refused or reset. [F11, F19]
- portfolio-service mirrors the gateway degradation: error_rate steps from 5.04% avg in the 09:40 bucket / ~0.2% baseline to 22.46% at 09:58 and holds 21.6-22.5% through 11:46; latency_p99 goes from 1291.3ms at 09:40 to 4213.59ms at 09:58 and ~4100-4200ms thereafter. [F22]
- The suspect gateway deploy dep-5220 (v9.3.0, envoy 1.29->1.30, HTTP/2 to portfolio-service and order-service upstreams) was deployed at 08:00:00Z, ~1h48m before the first gateway latency jump at 09:51Z, and remained live (rolled_back_at null) with no intermediate gateway change. [F14, F1]
- order-service, which also received HTTP/2 from the same 08:00Z deploy, was unaffected: error_rate stayed 0.18-0.23% in every bucket 08:00-11:46Z and latency_p99 stayed flat at ~149-152ms across 09:40-11:46Z; a gateway-level HTTP/2 fault shared by both h2 upstreams would be expected to touch order-service too. [F20, F21]
- Gateway CPU and request rate stayed flat across onset (cpu_pct ~41-43%, request_rate ~1232-1293 req/s), ruling out gateway CPU saturation or a traffic surge. [F10]
- ledger-db shows no failover, saturation, latency or CPU signal around 09:50Z: replication_lag_ms flat ~39.4-40.7ms (max 44.8ms at 08:00Z), cpu_pct flat ~29.4-30.6%, slow_queries_per_min ~0.94-1.19/min, and connections_active actually drops after 09:45 (57.95 -> 54.49) rather than queueing. [F31, F32]
- ledger-db WARN logs for 06:00-11:59Z contain exactly one entry (07:15:46Z, 'slow request 2439ms on /internal/metrics', unrelated) with no DB/connection/failover errors; redis-cache WARN logs contain only 3 benign '/internal/metrics' entries spread across the window with nothing clustering at 09:50Z. [F33, F34]
- Widening the portfolio-service change history to 2026-03-01T00:00Z-2026-03-10T11:59Z returns only one config change (cfg-2207) and zero deployments, so no earlier same-service change can explain onset; the window as a whole contains exactly three changes (order-service log.sampling_rate only, and cfg-2207), with zero changes in the 2026-03-09T18:00Z-06:00Z pre-window baseline. [F17, F2, F16]
- The error signature (upstream timeout, no reset/refused) matches precedent HIST-115 ('order-service connection pool misconfiguration', resolution 'Reverted config') rather than HIST-116 ('HTTP/2 resets after gateway envoy upgrade', resolution 'Rolled back gateway'). [F5, F4]

## Assumptions (not verified)
- Assumption: the recorded cfg-2207 changed_at 09:52:00Z reflects runtime apply within ~1 minute; the pool metric step (F27) and the near-simultaneous portfolio p99 step (F30) support this, though the gateway's 3-min bucket onset window (09:48-09:51, F8) leaves ~1 bucket of ordering ambiguity.
- Assumption: a 5-connection pool is structurally insufficient for portfolio-service's normal load; this is implied by waiting=43 with total=5 (F12), but per-request connection-hold time at the pre-cut ~12 connections was never measured.
- Assumption: the ~12-connection pre-cut level was normal for the service under high concurrency (many short-lived acquisitions), so cutting the ceiling to 5 strangled it even though instantaneous in-use was only ~12.
- Assumption: the 08:00Z deploy's effect, if any, would have appeared promptly rather than latent ~1h48m; no data covers 08:00-09:48Z gateway/portfolio latency, so latency in that gap is assumed flat based on the 09:40 bucket being at baseline.
- Assumption: absence of redis-cache error logs indicates redis-cache was healthy; its health metrics (hit_ratio_pct, evictions_per_min, memory_pct, connected_clients) were never retrieved (F35), so redis-cache saturation is unsupported but not positively excluded.
- Assumption: the gateway log sample's single top_pattern (count 252 = total_matched) means no meaningful non-portfolio timeout lines exist, despite both gateway log queries returning truncated=true (40 of 252 rows returned).
- (demoted from facts: no verified observation backs it) Within the 09:52-09:54Z window all at 1-2 min resolution: the pool metric switches 11.89->5.0 at 09:53, portfolio-service p99 is already spiking in the 09:52 bucket, and portfolio-service 5xx first appear at 09:54 — effectively simultaneous with the 09:52:00Z config apply.

## Timeline
- `2026-03-10T08:00:00Z` Gateway deploy dep-5220 (v9.3.0, envoy 1.29->1.30, HTTP/2 enabled to portfolio-service and order-service upstreams) goes live. [F1, F14]
- `2026-03-10T08:00:00Z` portfolio-service db_pool_in_use steady at ~12 connections on the 50-slot pool; no pre-cut pool elevation. [F26, F27]
- `2026-03-10T09:40:00Z` Gateway p99 still at baseline 206.74ms, error_rate ~0.17-0.25%; portfolio-service p99 ~173-189ms and error_rate ~0.14-0.28% — all normal. [F7, F9, F28, F29]
- `2026-03-10T09:48:00Z` Gateway p99 normal on 3-min buckets (206.71ms); onset occurs between 09:48 and 09:51. [F8]
- `2026-03-10T09:52:00Z` Config cfg-2207 applies: portfolio-service db.pool.max_size 50 -> 5 ('cost-optimisation profile small'). Pool metric still reads 11.89 in the 09:52 bucket; portfolio-service p99 begins spiking in the 09:52 bucket (avg 2211.8ms). [F13, F27, F30, F28]
- `2026-03-10T09:53:00Z` Pool metric steps down sharply to 5.0 and pins there; 09:53:19Z first portfolio-service 503 'could not acquire JDBC connection within 3000ms'. [F27, F12, F30]
- `2026-03-10T09:53:53Z` First api-gateway WARN 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504'. [F11, F19, F15]
- `2026-03-10T09:54:00Z` portfolio-service error_rate first elevated to 22.3% and stays ~21.5-23.2%; HikariPool 'total=5, active=5, idle=0, waiting=43' signature first seen 09:54:29Z. [F29, F12]
- `2026-03-10T10:00:00Z` Gateway p99 ~4328.65ms and error_rate steady ~7.1-7.4%; portfolio-service p99 ~4213.59ms; degradation persists un-recovered. [F7, F9, F22, F23]
- `2026-03-10T10:05:00Z` Incident INC-002 reported by on-call SRE (13 minutes after cfg-2207 applied), with the 08:00Z gateway upgrade suspected. [F6]
- `2026-03-10T11:59:00Z` Latest data: gateway p99 still ~4.3s, error_rate ~7.2%, portfolio-service error_rate ~21.6-22.5%; incident unresolved. order-service, ledger-db and redis-cache all remain flat/unaffected throughout. [F23, F22, F20, F21, F31, F32]

## Recommended actions
- **[immediate]** Revert config change cfg-2207 on portfolio-service: restore db.pool.max_size from 5 to 50 (or remove the 'small' cost-optimisation profile from the service datasource) and force a runtime apply/pod restart if the pool does not resize in place. -- cfg-2207 is the sole portfolio-service change in ten days and the closest change to onset; the pool pins at exactly 5 with active=5=total, idle=0, waiting=43 and 3000ms acquisition timeouts (F12, F13, F17, F26, F27). Restoring the known-good ceiling of 50 directly removes the constraint causing the timeouts. [F13, F12, F27, F26, F17]
- **[immediate]** After the revert, confirm recovery on the same signals: portfolio-service db_pool_in_use returning above 5 without hitting a ceiling, portfolio-service p99 back to ~173-189ms and error_rate to ~0.2%, and gateway p99 returning to ~200ms with error_rate back to ~0.2% and 504s ceasing. -- This closes the one remaining gap — no positive 'revert restores service' confirmation exists — and is the cheapest discriminator against the residual alternative cause (dep-5220). Full recovery on revert confirms H1; failure to recover would reopen H2. [F27, F28, F29, F7, F9, F11]
- **[immediate]** Do NOT roll back gateway deploy dep-5220 as the first action; keep it live while the cfg-2207 revert is evaluated, and only roll it back if the revert does not restore p99/504s. -- dep-5220 is 1h48m before onset with no latent step, and order-service (the other h2 upstream from the same deploy) shows no deviation at all (p99 ~149ms, error ~0.2%), which contradicts a shared HTTP/2 fault on the gateway (F14, F20, F21, F24). [F14, F20, F21, F24]
- **[short_term]** Gate or block the 'small' cost-optimisation profile from being applied to any service datasource until it is load-tested: add an admission check that rejects db.pool.max_size reductions below a service-appropriate floor. -- The failure was a wrong environment/profile override of a datasource pool size, matching HIST-115 ('Datasource pool size overridden by a wrong environment profile', resolution 'Reverted config'); the same automation (infra-automation) applied this profile unconditionally (F5, F13). [F5, F13]
- **[short_term]** Add alerting on portfolio-service DB pool saturation (active == total, idle == 0, waiting > 0 for N minutes) and on JDBC acquisition-timeout rate, and instrument Hikari active/idle/waiting/total as first-class metrics since only db_pool_in_use is currently exposed. -- The pool's saturation and waiter count were observable only in application logs (total=5, active=5, idle=0, waiting=43) and portfolio-service exposes no hikaricp_* metrics (F12, F25), which is why the incident was detected via gateway 504s rather than at the pool. [F12, F25]
- **[long_term]** Require a sizing review and canary stage for cost-optimisation config profiles before fleet-wide apply, including a minimum-headroom rule derived from observed peak db_pool_in_use per service. -- portfolio-service was steady at ~12 connections against a 50-slot pool with zero saturation history, so the cut removed ~4x headroom instantly; a headroom rule derived from observed usage would have blocked a 50->5 change (F26, F27, F5). [F26, F27, F5]
- **[long_term]** Add a route/upstream-tagged error and latency metric on api-gateway (per-route 504 counts and p99) so route scope is measurable directly instead of inferred from log labels. -- The conclusion that degradation is confined to /v1/portfolio/* was established indirectly from gateway log upstream labels and upstream metrics because api-gateway exposes only aggregate cpu_pct/error_rate/latency_p99_ms/memory_pct/request_rate/restarts. [F11, F19, F24]

## Data gaps
- No metric/trace data covers 08:00-09:48Z gateway/portfolio latency, so it cannot be directly shown that no latent step followed the 08:00Z deploy; the gap is bridged only by the 09:40 bucket sitting at baseline (F28) and by order-service being flat (F20, F21).
- Gateways logs were truncated in both queries (40 of 252 matching rows returned), so a small number of non-portfolio upstream timeout lines outside the returned sample cannot be mechanically excluded; top_patterns showing count 252 = total_matched makes this unlikely.
- redis-cache health metrics (hit_ratio_pct, evictions_per_min, memory_pct, connected_clients) were never retrieved due to tool-budget exhaustion (F35), so redis-cache saturation cannot be positively excluded — only unsupported by the absence of error logs.
- ledger-db exposes no p99/query-latency metric and no failover/audit log at INFO level was checked (only WARN filtering), so a silent INFO-level failover is not 100% excluded even though replication_lag and CPU are flat (F31, F32, F33).
- No sub-120s granularity exists for portfolio-service p99/error_rate, so the ordering of the pool step vs the gateway p99 jump retains ~1 bucket ambiguity (09:48-09:51 gateway onset vs 09:52 apply); the finest evidence is log first-match 09:53:19Z pool error vs 09:53:53Z gateway 504.
- Whether the pre-cut ~12-connection level was a stable long-run level or a recent rise was not measured (pool utilization before 08:00Z not queried), so it cannot be ruled out that pool pressure was gradually building before the cut.

## What would change this conclusion
- Reverting cfg-2207 (pool back to 50) fails to restore gateway p99, portfolio-service p99 and 504 rate to baseline — this would invalidate H1 and reopen H2/the gateway upgrade.
- Evidence that gateway/portfolio latency already stepped at or shortly after 08:00Z (a latent-then-persistent step from dep-5220), which no data currently covers, would shift weight to H2.
- Detection of a connection reset/refused signature (rather than upstream timeout) on the gateway for portfolio-service, matching HIST-116's HTTP/2 envoy upgrade shape, would support H2 (F4, F15).
- Retrieval of redis-cache health metrics showing a hit-ratio collapse or eviction spike beginning ~09:50Z would support a shared-component contribution.
- Evidence that order-service (or another h2 upstream from dep-5220) also degraded on the h2 route after 08:00Z would undercut the scope-based exclusion of H2 (F20, F21, F24).
- A confirmed pool-size apply time materially earlier than 09:52:00Z (e.g. runtime apply at ~09:49Z) would remove the residual 09:48-09:51 versus 09:52 ordering tension in favor of H1 more strongly.

## Integrity warnings (set by code)
- fact demoted to assumption: Within the 09:52-09:54Z window all at 1-2 min resolution: the pool metric switch

## Investigation stats
- latency 158.68s, LLM calls 26, tokens 203273 (in 169628 / out 33645)
- tool calls 48 (4 failed, 0 partial, 0 recovered by retry), findings 35/35 verified
