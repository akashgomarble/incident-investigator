# Incident investigation report: INC-007
*System: single_agent | outcome: **inconclusive** | ended: agent_submitted*

## Summary
Onset is sharp: auth-service error_rate goes from ~0.2% to ~37% at the 14:02 bucket and holds a stable ~36% plateau through 15:59 with no recovery; the gateway shows only 'auth-service POST /v1/token/validate -> 401 invalid_token' (first 14:02:22Z). Two near-simultaneous changes hit auth-service: deploy dep-5710 v7.3.0 at 14:00Z ('Upgrade JWT library to 4.x', 'jjwt 0.11 -> 0.12 (breaking parser API)', 'TokenValidator rewritten') and config change cfg-2705 rotating jwt.signing.key_id at 14:01Z. Resource, capacity and dependency causes are ruled out (flat CPU/memory, 0 restarts, steady request_rate, normal user-db). However, because the deploy and the key rotation are within ~1 minute of each other, both timestamp-match the 14:02 onset, and the identity-namespace log pipeline is down (auth-service logs return 503) so the failing validation code path cannot be inspected, I cannot cleanly separate a code regression (bad_deployment, HIST-109-like) from a JWKS/key-rotation propagation fault (config_change, HIST-108-like). The stable non-recovering plateau slightly favors a code regression, so H1 (bad_deployment) is ranked first but confidence is only 0.55; outcome is inconclusive.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: auth-service token validation regression from the v7.3.0 JWT library upgrade | bad_deployment | auth-service | dep-5710 | 0.55 (medium) | active |
| 2 | H2: auth-service signing-key rotation broke validation (JWKS not fully propagated) | config_change | auth-service | cfg-2705 | 0.30 (low) | active |
| 3 | H3: Resource exhaustion / capacity saturation on auth-service | resource_exhaustion | auth-service | - | 0.03 (low) | rejected |
| 4 | H4: Dependency/third-party failure (user-db, redis-cache, kyc-provider) | dependency_failure | user-db | - | 0.02 (low) | rejected |

### H1: auth-service token validation regression from the v7.3.0 JWT library upgrade
Deploy dep-5710 (v7.3.0, 14:00Z) rewrote TokenValidator and upgraded jjwt 0.11 -> 0.12 ('breaking parser API'); the new parser rejects a large fraction (~36%) of otherwise-valid tokens, returning 401 invalid_token on /v1/token/validate. This is a code fault originating in auth-service.

**Causal chain**
1. dep-5710 deploys at 14:00Z and its rollout completes ~14:01-14:02 [F2]
2. rewritten TokenValidator / breaking jjwt parser API rejects a stable fraction of tokens [F2]
3. auth-service error_rate jumps to ~37% at the 14:02 bucket and stays there [F1]
4. 401 invalid_token surfaces at the gateway token-validate path [F4]

**Supporting evidence**
  - `F1` ✔ auth-service error_rate was ~0.2% until 14:00, then jumped to ~37% at the 14:02 bucket and held a stable ~36% plateau through 15:59 (no recovery).
  - `F2` ✔ A code deployment dep-5710 to auth-service v7.3.0 occurred at 14:00:00Z summarizing 'Upgrade JWT library to 4.x; refactor token validation' with changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; it remains live (not rolled back).
  - `F4` ✔ api-gateway logs show auth-service token validation failures only: pattern 'auth-service POST /v1/token/validate -> 401 invalid_token', first seen 14:02:22Z, 466 matches through 15:58:51Z.
  - `F7` ✔ api-gateway error_rate rose from ~0.2% to ~21% at the 14:00 bucket in step with auth-service, consistent with propagated 401s from the token-validation path.
  - `F9` ✔ Historical incident HIST-109 (bad_deployment) was a 'Token validation regression after auth library upgrade' from a JWT-library change, matching the dep-5710 summary; HIST-108 (config_change) was '401s after JWT signing key rotation' with 'key id before all pods loaded the JWKS', matching cfg-2705.
  - `F10` ✔ The 36% failure is a stable, non-recovering plateau lasting ~2 hours rather than a transient spike that decays after pod/JWKS reload.
