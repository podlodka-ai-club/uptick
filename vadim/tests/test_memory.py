import asyncio
from uuid import uuid4

from uptick_agent.memory import InMemoryMemory, JsonlMemory, NullMemory
from uptick_agent.models import MemoryEntry, MemoryQuery


def entry(
    content: str,
    *,
    run_id: str,
    importance: float = 0.5,
    kind: str = "experience",
) -> MemoryEntry:
    return MemoryEntry(
        id=uuid4().hex,
        run_id=run_id,
        kind=kind,
        content=content,
        importance=importance,
    )


def test_in_memory_recall_prefers_relevant_same_run() -> None:
    async def scenario() -> None:
        memory = InMemoryMemory()
        await memory.remember(entry("deployment finished successfully", run_id="old"))
        await memory.remember(entry("capacity exceeded; add a backend server", run_id="current"))
        await memory.remember(entry("routine overview is healthy", run_id="current"))

        matches = await memory.recall(
            MemoryQuery(text="backend capacity exceeded", run_id="current", limit=2)
        )

        assert matches[0].entry.content == "capacity exceeded; add a backend server"
        assert len(matches) == 1

    asyncio.run(scenario())


def test_jsonl_memory_survives_recreation(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "memory.jsonl"
        first = JsonlMemory(path)
        await first.remember(entry("exact fix is FIX-123", run_id="run-1", importance=0.9))
        await first.remember(entry("run completed", run_id="run-1", kind="outcome"))

        second = JsonlMemory(path)
        matches = await second.recall(MemoryQuery(text="FIX-123", run_id="run-2"))

        assert matches[0].entry.content == "exact fix is FIX-123"
        await second.clear("run-1")
        assert await second.recall(MemoryQuery(text="FIX-123")) == []

    asyncio.run(scenario())


def test_recall_excludes_incomplete_cross_run_entries_and_irrelevant_matches() -> None:
    async def scenario() -> None:
        memory = InMemoryMemory()
        await memory.remember(entry("exact fix is FIX-BROKEN", run_id="incomplete"))
        await memory.remember(entry("exact fix is FIX-GOOD", run_id="completed"))
        await memory.remember(entry("run completed", run_id="completed", kind="outcome"))
        await memory.remember(entry("unrelated deployment note", run_id="completed"))

        matches = await memory.recall(
            MemoryQuery(text="exact fix", run_id="current", include_other_runs=True)
        )

        assert [match.entry.content for match in matches] == ["exact fix is FIX-GOOD"]

    asyncio.run(scenario())


def test_null_memory_is_an_explicit_control_group() -> None:
    async def scenario() -> None:
        memory = NullMemory()
        await memory.remember(entry("ignored", run_id="run"))
        assert await memory.recall(MemoryQuery(text="ignored")) == []

    asyncio.run(scenario())
