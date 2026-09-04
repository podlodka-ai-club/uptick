# Stage 1 Contract Freeze

Decision status: **accepted**
Implementation status: **complete**

The implementation below is test-validated and the Stage 1 contract-freeze
review is complete. Later stages may depend on these contracts, but each later
stage still retains its own implementation and evidence gate.

## Scope

Stage 1 freezes generic boundaries before any episodic module, orchestrator,
LLM gateway, retrieval policy, trace system or promotion flow is introduced.
The ownership of the boundaries below is relative to `src/uptick_agent/`:

- `memory/contracts.py`: public data and capability contracts;
- `memory/config.py`: resolved feature declarations and fingerprinting;
- `memory/stores/*`: generic structured persistence and immutable snapshots;
- `memory/compatibility/legacy.py`: baseline `Memory` and JSONL compatibility.

Memory contracts contain no simulator model, provider type, SQL entity or
prompt-policy authority. Prompt-facing items use `UntrustedMemoryEnvelope` and
remain data; they never change tool permission or environment authorization.

## Contract rules

All public payloads and persisted records carry `schema_version` in
`major.minor` form. Version 1 readers reject an unknown major. Current 1.0
authoring/input remains strict about unknown fields; a supported newer 1.x
payload may carry additive unknown fields, which a 1.0 reader ignores.

State-changing store operations receive `(namespace, operation,
idempotency_key)`. A retry with the same canonical input returns its original
receipt; reusing that scoped key for different input raises
`MemoryConflictError`. Validation/configuration, conflict/concurrency,
transient infrastructure and permanent failures remain separate error classes.
Only transient failures are candidates for a bounded caller retry.

`ExperienceTransition`, `RunOutcome`, `MemoryContextRequest`,
`DecisionMemoryContext`, `MemoryContribution`, `ExperienceTransitionAssembler`,
`ExperienceSink`, `ContextContributor` and `ConsolidationParticipant` are
contract-only in this stage. No runtime is redirected to them yet.

Prompt-facing envelopes require a non-empty untrusted item and at least one
provenance reference. Transitions likewise require non-empty provenance, an
explicit closed trust classification, a one-based iteration and an explicit
terminal flag; transition-assembly requests carry the same one-based iteration
and terminal fact. A `RunOutcome` is always terminal and its status is exactly
one of `completed`, `failed`, `interrupted` or `excluded`.

Named numeric fields reject NaN and infinities, as do nested values in every
JSON-valued contract payload. Canonical JSON independently uses
`allow_nan=False`, so malformed non-finite values cannot be fingerprinted even
if validation is bypassed.

## Configuration

`MemoryConfiguration.legacy_baseline()` is canonical: legacy compatibility is
enabled and every future cognitive module is disabled. It is a development
profile with an experimental legacy module, because `status: default` requires
an approval record. World models require episodic memory or lessons; playbooks
require lessons or a world model. The canonical resolved JSON is SHA-256
fingerprinted.

Stage 3 will own configuration loading, approval-record verification, module
construction, diagnostics and context-budget enforcement. This stage defines
the values but does not wire them into `AgentRunner` or the CLI.

Subsequent status: Stage 3 now owns those responsibilities and is wired through
the compatibility runtime; see
[`STAGE_2_3_IMPLEMENTATION.md`](STAGE_2_3_IMPLEMENTATION.md).

## Persistence and snapshots

SQLite is the first durable structured system of record. Its schema has a
version table plus generic `memory_records`, idempotent operation receipts,
`memory_snapshots` and ordered snapshot-member rows. The in-memory store is the
reference implementation and must pass the same contract suite. Every public
store method validates caller-controlled namespaces, IDs, operations and
idempotency keys for string type, non-empty values and the declared maximum
lengths, and invalid calls raise `MemoryValidationError` before store
initialization or mutation. `RecordWrite` values are serialized and
revalidated at the append boundary so validation bypasses such as
`model_copy(update=...)` cannot enter persistence.

A snapshot atomically captures ordered record IDs and content hashes for one
namespace; its ID and content hash are immutable. Later writes do not alter it,
and Stage 1 exposes no delete, compaction or writable overlay API. JSONL is
only a legacy import/export format and is never a structured-store or snapshot
backend. Stores take defensive ownership of input payloads and every returned
record, receipt and snapshot is a deep copy, so caller mutation cannot alter
stored state or a later replay. SQLite write paths use `BEGIN IMMEDIATE` and a
bounded busy timeout across store instances; constraint races become conflicts,
busy/locked failures are transient, and corrupt or otherwise invalid database
state is permanent. Fresh initialization performs DDL in an autocommit-safe
phase, then inserts/checks exactly one singleton schema-version row in a
separate `BEGIN IMMEDIATE` transaction. Filesystem failures such as path
creation and database open/read/write are permanent.

## Deferred intentionally

Stage 1 does not enforce experiment phase/provenance admissibility, train/eval
namespace isolation, frozen-evaluation overlays, retention/redaction, evidence
promotion, budgeted selection traces, or decision-visible activation. Those
requirements are reserved for the Stage 5/7+ trace, evaluation and validation
work; callers must not infer that this generic persistence layer proves them.

## Acceptance

The contract suite runs unchanged against in-memory and SQLite stores, including
namespace-scoped duplicate retry, conflicting retry, deterministic listing,
defensive-copy behavior, immutable snapshot and SQLite reopen/cross-instance
behavior. The legacy adapter preserves current
`remember/recall/clear` semantics and is the only JSONL boundary. Architecture
tests reject simulator and provider imports from this Stage 1 boundary.
