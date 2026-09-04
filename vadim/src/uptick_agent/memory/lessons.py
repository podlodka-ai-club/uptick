"""Evidence-backed deterministic lesson memory."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC
from typing import Protocol, runtime_checkable

from pydantic import Field, ValidationError

from uptick_agent.memory.config import LessonSettings
from uptick_agent.memory.contracts import (
    ContextItem,
    ContractModel,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    MemoryValidationError,
    RunOutcome,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.lesson_contracts import (
    LessonCandidate,
    LessonEvidence,
    ValidatedLesson,
    snapshot_input_hash,
)
from uptick_agent.memory.stores.contracts import (
    RecordWrite,
    StoredRecord,
    StructuredMemoryStore,
    canonical_json,
    sha256_json,
    validate_namespace,
)
from uptick_agent.redaction import sanitize_json

LESSONS_MODULE_ID = "lessons"
LESSONS_MODULE_VERSION = "1.0"
LESSON_BATCH_RECORD_TYPE = "lesson-batch"
LESSON_BATCH_SCHEMA_VERSION = "1.0"
_RETENTION_POLICY_REF = "simulator-audit-retention-v1@1.0"
_WORD = re.compile(r"[\w-]+", re.UNICODE)


@runtime_checkable
class LessonEvidenceSource(Protocol):
    """The sole source boundary used by LessonsMemory."""

    async def capture(
        self, outcome: RunOutcome, *, idempotency_key: str
    ) -> LessonEvidence | None: ...


class LessonBatch(ContractModel):
    """One immutable finalization input and its complete validated output."""

    schema_version: str = Field(
        default=LESSON_BATCH_SCHEMA_VERSION, pattern=r"^[1-9][0-9]*\.[0-9]+$"
    )
    retention_policy_ref: str = _RETENTION_POLICY_REF
    settings: LessonSettings
    settings_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    outcome: RunOutcome
    evidence: LessonEvidence
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    lessons: list[ValidatedLesson]


def _tokens(value: object) -> set[str]:
    return {token.casefold() for token in _WORD.findall(canonical_json(value)) if len(token) > 1}


def _batch_id(run_id: str) -> str:
    return "lesson-batch-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _owned_settings(settings: LessonSettings) -> LessonSettings:
    try:
        return LessonSettings.model_validate(settings.model_dump(mode="json"))
    except (AttributeError, TypeError, ValueError, ValidationError) as error:
        raise MemoryValidationError("lesson settings are invalid") from error


def _owned_outcome(outcome: RunOutcome) -> RunOutcome:
    try:
        serialized = outcome.model_dump(mode="json")
        safe = sanitize_json(serialized)
        if not isinstance(safe, dict) or safe.get("run_id") != serialized.get("run_id"):
            raise MemoryValidationError("lesson outcome run_id contains credential-shaped content")
        owned = RunOutcome.model_validate(safe)
        if owned.finished_at.utcoffset() is None:
            raise MemoryValidationError("lesson outcome timestamp must include a timezone")
        return owned.model_copy(update={"finished_at": owned.finished_at.astimezone(UTC)})
    except (AttributeError, TypeError, ValueError, ValidationError) as error:
        raise MemoryValidationError("lesson outcome is invalid") from error


def _owned_evidence(evidence: LessonEvidence) -> LessonEvidence:
    try:
        return LessonEvidence.model_validate(evidence.model_dump(mode="json"))
    except (AttributeError, TypeError, ValueError, ValidationError) as error:
        raise MemoryPermanentError("lesson evidence is invalid") from error


def _owned_record(record: StoredRecord) -> StoredRecord:
    try:
        return StoredRecord.validate_integrity(record)
    except (TypeError, ValueError, ValidationError) as error:
        raise MemoryPermanentError("lesson evidence record is invalid") from error


def _snapshot_rank(evidence: LessonEvidence) -> tuple[int, tuple[str, ...]]:
    """Rank by immutable nested membership, never by caller timestamps."""

    return (
        len(evidence.snapshot.members),
        tuple(sorted(
            f"{member.record_id}:{member.content_hash}"
            for member in evidence.snapshot.members
        )),
    )


class LessonsMemory:
    """Persist validated lessons and expose only active derived-untrusted data."""

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        namespace: str,
        source: LessonEvidenceSource,
        settings: LessonSettings,
        module_version: str = LESSONS_MODULE_VERSION,
    ) -> None:
        self._store = store
        self._namespace = validate_namespace(namespace)
        if not isinstance(source, LessonEvidenceSource):
            raise MemoryValidationError("lessons requires a LessonEvidenceSource")
        self._source = source
        self._settings = _owned_settings(settings)
        if not module_version or len(module_version) > 64:
            raise MemoryValidationError("lessons module_version must contain 1-64 characters")
        self._module_version = module_version

    @property
    def settings(self) -> LessonSettings:
        return self._settings.model_copy(deep=True)

    async def finalize(self, outcome: RunOutcome, *, idempotency_key: str) -> None:
        owned_outcome = _owned_outcome(outcome)
        record_id = _batch_id(owned_outcome.run_id)
        existing = await self._store.get(namespace=self._namespace, record_id=record_id)
        if existing is not None:
            batch = self._read_batch(existing)
            await self._verify_evidence(batch.evidence)
            if batch.outcome.model_dump(mode="json") != owned_outcome.model_dump(mode="json"):
                raise MemoryConflictError("lesson finalization replay has conflicting outcome")
            return

        evidence = await self._source.capture(owned_outcome, idempotency_key=idempotency_key)
        if evidence is None:
            return
        evidence = _owned_evidence(evidence)
        await self._verify_evidence(evidence)
        from uptick_agent.memory.candidate_validation import (
            extract_candidates,
            validate_candidate,
        )

        candidates = extract_candidates(evidence, self._settings)
        if not isinstance(candidates, list) or not all(
            isinstance(candidate, LessonCandidate) for candidate in candidates
        ):
            raise MemoryPermanentError("lesson extractor returned invalid candidates")
        merged: dict[str, LessonCandidate] = {}
        for candidate in candidates:
            owned = LessonCandidate.model_validate(candidate.model_dump(mode="json"))
            merged[owned.semantic_hash] = owned
        validated: list[ValidatedLesson] = []
        for semantic_hash in sorted(merged):
            lesson = validate_candidate(merged[semantic_hash], evidence, self._settings)
            if not isinstance(lesson, ValidatedLesson):
                raise MemoryPermanentError("lesson validator returned invalid result")
            validated.append(ValidatedLesson.model_validate(lesson.model_dump(mode="json")))
        validated.sort(key=lambda lesson: lesson.candidate.semantic_hash)
        settings = _owned_settings(self._settings)
        batch = LessonBatch(
            settings=settings,
            settings_hash=sha256_json(settings.model_dump(mode="json")),
            outcome=owned_outcome,
            evidence=evidence,
            input_hash=snapshot_input_hash(evidence),
            lessons=validated,
        )
        write = RecordWrite(
            namespace=self._namespace,
            record_id=record_id,
            record_type=LESSON_BATCH_RECORD_TYPE,
            payload=batch.model_dump(mode="json"),
            created_at=owned_outcome.finished_at.astimezone(UTC),
        )
        try:
            await self._store.append(
                write,
                operation="finalize-lessons",
                idempotency_key=idempotency_key,
            )
        except MemoryConflictError:
            replay = await self._store.get(namespace=self._namespace, record_id=record_id)
            if replay is None:
                raise
            persisted = self._read_batch(replay)
            if persisted.outcome.model_dump(mode="json") != owned_outcome.model_dump(mode="json"):
                raise
        canonical = await self._store.get(namespace=self._namespace, record_id=record_id)
        if canonical is None:
            raise MemoryPermanentError("lesson batch is missing after append")
        self._read_batch(canonical)

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        records = await self._store.list(namespace=self._namespace)
        batches: list[tuple[tuple[int, tuple[str, ...]], LessonBatch]] = []
        for record in records:
            if record.record_type != LESSON_BATCH_RECORD_TYPE:
                raise MemoryPermanentError(
                    f"lessons namespace contains unknown record type {record.record_type!r}"
                )
            batch = self._read_batch(record)
            await self._verify_evidence(batch.evidence)
            batches.append((_snapshot_rank(batch.evidence), batch))
        if not batches:
            return MemoryContribution(
                module_id=LESSONS_MODULE_ID, module_version=self._module_version
            )
        self._require_nested_snapshots(batches)
        batches.sort(key=lambda item: item[0])
        batch = batches[-1][1]
        from uptick_agent.memory.candidate_validation import validate_candidate

        refreshed: list[ValidatedLesson] = []
        for lesson in batch.lessons:
            current = validate_candidate(lesson.candidate, batch.evidence, batch.settings)
            if not isinstance(current, ValidatedLesson):
                raise MemoryPermanentError("stored lesson validation result is invalid")
            if current.manifest.model_dump(mode="json") != lesson.manifest.model_dump(mode="json"):
                raise MemoryPermanentError("stored lesson validation manifest changed")
            if current.status != lesson.status:
                raise MemoryPermanentError("stored lesson status changed")
            refreshed.append(current)
        excluded = {request.run_id}
        physical = request.context.get("physical_run_id")
        if isinstance(physical, str):
            excluded.add(physical)
        query_tokens = _tokens(request.query)
        ranked: list[tuple[float, ValidatedLesson, int]] = []
        for lesson in refreshed:
            if lesson.status != "active":
                continue
            support_ids = set(lesson.manifest.support_run_ids) | set(
                lesson.manifest.support_logical_run_ids
            )
            if excluded & support_ids:
                continue
            lesson_tokens = _tokens(lesson.candidate.model_dump(mode="json"))
            overlap = len(query_tokens & lesson_tokens)
            if query_tokens and overlap == 0:
                continue
            denominator = max(len(query_tokens) * len(lesson_tokens), 1)
            ranked.append((overlap / math.sqrt(denominator), lesson, overlap))
        ranked.sort(key=lambda item: (-item[0], item[1].candidate.semantic_hash))
        if request.max_items is not None:
            ranked = ranked[: request.max_items]
        return MemoryContribution(
            module_id=LESSONS_MODULE_ID,
            module_version=self._module_version,
            items=[self._item(lesson, score, overlap) for score, lesson, overlap in ranked],
        )

    async def _verify_evidence(self, evidence: LessonEvidence) -> None:
        members = evidence.snapshot.members
        if evidence.records and evidence.snapshot.namespace != evidence.records[0].namespace:
            raise MemoryPermanentError("lesson evidence snapshot namespace mismatch")
        authoritative_snapshot = await self._store.get_snapshot(
            snapshot_id=evidence.snapshot.snapshot_id
        )
        if authoritative_snapshot is None:
            raise MemoryPermanentError("lesson evidence snapshot is missing")
        if authoritative_snapshot.model_dump(mode="json") != evidence.snapshot.model_dump(
            mode="json"
        ):
            raise MemoryPermanentError("lesson evidence snapshot changed after capture")
        if [member.record_id for member in members] != [
            record.record_id for record in evidence.records
        ]:
            raise MemoryPermanentError("lesson evidence records do not match snapshot membership")
        for member, embedded in zip(members, evidence.records, strict=True):
            record = _owned_record(embedded)
            if record.content_hash != member.content_hash:
                raise MemoryPermanentError("lesson evidence member hash mismatch")
            current = await self._store.get(namespace=record.namespace, record_id=record.record_id)
            if current is None or current.content_hash != record.content_hash:
                raise MemoryPermanentError("lesson evidence record changed after capture")

    def _read_batch(self, record: StoredRecord) -> LessonBatch:
        try:
            owned = StoredRecord.validate_integrity(record)
            if owned.record_type != LESSON_BATCH_RECORD_TYPE:
                raise MemoryPermanentError("stored lesson batch has an invalid record type")
            batch = LessonBatch.model_validate(owned.payload)
            if batch.retention_policy_ref != _RETENTION_POLICY_REF:
                raise MemoryPermanentError("stored lesson retention policy is unsupported")
            if owned.namespace != self._namespace:
                raise MemoryPermanentError("stored lesson batch namespace mismatch")
            if owned.record_id != _batch_id(batch.outcome.run_id):
                raise MemoryPermanentError("stored lesson batch ID mismatch")
            if owned.created_at != batch.outcome.finished_at:
                raise MemoryPermanentError("stored lesson batch timestamp mismatch")
            if batch.settings_hash != sha256_json(batch.settings.model_dump(mode="json")):
                raise MemoryPermanentError("stored lesson settings hash mismatch")
            if batch.input_hash != snapshot_input_hash(batch.evidence):
                raise MemoryPermanentError("stored lesson input hash mismatch")
            if batch.settings != self._settings:
                raise MemoryPermanentError("stored lesson settings do not match runtime")
            from uptick_agent.memory.candidate_validation import (
                extract_candidates,
                validate_candidate,
            )

            expected = extract_candidates(batch.evidence, batch.settings)
            regenerated = [
                validate_candidate(candidate, batch.evidence, batch.settings)
                for candidate in expected
            ]
            regenerated.sort(key=lambda lesson: lesson.candidate.semantic_hash)
            if [lesson.model_dump(mode="json") for lesson in regenerated] != [
                lesson.model_dump(mode="json") for lesson in batch.lessons
            ]:
                raise MemoryPermanentError(
                    "stored lesson batch does not match deterministic regeneration"
                )
            return batch
        except MemoryPermanentError:
            raise
        except MemoryValidationError as error:
            raise MemoryPermanentError("stored lesson batch validation failed") from error
        except (TypeError, ValueError, ValidationError) as error:
            raise MemoryPermanentError("stored lesson batch is invalid") from error

    @staticmethod
    def _require_nested_snapshots(
        batches: list[tuple[tuple[int, tuple[str, ...]], LessonBatch]],
    ) -> None:
        """Reject incomparable branches before selecting a newest batch."""

        for _, batch in batches:
            snapshot = batch.evidence.snapshot
            members = {
                (member.record_id, member.content_hash) for member in snapshot.members
            }
            for _, other in batches:
                if other is batch:
                    continue
                if other.evidence.snapshot.namespace != snapshot.namespace:
                    raise MemoryPermanentError("lesson batches use different source namespaces")
                other_members = {
                    (member.record_id, member.content_hash)
                    for member in other.evidence.snapshot.members
                }
                if not (members <= other_members or other_members <= members):
                    raise MemoryPermanentError("lesson snapshots are not nested")

    def _item(self, lesson: ValidatedLesson, score: float, overlap: int) -> ContextItem:
        return ContextItem(
            envelope=UntrustedMemoryEnvelope(
                item_id=lesson.candidate.lesson_id,
                artefact_type="lesson",
                origin_module=LESSONS_MODULE_ID,
                origin_version=self._module_version,
                trust_classification="derived_untrusted",
                provenance=lesson.provenance,
                item={
                    "lesson": lesson.candidate.model_dump(mode="json"),
                    "estimated_utility": lesson.estimated_utility,
                    "confidence": lesson.confidence,
                    "status": lesson.status,
                },
            ),
            score=score,
            selection_reason=f"lessons lexical overlap={overlap}",
            estimated_tokens=0,
        )


__all__ = [
    "LESSONS_MODULE_ID",
    "LESSONS_MODULE_VERSION",
    "LESSON_BATCH_RECORD_TYPE",
    "LessonBatch",
    "LessonEvidenceSource",
    "LessonsMemory",
]
