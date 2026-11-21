# Incident investigation report: INC-006
*System: multi_agent | outcome: **inconclusive** | ended: budget_exhausted:deadline*

## Summary
The investigation could not definitively identify a root cause; the leading hypothesis (downstream market-data-service latency increase) has low confidence (0.28). Verified observations confirm a sharp latency rise in pricing-engine starting at 13:18‑13:22 with stable CPU and no recent deployments, but missing downstream and network metrics prevent ruling out other causes.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Downstream market-data-service latency increase | dependency_failure | market-data-service | null | 0.28 (low) | active |
| 2 | H2: Redis-cache resource exhaustion causing request delays | resource_exhaustion | redis-cache | null | 0.25 (low) | active |
| 3 | H3: Network congestion between pricing-engine and its dependencies | infrastructure_failure | network | null | 0.22 (low) | active |
| 4 | H4: Shared library regression introduced via a recent deployment | bad_deployment | shared-library | null | 0.20 (low) | active |

### H1: Downstream market-data-service latency increase
A latency spike in market-data-service caused pricing-engine calls to take longer, inflating its p99 latency.

**Causal chain**
1. market-data-service experienced increased response latency around 13:18 *(assumed)*
2. pricing-engine observed p99 latency rise to >2 s [F1, F2, F3]

**Supporting evidence**
  - `F1` ✔ Pricing-engine p99 latency sharply increased at 2026-04-09T13:22:00Z, jumping from ~60 ms to >2 s.
  - `F2` ✔ After the onset, pricing-engine p99 latency stayed elevated (≈1.8‑2.2 s) through the observation window.
  - `F3` ✔ Coarser 6‑minute buckets confirm the latency surge beginning at the 13:18 bucket (max ≈ 2.2 s) and remaining high thereafter.
  - `F4` ✔ CPU usage for pricing-engine stayed around 38‑41 % with no noticeable spike during the latency degradation.
**Contradicting evidence**
  - (none)
**Assumptions**
  - market-data-service latency metrics would show a spike at 13:18
  - no other downstream service caused the delay

### H2: Redis-cache resource exhaustion causing request delays
High load or contention on the shared redis-cache increased request latency for pricing-engine, leading to degraded quote times.

**Causal chain**
1. redis-cache experienced high latency or connection pool exhaustion around 13:18 *(assumed)*
2. pricing-engine observed p99 latency rise to >2 s [F1, F2, F3]

**Supporting evidence**
  - `F1` ✔ Pricing-engine p99 latency sharply increased at 2026-04-09T13:22:00Z, jumping from ~60 ms to >2 s.
  - `F2` ✔ After the onset, pricing-engine p99 latency stayed elevated (≈1.8‑2.2 s) through the observation window.
  - `F3` ✔ Coarser 6‑minute buckets confirm the latency surge beginning at the 13:18 bucket (max ≈ 2.2 s) and remaining high thereafter.
  - `F4` ✔ CPU usage for pricing-engine stayed around 38‑41 % with no noticeable spike during the latency degradation.
  - `F8` ✔ Redis-cache is a shared datastore used by multiple services (pricing-engine, market-data-service, etc.).
**Contradicting evidence**
  - (none)
**Assumptions**
  - redis-cache latency metrics would show a spike at 13:18
  - pricing-engine reliance on redis-cache is significant for quote generation

### H3: Network congestion between pricing-engine and its dependencies
A transient network issue increased round‑trip times to downstream services, causing pricing-engine latency to jump.

**Causal chain**
1. network latency or packet loss increased for traffic to market-data-service and redis-cache around 13:18 *(assumed)*
2. pricing-engine observed p99 latency rise to >2 s [F1, F2, F3]

**Supporting evidence**
  - `F1` ✔ Pricing-engine p99 latency sharply increased at 2026-04-09T13:22:00Z, jumping from ~60 ms to >2 s.
  - `F2` ✔ After the onset, pricing-engine p99 latency stayed elevated (≈1.8‑2.2 s) through the observation window.
  - `F3` ✔ Coarser 6‑minute buckets confirm the latency surge beginning at the 13:18 bucket (max ≈ 2.2 s) and remaining high thereafter.
  - `F4` ✔ CPU usage for pricing-engine stayed around 38‑41 % with no noticeable spike during the latency degradation.
**Contradicting evidence**
  - (none)
**Assumptions**
  - network monitoring would show elevated latency or errors at the time
  - no other infrastructure component (e.g., load balancer) failed

### H4: Shared library regression introduced via a recent deployment
A recent deployment of a shared library used by pricing-engine introduced a performance regression, causing higher processing latency.

**Causal chain**
1. a deployment of a shared library (used by pricing-engine) occurred shortly before 13:18 and introduced slower code paths *(assumed)*
2. pricing-engine observed p99 latency rise to >2 s [F1, F2, F3]

**Supporting evidence**
  - `F1` ✔ Pricing-engine p99 latency sharply increased at 2026-04-09T13:22:00Z, jumping from ~60 ms to >2 s.
  - `F2` ✔ After the onset, pricing-engine p99 latency stayed elevated (≈1.8‑2.2 s) through the observation window.
  - `F3` ✔ Coarser 6‑minute buckets confirm the latency surge beginning at the 13:18 bucket (max ≈ 2.2 s) and remaining high thereafter.
  - `F4` ✔ CPU usage for pricing-engine stayed around 38‑41 % with no noticeable spike during the latency degradation.
