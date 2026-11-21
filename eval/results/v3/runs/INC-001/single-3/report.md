# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order-service placement 500s are caused by bad deployment dep-5101 (v2.14.0, deployed 12:10Z), which added a new MarginCheckValidator to the order validation chain for F&O orders. Starting 12:12:31Z (first error, ~2 min post-deploy) it throws 'NullPointerException: marginProfile is null' for orderType=FNO, rejecting orders with HTTP 500. Error rate jumped from ~0.2% to ~18.3% at the 12:12 bucket and persisted. No pre-deploy occurrences of this error exist, and CPU, DB pool, latency, request rate and the pricing-engine error rate are all flat, ruling out resource, capacity, infra and dependency causes. Fix is to roll back dep-5101 or hotfix the null-margin handling.

## Probable root cause
**dep-5101 bad deployment: new MarginCheckValidator NPEs on F&O orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: dep-5101 bad deployment: new MarginCheckValidator NPEs on F&O orders | bad_deployment | order-service | dep-5101 | 0.90 (high) | active |
| 2 | H2: F&O accounts lack margin profile data (data_issue) | data_issue | order-service | - | 0.12 (low) | rejected |
| 3 | H3: Resource/dependency/infra fault (pool, downstream, shared DB) | dependency_failure | ledger-db | - | 0.05 (low) | rejected |

### H1: dep-5101 bad deployment: new MarginCheckValidator NPEs on F&O orders
order-service v2.14.0 (dep-5101, 12:10Z) introduced MarginCheckValidator into the validation chain and removed legacy OrderRules.validateMargin(); the new validator dereferences a null marginProfile for FNO orders, throwing NullPointerException and rejecting the order with HTTP 500. The fault originates in the order-service code release.

**Causal chain**
1. dep-5101 deployed order-service v2.14.0 at 12:10Z adding MarginCheckValidator [F4]
2. First MarginCheckValidator NPE occurs at 12:12:31Z, ~2 min after deploy, with no prior occurrence in days of logs [F2, F5]
3. Error rate jumps from ~0.2% to ~18.3% in the same 12:12 bucket [F1]
4. All errors are NullPointerException: marginProfile is null, exclusively for orderType=FNO, i.e. the exact new F&O path [F2, F3]
5. No resource, latency, pool, downstream or shared-component anomaly accompanies onset, so the fault is in the new code path, not the environment [F6, F7, F8, F9]

**Supporting evidence**
  - `F1` ✔ order-service error rate jumped sharply at the 12:12 bucket: from ~0.2% baseline (11:00-12:00 buckets) to 18.33% avg at 12:12 and remains ~18-19% through 13:48.
  - `F2` ✔ First ERROR log in order-service is at 12:12:31Z; all 426 errors in the window match two patterns referencing MarginCheckValidator and 'marginProfile is null', both first-seen at 12:12.
  - `F3` ✔ Every logged exception carries orderType=FNO; the two patterns account for 224 + 202 of 426 errors.
  - `F4` ✔ Deployment dep-5101 to order-service v2.14.0 went live at 12:10:00Z with summary 'Refactor order validation; add margin check for F&O orders', adding 'new MarginCheckValidator in validation chain' and removing 'legacy OrderRules.validateMargin()'. Not rolled back.
  - `F5` ✔ No occurrence of the marginProfile/MarginCheckValidator error exists before the deploy: a log query covering 2026-03-01 to 12:12 returned total_matched=0, and a 00:00-12:15 query shows the first match at 12:12:31Z, after dep-5101.
  - `F6` ✔ order-service CPU stayed flat (~40% avg, max 43.44) across the whole window, showing no resource pressure coincident with onset.
  - `F7` ✔ DB connection pool usage stayed flat (~14 connections avg, max 15.24) across the window; no pool exhaustion.
  - `F8` ✔ order-service latency p99 stayed flat (~150 ms mean, max 164.98) across onset; request rate stayed flat (~232 req/s). This is a hard-failure pattern, not a saturation/latency pattern.
  - `F9` ✔ Downstream pricing-engine error rate stayed flat at ~0.22% throughout the incident window; no downstream dependency degraded.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The validation chain change shipped atomically with v2.14.0 (change summary explicitly lists the new validator)
  - marginProfile was previously populated/skipped by the removed legacy validateMargin() so only the new path fails

### H2: F&O accounts lack margin profile data (data_issue)
The marginProfile records for FNO accounts are missing/not provisioned, so any consumer, including the new validator, would see null; the deploy only exposed a pre-existing data gap.

**Causal chain**
1. marginProfile is null for many distinct accounts in the error logs [F2]

**Supporting evidence**
  - `F2` ✔ First ERROR log in order-service is at 12:12:31Z; all 426 errors in the window match two patterns referencing MarginCheckValidator and 'marginProfile is null', both first-seen at 12:12.
**Contradicting evidence**
  - `F5` ✔ No occurrence of the marginProfile/MarginCheckValidator error exists before the deploy: a log query covering 2026-03-01 to 12:12 returned total_matched=0, and a 00:00-12:15 query shows the first match at 12:12:31Z, after dep-5101.
  - `F4` ✔ Deployment dep-5101 to order-service v2.14.0 went live at 12:10:00Z with summary 'Refactor order validation; add margin check for F&O orders', adding 'new MarginCheckValidator in validation chain' and removing 'legacy OrderRules.validateMargin()'. Not rolled back.
  - `F8` ✔ order-service latency p99 stayed flat (~150 ms mean, max 164.98) across onset; request rate stayed flat (~232 req/s). This is a hard-failure pattern, not a saturation/latency pattern.
**Assumptions**
  - Margin data is stored externally and could be absent rather than simply unread by the new code path
