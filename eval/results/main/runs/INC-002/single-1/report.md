# Incident investigation report: INC-002
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The faulty change is cfg-2207 at 09:52, an automated cost-optimisation profile that shrank portfolio-service's DB connection pool from 50 to 5. With steady ~565 req/s the 5-connection pool immediately saturated (db_pool_in_use pinned at exactly 5, HikariPool total=5/active=5/idle=0 with waiting queues up to 57 and 3000ms acquire timeouts). This drove portfolio-service p99 from ~180ms to ~4.1s and 5xx to ~22%, which surfaced at api-gateway as upstream 504 timeouts on /v1/portfolio/holdings (gateway p99 ~4.3s, 5xx ~7%). The reporter's suspicion of the 08:00 gateway HTTP/2 deployment is not supported: it preceded onset by ~1h50m, gateway CPU/memory stayed flat, and no gateway-internal errors appeared. Root-cause category: config_change; fault origin: portfolio-service.

## Probable root cause
**Config change cfg-2207 capped portfolio-service's DB pool at 5, exhausting it** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.92, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Config change cfg-2207 capped portfolio-service's DB pool at 5, exhausting it | config_change | portfolio-service | cfg-2207 | 0.92 (high) | active |
| 2 | H2: api-gateway dep-5220 (HTTP/2 upgrade) caused the timeouts | bad_deployment | api-gateway | dep-5220 | 0.06 (low) | rejected |
| 3 | H3: Backing dependency (ledger-db / market-data) failure stalled portfolio queries | dependency_failure | ledger-db | - | 0.02 (low) | rejected |

### H1: Config change cfg-2207 capped portfolio-service's DB pool at 5, exhausting it
At 09:52 an automated cost-optimisation profile 'small' set portfolio-service db.pool.max_size from 50 to 5. With steady ~565 req/s, only 5 DB connections cannot serve concurrent /v1/portfolio/holdings requests; HikariPool queues (waiting up to 57) and requests fail after the 3000ms acquisition timeout, raising portfolio-service latency/5xx and propagating upstream timeouts to api-gateway as 504s.

**Causal chain**
1. cfg-2207 sets portfolio-service db.pool.max_size 50 -> 5 at 09:52 [F1]
2. db_pool_in_use falls to a hard ceiling of 5 from 10:00 onward [F2]
3. HikariPool exhausts: total=5, active=5, idle=0, waiting 25-57; acquire timeouts at 3000ms [F3]
4. portfolio-service p99 180ms -> 3031ms and error_rate 0.2% -> 13.5%/22% at the 09:50 bucket [F5]
5. api-gateway logs upstream 504 timeouts to portfolio-service starting 09:53:53 [F4]
6. api-gateway p99 206ms -> 3120ms, error_rate 0.22% -> 7.2% [F6]

**Supporting evidence**
  - `F1` ✔ Config change cfg-2207 reduced portfolio-service datasource pool max_size from 50 to 5 at 2026-03-10T09:52:00Z, and portfolio-service logged it applied at 09:52:13.
  - `F2` ✔ portfolio-service db_pool_in_use was ~12 connections through 09:40, then dropped and became pinned at exactly 5 (avg=5.0, max=5) from 10:00 onward.
  - `F3` ✔ portfolio-service began logging HikariPool exhaustion errors at 09:54:29 with the pool capped at total=5, active=5, idle=0 and waiting queues in the 25-57 range; the same 'could not acquire JDBC connection within 3000ms' errors begin at 09:53:19.
  - `F4` ✔ api-gateway began logging 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' at 09:53:53, immediately after the pool change; these are the only new gateway warnings in the window.
  - `F5` ✔ portfolio-service latency_p99 jumped from ~180ms (through 09:40) to ~3031ms at the 09:50 bucket, and error_rate from ~0.2% to 13.5% at 09:50 then ~22% sustained.
  - `F6` ✔ api-gateway latency_p99 jumped from ~206ms through 09:40 to ~3120ms at the 09:50 bucket, and error_rate from ~0.22% to 4.44% then ~7.2% sustained.
  - `F11` ✔ portfolio-service CPU (~36%) and restart count (0) remained flat/zero across the window.
  - `F12` ✔ portfolio-service depends on ledger-db (shared), market-data-service and redis-cache; historical incident HIST-115 describes an identical symptom from a datasource pool size overridden by a wrong profile.
**Contradicting evidence**
  - (none)
**Assumptions**
  - db.pool.max_size=5 is the effective runtime pool size for this HikariPool-1 datasource
  - no other component changed simultaneously (no deployments in window other than dep-5220)

### H2: api-gateway dep-5220 (HTTP/2 upgrade) caused the timeouts
The reporter suspects the 08:00 envoy/HTTP-2 gateway upgrade. However onset is at 09:50-09:53, ~1h50m after the deploy, gateway CPU/memory are flat, and the failing slogans point to portfolio-service pool exhaustion, not gateway-side faults.

