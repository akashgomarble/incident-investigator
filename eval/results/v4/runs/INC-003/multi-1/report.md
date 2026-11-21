# Incident investigation report: INC-003
*System: multi_agent | outcome: **inconclusive** | ended: critic_inconclusive*

## Summary
The evidence cannot separate the two live hypotheses, so this is genuinely inconclusive. H1 (external partner api.exchange-partner.example degraded) remains the leading hypothesis at 0.60 confidence: exchange-gateway's connect timeouts to the partner host begin at 11:01:25Z, ~80s before order-service's first 504, the gateway's p99 pins at its 5000ms timeout bound, its upstream_error_rate to the partner holds ~45%, every order-service error is a downstream exchange-gateway 504 (placeOrder), and the v2.15.2 rollback produced zero recovery while the gateway stayed broken. H5 (exchange-gateway's own egress/DNS path broken) at 0.16 produces byte-identical internal telemetry, and the two imply different remediations (partner failover/secondary route vs network/gateway repair). The decisive discriminators were unobtainable: all infra/network metric queries failed with budget_exhausted, no second direct vantage point to the partner host exists, and no partner status/notice source is retrievable. H2, H3 and H4 are rejected by direct measurement.

## Probable root cause
**Inconclusive** -- the evidence does not single out one cause. See ranked hypotheses and data gaps.

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: External partner api.exchange-partner.example degraded → exchange-gateway 504s → order-service submit failures | dependency_failure | external exchange partner API api.exchange-partner.example (called via exchange-gateway) | null | 0.60 (medium) | active |
| 2 | H5: exchange-gateway-side egress failure (routing/NAT/packet-loss path to the partner host) — not a partner outage | infrastructure_failure | exchange-gateway egress/network path to api.exchange-partner.example | null | 0.16 (low) | active |
| 3 | H3: Shared data layer (ledger-db / redis-cache / kafka) as a second contributing factor to the residual ~30% | infrastructure_failure | ledger-db / redis-cache / kafka (shared multi-tenant components) | null | 0.03 (low) | rejected |
| 4 | H2: order-service v2.15.2 idempotency/retry code introduced the submit failures | bad_deployment | order-service | dep-5333 | 0.02 (low) | rejected |
| 5 | H4: pricing-engine cache TTL config change (cfg-2290) as the trigger | config_change | pricing-engine | cfg-2290 | 0.01 (low) | rejected |

### H1: External partner api.exchange-partner.example degraded → exchange-gateway 504s → order-service submit failures
The external partner API api.exchange-partner.example (called by exchange-gateway for placeOrder/quoteSnapshot) began failing at ~11:01:25Z, producing 5s connect-phase timeouts, a ~45% upstream_error_rate and an opened circuit breaker. exchange-gateway therefore returns 504/503 to its callers, and order-service's submit path — which synchronously calls exchange-gateway — fails ~30% of the time. order-service is a victim; v2.15.2 is coincidental and its rollback correctly did not help. Remaining open detail: whether the failing element is the partner's edge/backend or the last-mile path from the gateway (see H5).

**Causal chain**
1. The external partner host becomes unreachable/slow for placeOrder and quoteSnapshot, producing connect-phase timeouts bounded at the configured 5000ms from 11:01:25Z [F10, F32, F33, F35, F36]
2. Some requests still reach the partner edge and get an HTTP 503 carrying x-partner-request-id, showing a functioning local egress path for those calls — i.e. the partner edge itself is answering with errors [F33, F47]
3. exchange-gateway error_rate spikes to ~43%, p99 pins at the 5000ms timeout, and its upstream_error_rate to the partner stays ~45%; circuit breaker opens 11:03:26Z [F11, F17, F18, F10]
4. order-service, which calls exchange-gateway synchronously on submit, logs only 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' from 11:02:45Z [F9, F8, F16]
5. order-service error_rate settles at ~30% and its rollback at 11:16:38Z produces no recovery while the gateway stays broken at ~45% [F14, F15, F13]
6. Other dependents of exchange-gateway (market-data-service) show concurrent, smaller failures with no same-signature error of their own, confirming a shared upstream problem rather than an order-service defect; their degradation begins after the gateway's [F19, F38, F39, F43, F5]
7. No config/flag change exists anywhere on the partner path (gateway, order-service, api-gateway, market-data-service) in-window or in the two weeks prior, and the pre-release window is clean (flat gateway p99 115–139ms to 10:50Z), so the origin is the partner path, not a release or a flag flip [F21, F22, F23, F24, F12, F2]
8. Historical precedent supports this class: HIST-103 (2025-10-21) 'Order failures during exchange partner outage' had identical symptoms (504 on order submit, upstream timeouts) and root cause 'External exchange API outage on the partner side', resolved by partner failover with no order-service rollback fix [F52, F6, F7]

