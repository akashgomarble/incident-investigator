# Incident investigation report: INC-012
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause (confidence ~0.64-0.85, medium-high): the shared Postgres ledger-db ran out of disk space, so it could no longer extend relation files or write pg_wal temporary WAL segments, and every writer to that shared database failed at once. ledger-db disk_used_pct climbed smoothly from 78.16% at 11:00Z to 100% by ~14:56Z — a gradual, ~9-hour pre-existing ramp — then stayed pinned at 100% with no reclaim, and the failure persisted unchanged long after the v6.0.0 rollback at 15:12Z. payment-service v6.0.0 (dep-6203) is incidental: the disk was already ~99% at its 14:46Z deploy, the error signature is storage-layer, and settlement-service and api-gateway failed in the same 15:00Z bucket despite never running the mandate flow. The mandate-flow / new MANDATE_HOLD entry-type hypotheses are positively killed (zero enum/constraint/MANDATE_HOLD matches in either service's logs). Remaining uncertainty: the precise growth mechanism (retained WAL vs base-table/autovacuum) is unpinned, and a neighbour write-flood into the same shared DB (H4) cannot be formally excluded because no per-writer write/QPS metric exists.

## Probable root cause
**Shared ledger-db storage exhaustion (disk full / WAL) on the DB node itself — independent of v6.0.0** -- resource_exhaustion in `ledger-db`, triggered by `null` (confidence 0.64, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Shared ledger-db storage exhaustion (disk full / WAL) on the DB node itself — independent of v6.0.0 | resource_exhaustion | ledger-db | null | 0.64 (medium) | active |
| 2 | H4: A neighbour writer floods the shared ledger-db, driving the disk to exhaustion for all writers | capacity_saturation | order-service | null | 0.18 (low) | active |
| 3 | H2: v6.0.0 mandate-flow regression / new MANDATE_HOLD entry type breaks ledger writes (reporter's suspicion) | bad_deployment | payment-service | dep-6203 | 0.04 (low) | rejected |
| 4 | H3: Cross-service contract mismatch: ledger-db schema/enum rejects MANDATE_HOLD | config_change | ledger-db | null | 0.03 (low) | rejected |

### H1: Shared ledger-db storage exhaustion (disk full / WAL) on the DB node itself — independent of v6.0.0
The shared Postgres ledger-db ran out of disk space when its volume (WAL / base-table growth) filled to 100%, so it could not extend relation files or write pg_wal temp WAL segments ('No space left on device'). Every writer to that shared DB — payment-service, settlement-service, order-service, portfolio-service — failed at the same instant. The fill is a long, steady, pre-existing ramp (78.16% at 11:00Z rising monotonically to 100% at ~14:56Z, ~9 hours in progress) that is neither stepwise nor aligned to any deployment, and the failure persists unchanged long after the 15:12Z v6.0.0 rollback with no reclaim event. v6.0.0 is incidental: the disk was already ~99% when it deployed at 14:46Z. This matches the historical ledger-db storage pattern (HIST-114 'retained WAL until disk filled').

**Causal chain**
1. ledger-db disk_used_pct climbs steadily from 78.16% (11:00Z) to 100% (~14:56Z) — a gradual ~9h pre-existing ramp, not a deploy-aligned step [F31, F32]
2. Volume reaches the 100% ceiling ~4 min before the 15:00Z error onset and stays pinned at exactly 100.0% through 16:28Z with no reclaim/step drop anywhere [F31, F33]
3. Postgres cannot create pg_wal/xlogtemp.* nor extend base/ relation files → 'PANIC: could not write to file pg_wal/xlogtemp.<n>: No space left on device' and 'could not extend file base/<n>/<n>: No space left on device' [F9, F21]
4. payment-service writes to the shared ledger-db fail at the write syscall/commit → 179/179 ERROR logs 'ledger write failed: ERROR: could not extend file: No space left on device' [F4, F8, F16]
5. Because ledger-db is shared:true, every other writer fails simultaneously: settlement-service 0.26%→53.56%, api-gateway 0.27%→16.43% in the same 15:00Z bucket [F4, F10, F17]
6. The failure signature is storage-layer only — zero matches for 'MANDATE_HOLD', 'enum', 'unknown entry type' or 'check constraint' in either service, so no schema/entry-type rejection occurred [F12, F19, F20, F24]
7. Rollback at 15:12Z produced no recovery: exhaustion errors recur unchanged (base/ relation errors at 15:12:32–15:13:30Z, pg_wal PANICs through 16:57:21Z) and disk stays pinned at 100% [F22, F23, F31, F33]
8. replication_lag_ms rises steadily and does not recover (~3025ms at 14:00Z → 4836ms by 16:28Z), consistent with ongoing storage/WAL pressure on the node [F36]

**Supporting evidence**
  - `F31` ✔ ledger-db disk_used_pct rises monotonically and gradually from 94.76% at 14:00Z to 100.0% at ~14:56Z, reaching the 100% ceiling ~4-10 minutes BEFORE the 15:00Z error onset, then stays pinned at exactly 100.0% in every bucket from 15:00Z through 16:28Z — i.e. there is NO reclaim/step drop and NO recovery at any point in the window, including after the 15:12Z rollback.
  - `F32` ✔ The disk fill is a long, pre-existing, steady ramp — not stepwise and not aligned to any deployment: disk_used_pct climbs smoothly from 78.16% at 11:00Z to 100% at 15:00Z (~0.55%/6min, ~21.8 percentage points over 4 hours), i.e. the exhaustion was already ~9 hours in progress before the 14:46Z v6.0.0 deploy and the 15:10Z report. This establishes the fill as gradual/pre-existing rather than a deploy-induced step.
  - `F33` ✔ Explicitly: NO WAL-prune / free-space reclaim event is visible in ledger-db disk_used_pct over 11:00Z-16:28Z. The series never decreases by any bucket — it is strictly non-decreasing to the 100% ceiling where it flattens; there is no step drop after the 15:12Z rollback (still avg 100.0 at 15:12Z and 16:28Z).
  - `F8` ✔ The overwhelming payment-service error signature is a ledger write failure: 179/179 ERROR logs match 'ledger write failed: ERROR: could not extend file: No space left on device'.
  - `F9` ✔ The ledger DB (ledger-db; there is no 'ledger-service') emits 143 ERROR logs in the same window, with two patterns: 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (72x, first 15:01:01Z) and 'could not extend file "base/<n>/<n>": No space left on device' (71x, first 15:01:39Z).
  - `F21` ✔ ledger-db 'No space left on device' errors total 292 in 15:00Z-16:59Z, first 15:01:01Z, last 16:57:21Z, split into two patterns: 'PANIC: could not write to file "pg_wal/xlogtemp.<n>"' (153x, first 15:01:01Z) and 'could not extend file "base/<n>/<n>"' (139x, first 15:01:39Z).
  - `F22` ✔ Disk-exhaustion errors continue through the late window with no signature change after the 15:12Z rollback: pg_wal/xlogtemp PANICs recur from 16:01:03Z through 16:57:21Z (81 matches in the 16:00-16:59 sub-window alone), and the last 'No space left on device' record overall is 16:57:21Z.
  - `F23` ✔ Both exhaustion patterns persist past the rollback instant 15:12Z with unchanged message form — e.g. 'could not extend file "base/16402/2619": No space left on device' at 15:12:32Z, 15:12:45Z, 15:13:30Z, and pg_wal PANIC at 15:12:41Z — so the rollback did not clear or alter the failure mode.
  - `F10` ✔ Blast radius is not payment-service alone: settlement-service error_rate rose from ~0.26% at 14:58Z to 53.56% at 15:00Z and api-gateway from ~0.27% to 16.43% in the same 15:00Z bucket, both sustained afterward.
  - `F12` ✔ The observed failure mode is storage/disk exhaustion on the ledger database (No space left on device, incl. pg_wal WAL temp files), not a schema/entry-type error — no 'unknown ledger entry type' or mandate-flow error appears among the 179 payment-service ERROR logs.
  - `F19` ✔ In the full 15:00Z-16:59Z window there are ZERO log matches for any of the mandate/schema signature terms in payment-service: 'MANDATE_HOLD' = 0 matches (result untruncated, total_matched 0).
  - `F20` ✔ ledger-db logs for 15:00Z-16:59Z contain ZERO matches for 'MANDATE_HOLD', 'enum', 'unknown entry type', and 'check constraint' — all four queries returned total_matched 0, untruncated. The mandated new-ledger-entry-type / enum / constraint failure modes are absent.
  - `F24` ✔ Formal hypothesis status: H2/H3 (mandate flow / new ledger entry type producing an unknown-entry-type, enum, or constraint violation) is KILLED — none of those terms occurs anywhere in either service's late-window logs, while H1 (independent ledger-db storage exhaustion, matching HIST-114 'ledger-db disk full from retained WAL') is REVIVED/strengthened: the only failure signature present is disk exhaustion, and it outlives the v6.0.0 rollback.
  - `F4` ✔ payment-service writes to ledger-db (kind 'db'), and ledger-db is flagged shared:true, used directly by order-service, portfolio-service, payment-service and settlement-service. kafka is also shared:true. This makes ledger-db a shared component through which a neighbour could affect payment-service.
  - `F17` ✔ ledger-db is shared and is written by three other services besides payment-service: order-service, portfolio-service and settlement-service all have db edges to ledger-db — so a schema change to the ledger entry type could in principle affect all of them, and conversely they are neighbours that could hurt payment-service through the shared database.
  - `F36` ✔ replication_lag_ms on ledger-db does not recover after the 15:12Z rollback but rises steadily across the whole window: from ~3025ms at 14:00Z to 4027ms at 15:12Z and up to ~4836ms by 16:28Z (max 5070.22), consistent with ongoing, unrecovered storage pressure rather than a transient blip cleared by the rollback.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The exact growth mechanism (retained WAL from an inactive replication slot vs base-table/relation growth vs autovacuum) is not pinned; no WAL-size or archive-backlog metric exists. F36's rising replication_lag is suggestive of WAL retention but is not direct evidence.
  - No positive exclusion that v6.0.0's extra mandate writes contributed the final fraction of a percent — but the disk was already ~99% at its 14:46Z deploy, so exhaustion was imminent regardless.
  - No log content after ~15:13Z was individually inspected, though totals/last-timestamps (16:57:21Z) and the disk curve confirm late-window persistence.

### H4: A neighbour writer floods the shared ledger-db, driving the disk to exhaustion for all writers
One of the other shared-ledger-db writers (order-service, portfolio-service, settlement-service) began issuing an abnormal volume of writes/long transactions, growing WAL/temp space and filling the shared volume, so payment-service (a co-tenant) fails at 60% and settlement-service at 53% as co-victims. The fault originates outside both payment-service and the ledger-db node itself. The mechanism (disk exhaustion of shared storage) and blast radius are identical to H1; only the origin of the growth differs.

**Causal chain**
1. ledger-db is shared:true and written directly by payment-, order-, portfolio- and settlement-service [F4, F17, F25]
2. a neighbour's write/transaction volume increased around 15:00Z (unmeasured — no per-service write/QPS or transaction-rate metric is reachable with the available tools, and error_rate for order-service was never queried) *(assumed)*
3. shared volume / WAL temp space fills → Postgres 'No space left on device' for all writers [F9, F21]
4. all writers fail simultaneously: payment 60.65%, settlement 53.56%, api-gateway 16.43% in the same 15:00Z bucket [F7, F10]

**Supporting evidence**
  - `F4` ✔ payment-service writes to ledger-db (kind 'db'), and ledger-db is flagged shared:true, used directly by order-service, portfolio-service, payment-service and settlement-service. kafka is also shared:true. This makes ledger-db a shared component through which a neighbour could affect payment-service.
  - `F9` ✔ The ledger DB (ledger-db; there is no 'ledger-service') emits 143 ERROR logs in the same window, with two patterns: 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (72x, first 15:01:01Z) and 'could not extend file "base/<n>/<n>": No space left on device' (71x, first 15:01:39Z).
  - `F10` ✔ Blast radius is not payment-service alone: settlement-service error_rate rose from ~0.26% at 14:58Z to 53.56% at 15:00Z and api-gateway from ~0.27% to 16.43% in the same 15:00Z bucket, both sustained afterward.
  - `F17` ✔ ledger-db is shared and is written by three other services besides payment-service: order-service, portfolio-service and settlement-service all have db edges to ledger-db — so a schema change to the ledger entry type could in principle affect all of them, and conversely they are neighbours that could hurt payment-service through the shared database.
  - `F25` ✔ ledger-db is a shared postgres with exactly four direct writers: order-service, portfolio-service, payment-service and settlement-service all have db edges to ledger-db (tc-027/tc-045). This is the set of services whose write load could flood ledger-db, so H4 depends on any of these three non-payment writers.
**Contradicting evidence**
  - `F27` ✔ No deployment by ANY writer occurred near the 15:00Z onset: across all services in 13:00-16:59Z the only deployment was payment-service v6.0.0 (dep-6203) at 14:46Z, and order-service specifically had zero deployments in the 2026-05-25 to 15:00Z lookback. There is no release-based evidence that a neighbour (order/portfolio/settlement) introduced new write load around onset.
  - `F32` ✔ The disk fill is a long, pre-existing, steady ramp — not stepwise and not aligned to any deployment: disk_used_pct climbs smoothly from 78.16% at 11:00Z to 100% at 15:00Z (~0.55%/6min, ~21.8 percentage points over 4 hours), i.e. the exhaustion was already ~9 hours in progress before the 14:46Z v6.0.0 deploy and the 15:10Z report. This establishes the fill as gradual/pre-existing rather than a deploy-induced step.
  - `F34` ✔ connections_active on ledger-db does NOT change after the rollback and shows no flood spike: it oscillates in a narrow 60-66 range (avg 59.5 at 14:00Z, 61.43 at 15:00Z, brief max 68.34 at 15:08Z, 61.92 at 15:12Z, 62.53 at 16:28Z), mean 62.27 over the window with 0 missing points. No connection surge accompanies the 15:00Z onset.
  - `F35` ✔ ledger-db cpu_pct and slow_queries_per_min show no saturation or step at onset, so these metrics contain no positive evidence of a query/IO flood from a neighbour service: cpu_pct stays flat ~29-31% (29.57 at 15:00Z, 30.12 at 15:12Z, 29.99 at 16:28Z, mean 29.99), and slow_queries_per_min stays flat ~1/min (0.97 at 15:00Z, 0.99 at 15:12Z, 1.05 at 16:28Z, mean 1.0, min 0.41 max 1.75).
**Assumptions**
  - A neighbour's write volume actually increased near 15:00Z. No writer metric was obtainable (F26), no writer deployment or config change exists near onset (F27, F28), and the disk ramp is smooth/constant over 9h with no load-like inflection (F32).
  - The flat connections_active (60-66), cpu_pct (~30%) and slow_queries_per_min (~1/min) on ledger-db give no positive evidence of a flood, but a pure write-volume surge is not fully excluded by these metrics.

### H2: v6.0.0 mandate-flow regression / new MANDATE_HOLD entry type breaks ledger writes (reporter's suspicion)
payment-service v6.0.0 introduced the new ledger entry type MANDATE_HOLD and a mandatory write on the mandate create/approve path. Either the writer-side code is buggy (over-sized/unbounded writes, WAL amplification, retry storm) or ledger-db rejects the new entry type, so the new flow generates ledger write failures that escalate payment-service's error rate to 60%.

**Causal chain**
1. dep-6203 ships mandate create/approve endpoints and the new ledger entry type MANDATE_HOLD at 14:46Z [F1, F13]
2. the new mandate path issues additional/large ledger writes (writer-side bug) or a rejected entry type *(assumed)*
3. ledger-db write returns an error → 'ledger write failed' surfaces in payment-service at 60% error rate [F8]

**Supporting evidence**
  - `F1` ✔ Across all services, only one deployment occurred in the entire incident window 13:00-16:59Z: payment-service v6.0.0 (dep-6203) deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes ['mandate create/approve endpoints','new ledger entry type MANDATE_HOLD'], status 'rolled_back' with rolled_back_at 2026-06-01T15:12:00Z. This matches the report's ~15-minutes-after-release onset.
  - `F13` ✔ The only deployment across ALL services in 14:30-16:59 was payment-service v6.0.0 (dep-6203), shipped 2026-06-01T14:46:00Z by p.nair with changes ['mandate create/approve endpoints','new ledger entry type MANDATE_HOLD'], status rolled_back at 2026-06-01T15:12:00Z.
  - `F8` ✔ The overwhelming payment-service error signature is a ledger write failure: 179/179 ERROR logs match 'ledger write failed: ERROR: could not extend file: No space left on device'.
**Contradicting evidence**
  - `F8` ✔ The overwhelming payment-service error signature is a ledger write failure: 179/179 ERROR logs match 'ledger write failed: ERROR: could not extend file: No space left on device'.
  - `F9` ✔ The ledger DB (ledger-db; there is no 'ledger-service') emits 143 ERROR logs in the same window, with two patterns: 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (72x, first 15:01:01Z) and 'could not extend file "base/<n>/<n>": No space left on device' (71x, first 15:01:39Z).
  - `F12` ✔ The observed failure mode is storage/disk exhaustion on the ledger database (No space left on device, incl. pg_wal WAL temp files), not a schema/entry-type error — no 'unknown ledger entry type' or mandate-flow error appears among the 179 payment-service ERROR logs.
  - `F10` ✔ Blast radius is not payment-service alone: settlement-service error_rate rose from ~0.26% at 14:58Z to 53.56% at 15:00Z and api-gateway from ~0.27% to 16.43% in the same 15:00Z bucket, both sustained afterward.
  - `F19` ✔ In the full 15:00Z-16:59Z window there are ZERO log matches for any of the mandate/schema signature terms in payment-service: 'MANDATE_HOLD' = 0 matches (result untruncated, total_matched 0).
  - `F20` ✔ ledger-db logs for 15:00Z-16:59Z contain ZERO matches for 'MANDATE_HOLD', 'enum', 'unknown entry type', and 'check constraint' — all four queries returned total_matched 0, untruncated. The mandated new-ledger-entry-type / enum / constraint failure modes are absent.
  - `F22` ✔ Disk-exhaustion errors continue through the late window with no signature change after the 15:12Z rollback: pg_wal/xlogtemp PANICs recur from 16:01:03Z through 16:57:21Z (81 matches in the 16:00-16:59 sub-window alone), and the last 'No space left on device' record overall is 16:57:21Z.
  - `F23` ✔ Both exhaustion patterns persist past the rollback instant 15:12Z with unchanged message form — e.g. 'could not extend file "base/16402/2619": No space left on device' at 15:12:32Z, 15:12:45Z, 15:13:30Z, and pg_wal PANIC at 15:12:41Z — so the rollback did not clear or alter the failure mode.
  - `F31` ✔ ledger-db disk_used_pct rises monotonically and gradually from 94.76% at 14:00Z to 100.0% at ~14:56Z, reaching the 100% ceiling ~4-10 minutes BEFORE the 15:00Z error onset, then stays pinned at exactly 100.0% in every bucket from 15:00Z through 16:28Z — i.e. there is NO reclaim/step drop and NO recovery at any point in the window, including after the 15:12Z rollback.
  - `F32` ✔ The disk fill is a long, pre-existing, steady ramp — not stepwise and not aligned to any deployment: disk_used_pct climbs smoothly from 78.16% at 11:00Z to 100% at 15:00Z (~0.55%/6min, ~21.8 percentage points over 4 hours), i.e. the exhaustion was already ~9 hours in progress before the 14:46Z v6.0.0 deploy and the 15:10Z report. This establishes the fill as gradual/pre-existing rather than a deploy-induced step.
**Assumptions**
  - The new mandate path is exercised by production traffic at meaningful volume.
  - The observed failures are caused by this path rather than by shared-storage exhaustion.
**Rejected because:** Mechanism mismatch and timeline both fail: the error text is storage exhaustion, not a contract/schema failure — 179/179 payment-service ERROR logs are 'could not extend file: No space left on device' (F8) and ledger-db emits pg_wal xlogtemp PANICs (F9, F21). Zero MANDATE_HOLD/enum/unknown-entry-type/constraint matches exist in either service (F19, F20, F24). The disk was already on a ~9h ramp to 100% reaching the ceiling at ~14:56Z, only 10 min after the 14:46Z deploy, and was ~99% at deploy time, so a 14-min-old release cannot explain the fill (F31, F32). A v6.0.0-only regression also cannot explain settlement-service (53.56%) and api-gateway (16.43%) failing in the same bucket (F10), services that do not run the mandate flow. Rollback at 15:12Z left errors unchanged through 16:57Z (F22, F23).

### H3: Cross-service contract mismatch: ledger-db schema/enum rejects MANDATE_HOLD
ledger-db's entry-type enum/constraint does not accept MANDATE_HOLD (ledger-db owns the schema; no ledger-service exists to deploy), so mandate-flow ledger writes are rejected at the shared DB and payment-service's error rate hits 60%. The fault origin would be the ledger-db schema/config side, not payment-service code.

**Causal chain**
1. ledger-db owns the entry-type enum/constraint with no ledger-service in front of it [F15, F16]
2. v6.0.0 begins writing MANDATE_HOLD, which the constraint rejects [F1]
3. writes fail and surface as payment-service ledger write failures *(assumed)*

**Supporting evidence**
  - `F1` ✔ Across all services, only one deployment occurred in the entire incident window 13:00-16:59Z: payment-service v6.0.0 (dep-6203) deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes ['mandate create/approve endpoints','new ledger entry type MANDATE_HOLD'], status 'rolled_back' with rolled_back_at 2026-06-01T15:12:00Z. This matches the report's ~15-minutes-after-release onset.
  - `F15` ✔ There is NO ledger-service; the known_services list has no such name, so all ledger writes go directly to the shared ledger-db (postgres, owner platform, shared=true) and any ledger-side change would be a schema/config change on ledger-db, not a separate service release.
  - `F16` ✔ payment-service's dependencies are: ledger-db (kind db), bank-partner-api (external), kafka (queue-produce); api-gateway is the only caller of payment-service.
  - `F4` ✔ payment-service writes to ledger-db (kind 'db'), and ledger-db is flagged shared:true, used directly by order-service, portfolio-service, payment-service and settlement-service. kafka is also shared:true. This makes ledger-db a shared component through which a neighbour could affect payment-service.
**Contradicting evidence**
  - `F8` ✔ The overwhelming payment-service error signature is a ledger write failure: 179/179 ERROR logs match 'ledger write failed: ERROR: could not extend file: No space left on device'.
  - `F9` ✔ The ledger DB (ledger-db; there is no 'ledger-service') emits 143 ERROR logs in the same window, with two patterns: 'PANIC: could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (72x, first 15:01:01Z) and 'could not extend file "base/<n>/<n>": No space left on device' (71x, first 15:01:39Z).
  - `F12` ✔ The observed failure mode is storage/disk exhaustion on the ledger database (No space left on device, incl. pg_wal WAL temp files), not a schema/entry-type error — no 'unknown ledger entry type' or mandate-flow error appears among the 179 payment-service ERROR logs.
  - `F2` ✔ No configuration or feature-flag changes were recorded for any service in 2026-06-01T13:00:00Z-16:59:00Z (and none in the wider 2026-05-25 to 06-01 window). So the ledger write failures cannot be attributed to a config/flag change.
  - `F14` ✔ No config or feature-flag changes were recorded for any service in 14:30-16:59; get_config_changes with scope 'all services' returned count 0.
  - `F3` ✔ There was no ledger-side (ledger-db) or other-service deployment in the window, and the only deployment in the last 7 days present in the change list is payment-service v6.0.0 — so no ledger-side change is present in the change list as a competing candidate.
  - `F18` ✔ No ledger-side release is visible: settlement-service (the other payments-owner service touching ledger-db) had 0 deployments in 13:00-16:59, and ledger-db had 0 config changes in that period. Past ledger incidents are separate causes (HIST-114 disk full from WAL, HIST-111 unindexed report queries) — leads only, not evidence.
  - `F19` ✔ In the full 15:00Z-16:59Z window there are ZERO log matches for any of the mandate/schema signature terms in payment-service: 'MANDATE_HOLD' = 0 matches (result untruncated, total_matched 0).
  - `F20` ✔ ledger-db logs for 15:00Z-16:59Z contain ZERO matches for 'MANDATE_HOLD', 'enum', 'unknown entry type', and 'check constraint' — all four queries returned total_matched 0, untruncated. The mandated new-ledger-entry-type / enum / constraint failure modes are absent.
**Assumptions**
  - ledger-db has a strict enum/CHECK constraint rejecting unknown entry types (never verified).
  - A ledger-db schema change occurred pre-window and is invisible to the change list.
**Rejected because:** Mechanism does not match: the errors are storage-layer ('No space left on device', pg_wal xlogtemp / base/ relation extension) (F8, F9, F12, F21), which a constraint violation cannot produce, and zero enum/constraint/'unknown entry type' matches exist in ledger-db or payment-service logs (F19, F20). No config/flag change was recorded for any service in the incident or 7-day window (F2, F14), no ledger-side deployment is in the change list (F3, F18), and a constraint rejection would not take settlement-service and api-gateway down in the same 15:00Z bucket (F10).

## Facts (verified observations)
- payment-service error_rate was ~0.2% through 14:58Z then jumped to 60.65% in the 15:00Z bucket, sustained ~58-66% through 16:00Z; first ERROR log at 15:00:26Z. [F7]
- 179/179 payment-service ERROR logs read 'ledger write failed: ERROR: could not extend file: No space left on device'. [F8, F12]
- ledger-db emitted 292 'No space left on device' errors in 15:00Z-16:59Z (first 15:01:01Z, last 16:57:21Z): pg_wal/xlogtemp PANICs (153x) and 'could not extend file base/<n>/<n>' (139x). [F9, F21]
- Blast radius is shared-DB-shaped: settlement-service rose to 53.56% and api-gateway to 16.43% in the same 15:00Z bucket, both sustained. [F10]
- ledger-db is shared (shared=true) with four direct writers: order-service, portfolio-service, payment-service, settlement-service. [F4, F17, F25]
- ledger-db disk_used_pct rises monotonically from 94.76% at 14:00Z to 100.0% at ~14:56Z, stays pinned at exactly 100.0% in every bucket 15:00Z-16:28Z, with no reclaim/step drop and no recovery, including after the 15:12Z rollback. [F31, F33]
- The disk fill is a long, steady, pre-existing ramp: 78.16% at 11:00Z to 100% at ~15:00Z (~21.8 percentage points over 4 hours), not stepwise and not aligned to any deployment. [F32]
- Only one deployment occurred across all services in the incident window: payment-service v6.0.0 (dep-6203) at 2026-06-01T14:46:00Z, status rolled_back at 15:12:00Z; no ledger-side or other-service deployment exists. [F1, F3, F13]
- No configuration or feature-flag changes were recorded for any service in 13:00-16:59Z (or in the wider 2026-05-25 to 06-01 window). [F2, F14, F28]
- Disk-exhaustion errors continue past the 15:12Z rollback with unchanged message form through 16:57:21Z (81 pg_wal PANIC matches in the 16:00-16:59 sub-window alone). [F22, F23]
- Zero log matches for 'MANDATE_HOLD' in payment-service (F19) and zero matches for 'MANDATE_HOLD', 'enum', 'unknown entry type' or 'check constraint' in ledger-db across the full late window (F20); untruncated queries. [F19, F20]
- ledger-db connections_active stays in a narrow 60-66 range (mean 62.27) with no surge at onset; cpu_pct flat ~29-31% and slow_queries_per_min flat ~1/min. [F34, F35]
- replication_lag_ms on ledger-db rises steadily across the window and does not recover after rollback: ~3025ms at 14:00Z to 4027ms at 15:12Z to ~4836ms by 16:28Z. [F36]
- No writer deployment or config change occurred near the 15:00Z onset (order-service had zero deployments in the 2026-05-25 to 15:00Z lookback). [F27, F28]
- History contains ledger-db storage incidents with differing mechanisms: HIST-114 'ledger-db disk full from retained WAL (inactive replication slot)', HIST-110 IOPS burst-credit throttling, HIST-111 unindexed reporting scans. [F5, F29]
- No per-service write/QPS or transaction-rate metric is obtainable from the accessible toolset, so the H1-vs-H4 discriminator (writer throughput before vs after onset) could not be measured. [F26]

## Assumptions (not verified)
- Assumption: the precise growth mechanism (retained WAL from an inactive replication slot vs base-table/relation growth vs autovacuum/WAL retention) is not pinned — no WAL-size, wal_keep, or archive-backlog metric exists; F36's rising replication_lag is suggestive of WAL retention but is not direct proof.
- Assumption: v6.0.0's extra mandate writes did not materially cause the fill — the disk was already ~99% at its 14:46Z deploy and the ramp predates it by ~9 hours, so exhaustion was imminent regardless; no positive exclusion of a final-fraction contribution was possible.
- Assumption: no log content after ~15:13Z was individually inspected, but totals and last-timestamps (16:57:21Z) plus the flat 100% disk curve confirm late-window persistence of the same failure mode.
- Assumption: the reported onset framing ('~15 minutes after release', '~15:25 rollout') is inaccurate; verified data place onset in the 15:00Z bucket, before/around the 14:46Z deploy — the reported timeline should be corrected.
- Assumption: ledger-db's disk exhaustion degrades all four of its direct writers; only payment-service, settlement-service and api-gateway error rates were observed directly, so the durability of impact on order-service/portfolio-service is unconfirmed.

## Timeline
- `2026-06-01T11:00:00Z` ledger-db disk_used_pct at 78.16%, beginning of the smooth monotonic fill ramp. [F32]
- `2026-06-01T14:00:00Z` ledger-db disk_used_pct 94.76%; replication_lag_ms ~3025ms. [F31, F36]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (dep-6203) deployed by p.nair ('New UPI mandate flow', new MANDATE_HOLD entry type); the only deployment in the entire window. [F1, F3, F13]
- `2026-06-01T14:56:00Z` ledger-db disk_used_pct reaches the 100.0% ceiling, ~4-10 minutes before the error onset. [F31]
- `2026-06-01T15:00:00Z` Onset: payment-service error_rate jumps to 60.65%; settlement-service to 53.56%; api-gateway to 16.43% in the same bucket. First payment-service ERROR log at 15:00:26Z. [F7, F10]
- `2026-06-01T15:01:01Z` First ledger-db pg_wal/xlogtemp PANIC 'No space left on device'; relation-extension errors follow at 15:01:39Z. [F9, F21]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rolled back (rolled_back_at); error signatures and 100% disk usage unchanged immediately after. [F1, F23, F33, F36]
- `2026-06-01T16:28:00Z` disk_used_pct still pinned at exactly 100.0%; replication_lag_ms ~4836ms with no reclaim or recovery. [F31, F33, F36]
- `2026-06-01T16:57:21Z` Last observed 'No space left on device' record; exhaustion errors persisted the whole late window with no signature change. [F22]

## Recommended actions
- **[immediate]** Immediate: free space on the ledger-db volume — drop/recreate the inactive replication slot or run pg_replication_slot_advance / restart the standby, and remove non-critical pg_wal/xlogtemp and archive contents after verification — then confirm disk_used_pct drops below 100% and that service error rates recover. -- The failure mode is unambiguous storage exhaustion (pg_wal xlogtemp and relation-extension 'No space left on device', F8/F9/F21) with the disk pinned at 100% and no reclaim even after the 15:12Z rollback (F22/F23/F31/F33); reclaiming space is the only change that addresses the observed mechanism. HIST-114 'retained WAL until disk filled' is the closest precedent (F5/F29; F36's rising replication_lag is consistent with WAL retention). [F8, F9, F21, F22, F23, F31, F33, F36, F5, F29]
- **[immediate]** Immediate: do NOT re-deploy or further iterate on payment-service v6.0.0 as a remedy; keep it rolled back and record that the rollback did not resolve the incident. -- The rollback at 15:12Z left disk usage at 100% and error signatures unchanged through 16:57Z (F23/F22/F33), and services that never run the mandate flow (settlement-service, api-gateway) failed in the same 15:00Z bucket (F10), so the deploy is incidental. [F1, F10, F22, F23, F33]
- **[immediate]** Immediate: set up a disk-space alert and a WAL/archive-backlog + replication-slot-age monitor on ledger-db (currently available metrics include no WAL-size surface), and verify ledger-db's volume is actually sized for WAL plus base-table growth. -- The ~9-hour gradual fill (78.16% at 11:00Z to 100% by ~14:56Z, F32/F31) went unnoticed and page-level WAL metrics are absent, so the incident surfaced only as a hard multi-service outage (F7/F10). [F31, F32, F7, F10]
- **[short_term]** Short term: after reclaim, confirm recovery by verifying ledger-db disk_used_pct falls below the 100% ceiling and does not re-climb, and that payment-service, settlement-service and api-gateway error rates return to baseline; keep monitoring replication_lag_ms (currently ~4836ms) until it normalizes. -- No metric in the available window shows any reclaim or error recovery after the rollback (F31/F33/F36), so the efficacy of the rollback-then-reclaim sequence must be verified, not assumed. [F31, F33, F36, F22]
- **[short_term]** Short term: pin down the growth mechanism by adding and baselining WAL directory size, archive backlog, replication-slot age/active flag, and base-table/autovacuum growth for ledger-db; if a slot is holding WAL, add max_slot_wal_keep_size and slot-inactivity alerting. -- The exact mechanism (retained WAL vs base-table growth vs autovacuum) is unresolved and prevents a targeted permanent fix; F36's steadily rising replication_lag_ms is consistent with WAL retention but no size metric exists. [F36, F26]
- **[short_term]** Short term: instrument and alert on per-writer write/QPS and transaction rate into ledger-db so a future neighbour-driven load flood can be measured and separated from node-local growth. -- No per-writer write metric exists in the accessible toolset, which is exactly why H4 (neighbour flooding the shared DB) could not be formally excluded and H1 could not be positively confirmed for the no-spike claim (F26/F30/F34/F35). [F26, F30, F34, F35]
- **[long_term]** Long term: cap and recycle ledger-db storage capacity — enforce max_slot_wal_keep_size, add automatic WAL/archive pruning, autovacuum tuning, and partition/archive the ledger base tables — with scheduled disk-usage and slot-health reports, and pre-agreed isolation between the shared ledger-db and its four writers (order-service, portfolio-service, payment-service, settlement-service). -- The shared ledger-db is a single point of failure for four services (F4/F17/F25), the fill is a recurring storage pattern (HIST-114, HIST-110, HIST-111; F5/F29), and capacity exhaustion recurs because no automatic retention limit exists. [F4, F17, F25, F5, F29]
- **[long_term]** Long term: correct the incident's reported timeline and deploy/rollback process notes so that 'onset ~15 min after v6.0.0' is not carried forward as causal; document that onset in verified data precedes/matches the 14:46Z deploy and that rollback was not the remedy. -- The reporter's framing and the verified onset (15:00Z bucket, F7/F10) conflict with the 14:46Z deploy time (F1/F13), and the rollback had no effect (F22/F23); leaving this uncorrected invites repeating the same misdiagnosis. [F1, F13, F7, F10, F22, F23]

## Data gaps
- No WAL directory size, wal_keep, archive-backlog, or replication-slot-age metric exists for ledger-db (available_metrics are only connections_active, cpu_pct, disk_used_pct, replication_lag_ms, slow_queries_per_min), so the fill cannot be positively attributed to retained WAL versus base-table/autovacuum growth.
- No per-writer write/QPS or transaction-rate metric is exposed by any accessible tool; the H1-vs-H4 discriminator (writer throughput before vs after 15:00Z) was never measured, so neither H4 nor H1's no-writer-spike claim is positively confirmed.
- Log queries were truncated (payment-service 179 matched / 40 returned; ledger-db 292 matched / 40 returned), and individual log content after ~15:13Z was not inspected; late-window signature changes or brief recovery gaps cannot be excluded.
- order-service error_rate could not be queried and order-service/portfolio-service deployment lookbacks beyond order-service were not executed, so the full blast radius and any recent neighbour release are only partially checked (order-service lookback returned count 0).
- Post-window data stops at 16:28Z (metrics) / 16:57Z (logs); no data shows whether the service was ever restored or space reclaimed after the window, so the rollback-then-reclaim remedy's outcome is unverified.
- The payment-service-scoped config/flag change query was never executed (only the all-services scope returned 0), so a payment-service-scoped toggle/itemization cannot be fully excluded.

## What would change this conclusion
- A measured per-writer write/QPS or transaction-rate surge from order-service, portfolio-service or settlement-service beginning near 15:00Z would move the origin from H1 (node-local WAL/relation growth) to H4 (neighbour write-flood into the shared ledger-db).
- A WAL-size / archive-backlog / replication-slot metric showing retained WAL from an inactive slot would positively confirm H1's mechanism, whereas a metric showing base-table growth or a long-running report query would redirect the fix.
- Evidence that ledger-db disk_used_pct recovered (dropped below 100%) only after the 15:12Z rollback — or that errors cleared while disk stayed exhausted — would undercut the storage-exhaustion conclusion and revive a v6.0.0/contract cause.
- Any 'MANDATE_HOLD', 'unknown entry type', enum, or check-constraint failure appearing in payment-service or ledger-db logs (currently 0 matches across untruncated full-window queries) would revive H2/H3.
- A ledger-side schema/config change or deployment near onset (currently none in the change list) would introduce a competing config_change origin.

## Investigation stats
- latency 184.46s, LLM calls 29, tokens 212238 (in 173942 / out 38296)
- tool calls 46 (4 failed, 0 partial, 0 recovered by retry), findings 36/36 verified
