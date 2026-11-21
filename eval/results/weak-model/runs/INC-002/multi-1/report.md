# Incident investigation report: INC-002
*System: multi_agent | outcome: **inconclusive** | ended: budget_exhausted:deadline*

## Summary
Investigation is inconclusive. The leading hypothesis—that the portfolio-service database pool reduction caused connection starvation—has only 45% confidence and is not fully supported by the verified data. Key observations show simultaneous latency spikes in both api-gateway and portfolio-service starting before the config change, and critical resource utilization metrics are missing. Immediate mitigation and targeted data collection are required to discriminate between the remaining hypotheses.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Portfolio-service DB pool reduction caused connection starvation | config_change | portfolio-service | F2 | 0.45 (low) | active |
| 2 | H2: Gateway HTTP/2 upgrade introduced resource pressure | bad_deployment | api-gateway | F1 | 0.30 (low) | active |
| 3 | H3: Shared ledger‑db overload degraded portfolio-service | dependency_failure | ledger-db | null | 0.20 (low) | active |
| 4 | H4: Load balancer/network issue affecting portfolio traffic | infrastructure_failure | load‑balancer | null | 0.10 (low) | active |

### H1: Portfolio-service DB pool reduction caused connection starvation
A configuration change at 2026-03-10T09:52:00Z reduced the portfolio-service database pool max size from 50 to 5, starving connections to the shared ledger‑db. This caused portfolio-service latency spikes, which propagated to api‑gateway latency and resulted in 504 upstream timeouts for the /v1/portfolio/* endpoint.

**Causal chain**
1. Config change reduced DB pool size [F2]
2. Reduced pool caused connection starvation on ledger‑db *(assumed)*
3. Connection starvation increased portfolio-service latency [F10]
4. Increased latency in portfolio-service caused api‑gateway latency spikes [F7]
5. Gateway latency spikes produced 504 upstream timeouts [F9]

**Supporting evidence**
  - `F2` ✔ A configuration change reduced portfolio-service database pool max size from 50 to 5 at 2026-03-10T09:52:00Z.
  - `F10` ✔ portfolio-service p99 latency spiked to ~1865 ms (avg) and up to 4546 ms (max) starting at 2026-03-10T09:50:00Z.
  - `F7` ✔ api-gateway p99 latency spiked to ~1850 ms (avg) and up to 4418 ms (max) starting at 2026-03-10T09:50:00Z.
  - `F9` ✔ First 504 WARN log for portfolio endpoint recorded at 2026-03-10T09:53:53Z: "upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504".
  - `F8` ✔ api-gateway error_rate jumped to >1 % (avg 1.66 %) and max 7.18 % starting at 2026-03-10T09:50:00Z.
  - `F11` ✔ Elevated latency and 504 errors are concentrated on the endpoint GET /v1/portfolio/holdings routed to upstream service portfolio-service.
**Contradicting evidence**
  - `F7` ✔ api-gateway p99 latency spiked to ~1850 ms (avg) and up to 4418 ms (max) starting at 2026-03-10T09:50:00Z.
**Assumptions**
  - DB connection starvation directly translates to observed latency spikes in portfolio-service

### H2: Gateway HTTP/2 upgrade introduced resource pressure
The api‑gateway upgrade to version v9.3.0 at 2026-03-10T08:00:00Z enabled HTTP/2 to upstreams, increasing connection handling overhead and exhausting gateway resources, which manifested as latency spikes and 504 timeouts for the portfolio endpoint.

**Causal chain**
1. Gateway upgraded to HTTP/2-enabled version [F1]
2. HTTP/2 increased connection/resource usage beyond capacity *(assumed)*
3. Resource pressure caused gateway latency spikes [F7]
4. Latency spikes led to upstream 504 errors [F9]

**Supporting evidence**
  - `F1` ✘ unverified A deployment of api-gateway version v9.3.0 was applied at 2026-03-10T08:00:00Z, upgrading the envoy filter chain and enabling HTTP/2 to upstreams.
  - `F7` ✔ api-gateway p99 latency spiked to ~1850 ms (avg) and up to 4418 ms (max) starting at 2026-03-10T09:50:00Z.
  - `F8` ✔ api-gateway error_rate jumped to >1 % (avg 1.66 %) and max 7.18 % starting at 2026-03-10T09:50:00Z.
  - `F9` ✔ First 504 WARN log for portfolio endpoint recorded at 2026-03-10T09:53:53Z: "upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504".
**Contradicting evidence**
  - `F12` ✘ unverified No metric data retrieved for api-gateway CPU, memory, or connection usage during the incident window.
  - `F7` ✔ api-gateway p99 latency spiked to ~1850 ms (avg) and up to 4418 ms (max) starting at 2026-03-10T09:50:00Z.
**Assumptions**
  - HTTP/2 upgrade materially increased connection count or processing load on the gateway
  - No other concurrent changes explain the latency

### H3: Shared ledger‑db overload degraded portfolio-service
The shared ledger‑db (PostgreSQL) became overloaded due to concurrent load from multiple services, causing high query latency for portfolio-service, which then propagated to api‑gateway resulting in timeouts.

**Causal chain**
1. Ledger‑db experienced overload *(assumed)*
2. Overload increased portfolio-service latency [F10]
3. Portfolio latency caused gateway latency spikes [F7]
4. Gateway latency produced 504 upstream errors [F9]

**Supporting evidence**
  - `F5` ✔ Portfolio-service accesses three downstream components: ledger-db (shared PostgreSQL), market-data-service (HTTP), and redis-cache (shared Redis).
  - `F10` ✔ portfolio-service p99 latency spiked to ~1865 ms (avg) and up to 4546 ms (max) starting at 2026-03-10T09:50:00Z.
  - `F7` ✔ api-gateway p99 latency spiked to ~1850 ms (avg) and up to 4418 ms (max) starting at 2026-03-10T09:50:00Z.
  - `F9` ✔ First 504 WARN log for portfolio endpoint recorded at 2026-03-10T09:53:53Z: "upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504".
  - `F11` ✔ Elevated latency and 504 errors are concentrated on the endpoint GET /v1/portfolio/holdings routed to upstream service portfolio-service.
**Contradicting evidence**
  - `F6` ✘ unverified Both ledger-db and redis-cache are marked as shared resources used by multiple services.
**Assumptions**
  - Ledger‑db overload directly translates to observed latency in portfolio-service
  - Shared nature of ledger‑db leads to contention

### H4: Load balancer/network issue affecting portfolio traffic
A change or failure in the load balancer or network path for the /v1/portfolio/* endpoint introduced additional latency and packet loss, causing api‑gateway timeouts.

**Causal chain**
1. Load balancer/network introduced latency *(assumed)*
2. Increased latency manifested as gateway latency spikes [F7]
3. Latency spikes resulted in 504 upstream errors [F9]

**Supporting evidence**
  - `F7` ✔ api-gateway p99 latency spiked to ~1850 ms (avg) and up to 4418 ms (max) starting at 2026-03-10T09:50:00Z.
  - `F9` ✔ First 504 WARN log for portfolio endpoint recorded at 2026-03-10T09:53:53Z: "upstream timeout portfolio-service GET /v1/portfolio/holdings -> 504".
  - `F8` ✔ api-gateway error_rate jumped to >1 % (avg 1.66 %) and max 7.18 % starting at 2026-03-10T09:50:00Z.
**Contradicting evidence**
  - (none)
**Assumptions**
  - No other network changes were made; the observed latency is attributable to LB/network

## Facts (verified observations)
- A configuration change reduced the portfolio-service database pool max size from 50 to 5 at 2026-03-10T09:52:00Z. [F2]
- api-gateway p99 latency spiked to ~1850 ms (avg) and up to 4418 ms (max) starting at 2026-03-10T09:50:00Z. [F7]
- api-gateway error rate jumped to >1 % (avg 1.66 %) and max 7.18 % starting at 2026-03-10T09:50:00Z. [F8]
- First 504 WARN log for the portfolio endpoint was recorded at 2026-03-10T09:53:53Z. [F9]
- portfolio-service p99 latency spiked to ~1865 ms (avg) and up to 4546 ms (max) starting at 2026-03-10T09:50:00Z. [F10]
- Elevated latency and 504 errors are concentrated on the endpoint GET /v1/portfolio/holdings routed to upstream service portfolio-service. [F11]
- api-gateway calls portfolio-service via HTTP for the /v1/portfolio/* endpoint. [F4]
- Portfolio-service accesses three downstream components: ledger-db (shared PostgreSQL), market-data-service (HTTP), and redis-cache (shared Redis). [F5]

## Assumptions (not verified)
- DB connection starvation directly translates to the observed latency spikes in portfolio-service.
- The HTTP/2 upgrade materially increased connection count or processing load on the gateway, leading to resource pressure.
- Ledger‑db overload directly translates to the observed latency in portfolio-service.
- No other network or load‑balancer changes were made that could explain the latency.

## Timeline
- `2026-03-10T08:00:00Z` api-gateway upgraded to version v9.3.0 with HTTP/2 enabled (unverified deployment). []
- `2026-03-10T09:50:00Z` Simultaneous latency spikes begin in api-gateway (p99 ~1850 ms) and portfolio-service (p99 ~1865 ms). [F7, F10]
- `2026-03-10T09:50:00Z` api-gateway error rate rises above 1 %. [F8]
- `2026-03-10T09:52:00Z` Configuration change reduces portfolio-service DB pool max size from 50 to 5. [F2]
- `2026-03-10T09:53:53Z` First 504 warning logged for portfolio endpoint (upstream timeout). [F9]

## Recommended actions
- **[immediate]** Revert the portfolio-service database pool max size back to its prior value (50) and monitor latency. -- If connection starvation is the cause, restoring pool capacity should alleviate latency; this change is low‑risk and can be rolled back quickly. [F2]
- **[immediate]** Temporarily disable HTTP/2 on api-gateway (rollback to previous envoy filter configuration) and observe if latency improves. -- If the HTTP/2 upgrade introduced resource pressure, disabling it provides a quick mitigation while data is collected.
- **[short_term]** Collect missing resource utilization metrics for api-gateway (CPU, memory, active connections, thread‑pool usage) for the window 08:00‑10:30 UTC. -- These metrics are required to confirm or refute hypothesis H2 regarding gateway resource saturation.
- **[short_term]** Gather portfolio-service DB pool metrics (active connections, wait queue length, acquisition latency) before and after the 09:52 change. -- Direct evidence of connection starvation will validate or invalidate hypothesis H1.
- **[short_term]** Obtain ledger‑db performance metrics (CPU, active connections, query latency, timeout logs) for 09:30‑10:30 UTC. -- Needed to assess hypothesis H3 that shared ledger‑db overload contributed to the incident.
- **[short_term]** Review load‑balancer configuration change logs and network path health for the /v1/portfolio/* endpoint. -- To evaluate hypothesis H4 concerning possible LB/network issues.
- **[long_term]** Implement alerting on portfolio-service DB pool usage and api-gateway resource saturation thresholds. -- Pre‑emptively detect similar conditions in the future.
- **[long_term]** Establish a post‑mortem process to verify all dependency flags (e.g., shared resources) and ensure they are documented and observable. -- Reduces uncertainty from unverified assumptions like the shared nature of ledger‑db and redis‑cache.

## Data gaps
- Missing api-gateway resource utilization metrics (CPU %, memory %, active connection count, thread‑pool usage) for the incident window.
- No portfolio-service database pool usage metrics (active connections, wait queue length, acquisition latency) around the 09:52 config change.
- Ledger‑db performance metrics (CPU, connections, query latency) are unavailable for the relevant period.
- Verification of the 'shared' flag for ledger‑db and redis‑cache is unverified.
- Load‑balancer change logs and network latency measurements for the portfolio endpoint are absent.

## What would change this conclusion
- Evidence of active connection starvation in portfolio-service after the DB pool reduction (e.g., high wait queue length, increased acquisition latency).
- Metrics showing api-gateway CPU, memory, or connection saturation coinciding with the latency spike, confirming resource pressure from the HTTP/2 upgrade.
- Ledger‑db load metrics indicating overload (high CPU, connection count, query latency) during the incident window.
- Logs or change records indicating a load‑balancer or network configuration change affecting the /v1/portfolio/* path.

## Investigation stats
- latency 505.77s, LLM calls 22, tokens 88295 (in 72235 / out 16060)
- tool calls 14 (1 failed, 0 partial, 0 recovered by retry), findings 9/18 verified
