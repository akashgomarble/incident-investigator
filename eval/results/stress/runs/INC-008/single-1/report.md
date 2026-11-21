# Incident investigation report: INC-008
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Login slowness/503s on auth-service began abruptly at ~06:52Z on 2026-04-22. The fault is a 4x surge in inbound traffic (api-gateway request_rate 1250 -> ~4900 req/s; auth-service 390 -> ~1500 req/s) that saturated auth-service worker pools (CPU 38% -> 97%, accept queue full, 503s) while the autoscaler was capped at maxReplicas 8 (desired 14). No auth-service deployment or config change occurred, memory stayed flat, and redis-cache/user-db are healthy, which rules out the reported "bcrypt regression" hypothesis (a code release) and dependency failures. The best-supported root cause is capacity_saturation: provisioned/autoscaler-max capacity could not absorb the sudden load. The origin of the surge (legitimate demand spike vs. abusive login flood) is not fully determined from available data — this is a data gap. Outcome: capacity_saturation at auth-service as the proximate root cause.

## Probable root cause
**Capacity saturation: 4x traffic surge overran auth-service provisioned/autoscaler-max capacity** -- capacity_saturation in `auth-service` (confidence 0.72, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Capacity saturation: 4x traffic surge overran auth-service provisioned/autoscaler-max capacity | capacity_saturation | auth-service | - | 0.72 (medium) | active |
| 2 | H4: Autoscaler misconfiguration / too-low maxReplicas (config_change) | config_change | auth-service | - | 0.08 (low) | active |
| 3 | H2: Reported bcrypt/auth-service code regression (bad deployment) — rejected | bad_deployment | auth-service | - | 0.06 (low) | rejected |
| 4 | H3: Dependency failure (redis-cache / user-db / kyc-provider) | dependency_failure | redis-cache | - | 0.04 (low) | rejected |

### H1: Capacity saturation: 4x traffic surge overran auth-service provisioned/autoscaler-max capacity
A sudden ~4x increase in inbound traffic (visible at api-gateway 1250->~4900 req/s and auth-service 390->~1500 req/s) saturated auth-service's bounded worker pool (64 workers), driving CPU to ~97%, filling the accept queue, and returning 503s; the autoscaler could not add capacity because desired replicas 14 exceeded maxReplicas 8. No faulty code/config change is involved; the fault originates in auth-service capacity limits under the surge.

**Causal chain**
1. External inbound traffic to the edge rises ~4x starting ~06:30-06:45Z [F9, F16]
2. Surge propagates to auth-service request_rate (390->1500 req/s) [F4]
3. auth-service fixed 64-worker pool saturates; CPU hits ~97%; accept queue fills [F3, F5, F20]
4. Autoscaler cannot scale past maxReplicas 8, so saturation persists and requests are rejected with 503 [F6, F2]
5. Downstream (redis/user-db) and code/config unchanged, so the constraint is auth-service capacity itself [F12, F14, F15]

**Supporting evidence**
  - `F1` ✔ auth-service p99 latency was ~90ms (max ~97ms) until 06:44Z, then jumped to ~2800-4100ms from the 06:52Z bucket onward and stayed there through 08:59Z.
  - `F2` ✔ auth-service error_rate (HTTP 5xx) was ~0.2% until 06:44Z, then rose to ~13-15% from the 06:52Z bucket onward.
  - `F3` ✔ auth-service CPU was ~38% until 06:44Z, then jumped to ~97% (max 99.54%) from 06:52Z onward.
  - `F4` ✔ auth-service request_rate was ~390-406 req/s until 06:44Z, then quadrupled to ~1234-1525 req/s from 06:52Z onward.
  - `F5` ✔ auth-service logs show worker pool saturation and 503s: pattern 'worker pool saturated (active=64/64, queue=...)' and 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)', first at 06:54:53Z / 06:55:42Z.
  - `F6` ✔ Auth-service autoscaler could not scale: log 'autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further' at 06:57:15Z.
  - `F7` ✔ auth-service rate_limited_per_min was ~2/min until 06:44Z, then jumped to ~548-908/min from 06:52Z onward.
  - `F8` ✔ auth-service memory_pct stayed flat (~51-53%) throughout the incident window, showing no memory growth.
  - `F9` ✔ api-gateway request_rate was ~1250 req/s until 06:30Z, then rose to ~4900 req/s from the 06:45-07:00Z buckets onward.
  - `F11` ✔ api-gateway CPU remained flat at ~42% (min 36.9, max 47.2) across the entire window, i.e. the gateway itself was not saturated.
  - `F12` ✔ No auth-service deployment or config change exists in the incident window: only one deployment total (portfolio-service v4.4.0 at 05:50Z, 'CSV export endpoint') and zero config changes.
  - `F13` ✔ auth-service restarts metric was 0 for the entire window (no crash/restart loop).
  - `F14` ✔ user-db CPU stayed flat (~18%, max ~20%) throughout — the database was not under stress.
  - `F15` ✔ redis-cache was healthy: CPU ~22%, hit_ratio ~96%, connected_clients ~310, all flat across the window.
  - `F16` ✔ api-gateway has no upstream dependencies in the graph (empty edges/nodes), so the traffic surge observed at the gateway originates from outside the mesh (external client traffic) at the edge.
  - `F18` ✔ Historical incident HIST-113 (2026-01-28) parallels this event: 'Record market-open volume exceeded provisioned capacity', category capacity_saturation, resolution 'Raised replica count'.
  - `F19` ✔ The 4x surge (gateway ~1250->~4900 req/s, auth ~390->~1500 req/s) started in the 06:30-06:52Z window, matching the auth p99/CPU/error onset (06:52Z), i.e. load rose first, saturation followed.
  - `F20` ✔ auth-service worker pool is bounded at 64 active workers ('active=64/64'), so at ~1500 req/s the pool and accept queue saturate, producing 503s — a capacity limit, not a per-request slowdown like bcrypt.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The surge is genuine external inbound load (or abusive flood) rather than an artifact of the gateway retrying/inflating requests
  - maxReplicas 8 is the effective capacity ceiling that prevented recovery

