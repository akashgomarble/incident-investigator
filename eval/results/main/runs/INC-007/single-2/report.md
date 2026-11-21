# Incident investigation report: INC-007
*System: single_agent | outcome: **inconclusive** | ended: agent_submitted*

## Summary
Users see 401 invalid_token on ~36% of auth-service token validations starting 14:02:22Z, a stable plateau that also drives api-gateway to ~21% errors. Onset is tightly bracketed by two coincident auth-service changes: code deploy dep-5710 (v7.3.0, jjwt 0.11->0.12 breaking parser API, TokenValidator rewritten) at 14:00:00Z and config change cfg-2705 (jwt signing key rotation k-2025-10 -> k-2026-04) at 14:01:00Z. All resource, latency, restart, rate-limit and redis-cache metrics are flat, ruling out resource exhaustion, capacity saturation, and dependency failure; market-data-service's earlier 11:35 deploy is unrelated (unaffected). Both remaining hypotheses (bad_deployment from the JWT library rewrite vs config_change from the signing-key rotation) are plausible and cannot be separated because auth-service's own logs are unavailable (503 log-pipeline outage), so signature/key-id failures cannot be distinguished from parser/validation failures. Outcome is inconclusive; the two candidate triggers are identified and all alternatives ruled out.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: auth-service v7.3.0 JWT library upgrade broke token validation for a subset of tokens | bad_deployment | auth-service | dep-5710 | 0.40 (low) | active |
| 2 | H2: auth-service signing-key rotation (cfg-2705) not fully propagated caused 401s | config_change | auth-service | cfg-2705 | 0.40 (low) | active |
| 3 | H3: Dependency (redis-cache / key material store) failure | dependency_failure | redis-cache | - | 0.03 (low) | rejected |
| 4 | H4: Resource exhaustion / capacity saturation at auth-service | resource_exhaustion | auth-service | - | 0.02 (low) | rejected |

### H1: auth-service v7.3.0 JWT library upgrade broke token validation for a subset of tokens
dep-5710 (v7.3.0, 14:00) upgraded jjwt 0.11->0.12 with a breaking parser API and rewrote TokenValidator; the rewritten validation logic rejects a stable ~36% of otherwise-valid tokens, returning 401 invalid_token, matching the HIST-109 pattern (JWT library upgrade changed clock-skew default).

**Causal chain**
1. dep-5710 deployed to auth-service at 14:00:00Z [F4]
2. JWT parser API change / rewritten TokenValidator alters validation behavior [F4]
3. a subset of tokens fails validation -> 401 invalid_token beginning 14:02:22Z [F2, F1]
4. caller api-gateway surfaces ~21% errors [F3]

**Supporting evidence**
  - `F4` ✔ Code deploy dep-5710 to auth-service v7.3.0 at 14:00:00Z (author d.verma) upgraded jjwt 0.11->0.12 with a breaking parser API and rewrote TokenValidator; status is live and rolled_back_at is null.
  - `F1` ✔ auth-service HTTP 5xx error rate jumps from ~0.2% to ~36% in the 14:00 bucket and stays on a stable ~36% plateau through 15:48; pre-onset buckets 13:00-13:48 are all ~0.2%.
  - `F2` ✔ api-gateway logs show 401 invalid_token on auth-service POST /v1/token/validate beginning at 14:02:22Z; all entries are level WARN (no ERROR), and the same pattern persists at least to 15:58:51Z.
  - `F3` ✔ api-gateway (the only upstream caller of auth-service) also degrades at the same time: error_rate goes from ~0.2% to ~21% starting in the 14:00 bucket.
  - `F10` ✔ Historical incident HIST-109 (2025-07-11) was a token-validation regression with identical symptoms (401 invalid_token) caused by a JWT library upgrade changing the clock-skew default (bad_deployment), resolved by rollback. HIST-108 (2025-10-01) was 401s after signing-key rotation caused by publishing a new key id before all pods loaded the JWKS (config_change).
  - `F11` ✔ Only two changes to any service in the incident window (auth-service dep-5710 at 14:00 and cfg-2705 at 14:01); no other service deployed or changed config between 13:00 and 15:59.
