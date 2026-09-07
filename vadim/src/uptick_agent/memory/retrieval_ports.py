"""Neutral ports for optional retrieval capabilities."""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from typing import Protocol, runtime_checkable

from uptick_agent.memory.contracts import MemoryContextRequest


@runtime_checkable
class EmbeddingPort(Protocol):
    """Generate one query vector and one vector for each admitted passage."""

    @property
    def dimension(self) -> int | None: ...

    def embed_query(self, text: str) -> Sequence[float] | Awaitable[Sequence[float]]: ...

    def embed_passages(
        self, texts: Sequence[str]
    ) -> Sequence[Sequence[float]] | Awaitable[Sequence[Sequence[float]]]: ...


@runtime_checkable
class ReasonedQueryPort(Protocol):
    """Reformulate a request using only its public request/context payload."""

    def rewrite(self, request: MemoryContextRequest) -> str | Awaitable[str]: ...