**Supporting evidence**
  - `F8` ✔ order-service error_rate jumped from a ~0.2% baseline (08:00–10:49 buckets, max 0.32) to 30.81% in the 11:02 bucket and stayed ~30–31% through 12:59. The last clean bucket is 10:49 (avg 0.21).
  - `F9` ✔ The first order-service ERROR is at 11:02:45Z with signature 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; all 349 matched errors are this one pattern, i.e. 504s returned from the exchange-gateway call, not local application exceptions.
  - `F10` ✔ exchange-gateway shows the EARLIEST failure: connect timeouts to the external partner start at 11:01:25Z, ~80s before order-service's first 504. Signature: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163 occurrences), plus quoteSnapshot timeouts (146) and 503s from exchange-api. A circuit breaker opened at 11:03:26Z.
  - `F11` ✔ exchange-gateway error_rate rose to 43.16% in the 11:02 bucket (baseline ~0.2%, max 0.32 through 10:36). The 10:49 bucket already shows avg 3.69 / max 45.46, consistent with onset at ~11:01 within that 13-min bucket.
  - `F12` ✔ No anomaly exists in the pre-release window for exchange-gateway: at 1-minute resolution, latency p99 was flat 115.5–138.7ms continuously from 10:30 to 10:50Z, showing no degradation or saturation before the release.
  - `F13` ✔ order-service v2.15.2 rollout completed at 10:48:02Z ('deployment dep-5333 (v2.15.2) rollout complete, all pods ready'), i.e. ~13.5 minutes before the first order-service 504 at 11:02:45Z; order-service logged 'connection pool stats: healthy' at 10:47:04 and 'health check ok' at 11:03:31.
  - `F14` ✔ The order-service v2.15.2 rollback completed at 11:16:38Z ('rollback of dep-5333 complete, previous version restored'), but the order-service error_rate did NOT recover: it was 31.18% in the 11:16 bucket and remained ~30–32% through 12:58 (last bucket avg 30.46, max 30.54).
  - `F15` ✔ exchange-gateway's error_rate also shows no post-rollback recovery: 43.66% in the 11:16 bucket and still 45.13% at 12:58 (max 46.67), i.e. the dependency stayed broken independently of the order-service version.
  - `F16` ✔ The failing order-service errors are downstream timeouts, not application exceptions: 310 ERROR logs between 11:16:05Z and 12:58:54Z are all one pattern, 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder) orderId=<n>' (e.g. 11:16:05Z 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder) orderId=e0216d32').
  - `F17` ✔ exchange-gateway p99 latency jumped from ~128ms flat (10:30–10:54, max 136.07) to avg 1389.09 / max 5152.47ms in the 10:58 bucket and stayed pinned around the 5000ms timeout through 12:58 (e.g. 12:58 avg 5048.37), confirming timeouts dominate the gateway's latency.
  - `F18` ✔ exchange-gateway's upstream_error_rate (its calls to the external partner) rose from a flat baseline ~0.30% (10:40–10:56, max 0.32) to 33.95%/max 45.66 in the 11:00 bucket and stayed ~44–47% through 12:56 (last avg 45.76), pinpointing the external upstream call as the persistent failure.
  - `F19` ✔ market-data-service — another caller of exchange-gateway and a user of shared redis-cache — shows a concurrent, much smaller error signal: error_rate went from ~0.23% baseline (10:30–10:58) to 6.48% in the 11:02 bucket and stayed ~6.4–6.9% through 12:58 (last avg 6.76). This indicates the exchange-gateway/partner failure propagates to other dependents, not just order-service.
  - `F21` ✔ Per-service flag/config queries for the two services on the failing partner path returned zero changes in the window: exchange-gateway count 0 and order-service count 0 for 2026-03-17T00:00Z-12:59Z.
  - `F22` ✔ No flag/config change occurred near 11:01Z: a narrow 10:40Z-11:10Z all-services query returned count 0, i.e. nothing was flipped in the 21 minutes bracketing the first exchange-gateway partner connect timeout at 11:01:25Z and the order-service 504 onset at 11:02:45Z.
  - `F23` ✔ Other services that could plausibly host partner-routing/endpoint flags also show zero in-window changes: api-gateway count 0 and market-data-service count 0 for 2026-03-17T00:00Z-12:59Z.
  - `F24` ✔ A wider exchange-gateway query spanning 2026-03-01T00:00Z-2026-03-18T00:00Z also returned count 0, so no configuration/flag change to exchange-gateway (e.g. a stale partner endpoint or host selection) exists in the two weeks preceding the incident either; the partner-path failure is not attributable to a gateway-side config edit.
  - `F32` ✔ exchange-gateway's ERROR logs mentioning api.exchange-partner.example in 10:55–12:59Z comprise exactly TWO patterns and nothing else: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163 occurrences, first seen 11:01:25Z) and the same pattern with op=quoteSnapshot (146 occurrences, first seen 11:01:43Z). No DNS-resolution error and no TLS-handshake error appears in this host-filtered result.
  - `F33` ✔ The unfiltered exchange-gateway ERROR set for the same window (total_matched 470) contains exactly three patterns: the op=placeOrder connect timeout (163), 'exchange-api returned <n> Service Unavailable (x-partner-request-id=<n>)' (161, first seen 11:02:20Z), and the op=quoteSnapshot connect timeout (146). There is no third error class — no resolver/DNS failure, no TLS/certificate error, no connect-refused — so the taxonomy is exhaustive as far as ERROR-level logs go.
  - `F38` ✔ market-data-service WARN+ logs in 10:45–12:59Z contain exactly ONE pattern: 'stale quotes: exchange feed lag <n>s via exchange-gateway' (142 matches, first 11:02:59Z, last 12:57:49Z, all WARN). This is a downstream feed-lag symptom observed through the gateway, NOT a connect-timeout/upstream-connect failure of its own; the lag values quoted range 2s–12s.
  - `F43` ✔ The market-data-service 'stale quotes ... via exchange-gateway' WARNs begin at 11:02:59Z, i.e. ~94s AFTER exchange-gateway's first partner connect timeout (11:01:25Z, F10) and ~14s after order-service's first 504 (11:02:45Z, F9) — the second caller's degradation is temporally downstream of the gateway failure, not prior to it.
  - `F47` ✔ The exchange-gateway error taxonomy is exhaustive (470 ERRORs, three patterns only: placeOrder connect timeout 163, exchange-api 503 x-partner-request-id 161, quoteSnapshot connect timeout 146) with no resolver/TLS/connect-refused class; the 503 responses carrying x-partner-request-id show that at least some requests DID reach the partner's edge and got an HTTP response, which argues the local egress path was functional for those calls rather than wholly broken.
  - `F52` ✔ Precedent search for partner-outage records returns two external-dependency incidents, headed by HIST-103 (2025-10-21) 'Order failures during exchange partner outage', services [order-service, exchange-gateway], symptoms ['504 on order submit','upstream timeouts'], root_cause 'External exchange API outage on the partner side', category 'dependency_failure', resolution 'Failed over to secondary exchange route; partner fixed'; HIST-105 (2025-11-19) is a bank-partner 5xx for 40 minutes. These are leads only; they show the team has previously recorded partner-side outages as incidents, but nothing in the retrieved set records a partner-side notice for the current 2026-03-17 event.
  - `F2` ✔ No other service was released near onset: the all-services deployment query returned count 1, so no deployment exists for pricing-engine, exchange-gateway, api-gateway or any shared datastore in the window.
**Contradicting evidence**
  - (none)
**Assumptions**
  - The failing element is the partner's own service/edge rather than exchange-gateway's last-mile egress path; internal telemetry is byte-identical for both readings and the vendor node exposes no telemetry (this is exactly the H1/H5 split).
  - The step from a ~45% partner-path failure to order-service's ~30% failure rate is proportional to order-service's share of placeOrder/quoteSnapshot calls — not directly measured.
  - The 503s carrying x-partner-request-id reflect a genuine partner-side response rather than a vendor edge proxy that is part of the gateway's own egress chain.

