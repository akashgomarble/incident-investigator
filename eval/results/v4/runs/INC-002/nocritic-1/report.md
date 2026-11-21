# Incident investigation report: INC-002
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The incident is most likely caused by config change cfg-2207, which cut portfolio-service's datasource pool (db.pool.max_size 50->5) at 09:52Z and saturated portfolio-service, propagating upstream as gateway 504s on /v1/portfolio/* (H2, confidence 0.60, medium). The onset evidence is the strongest discriminator: gateway and portfolio-service p99 both jump in the same 09:50-09:52Z bucket and then hold ~4.3s, and cfg-2207 is the only change at that moment, whereas the gateway HTTP/2 deploy (dep-5220) preceded onset by 110 minutes with flat latency in between (H1 downgraded to 0.12). Confidence is medium rather than high because no metric or log data was retrieved from portfolio-service's own pool/CPU metrics or from the shared datastores (ledger-db, redis-cache), so an independent datastore fault (H3, 0.22) cannot be fully excluded. Recommendation: roll back cfg-2207 first (fast, low-risk, directly tests the top hypothesis), and if latency does not recover, escalate to datastore-layer checks before touching the gateway deploy.

## Probable root cause
**cfg-2207 portfolio-service DB pool cut (50->5) exhausted its ledger-db connection pool, saturating portfolio-service** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: cfg-2207 portfolio-service DB pool cut (50->5) exhausted its ledger-db connection pool, saturating portfolio-service | config_change | portfolio-service | cfg-2207 | 0.60 (medium) | active |
| 2 | H3: Shared ledger-db (or redis-cache) degraded independently of any change, slowing portfolio-service | infrastructure_failure | ledger-db | null | 0.22 (low) | active |
| 3 | H1: Gateway HTTP/2-to-upstream upgrade (dep-5220) introduced protocol-level stalls causing 504s | bad_deployment | api-gateway | dep-5220 | 0.12 (low) | active |
| 4 | H5: Gateway deploy is coincident-but-harmless; scheduling/chance aside, portfolio path self-saturated (resource exhaustion without a triggering release) | resource_exhaustion | portfolio-service | null | 0.10 (low) | active |
| 5 | H4: Shared infrastructure in front of the gateway (LB/DNS/mesh) degraded, throttling /v1/portfolio/* | infrastructure_failure | api-gateway (upstream network path) | null | 0.05 (low) | active |

### H2: cfg-2207 portfolio-service DB pool cut (50->5) exhausted its ledger-db connection pool, saturating portfolio-service
Config change cfg-2207 ('Apply cost-optimisation profile small', db.pool.max_size 50->5) at 09:52Z shrank portfolio-service's datasource pool, so portfolio-service requests queue/block waiting for a connection to ledger-db, driving portfolio-service latency to ~4.3s and propagating upstream as gateway 504s on /v1/portfolio/*.

**Causal chain**
1. cfg-2207 changes portfolio-service db.pool.max_size 50 -> 5 at 09:52Z via an automated cost-optimisation profile [F2, F13]
2. With only 5 pooled connections, portfolio-service requests block/queue on the ledger-db pool (pool exhaustion), the same mechanism as the matching precedent HIST-115 [F16, F5]
3. portfolio-service p99 jumps in the 09:50Z bucket alongside the gateway, showing the latency originates inside the portfolio-service path [F12, F8]
4. Slow portfolio-service calls exceed the gateway upstream timeout -> 504s confined to /v1/portfolio/holdings [F10, F9]

**Supporting evidence**
  - `F2` ✔ Config change cfg-2207: portfolio-service key 'db.pool.max_size' changed 50 -> 5 at 2026-03-10T09:52:00Z by infra-automation, description 'Apply cost-optimisation profile small to service datasource'. This is the latest change before the 10:05Z report and touches the reported call path's own service.
  - `F13` ✔ Full change list in the window 2026-03-09T22:00Z–11:59Z contains exactly one deployment and two config changes: dep-5220 api-gateway v9.3.0 at 08:00Z ('envoy 1.29 -> 1.30', 'upstream protocol h2 for portfolio-service and order-service', rolled_back_at null, status live), cfg-2199 order-service log.sampling_rate 0.1->0.2 at 07:00Z, cfg-2207 portfolio-service db.pool.max_size 50->5 at 09:52Z ('Apply cost-optimisation profile small to service datasource'). No change to ledger-db, redis-cache, DNS, LB or mesh, and no rollback.
  - `F14` ✔ Timing correlation: onset (gateway/portfolio-service first anomaly in the 09:50Z bucket, 09:50–09:52Z) is 110 minutes AFTER the 08:00Z gateway HTTP/2 deploy but coincides (same minute) with cfg-2207 portfolio-service db.pool.max_size 50->5 at 09:52:00Z. The gateway deploy cannot explain a 09:50 onset without a delayed trigger; the pool cut is the only change at onset.
  - `F12` ✔ portfolio-service p99 was flat at ~170-190 ms from 08:00 to 09:45Z, then rose in the SAME 09:50Z bucket as the gateway (avg 1865.15 ms, max 4546.3 ms) and stayed ~4100-4300 ms. Its p99 tracks the gateway almost exactly, so the added latency is inside the portfolio-service path, not introduced at the gateway edge.
  - `F8` ✔ Zooming api-gateway p99 to 2-minute buckets pins onset to 09:50-09:52Z: 09:50 avg 201.4 ms (max 201.64), 09:52 avg 2309.55 ms (max 4418.76), 09:54 avg 4293.88 ms.
  - `F16` ✔ A second precedent that matches the current service path rather than the gateway: HIST-115 (2025-09-25, order-service, match_score 7): symptoms 'connection is not available','request timed out','pool exhausted', category config_change, root cause 'Datasource pool size overridden by a wrong environment profile', fix 'Reverted config; added validation'. This mirrors cfg-2207 (pool size cut via a profile) on the portfolio-service->ledger-db edge.
  - `F5` ✔ Precedent HIST-116 (2025-12-18) 'HTTP/2 resets after gateway envoy upgrade' on api-gateway with symptoms '504 at gateway','upstream resets','timeouts', root cause 'Envoy upgrade changed HTTP/2 max concurrent streams default', resolution 'Rolled back gateway'. Also HIST-115 (2025-09-25) 'Datasource pool size overridden by a wrong environment profile' on order-service with symptoms 'connection is not available','request timed out','pool exhausted'. These are leads only.
  - `F10` ✔ All gateway 504/timeout errors are confined to a single route: 252 matched WARN entries, top pattern "upstream timeout portfolio-service GET /v<n>/portfolio/holdings -> <n> (trace <n>)" (count 252), first_seen 2026-03-10T09:53:53Z. No other route appears.
**Contradicting evidence**
  - `F7` ✔ api-gateway p99 latency was flat at ~200-230 ms from 08:00 to 09:45Z, then jumped in the 09:50Z bucket (avg 1849.99 ms, max 4418.76 ms) and stayed at ~4300 ms through 11:55Z.
  - `F8` ✔ Zooming api-gateway p99 to 2-minute buckets pins onset to 09:50-09:52Z: 09:50 avg 201.4 ms (max 201.64), 09:52 avg 2309.55 ms (max 4418.76), 09:54 avg 4293.88 ms.
**Assumptions**
  - cfg-2207 propagates promptly (pool resize applied on reload) rather than only at next restart
  - The 09:52 config commit and the 09:50-09:52 metric onset offset is measurement-bucket granularity, not a genuine 2-minute lead of the symptom over the cause
**Rejected because:** null

### H3: Shared ledger-db (or redis-cache) degraded independently of any change, slowing portfolio-service
A shared datastore on the portfolio path — ledger-db (shared:true) or redis-cache (shared:true) — began failing or throttling (as in HIST-102 failover / HIST-110 IOPS throttling) around 09:50Z, so portfolio-service calls blocked on the datastore and the gateway reflected the upstream wait as 504s. No change was recorded for these components.

**Causal chain**
1. portfolio-service depends on ledger-db and redis-cache (both shared:true) on the /v1/portfolio/* path [F4, F18]
2. ledger-db or redis-cache degrades (failover, IOPS throttling, connection saturation) around 09:50Z [F17]
3. portfolio-service requests block on the degraded datastore -> p99 ~4.3s [F12]
4. Gateway surfaces the upstream wait as 504s on /v1/portfolio/holdings [F10, F9]

**Supporting evidence**
  - `F4` ✔ The /v1/portfolio/* chain is api-gateway -> portfolio-service -> {ledger-db (db), market-data-service (http), redis-cache (cache)}; market-data-service -> redis-cache and exchange-gateway. ledger-db is shared by order-service, portfolio-service, payment-service and settlement-service; redis-cache is shared by auth-service, portfolio-service and market-data-service. Both datastores are flagged shared:true, owner platform.
  - `F17` ✔ Precedent HIST-102 (2025-09-02, portfolio-service + ledger-db) shows the exact symptom pair 'portfolio page timeouts','504 at gateway' with prior root cause 'Unplanned ledger-db primary failover; 6 min of refused connections' and fix 'Failover completed; added retry'; HIST-110 (2026-01-15) shows ledger-db-side slowness ('statement timeouts','slow queries', root cause 'Cloud volume burst credits exhausted; IOPS throttled') propagating to order latency. Both are leads showing a datastore fault can surface as gateway 504s on this path.
  - `F18` ✔ Dependency graph confirms the /v1/portfolio/* chain and its shared components: portfolio-service -> ledger-db (db, shared:true, owner platform), market-data-service (http), redis-cache (cache, shared:true, owner platform); market-data-service -> redis-cache and exchange-gateway; api-gateway -> portfolio-service. ledger-db and redis-cache are the two shared nodes through which a neighbour could injure portfolio-service.
  - `F12` ✔ portfolio-service p99 was flat at ~170-190 ms from 08:00 to 09:45Z, then rose in the SAME 09:50Z bucket as the gateway (avg 1865.15 ms, max 4546.3 ms) and stayed ~4100-4300 ms. Its p99 tracks the gateway almost exactly, so the added latency is inside the portfolio-service path, not introduced at the gateway edge.
**Contradicting evidence**
  - `F6` ✔ No deployment in the window was rolled back (get_deployments returned only dep-5220 with rolled_back_at null) and no changes were logged for ledger-db, redis-cache, DNS, load balancer or service mesh in the config-change result.
  - `F8` ✔ Zooming api-gateway p99 to 2-minute buckets pins onset to 09:50-09:52Z: 09:50 avg 201.4 ms (max 201.64), 09:52 avg 2309.55 ms (max 4418.76), 09:54 avg 4293.88 ms.
  - `F14` ✔ Timing correlation: onset (gateway/portfolio-service first anomaly in the 09:50Z bucket, 09:50–09:52Z) is 110 minutes AFTER the 08:00Z gateway HTTP/2 deploy but coincides (same minute) with cfg-2207 portfolio-service db.pool.max_size 50->5 at 09:52:00Z. The gateway deploy cannot explain a 09:50 onset without a delayed trigger; the pool cut is the only change at onset.
**Assumptions**
  - A datastore fault can occur with no recorded change (no deploy/config visible in the window, per F6/F13)
  - The datastore would produce no ERROR-level gateway logs, only upstream timeouts
**Rejected because:** null

### H1: Gateway HTTP/2-to-upstream upgrade (dep-5220) introduced protocol-level stalls causing 504s
api-gateway v9.3.0 (dep-5220, envoy 1.29->1.30, upstream protocol h2 to portfolio-service and order-service) deployed 08:00Z introduced an HTTP/2-to-upstream behavior change (e.g. max concurrent streams default shift) that stalls portfolio-service calls, producing upstream timeouts and 504s on /v1/portfolio/* and elevated gateway p99.

**Causal chain**
1. dep-5220 deploys envoy 1.30 with upstream protocol h2 for portfolio-service at 08:00Z [F1, F13]
2. Envoy HTTP/2 default change (max concurrent streams) causes upstream resets/stalls on portfolio-service calls [F5, F15]
3. Stalled upstream calls hit gateway timeout -> 504 on /v1/portfolio/holdings [F10, F9]
4. Gateway p99 and error rate rise [F7, F8, F9]

**Supporting evidence**
  - `F1` ✔ Deployment dep-5220: api-gateway v9.3.0 deployed 2026-03-10T08:00:00Z by platform-team, 'Upgrade envoy filter chain; enable HTTP/2 to upstreams' (envoy 1.29 -> 1.30, upstream protocol h2 for portfolio-service and order-service), status live, rolled_back_at null.
  - `F5` ✔ Precedent HIST-116 (2025-12-18) 'HTTP/2 resets after gateway envoy upgrade' on api-gateway with symptoms '504 at gateway','upstream resets','timeouts', root cause 'Envoy upgrade changed HTTP/2 max concurrent streams default', resolution 'Rolled back gateway'. Also HIST-115 (2025-09-25) 'Datasource pool size overridden by a wrong environment profile' on order-service with symptoms 'connection is not available','request timed out','pool exhausted'. These are leads only.
  - `F10` ✔ All gateway 504/timeout errors are confined to a single route: 252 matched WARN entries, top pattern "upstream timeout portfolio-service GET /v<n>/portfolio/holdings -> <n> (trace <n>)" (count 252), first_seen 2026-03-10T09:53:53Z. No other route appears.
  - `F15` ✔ Historical precedent with the closest signature match is HIST-116 (2025-12-18, api-gateway, match_score 6): 'HTTP/2 resets after gateway envoy upgrade', symptoms '504 at gateway','upstream resets','timeouts', category bad_deployment, prior root cause 'Envoy upgrade changed HTTP/2 max concurrent streams default', fix 'Rolled back gateway'.
  - `F13` ✔ Full change list in the window 2026-03-09T22:00Z–11:59Z contains exactly one deployment and two config changes: dep-5220 api-gateway v9.3.0 at 08:00Z ('envoy 1.29 -> 1.30', 'upstream protocol h2 for portfolio-service and order-service', rolled_back_at null, status live), cfg-2199 order-service log.sampling_rate 0.1->0.2 at 07:00Z, cfg-2207 portfolio-service db.pool.max_size 50->5 at 09:52Z ('Apply cost-optimisation profile small to service datasource'). No change to ledger-db, redis-cache, DNS, LB or mesh, and no rollback.
**Contradicting evidence**
  - `F7` ✔ api-gateway p99 latency was flat at ~200-230 ms from 08:00 to 09:45Z, then jumped in the 09:50Z bucket (avg 1849.99 ms, max 4418.76 ms) and stayed at ~4300 ms through 11:55Z.
  - `F8` ✔ Zooming api-gateway p99 to 2-minute buckets pins onset to 09:50-09:52Z: 09:50 avg 201.4 ms (max 201.64), 09:52 avg 2309.55 ms (max 4418.76), 09:54 avg 4293.88 ms.
  - `F12` ✔ portfolio-service p99 was flat at ~170-190 ms from 08:00 to 09:45Z, then rose in the SAME 09:50Z bucket as the gateway (avg 1865.15 ms, max 4546.3 ms) and stayed ~4100-4300 ms. Its p99 tracks the gateway almost exactly, so the added latency is inside the portfolio-service path, not introduced at the gateway edge.
  - `F14` ✔ Timing correlation: onset (gateway/portfolio-service first anomaly in the 09:50Z bucket, 09:50–09:52Z) is 110 minutes AFTER the 08:00Z gateway HTTP/2 deploy but coincides (same minute) with cfg-2207 portfolio-service db.pool.max_size 50->5 at 09:52:00Z. The gateway deploy cannot explain a 09:50 onset without a delayed trigger; the pool cut is the only change at onset.
  - `F6` ✔ No deployment in the window was rolled back (get_deployments returned only dep-5220 with rolled_back_at null) and no changes were logged for ledger-db, redis-cache, DNS, load balancer or service mesh in the config-change result.
  - `F11` ✔ api-gateway emitted zero ERROR-level logs in the reported window (09:30-11:59Z, total_matched 0); the only other WARN besides 504 timeouts was a single 'clock drift ... resyncing with NTP' at 10:38:45Z.
**Assumptions**
  - The gateway envoy HTTP/2 change has a delayed trigger (e.g. connection reuse exhaustion or stream-limit hit only after 110 minutes of traffic) that would explain a 09:50 onset rather than an 08:00 onset
  - No gateway-side error/exception is expected in logs (matches F11 zero ERRORs), i.e. the failure manifests only as upstream timeouts
**Rejected because:** null

### H5: Gateway deploy is coincident-but-harmless; scheduling/chance aside, portfolio path self-saturated (resource exhaustion without a triggering release)
portfolio-service's own resources (CPU/memory/threads/connection handling) saturated due to a traffic or query change with no faulty deploy, producing the latency and 504s.

**Causal chain**
1. portfolio-service resource usage climbs (traffic/query mix change) with no corresponding code/config cause *(assumed)*
2. Requests slow to ~4.3s p99 [F12]
3. Gateway times out -> 504 on portfolio route [F10]

**Supporting evidence**
  - `F12` ✔ portfolio-service p99 was flat at ~170-190 ms from 08:00 to 09:45Z, then rose in the SAME 09:50Z bucket as the gateway (avg 1865.15 ms, max 4546.3 ms) and stayed ~4100-4300 ms. Its p99 tracks the gateway almost exactly, so the added latency is inside the portfolio-service path, not introduced at the gateway edge.
  - `F10` ✔ All gateway 504/timeout errors are confined to a single route: 252 matched WARN entries, top pattern "upstream timeout portfolio-service GET /v<n>/portfolio/holdings -> <n> (trace <n>)" (count 252), first_seen 2026-03-10T09:53:53Z. No other route appears.
**Contradicting evidence**
  - `F14` ✔ Timing correlation: onset (gateway/portfolio-service first anomaly in the 09:50Z bucket, 09:50–09:52Z) is 110 minutes AFTER the 08:00Z gateway HTTP/2 deploy but coincides (same minute) with cfg-2207 portfolio-service db.pool.max_size 50->5 at 09:52:00Z. The gateway deploy cannot explain a 09:50 onset without a delayed trigger; the pool cut is the only change at onset.
  - `F2` ✔ Config change cfg-2207: portfolio-service key 'db.pool.max_size' changed 50 -> 5 at 2026-03-10T09:52:00Z by infra-automation, description 'Apply cost-optimisation profile small to service datasource'. This is the latest change before the 10:05Z report and touches the reported call path's own service.
  - `F13` ✔ Full change list in the window 2026-03-09T22:00Z–11:59Z contains exactly one deployment and two config changes: dep-5220 api-gateway v9.3.0 at 08:00Z ('envoy 1.29 -> 1.30', 'upstream protocol h2 for portfolio-service and order-service', rolled_back_at null, status live), cfg-2199 order-service log.sampling_rate 0.1->0.2 at 07:00Z, cfg-2207 portfolio-service db.pool.max_size 50->5 at 09:52Z ('Apply cost-optimisation profile small to service datasource'). No change to ledger-db, redis-cache, DNS, LB or mesh, and no rollback.
**Assumptions**
  - A load/query change occurred that is not visible in the change list
**Rejected because:** null

### H4: Shared infrastructure in front of the gateway (LB/DNS/mesh) degraded, throttling /v1/portfolio/*
A load balancer, DNS, or service-mesh component upstream of api-gateway degraded and specifically affected the portfolio path, inflating gateway p99 and producing 504s on /v1/portfolio/*.

**Causal chain**
1. A shared network component (LB/DNS/mesh) in front of or between gateway and portfolio-service degrades *(assumed)*
2. It selectively slows the portfolio route [F10]
3. Gateway p99 rises and 504s appear [F7, F9]

**Supporting evidence**
  - `F10` ✔ All gateway 504/timeout errors are confined to a single route: 252 matched WARN entries, top pattern "upstream timeout portfolio-service GET /v<n>/portfolio/holdings -> <n> (trace <n>)" (count 252), first_seen 2026-03-10T09:53:53Z. No other route appears.
**Contradicting evidence**
  - `F6` ✔ No deployment in the window was rolled back (get_deployments returned only dep-5220 with rolled_back_at null) and no changes were logged for ledger-db, redis-cache, DNS, load balancer or service mesh in the config-change result.
  - `F12` ✔ portfolio-service p99 was flat at ~170-190 ms from 08:00 to 09:45Z, then rose in the SAME 09:50Z bucket as the gateway (avg 1865.15 ms, max 4546.3 ms) and stayed ~4100-4300 ms. Its p99 tracks the gateway almost exactly, so the added latency is inside the portfolio-service path, not introduced at the gateway edge.
  - `F7` ✔ api-gateway p99 latency was flat at ~200-230 ms from 08:00 to 09:45Z, then jumped in the 09:50Z bucket (avg 1849.99 ms, max 4418.76 ms) and stayed at ~4300 ms through 11:55Z.
  - `F8` ✔ Zooming api-gateway p99 to 2-minute buckets pins onset to 09:50-09:52Z: 09:50 avg 201.4 ms (max 201.64), 09:52 avg 2309.55 ms (max 4418.76), 09:54 avg 4293.88 ms.
  - `F11` ✔ api-gateway emitted zero ERROR-level logs in the reported window (09:30-11:59Z, total_matched 0); the only other WARN besides 504 timeouts was a single 'clock drift ... resyncing with NTP' at 10:38:45Z.
**Assumptions**
  - A network-layer fault can degrade portfolio traffic without touching other routes and without any recorded DNS/LB/mesh change
**Rejected because:** null

## Facts (verified observations)
- The /v1/portfolio/* chain is api-gateway -> portfolio-service -> {ledger-db (db), market-data-service (http), redis-cache (cache)}; ledger-db and redis-cache are shared (shared:true, owner platform) across multiple services. [F4, F18]
- A config change cfg-2207 changed portfolio-service key 'db.pool.max_size' from 50 to 5 at 2026-03-10T09:52:00Z by infra-automation, described as 'Apply cost-optimisation profile small to service datasource'; it is the latest change before the 10:05Z report and touches the reported call path's own service. [F2, F13]
- api-gateway p99 was flat at ~200-230 ms from 08:00 to 09:45Z, then jumped in the 09:50Z bucket (avg 1849.99 ms, max 4418.76 ms) and stayed ~4300 ms through 11:55Z. [F7]
- 2-minute-bucket zoom pins the gateway onset to 09:50-09:52Z: 09:50 avg 201.4 ms, 09:52 avg 2309.55 ms (max 4418.76), 09:54 avg 4293.88 ms. [F8]
- portfolio-service p99 was flat at ~170-190 ms from 08:00 to 09:45Z, then rose in the SAME 09:50Z bucket as the gateway (avg 1865.15 ms, max 4546.3 ms) and stayed ~4100-4300 ms; it tracks the gateway almost exactly, indicating the added latency originates inside the portfolio-service path rather than at the gateway edge. [F12]
- api-gateway error_rate was 0.17-0.31% from 08:00 through 09:45Z, then rose to 1.66% avg (max 7.18) in the 09:50Z bucket and held ~7.1-7.3% thereafter. [F9]
- All gateway 504/timeout errors are confined to a single route: 252 matched WARN entries with top pattern 'upstream timeout portfolio-service GET /v<n>/portfolio/holdings', first_seen 2026-03-10T09:53:53Z; no other route appears. [F10]
- api-gateway emitted zero ERROR-level logs in the window 09:30-11:59Z; the only other WARN besides 504 timeouts was a single 'clock drift ... resyncing with NTP' at 10:38:45Z. [F11]
- Deployment dep-5220 (api-gateway v9.3.0, envoy 1.29->1.30, upstream protocol h2 for portfolio-service and order-service) went live at 08:00:00Z and has rolled_back_at null. [F1]
- The full change list for 2026-03-09T22:00Z-11:59Z contains exactly one deployment and two config changes (dep-5220 at 08:00Z, cfg-2199 order-service log.sampling_rate at 07:00Z, cfg-2207 portfolio-service db.pool.max_size at 09:52Z); there was no change to ledger-db, redis-cache, DNS, LB or mesh, and no rollback. [F13, F6]
- Precedent HIST-115 (order-service, match_score 7) shows symptoms 'connection is not available','request timed out','pool exhausted' with root cause 'Datasource pool size overridden by a wrong environment profile' and fix 'Reverted config; added validation', mirroring cfg-2207 on the portfolio-service->ledger-db edge. [F16]
- Precedent HIST-116 (api-gateway, match_score 6) matches the HTTP/2 signature ('HTTP/2 resets after gateway envoy upgrade', 504s, upstream resets, timeouts; root cause 'Envoy upgrade changed HTTP/2 max concurrent streams default'; fix 'Rolled back gateway'), and HIST-102/HIST-110 show a datastore fault on this path can also surface as gateway 504s; these are leads only. [F5, F15, F17]

## Assumptions (not verified)
- Assumption: cfg-2207 propagates promptly (the pool resize is applied on reload) rather than only at the next restart.
- Assumption: the 09:50-09:52 offset between the 09:52 config commit and the metric onset is measurement-bucket granularity, not a genuine 2-minute lead of symptom over cause.
- Assumption: portfolio-service's db.pool.max_size governs the connections available to ledger-db, so a 50->5 cut can exhaust the pool and cause request blocking/queueing.
- Assumption: the gateway 504s on /v1/portfolio/* are a downstream reflection of portfolio-service slowness, consistent with portfolio-service p99 tracking gateway p99 (F12).
- Assumption: no unrecorded binary/config rollout occurred on ledger-db or redis-cache in the window; the visible change list (F13) is complete.
- Assumption: the absence of other routes in the 504 log pattern (F10) means the fault is specific to the portfolio path and not a gateway-wide protocol defect.
- (demoted from facts: no verified observation backs it) Onset (09:50-09:52Z) is 110 minutes after the 08:00Z gateway HTTP/2 deploy but coincides in the same minute with cfg-2207 at 09:52:00Z; the deploy cannot explain a 09:50 onset without a delayed trigger, and the pool cut is the only change at onset.

## Timeline
- `2026-03-10T08:00:00Z` Deployment dep-5220: api-gateway v9.3.0 goes live (envoy 1.29->1.30; upstream protocol h2 for portfolio-service and order-service); not rolled back (F1, F13). [F1, F13]
- `2026-03-10T08:00:00Z-09:45:00Z` api-gateway p99 stays flat at ~200-230 ms and error_rate at 0.17-0.31%; portfolio-service p99 stays flat at ~170-190 ms — no impact from the 08:00Z gateway deploy for the first 105+ minutes (F7, F9, F12). [F7, F9, F12]
- `2026-03-10T09:50:00Z` Onset: gateway p99 jumps in the 09:50Z bucket (avg 1849.99 ms, max 4418.76) and error_rate rises to 1.66% avg; portfolio-service p99 rises in the SAME bucket (avg 1865.15 ms, max 4546.3) (F7, F8, F9, F12). [F7, F8, F9, F12]
- `2026-03-10T09:52:00Z` Config change cfg-2207: portfolio-service db.pool.max_size 50->5 ('Apply cost-optimisation profile small to service datasource') by infra-automation — the only change coincident with onset (F2, F13, F14). [F2, F13, F14]
- `2026-03-10T09:53:00Z onward` Gateway 504/timeout WARN entries first seen 09:53:53Z, all confined to /v1/portfolio/holdings ('upstream timeout portfolio-service'); gateway p99 holds ~4300 ms and error_rate ~7.1-7.3% through 11:55Z (F7, F9, F10). [F7, F9, F10]
- `2026-03-10T10:05:00Z` Incident INC-002 reported by on-call SRE: portfolio page spinning/timing out, gateway p99 through the roof, suspicion placed on the morning gateway HTTP/2 upgrade. [F1, F14]
- `2026-03-10T11:59:00Z` Latest data: no rollback of dep-5220 and no revert of cfg-2207 recorded; degradation persists (F6, F13). [F6, F13]

## Recommended actions
- **[immediate]** Revert config change cfg-2207 (portfolio-service db.pool.max_size 5 -> 50) and watch api-gateway and portfolio-service p99 over the next 10 minutes. -- cfg-2207 is the only change coincident with the 09:50-09:52Z onset, and the top hypothesis (H2, 0.60) attributes the saturation to a pool cut via a profile; a config revert is fast, low-risk and directly tests the leading hypothesis. HIST-115 shows the same 'wrong environment profile cut the datasource pool' pattern with fix 'Reverted config'. [F2, F13, F14, F16]
- **[immediate]** While the revert propagates, verify portfolio-service db_pool_in_use, cpu_pct, memory_pct, restarts and its WARN/ERROR logs over 09:40-10:00Z, and pull ledger-db/redis-cache connection-pool, IOPS and GC telemetry for the same window. -- Distinguishes H2 (pool exhausted in portfolio-service) from H3 (shared datastore degraded independently). These metrics were never retrieved, which is the main reason confidence is capped at medium. [F2, F12, F18]
- **[immediate]** Do NOT roll back dep-5220 (the gateway HTTP/2 upgrade) as a first move; if p99 remains elevated after the cfg-2207 revert, then roll back api-gateway to v9.3.0-predecessor and re-measure /v1/portfolio/* latency. -- The 08:00Z deploy preceded onset by 110 minutes with flat latency in between, so it requires an implausible delayed trigger; rolling it back first would discard the stronger causal evidence and add avoidable risk. It stays as the fallback if the pool revert fails. [F1, F7, F12, F14]
- **[short_term]** If neither revert restores latency, capture a live latency profile of portfolio-service -> ledger-db (connection acquisition wait time vs query time) and check ledger-db for primary failover or I/O throttling, per the HIST-102 / HIST-110 patterns. -- This is the discriminating step for the remaining H3 datastore-degradation hypothesis, which could not be ruled in or out from change data alone. [F17, F4, F18]
- **[short_term]** Confirm fix: verify p99 returns to ~200 ms baseline on both api-gateway and portfolio-service and that gateway 504s on /v1/portfolio/holdings cease, then close the incident with the confirmed cause. -- Establishes causal validation (a step explicitly missing from the evidence) and ensures the degradation has fully cleared before declaring resolution. [F7, F10, F12]
- **[long_term]** Add guardrails on datasource pool configuration: require a minimum pool size floor per service class and enforce change review/validation for 'cost-optimisation profile' configs applied by automation to request-path services. -- Prevents recurrence of the HIST-115 failure mode ('datasource pool size overridden by a wrong environment profile'); cfg-2207 was committed by infra-automation with no validation gate. [F2, F16]
- **[long_term]** Add per-service alerting on db_pool_in_use / connection-acquisition latency and a golden-signal alert keyed to /v1/portfolio/* so pool exhaustion is caught before it manifests as gateway 504s. -- The incident was detected only at the gateway edge (504s, p99) after portfolio-service was already saturated; portfolio-service exposes db_pool_in_use but no alert fired on it. [F9, F10, F12]

## Data gaps
- get_service_dependencies for api-gateway direction=upstream returned empty, so callers in front of the gateway (DNS/LB/mesh) could not be enumerated; cannot confirm whether any component upstream of the gateway changed in the window (limits rejection of H4).
- No metric time series were retrieved for api-gateway, portfolio-service, ledger-db or redis-cache (tool budget exhausted); cannot establish exact onset ordering within the 09:50-09:52Z bucket, nor whether the shared datastores themselves degraded.
- portfolio-service's own resource metrics (cpu_pct, memory_pct, db_pool_in_use, GC) and its WARN/ERROR logs were never queried, although the metric list exists; cannot directly confirm pool exhaustion as opposed to other in-service saturation — this is the biggest gap in supporting H2.
- No telemetry for ledger-db or redis-cache (connection-pool saturation, IOPS, GC); H3 cannot be excluded from data, only from the absence of a recorded change.
- Deployment records exist only for api-gateway; an unrecorded binary/config rollout on the shared datastores cannot be fully ruled out.
- No explicit effective-propagation time for cfg-2207 (commit vs pod reload of db.pool.max_size), so the 09:52 commit cannot be perfectly reconciled with the 09:50 metric bucket onset.
- Blast-radius confirmation for non-portfolio routes relied on a truncated log-pattern summary (40 of 252 rows returned); absence of other routes in the timeout signature is well-supported but not exhaustively verified.
- No post-change validation data exists for a rollback of cfg-2207 or dep-5220; no remediation event appears in the change list, so neither candidate change has been causally validated by recovery.

## What would change this conclusion
- portfolio-service db_pool_in_use rising to/near the new limit of 5 at 09:50Z with no evidence of a datastore-side fault would strongly confirm H2 and raise confidence to high.
- Latency persisting unchanged after reverting cfg-2207 would falsify H2 as the sole cause and shift weight to H1 (gateway HTTP/2) or H3 (datastore degradation).
- Evidence of ledger-db failover, connection refusals, or I/O throttling beginning at ~09:50Z (HIST-102/HIST-110 pattern) while portfolio-service's pool was never saturated would overturn H2 in favor of H3.
- Finding that cfg-2207's pool resize only takes effect at next pod restart (i.e., it had not yet applied by 10:05Z) would remove it as the cause and strengthen H1 or H3.
- A delayed gateway-side HTTP/2 effect (e.g., connection-reuse exhaustion or a stream-limit hit appearing only after ~110 minutes of traffic, or evidence of upstream resets in gateway logs) would overturn the timing argument and support H1.
- Portfolio-service p99 rising strictly before gateway p99 with matching service-internal metrics would confirm the latency originates inside portfolio-service; the reverse (gateway-first) would point to the gateway or the network path in front of it (H4).

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: Onset (09:50-09:52Z) is 110 minutes after the 08:00Z gateway HTTP/2 deploy but c

## Investigation stats
- latency 130.54s, LLM calls 12, tokens 85571 (in 69538 / out 16033)
- tool calls 22 (0 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
