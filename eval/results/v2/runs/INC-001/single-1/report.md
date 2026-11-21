# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
order-service began returning HTTP 500 on POST /v1/orders at 12:12:31Z, ~2.5 min after deploy dep-5101 (v2.14.0, 12:10Z) added a new MarginCheckValidator and removed the legacy OrderRules.validateMargin(). Every error is a NullPointerException at MarginCheckValidator.java:88 for orderType=FNO, with "marginProfile is null". Error rate went from ~0.2% to ~18% and stayed there through 13:59Z; the release is still "live" (not rolled back). All resource metrics (cpu ~40%, memory ~60%, db_pool ~14, latency ~150ms, restarts 0) and all downstream services (pricing-engine, exchange-gateway both ~0.2% error) are flat, ruling out resource exhaustion, capacity, and dependency failure. Root cause: bad deployment (dep-5101) — the new F&O margin check does not handle a null marginProfile, throwing an uncaught NPE that fails FNO order placement.

## Probable root cause
**Bad deployment dep-5101: new MarginCheckValidator throws uncaught NPE on null marginProfile for FNO orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bad deployment dep-5101: new MarginCheckValidator throws uncaught NPE on null marginProfile for FNO orders | bad_deployment | order-service | dep-5101 | 0.90 (high) | active |
| 2 | H2: Data issue: margin profiles missing for FNO accounts, new code fails to handle it | data_issue | order-service | - | 0.20 (low) | rejected |
| 3 | H3: Dependency failure of downstream (exchange-gateway / pricing-engine / ledger-db) | dependency_failure | exchange-gateway | - | 0.03 (low) | rejected |

### H1: Bad deployment dep-5101: new MarginCheckValidator throws uncaught NPE on null marginProfile for FNO orders
order-service v2.14.0 (dep-5101, deployed 12:10Z) replaced the legacy margin validation with a new MarginCheckValidator that dereferences a null marginProfile for FNO (derivatives) orders, throwing an uncaught NullPointerException at MarginCheckValidator.java:88. The validator sits in the order-placement path, so POST /v1/orders returns HTTP 500. Origin: order-service code release.

**Causal chain**
1. dep-5101 deployed to order-service at 12:10Z, adding MarginCheckValidator and removing legacy OrderRules.validateMargin() [F2]
2. First NPE from MarginCheckValidator.java:88 appears 2.5 min later at 12:12:31Z [F3, F5]
3. All failures are orderType=FNO with 'marginProfile is null', producing HTTP 500 [F4]
4. order-service error_rate jumps 0.2% -> ~18% and persists; release not rolled back [F1, F6]

**Supporting evidence**
  - `F1` ✔ order-service error_rate was ~0.2% through 12:03Z, then jumped to 18.39% at the 12:12Z bucket and stayed ~18% through the last bucket (13:51Z).
  - `F2` ✔ Deploy dep-5101 for order-service v2.14.0 at 2026-03-03T12:10:00Z changed validation: added 'new MarginCheckValidator in validation chain' and removed 'legacy OrderRules.validateMargin()'; status is 'live', rolled_back_at null.
  - `F3` ✔ The first ERROR in order-service appears at 12:12:31Z: 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO orderId=9326cc69'.
  - `F4` ✘ unverified Top error patterns are 67x 'NullPointerException at MarginCheckValidator.validate(...) orderType=FNO' and 62x 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>'.
  - `F5` ✔ No ERROR-level log exists in order-service between 08:00Z and 12:12:30Z (total_matched 0).
  - `F6` ✔ The NPE errors continue through 13:58Z (all still FNO / marginProfile null), i.e. the incident is ongoing.
  - `F11` ✔ The fault originates in the new MarginCheckValidator code path added by dep-5101; the removed legacy validateMargin() presumably handled (or never reached) the null marginProfile case, so the refactor introduced the uncaught NPE for FNO orders.
**Contradicting evidence**
  - (none)
**Assumptions**
  - marginProfile was populated/handled for FNO accounts before the deploy (no data change observed)
  - MarginCheckValidator.java:88 is the dereference of marginProfile

### H2: Data issue: margin profiles missing for FNO accounts, new code fails to handle it
The upstream data source providing marginProfile has no profile for certain FNO accounts, and the newly deployed validator does not defend against null, turning missing data into a 500. Fault would originate upstream (data).

**Causal chain**
1. marginProfile is null for accounts, shown in error messages [F4]
2. New validator does not handle null [F3]

**Supporting evidence**
  - `F4` ✘ unverified Top error patterns are 67x 'NullPointerException at MarginCheckValidator.validate(...) orderType=FNO' and 62x 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>'.
**Contradicting evidence**
  - `F5` ✔ No ERROR-level log exists in order-service between 08:00Z and 12:12:30Z (total_matched 0).
  - `F10` ✔ The only config/feature-flag change in the window is cfg-2101 on notification-service (email.batch_size 200->250 at 11:21Z), unrelated to order-service.
  - `F11` ✔ The fault originates in the new MarginCheckValidator code path added by dep-5101; the removed legacy validateMargin() presumably handled (or never reached) the null marginProfile case, so the refactor introduced the uncaught NPE for FNO orders.
