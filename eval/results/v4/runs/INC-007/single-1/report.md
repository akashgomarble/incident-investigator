# Incident investigation report: INC-007
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Users experience 401 invalid_token on ~36% of /token/validate calls, beginning sharply in the 14:00 bucket (auth-service error rate 0.22% -> 30.52%, sustained ~36%; api-gateway logs 466 'auth-service ... 401 invalid_token'). Two changes landed within one minute: dep-5710 (auth-service v7.3.0, JWT lib 0.11->0.12 'breaking parser API' + TokenValidator rewritten) at 14:00:00Z, and cfg-2705 (signing-key rotation k-2025-10 -> k-2026-04) at 14:01:00Z. The deploy precedes the config change and the onset, favoring the bad deployment (H1); the config rotation (H2) is one minute too late. Infra/resource causes are ruled out (redis-cache, user-db, auth CPU/mem/restarts all flat). Best hypothesis: bad_deployment dep-5710 with a rewritten JWT validator selectively rejecting valid tokens (mirrors HIST-109). Confidence 0.62 because auth-service logs are unavailable (pipeline down), so I cannot see the exact validation failure to fully separate it from the key-rotation cause.

## Probable root cause
**JWT library upgrade / TokenValidator rewrite (dep-5710) rejects a subset of valid tokens** -- bad_deployment in `auth-service`, triggered by `dep-5710` (confidence 0.62, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: JWT library upgrade / TokenValidator rewrite (dep-5710) rejects a subset of valid tokens | bad_deployment | auth-service | dep-5710 | 0.62 (medium) | active |
| 2 | H2: Scheduled signing-key rotation (cfg-2705) invalidated a subset of tokens | config_change | auth-service | cfg-2705 | 0.28 (low) | active |
| 3 | H3: Shared dependency (redis-cache) or infrastructure degradation | infrastructure_failure | redis-cache | - | 0.03 (low) | rejected |
| 4 | H4: Resource exhaustion / capacity saturation on auth-service | capacity_saturation | auth-service | - | 0.03 (low) | rejected |

### H1: JWT library upgrade / TokenValidator rewrite (dep-5710) rejects a subset of valid tokens
auth-service v7.3.0 deployed at 14:00 refactored token validation and upgraded jjwt 0.11->0.12 with a 'breaking parser API'. The rewritten validator mis-validates a subset of otherwise-valid tokens (e.g. changed clock-skew/claim-parsing behavior), producing persistent 401 invalid_token for ~36% of /token/validate calls. Fault originates in the auth-service release.

**Causal chain**
1. dep-5710 deployed to auth-service at 14:00:00Z [F3]
2. auth-service error rate jumps in the same 14:00 bucket (0.22%->30.52%) [F1]
3. downstream symptom surfaces as 401 invalid_token on /token/validate at the gateway [F2]
4. partial, stable ~36% failure with no resource/infra anomaly is consistent with selective validation logic failure [F5, F6, F7]

**Supporting evidence**
  - `F1` ✔ auth-service error rate jumped sharply from ~0.22% to 30.52% in the 14:00 bucket and stayed near 36% through 15:48 (last 38.23%).
  - `F2` ✔ api-gateway logs show 466 WARN entries, all the same pattern 'auth-service POST /v1/token/validate -> 401 invalid_token', first seen 14:02:22Z.
  - `F3` ✔ A deployment of auth-service (dep-5710, v7.3.0) landed at 14:00:00Z whose changes are 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; it is still live and not rolled back.
  - `F5` ✔ auth-service resource metrics were flat across onset: CPU ~38% (37.9-38.7), memory ~52% (51.25-52.89), restarts 0, request_rate ~400 req/s unchanged, latency_p99 ~90ms unchanged.
  - `F9` ✔ api-gateway error rate also jumped at 14:00 (0.16% -> 17.67%, sustained ~21%), while its latency_p99 stayed flat ~210ms.
  - `F11` ✔ Historical incident HIST-109 (2025-07-11) matches this symptom set: '401 invalid_token' after a JWT library upgrade that changed clock-skew default, category bad_deployment.
**Contradicting evidence**
  - `F4` ✔ A config change cfg-2705 rotated auth-service jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01:00Z, one minute AFTER incident onset.
**Assumptions**
  - A code path in the rewritten validator selectively fails tokens (e.g. skew/claim handling), which could not be confirmed because auth-service logs are unavailable.
  - The error_rate metric labeled '5xx' actually reflects the observed 401 failures.

### H2: Scheduled signing-key rotation (cfg-2705) invalidated a subset of tokens
cfg-2705 rotated jwt.signing.key_id from k-2025-10 to k-2026-04, publishing a new key id before all pods/validators loaded it (as in HIST-108), causing a fraction of tokens to be validated against the wrong key and return 401 invalid_token.

**Causal chain**
1. cfg-2705 rotated the signing key id at 14:01:00Z [F4]
2. tokens validated against a not-yet-propagated key return 401 invalid_token [F2]
3. auth-service error rate elevated ~36% [F1]

**Supporting evidence**
  - `F1` ✔ auth-service error rate jumped sharply from ~0.22% to 30.52% in the 14:00 bucket and stayed near 36% through 15:48 (last 38.23%).
  - `F2` ✔ api-gateway logs show 466 WARN entries, all the same pattern 'auth-service POST /v1/token/validate -> 401 invalid_token', first seen 14:02:22Z.
  - `F4` ✔ A config change cfg-2705 rotated auth-service jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01:00Z, one minute AFTER incident onset.
  - `F12` ✔ Historical incident HIST-108 (2025-10-01) matches the alternative: '401s after JWT signing key rotation', category config_change, caused by publishing a new key id before all pods loaded the JWKS.
**Contradicting evidence**
  - `F3` ✔ A deployment of auth-service (dep-5710, v7.3.0) landed at 14:00:00Z whose changes are 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; it is still live and not rolled back.
  - `F10` ✔ No JWKS/key/expired-related log lines were found in api-gateway logs (searches for 'jwks', 'key', 'expired' all returned 0 matches).
**Assumptions**
  - Key distribution is per-pod and lagged, so only some validations fail.
  - The rotation, though timestamped 14:01, could have been applied/effective slightly before its recorded time.

### H3: Shared dependency (redis-cache) or infrastructure degradation
A shared datastore (redis-cache) or infrastructure fault degraded auth-service token validation, causing 401s.

**Causal chain**
1. redis-cache degrades around 14:00 *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ Shared datastore redis-cache was healthy and unchanged across onset: hit_ratio ~96%, memory ~70%, evictions ~3/min.
  - `F5` ✔ auth-service resource metrics were flat across onset: CPU ~38% (37.9-38.7), memory ~52% (51.25-52.89), restarts 0, request_rate ~400 req/s unchanged, latency_p99 ~90ms unchanged.
  - `F7` ✔ user-db CPU stayed flat ~18% across onset, ruling out a database-side fault.
  - `F9` ✔ api-gateway error rate also jumped at 14:00 (0.16% -> 17.67%, sustained ~21%), while its latency_p99 stayed flat ~210ms.
**Rejected because:** redis-cache hit_ratio/memory/evictions and user-db CPU were all flat through onset; no infra anomaly (F6, F7, F5).

### H4: Resource exhaustion / capacity saturation on auth-service
auth-service ran out of CPU/memory/connections or was overloaded, producing validation failures.

**Causal chain**
1. auth-service resource saturation at 14:00 *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F5` ✔ auth-service resource metrics were flat across onset: CPU ~38% (37.9-38.7), memory ~52% (51.25-52.89), restarts 0, request_rate ~400 req/s unchanged, latency_p99 ~90ms unchanged.
  - `F8` ✔ auth-service logs are NOT retrievable: query returned backend_unavailable 503 after 3 attempts (matching the report's note that the identity log pipeline is down).
**Rejected because:** CPU ~38%, memory ~52%, request_rate ~400 req/s, restarts 0 all unchanged at onset (F5); failures are deterministic 401s not timeouts.

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- The 14:00 error-rate jump and the gateway 401s are the same event.
- cfg-2705's recorded timestamp approximates when it took effect (it postdates onset, weakening it as the cause).
- Neither dep-5710 nor cfg-2705 was rolled back before the current time (both show rolled_back_at: null).

## Timeline
- `2026-04-15T14:00:00Z` dep-5710 auth-service v7.3.0 deployed (jjwt 0.11->0.12 breaking parser API; TokenValidator rewritten) [F3]
- `2026-04-15T14:00:00Z` auth-service error_rate jumps 0.22% -> 30.52%, api-gateway error_rate 0.16% -> 17.67% in the same bucket [F1, F9]
- `2026-04-15T14:01:00Z` cfg-2705 signing-key rotation k-2025-10 -> k-2026-04 (one minute after onset) [F4]
- `2026-04-15T14:02:22Z` First api-gateway WARN 'auth-service POST /v1/token/validate -> 401 invalid_token' [F2]
- `2026-04-15T14:12:00Z` INC-007 reported; ~36% error rate sustained through 15:48 [F1]

## Recommended actions
- **[immediate]** Roll back auth-service to the pre-dep-5710 version (v7.3.0 -> previous) and confirm 401 rate drops within one bucket. -- The deploy is the earliest change and the leading hypothesis; a rollback is the fastest way to confirm cause and restore service. If 401s persist after rollback, escalate to H2 (key rotation). [F1, F3]
- **[immediate]** In parallel, verify JWKS/key propagation: check that all auth-service pods loaded k-2026-04 and consider re-publishing the previous key id if rotation is implicated. -- H2 is the live alternative; key-id mismatch produces identical 401 invalid_token symptoms (HIST-108). [F4, F12]
- **[short_term]** If rollback is not possible, prepare a compatibility fix for the jjwt 0.12 parser rewrite, especially clock-skew and claim-parsing defaults; pin explicit skew config. -- HIST-109 was the same class of regression fixed by pinning skew config. [F11]
- **[short_term]** Restore the identity namespace log pipeline so auth-service validation errors become searchable; without it root cause cannot be fully confirmed. -- auth-service logs returned 503, blocking direct confirmation of the exact validation failure. [F8]

## Data gaps
- auth-service logs are unretrievable (backend 503, identity log pipeline down) - cannot see the exact validator error (key-not-found vs skew/claim vs parse failure) that would definitively separate H1 from H2.
- error_rate metric is labeled '% of requests (HTTP 5xx)' yet the observed failures are 401s; the metric's semantics vs true 401 count are unconfirmed.
- No per-key or per-pod breakdown of which tokens fail, so the '~1/3 partial' scope cannot be attributed to a specific key, pod, or token cohort.
- No access to auth-service internal logs to confirm whether dep-5710 introduced a skew/claim regression versus a JWKS/propagation issue.

## Investigation stats
- latency 65.37s, LLM calls 6, tokens 87532 (in 78929 / out 8603)
- tool calls 27 (3 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
