# Incident investigation report: INC-008
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
Login latency and 503s beginning in the 06:50Z bucket are best explained by capacity saturation of auth-service (H1, confidence 0.58, medium): CPU jumped from ~37-38% to ~97% while memory stayed flat, and the 503s are app-generated slowness from accept-queue overflow and a saturated 64/64 worker pool, with the autoscaler explicitly blocked at maxReplicas=8 while desiring 14 (F8, F9, F10, F11, F12). The reporter's bcrypt-regression theory (H2) is not supported by the change record: no auth-service release or rollback exists in the window, and the sole auth-service-touching change is a verbosity-only log.level edit ~3h10m before onset (F1, F3, F18, F19). A shared-dependency fault (H3) remains possible but untested — redis-cache and the untraced kyc-provider could not be measured — which is the main reason confidence is capped at medium rather than high. The verdict is root_cause_identified for "traffic exceeded provisioned auth-service capacity", but the underlying demand driver (a genuine workload increase vs. a silently costlier login code path delivered outside the change feed) is not fully pinned down.

## Probable root cause
**Capacity saturation: login traffic outran provisioned auth-service capacity (HPA capped at maxReplicas=8)** -- capacity_saturation in `auth-service`, triggered by `null` (confidence 0.58, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Capacity saturation: login traffic outran provisioned auth-service capacity (HPA capped at maxReplicas=8) | capacity_saturation | auth-service | null | 0.58 (medium) | active |
| 2 | H2: Recurrence of HIST-101 bcrypt regression via an unobserved code/flag change on auth-service | bad_deployment | auth-service | null | 0.20 (low) | active |
| 3 | H3: Shared neighbor degradation (redis-cache / kyc-provider) driving auth-service login faults | dependency_failure | redis-cache | null | 0.15 (low) | active |
| 4 | H4: auth-service log.level INFO->WARN config change as trigger | config_change | auth-service | cfg-2801 | 0.03 (low) | rejected |

### H1: Capacity saturation: login traffic outran provisioned auth-service capacity (HPA capped at maxReplicas=8)
Auth-service CPU climbed from ~38% to ~97% at 06:50 with flat memory and app-generated 503s from accept-queue overflow and worker-pool saturation; the autoscaler wanted 14 replicas but maxReplicas=8 blocked it. A workload/traffic increase pushed the login path past provisioned capacity, making login CPU-bound (4s p99) and shedding requests. No faulty release or config change is required.

**Causal chain**
1. Login request rate/CPU demand rises around 06:50 (workload increase or login-heavy burst) *(assumed)*
2. auth-service CPU saturates: 37-38% baseline -> 84-97% at the 06:50 bucket [F11, F12]
3. Worker pool saturates (active=64/64, queue 39k-98k) and accept queue overflows, producing app-generated 503s on /v1/auth/login [F10]
4. Autoscaler cannot relieve it: desired 14 replicas exceeds maxReplicas 8 [F10]
5. Queued/serialized login work pushes p99 to ~3.8s and raises 5xx to ~14% [F8, F9]
6. api-gateway inherits the latency (flat 210ms -> ~2.4-3.9s in the same bucket) [F13]
7. Safety check: no deployment, auth release/rollback, or shared-component config change coincides with onset, so no change-side trigger explains it [F1, F2, F3, F7, F14, F18]

**Supporting evidence**
  - `F8` ✔ auth-service p99 latency was flat at ~88–91ms from 05:00 through 06:40, then jumped to avg 2288.53ms (max 4012.81) in the 06:50 bucket and stayed ~3770–3867ms through 08:50. Onset is the 06:50–07:00 bucket.
  - `F9` ✔ auth-service error_rate (HTTP 5xx %) was ~0.2% until 06:40, then rose to avg 7.11% (max 14.76) in the 06:50 bucket and ~14% thereafter (e.g. 14.13% at 08:50). Same onset bucket as latency.
  - `F10` ✔ auth-service error signature on /v1/auth/login is app-generated 503s from accept-queue overflow plus worker-pool saturation, not downstream timeouts: top pattern 'request rejected: accept queue full, returning <n> (POST /v<n>/auth/login)' count=372 first_seen 06:55:42; 'worker pool saturated (active=64/64, queue=<n>)' count=249 first_seen 06:54:53 (e.g. queue=39035, queue=98763), and 'autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further' at 06:57:15.
  - `F11` ✔ auth-service CPU jumped from a stable ~37–38% baseline (05:00–06:40) to avg 84.31% (max 97.33) in the 06:50 bucket and ~97% thereafter, coincident with the latency/error onset.
  - `F12` ✔ auth-service memory stayed flat (~47–57%, avg ~52%) across the entire 05:00–09:00 window including after onset (e.g. 51.46% at 06:50, 51.76% last), so memory is NOT a contributing trend; CPU is the deviating resource.
  - `F13` ✔ api-gateway p99 latency is NOT isolated: it was flat ~206–213ms through 06:40, then jumped to avg 2441.06ms (max 4200.87) in the same 06:50 bucket and ~3920ms thereafter — matching the auth-service onset bucket, consistent with gateway inheriting auth-service latency.
  - `F17` ✔ The nearest precedent in the wider incident search is HIST-113 (2026-01-28, pricing-engine CPU saturation at market open, root cause 'Record market-open volume exceeded provisioned capacity', category capacity_saturation) — pre-scaled/raised replicas as resolution — which matches the current CPU-saturation-with-autoscaler-cap signature more closely than HIST-101 (bcrypt bad deployment).
  - `F2` ✔ The same near-empty change set holds with the window widened back to 2026-04-18, so the 04-21→04-22 result is not an artifact of a too-narrow window: still exactly 1 deployment (dep-5803) and 1 config change (cfg-2801) in the 4-day span.
  - `F14` ✔ Widening the query window does not change the near-empty change set: even over a month (2026-03-22 → 2026-04-22) for redis-cache the config-change count is 0, and for portfolio-service over 05:00-08:59 on 04-22 the count is 0 — so no shared-component or neighbour config change coincides with the onset.
**Contradicting evidence**
  - (none)
**Assumptions**
  - A genuine increase in login/CPU demand occurred at ~06:50 that the findings do not directly quantify (no RPS/QPS metric was captured).
  - maxReplicas=8 is a long-standing cap rather than a recently-tightened limit (no HPA config history available).

### H2: Recurrence of HIST-101 bcrypt regression via an unobserved code/flag change on auth-service
The reporter's suspicion: auth-service login became CPU-bound (~97% CPU) from added hashing work (e.g. bcrypt cost raised), matching HIST-101's symptom profile (login p99 4s, 503s, high CPU) and its CPU-bound/blast-radius-only-auth-service signature. Because no deployment is visible in the change feed, the trigger would have to be a code path change delivered outside the enumerated feed (e.g. a feature flag enabling a costlier hashing path).

**Causal chain**
1. A code path in auth-service is changed to do more hashing work (bcrypt cost/rounds raised) without a visible deployment record *(assumed)*
2. Per-request CPU cost rises on the login path, saturating auth-service CPU to ~97% [F11]
3. Worker pool and accept queue saturate, producing 503s and ~3.8s p99 [F10, F8, F9]
4. Symptom profile matches HIST-101 (login p99 4s, 503, high CPU), blast radius auth-service only [F6]
5. api-gateway latency mirrors auth-service as the downstream victim [F13]

**Supporting evidence**
  - `F6` ✔ Historical precedent HIST-101 (2025-08-14, 'Login latency after auth-service release') with symptoms 'login p99 4s', '503 on login', 'high cpu'; root cause 'bcrypt cost factor raised 10->14 in release v6.4.0', category bad_deployment, resolution 'Rolled back v6.4.0'. Its blast radius was auth-service only. Related past incidents: HIST-108 (2025-10-01, JWT signing-key rotation, 401s), HIST-107 (2025-12-09, redis cold-cache storm), HIST-109 (2025-07-11 token validation).
  - `F8` ✔ auth-service p99 latency was flat at ~88–91ms from 05:00 through 06:40, then jumped to avg 2288.53ms (max 4012.81) in the 06:50 bucket and stayed ~3770–3867ms through 08:50. Onset is the 06:50–07:00 bucket.
  - `F9` ✔ auth-service error_rate (HTTP 5xx %) was ~0.2% until 06:40, then rose to avg 7.11% (max 14.76) in the 06:50 bucket and ~14% thereafter (e.g. 14.13% at 08:50). Same onset bucket as latency.
  - `F10` ✔ auth-service error signature on /v1/auth/login is app-generated 503s from accept-queue overflow plus worker-pool saturation, not downstream timeouts: top pattern 'request rejected: accept queue full, returning <n> (POST /v<n>/auth/login)' count=372 first_seen 06:55:42; 'worker pool saturated (active=64/64, queue=<n>)' count=249 first_seen 06:54:53 (e.g. queue=39035, queue=98763), and 'autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further' at 06:57:15.
  - `F11` ✔ auth-service CPU jumped from a stable ~37–38% baseline (05:00–06:40) to avg 84.31% (max 97.33) in the 06:50 bucket and ~97% thereafter, coincident with the latency/error onset.
  - `F13` ✔ api-gateway p99 latency is NOT isolated: it was flat ~206–213ms through 06:40, then jumped to avg 2441.06ms (max 4200.87) in the same 06:50 bucket and ~3920ms thereafter — matching the auth-service onset bucket, consistent with gateway inheriting auth-service latency.
**Contradicting evidence**
  - `F1` ✔ Only one deployment exists across all services in the incident window: portfolio-service v4.4.0 deployed 2026-04-22T05:50:00Z (author ci-bot, 'Holdings export', changes ['CSV export endpoint'], status live, rolled_back_at null). No auth-service release and no rollback appear.
  - `F7` ✔ The HIST-101 analog does not match the change record: HIST-101's trigger was a code release (v6.4.0) but no auth-service or any-service release/rollback occurred in this window, so the observed change feed gives no deployment-side explanation for a bcrypt-style regression.
  - `F18` ✔ HIST-101 (2025-08-14) is the only login+503+high-CPU precedent and its category is bad_deployment with resolution 'Rolled back v6.4.0'; no rollout or rollback of any version exists in the current window, so the current incident cannot be a replay of the same trigger by the change feed alone (it could still share the symptom profile without a release).
  - `F2` ✔ The same near-empty change set holds with the window widened back to 2026-04-18, so the 04-21→04-22 result is not an artifact of a too-narrow window: still exactly 1 deployment (dep-5803) and 1 config change (cfg-2801) in the 4-day span.
  - `F14` ✔ Widening the query window does not change the near-empty change set: even over a month (2026-03-22 → 2026-04-22) for redis-cache the config-change count is 0, and for portfolio-service over 05:00-08:59 on 04-22 the count is 0 — so no shared-component or neighbour config change coincides with the onset.
**Assumptions**
  - A feature-flag or out-of-band code change altering the login hashing path occurred near 06:50; the flag tool was unavailable so this could not be checked.
  - No release/rollback is visible only because the change feed does not cover this class of change.

### H3: Shared neighbor degradation (redis-cache / kyc-provider) driving auth-service login faults
The real fault originates outside auth-service: the only neighbours that can inject into the login path are the shared redis-cache (session/token store, five callers) or the untraced external kyc-provider (has_telemetry:false). A redis cold-cache/failover/connection storm (as in HIST-107) or kyc-provider slowdown would stall login handling, exhausting auth-service workers and producing 503s and 4s latency.

**Causal chain**
1. redis-cache (or kyc-provider) degrades at ~06:50 (failover, cold cache, connection saturation) or kyc-provider slows *(assumed)*
2. auth-service login path blocks on the shared store/external call, holding workers longer *(assumed)*
3. Worker pool saturates (64/64, queues of 39k-98k) and accept queue overflows -> app 503s [F10]
4. auth-service CPU rises to ~97% from retry/blocked handling and latency climbs to ~3.8s p99 [F11, F8, F9]
5. api-gateway inherits the latency in the same onset bucket [F13]

**Supporting evidence**
  - `F4` ✔ Dependency map for auth-service: outbound edges to user-db (kind db), redis-cache (kind cache), kyc-provider (kind external); inbound edge api-gateway → auth-service (kind http). Node types: api-gateway/edge, user-db/postgres, redis-cache/redis (shared:true), kyc-provider/external with has_telemetry:false.
  - `F5` ✔ redis-cache is the only shared datastore in auth-service's map (shared:true), while user-db is a non-shared postgres; querying redis-cache downstream returned an empty edge list (no callees)
  - `F15` ✔ auth-service's full dependency map (downstream direction) confirms exactly three outbound edges — user-db (postgres, non-shared), redis-cache (redis, shared:true) and kyc-provider (external, has_telemetry:false) — so the only neighbours that could inject a fault into the login path are the shared redis-cache and the untraced kyc-provider.
  - `F16` ✔ redis-cache is shared by five callers (auth-service, portfolio-service, pricing-engine, market-data-service) plus api-gateway/http edges, so a redis degradation could in principle transit to login — but there is no redis config change and no adjacency to a deployed change, and the multi-service redis caller set shows no onboarding change in the window.
**Contradicting evidence**
  - `F10` ✔ auth-service error signature on /v1/auth/login is app-generated 503s from accept-queue overflow plus worker-pool saturation, not downstream timeouts: top pattern 'request rejected: accept queue full, returning <n> (POST /v<n>/auth/login)' count=372 first_seen 06:55:42; 'worker pool saturated (active=64/64, queue=<n>)' count=249 first_seen 06:54:53 (e.g. queue=39035, queue=98763), and 'autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further' at 06:57:15.
**Assumptions**
  - A redis/kyc degradation actually occurred at onset; no redis/user-db/kyc metric series were retrieved, so this is untested.
  - Blocking on a downstream would manifest as app-side worker saturation rather than visible downstream timeout errors.

### H4: auth-service log.level INFO->WARN config change as trigger
The sole auth-service-touching change in the window (log.level INFO->WARN at 03:40Z, 'Reduce log volume') altered runtime behavior and caused the login latency/503s.

**Causal chain**
1. auth-service log.level INFO->WARN applied at 03:40Z [F3]
2. Reduced verbosity changes a code path / raises CPU on login handling *(assumed)*
3. Login CPU saturates and 503s appear at 06:50 [F11, F10, F8, F9]

**Supporting evidence**
  - `F3` ✔ The single config change is auth-service log.level INFO→WARN at 2026-04-22T03:40:00Z by config-bot, description 'Reduce log volume' — a logging-verbosity change, ~3h22m before the 07:06Z report.
**Contradicting evidence**
  - `F19` ✔ The auth-service log.level INFO→WARN change at 2026-04-22T03:40:00Z is the sole auth-service-touching change; it is verbosity-only ('Reduce log volume') and precedes onset by ~3h10m, with no plausible mechanism to raise CPU from 38% to 97% (it would if anything reduce I/O), so it is not a temporal/causal match for the 06:50 onset.
  - `F8` ✔ auth-service p99 latency was flat at ~88–91ms from 05:00 through 06:40, then jumped to avg 2288.53ms (max 4012.81) in the 06:50 bucket and stayed ~3770–3867ms through 08:50. Onset is the 06:50–07:00 bucket.
  - `F9` ✔ auth-service error_rate (HTTP 5xx %) was ~0.2% until 06:40, then rose to avg 7.11% (max 14.76) in the 06:50 bucket and ~14% thereafter (e.g. 14.13% at 08:50). Same onset bucket as latency.
**Assumptions**
  - A verbosity change could plausibly raise CPU; the mechanism is unknown and counter-indicated (would reduce I/O).
**Rejected because:** F19: the change is verbosity-only, precedes onset by ~3h10m, and has no plausible mechanism to raise CPU from 38% to 97% (it would if anything reduce I/O); timing and mechanism both fail.

## Facts (verified observations)
- auth-service p99 latency was flat at ~88-91ms from 05:00 through 06:40, then jumped to avg 2288.53ms (max 4012.81) in the 06:50 bucket and stayed ~3770-3867ms through 08:50; onset is the 06:50-07:00 bucket. [F8]
- auth-service error_rate (HTTP 5xx %) was ~0.2% until 06:40, then rose to avg 7.11% (max 14.76) in the 06:50 bucket and ~14% thereafter (14.13% at 08:50), with the same onset bucket as latency. [F9]
- The /v1/auth/login error signature is app-generated 503s from accept-queue overflow ('accept queue full') count=372 first_seen 06:55:42 and worker-pool saturation ('worker pool saturated (active=64/64, queue=39035/98763)') count=249 first_seen 06:54:53, plus 'autoscaler: desired replicas 14 exceeds maxReplicas 8; cannot scale further' at 06:57:15 — not downstream timeouts. [F10]
- auth-service CPU jumped from a stable ~37-38% baseline (05:00-06:40) to avg 84.31% (max 97.33) in the 06:50 bucket and ~97% thereafter, coincident with the latency/error onset. [F11]
- auth-service memory stayed flat (~47-57%, avg ~52%) across the whole 05:00-09:00 window including after onset (51.46% at 06:50), so memory is not a contributing trend; CPU is the deviating resource. [F12]
- api-gateway p99 latency was flat ~206-213ms through 06:40, then jumped to avg 2441.06ms (max 4200.87) in the same 06:50 bucket and ~3920ms thereafter, consistent with the gateway inheriting auth-service latency. [F13]
- Only one deployment exists across all services in the incident window: portfolio-service v4.4.0 at 2026-04-22T05:50:00Z (CSV export endpoint, live, not rolled back). No auth-service release and no rollback appear. [F1]
- Widening the window to 2026-04-18 still yields exactly 1 deployment (dep-5803) and 1 config change (cfg-2801) over the 4-day span. [F2]
- The single config change is auth-service log.level INFO->WARN at 2026-04-22T03:40:00Z ('Reduce log volume'), a verbosity-only change ~3h22m before the 07:06Z report. [F3]
- auth-service's dependency map has exactly three outbound edges — user-db (postgres, non-shared), redis-cache (redis, shared:true) and kyc-provider (external, has_telemetry:false) — so the only neighbours that could inject a fault into the login path are the shared redis-cache and the untraced kyc-provider. [F15, F4]
- redis-cache is shared by five callers (auth-service, portfolio-service, pricing-engine, market-data-service) plus api-gateway/http edges; querying redis-cache downstream returned an empty edge list (no callees). [F16, F5]
- Widening the query window does not change the near-empty change set: over a month (2026-03-22 to 2026-04-22) redis-cache config-change count is 0, and portfolio-service over 05:00-08:59 on 04-22 is 0, so no shared-component or neighbour config change coincides with onset. [F14]
- Historical precedent HIST-101 (2025-08-14) had symptoms login p99 4s, 503 on login, high CPU with root cause bcrypt cost factor raised 10->14 in release v6.4.0 and was resolved by rolling back v6.4.0; its blast radius was auth-service only. [F6]

## Assumptions (not verified)
- A genuine increase in login/CPU demand occurred at ~06:50 that the findings do not directly quantify (no RPS/QPS metric was captured); the CPU rise from ~38% to ~97% is treated as evidence of demand pressure rather than proof of it.
- maxReplicas=8 is a long-standing cap rather than a recently-tightened limit, since no HPA configuration history was available; only the runtime log line 'desired replicas 14 exceeds maxReplicas 8' is observed.
- The production workload mix (auth-service, portfolio-service, redis-cache) behaves the same in this window as during HIST-113 and HIST-101, so those precedents remain applicable analogues.
- Application-level change feeds (deployments and config changes) are a complete record of code/flag-driven behavioral changes for auth-service in this window; a feature-flag flip or out-of-band code path change would not appear in them and therefore cannot be excluded.
- The 503s are attributable to auth-service rather than to api-gateway or an upstream client, since the observed 503 error strings are generated inside auth-service's own accept-queue/worker-pool path.
- The telemetry collector was not systematically down at 06:50: the flat memory metric and flat api-gateway baseline before onset argue the collection pipeline was healthy, so the observed CPU/latency rise is real signal rather than a collection artifact.
- (demoted from facts: no verified observation backs it) The sole auth-service-touching change (log.level INFO->WARN) is verbosity-only and precedes onset by ~3h10m, with no plausible mechanism to raise CPU from 38% to 97%; it is not a temporal or causal match for the 06:50 onset.
- (demoted from facts: no verified observation backs it) HIST-101's trigger was a code release (v6.4.0 bcrypt cost 10->14) but no auth-service or any-service release/rollback occurred in this window, so the change feed gives no deployment-side explanation for a bcrypt-style regression.
- (demoted from facts: no verified observation backs it) No rollout or rollback of any version exists in the current window, so the incident cannot be a replay of HIST-101's trigger by the change feed alone (it could still share the symptom profile without a release).
- (demoted from facts: no verified observation backs it) The nearest precedent in the wider incident search is HIST-113 (2026-01-28, pricing-engine CPU saturation at market open, root cause 'Record market-open volume exceeded provisioned capacity', category capacity_saturation, resolved by pre-scaling/raising replicas), which matches the current CPU-saturation-with-autoscaler-cap signature more closely than HIST-101.

## Timeline
- `2026-04-22T03:40:00Z` Sole auth-service-touching change: log.level INFO->WARN ('Reduce log volume') by config-bot — verbosity-only, ~3h10m before onset. [F3, F19]
- `2026-04-22T05:50:00Z` Only deployment in the window: portfolio-service v4.4.0 (CSV export endpoint); no auth-service release and no rollback anywhere in the window. [F1]
- `2026-04-22T05:00:00Z-06:40:00Z` Steady state: auth-service p99 flat ~88-91ms, 5xx ~0.2%, CPU ~37-38%, memory flat ~52%; api-gateway p99 flat ~206-213ms. [F8, F9, F11, F12, F13]
- `2026-04-22T06:50:00Z` Onset bucket: auth-service p99 jumps to avg 2288.53ms (max 4012.81), 5xx rises to avg 7.11% (max 14.76), CPU rises to avg 84.31% (max 97.33); api-gateway p99 jumps to avg 2441.06ms in the same bucket. Memory unchanged. [F8, F9, F11, F13, F12]
- `2026-04-22T06:54:53Z` First 'worker pool saturated (active=64/64, queue=39035)' message on auth-service (count=249 across incident). [F10]
- `2026-04-22T06:55:42Z` First 'request rejected: accept queue full, returning 503 (POST /v1/auth/login)' message (count=372 across incident). [F10]
- `2026-04-22T06:57:15Z` Autoscaler log: 'desired replicas 14 exceeds maxReplicas 8; cannot scale further' — scaling blocked at the configured cap. [F10]
- `2026-04-22T07:06:00Z` Incident INC-008 [SEV-2] reported: login latency ~4s and 503s; reporter suspects recurrence of the HIST-101 bcrypt regression. []
- `2026-04-22T08:50:00Z-08:59:00Z` Sustained degradation: auth-service p99 ~3770-3867ms, 5xx ~14%, CPU ~97%, memory still flat ~52%; dependency map confirms only user-db, redis-cache and kyc-provider as outbound neighbours. [F8, F9, F11, F12, F15]

## Recommended actions
- **[immediate]** Relieve the capacity cap now: raise auth-service HPA maxReplicas above 8 (e.g. to 14-16, matching the autoscaler's expressed desire of 14) and confirm pods actually schedule and CPU per pod drops off ~97%. -- The autoscaler explicitly logged 'desired replicas 14 exceeds maxReplicas 8; cannot scale further' while worker pools were saturated at 64/64 and accepting queue overflow 503s; releasing the cap is the direct, reversible mitigation for the CPU saturation. [F10, F11, F10]
- **[immediate]** As a parallel safe mitigation, verify there is no pending auth-service deploy/flag change to halt: confirm the change feed truly shows no auth-service release/rollback and, if a deploy pipeline for auth-service is armed, hold it until the capacity question is resolved. -- Both the capacity and the bad-deployment hypotheses predict login 503s; holding unrelated auth-service changes preserves the clean observation window and avoids confounding the capacity fix, while F1/F2/F7/F18 show no release is actually pending. [F1, F2, F7, F18]
- **[short_term]** Capture the missing demand measurement: pull auth-service RPS/QPS, request-concurrency and per-pod CPU for 05:00-09:00Z to confirm whether request volume genuinely rose at 06:50 (capacity trigger) versus CPU-per-request rising at flat load (costlier code path). -- H1 assumes a genuine demand increase that no retrieval quantified; distinguishing volume rise from per-request cost rise is what separates H1 from a hidden-cost increase and validates whether the replica bump is the right durable fix. [F8, F11, F17]
- **[short_term]** Close the shared-dependency branch: query redis-cache and user-db metric series (p99, errors, connections, evictions, failover events) for 06:40-07:10Z, and check whether other redis-cache callers (portfolio-service, pricing-engine, market-data-service) show the same onset. -- H3 remains a live low-confidence alternative because redis-cache is the only shared datastore in auth-service's map and no redis/user-db metrics were ever retrieved; if the other four redis callers are clean at 06:50 the shared-dependency branch can be discarded. [F4, F5, F15, F16]
- **[short_term]** After the fix, verify recovery against the same signals used for onset: auth-service p99 back to ~90ms, 5xx rate back toward ~0.2%, CPU back to ~38%, and api-gateway p99 back to ~210ms. -- The onset was cleanly visible across four independent series sharing the 06:50 bucket; the same series with the same thresholds give an unambiguous pass/fail on the remediation. [F8, F9, F11, F13]
- **[long_term]** Retrieve the HPA/autoscaler configuration history for auth-service (maxReplicas and target thresholds) to determine whether 8 is a long-standing cap or was recently tightened, and whether request-based scaling targets are appropriate for the login path. -- The remediated cap was hit under production load; knowing whether it was long-standing or recently tightened determines whether the failover to 14+ replicas should be made permanent and whether the scaling policy needs rewriting. [F10]
- **[long_term]** Add alerting on auth-service CPU saturation together with 'desired replicas exceeds maxReplicas' and on accept-queue-full/worker-pool-saturated log patterns, so the next capacity cliff pages before login latency reaches 4s. -- The autoscaler's inability to scale was only discoverable in application logs after 503s were already being served (06:54:53-06:57:15); an early-warning alert on the cap breach directly prevents recurrence of this failure mode, mirroring HIST-113's pre-scaling resolution. [F10, F17]
- **[long_term]** Standardize dependency-map completeness: ensure shared components (redis-cache) and external nodes (kyc-provider, has_telemetry:false) have metric coverage or an explicit documented reason for the gap, so the next login incident does not stall on unverifiable neighbours. -- This investigation's residual uncertainty came entirely from the inability to measure redis-cache and kyc-provider, the two neighbours capable of injecting into the login path (F4, F15); closing that telemetry gap converts a medium-confidence verdict into a high-confidence one. [F4, F15, F5]

## Data gaps
- No feature-flag change-log tool was available, so flags could not be enumerated for the window; a feature-flag flip (e.g. a new auth code path, bcrypt cost, cache bypass) between 04-21 and 04-22 08:59Z remains unchecked.
- No RPS/QPS or request-concurrency metric for auth-service was captured, so the presumed demand increase at 06:50 is inferred from CPU/latency rather than measured — the single biggest gap limiting confidence to medium.
- No redis-cache metric series (failover, eviction, connection saturation, cache hit rate) was retrieved; HIST-107 shows redis failover can cause shared-component storms, so a redis-cache-side event cannot be confirmed or excluded.
- No user-db or kyc-provider metric/status data exists or was retrieved; kyc-provider is external with has_telemetry:false, so an upstream identity/secret dependency fault on the login path cannot be excluded.
- No autoscaler/HPA configuration history (maxReplicas=8) was retrieved, so it is unresolved whether the replica cap itself was recently changed or is long-standing.
- No infrastructure-change feed (node/volume/LB/network) was queried, so infra-level changes in the window are unenumerated; no k8s node/container resource-request or CPU-limit change record exists in the change feed for auth-service.
- The auth-service log query was truncated (30 of 657 matched), so the absence of other error patterns — including upstream timeout errors — is not proven.
- api-gateway error_rate was never retrieved (error-call budget exhausted), so a gateway-originated error component cannot be quantified and 503s cannot be attributed between upstream and gateway with full certainty.
- Other services sharing redis-cache (portfolio-service, pricing-engine, market-data-service) were not checked for a coincident onset, which is exactly the measurement that would settle the shared-dependency branch.

## What would change this conclusion
- Direct measurements showing auth-service RPS/request concurrency was flat or declining at 06:50 while CPU per request rose would undercut H1's demand-increase premise and point to a per-request cost increase (H2 or an unreported code/flag change).
- Evidence of a redis-cache failover, cold-cache storm or connection saturation at ~06:50Z, or a simultaneous latency/error onset in the other four redis-cache callers (portfolio-service, pricing-engine, market-data-service), would promote H3 (shared-dependency fault) over H1.
- Discovery of an auth-service release, hotfix, or feature-flag flip affecting the login hashing path anywhere between 2026-04-21 and 2026-04-22T08:59Z (outside the enumerated application change feed) would revive H2 as a recurrence of the HIST-101 bcrypt regression.
- Evidence that the auth-service container CPU limit or request/limit configuration was recently tightened — explaining CPU saturation at flat memory without a genuine demand increase — would reclassify the root cause as a configuration change rather than raw capacity saturation.
- Proof that api-gateway 503s or latency preceded (rather than followed) the auth-service onset, or that the gateway's own accept-queue/worker-pool saturated independently, would shift the fault upstream of auth-service and invalidate the H1 capacity attribution.
- Resolution of the incident by the replica-cap increase alone (p99, 5xx and CPU returning to baseline without any code or dependency change) would strongly confirm H1 as the root cause and close out the remaining uncertainty.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: The sole auth-service-touching change (log.level INFO->WARN) is verbosity-only a
- fact demoted to assumption: HIST-101's trigger was a code release (v6.4.0 bcrypt cost 10->14) but no auth-se
- fact demoted to assumption: No rollout or rollback of any version exists in the current window, so the incid
- fact demoted to assumption: The nearest precedent in the wider incident search is HIST-113 (2026-01-28, pric

## Investigation stats
- latency 98.17s, LLM calls 13, tokens 81020 (in 63849 / out 17171)
- tool calls 24 (2 failed, 0 partial, 0 recovered by retry), findings 19/19 verified
