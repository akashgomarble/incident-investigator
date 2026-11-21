# Evaluation results

- LLM: `openai/gpt-oss-120b`  |  trials per incident: 2  |  random tool faults: 0% of calls fail once (a retry succeeds), 0% of distinct calls fail on every attempt
- Incidents: 12  |  all systems use the same tools, fault injection, tool-call budget, evidence rules and scorer

## Headline comparison

| Metric | Multi-agent | Single-agent baseline |
|---|---|---|
| Root-cause accuracy (all incidents) | 29% | 17% |
|   ...runs correct, with 95% Wilson interval | 7/24 (15%-49%) | 4/24 (7%-36%) |
|   ...on incidents with a determinable cause | 23% | 18% |
|   ...correctly inconclusive when it should be | 100% | 0% |
| Correct service ranked #1 | 36% | 18% |
| Correct cause within top-3 hypotheses | 36% | 18% |
| Triggering change id identified | 8% | 25% |
| Wrong but stated as identified (lower is better) | 25% | 8% |
| Blamed the planted decoy (lower is better) | 0% | 4% |
| Abstained although answerable (lower is better) | 50% | 73% |
| Evidence grounding: cited findings verified | 96% | 32% |
| Evidence grounding: quotes found in tool output | 97% | 32% |
| Causal-chain hops backed by verified findings | 50% | 31% |
| Key-evidence recall | 29% | 10% |
| Mean confidence in the top hypothesis | 0.56 | 0.80 |
| Tool calls that returned an error after retries, per incident (injected faults and bad requests) | 1.2 | 0.2 |
| Tool calls recovered by a retry, per incident | 0.0 | 0.1 |
| Data gaps listed in the report, per incident | 3.9 | 0.7 |
| Tool calls per incident (mean) | 13.8 | 9.2 |
| LLM calls per incident (mean) | 21.0 | 9.8 |
| Latency per incident, mean (s) | 622.3 | 531.4 |
| Latency per incident, median (s) | 549.1 | 500.5 |
| Input tokens per incident (mean) | 60,680 | 46,074 |
| Output tokens per incident (mean) | 12,334 | 3,617 |
| Total tokens per incident (mean) | 73,014 | 49,691 |
| Crashed runs | 0 | 0 |

## Is stated confidence informative?

Runs grouped by the confidence the system stated for its top hypothesis. A useful confidence is one where the
higher bands are correct more often. Cells are correct/runs.

| Stated confidence | Multi-agent | Single-agent baseline |
|---|---|---|
| below 0.50 (low) | 1/3 | no runs |
| 0.50 to 0.74 (medium) | 6/12 | 0/2 |
| 0.75 and above (high) | 0/1 | 4/6 |

## Per incident

| Incident | Tags | Multi-agent: result (top hypothesis, confidence) | Single-agent baseline: result (top hypothesis, confidence) |
|---|---|---|---|
| INC-001 | clear-signal | PASS - identified: order-service / bad_deployment, 0.6 | PASS - identified: order-service / bad_deployment, 0.86 |
| INC-002 | misleading-report, decoy-deploy, symptom-not-at-cause | 1/2 - inconclusive: portfolio-service / config_change, 0.45 | 1/2 - inconclusive: None / None, None |
| INC-003 | misleading-report, decoy-deploy, external-dependency | FAIL - identified: order-service / bad_deployment, 0.5 | FAIL - inconclusive: None / None, None |
| INC-004 | slow-burn, needs-wider-window, misleading-report, decoy-config | FAIL - inconclusive: None / None, None | FAIL - inconclusive: None / None, None |
| INC-005 | conflicting-observations, decoy-config, misleading-history | 1/2 - identified: payment-service / credential_expiry, 0.6 | 1/2 - inconclusive: None / None, None |
| INC-006 | tool-failures, cascading, symptom-not-at-cause | FAIL - inconclusive: market-data-service / dependency_failure, 0.28 | FAIL - inconclusive: market-data-service / resource_exhaustion, 0.68 |
| INC-007 | inconclusive, tool-failures, permanent-outage, two-simultaneous-changes | PASS - inconclusive: auth-service / bad_deployment, 0.6 | FAIL - inconclusive: None / None, None |
| INC-008 | misleading-history, no-change-cause | 1/2 - identified: auth-service / capacity_saturation, 0.6 | FAIL - inconclusive: None / None, None |
| INC-009 | noisy-neighbour, symptom-not-at-cause, decoy-config, multi-hop | FAIL - inconclusive: ledger-db / resource_exhaustion, 0.6 | FAIL - identified: ledger-db / config_change, 0.86 |
| INC-010 | misleading-report, decoy-config, symptom-not-at-cause, multi-hop | FAIL - identified: order-service / dependency_failure, 0.62 | FAIL - inconclusive: None / None, None |
| INC-011 | tool-failures, incomplete-results, decoy-deploy, misleading-report | FAIL - identified: pricing-engine / resource_exhaustion, 0.5 | FAIL - inconclusive: None / None, None |
| INC-012 | misleading-report, decoy-deploy, tool-failures, symptom-not-at-cause | FAIL - inconclusive: None / None, None | FAIL - inconclusive: None / None, None |

## Accuracy by incident characteristic

| Tag | Incidents | Multi-agent | Single-agent baseline |
|---|---|---|---|
| cascading | 1 | 0% | 0% |
| clear-signal | 1 | 100% | 100% |
| conflicting-observations | 1 | 50% | 50% |
| decoy-config | 4 | 12% | 12% |
| decoy-deploy | 4 | 12% | 12% |
| external-dependency | 1 | 0% | 0% |
| incomplete-results | 1 | 0% | 0% |
| inconclusive | 1 | 100% | 0% |
| misleading-history | 2 | 50% | 25% |
| misleading-report | 6 | 8% | 8% |
| multi-hop | 2 | 0% | 0% |
| needs-wider-window | 1 | 0% | 0% |
| no-change-cause | 1 | 50% | 0% |
| noisy-neighbour | 1 | 0% | 0% |
| permanent-outage | 1 | 100% | 0% |
| slow-burn | 1 | 0% | 0% |
| symptom-not-at-cause | 5 | 10% | 10% |
| tool-failures | 4 | 25% | 0% |
| two-simultaneous-changes | 1 | 100% | 0% |

Metric definitions are in `eval/scoring.py`; per-run reports and traces are under `runs/` next to this file.
