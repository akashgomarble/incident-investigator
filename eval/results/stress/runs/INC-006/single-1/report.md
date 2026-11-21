# Incident investigation report: INC-006
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
pricing-engine p99 latency degraded from ~60ms to >2s starting ~13:12, driven by a downstream cold-cache storm. The root fault originates in redis-cache (shared platform datastore): the primary node redis-cache-2 failed a health check at 13:22:10 ('3 consecutive PING timeouts'), and failover promoted replica redis-cache-3, whose dataset was EMPTY because it was still resyncing. This wiped the quote cache (memory_pct collapsed 70%->~8%, hit_ratio 96%->14%), so market-data-service had to rebuild every quote from the exchange feed on demand ('cache miss storm' x289), driving its CPU to ~90% and p99 to ~1.9s. That propagated up to pricing-engine (p99 up to 2.3s, error_rate 0.2%->4%, deadline-exceeded errors). exchange-gateway is healthy (flat ~130ms), no deployments to pricing-engine/market-data-service/redis-cache, and request rates are flat -- ruling out bad deployment, config change, dependency/exchange failure and capacity saturation. This matches historical incident HIST-107 ('Cold cache storm after redis failover / Redis primary failure promoted an empty replica'), but that is a lead, not evidence. Slow recovery as the cache warms (hit ratio climbing 14%->45%) is consistent with continued cold-cache rebuilding.

## Probable root cause
**redis-cache primary failure promoted an empty replica -> cold-cache storm in market-data-service -> pricing-engine latency** -- infrastructure_failure in `redis-cache` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: redis-cache primary failure promoted an empty replica -> cold-cache storm in market-data-service -> pricing-engine latency | infrastructure_failure | redis-cache | - | 0.85 (high) | active |
| 2 | H2: Exchange feed / exchange-gateway degradation causing slow quote rebuilds | dependency_failure | exchange-gateway | - | 0.10 (low) | rejected |
| 3 | H3: Bad deployment or config/feature-flag change to pricing-engine or market-data-service | bad_deployment | market-data-service | - | 0.05 (low) | rejected |
| 4 | H4: Capacity saturation / resource exhaustion on market-data-service (traffic or memory) | capacity_saturation | market-data-service | - | 0.05 (low) | rejected |

### H1: redis-cache primary failure promoted an empty replica -> cold-cache storm in market-data-service -> pricing-engine latency
redis-cache's primary (redis-cache-2) failed its health check (~13:22) and the cluster promoted replica redis-cache-3, which was still resyncing and therefore had an empty dataset. This wiped the quote cache (memory 70%->8%, hit ratio 96%->14%). market-data-service, which serves pricing-engine and reads quotes from redis-cache, had to rebuild every quote from the exchange feed on demand (cache miss storm), saturating its CPU (~90%) and raising its p99 to ~1.9s, which pushed pricing-engine p99 to >2s with deadline-exceeded errors.

**Causal chain**
1. redis-cache-2 primary fails health check (3 PING timeouts) at 13:22:10 [F3]
2. failover promotes still-resyncing replica redis-cache-3 with an empty dataset [F3]
3. quote cache is wiped: memory 70%->8% and hit_ratio 96%->14% [F4, F5]
4. market-data-service must rebuild quotes from exchange feed on demand ('cache miss storm' x289), CPU->90%, p99->1.9s [F6, F7]
5. pricing-engine quote calls hit deadline -> p99 >2s and error_rate 0.2%->4% [F1, F2]

**Supporting evidence**
  - `F1` ✔ pricing-engine p99 latency was ~60ms until 13:00, then jumped to avg 421ms (max 2228ms) in the 13:12 bucket, peaking at avg 2179ms / max 2309ms at 13:24, still elevated (~1170ms) at 14:48.
  - `F2` ✔ pricing-engine error_rate rose from ~0.2% baseline to ~4% from the 13:12 bucket onward, and its logs show 192 ERROR entries 'quote computation deadline exceeded: market-data-service took <n>ms' (first 13:23:57).
  - `F3` ✔ redis-cache primary node failed: log at 13:22:10 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down', and at 13:22:52 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)'
  - `F4` ✔ redis-cache memory_pct collapsed from ~70% (12:48, max 78.5) to avg 8.04%/max 9.34% at 13:24, consistent with the dataset being emptied by the empty-replica failover; it then recovers slowly (29.66% avg at 14:48).
  - `F5` ✔ redis-cache hit_ratio_pct dropped from ~96% baseline to avg 14.02%/max 16.22% at 13:24, then slowly climbed (45.78% avg at 14:48); evictions_per_min stayed flat ~3 and CPU ~22%, so the cache was not under memory pressure/evicting.
  - `F6` ✔ market-data-service logged 289 WARN 'cache miss storm: quote:<id> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' entries between 13:22:25 and 14:58:22, with rebuild times in the 1-8.7s range.
  - `F7` ✔ market-data-service latency_p99 jumped from ~40ms (through 13:00) to avg 352.7ms/max 1979ms at 13:12, peaked avg 1863ms at 13:24, and remains ~976ms at 14:48; its cpu_pct rose from ~44% to avg 89.99%/max 94.51% at 13:24.
  - `F11` ✔ redis-cache logged a client reconnect storm after failover: 'client reconnect storm: 15048 new connections in 10s' at 13:30:20, 29623 at 13:35:30, 92851 at 13:40:15; connected_clients stayed within normal ~260-310.
  - `F13` ✔ Dependency graph confirms pricing-engine -> market-data-service (http) -> redis-cache (cache, shared) and market-data-service -> exchange-gateway (http); redis-cache is a shared datastore used by both pricing-engine and market-data-service.
