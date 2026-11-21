# Incident investigation report: INC-007
*System: multi_agent | outcome: **inconclusive** | ended: critic_inconclusive*

## Summary
The fault is well-localized to auth-service's own token-validation path: a single sharp, non-periodic step at 14:02Z to a flat ~35-37% error rate sustained for ~2h with request_rate unchanged, observed independently at api-gateway with the identical single 'invalid_token' signature, while order-service and all other callers stay clean and shared/upstream causes (redis-cache, gateway, minting tier) are excluded. Three auth-service-relevant changes exist around the onset — dep-5710 (v7.3.0, jjwt 0.11→0.12 + rewritten TokenValidator, 14:00:00Z) and cfg-2705 (kid rotation k-2025-10→k-2026-04, 14:01:00Z) — with neither rolled back or re-published after onset. However, the decisive discriminator between the two leading hypotheses (whether the rejected population contains only old-kid/unconverged tokens vs also new-kid tokens mis-parsed by the new validator) is structurally unavailable in the reachable telemetry: caller logs carry only a trace id, no kid/iss/exp; auth-service's own store is unreachable (503); and no keys/JWKS publication record or per-pod dimension exists. The #1-vs-#2 ranking is an exact tie (H1 0.40 / H2 0.40) decided only by deploy preceding rotation by one minute, so no root cause can be confirmed and the outcome is inconclusive.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bad deployment: JWT library upgrade (jjwt 0.11→0.12) + rewritten TokenValidator in auth-service v7.3.0 | bad_deployment | auth-service | dep-5710 | 0.40 (low) | active |
| 2 | H2: Config change: scheduled signing-key rotation (cfg-2705) to kid k-2026-04 without staged/dual-key propagation | config_change | auth-service | cfg-2705 | 0.40 (low) | active |
| 3 | H5: Partial-fleet failure: one of N auth-service replicas serves bad validations (~33%) | bad_deployment | auth-service | dep-5710 | 0.15 (low) | active |
| 4 | H4: Upstream caller/gateway mints or forwards malformed/expired tokens; auth-service correctly rejects | config_change | api-gateway | null | 0.04 (low) | active |
| 5 | H3: Shared dependency failure: redis-cache (shared token/session store) partially degraded | dependency_failure | redis-cache | null | 0.02 (low) | active |
| 6 | H6: Clock skew / expired signing key on a subset of instances causes invalid_token on valid tokens | infrastructure_failure | auth-service | null | 0.01 (low) | active |
| 7 | H7: Telemetry artifact: 401 spike partly an instrument of the identity-namespace log-pipeline outage | unknown | auth-service | null | 0.01 (low) | active |

### H1: Bad deployment: JWT library upgrade (jjwt 0.11→0.12) + rewritten TokenValidator in auth-service v7.3.0
auth-service v7.3.0 (dep-5710, deployed 14:00:00Z) rewrote TokenValidator and upgraded jjwt 0.11→0.12, a breaking parser API change, 2 minutes before the 14:02Z metric step. The rewritten validator applies changed acceptance semantics (clock-skew default, algorithm/kid strictness, exp/nbf handling) so a deterministic class of previously-valid tokens is now rejected, yielding a steady ~36% invalid_token rate that persists because the deploy was never rolled back. The deploy is the only auth-side change that strictly precedes the symptom and is never reverted.

**Causal chain**
1. dep-5710 (auth-service v7.3.0) deployed at 14:00:00Z with 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live, not rolled back. [F1, F13]
2. The rewritten validator changes token-acceptance semantics so a class of previously-valid tokens is now rejected (HIST-109 shows this symptom class from a JWT library upgrade). [F18]
3. The rejected class is deterministic and fixed, so the error rate steps and stays flat rather than decaying. *(assumed)*
4. auth-service error_rate steps from ~0.2% baseline to ~36% at the 14:00-14:02 window and stays flat at 35-37% through 15:55, with request_rate unchanged (no traffic surge). [F7, F8, F9]
5. The rejection surfaces to callers as 401 invalid_token; api-gateway shows the identical single-pattern signature from 14:02:22Z through 15:58:51Z (a sampled WARN stream, not the literal rate). [F10, F25, F27, F28]
6. No rollback (rolled_back_at null) or post-onset change occurred, so the elevated rate persists for the whole ~2h window. [F19, F23]

**Supporting evidence**
  - `F1` ✔ auth-service was deployed at 2026-04-15T14:00:00Z (dep-5710, v7.3.0) with a breaking JWT library upgrade 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live, not rolled back. This is 12 minutes before the 14:12Z incident report.
  - `F13` ✔ Independent re-query of deployments for 13:30-15:59Z returns exactly one deploy, dep-5710: auth-service v7.3.0 at 2026-04-15T14:00:00Z, author d.verma, changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten', status 'live'. It lands 2 minutes before the metric step and 12 minutes before the 14:12Z report.
  - `F7` ✔ auth-service error_rate (HTTP 5xx) stepped sharply from a flat baseline of ~0.2% (range 0.17-0.28% over 12:00-13:59) to ~36% beginning at the 14:00-14:02 onset window. With 120s buckets, 13:50-14:00 remain <=0.28% and the 14:02:00 bucket jumps to 37.21% avg (max 38.34%); the level then stays flat at 35-37% through 15:55 (36.27%), i.e. a single step, not periodic.
  - `F8` ✔ The onset is bounded to 14:00:00-14:02:00Z: the 120s bucket starting 14:00:00Z is still at baseline (avg 0.21%, max 0.26%) while the 14:02:00Z bucket is already 37.21%. The earlier 300s bucket at 14:00:00Z showing avg 22.21% is a partial-bucket artifact; the finer resolution shows the true step at ~14:02.
  - `F9` ✔ auth-service request_rate is unchanged across the onset: it is ~386-413 req/s both before (e.g. 398.11 at 14:05, 401.35 at 14:00) and after (405.81 at 15:55), so the 36% error share is not driven by a traffic surge or drop.
  - `F10` ✔ The api-gateway independently shows the same 401 invalid_token signature against auth-service, beginning 2026-04-15T14:02:22Z (466 WARN lines matching 'auth-service POST /v<n>/token/validate -> <n> invalid_token', last 15:58:51Z). Gateway error_rate stepped at the same time: 0.24% at 13:59 -> 21.06% at 14:02 and holding ~21% (14:05, 14:08). Note gateway error_rate is 5xx-only, so the 21% reflects the fraction of gateway traffic that proxies to auth-service.
  - `F27` ✔ The rejected-token cohort is a single stable signature, not a mixed/random bag: top_patterns collapses 100% of the 466 matched WARN lines to one pattern 'auth-service POST /v<n>/token/validate -> <n> invalid_token (trace <n>)' (count 466, first_seen 14:02:22Z, last 15:58:51Z), and counts_by_level shows WARN 466 / nothing else.
  - `F28` ✔ The rejection stream is continuous and steady across the whole window rather than bursty or random: sampling early (14:02:22–14:12:28Z) and late (15:52:19–15:58:51Z) shows the same interleaved cadence of roughly one 401 every 10-30s, with the identical message form at both ends and last_match still '2026-04-15T15:58:51Z' — a persistent systematic class, not a varying fraction that dies out.
  - `F18` ✔ Historical precedents for this exact signature are auth-service-side and config/deploy-caused: HIST-108 (2025-10-01, '401s after JWT signing key rotation', root cause 'Key rotation published new key id before all pods loaded the JWKS', resolution 'Re-published previous key; staged rotation') and HIST-109 (2025-07-11, 'Token validation regression after auth library upgrade', root cause 'JWT library upgrade changed clock-skew default'). Both are leads only, and HIST-109 shows the same symptom class can come from the library deploy rather than the rotation, so the two candidates are not separable on precedent alone.
  - `F3` ✔ Across ALL services in 2026-04-15T00:00Z–15:59Z only two deploys occurred: market-data-service v5.0.3 at 11:35:00Z ('Fix symbol mapping') and auth-service v7.3.0 at 14:00:00Z. No other service was deployed in the window.
  - `F15` ✔ Config changes for 2026-04-15T00:00-13:30Z: count 0. A separate query for 2026-04-14 onward scoped to api-gateway also returned count 0. Combined with the deployment list, only two changes exist in the whole day window and both are auth-service changes; no gateway/ingress or shared-cache change coincides with the 401 spike.