### H5: exchange-gateway-side egress failure (routing/NAT/packet-loss path to the partner host) — not a partner outage
The connect-phase 5s timeouts to api.exchange-partner.example originate in exchange-gateway's own outbound path: a route/egress/NAT/packet-loss fault outside order-service and outside the partner API. This produces near-identical internal telemetry to H1, but the correct fix is a network/gateway-repair action, not partner failover — which is why it must be stated separately.

**Causal chain**
1. exchange-gateway's outbound connection attempt to api.exchange-partner.example hangs in the connect phase, hitting the configured 5000ms bound (~309 connect-timeout errors: 163 placeOrder, 146 quoteSnapshot) with no refused-connection or resolution failure [F35, F32, F33]
2. The error taxonomy contains no DNS-resolution, TLS-handshake or resolv signature anywhere in the exchange-gateway corpus, and no infra/network metric (dns_query_latency_ms, network_rx_dropped, egress_bytes_per_s) could be retrieved, so a local egress blackhole cannot be positively confirmed or excluded [F34, F45, F44]
3. But the 503 responses carrying x-partner-request-id show some connections completed the full round trip to the partner edge, which argues the egress path was functional for part of the traffic and weakens a wholesale route/NAT break [F33, F47]
4. No config/flag/route change to exchange-gateway exists in-window or in the preceding two weeks, so H5 requires an unrecorded network/route/NAT change [F21, F24, F49]
5. Callers see the same 504s as in H1 (exchange-gateway returns 504/503), so the symptom does not discriminate the two readings [F9, F16, F11]
6. No second direct vantage point to the partner host exists in the corpus, and the gateway logs carry no pod/instance tag, so a per-node egress fault cannot be localised even in principle from the telemetry [F37, F42, F48]

**Supporting evidence**
  - `F35` ✔ The timeout value is fixed and bounded at the configured limit: every sampled line reads 'upstream connect timeout after 5000ms', i.e. it is client-side timeout-bounded (a hang in the connect phase), not an immediate connection refusal or a name-resolution error which would return without consuming the full 5s.
  - `F32` ✔ exchange-gateway's ERROR logs mentioning api.exchange-partner.example in 10:55–12:59Z comprise exactly TWO patterns and nothing else: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163 occurrences, first seen 11:01:25Z) and the same pattern with op=quoteSnapshot (146 occurrences, first seen 11:01:43Z). No DNS-resolution error and no TLS-handshake error appears in this host-filtered result.
  - `F33` ✔ The unfiltered exchange-gateway ERROR set for the same window (total_matched 470) contains exactly three patterns: the op=placeOrder connect timeout (163), 'exchange-api returned <n> Service Unavailable (x-partner-request-id=<n>)' (161, first seen 11:02:20Z), and the op=quoteSnapshot connect timeout (146). There is no third error class — no resolver/DNS failure, no TLS/certificate error, no connect-refused — so the taxonomy is exhaustive as far as ERROR-level logs go.
  - `F34` ✔ Targeted searches for alternate failure modes returned nothing: a WARN-level search for 'dns' in exchange-gateway 10:55–12:59Z returned total_matched 0, and a WARN-level search for 'tls' returned total_matched 0 (both over the full available data_range 07:00–12:59Z). A search for 'resolv' at INFO level also returned 0.
  - `F49` ✔ No config/change record for exchange-gateway exists in the 48h window 2026-03-16T00:00Z-2026-03-17T13:00Z: count 0, empty changes list, partial=false, scope 'exchange-gateway'. Combined with earlier zero-counts, no gateway-side setting (endpoint/host selection, egress proxy, timeout) was altered, so there is no recorded gateway config cause.
**Contradicting evidence**
  - `F33` ✔ The unfiltered exchange-gateway ERROR set for the same window (total_matched 470) contains exactly three patterns: the op=placeOrder connect timeout (163), 'exchange-api returned <n> Service Unavailable (x-partner-request-id=<n>)' (161, first seen 11:02:20Z), and the op=quoteSnapshot connect timeout (146). There is no third error class — no resolver/DNS failure, no TLS/certificate error, no connect-refused — so the taxonomy is exhaustive as far as ERROR-level logs go.
  - `F47` ✔ The exchange-gateway error taxonomy is exhaustive (470 ERRORs, three patterns only: placeOrder connect timeout 163, exchange-api 503 x-partner-request-id 161, quoteSnapshot connect timeout 146) with no resolver/TLS/connect-refused class; the 503 responses carrying x-partner-request-id show that at least some requests DID reach the partner's edge and got an HTTP response, which argues the local egress path was functional for those calls rather than wholly broken.
**Assumptions**
  - A network/route/NAT/egress change at the gateway or its host could occur with no config/flag record and no warning-severity log — untested; no network change-record API was available.
  - It is not excluded that a partner outage and a gateway-side egress fault would both satisfy every observed signal.

### H3: Shared data layer (ledger-db / redis-cache / kafka) as a second contributing factor to the residual ~30%
The shared multi-tenant layer (ledger-db, redis-cache, kafka) would be the only structurally plausible second contributor to the residual ~30% order-service error rate not attributable to the partner path. On the measured signals it is excluded as an origin: no metric shows a step at onset and every order-service error is a downstream exchange-gateway 504. It cannot be fully excluded as a latency-only contributor because redis-cache/kafka expose no latency or error metric.

**Causal chain**
1. ledger-db/redis-cache/kafka would have to degrade at ~11:01 to explain the onset *(assumed)*
2. Measured shared-component health shows no step at onset: ledger-db connections 56–68, slow queries 0.4–1.6/min, replication lag 36–44ms; redis-cache hit ratio 94–98%; kafka throughput 2297–2762 msg/s [F26, F27, F28, F29, F30]
3. All observed order-service errors are downstream exchange-gateway 504s, not DB/cache/kafka call failures [F9, F16]
4. No config change to any shared datastore exists in the window [F2]

**Supporting evidence**
  - `F5` ✔ Shared-component reachability through neighbours: ledger-db is written by four services (order-service, portfolio-service, payment-service, settlement-service); pricing-engine and market-data-service both use redis-cache as a cache, and market-data-service also calls exchange-gateway — so order-service's neighbours can affect it via redis-cache and ledger-db regardless of the order-service release.
