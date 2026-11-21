# Incident investigation report: INC-012
*System: multi_agent | outcome: **root_cause_identified** | ended: critic_accepted*

## Summary
Root cause (high confidence, 0.78): the shared PostgreSQL datastore ledger-db exhausted its data volume on /var/lib/postgresql/data, with WAL retained by an inactive replication slot, and every writer on that datastore surfaced "No space left on device" write failures. This is strongly supported: disk-pressure warnings began at 13:10Z (~1.5h before the only change in the window, payment-service v6.0.0 at 14:46Z), ledger-db itself logged internal storage-exhaustion PANICs on pg_wal and data files at 15:01Z, and order-service — which received no deployment and never writes MANDATE_HOLD — failed with the identical storage error in the same minute. payment-service v6.0.0 is best explained as a temporal coincidence (possibly a minor, innocent contributor to write volume), not the origin of the fault; the reported "new ledger entry type" theory is rejected on mechanism and scope. The rollback of v6.0.0 at 15:12Z did not restore service because it did not touch the exhausted shared volume.

## Probable root cause
**Independent ledger-db storage exhaustion (inactive replication slot retaining WAL) fills the disk; all four writer services fail** -- resource_exhaustion in `ledger-db`, triggered by `null` (confidence 0.78, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Independent ledger-db storage exhaustion (inactive replication slot retaining WAL) fills the disk; all four writer services fail | resource_exhaustion | ledger-db | null | 0.78 (high) | active |
| 2 | H4: Rollback of payment-service is masking a still-broken shared datastore; the fault predates and outlives the deploy | resource_exhaustion | ledger-db | null | 0.62 (medium) | active |
| 3 | H2: payment-service v6.0.0 mandate flow's new MANDATE_HOLD write pattern amplified ledger-db pressure and triggered the disk exhaustion | bad_deployment | payment-service | dep-6203 | 0.22 (low) | active |
| 4 | H3: v6.0.0's new MANDATE_HOLD entry type was rejected by ledger-db schema/enum (or a missing migration), producing ledger write failures | data_issue | ledger-db | dep-6203 | 0.03 (low) | rejected |

### H1: Independent ledger-db storage exhaustion (inactive replication slot retaining WAL) fills the disk; all four writer services fail
ledger-db, the shared postgres backing ledger writes, ran out of disk on /var/lib/postgresql/data because an inactive replication slot retained WAL. Disk crossed 90% at 13:10Z and 98% at 14:48Z, then hard-failed at ~15:01Z with PANICs writing pg_wal and extending data files. Every writer on the shared datastore — payment-service, order-service (and likely portfolio/settlement) — surfaced 'No space left on device' write failures within the same minute. payment-service v6.0.0 is a temporal coincidence plus a plausible innocent contributor of a small amount of extra write volume, not the origin of the fault.

**Causal chain**
1. Inactive replication slot retains WAL on ledger-db, consuming disk persistently from at least 13:02Z [F9, F17]
2. Disk utilisation climbs 90% (13:10Z) → 95% (14:15Z) → 98% (14:48Z) on the data volume [F9]
3. Disk hits full: PANICs 'could not write to file pg_wal/xlogtemp' (15:01:01Z) and 'could not extend file base/...' (15:01:39Z) [F10]
4. All clients of the shared datastore get 'No space left on device' on their writes — payment-service 'ledger write failed: ERROR: could not extend file' at 15:00:26Z [F8, F12]
5. order-service fails independently with 'INSERT INTO orders failed: No space left on device' at 15:00:42Z, proving the blast radius is the shared store, not the payment code path [F11, F16, F12]
6. payment-service error rate jumps from ~0.2% to ~60% in the 15:00Z bucket and stays there; the mandate flow is one of many writers affected [F7]

**Supporting evidence**
  - `F8` ✔ The dominant payment-service ERROR signature is a downstream ledger write failure wrapping a storage error: 354 occurrences of 'ledger write failed: ERROR: could not extend file: No space left on device', first_seen 2026-06-01T15:00:26Z, level ERROR. No timeout, auth, or app-exception patterns dominate (only 2 GC WARNs, 2 connection-reset WARNs, 1 TLS WARN in the whole set).
  - `F9` ✔ ledger-db (the actual backing DB; there is no service named 'ledger-service') showed disk pressure BEFORE payment-service's errors: WARN 'disk usage 90% on /var/lib/postgresql/data' at 13:10:01Z, 95% at 14:15:45Z, and 98% at 14:48:20Z, plus repeated inactive replication-slot WAL retention warnings from 13:02:25Z. Hard write failures followed at 15:01:01Z.
  - `F10` ✔ ledger-db's failures are internal storage exhaustion, not downstream dependency failures: 153 PANICs 'could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (first 15:01:01Z) and 139 'could not extend file "base/<n>/<n>": No space left on device' (first 15:01:39Z) — the DB itself ran out of disk for WAL and data files.
  - `F11` ✔ order-service shows the SAME storage error independently, not routed through payment: 236 occurrences of 'INSERT INTO orders failed: No space left on device', first_seen 2026-06-01T15:00:42Z — i.e. it began degrading in the same minute as payment-service, indicating a shared-storage blast radius rather than a payment-v6.0.0-specific bug.
  - `F12` ✔ Ordering of onsets: ledger-db disk-pressure WARNs (13:10Z onward) precede payment-service errors; ledger-db hard PANICs (15:01:01Z) are essentially simultaneous with, not after, payment-service's first error (15:00:26Z) and order-service's first error (15:00:42Z) — the consumers surfaced errors within the same minute the DB ran out of space.
  - `F16` ✔ ledger-db is a shared postgres (owner 'platform', shared: true) with four writer services — order-service, portfolio-service, payment-service, settlement-service all hold 'kind':'db' edges to it. This is the channel by which a payment-service change could affect or be co-affected with neighbours, and explains a shared-storage blast radius.
  - `F17` ✔ The closest historical precedent for this exact symptom set is HIST-114 (2025-04-18) 'ledger-db disk full from retained WAL' with symptoms 'no space left on device','write failures' and root cause 'Inactive replication slot retained WAL until disk filled' (category resource_exhaustion) — matching the current signature and the T1-reported inactive-replication-slot WAL warnings. No historical incident has 'new ledger entry type' / enum / schema-migration as a root cause in the top matches.
  - `F5` ✔ There is no service named 'ledger-service' in the topology/release system; the only ledger component is the shared datastore ledger-db (known_services include 'ledger-db' and no separate ledger-service). So no ledger-side application deploy could have happened.
  - `F15` ✔ No deployment and no config change was EVER recorded for ledger-db: count 0 for deployments and count 0 for config changes over 2026-05-25T00:00Z-2026-06-01T16:59Z. Therefore no DB-side migration or schema change exists in the window to key a 'missing DB migration' or 'new entry type not accepted' mechanism.
**Contradicting evidence**
  - `F1` ✔ Exactly one deployment occurred anywhere in the 13:30-15:30Z window: dep-6203, payment-service v6.0.0 (author p.nair, 'New UPI mandate flow'), deployed_at 2026-06-01T14:46:00Z, changes include 'new ledger entry type MANDATE_HOLD', status 'rolled_back' at 2026-06-01T15:12:00Z.
  - `F13` ✔ The only change anywhere in 12:00-16:59Z is dep-6203: payment-service v6.0.0 'New UPI mandate flow' (changes include 'new ledger entry type MANDATE_HOLD'), deployed 2026-06-01T14:46:00Z, status 'rolled_back' with rolled_back_at 2026-06-01T15:12:00Z. It is the earliest change preceding the 15:00Z onset, starting ~14 min BEFORE onset, and was rolled back ~12 min after onset.
**Assumptions**
  - The disk-pressure WARNs and WAL-retention warnings originate from the same inactive replication slot mechanism as HIST-114 (the log wording matches but the slot identity was not directly read)
  - The 15:00 bucket boundary hides a sub-minute onset; the true head of the causal chain is the disk fill, which predates it by ~2 hours
  - ledger-db disk capacity/occupancy was a genuine exhausted-volume rather than a quota/permission error (message text says No space left on device)

### H4: Rollback of payment-service is masking a still-broken shared datastore; the fault predates and outlives the deploy
payment-service v6.0.0 was rolled back at 15:12Z, ~12 minutes after onset, but the ledger-db disk exhaustion that actually causes the write failures is untouched by that rollback. If failures persist after 15:12Z, it confirms the origin is ledger-db's exhausted storage, not the release. This is a discriminator-framing hypothesis about the intervention evidence rather than a separate origin, and it converges on ledger-db resource exhaustion.

**Causal chain**
1. ledger-db begins disk-pressure warnings at 13:10Z and WAL-retention warnings from 13:02Z, well before any deploy [F9]
2. payment-service v6.0.0 deploys 14:46Z and is rolled back 15:12Z, but the underlying volume stays full [F1, F13]
3. payment-service error rate stays 59-64% through the end of the window (16:59Z), past the 15:12Z rollback [F7]
4. order-service, which received no change at all, is failing with the same storage error from 15:00:42Z, so removing payment v6.0.0 cannot restore writes [F11, F16]

**Supporting evidence**
  - `F7` ✔ payment-service error_rate was at baseline ~0.19-0.29% through the 2026-06-01T14:56:00Z bucket, then jumped to avg 60.15% / max 60.8% in the 15:00:00Z bucket and stayed 59-64% for the rest of the window. The departure is therefore at ~15:00Z, not ~15:10-15:15Z as the report's '~15 min after v6.0.0' claim implies (15:10 is only the report time). The 240s bucket means onset is bounded to 14:56-15:00.
  - `F9` ✔ ledger-db (the actual backing DB; there is no service named 'ledger-service') showed disk pressure BEFORE payment-service's errors: WARN 'disk usage 90% on /var/lib/postgresql/data' at 13:10:01Z, 95% at 14:15:45Z, and 98% at 14:48:20Z, plus repeated inactive replication-slot WAL retention warnings from 13:02:25Z. Hard write failures followed at 15:01:01Z.
  - `F11` ✔ order-service shows the SAME storage error independently, not routed through payment: 236 occurrences of 'INSERT INTO orders failed: No space left on device', first_seen 2026-06-01T15:00:42Z — i.e. it began degrading in the same minute as payment-service, indicating a shared-storage blast radius rather than a payment-v6.0.0-specific bug.
  - `F13` ✔ The only change anywhere in 12:00-16:59Z is dep-6203: payment-service v6.0.0 'New UPI mandate flow' (changes include 'new ledger entry type MANDATE_HOLD'), deployed 2026-06-01T14:46:00Z, status 'rolled_back' with rolled_back_at 2026-06-01T15:12:00Z. It is the earliest change preceding the 15:00Z onset, starting ~14 min BEFORE onset, and was rolled back ~12 min after onset.
  - `F16` ✔ ledger-db is a shared postgres (owner 'platform', shared: true) with four writer services — order-service, portfolio-service, payment-service, settlement-service all hold 'kind':'db' edges to it. This is the channel by which a payment-service change could affect or be co-affected with neighbours, and explains a shared-storage blast radius.
**Contradicting evidence**
  - `F1` ✔ Exactly one deployment occurred anywhere in the 13:30-15:30Z window: dep-6203, payment-service v6.0.0 (author p.nair, 'New UPI mandate flow'), deployed_at 2026-06-01T14:46:00Z, changes include 'new ledger entry type MANDATE_HOLD', status 'rolled_back' at 2026-06-01T15:12:00Z.
**Assumptions**
  - Error rate after 15:12Z is at least partly attributable to the same storage failure rather than a clean recovery delayed by in-flight retries (the bucket data shows sustained 59-64%, which favours persistence)
  - No disk-reclamation intervention occurred between 15:12Z and 16:59Z

### H2: payment-service v6.0.0 mandate flow's new MANDATE_HOLD write pattern amplified ledger-db pressure and triggered the disk exhaustion
The v6.0.0 deploy at 14:46Z introduced a new UPI mandate flow whose MANDATE_HOLD ledger entries (and possibly retry-on-error behaviour) materially increased write volume / WAL generation against ledger-db, pushing an already-nearly-full disk over the edge ~14 minutes later. Without the extra writes the disk would not have filled in this window. The fault origin is the payment-service release, but the visible failure is storage exhaustion on a shared dependency.

**Causal chain**
1. ledger-db disk is already high (90% at 13:10Z) before any deploy — a pre-existing, non-deploy condition [F9]
2. payment-service v6.0.0 (new UPI mandate flow, new MANDATE_HOLD entry type) deploys at 14:46Z — the only change in the entire window [F1, F13, F2, F14]
3. The new mandate flow writes extra/novel ledger entries, accelerating WAL generation and data growth on the already-tight volume *(assumed)*
4. Disk reaches 98% at 14:48Z (~2 min after deploy) and hard-fails at 15:00-15:01Z [F9, F10, F12]
5. payment-service errors at 15:00:26Z with the storage error, and the 14:46Z deploy sits ~14 min before the 15:00Z onset [F8, F7, F13]

**Supporting evidence**
  - `F1` ✔ Exactly one deployment occurred anywhere in the 13:30-15:30Z window: dep-6203, payment-service v6.0.0 (author p.nair, 'New UPI mandate flow'), deployed_at 2026-06-01T14:46:00Z, changes include 'new ledger entry type MANDATE_HOLD', status 'rolled_back' at 2026-06-01T15:12:00Z.
  - `F7` ✔ payment-service error_rate was at baseline ~0.19-0.29% through the 2026-06-01T14:56:00Z bucket, then jumped to avg 60.15% / max 60.8% in the 15:00:00Z bucket and stayed 59-64% for the rest of the window. The departure is therefore at ~15:00Z, not ~15:10-15:15Z as the report's '~15 min after v6.0.0' claim implies (15:10 is only the report time). The 240s bucket means onset is bounded to 14:56-15:00.
  - `F13` ✔ The only change anywhere in 12:00-16:59Z is dep-6203: payment-service v6.0.0 'New UPI mandate flow' (changes include 'new ledger entry type MANDATE_HOLD'), deployed 2026-06-01T14:46:00Z, status 'rolled_back' with rolled_back_at 2026-06-01T15:12:00Z. It is the earliest change preceding the 15:00Z onset, starting ~14 min BEFORE onset, and was rolled back ~12 min after onset.
  - `F14` ✔ No config or feature-flag changes were recorded for ALL services over 12:00-16:59Z (count 0), so the change surface is deploy-only; the v6.0.0 deploy is not accompanied by any flag/config change that could explain the errors.
**Contradicting evidence**
  - `F9` ✔ ledger-db (the actual backing DB; there is no service named 'ledger-service') showed disk pressure BEFORE payment-service's errors: WARN 'disk usage 90% on /var/lib/postgresql/data' at 13:10:01Z, 95% at 14:15:45Z, and 98% at 14:48:20Z, plus repeated inactive replication-slot WAL retention warnings from 13:02:25Z. Hard write failures followed at 15:01:01Z.
  - `F11` ✔ order-service shows the SAME storage error independently, not routed through payment: 236 occurrences of 'INSERT INTO orders failed: No space left on device', first_seen 2026-06-01T15:00:42Z — i.e. it began degrading in the same minute as payment-service, indicating a shared-storage blast radius rather than a payment-v6.0.0-specific bug.
  - `F12` ✔ Ordering of onsets: ledger-db disk-pressure WARNs (13:10Z onward) precede payment-service errors; ledger-db hard PANICs (15:01:01Z) are essentially simultaneous with, not after, payment-service's first error (15:00:26Z) and order-service's first error (15:00:42Z) — the consumers surfaced errors within the same minute the DB ran out of space.
  - `F16` ✔ ledger-db is a shared postgres (owner 'platform', shared: true) with four writer services — order-service, portfolio-service, payment-service, settlement-service all hold 'kind':'db' edges to it. This is the channel by which a payment-service change could affect or be co-affected with neighbours, and explains a shared-storage blast radius.
  - `F15` ✔ No deployment and no config change was EVER recorded for ledger-db: count 0 for deployments and count 0 for config changes over 2026-05-25T00:00Z-2026-06-01T16:59Z. Therefore no DB-side migration or schema change exists in the window to key a 'missing DB migration' or 'new entry type not accepted' mechanism.
**Assumptions**
  - v6.0.0's mandate flow measurably increased ledger-db write volume — no insert-rate or write-volume metric was ever retrieved (explicit GAP)
  - The 98% reading at 14:48Z reflects accelerated fill rather than the same slow trajectory continuing from 13:10Z
  - order-service's simultaneous failure is collateral (contention on the exhausted volume) rather than independent evidence that the trigger is elsewhere

### H3: v6.0.0's new MANDATE_HOLD entry type was rejected by ledger-db schema/enum (or a missing migration), producing ledger write failures
The new ledger entry type MANDATE_HOLD is not accepted by the ledger-db schema or enum because the corresponding schema migration/registration was never deployed; the write path fails on the type itself and payment-service surfaces the failure as 'ledger write failed'. This is the reporter's stated suspicion and the orchestrator's second theory, both of which name ledger-db as the fault site.

**Causal chain**
1. v6.0.0 introduces a new ledger entry type MANDATE_HOLD [F1]
2. No schema migration, enum registration or config change was deployed for ledger-db in the window, so the new value is unregistered [F15, F2, F14]
3. ledger-db rejects the unrecognised entry type, returning an error that payment-service wraps as 'ledger write failed' *(assumed)*
4. payment-service error rate reaches ~60% from the 15:00Z bucket [F7]

**Supporting evidence**
  - `F1` ✔ Exactly one deployment occurred anywhere in the 13:30-15:30Z window: dep-6203, payment-service v6.0.0 (author p.nair, 'New UPI mandate flow'), deployed_at 2026-06-01T14:46:00Z, changes include 'new ledger entry type MANDATE_HOLD', status 'rolled_back' at 2026-06-01T15:12:00Z.
  - `F15` ✔ No deployment and no config change was EVER recorded for ledger-db: count 0 for deployments and count 0 for config changes over 2026-05-25T00:00Z-2026-06-01T16:59Z. Therefore no DB-side migration or schema change exists in the window to key a 'missing DB migration' or 'new entry type not accepted' mechanism.
**Contradicting evidence**
  - `F8` ✔ The dominant payment-service ERROR signature is a downstream ledger write failure wrapping a storage error: 354 occurrences of 'ledger write failed: ERROR: could not extend file: No space left on device', first_seen 2026-06-01T15:00:26Z, level ERROR. No timeout, auth, or app-exception patterns dominate (only 2 GC WARNs, 2 connection-reset WARNs, 1 TLS WARN in the whole set).
  - `F10` ✔ ledger-db's failures are internal storage exhaustion, not downstream dependency failures: 153 PANICs 'could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (first 15:01:01Z) and 139 'could not extend file "base/<n>/<n>": No space left on device' (first 15:01:39Z) — the DB itself ran out of disk for WAL and data files.
  - `F11` ✔ order-service shows the SAME storage error independently, not routed through payment: 236 occurrences of 'INSERT INTO orders failed: No space left on device', first_seen 2026-06-01T15:00:42Z — i.e. it began degrading in the same minute as payment-service, indicating a shared-storage blast radius rather than a payment-v6.0.0-specific bug.
  - `F5` ✔ There is no service named 'ledger-service' in the topology/release system; the only ledger component is the shared datastore ledger-db (known_services include 'ledger-db' and no separate ledger-service). So no ledger-side application deploy could have happened.
  - `F17` ✔ The closest historical precedent for this exact symptom set is HIST-114 (2025-04-18) 'ledger-db disk full from retained WAL' with symptoms 'no space left on device','write failures' and root cause 'Inactive replication slot retained WAL until disk filled' (category resource_exhaustion) — matching the current signature and the T1-reported inactive-replication-slot WAL warnings. No historical incident has 'new ledger entry type' / enum / schema-migration as a root cause in the top matches.
  - `F18` ✔ Historical precedent is predominantly resource/infra on ledger-db (HIST-114 disk full; HIST-110 IOPS throttling; HIST-102 failover) rather than schema/enum issues, and prior payment-service failures (HIST-105) were an external bank partner API outage — so neither 'entry type rejected by schema' nor the reported bank dependency has a recorded precedent matching this incident.
**Assumptions**
  - An unregistered enum value would surface as a storage 'No space left on device' error — which is the opposite of what such a rejection would look like
  - Non-payment writers would also fail for the same reason (they cannot, since they do not write MANDATE_HOLD — yet order-service failed identically)
**Rejected because:** Mechanism mismatch and scope mismatch. The dominant error (354 occurrences) is a storage error — 'could not extend file: No space left on device' — not a type/enum/constraint violation; no app-exception, schema or type error pattern is present (F8). ledger-db's own logs show internal storage exhaustion PANICs (F10). order-service, which never writes MANDATE_HOLD, fails identically and simultaneously (F11), which a type-rejection mechanism cannot explain. No 'new ledger entry type' root cause exists in any historical incident, while the disk-full/WAL precedent matches exactly (F17, F18).

## Facts (verified observations)
- The dominant payment-service error is a downstream storage failure: 354 occurrences of 'ledger write failed: ERROR: could not extend file: No space left on device', first seen 2026-06-01T15:00:26Z; there is no dominant timeout, auth, or app-exception pattern. [F8]
- ledger-db showed disk pressure well before payment-service errors: 90% on /var/lib/postgresql/data at 13:10:01Z, 95% at 14:15:45Z, 98% at 14:48:20Z, plus repeated inactive-replication-slot WAL retention warnings from 13:02:25Z; hard write failures followed at 15:01:01Z. [F9]
- ledger-db's own logs show internal storage exhaustion PANICs: 153 'could not write to file "pg_wal/xlogtemp.<n>": No space left on device' (first 15:01:01Z) and 139 'could not extend file "base/<n>/<n>": No space left on device' (first 15:01:39Z). [F10]
- order-service independently produced 236 occurrences of 'INSERT INTO orders failed: No space left on device' (first_seen 2026-06-01T15:00:42Z), the same storage error in the same minute as payment-service, indicating a shared-storage blast radius rather than a payment-specific bug. [F11]
- ledger-db is a shared postgres (owner platform, shared: true) with four writer services — order-service, portfolio-service, payment-service and settlement-service all hold db edges to it. [F4, F16]
- Exactly one deployment occurred in 12:00-16:59Z: dep-6203 payment-service v6.0.0 'New UPI mandate flow' (changes include 'new ledger entry type MANDATE_HOLD'), deployed 2026-06-01T14:46:00Z, rolled back at 2026-06-01T15:12:00Z — i.e. after the 13:10Z start of disk pressure and ~14 min before the 15:00Z onset. [F1, F13]
- No config or feature-flag changes were recorded for any service in 12:00-16:59Z (count 0); the change surface is deploy-only. [F2, F14]
- No deployment and no config change was ever recorded for ledger-db (count 0 for both over 2026-05-25T00:00Z-2026-06-01T16:59Z), so no DB-side migration or schema change exists in the window. [F15]
- payment-service error rate sat at baseline ~0.19-0.29% through the 14:56:00Z bucket, then jumped to avg 60.15% / max 60.8% in the 15:00:00Z bucket and stayed 59-64% for the rest of the window — including well past the 15:12Z rollback. [F7]
- There is no service named 'ledger-service' in the topology/release system; the only ledger component is the shared datastore ledger-db, so no ledger-side application deploy could have occurred. [F5]
- Historical precedent matches this signature: HIST-114 (2025-04-18) 'ledger-db disk full from retained WAL' with symptoms 'no space left on device' / 'write failures', root cause 'Inactive replication slot retained WAL until disk filled'; no historical incident has a 'new ledger entry type' / enum / schema-migration root cause in the top matches. [F6, F17]

## Assumptions (not verified)
- The inactive-replication-slot WAL retention warnings (from 13:02:25Z) and the disk-pressure WARNs originate from the same mechanism as historical incident HIST-114; the specific slot identity was not directly read in this investigation (log wording matches, slot name unverified).
- The disk-full condition was a genuine exhausted-volume state rather than a quota or permission error, based on the 'No space left on device' message text.
- The 240s error-rate bucket hides a sub-minute onset; the true head of the causal chain is the disk fill, which begins ~2 hours before the error onset.
- The observed errors are the complete picture for the checked services; unexamined components (api-gateway, kafka, redis-cache, bank-partner-api) did not introduce an earlier independent failure.
- (demoted from facts: no verified observation backs it) Ordering of onsets: ledger-db disk-pressure WARNs (13:10Z onward) precede payment-service errors, while ledger-db hard PANICs (15:01:01Z) are essentially simultaneous with payment-service's first error (15:00:26Z) and order-service's first error (15:00:42Z).

## Timeline
- `2026-06-01T13:02:25Z` Repeated inactive-replication-slot WAL retention warnings begin on ledger-db. [F9]
- `2026-06-01T13:10:01Z` ledger-db WARN: disk usage 90% on /var/lib/postgresql/data — the first observable sign of the causal chain, ~1.5h before any deployment. [F9]
- `2026-06-01T14:15:45Z` ledger-db disk usage 95%. [F9]
- `2026-06-01T14:46:00Z` dep-6203 payment-service v6.0.0 'New UPI mandate flow' (new ledger entry type MANDATE_HOLD) deployed — the only change anywhere in the window. [F1, F13]
- `2026-06-01T14:48:20Z` ledger-db disk usage 98%. [F9]
- `2026-06-01T14:56:00Z` payment-service error rate still at baseline (~0.19-0.29%) through the 14:56:00Z bucket; onset therefore falls inside 14:56-15:00Z. [F7]
- `2026-06-01T15:00:26Z` First payment-service error: 'ledger write failed: ERROR: could not extend file: No space left on device' (354 occurrences follow); error rate jumps to ~60.15% for the 15:00:00Z bucket. [F7, F8]
- `2026-06-01T15:00:42Z` order-service independently begins failing: 'INSERT INTO orders failed: No space left on device' (236 occurrences). [F11]
- `2026-06-01T15:01:01Z` ledger-db internal storage exhaustion: 153 PANICs writing pg_wal (xlogtemp) 'No space left on device'. [F10]
- `2026-06-01T15:01:39Z` ledger-db further PANICs extending data files under base/. [F10]
- `2026-06-01T15:10:00Z` Incident INC-012 reported by on-call SRE; rollback of payment-service v6.0.0 initiated. [F1]
- `2026-06-01T15:12:00Z` payment-service v6.0.0 rolled back (dep-6203 status 'rolled_back'). [F1, F13]
- `2026-06-01T16:59:00Z` Latest data: payment-service error rate remains 59-64% — service has not recovered, consistent with the untouched exhausted shared volume. [F7]

## Recommended actions
- **[immediate]** Immediately free space on ledger-db's data volume (priority mitigation): drop/disable the inactive replication slot retaining WAL and run a WAL/space reclamation, then verify disk_used_pct has dropped below ~80% on /var/lib/postgresql/data for the ledger-db component. -- ledger-db itself is PANIC-ing on pg_wal and data-file extension (F10) after crossing 90% at 13:10Z / 98% at 14:48Z (F9); the retained WAL from the inactive replication slot is the mechanism matching HIST-114 (F6, F17). This is the only action that addresses the actual cause. [F9, F10, F17]
- **[immediate]** Treat the payment-service v6.0.0 rollback as non-curative and keep it reverted only for safety; do not expect recovery from it. Re-verify service health after the disk reclamation above. -- v6.0.0 was already rolled back at 15:12Z yet error rate stayed 59-64% to 16:59Z (F7, F13), confirming the fault predates and outlives the deploy (H4). [F7, F13]
- **[short_term]** Add monitoring/alerting on ledger-db capacity: alert at 80% disk_used_pct and on any inactive replication slot retaining WAL for more than N hours; wire disk_used_pct and replication_lag_ms into a dashboard for all four writer services. -- The fill was visible as WARN log lines from 13:10:01Z onward but never triggered a mitigation before hard failure at 15:01Z (F9, F10); the pre-90% trajectory was not tracked by any metric (data gap). [F9, F10]
- **[short_term]** Verify blast radius and recovery across the shared datastore: check order-service, portfolio-service and settlement-service for 'No space left on device' errors and confirm they recover once ledger-db space is reclaimed; also inspect api-gateway, kafka and bank-partner-api for unreviewed impact. -- order-service already showed identical storage failures from 15:00:42Z (F11) and all four writer services share ledger-db (F4, F16); other components were not fully examined. [F4, F11, F16]
- **[long_term]** Confirm whether payment-service v6.0.0's MANDATE_HOLD flow added meaningful ledger-db write volume, and if so re-deploy v6.0.0 only after ledger-db has headroom and a per-client insert-rate/WAL-generation watch is in place. -- No insert-rate or write-volume metric was ever retrieved, so v6.0.0's contribution to fill rate is unquantified; it cannot currently be excluded as a minor accelerator (F1, F13, gap list). [F1, F13]
- **[long_term]** Harden the shared postgres: enforce replication-slot hygiene (auto-drop or alert on inactive slots), set max_slot_wal_keep_size, and add a recurring capacity-review process for ledger-db's data volume. -- HIST-114 was the identical failure mode ('ledger-db disk full from retained WAL', inactive replication slot) on 2025-04-18 (F6, F17); a repeat is evidence of missing preventive control. [F6, F17]

## Data gaps
- Per-component telemetry for ledger-db and kafka (disk_used_pct, write-failure counters, connection/refused errors, IOPS throttling) was never queried, and no metric baseline before 14:46Z was compared — so the disk-fill trajectory rests on log-derived WARN values (90%/95%/98%) rather than an independent metric series.
- No deployment or config history was retrieved for ledger-db, kafka, user-db or redis-cache individually; the 'all services' query for the window returned only dep-6203, but per-component release history is not enumerated.
- No tool call could read ledger-db's schema / ledger_entry_type enum directly, so MANDATE_HOLD's acceptance status cannot be positively confirmed — only that no DB-side migration exists (F15) and no enum/type error pattern appears in logs (F8).
- No write-volume or insert-rate metric per client service was retrieved, so it cannot be quantified whether v6.0.0's MANDATE_HOLD flow increased ledger-db write volume before 15:00Z.
- Onset resolution is bounded only to the 14:56-15:00Z window (240s bucket plus first ERROR log at 15:00:26Z); no finer-grained error-rate point was obtained.
- Blast radius is potentially under-counted: api-gateway, redis-cache, kafka and bank-partner-api were never queried; settlement-service returned 0 WARN/ERROR logs, and only payment-service, order-service and ledger-db were confirmed degrading.
- Tool failures limited coverage: query_logs/get_deployments for 'ledger-service' failed (not_found) and query_metrics(ledger-db, error_rate) failed (not_found), because ledger-db exposes no error_rate metric.

## What would change this conclusion
- Evidence that ledger-db disk usage was NOT the constraint — e.g. a ledger-db disk_used_pct metric showing ample free space at ~15:00Z — would undermine H1 and shift weight to the deploy-driven hypotheses (H2/H3).
- Discovery of a failed or partial schema migration / enum registration for the MANDATE_HOLD ledger entry type would support H3 (currently rejected) despite the storage-error signature.
- Proof that errors are confined to payment-service and that order-service/other writers fail for an unrelated reason would undercut the shared-datastore blast radius and re-open a payment-service-specific cause.
- Recovery without any disk reclamation (e.g. error rate returning to baseline at 15:12Z with no space freed) would contradict H4 and point back to the rollback curing the fault, reviving H2.
- Evidence that the disk fill accelerated sharply starting exactly at deployment time (14:46Z) with a measurable insert-rate spike would promote H2 from coincidence/accelerator to primary trigger.

## Integrity warnings (set by code)
- fact demoted to assumption: Ordering of onsets: ledger-db disk-pressure WARNs (13:10Z onward) precede paymen

## Investigation stats
- latency 145.61s, LLM calls 13, tokens 86759 (in 67356 / out 19403)
- tool calls 24 (4 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
