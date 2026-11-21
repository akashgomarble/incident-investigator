# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Order placement 500s began at ~12:12Z, ~2.5 minutes after order-service v2.14.0 (dep-5101, 12:10Z) deployed a refactor that added MarginCheckValidator for F&O orders and removed legacy OrderRules.validateMargin(). All 426 errors are NullPointerExceptions in MarginCheckValidator.validate (line 88) with 'marginProfile is null', exclusively for orderType=FNO, producing HTTP 500 and an ~18% error rate that persists unchanged to 13:59Z. Alternative causes are ruled out: pricing-engine/exchange-gateway/ledger-db show no errors, no dependency or latency degradation, no CPU/memory/pool/restart signal, and the only config change (notification email batch size) is unrelated. Root cause is the bad deployment of the new validator's unguarded null dereference; remediation is rollback of dep-5101 / fix the null handling.

## Probable root cause
**New MarginCheckValidator in dep-5101 dereferences a null margin profile for F&O orders** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.86, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: New MarginCheckValidator in dep-5101 dereferences a null margin profile for F&O orders | bad_deployment | order-service | dep-5101 | 0.86 (high) | active |
| 2 | H2: Upstream margin/profile data became missing for F&O accounts (data_issue) | data_issue | unknown | - | 0.10 (low) | rejected |
| 3 | H3: Dependency failure in pricing-engine / exchange-gateway / external exchange | dependency_failure | pricing-engine | - | 0.04 (low) | rejected |
| 4 | H4: Resource exhaustion / capacity saturation in order-service | resource_exhaustion | order-service | - | 0.03 (low) | rejected |

### H1: New MarginCheckValidator in dep-5101 dereferences a null margin profile for F&O orders
order-service v2.14.0 (dep-5101, deployed 12:10Z) added MarginCheckValidator to the validation chain for F&O orders. The new validator reads a margin profile that is null/absent for FNO orders and dereferences it at MarginCheckValidator.java:88 without a null guard, throwing NullPointerException and returning HTTP 500. Because the faulty path is only reached for derivatives (FNO) orders, ~18% of traffic (the FNO share) fails while other order types are unaffected. Replacing legacy OrderRules.validateMargin() with the new validator removed the previously-working handling.

**Causal chain**
1. dep-5101 deploys order-service v2.14.0 at 12:10Z introducing MarginCheckValidator [F4, F5]
2. First NPE in MarginCheckValidator.validate at 12:12:31Z, 2.5 min later [F2, F5]
3. All failures are orderType=FNO with 'marginProfile is null' -> HTTP 500 [F3, F8]
4. Error rate locks at ~18% (FNO share) with no recovery, no resource/pool/dependency signal [F1, F7, F9, F10]

**Supporting evidence**
  - `F1` ✔ order-service error_rate jumped from ~0.2% (pre-12:00) to ~14.7% in the 12:10 bucket and stayed ~18-19% through 13:59.
  - `F2` ✔ The first error log in order-service is at 12:12:31Z, inside the same 10-minute bucket where error_rate spiked; the incident onset is therefore ~12:12Z.
  - `F3` ✔ Every order-service error is a NullPointerException in the new MarginCheckValidator.validate, with 'marginProfile is null' and orderType=FNO. 426 ERROR entries total; 224 are the NPE pattern and 202 the rejection pattern.
  - `F4` ✔ Deployment dep-5101 (order-service v2.14.0, 12:10:00Z) introduced 'new MarginCheckValidator in validation chain' and removed 'legacy OrderRules.validateMargin()'; it is live and not rolled back.
  - `F5` ✔ The rollout-complete log for dep-5101 is at 12:10:08Z, ~2.5 minutes before the first error at 12:12:31Z.
  - `F8` ✔ No EQ (non-derivative) order produced any error; only orderType=FNO orders appear in the failures, matching the report's 'mostly derivatives orders'.
  - `F10` ✔ Errors are continuous and steady from 12:12 to 13:58 (~18-19% error rate, no recovery), consistent with a persistent code defect rather than a transient blip or recovery.
