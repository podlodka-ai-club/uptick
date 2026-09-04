"""Capture immutable episodic evidence for deterministic lesson validation.

This adapter is intentionally persistence-facing.  It does not use
``EpisodicMemory`` because lesson validation must see the authoritative
snapshot and the exact records that were members of that snapshot.  Run
classification and environment/scenario content hashes are supplied by the
experiment owner as immutable declarations; they are never inferred from
episodic records or a live simulator.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryConflictError,
    MemoryPermanentError,
    MemoryValidationError,
    RunOutcome,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence, LessonRunDeclaration
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    StoredRecord,
    StructuredMemoryStore,
    canonical_json,
    sha256_json,
    validate_identifier,
    validate_namespace,
)
from uptick_agent.redaction import sanitize_json

_TRANSITION_RECORD_TYPE = "experience-transition"
_OUTCOME_RECORD_TYPE = "run-outcome"
_DECLARATION_RECORD_TYPE = "lesson-run-declaration"
_CONTEXT_RECORD_TYPE = "lesson-capture-context"
_DECLARATION_OPERATION = "declare-lesson-run"
_CONTEXT_OPERATION = "freeze-lesson-context"
_SNAPSHOT_OPERATION = "capture-lesson-evidence"


def _owned_declaration(value: object) -> LessonRunDeclaration:
    """Round-trip a declaration so caller-owned mutable state cannot leak in."""

    if not isinstance(value, LessonRunDeclaration):
        raise MemoryValidationError("run_declarations must contain LessonRunDeclaration values")
    try:
        owned = LessonRunDeclaration.model_validate(
            value.model_dump(mode="python", round_trip=True, warnings="error")
        )
        serialized = owned.model_dump(mode="json")
        safe = sanitize_json(serialized)
        if safe != serialized:
            raise MemoryValidationError(
                "run declaration contains unredacted credential-shaped content"
            )
        return LessonRunDeclaration.model_validate(safe)
    except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
        raise MemoryValidationError("run declaration is invalid") from error


def _owned_declaration_payload(value: object) -> LessonRunDeclaration:
    """Decode a declaration stored inside the immutable capture context."""

    if not isinstance(value, dict):
        raise MemoryValidationError("stored run declaration must be an object")
    try:
        declaration = LessonRunDeclaration.model_validate(value)
    except (TypeError, ValueError, ValidationError) as error:
        raise MemoryValidationError("stored run declaration is invalid") from error
    return _owned_declaration(declaration)


def _owned_outcome(value: object) -> RunOutcome:
    """Round-trip the final outcome before using it to select evidence."""

    if not isinstance(value, RunOutcome):
        raise MemoryValidationError("outcome must be a RunOutcome")
    try:
        owned = RunOutcome.model_validate(
            value.model_dump(mode="python", round_trip=True, warnings="error")
        )
        if owned.finished_at.utcoffset() is None:
            raise MemoryValidationError("outcome timestamp must include a timezone")
        serialized = owned.model_dump(mode="json")
        safe = sanitize_json(serialized)
        if not isinstance(safe, dict) or safe.get("run_id") != serialized["run_id"]:
            raise MemoryValidationError("outcome ID contains credential-shaped content")
        redacted = RunOutcome.model_validate(safe)
        return redacted.model_copy(update={"finished_at": redacted.finished_at.astimezone(UTC)})
    except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
        raise MemoryValidationError("outcome is invalid") from error


def _owned_record(value: object) -> StoredRecord:
    """Round-trip and integrity-check a record returned by a store."""

    try:
        return StoredRecord.validate_integrity(value)
    except (TypeError, ValueError, ValidationError) as error:
        raise MemoryPermanentError("episodic source returned an invalid record") from error


def _owned_snapshot(value: object) -> MemorySnapshot:
    """Round-trip and integrity-check a snapshot returned by a store."""

    try:
        return MemorySnapshot.validate_integrity(value)
    except (TypeError, ValueError, ValidationError) as error:
        raise MemoryPermanentError("episodic source returned an invalid snapshot") from error


class StoredEpisodicLessonSource:
    """Capture one immutable, complete episodic input bundle for lessons.

    The source writes declarations to a separate namespace before freezing the
    episodic namespace.  A declaration record is addressed by the stable run
    id and can never be replaced.  Snapshot and member reads always come from
    the store after the write; append and snapshot receipts are treated only as
    operation acknowledgements and are never used as authoritative data.
    """

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        episodic_namespace: str,
        declaration_namespace: str,
        run_declarations: Sequence[LessonRunDeclaration],
    ) -> None:
        self._store = store
        self._episodic_namespace = validate_namespace(episodic_namespace)
        self._declaration_namespace = validate_namespace(declaration_namespace)
        if self._episodic_namespace == self._declaration_namespace:
            raise MemoryValidationError(
                "episodic_namespace and declaration_namespace must be disjoint"
            )
        if isinstance(run_declarations, (str, bytes)) or not isinstance(
            run_declarations, Sequence
        ):
            raise MemoryValidationError("run_declarations must be a sequence")
        owned = [_owned_declaration(value) for value in run_declarations]
        if len({declaration.run_id for declaration in owned}) != len(owned):
            raise MemoryValidationError("run declarations must have unique run_id values")
        # Stable ordering makes the complete input bundle reproducible even if
        # the caller assembled declarations in a different order.
        owned.sort(
            key=lambda declaration: (
                declaration.logical_run_id,
                declaration.attempt_index,
                declaration.run_id,
            )
        )
        self._run_declarations = tuple(owned)

    async def capture(
        self,
        outcome: RunOutcome,
        *,
        idempotency_key: str,
    ) -> LessonEvidence | None:
        """Capture evidence for a declared learning run.

        Every declared learning finalization is captured so the validator can
        revalidate prior lessons against failed, interrupted, excluded, retry,
        and otherwise ineligible runs as counter-evidence.  Missing
        declarations and frozen-evaluation finalizations return before any
        store read or write.  The declaration remains the sole source of
        eligibility; a successful outcome never synthesizes eligibility.
        """

        owned_outcome = _owned_outcome(outcome)
        try:
            validate_identifier(
                idempotency_key,
                name="idempotency_key",
                max_length=256,
            )
        except MemoryValidationError:
            raise

        # This branch intentionally precedes declaration persistence and all
        # evidence reads.  Frozen evaluation is held out of both support and
        # counter-evidence, while learning failures remain observable to the
        # validator as possible counter-evidence.
        current = next(
            (
                declaration
                for declaration in self._run_declarations
                if declaration.run_id == owned_outcome.run_id
            ),
            None,
        )
        if (
            current is None
            or current.phase == "frozen_evaluation"
        ):
            return None

        snapshot_id = self._snapshot_id(
            owned_outcome.run_id,
            episodic_namespace=self._episodic_namespace,
            declaration_namespace=self._declaration_namespace,
        )
        existing_snapshot = await self._read_snapshot(snapshot_id)
        existing_context = await self._read_capture_context(snapshot_id, owned_outcome)
        if existing_context is None:
            if existing_snapshot is not None:
                raise MemoryPermanentError(
                    "lesson capture context is missing for immutable snapshot"
                )
            frozen_runs = await self._merge_prior_declarations()
            await self._persist_declarations(
                frozen_runs,
                idempotency_key=idempotency_key,
            )
            # Freeze the intent before creating the snapshot.  If the process
            # stops at either boundary, replay can safely resume from this
            # immutable declaration/outcome record.
            await self._persist_capture_context(
                snapshot_id,
                owned_outcome,
                frozen_runs,
                idempotency_key,
            )
            snapshot = await self._ensure_snapshot(
                snapshot_id=snapshot_id,
                idempotency_key=idempotency_key,
            )
        else:
            frozen_runs = existing_context
            self._validate_supplied_declarations(frozen_runs)
            await self._verify_declarations(frozen_runs)
            if existing_snapshot is None:
                snapshot = await self._ensure_snapshot(
                    snapshot_id=snapshot_id,
                    idempotency_key=idempotency_key,
                )
            else:
                snapshot = existing_snapshot
        records = await self._read_members(snapshot)
        reread = await self._read_snapshot(snapshot_id)
        if reread is None:
            raise MemoryPermanentError("episodic snapshot disappeared while reading its members")
        if reread.model_dump(mode="json") != snapshot.model_dump(mode="json"):
            raise MemoryPermanentError("episodic snapshot changed while reading its members")

        self._validate_records(records, owned_outcome)
        return LessonEvidence(
            snapshot=snapshot,
            records=records,
            runs=frozen_runs,
        )

    async def _persist_declarations(
        self,
        declarations: Sequence[LessonRunDeclaration],
        *,
        idempotency_key: str,
    ) -> None:
        for declaration in declarations:
            record_id = self._declaration_record_id(declaration.run_id)
            existing = await self._store.get(
                namespace=self._declaration_namespace,
                record_id=record_id,
            )
            if existing is not None:
                self._validate_declaration_record(existing, declaration, record_id=record_id)
                continue

            write = RecordWrite(
                namespace=self._declaration_namespace,
                record_id=record_id,
                record_type=_DECLARATION_RECORD_TYPE,
                payload=declaration.model_dump(mode="json"),
            )
            operation_key = self._operation_key(
                "declaration",
                idempotency_key,
                declaration.run_id,
            )
            try:
                # The returned receipt is deliberately ignored.  The next
                # authoritative read verifies the record and its hash.
                await self._store.append(
                    write,
                    operation=_DECLARATION_OPERATION,
                    idempotency_key=operation_key,
                )
            except MemoryConflictError:
                # A concurrent writer may have installed the immutable record
                # between our get and append.  Accept it only after reading and
                # validating the canonical stored value.
                existing = await self._store.get(
                    namespace=self._declaration_namespace,
                    record_id=record_id,
                )
                if existing is None:
                    raise
            else:
                existing = await self._store.get(
                    namespace=self._declaration_namespace,
                    record_id=record_id,
                )
            if existing is None:
                raise MemoryPermanentError("run declaration is missing after append")
            self._validate_declaration_record(existing, declaration, record_id=record_id)

    def _validate_declaration_record(
        self,
        value: object,
        declaration: LessonRunDeclaration,
        *,
        record_id: str,
    ) -> None:
        record = _owned_record(value)
        expected_payload = declaration.model_dump(mode="json")
        if record.namespace != self._declaration_namespace:
            raise MemoryPermanentError("run declaration namespace mismatch")
        if record.record_id != record_id:
            raise MemoryPermanentError("run declaration record id mismatch")
        if record.record_type != _DECLARATION_RECORD_TYPE:
            raise MemoryConflictError("run declaration record type conflicts")
        if record.payload != expected_payload:
            raise MemoryConflictError("run declaration is immutable and conflicts")

    @staticmethod
    def _owned_runs(
        declarations: Sequence[LessonRunDeclaration],
    ) -> list[LessonRunDeclaration]:
        return [_owned_declaration(declaration) for declaration in declarations]

    async def _merge_prior_declarations(self) -> list[LessonRunDeclaration]:
        """Merge authoritative prior declarations before freezing a new input."""

        prior: dict[str, LessonRunDeclaration] = {}
        for value in await self._store.list(namespace=self._declaration_namespace):
            record = _owned_record(value)
            if record.record_type != _DECLARATION_RECORD_TYPE:
                continue
            declaration = self._declaration_from_record(record)
            previous = prior.get(declaration.run_id)
            if previous is not None and previous.model_dump(mode="json") != declaration.model_dump(
                mode="json"
            ):
                raise MemoryConflictError("stored run declarations conflict")
            prior[declaration.run_id] = declaration
        for declaration in self._run_declarations:
            previous = prior.get(declaration.run_id)
            if previous is not None:
                if previous.model_dump(mode="json") != declaration.model_dump(mode="json"):
                    raise MemoryConflictError("run declaration is immutable and conflicts")
            else:
                prior[declaration.run_id] = declaration
        merged = list(prior.values())
        merged.sort(
            key=lambda declaration: (
                declaration.logical_run_id,
                declaration.attempt_index,
                declaration.run_id,
            )
        )
        return self._owned_runs(merged)

    async def _verify_declarations(
        self,
        declarations: Sequence[LessonRunDeclaration],
    ) -> None:
        for declaration in declarations:
            record_id = self._declaration_record_id(declaration.run_id)
            record = await self._store.get(
                namespace=self._declaration_namespace,
                record_id=record_id,
            )
            if record is None:
                raise MemoryPermanentError("frozen run declaration is missing")
            self._validate_declaration_record(record, declaration, record_id=record_id)

    @staticmethod
    def _declaration_from_record(record: StoredRecord) -> LessonRunDeclaration:
        if record.record_id != StoredEpisodicLessonSource._declaration_record_id(
            record.payload.get("run_id")
        ):
            raise MemoryPermanentError("stored run declaration record id mismatch")
        try:
            return _owned_declaration_payload(record.payload)
        except (MemoryValidationError, TypeError, ValueError) as error:
            raise MemoryPermanentError("stored run declaration is invalid") from error

    async def _persist_capture_context(
        self,
        snapshot_id: str,
        outcome: RunOutcome,
        runs: Sequence[LessonRunDeclaration],
        idempotency_key: str,
    ) -> None:
        record_id = self._context_record_id(snapshot_id)
        expected_payload = self._context_payload(snapshot_id, outcome, runs)
        existing = await self._store.get(
            namespace=self._declaration_namespace,
            record_id=record_id,
        )
        if existing is None:
            try:
                # The context freezes the run metadata alongside the snapshot;
                # later declarations cannot change a replayed input bundle.
                await self._store.append(
                    RecordWrite(
                        namespace=self._declaration_namespace,
                        record_id=record_id,
                        record_type=_CONTEXT_RECORD_TYPE,
                        payload=expected_payload,
                    ),
                    operation=_CONTEXT_OPERATION,
                    idempotency_key=self._operation_key(
                        "context", idempotency_key, snapshot_id
                    ),
                )
            except MemoryConflictError:
                existing = await self._store.get(
                    namespace=self._declaration_namespace,
                    record_id=record_id,
                )
            else:
                existing = await self._store.get(
                    namespace=self._declaration_namespace,
                    record_id=record_id,
                )
        if existing is None:
            raise MemoryPermanentError("lesson capture context is missing after append")
        self._validate_capture_context_record(
            existing,
            snapshot_id=snapshot_id,
            outcome=outcome,
            expected_runs=runs,
        )

    async def _read_capture_context(
        self,
        snapshot_id: str,
        outcome: RunOutcome,
    ) -> list[LessonRunDeclaration] | None:
        record = await self._store.get(
            namespace=self._declaration_namespace,
            record_id=self._context_record_id(snapshot_id),
        )
        if record is None:
            return None
        return self._validate_capture_context_record(
            record,
            snapshot_id=snapshot_id,
            outcome=outcome,
            expected_runs=None,
        )

    def _validate_capture_context_record(
        self,
        value: object,
        *,
        snapshot_id: str,
        outcome: RunOutcome,
        expected_runs: Sequence[LessonRunDeclaration] | None,
    ) -> list[LessonRunDeclaration]:
        record = _owned_record(value)
        if record.namespace != self._declaration_namespace:
            raise MemoryPermanentError("lesson capture context namespace mismatch")
        record_id = self._context_record_id(snapshot_id)
        if record.record_id != record_id:
            raise MemoryPermanentError("lesson capture context record id mismatch")
        if record.record_type != _CONTEXT_RECORD_TYPE:
            raise MemoryConflictError("lesson capture context record type conflicts")
        payload = record.payload
        expected_keys = {
            "snapshot_id",
            "outcome_run_id",
            "outcome_hash",
            "runs",
        }
        if set(payload) != expected_keys:
            raise MemoryPermanentError("lesson capture context payload shape is invalid")
        if payload["snapshot_id"] != snapshot_id:
            raise MemoryPermanentError("lesson capture context snapshot id mismatch")
        if payload["outcome_run_id"] != outcome.run_id:
            raise MemoryPermanentError("lesson capture context outcome id mismatch")
        if payload["outcome_hash"] != self._outcome_hash(outcome):
            raise MemoryConflictError("lesson capture context outcome conflicts")
        raw_runs = payload["runs"]
        if not isinstance(raw_runs, list):
            raise MemoryPermanentError("lesson capture context runs are invalid")
        try:
            runs = [_owned_declaration_payload(item) for item in raw_runs]
        except (MemoryValidationError, TypeError, ValueError) as error:
            raise MemoryPermanentError("lesson capture context runs are invalid") from error
        if len({run.run_id for run in runs}) != len(runs):
            raise MemoryPermanentError("lesson capture context runs are duplicated")
        if expected_runs is not None:
            expected = self._owned_runs(expected_runs)
            if [run.model_dump(mode="json") for run in runs] != [
                run.model_dump(mode="json") for run in expected
            ]:
                raise MemoryConflictError("lesson capture context is immutable and conflicts")
        return runs

    def _validate_supplied_declarations(
        self,
        frozen_runs: Sequence[LessonRunDeclaration],
    ) -> None:
        frozen_by_id = {run.run_id: run for run in frozen_runs}
        for supplied in self._run_declarations:
            frozen = frozen_by_id.get(supplied.run_id)
            if frozen is None:
                # New metadata is deliberately ignored on immutable replay.
                continue
            if supplied.model_dump(mode="json") != frozen.model_dump(mode="json"):
                raise MemoryConflictError("run declaration is immutable and conflicts")

    @staticmethod
    def _context_payload(
        snapshot_id: str,
        outcome: RunOutcome,
        runs: Sequence[LessonRunDeclaration],
    ) -> dict[str, object]:
        return {
            "snapshot_id": snapshot_id,
            "outcome_run_id": outcome.run_id,
            "outcome_hash": StoredEpisodicLessonSource._outcome_hash(outcome),
            "runs": [run.model_dump(mode="json") for run in runs],
        }

    @staticmethod
    def _outcome_hash(outcome: RunOutcome) -> str:
        return sha256_json(outcome.model_dump(mode="json"))

    async def _ensure_snapshot(
        self,
        *,
        snapshot_id: str,
        idempotency_key: str,
    ) -> MemorySnapshot:
        # A canonical pre-read makes replay independent of the caller's cached
        # snapshot receipt and avoids trying to create a second immutable
        # snapshot after later episodic appends.
        snapshot = await self._read_snapshot(snapshot_id)
        if snapshot is None:
            operation_key = self._operation_key("snapshot", idempotency_key, snapshot_id)
            try:
                # As with declarations, the receipt is not authoritative.
                await self._store.create_snapshot(
                    namespace=self._episodic_namespace,
                    snapshot_id=snapshot_id,
                    operation=_SNAPSHOT_OPERATION,
                    idempotency_key=operation_key,
                )
            except MemoryConflictError:
                snapshot = await self._read_snapshot(snapshot_id)
                if snapshot is None:
                    raise
            snapshot = await self._read_snapshot(snapshot_id)
        if snapshot is None:
            raise MemoryPermanentError("lesson evidence snapshot is missing after creation")
        if snapshot.namespace != self._episodic_namespace:
            raise MemoryPermanentError("lesson evidence snapshot namespace mismatch")
        if len({member.record_id for member in snapshot.members}) != len(snapshot.members):
            raise MemoryPermanentError("lesson evidence snapshot has duplicate members")
        return snapshot

    async def _read_snapshot(self, snapshot_id: str) -> MemorySnapshot | None:
        value = await self._store.get_snapshot(snapshot_id=snapshot_id)
        return None if value is None else _owned_snapshot(value)

    async def _read_members(self, snapshot: MemorySnapshot) -> list[StoredRecord]:
        records: list[StoredRecord] = []
        for member in snapshot.members:
            value = await self._store.get(
                namespace=self._episodic_namespace,
                record_id=member.record_id,
            )
            if value is None:
                raise MemoryPermanentError(
                    f"episodic snapshot member {member.record_id!r} is missing"
                )
            record = _owned_record(value)
            if record.namespace != self._episodic_namespace:
                raise MemoryPermanentError("episodic member namespace mismatch")
            if record.record_id != member.record_id:
                raise MemoryPermanentError("episodic snapshot member record id mismatch")
            if record.content_hash != member.content_hash:
                raise MemoryPermanentError(
                    f"episodic snapshot member {member.record_id!r} content hash mismatch"
                )
            records.append(record)
        return records

    def _validate_records(self, records: list[StoredRecord], outcome: RunOutcome) -> None:
        matching_outcomes: list[RunOutcome] = []
        for record in records:
            if record.record_type == _TRANSITION_RECORD_TYPE:
                try:
                    transition = ExperienceTransition.model_validate(record.payload)
                except (TypeError, ValueError, ValidationError) as error:
                    raise MemoryPermanentError("episodic transition payload is invalid") from error
                if transition.transition_id != record.record_id:
                    raise MemoryPermanentError("episodic transition record id mismatch")
            elif record.record_type == _OUTCOME_RECORD_TYPE:
                try:
                    stored_outcome = RunOutcome.model_validate(record.payload)
                except (TypeError, ValueError, ValidationError) as error:
                    raise MemoryPermanentError("episodic outcome payload is invalid") from error
                expected_id = hashlib.sha256(
                    f"run-outcome:{stored_outcome.run_id}".encode()
                ).hexdigest()
                if record.record_id != expected_id:
                    raise MemoryPermanentError("episodic outcome record id mismatch")
                if stored_outcome.run_id == outcome.run_id:
                    matching_outcomes.append(stored_outcome)
            else:
                raise MemoryPermanentError(
                    f"episodic namespace contains unknown record type {record.record_type!r}"
                )

        if len(matching_outcomes) != 1:
            raise MemoryPermanentError("current run outcome is absent from episodic snapshot")
        if matching_outcomes[0].model_dump(mode="json") != outcome.model_dump(mode="json"):
            raise MemoryPermanentError(
                "episodic snapshot current outcome does not match final outcome"
            )

    @staticmethod
    def _declaration_record_id(run_id: str) -> str:
        return f"lesson-run:{sha256_json({'run_id': run_id})}"

    @staticmethod
    def _snapshot_id(
        run_id: str,
        *,
        episodic_namespace: str,
        declaration_namespace: str,
    ) -> str:
        return "lesson-snapshot:" + sha256_json(
            {
                "episodic_namespace": episodic_namespace,
                "declaration_namespace": declaration_namespace,
                "run_id": run_id,
            }
        )

    @staticmethod
    def _context_record_id(snapshot_id: str) -> str:
        return f"lesson-context:{sha256_json({'snapshot_id': snapshot_id})}"

    @staticmethod
    def _operation_key(kind: str, idempotency_key: str, value: str) -> str:
        return hashlib.sha256(
            canonical_json(
                {"kind": kind, "idempotency_key": idempotency_key, "value": value}
            ).encode()
        ).hexdigest()


__all__ = ["StoredEpisodicLessonSource"]
