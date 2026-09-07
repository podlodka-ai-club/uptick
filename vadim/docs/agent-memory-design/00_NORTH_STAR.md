# North Star — General-Purpose Learning Memory for Agents

## 1. Problem

Most LLM agents have one of two weak forms of memory:

- raw conversational/history storage;
- retrieval of previously written notes or lessons.

That is useful, but it is not yet a learning system.

A learning agent should be able to transform repeated experience into a progressively more compact and useful internal model of the world.

The intended progression is:

```text
observations
    ↓
episodes
    ↓
lessons
    ↓
hypotheses / world knowledge
    ↓
strategies / playbooks
    ↓
better decisions
```

This transformation is not necessarily linear. New evidence can invalidate or weaken old conclusions.

## 2. Product goal

Create a reusable memory subsystem for autonomous agents that can operate in unknown environments and improve through experience **without retraining model weights**.

The LLM is the reasoning engine.
Memory is a separate cognitive subsystem.

Memory owns persistence, retrieval, consolidation and knowledge evolution. It does not own environment-specific business rules and it does not directly execute actions.

## 3. What success looks like

A successful system should demonstrate all of the following:

- **Cross-run learning:** later runs make measurably better decisions because of earlier runs.
- **Generalisation:** the agent can infer a useful rule from multiple different episodes instead of memorising one exact state.
- **Revision:** beliefs can lose confidence or be superseded when contradicted.
- **Transfer:** the memory subsystem can be attached to another environment without being rewritten.
- **Ablation:** each major memory mechanism can be independently disabled and its contribution measured.
- **Explainability:** for an important decision, we can inspect what memories/hypotheses influenced it.
- **Bounded complexity:** the amount of raw accumulated experience can grow while the working decision context stays bounded.
- **No oracle dependence:** learning comes from observable interaction/outcomes, not hidden simulator rules.

## 4. Simulator's role

The e-commerce/SRE simulator is a controlled experimental environment.

It provides:

- repeatable worlds through seeds;
- measurable outcomes such as revenue, lost revenue, infrastructure/deployment cost and final balance;
- delayed consequences;
- partial observability;
- actions that may be useful, useless or harmful;
- hidden regularities that can be learned.

It must never become part of the conceptual core of memory.

The simulator is also the only execution environment authorized for the first
implementation. References to Kubernetes, SSH, browsers or other real systems
describe future adapter compatibility, not approval for production or
privileged use. Such an adapter requires its own action-authorization,
least-privilege, rollback and rollout review before it may execute actions.

Simulator-specific concepts are translated through an adapter into generic concepts such as:

- observation;
- action;
- result;
- episode;
- objective metric;
- temporal relation;
- evidence.

## 5. Memory is not one thing

We deliberately separate:

### Experience
What happened.

### Knowledge
What we currently believe about how the world works.

### Strategy
What approach tends to work under some conditions.

### Skill / tool use
How to perform a specific operation.

These concepts may reference each other but are not interchangeable.

Example:

```text
Episode:
CPU rose after deployment X; requests started failing.

Lesson:
When failure begins immediately after a deployment, inspect the deployment before scaling.

World hypothesis:
Certain deployments can create application-level failures that extra capacity will not fix.

Strategy:
For post-deploy failures, diagnose release health before spending money on scaling.

Tool knowledge:
get_deployments reveals deployment history.
```

## 6. Hypotheses rather than immutable truth

World knowledge should be treated as hypotheses supported by evidence.

A knowledge item should be able to represent:

- evidence for;
- evidence against;
- confidence;
- scope / applicability conditions;
- provenance;
- last validation time;
- status: candidate, active, disputed, superseded.

Environment observations and every memory artefact derived from them are
untrusted data. Memory may inform a decision, but it must never grant a new
capability, weaken policy, or expand the set of actions authorized by the
environment adapter.

This prevents an early accidental correlation from becoming permanent doctrine.

## 7. Memory should generate new knowledge

Storage and retrieval are necessary but insufficient.

The memory subsystem should eventually perform offline/periodic processes that:

- merge duplicate observations;
- find recurring patterns;
- compare similar situations with different outcomes;
- identify contradictions;
- propose generalized lessons;
- connect previously unrelated memories;
- promote stable lessons into world hypotheses;
- demote or forget stale knowledge.

This is the "dreaming" / consolidation stage.

## 8. Architecture invariant

The North Star must survive replacing:

- the simulator;
- the LLM provider;
- the persistence technology;
- the retrieval algorithm;
- any optional memory module.

If replacing one of these forces changes across the whole system, the architecture has leaked.

## 9. Non-goals for the first implementation

Do not try to solve everything immediately.

Initial versions do not need:

- biologically accurate human memory;
- a perfect knowledge graph;
- learned neural embeddings;
- autonomous scientific discovery;
- model-weight fine-tuning;
- a giant generic ontology.

Prefer transparent mechanisms that can be inspected and benchmarked.

## 10. Decision rule for future work

Before implementing a feature, answer:

1. Which learning hypothesis does this feature represent?
2. Which stable interface owns it?
3. Can it be disabled independently?
4. How will its effect be measured?
5. Can it work in a different environment?
6. What evidence would convince us to remove it?

A feature that cannot answer these questions should not enter the core.