**Rejected because:** If margin data were broadly missing, the removed legacy OrderRules.validateMargin() would have failed on the same orders before 12:10. There are zero marginProfile errors before the deploy (F5) and the failure set is exactly the new validator's code path (F4). Timing localized to the deploy, not to a data-load event; no data/ETL change appears in config changes (F10).

### H3: Resource/dependency/infra fault (pool, downstream, shared DB)
A resource exhaustion, capacity, credential or downstream dependency failure caused order-service 500s.

**Causal chain**
1. Downstream or datastore degradation propagates 500s to order placement *(assumed)*
2. But CPU, pool, latency, request rate and pricing-engine error rate are all flat [F6, F7, F8, F9]

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ order-service CPU stayed flat (~40% avg, max 43.44) across the whole window, showing no resource pressure coincident with onset.
  - `F7` ✔ DB connection pool usage stayed flat (~14 connections avg, max 15.24) across the window; no pool exhaustion.
  - `F8` ✔ order-service latency p99 stayed flat (~150 ms mean, max 164.98) across onset; request rate stayed flat (~232 req/s). This is a hard-failure pattern, not a saturation/latency pattern.
  - `F9` ✔ Downstream pricing-engine error rate stayed flat at ~0.22% throughout the incident window; no downstream dependency degraded.
  - `F11` ✔ portfolio-service logged only 2 WARN entries (slow /internal/metrics request, deprecated TLS cipher) and no errors across the window.
  - `F12` ✔ order-service dependency map: it calls ledger-db, pricing-engine, exchange-gateway and kafka; ledger-db is a shared postgres. No shared-component or external-provider failure signal appeared in logs or metrics.
  - `F2` ✔ First ERROR log in order-service is at 12:12:31Z; all 426 errors in the window match two patterns referencing MarginCheckValidator and 'marginProfile is null', both first-seen at 12:12.
**Assumptions**
  - A shared component could fail without visible telemetry
**Rejected because:** All resource and dependency metrics are flat through onset (F6-F9); the errors are a deterministic NPE with a specific null field, not timeouts/connection errors (F2); pricing-engine, portfolio-service and ledger-db show no errors (F9, F11, F12).

## Facts (verified observations)
- order-service error rate rose from ~0.2% to ~18.3% at the 12:12 bucket and stayed elevated. [F1]
- All 426 order-service errors are marginProfile-null NPEs in MarginCheckValidator, first at 12:12:31Z, all orderType=FNO. [F2, F3]
- dep-5101 deployed order-service v2.14.0 at 12:10Z adding MarginCheckValidator and removing legacy OrderRules.validateMargin(); not rolled back. [F4]
- No occurrences of the marginProfile error exist before the deploy. [F5]
- order-service CPU (~40%), db pool (~14 conns), latency p99 (~150 ms) and request rate (~232 req/s) are flat across onset. [F6, F7, F8]
- Downstream pricing-engine error rate stayed flat at ~0.22%. [F9]
- The only other change in the window was an unrelated notification-service email batch-size config tweak. [F10]

## Assumptions (not verified)
- The change summary 'new MarginCheckValidator in validation chain' shipped atomically with v2.14.0.
- marginProfile was handled safely by the removed legacy validateMargin(), so failures began only with the new code path.

## Timeline
- `2026-03-03T11:00:00Z` Baseline: order-service error rate ~0.2%, CPU ~40%, pool ~14, p99 ~150ms, req ~232/s [F1, F6, F7, F8]
- `2026-03-03T12:10:00Z` dep-5101 deploys order-service v2.14.0 adding MarginCheckValidator for F&O orders [F4]
- `2026-03-03T12:12:31Z` First 'NullPointerException at MarginCheckValidator.validate line 88 orderType=FNO' logged [F2, F5]
- `2026-03-03T12:12:00Z` Error rate jumps to 18.33% avg in the 12:12 bucket [F1]
- `2026-03-03T12:22:00Z` Incident INC-001 reported by on-call SRE []
- `2026-03-03T13:48:00Z` Error rate still ~18.75% avg; fault persists, deployment not rolled back [F1, F4]

## Recommended actions
- **[immediate]** Roll back dep-5101 (order-service v2.14.0) to the last known-good version immediately to restore order placement. -- Onset coincides with the deploy and every failure is in the new validator's code path; rollback is the fastest recovery. [F1, F2, F4, F5]
- **[immediate]** If rollback is not possible, hotfix MarginCheckValidator.validate to null-guard marginProfile (or restore legacy OrderRules.validateMargin()) and ship as a patch. -- The NPE is a straightforward null dereference on marginProfile for FNO orders. [F2, F3]
- **[short_term]** Audit why marginProfile is null for FNO orders and whether the removed legacy path populated/skipped it, to avoid regressions on re-enable. -- Determines whether the new path needs a data fallback or the margin source needs fixing. [F2, F4]
- **[long_term]** Add a canary/error-rate gate and unit tests covering FNO margin validation before validation-chain changes reach 100% of traffic. -- A 2-minute ramp to ~18% error rate should have been caught by progressive delivery. [F1, F2]

## Data gaps
- No raw stack trace or source line context beyond 'MarginCheckValidator.java:88'; the exact null source (missing record vs. unpopulated field) is not directly observable from logs alone.
- No telemetry from exchange-api (external, has_telemetry=false), though no evidence points there.
- order-service logs were truncated (426 matched, 30/20 returned); pattern counts were still complete, so the FNO-only scope is supported but per-account coverage is sampled.

## Investigation stats
- latency 53.07s, LLM calls 5, tokens 63085 (in 53173 / out 9912)
- tool calls 16 (0 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
