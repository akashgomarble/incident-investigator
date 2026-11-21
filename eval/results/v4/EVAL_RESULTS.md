# Evaluation results

- LLM: `deepseek-ai/DeepSeek-V4.1-Flash`  |  trials per incident: 3  |  random tool faults: 0% of calls fail once (a retry succeeds), 0% of distinct calls fail on every attempt
- Incidents: 12  |  all systems use the same tools, fault injection, tool-call budget, evidence rules and scorer

## Headline comparison

| Metric | Multi-agent | Single-agent baseline | Multi-agent without Critic (ablation) |
|---|---|---|---|
| Root-cause accuracy (all incidents) | 89% | 94% | 86% |
|   ...runs correct, with 95% Wilson interval | 32/36 (75%-96%) | 34/36 (82%-98%) | 31/36 (71%-94%) |
|   ...on incidents with a determinable cause | 88% | 100% | 85% |
|   ...correctly inconclusive when it should be | 100% | 33% | 100% |
| Correct service ranked #1 | 100% | 100% | 94% |
| Correct cause within top-3 hypotheses | 100% | 100% | 97% |
| Triggering change id identified | 83% | 100% | 89% |
| Wrong but stated as identified (lower is better) | 0% | 6% | 3% |
| Blamed the planted decoy (lower is better) | 0% | 0% | 0% |
| Abstained although answerable (lower is better) | 12% | 0% | 12% |
| Evidence grounding: cited findings verified | 99% | 100% | 99% |
| Evidence grounding: quotes found in tool output | 98% | 99% | 98% |
| Causal-chain hops backed by verified findings | 96% | 99% | 93% |
| Key-evidence recall | 90% | 94% | 81% |
| Mean confidence in the top hypothesis | 0.68 | 0.84 | 0.55 |
| Tool calls that returned an error after retries, per incident (injected faults and bad requests) | 4.2 | 1.1 | 1.8 |
| Tool calls recovered by a retry, per incident | 0.2 | 0.2 | 0.1 |
| Data gaps listed in the report, per incident | 8.8 | 3.6 | 8.0 |
| Tool calls per incident (mean) | 44.6 | 23.4 | 23.1 |
| LLM calls per incident (mean) | 27.3 | 6.0 | 12.8 |
| Latency per incident, mean (s) | 280.2 | 71.4 | 127.7 |
| Latency per incident, median (s) | 263.7 | 64.7 | 121.8 |
| Input tokens per incident (mean) | 187,619 | 76,286 | 64,923 |
| Output tokens per incident (mean) | 36,934 | 8,387 | 16,783 |
| Total tokens per incident (mean) | 224,553 | 84,673 | 81,706 |
| Crashed runs | 0 | 0 | 0 |

## Is stated confidence informative?

Runs grouped by the confidence the system stated for its top hypothesis. A useful confidence is one where the
higher bands are correct more often. Cells are correct/runs.

| Stated confidence | Multi-agent | Single-agent baseline | Multi-agent without Critic (ablation) |
|---|---|---|---|
| below 0.50 (low) | 3/5 | 1/1 | 3/7 |
| 0.50 to 0.74 (medium) | 16/18 | 2/4 | 28/29 |
| 0.75 and above (high) | 13/13 | 31/31 | no runs |

## Per incident