### H4: Autoscaler misconfiguration / too-low maxReplicas (config_change)
The autoscaler's maxReplicas=8 is the binding constraint that prevented auth-service from absorbing the surge — this could be a pre-existing misconfiguration. There is no config-change event in the window, so this is at most a latent contributing factor rather than the trigger; the trigger remains the traffic surge.

**Causal chain**
1. Traffic surge arrives [F9]
2. Autoscaler wants 14 replicas but is capped at 8 [F6]
3. Capacity ceiling prevents recovery, prolonging 503s [F2]

**Supporting evidence**
  - `F6` ✔ Auth-service autoscaler could not scale: log 'autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further' at 06:57:15Z.
**Contradicting evidence**
  - `F12` ✔ No auth-service deployment or config change exists in the incident window: only one deployment total (portfolio-service v4.4.0 at 05:50Z, 'CSV export endpoint') and zero config changes.
**Assumptions**
  - maxReplicas=8 was not recently changed (no change event observed)

### H2: Reported bcrypt/auth-service code regression (bad deployment) — rejected
The on-call's guess: a new auth-service release reintroduced an expensive bcrypt cost factor, slowing each login to ~4s and causing 503s. This is contradicted: there is no auth-service deployment or config change in the window (only unrelated portfolio-service v4.4.0), memory stayed flat, restarts were 0, and CPU/error onset coincides with a 4x request-rate surge rather than a per-request cost change.

**Causal chain**
(none given)

**Supporting evidence**
  - `F17` ✔ Historical incident HIST-101 (2025-08-14) had the same symptom profile ('login p99 4s','503 on login','high cpu') but root cause 'bcrypt cost factor raised 10->14 in release v6.4.0' (a bad_deployment).
