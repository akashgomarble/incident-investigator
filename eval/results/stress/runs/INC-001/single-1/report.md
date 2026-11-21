# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order placement 500s are caused by deployment dep-5101 (order-service v2.14.0, deployed 12:10:00Z, 'Refactor order validation; add margin check for F&O orders'). The new MarginCheckValidator dereferences a null marginProfile for orderType=FNO orders, throwing NullPointerException at MarginCheckValidator.java:88 and returning HTTP 500. Errors start at 12:12:31Z (~2 min after rollout) and lift error_rate from ~0.2% to ~18.5%, sustained through the latest data. All competing causes are ruled out: no order-service config change, flat CPU/memory/latency/db-pool, flat request rate, and a clean downstream dependency (pricing-engine at 0.2% errors with no error logs). Recommendation: roll back dep-5101 or hotfix the null handling in MarginCheckValidator.

## Probable root cause
**bad_deployment: order-service v2.14.0 MarginCheckValidator dereferences a null marginProfile for F&O orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: bad_deployment: order-service v2.14.0 MarginCheckValidator dereferences a null marginProfile for F&O orders | bad_deployment | order-service | dep-5101 | 0.90 (high) | active |
| 2 | H2: data_issue: margin profile records missing/not populated upstream | data_issue | ledger-db | - | 0.15 (low) | rejected |
| 3 | H3: config_change or resource/capacity saturation on order-service | resource_exhaustion | order-service | - | 0.05 (low) | rejected |

### H1: bad_deployment: order-service v2.14.0 MarginCheckValidator dereferences a null marginProfile for F&O orders
Deploy dep-5101 (v2.14.0) introduced a new MarginCheckValidator intended to check margin for F&O orders. It dereferences a marginProfile object that is null (or a value that must be explicitly loaded) for FNO orders, throwing NullPointerException at MarginCheckValidator.java:88 and causing HTTP 500 rejections. The legacy OrderRules.validateMargin() that previously handled this path was removed, so the new code is now on the hot path for every F&O order.

**Causal chain**
1. dep-5101 (v2.14.0) adds MarginCheckValidator and removes legacy OrderRules.validateMargin() [F4]
2. rollout completes 12:10:08; first NPE 12:12:31 (~2 min later) [F5, F2]
3. new validator dereferences null marginProfile for orderType=FNO -> NullPointerException at MarginCheckValidator.java:88 [F2, F3, F11]
4. exception propagates to POST /v1/orders as HTTP 500, lifting error_rate to ~18% [F1, F3]

**Supporting evidence**
  - `F1` ✔ order-service error_rate jumped from ~0.2% to ~18.3% between the 12:00 and 12:12 buckets and stayed elevated (~18-20%) through 13:48.
  - `F2` ✔ First ERROR in order-service is at 12:12:31, a NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) for orderType=FNO; 426 ERROR entries follow.
  - `F3` ✔ The dominant error patterns are the NPE in MarginCheckValidator (224 occurrences) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202 occurrences).
  - `F4` ✔ Deployment dep-5101 (order-service v2.14.0) was deployed at 12:10:00 with summary 'Refactor order validation; add margin check for F&O orders', adding a new MarginCheckValidator and removing legacy OrderRules.validateMargin(); status live, never rolled back.
  - `F5` ✔ order-service logged 'deployment dep-5101 (v2.14.0) rollout complete, all pods ready' at 12:10:08, then the first NPE at 12:12:31 — errors began ~2 minutes after rollout completed.
  - `F6` ✔ latency_p99_ms was flat at ~150ms across the whole window, including after 12:12 (e.g. 151.86 at 12:12, 147.54 at 12:36).
  - `F7` ✔ db_pool_in_use stayed flat at ~14 connections (max 15.24) throughout, showing no connection-pool exhaustion.
  - `F8` ✔ cpu_pct stayed flat at ~40% and request_rate flat at ~232 req/s across the window — no load spike or CPU saturation.
  - `F9` ✔ The only config change in the window across all services was on notification-service (cfg-2101, 'Tune email batching', 11:21) — unrelated to order-service; order-service had zero config changes.
  - `F10` ✔ Downstream dependency pricing-engine error_rate stayed flat at ~0.22% with zero WARN/ERROR logs in the window; no dependency-side errors.
  - `F11` ✔ Errors are scope-limited to orderType=FNO (derivatives) orders; only FNO orders produce the NPE, consistent with a new margin check gated to F&O orders.
  - `F12` ✔ The error is deterministic across many distinct accounts (99733, 10578, 79537, 57680, 32892, ...) — essentially every FNO order fails, not a subset tied to specific bad data.
