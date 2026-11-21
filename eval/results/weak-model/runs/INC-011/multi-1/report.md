# Incident investigation report: INC-011
*System: multi_agent | outcome: **root_cause_identified** | ended: budget_exhausted:deadline*

## Summary
The investigation could not definitively identify a root cause. Latency spikes in both order-service and pricing-engine began at 12:05, coinciding with a market-data-service deployment, but no direct evidence links the spike to resource exhaustion, protobuf incompatibility, or a hidden config change. Confidence in the leading hypothesis (resource exhaustion) remains medium (0.50) but is unsupported by concrete resource metrics, so the conclusion is inconclusive. Immediate mitigations and further data collection are recommended.

## Probable root cause
**Pricing-engine resource exhaustion caused latency spikes** -- resource_exhaustion in `pricing-engine`, triggered by `null` (confidence 0.50, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: Pricing-engine resource exhaustion caused latency spikes | resource_exhaustion | pricing-engine | null | 0.50 (medium) | active |
| 2 | H1: Market-data-service protobuf change broke pricing-engine processing | bad_deployment | market-data-service | F1 | 0.45 (low) | active |
| 3 | H3: Undetected config change lowered order-service timeout threshold | config_change | order-service | null | 0.35 (low) | active |

### H2: Pricing-engine resource exhaustion caused latency spikes
Pricing-engine suffered a resource exhaustion (CPU or memory) event around 12:05, causing its request latency to increase dramatically; order-service calls timed out as a result.

**Causal chain**
1. pricing-engine experienced resource exhaustion (CPU/memory) leading to processing slowdown *(assumed)*
2. pricing-engine p99 latency spiked to >2.5 s [F9]
3. order-service quote calls to pricing-engine timed out [F7, F6]
4. order-service observed latency spike as calls blocked [F8]

**Supporting evidence**
  - `F9` ✔ pricing-engine p99 latency also spiked at the 12:05 bucket (2026-05-20T12:05:00Z). Avg 2591 ms, max 2849 ms.
  - `F7` ✔ All timeout errors share the signature "pricing-engine quote call timed out after <n>ms orderId=<n>".
  - `F6` ✔ The first quote timeout error in order-service occurred at 2026-05-20T12:06:10Z.
  - `F8` ✔ order-service p99 latency spiked from ~150 ms to >2.3 s starting at the 12:05 bucket (2026-05-20T12:05:00Z). Avg 2306 ms, max 3011 ms.
**Contradicting evidence**
  - (none)
**Assumptions**
  - no other observable resource metrics were collected, so exhaustion is inferred from latency

### H1: Market-data-service protobuf change broke pricing-engine processing
The market-data-service deployment introduced a new protobuf quote serialization that pricing-engine could not handle, causing pricing-engine to stall on each request, leading to latency spikes and order-service quote timeouts.

**Causal chain**
1. market-data-service deployed new version with protobuf serialization [F1]
2. pricing-engine receives incompatible serialized data and spends excessive time processing or fails *(assumed)*
3. pricing-engine latency spiked (p99 >2.5s) [F9]
4. order-service calls pricing-engine for quote and experiences timeout errors [F7, F6]
5. order-service latency spikes as calls block [F8]

**Supporting evidence**
  - `F1` ✔ market-data-service deployed a new version v5.2.0 with protobuf quote serialization at 2026-05-20T11:56:00Z
  - `F9` ✔ pricing-engine p99 latency also spiked at the 12:05 bucket (2026-05-20T12:05:00Z). Avg 2591 ms, max 2849 ms.
  - `F7` ✔ All timeout errors share the signature "pricing-engine quote call timed out after <n>ms orderId=<n>".
  - `F6` ✔ The first quote timeout error in order-service occurred at 2026-05-20T12:06:10Z.
  - `F8` ✔ order-service p99 latency spiked from ~150 ms to >2.3 s starting at the 12:05 bucket (2026-05-20T12:05:00Z). Avg 2306 ms, max 3011 ms.
**Contradicting evidence**
  - (none)
**Assumptions**
  - pricing-engine depends on market-data-service for quote data
  - the protobuf change is incompatible with pricing-engine's deserializer

### H3: Undetected config change lowered order-service timeout threshold
A configuration change (not captured in the audit) reduced the timeout value used by order-service when calling pricing-engine, making the existing latency spike exceed the new threshold and resulting in quote timeouts.

**Causal chain**
1. a config change lowered the quote-call timeout in order-service *(assumed)*
2. pricing-engine latency spike (F9) now exceeds the lowered threshold [F9]
3. order-service records timeout errors [F7, F6]
4. order-service latency spikes as calls block [F8]

**Supporting evidence**
  - `F7` ✔ All timeout errors share the signature "pricing-engine quote call timed out after <n>ms orderId=<n>".
  - `F6` ✔ The first quote timeout error in order-service occurred at 2026-05-20T12:06:10Z.
  - `F8` ✔ order-service p99 latency spiked from ~150 ms to >2.3 s starting at the 12:05 bucket (2026-05-20T12:05:00Z). Avg 2306 ms, max 3011 ms.
  - `F9` ✔ pricing-engine p99 latency also spiked at the 12:05 bucket (2026-05-20T12:05:00Z). Avg 2591 ms, max 2849 ms.
**Contradicting evidence**
  - `F4` ✔ order-service had no configuration changes between 2026-05-20T10:00:00Z and 2026-05-20T14:00:00Z
**Assumptions**
  - config audit was incomplete (F5) and may have missed a change
  - order-service uses a timeout value that can be altered via config

## Facts (verified observations)
- Market-data-service deployed version v5.2.0 with a new protobuf quote serialization at 2026-05-20T11:56:00Z. [F1]
- Order-service had no deployments between 2026-05-20T10:00:00Z and 2026-05-20T14:00:00Z. [F2]
- Pricing-engine had no deployments between 2026-05-20T10:00:00Z and 2026-05-20T14:00:00Z. [F3]
- Order-service had no configuration changes between 2026-05-20T10:00:00Z and 2026-05-20T14:00:00Z. [F4]
- The config audit returned an incomplete result; changes may be missing. [F5]
- The first quote timeout error in order-service occurred at 2026-05-20T12:06:10Z. [F6]
- All timeout errors share the signature "pricing-engine quote call timed out after <n>ms orderId=<n>". [F7]
- Order-service p99 latency spiked from ~150 ms to >2.3 s starting at 12:05. [F8]
- Pricing-engine p99 latency also spiked at 12:05 (avg 2591 ms, max 2849 ms). [F9]
- Market-data-service p99 latency remained stable (~40 ms) across the same period. [F10]

## Assumptions (not verified)
- Resource exhaustion in pricing-engine is inferred from latency spikes without direct CPU/memory metrics.
- Pricing-engine depends on market-data-service for quote data.
- The new protobuf serialization introduced in market-data-service may be incompatible with pricing-engine's deserializer.
- Order-service timeout threshold can be altered via configuration, and a hidden config change could have lowered it.

## Timeline
- `2026-05-20T11:56:00Z` Market-data-service deployed version v5.2.0 with new protobuf serialization. [F1]
- `2026-05-20T12:05:00Z` Latency spikes observed: order-service p99 >2.3 s, pricing-engine p99 >2.5 s. [F8, F9]
- `2026-05-20T12:05:00Z` Market-data-service latency remained stable. [F10]
- `2026-05-20T12:06:10Z` First quote timeout error recorded in order-service. [F6]

## Recommended actions
- **[immediate]** Temporarily increase the order-service quote call timeout to 5 s to prevent immediate failures. -- Mitigates impact while root cause is investigated; latency spikes exceed current timeout. [F8, F6]
- **[immediate]** Restart the pricing-engine service to clear any transient resource lock or memory pressure. -- If resource exhaustion is present, a restart may restore normal performance. [F9]
- **[short_term]** Collect CPU, memory, and GC metrics for pricing-engine covering the 12:00‑13:00 window. -- Direct evidence needed to confirm or refute the resource exhaustion hypothesis (H2).
- **[short_term]** Retrieve full configuration change history for order-service (including the missing shard) and audit timeout settings. -- Needed to evaluate the hidden config‑change hypothesis (H3).
- **[short_term]** Gather pricing-engine logs for deserialization errors or stack traces around 12:05‑12:10. -- Would provide direct evidence for protobuf incompatibility hypothesis (H1).
- **[short_term]** Create a complete service dependency map to confirm that order-service calls pricing-engine and that pricing-engine consumes market-data-service data for quotes. -- Clarifies call chain and validates assumptions about dependencies.
- **[long_term]** Implement continuous resource‑usage alerting for pricing-engine (CPU >80%, memory >75%) and automated rollback of deployments that cause latency spikes. -- Prevents recurrence of resource‑exhaustion‑related incidents. [F9]
- **[long_term]** Add compatibility tests for protobuf schema changes in market-data-service that verify downstream services can deserialize new messages before deployment. -- Mitigates risk of future serialization incompatibility (H1).
- **[long_term]** Ensure config audit pipelines are fully reliable and capture all shards; enforce versioned config storage. -- Reduces chance of hidden config changes causing outages (H3).

## Data gaps
- No CPU, memory, or GC metrics for pricing-engine during the incident window.
- Pricing-engine application logs around the time of latency spikes are missing.
- Complete configuration change history for order-service is unavailable due to a shard failure.
- Service dependency mapping was not retrieved, leaving uncertainty about the exact call chain for quote generation.

## What would change this conclusion
- If pricing-engine resource usage metrics show CPU or memory saturation coinciding with the latency spike, the resource‑exhaustion hypothesis (H2) would become strongly supported.
- If pricing-engine logs contain protobuf deserialization errors triggered by the new market-data-service schema, the protobuf incompatibility hypothesis (H1) would be confirmed.
- If a hidden configuration change lowering the order-service timeout is discovered, the config‑change hypothesis (H3) would become the leading explanation.

## Integrity warnings (set by code)
- reporter wrote outcome=inconclusive; overridden to root_cause_identified by rule (top hypothesis is above threshold and clearly ahead)

## Investigation stats
- latency 915.91s, LLM calls 18, tokens 53321 (in 42941 / out 10380)
- tool calls 9 (0 failed, 1 partial, 0 recovered by retry), findings 10/10 verified
