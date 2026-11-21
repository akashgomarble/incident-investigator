# Incident investigation report: INC-002
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Portfolio page timeouts originate in portfolio-service, not the api-gateway. At 09:52 a cost-optimisation config change (cfg-2207, infra-automation) cut portfolio-service's datasource pool max_size from 50 to 5. The pool immediately saturated (total=5, active=5, idle=0, waiting 25-57), and requests could not acquire a JDBC connection within 3000ms, producing errors that surfaced as gateway p99 >4s and 504s. The suspected gateway HTTP/2 upgrade (dep-5220, 08:00) is contradicted: onset was 09:45/09:53, ~1h45m after deploy; order-service also received the h2 change but is unaffected; and the gateway itself has flat CPU/request rate. ledger-db is healthy. The only change matching onset is the pool config change.

## Probable root cause
**Config change cfg-2207 shrunk portfolio-service DB pool 50→5, causing pool exhaustion and upstream timeouts** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.93, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Config change cfg-2207 shrunk portfolio-service DB pool 50→5, causing pool exhaustion and upstream timeouts | config_change | portfolio-service | cfg-2207 | 0.93 (high) | active |
| 2 | H2: api-gateway HTTP/2 upgrade (dep-5220) to upstreams broke portfolio-service calls | bad_deployment | api-gateway | dep-5220 | 0.03 (low) | rejected |
| 3 | H3: ledger-db / shared datastore degradation slowed portfolio queries | infrastructure_failure | ledger-db | - | 0.02 (low) | rejected |

### H1: Config change cfg-2207 shrunk portfolio-service DB pool 50→5, causing pool exhaustion and upstream timeouts
An automated cost-optimisation config change (cfg-2207, 09:52) reduced portfolio-service's HikariCP datasource max_size from 50 to 5. With normal request rate (~560 req/s) the 5-connection pool saturated immediately (total=5, active=5, idle=0, waiting 25-57); connections were held for the full 3000ms acquire timeout, so requests queued and failed, portfolio-service p99 rose to ~4.2s, and the api-gateway surfaced these as upstream timeouts/504s (perceived as portfolio page timeout).

**Causal chain**
1. cfg-2207 applied at 09:52:13 sets db.pool.max_size=5 [F6, F7]
2. pool shrinks and saturates (db_pool_in_use pins at 5, total=5 active=5 idle=0 waiting>0) [F8, F5]
3. requests fail to acquire a connection within 3000ms, portfolio p99 → ~4.2s [F5, F4]
4. gateway reports upstream timeout portfolio-service /v1/portfolio/holdings -> 504, p99 >4s [F3, F1]

**Supporting evidence**
  - `F4` ✔ portfolio-service p99 latency rose identically: ~180ms until 09:30, 2079ms at 09:45, ~4180ms from 10:00.
  - `F5` ✔ portfolio-service errors are dominated by JDBC connection-acquire failures and Hikari pool exhaustion: 'could not acquire JDBC connection within 3000ms' (270x) and 'HikariPool-1 - Connection is not available ... (total=5, active=5, idle=0' (233x), first seen 09:53:19.
  - `F6` ✔ A config change cfg-2207 on portfolio-service changed db.pool.max_size from 50 to 5 at 09:52:00Z, author infra-automation, description 'Apply cost-optimisation profile small'.
  - `F7` ✔ portfolio-service's own log shows the config change applied at 09:52:13 and the first connection-acquire failure at 09:53:19.
  - `F8` ✔ portfolio-service db_pool_in_use was steady ~12 (max 13.3) until 09:30, dropped to 8.62 at 09:45 and then pinned at exactly 5.0 for every bucket from 10:00 onward.
  - `F9` ✔ portfolio-service request rate is flat (~560 req/s) across the window; no traffic surge triggered the failure.
  - `F10` ✔ portfolio-service cpu_pct is flat at ~36% throughout, ruling out CPU saturation.
  - `F13` ✔ ledger-db is healthy: cpu_pct flat ~30%, slow_queries_per_min flat ~1, connections_active slightly LOWER (~55 vs ~62), no failover or IOPS anomaly.
  - `F16` ✔ There were no portfolio-service deployments in the window (count 0).
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 09:52 config change took effect without a restart, so the pool resized in place (consistent with F8 dropping at the 09:45/10:00 buckets).

