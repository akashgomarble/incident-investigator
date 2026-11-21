# Incident investigation report: INC-006
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
The incident was caused by a sudden CPU‑bound resource exhaustion in market-data-service, which drove its request latency from ~40 ms to ~1800 ms. Pricing-engine, which depends on market-data-service, timed out on each quote computation, producing the observed deadline‑exceeded errors.

## Probable root cause
**Resource exhaustion in market-data-service** -- resource_exhaustion in `market-data-service` (confidence 0.82, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Resource exhaustion in market-data-service | resource_exhaustion | market-data-service | - | 0.82 (high) | active |
| 2 | H2: Pricing-engine internal processing slowdown | bad_deployment | pricing-engine | - | 0.35 (low) | rejected |

### H1: Resource exhaustion in market-data-service
A CPU‑bound spike in market-data-service caused its request latency to rise sharply, which in turn triggered deadline‑exceeded errors in pricing‑engine.

**Causal chain**
1. CPU usage surge → latency spike in market-data-service [F3, F2]
2. Latency spike → pricing-engine deadline errors [F1]

**Supporting evidence**
  - `F1` ✔ pricing-engine emitted ERROR logs "quote computation deadline exceeded: market-data-service took <n>ms" starting at 2026-04-09T13:23:57Z and continuing every few seconds.
  - `F2` ✔ market-data-service p99 latency spiked from ~40 ms to ~1800 ms, with a bucket at 2026-04-09T13:20:00Z showing avg 977 ms, max 1979.98 ms.
  - `F3` ✔ market-data-service CPU usage rose sharply, reaching avg 90.89 % and max 94.51 % at 2026-04-09T13:24:00Z.
  - `F4` ✔ market-data-service request rate remained stable (~950 req/s) throughout the incident window.
  - `F5` ✔ No deployments were recorded for pricing-engine or market-data-service on 2026‑04‑09.
  - `F6` ✔ No configuration or feature‑flag changes were made for any service in the incident window.
**Contradicting evidence**
  - (none)

### H2: Pricing-engine internal processing slowdown
Pricing-engine itself became CPU‑bound or deadlocked, causing it to miss its internal deadline regardless of market-data-service latency.

**Causal chain**
1. Pricing-engine slowdown → deadline errors [F1]

**Supporting evidence**
  - `F1` ✔ pricing-engine emitted ERROR logs "quote computation deadline exceeded: market-data-service took <n>ms" starting at 2026-04-09T13:23:57Z and continuing every few seconds.
**Contradicting evidence**
  - `F2` ✔ market-data-service p99 latency spiked from ~40 ms to ~1800 ms, with a bucket at 2026-04-09T13:20:00Z showing avg 977 ms, max 1979.98 ms.
  - `F3` ✔ market-data-service CPU usage rose sharply, reaching avg 90.89 % and max 94.51 % at 2026-04-09T13:24:00Z.
  - `F5` ✔ No deployments were recorded for pricing-engine or market-data-service on 2026‑04‑09.
  - `F6` ✔ No configuration or feature‑flag changes were made for any service in the incident window.
**Assumptions**
  - pricing-engine CPU metrics would show a spike
**Rejected because:** Contradicted by stable request rate, lack of any deployment or config change, and strong evidence of upstream latency.

## Facts (verified observations)
- Pricing-engine deadline errors began shortly after market-data-service latency spiked. [F1, F2]
- CPU usage and latency spikes co‑occurred, while request volume stayed constant. [F3, F2, F4]
- No recent code or config changes could have introduced a regression. [F5, F6]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-04-09T13:20:00Z` market-data-service latency p99 jumps to ~1 s (avg 977 ms, max 1979 ms) [F2]
- `2026-04-09T13:24:00Z` market-data-service CPU peaks at ~94 % [F3]
- `2026-04-09T13:23:57Z` First pricing-engine deadline‑exceeded log entry [F1]
- `2026-04-09T13:30‑14:30Z` Continued deadline errors and high latency [F1]

## Recommended actions
- **[immediate]** Investigate market-data-service CPU usage (e.g., thread pool saturation, GC pauses) and apply appropriate scaling or optimisation. -- CPU spike is the root cause of latency and downstream timeouts. [F2, F3]
- **[short_term]** Add alerting on market-data-service latency and CPU thresholds to catch similar spikes earlier. -- Early detection can prevent downstream impact on pricing-engine. [F2, F3]
- **[short_term]** Review market-data-service code paths for potential CPU‑intensive queries triggered at ~13:20 (e.g., market‑wide data refresh). -- Identify the specific operation causing the CPU burst. [F3]

## Data gaps
- No CPU/memory metrics for pricing-engine to rule out local overload.
- No detailed trace data linking individual pricing-engine requests to market-data-service latency.
- No internal logs from market-data-service beyond errors (none found).

## Investigation stats
- latency 295.36s, LLM calls 14, tokens 77818 (in 72123 / out 5695)
- tool calls 13 (2 failed, 1 partial, 1 recovered by retry), findings 7/7 verified
