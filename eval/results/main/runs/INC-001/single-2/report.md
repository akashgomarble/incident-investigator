# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order placement 500s on order-service are caused by the v2.14.0 release (dep-5101, deployed 12:10Z) which added a new MarginCheckValidator to the validation chain and removed the legacy OrderRules.validateMargin(). The new validator throws NullPointerException at MarginCheckValidator.java:88 ('marginProfile is null') for F&O (FNO) orders, returning HTTP 500. Errors began at 12:12:31Z, 2.4 minutes after rollout completed, and error_rate jumped from ~0.2% to ~18% where it remains (~257 more errors through 13:58Z). Only FNO orders are affected, matching the support tickets. All order-service resources (CPU ~40%, memory ~60%, db pool ~14, restarts 0, p99 ~150ms) are flat, and all downstream dependencies (pricing-engine, exchange-gateway, portfolio-service) are clean, ruling out resource exhaustion, capacity saturation and dependency failure. The deploy has not been rolled back. Recommended fix: roll back dep-5101 / hotfix the null-marginProfile handling.

## Probable root cause
**New MarginCheckValidator in v2.14.0 dereferences null marginProfile for F&O accounts** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.85, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: New MarginCheckValidator in v2.14.0 dereferences null marginProfile for F&O accounts | bad_deployment | order-service | dep-5101 | 0.85 (high) | active |
| 2 | H2: Data issue: marginProfile data missing for some accounts, causing validator abort | data_issue | order-service | - | 0.12 (low) | active |
| 3 | H3: Dependency failure (exchange/pricing/market-data) causing order rejections | dependency_failure | unknown | - | 0.03 (low) | rejected |

### H1: New MarginCheckValidator in v2.14.0 dereferences null marginProfile for F&O accounts
Deploy dep-5101 (order-service v2.14.0) introduced a new MarginCheckValidator in the validation chain replacing the legacy OrderRules.validateMargin(). The new code dereferences a margin profile that the legacy path had handled (or fetched) differently, throwing NullPointerException at MarginCheckValidator.java:88 and returning HTTP 500 for every F&O order whose marginProfile is null. Fault originates in the order-service code release.

**Causal chain**
1. dep-5101 deploys new MarginCheckValidator and removes legacy margin validation [F1, F2]
2. New validator dereferences null marginProfile for FNO orders [F3, F6]
3. Unhandled NPE propagates to HTTP 500 on POST /v1/orders [F6]
4. Error rate jumps from ~0.2% to ~18% and stays elevated [F4, F5]

**Supporting evidence**
  - `F1` ✔ order-service deploy dep-5101 (v2.14.0, 'Refactor order validation; add margin check for F&O orders') went live at 12:10:00Z, adding 'new MarginCheckValidator' and removing legacy OrderRules.validateMargin().
  - `F2` ✔ order-service logs show the v2.14.0 rollout completed and all pods ready at 12:10:08Z.
  - `F3` ✔ The first order-service ERROR appears at 12:12:31Z, ~2.4 min after the deploy, and is a NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) for orderType=FNO.
  - `F4` ✔ order-service error_rate jumped from ~0.2% baseline to 17.95% in the bucket starting 12:12Z and stayed elevated at ~18% through 13:48Z.
  - `F5` ✔ All 191 order-service errors in 11:30-13:00Z and 257 in 12:55-13:59Z are the same two NPE patterns; the failures affect only FNO (F&O) orders, consistent with support tickets about derivatives orders.
  - `F6` ✔ The error is a Java NullPointerException 'marginProfile is null for account <id>', i.e. the new validator dereferences a null margin profile rather than a downstream timeout or pool error.
  - `F10` ✔ The deploy was never rolled back — dep-5101 status is 'live' with rolled_back_at null — and errors are still occurring at 13:58:50Z, the last data point.
**Contradicting evidence**
  - `F7` ✔ order-service resource metrics are flat across the incident: cpu_pct ~40% (mean 40.14), memory_pct ~60% (mean 60.08), db_pool_in_use ~14 connections (mean 14.04), restarts 0, latency_p99_ms ~150ms unchanged.
  - `F9` ✔ Downstream dependencies show no faults: pricing-engine error_rate flat at ~0.22% and zero ERROR log entries; exchange-gateway and portfolio-service also have zero ERROR entries.
**Assumptions**
  - The NPE stack frame MarginCheckValidator.java:88 is in code added by dep-5101 (validator is named in the deploy summary).
  - The legacy OrderRules.validateMargin() removed in the deploy previously guarded the null case.

### H2: Data issue: marginProfile data missing for some accounts, causing validator abort
The failure is driven by missing/null margin-profile records for a subset of F&O accounts (e.g. a partial data load or newly onboarded accounts), which the pre-existing code tolerated but the new validator does not. Fault would originate in the data/margin store rather than the release itself.

**Causal chain**
1. Some FNO accounts lack a marginProfile [F6]
2. New validator does not handle null and throws NPE [F3]

**Supporting evidence**
  - `F6` ✔ The error is a Java NullPointerException 'marginProfile is null for account <id>', i.e. the new validator dereferences a null margin profile rather than a downstream timeout or pool error.
**Contradicting evidence**
  - `F3` ✔ The first order-service ERROR appears at 12:12:31Z, ~2.4 min after the deploy, and is a NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) for orderType=FNO.
  - `F4` ✔ order-service error_rate jumped from ~0.2% baseline to 17.95% in the bucket starting 12:12Z and stayed elevated at ~18% through 13:48Z.
  - `F7` ✔ order-service resource metrics are flat across the incident: cpu_pct ~40% (mean 40.14), memory_pct ~60% (mean 60.08), db_pool_in_use ~14 connections (mean 14.04), restarts 0, latency_p99_ms ~150ms unchanged.
  - `F9` ✔ Downstream dependencies show no faults: pricing-engine error_rate flat at ~0.22% and zero ERROR log entries; exchange-gateway and portfolio-service also have zero ERROR entries.
