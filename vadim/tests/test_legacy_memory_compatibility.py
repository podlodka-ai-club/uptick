import asyncio

from uptick_agent.memory import InMemoryMemory, LegacyMemoryAdapter
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
