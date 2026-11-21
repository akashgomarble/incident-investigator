# Incident investigation report: INC-008
*System: multi_agent | outcome: **inconclusive** | ended: critic_accepted*

## Summary
Login slowness and 503s on auth-service trace to auth-service itself becoming CPU-saturated: aggregate CPU stepped from a ~34-40% baseline to ~96-97% at ~06:52 in the same 2-minute bucket that request_rate first upticked (from a flat ~400 req/s), with p99 inflecting one bucket later at 06:54 (90 ms -> 3.76-3.88 s), all 64 workers saturated (queue 39035) and 'accept queue full' 503s on /v1/auth/login from 06:55:42. Every measured dependency candidate is exonerated as an origin: redis-cache and its three co-consumers plus the only service deployed this week (portfolio-service) are flat across onset; user-db is flat on cpu/lag/slow-queries/disk through 06:51 with connections_active rising only after auth onset; api-gateway degrades 3-4 minutes later with no resource saturation of its own. The reporter's bcrypt-regression framing is not supported - there was no auth-service deployment in the entire week (F10) and no bcrypt signature in the logs (F7). The best-supported origin is therefore an in-process per-request CPU-cost increase inside auth-service (H1, confidence 0.45) on an unchanged workload, but because the trigger is unidentified and the CPU-vs-request-rate ordering cannot be resolved below 2 minutes, H1 does not cross the 0.50 threshold and the outcome is honestly inconclusive.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: auth-service CPU saturation from a rise in per-request compute cost (no tracked change; bcrypt framing unsupported) | unknown | auth-service | null | 0.45 (low) | active |
| 2 | H2: Capacity ceiling: login demand outran auth-service's provisioned concurrency with no faulty change | capacity_saturation | auth-service | null | 0.22 (low) | active |
| 3 | H4: Dependency-first degradation on auth-service's synchronous login path (user-db, kyc-provider, or api-gateway path) | dependency_failure | user-db | null | 0.12 (low) | active |
| 4 | H3: Reporter's prior-incident mechanism: bcrypt cost regression from an auth-service release | bad_deployment | auth-service | null | 0.05 (low) | rejected |

### H1: auth-service CPU saturation from a rise in per-request compute cost (no tracked change; bcrypt framing unsupported)
auth-service itself became CPU-bound at ~06:50-06:52: its aggregate CPU stepped from a flat ~34-40% baseline to ~97% while inbound request_rate was still at baseline (~400 req/s, 452 at 06:50), i.e. CPU/request cost rose rather than load rising first. All 64 workers saturate (queue 39035), the accept queue fills and returns 503 on /v1/auth/login, and accepted requests wait behind the saturated pool (p99 ~90 ms -> 3.8 s). The origin is inside auth-service's request-handling path. The trigger is genuinely unknown: no auth-service deploy or meaningful config change exists in the week, so the cost increase must come from an untracked change (in-flight code, feature flag, request-mix/JWT-size change, or an injected per-request work path) or from an in-process pathology (retry spin, lock contention, GC) that presents as CPU. The bcrypt framing of the original report is NOT supported (F7: no bcrypt signature; F10/F11: no auth-service release), so the mechanism is 'CPU cost per request rose', cause unidentified.

**Causal chain**
1. auth-service CPU steps from ~34.4% (06:50 bucket) to 96.89% (06:52 bucket) while inbound request_rate is still baseline (452.43 at 06:50) — cost per request rose on a flat workload [F22, F21, F25]
2. At ~97% CPU all 64 workers are occupied and the internal queue grows to 39035 [F4, F3, F7]
3. The accept queue overflows, so the listener returns 503 on /v1/auth/login (first at 06:55:42); error rate settles at ~14% [F3, F2]
4. Requests that are accepted wait behind the saturated pool, pushing p99 from ~90 ms to ~3.8 s (inflection 06:54 bucket) [F1, F23]
5. Elevated request_rate (742 at 06:52 -> 1749 at 06:58) and rate_limited_per_min (1.2 -> 451 at 06:54) are consequences — retries/backlog against an already-slow service — and the HPA cap (desired 14 > maxReplicas 8) prevents scaling out of it [F21, F24, F6, F25]

