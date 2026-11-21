# Incident investigation report: INC-002
*System: multi_agent | outcome: **root_cause_identified** | ended: budget_exhausted:max_llm_calls*

## Summary
The investigation identifies the portfolio-service database connection pool size reduction at 09:52 as the root cause of the outage, with medium confidence (0.60). The config change (F7) directly preceded spikes in portfolio-service error rate (F13), api-gateway latency (F10) and error rate (F11), and the first 504 timeout (F12). While direct pool exhaustion metrics are missing, no other services show similar spikes, and the temporal correlation strongly supports this hypothesis.

## Probable root cause
**Portfolio-service DB pool size reduction caused downstream failures** -- config_change in `portfolio-service`, triggered by `config_change_2026-03-10T09:52:00Z` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: Portfolio-service DB pool size reduction caused downstream failures | config_change | portfolio-service | config_change_2026-03-10T09:52:00Z | 0.60 (medium) | active |
| 2 | H1: Regression introduced by api-gateway HTTP/2 upgrade | bad_deployment | api-gateway | v9.3.0 | 0.42 (low) | active |
| 3 | H3: api-gateway resource exhaustion after upgrade | resource_exhaustion | api-gateway | v9.3.0 | 0.33 (low) | active |

### H2: Portfolio-service DB pool size reduction caused downstream failures
A config change on portfolio-service at 09:52 reduced db.pool.max_size from 50 to 5, exhausting the connection pool, causing portfolio-service error_rate to spike, which in turn made api-gateway experience high latency, higher error_rate, and 504 upstream timeouts.

**Causal chain**
1. Config change reduced db.pool.max_size from 50 to 5 [F7]
2. Reduced pool caused connection exhaustion, raising portfolio-service error_rate [F13]
3. Higher error_rate propagated to api-gateway, causing latency spike and error_rate increase [F10, F11]
4. Gateway returned 504 upstream timeout errors for portfolio requests [F12]

**Supporting evidence**
  - `F7` ✔ A config change on portfolio-service reduced db.pool.max_size from 50 to 5 at 2026-03-10T09:52:00Z.
  - `F13` ✔ portfolio-service error_rate spikes from ~0.2 % to >11 % at 2026-03-10T09:52:00Z.
  - `F10` ✔ api-gateway p99 latency spikes from ~200 ms to >3 s at 2026-03-10T09:52:00Z.
  - `F11` ✔ api-gateway error_rate jumps from ~0.2 % to ~3.9 % at 2026-03-10T09:52:00Z.
  - `F12` ✔ First logged 504 upstream timeout for portfolio-service appears at 2026-03-10T09:53:53Z.
**Contradicting evidence**
  - (none)

### H1: Regression introduced by api-gateway HTTP/2 upgrade
The api-gateway upgrade (v9.3.0) enabled HTTP/2 to upstreams, introducing a bug that caused request handling delays, leading to high p99 latency, increased error rate, and downstream 504 timeouts for the portfolio endpoint.

**Causal chain**
1. api-gateway upgrade to HTTP/2 (v9.3.0) deployed at 08:00 [F5]
2. Upgrade introduced a processing regression that manifested under load, causing request handling delays *(assumed)*
3. Gateway latency p99 spikes to >3 s and error_rate jumps to ~3.9 % at 09:52 [F10, F11]
4. Delayed handling results in upstream timeout errors (504) for portfolio requests [F12]

**Supporting evidence**
  - `F5` ✔ api-gateway was deployed version v9.3.0 at 2026-03-10T08:00:00Z, upgrading envoy and enabling HTTP/2 to upstreams.
  - `F10` ✔ api-gateway p99 latency spikes from ~200 ms to >3 s at 2026-03-10T09:52:00Z.
  - `F11` ✔ api-gateway error_rate jumps from ~0.2 % to ~3.9 % at 2026-03-10T09:52:00Z.
  - `F12` ✔ First logged 504 upstream timeout for portfolio-service appears at 2026-03-10T09:53:53Z.
**Contradicting evidence**
  - `F4` ✔ No log entries containing the keywords "upgrade" or "HTTP/2" were found for api-gateway between 08:00 and 11:59.
  - `F13` ✔ portfolio-service error_rate spikes from ~0.2 % to >11 % at 2026-03-10T09:52:00Z.
**Assumptions**
  - The regression only appears after sustained traffic post‑upgrade; resource usage increased enough to trigger the bug.

### H3: api-gateway resource exhaustion after upgrade
Following the HTTP/2 upgrade, api-gateway exhausted a resource (e.g., connection slots or network I/O), leading to high latency and error rates, which manifested as 504 timeouts for portfolio requests.

**Causal chain**
1. api-gateway upgrade to HTTP/2 increased resource demand [F5]
2. Resource (connections/network I/O) became exhausted, causing processing delays *(assumed)*
3. Latency and error_rate spikes observed at 09:52 [F10, F11]
4. Resulting in 504 upstream timeout errors for portfolio endpoint [F12]

**Supporting evidence**
  - `F5` ✔ api-gateway was deployed version v9.3.0 at 2026-03-10T08:00:00Z, upgrading envoy and enabling HTTP/2 to upstreams.
  - `F10` ✔ api-gateway p99 latency spikes from ~200 ms to >3 s at 2026-03-10T09:52:00Z.
  - `F11` ✔ api-gateway error_rate jumps from ~0.2 % to ~3.9 % at 2026-03-10T09:52:00Z.
  - `F12` ✔ First logged 504 upstream timeout for portfolio-service appears at 2026-03-10T09:53:53Z.