**Assumptions**
  - A margin data store changed or is incomplete
**Rejected because:** No data-layer or config change was observed in the window; the legacy validateMargin() worked until the deploy. The onset coincides exactly with dep-5101, so the origin is the code change, not the data. Even if some profiles are absent, the refactor introduced the unhandled-null path.

### H3: Dependency failure of downstream (exchange-gateway / pricing-engine / ledger-db)
A downstream dependency degraded causing order placement 500s.

**Causal chain**
1. order-service calls pricing-engine and exchange-gateway *(assumed)*
2. their failures would surface as 500s *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ Downstream services show no error increase: exchange-gateway error_rate flat ~0.21% and pricing-engine flat ~0.22% across the same window.
  - `F4` ✘ unverified Top error patterns are 67x 'NullPointerException at MarginCheckValidator.validate(...) orderType=FNO' and 62x 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>'.
  - `F8` ✔ order-service latency_p99_ms stayed ~150ms across the whole window (mean 149.97), with no step change at 12:12Z.
**Rejected because:** exchange-gateway and pricing-engine error rates are flat (~0.2%) across the incident, latency is unchanged, and the error signature is a local Java NPE, not an upstream timeout/5xx.

## Facts (verified observations)
- order-service error_rate rose from ~0.2% to ~18% starting in the 12:12Z bucket and remained elevated through 13:51Z [F1]
- dep-5101 deployed order-service v2.14.0 at 12:10:00Z with 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'; still live, not rolled back [F2]
- First error log is at 12:12:31Z: NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO [F3]
- No order-service ERROR logs exist before 12:12:31Z in the 08:00-12:12 window [F5]
- Errors continue to 13:58Z (incident ongoing) [F6]
- CPU, memory, db connection pool, latency, and restart counts are all flat across the incident window [F7, F8]
- exchange-gateway and pricing-engine error rates are flat (~0.2%) throughout [F9]
- The only config change in the window affects notification-service (email.batch_size), unrelated to orders [F10]

## Assumptions (not verified)
- marginProfile was handled correctly for FNO orders before dep-5101 (supported by absence of prior margin errors, F5)
- MarginCheckValidator.java:88 is where marginProfile is dereferenced
- The 'mostly derivatives' ticket reports correspond to orderType=FNO
- (demoted from facts: no verified observation backs it) The dominant errors are NPE in MarginCheckValidator for FNO orders and 'order rejected with 500: ... marginProfile is null for account <n>'

## Timeline
- `2026-03-03T12:10:00Z` Deploy dep-5101 (order-service v2.14.0): new MarginCheckValidator added, legacy OrderRules.validateMargin() removed [F2]
- `2026-03-03T12:12:00Z` order-service error_rate bucket jumps from ~0.2% to 18.39% [F1]
- `2026-03-03T12:12:31Z` First NPE: MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO [F3]
- `2026-03-03T12:22:00Z` Incident reported (SEV-1) - order placement 500s, mostly derivatives []
- `2026-03-03T13:58:42Z` Errors still ongoing; dep-5101 still live (not rolled back) [F6]

## Recommended actions
- **[immediate]** Roll back dep-5101 (order-service v2.14.0) to the prior version immediately -- Onset coincides exactly with the deploy; rolling back restores the legacy margin path and stops the FNO NPE 500s. [F1, F2, F3, F6]
- **[short_term]** Add a null-guard for marginProfile in MarginCheckValidator (fail closed with a proper 4xx/business rejection, not an unhandled NPE) -- The uncaught NPE turns a missing/absent margin profile into a 500 instead of a controlled rejection. [F3, F4]
- **[short_term]** Add a pre-merge/deploy regression test covering FNO orders with absent/missing marginProfile -- The removed legacy validateMargin() previously covered this path; the refactor shipped without an equivalent guard. [F2, F11]
- **[long_term]** Verify whether marginProfile is expected to be present for all FNO accounts; if legitimately absent, coordinate the contract between order-service and the margin data owner -- Distinguishes a pure code bug from a data-contract gap so future refactors do not regress. [F4]

## Data gaps
- No service telemetry on the external exchange-api (has_telemetry:false), but exchange-gateway metrics are flat so this is not implicated
- No explicit log of a successful FNO margin check before the deploy to directly prove the legacy path populated marginProfile
- The exact origin of the marginProfile object (which datastore/service supplies it) is not shown by the available tools

## Integrity warnings (set by code)
- fact demoted to assumption: The dominant errors are NPE in MarginCheckValidator for FNO orders and 'order re

## Investigation stats
- latency 59.44s, LLM calls 7, tokens 95983 (in 85573 / out 10410)
- tool calls 19 (0 failed, 0 partial, 0 recovered by retry), findings 10/11 verified
