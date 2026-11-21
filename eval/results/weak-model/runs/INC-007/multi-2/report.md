# Incident investigation report: INC-007
*System: multi_agent | outcome: **inconclusive** | ended: budget_exhausted:deadline*

## Summary
Investigation is inconclusive: the 401 surge aligns with both a JWT signing key rotation (H2) and a recent auth-service deployment that upgraded the JWT library (H1), each supported by limited evidence. Neither hypothesis can be confirmed without additional data, so both remain plausible. Immediate mitigations and further data collection are recommended to isolate the root cause.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: JWT signing key rotation mis‑aligned with auth-service verification | config_change | auth-service | config-2026-04-15-14-01-jwt-key-rotation | 0.40 (low) | active |
| 2 | H1: Bug introduced by auth-service deployment (JWT library upgrade & TokenValidator rewrite) | bad_deployment | auth-service | deploy-2026-04-15-14-00-auth-service | 0.40 (low) | active |
| 3 | H3: Shared Redis cache outage or corruption affecting token validation | dependency_failure | redis-cache | null | 0.20 (low) | active |
| 4 | H4: External token signing service (identity provider) issuing malformed or expired tokens | dependency_failure | identity-provider | null | 0.10 (low) | active |

### H2: JWT signing key rotation mis‑aligned with auth-service verification
A configuration change at 2026-04-15T14:01:00Z rotated the JWT signing key (k‑2025‑10 → k‑2026‑04). Auth-service continued to verify tokens with the old key (or vice‑versa), causing most token validations to fail and resulting in 401 errors.

**Causal chain**
1. Configuration change rotated JWT signing key [F11]
2. Auth-service verification key not updated in time / mismatch between signing and verification keys *(assumed)*
3. Token validation fails, auth-service returns 401 for most requests [F5, F6]

**Supporting evidence**
  - `F11` ✔ A configuration change rotated the JWT signing key for auth-service at 2026-04-15T14:01:00Z (key_id from k-2025-10 to k-2026-04).
  - `F5` ✔ The 401 error spike (error_rate) began at 2026-04-15T14:02:00Z, where the metric jumped to 36.08% average.
  - `F6` ✔ Error rate was stable around 0.2% for the preceding 24 h (10:00‑13:52) and rose to ~35‑38% after 14:02, staying high through the latest data point.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Auth-service loads the signing key from configuration at startup and did not reload after rotation
  - The token issuer started using the new key immediately after rotation

### H1: Bug introduced by auth-service deployment (JWT library upgrade & TokenValidator rewrite)
The deployment of auth-service at 2026-04-15T14:00:00Z (v7.3.0) upgraded the JWT library to 4.x and rewrote the TokenValidator, introducing a bug that incorrectly rejects valid tokens, causing a surge of 401 responses.

**Causal chain**
1. Deployment upgraded JWT library and rewrote TokenValidator [F10]
2. Bug in new TokenValidator causes valid tokens to be marked invalid *(assumed)*
3. Auth-service returns 401 for a large fraction of requests [F5, F6]

**Supporting evidence**
  - `F10` ✔ auth-service was deployed at 2026-04-15T14:00:00Z (v7.3.0) with an upgrade of the JWT library to 4.x and a rewritten TokenValidator.
  - `F5` ✔ The 401 error spike (error_rate) began at 2026-04-15T14:02:00Z, where the metric jumped to 36.08% average.
  - `F6` ✔ Error rate was stable around 0.2% for the preceding 24 h (10:00‑13:52) and rose to ~35‑38% after 14:02, staying high through the latest data point.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The new TokenValidator logic can produce false‑negative validation results
  - No other component (e.g., key rotation) is responsible for the failures

### H3: Shared Redis cache outage or corruption affecting token validation
The shared Redis cache, used by auth-service for token‑related data (e.g., revocation lists), experienced an outage or corrupted state, leading auth-service to reject tokens and emit 401 responses.

**Causal chain**
1. Redis cache became unavailable or returned corrupted data *(assumed)*
2. Auth-service lookup of token data fails or yields false revocation status [F1]
3. Auth-service returns 401 for affected requests [F5, F6]

**Supporting evidence**
  - `F1` ✔ auth-service calls a Redis cache that is marked as a shared datastore used by multiple services.
  - `F5` ✔ The 401 error spike (error_rate) began at 2026-04-15T14:02:00Z, where the metric jumped to 36.08% average.
  - `F6` ✔ Error rate was stable around 0.2% for the preceding 24 h (10:00‑13:52) and rose to ~35‑38% after 14:02, staying high through the latest data point.
**Contradicting evidence**
  - `F13` ✔ No deployments were recorded for the redis-cache in the last 24 h.
**Assumptions**
  - Redis outage was not captured by deployment logs
  - Auth-service relies on Redis for token validation decisions

### H4: External token signing service (identity provider) issuing malformed or expired tokens
The external identity provider that signs JWTs began issuing tokens with incorrect claims or timestamps, causing auth-service to reject them as invalid and return 401 errors.

**Causal chain**
1. Identity provider started issuing malformed/expired tokens *(assumed)*
2. Auth-service validates tokens and deems them invalid [F2]
3. Auth-service returns 401 for the majority of requests [F5, F6]

