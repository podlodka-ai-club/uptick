# Architecture Fitness Tests

## 1. Purpose

The system is likely to become cognitively complex. Architectural constraints therefore need executable enforcement.

Unit tests verify behavior.
Architecture tests verify that the system stays understandable.

## 2. Core dependency rules

At minimum enforce:

```text
memory core MUST NOT import simulator
memory core MUST NOT import concrete LLM providers
decision layer MUST NOT import concrete memory modules
agent runner MUST NOT import concrete memory modules
agent runner MUST NOT import concrete LLM providers
LLM providers MUST NOT import agent runtime
environment adapters MUST NOT be imported by memory modules
one memory module MUST NOT import another module's implementation package
```

Allowed communication between memory modules is through orchestrator contracts or stable shared contracts.

## 3. Orchestrator composition rule

`MemoryOrchestrator` is the place where modules are composed.

Test that:

- runner has exactly one high-level memory dependency;
- decision maker consumes only the read-model contract;
- adding/removing a module requires configuration/registration changes, not runner changes.

## 4. Environment leakage test

Search/import-test for forbidden references such as:

```text
uptick_agent.simulator
Simulator*
deployment-specific hidden-domain types
```

inside:

```text
memory/contracts
memory/modules/*
agent/decision
llm/contracts
```

Simulator vocabulary may appear in learned data at runtime; it must not become a compile-time dependency.

Also test the execution authorization boundary:

- the first-release profile admits only the approved simulator adapter ID/version;
- unknown and non-simulator adapters fail before session creation;
- no alternate or direct action-dispatch path bypasses adapter admission.

## 5. Provider leakage test

Only `llm/providers/*` may depend directly on provider SDKs/transport details.

Examples of forbidden dependencies elsewhere:

```text
openai SDK response objects
anthropic SDK response objects
Codex-specific process protocol
provider auth configuration
```

## 6. Module independence test

For each memory module:

1. resolve a valid configuration in which the module and any required
   dependants are disabled;
2. run baseline flow;
3. assert the disabled module has zero construction, read, write,
   consolidation, cache-hit and context-contribution events;
4. enable the module with only its minimum declared dependencies;
5. run module tests and assert only declared contributors are present.

A module that cannot be switched off independently has leaked.

Apply the same zero-event rule to every retrieval strategy exposed as an
independent experimental switch (`lexical`, `structured`, `semantic`).

## 7. Configuration dependency test

Dependencies must be declarative.

Example expected failure:

```text
world_model.enabled = true
episodic.enabled = false
lessons.enabled = false
```

Because neither declared evidence dependency is enabled, fail at startup with a
clear configuration error — not halfway through a run. Consolidation is not a
hard dependency of `world_model`.

Also reject:

- `status: default` without a valid `approval_record_id`;
- an approval for a different module version or resolved-config fingerprint;
- an experimental module in a profile labeled as default/promotion evidence.

## 8. Persistence abstraction test

Memory logic should not rely on a specific store.

At least one contract-level test should run against:

```text
in-memory store
SQLite test store
```

Higher-level module semantics should be identical.

JSONL participates only in legacy-adapter and import/export tests. It is not a
conforming writable store for structured snapshots or consolidation commits.

## 9. Decision context test

Ensure `DecisionMemoryContext` contains only stable read models.

It must not contain:

- database entities;
- simulator response models;
- provider response types;
- direct references to module implementation classes.

It must also satisfy the configured hard item/token budget on every path.
Selection must be deterministic for equal inputs, including tie-breaking and
overflow behavior, and a versioned trace must explain every selected or dropped
item.

Every prompt-facing memory item must retain its untrusted-data envelope and
transitive provenance. Hostile observation, derived lesson and playbook fixtures
must not alter system/developer policy, tool schemas, approval state or the
adapter action allowlist.

## 10. Runner complexity guard

The runner should remain orchestration code.

A review/fitness rule should reject additions that implement:

