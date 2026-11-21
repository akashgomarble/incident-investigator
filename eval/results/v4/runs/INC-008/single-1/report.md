# Incident investigation report: INC-008
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Onset was ~06:49-06:50Z. Load on the auth-service login path rose ~4x (request_rate ~395 -> ~1500 req/s) while every other backend stayed flat, and auth-service CPU pinned at ~97% with its worker pool saturated (64/64, queue up to ~98k) and 503s ("accept queue full") on POST /v1/auth/login only. The HPA repeatedly wanted 14 replicas but was capped at maxReplicas 8, so it could not scale into the surge. There was NO auth-service deployment in the window (last deploy window empty, only an unrelated portfolio-service v4.4.0 at 05:50), NO relevant config change (only log.level INFO->WARN at 03:40), and NO degraded dependency (redis hit_ratio steady 96%, user-db CPU 18% with flat slow queries). The report's "bcrypt regression" hypothesis is contradicted: a code regression needs a release, and none occurred; the signature is demand exceeding provisioned capacity on a workload (login) that is CPU-expensive. Root cause: capacity saturation of auth-service — login traffic outgrew the provisioned replica ceiling (maxReplicas=8), causing CPU saturation, worker-pool/accept-queue exhaustion, latency ~3.8s and 503s. Whether the extra login traffic is organic or a stuffing/retry burst is a data gap, but the fault originates at auth-service's provisioning ceiling.

