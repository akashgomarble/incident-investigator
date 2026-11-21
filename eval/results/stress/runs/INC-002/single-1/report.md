# Incident investigation report: INC-002
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Portfolio page timeouts and gateway 504s were caused by a config change (cfg-2207) at 09:52:13 that cut portfolio-service's datasource HikariCP max pool size from 50 to 5. The pool (total=5) saturated immediately under normal ~565 req/s load, producing "could not acquire JDBC connection" and HikariPool timeout errors from 09:53:19, which propagated as 3-4.5s upstream timeouts and 504s at api-gateway. The gateway HTTP/2 upgrade (dep-5220, 08:00) is a red herring: gateway latency was flat at ~206ms for 1h44m after the deploy and only jumped at ~09:44-09:48, coinciding with the pool config change (applied 09:52), not the deploy.

## Probable root cause
**Config change cut portfolio-service DB pool to 5 connections, exhausting it under normal load** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Config change cut portfolio-service DB pool to 5 connections, exhausting it under normal load | config_change | portfolio-service | cfg-2207 | 0.90 (high) | active |
| 2 | H2: Gateway HTTP/2 upgrade (dep-5220) introduced the fault | bad_deployment | api-gateway | dep-5220 | 0.05 (low) | rejected |
| 3 | H3: ledger-db infrastructure/IO fault (historical pattern HIST-102/HIST-110) | infrastructure_failure | ledger-db | - | 0.05 (low) | rejected |

### H1: Config change cut portfolio-service DB pool to 5 connections, exhausting it under normal load
cfg-2207 (infra-automation) applied a 'cost-optimisation profile small' at 09:52, reducing portfolio-service datasource HikariCP max_size from 50 to 5. Under steady ~565 req/s the 5-connection pool saturated (active=5, idle=0, waiting 25-57), requests blocked up to 3000ms and failed to acquire connections, driving portfolio latency to ~4.2s and 504s at api-gateway. Onset aligns with the config-change time, not the 08:00 gateway deploy.

**Causal chain**
1. cfg-2207 applied, db.pool.max_size 50->5 at 09:52:13 [F2, F3]
2. pool capped at 5 connections (db_pool_in_use avg=5.0) [F4]
3. 5 connections exhausted at ~565 req/s -> HikariPool waiting=43, acquire timeout 3000ms [F5, F10]
4. portfolio requests fail/slow -> 504 upstream timeouts at gateway [F6, F7]
5. gateway p99 and 5xx spike [F1, F8]

**Supporting evidence**
  - `F1` ✔ api-gateway p99 latency was normal (~205-207ms avg) from 09:00 through 09:40, then jumped to avg 1753ms / max 4418ms in the 09:48 bucket and stayed ~4300ms through 11:59.
  - `F2` ✔ A config change cfg-2207 for portfolio-service set db.pool.max_size from 50 to 5 at 2026-03-10T09:52:00Z (author infra-automation, 'cost-optimisation profile small').
  - `F3` ✔ portfolio-service log confirms the config applied at 09:52:13: 'config change cfg-2207 applied: db.pool.max_size=5'.
  - `F4` ✔ portfolio-service db_pool_in_use dropped from ~12 connections (08:00-09:40) to exactly 5.0 from 10:00 onward, matching the new max_size=5.
  - `F5` ✔ portfolio-service HikariPool errors show total=5, active=5, idle=0, waiting=25-57, e.g. 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' starting 09:54:29.
  - `F6` ✔ portfolio-service request errors begin at 09:53:19 with 'could not acquire JDBC connection within 3000ms'; 264 ERROR entries and top pattern count 142 for that message.
  - `F7` ✔ api-gateway logs show only 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' warnings (133 of 134 matches), first at 09:53:53 — the 504s are gateway-side timeouts waiting on portfolio-service.
  - `F8` ✔ api-gateway 5xx error_rate rose from ~0.2% (09:00-09:40) to avg 2.07%/max 7.74% in the 09:48 bucket and ~7.2% thereafter.
  - `F10` ✔ portfolio-service request_rate stayed flat ~560-585 req/s across the entire window (08:00-11:59), so load did not increase.
  - `F12` ✔ portfolio-service 5xx error_rate held ~0.2% through the 09:40 bucket then jumped to avg 13.5%/max 24.02% in the 09:50 bucket and ~22% afterward.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 09:44 latency uptick and 09:52 config apply are the same incident onset (config propagation/jitter explains ~1 min gap between 09:52 apply and 09:53 first error).
  - Pool size 5 is insufficient for the observed steady request rate; 50 was sufficient.

