# HU-xiaobai/xMemory integration

This is an optional, experimental adapter for the public `xMemory` facade in
the [HU-xiaobai/xMemory repository](https://github.com/HU-xiaobai/xMemory).
The adapter does not import that repository or add it as a dependency.  The
composition root supplies a facade object implementing the small protocol in
`uptick_agent.integrations.xmemory`.

The implementation was checked against upstream `main` at
[`375ae1495095aa14a39eb169f83737f4779391c6`](https://github.com/HU-xiaobai/xMemory/tree/375ae1495095aa14a39eb169f83737f4779391c6).
The facade exposes `add_messages`, `flush`, `wait_for_semantic`, and `search`.
`add_messages` accepts a `user_id` and message dictionaries containing at least
`role` and `content`, with optional timestamps and metadata.  `search` returns
`{"episodic": [...], "semantic": [...]}`.  `flush` can return `None` when its
buffer is empty or when upstream catches an error.  `wait_for_semantic=True`
means that no semantic task is still active; it does not independently prove
that every task succeeded.

## Vadim boundary

`XMemoryModule` implements the native structured-memory capabilities:

- `ContextContributor.retrieve` calls the facade's bounded hybrid search and
  returns `MemoryContribution` items classified as `external_untrusted`.
- `ExperienceSink.record` serializes one validated `ExperienceTransition` as a
  single message and calls `add_messages` once.
- `RunFinalizer.finalize` calls `flush`, then waits for semantic generation.

The runner-facing `build_xmemory_runtime` helper composes the module through
`MemoryOrchestrator` with module ID `xmemory`.  Its compatibility `remember`
hook is intentionally a no-op because the xMemory facade has no equivalent
structured `MemoryEntry` operation.  `clear` raises a typed error: this
integration makes no deletion claim.

`namespace` is the local durable journal namespace.  `user_id` is a fixed,
path-free xMemory owner identifier for the module lifetime.  Neither value is
derived from a request or transition.  Use a separate upstream `MemoryConfig.storage_path` and a durable SQLite journal
for each independent owner. Different `user_id` strings alone are not a tenant
security boundary: upstream repository operations can scan files across owners.
Reuse the same journal on restart; an in-memory journal protects only the current
process lifetime.

The orchestrator owns global item and token budgets.  The adapter additionally
caps each upstream episodic and semantic `top_k` and trims the normalized
combined result to the request's `max_items`.  Search queries and all returned
JSON pass through Vadim's shared redaction boundary.  Empty results carry an
explicit warning because upstream xMemory catches search failures and returns
empty collections; zero hits are not authoritative health evidence.

## Write and finalize safety

The SQLite or in-memory `StructuredMemoryStore` is used only for a local
intent/receipt journal.  Before an external write, the adapter appends a
`xmemory-write-intent` record containing the input hash and a unique attempt
owner token.  After a confirmed upstream response it appends an
`xmemory-write-receipt` record.  A completed receipt replays without calling
`add_messages`; a pending intent is treated as possibly submitted and refuses
retry.  Any facade exception is therefore surfaced as a conflict rather than a
transient error that `MemoryOrchestrator` could retry.

The upstream response must be an object with `status="success"` and
`messages_added=1`.  Upstream `episode_object` model instances are omitted from
the persisted receipt; only safe primitive result fields are retained.  The
receipt is an observation of the facade response, not an independently
verified external artifact ID.

Finalization has a separate intent/receipt journal.  It requires that
`flush` returns and `wait_for_semantic(...) is True`; a false wait result or an
exception is a typed permanent failure and leaves the intent pending, so the
orchestrator cannot repeat a mutating flush automatically.  A completed
finalization receipt means that the calls completed as observed by the
adapter.  External xMemory consolidation remains the upstream algorithm and
is outside Vadim's Stage 9 consolidation.

## Evaluation boundary

The adapter rejects construction for `phase="evaluation"` or
`read_only=True`, before any facade method can run.  The upstream library has
no public immutable snapshot/export contract.  A copied storage directory is
not treated as a frozen evidence snapshot.  Consequently this integration is
training-only until a separately verified snapshot/manifest adapter exists.

## Dependency-injected composition example

Install and configure the pinned upstream checkout in a separate application
runtime following its README; it is not added to Vadim's default dependencies.
The application explicitly constructs its configured upstream facade. For example,
with the upstream checkout available on `PYTHONPATH`:

```python
from src.api.facade import xMemory
from src.config import MemoryConfig

# Supply independently configured upstream clients. This constructor can load
# models and initialize upstream resources; it is not a network-free operation.
facade = xMemory(
    MemoryConfig(storage_path="./artifacts/xmemory/owner-a/upstream"),
    llm_client=configured_llm_client,
    embedding_client=configured_embedding_client,
)
```

Pass that facade into the native runner composition below. `model` and
`environment` are the same decision model and environment adapters used by the
ordinary agent; neither needs xMemory-specific changes.

```python
from pathlib import Path

from uptick_agent import AgentConfig, AgentRunner
from uptick_agent.integrations.xmemory import build_xmemory_runtime
from uptick_agent.memory.config import MemoryConfiguration, ModuleConfig
from uptick_agent.memory.stores import SqliteStructuredStore


async def run_with_xmemory(facade, model, environment, seed: int):
    configuration = MemoryConfiguration(
        schema_version="1.3",
        profile_id="xmemory-training",
        compatibility_legacy=ModuleConfig(enabled=False),
        xmemory=ModuleConfig(
            enabled=True, version="1.0", max_context_items=8,
            max_context_tokens=4000,
        ),
    )
    journal_path = Path("artifacts/xmemory/owner-a/journal.sqlite")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    memory = build_xmemory_runtime(
        configuration, facade, SqliteStructuredStore(journal_path),
        namespace="owner-a-training-journal", user_id="owner-a-training",
        ownership="borrowed",
    )
    try:
        runner = AgentRunner(
            config=AgentConfig(), model=model, memory=memory,
            environment=environment,
        )
        return await runner.run(seed)
    finally:
        memory.close()
        # Borrowed ownership: the application closes facade after its last run.
```

Choose `ownership="owned"` when the runtime should call `facade.close()`.
Closing a borrowed module never closes the injected facade. Calls after module
close fail explicitly. Synchronous upstream work runs in a worker thread;
cancelling the awaiting task does not stop an already executing upstream call.
The journal blocks replay of an ambiguous write or flush. Inspect upstream state
before any manual reconciliation; changing the idempotency key is a new write,
not a safe retry. This adapter cannot promise bounded shutdown for an upstream
call that never returns.

## Verification performed

Contract tests cover redaction and untrusted context, global budgets, durable
write/finalize replay, ambiguity, concurrent ownership, malformed responses,
safe owner IDs, closure and evaluation rejection. A separate smoke exercised
**the real pinned upstream facade and this adapter with SQLite**, injecting a
fake memory system and stubbing heavy imports. It confirmed one external add
on replay, all four journal record kinds, bounded retrieval, flush/wait and one
owned close. An Episode-shaped non-JSON object in the real response shape was
successfully omitted from the receipt.

This is facade/adapter verification. The full upstream embedding, generation,
Chroma/BM25 pipeline has not been installed or run, and no xMemory task-quality
score has been measured. The separate hosted xmemory.ai product is not this
integration target.

The upstream project remains a research library with local model, embedding,
Chroma, and BM25 setup requirements.  Run the focused adapter tests with:

```bash
cd vadim
UV_CACHE_DIR=/private/tmp/uptick-uv-cache \
  uv run --extra codex --locked pytest -q tests/test_xmemory_integration.py
```
