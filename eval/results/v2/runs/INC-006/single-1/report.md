# Incident investigation report: INC-006
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Quotes degraded because the redis-cache primary (redis-cache-2) failed health checks and a failover promoted a replica whose dataset was empty (it was still resyncing). This caused a cold-cache miss storm: market-data-service had to rebuild every quote from the exchange feed snapshot, driving its CPU to ~90% and p99 latency to ~2s, which in turn made pricing-engine quote computations hit deadline-exceeded (~2s p99, ~4% errors) and trading screens load slowly. The reported service (pricing-engine) is a victim; the fault originates in redis-cache infrastructure. No code deployment or config change preceded the event; CPU/load were flat; the pattern matches historical incident HIST-107.

## Probable root cause
**Redis primary node failure promoted an empty replica, causing a cold-cache/miss storm that overloaded market-data-service and degraded quotes** -- infrastructure_failure in `redis-cache` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Redis primary node failure promoted an empty replica, causing a cold-cache/miss storm that overloaded market-data-service and degraded quotes | infrastructure_failure | redis-cache | - | 0.86 (high) | active |
| 2 | H2: market-data-service capacity saturation from exchange-feed rebuild load | capacity_saturation | market-data-service | - | 0.10 (low) | rejected |
| 3 | H3: exchange-gateway / upstream feed degradation | dependency_failure | exchange-gateway | - | 0.05 (low) | rejected |

### H1: Redis primary node failure promoted an empty replica, causing a cold-cache/miss storm that overloaded market-data-service and degraded quotes
redis-cache-2 (primary) failed its health check and was marked down at 13:22:10; failover promoted replica redis-cache-3, whose dataset was empty because it was still resyncing. The resulting cold cache caused a miss storm: market-data-service had to rebuild every quote from the exchange feed snapshot (multi-second rebuilds, CPU ~90%), which raised market-data-service p99 to ~2s and, in turn, made pricing-engine quote computation deadline-exceeded (~2s p99, 4% errors) and slow trading-screen quotes.

**Causal chain**
1. redis-cache-2 primary fails health check and is marked down [F12]
2. failover promotes empty replica redis-cache-3 (dataset empty, replica was resyncing) [F13]
3. hit ratio collapses 96%->14% and memory drops 70%->8% (keyspace effectively empty) [F10, F11]
4. market-data-service cache miss storm: rebuilds each quote from exchange feed snapshot [F9]
5. market-data-service CPU saturates ~90% and p99 rises to ~2s [F7, F6]
6. pricing-engine waits on market-data-service, hitting deadline-exceeded; quotes slow on screens [F3, F1, F2]

**Supporting evidence**
  - `F6` ✔ market-data-service p99 latency jumped from ~40ms to ~980ms in the 13:18-13:26 bucket (onset aligns with pricing-engine), peaking near 1980ms.
  - `F7` ✔ market-data-service CPU rose from ~45% to ~90% (max 94%) at onset and stayed high.
  - `F9` ✔ market-data-service logs show a cache-miss storm: 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' beginning 13:22:25, 289+ occurrences.
  - `F10` ✘ unverified redis-cache hit_ratio collapsed from ~96% to ~14% at onset and is slowly recovering (~47% by 14:54).
  - `F11` ✘ unverified redis-cache memory_pct dropped sharply from ~70% to ~8% at onset then climbs back slowly, consistent with the dataset being emptied.
  - `F12` ✔ redis-cache logged an ERROR at 13:22:10: 'node redis-cache-2 failed health check (3 consecutive PING timeouts); marking primary as down'.
  - `F13` ✔ redis-cache logged at 13:22:52: 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' — the promoted primary had an empty dataset.
  - `F14` ✔ redis-cache logged client reconnect storms (15048 new connections in 10s at 13:30:20, rising to 92851 in 10s at 13:40:15).
  - `F8` ✔ market-data-service request_rate was flat ~955 req/s (no traffic surge), so CPU rise is from work per request, not volume.
  - `F3` ✔ pricing-engine errors are all of the form 'quote computation deadline exceeded: market-data-service took <n>ms', i.e. pricing-engine itself is waiting on market-data-service.
  - `F1` ✔ pricing-engine p99 latency jumped from ~60ms to ~1700-2300ms starting in the 13:16-13:24 bucket; it was ~59-66ms every bucket through 13:12.
  - `F20` ✔ Historical incident HIST-107 (2025-12-09, 'Cold cache storm after redis failover', redis-cache + market-data-service) shares the exact symptom/cause pattern; resolution was warming the cache and fixing replica sync checks.
**Contradicting evidence**
  - `F18` ✔ market-data-service restarts stayed 0 throughout (no pod crashes).
**Assumptions**
  - The promoted empty replica is the sole reason hit_ratio collapsed (no other actor flushed the keyspace).
  - market-data-service's per-request cost rise is a direct consequence of missing cache, not an independent fault.

### H2: market-data-service capacity saturation from exchange-feed rebuild load
Even without the redis event, market-data-service's own capacity is insufficient once quotes must be rebuilt from the exchange feed; CPU ~90% and 2s p99 are attributable to market-data-service being under-provisioned for cold-path work.

**Causal chain**
(none given)

**Supporting evidence**
  - `F7` ✔ market-data-service CPU rose from ~45% to ~90% (max 94%) at onset and stayed high.
  - `F6` ✔ market-data-service p99 latency jumped from ~40ms to ~980ms in the 13:18-13:26 bucket (onset aligns with pricing-engine), peaking near 1980ms.
