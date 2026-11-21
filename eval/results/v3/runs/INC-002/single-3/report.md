# Incident investigation report: INC-002
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Portfolio page timeouts/504s (INC-002) originate in portfolio-service, not api-gateway. At 09:52:13 an automated cost-optimisation config change (cfg-2207) reduced portfolio-service's datasource pool max_size from 50 to 5. Within ~2 minutes every request thread contended for 5 connections; HikariPool reported total=5, active=5, idle=0, waiting=40-57, producing "could not acquire JDBC connection within 3000ms" errors. portfolio-service p99 jumped from ~180ms to ~4.2s and its 5xx rate from ~0.2% to ~22%; api-gateway surfaced these as upstream 504 timeouts on /v1/portfolio/holdings. The suspected 08:00 gateway HTTP/2 upgrade (dep-5220) is a red herring: gateway latency stayed flat (~210ms) for ~1h45m after it and only broke at 09:52. Ledger-db and other shared components are healthy, and request rate is unchanged (~565 req/s), ruling out dependency failure, infrastructure failure and capacity saturation. Root cause: config_change in portfolio-service.

## Probable root cause
**Config change shrank portfolio-service DB pool to 5, causing pool exhaustion** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.92, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Config change shrank portfolio-service DB pool to 5, causing pool exhaustion | config_change | portfolio-service | cfg-2207 | 0.92 (high) | active |
| 2 | H2: Gateway HTTP/2-to-upstream upgrade (dep-5220) broke portfolio proxying | bad_deployment | api-gateway | dep-5220 | 0.05 (low) | rejected |
| 3 | H3: Shared ledger-db / infrastructure degradation | infrastructure_failure | ledger-db | - | 0.03 (low) | rejected |

### H1: Config change shrank portfolio-service DB pool to 5, causing pool exhaustion
At 09:52:13 config change cfg-2207 applied db.pool.max_size=5 to portfolio-service (down from 50) via a cost-optimisation profile. The workload (~565 req/s) needs ~12 connections, so all 5 connections stayed busy (active=5, idle=0, waiting up to 57) and every /v1/portfolio/holdings request failed after the 3000ms acquire timeout, driving portfolio p99 to ~4.2s and 22% 5xx, which the gateway surfaced as upstream 504s.

**Causal chain**
1. cfg-2207 sets db.pool.max_size=50->5 on portfolio-service at 09:52:13 [F1]
2. HikariPool saturates: total=5, active=5, idle=0, waiting=40-57 [F3, F4]
3. Portfolio requests fail on 3s connection-acquire timeout; p99 ->4.2s, 5xx ->22% [F5, F3]
4. api-gateway returns upstream 504 on /v1/portfolio/holdings, p99 ->4.3s [F7, F6]

**Supporting evidence**
  - `F1` ✔ A config change cfg-2207 on portfolio-service set db.pool.max_size from 50 to 5 at 09:52, authored by infra-automation under a cost-optimisation profile.
  - `F3` ✔ portfolio-service HikariPool errors begin at 09:53:19 and show total=5, active=5, idle=0, waiting=43-57, i.e. the pool is permanently saturated at the new size of 5.
  - `F4` ✔ portfolio-service db_pool_in_use was ~12 connections before the change and pinned to exactly 5.0 from 10:00 onward.
  - `F5` ✔ portfolio-service p99 latency rose from ~180ms to ~4.2s and 5xx error rate from ~0.2% to ~22% within the 09:45-10:00 window, staying elevated through 11:59.
  - `F6` ✔ api-gateway p99 stayed flat at ~205-214ms from 06:00 through 09:30 (including 1h45m after the 08:00 deploy), then jumped to 2148ms in the 09:45 bucket and ~4.3s thereafter.
  - `F7` ✔ api-gateway logs show upstream timeouts to portfolio-service returning 504 on GET /v1/portfolio/holdings starting 09:53:53.
  - `F8` ✔ portfolio-service request_rate is unchanged at ~565-575 req/s across the incident, so traffic did not grow.
  - `F9` ✔ ledger-db remained healthy: cpu_pct steady ~30%, connections_active ~55-62 with no spike.
  - `F10` ✔ Other downstream services were unaffected: order-service 5xx stayed ~0.2% and market-data-service p99 stayed ~40ms throughout.
  - `F11` ✔ A historical incident (HIST-115) had the same mechanism: a datasource pool size overridden by a wrong environment profile, categorised config_change.
  - `F12` ✔ Interpretation: the fault originates in portfolio-service from a config change that shrank its DB connection pool below what its ~565 req/s workload needs; api-gateway 504s are a downstream symptom, and the 08:00 HTTP/2 deploy is not causal since latency stayed flat for ~1h45m after it.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The config value is the effective connection limit of the HikariPool (total=5 in logs matches new value 5).

