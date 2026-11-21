# Incident investigation report: INC-007
*System: single_agent | outcome: **inconclusive** | ended: agent_submitted*

## Summary
auth-service began returning 401 invalid_token on ~36% of POST /v1/token/validate calls at 14:02 UTC on 2026-04-15; api-gateway propagated ~21% 5xx upstream. Two changes landed 1-2 minutes earlier — deploy dep-5710 (v7.3.0, jjwt 0.11→0.12 breaking parser API, TokenValidator rewritten) at 14:00 and config change cfg-2705 (JWT signing-key rotation k-2025-10→k-2026-04) at 14:01. All resource/infra metrics are flat and neither change has been rolled back, so there is no rollback evidence to discriminate. Both the deploy and the key rotation match historical analogues (HIST-109 bad_deployment; HIST-108 config_change) and the ~1/3 partial failure rate is consistent with either. auth-service logs are unsearchable (log backend 503), removing the decisive evidence. Outcome: inconclusive between bad_deployment (JWT library upgrade) and config_change (signing-key rotation); the root cause is one of these two auth-service changes.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: JWT library upgrade (dep-5710 v7.3.0) introduced a token-validation bug | bad_deployment | auth-service | dep-5710 | 0.42 (low) | active |
| 2 | H2: JWT signing-key rotation (cfg-2705) invalidated tokens | config_change | auth-service | cfg-2705 | 0.40 (low) | active |
| 3 | H3: Resource exhaustion / capacity saturation | resource_exhaustion | auth-service | - | 0.03 (low) | rejected |
| 4 | H4: Dependency or infrastructure failure (redis-cache / user-db / kyc-provider) | dependency_failure | redis-cache | - | 0.03 (low) | rejected |

### H1: JWT library upgrade (dep-5710 v7.3.0) introduced a token-validation bug
The auth-service deploy dep-5710 at 14:00 replaced the token parser (jjwt 0.11 -> 0.12, 'breaking parser API') and rewrote TokenValidator, causing ~1/3 of valid tokens to be rejected as invalid_token starting 14:02. Exact analogue of HIST-109 (JWT library upgrade changed validation default -> 401 invalid_token).

**Causal chain**
1. dep-5710 deployed 14:00 with breaking JWT parser change [F3]
2. auth-service error_rate jumps 0.21% -> 37% at 14:02 [F1]
3. validation failures surface as 401 invalid_token at gateway [F5, F6]
4. resource metrics flat, so failure is in validation logic not resources [F7, F8]

**Supporting evidence**
  - `F1` ✔ auth-service error_rate was ~0.2% until 14:00 and jumped to 37.21% in the 14:02 bucket, last normal sample 14:00:00 = 0.21.
  - `F3` ✔ A code deployment dep-5710 for auth-service (v7.3.0, 'jjwt 0.11 -> 0.12 (breaking parser API)', 'TokenValidator rewritten') completed at 2026-04-15T14:00:00Z and is still live with rolled_back_at null.
  - `F5` ✔ api-gateway logged 466 WARN entries, all of the pattern 'auth-service POST /v1/token/validate -> 401 invalid_token', first seen 2026-04-15T14:02:22Z.
  - `F7` ✔ auth-service resource metrics are flat across the incident: cpu_pct ~36-42%, memory_pct ~46-58%, latency_p99_ms ~82-101ms, restarts 0 throughout.
  - `F12` ✔ Historical incident HIST-109 (2025-07-11) is 'Token validation regression after auth library upgrade' with symptoms '401 invalid_token','token validation failures', root cause 'JWT library upgrade changed clock-skew default', category bad_deployment.
**Contradicting evidence**
  - `F4` ✔ A config change cfg-2705 rotated auth-service jwt.signing.key_id from k-2025-10 to k-2026-04 at 2026-04-15T14:01:00Z (author security-automation, 'Scheduled signing-key rotation').
  - `F13` ✔ Historical incident HIST-108 (2025-10-01) is '401s after JWT signing key rotation' with symptoms '401 invalid_token','users logged out', root cause 'Key rotation published new key id before all pods loaded the JWKS', category config_change.
**Assumptions**
  - The log-pipeline outage (F11) hides the auth-service-side stack trace that would name the failing component
  - A 14:02 onset 1-2 min after a 14:00 deploy is consistent with rollout completion
  - Neither change was reverted (F17), so the absence of rollback recovery is not evidence against this hypothesis

