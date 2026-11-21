# Incident investigation report: INC-007
*System: single_agent | outcome: **inconclusive** | ended: agent_submitted*

## Summary
auth-service began failing ~1/3 of token validations with 401 invalid_token at ~14:01, immediately after two co-timed changes: deploy dep-5710 (JWT library upgrade jjwt 0.11→0.12, TokenValidator rewritten, 14:00) and config change cfg-2705 (JWT signing-key rotation k-2025-10→k-2026-04, 14:01). Resources (CPU ~38%, memory ~52%, no restarts), request rate (~400 rps) and latency (~90ms p99) are all flat, ruling out resource/capacity/infra causes; user-db and redis-cache are quiet. The fault originates in auth-service. A bad_deployment (faulty new TokenValidator / library upgrade) is best supported, closely followed by a config_change (key rotation / JWKS propagation). Because the two changes are 60s apart and auth-service's own logs are unavailable (identity-namespace pipeline down), I cannot fully separate them; timing alone is ambiguous, so I rank the deploy first on shape and historical symptom match but flag the rotation as a live alternative.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bad deploy: auth-service v7.3.0 JWT library/TokenValidator regression rejects valid tokens | bad_deployment | auth-service | dep-5710 | 0.50 (medium) | active |
| 2 | H2: Config change: JWT signing-key rotation cfg-2705 broke token validation | config_change | auth-service | cfg-2705 | 0.35 (low) | active |
| 3 | H3: Resource/capacity or infrastructure failure in auth path | capacity_saturation | auth-service | - | 0.05 (low) | rejected |

### H1: Bad deploy: auth-service v7.3.0 JWT library/TokenValidator regression rejects valid tokens
The 14:00 deployment dep-5710 upgraded jjwt 0.11->0.12 (breaking parser API) and rewrote TokenValidator. The new validation path rejects a stable ~1/3 of otherwise-valid tokens with 401 invalid_token, producing persistent errors from ~14:01 until the current time.

**Causal chain**
1. dep-5710 deployed auth-service v7.3.0 at 14:00 with jjwt 0.11->0.12 and a rewritten TokenValidator [F3]
2. New validation logic rejects tokens -> 401 invalid_token at api-gateway [F2]
3. Stable ~36% error rate from 14:01 onward [F1]

**Supporting evidence**
  - `F1` ✔ auth-service error_rate stayed at ~0.2% (13:40-13:58) then rose abruptly to avg 24.86% (max 38.34) in the 14:01 bucket, holding at ~35-37% from 14:04 onward.
  - `F2` ✔ api-gateway logged 401 invalid_token for auth-service /v1/token/validate, first match 2026-04-15T14:02:22Z, last 15:58:51Z, total 466, all WARN.
  - `F3` ✔ Deployment dep-5710 to auth-service v7.3.0 went out at 2026-04-15T14:00:00Z: 'Upgrade JWT library to 4.x; refactor token validation', changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten', status live, rolled_back_at null.
  - `F12` ✔ Historical incident HIST-109 (2025-07-11) had identical symptoms (401 invalid_token) with root cause 'JWT library upgrade changed clock-skew default', category bad_deployment.
**Contradicting evidence**
  - `F4` ✔ Config change cfg-2705 changed auth-service key jwt.signing.key_id from k-2025-10 to k-2026-04 at 2026-04-15T14:01:00Z (scheduled signing-key rotation, author security-automation).
**Assumptions**
  - The new TokenValidator has a bug (e.g., algorithm/claim/clock-skew handling) that rejects a token class
  - cfg-2705 at 14:01 is not the actual trigger
  - ~36% corresponds to the fraction of pods/requests hitting the new code path

### H2: Config change: JWT signing-key rotation cfg-2705 broke token validation
At 14:01 cfg-2705 rotated jwt.signing.key_id k-2025-10 -> k-2026-04. If the new key id was published before all pods loaded the JWKS (per HIST-108), tokens signed/verified against the wrong key fail, yielding 401 invalid_token.