### H2: Gateway HTTP/2-to-upstream upgrade (dep-5220) broke portfolio proxying
The 08:00 api-gateway v9.3.0 upgrade enabling HTTP/2 to portfolio-service would cause gateway-side timeouts and 504s on portfolio routes.

**Causal chain**
1. dep-5220 enables h2 to upstreams at 08:00 [F2]
2. No latency impact for 1h45m post-deploy [F6]

**Supporting evidence**
  - `F2` ✔ Only one deployment occurred in the window: api-gateway v9.3.0 (dep-5220) at 08:00, enabling HTTP/2 to upstreams; no rollback.
**Contradicting evidence**
  - `F1` ✔ A config change cfg-2207 on portfolio-service set db.pool.max_size from 50 to 5 at 09:52, authored by infra-automation under a cost-optimisation profile.
  - `F3` ✔ portfolio-service HikariPool errors begin at 09:53:19 and show total=5, active=5, idle=0, waiting=43-57, i.e. the pool is permanently saturated at the new size of 5.
  - `F6` ✔ api-gateway p99 stayed flat at ~205-214ms from 06:00 through 09:30 (including 1h45m after the 08:00 deploy), then jumped to 2148ms in the 09:45 bucket and ~4.3s thereafter.
  - `F10` ✔ Other downstream services were unaffected: order-service 5xx stayed ~0.2% and market-data-service p99 stayed ~40ms throughout.
**Rejected because:** gateway latency stayed flat ~206-214ms for ~1h45m after the 08:00 deploy and only broke at 09:52 (F6); the failure begins exactly at the cfg-2207 apply time (F1) and originates inside portfolio-service as JDBC connection-acquire timeouts (F3), which a gateway protocol change cannot produce. Also, order-service (also moved to h2) was unaffected (F10).

