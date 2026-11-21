# Incident investigation report: INC-008
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Login latency (~3.8s) and 503s on /v1/auth/login began at ~06:45-06:48Z, coinciding exactly with a ~4x surge in request rate at the edge: api-gateway ~1250->4900 req/s and auth-service ~390->1500 req/s. auth-service's worker pool saturated (active=64/64, queue up to ~98k) and its accept queue filled, returning 503s, while the HPA could only request 14 replicas against a hard maxReplicas=8 cap. No auth-service deployment existed in the prior 33h, the only config change was a cosmetic log-level tweak at 03:40Z, and there are no bcrypt or dependency-error log lines; user-db slow queries and redis-cache stayed flat. This is a capacity_saturation event (provisioned auth-service capacity + autoscaler ceiling exceeded by a traffic surge), NOT the bcrypt code regression the report guessed at. Confidence 0.72: the causal chain is well-supported and the competing hypotheses are ruled out, but the absolute origin of the external traffic surge is not directly observable and cpu_pct data was unavailable.

## Probable root cause
**External traffic surge saturates auth-service worker pool; autoscaler capped at maxReplicas=8** -- capacity_saturation in `auth-service` (confidence 0.72, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External traffic surge saturates auth-service worker pool; autoscaler capped at maxReplicas=8 | capacity_saturation | auth-service | - | 0.72 (medium) | active |
| 2 | H2: bcrypt/code regression in an auth-service release (as the report guessed) | bad_deployment | auth-service | - | 0.05 (low) | rejected |
| 3 | H3: Downstream dependency failure (user-db / redis-cache / kyc-provider) degrading auth-service | dependency_failure | user-db | - | 0.05 (low) | rejected |

### H1: External traffic surge saturates auth-service worker pool; autoscaler capped at maxReplicas=8
At ~06:48Z an external client traffic surge multiplied request rate across api-gateway (~1250->4900 req/s) and auth-service (~390->1500 req/s). auth-service's 64-worker pool saturated, the accept queue filled, and it began returning 503 on /v1/auth/login; the autoscaler could not add capacity because it is capped at maxReplicas=8 while requesting 14. The fault originates in capacity provisioning for auth-service (capacity_saturation), not in code or a dependency.

**Causal chain**
1. External traffic surge -> api-gateway and auth-service request rates rise ~4x at 06:48Z [F2, F3]
2. auth-service worker pool (64/64) saturates and accept queue fills [F5]
3. Autoscaler capped at maxReplicas 8 cannot add capacity (wants 14) [F6]
4. Requests rejected with 503 -> p99 ~3.8s and 14% error rate [F1, F5]

**Supporting evidence**
  - `F1` ✔ auth-service p99 latency was steady ~90ms until 06:45Z, then jumped to ~3.8s and stayed there through 08:59Z.
  - `F2` ✔ auth-service request_rate rose from ~390 req/s to ~1500 req/s at the same onset (06:48 bucket, max 1813.74).
  - `F3` ✔ api-gateway request_rate rose from ~1250 req/s to ~4900 req/s at the same onset (06:48 bucket, max 3413.7; then ~4900).
  - `F4` ✔ api-gateway p99 latency jumped from ~210ms to ~3.9s in the same 06:48Z window.
  - `F5` ✔ auth-service logs show worker pool saturation (active=64/64, queue up to 98763) beginning 06:54:53Z and 503s 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' beginning 06:55:42Z.
  - `F6` ✔ auth-service autoscaler repeatedly reports it wants 14 replicas but is capped at maxReplicas 8, from 06:57:15Z onward (34 occurrences).
  - `F7` ✔ user-db connections_active rose from ~25 to ~78 at the same onset (06:48Z), but slow_queries_per_min stayed flat at ~0.3 throughout.
  - `F12` ✔ auth-service memory_pct stayed flat (~52%) and restarts stayed 0 throughout the incident — no memory leak or pod restarts.
**Contradicting evidence**
  - (none)
**Assumptions**
  - auth-service worker pool = 64 per replica and is the throughput bottleneck
  - the 4x traffic is external/legitimate and not an internal retry storm

### H2: bcrypt/code regression in an auth-service release (as the report guessed)
A new auth-service release raised bcrypt cost factor (or similar) and made login CPU-bound, causing the latency and 503s — mirroring HIST-101.

**Causal chain**
1. New auth-service deploy raises bcrypt cost -> slow login *(assumed)*

**Supporting evidence**
  - `F13` ✔ A historical incident HIST-101 (2025-08-14) matches the reported symptom (login p99 4s, 503, high cpu) but its root cause was a bcrypt cost-factor bump in release v6.4.0.
**Contradicting evidence**
  - `F9` ✔ There were no code deployments for auth-service between 2026-04-21T00:00Z and 2026-04-22T09:00Z (count 0); the only deployment in the window was portfolio-service dep-5803 at 05:50Z.
  - `F10` ✔ The only config change in the window was auth-service log.level INFO->WARN at 03:40Z (cfg-2801), before onset and cosmetic.
  - `F11` ✔ No occurrences of 'bcrypt' appear in auth-service logs for 06:30-09:00Z, and no kyc-provider-related INFO/WARN log lines appear.
  - `F2` ✔ auth-service request_rate rose from ~390 req/s to ~1500 req/s at the same onset (06:48 bucket, max 1813.74).
  - `F3` ✔ api-gateway request_rate rose from ~1250 req/s to ~4900 req/s at the same onset (06:48 bucket, max 3413.7; then ~4900).
  - `F2` ✔ auth-service request_rate rose from ~390 req/s to ~1500 req/s at the same onset (06:48 bucket, max 1813.74).
**Assumptions**
  - a deploy happened but telemetry missed it
**Rejected because:** No auth-service deployment in the last 33h (F9), no relevant config change (F10), no bcrypt log lines (F11). Onset coincides with a 4x request-rate surge, not a release. Cross-service load increase is inconsistent with a pure bcrypt cost bump, and request_rate rose rather than only latency.

### H3: Downstream dependency failure (user-db / redis-cache / kyc-provider) degrading auth-service
A datastore or external KYC provider slowed/failed, causing auth-service login latency and 503s.

**Causal chain**
1. Downstream slows -> auth-service calls hang -> latency/503 *(assumed)*

**Supporting evidence**
  - `F7` ✔ user-db connections_active rose from ~25 to ~78 at the same onset (06:48Z), but slow_queries_per_min stayed flat at ~0.3 throughout.
**Contradicting evidence**
  - `F7` ✔ user-db connections_active rose from ~25 to ~78 at the same onset (06:48Z), but slow_queries_per_min stayed flat at ~0.3 throughout.
  - `F8` ✔ redis-cache showed no anomaly at onset: connected_clients flat ~305-316 and evictions_per_min flat ~3.
  - `F11` ✔ No occurrences of 'bcrypt' appear in auth-service logs for 06:30-09:00Z, and no kyc-provider-related INFO/WARN log lines appear.
**Assumptions**
  - a dependency degraded without emitting its own error signals
**Rejected because:** user-db slow_queries flat at ~0.3 (F7); redis-cache clients/evictions flat (F8); no kyc-provider errors in auth-service logs (F11). Connection rise in user-db is a downstream consequence of higher auth traffic, not a cause.

## Facts (verified observations)
- auth-service p99 latency jumped from ~90ms to ~3.8s at 06:45-07:00Z and remained there. [F1]
- auth-service request_rate rose ~4x (395->1500 req/s) at 06:48Z. [F2]
- api-gateway request_rate rose ~4x (1250->4900 req/s) at the same 06:48Z onset. [F3]
- auth-service worker pool saturated (active=64/64) and returned 503 'accept queue full' on /v1/auth/login from 06:54-06:55Z. [F5]
- auth-service autoscaler wanted 14 replicas but was capped at maxReplicas 8. [F6]
- No auth-service deployments occurred in the incident window (count 0). [F9]
- The only config change was auth-service log.level INFO->WARN at 03:40Z. [F10]
- No bcrypt or kyc-provider log lines were present in auth-service logs. [F11]
- redis-cache and user-db slow-query metrics showed no anomaly at onset. [F8, F7]
- auth-service memory and restart counts were flat. [F12]

## Assumptions (not verified)
- auth-service worker pool capacity of 64 per replica is the binding throughput limit.
- The ~4x traffic increase is external/legitimate client load rather than an internal retry storm (no evidence of retry loops in logs; api-gateway logged only 1 benign retry).
- The production autoscaler ceiling maxReplicas=8 has not changed recently (no config change observed).

## Timeline
- `2026-04-22T03:40:00Z` Config change cfg-2801: auth-service log.level INFO->WARN (cosmetic, pre-onset). [F10]
- `2026-04-22T05:50:00Z` Only deployment in window: portfolio-service v4.4.0 (dep-5803), unrelated service. [F9]
- `2026-04-22T06:48:00Z` Traffic surge: api-gateway request_rate 1250->4900 req/s; auth-service 395->1500 req/s; p99 climbs. [F2, F3, F1]
- `2026-04-22T06:54:53Z` auth-service worker pool saturated (active=64/64, queue up to 98k). [F5]
- `2026-04-22T06:55:42Z` First 503 'accept queue full' on POST /v1/auth/login. [F5]
- `2026-04-22T06:57:15Z` Autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further. [F6]
- `2026-04-22T07:06:00Z` Incident reported by on-call SRE. []
- `2026-04-22T08:59:00Z` Symptoms persist: p99 ~3.8s, auth error_rate ~14%, api-gateway error_rate ~9%. [F1]

## Recommended actions
- **[immediate]** Immediately raise auth-service autoscaler maxReplicas above 8 (e.g. to 16-20) and/or manually scale out to absorb the surge. -- Autoscaler wanted 14 replicas but was capped at 8; this is the direct reason capacity could not expand. [F6]
- **[immediate]** Identify and, if appropriate, throttle/rate-limit the source of the ~4x external traffic surge at api-gateway, or confirm it is legitimate and scale to match. -- The surge is the trigger; without managing it the service remains saturated. [F3, F2]
- **[short_term]** Add/adjust edge rate limiting and pre-scaling policies so autoscaling headroom exists above the current maxReplicas. -- Prevents recurrence of capacity_saturation under volume spikes (cf. HIST-113). [F6, F14]
- **[short_term]** Do NOT roll back auth-service (nothing to roll back); avoid the bcrypt fix suggested by the historical analogy. -- No auth-service deploy exists in the window; a rollback would be ineffective and risky. [F9, F13]

## Data gaps
- cpu_pct for auth-service, api-gateway and user-db could not be retrieved (metric backend returned 503 then circuit-open) - cannot directly confirm CPU saturation.
- rate_limited_per_min and redis hit_ratio_pct unavailable (backend 503/circuit-open) - cannot confirm whether edge rate limiting engaged or cache effectiveness changed.
- The origin/source of the external 4x traffic surge is not directly observable from the available tools; api-gateway has no upstream nodes in the dependency graph, so the surge can only be inferred from request_rate.
- No per-replica throughput/cluster maxReplicas configuration object was directly retrievable; the 'maxReplicas 8' and 'desired 14' figures come from autoscaler log messages only.

## Investigation stats
- latency 65.66s, LLM calls 8, tokens 128426 (in 118635 / out 9791)
- tool calls 39 (12 failed, 0 partial, 6 recovered by retry), findings 14/14 verified