**Causal chain**
1. cfg-2705 rotated the signing key id at 14:01 [F4]
2. Pods/consumers with stale or missing key verify against wrong key -> 401 invalid_token [F2]
3. Elevated error rate from 14:01 onward [F1]

**Supporting evidence**
  - `F4` ✔ Config change cfg-2705 changed auth-service key jwt.signing.key_id from k-2025-10 to k-2026-04 at 2026-04-15T14:01:00Z (scheduled signing-key rotation, author security-automation).
  - `F2` ✔ api-gateway logged 401 invalid_token for auth-service /v1/token/validate, first match 2026-04-15T14:02:22Z, last 15:58:51Z, total 466, all WARN.
  - `F13` ✔ Historical incident HIST-108 (2025-10-01) had symptoms '401 invalid_token','users logged out' with root cause 'Key rotation published new key id before all pods loaded the JWKS', category config_change.
**Contradicting evidence**
  - `F1` ✔ auth-service error_rate stayed at ~0.2% (13:40-13:58) then rose abruptly to avg 24.86% (max 38.34) in the 14:01 bucket, holding at ~35-37% from 14:04 onward.
  - `F12` ✔ Historical incident HIST-109 (2025-07-11) had identical symptoms (401 invalid_token) with root cause 'JWT library upgrade changed clock-skew default', category bad_deployment.
**Assumptions**
  - JWKS propagation lag or a wrong key causes mismatches
  - The rotation, not the deploy, is causal

### H3: Resource/capacity or infrastructure failure in auth path
auth-service or its datastores ran out of CPU/memory/connections, or redis/user-db failed, causing validation failures.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ auth-service cpu_pct ranged only 34-42% (mean 37.96) with no step change at onset.
  - `F7` ✔ auth-service memory_pct was flat (mean 52.06, max 58.42) across the window.
  - `F8` ✔ auth-service latency_p99_ms was flat ~90ms (mean 90.11) with no degradation at onset.
  - `F9` ✔ auth-service request_rate stayed ~400 req/s (mean 398.82) and restarts were 0 throughout.
  - `F10` ✔ user-db returned 0 ERROR log entries in the window.
  - `F11` ✔ redis-cache logged only 3 WARNs: 'clock drift 38ms detected, resyncing with NTP' (13:11), slow request on /internal/metrics (15:31), and 'GC pause 326ms exceeded 200ms target' (15:47).
**Rejected because:** CPU ~38%, memory ~52%, latency ~90ms, request rate ~400rps and restarts=0 are all flat with no step change at onset; user-db 0 errors; redis only 3 unrelated WARNs. No resource or infra degradation correlates with onset.

## Facts (verified observations)
- auth-service error rate was ~0.2% until 13:58, jumped to 24.86% in the 14:01 bucket and stabilized at ~36% from 14:04 onward. [F1]
- api-gateway first logged 'auth-service POST /v1/token/validate -> 401 invalid_token' at 14:02:22Z, with 466 such events through 15:58:51Z. [F2]
- The only relevant changes are deploy dep-5710 (auth-service v7.3.0, 14:00Z, 'jjwt 0.11 -> 0.12 (breaking parser API)', 'TokenValidator rewritten', status live, not rolled back) and config change cfg-2705 (jwt.signing.key_id k-2025-10 -> k-2026-04, 14:01Z). [F3, F4, F5]
- auth-service CPU (~38%), memory (~52%), latency p99 (~90ms) and request_rate (~400 req/s) are flat across the incident; restarts are 0. [F6, F7, F8, F9]
- user-db logged 0 ERROR entries and redis-cache logged only 3 unrelated WARNs (clock drift, slow /internal/metrics, GC pause) in the window. [F10, F11]
- Historical incident HIST-109 matches symptoms exactly (401 invalid_token after JWT library upgrade, category bad_deployment) and HIST-108 matches a key-rotation cause (401s after signing key rotation, category config_change). [F12, F13]
- auth-service logs became fully unavailable (query_logs returned 503 then circuit_open) and api-gateway logs contain no 'signature', 'key', or 'expired' strings. [F14, F15]