**Contradicting evidence**
  - `F2` ✔ A config change cfg-2705 rotated the auth-service JWT signing key id from 'k-2025-10' to 'k-2026-04' at 2026-04-15T14:01:00Z, authored by 'security-automation', described as 'Scheduled signing-key rotation'. It is one minute after the v7.3.0 deploy and 11 minutes before the report.
  - `F14` ✔ Independent re-query of config changes for 13:30-15:59Z returns exactly one change, cfg-2705: auth-service key 'jwt.signing.key_id' old_value 'k-2025-10' -> new_value 'k-2026-04' at 2026-04-15T14:01:00Z, author 'security-automation', described 'Scheduled signing-key rotation'. It is 1 minute after the deploy and 1 minute before the first 401 log.
  - `F30` ✔ Because no token attribute is logged by any reachable source, the H1-vs-H2 discriminator (does the rejected set include the NEW kid k-2026-04 or only old-kid/unconverged tokens) cannot be decided from caller logs; the gateway sees only the already-collapsed 'invalid_token' outcome. Answer to objective: kid distribution = unknown/not-recorded, but cohort stability = a single stable repeating signature with unique trace ids per request, implying a systematic rejection class rather than a random subset of callers.
**Assumptions**
  - ASSUMED: a breaking parser change rejects a deterministic ~1/3 of otherwise-valid tokens rather than all-or-none; no mechanism evidence exists (F18 is precedent only, F30 says the kid question is unobservable)
  - ASSUMED: the ~36% is spread across the fleet, not concentrated in a subset; no per-pod dimension exists (F12)
  - The rejected cohort is a single stable signature (F27) — consistent with a parse-strictness rule, but equally consistent with an unconverged-fleet share

### H2: Config change: scheduled signing-key rotation (cfg-2705) to kid k-2026-04 without staged/dual-key propagation
cfg-2705 rotated auth-service jwt.signing.key_id k-2025-10 → k-2026-04 at 14:01:00Z, immediately before the first 401 and the metric step. If the new kid was published without a grace/dual-key window (and none was ever configured — cfg-2705 is the only config change all day), tokens or instances still on the old key fail as invalid_token at a steady share, matching HIST-108. The rotation is a real auth-side change contemporaneous with the step, and its failure mode is structurally unverifiable in the retrieved data.

**Causal chain**
1. cfg-2705 rotated jwt.signing.key_id k-2025-10 -> k-2026-04 at 14:01:00Z, authored by 'security-automation', 'Scheduled signing-key rotation'; it is the ONLY config change all day (all-services count 0 before and after). [F2, F14, F20, F19]
2. The rotation precedes the symptom: k-2026-04 publish timestamp 14:01:00Z < 14:02:00Z metric step and < 14:02:22Z first gateway invalid_token log. [F22]
3. Without staged JWKS propagation / a dual-key window, a stable fraction of validations fails with invalid_token (HIST-108 is the exact signature). [F6, F18]
4. auth-service error_rate steps to ~36% at 14:02 and stays flat through 15:55 (single step, not periodic). [F7, F8]
5. The same 401 invalid_token signature is observed at api-gateway from 14:02:22Z onward (sampled WARN stream). [F10, F31, F32]
6. The rate does not decay over 2h; the conventional HIST-108 resolution path (re-publish old key / rollback) never occurs. [F19, F23]

