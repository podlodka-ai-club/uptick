# Evaluation and Ablation Strategy

## 1. What we are actually evaluating

The simulator score is not the product goal.

It is a measurable proxy used to answer:

> Does this memory architecture make an agent adapt to an unknown world better?

We therefore need to measure both task performance and learning behavior.

## 2. Primary task metrics

For the current simulator:

- final balance;
- successful purchases;
- lost purchases;
- revenue;
- lost revenue;
- server cost;
- deployment cost;
- number of steps;
- completion/terminal status.

For the simulator, the default primary endpoint is
`final_balance_minor`. The primary paired contrast is:

```text
candidate.final_balance_minor - baseline.final_balance_minor
```

Completion/terminal status, lost purchases, lost revenue, server cost and
deployment cost are non-compensable guardrail metrics: an improvement in final
balance cannot mask a configured guardrail failure. Their numeric thresholds,
the minimum meaningful balance improvement and the uncertainty rule are set
from the Stage 0 baseline and pre-registered before any promotion run; until
then experiments are exploratory only.

Regardless of whether a promotion profile argues from the default primary
endpoint or another declared project metric, every simulator promotion profile
must include these canonical guardrails as non-compensable checks.

Prefer aggregate distributions across seeds, not one impressive run.

Each comparison uses the same seed/scenario and replicate matrix for baseline
and candidate conditions. A comparison block is identified by
`(environment_id, scenario_id, world_seed, replicate_index)` and contains one
requested run for each condition. The primary estimand is a paired
candidate-minus-baseline contrast across complete blocks; the promotion profile
must define its estimator, uncertainty level, clustering/weighting rule and the
denominator treatment for failed, retried and excluded attempts. Every attempt
is retained; pass@k or best-of-k selection must not be used as promotion
evidence.

## 3. Learning metrics

Also measure:

### Sample efficiency
How many training runs are needed before performance improves?

### Generalisation
Does learning help on unseen seeds/scenarios?

### Retention
Does old useful knowledge remain useful after new runs?

### Negative transfer
Does learned knowledge hurt in a different condition?

### Adaptation
How quickly does confidence change after contradictory evidence?

### Memory efficiency
How many stored artefacts and how many decision-context tokens are required?

### Retrieval precision
How often did retrieved knowledge actually relate to the decision/problem?

### Knowledge utility
How often did a retrieved lesson/hypothesis influence a better decision?

Every learning metric needs a versioned metric contract before it can be used
for promotion. The contract defines its unit and denominator, collection/test
schedule, formula, missing/failure handling and calibration. When a metric uses
human or model judgment, it also defines blinded inputs, assessor independence,
agreement measurement and adjudication. A model's self-reported explanation is
not evidence that a memory item causally improved the decision; causal utility
requires a declared counterfactual or controlled-replay method.

Minimum metric-contract schema:

```text
metric_id and version
construct being measured
unit of analysis and denominator
collection/test schedule
formula and direction
missing, failed and excluded-attempt handling
calibration/reference cases
uncertainty/reliability method
judge/blinding/agreement/adjudication protocol, when applicable
promotion role: primary | guardrail | diagnostic-only
```

## 4. Train and evaluation separation

Use at least two seed sets:

```text
training seeds
evaluation seeds
```

Recommended experimental loop:

```text
1. reset agent memory
2. train on training seeds
3. create an immutable, content-addressed frozen-memory snapshot
4. evaluate on held-out seeds
5. verify that all decision-visible memory ancestry resolves only to the frozen
   input snapshot
6. save full experiment metadata and every run attempt
```

Frozen evaluation is read-only for every memory path visible to a decision.
Memory writes are rejected or quarantined in an overlay that cannot be read
until the experiment ends. An unchanged input hash is insufficient if a run can
retrieve from an evaluation-written overlay or another evaluation attempt.

Before freezing, snapshot creation must walk transitive provenance and prove
that every source attempt belongs to the declared training phase and run matrix.
Unknown, evaluation-phase or cross-experiment ancestry is a hard failure.

Do not learn on the same runs used to claim final evaluation quality.

A separate "online adaptation" experiment may explicitly allow learning during
evaluation, but it must be labeled separately. It must use a separate
store/cache namespace and must not be reported as a frozen-memory result.

