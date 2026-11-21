# Incident investigation report: INC-008
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
At ~06:47-06:50 on 2026-04-22 auth-service login traffic surged ~4x (400 -> ~1490 req/s at auth; api-gateway 1250 -> ~4900 req/s), driving auth-service CPU to ~97% and saturating its fixed worker pool (64/64) so the accept queue overflowed and /v1/auth/login returned 503. The autoscaler wanted 14 replicas but is capped at maxReplicas=8 and cannot scale further. There was NO auth-service deployment (0 in the last 3 weeks) and no relevant config change, so the reported bcrypt/deploy regression is ruled out; dependencies (user-db, redis-cache, kyc) and the unrelated portfolio-service stayed healthy. Root cause is capacity saturation of auth-service (insufficient provisioned/scalable capacity for the login traffic surge). The origin of the surge (attack vs legitimate) is a data gap.

## Probable root cause
**Capacity saturation of auth-service under a ~4x login traffic surge (maxReplicas cap)** -- capacity_saturation in `auth-service` (confidence 0.72, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Capacity saturation of auth-service under a ~4x login traffic surge (maxReplicas cap) | capacity_saturation | auth-service | - | 0.72 (medium) | active |
| 2 | H2: bad_deployment (bcrypt cost regression) as reported | bad_deployment | auth-service | - | 0.05 (low) | rejected |
| 3 | H3: dependency_failure (user-db / redis-cache / kyc-provider) | dependency_failure | user-db | - | 0.05 (low) | rejected |

### H1: Capacity saturation of auth-service under a ~4x login traffic surge (maxReplicas cap)
A sudden ~4x surge in login traffic (auth-service 400->1490 req/s; gateway 1250->4900 req/s) at ~06:50 exceeded auth-service's provisioned/scalable capacity. CPU hit ~97%, the fixed worker pool (64/64) saturated, and the accept queue overflowed producing 503s on /v1/auth/login; the autoscaler wanted 14 replicas but is capped at maxReplicas=8, so it could not recover.

**Causal chain**
1. Login traffic surges ~4x at ~06:50 (auth 400->1490 req/s, gateway 1250->4900 req/s) [F4, F5]
2. auth-service CPU saturates ~97% and worker pool fills (64/64) with accept-queue backlog [F3, F6]
3. Accept queue overflows -> /v1/auth/login returns 503; p99 ~3.8s [F6, F2, F1]
4. Autoscaler cannot add capacity (desired 14 > maxReplicas 8), so saturation persists [F7]
5. 503s propagate to api-gateway (error_rate ~9%) [F14]

**Supporting evidence**
  - `F1` ✔ auth-service latency p99 jumped from a ~90ms baseline to ~3.8s at 06:50 (avg 88.3 at 06:40 -> 2288.53 at 06:50) and stayed elevated (~3.8-3.9s) through 08:59.
  - `F2` ✔ auth-service error_rate rose from ~0.2% to ~14% at the same 06:50 onset.
  - `F3` ✔ auth-service CPU jumped from ~38% to ~97% at 06:50 and remained saturated.
  - `F4` ✔ auth-service request rate rose ~4x from ~400 req/s to ~1490 req/s at 06:50 onset.
  - `F5` ✔ api-gateway request rate also rose ~4x from ~1250 to ~4900 req/s at 06:50, while its CPU stayed flat (~42%), so the gateway is not the bottleneck.
  - `F6` ✔ auth-service logs show worker pool saturation and 503 rejections on login: 'worker pool saturated (active=64/64, queue=39035)' and 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)', first seen 06:54:53.
  - `F7` ✔ The autoscaler is capped: 'autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further' (first seen 06:57:15).
  - `F12` ✔ The unrelated portfolio-service request rate and error rate stayed flat (~570 req/s, ~0.2%) across the onset, so the surge is not a system-wide traffic increase.
  - `F13` ✘ unverified auth-service's own rate limiter engaged heavily: rate_limited_per_min went from ~2/min baseline to ~900/min at 06:50.
  - `F14` ✘ unverified api-gateway error_rate rose from ~0.2% to ~9% at 06:50, consistent with 503s propagating up from auth-service.
  - `F8` ✔ auth-service restarts metric is flat at 0 throughout (no crash loop / OOM); memory_pct steady ~52% (no leak).
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 06:50 traffic surge is genuinely exogenous (attack or legitimate spike) rather than produced by auth-service itself
  - maxReplicas=8 is a pre-existing provisioning cap, not itself a just-changed setting

### H2: bad_deployment (bcrypt cost regression) as reported
The reporter's hypothesis: a recent auth-service release raised bcrypt cost, making login slow/erroring (mirrors HIST-101).

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ No auth-service deployment exists in the last 3 weeks (count 0 for 2026-04-01..09:00); the only deployment in the window is portfolio-service v4.4.0 at 05:50, unrelated.
  - `F4` ✔ auth-service request rate rose ~4x from ~400 req/s to ~1490 req/s at 06:50 onset.
  - `F10` ✔ Only config change in window is auth-service log.level INFO->WARN at 03:40 (reduce log volume), well before onset and plausibly why no INFO login logs are visible.
**Rejected because:** No auth-service deployment exists in the last 3 weeks (tc-032 count 0) and no relevant config change; onset coincides exactly with a 4x traffic surge, and dependencies/portfolio-service are healthy. The symptom shape (traffic surge, CPU/queue saturation) does not match a code-path regression.

