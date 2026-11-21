# Incident investigation report: INC-007
*System: multi_agent | outcome: **inconclusive** | ended: critic_disabled*

## Summary
A hard step change took auth-service from ~0.2% to a flat ~36% 401 invalid_token error rate with onset between 14:00Z and 14:02Z, and it has persisted through 15:54Z. The two 14:00–14:01Z changes are the only ones in scope and both plausibly target token validation: deploy dep-5710 (jjwt 0.11→0.12 breaking parser API + TokenValidator rewrite) at 14:00Z and signing-key rotation cfg-2705 (jwt.signing.key_id k-2025-10→k-2026-04) at 14:01Z. The ranking is genuinely close — H1 key rotation at 0.42 versus H2 deploy regression at 0.40 — and each maps to a near-identical real precedent (HIST-108 and HIST-109, respectively), so we cannot separate them with the evidence available. The outcome is therefore inconclusive: neither change was reverted, auth-service logs are unsearchable, and no per-replica or per-caller breakdown exists to prove the partial failure domain that the flat ~36% plateau implies. Safe mitigations plus a discriminating test are the right next step.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: JWT signing-key rotation (cfg-2705) published new key id before all auth-service pods loaded the new JWKS key | config_change | auth-service | cfg-2705 | 0.42 (low) | active |
| 2 | H2: auth-service deploy dep-5710 (jjwt 0.11->0.12 + TokenValidator rewrite) introduced a validation regression rejecting valid tokens | bad_deployment | auth-service | dep-5710 | 0.40 (low) | active |
| 3 | H3: Fault originates outside auth-service: api-gateway/caller or shared redis-cache path corrupts or drops tokens so auth-service correctly rejects them | dependency_failure | api-gateway | null | 0.12 (low) | active |
| 4 | H4: Identity log-pipeline outage and the 401s share a common credential/config/network change this morning | unknown | unknown | null | 0.05 (low) | active |
| 5 | H5: Clock skew / NTP drift or resource exhaustion on a subset of auth-service nodes | infrastructure_failure | auth-service fleet (node/host layer) | null | 0.05 (low) | active |

### H1: JWT signing-key rotation (cfg-2705) published new key id before all auth-service pods loaded the new JWKS key
The scheduled signing-key rotation cfg-2705 changed jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01Z, one minute after the JWT library deploy. Pods that had not yet refreshed the key/JWKS continued to validate against the old (or a mismatched) key material, so assertions signed with one key id were rejected by replicas holding the other — a partial failure domain producing a flat ~36% 401 invalid_token plateau with unchanged latency, CPU, and throttling. Origin: auth-service signing-key configuration change.

**Causal chain**
1. cfg-2705 rotates jwt.signing.key_id k-2025-10 -> k-2026-04 at 14:01Z [F2]
2. Not all auth-service pods load the new key/JWKS atomically, so a subset of replicas validate against stale or mismatched key material (same mechanism as HIST-108: key id published before all pods loaded JWKS) [F13]
3. Tokens signed/verified against one key id are rejected by replicas holding the other, yielding 401 invalid_token [F13]
4. Only the subset of replicas/keys out of sync fails, giving a flat ~36% (roughly one-third) error plateau rather than whole-fleet failure [F7, F8, F12]
5. No latency, CPU, or throttle change accompanies the error step, consistent with a key-mismatch rejection path rather than overload [F9, F10, F11]

