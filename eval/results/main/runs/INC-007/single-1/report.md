# Incident investigation report: INC-007
*System: single_agent | outcome: **inconclusive** | ended: agent_submitted*

## Summary
Onset at 14:00 on 2026-04-15: auth-service error_rate jumped from ~0.2% to ~36% and held flat through 15:59; api-gateway logged 466 'auth-service POST /v1/token/validate -> 401 invalid_token' WARNs and its own error_rate rose to ~21%. Two changes coincide at onset — deploy dep-5710 (jjwt 0.11->0.12 breaking parser API, TokenValidator rewritten) at 14:00 and config cfg-2705 (jwt.signing.key_id rotation) at 14:01. Auth-service resource/latency metrics are flat and its dependencies (redis ~96% hit, user-db ~25 conns) are healthy, ruling out resource/dependency causes. The failure is a steady deterministic ~36% partial rather than a transient rollout burst, which favors a persistent validator regression from dep-5710 over a key-rotation JWKS-load race (HIST-109 vs HIST-108 analogues). However, auth-service logs are unsearchable (confirmed pipeline 503), so the actual validation failure reason and any JWKS/key-id evidence cannot be confirmed; dep-5710 vs cfg-2705 are not separable from available telemetry. Outcome: inconclusive, leaning bad_deployment (dep-5710).

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: auth-service deploy dep-5710 (JWT lib 4.x + rewritten TokenValidator) rejects a subset of valid tokens | bad_deployment | auth-service | dep-5710 | 0.50 (medium) | active |
| 2 | H2: Scheduled signing-key rotation cfg-2705 republished new key id before all validators loaded JWKS | config_change | auth-service | cfg-2705 | 0.35 (low) | active |
| 3 | H3: Dependency or resource failure (redis/user-db/external) | dependency_failure | redis-cache | - | 0.05 (low) | rejected |

### H1: auth-service deploy dep-5710 (JWT lib 4.x + rewritten TokenValidator) rejects a subset of valid tokens
Deploy dep-5710 at 14:00 upgraded jjwt 0.11->0.12 (breaking parser API) and rewrote TokenValidator. The rewritten validation path deterministically rejects ~36% of otherwise-valid tokens (e.g., changed claim/clock-skew/alg-handling default), producing sustained 401 invalid_token on /token/validate. Fault originates in the auth-service code release.

**Causal chain**
1. dep-5710 deployed at 14:00 rewrites TokenValidator with jjwt 0.12 breaking parser API [F2]
2. Rewritten validator rejects a subset of valid tokens -> 401 invalid_token [F3]
3. auth-service error_rate rises to ~36% and stays flat [F1, F10]
4. api-gateway surfaces 401s to users (logged out) [F4]

**Supporting evidence**
  - `F1` ✔ auth-service error_rate jumped from ~0.2% to 31.81% in the 14:00 bucket and stayed elevated (~36%) through 15:59, with pre-14:00 baseline ~0.2%.
  - `F2` ✔ Two changes hit auth-service at onset: deploy dep-5710 (v7.3.0) at 14:00 'Upgrade JWT library to 4.x; refactor token validation' with 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; and config cfg-2705 at 14:01 changing jwt.signing.key_id k-2025-10 -> k-2026-04 (scheduled signing-key rotation).
  - `F3` ✔ api-gateway logs show 466 WARN entries of the single pattern 'auth-service POST /v1/token/validate -> 401 invalid_token', first seen 14:02:22Z, continuing through 15:58:51Z; no ERROR-level gateway logs exist.
  - `F5` ✔ auth-service resource metrics are unchanged across the incident: cpu_pct ~38%, memory_pct ~52%, restarts 0 for the whole window, request_rate flat ~400 req/s.
  - `F6` ✔ auth-service latency_p99_ms is flat (~90ms) across the entire incident window; no latency degradation accompanies the 401s.
  - `F9` ✔ Historical incidents include HIST-108 (2025-10-01, 401s after JWT signing key rotation, category config_change, cause: new key id published before all pods loaded JWKS) and HIST-109 (2025-07-11, token validation regression after auth library upgrade, category bad_deployment, cause: JWT library upgrade changed clock-skew default).
  - `F10` ✔ Inference: the failure is a deterministic partial (steady ~36%, flat for ~2h) rather than a transient rollout-window burst, which matches a persistent validator regression more than a key-rotation JWKS-load race (which would resolve as pods load the new keys).
