# Synthetic incident dataset

One fictional brokerage platform (10 services, 4 datastores/brokers, 4 external providers; see `data/topology.json`) and 12 incidents on it. `python dataset/generate.py` regenerates everything deterministically; a test checks the committed files match the generator.

Each incident folder holds what the tools can see (`incident.json`, `logs.jsonl`, `metrics.json`, `deployments.json`, `config_changes.json`), the scripted tool faults (`faults.json`) and `ground_truth.json`, which only `eval/scoring.py` reads. `data/historical_incidents.json` holds 16 past post-mortems, several chosen to look like a current incident while having a different cause.

Every incident also carries background noise: routine INFO lines, occasional unrelated WARNs, and unrelated deploys or config changes on other services.

| Incident | Reported as | Actual cause | What makes it hard | Tool faults |
|---|---|---|---|---|
| INC-001 | Order placement returning 500s (on `order-service`) | bad_deployment in `order-service` (`dep-5101`) | Release v2.14.0 added a validator that NPEs for F&O orders; errors begin 2 min after rollout. | none |
| INC-002 | Portfolio page timing out for users (on `api-gateway`) | config_change in `portfolio-service` (`cfg-2207`) | An automated profile shrank the portfolio-service DB pool 50->5; the gateway deploy 112 min earlier is unrelated. | none |
| INC-003 | Orders failing since this morning's order-service release (on `order-service`) | dependency_failure in `exchange-gateway` | The external exchange API degraded at 11:01. order-service was healthy for 13 min after its deploy, an undeployed service (market-data) is hit too, and the rollback changed nothing. | none |
| INC-004 | notification-service keeps getting OOMKilled (on `notification-service`) | bad_deployment in `notification-service` (`dep-5402`) | v3.8.0 (3 days ago) added an unbounded template cache; memory saw-tooths to OOM every ~22h. Two restarts predate the Kafka change the report blames. | none |
| INC-005 | Customers say payments fail, but the error-rate dashboard is green (on `payment-service`) | credential_expiry in `payment-service` | The mTLS client certificate expired at 12:15. HTTP error_rate stays flat because failures are returned as 200 PENDING_RETRY; payment_success_rate and logs show the truth. | none |
| INC-006 | Quote latency degraded across trading screens (on `pricing-engine`) | infrastructure_failure in `redis-cache` | Redis primary failed and an empty replica was promoted -> cold cache -> miss storm in market-data -> slow pricing. | `query_logs` timeout x2; `query_metrics` unavailable x4; `get_deployments` partial x1 |
| INC-007 | Users being logged out / 401s on every API call (on `auth-service`) | Not determinable (expected outcome: inconclusive) | A JWT library upgrade and a signing-key rotation landed 1 minute apart on auth-service and its logs are unavailable. Nothing in the reachable data separates the two; the honest answer is inconclusive, naming both candidates. | `query_logs` unavailable (permanent) |
| INC-008 | Login is slow or failing (on `auth-service`) | capacity_saturation in `auth-service` | A push campaign quadrupled login traffic; auth-service is capped at 8 replicas and saturates. No deploy is involved. | none |
| INC-009 | Order placement very slow, some orders timing out (on `order-service`) | bad_deployment in `portfolio-service` (`dep-5905`) | portfolio-service v4.5.0 runs an unindexed query that saturates the shared ledger-db; order-service is a victim. | none |
| INC-010 | Trade settlements are stuck (on `settlement-service`) | bad_deployment in `order-service` (`dep-6010`) | order-service v2.16.0 renamed an event field; the v1-only settlement consumer is stuck on the first v2 message. The poll-size change 5 min earlier is a coincidence. | none |
| INC-011 | Order quotes timing out (on `order-service`) | config_change in `pricing-engine` (`cfg-3120`) | A feature flag ramped an expensive Monte-Carlo calculator 5%->100%. The first config-change query silently omits it (partial result); market-data metrics are healthy. | `get_config_changes` partial x1 |
| INC-012 | Payments broken after payment-service v6.0.0 (on `payment-service`) | resource_exhaustion in `ledger-db` | ledger-db's disk filled (WAL retained by an inactive replication slot). Disk was climbing hours before the deploy, undeployed services fail the same way, and the rollback did not help. | `query_metrics` rate_limited x1 |

## Coverage of the brief

| Requirement | Incidents |
|---|---|
| Misleading evidence (a correlated change, a biased report, or a look-alike past incident) | INC-002, 003, 004, 005, 008, 009, 010, 011, 012 |
| Conflicting observations | INC-005 (HTTP error-rate metric is flat while logs and the success-rate metric show failure); INC-003 and INC-012 (a rollback that did not help contradicts the report) |
| Tool failures | INC-006 (timeouts recovered by retry, a 503 that outlasts the retries, a partial result), INC-007 (a data source that never comes back), INC-011 (a partial result that hides the decisive change), INC-012 (rate limit) |
| Evidence insufficient for a conclusion | INC-007 |
| Cause is not where the symptom was reported | INC-002, 003, 006, 009, 010, 011, 012 |
| Needs a wider time window than the report suggests | INC-004 |

## Sanity check on difficulty

The scripted test double (`--llm fake`) follows a naive strategy: look at the reported service and blame the most recent deployment. It scores 3 of 12 on this dataset, so the decoys do their job and the dataset cannot be solved by that shortcut.
