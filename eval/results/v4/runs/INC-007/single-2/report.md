# Incident investigation report: INC-007
*System: single_agent | outcome: **inconclusive** | ended: agent_submitted*

## Summary
401 invalid_token failures on auth-service POST /v1/token/validate began sharply at ~14:02 (auth-service error_rate 0.16% at 14:01 -> 36.08% at 14:02; api-gateway 5xx 0.2% -> ~21%; api-gateway first invalid_token log 14:02:22Z). The failure is partial and steady (~36%) and confined to token validation; auth-service resources, latency, restarts and its datastore/cache dependencies are all healthy. Two auth-service changes landed in the 14:00-14:01 minute interval, immediately before onset: deploy dep-5710 v7.3.0 upgrading jjwt 0.11->0.12 with a 'breaking parser API' and a rewritten TokenValidator (config_change-adjacent), and config change cfg-2705 rotating jwt.signing.key_id k-2025-10 -> k-2026-04. Both remain live (no rollback). The auth-service logs that would show the actual rejection reason are unsearchable (log pipeline down; query_logs 503). Because the two candidate triggers are co-timed within ~60s and the internal error text is unavailable, I cannot mechanically separate a config_change (key rotation, exact historical match HIST-108) from a bad_deployment (JWT library upgrade, HIST-109). Outcome is inconclusive; H1 (config_change) and H2 (bad_deployment) are near-equal, with H1 slightly favored by the exact historical symptom match and the partial-failure/old-token-cohort shape.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: JWT signing-key rotation (cfg-2705) caused 401 invalid_token | config_change | auth-service | cfg-2705 | 0.40 (low) | active |
| 2 | H2: auth-service v7.3.0 JWT library upgrade (dep-5710) broke token validation | bad_deployment | auth-service | dep-5710 | 0.40 (low) | active |
| 3 | H3: Capacity/resource or dependency fault at auth-service | resource_exhaustion | - | - | 0.05 (low) | rejected |

### H1: JWT signing-key rotation (cfg-2705) caused 401 invalid_token
The scheduled jwt.signing.key_id rotation at 14:01 (k-2025-10 -> k-2026-04) made auth-service reject tokens signed under the previous key id (or pods that had not loaded the new JWKS), producing a steady ~36% invalid_token rate on POST /v1/token/validate from 14:02 onward.

**Causal chain**
1. cfg-2705 rotates signing key id k-2025-10 -> k-2026-04 at 14:01 [F4]
2. tokens bearing the old kid / pods with stale JWKS fail validation *(assumed)*
3. auth-service returns 401 invalid_token for ~36% of validations from 14:02 [F1, F2]

**Supporting evidence**
  - `F1` ✔ auth-service error_rate (unit '% of requests') was flat ~0.2% through 14:01 and jumped to 36.08% at the 14:02 minute bucket, staying ~34-38% thereafter.
  - `F2` ✔ api-gateway began logging 'auth-service POST /v1/token/validate -> 401 invalid_token' at 14:02:22Z; 466 such WARN entries by 15:58 and this is the ONLY warning pattern present.
  - `F4` ✔ auth-service config change cfg-2705 (jwt.signing.key_id old k-2025-10 -> new k-2026-04, 'Scheduled signing-key rotation' by security-automation) occurred at 2026-04-15T14:01:00Z.
  - `F8` ✔ Historical incident HIST-108 (2025-10-01) is an exact symptom match: '401s after JWT signing key rotation', root cause 'Key rotation published new key id before all pods loaded the JWKS', category config_change.
  - `F10` ✔ The failure rate is partial and steady (~36% at auth-service, ~21% at gateway) rather than a clean step to ~100% or a recovering ramp.
**Contradicting evidence**
  - `F3` ✔ auth-service deploy dep-5710 v7.3.0 'Upgrade JWT library to 4.x; refactor token validation' with changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten' was deployed at 2026-04-15T14:00:00Z and is still live (never rolled back).
**Assumptions**
  - auth-service validates tokens by resolving the signing key id, and tokens minted before the rotation carry the old kid
  - the rotation was published without a staged JWKS rollout, as in HIST-108

### H2: auth-service v7.3.0 JWT library upgrade (dep-5710) broke token validation
Deploy dep-5710 at 14:00 upgraded jjwt 0.11 -> 0.12 ('breaking parser API') and rewrote TokenValidator, introducing a regression that rejects a subset of otherwise-valid tokens, yielding the steady ~36% invalid_token rate from 14:02.

**Causal chain**
1. dep-5710 rewrites token validation with a breaking jjwt parser API change at 14:00 [F3]
2. new validator rejects a subset of tokens *(assumed)*
3. auth-service returns 401 invalid_token from 14:02 [F2]

**Supporting evidence**
  - `F1` ✔ auth-service error_rate (unit '% of requests') was flat ~0.2% through 14:01 and jumped to 36.08% at the 14:02 minute bucket, staying ~34-38% thereafter.
  - `F2` ✔ api-gateway began logging 'auth-service POST /v1/token/validate -> 401 invalid_token' at 14:02:22Z; 466 such WARN entries by 15:58 and this is the ONLY warning pattern present.
  - `F3` ✔ auth-service deploy dep-5710 v7.3.0 'Upgrade JWT library to 4.x; refactor token validation' with changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten' was deployed at 2026-04-15T14:00:00Z and is still live (never rolled back).
  - `F9` ✔ Historical incident HIST-109 (2025-07-11) is also a close match: 'Token validation regression after auth library upgrade', root cause 'JWT library upgrade changed clock-skew default', category bad_deployment.
