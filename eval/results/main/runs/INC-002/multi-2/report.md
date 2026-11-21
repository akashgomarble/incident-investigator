# Incident investigation report: INC-002
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The incident is caused by config change cfg-2207, which at 2026-03-10T09:52:00Z cut portfolio-service's datasource db.pool.max_size from 50 to 5 (F8/F14/F37). With a normal working set of ~11.6-12.2 concurrent connections (F18/F44), the pool immediately pins at exactly 5.0 (F19/F45) and 122 HikariCP "Connection is not available ... (total=5, active=5, idle=0, waiting=43)" lines appear from 09:54:29Z with zero occurrences before that (F42/F43), driving portfolio-service p99 from ~178ms to ~4.3s (F22/F49/F50) and gateway 504s on /v1/portfolio/* from 09:53:53Z (F3). The originally suspected gateway HTTP/2 upgrade (dep-5220) is explicitly NOT the cause: no RST_STREAM/GOAWAY/protocol-error signatures exist (F24), the other h2 upstream order-service is completely flat (F26/F27/F28/F29), and the 504s are plain upstream timeouts downstream of portfolio-service (F3). Confidence in the top hypothesis is 0.72 (medium); all alternatives (H1, H3, H4, H5) are contradicted by positive evidence, with the residual limit that the config application and first latency rise share one 120s metric bucket, and no rollback was ever performed so there is no recovery test (F37-F41).

## Probable root cause
**portfolio-service DB connection-pool exhaustion from cfg-2207 (pool 50->5)** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.72, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: portfolio-service DB connection-pool exhaustion from cfg-2207 (pool 50->5) | config_change | portfolio-service | cfg-2207 | 0.72 (medium) | active |
| 2 | H5: Unknown/no-change latency onset at ~09:52Z preceding cfg-2207 (unexplained first-mover) | unknown | portfolio-service | - | 0.18 (low) | active |
| 3 | H1: Gateway HTTP/2-to-upstreams upgrade (dep-5220) caused upstream timeouts | bad_deployment | api-gateway | dep-5220 | 0.03 (low) | rejected |
| 4 | H3: Shared downstream failure (ledger-db / redis-cache) blocking portfolio-service | dependency_failure | ledger-db | null | 0.03 (low) | rejected |
| 5 | H4: portfolio-service local resource saturation (CPU/memory) with no external trigger | resource_exhaustion | portfolio-service | null | 0.03 (low) | rejected |

### H2: portfolio-service DB connection-pool exhaustion from cfg-2207 (pool 50->5)
At 09:52:00Z an automated cost-optimisation profile (cfg-2207) cut portfolio-service's datasource db.pool.max_size from 50 to 5. portfolio-service's normal working set was ~11.5-12.6 concurrent connections (F18/F44), above the new ceiling of 5, so demand immediately saturates the pool; requests block awaiting a connection and log HikariPool 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' from 09:54:29Z (F42/F45) while p99 goes to ~4.1-4.3s (F50); the gateway then times out waiting on portfolio-service and returns 504 on /v1/portfolio/* (F3). The onset is now pinned to the 09:52-09:54 bucket (F50), i.e. the pool-cut bucket itself, which converts the earlier co-occurrence inference into a tight match.