### H2: Gateway HTTP/2 upgrade (dep-5220) introduced the fault
The api-gateway v9.3.0 deploy at 08:00 enabling HTTP/2 to upstreams (portfolio-service, order-service) caused upstream timeouts and 504s.

**Causal chain**
1. dep-5220 at 08:00 changes upstream protocol [F9]
2. gateway degrades / 504s appear [F7, F8]

**Supporting evidence**
  - `F7` ✔ api-gateway logs show only 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504' warnings (133 of 134 matches), first at 09:53:53 — the 504s are gateway-side timeouts waiting on portfolio-service.
  - `F8` ✔ api-gateway 5xx error_rate rose from ~0.2% (09:00-09:40) to avg 2.07%/max 7.74% in the 09:48 bucket and ~7.2% thereafter.
**Contradicting evidence**
  - `F1` ✔ api-gateway p99 latency was normal (~205-207ms avg) from 09:00 through 09:40, then jumped to avg 1753ms / max 4418ms in the 09:48 bucket and stayed ~4300ms through 11:59.
  - `F5` ✔ portfolio-service HikariPool errors show total=5, active=5, idle=0, waiting=25-57, e.g. 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' starting 09:54:29.
  - `F6` ✔ portfolio-service request errors begin at 09:53:19 with 'could not acquire JDBC connection within 3000ms'; 264 ERROR entries and top pattern count 142 for that message.
**Rejected because:** Timing contradicts: gateway p99 and error_rate were flat/normal (~206ms, 0.2% 5xx) for 1h44m after the 08:00 deploy and only degraded at ~09:44-09:48; the 504s are specifically 'upstream timeout portfolio-service' driven by portfolio pool exhaustion; gateway CPU was flat ~42%. No rollback evidence ties recovery to the deploy.

### H3: ledger-db infrastructure/IO fault (historical pattern HIST-102/HIST-110)
A ledger-db primary failover or IOPS throttling caused portfolio-service DB connection failures and downstream timeouts.

