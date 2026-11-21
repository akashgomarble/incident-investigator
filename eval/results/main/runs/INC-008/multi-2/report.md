# Incident investigation report: INC-008
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
INC-008 is a demand-driven capacity saturation of auth-service (H2, confidence 0.60, medium): a ~4.5x step in auth-service inbound request_rate at 06:51-06:52 (375 -> 715 -> 1814 req/s) coincides to the minute with CPU pegging at ~97%, saturation of the 64-worker pool (active=64/64, queue ~98k), accept-queue-full 503s on /v1/auth/login, and an autoscaler that could not scale past maxReplicas 8 — a single-phase CPU-bound saturation that persisted unchanged for the full 2-hour tail. The surge is not an auth-service artifact: api-gateway's own inbound traffic stepped ~3.8x in the same 06:51 minute with no lead time, and gateway error/latency onset followed a bucket later (06:54), confirming the demand rise originates upstream of the gateway and undermining the retry-storm alternative (H5). The reporter's bcrypt-regression theory (H1) is rejected — there was no auth-service deployment or config logic change in the window and no bcrypt or timeout log signature — and the shared redis-cache theory (H3) is rejected on flat cache metrics at both 05:50 and 06:52. Residual uncertainty is limited to the ultimate origin of the demand step (organic vs. client-side amplification), which could not be measured because no gateway retry/5xx counter was retrieved.

## Probable root cause
**Demand surge (organic) saturates auth-service capacity** -- capacity_saturation in `auth-service` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: Demand surge (organic) saturates auth-service capacity | capacity_saturation | auth-service | - | 0.60 (medium) | active |
| 2 | H5: Upstream retry storm amplifies login traffic at auth-service | capacity_saturation | api-gateway | - | 0.15 (low) | active |
| 3 | H4: Internal resource exhaustion / GC-heap (or pool) collapse inside auth-service | resource_exhaustion | auth-service | - | 0.04 (low) | active |
| 4 | H3: Shared redis-cache fault propagated from portfolio-service v4.4.0 degrades auth-service | dependency_failure | redis-cache | dep-5803 (portfolio-service v4.4.0, 05:50Z) | 0.03 (low) | rejected |
| 5 | H1: Reporter's theory: auth-service bcrypt hashing regression reintroduced | bad_deployment | auth-service | - | 0.02 (low) | rejected |

### H2: Demand surge (organic) saturates auth-service capacity
Inbound login/request demand stepped up ~4.5x at 06:51-06:52 (auth-service request_rate 375->529->715->1814 req/s, F26), coincident with the CPU step to ~97% at 06:52 (F9/F27). The 64-worker pool saturated (active=64/64, queue ~98k), the accept queue filled and returned 503s, and the autoscaler cap (maxReplicas 8) prevented relief. This is demand-driven saturation of a service with no headroom, not a faulty change. The new gateway evidence (F43/F44) shows the SAME step at the SAME minute at api-gateway, so the surge is not an auth-only artifact; it propagates from the gateway edge.

**Causal chain**
1. Inbound request traffic at api-gateway steps ~3.8x at 06:51 (1348.68 -> 1652.95 req/s) and continues to 2164.64 at 06:52, rising to ~5000 by 07:10 [F43, F44, F45]
2. auth-service inbound request_rate steps at the same minute (375.35 -> 529.5 at 06:51, 715.14 at 06:52, climbing to ~1814 by 06:59) [F26, F27, F44]
3. auth-service CPU steps to 96.82% at 06:52, same minute as the demand step (37-40% through 06:49) [F9, F14, F27]
4. The 64-worker pool saturates (active=64/64, queue depth ~98k) and the accept queue fills [F11, F15]
5. Accept-queue-full requests are rejected with 503 on POST /v1/auth/login; rate-limiting steps 1.46 -> 607.72/min [F15, F30]
6. Autoscaler wants 14 replicas but is capped at maxReplicas 8, so capacity cannot be added while CPU pins ~97% (never relieved across the 2h tail) [F12, F17, F51]
7. Result: p99 holds ~3.68-3.89s, error rate ~13.8-14.1%, and the same in-process rejection signature persists unchanged to 08:58Z [F7, F8, F49, F53]

