# Incident investigation report: INC-009
*System: multi_agent | outcome: **inconclusive** | ended: budget_exhausted:deadline*

## Summary
The investigation did not identify a definitive root cause; the two leading hypotheses—resource exhaustion on ledger-db due to a traffic surge (confidence 0.60) and ledger-db connection pool saturation (confidence 0.50)—remain plausible. Verified observations show a sharp latency increase, statement timeouts, CPU >95%, and active connections rising to ~187, but key metrics (request rate, pool configuration) are missing. Immediate mitigations to reduce load and increase pool capacity are recommended, alongside data collection to discriminate between the hypotheses.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: Resource exhaustion on ledger-db due to traffic surge | resource_exhaustion | ledger-db | null | 0.60 (medium) | active |
| 2 | H3: Ledger-db connection pool saturation | capacity_saturation | ledger-db | null | 0.50 (medium) | active |
| 3 | H1: Autovacuum config change degraded ledger-db performance | config_change | ledger-db | cfg-2903 | 0.30 (low) | active |
| 4 | H4: Downstream pricing-engine slowdown cascaded to DB overload | dependency_failure | pricing-engine | null | 0.20 (low) | active |

### H2: Resource exhaustion on ledger-db due to traffic surge
A sudden increase in order-service request volume caused a surge of queries against ledger-db, driving active connections up and CPU to >95%, which resulted in statement timeouts and the observed order-service latency spike.

**Causal chain**
1. Traffic surge on order-service increased DB query rate *(assumed)*
2. Active DB connections rose from ~95 to ~187 [F11]
3. CPU usage on ledger-db spiked to >95% [F10]
4. High CPU and connection count caused statement timeouts [F8]
5. Statement timeouts manifested as order-service p99 latency >5 s [F7, F9]

**Supporting evidence**
  - `F7` ✔ order-service p99 latency jumped from ~150 ms to >800 ms (avg 831.55 ms, max 5607.8 ms) at 2026-05-05T08:52:00Z
  - `F8` ✔ order-service error logs containing "statement timeout" first appear at 2026-05-05T09:00:42Z
  - `F9` ✔ order-service error_rate metric rose to ~9 % at 2026-05-05T09:00:00Z (previously ≤0.3 %)
  - `F10` ✔ ledger-db CPU usage spiked to 46.67 % at 2026-05-05T08:52:00Z and to >95 % from 09:00 onward
  - `F11` ✔ ledger-db active connections rose to 94.98 at 2026-05-05T08:52:00Z and to 186.98 at 2026-05-05T09:00:00Z
**Contradicting evidence**
  - (none)
**Assumptions**
  - Order-service request rate increased sharply around 08:45‑09:00

### H3: Ledger-db connection pool saturation
The ledger-db connection pool reached its maximum size, causing new queries to wait for a free slot; the resulting queuing led to statement timeouts and elevated order-service latency.

**Causal chain**
1. Connection pool limit was reached *(assumed)*
2. Active connections grew to ~187, exceeding typical pool capacity [F11]
3. Queued queries timed out, producing 'statement timeout' errors [F8]
4. Order-service observed high latency and error rate [F7, F9]

**Supporting evidence**
  - `F11` ✔ ledger-db active connections rose to 94.98 at 2026-05-05T08:52:00Z and to 186.98 at 2026-05-05T09:00:00Z
  - `F8` ✔ order-service error logs containing "statement timeout" first appear at 2026-05-05T09:00:42Z
  - `F7` ✔ order-service p99 latency jumped from ~150 ms to >800 ms (avg 831.55 ms, max 5607.8 ms) at 2026-05-05T08:52:00Z
  - `F9` ✔ order-service error_rate metric rose to ~9 % at 2026-05-05T09:00:00Z (previously ≤0.3 %)
**Contradicting evidence**
  - (none)
**Assumptions**
  - Configured pool max < 180 connections

### H1: Autovacuum config change degraded ledger-db performance
The reduction of autovacuum_vacuum_scale_factor on ledger-db (cfg-2903) caused autovacuum to run less frequently, allowing table bloat and dead tuples to accumulate, which increased query execution cost, leading to CPU saturation and statement timeouts observed by order-service.

**Causal chain**
1. cfg-2903 reduced autovacuum frequency [F2]
2. Reduced autovacuum allowed table bloat / dead tuples to increase query cost *(assumed)*
3. Higher query cost drove CPU usage >95% and active connections up, causing statement timeouts [F10, F11, F8]

**Supporting evidence**
  - `F2` ✔ Configuration change on ledger-db: autovacuum_vacuum_scale_factor reduced from 0.2 to 0.1 at 2026-05-05T08:00:00Z (id cfg-2903).
  - `F10` ✔ ledger-db CPU usage spiked to 46.67 % at 2026-05-05T08:52:00Z and to >95 % from 09:00 onward
  - `F11` ✔ ledger-db active connections rose to 94.98 at 2026-05-05T08:52:00Z and to 186.98 at 2026-05-05T09:00:00Z
  - `F8` ✔ order-service error logs containing "statement timeout" first appear at 2026-05-05T09:00:42Z
**Contradicting evidence**
  - `F15` ✔ No WARN‑level autovacuum log entries were found for ledger-db between 07:00 and 10:30 on 2026‑05‑05.
  - `F16` ✔ No WARN‑level lock‑contention log entries were found for ledger-db in the same window.
**Assumptions**
  - Table bloat increased sufficiently within <1 h after config change

### H4: Downstream pricing-engine slowdown cascaded to DB overload
A slowdown in the pricing-engine service caused order-service request handling threads to block longer, increasing the duration of DB transactions and ultimately saturating ledger-db, leading to statement timeouts.

