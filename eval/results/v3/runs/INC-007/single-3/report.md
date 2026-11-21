# Incident investigation report: INC-007
*System: single_agent | outcome: **inconclusive** | ended: agent_submitted*

## Summary
Onset was sharp at ~14:00-14:02: auth-service HTTP 5xx error_rate jumped from ~0.2% to ~36% and held steady for two hours; api-gateway showed 401 invalid_token specifically on POST /v1/token/validate (466 matches, first 14:02:22Z). No resource, capacity, dependency or infra signal (flat CPU/memory/latency, zero restarts, stable request rates, healthy redis/user-db). Exactly two changes bracket the onset: code deploy dep-5710 (auth-service v7.3.0, JWT lib jjwt 0.11->0.12 with a 'breaking parser API', TokenValidator rewritten) at 14:00, and config change cfg-2705 (jwt.signing.key_id rotation k-2025-10 -> k-2026-04) at 14:01. Both are strong, near-identical fault vectors and are only one minute apart; the decisive evidence — auth-service's own logs — is unavailable because the identity namespace log pipeline is down. With neither change rolled back and no separation possible from metrics alone, the origin is auth-service (identity namespace) but the specific trigger cannot be definitively distinguished. Best-ranked root cause is the JWT library upgrade deploy (earliest change, directly rewrites the token parser); the signing-key rotation is a close second (matching HIST-108).

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: auth-service v7.3.0 JWT library upgrade broke token validation (bad_deployment) | bad_deployment | auth-service | dep-5710 | 0.50 (medium) | active |
| 2 | H2: JWT signing-key rotation caused validation failures (config_change) | config_change | auth-service | cfg-2705 | 0.35 (low) | active |
| 3 | H3: Dependency/infrastructure failure (rejected) | dependency_failure | redis-cache | - | 0.03 (low) | rejected |

### H1: auth-service v7.3.0 JWT library upgrade broke token validation (bad_deployment)
Deploy dep-5710 (auth-service v7.3.0) upgraded jjwt 0.11->0.12 with a 'breaking parser API' and rewrote TokenValidator at 14:00. The refactor introduced a validation defect (e.g. changed default/parsing behaviour) so that a large fraction of otherwise-valid tokens now fail parsing, returning 401 invalid_token on POST /v1/token/validate.

**Causal chain**
1. dep-5710 deploys token-validation rewrite at 14:00 [F4]
2. first 401 invalid_token at 14:02:22 [F3]
3. auth-service 5xx rises to ~36% and persists [F1]
4. no rollback, no recovery [F10]

**Supporting evidence**
  - `F1` ✔ auth-service HTTP 5xx error_rate jumped from ~0.2% to ~36% starting in the 14:00 bucket and stayed ~36% through 15:45.
  - `F3` ✔ api-gateway WARN logs show 401 invalid_token on POST /v1/token/validate first appearing 2026-04-15T14:02:22Z with 466 matches; the only pattern present.
  - `F4` ✔ A code deployment dep-5710 for auth-service v7.3.0 occurred at 14:00 (summary: 'Upgrade JWT library to 4.x; refactor token validation') with changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live, not rolled back.
  - `F6` ✔ auth-service cpu_pct (~38%), memory_pct (~52%), latency_p99_ms (~90ms) and restarts (0) were flat across the incident; no resource or saturation signal.
  - `F7` ✔ auth-service request_rate and api-gateway request_rate were stable (~395 req/s and ~1270 req/s), so the failure is not load-driven.
  - `F10` ✔ No rollback of either change has occurred (dep-5710 status live, rolled_back_at null; cfg-2705 is the current key), and the error rate has not recovered — consistent with a persistent fault, not a transient.
  - `F12` ✔ The failing operation is a healthy-path token validation call: request volume and latency are normal while ~36% of validate calls return 401, so the fault is in the validation logic/key material rather than in capacity or transport.
  - `F11` ✔ The two closest historical incidents are HIST-109 ('Token validation regression after auth library upgrade', category bad_deployment, JWT library upgrade) and HIST-108 ('401s after JWT signing key rotation', category config_change) — both mirror current symptoms but are leads, not evidence.
**Contradicting evidence**
  - `F5` ✔ A config change cfg-2705 rotated auth-service jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01 ('Scheduled signing-key rotation').
**Assumptions**
  - The deployed TokenValidator rewrite contains a validation defect.
  - The 14:00 deploy rollout completed within ~2 minutes to all pods.

### H2: JWT signing-key rotation caused validation failures (config_change)
Config change cfg-2705 rotated auth-service jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01. If the new key/JWKS was published before all pods loaded it (the HIST-108 pattern), tokens signed/validated against the new key id fail, producing 401 invalid_token for the affected fraction of traffic.

**Causal chain**
1. cfg-2705 rotates signing key id at 14:01 [F5]
2. 401 invalid_token begins 14:02 [F3]
3. error rate locks at ~36% with no recovery [F1]

**Supporting evidence**
  - `F1` ✔ auth-service HTTP 5xx error_rate jumped from ~0.2% to ~36% starting in the 14:00 bucket and stayed ~36% through 15:45.
  - `F3` ✔ api-gateway WARN logs show 401 invalid_token on POST /v1/token/validate first appearing 2026-04-15T14:02:22Z with 466 matches; the only pattern present.
  - `F5` ✔ A config change cfg-2705 rotated auth-service jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01 ('Scheduled signing-key rotation').
  - `F10` ✔ No rollback of either change has occurred (dep-5710 status live, rolled_back_at null; cfg-2705 is the current key), and the error rate has not recovered — consistent with a persistent fault, not a transient.
  - `F11` ✔ The two closest historical incidents are HIST-109 ('Token validation regression after auth library upgrade', category bad_deployment, JWT library upgrade) and HIST-108 ('401s after JWT signing key rotation', category config_change) — both mirror current symptoms but are leads, not evidence.