## 5. Baselines

Always retain:

### B0 — No memory
Use `NullMemory` / no persistent or cross-run memory modules. Bounded current-run
state that is part of the decision runtime may remain, but it must be identical
across all compared conditions and recorded in the experiment manifest.

### B1 — Current baseline
Existing lexical `Memory` behavior through a dedicated compatibility adapter.
It retains the legacy entry format and lexical scoring and is not the structured
episodic module.

### B2 — Raw episodic structured memory
The first-class structured episodic module with explicit action/outcome fields
and no legacy lexical compatibility adapter. No lesson or higher abstraction.

These baselines let us distinguish improvements due to architecture from improvements due simply to giving the LLM more previous text.

## 6. Ablation matrix

Each memory hypothesis needs an on/off comparison.

Example:

| Variant | Legacy lexical | Episodic | Lessons | World model | Consolidation | Advanced retrieval | Playbooks | Tool knowledge | Forgetting |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A0 | no | no | no | no | no | no | no | no | no |
| A1 | yes | no | no | no | no | no | no | no | no |
| A2 | no | yes | no | no | no | no | no | no | no |
| A3 | no | yes | yes | no | no | no | no | no | no |
| A4 | no | yes | yes | yes | no | no | no | no | no |
| A5 | no | yes | yes | yes | yes | no | no | no | no |
| A6 | no | yes | yes | yes | yes | yes | no | no | no |
| A7 | no | yes | yes | yes | yes | yes | yes | no | no |
| A8 | no | yes | yes | yes | yes | yes | yes | yes | no |
| A9 | no | yes | yes | yes | yes | yes | yes | yes | yes |

Also run targeted ablations:

```text
A6 minus world model
A6 minus contradiction tracking
A6 minus consolidation
A6 minus structured retrieval
A8 minus tool knowledge
```

For every module under promotion, the promotion profile must name its planned
baseline/candidate contrast. It must also state which interactions are
estimated, which are intentionally out of scope, and which require a factorial
or other dedicated follow-up. A cumulative A0–A9 trend alone does not identify
an individual module's contribution.

## 7. Feature flags are experimental controls

Feature flags are not only operational convenience.

They are part of the scientific method of the project.

Every new cognitive mechanism should have:

```text
enabled/disabled state
version
configuration
experiment label
```

Experiment results must record the full memory configuration.

They must record the resolved configuration and its stable fingerprint, not only
the user-supplied fragment. An ablation is invalid unless diagnostics prove that
each disabled module had zero construction, read, write, consolidation, cache
hit and context-contribution events. It must also prove that no decision-visible
item, initial-memory fragment or static prompt fragment originates from the
disabled mechanism or one of its prior snapshots.

## 8. Preventing benchmark overfitting

The simulator is known to developers. The agent must not receive hidden rules.

Avoid encoding simulator-specific knowledge in:

- prompts;
- memory module code;
- retrieval heuristics;
- initial lessons;
- "generic" helper functions that actually reveal rules.

The adapter may translate structures. It must not add hidden domain truth.

Each promotion experiment must attach a contamination-audit artefact covering
prompts, code/configuration, adapters, initial memory and imported fixtures. A
locked final holdout must include scenario or environment families that are not
merely different random seeds of the development worlds. Development results
may guide iteration; the locked final holdout may be used only after the
configuration and promotion profile are frozen.

A scenario/environment family is defined by its underlying causal mechanism,
available action/evidence topology and delayed-effect structure, not merely by a
random seed or cosmetic parameters. The locked holdout must contain at least one
family whose causal/action structure was not used for prompt, retrieval,
validator or module tuning. Its contents and access procedure are recorded in a
versioned holdout manifest before the promotion configuration is frozen.

## 9. Causal-learning sanity checks

A high final score does not prove the memory contains good knowledge.

Inspect whether:

- the same lesson is supported by several distinct contexts;
- contradictions lower confidence;
- spurious rules disappear over time;
- knowledge can predict outcomes before observing them;
- the agent can explain which evidence supports a hypothesis.

