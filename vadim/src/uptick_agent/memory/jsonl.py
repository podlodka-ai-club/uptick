from __future__ import annotations

import asyncio
from pathlib import Path

from uptick_agent.memory.compatibility.contracts import MemoryEntry, MemoryMatch, MemoryQuery
from uptick_agent.memory.in_memory import InMemoryMemory
from uptick_agent.redaction import sanitize_json


class JsonlMemory:
    """Durable memory with the same retrieval semantics as the in-memory baseline."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._memory = InMemoryMemory()
        self._loaded = False
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            entries = await asyncio.to_thread(self._read_entries)
            self._memory = InMemoryMemory(entries)
            self._loaded = True

    def _read_entries(self) -> list[MemoryEntry]:
        if not self.path.exists():
            return []
        entries: list[MemoryEntry] = []
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if line.strip():
                    try:
                        entry = MemoryEntry.model_validate_json(line)
                        entries.append(
                            MemoryEntry.model_validate(sanitize_json(entry.model_dump(mode="json")))
                        )
                    except ValueError as error:
                        raise ValueError(
                            f"invalid memory entry at {self.path}:{line_number}"
                        ) from error
        return entries

    async def remember(self, entry: MemoryEntry) -> None:
        await self._ensure_loaded()
        async with self._lock:
            safe_entry = MemoryEntry.model_validate(sanitize_json(entry.model_dump(mode="json")))
            await self._memory.remember(safe_entry)
            payload = safe_entry.model_dump_json() + "\n"
            await asyncio.to_thread(self._append, payload)

    def _append(self, payload: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as target:
            target.write(payload)

    async def recall(self, query: MemoryQuery) -> list[MemoryMatch]:
        await self._ensure_loaded()
        return await self._memory.recall(query)

    async def clear(self, run_id: str | None = None) -> None:
        await self._ensure_loaded()
        async with self._lock:
            await self._memory.clear(run_id)
            payload = "".join(entry.model_dump_json() + "\n" for entry in self._memory.entries)
            await asyncio.to_thread(self._rewrite, payload)

    def _rewrite(self, payload: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)
