# Incident investigation report: INC-012
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
Root cause is NOT the payment-service v6.0.0 release. The shared ledger-db (Postgres) filled its data volume: disk_used_pct climbed steadily from 78.6% at 11:00 (before any deploy) to 100% at ~15:00, coincident with WARN logs about an inactive replication slot 'analytics_cdc' retaining 33–93 GB of WAL since 12:19. Once the volume hit 100%, ledger-db could no longer extend files, so ALL writers to it failed with "No space left on device" at 15:00–15:01 — payment-service (ledger writes) AND order-service (orders) simultaneously. payment-service's spike to 60-70% error rate is collateral damage from its shared datastore. The v6.0.0 deploy (14:46) and its rollback (15:12) are red herrings: disk was already at 97.9% before the deploy, bank_api_error_rate is flat, and error rate stayed at ~62% for 1h45m after the rollback. This matches HIST-114 exactly. category=resource_exhaustion, root_cause_service=ledger-db.

## Probable root cause
**ledger-db disk exhaustion from inactive replication slot retaining WAL** -- resource_exhaustion in `ledger-db` (confidence 0.90, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: ledger-db disk exhaustion from inactive replication slot retaining WAL | resource_exhaustion | ledger-db | - | 0.90 (high) | active |
| 2 | H2: payment-service v6.0.0 mandate flow broke ledger writes | bad_deployment | payment-service | dep-6203 | 0.05 (low) | rejected |
| 3 | H3: bank-partner-api (external dependency) degradation | dependency_failure | bank-partner-api | - | 0.02 (low) | rejected |
| 4 | H4: ledger-db connection pool exhaustion | capacity_saturation | ledger-db | - | 0.02 (low) | rejected |

### H1: ledger-db disk exhaustion from inactive replication slot retaining WAL
ledger-db's data volume (shared Postgres) filled with WAL retained by the inactive replication slot 'analytics_cdc' from ~12:19 onward. Disk reached 100% at ~15:00, and Postgres could no longer extend files, so all writers failed with 'No space left on device' — including payment-service ledger writes and order-service order inserts. payment-service v6.0.0 is a coincidental red herring.

**Causal chain**
1. Replication slot 'analytics_cdc' goes inactive ~12:19, retaining tens of GB of WAL [F4, F10]
2. ledger-db disk_used_pct climbs 78.6% -> 100% between 11:00 and 15:00 [F3, F4]
3. Disk at 100% -> Postgres cannot extend files / write WAL [F4, F3]
4. All shared-datastore writers fail: payment-service ledger writes and order-service inserts at ~15:00 [F2, F5, F15]
5. payment-service error rate spikes to 60-70% and stays high after rollback [F1, F14]

**Supporting evidence**
  - `F1` ✔ payment-service error rate was normal (~0.2%) through 14:45 and jumped to avg 20.22% (max 60.8%) in the 14:54 bucket, then held ~60-70% through the end of data (16:51 last bucket avg 61.47%).
  - `F2` ✔ payment-service WARN+ logs from 15:00:26 onward are overwhelmingly ERROR 'ledger write failed: ERROR: could not extend file: No space left on device' (354 of 358 matches).
  - `F3` ✔ ledger-db disk_used_pct rose monotonically from 78.63% at 11:00 to 99.39% at 14:45, reached 100.0 at 15:00 and stayed pinned at 100 through 16:45; it was already 97.9% at 14:30, before the 14:46 deploy.
  - `F4` ✔ ledger-db logs show repeated WARN 'replication slot \'analytics_cdc\' is inactive; retaining <N> MB of WAL' from 12:19:34 onward, with disk-usage warnings rising 90% (13:10) -> 95% (14:15) -> 98% (14:48), then PANIC/could-not-write errors with 'No space left on device' starting 15:01:01.
  - `F5` ✔ order-service — an independent service that only shares ledger-db — shows 236 ERRORs 'INSERT INTO orders failed: No space left on device', first at 15:00:42, i.e. the same ~15:00 onset as payment-service, despite having no deploy of its own.
  - `F13` ✔ ledger-db is shared by order-service, portfolio-service, payment-service and settlement-service.
  - `F14` ✔ Error rate remained ~62% for ~1h45m after the 15:12 rollback of v6.0.0, so the rollback did not recover the incident — the faulty change was not in payment-service code.
  - `F15` ✔ Because order-service (no deploy) failed at the same second-minute and with the same disk error as payment-service, the fault originates in the shared ledger-db, not in payment-service v6.0.0; payment-service is the reported victim.
  - `F10` ✔ ledger-db replication_lag_ms rose from ~926ms at 11:00 to ~5.2s at 16:45, consistent with an inactive/falling-behind slot retaining WAL.
  - `F11` ✔ Historical incident HIST-114 (2025-04-18, ledger-db) has identical symptoms 'no space left on device' / 'write failures' with root cause 'Inactive replication slot retained WAL until disk filled' (resource_exhaustion).
  - `F12` ✔ payment-service dependency graph: it writes to shared ledger-db and calls bank-partner-api, and its only caller is api-gateway.
**Contradicting evidence**
  - (none)
**Assumptions**
  - Disk growth was driven predominantly by retained WAL (slot messages are frequent but quantify MB, not GB; not proven to be the sole consumer).

### H2: payment-service v6.0.0 mandate flow broke ledger writes
The new mandate flow / MANDATE_HOLD ledger entry type in v6.0.0 caused ledger write failures that propagate to a 60% payment error rate.

**Causal chain**
(none given)

**Supporting evidence**
  - `F6` ✔ Only one deployment exists across all services in the window: dep-6203 payment-service v6.0.0 at 14:46:00 ('New UPI mandate flow', 'new ledger entry type MANDATE_HOLD'), rolled_back at 15:12:00; ledger-db had zero deployments.
**Contradicting evidence**
  - `F3` ✔ ledger-db disk_used_pct rose monotonically from 78.63% at 11:00 to 99.39% at 14:45, reached 100.0 at 15:00 and stayed pinned at 100 through 16:45; it was already 97.9% at 14:30, before the 14:46 deploy.
  - `F5` ✔ order-service — an independent service that only shares ledger-db — shows 236 ERRORs 'INSERT INTO orders failed: No space left on device', first at 15:00:42, i.e. the same ~15:00 onset as payment-service, despite having no deploy of its own.
  - `F8` ✔ payment-service bank_api_error_rate was flat at ~0.20% throughout 14:00-16:56, and request_rate stayed ~100 req/s — no upstream bank/partner degradation and no traffic surge.
  - `F14` ✔ Error rate remained ~62% for ~1h45m after the 15:12 rollback of v6.0.0, so the rollback did not recover the incident — the faulty change was not in payment-service code.
  - `F15` ✔ Because order-service (no deploy) failed at the same second-minute and with the same disk error as payment-service, the fault originates in the shared ledger-db, not in payment-service v6.0.0; payment-service is the reported victim.
**Rejected because:** Disk was already 97.9% at 14:30 before the 14:46 deploy; order-service (no deploy) failed identically at 15:00:42; rolling back v6.0.0 at 15:12 left error rate at ~62% for 1h45m. The error is a disk-space error, not a mandate/entry-type validation error.

### H3: bank-partner-api (external dependency) degradation
The external bank partner API degraded/unavailable, causing payment failures.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F8` ✔ payment-service bank_api_error_rate was flat at ~0.20% throughout 14:00-16:56, and request_rate stayed ~100 req/s — no upstream bank/partner degradation and no traffic surge.
  - `F1` ✔ payment-service error rate was normal (~0.2%) through 14:45 and jumped to avg 20.22% (max 60.8%) in the 14:54 bucket, then held ~60-70% through the end of data (16:51 last bucket avg 61.47%).
  - `F2` ✔ payment-service WARN+ logs from 15:00:26 onward are overwhelmingly ERROR 'ledger write failed: ERROR: could not extend file: No space left on device' (354 of 358 matches).
**Rejected because:** bank_api_error_rate is flat at ~0.20% throughout; errors reference ledger file extension, not bank calls; order-service is unaffected by any bank dependency yet failed.

### H4: ledger-db connection pool exhaustion
ledger-db ran out of available connections, blocking writes.

**Causal chain**
(none given)

**Supporting evidence**
  - (none)
**Contradicting evidence**
  - `F9` ✔ ledger-db connections_active stayed flat ~54-72 across the whole window; no connection-pool exhaustion.
  - `F2` ✔ payment-service WARN+ logs from 15:00:26 onward are overwhelmingly ERROR 'ledger write failed: ERROR: could not extend file: No space left on device' (354 of 358 matches).
**Rejected because:** connections_active stayed flat (~62 avg) and the failing error is explicitly 'No space left on device', not a pool/timeout error.

## Facts (verified observations)
- payment-service error rate jumped from ~0.2% to 60-70% starting in the 14:54 bucket and stayed elevated through the end of data. [F1]
- payment-service ledger writes failed with 'could not extend file: No space left on device' from 15:00:26. [F2]
- ledger-db disk_used_pct rose from 78.63% (11:00) to 100% (15:00) and stayed at 100%; already 97.9% at 14:30, before the deploy. [F3]
- ledger-db logged an inactive replication slot 'analytics_cdc' retaining 33–93 GB of WAL from 12:19, and hit disk-usage 90/95/98% warnings before PANIC 'No space left on device' at 15:01. [F4]
- order-service, which has no deploy and only shares ledger-db, also failed with 'No space left on device' from 15:00:42. [F5]
- The only deployment was dep-6203 payment-service v6.0.0 at 14:46, rolled back at 15:12; ledger-db had no deployments, and there were no config changes. [F6, F7]
- payment-service bank_api_error_rate stayed flat ~0.20% and request_rate ~100 req/s. [F8]
- ledger-db connections_active stayed flat (~54-72). [F9]
- ledger-db replication_lag_ms climbed from ~0.9s to ~5.2s over the window. [F10]
- ledger-db is a shared Postgres used by order-service, portfolio-service, payment-service and settlement-service. [F13]
- Historical incident HIST-114 had the same symptom and root cause (inactive replication slot retained WAL until disk filled). [F11]

## Assumptions (not verified)
- The inactive replication slot 'analytics_cdc' (and its retained WAL) is the primary consumer of the ledger-db volume; the slot messages quantify retained MB but the exact GB share of total disk use is not directly shown.
- The huge per-message 'retaining <N> MB' values are WAL retained by that slot, consistent with the disk growth trend.

## Timeline
- `2026-06-01T11:00:00Z` ledger-db disk_used_pct already 78.63% and rising (pre-incident baseline drift). [F3]
- `2026-06-01T12:19:34Z` First WARN: replication slot 'analytics_cdc' inactive, retaining 63157 MB of WAL. [F4]
- `2026-06-01T13:10:01Z` ledger-db disk usage 90% warning. [F4]
- `2026-06-01T14:15:45Z` ledger-db disk usage 95% warning. [F4]
- `2026-06-01T14:30:00Z` ledger-db disk 97.9% — before the payment-service deploy. [F3]
- `2026-06-01T14:46:00Z` dep-6203 payment-service v6.0.0 (new UPI mandate flow, MANDATE_HOLD ledger entry) deployed. [F6]
- `2026-06-01T14:48:20Z` ledger-db disk usage 98% warning. [F4]
- `2026-06-01T14:54:00Z` payment-service error rate bucket spikes to avg 20.22% (max 60.8%). [F1]
- `2026-06-01T15:00:00Z` ledger-db disk_used_pct reaches 100%. [F3]
- `2026-06-01T15:00:26Z` First payment-service 'ledger write failed: No space left on device'. [F2]
- `2026-06-01T15:00:42Z` First order-service 'INSERT INTO orders failed: No space left on device'. [F5]
- `2026-06-01T15:01:01Z` First ledger-db PANIC 'could not write to file pg_wal/xlogtemp...: No space left on device'. [F4]
- `2026-06-01T15:10:00Z` Incident reported (IN-012). []
- `2026-06-01T15:12:00Z` v6.0.0 rolled back — but error rate stays ~62% afterwards. [F6, F14]
- `2026-06-01T16:59:00Z` Latest data: ledger-db still at 100% disk, payment-service error rate still ~62%. [F3, F1]

## Recommended actions
- **[immediate]** Immediately free space on ledger-db: drop/inactivate the orphaned replication slot 'analytics_cdc' (after confirming the consumer is truly gone) and/or expand the volume, then verify disk drops below 100% and writes resume. -- Volume is at 100% and all shared-datastore writers (payment-service, order-service) are failing; freeing the retained WAL is the direct remediation. [F3, F4, F5, F2]
- **[immediate]** Re-enable/restore the v6.0.0 deploy if it was rolled back solely on the assumption it caused the outage; the rollback did not fix anything and may have removed an intended feature. -- Rollback at 15:12 did not change the ~62% error rate; the deploy was not the cause. [F6, F14]
- **[short_term]** Add alerting on ledger-db disk_used_pct (warn >85%, page >92%) and on inactive/behind replication slots (retained WAL), as recommended in HIST-114. -- Disk was visibly climbing for 4 hours with no page; the same failure mode occurred in HIST-114. [F3, F4, F11]
- **[short_term]** Audit replication slot lifecycle: any slot with no active consumer should be dropped automatically or alerted within minutes. -- A single inactive slot retained tens of GB of WAL and took down a shared database. [F4, F10]
- **[long_term]** Reduce blast radius by decoupling non-critical services from the highly-shared ledger-db and reviewing volume headroom / autogrowth policies. -- A shared Postgres amplified a disk issue into a multi-service, SEV-1 outage. [F13, F5]

## Data gaps
- No filesystem-level breakdown (WAL vs tables vs temp files) to quantify exactly how much space the retained 'analytics_cdc' WAL consumed versus other growth; the slot messages report retained MB but not the total.
- No ledger-db restart/failover logs or evidence of whether the DB recovered automatically after space was freed (volume still 100% at end of data, so recovery was never reached).
- No logs from the slot's intended consumer (e.g. analytics/CDC pipeline) to confirm the slot is genuinely orphaned versus merely lagging.
- settlement-service and portfolio-service showed 0 ERRORs in the window, but these are complete (non-truncated) results only within their reported data range; they may write less frequently and simply had no failing writes yet.

## Investigation stats
- latency 56.88s, LLM calls 5, tokens 60730 (in 52579 / out 8151)
- tool calls 19 (2 failed, 0 partial, 0 recovered by retry), findings 15/15 verified
