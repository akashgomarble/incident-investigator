# Incident investigation report: INC-009
*System: single_agent | outcome: **inconclusive** | ended: agent_submitted*

## Summary
Order placement latency rose sharply after 08:54 with many ledger‑db statement‑timeout errors and a jump in the order‑service DB‑pool usage. A DBA‑initiated autovacuum configuration change occurred at 08:00, which temporally precedes the degradation. No code changes touched order‑service. Two plausible root‑cause narratives remain: the autovacuum change degraded ledger‑db performance (config_change) or the resulting slow INSERTs exhausted the order‑service DB pool (resource_exhaustion). Without direct ledger‑db performance metrics, the evidence cannot definitively favour one hypothesis.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Aggressive autovacuum caused ledger-db slowdown → statement timeouts → order-service latency | config_change | ledger-db | cfg-2903 | 0.68 (medium) | active |
| 2 | H2: Order‑service DB‑pool exhaustion caused by slow INSERTs | resource_exhaustion | order-service | none | 0.55 (medium) | active |

### H1: Aggressive autovacuum caused ledger-db slowdown → statement timeouts → order-service latency
The autovacuum_vacuum_scale_factor change made ledger-db run more aggressive vacuuming, increasing I/O and lock contention, which slowed INSERT statements, causing statement timeouts and a cascade of high DB‑pool usage and p99 latency in order‑service.

**Causal chain**
1. autovacuum change → DB slowdown [F5]
2. DB slowdown → statement timeouts [F1]
3. timeouts → connection pool exhaustion & latency spike [F2, F3]

**Supporting evidence**
  - `F5` ✘ unverified DBAs changed ledger-db autovacuum_vacuum_scale_factor from 0.2 to 0.1 at 08:00
  - `F1` ✔ order-service logs show repeated statement timeout errors from ledger-db starting at 09:00:42
  - `F2` ✔ order-service p99 latency spiked from ~150 ms to >5 s starting at the 08:54 bucket
  - `F3` ✔ order-service DB connection pool usage jumped from ~14 to ~45 connections beginning at 08:54
  - `F7` ✔ order-service depends on ledger-db (shared datastore) as per service graph
**Contradicting evidence**
  - (none)
**Assumptions**
  - ledger-db performance degraded due to more aggressive autovacuum; no other concurrent heavy load on ledger-db

### H2: Order‑service DB‑pool exhaustion caused by slow INSERTs
Slow INSERT statements against ledger-db kept DB connections occupied, inflating the order‑service connection pool usage and leading to statement timeouts and high request latency.

**Causal chain**
1. slow INSERTs → connections stay busy [F1]
2. busy connections → pool usage spikes [F3]
3. pool saturation → request latency spikes [F2]

**Supporting evidence**
  - `F1` ✔ order-service logs show repeated statement timeout errors from ledger-db starting at 09:00:42
  - `F2` ✔ order-service p99 latency spiked from ~150 ms to >5 s starting at the 08:54 bucket
  - `F3` ✔ order-service DB connection pool usage jumped from ~14 to ~45 connections beginning at 08:54
  - `F7` ✔ order-service depends on ledger-db (shared datastore) as per service graph
**Contradicting evidence**
  - `F4` ✔ order-service request rate stayed roughly constant (~230 req/s) throughout the incident window
**Assumptions**
  - the pool usage increase is the primary driver of latency rather than a symptom

## Facts (verified observations)
- The incident manifested as high p99 latency and statement timeouts in order‑service. [F1, F2]

## Assumptions (not verified)
- The autovacuum change is the only recent configuration change affecting ledger‑db.
- No other hidden deployments or feature‑flag flips impacted order‑service.

## Timeline
- `2026-05-05T08:00:00Z` autovacuum_vacuum_scale_factor reduced on ledger‑db [F5]
- `2026-05-05T08:54:00Z` order‑service p99 latency spikes; DB pool usage rises [F2, F3]
- `2026-05-05T09:00:42Z` first ledger‑db statement timeout logged [F1]

## Recommended actions
- **[immediate]** Collect ledger‑db performance metrics (CPU, I/O, lock wait times) for the incident window. -- Direct evidence is needed to confirm whether the autovacuum change degraded DB performance.
- **[short_term]** Temporarily revert autovacuum_vacuum_scale_factor to its previous value (0.2) and monitor latency. -- If latency improves, it validates the config_change hypothesis.
- **[short_term]** Increase order‑service DB‑pool size or add connection‑retry back‑off while investigating root cause. -- Mitigates impact of pool exhaustion regardless of underlying cause.

## Data gaps
- No telemetry from ledger‑db (CPU, I/O, lock stats, query latency) for the period.
- No autovacuum logs to see actual vacuum activity and its impact on concurrent transactions.

## Investigation stats
- latency 499.58s, LLM calls 8, tokens 41130 (in 35770 / out 5360)
- tool calls 7 (0 failed, 0 partial, 0 recovered by retry), findings 6/7 verified
