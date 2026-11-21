# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order placement on POST /v1/orders began returning HTTP 500 at ~12:12Z, ~2 minutes after order-service deployment dep-5101 (v2.14.0) completed its rollout at 12:10:08Z. The deploy added a new MarginCheckValidator and removed the legacy OrderRules.validateMargin(); its signature error is 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO' / 'marginProfile is null for account <n>', matching the reported derivatives (F&O) failures. The deploy is the only change correlated with onset and was never rolled back, so error_rate stayed at ~18.5% (vs ~0.2% before) through the latest data. All alternatives were excluded: latency/CPU/memory/db-pool/restarts and traffic are flat (not resource/capacity), and downstream dependencies (pricing-engine, exchange-gateway, portfolio-service, ledger-db) are healthy with zero error logs (not dependency failure). Root cause is a bad deployment in order-service.

## Probable root cause
**order-service deploy dep-5101 v2.14.0 introduced a NPE-prone MarginCheckValidator for F&O orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.92, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service deploy dep-5101 v2.14.0 introduced a NPE-prone MarginCheckValidator for F&O orders | bad_deployment | order-service | dep-5101 | 0.92 (high) | active |
| 2 | H2: Downstream/dependency failure (portfolio/market-data providing null margin profile) | dependency_failure | portfolio-service | - | 0.05 (low) | rejected |
| 3 | H3: Resource exhaustion or capacity saturation on order-service | resource_exhaustion | order-service | - | 0.03 (low) | rejected |
| 4 | H4: Config/feature-flag change caused the failures | config_change | order-service | - | 0.02 (low) | rejected |

### H1: order-service deploy dep-5101 v2.14.0 introduced a NPE-prone MarginCheckValidator for F&O orders
The v2.14.0 refactor that replaced legacy OrderRules.validateMargin() with a new MarginCheckValidator left the margin profile unpublished/null for F&O orders, so MarginCheckValidator.validate() dereferences a null marginProfile and throws NullPointerException, causing POST /v1/orders to return HTTP 500. Because only F&O orders flow through the new code path, the failure is scoped to derivatives orders (~18% of traffic), and the deploy was never rolled back, so the failure persists.

**Causal chain**
1. dep-5101 v2.14.0 deployed to order-service at 12:10:00Z, rollout complete 12:10:08Z [F1]
2. New MarginCheckValidator added to validation chain; legacy margin validation removed [F1]
3. Threshold: order-service error_rate rises 0.2% -> 18.5% in the 12:12 bucket [F3]
4. First MarginCheckValidator NPE logged at 12:12:31Z, only ~2.3 min after rollout [F2]
5. NPE 'marginProfile is null' is thrown on F&O (derivatives) orders, matching the reported derivatives-order 500s [F2]
6. No rollback (status live) so errors continue through 13:58Z [F4, F5]

**Supporting evidence**
  - `F1` ✔ order-service deployment dep-5101 (v2.14.0) was deployed at 2026-03-03T12:10:00Z and its rollout completed at 12:10:08Z; its change summary is 'Refactor order validation; add margin check for F&O orders' with changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'.
  - `F2` ✔ order-service began throwing NullPointerException in MarginCheckValidator.validate at 12:12:31Z (first occurrence), with dominant patterns 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO' and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>'.
  - `F3` ✔ order-service error_rate jumped from ~0.2% (buckets 11:30–12:05) to 18.49% in the 12:12 bucket and stayed elevated (~17.9–18.97%) through 13:57, with no recovery.
  - `F4` ✔ The errors continue at high volume through the latest data: 12:59–13:59 window logged 238 ERROR entries, same MarginCheckValidator NPE patterns first_seen 12:59:08.
  - `F5` ✔ Deployment dep-5101 was NOT rolled back: status 'live', rolled_back_at null. The only other recent deployment was portfolio-service dep-5088 at 09:36 (dependency bumps, grpc 1.62->1.63), which predates onset by ~2.5h and was followed by no impact.
  - `F6` ✔ order-service resource/load metrics are flat across the incident: cpu_pct ~40%, memory_pct ~60%, db_pool_in_use ~14 connections, request_rate ~232 req/s, latency_p99 ~150ms, restarts 0 — no saturation, exhaustion or restart.
  - `F7` ✔ Downstream dependencies are healthy: pricing-engine error_rate ~0.22%, exchange-gateway error_rate ~0.21% with zero ERROR logs, portfolio-service error_rate ~0.21% with zero ERROR logs, ledger-db slow_queries_per_min ~1.0 and cpu_pct ~30% — all unchanged across the incident window.
  - `F8` ✔ api-gateway error_rate rose from ~0.2% to ~3.4% starting in the 12:10 bucket, consistent with propagated order-service failures (its own metrics and downstreams otherwise normal).
  - `F9` ✔ The only config/feature-flag change in the window was notification-service cfg-2101 (email.batch_size 200->250) at 11:21Z, unrelated to order-service. order-service log shows 'config reloaded (no changes)' at 12:07:51Z.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The marginProfile was previously populated for F&O orders under the removed legacy path, so this is a regression in the new validator rather than a pre-existing data gap
  - No exogenous change to upstream margin data occurred at ~12:12 (not directly observed)