**Supporting evidence**
  - `F22` ✔ auth-service per-service CPU jumped FIRST, in the 06:52 bucket: 96.89% avg / 96.96% max, from a flat ~34-40% baseline (06:50 bucket was 34.42 avg). It held 96.6-98.8% through 07:14 (07:12 max 98.84). The CPU step (06:52) and the request_rate step (06:52, 742.81) land in the SAME 2-min bucket — request rate did not precede CPU.
  - `F21` ✔ auth-service inbound request_rate was flat at ~381-418 req/s for the entire baseline 06:00-06:48 (e.g. 06:42 avg 403.12, 06:44 avg 404.07, 06:48 avg 386.14). It did NOT rise before the CPU jump: at the 06:50 bucket it was still only 452.43 avg (max 529.5), and it inflected upward only in the 06:52 bucket (742.81) and after — 1094.83 at 06:54, 1350.36 at 06:56, peaking 1749.75 avg at 06:58.
  - `F25` ✔ Interpretation for the H1-vs-H2 test: within the 2-min resolution, request_rate did NOT rise before the ~38%->~97% CPU jump — CPU saturates in the 06:52 bucket side-by-side with the first request-rate uptick, and auth-service p99 only moves one bucket later (06:54). The elevated inbound rate itself is best read as a consequence of the already-slow service (retries/backlog: once p99 goes to ~3.76 s at 06:54, request_rate keeps climbing to 1749.75 at 06:58 and rate_limited caps at ~900/min), not as a pre-onset volume surge. This ordering is consistent with H1 (CPU cost per request rose on an unchanged ~400 req/s baseline) and is not explained by H2's premise of a login surge that outran capacity before the CPU climb.
  - `F4` ✔ auth-service CPU jumped from ~38% (05:30-06:42) to 89.96% avg (max 97.33) in the 06:51 bucket and stayed ~96-97% through 08:57 — a sharp, sustained CPU saturation coincident with the latency/error onset.
  - `F3` ✔ First 503 on /v1/auth/login is logged at 06:55:42 as 'accept queue full, returning 503 (POST /v1/auth/login)'; the immediately preceding WARN at 06:54:53 is 'worker pool saturated (active=64/64, queue=39035)'. Top ERROR pattern (372 occurrences) is the accept-queue-full 503.
  - `F7` ✔ No bcrypt/CPU-regression or panic log signature appears in the auth-service WARN/ERROR set; the observed log patterns are queue/pool saturation (worker pool saturated active=64/64), accept queue full 503s, one /internal/metrics slow-request, and an NTP clock-drift message — the evidence points to saturation, not an explicit bcrypt message.
  - `F1` ✔ auth-service p99 latency rose from a ~89-90 ms baseline (05:30-06:42) to 2532 ms in the 06:51 bucket and held ~3.78-3.88 s through 08:57, so onset is between 06:42 and 06:51.
  - `F2` ✔ auth-service error_rate was 0.18-0.25% through 06:42, then spiked to 7.88% in the 06:51 bucket (max 14.76) and settled at ~14% (max 15.67) from 07:00 onward.
  - `F23` ✔ The p99 latency inflection is in the 06:54 bucket (3759.86 ms avg, 3849.2 max), i.e. AFTER both the CPU step and the request_rate step. The 06:50-06:52 buckets were still baseline-fast (93.94 ms avg at 06:50, 91.5 ms avg at 06:52); p99 stayed ~3.55-3.93 s from 06:54 through 07:14.
  - `F24` ✔ rate_limited_per_min is a genuine exposed auth-service metric (listed in available_metrics) and shows the same ordering: flat ~1.3-2.6 req/min through 06:52 (1.21 at 06:52), then a sharp rise only at 06:54 (451.86 avg, max 902.06) and ~868-920 req/min from 06:56 onward — it lags rather than leads the CPU step, consistent with the documented autoscaler cap (maxReplicas 8) rejecting surplus connections once CPU saturated. request_rate IS exposed, so it is the direct proxy; rate_limited_per_min corroborates.
**Contradicting evidence**
  - `F10` ✔ There was NO auth-service deployment between 2026-04-15T00:00Z and 2026-04-22T09:00Z: the deployment list for that whole week contains exactly one entry, portfolio-service v4.4.0 at 2026-04-22T05:50:00Z (status live, rolled_back_at null). This contradicts the report's premise of an auth-service release.
  - `F11` ✔ The only config change in the 2026-04-15..2026-04-22 window is cfg-2801 on auth-service: log.level INFO->WARN at 2026-04-22T03:40:00Z by config-bot ("Reduce log volume"). Nothing else changed in config across all services.
  - `F5` ✔ auth-service memory stayed flat at ~51-53% for the entire window and restarts were 0 throughout, so the failure is not a memory-driven crash or pod restart.
  - `F6` ✔ Rate-limiting on auth-service rose from ~2 req/min baseline to 487 in the 06:51 bucket and ~900 req/min (max 979.25) from 07:00 on, alongside an autoscaler message 'desired replicas 14 exceeds maxReplicas 8; cannot scale further' — indicating demand/connection pressure the service could not scale out of.
**Assumptions**
  - The CPU is consumed by auth-service's own request path (per-request compute) rather than by an in-process stall that presents as CPU (spinning retry loop, lock contention, GC).
  - Some untracked change or request-mix shift raised per-request CPU cost; no tracked deploy/config event explains it (F10, F11).
  - The 2-min bucket resolution does not hide a request_rate rise in the tens of seconds immediately before the CPU step; F25 accepts this cannot be excluded below 2 minutes.
  - Aggregate CPU across the 8-replica cap is representative of per-pod cost; no per-pod series exists.

### H2: Capacity ceiling: login demand outran auth-service's provisioned concurrency with no faulty change
The origin is demand plus a provisioning ceiling rather than a change: a login-traffic surge hit auth-service, whose autoscaler is capped (autoscaler message 'desired replicas 14 exceeds maxReplicas 8; cannot scale further'). With fixed capacity and 64 workers/pod, the worker pool and accept queue saturate, producing ~3.8 s p99 and 503s, and ~97% CPU is the expected consequence of running at provisioned capacity. This differs from H1 in origin (demand vs. per-request cost) and in fix (raise the replica/pool ceiling and/or rate-limit the surge vs. find the code path that got more expensive).

**Causal chain**
1. Login request volume surges, visible as rate-limited requests climbing from ~2/min to 487 (06:51) and ~900/min (07:00+) [F6]
2. The autoscaler cannot absorb the surge: desired replicas 14 exceeds maxReplicas 8, so no additional capacity is added [F6]
3. Fixed worker capacity (64/64) is exhausted and the internal queue grows to 39035, driving CPU to ~96-97% [F3, F7, F4]
4. The accept queue fills and returns 503 on /v1/auth/login; queued requests see multi-second p99 (~90 ms -> 3.8 s) [F3, F1, F2]

