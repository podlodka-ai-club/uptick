# Closing one learning cycle

Requested 2026-09-05 after the architecture correction. This is a bounded
mechanism experiment, not completion of the main simulator's effectiveness gate.

## Acceptance

The same frozen agent code, model, prompt, generation settings and decision
budget must run both conditions. Earlier experience must pass through the real
runner, episodic store, independent world-pattern validator and context merge.
After closing and reopening SQLite, a subsequent run must receive the retained
hypothesis with source provenance. The report must show the actual selected
memory IDs, model decision and observed incident outcome for each condition.
No-memory failures, training failures and timeouts remain in the denominator.
A zero improvement is a result, not permission to rewrite or omit attempts.

## Controlled incident simulator

Use a small local simulator fixture outside memory core. It exposes four opaque
incident codes and two typed repair choices. Its finite public tool surface and
observable repair results are the only information given to the agent. The
incident-to-repair mapping is held by the environment/evaluator, never injected
into the decision prompt or memory as a prewritten lesson.

Training includes two distinct declared fixture contexts per incident type.
Context content hashes must cover the actual fixture specifications and adapter
implementation. These are identities of this controlled fixture, not substitute
identities for the hosted SRE world. All contexts share a designed causal family:
this test does not demonstrate independent-family generalisation.

The agent discovers outcomes through actual typed actions. Every transition,
including unsuccessful repair attempts, is recorded through the normal memory
port. Completed training runs can supply support under the existing validator;
failed runs remain available as counter-evidence. Do not change activation
thresholds to make the experiment pass.

Use the existing typed `ApplyFix(message=...)` action with two listed opaque
repair identifiers. World-pattern query projects the observable incident code,
`action.message` repair identifier,
and observed recovery result. The resulting claim is a scoped observational
regularity with independently recomputed support/counter evidence. It is not
an unrestricted natural-language causal theory.

## Evaluation

Freeze training records and derived hypotheses before testing. Reopen storage
and use the existing verified snapshot reader with isolated evaluation writes.
Keep episodic recording enabled but its decision-context allowance at zero in
the hypothesis condition, so raw episodes do not confound whether validated
hypotheses reached the model. World hypotheses use the normal independent
validator and context budget. Neither condition receives a hand-written lesson.

Run two new evaluation variants for each of the four incident codes, paired
with no memory and frozen hypothesis memory. Use at most three decisions for
each training run and one repair decision per evaluation case; choose and seal
wall budgets and generation settings before calling the provider. A small
predeclared development set is sufficient for a mechanism check; no powered
statistical or production-default claim follows. Keep golden mappings out of
training annotations, candidate generation, queries and model-visible context.

Use the actual configured LLM provider, not a fake decision policy that branches
on the presence of a memory item. Deterministic test doubles are only for
regression tests of storage, isolation, lifecycle and report accounting.

Before external model calls, retain the source revision/hash, dependency lock,
fixture definitions/hashes, case order, budgets, model settings and both
condition definitions. Never overwrite a prior attempt or change its limits.
Retain a concise durable report and ignored raw traces/SQLite/source capsules.

## Limits

This proves only the observed scope: learning from a controlled local simulator,
durable retrieval, and any measured decision difference in those paired cases.
It does not prove hosted SRE success, superiority to Alex's agent, broader
transfer, causal-family holdout performance or upstream xMemory effectiveness.
Raw SRE traces with unknown world identity remain ineligible for promoted world
knowledge under the existing policy.
