# Incident investigation report: INC-002
*System: multi_agent | outcome: **inconclusive** | ended: critic_disabled*

## Summary
INC-002 remains inconclusive. The reviewed ranking puts three hypotheses within statistical noise of each other (H2 pool reduction 0.30, H3 shared datastore/cache degradation 0.30, H4 unrecorded event 0.28), all well below the 0.50 support threshold, and the reporter's gateway-HTTP/2 hypothesis is last at 0.12. Two solid facts constrain the search space: the gateway was healthy (p99 ~200ms, 5xx ~0.2%) for the first 1h45m after the 08:00Z deploy and degraded abruptly in the 09:45→09:50 bucket (F8, F9, F10, F15), so the gateway upgrade did not land just before onset; and the portfolio-service pool cut to 5 landed at 09:52Z, 2-5 minutes AFTER the first degraded bucket (F2, F14, F15), so neither recorded change aligns with onset. Critically, no downstream telemetry (portfolio-service, ledger-db, redis-cache) and no gateway ERROR logs could be retrieved (F11, F12), so the decisive test — whether a downstream/shared component degraded before the gateway's 09:50 rise — is unanswered. Recommend safe mitigations plus targeted queries to discriminate between the remaining branches rather than acting on the pooled guess.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H2: portfolio-service datasource pool reduction (50->5) exhausts DB connections | config_change | portfolio-service | cfg-2207 | 0.30 (low) | active |
| 2 | H3: Shared datastore/cache degradation on the portfolio path (ledger-db or redis-cache) | dependency_failure | ledger-db | null | 0.30 (low) | active |
| 3 | H4: Unrecorded/independent change or runtime event at ~09:47Z not in the change inventory | unknown | unknown | null | 0.28 (low) | active |
| 4 | H1: Reporter's hypothesis: gateway HTTP/2-to-upstream upgrade causes upstream stalls | bad_deployment | api-gateway | dep-5220 | 0.12 (low) | active |