### H2: JWT signing-key rotation (cfg-2705) invalidated tokens
The scheduled signing-key rotation cfg-2705 at 14:01 switched jwt.signing.key_id to k-2026-04; a subset of pods/tokens still on the old key id (k-2025-10) were rejected, producing ~1/3 invalid_token starting 14:02. Exact analogue of HIST-108 (key rotation before all pods loaded the new key).

**Causal chain**
1. cfg-2705 rotated signing key id 14:01 [F4]
2. error_rate jumps to ~36% at 14:02 [F1]
3. only a fraction of validations fail (~1/3), consistent with partial key propagation [F15, F5]
4. no resource/infra cause [F7, F9, F10]

**Supporting evidence**
  - `F1` ✔ auth-service error_rate was ~0.2% until 14:00 and jumped to 37.21% in the 14:02 bucket, last normal sample 14:00:00 = 0.21.
  - `F4` ✔ A config change cfg-2705 rotated auth-service jwt.signing.key_id from k-2025-10 to k-2026-04 at 2026-04-15T14:01:00Z (author security-automation, 'Scheduled signing-key rotation').
  - `F5` ✔ api-gateway logged 466 WARN entries, all of the pattern 'auth-service POST /v1/token/validate -> 401 invalid_token', first seen 2026-04-15T14:02:22Z.
  - `F13` ✔ Historical incident HIST-108 (2025-10-01) is '401s after JWT signing key rotation' with symptoms '401 invalid_token','users logged out', root cause 'Key rotation published new key id before all pods loaded the JWKS', category config_change.
  - `F15` ✔ Failure fraction is partial, not total: auth-service error rate plateaued near 36% (~1/3) rather than ~100%, and gateway near 21%.
**Contradicting evidence**
  - `F3` ✔ A code deployment dep-5710 for auth-service (v7.3.0, 'jjwt 0.11 -> 0.12 (breaking parser API)', 'TokenValidator rewritten') completed at 2026-04-15T14:00:00Z and is still live with rolled_back_at null.
  - `F12` ✔ Historical incident HIST-109 (2025-07-11) is 'Token validation regression after auth library upgrade' with symptoms '401 invalid_token','token validation failures', root cause 'JWT library upgrade changed clock-skew default', category bad_deployment.
**Assumptions**
  - Key rotation propagates via config push on a ~1-min cycle reaching 14:02
  - A stable ~1/3 failure fraction reflects a fixed subset of pods/tokens on the stale key
  - The rotation was not accompanied by a JWKS pre-publish step

### H3: Resource exhaustion / capacity saturation
auth-service ran out of CPU/memory/connections or was saturated by load, causing errors.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ auth-service resource metrics are flat across the incident: cpu_pct ~36-42%, memory_pct ~46-58%, latency_p99_ms ~82-101ms, restarts 0 throughout.
  - `F8` ✔ auth-service request_rate was steady ~358-435 req/s before and after onset (no traffic surge); rate_limited_per_min stayed ~2/min.
  - `F9` ✔ Shared dependency redis-cache stayed healthy: hit_ratio_pct ~94-98%, evictions_per_min ~1.5-5, memory_pct ~60-77%, with no step change at onset.
  - `F10` ✔ user-db connections_active stayed flat (~22-27) across the window, no connection exhaustion.
**Rejected because:** cpu_pct (~38%), memory_pct (~52%), latency_p99 (~90ms) and restarts (0) are all flat across onset (F7); request_rate is steady ~400 req/s with no surge (F8); redis hit_ratio ~96% and user-db connections flat (F9, F10).

### H4: Dependency or infrastructure failure (redis-cache / user-db / kyc-provider)
A downstream datastore or the external kyc-provider failed, causing token validation errors.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ Shared dependency redis-cache stayed healthy: hit_ratio_pct ~94-98%, evictions_per_min ~1.5-5, memory_pct ~60-77%, with no step change at onset.
  - `F10` ✔ user-db connections_active stayed flat (~22-27) across the window, no connection exhaustion.
  - `F5` ✔ api-gateway logged 466 WARN entries, all of the pattern 'auth-service POST /v1/token/validate -> 401 invalid_token', first seen 2026-04-15T14:02:22Z.
**Rejected because:** redis-cache hit_ratio stays ~96% with no eviction or memory step change (F9); user-db connections flat (F10); logs show 401 invalid_token (an auth decision), not upstream timeouts/5xx (F5).

