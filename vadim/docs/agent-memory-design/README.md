# Agent Memory Design — Documentation Index

This directory is the entry point for implementing a **general-purpose
self-improving agent memory system** on top of the isolated baseline under
`vadim/src/uptick_agent`.

Stages 1–3 are **complete**. The accepted Stage 1 freeze is recorded in
[`STAGE_1_CONTRACT_FREEZE.md`](STAGE_1_CONTRACT_FREEZE.md); the Stage 2/3 runtime
integration and its deliberate deferrals are recorded in
[`STAGE_2_3_IMPLEMENTATION.md`](STAGE_2_3_IMPLEMENTATION.md). Stage 0 currently
provides an offline preregistration/reporting scaffold, not collected live
baseline evidence. Each later stage retains its own implementation and evidence
gate; examples marked conceptual are not substitutes for those gates.

The initial simulator policy profile is now owner-approved:

- candidate activation requires support from at least two independent completed
  eligible learning runs spanning at least two distinct scenario/environment
  contexts, complete
  transitive provenance, all known counter-evidence and zero unresolved
  contradictions;
- raw prompt, observation and trace bodies plus snapshots are retained for at
  least 90 days; experiment summaries and validation, promotion, approval and
  rollback records are kept for the project lifetime; legal/incident holds
  override deletion, which must be governed by a versioned policy approved by
  the project owner;
- raw prompt, observation and trace bodies are enabled for the simulator
  profile but independently configurable; credentials and secrets are never
  persisted.

The normative details and policy identifiers live in
[`01_ARCHITECTURE_RFC.md`](01_ARCHITECTURE_RFC.md). Implementations fail closed
when a required policy reference or check is missing and must not invent local
defaults.

## North Star

We are **not** building an agent that learns the hidden rules of one simulator.

We are building a reusable agent architecture that can:

1. observe an unknown environment;
2. accumulate experience across runs;
3. extract reusable lessons from experience;
4. form and revise hypotheses about how the world works;
5. retrieve only the knowledge relevant to the current situation;
6. improve its decisions over time;
7. remain modular enough that every memory capability can be enabled, disabled, replaced, and benchmarked independently.

The e-commerce/SRE simulator is the **first experimental world** used to test these claims.

## Reading order

For an implementation agent, read these documents in order:

1. [`00_NORTH_STAR.md`](00_NORTH_STAR.md) — what we are ultimately building and what we explicitly are not building.
2. [`01_ARCHITECTURE_RFC.md`](01_ARCHITECTURE_RFC.md) — module boundaries, orchestration, dependency rules, Mermaid diagrams, LLM abstraction.
3. [`02_MEMORY_MODEL.md`](02_MEMORY_MODEL.md) — memory types, lifecycle, consolidation/dreaming, confidence, links and retrieval.
4. [`03_IMPLEMENTATION_PLAN.md`](03_IMPLEMENTATION_PLAN.md) — executable staged plan, dependencies, parallelisation and Definition of Done.
5. [`04_EVALUATION_AND_ABLATIONS.md`](04_EVALUATION_AND_ABLATIONS.md) — how to prove which mechanisms actually help.
6. [`05_ARCHITECTURE_TESTS.md`](05_ARCHITECTURE_TESTS.md) — fitness functions that prevent abstraction leakage and architectural decay.

## Current implementation

The current local baseline already has several useful seams:

- `AgentRunner` owns the runtime loop.
- `DecisionModel` remains the runner port while a small compatibility bridge
  drives it through the provider-neutral `LlmClient` registry.
- `Environment` is already a port.
- `AgentRunner` consumes one `AgentMemory` boundary and receives a normalized
  `DecisionMemoryContext`; legacy `remember/recall/clear` behavior is contained
  inside the compatibility runtime.
- `InMemoryMemory` is deliberately simple and `NullMemory` already provides a no-memory control group.
- simulator-specific code is under `src/uptick_agent/simulator`.
- OpenAI and Codex implementations are separated under `src/uptick_agent/llm`
  and provider SDK objects stay inside their adapters.

The proposed architecture should **evolve these seams rather than rewrite the whole agent at once**.

## Core implementation rule

Whenever a new capability is proposed, ask:

> Can this capability be turned off without changing the rest of the agent?

If the answer is no, the abstraction boundary is probably wrong.

Every memory mechanism must be an independently configurable module.
The orchestrator composes enabled modules.
The decision model consumes a stable context contract and must not know which memory modules produced it.

## Suggested target package shape

```text
uptick_agent/
  agent/
    runner.py
    decision.py
    context.py

  memory/
    contracts.py
    orchestrator.py
    config.py
    stores/
      in_memory.py
      sqlite.py
    compatibility/
      legacy_jsonl.py
    modules/
      episodic/
      lessons/
      world_model/
      playbooks/
      tool_knowledge/
      consolidation/
      forgetting/
    retrieval/
    links/

  llm/
    contracts.py
    registry.py
    providers/
      codex.py
      openai.py
      anthropic.py
      openai_compatible.py

  environment/
    contracts.py

  adapters/
    simulator/

  experiments/
    runner.py
    ablations.py
    metrics.py

  architecture_tests/
```

This is a target direction, not a required one-shot folder migration.
