# Incident investigation report: INC-007
*System: single_agent | outcome: **inconclusive** | ended: agent_submitted*

## Summary
Users are logged out / 401 invalid_token on ~36% of auth-service token validations, beginning abruptly between 13:59 and 14:02 on 2026-04-15 and holding on a flat ~36% plateau through the latest data (15:59), with no recovery and no restarts. The symptom is confined to auth-service's token/validate path (api-gateway reflects it as ~21% errors; no other downstream, datastore or external provider shows anomalies; all resource metrics flat). Two changes bracket the onset within one minute: code deployment dep-5710 (auth-service v7.3.0, 14:00:00Z — jjwt 0.11→0.12 breaking parser API, 'TokenValidator rewritten') and config change cfg-2705 (jwt.signing.key_id k-2025-10→k-2026-04, 14:01:00Z, 'Scheduled signing-key rotation'). Both have near-exact historical precedents producing the identical symptom (HIST-108 key rotation / config_change; HIST-109 library upgrade / bad_deployment). The only source that would reveal WHY a token is rejected ('unknown kid' vs parser/claim error) is auth-service's own logs, which are unsearchable (503, consistent with the reported identity-namespace log-pipeline outage). Because the two triggers are co-timed and the discriminator is unavailable, I cannot mechanically separate them, so the outcome is inconclusive with key rotation ranked marginally first (most recent-on-point precedent match and a stable partial-failure fraction), JWT library upgrade a close second.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: JWT signing-key rotation published new key_id before all pods/JWKS loaded → partial 'invalid_token' 401s | config_change | auth-service | cfg-2705 | 0.45 (low) | active |
| 2 | H2: JWT library upgrade (dep-5710) rewrote TokenValidator with a breaking parser API → partial 401 invalid_token | bad_deployment | auth-service | dep-5710 | 0.40 (low) | active |
| 3 | H3: Shared dependency (redis-cache/JWKS cache or user-db) degraded → validation failures | dependency_failure | redis-cache | - | 0.05 (low) | rejected |
| 4 | H4: Resource exhaustion / capacity saturation on auth-service | resource_exhaustion | auth-service | - | 0.03 (low) | rejected |

### H1: JWT signing-key rotation published new key_id before all pods/JWKS loaded → partial 'invalid_token' 401s
cfg-2705 rotated jwt.signing.key_id to k-2026-04 at 14:01, but the new key material was not loaded by all auth-service replicas/JWKS consumers, so a stable fraction (~36%) of token validations referencing the new key_id fail with 401 invalid_token. Fault originates in auth-service configuration change.

**Causal chain**
1. cfg-2705 sets jwt.signing.key_id=k-2026-04 at 14:01 [F5]
2. Not all replicas/JWKS consumers updated, so a fixed subset of validations cannot resolve the key *(assumed)*
3. token/validate returns 401 invalid_token at a stable ~36% [F1, F3]
4. api-gateway surfaces the propagated 401s as ~21% errors [F2]

**Supporting evidence**
  - `F1` ✔ auth-service error_rate jumped abruptly from ~0.2% (13:59 bucket 0.21%) to ~36% at 14:00-14:02 and stayed on a flat plateau (~36%) through 15:59 with no recovery.
  - `F3` ✔ api-gateway WARN logs show a single recurring pattern: 'auth-service POST /v1/token/validate -> 401 invalid_token', 466 matches, first_seen 14:02:22Z, and no other error pattern.
  - `F5` ✔ A config change cfg-2705 to auth-service changed jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01:00Z, described as 'Scheduled signing-key rotation' by security-automation.
  - `F8` ✔ Historical incident HIST-108 (2025-10-01) is a near-exact match: '401s after JWT signing key rotation', root cause 'Key rotation published new key id before all pods loaded the JWKS', category config_change; HIST-109 is a competing match: 'auth library upgrade' → 401 invalid_token, category bad_deployment.
  - `F2` ✔ api-gateway error_rate jumped at the same 14:00 boundary from ~0.2% to ~21% and stayed flat; its request_rate (~1260 req/s) was unchanged, so the fault is not gateway-side capacity.
**Contradicting evidence**
  - `F4` ✔ A code deployment dep-5710 to auth-service v7.3.0 occurred at 14:00:00Z: 'Upgrade JWT library to 4.x; refactor token validation', changes incl. 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live, not rolled back.
**Assumptions**
  - A key rotation can leave a stable (non-recovering) fraction of tokens invalid if JWKS/key loading is staggered and never reconciles
  - The 14:01 config timestamp is the effective activation time of the new key_id