**Contradicting evidence**
  - `F5` ✔ No deployments were recorded for pricing-engine between 2026-04-09T12:00:00Z and 2026-04-09T14:00:00Z.
  - `F6` ✔ No configuration changes were recorded for pricing-engine in the same window.
  - `F9` ✔ No deployments were recorded for any service (all services scope) in the window 2026-04-09T12:00:00Z to 2026-04-09T14:00:00Z.
**Assumptions**
  - the shared library deployment is not recorded in pricing-engine's own deployment history
  - no other service deployments occurred that could explain the latency

## Facts (verified observations)
- Pricing-engine p99 latency sharply increased at 2026-04-09T13:22:00Z, jumping from ~60 ms to >2 s. [F1]
- After the onset, pricing-engine p99 latency stayed elevated (≈1.8‑2.2 s) through the observation window. [F2]
- Coarser 6‑minute buckets confirm the latency surge beginning at the 13:18 bucket (max ≈ 2.2 s) and remaining high thereafter. [F3]
- CPU usage for pricing-engine stayed around 38‑41 % with no noticeable spike during the latency degradation. [F4]
- No deployments were recorded for pricing-engine between 2026-04-09T12:00:00Z and 2026-04-09T14:00:00Z. [F5]
- No configuration changes were recorded for pricing-engine in the same window. [F6]
- The service dependency graph shows pricing-engine calls market-data-service (http) and redis-cache (cache). [F7]
- Redis-cache is a shared datastore used by multiple services including pricing-engine and market-data-service. [F8]
- No deployments were recorded for any service in the window 2026-04-09T12:00:00Z to 2026-04-09T14:00:00Z. [F9]

## Assumptions (not verified)
- market-data-service latency metrics would show a spike at 13:18
- no other downstream service caused the delay
- redis-cache latency metrics would show a spike at 13:18
- pricing-engine reliance on redis-cache is significant for quote generation
- network monitoring would show elevated latency or errors at the time
- no other infrastructure component (e.g., load balancer) failed
- the shared library deployment is not recorded in pricing-engine's own deployment history
- no other service deployments occurred that could explain the latency

## Timeline
- `2026-04-09T13:00:00Z` Baseline CPU usage observed (38‑41 %). [F4]
- `2026-04-09T13:18:00Z` Coarse latency bucket indicates start of surge (max ≈2.2 s). [F3]
- `2026-04-09T13:22:00Z` Pricing-engine p99 latency spikes sharply (>2 s). [F1]
- `2026-04-09T13:30:00Z` Incident reported by on‑call SRE; alerts fire (deadline‑exceeded). []
- `2026-04-09T13:30:00Z – 14:59:00Z` Elevated latency persists (1.8‑2.2 s). [F2]

## Recommended actions
- **[immediate]** Collect and examine latency metrics for market-data-service and redis-cache for the window 13:00‑14:00 UTC. -- Missing downstream latency data is a key gap; confirming or refuting spikes will discriminate between H1 and H2.
- **[immediate]** Run network health checks (e.g., ping, traceroute, packet loss) between pricing-engine and its downstream services. -- Network congestion is hypothesis H3; direct measurement will clarify if network latency contributed.
- **[short_term]** Enable detailed request tracing (e.g., OpenTelemetry) for pricing-engine calls to market-data-service and redis-cache. -- Will provide per‑request latency breakdown to pinpoint the slow path.
- **[short_term]** Query deployment and configuration change history for downstream services (market-data-service, redis-cache, exchange-gateway, order-service, api-gateway) for the incident window. -- Addresses gaps that could reveal a hidden change supporting H1, H2, H3, or H4.
- **[short_term]** Restart pricing-engine pods to clear any potential in‑process state or resource contention. -- Safe mitigation; if latency drops, suggests transient internal issue. [F4]
- **[long_term]** Add alerting on downstream service latency (market-data-service, redis-cache) and on network RTTs. -- Prevents recurrence by surfacing similar latency spikes early.
- **[long_term]** Implement version‑aware deployment tracking for shared libraries and enforce audit of any library updates. -- Mitigates risk of hidden shared‑library regressions (H4).

## Data gaps
- Missing error_rate metric for pricing-engine; cannot correlate errors with latency.
- Latency metrics for downstream services (market-data-service, redis-cache, exchange-gateway, etc.) were not retrieved.
- Network latency and packet loss measurements between pricing-engine and its dependencies are absent.
- Deployment and configuration change history for downstream services is unavailable.
- Historical incident data for similar pricing-engine latency patterns was not searched.

## What would change this conclusion
- Observation of a clear latency spike in market-data-service at ~13:18‑13:22 would raise confidence in H1.
- Observation of a latency spike or high contention metrics in redis-cache at the same time would support H2.
- Network monitoring showing elevated RTTs or packet loss between pricing-engine and its dependencies would support H3.
- Discovery of a recent shared‑library deployment affecting pricing-engine would support H4.

## Investigation stats
- latency 536.59s, LLM calls 16, tokens 50467 (in 40843 / out 9624)
- tool calls 10 (2 failed, 1 partial, 0 recovered by retry), findings 9/9 verified