**Supporting evidence**
  - `F2` ✔ A config change cfg-2705 rotated the auth-service JWT signing key id from 'k-2025-10' to 'k-2026-04' at 2026-04-15T14:01:00Z, authored by 'security-automation', described as 'Scheduled signing-key rotation'. It is one minute after the v7.3.0 deploy and 11 minutes before the report.
  - `F14` ✔ Independent re-query of config changes for 13:30-15:59Z returns exactly one change, cfg-2705: auth-service key 'jwt.signing.key_id' old_value 'k-2025-10' -> new_value 'k-2026-04' at 2026-04-15T14:01:00Z, author 'security-automation', described 'Scheduled signing-key rotation'. It is 1 minute after the deploy and 1 minute before the first 401 log.
  - `F20` ✔ The only config change all day is cfg-2705 in auth-service: key 'jwt.signing.key_id' old_value 'k-2025-10' -> new_value 'k-2026-04' at 2026-04-15T14:01:00Z. Scoped to auth-service it is still count 1, and for 2026-04-15T00:00-13:59Z the all-services count is 0 — meaning no earlier grace/dual-key window was configured for k-2025-10 either.
  - `F22` ✔ The k-2026-04 publication timestamp is 14:01:00Z, i.e. the config write precedes the 14:02Z metric step and the 14:02:22Z first gateway invalid_token log; no keys publication record exists BEFORE 14:01Z in any service, so the causal order cannot be inverted by a pre-14:01Z keys publish in the available records.
  - `F6` ✔ Historical precedent HIST-108 (2025-10-01, '401s after JWT signing key rotation', auth-service, symptoms '401 invalid_token','users logged out') is the top match (match_score 7): root cause 'Key rotation published new key id before all pods loaded the JWKS', resolved by re-publishing the previous key and a staged rotation. It is a lead only, not evidence about the current incident.
  - `F18` ✔ Historical precedents for this exact signature are auth-service-side and config/deploy-caused: HIST-108 (2025-10-01, '401s after JWT signing key rotation', root cause 'Key rotation published new key id before all pods loaded the JWKS', resolution 'Re-published previous key; staged rotation') and HIST-109 (2025-07-11, 'Token validation regression after auth library upgrade', root cause 'JWT library upgrade changed clock-skew default'). Both are leads only, and HIST-109 shows the same symptom class can come from the library deploy rather than the rotation, so the two candidates are not separable on precedent alone.
  - `F7` ✔ auth-service error_rate (HTTP 5xx) stepped sharply from a flat baseline of ~0.2% (range 0.17-0.28% over 12:00-13:59) to ~36% beginning at the 14:00-14:02 onset window. With 120s buckets, 13:50-14:00 remain <=0.28% and the 14:02:00 bucket jumps to 37.21% avg (max 38.34%); the level then stays flat at 35-37% through 15:55 (36.27%), i.e. a single step, not periodic.
  - `F8` ✔ The onset is bounded to 14:00:00-14:02:00Z: the 120s bucket starting 14:00:00Z is still at baseline (avg 0.21%, max 0.26%) while the 14:02:00Z bucket is already 37.21%. The earlier 300s bucket at 14:00:00Z showing avg 22.21% is a partial-bucket artifact; the finer resolution shows the true step at ~14:02.
  - `F10` ✔ The api-gateway independently shows the same 401 invalid_token signature against auth-service, beginning 2026-04-15T14:02:22Z (466 WARN lines matching 'auth-service POST /v<n>/token/validate -> <n> invalid_token', last 15:58:51Z). Gateway error_rate stepped at the same time: 0.24% at 13:59 -> 21.06% at 14:02 and holding ~21% (14:05, 14:08). Note gateway error_rate is 5xx-only, so the 21% reflects the fraction of gateway traffic that proxies to auth-service.
  - `F19` ✔ No config change of any kind occurred in any service between 2026-04-15T14:02Z and 15:59Z (count 0, scope 'all services'), so there was no post-onset re-publication of k-2025-10, no rollback of cfg-2705, and no grace-window/dual-key config write after the 401 onset.
**Contradicting evidence**
  - `F1` ✔ auth-service was deployed at 2026-04-15T14:00:00Z (dep-5710, v7.3.0) with a breaking JWT library upgrade 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live, not rolled back. This is 12 minutes before the 14:12Z incident report.
  - `F13` ✔ Independent re-query of deployments for 13:30-15:59Z returns exactly one deploy, dep-5710: auth-service v7.3.0 at 2026-04-15T14:00:00Z, author d.verma, changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten', status 'live'. It lands 2 minutes before the metric step and 12 minutes before the 14:12Z report.
  - `F24` ✔ The config-change API exposes no key-material or JWKS fields (only id/service/key/old_value/new_value/changed_at/author/description) and auth-service spans multiple instances only via an opaque summary ('Upgrade JWT library to 4.x; refactor token validation'), so whether k-2025-10 remained accepted in a dual-key/grace window after 14:01Z is not observable in this data.
  - `F28` ✔ The rejection stream is continuous and steady across the whole window rather than bursty or random: sampling early (14:02:22–14:12:28Z) and late (15:52:19–15:58:51Z) shows the same interleaved cadence of roughly one 401 every 10-30s, with the identical message form at both ends and last_match still '2026-04-15T15:58:51Z' — a persistent systematic class, not a varying fraction that dies out.
**Assumptions**
  - ASSUMED: the rotation had NO grace/dual-key window; the config API exposes no key-material/JWKS fields and no 'jwks'/'identity' service exists, so this is structurally unobservable (F24, F16, F21, F30)
  - ASSUMED: the stable share corresponds to old-kid tokens in steady-state circulation — but the rate does not decay over 2h, which weakens pure old-kid circulation and is not explained by the conventional HIST-108 mechanism
  - Two concurrent auth-side changes exist one minute apart; the rotation cannot be isolated from the deploy as the trigger in available data

### H5: Partial-fleet failure: one of N auth-service replicas serves bad validations (~33%)
A single bad replica (bad v7.3.0 canary pod or one pod that never loaded the rotated key) handles roughly a third of traffic and fails all its validations, matching the observed ~33-36% error share. This predicts a constant traffic share failing — exactly the flat 2h plateau observed — and cannot be excluded because no per-pod dimension exists in auth-service telemetry.

**Causal chain**
1. v7.3.0 rolled out at 14:00Z; if one of ~3 replicas is bad, ~33% of traffic fails all its validations. [F1, F13]
2. No per-pod/per-replica dimension exists for auth-service metrics, so the traffic split cannot be measured. [F12]
3. A fixed 36% step at 14:02 flatlining over 2h matches a constant traffic share routed to one bad replica; request_rate is unchanged so the share is genuine. [F7, F8, F9]
4. No decay over 2h is consistent with a constant bad-replica traffic share (this hypothesis survives the no-decay test that weakens H2). [F28, F23]

**Supporting evidence**
  - `F1` ✔ auth-service was deployed at 2026-04-15T14:00:00Z (dep-5710, v7.3.0) with a breaking JWT library upgrade 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live, not rolled back. This is 12 minutes before the 14:12Z incident report.
  - `F13` ✔ Independent re-query of deployments for 13:30-15:59Z returns exactly one deploy, dep-5710: auth-service v7.3.0 at 2026-04-15T14:00:00Z, author d.verma, changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten', status 'live'. It lands 2 minutes before the metric step and 12 minutes before the 14:12Z report.
  - `F7` ✔ auth-service error_rate (HTTP 5xx) stepped sharply from a flat baseline of ~0.2% (range 0.17-0.28% over 12:00-13:59) to ~36% beginning at the 14:00-14:02 onset window. With 120s buckets, 13:50-14:00 remain <=0.28% and the 14:02:00 bucket jumps to 37.21% avg (max 38.34%); the level then stays flat at 35-37% through 15:55 (36.27%), i.e. a single step, not periodic.
  - `F8` ✔ The onset is bounded to 14:00:00-14:02:00Z: the 120s bucket starting 14:00:00Z is still at baseline (avg 0.21%, max 0.26%) while the 14:02:00Z bucket is already 37.21%. The earlier 300s bucket at 14:00:00Z showing avg 22.21% is a partial-bucket artifact; the finer resolution shows the true step at ~14:02.
  - `F9` ✔ auth-service request_rate is unchanged across the onset: it is ~386-413 req/s both before (e.g. 398.11 at 14:05, 401.35 at 14:00) and after (405.81 at 15:55), so the 36% error share is not driven by a traffic surge or drop.
  - `F28` ✔ The rejection stream is continuous and steady across the whole window rather than bursty or random: sampling early (14:02:22–14:12:28Z) and late (15:52:19–15:58:51Z) shows the same interleaved cadence of roughly one 401 every 10-30s, with the identical message form at both ends and last_match still '2026-04-15T15:58:51Z' — a persistent systematic class, not a varying fraction that dies out.