**Assumptions**
  - The set of affected accounts is a specific cohort with no margin record.
  - No data-pipeline change occurred in the window (none observed).

### H3: Dependency failure (exchange/pricing/market-data) causing order rejections
A downstream dependency (pricing-engine, exchange-gateway, or the external exchange API) degraded and caused order-submit failures, as in HIST-103.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ The error is a Java NullPointerException 'marginProfile is null for account <id>', i.e. the new validator dereferences a null margin profile rather than a downstream timeout or pool error.
  - `F7` ✔ order-service resource metrics are flat across the incident: cpu_pct ~40% (mean 40.14), memory_pct ~60% (mean 60.08), db_pool_in_use ~14 connections (mean 14.04), restarts 0, latency_p99_ms ~150ms unchanged.
  - `F9` ✔ Downstream dependencies show no faults: pricing-engine error_rate flat at ~0.22% and zero ERROR log entries; exchange-gateway and portfolio-service also have zero ERROR entries.
**Rejected because:** pricing-engine error_rate stays flat at ~0.22% with zero ERROR logs; exchange-gateway and portfolio-service have zero ERROR logs; order-service latency_p99 is unchanged (~150ms) and the error is a local Java NPE, not a timeout/504.

## Facts (verified observations)
- order-service dep-5101 (v2.14.0) deployed at 12:10Z added 'new MarginCheckValidator in validation chain' and removed legacy OrderRules.validateMargin(). [F1]
- The rollout of dep-5101 completed at 12:10:08Z. [F2]
- First order-service error at 12:12:31Z is a NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) for orderType=FNO. [F3]
- order-service error_rate rose from ~0.2% to 17.95% at 12:12Z and remains ~18% to 13:48Z. [F4]
- All error logs for order-service are the two MarginCheckValidator NPE patterns, affecting only FNO orders. [F5]
- The error is a Java NullPointerException 'marginProfile is null for account <id>'. [F6]
- order-service cpu, memory, db pool, restarts and latency are flat across the incident window. [F7]
- The only config change was notification-service email.batch_size (unrelated). [F8]
- pricing-engine, exchange-gateway and portfolio-service show no errors — dependencies are healthy. [F9]
- dep-5101 was not rolled back and errors continue through 13:58:50Z. [F10]
- The earlier dep-5088 (portfolio-service 09:36Z) predates onset by ~2.5h and its service is error-free. [F11]

## Assumptions (not verified)
- MarginCheckValidator.java:88 belongs to the code added by dep-5101, per the deploy summary naming 'new MarginCheckValidator'.
- The removed legacy OrderRules.validateMargin() previously handled the null-marginProfile case.
- The marginProfile for affected accounts is genuinely absent/null in order-service's data at validation time (cause of the null is not fully determined — code defect vs data gap).

## Timeline
- `2026-03-03T09:36:00Z` portfolio-service v4.2.1 (dep-5088, grpc bump) deployed — unrelated precursor. [F11]
- `2026-03-03T11:21:00Z` notification-service config change cfg-2101 (email.batch_size 200->250) — unrelated. [F8]
- `2026-03-03T12:10:00Z` order-service v2.14.0 (dep-5101) deployed: new MarginCheckValidator, legacy margin validation removed. [F1]
- `2026-03-03T12:10:08Z` dep-5101 rollout complete, all pods ready. [F2]
- `2026-03-03T12:12:31Z` First order-service 500: NPE at MarginCheckValidator.validate line 88 for FNO order. [F3]
- `2026-03-03T12:12:00Z` error_rate jumps from ~0.2% to 17.95%. [F4]
- `2026-03-03T12:22:00Z` Incident INC-001 reported to on-call SRE. []
- `2026-03-03T13:58:50Z` Errors still occurring; dep-5101 still live, no rollback. [F10]

## Recommended actions
- **[immediate]** Roll back order-service to the pre-dep-5101 version (or hotfix MarginCheckValidator to null-guard / restore legacy OrderRules.validateMargin() fallback). -- The NPE is in the newly deployed validator and error_rate jumped immediately after rollout; rolling back removes the trigger. [F1, F3, F4]
- **[short_term]** Add null-handling and unit tests for accounts without a marginProfile in the F&O validation path before re-releasing. -- The regression is a missing null guard on marginProfile; other null-field paths in the new validator likely share the defect. [F6]
- **[short_term]** Verify the marginProfile source for affected accounts (confirm whether it is genuinely null for some accounts) to ensure the hotfix matches reality. -- Distinguishes whether a null guard suffices or the margin data itself needs correction. [F6]
- **[long_term]** Gate order-validation changes behind canary/flag and add a synthetic FNO order smoke test in CI/deploy checks. -- A 2.4-minute detection gap and an un-rolled-back live deploy let ~18% error rate persist for >1.5h. [F3, F10]

## Data gaps
- No direct source-code/diff for MarginCheckValidator.java was available, so the exact null-dereference line cannot be confirmed beyond the stack-trace frame.
- It is not determined why marginProfile is null for the affected F&O accounts (absent record vs. not fetched) — no query into the margin/ledger data store was possible.
- No margin-store/ledger-db telemetry was checked for null or missing profile records around 12:10Z.
- The 11:30-13:00Z error log query was truncated (40 of 191) and the 12:55-13:59Z query truncated (10 of 257); all sampled patterns were identical, but not every line was inspected.

## Investigation stats
- latency 36.5s, LLM calls 4, tokens 45828 (in 39487 / out 6341)
- tool calls 18 (0 failed, 0 partial, 0 recovered by retry), findings 11/11 verified
