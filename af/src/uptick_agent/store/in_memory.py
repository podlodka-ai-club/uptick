from __future__ import annotations

from typing import cast

from uptick_agent.core.bootstrap_models import (
    EnvironmentBootstrapArtifact,
    EnvironmentBootstrapIdentity,
)
from uptick_agent.core.models import JsonObject, RunManifest, RunResult
from uptick_agent.core.trace_models import RunFinishedPayload, TraceEvent, run_stream_id


class InMemoryRunStore:
    """Deterministic event store for tests and local replay."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []
        self.manifests: dict[str, RunManifest] = {}
        self.bootstrap_artifacts: dict[str, EnvironmentBootstrapArtifact] = {}

    async def load_bootstrap(
        self,
        identity: EnvironmentBootstrapIdentity,
    ) -> EnvironmentBootstrapArtifact | None:
        artifact = self.bootstrap_artifacts.get(identity.model_dump_json())
        return artifact.model_copy(deep=True) if artifact is not None else None

    async def save_bootstrap(self, artifact: EnvironmentBootstrapArtifact) -> None:
        key = artifact.identity.model_dump_json()
        existing = self.bootstrap_artifacts.get(key)
        if existing is not None and existing != artifact:
            raise ValueError("bootstrap identity collision with different artifact")
        self.bootstrap_artifacts[key] = artifact.model_copy(deep=True)

    async def record(self, event: TraceEvent) -> None:
        expected = 1 + max(
            (item.sequence for item in self.events if item.stream_id == event.stream_id),
            default=0,
        )
        if event.sequence != expected:
            raise ValueError(
                f"trace stream {event.stream_id!r} expected sequence {expected}, "
                f"received {event.sequence}"
            )
        self.events.append(event.model_copy(deep=True))

    async def load_stream(self, stream_id: str) -> list[TraceEvent]:
        return [
            event.model_copy(deep=True) for event in self.events if event.stream_id == stream_id
        ]

    async def save_result(self, result: RunResult) -> None:
        stream_id = run_stream_id(result.run_id)
        sequence = 1 + max(
            (event.sequence for event in self.events if event.stream_id == stream_id),
            default=0,
        )
        await self.record(
            TraceEvent(
                stream_id=stream_id,
                stream_kind="run",
                sequence=sequence,
                kind="run_finished",
                payload=RunFinishedPayload(
                    run_id=result.run_id,
                    result=result,
                    result_details=cast(JsonObject, result.model_dump(mode="json")),
                    metrics=result.metrics,
                ),
            )
        )

    async def save_manifest(self, manifest: RunManifest) -> None:
        self.manifests[manifest.run_id] = manifest.model_copy(deep=True)

    async def load_manifest(self, run_id: str) -> RunManifest | None:
        manifest = self.manifests.get(run_id)
        return manifest.model_copy(deep=True) if manifest is not None else None
