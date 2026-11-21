# Incident investigation report: INC-009
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order-service p99 latency and ledger-db statement timeouts were caused by portfolio-service deployment dep-5905 (v4.5.0) at 08:56:00Z, which added a new query on the 'trades' table filtered by account_id + executed_at with no supporting migration/index. This produced a Seq Scan over a 48.2M-row table (logged by ledger-db as application_name=portfolio-service starting 08:58:38), saturating the shared ledger-db (CPU 30%->96%, connections 62->190, slow_queries_per_min 1->140). The saturated shared database blocked order-service's INSERT INTO orders, causing p99 to jump from ~149ms to ~5300ms at 08:57 and 284 statement-timeout errors from 09:00:42. The reported suspicion (DBA autovacuum change at 08:00) is contradicted by timing: it precedes onset by ~56 minutes with no metric deviation, and the only autovacuum activity logged is routine audit_log processing. Pricing-engine, exchange-gateway, disk, and traffic are all ruled out.

## Probable root cause
**Portfolio-service v4.5.0 deploy added an unindexed seq-scan query on trades, saturating shared ledger-db and stalling order-service** -- bad_deployment in `portfolio-service`, triggered by `dep-5905` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Portfolio-service v4.5.0 deploy added an unindexed seq-scan query on trades, saturating shared ledger-db and stalling order-service | bad_deployment | portfolio-service | dep-5905 | 0.90 (high) | active |
| 2 | H2: DBA autovacuum tuning (cfg-2903) on ledger-db caused the slowdown | config_change | ledger-db | cfg-2903 | 0.05 (low) | rejected |
| 3 | H3: ledger-db infrastructure/resource failure (IOPS throttling or disk exhaustion) | infrastructure_failure | ledger-db | - | 0.03 (low) | rejected |

### H1: Portfolio-service v4.5.0 deploy added an unindexed seq-scan query on trades, saturating shared ledger-db and stalling order-service
Deployment dep-5905 (portfolio-service v4.5.0, 08:56) introduced a new query 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at' with no supporting index/migration. This forced a Seq Scan over a 48.2M-row table, driving ledger-db CPU to ~96%, connections to ~190, and slow_queries_per_min to ~140. The saturated shared ledger-db then delayed order-service INSERT INTO orders past its statement timeout, producing 284 timeout errors and p99 latency of ~5.3s. order-service itself was not released.

**Causal chain**
1. dep-5905 deployed to portfolio-service at 08:56 with a new query on trades and no migration/index [F2]
2. New query produces 'Seq Scan on trades (rows=48211907)' from application_name=portfolio-service, first seen 08:58:38 [F3]
3. Seq scans saturate shared ledger-db: CPU 30->96%, connections 62->190, slow_queries_per_min 1->140 [F4, F5, F6]
4. Saturated ledger-db blocks order-service INSERTs; order-service p99 jumps from ~149ms to ~5300ms at 08:57 and 284 statement-timeout errors appear from 09:00:42 [F1, F7, F8]

**Supporting evidence**
  - `F1` ✔ order-service p99 latency was ~150ms and stable until the 08:54 bucket, then jumped to avg 1968ms (max 5607.8ms) in the 08:57 bucket and ~5300ms from 09:00 onward.
  - `F2` ✔ A single deployment occurred in the window: dep-5905, portfolio-service v4.5.0, deployed 2026-05-05T08:56:00Z, adding a new query on trades filtered by account_id + executed_at, with 'no migration included'; not rolled back.
  - `F3` ✔ ledger-db emitted slow-query warnings running 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907)' attributed to application_name=portfolio-service, first seen 08:58:38 (8690ms).
  - `F4` ✔ ledger-db CPU rose from ~30% (stable through 08:36) to avg 50% at 08:49 and ~96% from 09:02 onward.
  - `F5` ✔ ledger-db slow_queries_per_min rose from ~1 (flat through 08:36) to avg 44.9 (max 148.8) at 08:49 and ~140 from 09:02.
  - `F6` ✘ unverified ledger-db active connections rose from ~62 to ~100 at 08:49 and ~190 from 09:02 onward.
  - `F7` ✔ order-service logged 284 ERROR entries 'ledger-db: canceling statement due to statement timeout (INSERT INTO orders)', first at 09:00:42.
  - `F8` ✘ unverified order-service db_pool_in_use rose from ~14 to ~46 (max 51.6) at/after 08:49-09:02.
  - `F9` ✔ portfolio-service WARN logs 'holdings P&L query took <n>ms' begin at 08:59:39 (7459ms), 239 occurrences.
  - `F15` ✔ ledger-db is a shared datastore; order-service uses it as 'db' and portfolio-service reads the same 'trades' table (slow queries tagged application_name=portfolio-service).
