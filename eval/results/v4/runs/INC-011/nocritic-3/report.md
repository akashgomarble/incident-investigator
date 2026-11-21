# Incident investigation report: INC-011
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
The top hypothesis (confidence 0.55, medium) is that config change cfg-3120 on pricing-engine — ramping flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04Z — moved all quote computation onto the CPU-expensive GreeksV2 calculator path, saturating pricing-engine CPU (~40%→99%) and pushing p99 from ~60ms to ~2.6s, which blew order-service's 2000ms quote budget. This is clearly ahead of the field (next: organic capacity saturation at 0.30) and is supported by co-timed CPU/latency onset, an error signature explicitly naming calculator=GreeksV2, and the market-data-service deploy (dep-6111) showing zero errors. Confidence is capped at medium because the metric bucket size (420s) prevents ordering cfg-3120's 12:04 timestamp against the true onset within the 11:58→12:05 interval, so a purely organic volume-driven saturation cannot be fully excluded. We recommend an immediate rollback of cfg-3120 to 5% (or pausing the GreeksV2 ramp) as a safe, reversible mitigation, with request_rate/depth checks run in parallel to discriminate H1 from H2.

## Probable root cause
**cfg-3120 GreeksV2 100% ramp saturated pricing-engine CPU** -- config_change in `pricing-engine`, triggered by `cfg-3120` (confidence 0.55, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: cfg-3120 GreeksV2 100% ramp saturated pricing-engine CPU | config_change | pricing-engine | cfg-3120 | 0.55 (medium) | active |
| 2 | H2: Organic capacity saturation (market-open quote volume) on pricing-engine, HIST-113-like | capacity_saturation | pricing-engine | null | 0.30 (low) | active |
| 3 | H3: dep-6111 protobuf serialization change broke pricing-engine's parsing of market-data-service quotes | bad_deployment | market-data-service | dep-6111 | 0.15 (low) | active |
| 4 | H4: Shared redis-cache degradation (cold cache / failover) on the quote path | dependency_failure | redis-cache | null | 0.10 (low) | active |

### H1: cfg-3120 GreeksV2 100% ramp saturated pricing-engine CPU
Config change cfg-3120 on pricing-engine ramped flags.pricing.greeks_v2.rollout_pct from 5 to 100 at 12:04Z, switching all quote computation to the GreeksV2 calculator path. GreeksV2 is materially more CPU-expensive than the prior default, so pricing-engine CPU went from ~40% to ~99% and per-request compute latency rose to ~2.6s, blowing the 2000ms quote budget. order-service, which calls pricing-engine synchronously for quotes, then timed out.

**Causal chain**
1. cfg-3120 ramps greeks_v2.rollout_pct 5->100 on pricing-engine at 12:04Z, routing all quote traffic to the GreeksV2 calculator [F8, F14]
2. GreeksV2 computation is more expensive, driving pricing-engine CPU from ~40% to 99-100% saturated [F5]
3. CPU saturation raises per-request quote compute time, p99 60ms -> ~2600ms, exceeding the 2000ms budget [F4, F2]
4. order-service's synchronous pricing-engine quote call then exceeds its 2500ms client timeout [F1, F10, F15]

**Supporting evidence**
  - `F8` ✔ Exactly one config/flag change was found in the window across all services: cfg-3120 on pricing-engine, key flags.pricing.greeks_v2.rollout_pct, changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic') -- 8 minutes after the market-data-service deploy and 12 minutes before the incident was reported at 12:16Z.
  - `F14` ✔ The only config/flag change in the window is cfg-3120 on pricing-engine: flags.pricing.greeks_v2.rollout_pct changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments, 'Ramp Greeks v2 experiment to all traffic'. This timestamp sits BETWEEN the 11:58-12:05 latency/CPU onset bucket and is ~2 minutes before the first order-service quote timeout at 12:06:10Z. Note the pricing-engine error signature explicitly names the ramped calculator: 'calculator=GreeksV2'.
  - `F5` ✔ pricing-engine cpu_pct was ~40% through 11:58 (11:58 avg 39.89), then saturated to avg 99.11-99.33% / max 100% starting in the 12:05 bucket and remained pinned ~99% for the rest of the window — i.e. CPU saturation and the latency jump start in the same 12:05 bucket.
  - `F4` ✔ pricing-engine latency_p99_ms was stable ~59-60ms through 11:58 (11:58 avg 58.97), then jumped to avg 2609.71 / max 2849.25 in the 12:05 bucket and stayed ~2600-2700ms thereafter. Onset is between 11:58 and 12:05, and 2600ms is ~43x the baseline. (420s buckets, so exact minute within 12:05-12:12 is not resolvable at this resolution.)
  - `F2` ✘ unverified Pricing-engine emitted its first error at 2026-05-20T12:06:41Z — 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' — all 276 errors share this identical pattern; the downstream-error signature is a deadline/timeout, not a (de)serialization failure.
  - `F1` ✔ First order-service quote timeout occurred at 2026-05-20T12:06:10Z ("pricing-engine quote call timed out after 2500ms orderId=4bd44879"), about 10 minutes before the 12:16 report. All 225 error logs in the window share the single pattern 'pricing-engine quote call timed out after <n>ms'.
  - `F10` ✔ order-service quote path dependency graph: order-service -> pricing-engine (http), -> ledger-db (db), -> exchange-gateway (http), -> kafka (queue-produce); pricing-engine -> market-data-service (http) and -> redis-cache (cache). So order-service's quote does NOT call market-data-service directly -- it goes order-service -> pricing-engine -> market-data-service.
  - `F15` ✔ Dependency graph confirms order-service's quote path is order-service -> pricing-engine -> market-data-service (http), with pricing-engine also depending on redis-cache (cache) and market-data-service also depending on redis-cache. So market-data-service is only reachable from order-service transitively through pricing-engine.
**Contradicting evidence**
  - `F6` ✔ order-service latency_p99_ms rose from ~149-154ms baseline (11:58 avg 150.91) to avg 2433-2972ms in the same 12:05 bucket, tracking the pricing-engine onset; order-service available metrics include db_pool_in_use and restarts (not yet examined).
**Assumptions**
  - GreeksV2 is more CPU-expensive than the preceding default path (not directly measured)
  - request_rate/depth did not also rise sharply at the same time (not checked)

### H2: Organic capacity saturation (market-open quote volume) on pricing-engine, HIST-113-like
A surge in quote request volume/recursion depth exceeded provisioned pricing-engine capacity (CPU), pinning it at 99-100% and pushing p99 to ~2.6s. The GreeksV2 ramp is a coincidental bystander; the true trigger is load outgrowing provisioned capacity, exactly matching historical incident HIST-113 (same symptom pair, capacity_saturation, fixed by raising replicas).

**Causal chain**
1. Quote request volume/load rises beyond provisioned pricing-engine capacity *(assumed)*
2. pricing-engine CPU saturates to 99-100% [F5]
3. p99 latency rises ~43x to ~2600ms, exceeding budget and client timeout [F4, F2, F1]

**Supporting evidence**
  - `F5` ✔ pricing-engine cpu_pct was ~40% through 11:58 (11:58 avg 39.89), then saturated to avg 99.11-99.33% / max 100% starting in the 12:05 bucket and remained pinned ~99% for the rest of the window — i.e. CPU saturation and the latency jump start in the same 12:05 bucket.
  - `F4` ✔ pricing-engine latency_p99_ms was stable ~59-60ms through 11:58 (11:58 avg 58.97), then jumped to avg 2609.71 / max 2849.25 in the 12:05 bucket and stayed ~2600-2700ms thereafter. Onset is between 11:58 and 12:05, and 2600ms is ~43x the baseline. (420s buckets, so exact minute within 12:05-12:12 is not resolvable at this resolution.)
  - `F2` ✘ unverified Pricing-engine emitted its first error at 2026-05-20T12:06:41Z — 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' — all 276 errors share this identical pattern; the downstream-error signature is a deadline/timeout, not a (de)serialization failure.
  - `F1` ✔ First order-service quote timeout occurred at 2026-05-20T12:06:10Z ("pricing-engine quote call timed out after 2500ms orderId=4bd44879"), about 10 minutes before the 12:16 report. All 225 error logs in the window share the single pattern 'pricing-engine quote call timed out after <n>ms'.
  - `F12` ✔ Historical precedent search for quote-timeout/serialization patterns returned: HIST-113 (2026-01-28) pricing-engine CPU saturation at market open -> 'quote timeouts','cpu 99%', root cause 'Record market-open volume exceeded provisioned capacity' (capacity_saturation); HIST-107 (2025-12-09) Cold cache storm after redis failover, services redis-cache+market-data-service, symptoms 'cache miss storm','quote latency', root cause 'Redis primary failure promoted an empty replica'; HIST-101 (2025-08-14) auth-service bad_deployment root cause 'bcrypt cost factor raised 10->14 in release v6.4.0'. These are leads only.
  - `F17` ✔ Historical precedents for the exact symptom pair ('quote timeouts' + 'cpu 99%' on pricing-engine) exist: HIST-113 (2026-01-28) root cause 'Record market-open volume exceeded provisioned capacity' (capacity_saturation, resolution raised replica count). HIST-107 (2025-12-09) is a shared-redis cold-cache storm with 'quote latency'. HIST-101 (2025-08-14) is a bad_deployment whose symptom included 'high cpu'. These are leads only, not evidence about this incident.
**Contradicting evidence**
  - `F8` ✔ Exactly one config/flag change was found in the window across all services: cfg-3120 on pricing-engine, key flags.pricing.greeks_v2.rollout_pct, changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic') -- 8 minutes after the market-data-service deploy and 12 minutes before the incident was reported at 12:16Z.
  - `F14` ✔ The only config/flag change in the window is cfg-3120 on pricing-engine: flags.pricing.greeks_v2.rollout_pct changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments, 'Ramp Greeks v2 experiment to all traffic'. This timestamp sits BETWEEN the 11:58-12:05 latency/CPU onset bucket and is ~2 minutes before the first order-service quote timeout at 12:06:10Z. Note the pricing-engine error signature explicitly names the ramped calculator: 'calculator=GreeksV2'.
**Assumptions**
  - A load/volume increase occurred in the 11:58-12:05 window (request_rate not queried)
  - cfg-3120's 12:04 timing is coincidental rather than causal

### H3: dep-6111 protobuf serialization change broke pricing-engine's parsing of market-data-service quotes
market-data-service v5.2.0 switched quote serialization to protobuf at 11:56Z. pricing-engine, which calls market-data-service on the quote path, cannot correctly parse the new payload, causing retries/wasteful fallback compute that saturates CPU and blows the compute deadline. The reporter's stated suspicion.

**Causal chain**
1. dep-6111 switches market-data-service quote payloads to protobuf at 11:56Z [F7, F13]
2. pricing-engine (the caller) cannot parse/handle the new format, retrying or falling back and burning CPU *(assumed)*
3. pricing-engine CPU saturates and quote compute exceeds deadline [F5, F2]
4. order-service quote call times out [F1]

**Supporting evidence**
  - `F7` ✔ Only one deployment occurred across ALL services in 11:30-13:59: dep-6111, market-data-service v5.2.0, deployed 2026-05-20T11:56:00Z by v.rao, 'Switch quote serialization to protobuf' / 'protobuf quote payloads for internal callers', status live, rolled_back_at null (i.e., NOT rolled back as of 13:59).
  - `F13` ✔ The only code deployment in 11:30-13:59 across ALL services is dep-6111: market-data-service v5.2.0, deployed 2026-05-20T11:56:00Z by v.rao, summary 'Switch quote serialization to protobuf', status 'live', rolled_back_at null. It precedes the first order-service quote timeout (12:06:10Z) by ~10 minutes but is NOT on order-service's direct call path (order-service does not call market-data-service directly; it reaches it only via pricing-engine).
  - `F10` ✔ order-service quote path dependency graph: order-service -> pricing-engine (http), -> ledger-db (db), -> exchange-gateway (http), -> kafka (queue-produce); pricing-engine -> market-data-service (http) and -> redis-cache (cache). So order-service's quote does NOT call market-data-service directly -- it goes order-service -> pricing-engine -> market-data-service.
**Contradicting evidence**
  - `F3` ✔ market-data-service logged ZERO errors between 08:00Z and 13:59Z; its only log of any level in this span is a single benign WARN 'deprecated TLS cipher offered by client' at 11:43:53Z. It shows no error/latency symptom despite the report blaming its serialization change.
  - `F2` ✘ unverified Pricing-engine emitted its first error at 2026-05-20T12:06:41Z — 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' — all 276 errors share this identical pattern; the downstream-error signature is a deadline/timeout, not a (de)serialization failure.
  - `F14` ✔ The only config/flag change in the window is cfg-3120 on pricing-engine: flags.pricing.greeks_v2.rollout_pct changed 5 -> 100 at 2026-05-20T12:04:00Z by pm-experiments, 'Ramp Greeks v2 experiment to all traffic'. This timestamp sits BETWEEN the 11:58-12:05 latency/CPU onset bucket and is ~2 minutes before the first order-service quote timeout at 12:06:10Z. Note the pricing-engine error signature explicitly names the ramped calculator: 'calculator=GreeksV2'.
**Assumptions**
  - pricing-engine parses market-data-service payloads in a way incompatible with protobuf
  - the (de)serialization breakage manifests only as CPU-bound retries rather than parse errors

### H4: Shared redis-cache degradation (cold cache / failover) on the quote path
redis-cache, shared by pricing-engine and market-data-service, degraded (cold cache or failover), forcing pricing-engine to recompute quotes from source and saturating CPU. Matches HIST-107 cold-cache storm precedent.

**Causal chain**
1. redis-cache cold/failed, cache misses on the quote path *(assumed)*
2. pricing-engine recomputes from source, driving CPU to 99% [F5]
3. quote latency blows budget -> order-service timeout [F2, F1]

**Supporting evidence**
  - `F11` ✔ Shared components in the quote path: redis-cache is marked shared:true and is a dependency of BOTH pricing-engine (cache) and market-data-service (cache); ledger-db and kafka are also shared:true. market-data-service additionally calls exchange-gateway (http) and is called by portfolio-service and api-gateway.
  - `F12` ✔ Historical precedent search for quote-timeout/serialization patterns returned: HIST-113 (2026-01-28) pricing-engine CPU saturation at market open -> 'quote timeouts','cpu 99%', root cause 'Record market-open volume exceeded provisioned capacity' (capacity_saturation); HIST-107 (2025-12-09) Cold cache storm after redis failover, services redis-cache+market-data-service, symptoms 'cache miss storm','quote latency', root cause 'Redis primary failure promoted an empty replica'; HIST-101 (2025-08-14) auth-service bad_deployment root cause 'bcrypt cost factor raised 10->14 in release v6.4.0'. These are leads only.
  - `F17` ✔ Historical precedents for the exact symptom pair ('quote timeouts' + 'cpu 99%' on pricing-engine) exist: HIST-113 (2026-01-28) root cause 'Record market-open volume exceeded provisioned capacity' (capacity_saturation, resolution raised replica count). HIST-107 (2025-12-09) is a shared-redis cold-cache storm with 'quote latency'. HIST-101 (2025-08-14) is a bad_deployment whose symptom included 'high cpu'. These are leads only, not evidence about this incident.
**Contradicting evidence**
  - `F16` ✔ redis-cache is a shared:true datastore with multiple upstream consumers (auth-service, portfolio-service, pricing-engine, market-data-service), so it is a plausible shared-component path; however T1 reported no serialization/deserialization errors and the pricing-engine failure mode is CPU saturation (99-100%) on GreeksV2 computation, which a cache miss storm would not by itself explain. A cold-cache storm (HIST-107) would be expected to show cache-miss symptoms, not 100% CPU pinned on the calculator.
  - `F2` ✘ unverified Pricing-engine emitted its first error at 2026-05-20T12:06:41Z — 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2' — all 276 errors share this identical pattern; the downstream-error signature is a deadline/timeout, not a (de)serialization failure.
**Assumptions**
  - redis-cache actually degraded (no redis metrics were queried)

## Facts (verified observations)
- The first order-service quote timeout occurred at 12:06:10Z, with the message 'pricing-engine quote call timed out after 2500ms'; all 225 error logs in the window share a single timeout pattern (no serialization errors). [F1]
- pricing-engine latency_p99_ms was stable at ~59-60ms through 11:58, then jumped to ~2600ms (max 2849ms) in the 12:05 bucket and stayed there. [F4]
- pricing-engine cpu_pct was ~40% through 11:58, then saturated to ~99% (max 100%) starting in the same 12:05 bucket and stayed pinned for the rest of the window. [F5]
- order-service latency_p99_ms rose from ~150ms baseline to ~2400-2970ms in the same 12:05 bucket, tracking the pricing-engine onset. [F6]
- Exactly one config/flag change existed in the window: cfg-3120 on pricing-engine, key flags.pricing.greeks_v2.rollout_pct 5 -> 100 at 12:04:00Z by pm-experiments ('Ramp Greeks v2 experiment to all traffic'). [F8]
- Only one deployment occurred across all services in 11:30-13:59: dep-6111, market-data-service v5.2.0 ('Switch quote serialization to protobuf'), deployed 11:56:00Z, status live, rolled_back_at null. [F7]
- market-data-service logged zero errors between 08:00Z and 13:59Z; its only log in that span was a benign TLS WARN at 11:43:53Z — no error or latency symptom despite the report blaming its serialization change. [F3]
- order-service's quote path is order-service -> pricing-engine -> market-data-service; order-service does not call market-data-service directly. [F10, F15]
- cfg-3120's timestamp (12:04:00Z) sits between the 11:58-12:05 latency/CPU onset bucket and is ~2 minutes before the first order-service quote timeout at 12:06:10Z. [F14]
- Historical precedents match the 'quote timeouts' + 'cpu 99%' pattern (HIST-113 capacity saturation; HIST-107 redis cold-cache storm; HIST-101 bad deployment); these are leads, not evidence about this incident. [F17]
- A complete retried get_config_changes returned exactly one config change (cfg-3120), following an initial partial result due to a config-audit shard being down. [F9]

## Assumptions (not verified)
- ASSUMPTION: GreeksV2 is materially more CPU-expensive than the preceding default calculator path (statically inferred, not directly measured).
- ASSUMPTION: request_rate / recursion depth did not also rise sharply at the same time (not checked), which is required to separate H1 from H2.
- ASSUMPTION: cfg-3120 took effect promptly at 12:04:00Z rather than being batched/delayed.
- ASSUMPTION: The absence of serialization errors established for ERROR-level streams of the three named services generalizes sufficiently to weaken H3.
- ASSUMPTION: The retried complete get_config_changes (count 1) accurately enumerates the window's config changes despite the initial shard outage.
- (demoted from facts: no verified observation backs it) Pricing-engine's first error at 12:06:41Z was a request deadline exceeded on the quote path (2000ms budget) with calculator=GreeksV2; all 276 pricing-engine errors share this identical pattern.
- (demoted from facts: no verified observation backs it) redis-cache is a shared component of both pricing-engine and market-data-service; the pricing-engine failure mode is CPU saturation on the calculator, which a cache-miss storm would not by itself explain.
- (demoted from facts: no verified observation backs it) The ranked candidate assessment identifies cfg-3120 as the leading candidate based on timing alignment with the CPU/latency onset, ownership of the degraded dependency, and the error naming calculator=GreeksV2.

## Timeline
- `2026-05-20T11:43:53Z` market-data-service emits its only log in the window — a benign TLS cipher WARN; no errors. [F3]
- `2026-05-20T11:56:00Z` dep-6111 deployed: market-data-service v5.2.0 'Switch quote serialization to protobuf' (status live, never rolled back). [F7, F13]
- `2026-05-20T11:58:00Z` Last baseline bucket: pricing-engine CPU ~40% (39.89) and p99 ~59ms (58.97); order-service p99 ~151ms. [F4, F5, F6]
- `2026-05-20T12:04:00Z` cfg-3120 applied on pricing-engine: flags.pricing.greeks_v2.rollout_pct ramped 5 -> 100 ('Ramp Greeks v2 experiment to all traffic'). [F8, F14]
- `2026-05-20T12:05:00Z` Onset bucket: pricing-engine CPU saturates to ~99% (max 100%) and p99 jumps to ~2600ms in the same 420s bucket; order-service p99 rises to ~2400-2970ms. [F4, F5, F6]
- `2026-05-20T12:06:10Z` First order-service quote timeout: 'pricing-engine quote call timed out after 2500ms'. [F1]
- `2026-05-20T12:06:41Z` Pricing-engine first error: 'request deadline exceeded computing quote (2000ms budget) calculator=GreeksV2'. [F2]
- `2026-05-20T12:16:00Z` Incident INC-011 reported by on-call SRE; pricing-engine CPU remained pinned ~99% and p99 ~2600-2700ms through end of window. [F4, F5]

## Recommended actions
- **[immediate]** Immediately roll back cfg-3120: revert flags.pricing.greeks_v2.rollout_pct from 100 back to 5 (or 0) on pricing-engine to take traffic off the GreeksV2 calculator path. -- cfg-3120 is the only change aligned with the 11:58->12:05 CPU/latency onset and the 12:06:10 product failure; reverting is reversible, low-risk, and directly tests H1 by restoring the prior calculator path. [F8, F14, F2, F4, F5]
- **[immediate]** In parallel, query pricing-engine request_rate / recursion depth / quote volume for 11:50-12:10 at the finest available resolution to test whether load rose independently of cfg-3120. -- Discriminates H1 (flag-driven CPU) from H2 (organic capacity saturation); without this the two remain indistinguishable on CPU metrics alone. [F4, F5, F12, F17]
- **[short_term]** After rollback, confirm recovery: verify pricing-engine cpu_pct returns toward ~40% and p99 returns to ~60ms, and that order-service quote timeouts cease. -- Validates that cfg-3120 was the cause; failure to recover would point to H2 (capacity) or a shared-component cause (H4). [F4, F5, F1, F2]
- **[short_term]** Do NOT roll back dep-6111 (market-data-service v5.2.0) as a first action; if it remains suspect, first check pricing-engine for parse/(de)serialization errors on the market-data-service call path before acting. -- market-data-service shows zero errors (F3) and is only transitively on order-service's path (F10/F15), so it is a weak candidate; an unwarranted rollback could destabilize other callers. [F3, F7, F10, F15, F13]
- **[short_term]** Query redis-cache (and ledger-db/kafka) cache-hit/miss, latency, and availability metrics for the window to confirm/deny a shared-datastore contribution. -- Closes the shared-component hypothesis (H4, HIST-107 precedent); a cache-miss storm would show miss/latency symptoms, not calculator CPU saturation. [F11, F12, F16, F17]
- **[short_term]** If load (not the flag) is the driver, raise pricing-engine replica count / CPU allocation as the mitigation, mirroring the HIST-113 resolution. -- Provides the alternative remedy if request_rate confirms organic capacity saturation (H2). [F12, F17]
- **[long_term]** Adopt guarded rollout for CPU-heavy calculator experiment flags: require canary/staged ramp with CPU and p99 SLO gates and automatic rollback instead of a 5->100 jump. -- Prevents recurrence of an instant all-traffic ramp onto a CPU-expensive path (the cfg-3120 5->100 change). [F8, F14]
- **[long_term]** Instrument pricing-engine per-calculator CPU cost/latency and alarm on calculator-path CPU saturation and quote p99 breaching budget, with correlation to in-flight config changes. -- Would have caught the GreeksV2 CPU cost directly and made CPU/flag correlation explicit, addressing the measurement gap behind the H1/H2 ambiguity. [F4, F5, F2]

## Data gaps
- pricing-engine memory_pct, error_rate, request_rate, restarts, and thread/pool metrics were not queried; memory/pool/thread exhaustion or restarts on the quote path cannot be ruled out.
- Metric bucketing at 420s prevents resolving the true onset below the 11:58->12:05 interval, so cfg-3120 (12:04) cannot be definitively ordered against onset — the key limitation for H1 vs H2.
- No metric queries were run for pricing-engine, market-data-service, redis-cache, ledger-db, or kafka dependencies, so no dependency-level metric evidence exists to confirm/deny a shared-component cause.
- Serialization-error absence is established only for ERROR-level streams of the three named services; a broad multi-service/multi-level scan was not performed.
- The initial get_config_changes was partial (config-audit shard 2/3 down); enumeration relies on the retry, leaving residual risk of an unseen config change.
- No deployment diff/compatibility data for the dep-6111 protobuf switch, so whether it broke internal callers (e.g. pricing-engine parsing) is unconfirmed.
- No historical precedent with a serialization/payload-incompatibility root cause was found, so H3 has no confirming precedent either way.

## What would change this conclusion
- Evidence that quote request_rate/recursion depth rose sharply at ~12:04-12:05 independent of cfg-3120, which would favor organic capacity saturation (H2) and demote the config change to a coincidental bystander.
- Demonstration that reverting cfg-3120 does NOT restore CPU/p99/latency, which would falsify H1.
- Pricing-engine logs/metrics showing parse or (de)serialization errors on the market-data-service call path, which would revive H3.
- redis-cache (or ledger-db/kafka) metrics showing a cold-cache/failover storm coincident with onset, which would support H4.
- Pricing-engine memory/thread/pool saturation or restarts coincident with onset, indicating a cause other than the GreeksV2 computation path.
- Finer-resolution metrics showing CPU/latency onset clearly BEFORE 12:04 (i.e. before cfg-3120), which would undercut H1's causal timing.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: Pricing-engine's first error at 12:06:41Z was a request deadline exceeded on the
- fact demoted to assumption: redis-cache is a shared component of both pricing-engine and market-data-service
- fact demoted to assumption: The ranked candidate assessment identifies cfg-3120 as the leading candidate bas

## Investigation stats
- latency 97.2s, LLM calls 12, tokens 69376 (in 55049 / out 14327)
- tool calls 22 (0 failed, 1 partial, 0 recovered by retry), findings 17/18 verified
