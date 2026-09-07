from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from uptick_agent._model_base import StrictModel, preserve_legacy_identity


class MemoryEntry(StrictModel):
    id: str
    run_id: str | None = None
    kind: Literal["observation", "experience", "outcome", "lesson"]
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    importance: float = Field(default=0.5, ge=0, le=1)
    tags: set[str] = Field(default_factory=set)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryQuery(StrictModel):
    text: str = ""
    run_id: str | None = None
    include_other_runs: bool = True
    kinds: set[Literal["observation", "experience", "outcome", "lesson"]] | None = None
    tags: set[str] = Field(default_factory=set)
    limit: int = Field(default=10, ge=0, le=100)


class MemoryMatch(StrictModel):
    entry: MemoryEntry
    score: float


preserve_legacy_identity(MemoryEntry, MemoryQuery, MemoryMatch)
