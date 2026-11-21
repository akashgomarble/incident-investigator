# Incident investigation report: INC-006
*System: single_agent | outcome: **inconclusive** | ended: agent_submitted*

## Summary
Pricing‑engine is reporting deadline‑exceeded errors that trace to market‑data‑service latency. Metrics show a sharp rise in market‑data‑service p99 latency, CPU usage, and error rate around 13:20, while request volume stayed flat and no new deployments or config changes were recorded. This points to a possible resource‑exhaustion event on market‑data‑service, but without logs from that service or visibility into its downstream dependencies we cannot rule out a downstream failure. The root cause remains undetermined.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Resource exhaustion on market-data-service | resource_exhaustion | market-data-service | - | 0.68 (medium) | active |
| 2 | H2: Downstream dependency failure (exchange‑gateway) | dependency_failure | exchange-gateway | - | 0.45 (low) | active |

### H1: Resource exhaustion on market-data-service
A sudden CPU saturation on market-data-service caused latency spikes and downstream deadline‑exceeded errors in pricing‑engine.

**Causal chain**
1. CPU usage spiked → latency ↑ → pricing‑engine deadline errors [F3, F2, F1]

**Supporting evidence**
  - `F2` ✔ market-data-service p99 latency spiked to avg 977 ms (max 1979.98 ms) at 13:20
  - `F3` ✔ market-data-service CPU usage rose to avg 90.89 % (max 94.51 %) at 13:24
  - `F4` ✔ market-data-service HTTP error rate jumped to avg 3.23 % (max 3.35 %) at 13:24
  - `F1` ✔ pricing-engine emitted ERROR logs stating "quote computation deadline exceeded: market-data-service took 5171ms"
  - `F9` ✔ market-data-service request rate stayed around 950 req/s (avg 979.01 at 13:20) – no traffic surge
**Contradicting evidence**
  - `F5` ✔ No deployments were recorded for any service between 12:00 and 15:00 on 2026‑04‑09
  - `F6` ✔ No configuration changes were recorded for market-data-service between 12:00 and 15:00 on 2026‑04‑09
  - `F7` ✔ market-data-service emitted zero ERROR logs containing "timeout" in the examined window

### H2: Downstream dependency failure (exchange‑gateway)
market-data-service experienced latency due to a problem in its downstream dependency (exchange‑gateway), propagating to pricing‑engine.

**Causal chain**
1. exchange‑gateway slowdown → market-data-service latency ↑ → pricing‑engine deadline errors [F2, F1]

**Supporting evidence**
  - `F2` ✔ market-data-service p99 latency spiked to avg 977 ms (max 1979.98 ms) at 13:20
  - `F1` ✔ pricing-engine emitted ERROR logs stating "quote computation deadline exceeded: market-data-service took 5171ms"
**Contradicting evidence**
  - `F7` ✔ market-data-service emitted zero ERROR logs containing "timeout" in the examined window
  - `F5` ✔ No deployments were recorded for any service between 12:00 and 15:00 on 2026‑04‑09
  - `F6` ✔ No configuration changes were recorded for market-data-service between 12:00 and 15:00 on 2026‑04‑09
**Assumptions**
  - exchange-gateway experienced an unobserved issue

## Facts (verified observations)
- Pricing‑engine deadline errors coincide with market‑data‑service latency spikes. [F1, F2]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-04-09T13:20:00Z` market-data-service latency, CPU, and error rate spike [F2, F3, F4]
- `2026-04-09T13:23:57Z` first pricing-engine deadline‑exceeded log [F1]

## Recommended actions
- **[immediate]** Collect detailed CPU and thread‑level profiling from market-data-service for the spike window. -- To confirm whether CPU saturation caused the latency. [F3]
- **[short_term]** Enable higher‑verbosity logging on market-data-service and its downstream (exchange-gateway) for the next hour. -- Current logs show no errors; richer logs may reveal hidden failures. [F7]
- **[short_term]** Review recent autoscaling events or resource limits for market-data-service. -- Resource limits could explain CPU spikes. [F3]

## Data gaps
- Deployment data for individual services other than the global view is incomplete (tc-004 partial).
- No metrics or logs for downstream services such as exchange-gateway or redis-cache.
- Absence of detailed request traces linking pricing-engine calls to market-data-service latency.

## Investigation stats
- latency 382.89s, LLM calls 16, tokens 96722 (in 89523 / out 7199)
- tool calls 15 (2 failed, 1 partial, 0 recovered by retry), findings 9/9 verified