**Contradicting evidence**
  - `F9` ✔ The first order-service ERROR is at 11:02:45Z with signature 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; all 349 matched errors are this one pattern, i.e. 504s returned from the exchange-gateway call, not local application exceptions.
  - `F16` ✔ The failing order-service errors are downstream timeouts, not application exceptions: 310 ERROR logs between 11:16:05Z and 12:58:54Z are all one pattern, 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder) orderId=<n>' (e.g. 11:16:05Z 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder) orderId=e0216d32').
  - `F10` ✔ exchange-gateway shows the EARLIEST failure: connect timeouts to the external partner start at 11:01:25Z, ~80s before order-service's first 504. Signature: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163 occurrences), plus quoteSnapshot timeouts (146) and 503s from exchange-api. A circuit breaker opened at 11:03:26Z.
  - `F18` ✔ exchange-gateway's upstream_error_rate (its calls to the external partner) rose from a flat baseline ~0.30% (10:40–10:56, max 0.32) to 33.95%/max 45.66 in the 11:00 bucket and stayed ~44–47% through 12:56 (last avg 45.76), pinpointing the external upstream call as the persistent failure.
  - `F26` ✔ ledger-db shows no anomaly across the incident window: connections_active stayed in a narrow band (min 56.43, max 67.73, mean 62.02; 10:45 avg 60.85, 12:57 avg 63.97) with no step at 11:01-11:02Z and no sustained rise through 12:59Z.
  - `F27` ✔ ledger-db slow_queries_per_min is likewise flat and low for the entire 10:45Z-12:57Z window (min 0.4, max 1.6, mean 0.99), with no excursion at 11:01-11:02Z (11:00 bucket avg 0.86, 11:03 avg 1.18).
  - `F28` ✔ ledger-db replication_lag_ms is stable with no onset: min 36.23 / max 44.07 / mean 40.21 ms, essentially unchanged across the window (10:45 avg 39.34; 12:57 avg 39.29).
  - `F29` ✔ redis-cache (shared by order-service neighbours pricing-engine and market-data-service) shows no degradation: hit_ratio_pct is steady ~95-97% for the whole window (min 94.26, max 97.8, mean 96.04; 11:00 avg 95.85, 11:03 avg 95.46, 12:57 avg 96.19).
  - `F30` ✔ kafka message throughput is steady with no saturation or step change: messages_in_per_s min 2297.39 / max 2762.08 / mean 2544.19, 11:00 avg 2568.51, 11:03 avg 2494.22, 12:57 avg 2467.87.
  - `F2` ✔ No other service was released near onset: the all-services deployment query returned count 1, so no deployment exists for pricing-engine, exchange-gateway, api-gateway or any shared datastore in the window.
**Assumptions**
  - redis-cache and kafka expose no latency/error metric, so a latency-only fault is not measurable with the available metric sets (F31).
  - ledger-db cpu_pct and disk_used_pct were not queried, so CPU/disk saturation is not fully excluded.
**Rejected because:** Directly contradicted by measurement: every order-service error is an exchange-gateway call failure (F9, F16), not a DB/cache call; ledger-db connections_active (56–68), slow_queries/min (0.4–1.6) and replication_lag (36–44ms) are flat with no step at 11:01–11:02 (F26, F27, F28); redis-cache hit_ratio steady 94–98% and kafka throughput steady 2297–2762 msg/s with no step (F29, F30); and no config change to any shared datastore exists in the window (F2).

### H2: order-service v2.15.2 idempotency/retry code introduced the submit failures
The reporter's suspicion: the v2.15.2 release ('Add idempotency keys to order submit', 'retry-safe submit handler') introduced a defect that produced ~30% 504s.

**Causal chain**
1. dep-5333 deployed 10:48:00Z, rollout complete 10:48:02Z [F1, F13]
2. New idempotency/retry-safe submit path causes submits to block or fail *(assumed)*
3. order-service error_rate jumps to ~30% at 11:02 and errors surface as 504s [F8, F9]
4. Rollback does not resolve, so the release is not the origin [F14, F16]

**Supporting evidence**
  - `F1` ✔ The only deployment to any service in 2026-03-17 00:00Z–12:59Z was order-service v2.15.2 (dep-5333), deployed 2026-03-17T10:48:00Z by a.khan, summary 'Add idempotency keys to order submit', with changes 'Idempotency-Key header persisted in orders table' and 'retry-safe submit handler'; status already 'rolled_back' at 2026-03-17T11:16:00Z.
  - `F8` ✔ order-service error_rate jumped from a ~0.2% baseline (08:00–10:49 buckets, max 0.32) to 30.81% in the 11:02 bucket and stayed ~30–31% through 12:59. The last clean bucket is 10:49 (avg 0.21).
  - `F9` ✔ The first order-service ERROR is at 11:02:45Z with signature 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; all 349 matched errors are this one pattern, i.e. 504s returned from the exchange-gateway call, not local application exceptions.
**Contradicting evidence**
  - `F14` ✔ The order-service v2.15.2 rollback completed at 11:16:38Z ('rollback of dep-5333 complete, previous version restored'), but the order-service error_rate did NOT recover: it was 31.18% in the 11:16 bucket and remained ~30–32% through 12:58 (last bucket avg 30.46, max 30.54).
  - `F15` ✔ exchange-gateway's error_rate also shows no post-rollback recovery: 43.66% in the 11:16 bucket and still 45.13% at 12:58 (max 46.67), i.e. the dependency stayed broken independently of the order-service version.
  - `F10` ✔ exchange-gateway shows the EARLIEST failure: connect timeouts to the external partner start at 11:01:25Z, ~80s before order-service's first 504. Signature: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163 occurrences), plus quoteSnapshot timeouts (146) and 503s from exchange-api. A circuit breaker opened at 11:03:26Z.
  - `F16` ✔ The failing order-service errors are downstream timeouts, not application exceptions: 310 ERROR logs between 11:16:05Z and 12:58:54Z are all one pattern, 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder) orderId=<n>' (e.g. 11:16:05Z 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder) orderId=e0216d32').
  - `F17` ✔ exchange-gateway p99 latency jumped from ~128ms flat (10:30–10:54, max 136.07) to avg 1389.09 / max 5152.47ms in the 10:58 bucket and stayed pinned around the 5000ms timeout through 12:58 (e.g. 12:58 avg 5048.37), confirming timeouts dominate the gateway's latency.
  - `F18` ✔ exchange-gateway's upstream_error_rate (its calls to the external partner) rose from a flat baseline ~0.30% (10:40–10:56, max 0.32) to 33.95%/max 45.66 in the 11:00 bucket and stayed ~44–47% through 12:56 (last avg 45.76), pinpointing the external upstream call as the persistent failure.
  - `F19` ✔ market-data-service — another caller of exchange-gateway and a user of shared redis-cache — shows a concurrent, much smaller error signal: error_rate went from ~0.23% baseline (10:30–10:58) to 6.48% in the 11:02 bucket and stayed ~6.4–6.9% through 12:58 (last avg 6.76). This indicates the exchange-gateway/partner failure propagates to other dependents, not just order-service.
  - `F13` ✔ order-service v2.15.2 rollout completed at 10:48:02Z ('deployment dep-5333 (v2.15.2) rollout complete, all pods ready'), i.e. ~13.5 minutes before the first order-service 504 at 11:02:45Z; order-service logged 'connection pool stats: healthy' at 10:47:04 and 'health check ok' at 11:03:31.
