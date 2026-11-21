# Incident investigation report: INC-008
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Login latency (~3.8s p99) and 503s on /v1/auth/login began abruptly at ~06:48-06:52 on 2026-04-22, when inbound traffic at api-gateway surged ~4x (1255 -> ~4900 req/s). This propagated to auth-service (request rate ~395 -> ~1500 req/s), saturating its CPU (~97%) and its fixed 64-worker pool; the autoscaler wanted 14 replicas but is capped at maxReplicas 8, so excess requests were rejected with accept-queue-full 503s (~14% error rate). All downstream dependencies (redis-cache, user-db) stayed healthy, and there was NO auth-service deployment or traffic-related config change in the window. The reported 'bcrypt regression' hypothesis is contradicted: per-request cost did not change and the fault tracks external load, not code. Root cause is capacity saturation at auth-service driven by an external traffic surge; the ultimate source of the surge (organic vs abusive bot traffic) could not be identified from available telemetry.

## Probable root cause
**Capacity saturation of auth-service from a 4x inbound traffic surge concentrated on the login path** -- capacity_saturation in `auth-service` (confidence 0.70, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Capacity saturation of auth-service from a 4x inbound traffic surge concentrated on the login path | capacity_saturation | auth-service | - | 0.70 (medium) | active |
| 2 | H2: bcrypt / auth code regression after an auth-service release (the reported guess) | bad_deployment | auth-service | - | 0.05 (low) | rejected |
| 3 | H3: Dependency failure of a shared backend (redis-cache, user-db) or external kyc-provider | dependency_failure | redis-cache | - | 0.05 (low) | rejected |

### H1: Capacity saturation of auth-service from a 4x inbound traffic surge concentrated on the login path
An uncontrolled ~4x surge of inbound requests at the api-gateway (1255 -> ~4900 req/s starting ~06:48) drove auth-service from ~395 to ~1500 req/s, pinning CPU at ~97% and saturating the fixed 64-worker pool. The autoscaler wanted 14 replicas but is capped at maxReplicas 8, so requests were rejected with accept-queue-full 503s on /v1/auth/login. The fault originates as capacity/provisioning limits at auth-service being exceeded by sustained external load; all downstream dependencies remained healthy.

**Causal chain**
1. Inbound load at api-gateway increases ~4x at 06:48 [F3, F16]
2. auth-service receives ~4x its normal request rate (~1500 req/s) [F4]
3. auth-service CPU saturates (~97%) and the 64-worker pool fills [F5, F6]
4. Autoscaler cannot add capacity beyond maxReplicas 8 [F7]
5. Requests queue then are rejected with 503; latency ~3.8s and error_rate ~14% [F8, F9]

**Supporting evidence**
  - `F3` ✔ api-gateway inbound request_rate rose ~4x, from ~1255 req/s to ~4900 req/s, starting 06:47-06:52 (1255 -> 4900; 1364.4 at 06:48 -> 4763 at 07:00).
  - `F4` ✔ auth-service request_rate rose from ~395 req/s to ~1500 req/s at the same time as the gateway surge (1091.52 at 06:49 -> 1504.93 by 08:59).
  - `F5` ✔ auth-service CPU sat at ~97-98% (from ~38% baseline) coincident with the surge, peaking at 99.54%.
  - `F6` ✔ auth-service logged worker pool saturation (active=64/64, queue tens of thousands) and accept-queue-full 503s on POST /v1/auth/login.
  - `F7` ✔ Autoscaler could not scale: 'desired replicas 14 exceeds maxReplicas 8; cannot scale further' (repeated 34 times in the window).
  - `F8` ✔ auth-service p99 latency jumped from ~90ms (<06:48) to ~3800-4000ms (06:52 onward), simultaneous with the traffic surge.
  - `F9` ✔ auth-service error_rate jumped from ~0.2% to ~14% at 06:52, coincident with latency onset.
  - `F10` ✔ api-gateway latency_p99 rose in lockstep with auth-service (210ms -> ~3900ms).
  - `F11` ✔ redis-cache stayed healthy: hit ratio ~96% (flat), connected_clients ~310 (flat), cpu ~22% (flat) throughout the incident.
  - `F12` ✔ user-db stayed healthy: CPU flat ~18%, connections_active rose from ~25 to ~78 but nowhere near exhaustion.
  - `F13` ✔ auth-service memory stayed flat ~52% and restarts were 0 during the incident (no crash/OOM).
  - `F14` ✔ auth-service rate_limited_per_min rose from ~2/min to ~900/min, showing the limiter engaging on the surge but not stopping it.
  - `F15` ✔ portfolio-service (the only other service with a recent deploy) request_rate stayed flat ~570 req/s, i.e. NOT all services saw the 4x surge.
  - `F16` ✔ api-gateway has no upstream service nodes in the dependency graph; it is the external edge, and its own CPU stayed flat ~42% during the surge.
  - `F19` ✔ auth-service runs a fixed 64-worker pool and is capped at maxReplicas 8; sustained request_rate ~1500 req/s saturated it while downstreams stayed idle.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The gateway request_rate is genuine inbound demand and its 4x increase is the root driver rather than a downstream retry amplification.
  - No auth-service release in the window means per-request cost did not change.
  - maxReplicas 8 is a real, binding configured cap (not a metric artifact).

### H2: bcrypt / auth code regression after an auth-service release (the reported guess)
A code change in auth-service increased per-request cost (e.g. bcrypt cost factor), raising login latency and CPU and producing 503s under normal load, mirroring HIST-101.

**Causal chain**
(none given)

**Supporting evidence**
  - `F17` ✔ Prior incident HIST-101 (2025-08-14) attributed identical symptoms (login p99 4s, 503, high cpu) to a bcrypt cost factor increase in auth release v6.4.0, fixed by rollback.
**Contradicting evidence**
  - `F1` ✔ No deployment occurred for auth-service in the window (nor any auth deploy since before 2026-04-15); the only deploy fleet-wide was portfolio-service v4.4.0 at 05:50.
  - `F3` ✔ api-gateway inbound request_rate rose ~4x, from ~1255 req/s to ~4900 req/s, starting 06:47-06:52 (1255 -> 4900; 1364.4 at 06:48 -> 4763 at 07:00).
  - `F4` ✔ auth-service request_rate rose from ~395 req/s to ~1500 req/s at the same time as the gateway surge (1091.52 at 06:49 -> 1504.93 by 08:59).
  - `F15` ✔ portfolio-service (the only other service with a recent deploy) request_rate stayed flat ~570 req/s, i.e. NOT all services saw the 4x surge.
  - `F13` ✔ auth-service memory stayed flat ~52% and restarts were 0 during the incident (no crash/OOM).
**Assumptions**
  - None verified; relies on symptom similarity to HIST-101.
**Rejected because:** No auth-service deployment exists in the window (F1); CPU saturation tracks an external 4x request-rate surge rather than constant load (F3,F4); other services behind the same gateway did not degrade (F15); memory/restarts are flat, inconsistent with a code-path regression (F13). Only similarity is a past incident lead (F17).

### H3: Dependency failure of a shared backend (redis-cache, user-db) or external kyc-provider
A degradation in a downstream dependency (redis-cache, user-db, or kyc-provider) slowed auth-service login handling and caused 503s.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ redis-cache stayed healthy: hit ratio ~96% (flat), connected_clients ~310 (flat), cpu ~22% (flat) throughout the incident.
  - `F12` ✔ user-db stayed healthy: CPU flat ~18%, connections_active rose from ~25 to ~78 but nowhere near exhaustion.
  - `F13` ✔ auth-service memory stayed flat ~52% and restarts were 0 during the incident (no crash/OOM).
**Assumptions**
  - kyc-provider health is unknown (no telemetry).
**Rejected because:** redis-cache remained flat and healthy (hit ratio 96%, clients ~310, cpu ~22%); user-db CPU flat ~18% and connections far from exhaustion; auth-service memory flat and no restarts. No downstream shows a coincident onset. (kyc-provider has no telemetry, so it cannot be fully excluded, but nothing implicates it.)

## Facts (verified observations)
- No auth-service deployment occurred in the window; the only deploy fleet-wide was portfolio-service v4.4.0 at 05:50. [F1]
- The only config change was auth-service log.level INFO->WARN at 03:40 (log verbosity only). [F2]
- api-gateway inbound request_rate rose ~4x (~1255 -> ~4900 req/s) beginning ~06:48. [F3, F16]
- auth-service request_rate rose from ~395 to ~1500 req/s coincident with the gateway surge. [F4]
- auth-service CPU rose from ~38% to ~97% with the surge. [F5]
- auth-service logged worker-pool saturation (64/64) and accept-queue-full 503s on /v1/auth/login. [F6]
- Autoscaler could not scale: desired 14 exceeds maxReplicas 8. [F7]
- auth-service p99 latency jumped to ~3.8-4.0s and error_rate to ~14% at 06:52. [F8, F9]
- redis-cache and user-db stayed healthy and flat during the incident. [F11, F12]
- auth-service memory was flat and restarts were 0. [F13]
- portfolio-service traffic stayed flat, showing the surge was not fleet-wide. [F15]
- A historical incident with identical symptoms was caused by a bcrypt cost increase in a prior auth release. [F17]
- No credential-stuffing/401 spike was found in auth-service logs. [F18]

## Assumptions (not verified)
- The gateway request_rate measures genuine inbound demand (external clients), not an artifact of internal retries.
- auth-service CPUs at ~97% are effectively saturated for this workload.
- maxReplicas=8 is a real configured limit and the worker pool is fixed at 64.
- kyc-provider (no telemetry) was not the bottleneck, though this is unverified.

## Timeline
- `2026-04-22T03:40:00Z` auth-service config change: log.level INFO -> WARN (log verbosity only). [F2]
- `2026-04-22T05:50:00Z` portfolio-service v4.4.0 deployed (unrelated; its traffic stayed flat). [F1, F15]
- `2026-04-22T06:47:00Z` api-gateway inbound request_rate begins climbing (~1364 -> 2810 -> 4365 req/s). [F3]
- `2026-04-22T06:49:00Z` auth-service request_rate rises to ~1091 req/s; p99 latency rises toward ~2-4s. [F4, F8]
- `2026-04-22T06:52:00Z` auth-service p99 ~1925ms and error_rate jumps to ~14%; onset of 503s. [F8, F9]
- `2026-04-22T06:54:53Z` auth-service logs worker pool saturated (active=64/64, queue=39035). [F6]
- `2026-04-22T06:57:15Z` Autoscaler warns desired 14 exceeds maxReplicas 8; cannot scale further. [F7]
- `2026-04-22T07:06:00Z` Incident INC-008 reported (auth-service p99 3.8s, 503 on login). []
- `2026-04-22T08:59:00Z` Conditions persist: auth-service CPU ~97%, p99 ~3952ms, error_rate ~14%, 503s ongoing. [F5, F8, F9]

## Recommended actions
- **[immediate]** Immediately raise auth-service maxReplicas above 8 (e.g. to 14-16) to match the autoscaler's desired count, and/or shed/queue excess login traffic at the api-gateway. -- Autoscaler is blocked at maxReplicas 8 while wanting 14; the 64-worker pool is fully saturated and requests are being 503'd. [F6, F7, F14]
- **[immediate]** Identify and, if needed, throttle the source of the 4x inbound surge at the edge (rate-limit/bot mitigation), since portfolio-service did not see the surge and the load is concentrated on the login path. -- A targeted login-path surge suggests abusive/automated traffic; mitigating at the edge protects all backends. [F3, F15, F16]
- **[short_term]** Do NOT roll back auth-service expecting recovery; there is no auth deploy in the window, so a rollback cannot address the cause and would add risk. -- The reported bcrypt-regression guess is contradicted by the absence of any auth deployment and by the flat downstreams. [F1, F11, F12]
- **[long_term]** Review autoscaling policy/limits and worker-pool sizing for auth-service (raise maxReplicas ceiling, consider headroom), and align request-rate limits with expected peaks. -- A hard maxReplicas cap of 8 made the service unable to absorb a foreseeable load multiple. [F7]
- **[long_term]** Add traffic-source attribution/telemetry at api-gateway (per-client, per-endpoint) so future surges can be classified as organic vs abusive. -- The origin of the surge could not be determined from current telemetry (api-gateway has no upstream nodes). [F16]

## Data gaps
- The source/cause of the 4x inbound traffic surge is unidentified: api-gateway shows no upstream service nodes and logs give no client/endpoint attribution; cannot distinguish organic load spike from abusive bot traffic.
- kyc-provider (external dependency of auth-service) has no telemetry, so its health during the incident is unknown and cannot be fully excluded.
- No client-IP / per-endpoint request breakdown was available to confirm the surge was login-specific.
- auth-service logs only cover WARN+; INFO-level batch latency detail before the surge is sparse, so the pre-surge p50 trend is only partially observable.

## Investigation stats
- latency 83.53s, LLM calls 10, tokens 201830 (in 188640 / out 13190)
- tool calls 45 (2 failed, 0 partial, 0 recovered by retry), findings 19/19 verified