**Causal chain**
1. Pricing-engine experienced latency spikes *(assumed)*
2. Order-service threads waited longer for pricing responses, extending DB transaction time *(assumed)*
3. Extended DB usage increased CPU and connection pressure on ledger-db [F10, F11]
4. Resulted in statement timeouts and order-service latency [F8, F7]

**Supporting evidence**
  - `F10` ✔ ledger-db CPU usage spiked to 46.67 % at 2026-05-05T08:52:00Z and to >95 % from 09:00 onward
  - `F11` ✔ ledger-db active connections rose to 94.98 at 2026-05-05T08:52:00Z and to 186.98 at 2026-05-05T09:00:00Z
  - `F8` ✔ order-service error logs containing "statement timeout" first appear at 2026-05-05T09:00:42Z
  - `F7` ✔ order-service p99 latency jumped from ~150 ms to >800 ms (avg 831.55 ms, max 5607.8 ms) at 2026-05-05T08:52:00Z
**Contradicting evidence**
  - (none)
**Assumptions**
  - Pricing-engine latency increased concurrently with DB metrics

## Facts (verified observations)
- Order-service latency (p99) jumped from ~150 ms to >800 ms at 08:52 and stayed >5 s through 10:52. [F7, F12]
- Order-service error logs contain "statement timeout" starting at 09:00:42. [F8]
- Order-service error rate rose to ~9 % at 09:00. [F9]
- Ledger-db CPU usage spiked to >95 % from 09:00 onward. [F10, F13]
- Ledger-db active connections rose from ~95 at 08:52 to ~187 at 09:00. [F11, F14]
- No WARN‑level autovacuum or lock‑contention logs were observed between 07:00 and 10:30. [F15, F16]
- Ledger-db disk usage remained steady (~71 %) and replication lag stayed ~40 ms during the incident. [F17, F18]
- A configuration change reduced autovacuum_vacuum_scale_factor on ledger-db at 08:00 (cfg-2903). [F2]
- Order-service directly accesses ledger-db (shared PostgreSQL datastore). [F3]
- Portfolio-service deployment occurred at 08:56 (dep-5905). [F1]

## Assumptions (not verified)
- Order-service request rate increased sharply around 08:45‑09:00 (required for H2).
- Ledger-db connection pool maximum size is configured below 180 connections (required for H3).
- Table bloat could have increased sufficiently within <1 h after the autovacuum config change (required for H1).
- Pricing-engine latency did not change significantly during the incident (required for H4).

## Timeline
- `2026-05-05T08:00:00Z` Autovacuum configuration change on ledger-db (vacuum_scale_factor reduced). [F2]
- `2026-05-05T08:52:00Z` Order-service p99 latency jumps to >800 ms; ledger-db CPU spikes to 46.67 %; active connections rise to ~95. [F7, F10, F11]
- `2026-05-05T08:56:00Z` Portfolio-service version v4.5.0 deployed. [F1]
- `2026-05-05T09:00:00Z` Ledger-db CPU exceeds 95 %; active connections reach ~187; order-service error rate hits ~9 %; first statement timeout logs appear. [F9, F10, F11, F14, F8]
- `2026-05-05T09:00:42Z` First "statement timeout" log entry recorded in order-service. [F8]
- `2026-05-05T10:52:00Z` Order-service latency remains >5 s; ledger-db metrics stay at high CPU and connection levels. [F12]

## Recommended actions
- **[immediate]** Temporarily throttle incoming order-service requests via api-gateway rate limiting to reduce DB load. -- Reduces the surge of queries that likely caused CPU and connection spikes, mitigating impact while root cause is clarified. [F7, F10, F11]
- **[immediate]** Increase ledger-db connection pool max size (e.g., raise from current setting to 250) and restart order-service to apply. -- If pool saturation (H3) is the cause, a larger pool will prevent queuing and timeouts.
- **[short_term]** Collect and enable order-service request rate, CPU, and memory metrics; enable detailed ledger-db connection pool usage metrics. -- Fills critical data gaps needed to confirm or refute traffic surge vs. pool saturation hypotheses.
- **[short_term]** Review and, if necessary, revert the autovacuum_vacuum_scale_factor change (cfg-2903) after confirming its impact. -- Although current evidence (no autovacuum warnings) does not strongly support H1, reverting removes a potential contributor.
- **[long_term]** Implement alerting on ledger-db CPU >80 % and active connections >150 to trigger automated scaling or throttling. -- Prevents recurrence by providing early warning of resource exhaustion. [F10, F11]
- **[long_term]** Perform capacity planning for ledger-db, including I/O and network latency monitoring, and consider scaling resources. -- Addresses underlying resource limits that could cause both H2 and H3 scenarios.

## Data gaps
- Missing order-service resource metrics (CPU %, memory %, request rate) for the incident window.
- Missing ledger-db connection pool configuration details (max size, current usage) and per‑connection wait time metrics.
- No ledger-db I/O throughput (disk read/write) data to assess possible I/O bottleneck.
- No network latency measurements between order-service and ledger-db during the incident.

## What would change this conclusion
- A verified spike in order-service request rate coinciding with the latency onset would strongly support hypothesis H2 (traffic‑driven resource exhaustion).
- Evidence that the configured connection pool max size is below 180 connections (e.g., config dump) would validate hypothesis H3 (pool saturation).
- Logs showing increased autovacuum activity or table bloat metrics would shift confidence toward hypothesis H1.

## Investigation stats
- latency 555.09s, LLM calls 19, tokens 83783 (in 70233 / out 13550)
- tool calls 13 (0 failed, 0 partial, 0 recovered by retry), findings 17/18 verified
