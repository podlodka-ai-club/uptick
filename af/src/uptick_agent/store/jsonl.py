from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import cast
from uuid import uuid4

from uptick_agent.core.bootstrap_models import (
    EnvironmentBootstrapArtifact,
    EnvironmentBootstrapIdentity,
)
from uptick_agent.core.models import JsonObject, RunManifest, RunResult
from uptick_agent.core.trace_models import (
    TRACE_SCHEMA_VERSION,
    RunFinishedPayload,
    TraceEvent,
    run_stream_id,
)


class JsonlRunStore:
    """Append-only, versioned JSONL source of truth for run events."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.path = self.root / "trace.jsonl"
        self.manifest_directory = self.root / "manifests"
        self.bootstrap_directory = self.root / "bootstrap"
        self._lock = asyncio.Lock()
        self._sequences: dict[str, int] = {}
        self._trace_version: int | None = None
        self._validate_existing_trace()

    def ensure_writable(self) -> None:
        if self._trace_version not in {None, TRACE_SCHEMA_VERSION}:
            raise ValueError(
                "legacy trace is read-only; choose a fresh run_store.path for trace v6"
            )

    async def load_bootstrap(
        self,
        identity: EnvironmentBootstrapIdentity,
    ) -> EnvironmentBootstrapArtifact | None:
        path = self._bootstrap_path(identity)
        if not path.exists():
            return None
        artifact = await asyncio.to_thread(
            EnvironmentBootstrapArtifact.model_validate_json,
            path.read_text("utf-8"),
        )
        if artifact.identity != identity:
            raise ValueError("bootstrap artifact identity does not match its lookup key")
        return artifact

    async def save_bootstrap(self, artifact: EnvironmentBootstrapArtifact) -> None:
        async with self._lock:
            self.ensure_writable()
            await asyncio.to_thread(self._write_bootstrap, artifact)

    async def record(self, event: TraceEvent) -> None:
        line = event.model_dump_json() + "\n"
        async with self._lock:
            self.ensure_writable()
            if event.schema_version != TRACE_SCHEMA_VERSION:
                raise ValueError("new trace events must use schema v6")
            expected = self._sequences.get(event.stream_id, 0) + 1
            if event.sequence != expected:
                raise ValueError(
                    f"trace stream {event.stream_id!r} expected sequence {expected}, "
                    f"received {event.sequence}"
                )
            await asyncio.to_thread(self._append, line)
            self._trace_version = event.schema_version
            self._sequences[event.stream_id] = event.sequence

    async def load_stream(self, stream_id: str) -> list[TraceEvent]:
        if not self.path.exists():
            return []
        return await asyncio.to_thread(self._read_stream, stream_id)

    async def save_result(self, result: RunResult) -> None:
        stream_id = run_stream_id(result.run_id)
        sequence = self._sequences.get(stream_id)
        if sequence is None:
            existing = await self.load_stream(stream_id)
            sequence = max((event.sequence for event in existing), default=0)
        await self.record(
            TraceEvent(
                stream_id=stream_id,
                stream_kind="run",
                sequence=sequence + 1,
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
        async with self._lock:
            self.ensure_writable()
            await asyncio.to_thread(self._write_manifest, manifest)

    async def load_manifest(self, run_id: str) -> RunManifest | None:
        path = self._manifest_path(run_id)
        if not path.exists():
            return None
        return await asyncio.to_thread(RunManifest.model_validate_json, path.read_text("utf-8"))

    def _append(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as target:
            target.write(line)

    def _read_stream(self, stream_id: str) -> list[TraceEvent]:
        events: list[TraceEvent] = []
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    event = TraceEvent.model_validate_json(line)
                except ValueError as error:
                    raise ValueError(f"invalid run event at {self.path}:{line_number}") from error
                if event.stream_id == stream_id:
                    events.append(event)
        return events

    def _read_all_events(self) -> list[TraceEvent]:
        events: list[TraceEvent] = []
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    events.append(TraceEvent.model_validate_json(line))
                except ValueError as error:
                    raise ValueError(f"invalid run event at {self.path}:{line_number}") from error
        return events

    def _validate_existing_trace(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    event = TraceEvent.model_validate_json(line)
                except ValueError as error:
                    raise ValueError(
                        f"trace {self.path} contains unsupported or mixed data "
                        f"at line {line_number}"
                    ) from error
                if self._trace_version not in {None, event.schema_version}:
                    raise ValueError(f"trace {self.path} contains mixed schema versions")
                self._trace_version = event.schema_version
                expected = self._sequences.get(event.stream_id, 0) + 1
                if event.sequence != expected:
                    raise ValueError(
                        f"trace {self.path} has invalid sequence for stream "
                        f"{event.stream_id!r} at line {line_number}"
                    )
                self._sequences[event.stream_id] = event.sequence

    def _manifest_path(self, run_id: str) -> Path:
        safe_key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        return self.manifest_directory / f"{safe_key}.json"

    def _bootstrap_path(self, identity: EnvironmentBootstrapIdentity) -> Path:
        encoded = identity.model_dump_json().encode("utf-8")
        safe_key = hashlib.sha256(encoded).hexdigest()
        return self.bootstrap_directory / f"{safe_key}.json"

    def _write_bootstrap(self, artifact: EnvironmentBootstrapArtifact) -> None:
        self.bootstrap_directory.mkdir(parents=True, exist_ok=True)
        target = self._bootstrap_path(artifact.identity)
        serialized = artifact.model_dump_json(indent=2)
        if target.exists():
            existing = EnvironmentBootstrapArtifact.model_validate_json(target.read_text("utf-8"))
            if existing != artifact:
                raise ValueError("bootstrap identity collision with different artifact")
            return
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(serialized, encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_manifest(self, manifest: RunManifest) -> None:
        self.manifest_directory.mkdir(parents=True, exist_ok=True)
        target = self._manifest_path(manifest.run_id)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
