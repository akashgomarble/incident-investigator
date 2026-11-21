# Evaluation results

- LLM: `deepseek-ai/DeepSeek-V4.1-Flash`  |  trials per incident: 3  |  random tool-fault rate (chaos): 0.0
- Incidents: 12  |  both systems use the same tools, fault injection, tool-call budget, evidence rules and scorer

## Headline comparison

| Metric | Multi-agent | Single-agent baseline |
|---|---|---|
| Root-cause accuracy (all incidents) | 94% | 100% |
|   ...on incidents with a determinable cause | 94% | 100% |
|   ...correctly inconclusive when it should be | 100% | 100% |
| Correct service ranked #1 | 100% | 100% |
| Correct cause within top-3 hypotheses | 100% | 100% |
| Triggering change id identified | 89% | 100% |
| Wrong but stated as identified (lower is better) | 0% | 0% |
| Blamed the planted decoy (lower is better) | 0% | 0% |
| Abstained although answerable (lower is better) | 6% | 0% |
| Evidence grounding: cited findings verified | 99% | 98% |
| Evidence grounding: quotes found in tool output | 99% | 97% |
| Causal-chain hops backed by verified findings | 96% | 99% |
| Key-evidence recall | 96% | 88% |
| Tool calls per incident (mean) | 49.6 | 22.7 |
| LLM calls per incident (mean) | 30.2 | 5.8 |
| Latency per incident, mean (s) | 198.1 | 49.4 |
| Latency per incident, median (s) | 179.3 | 47.8 |
| Input tokens per incident (mean) | 214,027 | 72,680 |
| Output tokens per incident (mean) | 40,170 | 8,252 |
| Total tokens per incident (mean) | 254,197 | 80,932 |
| Crashed runs | 0 | 0 |

## Per incident

| Incident | Tags | Multi-agent: result (top hypothesis, confidence) | Single-agent baseline: result (top hypothesis, confidence) |
|---|---|---|---|
| INC-001 | clear-signal | PASS - identified: order-service / bad_deployment, 0.55 | PASS - identified: order-service / bad_deployment, 0.9 |
| INC-002 | misleading-report, decoy-deploy, symptom-not-at-cause | PASS - identified: portfolio-service / config_change, 0.85 | PASS - identified: portfolio-service / config_change, 0.92 |
| INC-003 | misleading-report, decoy-deploy, external-dependency | PASS - identified: api.exchange-partner.example (external exchange partner, reached via exchange-gateway) / dependency_failure, 0.82 | PASS - identified: exchange-api / dependency_failure, 0.86 |
| INC-004 | slow-burn, needs-wider-window, misleading-report, decoy-config | PASS - identified: notification-service / bad_deployment, 0.72 | PASS - identified: notification-service / bad_deployment, 0.86 |
| INC-005 | conflicting-observations, decoy-config, misleading-history | PASS - identified: payment-service / credential_expiry, 0.68 | PASS - identified: payment-service / credential_expiry, 0.92 |
| INC-006 | tool-failures, cascading, symptom-not-at-cause | PASS - identified: redis-cache / infrastructure_failure, 0.78 | PASS - identified: redis-cache / infrastructure_failure, 0.86 |
| INC-007 | inconclusive, tool-failures, permanent-outage, two-simultaneous-changes | PASS - inconclusive: auth-service / bad_deployment, 0.4 | PASS - inconclusive: auth-service / bad_deployment, 0.5 |
| INC-008 | misleading-history, no-change-cause | 2/3 - identified: auth-service / capacity_saturation, 0.6 | PASS - identified: auth-service / capacity_saturation, 0.8 |
| INC-009 | noisy-neighbour, symptom-not-at-cause, decoy-config, multi-hop | PASS - identified: portfolio-service / bad_deployment, 0.88 | PASS - identified: portfolio-service / bad_deployment, 0.9 |
| INC-010 | misleading-report, decoy-config, symptom-not-at-cause, multi-hop | 2/3 - inconclusive: order-service / bad_deployment, 0.5 | PASS - identified: order-service / bad_deployment, 0.87 |
| INC-011 | tool-failures, incomplete-results, decoy-deploy, misleading-report | PASS - identified: pricing-engine / config_change, 0.82 | PASS - identified: pricing-engine / config_change, 0.9 |
| INC-012 | misleading-report, decoy-deploy, tool-failures, symptom-not-at-cause | PASS - identified: ledger-db / resource_exhaustion, 0.78 | PASS - identified: ledger-db / resource_exhaustion, 0.9 |

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
| misleading-history | 2 | 83% | 100% |
| misleading-report | 6 | 94% | 100% |
| multi-hop | 2 | 83% | 100% |
| needs-wider-window | 1 | 100% | 100% |
| no-change-cause | 1 | 67% | 100% |
| noisy-neighbour | 1 | 100% | 100% |
| permanent-outage | 1 | 100% | 100% |
| slow-burn | 1 | 100% | 100% |
| symptom-not-at-cause | 5 | 93% | 100% |
| tool-failures | 4 | 100% | 100% |
| two-simultaneous-changes | 1 | 100% | 100% |

Metric definitions are in `eval/scoring.py`; per-run reports and traces are under `runs/` next to this file.