**Contradicting evidence**
  - (none)
**Assumptions**
  - marginProfile is expected to be non-null for valid FNO orders in the normal flow (i.e. this is a code defect, not intentional validation)
  - no upstream data problem is suppressing marginProfile — supported by clean dependency telemetry (F10) but not proven at the data layer

### H2: data_issue: margin profile records missing/not populated upstream
Margin profile records (in ledger-db or an upstream margin service) are missing or null for many FNO accounts, so a correctly-written validator receives null marginProfile and fails.

**Causal chain**
1. margin profile source returns null for FNO accounts *(assumed)*
2. validator throws on null marginProfile [F3]

**Supporting evidence**
  - `F3` ✔ The dominant error patterns are the NPE in MarginCheckValidator (224 occurrences) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202 occurrences).
**Contradicting evidence**
  - `F4` ✔ Deployment dep-5101 (order-service v2.14.0) was deployed at 12:10:00 with summary 'Refactor order validation; add margin check for F&O orders', adding a new MarginCheckValidator and removing legacy OrderRules.validateMargin(); status live, never rolled back.
  - `F5` ✔ order-service logged 'deployment dep-5101 (v2.14.0) rollout complete, all pods ready' at 12:10:08, then the first NPE at 12:12:31 — errors began ~2 minutes after rollout completed.
  - `F10` ✔ Downstream dependency pricing-engine error_rate stayed flat at ~0.22% with zero WARN/ERROR logs in the window; no dependency-side errors.
  - `F12` ✔ The error is deterministic across many distinct accounts (99733, 10578, 79537, 57680, 32892, ...) — essentially every FNO order fails, not a subset tied to specific bad data.
  - `F11` ✔ Errors are scope-limited to orderType=FNO (derivatives) orders; only FNO orders produce the NPE, consistent with a new margin check gated to F&O orders.
**Rejected because:** The failure begins within ~2 minutes of dep-5101 rollout and is deterministic across virtually every FNO account (F12) rather than a specific bad-data subset; dependency telemetry (pricing-engine) is clean (F10) and order-service db_pool connections are normal (F7), leaving no evidence of missing upstream data. A sudden simultaneous null across all accounts is far better explained by new code than by mass data loss with no other symptoms.

### H3: config_change or resource/capacity saturation on order-service
A config/feature-flag change or resource exhaustion (pool/CPU/memory) on order-service caused the failures.

**Causal chain**
1. a config or resource change degrades order-service *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ db_pool_in_use stayed flat at ~14 connections (max 15.24) throughout, showing no connection-pool exhaustion.
  - `F8` ✔ cpu_pct stayed flat at ~40% and request_rate flat at ~232 req/s across the window — no load spike or CPU saturation.
  - `F9` ✔ The only config change in the window across all services was on notification-service (cfg-2101, 'Tune email batching', 11:21) — unrelated to order-service; order-service had zero config changes.
  - `F6` ✔ latency_p99_ms was flat at ~150ms across the whole window, including after 12:12 (e.g. 151.86 at 12:12, 147.54 at 12:36).
  - `F12` ✔ The error is deterministic across many distinct accounts (99733, 10578, 79537, 57680, 32892, ...) — essentially every FNO order fails, not a subset tied to specific bad data.
**Rejected because:** No config changes on order-service (F9); db_pool_in_use (F7), cpu_pct (F8) and request_rate are flat, latency is unchanged (F6), and the failures carry an application NPE rather than timeout/pool-exhaustion messages — inconsistent with resource or capacity saturation.

