# Incident investigation report: INC-001
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_disabled*

## Summary
Order placement 500s are attributed to the order-service v2.14.0 deployment (dep-5101) at 12:10:08Z, whose newly added MarginCheckValidator dereferences a null margin profile for derivatives (orderType=FNO) orders and throws NullPointerException at MarginCheckValidator.java:88. The top hypothesis H1 is clearly ahead (0.60 vs 0.12 for the next candidate) and is supported by a tight temporal sequence (deploy completed, error-rate crossing at the 12:10Z bucket, first NPE ERROR at 12:12:31Z), by error text that names the new validator, and by the absence of any competing change in the window. Shared-component causes are excluded by flat ledger-db connections_active and zero kafka under_replicated_partitions, and an external exchange-api fault is excluded by the error mechanism and by flat latency. Confidence is medium rather than high because the upstream source of the null marginProfile was never identified in telemetry, so the split between "new code assumption" and "data-path change" (H1 vs H2) is argued largely from timing.

## Probable root cause
**order-service v2.14.0 MarginCheckValidator NPE on F&O orders (bad deployment)** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.60, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: order-service v2.14.0 MarginCheckValidator NPE on F&O orders (bad deployment) | bad_deployment | order-service | dep-5101 | 0.60 (medium) | active |
| 2 | H2: Upstream margin/account data source returned null margin profiles for F&O accounts (dependency/data failure) | dependency_failure | margin-profile data path (unidentified; margin-service/account-service do not exist as services) | null | 0.12 (low) | active |
| 3 | H3: Shared-component degradation (ledger-db pool exhaustion or kafka broker problem) coinciding with onset | resource_exhaustion | ledger-db or kafka | null | 0.05 (low) | rejected |
| 4 | H4: External exchange-api degradation re-run of the HIST-103 failure mode | dependency_failure | exchange-api (external) | null | 0.03 (low) | rejected |

### H1: order-service v2.14.0 MarginCheckValidator NPE on F&O orders (bad deployment)
The 12:10:08Z deploy of order-service v2.14.0 introduced a new MarginCheckValidator in the validation chain that dereferences a null margin profile for F&O orders (orderType=FNO), throwing NullPointerException at MarginCheckValidator.java:88 and causing POST /v1/orders to return HTTP 500. The fault originates in the order-service release itself; the null margin profile is the input condition the new validator cannot handle (either bad data or an unchecked assumption in the new code), not a separate upstream failure.

**Causal chain**
1. order-service v2.14.0 deployed at 12:10:00Z / rollout complete 12:10:08Z, adding 'new MarginCheckValidator in validation chain' and removing legacy OrderRules.validateMargin() [F1, F13]
2. Error rate crosses from 0.16-0.26% baseline to 11.17% avg in the 12:10Z bucket, the first bucket after the rollout [F9, F18]
3. First ERROR log appears at 12:12:31Z with NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO — no ERROR of any kind existed in this log stream before the rollout [F7, F13]
4. Validator throws when marginProfile is null for the account; the NPE propagates out of the validation chain and POST /v1/orders returns 500 [F8, F14]
5. Only FNO orders hit the new margin path, so 100% of error samples name orderType=FNO while non-FNO paths remain clean [F11, F14]
6. Failure is not self-healing; error rate plateaus ~18-19% and is unrecovered at 13:55Z because the deploy was never rolled back [F10, F1]
7. api-gateway error rate jumps in the same 12:12Z bucket as its downstream caller's 500s (propagation, not origin) [F15]