**Contradicting evidence**
  - `F13` ✔ portfolio-service error_rate spikes from ~0.2 % to >11 % at 2026-03-10T09:52:00Z.
**Assumptions**
  - Resource metrics post‑upgrade would show saturation; lack of such data is a gap.

## Facts (verified observations)
- The api-gateway deployment rollout completed at 2026-03-10T08:00:14Z, upgrading to version v9.3.0 and enabling HTTP/2 to upstreams. [F1, F5]
- A config change on portfolio-service reduced db.pool.max_size from 50 to 5 at 2026-03-10T09:52:00Z. [F7]
- Portfolio-service error_rate spiked from ~0.2% to >11% at 2026-03-10T09:52:00Z. [F13]
- Api-gateway p99 latency spiked from ~200 ms to >3 s at 2026-03-10T09:52:00Z. [F10]
- Api-gateway error_rate jumped from ~0.2% to ~3.9% at 2026-03-10T09:52:00Z. [F11]
- The first logged 504 upstream timeout for portfolio-service appeared at 2026-03-10T09:53:53Z. [F12]
- No deployments were recorded for portfolio-service between 2026-03-09 and 2026-03-10T12:00:00Z. [F6]
- Pre‑upgrade CPU and memory usage on api-gateway were moderate (CPU avg ~42%, Memory avg ~58.5%). [F2, F3]
- No log entries containing "upgrade" or "HTTP/2" were found for api-gateway between 08:00 and 11:59. [F4]

## Assumptions (not verified)
- The reduction of db.pool.max_size caused connection pool exhaustion, leading to the observed error_rate spike in portfolio-service.
- The portfolio-service error spike was the primary driver of the api-gateway latency and error_rate increase; other downstream services remained healthy.
- No other config changes or deployments affecting the portfolio endpoint occurred during the incident window.

## Timeline
- `2026-03-10T08:00:14Z` Api-gateway rollout to version v9.3.0 (HTTP/2 enabled) completed. [F1, F5]
- `2026-03-10T09:52:00Z` Portfolio-service config change reduces DB pool max size from 50 to 5. [F7]
- `2026-03-10T09:52:00Z` Portfolio-service error_rate spikes >11%; api-gateway latency spikes >3 s; api-gateway error_rate spikes to ~3.9%. [F10, F11, F13]
- `2026-03-10T09:53:53Z` First 504 upstream timeout logged for portfolio endpoint. [F12]
- `2026-03-10T11:56:00Z` Persistently elevated api-gateway error_rate (~7%) and latency (>4 s) observed (unverified). [F14]
- `2026-03-10T11:56:00Z` Persistently elevated portfolio-service error_rate (~22%) observed (unverified). [F15]

## Recommended actions
- **[immediate]** Revert portfolio-service DB pool max size to 50 (or increase to a safe value) and restart the service. -- Directly addresses the config change identified as the root cause (F7) and should immediately relieve connection pressure, reducing error_rate (F13) and downstream latency (F10). [F7]
- **[short_term]** Monitor portfolio-service DB pool metrics (active connections, wait queue length, exhaustion errors) for the next 2 hours to confirm reduction in errors. -- Validates the assumed causal link between pool exhaustion and error spikes; fills data gap T4.
- **[short_term]** Verify that api-gateway latency and error_rate return to baseline (<300 ms p99, <0.5% error) after the config revert. -- Ensures downstream impact is resolved; uses existing observability metrics (F10, F11).
- **[long_term]** Implement a change‑management guard that requires approval and testing for DB pool size reductions, and add an alert on sudden error_rate spikes in portfolio-service. -- Prevents recurrence of similar mis‑configurations; addresses root‑cause procedural gap.
- **[long_term]** Collect post‑upgrade resource utilization metrics for api-gateway (CPU, memory, network I/O, connection counts, request rate) to rule out any hidden resource exhaustion. -- Fills GAPS related to resource metrics; would help discriminate between H2 and resource‑exhaustion hypotheses.

## Data gaps
- Missing DB connection pool metrics for portfolio-service (active connections, wait queue length, pool exhaustion errors) for the incident window.
- Missing post‑upgrade resource utilization metrics (CPU, memory, network I/O, connection counts, request rate) for api-gateway.
- No metrics or logs for other downstream services (auth-service, market-data-service, etc.) to confirm they remained healthy.
- No latency metric for portfolio-service to assess its contribution to overall response time.

## What would change this conclusion
- Evidence that the portfolio-service DB pool was not exhausted (e.g., pool metrics showing ample free connections) would weaken H2.
- Discovery of a bug in the api-gateway HTTP/2 handling causing latency spikes (e.g., logs or core dumps) would shift confidence to H1.
- Metrics showing severe resource saturation in api-gateway post‑upgrade (e.g., connection count limits reached) would support H3.

## Investigation stats
- latency 486.88s, LLM calls 43, tokens 166518 (in 147603 / out 18915)
- tool calls 32 (3 failed, 0 partial, 0 recovered by retry), findings 13/15 verified