**Contradicting evidence**
  - `F12` ✔ Repeated per-instance/replica breakdown of auth-service (to test whether the 36% is concentrated in a subset of pods) was not obtainable: metric names available for auth-service are only cpu_pct/error_rate/latency_p99_ms/memory_pct/rate_limited_per_min/request_rate/restarts, and no per-pod dimension was returned in any query.
**Assumptions**
  - ASSUMED: rollout was partial/canary rather than all-at-once (only one deploy record, no rollout detail)
  - ASSUMED: one replica handles almost exactly ~1/3 of traffic and the other pods are healthy
  - ASSUMED: the flat 36% is an instance-share effect rather than a global acceptance rule

### H4: Upstream caller/gateway mints or forwards malformed/expired tokens; auth-service correctly rejects
A caller (api-gateway or another minting tier) produces or forwards tokens that are malformed or expired, and auth-service's 401 invalid_token is correct behavior — fault origin upstream of auth-service. Its decisive axis (a token-MINTING caller also emitting 401s) was not closed: the enumeration was budget-truncated, and no minting tier is even enumerated in the service inventory.

**Causal chain**
1. api-gateway had 0 deploys and 0 config changes in the window; no other service changed either (only market-data and auth-service changed all day). [F5, F15, F3, F13]
2. api-gateway independently shows the SAME 401 invalid_token signature against auth-service from 14:02:22Z; gateway request_rate is flat so it forwards the same volume. [F10, F31, F32, F33]
3. No recorded caller-side deploy or config change coincides with onset; the only recorded changes are auth-service-side. [F15, F3]
4. A caller changed token minting without any recorded deploy or config change. *(assumed)*

**Supporting evidence**
  - `F10` ✔ The api-gateway independently shows the same 401 invalid_token signature against auth-service, beginning 2026-04-15T14:02:22Z (466 WARN lines matching 'auth-service POST /v<n>/token/validate -> <n> invalid_token', last 15:58:51Z). Gateway error_rate stepped at the same time: 0.24% at 13:59 -> 21.06% at 14:02 and holding ~21% (14:05, 14:08). Note gateway error_rate is 5xx-only, so the 21% reflects the fraction of gateway traffic that proxies to auth-service.
**Contradicting evidence**
  - `F5` ✔ No deploys or config changes were found for api-gateway (deploys count 0 for 2026-04-14→14-15; config changes count 0) nor config changes for redis-cache (count 0) across 2026-04-14 → 2026-04-15T15:59Z, i.e. the 401 spike is not accompanied by a scoring gateway/ingress or shared-cache config change in the data available.
  - `F15` ✔ Config changes for 2026-04-15T00:00-13:30Z: count 0. A separate query for 2026-04-14 onward scoped to api-gateway also returned count 0. Combined with the deployment list, only two changes exist in the whole day window and both are auth-service changes; no gateway/ingress or shared-cache change coincides with the 401 spike.
  - `F3` ✔ Across ALL services in 2026-04-15T00:00Z–15:59Z only two deploys occurred: market-data-service v5.0.3 at 11:35:00Z ('Fix symbol mapping') and auth-service v7.3.0 at 14:00:00Z. No other service was deployed in the window.
  - `F1` ✔ auth-service was deployed at 2026-04-15T14:00:00Z (dep-5710, v7.3.0) with a breaking JWT library upgrade 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live, not rolled back. This is 12 minutes before the 14:12Z incident report.
  - `F13` ✔ Independent re-query of deployments for 13:30-15:59Z returns exactly one deploy, dep-5710: auth-service v7.3.0 at 2026-04-15T14:00:00Z, author d.verma, changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten', status 'live'. It lands 2 minutes before the metric step and 12 minutes before the 14:12Z report.
  - `F2` ✔ A config change cfg-2705 rotated the auth-service JWT signing key id from 'k-2025-10' to 'k-2026-04' at 2026-04-15T14:01:00Z, authored by 'security-automation', described as 'Scheduled signing-key rotation'. It is one minute after the v7.3.0 deploy and 11 minutes before the report.
  - `F14` ✔ Independent re-query of config changes for 13:30-15:59Z returns exactly one change, cfg-2705: auth-service key 'jwt.signing.key_id' old_value 'k-2025-10' -> new_value 'k-2026-04' at 2026-04-15T14:01:00Z, author 'security-automation', described 'Scheduled signing-key rotation'. It is 1 minute after the deploy and 1 minute before the first 401 log.
  - `F36` ✔ The full service inventory returned by the telemetry tool lists no service named 'user-service' and no minting tier between api-gateway and auth-service; known services are api-gateway, auth-service, order-service, portfolio-service, pricing-engine, market-data-service, payment-service, settlement-service, notification-service, exchange-gateway, ledger-db, user-db, redis-cache, kafka, exchange-api, bank-partner-api, kyc-provider, email-provider.
**Assumptions**
  - ASSUMED: a caller changed token minting without a recorded deploy/config change
  - ASSUMED: an unenumerated/undocumented minting caller exists — none appears in the dependency graph or service inventory (F36, F37, F38, F39, F42), and the minting-caller sweep was budget-truncated, so this is un-closed rather than refuted

### H3: Shared dependency failure: redis-cache (shared token/session store) partially degraded
redis-cache is the only shared component in auth-service's immediate topology; partial degradation could make a fraction of token/session lookups miss, surfacing as invalid_token on ~1/3 of validations.

**Causal chain**
1. auth-service -> redis-cache (cache), flagged shared:true, owner platform; it is the only shared component in auth-service's immediate set. [F4, F17]
2. redis-cache is consumed by portfolio-service, pricing-engine and market-data-service too, so degradation should co-hit them — not observed (order-service flat). [F17, F11]
3. No config change for redis-cache in 2026-04-14 -> 15:59Z (count 0), so no redis-side change coincides with the step. [F5]
4. Partial cache degradation gets reported as invalid_token on 36% of validations. *(assumed)*

**Supporting evidence**
  - `F4` ✔ auth-service's dependency map: edge 'auth-service -> redis-cache (cache)' where redis-cache is flagged "shared":true (owner platform); edges to user-db (postgres) and kyc-provider (external, has_telemetry:false); and 'api-gateway -> auth-service (http)'. So redis-cache is the only shared component in auth-service's immediate dependency set.
  - `F17` ✔ redis-cache is flagged "shared":true (owner platform) and is consumed by auth-service, portfolio-service, pricing-engine and market-data-service; it is the only shared component in auth-service's immediate topology, so a redis-cache fault should also hit those three services, which would not produce an auth-service-only 401 signature.
