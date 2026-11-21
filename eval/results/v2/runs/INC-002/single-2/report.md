# Incident investigation report: INC-002
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The portfolio page timeouts originate in portfolio-service, not api-gateway. At 09:52 UTC the infra-automation cost-optimisation profile cfg-2207 reduced portfolio-service db.pool.max_size from 50 to 5. The HikariCP pool immediately saturated (total=5, active=5, idle=0, requests waiting), JDBC connection acquisition exceeded the 3000ms timeout, portfolio-service error rate rose to ~22% and p99 to ~4.5s, and api-gateway surfaced 504 upstream timeouts on /v1/portfolio/holdings. The human-reported suspicion (gateway HTTP/2 deploy dep-5220 at 08:00) is contradicted: latency was normal for ~1h45m after that deploy, the same HTTP/2 change applied to order-service caused no impact, and the errors are JDBC pool exhaustion, not protocol failures. Request rate and CPU were flat and ledger-db/market-data-service were healthy, ruling out load, resource exhaustion, and dependency failure. This matches historical incident HIST-115 (same signature, config_change root cause). Fix: revert cfg-2207 to restore pool size 50.

## Probable root cause
**portfolio-service JDBC pool capped at 5 by cost-optimisation config change cfg-2207** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.88, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: portfolio-service JDBC pool capped at 5 by cost-optimisation config change cfg-2207 | config_change | portfolio-service | cfg-2207 | 0.88 (high) | active |
| 2 | H2: api-gateway HTTP/2 upgrade (dep-5220) broke upstream calls to portfolio-service | bad_deployment | api-gateway | dep-5220 | 0.06 (low) | rejected |
| 3 | H3: Traffic/load outgrew provisioned JDBC capacity (capacity_saturation) | capacity_saturation | portfolio-service | - | 0.04 (low) | rejected |
| 4 | H4: Dependency or shared-datastore failure (ledger-db / market-data-service) | dependency_failure | portfolio-service | - | 0.02 (low) | rejected |

