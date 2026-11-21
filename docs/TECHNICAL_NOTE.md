# Technical note

**Key decisions**

- *Blackboard, not a pipeline.* Six agents read and write one typed investigation state; no agent
  calls another. The Orchestrator schedules a task graph in parallel waves. The Critic can send
  evidence requests back, which become new tasks. Hypotheses keep their ids across rounds, so
  beliefs are revised instead of regenerated.
- *Code owns what can be checked mechanically.* Ids, budgets, retries, termination, the confidence
  cap, the outcome rule and the fact/assumption split are code. Models plan, read telemetry, form
  and attack hypotheses, and write.
- *Evidence is verified, not trusted.* Every finding cites a tool call and a verbatim quote, which
  code checks against the recorded output. Unverified findings cannot become facts.
- *Failures are typed.* Tools return structured errors. The executor retries with backoff, opens a
  circuit breaker on a dead source and never hides a partial result. Every agent has a coded
  fallback, so a run always ends with a report.
- *A fair baseline.* The single agent gets the same tools, faults, budget, checks and scorer.

**Results** (12 incidents x 3 trials, DeepSeek-V4.1-Flash, final round)

The single agent scored 94% root-cause accuracy (34/36), the team 89% (32/36); the intervals
overlap. The team used 2.7x the tokens (225k against 85k) and 3.9x the latency. They fail
differently: the team's four misses are abstentions; the single agent's two are a root cause
claimed on the undeterminable incident. Removing the Critic cuts 64% of the team's tokens for three
points of accuracy. Random tool faults degraded neither. The team does not pay for itself on accuracy.

**Trade-offs**

- Review rounds buy an audit trail and lower stated confidence for minutes of latency. A sensible
  production default is single agent first, team on escalation.
- Strict structure makes runs inspectable, but hand-offs lose detail.
- A conservative Critic avoids confident wrong answers but abstains on some answerable incidents.

**Limitations**

- Prompts were tuned on the evaluation set; there is no held-out split. Across four rounds the team
  scored 94%, 100%, 97% and 89%, so small differences are noise.
- 12 synthetic incidents do not separate the systems: a strong model solves almost all alone.
- The weaker-model run was dominated by provider timeouts and proves little. It did show that a
  deadline-cut team run could claim a root cause (25% wrong-but-confident); such runs are now
  forced to "inconclusive" (tested and replayed on saved traces).
- Quote verification proves the text exists, not that it was interpreted correctly.
- The wall-clock limit is checked between model calls and can be overshot. State is in memory.

**Improvements, in order**

1. A held-out incident set with larger topologies and evidence that exceeds one context window,
   where decomposition should win.
2. Adaptive routing: single agent first, Critic only when confidence is low or hypotheses are close.
3. Pass change ids as typed fields end to end.
4. Prompt caching, smaller digests and a cheaper model for specialists.
5. Semantic entailment checks, persistence and resume, real telemetry adapters.