**Contradicting evidence**
  - `F17` ✔ redis-cache is flagged "shared":true (owner platform) and is consumed by auth-service, portfolio-service, pricing-engine and market-data-service; it is the only shared component in auth-service's immediate topology, so a redis-cache fault should also hit those three services, which would not produce an auth-service-only 401 signature.
  - `F11` ✔ order-service is NOT co-affected at this signature: its error_rate stays flat at ~0.17-0.22% across 13:00-15:45 (14:00 bucket avg 0.22%, 15:45 avg 0.20%) and its only WARN logs in the window are unrelated (a 38ms clock-drift NTP resync at 14:38 and a 260ms GC pause at 15:42).
  - `F5` ✔ No deploys or config changes were found for api-gateway (deploys count 0 for 2026-04-14→14-15; config changes count 0) nor config changes for redis-cache (count 0) across 2026-04-14 → 2026-04-15T15:59Z, i.e. the 401 spike is not accompanied by a scoring gateway/ingress or shared-cache config change in the data available.
  - `F7` ✔ auth-service error_rate (HTTP 5xx) stepped sharply from a flat baseline of ~0.2% (range 0.17-0.28% over 12:00-13:59) to ~36% beginning at the 14:00-14:02 onset window. With 120s buckets, 13:50-14:00 remain <=0.28% and the 14:02:00 bucket jumps to 37.21% avg (max 38.34%); the level then stays flat at 35-37% through 15:55 (36.27%), i.e. a single step, not periodic.
  - `F8` ✔ The onset is bounded to 14:00:00-14:02:00Z: the 120s bucket starting 14:00:00Z is still at baseline (avg 0.21%, max 0.26%) while the 14:02:00Z bucket is already 37.21%. The earlier 300s bucket at 14:00:00Z showing avg 22.21% is a partial-bucket artifact; the finer resolution shows the true step at ~14:02.
**Assumptions**
  - ASSUMED: redis failures map to invalid_token rather than 5xx/timeout errors
  - ASSUMED: failures would partition to ~1/3 of requests without touching other consumers of the same shared cache

### H6: Clock skew / expired signing key on a subset of instances causes invalid_token on valid tokens
Some auth-service instances have clock skew or a stale/expired key, and their nbf/exp checks reject otherwise-valid tokens — producing invalid_token on a subset of traffic. Requires skew to begin exactly at the 14:02 step, coincident with the deploy and rotation.

**Causal chain**
1. Clock skew or expired key on a subset of instances forces expiry-check failures on valid tokens. *(assumed)*
2. order-service (a nearby service) shows only a benign 38ms NTP resync at 14:38 and a 260ms GC pause at 15:42 — no NTP event at 14:02. [F11]
3. No restart/health series or per-pod data exists to confirm or refute instance-level skew. [F12]
4. A skew event would need to begin exactly at 14:02 to match the step, coincident with the deploy and rotation. [F7, F8]

**Supporting evidence**
  - `F18` ✔ Historical precedents for this exact signature are auth-service-side and config/deploy-caused: HIST-108 (2025-10-01, '401s after JWT signing key rotation', root cause 'Key rotation published new key id before all pods loaded the JWKS', resolution 'Re-published previous key; staged rotation') and HIST-109 (2025-07-11, 'Token validation regression after auth library upgrade', root cause 'JWT library upgrade changed clock-skew default'). Both are leads only, and HIST-109 shows the same symptom class can come from the library deploy rather than the rotation, so the two candidates are not separable on precedent alone.
**Contradicting evidence**
  - `F7` ✔ auth-service error_rate (HTTP 5xx) stepped sharply from a flat baseline of ~0.2% (range 0.17-0.28% over 12:00-13:59) to ~36% beginning at the 14:00-14:02 onset window. With 120s buckets, 13:50-14:00 remain <=0.28% and the 14:02:00 bucket jumps to 37.21% avg (max 38.34%); the level then stays flat at 35-37% through 15:55 (36.27%), i.e. a single step, not periodic.
  - `F8` ✔ The onset is bounded to 14:00:00-14:02:00Z: the 120s bucket starting 14:00:00Z is still at baseline (avg 0.21%, max 0.26%) while the 14:02:00Z bucket is already 37.21%. The earlier 300s bucket at 14:00:00Z showing avg 22.21% is a partial-bucket artifact; the finer resolution shows the true step at ~14:02.
  - `F11` ✔ order-service is NOT co-affected at this signature: its error_rate stays flat at ~0.17-0.22% across 13:00-15:45 (14:00 bucket avg 0.22%, 15:45 avg 0.20%) and its only WARN logs in the window are unrelated (a 38ms clock-drift NTP resync at 14:38 and a 260ms GC pause at 15:42).
  - `F1` ✔ auth-service was deployed at 2026-04-15T14:00:00Z (dep-5710, v7.3.0) with a breaking JWT library upgrade 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live, not rolled back. This is 12 minutes before the 14:12Z incident report.
  - `F13` ✔ Independent re-query of deployments for 13:30-15:59Z returns exactly one deploy, dep-5710: auth-service v7.3.0 at 2026-04-15T14:00:00Z, author d.verma, changes 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten', status 'live'. It lands 2 minutes before the metric step and 12 minutes before the 14:12Z report.
  - `F12` ✔ Repeated per-instance/replica breakdown of auth-service (to test whether the 36% is concentrated in a subset of pods) was not obtainable: metric names available for auth-service are only cpu_pct/error_rate/latency_p99_ms/memory_pct/rate_limited_per_min/request_rate/restarts, and no per-pod dimension was returned in any query.
**Assumptions**
  - ASSUMED: skew began exactly at the 14:02 step, coincident with the deploy and key rotation

### H7: Telemetry artifact: 401 spike partly an instrument of the identity-namespace log-pipeline outage
The log-pipeline outage in the identity namespace distorts or synchronizes error reporting, making the failure rate look like a clean 36% step when reality differs.