**Supporting evidence**
  - `F2` ✔ A scheduled signing-key rotation config change cfg-2705 on auth-service changed key 'jwt.signing.key_id' from 'k-2025-10' to 'k-2026-04' at 2026-04-15T14:01:00Z by security-automation, one minute after the JWT library deploy and ~11 min before onset.
  - `F3` ✔ Two changes in the 14:00–14:01Z window both touch token validation / signing material (JWT parser rewrite and signing key_id rotation); these are the only deployments and only config change in scope (deployments count=2, config changes count=1) for 2026-04-14T00:00Z–2026-04-15T15:59Z across all services.
  - `F13` ✔ HIST-108 (2025-10-01) is the closest genuine precedent: '401s after JWT signing key rotation', service auth-service, symptoms '401 invalid_token','users logged out', root cause 'Key rotation published new key id before all pods loaded the JWKS', category config_change, resolution 'Re-published previous key; staged rotation'. Its symptoms and category match the current incident's dominant signals (401 invalid_token, logged-out users) and the cfg-2705 signing-key rotation.
  - `F7` ✔ auth-service error_rate is at baseline ~0.2% (avg 0.21, max 0.26) at 14:00:00Z and jumps to 37.21% avg (max 38.34) in the next 2-minute bucket at 14:02:00Z, then stays at ~35–37% for the rest of the window — a step change with onset between 14:00 and 14:02.
  - `F8` ✔ The 6-minute-bucket view places the onset in the 14:00:00Z bucket (avg 24.84%, max 38.34%) vs 13:54:00Z (avg 0.25%), which the 2-minute zoom resolves to 14:00→14:02; the plateau level is ~36% (mean ~36.2% per 6-min bucket) at least through 15:54.
  - `F12` ✔ A flat ~36% error plateau (roughly one third of ~400 req/s) with unchanged latency, CPU and throttling is consistent with a partial failure domain — e.g. one replica group / shard / key subset failing — rather than a whole-fleet or global overload fault.
  - `F9` ✔ auth-service request_rate is unchanged across the onset: ~386–412 req/s before (13:54 bucket avg 396.61) and ~392–412 req/s after (14:00 avg 399.02, 14:06 avg 402.8); the error jump is not caused by a traffic surge.
  - `F10` ✔ auth-service latency_p99_ms stays flat at ~88–92 ms across the onset (13:54 avg 90.64, 14:03 avg 90.4, 14:12 avg 88.74); no latency degradation accompanies the error step, arguing against resource/saturation-driven timeouts.
  - `F11` ✔ auth-service CPU and rate-limit counters are flat across the onset: cpu_pct ~37.4–38.6% in every 15-min bucket from 13:00 to 15:45, and rate_limited_per_min ~1.75–2.18/min unchanged — no CPU saturation or throttle signal at 14:00.
**Contradicting evidence**
  - `F4` ✔ No rollback occurred for either change: dep-5710 shows rolled_back_at null and status live; the signing-key config change has no rollback/revert entry. Neither change was reverted as of the latest data.
  - `F15` ✔ Both auth precedents were resolved by reverting the triggering change (HIST-108: 'Re-published previous key'; HIST-109: 'Rolled back'), whereas in the current incident neither candidate change was reverted (dep-5710 status live, rolled_back_at null; cfg-2705 no revert). HIST-101 (auth-service release v6.4.0, 'bcrypt cost factor raised 10->14') is a lower-relevance auth precedent resolved by 'Rolled back v6.4.0' and had different symptoms (latency/CPU/503, not 401s).
**Assumptions**
  - Pods do not atomically reload the rotated key; a stale-key window exists (not verifiable — auth-service logs unsearchable, no per-replica metric)
  - cfg-2705 actually took effect on validation paths at ~14:01Z
  - The ~36% plateau maps to a key/replica subset rather than a caller subset — per-replica and per-caller splits were never obtained

### H2: auth-service deploy dep-5710 (jjwt 0.11->0.12 + TokenValidator rewrite) introduced a validation regression rejecting valid tokens
dep-5710, deployed at 14:00Z with the breaking jjwt parser API change and a rewritten TokenValidator, changed the runtime semantics of token validation (e.g. clock-skew/leeway default, claim validation, audience/issuer check), so a fraction of legitimately valid tokens now fail verification with 401 invalid_token. Origin: auth-service code release. The fraction (~36%) would reflect the subset of tokens that rely on the changed semantics.