**Contradicting evidence**
  - `F5` ✔ Config change cfg-2705 rotated auth-service jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01:00Z, described as a scheduled signing-key rotation executed by security-automation.
**Assumptions**
  - The jjwt 0.12 parser rewrite actually changed acceptance behavior for a token subset (e.g. clock-skew, claim/alg handling).
  - Onset is caused by this deploy rather than the key rotation 1 minute later.

### H2: auth-service signing-key rotation (cfg-2705) not fully propagated caused 401s
cfg-2705 (14:01) rotated jwt.signing.key_id k-2025-10 -> k-2026-04; the new key id was published before all auth-service pods/consumers loaded the corresponding key material (JWKS), so tokens under the old/new key id are rejected, returning 401 invalid_token, matching HIST-108.

**Causal chain**
1. cfg-2705 rotated signing key_id at 14:01:00Z [F5]
2. new key id propagated inconsistently across validation paths [F5]
3. tokens failing signature/key-id check -> 401 invalid_token from 14:02:22Z [F2, F1]
4. api-gateway propagates ~21% errors [F3]

**Supporting evidence**
  - `F5` ✔ Config change cfg-2705 rotated auth-service jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01:00Z, described as a scheduled signing-key rotation executed by security-automation.
  - `F1` ✔ auth-service HTTP 5xx error rate jumps from ~0.2% to ~36% in the 14:00 bucket and stays on a stable ~36% plateau through 15:48; pre-onset buckets 13:00-13:48 are all ~0.2%.
  - `F2` ✔ api-gateway logs show 401 invalid_token on auth-service POST /v1/token/validate beginning at 14:02:22Z; all entries are level WARN (no ERROR), and the same pattern persists at least to 15:58:51Z.
  - `F3` ✔ api-gateway (the only upstream caller of auth-service) also degrades at the same time: error_rate goes from ~0.2% to ~21% starting in the 14:00 bucket.
  - `F10` ✔ Historical incident HIST-109 (2025-07-11) was a token-validation regression with identical symptoms (401 invalid_token) caused by a JWT library upgrade changing the clock-skew default (bad_deployment), resolved by rollback. HIST-108 (2025-10-01) was 401s after signing-key rotation caused by publishing a new key id before all pods loaded the JWKS (config_change).
  - `F11` ✔ Only two changes to any service in the incident window (auth-service dep-5710 at 14:00 and cfg-2705 at 14:01); no other service deployed or changed config between 13:00 and 15:59.
**Contradicting evidence**
  - `F4` ✔ Code deploy dep-5710 to auth-service v7.3.0 at 14:00:00Z (author d.verma) upgraded jjwt 0.11->0.12 with a breaking parser API and rewrote TokenValidator; status is live and rolled_back_at is null.
**Assumptions**
  - Key material for k-2026-04 was not available on all verification paths at 14:01-14:02.
  - Onset is caused by this rotation rather than the deploy 1 minute earlier.

### H3: Dependency (redis-cache / key material store) failure
A shared dependency (redis-cache) failed and caused validation failures.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ redis-cache (an auth-service dependency) shows no degradation at onset: hit_ratio_pct stays ~96% across the window.
  - `F6` ✔ auth-service shows no resource pressure at onset: cpu_pct stays ~38%, memory_pct ~52%, latency_p99_ms ~90ms, rate_limited_per_min ~2/min, request_rate ~400 req/s, and restarts remain 0 throughout the window.
**Rejected because:** redis-cache hit_ratio_pct is flat ~96% and healthy across the window (F9); no evidence of cache or dependency fault.

### H4: Resource exhaustion / capacity saturation at auth-service
auth-service ran out of CPU/memory/connections and failed requests.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ auth-service shows no resource pressure at onset: cpu_pct stays ~38%, memory_pct ~52%, latency_p99_ms ~90ms, rate_limited_per_min ~2/min, request_rate ~400 req/s, and restarts remain 0 throughout the window.
**Rejected because:** CPU (~38%), memory (~52%), latency (~90ms), rate-limit (~2/min) and restarts (0) are all flat across onset (F6); request_rate is steady ~400/s with no load spike (capacity ruled out).

