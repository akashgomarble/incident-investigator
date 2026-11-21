# Incident investigation report: INC-012
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
The incident is a shared-infrastructure failure: ledger-db's data volume hit 100% and Postgres could no longer extend its files, so every writer's ledger writes failed. payment-service v6.0.0 (MANDATE_HOLD flow) is a coincidental amplifier — it added write volume and made payment the most visible victim, but the disk was already saturated at 14:55Z, ~5 minutes before payment's first write error at 15:00:26Z. The decisive evidence is that order-service and settlement-service, which never received v6.0.0 and do not use the mandate flow, spiked in the same 15:00Z bucket to 47.65% and 53.91%, and all three services stayed elevated (~59-64% / ~47% / ~54%) through 16:55Z after the 15:12Z payment rollback while disk remained pinned at 100%. Confidence is medium (0.72): the origin (ledger-db storage layer) is well supported, but the specific mechanism detail — that the inactive analytics_cdc replication slot's retained WAL is what filled the disk — remains inference-only.

## Probable root cause
**ledger-db disk exhaustion from retained WAL (shared infra) — payment v6.0.0 is a coincidental pairing** -- resource_exhaustion in `ledger-db`, triggered by `null` (confidence 0.72, medium)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk exhaustion from retained WAL (shared infra) — payment v6.0.0 is a coincidental pairing | resource_exhaustion | ledger-db | null | 0.72 (medium) | active |
| 2 | H2: payment-service v6.0.0 mandate flow regression writes a bad MANDATE_HOLD payload ledger-db cannot accept | bad_deployment | payment-service | dep-6203 | 0.05 (low) | rejected |
| 3 | H3: Independent shared-dependency failure (kafka or bank-partner-api) as the common trigger | dependency_failure | kafka | null | 0.03 (low) | rejected |
| 4 | H4: Shared ledger-db failure that is configuration/schema-triggered — a pending ledger config or migration that must accompany MANDATE_HOLD | config_change | ledger-db | null | 0.03 (low) | rejected |