### H3: Shared ledger-db / infrastructure degradation
Portfolio timeouts could stem from ledger-db or another shared component degrading.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F3` ✔ portfolio-service HikariPool errors begin at 09:53:19 and show total=5, active=5, idle=0, waiting=43-57, i.e. the pool is permanently saturated at the new size of 5.
  - `F9` ✔ ledger-db remained healthy: cpu_pct steady ~30%, connections_active ~55-62 with no spike.
  - `F10` ✔ Other downstream services were unaffected: order-service 5xx stayed ~0.2% and market-data-service p99 stayed ~40ms throughout.
**Rejected because:** ledger-db cpu ~30% and connections ~55-62 with no spike (F9); the error is client-side pool acquisition (total=5) not DB-side slowness, and order-service/payment-service using the same ledger-db were unaffected (F10).

## Facts (verified observations)
- Config change cfg-2207 reduced portfolio-service db.pool.max_size from 50 to 5 at 09:52. [F1]
- Only deployment in the window was api-gateway v9.3.0 at 08:00 enabling HTTP/2 to upstreams. [F2]
- portfolio-service HikariPool showed total=5, active=5, idle=0, waiting up to 57 from 09:54, with connection-acquire timeouts from 09:53:19. [F3]
- portfolio-service p99 went from ~180ms to ~4.2s and 5xx from ~0.2% to ~22%. [F5]
- api-gateway p99 was flat ~210ms until 09:30 and jumped to ~4.3s from the 09:45 bucket. [F6]
- api-gateway emitted upstream 504 timeouts to portfolio-service from 09:53:53. [F7]
- portfolio-service request_rate was unchanged at ~565-575 req/s. [F8]
- ledger-db cpu ~30% and connections ~55-62, no anomaly. [F9]
- order-service 5xx ~0.2% and market-data-service p99 ~40ms throughout. [F10]

## Assumptions (not verified)
- The HikariPool 'total=5' in logs is the effective pool limit set by cfg-2207 (matches new_value 5).
- The last-known-good pool size of 50 is sufficient for the current ~565 req/s workload (pre-change usage ~12 connections).
- No other unlisted change occurred between 08:00 and 10:00 that could independently affect portfolio-service.

## Timeline
- `2026-03-10T07:00:00Z` Config cfg-2199 (order-service log sampling) changed - unrelated. []
- `2026-03-10T08:00:00Z` api-gateway v9.3.0 deployed (dep-5220), enabling HTTP/2 to upstreams; no latency impact. [F2, F6]
- `2026-03-10T09:52:13Z` Config cfg-2207 applied: portfolio-service db.pool.max_size=5 (was 50). [F1]
- `2026-03-10T09:53:19Z` First portfolio-service 'could not acquire JDBC connection within 3000ms' error. [F3]
- `2026-03-10T09:53:53Z` First api-gateway upstream 504 on /v1/portfolio/holdings. [F7]
- `2026-03-10T09:54:29Z` HikariPool reports total=5, active=5, idle=0, waiting=43. [F3]
- `2026-03-10T10:00:00Z` portfolio p99 ~4.2s and 5xx ~22%; gateway p99 ~4.3s (steady-state outage). [F5, F6]
- `2026-03-10T10:05:00Z` Incident INC-002 reported against api-gateway. []
- `2026-03-10T11:59:00Z` Outage persists; db_pool_in_use still pinned at 5. [F4]

## Recommended actions
- **[immediate]** Immediately revert cfg-2207 / raise portfolio-service db.pool.max_size back to 50 (or a value sized to ~565 req/s). -- The pool was cut from 50 to 5 while the workload needs ~12 connections; reverting should restore capacity and clear the 504s. [F1, F3, F4]
- **[short_term]** Add validation/guardrails so cost-optimisation profiles cannot set a datasource pool below the service's observed peak connection usage, and require review for DB pool changes. -- An automated 'small' profile silently shrank a production pool by 10x; this matches prior incident HIST-115. [F1, F11]
- **[short_term]** Alert on DB pool saturation (active==max && waiting>0) per service rather than only on gateway latency. -- Detection was via user-reported gateway latency; the pool-level signal (waiting=43) was available minutes earlier. [F3]
- **[short_term]** Do not roll back dep-5220 as a fix for this incident; keep the HTTP/2 upgrade unless separate evidence emerges. -- Gateway latency was flat for 1h45m after the 08:00 deploy and all symptoms begin at the 09:52 config apply. [F2, F6]

## Data gaps
- Config-change data shows cfg-2207 changed at 09:52:00 but portfolio log confirms apply at 09:52:13; the exact propagation/rollout across portfolio-service replicas (whether all replicas restarted or hot-reloaded the pool) is not directly observed.
- No metric was found exposing HikariPool 'waiting' count as a time-series; the waiting values come only from log lines, so precise correlation of waiting vs request_rate is inferred rather than plotted.
- The incident report's initial suspicion (gateway HTTP/2 upgrade) cannot be fully disproven by a rollback test since dep-5220 was never rolled back; rejection rests on timing and error-message mismatch, not a controlled revert.

## Investigation stats
- latency 67.98s, LLM calls 6, tokens 91111 (in 78916 / out 12195)
- tool calls 18 (0 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