**Supporting evidence**
  - `F26` ✔ auth-service request_rate was flat at ~363-416 req/s through 06:50 (06:47=363.28, 06:48=385.12, 06:49=387.16, 06:50=375.35) then STEPPED UP starting 06:51 (529.5) and 06:52 (715.14), climbing monotonically to 1813.74 at 06:59 and holding ~1442-1534 req/s through 07:10. This is a ~4.5x demand increase, not a flat load.
  - `F27` ✔ The request_rate step (06:51: 529.5, 06:52: 715.14) begins one minute at/just before the CPU step at 06:52 (96.82%) established in F9, so the demand rise and the CPU jump are essentially simultaneous; the earlier 06:50 bucket (avg 452.43/max 529.5 at 2-min resolution) reflects the same onset because the 06:50 2-min bucket spans 06:50-06:51.
  - `F9` ✔ Minute-resolution CPU shows the true onset at 06:52:00Z: values were 33-40% through 06:51 (06:50=33.46, 06:51=35.39) then jumped to 96.82% at 06:52 and stayed ~96-98% (97.26 at 06:59, 98.35 at 07:00).
  - `F14` ✔ auth-service latency and CPU step at the SAME minute: p99 is ~88-91ms through 06:41 then jumps in the 06:48 bucket (avg 628.19, max 3849.2) and holds ~3680-3893ms from 06:55 onward, while CPU is 37-40% through 06:49 (06:49 avg 36.41) and jumps to 96.86% at 06:52. Latency is therefore tightly coupled to CPU, not to any downstream call.
  - `F11` ✔ auth-service logs show two dominant signatures at onset: ERROR "request rejected: accept queue full, returning 503 (POST /v1/auth/login)" (372 occurrences, first_seen 06:55:42) and WARN "worker pool saturated (active=64/64, queue=...)" (249 occurrences, first_seen 06:54:53); the earliest warning in the window is only "slow request 3759ms on /internal/metrics" at 06:19:11, before the CPU step.
  - `F15` ✔ The 503s are saturation-rejection errors, not auth/bcrypt errors: the dominant ERROR pattern is 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' (372 occurrences, first_seen 06:55:42), paired with WARN 'worker pool saturated (active=64/64, queue=...)' (249 occurrences, first_seen 06:54:53, e.g. queue=98763). The full matched set of 656 log lines contains only these patterns plus an autoscaler cap warning and one clock-drift warning.
  - `F30` ✔ rate_limited_per_min shows a sharp step consistent with saturation/rejection: flat ~1.46-2.25 req/min through 06:51 (06:48=1.93, 06:51=1.46) then jumping to 607.72 avg (max 919.45) in the 06:54 bucket and holding ~852-926/min through 07:09.
  - `F12` ✔ auth-service was already at its scaling cap during the incident: repeated WARN "autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further" (34 occurrences, first_seen 06:57:15), so the service could not add capacity while CPU was pinned at ~97%.
  - `F17` ✔ The service could not add capacity while CPU was pegged: autoscaler WARN 'desired replicas 14 exceeds maxReplicas 8; cannot scale further' (first_seen 06:57:15) recurs through 07:01:30, so saturated CPU could not be relieved by horizontal scaling.
  - `F51` ✔ The autoscaler cap warning continues throughout the tail (33 occurrences, first_seen 07:01:30Z and recurring e.g. 07:06:04Z and 07:06:27Z) — the service remains unable to scale beyond maxReplicas 8 for the whole 2-hour tail, i.e. the capacity shortfall identified at onset (F12/F17) was never relieved during the incident.
  - `F7` ✔ auth-service p99 latency was flat ~88-91ms from 04:00 to 06:40, then jumped to avg 2288.53ms (max 4012.81) in the 06:50 bucket and stayed ~3770-3867ms for the rest of the window.
  - `F8` ✔ auth-service error rate (% of requests HTTP 5xx) was flat ~0.18-0.25% until 06:40, then rose to avg 7.11% (max 14.76) in 06:50 bucket and held ~13.8-14.1% through 08:50.
  - `F13` ✔ The observed auth-service profile (CPU pegged ~97% from 06:52, worker pool saturated active=64/64 with rising queue depth, accept-queue 503s, scaling capped) is a saturation/CPU-bound signature within auth-service rather than a downstream-latency signature; the log evidence does not show timeout/dependency error messages at onset.
  - `F31` ✔ restarts stayed 0 throughout 06:30-07:10 (all points avg/max 0.0), so no pod restart or crash-restart occurred at onset; combined with F1 (no auth deployment) this is not a redeploy/rollout artifact.
  - `F43` ✔ api-gateway inbound request_rate shows the SAME step at the SAME minute as auth-service: flat at 1178.92-1348.68 req/s through 06:50, then 06:51=1652.95, 06:52=2164.64, rising monotonically to 4859.74 at 06:59 and holding ~4544.91-4990.58 through 07:10 (summary first 1260.78, last 4990.58). The gateway is not flat.
  - `F44` ✔ Side-by-side minute-resolution comparison: both series are flat through 06:50 (gateway 1348.68 / auth-service 375.35) and both make their first step in the 06:51 bucket (gateway 1652.95 = +22.6%; auth-service 529.5 = +41.1%), with the step continuing in 06:52 (gateway 2164.64, auth-service 715.14). Step timing is coincident to the minute; the gateway has no lead time.
  - `F45` ✔ Magnitude comparison: gateway inbound grows by ~3.8x (1260.78 -> peak 5197.35 at 07:04), while auth-service inbound grows ~4.8x from its pre-step level (375.35 -> peak 1813.74 at 06:59). The gateway step is at least as large as, and in absolute terms far larger than, the auth-service step — the gateway is carrying the demand rise, not merely reflecting a fraction of it.
  - `F46` ✔ Gateway error_rate and latency do NOT precede the auth-service onset — they begin degrading in the 06:54 bucket, i.e. ~2-3 minutes AFTER both traffic series step at 06:51-06:52. error_rate is 0.13-0.30% through the 06:52 bucket (06:52 avg 0.21, max 0.24) then 06:54 avg 4.78/max 9.4, holding ~8.66-9.32% to 07:10. Gateway p99 is 202.8-219.82ms through the 06:52 bucket (06:52 avg 209.64) then 06:54 avg 3979.99/max 4024.51, holding ~3867-3960ms.
  - `F47` ✔ api-gateway's own latency signature is the auth-service latency propagated outward, not an independent gateway-side stall: gateway p99 settles at ~3924-3979ms (max 4200.87) from 06:54 onward, statistically the same value as auth-service p99 ~3770-3867ms (F7) and the 4s symptom, and the gateway does not saturate at a level that would explain a several-fold amplification on its own (inbound only ~3.8x while errors rise only to ~9%, not to ~100%).
  - `F49` ✔ The tail is a pure continuation of the onset signatures at essentially the same level, not a new or escalating phase: the ERROR accept-queue-full pattern runs at the same volume in the 2-hour tail (358 occurrences over 07:00-08:59) as the onset period did (372 occurrences identified in the earlier truncated window), and it persists to the last matched line at 2026-04-22T08:58:57Z. The queue-depth values in the tail (74318 at 07:00:10, 93865 at 07:00:50, 91429 at 07:02:03) are the same order of magnitude as the onset (queue=98763 seen at 06:54).
  - `F53` ✔ The post-onset tail does NOT support a multi-phase or dependency-driven mechanism: no downstream-timeout, connection-reset, gateway/proxy, or retry-storm signature appears in 07:00-08:59Z, and the only error family present is the same in-process saturation rejection (accept queue full / worker pool saturated) seen at onset. The tail therefore confirms a single-phase capacity-saturation event rather than revealing a second error family after 07:02.
**Contradicting evidence**
  - `F42` ✔ Gap-driving fact: the api-gateway available_metrics list was never retrieved, so no gateway-side error/retry metric value (5xx upstream count, retries, timeouts) for 06:45-07:15Z exists in this round's evidence; the H5 signal is absent from the dataset rather than measured flat.
**Assumptions**
  - That request_rate counts inbound offered load rather than a metric artifact or the service's own re-emission
  - That the surge is genuine demand rather than upstream amplification (F42: no gateway retry/5xx counter was retrieved, so H2 vs H5 still rests on rate timing alone)
  - That the auth-service autoscaler cap is independent of the gateway and would not be the binding constraint in a retry scenario

### H5: Upstream retry storm amplifies login traffic at auth-service
The 4.5x auth-service request_rate step is not (wholly) organic demand but a retry/amplification effect: a failing or frustrated upstream caller — client or api-gateway on upstream timeouts — multiplies effective login requests and pins auth-service CPU. Weakened by F43/F44/F45: the gateway's own inbound traffic also steps ~3.8x at the same minute, so the demand rise is upstream of the gateway, not generated between gateway and auth-service.

**Causal chain**
1. An upstream caller (client or gateway) begins retrying, multiplying effective requests *(assumed)*
2. Effective auth-service request_rate steps ~4.5x at 06:51-06:52 (375->715->1814 req/s) [F26, F27]
3. auth-service CPU pins ~97% at the same minute and the worker pool saturates at 64/64 [F9, F11]
4. Accept queue fills; requests rejected 503; rate limiting steps to ~852-926/min [F15, F30]
5. No gateway config change (count 0) altered routing/timeouts, so any amplification would be behavioural, not config-driven [F21, F5, F38]
6. No gateway deployment or config change and no retry-storm precedent exists in the incident store, so the amplifier has no recorded origin [F38, F40, F41]
7. Contradiction: api-gateway inbound request_rate itself steps ~3.8x at the same 06:51 minute (not flat), so the demand rise is already present at the gateway edge and cannot be attributed to retries generated between the gateway and auth-service [F43, F44, F45]
8. Gateway error_rate and p99 only degrade 2-3 min after the step, and the stall appears to be auth-service latency propagated outward rather than an independent gateway-side amplifier [F46, F47]

