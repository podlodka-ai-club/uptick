"""Adapters that bind pre-started runs and telemetry to the runner ports."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.memory.compatibility.contracts import MemoryEntry
from uptick_agent.memory.contracts import (
    DecisionMemoryContext,
    ExperienceTransition,
    MemoryContextRequest,
    RunOutcome,
)
from uptick_agent.ports import AgentMemory, DecisionModel, Environment, EnvironmentSession
from uptick_agent.runs.runtime_results import RuntimeRunResult, RuntimeStepRecord


class _PrestartedEnvironment:
    """One-shot facade that gives AgentRunner an already-started session."""

    def __init__(
        self,
        environment: Environment,
        session: EnvironmentSession,
        latest: ToolResult,
        *,
        environment_id: str,
        scenario_id: str,
    ):
        self._environment = environment
        self._session = _AttributedSession(session, environment_id, scenario_id)
        self._latest = latest
        self._consumed = False
        self.decision_spec = environment.decision_spec

    async def start(
        self, *, seed: int, agent_id: str, agent_version: str
    ) -> tuple[EnvironmentSession, ToolResult]:
        if self._consumed:
            raise RuntimeError("prestarted environment cannot be started twice")
        self._consumed = True
        if seed != self._session.seed:
            raise ValueError("prestarted environment seed changed")
        return self._session, self._latest

    async def execute(self, session: EnvironmentSession, action: object) -> ToolResult:
        return await self._environment.execute(
            self._session._session if isinstance(session, _AttributedSession) else session,
            action,
        )  # type: ignore[arg-type]

    def public_state(self, session: EnvironmentSession) -> Mapping[str, object]:
        state = self._environment.public_state(
            self._session._session if isinstance(session, _AttributedSession) else session
        )
        if isinstance(state, Mapping):
            return state
        return state.model_dump(mode="json")

    async def finish(
        self,
        session: EnvironmentSession,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ) -> RuntimeRunResult:
        return await self._environment.finish(
            self._session._session if isinstance(session, _AttributedSession) else session,
            steps=steps,
            duration_seconds=duration_seconds,
            stop_reason=stop_reason,
        )


class _AttributedSession:
    """Delegate adapter state while adding verified profile attribution."""

    def __init__(self, session: EnvironmentSession, environment_id: str, scenario_id: str):
        self._session = session
        self.environment_id = environment_id
        self.scenario_id = scenario_id

    def __getattr__(self, name: str) -> object:
        return getattr(self._session, name)


class _TraceObserver:
    """Capture the runner's actual step and finish records for the artifact."""

    def __init__(self) -> None:
        self.steps: list[RuntimeStepRecord] = []
        self.startup_artifacts: dict[str, str] = {}
        self.result: RuntimeRunResult | None = None

    async def on_step(self, record: RuntimeStepRecord) -> None:
        self.steps.append(record.model_copy(deep=True))

    async def on_finish(self, result: RuntimeRunResult) -> None:
        self.result = result.model_copy(deep=True)


class _FinalizationError(RuntimeError):
    pass


class _MemoryAdapter:
    """Preserve AgentMemory while labeling finalization failures precisely."""

    def __init__(self, memory: AgentMemory) -> None:
        self._memory = memory
        self._context_items_total = 0
        self._context_tokens_total = 0
        self.stored_artifacts: int | None = None

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext:
        context = await self._memory.build_context(request)
        diagnostics = self._memory.context_diagnostics
        if isinstance(diagnostics, Mapping):
            used_items = diagnostics.get("used_items")
            used_tokens = diagnostics.get("used_estimated_tokens")
            if isinstance(used_items, int) and used_items >= 0:
                self._context_items_total += used_items
            if isinstance(used_tokens, int) and used_tokens >= 0:
                self._context_tokens_total += used_tokens
        return context

    async def remember(self, entry: MemoryEntry) -> None:
        await self._memory.remember(entry)

    async def record_transition(self, transition: ExperienceTransition) -> None:
        await self._memory.record_transition(transition)

    async def clear(self, run_id: str | None = None) -> None:
        await self._memory.clear(run_id)

    async def finalize_run(self, outcome: RunOutcome) -> None:
        try:
            await self._memory.finalize_run(outcome)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            raise _FinalizationError("memory finalization failed") from error

    async def record_trace(self, write: object) -> object:
        return await self._memory.record_trace(write)  # type: ignore[arg-type]

    @property
    def context_diagnostics(self) -> dict[str, object]:
        return self._memory.context_diagnostics

    @property
    def module_telemetry(self) -> Mapping[str, object] | None:
        value = getattr(self._memory, "module_telemetry", None)
        return value if isinstance(value, Mapping) else None

    @property
    def telemetry_totals(self) -> dict[str, int]:
        return {
            "context_items": self._context_items_total,
            "context_tokens": self._context_tokens_total,
        }

    @property
    def frozen_snapshot_members(self) -> int | None:
        value = getattr(self._memory, "frozen_snapshot_members", None)
        return value if isinstance(value, int) and value >= 0 else None


class _TelemetryModelAdapter:
    """Collect one neutral telemetry sample after every model decision."""

    def __init__(self, model: DecisionModel) -> None:
        self.model = model
        self.samples: list[object] = []

    async def decide(self, context: object) -> object:
        try:
            return await self.model.decide(context)  # type: ignore[arg-type]
        finally:
            telemetry = getattr(self.model, "last_telemetry", None)
            if telemetry is not None:
                self.samples.append(telemetry)

    def prompt_trace(self, context: object) -> object:
        builder = getattr(self.model, "prompt_trace", None)
        if callable(builder):
            return builder(context)
        return {"trace_status": "unavailable"}