### H3: dependency_failure (user-db / redis-cache / kyc-provider)
A downstream dependency (user-db, redis-cache, or external kyc-provider) is slow, serializing auth worker threads and causing timeouts.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ Downstream dependencies are healthy: user-db slow_queries_per_min flat ~0.3 and redis-cache/user-db logs contain only isolated clock-drift/TLS warnings; user-db connections rose with load (25->79) but not exhausted.
  - `F6` ✔ auth-service logs show worker pool saturation and 503 rejections on login: 'worker pool saturated (active=64/64, queue=39035)' and 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)', first seen 06:54:53.
**Rejected because:** user-db slow_queries flat (~0.3/min), redis-cache/user-db logs show only isolated benign warnings, connection counts rose proportionally with load but are far from exhaustion, and there are no downstream timeout errors in auth-service logs (only accept-queue/worker-pool saturation).

## Facts (verified observations)
- auth-service p99 jumped from ~90ms to ~3.8s at 06:50 and stayed elevated. [F1]
- auth-service error_rate rose ~0.2% -> ~14% and 503s are exclusively on POST /v1/auth/login. [F2, F6]
- auth-service CPU rose ~38% -> ~97% and its worker pool saturated at 64/64 with accept-queue backlog. [F3, F6]
- auth-service request rate rose ~4x (400 -> ~1490 req/s) at onset. [F4]
- api-gateway request rate rose ~4x (1250 -> ~4900 req/s) with flat ~42% CPU, indicating the surge enters from upstream. [F5]
- The autoscaler is capped: desired 14 replicas > maxReplicas 8, cannot scale further. [F7]
- auth-service had 0 restarts and steady ~52% memory (no crash loop/OOM/leak). [F8]
- No auth-service deployment in the last 3 weeks; only portfolio-service v4.4.0 deployed, unrelated. [F9]
- Only config change was auth-service log.level INFO->WARN at 03:40, well before onset. [F10]
- user-db/redis-cache healthy; unrelated portfolio-service traffic/errors flat, so the surge is not system-wide. [F11, F12]

## Assumptions (not verified)
- The 06:50 login traffic surge is exogenous (attack or legitimate spike), not self-generated by auth-service.
- maxReplicas=8 and the worker-pool size of 64 are pre-existing provisioning settings, not just-changed values.
- The log.level INFO->WARN change at 03:40 is why per-request login INFO logs are absent, and it did not affect behavior.

## Timeline
- `2026-04-22T03:40:00Z` auth-service config change log.level INFO->WARN (reduce log volume) - benign, hours before onset [F10]
- `2026-04-22T05:50:00Z` portfolio-service v4.4.0 deployed (unrelated service; stayed healthy) [F9]
- `2026-04-22T06:47:00Z` Onset: auth-service and api-gateway request rates begin ~4x surge [F4, F5]
- `2026-04-22T06:50:00Z` auth-service CPU ~97%, p99 ~3.8s, error_rate ~14%; rate_limited ~900/min [F1, F2, F3, F13]
- `2026-04-22T06:54:53Z` First 'worker pool saturated (active=64/64)' log [F6]
- `2026-04-22T06:55:42Z` First 'accept queue full, returning 503 (POST /v1/auth/login)' [F6]
- `2026-04-22T06:57:15Z` Autoscaler blocked: desired 14 > maxReplicas 8 [F7]
- `2026-04-22T07:06:00Z` Incident reported by on-call SRE []
- `2026-04-22T08:59:00Z` Saturation persists at latest data; no recovery [F1, F2, F3]

## Recommended actions
- **[immediate]** Immediately raise auth-service maxReplicas (autoscaler cap) above the current bottleneck (desired 14) and/or add capacity so login traffic can be served -- Autoscaler wants 14 replicas but is capped at 8; lifting the cap is the fastest path to absorb the surge. [F7, F6]
- **[immediate]** Identify and, if malicious, block the source of the 4x login surge (per-IP/source rate limiting, WAF); check whether it is credential stuffing -- rate_limited_per_min rose to ~900/min and the surge is login-specific; if it is abuse, blocking it removes the load. [F13, F5]
- **[short_term]** Tune/verify accept-queue and worker-pool sizing and auth-service resource requests/limits so queueing sheds load gracefully instead of 503ing -- Accept queue overflowed (queue values in the tens of thousands) producing 503s rather than graceful degradation. [F6]
- **[short_term]** Add alerting on autoscaler maxReplicas saturation and on rate_limited spikes so capacity cliffs are detected before 503s -- The 'cannot scale further' condition was only visible in logs after errors began. [F7]
- **[long_term]** Confirm root cause of the surge and capacity headroom via a load test / capacity review of the login path -- Ensures provisioned capacity and autoscaling match peak login demand. [F4]

## Data gaps
- Identity/source of the 4x login traffic surge (malicious credential-stuffing vs legitimate spike) is unknown - no client IP / user-agent / geo data was queried.
- No direct auth-service INFO-level login logs are available after 03:40 (log.level was set to WARN), so per-request detail on the login path is limited.
- api-gateway shows no upstream node in the dependency graph, so the external traffic source could not be directly observed via telemetry.
- kyc-provider has no telemetry (external), so it can only be assessed indirectly through auth-service logs; no kyc-related errors were seen, but this is not a positive health confirmation.

## Investigation stats
- latency 86.4s, LLM calls 9, tokens 157872 (in 142819 / out 15053)
- tool calls 32 (3 failed, 0 partial, 0 recovered by retry), findings 12/14 verified