**Contradicting evidence**
  - `F3` ✔ A config change cfg-2705 rotated auth-service's jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01:00Z, described as a scheduled signing-key rotation.
**Assumptions**
  - The deploy rollout finished before the 14:02 onset bucket.
  - cfg-2705 alone would not produce a stable 36% non-recovering rate.

### H2: auth-service signing-key rotation broke validation (JWKS not fully propagated)
Config change cfg-2705 (14:01Z) rotated jwt.signing.key_id to k-2026-04 before all auth-service pods loaded the new JWKS, so a subset of token validations fail; this mirrors historical incident HIST-108. If so the fault originates in the config change to auth-service.

**Causal chain**
1. cfg-2705 rotates signing key_id at 14:01Z [F3]
2. pods that have not loaded the new JWKS reject tokens (signature/key-id mismatch) [F9]
3. auth-service error_rate jumps to ~37% at the 14:02 bucket [F1]
4. 401 invalid_token surfaces at the gateway token-validate path [F4]

**Supporting evidence**
  - `F1` ✔ auth-service error_rate was ~0.2% until 14:00, then jumped to ~37% at the 14:02 bucket and held a stable ~36% plateau through 15:59 (no recovery).
  - `F3` ✔ A config change cfg-2705 rotated auth-service's jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01:00Z, described as a scheduled signing-key rotation.
  - `F4` ✔ api-gateway logs show auth-service token validation failures only: pattern 'auth-service POST /v1/token/validate -> 401 invalid_token', first seen 14:02:22Z, 466 matches through 15:58:51Z.
  - `F7` ✔ api-gateway error_rate rose from ~0.2% to ~21% at the 14:00 bucket in step with auth-service, consistent with propagated 401s from the token-validation path.
  - `F9` ✔ Historical incident HIST-109 (bad_deployment) was a 'Token validation regression after auth library upgrade' from a JWT-library change, matching the dep-5710 summary; HIST-108 (config_change) was '401s after JWT signing key rotation' with 'key id before all pods loaded the JWKS', matching cfg-2705.
**Contradicting evidence**
  - `F10` ✔ The 36% failure is a stable, non-recovering plateau lasting ~2 hours rather than a transient spike that decays after pod/JWKS reload.
**Assumptions**
  - A key rotation failure can persist ~2h without recovering (JWKS never fully reloads).
  - The rotation is the dominant change despite preceding the deploy in description.

### H3: Resource exhaustion / capacity saturation on auth-service
auth-service ran out of CPU, memory, restarts or connection capacity, causing validation failures.

