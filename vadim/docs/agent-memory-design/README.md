# Agent Memory Design — Documentation Index

This directory is the entry point for implementing a **general-purpose
self-improving agent memory system** on top of the isolated baseline under
`vadim/src/uptick_agent`.

Stages 1–5 are **complete**. The accepted Stage 1 freeze is recorded in
[`STAGE_1_CONTRACT_FREEZE.md`](STAGE_1_CONTRACT_FREEZE.md); the Stage 2/3 runtime
integration and its deliberate deferrals are recorded in
[`STAGE_2_3_IMPLEMENTATION.md`](STAGE_2_3_IMPLEMENTATION.md); the first-class
episodic implementation is recorded in
[`STAGE_4_IMPLEMENTATION.md`](STAGE_4_IMPLEMENTATION.md); structured audit,
correlations and raw-content policy scope are recorded in
[`STAGE_5_IMPLEMENTATION.md`](STAGE_5_IMPLEMENTATION.md). Stage 0 currently
provides an offline preregistration/reporting scaffold, not collected live
baseline evidence. Each later stage retains its own implementation and evidence
gate; examples marked conceptual are not substitutes for those gates.

Stage 6 now has a verified experimental lessons implementation; its held-out
effectiveness gate is still open. See
[`STAGE_6_IMPLEMENTATION.md`](STAGE_6_IMPLEMENTATION.md) for contracts, validation
rules and limits. The v2 adapter is implemented and exercised live; the CLI
defaults to v2 and retains explicit v1 compatibility. Read
[`../SIMULATOR_V2_ADAPTER.md`](../SIMULATOR_V2_ADAPTER.md) for the actual pilot
outcomes. The follow-up no-stop wait correction and its frozen diagnostic are
recorded in [`V2_POLICY_GUARD_RESULTS.md`](V2_POLICY_GUARD_RESULTS.md).
Successful SRE behavior has not yet been demonstrated.

The implemented experimental A0–A9 composition, commands and limitations are
documented in [`EXPERIMENTAL_MEMORY_GUIDE.md`](EXPERIMENTAL_MEMORY_GUIDE.md).
Implementation and evidence status is tracked in
[`REMAINING_EXECUTION.md`](REMAINING_EXECUTION.md). The distinct uptime/cost
experiment protocol is in
[`SIMULATOR_V2_EVALUATION_PROFILE.md`](SIMULATOR_V2_EVALUATION_PROFILE.md);
[`STAGE6_V2_DIAGNOSIS.md`](STAGE6_V2_DIAGNOSIS.md) records why the existing
live traces do not establish eligible lesson support or learning utility.
The sealed live integration record is in
[`V2_LIVE_INTEGRATION_RESULTS.md`](V2_LIVE_INTEGRATION_RESULTS.md).
The current completeness and dependency audit is in
[`ARCHITECTURE_AUDIT.md`](ARCHITECTURE_AUDIT.md); the evidence-based comparison
with the sibling baseline is in [`AGENT_COMPARISON.md`](AGENT_COMPARISON.md).
The optional research xMemory adapter is documented separately in
[`../XMEMORY_INTEGRATION.md`](../XMEMORY_INTEGRATION.md).
The controlled learning-cycle protocol and its oracle-isolation checks are
specified in [`LEARNING_CYCLE_PLAN.md`](LEARNING_CYCLE_PLAN.md). The real-model
results, frozen source identities and limitations are recorded in
[`LEARNING_CYCLE_RESULTS.md`](LEARNING_CYCLE_RESULTS.md).

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
- `AgentRunner` assembles one generic structured transition after each executed
  action and records the terminal transition before final run evidence.
- The experimental episodic-only profile persists full transitions and typed
  outcomes through either structured store and retrieves bounded untrusted
  episode views. It is programmatic only until safe namespace/reset lifecycle
  exists.
- Optional structured audit records selection, provider-neutral requests,
  decisions, receipt-backed item creation and runner-observed outcomes. Raw
  audit bodies are independently configurable; core structured evidence remains
  in sanitized metadata. Retention execution and evaluation promotion remain
  later-stage responsibilities.
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
