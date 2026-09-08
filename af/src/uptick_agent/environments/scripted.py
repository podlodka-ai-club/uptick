from __future__ import annotations

from collections.abc import Iterable

from uptick_agent.core.models import (
    CapabilityCall,
    CapabilityCatalog,
    EnvironmentState,
    EnvironmentTelemetry,
    Observation,
    RunCompletion,
    RunResult,
    RunSpec,
)


class ScriptedEnvironment:
    """Deterministic environment adapter for runtime and portability tests."""

    def __init__(
        self,
        *,
        name: str,
        catalog: CapabilityCatalog,
        observations: Iterable[Observation],
        result: RunResult,
        bootstrap_text: str | None = None,
    ) -> None:
        self.name = name
        self._catalog = catalog
        self._observations = list(observations)
        self._result = result
        self._bootstrap_text = bootstrap_text or f"Environment {name} exposes scripted tools."
        self._run_id: str | None = None
        self.calls: list[CapabilityCall] = []

    async def initialize(self) -> str:
        return self._bootstrap_text

    async def bootstrap_capabilities(self) -> CapabilityCatalog:
        return self._catalog.model_copy(deep=True)

    async def start(self, spec: RunSpec) -> EnvironmentState:
        if spec.environment_profile is None:
            raise ValueError("RunSpec requires a resolved environment profile")
        self._run_id = spec.run_id
        observation = Observation(action_kind="start", summary=f"{self.name} started")
        return EnvironmentState(
            profile=spec.environment_profile,
            status="active",
            latest_observation=observation,
        )

    async def capabilities(self, state: EnvironmentState) -> CapabilityCatalog:
        del state
        return self._catalog.model_copy(deep=True)

    async def execute(self, call: CapabilityCall, state: EnvironmentState) -> Observation:
        del state
        if self._catalog.find(call.name) is None:
            raise ValueError(f"unknown scripted capability {call.name!r}")
        if not self._observations:
            raise RuntimeError("scripted environment has no observation remaining")
        self.calls.append(call)
        return self._observations.pop(0)

    def reduce(
        self,
        state: EnvironmentState,
        call: CapabilityCall,
        observation: Observation,
    ) -> EnvironmentState:
        del call
        return state.model_copy(
            update={
                "status": "terminal" if observation.terminal else "active",
                "latest_observation": observation,
            },
            deep=True,
        )

    async def result(self, state: EnvironmentState, completion: RunCompletion) -> RunResult | None:
        del state
        return self._result.model_copy(
            update={
                "run_id": self._run_id,
                "steps": completion.steps,
                "duration_seconds": completion.duration_seconds,
                "stop_reason": completion.stop_reason,
                "forced": completion.forced,
            }
        )

    def telemetry(self, run_id: str) -> EnvironmentTelemetry:
        if run_id != self._run_id:
            raise ValueError(f"unknown scripted run {run_id!r}")
        return EnvironmentTelemetry()