**Causal chain**
1. dep-5710 deploys jjwt 0.11->0.12 (breaking parser API) and a rewritten TokenValidator at 14:00Z [F1]
2. The parser rewrite changes validation semantics (per HIST-109, an auth library upgrade changed the clock-skew default and broke token validation) [F14]
3. A subset of tokens that previously passed now fail verification -> 401 invalid_token at ~36% [F14]
4. Error step appears in the 14:00->14:02 bucket, i.e. immediately after the 14:00Z deploy [F7, F8]
5. No latency/CPU/throttle change: the failures are semantic rejections, not resource stalls [F10, F11]

**Supporting evidence**
  - `F1` ✔ auth-service deployed v7.3.0 at 2026-04-15T14:00:00Z by d.verma, 'Upgrade JWT library to 4.x; refactor token validation', with changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live, rolled_back_at null. This is 12 minutes before the 14:12Z report.
  - `F3` ✔ Two changes in the 14:00–14:01Z window both touch token validation / signing material (JWT parser rewrite and signing key_id rotation); these are the only deployments and only config change in scope (deployments count=2, config changes count=1) for 2026-04-14T00:00Z–2026-04-15T15:59Z across all services.
  - `F14` ✔ HIST-109 (2025-07-11) is a second genuine precedent with the same symptom signature: 'Token validation regression after auth library upgrade', service auth-service, symptoms '401 invalid_token','token validation failures', root cause 'JWT library upgrade changed clock-skew default', category bad_deployment, resolution 'Rolled back; pinned skew config'. This parallels dep-5710's 'jjwt 0.11 -> 0.12 (breaking parser API)' + 'TokenValidator rewritten'.
  - `F7` ✔ auth-service error_rate is at baseline ~0.2% (avg 0.21, max 0.26) at 14:00:00Z and jumps to 37.21% avg (max 38.34) in the next 2-minute bucket at 14:02:00Z, then stays at ~35–37% for the rest of the window — a step change with onset between 14:00 and 14:02.
  - `F8` ✔ The 6-minute-bucket view places the onset in the 14:00:00Z bucket (avg 24.84%, max 38.34%) vs 13:54:00Z (avg 0.25%), which the 2-minute zoom resolves to 14:00→14:02; the plateau level is ~36% (mean ~36.2% per 6-min bucket) at least through 15:54.
  - `F9` ✔ auth-service request_rate is unchanged across the onset: ~386–412 req/s before (13:54 bucket avg 396.61) and ~392–412 req/s after (14:00 avg 399.02, 14:06 avg 402.8); the error jump is not caused by a traffic surge.
  - `F10` ✔ auth-service latency_p99_ms stays flat at ~88–92 ms across the onset (13:54 avg 90.64, 14:03 avg 90.4, 14:12 avg 88.74); no latency degradation accompanies the error step, arguing against resource/saturation-driven timeouts.
  - `F11` ✔ auth-service CPU and rate-limit counters are flat across the onset: cpu_pct ~37.4–38.6% in every 15-min bucket from 13:00 to 15:45, and rate_limited_per_min ~1.75–2.18/min unchanged — no CPU saturation or throttle signal at 14:00.
**Contradicting evidence**
  - `F4` ✔ No rollback occurred for either change: dep-5710 shows rolled_back_at null and status live; the signing-key config change has no rollback/revert entry. Neither change was reverted as of the latest data.
**Assumptions**
  - The rewritten validator actually rejects valid tokens at ~1/3 rate rather than 0% or 100%
  - Onset bucket granularity (2-min) does not hide a symptom start before 14:00Z
  - No client-side token-format change explains the fraction

### H3: Fault originates outside auth-service: api-gateway/caller or shared redis-cache path corrupts or drops tokens so auth-service correctly rejects them
auth-service is behaving correctly; an upstream caller (api-gateway) or the shared redis-cache path mangles, truncates, or drops tokens (or serves stale session state) for roughly a third of requests, so auth-service legitimately returns 401 invalid_token. Origin: a component upstream of or shared with auth-service. This contradicts the reporter's assumption that auth-service is broken.

