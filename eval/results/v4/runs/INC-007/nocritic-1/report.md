# Incident investigation report: INC-007
*System: multi_agent | outcome: **inconclusive** | ended: critic_disabled*

## Summary
The incident is real and unambiguous: auth-service's error rate stepped from ~0.2% to 36.08% in the 14:02Z bucket and has stayed flat at ~36% through 15:55Z, with no load, latency, or CPU change, so it is a quality-of-response regression from a change on auth-service itself (F7, F8, F9, F10, F12). The two changes that bracket the onset are dep-5710 (jjwt 0.11 -> 0.12, TokenValidator rewritten, live 14:00Z) and cfg-2705 (scheduled signing-key rotation, k-2025-10 -> k-2026-04, 14:01Z), and no other service changed anything in the window (F1, F2, F14, F18). The reviewed ranking puts H1 (key rotation invalidating tokens before all validators loaded the new key) on top at only 0.42, with H2 (JWT library regression) at 0.32 and a combined-change H3 at 0.15 — confidences capped because the mechanism is unobservable. Because the top hypothesis is below the 0.50 support threshold and the discriminating evidence (auth-service logs, per-replica/per-key breakdown, JWKS publication) is unavailable, the outcome is inconclusive rather than a confident root cause.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Signing-key rotation (cfg-2705) invalidated tokens before all validators loaded the new key | config_change | auth-service | cfg-2705 | 0.42 (low) | active |
| 2 | H2: JWT library upgrade in dep-5710 introduced a token-validation regression | bad_deployment | auth-service | dep-5710 | 0.32 (low) | active |
| 3 | H3: Combined/double change: both the code upgrade and the key rotation are required to produce the 36% rate | bad_deployment | auth-service | dep-5710 | 0.15 (low) | active |
| 4 | H4: Reporter's counter-framing: fault originates at api-gateway (header stripping / own validation), not auth-service | unknown | api-gateway | null | 0.03 (low) | rejected |

### H1: Signing-key rotation (cfg-2705) invalidated tokens before all validators loaded the new key
security-automation's scheduled rotation of jwt.signing.key_id from 'k-2025-10' to 'k-2026-04' (cfg-2705, 14:01Z) caused auth-service to reject tokens signed under the previous key (or presented with an unknown/old kid) with 401 invalid_token. Until all validators/pods have loaded the new key material, a fraction of validations fails, producing a flat elevated 401 rate from the 14:02 step onward.

**Causal chain**
1. security-automation rotates jwt.signing.key_id to k-2026-04 at 14:01Z (cfg-2705) [F2, F14]
2. The new key id is published/expected cluster-wide but the key material or JWKS view reaching validators is not uniformly updated *(assumed)*
3. Tokens signed with the prior key (k-2025-10) or carrying the old kid are rejected as invalid_token *(assumed)*
4. auth-service error_rate steps to ~36% at 14:02Z and stays flat, consistent with a stable fraction of mismatched validations [F7, F8, F15]
5. Failures are fast 401s with flat latency/CPU/request rate, consistent with validation rejection not saturation [F9, F10, F12]
6. api-gateway 5xx rises (~21%) as propagation from auth-service's 401/5xx, with no gateway-side change [F11, F18]
7. The higher-matching precedent HIST-108 is exactly this mechanism (key rotation before pods loaded JWKS), match_score 7 [F6, F17]