## Facts (verified observations)
- auth-service 5xx error rate rose from ~0.2% to a stable ~36% starting in the 14:00 bucket, coincident with the incident onset. [F1]
- 401 invalid_token responses on POST /v1/token/validate begin at 14:02:22Z and persist, seen in api-gateway logs at WARN level. [F2]
- api-gateway, the sole upstream caller of auth-service, degrades in lockstep to ~21% error rate. [F3]
- auth-service received a JWT library upgrade deploy (dep-5710, v7.3.0) at 14:00:00Z that rewrote TokenValidator with a breaking parser API. [F4]
- auth-service signing key_id was rotated (cfg-2705) at 14:01:00Z from k-2025-10 to k-2026-04. [F5]
- auth-service showed no CPU, memory, latency, rate-limit or restart anomalies at onset. [F6]
- market-data-service, which deployed earlier at 11:35, was unaffected (error rate flat ~0.2%), excluding its deploy as a cause. [F7]
- auth-service application logs were unretrievable (503), preventing discrimination between failure modes. [F8]
- redis-cache, an auth-service dependency, remained healthy (hit ratio ~96%), excluding a cache dependency failure. [F9]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-04-15T11:35:00Z` market-data-service deploys v5.0.3 (unrelated; service stays healthy). [F7]
- `2026-04-15T14:00:00Z` auth-service deploys v7.3.0 (dep-5710): jjwt 0.11->0.12, TokenValidator rewritten. [F4]
- `2026-04-15T14:01:00Z` auth-service config change cfg-2705 rotates jwt.signing.key_id k-2025-10 -> k-2026-04. [F5]
- `2026-04-15T14:02:22Z` First 401 invalid_token on auth-service /v1/token/validate appears in api-gateway logs. [F2]
- `2026-04-15T14:12:00Z` auth-service error rate reads ~36%, api-gateway ~21%; incident reported as INC-007. [F1, F3]
- `2026-04-15T15:48:00Z` Error rate still ~36% (no self-recovery), deploy remains live and not rolled back. [F1]

## Recommended actions
- **[immediate]** Obtain the actual auth-service validation error detail (exception class/message) via direct pod access or log shipping bypass, to distinguish signature/key-id failure from parser/validation failure. -- This single piece of evidence separates H1 from H2; auth-service logs are currently unretrievable (503). [F8]
- **[immediate]** If the failure is a recent regression, roll back dep-5710 (v7.3.0 -> previous version) and measure whether 401s recover. -- Fastest test of the bad_deployment hypothesis; HIST-109 was resolved by rollback. [F4, F10]
- **[immediate]** Re-publish/restore the previous signing key (k-2025-10) and confirm JWKS propagation to all validation paths as an independent test of cfg-2705. -- Directly tests the config_change hypothesis; HIST-108 resolution was re-publishing the previous key. [F5, F10]
- **[short_term]** Decouple future deploys from scheduled key rotations (do not ship both in the same minute) and stage key rotations with propagated JWKS. -- The two coincident changes are what makes this incident ambiguous. [F4, F5]
- **[short_term]** Restore the identity-namespace log pipeline and add alerting that fires specifically on invalid_token rate. -- Log outage blocked root-cause determination and onset detection was delayed to the report. [F8, F2]

## Data gaps
- auth-service application logs (and any JWKS/key-load messages) are unretrievable due to the identity-namespace log-pipeline outage (503), so the precise validation failure mode is unknown.
- No visibility into per-pod rollout state for dep-5710 vs the timing of keyset propagation for cfg-2705, so the two triggers cannot be temporally separated below the 1-minute gap.
- No metric decomposes the 36% failures by token issuer/key-id/version, which would directly attribute failures to key-id vs parser behavior.

## Integrity warnings (set by code)
- fact: removed citations to non-existent findings ['F25']

## Investigation stats
- latency 54.06s, LLM calls 7, tokens 90793 (in 81763 / out 9030)
- tool calls 29 (4 failed, 0 partial, 0 recovered by retry), findings 13/13 verified