## Facts (verified observations)
- auth-service error rate was ~0.2% through 14:00 then jumped to 37.21% at 14:02. [F1]
- The error rate stayed ~35-38% continuously to 15:59 with no recovery. [F2]
- Deploy dep-5710 (v7.3.0, jjwt 0.11->0.12 breaking parser API, TokenValidator rewritten) completed 14:00 and is not rolled back. [F3]
- Config change cfg-2705 rotated jwt.signing.key_id k-2025-10 -> k-2026-04 at 14:01. [F4]
- api-gateway logged 466 '401 invalid_token' WARNs starting 14:02:22. [F5]
- api-gateway error rate rose 0.24% -> 21% at the 14:02 bucket. [F6]
- auth-service CPU, memory, latency and restarts are flat across onset. [F7]
- auth-service request rate and rate-limiting are unchanged across onset. [F8]
- redis-cache hit ratio, evictions and memory show no step change. [F9]
- user-db active connections are flat. [F10]
- auth-service logs are unretrievable (log backend 503). [F11]
- Past incidents HIST-109 (JWT library upgrade) and HIST-108 (signing key rotation) both show 401 invalid_token on auth-service. [F12, F13]
- The failure is partial (~1/3), not total. [F15]

## Assumptions (not verified)
- The 14:02 onset (1-2 min after both changes) is equally consistent with deploy rollout completion and config propagation.
- A stable ~1/3 failure fraction reflects a partial subset (pods/tokens) rather than a random logic error.
- Historical incidents HIST-109/HIST-108 are plausible leads, not proof of the current cause.

## Timeline
- `2026-04-15T13:59:00Z` Last normal bucket: auth-service error_rate 0.21%, gateway 0.24% [F1, F6]
- `2026-04-15T14:00:00Z` Deploy dep-5710 (v7.3.0, JWT lib 0.11->0.12, TokenValidator rewritten) completes [F3]
- `2026-04-15T14:01:00Z` Config change cfg-2705 rotates JWT signing key id k-2025-10 -> k-2026-04 [F4]
- `2026-04-15T14:02:00Z` auth-service error_rate jumps to ~37%; gateway error_rate to ~21% and first 401 invalid_token logged at 14:02:22 [F1, F5, F6]
- `2026-04-15T14:12:00Z` Incident reported by on-call SRE (reported_at) []
- `2026-04-15T15:59:00Z` Incident still ongoing; error rate ~36-38%, no rollback performed [F2, F17]

## Recommended actions
- **[immediate]** Retrieve auth-service application logs from the identity namespace once the log pipeline is restored (or via a pod-level tail) to see the exact validation failure reason. -- This is the single piece of missing evidence that would separate the deploy from the key rotation (stack trace, key id used, clock-skew error). [F11]
- **[immediate]** Roll back deploy dep-5710 to the previous version first (fastest, reversible) while leaving the key rotation in place; if error rate recovers, the deploy is the cause. -- dep-5710 is a higher-confidence analogue (HIST-109 same symptom+service) and is a cleanly reversible lever; a controlled rollback is itself a discriminating experiment. [F3, F12, F17]
- **[immediate]** If rollback of the deploy does NOT recover, revert cfg-2705 (restore key id k-2025-10) or pre-publish the new JWKS to all pods and force a config reload. -- Tests the key-rotation hypothesis; HIST-108 shows the fix is republishing the previous key and staging rotations. [F4, F13]
- **[short_term]** Add auth-service-side metrics/log labels for validation failure reason (bad signature vs expired vs untrusted issuer/skew) and key id in use. -- Current telemetry aggregates all invalid_token failures, preventing discrimination between key and parser causes. [F5]
- **[long_term]** Stage JWT signing-key rotations (pre-publish JWKS, drain old key) and decouple them from library upgrades; add a canary stage to auth-service deploys. -- Both trigger types have previously caused 401 outages (HIST-108, HIST-109); staggering changes removes this ambiguity. [F12, F13]

## Data gaps
- auth-service application logs are unavailable (log backend returned 503, tc-021) — the namespace log-pipeline outage removes the decisive evidence (stack trace, key id, clock-skew vs signature error).
- No rollback of either dep-5710 or cfg-2705 has occurred and the incident is still ongoing, so there is no rollback-recovery observation to attribute the fault.
- No per-pod / per-replica breakdown of auth-service error rate is available, so a partial-fleet (stale-key or mixed-version) pattern cannot be confirmed vs a uniform logic bug.
- api-gateway logs contain no key/JWKS-related lines (0 matches), so the key-id path is not directly observable.
- kyc-provider has no telemetry, so its health could not be verified (though logs show 401 invalid_token, not provider timeouts).

## Investigation stats
- latency 62.78s, LLM calls 8, tokens 136637 (in 125986 / out 10651)
- tool calls 35 (3 failed, 0 partial, 0 recovered by retry), findings 17/17 verified