**Contradicting evidence**
  - `F10` ✔ The failure rate is partial and steady (~36% at auth-service, ~21% at gateway) rather than a clean step to ~100% or a recovering ramp.
**Assumptions**
  - the rewritten validator mishandles a token class (e.g. clock skew or key handling) producing partial rather than total failure
  - no canary limited the failure to a subset of instances

### H3: Capacity/resource or dependency fault at auth-service
auth-service 401s stem from resource exhaustion or a downstream dependency (redis-cache, user-db, kyc-provider) degrading.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ auth-service CPU (~38%), memory (~52%), latency_p99 (~90ms), request_rate (~400 rps), rate_limited (~2/min) and restarts (0) all stayed flat across the incident; redis-cache and user-db logs show no error patterns.
  - `F1` ✔ auth-service error_rate (unit '% of requests') was flat ~0.2% through 14:01 and jumped to 36.08% at the 14:02 minute bucket, staying ~34-38% thereafter.
**Rejected because:** CPU/memory/latency/restarts are flat, redis and user-db show no errors, and kyc-provider is unrelated to token validation; the onset is a clean step coincident with two auth-service changes, not a load ramp.

## Facts (verified observations)
- auth-service error_rate jumped from 0.16% (14:01) to 36.08% (14:02) and stayed ~34-38%. [F1]
- api-gateway logged 401 invalid_token for POST /v1/token/validate starting 14:02:22Z; it is the only warning pattern. [F2]
- auth-service deploy dep-5710 (jjwt 0.11->0.12 breaking parser API, TokenValidator rewritten) at 14:00 is live, not rolled back. [F3]
- auth-service config cfg-2705 rotated jwt.signing.key_id k-2025-10 -> k-2026-04 at 14:01. [F4]
- api-gateway 5xx rose to ~21% in the 14:00 bucket and held steady. [F5]
- auth-service CPU, memory, latency, request_rate, rate-limit and restarts stayed flat; redis/user-db healthy. [F6]
- auth-service logs are unsearchable (query_logs 503 on all attempts). [F7]
- HIST-108 is an exact historical match for key-rotation-induced 401s; HIST-109 matches a library-upgrade regression. [F8, F9]
- The failure is partial and steady (~36%), not a step to ~100%. [F10]

## Assumptions (not verified)
- auth-service validates tokens by resolving the token's signing key id against a JWKS/store, so a rotation can invalidate tokens carrying the old kid
- the observed 401 invalid_token is generated by auth-service's validation logic (not an upstream proxy)
- the auth-service version running before 14:00 had no equivalent failure (no earlier 401s observed)
- the 14:00 deploy and 14:01 rotation are independent changes (not part of one coordinated release)

## Timeline
- `2026-04-15T13:00:00Z` auth-service error_rate normal ~0.2%; no invalid_token logs at api-gateway. [F1, F2]
- `2026-04-15T14:00:00Z` auth-service deploy dep-5710 v7.3.0 (jjwt 0.11->0.12 breaking parser API, TokenValidator rewritten) goes live. [F3]
- `2026-04-15T14:01:00Z` Scheduled signing-key rotation cfg-2705 (k-2025-10 -> k-2026-04). [F4]
- `2026-04-15T14:02:00Z` auth-service error_rate jumps to 36.08%; api-gateway 5xx jumps to ~7-21%. [F1, F5]
- `2026-04-15T14:02:22Z` First api-gateway '401 invalid_token' log for POST /v1/token/validate. [F2]
- `2026-04-15T14:12:00Z` Incident INC-007 reported (SEV-1); auth-service logs unsearchable. [F7]
- `2026-04-15T15:58:00Z` 466 invalid_token warnings logged; failure rate still steady ~21% at gateway, no rollback performed. [F2, F5]

## Recommended actions
- **[immediate]** Restore/repair the identity-namespace log pipeline and re-query auth-service logs for the exact token-validation rejection reason (kid mismatch vs parser error). -- The decisive evidence separating a key-rotation event from a JWT-parser regression is the internal rejection message, which is currently unsearchable (F7). [F7]
- **[immediate]** Roll back or revert the JWT signing-key rotation cfg-2705 (re-publish previous key id k-2025-10 / staged JWKS rollout) as the first mitigation, since it is the exact-match historical pattern. -- HIST-108 resolution was exactly 'Re-published previous key; staged rotation'; a single config revert is low-risk. [F4, F8]
- **[immediate]** If key-rotation revert does not restore validation, roll back auth-service deploy dep-5710 to the previous version to test the parser-regression hypothesis. -- dep-5710 is a breaking jjwt parser upgrade that is still live; a rollback cleanly tests H2. [F3, F9]
- **[long_term]** Add staged/canary JWKS rollout and negative-validation smoke tests for token validation on future key rotations and JWT library upgrades. -- Both candidate causes are change-management failures that a staged rollout plus validation smoke test would catch. [F3, F4]

## Data gaps
- auth-service logs are unsearchable (query_logs 503) — the actual token-validation rejection reason (kid mismatch, signature error, parse exception) is unknown, which is the key discriminator between H1 and H2.
- No pod-level / per-instance view of auth-service to check whether failure is limited to a subset of instances (would favor a partial rollout or stale-JWKS pod).
- No token-level detail (issuer kid, issuance time) to confirm whether failures correlate with tokens minted before 14:01 (rotation) vs. a token class (parser).
- No rollback has been performed for either change, so recovery-on-rollback cannot be used to confirm causality.

## Investigation stats
- latency 86.49s, LLM calls 10, tokens 170166 (in 159785 / out 10381)
- tool calls 38 (4 failed, 0 partial, 0 recovered by retry), findings 10/10 verified