**Supporting evidence**
  - `F6` ✔ Rate-limiting on auth-service rose from ~2 req/min baseline to 487 in the 06:51 bucket and ~900 req/min (max 979.25) from 07:00 on, alongside an autoscaler message 'desired replicas 14 exceeds maxReplicas 8; cannot scale further' — indicating demand/connection pressure the service could not scale out of.
  - `F3` ✔ First 503 on /v1/auth/login is logged at 06:55:42 as 'accept queue full, returning 503 (POST /v1/auth/login)'; the immediately preceding WARN at 06:54:53 is 'worker pool saturated (active=64/64, queue=39035)'. Top ERROR pattern (372 occurrences) is the accept-queue-full 503.
  - `F1` ✔ auth-service p99 latency rose from a ~89-90 ms baseline (05:30-06:42) to 2532 ms in the 06:51 bucket and held ~3.78-3.88 s through 08:57, so onset is between 06:42 and 06:51.
  - `F2` ✔ auth-service error_rate was 0.18-0.25% through 06:42, then spiked to 7.88% in the 06:51 bucket (max 14.76) and settled at ~14% (max 15.67) from 07:00 onward.
  - `F4` ✔ auth-service CPU jumped from ~38% (05:30-06:42) to 89.96% avg (max 97.33) in the 06:51 bucket and stayed ~96-97% through 08:57 — a sharp, sustained CPU saturation coincident with the latency/error onset.
**Contradicting evidence**
  - `F21` ✔ auth-service inbound request_rate was flat at ~381-418 req/s for the entire baseline 06:00-06:48 (e.g. 06:42 avg 403.12, 06:44 avg 404.07, 06:48 avg 386.14). It did NOT rise before the CPU jump: at the 06:50 bucket it was still only 452.43 avg (max 529.5), and it inflected upward only in the 06:52 bucket (742.81) and after — 1094.83 at 06:54, 1350.36 at 06:56, peaking 1749.75 avg at 06:58.
  - `F24` ✔ rate_limited_per_min is a genuine exposed auth-service metric (listed in available_metrics) and shows the same ordering: flat ~1.3-2.6 req/min through 06:52 (1.21 at 06:52), then a sharp rise only at 06:54 (451.86 avg, max 902.06) and ~868-920 req/min from 06:56 onward — it lags rather than leads the CPU step, consistent with the documented autoscaler cap (maxReplicas 8) rejecting surplus connections once CPU saturated. request_rate IS exposed, so it is the direct proxy; rate_limited_per_min corroborates.
  - `F25` ✔ Interpretation for the H1-vs-H2 test: within the 2-min resolution, request_rate did NOT rise before the ~38%->~97% CPU jump — CPU saturates in the 06:52 bucket side-by-side with the first request-rate uptick, and auth-service p99 only moves one bucket later (06:54). The elevated inbound rate itself is best read as a consequence of the already-slow service (retries/backlog: once p99 goes to ~3.76 s at 06:54, request_rate keeps climbing to 1749.75 at 06:58 and rate_limited caps at ~900/min), not as a pre-onset volume surge. This ordering is consistent with H1 (CPU cost per request rose on an unchanged ~400 req/s baseline) and is not explained by H2's premise of a login surge that outran capacity before the CPU climb.
**Assumptions**
  - The surge is exogenous (real or synthetic login flood) and not itself produced by clients retrying an already-slow service.
  - The maxReplicas=8 ceiling is genuinely binding — F6 quotes the HPA message but no replica/time series was retrieved.
  - A ~400 req/s workload could not, by itself, have been at 38% CPU a minute earlier if the surge were the sole cause; this hypothesis must assume a much larger effective concurrency than the measured request_rate suggests, or that request_rate is under-reported.

### H4: Dependency-first degradation on auth-service's synchronous login path (user-db, kyc-provider, or api-gateway path)
Something on auth-service's synchronous login path degraded first and auth-service queued on it, saturating its worker pool and accept queue. The tested shared components are exonerated (redis-cache flat CPU 21.5% and hit ratio 95.9% at onset; its three co-consumers and portfolio-service all flat), so the remaining untested candidates are user-db (the login path's datastore; only proxy metrics exist — connections_active, replication_lag_ms, slow_queries_per_min, disk_used_pct) and the external kyc-provider (has_telemetry=false, unmeasurable). This is the orchestrator's 'victim' theory, now narrowed to the components that were never measured.

**Causal chain**
1. user-db or kyc-provider on the login path degrades at or just before onset *(assumed)*
2. auth-service's synchronous calls block, consuming all 64 workers and growing the internal queue [F3, F7]
3. Worker pool saturation persists without a strong CPU-bound signature in logs, and p99 rises to ~3.8 s in the 06:51 bucket [F1, F3, F7]
4. The saturated/queued service overflows the accept queue and returns 503; request_rate and rate-limit counters then climb as clients retry [F3, F2, F21, F24]

**Supporting evidence**
  - `F8` ✔ auth-service's dependency graph: outgoing edges to user-db (db), redis-cache (cache), kyc-provider (external, has_telemetry=false); api-gateway has an http edge INTO auth-service. No edge to a separate session/token store or secrets service was returned.
  - `F9` ✔ redis-cache is a SHARED datastore (shared:true) consumed by four services: auth-service, portfolio-service, pricing-engine, market-data-service — so a change or degradation in redis-cache or in any neighbouring consumer can affect auth-service login.
  - `F13` ✔ Relevant precedents for auth-service slowness that do NOT involve an auth-service release: HIST-107 (2025-12-09) 'Cold cache storm after redis failover' on redis-cache/market-data-service, root cause 'Redis primary failure promoted an empty replica', resolution 'Warmed cache; fixed replica sync checks'; and HIST-110 (2026-01-15) latency from datastore IOPS throttling. Both are infrastructure/datastore-side leads matching the shared redis-cache path.
  - `F19` ✔ user-db does not expose latency_p99_ms; its available metrics are connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min, so user-db latency/IOPS could not be compared against onset with this query.