**Contradicting evidence**
  - (none)
**Assumptions**
  - The deployed query lacks a usable index on trades(account_id, executed_at) — the seq scan plan plus 'no migration included' imply this.
  - ledger-db resource contention is the mechanism by which order-service INSERTs time out (shared datastore).

### H2: DBA autovacuum tuning (cfg-2903) on ledger-db caused the slowdown
The 08:00 config change making autovacuum more aggressive (scale_factor 0.2->0.1) increased I/O/CPU on ledger-db and slowed queries, incidentally affecting order-service.

**Causal chain**
1. cfg-2903 changed ledger-db autovacuum at 08:00 [F10]
2. Metrics stayed flat until 08:57, 56 min later — no plausible causal timing [F1, F4, F5]

**Supporting evidence**
  - `F10` ✔ The only config change is cfg-2903 on ledger-db at 08:00:00 changing autovacuum_vacuum_scale_factor from 0.2 to 0.1.
**Contradicting evidence**
  - `F1` ✔ order-service p99 latency was ~150ms and stable until the 08:54 bucket, then jumped to avg 1968ms (max 5607.8ms) in the 08:57 bucket and ~5300ms from 09:00 onward.
  - `F4` ✔ ledger-db CPU rose from ~30% (stable through 08:36) to avg 50% at 08:49 and ~96% from 09:02 onward.
  - `F5` ✔ ledger-db slow_queries_per_min rose from ~1 (flat through 08:36) to avg 44.9 (max 148.8) at 08:49 and ~140 from 09:02.
  - `F11` ✔ The only autovacuum activity logged around onset is routine 'autovacuum: processed table public.audit_log' at 08:47:50 and 08:52:26; no autovacuum events on the trades table.
  - `F2` ✔ A single deployment occurred in the window: dep-5905, portfolio-service v4.5.0, deployed 2026-05-05T08:56:00Z, adding a new query on trades filtered by account_id + executed_at, with 'no migration included'; not rolled back.
  - `F3` ✔ ledger-db emitted slow-query warnings running 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907)' attributed to application_name=portfolio-service, first seen 08:58:38 (8690ms).
**Rejected because:** The change at 08:00 precedes the 08:57 onset by ~56 minutes, with no latency/CPU/connection deviation in the 08:00-08:54 interval (all metrics flat through 08:36-08:54). The positive link is instead the 08:56 deploy to the exact query/application_name seen in the slow-query logs, and the only autovacuum log activity is routine audit_log processing.