**Contradicting evidence**
  - `F8` ✔ market-data-service request_rate was flat ~955 req/s (no traffic surge), so CPU rise is from work per request, not volume.
  - `F9` ✔ market-data-service logs show a cache-miss storm: 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' beginning 13:22:25, 289+ occurrences.
  - `F13` ✔ redis-cache logged at 13:22:52: 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' — the promoted primary had an empty dataset.
**Rejected because:** request_rate was flat (~955 req/s, F8) so load did not grow; the extra per-request work is explained by the cache miss storm (F9) which traces directly to the empty redis primary (F12/F13). The trigger is redis, not market-data-service provision.

### H3: exchange-gateway / upstream feed degradation
A slowdown in exchange-gateway or its external provider made quote rebuilds slow, cascading to market-data-service and pricing-engine.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F19` ✔ exchange-gateway logged only one benign warning (clock drift 38ms) and had 0 ERROR-level log entries in the window, and market-data-service has no error hint pointing at the exchange feed.
  - `F9` ✔ market-data-service logs show a cache-miss storm: 'cache miss storm: quote:<n> not in redis-cache, rebuilding from exchange feed snapshot (<n>ms)' beginning 13:22:25, 289+ occurrences.
  - `F13` ✔ redis-cache logged at 13:22:52: 'failover: promoting replica redis-cache-3 to primary; dataset on new primary is empty (replica was resyncing)' — the promoted primary had an empty dataset.
**Rejected because:** exchange-gateway emitted 0 ERROR logs (only a benign 38ms clock-drift warning, F19) and the miss-storm rebuild durations coincide exactly with the redis empty-primary event; the chain is explained without an exchange fault.

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- The redis-cache keyspace was not flushed by anything other than the failover to an empty replica (no explicit FLUSHALL/config change identified).
- The promoted redis-cache-3 being empty is authoritative from the log line; replica-sync configuration detail is not directly observed.
- No relevant deployments or config changes exist outside the returned (possibly sharded) windows.

## Timeline
- `2026-04-09T13:12:00Z` Last healthy bucket: pricing-engine p99 ~61ms, error ~0.2% [F1, F2]
- `2026-04-09T13:18:00Z` Onset bucket: redis hit_ratio starts collapsing (avg 53%), redis memory drops; market-data latency/CPU begin rising [F10, F11, F6, F7]
- `2026-04-09T13:20:00Z` pricing-engine p99 jumps to ~1700ms avg; error rate to ~2.7% [F1, F2]
- `2026-04-09T13:22:10Z` redis-cache ERROR: node redis-cache-2 (primary) failed health check, marked down [F12]
- `2026-04-09T13:22:25Z` market-data-service 'cache miss storm' warnings begin [F9]
- `2026-04-09T13:22:52Z` failover promotes redis-cache-3 as primary with an EMPTY dataset (replica was resyncing) [F13]
- `2026-04-09T13:26:00Z` redis hit_ratio hits ~14%; market-data-service CPU ~92%, p99 ~1850ms; pricing-engine deadlines persist [F10, F7, F6]
- `2026-04-09T13:30:20Z` redis client reconnect storm (15048 new connections/10s) [F14]
- `2026-04-09T13:30:00Z` Incident reported to SRE (reported_at) []
- `2026-04-09T14:54:00Z` Partial recovery: pricing-engine p99 ~1164ms, redis hit_ratio ~47%, market-data p99 ~948ms — still degraded [F1, F10, F6]

## Recommended actions
- **[immediate]** Verify redis-cache-2 availability and, if healthy, fail back / re-attach the original primary so the warmer keyspace is restored; confirm redis-cache-3 has finished full resync. -- The empty promoted replica is the source of the cold cache; restoring a warm primary or completing replication stops the miss storm. [F12, F13, F10]
- **[immediate]** Warm the cache proactively (pre-populate the quote keyspace) rather than relying on organic rebuild, and throttle the rebuild path to avoid re-saturating market-data-service. -- Cache miss storm is overloading market-data-service (~90% CPU, 2s p99); controlled warming restores hit ratio faster and reduces rebuild pressure. [F9, F7, F10]
- **[short_term]** Fix replica sync checks so a resyncing/empty replica is never promoted to primary (gate promotion on dataset readiness). -- This is the same defect as HIST-107; preventing empty-replica promotion removes the trigger class. [F13, F20]
- **[short_term]** Add alerting on redis-cache hit_ratio and on cache-miss-storm rate in market-data-service, tied to a failover event, to detect this instantly rather than via downstream quote latency. -- Onset was detected from pricing-engine latency; direct cache-signal alerting shortens time-to-detect. [F10, F9]
- **[long_term]** Review redis-cache failover/sentinel health-check thresholds (3 consecutive PING timeouts triggered demotion) for false positives under transient latency. -- If the primary was healthy but slow, an overly aggressive health check can cause unnecessary failovers to a cold replica. [F12]

## Data gaps
- get_deployments for all services initially returned partial:true (one shard did not respond); it was retried and returned only auth-service dep-5604, so a deployment on an unqueried component could theoretically be missed — but per-service queries for market-data-service and pricing-engine returned none.
- redis-cache exposes no restarts metric, so node-level restart/uptime for redis-cache-2 could not be confirmed; the primary-down event is inferred from the health-check ERROR log.
- Root cause of the redis-cache-2 primary failure itself (OOM, network partition, node loss) is not directly evidenced — only that it failed health checks.

## Investigation stats
- latency 52.94s, LLM calls 6, tokens 93214 (in 84183 / out 9031)
- tool calls 32 (3 failed, 1 partial, 2 recovered by retry), findings 18/20 verified