**Supporting evidence**
  - `F26` ✔ auth-service request_rate was flat at ~363-416 req/s through 06:50 (06:47=363.28, 06:48=385.12, 06:49=387.16, 06:50=375.35) then STEPPED UP starting 06:51 (529.5) and 06:52 (715.14), climbing monotonically to 1813.74 at 06:59 and holding ~1442-1534 req/s through 07:10. This is a ~4.5x demand increase, not a flat load.
  - `F27` ✔ The request_rate step (06:51: 529.5, 06:52: 715.14) begins one minute at/just before the CPU step at 06:52 (96.82%) established in F9, so the demand rise and the CPU jump are essentially simultaneous; the earlier 06:50 bucket (avg 452.43/max 529.5 at 2-min resolution) reflects the same onset because the 06:50 2-min bucket spans 06:50-06:51.
  - `F9` ✔ Minute-resolution CPU shows the true onset at 06:52:00Z: values were 33-40% through 06:51 (06:50=33.46, 06:51=35.39) then jumped to 96.82% at 06:52 and stayed ~96-98% (97.26 at 06:59, 98.35 at 07:00).
  - `F11` ✔ auth-service logs show two dominant signatures at onset: ERROR "request rejected: accept queue full, returning 503 (POST /v1/auth/login)" (372 occurrences, first_seen 06:55:42) and WARN "worker pool saturated (active=64/64, queue=...)" (249 occurrences, first_seen 06:54:53); the earliest warning in the window is only "slow request 3759ms on /internal/metrics" at 06:19:11, before the CPU step.
  - `F15` ✔ The 503s are saturation-rejection errors, not auth/bcrypt errors: the dominant ERROR pattern is 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' (372 occurrences, first_seen 06:55:42), paired with WARN 'worker pool saturated (active=64/64, queue=...)' (249 occurrences, first_seen 06:54:53, e.g. queue=98763). The full matched set of 656 log lines contains only these patterns plus an autoscaler cap warning and one clock-drift warning.
  - `F30` ✔ rate_limited_per_min shows a sharp step consistent with saturation/rejection: flat ~1.46-2.25 req/min through 06:51 (06:48=1.93, 06:51=1.46) then jumping to 607.72 avg (max 919.45) in the 06:54 bucket and holding ~852-926/min through 07:09.
  - `F21` ✔ Config-change query for api-gateway over the same window returned count 0, scope api-gateway — no gateway config/flag change that could have altered login routing, timeouts or 503 behaviour.
  - `F40` ✔ No alert or incident record matching a client/load-generator/retry-storm amplifier exists in the historical incident store: the 6 hits for 'retry storm client retry loop gateway 503 surge' are HIST-101 (bcrypt bad_deployment), HIST-102 (ledger-db failover, 'added retry'), HIST-103 (external exchange outage), HIST-104 (Kafka rebalance), HIST-106 (mTLS cert expiry) and HIST-107 (cold cache storm) — none is a client retry-loop or load-generator precedent for the current window, and none is dated 2026-04-22.
  - `F41` ✔ Inference: with no gateway deployment, no gateway config change and no retry-storm precedent record, the only remaining source of the F26 ~4.5x request_rate step (375.35 req/s at 06:50 to 715.14 at 06:52) is client-side/demand behaviour outside any recorded change event — which is consistent with organic demand surge (H2) but does not distinguish H2 from a client-side retry loop (H5), because a retry loop would also produce no deployment or config record.
**Contradicting evidence**
  - `F43` ✔ api-gateway inbound request_rate shows the SAME step at the SAME minute as auth-service: flat at 1178.92-1348.68 req/s through 06:50, then 06:51=1652.95, 06:52=2164.64, rising monotonically to 4859.74 at 06:59 and holding ~4544.91-4990.58 through 07:10 (summary first 1260.78, last 4990.58). The gateway is not flat.
  - `F44` ✔ Side-by-side minute-resolution comparison: both series are flat through 06:50 (gateway 1348.68 / auth-service 375.35) and both make their first step in the 06:51 bucket (gateway 1652.95 = +22.6%; auth-service 529.5 = +41.1%), with the step continuing in 06:52 (gateway 2164.64, auth-service 715.14). Step timing is coincident to the minute; the gateway has no lead time.
  - `F45` ✔ Magnitude comparison: gateway inbound grows by ~3.8x (1260.78 -> peak 5197.35 at 07:04), while auth-service inbound grows ~4.8x from its pre-step level (375.35 -> peak 1813.74 at 06:59). The gateway step is at least as large as, and in absolute terms far larger than, the auth-service step — the gateway is carrying the demand rise, not merely reflecting a fraction of it.
  - `F46` ✔ Gateway error_rate and latency do NOT precede the auth-service onset — they begin degrading in the 06:54 bucket, i.e. ~2-3 minutes AFTER both traffic series step at 06:51-06:52. error_rate is 0.13-0.30% through the 06:52 bucket (06:52 avg 0.21, max 0.24) then 06:54 avg 4.78/max 9.4, holding ~8.66-9.32% to 07:10. Gateway p99 is 202.8-219.82ms through the 06:52 bucket (06:52 avg 209.64) then 06:54 avg 3979.99/max 4024.51, holding ~3867-3960ms.
  - `F47` ✔ api-gateway's own latency signature is the auth-service latency propagated outward, not an independent gateway-side stall: gateway p99 settles at ~3924-3979ms (max 4200.87) from 06:54 onward, statistically the same value as auth-service p99 ~3770-3867ms (F7) and the 4s symptom, and the gateway does not saturate at a level that would explain a several-fold amplification on its own (inbound only ~3.8x while errors rise only to ~9%, not to ~100%).
  - `F38` ✔ No deployment and no config change exists for api-gateway anywhere near the H5 window: scoped deployment query for api-gateway 2026-04-22T06:00-07:30Z returned count 0, and scoped config-change query returned count 0; an unscoped query over the same window returned count 0 for all services. So no gateway change could have introduced a retry/amplification behaviour at onset.
**Assumptions**
  - That a retry mechanism exists above the gateway capable of ~4x amplification
  - That the request_rate step reflects multiplied retries rather than genuine broad demand
  - That /v1/auth/login is the amplified endpoint (metrics have no per-endpoint rate, F18)

### H4: Internal resource exhaustion / GC-heap (or pool) collapse inside auth-service
A non-code internal resource exhaustion in auth-service starts at ~06:52, cascading into CPU pegging, worker-pool saturation and accept-queue 503s, independently of demand. Retained as a residual only: memory_pct is flat (F28), no GC metric exists (F29), no restarts occurred (F31), and there is no supporting pre-onset signal.

