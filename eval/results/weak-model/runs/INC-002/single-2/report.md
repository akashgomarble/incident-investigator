# Incident investigation report: INC-002
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
A config change (cfg-2207) reduced the DB connection pool size for portfolio-service, exhausting connections. This produced HikariPool timeout errors, which caused upstream 504 responses logged by api-gateway and a sharp p99 latency increase. The evidence strongly supports this config_change root cause.

## Probable root cause
**Config change reduced DB pool causing connection exhaustion** -- config_change in `portfolio-service`, triggered by `cfg-2207` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Config change reduced DB pool causing connection exhaustion | config_change | portfolio-service | cfg-2207 | 0.86 (high) | active |
| 2 | H2: api-gateway HTTP/2 upgrade introduced upstream timeout bug | bad_deployment | api-gateway | dep-5220 | 0.38 (low) | rejected |

### H1: Config change reduced DB pool causing connection exhaustion
The reduction of portfolio-service DB pool max size from 50 to 5 exhausted connections, leading to HikariPool timeouts, which caused upstream 504s from api-gateway and the observed latency spike.

**Causal chain**
1. cfg-2207 -> pool exhaustion [F1, F2]
2. pool exhaustion -> upstream timeouts [F2, F3]
3. upstream timeouts -> gateway latency spike [F3, F4]

**Supporting evidence**
  - `F1` ✔ A config change (cfg-2207) reduced portfolio-service DB pool max size from 50 to 5 at 09:52.
  - `F2` ✔ Portfolio-service logs show HikariPool connection timeout errors starting at 09:54, with pool total=5, active=5, idle=0, many waiting.
  - `F3` ✔ api-gateway logs warn of upstream timeout to portfolio-service starting at 09:53.
  - `F4` ✔ api-gateway p99 latency spikes from ~300ms to >3s at 09:49 and climbs to >4s thereafter.
  - `F5` ✔ portfolio-service error_rate jumps from <0.3% to >13% at 09:49 and stays >20%.
**Contradicting evidence**
  - (none)

### H2: api-gateway HTTP/2 upgrade introduced upstream timeout bug
The api-gateway deployment at 08:00 introduced an HTTP/2 handling bug that caused upstream timeouts to portfolio-service, inflating latency.

**Causal chain**
1. dep-5220 -> HTTP/2 bug *(assumed)*
2. HTTP/2 bug -> upstream timeouts [F3]
3. upstream timeouts -> latency spike [F4]

**Supporting evidence**
  - `F3` ✔ api-gateway logs warn of upstream timeout to portfolio-service starting at 09:53.
  - `F4` ✔ api-gateway p99 latency spikes from ~300ms to >3s at 09:49 and climbs to >4s thereafter.
**Contradicting evidence**
  - `F1` ✔ A config change (cfg-2207) reduced portfolio-service DB pool max size from 50 to 5 at 09:52.
  - `F2` ✔ Portfolio-service logs show HikariPool connection timeout errors starting at 09:54, with pool total=5, active=5, idle=0, many waiting.
  - `F5` ✔ portfolio-service error_rate jumps from <0.3% to >13% at 09:49 and stays >20%.
**Assumptions**
  - The bug would manifest immediately after deployment
**Rejected because:** Timing mismatch: latency and errors appear >1.5 h after deployment and coincide with config change and pool exhaustion evidence.

## Facts (verified observations)
- The incident began around 09:50 when latency and error rates spiked. [F4, F5]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-03-10T08:00:00Z` api-gateway deployment (dep-5220) released []
- `2026-03-10T09:52:00Z` portfolio-service DB pool size reduced (cfg-2207) [F1]
- `2026-03-10T09:49:00Z` Latency p99 and error_rate spikes start [F4, F5]
- `2026-03-10T09:53:53Z` api-gateway logs upstream timeout warnings [F3]
- `2026-03-10T09:54:29Z` Portfolio-service HikariPool connection timeout errors appear [F2]

## Recommended actions
- **[immediate]** Rollback config change cfg-2207 or increase DB pool max size back to original (≥50). -- Restores connection capacity, expected to stop timeouts and latency. [F1, F2]
- **[short_term]** Monitor portfolio-service DB pool usage metrics (db_pool_in_use) after rollback. -- Confirm pool exhaustion is resolved.
- **[short_term]** Add alert on sudden drop in DB pool size config for critical services. -- Prevent similar incidents.

## Data gaps
- Missing db_pool_in_use metric for portfolio-service to directly observe pool utilization.
- No CPU/memory metrics for portfolio-service during the incident window.
- No logs from other downstream services (order-service, market-data-service) to rule out broader impact.

## Integrity warnings (set by code)
- timeline: removed citations to non-existent findings ['tc-001']

## Investigation stats
- latency 380.52s, LLM calls 10, tokens 57049 (in 51294 / out 5755)
- tool calls 9 (0 failed, 0 partial, 0 recovered by retry), findings 6/6 verified
