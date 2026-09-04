from __future__ import annotations

from typing import Any, Protocol

from uptick_agent.memory.contracts import (
    DecisionMemoryContext,
    ExperienceTransition,
    MemoryContextRequest,
    RunOutcome,
)
from uptick_agent.models import (
    AgentAction,
    DecisionContext,
    MemoryEntry,
    MemoryMatch,
    MemoryQuery,
    NextStep,
    RunResult,
    StepRecord,
    ToolResult,
)


class Memory(Protocol):
    """Replace this port to compare retention and retrieval strategies."""

    async def remember(self, entry: MemoryEntry) -> None: ...

    async def recall(self, query: MemoryQuery) -> list[MemoryMatch]: ...

    async def clear(self, run_id: str | None = None) -> None: ...


class AgentMemory(Protocol):
    """Single runner-facing memory boundary during legacy migration."""

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext: ...

    async def remember(self, entry: MemoryEntry) -> None: ...

    async def record_transition(self, transition: ExperienceTransition) -> None: ...

    async def clear(self, run_id: str | None = None) -> None: ...

    async def finalize_run(self, outcome: RunOutcome) -> None: ...

    @property
    def context_diagnostics(self) -> dict[str, Any]: ...


class DecisionModel(Protocol):
    """Turns context into one schema-constrained decision."""

    async def decide(self, context: DecisionContext) -> NextStep: ...


class EnvironmentSession(Protocol):
    run_id: str
    seed: int


class Environment(Protocol):
    """A world adapter. The runner does not depend on HTTP or this simulator."""

    async def start(
        self, *, seed: int, agent_id: str, agent_version: str
    ) -> tuple[EnvironmentSession, ToolResult]: ...

    async def execute(self, session: EnvironmentSession, action: AgentAction) -> ToolResult: ...

    async def finish(
        self,
        session: EnvironmentSession,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ) -> RunResult: ...


class RunObserver(Protocol):
    async def on_step(self, record: StepRecord) -> None: ...

    async def on_finish(self, result: RunResult) -> None: ...