**Supporting evidence**
  - `F1` ✔ order-service was deployed to v2.14.0 at 2026-03-03T12:10:00Z by r.iyer, 12 minutes before the 12:22Z report; summary is 'Refactor order validation; add margin check for F&O orders' with changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'. Status is 'live' with rolled_back_at: null.
  - `F7` ✔ order-service ERROR logs start at 12:12:31Z with 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO' and continue through 13:58:50Z; 426 ERROR entries were matched, all of the two marginProfile/NPE patterns.
  - `F8` ✔ The 500 error signature is a null margin profile: 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202 occurrences), alongside 224 NPE-at-MarginCheckValidator occurrences.
  - `F9` ✔ The error rate metric crosses from baseline to incident level in the 12:10:00Z 5-minute bucket: avg 11.17% / max 19.63%, versus 0.16-0.26% for every bucket from 11:00 to 12:05Z. Onset is therefore ~12:10Z, i.e. about 12 minutes before the 12:22Z report.
  - `F10` ✔ Error rate plateaus at ~18-19% and has not recovered by the latest data: buckets from 12:15Z through 13:55Z all avg 18.0-19.0% (peak max 20.19%), last bucket 13:55Z avg 18.74%.
  - `F11` ✔ Every error sample names orderType=FNO; a filtered query for 'orderType=' returned 224 ERROR matches, 224 of which are the FNO MarginCheckValidator NPE pattern, and no non-FNO error pattern appears in the top_patterns.
  - `F13` ✔ order-service v2.14.0 rollout completed at 12:10:08Z ('deployment dep-5101 (v2.14.0) rollout complete, all pods ready'), and the very next ERROR entries in the same log stream begin at 12:12:31Z with the MarginCheckValidator NPE — no ERROR of any kind appears in this stream before the rollout; the preceding INFO entries (11:56:19Z health check ok, 11:59:51Z scheduled job heartbeat ok, 12:07:51Z config reloaded (no changes)) show a clean pre-rollout state.
  - `F14` ✔ The failing error signature is a newly-introduced validator: two patterns dominate order-service logs — 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO' and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' — matching the v2.14.0 change 'new MarginCheckValidator in validation chain'. Both first appear only after 12:10Z (12:12:31Z and 12:12:44Z respectively).
  - `F18` ✔ Temporal ordering places the order-service deploy as the only candidate event immediately preceding onset: the deploy completed at 12:10:08Z (tc-017), the error-rate metric crosses into incident level in the 12:10Z bucket (T1/F9), and the first ERROR log is 12:12:31Z. No other change lies in this window (T2: the only other deploy, portfolio-service v4.2.1, was at 09:36Z, ~2h46m earlier; no config change in 11:50-13:59Z).
  - `F15` ✔ api-gateway (order-service's inbound HTTP caller, i.e. downstream casualty) shows an error_rate jump that starts in the SAME 12:12Z bucket as the order-service errors: from a flat baseline of 0.20-0.28% for every 11:00-12:00Z bucket to avg 3.41% / max 3.66% at 12:12:00Z, staying elevated (3.33-3.46%) through 13:48Z. Its onset is consistent with, and explained by, downstream propagation of order-service 500s.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The marginProfile value omitted for FNO orders was previously defaulted/computed by the removed legacy OrderRules.validateMargin() code path; the new validator does not replicate that fallback (mechanism inferred from F1's 'removed legacy OrderRules.validateMargin()' plus the NPE, not directly observed).
  - No config/flag change contributed — supported by F4, but F4 relies on the change-history tool's completeness.

### H2: Upstream margin/account data source returned null margin profiles for F&O accounts (dependency/data failure)
The marginProfile field is populated by an upstream source (a margin/account service or the margin data path). That source began returning null/absent margin profiles for F&O accounts around 12:10Z, and order-service v2.14.0's new validator turned what would previously have been tolerated into a hard 500. Origin is the data/dependency supplying margin profiles, not the order-service code.

**Causal chain**
1. Something began supplying null marginProfile for FNO accounts around onset *(assumed)*
2. Errors are specifically 'marginProfile is null for account <n>' — a data-absence condition, not a transport error [F8, F7]
3. New MarginCheckValidator has no null guard, so null data becomes a 500 [F1, F14]

**Supporting evidence**
  - `F8` ✔ The 500 error signature is a null margin profile: 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202 occurrences), alongside 224 NPE-at-MarginCheckValidator occurrences.
  - `F14` ✔ The failing error signature is a newly-introduced validator: two patterns dominate order-service logs — 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO' and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' — matching the v2.14.0 change 'new MarginCheckValidator in validation chain'. Both first appear only after 12:10Z (12:12:31Z and 12:12:44Z respectively).