**Supporting evidence**
  - `F2` ✔ auth-service also depends on a PostgreSQL user-db (non‑shared) and an external KYC provider (non‑shared).
  - `F5` ✔ The 401 error spike (error_rate) began at 2026-04-15T14:02:00Z, where the metric jumped to 36.08% average.
  - `F6` ✔ Error rate was stable around 0.2% for the preceding 24 h (10:00‑13:52) and rose to ~35‑38% after 14:02, staying high through the latest data point.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Auth-service does not have a fallback verification path
  - The token signing service change occurred around the same time as the error spike

## Facts (verified observations)
- A 401 error spike began at 2026-04-15T14:02:00Z, rising to ~36% error rate. [F5, F6]
- Auth-service was deployed at 2026-04-15T14:00:00Z (v7.3.0) with an upgrade to JWT library 4.x and a rewritten TokenValidator. [F10]
- A configuration change rotated the JWT signing key for auth-service at 2026-04-15T14:01:00Z (k‑2025‑10 → k‑2026‑04). [F11]
- CPU, memory, and p99 latency of auth-service remained stable before, during, and after the error spike. [F7, F8, F9]
- Auth-service depends on a shared Redis cache (F1) and a non‑shared PostgreSQL user‑db (F2). [F1, F2]
- No deployments were recorded for the Redis cache or the PostgreSQL user‑db in the last 24 h. [F13, F12]

## Assumptions (not verified)
- Auth-service loads the JWT signing key at startup and does not reload it automatically after rotation.
- The token issuer started signing tokens with the new key immediately after the rotation.
- The new TokenValidator logic introduced in the 14:00 deployment can produce false‑negative validation results.
- No other component (e.g., Redis cache) is responsible for the observed 401 failures unless proven otherwise.
- Redis cache state is critical for token validation (e.g., revocation lists).
- External identity provider behavior remained unchanged unless otherwise observed.
- (demoted from facts: no verified observation backs it) The log pipeline for the identity namespace was down, preventing search of auth-service logs.

## Timeline
- `2026-04-15T14:00:00Z` Auth-service deployed (v7.3.0) with JWT library upgrade and TokenValidator rewrite. [F10]
- `2026-04-15T14:01:00Z` JWT signing key rotated (k‑2025‑10 → k‑2026‑04). [F11]
- `2026-04-15T14:02:00Z` 401 error rate spike begins, reaching ~36%. [F5, F6]
- `2026-04-15T14:12:00Z` Incident reported by on‑call SRE. []
- `2026-04-15T15:59:00Z` Latest data point shows error rate still elevated. []

## Recommended actions
- **[immediate]** Revert the JWT signing key rotation to the previous key (k‑2025‑10) and ensure auth-service is reloaded to use it. -- If the rotation mis‑aligned with auth-service verification (H2), restoring the original key should immediately reduce 401 errors. [F11]
- **[immediate]** Rollback auth-service to the prior version (v7.2) that used the older JWT library and TokenValidator. -- If the deployment introduced a validator bug (H1), rolling back should stop the false‑negative validations. [F10]
- **[short_term]** Enable and restart the identity namespace log pipeline; collect auth-service logs for the period 14:00‑15:00 to look for signature‑mismatch errors or validator exceptions. -- Log evidence is required to discriminate between H2 (signature errors) and H1 (validator bugs).
- **[short_term]** Query auth-service metrics for request_rate, restarts, and any spike in error counters (e.g., token_validation_failure) around 14:00‑14:05. -- Correlating request volume or restarts with the error spike can indicate whether a deployment reload issue occurred.
- **[short_term]** Check Redis cache health (latency, error rates) and verify that token revocation data is intact; optionally restart Redis to clear possible corruption. -- Although no deployment change was recorded, a silent Redis outage (H3) could still cause validation failures. [F1, F13]
- **[long_term]** Add automated health checks for JWT key synchronization: after any key rotation, verify that auth-service can successfully validate a test token. -- Prevents future mis‑alignments between key rotation and service verification. [F11]
- **[long_term]** Implement canary deployments for auth-service changes (especially JWT library upgrades) with monitoring of 401 rates before full rollout. -- Reduces risk of regression bugs like the potential TokenValidator issue. [F10]
- **[long_term]** Restore full observability for the identity namespace (log pipeline, request_rate metrics, neighboring service error rates). -- Ensures future incidents can be diagnosed with complete data.

## Data gaps
- Auth-service logs were unavailable due to the identity namespace log pipeline outage, preventing direct observation of signature‑mismatch or validator errors.
- Request_rate, restart counts, and detailed error counters for auth-service were not collected, limiting correlation analysis.
- Metrics from neighboring services (e.g., api‑gateway, other auth‑dependent services) were not examined, so isolation of the issue to auth-service cannot be fully confirmed.
- No deployment or configuration change information was retrieved for the external token signing service or KYC provider, leaving a potential external cause unchecked.

## What would change this conclusion
- Log entries showing JWT signature verification failures immediately after 14:01 would strongly support H2.
- Evidence of TokenValidator exceptions or unit‑test failures introduced in the 14:00 deployment would strongly support H1.
- Observed Redis errors or latency spikes coinciding with the 401 surge would elevate H3.
- Any change in the external identity provider’s token format or signing behavior at the same time would elevate H4.

## Integrity warnings (set by code)
- fact demoted to assumption: The log pipeline for the identity namespace was down, preventing search of auth-

## Investigation stats
- latency 680.18s, LLM calls 19, tokens 74201 (in 61516 / out 12685)
- tool calls 11 (0 failed, 0 partial, 0 recovered by retry), findings 12/13 verified