### H1: ledger-db disk exhaustion from retained WAL (shared infra) — payment v6.0.0 is a coincidental pairing
The fault originates in the shared ledger-db storage layer: an inactive replication slot ('analytics_cdc') retained 78 GB of WAL, the data volume filled to 100%, and Postgres could no longer extend files (xlogtemp, base/*). All four writer services' writes fail simultaneously; payment-service's v6.0.0 MANDATE_HOLD work simply added write volume and made payment the most visible victim, but the disk was already ~97-99% full when it deployed (14:46Z) and hit exactly 100% at 14:55Z — before payment's first error at 15:00:26Z.

**Causal chain**
1. Inactive replication slot 'analytics_cdc' begins retaining large volumes of WAL on ledger-db [F13]
2. Retained WAL grows the ledger-db data volume: disk_used_pct climbs steadily and crosses ~99% by 14:50Z [F10, F11, F13]
3. Disk reaches exactly 100.0% at 14:55Z and stays pinned; WARN logs already fired at 95% (14:15:45Z) and 98% (14:48:20Z) [F10, F11]
4. ledger-db can no longer extend data files: PANIC 'could not write to file pg_wal/xlogtemp...' (15:01:01Z) and 'could not extend file base/16402/2619' (15:01:39Z) [F11]
5. Every writer service into ledger-db fails: payment-service logs 'ledger write failed: could not extend file: No space left on device' from 15:00:26Z [F9]
6. Because ledger-db is a shared single point of failure, order-service (47.65%) and settlement-service (53.91%) spike in the same 15:00Z bucket as payment-service (61.52%) [F5, F8, F12]
7. Reported symptom: payment-service error rate ~60% with ledger write failures, sustained because the disk remains full [F8, F9, F10]

**Supporting evidence**
  - `F5` ✔ ledger-db is a SHARED datastore with four writer services: order-service, portfolio-service, payment-service and settlement-service all have 'db' edges into ledger-db; each of those services (except payment-service, which is also called by api-gateway) is reached from api-gateway over http. ledger-db is therefore a single shared point of failure coupling payment-service and settlement-service to order-service and portfolio-service.
  - `F8` ✔ payment-service error_rate changed from a stable ~0.18–0.28% baseline (14:00–14:55 buckets) to 61.52% in the 15:00:00Z bucket, then held at ~59–64% through 16:55Z (unit: % of requests, HTTP 5xx). The onset is bounded between the 14:55 and 15:00 buckets.
  - `F9` ✔ The earliest payment-service ledger write failure is logged at 2026-06-01T15:00:26Z; the dominant (and only) error pattern over 14:00–17:00 is verbatim: 'ledger write failed: ERROR: could not extend file: No space left on device (payment e3489438)'. 354 ERROR lines matched, all this single pattern, spanning 15:00:26Z to 16:57:54Z.
  - `F10` ✔ ledger-db disk_used_pct rose steadily from 89.39% at 13:00Z, crossed ~99% by 14:50, hit exactly 100.0 at 14:55Z and remained pinned at 100.0% for every 5-minute bucket through 16:55Z. This saturation predates the payment error onset by ~5–10 minutes.
  - `F11` ✔ ledger-db itself is failing on writes with verbatim signatures: 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device' (first at 15:01:01Z, 153 occurrences) and 'could not extend file "base/16402/2619": No space left on device' (first at 15:01:39Z, 139 occurrences). Its top patterns also include WARN 'disk usage 95% on /var/lib/postgresql/data' at 14:15:45Z and 'disk usage 98%' at 14:48:20Z.
  - `F12` ✔ Blast radius is not limited to payment-service: order-service error_rate jumped from ~0.18–0.28% to 47.65% in the same 15:00Z bucket, and settlement-service from ~0.13–0.24% to 53.91% in the same 15:00Z bucket; both remain elevated (~47% and ~54%) through 16:55Z. The onset is simultaneous across all three services.
  - `F13` ✔ ledger-db exposes WARN about an inactive replication slot retaining WAL ('replication slot 'analytics_cdc' is inactive; retaining 78423 MB of WAL', first seen 14:16:25Z), which is consistent with disk growth, and a late WARN 'clock drift <n>ms detected, resyncing with NTP' first seen 16:23:23Z (clock drift, if real, could affect timestamp precision on ledger-db).
  - `F14` ✔ Precedent HIST-114 (2025-04-18, 'ledger-db disk full from retained WAL') is the closest known match to the current signature: services ['ledger-db'], symptoms ['no space left on device','write failures'], root_cause 'Inactive replication slot retained WAL until disk filled', category resource_exhaustion, resolution 'Dropped slot; added slot-lag alert'. It matched both the write-failure query (match_score 10) and the retained-WAL query (match_score 7).
  - `F15` ✔ The HIST-114 precedent was resolved by two steps beyond stopping the writer: 'Dropped slot; added slot-lag alert' — i.e. clearing the retained WAL on the storage layer plus adding monitoring, not by rolling back an application release. The incident record lists no application-code change as cause or fix.
  - `F17` ✔ Precedent ledger 'write failures' in the corpus have non-application root causes: HIST-114 = inactive replication slot retaining WAL (resource_exhaustion); HIST-102 = unplanned ledger-db primary failover / refused connections (infrastructure_failure); HIST-110 = ledger-db IOPS throttling / burst credits exhausted (infrastructure_failure); HIST-111 = unindexed reporting query (bad_deployment, but a reporting job, not a ledger write path). No precedent attributes ledger write failures to a rejected new entry type from a payment-service release.
**Contradicting evidence**
  - `F1` ✔ In the window 14:30Z-15:10Z the only deployment across ALL services was payment-service v6.0.0 (id dep-6203) by p.nair at 2026-06-01T14:46:00Z, summary 'New UPI mandate flow', changes ['mandate create/approve endpoints','new ledger entry type MANDATE_HOLD'], status 'rolled_back' at 2026-06-01T15:12:00Z. This is ~24 min before the 15:10Z report and ~15 min before symptom onset as reported.
**Assumptions**
  - That disk growth was caused by the analytics_cdc slot retention rather than by some other (unmeasured) table/bloat mechanism — not directly verified, only inferred from the WAL-retention WARN.
  - That the reported symptom was caused by these errors and not by volume that v6.0.0 added.

### H2: payment-service v6.0.0 mandate flow regression writes a bad MANDATE_HOLD payload ledger-db cannot accept
The reporter's suspicion: the new v6.0.0 mandate flow introduced the new ledger entry type MANDATE_HOLD with a payload/size that ledger-db rejects, so ledger writes from payment-service fail and error rate hits 60%. Origin is the payment-service release (bad_deployment), fix is rollback (or fixing the entry type).

**Causal chain**
1. payment-service v6.0.0 deploys at 14:46Z with new mandate create/approve endpoints and new ledger entry type MANDATE_HOLD [F1]
2. The mandate flow writes MANDATE_HOLD ledger entries that ledger-db rejects, producing write failures *(assumed)*
3. payment-service error rate jumps to 61.52% with ledger write failures [F8, F9]
4. Rollback at 15:12Z restores writes and clears the symptom *(assumed)*

**Supporting evidence**
  - `F1` ✔ In the window 14:30Z-15:10Z the only deployment across ALL services was payment-service v6.0.0 (id dep-6203) by p.nair at 2026-06-01T14:46:00Z, summary 'New UPI mandate flow', changes ['mandate create/approve endpoints','new ledger entry type MANDATE_HOLD'], status 'rolled_back' at 2026-06-01T15:12:00Z. This is ~24 min before the 15:10Z report and ~15 min before symptom onset as reported.
**Contradicting evidence**
  - `F2` ✔ No deployment occurred for any ledger component, and no service other than payment-service deployed in the 14:30Z-15:10Z window; deployment count over all services was 1.
  - `F5` ✔ ledger-db is a SHARED datastore with four writer services: order-service, portfolio-service, payment-service and settlement-service all have 'db' edges into ledger-db; each of those services (except payment-service, which is also called by api-gateway) is reached from api-gateway over http. ledger-db is therefore a single shared point of failure coupling payment-service and settlement-service to order-service and portfolio-service.
  - `F8` ✔ payment-service error_rate changed from a stable ~0.18–0.28% baseline (14:00–14:55 buckets) to 61.52% in the 15:00:00Z bucket, then held at ~59–64% through 16:55Z (unit: % of requests, HTTP 5xx). The onset is bounded between the 14:55 and 15:00 buckets.
  - `F9` ✔ The earliest payment-service ledger write failure is logged at 2026-06-01T15:00:26Z; the dominant (and only) error pattern over 14:00–17:00 is verbatim: 'ledger write failed: ERROR: could not extend file: No space left on device (payment e3489438)'. 354 ERROR lines matched, all this single pattern, spanning 15:00:26Z to 16:57:54Z.
  - `F10` ✔ ledger-db disk_used_pct rose steadily from 89.39% at 13:00Z, crossed ~99% by 14:50, hit exactly 100.0 at 14:55Z and remained pinned at 100.0% for every 5-minute bucket through 16:55Z. This saturation predates the payment error onset by ~5–10 minutes.
  - `F11` ✔ ledger-db itself is failing on writes with verbatim signatures: 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device' (first at 15:01:01Z, 153 occurrences) and 'could not extend file "base/16402/2619": No space left on device' (first at 15:01:39Z, 139 occurrences). Its top patterns also include WARN 'disk usage 95% on /var/lib/postgresql/data' at 14:15:45Z and 'disk usage 98%' at 14:48:20Z.
  - `F12` ✔ Blast radius is not limited to payment-service: order-service error_rate jumped from ~0.18–0.28% to 47.65% in the same 15:00Z bucket, and settlement-service from ~0.13–0.24% to 53.91% in the same 15:00Z bucket; both remain elevated (~47% and ~54%) through 16:55Z. The onset is simultaneous across all three services.
  - `F16` ✔ No historical incident in the retrieved precedents matches 'rejected new ledger entry type' / ledger schema-validation-after-deploy: the entry-type/schema query returned HIST-109 (JWT library clock-skew, match 3), HIST-105 (bank partner 5xx, 2), HIST-108 (JWT key rotation, 2), HIST-111 (unindexed report query, 2), HIST-114 (disk full, 2), HIST-101 (bcrypt cost, 1) — none describe a rejected new ledger entry type or a ledger-service schema/validation change.
  - `F17` ✔ Precedent ledger 'write failures' in the corpus have non-application root causes: HIST-114 = inactive replication slot retaining WAL (resource_exhaustion); HIST-102 = unplanned ledger-db primary failover / refused connections (infrastructure_failure); HIST-110 = ledger-db IOPS throttling / burst credits exhausted (infrastructure_failure); HIST-111 = unindexed reporting query (bad_deployment, but a reporting job, not a ledger write path). No precedent attributes ledger write failures to a rejected new entry type from a payment-service release.
  - `F18` ✔ No unreleased or pending ledger-db configuration change was found: config-change queries returned count 0 for both all-services (2026-06-01T00:00–16:59Z) and ledger-db scope (2026-05-01T00:00–2026-06-01T14:30Z), and the ledger-db deployment query (2026-05-01–2026-06-01T15:15Z) returned count 0 with an empty deployments list. So nothing in the change/config inventory supplies a pending schema or flag that must accompany the MANDATE_HOLD entry type.
  - `F19` ✔ Precedent for whether rollback alone suffices is only partial and points the other way: the code-regression precedents were resolved by rollback (HIST-109 'Rolled back; pinned skew config', HIST-101 'Rolled back v6.4.0'), whereas the ledger-db write-failure precedent HIST-114 required dropping the slot and adding an alert. The corpus contains no incident where a payment-service release caused ledger write failures and rollback alone resolved it.
**Assumptions**
  - That a ledger-side schema/validation layer exists that could 'reject' the entry type (no ledger-service exists in inventory; ledger is a bare postgres node).
  - That rollback stops the errors despite the disk still being at 100%.
**Rejected because:** Contradicted on mechanism, scope and timing. (a) Mechanism: the actual error text is a storage error — 'could not extend file: No space left on device' and ledger-db PANICs on pg_wal/base file writes — not a schema/validation rejection; no finding shows any 'unknown entry type' error (F9, F11). The only rejection-shaped evidence is the reporter's description, and the corpus has zero precedent for a rejected entry type from a release (F16, F17). (b) Scope: order-service and settlement-service, which did NOT receive v6.0.0 and do not use the mandate flow, spiked simultaneously in the same 15:00Z bucket (F5, F12) — a caller-side regression cannot explain them. (c) Timing/state: ledger-db was already at ~99-100% disk before payment's first write error (F10, F11); the disk fullness is sufficient and independent of the new entry type (F2, F18). (d) Precedent: no incident in the corpus was fixed by rolling back a payment release for ledger write failures; the matching precedent required clearing storage (F15, F17, F19).

### H3: Independent shared-dependency failure (kafka or bank-partner-api) as the common trigger
A second shared component — kafka (produced to by payment-service and order-service, consumed by settlement-service) or the external bank-partner-api — degraded independently around 15:00Z and produced the simultaneous failures, with the ledger disk fullness being an unrelated or secondary fact.

**Causal chain**
1. A shared component used by all affected services degrades around the onset window *(assumed)*
2. Failure propagates to payment-service, order-service and settlement-service simultaneously [F6, F12]
3. Reported as payment-service payment failures and ledger write failures [F8]

**Supporting evidence**
  - `F6` ✔ kafka is a second SHARED component: payment-service and order-service produce to it, while settlement-service and notification-service consume from it, so a kafka problem propagates between the payments and trading/growth paths independently of the v6.0.0 change.
  - `F12` ✔ Blast radius is not limited to payment-service: order-service error_rate jumped from ~0.18–0.28% to 47.65% in the same 15:00Z bucket, and settlement-service from ~0.13–0.24% to 53.91% in the same 15:00Z bucket; both remain elevated (~47% and ~54%) through 16:55Z. The onset is simultaneous across all three services.
**Contradicting evidence**
  - `F9` ✔ The earliest payment-service ledger write failure is logged at 2026-06-01T15:00:26Z; the dominant (and only) error pattern over 14:00–17:00 is verbatim: 'ledger write failed: ERROR: could not extend file: No space left on device (payment e3489438)'. 354 ERROR lines matched, all this single pattern, spanning 15:00:26Z to 16:57:54Z.
  - `F10` ✔ ledger-db disk_used_pct rose steadily from 89.39% at 13:00Z, crossed ~99% by 14:50, hit exactly 100.0 at 14:55Z and remained pinned at 100.0% for every 5-minute bucket through 16:55Z. This saturation predates the payment error onset by ~5–10 minutes.
  - `F11` ✔ ledger-db itself is failing on writes with verbatim signatures: 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device' (first at 15:01:01Z, 153 occurrences) and 'could not extend file "base/16402/2619": No space left on device' (first at 15:01:39Z, 139 occurrences). Its top patterns also include WARN 'disk usage 95% on /var/lib/postgresql/data' at 14:15:45Z and 'disk usage 98%' at 14:48:20Z.
**Assumptions**
  - That kafka or bank-partner-api is actually unhealthy — no metric/trace check of kafka, bank-partner-api or their lag/5xx was performed (GAPS: shared-component health unverified).
**Rejected because:** The observed error text is unequivocally a disk-full error from ledger-db's own filesystem (F9, F11), which a kafka or bank-partner-api fault cannot produce, and ledger-db disk is verified pinned at 100% (F10). The shared-component health check was never performed (tool budget), so this hypothesis rests entirely on assumption and is contradicted by the concrete error signatures.

### H4: Shared ledger-db failure that is configuration/schema-triggered — a pending ledger config or migration that must accompany MANDATE_HOLD
The origin is a ledger-db-side state change: an unreleased/pending ledger configuration or schema migration was needed to accept the new MANDATE_HOLD entries, and its absence (or a rolled-out-but-broken config) caused writes to fail across all writer services. Distinguished from H1 in that the failing mechanism is a config/schema state owned by the platform, not raw disk capacity.

**Causal chain**
1. A pending ledger-db config or schema migration is required to support the new MANDATE_HOLD entry type *(assumed)*
2. Without it, ledger writes for the new entry type are rejected at the datastore *(assumed)*
3. All writer services that touch the affected tables fail simultaneously [F5, F12]
4. Symptom: ledger write failures and ~60% payment-service error rate [F8, F9]

**Supporting evidence**
  - `F5` ✔ ledger-db is a SHARED datastore with four writer services: order-service, portfolio-service, payment-service and settlement-service all have 'db' edges into ledger-db; each of those services (except payment-service, which is also called by api-gateway) is reached from api-gateway over http. ledger-db is therefore a single shared point of failure coupling payment-service and settlement-service to order-service and portfolio-service.
  - `F12` ✔ Blast radius is not limited to payment-service: order-service error_rate jumped from ~0.18–0.28% to 47.65% in the same 15:00Z bucket, and settlement-service from ~0.13–0.24% to 53.91% in the same 15:00Z bucket; both remain elevated (~47% and ~54%) through 16:55Z. The onset is simultaneous across all three services.
**Contradicting evidence**
  - `F3` ✔ Zero config changes were recorded across all services in 14:30Z-15:10Z (count 0, empty list), so no config/flag change explains the onset in that window.
  - `F10` ✔ ledger-db disk_used_pct rose steadily from 89.39% at 13:00Z, crossed ~99% by 14:50, hit exactly 100.0 at 14:55Z and remained pinned at 100.0% for every 5-minute bucket through 16:55Z. This saturation predates the payment error onset by ~5–10 minutes.
  - `F11` ✔ ledger-db itself is failing on writes with verbatim signatures: 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device' (first at 15:01:01Z, 153 occurrences) and 'could not extend file "base/16402/2619": No space left on device' (first at 15:01:39Z, 139 occurrences). Its top patterns also include WARN 'disk usage 95% on /var/lib/postgresql/data' at 14:15:45Z and 'disk usage 98%' at 14:48:20Z.
  - `F16` ✔ No historical incident in the retrieved precedents matches 'rejected new ledger entry type' / ledger schema-validation-after-deploy: the entry-type/schema query returned HIST-109 (JWT library clock-skew, match 3), HIST-105 (bank partner 5xx, 2), HIST-108 (JWT key rotation, 2), HIST-111 (unindexed report query, 2), HIST-114 (disk full, 2), HIST-101 (bcrypt cost, 1) — none describe a rejected new ledger entry type or a ledger-service schema/validation change.
  - `F18` ✔ No unreleased or pending ledger-db configuration change was found: config-change queries returned count 0 for both all-services (2026-06-01T00:00–16:59Z) and ledger-db scope (2026-05-01T00:00–2026-06-01T14:30Z), and the ledger-db deployment query (2026-05-01–2026-06-01T15:15Z) returned count 0 with an empty deployments list. So nothing in the change/config inventory supplies a pending schema or flag that must accompany the MANDATE_HOLD entry type.
**Assumptions**
  - That a pending/unreleased ledger config exists but is simply not representable in the change inventory — the GAPS note that no schema/flag endpoint exists, so this is unobserved rather than proven.
**Rejected because:** No config change is recorded anywhere in the window (F3, count 0) and no ledger-db deploy or config change exists from 2026-05-01 through the incident (F18, count 0), so there is no evidence of any config/schema change to blame. The observable failure mechanism is disk exhaustion (F10, F11), not a validation/schema error, and no corpus precedent describes a rejected entry type (F16). This hypothesis survives only as an unobservable possibility and cannot be supported by admissible evidence.

## Facts (verified observations)
- The only deployment across all services in 14:30Z-15:10Z was payment-service v6.0.0 (dep-6203, 'New UPI mandate flow', changes include 'new ledger entry type MANDATE_HOLD'), deployed 14:46:00Z and rolled back 15:12:00Z; no ledger component deployed and total deployment count was 1. [F1, F2]
- Zero config changes were recorded across all services in 14:30Z-15:10Z, and ledger-db had zero deployments and zero config changes from 2026-05-01 through the incident, so no config/schema/flag change explains the onset. [F3, F18]
- ledger-db is a shared datastore with four writers (order-service, portfolio-service, payment-service, settlement-service), making it a single point of failure coupling the payments and trading/settlements paths. [F5, F4]
- ledger-db disk_used_pct rose from 89.39% at 13:00Z and reached exactly 100.0% at 14:55Z, remaining pinned at 100.0% for every 5-minute bucket through 16:55Z — before payment-service's first write error. [F10]
- ledger-db itself was failing on writes with verbatim 'PANIC: could not write to file "pg_wal/xlogtemp.37478": No space left on device' (first 15:01:01Z, 153 occurrences) and 'could not extend file "base/16402/2619": No space left on device' (first 15:01:39Z, 139 occurrences), preceded by WARN 'disk usage 95%' at 14:15:45Z and 'disk usage 98%' at 14:48:20Z. [F11]
- payment-service error_rate moved from a stable ~0.18-0.28% baseline to 61.52% in the 15:00:00Z bucket and held ~59-64% through 16:55Z; its only error pattern (354 lines, 15:00:26Z-16:57:54Z) is verbatim 'ledger write failed: ERROR: could not extend file: No space left on device' — a storage error, not a schema/validation rejection. [F8, F9]
- Blast radius is not limited to payment-service: order-service jumped from ~0.18-0.28% to 47.65% and settlement-service from ~0.13-0.24% to 53.91% in the same 15:00Z bucket, both staying elevated through 16:55Z, i.e. onset was simultaneous across services that never received v6.0.0. [F12]
- Precedent HIST-114 (2025-04-18, 'ledger-db disk full from retained WAL') is the closest known match: symptom 'no space left on device'/write failures, root cause 'inactive replication slot retained WAL until disk filled', resolution 'Dropped slot; added slot-lag alert' — a storage-layer fix, not an application rollback. [F14, F15]
- No historical incident matches a 'rejected new ledger entry type' or ledger schema-validation-after-deploy signature (best match_score 3, loosely related), while prior ledger write failures in the corpus have non-application root causes (retained WAL, primary failover, IOPS throttling). [F16]

## Assumptions (not verified)
- That the disk growth was caused by the analytics_cdc inactive replication slot's retained WAL rather than another unmeasured table/bloat mechanism — inferred from the retention WARN (F13), not directly measured (no WAL-size or slot-size metric).
- That the reported symptom ('ledger write failures', 60% error rate) is caused by the disk-full storage errors rather than by volume added by the v6.0.0 MANDATE_HOLD work; F12 (non-payment writers failing simultaneously) supports this but does not fully exclude an added-volume contribution.
- That no further deployments or config changes occurred between 15:10Z and 16:59Z; the post-15:10Z deployment window could not be verified due to a query argument error.
- That ledger-db's disk saturation was not itself caused by a shared dependency (kafka/bank-partner-api) fault, since no kafka or bank-partner-api health check was performed.
- (demoted from facts: no verified observation backs it) ledger-db emits WARN that replication slot 'analytics_cdc' is inactive and retaining 78423 MB of WAL (first seen 14:16:25Z), consistent with the observed disk growth.

## Timeline
- `2026-05-01T00:00:00Z` No ledger-db deployment or config change occurs at any point from this date through the incident (count 0), establishing no ledger-side change preceded the fault (F18). [F18]
- `2026-06-01T13:00:00Z` ledger-db disk_used_pct is 89.39% and begins rising steadily toward saturation (F10). [F10]
- `2026-06-01T14:15:45Z` ledger-db logs WARN 'disk usage 95% on /var/lib/postgresql/data' (F11). [F11]
- `2026-06-01T14:16:25Z` ledger-db logs WARN that replication slot 'analytics_cdc' is inactive and retaining 78423 MB of WAL (F13). [F13]
- `2026-06-01T14:46:00Z` payment-service v6.0.0 (dep-6203, 'New UPI mandate flow', new ledger entry type MANDATE_HOLD) deploys — the only deployment across all services in the window (F1, F2). [F1, F2]
- `2026-06-01T14:48:20Z` ledger-db logs WARN 'disk usage 98%' (F11). [F11]
- `2026-06-01T14:55:00Z` ledger-db disk_used_pct reaches exactly 100.0% and stays pinned there through 16:55Z — before any payment error (F10). [F10]
- `2026-06-01T15:00:00Z` Simultaneous error-rate spike across all three writers: payment-service 61.52%, order-service 47.65%, settlement-service 53.91% (F8, F12). [F8, F12]
- `2026-06-01T15:00:26Z` Earliest payment-service ledger write failure logged: 'ledger write failed: ERROR: could not extend file: No space left on device' (F9). [F9]
- `2026-06-01T15:01:01Z` ledger-db PANICs on 'could not write to file "pg_wal/xlogtemp.37478": No space left on device', followed by 'could not extend file "base/16402/2619"' at 15:01:39Z (F11). [F11]
- `2026-06-01T15:10:00Z` Incident INC-012 reported by on-call SRE; reporter describes payment failures and announces an immediate rollback (F1). [F1]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rollback completes (dep-6203 status 'rolled_back') (F1). [F1]
- `2026-06-01T16:55:00Z` All three services remain elevated (payment ~59-64%, order ~47%, settlement ~54%) and ledgger-db disk remains pinned at 100% — no recovery after the rollback (F8, F12, F10). [F8, F12, F10]

## Recommended actions
- **[immediate]** Free ledger-db disk capacity on the shared storage layer: drop (or re-activate) the inactive replication slot 'analytics_cdc' to release the ~78 GB of retained WAL, per precedent HIST-114 ('Dropped slot'). -- The disk is the verified failure point (pinned at 100% since 14:55Z, F10) and ledger-db is PANICking on pg_wal/base writes (F11). Dropping the retained-WAL slot is the exact fix used in the closest precedent (F14, F15). Do NOT expect the paymment rollback to fix this — writes stayed broken through 16:55Z after the 15:12Z rollback (F8, F12). [F10, F11, F13, F14, F15]
- **[immediate]** Verify recovery immediately after clearing storage: re-check ledger-db disk_used_pct (should fall below 100%) and confirm payment-service, order-service and settlement-service error_rate return toward their ~0.2% baselines; this also confirms H1 and retires the residual doubt about the storage-vs-volume split. -- Cause precedes onset and is currently pinned (F10); a post-clear recovery observation is the one missing piece that would fully confirm H1, and it discriminates H1 from an added-volume-only explanation. [F10, F8, F12]
- **[short_term]** Do not pursue a payment-side schema/validation fix or treat the v6.0.0 rollback as the remediation: confirm the rollback did not restore writes (already true through 16:55Z) and avoid re-rolling-forward MANDATE_HOLD until the disk is clear. -- The observed error is a storage error, not a rejection (F9, F11); no rejected-entry-type precedent exists (F16), zero ledger config/deploy changes occurred (F3, F18), and non-payment writers failed simultaneously (F12, F5). Rolling back a payment release cannot fix a full disk (F19). [F9, F11, F16, F3, F18, F12, F19]
- **[short_term]** Restore/monitor the three writer services' error rates and backlog after the disk fix; check settlement-service consumer_lag and any queue backlog built up during 15:00-17:00Z as the writers recover. -- All three writers share ledger-db (F5) and were failing for ~2 hours (F8, F12); backlog was never measured and may delay apparent recovery. [F5, F8, F12]
- **[long_term]** Add a slot-lag / retained-WAL alert and a disk-usage alarm on ledger-db (e.g. page at 85-90% on /var/lib/postgresql/data) so an inactive replication slot cannot silently fill the volume again. -- HIST-114's resolution was 'Dropped slot; added slot-lag alert' (F15), and the only pre-failure warnings were disk-usage WARNs at 95%/98% (F11) and the slot-retention WARN (F13) that no alert acted on. This prevents recurrence of the verified fault mechanism. [F13, F15, F11, F14]
- **[long_term]** Add write-volume/rate headroom monitoring on ledger-db and review write amplification from new ledger entry types (such as MANDATE_HOLD) against capacity, so future releases that add write volume are gated on storage headroom. -- The v6.0.0 MANDATE_HOLD flow added write volume to an already ~97-99% full volume (F1, F10), making payment the most visible victim; capacity headroom would keep a coincidental amplifier from becoming the visible failure. [F1, F10]

## Data gaps
- Post-15:10Z deployment/config window could not be queried (get_deployments failed with invalid_arguments 'end must be after start'), so existence of further changes between 15:10Z and 16:59Z is unverified.
- No schema-change or feature-flag endpoint was available; the absence of a pending ledger config is only shown indirectly via empty deployment/config-change inventories (F18) and cannot exclude an unrepresentable pending parameter value.
- No metric/trace-level health check of ledger-db, kafka or bank-partner-api (saturation, connections, lag, 5xx) was performed, so the shared-component health picture rests on disk_used_pct and log signatures only.
- ledger-db has no error_rate metric and connections_active, replication_lag_ms and slow_queries_per_min were not retrieved, so write/connection saturation and replication lag are not quantified.
- Blast radius beyond payment/order/settlement-service (api-gateway, auth-service, notification-service, exchange-gateway, kafka) and settlement-service consumer_lag was not checked before the tool budget was exhausted.
- Historical records lack per-incident timelines and verbatim errors and contain no precedent for a rejected new ledger entry type (best match_score 3), so precedent cannot confirm whether any writer rollback alone ever restored ledger writes.
- The specific mechanism that the analytics_cdc slot's retained WAL filled the disk is inference from a WARN (F13); no WAL-size or slot-size metric and no post-clear recovery observation exist yet.

## What would change this conclusion
- A post-clear observation showing ledger-db disk drops below 100% but the three writers' error rates stay elevated in the same storage-error pattern — that would shift the conclusion from disk exhaustion to another ledger-db storage-layer fault.
- Discovery of a ledger-side validation or schema error (e.g. 'unknown entry type MANDATE_HOLD') in ledger-db logs, or a ledger config/schema change invisible to the current inventory, would revive H2/H4.
- Evidence that kafka or bank-partner-api was independently degraded around 15:00Z (lag/5xx/traces) would revive H3 as a co-cause, since that health check was never run.
- A finding that payment-service write volume alone (not the shared disk state) accounted for the failures, e.g. error rate recovering after the 15:12Z rollback despite disk still full, would contradict H1.
- Evidence that the disk was filled by a mechanism other than the analytics_cdc slot's retained WAL (e.g. table bloat or another retention mechanism) would change the mechanism detail without overturning ledger-db storage as the origin.

## Integrity warnings (set by code)
- fact demoted to assumption: ledger-db emits WARN that replication slot 'analytics_cdc' is inactive and retai

## Investigation stats
- latency 105.99s, LLM calls 14, tokens 93999 (in 75661 / out 18338)
- tool calls 24 (4 failed, 0 partial, 0 recovered by retry), findings 19/19 verified
