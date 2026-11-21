# Incident investigation report: INC-007
*System: multi_agent | outcome: **inconclusive** | ended: critic_inconclusive*

## Summary
INC-007 is a real, sharp, stable step in auth-service error rate from ~0.2% to a ~36% plateau at 14:01-14:02Z (F8/F9/F14/F23), with full and undegraded request volume, normal latency/CPU/memory, no throttling, no downstream datastore degradation, and no telemetry artifact — but the mechanism remains undetermined. Exactly two changes sit inside the same unresolvable 60-second onset bucket: the auth-service v7.3.0 deploy with a breaking JWT-parser rewrite at 14:00:00Z (F4/F31) and the signing-key rotation k-2025-10 -> k-2026-04 at 14:01:00Z (F5/F32). The top hypothesis H1 (parser regression rejecting a token subset) is only marginally ahead of H2 (key-rotation/JWKS lag) and H3 (one-of-three replica/AZ failing) at 0.40/0.40/0.30, and every decisive discriminator is absent from this environment: no per-instance/AZ/kid/issuer/tenant metric dimension (F20/F24), no jwks_key_resolution_miss (F20), no error-signature/status-class split (F29), no pod-lifecycle or apiserver source (F40/F41), and auth-service logs are hard 503 (F13/F37). Because no single hypothesis can be raised above a coin flip, the correct outcome is inconclusive rather than a certified root cause. Mitigation should proceed on the safest change-reversal path while discriminators are obtained.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: auth-service v7.3.0 JWT-4.x parser rewrite rejects a fixed subset of tokens | bad_deployment | auth-service | dep-5710 (auth-service v7.3.0, 2026-04-15T14:00:00Z) | 0.40 (low) | active |
| 2 | H2: 14:01Z signing-key rotation (k-2025-10 -> k-2026-04) broke key resolution for a subset of validations | config_change | auth-service | cfg-2705 (jwt.signing.key_id k-2025-10 -> k-2026-04, 2026-04-15T14:01:00Z) | 0.40 (low) | active |
| 3 | H3: Partial failure: one of three auth-service replicas/AZ rejects all auth validations | infrastructure_failure | auth-service (one of three replicas/AZs of the auth validation path) | dep-5710 (auth-service v7.3.0, 2026-04-15T14:00:00Z) | 0.30 (low) | active |
| 4 | H6: Incomplete 14:00Z rollout: intra-fleet version skew between replicas serving old and new auth code | bad_deployment | auth-service (intra-fleet skew created by the v7.3.0 rollout) | dep-5710 (auth-service v7.3.0, 2026-04-15T14:00:00Z) | 0.18 (low) | active |
| 5 | H4: Shared redis-cache degradation indirectly breaking auth validation for a subset of paths | dependency_failure | redis-cache (shared cache consumed by auth-service and 3 others) | - | 0.05 (low) | rejected |
| 6 | H5: Telemetry/log-pipeline artifact inflating the apparent 36% 401 rate | unknown | observability pipeline (identity namespace) | - | 0.03 (low) | rejected |

### H1: auth-service v7.3.0 JWT-4.x parser rewrite rejects a fixed subset of tokens
The 14:00Z deploy of auth-service v7.3.0 (jjwt 0.11->0.12 breaking parser API, TokenValidator rewritten) introduced a validation regression so a deterministic subset of otherwise-valid tokens is rejected as invalid_token, producing a stable ~36% 401 step on the auth validation path with no resource/latency/dependency abnormality.

**Causal chain**
1. Deploy dep-5710 (auth-service v7.3.0, breaking JWT parser-API change, TokenValidator rewritten) lands at 14:00:00Z, status=live, not rolled back. [F4, F31, F33]
2. The rewritten TokenValidator/parser changes which token shapes/claims are accepted, so a fixed subset of previously-valid tokens now fails validation. *(assumed)*
3. auth-service error_rate steps from ~0.2% to ~36% between 14:01Z and 14:02Z and holds ~36-37% for two hours. [F8, F14, F23, F9]
4. Fault is confined to the auth validation path: request_rate flat, latency/CPU/memory unchanged, shared dependencies flat, throttling flat, no restarts. [F10, F11, F15, F18, F22, F21]
5. Neighbouring consumers of the shared redis-cache (portfolio-service) stay at baseline, so the fault is auth's own validation code, not a shared dependency. [F17, F28]
6. api-gateway's errors step at the same instant and its 21% is arithmetically consistent with auth's rejected validations weighted by a larger traffic mix. [F16, F25, F26, F30]

