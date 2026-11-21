# Incident investigation report: INC-007
*System: single_agent | outcome: **inconclusive** | ended: agent_submitted*

## Summary
auth-service began returning 401 invalid_token for ~36% of token validations starting ~14:00-14:02Z on 2026-04-15. Two changes landed within 2 minutes of each other and of the onset: deployment dep-5710 (auth-service v7.3.0, jjwt 0.11->0.12 breaking parser API, TokenValidator rewritten, at 14:00:00Z) and config change cfg-2705 (jwt.signing.key_id rotated k-2025-10 -> k-2026-04, at 14:01:00Z). All infrastructure signals are clean (auth-service CPU ~38%, memory ~52%, no restarts, latency flat ~90ms; redis-cache and user-db healthy), ruling out resource/infrastructure/dependency causes. Auth-service's own logs are unsearchable (log-pipeline outage), so the decisive discriminator between the two code/config candidates is unavailable. Both historical analogues exist (HIST-108 key-rotation/partial JWKS; HIST-109 JWT library upgrade). The evidence cannot separate the deployment from the key rotation, so the outcome is inconclusive.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: JWT library upgrade deploy (dep-5710) broke token validation | bad_deployment | auth-service | dep-5710 | 0.40 (low) | active |
| 2 | H2: JWT signing-key rotation (cfg-2705) caused tokens to fail validation | config_change | auth-service | cfg-2705 | 0.35 (low) | active |
| 3 | H3: Resource exhaustion / infrastructure failure in auth-service or its datastores | resource_exhaustion | auth-service | - | 0.03 (low) | rejected |
| 4 | H4: Capacity saturation from traffic surge | capacity_saturation | auth-service | - | 0.02 (low) | rejected |

### H1: JWT library upgrade deploy (dep-5710) broke token validation
auth-service v7.3.0 (dep-5710, 14:00:00Z) replaced jjwt 0.11 with 0.12 (breaking parser API) and rewrote TokenValidator; the new parser path rejects previously valid tokens, producing 401 invalid_token for a stable ~36% of /token/validate calls. Mirrors HIST-109 (library upgrade changed clock-skew default).

**Causal chain**
1. dep-5710 deployed auth-service v7.3.0 at 14:00:00Z with 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten' [F3]
2. New validator rejects a subset of tokens -> 401 invalid_token from 14:02:22Z [F2]
3. auth-service error_rate jumps to ~36% at 14:00Z bucket [F1]

**Supporting evidence**
  - `F1` ✔ auth-service error_rate (HTTP 5xx %) was ~0.2% through 13:45Z, then jumped to 31.81% in the 14:00Z bucket and held ~36% through the end of data.
  - `F2` ✔ api-gateway WARN logs show 466 occurrences of the pattern 'auth-service POST /v<n>/token/validate -> <n> invalid_token', first seen 2026-04-15T14:02:22Z.
  - `F3` ✔ Deployment dep-5710 changed auth-service to v7.3.0 at 14:00:00Z, described as 'Upgrade JWT library to 4.x; refactor token validation' with changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; it is live and not rolled back.
  - `F11` ✔ Historical incidents include both a key-rotation 401 case (HIST-108: 'Key rotation published new key id before all pods loaded the JWKS') and a JWT-library-upgrade 401 case (HIST-109: JWT library upgrade changed clock-skew default) — i.e., both current candidates have precedent.
**Contradicting evidence**
  - `F6` ✔ auth-service request_rate is unchanged across the onset (~400 req/s), so the 401 spike is not load-driven; rate_limited_per_min is also flat at ~2.
**Assumptions**
  - The rewritten parser/clock-skew default is actually stricter and rejects real live tokens
  - dep-5710 pods serve the failing fraction while others serve fine (or a specific token class is rejected)

### H2: JWT signing-key rotation (cfg-2705) caused tokens to fail validation
Automatic signing-key rotation at 14:01:00Z changed jwt.signing.key_id from k-2025-10 to k-2026-04 before all auth-service pods loaded the new key material in their JWKS, so tokens signed/validated against the stale key are rejected, yielding 401 invalid_token for a subset of requests. Mirrors HIST-108.

**Causal chain**
1. cfg-2705 rotated jwt.signing.key_id k-2025-10 -> k-2026-04 at 14:01:00Z [F4]
2. Pods with stale/absent new key reject tokens -> 401 invalid_token from 14:02:22Z [F2]
3. auth-service error_rate jumps to ~36% at 14:00Z bucket [F1]

**Supporting evidence**
  - `F1` ✔ auth-service error_rate (HTTP 5xx %) was ~0.2% through 13:45Z, then jumped to 31.81% in the 14:00Z bucket and held ~36% through the end of data.
  - `F2` ✔ api-gateway WARN logs show 466 occurrences of the pattern 'auth-service POST /v<n>/token/validate -> <n> invalid_token', first seen 2026-04-15T14:02:22Z.
  - `F4` ✔ A single config change cfg-2705 rotated auth-service jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01:00Z ('Scheduled signing-key rotation').
  - `F11` ✔ Historical incidents include both a key-rotation 401 case (HIST-108: 'Key rotation published new key id before all pods loaded the JWKS') and a JWT-library-upgrade 401 case (HIST-109: JWT library upgrade changed clock-skew default) — i.e., both current candidates have precedent.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Key rotation is not announced to validation pods before the key_id switch
  - The ~36% fraction reflects a pod/key-pool split rather than a token class