| Incident | Tags | Multi-agent: result (top hypothesis, confidence) | Single-agent baseline: result (top hypothesis, confidence) | Multi-agent without Critic (ablation): result (top hypothesis, confidence) |
|---|---|---|---|---|
| INC-001 | clear-signal | PASS - identified: order-service / bad_deployment, 0.68 | PASS - identified: order-service / bad_deployment, 0.86 | PASS - identified: order-service / bad_deployment, 0.6 |
| INC-002 | misleading-report, decoy-deploy, symptom-not-at-cause | PASS - identified: portfolio-service / config_change, 0.78 | PASS - identified: portfolio-service / config_change, 0.93 | 2/3 - identified: portfolio-service / config_change, 0.6 |
| INC-003 | misleading-report, decoy-deploy, external-dependency | 2/3 - inconclusive: external exchange partner API api.exchange-partner.example (called via exchange-gateway) / dependency_failure, 0.6 | PASS - identified: exchange-api / dependency_failure, 0.85 | PASS - identified: exchange-api (api.exchange-partner.example) / dependency_failure, 0.6 |
| INC-004 | slow-burn, needs-wider-window, misleading-report, decoy-config | PASS - identified: notification-service / bad_deployment, 0.78 | PASS - identified: notification-service / bad_deployment, 0.85 | PASS - identified: notification-service / bad_deployment, 0.6 |
| INC-005 | conflicting-observations, decoy-config, misleading-history | PASS - identified: payment-service (outbound bank mTLS client certificate CN=payments-client.orangeinvest.internal) / credential_expiry, 0.82 | PASS - identified: payment-service / credential_expiry, 0.9 | PASS - identified: payment-service / credential_expiry, 0.6 |
| INC-006 | tool-failures, cascading, symptom-not-at-cause | 2/3 - inconclusive: redis-cache / infrastructure_failure, 0.5 | PASS - identified: redis-cache / infrastructure_failure, 0.85 | 2/3 - identified: redis-cache / infrastructure_failure, 0.55 |
| INC-007 | inconclusive, tool-failures, permanent-outage, two-simultaneous-changes | PASS - inconclusive: auth-service / config_change, 0.42 | 1/3 - identified: auth-service / bad_deployment, 0.62 | PASS - inconclusive: auth-service / config_change, 0.42 |
| INC-008 | misleading-history, no-change-cause | 1/3 - identified: auth-service / capacity_saturation, 0.55 | PASS - identified: auth-service / capacity_saturation, 0.72 | 1/3 - inconclusive: auth-service / capacity_saturation, 0.45 |
| INC-009 | noisy-neighbour, symptom-not-at-cause, decoy-config, multi-hop | PASS - identified: portfolio-service / bad_deployment, 0.62 | PASS - identified: portfolio-service / bad_deployment, 0.85 | 2/3 - identified: portfolio-service / bad_deployment, 0.6 |
| INC-010 | misleading-report, decoy-config, symptom-not-at-cause, multi-hop | PASS - identified: order-service / bad_deployment, 0.72 | PASS - identified: order-service / bad_deployment, 0.86 | PASS - identified: order-service / bad_deployment, 0.6 |
| INC-011 | tool-failures, incomplete-results, decoy-deploy, misleading-report | PASS - identified: pricing-engine / config_change, 0.72 | PASS - identified: pricing-engine / config_change, 0.86 | PASS - identified: pricing-engine / config_change, 0.6 |
| INC-012 | misleading-report, decoy-deploy, tool-failures, symptom-not-at-cause | PASS - identified: ledger-db / resource_exhaustion, 0.82 | PASS - identified: ledger-db / resource_exhaustion, 0.9 | PASS - identified: ledger-db / resource_exhaustion, 0.6 |

## Accuracy by incident characteristic

| Tag | Incidents | Multi-agent | Single-agent baseline | Multi-agent without Critic (ablation) |
|---|---|---|---|---|
| cascading | 1 | 67% | 100% | 67% |
| clear-signal | 1 | 100% | 100% | 100% |
| conflicting-observations | 1 | 100% | 100% | 100% |
| decoy-config | 4 | 100% | 100% | 92% |
| decoy-deploy | 4 | 92% | 100% | 92% |
| external-dependency | 1 | 67% | 100% | 100% |
| incomplete-results | 1 | 100% | 100% | 100% |
| inconclusive | 1 | 100% | 33% | 100% |
| misleading-history | 2 | 67% | 100% | 67% |
| misleading-report | 6 | 94% | 100% | 94% |
| multi-hop | 2 | 100% | 100% | 83% |
| needs-wider-window | 1 | 100% | 100% | 100% |
| no-change-cause | 1 | 33% | 100% | 33% |
| noisy-neighbour | 1 | 100% | 100% | 67% |
| permanent-outage | 1 | 100% | 33% | 100% |
| slow-burn | 1 | 100% | 100% | 100% |
| symptom-not-at-cause | 5 | 93% | 100% | 80% |
| tool-failures | 4 | 92% | 83% | 92% |
| two-simultaneous-changes | 1 | 100% | 33% | 100% |

Metric definitions are in `eval/scoring.py`; per-run reports and traces are under `runs/` next to this file.