**Contradicting evidence**
  - `F1` ✔ order-service was deployed to v2.14.0 at 2026-03-03T12:10:00Z by r.iyer, 12 minutes before the 12:22Z report; summary is 'Refactor order validation; add margin check for F&O orders' with changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'. Status is 'live' with rolled_back_at: null.
  - `F13` ✔ order-service v2.14.0 rollout completed at 12:10:08Z ('deployment dep-5101 (v2.14.0) rollout complete, all pods ready'), and the very next ERROR entries in the same log stream begin at 12:12:31Z with the MarginCheckValidator NPE — no ERROR of any kind appears in this stream before the rollout; the preceding INFO entries (11:56:19Z health check ok, 11:59:51Z scheduled job heartbeat ok, 12:07:51Z config reloaded (no changes)) show a clean pre-rollout state.
  - `F9` ✔ The error rate metric crosses from baseline to incident level in the 12:10:00Z 5-minute bucket: avg 11.17% / max 19.63%, versus 0.16-0.26% for every bucket from 11:00 to 12:05Z. Onset is therefore ~12:10Z, i.e. about 12 minutes before the 12:22Z report.
  - `F18` ✔ Temporal ordering places the order-service deploy as the only candidate event immediately preceding onset: the deploy completed at 12:10:08Z (tc-017), the error-rate metric crosses into incident level in the 12:10Z bucket (T1/F9), and the first ERROR log is 12:12:31Z. No other change lies in this window (T2: the only other deploy, portfolio-service v4.2.1, was at 09:36Z, ~2h46m earlier; no config change in 11:50-13:59Z).
  - `F17` ✔ pricing-engine — order-service's other HTTP dependency — shows no error anomaly at all: its error_rate is flat 0.19-0.25% avg (max 0.28-0.32%) across every 11:00-13:48Z bucket, including the 12:12Z bucket. It is not temporally correlated with the onset.
  - `F16` ✔ The shared components flagged in T2 show NO anomaly at or around onset: ledger-db connections_active is flat (avg 61.0-63.1 connections across every 11:00-13:48Z bucket, min 56.25 / max 67.78 overall — no pool exhaustion), and kafka under_replicated_partitions is 0.0 for every bucket across the whole window (no broker/rebalance problem).
**Assumptions**
  - An identifiable upstream service actually emits marginProfile and degraded at ~12:10Z — telemetry for it does not exist (GAPS: margin-service/account-service not_found), so this is unverified.
  - A dependency-side change occurred but is absent from change history (F2/F3/F4 show nothing).

### H3: Shared-component degradation (ledger-db pool exhaustion or kafka broker problem) coinciding with onset
A shared component in order-service's dependency graph — most plausibly ledger-db connection pool exhaustion (per precedent HIST-115) or a kafka broker/rebalance issue — degraded around 12:10Z and caused order placement 500s; the deploy timing is coincidence.

**Causal chain**
1. ledger-db / kafka are shared components in order-service's dependency path [F5]
2. A pool/broker fault would surface as connection/timeout errors and elevated latency in order-service *(assumed)*
3. order-service returns 500 on affected requests [F8]

**Supporting evidence**
  - `F5` ✔ order-service dependency map: outbound edges to ledger-db (db), pricing-engine (http), exchange-gateway (http), kafka (queue-produce); inbound from api-gateway (http); pricing-engine itself calls market-data-service and redis-cache. Shared components (shared:true) are ledger-db (postgres), redis-cache (redis) and kafka (kafka); exchange-api is external with has_telemetry:false.
  - `F6` ✔ Precedents (leads only, not evidence about this incident): HIST-115 (2025-09-25) 'order-service connection pool misconfiguration', category config_change, symptoms 'connection is not available', 'request timed out', 'pool exhausted', resolution 'Reverted config'; HIST-110 (2026-01-15) 'Slow orders from ledger-db IOPS throttling'; HIST-103 (2025-10-21) order failures/504 during exchange-api outage; HIST-104 (2025-11-05) notification lag after Kafka broker rebalance; HIST-109 (2025-07-11) 'Token validation regression after auth library upgrade', category bad_deployment.