**Contradicting evidence**
  - (none)
**Assumptions**
  - market-data-service's quote path actually reads from the redis-cache quote keys (strongly implied by the 'not in redis-cache' messages)
  - pricing-engine does not itself read redis-cache for the failing calls; its latency is dominated by downstream market-data-service
  - the redis-cache node health-check failure was a genuine node fault rather than a network partition that later healed

### H2: Exchange feed / exchange-gateway degradation causing slow quote rebuilds
Slow quote rebuilds could be caused by the upstream exchange feed (exchange-gateway) being slow, not by the cache. Rejected: exchange-gateway latency_p99 is flat ~130ms across the entire window with no error deviation, and the rebuild cost is explained by cache misses (full-snapshot rebuild) rather than upstream slowness; market-data-service request rate is also flat.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ exchange-gateway was healthy: latency_p99 flat ~130ms across the whole window (12:00-14:48), no latency deviation, and only one benign log entry ('clock drift 38ms detected, resyncing with NTP').
  - `F6` ✔ market-data-service logged 289 WARN 'cache miss storm: quote:<id> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' entries between 13:22:25 and 14:58:22, with rebuild times in the 1-8.7s range.
**Rejected because:** exchange-gateway p99 flat ~130ms and error-free; rebuild latency is explained by the cache-miss storm, not upstream feed latency.

### H3: Bad deployment or config/feature-flag change to pricing-engine or market-data-service
A code or config change could have introduced the latency. Rejected: there were no deployments to pricing-engine, market-data-service or redis-cache (only an unrelated auth-service release), and request rates were flat, so nothing changed on the trading code/config path at onset.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ No deployments to pricing-engine, market-data-service or redis-cache in the window; the only deployment on 2026-04-09 was auth-service v7.2.0 (dep-5604, 'Session cookie hardening') at 11:30, unrelated to the trading path.
  - `F10` ✔ Request rates were flat across the incident: pricing-engine ~640 req/s, market-data-service ~950 req/s throughout, with no traffic surge at onset.
**Rejected because:** No deployments to the involved services (count 0); only an unrelated auth-service release. Config-change tooling timed out, so a flag change is not fully excluded, but nothing points to one.