**Assumptions**
  - The new retry/idempotency code could generate exchange-gateway 504s.
  - No other cause explains the failures.
**Rejected because:** Rollback of dep-5333 completed at 11:16:38Z with zero recovery (F14, F16); all order-service errors are downstream exchange-gateway 504s, not local application exceptions (F9, F16); exchange-gateway's onset (11:01:25Z) precedes order-service's (11:02:45Z) and the release completed ~13.5 min earlier with healthy pool/health signals (F13); the same exchange-gateway failure independently hits market-data-service, which was not released (F19, F2).

### H4: pricing-engine cache TTL config change (cfg-2290) as the trigger
The only non-deploy change in the window, pricing-engine cache.ttl_seconds 5→4 at 09:20Z, degraded the quote/order path and produced submit failures.

**Causal chain**
1. cfg-2290 shortens pricing-engine quote cache TTL at 09:20Z [F3]
2. This degrades pricing-engine or increases load downstream *(assumed)*
3. order-service submits time out ~30% [F8]

**Supporting evidence**
  - `F3` ✔ The only config change across all services in the window was pricing-engine cache.ttl_seconds changed 5 -> 4 (cfg-2290) at 2026-03-17T09:20:00Z by config-bot, described as 'Shorter quote cache TTL' — 88 minutes before the order-service release.
**Contradicting evidence**
  - `F8` ✔ order-service error_rate jumped from a ~0.2% baseline (08:00–10:49 buckets, max 0.32) to 30.81% in the 11:02 bucket and stayed ~30–31% through 12:59. The last clean bucket is 10:49 (avg 0.21).
  - `F9` ✔ The first order-service ERROR is at 11:02:45Z with signature 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; all 349 matched errors are this one pattern, i.e. 504s returned from the exchange-gateway call, not local application exceptions.
  - `F10` ✔ exchange-gateway shows the EARLIEST failure: connect timeouts to the external partner start at 11:01:25Z, ~80s before order-service's first 504. Signature: 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163 occurrences), plus quoteSnapshot timeouts (146) and 503s from exchange-api. A circuit breaker opened at 11:03:26Z.
  - `F12` ✔ No anomaly exists in the pre-release window for exchange-gateway: at 1-minute resolution, latency p99 was flat 115.5–138.7ms continuously from 10:30 to 10:50Z, showing no degradation or saturation before the release.
  - `F16` ✔ The failing order-service errors are downstream timeouts, not application exceptions: 310 ERROR logs between 11:16:05Z and 12:58:54Z are all one pattern, 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder) orderId=<n>' (e.g. 11:16:05Z 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder) orderId=e0216d32').
  - `F18` ✔ exchange-gateway's upstream_error_rate (its calls to the external partner) rose from a flat baseline ~0.30% (10:40–10:56, max 0.32) to 33.95%/max 45.66 in the 11:00 bucket and stayed ~44–47% through 12:56 (last avg 45.76), pinpointing the external upstream call as the persistent failure.
**Assumptions**
  - A 1s TTL reduction could propagate into 504s on exchange-gateway calls.
**Rejected because:** 88 minutes elapse between cfg-2290 and onset with no symptom signal (F3, F8); the observed failures are exchange-gateway→external-partner connect timeouts, a path pricing-engine's cache TTL does not touch (F9, F10, F18); and the pre-onset gateway latency was flat (F12).