**Contradicting evidence**
  - `F14` ✔ redis-cache showed no degradation at onset: CPU averaged 21.55% in the 06:51 bucket vs 22.33% at 06:42 and 22.14% at 07:00 (window mean 22.02%, max 25.59), i.e. flat through the auth-service onset.
  - `F15` ✔ redis-cache showed no cache-storm or capacity signature at onset: hit_ratio_pct stayed 95.86% in the 06:51 bucket (window mean 96.02%, min 93.87%) and evictions_per_min stayed ~3/min (3.14 at 06:51, window mean 2.99), matching the pre-onset 06:42 values (96.01%, 2.75).
  - `F16` ✔ portfolio-service — the only service deployed in the week (v4.4.0 at 05:50Z) and a co-consumer of redis-cache — shows no latency change at onset: p99 avg 180.28 ms in the 06:51 bucket vs 180.46 ms at 06:42, holding ~174-185 ms for the whole window (mean 180.23 ms).
  - `F17` ✔ The other two shared-redis consumers are also flat across onset: pricing-engine p99 avg 59.66 ms in the 06:51 bucket (window mean 59.89 ms) and market-data-service p99 avg 40.17 ms (window mean 39.90 ms), both unchanged from 06:42.
  - `F18` ✔ No dependency latency rise precedes or coincides with auth-service's onset: the shared redis-cache, its three co-consumers, and portfolio-service all remain at baseline in the 06:42 and 06:51 buckets while auth-service p99 rose from ~90 ms to 2532 ms in that same 06:51 bucket — so the dependency-first / shared-component-degradation candidates are not supported by the measured services.
  - `F27` ✔ user-db connections_active was FLAT through the auth-service onset, then doubled: it held 22.79-27.75 for every minute 06:30-06:51 (06:42=26.17, 06:51=26.39) and jumped to 72.59 at 06:52, staying 72.22-84.53 through 07:15 (e.g. 06:54=79.84, 07:01=84.53). The rise therefore begins one bucket AFTER auth-service's 06:51 onset (auth p99 2532 ms in the 06:51 bucket), not before it.
  - `F28` ✔ user-db's other four metrics are flat across onset with no inflection: replication_lag_ms stayed 27.65-32.31 ms for all 46 minutes (06:42=31.44, 06:51=30.99, 06:52=30.47; mean 29.96), cpu_pct stayed 16.63-19.94% (06:42=16.98, 06:51=18.97, 06:52=19.24; mean 18.0), slow_queries_per_min stayed 0.18-0.51/min (06:42=0.22, 06:51=0.35, 07:10=0.51), and disk_used_pct stayed 43.41-44.87% (06:42=43.73, 06:51=43.9) — no saturation, lag spike, slow-query burst or disk exhaustion at or before 06:51.
**Assumptions**
  - user-db could have degraded in a way its flat proxy metrics (replication_lag_ms 27.65-32.31, cpu_pct 16.6-19.9, slow_queries 0.18-0.51, disk 43.4-44.9) would not show — e.g. a latency/IOPS or connection-pool wall on the auth path.
  - kyc-provider is called synchronously on /v1/auth/login and could be slow/failing; it has no telemetry at all (has_telemetry=false).
  - Blocking on a dependency can drive auth-service CPU to 97% (e.g. retry spin, TLS/JSON rework on retries); otherwise the flat, saturated CPU signature favours an in-process cause.

### H3: Reporter's prior-incident mechanism: bcrypt cost regression from an auth-service release
A release of auth-service raised the bcrypt cost factor (as in HIST-101, 2025-08-14), making each login hash far more CPU-expensive; CPU saturates, workers queue, and the accept queue returns 503. Retained as the reporter's explicit suspicion, but its stated prerequisite — an auth-service release — is falsified by the deployment inventory.

**Causal chain**
1. An auth-service release raising the bcrypt cost factor is deployed before onset *(assumed)*
2. Login hashing becomes CPU-bound, driving CPU to ~97% [F4]
3. Worker pool saturates (64/64, queue 39035) and the accept queue returns 503 [F3]
4. p99 rises to ~3.8 s and error rate to ~14% [F1, F2]

**Supporting evidence**
  - `F4` ✔ auth-service CPU jumped from ~38% (05:30-06:42) to 89.96% avg (max 97.33) in the 06:51 bucket and stayed ~96-97% through 08:57 — a sharp, sustained CPU saturation coincident with the latency/error onset.
  - `F3` ✔ First 503 on /v1/auth/login is logged at 06:55:42 as 'accept queue full, returning 503 (POST /v1/auth/login)'; the immediately preceding WARN at 06:54:53 is 'worker pool saturated (active=64/64, queue=39035)'. Top ERROR pattern (372 occurrences) is the accept-queue-full 503.
  - `F1` ✔ auth-service p99 latency rose from a ~89-90 ms baseline (05:30-06:42) to 2532 ms in the 06:51 bucket and held ~3.78-3.88 s through 08:57, so onset is between 06:42 and 06:51.
  - `F2` ✔ auth-service error_rate was 0.18-0.25% through 06:42, then spiked to 7.88% in the 06:51 bucket (max 14.76) and settled at ~14% (max 15.67) from 07:00 onward.
  - `F12` ✔ Precedent HIST-101 (2025-08-14) is the prior bcrypt regression and the basis for the report's guess: symptoms 'login p99 4s, 503 on login, high cpu', root cause 'bcrypt cost factor raised 10->14 in release v6.4.0', resolution 'Rolled back v6.4.0'. Its prerequisite is an auth-service release, which is absent in the current window (finding above).