**Contradicting evidence**
  - `F2` ✔ Two changes hit auth-service at onset: deploy dep-5710 (v7.3.0) at 14:00 'Upgrade JWT library to 4.x; refactor token validation' with 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; and config cfg-2705 at 14:01 changing jwt.signing.key_id k-2025-10 -> k-2026-04 (scheduled signing-key rotation).
**Assumptions**
  - The steady ~36% rate reflects a deterministic validation-code regression rather than a key-distribution race.
  - No rollback of dep-5710 was attempted, so recovery is unverified.

### H2: Scheduled signing-key rotation cfg-2705 republished new key id before all validators loaded JWKS
Config change cfg-2705 at 14:01 rotated jwt.signing.key_id k-2025-10 -> k-2026-04. Validator pods not yet loading the new JWKS reject tokens, causing 401 invalid_token. Matches HIST-108's known failure mode.

**Causal chain**
1. cfg-2705 rotates signing key id at 14:01 [F2]
2. Validators lack/refuse the new key -> 401 invalid_token [F3]
3. Sustained errors surface at gateway [F4]

**Supporting evidence**
  - `F2` ✔ Two changes hit auth-service at onset: deploy dep-5710 (v7.3.0) at 14:00 'Upgrade JWT library to 4.x; refactor token validation' with 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; and config cfg-2705 at 14:01 changing jwt.signing.key_id k-2025-10 -> k-2026-04 (scheduled signing-key rotation).
  - `F3` ✔ api-gateway logs show 466 WARN entries of the single pattern 'auth-service POST /v1/token/validate -> 401 invalid_token', first seen 14:02:22Z, continuing through 15:58:51Z; no ERROR-level gateway logs exist.
  - `F9` ✔ Historical incidents include HIST-108 (2025-10-01, 401s after JWT signing key rotation, category config_change, cause: new key id published before all pods loaded JWKS) and HIST-109 (2025-07-11, token validation regression after auth library upgrade, category bad_deployment, cause: JWT library upgrade changed clock-skew default).
**Contradicting evidence**
  - `F1` ✔ auth-service error_rate jumped from ~0.2% to 31.81% in the 14:00 bucket and stayed elevated (~36%) through 15:59, with pre-14:00 baseline ~0.2%.
  - `F10` ✔ Inference: the failure is a deterministic partial (steady ~36%, flat for ~2h) rather than a transient rollout-window burst, which matches a persistent validator regression more than a key-rotation JWKS-load race (which would resolve as pods load the new keys).
**Assumptions**
  - Old key was not retained alongside new key, so old-key tokens fail.
  - The steady 2h plateau could reflect a permanently unincluded key rather than a race.

### H3: Dependency or resource failure (redis/user-db/external)
The 401s are caused by a degraded shared datastore or external dependency of auth-service.

