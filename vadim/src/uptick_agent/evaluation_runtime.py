"""Provider- and environment-neutral execution for the Stage 7 v2 matrix.

The runtime deliberately owns only experiment evidence and ordering.  The
environment, model, and memory implementations are injected through narrow
factories, so this module can run deterministic fakes as well as the real
adapters without importing a provider SDK or simulator client.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from uptick_agent.evaluation import (
    FrozenEvaluationBinding,
    MemoryTelemetry,
    ProviderTelemetry,
    V2AttemptRecord,
    V2Condition,
    V2Manifest,
    V2OutcomeMetrics,
    V2Report,
    V2RunMatrixBlock,
    V2SnapshotRef,
    aggregate_report,
    environment_pin_for_seed,
    freeze_evaluation_binding,
    sha256_json,
)
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.contracts import (
    DecisionMemoryContext,
    ExperienceTransition,
    MemoryContextRequest,
    RunOutcome,
)
from uptick_agent.memory.in_memory import InMemoryMemory
from uptick_agent.memory.stores.contracts import (
    RecordWrite,
    StoredRecord,
    StructuredMemoryStore,
    canonical_json,
)
from uptick_agent.memory.stores.in_memory import InMemoryStructuredStore
from uptick_agent.models import (
    AgentConfig,
    MemoryEntry,
    RunResult,
    StepRecord,
    ToolResult,
)
from uptick_agent.ports import AgentMemory, DecisionModel, Environment, EnvironmentSession
from uptick_agent.redaction import redact_text, sanitize_json
from uptick_agent.runner import AgentRunner

T = TypeVar("T")
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")


def _json_value(value: object) -> object:
    """Return a redacted, finite JSON-compatible value for evidence storage."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif is_dataclass(value):
        value = asdict(value)
    return sanitize_json(value)


def _as_json_mapping(value: object) -> dict[str, object]:
    safe = _json_value(value)
    if not isinstance(safe, dict):
        raise TypeError("artifact value must be a JSON object")
    return safe


class EvaluationArtifactStore(Protocol):
    """Durable boundary for immutable evaluation artifacts."""

    def write_manifest(self, manifest: V2Manifest) -> str: ...

    def put(self, kind: str, artifact_id: str, value: object) -> str: ...

    def append_lifecycle(self, event: LifecycleEvent) -> None: ...


class InMemoryEvaluationArtifactStore:
    """Small deterministic store for unit tests and embedded callers."""

    def __init__(self) -> None:
        self.manifest: V2Manifest | None = None
        self.artifacts: dict[tuple[str, str], dict[str, object]] = {}
        self.lifecycle: list[LifecycleEvent] = []

    def write_manifest(self, manifest: V2Manifest) -> str:
        if self.manifest is not None and self.manifest.manifest_hash != manifest.manifest_hash:
            raise ValueError("evaluation manifest is immutable")
        self.manifest = V2Manifest.model_validate(manifest.model_dump(mode="json"))
        return manifest.manifest_hash

    def put(self, kind: str, artifact_id: str, value: object) -> str:
        _validate_artifact_key(kind, artifact_id)
        safe = _as_json_mapping(value)
        digest = sha256_json(safe)
        key = (kind, artifact_id)
        previous = self.artifacts.get(key)
        if previous is not None and previous["hash"] != digest:
            raise ValueError("evaluation artifact is immutable")
        self.artifacts[key] = {"hash": digest, "value": safe}
        return digest

    def append_lifecycle(self, event: LifecycleEvent) -> None:
        self.lifecycle.append(LifecycleEvent.model_validate(event.model_dump(mode="json")))