**Contradicting evidence**
  - `F10` ✔ There was NO auth-service deployment between 2026-04-15T00:00Z and 2026-04-22T09:00Z: the deployment list for that whole week contains exactly one entry, portfolio-service v4.4.0 at 2026-04-22T05:50:00Z (status live, rolled_back_at null). This contradicts the report's premise of an auth-service release.
  - `F7` ✔ No bcrypt/CPU-regression or panic log signature appears in the auth-service WARN/ERROR set; the observed log patterns are queue/pool saturation (worker pool saturated active=64/64), accept queue full 503s, one /internal/metrics slow-request, and an NTP clock-drift message — the evidence points to saturation, not an explicit bcrypt message.
  - `F11` ✔ The only config change in the 2026-04-15..2026-04-22 window is cfg-2801 on auth-service: log.level INFO->WARN at 2026-04-22T03:40:00Z by config-bot ("Reduce log volume"). Nothing else changed in config across all services.
**Assumptions**
  - The tracked deployment inventory is incomplete (off-list or un-tracked code change) — directly contradicted by F10.
  - The bcrypt signature would not necessarily appear as an explicit log line.
**Rejected because:** F10 shows exactly one deployment in the entire 2026-04-15..2026-04-22 window and it is portfolio-service v4.4.0 at 05:50Z, not auth-service; F11 shows the only config change is a log-level INFO->WARN tweak three hours before onset. HIST-101's stated prerequisite (an auth-service release) is absent, and F7 finds no bcrypt or panic signature in the WARN/ERROR set. F12 is a past-incident precedent and weak evidence on its own. H3 is kept rejected; the surviving CPU-cost mechanism (H1) is deliberately re-framed without asserting bcrypt or a deploy.

## Facts (verified observations)
- auth-service p99 latency rose from a ~89-90 ms baseline (05:30-06:42) to 2532 ms in the 06:51 bucket and held ~3.78-3.88 s through 08:57; onset is between 06:42 and 06:51. [F1]
- auth-service error_rate was 0.18-0.25% through 06:42, spiked to 7.88% in the 06:51 bucket, and settled at ~14% from 07:00 onward. [F2]
- The first 503 on /v1/auth/login is logged at 06:55:42 as 'accept queue full, returning 503'; the immediately preceding WARN at 06:54:53 is 'worker pool saturated (active=64/64, queue=39035)'. [F3]
- auth-service CPU jumped from ~38% (05:30-06:42) to 89.96% avg (max 97.33) in the 06:51 bucket and stayed ~96-97% through 08:57. [F4]
- auth-service memory stayed flat at ~51-53% and restarts were 0 for the entire window, so this is not a memory-driven crash or pod restart. [F5]
- There was NO auth-service deployment between 2026-04-15 and 2026-04-22; the only deployment in that week is portfolio-service v4.4.0 at 2026-04-22T05:50:00Z. [F10]
- The only config change in the 2026-04-15..2026-04-22 window is cfg-2801 on auth-service: log.level INFO->WARN at 2026-04-22T03:40:00Z. [F11]
- Precedent HIST-101 (2025-08-14) is the prior bcrypt regression (cost factor 10->14 in release v6.4.0); its prerequisite - an auth-service release - is absent in the current window. [F12]
- auth-service's dependency graph: outgoing edges to user-db (db), redis-cache (cache), kyc-provider (external, has_telemetry=false); api-gateway has an http edge INTO auth-service. [F8]
- redis-cache is a shared datastore consumed by four services: auth-service, portfolio-service, pricing-engine, market-data-service. [F9]
- redis-cache showed no degradation at onset: CPU averaged 21.55% in the 06:51 bucket vs 22.33% at 06:42 and 22.14% at 07:00. [F14]
- redis-cache showed no cache-storm or capacity signature at onset: hit_ratio_pct stayed 95.86% in the 06:51 bucket and evictions_per_min stayed ~3/min, matching pre-onset values. [F15]
- portfolio-service - the only service deployed this week and a co-consumer of redis-cache - shows no latency change at onset: p99 avg 180.28 ms in the 06:51 bucket vs 180.46 ms at 06:42. [F16]
- The other two shared-redis consumers are also flat across onset: pricing-engine p99 avg 59.66 ms and market-data-service p99 avg 40.17 ms in the 06:51 bucket, both unchanged from 06:42. [F17]
- user-db does not expose latency_p99_ms; its available metrics are connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min. [F19]
- The metric backend returns auth-service sub-buckets at bucket_seconds=120 (2-min resolution); all four series share that grid with missing_points=0 and partial=false. [F20]
- auth-service inbound request_rate was flat at ~381-418 req/s for the entire baseline 06:00-06:48; at the 06:50 bucket it was still only 452.43 avg and it inflected upward only in the 06:52 bucket (742.81), reaching 1350.36 at 06:56 and peaking 1749.75 avg at 06:58. [F21]
- auth-service per-service CPU jumped FIRST, in the 06:52 bucket (96.89% avg / 96.96% max) from a flat ~34-40% baseline; the CPU step and the request_rate step (742.81) land in the SAME 2-min bucket, so request rate did not precede CPU. [F22]
- The p99 latency inflection is in the 06:54 bucket (3759.86 ms avg), AFTER both the CPU step and the request_rate step; the 06:50-06:52 buckets were still baseline-fast (93.94 ms avg at 06:50, 91.5 ms avg at 06:52). [F23]
- rate_limited_per_min was flat ~1.3-2.6 req/min through 06:52, then rose sharply at 06:54 (451.86 avg), lagging rather than leading the CPU step. [F24]
- user-db connections_active was flat (22.79-27.75) through the auth-service onset, then jumped to 72.59 at 06:52 - one bucket AFTER auth-service's 06:51 onset. [F27]
- user-db's other four metrics are flat across onset: replication_lag_ms 27.65-32.31, cpu_pct 16.63-19.94%, slow_queries_per_min 0.18-0.51, disk_used_pct 43.41-44.87 - no saturation, lag spike, slow-query burst or disk exhaustion at or before 06:51. [F28]
- api-gateway was flat through the auth-service onset and only degraded at 06:54 (~3 minutes later): latency_p99_ms held 198.84-226.13 ms for 06:30-06:53 then leapt to 4024.51 at 06:54; error_rate stepped from 0.11-0.31% to 9.4% at 06:55. [F29]
- api-gateway showed no resource saturation as trigger: cpu_pct stayed 36.92-45.98% for the entire window including the 4024 ms latency bucket, never approaching saturation. [F30]
- Rate-limiting on auth-service rose from ~2 req/min baseline to 487 in the 06:51 bucket and ~900 req/min (max 979.25) from 07:00 on, alongside an autoscaler message 'desired replicas 14 exceeds maxReplicas 8; cannot scale further'. [F6]

