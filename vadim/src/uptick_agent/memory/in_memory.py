from __future__ import annotations

import math
import re
from collections.abc import Iterable

from uptick_agent.memory.compatibility.contracts import MemoryEntry, MemoryMatch, MemoryQuery
from uptick_agent.redaction import sanitize_json

_WORD = re.compile(r"[\w-]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in _WORD.findall(text) if len(token) > 1}


class InMemoryMemory:
    """Transparent lexical memory, intentionally simple enough to be a baseline."""

    def __init__(self, entries: Iterable[MemoryEntry] = ()) -> None:
        self._entries = [self._safe_entry(entry) for entry in entries]

    @staticmethod
    def _safe_entry(entry: MemoryEntry) -> MemoryEntry:
        return MemoryEntry.model_validate(sanitize_json(entry.model_dump(mode="json")))

    @property
    def entries(self) -> tuple[MemoryEntry, ...]:
        return tuple(self._entries)

    async def remember(self, entry: MemoryEntry) -> None:
        self._entries.append(self._safe_entry(entry))

    async def recall(self, query: MemoryQuery) -> list[MemoryMatch]:
        if query.limit == 0:
            return []

        query_tokens = _tokens(query.text)
        candidates: list[tuple[float, int, MemoryEntry]] = []
        total = max(len(self._entries), 1)
        completed_run_ids = {
            entry.run_id
            for entry in self._entries
            if entry.kind == "outcome" and entry.run_id is not None
        }

        for index, entry in enumerate(self._entries):
            if query.kinds is not None and entry.kind not in query.kinds:
                continue
            if query.tags and not query.tags.issubset(entry.tags):
                continue
            if not query.include_other_runs and query.run_id != entry.run_id:
                continue
            is_other_concrete_run = entry.run_id is not None and entry.run_id != query.run_id
            if is_other_concrete_run and entry.run_id not in completed_run_ids:
                continue

            entry_tokens = _tokens(entry.content)
            overlap = len(query_tokens & entry_tokens)
            if overlap == 0:
                continue
            lexical = overlap / math.sqrt(max(len(query_tokens) * len(entry_tokens), 1))
            recency = (index + 1) / total
            same_run = 1.0 if query.run_id is not None and entry.run_id == query.run_id else 0.0
            score = lexical * 0.55 + entry.importance * 0.2 + same_run * 0.15 + recency * 0.1
            candidates.append((score, index, entry))

        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        matches: list[MemoryMatch] = []
        seen_content: set[str] = set()
        for score, _, entry in candidates:
            if entry.content in seen_content:
                continue
            seen_content.add(entry.content)
            matches.append(MemoryMatch(entry=entry, score=score))
            if len(matches) == query.limit:
                break
        return matches

    async def clear(self, run_id: str | None = None) -> None:
        if run_id is None:
            self._entries.clear()
            return
        self._entries = [entry for entry in self._entries if entry.run_id != run_id]


class NullMemory:
    """Control group for experiments that intentionally disable memory."""

    async def remember(self, entry: MemoryEntry) -> None:
        return None

    async def recall(self, query: MemoryQuery) -> list[MemoryMatch]:
        return []

    async def clear(self, run_id: str | None = None) -> None:
        return None