## Facts (verified observations)
- order-service error_rate jumped from a ~0.2% baseline to 30.81% in the 11:02 bucket and stayed ~30-31% through 12:59; the last clean bucket is 10:49. [F8]
- The first order-service ERROR is at 11:02:45Z with signature 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)'; all 349 matched errors are this pattern, i.e. downstream 504s rather than local application exceptions. [F9]
- exchange-gateway shows the earliest failure: connect timeouts to api.exchange-partner.example begin at 11:01:25Z (~80s before order-service's first 504), signature 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' (163 occurrences) plus quoteSnapshot timeouts (146) and 503s from exchange-api; a circuit breaker opened at 11:03:26Z. [F10]
- exchange-gateway p99 latency jumped from ~128ms flat (10:30-10:54) to avg 1389.09 / max 5152.47ms in the 10:58 bucket and stayed pinned near the 5000ms timeout through 12:58. [F17]
- exchange-gateway's upstream_error_rate (its calls to the external partner) rose from a flat ~0.30% baseline to 33.95%/max 45.66 in the 11:00 bucket and stayed ~44-47% through 12:56. [F18]
- order-service v2.15.2 rollout completed at 10:48:02Z, ~13.5 minutes before the first order-service 504 at 11:02:45Z, with healthy pool stats at 10:47:04 and 'health check ok' at 11:03:31. [F13]
- The v2.15.2 rollback completed at 11:16:38Z but order-service error_rate did not recover: 31.18% in the 11:16 bucket and ~30-32% through 12:58. [F14]
- exchange-gateway's error_rate shows no post-rollback recovery either: 43.66% in the 11:16 bucket and 45.13% at 12:58, i.e. the dependency stayed broken independently of the order-service version. [F15]
- The failing order-service errors after the rollback remain downstream timeouts: 310 ERROR logs between 11:16:05Z and 12:58:54Z are all 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)' (e.g. 11:16:05Z orderId=e0216d32). [F16]
- The exchange-gateway failure was already present pre-release: no anomaly existed in the pre-release window, with p99 flat 115.5-138.7ms continuously from 10:30 to 10:50Z. [F12]
- The exchange-gateway error taxonomy in 10:55-12:59Z is exhaustive: 470 ERRORs comprising exactly three patterns — placeOrder connect timeout (163), 'exchange-api returned <n> Service Unavailable (x-partner-request-id=<n>)' (161, first seen 11:02:20Z), and quoteSnapshot connect timeout (146) — with no DNS-resolution, TLS/certificate, or connect-refused class. [F33, F32]
- The timeout is fixed and bounded at the configured limit: every sampled line reads 'upstream connect timeout after 5000ms', i.e. a connect-phase hang at the client-side bound rather than an immediate refusal or name-resolution error. [F35]
- No DNS-resolution failure signature exists in the exchange-gateway corpus: targeted WARN searches for 'dns' and 'tls' over 10:55-12:59Z (full data_range 07:00-12:59Z) returned total_matched 0, and an INFO search for 'resolv' returned 0. [F34, F45]
- None of the exchange-gateway error lines carry a pod or instance identifier (ERROR-level search for 'pod' returned 0), so logs cannot be split by gateway instance to detect a per-node egress/DNS fault. [F37]
- market-data-service — another exchange-gateway caller — shows a concurrent, much smaller error signal: error_rate rose from ~0.23% baseline to 6.48% in the 11:02 bucket and stayed ~6.4-6.9% through 12:58. [F19]
- market-data-service's own request path shows no saturation (request_rate 862.6-1052.2, latency_p99 ~35.4-44.6ms), and its only WARN+ pattern is 'stale quotes: exchange feed lag <n>s via exchange-gateway' (142 matches), the first at 11:02:59Z — ~94s after the gateway's first partner connect timeout and ~14s after order-service's first 504. [F40, F38, F43]
- market-data-service has no same-signature failure: targeted searches returned 0 matches for 'exchange-partner', for 'timeout' at WARN+, and for 'upstream connect' at WARN+ across 07:00-12:59Z. [F39]
- No successful partner call is visible anywhere in 10:45-12:59Z from any searchable caller: market-data-service, order-service and pricing-engine logs contain zero lines mentioning 'exchange-partner'; nothing in the retrieved data shows a 2xx/success count on api.exchange-partner.example. [F41]
- No config/flag change occurred anywhere in the window near onset: a narrow 10:40-11:10Z all-services query returned count 0, and per-service queries for exchange-gateway and order-service returned 0 for the whole day. [F22, F21]
- A wider exchange-gateway config/flag query spanning 2026-03-01 to 2026-03-18 returned count 0, so no gateway-side config edit (e.g. a stale partner endpoint or host selection) exists in the two weeks preceding the incident; a 48h exchange-gateway change query also returned count 0 with partial=false. [F24, F49]
- The only deployment to any service in the window was order-service v2.15.2 (dep-5333) at 10:48:00Z, and the only config change was pricing-engine cache.ttl_seconds 5->4 (cfg-2290) at 09:20:00Z, 88 minutes before the release. [F1, F2, F3]
- The shared multi-tenant data layer shows no anomaly across the window: ledger-db connections_active stayed in a narrow band (min 56.43, max 67.73; 10:45 avg 60.85, 12:57 avg 63.97), slow_queries_per_min flat at 0.4-1.6 (11:00 avg 0.86, 11:03 avg 1.18), and replication_lag_ms stable at 36.23-44.07ms. [F26, F27, F28]
- redis-cache hit_ratio_pct is steady ~95-97% for the whole window (11:00 avg 95.85, 11:03 avg 95.46, 12:57 avg 96.19) and kafka messages_in_per_s is steady at 2297.39-2762.08 (11:00 avg 2568.51, 11:03 avg 2494.22, 12:57 avg 2467.87): no degradation or step change at onset. [F29, F30]
- redis-cache and kafka expose no latency or error-rate metric (available metric sets are limited to redis-cache [connected_clients, cpu_pct, evictions_per_min, hit_ratio_pct, memory_pct] and kafka [cpu_pct, messages_in_per_s, under_replicated_partitions]), so latency saturation for these components cannot be measured. [F31]
- exchange-gateway's only outbound edge is to an external node 'exchange-api' (kind 'external', owner 'vendor', has_telemetry=false); the failing target api.exchange-partner.example sits outside the instrumented estate, so no partner-side metric can corroborate or exclude a partner outage. [F50]
- All four infra/network-layer metric queries for the exchange-gateway namespace (dns_query_latency_ms, pod_restarts, network_rx_dropped, egress_bytes_per_s) over 10:40-12:59Z failed with budget_exhausted before executing, so no infra/network metric values exist in the evidence. [F44]
- Because the vendor node carries has_telemetry=false and no status-page/partner-notice record was retrievable, the evidence base is structurally incapable of confirming H1 (partner-acknowledged outage) or H5 (unrecorded gateway-side egress change); the H1-vs-H5 call rests entirely on the log signature (connect-phase 5s timeout to a resolved host, no DNS/TLS error). [F50]
- Historical precedent: HIST-103 (2025-10-21) 'Order failures during exchange partner outage' affected order-service and exchange-gateway with symptoms ['504 on order submit','upstream timeouts'], root cause 'External exchange API outage on the partner side', resolved by failing over to a secondary exchange route; and no historical incident records an order-service rollback that fixed order-submit 504s. [F52]
- No network-infrastructure precedent exists in the searched incident history: a query for egress/firewall/NAT/route/connectivity terms returned a single hit (HIST-104) which is a Kafka partition-reassignment incident with no network-change component. [F51]

## Assumptions (not verified)
- Assumption: the failing element is the partner's own service/edge rather than exchange-gateway's last-mile egress path — internal telemetry is byte-identical for both readings and the vendor node exposes no telemetry (this is exactly the H1/H5 split, left unresolved).
- Assumption: the step from a ~45% partner-path failure rate to order-service's ~30% failure rate is proportional to order-service's share of placeOrder/quoteSnapshot calls; this ratio was not directly measured.
- Assumption: the 503s carrying x-partner-request-id reflect a genuine partner-side response rather than a vendor edge proxy that is part of the gateway's own egress chain.
- Assumption: a network/route/NAT/egress change at the gateway or its host could occur with no config/flag record and no warning-severity log — untested, as no network change-record API was available.
- Assumption: the conclusive absence of DNS/TLS/resolver signatures holds at the severity levels queried (WARN/ERROR, plus one INFO 'resolv' search); a lower-severity (INFO/DEBUG) or unlogged occurrence cannot be excluded.
- Assumption: the config/flag tool enumerates all flag keys; its output was structurally identical to get_config_changes (only cfg-2290), so true feature-flag completeness is not provable.
- Assumption: redis-cache and kafka are healthy on the basis of hit-ratio and throughput only, since no latency/error metric exists for either component.
- Assumption: ledger-db CPU/disk saturation is not an independent contributor, based on connection/slow-query/replication signals only; cpu_pct and disk_used_pct were in the available metric list but were not queried.
- (demoted from facts: no verified observation backs it) Because the error reaches the connect phase with no DNS or TLS error in the entire error set, the hostname must have resolved to an IP and the gateway proceeded to attempt the TCP connection.
- (demoted from facts: no verified observation backs it) The 503 responses carrying x-partner-request-id show that at least some requests reached the partner's edge and got an HTTP response, arguing the local egress path was functional for those calls rather than wholly broken.
- (demoted from facts: no verified observation backs it) No non-gateway caller directly touches api.exchange-partner.example in the log corpus: the host string appears only in exchange-gateway's error output, and every non-gateway search for it returned 0, so there is no independent direct caller to serve as a true second vantage point.