### H2: api-gateway HTTP/2 upgrade (dep-5220) to upstreams broke portfolio-service calls
The suspected cause: dep-5220 (08:00, 'enable HTTP/2 to upstreams' for portfolio-service and order-service) introduced an incompatibility or protocol-handling fault causing portfolio timeouts.

**Causal chain**
1. gateway deploy enables h2 to portfolio-service at 08:00 [F11]
2. h2 causes portfolio calls to time out *(assumed)*

**Supporting evidence**
  - `F1` ✔ api-gateway p99 latency was ~205-214ms until 09:30, then jumped to 2147ms avg at the 09:45 bucket and plateaued at ~4300ms from 10:00 onward.
  - `F3` ✔ Gateway logs are WARN-only and show 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' from 09:53:53 onward (252 occurrences); no gateway-internal errors.
**Contradicting evidence**
  - `F12` ✔ order-service, which also received the h2 upstream change, has flat error_rate (~0.2%) and flat p99 (~150ms) throughout the incident window.
  - `F14` ✔ api-gateway cpu_pct (~42%) and request_rate (~1246 req/s) are flat across the window; the gateway is not saturated.
  - `F5` ✔ portfolio-service errors are dominated by JDBC connection-acquire failures and Hikari pool exhaustion: 'could not acquire JDBC connection within 3000ms' (270x) and 'HikariPool-1 - Connection is not available ... (total=5, active=5, idle=0' (233x), first seen 09:53:19.
  - `F7` ✔ portfolio-service's own log shows the config change applied at 09:52:13 and the first connection-acquire failure at 09:53:19.
  - `F8` ✔ portfolio-service db_pool_in_use was steady ~12 (max 13.3) until 09:30, dropped to 8.62 at 09:45 and then pinned at exactly 5.0 for every bucket from 10:00 onward.
**Rejected because:** Onset is ~1h45m AFTER the deploy (09:45/09:53 vs 08:00) and the failure aligns to the 09:52 config change instead (F7). order-service received the identical h2 change and is completely unaffected (F12), and the gateway itself shows no saturation or internal errors (F14, F3). Portfolio errors are explicit JDBC/Hikari pool exhaustion, not protocol errors (F5).

### H3: ledger-db / shared datastore degradation slowed portfolio queries
A ledger-db infrastructure problem (failover, IOPS throttle) made portfolio queries slow, exhausting connections.

**Causal chain**
1. ledger-db slows or fails over *(assumed)*

**Supporting evidence**
  - `F4` ✔ portfolio-service p99 latency rose identically: ~180ms until 09:30, 2079ms at 09:45, ~4180ms from 10:00.
**Contradicting evidence**
  - `F13` ✔ ledger-db is healthy: cpu_pct flat ~30%, slow_queries_per_min flat ~1, connections_active slightly LOWER (~55 vs ~62), no failover or IOPS anomaly.
  - `F5` ✔ portfolio-service errors are dominated by JDBC connection-acquire failures and Hikari pool exhaustion: 'could not acquire JDBC connection within 3000ms' (270x) and 'HikariPool-1 - Connection is not available ... (total=5, active=5, idle=0' (233x), first seen 09:53:19.
  - `F8` ✔ portfolio-service db_pool_in_use was steady ~12 (max 13.3) until 09:30, dropped to 8.62 at 09:45 and then pinned at exactly 5.0 for every bucket from 10:00 onward.
**Rejected because:** ledger-db cpu (~30%), slow_queries (~1/min) and connections are all flat/healthy; connections_active even fell (F13). Failures are client-side pool exhaustion (total=5), and the pool shrank rather than being blocked by a slow DB (F5, F8).