**Causal chain**
1. dep-5220 went live at 08:00 [F7]
2. no gateway errors between 08:00 and 09:50 [F10]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F1` ✔ Config change cfg-2207 reduced portfolio-service datasource pool max_size from 50 to 5 at 2026-03-10T09:52:00Z, and portfolio-service logged it applied at 09:52:13.
  - `F3` ✔ portfolio-service began logging HikariPool exhaustion errors at 09:54:29 with the pool capped at total=5, active=5, idle=0 and waiting queues in the 25-57 range; the same 'could not acquire JDBC connection within 3000ms' errors begin at 09:53:19.
  - `F7` ✔ The only api-gateway deployment was dep-5220 (envoy filter chain / HTTP/2 to upstreams) at 2026-03-10T08:00:00Z, ~1h50m before the 09:50 onset, and it is still live with no rollback.
  - `F8` ✔ api-gateway CPU (mean ~42%) and memory (mean ~58%) remained flat across the whole window, including after onset.
  - `F10` ✔ portfolio-service logged no WARN/ERROR timeouts before 09:53 (zero matches for 'timeout' 08:00-09:55), and api-gateway had zero warnings between its 08:00 deploy and 09:50 (only a single unrelated 08:28 retry).
**Rejected because:** Deploy at 08:00 precedes onset by ~1h50m with no symptoms in between; gateway CPU/memory flat; no gateway-side error messages; root symptom (HikariPool total=5) matches cfg-2207 exactly.

### H3: Backing dependency (ledger-db / market-data) failure stalled portfolio queries
A downstream datastore failure could stall portfolio-service requests. But the error is a client-side pool acquisition timeout (HikariPool total=5, active=5), not DB query timeouts; latency onset coincides with the pool-size change, and no dependency error logs appear.

**Causal chain**
1. portfolio-service calls ledger-db and market-data-service [F12]
2. those calls hang, stalling requests *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F2` ✔ portfolio-service db_pool_in_use was ~12 connections through 09:40, then dropped and became pinned at exactly 5 (avg=5.0, max=5) from 10:00 onward.
  - `F3` ✔ portfolio-service began logging HikariPool exhaustion errors at 09:54:29 with the pool capped at total=5, active=5, idle=0 and waiting queues in the 25-57 range; the same 'could not acquire JDBC connection within 3000ms' errors begin at 09:53:19.
  - `F5` ✔ portfolio-service latency_p99 jumped from ~180ms (through 09:40) to ~3031ms at the 09:50 bucket, and error_rate from ~0.2% to 13.5% at 09:50 then ~22% sustained.
**Rejected because:** No ledger-db/market-data error evidence; failure mode is local pool exhaustion with a pool capped at 5, not downstream latency; onset matches cfg-2207.

## Facts (verified observations)
- cfg-2207 set portfolio-service db.pool.max_size from 50 to 5 at 09:52:00Z. [F1]
- portfolio-service db_pool_in_use was pinned at exactly 5 connections from 10:00 onward, down from ~12. [F2]
- portfolio-service HikariPool exhausted with total=5, active=5, idle=0 and pending waiters from 09:54:29. [F3]
- portfolio-service p99 and error_rate spiked at the 09:50 bucket coincident with the pool change. [F5]
- api-gateway 504 upstream timeouts to portfolio-service began 09:53:53 and are its only new warnings. [F4]
- The gateway HTTP/2 deploy predates the onset by ~1h50m with no symptoms in between. [F7, F10]
- Gateway CPU/memory and portfolio-service CPU/restarts stayed flat across the incident. [F8, F11]
- Request traffic was flat (~560-570 req/s), so there was no load surge. [F9]

## Assumptions (not verified)
- cfg-2207's max_size=5 is the effective runtime limit for the HikariPool-1 datasource
- No other simultaneous change contributed; dep-5220 is the only deployment in the window
- The gateway 504s are caused by, not merely correlated with, portfolio-service slowness (same error path /v1/portfolio/holdings)

## Timeline
- `2026-03-10T08:00:00Z` api-gateway deploy dep-5220 (envoy 1.30, HTTP/2 to upstreams) goes live; no symptoms follow [F7, F10]
- `2026-03-10T09:52:00Z` Config change cfg-2207 reduces portfolio-service db.pool.max_size 50 -> 5 [F1]
- `2026-03-10T09:52:13Z` portfolio-service logs 'config change cfg-2207 applied: db.pool.max_size=5' [F1]
- `2026-03-10T09:50:00Z` portfolio-service and api-gateway p99/error_rate spike in the 09:50 metric bucket [F5, F6]
- `2026-03-10T09:53:19Z` First portfolio-service 'could not acquire JDBC connection within 3000ms' error [F3]
- `2026-03-10T09:53:53Z` First api-gateway 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' [F4]
- `2026-03-10T09:54:29Z` HikariPool exhaustion: 'total=5, active=5, idle=0, waiting=43' [F3]
- `2026-03-10T10:00:00Z` db_pool_in_use pinned at 5; portfolio-service error_rate sustained ~22%, gateway ~7% [F2, F5, F6]
- `2026-03-10T10:05:00Z` Incident INC-002 reported against api-gateway []

## Recommended actions
- **[immediate]** Revert cfg-2207 / restore portfolio-service db.pool.max_size to 50 (or an appropriately sized value) immediately -- The pool cap of 5 is the direct cause of connection starvation and the 504s; restoring capacity should clear the incident. [F1, F2, F3]
- **[short_term]** Add a guardrail/validation so cost-optimisation profiles cannot shrink a production datasource pool below a safe floor without review -- The change came from an automated 'small' profile (infra-automation) and was applied without gating. [F1, F12]
- **[short_term]** Add alerting on db_pool_in_use approaching max and on HikariPool waiting-queue depth -- Pool saturation was visible in metrics at 10:00 but only reported as a gateway symptom. [F2, F3]
- **[long_term]** Review the cost-optimisation profile automation and require canary + rollback for datasource tuning -- Prevents recurrence of pool misconfiguration (matches HIST-115). [F12]

## Data gaps
- No direct evidence of who/what triggered infra-automation to apply the 'small' cost-optimisation profile at 09:52 (change metadata only shows author 'infra-automation').
- Config-change list was for the whole fleet; not verified that no non-listed config change to ledger-db/market-data occurred.
- ledger-db and market-data-service telemetry not queried directly; ruled out only by absence of downstream error logs and by error semantics.

## Investigation stats
- latency 48.97s, LLM calls 5, tokens 67557 (in 60192 / out 7365)
- tool calls 19 (0 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