**Contradicting evidence**
  - `F4` ✔ A code deployment dep-5710 for auth-service v7.3.0 occurred at 14:00 (summary: 'Upgrade JWT library to 4.x; refactor token validation') with changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live, not rolled back.
  - `F2` ✔ api-gateway error_rate rose in the same 14:00 bucket from ~0.2% to ~21%, indicating the failures surface on calls the gateway makes to auth-service.
**Assumptions**
  - New key id published/loaded inconsistently across pods or old tokens signed with the previous key are rejected.
  - A ~1/3 failure fraction reflects the share of tokens tied to the rotated key.

### H3: Dependency/infrastructure failure (rejected)
A downstream dependency or shared component (redis-cache, user-db, kyc-provider) or node-level resource exhaustion caused the 401s.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ auth-service cpu_pct (~38%), memory_pct (~52%), latency_p99_ms (~90ms) and restarts (0) were flat across the incident; no resource or saturation signal.
  - `F7` ✔ auth-service request_rate and api-gateway request_rate were stable (~395 req/s and ~1270 req/s), so the failure is not load-driven.
  - `F8` ✔ Downstream dependencies redis-cache (cpu ~22%) and user-db are healthy; kyc-provider is an external vendor with no telemetry but is not on the token-validation path.
**Rejected because:** redis-cache cpu flat ~22%, user-db and redis show no error/health degradation, auth-service cpu/memory/latency/restarts all normal, request rates stable; no signal of a dependency or infra fault, and 401 invalid_token is a validation-logic outcome, not a connectivity error.

## Facts (verified observations)
- auth-service error_rate jumped from ~0.2% to ~36% at the 14:00 bucket and stayed ~36% through end of window. [F1]
- api-gateway error_rate rose to ~21% in the same bucket. [F2]
- All 466 api-gateway warnings are 401 invalid_token on POST /v1/token/validate, first seen 14:02:22Z. [F3]
- auth-service deploy dep-5710 (v7.3.0) shipped a JWT library upgrade (jjwt 0.11->0.12, breaking parser API) and a TokenValidator rewrite at 14:00. [F4]
- Config change cfg-2705 rotated jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01. [F5]
- auth-service cpu/memory/latency/restarts were flat; no resource or saturation signal. [F6]
- Request rates for auth-service and api-gateway were stable. [F7]
- redis-cache and user-db showed no degraded health; kyc-provider has no telemetry and is not on the token path. [F8]
- auth-service logs are unavailable (backend 503) so the service-side 401 reason cannot be read. [F9]
- Neither dep-5710 nor cfg-2705 has been rolled back and the error rate has not recovered. [F10]

## Assumptions (not verified)
- Both the 14:00 deploy and 14:01 config change were fully applied within ~1-2 minutes to the affected fleet.
- The ~36% failure fraction is steady-state and reflects a deterministic subset of validate calls.
- kyc-provider is not part of the token-validation path.

## Timeline
- `2026-04-15T14:00:00Z` auth-service deploy dep-5710 (v7.3.0, jjwt 0.11->0.12 breaking parser API, TokenValidator rewritten) goes live. [F4]
- `2026-04-15T14:01:00Z` Config change cfg-2705 rotates jwt.signing.key_id to k-2026-04. [F5]
- `2026-04-15T14:02:22Z` First api-gateway WARN: POST /v1/token/validate -> 401 invalid_token. [F3]
- `2026-04-15T14:00:00Z` auth-service 5xx error_rate rises to ~36%; api-gateway to ~21%. [F1, F2]
- `2026-04-15T14:12:00Z` Incident reported (SEV-1). []
- `2026-04-15T15:59:00Z` Error rate still ~36% with no rollback and no recovery. [F1, F10]

## Recommended actions
- **[immediate]** Restore the identity-namespace log pipeline (or pull auth-service container logs directly) to obtain the exact parse/validation error and key-id mismatch details before/while mitigating. -- The decisive evidence separating the deploy from the config change is missing (F9); without it the trigger is ambiguous. [F9]
- **[immediate]** Roll back dep-5710 (auth-service to pre-v7.3.0) first, since it is the earliest change and directly rewrites the token parser, and watch the error rate. -- If it is the deploy, rolling back is the fastest fix and recovers service; if error rate does not drop it implicates the config change. [F4, F1]
- **[immediate]** If rollback of dep-5710 does not resolve it, revert cfg-2705 to the previous key id k-2025-10 and re-publish the old key while staging the rotation. -- Matches the HIST-108 failure mode (key id published before all pods loaded the JWKS). [F5, F11]
- **[short_term]** Stage signin-key rotations (publish new key alongside old, wait for JWKS propagation across all pods) and gate JWT library upgrades behind canary + token-parse tests. -- Both candidate triggers are change-management risks with prior incidents (HIST-108, HIST-109). [F11]
- **[long_term]** Add a validation canary/alert on invalid_token rate and on signing-key id propagation so this class of failure is detected before SEV-1. -- The failure was invisible for ~2h and log pipeline outage hid the cause. [F9, F1]

## Data gaps
- auth-service logs are unsearchable (identity-namespace log pipeline down) — the exact service-side error for the 401 invalid_token (parse failure vs. unknown/rotated key id) is not observable, which is the key evidence needed to separate H1 from H2.
- No telemetry from kyc-provider (external, has_telemetry=false), though it does not appear on the token-validation path.
- Deployment metadata does not indicate whether dep-5710 used a canary/partial rollout, so the ~36% failure fraction cannot be attributed to a partial fleet version.

## Investigation stats
- latency 54.83s, LLM calls 6, tokens 88212 (in 79255 / out 8957)
- tool calls 28 (4 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
