# Incident investigation report: INC-001
*System: single_agent | outcome: **root_cause_identified** | ended: agent_submitted*

## Summary
A recent deployment (v2.14.0) added a margin‑check validator that does not handle missing marginProfile data, resulting in NullPointerExceptions and a surge of HTTP 500 errors in order-service.

## Probable root cause
**Bad deployment introduced a null‑pointer in the new margin check validator** -- bad_deployment in `order-service`, triggered by `dep-5101` (confidence 0.78, high)

## Ranked hypotheses
| # | Hypothesis | Category | Service | Trigger | Confidence | Status |
|---|---|---|---|---|---|---|
| 1 | H1: Bad deployment introduced a null‑pointer in the new margin check validator | bad_deployment | order-service | dep-5101 | 0.78 (high) | active |
| 2 | H2: Data issue: marginProfile missing in account records | data_issue | account-data-pipeline | unknown | 0.45 (low) | active |

### H1: Bad deployment introduced a null‑pointer in the new margin check validator
The v2.14.0 deployment added a margin check that assumes a non‑null marginProfile; for many accounts the profile is missing, causing a NullPointerException and HTTP 500 responses.

**Causal chain**
1. deployment v2.14.0 -> margin check validator expects marginProfile [F3]
2. missing marginProfile for accounts -> NullPointerException [F1]
3. NPE propagates to HTTP 500 -> error_rate spike [F2]

**Supporting evidence**
  - `F1` ✔ order-service logged HTTP 500 errors with NullPointerException: marginProfile is null for various accounts
  - `F2` ✔ order-service error_rate metric spiked from ~0.2% to ~18% starting at 12:12 UTC
  - `F3` ✔ order-service was deployed version v2.14.0 at 12:10 UTC with summary 'Refactor order validation; add margin check for F&O orders'
**Contradicting evidence**
  - (none)
**Assumptions**
  - marginProfile is fetched from account data and can be null for some accounts
  - the new validator does not guard against null

### H2: Data issue: marginProfile missing in account records
A recent change in the account data pipeline stopped populating marginProfile for many accounts; order-service code (unchanged) dereferences it, causing NPEs.

**Causal chain**
1. account data pipeline stopped providing marginProfile *(assumed)*
2. order-service dereferences null marginProfile -> NPE [F1]
3. NPE leads to HTTP 500 -> error_rate spike [F2]

**Supporting evidence**
  - `F1` ✔ order-service logged HTTP 500 errors with NullPointerException: marginProfile is null for various accounts
  - `F2` ✔ order-service error_rate metric spiked from ~0.2% to ~18% starting at 12:12 UTC
**Contradicting evidence**
  - `F3` ✔ order-service was deployed version v2.14.0 at 12:10 UTC with summary 'Refactor order validation; add margin check for F&O orders'
**Assumptions**
  - a data pipeline change occurred shortly before the incident
  - order-service code has always expected marginProfile to be present

## Facts (verified observations)
- The incident was triggered by a code change, not a config change or resource exhaustion. [F1, F2, F3, F4, F5]

## Assumptions (not verified)
- marginProfile is required for order validation and is normally non‑null
- no other services (e.g., pricing-engine) emit related errors, indicating the problem is internal to order-service

## Timeline
- `2026-03-03T12:10:00Z` order-service deployed v2.14.0 with new margin check [F3]
- `2026-03-03T12:12:44Z` first observed NullPointerException in order-service logs [F1]
- `2026-03-03T12:12:00Z` error_rate metric spikes to ~18% [F2]

## Recommended actions
- **[immediate]** Rollback order-service to previous version (v2.13.x) or hot‑fix the validator to guard against null marginProfile -- Stops the NullPointerException and restores normal order placement [F1, F3]
- **[short_term]** Add defensive null checks and logging for marginProfile in order validation code -- Prevents similar failures if data gaps reappear [F1]
- **[short_term]** Investigate account data pipeline to confirm marginProfile population and add validation -- Addresses the alternative data‑issue hypothesis and closes the data gap

## Data gaps
- No direct evidence of why marginProfile is null for the affected accounts (e.g., account service logs or data pipeline change records).

## Investigation stats
- latency 159.73s, LLM calls 9, tokens 44793 (in 40413 / out 4380)
- tool calls 8 (0 failed, 0 partial, 0 recovered by retry), findings 6/6 verified