## Facts (verified observations)
- api-gateway p99 jumped from ~210ms to ~4.3s at the 09:45-10:00 window; error rate rose from 0.2% to ~7.2%. [F1, F2]
- Gateway logs show only upstream timeouts to portfolio-service returning 504, no gateway-internal faults. [F3]
- portfolio-service p99 rose in lockstep with the gateway; its errors are JDBC connection-acquire failures and HikariPool exhaustion (total=5, active=5, idle=0, waiting up to 57). [F4, F5]
- A config change cfg-2207 changed portfolio-service db.pool.max_size from 50 to 5 at 09:52:00. [F6]
- portfolio-service log shows the config applied at 09:52:13 and the first connection failure at 09:53:19. [F7]
- portfolio-service db_pool_in_use dropped from ~12 to exactly 5 after the change. [F8]
- portfolio-service request rate and CPU are flat; no traffic or CPU trigger. [F9, F10]
- The only deployment was api-gateway dep-5220 at 08:00 enabling HTTP/2 to portfolio-service AND order-service. [F11]
- order-service (identical h2 change) is completely unaffected; gateway CPU/request-rate flat. [F12, F14]
- ledger-db metrics are healthy; no datastore-side fault. [F13]
- No portfolio-service deployments occurred. [F16]

## Assumptions (not verified)
- cfg-2207 took effect in place without a restart (F8 shows the pool dropping precisely at the change, consistent with in-place resize).
- The gateway 504s are the downstream symptom of portfolio-service failures, not an independent gateway fault (supported by F3, F5).

## Timeline
- `2026-03-10T08:00:00Z` api-gateway dep-5220 (v9.3.0) deployed, enabling HTTP/2 to portfolio-service and order-service. No latency impact (p99 stays ~210ms for ~1h45m). [F11, F1]
- `2026-03-10T09:52:00Z` Config change cfg-2207 applies cost-optimisation profile: portfolio-service db.pool.max_size 50 -> 5. [F6]
- `2026-03-10T09:52:13Z` portfolio-service logs 'config change cfg-2207 applied: db.pool.max_size=5'. [F7]
- `2026-03-10T09:53:19Z` First 'could not acquire JDBC connection within 3000ms' error in portfolio-service. [F7, F5]
- `2026-03-10T09:53:53Z` First 'upstream timeout portfolio-service -> 504' at api-gateway. [F3]
- `2026-03-10T10:05:00Z` Incident INC-002 reported (portfolio page timing out, gateway p99 high). [F1]
- `2026-03-10T10:00:00Z` portfolio-service db_pool_in_use pins at 5.0; gateway/portfolio p99 plateau ~4.3s. [F8, F1]

## Recommended actions
- **[immediate]** Immediately revert cfg-2207: restore portfolio-service db.pool.max_size to 50 (or validate a correctly-sized value) and confirm pool recovery. -- The pool shrink is the direct cause; restoring pool size should clear the exhaustion and recover p99. [F6, F7, F8]
- **[immediate]** Do NOT roll back gateway dep-5220 as the primary remediation; keep it in place unless independent evidence emerges. -- The h2 upgrade deploy predates onset by ~1h45m, is flat on order-service, and the gateway is not saturated — rolling it back would not fix the fault and adds risk. [F11, F12, F14]
- **[short_term]** Add a guardrail/validation on datasource pool-size changes (minimum bound and canary) before infra-automation applies cost-optimisation profiles. -- Automated config overrode the pool to an unsafe value, mirroring HIST-115. [F6, F17]
- **[short_term]** Add alerting on Hikari pool saturation (active==total, waiting>0) and on client-side connection-acquire failures, distinct from generic 5xx. -- The exhaustion was visible in metrics/logs before the user-facing report and could have been detected faster. [F5, F8]
- **[long_term]** Audit other services for the same 'small' cost-optimisation profile being applied to datasources. -- Same automation could have silently shrunk pools elsewhere. [F6]

## Data gaps
- No confirmation of the fix outcome: we only have data through 11:59; cannot verify recovery after reverting cfg-2207.
- Exact mechanism of the pool resize (whether HikariCP resized in place or on refresh) is inferred from the metric step, not directly logged.
- No per-endpoint breakdown confirming that only /v1/portfolio/* paths on portfolio-service hit the DB pool, though all observed failures are on /v1/portfolio/holdings.

## Investigation stats
- latency 52.41s, LLM calls 6, tokens 98003 (in 89242 / out 8761)
- tool calls 26 (1 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