**Contradicting evidence**
  - `F9` ✔ No order-service restarts occurred during the window (restarts metric stays 0), and db_pool_in_use stays ~14 connections (no saturation).
**Assumptions**
  - The null margin profile is not itself caused by an upstream data change — no data-owning service deployed or changed config in the window.
  - MarginCheckValidator.java:88 is the dereference of the margin profile returned by the (assumed) lookup.
  - The FNO share of order traffic is ~18%, consistent with the observed error rate.

### H2: Upstream margin/profile data became missing for F&O accounts (data_issue)
A margin-profile source (e.g., a reference/margin data store) stopped returning profiles for FNO accounts, so the validator received null even though its logic is correct; the deployment coincidence would then be incidental.

**Causal chain**
(none given)

**Supporting evidence**
  - `F3` ✔ Every order-service error is a NullPointerException in the new MarginCheckValidator.validate, with 'marginProfile is null' and orderType=FNO. 426 ERROR entries total; 224 are the NPE pattern and 202 the rejection pattern.
**Contradicting evidence**
  - `F4` ✔ Deployment dep-5101 (order-service v2.14.0, 12:10:00Z) introduced 'new MarginCheckValidator in validation chain' and removed 'legacy OrderRules.validateMargin()'; it is live and not rolled back.
  - `F6` ✔ The only config change in the window is notification-service 'email.batch_size' 200->250 at 11:21Z, unrelated to order validation.
  - `F7` ✔ pricing-engine, exchange-gateway and ledger-db show zero ERROR logs across 08:00-13:59Z, and order-service latency_p99 (~150ms), cpu (~40%), memory (~60%) and request_rate (~233 req/s) are flat across the incident.
**Rejected because:** No data-owning service deployed or changed config in the window (only dep-5088 portfolio-service grpc bump and cfg-2101 notification email batch size). The failure set is 100% FNO and tied to a brand-new code path; historically FNO orders worked with the legacy validator, so the missing profile only becomes fatal after the refactor removed the legacy handling. No telemetry shows a data source failing.

### H3: Dependency failure in pricing-engine / exchange-gateway / external exchange
A downstream dependency (pricing-engine, exchange-gateway, or the external exchange-api) degraded and caused order placement 500s.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F3` ✔ Every order-service error is a NullPointerException in the new MarginCheckValidator.validate, with 'marginProfile is null' and orderType=FNO. 426 ERROR entries total; 224 are the NPE pattern and 202 the rejection pattern.
  - `F7` ✔ pricing-engine, exchange-gateway and ledger-db show zero ERROR logs across 08:00-13:59Z, and order-service latency_p99 (~150ms), cpu (~40%), memory (~60%) and request_rate (~233 req/s) are flat across the incident.
**Rejected because:** pricing-engine, exchange-gateway and ledger-db produced zero ERROR logs over the whole window, and order-service latency stayed flat (~150ms, no timeouts/504s). The error is a local NullPointerException in order-service validation, not an upstream call failure.

### H4: Resource exhaustion / capacity saturation in order-service
CPU, memory, DB connection pool or thread exhaustion in order-service produced the 500s.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F7` ✔ pricing-engine, exchange-gateway and ledger-db show zero ERROR logs across 08:00-13:59Z, and order-service latency_p99 (~150ms), cpu (~40%), memory (~60%) and request_rate (~233 req/s) are flat across the incident.
  - `F9` ✔ No order-service restarts occurred during the window (restarts metric stays 0), and db_pool_in_use stays ~14 connections (no saturation).
**Rejected because:** cpu ~40%, memory ~60%, db_pool_in_use ~14/15 with no saturation, zero restarts, and request_rate flat (~233 req/s). Errors are deterministic NPEs in a specific validator, not load-driven timeouts.