### H2: Downstream/dependency failure (portfolio/market-data providing null margin profile)
A downstream service (e.g. portfolio-service or market-data-service) stopped returning margin profiles, leaving marginProfile null and triggering the NPE.

**Causal chain**
1. portfolio-service dep-5088 v4.2.1 deployed 09:36Z [F5]
2. portfolio-service error_rate flat ~0.21% and zero ERROR logs [F7]
3. dep-5088 predates onset by ~2.5h with no correlated impact [F5]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F5` ✔ Deployment dep-5101 was NOT rolled back: status 'live', rolled_back_at null. The only other recent deployment was portfolio-service dep-5088 at 09:36 (dependency bumps, grpc 1.62->1.63), which predates onset by ~2.5h and was followed by no impact.
  - `F7` ✔ Downstream dependencies are healthy: pricing-engine error_rate ~0.22%, exchange-gateway error_rate ~0.21% with zero ERROR logs, portfolio-service error_rate ~0.21% with zero ERROR logs, ledger-db slow_queries_per_min ~1.0 and cpu_pct ~30% — all unchanged across the incident window.
**Assumptions**
  - marginProfile originates from portfolio-service (dependency graph from order-service does not show an explicit portfolio/margin edge)
**Rejected because:** portfolio-service and all order-service downstreams (pricing-engine, exchange-gateway, ledger-db) show flat error/latency/CPU and zero error logs; dep-5088 predates onset with no impact. No upstream-provider degradation observed.

### H3: Resource exhaustion or capacity saturation on order-service
CPU/memory/connection-pool exhaustion or traffic outgrowing capacity caused order failures.

**Causal chain**
1. order-service cpu ~40%, memory ~60%, db_pool ~14 conns, request_rate ~232/s all flat [F6]
2. latency_p99 ~150ms flat and restarts 0 [F6]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ order-service resource/load metrics are flat across the incident: cpu_pct ~40%, memory_pct ~60%, db_pool_in_use ~14 connections, request_rate ~232 req/s, latency_p99 ~150ms, restarts 0 — no saturation, exhaustion or restart.
**Rejected because:** All resource/load metrics are flat across onset; no saturation, exhaustion, latency increase or restarts. The fault began exactly at deploy time, not with load.

### H4: Config/feature-flag change caused the failures
A configuration or feature-flag change disabled margin-profile population or enabled the new validation path incorrectly.

**Causal chain**
1. Only config change in window is notification-service email.batch_size (cfg-2101) [F9]
2. order-service logged 'config reloaded (no changes)' at 12:07:51Z [F9]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ The only config/feature-flag change in the window was notification-service cfg-2101 (email.batch_size 200->250) at 11:21Z, unrelated to order-service. order-service log shows 'config reloaded (no changes)' at 12:07:51Z.
**Rejected because:** The sole config change is on notification-service and is unrelated; order-service reported no config changes. Fault correlates with a code deployment, not a config change.

## Facts (verified observations)
- order-service v2.14.0 (dep-5101) deployed 12:10:00Z, rollout complete 12:10:08Z, change = new MarginCheckValidator added, legacy OrderRules.validateMargin() removed. [F1]
- First MarginCheckValidator NullPointerException logged at 12:12:31Z with message 'marginProfile is null', orderType=FNO. [F2]
- order-service error_rate rose from ~0.2% to 18.49% at the 12:12 bucket and remained ~18% through 13:57Z. [F3, F4]
- dep-5101 status is 'live' with rolled_back_at null; no rollback occurred. [F5]
- order-service cpu_pct, memory_pct, db_pool_in_use, request_rate, latency_p99_ms and restarts are all flat across the incident. [F6]
- pricing-engine, exchange-gateway, portfolio-service and ledger-db are all healthy (flat error_rate, no error logs). [F7]
- Only config change was notification-service email.batch_size; order-service had no config change. [F9]
- api-gateway error_rate rose only to ~3.4%, consistent with propagated order-service failures. [F8]

## Assumptions (not verified)
- Under the removed legacy OrderRules.validateMargin(), F&O margin profiles were handled correctly, so the NPE represents a regression introduced by dep-5101 rather than a pre-existing latent data gap.
- No simultaneous exogenous change affected upstream margin data at ~12:12Z (not directly observable from available telemetry).
- The ~18% error rate corresponds to the F&O/derivatives subset of order traffic, consistent with the reported 'mostly derivatives orders'.

## Timeline
- `2026-03-03T09:36:00Z` portfolio-service dep-5088 (v4.2.1) deployed; no subsequent impact. [F5]
- `2026-03-03T11:21:00Z` notification-service config change cfg-2101 (email.batch_size 200->250); unrelated. [F9]
- `2026-03-03T12:07:51Z` order-service logs 'config reloaded (no changes)'. [F9]
- `2026-03-03T12:10:00Z` order-service deployment dep-5101 (v2.14.0) begins; new MarginCheckValidator added. [F1]
- `2026-03-03T12:10:08Z` dep-5101 rollout complete, all pods ready. [F1]
- `2026-03-03T12:12:00Z` order-service error_rate jumps from ~0.2% to 18.49%. [F3]
- `2026-03-03T12:12:31Z` First MarginCheckValidator NPE (marginProfile null, orderType=FNO) logged. [F2]
- `2026-03-03T12:22:00Z` Incident reported by on-call SRE (SEV-1). []
- `2026-03-03T13:58:50Z` Same NPE errors still occurring at high volume; no rollback. [F4]

## Recommended actions
- **[immediate]** Roll back order-service from v2.14.0 (dep-5101) to the previous version immediately. -- The failure began at this deploy, the deploy is still 'live' with no rollback, and persisting errors are caused by the new MarginCheckValidator code path. [F1, F2, F3, F5]
- **[short_term]** Fix MarginCheckValidator to handle a null/absent marginProfile for F&O orders (restore legacy validateMargin behavior or publish margin profiles before validation). -- The NPE is 'marginProfile is null for account <n>'; null handling is missing in the new validator. [F2]
- **[short_term]** Confirm whether margin profiles for F&O accounts are populated at all (check the source of marginProfile) to ensure the fix addresses the true null source. -- Distinguishes a validator code bug from a missing-data dependency without blocking the rollback. [F2]
- **[long_term]** Add a regression test covering F&O order validation with an unpopulated margin profile, plus a canary/staged rollout gate that watches order-service error_rate. -- The change shipped straight to all pods (rollout complete) and error_rate alert fired only after ~12 minutes of failures. [F1, F3]

## Data gaps
- Exact source of 'marginProfile' for F&O orders is not shown in telemetry; the order-service dependency graph shows no explicit portfolio/margin-service edge, so where the null originates (code regression vs missing upstream data) is inferred, not proven.
- No post-rollback data exists yet, so recovery after reverting dep-5101 is unverified.
- ledger-db error_rate metric does not exist (only connections/cpu/disk/replication/slow_queries were available), so DB error behavior could not be checked directly; slow_queries and cpu were normal.
- The single GC-pause WARN at 12:37Z and portfolio-service WARNs were not investigated further as they do not correlate with onset.

## Investigation stats
- latency 71.81s, LLM calls 6, tokens 85555 (in 77052 / out 8503)
- tool calls 25 (1 failed, 0 partial, 0 recovered by retry), findings 11/11 verified