## Assumptions (not verified)
- The CPU is consumed by auth-service's own request path (per-request compute) rather than by an in-process stall that presents as CPU (spinning retry loop, lock contention, GC).
- Some untracked change or request-mix shift raised per-request CPU cost, since no tracked deploy or config event explains it (F10, F11).
- The 2-min bucket resolution does not hide a request_rate rise in the tens of seconds immediately before the CPU step; this cannot be excluded below 2 minutes (F20, F25).
- Aggregate CPU across the 8-replica cap is representative of per-pod cost; no per-pod series exists.
- kyc-provider is called synchronously on /v1/auth/login and could be slow/failing, but it has no telemetry at all (has_telemetry=false) so this was never directly measured (F8).
- user-db could have degraded in a way its flat proxy metrics (replication_lag, cpu_pct, slow_queries, disk) would not show - e.g. a latency/IOPS or connection-pool wall on the auth path - since it exposes no latency_p99_ms (F19, F28).
- The user-db connections_active jump at 06:52 is a downstream consequence (auth-service opening more/retrying connections) rather than an independent db-side saturation; the 06:52 timestamp supports this but a pool-limit check was not retrievable.
- The so-called api-gateway degradation is propagation of auth-service slowness rather than a gateway-local event; this rests on metrics-only ordering (latency 06:54, errors 06:55 vs auth p99 onset 06:51 and first 503 06:55:42) without gateway logs (F29, F30, F31).
- The metric backend's 2-min resolution and the absence of any earlier inflection in the 06:30-06:51 stretch for the measured neighbours is sufficient to treat the dependency-first candidates as not supported; no bucket earlier than 06:30 was inspectable (F26).
- The recorded maxReplicas=8 ceiling is genuinely binding; the HPA message is quoted but no replica/time series was retrieved (F6).
- (demoted from facts: no verified observation backs it) No bcrypt/CPU-regression or panic signature appears in the auth-service WARN/ERROR set; observed patterns are queue/pool saturation, accept-queue-full 503s, one /internal/metrics slow request, and an NTP clock-drift message.
- (demoted from facts: no verified observation backs it) No dependency latency rise precedes or coincides with auth-service's onset: shared redis-cache, its three co-consumers, and portfolio-service all remain at baseline in the 06:42 and 06:51 buckets while auth-service p99 rose.
- (demoted from facts: no verified observation backs it) Interpretation of the H1-vs-H2 test: request_rate did not rise before the ~38%->~97% CPU jump within 2-min resolution; the elevated inbound rate is best read as retry/backlog consequence of the already-slow service, consistent with H1 and not explained by H2's pre-onset surge premise.
- (demoted from facts: no verified observation backs it) The api-gateway deviation sequence (latency 06:54, errors 06:55) lines up with and follows auth-service's own saturation evidence (p99 2532 ms at 06:51; first accept-queue-full 503 at 06:55:42), consistent with the gateway propagating slowness/503s from auth-service.
- (demoted from facts: no verified observation backs it) Relevant precedents for auth-service slowness that do NOT involve an auth-service release are HIST-107 (cold cache storm after redis failover) and HIST-110 (datastore IOPS throttling).