**Supporting evidence**
  - `F4` ✔ Across all services, exactly 2 deployments exist in 2026-04-13T00:00Z..2026-04-15T16:00Z: market-data-service v5.0.3 (11:35:00Z, ci-bot, "Fix symbol mapping") and auth-service v7.3.0 (14:00:00Z, d.verma, "Upgrade JWT library to 4.x; refactor token validation", changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'). Both have status=live and rolled_back_at=null.
  - `F31` ✔ Caller-scoped change enumeration in the incident window (2026-04-15T13:50Z..14:10Z) returns exactly ONE deployment across all services: dep-5710, auth-service v7.3.0, deployed_at 2026-04-15T14:00:00Z by d.verma, "Upgrade JWT library to 4.x; refactor token validation"; scope='all services', partial=false, count=1. No second/rolling/partial-rollout event and no replica-set or pod-eviction record appears in the deployments source.
  - `F8` ✔ auth-service error_rate left baseline abruptly between 14:00:00Z (avg 0.21%) and 14:02:00Z (avg 37.21%) — a sharp step, not a ramp, so true onset is ~14:01-14:02Z, about 10 minutes before the 14:12 report time.
  - `F14` ✔ auth-service error_rate is a genuine step change measured on its own metric backend: 0.26% at 14:00Z, 0.16% at 14:01Z, then 36.08% at 14:02Z, 38.34% at 14:03Z, holding 36.22-38.53% through 14:06Z at 60-second resolution. The onset is between 14:01:00Z and 14:02:00Z.
  - `F23` ✔ Onset confirmed at 60s resolution: error_rate goes 14:00Z=0.26, 14:01Z=0.16, then 14:02Z=36.08, 14:03Z=38.34 (points continue 36.22, 37.97, 38.53 ... 34.40, 34.92 through 14:10Z), so the step is between 14:01:00Z and 14:02:00Z — mid-minute, immediately after the 14:00:00Z v7.3.0 deployment (F4) and the 14:01:00Z key rotation (F5), which this metric cannot separate.
  - `F9` ✔ The elevated error rate is a stable step of ~36-37%, not transient or growing: it holds ~35-37% continuously from 14:02Z through 15:56Z (last point avg 36.53).
  - `F10` ✔ The error step is NOT correlated with traffic: auth-service request_rate is flat at ~400 req/s both before and after onset (13:00-13:56 avg ~400, 14:02-15:56 avg ~400), with no traffic spike at 14:00-14:02.
  - `F11` ✔ auth-service looks normal-but-rejecting: latency_p99_ms stays ~81-101ms (mean 90.01) with no change across the 14:02 onset, CPU stays ~37-42% (mean 38.02) and memory ~47-58% (mean 51.89) both flat — no latency/CPU/memory abnormality accompanies the 401 step.
  - `F17` ✔ Failure does NOT spread to every consumer of the shared cache: portfolio-service (per F2 a redis-cache consumer) stays at baseline error_rate ~0.17-0.25% throughout 13:00-15:51Z, unaffected by the 14:02 step, so the failure is confined to the auth validation path rather than any service touching redis-cache.
  - `F18` ✔ The auth path's shared dependencies show no degradation across the onset: redis-cache hit_ratio_pct is flat at 95.51-96.46% (mean 95.97) for 13:00-15:48Z, and user-db slow_queries_per_min is flat at 0.26-0.34 (mean 0.3) in the same window, so cache-miss or slow-store explanations are not supported by these two metrics.
  - `F25` ✔ Inside 14:02-15:00Z auth-service runs at ~399.8 req/s (mean) with error_rate mean 36.17%, while api-gateway runs at ~1274.07 req/s with error_rate mean 21.08% — so the two error percentages are computed over very different denominator sizes (gateway handles ~3.2x auth-service's traffic), which is the arithmetic shape of a denominator effect rather than two distinct failure modes.
  - `F26` ✔ Converting to absolute counts, the two services are NOT inconsistent: auth-service ~399.8 req/s x 36.17% = ~144.6 failed validations/s; api-gateway ~1274.07 req/s x 21.08% = ~268.6 failed req/s. The gateway's absolute error volume is ~1.86x auth-service's, and auth-service's errors account for ~53.8% (144.6/268.6) of the gateway's errors — so the same underlying validation failure, weighted by the gateway's larger traffic mix, explains both percentages (the gateway also serves non-auth routes, hence >50%).
**Contradicting evidence**
  - `F5` ✔ Exactly one config change exists in the whole window: auth-service jwt.signing.key_id rotated from k-2025-10 to k-2026-04 at 2026-04-15T14:01:00Z by security-automation, described as "Scheduled signing-key rotation".
  - `F16` ✔ The failure is uniform-by-caller, not a subset of clients: api-gateway (the caller of auth-service in the dependency graph) steps at the same time and its error_rate rises from a ~0.2% baseline to ~21% (13:56Z avg 5.48/max 21.73 partial bucket, then 21.15, 21.01, 21.2... 20.96 to 15:56Z), i.e. a large but different fraction than auth-service's ~36%.
  - `F30` ✔ Consistent with the denominator reading, auth-service's flat ~400 req/s against the gateway's ~1274 req/s means the gateway routes ~68% of its traffic away from auth-service; if 36% of the ~400 auth validations/s are rejected, that alone can only account for ~11% of the gateway's 1274 req/s — so an additional ~10 points of the gateway's 21% must come from other gateway paths feeding off the same rejected tokens or additional failed calls, not from auth-service alone.
**Assumptions**
  - The rewritten parser actually rejects that subset of tokens — no 401 error signature was obtainable (F13 logs 503; F29 no status-class metric).
  - The ~36% is a property of token/claim mix, not a replica split — per-replica dimension never retrieved (F20/F24).
  - The 14:00Z deploy is causally prior to the onset; F23 shows deploy and rotation fall in the same 60s bucket, so ordering is not evidenced.

### H2: 14:01Z signing-key rotation (k-2025-10 -> k-2026-04) broke key resolution for a subset of validations
The scheduled signing-key rotation at 14:01:00Z published key id k-2026-04 without every token-validating component loading the matching JWKS entry, so validations requiring the rotated kid fail as invalid_token; the stable fraction reflects the share of tokens/paths hitting the rotated key, echoing HIST-108.

**Causal chain**
1. security-automation rotates jwt.signing.key_id from k-2025-10 to k-2026-04 at 14:01:00Z; the only config change in the window. [F5, F32, F33]
2. The rotation publishes a new key id before all validators have loaded its JWKS material, so tokens requiring the rotated kid cannot be verified. *(assumed)*
3. auth-service error_rate steps from ~0.2% to ~36% inside the same minute as the rotation (onset 14:01-14:02Z). [F14, F23, F8, F5]
4. The failure is stable, not transient, for two hours — consistent with a persistent key-id mismatch rather than a short propagation window. [F9]
5. No resource or dependency degradation accompanies the step, matching a pure key-resolution/mismatch failure. [F11, F18, F17, F28]

**Supporting evidence**
  - `F5` ✔ Exactly one config change exists in the whole window: auth-service jwt.signing.key_id rotated from k-2025-10 to k-2026-04 at 2026-04-15T14:01:00Z by security-automation, described as "Scheduled signing-key rotation".
  - `F32` ✔ Caller-scoped config enumeration in the same window (2026-04-15T13:50Z..14:10Z) returns exactly ONE config change across all services: cfg-2705, auth-service, key jwt.signing.key_id, old_value k-2025-10 -> new_value k-2026-04 at 2026-04-15T14:01:00Z by security-automation, description "Scheduled signing-key rotation" (count=1, scope='all services'). This is the key-material rotation event, and its timestamp is 14:01:00Z — one minute AFTER the 14:00:00Z deployment.
  - `F14` ✔ auth-service error_rate is a genuine step change measured on its own metric backend: 0.26% at 14:00Z, 0.16% at 14:01Z, then 36.08% at 14:02Z, 38.34% at 14:03Z, holding 36.22-38.53% through 14:06Z at 60-second resolution. The onset is between 14:01:00Z and 14:02:00Z.
  - `F23` ✔ Onset confirmed at 60s resolution: error_rate goes 14:00Z=0.26, 14:01Z=0.16, then 14:02Z=36.08, 14:03Z=38.34 (points continue 36.22, 37.97, 38.53 ... 34.40, 34.92 through 14:10Z), so the step is between 14:01:00Z and 14:02:00Z — mid-minute, immediately after the 14:00:00Z v7.3.0 deployment (F4) and the 14:01:00Z key rotation (F5), which this metric cannot separate.
  - `F8` ✔ auth-service error_rate left baseline abruptly between 14:00:00Z (avg 0.21%) and 14:02:00Z (avg 37.21%) — a sharp step, not a ramp, so true onset is ~14:01-14:02Z, about 10 minutes before the 14:12 report time.
  - `F9` ✔ The elevated error rate is a stable step of ~36-37%, not transient or growing: it holds ~35-37% continuously from 14:02Z through 15:56Z (last point avg 36.53).
  - `F18` ✔ The auth path's shared dependencies show no degradation across the onset: redis-cache hit_ratio_pct is flat at 95.51-96.46% (mean 95.97) for 13:00-15:48Z, and user-db slow_queries_per_min is flat at 0.26-0.34 (mean 0.3) in the same window, so cache-miss or slow-store explanations are not supported by these two metrics.
  - `F36` ✔ Historical precedent search for secret/key-material publication, DNS or mesh-routing changes returns exactly ONE incident: HIST-108 (2025-10-01), title "401s after JWT signing key rotation", services [auth-service], symptoms ["401 invalid_token", "users logged out"], root_cause "Key rotation published new key id before all pods loaded the JWKS", category config_change, match_score 2. This is a lead only, not evidence about INC-007.
**Contradicting evidence**
  - `F4` ✔ Across all services, exactly 2 deployments exist in 2026-04-13T00:00Z..2026-04-15T16:00Z: market-data-service v5.0.3 (11:35:00Z, ci-bot, "Fix symbol mapping") and auth-service v7.3.0 (14:00:00Z, d.verma, "Upgrade JWT library to 4.x; refactor token validation", changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'). Both have status=live and rolled_back_at=null.
  - `F16` ✔ The failure is uniform-by-caller, not a subset of clients: api-gateway (the caller of auth-service in the dependency graph) steps at the same time and its error_rate rises from a ~0.2% baseline to ~21% (13:56Z avg 5.48/max 21.73 partial bucket, then 21.15, 21.01, 21.2... 20.96 to 15:56Z), i.e. a large but different fraction than auth-service's ~36%.
  - `F30` ✔ Consistent with the denominator reading, auth-service's flat ~400 req/s against the gateway's ~1274 req/s means the gateway routes ~68% of its traffic away from auth-service; if 36% of the ~400 auth validations/s are rejected, that alone can only account for ~11% of the gateway's 1274 req/s — so an additional ~10 points of the gateway's 21% must come from other gateway paths feeding off the same rejected tokens or additional failed calls, not from auth-service alone.
**Assumptions**
  - A JWKS publication/propagation lag or key-id mismatch actually occurred — no JWKS/key-lookup telemetry exists (F20 jwks_key_resolution_miss not_found).
  - The 36% share corresponds to tokens requiring the rotated kid rather than an instance split — untested (F24).
  - The rotation, not the 14:00Z deploy, is the operative change; both fall inside the same 60s onset bucket (F23).

### H3: Partial failure: one of three auth-service replicas/AZ rejects all auth validations
The stable ~36% plateau is a 1-of-3 split: one auth-service replica (or one AZ of the auth path, plausibly a bad instance created by the 14:00Z rollout) rejects all validations while the other two serve normally, so only ~1/3 of requests get 401 invalid_token.

**Causal chain**
1. The error step is a near-exact 1/3 plateau (36.05-37.49%) — but this magnitude is degenerate between a uniform fraction and a one-of-three partition, and no per-instance/AZ series exists (F20/F24). [F12, F24, F9]
2. Onset is a sharp step at 14:01-14:02Z with no change in aggregate request rate or resource usage, consistent with one share of the fleet failing outright. [F14, F23, F10, F11]
3. The failing domain rejects validations locally (invalid_token) rather than erroring on a dependency — shared cache and user-db metrics are flat and portfolio-service (same cache consumer) is unaffected. [F18, F17, F28]
4. The failure is not a pod restart or throttling artifact: restarts flat at 0.0 and rate_limited flat across the onset. [F21, F40, F22]
5. No lifecycle/instance source exists to test the partition (no pod_start_time/uptime, no kubernetes service), so the partition cannot be observed either way. [F41, F42]

**Supporting evidence**
  - `F12` ✔ The steady ~36% (36.05-37.49% across the plateau) is consistent with exactly one of three failure domains (one replica or one AZ of a 3-way split) rejecting, rather than a broad dependency outage; this is an inference from the near-1/3 magnitude, replica/AZ breakdown data was not obtained.
  - `F9` ✔ The elevated error rate is a stable step of ~36-37%, not transient or growing: it holds ~35-37% continuously from 14:02Z through 15:56Z (last point avg 36.53).
  - `F14` ✔ auth-service error_rate is a genuine step change measured on its own metric backend: 0.26% at 14:00Z, 0.16% at 14:01Z, then 36.08% at 14:02Z, 38.34% at 14:03Z, holding 36.22-38.53% through 14:06Z at 60-second resolution. The onset is between 14:01:00Z and 14:02:00Z.
  - `F10` ✔ The error step is NOT correlated with traffic: auth-service request_rate is flat at ~400 req/s both before and after onset (13:00-13:56 avg ~400, 14:02-15:56 avg ~400), with no traffic spike at 14:00-14:02.
  - `F11` ✔ auth-service looks normal-but-rejecting: latency_p99_ms stays ~81-101ms (mean 90.01) with no change across the 14:02 onset, CPU stays ~37-42% (mean 38.02) and memory ~47-58% (mean 51.89) both flat — no latency/CPU/memory abnormality accompanies the 401 step.
  - `F21` ✔ Fallback for the missing per-instance dimension, auth-service restarts (count, cumulative) is flat at 0.0 for every 120s bucket from 13:30:00Z through 14:30:00Z (61 points, min 0.0, max 0.0, missing_points 0) — i.e. 0 restart events and no restart-induced process-start step anywhere around the 14:00-14:02Z onset. So the failure was not triggered by (and did not cause) any auth-service pod restart in this window.
  - `F22` ✔ The only other discriminating metric on auth-service, rate_limited_per_min, stays at its normal ~2 req/min level across the onset (13:30 2.26, 13:58 2.0, 14:00 1.49, 14:02 2.35, 14:04 2.25, mean 2.01, max 3.23, missing_points 0), with no step at 14:02Z. The 401s are therefore not being produced by rate limiting; the error_rate step and the rejection volume are uncorrelated with throttling.
**Contradicting evidence**
  - `F16` ✔ The failure is uniform-by-caller, not a subset of clients: api-gateway (the caller of auth-service in the dependency graph) steps at the same time and its error_rate rises from a ~0.2% baseline to ~21% (13:56Z avg 5.48/max 21.73 partial bucket, then 21.15, 21.01, 21.2... 20.96 to 15:56Z), i.e. a large but different fraction than auth-service's ~36%.
  - `F30` ✔ Consistent with the denominator reading, auth-service's flat ~400 req/s against the gateway's ~1274 req/s means the gateway routes ~68% of its traffic away from auth-service; if 36% of the ~400 auth validations/s are rejected, that alone can only account for ~11% of the gateway's 1274 req/s — so an additional ~10 points of the gateway's 21% must come from other gateway paths feeding off the same rejected tokens or additional failed calls, not from auth-service alone.
  - `F24` ✔ Because no per-instance/AZ series exists, the near-1/3 magnitude is the only partition evidence available, and it is degenerate: with n=3 replicas, 'one replica fully failing' and 'a uniform 36% of tokens failing on all replicas' produce an identical aggregate error_rate. The metric backend supplies nothing that discriminates, so H3 remains untested rather than supported by this data.
  - `F42` ✔ H3 (one-of-three replica/AZ rejecting) and H6 (intra-fleet version skew) remain UNTESTED, not refuted: with no per-instance dimension anywhere (F20) and no lifecycle source (above), the aggregate ~36% cannot be split, and the observed restart-free, steady-state fleet is equally consistent with 'all three replicas uniformly reject a fixed ~36% of tokens' and with 'one replica rejects everything'. The only surviving positive signal about fleet composition is the absence of any restart, and even that is silent about replicas that were replaced rather than restarted in place.
**Assumptions**
  - auth-service runs at least 3 replicas / serves a 3-way replica or AZ split — never confirmed (F13, F20, F41).
  - The 1/3 magnitude is not coincidental (F24 shows it is non-identifiable).
  - Any failing replica is causally tied to the 14:00Z rollout rather than incidental.

### H6: Incomplete 14:00Z rollout: intra-fleet version skew between replicas serving old and new auth code
At 14:00Z v7.3.0 was rolled to only part of auth-service's fleet (or one replica stayed on old code / old JWKS state), leaving two mismatched validation behaviours in the fleet: the skewed share of traffic hits a validator that rejects tokens the other share accepts, producing a flat ~1/3 step on auth-service.

**Causal chain**
1. auth-service v7.3.0 deploys at 14:00:00Z, is live, rolled_back_at=null; it is the ONLY deployment in the window and no partial/rolling-rollout event is recorded. [F4, F31, F33]
2. The rollout leaves part of the fleet on the old validation code/state, so two validation behaviours coexist. *(assumed)*
3. The skewed share of traffic is rejected at a flat ~1/3 rate for the whole plateau rather than a brief ramp. [F14, F23, F9]
4. No restart accompanies the step, consistent with a rollout config/partition issue or a process that never restarted. [F21, F40]
5. The gateway's absolute error volume exceeds what auth's 36% supplies, which could reflect a second skewed validation path — but F26/F30 attribute the gap to the gateway serving non-auth routes, so no second mechanism is required. [F16, F25, F26, F30]

**Supporting evidence**
  - `F4` ✔ Across all services, exactly 2 deployments exist in 2026-04-13T00:00Z..2026-04-15T16:00Z: market-data-service v5.0.3 (11:35:00Z, ci-bot, "Fix symbol mapping") and auth-service v7.3.0 (14:00:00Z, d.verma, "Upgrade JWT library to 4.x; refactor token validation", changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'). Both have status=live and rolled_back_at=null.
  - `F9` ✔ The elevated error rate is a stable step of ~36-37%, not transient or growing: it holds ~35-37% continuously from 14:02Z through 15:56Z (last point avg 36.53).
  - `F14` ✔ auth-service error_rate is a genuine step change measured on its own metric backend: 0.26% at 14:00Z, 0.16% at 14:01Z, then 36.08% at 14:02Z, 38.34% at 14:03Z, holding 36.22-38.53% through 14:06Z at 60-second resolution. The onset is between 14:01:00Z and 14:02:00Z.
  - `F21` ✔ Fallback for the missing per-instance dimension, auth-service restarts (count, cumulative) is flat at 0.0 for every 120s bucket from 13:30:00Z through 14:30:00Z (61 points, min 0.0, max 0.0, missing_points 0) — i.e. 0 restart events and no restart-induced process-start step anywhere around the 14:00-14:02Z onset. So the failure was not triggered by (and did not cause) any auth-service pod restart in this window.
  - `F30` ✔ Consistent with the denominator reading, auth-service's flat ~400 req/s against the gateway's ~1274 req/s means the gateway routes ~68% of its traffic away from auth-service; if 36% of the ~400 auth validations/s are rejected, that alone can only account for ~11% of the gateway's 1274 req/s — so an additional ~10 points of the gateway's 21% must come from other gateway paths feeding off the same rejected tokens or additional failed calls, not from auth-service alone.
**Contradicting evidence**
  - `F9` ✔ The elevated error rate is a stable step of ~36-37%, not transient or growing: it holds ~35-37% continuously from 14:02Z through 15:56Z (last point avg 36.53).
  - `F26` ✔ Converting to absolute counts, the two services are NOT inconsistent: auth-service ~399.8 req/s x 36.17% = ~144.6 failed validations/s; api-gateway ~1274.07 req/s x 21.08% = ~268.6 failed req/s. The gateway's absolute error volume is ~1.86x auth-service's, and auth-service's errors account for ~53.8% (144.6/268.6) of the gateway's errors — so the same underlying validation failure, weighted by the gateway's larger traffic mix, explains both percentages (the gateway also serves non-auth routes, hence >50%).
  - `F30` ✔ Consistent with the denominator reading, auth-service's flat ~400 req/s against the gateway's ~1274 req/s means the gateway routes ~68% of its traffic away from auth-service; if 36% of the ~400 auth validations/s are rejected, that alone can only account for ~11% of the gateway's 1274 req/s — so an additional ~10 points of the gateway's 21% must come from other gateway paths feeding off the same rejected tokens or additional failed calls, not from auth-service alone.
  - `F31` ✔ Caller-scoped change enumeration in the incident window (2026-04-15T13:50Z..14:10Z) returns exactly ONE deployment across all services: dep-5710, auth-service v7.3.0, deployed_at 2026-04-15T14:00:00Z by d.verma, "Upgrade JWT library to 4.x; refactor token validation"; scope='all services', partial=false, count=1. No second/rolling/partial-rollout event and no replica-set or pod-eviction record appears in the deployments source.
  - `F42` ✔ H3 (one-of-three replica/AZ rejecting) and H6 (intra-fleet version skew) remain UNTESTED, not refuted: with no per-instance dimension anywhere (F20) and no lifecycle source (above), the aggregate ~36% cannot be split, and the observed restart-free, steady-state fleet is equally consistent with 'all three replicas uniformly reject a fixed ~36% of tokens' and with 'one replica rejects everything'. The only surviving positive signal about fleet composition is the absence of any restart, and even that is silent about replicas that were replaced rather than restarted in place.
**Assumptions**
  - auth-service runs more than one replica type/version (never confirmed; no per-replica series, F13/F20/F41).
  - The 14:00Z rollout was partial or non-atomic — F31 records a single non-partial deploy event with partial=false.
  - The un-upgraded replica was never cycled (F21/F40 cannot see replaced-not-restarted pods).

### H4: Shared redis-cache degradation indirectly breaking auth validation for a subset of paths
The shared redis-cache used by auth-service (and three other services) is degraded in a way that only affects auth's validation/session-lookup path, so a subset of validations cannot be completed and surface as 401 invalid_token.

**Causal chain**
1. auth-service depends on redis-cache, flagged shared=true and consumed by four services, so a cache fault can reach auth-service indirectly. [F1, F2]
2. A cache degradation causes a subset of token/session validations to fail, surfacing as 401 invalid_token for ~36% of calls. *(assumed)*
3. The failure appears as a sharp step and persists at a stable fraction for two hours. [F14, F9]

**Supporting evidence**
  - `F1` ✔ auth-service's dependency graph: api-gateway calls auth-service over http; auth-service in turn depends on user-db (postgres), redis-cache (cache) and kyc-provider (external, has_telemetry=false). redis-cache is flagged shared=true.
  - `F2` ✔ Blast radius of the shared cache: redis-cache is consumed by four services (auth-service, portfolio-service, pricing-engine, market-data-service), so a redis-cache change or failure can affect auth-service indirectly through a neighbour.
**Contradicting evidence**
  - `F17` ✔ Failure does NOT spread to every consumer of the shared cache: portfolio-service (per F2 a redis-cache consumer) stays at baseline error_rate ~0.17-0.25% throughout 13:00-15:51Z, unaffected by the 14:02 step, so the failure is confined to the auth validation path rather than any service touching redis-cache.
  - `F18` ✔ The auth path's shared dependencies show no degradation across the onset: redis-cache hit_ratio_pct is flat at 95.51-96.46% (mean 95.97) for 13:00-15:48Z, and user-db slow_queries_per_min is flat at 0.26-0.34 (mean 0.3) in the same window, so cache-miss or slow-store explanations are not supported by these two metrics.
  - `F19` ✔ INCOMPLETE / NOT REACHED: redis-cache and user-db expose no error_rate metric, so component-level error clustering on the shared dependencies could not be measured; the available metric sets are connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct (redis-cache) and connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min (user-db).
  - `F28` ✔ The failure does not propagate into the auth path's datastores: user-db connections_active over 14:02-15:00Z is flat (mean 25.24, min 22.78, max 27.21, missing_points 0), and redis-cache evictions_per_min is also flat (mean 3.02, min 1.48, max 4.58), i.e. no connection exhaustion, eviction storm or load shift at the 14:02 onset in either dependency.
**Assumptions**
  - A redis-cache fault exists that degrades only the auth path despite four consumers.
**Rejected because:** Contradicted: redis-cache hit_ratio_pct is flat at 95.5-96.5% and user-db slow_queries flat across the onset (F18); evictions and connections_active also flat (F28); and the failure does not spread to portfolio-service, another consumer of the same shared cache (F17). A shared-dependency fault confined to one consumer's path with a flat cache hit ratio is unsupported.

### H5: Telemetry/log-pipeline artifact inflating the apparent 36% 401 rate
The reported 36% invalid_token rate and the 401 step are at least partly an artifact of the broken identity-namespace log/metric pipeline rather than a real increase in rejected validations.

**Causal chain**
1. The identity-namespace log pipeline has been down since the morning and auth-service logs were unsearchable. [F13, F37]
2. Missing or distorted ingestion could shift the computed error fraction upward, producing an apparent 36% 401 rate. *(assumed)*
3. The 401 step is reported on auth-service's own metric at 14:02Z and persists. [F14, F9]

**Supporting evidence**
  - `F13` ✔ Per-replica, per-AZ, and dependent-service telemetry was NOT retrievable: query_logs for auth-service returned backend 503, and query_metrics for 'identity-service' returned not_found (known services list contains no identity-service; user-db/redis-cache/kafka/api-gateway were never queried before the budget ran out).
  - `F37` ✔ The auth-service log pipeline is still down (backend unavailable (503), gave up after 3 attempts) for 13:55-14:10Z, confirming the incident's own caveat: no auth-service-side log evidence exists for this window and nothing about which replica logged what can be obtained from it.
**Contradicting evidence**
  - `F15` ✔ The 401 step is NOT explained by missing log/metric ingestion: auth-service request_rate is flat across the onset (13:56-14:06Z averages 394.35, 401.32, 389.78, 376.59, 424.96, 380.77, 404.65, 389.92, 406.43, 387.38, 401.91 req/s; no drop in traffic or in data points, missing_points=0), so the error fraction is being computed over a full, undegraded request volume.
  - `F14` ✔ auth-service error_rate is a genuine step change measured on its own metric backend: 0.26% at 14:00Z, 0.16% at 14:01Z, then 36.08% at 14:02Z, 38.34% at 14:03Z, holding 36.22-38.53% through 14:06Z at 60-second resolution. The onset is between 14:01:00Z and 14:02:00Z.
  - `F10` ✔ The error step is NOT correlated with traffic: auth-service request_rate is flat at ~400 req/s both before and after onset (13:00-13:56 avg ~400, 14:02-15:56 avg ~400), with no traffic spike at 14:00-14:02.
  - `F16` ✔ The failure is uniform-by-caller, not a subset of clients: api-gateway (the caller of auth-service in the dependency graph) steps at the same time and its error_rate rises from a ~0.2% baseline to ~21% (13:56Z avg 5.48/max 21.73 partial bucket, then 21.15, 21.01, 21.2... 20.96 to 15:56Z), i.e. a large but different fraction than auth-service's ~36%.
**Assumptions**
  - That the metric backend computing error_rate is itself affected by the log-pipeline outage.
**Rejected because:** Contradicted: the step was measured on auth-service's own metric backend with missing_points=0 and a full, undegraded request volume (F15, F14, F10), and api-gateway shows a synchronous error rise on its own telemetry (F16). The log-pipeline outage explains why error text is unreadable, not why the metric stepped.

## Facts (verified observations)
- auth-service error_rate steps abruptly from 0.26%/0.16% at 14:00-14:01Z to 36.08% at 14:02Z, i.e. onset between 14:01:00Z and 14:02:00Z. [F8, F14, F23]
- The elevated error rate is a stable plateau of ~35-37% continuously from 14:02Z through 15:56Z, not transient or growing. [F9]
- The step is not correlated with traffic: request_rate is flat at ~400 req/s before and after onset with no spike at 14:00-14:02Z. [F10, F15]
- auth-service looks resource-normal while rejecting: latency_p99 ~81-101ms, CPU ~37-42%, memory ~47-58%, all flat across the onset. [F11]
- The error fraction is computed over a full, undegraded request volume with missing_points=0, so the step is not a telemetry-ingestion artifact. [F14, F15]
- api-gateway, the caller of auth-service, shows a synchronous error rise from ~0.2% baseline to ~21% over the same window. [F16]
- api-gateway's ~21% and auth-service's ~36% are reconcilable arithmetically: ~1274.07 req/s x 21.08% = ~268.6 failed req/s vs ~399.8 req/s x 36.17% = ~144.6 failed validations/s, with auth-service accounting for ~53.8% of the gateway's errors, i.e. a denominator/traffic-mix effect rather than two distinct failure modes. [F25]
- Neither service shows throttling that could explain the difference: auth-service rate_limited_per_min stays flat at ~2 req/min across the onset and api-gateway exposes no rate_limited metric. [F22, F27]
- Shared-dependency degradation is not visible: redis-cache hit_ratio_pct flat at 95.51-96.46%, user-db slow_queries_per_min flat at 0.26-0.34, user-db connections_active flat (~25.24), redis-cache evictions_per_min flat (~3.02). [F18, F28]
- The failure does not spread to other consumers of the shared redis-cache: portfolio-service stays at baseline ~0.17-0.25% error_rate throughout 13:00-15:51Z. [F17]
- Exactly two deployments exist in the window 2026-04-13T00:00Z-2026-04-15T16:00Z: market-data-service v5.0.3 (11:35Z) and auth-service v7.3.0 (14:00:00Z, 'Upgrade JWT library to 4.x; refactor token validation', jjwt 0.11->0.12 breaking parser API, TokenValidator rewritten), both status=live, rolled_back_at=null. [F4]
- Caller-scoped enumeration of the incident window (13:50-14:10Z) returns exactly one deployment: dep-5710, auth-service v7.3.0 at 14:00:00Z, scope='all services', partial=false, count=1; no second/rolling/partial rollout or pod-eviction record. [F31]
- Exactly one config change exists across all services in the window: cfg-2705, auth-service jwt.signing.key_id k-2025-10 -> k-2026-04 at 14:01:00Z by security-automation, 'Scheduled signing-key rotation'. [F5, F32]
- Service-scoped queries agree with global ones: for auth-service only dep-5710 (14:00:00Z) and cfg-2705 (14:01:00Z) exist; the change sources expose no secret-publish, mesh-route, DNS, autoscaler, replica-set or eviction object type. [F33]
- The deployment (14:00:00Z) and the key rotation (14:01:00Z) both fall inside the same unresolvable 60-second onset bucket, so the metric cannot order them causally. [F23, F32]
- No pod restart occurred around onset: auth-service restarts is 0.0 at every bucket from 13:30Z through 14:30Z (and 13:55-14:10Z), missing_points 0. [F21, F40]
- No per-instance, per-AZ, per-kid/issuer/tenant dimension exists on auth-service's metric backend (complete set: cpu_pct, error_rate, latency_p99_ms, memory_pct, rate_limited_per_min, request_rate, restarts); error_rate_by_instance, error_rate_by_az and jwks_key_resolution_miss all returned not_found, so the ~36% cannot be partitioned. [F20]
- No error-signature/status-class metric exists on either endpoint (auth-service and api-gateway expose only aggregate error_rate whose unit is HTTP 5xx), so invalid_token vs expired vs unable-to-validate cannot be separated on the metric side. [F29]
- auth-service logs are unsearchable: query_logs returned backend unavailable (503) after 3 attempts for 13:55-14:10Z, leaving no auth-service-side log evidence for the window. [F13, F37]
- A non-metric source covers the window: api-gateway WARN logs returned 31 matches from 14:02:22Z to 14:09:56Z, all of the pattern 'auth-service POST /v<n>/token/validate -> <n> invalid_token (trace <n>)' with 30 unique trace IDs spread continuously and no clustering on a client or route beyond POST /v1/token/validate, but no pod/replica/instance attribute. [F38, F39]
- No container/pod-lifecycle, apiserver-event or process-start source is reachable: pod_start_time and uptime_seconds return not_found, and 'kubernetes'/'kube-system' are unknown services, so instance count and instance add/remove in 13:55-14:10Z are unmeasurable. [F41]
- The 'identity' namespace is not queryable: get_deployments/get_config_changes for 'identity-service' return not_found, and the 18 known_services contain no identity-service and no separate secret/DNS/mesh/JWKS object. [F34]
- auth-service's object graph contains only user-db, redis-cache and kyc-provider — no JWKS/key-store/secret-store, mesh/ingress, DNS or load-balancer node — so such changes would not surface as a dependency-graph object. [F35]
- Historical precedent search returned exactly one matching incident: HIST-108 (2025-10-01), '401s after JWT signing key rotation', root cause 'Key rotation published new key id before all pods loaded the JWKS', category config_change; HIST-109 is the analogous 'token validation regression after auth library upgrade' incident — both are leads only, not evidence about INC-007. [F36]
- The baseline period 2026-04-11T00:00Z-2026-04-13T00:00Z is clean (0 deployments, 0 config changes), so the only recent changes are those on 04-15. [F6]

## Assumptions (not verified)
- Assumed: the reported '401 invalid_token' symptom text reflects the actual failing code path; no auth-service log or status-class metric was obtainable to verify the signature (F13/F29/F37).
- Assumed (H1): the rewritten JWT 4.x parser in v7.3.0 actually rejects a deterministic subset of otherwise-valid tokens; no error signature or per-claim/per-token dimension exists to demonstrate this (F20/F29).
- Assumed (H2): the 14:01Z key rotation published k-2026-04 without every validating component loading the matching JWKS entry; no JWKS publication object or key-resolution metric is queryable (F20/F34/F35).
- Assumed (H3): auth-service runs at least three replicas or serves a 3-way replica/AZ split; replica count and per-instance series were never obtainable (F20/F41).
- Assumed: the ~36% is a property of token/claim mix (or of a replica split) rather than of the traffic mix; this is untestable with the available dimensions (F20/F24).
- Assumed: the 14:00Z deploy is causally prior to onset; F23 places the deploy and rotation in the same 60s bucket, so neither ordering nor exclusivity is evidenced.
- Assumed: 'no restart' implies no replica was cycled; a rollout that replaces pods without incrementing the restarts counter would not be visible (F40).
- Assumed: the absence of secret/DNS/mesh/replica objects in the deployment and config sources reflects genuine absence of such changes rather than those sources being partial (F31/F32/F33).
- (demoted from facts: no verified observation backs it) The near-1/3 magnitude is degenerate as partition evidence: with n=3 replicas, 'one replica fully failing' and 'a uniform 36% of tokens failing on all replicas' produce an identical aggregate error_rate, so H3 is untested rather than supported.

## Timeline
- `2026-04-13T00:00:00Z` Baseline window 04-11 to 04-13 confirmed clean: 0 deployments, 0 config changes across all services. [F6]
- `2026-04-15T13:30:00Z` auth-service restarts remain flat at 0.0 in every bucket; rate_limited_per_min at its normal ~2 req/min level. [F21, F22]
- `2026-04-15T13:55:00Z` auth-service log pipeline still down (backend 503 after 3 attempts) for 13:55-14:10Z; no auth log evidence for the window. [F37, F13]
- `2026-04-15T14:00:00Z` Deployment dep-5710: auth-service v7.3.0 goes live ('Upgrade JWT library to 4.x; refactor token validation'; jjwt 0.11->0.12 breaking parser API; TokenValidator rewritten), scope='all services', partial=false, rolled_back_at=null. [F4, F31]
- `2026-04-15T14:00:00Z` auth-service error_rate 0.26% and request_rate ~400 req/s — still at baseline immediately after the deploy. [F14, F15]
- `2026-04-15T14:01:00Z` Config change cfg-2705: auth-service jwt.signing.key_id rotated k-2025-10 -> k-2026-04 by security-automation ('Scheduled signing-key rotation'). [F5, F32]
- `2026-04-15T14:01:00Z` auth-service error_rate 0.16% at the 14:01Z sample — still baseline one minute after deploy and at rotation time. [F14, F23]
- `2026-04-15T14:02:00Z` auth-service error_rate jumps to 36.08% (38.34% at 14:03Z), onset in the 14:01-14:02Z bucket; request_rate unchanged at ~400 req/s. [F8, F14, F15, F23]
- `2026-04-15T14:02:22Z` First api-gateway WARN entries: 'auth-service POST /v1/token/validate -> invalid_token' with trace IDs (e.g. 09dac90d, 6fb4c9a8); the pattern repeats continuously to 14:09:56Z, no pod identifier. [F38, F39]
- `2026-04-15T14:02:00Z` api-gateway error_rate steps synchronously from ~0.2% baseline to ~21% while handling ~1274 req/s. [F16, F25]
- `2026-04-15T14:02:00Z` No accompanying abnormality: auth-service latency_p99 ~90ms, CPU ~38%, memory ~52%, rate_limited ~2/min, restarts 0.0; redis-cache hit_ratio and user-db slow_queries/connections flat. [F11, F21, F22, F18, F28]
- `2026-04-15T14:12:00Z` Incident reported: ~1/3 of token validations failing with 401 invalid_token; auth-service error rate ~36%. [F8]
- `2026-04-15T15:56:00Z` Plateau persists: error_rate still ~36.53% with no rollback applied (rolled_back_at=null), i.e. the change(s) were never reverted. [F9, F4]

## Recommended actions
- **[immediate]** Roll back auth-service to the pre-14:00Z version (revert dep-5710, v7.3.0) so the JWT 0.11 parser/TokenValidator is restored; this is a single, bounded change and is unambiguously one of the two candidate triggers. -- The ~36% step began within the same 60s bucket as the v7.3.0 deploy that rewrote token validation on a breaking library upgrade (F4/F23/F31), and no rollback has been applied (rolled_back_at=null). H1 is one of the two co-leading hypotheses at 0.40 and is reversible within minutes; the change is confined to one service and the request volume is unaffected, so the rollback carries low collateral risk. [F4, F31, F23, F14, F9]
- **[immediate]** Simultaneously verify key resolution before concluding: check that every auth-service instance and any JWKS consumer has loaded the k-2026-04 entry published at 14:01:00Z (cfg-2705), and if any component is on the old kid/trust store, complete the rollout/reload or revert jwt.signing.key_id to k-2025-10. -- H2 (signing-key rotation broke key resolution for a subset of validations) is tied with H1 at 0.40 and sits in the same onset bucket (F5/F23/F32); the historic precedent HIST-108 is exactly this failure mode (F36). This action is the counterpart mitigation to the rollback and can be done in parallel. [F5, F32, F23, F36]
- **[immediate]** Obtain an error-signature/status-class split for the failing validations (invalid_token vs expired vs unable-to-validate) or any per-instance routing data — e.g. export auth-service request logs from the api-gateway side, add a trace-ID join, or inspect the trace backend with the trace IDs already captured (09dac90d, 6fb4c9a8, 30 total from F38/F39). -- This is the single most decisive discriminator between H1 (parser rejects a token subset) and H2 (key resolution fails) and it is currently missing only because the destination metric/log source lacks it (F29/F13/F37). The api-gateway trace IDs are the one reachable artefact that can be joined to a per-request reason without waiting on the identity log pipeline. [F38, F39, F29, F13, F37]
- **[short_term]** Instrument and measure per-replica / per-AZ error_rate and per-kid key-resolution counters (e.g. add error_rate_by_instance and a jwks_key_resolution_miss metric), then re-measure during a controlled canary or after the mitigation to see whether the failure is one-of-three (H3/H6) or a uniform per-token fraction (H1/H2). -- The ~36% plateau is degenerate as partition evidence: with n=3 replicas, one replica failing and a uniform 36% failure produce an identical aggregate (F12/F24), and no dimensioned series exists (F20). This single addition converts the central open question into a measurable one. [F12, F20, F24, F41]
- **[short_term]** After the change is reverted or corrected, confirm recovery by watching auth-service error_rate return to the ~0.2% baseline and api-gateway error_rate fall back from ~21%; hold the change reverted until the error rate is stable for at least 30 minutes at the plateau's duration. -- The plateau has held stable for ~2 hours (F9), so a mitigation is only confirmed if the step reverses durably; this also distinguishes a change-triggered regression from a self-limiting event. [F9, F16, F8]
- **[short_term]** Restore the identity-namespace log pipeline (the separate open ticket referenced in the incident) and ensure auth-service emits per-pod logs with an explicit validation-failure reason and the key id used. -- The 503 on auth-service logs is what forced this investigation into a metric-only, dimension-poor position (F13/F37); restoring the pipeline directly removes the blocker for H1-vs-H2 discrimination in this and future incidents. [F13, F37, F29]
- **[long_term]** Adopt a deploy-and-rotate separation policy: never land a breaking JWT parser/library upgrade and a signing-key rotation within the same maintenance window, and gate JWT library major upgrades (jjwt 0.11->0.12) behind a canary with token-mix validation and a rollback runbook. -- Both candidate triggers landed one minute apart inside the same unresolvable onset bucket (F4/F5/F23/F31/F32), which is precisely what made this incident undecidable; precedent HIST-108 and HIST-109 show both change types have caused identical 401-outage patterns before (F7/F36). [F4, F5, F23, F31, F32, F7, F36]
- **[long_term]** Add key-rotation readiness checks to the scheduled rotation automation: verify all validating components have loaded the new kid before activating it, and add an explicit jwks_key_resolution_miss alert. -- HIST-108 was exactly 'key rotation published new key id before all pods loaded the JWKS' (F36) and the 14:01Z rotation (F5/F32) is a candidate trigger; a readiness gate plus a miss alert would prevent and immediately identify this class of recurrence. [F5, F32, F36]
- **[long_term]** Add per-instance/AZ/version and per-token-dimension observability to auth-service and the validation path (including pod-lifecycle/apiserver event visibility) as a standing capability. -- The investigation could not obtain per-replica, per-AZ, per-kid, lifecycle or status-class data from any reachable source (F20/F29/F41), which is the structural reason the three hypotheses could not be separated. [F20, F29, F41, F24]

## Data gaps
- No error-signature or status-class data: no invalid_token vs expired vs unable-to-validate split exists on either endpoint (auth-service and api-gateway expose only aggregate error_rate, unit 'HTTP 5xx'), and auth-service logs are unsearchable (503) — this is the single most valuable missing dimension for choosing between H1 and H2 (F13, F29, F37).
- No per-instance, per-AZ, per-kid/issuer/tenant, or per-replica error partition: all dimensioned queries return not_found, so the ~36% cannot be split and 'one-of-three failing' cannot be separated from 'uniform 36% failing' (F20, F24).
- No JWKS/key-resolution telemetry: jwks_key_resolution_miss does not exist and there is no queryable JWKS publication, key-activation or trust-store object, so H2's propagation-lag claim is unverifiable rather than supported (F20, F34, F35).
- No pod/container lifecycle, apiserver-event or process-start source anywhere ('kubernetes'/'kube-system' unknown; pod_start_time/uptime_seconds not_found), so instance count and instance add/remove in the onset window are unmeasurable (F40, F41).
- Reachable non-metric evidence is thin: api-gateway WARN logs give 30 unique trace IDs for failing /v1/token/validate calls but carry no pod/replica attribute and no trace backend was reachable to join them to a validation reason (F38, F39).
- Change-source coverage is bounded by schema: only deployments and config_changes are enumerable, so secret/DNS/service-mesh/autoscaler/replica-set changes cannot be proven absent — their absence in the returned sets is not proof they do not exist elsewhere (F31, F32, F33, F34).
- The 'identity' namespace is not queryable at all, so any namespace-scoped secret/DNS/mesh source the incident points at cannot be enumerated (F34).
- Shared-dependency health was sampled with one metric each (redis-cache hit_ratio_pct, user-db slow_queries_per_min) because redis-cache and user-db expose no error_rate metric; a saturation-only failure cannot be fully excluded (F18, F19, F28).
- Historical precedent search was limited and returned only HIST-108, so H3 (partial replica/AZ failure) has no precedent comparison either for or against (F36, F7).

## What would change this conclusion
- A readable auth-service log line or status-class metric showing the failure reason: 'invalid_token' with a key-id/JWKS-miss signature would move the conclusion to H2; a parser/crypto exception (e.g. claim-parsing or algorithm-allowlist error) on a deterministic token subset would move it to H1.
- A per-instance or per-AZ error_rate showing one replica at ~100% error while the others sit at baseline would move the conclusion to H3 (or H6 if the deviating replica runs different code/version), and would overturn the uniform-fraction framing.
- A per-token/kid split showing failures concentrated on tokens signed with the new k-2026-04 key would support H2 and reorder it above H1.
- Rollback of v7.3.0 alone resolving the error rate to baseline would confirm H1; conversely, completing/repairing key resolution (or reverting the rotation) resolving it would confirm H2.
- Evidence of a JWKS publication/propagation event around 14:01Z that outran pod JWKS loading would confirm H2's load-bearing hop, which is currently assumed rather than evidenced.
- Resolution of the plateau with no change applied (self-clearing) would falsify all three change-triggered hypotheses and reopen the field.
- Discovery of a partial rolling update, pod replacement or a replica running old code (e.g. a pod lifecycle/apiserver record, which is currently unqueryable) would support H6 and potentially H3.

## Integrity warnings (set by code)
- fact demoted to assumption: The near-1/3 magnitude is degenerate as partition evidence: with n=3 replicas, '

## Investigation stats
- latency 270.38s, LLM calls 32, tokens 283440 (in 234326 / out 49114)
- tool calls 55 (15 failed, 0 partial, 0 recovered by retry), findings 42/42 verified