### H1: portfolio-service JDBC pool capped at 5 by cost-optimisation config change cfg-2207
At 09:52 infra-automation applied the 'small' cost-optimisation profile to portfolio-service datasource, reducing db.pool.max_size from 50 to 5 (cfg-2207). The pool became saturated (active=5, idle=0, waiting up to 57), JDBC connection acquisition exceeded the 3000ms timeout, requests failed with 22% error rate, portfolio latency hit ~4.5s, and api-gateway returned 504s on /v1/portfolio/*. The fault originates in portfolio-service configuration, not the gateway deploy.

**Causal chain**
1. cfg-2207 reduces db.pool.max_size 50 -> 5 [F3]
2. pool pinned at 5 with active=5, idle=0, waiting up to 57 and 3000ms acquire timeouts [F6, F7]
3. portfolio-service p99 -> ~4.2s and error rate -> ~22% [F4, F5]
4. api-gateway returns 504 upstream timeouts on /v1/portfolio/* [F11]

**Supporting evidence**
  - `F3` ✔ A config change cfg-2207 on portfolio-service reduced db.pool.max_size from 50 to 5 at 09:52 via the 'small' cost-optimisation profile.
  - `F6` ✔ portfolio-service logs show 503 ERROR entries from 09:53:19, dominated by 'could not acquire JDBC connection within 3000ms' (270) and 'HikariPool-1 - Connection is not available... (total=5, active=5, idle=0, waiting=43)' (233).
  - `F7` ✔ portfolio-service db_pool_in_use dropped from ~12 to exactly 5.0 from the 10:00 bucket onward, pinned at the new max_size=5.
  - `F4` ✔ portfolio-service p99 latency jumped to ~4180ms in the 09:45 bucket (max 4546.3), matching the gateway latency spike.
  - `F5` ✔ portfolio-service error rate rose from ~0.2% to ~22% starting in the 09:45 bucket (max 24.02).
  - `F11` ✔ gateway logs show 254 WARN entries, 252 of them 'upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504', beginning 09:53:53.
  - `F8` ✔ portfolio-service request_rate stayed flat at ~560 req/s across the whole window (08:00-11:59), showing no traffic surge.
  - `F12` ✔ portfolio-service CPU stayed flat ~36% throughout the incident (max 39.78), ruling out application CPU exhaustion.
  - `F13` ✔ ledger-db slow_queries_per_min stayed flat ~1.0 across the window, indicating the shared database was healthy.
  - `F14` ✔ market-data-service (a portfolio-service dependency) p99 stayed flat ~40ms throughout, healthy.
  - `F15` ✔ Historical incident HIST-115 describes the same signature: 'connection pool misconfiguration' caused by a datasource pool overridden by a wrong environment profile, category config_change.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The 'small' profile's db.pool.max_size=5 is the value that took effect (matches observed pool ceiling of exactly 5)
  - Portfolio /holdings endpoint is DB-bound and cannot serve without a pooled JDBC connection

### H2: api-gateway HTTP/2 upgrade (dep-5220) broke upstream calls to portfolio-service
The reported hypothesis: the gateway v9.3.0 deploy at 08:00 switching upstream protocol to HTTP/2 introduced the fault. Rejected because latency was normal for ~1h45m after the deploy (08:00-09:45), the identical change was applied to order-service which remained completely healthy, and the portfolio-service error logs show a JDBC connection-pool exhaustion cause rather than a protocol/h2 failure.

**Causal chain**
1. dep-5220 enables h2 to portfolio-service at 08:00 [F2]
2. portfolio-service calls degrade ~09:45 [F4]

**Supporting evidence**
  - `F2` ✔ The only code deployment in the window was dep-5220 on api-gateway (v9.3.0) at 08:00, enabling HTTP/2 to upstreams for BOTH portfolio-service and order-service; it was never rolled back.
**Contradicting evidence**
  - `F9` ✔ order-service, which also received the h2 upstream upgrade from dep-5220, shows completely flat p99 (~150ms) with no spike all day.
  - `F10` ✔ order-service error rate stayed ~0.2% throughout, unaffected.
  - `F6` ✔ portfolio-service logs show 503 ERROR entries from 09:53:19, dominated by 'could not acquire JDBC connection within 3000ms' (270) and 'HikariPool-1 - Connection is not available... (total=5, active=5, idle=0, waiting=43)' (233).
  - `F3` ✔ A config change cfg-2207 on portfolio-service reduced db.pool.max_size from 50 to 5 at 09:52 via the 'small' cost-optimisation profile.
  - `F4` ✔ portfolio-service p99 latency jumped to ~4180ms in the 09:45 bucket (max 4546.3), matching the gateway latency spike.
**Rejected because:** Gateway latency was normal until 09:45, ~1h45m after the 08:00 deploy; the same h2 change on order-service caused no impact; error evidence points to portfolio JDBC pool exhaustion, not protocol errors.

### H3: Traffic/load outgrew provisioned JDBC capacity (capacity_saturation)
Request rate stayed flat at ~560 req/s the entire window with no surge, so growth in load did not drive the failure; the pool ceiling was lowered by config, not exceeded by demand.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ portfolio-service request_rate stayed flat at ~560 req/s across the whole window (08:00-11:59), showing no traffic surge.
**Rejected because:** request_rate is flat (~560 req/s) before and during the incident, so there was no load increase.

### H4: Dependency or shared-datastore failure (ledger-db / market-data-service)
portfolio-service dependencies ledger-db (slow_queries flat ~1/min) and market-data-service (p99 flat ~40ms) were both healthy; nothing indicates a downstream dependency fault.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F13` ✔ ledger-db slow_queries_per_min stayed flat ~1.0 across the window, indicating the shared database was healthy.
  - `F14` ✔ market-data-service (a portfolio-service dependency) p99 stayed flat ~40ms throughout, healthy.
**Rejected because:** ledger-db slow queries and market-data-service latency are flat and healthy throughout.

## Facts (verified observations)
- (none)

## Assumptions (not verified)
- cfg-2207's 'small' profile value of 5 is what took effect (corroborated by pool ceiling of exactly 5 in metrics and logs).
- The /v1/portfolio/holdings path requires a pooled JDBC connection to ledger-db and cannot degrade gracefully.
- No other unobserved change occurred between 09:45 and 09:53 that could independently cause the pool saturation.

## Timeline
- `2026-03-10T07:00:00Z` cfg-2199 order-service log.sampling_rate 0.1->0.2 (cosmetic; no latency impact). [F10]
- `2026-03-10T08:00:00Z` dep-5220 api-gateway v9.3.0 deployed: envoy 1.29->1.30, HTTP/2 to portfolio-service and order-service. No immediate impact. [F2, F9]
- `2026-03-10T09:45:00Z` api-gateway and portfolio-service p99 latency ramp begins (~2100ms avg bucket, max ~4600ms); portfolio db_pool_in_use begins declining. [F1, F4, F7]
- `2026-03-10T09:52:00Z` cfg-2207 reduces portfolio-service db.pool.max_size 50->5 via 'small' cost-optimisation profile. [F3]
- `2026-03-10T09:53:19Z` portfolio-service logs first JDBC connection-acquisition timeouts; HikariPool 'Connection is not available' (total=5, active=5, idle=0, waiting increasing). [F6]
- `2026-03-10T09:53:53Z` api-gateway logs first 'upstream timeout portfolio-service ... -> 504' warnings. [F11]
- `2026-03-10T10:00:00Z` portfolio-service db_pool_in_use pinned at exactly 5.0; error rate ~22%; p99 ~4.2s sustained. [F7, F5, F4]
- `2026-03-10T10:05:00Z` Incident INC-002 reported (portfolio page timing out). []
- `2026-03-10T11:59:00Z` Incident still ongoing; no rollback of dep-5220; cfg-2207 not yet reverted. [F1, F7]

## Recommended actions
- **[immediate]** Revert cfg-2207 to restore portfolio-service db.pool.max_size=50 (or set an appropriate validated size for observed ~560 req/s). -- The pool ceiling of 5 is the direct cause; observed demand sustained ~12 in-use connections pre-incident, so 5 cannot serve load. [F3, F7, F6]
- **[immediate]** Do NOT roll back dep-5220 as the primary mitigation; if performed, expect no recovery since the h2 change is not the cause. -- order-service uses the same h2 change with zero impact and gateway latency was normal for ~1h45m post-deploy. [F2, F9, F10]
- **[short_term]** Add a guardrail/validation to infra-automation profiles so cost-optimisation cannot shrink datasource pools below a service's minimum required size. -- A blanket 'small' profile silently cut a production pool by 90%, echoing HIST-115. [F3, F15]
- **[short_term]** Alert on db_pool_in_use being pinned at max_size with waiting>0 and on JDBC acquire-timeout log rate. -- Pool saturation was observable in metrics/logs minutes before user impact escalated. [F6, F7]
- **[long_term]** Review change-management for automated config pushes during business hours (cfg-2207 applied 09:52 with no canary). -- Automated unattended config change caused a SEV-2 outage. [F3]

## Data gaps
- No direct log line stating the pool size was reloaded at 09:52; causation relies on timing correlation plus the pool ceiling of exactly 5 matching the new config value.
- The latency ramp begins in the 09:45 bucket while cfg-2207 is stamped 09:52; the 15-minute bucket granularity makes the exact onset ambiguous (metrics cannot resolve sub-15-min).
- ledger-db has no latency_p99_ms metric; health was assessed only via slow_queries_per_min and dependency-serving services' latency.
- No telemetry for external providers (bank-partner-api, kyc-provider) but they are not on the portfolio path.

## Investigation stats
- latency 47.58s, LLM calls 5, tokens 56082 (in 49085 / out 6997)
- tool calls 17 (1 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