**Contradicting evidence**
  - `F12` ✔ No auth-service deployment or config change exists in the incident window: only one deployment total (portfolio-service v4.4.0 at 05:50Z, 'CSV export endpoint') and zero config changes.
  - `F13` ✔ auth-service restarts metric was 0 for the entire window (no crash/restart loop).
  - `F8` ✔ auth-service memory_pct stayed flat (~51-53%) throughout the incident window, showing no memory growth.
  - `F4` ✔ auth-service request_rate was ~390-406 req/s until 06:44Z, then quadrupled to ~1234-1525 req/s from 06:52Z onward.
  - `F19` ✔ The 4x surge (gateway ~1250->~4900 req/s, auth ~390->~1500 req/s) started in the 06:30-06:52Z window, matching the auth p99/CPU/error onset (06:52Z), i.e. load rose first, saturation followed.
  - `F20` ✔ auth-service worker pool is bounded at 64 active workers ('active=64/64'), so at ~1500 req/s the pool and accept queue saturate, producing 503s — a capacity limit, not a per-request slowdown like bcrypt.
**Rejected because:** No auth-service deployment or config change exists (tc-020, tc-018); memory flat (F8) and restarts 0 (F13) are inconsistent with a bcrypt cost regression; latency rose together with a 4x traffic surge (F4, F19), matching saturation not per-request slowdown.