## Timeline
- `2026-04-22T03:40:00Z` Only config change in the week: cfg-2801 on auth-service, log.level INFO->WARN by config-bot. Three hours before onset. [F11]
- `2026-04-22T05:30:00Z` Baseline: auth-service p99 ~89-90 ms (through 06:42), CPU ~38%, request_rate ~381-418 req/s. [F1, F4, F21]
- `2026-04-22T05:50:00Z` portfolio-service v4.4.0 deployed - the only deployment in the entire 2026-04-15..04-22 window; it remains flat across onset. [F10, F16]
- `2026-04-22T06:50:00Z` auth-service request_rate still essentially baseline at 452.43 avg (max 529.5); p99 still baseline-fast at 93.94 ms. [F21, F23]
- `2026-04-22T06:51:00Z` auth-service p99 jumps to 2532 ms; error_rate spikes to 7.88%; CPU bucket averages 89.96% (max 97.33). Dependency candidates (redis-cache, its three co-consumers, portfolio-service) all remain at baseline in this bucket. [F1, F2, F4, F14, F15, F16, F17, F18]
- `2026-04-22T06:52:00Z` auth-service CPU steps to 96.89% avg in the per-service series, in the SAME 2-min bucket that request_rate first upticks to 742.81 - CPU did not follow a prior request-rate rise. user-db connections_active simultaneously jumps from ~26 to 72.59, i.e. after auth onset. [F22, F21, F27, F25]
- `2026-04-22T06:54:00Z` auth-service p99 inflects to 3759.86 ms avg; rate_limited_per_min rises to 451.86; api-gateway p99 simultaneously leaps to 4024.51 ms with its CPU only 39.4%. [F23, F24, F29, F30]
- `2026-04-22T06:54:53Z` auth-service WARN 'worker pool saturated (active=64/64, queue=39035)' - all 64 workers saturated, 39035 queued. [F3]
- `2026-04-22T06:55:00Z` api-gateway error_rate steps from ~0.2% to 9.4%, consistent with propagating auth-service 503s. [F29, F31]
- `2026-04-22T06:55:42Z` First 503 logged on /v1/auth/login: 'accept queue full, returning 503'; becomes the top ERROR pattern (372 occurrences). [F3, F31]
- `2026-04-22T06:58:00Z` auth-service request_rate peaks at 1749.75 avg as retries/backlog accumulate against the saturated service. [F21, F25]
- `2026-04-22T07:00:00Z` Sustained failure: error_rate ~14%, rate_limited_per_min ~900 (max 979.25), with autoscaler message 'desired replicas 14 exceeds maxReplicas 8; cannot scale further'. [F2, F6]
- `2026-04-22T07:06:00Z` Incident INC-008 reported by on-call SRE as login latency ~4s and 503s, suspected bcrypt regression. [F12]
- `2026-04-22T07:14:00Z` CPU remains 96.6-98.8%; p99 held ~3.55-3.93 s through this point and ~3.78-3.88 s through 08:57. [F22, F23, F1]
- `2026-04-22T08:59:00Z` Latest data; auth-service still CPU-saturated (~96-97%), p99 ~3.8 s, error_rate ~14%. Memory flat and restarts 0 throughout. [F4, F1, F2, F5]

## Recommended actions
- **[immediate]** Mitigate on the auth-service side now: shed or throttle inbound load at the edge for /v1/auth/login and/or add capacity to auth-service (raise the autoscaler maxReplicas above the recorded maxReplicas=8 cap, or raise the per-pod worker pool above 64) to drain the 39035-deep worker queue and clear the accept-queue-full 503s. -- The verified failure chain is auth-service-local: CPU 34-40% -> 96.9%+, 64/64 workers saturated with queue 39035, accept-queue-full 503 on /v1/auth/login, p99 90 ms -> 3.8 s. Capacity was the binding constraint on absorbing the load, and the autoscaler itself reported it could not scale further (F4, F3, F6, F22). SPOF caution: raising the replica ceiling only masks a per-request CPU-cost problem, so pair it with the short-term diagnosis below. [F3, F4, F6, F22]
- **[immediate]** Do NOT roll back auth-service expecting a bcrypt fix, and do not attribute this to a bad auth-service deploy: confirm that no auth-service release exists in the window before choosing a rollback target. -- The report's premise is falsified: the only deployment in 2026-04-15..2026-04-22 is portfolio-service v4.4.0 at 05:50Z, and the only config change is a log-level INFO->WARN tweak at 03:40Z (F10, F11). No bcrypt signature appears in the WARN/ERROR set (F7). Rolling back a non-existent auth-service release is not actionable. [F10, F11, F7, F12]
- **[immediate]** Do NOT apply a mitigation based on redis-cache, user-db, or api-gateway as the origin (e.g. cache warm/failover actions from HIST-107, or DB connection-pool changes) as the first-line fix. -- All measured dependency candidates are victims, not origin: redis-cache CPU/hit-ratio/evictions flat across onset; its three co-consumers and portfolio-service flat; user-db flat on cpu/lag/slow-queries/disk through 06:51 with connections_active rising only after auth onset; api-gateway degrades 3-4 min later with CPU only 37-46% (F14, F15, F16, F17, F18, F27, F28, F29, F30, F31). [F14, F15, F16, F17, F18, F27, F28, F29, F30, F31]
- **[short_term]** Discriminate the trigger inside auth-service's request path: pull the auth-service change/flag history and in-flight code state (feature flags, traffic-shadowing, request-mix and JWT-size distribution), and capture a CPU profile or flame graph of a saturated pod to identify which request-handling code path became more expensive per request. -- The origin is inside auth-service's request-handling path on an unchanged ~400 req/s workload (F21, F22, F25), but the trigger is genuinely unknown - no tracked deploy or config change exists in the week (F10, F11). Profiling is the direct test of H1's mechanism and is the only way to name the exact expensive path. [F21, F22, F25, F10, F11]
- **[short_term]** Instrument and verify saturation recovery: add per-pod CPU, accept-queue depth, and worker-pool occupancy as exposed time series for auth-service, then confirm after mitigation that queue depth drains, p99 returns to the ~90 ms baseline, error_rate returns to ~0.2%, and rate_limited_per_min returns to ~2/min. -- Accept-queue/worker-pool occupancy exist only in log messages, not as metrics, and CPU is aggregate-only, so the saturation cannot be plotted against onset (F3, F20, F25). Verified recovery requires the same signals that failed here, and low rate_limited/error values are the observable definition of resolved (F2, F6, F24). [F3, F2, F6, F24]
- **[short_term]** Close the observability gaps on the unmeasured dependencies: add telemetry to kyc-provider (currently has_telemetry=false) and add latency/error series to redis-cache and user-db on the login path so future auth-service incidents can be resolved dependency-first or not. -- kyc-provider and parts of the user-db path could not be measured at all, and redis-cache exposes no latency/error metric - so H4's remaining candidates were never directly falsified by latency, only by proxy and by temporal ordering (F8, F19, F26). [F8, F19, F26]
- **[long_term]** Add a workload-independent guard on per-request compute cost: track and alert on CPU-per-request (or queue depth per unit of sustained inbound rate) for auth-service, and require a change/flag audit step before any auth-service release, to catch an untracked per-request cost increase before it saturates the worker pool. -- H1's mechanism is a rise in CPU cost per request at unchanged load (F21, F22, F25); a CPU-per-request SLO would have fired at the 06:52 CPU step, well before the 06:55:42 accept-queue-full 503. An untracked change is the leading explanation for the trigger (F10, F11). [F21, F22, F25, F10, F11, F3]

