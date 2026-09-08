from __future__ import annotations

from uptick_agent.core.bootstrap_models import (
    EnvironmentBootstrapArtifact,
    EnvironmentBootstrapIdentity,
)
from uptick_agent.core.contracts import RunStore
from uptick_agent.core.models import RunManifest, RunResult
from uptick_agent.core.trace_models import DecisionTracePayload, TraceEvent


class ConsoleReportingRunStore:
    """RunStore decorator that preserves concise CLI progress output."""

    def __init__(self, inner: RunStore) -> None:
        self._inner = inner

    async def load_bootstrap(
        self,
        identity: EnvironmentBootstrapIdentity,
    ) -> EnvironmentBootstrapArtifact | None:
        return await self._inner.load_bootstrap(identity)

    async def save_bootstrap(self, artifact: EnvironmentBootstrapArtifact) -> None:
        await self._inner.save_bootstrap(artifact)

    async def record(self, event: TraceEvent) -> None:
        await self._inner.record(event)
        if isinstance(event.payload, DecisionTracePayload):
            decision = event.payload.decision
            observation = event.payload.observation
            if decision is not None and observation is not None:
                action = decision.envelope.selected_action
                marker = "ok" if observation.ok else "error"
                print(
                    f"step={event.payload.iteration} action={action.name} result={marker} "
                    f"{observation.summary}"
                )

    async def load_stream(self, stream_id: str) -> list[TraceEvent]:
        return await self._inner.load_stream(stream_id)

    async def save_result(self, result: RunResult) -> None:
        await self._inner.save_result(result)
        print(f"run={result.run_id} status={result.status} steps={result.steps}")

    async def save_manifest(self, manifest: RunManifest) -> None:
        await self._inner.save_manifest(manifest)

    async def load_manifest(self, run_id: str) -> RunManifest | None:
        return await self._inner.load_manifest(run_id)