**Causal chain**
1. redis/user-db degrade *(assumed)*
2. auth-service validation fails *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F5` ✔ auth-service resource metrics are unchanged across the incident: cpu_pct ~38%, memory_pct ~52%, restarts 0 for the whole window, request_rate flat ~400 req/s.
  - `F6` ✔ auth-service latency_p99_ms is flat (~90ms) across the entire incident window; no latency degradation accompanies the 401s.
  - `F7` ✔ Auth-service dependencies are healthy throughout: redis-cache hit_ratio_pct ~96% flat, user-db connections_active ~25 flat. market-data-service error_rate flat ~0.2% (its deploy dep-5699 at 11:35 is not associated with the incident).
**Assumptions**
  - None — contradicted by healthy dependency telemetry.
**Rejected because:** redis hit_ratio ~96%, user-db connections ~25, auth CPU/memory/restarts flat; no dependency telemetry degrades at 14:00 (F5, F6, F7).

## Facts (verified observations)
- auth-service error_rate rose from ~0.2% pre-14:00 to 31.81% in the 14:00 bucket and held ~36% through 15:59. [F1]
- Deploy dep-5710 (auth-service v7.3.0) at 14:00 rewrote TokenValidator and upgraded jjwt 0.11->0.12 (breaking parser API); config cfg-2705 at 14:01 rotated jwt.signing.key_id. [F2]
- api-gateway logged 466 'auth-service POST /v1/token/validate -> 401 invalid_token' WARNs from 14:02:22Z through 15:58:51Z; no ERROR-level gateway logs. [F3]
- auth-service cpu ~38%, memory ~52%, restarts 0, request_rate ~400 req/s — all flat across the incident. [F5]
- auth-service latency_p99_ms flat ~90ms; redis hit_ratio ~96% and user-db connections ~25 all flat. [F6, F7]
- auth-service logs are unavailable (503) on every attempt; the identity-namespace log pipeline outage is confirmed. [F8]
- Historical analogues: HIST-108 (401s after signing-key rotation, config_change) and HIST-109 (validation regression after library upgrade, bad_deployment). [F9]

## Assumptions (not verified)
- The steady ~36% failure is a deterministic partial rejection, not a transient key-distribution race.
- No other change (e.g., a feature flag not captured here) contributed.
- Metric error_rate for auth-service reflects the 401s under investigation.

## Timeline
- `2026-04-15T11:35:00Z` dep-5699 market-data-service v5.0.3 deployed (unrelated; error_rate stays flat). [F7]
- `2026-04-15T13:45:00Z` Last normal bucket: auth-service error_rate ~0.23%. [F1]
- `2026-04-15T14:00:00Z` dep-5710 auth-service v7.3.0 deployed (JWT lib upgrade, TokenValidator rewritten); error_rate climbs to 31.81% in this bucket. [F1, F2]
- `2026-04-15T14:01:00Z` cfg-2705 rotates jwt.signing.key_id k-2025-10 -> k-2026-04. [F2]
- `2026-04-15T14:02:22Z` First api-gateway 401 invalid_token on /token/validate. [F3]
- `2026-04-15T14:12:00Z` Incident reported; error_rate steady ~36%. [F1]
- `2026-04-15T15:58:51Z` 401 invalid_token still occurring; no recovery observed. [F3]

## Recommended actions
- **[immediate]** Roll back dep-5710 (revert to v7.2.x) and observe whether the 401 rate returns to baseline. -- The deploy is the leading hypothesis and a rollback is the fastest discriminating test; no rollback has been attempted (dep-5710 status live, rolled_back_at null). [F1, F2]
- **[immediate]** If rollback of dep-5710 does not recover, re-publish the previous signing key (k-2025-10) and re-stage cfg-2705. -- cfg-2705 is the alternate hypothesis; re-publishing the prior key matches HIST-108's resolution. [F2, F9]
- **[short_term]** Restore the identity-namespace log pipeline and capture the exact validation failure reason from auth-service. -- Auth-service logs are the decisive evidence to separate a code regression from a key-availability failure and are currently unsearchable. [F8]
- **[short_term]** Diff deps for jjwt 0.12 / TokenValidator behavior (clock-skew, alg allowlist, claim handling) against the 0.11 baseline. -- HIST-109 shows a library upgrade can change a validation default; a deterministic subset failure fits this. [F2, F9]
- **[long_term]** Require dual-key (old+new) JWKS overlap and staged rotation with validator pre-load before cutover. -- Prevents recurrence of the HIST-108 rotation-race class of incident. [F9]

## Data gaps
- auth-service logs are unsearchable (confirmed 503) — the exact validation failure reason (key not found vs signature/claim invalid) is unknown.
- No JWKS/key-metadata or key-id telemetry is available to confirm whether the new signing key was distributed to validators.
- No evidence of any rollback of dep-5710 or cfg-2705, so neither hypothesis has been experimentally confirmed or excluded.
- Deploy/config timing (14:00 vs 14:01) is too close to separate the two triggers by onset time alone.

## Investigation stats
- latency 58.77s, LLM calls 8, tokens 102155 (in 93385 / out 8770)
- tool calls 30 (7 failed, 0 partial, 0 recovered by retry), findings 10/10 verified