## Timeline
- `2026-03-17T09:20:00Z` Only config change in the window: pricing-engine cache.ttl_seconds 5 -> 4 (cfg-2290) by config-bot; 88 minutes before any symptom. [F3, F20]
- `2026-03-17T10:30:00Z` Pre-onset baseline: exchange-gateway p99 flat 115.5-138.7ms through 10:50Z with no degradation or saturation. [F12]
- `2026-03-17T10:47:04Z` order-service logs 'connection pool stats: healthy'. [F13]
- `2026-03-17T10:48:00Z` order-service v2.15.2 (dep-5333) deployed by a.khan — the only deployment to any service in the window. [F1, F2]
- `2026-03-17T10:48:02Z` v2.15.2 rollout completes, all pods ready. [F13]
- `2026-03-17T10:49:00Z` Last clean order-service error_rate bucket (avg 0.21%). [F8]
- `2026-03-17T11:01:25Z` Earliest failure anywhere: exchange-gateway 'upstream connect timeout after 5000ms host=api.exchange-partner.example op=placeOrder' to the external partner begins. [F10, F35]
- `2026-03-17T11:01:43Z` quoteSnapshot connect timeouts to the same partner host begin (146 occurrences). [F32]
- `2026-03-17T11:02:20Z` First 'exchange-api returned <n> Service Unavailable (x-partner-request-id=<n>)' from exchange-gateway (161 occurrences total). [F33, F47]
- `2026-03-17T11:02:00Z` Metric buckets show order-service error_rate at 30.81% and exchange-gateway error_rate at 43.16% (baseline ~0.2%). [F8, F11]
- `2026-03-17T11:02:45Z` First order-service ERROR: 'exchange-gateway call failed: 504 Gateway Timeout (placeOrder)' — ~80s after the gateway's first partner connect timeout. [F9, F10]
- `2026-03-17T11:02:59Z` First market-data-service WARN 'stale quotes: exchange feed lag <n>s via exchange-gateway' — ~94s after the gateway failure, ~14s after order-service's first 504. [F38, F43]
- `2026-03-17T11:03:26Z` exchange-gateway circuit breaker opens. [F10]
- `2026-03-17T11:03:31Z` order-service logs 'health check ok' despite the failing submit path. [F13]
- `2026-03-17T11:10:00Z` Incident reported by on-call SRE; order-service v2.15.2 rollback underway. []
- `2026-03-17T11:16:00Z` Rollback of dep-5333 recorded as complete (dep-5333 status 'rolled_back'). [F1]
- `2026-03-17T11:16:38Z` order-service rollback completes ('previous version restored'). [F14]
- `2026-03-17T11:16:00Z` Bucket after rollback: order-service error_rate 31.18% and exchange-gateway error_rate 43.66% — zero recovery on either service. [F14, F15]
- `2026-03-17T11:16:05Z` Post-rollback order-service errors resume with the same single pattern: 'exchange-gateway call failed: <n> Gateway Timeout (placeOrder)'; 310 such logs through 12:58:54Z. [F16]
- `2026-03-17T12:56:00Z` exchange-gateway upstream_error_rate still avg 45.76% — the external partner path remains broken ~2 hours after onset. [F18]
- `2026-03-17T12:57:00Z` Shared-layer metrics remain flat through end of window: ledger-db connections_active avg 63.97 / slow_queries flat; redis-cache hit_ratio 96.19%; kafka 2467.87 msg/s. [F26, F27, F28, F29, F30]
- `2026-03-17T12:58:00Z` No recovery by latest data: order-service error_rate avg 30.46, exchange-gateway avg 5048.37ms p99 / error_rate 45.13%, market-data-service error_rate 6.76%. [F14, F15, F17, F19]

## Recommended actions
- **[immediate]** Keep order-service on the rolled-back version (do not re-deploy v2.15.2) and record explicitly that the rollback is not a fix: the error rate did not move (31.18% at 11:16, ~30-32% through 12:58). Do not spend further incident time on order-service changes. -- Rollback completed at 11:16:38Z with zero recovery while the gateway stayed broken, so order-service is a victim and any further order-service churn is wasted mitigation time. [F14, F15, F16]
- **[immediate]** Execute partner-path failover: switch exchange-gateway's outbound exchange route to the documented secondary exchange route used in HIST-103, and have the partner/vendor liaison open a ticket against api.exchange-partner.example for a ~45% connect-timeout/503 condition since 11:01:25Z. -- H1 is the leading hypothesis (0.60). The partner path has been failing continuously for ~2h with ~45% upstream errors and a circuit breaker open since 11:03:26Z, and the historical precedent HIST-103 for the exact same symptoms was resolved by failing over to a secondary exchange route then having the partner fix the issue. [F10, F18, F52, F47]
- **[immediate]** In parallel, run the primary H1/H5 discriminator from a second vantage point: a synthetic probe / curl to api.exchange-partner.example:443 from (a) a different network egress than exchange-gateway's pods, and (b) a node in the exchange-gateway namespace. A failure from all vantage points points to H1 (partner); failure only from the gateway namespace/egress points to H5 (gateway egress/network path). -- H1 and H5 produce byte-identical internal telemetry and imply different fixes (partner failover vs network/gateway repair); the corpus contains no independent direct caller to the partner host, so a probe is the only way to split them. [F42, F48, F53]
- **[short_term]** Complete the failed network-layer measurement for the exchange-gateway namespace (dns_query_latency_ms, pod_restarts, network_rx_dropped, egress_bytes_per_s, NAT/interface counters) with a fresh query budget, and enumerate available_metrics for exchange-gateway first to confirm these names exist. -- All four infra/network metric queries failed with budget_exhausted before executing, so the core question — whether any network-layer signal has onset near 11:01Z — was never answered; this is the measurement that can positively support or exclude H5. [F44]
- **[short_term]** If the probe shows gateway-side-only failure (H5): engage the network/platform team for route/NAT/egress repair on the exchange-gateway path and re-check per-pod behaviour; if all vantage points fail (H1): complete the secondary-route failover and hold it until the partner confirms recovery. -- The correct remediation path is conditional on the discriminator result; committing to a single fix before the probe risks another failed mitigation round like the rollback. [F44, F42, F52]
- **[long_term]** Add pod/instance identity to exchange-gateway logs and emit an outbound success metric (2xx/upstream_error_rate keyed by partner host:port) on every service that calls the partner or the gateway; today no caller or probe records a successful partner call, and the logs carry no pod tag. -- The missing per-instance identity and the absence of any success-path signal are precisely what made a per-node egress fault undetectable and forced this incident to inconclusive; both are cheap to add. [F37, F41, F31]
- **[long_term]** Instrument and alert on the external partner path explicitly: alert on exchange-gateway upstream_error_rate to api.exchange-partner.example and on 5s connect-phase timeouts, with a runbook that routes to partner failover/secondary route first (per HIST-103) rather than to a code rollback. -- The partner node has has_telemetry=false and no status source, so internal alerting on the gateway's upstream_error_rate is the only early-warning available; the historical precedent shows the team's recorded resolution was route failover, not rollback. [F18, F50, F52]
- **[long_term]** Add latency/error metrics for redis-cache and kafka, and query ledger-db cpu_pct/disk_used_pct in future shared-component investigations, so a latency-only shared-layer contributor cannot go undetected. -- The shared layer was excluded as an origin only on hit-ratio/throughput/connections evidence because no latency or error metric exists for redis-cache or kafka, and ledger-db cpu/disk were never queried — this is a standing blind spot in the exclusion argument. [F31, F26, F29, F30]