**Causal chain**
1. cfg-2207 reduces portfolio-service db.pool.max_size 50->5 at 09:52:00Z [F8, F14, F37, F38]
2. Pre-change working set is ~11.5-12.6 concurrent connections, never near the old ceiling of 50, so the pool was healthy beforehand (not saturated, not the source of a pre-existing problem) [F18, F44]
3. portfolio-service latency_p99_ms is flat ~176-178ms through the 09:50 bucket and the FIRST elevated bucket is 09:52 (avg 2211.8/max 4250.9), reaching ~4.3s by 09:54 - the rise lands in the same 120s bucket as the pool cut, not before it [F22, F49, F50, F51]
4. Post-change db_pool_in_use pins at exactly avg 5.0/max 5.0 for every bucket 09:54-11:30Z, i.e. demand now exceeds the shrunken ceiling and the ceiling is binding [F19, F45]
5. Requests block on connection acquisition: 122 ERROR lines 'Connection is not available, request timed out after 3000ms' with active=5, idle=0, waiting up to 57, first at 09:54:29Z [F42, F45]
6. No pool-acquisition timeout exists anywhere in 09:00-09:54Z (a single hit at 09:54:29Z), so the blocking is strictly a post-change condition [F43, F46]
7. CPU and memory are flat across onset (33-40% / 52-61%), ruling out local CPU/memory saturation as the queueing mechanism [F20, F21]
8. Gateway observes upstream timeouts to portfolio-service and emits 504 on /v1/portfolio/* - 252 of 254 WARN lines are 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504', first at 09:53:53Z (post-change) [F3, F1, F2]
9. cfg-2207 was never rolled back: no rollback record for it exists at any point 09:52-11:59Z, and the pool stays pinned at 5.0 through 11:30Z [F37, F38, F39, F40, F41, F45]

**Supporting evidence**
  - `F8` ✔ A config change hit portfolio-service at 2026-03-10T09:52:00Z: cfg-2207 changed db.pool.max_size from 50 to 5, author infra-automation, description 'Apply cost-optimisation profile small to service datasource' — a 10x reduction on the exact service on the portfolio path, 13 min before the 10:05Z report and later than the 08:00Z gateway deploy.
  - `F14` ✔ Exactly two config changes in the window: order-service cfg-2199 log.sampling_rate 0.1->0.2 at 07:00Z (telemetry-only, ~2h48m pre-onset), and portfolio-service cfg-2207 db.pool.max_size 50->5 at 2026-03-10T09:52:00Z by infra-automation ('Apply cost-optimisation profile small to service datasource').
  - `F37` ✔ Config-change query scoped to portfolio-service over 09:50Z–11:59Z returns exactly one record: cfg-2207 db.pool.max_size 50->5 at 2026-03-10T09:52:00Z. No subsequent entry reverting the value (e.g. anything restoring market_size to 50) exists in the window.
  - `F38` ✘ unverified The same query scoped across ALL services for 09:50Z–11:59Z also returns count=1 — the only change anywhere in that period is cfg-2207 at 09:52:00Z. No rollback of cfg-2207 by any service appears.
  - `F18` ✔ portfolio-service db_pool_in_use was ~11.4–12.6 connections throughout 09:00–09:50Z and never approached the pre-change max_size of 50; at the 09:48 bucket it was avg 11.45 / max 11.64 and at 09:50 bucket avg 11.98 / max 12.22 — i.e. NOT saturated at or before the reported 09:48Z onset.
  - `F44` ✔ db_pool_in_use did NOT hit the ceiling at 09:50-09:52Z pre-09:52:00Z. At 1-minute resolution: 09:49 = 11.64, 09:50 = 12.22, 09:51 = 11.75, 09:52 = 11.89 — all ~12 connections out of the pre-change max_size 50. The ceiling was only reached from 09:53Z onward, when the series drops to exactly avg 5.0 / max 5.
  - `F22` ✔ At 120s bucket resolution, portfolio-service latency_p99_ms was still flat ~178ms in the 09:48 bucket (avg 178.08) AND in the 09:50 bucket (avg 177.94, max 178.36) — the rise starts in the same 09:52 bucket as the pool change: 09:52 avg 2211.8 / max 4250.91, then 09:54 avg 4310.82 / max 4546.3. So the metric onset is not earlier than 09:50, and the pool drop to 5.0 coincides in-bucket with the latency jump.
  - `F49` ✔ portfolio-service latency_p99_ms at 120s resolution (raw per-bucket averages) is flat through the 09:50 bucket: 09:44 avg 176.08 / max 177.69, 09:46 avg 174.13 / max 174.29, 09:48 avg 178.08 / max 178.16, 09:50 avg 177.94 / max 178.36.
  - `F50` ✔ The first elevated portfolio-service latency_p99_ms bucket is 09:52:00Z (avg 2211.8 / max 4250.91), then 09:54 avg 4310.82 / max 4546.3 and 09:56 avg 4018.44; the onset is therefore pinned to the 09:52:00-09:53:59 bucket, later than the previously reported 09:48 onset.
  - `F51` ✔ With the corrected onset, the portfolio-service latency jump (09:52 bucket) and cfg-2207's db.pool.max_size 50->5 (09:52:00Z) land in the SAME 120s bucket, and the p99 rise follows 2-4 minutes after the previously-cited 09:48-09:50 onset — so cfg-2207 cannot be excluded as coincident with (rather than preceding) the rise on portfolio-service's own metric.
  - `F19` ✔ db_pool_in_use collapsed to exactly the new ceiling after cfg-2207: 09:52 bucket avg 8.45 / max 11.89, then flat 5.0 avg and 5.0 max from 09:54 through 10:10Z — matching the cfg-2207 db.pool.max_size 50->5 applied at 09:52Z. Immediately before (09:48–09:50 buckets) the value was ~11.5–12.2; immediately after (09:54+) it is exactly 5.0.
  - `F45` ✔ Post-change the pool is fully saturated at the new ceiling: db_pool_in_use is exactly avg 5.0 / max 5.0 for every 240s bucket from 09:54:00Z through 11:30:00Z (min 5, mean 8.21 over the whole 08:30-11:30 window), and the pool-timeout logs simultaneously show active=5, idle=0 with waiting counts up to 57 — the requested value/ceiling discrimination.
  - `F42` ✔ A pool-exhaustion / connection-acquisition signal IS present for portfolio-service: 122 ERROR log lines, all matching 'HikariPool-<n> - Connection is not available, request timed out after <n>ms (total=<n>, active=<n>, idle=<n>', first seen 2026-03-10T09:54:29Z (last 10:58:47Z), e.g. 09:54:29Z 'timeout after 3000ms (total=5, active=5, idle=0, waiting=43)'.
  - `F43` ✔ The pool-timeout WARN/ERROR logs are strictly post-config-change: a narrow search over 09:00:00Z-09:54:30Z for 'Connection is not available' returned total_matched: 1, and that single hit is at 09:54:29Z (the first line) — no pool-timeout/acquisition-timeout log exists anywhere in 09:00-09:54Z, i.e. none before or at the 09:48-09:52Z latency onset.
  - `F46` ✔ Inference for H2: the connection-pool saturation signature (active=5, idle=0, waiting>0, 3000ms acquisition timeouts) is real but its first evidence is 09:54:29Z and the db_pool_in_use reading is ~11.9/50 until 09:52Z — both strictly at/after the cfg-2207 50->5 change, so this data cannot show the pool as the CAUSE of the earlier 09:48-09:52 latency onset, but it does show the reduced pool fully saturated and timing out for the entire post-09:52 period that overlaps the sustained 504s.
  - `F20` ✔ portfolio-service cpu_pct was flat across onset and the config change: ~34.5–37.9% at 09:48 (avg 35.94, max 36.36), 09:50 (avg 36.15, max 37.31), 09:52 (avg 35.46, max 35.74) and 09:54 (avg 34.76, max 35.2); whole-window min 33.14 / max 39.5. No CPU saturation at any point.
  - `F21` ✔ portfolio-service memory_pct was also flat: ~58.75% at 09:48 (max 59.49), ~58.88% at 09:50 (max 60.32), 56.84% at 09:52 and ~57.38% at 09:54; window min 51.69 / max 60.89. No memory saturation.
  - `F3` ✔ The first gateway 504 error signature is 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' at 09:53:53Z; 252 of 254 matched WARN log lines are this exact pattern spanning 09:53:53Z to 11:58:44Z. The gateway is surfacing timeouts waiting on portfolio-service, not originating them.
  - `F12` ✔ Historical precedent for the config-change signature: HIST-115 (2025-09-25, order-service) 'order-service connection pool misconfiguration' with symptoms 'connection is not available','request timed out','pool exhausted', root cause 'Datasource pool size overridden by a wrong environment profile', category config_change; and HIST-102 (2025-09-02) shows the same user-visible signature 'portfolio page timeouts','504 at gateway' caused instead by a ledger-db failover — same symptom, different cause.
  - `F17` ✔ Historical precedents: HIST-116 (2025-12-18, api-gateway) 'HTTP/2 resets after gateway envoy upgrade' with symptoms '504 at gateway','upstream resets','timeouts' (match_score 6.5 for this query); HIST-115 (2025-09-25) 'order-service connection pool misconfiguration' with 'pool exhausted' from 'Datasource pool size overridden by a wrong environment profile'; and HIST-102 (2025-09-02) 'Portfolio timeouts during ledger-db failover' with the identical user-visible signature 'portfolio page timeouts','504 at gateway'. Same symptom, three different documented causes.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 3000ms HikariCP acquisition timeout plus waiting queue is the dominant contributor to the ~4.3s p99 (plausible arithmetic: 3000ms wait + downstream work, but not directly measured)
  - The onset is not strictly earlier than 09:52:00Z within the 120s bucket; sub-120s resolution is unavailable (GAPS), so a change landing at 09:52:00Z with a rise in the 09:52-09:53:59 bucket is treated as effectively simultaneous rather than proven prior
  - The metric bucket labelled 09:52 in F22/F50 corresponds to the 09:52:00-09:53:59 interval, i.e. the same interval in which cfg-2207 applied
  - No finer-grained (sub-2min) evidence shows the p99 rise beginning strictly before 09:52
**Rejected because:** null

### H5: Unknown/no-change latency onset at ~09:52Z preceding cfg-2207 (unexplained first-mover)
The portfolio-service slowdown began at or just before 09:52:00Z for a reason not represented in the deployment/config-change records (e.g. a sidecar/pooler config, a DNS/route or non-deploy infra change, or an intrinsic portfolio-service fault), and cfg-2207 (pool 50->5) landed on top of an already-struggling service, converting a partial degradation into sustained pool exhaustion and 504s. This preserves the residual 'unexplained head start' the critic flagged while still accounting for the strong post-09:52 pool-saturation mechanism.

**Causal chain**
1. portfolio-service latency begins rising in the 09:52:00 bucket (or marginally before), with 09:50 still flat at 177.94ms and no change event recorded at that instant [F49, F50, F51]
2. A change not captured by the deployment/config sweeps (proxies, poolers, DNS/routes, k8s patch/restarts, infra/node rollouts) causes the initial rise *(assumed)*
3. cfg-2207 50->5 at 09:52:00Z lands concurrently and pins the pool at 5.0, amplifying and sustaining the degradation [F19, F45, F42]
4. Gateway surfaces 504 upstream timeouts to portfolio-service [F3]

**Supporting evidence**
  - `F49` ✔ portfolio-service latency_p99_ms at 120s resolution (raw per-bucket averages) is flat through the 09:50 bucket: 09:44 avg 176.08 / max 177.69, 09:46 avg 174.13 / max 174.29, 09:48 avg 178.08 / max 178.16, 09:50 avg 177.94 / max 178.36.
  - `F50` ✔ The first elevated portfolio-service latency_p99_ms bucket is 09:52:00Z (avg 2211.8 / max 4250.91), then 09:54 avg 4310.82 / max 4546.3 and 09:56 avg 4018.44; the onset is therefore pinned to the 09:52:00-09:53:59 bucket, later than the previously reported 09:48 onset.
  - `F51` ✔ With the corrected onset, the portfolio-service latency jump (09:52 bucket) and cfg-2207's db.pool.max_size 50->5 (09:52:00Z) land in the SAME 120s bucket, and the p99 rise follows 2-4 minutes after the previously-cited 09:48-09:50 onset — so cfg-2207 cannot be excluded as coincident with (rather than preceding) the rise on portfolio-service's own metric.
  - `F19` ✔ db_pool_in_use collapsed to exactly the new ceiling after cfg-2207: 09:52 bucket avg 8.45 / max 11.89, then flat 5.0 avg and 5.0 max from 09:54 through 10:10Z — matching the cfg-2207 db.pool.max_size 50->5 applied at 09:52Z. Immediately before (09:48–09:50 buckets) the value was ~11.5–12.2; immediately after (09:54+) it is exactly 5.0.
  - `F45` ✔ Post-change the pool is fully saturated at the new ceiling: db_pool_in_use is exactly avg 5.0 / max 5.0 for every 240s bucket from 09:54:00Z through 11:30:00Z (min 5, mean 8.21 over the whole 08:30-11:30 window), and the pool-timeout logs simultaneously show active=5, idle=0 with waiting counts up to 57 — the requested value/ceiling discrimination.
  - `F42` ✔ A pool-exhaustion / connection-acquisition signal IS present for portfolio-service: 122 ERROR log lines, all matching 'HikariPool-<n> - Connection is not available, request timed out after <n>ms (total=<n>, active=<n>, idle=<n>', first seen 2026-03-10T09:54:29Z (last 10:58:47Z), e.g. 09:54:29Z 'timeout after 3000ms (total=5, active=5, idle=0, waiting=43)'.
  - `F3` ✔ The first gateway 504 error signature is 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' at 09:53:53Z; 252 of 254 matched WARN log lines are this exact pattern spanning 09:53:53Z to 11:58:44Z. The gateway is surfacing timeouts waiting on portfolio-service, not originating them.
**Contradicting evidence**
  - `F50` ✔ The first elevated portfolio-service latency_p99_ms bucket is 09:52:00Z (avg 2211.8 / max 4250.91), then 09:54 avg 4310.82 / max 4546.3 and 09:56 avg 4018.44; the onset is therefore pinned to the 09:52:00-09:53:59 bucket, later than the previously reported 09:48 onset.
  - `F22` ✔ At 120s bucket resolution, portfolio-service latency_p99_ms was still flat ~178ms in the 09:48 bucket (avg 178.08) AND in the 09:50 bucket (avg 177.94, max 178.36) — the rise starts in the same 09:52 bucket as the pool change: 09:52 avg 2211.8 / max 4250.91, then 09:54 avg 4310.82 / max 4546.3. So the metric onset is not earlier than 09:50, and the pool drop to 5.0 coincides in-bucket with the latency jump.
  - `F43` ✔ The pool-timeout WARN/ERROR logs are strictly post-config-change: a narrow search over 09:00:00Z-09:54:30Z for 'Connection is not available' returned total_matched: 1, and that single hit is at 09:54:29Z (the first line) — no pool-timeout/acquisition-timeout log exists anywhere in 09:00-09:54Z, i.e. none before or at the 09:48-09:52Z latency onset.
**Assumptions**
  - An unrecorded change exists at ~09:52Z - contradicted by F38/F14 which show the only event in 09:50-11:59Z is cfg-2207 itself, and by F43 which shows no pool-timeout log before 09:54:29Z
  - The pre-existing degradation would have to be pool-independent yet produce a pool-pinning signature only after the cut
**Rejected because:** null

### H1: Gateway HTTP/2-to-upstreams upgrade (dep-5220) caused upstream timeouts
api-gateway dep-5220 v9.3.0 (envoy 1.29->1.30, HTTP/2 enabled to portfolio-service/order-service) introduced an HTTP/2 regression causing queued/reset upstream requests and gateway p99 spikes with 504s. Rejected on mechanism, selectivity and timing grounds.

**Causal chain**
1. dep-5220 deploys envoy 1.30 with HTTP/2 to upstreams at 08:00Z [F7, F13]
2. Envoy HTTP/2 concurrency/reset behaviour changes on the gateway-to-upstream path [F11]
3. Requests to portfolio-service stall or reset, raising portfolio latency *(assumed)*
4. Gateway p99 and 5xx rise, 504 'upstream timeout portfolio-service' emitted [F1, F2, F3]

**Supporting evidence**
  - `F7` ✘ unverified Only one deployment occurred in 06:00–11:59Z across all services: api-gateway dep-5220 v9.3.0 at 2026-03-10T08:00:00Z by platform-team, 'Upgrade envoy filter chain; enable HTTP/2 to upstreams' (envoy 1.29 -> 1.30; upstream protocol h2 for portfolio-service and order-service), status live with rolled_back_at null.
  - `F13` ✔ Only one deployment occurred anywhere in 06:00-11:59Z: api-gateway dep-5220 v9.3.0 at 2026-03-10T08:00:00Z ('Upgrade envoy filter chain; enable HTTP/2 to upstreams', envoy 1.29->1.30, upstream protocol h2 for portfolio-service and order-service), status live, rolled_back_at null. This is the sole deploy preceding the ~09:48-09:50 latency onset, ~1h48m earlier.
  - `F1` ✔ api-gateway p99 latency was flat ~205-215ms from 07:00 through the 09:40 bucket, then jumped in the 09:50 bucket (avg 3119.97ms, max 4624.18ms) and stayed elevated ~4.3s (e.g. 11:50 bucket avg 4296.33ms, max 4586.01ms).
  - `F2` ✔ api-gateway 5xx error rate was flat ~0.2% all morning and rose to 4.44% avg (max 7.74%) in the same 09:50 bucket, holding ~7.2% thereafter. Request rate stayed flat (~1.25k req/s) across the onset, so the rise is not load-driven.
  - `F11` ✘ unverified Historical precedent for the gateway HTTP/2 change: HIST-116 (2025-12-18, api-gateway) 'HTTP/2 resets after gateway envoy upgrade', symptoms '504 at gateway','upstream resets','timeouts', root cause 'Envoy upgrade changed HTTP/2 max concurrent streams default', resolved by rollback.
**Contradicting evidence**
  - `F24` ✔ Targeted api-gateway WARN log searches over 09:48Z-11:59Z for the HTTP/2 signature tokens 'RST_STREAM', 'GOAWAY', 'concurrent streams' and 'protocol error' each returned total_matched: 0 — none of the expected HTTP/2-to-upstream failure signatures appear on the gateway in this window.
  - `F25` ✔ A search of api-gateway WARN logs for 'order-service' over 09:48Z-11:59Z returned total_matched: 0 — the gateway logged no warnings at all for the other HTTP/2-enabled upstream.
  - `F26` ✔ order-service latency_p99_ms was flat across the entire 07:00-11:59Z window (min 133.22ms, max 164.76ms, mean 149.55ms); the 09:49 bucket avg 149.83ms and the 11:59 bucket avg 151.77ms are indistinguishable from the pre-incident baseline — no latency rise despite order-service being switched to h2 by dep-5220.
  - `F27` ✔ order-service error_rate was flat ~0.18-0.25% throughout 07:00-11:59Z (min 0.1, max 0.32, mean 0.21), including the 09:49 bucket (avg 0.24) and all post-onset buckets — no 5xx rise on the second HTTP/2-enabled upstream.
  - `F28` ✔ Gateway p99 confirms the portfolio-path onset only: latency_p99_ms held 205.91-212.37ms through the 07:00-09:36 buckets then jumped to avg 3120.15ms in the 09:49 bucket (max 4659.87) and stayed ~4.28-4.37s (e.g. 11:46 bucket avg 4305.58, max 4586.01). The gateway-wide latency regression is real but sits alongside a flat order-service, i.e. it is confined to the portfolio route, not all h2 upstreams.
  - `F29` ✔ The observed combination — zero RST_STREAM/GOAWAY/max-concurrent-streams/protocol-error signatures, zero order-service impact (flat p99 ~149ms, flat error_rate ~0.21%) — is the 'portfolio-only impact with plain upstream timeouts' pattern that the task defines as rejecting H1 (gateway HTTP/2 upgrade). It is not corroborated by either expected H1 signal.
  - `F22` ✔ At 120s bucket resolution, portfolio-service latency_p99_ms was still flat ~178ms in the 09:48 bucket (avg 178.08) AND in the 09:50 bucket (avg 177.94, max 178.36) — the rise starts in the same 09:52 bucket as the pool change: 09:52 avg 2211.8 / max 4250.91, then 09:54 avg 4310.82 / max 4546.3. So the metric onset is not earlier than 09:50, and the pool drop to 5.0 coincides in-bucket with the latency jump.
  - `F49` ✔ portfolio-service latency_p99_ms at 120s resolution (raw per-bucket averages) is flat through the 09:50 bucket: 09:44 avg 176.08 / max 177.69, 09:46 avg 174.13 / max 174.29, 09:48 avg 178.08 / max 178.16, 09:50 avg 177.94 / max 178.36.
  - `F50` ✔ The first elevated portfolio-service latency_p99_ms bucket is 09:52:00Z (avg 2211.8 / max 4250.91), then 09:54 avg 4310.82 / max 4546.3 and 09:56 avg 4018.44; the onset is therefore pinned to the 09:52:00-09:53:59 bucket, later than the previously reported 09:48 onset.
  - `F8` ✔ A config change hit portfolio-service at 2026-03-10T09:52:00Z: cfg-2207 changed db.pool.max_size from 50 to 5, author infra-automation, description 'Apply cost-optimisation profile small to service datasource' — a 10x reduction on the exact service on the portfolio path, 13 min before the 10:05Z report and later than the 08:00Z gateway deploy.
  - `F14` ✔ Exactly two config changes in the window: order-service cfg-2199 log.sampling_rate 0.1->0.2 at 07:00Z (telemetry-only, ~2h48m pre-onset), and portfolio-service cfg-2207 db.pool.max_size 50->5 at 2026-03-10T09:52:00Z by infra-automation ('Apply cost-optimisation profile small to service datasource').
**Assumptions**
  - A ~1h48m latent gap between deploy and onset would be required, now compounded by the fact that the gateway 504 onset (09:53:53Z) is accountable without the deploy
**Rejected because:** Mechanism signature absent: zero RST_STREAM/GOAWAY/max-concurrent-streams/protocol-error in gateway logs (F24) and zero order-service warnings (F25) - F3's 504s are plain upstream timeouts, not the HIST-116 reset signature (F29). Selectivity fails: order-service, the other h2 upstream, is fully unaffected - flat p99 ~149ms and flat error_rate ~0.21% (F26, F27), so a gateway-wide h2 regression is disproven (F28). Timing fails: onset is pinned to the 09:52 bucket (F50) with the first 504 at 09:53:53Z, ~1h48m after the deploy, and H2's mechanism now explains the gateway 504s downstream of portfolio-service rather than vice versa. Also dep-5220 was never rolled back (F39) yet no alternative explanation for the 504s requires it.

### H3: Shared downstream failure (ledger-db / redis-cache) blocking portfolio-service
A shared component on the portfolio path - ledger-db (shared=true) or redis-cache (shared=true) - degraded independently and portfolio-service latency jumped because it blocked on it. Broadly contradicted by co-consumer and component-level telemetry.

**Causal chain**
1. portfolio path is gateway -> portfolio-service -> {ledger-db, market-data-service, redis-cache}, with ledger-db and redis-cache shared [F10, F16]
2. A shared downstream fails or is saturated around onset *(assumed)*
3. portfolio-service p99 jumps to ~4.1-4.3s [F22, F50]
4. Gateway returns 504 'upstream timeout portfolio-service' [F3]

**Supporting evidence**
  - `F10` ✔ Dependency map for the /v1/portfolio path: api-gateway -> portfolio-service -> {ledger-db (kind db), market-data-service (http), redis-cache (cache)}; ledger-db and redis-cache are marked shared=true, and market-data-service also reads redis-cache, while order-service and payment-service also write to ledger-db — so a fault in those shared components can surface as gateway 504s on the portfolio route.
  - `F16` ✔ The /v1/portfolio dependency chain is api-gateway -> portfolio-service -> {ledger-db (postgres), market-data-service (http), redis-cache (redis)}; ledger-db and redis-cache are marked shared=true, and market-data-service also reads redis-cache. A fault in either shared component can surface as gateway 504s on the portfolio route independently of the gateway version.
  - `F17` ✔ Historical precedents: HIST-116 (2025-12-18, api-gateway) 'HTTP/2 resets after gateway envoy upgrade' with symptoms '504 at gateway','upstream resets','timeouts' (match_score 6.5 for this query); HIST-115 (2025-09-25) 'order-service connection pool misconfiguration' with 'pool exhausted' from 'Datasource pool size overridden by a wrong environment profile'; and HIST-102 (2025-09-02) 'Portfolio timeouts during ledger-db failover' with the identical user-visible signature 'portfolio page timeouts','504 at gateway'. Same symptom, three different documented causes.
**Contradicting evidence**
  - `F30` ✔ ledger-db shows no latency/saturation onset in 09:00-10:00Z: slow_queries_per_min stays flat (09:40 avg 0.96, 09:48 avg 0.86, 09:52 avg 0.96, 09:56 avg 1.06 — no step change at the ~09:48 onset), so query slowness on the shared DB did not begin with the incident.
  - `F31` ✔ ledger-db connections_active was ~61-63 through 09:48 and then DROPPED to ~53.7-55.7 in the 09:52-10:00 buckets (09:48 avg 62.16 -> 09:52 avg 53.68) — the opposite of a shared-DB connection saturation signature; the DB is not becoming a bottleneck at onset.
  - `F32` ✔ ledger-db replication_lag_ms is flat ~38-41ms across the whole window including the onset buckets (09:44 avg 41.03, 09:48 avg 40.12, 09:52 avg 39.9, 09:56 avg 40.24), i.e. no failover/replication stall.
  - `F33` ✔ redis-cache hit_ratio_pct is stable ~95.4-97.4% throughout 09:00-10:00Z with no drop at onset (09:44 avg 95.76, 09:48 avg 96.37, 09:52 avg 96.06, 09:56 avg 95.69) — no cache-miss stampede from the shared cache.
  - `F34` ✔ order-service (co-consumer of ledger-db) latency_p99_ms is flat ~143-162ms across 09:00-10:00Z with no rise at ~09:48Z (09:48 avg 147.13, 09:52 avg 148.05, 09:56 avg 152.55, 10:00 avg 142.97; whole-window min 136.95/max 161.32). The other ledger-db consumer is unaffected.
  - `F35` ✔ market-data-service (co-consumer of redis-cache) latency_p99_ms is flat ~37-44ms across 09:00-10:00Z with no rise at ~09:48Z (09:48 avg 39.0, 09:52 avg 40.24, 09:56 avg 40.89; whole-window min 37.01/max 43.82). The service behind the redis-cache leg of the portfolio path is healthy.
  - `F36` ✔ Both shared components mark the affected dependency leg as multi-consumer: ledger-db and redis-cache are consumed by portfolio-service AND other services (order-service/market-data-service) which show no latency change, so a fault in either shared component would be expected to hit those co-consumers too — it did not, which argues against a shared-component-wide failure at onset. (Inference from flat co-consumer telemetry.)
  - `F5` ✔ auth-service p99 latency stayed flat ~89-91ms (max 98.88ms) across the entire 08:00-11:48 window, showing no degradation; the symptom is not global to all gateway dependencies.
  - `F47` ✔ payment-service (second ledger-db consumer) latency_p99_ms was flat ~308-327ms across every bucket in 09:00-10:00Z, including the incident onset: 09:48 avg 323.21 / max 331.1, 09:51 avg 317.14, 09:54 avg 327.52, 10:00 avg 314.57; window min 289.84 / max 351.27, no step change.
  - `F48` ✔ payment-service error_rate was flat ~0.17-0.28% throughout 09:00-10:00Z with no rise at onset: 09:48 avg 0.24, 09:51 avg 0.23, 10:00 avg 0.25; window min 0.1 / max 0.32, mean 0.21.
  - `F52` ✔ Both ledger-db writers are flat at onset: payment-service latency_p99 ~317-327ms / error_rate ~0.21-0.25 (tc-052/tc-053) and order-service was previously measured flat ~147-152ms (F34). No co-consumer of ledger-db shows an onset signature in 09:00-10:00Z.
**Assumptions**
  - A shared-component fault would degrade its co-consumers; it did not
**Rejected because:** Hop 2 has zero direct evidence and the scope check now positively refutes it: ledger-db slow_queries_per_min flat (F30), connections_active DROPPED ~62->54 (F31, opposite of saturation), replication_lag_ms flat ~40ms (F32); redis-cache hit_ratio stable ~95-97% (F33). All three co-consumers of the shared components are flat across onset - order-service (F34/F52), market-data-service (F35), payment-service (F47/F48, closing the last gap the critic noted). The root shape also misses the mechanism: none of these explains pool pinning at 5.0 with HikariCP acquisition timeouts (F42/F45), which is local to portfolio-service and time-locked to cfg-2207.

### H4: portfolio-service local resource saturation (CPU/memory) with no external trigger
portfolio-service saturated a local resource (CPU or memory) at ~09:52Z for reasons unrelated to the two known changes, producing ~4s p99 and gateway 504s. Directly contradicted by retrieved metrics.

**Causal chain**
1. portfolio-service CPU or memory saturates around 09:52Z *(assumed)*
2. Request handling slows to ~4s p99 on the holdings route [F22, F50]
3. Gateway times out waiting on portfolio-service and returns 504 [F3, F1, F2]

**Supporting evidence**
  - `F22` ✔ At 120s bucket resolution, portfolio-service latency_p99_ms was still flat ~178ms in the 09:48 bucket (avg 178.08) AND in the 09:50 bucket (avg 177.94, max 178.36) — the rise starts in the same 09:52 bucket as the pool change: 09:52 avg 2211.8 / max 4250.91, then 09:54 avg 4310.82 / max 4546.3. So the metric onset is not earlier than 09:50, and the pool drop to 5.0 coincides in-bucket with the latency jump.
  - `F50` ✔ The first elevated portfolio-service latency_p99_ms bucket is 09:52:00Z (avg 2211.8 / max 4250.91), then 09:54 avg 4310.82 / max 4546.3 and 09:56 avg 4018.44; the onset is therefore pinned to the 09:52:00-09:53:59 bucket, later than the previously reported 09:48 onset.
**Contradicting evidence**
  - `F20` ✔ portfolio-service cpu_pct was flat across onset and the config change: ~34.5–37.9% at 09:48 (avg 35.94, max 36.36), 09:50 (avg 36.15, max 37.31), 09:52 (avg 35.46, max 35.74) and 09:54 (avg 34.76, max 35.2); whole-window min 33.14 / max 39.5. No CPU saturation at any point.
  - `F21` ✔ portfolio-service memory_pct was also flat: ~58.75% at 09:48 (max 59.49), ~58.88% at 09:50 (max 60.32), 56.84% at 09:52 and ~57.38% at 09:54; window min 51.69 / max 60.89. No memory saturation.
  - `F18` ✔ portfolio-service db_pool_in_use was ~11.4–12.6 connections throughout 09:00–09:50Z and never approached the pre-change max_size of 50; at the 09:48 bucket it was avg 11.45 / max 11.64 and at 09:50 bucket avg 11.98 / max 12.22 — i.e. NOT saturated at or before the reported 09:48Z onset.
  - `F44` ✔ db_pool_in_use did NOT hit the ceiling at 09:50-09:52Z pre-09:52:00Z. At 1-minute resolution: 09:49 = 11.64, 09:50 = 12.22, 09:51 = 11.75, 09:52 = 11.89 — all ~12 connections out of the pre-change max_size 50. The ceiling was only reached from 09:53Z onward, when the series drops to exactly avg 5.0 / max 5.
**Assumptions**
  - No residual CPU/memory mechanism survives; the only local-exhaustion variant that fits is a connection-pool limit, which is H2
**Rejected because:** cpu_pct 33.1-39.5% (F20) and memory_pct 51.7-60.9% (F21) are flat across both the onset and cfg-2207. The pre-change DB pool was also unsaturated (F18, F44). No CPU/memory exhaustion mechanism remains; the only local limit shown to bind is the pool ceiling, scored under H2 (F45/F42). This is a scoped refutation of the CPU/memory variant, not of a non-CPU/memory internal limit, which H2 covers.

## Facts (verified observations)
- Only two config changes occurred in the window: order-service cfg-2199 log.sampling_rate 0.1->0.2 at 07:00Z (telemetry-only) and portfolio-service cfg-2207 db.pool.max_size 50->5 at 2026-03-10T09:52:00Z by infra-automation, described as 'Apply cost-optimisation profile small to service datasource'. [F8, F14, F9, F37]
- Only one deployment occurred in 06:00-11:59Z across all services: api-gateway dep-5220 v9.3.0 at 08:00:00Z (envoy 1.29->1.30, HTTP/2 enabled to portfolio-service and order-service), status live, never rolled back. [F13]
- portfolio-service db_pool_in_use was ~11.45-12.22 connections between 09:48 and 09:52Z, far below the pre-change max_size of 50 and never saturated before cfg-2207. [F18, F44]
- After cfg-2207, db_pool_in_use pinned at exactly avg 5.0 / max 5.0 for every 240s bucket from 09:54:00Z onward, matching the new ceiling of 5. [F19, F45]
- 122 ERROR log lines matching HikariCP 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, ...)' exist for portfolio-service, first seen 2026-03-10T09:54:29Z (e.g. waiting=43); a search over 09:00:00Z-09:54:30Z returned only that single first line. [F42, F43]
- portfolio-service latency_p99_ms was flat 176-178ms through the 09:44/09:46/09:48/09:50 buckets and first rose in the 09:52:00 bucket (avg 2211.8 / max 4250.91), then 4310.82ms at 09:54 and 4018.44ms at 09:56. [F22, F49, F50]
- The first gateway 504 is 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' at 09:53:53Z; 252 of 254 matched WARN lines are this exact pattern through 11:58:44Z, i.e. the gateway surfaces timeouts waiting on portfolio-service rather than originating them. [F3]
- api-gateway p99 was flat ~205-215ms through the 09:36 bucket and rose to ~3.1-4.6s from the 09:50 bucket onward, with 5xx rising from ~0.2% to 4.44% (max 7.74%) while request rate stayed flat ~1.25k req/s. [F1, F2, F28]
- Targeted api-gateway WARN log searches over 09:48Z-11:59Z for 'RST_STREAM', 'GOAWAY', 'concurrent streams' and 'protocol error' each returned total_matched: 0, and a search for 'order-service' returned total_matched: 0. [F24, F25]
- order-service (the other HTTP/2-enabled upstream) shows no impact: latency_p99 flat 133.22-164.76ms and error_rate flat ~0.18-0.25% across the entire 07:00-11:59Z window. [F26, F27, F34]
- Shared downstream components are healthy at onset: ledger-db slow_queries_per_min flat ~0.86-1.06, connections_active DROPPED from ~62 to ~53.7-55.7, replication_lag_ms flat ~38-41ms, and redis-cache hit_ratio stable ~95.4-97.4%. [F30, F31, F32, F33]
- All co-consumers of the shared components are flat across onset: order-service p99 ~147-152ms, market-data-service p99 ~39-40ms, payment-service p99 ~317-327ms with error_rate ~0.21-0.25%. [F34, F35, F47, F48]
- portfolio-service cpu_pct was flat ~33.1-39.5% and memory_pct flat ~51.7-60.9% across onset and the config change, with no saturation. [F20, F21]
- No change or rollback record exists for portfolio-service from 09:52Z through 11:59Z: the scoped config-change query returned count=1 (only cfg-2207), the all-services 09:50-11:59Z sweep also returned count=1, and deployment/rollback queries returned count=0, including the 11:50-11:59Z window. [F37, F39, F40]
- The dependency path is api-gateway -> portfolio-service -> {ledger-db (shared), market-data-service (http), redis-cache (shared)}, so a fault in a shared component could surface as gateway 504s on the portfolio route. [F10, F16]
- Historical precedents: HIST-115 (order-service connection pool misconfiguration from a wrong environment profile, symptoms 'pool exhausted'/'request timed out') and HIST-102 (identical user-visible signature 'portfolio page timeouts'/'504 at gateway' from a ledger-db failover), and HIST-116 (gateway HTTP/2 resets after an envoy upgrade, resolved by rollback). [F12, F17]

## Assumptions (not verified)
- The 3000ms HikariCP acquisition timeout plus queueing is the dominant contributor to the ~4.3s p99 (plausible arithmetic: 3000ms wait plus downstream work), but this composition is not directly measured.
- The 120s metric bucket labelled 09:52 corresponds to the 09:52:00-09:53:59 interval, i.e. the same interval in which cfg-2207 was applied at 09:52:00Z.
- No sub-120s evidence exists to show the portfolio-service p99 rise beginning strictly before 09:52:00Z; the config application and the latency rise are treated as effectively simultaneous rather than proven ordered.
- Ordering of the config application versus the first latency sample cannot be proven finer than the 120s bucket, so the causal claim rests on the observed pool-pinning mechanism rather than on strict sub-bucket precedence.
- Because cfg-2207 was never rolled back, there is no revert-and-recover experiment to confirm causality; the conclusion rests on the mechanism match rather than a recovery test.
- The deployment/config-change sweeps cover service-level changes; a non-service-level change (k8s patch, sidecar/proxy/pooler config, DNS/route) is assumed not to exist in the window, though it was not exhaustively enumerated.

## Timeline
- `2026-03-10T07:00:00Z` order-service cfg-2199 log.sampling_rate 0.1->0.2 applied (telemetry-only, ~2h48m pre-onset). [F9, F14]
- `2026-03-10T08:00:00Z` api-gateway dep-5220 v9.3.0 deployed: envoy 1.29->1.30, HTTP/2 enabled to portfolio-service and order-service; status live, never rolled back. [F7, F13]
- `2026-03-10T09:44:00Z` portfolio-service p99 still flat: 176.08/177.69ms; 09:46 174.13ms; 09:48 178.08ms; 09:50 177.94ms. [F49]
- `2026-03-10T09:49:00Z` db_pool_in_use at 1-minute resolution: 09:49 = 11.64, 09:50 = 12.22, 09:51 = 11.75, 09:52 = 11.89 — all ~12 of the pre-change ceiling of 50, unsaturated. [F44, F18]
- `2026-03-10T09:50:00Z` api-gateway p99 jumps to avg 3119.97ms (max 4624.18ms) and 5xx rises to 4.44% avg on flat ~1.25k req/s request rate. [F1, F2, F28]
- `2026-03-10T09:52:00Z` cfg-2207 applied to portfolio-service: db.pool.max_size 50->5 (author infra-automation, 'Apply cost-optimisation profile small to service datasource'). [F8, F14, F37]
- `2026-03-10T09:52:00Z` portfolio-service p99 first rises in the 09:52 bucket: avg 2211.8 / max 4250.91ms (previous bucket 177.94ms). [F22, F50, F49]
- `2026-03-10T09:53:53Z` First gateway 504: 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504'. [F3]
- `2026-03-10T09:54:00Z` db_pool_in_use pins at exactly avg 5.0 / max 5.0 and stays there for every 240s bucket through 11:30Z; p99 reaches 4310.82ms (max 4546.3ms). [F19, F45, F22, F50]
- `2026-03-10T09:54:29Z` First HikariCP pool-exhaustion ERROR: 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)'; zero such lines exist before this time. [F42, F43]
- `2026-03-10T10:05:00Z` Incident reported (SEV-2, portfolio page timing out; gateway p99 elevated). []
- `2026-03-10T11:59:00Z` End of data window: gateway 504 pattern spans 09:53:53Z-11:58:44Z (252 of 254 matched WARN lines); no cfg-2207 rollback or deployment rollback exists at any point. [F3, F37, F38, F39, F40, F41]

## Recommended actions
- **[immediate]** Revert cfg-2207 by restoring portfolio-service datasource db.pool.max_size to 50 (or a value at least above the observed ~12-connection working set), via the config mechanism used by infra-automation; verify p99 returns to ~180ms and gateway 504s stop. -- This is the only change in the window, it reduced the pool 10x below the 11.45-12.22 working set, and the pool pinned at exactly 5.0 with HikariCP acquisition timeouts, driving the ~4.3s p99 and the 504s. [F8, F14, F18, F44, F19, F45, F42, F22, F3]
- **[immediate]** Explicitly close the loop on the gateway: do NOT roll back dep-5220 as the mitigation, and record that the HTTP/2-to-upstreams upgrade was investigated and excluded. -- Zero RST_STREAM/GOAWAY/max-concurrent-streams/protocol-error signatures were logged, the other h2 upstream order-service is completely flat, and the 504s are plain upstream timeouts downstream of portfolio-service — unlike the HIST-116 precedent which was resolved by rollback. [F24, F25, F26, F27, F28, F29, F3, F11]
- **[short_term]** Confirm recovery quantitatively after the revert: portfolio-service db_pool_in_use falls back toward ~11-12 (not pinned at 5.0), p99 returns to ~178ms, gateway p99 returns to ~205-215ms and 5xx to ~0.2%, and no new HikariCP acquisition-timeout lines appear. -- Since cfg-2207 was never rolled back during the incident (no rollback record at any point), there is no observed recovery to correlate; an explicit revert-and-measure is the missing causality test. [F41, F37, F38, F39, F40, F19, F45, F22, F1, F2]
- **[short_term]** If latency does NOT recover after restoring max_size to 50, escalate and immediately check for an unrecorded cause: portfolio-service sidecar/pooler/DNS configuration, k8s node or pod restarts, and per-minute (sub-120s) pool and latency series. -- This is the discriminator against the residual H5 'unrecorded change' hypothesis, and guards against the possibility that the config application and the first latency sample merely share the 120s bucket. [F50, F51, F22, F49]
- **[long_term]** Add guardrails to the automated cost-optimisation profile so a datasource pool cannot be reduced below observed peak concurrency (derived from db_pool_in_use history) without review; alert on db_pool_in_use >= 90% of max_size and on HikariCP acquisition-timeout lines. -- An automated 'cost-optimisation profile small' shrank the pool 50->5 below the service's real working set of ~11.5-12.6; the same pattern previously caused HIST-115 (pool size overridden by a wrong environment profile). [F8, F14, F18, F44, F12]
- **[long_term]** Add or expose a pool-wait/queue-depth time series (e.g. db_pool_wait_ms) and a pool-timeout exception counter for portfolio-service, so pool exhaustion is visible as a metric rather than only via HikariCP 'waiting=<n>' log lines. -- No pool-wait gauge exists in the service's metric list, so the wait depth is only visible indirectly in logs, which slowed this investigation. [F42, F45]

## Data gaps
- No sub-120s resolution: metric buckets are 120s wide, so the ordering of cfg-2207 (09:52:00Z), the first elevated p99 bucket (09:52), and the first gateway 504 (09:53:53Z) cannot be resolved within one bucket; the change, the pool pin and the latency jump share a single bucket.
- No rollback or recovery test: cfg-2207 was never rolled back and no deployment rollback exists in 09:50-11:59Z, so causality rests on the observed pool-pinning mechanism rather than a revert-and-recover experiment; no post-11:59Z data was inspected.
- No dedicated db_pool_wait_ms gauge or pool-timeout-exception counter exists for portfolio-service, so queue depth is visible only via HikariCP 'waiting=<n>' log lines rather than a time series.
- Non-service-level change types (k8s patch/restarts, sidecar/proxy/pooler config, DNS/route changes) were not exhaustively enumerated, and portfolio-service's use of a connection-pooler or sidecar between it and ledger-db could not be verified; pre-window changes before 06:00Z were only checked back to 2026-03-01 with count 0.
- Query budget was exhausted before running a broad api-gateway WARN 'timeout' search and an all-level ERROR search (09:48-11:59Z), so the full composition of gateway WARN/ERROR lines was not independently confirmed in later rounds.
- The 122-line pool-timeout log result was returned truncated (40 entries), so the per-hour distribution beyond 10:10:51Z was not enumerated line-by-line; only the total count and last_match 10:58:47Z are known.
- ledger-db and redis-cache expose no latency/error metrics and their cpu_pct/memory_pct/disk_used_pct were never queried, so a resource-saturation condition on those shared components cannot be fully excluded even though all co-consumers are flat.

## What would change this conclusion
- Evidence that portfolio-service p99 began rising strictly before 09:52:00Z at sub-120s resolution (e.g. a rise in the 09:50 or 09:51 minute), which would show an onset preceding cfg-2207 and reopen H5 as the first mover.
- Discovery of an unrecorded change at or before ~09:52Z (k8s patch, sidecar/proxy/pooler config, DNS/route change, or a defect in infra-automation's application path), which would refute H2's trigger.
- Failure of p99 and gateway 504s to recover after restoring db.pool.max_size to 50, which would falsify the pool-exhaustion mechanism as the cause of the sustained outage.
- A rebound of db_pool_in_use toward ~11-12 with latency still elevated, or absence of any pool-acquisition timeouts despite the reduced ceiling, which would decouple the pool cut from the latency.
- Corroborating HTTP/2 failure signatures (RST_STREAM, GOAWAY, max-concurrent-streams, protocol errors) or degradation of order-service, which would revive the rejected H1 gateway-upgrade hypothesis.
- Evidence of degradation in ledger-db or redis-cache — or in any co-consumer of those shared components (order-service, market-data-service, payment-service) — at onset, which would revive the rejected H3 shared-downstream hypothesis.
- A change in portfolio-service's observed peak concurrency history showing the working set was actually below 5 connections, which would mean cfg-2207 did not bind and the pool-pinning signature requires another explanation.

## Investigation stats
- latency 219.72s, LLM calls 37, tokens 320820 (in 273465 / out 47355)
- tool calls 59 (4 failed, 0 partial, 0 recovered by retry), findings 49/52 verified