**Contradicting evidence**
  - `F16` ✔ The shared components flagged in T2 show NO anomaly at or around onset: ledger-db connections_active is flat (avg 61.0-63.1 connections across every 11:00-13:48Z bucket, min 56.25 / max 67.78 overall — no pool exhaustion), and kafka under_replicated_partitions is 0.0 for every bucket across the whole window (no broker/rebalance problem).
  - `F7` ✔ order-service ERROR logs start at 12:12:31Z with 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO' and continue through 13:58:50Z; 426 ERROR entries were matched, all of the two marginProfile/NPE patterns.
  - `F12` ✔ order-service latency_p99_ms is flat across the whole window (144-165ms, every 8-min bucket 11:00-13:56Z ~147-153ms), showing no latency degradation accompanying the error spike.
  - `F11` ✔ Every error sample names orderType=FNO; a filtered query for 'orderType=' returned 224 ERROR matches, 224 of which are the FNO MarginCheckValidator NPE pattern, and no non-FNO error pattern appears in the top_patterns.
  - `F1` ✔ order-service was deployed to v2.14.0 at 2026-03-03T12:10:00Z by r.iyer, 12 minutes before the 12:22Z report; summary is 'Refactor order validation; add margin check for F&O orders' with changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'. Status is 'live' with rolled_back_at: null.
  - `F13` ✔ order-service v2.14.0 rollout completed at 12:10:08Z ('deployment dep-5101 (v2.14.0) rollout complete, all pods ready'), and the very next ERROR entries in the same log stream begin at 12:12:31Z with the MarginCheckValidator NPE — no ERROR of any kind appears in this stream before the rollout; the preceding INFO entries (11:56:19Z health check ok, 11:59:51Z scheduled job heartbeat ok, 12:07:51Z config reloaded (no changes)) show a clean pre-rollout state.
  - `F9` ✔ The error rate metric crosses from baseline to incident level in the 12:10:00Z 5-minute bucket: avg 11.17% / max 19.63%, versus 0.16-0.26% for every bucket from 11:00 to 12:05Z. Onset is therefore ~12:10Z, i.e. about 12 minutes before the 12:22Z report.
**Assumptions**
  - T2's failed ledger-db/kafka error_rate queries (tc-018/tc-019) mean those services are not independently confirmed healthy, though their direct pool/replica metrics are flat.
**Rejected because:** Directly contradicted by F16: ledger-db connections_active is flat (56-68, no pool exhaustion) and kafka under_replicated_partitions is 0.0 across the whole window, ruling out the HIST-115 and HIST-104 signatures. Also contradicted by the error mechanism (F7/F8): the failure is a Java NPE on a null margin profile in a newly-added validator, not 'connection is not available' / 'request timed out'; and F12 shows no latency degradation, which a DB/broker fault would produce.

### H4: External exchange-api degradation re-run of the HIST-103 failure mode
The external exchange-api (has_telemetry:false, no change history available) degraded and order-service surfaced the failure as 500s, repeating the HIST-103 order-failure pattern. The derivatives-order skew reflects which order types route to the external venue.

**Causal chain**
1. order-service has an outbound edge to exchange-gateway and external exchange-api with no telemetry [F5]
2. exchange-api degrades, violating its SLA *(assumed)*
3. order-service surfaces errors from that path as 500s *(assumed)*