### H3: Dependency failure (redis-cache / user-db / kyc-provider)
A downstream datastore or the external kyc-provider degraded, causing auth-service logins to stall. Contradicted: redis-cache (CPU 22%, hit ratio 96%, clients flat) and user-db (CPU 18%) are healthy throughout; no dependency error signatures appear in auth logs (only accept-queue/worker-pool saturation).

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F14` ✔ user-db CPU stayed flat (~18%, max ~20%) throughout — the database was not under stress.
  - `F15` ✔ redis-cache was healthy: CPU ~22%, hit_ratio ~96%, connected_clients ~310, all flat across the window.
  - `F5` ✔ auth-service logs show worker pool saturation and 503s: pattern 'worker pool saturated (active=64/64, queue=...)' and 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)', first at 06:54:53Z / 06:55:42Z.
**Rejected because:** redis-cache and user-db metrics are flat/healthy (tc-025, tc-026, tc-027, tc-023); auth logs show local pool saturation, not dependency timeouts.

## Facts (verified observations)
- auth-service p99 latency jumped from ~90ms to ~2800-4100ms at the 06:52Z bucket and stayed elevated. [F1]
- auth-service 5xx error_rate jumped from ~0.2% to ~13-15% at the 06:52Z bucket. [F2]
- auth-service CPU jumped from ~38% to ~97% at the 06:52Z bucket. [F3]
- auth-service request_rate quadrupled from ~390 req/s to ~1500 req/s at the 06:52Z bucket. [F4]
- auth-service logged worker-pool saturation (active=64/64) and 503 rejections on POST /v1/auth/login starting 06:54-06:55Z. [F5]
- auth-service autoscaler could not scale: desired 14 > maxReplicas 8. [F6]
- auth-service rate_limited_per_min jumped from ~2/min to ~548-908/min at 06:52Z. [F7]
- auth-service memory_pct stayed flat (~51-53%); no memory growth. [F8]
- api-gateway request_rate rose ~4x from ~1250 to ~4900 req/s starting the 06:45-07:00Z buckets. [F9]
- api-gateway error_rate rose to ~9% and p99 to ~3900ms starting the 06:45Z bucket. [F10]
- api-gateway CPU stayed flat (~42%), so the gateway itself was not the saturated component. [F11]
- No auth-service deployment or config change occurred; only an unrelated portfolio-service v4.4.0 CSV-export deploy at 05:50Z. [F12]
- auth-service restarts were 0 throughout; no crash loop. [F13]
- user-db CPU stayed flat (~18%); the database was not stressed. [F14]
- redis-cache was healthy and flat (CPU ~22%, hit ratio ~96%, clients ~310). [F15]
- api-gateway has no upstream dependencies, so the surge enters at the edge from external traffic. [F16]
- Historical HIST-101 was a bcrypt bad deployment with the same symptoms; HIST-113 was capacity saturation from record volume. [F17, F18]

## Assumptions (not verified)
- The observed 4x request_rate increase reflects real inbound request volume (or an abusive flood), not a metric artifact.
- maxReplicas=8 is genuinely the effective ceiling and was not itself just changed (no config-change event observed).
- kyc-provider has no telemetry, so a hidden kyc-provider degradation cannot be fully excluded, though auth logs show only local pool saturation.

## Timeline
- `2026-04-22T05:50:00Z` Unrelated portfolio-service v4.4.0 (CSV export) deployed. [F12]
- `2026-04-22T06:30:00Z-06:45:00Z` api-gateway request_rate begins rising toward ~4900 req/s (4x). [F9]
- `2026-04-22T06:52:00Z` auth-service p99 jumps to ~2.8-4.1s, CPU to ~97%, error_rate to ~8.8-15%, request_rate to ~1234 req/s; rate limiting spikes. [F1, F2, F3, F4, F7]
- `2026-04-22T06:54:53Z` auth-service logs 'worker pool saturated (active=64/64, queue=39035)'. [F5]
- `2026-04-22T06:55:42Z` auth-service returns first 503: 'accept queue full ... (POST /v1/auth/login)'. [F5]
- `2026-04-22T06:57:15Z` Autoscaler cannot scale: 'desired replicas 14 exceeds maxReplicas 8'. [F6]
- `2026-04-22T07:06:00Z` Incident INC-008 reported by on-call SRE (reported service auth-service). []
- `2026-04-22T08:59:00Z` Symptoms persist (auth p99 still ~3.8s, error_rate ~14%). [F1, F2]

## Recommended actions
- **[immediate]** Raise auth-service maxReplicas (e.g. 8 -> 14+) or pre-scale it immediately to restore capacity; until then the surge cannot be absorbed. -- Autoscaler is blocked at maxReplicas 8 while desired is 14, so capacity cannot grow and 503s persist. [F6, F2]
- **[immediate]** Enable/tighten rate limiting and bot protection at api-gateway/edge for /v1/auth/login to shed or shape the abusive portion of the surge. -- The surge enters at the edge (gateway has no upstreams) and 900+/min requests are already being rate-limited; shaping at the edge protects auth-service. [F7, F16]
- **[immediate]** Do NOT roll back auth-service for a bcrypt-style regression — no deploy or config change occurred; verify before any rollback. -- No auth-service deployment/config change exists and memory is flat, so a rollback would not address the cause. [F12, F8, F13]
- **[short_term]** Capture traffic analytics (source IPs/ASN, endpoint mix, auth success/fail ratio) to determine whether the surge is legitimate demand or a credential-stuffing/brute-force flood. -- Distinguishing demand spike from attack determines the durable fix (capacity plan vs. abuse mitigation). [F7, F9]
- **[long_term]** Review autoscaler maxReplicas headroom and add pre-scale policy for known peak/attack windows. -- Low maxReplicas headroom is the binding capacity ceiling that turned a load surge into an outage; precedent HIST-113 used the same remedy. [F6, F18]

## Data gaps
- Historical incident search initially failed (503) and later returned limited matches; broader history could not be fully reviewed.
- No per-source-IP / client attribution data was available, so it cannot be determined whether the 4x traffic surge is legitimate demand or an abusive brute-force/credential-stuffing flood.
- kyc-provider is an external dependency with no telemetry (has_telemetry=false), so a hidden kyc-provider degradation cannot be fully excluded.
- get_deployments and one metrics call timed out transiently (tc-017, tc-010, tc-032); the deployment list was re-confirmed empty of auth-service changes via tc-020/tc-031.
- No direct evidence (config export) of the actual autoscaler maxReplicas setting or when it was last changed.

## Investigation stats
- latency 78.49s, LLM calls 9, tokens 146583 (in 135347 / out 11236)
- tool calls 37 (5 failed, 0 partial, 9 recovered by retry), findings 20/20 verified