**Causal chain**
1. identity-namespace log pipeline down since the morning; auth-service logs unsearchable. [F21]
2. Metric error_rate is independent of the log pipeline and shows a real 0.2% -> 36% step; request_rate unchanged so the share is genuine, not a denominator artifact. [F7, F8, F9]
3. api-gateway (a separate telemetry source) independently shows the same 401 signature from 14:02:22Z, so the spike is not a single-source artifact. [F10, F31, F33]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ auth-service error_rate (HTTP 5xx) stepped sharply from a flat baseline of ~0.2% (range 0.17-0.28% over 12:00-13:59) to ~36% beginning at the 14:00-14:02 onset window. With 120s buckets, 13:50-14:00 remain <=0.28% and the 14:02:00 bucket jumps to 37.21% avg (max 38.34%); the level then stays flat at 35-37% through 15:55 (36.27%), i.e. a single step, not periodic.
  - `F8` ✔ The onset is bounded to 14:00:00-14:02:00Z: the 120s bucket starting 14:00:00Z is still at baseline (avg 0.21%, max 0.26%) while the 14:02:00Z bucket is already 37.21%. The earlier 300s bucket at 14:00:00Z showing avg 22.21% is a partial-bucket artifact; the finer resolution shows the true step at ~14:02.
  - `F9` ✔ auth-service request_rate is unchanged across the onset: it is ~386-413 req/s both before (e.g. 398.11 at 14:05, 401.35 at 14:00) and after (405.81 at 15:55), so the 36% error share is not driven by a traffic surge or drop.
  - `F10` ✔ The api-gateway independently shows the same 401 invalid_token signature against auth-service, beginning 2026-04-15T14:02:22Z (466 WARN lines matching 'auth-service POST /v<n>/token/validate -> <n> invalid_token', last 15:58:51Z). Gateway error_rate stepped at the same time: 0.24% at 13:59 -> 21.06% at 14:02 and holding ~21% (14:05, 14:08). Note gateway error_rate is 5xx-only, so the 21% reflects the fraction of gateway traffic that proxies to auth-service.
  - `F31` ✔ api-gateway is a confirmed HTTP caller of auth-service and shows the invalid_token signature against auth-service only, beginning 2026-04-15T14:02:22Z: 466 WARN lines all matching 'auth-service POST /v<n>/token/validate -> <n> invalid_token', first_match 14:02:22Z, last_match 15:58:51Z, and it is the ONLY top pattern in the gateway's WARN set (no other downstream service appears).
  - `F33` ✔ Gateway request_rate is flat across the onset (1264.9 req/s at 13:00, 1263.56 at 14:00, 1287.22 at 14:15, 1268.06 at 15:45), so the gateway 401 lines are not driven by a traffic surge and the gateway is still forwarding the same volume to auth-service.
**Assumptions**
  - ASSUMED: a log-pipeline outage could alter a metrics-series step; no mechanism for this is supported

## Facts (verified observations)
- auth-service error_rate (HTTP 5xx) stepped sharply from a flat ~0.2% baseline (12:00-13:59) to ~36%, with the onset bounded to the 14:00:00-14:02:00Z window (14:00 bucket at baseline avg 0.21%/max 0.26%; 14:02:00 bucket at 37.21%) and holding flat at 35-37% through 15:55 (36.27%); a single step, not periodic. [F7, F8]
- auth-service request_rate is unchanged across the onset (~386-413 req/s both before and after), so the 36% error share is not driven by a traffic surge or drop. [F9]
- api-gateway independently observes the same 401 invalid_token signature against auth-service beginning 2026-04-15T14:02:22Z: 466 WARN lines matching 'auth-service POST /v<n>/token/validate -> <n> invalid_token', last 15:58:51Z; gateway error_rate stepped 0.24% at 13:59 -> 21.06% at 14:02, holding ~21%. [F10, F31, F32]
- The rejection stream is a single stable signature, not a mixed/random bag: 100% of the 466 matched WARN lines collapse to one pattern, all at WARN level, and sampling early (14:02:22-14:12:28Z) and late (15:52:19-15:58:51Z) shows the same cadence with identical message form, i.e. a persistent systematic class that does not die out. [F27, F28]
- api-gateway's 466 rejected-token lines contain no token attributes whatsoever (no kid, iss, sub, jti, aud or expiry); the only per-request identifier is a trace id, and a targeted 'kid' search over 14:00-15:59Z returned 0 matches. [F25, F26, F43]
- Joining the sampled trace id '09dac90d' to the api-gateway store returns exactly one line (the WARN itself) with no companion span/request record, so the join to per-request telemetry returns only the original reject line. [F44]
- order-service is NOT co-affected: its error_rate stays flat at ~0.17-0.22% across 13:00-15:45 and a targeted search for 'invalid_token' on order-service over 13:00-15:59Z returned total_matched 0. [F11, F34]
- api-gateway is the ONLY non-auth-service source of this 401 signature: order-service, portfolio-service and pricing-engine log searches for 'token'/'invalid_token' all returned total_matched 0. [F29]
- Gateway request_rate is flat across the onset (~1263-1287 req/s), so the gateway 401 lines are not traffic-driven and the gateway forwards the same volume to auth-service. [F33]
- redis-cache is the only shared component in auth-service's immediate dependency topology (flagged shared:true, consumed by auth-service, portfolio-service, pricing-engine, market-data-service), so a redis fault should co-hit those three services and would not produce an auth-service-only 401 signature. [F4, F17]
- No deploys or config changes were found for api-gateway or redis-cache across 2026-04-14 to 2026-04-15T15:59Z, and no service in the whole day window has a config change coinciding with the 401 spike other than the auth-service pair. [F5, F15]
- Only two deploys occurred across ALL services in 2026-04-15T00:00-15:59Z: market-data-service v5.0.3 at 11:35:00Z and auth-service v7.3.0 at 14:00:00Z; the auth-service deploy (dep-5710) carried 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten', status live, and lands 2 minutes before the metric step. [F3, F1, F13]
- Exactly one config change exists for 13:30-15:59Z: cfg-2705, auth-service key 'jwt.signing.key_id' old 'k-2025-10' -> new 'k-2026-04' at 2026-04-15T14:01:00Z, author 'security-automation', described 'Scheduled signing-key rotation'; it is 1 minute after the deploy and 1 minute before the first 401 log. [F14, F2]
- The k-2026-04 config write at 14:01:00Z precedes the 14:02Z metric step and the 14:02:22Z first gateway invalid_token log, and no keys publication record exists before 14:01Z in any service. [F22]
- No config change of any kind occurred in any service between 14:02Z and 15:59Z (count 0), and dep-5710 remains status 'live' with rolled_back_at null; there was no post-onset re-publication of k-2025-10, no rollback of cfg-2705, and no grace-window/dual-key config write after onset. [F19, F23]
- The only config change all day is cfg-2705; for 2026-04-15T00:00-13:59Z the all-services config-change count is 0, meaning no earlier grace/dual-key window was configured for k-2025-10 either. [F20]
- No separate keys/JWKS publication record exists for any service: get_config_changes(service='jwks') fails not_found and the known_services list contains no 'identity', 'keys' or 'jwks-service'; auth-service is the only identity-side service. [F21]
- auth-service remains un-joinable: its own log store is unreachable (backend 503 'gave up after 3 attempts') and its metric set exposes only cpu_pct/error_rate/latency_p99_ms/memory_pct/rate_limited_per_min/request_rate/restarts with no per-pod dimension; no per-instance/replica breakdown was obtainable. [F47, F12]
- The trace/span store query never executed (budget_exhausted), and targeted api-gateway field probes for 'pod', 'iss', 'exp', 'k-2026-04', 'k-2025-10' were rejected unexecuted, so 'attribute absent' cannot be distinguished from 'store unreachable' and the kid distribution and per-pod termination remain undetermined. [F45, F46]
- Historical precedents for this exact signature are auth-service-side and config/deploy-caused: HIST-108 (signing-key rotation publishing new kid before all pods loaded JWKS) and HIST-109 (JWT library upgrade changing clock-skew default); both are leads only, and HIST-109 shows the symptom class can come from either the library deploy or the rotation, so the two candidates are not separable on precedent alone. [F18]
- No minting tier exists between api-gateway and auth-service: the only inbound edge to auth-service is {'from':'api-gateway','to':'auth-service','kind':'http'}, the service inventory lists no minting tier and no 'user-service' or 'bff-web', and candidate minting-tier services show no change records. [F37, F36, F38, F39, F40, F41]
- auth-service CPU is flat through the incident (~37.95% at 13:00 to ~38.14% at 15:48, missing_points 0), so the ~36% 401 rate is not accompanied by a resource-saturation signature at the validating service. [F35]

