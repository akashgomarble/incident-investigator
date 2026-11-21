# Incident investigation report: INC-002
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The portfolio page timeouts are caused by config change cfg-2207, an automated infra-automation "small" cost-optimisation profile that cut portfolio-service's HikariCP datasource pool from 50 to 5 at 09:52:13Z and was never reverted. The pool immediately pinned at exactly the 5-connection ceiling with zero variance (F16/F25), application threads queued (HikariPool total=5, active=5, idle=0, waiting=25–57; F18/F27), each request waited ~3s on a JDBC acquire and held its upstream connection open, so portfolio-service p99 stepped to ~4.25s with an ~22% error rate (F15/F28) and the gateway — with flat CPU/memory and a held-open-until-timeout 504 signature — followed 34s later at 09:53:53Z (F9/F11/F17). Load and CPU were flat through onset (F29), which rules out a traffic surge, and the gateway HTTP/2 upgrade (08:00Z) is contradicted: the pool sat stable at ~11.6–12.3 connections for ~113 minutes after that deploy and the onset tracks the 09:52 config change, not the deploy (F26/F13/F14). Confidence 0.78 (high, capped by the Critic at 0.85); the gateway is a victim, not the cause.

## Probable root cause
**portfolio-service DB connection-pool shrink (cfg-2207) starves /v1/portfolio/holdings; gateway 504s are a symptom** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.78, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service DB connection-pool shrink (cfg-2207) starves /v1/portfolio/holdings; gateway 504s are a symptom | config_change | portfolio-service | cfg-2207 | 0.78 (high) | active |
| 2 | H3: Shared datastore (ledger-db or redis-cache) slow, degrading portfolio-service via a neighbour | infrastructure_failure | ledger-db | null | 0.10 (low) | active |
| 3 | H2: Gateway HTTP/2 envoy 1.30 upgrade (v9.3.0) mishandles upstreams to portfolio-service | bad_deployment | api-gateway | tc-003 | 0.07 (low) | active |
| 4 | H4: Capacity/load surge saturates portfolio-service's pool with no faulty change | capacity_saturation | portfolio-service | null | 0.03 (low) | rejected |

### H1: portfolio-service DB connection-pool shrink (cfg-2207) starves /v1/portfolio/holdings; gateway 504s are a symptom
At 09:52:13Z an automated infra-automation job applied the 'small' cost-optimisation profile to portfolio-service, cutting its HikariCP datasource pool from 50 to 5 (cfg-2207, never reverted, F2/F22). The pool cap became effective in the 09:53 bucket: db_pool_in_use drops from ~11.5-12.5 to a hard pinned 5.0 with zero variance (F16/F25) while request_rate (~550-580 rps) and cpu (~35%) are unchanged (F29), so the pin is caused by the size limit, not by load or CPU. Application threads then queue for a connection: the first ERROR at 09:53:19Z is 'could not acquire JDBC connection within 3000ms' on GET /v1/portfolio/holdings (F17), escalating to explicit HikariPool exhaustion 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' with 25-57 waiters (F18/F27). Each request now waits ~3s and holds its upstream connection open, so portfolio-service p99 steps from ~173ms to ~4.25s in the 09:53 minute (F15), portfolio-service error_rate jumps ~90x to ~22% (F28), and the gateway — whose own CPU/memory stay flat (F11) — holds connections to timeout and emits the identical 'upstream timeout to portfolio-service' 504 WARN, first at 09:53:53Z, 34s after the upstream acquire failure (F9/F17) and one minute after the gateway's own 5xx onset (F14). The gateway is a victim.

**Causal chain**
1. infra-automation applies cost-optimisation 'small' profile to portfolio-service, db.pool.max_size 50->5 at 09:52Z; applied in-log at 09:52:13Z; never reverted [F2, F17, F22]
2. Pool cap takes effect: db_pool_in_use falls from ~11.5-12.5 to a pinned 5.0 from the 09:53 bucket onward, with zero headroom [F16, F25, F26]
3. The pin is not load- or CPU-driven: request_rate holds ~550-580 rps and cpu ~35-38% straight through onset [F29]
4. Threads cannot acquire a connection; HikariCP 3s acquire timeouts and pool exhaustion with waiting=25-57, active=5, idle=0 [F17, F18, F27]
5. portfolio-service p99 steps from ~173ms to ~4.25s in the same 09:53 minute and error_rate jumps ~90x to ~22% [F15, F28]
6. Requests are held open past the gateway's upstream timeout; gateway emits route-specific 'upstream timeout to portfolio-service' 504 WARNs (held-open, not fast-fail), first gateway 504 at 09:53:53Z 34s after the upstream acquire error [F9, F17, F12]
7. Gateway p99 saturates ~4.2-4.6s from the 09:53 bucket and 5xx steps to ~7% at 09:54, while gateway CPU (~42%) and memory (~58%) remain flat [F13, F14, F7, F8, F11]