- lesson extraction;
- hypothesis confidence;
- retrieval scoring;
- consolidation;
- provider conversion.

inside `AgentRunner`.

## 11. Suggested Python tooling

The exact mechanism can stay lightweight.

Possible approaches:

- AST-based import checks;
- `import-linter`;
- package-level dependency tests;
- simple pytest scans for forbidden imports.

Do not introduce a large architecture framework unless needed.

A basic AST test is sufficient to start.

## 12. Example pseudo-test

```python
def test_memory_does_not_import_simulator():
    forbidden = {"uptick_agent.simulator"}
    imports = scan_imports("src/uptick_agent/memory")

    assert not imports_matching(imports, forbidden)
```

And:

```python
@pytest.mark.parametrize(
    "module",
    [
        "compatibility.legacy",
        "episodic",
        "lessons",
        "world_model",
        "consolidation",
        "advanced_retrieval",
        "playbooks",
        "tool_knowledge",
        "forgetting",
    ],
)
def test_agent_runs_when_optional_memory_module_disabled(module):
    config = valid_config_without(module, disable_dependants=True)
    agent = build_agent(config)
    run_smoke_test(agent)
    assert agent.diagnostics.activity_events_for(module) == []


@pytest.mark.parametrize(
    "switch",
    ["retrieval.lexical", "retrieval.structured", "retrieval.semantic"],
)
def test_disabled_retrieval_strategy_has_no_activity(switch):
    config = valid_config_with_switch_disabled(switch)
    agent = build_agent(config)
    run_smoke_test(agent)
    assert agent.diagnostics.activity_events_for(switch) == []
```

## 13. Experiment integrity tests

At minimum enforce:

- train/evaluation namespaces are isolated;
- frozen-memory evaluation cannot mutate its input snapshot;
- frozen-memory decisions cannot read an overlay or any item whose ancestry does
  not resolve exclusively to the declared input snapshot;
- snapshot creation rejects unknown, evaluation-phase and cross-experiment
  provenance;
- snapshot ID and content hash match before and after evaluation;
- every requested run has a retained terminal, failed, interrupted or excluded
  attempt record;
- the resolved config fingerprint matches the manifest;
- disabled modules produce zero lifecycle and contribution events;
- no decision-visible item or static prompt fragment originates from a disabled
  module or one of its snapshots;
- decision traces contain the selected memory IDs, scores, budget decisions and
  action/outcome correlation;
- exactly two completed eligible learning runs with distinct logical `run_id`
  values and two immutable scenario/environment contexts can satisfy the
  support-count checks, while one run, retries, two runs from one context or any
  frozen-evaluation run cannot;
- incomplete provenance, omitted results from declared counter-evidence
  searches, any unresolved contradiction, an unknown policy version or a
  malformed validation manifest produces zero decision-visible activation;
- insufficient support leaves an item `candidate`, while unresolved
  contradictions make it `disputed`;
- promotion cannot begin without a complete audit-retention policy reference
  and a tested rollback target; records under hold survive compaction/deletion;
- raw prompt, observation and trace bodies plus snapshots cannot expire before
  90 days after completion; project-lifetime records survive normal compaction,
  and deletion without a versioned policy approved by the project owner fails;
- active candidates with expiring raw provenance are revalidated, demoted or
  retain that provenance before deletion;
- disabling any raw-content class persists no body, while enabled classes
  record the applicable policy versions; and
- safe synthetic credential/secret fixtures never appear in primary storage,
  snapshots, manifests, diagnostics, retry payloads or backup fixtures; a
  redaction failure records only a metadata audit/quarantine event.

## 14. Definition of Done for any new module

A memory module is not complete until:

- its responsibility is written in one sentence;
- its public contract is explicit;
- it can be disabled by configuration;
- it does not import another module implementation;
- its persistence is behind a contract;
- LLM use is behind `LlmClient`;
- simulator types do not appear in it;
- unit tests exist;
- at least one architecture test protects its boundary;
- its contribution can be measured through an ablation.