**Causal chain**
1. api-gateway calls auth-service for validation (auth-service is 'called by api-gateway') [F5]
2. api-gateway routing/header handling (or a deploy there) alters or truncates Authorization material for a subset of callers *(assumed)*
3. auth-service receives malformed/invalid tokens and correctly rejects them with 401 invalid_token *(assumed)*
4. redis-cache is the only shared component in the graph and 0 config changes were seen for it, a possible alternate corruption/staleness origin but with no observed change [F5]

**Supporting evidence**
  - `F5` ✔ auth-service dependency graph: it calls user-db (postgres), redis-cache (cache), kyc-provider (external), and is called by api-gateway (http). redis-cache is the only component marked shared:true ('shared datastores are used by several services'); all other nodes are shared:false. redis-cache had 0 config changes in the window.
**Contradicting evidence**
  - `F3` ✔ Two changes in the 14:00–14:01Z window both touch token validation / signing material (JWT parser rewrite and signing key_id rotation); these are the only deployments and only config change in scope (deployments count=2, config changes count=1) for 2026-04-14T00:00Z–2026-04-15T15:59Z across all services.
  - `F7` ✔ auth-service error_rate is at baseline ~0.2% (avg 0.21, max 0.26) at 14:00:00Z and jumps to 37.21% avg (max 38.34) in the next 2-minute bucket at 14:02:00Z, then stays at ~35–37% for the rest of the window — a step change with onset between 14:00 and 14:02.
  - `F8` ✔ The 6-minute-bucket view places the onset in the 14:00:00Z bucket (avg 24.84%, max 38.34%) vs 13:54:00Z (avg 0.25%), which the 2-minute zoom resolves to 14:00→14:02; the plateau level is ~36% (mean ~36.2% per 6-min bucket) at least through 15:54.
**Assumptions**
  - An api-gateway deploy or routing change near 14:00Z exists but was never queried (budget exhausted) — no finding supports it
  - Caller-side token mutation at ~1/3 rate is possible without any recorded change
  - The error step at 14:00-14:02 may be coincidental to the step at 14:00Z rather than caused by it

### H4: Identity log-pipeline outage and the 401s share a common credential/config/network change this morning
The identity-namespace log pipeline has been down since the morning and the 401s began at 14:00Z; a single credential, secret, certificate or network/config change could have broken both log shipping and token signing/key distribution. Alternatively the 401s are a real but separate fault amplified in perception because missing logs prevent diagnosis. Origin: an unidentified shared control-plane/credential change, not the 14:00Z auth-service changes.

