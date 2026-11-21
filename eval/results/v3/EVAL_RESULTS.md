# Evaluation results

- LLM: `deepseek-ai/DeepSeek-V4.1-Flash`  |  trials per incident: 3  |  random tool-fault rate (chaos): 0.0
- Incidents: 12  |  both systems use the same tools, fault injection, tool-call budget, evidence rules and scorer

## Headline comparison

| Metric | Multi-agent | Single-agent baseline |
|---|---|---|
| Root-cause accuracy (all incidents) | 97% | 100% |
|   ...on incidents with a determinable cause | 97% | 100% |
|   ...correctly inconclusive when it should be | 100% | 100% |
| Correct service ranked #1 | 100% | 100% |
| Correct cause within top-3 hypotheses | 100% | 100% |
| Triggering change id identified | 94% | 100% |
| Wrong but stated as identified (lower is better) | 0% | 0% |
| Blamed the planted decoy (lower is better) | 0% | 0% |
| Abstained although answerable (lower is better) | 3% | 0% |
| Evidence grounding: cited findings verified | 100% | 99% |
| Evidence grounding: quotes found in tool output | 98% | 98% |
| Causal-chain hops backed by verified findings | 98% | 98% |
| Key-evidence recall | 93% | 92% |
| Tool calls per incident (mean) | 43.1 | 23.5 |
| LLM calls per incident (mean) | 26.1 | 6.2 |
| Latency per incident, mean (s) | 179.9 | 53.0 |
| Latency per incident, median (s) | 171.5 | 50.6 |
| Input tokens per incident (mean) | 173,674 | 80,402 |
| Output tokens per incident (mean) | 35,490 | 8,772 |
| Total tokens per incident (mean) | 209,163 | 89,173 |
| Crashed runs | 0 | 0 |

## Per incident

| Incident | Tags | Multi-agent: result (top hypothesis, confidence) | Single-agent baseline: result (top hypothesis, confidence) |
|---|---|---|---|
| INC-001 | clear-signal | PASS - identified: order-service / bad_deployment, 0.88 | PASS - identified: order-service / bad_deployment, 0.88 |
| INC-002 | misleading-report, decoy-deploy, symptom-not-at-cause | PASS - identified: portfolio-service / config_change, 0.78 | PASS - identified: portfolio-service / config_change, 0.9 |
| INC-003 | misleading-report, decoy-deploy, external-dependency | PASS - identified: exchange-api (external vendor) / dependency_failure, 0.72 | PASS - identified: exchange-api / dependency_failure, 0.86 |
| INC-004 | slow-burn, needs-wider-window, misleading-report, decoy-config | PASS - identified: notification-service / bad_deployment, 0.7 | PASS - identified: notification-service / bad_deployment, 0.9 |
| INC-005 | conflicting-observations, decoy-config, misleading-history | PASS - identified: payment-service (client certificate / credential lifecycle) / credential_expiry, 0.86 | PASS - identified: payment-service / credential_expiry, 0.9 |
| INC-006 | tool-failures, cascading, symptom-not-at-cause | PASS - identified: redis-cache / infrastructure_failure, 0.74 | PASS - identified: redis-cache / infrastructure_failure, 0.82 |
| INC-007 | inconclusive, tool-failures, permanent-outage, two-simultaneous-changes | PASS - inconclusive: auth-service / bad_deployment, 0.45 | PASS - inconclusive: auth-service / bad_deployment, 0.42 |
| INC-008 | misleading-history, no-change-cause | PASS - identified: auth-service / capacity_saturation, 0.85 | PASS - identified: auth-service / capacity_saturation, 0.72 |
| INC-009 | noisy-neighbour, symptom-not-at-cause, decoy-config, multi-hop | PASS - identified: portfolio-service / bad_deployment, 0.72 | PASS - identified: portfolio-service / bad_deployment, 0.9 |
| INC-010 | misleading-report, decoy-config, symptom-not-at-cause, multi-hop | 2/3 - identified: order-service / bad_deployment, 0.7 | PASS - identified: order-service / bad_deployment, 0.9 |
| INC-011 | tool-failures, incomplete-results, decoy-deploy, misleading-report | PASS - identified: pricing-engine / config_change, 0.72 | PASS - identified: pricing-engine / config_change, 0.85 |
| INC-012 | misleading-report, decoy-deploy, tool-failures, symptom-not-at-cause | PASS - identified: ledger-db / resource_exhaustion, 0.83 | PASS - identified: ledger-db / resource_exhaustion, 0.9 |

## Accuracy by incident characteristic

| Tag | Incidents | Multi-agent | Single-agent baseline |
|---|---|---|---|
| cascading | 1 | 100% | 100% |
| clear-signal | 1 | 100% | 100% |
| conflicting-observations | 1 | 100% | 100% |
| decoy-config | 4 | 92% | 100% |
| decoy-deploy | 4 | 100% | 100% |
| external-dependency | 1 | 100% | 100% |
| incomplete-results | 1 | 100% | 100% |
| inconclusive | 1 | 100% | 100% |
| misleading-history | 2 | 100% | 100% |
| misleading-report | 6 | 94% | 100% |
| multi-hop | 2 | 83% | 100% |
| needs-wider-window | 1 | 100% | 100% |
| no-change-cause | 1 | 100% | 100% |
| noisy-neighbour | 1 | 100% | 100% |
| permanent-outage | 1 | 100% | 100% |
| slow-burn | 1 | 100% | 100% |
| symptom-not-at-cause | 5 | 93% | 100% |
| tool-failures | 4 | 100% | 100% |
| two-simultaneous-changes | 1 | 100% | 100% |

Metric definitions are in `eval/scoring.py`; per-run reports and traces are under `runs/` next to this file.