## Facts (verified observations)
- order-service error_rate rose from ~0.22% before 12:00 to 14.74% at 12:10 and ~18% thereafter. [F1]
- First order-service error logged at 2026-03-03T12:12:31Z. [F2]
- 426 ERROR entries, all NullPointerException in MarginCheckValidator.validate with orderType=FNO / 'marginProfile is null'. [F3]
- dep-5101 (order-service v2.14.0) at 12:10Z added MarginCheckValidator and removed legacy OrderRules.validateMargin(). [F4]
- Deployment rollout completed at 12:10:08Z, before the first error at 12:12:31Z. [F5]
- Only config change was notification-service email.batch_size 200->250, unrelated. [F6]
- pricing-engine, exchange-gateway and ledger-db had zero ERROR logs; order-service latency/cpu/memory/request_rate flat. [F7]
- No EQ (non-FNO) order errors occurred; failures are exclusively derivatives orders. [F8]
- No order-service restarts; db_pool_in_use flat at ~14 connections. [F9]
- Errors persist steadily to 13:58Z with no recovery. [F10]

## Assumptions (not verified)
- MarginCheckValidator.java:88 is where the (assumed) null margin profile is dereferenced; source not available in tools.
- The null margin profile is produced/fetched within order-service rather than by an external data source, since no data-owning service deployed or changed config in the window.
- The ~18% steady error rate equals the FNO share of order traffic.
- query_logs results for order-service were truncated (426 matched, 40 returned) but two independent queries confirmed the same two error patterns and no other error types.

## Timeline
- `2026-03-03T11:21:00Z` notification-service config change cfg-2101 (email.batch_size 200->250) — unrelated to orders [F6]
- `2026-03-03T12:10:00Z` order-service v2.14.0 (dep-5101) deployed: new MarginCheckValidator, legacy validateMargin removed [F4]
- `2026-03-03T12:10:08Z` dep-5101 rollout complete, all pods ready [F5]
- `2026-03-03T12:12:31Z` First NullPointerException in MarginCheckValidator.validate (orderType=FNO) — incident onset [F2]
- `2026-03-03T12:20:00Z` error_rate stabilizes at ~18-19% and remains there through end of window [F1]
- `2026-03-03T12:22:00Z` Incident INC-001 reported by on-call SRE []
- `2026-03-03T13:58:50Z` Latest FNO MarginCheckValidator NPE logged; no recovery, deploy still live [F10]

## Recommended actions
- **[immediate]** Roll back order-service dep-5101 (v2.14.0) to the prior version to restore F&O order placement immediately. -- The faulty validator was introduced by this deployment; rollback removes the unguarded null dereference and is the fastest mitigation. [F2, F4, F10]
- **[short_term]** Add a null guard / default handling for missing marginProfile in MarginCheckValidator.validate (line 88) and re-establish the F&O margin validation logic that the legacy OrderRules.validateMargin provided. -- Root cause is the unguarded dereference of a null margin profile for FNO orders. [F3]
- **[short_term]** Add a canary/staged rollout with F&O-order synthetic checks so a validator regression is caught before full rollout. -- The bad deploy reached 100% of pods with no gate; FNO fraction of traffic (~18%) is enough to be detected in canary. [F4, F8]
- **[long_term]** Alert on per-order-type error rate and on NullPointerException patterns in validation, not just aggregate 5xx. -- Aggregate error_rate only flagged the issue ~10 min after onset; order-type scoping would localize it faster. [F1, F8]

## Data gaps
- No trace/stack detail beyond 'MarginCheckValidator.java:88' or the source of marginProfile (DB query, cache, or field) — root cause is inferred from the deployment diff plus log message.
- order-service error logs were truncated (truncated:true, 426 matched); a small number of non-FNO/non-margin errors cannot be strictly excluded, though two queries showed only the two FNO patterns.
- No per-order-type request/error breakdown metric available to confirm quantitatively that the ~18% equals the FNO traffic share.
- No data-service telemetry to fully prove margin-profile source data was healthy; ruled out only by absence of related deploys/config changes and by historical FNO success under the legacy validator.

## Investigation stats
- latency 57.53s, LLM calls 5, tokens 60977 (in 54172 / out 6805)
- tool calls 21 (0 failed, 0 partial, 0 recovered by retry), findings 10/10 verified