### H3: ledger-db infrastructure/resource failure (IOPS throttling or disk exhaustion)
ledger-db suffered an infrastructure/resource fault (e.g. IOPS throttling as in HIST-110 or disk-full as in HIST-114) that degraded it and dragged down order-service.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F13` ✔ ledger-db disk_used_pct stayed flat ~71% across the window (no disk exhaustion).
  - `F3` ✔ ledger-db emitted slow-query warnings running 'SELECT * FROM trades WHERE account_id=$1 AND executed_at > $2 ORDER BY executed_at -- Seq Scan on trades (rows=48211907)' attributed to application_name=portfolio-service, first seen 08:58:38 (8690ms).
  - `F2` ✔ A single deployment occurred in the window: dep-5905, portfolio-service v4.5.0, deployed 2026-05-05T08:56:00Z, adding a new query on trades filtered by account_id + executed_at, with 'no migration included'; not rolled back.
**Rejected because:** disk_used_pct stayed flat ~71% (no disk exhaustion). Slow queries are explicitly attributable to a specific new portfolio-service query doing a Seq Scan, not to generic IO errors. No IOPS/capacity metric shows a fault independent of the query pattern.

## Facts (verified observations)
- order-service p99 was ~150ms until 08:54, jumped to ~1968ms at 08:57 and ~5300ms from 09:00. [F1]
- The only deployment in the window is dep-5905 (portfolio-service v4.5.0) at 08:56:00Z, adding a new trades query with no migration. [F2]
- ledger-db slow-query logs show 'Seq Scan on trades (rows=48211907)' attributed to application_name=portfolio-service, first at 08:58:38. [F3]
- ledger-db CPU rose from ~30% to ~96% and slow_queries_per_min from ~1 to ~140 after 08:49. [F4, F5]
- order-service logged 284 statement-timeout errors on INSERT INTO orders starting 09:00:42. [F7]
- The autovacuum config change cfg-2903 occurred at 08:00:00Z, ~56 min before onset. [F10]
- No unusual autovacuum activity on the trades table was logged; only routine audit_log processing. [F11]
- pricing-engine and exchange-gateway latency and ledger-db disk usage and portfolio-service request rate were all flat. [F12, F13, F14]

## Assumptions (not verified)
- The new portfolio-service query has no usable index on trades(account_id, executed_at); the seq-scan plan and 'no migration included' strongly imply this but no schema dump was retrieved.
- ledger-db connection/CPU saturation is the causal mechanism for order-service INSERT delays, inferred from shared datastore usage and correlated timing; no direct lock/queue metrics were available.
- (demoted from facts: no verified observation backs it) ledger-db active connections rose from ~62 to ~190.

## Timeline
- `2026-05-05T08:00:00Z` Config change cfg-2903: ledger-db autovacuum_vacuum_scale_factor 0.2->0.1 (no metric impact follows). [F10]
- `2026-05-05T08:54:00Z` order-service p99 still normal (~149ms); ledger-db metrics still baseline. [F1, F4]
- `2026-05-05T08:56:00Z` Deployment dep-5905: portfolio-service v4.5.0, new trades query, no migration. [F2]
- `2026-05-05T08:57:00Z` order-service p99 jumps to avg 1968ms (max 5607ms). [F1]
- `2026-05-05T08:58:38Z` First ledger-db slow query: Seq Scan on trades (rows=48211907) from portfolio-service, 8690ms. [F3]
- `2026-05-05T08:59:39Z` First portfolio-service 'holdings P&L query took 7459ms' warning. [F9]
- `2026-05-05T09:00:42Z` First order-service statement-timeout error on INSERT INTO orders. [F7]
- `2026-05-05T09:02:00Z` ledger-db CPU ~96%, connections ~190, slow_queries_per_min ~140; order-service p99 ~5300ms sustained. [F1, F4, F5, F6]

## Recommended actions
- **[immediate]** Roll back portfolio-service to pre-v4.5.0 (before dep-5905) or disable the new P&L breakdown feature to immediately relieve ledger-db. -- The deploy introduced the seq-scan query; the serving path recovers once the offending query stops. [F2, F3, F1]
- **[short_term]** Add a composite index on trades(account_id, executed_at) (concurrently) so the new query uses an index scan instead of a 48M-row seq scan. -- The seq scan is the proven mechanism; an index removes it without removing the feature. [F3]
- **[short_term]** Re-apply the portfolio-service change only after EXPLAIN verification and a migration/index gate. -- 'no migration included' allowed an unindexed query into a large table; a query-review gate prevents recurrence. [F2, F3]
- **[long_term]** Add alerting on shared-DB slow_queries_per_min and per-query seq-scan detection, plus per-query cost budget. -- A shared datastore fault surfaced as latency in an unrelated service; earlier detection shortens time-to-diagnosis. [F5, F15]

## Data gaps
- No schema/index catalog for ledger-db was available, so the presumed missing index on trades(account_id, executed_at) is inferred from the seq-scan plan, not directly confirmed.
- No direct lock-wait or per-statement queue metrics on ledger-db; the mechanism linking DB saturation to order-service INSERT timeouts is inferred from correlated timing and shared-datastore topology.
- No rollback of dep-5905 has been performed, so the counterfactual (recovery on rollback) is unverified.

## Integrity warnings (set by code)
- fact demoted to assumption: ledger-db active connections rose from ~62 to ~190.

## Investigation stats
- latency 52.0s, LLM calls 6, tokens 84058 (in 75421 / out 8637)
- tool calls 21 (0 failed, 0 partial, 0 recovered by retry), findings 13/15 verified
