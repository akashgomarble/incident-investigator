# Incident investigation report: INC-008
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
At ~06:50 UTC a large inbound request flood concentrated on the api-gateway -> auth-service login path (~3.6x on gateway, ~3.9x on auth, all on /v1/auth/login) drove auth-service CPU from ~38% to ~97% and saturated its worker pool (active=64/64, accept-queue full), producing ~3.8s p99 login latency and ~14% 503s. Traffic began rising (06:50) BEFORE latency degraded (06:54), proving load-driven saturation rather than a per-request code slowdown. The incident report's bcrypt-regression guess is not supported: no auth-service deployment occurred in the window and request_rate rose 4x, which a bcrypt cost change could not cause. Other gateway backends (order, market-data, payment, portfolio) and auth's downstreams (user-db, redis-cache) are flat/healthy. Fault origin = demand outgrowing provisioned capacity (autoscaler capped at maxReplicas 8).

## Probable root cause
**Login request flood saturates provisioned auth capacity** -- capacity_saturation in `auth-service` (confidence 0.80, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Login request flood saturates provisioned auth capacity | capacity_saturation | auth-service | - | 0.80 (high) | active |
| 2 | H2: bcrypt/CPU regression from an auth-service release (reported guess) | bad_deployment | auth-service | - | 0.05 (low) | rejected |
| 3 | H3: Downstream dependency degradation (user-db / redis-cache / kyc-provider) | dependency_failure | user-db | - | 0.05 (low) | rejected |

### H1: Login request flood saturates provisioned auth capacity
A sudden flood of /v1/auth/login requests arriving via api-gateway (~3.6-3.9x baseline) drove auth-service CPU to ~97% and saturated its fixed worker pool (64/64, accept queues of tens of thousands). Because the autoscaler was capped at maxReplicas 8 (wanted 14), the service could not absorb the load, producing ~3.8s p99 latency and ~14% 503s. The fault is demand outgrowing provisioned capacity, with no faulty code or config change.

**Causal chain**
1. Inbound login traffic surges on the gateway->auth path at ~06:50 [F2, F7, F14]
2. auth-service CPU saturates to ~97% and worker pool fills 64/64 [F3, F4]
3. Autoscaler cannot add replicas (maxReplicas 8 < desired 14) [F5]
4. Latency ~3.8s and 503s on /v1/auth/login [F1, F6, F4]

**Supporting evidence**
  - `F1` ✔ auth-service p99 latency was ~88-94ms until 06:52, then jumped to 3759ms in the 06:54 bucket and stayed ~3.8s.
  - `F2` ✔ auth-service request_rate was ~390 req/s through 06:48, then climbed 452 (06:50) -> 742 (06:52) -> 1094 (06:54) -> 1350 (06:56), i.e. inbound traffic rose ~2 buckets before latency degraded.
  - `F3` ✔ auth-service CPU rose from ~38% (06:30) to 97% (07:00 onward).
  - `F4` ✔ auth-service logged worker-pool saturation with active=64/64 and accept-queue full returning 503 on POST /v1/auth/login, first at 06:54:53.
  - `F5` ✔ auth-service autoscaler could not scale past maxReplicas: 'desired replicas 14 exceeds maxReplicas 8'.
  - `F6` ✔ auth-service error_rate jumped from ~0.2% to 6.71% (06:54) then ~14% from 06:56.
  - `F7` ✔ api-gateway request_rate roughly doubled from ~1250 req/s to ~4900 req/s at the same onset.
  - `F14` ✔ auth-service rate_limited_per_min rose from ~2/min to ~900/min at onset, confirming a flood of login attempts rather than a uniform platform load rise.
  - `F9` ✔ Other api-gateway backends show flat request_rate across the onset: order-service ~228->231, market-data-service ~942->948, payment-service ~99->100, portfolio-service ~571->572 req/s.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The extra gateway traffic destined for auth is the login flood (gateway logs are sparse, so the exact paths/clients are not directly enumerated)
  - Distinguishing malicious (credential stuffing) vs a client retry storm does not change the origin category

### H2: bcrypt/CPU regression from an auth-service release (reported guess)
As in HIST-101, an auth-service release raised bcrypt cost, slowing each login and cascading to high CPU and 503s.

**Causal chain**
(none given)

**Supporting evidence**
  - `F1` ✔ auth-service p99 latency was ~88-94ms until 06:52, then jumped to 3759ms in the 06:54 bucket and stayed ~3.8s.
  - `F3` ✔ auth-service CPU rose from ~38% (06:30) to 97% (07:00 onward).
  - `F6` ✔ auth-service error_rate jumped from ~0.2% to 6.71% (06:54) then ~14% from 06:56.
**Contradicting evidence**
  - `F2` ✔ auth-service request_rate was ~390 req/s through 06:48, then climbed 452 (06:50) -> 742 (06:52) -> 1094 (06:54) -> 1350 (06:56), i.e. inbound traffic rose ~2 buckets before latency degraded.
  - `F10` ✔ The only deployment in the window was portfolio-service v4.4.0 at 05:50; no auth-service deployment occurred.
  - `F9` ✔ Other api-gateway backends show flat request_rate across the onset: order-service ~228->231, market-data-service ~942->948, payment-service ~99->100, portfolio-service ~571->572 req/s.
  - `F12` ✔ auth-service memory_pct stayed flat ~52% and restarts stayed 0 throughout, ruling out a crash/leak.
  - `F14` ✔ auth-service rate_limited_per_min rose from ~2/min to ~900/min at onset, confirming a flood of login attempts rather than a uniform platform load rise.
**Rejected because:** No auth-service deployment exists in the window (only portfolio-service v4.4.0), and request_rate rose ~4x, which a cost-factor change cannot cause; memory and restarts are flat and the surge is confined to the login path, not uniform.

### H3: Downstream dependency degradation (user-db / redis-cache / kyc-provider)
A slow datastore or external KYC provider made auth requests slow, backing up the worker pool.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F13` ✔ auth-service downstreams were healthy: user-db slow_queries_per_min flat ~0.3, redis-cache hit_ratio flat ~96%.
  - `F2` ✔ auth-service request_rate was ~390 req/s through 06:48, then climbed 452 (06:50) -> 742 (06:52) -> 1094 (06:54) -> 1350 (06:56), i.e. inbound traffic rose ~2 buckets before latency degraded.
**Rejected because:** user-db slow queries and connections, and redis-cache hit ratio, are flat across onset; no downstream latency degradation is visible, and a downstream slowdown would not multiply inbound request_rate ~4x.

## Facts (verified observations)
- auth-service p99 rose from ~90ms to ~3.8s starting 06:54. [F1]
- auth-service request_rate began rising at 06:50, before latency degraded at 06:54. [F2]
- auth-service CPU climbed from ~38% to ~97%. [F3]
- auth-service worker pool saturated 64/64 with accept-queue-full 503s. [F4]
- Autoscaler was capped at maxReplicas 8 while desiring 14. [F5]
- auth-service error_rate rose to ~14%. [F6]
- api-gateway request_rate doubled from ~1250 to ~4900 req/s. [F7]
- api-gateway itself was not saturated (CPU ~42%, one retry warning). [F8]
- Other gateway backends stayed flat (order, market-data, payment, portfolio). [F9]
- Only deployment in window was portfolio-service v4.4.0; no auth-service deploy. [F10]
- Only config change was auth-service log.level INFO->WARN at 03:40. [F11]
- auth-service memory flat ~52% and restarts 0. [F12]
- user-db and redis-cache healthy across onset. [F13]
- auth-service rate_limited_per_min rose from ~2 to ~900/min. [F14]

## Assumptions (not verified)
- Gateway access logs are sparse, so the exact source/paths of the extra ~3650 req/s are inferred from the rate-limited counter and login-only errors rather than enumerated directly.
- kyc-provider has no telemetry; its health is only observable through auth-service, which limits full exclusion of a KYC-side slowdown (though it would not explain the 4x inbound rate).
- The rate_limited_per_min unit (req/min) indicates login attempts rejected by auth's own limiter.

## Timeline
- `2026-04-22T03:40:00Z` auth-service log.level changed INFO->WARN (only config change; before incident) [F11]
- `2026-04-22T05:50:00Z` portfolio-service v4.4.0 deployed (only deployment in window; unrelated) [F10]
- `2026-04-22T06:48:00Z` auth-service still normal: ~390 req/s, ~89ms p99, 0.2% errors [F2, F1]
- `2026-04-22T06:50:00Z` auth-service request_rate begins rising (452 req/s); gateway p50 473ms [F2]
- `2026-04-22T06:54:00Z` auth-service latency explodes to 3759ms; error_rate 6.7%; worker pool 64/64 saturated at 06:54:53 [F1, F4, F6]
- `2026-04-22T06:57:15Z` Autoscaler cannot scale: desired 14 exceeds maxReplicas 8 [F5]
- `2026-04-22T07:00:00Z` auth CPU ~97%, ~1500 req/s, ~14% errors, ~900 rate-limited/min; gateway ~4900 req/s [F3, F6, F7, F14]
- `2026-04-22T08:59:00Z` Degradation persists to latest data [F1, F6]

## Recommended actions
- **[immediate]** Raise auth-service maxReplicas above 8 (immediate scaling headroom) and let it scale to ~14 to absorb demand -- Autoscaler was blocked at maxReplicas 8 while desiring 14; capacity is the binding constraint (F5, F3) [F5, F3]
- **[immediate]** Enable/verify edge rate limiting and bot mitigation on /v1/auth/login to shed flood traffic -- auth-service already rate-limits ~900/min; pushing protection to the edge protects the worker pool (F14, F4) [F14, F4]
- **[short_term]** Do NOT roll back auth-service (no deploy exists); instead confirm the traffic source (credential stuffing vs client retry storm) via gateway access logs/WAF -- Prevents pursuing the unsupported bcrypt/rollback theory and identifies whether the flood is malicious (F10, F2) [F10, F2]
- **[long_term]** Add pre-scaling/scheduled scaling and higher replica ceilings for auth-service ahead of known peak login windows -- The service has no spare headroom; demand growth needs provisioned capacity (F5) [F5]
- **[long_term]** Add queue-depth/p99 capacity alerting on auth-service worker pool so saturation is caught before 503s -- Queue grew to tens of thousands before 503s surfaced (F4) [F4]

## Data gaps
- No direct per-client / per-path breakdown of the api-gateway traffic surge; the flood's origin (malicious vs. benign) is not directly proven.
- kyc-provider (external, no telemetry) latency/health across onset is unobserved; only inferable via auth-service.
- api-gateway error_rate rose to ~9% but gateway logs show no matching error detail, so the split of gateway 5xx between auth-induced and other causes is unclear.
- auth-service logs only cover from ~03:00, limiting pre-incident baseline context.

## Investigation stats
- latency 94.02s, LLM calls 9, tokens 164167 (in 149974 / out 14193)
- tool calls 36 (3 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