These diagnostics do not replace the candidate-activation gate. Under
`simulator-candidate-validation-v1@1.0`, activation requires at least two
completed eligible learning runs with distinct logical `run_id` values, spanning
at least two immutable `(environment_id, scenario_id)` contexts, complete
transitive provenance, all results from the declared deterministic
counter-evidence searches and zero unresolved contradictions. Retries of one
logical run do not add independent support, and frozen-evaluation runs are never
eligible. A manifest records every input and count needed to reproduce that
decision.

## 10. Suggested experiment artefact

Every experiment should output machine-readable data similar to:

```json
{
  "experiment_id": "...",
  "manifest_version": "...",
  "source_revision": "...",
  "agent_version": "...",
  "environment_version": "...",
  "llm": {
    "provider": "...",
    "model": "...",
    "settings": {},
    "prompt_hash": "..."
  },
  "resolved_config_fingerprint": "...",
  "memory_config": {
    "episodic": true,
    "lessons": true,
    "world_model": false
  },
  "training_seeds": [],
  "evaluation_seeds": [],
  "run_matrix": [],
  "input_memory_snapshot": {"id": "...", "hash": "..."},
  "output_memory_snapshot": {"id": "...", "hash": "..."},
  "attempts": [],
  "failure_and_exclusion_policy": {},
  "candidate_validation_policy": {
    "id": "simulator-candidate-validation-v1",
    "version": "1.0"
  },
  "promotion_profile": {
    "schema_version": "...",
    "primary_endpoint": {},
    "guardrails": [],
    "paired_analysis": {},
    "sample_size_justification": {},
    "planned_contrasts": [],
    "rollback_target": {}
  },
  "audit_retention_policy": {
    "id": "simulator-audit-retention-v1",
    "version": "1.0"
  },
  "raw_content_storage": {
    "policy_id": "simulator-raw-content-v1",
    "policy_version": "1.0",
    "prompts": "enabled",
    "observations": "enabled",
    "decision_traces": "enabled",
    "retention_policy_ref": "simulator-audit-retention-v1@1.0"
  },
  "metrics": {},
  "memory_metrics": {},
  "timing": {}
}
```

For frozen-memory evaluation, `output_memory_snapshot` is either identical to
the input snapshot or identifies a quarantined overlay that was never
decision-visible. Training and explicitly labeled online-adaptation experiments
may produce a different decision-visible output snapshot.

The resolved raw-content settings are evidence, not merely operator intent.
The experiment must prove that enabled body classes were captured after
mandatory secret removal, disabled body classes persisted no body, and no
snapshot or secondary artefact reintroduced excluded content.

## 11. Promotion rule

A module should move from "experimental" to "default" only if:

- it improves relevant held-out metrics or clearly improves another declared project metric;
- its result is reproducible;
- its complexity/cost is understood;
- removing it causes a measurable regression.

If none of those happen, keeping it disabled is a valid result.

These bullets are necessary but not a complete decision rule. Before running a
promotion experiment, its versioned promotion profile must pre-register:

- profile schema version;
- primary endpoint, formula, direction and units;
- evaluation horizon and early-terminal/timeout treatment;
- scenario weighting and aggregation rules;
- minimum practically meaningful effect;
- non-compensable safety/cost/negative-transfer guardrails;
- exact seed/scenario and replicate matrix;
- comparison block key, paired estimand and estimator;
- uncertainty level, clustering and multiplicity rule;
- sample-size or precision justification and minimum independent block count;
- treatment of failed, interrupted, retried and excluded runs;
- versioned contracts for every learning-behavior metric used in the decision;
- planned module contrasts and interaction scope;
- contamination-audit and locked-holdout references;
- criteria for reproducibility and rollback;
- tested rollback target/configuration, activation authority and post-rollback
  verification;
- audit-retention policy reference;
- accountable approver and decision-record location.

Without a complete profile, the experiment may be exploratory but cannot
promote a module to default.

The automatic candidate validator may activate individual memory items in the
simulator, but changing a module from `experimental` to `default` always requires
a recorded human approval in the promotion decision record.

Minimum promotion decision record:

```text
record ID and schema version
module ID/version and resolved config fingerprint
promotion-profile ID/version
experiment/result and evidence references
canonical guardrail results
decision: approve | reject | rollback
human approver identity and authentication method
timestamp
tested rollback target and verification result
record content hash/signature
```

The default configuration references this record by ID. The composition root
must verify that the record approves the exact module version and configuration
before accepting `status: default`.