### H2: JWT library upgrade (dep-5710) rewrote TokenValidator with a breaking parser API → partial 401 invalid_token
dep-5710 (auth-service v7.3.0, 14:00) replaced jjwt 0.11 with 0.12 and rewrote TokenValidator; the new parser mishandles a subset of tokens (e.g. a claim/header/algorithm/skew default), producing a stable ~36% 401 invalid_token. Fault originates in the released code.

**Causal chain**
1. dep-5710 deploys at 14:00 with 'TokenValidator rewritten' and breaking jjwt API change [F4]
2. New parser rejects a subset of otherwise-valid tokens *(assumed)*
3. token/validate returns 401 invalid_token at a stable ~36% [F1, F3]
4. api-gateway surfaces propagated 401s as ~21% errors [F2]

**Supporting evidence**
  - `F1` ✔ auth-service error_rate jumped abruptly from ~0.2% (13:59 bucket 0.21%) to ~36% at 14:00-14:02 and stayed on a flat plateau (~36%) through 15:59 with no recovery.
  - `F3` ✔ api-gateway WARN logs show a single recurring pattern: 'auth-service POST /v1/token/validate -> 401 invalid_token', 466 matches, first_seen 14:02:22Z, and no other error pattern.
  - `F4` ✔ A code deployment dep-5710 to auth-service v7.3.0 occurred at 14:00:00Z: 'Upgrade JWT library to 4.x; refactor token validation', changes incl. 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live, not rolled back.
  - `F8` ✔ Historical incident HIST-108 (2025-10-01) is a near-exact match: '401s after JWT signing key rotation', root cause 'Key rotation published new key id before all pods loaded the JWKS', category config_change; HIST-109 is a competing match: 'auth library upgrade' → 401 invalid_token, category bad_deployment.
  - `F2` ✔ api-gateway error_rate jumped at the same 14:00 boundary from ~0.2% to ~21% and stayed flat; its request_rate (~1260 req/s) was unchanged, so the fault is not gateway-side capacity.
**Contradicting evidence**
  - `F5` ✔ A config change cfg-2705 to auth-service changed jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01:00Z, described as 'Scheduled signing-key rotation' by security-automation.
**Assumptions**
  - A parser rewrite can fail a stable subset of tokens rather than all tokens
  - No clock-skew/config pin masked the failure

### H3: Shared dependency (redis-cache/JWKS cache or user-db) degraded → validation failures
A shared datastore used by auth-service (redis-cache for token/key cache, or user-db) degraded at ~14:00, causing token validation to fail 401.

**Causal chain**
1. redis-cache or user-db fault at ~14:00 *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ No resource saturation or restarts: auth-service cpu_pct, memory_pct, latency_p99_ms, request_rate and rate_limited_per_min were all flat across the incident, and restarts was 0 for both auth-service and api-gateway.
  - `F7` ✔ auth-service logs are not retrievable (backend unavailable, HTTP 503 after 3 attempts), and user-db/redis-cache show only benign warnings (GC pauses, TLS cipher, clock drift); kyc-provider has no telemetry.
  - `F9` ✔ The failure is confined to the auth-service token/validate path; no other downstream of api-gateway produced errors and user-db/redis-cache/kyc-provider were healthy.
**Assumptions**
  - A cache miss could surface as 401 rather than a 5xx/error
**Rejected because:** redis-cache cpu_pct/hit_ratio were flat and user-db had only benign warnings; auth-service latency and resource metrics unchanged, so no dependency degradation is visible.

### H4: Resource exhaustion / capacity saturation on auth-service
auth-service ran out of CPU/memory/connections or was saturated by load, causing validation failures.

**Causal chain**
1. auth-service resource saturation at 14:00 *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ No resource saturation or restarts: auth-service cpu_pct, memory_pct, latency_p99_ms, request_rate and rate_limited_per_min were all flat across the incident, and restarts was 0 for both auth-service and api-gateway.
  - `F2` ✔ api-gateway error_rate jumped at the same 14:00 boundary from ~0.2% to ~21% and stayed flat; its request_rate (~1260 req/s) was unchanged, so the fault is not gateway-side capacity.
**Rejected because:** cpu_pct (~38%), memory_pct (~52%), latency_p99 (~90ms), request_rate (~400 req/s) and restarts (0) were all flat across onset; no saturation.