**Supporting evidence**
  - `F5` ✔ order-service dependency map: outbound edges to ledger-db (db), pricing-engine (http), exchange-gateway (http), kafka (queue-produce); inbound from api-gateway (http); pricing-engine itself calls market-data-service and redis-cache. Shared components (shared:true) are ledger-db (postgres), redis-cache (redis) and kafka (kafka); exchange-api is external with has_telemetry:false.
  - `F6` ✔ Precedents (leads only, not evidence about this incident): HIST-115 (2025-09-25) 'order-service connection pool misconfiguration', category config_change, symptoms 'connection is not available', 'request timed out', 'pool exhausted', resolution 'Reverted config'; HIST-110 (2026-01-15) 'Slow orders from ledger-db IOPS throttling'; HIST-103 (2025-10-21) order failures/504 during exchange-api outage; HIST-104 (2025-11-05) notification lag after Kafka broker rebalance; HIST-109 (2025-07-11) 'Token validation regression after auth library upgrade', category bad_deployment.
**Contradicting evidence**
  - `F7` ✔ order-service ERROR logs start at 12:12:31Z with 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO' and continue through 13:58:50Z; 426 ERROR entries were matched, all of the two marginProfile/NPE patterns.
  - `F8` ✔ The 500 error signature is a null margin profile: 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202 occurrences), alongside 224 NPE-at-MarginCheckValidator occurrences.
  - `F14` ✔ The failing error signature is a newly-introduced validator: two patterns dominate order-service logs — 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO' and 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' — matching the v2.14.0 change 'new MarginCheckValidator in validation chain'. Both first appear only after 12:10Z (12:12:31Z and 12:12:44Z respectively).
  - `F13` ✔ order-service v2.14.0 rollout completed at 12:10:08Z ('deployment dep-5101 (v2.14.0) rollout complete, all pods ready'), and the very next ERROR entries in the same log stream begin at 12:12:31Z with the MarginCheckValidator NPE — no ERROR of any kind appears in this stream before the rollout; the preceding INFO entries (11:56:19Z health check ok, 11:59:51Z scheduled job heartbeat ok, 12:07:51Z config reloaded (no changes)) show a clean pre-rollout state.
  - `F12` ✔ order-service latency_p99_ms is flat across the whole window (144-165ms, every 8-min bucket 11:00-13:56Z ~147-153ms), showing no latency degradation accompanying the error spike.
  - `F11` ✔ Every error sample names orderType=FNO; a filtered query for 'orderType=' returned 224 ERROR matches, 224 of which are the FNO MarginCheckValidator NPE pattern, and no non-FNO error pattern appears in the top_patterns.
**Assumptions**
  - An undetectable vendor-side change occurred at ~12:10Z — pure supposition; no telemetry or change history exists for exchange-api.
**Rejected because:** No mechanism match: the observed errors are a Java NullPointerException inside order-service's own newly-deployed MarginCheckValidator (F7/F8/F14), not an exchange/RPC error, timeout, or 504 as in HIST-103. F12 shows no latency change (external dependency faults typically add latency), and HIST-103 is a past-incident lead, not evidence about this incident.