**Causal chain**
1. An internal resource (heap/GC or connection/thread pool) begins to exhaust at ~06:52 *(assumed)*
2. CPU pins ~97% and latency steps at the same minute (06:52) [F9, F14]
3. Worker pool saturates active=64/64 with deep queue [F11]
4. Accept queue rejects with 503; scaling capped at 8 prevents relief [F15, F12]
5. Contradiction: memory_pct shows no step or trend at 06:52 (flat 47.6-55.2%), no GC metric exists, and restarts stayed 0 — no supporting signal for an internal-exhaustion mechanism [F28, F29, F31]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F28` ✔ auth-service memory_pct shows NO step change at 06:52: values wander in a flat 47.56-55.22% band across the whole 06:30-07:10 window (06:50=50.77 avg/52.95 max, 06:52=51.61 avg/53.39 max, 06:58=52.04, 07:02=49.34) with no upward trend or discontinuity.
  - `F29` ✔ auth-service exposes NO GC metric: querying 'gc_pause_ms' returned not_found, and the full available_metrics list is [cpu_pct, error_rate, latency_p99_ms, memory_pct, rate_limited_per_min, request_rate, restarts]. Therefore the requested GC-trend comparison at 06:52 cannot be made from this telemetry.
  - `F31` ✔ restarts stayed 0 throughout 06:30-07:10 (all points avg/max 0.0), so no pod restart or crash-restart occurred at onset; combined with F1 (no auth deployment) this is not a redeploy/rollout artifact.
  - `F26` ✔ auth-service request_rate was flat at ~363-416 req/s through 06:50 (06:47=363.28, 06:48=385.12, 06:49=387.16, 06:50=375.35) then STEPPED UP starting 06:51 (529.5) and 06:52 (715.14), climbing monotonically to 1813.74 at 06:59 and holding ~1442-1534 req/s through 07:10. This is a ~4.5x demand increase, not a flat load.
  - `F9` ✔ Minute-resolution CPU shows the true onset at 06:52:00Z: values were 33-40% through 06:51 (06:50=33.46, 06:51=35.39) then jumped to 96.82% at 06:52 and stayed ~96-98% (97.26 at 06:59, 98.35 at 07:00).
**Assumptions**
  - That a resource-exhaustion mechanism would peg CPU rather than be its consequence
  - That the demand step (F26) is coincidental to, rather than the cause of, the resource pressure

### H3: Shared redis-cache fault propagated from portfolio-service v4.4.0 degrades auth-service
The portfolio-service v4.4.0 deploy at 05:50Z perturbed the shared redis-cache; cache degradation on auth-service session lookups caused retries/queueing, surfacing as login slowness and 503s. Rejected: redis-cache proxy metrics are flat at both 05:50 and 06:52, config count 0, no timeout/dependency-error signature, and the ~1h quiet gap breaks any propagation path.

**Causal chain**
1. portfolio-service v4.4.0 deploys at 05:50Z touching shared redis-cache interactions [F1, F4, F25]
2. auth-service login path depends on redis-cache for session lookup *(assumed)*
3. Shared redis-cache degrades (miss storm / latency) propagating to auth-service *(assumed)*
4. auth-service worker pool saturates; accept queue rejects 503; p99 ~3.8s [F11, F15, F7]

**Supporting evidence**
  - `F1` ✔ No auth-service deployment occurred in the incident window; the only deployment across ALL services was portfolio-service v4.4.0 at 2026-04-22T05:50:00Z ('Holdings export', status live, not rolled back).
  - `F4` ✔ redis-cache is flagged shared:true and is used as a cache by auth-service, portfolio-service, pricing-engine and market-data-service — so a change or fault in a non-auth service (e.g. portfolio-service, which deployed at 05:50Z) can degrade auth-service through this shared component.
  - `F25` ✔ redis-cache is a redis-type datastore, owner platform, shared=true, with caller→callee cache edges from auth-service, portfolio-service, pricing-engine and market-data-service; portfolio-service had exactly one deployment in 03:00-09:00Z, dep-5803 v4.4.0 at 05:50:00Z, status live, rolled_back_at null — the only change preceding the 06:52 onset on any service sharing that cache.
**Contradicting evidence**
  - `F32` ✔ No step change in any redis-cache proxy metric at ~06:52: hit_ratio_pct stays 95.28-96.52% across all 240s buckets from 06:00 to 07:20 (06:48 avg 96.11, 06:52 avg 96.12, 06:56 avg 95.28); evictions_per_min stays 2.24-3.62 keys/min (06:48 2.83, 06:52 3.13); cpu_pct stays 21.51-22.84% (06:48 21.86, 06:52 21.62); connected_clients stays 305.38-320.94 (06:48 311.32, 06:52 310.61). redis-cache shows no degradation at the onset minute.
  - `F33` ✔ redis-cache was also unchanged at the 05:50Z portfolio-service v4.4.0 deployment (the H3 propagation path): hit_ratio_pct 95.05-96.52% over 06:00-06:52, cpu_pct 21.51-22.78%, evictions_per_min 2.24-3.31, connected_clients ~304-321 — all within the same band as the rest of the window, with no rise following 05:50Z. None of the four metrics expose an anomaly that could have propagated into auth-service.
  - `F22` ✔ Config-change query for redis-cache over the same window returned count 0, scope redis-cache — the shared cache itself was not reconfigured, so a config-driven H3 variant is excluded (a fault/load-driven H3 is not excluded by this).
  - `F16` ✔ No timeout, dependency-error, or bcrypt-specific log signature is present: a WARN-level search for 'timeout' in the incident window returns total_matched 0, and a search for 'bcrypt' returns 0. Combined with F11/F13 this argues against a downstream-timeout mechanism at onset.
  - `F13` ✔ The observed auth-service profile (CPU pegged ~97% from 06:52, worker pool saturated active=64/64 with rising queue depth, accept-queue 503s, scaling capped) is a saturation/CPU-bound signature within auth-service rather than a downstream-latency signature; the log evidence does not show timeout/dependency error messages at onset.
  - `F7` ✔ auth-service p99 latency was flat ~88-91ms from 04:00 to 06:40, then jumped to avg 2288.53ms (max 4012.81) in the 06:50 bucket and stayed ~3770-3867ms for the rest of the window.
  - `F11` ✔ auth-service logs show two dominant signatures at onset: ERROR "request rejected: accept queue full, returning 503 (POST /v1/auth/login)" (372 occurrences, first_seen 06:55:42) and WARN "worker pool saturated (active=64/64, queue=...)" (249 occurrences, first_seen 06:54:53); the earliest warning in the window is only "slow request 3759ms on /internal/metrics" at 06:19:11, before the CPU step.
**Assumptions**
  - That a cache fault produces CPU saturation rather than timeout errors (no timeouts observed, F16)
  - That redis-cache could degrade for auth-service callers without any proxy metric stepping (all four proxy metrics flat, F32/F33)
  - That a 05:50 trigger would manifest only at 06:52 after a ~1h quiet gap
**Rejected because:** redis-cache proxy metrics (hit_ratio_pct, evictions_per_min, cpu_pct, connected_clients) are flat at both 06:52 and 05:50 — no degradation at onset (F32, F33); redis-cache config-change count 0 (F22); no timeout/dependency-error log signature at onset (F16); and the ~1h quiet gap between the 05:50 deploy and the 06:52 step contradicts a propagation path. Survives only as an unverified sub-4-min blip (240s buckets, F19/F32).

### H1: Reporter's theory: auth-service bcrypt hashing regression reintroduced
A newly deployed auth-service release reintroduced the 2025 bcrypt cost-factor regression, burning CPU in password verification and driving p99 ~3.8s and 503s, mirroring HIST-101. Rejected: no auth-service release exists in the window, no bcrypt log signature, and the mechanism is demand-driven saturation-rejection.

**Causal chain**
1. A new auth-service release raised the bcrypt cost factor, making password hashing CPU-bound *(assumed)*
2. Higher per-request CPU drives node CPU to ~97% at 06:52 [F9]
3. CPU saturation exhausts the 64-worker pool [F11]
4. Accept queue fills; requests rejected 503 [F15]
5. p99 rises to ~3.8-4s, error rate ~14% [F7, F8]

**Supporting evidence**
  - `F6` ✔ Historical precedent HIST-101 (2025-08-14) 'Login latency after auth-service release' matches the current symptom set exactly — login p99 4s, 503 on login, high cpu — with root cause 'bcrypt cost factor raised 10->14 in release v6.4.0' and resolution 'Rolled back v6.4.0'; it was a bad_deployment. This is a lead, not evidence about the current incident.
  - `F7` ✔ auth-service p99 latency was flat ~88-91ms from 04:00 to 06:40, then jumped to avg 2288.53ms (max 4012.81) in the 06:50 bucket and stayed ~3770-3867ms for the rest of the window.
  - `F8` ✔ auth-service error rate (% of requests HTTP 5xx) was flat ~0.18-0.25% until 06:40, then rose to avg 7.11% (max 14.76) in 06:50 bucket and held ~13.8-14.1% through 08:50.
  - `F9` ✔ Minute-resolution CPU shows the true onset at 06:52:00Z: values were 33-40% through 06:51 (06:50=33.46, 06:51=35.39) then jumped to 96.82% at 06:52 and stayed ~96-98% (97.26 at 06:59, 98.35 at 07:00).
  - `F11` ✔ auth-service logs show two dominant signatures at onset: ERROR "request rejected: accept queue full, returning 503 (POST /v1/auth/login)" (372 occurrences, first_seen 06:55:42) and WARN "worker pool saturated (active=64/64, queue=...)" (249 occurrences, first_seen 06:54:53); the earliest warning in the window is only "slow request 3759ms on /internal/metrics" at 06:19:11, before the CPU step.
  - `F13` ✔ The observed auth-service profile (CPU pegged ~97% from 06:52, worker pool saturated active=64/64 with rising queue depth, accept-queue 503s, scaling capped) is a saturation/CPU-bound signature within auth-service rather than a downstream-latency signature; the log evidence does not show timeout/dependency error messages at onset.
**Contradicting evidence**
  - `F1` ✔ No auth-service deployment occurred in the incident window; the only deployment across ALL services was portfolio-service v4.4.0 at 2026-04-22T05:50:00Z ('Holdings export', status live, not rolled back).
  - `F24` ✔ Deployment query scoped to auth-service returned count 0 for 2026-04-21T06:00Z-2026-04-22T09:00Z — no auth-service release and no canary/flagged rollout record, which argues against H1 (bcrypt regression reintroduced by a release).
  - `F16` ✔ No timeout, dependency-error, or bcrypt-specific log signature is present: a WARN-level search for 'timeout' in the incident window returns total_matched 0, and a search for 'bcrypt' returns 0. Combined with F11/F13 this argues against a downstream-timeout mechanism at onset.
  - `F15` ✔ The 503s are saturation-rejection errors, not auth/bcrypt errors: the dominant ERROR pattern is 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' (372 occurrences, first_seen 06:55:42), paired with WARN 'worker pool saturated (active=64/64, queue=...)' (249 occurrences, first_seen 06:54:53, e.g. queue=98763). The full matched set of 656 log lines contains only these patterns plus an autoscaler cap warning and one clock-drift warning.
  - `F14` ✔ auth-service latency and CPU step at the SAME minute: p99 is ~88-91ms through 06:41 then jumps in the 06:48 bucket (avg 628.19, max 3849.2) and holds ~3680-3893ms from 06:55 onward, while CPU is 37-40% through 06:49 (06:49 avg 36.41) and jumps to 96.86% at 06:52. Latency is therefore tightly coupled to CPU, not to any downstream call.
  - `F26` ✔ auth-service request_rate was flat at ~363-416 req/s through 06:50 (06:47=363.28, 06:48=385.12, 06:49=387.16, 06:50=375.35) then STEPPED UP starting 06:51 (529.5) and 06:52 (715.14), climbing monotonically to 1813.74 at 06:59 and holding ~1442-1534 req/s through 07:10. This is a ~4.5x demand increase, not a flat load.
  - `F43` ✔ api-gateway inbound request_rate shows the SAME step at the SAME minute as auth-service: flat at 1178.92-1348.68 req/s through 06:50, then 06:51=1652.95, 06:52=2164.64, rising monotonically to 4859.74 at 06:59 and holding ~4544.91-4990.58 through 07:10 (summary first 1260.78, last 4990.58). The gateway is not flat.
  - `F44` ✔ Side-by-side minute-resolution comparison: both series are flat through 06:50 (gateway 1348.68 / auth-service 375.35) and both make their first step in the 06:51 bucket (gateway 1652.95 = +22.6%; auth-service 529.5 = +41.1%), with the step continuing in 06:52 (gateway 2164.64, auth-service 715.14). Step timing is coincident to the minute; the gateway has no lead time.
**Assumptions**
  - That a bcrypt regression release occurred but was not captured by the deployment query (F24 shows count 0)
  - That the CPU burn is in password hashing and not present on non-login paths (no per-endpoint metrics, F18)
**Rejected because:** No auth-service deployment exists in the window (F1, F24 count 0), and a regression requires a release. No bcrypt log signature (F16). The symptom is saturation-rejection (accept queue full, 503) coincident with a ~4.5x demand step (F26/F27) that is echoed independently at the gateway (F43/F44). HIST-101 is a misleading precedent here.

## Facts (verified observations)
- auth-service p99 latency was flat ~88-91ms from 04:00 to 06:40, then jumped to avg 2288.53ms (max 4012.81) in the 06:50 bucket and held ~3770-3867ms for the rest of the window. [F7]
- Error rate (% HTTP 5xx) on auth-service was flat ~0.18-0.25% until 06:40, then rose to avg 7.11% (max 14.76) in the 06:50 bucket and held ~13.8-14.1% through 08:50. [F8]
- Minute-resolution CPU establishes the true onset at 06:52:00Z: 33-40% through 06:51 (06:50=33.46, 06:51=35.39), then 96.82% at 06:52 and ~96-98% thereafter. [F9]
- Latency and CPU step at the same minute (latency bucket 06:48, CPU 96.86% at 06:52); latency is tightly coupled to CPU, not to any downstream call. [F14]
- auth-service request_rate was flat ~363-416 req/s through 06:50 (06:50=375.35) and stepped up starting 06:51 (529.5), 06:52 (715.14), climbing monotonically to 1813.74 at 06:59 — a ~4.5x demand increase, not flat load. [F26]
- The request_rate step begins at/just before the CPU step, so the demand rise and the CPU jump are essentially simultaneous. [F27]
- api-gateway inbound request_rate shows the same step at the same minute: flat 1178.92-1348.68 req/s through 06:50, then 06:51=1652.95, 06:52=2164.64, rising to 4859.74 at 06:59; the gateway is not flat. [F43]
- Side-by-side minute-resolution comparison shows both series flat through 06:50 and both making their first step in the 06:51 bucket (gateway +22.6%, auth-service +41.1%) — coincident to the minute, no gateway lead time. [F44]
- Gateway inbound grows ~3.8x (1260.78 -> peak 5197.35) while auth-service inbound grows ~4.8x (375.35 -> 1813.74); the gateway carries the demand rise rather than reflecting a fraction of it. [F45]
- Gateway error_rate and latency degrade in the 06:54 bucket, ~2-3 minutes AFTER both traffic series step at 06:51-06:52 (error_rate 0.21% through 06:52, then 4.78 avg at 06:54; p99 209.64ms through 06:52, then 3979.99 avg at 06:54). [F46]
- auth-service logs at onset show two dominant signatures: ERROR 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' (372 occurrences, first_seen 06:55:42) and WARN 'worker pool saturated (active=64/64, queue=...)' (249 occurrences, first_seen 06:54:53). [F11]
- The 503s are saturation-rejection errors, not auth/bcrypt errors; the full matched set of 656 log lines contains only these patterns plus an autoscaler cap warning and one clock-drift warning. [F15]
- No timeout, dependency-error, or bcrypt-specific log signature exists in the incident window: WARN search for 'timeout' returned 0 matches and 'bcrypt' returned 0 matches. [F16]
- auth-service was already at its scaling cap: repeated WARN 'autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further' (34 occurrences, first_seen 06:57:15), so capacity could not be added while CPU was pinned ~97%. [F12, F17]
- The autoscaler cap warning continues throughout the 2-hour tail (33 occurrences, first_seen 07:01:30), so the capacity shortfall was never relieved during the incident. [F51]
- rate_limited_per_min was flat ~1.46-2.25 req/min through 06:51 then jumped to 607.72 avg (max 919.45) in the 06:54 bucket, holding ~852-926/min through 07:09 — consistent with saturation/rejection. [F30]
- restarts stayed 0 throughout 06:30-07:10, so no pod restart/crash-restart occurred at onset; combined with no auth deployment this is not a rollout artifact. [F31]
- No auth-service deployment occurred in the incident window; the only deployment across all services was portfolio-service v4.4.0 at 05:50:00Z ('Holdings export', status live). [F1]
- The only config change in the window on auth-service was a logging change log.level INFO->WARN at 03:40:00Z by config-bot, which does not alter auth logic. [F2, F23]
- Deployment query scoped to auth-service returned count 0 for 2026-04-21T06:00Z-2026-04-22T09:00Z — no auth-service release and no canary/flagged rollout record. [F24]
- No redis-cache proxy metric stepped at ~06:52: hit_ratio_pct 95.28-96.52%, evictions_per_min 2.24-3.62, cpu_pct 21.51-22.84%, connected_clients 305-321 across all 240s buckets 06:00-07:20. [F32]
- redis-cache metrics were also unchanged at the 05:50Z portfolio-service v4.4.0 deploy: hit_ratio 95.05-96.52%, cpu_pct 21.51-22.78%, evictions 2.24-3.31, connected_clients ~304-321 — no anomaly that could have propagated into auth-service. [F33]
- Config-change queries for redis-cache, user-db and api-gateway all returned count 0 in the window, excluding a config-driven variant of the shared-cache theory and any gateway config-driven amplification. [F22, F20, F21]
- user-db connections_active steps sharply exactly in the 06:52 bucket (~24-26 to 76.2 avg, max 79.84, holding 77-80), a ~3x step with no intermediate ramp and no lead time before the auth-service onset. [F34]
- The other three user-db metrics are flat through 06:00-07:20: slow_queries_per_min 0.25-0.35, cpu_pct 17.37-18.97%, replication_lag_ms 28.68-31.30ms — no DB-side resource starvation. [F36]
- auth-service memory_pct shows no step change at 06:52, wandering in a flat 47.56-55.22% band across 06:30-07:10 with no upward trend or discontinuity. [F28]
- auth-service exposes no GC metric ('gc_pause_ms' returned not_found), so a GC-trend comparison cannot be made from this telemetry. [F29]
- No deployment or config change exists for api-gateway anywhere near the H5 window (scoped and unscoped queries count 0), so no gateway change could have introduced retry/amplification behaviour at onset. [F38]
- api-gateway is the direct caller of auth-service over http, with has_telemetry=true, so gateway-side telemetry exists in principle. [F39]
- The api-gateway available_metrics list was never retrieved, so no gateway-side 5xx/retry/timeout counter for 06:45-07:15Z exists in this round's evidence — the H5 signal is absent from the dataset rather than measured flat. [F42]
- The full post-onset tail 07:00-08:59Z contains exactly four distinct log patterns (accept-queue-full 503, worker pool saturated, autoscaler cap, one clock-drift warning), with no fifth pattern. [F48]
- The tail is a pure continuation of onset signatures at the same level: 358 accept-queue-full errors over the 2-hour tail vs 372 at onset, persisting to 08:58:57Z, with queue depths (74318, 93865, 91429) of the same order as onset (98763). [F49]
- No timeout or connection-reset signature appears anywhere in the post-onset tail: WARN+ searches for 'timeout' and 'reset' over 07:00-08:59Z both returned 0 matches. [F50]
- The only new tail log signature is a single benign 'clock drift Nms detected, resyncing with NTP' at 07:29:32Z, ~37 minutes after onset, not on the request path — it cannot explain the 06:52 step. [F52]
- auth-service metrics expose no endpoint-level breakdown, so login/password-verification latency could not be separated from other endpoints; the only endpoint named in logs is /v1/auth/login. [F18]
- No alert or incident record matching a client/load-generator/retry-storm amplifier exists in the historical incident store, and none is dated 2026-04-22. [F40]
- The only pre-06:52 browser-of-record precedent HIST-101 (2025-08-14, bcrypt cost factor 10->14 in auth-service v6.4.0, fixed by rollback) matches the symptom set but is a lead, not evidence about this incident. [F6]

## Assumptions (not verified)
- Assumption: request_rate counts genuine inbound offered load at auth-service and at api-gateway, rather than a metric artifact or a re-emission of the service's own retries.
- Assumption: the ~4.5x auth-service inbound step is driven by the demand rise observed at the gateway edge (F43/F44/F45), not by amplification generated between gateway and auth-service; the gateway retry/5xx counter was never retrieved (F42), so H5 is weakened rather than positively excluded.
- Assumption: the ~3x user-db connections_active step at 06:52 is a consequence of auth-service saturation (holding connections open / worker queueing) rather than an independent DB-side cause, since it has no lead time and the other three DB metrics are flat.
- Assumption: 'no deployment / no config change' conclusions hold outside the queried scopes; some config-change queries (user-db, api-gateway) and the parallel historical shared-cache search were not executed.
- Assumption: the absence of a bcrypt log signature is not conclusive because log.level was lowered INFO->WARN at 03:40Z (F2), so bcrypt work could occur without being logged; however no deployment exists to have introduced such a change (F1/F24).
- Assumption: the autoscaler maxReplicas 8 cap is the binding capacity constraint for auth-service and is independent of any gateway-side scaling.
- Assumption: the 240s bucket granularity of the dependency metric series could mask a sub-4-minute redis-cache blip (F19/F32); no such blip is asserted as fact.
- (demoted from facts: no verified observation backs it) Neither dependency shows any anomaly in the 06:40-06:52 pre-onset interval; the user-db connection rise lands in the same 06:52 bucket as auth-service's CPU/latency step, consistent with being a consequence of auth-service saturation rather than a cause.
- (demoted from facts: no verified observation backs it) The tail confirms a single-phase capacity-saturation event rather than a second error family: no downstream-timeout, connection-reset, gateway/proxy or retry-storm signature appears in 07:00-08:59Z.
- (demoted from facts: no verified observation backs it) The observed auth-service profile is a saturation/CPU-bound signature within auth-service rather than a downstream-latency signature; logs show no timeout/dependency error messages at onset.

## Timeline
- `2026-04-22T03:40:00Z` Only auth-service config change in the window: cfg-2801 log.level INFO->WARN by config-bot ('Reduce log volume'); no auth-logic change. [F2, F23]
- `2026-04-22T05:50:00Z` Only deployment across all services in the window: portfolio-service v4.4.0 ('Holdings export'), status live, not rolled back — precedes onset by ~1h with no coincident redis-cache or auth-service anomaly. [F1, F25, F33]
- `2026-04-22T06:19:11Z` Earliest window log warning: 'slow request 3759ms on /internal/metrics' — before the CPU step; no further signature until onset. [F11]
- `2026-04-22T06:51:00Z` Demand step begins at both edges: auth-service request_rate 375.35 (06:50) -> 529.5 (06:51) -> 715.14 (06:52); api-gateway inbound 1348.68 -> 1652.95 (06:51) -> 2164.64 (06:52) — same minute, no gateway lead time. [F26, F27, F43, F44]
- `2026-04-22T06:52:00Z` CPU onset at auth-service: 35.39% (06:51) -> 96.82% (06:52), holding ~96-98%; latency and CPU step in the same minute. [F9, F14]
- `2026-04-22T06:52:00Z` user-db connections_active steps from ~25 avg to 76.2 avg (max 79.84) in the same 06:52 bucket, with no earlier ramp — at or after the auth-service onset, not before it. [F34, F35]
- `2026-04-22T06:52:00Z` redis-cache proxy metrics (hit_ratio_pct, evictions_per_min, cpu_pct, connected_clients) show no step at onset, and none at the 05:50 deploy either. [F32, F33]
- `2026-04-22T06:54:00Z` api-gateway error_rate and latency begin degrading (~2-3 min after the traffic step): error_rate 4.78 avg, p99 3979.99 avg — the gateway signature is the auth-service latency propagated outward. [F46, F47]
- `2026-04-22T06:54:53Z` First WARN 'worker pool saturated (active=64/64, queue=...)' at auth-service; first ERROR 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' at 06:55:42. [F11, F15]
- `2026-04-22T06:54:00Z` auth-service rate_limited_per_min steps from ~1.46/min to 607.72 avg (max 919.45), holding ~852-926/min through 07:09. [F30]
- `2026-04-22T06:57:15Z` First autoscaler WARN 'desired replicas 14 exceeds maxReplicas 8; cannot scale further' — capacity cannot be added while CPU is pinned. [F12, F17]
- `2026-04-22T07:06:00Z` Incident INC-008 reported by on-call SRE: login latency ~4s and 503s on /v1/auth/login, suspected as a bcrypt regression recurrence. []
- `2026-04-22T07:29:32Z` Only new tail signature: single benign 'clock drift ... resyncing with NTP' warning (count 1), ~37 min after onset, not on the request path. [F52]
- `2026-04-22T08:58:57Z` Tail unchanged: post-onset 07:00-08:59Z contains exactly four log patterns, with accept-queue-full 503s (358) and worker-pool saturation (238) at the same volume as onset — a single-phase saturation event persisting to the last matched line. [F48, F49, F50, F53]

## Recommended actions
- **[immediate]** Raise the auth-service autoscaler cap above maxReplicas 8 (ConfigMap/HPA spec for auth-service; the cap is what produced 'desired replicas 14 exceeds maxReplicas 8') so the service can add the ~14 replicas it is already asking for, and re-check CPU/latency within minutes. -- The autoscaler warning shows the service wanted 14 replicas but was capped at 8 for the entire incident while CPU sat at ~97% and the worker pool stayed 64/64 (F12/F17/F51); this is the single most direct relief for a capacity saturation root cause. [F12, F17, F51, F9]
- **[immediate]** Apply edge-level admission control / rate limiting (api-gateway) or per-client request caps to shed excess inbound login load, and confirm that gateway inbound request_rate is the source of the surge rather than a downstream amplification. -- Gateway inbound traffic stepped first and largest (~3.8x, to ~5197 req/s) at the same 06:51 minute as auth-service, so the surge enters at the edge (F43/F44/F45); shedding at the edge buys headroom for the auth-service pool. [F43, F44, F45]
- **[immediate]** Do NOT roll back or redeploy auth-service as the initial mitigation, and remove the bcrypt-regression hypothesis from the incident comms. -- No auth-service deployment or canary exists in the window, no bcrypt log signature exists, and the 503s are accept-queue/saturation rejections rather than password-verification failures — the reporter's HIST-101 analogy does not apply (F1/F24/F16/F15). [F1, F24, F16, F15, F6]
- **[short_term]** Once the cap is raised, verify relief by watching auth-service cpu_pct, latency_p99_ms, error_rate, rate_limited_per_min and the accept-queue-full / worker-pool-saturated log counters return toward baseline over the next 15-30 minutes. -- The incident showed no relief for 2 hours: latency held ~3.8s, errors ~14%, and queue depths ~74k-99k while capped (F7/F8/F9/F30/F49/F51) — recovery must be verified against these same signals. [F7, F8, F9, F30, F49, F51]
- **[short_term]** Retrieve the api-gateway available_metrics list and query gateway-side retry/5xx-upstream/timeout counters and cpu_pct for 06:45-07:15Z, plus a longer gateway request_rate baseline before 06:40Z, to establish whether the demand step is organic or partly client-side retry amplification. -- This is the one unresolved discriminator: the gateway retry/5xx counter was never retrieved (F42), so H5 is weakened but not positively excluded (F41); the answer changes whether the durable fix is capacity headroom or client-side rate limiting. [F42, F39, F38, F43]
- **[short_term]** Add per-endpoint/per-route request-rate, latency and CPU breakdowns to auth-service and api-gateway, so a future surge can be attributed to /v1/auth/login versus all routes. -- Neither service exposes endpoint-level metrics today (F18), which is exactly why H2 (broad surge) versus H5 (login-specific retry loop) could not be separated at the metric level. [F18, F29]
- **[long_term]** Size auth-service capacity/headroom for the observed peak offered load (~1814 req/s at auth-service, ~5197 req/s at the gateway) rather than the pre-incident ~375 req/s baseline, and re-baseline autoscaling thresholds accordingly. -- The service had no headroom: a ~4.5x step took it straight from 35% to 97% CPU and saturated the 64-worker pool, and the pre-step baseline left no margin (F26/F9/F11). [F26, F9, F11, F43]
- **[long_term]** Alert on the autoscaler cap condition ('desired replicas exceeds maxReplicas') as a first-class capacity alert rather than a routine WARN, and on sustained CPU > 90% plus rising worker-queue depth. -- The cap warning ran for the entire 2-hour incident (34 occurrences at onset, 33 in the tail) without preventing or shortening the outage (F12/F17/F51). [F12, F17, F51]
- **[long_term]** Add GC/heap telemetry to auth-service and re-verify memory_pct behaviour under load, since no GC metric exists and memory_pct alone cannot exclude an internal GC/heap contribution. -- The internal resource-exhaustion hypothesis (H4) cannot be fully tested from current telemetry: 'gc_pause_ms' returned not_found, and memory_pct is flat (F29/F28). [F29, F28]

## Data gaps
- api-gateway's available_metrics list was never retrieved, so no gateway-side 5xx-upstream, retry or timeout counter for 06:45-07:15Z exists — the H2-vs-H5 discriminator is absent from the dataset rather than measured flat (F42).
- api-gateway cpu_pct, an extended pre-06:40Z gateway request_rate baseline, and gateway logs for 06:45-07:00Z could not be queried (budget_exhausted), so it is unverified whether the gateway itself was CPU-saturated or whether retries/client reconnects are visible at the gateway edge.
- No caller-side/client-side or edge/LB telemetry exists for traffic arriving at api-gateway, and the metric window starts at 04:00Z — the ultimate origin of the 06:51 demand step (real external users vs. one misbehaving client/retry loop) cannot be settled.
- auth-service has no per-endpoint/per-path metrics (only aggregate cpu_pct, error_rate, latency_p99_ms, memory_pct, rate_limited_per_min, request_rate, restarts), and logs only ever name /v1/auth/login — so it cannot be shown whether the surge and slowdown are login-specific or spread across all endpoints (F18).
- auth-service exposes no GC metric ('gc_pause_ms' not_found), and memory_pct is flat, so a GC-pause/heap-pressure contribution (H4) cannot be confirmed or excluded from telemetry (F28/F29).
- redis-cache and user-db expose no latency metric, so 'auth-service slow because a dependency is slow' could not be tested at the latency level; the redis-cache series was also only available in 240s buckets, so a sub-4-minute cache blip in the onset bucket cannot be excluded (F19/F32).
- user-db api-gateway config-change queries and the historical search for a shared-cache/login-slowness precedent were not executed (budget_exhausted), so a config-level change on those components and any historical shared-cache precedent remain unverified.
- The post-onset auth-service log tail query and several targeted keyword probes (gateway, retry, connect) failed with budget_exhausted, so the absence of gateway/proxy, connection and retry signatures is inferred from the exhaustive top_patterns enumeration (exactly four patterns over all 630 matched lines) rather than from direct keyword probes.
- The tail log queries returned truncated=true (40 of 358 / 40 of 630 lines), so pattern-level completeness is established but per-line confirmation of later timestamps is incomplete; a rare late-occurring unique message could in principle be missed.
- The ~3x user-db connections_active step could not be attributed to auth-service specifically, since user-db is shared and other clients' connection counts were not queried.

## What would change this conclusion
- A gateway-side retry/5xx-upstream or timeout counter stepping sharply BEFORE or independently of the 06:51 gateway inbound request step would indicate amplification rather than genuine offered load and would move H5 above H2.
- Evidence that api-gateway inbound request_rate was flat while auth-service inbound stepped ~4.5x would be direct evidence of retry amplification between gateway and auth-service, overturning H2 as the trigger.
- A redis-cache metric anomaly (hit_ratio drop, eviction spike, CPU step) within the 06:48-06:52 bucket at finer than 240s granularity would revive the shared-cache hypothesis (H3) and change the remediation.
- Discovery of a post-06:40Z auth-service change record of any kind (release, canary, feature flag or config item) not captured by the deployment/config queries would reopen the bad-deployment hypothesis (H1).
- A memory or GC-pause step at 06:52 (requiring GC telemetry that does not currently exist) would support in-process resource exhaustion (H4) over demand-driven saturation.
- Evidence that the traffic surge was concentrated exclusively on /v1/auth/login while all other routes stayed flat would favour a login-specific retry loop (H5) over broad organic demand (H2).
- Finding that the 06:52 CPU step preceded the 06:51 request-rate step at minute resolution, or that the user-db/redis-cache anomaly preceded auth-service onset, would break the demand-causes-saturation ordering.

## Integrity warnings (set by code)
- fact demoted to assumption: Neither dependency shows any anomaly in the 06:40-06:52 pre-onset interval; the 
- fact demoted to assumption: The tail confirms a single-phase capacity-saturation event rather than a second 
- fact demoted to assumption: The observed auth-service profile is a saturation/CPU-bound signature within aut

## Investigation stats
- latency 254.47s, LLM calls 37, tokens 344945 (in 290118 / out 54827)
- tool calls 67 (13 failed, 0 partial, 0 recovered by retry), findings 53/53 verified
