"""Bounded, opt-in observed learning around an existing memory runtime.

The bridge deliberately sits outside the memory orchestrator.  The wrapped
runtime owns the ordinary transition and outcome writes; this module only
observes the authoritative records after those writes complete and passes a
verified snapshot to descriptive observed-memory writers.  It contains no
environment or simulator policy.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import Field, JsonValue

from uptick_agent.memory.contracts import (
    ContractModel,
    DecisionMemoryContext,
    ExperienceTransition,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryPermanentError,
    MemoryValidationError,
    RunOutcome,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.settings import PatternQuerySettings
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    StoredRecord,
    StructuredMemoryStore,
    sha256_json,
    validate_namespace,
)
from uptick_agent.redaction import sanitize_json

_TRANSITION_RECORD_TYPE = "experience-transition"
_OUTCOME_RECORD_TYPE = "run-outcome"
_CHECKPOINT_RECORD_TYPE = "online-learning-checkpoint"
_CHECKPOINT_SCHEMA_VERSION = "1.0"
_TRANSITION_NAMESPACE_SUFFIX = ":transitions"
_CHECKPOINT_NAMESPACE_SUFFIX = ":checkpoints"


class MemoryRuntime(Protocol):
    """The small runner-facing surface required from the wrapped runtime."""

    async def record_transition(self, transition: ExperienceTransition) -> None: ...

    async def finalize_run(self, outcome: RunOutcome) -> None: ...

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext: ...


class ObservedWriter(Protocol):
    """A descriptive observed-memory writer (world model or metric lessons)."""

    async def record_observed(
        self,
        evidence: LessonEvidence,
        learning_cutoffs: Mapping[str, datetime],
        *,
        idempotency_key: str,
    ) -> None: ...


type ProjectorResult = Sequence[ExperienceTransition]
type Projector = Callable[
    [LessonEvidence, Mapping[str, datetime]],
    ProjectorResult | Awaitable[ProjectorResult],
]


class OnlineLearningDiagnostics(ContractModel):
    """JSON-safe operational state for the bridge.

    This is diagnostic state only.  It is never inserted into a decision
    prompt and intentionally contains no raw observations or credentials.
    """

    schema_version: str = Field(default=_CHECKPOINT_SCHEMA_VERSION)
    enabled: bool = True
    source_namespace: str
    derived_namespace: str
    pattern_settings: dict[str, JsonValue]
    every_n_transitions: int = Field(ge=1)
    active_run_id: str | None = None
    pending_transition_count: int = Field(default=0, ge=0)
    materialization_count: int = Field(default=0, ge=0)
    skipped_duplicate_count: int = Field(default=0, ge=0)
    last_snapshot_id: str | None = None
    last_snapshot_content_hash: str | None = None
    last_progress_hash: str | None = None
    last_cutoff: str | None = None
    last_error: str | None = None


class _CheckpointPayload(ContractModel):
    """Durable bridge identity and cumulative cutoff receipt."""

    schema_version: Literal[_CHECKPOINT_SCHEMA_VERSION] = _CHECKPOINT_SCHEMA_VERSION
    source_namespace: str
    derived_namespace: str
    run_id: str
    source_progress_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    source_snapshot_id: str
    source_snapshot_content_hash: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    materialized_snapshot_id: str
    materialized_snapshot_content_hash: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    transition_ids: list[str]
    cutoff: datetime
    learning_cutoffs: dict[str, datetime]
    settings: dict[str, JsonValue]
    projector_identity: str
    writer_identity: tuple[str, ...]


@dataclass(frozen=True)
class _PersistedEvidence:
    evidence: LessonEvidence
    transition_records: tuple[StoredRecord, ...]
    source_progress_hash: str


class OnlineLearningMemory:
    """Opt-in bridge that flushes observed summaries at bounded checkpoints.

    ``base`` always receives the transition or outcome first.  The bridge then
    reads records back from ``store`` and creates an immutable snapshot.  A
    projector can persist a generic derived transition prefix before writers
    consume it; the bridge does not interpret operation IDs, objectives, or
    simulator-specific fields.
    """

    def __init__(
        self,
        base: MemoryRuntime,
        store: StructuredMemoryStore,
        *,
        source_namespace: str,
        derived_namespace: str,
        writers: Sequence[ObservedWriter],
        pattern_settings: PatternQuerySettings,
        projector: Projector | None = None,
        every_n_transitions: int = 8,
    ) -> None:
        # ``StructuredMemoryStore`` is intentionally a non-runtime-checkable
        # protocol, so validate the narrow duck-typed persistence surface.
        required = ("append", "get", "list", "create_snapshot", "get_snapshot")
        if any(not callable(getattr(store, name, None)) for name in required):
            raise MemoryValidationError("online learning requires a structured memory store")
        if base is None:
            raise MemoryValidationError("online learning requires a base memory runtime")
        if isinstance(writers, (str, bytes)) or not isinstance(writers, Sequence):
            raise MemoryValidationError("online learning writers must be a sequence")
        if not writers:
            raise MemoryValidationError("online learning requires at least one observed writer")
        for writer in writers:
            if not callable(getattr(writer, "record_observed", None)):
                raise MemoryValidationError("online learning writer lacks record_observed")
        if type(every_n_transitions) is not int or every_n_transitions < 1:
            raise MemoryValidationError("every_n_transitions must be a positive integer")
        if projector is not None and not callable(projector):
            raise MemoryValidationError("online learning projector must be callable")
        if not isinstance(pattern_settings, PatternQuerySettings):
            raise MemoryValidationError("online learning requires PatternQuerySettings")

        source = validate_namespace(source_namespace)
        derived = validate_namespace(derived_namespace)
        if (
            source == derived
            or source.startswith(derived + ":")
            or derived.startswith(source + ":")
        ):
            raise MemoryValidationError(
                "online learning source and derived namespaces must be disjoint"
            )

        self._base = base
        self._store = store
        self._source_namespace = source
        self._derived_namespace = derived
        self._projection_namespace = derived + _TRANSITION_NAMESPACE_SUFFIX
        self._checkpoint_namespace = derived + _CHECKPOINT_NAMESPACE_SUFFIX
        validate_namespace(self._projection_namespace)
        validate_namespace(self._checkpoint_namespace)
        self._writers = tuple(writers)
        self._settings = PatternQuerySettings.model_validate(
            pattern_settings.model_dump(mode="json")
        )
        for writer in self._writers:
            writer_settings = getattr(writer, "settings", None)
            if writer_settings is None:
                writer_settings = getattr(writer, "_settings", None)
            if isinstance(writer_settings, PatternQuerySettings) and (
                writer_settings.model_dump(mode="json") != self._settings.model_dump(mode="json")
            ):
                raise MemoryValidationError(
                    "online learning writer settings differ from pattern_settings"
                )
        self._projector = projector
        self._projector_identity = self._identity_for_projector(projector)
        self._writer_identity = tuple(self._identity_for_writer(writer) for writer in self._writers)
        self._every_n_transitions = every_n_transitions
        self._active_run_id: str | None = None
        self._diagnostics = OnlineLearningDiagnostics(
            source_namespace=source,
            derived_namespace=derived,
            pattern_settings=self._settings.model_dump(mode="json"),
            every_n_transitions=every_n_transitions,
        )

    @property
    def online_learning_enabled(self) -> bool:
        """Whether this bridge is actively materializing observed summaries."""

        return True

    @property
    def observed_learning_enabled(self) -> bool:
        """Compatibility name for callers that expose an observed capability."""

        return True

    @property
    def diagnostics(self) -> dict[str, JsonValue]:
        """Return a detached JSON-safe diagnostics object."""

        return self._diagnostics.model_dump(mode="json")

    @property
    def online_learning_diagnostics(self) -> dict[str, JsonValue]:
        return self.diagnostics

    @property
    def pattern_settings(self) -> PatternQuerySettings:
        return self._settings.model_copy(deep=True)

    @property
    def context_diagnostics(self) -> dict[str, JsonValue]:
        """Delegate base diagnostics when available, with online state added."""

        value = getattr(self._base, "context_diagnostics", {})
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if not isinstance(value, dict):
            value = {}
        return {**value, "online_learning": self.diagnostics}

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext:
        return await self._base.build_context(request)

    async def learn_persisted_run(self, run_id: str) -> None:
        """Explicitly bootstrap from a caller-selected, already persisted prior run.

        This does not fabricate a transition or terminal outcome. Callers own
        selection of historical learning runs; a live run cannot switch its
        learning source through this bootstrap API.
        """
        if self._active_run_id is not None:
            raise MemoryValidationError("historical bootstrap requires an idle learning runtime")
        persisted = await self._persisted_source_evidence()
        current = self._current_run_transitions(persisted.transition_records, run_id)
        if not current:
            raise MemoryValidationError("historical bootstrap run has no persisted transitions")
        await self._materialize(persisted, run_id, current)

    async def record_transition(self, transition: ExperienceTransition) -> None:
        """Persist through ``base`` first, then checkpoint observed learning."""

        if not isinstance(transition, ExperienceTransition):
            raise MemoryValidationError("online learning transition is invalid")
        if self._active_run_id is None:
            self._active_run_id = transition.run_id
        elif self._active_run_id != transition.run_id:
            raise MemoryValidationError(
                "online learning runtime cannot mix active run IDs before finalization"
            )

        await self._base.record_transition(transition)
        persisted = await self._persisted_source_evidence()
        current = self._current_run_transitions(persisted.transition_records, transition.run_id)
        self._set_pending(transition.run_id, len(current))
        if len(current) % self._every_n_transitions == 0:
            await self._materialize(persisted, transition.run_id, current)

    async def finalize_run(self, outcome: RunOutcome) -> None:
        """Finalize through ``base`` first, then flush any remaining interval."""

        if not isinstance(outcome, RunOutcome):
            raise MemoryValidationError("online learning outcome is invalid")
        await self._base.finalize_run(outcome)
        if self._active_run_id is not None and self._active_run_id != outcome.run_id:
            raise MemoryValidationError(
                "online learning finalization run ID does not match active transitions"
            )

        persisted = await self._persisted_source_evidence()
        current = self._current_run_transitions(persisted.transition_records, outcome.run_id)
        if current:
            self._set_pending(outcome.run_id, len(current))
            await self._materialize(persisted, outcome.run_id, current)
        self._active_run_id = None
        self._diagnostics = self._diagnostics.model_copy(
            update={"active_run_id": None, "pending_transition_count": 0}
        )

    async def remember(self, entry: Any) -> None:
        method = getattr(self._base, "remember", None)
        if method is None:
            return None
        result = method(entry)
        if inspect.isawaitable(result):
            await result

    async def clear(self, run_id: str | None = None) -> None:
        method = getattr(self._base, "clear", None)
        if method is None:
            return None
        result = method(run_id)
        if inspect.isawaitable(result):
            await result

    async def record_trace(self, write: Any) -> Any:
        method = getattr(self._base, "record_trace", None)
        if method is None:
            return None
        result = method(write)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _materialize(
        self,
        persisted: _PersistedEvidence,
        run_id: str,
        current: tuple[StoredRecord, ...],
    ) -> None:
        cutoff = max(
            ExperienceTransition.model_validate(record.payload).occurred_at for record in current
        ).astimezone(UTC)
        cutoffs = await self._known_cutoffs()
        cutoffs[run_id] = cutoff
        progress_hash = sha256_json(
            {
                "source_records": self._progress_hash(persisted.transition_records),
                "learning_cutoffs": {
                    key: value.isoformat() for key, value in sorted(cutoffs.items())
                },
            }
        )
        checkpoint_id = self._checkpoint_id(progress_hash)
        existing = await self._store.get(
            namespace=self._checkpoint_namespace,
            record_id=checkpoint_id,
        )
        if existing is not None:
            self._validate_checkpoint(existing)
            self._diagnostics = self._diagnostics.model_copy(
                update={
                    "skipped_duplicate_count": self._diagnostics.skipped_duplicate_count + 1,
                    "last_progress_hash": progress_hash,
                    "last_cutoff": cutoff.isoformat(),
                }
            )
            return

        evidence = persisted.evidence
        if self._projector is not None:
            projected = self._projector(evidence, cutoffs)
            if inspect.isawaitable(projected):
                projected = await projected
            evidence = await self._persist_projected_evidence(projected, cutoffs)

        # Every writer receives the same immutable evidence and key.  A writer
        # may be a world summary, a metric lesson view, or another descriptive
        # module; the bridge does not privilege any one of them.
        writer_key = "online-observed:" + progress_hash
        for writer in self._writers:
            await writer.record_observed(
                evidence,
                cutoffs,
                idempotency_key=writer_key,
            )

        payload: dict[str, JsonValue] = {
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "source_namespace": self._source_namespace,
            "derived_namespace": self._derived_namespace,
            "run_id": run_id,
            "source_progress_hash": progress_hash,
            "source_snapshot_id": persisted.evidence.snapshot.snapshot_id,
            "source_snapshot_content_hash": persisted.evidence.snapshot.content_hash,
            "materialized_snapshot_id": evidence.snapshot.snapshot_id,
            "materialized_snapshot_content_hash": evidence.snapshot.content_hash,
            "transition_ids": [record.record_id for record in current],
            "cutoff": cutoff.isoformat(),
            "learning_cutoffs": {key: value.isoformat() for key, value in sorted(cutoffs.items())},
            "settings": self._settings.model_dump(mode="json"),
            "projector_identity": self._projector_identity,
            "writer_identity": list(self._writer_identity),
        }
        safe_payload = sanitize_json(payload)
        if safe_payload != payload:
            raise MemoryPermanentError("online learning checkpoint is not JSON safe")
        write = RecordWrite(
            namespace=self._checkpoint_namespace,
            record_id=checkpoint_id,
            record_type=_CHECKPOINT_RECORD_TYPE,
            payload=safe_payload,
            created_at=cutoff,
        )
        try:
            await self._store.append(
                write,
                operation="record-online-learning-checkpoint",
                idempotency_key=checkpoint_id,
            )
        except MemoryConflictError:
            actual = await self._store.get(
                namespace=self._checkpoint_namespace,
                record_id=checkpoint_id,
            )
            if actual is None or actual.payload != safe_payload:
                raise

        self._diagnostics = self._diagnostics.model_copy(
            update={
                "materialization_count": self._diagnostics.materialization_count + 1,
                "last_snapshot_id": evidence.snapshot.snapshot_id,
                "last_snapshot_content_hash": evidence.snapshot.content_hash,
                "last_progress_hash": progress_hash,
                "last_cutoff": cutoff.isoformat(),
                "last_error": None,
            }
        )

    async def _persisted_source_evidence(self) -> _PersistedEvidence:
        records = [
            StoredRecord.validate_integrity(record)
            for record in await self._store.list(namespace=self._source_namespace)
        ]
        records.sort(key=lambda record: (record.created_at, record.record_id))
        transition_records = tuple(
            record for record in records if record.record_type == _TRANSITION_RECORD_TYPE
        )
        members = [
            SnapshotMember(record_id=record.record_id, content_hash=record.content_hash)
            for record in records
        ]
        member_hash = sha256_json(
            {
                "namespace": self._source_namespace,
                "members": [member.model_dump(mode="json") for member in members],
            }
        )
        snapshot_id = "online-source-" + member_hash
        await self._store.create_snapshot(
            namespace=self._source_namespace,
            snapshot_id=snapshot_id,
            operation="freeze-online-learning-source",
            idempotency_key=snapshot_id,
        )
        snapshot = await self._store.get_snapshot(snapshot_id=snapshot_id)
        if snapshot is None:
            raise MemoryPermanentError("online learning source snapshot is missing")
        snapshot = MemorySnapshot.validate_integrity(snapshot)
        if snapshot.namespace != self._source_namespace:
            raise MemoryPermanentError("online learning source snapshot namespace changed")
        by_id = {record.record_id: record for record in records}
        evidence_records: list[StoredRecord] = []
        for member in snapshot.members:
            record = by_id.get(member.record_id)
            if record is None or record.content_hash != member.content_hash:
                raise MemoryPermanentError("online learning source snapshot member changed")
            evidence_records.append(record)
        evidence = LessonEvidence(snapshot=snapshot, records=evidence_records, runs=[])
        return _PersistedEvidence(
            evidence=evidence,
            transition_records=transition_records,
            source_progress_hash=self._progress_hash(transition_records),
        )

    async def _persist_projected_evidence(
        self,
        projected: object,
        cutoffs: Mapping[str, datetime],
    ) -> LessonEvidence:
        if isinstance(projected, (str, bytes)) or not isinstance(projected, Sequence):
            raise MemoryValidationError("online learning projector must return a sequence")
        owned: list[ExperienceTransition] = []
        for candidate in projected:
            if not isinstance(candidate, ExperienceTransition):
                raise MemoryValidationError(
                    "online learning projector returned a non-transition value"
                )
            candidate_cutoff = cutoffs.get(candidate.run_id)
            if candidate_cutoff is None:
                raise MemoryValidationError(
                    "online learning projector returned a run without a learning cutoff"
                )
            if candidate.occurred_at > candidate_cutoff:
                raise MemoryValidationError(
                    "online learning projector returned a transition after the cutoff"
                )
            owned.append(ExperienceTransition.model_validate(candidate.model_dump(mode="json")))
        by_id: dict[str, StoredRecord] = {}
        for transition in sorted(owned, key=lambda item: item.transition_id):
            write = RecordWrite(
                namespace=self._projection_namespace,
                record_id=transition.transition_id,
                record_type=_TRANSITION_RECORD_TYPE,
                payload=transition.model_dump(mode="json"),
                created_at=transition.occurred_at,
            )
            try:
                await self._store.append(
                    write,
                    operation="persist-online-learning-projection",
                    idempotency_key="projection:" + transition.transition_id,
                )
            except MemoryConflictError:
                existing = await self._store.get(
                    namespace=self._projection_namespace,
                    record_id=transition.transition_id,
                )
                if existing is None or existing.payload != write.payload:
                    raise
            existing = await self._store.get(
                namespace=self._projection_namespace,
                record_id=transition.transition_id,
            )
            if existing is None:
                raise MemoryPermanentError("online learning projection is missing after append")
            by_id[existing.record_id] = StoredRecord.validate_integrity(existing)

        records = [
            StoredRecord.validate_integrity(record)
            for record in await self._store.list(namespace=self._projection_namespace)
        ]
        records.sort(key=lambda record: (record.created_at, record.record_id))
        members = [
            SnapshotMember(record_id=record.record_id, content_hash=record.content_hash)
            for record in records
        ]
        member_hash = sha256_json(
            {
                "namespace": self._projection_namespace,
                "members": [member.model_dump(mode="json") for member in members],
            }
        )
        snapshot_id = "online-projection-" + member_hash
        await self._store.create_snapshot(
            namespace=self._projection_namespace,
            snapshot_id=snapshot_id,
            operation="freeze-online-learning-projection",
            idempotency_key=snapshot_id,
        )
        snapshot = await self._store.get_snapshot(snapshot_id=snapshot_id)
        if snapshot is None:
            raise MemoryPermanentError("online learning projection snapshot is missing")
        snapshot = MemorySnapshot.validate_integrity(snapshot)
        if snapshot.namespace != self._projection_namespace:
            raise MemoryPermanentError("online learning projection namespace changed")
        return LessonEvidence(snapshot=snapshot, records=records, runs=[])

    @staticmethod
    def _current_run_transitions(
        records: Sequence[StoredRecord], run_id: str
    ) -> tuple[StoredRecord, ...]:
        current: list[StoredRecord] = []
        for record in records:
            if record.record_type != _TRANSITION_RECORD_TYPE:
                continue
            transition = ExperienceTransition.model_validate(record.payload)
            if transition.run_id == run_id:
                current.append(record)
        return tuple(sorted(current, key=lambda record: (record.created_at, record.record_id)))

    @staticmethod
    def _progress_hash(records: Sequence[StoredRecord]) -> str:
        return sha256_json(
            [
                {"record_id": record.record_id, "content_hash": record.content_hash}
                for record in sorted(records, key=lambda item: item.record_id)
            ]
        )

    def _checkpoint_id(self, progress_hash: str) -> str:
        return "online-checkpoint-" + progress_hash

    async def _known_cutoffs(self) -> dict[str, datetime]:
        """Recover trusted cumulative cutoffs from completed bridge receipts."""

        cutoffs: dict[str, datetime] = {}
        records = await self._store.list(namespace=self._checkpoint_namespace)
        for record in records:
            current = StoredRecord.validate_integrity(record)
            if current.record_type != _CHECKPOINT_RECORD_TYPE:
                raise MemoryPermanentError(
                    "online learning checkpoint namespace contains unknown data"
                )
            checkpoint = self._validate_checkpoint(current)
            for run_id, cutoff in checkpoint.learning_cutoffs.items():
                prior = cutoffs.get(run_id)
                if prior is None or cutoff > prior:
                    cutoffs[run_id] = cutoff.astimezone(UTC)
        return dict(sorted(cutoffs.items()))

    def _validate_checkpoint(self, record: StoredRecord) -> _CheckpointPayload:
        try:
            checkpoint = _CheckpointPayload.model_validate(record.payload)
        except (TypeError, ValueError) as error:
            raise MemoryPermanentError("stored online learning checkpoint is invalid") from error
        if record.namespace != self._checkpoint_namespace:
            raise MemoryPermanentError("stored online learning checkpoint namespace is invalid")
        if record.record_id != self._checkpoint_id(checkpoint.source_progress_hash):
            raise MemoryPermanentError("stored online learning checkpoint ID is invalid")
        if checkpoint.source_namespace != self._source_namespace:
            raise MemoryPermanentError("stored online learning checkpoint source differs")
        if checkpoint.derived_namespace != self._derived_namespace:
            raise MemoryPermanentError(
                "stored online learning checkpoint derived namespace differs"
            )
        if checkpoint.settings != self._settings.model_dump(mode="json"):
            raise MemoryConflictError(
                "online learning pattern settings differ from persisted checkpoints"
            )
        if checkpoint.projector_identity != self._projector_identity:
            raise MemoryConflictError(
                "online learning projector differs from persisted checkpoints"
            )
        if checkpoint.writer_identity != self._writer_identity:
            raise MemoryConflictError("online learning writers differ from persisted checkpoints")
        if any(value.utcoffset() is None for value in checkpoint.learning_cutoffs.values()):
            raise MemoryPermanentError("stored online learning cutoff lacks timezone")
        return checkpoint

    @staticmethod
    def _identity_for_projector(projector: Projector | None) -> str:
        if projector is None:
            return "none"
        module = getattr(projector, "__module__", type(projector).__module__)
        qualname = getattr(projector, "__qualname__", type(projector).__qualname__)
        return f"{module}:{qualname}"

    @staticmethod
    def _identity_for_writer(writer: ObservedWriter) -> str:
        identity: dict[str, object] = {
            "type": f"{type(writer).__module__}:{type(writer).__qualname__}"
        }
        for name in ("settings", "_settings", "metric", "_metric"):
            value = getattr(writer, name, None)
            if hasattr(value, "model_dump"):
                identity[name] = value.model_dump(mode="json")
        return identity["type"] + ":" + sha256_json(identity)

    def _set_pending(self, run_id: str, count: int) -> None:
        self._diagnostics = self._diagnostics.model_copy(
            update={
                "active_run_id": run_id,
                "pending_transition_count": count,
            }
        )


# The runtime alias keeps the bridge discoverable to callers that name all
# runner-facing facades ``*Runtime`` while retaining the more precise class
# name for new code.
OnlineLearningRuntime = OnlineLearningMemory


__all__ = [
    "MemoryRuntime",
    "ObservedWriter",
    "OnlineLearningDiagnostics",
    "OnlineLearningMemory",
    "OnlineLearningRuntime",
    "Projector",
]
