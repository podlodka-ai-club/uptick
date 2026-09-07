# Stage 4 Implementation Record

Decision status: **accepted**
Implementation status: **complete**

## Delivered

- `AgentRunner` constructs exactly one `ExperienceTransition` after every
  executed action, including the terminal action. Construction happens before
  legacy experience writes, observer delivery and run-state mutation.
- `DefaultExperienceTransitionAssembler` is a pure generic core. It imports no
  simulator, provider or memory-module implementation, normalizes occurrence
  time to UTC, hashes direct inputs into provenance references, derives metric
  deltas only from matching name/unit observations and preserves opaque
  operation links without claiming causality.
- The additive transition contract is authored as schema `1.1` with
  `ObjectiveMetricDelta` and `OperationLink`. Existing `1.0` transition payloads
  remain readable with empty additive fields. A current `1.1` assembly request
  requires explicit trust and an aware occurrence timestamp.
- Simulator responses expose generic objective snapshots and initiated/observed
  operation links at the environment boundary. Final economy values are exposed
  as generic absolute objective metrics on `RunResult` and `RunOutcome`.
- `EpisodicMemory` implements `ExperienceSink`, `ContextContributor` and
  `RunFinalizer` over the existing `StructuredMemoryStore`. It persists full
  structured transitions and terminal outcomes; prompt-facing retrieval emits
  bounded untrusted episode views with original provenance.
- Current-run episodes are immediately eligible. Historical episodes become
  eligible only after a completed outcome for their run. Failed, interrupted
  and unfinished historical runs are not retrieved.
- Retrieval is deterministic and dependency-free: lexical overlap, same-run
  preference and stable recency/tie-breaking. The orchestrator remains the sole
  owner of hard item and byte-estimate budgets.
- Transition and outcome writes receive deterministic idempotency keys. A
  transient module failure is retried once with the same key; validation,
  conflict and permanent failures are not retried.
- Raw episode bodies remain enabled for the simulator profile, but the shared
  persistence boundary removes the repository's credential-shaped fixtures
  before provenance hashing and storage. Direct episodic writes that bypass
  this assembly boundary are rejected when redaction would change them. The
  same shared redactor protects structured records, legacy JSONL memory and
  JSONL observer output.

## Composition

`MemoryConfiguration.episodic_only()` is an experimental profile with only the
`episodic@1.0` module enabled. `episodic_memory_runtime(store, namespace=...)`
is the programmatic composition root for `AgentRunner`; it supports both the
in-memory and SQLite structured stores.

The namespace passed to that factory is owned by the episodic module and must
be fresh for an independent experiment. The structured-store contract has no
delete/reset operation. Consequently this runtime rejects `clear()` and there
is no persistent episodic CLI or benchmark mode yet. Silently reusing or
pretending to clear a namespace would invalidate experiment isolation.

The canonical `legacy_baseline()` profile is unchanged: the episodic module is
not constructed, read or written, while the runner's structured transition
dispatch is a zero-effect no-op through the orchestrator.

## Lifecycle and failure semantics

- If transition assembly or persistence fails, the action is not written as a
  legacy experience and is not sent to the observer. The original error aborts
  the run; after a session has started, failed/interrupted outcome recording is
  attempted without masking that error.
- A terminal transition is recorded before terminal legacy evidence and typed
  outcome finalization.
- If the world has completed and final memory persistence fails, the memory
  error is surfaced. The factual completed outcome is not rewritten as a
  failed world run.
- An environment start failure occurs before a stable run ID exists and cannot
  produce a `RunOutcome`.

## Verification boundary

The offline suite covers:

- deterministic assembly, provenance hashes, UTC normalization, metric
  matching, operation links and invalid-input rejection;
- runner ordering, terminal episodes, objective propagation and failure paths;
- in-memory and SQLite persistence, idempotent replay, SQLite reopen, namespace
  isolation and completed-run retrieval eligibility;
- corrupt/unknown episodic records failing closed;
- credential-shaped values redacted before transition hashing and persistence,
  with unsafe direct transition bypasses rejected;
- programmatic episodic composition and explicit unsupported clear behavior;
- one bounded transient retry with a stable idempotency key and no retry for
  non-transient errors;
- exact simulator metric/link mapping and architecture import boundaries.

The optional Codex SDK test and live simulator test remain environment-gated;
neither blocks the dependency-free episodic implementation.

## Deferred intentionally

Stage 4 does not add policy-configurable raw-content classes, per-write redaction
audit/quarantine records, retention execution, decision traces, experiment
manifests or frozen snapshots, promotion/evidence validation, lessons, causal
attribution beyond observed operation links, embeddings, semantic retrieval,
consolidation, scheduling, compaction or deletion. Those remain owned by later
stages and must not be inferred from raw episodic history.