## Facts (verified observations)
- auth-service error_rate rose abruptly from ~0.2% (13:59 = 0.21%) to ~36% at 14:00-14:02 and stayed on a flat ~36% plateau through 15:59 with no recovery. [F1]
- api-gateway error_rate rose at the same 14:00 boundary from ~0.2% to ~21% while its request_rate (~1260 req/s) was unchanged. [F2]
- The only error pattern in api-gateway logs is 'auth-service POST /v1/token/validate -> 401 invalid_token' (466 matches, first_seen 14:02:22Z). [F3]
- auth-service deployment dep-5710 (v7.3.0) occurred at 14:00:00Z with a breaking jjwt 0.11→0.12 parser API change and a rewritten TokenValidator, and was not rolled back. [F4]
- auth-service config change cfg-2705 rotated jwt.signing.key_id to k-2026-04 at 14:01:00Z via a scheduled security-automation rotation. [F5]
- auth-service and api-gateway resource metrics were flat and restarts were 0 throughout the incident. [F6]
- auth-service logs are unavailable (503) and no other service shows a causal anomaly. [F7]
- Two past incidents with the identical 401 invalid_token symptom exist: HIST-108 (key rotation, config_change) and HIST-109 (JWT library upgrade, bad_deployment). [F8]

## Assumptions (not verified)
- A key rotation can leave a stable, non-recovering fraction of tokens invalid if key/JWKS loading across replicas is staggered and never reconciles.
- A JWT parser rewrite can reject a stable subset of tokens rather than all tokens.
- The config timestamp (14:01) equals the effective activation time of the new key_id, and the deploy timestamp (14:00) equals effective code activation.
- Absence of observed anomalies in a healthy datastore implies the datastore is not causal.

## Timeline
- `2026-04-15T11:35:00Z` dep-5699 market-data-service v5.0.3 deployed (unrelated; no auth impact observed) []
- `2026-04-15T13:59:00Z` Last normal auth-service error_rate bucket (0.21%) [F1]
- `2026-04-15T14:00:00Z` dep-5710 auth-service v7.3.0 deployed (JWT library upgrade, TokenValidator rewritten) [F4]
- `2026-04-15T14:00:00Z` auth-service error_rate jumps to 31.8% and api-gateway to 18.4% [F1, F2]
- `2026-04-15T14:01:00Z` cfg-2705 rotates jwt.signing.key_id to k-2026-04 [F5]
- `2026-04-15T14:02:22Z` First api-gateway '401 invalid_token' WARN log for token/validate [F3]
- `2026-04-15T14:12:00Z` Incident INC-007 reported (SEV-1) []
- `2026-04-15T15:59:00Z` Error rate still flat at ~36% (no recovery); no rollback recorded [F1]

## Recommended actions
- **[immediate]** Restore observability first: bring back the identity-namespace log pipeline so auth-service token-validation logs (which reason a token is rejected — e.g. unknown kid vs parser/claim error) become readable. -- auth-service logs are the single discriminator between the two co-timed candidates and are currently unsearchable (503). [F7]
- **[immediate]** A/B the two triggers: revert cfg-2705 (re-publish previous key_id k-2025-10 / staged rotation) and observe whether the ~36% 401s clear; if not, roll back dep-5710 (v7.3.0). -- The config change is the cheapest reversible lever and matches the most recent-on-point precedent (HIST-108); separating the two isolates the culprit. [F4, F5, F8]
- **[immediate]** If config rollback does not recover, roll back dep-5710 to the prior auth-service version and confirm error_rate returns to ~0.2%. -- Directly tests H2 (bad_deployment) and restores service if the parser rewrite is the fault. [F4, F1]
- **[long_term]** Add a canary/staged-rollout gate and a JWKS/key-load readiness check before key rotations go live, so a new key_id is never published before all replicas can validate against it. -- Prevents recurrence of the HIST-108 pattern regardless of which candidate is at fault. [F8]
- **[long_term]** Instrument auth-service token validation with a reason code (unknown_kid / signature / expired / claim / parse) exported to metrics, and emit a normal 4xx counter separate from HTTP 5xx. -- Removes reliance on searchable logs for future 401 incidents and clarifies the current 5xx-labelled error_rate. [F1, F3]

## Data gaps
- auth-service logs are unsearchable (backend 503 after 3 attempts) — the internal reason a token is rejected (unknown kid vs parser/claim/clock-skew error) cannot be retrieved, which is the key discriminator between H1 and H2.
- No rollback or config-revert has been attempted/recorded, so recovery-on-revert evidence is unavailable to separate the two co-timed triggers.
- kyc-provider (external, called by auth-service) has no telemetry, so it cannot be fully excluded as a contributor; it is only observable indirectly via auth-service.
- No per-key-id / per-token-version breakdown of validations exists, so whether the failing ~36% is a fixed subset (key-related) vs a token-feature subset (parser-related) cannot be confirmed.
- auth-service's error_rate unit is labelled '% of requests (HTTP 5xx)' yet the observed failures are 401 (client errors) — the mismatch is unexplained and could indicate the metric aggregates differently than labelled.

## Investigation stats
- latency 72.72s, LLM calls 12, tokens 190603 (in 179539 / out 11064)
- tool calls 42 (4 failed, 0 partial, 0 recovered by retry), findings 9/9 verified