### H3: Resource exhaustion / infrastructure failure in auth-service or its datastores
auth-service 401 spike caused by CPU/memory/connection exhaustion, pod restarts, or redis/user-db degradation.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F5` ✔ auth-service resource metrics are flat across the onset: cpu_pct ~38%, memory_pct ~52%, latency_p99_ms ~90ms, and restarts constant 0 — no resource or restart anomaly.
  - `F7` ✔ Downstream shared components are healthy: redis-cache hit_ratio_pct ~96% and memory_pct ~69%; user-db slow_queries_per_min ~0.3 — no dependency degradation coinciding with onset.
  - `F8` ✔ api-gateway error_rate tracks auth-service: ~0.2% through 13:48Z, then ~21% from the 14:00Z bucket onward, with gateway latency_p99 unchanged (~210ms).
**Rejected because:** auth-service cpu_pct ~38%, memory_pct ~52%, restarts=0, latency flat ~90ms; redis hit_ratio ~96%, memory ~69%; user-db slow_queries ~0.3 — all flat across onset (F5, F7).

### H4: Capacity saturation from traffic surge
401 spike caused by request-rate growth outpacing auth-service capacity.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ auth-service request_rate is unchanged across the onset (~400 req/s), so the 401 spike is not load-driven; rate_limited_per_min is also flat at ~2.
**Rejected because:** request_rate is flat at ~400 req/s and rate_limited_per_min ~2 across onset (F6); latency unchanged.

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- The 401s originate in auth-service token validation (the gateway WARN message names auth-service POST /token/validate returning invalid_token).
- No third change (e.g., a secret/token expiry or JWKS distribution job) exists outside the deployments and config-changes tools I queried for the 06:00-15:59Z window.
- The uniform ~36% failure fraction can be produced by either a token-class rejection or a partial pod/key-pool split; I could not verify which.
- reduce: the metric 'error_rate' buckets are 900s wide, so onset is bounded to within the 14:00:00Z bucket, not to the minute.

## Timeline
- `2026-04-15T13:45:00Z` Last healthy auth-service bucket: error_rate 0.23% [F1]
- `2026-04-15T14:00:00Z` dep-5710 deploys auth-service v7.3.0 (jjwt 0.11->0.12 breaking parser API; TokenValidator rewritten) [F3]
- `2026-04-15T14:00:00Z` auth-service error_rate bucket jumps to 31.81%; api-gateway error_rate to 17.67% [F1, F8]
- `2026-04-15T14:01:00Z` cfg-2705 rotates jwt.signing.key_id k-2025-10 -> k-2026-04 [F4]
- `2026-04-15T14:02:22Z` First api-gateway WARN 'auth-service POST /v1/token/validate -> 401 invalid_token' [F2]
- `2026-04-15T14:12:00Z` Incident reported (SEV-1); auth-service logs unavailable due to pipeline outage [F9]
- `2026-04-15T15:59:00Z` Error rate persists ~36% with no rollback of dep-5710 [F1]

## Recommended actions
- **[immediate]** Roll back dep-5710 (auth-service to pre-v7.3.0) as the fastest reversible test; if 401s stop within one deploy window, H1 is confirmed. -- Deploy is the top-ranked candidate and is cleanly reversible; it is the single most informative action. [F1, F3]
- **[immediate]** If rollback does not restore service, re-publish the previous key (k-2025-10) / verify JWKS propagation across all auth-service pods for cfg-2705. -- Key rotation is the equally-plausible alternative (H2) and mirrors HIST-108; staggered key distribution would leave a stable failing fraction. [F4, F11]
- **[immediate]** Restore/reroute the identity-namespace log pipeline so auth-service token-validation error detail (signature vs parse/format vs expired) is searchable. -- The validator's own error message is the decisive discriminator between a parser bug and a key mismatch and is currently unavailable. [F9]
- **[short_term]** Compare the set of failing vs succeeding tokens (issuing pod/key id, iat/exp, algorithm) against the new parser and key pool to isolate which change rejects them. -- Distinguishes a token-class rejection (parser/H1) from a pod/key-pool split (rotation/H2). [F2, F3, F4]
- **[long_term]** Decouple future JWT library upgrades and signing-key rotations from the same maintenance window, and gate key rotation on confirmed JWKS propagation. -- Both candidate triggers landed within 2 minutes, making attribution and rollback ambiguous. [F3, F4]

## Data gaps
- auth-service validator logs are unsearchable (log-pipeline outage): the exact rejection reason (signature failure vs parser/format error vs expiry/clock-skew) — the single best discriminator between H1 and H2 — is unavailable.
- No per-pod / per-instance breakdown of auth-service error_rate, so a pod-level split (key-pool only loaded on some pods) cannot be confirmed or excluded.
- No data on JWKS key-distribution timing or which key_id each pod currently trusts.
- No visibility into whether the failing 36% correlates with tokens issued before 14:00 vs after, nor with a specific signing key/algorithm.
- Cannot confirm whether dep-5710 and cfg-2705 were intended to ship together (change-management/CI context), which would explain the 1-minute offset.

## Investigation stats
- latency 50.94s, LLM calls 6, tokens 75201 (in 66634 / out 8567)
- tool calls 25 (5 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
