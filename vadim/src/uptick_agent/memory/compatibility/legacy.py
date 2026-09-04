"""Explicit adapter for the baseline ``Memory`` protocol and JSONL exchange."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

from uptick_agent.models import MemoryEntry, MemoryMatch, MemoryQuery
from uptick_agent.ports import Memory


class LegacyMemoryAdapter:
    """Preserves baseline lexical-memory semantics without making JSONL a store.

    JSONL is limited to legacy import/export.  Structured records and snapshots
    use the separate Stage 1 store contract.
    """

    def __init__(self, delegate: Memory) -> None:
        self._delegate = delegate

    async def remember(self, entry: MemoryEntry) -> None:
        await self._delegate.remember(entry)

    async def recall(self, query: MemoryQuery) -> list[MemoryMatch]:
        return await self._delegate.recall(query)

    async def clear(self, run_id: str | None = None) -> None:
        await self._delegate.clear(run_id)

    async def import_jsonl(self, path: str | Path) -> int:
        entries = await asyncio.to_thread(self.read_jsonl, path)
        for entry in entries:
            await self.remember(entry)
        return len(entries)

    @staticmethod
    def read_jsonl(path: str | Path) -> list[MemoryEntry]:
        source_path = Path(path)
        if not source_path.exists():
            return []
        entries: list[MemoryEntry] = []
        with source_path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    entries.append(MemoryEntry.model_validate_json(line))
                except ValueError as error:
                    raise ValueError(
                        f"invalid legacy memory entry at {source_path}:{line_number}"
                    ) from error
        return entries

    @staticmethod
    def write_jsonl(path: str | Path, entries: Iterable[MemoryEntry]) -> None:
        target_path = Path(path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(entry.model_dump_json() + "\n" for entry in entries)
        target_path.write_text(payload, encoding="utf-8")