## Data gaps
- Neighbouring-service correlation is unanswered: 'user-service' returned not_found (not in known_services) and the session-service query plus remaining calls hit the tool-call budget, so it cannot be confirmed or denied that auth-service is the only affected service.
- No per-pod CPU series exists for auth-service (available_metrics has only aggregate cpu_pct), so the 38%->97% jump cannot be attributed to individual pods versus an aggregate of the 8-replica cap; this also means H1 (all pods' per-request cost rose) cannot be distinguished from a pods-count effect.
- No accept-queue-depth or worker-pool-occupancy time series is exposed for auth-service; those quantities exist only in log messages, so their timing relies on log first-seen times rather than metric buckets and cannot be plotted against onset at 2-min resolution.
- The finest available granularity is bucket_seconds=120; a request-rate rise within tens of seconds before the 06:52 CPU step is not ruled out, so the H1-vs-H2 temporal precedence is only established at 2-minute resolution.
- No metric bucket earlier than 06:30 was retrieved for user-db or api-gateway (budget exhausted), so a pre-06:30 inflection in either component cannot be excluded by direct measurement; the flatness claim is bounded to 06:30-06:51/06:54.
- api-gateway and user-db WARN/ERROR logs, api-gateway request_rate/memory_pct/restarts, and user-db connection-count limits were not retrievable (budget_exhausted), so it cannot be confirmed from logs whether the gateway's 06:54 latency jump is propagated auth-service slowness versus a gateway-local event, nor whether the user-db connection jump at 06:52 is a pool-limit saturation.
- api-gateway exposes no connection/queue-count metric, so gateway accept-queue pressure - the closest analogue to auth-service's 'accept queue full' - is unmeasurable and a queue-level gateway origin cannot be evaluated.
- kyc-provider (external, has_telemetry=false) has no telemetry, so its latency/error contribution to login was never measurable; redis-cache exposes no latency/error metric, so its health is inferred only from CPU/hit-ratio/eviction proxies.
- Deployment/config changes on the shared components themselves (user-db, redis-cache) beyond their appearance in the service-level lists were not checked, and no rollback/restart/flag events for redis-cache were returned, so a redis failover/replica event (the HIST-107 mechanism) around onset cannot be confirmed or excluded.
- The exact minute of the p99 inflection is not pinned down (bounded by the 9-minute baseline 06:42 and elevated 06:51 buckets); no sub-bucket zoom was run and no auth-service tracing or release event was pulled.
- The precise trigger of the CPU cost increase is unidentified: no profiling, no feature-flag audit, and no request-mix/JWT-size distribution exists, so the mechanism is stated only as 'CPU cost per request rose, cause unidentified'.
- Lateral impact paths through user-db (shared-postgres enumeration) and api-gateway dependencies were not mapped (get_service_dependencies run only for auth-service and redis-cache), so a neighbour damaging auth-service through the user DB or the gateway cannot be ruled out.

## What would change this conclusion
- Evidence that a dependency on the login path degraded BEFORE 06:51 (e.g. a kyc-provider slowdown, or a user-db latency/IOPS or connection-pool wall masked by its flat proxy metrics) would move the conclusion to a dependency-first origin (H4) and change the fix to that component.
- A sub-2-minute series showing request_rate rising before the CPU step (rather than stepping in the same 06:52 bucket) would support H2's demand-surge/capacity-ceiling reading and change the primary fix to rate-limiting/raising the replica ceiling rather than the auth-service code path.
- Discovery of an auth-service deployment, feature-flag flip, or in-flight code change in the window would restore a change-triggered origin (and, if a bcrypt cost increase were found, H3) - none currently exists in the tracked inventory.
- A CPU profile or flame graph showing the CPU is consumed by an in-process stall (spinning retry loop, lock contention, GC) rather than per-request compute would invalidate H1's stated mechanism while leaving the saturation chain intact.
- Evidence of a redis-cache failover or replica event around onset (the HIST-107 mechanism) would reopen the shared-component hypothesis, since redis exposes only CPU/hit-ratio/eviction proxies and no failover events were retrieved.
- Log evidence showing api-gateway errors originated locally (rather than propagating auth-service 503s) would make a gateway-side origin viable; currently only metrics-only ordering is available.
- Any auth-service-side change mitigation (throttle/capacity) that fails to restore p99 and drain the worker queue would falsify the auth-service-local origin and point elsewhere.

## Integrity warnings (set by code)
- fact demoted to assumption: No bcrypt/CPU-regression or panic signature appears in the auth-service WARN/ERR
- fact demoted to assumption: No dependency latency rise precedes or coincides with auth-service's onset: shar
- fact demoted to assumption: Interpretation of the H1-vs-H2 test: request_rate did not rise before the ~38%->
- fact demoted to assumption: The api-gateway deviation sequence (latency 06:54, errors 06:55) lines up with a
- fact demoted to assumption: Relevant precedents for auth-service slowness that do NOT involve an auth-servic

## Investigation stats
- latency 204.47s, LLM calls 22, tokens 189118 (in 155023 / out 34095)
- tool calls 36 (3 failed, 0 partial, 0 recovered by retry), findings 31/31 verified