## Facts (verified observations)
- order-service error_rate rose from ~0.2% to ~18.3% between 12:00 and 12:12 buckets and remained ~18-20% through 13:48. [F1]
- First order-service ERROR is a NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) for orderType=FNO at 12:12:31Z. [F2]
- 224 NPE events in MarginCheckValidator and 202 'order rejected with 500 ... marginProfile is null' events dominate. [F3]
- Deployment dep-5101 (order-service v2.14.0) went out at 12:10:00Z, adding MarginCheckValidator and removing legacy OrderRules.validateMargin; never rolled back. [F4]
- order-service logged rollout complete for dep-5101 at 12:10:08Z, then first NPE at 12:12:31Z. [F5]
- order-service latency_p99 was flat ~150ms, db_pool_in_use flat ~14 connections, cpu_pct flat ~40%, request_rate flat ~232 req/s throughout the incident. [F6, F7, F8]
- No config changes occurred on order-service in the window; the only config change was on notification-service (email.batch_size). [F9]
- Downstream dependency pricing-engine stayed at ~0.22% error_rate with zero WARN/ERROR logs in the window. [F10]
- All failing orders are orderType=FNO, and failures span many distinct accounts (99733, 10578, 79537, ...). [F11, F12]

## Assumptions (not verified)
- marginProfile is expected to be non-null for a valid FNO order in the normal flow, so the NPE is a code defect rather than intended validation.
- The v2.14.0 rollout to all pods occurred at 12:10:08Z; there was no canary/paused rollout that would explain a late-arriving failure.
- No margin-profile data-layer telemetry was directly available; the data_issue alternative is rejected on indirect evidence (clean dependency telemetry, determinism, timing).

## Timeline
- `2026-03-03T12:07:51Z` order-service config reloaded (no changes) [F5]
- `2026-03-03T12:10:00Z` Deployment dep-5101 (order-service v2.14.0) applied — adds MarginCheckValidator, removes legacy margin validation [F4]
- `2026-03-03T12:10:08Z` dep-5101 rollout complete, all pods ready [F5]
- `2026-03-03T12:12:31Z` First NPE in MarginCheckValidator (orderType=FNO) [F2]
- `2026-03-03T12:12:00Z` order-service error_rate bucket jumps to 18.33% avg [F1]
- `2026-03-03T13:48:00Z` error_rate still elevated ~18.75%; incident ongoing [F1]

## Recommended actions
- **[immediate]** Roll back order-service to the pre-v2.14.0 version (revert dep-5101) to immediately restore F&O order placement. -- The fault was introduced by dep-5101 which is deployed and live; errors began ~2 min after rollout and are scoped exactly to the new F&O margin check. [F4, F5, F2, F1]
- **[short_term]** Hotfix MarginCheckValidator to null-guard / correctly resolve marginProfile for FNO orders before re-deploying the refactor. -- The NPE is at MarginCheckValidator.java:88 on a null marginProfile; a null check plus correct profile loading must land before v2.14.0 is retried. [F2, F3]
- **[short_term]** Add a pre-deploy unit/integration test covering F&O order validation paths with missing-optional margin data. -- The regression escaped to production because the new validator was not exercised against null/absent margin profiles for FNO orders. [F4, F11]
- **[long_term]** Add canary/progressive rollout with error-rate auto-abort for order-service so a validator regression is caught before full rollout. -- Full rollout at 12:10 followed by 18% error rate at 12:12 could have been caught and auto-reverted earlier. [F1, F5]

## Data gaps
- No WARN/ERROR was obtained for exchange-gateway and ledger-db (queries timed out after 3 attempts); their health is inferred only indirectly from order-service's flat latency and clean dependency metrics.
- No direct telemetry on the margin-profile data source was obtainable, so a latent data-population problem (H2) cannot be fully excluded from the data layer itself.
- search_historical_incidents for 'bad deployment NullPointerException validator order-service' failed with backend 503 (unavailable), so that historical lead was not retrievable.

## Investigation stats
- latency 53.81s, LLM calls 6, tokens 64984 (in 58262 / out 6722)
- tool calls 22 (5 failed, 0 partial, 5 recovered by retry), findings 12/12 verified
