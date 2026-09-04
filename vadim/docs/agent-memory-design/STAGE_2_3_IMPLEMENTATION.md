# Stage 2/3 Implementation Record

Decision status: **accepted**
Implementation status: **complete**

## Delivered

- Provider-neutral LLM messages, settings, structured/text requests, results,
  capabilities and retry-relevant error taxonomy.
- Explicit provider registry/config with OpenAI and subscription-auth Codex
  adapters. Provider SDK request/response objects remain private.
- Structured generation as the decision path; Codex rejects text generation
  and portable settings it cannot honor.
- Configuration-resolved `MemoryOrchestrator` with dependency and approval
  checks before construction, narrow lifecycle dispatch, deterministic merge,
  duplicate handling and hard module/type/global budgets.
- Versioned diagnostics containing the resolved fingerprint, estimator,
  effective limits, contributors, selection evidence, consumption and
  truncation outcomes.
- A single runner-facing `AgentMemory` boundary. The compatibility profile
  projects lexical memory into untrusted normalized envelopes while preserving
  existing in-memory/JSONL write and clear semantics.
- Terminal evidence is stored before end-of-run finalization; failed and
  interrupted runs also emit a typed `RunOutcome` after a session has started.

## Resolved compatibility profile

`compatibility.legacy` is enabled; episodic, lessons, world-model, playbook,
tool-knowledge, consolidation and forgetting modules are disabled. Disabled
modules are not constructed or dispatched. The default memory-context budget
uses `utf8-byte-upper-bound@1.0`, which is conservative but deterministic and
dependency-free. The additive Stage 3 module-budget and estimator configuration
shapes author schema `1.1`; the frozen generic payload contracts remain `1.0`.

## Deliberately deferred

Stage 3 does not fabricate `ExperienceTransition` values from legacy writes.
Transition assembly and the first structured episodic module belong to Stage 4.
There is no consolidation scheduler, semantic retrieval, provider-specific
tokenizer dependency, promotion workflow or future module scaffold.

The existing CLI benchmark `summary.json` remains a convenience aggregate, not
the evaluation manifest defined in `04_EVALUATION_AND_ABLATIONS.md`. Full
resolved memory configuration, source/provider fingerprints, snapshots and the
run matrix belong to the Stage 5 evidence work; until then, benchmark summaries
must not be used as validation or promotion evidence.

The pre-Stage-3 `DecisionContext.recalled_memories` field remains only for
callers that construct the compatibility model directly; the runner populates
`memory_context` instead.

## Verification boundary

The offline suite covers provider contracts and registry selection, legacy
projection, disabled-module isolation, deterministic selection and budgets,
approval/finalization lifecycle, runner diagnostics, benchmark carry/isolation
and existing baseline behavior. Codex SDK execution still requires the optional
`openai-codex` extra, and live simulator verification remains opt-in.
