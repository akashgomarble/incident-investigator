# Incident investigation report: INC-012
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Ledger-db's shared Postgres volume filled to capacity from WAL retained by an inactive replication slot (analytics_cdc, 78423 MB retained), so writes began failing with ENOSPC and payment-service's ledger writes failed as a verbatim passthrough of that storage error; payment-service v6.0.0 was coincidental timing, not the cause. Confidence 0.82 (high), supported by the disk warning at 14:15:45Z leading both the 14:46:50Z rollout and the first payment error at 15:00:26Z by ~45 minutes. The failure persisted at ~60% error rate for ~1h45m after the 15:12Z rollback with zero config/deploy changes in the window, and the error is entry-type-agnostic (flat p99 latency = fail-fast), which contradicts the MANDATE_HOLD schema-mismatch suspicion. Excellent match to precedent HIST-114 (inactive replication slot retaining WAL until disk filled).

## Probable root cause
**ledger-db disk exhaustion from retained WAL (inactive replication slot) — storage-layer root cause independent of v6.0.0** -- resource_exhaustion in `ledger-db`, triggered by `null` (confidence 0.82, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: ledger-db disk exhaustion from retained WAL (inactive replication slot) — storage-layer root cause independent of v6.0.0 | resource_exhaustion | ledger-db | null | 0.82 (high) | active |
| 2 | H4: Verdict: ledger-db volume at capacity / WAL backlog exhausted — cause-side resource exhaustion with v6.0.0 as coincidental timing | resource_exhaustion | ledger-db | null | 0.80 (high) | active |
| 3 | H3: Shared ledger-db saturated by a neighbouring caller (order/portfolio/settlement) writing excessive volume | capacity_saturation | order-service | portfolio-service | settlement-service (shared ledger-db caller) | null | 0.20 (low) | active |
| 4 | H1: Reporter's suspicion: v6.0.0 MANDATE_HOLD ledger entry type rejected by ledger-db (schema/validation mismatch) | bad_deployment | payment-service | dep-6203 | 0.05 (low) | rejected |

### H2: ledger-db disk exhaustion from retained WAL (inactive replication slot) — storage-layer root cause independent of v6.0.0
ledger-db's shared Postgres disk filled because an inactive replication slot (analytics_cdc) retained WAL (78423 MB) until the volume hit 100% capacity; once writes to pg_wal/pg data directory failed with ENOSPC, every writer — including payment-service's ledger writes — began failing with 'No space left on device'. This matches precedent HIST-114 exactly.

**Causal chain**
1. analytics_cdc replication slot goes inactive and retains 78423 MB of WAL without being consumed [F11]
2. ledger-db disk climbs 95% (14:15:45Z) -> 98% (14:48:20Z) -> full; first PANIC 'could not write to pg_wal/xlogtemp' at 15:01:01Z [F11, F15]
3. payment-service's ledger writes hit the same out-of-space condition and fail fast [F8, F10, F12]
4. payment-service error rate jumps to ~60% and stays there even after the v6.0.0 rollback, because the DB is still full [F9, F14, F8]

**Supporting evidence**
  - `F8` ✔ First payment-service error is 2026-06-01T15:00:26Z; the ~15:25Z claim is wrong and the 15:10Z report is also ~10 min late. All 354 ERRORs match a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device', spanning 15:00:26Z to 16:57:54Z (gap in coverage between 15:00 and first_match).
  - `F11` ✔ ledger-db (the ledger writer's datastore — no 'ledger-service' exists) shows the CAUSE-side first: disk warning 'disk usage 95% on /var/lib/postgresql/data' at 14:15:45Z, 98% at 14:48:20Z, then first hard error at 15:01:01Z 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device'. 292 ERRORs total; top patterns include 153 WAL PANICs and 139 'could not extend file "base/16402/2619"'. Also 'replication slot analytics_cdc is inactive; retaining 78423 MB of WAL' at 14:16:25Z.
  - `F12` ✔ Downstream-side confirmed: the payment-service error string is the verbatim PostgreSQL/DB error surfaced by the ledger writer, identical to ledger-db's own error class ('No space left on device'); DB disk saturation at ledger-db began ~45 min before payment errors and preceded them. payment-service has no 'restarts' or DB-pool metric anomaly needed — its error is a passthrough of the writer's storage failure.
  - `F15` ✔ Change ordering relative to onset: v6.0.0 rollout completed 14:46:50Z, first payment error 15:00:26Z (+13m36s after deploy), rollback 15:12:00Z. The ledger-db disk warning pre-dates the deploy by ~31 min (14:15:45Z, 95%), so the resource-exhaustion signal leads the deployment, not the reverse.
  - `F18` ✔ Precedent check contradicts the described mechanism: HIST-114 (2025-04-18) has the identical symptom set ('no space left on device', 'write failures') and root cause 'Inactive replication slot retained WAL until disk filled', exactly matching the current ledger-db evidence — whereas no precedent (HIST-111/109/108/110/102) cites a new ledger entry type or mandate-flow change as a cause.
  - `F14` ✔ Config/flag changes for 14:00-15:30Z returned count 0, and deployments for the post-onset window 15:30-16:59Z also returned count 0 — so nothing was changed between the 15:12Z rollback and 16:59Z, i.e. the error rate stayed at ~60% for ~1h45m despite the rollback.
  - `F10` ✔ payment-service latency_p99_ms is flat across the whole window (baseline ~315-330ms, incident ~308-331ms; min 292.53, max 348.32) — NO latency anomaly. The failure is not a timeout/slowness symptom; requests fail fast.
  - `F17` ✔ ledger-db is a shared Postgres with four caller services: order-service, portfolio-service, payment-service, settlement-service all hold 'db' edges to it (api-gateway -> order-service/portfolio-service/payment-service HTTP). Any of these neighbours can saturate the shared store and hurt payment-service via it.
**Contradicting evidence**
  - `F1` ✔ Across all services in the incident window 2026-06-01T14:00–15:30Z, exactly one deployment occurred: payment-service v6.0.0 (id dep-6203), deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes 'mandate create/approve endpoints' and 'new ledger entry type MANDATE_HOLD', status 'rolled_back' with rolled_back_at 2026-06-01T15:12:00Z.
**Assumptions**
  - the inactive slot's retained WAL is the dominant consumer of disk (vs. some other growth source)
  - no cleanup/replication-slot drop occurred during the incident to explain the sustained failure

### H4: Verdict: ledger-db volume at capacity / WAL backlog exhausted — cause-side resource exhaustion with v6.0.0 as coincidental timing
Root origin is ledger-db storage exhaustion. The earliest causal signal (95% disk, 14:15:45Z) leads the deploy by ~31 min and the retained-WAL replication-slot mechanism exactly matches precedent HIST-114. v6.0.0 rolled out 14:46:50Z and first payment error was 15:00:26Z, but the rollback at 15:12Z produced no recovery for ~1h45m, and the error string is a verbatim DB passthrough — all consistent with a DB-side storage fault, not a code fault.

**Causal chain**
1. Inactive replication slot retained WAL and/or the volume reached 100% capacity [F11, F18]
2. ledger-db began emitting storage errors (WAL PANIC, could not extend file) at 15:01:01Z [F11]
3. payment-service ledger writes fail with the identical DB error string [F8, F12]
4. payment error rate stays ~60% after rollback confirming the fault is DB-side [F14]

**Supporting evidence**
  - `F11` ✔ ledger-db (the ledger writer's datastore — no 'ledger-service' exists) shows the CAUSE-side first: disk warning 'disk usage 95% on /var/lib/postgresql/data' at 14:15:45Z, 98% at 14:48:20Z, then first hard error at 15:01:01Z 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device'. 292 ERRORs total; top patterns include 153 WAL PANICs and 139 'could not extend file "base/16402/2619"'. Also 'replication slot analytics_cdc is inactive; retaining 78423 MB of WAL' at 14:16:25Z.
  - `F12` ✔ Downstream-side confirmed: the payment-service error string is the verbatim PostgreSQL/DB error surfaced by the ledger writer, identical to ledger-db's own error class ('No space left on device'); DB disk saturation at ledger-db began ~45 min before payment errors and preceded them. payment-service has no 'restarts' or DB-pool metric anomaly needed — its error is a passthrough of the writer's storage failure.
  - `F15` ✔ Change ordering relative to onset: v6.0.0 rollout completed 14:46:50Z, first payment error 15:00:26Z (+13m36s after deploy), rollback 15:12:00Z. The ledger-db disk warning pre-dates the deploy by ~31 min (14:15:45Z, 95%), so the resource-exhaustion signal leads the deployment, not the reverse.
  - `F14` ✔ Config/flag changes for 14:00-15:30Z returned count 0, and deployments for the post-onset window 15:30-16:59Z also returned count 0 — so nothing was changed between the 15:12Z rollback and 16:59Z, i.e. the error rate stayed at ~60% for ~1h45m despite the rollback.
  - `F18` ✔ Precedent check contradicts the described mechanism: HIST-114 (2025-04-18) has the identical symptom set ('no space left on device', 'write failures') and root cause 'Inactive replication slot retained WAL until disk filled', exactly matching the current ledger-db evidence — whereas no precedent (HIST-111/109/108/110/102) cites a new ledger entry type or mandate-flow change as a cause.
  - `F10` ✔ payment-service latency_p99_ms is flat across the whole window (baseline ~315-330ms, incident ~308-331ms; min 292.53, max 348.32) — NO latency anomaly. The failure is not a timeout/slowness symptom; requests fail fast.
  - `F8` ✔ First payment-service error is 2026-06-01T15:00:26Z; the ~15:25Z claim is wrong and the 15:10Z report is also ~10 min late. All 354 ERRORs match a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device', spanning 15:00:26Z to 16:57:54Z (gap in coverage between 15:00 and first_match).
**Contradicting evidence**
  - `F1` ✔ Across all services in the incident window 2026-06-01T14:00–15:30Z, exactly one deployment occurred: payment-service v6.0.0 (id dep-6203), deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes 'mandate create/approve endpoints' and 'new ledger entry type MANDATE_HOLD', status 'rolled_back' with rolled_back_at 2026-06-01T15:12:00Z.
**Assumptions**
  - the inactive slot WAL retention is the primary disk consumer
  - no separate out-of-band cleanup/remediation completed before 16:59Z

### H3: Shared ledger-db saturated by a neighbouring caller (order/portfolio/settlement) writing excessive volume
One of the other shared-ledger-db callers (order-service, portfolio-service, settlement-service) or a background process drove abnormal write/WAL volume into the shared Postgres, exhausting disk and thereby breaking payment-service's ledger writes. v6.0.0 is a downstream victim, not the origin.

**Causal chain**
1. A neighbour caller (or background process) increases write/WAL volume to shared ledger-db *(assumed)*
2. ledger-db disk fills (95% 14:15:45Z -> full 15:01:01Z) [F11, F15]
3. payment-service ledger writes fail with ENOSPC [F8, F12]

**Supporting evidence**
  - `F5` ✔ ledger-db is a shared Postgres datastore with four other callers besides payment-service: order-service, portfolio-service, settlement-service (all 'db' edges), and it is reached via api-gateway -> order-service / portfolio-service / payment-service HTTP edges; so neighboring services can affect payment-service through the shared ledger-db.
  - `F17` ✔ ledger-db is a shared Postgres with four caller services: order-service, portfolio-service, payment-service, settlement-service all hold 'db' edges to it (api-gateway -> order-service/portfolio-service/payment-service HTTP). Any of these neighbours can saturate the shared store and hurt payment-service via it.
  - `F11` ✔ ledger-db (the ledger writer's datastore — no 'ledger-service' exists) shows the CAUSE-side first: disk warning 'disk usage 95% on /var/lib/postgresql/data' at 14:15:45Z, 98% at 14:48:20Z, then first hard error at 15:01:01Z 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device'. 292 ERRORs total; top patterns include 153 WAL PANICs and 139 'could not extend file "base/16402/2619"'. Also 'replication slot analytics_cdc is inactive; retaining 78423 MB of WAL' at 14:16:25Z.
**Contradicting evidence**
  - `F1` ✔ Across all services in the incident window 2026-06-01T14:00–15:30Z, exactly one deployment occurred: payment-service v6.0.0 (id dep-6203), deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes 'mandate create/approve endpoints' and 'new ledger entry type MANDATE_HOLD', status 'rolled_back' with rolled_back_at 2026-06-01T15:12:00Z.
  - `F2` ✔ The deployment search over the wider pre-window 2026-06-01T08:00:00Z–16:59:00Z still returned only the single payment-service v6.0.0 deployment; no ledger-service, no shared-component, and no other service release exists in scope.
  - `F13` ✔ Independent change re-query for 14:30-15:30Z again returns exactly one change: payment-service v6.0.0 (dep-6203), deployed 2026-06-01T14:46:00Z, 'new ledger entry type MANDATE_HOLD', status 'rolled_back' with rolled_back_at 2026-06-01T15:12:00Z. No downstream/shared-service deployment exists in the window.
  - `F6` ✔ Historical incident search returned 6 precedents; the two closest to 'ledger write failure' are HIST-114 (2025-04-18, ledger-db disk full from retained WAL, symptom 'write failures', root cause 'Inactive replication slot retained WAL until disk filled') and HIST-111 (2025-05-22, 'New report query without index did sequential scans'). None of the precedents cite a new ledger entry type or MANDATE_HOLD as a cause.
  - `F18` ✔ Precedent check contradicts the described mechanism: HIST-114 (2025-04-18) has the identical symptom set ('no space left on device', 'write failures') and root cause 'Inactive replication slot retained WAL until disk filled', exactly matching the current ledger-db evidence — whereas no precedent (HIST-111/109/108/110/102) cites a new ledger entry type or mandate-flow change as a cause.
**Assumptions**
  - WAL/disk growth in 14:15-15:01Z is attributable to a caller rather than the inactive-slot WAL retention already observed
  - per-caller write-volume metrics would show a spike
  - the inactive slot is not itself the dominant growth source

### H1: Reporter's suspicion: v6.0.0 MANDATE_HOLD ledger entry type rejected by ledger-db (schema/validation mismatch)
payment-service v6.0.0 introduced a new ledger entry type (MANDATE_HOLD); ledger-db's schema/validation does not support it, so ledger writes fail and payment-service surfaces the rejection as 'ledger write failed'.

**Causal chain**
1. payment-service v6.0.0 (dep-6203) deployed 14:46Z introduces MANDATE_HOLD entry type [F1, F13]
2. ledger-db schema/validation does not accept MANDATE_HOLD (no migration/support change deployed) *(assumed)*
3. ledger writes for the new entry type are rejected, producing a ledger-write error in payment-service *(assumed)*
4. payment-service error rate jumps to ~60% [F9]

**Supporting evidence**
  - `F1` ✔ Across all services in the incident window 2026-06-01T14:00–15:30Z, exactly one deployment occurred: payment-service v6.0.0 (id dep-6203), deployed 2026-06-01T14:46:00Z by p.nair, summary 'New UPI mandate flow', changes 'mandate create/approve endpoints' and 'new ledger entry type MANDATE_HOLD', status 'rolled_back' with rolled_back_at 2026-06-01T15:12:00Z.
  - `F13` ✔ Independent change re-query for 14:30-15:30Z again returns exactly one change: payment-service v6.0.0 (dep-6203), deployed 2026-06-01T14:46:00Z, 'new ledger entry type MANDATE_HOLD', status 'rolled_back' with rolled_back_at 2026-06-01T15:12:00Z. No downstream/shared-service deployment exists in the window.
  - `F16` ✔ Schema/support-change angle: no migration, config, feature-flag or ledger-side release was recorded anywhere in the incident window, so the new ledger entry type MANDATE_HOLD has no corresponding schema/config/support change in the inventory — the only artifact accompanying it is the payment-service code deploy itself. Whether MANDATE_HOLD is accepted by ledger-db cannot be tested, since the DB's failure mode (out-of-disk WAL) is entry-type-agnostic.
**Contradicting evidence**
  - `F8` ✔ First payment-service error is 2026-06-01T15:00:26Z; the ~15:25Z claim is wrong and the 15:10Z report is also ~10 min late. All 354 ERRORs match a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device', spanning 15:00:26Z to 16:57:54Z (gap in coverage between 15:00 and first_match).
  - `F11` ✔ ledger-db (the ledger writer's datastore — no 'ledger-service' exists) shows the CAUSE-side first: disk warning 'disk usage 95% on /var/lib/postgresql/data' at 14:15:45Z, 98% at 14:48:20Z, then first hard error at 15:01:01Z 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device'. 292 ERRORs total; top patterns include 153 WAL PANICs and 139 'could not extend file "base/16402/2619"'. Also 'replication slot analytics_cdc is inactive; retaining 78423 MB of WAL' at 14:16:25Z.
  - `F12` ✔ Downstream-side confirmed: the payment-service error string is the verbatim PostgreSQL/DB error surfaced by the ledger writer, identical to ledger-db's own error class ('No space left on device'); DB disk saturation at ledger-db began ~45 min before payment errors and preceded them. payment-service has no 'restarts' or DB-pool metric anomaly needed — its error is a passthrough of the writer's storage failure.
  - `F15` ✔ Change ordering relative to onset: v6.0.0 rollout completed 14:46:50Z, first payment error 15:00:26Z (+13m36s after deploy), rollback 15:12:00Z. The ledger-db disk warning pre-dates the deploy by ~31 min (14:15:45Z, 95%), so the resource-exhaustion signal leads the deployment, not the reverse.
  - `F18` ✔ Precedent check contradicts the described mechanism: HIST-114 (2025-04-18) has the identical symptom set ('no space left on device', 'write failures') and root cause 'Inactive replication slot retained WAL until disk filled', exactly matching the current ledger-db evidence — whereas no precedent (HIST-111/109/108/110/102) cites a new ledger entry type or mandate-flow change as a cause.
  - `F14` ✔ Config/flag changes for 14:00-15:30Z returned count 0, and deployments for the post-onset window 15:30-16:59Z also returned count 0 — so nothing was changed between the 15:12Z rollback and 16:59Z, i.e. the error rate stayed at ~60% for ~1h45m despite the rollback.
**Assumptions**
  - ledger-db enforces a schema/validation that would reject MANDATE_HOLD
  - the new entry type is actually used by the failing request path
  - the failure is entry-type-specific
**Rejected because:** The observed error signature is the verbatim PostgreSQL storage-layer error 'could not extend file: No space left on device' (F8, F12), not a validation/constraint/schema error. The new entry type cannot be tested as the cause because the DB failure mode is entry-type-agnostic (F16). The ledger-db disk warning started 31 min BEFORE the deploy (F15, F11) and the error rate stayed at ~60% for ~1h45m after the rollback (F14) — a code-rejection mechanism would have cleared on rollback. No precedent cites an entry-type cause (F18).

## Facts (verified observations)
- ledger-db showed the cause-side progression first: disk usage 95% at 14:15:45Z, 98% at 14:48:20Z, then the first hard error at 15:01:01Z 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device'; 292 ERRORs total including 153 WAL PANICs and 139 'could not extend file "base/16402/2619"'. [F11]
- ledger-db logged 'replication slot analytics_cdc is inactive; retaining 78423 MB of WAL' at 14:16:25Z. [F11]
- All 354 payment-service ERRORs match a single pattern: 'ledger write failed: ERROR: could not extend file: No space left on device', spanning 15:00:26Z to 16:57:54Z — a verbatim PostgreSQL storage-layer error, identical in class to ledger-db's own error. [F8]
- Change ordering: payment-service v6.0.0 rollout completed 14:46:50Z, first payment error 15:00:26Z (+13m36s after deploy), rollback at 15:12:00Z; the ledger-db disk warning (14:15:45Z) pre-dates the deploy by ~31 minutes. [F15, F7]
- payment-service error_rate jumped from a stable ~0.19-0.29% baseline (14:30-14:54Z) to avg 30.41% / max 60.8% in the 14:58-15:02Z bucket, then held ~60-65% through 16:58Z. [F9]
- payment-service latency_p99_ms is flat across the whole window (baseline ~315-330ms, incident ~308-331ms) — no latency anomaly, indicating fail-fast failures rather than timeouts or slowness. [F10]
- Across the incident window and the widened 08:00-16:59Z window, exactly one deployment occurred (payment-service v6.0.0, dep-6203, rolled back at 15:12Z); no ledger-service, shared-component, or other service release exists in scope. [F1, F2, F13]
- Config-change queries for 14:00-15:30Z and the widened 08:00-16:59Z window returned zero changes, and deployments for the post-onset window 15:30-16:59Z also returned zero — nothing changed between the 15:12Z rollback and 16:59Z, yet the error rate stayed at ~60% for ~1h45m. [F3, F14]
- Precedent HIST-114 (2025-04-18) has the identical symptom set ('no space left on device', 'write failures') and root cause 'Inactive replication slot retained WAL until disk filled', matching the current ledger-db evidence; no precedent cites a new ledger entry type or mandate-flow change as a cause. [F18]
- ledger-db is a shared Postgres datastore with four callers: order-service, portfolio-service, payment-service and settlement-service all hold 'db' edges to it, so neighbouring services can affect payment-service through the shared store. [F5, F17, F4]

## Assumptions (not verified)
- The inactive analytics_cdc slot's retained WAL (78423 MB) is the dominant consumer of disk, as opposed to some other growth source; this is inferred from the retained-WAL size message rather than a measured attribution.
- No out-of-band cleanup or replication-slot drop occurred during the incident that would explain the sustained failure differently than continuous disk saturation.
- The disk_used_pct metric trend (not queried before budget exhaustion) corroborates the 95%/98%/100% log progression; the conclusion currently rests on the log-observed disk progression.
- The single sampled error pattern (ledger write failed / ENOSPC) is representative of the full incident; the 15:13-16:57Z window was not sampled for a second, evolving error signature.
- Nothing was changed between the 15:12Z rollback and 16:59Z (consistent with empty change queries for 15:30-16:59Z), so the sustained ~60% error rate reflects the unremediated DB condition.
- (demoted from facts: no verified observation backs it) No migration, config, feature-flag or ledger-side release was recorded in the incident window, so the new MANDATE_HOLD ledger entry type has no corresponding schema/config/support change; whether MANDATE_HOLD is accepted by ledger-db cannot be tested because the DB's failure mode (out-of-disk WAL) is entry-type-agnostic.

## Timeline
- `2025-04-18T00:00:00Z` Precedent HIST-114: identical symptom set ('no space left on device', 'write failures') with root cause 'Inactive replication slot retained WAL until disk filled'. [F18, F6]
- `2026-06-01T14:15:45Z` ledger-db logs 'disk usage 95% on /var/lib/postgresql/data' — the earliest cause-side signal, ~31 min before the deploy and ~45 min before the first payment error. [F11, F15]
- `2026-06-01T14:16:25Z` ledger-db logs 'replication slot analytics_cdc is inactive; retaining 78423 MB of WAL'. [F11]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (dep-6203, 'New UPI mandate flow', new ledger entry type MANDATE_HOLD) deployed by p.nair — the only deployment in the window. [F1, F13]
- `2026-06-01T14:46:50Z` v6.0.0 rollout completes: 'deployment dep-6203 (v6.0.0) rollout complete, all pods ready'. [F7]
- `2026-06-01T14:48:20Z` ledger-db disk usage reaches 98%. [F11]
- `2026-06-01T14:58:00Z` payment-service error_rate bucket jumps from ~0.19-0.29% baseline to avg 30.41% / max 60.8%. [F9]
- `2026-06-01T15:00:26Z` First payment-service ERROR: 'ledger write failed: ERROR: could not extend file: No space left on device'. [F8, F15]
- `2026-06-01T15:01:01Z` ledger-db first hard error: 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device'; volume at 100% capacity. [F11, F12]
- `2026-06-01T15:10:00Z` Incident INC-012 reported by on-call SRE; payment-service error rate 60%, ledger write failures. [F8]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rolled back (rolled_back_at 15:12:00Z). [F1, F13]
- `2026-06-01T15:30:00Z` No config/flag changes for 14:00-15:30Z and no deployments for 15:30-16:59Z; error rate stays ~60% for ~1h45m after rollback. [F14]
- `2026-06-01T16:57:54Z` Last observed payment-service error in the sampled set, still the same ENOSPC passthrough pattern; error rate held ~60-65% through 16:58Z. [F8, F9]

## Recommended actions
- **[immediate]** Drop or reactivate the inactive replication slot analytics_cdc on ledger-db (e.g. pg_drop_replication_slot('analytics_cdc') after confirming the consumer is truly retired, or restart the consumer so the slot is no longer inactive) to release the 78423 MB of retained WAL and restore free space on /var/lib/postgresql/data. -- This is the identified root cause: an inactive replication slot retained 78423 MB of WAL until the volume hit 100% capacity, after which all writers including payment-service failed with ENOSPC. [F11, F18]
- **[immediate]** Confirm recovery by verifying ledger-db disk usage drops below the warning threshold and that 'could not extend file' / pg_wal ENOSPC PANICs stop, then confirm payment-service error_rate returns to its ~0.19-0.29% baseline (do not expect recovery from the rollback alone). -- The 15:12Z rollback produced no recovery for ~1h45m, and payment-service's failures are a passthrough of ledger-db storage failure, so recovery must be verified at the DB, not at payment-service. [F14, F12, F8, F9]
- **[short_term]** Add an automated alert/guardrail on ledger-db replication slots: page on 'inactive slot retaining WAL' and on disk_used_pct breaching a threshold (e.g. 80% warn / 90% page), and monitor replication_lag_ms and disk_used_pct as first-class signals for the shared ledger-db. -- The cause-side disk warning at 14:15:45Z preceded payment impact by ~45 minutes but did not drive timely action; disk_used_pct and replication_lag_ms exist as metrics but were never queried, so a metric-level guardrail would close the detection gap. [F11, F15]
- **[short_term]** Run an explicit post-rollout attribution: query ledger-db disk_used_pct, replication_lag_ms and connections_active, plus per-caller (order-service, portfolio-service, settlement-service, payment-service) write/WAL volume for 14:00-16:59Z, to confirm the inactive slot is the dominant consumer rather than a neighbouring caller. -- This is the discriminator between the top hypothesis (slot-retained WAL) and H3 (a neighbouring caller saturating the shared ledger-db); H3 remains structurally unevidenced but shares the same resource-exhaustion origin. [F11, F5, F17]
- **[long_term]** Add capacity/size management for WAL: enforce max_slot_wal_keep_size on the Postgres instance and include replication-slot retention in change review so an inactive consumer cannot silently consume the volume again. -- Precedent HIST-114 has the same root cause in 2025-04-18, so this failure mode has recurred; a durable guard is needed, not just cleanup. [F18, F6]
- **[long_term]** Decouple ledger-db disk headroom from application releases: alarm on shared-datastore capacity independently of deploys and document that ENOSPC passthrough errors on payment-service should route to the ledger-db path first (rollback of payment-service is not the mitigation). -- v6.0.0 was coincidental and rolling it back did not help (F14); runbooks should redirect responders to the shared storage layer when the error is a DB passthrough (F8, F12), while MANDATE_HOLD support itself is handled as a separate validation item (F16). [F14, F8, F12, F16]

## Data gaps
- No schema/migration change feed was available; an out-of-band ledger-db schema migration would be invisible, so a MANDATE_HOLD-support mismatch cannot be fully excluded (though no such change was scheduled in the window).
- Deployment records carry no schema/migration sub-records, so it is unknown whether v6.0.0 included or depended on a ledger-db migration, and the deploy-vs-migration ordering is unverified.
- Config-change results were empty but expose no per-flag breakdown, so I cannot distinguish 'no flags changed' from 'flag telemetry not collected'.
- Budget was exhausted before ledger-db/kafka component metrics were queried (available_metrics then values), leaving shared-component health at onset unconfirmed.
- settlement-service log query returned 0 matched entries and its error_rate metric was never executed, so blast radius beyond payment-service and ledger-db is unverified.
- ledger-db has no error_rate metric; disk_used_pct and replication_lag_ms were never queried, so no metric-level confirmation of the disk trend exists — the conclusion rests on log-observed 95%/98%/100%.
- payment-service log result was truncated (40 of 354 ERRORs) and 15:13-16:57Z was not sampled for new error signatures, so a second/evolving signature cannot be ruled out.
- Deployment/rollout events for services other than payment-service were not checked.
- The specific trigger that drove disk growth between 14:15:45Z and 15:01:01Z (which caller or process, if any, versus the slot retention alone) is not pinned by the change inventory.
- Whether the inactive-slot retained WAL is the dominant disk consumer is inferred from its 78423 MB size rather than measured attribution.

## What would change this conclusion
- A measured disk-usage attribution showing the inactive analytics_cdc slot is NOT the dominant consumer of ledger-db's volume (e.g. per-caller WAL/table growth accounting for most of the usage).
- Evidence that a neighbouring shared-ledger-db caller (order-service, portfolio-service, settlement-service) or a background process spiked write/WAL volume in 14:15-15:01Z and is the true trigger.
- ledger-db disk_used_pct or replication_lag_ms metrics showing the disk was not actually at/near capacity, contradicting the 95%/98%/100% log progression.
- An out-of-band ledger-db schema/migration change (or a test showing ledger-db rejects MANDATE_HOLD) that would make the new entry type a contributing cause after all.
- Evidence of an alternate error signature in the unsampled 15:13-16:57Z window (e.g. validation/constraint errors) indicating a second, independent failure mode.
- Evidence that a cleanup or slot drop occurred before 16:59Z yet failures continued, which would break the ENOSPC mechanism as the explanation for the sustained outage.

## Integrity warnings (set by code)
- fact demoted to assumption: No migration, config, feature-flag or ledger-side release was recorded in the in

## Investigation stats
- latency 115.41s, LLM calls 12, tokens 77662 (in 61146 / out 16516)
- tool calls 21 (2 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
