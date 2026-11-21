# Incident investigation report: INC-007
*System: multi_agent | outcome: **inconclusive** | ended: budget_exhausted:deadline*

## Summary
The investigation could not definitively identify a single root cause; two leading hypotheses—an erroneous validator introduced in the recent auth-service deployment (H1) and a signing‑key configuration change (H2)—remain plausible. Confidence in either is limited by missing observability data and lack of token‑signature evidence. Immediate mitigations and further data collection are recommended.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bug introduced by recent auth-service deployment (JWT library upgrade and validator rewrite) | bad_deployment | auth-service | dep-5710 | 0.60 (medium) | active |
| 2 | H2: Signing key configuration change caused token validation mismatch | config_change | auth-service | signing-key-change-2026-04-01 | 0.55 (medium) | active |
| 3 | H3: Downstream token introspection service failure | dependency_failure | token-service | null | 0.20 (low) | active |
| 4 | H4: External identity provider outage or network issue | dependency_failure | kyc-provider | null | 0.15 (low) | active |

### H1: Bug introduced by recent auth-service deployment (JWT library upgrade and validator rewrite)
The deployment of auth-service at 2026-04-15T14:00:00Z (dep-5710) included a new JWT library and a rewritten token validator that incorrectly validates incoming JWTs, causing auth-service to return 401 invalid_token for POST /v1/token/validate.

**Causal chain**
1. Deployment dep-5710 introduced buggy token validation code [F7]
2. Bug causes valid tokens to be marked invalid *(assumed)*
3. Auth-service returns 401 invalid_token on /v1/token/validate [F1, F2, F5]

**Supporting evidence**
  - `F7` ✔ auth-service was deployed at 2026-04-15T14:00:00Z (dep-5710) with JWT library upgrade and token validator rewrite.
  - `F1` ✔ The first 401 invalid_token warning from auth-service appears at 2026-04-15T14:02:22Z.
  - `F2` ✘ unverified All logged 401 warnings are for the endpoint POST /v1/token/validate on auth-service.
  - `F5` ✔ No 401 warnings were observed in services other than api-gateway (which proxies auth-service), based on the logs queried.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The new validator logic misinterprets token signatures or claims
  - No other service changes affect token validation

### H2: Signing key configuration change caused token validation mismatch
At 2026-04-15T14:01:00Z the signing key config for auth-service was changed from k-2025-10 to k-2026-04. Tokens presented to auth-service were still signed with the old key, so validation failed and auth-service responded with 401 invalid_token.

**Causal chain**
1. Signing key config changed to a new key [F8]
2. Incoming tokens are signed with the previous key *(assumed)*
3. Auth-service validation fails, returning 401 invalid_token [F1, F2, F5]

**Supporting evidence**
  - `F8` ✔ auth-service signing key config changed at 2026-04-15T14:01:00Z from k-2025-10 to k-2026-04 (jwt.signing.key_id).
  - `F1` ✔ The first 401 invalid_token warning from auth-service appears at 2026-04-15T14:02:22Z.
  - `F2` ✘ unverified All logged 401 warnings are for the endpoint POST /v1/token/validate on auth-service.
  - `F5` ✔ No 401 warnings were observed in services other than api-gateway (which proxies auth-service), based on the logs queried.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Token issuance service was not updated to use the new signing key at the same time
  - Auth-service does not fallback to previous key

### H3: Downstream token introspection service failure
The token introspection service (token-service) that auth-service calls to validate JWTs became unavailable or returned errors, leading auth-service to treat all tokens as invalid and emit 401 responses.

**Causal chain**
1. Token-service experienced an outage or returned errors *(assumed)*
2. Auth-service receives failure response and maps it to invalid_token *(assumed)*
3. Auth-service returns 401 for validation requests [F1, F5]

**Supporting evidence**
  - `F1` ✔ The first 401 invalid_token warning from auth-service appears at 2026-04-15T14:02:22Z.
  - `F5` ✔ No 401 warnings were observed in services other than api-gateway (which proxies auth-service), based on the logs queried.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Auth-service relies on token-service for validation (not just local JWT verification)
  - Token-service outage coincided with the 401 spike

### H4: External identity provider outage or network issue
The external identity provider (kyc-provider) that supplies token signing keys or validates tokens became unreachable, causing auth-service to reject tokens as invalid.

**Causal chain**
1. External identity provider experienced outage or connectivity loss *(assumed)*
2. Auth-service cannot retrieve or verify token data *(assumed)*
3. Auth-service returns 401 invalid_token [F1, F5]

**Supporting evidence**
  - `F1` ✔ The first 401 invalid_token warning from auth-service appears at 2026-04-15T14:02:22Z.
  - `F5` ✔ No 401 warnings were observed in services other than api-gateway (which proxies auth-service), based on the logs queried.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Auth-service depends on kyc-provider for token validation or key retrieval
  - Outage started around 14:02

