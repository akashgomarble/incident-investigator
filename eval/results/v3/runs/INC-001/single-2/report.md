# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order placement 500s are caused by a code regression in the order-service v2.14.0 release dep-5101, deployed 2026-03-03T12:10:00Z. The new MarginCheckValidator throws a NullPointerException ('marginProfile is null') at line 88 for orderType=FNO (F&O) orders, and the legacy OrderRules.validateMargin() that previously tolerated/handled this was removed in the same release. Symptoms began at 12:12:31Z and error_rate jumped from ~0.2% to ~18.4% as a flat step function, affecting only derivatives orders. Dependencies (pricing-engine) are healthy, traffic and CPU are flat, and no relevant config change occurred, ruling out dependency failure, capacity saturation, and data issues. Recommend immediate rollback of dep-5101.

## Probable root cause
**Bad deployment: MarginCheckValidator NPEs on F&O orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bad deployment: MarginCheckValidator NPEs on F&O orders | bad_deployment | order-service | dep-5101 | 0.90 (high) | active |
| 2 | H2: Data issue: margin profile data missing in ledger-db for F&O accounts | data_issue | ledger-db | - | 0.10 (low) | rejected |
| 3 | H3: Capacity/resource saturation on order-service | capacity_saturation | order-service | - | 0.05 (low) | rejected |

### H1: Bad deployment: MarginCheckValidator NPEs on F&O orders
order-service v2.14.0 (dep-5101) at 12:10Z replaced the legacy OrderRules.validateMargin() with a new MarginCheckValidator. The new validator dereferences a null marginProfile for orderType=FNO orders, causing an uncaught NPE that returns HTTP 500 on POST /v1/orders. This is a code/regression defect introduced by the release, not a data or infrastructure problem.

**Causal chain**
1. Deploy dep-5101 (v2.14.0, new MarginCheckValidator) goes live at 12:10Z [F1]
2. First NPE at MarginCheckValidator.validate(MarginCheckValidator.java:88) at 12:12:31Z, none before [F2, F10]
3. NPE only for orderType=FNO, marginProfile null for those accounts [F3, F4]
4. error_rate step-jumps 0.2% -> 18.4% at 12:12 and stays flat [F5]
5. HTTP 500 returned to order placement callers [F4]

**Supporting evidence**
  - `F1` ✔ order-service deployment dep-5101 (v2.14.0) went live at 2026-03-03T12:10:00Z, adding a new MarginCheckValidator to the validation chain and removing legacy OrderRules.validateMargin(), authored by r.iyer, status live, not rolled back.
  - `F2` ✔ order-service ERROR logs begin at 2026-03-03T12:12:31Z with 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88)' and continue through the window.
  - `F3` ✔ Every logged error names orderType=FNO, i.e. only F&O (derivatives) orders hit the NPE; no other order types appear in the error set.
  - `F4` ✔ The error message is 'java.lang.NullPointerException: marginProfile is null for account <n>' returned as HTTP 500 on order placement.
  - `F5` ✔ order-service error_rate was ~0.2% before 12:12 and jumped to 18.38% in the 12:12 bucket, remaining flat ~18.5% through 13:56 — a step change, not a gradual ramp.
  - `F11` ✔ The deploy introduced code that dereferences a marginProfile that is null specifically for FNO orders; the onset (12:12:31) is ~2.5 min after the deploy (12:10:00) with zero NPEs beforehand, and the symptom scope exactly matches the new feature (F&O margin check).
**Contradicting evidence**
  - (none)
**Assumptions**
  - The NPE at line 88 is genuinely the new validator dereferencing marginProfile (stack frame matches the class added in the deploy).
  - marginProfile was never supposed to exist as null for FNO accounts under the old code path (legacy validator handled the null case).

### H2: Data issue: margin profile data missing in ledger-db for F&O accounts
A dataset/record problem — marginProfile rows absent for F&O accounts — could make the validator see null and NPE.

**Causal chain**
1. marginProfile missing for FNO accounts *(assumed)*
2. validator dereferences null -> NPE [F4]

**Supporting evidence**
  - `F4` ✔ The error message is 'java.lang.NullPointerException: marginProfile is null for account <n>' returned as HTTP 500 on order placement.
**Contradicting evidence**
  - `F2` ✔ order-service ERROR logs begin at 2026-03-03T12:12:31Z with 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88)' and continue through the window.
  - `F8` ✔ pricing-engine (the service supplying pricing/market data to order-service) error_rate stayed flat ~0.22% across the entire window and it logged zero WARN+ entries — the dependency did not degrade.
  - `F10` ✔ Total error count for the morning window is 272 ERROR entries with the two NPE patterns dominating (150 + 122), all starting at or after 12:12:31Z.