### H4: Capacity saturation / resource exhaustion on market-data-service (traffic or memory)
Market-data-service CPU hit ~90%, which could suggest resource exhaustion. Rejected as the origin: request rate was flat (~950 req/s, no surge), restarts stayed 0, and the CPU spike is downstream of the cache misses (rebuilding from the feed), so it is a symptom of the cold cache, not the root cause.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F10` ✔ Request rates were flat across the incident: pricing-engine ~640 req/s, market-data-service ~950 req/s throughout, with no traffic surge at onset.
  - `F12` ✔ market-data-service restarts stayed at 0 for the entire window (no OOM/restart driven behavior).
  - `F6` ✔ market-data-service logged 289 WARN 'cache miss storm: quote:<id> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' entries between 13:22:25 and 14:58:22, with rebuild times in the 1-8.7s range.
**Rejected because:** No traffic surge, no restarts, and CPU rise is explained by the cache-miss rebuild work initiated by the redis failover.

## Facts (verified observations)
- pricing-engine p99 latency rose from ~60ms to >2s beginning in the 13:12 bucket. [F1]
- pricing-engine error_rate rose from ~0.2% to ~4% and it logged 192 'quote computation deadline exceeded: market-data-service took <n>ms' errors. [F2]
- redis-cache primary redis-cache-2 failed its health check at 13:22:10 and failover promoted an empty, still-resyncing replica redis-cache-3. [F3]
- redis-cache memory_pct fell from ~70% to ~8% and hit_ratio from ~96% to ~14% after failover. [F4, F5]
- market-data-service logged 289 'cache miss storm ... rebuilding from exchange feed snapshot' warnings with 1-8.7s rebuild times. [F6]
- market-data-service p99 rose to ~1.9s and CPU to ~90%. [F7]
- exchange-gateway was healthy with flat ~130ms p99. [F8]
- No deployments occurred to pricing-engine, market-data-service or redis-cache; only an unrelated auth-service release. [F9]
- Request rates for pricing-engine (~640/s) and market-data-service (~950/s) were flat through the incident. [F10]

## Assumptions (not verified)
- market-data-service's quote serving path reads quote keys from redis-cache (implied by 'not in redis-cache' messages).
- pricing-engine's failing quote calls are dominated by the market-data-service dependency rather than its own code path.
- The redis-cache health-check failure reflects a genuine node fault rather than a transient monitor blip that self-healed.
- Metric buckets are 12-minute averages, so onset is bracketed between 13:00 and 13:24 rather than pinned to a second.

## Timeline
- `2026-04-09T13:12:00Z` pricing-engine and market-data-service p99 latency begin to spike; pricing-engine error_rate rises toward 4% [F1, F2, F7]
- `2026-04-09T13:19:38Z` exchange-gateway logs a benign 38ms NTP clock resync (no latency impact) [F8]
- `2026-04-09T13:22:10Z` redis-cache-2 primary fails health check (3 consecutive PING timeouts), marked down [F3]
- `2026-04-09T13:22:25Z` market-data-service begins logging 'cache miss storm ... not in redis-cache, rebuilding from exchange feed snapshot' [F6]
- `2026-04-09T13:22:52Z` failover promotes replica redis-cache-3 to primary with an empty dataset (replica was resyncing) [F3]
- `2026-04-09T13:24:00Z` redis-cache memory 70%->8% and hit_ratio 96%->14%; market-data-service CPU ~90%, p99 ~1.9s; pricing-engine p99 peak ~2.2s [F4, F5, F7, F1]
- `2026-04-09T13:30:20Z` redis-cache client reconnect storm (15048 new connections in 10s), escalating to 92851 by 13:40 [F11]
- `2026-04-09T13:30:00Z` Incident INC-006 reported by on-call SRE []
- `2026-04-09T14:48:00Z` Partial recovery as cache warms: redis hit_ratio back to ~46%, pricing-engine p99 ~1170ms, market-data-service p99 ~976ms (still elevated) [F5, F1, F7]

## Recommended actions
- **[immediate]** Warm the redis-cache quote keyspace (preload/prime the hot quotes) to accelerate recovery, instead of waiting for organic re-population. -- Cache hit ratio is only ~46% at 14:48 and latency is still elevated; historic resolution for HIST-107 was cache warming. [F5, F1, F7]
- **[immediate]** Verify/fix redis replica sync before advertising a replica as promotable (require confirmed full resync so failover never promotes an empty dataset). -- The fault propagated because the promoted replica had an empty dataset while still resyncing. [F3, F4]
- **[short_term]** Investigate why redis-cache-2 primary failed its health check (node resource/infra fault, network partition) to prevent recurrence. -- The originating event is the primary health-check failure; root infra cause is still unknown. [F3]
- **[short_term]** Add request coalescing / single-flight and a grace fallback for quote rebuilds so a cold cache cannot amplify into a CPU-bound rebuild storm. -- Each miss triggers a full exchange-feed snapshot rebuild, saturating market-data-service CPU. [F6, F7]
- **[long_term]** Add alerting on redis-cache hit_ratio and memory_pct drop plus replica-sync state, and on market-data-service cache-miss-storm rate. -- Detection currently depends on the downstream pricing-engine deadline alerts, not the root cache signal. [F5, F2]

## Data gaps
- get_config_changes timed out on every attempt (tc-005, tc-007) and redis-cache INFO-level logs were unretrievable (tc-033 timeout), so a config/feature-flag change cannot be fully excluded as a contributing factor.
- The underlying reason why redis-cache-2 failed its health check (node hardware, network partition, OOM, or a control-plane fault) is not established; only the failover consequence is observed.
- The first data point of degradation is the 13:12 metric bucket (12-minute resolution); the exact onset second between 13:00 and 13:22 is not resolved.
- Deployment listing was initially partial (tc-004 flagged incomplete shards) though a retry returned a clean, if minimal, result; a very recent unrelated deployment cannot be 100% excluded.

## Investigation stats
- latency 95.89s, LLM calls 8, tokens 128307 (in 113961 / out 14346)
- tool calls 33 (5 failed, 1 partial, 10 recovered by retry), findings 13/13 verified
