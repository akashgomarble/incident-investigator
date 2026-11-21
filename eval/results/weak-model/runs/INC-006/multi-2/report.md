# Incident investigation report: INC-006
*System: multi_agent | outcome: **inconclusive** | ended: budget_exhausted:deadline*

## Summary
The investigation did not identify a definitive root cause for the pricing-engine latency degradation. Evidence shows a CPU spike in market-data-service preceding the latency onset and increased error rates, while pricing-engine resources remained stable. However, missing deployment, configuration, and latency metrics for downstream services prevent a conclusive attribution.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|

## Facts (verified observations)
- No deployments were recorded for pricing-engine between 2026-04-09T00:00:00Z and 2026-04-09T14:59:00Z. [F1]
- No configuration changes were recorded for pricing-engine in the same period. [F2]
- The overall deployments list is incomplete; only one deployment (auth-service v7.2.0 at 2026-04-09T11:30:00Z) was returned with a warning of missing shards. [F3]
- Pricing-engine depends on market-data-service (http) and redis-cache (cache); it is also called by order-service (http) and indirectly by api-gateway via order-service. [F4]
- Redis-cache is a shared datastore used by multiple services (pricing-engine, market-data-service, etc.). [F5]
- Pricing-engine p99 latency spikes above 2000 ms at 2026-04-09T13:22:00Z, marking the onset of the degradation. [F6]
- Pricing-engine p99 latency remains elevated (avg > 2200 ms) through 13:24‑13:30 Z. [F7]
- Pricing-engine logs contain repeated deadline‑exceeded errors: "quote computation deadline exceeded: market-data-service took <n>ms". [F8]
- Market-data-service error_rate jumps from ~0.2 % to >1 % at 13:18 Z and stays around 3 % thereafter. [F9]
- No other services in the queried set show elevated p99 latency; the only latency spike observed is in pricing-engine. [F10]
- Pricing-engine CPU usage stayed roughly constant (≈39‑41 %) from 13:00 to 14:56 Z, with no spike at the latency onset. [F11]
- Pricing-engine memory usage remained stable around 48‑51 % throughout the window, with no notable increase at 13:22 Z. [F12]
- Market-data-service CPU usage spiked sharply starting at 13:20 Z, reaching an average of ~90 % and a max of ~94 % by 13:24 Z, and stayed elevated thereafter. [F13]
- Redis-cache CPU usage stayed flat around 21‑22 % for the entire period, showing no spike concurrent with the pricing-engine latency degradation. [F14]

## Assumptions (not verified)
- No deployments or configuration changes occurred for market-data-service, redis-cache, order-service, or other downstream components during the incident window (due to missing records).
- The latency spikes observed in pricing-engine are caused by downstream request latency rather than internal processing bottlenecks, based on stable CPU/memory on pricing-engine.
- (demoted from facts: no verified observation backs it) The observed CPU spike in market-data-service precedes the pricing-engine latency onset (13:22 Z), while pricing-engine’s own CPU and memory remain stable, suggesting downstream resource pressure may be contributing to the latency degradation.

## Timeline
- `2026-04-09T13:18:00Z` Market-data-service error_rate jumps to >1 %, staying around 3 % thereafter. [F9]
- `2026-04-09T13:20:00Z` Market-data-service CPU usage begins sharp rise, reaching ~90 % by 13:24 Z. [F13]
- `2026-04-09T13:22:00Z` Pricing-engine p99 latency spikes above 2000 ms (onset of degradation). [F6]
- `2026-04-09T13:23:57Z` Pricing-engine logs deadline‑exceeded errors referencing market-data-service. [F8]
- `2026-04-09T13:24:00Z` Pricing-engine p99 latency remains elevated (>2200 ms). [F7]
- `2026-04-09T13:00:00Z‑2026-04-09T14:56:00Z` Pricing-engine CPU and memory usage stay stable (≈40 % CPU, ≈50 % memory). [F11, F12]
- `2026-04-09T14:59:00Z` Investigation snapshot taken; Redis-cache CPU flat, no spike observed. [F14]

## Recommended actions
- **[immediate]** Throttle or temporarily reduce request rate from pricing-engine to market-data-service (e.g., enable rate limiting or circuit breaker). -- Market-data-service CPU is saturated (~90 %); reducing load may relieve pressure and restore latency. [F13]
- **[immediate]** Restart market-data-service to clear any transient resource contention. -- A quick restart can free CPU and may immediately reduce deadline‑exceeded errors while longer diagnostics run. [F13]
- **[short_term]** Collect missing deployment and configuration change records for market-data-service, redis-cache, and order-service (e.g., query CI/CD logs, config management DB). -- Current gaps prevent confirming whether a recent change caused the CPU spike. [F3]
- **[short_term]** Retrieve and store market-data-service latency_p99_ms metric (resolve backend 503) and pricing-db resource metrics (CPU, memory, connection pool latency). -- Direct latency evidence and DB load data are needed to validate the downstream pressure hypothesis. [F9, F15]
- **[long_term]** Implement continuous health checks and alerts for downstream services (CPU >80 %, error_rate >1 %) that feed into pricing-engine SLA monitoring. -- Proactive detection of downstream resource saturation will prevent similar latency degradations. [F13]
- **[long_term]** Improve deployment inventory completeness (ensure all shards are captured) and integrate with incident response tooling. -- Incomplete deployment data (F3) hampers root‑cause analysis in future incidents. [F3]

## Data gaps
- Deployments data for all services is incomplete; missing shards may hide deployments of pricing-engine or its dependencies.
- No configuration change records were found for services other than pricing-engine; downstream config changes could be unknown.
- Market-data-service latency_p99_ms metric could not be retrieved due to backend unavailability (503).
- Pricing-db metrics (CPU, memory, connection pool latency) were not found.
- Network I/O, DB connection pool latency, and request_rate/error_rate metrics for pricing-engine and market-data-service were not collected.

## What would change this conclusion
- Evidence of a deployment or configuration change in market-data-service (or another downstream dependency) that coincides with the CPU spike.
- Successful retrieval of market-data-service latency_p99_ms showing a matching latency spike that aligns with pricing-engine deadline‑exceeded errors.
- Metrics indicating pricing-db resource exhaustion (e.g., high CPU or connection pool latency) that correlate with the pricing-engine latency increase.

## Integrity warnings (set by code)
- fact demoted to assumption: The observed CPU spike in market-data-service precedes the pricing-engine latenc

## Investigation stats
- latency 497.87s, LLM calls 20, tokens 78227 (in 65552 / out 12675)
- tool calls 15 (2 failed, 1 partial, 1 recovered by retry), findings 15/15 verified
