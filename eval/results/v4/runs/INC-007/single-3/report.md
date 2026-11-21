# Incident investigation report: INC-007
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
auth-service token validation began failing with 401 invalid_token at ~14:02 on 2026-04-15, plateauing at ~36% of requests and driving ~21% 5xx at api-gateway; it has not self-recovered. Two auth-service changes landed within one minute: deploy dep-5710 (jjwt 0.11->0.12 breaking parser API, TokenValidator rewritten) at 14:00 and config cfg-2705 (signing-key rotation k-2025-10 -> k-2026-04) at 14:01. Dependencies (user-db, redis-cache), load, CPU/memory and restarts are all flat, ruling out dependency failure and resource/capacity causes. The fault is a partial (not total) failure, which is more consistent with a signing-key mismatch (fraction of tokens) than a uniform code bug in the rewritten validator, and matches historical incident HIST-108. However, because auth-service logs are unavailable (503) and both triggers are within ~1 minute, the two cannot be cleanly separated; the config-change (key rotation) hypothesis is ranked first at moderate confidence with the bad_deployment hypothesis a close second.

## Probable root cause
**Signing-key rotation broke a subset of token validations** -- config_change in `auth-service`, triggered by `cfg-2705` (confidence 0.55, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Signing-key rotation broke a subset of token validations | config_change | auth-service | cfg-2705 | 0.55 (medium) | active |
| 2 | H2: JWT library upgrade deploy introduced a validation regression | bad_deployment | auth-service | dep-5710 | 0.35 (low) | active |
| 3 | H3: Dependency/datastore failure | dependency_failure | user-db | - | 0.05 (low) | rejected |
| 4 | H4: Resource exhaustion / capacity saturation on auth-service | capacity_saturation | auth-service | - | 0.03 (low) | rejected |

### H1: Signing-key rotation broke a subset of token validations
The scheduled auth-service signing-key rotation (cfg-2705, k-2025-10 -> k-2026-04 at 14:01) caused validation to reject tokens still associated with the old key, producing 401 invalid_token for ~1/3 of requests — the same failure mode as HIST-108 (new key id published before all validators/JWKS caches caught up).

**Causal chain**
1. cfg-2705 rotates signing key k-2025-10 -> k-2026-04 [F3]
2. Validators/caches still hold old key, so a fraction of tokens mismatch [F10]
3. ~36% of /v1/token/validate return 401 invalid_token from 14:02 [F4, F8]
4. Gateway surfaces ~21% 5xx [F5]

**Supporting evidence**
  - `F3` ✔ A scheduled signing-key rotation cfg-2705 changed auth-service jwt.signing.key_id from k-2025-10 to k-2026-04 at 2026-04-15T14:01:00Z.
  - `F4` ✔ api-gateway logs show 401 invalid_token on POST /v1/token/validate starting 2026-04-15T14:02:22Z — first observed error, ~1m22s after the deploy and ~1m22s after the key rotation.
  - `F8` ✔ The failure is partial: ~36% of token validations fail, not 100%, with a steady plateau (no recovery to present).
  - `F10` ✔ Historical incident HIST-108 (2025-10-01) had identical auth-service symptoms (401 invalid_token, users logged out) with root cause a signing-key rotation publishing a new key id before all pods loaded the JWKS.
  - `F13` ✔ Inference: a uniform code bug in the rewritten TokenValidator would fail essentially all validations on the deployed fleet (restarts=0, so no partial rollout), which is inconsistent with the observed ~36% partial failure; a key-mismatch affecting a subset of tokens better matches a fractional failure.
  - `F1` ✔ auth-service error_rate was ~0.2% until 14:00, then jumped to ~36% in the 14:02 bucket and stayed there through 15:58.
**Contradicting evidence**
  - `F2` ✔ auth-service deploy dep-5710 (v7.3.0) replaced the JWT library (jjwt 0.11 -> 0.12, breaking parser API) and rewrote TokenValidator, deployed 2026-04-15T14:00:00Z, status live, not rolled back.
  - `F11` ✔ Historical incident HIST-109 (2025-07-11) had the same symptom class after a JWT library upgrade (clock-skew default change), resolved by rollback.
**Assumptions**
  - Some tokens in circulation are still keyed under k-2025-10
  - JWKS/key propagation lag produces a partial, not total, failure

### H2: JWT library upgrade deploy introduced a validation regression
Deploy dep-5710 (v7.3.0, jjwt 0.11 -> 0.12 breaking parser API, TokenValidator rewritten at 14:00) introduced a bug that rejects valid tokens, mirroring HIST-109 (library upgrade affecting validation), accepted as 401 invalid_token.

**Causal chain**
1. dep-5710 rewrites TokenValidator with a breaking jjwt API change [F2]
2. New validation logic rejects tokens it previously accepted [F11]
3. auth-service error_rate rises to ~36% from 14:02 [F4, F1]

**Supporting evidence**
  - `F2` ✔ auth-service deploy dep-5710 (v7.3.0) replaced the JWT library (jjwt 0.11 -> 0.12, breaking parser API) and rewrote TokenValidator, deployed 2026-04-15T14:00:00Z, status live, not rolled back.
  - `F4` ✔ api-gateway logs show 401 invalid_token on POST /v1/token/validate starting 2026-04-15T14:02:22Z — first observed error, ~1m22s after the deploy and ~1m22s after the key rotation.
  - `F11` ✔ Historical incident HIST-109 (2025-07-11) had the same symptom class after a JWT library upgrade (clock-skew default change), resolved by rollback.
  - `F1` ✔ auth-service error_rate was ~0.2% until 14:00, then jumped to ~36% in the 14:02 bucket and stayed there through 15:58.
**Contradicting evidence**
  - `F8` ✔ The failure is partial: ~36% of token validations fail, not 100%, with a steady plateau (no recovery to present).
  - `F13` ✔ Inference: a uniform code bug in the rewritten TokenValidator would fail essentially all validations on the deployed fleet (restarts=0, so no partial rollout), which is inconsistent with the observed ~36% partial failure; a key-mismatch affecting a subset of tokens better matches a fractional failure.
**Assumptions**
  - The rewritten parser fails on a subset of legitimate tokens rather than all of them
  - No rollback has been attempted yet

### H3: Dependency/datastore failure
Failure originates in a downstream dependency (user-db, redis-cache) or external provider (kyc-provider).

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ Dependency datastores are healthy: redis-cache hit_ratio ~96% and connected_clients ~310, user-db connections_active ~25 — all flat across onset.
  - `F4` ✔ api-gateway logs show 401 invalid_token on POST /v1/token/validate starting 2026-04-15T14:02:22Z — first observed error, ~1m22s after the deploy and ~1m22s after the key rotation.
**Rejected because:** user-db connections, redis-cache hit_ratio/connected_clients all flat across onset (F7); no dependency error metrics; failure clearly starts at 14:02 coinciding with auth-service changes (F4).

### H4: Resource exhaustion / capacity saturation on auth-service
auth-service ran out of CPU/memory or was saturated by load, causing validation failures.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ auth-service CPU (~38%), memory (~52%), latency_p99 (~90ms) and request_rate (~400 req/s) are all flat across the onset; restarts are 0 throughout.
**Rejected because:** CPU (~38%), memory (~52%), request_rate (~400 req/s) and latency_p99 (~90ms) are flat across onset; restarts=0 (F6); failures are 401s not 5xx from overload.

## Facts (verified observations)
- auth-service error_rate jumped from ~0.2% to ~36% at 14:02 and plateaued through 15:58. [F1, F8]
- Deploy dep-5710 rewrote auth-service TokenValidator with a breaking jjwt 0.11->0.12 API change at 14:00. [F2]
- Config cfg-2705 rotated auth-service signing key k-2025-10 -> k-2026-04 at 14:01. [F3]
- First 401 invalid_token logged at api-gateway 14:02:22Z on POST /v1/token/validate. [F4]
- api-gateway 5xx rose to ~21% in the same window (downstream effect). [F5]
- auth-service CPU, memory, latency, request_rate flat and restarts=0 across onset. [F6]
- redis-cache and user-db metrics healthy and flat across onset. [F7]
- auth-service logs unsearchable (backend 503). [F9]
- HIST-108: prior identical 401/logout incident caused by signing-key rotation vs JWKS propagation. [F10]
- HIST-109: prior 401 incident after JWT library upgrade, fixed by rollback. [F11]
- No api-gateway deploy or config change in window. [F12]

## Assumptions (not verified)
- Tokens in circulation are split between the old and new signing keys, enabling a partial failure.
- The deploy rollout completed synchronously (restarts=0 means all pods run v7.3.0), so a uniform code bug would fail ~100%.
- No rollback of dep-5710 has been attempted; if one had and did not help, H2 would be weakened.

## Timeline
- `2026-04-15T11:35:00Z` Unrelated deploy dep-5699 (market-data-service v5.0.3) — no correlation to auth symptoms. [F2]
- `2026-04-15T14:00:00Z` auth-service deploy dep-5710 (v7.3.0) — jjwt 0.11->0.12 breaking parser API, TokenValidator rewritten. [F2]
- `2026-04-15T14:01:00Z` Config cfg-2705 rotates jwt.signing.key_id k-2025-10 -> k-2026-04. [F3]
- `2026-04-15T14:02:00Z` auth-service error_rate jumps to ~37%; first api-gateway 401 invalid_token at 14:02:22Z. [F1, F4]
- `2026-04-15T14:12:00Z` Incident INC-007 reported (users logged out / 401s). []
- `2026-04-15T15:58:00Z` Error rate still ~36%; no recovery, no rollback recorded. [F8]

## Recommended actions
- **[immediate]** If the signing-key rotation is suspect, revert cfg-2705 to k-2025-10 (or publish the previous key alongside the new one) and observe whether 401s drop. -- Fastest way to confirm/deny the key-rotation hypothesis; HIST-108 resolution was re-publishing the previous key. [F3, F10]
- **[immediate]** If no improvement after key revert, roll back deploy dep-5710 to v7.2.x and observe recovery. -- HIST-109 was resolved by rolling back the JWT library upgrade; separates H2 from H1. [F2, F11]
- **[short_term]** Restore the identity-namespace log pipeline and capture auth-service TokenValidator logs (error type: key-not-found vs signature/clock-skew). -- The exact rejection reason would cleanly discriminate key-mismatch from parser bug. [F9]
- **[short_term]** Add an alert on token-validation 401 ratio and a JWKS/key-id match gauge to catch partial validation failures earlier. -- The 36% partial failure lacked a dedicated signal and went unnoticed for ~10 minutes. [F1, F8]
- **[long_term]** Stagger future rotations and deploys of auth-service by a safe interval; add a canary/JWKS-propagation check before full key cutover. -- Two overlapping changes within 1 minute made root cause ambiguous; HIST-108 recommends staged rotation. [F2, F3, F10]

## Data gaps
- auth-service logs unavailable (backend 503), so the literal validation error (key-not-found vs signature/clock-skew vs parser exception) could not be read — this is the single most discriminating missing datum.
- No explicit JWKS/key-propagation telemetry or config subkeys showing which validators loaded which key.
- Could not determine the exact traffic split by token key, so the ~36% partial rate is consistent with but not proven to be a token-fraction.
- No deployment canary/phased-rollout metadata to confirm whether v7.3.0 was fully or partially rolled out before 14:02.

## Investigation stats
- latency 212.4s, LLM calls 9, tokens 140102 (in 129798 / out 10304)
- tool calls 37 (6 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
