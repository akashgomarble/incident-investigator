# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order placement 500s are caused by the order-service deploy dep-5101 (v2.14.0, rolled out 12:10:08Z), which added MarginCheckValidator and removed the legacy margin validation. The new validator dereferences a null marginProfile for F&O orders, throwing NullPointerException at MarginCheckValidator.java:88 and returning HTTP 500. Onset is exact: error_rate rises from ~0.2% to ~18.5% at the 12:12 bucket, first NPE logged at 12:12:31Z, and FNO-specific messages match the reported 'mostly derivatives orders' symptom. Downstream dependencies (pricing-engine, exchange-gateway) and order-service resources (CPU/memory/pool/latency/request rate/restarts) are all flat, ruling out dependency, capacity, resource and config causes. Recommended action: roll back dep-5101 to the prior version.

## Probable root cause
**New MarginCheckValidator null-dereferences marginProfile for FNO orders (bad deployment dep-5101)** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: New MarginCheckValidator null-dereferences marginProfile for FNO orders (bad deployment dep-5101) | bad_deployment | order-service | dep-5101 | 0.90 (high) | active |
| 2 | H2: Data issue: missing margin profiles for F&O accounts | data_issue | ledger-db | - | 0.15 (low) | rejected |
| 3 | H3: Dependency failure (pricing-engine / exchange-gateway / market-data) | dependency_failure | exchange-gateway | - | 0.05 (low) | rejected |
| 4 | H4: Resource exhaustion / capacity saturation in order-service | capacity_saturation | order-service | - | 0.05 (low) | rejected |

### H1: New MarginCheckValidator null-dereferences marginProfile for FNO orders (bad deployment dep-5101)
order-service v2.14.0 (dep-5101, 12:10Z) added MarginCheckValidator and removed legacy OrderRules.validateMargin(). The new validator dereferences a null marginProfile for F&O orders, throwing NullPointerException at MarginCheckValidator.java:88 and returning HTTP 500. Because FNO orders are ~18% of order flow, aggregate error rate rose to ~18%.

**Causal chain**
1. Deploy dep-5101 (v2.14.0) introduces new MarginCheckValidator into validation chain at 12:10Z [F1]
2. First NPE at MarginCheckValidator.validate:88 (orderType=FNO) at 12:12:31Z [F3]
3. Null marginProfile causes NPE -> order rejected with 500 [F4]
4. Aggregate error_rate jumps to ~18% from 12:12Z onward [F2]
5. api-gateway error rate rises only as a pass-through of order-service failures [F5, F6]

**Supporting evidence**
  - `F1` ✔ order-service deployment dep-5101 (v2.14.0) completed rollout at 12:10:08Z; its change summary is 'Refactor order validation; add margin check for F&O orders' with 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'.
  - `F2` ✔ order-service error_rate jumped from ~0.2% (buckets through 12:05) to 18.49% at the 12:12 bucket and stayed ~18% through end of window.
  - `F3` ✔ The first order-service errors after the deploy are NullPointerExceptions at MarginCheckValidator.validate(MarginCheckValidator.java:88) for orderType=FNO, first seen 12:12:31Z.
  - `F4` ✔ Errors are the dominant pattern in order-service logs (426 ERROR of 427 matches, truncated) and are overwhelmingly of two forms: NPE in MarginCheckValidator (224) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202), both FNO-scoped.
  - `F5` ✔ Downstream services are normal: pricing-engine error_rate stayed ~0.2% (max 0.32) across the whole window, and exchange-gateway error_rate stayed ~0.2% (max 0.32).
  - `F7` ✔ order-service resource metrics are flat through the incident: cpu_pct ~40%, memory_pct ~60%, db_pool_in_use ~14, restarts 0.
  - `F8` ✔ order-service request_rate is flat at ~233 req/s across the window (min 210, max 256), so there was no traffic surge.
  - `F10` ✔ There were zero WARN/ERROR log entries in order-service before the deploy window 11:30-12:12Z (prior WARN query returned 0 matches over 08:00-13:59).
  - `F11` ✔ order-service latency_p99_ms is flat ~150ms throughout, so the failures are fast rejections, not slow/timeout failures.
**Contradicting evidence**
  - (none)
**Assumptions**
  - marginProfile being null is the actual defect condition (vs. a data issue where margin profiles are genuinely missing)
  - the 18% error rate corresponds to the FNO share of order traffic rather than a separate issue

### H2: Data issue: missing margin profiles for F&O accounts
A subset of F&O accounts lack a marginProfile record, so any margin validation fails (and the new validator does not handle it). The fault would originate in the data/datastore rather than the code. Weakened by the fact that the same validator class is new as of 12:10 and no errors existed before, so absence of data alone would not explain onset.

**Causal chain**
(none given)

**Supporting evidence**
  - `F4` ✔ Errors are the dominant pattern in order-service logs (426 ERROR of 427 matches, truncated) and are overwhelmingly of two forms: NPE in MarginCheckValidator (224) and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202), both FNO-scoped.