## Assumptions (not verified)
- ASSUMED: a breaking parser change (jjwt 0.12 acceptance semantics — clock-skew default, algorithm/kid strictness, exp/nbf handling) rejects a deterministic ~1/3 of otherwise-valid tokens rather than all-or-none; no mechanism evidence exists in the retrieved data and HIST-109 is precedent only.
- ASSUMED: the ~36% failure is spread across the fleet rather than concentrated in a subset; no per-pod dimension exists on auth-service metrics, so per-instance partitioning is impossible.
- ASSUMED: the rejection cohort is a single stable signature, which is consistent with a parse-strictness rule but equally consistent with an unconverged-fleet share.
- ASSUMED: the cfg-2705 key rotation had NO grace/dual-key window; the config API exposes no key-material/JWKS fields and no keys/JWKS service exists, so this is structurally unobservable.
- ASSUMED: the stable flat 36% corresponds to old-kid tokens in steady-state circulation — but the rate does not decay over 2h, which weakens pure old-kid circulation and is not explained by the conventional HIST-108 mechanism.
- ASSUMED: two concurrent auth-side changes one minute apart cannot be isolated from one another as the trigger in the available data; the ranking is decided only by deploy-first ordering, not by mechanism.
- ASSUMED: the api-gateway 466-line WARN stream is a sampled/aggregated record, not a literal count of all rejects (a 36% rate against ~400 req/s for 2h would be on the order of a million rejects), so cohort claims drawn from it are sampled.
- ASSUMED: the identity-namespace log-pipeline outage is independent of the 401 step; no change record links it to the incident, and a log-pipeline outage has no request-path effect that could produce a 36% 401 step.
- ASSUMED: the discovered topology (single inbound edge api-gateway -> auth-service, no minting tier in the service inventory) is complete, so no undocumented/uninstrumented minting caller exists that could inject malformed tokens.
- (demoted from facts: no verified observation backs it) The config-change API exposes no key-material or JWKS fields, so whether k-2025-10 remained accepted in a dual-key/grace window after 14:01Z is not observable in this data.
- (demoted from facts: no verified observation backs it) Because no token attribute is logged by any reachable source, the H1-vs-H2 discriminator (does the rejected set include the NEW kid k-2026-04 or only old-kid/unconverged tokens) cannot be decided from caller logs; the gateway sees only the already-collapsed 'invalid_token' outcome.

## Timeline
- `2026-04-15T00:00:00Z` Start of the incident day window; no config changes occur anywhere until 14:01Z (all-services config count 0 for 00:00-13:59Z). [F20, F15]
- `2026-04-15T11:35:00Z` market-data-service v5.0.3 deployed ('Fix symbol mapping') — the only other deploy in the day, unrelated to the auth signature. [F3]
- `2026-04-15T14:00:00Z` dep-5710: auth-service v7.3.0 deployed (author d.verma) with 'jjwt 0.11 -> 0.12 (breaking parser API)' and 'TokenValidator rewritten'; status live. [F1, F13]
- `2026-04-15T14:01:00Z` cfg-2705: auth-service jwt.signing.key_id rotated k-2025-10 -> k-2026-04 by 'security-automation' ('Scheduled signing-key rotation'); the only config change all day. [F2, F14, F20]
- `2026-04-15T14:02:00Z` auth-service error_rate steps from baseline (~0.21%) to 37.21% in the 14:02:00Z bucket; a single step, not a gradual or periodic rise. [F7, F8]
- `2026-04-15T14:02:22Z` First api-gateway WARN line 'auth-service POST /v1/token/validate -> 401 invalid_token (trace <hex>)' — the first observable 401. [F10, F31, F27]
- `2026-04-15T14:03:00Z` auth-service error_rate continues at 37.51%; api-gateway error_rate holds ~21%. [F47, F10]
- `2026-04-15T14:12:00Z` Incident INC-007 reported (SEV-1): ~a third of token validations fail with 401; auth-service logs not searchable due to identity-namespace log-pipeline outage. [F10]
- `2026-04-15T15:36:00Z` auth-service CPU remains flat (~38.10%), confirming no resource saturation accompanies the 401 plateau. [F35]
- `2026-04-15T15:55:00Z` auth-service error_rate still 36.27%, flat plateau unchanged ~2h after onset. [F7]
- `2026-04-15T15:58:51Z` Last matched api-gateway invalid_token WARN line (total 466 lines); stream shows no decay or burst structure. [F28, F31]
- `2026-04-15T15:59:00Z` Latest data: no rollback of dep-5710 and no config change after 14:02Z; no keys/JWKS publication record; per-pod dimension and token attributes remain unavailable. [F19, F23, F21, F12, F30]