class FilesystemEvaluationArtifactStore:
    """Filesystem-backed immutable manifest, artifact, and journal storage."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "artifacts").mkdir(exist_ok=True)

    def write_manifest(self, manifest: V2Manifest) -> str:
        payload = _as_json_mapping(manifest)
        path = self.root / "manifest.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if sha256_json(existing) != sha256_json(payload):
                raise ValueError("evaluation manifest is immutable")
            return manifest.manifest_hash
        _atomic_write(path, payload)
        return manifest.manifest_hash

    def put(self, kind: str, artifact_id: str, value: object) -> str:
        _validate_artifact_key(kind, artifact_id)
        payload = _as_json_mapping(value)
        digest = sha256_json(payload)
        directory = self.root / "artifacts" / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{sha256_json({'id': artifact_id})}.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("hash") != digest:
                raise ValueError("evaluation artifact is immutable")
            return digest
        _atomic_write(path, {"artifact_id": artifact_id, "hash": digest, "value": payload})
        return digest

    def append_lifecycle(self, event: LifecycleEvent) -> None:
        path = self.root / "lifecycle.jsonl"
        rendered = json.dumps(
            _as_json_mapping(event),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def has_lifecycle(self) -> bool:
        """Return whether this artifact directory already contains a journal."""

        path = self.root / "lifecycle.jsonl"
        return path.exists() and path.stat().st_size > 0


def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    rendered = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _validate_artifact_key(kind: str, artifact_id: str) -> None:
    if not _ID.fullmatch(kind) or not _ID.fullmatch(artifact_id):
        raise ValueError("artifact keys must be bounded identifiers")


def _stable_run_identifier(manifest_hash: str, *, block_id: str, condition_id: str) -> str:
    """Keep physical IDs bounded even when user-facing profile IDs are long."""

    digest = sha256_json(
        {"manifest_hash": manifest_hash, "block_id": block_id, "condition_id": condition_id}
    )
    return f"run:{manifest_hash[:16]}:{digest[:48]}"


class LifecycleEvent(BaseModel):
    """One immutable journal event containing a complete attempt snapshot."""

    model_config = {"extra": "forbid", "validate_default": True}

    sequence: int = Field(ge=0)
    recorded_at: datetime
    attempt: V2AttemptRecord
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _hash_matches(self) -> LifecycleEvent:
        payload = self.model_dump(mode="json")
        payload.pop("event_hash", None)
        if self.event_hash != sha256_json(payload):
            raise ValueError("lifecycle event hash does not match content")
        return self


class EvaluationJournal:
    """Append-only attempt journal with explicit lifecycle transition checks."""

    def __init__(
        self,
        manifest: V2Manifest,
        *,
        artifacts: EvaluationArtifactStore | None = None,
    ) -> None:
        self.manifest = V2Manifest.model_validate(manifest.model_dump(mode="json"))
        self.artifacts = artifacts or InMemoryEvaluationArtifactStore()
        self.artifacts.write_manifest(self.manifest)
        if getattr(self.artifacts, "has_lifecycle", lambda: False)():
            raise ValueError(
                "evaluation artifact directory already contains a lifecycle journal; "
                "resume/replay is not implemented"
            )
        self._events: list[LifecycleEvent] = []
        self._last_status: dict[str, str] = {}
        self._identity: dict[str, tuple[object, ...]] = {}

    @property
    def events(self) -> tuple[LifecycleEvent, ...]:
        return tuple(
            LifecycleEvent.model_validate(event.model_dump(mode="json")) for event in self._events
        )

    def append(self, attempt: V2AttemptRecord) -> LifecycleEvent:
        owned = V2AttemptRecord.model_validate(attempt.model_dump(mode="json"))
        if owned.manifest_id != self.manifest.manifest_id:
            raise ValueError("attempt does not belong to the sealed manifest")
        previous = self._last_status.get(owned.attempt_id)
        if previous is None and owned.status != "requested":
            raise ValueError("an attempt must start with a requested event")
        if previous is not None and not _allowed_transition(previous, owned.status):
            raise ValueError(f"invalid lifecycle transition {previous!r} -> {owned.status!r}")
        identity = (
            owned.manifest_id,
            owned.logical_run_id,
            owned.block_id,
            owned.phase,
            owned.condition_id,
            owned.environment_id,
            owned.scenario_id,
            owned.world_seed,
            owned.replicate_index,
            owned.attempt_index,
            owned.retry_of,
        )
        if previous is None:
            self._identity[owned.attempt_id] = identity
        elif self._identity[owned.attempt_id] != identity:
            raise ValueError("attempt identity changed across lifecycle events")
        payload = {
            "sequence": len(self._events),
            "recorded_at": datetime.now(UTC),
            "attempt": owned.model_dump(mode="json"),
        }
        event = LifecycleEvent(**payload, event_hash=sha256_json(payload))
        self._events.append(event)
        self._last_status[owned.attempt_id] = owned.status
        self.artifacts.append_lifecycle(event)
        return event

    def reduce_attempts(self) -> tuple[V2AttemptRecord, ...]:
        """Reduce immutable history to the latest snapshot per physical attempt."""

        latest: dict[str, V2AttemptRecord] = {}
        for event in self._events:
            latest[event.attempt.attempt_id] = event.attempt
        return tuple(latest.values())


def _allowed_transition(previous: str, current: str) -> bool:
    if previous in {"completed", "failed", "interrupted", "excluded"}:
        return False
    if previous == "requested":
        return current in {"running", "completed", "failed", "interrupted", "excluded"}
    return current in {"completed", "failed", "interrupted", "excluded"}


class EvaluationEnvironmentFactory(Protocol):
    def __call__(
        self, block: V2RunMatrixBlock, condition: V2Condition, attempt: V2AttemptRecord
    ) -> Environment | Awaitable[Environment]: ...


class EvaluationModelFactory(Protocol):
    def __call__(
        self,
        block: V2RunMatrixBlock,
        condition: V2Condition,
        attempt: V2AttemptRecord,
        run_id: str,
    ) -> DecisionModel | Awaitable[DecisionModel]: ...


class EvaluationMemoryFactory(Protocol):
    def __call__(
        self,
        block: V2RunMatrixBlock,
        condition: V2Condition,
        attempt: V2AttemptRecord,
        run_id: str,
        phase: Literal["training", "evaluation"],
        binding: FrozenEvaluationBinding | None,
    ) -> AgentMemory | Awaitable[AgentMemory]: ...


class EvaluationBindingFactory(Protocol):
    def __call__(
        self,
        condition: V2Condition,
        training_attempts: tuple[V2AttemptRecord, ...],
    ) -> FrozenEvaluationBinding | Awaitable[FrozenEvaluationBinding]: ...


class EvaluationConfigFactory(Protocol):
    def __call__(
        self, block: V2RunMatrixBlock, condition: V2Condition, attempt: V2AttemptRecord
    ) -> AgentConfig | Awaitable[AgentConfig]: ...


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

    async def finish(
        self,
        session: EnvironmentSession,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ) -> RunResult:
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
        self.steps: list[StepRecord] = []
        self.result: RunResult | None = None

    async def on_step(self, record: StepRecord) -> None:
        self.steps.append(record.model_copy(deep=True))

    async def on_finish(self, result: RunResult) -> None:
        self.result = result.model_copy(deep=True)


class _FinalizationError(RuntimeError):
    pass


class _MemoryAdapter:
    """Preserve AgentMemory while labeling finalization failures precisely."""

    def __init__(self, memory: AgentMemory) -> None:
        self._memory = memory
        self._context_items_total = 0
        self._context_tokens_total = 0

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
        except BaseException as error:
            raise _FinalizationError("memory finalization failed") from error

    async def record_trace(self, write: object) -> object:
        return await self._memory.record_trace(write)  # type: ignore[arg-type]

    @property
    def context_diagnostics(self) -> dict[str, object]:
        return self._memory.context_diagnostics

    @property
    def telemetry_totals(self) -> dict[str, int]:
        return {
            "context_items": self._context_items_total,
            "context_tokens": self._context_tokens_total,
        }


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


class SnapshotReadStore:
    """Strict read-only view containing only verified frozen members."""

    def __init__(self, base: StructuredMemoryStore, refs: Sequence[V2SnapshotRef]) -> None:
        self._base = base
        self._refs = tuple(refs)
        self._records: dict[tuple[str, str], StoredRecord] = {}
        self._snapshots: dict[str, object] = {}
        self._members: dict[str, dict[str, str]] = {}
        self._loaded = False

    async def load(self) -> None:
        if self._loaded:
            return
        from uptick_agent.memory.stores.contracts import MemorySnapshot

        for ref in self._refs:
            snapshot = await self._base.get_snapshot(snapshot_id=ref.snapshot_id)
            if snapshot is None:
                raise ValueError("frozen memory snapshot is missing")
            snapshot = MemorySnapshot.validate_integrity(snapshot)
            if snapshot.namespace != ref.namespace or snapshot.content_hash != ref.content_hash:
                raise ValueError("frozen memory snapshot hash changed")
            self._snapshots[ref.snapshot_id] = snapshot.model_copy(deep=True)
            admitted = self._members.setdefault(ref.namespace, {})
            for member in snapshot.members:
                previous = admitted.get(member.record_id)
                if previous is not None and previous != member.content_hash:
                    raise ValueError("frozen snapshot namespaces disagree about a member")
                admitted[member.record_id] = member.content_hash
                record = await self._base.get(namespace=ref.namespace, record_id=member.record_id)
                if record is None:
                    raise ValueError("frozen memory snapshot member is missing")
                record = StoredRecord.validate_integrity(record)
                if record.content_hash != member.content_hash:
                    raise ValueError("frozen memory snapshot member hash changed")
                self._records[(ref.namespace, member.record_id)] = record.model_copy(deep=True)
        self._loaded = True

    async def append(self, *args: object, **kwargs: object) -> None:
        raise ValueError("evaluation read store is immutable")

    async def get(self, *, namespace: str, record_id: str) -> StoredRecord | None:
        self._require_loaded()
        record = self._records.get((namespace, record_id))
        return record.model_copy(deep=True) if record is not None else None

    async def list(self, *, namespace: str) -> list[StoredRecord]:
        self._require_loaded()
        records = [
            record.model_copy(deep=True)
            for (record_namespace, _), record in self._records.items()
            if record_namespace == namespace
        ]
        return sorted(records, key=lambda item: (item.created_at, item.record_id))

    async def create_snapshot(self, *args: object, **kwargs: object) -> None:
        raise ValueError("evaluation read store cannot create snapshots")

    async def get_snapshot(self, *, snapshot_id: str):
        self._require_loaded()
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is not None:
            return snapshot.model_copy(deep=True)
        from uptick_agent.memory.stores.contracts import MemorySnapshot

        nested = await self._base.get_snapshot(snapshot_id=snapshot_id)
        if nested is None:
            return None
        nested = MemorySnapshot.validate_integrity(nested)
        admitted = self._members.get(nested.namespace)
        if admitted is None:
            raise ValueError("nested snapshot namespace is outside the frozen input")
        if any(admitted.get(member.record_id) != member.content_hash for member in nested.members):
            raise ValueError("nested snapshot contains post-freeze or changed members")
        self._snapshots[snapshot_id] = nested.model_copy(deep=True)
        return nested.model_copy(deep=True)

    @property
    def member_count(self) -> int:
        return sum(len(members) for members in self._members.values())

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("snapshot store must be loaded before reads")


class EvaluationMemoryFacade:
    """Reads from frozen training memory and writes to an isolated runtime."""

    def __init__(
        self,
        read_runtime: AgentMemory,
        write_runtime: AgentMemory,
        *,
        frozen_snapshot_members: int | None = None,
    ) -> None:
        self._read = read_runtime
        self._write = write_runtime
        self.frozen_snapshot_members = frozen_snapshot_members

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext:
        return await self._read.build_context(request)

    async def remember(self, entry: MemoryEntry) -> None:
        await self._write.remember(entry)

    async def record_transition(self, transition: ExperienceTransition) -> None:
        await self._write.record_transition(transition)

    async def clear(self, run_id: str | None = None) -> None:
        await self._write.clear(run_id)

    async def finalize_run(self, outcome: RunOutcome) -> None:
        await self._write.finalize_run(outcome)

    async def record_trace(self, write: object) -> object:
        return await self._write.record_trace(write)  # type: ignore[arg-type]

    @property
    def context_diagnostics(self) -> dict[str, object]:
        return self._read.context_diagnostics


class DefaultEvaluationMemoryFactory:
    """Compose canonical memory modules with immutable evaluation read views."""

    def __init__(self, manifest: V2Manifest, store: StructuredMemoryStore | None = None) -> None:
        self.manifest = manifest
        self.store = store or InMemoryStructuredStore()
        self._legacy_training: dict[str, InMemoryMemory] = {}
        self._training_declarations: dict[str, tuple[object, ...]] = {}
        self._training_runtimes: dict[str, object] = {}
        self._prepared = False

    async def prepare(self) -> None:
        """Refuse a second execution against a nonempty training store."""

        if self._prepared:
            raise ValueError("evaluation memory factory cannot be reused")
        self._prepared = True
        for condition in self.manifest.profile.conditions:
            base = self._namespace(condition.condition_id, "training")
            namespaces = self._module_namespaces(
                condition.condition_id,
                condition.memory_configuration,
                base,
            )
            for namespace in namespaces:
                if await self.store.list(namespace=namespace):
                    raise ValueError(
                        "evaluation training namespace is nonempty; resume/replay is unsupported"
                    )
                for suffix in (":snapshot", ":pre-freeze"):
                    if await self.store.get_snapshot(snapshot_id=f"{namespace}{suffix}"):
                        raise ValueError(
                            "evaluation training snapshot already exists; "
                            "resume/replay is unsupported"
                        )

    def _namespace(self, condition_id: str, phase: str, suffix: str = "") -> str:
        digest = sha256_json(
            {
                "manifest_hash": self.manifest.manifest_hash,
                "condition_id": condition_id,
                "phase": phase,
                "suffix": suffix,
            }
        )[:48]
        return f"v2-{phase}:{digest}"

    def memory_metadata(
        self,
        condition: V2Condition,
        attempt: V2AttemptRecord,
        phase: Literal["training", "evaluation"],
    ) -> dict[str, str]:
        base = self._namespace(condition.condition_id, "training")
        if phase == "training":
            memory_namespace = base
        else:
            suffix = sha256_json(
                {"attempt_id": attempt.attempt_id, "condition_id": condition.condition_id}
            )[:32]
            memory_namespace = self._namespace(condition.condition_id, "evaluation", suffix)
        return {
            "memory_namespace": memory_namespace,
            "audit_namespace": f"{memory_namespace}:audit",
        }

    @staticmethod
    def _module_namespaces(
        condition_id: str, configuration: MemoryConfiguration, base: str
    ) -> list[str]:
        namespaces: list[str] = []
        if configuration.episodic.enabled:
            namespaces.append(base)
        if configuration.lessons.enabled:
            namespaces.append(f"{base}:lessons")
        if any(
            module.enabled
            for module in (
                configuration.lessons,
                configuration.world_model,
                configuration.playbooks,
                configuration.tool_knowledge,
                configuration.consolidation,
            )
        ):
            namespaces.append(f"{base}:lessons:declarations")
        if configuration.world_model.enabled:
            namespaces.append(f"{base}:world")
        if configuration.playbooks.enabled:
            namespaces.append(f"{base}:playbooks")
        if configuration.tool_knowledge.enabled:
            namespaces.append(f"{base}:tool-knowledge")
        if configuration.consolidation.enabled:
            namespaces.append(f"{base}:consolidation")
        if configuration.consolidation.enabled or configuration.forgetting.enabled:
            namespaces.append(f"{base}:maintenance")
        if configuration.compatibility_legacy.enabled:
            namespaces.append(f"{base}:legacy")
        if configuration.audit.enabled:
            namespaces.append(f"{base}:audit")
        return namespaces

    def _declaration(self, block: V2RunMatrixBlock, attempt: V2AttemptRecord, phase: str):
        from uptick_agent.evaluation import environment_pin_for_seed
        from uptick_agent.memory.lesson_contracts import LessonRunDeclaration

        environment = environment_pin_for_seed(self.manifest.profile, block.world_seed)
        if not environment.context_identity_verified:
            return None
        return LessonRunDeclaration(
            run_id=attempt.run_id or "unknown",
            logical_run_id=attempt.logical_run_id,
            attempt_index=attempt.attempt_index,
            phase="learning" if phase == "training" else "frozen_evaluation",
            environment_id=attempt.environment_id,
            scenario_id=attempt.scenario_id,
            environment_content_hash=environment.environment_content_hash,
            scenario_content_hash=environment.scenario_content_hash,
            eligible=phase == "training" and attempt.attempt_index == 0,
        )

    def _audit_sink(
        self, store: StructuredMemoryStore, namespace: str, configuration: MemoryConfiguration
    ):
        if not configuration.audit.enabled:
            return None
        from uptick_agent.memory.audit import StructuredAuditTraceSink

        return StructuredAuditTraceSink(
            store,
            namespace=namespace,
            configuration=configuration.audit,
            runtime_configuration_fingerprint=configuration.fingerprint,
        )

    def _compose(
        self,
        store: StructuredMemoryStore,
        condition: V2Condition,
        *,
        episodic_namespace: str,
        lesson_namespace: str,
        audit_namespace: str,
        declarations: Sequence[object],
        legacy_delegate: InMemoryMemory | None = None,
        audit_store: StructuredMemoryStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> AgentMemory:
        configuration = condition.memory_configuration
        sink = self._audit_sink(audit_store or store, audit_namespace, configuration)
        from uptick_agent.experimental_runtime import compose_experimental_runtime

        return compose_experimental_runtime(
            configuration,
            store,
            namespace=episodic_namespace,
            condition_id=condition.condition_id,
            run_declarations=declarations,
            legacy_memory=legacy_delegate,
            clock=clock,
            audit_sink=sink,
        )

    async def __call__(
        self,
        block: V2RunMatrixBlock,
        condition: V2Condition,
        attempt: V2AttemptRecord,
        run_id: str,
        phase: Literal["training", "evaluation"],
        binding: FrozenEvaluationBinding | None,
    ) -> AgentMemory:
        base = self._namespace(condition.condition_id, "training")
        declaration = self._declaration(block, attempt, phase)
        if phase == "training":
            if declaration is not None:
                previous = self._training_declarations.setdefault(condition.condition_id, ())
                self._training_declarations[condition.condition_id] = (*previous, declaration)
            legacy = None
            if condition.memory_configuration.compatibility_legacy.enabled:
                legacy = self._legacy_training.setdefault(condition.condition_id, InMemoryMemory())
            runtime = self._compose(
                self.store,
                condition,
                episodic_namespace=base,
                lesson_namespace=f"{base}:lessons",
                audit_namespace=f"{base}:audit",
                declarations=self._training_declarations.get(condition.condition_id, ()),
                legacy_delegate=legacy,
            )
            self._training_runtimes[condition.condition_id] = runtime
            return runtime
        if binding is None:
            raise ValueError("evaluation memory requires a frozen binding")
        configuration = condition.memory_configuration
        read_store = SnapshotReadStore(self.store, binding.snapshot_refs)
        await read_store.load()
        legacy_enabled = configuration.compatibility_legacy.enabled
        read_legacy = None
        if legacy_enabled:
            read_records = await read_store.list(namespace=f"{base}:legacy")
            read_legacy = InMemoryMemory(
                MemoryEntry.model_validate(record.payload)
                for record in read_records
                if record.record_type == "memory-entry"
            )
        training_declarations = self._training_declarations.get(condition.condition_id, ())
        eval_declaration = (declaration,) if declaration is not None else ()
        eval_suffix = sha256_json(
            {"attempt_id": attempt.attempt_id, "condition_id": condition.condition_id}
        )[:32]
        eval_base = self._namespace(condition.condition_id, "evaluation", eval_suffix)
        read_runtime = self._compose(
            read_store,
            condition,
            episodic_namespace=base,
            lesson_namespace=f"{base}:lessons",
            audit_namespace=f"{eval_base}:audit",
            declarations=(*training_declarations, *eval_declaration),
            legacy_delegate=read_legacy,
            audit_store=self.store,
            clock=lambda: binding.created_at,
        )
        write_runtime = self._compose(
            self.store,
            condition,
            episodic_namespace=eval_base,
            lesson_namespace=f"{eval_base}:lessons",
            audit_namespace=f"{eval_base}:audit",
            declarations=eval_declaration,
            legacy_delegate=InMemoryMemory() if legacy_enabled else None,
            clock=lambda: binding.created_at,
        )
        return EvaluationMemoryFacade(
            read_runtime,
            write_runtime,
            frozen_snapshot_members=read_store.member_count,
        )

    async def freeze_binding(
        self, condition: V2Condition, training_attempts: tuple[V2AttemptRecord, ...]
    ) -> FrozenEvaluationBinding:
        base = self._namespace(condition.condition_id, "training")
        configuration = condition.memory_configuration
        namespaces = self._module_namespaces(condition.condition_id, configuration, base)
        training_runtime = self._training_runtimes.get(condition.condition_id)
        if configuration.consolidation.enabled:
            if training_runtime is None:
                raise ValueError("consolidation requires a composed training runtime")
            consolidate = getattr(training_runtime, "consolidate_before_freeze", None)
            if not callable(consolidate):
                raise ValueError("consolidation runtime does not expose its explicit operation")
            pre_snapshot_id = f"{base}:pre-freeze"
            await self.store.create_snapshot(
                namespace=base,
                snapshot_id=pre_snapshot_id,
                operation="evaluation-pre-freeze",
                idempotency_key=f"{pre_snapshot_id}:snapshot",
            )
            request_id = f"{self.manifest.manifest_id}:{condition.condition_id}:consolidate"
            await consolidate(
                pre_snapshot_id,
                request_id=request_id,
                idempotency_key=f"{request_id}:dry-run",
                apply=False,
            )
            await consolidate(
                pre_snapshot_id,
                request_id=request_id,
                idempotency_key=f"{request_id}:apply",
                apply=True,
            )
        if configuration.compatibility_legacy.enabled:
            legacy = self._legacy_training.get(condition.condition_id)
            if legacy is not None:
                for entry in legacy.entries:
                    await self.store.append(
                        RecordWrite(
                            namespace=f"{base}:legacy",
                            record_id=entry.id,
                            record_type="memory-entry",
                            payload=entry.model_dump(mode="json"),
                            created_at=entry.created_at,
                        ),
                        operation="evaluation-freeze-legacy",
                        idempotency_key=f"legacy:{entry.id}",
                    )
        await self._validate_training_provenance(condition, training_attempts, namespaces)
        refs: list[V2SnapshotRef] = []
        for namespace in namespaces:
            snapshot_id = f"{namespace}:snapshot"
            receipt = await self.store.create_snapshot(
                namespace=namespace,
                snapshot_id=snapshot_id,
                operation="evaluation-freeze-training",
                idempotency_key=f"{snapshot_id}:freeze",
            )
            refs.append(
                V2SnapshotRef(
                    namespace=namespace,
                    snapshot_id=snapshot_id,
                    content_hash=receipt.snapshot.content_hash,
                )
            )
        binding_digest = sha256_json(
            {
                "manifest_hash": self.manifest.manifest_hash,
                "condition_id": condition.condition_id,
            }
        )[:48]
        return freeze_evaluation_binding(
            self.manifest,
            condition_id=condition.condition_id,
            cache_namespace=f"v2-cache:{binding_digest}",
            audit_namespace=f"v2-audit:{binding_digest}",
            snapshot_refs=refs,
            training_attempt_ids=(item.attempt_id for item in training_attempts),
            training_world_contexts={
                item.world_seed: environment_pin_for_seed(self.manifest.profile, item.world_seed)
                for item in training_attempts
            },
        )

    async def _validate_training_provenance(
        self,
        condition: V2Condition,
        training_attempts: tuple[V2AttemptRecord, ...],
        namespaces: Sequence[str],
    ) -> None:
        """Reject pre-existing or cross-run records before freezing inputs."""

        from uptick_agent.memory.audit import AuditTraceEvent
        from uptick_agent.memory.candidate_validation import (
            _validate_outcome_record,
            _validate_transition_record,
            validate_evidence,
        )
        from uptick_agent.memory.consolidation import (
            CONSOLIDATION_APPLY_RECORD_TYPE,
            CONSOLIDATION_PLAN_RECORD_TYPE,
            ConsolidationApply,
            ConsolidationDelta,
            ConsolidationPlan,
            StoredSnapshotEvidenceSource,
        )
        from uptick_agent.memory.lesson_contracts import (
            LessonEvidence,
            LessonRunDeclaration,
            ValidatedLesson,
            declaration_hash,
            snapshot_input_hash,
        )
        from uptick_agent.memory.lessons import LessonBatch
        from uptick_agent.memory.maintenance import MaintenancePlan
        from uptick_agent.memory.patterns import ValidatedPattern
        from uptick_agent.memory.playbooks import PLAYBOOK_BATCH_RECORD_TYPE, PlaybookBatch
        from uptick_agent.memory.stores.contracts import MemorySnapshot
        from uptick_agent.memory.tool_knowledge import (
            TOOL_KNOWLEDGE_BATCH_RECORD_TYPE,
            ToolKnowledgeBatch,
        )
        from uptick_agent.memory.world_model import WORLD_BATCH_RECORD_TYPE, WorldHypothesisBatch

        attempts_by_run = {
            attempt.run_id: attempt for attempt in training_attempts if attempt.run_id
        }
        if len(attempts_by_run) != sum(attempt.run_id is not None for attempt in training_attempts):
            raise ValueError("training attempts must have unique physical run IDs")
        base = self._namespace(condition.condition_id, "training")

        def require_run(run_id: object, *, label: str) -> V2AttemptRecord:
            if not isinstance(run_id, str) or run_id not in attempts_by_run:
                raise ValueError(f"{label} references a run outside this training split")
            return attempts_by_run[run_id]

        def require_context(attempt: V2AttemptRecord, environment_id: object, scenario_id: object):
            if environment_id is not None and environment_id != attempt.environment_id:
                raise ValueError("training provenance environment identity changed")
            if scenario_id is not None and scenario_id != attempt.scenario_id:
                raise ValueError("training provenance scenario identity changed")

        def validate_declaration(value: object, *, label: str) -> LessonRunDeclaration:
            declaration = (
                value
                if isinstance(value, LessonRunDeclaration)
                else LessonRunDeclaration.model_validate(value)
            )
            attempt = require_run(declaration.run_id, label=label)
            if declaration.phase != "learning" or declaration.attempt_index != 0:
                raise ValueError("training provenance contains a non-learning declaration")
            require_context(attempt, declaration.environment_id, declaration.scenario_id)
            context = environment_pin_for_seed(self.manifest.profile, attempt.world_seed)
            if not context.context_identity_verified:
                raise ValueError("unverified training context cannot provide lesson provenance")
            if (
                declaration.environment_content_hash != context.environment_content_hash
                or declaration.scenario_content_hash != context.scenario_content_hash
            ):
                raise ValueError("training declaration context hashes do not match the manifest")
            return declaration

        def validate_record(record: StoredRecord, *, expected_namespace: str) -> None:
            if record.namespace != expected_namespace:
                raise ValueError("training provenance record crossed a memory namespace")
            payload = record.payload
            if record.record_type == "experience-transition":
                transition = _validate_transition_record(record)
                attempt = require_run(transition.run_id, label="experience transition")
                require_context(attempt, transition.environment_id, transition.scenario_id)
            elif record.record_type == "run-outcome":
                outcome = _validate_outcome_record(record)
                require_run(outcome.run_id, label="run outcome")
            elif record.record_type == "memory-entry":
                entry = MemoryEntry.model_validate(payload)
                require_run(entry.run_id, label="legacy memory entry")
            elif record.record_type == "audit-trace-event":
                event = AuditTraceEvent.model_validate(payload)
                require_run(event.run_id, label="audit trace")
            elif record.record_type == "lesson-run-declaration":
                validate_declaration(payload, label="lesson declaration")
            elif record.record_type == "lesson-capture-context":
                if set(payload) != {"snapshot_id", "outcome_run_id", "outcome_hash", "runs"}:
                    raise ValueError("lesson capture context payload shape is invalid")
                require_run(payload["outcome_run_id"], label="lesson capture context")
                runs = payload["runs"]
                if not isinstance(runs, list):
                    raise ValueError("lesson capture context declarations are invalid")
                for item in runs:
                    validate_declaration(
                        LessonRunDeclaration.model_validate(item), label="capture run"
                    )
            elif record.record_type == "lesson-batch":
                batch = LessonBatch.model_validate(payload)
                require_run(batch.outcome.run_id, label="lesson batch outcome")
                evidence = LessonEvidence.model_validate(batch.evidence.model_dump(mode="json"))
                evidence = validate_evidence(evidence)
                if evidence.snapshot.namespace != base:
                    raise ValueError("lesson evidence snapshot crossed the training namespace")
                for nested in evidence.records:
                    nested = StoredRecord.validate_integrity(nested)
                    validate_record(nested, expected_namespace=base)
                for declaration in evidence.runs:
                    validate_declaration(declaration, label="lesson evidence declaration")
            elif record.record_type in {
                "memory-maintenance-plan",
                "memory-maintenance-application",
            }:
                plan_payload = payload.get("plan")
                plan = MaintenancePlan.model_validate(plan_payload)
                if plan.namespace != base or plan.maintenance_namespace != expected_namespace:
                    raise ValueError("maintenance provenance crossed the training namespaces")
            elif record.record_type in {
                CONSOLIDATION_PLAN_RECORD_TYPE,
                CONSOLIDATION_APPLY_RECORD_TYPE,
            }:
                if expected_namespace != f"{base}:consolidation":
                    raise ValueError("consolidation provenance crossed the training namespace")
                if record.record_type == CONSOLIDATION_PLAN_RECORD_TYPE:
                    plan = ConsolidationPlan.model_validate(payload)
                    if record.record_id != f"consolidation-plan:{plan.plan_id}":
                        raise ValueError("consolidation plan record ID is invalid")
                    if record.created_at != plan.created_at:
                        raise ValueError("consolidation plan timestamp is invalid")
                else:
                    application = ConsolidationApply.model_validate(payload)
                    if record.record_id != f"consolidation-apply:{application.plan_id}":
                        raise ValueError("consolidation apply record ID is invalid")
                    if record.created_at != application.applied_at:
                        raise ValueError("consolidation apply timestamp is invalid")
            elif record.record_type in {
                WORLD_BATCH_RECORD_TYPE,
                PLAYBOOK_BATCH_RECORD_TYPE,
                TOOL_KNOWLEDGE_BATCH_RECORD_TYPE,
            }:
                batch_types = {
                    WORLD_BATCH_RECORD_TYPE: ("world", WorldHypothesisBatch),
                    PLAYBOOK_BATCH_RECORD_TYPE: ("playbooks", PlaybookBatch),
                    TOOL_KNOWLEDGE_BATCH_RECORD_TYPE: ("tool-knowledge", ToolKnowledgeBatch),
                }
                suffix, batch_type = batch_types[record.record_type]
                if expected_namespace != f"{base}:{suffix}":
                    raise ValueError("derived memory provenance crossed its namespace")
                batch = batch_type.model_validate(payload)
                if record.created_at != batch.outcome.finished_at:
                    raise ValueError("derived memory batch timestamp is invalid")
                require_run(batch.outcome.run_id, label="derived memory outcome")
            else:
                raise ValueError(f"unknown training provenance record type {record.record_type!r}")

        async def validate_maintenance_record(
            record: StoredRecord, *, expected_namespace: str
        ) -> None:
            if record.record_type not in {
                "memory-maintenance-plan",
                "memory-maintenance-application",
            }:
                return
            plan = MaintenancePlan.model_validate(record.payload.get("plan"))
            if plan.namespace != base or plan.maintenance_namespace != expected_namespace:
                raise ValueError("maintenance provenance crossed the training namespaces")
            snapshot = await self.store.get_snapshot(snapshot_id=plan.snapshot_id)
            if snapshot is None:
                raise ValueError("maintenance plan references a missing training snapshot")
            snapshot = MemorySnapshot.validate_integrity(snapshot)
            if (
                snapshot.namespace != base
                or snapshot.content_hash != plan.snapshot_content_hash
                or snapshot.members != plan.snapshot_members
            ):
                raise ValueError("maintenance plan references a changed training snapshot")
            members = {member.record_id: member.content_hash for member in snapshot.members}
            for member in snapshot.members:
                source = await self.store.get(namespace=base, record_id=member.record_id)
                if source is None or StoredRecord.validate_integrity(source).content_hash != (
                    member.content_hash
                ):
                    raise ValueError("maintenance snapshot member is not training provenance")
            for delta in plan.deltas:
                if any(
                    members.get(member.record_id) != member.content_hash
                    for member in delta.source_members
                ):
                    raise ValueError("maintenance delta references a foreign source member")
                if any(
                    members.get(item.artefact_id) != item.content_hash for item in delta.provenance
                ):
                    raise ValueError("maintenance delta provenance is not training evidence")

        async def validate_consolidation_record(
            record: StoredRecord, *, expected_namespace: str
        ) -> None:
            """Verify consolidation receipts against the exact training evidence."""

            if record.record_type not in {
                CONSOLIDATION_PLAN_RECORD_TYPE,
                CONSOLIDATION_APPLY_RECORD_TYPE,
            }:
                return
            consolidation_namespace = f"{base}:consolidation"
            if expected_namespace != consolidation_namespace:
                raise ValueError("consolidation provenance crossed the training namespace")

            async def load_source_snapshot(
                snapshot_id: str,
                snapshot_content_hash: str,
                snapshot_members: Sequence[object],
            ) -> tuple[MemorySnapshot, tuple[StoredRecord, ...], LessonEvidence]:
                raw_snapshot = await self.store.get_snapshot(snapshot_id=snapshot_id)
                if raw_snapshot is None:
                    raise ValueError("consolidation plan references a missing training snapshot")
                snapshot = MemorySnapshot.validate_integrity(raw_snapshot)
                if snapshot.namespace != base:
                    raise ValueError("consolidation source snapshot crossed the training namespace")
                expected_members = tuple(snapshot_members)
                if snapshot.content_hash != snapshot_content_hash or tuple(snapshot.members) != (
                    expected_members
                ):
                    raise ValueError("consolidation plan references a changed training snapshot")
                records: list[StoredRecord] = []
                for member in snapshot.members:
                    source = await self.store.get(namespace=base, record_id=member.record_id)
                    if source is None:
                        raise ValueError("consolidation snapshot member is missing")
                    owned = StoredRecord.validate_integrity(source)
                    if owned.content_hash != member.content_hash:
                        raise ValueError("consolidation snapshot member hash changed")
                    validate_record(owned, expected_namespace=base)
                    if owned.record_type not in {"experience-transition", "run-outcome"}:
                        raise ValueError("consolidation snapshot contains an unsupported record")
                    records.append(owned)
                evidence_source = StoredSnapshotEvidenceSource(
                    self.store,
                    evidence_namespace=base,
                    declaration_namespace=f"{base}:lessons:declarations",
                )
                evidence = await evidence_source.load(snapshot.snapshot_id)
                return snapshot, tuple(records), evidence

            def require_hash_pairs(
                identifiers: Sequence[str],
                hashes: Sequence[str],
                expected: Mapping[str, str],
                label: str,
            ) -> None:
                if len(identifiers) != len(hashes):
                    raise ValueError(f"consolidation {label} IDs and hashes differ")
                for identifier, content_hash in zip(identifiers, hashes, strict=True):
                    if expected.get(identifier) != content_hash:
                        raise ValueError(f"consolidation {label} hash is not training evidence")

            def validate_item(
                item: object,
                *,
                snapshot: MemorySnapshot,
                records: Mapping[str, StoredRecord],
                evidence: LessonEvidence,
                source_refs: Mapping[str, str],
                plan_settings: object,
            ) -> object:
                if not isinstance(item, dict):
                    raise ValueError("consolidation candidate item is malformed")
                kind = item.get("kind")
                validated_payload = item.get("validated")
                if kind == "lesson":
                    validated = ValidatedLesson.model_validate(validated_payload)
                    manifest = validated.manifest
                elif kind == "world_hypothesis":
                    validated = ValidatedPattern.model_validate(validated_payload)
                    manifest = validated.manifest
                else:
                    raise ValueError("consolidation candidate kind is invalid")
                if item.get("status") != validated.status:
                    raise ValueError("consolidation candidate status is not canonical")
                if item.get("candidate") != validated.candidate.model_dump(mode="json"):
                    raise ValueError("consolidation candidate payload is not canonical")
                expected_settings = (
                    getattr(plan_settings, "lesson_settings", None)
                    if kind == "lesson"
                    else getattr(plan_settings, "pattern_settings", None)
                )
                if expected_settings is None or item.get("settings") != (
                    expected_settings.model_dump(mode="json")
                ):
                    raise ValueError("consolidation candidate settings are not canonical")
                if (
                    manifest.snapshot_id != snapshot.snapshot_id
                    or manifest.snapshot_content_hash != snapshot.content_hash
                    or manifest.input_hash != snapshot_input_hash(evidence)
                ):
                    raise ValueError("consolidation candidate is bound to another snapshot")
                record_hashes = {
                    member.record_id: member.content_hash for member in snapshot.members
                }
                require_hash_pairs(
                    manifest.record_ids, manifest.record_hashes, record_hashes, "record"
                )
                require_hash_pairs(
                    getattr(manifest, "outcome_record_ids", ()),
                    getattr(manifest, "outcome_record_hashes", ()),
                    record_hashes,
                    "outcome record",
                )
                declaration_hashes = {
                    f"declaration:{run.run_id}:{run.attempt_index}": declaration_hash(run)
                    for run in evidence.runs
                }
                require_hash_pairs(
                    manifest.declaration_ids,
                    manifest.declaration_hashes,
                    declaration_hashes,
                    "declaration",
                )
                require_hash_pairs(
                    manifest.source_leaf_ids,
                    manifest.source_leaf_hashes,
                    source_refs,
                    "source leaf",
                )
                for field_name in (
                    "searched_evidence",
                    "support_evidence",
                    "counter_evidence",
                ):
                    require_hash_pairs(
                        getattr(manifest, f"{field_name}_ids"),
                        getattr(manifest, f"{field_name}_hashes"),
                        record_hashes,
                        field_name,
                    )
                if any(run_id not in attempts_by_run for run_id in manifest.support_run_ids):
                    raise ValueError("consolidation support references a foreign training run")
                logical_run_ids = {attempt.logical_run_id for attempt in training_attempts}
                if any(
                    logical_id not in logical_run_ids
                    for logical_id in manifest.support_logical_run_ids
                ):
                    raise ValueError("consolidation support references a foreign logical run")
                return validated

            if record.record_type == CONSOLIDATION_PLAN_RECORD_TYPE:
                plan = ConsolidationPlan.model_validate(record.payload)
                if record.record_id != f"consolidation-plan:{plan.plan_id}":
                    raise ValueError("consolidation plan record ID is invalid")
                if record.created_at != plan.created_at:
                    raise ValueError("consolidation plan timestamp is invalid")
                expected_settings = condition.memory_configuration.consolidation_settings
                if expected_settings is None or plan.settings.model_dump(mode="json") != (
                    expected_settings.model_dump(mode="json")
                ):
                    raise ValueError("consolidation plan settings are not the resolved condition")
                snapshot, records, evidence = await load_source_snapshot(
                    plan.snapshot_id, plan.snapshot_content_hash, plan.snapshot_members
                )
                record_map = {item.record_id: item for item in records}
                if len(record_map) != len(records):
                    raise ValueError("consolidation source snapshot repeats a record")
                if plan.evidence_input_hash != snapshot_input_hash(evidence):
                    raise ValueError("consolidation evidence input hash changed")
                member_ids = set(record_map)
                if any(record_id not in member_ids for record_id in plan.replay_record_ids):
                    raise ValueError("consolidation replay references a foreign record")
                if not evidence.runs:
                    if plan.unavailable_reason is None:
                        raise ValueError("consolidation plan lacks authoritative run declarations")
                    if plan.candidate_dispositions or plan.active_items or plan.deltas:
                        raise ValueError(
                            "unavailable consolidation plan contains derived knowledge"
                        )
                    return
                if plan.unavailable_reason is not None:
                    raise ValueError("known consolidation evidence cannot be marked unavailable")
                evidence = validate_evidence(evidence)
                source_refs: dict[str, str] = {}
                for source_record in records:
                    if source_record.record_type != "experience-transition":
                        continue
                    transition = _validate_transition_record(source_record)
                    for ref in transition.provenance:
                        prior = source_refs.get(ref.artefact_id)
                        if prior is not None and prior != ref.content_hash:
                            raise ValueError("consolidation source leaf hash changed")
                        source_refs[ref.artefact_id] = ref.content_hash
                for item in plan.candidate_dispositions:
                    validate_item(
                        item,
                        snapshot=snapshot,
                        records=record_map,
                        evidence=evidence,
                        source_refs=source_refs,
                        plan_settings=plan.settings,
                    )
                active_keys = {
                    canonical_json(item)
                    for item in plan.candidate_dispositions
                    if isinstance(item, dict) and item.get("status") == "active"
                }
                for item in plan.active_items:
                    if canonical_json(item) not in active_keys:
                        raise ValueError("consolidation active item is not a candidate disposition")
                    validate_item(
                        item,
                        snapshot=snapshot,
                        records=record_map,
                        evidence=evidence,
                        source_refs=source_refs,
                        plan_settings=plan.settings,
                    )
                for delta in plan.deltas:
                    owned_delta = ConsolidationDelta.model_validate(delta)
                    if owned_delta.operation != "create":
                        raise ValueError("consolidation delta operation is unsupported")
                    validate_item(
                        owned_delta.payload,
                        snapshot=snapshot,
                        records=record_map,
                        evidence=evidence,
                        source_refs=source_refs,
                        plan_settings=plan.settings,
                    )
                return

            application = ConsolidationApply.model_validate(record.payload)
            if record.record_id != f"consolidation-apply:{application.plan_id}":
                raise ValueError("consolidation apply record ID is invalid")
            if record.created_at != application.applied_at:
                raise ValueError("consolidation apply timestamp is invalid")
            plan_record = await self.store.get(
                namespace=consolidation_namespace,
                record_id=f"consolidation-plan:{application.plan_id}",
            )
            if plan_record is None:
                raise ValueError("consolidation apply references a missing plan")
            plan_record = StoredRecord.validate_integrity(plan_record)
            if plan_record.record_type != CONSOLIDATION_PLAN_RECORD_TYPE:
                raise ValueError("consolidation apply references a non-plan record")
            await validate_consolidation_record(
                plan_record, expected_namespace=consolidation_namespace
            )
            plan = ConsolidationPlan.model_validate(plan_record.payload)
            if (
                application.request_id != plan.request_id
                or application.snapshot_id != plan.snapshot_id
                or application.snapshot_content_hash != plan.snapshot_content_hash
                or application.evidence_input_hash != plan.evidence_input_hash
                or tuple(application.delta_ids)
                != tuple(
                    f"{delta.artefact_type}:{sha256_json(delta.payload)}" for delta in plan.deltas
                )
            ):
                raise ValueError("consolidation apply does not match its plan")

        async def validate_derived_record(record: StoredRecord, *, expected_namespace: str) -> None:
            if record.record_type not in {
                WORLD_BATCH_RECORD_TYPE,
                PLAYBOOK_BATCH_RECORD_TYPE,
                TOOL_KNOWLEDGE_BATCH_RECORD_TYPE,
            }:
                return
            batch_types = {
                WORLD_BATCH_RECORD_TYPE: ("world", WorldHypothesisBatch),
                PLAYBOOK_BATCH_RECORD_TYPE: ("playbooks", PlaybookBatch),
                TOOL_KNOWLEDGE_BATCH_RECORD_TYPE: ("tool-knowledge", ToolKnowledgeBatch),
            }
            suffix, batch_type = batch_types[record.record_type]
            if expected_namespace != f"{base}:{suffix}":
                raise ValueError("derived memory provenance crossed its namespace")
            batch = batch_type.model_validate(record.payload)
            if record.created_at != batch.outcome.finished_at:
                raise ValueError("derived memory batch timestamp is invalid")
            require_run(batch.outcome.run_id, label="derived memory outcome")
            evidence = validate_evidence(batch.evidence)
            if evidence.snapshot.namespace != base:
                raise ValueError("derived memory evidence crossed the training namespace")
            if batch.input_hash != snapshot_input_hash(evidence):
                raise ValueError("derived memory evidence input hash changed")
            if batch.settings_hash != sha256_json(batch.settings.model_dump(mode="json")):
                raise ValueError("derived memory settings hash changed")
            for nested in evidence.records:
                owned_nested = StoredRecord.validate_integrity(nested)
                validate_record(owned_nested, expected_namespace=base)
            for declaration in evidence.runs:
                validate_declaration(declaration, label="derived memory declaration")

        for namespace in namespaces:
            records = await self.store.list(namespace=namespace)
            for record in records:
                owned_record = StoredRecord.validate_integrity(record)
                validate_record(owned_record, expected_namespace=namespace)
                await validate_maintenance_record(owned_record, expected_namespace=namespace)
                await validate_consolidation_record(owned_record, expected_namespace=namespace)
                await validate_derived_record(owned_record, expected_namespace=namespace)


class EvaluationRuntime:
    """Execute the ordered v2 matrix and return a verified exploratory report."""

    def __init__(
        self,
        manifest: V2Manifest,
        *,
        environment_factory: EvaluationEnvironmentFactory,
        model_factory: EvaluationModelFactory,
        memory_factory: EvaluationMemoryFactory | None = None,
        config_factory: EvaluationConfigFactory | None = None,
        binding_factory: EvaluationBindingFactory | None = None,
        runner_factory: Callable[..., AgentRunner] | None = None,
        journal: EvaluationJournal | None = None,
    ) -> None:
        self.manifest = V2Manifest.model_validate(manifest.model_dump(mode="json"))
        self.environment_factory = environment_factory
        self.model_factory = model_factory
        default_memory = DefaultEvaluationMemoryFactory(self.manifest)
        self.memory_factory = memory_factory or default_memory
        self.config_factory = config_factory or self._default_config
        if binding_factory is not None:
            self.binding_factory = binding_factory
        elif memory_factory is None:
            self.binding_factory = default_memory.freeze_binding
        else:
            self.binding_factory = self._missing_binding
        self.runner_factory = runner_factory or AgentRunner
        self.journal = journal or EvaluationJournal(self.manifest)
        if self.journal.manifest.manifest_hash != self.manifest.manifest_hash:
            raise ValueError("journal does not belong to the sealed manifest")
        self.bindings: list[FrozenEvaluationBinding] = []

    async def _missing_binding(
        self, condition: V2Condition, training_attempts: tuple[V2AttemptRecord, ...]
    ) -> FrozenEvaluationBinding:
        raise ValueError("custom memory factories require an explicit binding factory")

    def _default_config(
        self, block: V2RunMatrixBlock, condition: V2Condition, attempt: V2AttemptRecord
    ) -> AgentConfig:
        return AgentConfig(
            agent_id="uptick-v2-evaluation",
            agent_version=self.manifest.profile.source.source_revision[:16],
            max_steps=self.manifest.profile.budget.max_steps,
            memory_recall_limit=min(
                condition.memory_configuration.context_budget.total_items,
                100,
            ),
            objective=(
                "Finish the simulation with uptime >=99%; minimize total infrastructure cost "
                "conditional on SLO success."
            ),
        )

    async def run(self) -> V2Report:
        training = [block for block in self.manifest.run_matrix if block.phase == "training"]
        evaluation = [block for block in self.manifest.run_matrix if block.phase == "evaluation"]
        conditions = {item.condition_id: item for item in self.manifest.profile.conditions}
        prepare = getattr(self.memory_factory, "prepare", None)
        if callable(prepare):
            await _maybe_await(prepare())
        for block in training:
            for condition_id in block.conditions:
                await self._run_bounded_cell(block, conditions[condition_id], binding=None)

        first = self.journal.reduce_attempts()
        for condition in self.manifest.profile.conditions:
            training_attempts = tuple(
                item
                for item in first
                if item.phase == "training"
                and item.condition_id == condition.condition_id
                and item.attempt_index == 0
                and item.status in {"completed", "failed", "interrupted", "excluded"}
            )
            try:
                binding = await _maybe_await(self.binding_factory(condition, training_attempts))
                if not isinstance(binding, FrozenEvaluationBinding):
                    raise TypeError("binding factory must return FrozenEvaluationBinding")
                if binding.manifest_hash != self.manifest.manifest_hash:
                    raise ValueError("binding does not match sealed manifest")
                # Persist the exact frozen input before the first evaluation
                # request.  A crash after this point must leave enough evidence
                # to distinguish a bound evaluation from an unbound one.
                self.journal.artifacts.put(
                    "binding",
                    binding.binding_id,
                    binding.model_dump(mode="json"),
                )
                self.bindings.append(binding)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Evaluation cells are retained as explicit startup failures
                # below; a failed freeze must not trigger API calls.
                self.journal.artifacts.put(
                    "binding-error",
                    f"{self.manifest.manifest_id}:{condition.condition_id}",
                    {
                        "condition_id": condition.condition_id,
                        "error": _failure_reason("binding", error),
                    },
                )
                continue

        for block in evaluation:
            for condition_id in block.conditions:
                condition_binding = next(
                    (item for item in self.bindings if item.condition_id == condition_id), None
                )
                await self._run_bounded_cell(
                    block, conditions[condition_id], binding=condition_binding
                )
        return aggregate_report(
            self.manifest,
            self.journal.reduce_attempts(),
            frozen_bindings=tuple(self.bindings),
        )

    async def _run_bounded_cell(
        self,
        block: V2RunMatrixBlock,
        condition: V2Condition,
        *,
        binding: FrozenEvaluationBinding | None,
    ) -> None:
        timeout = self.manifest.profile.budget.max_wall_seconds
        if timeout is None:
            await self._run_cell(block, condition, binding=binding)
            return
        deadline_expired = asyncio.Event()
        cell = asyncio.create_task(
            self._run_cell(
                block,
                condition,
                binding=binding,
                deadline_expired=deadline_expired,
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(cell), timeout)
        except TimeoutError:
            deadline_expired.set()
            cell.cancel()
            with suppress(asyncio.CancelledError):
                await cell
            logical_id = _stable_run_identifier(
                self.manifest.manifest_hash,
                block_id=block.block_id,
                condition_id=condition.condition_id,
            )
            attempt_id = f"{logical_id}:attempt-0"
            current = next(
                (
                    attempt
                    for attempt in reversed(self.journal.reduce_attempts())
                    if attempt.attempt_id == attempt_id
                ),
                None,
            )
            if current is not None and current.status in {"requested", "running"}:
                await self._terminal(
                    current,
                    status="interrupted",
                    failure_stage="execution",
                    failure_class="interrupted",
                    failure_reason="per-attempt wall time budget exceeded",
                )
        except asyncio.CancelledError:
            cell.cancel()
            with suppress(asyncio.CancelledError):
                await cell
            raise

    async def _run_cell(
        self,
        block: V2RunMatrixBlock,
        condition: V2Condition,
        *,
        binding: FrozenEvaluationBinding | None,
        deadline_expired: asyncio.Event | None = None,
    ) -> None:
        requested_at = datetime.now(UTC)
        logical_id = _stable_run_identifier(
            self.manifest.manifest_hash,
            block_id=block.block_id,
            condition_id=condition.condition_id,
        )
        attempt_id = f"{logical_id}:attempt-0"
        requested = V2AttemptRecord(
            manifest_id=self.manifest.manifest_id,
            attempt_id=attempt_id,
            logical_run_id=logical_id,
            block_id=block.block_id,
            phase=block.phase,
            condition_id=condition.condition_id,
            environment_id=block.environment_id,
            scenario_id=block.scenario_id,
            world_seed=block.world_seed,
            replicate_index=block.replicate_index,
            status="requested",
            requested_at=requested_at,
            frozen_binding_id=binding.binding_id if binding else None,
        )
        self.journal.append(requested)
        if block.phase == "evaluation" and binding is None:
            await self._terminal(
                requested,
                status="failed",
                failure_stage="startup",
                failure_class="validation",
                failure_reason="evaluation binding was not frozen before the cell",
            )
            return
        config: AgentConfig | None = None
        environment: Environment | None = None
        observer = _TraceObserver()
        try:
            config = await _maybe_await(self.config_factory(block, condition, requested))
            environment = await _maybe_await(self.environment_factory(block, condition, requested))
            session, latest = await environment.start(
                seed=block.world_seed,
                agent_id=config.agent_id,
                agent_version=config.agent_version,
            )
        except asyncio.CancelledError:
            trace_hash = _try_trace_artifact(self.journal, requested.attempt_id, observer, None)
            await self._terminal(
                requested,
                status="interrupted",
                failure_stage="startup",
                failure_class="interrupted",
                failure_reason=(
                    "per-attempt wall time budget exceeded"
                    if deadline_expired is not None and deadline_expired.is_set()
                    else "evaluation task cancelled"
                ),
                trace_hash=trace_hash,
            )
            await _close_resource(environment)
            raise
        except Exception as error:
            await self._terminal(
                requested,
                status="failed",
                failure_stage="startup",
                failure_class=_failure_class(error),
                failure_reason=_failure_reason("startup", error),
            )
            await _close_resource(environment)
            return

        run_id = getattr(session, "run_id", None)
        if not isinstance(run_id, str) or not run_id:
            await self._terminal(
                requested,
                status="failed",
                failure_stage="startup",
                failure_class="validation",
                failure_reason="startup returned no physical run ID",
            )
            await _close_resource(environment)
            return
        started_at = datetime.now(UTC)
        running = requested.model_copy(
            update={"status": "running", "run_id": run_id, "started_at": started_at}
        )
        self.journal.append(running)
        model: DecisionModel | None = None
        telemetry_model: _TelemetryModelAdapter | None = None
        try:
            model = await _maybe_await(self.model_factory(block, condition, running, run_id))
            telemetry_model = _TelemetryModelAdapter(model)
            memory = await _maybe_await(
                self.memory_factory(block, condition, running, run_id, block.phase, binding)
            )
            metadata = getattr(self.memory_factory, "memory_metadata", None)
            if callable(metadata):
                values = metadata(condition, running, block.phase)
                if not isinstance(values, Mapping):
                    raise TypeError("memory metadata factory must return a mapping")
                running = running.model_copy(
                    update={
                        key: value
                        for key, value in values.items()
                        if key in {"memory_namespace", "audit_namespace"} and isinstance(value, str)
                    }
                )
            memory_adapter = _MemoryAdapter(memory)
            runner = self.runner_factory(
                config=config,
                model=telemetry_model,
                memory=memory_adapter,
                environment=_PrestartedEnvironment(
                    environment,
                    session,
                    latest,
                    environment_id=block.environment_id,
                    scenario_id=block.scenario_id,
                ),
                observer=observer,
            )
            result = await runner.run(block.world_seed)
            result_hash = self.journal.artifacts.put(
                "run_result", running.attempt_id, result.model_dump(mode="json")
            )
            trace_hash = self.journal.artifacts.put(
                "trace", running.attempt_id, _trace_payload(observer, model)
            )
            outcome = _outcome(result)
            terminal_status: Literal["completed", "failed", "interrupted"]
            if result.status == "completed":
                terminal_status = "completed"
            elif result.status == "running":
                terminal_status = "interrupted"
            else:
                terminal_status = "failed" if result.status == "failed" else "interrupted"
            if terminal_status == "completed":
                await self._terminal(
                    running,
                    status="completed",
                    finished_at=datetime.now(UTC),
                    outcome=outcome,
                    result_hash=result_hash,
                    trace_hash=trace_hash,
                    provider_telemetry=_provider_telemetry(telemetry_model, model),
                    memory_telemetry=_memory_telemetry(memory_adapter, binding),
                )
            else:
                await self._terminal(
                    running,
                    status=terminal_status,
                    finished_at=datetime.now(UTC),
                    outcome=outcome,
                    failure_stage="execution",
                    failure_class="interrupted"
                    if terminal_status == "interrupted"
                    else "permanent",
                    failure_reason=(
                        "run returned running; incomplete SLO evidence is unsuccessful"
                        if result.status == "running"
                        else f"run returned status {result.status}"
                    ),
                    result_hash=result_hash,
                    trace_hash=trace_hash,
                    provider_telemetry=_provider_telemetry(telemetry_model, model),
                    memory_telemetry=_memory_telemetry(memory_adapter, binding),
                )
        except asyncio.CancelledError:
            trace_hash = _try_trace_artifact(self.journal, running.attempt_id, observer, model)
            await self._terminal(
                running,
                status="interrupted",
                finished_at=datetime.now(UTC),
                failure_stage="execution",
                failure_class="interrupted",
                failure_reason=(
                    "per-attempt wall time budget exceeded"
                    if deadline_expired is not None and deadline_expired.is_set()
                    else "evaluation task cancelled"
                ),
                trace_hash=trace_hash,
                provider_telemetry=_provider_telemetry(telemetry_model, model),
            )
            raise
        except _FinalizationError as error:
            trace_hash = _try_trace_artifact(self.journal, running.attempt_id, observer, model)
            await self._terminal(
                running,
                status="failed",
                finished_at=datetime.now(UTC),
                failure_stage="finalization",
                failure_class="permanent",
                failure_reason=_failure_reason("finalization", error),
                trace_hash=trace_hash,
                provider_telemetry=_provider_telemetry(telemetry_model, model),
            )
        except Exception as error:
            trace_hash = _try_trace_artifact(self.journal, running.attempt_id, observer, model)
            await self._terminal(
                running,
                status="failed",
                finished_at=datetime.now(UTC),
                failure_stage="execution",
                failure_class=_failure_class(error),
                failure_reason=_failure_reason("execution", error),
                trace_hash=trace_hash,
                provider_telemetry=_provider_telemetry(telemetry_model, model),
            )
        finally:
            await _close_resource(model)
            await _close_resource(environment)

    async def _terminal(self, base: V2AttemptRecord, **updates: object) -> None:
        status = updates.pop("status")
        finished_at = updates.pop("finished_at", datetime.now(UTC))
        terminal = base.model_copy(update={"status": status, "finished_at": finished_at, **updates})
        self.journal.append(terminal)


async def _maybe_await(value: T | Awaitable[T]) -> T:  # noqa: UP047
    if inspect.isawaitable(value):
        return await value
    return value


def _failure_reason(stage: str, error: BaseException) -> str:
    detail = redact_text(str(error))[:1_500]
    return f"{stage} failed: {type(error).__name__}{': ' + detail if detail else ''}"


async def _close_resource(resource: object | None) -> None:
    if resource is None:
        return
    closer = getattr(resource, "aclose", None)
    if not callable(closer):
        closer = getattr(resource, "close", None)
    if not callable(closer):
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception:
        return


def _failure_class(
    error: BaseException,
) -> Literal["validation", "transient", "permanent", "interrupted", "excluded"]:
    if isinstance(error, (ValueError, TypeError)):
        return "validation"
    return "permanent"


def _outcome(result: RunResult) -> V2OutcomeMetrics:
    status = (
        result.status
        if result.status in {"completed", "failed", "interrupted", "running"}
        else "failed"
    )
    return V2OutcomeMetrics(
        run_status=status,
        uptime_ratio=result.uptime_ratio,
        slo_passed=result.slo_passed,
        total_cost_minor=result.total_cost_minor,
        steps=result.steps,
        duration_seconds=result.duration_seconds,
    )


def _trace_payload(observer: _TraceObserver, model: DecisionModel | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "steps": [item.model_dump(mode="json") for item in observer.steps],
        "finish": observer.result.model_dump(mode="json") if observer.result else None,
    }
    if not observer.steps and observer.result is None:
        payload["trace_status"] = "unavailable"
        payload["model_type"] = type(model).__name__ if model else None
    return payload


def _try_trace_artifact(
    journal: EvaluationJournal,
    attempt_id: str,
    observer: _TraceObserver,
    model: DecisionModel | None,
) -> str | None:
    try:
        return journal.artifacts.put("trace", attempt_id, _trace_payload(observer, model))
    except Exception:
        return None


def _provider_telemetry(
    telemetry_model: _TelemetryModelAdapter | None, model: DecisionModel | None
) -> ProviderTelemetry:
    samples = list(telemetry_model.samples) if telemetry_model is not None else []
    if not samples and model is not None:
        value = getattr(model, "last_telemetry", None)
        if value is not None:
            samples.append(value)
    normalized = [_provider_sample(item) for item in samples]
    normalized = [item for item in normalized if item is not None]
    if not normalized:
        return ProviderTelemetry()
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "time_seconds",
        "cost_minor",
        "request_count",
        "retry_count",
        "usage_reported_requests",
    )
    sums = {
        field: (
            _sum_complete(normalized, field)
            if field
            in {
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
                "cost_minor",
            }
            else _sum_optional(item.get(field) for item in normalized)
        )
        for field in fields
    }
    requests = _sum_complete(normalized, "request_count")
    reported = _sum_complete(normalized, "usage_reported_requests")
    complete = requests is not None and reported is not None and reported >= requests
    currencies = {item.get("cost_currency") for item in normalized}
    cost_currency = next(iter(currencies)) if len(currencies) == 1 else None
    if len(currencies) != 1 or cost_currency is None or sums["cost_minor"] is None:
        sums["cost_minor"] = None
        cost_currency = None
    if not complete:
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "cost_minor",
        ):
            sums[field] = None
        cost_currency = None
    sources = {str(item["source"]) for item in normalized}
    source = next(iter(sources)) if len(sources) == 1 else "mixed"
    if source == "unavailable":
        source = "measured"
    return ProviderTelemetry(
        status="available" if complete else "partial",
        source=source,
        cost_currency=cost_currency,
        **sums,
    )


def _provider_sample(value: object) -> dict[str, object] | None:
    try:
        payload = _as_json_mapping(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, ProviderTelemetry):
        payload.setdefault("source", value.source)
    else:
        payload.setdefault("source", "measured")
    if "elapsed_seconds" in payload and "time_seconds" not in payload:
        payload["time_seconds"] = payload["elapsed_seconds"]
    if "cached_tokens" in payload and "cached_input_tokens" not in payload:
        payload["cached_input_tokens"] = payload["cached_tokens"]
    measurement_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "time_seconds",
        "cost_minor",
        "request_count",
        "retry_count",
        "usage_reported_requests",
    )
    if not any(payload.get(key) is not None for key in measurement_fields):
        return None
    return {
        key: payload.get(key)
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "time_seconds",
            "cost_minor",
            "request_count",
            "retry_count",
            "usage_reported_requests",
            "cost_currency",
            "source",
        )
    }


def _sum_optional(values: Iterable[object]) -> int | float | None:
    collected = [value for value in values if isinstance(value, (int, float))]
    return sum(collected) if collected else None


def _sum_complete(samples: Iterable[Mapping[str, object]], field: str) -> int | float | None:
    values = [sample.get(field) for sample in samples]
    if not values or not all(isinstance(value, (int, float)) for value in values):
        return None
    return sum(values)


def _memory_telemetry(
    memory: AgentMemory | None, binding: FrozenEvaluationBinding | None
) -> MemoryTelemetry:
    diagnostics = getattr(memory, "context_diagnostics", {}) if memory is not None else {}
    if not isinstance(diagnostics, Mapping):
        return MemoryTelemetry()
    values = {
        "context_items": diagnostics.get("used_items"),
        "context_tokens": diagnostics.get("used_estimated_tokens"),
        "stored_artifacts": diagnostics.get("stored_artifacts"),
        "snapshot_members": diagnostics.get("snapshot_members"),
    }
    totals = getattr(memory, "telemetry_totals", {}) if memory is not None else {}
    if isinstance(totals, Mapping):
        values.update(
            {
                "context_items": totals.get("context_items"),
                "context_tokens": totals.get("context_tokens"),
            }
        )
    values = {key: value for key, value in values.items() if isinstance(value, (int, float))}
    frozen_members = getattr(memory, "frozen_snapshot_members", None)
    if isinstance(frozen_members, int) and frozen_members >= 0:
        values["snapshot_members"] = frozen_members
    return MemoryTelemetry(status="available", **values) if values else MemoryTelemetry()


__all__ = [
    "EvaluationArtifactStore",
    "InMemoryEvaluationArtifactStore",
    "FilesystemEvaluationArtifactStore",
    "LifecycleEvent",
    "EvaluationJournal",
    "EvaluationEnvironmentFactory",
    "EvaluationModelFactory",
    "EvaluationMemoryFactory",
    "EvaluationBindingFactory",
    "EvaluationConfigFactory",
    "DefaultEvaluationMemoryFactory",
    "EvaluationRuntime",
]