**Causal chain**
1. Identity log pipeline down since the morning, before the 14:00Z auth changes *(assumed)*
2. A shared credential/secret/config change breaks both log shipping and token/key operations *(assumed)*
3. Token validation fails for a subset of requests, producing 401 invalid_token *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F16` ✔ No historical precedent was found coupling a logging/observability pipeline outage to auth or token-validation failures. Searches for log-pipeline/identity-namespace outages returned no incident about logs shipping going down; HIST-107 ('Cold cache storm after redis failover', redis-cache) is the only shared-component infra precedent and concerns cache, not logging. The searched corpus contains no incident whose title/root cause links a log pipeline to 401s.
  - `F17` ✔ The auth-service dependency graph contains no logging/observability node: nodes are api-gateway (edge, caller), user-db (postgres), redis-cache (redis, shared:true), kyc-provider (external, no telemetry). redis-cache is the only shared component; there is no telemetry-carrying logging component in the graph to correlate with the identity log-pipeline outage. The known-services list also contains no logging/observability service (list spans api-gateway, auth-service, order/portfolio/pricing/market-data/payment/settlement/notification/exchange-gateway, ledger-db, user-db, redis-cache, kafka, exchange-api, bank-partner-api, kyc-provider, email-provider), so the log pipeline is not queryable as a service.
  - `F18` ✔ Investigation of logging-touching changes was blocked: get_config_changes(service='logging') failed with not_found ('unknown service logging'); the enumerable known_services list contains no logging/observability component, and a full-scope deployment query for 2026-04-15 returned only dep-5699 (market-data-service, 11:35Z) and dep-5710 (auth-service, 14:00Z) — no logging component appears. So the specific hypothesis that one change broke both log shipping and token signing could not be tested from config/deploy data.
  - `F1` ✔ auth-service deployed v7.3.0 at 2026-04-15T14:00:00Z by d.verma, 'Upgrade JWT library to 4.x; refactor token validation', with changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live, rolled_back_at null. This is 12 minutes before the 14:12Z report.
  - `F2` ✔ A scheduled signing-key rotation config change cfg-2705 on auth-service changed key 'jwt.signing.key_id' from 'k-2025-10' to 'k-2026-04' at 2026-04-15T14:01:00Z by security-automation, one minute after the JWT library deploy and ~11 min before onset.
  - `F7` ✔ auth-service error_rate is at baseline ~0.2% (avg 0.21, max 0.26) at 14:00:00Z and jumps to 37.21% avg (max 38.34) in the next 2-minute bucket at 14:02:00Z, then stays at ~35–37% for the rest of the window — a step change with onset between 14:00 and 14:02.
  - `F8` ✔ The 6-minute-bucket view places the onset in the 14:00:00Z bucket (avg 24.84%, max 38.34%) vs 13:54:00Z (avg 0.25%), which the 2-minute zoom resolves to 14:00→14:02; the plateau level is ~36% (mean ~36.2% per 6-min bucket) at least through 15:54.
**Assumptions**
  - A shared change exists — untested: 'logging' is not a known service, get_config_changes(logging) returned not_found, and no logging node exists in the auth-service graph
  - Log-pipeline outage precedes onset by hours, so the two would have to be linked by a delayed mechanism, not a single 14:00Z event

### H5: Clock skew / NTP drift or resource exhaustion on a subset of auth-service nodes
A subset of auth-service instances suffer NTP/clock drift (failing JWT nbf/exp checks) or resource exhaustion (OOM/throttle) so a fraction of requests fail with 401. Origin: infrastructure/time-sync rather than a code or config change.

**Causal chain**
1. A subset of nodes drifts in time or runs out of memory/CPU *(assumed)*
2. Drifted replicas fail nbf/exp validation (or exhausted replicas error/restart), returning 401 for ~1/3 of traffic *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ auth-service latency_p99_ms stays flat at ~88–92 ms across the onset (13:54 avg 90.64, 14:03 avg 90.4, 14:12 avg 88.74); no latency degradation accompanies the error step, arguing against resource/saturation-driven timeouts.
  - `F11` ✔ auth-service CPU and rate-limit counters are flat across the onset: cpu_pct ~37.4–38.6% in every 15-min bucket from 13:00 to 15:45, and rate_limited_per_min ~1.75–2.18/min unchanged — no CPU saturation or throttle signal at 14:00.
  - `F7` ✔ auth-service error_rate is at baseline ~0.2% (avg 0.21, max 0.26) at 14:00:00Z and jumps to 37.21% avg (max 38.34) in the next 2-minute bucket at 14:02:00Z, then stays at ~35–37% for the rest of the window — a step change with onset between 14:00 and 14:02.
**Assumptions**
  - Clock skew could exist on a subset of nodes but no NTP or per-replica metric exists to show it
  - Exhaustion would normally raise latency/CPU — flat p99 and flat cpu_pct argue against it

## Facts (verified observations)
- auth-service deploy dep-5710 (v7.3.0) went live at 2026-04-15T14:00:00Z, carrying a breaking JWT parser API change (jjwt 0.11 -> 0.12) and a rewritten TokenValidator. [F1]
- Scheduled signing-key rotation cfg-2705 changed auth-service config jwt.signing.key_id from k-2025-10 to k-2026-04 at 2026-04-15T14:01:00Z, one minute after the deploy. [F2]
- Both changes in the 14:00–14:01Z window touch token validation/signing material, and they are the only deployments and only config change in scope for the whole 2026-04-14T00:00Z–2026-04-15T15:59Z window across all services (deployments count=2, config changes count=1). [F3]
- The auth-service error rate sits at baseline ~0.2% at 14:00:00Z and jumps to 37.21% avg (max 38.34) in the next 2-minute bucket at 14:02:00Z, then holds ~35–37% for the rest of the window — a step change with onset between 14:00 and 14:02. [F7]
- The 6-minute-bucket view localizes onset to the 14:00:00Z bucket (avg 24.84%, max 38.34%) versus 13:54:00Z (avg 0.25%), and the plateau is ~36% (mean ~36.2% per 6-min bucket) at least through 15:54. [F8]
- Request rate is unchanged across onset (~386–412 req/s before and after), so the error jump is not traffic-driven. [F9]
- Latency p99 stays flat at ~88–92 ms across onset, arguing against resource/saturation-driven timeouts. [F10]
- CPU (~37.4–38.6% in every 15-min bucket 13:00–15:45) and rate-limit counters (~1.75–2.18/min) are flat across onset — no saturation or throttle signal at 14:00. [F11]
- Neither candidate change was reverted: dep-5710 shows status live with rolled_back_at null, and cfg-2705 has no rollback/revert entry, as of the latest data. [F4, F15]
- Historical precedent HIST-108 (2025-10-01) matches the exact symptom set and cfg-2705's mechanism: '401s after JWT signing key rotation', root cause 'Key rotation published new key id before all pods loaded the JWKS', resolved by re-publishing the previous key and staging the rotation. [F6, F13]
- Historical precedent HIST-109 (2025-07-11) parallels dep-5710: 'Token validation regression after auth library upgrade', root cause 'JWT library upgrade changed clock-skew default', resolved by rollback and pinning the skew config. [F6, F14]
- Both matching auth precedents were resolved by reverting the triggering change; in this incident neither candidate was reverted. [F15]
- auth-service's dependency graph calls user-db, redis-cache (the only shared:true component, with 0 config changes in the window), kyc-provider, and is called by api-gateway; no per-replica metric exists in available_metrics (cpu_pct, error_rate, latency_p99_ms, memory_pct, rate_limited_per_min, request_rate, restarts). [F5]
- No historical precedent links a logging/observability pipeline outage to auth or token-validation failures, and no logging/observability node exists in the auth-service dependency graph or in the known-services list. [F16, F17]

## Assumptions (not verified)
- Assumption: auth-service pods do not atomically reload the rotated key, so a stale-key window existed after cfg-2705 — not verifiable because auth-service logs are unsearchable and no per-replica metric exists.
- Assumption: cfg-2705 actually took effect on live validation paths at ~14:01Z.
- Assumption: the ~36% plateau maps to a key/replica subset rather than a caller subset — per-replica and per-caller splits were never obtained.
- Assumption: the rewritten TokenValidator from dep-5710 rejects valid tokens at roughly a one-third rate rather than 0% or 100%; this rate is not directly observed, only inferred from the aggregate error rate.
- Assumption: the 2-minute bucket granularity does not hide a symptom onset before 14:00Z.
- Assumption: no client-side token-format or caller-side change explains the failing fraction.
- Assumption: an api-gateway routing deploy near 14:00Z does not exist — no deployment query for api-gateway was completed, so this is an assumption, not a verified fact.
- Assumption: the identity log-pipeline outage is a separate fault, not a shared cause of the 401s; the shared-cause hypothesis remains untested rather than disproven.
- (demoted from facts: no verified observation backs it) A flat ~36% plateau (about one third of ~400 req/s) with unchanged latency, CPU and throttling is consistent with a partial failure domain (e.g. one replica group/shard/key subset) rather than global overload.
- (demoted from facts: no verified observation backs it) The specific hypothesis that one change broke both log shipping and token signing could not be tested: get_config_changes(service='logging') returned not_found, and a full-scope deployment query for 2026-04-15 returned only dep-5699 (market-data-service, 11:35Z) and dep-5710.

## Timeline
- `2026-04-15T14:00:00Z` auth-service deploy dep-5710 (v7.3.0) goes live by d.verma: jjwt 0.11 -> 0.12 (breaking parser API) plus TokenValidator rewrite. [F1]
- `2026-04-15T14:00:00Z` auth-service error_rate still at baseline ~0.21% (max 0.26) in the 14:00:00Z reading; request rate ~399 req/s and latency p99 ~90 ms stay flat. [F7, F9, F10]
- `2026-04-15T14:01:00Z` Scheduled signing-key rotation cfg-2705 changes jwt.signing.key_id from k-2025-10 to k-2026-04, applied by security-automation — one minute after the deploy. [F2, F3]
- `2026-04-15T14:02:00Z` 2-minute bucket shows error_rate jumped to 37.21% avg (max 38.34) — the onset falls between 14:00 and 14:02Z. [F7, F8]
- `2026-04-15T14:03:00Z` Latency p99 remains flat at ~90.4 ms and CPU/throttling remain flat, confirming the fault is not saturation-driven. [F10, F11]
- `2026-04-15T14:12:00Z` Incident INC-007 (SEV-1) reported: ~one third of token validations fail with 401 invalid_token; auth-service error rate 36%; identity-namespace log pipeline already down since morning so auth-service logs are unsearchable. [F7]
- `2026-04-15T14:12:00Z–2026-04-15T15:54:00Z` Error rate holds a flat ~36% plateau (mean ~36.2% per 6-min bucket) with unchanged request rate, latency, CPU and throttling — partial failure domain signature. [F8, F12]
- `2026-04-15T15:59:00Z` Latest data: neither dep-5710 nor cfg-2705 has been rolled back or reverted; auth-service still calls user-db, redis-cache (only shared component, 0 config changes) and kyc-provider, and is called by api-gateway. [F4, F5, F15]

## Recommended actions
- **[immediate]** Stop the scheduled rotation from advancing and re-publish/restore the previous signing key material (revert config jwt.signing.key_id from k-2026-04 back to k-2025-10, i.e. roll back cfg-2705) on auth-service, then force a key/JWKS reload across all replicas. -- cfg-2705 (jwt.signing.key_id k-2025-10 -> k-2026-04 at 14:01Z) is the top-ranked hypothesis and matches HIST-108 exactly, whose documented resolution was 'Re-published previous key; staged rotation'. A key-id flip is cheap, reversible and does not require a code change. [F2, F6, F13, F15]
- **[immediate]** In parallel, if the error rate does not drop within one observation bucket after the key revert, roll back dep-5710 (auth-service v7.3.0) to the previous release to restore the pre-rewrite TokenValidator and jjwt version. -- H2 (confidence 0.40) is statistically indistinguishable from H1 and matches HIST-109, whose resolution was a rollback. Because neither change was ever reverted (F4/F15), neither mechanism has been excluded; reverting the second candidate is the fastest discrimination path. Order the two reverts so the cheaper config change is tested first. [F1, F4, F6, F14, F15]
- **[immediate]** Before/while reverting, capture the discriminating evidence with the remaining budget: query per-instance/replica error_rate for auth-service (or add a temporary per-replica error breakdown) and a per-caller breakdown of the failing requests at api-gateway. -- The flat ~36% plateau implies a partial failure domain (a key/replica subset or a caller subset) but this was never proven; splitting the failure by replica vs caller is exactly what separates H1 (key/replica subset) from H3 (upstream caller subset). [F5, F12]
- **[short_term]** Restore log searchability for the identity namespace (fix or bypass the log pipeline) and re-query auth-service logs for JWKS/key-loading errors vs token-parse/claim-validation errors in the 14:00–14:15Z window. -- The single mechanism-level discriminator between H1 and H2 is log-level and is currently unavailable; every hypothesis is capped at low confidence precisely because auth-service logs cannot be searched. [F7, F18]
- **[short_term]** After the error rate returns to baseline, re-run the key rotation as a staged rollout: publish the new key id, verify all pods have loaded the new JWKS, then cut signing over — and verify the jjwt 0.12 validator against a replay corpus of real tokens (clock-skew/leeway, claim, audience/issuer checks) before re-deploying v7.3.0. -- Both HIST-108 ('staged rotation') and HIST-109 ('pinned skew config') were fixed by making the change safe rather than merely reverting it; either candidate change remains the likely driver until disproven. [F6, F13, F14, F15]
- **[long_term]** Add per-replica and per-key-id validation metrics (JWKS load state, key id in use per pod, 401 reason-code breakdown) and alerting on auth-service 401 invalid_token rate. -- The absence of any per-replica metric in available_metrics (only cpu_pct, error_rate, latency_p99_ms, memory_pct, rate_limited_per_min, request_rate, restarts) is why the partial-failure-domain reading of the ~36% plateau could not be confirmed. Response was also delayed ~11–12 minutes from onset to report. [F8, F12]
- **[long_term]** Decouple auth-service observability from the identity-namespace log pipeline (or add an independent log/ship path and a canary on log delivery) so that a logging outage cannot blind auth investigations. -- The logging outage demonstrably blocked log-based triage in this incident; no logging component is even present in the auth-service dependency graph or known-services list, so this blind spot is structural. [F16, F17, F18]

## Data gaps
- auth-service logs are unsearchable (identity-namespace log pipeline down since the morning), so no log-level signature of the 401s exists — the JWKS stale-key mechanism (H1) and the parser-regression mechanism (H2) cannot be distinguished from logs.
- auth-service exposes no per-replica/instance metric, so the ~36% error rate cannot be split by replica and the partial-fleet hypothesis is unproven.
- No per-caller breakdown of the failing traffic was obtained, so it is unknown whether failures are uniform across callers or concentrated in a subset (H3 remains untested).
- get_deployments for api-gateway and get_config_changes for auth-service were never executed (tool-call budget exhausted), so an api-gateway routing deploy or additional auth-service config flips near 14:00–14:02Z cannot be ruled out.
- No precise time-alignment of the 401 onset against the 14:00Z deploy and 14:01Z key rotation was measured; only the ~14:12Z report time and the bucketed error series are available.
- Neighbor services could not be queried ('gateway' and 'user-service' returned not_found), so correlated latency/saturation in api-gateway, redis-cache, user-db or ledger-db in the 14:00–14:02Z window is unknown.
- Config/telemetry for the identity log pipeline is unreachable: get_config_changes(service='logging') returned not_found and 'logging' is absent from known_services, so the shared-cause hypothesis (H4) is untested, not disproven.
- Historical search is keyword-scored (match_score 1–6) with no free-text/exact-phrase control and a small corpus, so closer precedents (e.g. certificate expiry, partial-fleet auth failures) may exist but were not surfaced; the precedent set is a lower bound.

## What would change this conclusion
- Per-replica error-rate data showing failures confined to a replica subset holding one key id would strongly confirm H1; a uniform failure rate across all replicas with the same key id would argue against H1 and toward H2.
- auth-service logs (once the pipeline is restored) showing JWKS/key-load or key-not-found errors for k-2026-04 would confirm H1; showing claim/clock-skew/parse rejections on well-formed tokens would confirm H2.
- Reverting only cfg-2705 and seeing the error rate return to baseline within one bucket would confirm H1; no change after that revert but resolution after rolling back dep-5710 would confirm H2.
- A per-caller breakdown showing the ~36% failures concentrated in one caller, or a matching api-gateway routing deploy at ~14:00Z, would promote H3 and change the owner of the fault.
- Evidence of correlated failures/latency on redis-cache or user-db at 14:00–14:02Z, or a client-side token-format change, would shift support toward H3.
- Discovery of a shared credential/secret/network change affecting both log shipping and key distribution (or a direct log-pipeline fault coincident with 14:00Z) would revive H4.
- NTP/clock-drift evidence on a subset of auth-service nodes, or a rise in latency/CPU/restarts that the current flat series does not show, would revive H5 and overturn the 'no resource fault' reading.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: A flat ~36% plateau (about one third of ~400 req/s) with unchanged latency, CPU 
- fact demoted to assumption: The specific hypothesis that one change broke both log shipping and token signin

## Investigation stats
- latency 111.99s, LLM calls 14, tokens 83448 (in 66098 / out 17350)
- tool calls 24 (3 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
