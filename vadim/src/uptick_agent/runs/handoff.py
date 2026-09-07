"""Runtime-facing contracts for historical observation handoff."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol

from pydantic import Field, StrictInt

from uptick_agent._model_base import StrictModel
from uptick_agent.decisions.runtime import ToolResult

MIN_READ_BYTES = 4
MAX_READ_BYTES = 8 * 1024


class ObservationReadRequest(StrictModel):
    """The only read arguments accepted by the runtime handoff boundary."""

    record_id: str = Field(min_length=1, max_length=256)
    offset: StrictInt = Field(default=0, ge=0)
    max_bytes: StrictInt = Field(default=4096, ge=MIN_READ_BYTES, le=MAX_READ_BYTES)


class ObservationHandoffPort(Protocol):
    """Runner-facing handoff interface without a persistence dependency."""

    def begin(self, run_id: str) -> None: ...

    def note_transition(
        self, transition_id: str, *, current_iteration: int
    ) -> Awaitable[Any | None]: ...

    def snapshot(self, *, current_iteration: int) -> list[dict[str, Any]]: ...

    def read(
        self,
        request: ObservationReadRequest,
        *,
        current_iteration: int,
    ) -> Awaitable[ToolResult]: ...


class ObservationHandoffStartup(Protocol):
    """Composition-owned initializer for an explicitly opted-in new run."""

    async def initialize_new(
        self,
        *,
        session: object,
        initial_result: ToolResult,
    ) -> None: ...


__all__ = [
    "MAX_READ_BYTES",
    "MIN_READ_BYTES",
    "ObservationHandoffPort",
    "ObservationHandoffStartup",
    "ObservationReadRequest",
]
