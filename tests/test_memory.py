import asyncio
from uuid import uuid4

from uptick_agent.memory import InMemoryMemory, JsonlMemory, NullMemory
from uptick_agent.models import MemoryEntry, MemoryQuery


def entry(content: str, *, run_id: str, importance: float = 0.5) -> MemoryEntry:
    return MemoryEntry(
        id=uuid4().hex,
        run_id=run_id,
        kind="experience",
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
        assert len(matches) == 2

    asyncio.run(scenario())


def test_jsonl_memory_survives_recreation(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "memory.jsonl"
        first = JsonlMemory(path)
        await first.remember(entry("exact fix is FIX-123", run_id="run-1", importance=0.9))

        second = JsonlMemory(path)
        matches = await second.recall(MemoryQuery(text="FIX-123", run_id="run-2"))

        assert matches[0].entry.content == "exact fix is FIX-123"
        await second.clear("run-1")
        assert await second.recall(MemoryQuery(text="FIX-123")) == []

    asyncio.run(scenario())


def test_null_memory_is_an_explicit_control_group() -> None:
    async def scenario() -> None:
        memory = NullMemory()
        await memory.remember(entry("ignored", run_id="run"))
        assert await memory.recall(MemoryQuery(text="ignored")) == []

    asyncio.run(scenario())