## Recommended actions
- **[immediate]** Immediately roll back cfg-2705 by re-publishing/restoring jwt.signing.key_id to its prior value 'k-2025-10' (and, if the platform supports it, re-publishing the previous key material) — this is the safest first mitigation because no grace/dual-key window was ever configured (cfg-2705 is the only config change all day) and no post-onset re-publication occurred. -- cfg-2705 (kid k-2025-10 -> k-2026-04) is one of only two auth-service changes and precedes the first 401 by one minute; HIST-108's documented failure mode is exactly 'key rotation published new key id before all pods loaded the JWKS', resolved by re-publishing the previous key. This mitigation is low-risk and directly tests H2. [F2, F14, F20, F19, F22, F6, F18]
- **[immediate]** If the 401 rate does not fall after the key re-publication, roll back dep-5710 (revert auth-service to the pre-v7.3.0 jjwt 0.11 / prior TokenValidator) — the equivalent low-risk mitigation for the other auth-side change. -- dep-5710 (jjwt 0.11->0.12 breaking parser API + rewritten TokenValidator) is the only other auth-side change preceding the step; HIST-109 documents the same symptom class from a library upgrade that changed the clock-skew default. Sequencing the two rollbacks discriminates H1 from H2 operationally. [F1, F13, F18, F23]
- **[short_term]** Instrument the validator to emit a per-request structured rejection log carrying kid, iss, exp, sub and auth-service instance/pod id (including the reason string for invalid_token), so future incidents have the decisive attribute; the identity-namespace log pipeline must be restored first since auth-service logs are currently unreachable. -- The H1-vs-H2 discriminator (whether the rejected set includes the new kid k-2026-04 or only old-kid/unconverged tokens) is structurally unavailable: gateway lines carry only a trace id, no kid/iss/exp, and auth-service's own store returned backend 503. Without this, the same tie will recur. [F25, F26, F43, F44, F47, F30]
- **[short_term]** Verify the mitigation by re-checking auth-service error_rate and the api-gateway invalid_token WARN stream after each rollback step, confirming the step returns to the ~0.2% baseline and the 14:02Z plateau disappears; also confirm request_rate stays unchanged to rule out a traffic confound. -- The current symptom is a single sharp step to a flat ~36% with unchanged traffic, so a successful rollback should produce a symmetric drop to baseline; reversing the step is the cleanest confirmation of which change was causal. [F7, F8, F9, F10, F27, F28]
- **[long_term]** Configure a staged/dual-key rotation procedure with an explicit grace window (accept old and new kid simultaneously, with JWKS propagation verified across all replicas before retiring the old key) and make the grace-window state an observable config field. -- No grace/dual-key window was ever configured (all-services config count 0 before 14:01Z) and HIST-108's resolution was a staged rotation; making the window explicit and observable removes the structural unobservability that blocks H2's confirmation today. [F20, F24, F6, F18]
- **[long_term]** Add per-pod/per-replica dimensions (error_rate, restarts) to auth-service telemetry and enforce canary/gradual rollout with per-instance validation metrics so partial-fleet rollouts (H5) can be confirmed or excluded rather than inferred. -- No per-pod dimension exists on auth-service metrics, so the partial-fleet hypothesis (~one of N replicas serving bad validations at ~33%) can be neither confirmed nor excluded, and the ~36% share cannot be partitioned by instance. [F12, F47]
- **[long_term]** Add a canary gate for jjwt/library upgrades that validates a corpus of real production tokens against the new parser before full rollout, and gate deploys that change token acceptance semantics on a token-attribute canary. -- dep-5710 shipped a breaking parser API change and a rewritten TokenValidator with no acceptance-semantics gate; HIST-109 shows the same failure class recurred from a library upgrade, so a token-corpus canary prevents recurrence. [F1, F13, F18]

## Data gaps
- auth-service logs are not searchable (identity-namespace log-pipeline down since the morning, and the store returned backend 503 'gave up after 3 attempts'), so the server-side 401 reason string and the per-pod first-error time cannot be read from the rejecting component itself.
- No source reachable in this investigation logs a token 'kid', issuer, subject or expiry: the api-gateway WARN format truncates at 'invalid_token (trace <id>)' and a 'kid' search over 14:00-15:59Z returned 0 matches, so the decisive H1-vs-H2 discriminator (all-new / all-old / mixed kid in the rejected population) remains unresolved.
- The trace/span store query never executed (tool-call budget exhausted), so no result exists to distinguish 'attribute absent' from 'store unreachable'; targeted api-gateway field probes for 'pod', 'iss', 'exp', 'k-2026-04', 'k-2025-10' were rejected unexecuted, leaving kid distribution and per-pod termination undetermined.
- No per-pod/per-replica dimension exists on auth-service metrics (only cpu_pct/error_rate/latency_p99_ms/memory_pct/rate_limited_per_min/request_rate/restarts), so the partial-fleet hypothesis (H5) can be neither confirmed nor excluded.
- No keys/JWKS publication record is retrievable for any service (get_config_changes(service='jwks') fails not_found; no identity/keys service in known_services), and the config API exposes no key material or grace-window field, so whether a separate publisher existed and whether k-2025-10 remained accepted after 14:01Z cannot be shown directly.
- The minting-caller sweep (H4's decisive axis) was cut off by budget exhaustion: payment-service, settlement-service, market-data-service, pricing-engine, bff-web log/error_rate queries were not executed, so H4 is closed by topology (no minting tier, no non-gateway inbound edge) rather than by its own decisive test.
- Only api-gateway and order-service logs/metrics were inspected; whether the invalid_token/4xx signature appears in any other caller that could mint tokens is not established, and 'bff-web'/'user-service' do not exist in the telemetry inventory.
- The identity-namespace log-pipeline outage itself could not be correlated with the incident: no change/deploy/feature-flag record for it was found in the 2026-04-15 window, so whether it is coincidental or part of the same identity-side change train is unresolved.

## What would change this conclusion
- A per-request source showing the rejected population's kid distribution: if it included the NEW kid k-2026-04, that would support H1 (new parser mis-parsing new-kid tokens); if it contained only old-kid/unconverged tokens, that would support H2 (rotation without staged propagation).
- Re-publication or rollback of cfg-2705 (restoring k-2025-10) followed by a prompt return of error_rate to the ~0.2% baseline would confirm H2 as the root cause.
- Rollback of dep-5710 (revert to jjwt 0.11 / prior TokenValidator) followed by a prompt return to baseline would confirm H1 as the root cause; if the rate persists after both rollbacks, the causal locus moves off these two changes.
- A per-pod/per-replica breakdown showing the ~36% concentrated in a small subset of replicas (rather than spread fleet-wide) would elevate H5 (partial-fleet failure) over both H1 and H2.
- Evidence that a token-minting caller or gateway-side change injected malformed/expired tokens before 14:02Z would revive H4 and move fault origin upstream of auth-service.
- Evidence linking the onset to redis-cache (e.g. co-degradation of portfolio-service, pricing-engine or market-data-service, or redis-cache config changes) would revive H3, which current data contradicts.
- Evidence that the 401 step began before 14:00Z (e.g. the 14:00 partial-bucket artifact is real rather than a bucket artifact) would weaken the deploy/rotation timing premise, though no source currently shows this.

## Integrity warnings (set by code)
- fact demoted to assumption: The config-change API exposes no key-material or JWKS fields, so whether k-2025-
- fact demoted to assumption: Because no token attribute is logged by any reachable source, the H1-vs-H2 discr

## Investigation stats
- latency 265.35s, LLM calls 39, tokens 380497 (in 328559 / out 51938)
- tool calls 64 (8 failed, 0 partial, 0 recovered by retry), findings 46/47 verified
