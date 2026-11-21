# Incident investigation report: INC-008
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Login slowness and 503s on auth-service are caused by capacity saturation of auth-service under a genuine, externally-originated ~3.7x inbound traffic surge (H1, confidence 0.62): request_rate steps ~400 -> ~1,452 req/s in the same 06:52Z->06:54Z bucket as p99 (91ms -> 3.76s), error_rate (0.2% -> 11.7%) and CPU (~38% -> ~97%), with the only error signature being "accept queue full" 503s. The reported bcrypt-regression theory (H2) is rejected: there is no auth-service deployment in the window or the preceding three days, no request-path config change, and zero bcrypt log matches; the historical precedent HIST-101 required exactly such a bad deploy. Dependency-origin hypotheses (H3 shared-redis flood from portfolio-service v4.4.0, H4 downstream degradation) are contradicted by flat redis-cache CPU/memory and flat user-db CPU/slow-query metrics with no pre-06:52Z rise, plus a 64-minute quiet gap between the only deployment and onset. Confidence is capped rather than "inconclusive" because the remaining uncertainty concerns the identity of the traffic source and the unmeasurable kyc-provider path, not the location or mechanism of the failure.

## Probable root cause
**Externally-driven login traffic surge saturates auth-service CPU/accept queue (capacity_saturation at auth-service)** -- capacity_saturation in `auth-service`, triggered by `null` (confidence 0.62, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Externally-driven login traffic surge saturates auth-service CPU/accept queue (capacity_saturation at auth-service) | capacity_saturation | auth-service | null | 0.62 (medium) | active |
| 2 | H4: A downstream dependency of auth-service (redis-cache / user-db / kyc-provider) degraded and auth-service is only where it surfaces | dependency_failure | user-db / redis-cache / kyc-provider (unresolved) | null | 0.12 (low) | active |
| 3 | H3: portfolio-service v4.4.0 'Holdings export' floods the shared redis-cache, degrading auth-service transitively | bad_deployment | portfolio-service | dep-5803 | 0.10 (low) | active |
| 4 | H2: Reporter's suspicion: bcrypt/hashing regression reintroduced in auth-service | bad_deployment | auth-service | null | 0.04 (low) | rejected |

### H1: Externally-driven login traffic surge saturates auth-service CPU/accept queue (capacity_saturation at auth-service)
auth-service inbound request_rate steps from ~400 req/s to ~1,500 req/s at 06:54Z, pinning CPU at ~97% and overflowing the accept queue, which returns 503 'accept queue full' and inflates p99 to ~3.8s. Critically, the surge is externally-visible inbound demand, not an internal retry artifact: api-gateway's own inbound RPS rose in parallel (~1,244 -> ~4,763 req/s), and the gateway:auth RPS ratio stayed roughly constant (3.29 -> 3.14 -> 2.68), so auth-service is not re-requesting beyond the gateway's own traffic. Origin is auth-service capacity saturation under a genuine load spike; not a code regression.

**Causal chain**
1. api-gateway inbound request_rate rises ~3.8x in the same sub-minute window (~1,244 req/s at 06:48Z -> 3,175 at 06:54Z -> 4,763 at 07:00Z), and auth-service's inbound rate tracks it (~385 -> 1,012 -> 1,814 req/s) [F14, F15]
2. The gateway:auth RPS ratio stays roughly constant across onset (3.29 / 3.14 / 2.68), proving auth-service is not receiving ~3.7x the gateway's traffic and the surge is not a hidden retry/duplicate fan-out [F16, F17]
3. auth-service CPU saturates at onset: ~37-38% baseline, partial rise at 06:36Z, then pinned at ~97% avg / ~99% max from 06:54Z onward [F11]
4. auth-service accept queue saturates; the only ERROR signature is queue saturation ('request rejected: accept queue full, returning 503' on POST /v1/auth/login), first seen 06:55:42Z [F9, F19]
5. p99 latency and error_rate step up in the same 06:54Z bucket and remain elevated (~3.8s, ~14%) [F7, F8]
6. Impact propagates upstream to api-gateway, whose p99 steps to ~3.9s in the same bucket — consistent with auth-service being the constraint [F12]
7. auth-service memory is flat across the window, so the saturation is CPU/queue-bound rather than a leak [F25]
8. No auth-service deployment (0 in window and preceding three days) and no request-path config change occurred; the log.level INFO->WARN change does not touch bcrypt or the hashing path [F2, F3]
9. No bcrypt log signature exists (total_matched=0), so the reported bcrypt regression is not corroborated [F13]

**Supporting evidence**
  - `F10` ✔ auth-service request_rate steps up ~3.7x at the same 06:36Z->06:54Z bucket: ~393-442 req/s baseline (06:00-06:36Z) to 1452.66 req/s at 06:54Z, sustained ~1,490-1,510 req/s afterward, with a max of 1813.74. This coincides with the latency/error step.
  - `F14` ✔ api-gateway inbound request_rate rises in the same 06:50-07:00Z window as auth-service, on sub-minute (60s) buckets: ~1,244 req/s at 06:48Z -> 1,653 at 06:51Z -> 3,175 at 06:54Z -> 4,763 req/s at 07:00Z. It did NOT stay near ~400 req/s.
  - `F15` ✔ auth-service inbound request_rate on the same 60s buckets tracks the gateway: ~385 req/s at 06:48Z -> 1,012 at 06:54Z -> 1,814 at 06:59Z, i.e. both series move together rather than diverging.
  - `F16` ✔ The gateway:auth RPS ratio stays roughly constant across the onset: 06:30Z 1278.54/389.19 = 3.29; 06:54Z 3174.6/1012.36 = 3.14; 06:59Z 4859.74/1813.74 = 2.68. auth-service is not receiving ~3.7x the gateway's traffic; the gateway carries ~3x the auth-service load before, during and after onset.
  - `F17` ✔ Because the gateway's own inbound rate rose ~3.8x in parallel with auth-service's, the auth-service surge is externally-visible inbound demand, not a hidden internal retry/duplicate fan-out that would leave gateway inbound near baseline while auth-service spikes alone.
  - `F11` ✔ auth-service CPU saturates at onset: ~37-38% (06:00-06:18Z), partial rise at 06:36Z (avg 44.66, max 96.96), then pinned at ~97% avg / ~98-99% max from 06:54Z through 08:42Z.
  - `F9` ✔ The only ERROR signature in auth-service logs is queue saturation, not application/bcrypt errors: 372 ERROR entries, all matching "request rejected: accept queue full, returning <n> (POST /v<n>/auth/login)", first_seen 2026-04-22T06:55:42Z (returning 503), last 08:58:57Z.
  - `F19` ✔ auth-service rate_limited_per_min steps from ~1.2-2.9 req/min baseline (06:30-06:52Z) to 451.86 avg / 902.06 max at 06:54Z, then ~868-901 req/min, confirming the arrival surge is being refused at the same 06:54Z boundary.
  - `F7` ✔ auth-service p99 latency (latency_p99_ms) rises as a sharp STEP at the 06:52Z -> 06:54Z boundary: 91.5ms avg at 06:52Z, 93.94ms at 06:50Z, then 3759.86ms at 06:54Z, and remains ~3.6-3.9s through 08:54Z. Zoomed 120s buckets confirm the transition occurs within this single 2-minute bucket with no gradual ramp.
  - `F8` ✔ auth-service error_rate steps up in the same 06:48Z->06:54Z bucket: baseline ~0.19-0.27% through 06:48Z, then 11.72% at 06:54Z, sustained at ~13.7-14.4% through 08:54Z (max 15.67%).
  - `F12` ✔ api-gateway shows the SAME sharp step at the 06:36Z->06:54Z bucket: p99 ~208-211ms baseline (06:00-06:36Z) -> 3908.96ms at 06:54Z, sustained ~3.87-3.94s through 08:42Z (max 4250.7ms). Impact propagates upstream of auth-service.
  - `F25` ✔ auth-service memory_pct is FLAT across 06:30-07:10Z and shows no step at 06:52Z: 50.6 avg at 06:51Z, 52.23 at 06:54Z, 51.05 at 06:57Z, 50.22 at 07:00Z; summary mean 51.59, min 47.56, max 55.22. Memory does not rise with the latency/error step — the auth-service fault is CPU/queue-bound, not memory.
  - `F2` ✔ No auth-service deployment occurred in the window, nor in the preceding three days (2026-04-18T00:00Z–2026-04-22T08:59Z): auth-service-scoped deployment count is 0.
  - `F3` ✔ The only config change across all services in the window is on auth-service: key log.level, INFO→WARN, at 2026-04-22T03:40:00Z, described as 'Reduce log volume' — it does not touch bcrypt or the hashing/request path.
  - `F13` ✔ No bcrypt-related log messages exist in auth-service in the 06:50-08:59Z window: a WARN+ search for the term "bcrypt" returned total_matched=0 (searched data range 03:00-08:59Z). The report's bcrypt-regression hypothesis is not corroborated by any bcrypt log signature.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The load surge originates outside the observed services (external clients or an upstream caller not instrumented in this window); the rise in gateway inbound RPS is taken as the boundary of what we can measure, so the ultimate traffic source remains unidentified
  - Load precedes saturation: latency, error, CPU and request_rate all step within the same 06:36Z->06:54Z bucket, which is consistent with load-driven saturation but cannot be ordered below 2-minute resolution
  - auth-service's provisioned capacity cannot absorb ~1,500 req/s at its normal per-request CPU cost

### H4: A downstream dependency of auth-service (redis-cache / user-db / kyc-provider) degraded and auth-service is only where it surfaces
A downstream component auth-service depends on degraded at/around onset, so login calls block on it and auth-service queues saturate. Tested against all available dependency telemetry: redis-cache CPU is flat across 06:30-07:10Z (no rise at or before 06:52Z), redis-cache memory is flat/normal, and user-db CPU (18.6, mean 18.0) and slow_queries_per_min (0.32, mean 0.31) are flat with no pre-06:52Z degradation. kyc-provider remains unmeasurable (no telemetry).

**Causal chain**
1. A shared/external dependency of auth-service degrades around 06:5xZ *(assumed)*
2. Login requests block on that dependency, inflating per-request latency *(assumed)*
3. Blocked requests occupy workers until the accept queue overflows (503) and CPU rises to saturation [F9, F11]
4. Latency/errors step up at auth-service and propagate to api-gateway [F7, F8, F12]

**Supporting evidence**
  - `F4` ✔ auth-service dependency graph: it calls user-db (kind db), redis-cache (kind cache) and kyc-provider (external); api-gateway calls auth-service (http). redis-cache is flagged shared=true, while user-db, auth-service and api-gateway are shared=false; kyc-provider has has_telemetry=false.
**Contradicting evidence**
  - `F21` ✔ redis-cache cpu_pct is flat across 06:30-07:10Z: 21.95 avg at 06:30Z, 22.12 at 06:48Z, 21.21 at 06:51Z (just before auth-service's step), 21.86 at 06:54Z and 23.03 at 07:09Z; summary mean 22.17, max 25.59. No rise at or before 06:52Z.
  - `F22` ✔ redis-cache memory_pct is flat/normal across the window: 70.35 avg at 06:51Z, 71.88 at 06:54Z, 69.72 at 07:09Z; summary mean 69.98, max 76.3. No step at 06:52Z (the single 73.35 bump at 06:45Z is transient noise, within the 64.49-76.3 range).
  - `F23` ✔ user-db has no latency/error metrics; available_metrics are connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min. user-db cpu_pct is flat (18.62 avg at 06:51Z, 18.44 at 06:54Z, mean 18.0, range 16.63-19.94) and slow_queries_per_min is flat (0.32 at 06:51Z, 0.38 at 06:54Z, mean 0.31, range 0.18-0.51) — no pre-06:52Z degradation.
  - `F10` ✔ auth-service request_rate steps up ~3.7x at the same 06:36Z->06:54Z bucket: ~393-442 req/s baseline (06:00-06:36Z) to 1452.66 req/s at 06:54Z, sustained ~1,490-1,510 req/s afterward, with a max of 1813.74. This coincides with the latency/error step.
  - `F14` ✔ api-gateway inbound request_rate rises in the same 06:50-07:00Z window as auth-service, on sub-minute (60s) buckets: ~1,244 req/s at 06:48Z -> 1,653 at 06:51Z -> 3,175 at 06:54Z -> 4,763 req/s at 07:00Z. It did NOT stay near ~400 req/s.
  - `F15` ✔ auth-service inbound request_rate on the same 60s buckets tracks the gateway: ~385 req/s at 06:48Z -> 1,012 at 06:54Z -> 1,814 at 06:59Z, i.e. both series move together rather than diverging.
  - `F16` ✔ The gateway:auth RPS ratio stays roughly constant across the onset: 06:30Z 1278.54/389.19 = 3.29; 06:54Z 3174.6/1012.36 = 3.14; 06:59Z 4859.74/1813.74 = 2.68. auth-service is not receiving ~3.7x the gateway's traffic; the gateway carries ~3x the auth-service load before, during and after onset.
  - `F17` ✔ Because the gateway's own inbound rate rose ~3.8x in parallel with auth-service's, the auth-service surge is externally-visible inbound demand, not a hidden internal retry/duplicate fan-out that would leave gateway inbound near baseline while auth-service spikes alone.
  - `F24` ✔ kyc-provider has NO telemetry: 'kyc-provider' is an external dependency with no logs/metrics; it can only be observed through its caller auth-service. Its latency_p99, error_rate and CPU/memory cannot be reported for 06:30-07:10Z.
**Assumptions**
  - kyc-provider has no telemetry at all (F24), so a kyc-provider-origin fault cannot be excluded by measurement and is the only surviving unfalsified branch of this hypothesis
  - A dependency latency regression could coincidentally coincide with a parallel 3.8x rise in both gateway and auth-service inbound RPS, which no plausible dependency fault mechanism would produce

### H3: portfolio-service v4.4.0 'Holdings export' floods the shared redis-cache, degrading auth-service transitively
portfolio-service v4.4.0 added a CSV export endpoint at 05:50Z that hammers redis-cache (shared by auth-service, portfolio-service, pricing-engine, market-data-service), slowing auth's cache lookups and producing the 3.8s p99 and 503s. Tested against telemetry: redis-cache CPU and memory are flat across 06:30-07:10Z with no step at or before 06:52Z, and there was a 64-minute quiet gap between the deploy and onset with no intervening config change.

**Causal chain**
1. portfolio-service v4.4.0 (CSV 'Holdings export' endpoint) deploys at 05:50Z, live, not rolled back [F1, F27]
2. The export endpoint generates heavy load against redis-cache, which is shared by all four services [F4, F5, F26]
3. redis-cache contention slows auth-service's cache-dependent login path *(assumed)*
4. Slowed auth path backs up, CPU climbs and the accept queue overflows, yielding 3.8s p99 and 503 'accept queue full' [F7, F9, F11]

**Supporting evidence**
  - `F1` ✔ Across ALL services in 2026-04-21T00:00Z–2026-04-22T08:59Z there was exactly one deployment: portfolio-service v4.4.0 'Holdings export' (CSV export endpoint) at 2026-04-22T05:50:00Z, status live, not rolled back.
  - `F27` ✔ portfolio-service v4.4.0 ('Holdings export', CSV export endpoint, status live, rolled_back_at=null) was deployed at 2026-04-22T05:50:00Z and is the only portfolio-service deployment in 2026-04-21T00:00Z–2026-04-22T08:59Z.
  - `F4` ✔ auth-service dependency graph: it calls user-db (kind db), redis-cache (kind cache) and kyc-provider (external); api-gateway calls auth-service (http). redis-cache is flagged shared=true, while user-db, auth-service and api-gateway are shared=false; kyc-provider has has_telemetry=false.
  - `F5` ✔ redis-cache is shared by four callers — auth-service, portfolio-service, pricing-engine and market-data-service — so a fault originating in portfolio-service (which deployed at 05:50Z) could plausibly propagate to auth-service through that shared cache.
  - `F26` ✔ redis-cache is the single shared component connecting all four services: edges auth-service->redis-cache, portfolio-service->redis-cache, pricing-engine->redis-cache, market-data-service->redis-cache all have kind 'cache', and redis-cache has shared=true. portfolio-service additionally calls market-data-service (http), which itself calls redis-cache — so portfolio-service and market-data-service also co-call each other.
**Contradicting evidence**
  - `F28` ✔ The quiet gap between the portfolio-service deploy (05:50Z) and the auth-service onset (06:54Z = first elevated bucket) is 64 minutes; there was no config change in 05:00–07:10Z to explain a trigger in between (config_changes count = 0 for all services).
  - `F21` ✔ redis-cache cpu_pct is flat across 06:30-07:10Z: 21.95 avg at 06:30Z, 22.12 at 06:48Z, 21.21 at 06:51Z (just before auth-service's step), 21.86 at 06:54Z and 23.03 at 07:09Z; summary mean 22.17, max 25.59. No rise at or before 06:52Z.
  - `F22` ✔ redis-cache memory_pct is flat/normal across the window: 70.35 avg at 06:51Z, 71.88 at 06:54Z, 69.72 at 07:09Z; summary mean 69.98, max 76.3. No step at 06:52Z (the single 73.35 bump at 06:45Z is transient noise, within the 64.49-76.3 range).
  - `F10` ✔ auth-service request_rate steps up ~3.7x at the same 06:36Z->06:54Z bucket: ~393-442 req/s baseline (06:00-06:36Z) to 1452.66 req/s at 06:54Z, sustained ~1,490-1,510 req/s afterward, with a max of 1813.74. This coincides with the latency/error step.
  - `F12` ✔ api-gateway shows the SAME sharp step at the 06:36Z->06:54Z bucket: p99 ~208-211ms baseline (06:00-06:36Z) -> 3908.96ms at 06:54Z, sustained ~3.87-3.94s through 08:42Z (max 4250.7ms). Impact propagates upstream of auth-service.
  - `F14` ✔ api-gateway inbound request_rate rises in the same 06:50-07:00Z window as auth-service, on sub-minute (60s) buckets: ~1,244 req/s at 06:48Z -> 1,653 at 06:51Z -> 3,175 at 06:54Z -> 4,763 req/s at 07:00Z. It did NOT stay near ~400 req/s.
  - `F15` ✔ auth-service inbound request_rate on the same 60s buckets tracks the gateway: ~385 req/s at 06:48Z -> 1,012 at 06:54Z -> 1,814 at 06:59Z, i.e. both series move together rather than diverging.
**Assumptions**
  - The CSV export actually touches redis-cache heavily (no portfolio-service or redis-per-caller metrics were collected)
  - auth-service's login path materially depends on redis-cache for the slow part of each request
  - redis-cache contention would manifest as increased auth-service request volume rather than elevated per-request cache latency, and would leave redis-cache's own CPU at baseline 22%

### H2: Reporter's suspicion: bcrypt/hashing regression reintroduced in auth-service
A new auth-service release reintroduced expensive hashing (as in HIST-101), causing per-login CPU burn, ~4s login latency and 503s. Rejected: no auth-service deployment exists in the window or the preceding three days, no bcrypt log messages exist, the sole error signature is accept-queue-full, and the step coincides with a parallel ~3.8x traffic rise at both gateway and auth-service.

**Causal chain**
1. An auth-service deployment introduced a heavier bcrypt cost factor *(assumed)*
2. Hashing cost raises per-request CPU, driving auth-service CPU to saturation [F11]
3. Saturated CPU produces 4s login p99 and 503s [F7, F8]

**Supporting evidence**
  - `F7` ✔ auth-service p99 latency (latency_p99_ms) rises as a sharp STEP at the 06:52Z -> 06:54Z boundary: 91.5ms avg at 06:52Z, 93.94ms at 06:50Z, then 3759.86ms at 06:54Z, and remains ~3.6-3.9s through 08:54Z. Zoomed 120s buckets confirm the transition occurs within this single 2-minute bucket with no gradual ramp.
  - `F8` ✔ auth-service error_rate steps up in the same 06:48Z->06:54Z bucket: baseline ~0.19-0.27% through 06:48Z, then 11.72% at 06:54Z, sustained at ~13.7-14.4% through 08:54Z (max 15.67%).
**Contradicting evidence**
  - `F2` ✔ No auth-service deployment occurred in the window, nor in the preceding three days (2026-04-18T00:00Z–2026-04-22T08:59Z): auth-service-scoped deployment count is 0.
  - `F3` ✔ The only config change across all services in the window is on auth-service: key log.level, INFO→WARN, at 2026-04-22T03:40:00Z, described as 'Reduce log volume' — it does not touch bcrypt or the hashing/request path.
  - `F13` ✔ No bcrypt-related log messages exist in auth-service in the 06:50-08:59Z window: a WARN+ search for the term "bcrypt" returned total_matched=0 (searched data range 03:00-08:59Z). The report's bcrypt-regression hypothesis is not corroborated by any bcrypt log signature.
  - `F9` ✔ The only ERROR signature in auth-service logs is queue saturation, not application/bcrypt errors: 372 ERROR entries, all matching "request rejected: accept queue full, returning <n> (POST /v<n>/auth/login)", first_seen 2026-04-22T06:55:42Z (returning 503), last 08:58:57Z.
  - `F10` ✔ auth-service request_rate steps up ~3.7x at the same 06:36Z->06:54Z bucket: ~393-442 req/s baseline (06:00-06:36Z) to 1452.66 req/s at 06:54Z, sustained ~1,490-1,510 req/s afterward, with a max of 1813.74. This coincides with the latency/error step.
  - `F6` ✔ Historical precedent HIST-101 (2025-08-14, 'Login latency after auth-service release'): symptoms login p99 4s, 503 on login, high cpu; root cause 'bcrypt cost factor raised 10->14 in release v6.4.0'; resolution 'Rolled back v6.4.0'. Its signature required a bad auth-service deployment — which is absent in the current window.
  - `F14` ✔ api-gateway inbound request_rate rises in the same 06:50-07:00Z window as auth-service, on sub-minute (60s) buckets: ~1,244 req/s at 06:48Z -> 1,653 at 06:51Z -> 3,175 at 06:54Z -> 4,763 req/s at 07:00Z. It did NOT stay near ~400 req/s.
  - `F15` ✔ auth-service inbound request_rate on the same 60s buckets tracks the gateway: ~385 req/s at 06:48Z -> 1,012 at 06:54Z -> 1,814 at 06:59Z, i.e. both series move together rather than diverging.
  - `F16` ✔ The gateway:auth RPS ratio stays roughly constant across the onset: 06:30Z 1278.54/389.19 = 3.29; 06:54Z 3174.6/1012.36 = 3.14; 06:59Z 4859.74/1813.74 = 2.68. auth-service is not receiving ~3.7x the gateway's traffic; the gateway carries ~3x the auth-service load before, during and after onset.
**Assumptions**
  - HIST-101's bcrypt signature is specific enough to recur identically
  - The absence of a deployment record is a telemetry gap rather than reality
**Rejected because:** HIST-101's signature requires a bad auth-service deployment (F6); auth-service deployment count is 0 in the window and preceding three days (F2), with no request-path config change (F3). Zero bcrypt log matches in the onset window (F13); the only ERROR signature is accept-queue-full queue saturation, not hashing errors (F9). Degradation coincides with a parallel ~3.8x rise in BOTH gateway and auth-service inbound RPS with a constant ratio (F10, F14, F15, F16), which a fixed per-request hashing cost cannot produce.

## Facts (verified observations)
- auth-service p99 latency rises as a sharp step in the single 06:52Z->06:54Z bucket: 91.5ms at 06:52Z and 93.94ms at 06:50Z, then 3759.86ms at 06:54Z, sustained ~3.6-3.9s through 08:54Z with no gradual ramp. [F7]
- auth-service error_rate steps up in the same 06:48Z->06:54Z bucket: from ~0.19-0.27% baseline to 11.72% at 06:54Z, sustained ~13.7-14.4% through 08:54Z. [F8]
- auth-service request_rate steps up ~3.7x in the same bucket: ~393-442 req/s baseline to 1452.66 req/s at 06:54Z, sustained ~1,490-1,510 req/s (max 1813.74), coinciding with the latency and error step. [F10, F15]
- auth-service CPU saturates at onset: ~37-38% before, then pinned at ~97% average / ~98-99% max from 06:54Z through 08:42Z. [F11]
- The only ERROR signature in auth-service logs is queue saturation, not application or hashing errors: 372 entries matching "request rejected: accept queue full, returning 503 (POST /v1/auth/login)", first_seen 06:55:42Z, last 08:58:57Z. [F9]
- auth-service rate_limited_per_min steps from ~1.2-2.9 req/min baseline to 451.86 avg / 902.06 max at 06:54Z, then ~868-901 req/min, confirming arrivals are being refused at the same boundary. [F19]
- api-gateway shows the same sharp step at the 06:36Z->06:54Z bucket: p99 ~208-211ms baseline to 3908.96ms at 06:54Z, sustained ~3.87-3.94s, so impact propagates upstream of auth-service. [F12]
- api-gateway's own inbound request_rate rises in parallel on 60s buckets (~1,244 req/s at 06:48Z -> 1,653 at 06:51Z -> 3,175 at 06:54Z -> 4,763 req/s at 07:00Z), rather than staying near baseline. [F14]
- The gateway:auth RPS ratio stays roughly constant across onset (3.29 at 06:30Z, 3.14 at 06:54Z, 2.68 at 06:59Z), so auth-service is not receiving ~3.7x the gateway's traffic — the surge is not an internal retry/duplicate fan-out artifact. [F16]
- auth-service memory_pct is flat across 06:30-07:10Z with no step at 06:52Z (mean 51.59, range 47.56-55.22), so the fault is CPU/queue-bound rather than memory-bound. [F25]
- No auth-service deployment occurred in the window nor in the preceding three days (2026-04-18T00:00Z-2026-04-22T08:59Z): auth-service-scoped deployment count is 0. [F2]
- The only config change across all services in the window is auth-service log.level INFO->WARN at 03:40Z ('Reduce log volume'), which does not touch bcrypt or the hashing/request path. [F3]
- No bcrypt-related log messages exist in auth-service in the 06:50-08:59Z window: a WARN+ search for "bcrypt" returned total_matched=0. [F13]
- Historical precedent HIST-101 (2025-08-14) had the same presenting symptoms (login p99 4s, 503s, high CPU) but its signature required a bad auth-service deployment (bcrypt cost factor raised in v6.4.0) — which is absent in the current window. [F6]
- The only deployment across all services in 2026-04-21T00:00Z-2026-04-22T08:59Z was portfolio-service v4.4.0 'Holdings export' at 05:50Z, status live, not rolled back. [F1, F27]
- redis-cache is shared by auth-service, portfolio-service, pricing-engine and market-data-service; kyc-provider is external with has_telemetry=false. [F4, F26]
- redis-cache cpu_pct is flat across 06:30-07:10Z with no rise at or before 06:52Z (mean 22.17, max 25.59), and memory_pct is flat/normal (mean 69.98, max 76.3) with no step at 06:52Z. [F21, F22]
- user-db cpu_pct is flat (mean 18.0, range 16.63-19.94) and slow_queries_per_min is flat (mean 0.31, range 0.18-0.51) with no pre-06:52Z degradation. [F23]
- The quiet gap between the portfolio-service deploy at 05:50Z and the auth-service onset at 06:54Z is 64 minutes, with no config change in 05:00-07:10Z (config_changes count = 0 for all services). [F28]
- kyc-provider has no telemetry at all (no logs/metrics) and can only be observed through its caller auth-service. [F24]

## Assumptions (not verified)
- The load surge originates outside the observed services (external clients or an upstream caller not instrumented in this window); the rise in api-gateway inbound RPS is taken as the boundary of what is measurable, so the ultimate traffic source remains unidentified.
- Load precedes saturation: latency, error, CPU and request_rate all step within the same 06:36Z->06:54Z bucket, which is consistent with load-driven saturation but cannot be ordered below 2-minute resolution.
- auth-service's provisioned capacity cannot absorb ~1,500 req/s at its normal per-request CPU cost.
- The absence of any auth-service deployment record reflects reality rather than a telemetry gap; if deployment history were incomplete, a code-change origin could not be fully excluded.
- The 372 truncated ERROR log entries are representative: only the accept-queue-full pattern is present, and no lower-severity context contradicts it.
- A downstream dependency fault (e.g. kyc-provider) cannot be excluded by measurement because that component has no telemetry; it is excluded only indirectly, since auth-service's own CPU/queue saturates and the gateway and auth inbound rates rise in parallel — a pattern no plausible dependency fault would produce.

## Timeline
- `2026-04-22T03:40:00Z` Only in-window config change: auth-service log.level INFO->WARN ('Reduce log volume'); does not touch bcrypt or the request/hashing path. [F3]
- `2026-04-22T05:50:00Z` Only deployment across all services in the window: portfolio-service v4.4.0 'Holdings export' (CSV export endpoint), live, not rolled back. [F1, F27]
- `2026-04-22T06:00:00Z` Baseline: auth-service ~393-442 req/s, p99 ~91-94ms, error_rate ~0.19-0.27%, CPU ~37-38%; api-gateway p99 ~208-211ms, inbound RPS ~1,244. [F7, F8, F10, F11, F12, F14]
- `2026-04-22T06:36:00Z` First sign of change on auth-service CPU (avg 44.66, max 96.96) within the 06:36Z->06:54Z bucket. [F11]
- `2026-04-22T06:48:00Z` Last near-baseline 60s sample: auth-service ~385 req/s, api-gateway ~1,244 req/s; gateway:auth ratio 3.29. [F14, F15, F16]
- `2026-04-22T06:51:00Z` redis-cache cpu_pct 21.21 — at baseline immediately before the auth-service step; no dependency-side precursor. [F21]
- `2026-04-22T06:54:00Z` Sharp step in the same bucket: auth-service p99 93.94ms -> 3759.86ms, error_rate 0.2% -> 11.72%, request_rate -> 1452.66 req/s, CPU pinned ~97%, rate_limited_per_min -> 451.86; api-gateway p99 -> 3908.96ms; redis-cache and user-db metrics remain flat. [F7, F8, F10, F11, F12, F19, F21, F22, F23]
- `2026-04-22T06:55:42Z` First 'accept queue full' ERROR in auth-service logs, returning 503 on POST /v1/auth/login. [F9]
- `2026-04-22T06:59:00Z` auth-service 1813.74 req/s with api-gateway 4859.74 req/s; gateway:auth ratio 2.68 — ratios remain in the same range as before onset. [F15, F16]
- `2026-04-22T07:00:00Z` api-gateway inbound RPS reaches ~4,763 req/s, confirming a sustained parallel inbound surge. [F14]
- `2026-04-22T07:06:00Z` Incident INC-008 reported by on-call SRE (auth-service p99 3.8s, 503s on /v1/auth/login), framed as a suspected bcrypt regression as in HIST-101. [F6]
- `2026-04-22T08:58:57Z` Last 'accept queue full' 503 in the observed window; auth-service p99, error_rate and CPU remain at saturated levels (last data 08:59Z). [F9, F7, F8, F11]

## Recommended actions
- **[immediate]** Immediately shed/limit load at the edge rather than restarting auth-service: apply or tighten rate limiting / traffic throttling on api-gateway for POST /v1/auth/login (the component already identified as carrying the surge) so auth-service inbound is brought back toward ~400-450 req/s, the level at which CPU was ~38% and p99 ~91ms. -- auth-service is CPU-pinned at ~97% with an overflowing accept queue producing 503s (F9, F11); the driver is inbound demand of ~1,452-1,814 req/s versus a ~400 req/s baseline (F10, F15). Reducing arrivals is the only lever that addresses the verified mechanism, and auth-service is refusing ~868-901 req/min already (F19). [F9, F10, F11, F15, F19, F14]
- **[immediate]** Do NOT roll back or hot-patch auth-service code as the primary mitigation (no auth-service deployment is present to roll back); if any release action is taken, treat it as capacity-driven, not code-driven. -- There is no auth-service deployment in the window or preceding three days (F2), no request-path config change (F3), and zero bcrypt log matches (F13); the HIST-101 playbook (rolling back a bad release) does not apply. [F2, F3, F13, F6]
- **[short_term]** Scale out / add capacity to auth-service (additional replicas or CPU headroom) and/or raise its accept-queue depth, so ~1.5k req/s can be absorbed at normal per-request cost, then verify p99 returns to ~90-200ms and error_rate to <0.5%. -- The verified failure mode is capacity saturation of auth-service (CPU ~97%, accept queue full), not a defect (F11, F9, F7, F8). Raising capacity directly addresses the mechanism. [F9, F11, F7, F8]
- **[short_term]** Identify and characterize the traffic source: enable per-client / per-route request attribution at api-gateway and auth-service, and confirm whether the parallel rise in gateway inbound RPS (~1,244 -> ~4,763 req/s) is genuine external client demand, a marketing/retry storm, or an upstream caller outside the instrumented boundary. -- The gateway:auth ratio stayed roughly constant (3.29/3.14/2.68), so the surge is externally-visible inbound demand and not an internal retry artifact (F16, F17, F14), but the ultimate origin is unidentified; gateway INFO+ logs for the onset window contained no 'auth' matches. Addressing or rate-limiting the source prevents recurrence of the same saturation. [F14, F16, F17, F18]
- **[short_term]** Keep the shared-cache and dependency paths closed out rather than pursued as active mitigations: no action on portfolio-service v4.4.0 or redis-cache/kyc-provider is warranted on current evidence. -- redis-cache CPU/memory are flat with no pre-06:52Z rise (F21, F22), user-db CPU and slow_queries are flat (F23), and there is a 64-minute quiet gap between the only deployment and onset with no intervening change (F28); the H3/H4 paths are contradicted by measurement. [F21, F22, F23, F28, F1, F27]
- **[long_term]** Add missing observability to close the residual blind spots: per-client/session request attribution for auth-service (no session/token metric today), latency/error metrics for redis-cache, user-db and kyc-provider, and inbound metrics for portfolio-service, pricing-engine and market-data-service. -- auth-service exposes no unique-login/session metric (F18); redis-cache and user-db expose no latency/error metrics; kyc-provider has no telemetry at all (F24). These gaps forced the investigation to infer dependency health indirectly from CPU/memory proxies. [F18, F24, F20]
- **[long_term]** Establish capacity/load-shedding guardrails for auth-service: define a provisioned-RPS and CPU saturation threshold with automatic edge throttling and capacity alerts, and pre-agree the playbook branch 'load-driven saturation' vs 'bad release' so responders do not default to the bcrypt rollback playbook. -- This incident recurred in presentation to HIST-101 (F6) but had an entirely different mechanism; the acute need is preventing auth-service CPU-pinning under a ~3.7x inbound surge (F11, F10), and disambiguating the two signatures early. [F6, F10, F11, F2]

## Data gaps
- The ultimate source of the traffic surge is unidentified: api-gateway INFO+ logs for 06:45-07:05Z containing 'auth' returned total_matched=0 and no per-client breakdown exists, so the gateway inbound rise (~1,244 -> ~4,763 req/s) marks the boundary of measurement rather than the origin.
- auth-service exposes no session/token/unique-login metric (available: cpu_pct, error_rate, latency_p99_ms, memory_pct, rate_limited_per_min, request_rate, restarts), so a request-to-unique-session ratio could not be computed to positively separate distinct clients from duplicates.
- Onset could not be pinned below the 2-minute (and, for some series, 60s) bucket, so whether load strictly precedes saturation is not demonstrated — only that all signals (latency, error, CPU, request_rate) step within the same 06:36Z->06:54Z bucket.
- kyc-provider has no telemetry (has_telemetry=false) and can only be observed through auth-service, so a kyc-provider-origin dependency fault cannot be excluded by measurement — this is the only surviving unfalsified branch of H4.
- redis-cache and user-db expose no latency_p99/error_rate metrics; only CPU/memory and counter-style proxies were available. redis hit_ratio_pct, evictions_per_min, user-db replication_lag_ms and connections_active were never retrieved (queries budget-exhausted), so a subtle no-CPU/no-memory cache or DB degradation cannot be fully ruled out.
- No per-service request_rate or error signatures were pulled for portfolio-service, pricing-engine or market-data-service over 06:30-07:00Z, so the shared-cache/co-caller path (H3) could not be tested directly against those services' own traffic; it is contradicted only indirectly, via flat redis-cache and user-db metrics.
- Blast radius beyond auth-service and api-gateway is unverified: no metrics for session-service or other peer services were retrieved (queries budget-exhausted).
- Deployment and config-change history before 2026-04-21T00:00Z was not retrieved, and the 372 ERROR log entries were truncated (30 of 372 returned), so individual stack traces and lower-severity context around onset were not reviewed.

## What would change this conclusion
- Evidence that auth-service's ~3.7x request-rate rise is an internal retry/duplicate fan-out rather than genuine inbound demand — e.g. a per-client breakdown showing a small number of clients re-requesting, or gateway inbound RPS staying near baseline while auth-service spikes alone (would contradict F14/F16/F17 and re-open H4).
- Discovery of an auth-service deployment or a request-path/feature-flag change activated near 06:54Z (e.g. within the un-scanned pre-2026-04-21 history or a staged rollout) would re-open the bcrypt/code-regression hypothesis (H2) despite F2/F3.
- Any bcrypt- or hashing-related error/latency log signature in the truncated or lower-severity auth-service logs around onset (F13 currently shows total_matched=0).
- A pre-06:52Z degradation in a dependency not measurable here — particularly kyc-provider (no telemetry at all) or a redis-cache/user-db signal in the unqueried counters (hit_ratio_pct, evictions_per_min, replication_lag_ms, connections_active) — would support H4 and shift the origin off auth-service capacity.
- Evidence that portfolio-service v4.4.0's CSV export was driving heavy shared redis-cache traffic beginning before 06:52Z (e.g. elevated portfolio-service request_rate or redis evictions/hit-ratio degradation) would revive H3, notwithstanding the currently flat redis-cache CPU/memory and the 64-minute quiet gap.
- Demonstration that ~1,500 req/s is within auth-service's designed capacity at its normal per-request CPU cost (i.e. the capacity assumption is wrong) would shift the diagnosis from capacity saturation toward a per-request cost regression or a hidden resource bottleneck.

## Investigation stats
- latency 164.59s, LLM calls 25, tokens 174923 (in 144483 / out 30440)
- tool calls 39 (4 failed, 0 partial, 0 recovered by retry), findings 29/29 verified