**Supporting evidence**
  - `F2` ✔ A portfolio-service config change dropped the datasource connection pool from 50 to 5 at 2026-03-10T09:52:00Z (cfg-2207, infra-automation, cost-optimisation profile 'small') - 13 minutes before the 10:05Z incident report. This is the only config change on the portfolio path.
  - `F16` ✔ portfolio-service db_pool_in_use drops from ~10.8-12.6 connections (09:35-09:52, mean 9.83 overall) to a hard cap of exactly 5.0 from 09:53:00Z onward (5.0 flat through 10:00), confirming the pool max_size was reduced to 5 and became effective in the 09:53 bucket.
  - `F17` ✔ portfolio-service logs contain the exact config application: INFO at 09:52:13Z 'config change cfg-2207 applied: db.pool.max_size=5' — 100 seconds before the first gateway 504. The FIRST error is at 09:53:19Z, an ERROR 'GET /v1/portfolio/holdings failed: could not acquire JDBC connection within 3000ms' — i.e. the upstream acquire-failure precedes the gateway's first 504 at 09:53:53Z by 34s.
  - `F18` ✔ Pool-exhaustion signature is explicit in portfolio-service logs from 09:54:29Z: 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' (15 occurrences), with waiting queues of 25-57 threads against total=5, active=5, idle=0. The dominant error pattern is the 3s JDBC-acquire timeout, not a connection-refused/fast-fail, matching the gateway's 'held-open-until-timeout' 504 signature from F9.
  - `F22` ✔ cfg-2207 (portfolio-service db.pool.max_size 50 -> 5, changed_at 2026-03-10T09:52:00Z, author infra-automation) is STILL the latest/only portfolio-service config change through 2026-03-10T12:00Z — it was never reverted, and there is no revert entry in the all-services window 08:00-11:59Z.
  - `F25` ✔ portfolio-service db_pool_in_use was steady at ~11.5-12.5 connections until 09:50Z, then dropped to 8.45 (max 11.89) in the 09:52 bucket, and was pinned at exactly the 5-connection ceiling (avg=max=5.0, zero variance) for every 2-min bucket from 09:54 to 10:14Z. Pool saturation therefore begins at ~09:52, not at 08:00 or at first post-deploy traffic.
  - `F26` ✔ The pool was NOT pinned or elevated earlier in the day: 6-min buckets from 07:45 to 09:51Z all show db_pool_in_use between ~11.6 and ~12.3 (max up to 13.3), i.e. comfortably above 5 and stable before 09:52. This kills H2/H4 as an early-onset pool problem and shows the pre-onset pool was not near any ceiling of 5.
  - `F27` ✔ portfolio-service emits HikariCP pool-exhaustion errors with explicit waiters: 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' (47 occurrences) and 'could not acquire JDBC connection within 3000ms' on GET /v1/portfolio/holdings (38 occurrences); ERROR logs start 09:53:19Z, 85 total WARN+ errors returned in 09:20-10:15Z, all ERROR level, none before 09:53:19Z.
  - `F28` ✔ portfolio-service error_rate is flat ~0.14-0.28% through 09:52Z, then jumps to 22.3% in the 09:54 bucket and holds ~21.6-23.2% through 10:14Z — a ~90x step change at the same 09:52-09:54 moment the pool pins at 5.
  - `F29` ✔ Load into portfolio-service is NOT elevated around onset: request_rate holds ~550-580 req/s across 09:20-10:14Z (mean 565.0, first 568.44, last 552.59), and cpu_pct stays ~34-38% (mean 35.88, max 38.67) straight through the onset — so the pool pin is not explained by a CPU or traffic surge.
  - `F15` ✔ portfolio-service p99 likewise shows a clean single-bucket step at 09:53:00Z at 1-min resolution: 09:51=177.51ms, 09:52=172.69ms (baseline ~172-191ms), then 09:53=4250.91ms, 09:54=4546.3ms (summary max 4546.3), holding ~4.0-4.5s. The gateway latency and its upstream latency rise in the SAME 09:53 minute, so the reported 09:45 degraded bucket for portfolio-service is also a 3-min boundary artifact.
  - `F13` ✔ 1-min-resolution re-derivation shows the api-gateway p99 rise is a genuine step change beginning at the 09:53:00Z bucket: 09:51 avg=201.64ms and 09:52 avg=200.33ms (still baseline ~200ms), then 09:53 avg=4418.76ms, holding ~4.2-4.6s through 10:00. The earlier '09:51 partially-degraded bucket (avg 1606.91, max 4418.76)' from the 3-min view is a boundary artifact: at 1-min resolution the 09:51 and 09:52 buckets are flat at baseline, so true onset is 09:53:00Z, not 09:45/09:51.
  - `F14` ✔ At 1-min resolution the api-gateway 5xx error_rate is flat at baseline (~0.13-0.31%, 09:35-09:53) and only steps up at 09:54:00Z (avg=7.18, then 7.74/7.25/7.38...). The reported '09:45 bucket rising to 3.03%' is a 3-min-average artifact that smears the post-onset rise backward; the genuine 5xx onset is 09:54:00Z, one minute after the latency step.
  - `F9` ✔ The first error log is at 09:53:53 and every one of the 252 recurring WARN entries is the same signature: upstream timeout to portfolio-service on GET /v1/portfolio/holdings returning 504 — i.e. connections held open until timeout, not 502/connection-refused fast failures.
  - `F11` ✔ Gateway resource metrics are NOT saturated: CPU stays ~38-47% (avg 42.01) and memory ~51-64% (avg 57.94) across and after onset, flat versus the pre-incident baseline.
  - `F12` ✔ The only non-portfolio WARN signals in the window are isolated and pre/post-date the onset (one TLS cipher warning 07:27:55, one connection-reset retry 08:28:11, one clock-drift warning 10:38:45), consistent with errors being concentrated on the portfolio route rather than all gateway routes.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The HikariCP 'total=5' in the log is the same ceiling as the reduced max_size (the pool gauge itself is not exposed; F30).
  - portfolio-service's /holdings handler acquires a DB connection per request and holds it for the request duration (not directly instrumented beyond the observed waiter/waiting counts).
  - No concurrent shared-datastore fault is also in play: ledger-db/redis-cache metrics were not sampled (gap). A concurrent store slowdown would not by itself produce a pool pinned at exactly the 5-connection ceiling.