**Causal chain**
1. auth-service CPU/memory would rise or pods restart at onset [F5]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F5` ✔ auth-service had zero restarts across 12:00-15:59, and CPU (~38%) and memory (~52%) were flat with no step change at onset.
  - `F6` ✔ Dependency health was normal: user-db connections_active stayed ~25 across the window and auth-service request_rate stayed ~400 req/s with no downstream failure signals.
**Rejected because:** CPU (~38%), memory (~52%) and restarts (0) are flat across onset; request_rate and user-db connections normal — no resource or capacity signature.

### H4: Dependency/third-party failure (user-db, redis-cache, kyc-provider)
A downstream datastore or external provider degraded, causing auth-service validation errors.

**Causal chain**
1. downstream would show errors/latency [F6]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ Dependency health was normal: user-db connections_active stayed ~25 across the window and auth-service request_rate stayed ~400 req/s with no downstream failure signals.
**Rejected because:** user-db connections normal and auth-service request_rate steady; errors are specifically 401 invalid_token (an auth decision, not a downstream error).

## Facts (verified observations)
- auth-service error_rate rose from ~0.2% to ~37% at the 14:02 bucket and stayed at ~36% through 15:59 (no recovery). [F1]
- Deploy dep-5710 (auth-service v7.3.0) shipped at 14:00:00Z upgrading jjwt 0.11 -> 0.12 (breaking parser API) and rewriting TokenValidator; still live. [F2]
- Config change cfg-2705 rotated auth-service jwt.signing.key_id k-2025-10 -> k-2026-04 at 14:01:00Z. [F3]
- Gateway logs show only token-validation 401s: first 14:02:22Z, 466 matches through 15:58:51Z. [F4]
- auth-service had 0 restarts and flat CPU (~38%)/memory (~52%) across onset. [F5]
- user-db connections_active steady ~25 and auth-service request_rate steady ~400 req/s — no downstream failure. [F6]
- api-gateway error_rate rose in step with auth-service at 14:00. [F7]
- auth-service logs are unsearchable (503) due to the identity-namespace log pipeline outage. [F8]

## Assumptions (not verified)
- The 14:02 error-rate bucket onset reflects the deploy rollout completing, not a delayed effect of the 14:01 config change; both remain plausible.
- cfg-2705 and dep-5710 are the only relevant changes in the window (verified: get_deployments returned 2 total, only one on auth-service; get_config_changes returned 1).
- No other auth-service telemetry (traces) is available to attribute the exact failing check.

## Timeline
- `2026-04-15T14:00:00Z` Deploy dep-5710 (auth-service v7.3.0, JWT library 0.11->0.12, TokenValidator rewritten) begins rollout. [F2]
- `2026-04-15T14:01:00Z` Config change cfg-2705 rotates jwt.signing.key_id to k-2026-04. [F3]
- `2026-04-15T14:02:00Z` auth-service error_rate jumps from ~0.2% to 37.21%. [F1]
- `2026-04-15T14:02:22Z` First gateway log: auth-service POST /v1/token/validate -> 401 invalid_token. [F4]
- `2026-04-15T14:12:00Z` Incident INC-007 reported (users logged out / 401s). []
- `2026-04-15T15:59:00Z` Error rate still ~36%; 466 token-validation 401s logged; no recovery. [F1, F4]

## Recommended actions
- **[immediate]** Roll back deploy dep-5710 (auth-service v7.3.0) to the prior version and observe whether error_rate returns to baseline; if it does not, immediately revert cfg-2705. -- Two candidate triggers are within a minute; a rollback is the fastest test and the historical resolution for the library-upgrade class (HIST-109). [F2, F1]
- **[immediate]** If rolling back the deploy does not recover, re-publish the previous signing key / complete a staged JWKS reload as in HIST-108. -- Distinguishes the config_change hypothesis; key-rotation breakage is fixed by restoring key propagation. [F3, F9]
- **[short_term]** Restore auth-service (identity namespace) log ingestion so the specific validation failure (signature vs key-id vs clock-skew) is visible. -- The log-pipeline outage is the single biggest blocker to root-causing this incident. [F8]
- **[long_term]** Stop shipping JWT-library upgrades and signing-key rotations to auth-service simultaneously; stagger and gate them. -- Simultaneous changes made root cause ambiguous and rollback non-diagnostic. [F2, F3]

## Data gaps
- auth-service logs are unsearchable (identity-namespace pipeline down, 503), so the exact validation error (signature mismatch vs unknown key id vs parser exception) is unknown.
- redis-cache metrics could not be retrieved (503 / no error_rate metric), so its health is unconfirmed.
- auth-service latency_p99_ms timed out repeatedly — no latency data to compare against the historical failures.
- No trace-level data linking the 401s to a specific code path or key id.
- Deploy rollout completion time (per-pod) is not available, so the deploy vs config-change onset ordering cannot be resolved exactly.

## Investigation stats
- latency 75.75s, LLM calls 8, tokens 97880 (in 88891 / out 8989)
- tool calls 28 (7 failed, 0 partial, 7 recovered by retry), findings 10/10 verified