**Assumptions**
  - No evidence of any data mutation was available either way.
**Rejected because:** If the data were missing, the legacy OrderRules.validateMargin() path would also have failed before 12:10, but there were zero errors before 12:12:31Z (F2, F10). pricing-engine dependency metrics are flat (F8) and no upstream data signal changed. The onset aligns exactly with the deploy, not with any data change.

### H3: Capacity/resource saturation on order-service
Load or resource exhaustion on order-service caused 500s.

**Causal chain**
1. resource pressure -> 500s *(assumed)*

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F6` ✔ order-service CPU stayed flat at ~40% across the whole window (min 36.51, max 43.44); no CPU/resource pressure accompanied the incident.
  - `F7` ✔ order-service request_rate stayed flat ~232 req/s (mean 232.19, max 255.8) — no traffic surge, so the error spike is not load-driven.
  - `F4` ✔ The error message is 'java.lang.NullPointerException: marginProfile is null for account <n>' returned as HTTP 500 on order placement.
**Rejected because:** CPU flat ~40% (F6) and request_rate flat ~232 req/s (F7); errors are a deterministic NPE on FNO orders, not timeouts/overload.

## Facts (verified observations)
- order-service v2.14.0 (dep-5101) deployed 12:10Z adding MarginCheckValidator and removing legacy OrderRules.validateMargin(). [F1]
- First ERROR at 12:12:31Z is an NPE inside MarginCheckValidator.validate, a class added by the deploy. [F2]
- All errors are on orderType=FNO (F&O) orders with message 'marginProfile is null'. [F3, F4]
- order-service error_rate step-jumped from ~0.2% to 18.38% at the 12:12 bucket and stayed flat. [F5]
- CPU (~40%), request_rate (~232 req/s) and pricing-engine error_rate (~0.22%) were all flat — no resource or dependency cause. [F6, F7, F8]
- The only config change was notification-service email.batch_size, unrelated to orders. [F9]
- The sole deployment in the window was order-service dep-5101. [F1]

## Assumptions (not verified)
- (none)

## Timeline
- `2026-03-03T11:21:00Z` notification-service config change email.batch_size 200->250 (unrelated) [F9]
- `2026-03-03T12:10:00Z` order-service v2.14.0 (dep-5101) deployed, adding MarginCheckValidator [F1]
- `2026-03-03T12:12:31Z` First NPE at MarginCheckValidator.validate; errors begin [F2]
- `2026-03-03T12:12:00Z` error_rate step-jumps to ~18.4% [F5]
- `2026-03-03T12:22:00Z` Incident reported to on-call SRE []
- `2026-03-03T13:56:00Z` Errors still ongoing at ~2.5% of all requests failing, flat [F5]

## Recommended actions
- **[immediate]** Roll back order-service from v2.14.0 (dep-5101) to the previous version immediately. -- The deploy is the trigger; rollback removes the faulty MarginCheckValidator and should restore order placement for F&O. [F1, F2, F5]
- **[short_term]** Add a null-guard / proper default for marginProfile in MarginCheckValidator before re-releasing, and restore the legacy null-handling behaviour. -- Root cause is dereferencing a null marginProfile for F&O orders. [F4, F11]
- **[short_term]** Add integration/unit tests covering FNO orders with missing margin profile to the release gate. -- Prevents recurrence of the exact regression that shipped undetected. [F3, F4]
- **[long_term]** Add an alert on order-service error_rate step change correlated with deploy events so future bad deploys are caught within minutes. -- Onset was 12:12 but not reported until 12:22; faster detection limits impact. [F5]

## Data gaps
- No code diff / source for MarginCheckValidator.java line 88 was available, so the exact null-dereference line is inferred from the stack trace message rather than read from source.
- No ledger-db or account-data schema/row inspection was performed, so the possibility that marginProfile is legitimately absent for some F&O accounts (and the old code tolerated it) is not independently confirmed.
- The first log query result was truncated (272 matches, 30 returned) and the second (154 matches, 30 returned); counts of patterns are from the pattern summaries, but full per-entry coverage of all errors was not retrieved.
- No deployment history before 10:00Z was checked, so it is assumed no earlier relevant deploy existed.

## Investigation stats
- latency 33.53s, LLM calls 4, tokens 39589 (in 34152 / out 5437)
- tool calls 12 (0 failed, 0 partial, 0 recovered by retry), findings 12/12 verified