### H3: Shared datastore (ledger-db or redis-cache) slow, degrading portfolio-service via a neighbour
ledger-db and redis-cache are shared:true and on the /v1/portfolio/* path (F4/F5/F19/F20). A shared-store fault (failover, lock contention, neighbour load) could slow portfolio-service's DB/cache calls and present as gateway 504s. Precedent HIST-102. This remains untested — no ledger-db/redis-cache latency/error/connection metrics were sampled — but the measured failure mode here is pool-acquire starvation against a shrunk pool (active=5, idle=0, waiting=43-57, total=5) rather than slow queries, which is not what a slow store produces; and there were no deployments on either store in the window (F23).

**Causal chain**
1. ledger-db / redis-cache are shared stores on the portfolio path, reachable by neighbours [F4, F5, F19, F20]
2. A shared-store fault (failover/lock contention/neighbour load) would slow ledger-db or redis-cache *(assumed)*
3. portfolio-service DB/cache calls block to ~4s, its p99 rises [F10, F15]
4. Gateway upstream calls time out -> 504s [F9, F7, F8]

**Supporting evidence**
  - `F4` ✔ Dependency graph for /v1/portfolio/*: api-gateway -> portfolio-service (http) -> ledger-db (db), market-data-service (http), redis-cache (cache); market-data-service -> redis-cache and exchange-gateway. ledger-db, redis-cache and kafka are marked shared:true.
  - `F5` ✔ Shared blast-radius components on this path: ledger-db is also used by order-service, payment-service and auth-service may reach redis-cache; a shared datastore fault could affect portfolio-service via a neighbour.
  - `F6` ✔ Historical precedents (leads only): HIST-116 (2025-12-18) 'HTTP/2 resets after gateway envoy upgrade' with 504 at gateway; HIST-115 (2025-09-25) 'order-service connection pool misconfiguration' with 'pool exhausted' caused by a wrong environment profile; HIST-102 (2025-09-02) portfolio timeouts during ledger-db failover.
  - `F21` ✔ portfolio-service's only direct dependencies are ledger-db (db), market-data-service (http) and redis-cache (cache) — so any portfolio latency must route through one of these three, and gateway->portfolio-service is the only http edge into it.
**Contradicting evidence**
  - `F16` ✔ portfolio-service db_pool_in_use drops from ~10.8-12.6 connections (09:35-09:52, mean 9.83 overall) to a hard cap of exactly 5.0 from 09:53:00Z onward (5.0 flat through 10:00), confirming the pool max_size was reduced to 5 and became effective in the 09:53 bucket.
  - `F18` ✔ Pool-exhaustion signature is explicit in portfolio-service logs from 09:54:29Z: 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' (15 occurrences), with waiting queues of 25-57 threads against total=5, active=5, idle=0. The dominant error pattern is the 3s JDBC-acquire timeout, not a connection-refused/fast-fail, matching the gateway's 'held-open-until-timeout' 504 signature from F9.
  - `F27` ✔ portfolio-service emits HikariCP pool-exhaustion errors with explicit waiters: 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' (47 occurrences) and 'could not acquire JDBC connection within 3000ms' on GET /v1/portfolio/holdings (38 occurrences); ERROR logs start 09:53:19Z, 85 total WARN+ errors returned in 09:20-10:15Z, all ERROR level, none before 09:53:19Z.
  - `F26` ✔ The pool was NOT pinned or elevated earlier in the day: 6-min buckets from 07:45 to 09:51Z all show db_pool_in_use between ~11.6 and ~12.3 (max up to 13.3), i.e. comfortably above 5 and stable before 09:52. This kills H2/H4 as an early-onset pool problem and shows the pre-onset pool was not near any ceiling of 5.
  - `F23` ✔ No deployments at all on ledger-db or redis-cache in 2026-03-10T08:00Z-12:00Z (count:0 each), and zero deployments across all services in 09:00-11:59Z — so no restart/failover was introduced by a deployment on the shared stores in the window.
**Assumptions**
  - An unmeasured shared-store fault coexists, but would have to explain a pool pinned at exactly the new 5-connection ceiling while the pre-change pool sat at ~12 with headroom.

### H2: Gateway HTTP/2 envoy 1.30 upgrade (v9.3.0) mishandles upstreams to portfolio-service
api-gateway v9.3.0 at 08:00Z upgraded the envoy filter chain (1.29->1.30) and enabled HTTP/2 to upstreams (F1). The new HTTP/2 upstream path could reset/hang portfolio-service responses, producing hold-open 504s on /v1/portfolio/*. This is the reporter's suspicion and matches HIST-116. However no telemetry ties the deploy to onset: the gateway p99 step is a single clean bucket at 09:53Z (F13) with flat gateway CPU/memory (F11), and the co-degradation is on the upstream side, where Hikari acquire errors start 34s before the first gateway 504 (F17).

**Causal chain**
1. api-gateway v9.3.0 deploys at 08:00Z enabling HTTP/2 to upstreams (envoy 1.29->1.30) [F1]
2. New HTTP/2 upstream handling would need to hang only the portfolio route while other routes stay healthy *(assumed)*
3. Requests held open until upstream timeout -> 504 on /v1/portfolio/* [F9]
4. Gateway p99 saturates ~4.3s and 5xx ~7% [F7, F8, F13, F14]

**Supporting evidence**
  - `F1` ✔ Only one deployment across all services in the 2026-03-09T00:00Z -> 2026-03-10T11:59Z window: api-gateway v9.3.0 at 2026-03-10T08:00:00Z, 'Upgrade envoy filter chain; enable HTTP/2 to upstreams' (envoy 1.29 -> 1.30), status live, rolled_back_at null.
  - `F6` ✔ Historical precedents (leads only): HIST-116 (2025-12-18) 'HTTP/2 resets after gateway envoy upgrade' with 504 at gateway; HIST-115 (2025-09-25) 'order-service connection pool misconfiguration' with 'pool exhausted' caused by a wrong environment profile; HIST-102 (2025-09-02) portfolio timeouts during ledger-db failover.
  - `F9` ✔ The first error log is at 09:53:53 and every one of the 252 recurring WARN entries is the same signature: upstream timeout to portfolio-service on GET /v1/portfolio/holdings returning 504 — i.e. connections held open until timeout, not 502/connection-refused fast failures.
  - `F12` ✔ The only non-portfolio WARN signals in the window are isolated and pre/post-date the onset (one TLS cipher warning 07:27:55, one connection-reset retry 08:28:11, one clock-drift warning 10:38:45), consistent with errors being concentrated on the portfolio route rather than all gateway routes.
**Contradicting evidence**
  - `F13` ✔ 1-min-resolution re-derivation shows the api-gateway p99 rise is a genuine step change beginning at the 09:53:00Z bucket: 09:51 avg=201.64ms and 09:52 avg=200.33ms (still baseline ~200ms), then 09:53 avg=4418.76ms, holding ~4.2-4.6s through 10:00. The earlier '09:51 partially-degraded bucket (avg 1606.91, max 4418.76)' from the 3-min view is a boundary artifact: at 1-min resolution the 09:51 and 09:52 buckets are flat at baseline, so true onset is 09:53:00Z, not 09:45/09:51.
  - `F14` ✔ At 1-min resolution the api-gateway 5xx error_rate is flat at baseline (~0.13-0.31%, 09:35-09:53) and only steps up at 09:54:00Z (avg=7.18, then 7.74/7.25/7.38...). The reported '09:45 bucket rising to 3.03%' is a 3-min-average artifact that smears the post-onset rise backward; the genuine 5xx onset is 09:54:00Z, one minute after the latency step.
  - `F11` ✔ Gateway resource metrics are NOT saturated: CPU stays ~38-47% (avg 42.01) and memory ~51-64% (avg 57.94) across and after onset, flat versus the pre-incident baseline.
  - `F15` ✔ portfolio-service p99 likewise shows a clean single-bucket step at 09:53:00Z at 1-min resolution: 09:51=177.51ms, 09:52=172.69ms (baseline ~172-191ms), then 09:53=4250.91ms, 09:54=4546.3ms (summary max 4546.3), holding ~4.0-4.5s. The gateway latency and its upstream latency rise in the SAME 09:53 minute, so the reported 09:45 degraded bucket for portfolio-service is also a 3-min boundary artifact.
  - `F16` ✔ portfolio-service db_pool_in_use drops from ~10.8-12.6 connections (09:35-09:52, mean 9.83 overall) to a hard cap of exactly 5.0 from 09:53:00Z onward (5.0 flat through 10:00), confirming the pool max_size was reduced to 5 and became effective in the 09:53 bucket.
  - `F17` ✔ portfolio-service logs contain the exact config application: INFO at 09:52:13Z 'config change cfg-2207 applied: db.pool.max_size=5' — 100 seconds before the first gateway 504. The FIRST error is at 09:53:19Z, an ERROR 'GET /v1/portfolio/holdings failed: could not acquire JDBC connection within 3000ms' — i.e. the upstream acquire-failure precedes the gateway's first 504 at 09:53:53Z by 34s.
  - `F18` ✔ Pool-exhaustion signature is explicit in portfolio-service logs from 09:54:29Z: 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' (15 occurrences), with waiting queues of 25-57 threads against total=5, active=5, idle=0. The dominant error pattern is the 3s JDBC-acquire timeout, not a connection-refused/fast-fail, matching the gateway's 'held-open-until-timeout' 504 signature from F9.
  - `F28` ✔ portfolio-service error_rate is flat ~0.14-0.28% through 09:52Z, then jumps to 22.3% in the 09:54 bucket and holds ~21.6-23.2% through 10:14Z — a ~90x step change at the same 09:52-09:54 moment the pool pins at 5.
**Assumptions**
  - A latent HTTP/2 defect stayed silent for ~113 minutes after deploy and then tripped under unchanged load (request_rate flat, F29).
  - The envoy deploy at 08:00 would have to hang portfolio calls while the upstream's own measured failure mode is a DB-pool acquire timeout, which no HTTP/2 defect would produce.

### H4: Capacity/load surge saturates portfolio-service's pool with no faulty change
A surge in portfolio-page traffic (or a long-held-query pattern) exhausted portfolio-service capacity independent of any change. Directly contradicted by measurement: request_rate is flat at ~550-580 rps across 09:20-10:14Z (mean 565.0) and cpu stays ~34-38% through onset (F29), and the pre-onset pool sat comfortably at ~11.6-12.3 connections with headroom above 5 (F26). Load did not rise; the ceiling dropped.

**Causal chain**
1. Traffic/load on /v1/portfolio/* rises and saturates portfolio-service *(assumed)*
2. Requests queue and time out at the gateway [F9, F15]
3. Gateway p99/504 saturation [F13, F14]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F29` ✔ Load into portfolio-service is NOT elevated around onset: request_rate holds ~550-580 req/s across 09:20-10:14Z (mean 565.0, first 568.44, last 552.59), and cpu_pct stays ~34-38% (mean 35.88, max 38.67) straight through the onset — so the pool pin is not explained by a CPU or traffic surge.
  - `F26` ✔ The pool was NOT pinned or elevated earlier in the day: 6-min buckets from 07:45 to 09:51Z all show db_pool_in_use between ~11.6 and ~12.3 (max up to 13.3), i.e. comfortably above 5 and stable before 09:52. This kills H2/H4 as an early-onset pool problem and shows the pre-onset pool was not near any ceiling of 5.
  - `F16` ✔ portfolio-service db_pool_in_use drops from ~10.8-12.6 connections (09:35-09:52, mean 9.83 overall) to a hard cap of exactly 5.0 from 09:53:00Z onward (5.0 flat through 10:00), confirming the pool max_size was reduced to 5 and became effective in the 09:53 bucket.
  - `F25` ✔ portfolio-service db_pool_in_use was steady at ~11.5-12.5 connections until 09:50Z, then dropped to 8.45 (max 11.89) in the 09:52 bucket, and was pinned at exactly the 5-connection ceiling (avg=max=5.0, zero variance) for every 2-min bucket from 09:54 to 10:14Z. Pool saturation therefore begins at ~09:52, not at 08:00 or at first post-deploy traffic.
  - `F2` ✔ A portfolio-service config change dropped the datasource connection pool from 50 to 5 at 2026-03-10T09:52:00Z (cfg-2207, infra-automation, cost-optimisation profile 'small') - 13 minutes before the 10:05Z incident report. This is the only config change on the portfolio path.
**Assumptions**
  - A hidden load surge exists despite the flat, measured request_rate.
**Rejected because:** F29 shows request_rate flat at ~550-580 rps and CPU ~35% straight through onset, and F26 shows the pre-onset pool at ~11.6-12.3 with headroom, so no capacity/load surge occurred. The pool pinned at exactly the newly-lowered 5-connection ceiling (F16/F25), a limit set by cfg-2207 (F2), not by demand.

## Facts (verified observations)
- At 09:52:13Z portfolio-service logged 'config change cfg-2207 applied: db.pool.max_size=5', an infra-automation 'small' cost-optimisation profile that cut the datasource pool from 50 to 5; it is the only config change on the portfolio path and its author is infra-automation. [F2, F17, F22]
- cfg-2207 was never reverted: it is still the latest/only portfolio-service config change through 2026-03-10T12:00Z and there is no revert entry in the 08:00–11:59Z all-services window. [F22]
- portfolio-service db_pool_in_use was steady at ~11.5–12.5 connections until 09:50Z (6-min buckets 07:45–09:51Z all ~11.6–12.3, max 13.3), then dropped to 8.45 in the 09:52 bucket and pinned at exactly 5.0 with zero variance from 09:53Z onward — confirming the max_size reduction became effective in the 09:53 bucket. [F16, F25, F26]
- The pool pin is a size limit, not demand: request_rate held ~550–580 rps (mean 565.0) across 09:20–10:14Z and cpu_pct stayed ~34–38% (mean 35.88) straight through onset. [F29]
- The first portfolio-service error is at 09:53:19Z, an ERROR 'GET /v1/portfolio/holdings failed: could not acquire JDBC connection within 3000ms' — preceding the first gateway 504 at 09:53:53Z by 34 seconds; no ERROR-level logs exist before 09:53:19Z. [F17, F27]
- Pool-exhaustion is explicit in portfolio-service logs from 09:54:29Z: 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)', with waiting queues of 25–57 threads against total=5, active=5, idle=0. [F18, F27]
- portfolio-service p99 shows a clean single-bucket step at 09:53:00Z at 1-min resolution (09:51=177.51ms, 09:52=172.69ms, then 09:53=4250.91ms, 09:54=4546.3ms), and portfolio-service error_rate jumps ~90x from ~0.14–0.28% to ~22% in the 09:54 bucket, holding ~21.6–23.2%. [F15, F28]
- api-gateway p99 rises as a genuine step change beginning in the 09:53:00Z bucket at 1-min resolution (09:51 avg=201.64ms, 09:52 avg=200.33ms, 09:53 avg=4418.76ms), with the 5xx error rate stepping up one minute later at 09:54:00Z (~7.18%). [F13, F14]
- Every one of the 252 recurring gateway WARN entries has the same signature — 'upstream timeout to portfolio-service' on GET /v1/portfolio/holdings returning 504 — i.e. connections held open until timeout rather than fast 502/connection-refused failures, first at 09:53:53Z. [F9]
- api-gateway resource metrics are not saturated: CPU ~38–47% (avg 42.01) and memory ~51–64% (avg 57.94), flat versus the pre-incident baseline. [F11]
- The only deployment in the entire 2026-03-09T00:00Z–2026-03-10T11:59Z window is api-gateway v9.3.0 at 08:00:00Z ('Upgrade envoy filter chain; enable HTTP/2 to upstreams', envoy 1.29->1.30), live and never rolled back. [F1]
- No deployments touched ledger-db or redis-cache in 08:00–12:00Z (count 0 each) and there were zero deployments across all services in 09:00–11:59Z, so the shared stores saw no change-induced restart or failover in the window. [F23]
- The only non-portfolio gateway WARN signals in the window are isolated and pre/post-date onset (TLS cipher warning 07:27:55, connection-reset retry 08:28:11, clock-drift warning 10:38:45), consistent with errors concentrated on the portfolio route. [F12]
- The /v1/portfolio/* path is api-gateway -> portfolio-service (http) -> ledger-db (db), market-data-service (http), redis-cache (cache); ledger-db and redis-cache are marked shared:true and are reached by four and four neighbour services respectively. [F4, F19, F20, F21]
- Beyond the portfolio pool shrink and the earlier gateway upgrade, no other change was active on the portfolio path at 11:59Z. [F3]
- portfolio-service does not expose a pool-size gauge, connection-acquire wait time, in-flight or queue-depth metric; its available_metrics are cpu_pct, db_pool_in_use, error_rate, latency_p99_ms, memory_pct, request_rate, restarts, so the 5-connection ceiling and waiter counts were read from the HikariPool error logs. [F30]
- Queries for portfolio-service metrics request_error_rate and db_pool_size failed with not_found, confirming the pool-size gauge is not exposed under those names. [F30]

## Assumptions (not verified)
- The HikariCP 'total=5' in the log is the same ceiling as the reduced max_size; the pool-size gauge itself is not exposed (F30).
- Each /v1/portfolio/holdings request acquires a DB connection and holds it for the request duration; this is not directly instrumented beyond the observed waiter and waiting counts.
- No concurrent shared-datastore fault is also in play: ledger-db/redis-cache metrics were never sampled. A concurrent store slowdown would not by itself produce a pool pinned at exactly the 5-connection ceiling with zero variance.
- The pre-change HikariPool total=50 was never observed directly in a log line; the 50 value rests on the config-change record (F2/F22) plus the pre-onset db_pool_in_use plateau of ~11.6–12.3 (F26).

## Timeline
- `2026-03-10T08:00:00Z` Only deployment in the window: api-gateway v9.3.0, 'Upgrade envoy filter chain; enable HTTP/2 to upstreams' (envoy 1.29 -> 1.30), status live, rolled_back_at null. [F1]
- `2026-03-10T09:50:00Z` portfolio-service db_pool_in_use still steady at ~11.5–12.5 connections (6-min buckets 07:45–09:51Z all ~11.6–12.3); baseline gateway p99 ~200ms, portfolio-service p99 ~172–191ms. [F25, F26, F13, F15]
- `2026-03-10T09:52:00Z` db_pool_in_use drops to 8.45 (max 11.89) in the 09:52 bucket; cfg-2207 changed_at is 09:52:00Z. [F25, F2]
- `2026-03-10T09:52:13Z` portfolio-service logs INFO 'config change cfg-2207 applied: db.pool.max_size=5' — 100 seconds before the first gateway 504. [F17]
- `2026-03-10T09:53:00Z` Pool pins at exactly 5.0 with zero variance; gateway p99 and portfolio-service p99 both step in this same minute (gateway 09:53 avg=4418.76ms; portfolio-service 09:53=4250.91ms) after flat baseline at 09:51/09:52. [F16, F13, F15]
- `2026-03-10T09:53:19Z` First portfolio-service ERROR: 'GET /v1/portfolio/holdings failed: could not acquire JDBC connection within 3000ms' — the upstream acquire failure preceds the gateway symptom. [F17, F27]
- `2026-03-10T09:53:53Z` First api-gateway WARN: 'upstream timeout to portfolio-service' on GET /v1/portfolio/holdings returning 504 — 34s after the upstream acquire failure, 100s after the config application. [F9, F17]
- `2026-03-10T09:54:00Z` portfolio-service error_rate jumps to 22.3% (from ~0.14–0.28%) and gateway 5xx steps to ~7.18%, holding through 10:14Z / 11:45Z respectively. [F28, F14, F8]
- `2026-03-10T09:54:29Z` Explicit HikariPool exhaustion signature in portfolio-service logs: 'Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)', with waiting queues of 25–57 threads. [F18, F27]
- `2026-03-10T10:05:00Z` Incident INC-002 reported by on-call SRE: portfolio page spinning/timing out, gateway p99 > 4s, 504s on /v1/portfolio/*; reporter suspects the gateway HTTP/2 upgrade. [F1, F9]
- `2026-03-10T11:59:00Z` End of data window: cfg-2207 still the latest portfolio-service config change (never reverted); no deployments on ledger-db/redis-cache; gateway v9.3.0 still live. [F22, F24, F23]

## Recommended actions
- **[immediate]** Revert config change cfg-2207 on portfolio-service (restore db.pool.max_size from 5 back to 50) via infra-automation change control, then confirm the new value is applied and effective. -- cfg-2207 is the only change on the portfolio path at onset, is never reverted (F22/F24), and its 09:52:13Z application is immediately followed by the pool pinning at exactly 5.0 with zero variance (F16/F25) and by HikariCP acquire-timeout errors (F17/F18/F27). Restoring the ceiling removes the resource that is causing requests to queue. [F2, F16, F17, F18, F22, F25, F27]
- **[immediate]** Verify recovery after the revert: watch portfolio-service db_pool_in_use to confirm headroom returns above 5 (back toward ~11–12), and confirm the HikariCP 'total=5, active=5, idle=0, waiting=…' acquire-timeout errors and portfolio-service error_rate (~22%) stop. -- The pool being pinned at exactly 5.0 with waiters is the decisive measured mechanism (F16/F18/F27) and error_rate jumped ~90x at the same moment (F28); these are the exact signals that should reverse when the ceiling is restored. [F16, F18, F25, F27, F28]
- **[immediate]** Verify the gateway recovers as a victim: confirm gateway p99 returns from ~4.2–4.6s toward the ~200ms baseline, gateway 5xx falls from ~7% to ~0.2%, and 'upstream timeout to portfolio-service' 504 WARNs stop; do NOT roll back api-gateway v9.3.0 as the primary mitigation. -- Gateway CPU/memory stayed flat (F11) and every gateway WARN is a held-open upstream timeout that began 34s after the upstream acquire failure (F9/F17), so the gateway is downstream of the fault; the deploy produced no portfolio-path effect for ~113 minutes (F26). [F9, F11, F13, F14, F17, F26]
- **[immediate]** If latency/errors do not recover fully after the revert, immediately sample ledger-db and redis-cache latency, error-rate and connection metrics for the 09:00–10:10Z window to discriminate H3 (shared-store fault) from residual H1 effects. -- H3 was never directly tested — no ledger-db/redis-cache time-series was collected — and it is the only remaining live alternative to H1; it can be ruled in or out with a single metric query round. [F4, F19, F20, F23]
- **[short_term]** Add a guardrail to the infra-automation cost-optimisation profile: cap how far a 'small' profile may shrink a datasource pool (or require a minimum pool size relative to measured peak connections) and require a canary/step-down before applying pool reductions to production services. -- A 50->5 (10x) reduction was applied automatically with no observed canary, dropping the pool below the ~11.6–12.3 connections the service actually used (F25/F26) and pinning it at the new ceiling. [F2, F22, F25, F26]
- **[short_term]** Expose the missing portfolio-service signals — db_pool_size (ceiling), connection-acquire wait time, in-flight/queue depth — and alert on pool saturation and acquire-timeout rate, since these had to be reconstructed from HikariPool error logs. -- portfolio-service exposes no pool-size gauge or acquire-wait metric (F30), so the 5-connection ceiling and waiter counts came only from log samples (F18/F27); an alert on pool-in-use == ceiling with waiters would have caught this at 09:52. [F30, F18, F27, F16]
- **[long_term]** Alert on config changes to connection-pool settings on request-path services and require a pre-change check that the new pool size exceeds observed peak db_pool_in_use. -- The pool shrank below measured usage (~12) with no automated check comparing the proposed 5 against observed demand, and no alert fired on the config change itself (F2/F22/F25/F26). [F2, F22, F25, F26]
- **[long_term]** Re-run the untaken confirmation queries to close the evidence gaps: sub-bucket gateway timing vs the 08:00Z deploy, per-route/per-upstream gateway metrics (/v1/portfolio/* vs auth/accounts/market-data), and ledger-db/redis-cache health for 09:00–10:10Z. -- These were left uncollected due to tool-budget exhaustion; they would independently confirm route-specificity, the deploy being non-causal, and the absence of a concurrent shared-store fault underpinning H3. [F13, F14, F26, F12, F23]

## Data gaps
- No ledger-db or redis-cache latency/error/connection metrics were ever sampled for 09:00–10:10Z, so H3 (shared-datastore slowdown) remains directly untested; the conclusion rests on the shape of the failure (acquire-queue starvation against a pool pinned at exactly 5) rather than on direct evidence of store health.
- portfolio-service's own metric names request_error_rate and db_pool_size are not exposed (queries failed not_found); the 5-connection ceiling came from HikariCP log fields (total=5) rather than a pool-size gauge, and acquire-wait distribution / queue depth are unmeasured beyond sampled log values.
- The pre-change HikariPool total=50 was never observed in a log line; the 50 value rests on the config-change record plus the pre-onset db_pool_in_use plateau of ~11.6–12.3.
- Route-specificity is inferred from the log pattern (isolated non-portfolio WARNs) but per-route/per-upstream gateway metrics (/v1/portfolio/* vs auth/accounts/market-data over 09:40–10:10Z) were never queried.
- Neighbour-service traffic on the shared ledger-db / redis-cache (order-service, payment-service, auth-service) in 09:00–10:10Z was not measured, so a neighbour-load contribution to a store slowdown cannot be fully excluded.
- No cluster/infra events (node scaling, DB failover, lock contention) were available to query; only deployment lists were checked (count 0 for both stores), so an unreported infra event on the shared stores is not fully excluded.
- The exact api-gateway v9.3.0 deploy log line was not retrieved, so the deploy-to-onset gap is quantified only from the change record (08:00Z), not from a gateway log marker.

## What would change this conclusion
- Direct ledger-db or redis-cache metrics showing a latency/error/connection anomaly or a failover/lock-contention event starting ~09:45–09:52Z, which would revive H3 as a contributing or alternative cause.
- Evidence that portfolio-service db_pool_in_use was already at or near 5 before 09:52Z, or that the pool ceiling was never actually reduced, which would break the causal link between cfg-2207 and the pin.
- HikariPool logs showing waiters or acquire timeouts with a pool ceiling well above 5 (i.e. requests queueing despite headroom), which would indicate a slow store or slow query rather than size-induced starvation.
- A verified deploy or change on ledger-db / redis-cache, or a portfolio-service or gateway change, at ~09:52Z other than cfg-2207.
- Sub-minute resolution showing gateway/portfolio degradation beginning before 09:52:13Z (e.g. tracking the 08:00Z HTTP/2 upgrade rather than the config change), which would support H2.
- Requests in flight before 09:52 continuing to succeed with p99 ~200ms after the config change, which would show the pool shrink had no causal effect.

## Investigation stats
- latency 147.41s, LLM calls 24, tokens 170770 (in 139309 / out 31461)
- tool calls 38 (2 failed, 0 partial, 0 recovered by retry), findings 31/31 verified
