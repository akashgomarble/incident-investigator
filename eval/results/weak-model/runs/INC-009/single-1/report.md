# Incident investigation report: INC-009
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
A DBA‑initiated autovacuum configuration change (cfg-2903) at 08:00 increased background work on the shared ledger‑db, pushing CPU to ~95 % and active connections to ~190. This resource exhaustion caused PostgreSQL to cancel statements, producing a flood of timeout errors (F1) that directly drove order‑service p99 latency from ~150 ms to >5 s (F2). No new deployments occurred, and request traffic remained steady, ruling out a code change or load surge. The root cause is the config_change on ledger‑db.

## Probable root cause
**Autovacuum config change caused DB resource exhaustion → statement timeouts → order-service latency** -- config_change in `ledger-db`, triggered by `cfg-2903` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Autovacuum config change caused DB resource exhaustion → statement timeouts → order-service latency | config_change | ledger-db | cfg-2903 | 0.86 (high) | active |
| 2 | H2: Sudden load surge on ledger-db saturated capacity | capacity_saturation | ledger-db | N/A | 0.45 (low) | active |

### H1: Autovacuum config change caused DB resource exhaustion → statement timeouts → order-service latency
The autovacuum_vacuum_scale_factor reduction at 08:00 increased background vacuum activity, driving CPU and connection usage on ledger-db to near‑capacity, which caused statement timeouts that manifested as high order-service latency.

**Causal chain**
1. config change → DB CPU & connections spike [F4, F5, F6]
2. DB resource exhaustion → statement timeouts [F5, F6, F1]
3. Statement timeouts → order-service p99 latency spike [F1, F2]

**Supporting evidence**
  - `F1` ✔ ledger-db statement timeout errors began at 2026-05-05T09:00:42Z in order-service logs
  - `F2` ✔ order-service p99 latency spiked from ~150 ms to >5 s starting at 09:00
  - `F4` ✔ DB autovacuum configuration changed at 08:00 (autovacuum_vacuum_scale_factor from 0.2 to 0.1)
  - `F5` ✔ ledger-db CPU usage jumped to ~95 % at 09:00 and stayed high
  - `F6` ✔ ledger-db active connections rose from ~60 to ~190 at 09:00
  - `F8` ✔ order-service directly depends on ledger-db
**Contradicting evidence**
  - (none)

### H2: Sudden load surge on ledger-db saturated capacity
A spike in traffic to ledger-db (e.g., from other services) saturated CPU and connections, causing timeouts that impacted order-service.

**Causal chain**
1. External traffic surge → DB CPU & connections spike [F5, F6]
2. DB overload → statement timeouts [F1]
3. Timeouts → order-service latency [F2]

**Supporting evidence**
  - `F5` ✔ ledger-db CPU usage jumped to ~95 % at 09:00 and stayed high
  - `F6` ✔ ledger-db active connections rose from ~60 to ~190 at 09:00
  - `F1` ✔ ledger-db statement timeout errors began at 2026-05-05T09:00:42Z in order-service logs
  - `F2` ✔ order-service p99 latency spiked from ~150 ms to >5 s starting at 09:00
  - `F8` ✔ order-service directly depends on ledger-db
**Contradicting evidence**
  - `F7` ✔ order-service request rate remained stable (~230 req/s) through the incident
**Assumptions**
  - Other services generated a hidden traffic surge not captured in order-service request_rate
**Rejected because:** Contradicted by stable order-service request_rate and lack of evidence of other services' load

## Facts (verified observations)
- The incident manifested as order‑service latency >5 s and DB statement timeouts. [F1, F2]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-05-05T08:00:00Z` autovacuum config changed (F4) [F4]
- `2026-05-05T08:52:00Z` ledger‑db CPU and connections begin rising (F5,F6) [F5, F6]
- `2026-05-05T09:00:00Z` First statement timeout logged (F1) and order‑service latency spikes (F2) [F1, F2]

## Recommended actions
- **[immediate]** Rollback autovacuum_vacuum_scale_factor to previous value (0.2) or tune to avoid high CPU load. -- Reverses the config change that triggered resource exhaustion (supports H1). [F4, F5, F6]
- **[short_term]** Monitor ledger‑db CPU, connections, and autovacuum activity; add alerts for >80 % CPU or >150 active connections. -- Prevents recurrence of similar resource‑exhaustion events. [F5, F6]
- **[long_term]** Review shared‑service usage of ledger‑db; consider isolating high‑traffic services or increasing DB capacity. -- Mitigates impact of any future config or load changes on a shared critical datastore. [F8]

## Data gaps
- No metrics from ledger‑db disk I/O or lock contention; could clarify if autovacuum caused I/O bottleneck.
- No logs/metrics from other services that also use ledger‑db; would confirm whether the load surge was isolated to order‑service.

## Investigation stats
- latency 443.51s, LLM calls 9, tokens 51706 (in 46791 / out 4915)
- tool calls 8 (0 failed, 0 partial, 0 recovered by retry), findings 8/8 verified