**Contradicting evidence**
  - `F1` ✔ order-service deployment dep-5101 (v2.14.0) completed rollout at 12:10:08Z; its change summary is 'Refactor order validation; add margin check for F&O orders' with 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'.
  - `F2` ✔ order-service error_rate jumped from ~0.2% (buckets through 12:05) to 18.49% at the 12:12 bucket and stayed ~18% through end of window.
  - `F3` ✔ The first order-service errors after the deploy are NullPointerExceptions at MarginCheckValidator.validate(MarginCheckValidator.java:88) for orderType=FNO, first seen 12:12:31Z.
  - `F10` ✔ There were zero WARN/ERROR log entries in order-service before the deploy window 11:30-12:12Z (prior WARN query returned 0 matches over 08:00-13:59).
**Rejected because:** No errors existed before 12:10 despite the same accounts/flow; onset aligns exactly with the code deploy, and downstream data services showed no anomaly. The new code path is the proximate cause.

### H3: Dependency failure (pricing-engine / exchange-gateway / market-data)
A downstream dependency degraded causing order rejections.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F5` ✔ Downstream services are normal: pricing-engine error_rate stayed ~0.2% (max 0.32) across the whole window, and exchange-gateway error_rate stayed ~0.2% (max 0.32).
  - `F11` ✔ order-service latency_p99_ms is flat ~150ms throughout, so the failures are fast rejections, not slow/timeout failures.
**Rejected because:** pricing-engine and exchange-gateway error rates stayed ~0.2% throughout; error messages are local NPEs in order-service validation, not downstream timeouts.

### H4: Resource exhaustion / capacity saturation in order-service
CPU, memory, DB pool or connection saturation caused 500s.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ order-service resource metrics are flat through the incident: cpu_pct ~40%, memory_pct ~60%, db_pool_in_use ~14, restarts 0.
  - `F8` ✔ order-service request_rate is flat at ~233 req/s across the window (min 210, max 256), so there was no traffic surge.
  - `F11` ✔ order-service latency_p99_ms is flat ~150ms throughout, so the failures are fast rejections, not slow/timeout failures.
**Rejected because:** cpu ~40%, memory ~60%, db_pool_in_use ~14, restarts 0, request_rate flat ~233 req/s; latency flat. No resource pressure and no load spike.

## Facts (verified observations)
- order-service v2.14.0 (dep-5101) added MarginCheckValidator and removed legacy OrderRules.validateMargin(), rolling out at 12:10:08Z. [F1]
- order-service error_rate rose from ~0.2% to 18.49% at the 12:12 bucket and stayed ~18%. [F2]
- First errors are NullPointerException at MarginCheckValidator.validate:88 for orderType=FNO at 12:12:31Z. [F3]
- Errors are dominated by NPE in MarginCheckValidator and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null'. [F4]
- Downstream pricing-engine and exchange-gateway error rates stayed ~0.2%. [F5]
- api-gateway error rate rose to ~3.4% from 12:10, reflecting order-service failures. [F6]
- order-service cpu/memory/pool/restarts metrics are flat with no resource pressure. [F7]
- order-service request_rate was flat ~233 req/s (no traffic surge). [F8]
- No config changes touched order-service; only an unrelated notification-service batch-size change. [F9]
- No WARN/ERROR entries in order-service before the deploy window. [F10]
- order-service latency_p99 was flat ~150ms. [F11]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-03-03T11:21:00Z` Unrelated config change cfg-2101 (notification-service email.batch_size) [F9]
- `2026-03-03T12:10:08Z` order-service deploy dep-5101 (v2.14.0) rollout complete [F1, F2]
- `2026-03-03T12:12:00Z` error_rate bucket jumps to 18.49% [F2]
- `2026-03-03T12:12:31Z` First NullPointerException at MarginCheckValidator.validate:88 (orderType=FNO) [F3]
- `2026-03-03T12:22:00Z` Incident reported as SEV-1 []
- `2026-03-03T13:59:00Z` Error rate still ~18.4%; no recovery, no rollback [F2]

## Recommended actions
- **[immediate]** Roll back order-service dep-5101 to the previous version (pre-v2.14.0) immediately. -- The failure onset coincides exactly with this deploy; removing the new MarginCheckValidator restores the working legacy margin validation path. [F1, F2, F3]
- **[immediate]** Confirm recovery by watching order-service error_rate return to ~0.2% and absence of MarginCheckValidator NPEs after rollback. -- Error rate has stayed ~18% with no self-recovery; verify the rollback actually clears the symptom. [F2, F3]
- **[short_term]** Add null-handling/guard for marginProfile in MarginCheckValidator and unit tests for F&O orders with missing margin profiles. -- Root defect is a null-dereference on the new F&O margin-check path. [F3, F4]
- **[short_term]** Verify whether marginProfile is legitimately absent for some F&O accounts and fix the data path if so. -- Distinguish a purely defensive-coding bug from genuinely missing margin data. [F4]

## Data gaps
- order-service ERROR log query was truncated (returned 30 of 427 matches), so the full error distribution and any additional error patterns beyond the two NPE forms are not fully enumerated.
- query_logs for order-service timed out on two attempts (tc-001, tc-006), so INFO/DEBUG context around the deploy is only partially available.
- market-data-service error_rate query timed out (tc-016); its health was not directly verified (though pricing-engine, its caller, was normal).
- exchange-api is external with has_telemetry=false, so its health is inferred only from exchange-gateway's normal upstream_error_rate, not directly measured.

## Investigation stats
- latency 53.58s, LLM calls 5, tokens 54291 (in 47765 / out 6526)
- tool calls 20 (3 failed, 0 partial, 7 recovered by retry), findings 11/11 verified