**Supporting evidence**
  - `F2` ✔ Config change cfg-2705 on auth-service changed jwt.signing.key_id from 'k-2025-10' to 'k-2026-04' at 2026-04-15T14:01:00Z, authored by security-automation, described as 'Scheduled signing-key rotation'.
  - `F6` ✔ Historical precedent HIST-108 (2025-10-01, auth-service) also shows '401 invalid_token' / 'users logged out', root cause 'Key rotation published new key id before all pods loaded the JWKS', category config_change, resolution 'Re-published previous key; staged rotation'.
  - `F7` ✔ auth-service error_rate steps from a ~0.16-0.31% baseline to 36.08% in the 1-minute bucket at 14:02Z, with the last normal minute at 14:01Z (0.16%). Onset is a step change, not gradual.
  - `F8` ✔ The elevated 5xx plateau is sustained and flat: 36.5%, 36.7%, 36.08%, 35.82% ... through 15:55Z (avg 36.27%), with min 35.29% and max 40.33% over the post-onset window.
  - `F9` ✔ auth-service request_rate is unchanged across the onset: ~387-407 req/s at 12:00-13:55Z and ~385-414 req/s after 14:00Z (no traffic surge or collapse), so the 36% is a quality-of-response change, not a load change.
  - `F10` ✔ auth-service latency_p99_ms stays ~81-101 ms across the whole window including after onset (14:00Z avg 89.78, 15:55Z avg 88.47); the failures are fast 401s, not timeouts/saturation.
  - `F11` ✔ api-gateway error_rate steps in the same bucket from ~0.25% to 12.72% at 14:00Z (5-min bucket) and plateaus at ~20.5-21.7% through 15:55Z — roughly 58% of auth-service's 36%, so gateway already absorbs the auth failures.
  - `F12` ✔ auth-service cpu_pct is flat at ~37-39% before and after onset (13:48Z avg 37.84 -> 14:00Z avg 38.72 -> 15:48Z avg 38.14, max 42.31), confirming no resource-saturation signature accompanies the error step.
  - `F14` ✔ Change inventory for 2026-04-15T06:00-16:00Z contains exactly one deployment and one config change on auth-service, both within 2 minutes before the 14:02Z onset: dep-5710 (auth-service v7.3.0, deployed_at 2026-04-15T14:00:00Z, 'Upgrade JWT library to 4.x; refactor token validation', changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten', status 'live', rolled_back_at null) and cfg-2705 (auth-service, key 'jwt.signing.key_id', old_value 'k-2025-10' -> new_value 'k-2026-04', changed_at 2026-04-15T14:01:00Z, author security-automation, 'Scheduled signing-key rotation').
  - `F15` ✔ Ordering relative to onset: dep-5710 went live 2 minutes before the 14:02Z error step, cfg-2705 landed 1 minute before it (14:01Z), and the key rotation is the last change before onset. Both are still live/unrolled — dep-5710 status 'live' with rolled_back_at null — so no rollback has truncated the failure plateau (36.5%, 36.7% ... through 15:55Z).
  - `F17` ✔ Precedent ranking favors the config change: HIST-108 (2025-10-01, auth-service, '401 invalid_token' / 'users logged out', root_cause 'Key rotation published new key id before all pods loaded the JWKS', category config_change, resolution 'Re-published previous key; staged rotation') has match_score 7, while HIST-109 (2025-07-11, auth-service, root cause 'JWT library upgrade changed clock-skew default', category bad_deployment) scores 2. The higher-matching precedent (key rotation) maps to cfg-2705, the lower to dep-5710.
  - `F18` ✔ Contradiction check against the upstream/gateway theory holds: api-gateway shows zero deployments (count 0) and zero config changes (count 0) across 2026-04-15T06:00-16:00Z, so nothing changed at the gateway to originate the fault; its 5xx rise is propagation of auth-service failures, consistent with the team's F11. The only changes in the whole window are on auth-service.
**Contradicting evidence**
  - `F13` ✔ The failure is NOT bimodal at ~33% in the aggregate metric: the auth-service error_rate plateau sits consistently at ~35-39%, and the available metric set for auth-service (cpu_pct, error_rate, latency_p99_ms, memory_pct, rate_limited_per_min, request_rate, restarts) contains no per-replica/per-instance dimension, so per-pod clustering cannot be checked from these series.
  - `F16` ✔ Blast radius is neither replica- nor neighbour-scoped: the auth-service dependency map shows edges to user-db (db), redis-cache (cache, shared:true) and kyc-provider (external, has_telemetry:false), with its only dependent edge api-gateway -> auth-service (http). Neither cfg-2705 (a cluster-wide key_id value) nor dep-5710 (a whole-service version) is expressed as a per-replica or per-key shard in the change inventory, so the ~36% plateau cannot be attributed to exactly one of N replicas/keys from the change records — the 1-of-3 scoping is plausible for a signing-key rotation but unproven.
**Assumptions**
  - The new key id reached validators before the corresponding key material was uniformly available (no JWKS re-publication event is observable — F19)
  - A stable ~36% of traffic still presented old-key tokens; the 1-in-3 scoping is not proven (F13, F16)

### H2: JWT library upgrade in dep-5710 introduced a token-validation regression
auth-service v7.3.0 (dep-5710, live 14:00Z) upgraded jjwt 0.11 -> 0.12 with a rewritten TokenValidator, introducing a parsing/validation regression (e.g. changed default claim/clock-skew handling). All validations through the rewritten path fail or a class of tokens is now rejected, giving a flat ~36% 401 plateau.

**Causal chain**
1. dep-5710 deploys auth-service v7.3.0 with jjwt 0.11 -> 0.12 breaking parser API change and rewritten TokenValidator at 14:00Z [F1, F14]
2. The rewritten validator changes parsing/claim/skew semantics, rejecting previously valid tokens *(assumed)*
3. auth-service error_rate steps to ~36% at 14:02Z, a sustained flat plateau [F7, F8]
4. Fast 401s, unchanged latency/CPU/rate, consistent with a validation-code rejection [F9, F10, F12]
5. No rollback of dep-5710 (rolled_back_at null), so the plateau persists [F15]
6. Precedent HIST-109 (JWT library upgrade changed clock-skew default) is the same class of fault, match_score 2 [F5, F17]

**Supporting evidence**
  - `F1` ✔ Deployment dep-5710 on auth-service v7.3.0 went live at 2026-04-15T14:00:00Z (12 minutes before the incident was reported), described as 'Upgrade JWT library to 4.x; refactor token validation' with changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'. It is still status 'live' with rolled_back_at null.
  - `F5` ✔ Historical precedent HIST-109 (2025-07-11, auth-service) has the same symptoms '401 invalid_token' / 'token validation failures', root cause 'JWT library upgrade changed clock-skew default', category bad_deployment, resolution 'Rolled back; pinned skew config' (match_score 5).
  - `F7` ✔ auth-service error_rate steps from a ~0.16-0.31% baseline to 36.08% in the 1-minute bucket at 14:02Z, with the last normal minute at 14:01Z (0.16%). Onset is a step change, not gradual.
  - `F8` ✔ The elevated 5xx plateau is sustained and flat: 36.5%, 36.7%, 36.08%, 35.82% ... through 15:55Z (avg 36.27%), with min 35.29% and max 40.33% over the post-onset window.
  - `F9` ✔ auth-service request_rate is unchanged across the onset: ~387-407 req/s at 12:00-13:55Z and ~385-414 req/s after 14:00Z (no traffic surge or collapse), so the 36% is a quality-of-response change, not a load change.
  - `F10` ✔ auth-service latency_p99_ms stays ~81-101 ms across the whole window including after onset (14:00Z avg 89.78, 15:55Z avg 88.47); the failures are fast 401s, not timeouts/saturation.
  - `F12` ✔ auth-service cpu_pct is flat at ~37-39% before and after onset (13:48Z avg 37.84 -> 14:00Z avg 38.72 -> 15:48Z avg 38.14, max 42.31), confirming no resource-saturation signature accompanies the error step.
  - `F14` ✔ Change inventory for 2026-04-15T06:00-16:00Z contains exactly one deployment and one config change on auth-service, both within 2 minutes before the 14:02Z onset: dep-5710 (auth-service v7.3.0, deployed_at 2026-04-15T14:00:00Z, 'Upgrade JWT library to 4.x; refactor token validation', changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten', status 'live', rolled_back_at null) and cfg-2705 (auth-service, key 'jwt.signing.key_id', old_value 'k-2025-10' -> new_value 'k-2026-04', changed_at 2026-04-15T14:01:00Z, author security-automation, 'Scheduled signing-key rotation').
  - `F15` ✔ Ordering relative to onset: dep-5710 went live 2 minutes before the 14:02Z error step, cfg-2705 landed 1 minute before it (14:01Z), and the key rotation is the last change before onset. Both are still live/unrolled — dep-5710 status 'live' with rolled_back_at null — so no rollback has truncated the failure plateau (36.5%, 36.7% ... through 15:55Z).
**Contradicting evidence**
  - `F17` ✔ Precedent ranking favors the config change: HIST-108 (2025-10-01, auth-service, '401 invalid_token' / 'users logged out', root_cause 'Key rotation published new key id before all pods loaded the JWKS', category config_change, resolution 'Re-published previous key; staged rotation') has match_score 7, while HIST-109 (2025-07-11, auth-service, root cause 'JWT library upgrade changed clock-skew default', category bad_deployment) scores 2. The higher-matching precedent (key rotation) maps to cfg-2705, the lower to dep-5710.
**Assumptions**
  - The rewritten TokenValidator actually rejects ~1/3 of otherwise-valid tokens; the mechanism (which tokens, why 36%) is unverified and depends on unavailable logs
  - No per-replica clustering (F13)

### H3: Combined/double change: both the code upgrade and the key rotation are required to produce the 36% rate
Neither change alone yields a 36% failure; the dep-5710 rewrite changed how key ids are matched or how the JWKS is cached, and cfg-2705 then supplied a key id that the new code resolves incorrectly for a stable subset of tokens. The fault originates in the interaction on auth-service, driven by the code change exposing a latent key-handling path.

**Causal chain**
1. dep-5710 rewrites TokenValidator / parser at 14:00Z [F1, F14]
2. The rewritten validator changes key-resolution or JWKS reload behavior *(assumed)*
3. cfg-2705 publishes a new key id (k-2026-04) at 14:01Z that the new code mishandles for a fraction of tokens [F2]
4. The interaction produces the flat ~36% plateau beginning 14:02Z [F7, F8, F15]

**Supporting evidence**
  - `F1` ✔ Deployment dep-5710 on auth-service v7.3.0 went live at 2026-04-15T14:00:00Z (12 minutes before the incident was reported), described as 'Upgrade JWT library to 4.x; refactor token validation' with changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'. It is still status 'live' with rolled_back_at null.
  - `F2` ✔ Config change cfg-2705 on auth-service changed jwt.signing.key_id from 'k-2025-10' to 'k-2026-04' at 2026-04-15T14:01:00Z, authored by security-automation, described as 'Scheduled signing-key rotation'.
  - `F14` ✔ Change inventory for 2026-04-15T06:00-16:00Z contains exactly one deployment and one config change on auth-service, both within 2 minutes before the 14:02Z onset: dep-5710 (auth-service v7.3.0, deployed_at 2026-04-15T14:00:00Z, 'Upgrade JWT library to 4.x; refactor token validation', changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten', status 'live', rolled_back_at null) and cfg-2705 (auth-service, key 'jwt.signing.key_id', old_value 'k-2025-10' -> new_value 'k-2026-04', changed_at 2026-04-15T14:01:00Z, author security-automation, 'Scheduled signing-key rotation').
  - `F15` ✔ Ordering relative to onset: dep-5710 went live 2 minutes before the 14:02Z error step, cfg-2705 landed 1 minute before it (14:01Z), and the key rotation is the last change before onset. Both are still live/unrolled — dep-5710 status 'live' with rolled_back_at null — so no rollback has truncated the failure plateau (36.5%, 36.7% ... through 15:55Z).
**Contradicting evidence**
  - (none)
**Assumptions**
  - Both changes are in scope and neither alone reproduces the rate; unobserved from logs (F19)
  - The two changes are not independent — unproven

### H4: Reporter's counter-framing: fault originates at api-gateway (header stripping / own validation), not auth-service
The 401s originate at the edge/gateway tier, which strips or rewrites Authorization headers or enforces its own token validation, and the auth-service error metric reflects that rather than a genuine auth-service fault.

**Causal chain**
1. api-gateway mis-handles Authorization headers / validates tokens itself *(assumed)*
2. Downstream calls reach auth-service malformed and fail *(assumed)*
3. auth-service error_rate rises to ~36% [F7]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F11` ✔ api-gateway error_rate steps in the same bucket from ~0.25% to 12.72% at 14:00Z (5-min bucket) and plateaus at ~20.5-21.7% through 15:55Z — roughly 58% of auth-service's 36%, so gateway already absorbs the auth failures.
  - `F18` ✔ Contradiction check against the upstream/gateway theory holds: api-gateway shows zero deployments (count 0) and zero config changes (count 0) across 2026-04-15T06:00-16:00Z, so nothing changed at the gateway to originate the fault; its 5xx rise is propagation of auth-service failures, consistent with the team's F11. The only changes in the whole window are on auth-service.
**Assumptions**
  - An undocumented gateway behavior or change exists
**Rejected because:** F18 shows api-gateway had zero deployments and zero config changes across 06:00-16:00Z; its 5xx rise (F11, ~21% vs auth's 36%) is proportional propagation of auth-service failures. There is no gateway-side origin event.

## Facts (verified observations)
- auth-service error_rate steps from a ~0.16-0.31% baseline to 36.08% in the 1-minute bucket at 2026-04-15T14:02Z; the last normal minute is 14:01Z (0.16%). The onset is a step change, not gradual. [F7]
- The elevated error rate is a sustained, flat plateau: 36.5%, 36.7%, 36.08%, 35.82% ... through 15:55Z (avg 36.27%, min 35.29%, max 40.33%). [F8]
- auth-service request_rate is unchanged across onset (~387-407 req/s before, ~385-414 req/s after), so the 36% is a quality-of-response change, not a load change. [F9]
- auth-service latency_p99_ms stays ~81-101 ms across the whole window (14:00Z avg 89.78, 15:55Z avg 88.47); the failures are fast 401s, not timeouts or saturation. [F10]
- auth-service cpu_pct is flat at ~37-39% before and after onset (max 42.31), so no resource-saturation signature accompanies the error step. [F12]
- The change inventory for 2026-04-15T06:00-16:00Z contains exactly one deployment and one config change on auth-service, both within 2 minutes before the 14:02Z onset: dep-5710 (auth-service v7.3.0, deployed 14:00:00Z, 'Upgrade JWT library to 4.x; refactor token validation', jjwt 0.11 -> 0.12 breaking parser API, TokenValidator rewritten, status 'live', rolled_back_at null) and cfg-2705 (auth-service, jwt.signing.key_id old_value 'k-2025-10' -> new_value 'k-2026-04', changed_at 14:01:00Z, author security-automation, 'Scheduled signing-key rotation'). [F14, F1, F2]
- Ordering relative to onset: cfg-2705 landed at 14:01Z (the last change before the 14:02Z step) and dep-5710 went live at 14:00Z; neither has been rolled back (dep-5710 status 'live', rolled_back_at null), so no rollback has truncated the plateau. [F15]
- api-gateway error_rate steps in the same period from ~0.25% to 12.72% at 14:00Z (5-min bucket) and plateaus at ~20.5-21.7% through 15:55Z — roughly 58% of auth-service's 36% — indicating propagation rather than a gateway-side origin. [F11]
- api-gateway shows zero deployments and zero config changes across 2026-04-15T06:00-16:00Z; the only changes in the whole window are on auth-service, so nothing changed at the gateway to originate the fault. [F18]
- Historical precedent HIST-108 (2025-10-01, auth-service) has the same symptoms (401 invalid_token / users logged out) with root cause 'Key rotation published new key id before all pods loaded the JWKS', category config_change, resolution 'Re-published previous key; staged rotation', match_score 7; HIST-109 (2025-07-11, auth-service) matches the JWT-library-upgrade pattern ('JWT library upgrade changed clock-skew default', bad_deployment) with match_score 2 / 5 depending on ranking field. The higher-matching precedent maps to cfg-2705, the lower to dep-5710. [F17, F6, F5]
- auth-service's only dependent edge is api-gateway -> auth-service (http); its dependencies are user-db (postgres db), redis-cache (cache, shared:true) and kyc-provider (external, has_telemetry:false), and neither cfg-2705 nor dep-5710 is expressed as a per-replica or per-key shard in the change inventory. [F4]
- No secret/key-material rotation event, JWKS re-publication event, key-store change, or kill-switch/rollback flag change is visible in the tools; the only rotation is cfg-2705 operating on the jwt.signing.key_id config key. [F19]
- Across 2026-04-12 to 2026-04-15T14:12Z, only two deployments appear for all services: market-data-service v5.0.3 at 11:35Z and auth-service dep-5710 v7.3.0 at 14:00Z; no shared auth/token-library consumer service outside auth-service was deployed in the window. [F3]

## Assumptions (not verified)
- Assumption: the newly published key id (k-2026-04) reached validators before the corresponding key material was uniformly available across pods; no JWKS re-publication event is observable in the tools (F19), so this mechanism is inferred, not verified.
- Assumption: a stable ~36% of traffic still presented old-key tokens or a stale kid; the 1-in-3 scoping is not proven by any per-replica or per-key telemetry (F13, F16).
- Assumption: H2's mechanism — that the rewritten TokenValidator in jjwt 0.12 rejects ~1/3 of otherwise-valid tokens (e.g. via changed default claim or clock-skew handling) — is unverified and would require log access to confirm (F19).
- Assumption: dep-5710 and cfg-2705 are independent changes; the H3 claim that their interaction is required to produce 36% is unproven.
- Assumption: the api-gateway 5xx rise is proportional propagation of auth-service failures rather than an independent gateway problem, based on the absence of gateway changes (F18) and the ratio in F11.
- Assumption: the absence of any additional change row in the 06:00-16:00Z inventory (F14, F18) means no earlier same-day secret/key rotation or rollback on 2026-04-15 is a live candidate; a pre-06:00 rotation was not independently re-listed this round.
- Assumption: no unpublished consumer of a shared auth/token library outside auth-service is contributing; only auth-service's rollout/rollback record is published and secret-manager change events were not exposed.
- Assumption: redis-cache, user-db and kyc-provider are not themselves the trigger, since the error signature is fast, flat, latency-neutral 401s rather than latency/capacity degradation; none of these dependency-side series was queried within budget.
- (demoted from facts: no verified observation backs it) The auth-service metric set (cpu_pct, error_rate, latency_p99_ms, memory_pct, rate_limited_per_min, request_rate, restarts) contains no per-replica/per-instance dimension and no 401-vs-5xx breakdown, so per-pod clustering cannot be checked from these series; the plateau is consistently ~35-39%, not a clean 33%.

## Timeline
- `2025-07-11T00:00:00Z` Precedent HIST-109: auth-service 401 invalid_token / token validation failures caused by a JWT library upgrade changing the clock-skew default; resolved by rollback and pinned skew config (match_score 2/5). [F5]
- `2025-10-01T00:00:00Z` Precedent HIST-108: auth-service 401 invalid_token / users logged out caused by key rotation publishing a new key id before all pods loaded the JWKS; resolved by re-publishing the previous key and staging the rotation (match_score 7). [F6, F17]
- `2026-04-15T11:35:00Z` market-data-service v5.0.3 deployed ('Fix symbol mapping', ISIN map) — the only non-auth deployment in the window. [F3]
- `2026-04-15T14:00:00Z` dep-5710 goes live on auth-service v7.3.0: 'Upgrade JWT library to 4.x; refactor token validation' (jjwt 0.11 -> 0.12 breaking parser API, TokenValidator rewritten); still live, rolled_back_at null. [F1, F14, F15]
- `2026-04-15T14:00:00Z` api-gateway error_rate steps from ~0.25% to 12.72% in the 5-minute bucket and subsequently plateaus at ~20.5-21.7%. [F11]
- `2026-04-15T14:01:00Z` cfg-2705 lands: jwt.signing.key_id changed from 'k-2025-10' to 'k-2026-04' by security-automation ('Scheduled signing-key rotation') — the last change before onset; it is the only visible rotation event and no JWKS re-publication is observable. [F2, F15, F19]
- `2026-04-15T14:01:00Z` Last normal auth-service minute: error_rate 0.16%. [F7]
- `2026-04-15T14:02:00Z` auth-service error_rate steps to 36.08% (from a 0.16-0.31% baseline) with request_rate, latency_p99_ms and cpu_pct unchanged. [F7, F9, F10, F12]
- `2026-04-15T14:12:00Z` Incident reported: ~a third of token validations failing with 401 invalid_token; auth-service error rate 36%; identity-namespace log pipeline down since the morning so auth-service logs are not searchable. []
- `2026-04-15T15:55:00Z` Error plateau still flat: 36.5%, 36.7%, 36.08%, 35.82% ... (avg 36.27%) at auth-service; api-gateway still ~20.5-21.7%; no rollback of either change. [F8, F15, F11]

## Recommended actions
- **[immediate]** Restore visibility first: fix the identity-namespace log pipeline (separate open ticket) so auth-service 401 logs — specifically the 'invalid_token' variant and any 'unknown kid' vs signature/parse error text — become searchable; this is the single highest-value discriminator between H1 (cfg-2705) and H2 (dep-5710). -- Every remaining hypothesis separation currently rests only on timing and precedent match_score because logs are unsearchable (F19; data gaps). Without log text the two candidates cannot be distinguished. [F19]
- **[immediate]** Mitigate the key-rotation path (H1, top-ranked at 0.42): re-publish the previous key material for 'k-2025-10' alongside 'k-2026-04' so validators accept both, or stage the rotation (revert cfg-2705's jwt.signing.key_id to 'k-2025-10' and re-run the rotation gradually), which is exactly the HIST-108 resolution ('Re-published previous key; staged rotation'). -- cfg-2705 at 14:01Z is the last change before the 14:02Z step and the highest-matching precedent is a key-rotation-before-JWKS-load incident (F2, F7, F15, F17). This action is safe and reversible under either H1/H2/H3. [F2, F6, F7, F15, F17]
- **[immediate]** In parallel, roll back dep-5710 (auth-service v7.3.0) to the last known-good version; it is still live with rolled_back_at null, and HIST-109 shows a rollback-plus-config-pin is the proven resolution for this service's JWT-upgrade 401 pattern. -- dep-5710 is the only deployment in the window and went live 2 minutes before onset; rollback is the second safe mitigation and would directly test H2 if the key fix does not stop the plateau (F1, F5, F14, F15). [F1, F5, F14, F15]
- **[short_term]** Sequence the two mitigations (key re-publication/rotation staging first, then dep-5710 rollback) rather than doing both at once, and measure auth-service error_rate recovery after each step to attribute the fault. -- Applying both simultaneously would destroy the ability to discriminate H1, H2 and H3, and H3 explicitly claims neither change alone explains the 36% rate (F8, ongoing plateau gives a stable baseline to measure against). [F8, F15]
- **[short_term]** After mitigation, verify with per-replica/per-key telemetry: obtain a per-pod error-rate breakdown or a 401-vs-5xx and error-signature breakdown for auth-service, and confirm whether failures clustered on pods that had not loaded the new JWKS. -- The ~36% plateau is neither confirmed nor refuted as 1-of-N scoping because auth-service metrics have no per-replica dimension and no 401-vs-5xx breakdown (F13, F16); this evidence is what would confirm or overturn H1. [F13, F16]
- **[short_term]** Query the remaining callers of auth-service (order-service, portfolio-service, notification-service, exchange-gateway) for the same 14:02Z error step, to confirm blast radius is auth-service-internal rather than a shared-library consumer issue. -- Only api-gateway was sampled as a caller before budget expired; F3 shows no other service deployed, but unpublished consumers of a shared auth/token library were not ruled out (data gaps). [F3, F4, F11]
- **[long_term]** Pin and explicitly configure JWT parser semantics (clock-skew default, claim handling, unknown-kid behavior) in auth-service so a library upgrade cannot silently change validation semantics — the HIST-109 resolution pattern ('pinned skew config'). -- HIST-109 is the same service with the same symptom from a JWT library upgrade changing a default; the current dep-5710 rewrote TokenValidator, re-exposing that class of risk (F1, F5). [F1, F5]
- **[long_term]** Make signing-key rotations staged and dual-key by construction: publish the new JWKS/key material and keep the previous key accepted until every validator reports it loaded the new kid, and expose a JWKS-publication event that change tracking can see. -- HIST-108 (the highest-matching precedent, match_score 7) is exactly 'key rotation published new key id before all pods loaded the JWKS', and today no JWKS re-publication event is observable at all (F6, F17, F19). [F6, F17, F19]
- **[long_term]** Require change correlation for rotation automation: gate security-automation's scheduled key rotation so it cannot land within minutes of a simultaneous auth-service code deployment (dep-5710 at 14:00Z, cfg-2705 at 14:01Z), and fix the broken identity-namespace log pipeline as an SLO-tracked dependency so incidents in this namespace never start blind. -- Two overlapping changes 1 minute apart are why H1/H2/H3 cannot be separated (F1, F2, F15), and the log-pipeline outage removed the evidence that would have separated them immediately (F19). [F1, F2, F15, F19]

## Data gaps
- auth-service logs are not searchable (identity-namespace log pipeline down since the morning), so no log-level confirmation exists of which validation path, key id, or error signature produced the 401s; this is the primary reason the conclusion stays inconclusive.
- No per-replica/per-instance dimension and no 401-vs-5xx breakdown exist in the auth-service metric set, so the ~36% (≈1/3) failure ratio could not be tied to one of N replicas or one of {k-2025-10, k-2026-04}.
- No JWKS re-publication event, key-store change, or secret-manager event is exposed by the available tools; only the jwt.signing.key_id config key change (cfg-2705) is visible, so the distribution path of the new key id to validators — and why ~36% rather than 0% or 100% fail — is unverified.
- Dependency-side series (redis-cache, user-db) were never queried, and kyc-provider is external with has_telemetry:false, so those dependencies cannot be evaluated as contributing factors.
- Only api-gateway was sampled as a caller of auth-service; user-service and session-service lookups failed (tc-014, tc-015, no such services in the catalog) and order-service, portfolio-service, notification-service and exchange-gateway were not queried, so blast radius beyond the gateway is unverified.
- Only auth-service has a published rollout/rollback record, and the change inventory was scoped to 2026-04-15T06:00-16:00Z, so a pre-06:00Z same-day secret/key rotation or rollback on 2026-04-15 was not independently re-listed and cannot be fully excluded (though onset timing makes it unlikely).

## What would change this conclusion
- auth-service 401 logs (once the identity-namespace pipeline is restored) showing 'unknown kid' / signature-verification failures for tokens signed with k-2025-10 would confirm H1 (key rotation, cfg-2705) as root cause; logs showing parser/claim/clock-skew errors in the rewritten TokenValidator would instead support H2 (dep-5710).
- A per-replica or per-key error breakdown showing the ~36% clustered on the pods or keys that had not loaded the new key material would confirm H1; a uniform failure across all replicas would point to a cluster-wide bad build (H2) and would undermine the key-rotation framing.
- Rolling back cfg-2705 alone (restoring jwt.signing.key_id 'k-2025-10' / re-publishing the old key) and observing the error rate return to ~0.2% would confirm H1; if the 36% plateau persists after that revert, H1 is refuted and H2 (or H3) becomes the leading explanation.
- Rolling back dep-5710 alone and observing recovery would support H2 with HIST-109 as the matching mechanism, and would weaken the H1 precedence argument in F17.
- Neither single rollback restoring the service, while reverting both does, would confirm H3 (the combined-change interaction).
- Evidence of an unobserved pre-06:00Z or secret-store/key-store change on 2026-04-15 would add a candidate and would weaken the timing-based attribution of the 14:02Z step to cfg-2705 or dep-5710.
- Discovery of an unpublished consumer service that also validates tokens and failed at the same time could shift the fault from auth-service-internal to a shared-library/upstream cause, contradicting F3/F18 and the rejection of H4.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: The auth-service metric set (cpu_pct, error_rate, latency_p99_ms, memory_pct, ra

## Investigation stats
- latency 237.15s, LLM calls 12, tokens 75052 (in 57572 / out 17480)
- tool calls 24 (2 failed, 0 partial, 0 recovered by retry), findings 19/19 verified
