"""Example memory decorator for a focused team experiment."""

from uptick_agent.models import MemoryEntry, MemoryQuery
from uptick_agent.ports import Memory


class ImportanceMemory:
    """Keep the underlying store, but only persist sufficiently important events."""

    def __init__(self, inner: Memory, *, minimum: float = 0.7) -> None:
        self.inner = inner
        self.minimum = minimum

    async def remember(self, entry: MemoryEntry) -> None:
        if entry.importance >= self.minimum:
            await self.inner.remember(entry)

    async def recall(self, query: MemoryQuery):
        return await self.inner.recall(query)

    async def clear(self, run_id: str | None = None) -> None:
        await self.inner.clear(run_id)