## Data gaps
- All four exchange-gateway infra/network metric queries (dns_query_latency_ms, pod_restarts, network_rx_dropped, egress_bytes_per_s) failed with budget_exhausted, so the network layer was never measured by metrics — H5 can be neither positively supported nor excluded.
- No true second vantage point to api.exchange-partner.example exists in the data: no non-gateway caller dials the partner host directly, and no probe/synthetic check configuration was retrievable, so 'fails from all vantage points vs succeeds elsewhere' — the core H1/H5 discriminator — is unresolved.
- No partner-side telemetry or status/notice source is reachable: the vendor node exchange-api has has_telemetry=false and no partner acknowledgement record appears in the corpus, so H1 cannot be corroborated externally.
- No network/infrastructure change-record API was available (firewall rules, security groups, NAT gateways, route tables, VPC/peering), so an unrecorded egress/route change on the gateway path could not be checked; the only change API returns the standard config catalog (count 0 for exchange-gateway).
- exchange-gateway logs carry no pod/instance identifier (ERROR-level search for 'pod' returned 0), so a differing per-node error profile indicative of a per-node egress/DNS fault cannot be evaluated even in principle.
- No successful partner call is visible anywhere in 10:45-12:59Z, but the absence of a success signal cannot be read as proof of a partner-wide outage because no comparable success/probe source exists in the corpus; the success-oriented log searches also returned budget_exhausted.
- Feature-flag completeness is unproven: the flag endpoint returned output structurally identical to get_config_changes (only cfg-2290), so whether any flag key exists but is not enumerated cannot be verified; a pre-existing, unchanged bad partner-endpoint flag value also cannot be ruled out.
- market-data-service ERROR-level logs show zero matches and the WARN+ follow-up could not be run, so the claim that the second affected service shares the same partner-connect-timeout signature is unverified (its error_rate rise to ~6.5% is established, its signature is not).
- exchange-gateway log results were truncated (471 matched, 30 returned) and order-service results truncated (349 matched, 40 returned), so the exhaustiveness of the error taxonomy is inferred from matched counts rather than full inspection of every line.
- ledger-db cpu_pct and disk_used_pct were available but not queried, and no error_rate metric exists for ledger-db (query returned not_found), so CPU/disk saturation as an independent contributor is not fully excluded.
- No latency or error-rate metric exists for redis-cache or kafka, so their health is asserted only from hit-ratio and throughput; a latency-only shared saturation could go undetected.
- Blast radius is incomplete: order-service restarts/cpu_pct, exchange-gateway cpu_pct, api-gateway error rate, and other callers/dependencies were not queried, so the full set of affected services and their onset ordering is unknown.

## What would change this conclusion
- A synthetic probe or real transaction to api.exchange-partner.example from a network vantage point outside exchange-gateway's egress failing at the same time would confirm H1 (partner-side outage); the same probe succeeding from another vantage point while failing from the gateway namespace would flip the origin to H5 (gateway egress/DNS/network path).
- Network-layer metrics for the exchange-gateway namespace (dns_query_latency_ms spike, network_rx_dropped, egress_bytes_per_s collapse, pod restarts) with onset near 11:01:25Z would positively support H5 and demote H1.
- A partner-side status notice or vendor acknowledgement of an outage at ~11:01Z for api.exchange-partner.example would confirm H1 and raise its confidence well above 0.60.
- Per-pod log identity or a per-instance error breakdown showing one or a few gateway pods with a different error profile would indicate a node-local egress fault (H5) rather than a partner-wide outage (H1).
- Evidence that some caller or probe reached api.exchange-partner.example successfully during 10:45-12:59Z would strongly undercut H1 and favor a gateway-side path fault (H5).
- A recorded (or discovered) firewall/route/NAT/egress change on the exchange-gateway path at ~11:01Z would move the origin to H5 regardless of the log signature.
- Discovery of a feature-flag flip on partner routing/endpoint pointing at a bad host, or a pre-existing stale-but-unchanged partner endpoint value, would introduce a config_change origin that is not currently in the ranking.
- Evidence that redis-cache or kafka suffered latency-only saturation in the window (had such metrics existed) would reopen H3 as a second contributor to the residual ~30% error rate.
- A successful 504-free period on order-service after the v2.15.2 rollback would have reopened H2; the observed zero recovery (F14, F16) rules this out and further data here is unlikely to change the conclusion.

## Integrity warnings (set by code)
- fact demoted to assumption: Because the error reaches the connect phase with no DNS or TLS error in the enti
- fact demoted to assumption: The 503 responses carrying x-partner-request-id show that at least some requests
- fact demoted to assumption: No non-gateway caller directly touches api.exchange-partner.example in the log c

## Investigation stats
- latency 392.09s, LLM calls 38, tokens 355946 (in 304788 / out 51158)
- tool calls 64 (7 failed, 0 partial, 0 recovered by retry), findings 53/53 verified
