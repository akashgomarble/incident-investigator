# Incident investigation report: INC-009
*System: multi_agent | outcome: **root_cause_identified** | ended: budget_exhausted:deadline*

## Summary
The investigation did not find definitive evidence that the autovacuum configuration change caused the observed order-service latency spikes and statement timeouts. While the timing aligns, missing metrics on autovacuum activity, DB lock contention, and request volume prevent a conclusive root cause determination. Immediate mitigation by reverting the autovacuum setting is recommended, alongside targeted data collection to discriminate between the leading hypothesis and alternatives.

## Probable root cause
**Autovacuum config change increased DB contention causing statement timeouts** -- config_change in `ledger-db`, triggered by `F1` (confidence 0.55, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Autovacuum config change increased DB contention causing statement timeouts | config_change | ledger-db | F1 | 0.55 (medium) | active |
| 2 | H2: Sudden surge in order traffic saturated ledger-db capacity | capacity_saturation | order-service | null | 0.32 (low) | active |
| 3 | H3: Concurrent writes from another service caused ledger-db lock contention | dependency_failure | inventory-service | null | 0.21 (low) | active |
| 4 | H4: Network latency between order-service and ledger-db increased | infrastructure_failure | network | null | 0.12 (low) | active |

### H1: Autovacuum config change increased DB contention causing statement timeouts
A configuration change on ledger-db (autovacuum_vacuum_scale_factor lowered from 0.2 to 0.1) caused more aggressive autovacuum activity, increasing I/O and lock contention. This degraded query performance, leading to statement timeouts observed by order-service, which manifested as a latency spike and p99 >5s.

**Causal chain**
1. autovacuum config change at 08:00 increased autovacuum activity [F1]
2. increased autovacuum activity caused higher DB latency and more slow queries [F14]
3. higher DB latency caused order-service INSERT statements to exceed the statement timeout [F7, F8]
4. order-service observed timeouts, causing p99 latency spike [F12, F13]

**Supporting evidence**
  - `F1` ✔ ledger-db autovacuum_vacuum_scale_factor was changed from 0.2 to 0.1 at 2026-05-05T08:00:00Z
  - `F14` ✔ ledger-db slow_queries_per_min spiked at 2026-05-05T08:56:00Z (avg 70.42, max 139.68)
  - `F7` ✔ The first ERROR log from order-service containing a timeout appears at 2026-05-05T09:00:42Z with message "ledger-db: canceling statement due to statement timeout (INSERT INTO orders) orderId=15a39d3b".
  - `F8` ✔ A rapid series of identical ERROR messages continues from 09:00:42Z through at least 09:08:57Z, each indicating "ledger-db: canceling statement due to statement timeout (INSERT INTO orders)" with different orderId values.
  - `F12` ✔ order-service p99 latency spiked at 2026-05-05T08:56:00Z, with avg 1514.78 ms and max 5607.8 ms
  - `F13` ✔ Higher‑resolution data shows the latency jump beginning at 2026-05-05T08:58:00Z (avg 2878.21 ms, max 5607.8 ms)
**Contradicting evidence**
  - (none)

### H2: Sudden surge in order traffic saturated ledger-db capacity
A rapid increase in order placement traffic around 08:55 raised the number of concurrent INSERT statements against ledger-db, saturating its processing capacity. The resulting contention caused statement timeouts and the observed latency spike in order-service.

**Causal chain**
1. order traffic surged around 08:55, increasing DB load *(assumed)*
2. ledger-db became saturated, leading to higher query latency [F14]
3. order-service INSERT statements timed out [F7, F8]
4. order-service latency p99 spiked [F12, F13]

**Supporting evidence**
  - `F14` ✔ ledger-db slow_queries_per_min spiked at 2026-05-05T08:56:00Z (avg 70.42, max 139.68)
  - `F7` ✔ The first ERROR log from order-service containing a timeout appears at 2026-05-05T09:00:42Z with message "ledger-db: canceling statement due to statement timeout (INSERT INTO orders) orderId=15a39d3b".
  - `F8` ✔ A rapid series of identical ERROR messages continues from 09:00:42Z through at least 09:08:57Z, each indicating "ledger-db: canceling statement due to statement timeout (INSERT INTO orders)" with different orderId values.
  - `F12` ✔ order-service p99 latency spiked at 2026-05-05T08:56:00Z, with avg 1514.78 ms and max 5607.8 ms
  - `F13` ✔ Higher‑resolution data shows the latency jump beginning at 2026-05-05T08:58:00Z (avg 2878.21 ms, max 5607.8 ms)
**Contradicting evidence**
  - (none)
**Assumptions**
  - There was a traffic surge (not captured in the provided data)
  - No other services contributed to DB load

### H3: Concurrent writes from another service caused ledger-db lock contention
A different service that also writes to ledger-db (e.g., inventory-service) began a heavy write workload at 08:55, creating lock contention on the orders table. Order-service INSERTs were blocked, hitting the statement timeout and causing latency spikes.

**Causal chain**
1. another service started heavy writes to ledger-db *(assumed)*
2. lock contention on ledger-db increased query latency for order-service [F14]
3. order-service INSERT statements timed out [F7, F8]
4. order-service latency spiked [F12, F13]

**Supporting evidence**
  - `F14` ✔ ledger-db slow_queries_per_min spiked at 2026-05-05T08:56:00Z (avg 70.42, max 139.68)
  - `F7` ✔ The first ERROR log from order-service containing a timeout appears at 2026-05-05T09:00:42Z with message "ledger-db: canceling statement due to statement timeout (INSERT INTO orders) orderId=15a39d3b".
  - `F8` ✔ A rapid series of identical ERROR messages continues from 09:00:42Z through at least 09:08:57Z, each indicating "ledger-db: canceling statement due to statement timeout (INSERT INTO orders)" with different orderId values.
  - `F12` ✔ order-service p99 latency spiked at 2026-05-05T08:56:00Z, with avg 1514.78 ms and max 5607.8 ms
  - `F13` ✔ Higher‑resolution data shows the latency jump beginning at 2026-05-05T08:58:00Z (avg 2878.21 ms, max 5607.8 ms)
**Contradicting evidence**
  - (none)
**Assumptions**
  - ledger-db is shared with other services (unverified in F6)
  - The other service experienced a workload increase at the same time

### H4: Network latency between order-service and ledger-db increased
A network issue (e.g., packet loss or increased round‑trip time) on the path from order-service to ledger-db began around 08:55, making each DB request take longer and eventually exceed the statement timeout, leading to the observed latency spike.

**Causal chain**
1. network latency increased between order-service and ledger-db *(assumed)*
2. DB request round‑trip time grew, causing statement timeouts [F7, F8]
3. order-service latency spiked [F12, F13]

**Supporting evidence**
  - `F7` ✔ The first ERROR log from order-service containing a timeout appears at 2026-05-05T09:00:42Z with message "ledger-db: canceling statement due to statement timeout (INSERT INTO orders) orderId=15a39d3b".
  - `F8` ✔ A rapid series of identical ERROR messages continues from 09:00:42Z through at least 09:08:57Z, each indicating "ledger-db: canceling statement due to statement timeout (INSERT INTO orders)" with different orderId values.
  - `F12` ✔ order-service p99 latency spiked at 2026-05-05T08:56:00Z, with avg 1514.78 ms and max 5607.8 ms
  - `F13` ✔ Higher‑resolution data shows the latency jump beginning at 2026-05-05T08:58:00Z (avg 2878.21 ms, max 5607.8 ms)
**Contradicting evidence**
  - (none)
**Assumptions**
  - No other network paths were affected
  - Network metrics were not collected

## Facts (verified observations)
- The autovacuum_vacuum_scale_factor on ledger-db was changed from 0.2 to 0.1 at 08:00 UTC. [F1]
- Order-service p99 latency spiked at 08:56 UTC, reaching >5 s, with a higher‑resolution jump observed at 08:58 UTC. [F12, F13]
- Ledger-db slow_queries_per_min spiked at 08:56 UTC (avg 70.42, max 139.68). [F14]
- First order-service ERROR log indicating a statement timeout appeared at 09:00:42 UTC, with a rapid series of similar errors through at least 09:08:57 UTC. [F7, F8]
- Ledger-db emitted no ERROR logs containing "timeout" between 07:00 and 10:00 UTC. [F9, F10]
- No deployments or configuration changes were recorded for order-service or ledger-db during the incident window. [F2, F3, F4]
- Ledger-db is a shared PostgreSQL datastore used by multiple services (order-service, portfolio-service, payment-service, settlement-service). [F16]

## Assumptions (not verified)
- The autovacuum configuration change resulted in increased autovacuum activity (not directly observed).
- No other services experienced a traffic surge or workload increase that could have contributed to DB contention.
- Network latency between order-service and ledger-db remained stable (no network metrics collected).

## Timeline
- `2026-05-05T08:00:00Z` Autovacuum_vacuum_scale_factor lowered from 0.2 to 0.1 on ledger-db. [F1]
- `2026-05-05T08:56:00Z` Order-service p99 latency spike begins; ledger-db slow_queries_per_min spikes. [F12, F14]
- `2026-05-05T08:58:00Z` Higher‑resolution latency data confirms spike; payment-service remains stable. [F13, F15]
- `2026-05-05T09:00:42Z` First order-service ERROR log: statement timeout on INSERT into orders. [F7]
- `2026-05-05T09:00:42Z – 09:08:57Z` Rapid series of identical timeout errors in order-service logs. [F8]

## Recommended actions
- **[immediate]** Revert ledger-db autovacuum_vacuum_scale_factor to its previous value (0.2). -- If the config change increased autovacuum activity, reverting it will immediately reduce potential I/O and lock contention, mitigating the observed timeouts. [F1]
- **[short_term]** Collect autovacuum activity metrics (workers, I/O, duration, lock wait times) for ledger-db from 08:00 to 09:30 UTC. -- Direct evidence of increased autovacuum activity is required to confirm or refute H1.
- **[short_term]** Query ledger-db lock contention and pg_stat_activity for the incident window, focusing on INSERT statements from order-service and any other services. -- Helps determine whether lock contention caused the statement timeouts and distinguishes H1 from H2/H3.
- **[short_term]** Retrieve order-service request rate (orders per second) from 08:30 to 09:30 UTC and identify any traffic surge around 08:55. -- Validates or disproves H2 (traffic surge hypothesis).
- **[short_term]** Check other services that share ledger-db (e.g., inventory-service, shipping-service) for concurrent latency spikes or increased write load during the incident window. -- Addresses H3 and ensures no hidden cross‑service contention.
- **[short_term]** Monitor network latency and packet loss between order-service and ledger-db (e.g., via ping/trace routes) for the incident period. -- Provides data to evaluate H4 (network latency hypothesis).
- **[long_term]** Implement alerting on ledger-db slow_queries_per_min and autovacuum activity thresholds to catch similar patterns early. -- Prevents recurrence by surfacing abnormal DB behavior before it impacts services.
- **[long_term]** Document and enforce a change‑control checklist for autovacuum parameter modifications, including pre‑change performance baselines and post‑change monitoring. -- Ensures future config changes are validated and monitored, reducing risk of inadvertent contention.

## Data gaps
- Missing autovacuum activity metrics (workers count, I/O usage, duration, lock wait times) for the incident window.
- Absence of ledger-db internal error or timeout metrics (error_rate, lock contention stats).
- No request rate data for order-service to confirm or deny a traffic surge.
- No visibility into other services' DB usage patterns during the incident.
- Network path metrics between order-service and ledger-db were not collected.

## What would change this conclusion
- Evidence that autovacuum activity sharply increased after the 08:00 config change and directly caused lock contention or I/O saturation on ledger-db.
- Conversely, data showing autovacuum activity remained unchanged and that a traffic surge or another service’s write load coincided with the latency spike would shift the root cause away from H1.

## Integrity warnings (set by code)
- reporter wrote outcome=inconclusive; overridden to root_cause_identified by rule (top hypothesis is above threshold and clearly ahead)

## Investigation stats
- latency 559.75s, LLM calls 30, tokens 102585 (in 85564 / out 17021)
- tool calls 21 (3 failed, 0 partial, 0 recovered by retry), findings 15/16 verified
