import asyncio

from uptick_agent.memory import InMemoryMemory, LegacyMemoryAdapter, legacy_memory_runtime
from uptick_agent.memory.config import (
    AuditConfiguration,
    MemoryConfiguration,
    RawContentConfiguration,
)
from uptick_agent.memory.contracts import MemoryContextRequest
from uptick_agent.models import MemoryEntry, MemoryQuery


def _entry(entry_id: str, content: str) -> MemoryEntry:
    return MemoryEntry(id=entry_id, run_id="run-1", kind="experience", content=content)


def test_legacy_adapter_delegates_baseline_memory_semantics() -> None:
    async def scenario() -> None:
        adapter = LegacyMemoryAdapter(InMemoryMemory())
        await adapter.remember(_entry("entry", "exact legacy fix"))
        await adapter.remember(
            MemoryEntry(id="outcome", run_id="run-1", kind="outcome", content="done")
        )

        matches = await adapter.recall(MemoryQuery(text="legacy fix", run_id="run-2"))
        assert [match.entry.id for match in matches] == ["entry"]
        await adapter.clear("run-1")
        assert await adapter.recall(MemoryQuery(text="legacy fix", run_id="run-2")) == []

    asyncio.run(scenario())


def test_jsonl_is_explicit_legacy_import_export_only(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "legacy.jsonl"
        entries = [_entry("entry", "exported legacy fix")]
        LegacyMemoryAdapter.write_jsonl(path, entries)

        adapter = LegacyMemoryAdapter(InMemoryMemory())
        assert await adapter.import_jsonl(path) == 1
        assert (await adapter.recall(MemoryQuery(text="exported fix", run_id="run-1")))[
            0
        ].entry == entries[0]

    asyncio.run(scenario())


def test_legacy_primary_records_ignore_disabled_audit_raw_flags() -> None:
    async def scenario() -> None:
        store = InMemoryMemory()
        configuration = MemoryConfiguration.legacy_baseline(
            audit=AuditConfiguration(
                raw_content=RawContentConfiguration(
                    prompts=False,
                    observations=False,
                    decision_traces=False,
                )
            )
        )
        runtime = legacy_memory_runtime(store, configuration=configuration)
        entry = _entry("entry", "preserve this structured content")
        entry = entry.model_copy(update={"metadata": {"action": "restart"}})

        await runtime.remember(entry)

        assert store.entries[0] == entry

    asyncio.run(scenario())


def test_jsonl_import_export_scrubs_nested_secrets(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "legacy-secrets.jsonl"
        unsafe = _entry("entry", "token=topsecret useful legacy context").model_copy(
            update={"metadata": {"nested": {"password": "secret-value"}}}
        )

        LegacyMemoryAdapter.write_jsonl(path, [unsafe])

        serialized = path.read_text(encoding="utf-8")
        assert "topsecret" not in serialized
        assert "secret-value" not in serialized
        imported = LegacyMemoryAdapter.read_jsonl(path)
        assert imported[0].content == "<redacted> useful legacy context"
        assert imported[0].metadata == {"nested": {"password": "<redacted>"}}

    asyncio.run(scenario())


def test_legacy_runtime_projects_recall_into_the_normalized_untrusted_context() -> None:
    async def scenario() -> None:
        store = InMemoryMemory()
        runtime = legacy_memory_runtime(store)
        await runtime.remember(_entry("entry", "exact legacy fix"))

        context = await runtime.build_context(
            MemoryContextRequest(
                request_id="request",
                run_id="run-1",
                query="legacy fix",
                max_items=1,
            )
        )

        assert [item.envelope.item_id for item in context.items] == ["entry"]
        envelope = context.items[0].envelope
        assert envelope.origin_module == "compatibility.legacy"
        assert envelope.trust_classification == "external_untrusted"
        assert envelope.item["content"] == "exact legacy fix"
        assert runtime.context_diagnostics["configuration_fingerprint"]

    asyncio.run(scenario())