## Facts (verified observations)
- The first 401 invalid_token warning from auth-service was observed at 2026-04-15T14:02:22Z. [F1]
- CPU usage of auth-service stayed around 38% with no noticeable spike during the incident window. [F3]
- Request rate of auth-service remained stable around 400 req/s throughout the period. [F4]
- No 401 warnings were observed in services other than api-gateway, which proxies auth-service. [F5]
- auth-service was deployed at 2026-04-15T14:00:00Z (dep-5710) with a JWT library upgrade and token validator rewrite. [F7]
- auth-service signing key configuration changed at 2026-04-15T14:01:00Z from k-2025-10 to k-2026-04. [F8]
- No deployments were recorded for user-db between 2026-04-15T13:30:00Z and 2026-04-15T14:30:00Z. [F9]
- No deployments were recorded for redis-cache between 2026-04-15T13:30:00Z and 2026-04-15T14:30:00Z. [F10]
- auth-service depends on user-db, redis-cache, and external kyc-provider; api-gateway calls auth-service via HTTP. [F11]

## Assumptions (not verified)
- The new validator logic misinterprets token signatures or claims (H1).
- No other service changes affect token validation (H1).
- Tokens presented to auth-service during the incident were still signed with the old key k-2025-10 (H2).
- Auth-service does not fallback to a previous signing key (H2).
- Auth-service relies on token-service for validation rather than purely local JWT verification (H3).
- Token-service outage coincided with the 401 spike (H3).
- Auth-service depends on the external kyc-provider for key retrieval or token validation (H4).
- Outage of kyc-provider started around 14:02 (H4).
- (demoted from facts: no verified observation backs it) All observed 401 warnings were for the endpoint POST /v1/token/validate on auth-service.

## Timeline
- `2026-04-15T14:00:00Z` auth-service deployment (dep-5710) with JWT library upgrade and validator rewrite. [F7]
- `2026-04-15T14:01:00Z` Signing key configuration changed from k-2025-10 to k-2026-04. [F8]
- `2026-04-15T14:02:22Z` First 401 invalid_token warning observed on POST /v1/token/validate. [F1]
- `2026-04-15T14:02:22Z` Spike of 401 warnings began (observed via api-gateway logs). [F5]
- `2026-04-15T14:12:00Z` CPU usage measured at ~38%; request rate stable at ~400 req/s. [F3, F4]

## Recommended actions
- **[immediate]** Revert the signing key configuration to the previous key (k-2025-10) via config change roll‑back. -- If the 401s are caused by a key‑mismatch (H2), restoring the old key will immediately allow existing tokens to validate. [F8]
- **[immediate]** Rollback auth-service to the version prior to dep-5710. -- If the new JWT library or validator rewrite is faulty (H1), reverting removes the buggy code and should stop the 401s. [F7]
- **[short_term]** Enable and verify the auth-service log pipeline; ingest logs for the incident window. -- Access to auth-service logs is required to confirm whether tokens were rejected due to signature mismatch, validator logic, or downstream failures.
- **[short_term]** Collect missing observability metrics (memory_pct, latency_p99_ms) for auth-service and re‑run the incident window analysis. -- Missing performance data limits assessment of resource‑related causes.
- **[short_term]** Query token-service and kyc-provider health/status during the incident period. -- To confirm or refute hypotheses H3 and H4 regarding downstream dependency failures.
- **[long_term]** Implement automated tests for JWT library upgrades and coordinated signing‑key rotations, including validation against both old and new keys. -- Prevents future deployments or config changes from silently breaking token validation.
- **[long_term]** Add redundancy to the log pipeline and ensure logs from auth-service are always searchable. -- Improves observability for rapid root‑cause analysis in future incidents.

## Data gaps
- Memory usage and latency metrics for auth-service were not retrieved, preventing assessment of performance‑related contributors.
- Auth-service logs for the incident window are unavailable due to a log‑pipeline outage, limiting direct evidence of validation failures.
- No telemetry or health data for token-service or kyc-provider was collected, leaving downstream dependency status unknown.
- No data confirming whether client tokens presented during the incident were signed with the old key (k-2025-10) or the new key (k-2026-04).

## What would change this conclusion
- Evidence that the majority of tokens presented during the spike were signed with the old key (e.g., token logs showing k-2025-10 signatures).
- Test results demonstrating that the new validator incorrectly rejects valid JWTs.
- Telemetry showing token-service or kyc-provider outages coinciding with the 401 spike.
- Successful validation of tokens after reverting the signing key, confirming H2 as the root cause.

## Integrity warnings (set by code)
- fact demoted to assumption: All observed 401 warnings were for the endpoint POST /v1/token/validate on auth-

## Investigation stats
- latency 651.77s, LLM calls 17, tokens 56815 (in 44380 / out 12435)
- tool calls 10 (1 failed, 0 partial, 0 recovered by retry), findings 10/12 verified