## Probable root cause
**Capacity saturation: login traffic outgrew auth-service's replica ceiling** -- capacity_saturation in `auth-service` (confidence 0.72, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Capacity saturation: login traffic outgrew auth-service's replica ceiling | capacity_saturation | auth-service | - | 0.72 (medium) | active |
| 2 | H2: bcrypt/code regression in a recent auth-service release (report's hypothesis) | bad_deployment | auth-service | - | 0.08 (low) | rejected |
| 3 | H3: Dependency failure (redis-cache or user-db) cascading into auth-service | dependency_failure | redis-cache | - | 0.05 (low) | rejected |

### H1: Capacity saturation: login traffic outgrew auth-service's replica ceiling
Login-path load on auth-service rose ~4x at 06:49Z (request_rate ~395 -> ~1500 req/s) with no code or config change. Because login is CPU-intensive, auth-service CPU pinned ~97%, worker pool saturated (64/64, queue to ~98k) and the accept queue filled, producing ~3.8s latency and 503s on POST /v1/auth/login. The HPA could not absorb the surge because it was capped at maxReplicas 8 (it wanted 14). Downstream (redis, user-db) stayed healthy, so the fault originates in auth-service provisioning.

**Causal chain**
1. Login request rate rises ~4x (~395 -> ~1500 req/s) at 06:49Z [F3]
2. auth-service CPU saturates to ~97% and worker pool / accept queue exhaust [F4, F5]
3. Latency rises to ~3.8s and login returns 503 [F1, F2, F13]
4. HPA cannot scale past maxReplicas 8 (wanted 14), so saturation persists [F6]

**Supporting evidence**
  - `F1` ✔ auth-service p99 latency rose from ~90ms (through 06:40) to ~2288ms in the 06:50 bucket and stayed ~3800ms thereafter.
  - `F2` ✔ auth-service error_rate jumped from ~0.2% (through 06:40) to 7.11% in the 06:50 bucket and settled ~14% afterwards.
  - `F3` ✔ auth-service request_rate rose from a steady ~395 req/s (through 06:45) to ~1490 req/s from 07:00 onward, i.e. roughly a 4x increase.
  - `F4` ✔ auth-service CPU rose from ~38% to ~97% at the same 06:50 onset and stayed pinned near 97%.
  - `F5` ✔ auth-service logs show worker pool saturation (active=64/64, queue up to 98763) and 503s 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' starting 06:54:53/06:55:42.
  - `F6` ✔ auth-service autoscaler repeatedly reports wanting more replicas than allowed: 'autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further' (34 occurrences, 06:57-08:58).
  - `F7` ✔ api-gateway request_rate also rose ~4x at 06:50 (from ~1250 to ~4900 req/s) while its CPU stayed flat at ~42%, i.e. the gateway was not the bottleneck.
  - `F8` ✔ Other backends behind the gateway were flat across the onset: order-service (~230 req/s), market-data-service (~945), portfolio-service (~570), payment-service (~100) all unchanged.
  - `F11` ✔ Dependencies showed no degradation: redis-cache hit_ratio steady ~96% (no cold-cache storm), user-db CPU flat ~18% with slow_queries_per_min flat ~0.3, redis CPU ~22%.
  - `F12` ✔ user-db connections_active rose from ~25 to ~80 at onset (06:48 bucket onward), consistent with more login work reaching the DB, but without slow queries or CPU pressure.
  - `F13` ✔ All auth-service errors were confined to the login endpoint: querying '/v1/auth/' returned only 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' (372 matches).
  - `F14` ✔ The fault is demand exceeding auth-service's provisioned replica ceiling, not a code/config/dependency fault: load spiked 4x on the CPU-expensive login path, CPU pinned at 97%, and the HPA was capped at maxReplicas 8 — a signature consistent with capacity saturation, and analogous to past incident HIST-113 ('Record market-open volume exceeded provisioned capacity').
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 4x request-rate increase reflects real inbound login demand (organic surge or stuffing/attack) rather than a metrics artifact.
  - Login handling is CPU-bound such that 4x rate saturates the 8-replica ceiling.

### H2: bcrypt/code regression in a recent auth-service release (report's hypothesis)
A recent auth-service release raised the bcrypt cost factor (as in HIST-101), making each login far more CPU-expensive and causing 4s latency and 503s.

**Causal chain**
1. bad auth-service release raises bcrypt cost [F9]

**Supporting evidence**
  - `F1` ✔ auth-service p99 latency rose from ~90ms (through 06:40) to ~2288ms in the 06:50 bucket and stayed ~3800ms thereafter.
  - `F2` ✔ auth-service error_rate jumped from ~0.2% (through 06:40) to 7.11% in the 06:50 bucket and settled ~14% afterwards.
  - `F4` ✔ auth-service CPU rose from ~38% to ~97% at the same 06:50 onset and stayed pinned near 97%.
  - `F5` ✔ auth-service logs show worker pool saturation (active=64/64, queue up to 98763) and 503s 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' starting 06:54:53/06:55:42.
**Contradicting evidence**
  - `F3` ✔ auth-service request_rate rose from a steady ~395 req/s (through 06:45) to ~1490 req/s from 07:00 onward, i.e. roughly a 4x increase.
  - `F9` ✔ No auth-service deployment occurred in the incident window (get_deployments for auth-service over 2026-04-01..04-22 returned count 0); the only deploy was portfolio-service v4.4.0 at 05:50.
  - `F10` ✔ The only config change in the window was auth-service log.level INFO->WARN at 03:40 (cfg-2801), which is unrelated to request handling; no api-gateway config changes.
**Rejected because:** No auth-service deployment exists in the window (count 0 over Apr 1-22); a code regression requires a release. Also, CPU/latency rose in lockstep with a 4x request-rate increase rather than per-request cost rising at constant load, and dependencies are healthy.

### H3: Dependency failure (redis-cache or user-db) cascading into auth-service
A shared datastore degraded, slowing auth-service logins and causing timeouts/503s.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ Dependencies showed no degradation: redis-cache hit_ratio steady ~96% (no cold-cache storm), user-db CPU flat ~18% with slow_queries_per_min flat ~0.3, redis CPU ~22%.
  - `F12` ✔ user-db connections_active rose from ~25 to ~80 at onset (06:48 bucket onward), consistent with more login work reaching the DB, but without slow queries or CPU pressure.
  - `F5` ✔ auth-service logs show worker pool saturation (active=64/64, queue up to 98763) and 503s 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' starting 06:54:53/06:55:42.
**Rejected because:** redis hit_ratio steady ~96% with no CPU rise, and user-db CPU flat ~18% with flat slow queries; neither dependency shows degradation at onset. Errors are accept-queue/worker-pool saturation, not DB/cache timeouts.

## Facts (verified observations)
- auth-service p99 rose from ~90ms to ~3800ms and error_rate from ~0.2% to ~14% at the 06:50 onset. [F1, F2]
- auth-service request rate rose ~4x (from ~395 to ~1500 req/s) at onset with no code or config change. [F3, F9, F10]
- auth-service CPU pinned at ~97%; worker pool saturated (64/64, queue to ~98k); 503 'accept queue full' on login. [F4, F5, F13]
- auth-service autoscaler wanted 14 replicas but was capped at maxReplicas 8 throughout the incident. [F6]
- api-gateway traffic rose ~4x but its CPU stayed flat (~42%); all other backends were flat. [F7, F8]
- Dependencies were healthy at onset: redis hit_ratio ~96%, user-db CPU ~18% with flat slow queries. [F11]
- No auth-service deployment occurred in the incident window; only an unrelated portfolio-service deploy at 05:50. [F9]

## Assumptions (not verified)
- The 4x request-rate rise reflects real inbound demand on the login path rather than a metrics/scrape artifact.
- auth-service login handling is CPU-bound (e.g. password hashing), so a 4x rate saturates the 8-replica ceiling.
- Multiple/HA auth-service replicas exist behind the worker-pool metric, so raising the replica cap is a valid mitigation.

## Timeline
- `2026-04-22T03:40:00Z` auth-service config change log.level INFO->WARN (cfg-2801) — unrelated to request handling. [F10]
- `2026-04-22T05:50:00Z` Unrelated deploy: portfolio-service v4.4.0. [F9]
- `2026-04-22T06:49:31Z` auth-service 'config reloaded (no changes)' — just before onset. [F5]
- `2026-04-22T06:50:00Z` auth-service request_rate ~4x, CPU ~97%, p99 ~3.8s, error_rate ~14% all begin (06:50 bucket). [F1, F2, F3, F4]
- `2026-04-22T06:54:53Z` auth-service worker pool saturation (active=64/64) begins. [F5]
- `2026-04-22T06:55:42Z` First 503 'accept queue full' on POST /v1/auth/login. [F5, F13]
- `2026-04-22T06:57:15Z` Autoscaler reports desired 14 > maxReplicas 8; cannot scale further. [F6]
- `2026-04-22T07:06:00Z` Reported_at: on-call SRE reports login latency/503s. []
- `2026-04-22T08:59:00Z` Incident still ongoing at latest data: p99 ~3.8s, error_rate ~14%, CPU ~97%, HPA still capped. [F1, F2, F4, F6]

## Recommended actions
- **[immediate]** Raise auth-service HPA maxReplicas (currently 8) to at least 14-16 so it can scale into the login surge, and add capacity headroom (or a floor above current steady state). -- The autoscaler wanted 14 replicas but was capped at 8 the entire incident; scaling is the direct relief for CPU/worker-pool saturation. [F6, F4, F5]
- **[immediate]** Protect the login endpoint: rate-limit / add bot/credential-stuffing defenses or per-client throttling on POST /v1/auth/login so a login flood cannot saturate auth-service. -- All errors are confined to the login endpoint and load rose 4x only there; if this is a stuffing/retry burst, throttling cuts demand at the source. [F13, F3]
- **[short_term]** Verify whether the 4x login surge is organic, a retry storm from api-gateway, or an attack (check gateway egress/client mix and auth login failure sources) before treating as pure capacity. -- Distinguishes capacity_saturation from an abusive-traffic data_issue and determines the durable fix; gateway request_rate exceeds the sum of backend rates. [F7, F8]
- **[short_term]** Do NOT roll back or revert auth-service code: there was no auth-service release; the report's bcrypt-regression hypothesis is unsupported. -- No deployment exists in the window, so a rollback has no target and would not address a load-driven saturation. [F9, F10]
- **[long_term]** Add pre-scaling for known demand peaks and alerting on HPA hitting maxReplicas and on accept-queue saturation. -- Historical analog HIST-113 shows a repeated pattern of provisioned capacity being exceeded; earlier detection/prescale prevents recurrence. [F6, F14]

## Data gaps
- Source of the 4x login surge is unverified: no per-client/IP/attack telemetry or gateway client-mix data was available, so organic surge vs. credential-stuffing vs. retry loop cannot be separated.
- api-gateway INFO/WARN logs for auth requests returned 0 matches (only 2 generic 'connection reset' retries), so a gateway-side retry-amplification loop cannot be fully ruled out.
- kyc-provider (external, auth-service downstream) and bank-partner-api have no telemetry, so an external-provider contribution to login latency cannot be excluded from telemetry.
- No replica-count or per-pod CPU timeseries for auth-service was available; the maxReplicas=8 cap is inferred from autoscaler warnings rather than a scaling config read.

## Investigation stats
- latency 85.73s, LLM calls 10, tokens 225145 (in 213253 / out 11892)
- tool calls 45 (0 failed, 0 partial, 0 recovered by retry), findings 14/14 verified