## Facts (verified observations)
- order-service was deployed to v2.14.0 at 2026-03-03T12:10:00Z (deployment dep-5101, rollout complete 12:10:08Z, all pods ready, rolled_back_at null), 12 minutes before the 12:22Z report; the release summary is 'Refactor order validation; add margin check for F&O orders' with changes 'new MarginCheckValidator in validation chain' and 'remove legacy OrderRules.validateMargin()'. [F1, F13, F14]
- order-service ERROR logs begin at 12:12:31Z with 'NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO' and continue through 13:58:50Z; 426 ERROR entries matched the two marginProfile/NPE patterns. [F7, F8, F14]
- The 500 error signature is a null margin profile: 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>' (202 occurrences) alongside 224 NPE-at-MarginCheckValidator occurrences. [F8, F7]
- The order-service error rate crossed from a 0.16-0.26% baseline (every 11:00-12:05Z bucket) to 11.17% avg / 19.63% max in the 12:10:00Z bucket, i.e. onset ~12:10Z. [F9]
- Error rate plateaus at ~18-19% and had not recovered by the latest data (buckets 12:15Z through 13:55Z avg 18.0-19.0%, last bucket 13:55Z avg 18.74%). [F10]
- All error samples name orderType=FNO: a filtered query for 'orderType=' returned 224 ERROR matches, all of them the FNO MarginCheckValidator NPE pattern, with no non-FNO error pattern in the top patterns. [F11]
- No order-service ERROR of any kind appears in the log stream before the 12:10Z rollout; preceding entries (11:56:19Z health check ok, 11:59:51Z job heartbeat ok, 12:07:51Z config reloaded with no changes) show a clean pre-rollout state. [F13]
- No competing deployment or config change lies immediately before onset: the only other deploy in 24h is portfolio-service v4.2.1 at 09:36Z (~2h46m earlier), the only config change is notification-service email.batch_size at 11:21Z (not in order-service's dependency graph), and a tighter query for 11:50Z-13:59Z returned count 0. [F2, F3, F4]
- The shared components flagged as candidates show no anomaly around onset: ledger-db connections_active is flat (avg 61.0-63.1 per bucket, min 56.25 / max 67.78 overall) and kafka under_replicated_partitions is 0.0 for every bucket across the whole window. [F16]
- order-service latency_p99_ms is flat across the whole window (144-165ms; every 8-min bucket 11:00-13:56Z ~147-153ms), with no latency degradation accompanying the error spike. [F12]
- pricing-engine, order-service's other HTTP dependency, shows no error anomaly at all (error_rate flat 0.19-0.25% avg, max 0.28-0.32%) across every 11:00-13:48Z bucket including 12:12Z. [F17]
- api-gateway error_rate jumps in the same 12:12Z bucket as the order-service errors, from a 0.20-0.28% baseline to avg 3.41% / max 3.66%, staying elevated through 13:48Z, consistent with downstream propagation of order-service 500s. [F15]
- order-service's dependency map has outbound edges to ledger-db, pricing-engine, exchange-gateway and kafka, inbound from api-gateway; exchange-api is external with has_telemetry:false. [F5]

## Assumptions (not verified)
- Assumption: the marginProfile value omitted for FNO orders was previously defaulted or computed by the removed legacy OrderRules.validateMargin() code path, and the new validator does not replicate that fallback. This mechanism is inferred from F1 ('remove legacy OrderRules.validateMargin()') plus the observed NPE (F7/F8) rather than directly observed.
- Assumption: no config or feature-flag change contributed to the incident; this rests on F4, which depends on the change-history tool's completeness.
- Assumption: the null margin profile is an input condition (bad data or unchanged upstream behavior) rather than evidence of an independent upstream outage; upstream margin/account services are not visible in telemetry, so this is argued from the clean timing of every other change in the window rather than from direct upstream observation.
- Assumption: the deploy went to all instances of order-service, so the blast radius matches the full ~18-19% error rate; per-instance version mix was not retrieved.
- Assumption: api-gateway's elevated error rate is a downstream consequence of order-service 500s rather than an independent api-gateway fault; this rests on timing (F15) and the flat behavior of order-service's other dependencies, not on api-gateway logs naming order-service.

## Timeline
- `2026-03-03T09:36:00Z` Only other deployment in the 24h window: portfolio-service v4.2.1 ('Dependency bumps', grpc 1.62 -> 1.63), ~2h46m before onset. [F2]
- `2026-03-03T11:21:00Z` Only config change in 24h: notification-service email.batch_size 200 -> 250 (config-bot); notification-service is not in order-service's dependency graph. [F3]
- `2026-03-03T11:50:00Z` Start of tighter config-change window covering the onset; query 11:50Z-13:59Z returns count 0 (no config or flag change anywhere). [F4]
- `2026-03-03T12:10:00Z` order-service error rate crosses from 0.16-0.26% baseline to avg 11.17% / max 19.63% in the 12:10Z 5-minute bucket; onset ~12:10Z. [F9, F18]
- `2026-03-03T12:10:08Z` order-service v2.14.0 (deployment dep-5101) rollout completes, all pods ready; release adds MarginCheckValidator and removes legacy OrderRules.validateMargin(). [F1, F13]
- `2026-03-03T12:12:31Z` First order-service ERROR: NullPointerException at MarginCheckValidator.validate(MarginCheckValidator.java:88) orderType=FNO; no ERROR appears in the same stream before the rollout. [F7, F13, F14]
- `2026-03-03T12:12:44Z` Second error signature appears: 'order rejected with 500: java.lang.NullPointerException: marginProfile is null for account <n>'. [F8, F14]
- `2026-03-03T12:12:00Z` api-gateway error_rate jumps from 0.20-0.28% baseline to avg 3.41% / max 3.66% in the same 12:12Z bucket, consistent with downstream propagation from order-service. [F15]
- `2026-03-03T12:12:00Z` Shared components show no anomaly at onset: ledger-db connections_active flat and kafka under_replicated_partitions 0.0 across every bucket; pricing-engine error_rate also flat. [F16, F17]
- `2026-03-03T12:22:00Z` Incident INC-001 [SEV-1] reported by on-call SRE: spike in failed order placements, mostly derivatives orders per support tickets. [F9]
- `2026-03-03T13:55:00Z` Error rate still plateaued at ~18-19% (last bucket 13:55Z avg 18.74%); latency_p99_ms remains flat 147-153ms throughout. [F10, F12]
- `2026-03-03T13:58:50Z` Latest matched order-service ERROR entry; the incident is ongoing at the end of the data window. [F7]

## Recommended actions
- **[immediate]** Roll back order-service from v2.14.0 to the immediately preceding version (revert/roll back deployment dep-5101) and confirm pods are serving the prior version. -- The fault is isolated to the new release: rollout completed 12:10:08Z, error rate crossed at the 12:10Z bucket, the first ERROR in the log stream is the MarginCheckValidator NPE and appears only after the rollout, and no other change lies in the window. Rollback is the fastest route to stopping the FNO order failures. [F1, F9, F13, F14, F18]
- **[immediate]** If rollback is not immediately possible, disable/bypass the MarginCheckValidator step for orderType=FNO in the validation chain (feature flag or hotfix) rather than disabling order validation generally. -- All 224 matched error samples name orderType=FNO and the NPE is at MarginCheckValidator.java:88, so the null-margin path is the failing step; a targeted bypass preserves the rest of the validation chain. [F7, F8, F11]
- **[immediate]** Do not spend mitigation effort on ledger-db connection pools, Kafka brokers, or exchange-api; keep their standard monitoring running only. -- ledger-db connections_active is flat (no pool exhaustion, ruling out the HIST-115 signature) and kafka under_replicated_partitions is 0.0 all window (ruling out the HIST-104 signature); the error mechanism is a Java NPE, not a timeout or 504, and latency is flat, so HIST-103-style external degradation does not match. [F16, F12, F7, F8]
- **[short_term]** Verify recovery after rollback: confirm order-service error_rate returns to the 0.16-0.26% baseline and that the MarginCheckValidator NPE signature stops appearing in order-service logs. -- Error rate was still plateaued at ~18.7% in the 13:55Z bucket with the last ERROR at 13:58:50Z, so recovery must be demonstrated against those same signals. [F9, F10, F7]
- **[short_term]** Fix MarginCheckValidator to handle a null/absent marginProfile for FNO orders (restore the fallback behavior removed with legacy OrderRules.validateMargin(), or fail closed with a typed 4xx business error rather than an unhandled NPE/500) and ship as v2.14.1. -- The root cause is the new validator dereferencing a null margin profile; the legacy code path that handled it was removed in the same release, and today the condition surfaces as HTTP 500 instead of a client-visible business rejection. [F1, F7, F8, F14]
- **[short_term]** Instrument and confirm the margin-profile data path for FNO orders: log/annotate whether marginProfile is populated upstream, and add a null-input guard so an absent profile is distinguishable from a data-path failure. -- The upstream source of marginProfile is not identifiable from available service telemetry (margin-service/account-service do not exist as services), so the residual H2 possibility cannot be closed out without explicit input instrumentation; this also prevents recurrence of the same unhandled-NPE failure mode. [F7, F8]
- **[long_term]** Add a deploy-gated canary/guardrail for order-service: block or auto-rollback releases whose error rate on the FNO validation path exceeds a small threshold within N minutes of rollout completion. -- The change-history record shows the deploy was still live with rolled_back_at null more than 100 minutes after onset and the error rate stayed plateaued, while no automated guardrail halted a release whose first ERROR appeared 2 minutes 23 seconds after rollout completion. [F1, F13, F10]
- **[long_term]** Add an automated pre-release check that validates handling of null/optional inputs in newly added validation-chain steps, and require the removal of legacy code paths to be covered by equivalent-input tests. -- The failing condition is an unchecked assumption in new code replacing removed legacy behavior; a test asserting FNO orders with an absent margin profile would have caught the NPE at MarginCheckValidator.java:88 before rollout. [F1, F7, F8, F14]

## Data gaps
- No metric queries were completed for order-service resource health (restarts, cpu_pct) or redis-cache (hit_ratio_pct, memory_pct, evictions_per_min) - the redis-cache call returned budget_exhausted. redis-cache is a shared component in order-service's dependency graph, so it is not independently confirmed anomaly-free, and resource-based causation cannot be fully excluded.
- The source of the null marginProfile could not be identified: error_rate queries for margin-service and account-service failed not_found, and the known_services list contains no dedicated margin/account service. This is the main thing keeping the conclusion at medium confidence, since the H1 (new-code assumption) vs H2 (data-path change) distinction cannot be settled from upstream telemetry.
- No canary/partial-rollout or per-instance version-mix data was retrieved for dep-5101 v2.14.0, so the deployment blast radius across instances is unknown.
- Change and deployment history cannot be inspected for exchange-api (external, has_telemetry:false), so an undetectable vendor-side change cannot be excluded from change history alone - it is excluded only on mechanism and latency grounds.
- A broad multi-service error_rate comparison and filtered api-gateway ERROR log query for 12:00-12:30Z were not run (budget_exhausted), so the api-gateway-to-order-service causal direction rests on timing (F15) rather than on api-gateway logs naming order-service, and concurrent anomalies in neighbouring services are unverified.
- No query_logs scans for 'timeout' or 'RPC' were completed, so downstream RPC/dependency failures mentioned in the gap notes remain unconfirmed (consistent with the NPE mechanism, but not independently checked).
- Tool failures: tc-015/tc-016 (margin-service, account-service error_rate) and tc-018/tc-019/tc-020 (ledger-db, kafka, redis-cache error_rate) all returned not_found, so several shared components were assessed only via their own dedicated metrics (connections_active, under_replicated_partitions) rather than via error rate.

## What would change this conclusion
- Evidence that the FNO marginProfile nulls began (or the margin-profile data path degraded) strictly before the 12:10:08Z rollout, e.g. upstream margin-profile lookups returning null earlier in the day - that would move the origin toward H2 (upstream/data dependency) rather than the new code.
- A successful rollback that does NOT restore the baseline error rate, or that leaves the MarginCheckValidator NPE still appearing, which would invalidate H1 as the root cause.
- Identification of an upstream margin/account service whose error/latency telemetry shows an onset at or before 12:10Z, indicating the null profiles are themselves the failure while the deploy merely exposed them.
- Discovery of a config/flag change affecting the margin-profile path within 11:50-13:59Z that the config-change tool missed, which would weaken the 'no competing change' pillar of F4/F18.
- Evidence that the errors are not deploy-correlated on a per-instance basis (e.g. instances still on the prior version also throwing the MarginCheckValidator NPE), which would break the deploy-to-error correspondence.
- Recovery of api-gateway logs showing its 500s fail for reasons other than order-service, which would make api-gateway an independent contributor rather than a downstream casualty.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6

## Investigation stats
- latency 138.44s, LLM calls 13, tokens 85266 (in 68305 / out 16961)
- tool calls 24 (5 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