## Assumptions (not verified)
- Onset is ~14:00-14:02 (error_rate bucket boundary at 14:01 plus first gateway 401 at 14:02:22).
- The generic 'invalid_token' gateway message hides the auth-service-side failure reason (signature vs parse vs key-not-found).
- No other change (market-data-service dep-5699 at 11:35) is relevant to token validation.
- Historical incidents are analogous patterns, not proof of the current cause.

## Timeline
- `2026-04-15T11:35:00Z` market-data-service dep-5699 v5.0.3 deployed (Fix symbol mapping) - unrelated, pre-onset. [F5]
- `2026-04-15T13:58:00Z` auth-service error_rate still baseline ~0.2%. [F1]
- `2026-04-15T14:00:00Z` auth-service dep-5710 v7.3.0 deployed: jjwt 0.11->0.12 (breaking parser API), TokenValidator rewritten. [F3]
- `2026-04-15T14:01:00Z` Config change cfg-2705: jwt.signing.key_id k-2025-10 -> k-2026-04 (scheduled key rotation). error_rate bucket 14:01 rises to avg 24.86%. [F4, F1]
- `2026-04-15T14:02:22Z` First 401 invalid_token on auth-service /v1/token/validate logged at api-gateway. [F2]
- `2026-04-15T14:04:00Z` auth-service error_rate stabilizes ~36% and remains flat through 15:59. [F1]
- `2026-04-15T14:12:00Z` Incident INC-007 reported (users logged out / 401s). []

## Recommended actions
- **[immediate]** Roll back auth-service to the pre-dep-5710 version (or pin jjwt to 0.11) and observe whether 401 invalid_token rate returns to baseline. -- The deploy precedes onset by ~1-2 minutes and is the best-supported trigger; rollback both tests the bad-deployment hypothesis and (if it clears the errors) restores service. [F1, F2, F3]
- **[immediate]** If rollback does not clear the 401s, re-publish the previous signing key id (k-2025-10) / fix JWKS propagation for cfg-2705. -- Tests the alternative config_change hypothesis (HIST-108 pattern: key rotated before all pods loaded JWKS). [F4, F13]
- **[short_term]** Capture/auth-service-side error detail (signature mismatch vs parse/claim error) by restoring the identity-namespace log pipeline or adding a distinct error code at the gateway. -- The generic 'invalid_token' message is insufficient to separate a key mismatch from a library regression; this is the key discriminator. [F14, F15]
- **[long_term]** Stage key rotations and gate JWT-library upgrades so only one change lands at a time; add JWKS-load confirmation before publishing a new key id. -- Both prior incidents (HIST-108/HIST-109) stem from these change classes; co-timed changes made diagnosis ambiguous. [F12, F13]

## Data gaps
- auth-service logs are unavailable (identity-namespace pipeline down; query_logs returned 503 then circuit_open) so the auth-side failure reason for invalid_token cannot be read.
- api-gateway logs expose only the generic 'invalid_token' text; no 'signature'/'key'/'expired' detail to distinguish key mismatch from a parser/library bug.
- Deploy dep-5710 rollout state (how many replicas run the new version) is unknown, so the '~1/3 of pods' mechanism for the 36% rate is unverified.
- No rollback has been attempted, so causal confirmation (error rate normalizing on rollback) is not yet available.
- kyc-provider (external dependency) has no telemetry, so it cannot be fully excluded as a contributor, though the failure is in token validation which does not involve it.

## Investigation stats
- latency 85.19s, LLM calls 10, tokens 162613 (in 152003 / out 10610)
- tool calls 38 (13 failed, 0 partial, 10 recovered by retry), findings 15/15 verified
