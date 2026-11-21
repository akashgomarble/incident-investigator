# Evaluation results

- LLM: `deepseek-ai/DeepSeek-V4.1-Flash`  |  trials per incident: 2  |  random tool faults: 30% of calls fail once (a retry succeeds), 10% of distinct calls fail on every attempt
- Incidents: 12  |  all systems use the same tools, fault injection, tool-call budget, evidence rules and scorer

## Headline comparison

| Metric | Multi-agent | Single-agent baseline |
|---|---|---|
| Root-cause accuracy (all incidents) | 92% | 100% |
|   ...runs correct, with 95% Wilson interval | 22/24 (74%-98%) | 24/24 (86%-100%) |
|   ...on incidents with a determinable cause | 91% | 100% |
|   ...correctly inconclusive when it should be | 100% | 100% |
| Correct service ranked #1 | 100% | 100% |
| Correct cause within top-3 hypotheses | 100% | 100% |
| Triggering change id identified | 92% | 100% |
| Wrong but stated as identified (lower is better) | 0% | 0% |
| Blamed the planted decoy (lower is better) | 0% | 0% |
| Abstained although answerable (lower is better) | 9% | 0% |
| Evidence grounding: cited findings verified | 99% | 100% |
| Evidence grounding: quotes found in tool output | 98% | 100% |
| Causal-chain hops backed by verified findings | 96% | 100% |
| Key-evidence recall | 96% | 90% |
| Mean confidence in the top hypothesis | 0.67 | 0.84 |
| Tool calls that returned an error after retries, per incident (injected faults and bad requests) | 9.0 | 4.0 |
| Tool calls recovered by a retry, per incident | 10.9 | 5.5 |
| Data gaps listed in the report, per incident | 8.8 | 3.9 |
| Tool calls per incident (mean) | 46.3 | 23.5 |
| LLM calls per incident (mean) | 29.3 | 6.3 |
| Latency per incident, mean (s) | 295.4 | 76.8 |
| Latency per incident, median (s) | 284.1 | 66.4 |
| Input tokens per incident (mean) | 197,912 | 76,191 |
| Output tokens per incident (mean) | 38,595 | 8,406 |
| Total tokens per incident (mean) | 236,507 | 84,597 |
| Crashed runs | 0 | 0 |

## Is stated confidence informative?

Runs grouped by the confidence the system stated for its top hypothesis. A useful confidence is one where the
higher bands are correct more often. Cells are correct/runs.

| Stated confidence | Multi-agent | Single-agent baseline |
|---|---|---|
| below 0.50 (low) | 1/2 | no runs |
| 0.50 to 0.74 (medium) | 14/15 | 4/4 |
| 0.75 and above (high) | 7/7 | 20/20 |

## Per incident

| Incident | Tags | Multi-agent: result (top hypothesis, confidence) | Single-agent baseline: result (top hypothesis, confidence) |
|---|---|---|---|
| INC-001 | clear-signal | PASS - identified: order-service / bad_deployment, 0.74 | PASS - identified: order-service / bad_deployment, 0.9 |
| INC-002 | misleading-report, decoy-deploy, symptom-not-at-cause | PASS - identified: portfolio-service / config_change, 0.72 | PASS - identified: portfolio-service / config_change, 0.9 |
| INC-003 | misleading-report, decoy-deploy, external-dependency | PASS - identified: exchange-api (external exchange partner, reached via exchange-gateway) / dependency_failure, 0.55 | PASS - identified: exchange-api / dependency_failure, 0.86 |
| INC-004 | slow-burn, needs-wider-window, misleading-report, decoy-config | PASS - identified: notification-service / bad_deployment, 0.7 | PASS - identified: notification-service / bad_deployment, 0.86 |
| INC-005 | conflicting-observations, decoy-config, misleading-history | PASS - identified: payment-service / credential_expiry, 0.7 | PASS - identified: payment-service / credential_expiry, 0.9 |
| INC-006 | tool-failures, cascading, symptom-not-at-cause | PASS - identified: redis-cache / infrastructure_failure, 0.58 | PASS - identified: redis-cache / infrastructure_failure, 0.85 |
| INC-007 | inconclusive, tool-failures, permanent-outage, two-simultaneous-changes | PASS - inconclusive: auth-service (jjwt parser / TokenValidator code path) / bad_deployment, 0.45 | PASS - inconclusive: auth-service / bad_deployment, 0.55 |
| INC-008 | misleading-history, no-change-cause | 1/2 - inconclusive: auth-service / capacity_saturation, 0.5 | PASS - identified: auth-service / capacity_saturation, 0.72 |
| INC-009 | noisy-neighbour, symptom-not-at-cause, decoy-config, multi-hop | 1/2 - identified: portfolio-service / bad_deployment, 0.78 | PASS - identified: portfolio-service / bad_deployment, 0.86 |
| INC-010 | misleading-report, decoy-config, symptom-not-at-cause, multi-hop | PASS - identified: order-service / bad_deployment, 0.78 | PASS - identified: order-service / bad_deployment, 0.88 |
| INC-011 | tool-failures, incomplete-results, decoy-deploy, misleading-report | PASS - identified: pricing-engine / config_change, 0.6 | PASS - identified: pricing-engine / config_change, 0.9 |
| INC-012 | misleading-report, decoy-deploy, tool-failures, symptom-not-at-cause | PASS - identified: ledger-db / resource_exhaustion, 0.82 | PASS - identified: ledger-db / resource_exhaustion, 0.9 |

## Accuracy by incident characteristic

| Tag | Incidents | Multi-agent | Single-agent baseline |
|---|---|---|---|
| cascading | 1 | 100% | 100% |
| clear-signal | 1 | 100% | 100% |
| conflicting-observations | 1 | 100% | 100% |
| decoy-config | 4 | 88% | 100% |
| decoy-deploy | 4 | 100% | 100% |
| external-dependency | 1 | 100% | 100% |
| incomplete-results | 1 | 100% | 100% |
| inconclusive | 1 | 100% | 100% |
| misleading-history | 2 | 75% | 100% |
| misleading-report | 6 | 100% | 100% |
| multi-hop | 2 | 75% | 100% |
| needs-wider-window | 1 | 100% | 100% |
| no-change-cause | 1 | 50% | 100% |
| noisy-neighbour | 1 | 50% | 100% |
| permanent-outage | 1 | 100% | 100% |
| slow-burn | 1 | 100% | 100% |
| symptom-not-at-cause | 5 | 90% | 100% |
| tool-failures | 4 | 100% | 100% |
| two-simultaneous-changes | 1 | 100% | 100% |

Metric definitions are in `eval/scoring.py`; per-run reports and traces are under `runs/` next to this file.