### H2: portfolio-service datasource pool reduction (50->5) exhausts DB connections
cfg-2207 reduced portfolio-service db.pool.max_size 50->5 at 09:52Z via the cost-optimisation 'small' profile, mirroring HIST-115 (pool overridden by wrong profile). Threads on the portfolio request path block waiting for connections to ledger-db, so portfolio-service latency balloons and the gateway returns 504s on /v1/portfolio/*.

**Causal chain**
1. cfg-2207 sets portfolio-service db.pool.max_size 50->5 at 09:52Z [F2, F14]
2. portfolio-service threads contend for the tiny pool when talking to ledger-db and block/queue *(assumed)*
3. portfolio-service responses slow past the timeout, gateway emits 504s and p99 rises on /v1/portfolio/* [F8, F9, F4, F16]

**Supporting evidence**
  - `F2` ✔ A second, non-gateway change landed at 09:52:00Z on portfolio-service: db.pool.max_size reduced from 50 to 5 by infra-automation, description 'Apply cost-optimisation profile 'small' to service datasource'. This is ~1h52m after the gateway deploy and 13 minutes before the 10:05Z report.
  - `F14` ✔ The only config change on 2026-03-10 (all services) is cfg-2207 on portfolio-service: db.pool.max_size 50 -> 5 at 09:52:00Z, author infra-automation, 'Apply cost-optimisation profile 'small' to service datasource'. It has no rollback recorded and is the single non-gateway change touching the /v1/portfolio/* path.
  - `F4` ✔ The /v1/portfolio/* fan-out is: api-gateway → portfolio-service; portfolio-service → ledger-db (db), market-data-service (http), redis-cache (cache); market-data-service → redis-cache (cache) and exchange-gateway (http). ledger-db and redis-cache are marked shared=true.
  - `F16` ✔ /v1/portfolio/* topology: api-gateway -> portfolio-service -> {ledger-db (db), market-data-service (http), redis-cache (cache)}; market-data-service -> {redis-cache, exchange-gateway}. Both ledger-db and redis-cache are marked shared=true, so a shared-component fault can produce portfolio-path symptoms independently of the gateway.
  - `F6` ✔ Historical incidents contain precedents for the same symptom from BOTH sides. Gateway-side: HIST-116 (2025-12-18) 'HTTP/2 resets after gateway envoy upgrade', symptoms '504 at gateway','upstream resets','timeouts', root cause 'Envoy upgrade changed HTTP/2 max concurrent streams default', resolution 'Rolled back gateway', match_score 5. Downstream/shared-datastore: HIST-102 (2025-09-02) 'Portfolio timeouts during ledger-db failover', symptoms 'portfolio page timeouts','504 at gateway', root cause 'Unplanned ledger-db primary failover' (match_score 4); HIST-115 (2025-09-25) order-service pool exhaustion from 'Datasource pool size overridden by a wrong environment profile', category config_change.
  - `F18` ✔ Historical precedents exist for this exact symptom pattern from BOTH sides: HIST-116 (2025-12-18) 'HTTP/2 resets after gateway envoy upgrade' with '504 at gateway', 'upstream resets', 'timeouts' from 'Envoy upgrade changed HTTP/2 max concurrent streams default' (match_score 5.0-5.5, bad_deployment); and HIST-102 (2025-09-02) 'Portfolio timeouts during ledger-db failover' with 'portfolio page timeouts','504 at gateway' from an unplanned ledger-db primary failover (match_score 4). HIST-115 is a pool-misconfiguration precedent ('Datasource pool size overridden by a wrong environment profile', category config_change). These are leads only, not evidence of the current cause.
**Contradicting evidence**
  - `F15` ✔ Timing verdict: neither change aligns exactly with the 09:45->09:50 onset bucket. The gateway deploy (08:00:00Z) precedes onset by ~1h47m and the gateway was healthy at p99 ~200ms / 5xx ~0.2% throughout 08:00-09:45, so the gateway upgrade did NOT land just before onset. The portfolio-service pool cut (09:52:00Z) lands ~2-5 min AFTER the first degraded bucket (09:50, p99 avg 1849.99ms / max 4418.76ms), i.e. after symptoms began.
  - `F10` ✔ The gateway was healthy (p99 ~200ms, 5xx ~0.2%) for the first ~1h45m of the requested window (08:00-09:45) despite the gateway upgrade having reportedly shipped 'earlier today'. Degradation starts suddenly at ~09:47-09:50 rather than gradually, consistent with a discrete change or downstream failure at that minute, not a slow build.
**Assumptions**
  - The cost-optimisation profile landed at a time that should postdate the first degraded bucket — so either the recorded 09:52 timestamp is imprecise/backfilled, or the config change is not the trigger
  - portfolio-service thread-pool behavior depends materially on the DB pool size at this request volume

### H3: Shared datastore/cache degradation on the portfolio path (ledger-db or redis-cache)
A shared component reached via the portfolio fan-out — ledger-db (shared: order/payment/settlement/portfolio) or redis-cache (shared: auth/portfolio/market-data) — degraded or saturated at ~09:47Z, and the gateway shows the worst symptoms due to fan-out, producing portfolio-page 504s. Both prior patterns (ledger-db failover HIST-102) exist.

**Causal chain**
1. ledger-db / redis-cache sits on the /v1/portfolio/* fan-out and is shared across services [F4, F5, F16, F17]
2. A shared component began degrading/saturating at ~09:47Z, before or with the gateway onset *(assumed)*
3. portfolio-service and market-data-service calls to the shared component stall, propagating latency to the gateway and producing /v1/portfolio/* 504s [F8, F9, F10]

**Supporting evidence**
  - `F4` ✔ The /v1/portfolio/* fan-out is: api-gateway → portfolio-service; portfolio-service → ledger-db (db), market-data-service (http), redis-cache (cache); market-data-service → redis-cache (cache) and exchange-gateway (http). ledger-db and redis-cache are marked shared=true.
  - `F5` ✔ ledger-db is shared across the portfolio path and other services: order-service → ledger-db, payment-service → ledger-db, portfolio-service → ledger-db; redis-cache is shared by auth-service, portfolio-service and market-data-service.
  - `F16` ✔ /v1/portfolio/* topology: api-gateway -> portfolio-service -> {ledger-db (db), market-data-service (http), redis-cache (cache)}; market-data-service -> {redis-cache, exchange-gateway}. Both ledger-db and redis-cache are marked shared=true, so a shared-component fault can produce portfolio-path symptoms independently of the gateway.
  - `F17` ✔ ledger-db is shared well beyond the portfolio path: order-service, portfolio-service, payment-service and settlement-service all have db edges to ledger-db; api-gateway fronts order-service, portfolio-service and payment-service. A ledger-db fault would therefore precede/degrade symptoms across multiple services, not just the portfolio page.
  - `F10` ✔ The gateway was healthy (p99 ~200ms, 5xx ~0.2%) for the first ~1h45m of the requested window (08:00-09:45) despite the gateway upgrade having reportedly shipped 'earlier today'. Degradation starts suddenly at ~09:47-09:50 rather than gradually, consistent with a discrete change or downstream failure at that minute, not a slow build.
  - `F6` ✔ Historical incidents contain precedents for the same symptom from BOTH sides. Gateway-side: HIST-116 (2025-12-18) 'HTTP/2 resets after gateway envoy upgrade', symptoms '504 at gateway','upstream resets','timeouts', root cause 'Envoy upgrade changed HTTP/2 max concurrent streams default', resolution 'Rolled back gateway', match_score 5. Downstream/shared-datastore: HIST-102 (2025-09-02) 'Portfolio timeouts during ledger-db failover', symptoms 'portfolio page timeouts','504 at gateway', root cause 'Unplanned ledger-db primary failover' (match_score 4); HIST-115 (2025-09-25) order-service pool exhaustion from 'Datasource pool size overridden by a wrong environment profile', category config_change.
  - `F18` ✔ Historical precedents exist for this exact symptom pattern from BOTH sides: HIST-116 (2025-12-18) 'HTTP/2 resets after gateway envoy upgrade' with '504 at gateway', 'upstream resets', 'timeouts' from 'Envoy upgrade changed HTTP/2 max concurrent streams default' (match_score 5.0-5.5, bad_deployment); and HIST-102 (2025-09-02) 'Portfolio timeouts during ledger-db failover' with 'portfolio page timeouts','504 at gateway' from an unplanned ledger-db primary failover (match_score 4). HIST-115 is a pool-misconfiguration precedent ('Datasource pool size overridden by a wrong environment profile', category config_change). These are leads only, not evidence of the current cause.
**Contradicting evidence**
  - (none)
**Assumptions**
  - A downstream/shared component anomaly precedes the gateway's 09:50 onset (no downstream or ledger-db/redis-cache telemetry was obtained — F12, gaps)
  - The single sudden onset at 09:45-09:50 reflects a discrete downstream failure event not present in the recorded change list

### H4: Unrecorded/independent change or runtime event at ~09:47Z not in the change inventory
The sharp, discrete onset at 09:45->09:50 (F8, F9, F10) with no recorded deploy or config change within ~5 minutes aligns to an event absent from the inventory — e.g. an unrecorded runtime change, autoscaling/pool reconfiguration, or a network/egress shift. The gateway is where it surfaces most visibly (highest fan-out), but the origin is unrecorded.

**Causal chain**
1. Gateway is healthy 08:00-09:45 then degrades suddenly in the 09:50 bucket [F8, F9, F10, F15]
2. No recorded deploy/config change exists at or near the 09:45-09:50 onset minute [F1, F2, F3, F14]
3. An unrecorded event at ~09:47Z triggered the degradation *(assumed)*

**Supporting evidence**
  - `F8` ✔ api-gateway p99 latency was flat and healthy (~200-210ms avg, max ≤231ms) from 08:00 through the 09:45 bucket, then jumped to avg 1849.99ms / max 4418.76ms in the 09:50 bucket and stayed elevated at ~4260-4420ms avg (max up to 4711.85ms) through 11:55. This pins the gateway latency onset to the 09:45-09:50 transition (one 5-minute bucket boundary), well AFTER the 08:00 start of the window.
  - `F9` ✔ api-gateway error_rate (HTTP 5xx % of requests) was stable at ~0.17-0.28% from 08:00 through 09:45, then jumped to avg 1.66% / max 7.18% in the 09:50 bucket and held at ~7.0-7.5% through 11:55. The error onset coincides with the same 09:45→09:50 bucket as the latency jump, i.e. 504s/5xx begin in the same interval as the latency rise.
  - `F10` ✔ The gateway was healthy (p99 ~200ms, 5xx ~0.2%) for the first ~1h45m of the requested window (08:00-09:45) despite the gateway upgrade having reportedly shipped 'earlier today'. Degradation starts suddenly at ~09:47-09:50 rather than gradually, consistent with a discrete change or downstream failure at that minute, not a slow build.
  - `F15` ✔ Timing verdict: neither change aligns exactly with the 09:45->09:50 onset bucket. The gateway deploy (08:00:00Z) precedes onset by ~1h47m and the gateway was healthy at p99 ~200ms / 5xx ~0.2% throughout 08:00-09:45, so the gateway upgrade did NOT land just before onset. The portfolio-service pool cut (09:52:00Z) lands ~2-5 min AFTER the first degraded bucket (09:50, p99 avg 1849.99ms / max 4418.76ms), i.e. after symptoms began.
  - `F1` ✔ Exactly one deployment was recorded on 2026-03-10 across all services: api-gateway v9.3.0 at 08:00:00Z, 'Upgrade envoy filter chain; enable HTTP/2 to upstreams', with changes 'envoy 1.29 -> 1.30' and 'upstream protocol h2 for portfolio-service and order-service', author platform-team, status 'live', rolled_back_at null.
  - `F2` ✔ A second, non-gateway change landed at 09:52:00Z on portfolio-service: db.pool.max_size reduced from 50 to 5 by infra-automation, description 'Apply cost-optimisation profile 'small' to service datasource'. This is ~1h52m after the gateway deploy and 13 minutes before the 10:05Z report.
  - `F3` ✔ Only two config changes exist for 2026-03-10 in total: cfg-2199 (order-service log.sampling_rate 0.1→0.2 at 07:00:00Z) and cfg-2207 (portfolio-service db.pool.max_size 50→5 at 09:52:00Z). No other service had a config change.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The change inventory is complete for the window (both sweeps returned only these two changes — F1, F3, F13, F14)

### H1: Reporter's hypothesis: gateway HTTP/2-to-upstream upgrade causes upstream stalls
api-gateway v9.3.0 (dep-5220, 08:00Z) enabled HTTP/2 to portfolio-service/order-service and upgraded envoy 1.29->1.30; new HTTP/2 connection handling (e.g. max-concurrent-streams default change, like HIST-116) causes upstream connection resets/stalls, producing the gateway p99 >4s and 504s on /v1/portfolio/*.

**Causal chain**
1. dep-5220 lands at 08:00Z enabling HTTP/2 to upstreams and upgrading envoy [F1, F13]
2. envoy HTTP/2 handling introduces connection resets/stalls on upstream calls *(assumed)*
3. stalled upstream calls push gateway p99 above 4s and generate 504s on /v1/portfolio/* [F8, F9]

**Supporting evidence**
  - `F1` ✔ Exactly one deployment was recorded on 2026-03-10 across all services: api-gateway v9.3.0 at 08:00:00Z, 'Upgrade envoy filter chain; enable HTTP/2 to upstreams', with changes 'envoy 1.29 -> 1.30' and 'upstream protocol h2 for portfolio-service and order-service', author platform-team, status 'live', rolled_back_at null.
  - `F13` ✔ The only deployment on 2026-03-10 (across all services) is api-gateway v9.3.0 dep-5220 at 08:00:00Z, 'Upgrade envoy filter chain; enable HTTP/2 to upstreams' (envoy 1.29->1.30, h2 for portfolio-service and order-service), status 'live', rolled_back_at null. This lands ~1h45m BEFORE the 09:45->09:50 onset boundary, and a wider sweep from 2026-03-09 onward returns the same single deployment.
  - `F6` ✔ Historical incidents contain precedents for the same symptom from BOTH sides. Gateway-side: HIST-116 (2025-12-18) 'HTTP/2 resets after gateway envoy upgrade', symptoms '504 at gateway','upstream resets','timeouts', root cause 'Envoy upgrade changed HTTP/2 max concurrent streams default', resolution 'Rolled back gateway', match_score 5. Downstream/shared-datastore: HIST-102 (2025-09-02) 'Portfolio timeouts during ledger-db failover', symptoms 'portfolio page timeouts','504 at gateway', root cause 'Unplanned ledger-db primary failover' (match_score 4); HIST-115 (2025-09-25) order-service pool exhaustion from 'Datasource pool size overridden by a wrong environment profile', category config_change.
  - `F18` ✔ Historical precedents exist for this exact symptom pattern from BOTH sides: HIST-116 (2025-12-18) 'HTTP/2 resets after gateway envoy upgrade' with '504 at gateway', 'upstream resets', 'timeouts' from 'Envoy upgrade changed HTTP/2 max concurrent streams default' (match_score 5.0-5.5, bad_deployment); and HIST-102 (2025-09-02) 'Portfolio timeouts during ledger-db failover' with 'portfolio page timeouts','504 at gateway' from an unplanned ledger-db primary failover (match_score 4). HIST-115 is a pool-misconfiguration precedent ('Datasource pool size overridden by a wrong environment profile', category config_change). These are leads only, not evidence of the current cause.
**Contradicting evidence**
  - `F8` ✔ api-gateway p99 latency was flat and healthy (~200-210ms avg, max ≤231ms) from 08:00 through the 09:45 bucket, then jumped to avg 1849.99ms / max 4418.76ms in the 09:50 bucket and stayed elevated at ~4260-4420ms avg (max up to 4711.85ms) through 11:55. This pins the gateway latency onset to the 09:45-09:50 transition (one 5-minute bucket boundary), well AFTER the 08:00 start of the window.
  - `F9` ✔ api-gateway error_rate (HTTP 5xx % of requests) was stable at ~0.17-0.28% from 08:00 through 09:45, then jumped to avg 1.66% / max 7.18% in the 09:50 bucket and held at ~7.0-7.5% through 11:55. The error onset coincides with the same 09:45→09:50 bucket as the latency jump, i.e. 504s/5xx begin in the same interval as the latency rise.
  - `F10` ✔ The gateway was healthy (p99 ~200ms, 5xx ~0.2%) for the first ~1h45m of the requested window (08:00-09:45) despite the gateway upgrade having reportedly shipped 'earlier today'. Degradation starts suddenly at ~09:47-09:50 rather than gradually, consistent with a discrete change or downstream failure at that minute, not a slow build.
  - `F15` ✔ Timing verdict: neither change aligns exactly with the 09:45->09:50 onset bucket. The gateway deploy (08:00:00Z) precedes onset by ~1h47m and the gateway was healthy at p99 ~200ms / 5xx ~0.2% throughout 08:00-09:45, so the gateway upgrade did NOT land just before onset. The portfolio-service pool cut (09:52:00Z) lands ~2-5 min AFTER the first degraded bucket (09:50, p99 avg 1849.99ms / max 4418.76ms), i.e. after symptoms began.
**Assumptions**
  - The envoy HTTP/2 change produces a delayed failure mode that does not manifest for ~1h45m after deploy
  - No other factor intervened at ~09:47Z

## Facts (verified observations)
- Exactly one deployment was recorded on 2026-03-10 across all services: api-gateway v9.3.0 (dep-5220) at 08:00:00Z, 'Upgrade envoy filter chain; enable HTTP/2 to upstreams' (envoy 1.29->1.30, h2 for portfolio-service and order-service), status 'live', rolled_back_at null; a wider sweep from 2026-03-09 returns the same single deployment. [F1, F13]
- The only config change on 2026-03-10 is cfg-2207 on portfolio-service: db.pool.max_size 50 -> 5 at 09:52:00Z, author infra-automation, 'Apply cost-optimisation profile 'small' to service datasource', with no rollback recorded; it is the single non-gateway change touching the /v1/portfolio/* path. [F2, F14, F3]
- api-gateway p99 latency was flat and healthy (~200-210ms avg, max <=231ms) from 08:00 through the 09:45 bucket, then jumped to avg 1849.99ms / max 4418.76ms in the 09:50 bucket and stayed elevated (~4260-4420ms avg, max up to 4711.85ms) through 11:55, pinning onset to the 09:45-09:50 transition. [F8]
- api-gateway error_rate was stable at ~0.17-0.28% from 08:00 through 09:45, then jumped to avg 1.66% / max 7.18% in the 09:50 bucket and held at ~7.0-7.5% through 11:55, coinciding with the latency jump. [F9]
- The /v1/portfolio/* fan-out is api-gateway -> portfolio-service -> {ledger-db (db), market-data-service (http), redis-cache (cache)}, with market-data-service -> {redis-cache, exchange-gateway}; both ledger-db and redis-cache are marked shared=true. [F4, F16]
- ledger-db is shared well beyond the portfolio path (order-service, portfolio-service, payment-service, settlement-service all hold db edges to it), so a ledger-db fault would degrade symptoms across multiple services, not just the portfolio page. [F5, F17]
- Historical incidents show precedents for the same symptom pattern from BOTH sides: HIST-116 (gateway envoy HTTP/2 resets after upgrade, match_score 5) and HIST-102 (portfolio timeouts during ledger-db failover, match_score 4); HIST-115 is a datasource-pool-misconfiguration precedent. These are leads only, not evidence about INC-002. [F6, F18]
- Neither change touching the portfolio path has a recorded rollback (dep-5220 rolled_back_at null; cfg-2207 no rollback), so rollback behaviour could not be used as causal evidence. [F14]
- query_logs for api-gateway over 08:00-11:59 at min_level=ERROR returned 0 matches despite ~7% 5xx, so no gateway error signatures (upstream 5xx vs gateway-generated 504, connection resets, pool exhaustion) could be extracted. [F11]
- All five downstream metric queries were issued under wrong service names ('portfolio', 'pricing', 'market-data', 'positions') and failed with not_found; no downstream service was confirmed healthy or degraded, so blast radius and first-degrading-service remain unknown. [F12]

## Assumptions (not verified)
- Assumption: the recorded change inventory (one deployment at 08:00Z, one config change at 09:52Z) is complete for the window; H4 rests on this and both sweeps returned only these events (F1, F3, F13, F14).
- Assumption: the recorded cfg-2207 timestamp of 09:52Z is precise and not backfilled; if it is imprecise, the pool reduction could in principle align with the 09:45->09:50 onset.
- Assumption: portfolio-service thread-pool behaviour depends materially on the DB pool size at this request volume (required for H2 to explain the latency balloon).
- Assumption: a downstream/shared-component anomaly (ledger-db or redis-cache) precedes the gateway's 09:50 onset; no downstream or shared-component telemetry was obtained, so this is unverified.
- Assumption: the single sharp onset at 09:45-09:50 reflects a discrete event (change, downstream failure, or runtime reconfiguration) rather than a gradual build.
- Assumption: the envoy HTTP/2 change would have to exhibit a delayed failure mode (~1h45m after deploy) to be the cause; no such mechanism is documented in the available evidence.
- Assumption: 5xx observed at the gateway are a mix of gateway-generated 504s and upstream failures; the split could not be determined without logs.
- (demoted from facts: no verified observation backs it) Timing verdict: the gateway deploy (08:00Z) precedes onset by ~1h47m with the gateway healthy throughout, and the portfolio-service pool cut (09:52Z) lands 2-5 minutes AFTER the first degraded bucket (09:50) — neither recorded change aligns exactly with the 09:45->09:50 onset.

## Timeline
- `2026-03-10T07:00:00Z` Config change cfg-2199 on order-service (log.sampling_rate 0.1->0.2) — off the portfolio path. [F3]
- `2026-03-10T08:00:00Z` api-gateway v9.3.0 (dep-5220) deployed: envoy 1.29->1.30, HTTP/2 enabled to portfolio-service and order-service; status live, no rollback. [F1, F13]
- `2026-03-10T08:00:00Z-09:45:00Z` Gateway remains healthy (p99 ~200-210ms, error_rate ~0.17-0.28%) for ~1h45m after the deploy, despite the upgrade having shipped earlier. [F8, F9, F10]
- `2026-03-10T09:45:00Z-09:50:00Z` Abrupt onset bucket: gateway p99 jumps to avg 1849.99ms / max 4418.76ms and error_rate to avg 1.66% / max 7.18%; onset pinned to this single 5-minute bucket boundary. [F8, F9, F10]
- `2026-03-10T09:52:00Z` Config change cfg-2207 lands on portfolio-service: db.pool.max_size 50 -> 5 via the cost-optimisation 'small' profile — 2-5 minutes AFTER the first degraded bucket. [F2, F14, F15]
- `2026-03-10T10:05:00Z` Incident INC-002 reported: portfolio page spinning/timing out, gateway p99 >4s, 504s on /v1/portfolio/*. [F7]
- `2026-03-10T09:50:00Z-11:55:00Z` Gateway stays elevated: p99 ~4260-4420ms avg (max up to 4711.85ms), error_rate ~7.0-7.5%; neither change was rolled back. [F8, F9, F7]

## Recommended actions
- **[immediate]** Do NOT roll back anything yet. Before changing state, capture a 30-minute point-in-time snapshot of gateway and portfolio-service metrics plus a fine-grained (<=60s) view of the 09:40-09:55 window to confirm the onset ordering; a rollback now would destroy the ability to discriminate between H2/H3/H4. -- Neither recorded change aligns with onset (F15), and the gateway was healthy for 1h45m after dep-5220 (F8, F9, F10); a blind rollback of either change would be an uninformative action and could mask the real trigger. [F15, F8, F9, F10]
- **[immediate]** Query per-service latency/error/saturation metrics under the exact known service names for portfolio-service, ledger-db and redis-cache over 09:30-10:00Z (e.g. latency_p99_ms, error_rate, db connection pool in-use / active connections, redis latency and evictions) and compare each series' first-degraded timestamp against the gateway's 09:45-09:50 rise. -- This is the decisive test between H3 (shared component degraded first -> gateway is victim) and the gateway-path hypotheses; it was never run because the earlier queries used wrong service names (F12) and is the key unanswered question in the gap list. [F12, F4, F5, F16, F17]
- **[immediate]** Re-run api-gateway log queries without the ERROR-level filter (all levels, plus upstream-cluster-specific filters) and search for '504', 'upstream', 'connection reset', 'GOAWAY', 'REFUSED_STREAM', 'timeout' and pool-exhaustion strings over 09:40-10:10Z. -- The ERROR-level log stream returned zero entries despite ~7% 5xx (F11), so the split between gateway-generated 504s (upstream stall, H1/H2) and upstream-returned 5xx (downstream fault, H3) is currently unknowable. [F11, F9]
- **[short_term]** If the portfolio-service DB pool is implicated, verify empirically before re-applying anything: check portfolio-service concurrency/thread-pool saturation and ledger-db connection counts around 09:45-09:50Z, and confirm whether the 'small' cost-optimisation profile (cfg-2207) is correct for this service's request volume. -- cfg-2207 is the only config change on the portfolio path and mirrors the HIST-115 pool-override precedent (F2, F6, F14), but it landed after the first degraded bucket (F15), so its causal role is unproven and must be measured, not assumed. [F2, F14, F6, F15]
- **[short_term]** If fine-grained data shows the gateway onset genuinely precedes any downstream anomaly, roll back api-gateway to the pre-v9.3.0 envoy configuration (disable HTTP/2 to upstreams) as a controlled A/B test, observing p99 and 5xx for two consecutive 5-minute buckets before and after. -- HIST-116 is a direct precedent for envoy HTTP/2 upgrade causing 504s and upstream resets, and rollback was its resolution (F6, F18); this is only justified if the fine-grained ordering excludes a downstream trigger, since the coarse data currently contradicts a 08:00 deploy causing a 09:50 onset (F8, F9, F10). [F6, F18, F8, F9, F10, F1]
- **[long_term]** Harden change-management and observability controls: require a documented, timestamped metric baseline for datasource-pool/profile changes (cfg-2207) before rollout, add a gateway log pipeline that actually emits error-level entries during 5xx, and add alerting on exact service names (portfolio-service, ledger-db, redis-cache) so downstream blast-radius queries work during an incident. -- The zero-match gateway ERROR log (F11), the wrongly-named downstream queries (F12), and the unaligned config-change timestamp (F15) are what forced this incident to remain inconclusive; HIST-115 shows the pool-profile override failure mode is a recurring pattern (F6). [F11, F12, F6, F15]

## Data gaps
- No runtime metric samples were retrieved for api-gateway, portfolio-service, ledger-db or redis-cache around 08:00-10:05Z, so no change can be shown to correlate with the p99/504 onset.
- No per-service telemetry was obtained for portfolio-service, pricing-engine, market-data-service, auth-service, ledger-db, redis-cache or user-db — the downstream blast-radius queries were issued under wrong service names and returned not_found (F12).
- The api-gateway ERROR log stream returned zero entries despite ~7% 5xx (F11), so gateway error signatures (upstream 5xx vs gateway-generated 504, connection resets, pool exhaustion) are unavailable.
- Both onset signals fall in the same 300s bucket (09:45->09:50), so the intra-bucket ordering of the latency rise vs the 504 rise cannot be resolved.
- No ledger-db or redis-cache latency/error/connection/saturation data was obtained for 09:30-10:00Z, so it cannot be determined whether a shared datastore degraded before the gateway's 09:50 onset — the key test for the downstream-trigger branch.
- No deployment or config event exists within ~5 minutes of onset, so the trigger (if any) is either unrecorded or an unmonitored runtime/downstream event.
- No rollback events were observed for any service in the window, so rollback/roll-forward behaviour could not be used as causal evidence.

## What would change this conclusion
- Fine-grained (<=60s) metrics showing the gateway p99/5xx rise in the 09:45-09:50 bucket strictly precedes any portfolio-service, ledger-db or redis-cache anomaly would rule out H3's ordering assumption and shift weight toward the gateway-path hypotheses (H1/H2).
- Fine-grained metrics or logs showing portfolio-service DB connection-pool exhaustion, thread-pool saturation or a ledger-db/redis-cache anomaly at ~09:45-09:50 (before the gateway rise) would confirm H3 and make H2's pool cut a non-cause of onset.
- Evidence that cfg-2207's recorded 09:52Z timestamp is imprecise or backfilled (e.g. it actually applied at ~09:47Z) would let H2 reconcile with onset and raise its support.
- Gateway logs showing HTTP/2-specific upstream errors (GOAWAY, REFUSED_STREAM, connection resets) appearing at ~09:45, or a clean recovery on gateway-only rollback, would revive H1 despite its current low confidence.
- Discovery of an unrecorded change or runtime event (autoscaling event, pool reconfiguration, network/egress shift) at ~09:47Z would confirm H4 and explain the absence of an aligning recorded change.
- A successful controlled rollback (either cfg-2207 or dep-5220) with immediate symptom recovery would directly implicate that change and overturn the inconclusive verdict.

## Integrity warnings (set by code)
- critic disabled (ablation); analysis unreviewed, confidence capped at 0.6
- fact demoted to assumption: Timing verdict: the gateway deploy (08:00Z) precedes onset by ~1h47m with the ga

## Investigation stats
- latency 128.9s, LLM calls 13, tokens 78959 (in 62595 / out 16364)
- tool calls 24 (5 failed, 0 partial, 0 recovered by retry), findings 18/18 verified