**Causal chain**
1. ledger-db degrades *(assumed)*
2. portfolio DB connections fail [F6]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F5` ✔ portfolio-service HikariPool errors show total=5, active=5, idle=0, waiting=25-57, e.g. 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=5, active=5, idle=0, waiting=43)' starting 09:54:29.
  - `F13` ✔ ledger-db produced zero log matches (WARN+) for 06:00-11:59, and the DB metric path is described as a shared postgres with telemetry; no DB-side error evidence found.
**Rejected because:** ledger-db emitted zero WARN+ logs for 06:00-11:59, and the portfolio errors are client-side connection-acquire timeouts from a HikariPool of size exactly 5, not refused/errored DB connections. Historical similarity is only a lead, not evidence.

## Facts (verified observations)
- api-gateway p99 was ~206ms until 09:40, jumped to max 4418ms at 09:48 and stayed ~4300ms. [F1]
- Config change cfg-2207 reduced portfolio-service db.pool.max_size from 50 to 5 at 09:52:00Z. [F2, F3]
- portfolio-service db_pool_in_use fell to a hard cap of 5.0 connections after the change. [F4]
- HikariPool reported total=5, active=5, idle=0 with waiting queues of 25-57 from 09:54:29. [F5]
- portfolio-service connection-acquire errors began at 09:53:19 (264 ERROR entries). [F6]
- api-gateway logged 133 'upstream timeout portfolio-service ... -> 504' warnings starting 09:53:53. [F7]
- api-gateway 5xx rate rose from ~0.2% to ~7.2% at the 09:48 bucket. [F8]
- Only deployment in window was dep-5220 (api-gateway v9.3.0) at 08:00:00Z, not rolled back. [F9]
- portfolio-service request_rate was flat ~560-585 req/s all window (no load increase). [F10]
- api-gateway CPU was flat ~42% throughout, including during the incident. [F11]
- ledger-db had zero WARN+ log matches for 06:00-11:59. [F13]

## Assumptions (not verified)
- Config changes take effect near their 'changed_at' timestamp; the 09:52 apply closely precedes the 09:53 first error.
- The M-shaped 09:48 latency bucket reflects the onset edge (peak 4418ms) rather than continuous degradation from 08:00.
- Pool size 50 (prior value) was adequate for the steady ~565 req/s load, as evidenced by pool usage of ~12 of 50.

## Timeline
- `2026-03-10T08:00:00Z` dep-5220: api-gateway v9.3.0 deployed (envoy 1.29->1.30, HTTP/2 to upstreams). Gateway remains healthy. [F9]
- `2026-03-10T09:40:00Z` Last fully-healthy bucket: gateway p99 ~206ms, portfolio error_rate 0.2%, pool ~12 connections. [F1, F12, F4]
- `2026-03-10T09:48:00Z` First degraded bucket: gateway p99 avg 1753ms/max 4418ms; gateway 5xx 2.07%. [F1, F8]
- `2026-03-10T09:52:13Z` cfg-2207 applied: portfolio-service db.pool.max_size=5 (cost-optimisation 'small' profile). [F2, F3]
- `2026-03-10T09:53:19Z` First portfolio-service error: 'could not acquire JDBC connection within 3000ms'. [F6]
- `2026-03-10T09:53:53Z` First api-gateway 'upstream timeout portfolio-service -> 504'. [F7]
- `2026-03-10T09:54:29Z` HikariPool fully saturated: total=5, active=5, idle=0, waiting=43. [F5]
- `2026-03-10T10:00:00Z` db_pool_in_use pinned at 5.0; portfolio error_rate ~22%; gateway 5xx ~7.2% steady state. [F4, F12, F8]
- `2026-03-10T10:05:00Z` Incident INC-002 reported (portfolio page timeouts, gateway p99 high). []

## Recommended actions
- **[immediate]** Revert cfg-2207 immediately (restore portfolio-service db.pool.max_size to 50) to relieve pool exhaustion. -- The pool cap of 5 is the direct cause of connection-acquire timeouts and ~22% portfolio 5xx; restoring capacity should recover the service. [F2, F4, F5, F6]
- **[short_term]** Add guardrails/validation on DB pool-size config profiles so 'small' cost profiles cannot reduce pool size below observed steady-state concurrency. -- infra-automation applied a sub-capacity profile with no load check, mirroring a prior incident (HIST-115: pool overridden by wrong environment profile). [F2, F10]
- **[short_term]** Add alerting on HikariPool waiting-queue depth and pool saturation rate at portfolio-service (not just gateway p99). -- Detected only via gateway symptoms; a pool-level alert would localize the fault faster. [F5, F7]
- **[short_term]** Do NOT roll back dep-5220 based on this incident; verify gateway HTTP/2 upgrade separately. -- Timing and error semantics rule out the deploy as the cause; rolling it back would not address pool exhaustion. [F1, F7, F9]

## Data gaps
- market-data-service latency query failed (backend 503, tc-016); could not independently confirm no degradation on that downstream dependency of portfolio-service.
- No ledger-db logs/DB-internal metrics were available (0 matches) to positively rule out subtle DB-side slowness, though portfolio errors are client pool-acquire timeouts, not DB refusals.
- Exact propagation delay between the 09:44 latency uptick, the 09:52 config apply, and the 09:53 first error is inferred from bucket resolution (8-10 min buckets), not second-level data.
- No explicit rollback/recovery test was available to confirm restoring pool size recovers the service.

## Investigation stats
- latency 65.2s, LLM calls 5, tokens 63696 (in 56307 / out 7389)
- tool calls 17 (1 failed, 0 partial, 1 recovered by retry), findings 13/13 verified
