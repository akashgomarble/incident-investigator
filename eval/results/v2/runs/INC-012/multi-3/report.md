# Incident investigation report: INC-012
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The payments outage is caused by disk exhaustion on the shared ledger-db datastore, not by payment-service v6.0.0. An inactive replication slot ('analytics_cdc') retained tens of GB of WAL from ~13:02Z, driving disk_used_pct on a smooth ~1%/6-min ramp from 86.57% at 12:30Z to 100% at the 15:00Z bucket, where it stayed pinned through 16:54Z (F3, F21, F22, F23). Postgres then rejected all writes with hard 'No space left on device' errors (F2, F4), breaking three of the four shared-ledger-db writers — payment-service, order-service and settlement-service — at the same 15:00Z onset, while the v6.0.0 release is coincidental (it shipped at 14:46Z, ~1.5-2h after saturation began, and its 15:12Z rollback produced no recovery: error_rate held ~60% through 16:55Z on complete metrics). Confidence in H2 is 0.78 (high). The correct fix is to free disk on ledger-db (drop/repair the retaining replication slot and purge retained WAL), not to roll back the release.

## Probable root cause
**Shared ledger-db disk exhaustion from retained WAL (inactive analytics_cdc replication slot) — hard write failures across co-tenant writers** -- infrastructure_failure in `ledger-db`, triggered by `null` (confidence 0.78, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: Shared ledger-db disk exhaustion from retained WAL (inactive analytics_cdc replication slot) — hard write failures across co-tenant writers | infrastructure_failure | ledger-db | null | 0.78 (high) | active |
| 2 | H5: Shared ledger-db walking a chronic disk-usage ceiling under aggregate multi-writer load (capacity saturation) | capacity_saturation | ledger-db | null | 0.38 (low) | active |
| 3 | H3: Disk growth on ledger-db from a concurrent/undetected schema or config change on the shared DB or queue | config_change | ledger-db | null | 0.04 (low) | rejected |
| 4 | H6: v6.0.0's new mandate flow generated a burst of ledger writes that tipped an already-nearly-full disk over | bad_deployment | payment-service | dep-6203 | 0.03 (low) | rejected |
| 5 | H1: payment-service v6.0.0 new mandate flow writes a ledger entry type that schema validation rejects | bad_deployment | payment-service | dep-6203 | 0.02 (low) | rejected |
| 6 | H4: External upstream dependency (bank-partner-api / auth / KYC) failing, with payment-service as downstream notifier | dependency_failure | bank-partner-api | null | 0.02 (low) | rejected |

### H2: Shared ledger-db disk exhaustion from retained WAL (inactive analytics_cdc replication slot) — hard write failures across co-tenant writers
A replication slot on the shared ledger-db ('analytics_cdc', inactive but retaining tens of GB of WAL repeatedly from ~13:02Z) drove the datastore disk on a smooth monotonic ramp to 100%, where it stayed pinned for >2 hours. Postgres then rejected ALL writes (PANIC could not write pg_wal xlogtemp, could not extend base file), so every writer to the shared ledger-db — payment-service, order-service and settlement-service — got hard 'No space left on device' errors. The fault origin is the shared datastore (infrastructure), not payment-service code; the v6.0.0 release is coincidental.

**Causal chain**
1. Inactive replication slot 'analytics_cdc' on shared ledger-db repeatedly retains tens of GB of WAL from ~13:02Z [F3]
2. WAL retention drives a smooth monotonic disk ramp: 86.57% (12:30Z) -> 89.37% (13:00Z) -> 90.40% (13:12Z) -> 97.48% (14:30Z) -> 99.88% (14:54Z) -> 100% at 15:00Z (no step change at the 14:46Z deploy) [F21, F22]
3. ledger-db hits full disk and emits hard PANICs ('could not write to pg_wal/xlogtemp', 'could not extend file') at ~15:01Z [F4]
4. Disk stays pinned at 100% from 15:00Z to 16:54Z with no self-recovery; rollback does not relieve it [F23]
5. All writes to the shared ledger-db fail with 'No space left on device' — payment-service (354 errors), order-service (236 errors), and settlement-service all degraded at the same ~15:00Z onset [F2, F33, F31, F32, F36]
6. payment-service error rate jumps to ~61.5% at 15:00Z and stays ~60-64% through 16:55Z, persisting unchanged after the 15:12Z rollback [F1, F19]

**Supporting evidence**
  - `F3` ✔ ledger-db's own logs show disk usage escalating from 13:10:01Z: 'disk usage 90%', then 95% at 14:15:45Z and 98% at 14:48:20Z — all before the 15:00Z payment onset. WAL was being retained by an inactive replication slot ('analytics_cdc', tens of GB at a time) repeatedly from 13:02Z.
  - `F4` ✔ ledger-db began emitting hard disk-full errors at 15:01:01Z — 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device' (153 occurrences) and 'could not extend file "base/16402/2619"' (139 occurrences) from 15:01:39Z. DB-level failure onset (~15:01Z) is ~1 minute after the payment-service error spike (~15:00Z).
  - `F2` ✔ Every payment-service ERROR log (354 of them) since 14:00Z is the single pattern 'ledger write failed: ERROR: could not extend file: No space left on device', first at 15:00:26Z. This is a disk-full DB error, NOT a validation error on the new entry type, a DB constraint violation, or a timeout.
  - `F1` ✔ payment-service error_rate jumped from baseline ~0.18–0.28% to 61.52% in the 15:00:00Z 5-minute bucket, and stayed ~60–64% through 16:55Z. The last clean bucket was 14:55:00Z at avg 0.18.
  - `F12` ✔ Prior ledger-write-failure incidents resolved through infrastructure/DB fixes, not through application code rollback: HIST-114 (2025-04-18, ledger-db 'disk full from retained WAL', 'write failures', fixed by dropping a replication slot) and HIST-110 (2026-01-15, ledger-db IOPS throttling, 'statement timeouts', fixed by provisioned IOPS); a payment-failure precedent HIST-105 (2025-11-19) was an external bank-partner outage, not a release.
  - `F17` ✔ Prior ledger-write-failure precedent HIST-114 (2025-04-18, ledger-db 'disk full from retained WAL', symptoms 'no space left on device', 'write failures') resolved by 'Dropped slot; added slot-lag alert' — an infra fix, not an application rollback; this is the closest precedent to the current signature.
  - `F11` ✔ There is no 'ledger-service' in the environment: get_service_dependencies returned not_found for it, with known_services listing only ledger-db (plus user-db, redis-cache, kafka) as shared infra — so all 'ledger writes' go to the shared ledger-db via four calling services.
  - `F18` ✔ There is no 'ledger-service'; ledger-db is the shared write target, and its telemetry exists (has_telemetry true, and it emits its own disk/PANIC logs), so the fault is observable at the DB layer independently of payment-service. No deployment or config change touched ledger-db in the window.
  - `F21` ✔ ledger-db disk_used_pct ramp trajectory from 12:30Z: 86.57% (12:30Z) -> 89.37% (13:00Z) -> 90.40% (13:12Z) -> 94.76% (14:00Z) -> 97.48% (14:30Z) -> 98.52% (14:42Z) -> 99.88% (14:54Z), i.e. a steady ~1%/6-min climb hitting 100% precisely at the 15:00Z bucket. This is a smooth monotonic ramp with no step change at the 14:46Z deploy.
  - `F22` ✔ disk_used_pct was already above 86% at the start of the requested window (12:30Z) and crossed 90% by 13:12Z — roughly 1h34m BEFORE the 14:46Z v6.0.0 deploy — confirming saturation pre-dates the release and that the disk headroom (86->100% = ~13.5 points) was consumed BEFORE, not after, the deploy.
  - `F23` ✔ disk_used_pct is pinned at exactly 100% from 15:00Z all the way to 16:54Z (every 6-min bucket avg 100.0, max 100.0, first 15:00:00Z bucket). The DB remains full for >2 hours with no self-recovery, so WAL retaining slot pressure is not being relieved by the rollback.
  - `F31` ✔ settlement-service error_rate was flat ~0.17–0.23% through the 14:45Z bucket, then jumped to avg 18.07% (max 54.52) at the 14:54Z bucket and settled to a sustained ~54–56% (e.g. 55.14% at 15:03Z, 55.29% at 16:51Z) — a degradation mirroring payment-service's onset.
  - `F32` ✔ order-service error_rate was flat ~0.20–0.23% through the 14:45Z bucket, then jumped to avg 16.17% (max 49.57) at the 14:54Z bucket and sustained ~47–49% (47.52% at 15:03Z, 47.5% at 16:51Z) — again mirroring payment-service's onset.
  - `F33` ✔ order-service's ledger/order writes are failing with the SAME disk-full signature as payment-service: 236 ERROR logs, all the single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', spanning 15:00:42Z–16:57:47Z.
  - `F36` ✔ Three of the four shared-ledger-db writers (payment-service, settlement-service, order-service) all broke at the same 15:00Z onset with disk-full DB errors, while the fourth (portfolio-service) stayed healthy — so the blast radius is broad but NOT universal across co-tenants.
  - `F19` ✔ Post-rollback error_rate persistence CONFIRMED: payment-service error_rate stayed elevated (~55-70%) in every 5-min bucket from 15:10Z through 16:55Z, with NO recovery after the 15:12Z rollback. Bucket avgs: 60.36 at 15:10Z, 63.21 at 15:15Z, 64.07 at 15:20Z, 63.5-64.4 mid-window, and still 61.10 at 16:55Z (summary min 55.31, max 70.45, mean 62.29, missing_points 0). The rollback at 15:12Z produced no visible drop at all.
  - `F20` ✔ error_rate metric is complete and reliable for this window: missing_points 0 and partial false over 110 total points, so absence of recovery is a real absence, not missing data.
**Contradicting evidence**
  - `F5` ✔ payment-service p99 latency shows NO degradation: it is flat ~292–348 ms across 14:00Z–16:55Z (322.49 ms at 15:00Z, 315.98 ms at 16:55Z). The failure is a hard write error, not a latency/saturation symptom.
  - `F34` ✔ portfolio-service error_rate did NOT degrade: it stayed at baseline ~0.15–0.23% across the whole 15:00Z–16:51Z window (summary mean 0.21, max 0.32), with no observable jump after 15:00Z.
**Assumptions**
  - The analytics_cdc slot is the dominant (not necessarily sole) consumer of the freed-retention headroom; the smooth 13.5-point ramp is consistent with slot-mediated WAL growth but quantified WAL/slot retention size was never directly retrieved
  - The trigger/activation of the slot retaining WAL at ~13:02Z is not recorded as a change event and is presumed chronic/unmonitored infra behaviour
  - portfolio-service (the fourth shared writer) is effectively immune because it is read-mostly / low-write / touches different tables — not because the shared-disk fault is payment-specific

### H5: Shared ledger-db walking a chronic disk-usage ceiling under aggregate multi-writer load (capacity saturation)
The shared ledger-db's disk was on a chronic trajectory toward its capacity limit under aggregate multi-writer load (order, portfolio, payment, settlement), with WAL retention (analytics_cdc) as the dominant but not sole consumer of headroom; the failure is capacity saturation of the shared datastore. Origin is the shared datastore's capacity, and the v6.0.0 release is coincidental. This shares the SAME origin (ledger-db disk reaching capacity) as H2 and differs only in the accounting of which consumer(s) filled the disk; it is a secondary-detail variant of H2, retained separately only to record that the exact apportionment of the fill (slot-only vs aggregate) is unmeasured.

**Causal chain**
1. Ledger-db disk climbs steadily under multi-writer load from four services (order, portfolio, payment, settlement) [F10, F16, F3, F21]
2. Disk crosses its capacity ceiling at ~15:00Z with a smooth 86->100% ramp that began hours earlier and is unaffected by the 14:46Z deploy [F21, F22, F4]
3. Hard write failures begin across the co-tenant writers at the same onset [F2, F33, F31, F32]
4. payment-service error rate spikes ~15:00Z and persists post-rollback [F1, F19]

**Supporting evidence**
  - `F3` ✔ ledger-db's own logs show disk usage escalating from 13:10:01Z: 'disk usage 90%', then 95% at 14:15:45Z and 98% at 14:48:20Z — all before the 15:00Z payment onset. WAL was being retained by an inactive replication slot ('analytics_cdc', tens of GB at a time) repeatedly from 13:02Z.
  - `F4` ✔ ledger-db began emitting hard disk-full errors at 15:01:01Z — 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device' (153 occurrences) and 'could not extend file "base/16402/2619"' (139 occurrences) from 15:01:39Z. DB-level failure onset (~15:01Z) is ~1 minute after the payment-service error spike (~15:00Z).
  - `F10` ✔ ledger-db is a SHARED postgres datastore (shared: true, owner 'platform') written by four services — order-service, portfolio-service, payment-service and settlement-service (all kind 'db') — so a schema/entry-type change introduced by payment-service v6.0.0 touches a component also used by non-payment services.
  - `F16` ✔ ledger-db is a shared postgres (owner platform, shared:true) written by four services — order-service, portfolio-service, payment-service, settlement-service — so a shared-infra fault would be expected to affect more than payment-service, consistent with hypothesis (ii) and inconsistent with a payment-only application bug.
  - `F2` ✔ Every payment-service ERROR log (354 of them) since 14:00Z is the single pattern 'ledger write failed: ERROR: could not extend file: No space left on device', first at 15:00:26Z. This is a disk-full DB error, NOT a validation error on the new entry type, a DB constraint violation, or a timeout.
  - `F21` ✔ ledger-db disk_used_pct ramp trajectory from 12:30Z: 86.57% (12:30Z) -> 89.37% (13:00Z) -> 90.40% (13:12Z) -> 94.76% (14:00Z) -> 97.48% (14:30Z) -> 98.52% (14:42Z) -> 99.88% (14:54Z), i.e. a steady ~1%/6-min climb hitting 100% precisely at the 15:00Z bucket. This is a smooth monotonic ramp with no step change at the 14:46Z deploy.
  - `F22` ✔ disk_used_pct was already above 86% at the start of the requested window (12:30Z) and crossed 90% by 13:12Z — roughly 1h34m BEFORE the 14:46Z v6.0.0 deploy — confirming saturation pre-dates the release and that the disk headroom (86->100% = ~13.5 points) was consumed BEFORE, not after, the deploy.
**Contradicting evidence**
  - `F5` ✔ payment-service p99 latency shows NO degradation: it is flat ~292–348 ms across 14:00Z–16:55Z (322.49 ms at 15:00Z, 315.98 ms at 16:55Z). The failure is a hard write error, not a latency/saturation symptom.
**Assumptions**
  - Disk growth is not solely attributable to the analytics_cdc slot but includes aggregate write/retention pressure from the four co-tenants
  - The exact apportionment between slot-retained WAL and base-table growth was never measured

### H3: Disk growth on ledger-db from a concurrent/undetected schema or config change on the shared DB or queue
A change to ledger-db's schema/retention or to a shared messaging/schema component in the same window is the true fault, causing the disk to fill; the fault origin would be the shared component, not payment-service code.

**Causal chain**
1. A concurrent config/schema change to ledger-db or a shared messaging/schema component occurs in the window *(assumed)*
2. The change increases write volume/row size or reduces retention headroom *(assumed)*
3. Disk fills and writes fail [F2, F3, F4]
4. Rollback of payment-service is partial/ineffective; errors persist post-rollback [F1, F13, F19]

**Supporting evidence**
  - `F1` ✔ payment-service error_rate jumped from baseline ~0.18–0.28% to 61.52% in the 15:00:00Z 5-minute bucket, and stayed ~60–64% through 16:55Z. The last clean bucket was 14:55:00Z at avg 0.18.
  - `F13` ✔ The ONLY deployment in 12:00Z-17:30Z is payment-service v6.0.0 at 14:46:00Z (dep-6203, status rolled_back at 15:12:00Z); a widened 12:00Z-17:59Z payment-service query and a 15:00Z-17:00Z all-services query returned count 0, so no code change followed the 15:12Z rollback.
**Contradicting evidence**
  - `F8` ✔ There were zero config/flag changes across all services in the 2026-06-01T12:00Z–16:59Z window (count 0), and zero config changes scoped to ledger-db over a widened 12:00Z–17:30Z window (count 0).
  - `F7` ✔ The only deployment across all services between 2026-06-01T12:00Z and 16:59Z is payment-service v6.0.0, deployed 2026-06-01T14:46:00Z by p.nair with summary 'New UPI mandate flow' and changes 'mandate create/approve endpoints' + 'new ledger entry type MANDATE_HOLD'; it is already status 'rolled_back' as of 2026-06-01T15:12:00Z.
  - `F13` ✔ The ONLY deployment in 12:00Z-17:30Z is payment-service v6.0.0 at 14:46:00Z (dep-6203, status rolled_back at 15:12:00Z); a widened 12:00Z-17:59Z payment-service query and a 15:00Z-17:00Z all-services query returned count 0, so no code change followed the 15:12Z rollback.
  - `F3` ✔ ledger-db's own logs show disk usage escalating from 13:10:01Z: 'disk usage 90%', then 95% at 14:15:45Z and 98% at 14:48:20Z — all before the 15:00Z payment onset. WAL was being retained by an inactive replication slot ('analytics_cdc', tens of GB at a time) repeatedly from 13:02Z.
  - `F15` ✔ Timing falsifies the release as trigger: v6.0.0 deployed 14:46:00Z, but ledger-db disk pressure began well before it (90% at 13:10:01Z, 95% at 14:15:45Z, 98% at 14:48:20Z) — the saturation pre-dates the deploy by ~1.5-2 hours, so the deploy cannot have caused the disk exhaustion.
  - `F25` ✔ No deployment of any kind occurred between 15:00Z and 17:00Z across all services (count 0), so any WAL/replication-slot cleanup in that window was not recorded as a deployment.
  - `F26` ✔ Zero config/flag changes across all services in 15:00Z-17:00Z (count 0), and zero config changes scoped to ledger-db over 15:00Z-17:30Z (count 0) — no operational change (slot drop, WAL purge) is recorded via the config-change channel either.
  - `F27` ✔ No ledger-db deployment is recorded in 15:00Z-17:30Z (count 0), so no DB-side remediation event is visible in the deployment ledger during the incident window.
**Assumptions**
  - Such a change exists despite config-change queries returning count 0
**Rejected because:** Zero config/flag changes across all services in both the 12:00-16:59Z and 15:00-17:00Z windows (F8, F26), zero config scoped to ledger-db in widened windows (F8, F16), and no ledger-db deployment at all in the incident window (F27). Disk pressure predates the only recorded change (F3, F15, F21), leaving no change-event origin. Rejected.

### H6: v6.0.0's new mandate flow generated a burst of ledger writes that tipped an already-nearly-full disk over
payment-service v6.0.0's new mandate flow increased ledger write volume on an already-critical ledger-db disk, acting as the final push that turned near-full into full; the underlying exhaustion is on shared ledger-db. Origin would be the release.

**Causal chain**
1. ledger-db disk already critical (90% at 13:10Z, 95% at 14:15Z, 98% at 14:48Z) before the deploy [F3]
2. v6.0.0 new mandate flow adds MANDATE_HOLD ledger writes from 14:46Z [F7, F14]
3. Added write volume pushes the nearly-full disk past capacity *(assumed)*
4. ledger-db hits disk-full at 15:01Z; payment-service error rate spikes [F4, F1, F2]

**Supporting evidence**
  - `F3` ✔ ledger-db's own logs show disk usage escalating from 13:10:01Z: 'disk usage 90%', then 95% at 14:15:45Z and 98% at 14:48:20Z — all before the 15:00Z payment onset. WAL was being retained by an inactive replication slot ('analytics_cdc', tens of GB at a time) repeatedly from 13:02Z.
  - `F4` ✔ ledger-db began emitting hard disk-full errors at 15:01:01Z — 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device' (153 occurrences) and 'could not extend file "base/16402/2619"' (139 occurrences) from 15:01:39Z. DB-level failure onset (~15:01Z) is ~1 minute after the payment-service error spike (~15:00Z).
  - `F7` ✔ The only deployment across all services between 2026-06-01T12:00Z and 16:59Z is payment-service v6.0.0, deployed 2026-06-01T14:46:00Z by p.nair with summary 'New UPI mandate flow' and changes 'mandate create/approve endpoints' + 'new ledger entry type MANDATE_HOLD'; it is already status 'rolled_back' as of 2026-06-01T15:12:00Z.
  - `F14` ✔ v6.0.0 shipped exactly the 'new ledger entry type MANDATE_HOLD' plus mandate endpoints — matching hypothesis (i)'s mechanism — yet the observed failure signature is a disk-full error, not a validation error on the new type.
**Contradicting evidence**
  - `F2` ✔ Every payment-service ERROR log (354 of them) since 14:00Z is the single pattern 'ledger write failed: ERROR: could not extend file: No space left on device', first at 15:00:26Z. This is a disk-full DB error, NOT a validation error on the new entry type, a DB constraint violation, or a timeout.
  - `F15` ✔ Timing falsifies the release as trigger: v6.0.0 deployed 14:46:00Z, but ledger-db disk pressure began well before it (90% at 13:10:01Z, 95% at 14:15:45Z, 98% at 14:48:20Z) — the saturation pre-dates the deploy by ~1.5-2 hours, so the deploy cannot have caused the disk exhaustion.
  - `F13` ✔ The ONLY deployment in 12:00Z-17:30Z is payment-service v6.0.0 at 14:46:00Z (dep-6203, status rolled_back at 15:12:00Z); a widened 12:00Z-17:59Z payment-service query and a 15:00Z-17:00Z all-services query returned count 0, so no code change followed the 15:12Z rollback.
  - `F5` ✔ payment-service p99 latency shows NO degradation: it is flat ~292–348 ms across 14:00Z–16:55Z (322.49 ms at 15:00Z, 315.98 ms at 16:55Z). The failure is a hard write error, not a latency/saturation symptom.
  - `F21` ✔ ledger-db disk_used_pct ramp trajectory from 12:30Z: 86.57% (12:30Z) -> 89.37% (13:00Z) -> 90.40% (13:12Z) -> 94.76% (14:00Z) -> 97.48% (14:30Z) -> 98.52% (14:42Z) -> 99.88% (14:54Z), i.e. a steady ~1%/6-min climb hitting 100% precisely at the 15:00Z bucket. This is a smooth monotonic ramp with no step change at the 14:46Z deploy.
  - `F22` ✔ disk_used_pct was already above 86% at the start of the requested window (12:30Z) and crossed 90% by 13:12Z — roughly 1h34m BEFORE the 14:46Z v6.0.0 deploy — confirming saturation pre-dates the release and that the disk headroom (86->100% = ~13.5 points) was consumed BEFORE, not after, the deploy.
  - `F19` ✔ Post-rollback error_rate persistence CONFIRMED: payment-service error_rate stayed elevated (~55-70%) in every 5-min bucket from 15:10Z through 16:55Z, with NO recovery after the 15:12Z rollback. Bucket avgs: 60.36 at 15:10Z, 63.21 at 15:15Z, 64.07 at 15:20Z, 63.5-64.4 mid-window, and still 61.10 at 16:55Z (summary min 55.31, max 70.45, mean 62.29, missing_points 0). The rollback at 15:12Z produced no visible drop at all.
  - `F31` ✔ settlement-service error_rate was flat ~0.17–0.23% through the 14:45Z bucket, then jumped to avg 18.07% (max 54.52) at the 14:54Z bucket and settled to a sustained ~54–56% (e.g. 55.14% at 15:03Z, 55.29% at 16:51Z) — a degradation mirroring payment-service's onset.
  - `F32` ✔ order-service error_rate was flat ~0.20–0.23% through the 14:45Z bucket, then jumped to avg 16.17% (max 49.57) at the 14:54Z bucket and sustained ~47–49% (47.52% at 15:03Z, 47.5% at 16:51Z) — again mirroring payment-service's onset.
  - `F33` ✔ order-service's ledger/order writes are failing with the SAME disk-full signature as payment-service: 236 ERROR logs, all the single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', spanning 15:00:42Z–16:57:47Z.
**Assumptions**
  - The mandate flow materially increased write volume in the 14:46-15:01Z window
**Rejected because:** F21/F22 show a smooth monotonic disk ramp (86.57%@12:30Z -> 90.40%@13:12Z, ~1h34m pre-deploy) with NO step change at the 14:46Z deploy — the disk trajectory was independent of the release. F19/F20 confirm error rate persisted ~60% after the 15:12Z rollback with complete metrics, and F31/F32/F33 show order-service and settlement-service (which never ran v6.0.0) broke at the same onset with the same disk-full signature — a release-side volume push cannot explain co-tenant failures. Rejected.

### H1: payment-service v6.0.0 new mandate flow writes a ledger entry type that schema validation rejects
The new MANDATE_HOLD ledger entry type introduced by payment-service v6.0.0 is rejected by the ledger write path, causing the ~60% payment failure rate. Reporter's suspicion; rollback should fix it.

**Causal chain**
1. payment-service v6.0.0 deployed 14:46Z adding mandate endpoints + new ledger entry type MANDATE_HOLD [F7, F14]
2. New entry type flows to the ledger write path [F9]
3. Ledger write path rejects the entry type / validation fails *(assumed)*
4. payment-service returns 'ledger write failed' and error rate jumps to ~61% [F1, F2]

**Supporting evidence**
  - `F7` ✔ The only deployment across all services between 2026-06-01T12:00Z and 16:59Z is payment-service v6.0.0, deployed 2026-06-01T14:46:00Z by p.nair with summary 'New UPI mandate flow' and changes 'mandate create/approve endpoints' + 'new ledger entry type MANDATE_HOLD'; it is already status 'rolled_back' as of 2026-06-01T15:12:00Z.
  - `F14` ✔ v6.0.0 shipped exactly the 'new ledger entry type MANDATE_HOLD' plus mandate endpoints — matching hypothesis (i)'s mechanism — yet the observed failure signature is a disk-full error, not a validation error on the new type.
  - `F10` ✔ ledger-db is a SHARED postgres datastore (shared: true, owner 'platform') written by four services — order-service, portfolio-service, payment-service and settlement-service (all kind 'db') — so a schema/entry-type change introduced by payment-service v6.0.0 touches a component also used by non-payment services.
**Contradicting evidence**
  - `F2` ✔ Every payment-service ERROR log (354 of them) since 14:00Z is the single pattern 'ledger write failed: ERROR: could not extend file: No space left on device', first at 15:00:26Z. This is a disk-full DB error, NOT a validation error on the new entry type, a DB constraint violation, or a timeout.
  - `F3` ✔ ledger-db's own logs show disk usage escalating from 13:10:01Z: 'disk usage 90%', then 95% at 14:15:45Z and 98% at 14:48:20Z — all before the 15:00Z payment onset. WAL was being retained by an inactive replication slot ('analytics_cdc', tens of GB at a time) repeatedly from 13:02Z.
  - `F15` ✔ Timing falsifies the release as trigger: v6.0.0 deployed 14:46:00Z, but ledger-db disk pressure began well before it (90% at 13:10:01Z, 95% at 14:15:45Z, 98% at 14:48:20Z) — the saturation pre-dates the deploy by ~1.5-2 hours, so the deploy cannot have caused the disk exhaustion.
  - `F5` ✔ payment-service p99 latency shows NO degradation: it is flat ~292–348 ms across 14:00Z–16:55Z (322.49 ms at 15:00Z, 315.98 ms at 16:55Z). The failure is a hard write error, not a latency/saturation symptom.
  - `F13` ✔ The ONLY deployment in 12:00Z-17:30Z is payment-service v6.0.0 at 14:46:00Z (dep-6203, status rolled_back at 15:12:00Z); a widened 12:00Z-17:59Z payment-service query and a 15:00Z-17:00Z all-services query returned count 0, so no code change followed the 15:12Z rollback.
  - `F12` ✔ Prior ledger-write-failure incidents resolved through infrastructure/DB fixes, not through application code rollback: HIST-114 (2025-04-18, ledger-db 'disk full from retained WAL', 'write failures', fixed by dropping a replication slot) and HIST-110 (2026-01-15, ledger-db IOPS throttling, 'statement timeouts', fixed by provisioned IOPS); a payment-failure precedent HIST-105 (2025-11-19) was an external bank-partner outage, not a release.
  - `F19` ✔ Post-rollback error_rate persistence CONFIRMED: payment-service error_rate stayed elevated (~55-70%) in every 5-min bucket from 15:10Z through 16:55Z, with NO recovery after the 15:12Z rollback. Bucket avgs: 60.36 at 15:10Z, 63.21 at 15:15Z, 64.07 at 15:20Z, 63.5-64.4 mid-window, and still 61.10 at 16:55Z (summary min 55.31, max 70.45, mean 62.29, missing_points 0). The rollback at 15:12Z produced no visible drop at all.
  - `F21` ✔ ledger-db disk_used_pct ramp trajectory from 12:30Z: 86.57% (12:30Z) -> 89.37% (13:00Z) -> 90.40% (13:12Z) -> 94.76% (14:00Z) -> 97.48% (14:30Z) -> 98.52% (14:42Z) -> 99.88% (14:54Z), i.e. a steady ~1%/6-min climb hitting 100% precisely at the 15:00Z bucket. This is a smooth monotonic ramp with no step change at the 14:46Z deploy.
  - `F22` ✔ disk_used_pct was already above 86% at the start of the requested window (12:30Z) and crossed 90% by 13:12Z — roughly 1h34m BEFORE the 14:46Z v6.0.0 deploy — confirming saturation pre-dates the release and that the disk headroom (86->100% = ~13.5 points) was consumed BEFORE, not after, the deploy.
  - `F31` ✔ settlement-service error_rate was flat ~0.17–0.23% through the 14:45Z bucket, then jumped to avg 18.07% (max 54.52) at the 14:54Z bucket and settled to a sustained ~54–56% (e.g. 55.14% at 15:03Z, 55.29% at 16:51Z) — a degradation mirroring payment-service's onset.
  - `F32` ✔ order-service error_rate was flat ~0.20–0.23% through the 14:45Z bucket, then jumped to avg 16.17% (max 49.57) at the 14:54Z bucket and sustained ~47–49% (47.52% at 15:03Z, 47.5% at 16:51Z) — again mirroring payment-service's onset.
  - `F33` ✔ order-service's ledger/order writes are failing with the SAME disk-full signature as payment-service: 236 ERROR logs, all the single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', spanning 15:00:42Z–16:57:47Z.
**Assumptions**
  - Schema validation of the new entry type actually rejects MANDATE_HOLD
**Rejected because:** Actual error signature is a disk-full DB error ('could not extend file: No space left on device'), not a validation/constraint error (F2). Disk pressure began ~1.5-2h BEFORE the deploy (F3, F15, F21/F22). The deploy was rolled back at 15:12Z yet errors persisted at ~60% through 16:55Z with complete metrics (F19, F20). order-service and settlement-service, which never ran v6.0.0, failed identically (F31, F32, F33). HIST-114 was fixed by dropping a replication slot, not by rollback (F12, F17). Rejected.

### H4: External upstream dependency (bank-partner-api / auth / KYC) failing, with payment-service as downstream notifier
payment-service errors are a downstream symptom of an unrelated upstream dependency failing (bank-partner-api or a mandate/KYC provider); payment-service is only the reporter.

**Causal chain**
1. An upstream external dependency degrades *(assumed)*
2. payment-service calls it in the payment/mandate path and fails [F9]
3. payment-service error rate rises and logs 'ledger write failed' [F1, F2]

**Supporting evidence**
  - `F9` ✔ payment-service's dependency path is payment-service -> ledger-db (kind 'db'), payment-service -> kafka (kind 'queue-produce'), payment-service -> bank-partner-api (kind 'external'), with api-gateway -> payment-service (kind 'http') upstream.
**Contradicting evidence**
  - `F2` ✔ Every payment-service ERROR log (354 of them) since 14:00Z is the single pattern 'ledger write failed: ERROR: could not extend file: No space left on device', first at 15:00:26Z. This is a disk-full DB error, NOT a validation error on the new entry type, a DB constraint violation, or a timeout.
  - `F3` ✔ ledger-db's own logs show disk usage escalating from 13:10:01Z: 'disk usage 90%', then 95% at 14:15:45Z and 98% at 14:48:20Z — all before the 15:00Z payment onset. WAL was being retained by an inactive replication slot ('analytics_cdc', tens of GB at a time) repeatedly from 13:02Z.
  - `F4` ✔ ledger-db began emitting hard disk-full errors at 15:01:01Z — 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device' (153 occurrences) and 'could not extend file "base/16402/2619"' (139 occurrences) from 15:01:39Z. DB-level failure onset (~15:01Z) is ~1 minute after the payment-service error spike (~15:00Z).
  - `F5` ✔ payment-service p99 latency shows NO degradation: it is flat ~292–348 ms across 14:00Z–16:55Z (322.49 ms at 15:00Z, 315.98 ms at 16:55Z). The failure is a hard write error, not a latency/saturation symptom.
  - `F12` ✔ Prior ledger-write-failure incidents resolved through infrastructure/DB fixes, not through application code rollback: HIST-114 (2025-04-18, ledger-db 'disk full from retained WAL', 'write failures', fixed by dropping a replication slot) and HIST-110 (2026-01-15, ledger-db IOPS throttling, 'statement timeouts', fixed by provisioned IOPS); a payment-failure precedent HIST-105 (2025-11-19) was an external bank-partner outage, not a release.
  - `F31` ✔ settlement-service error_rate was flat ~0.17–0.23% through the 14:45Z bucket, then jumped to avg 18.07% (max 54.52) at the 14:54Z bucket and settled to a sustained ~54–56% (e.g. 55.14% at 15:03Z, 55.29% at 16:51Z) — a degradation mirroring payment-service's onset.
  - `F32` ✔ order-service error_rate was flat ~0.20–0.23% through the 14:45Z bucket, then jumped to avg 16.17% (max 49.57) at the 14:54Z bucket and sustained ~47–49% (47.52% at 15:03Z, 47.5% at 16:51Z) — again mirroring payment-service's onset.
  - `F33` ✔ order-service's ledger/order writes are failing with the SAME disk-full signature as payment-service: 236 ERROR logs, all the single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', spanning 15:00:42Z–16:57:47Z.
**Assumptions**
  - bank-partner-api or a KYC provider is genuinely failing in this window
**Rejected because:** The error signature is an internal shared-DB disk-full error (F2, F33), not an external API error. p99 latency is flat (F5), inconsistent with an upstream stall. ledger-db's own logs show the originating disk exhaustion (F3, F4). Critically, order-service and settlement-service — which do not call bank-partner-api on the payment path — failed with the same disk-full signature (F31, F33), so the origin is shared infra, not an external dependency. Rejected.

## Facts (verified observations)
- Every payment-service ERROR log since 14:00Z (354 of them) is the single pattern 'ledger write failed: ERROR: could not extend file: No space left on device', first at 15:00:26Z — a disk-full DB error, not a validation/constraint error or timeout. [F2]
- ledger-db's own logs show disk usage escalating from 13:10:01Z ('disk usage 90%'), 95% at 14:15:45Z and 98% at 14:48:20Z, with WAL retained by an inactive replication slot ('analytics_cdc', tens of GB at a time) repeatedly from 13:02Z. [F3]
- ledger-db emitted hard disk-full errors from 15:01:01Z: 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device' (153 occurrences) and 'could not extend file "base/16402/2619"' (139 occurrences) from 15:01:39Z — DB-level failure onset ~1 minute after the payment-service error spike. [F4]
- ledger-db disk_used_pct followed a smooth monotonic ramp: 86.57% (12:30Z) -> 89.37% (13:00Z) -> 90.40% (13:12Z) -> 94.76% (14:00Z) -> 97.48% (14:30Z) -> 98.52% (14:42Z) -> 99.88% (14:54Z), with no step change at the 14:46Z deploy. [F21]
- disk_used_pct was already above 86% at the start of the window (12:30Z) and crossed 90% by 13:12Z — ~1h34m before the 14:46Z v6.0.0 deploy — so the ~13.5 points of headroom were consumed before, not after, the release. [F22]
- disk_used_pct was pinned at exactly 100% from the 15:00Z bucket all the way to 16:54Z (every 6-min bucket avg 100.0), with no self-recovery for >2 hours. [F23]
- payment-service error_rate jumped from a ~0.18-0.28% baseline to 61.52% in the 15:00:00Z 5-minute bucket and stayed ~60-64% through 16:55Z (last clean bucket 14:55:00Z at avg 0.18). [F1]
- Post-rollback persistence is confirmed on complete, reliable metrics: error_rate stayed elevated (~55-70%; mean 62.29) in every 5-min bucket from 15:10Z through 16:55Z (still 61.10 at 16:55Z) with missing_points 0 and partial=false over 110 points — the 15:12Z rollback produced no visible drop. [F19, F20]
- settlement-service error_rate was flat ~0.17-0.23% through the 14:45Z bucket, jumped to avg 18.07% at the 14:54Z bucket and sustained ~54-56% through 16:51Z — mirroring payment-service's onset. [F31]
- order-service error_rate was flat ~0.20-0.23% through the 14:45Z bucket, jumped to avg 16.17% at the 14:54Z bucket and sustained ~47-49% through 16:51Z. [F32]
- order-service's writes are failing with the SAME disk-full signature as payment-service: 236 ERROR logs, all the single pattern 'INSERT INTO orders failed: No space left on device orderId=<n>', spanning 15:00:42Z-16:57:47Z. [F33]
- portfolio-service (the fourth shared-ledger-db writer) did NOT degrade: error_rate stayed at baseline ~0.15-0.23% across the whole 15:00Z-16:51Z window (mean 0.21, max 0.32), with only 1 benign WARN log, zero ERROR logs, and db_pool_in_use flat ~11.9-12.0 connections. [F34, F35]
- The only deployment across all services between 12:00Z and 16:59Z is payment-service v6.0.0 (dep-6203) at 14:46:00Z ('New UPI mandate flow'; changes 'mandate create/approve endpoints' and 'new ledger entry type MANDATE_HOLD'), already status 'rolled_back' at 15:12:00Z. [F7, F13, F14]
- There were zero config/flag changes across all services in the 12:00Z-16:59Z window and zero scoped to ledger-db in a widened 12:00Z-17:30Z window; separately, zero config/flag changes across all services in 15:00Z-17:00Z and zero scoped to ledger-db over 15:00Z-17:30Z. [F8, F26]
- No deployment of any kind occurred between 15:00Z and 17:00Z across all services (count 0), and no ledger-db deployment is recorded in 15:00Z-17:30Z (count 0). [F25, F27]
- ledger-db is a SHARED postgres datastore (shared:true, owner 'platform') written by four services — order-service, portfolio-service, payment-service and settlement-service — so a shared-infra fault is expected to affect more than payment-service. [F10, F16]
- There is no 'ledger-service' in the environment (get_service_dependencies returned not_found; known_services lists only ledger-db, user-db, redis-cache, kafka as shared infra), so all ledger writes go to the shared ledger-db via its calling services; ledger-db has its own telemetry and emits its own disk/PANIC logs. [F11, F18]
- Prior ledger-write-failure incidents resolved via infrastructure/DB fixes, not application rollback: HIST-114 (2025-04-18, ledger-db 'disk full from retained WAL', symptoms 'no space left on device'/'write failures', fixed by 'Dropped slot; added slot-lag alert') is the closest precedent; HIST-110 was fixed by provisioned IOPS; payment-failure precedent HIST-105 was an external bank-partner outage. [F12, F28]
- No current-incident (2026-06-01) record mentions an analytics_cdc slot drop or any WAL/slot cleanup; HIST-114 is a 2025 precedent mechanism, not a record of a 2026-06-01 cleanup. [F29]
- payment-service p99 latency shows no degradation (flat ~292-348 ms across 14:00Z-16:55Z; 322.49 ms at 15:00Z, 315.98 ms at 16:55Z) — consistent with a hard write error rather than latency/saturation. [F5]

## Assumptions (not verified)
- The analytics_cdc slot is the dominant (not necessarily sole) consumer of the consumed retention headroom; the smooth ~13.5-point ramp is consistent with slot-mediated WAL growth, but quantified WAL/slot retention size was never directly retrieved.
- The trigger/activation of the slot retaining WAL at ~13:02Z is not recorded as a change event and is presumed chronic/unmonitored infra behaviour.
- portfolio-service is effectively immune because it is read-mostly / low-write / touches different tables — not because the shared-disk fault is payment-specific.
- The residual H2-vs-H5 question (exact apportionment of the disk fill between slot-retained WAL and aggregate base-table growth) is a secondary accounting detail of the same origin, not an alternative root cause.
- The incident is still ongoing at 16:59Z (no recovery observed); the recovery mechanism is presumed to be a WAL/slot cleanup analogous to HIST-114, though no 2026-06-01 cleanup event was recorded.
- (demoted from facts: no verified observation backs it) Three of the four shared-ledger-db writers (payment-service, settlement-service, order-service) broke at the same 15:00Z onset with disk-full DB errors, while the fourth (portfolio-service) stayed healthy — blast radius is broad but not universal across co-tenants.
- (demoted from facts: no verified observation backs it) Timing falsifies the release as trigger: v6.0.0 deployed 14:46:00Z, but ledger-db disk pressure began well before it (90% at 13:10:01Z, 95% at 14:15:45Z, 98% at 14:48:20Z), pre-dating the deploy by ~1.5-2 hours.

## Timeline
- `2026-06-01T12:30:00Z` ledger-db disk_used_pct already at 86.57% at the start of the observed window, on a ~1%/6-min monotonic climb (F21, F22). [F21, F22]
- `2026-06-01T13:02:00Z` Inactive replication slot 'analytics_cdc' begins repeatedly retaining tens of GB of WAL on ledger-db; no change event explains the activation (F3). [F3]
- `2026-06-01T13:10:01Z` ledger-db logs disk usage 90% (F3, F15). [F3, F15]
- `2026-06-01T13:12:00Z` ledger-db disk_used_pct 90.40% — ~1h34m before the v6.0.0 deploy (F21, F22). [F21, F22]
- `2026-06-01T14:15:45Z` ledger-db logs disk usage 95% (F3, F15). [F3, F15]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (dep-6203, new UPI mandate flow / MANDATE_HOLD ledger entry type) deployed — roughly 1.5-2h AFTER disk saturation began (F7, F13, F14, F15). [F7, F13, F14, F15]
- `2026-06-01T14:48:20Z` ledger-db logs disk usage 98% (F3, F15). [F3, F15]
- `2026-06-01T14:54:00Z` disk_used_pct 99.88%; settlement-service and order-service error_rate jump in the 14:54Z bucket (18.07% and 16.17% avgs) (F21, F31, F32). [F21, F31, F32]
- `2026-06-01T15:00:00Z` Disk reaches and pins at 100%; payment-service error_rate jumps to 61.52% (last clean bucket 14:55Z at 0.18%); co-tenant writers break at the same onset (F1, F21, F23, F36). [F1, F21, F23, F36]
- `2026-06-01T15:00:26Z` First payment-service ERROR: 'ledger write failed: ERROR: could not extend file: No space left on device' (F2). [F2]
- `2026-06-01T15:00:42Z` First order-service disk-full write error: 'INSERT INTO orders failed: No space left on device' (F33). [F33]
- `2026-06-01T15:01:01Z` ledger-db begins hard disk-full failures: 'PANIC: could not write to file pg_wal/xlogtemp.37478: No space left on device' (153 occurrences); base-file extension failures from 15:01:39Z (139 occurrences) (F4). [F4]
- `2026-06-01T15:10:00Z` Incident INC-012 reported by on-call SRE; errors sustained at ~60% (F19). [F19]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rolled back — no recovery follows (F7, F19). [F7, F19]
- `2026-06-01T15:10:00Z-16:55:00Z` error_rate stays elevated (~55-70%, mean 62.29) in every 5-min bucket on complete metrics (missing_points 0); disk remains pinned at 100% through 16:54Z (F19, F20, F23). [F19, F20, F23]
- `2026-06-01T16:55:00Z` payment-service error_rate still 61.10%; ledger-db disk still 100% — no self-recovery, incident ongoing (F19, F23). [F19, F23]

## Recommended actions
- **[immediate]** IMMEDIATE MITIGATION: Free disk on the shared ledger-db — terminate/drop the inactive retaining replication slot 'analytics_cdc' (pg_drop_replication_slot) and force WAL checkpoint/retention release, then confirm disk_used_pct falls from 100%. This is the fix that resolved the matching precedent HIST-114 ('Dropped slot; added slot-lag alert'). -- Disk exhaustion on shared ledger-db is the root cause; the retaining inactive slot is the dominant fill driver. Dropping the slot is the proven remediation from the closest precedent, not a rollback. [F3, F4, F12, F17, F21, F23, F28]
- **[immediate]** IMMEDIATE: Confirm recovery by watching payment-service, order-service and settlement-service error_rate return toward baseline (~0.2%) and ledger-db disk_used_pct drop below 100% after the slot/WAL cleanup; roll back any remaining non-essential writer load if disk does not free promptly. -- Verifies the mitigation actually restores write capacity; error_rate recovery is the operational success signal and distinguishes cleanup-driven recovery from continued saturation. [F1, F19, F23, F33]
- **[immediate]** IMMEDIATE (do not repeat): Do NOT treat the v6.0.0 rollback as the fix — it has already been applied at 15:12Z and error_rate remained ~60% through 16:55Z; keep the rollback only if independently justified, not as the remedy for this incident. -- Post-rollback persistence on complete metrics falsifies the deployment as the cause, so relying on rollback will not restore service. [F19, F20, F7]
- **[short_term]** SHORT TERM: Add a slot-lag / replication-slot retention alert and a ledger-db disk-headroom alert (warn at ~80%, page at ~90%) so a retaining inactive slot is caught before disk reaches 100%. -- The disk ramp ran 86%->100% over ~2.5h with only log thresholds and no alerting; the HIST-114 fix explicitly added a slot-lag alert, and this recurrence shows it was missing or ineffective. [F3, F21, F22, F17]
- **[short_term]** SHORT TERM: Audit and clean up inactive/orphaned replication slots on the shared ledger-db and configure a retention cap (e.g. max_slot_wal_keep_size) so a single inactive slot cannot retain WAL to disk-full. -- Root mechanism is a single inactive slot retaining tens of GB of WAL; capping retention bounds the blast radius across all four co-tenant writers. [F3, F10, F16, F36]
- **[short_term]** SHORT TERM: Repair / drain the analytics_cdc consumer (or formally decommission it) and verify its slot is either active-and-lagging-within-bounds or removed, with a follow-up review of why it retained WAL from ~13:02Z with no recorded trigger. -- The slot 'analytics_cdc' is inactive and its activation at ~13:02Z has no recorded change event; without addressing the consumer, the slot will regrow. [F3, F28, F29]
- **[long_term]** LONG TERM: Move the shared ledger-db to capacity/headroom planning that accounts for aggregate multi-writer load plus WAL retention (provisioned IOPS/storage auto-grow), explicitly noting that H2 and H5 name the same origin — the datastore reaching capacity. Track WAL retention and base-table growth apportionment with metrics. -- The residual uncertainty is apportionment between slot-retained WAL and aggregate base-table growth, but either way the shared datastore is the single point of failure for four services. [F10, F16, F21, F24, F36]
- **[long_term]** LONG TERM: Make shared-infra faults distinguishable from app faults in runbooks: on a shared-DB disk-full signature ('No space left on device', xlogtemp PANIC), check co-tenant writers (order/settlement/portfolio) before considering application rollback. -- Three of four co-tenant writers broke together with the identical disk-full signature while the app-layer error pattern was a disk-full DB error, not a validation error; runbooks should route to infra remediation first. [F2, F31, F32, F33, F36, F12]

## Data gaps
- No direct observation of the WAL/slot retention SIZE or an explicit WAL/slot telemetry series — the claim that analytics_cdc is the dominant filler rests on the ledger-db log observation of 'tens of GB' retained plus the smooth ramp, not on a quantified WAL byte count.
- The exact apportionment of the disk fill between slot-retained WAL and aggregate base-table growth from the four co-tenants was never measured, which is the residual H2-vs-H5 distinction (same origin, different accounting).
- The ~13:02Z activation of the retaining analytics_cdc slot has no recorded change event; zero config/deployment events were found for ledger-db or any service in the window.
- No 2026-06-01 cleanup/slot-drop record exists: the deployment ledger shows no ledger-db deployment or config change 15:00Z-17:30Z, and incident records contain no current-incident cleanup entry — so the recovery mechanism is inferred from the HIST-114 precedent, not observed.
- The incident was still active at the latest data (16:54Z/16:55Z); no confirmation of recovery (error_rate back to ~0.2%, disk <100%) is available within the window.
- portfolio-service's immunity is unexplained at the data level (no finer-grained pool/latency or write-path check), bounding how universally the shared-infra blast radius can be claimed.
- settlement-service ERROR/WARN log query returned total_matched=0, so its ~55% error_rate could not be confirmed at the log level to be the same disk-full DB signature (order-service's was, F33).
- order-service ERROR log result was truncated (236 matched, 15 returned), so a minority of non-disk-full error signatures in the unreturned records cannot be fully excluded.
- Some legacy ledger-db metric names failed (tc-012 query_metrics disk_usage_pct; tc-015 error_rate), though the disk_used_pct series was ultimately obtained (F21/F22/F23).

## What would change this conclusion
- Evidence that ledger-db disk_used_pct did NOT ramp before 14:46Z (e.g. a step change coincident with the deploy) — would revive H1/H6 as release-triggered. Current data directly contradicts this (F21, F22).
- Evidence that only payment-service degraded while order-service and settlement-service (which never ran v6.0.0) stayed healthy — would point to a payment-specific origin. Current data contradicts this (F31, F32, F33, F36).
- Evidence that payment-service error_rate recovered promptly after the 15:12Z rollback — would support a deployment cause. Current data contradicts this (F19, F20).
- A verified (non-disk-full) schema/validation rejection of the MANDATE_HOLD entry type in payment-service logs — would revive H1. The observed error signature is exclusively disk-full (F2).
- Discovery that the error signature is actually an upstream/external failure (bank-partner-api/KYC) or a latency saturation, with flat disk usage — would revive H4. Flat p99 latency and DB-layer PANICs contradict this (F5, F4).
- Direct evidence that a different consumer (aggregate base-table growth, not the analytics_cdc slot) filled the disk — would shift the origin-accounting toward H5 (though the origin, ledger-db capacity, and the fix remain the same).
- Evidence that a ledger-db config/deployment change did occur in the window (contradicting the zero-count change queries) — would revive H3.

## Integrity warnings (set by code)
- fact demoted to assumption: Three of the four shared-ledger-db writers (payment-service, settlement-service,
- fact demoted to assumption: Timing falsifies the release as trigger: v6.0.0 deployed 14:46:00Z, but ledger-d

## Investigation stats
- latency 173.79s, LLM calls 26, tokens 186553 (in 153248 / out 33305)
- tool calls 40 (5 failed, 0 partial, 0 recovered by retry), findings 36/36 verified
