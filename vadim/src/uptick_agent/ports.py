from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel

from uptick_agent.decisions.runtime import RuntimeDecisionContext, ToolResult
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.memory.audit_contracts import AuditTraceEvent, AuditTraceWrite
from uptick_agent.memory.compatibility.contracts import MemoryEntry, MemoryMatch, MemoryQuery
from uptick_agent.memory.contracts import (
    DecisionMemoryContext,
    ExperienceTransition,
    MemoryContextRequest,
    RunOutcome,
)
from uptick_agent.runs.runtime_results import RuntimeRunResult, RuntimeStepRecord


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

    async def record_trace(self, write: AuditTraceWrite) -> AuditTraceEvent | None: ...

    @property
    def context_diagnostics(self) -> dict[str, Any]: ...


class DecisionModel(Protocol):
    """Turns context into one schema-constrained decision."""

    async def decide(self, context: RuntimeDecisionContext) -> BaseModel: ...


class EnvironmentSession(Protocol):
    run_id: str
    seed: int


class Environment(Protocol):
    """A world adapter. The runner does not depend on HTTP or this simulator."""

    @property
    def decision_spec(self) -> EnvironmentDecisionSpec: ...

    async def start(
        self, *, seed: int, agent_id: str, agent_version: str
    ) -> tuple[EnvironmentSession, ToolResult]: ...

    async def execute(self, session: EnvironmentSession, action: BaseModel) -> ToolResult: ...

    def public_state(self, session: EnvironmentSession) -> Mapping[str, Any] | BaseModel: ...

    async def finish(
        self,
        session: EnvironmentSession,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ) -> RuntimeRunResult: ...


class RunObserver(Protocol):
    async def on_step(self, record: RuntimeStepRecord) -> None: ...

    async def on_finish(self, result: RuntimeRunResult) -> None: ...
