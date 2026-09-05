"""Explicit, archive-preserving consolidation over immutable evidence snapshots.

The consolidator is deliberately outside the online runner path.  It plans
deterministic replay and contrast selections, asks the existing independent
validators to classify candidates, and persists an immutable plan.  Applying a
plan only records an auditable application receipt; episodic source rows are
never rewritten.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, ValidationError, model_validator

from uptick_agent.memory.candidate_validation import extract_candidates, validate_candidate
from uptick_agent.memory.contracts import (
    ConsolidationDelta,
    ConsolidationRequest,
    ConsolidationResult,
    ContextItem,
    ContractModel,
    ExperienceTransition,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    MemoryValidationError,
    ProvenanceRef,
    RunOutcome,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.lesson_contracts import (
    LESSON_RETENTION_POLICY,
    LESSON_VALIDATION_POLICY,
    LessonEvidence,
    LessonRunDeclaration,
    snapshot_input_hash,
)
from uptick_agent.memory.patterns import (
    REQUEST_SCOPE_MISSING,
    generate_pattern_candidates,
    request_scope_value,
    validate_pattern_candidate,
    verify_evidence_against_store,
)
from uptick_agent.memory.settings import ConsolidationSettings
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    StoredRecord,
    StructuredMemoryStore,
    canonical_json,
    sha256_json,
    validate_identifier,
    validate_namespace,
)
from uptick_agent.redaction import sanitize_json

CONSOLIDATION_MODULE_ID = "consolidation"
CONSOLIDATION_MODULE_VERSION = "1.0"
CONSOLIDATION_PLAN_RECORD_TYPE = "consolidation-plan"
CONSOLIDATION_APPLY_RECORD_TYPE = "consolidation-apply"
CONSOLIDATION_QUERY_CONTRACT = "memory-consolidation-query-v1@1.0"
CONSOLIDATION_RETENTION_POLICY = LESSON_RETENTION_POLICY
_TRANSITION_RECORD_TYPE = "experience-transition"
_OUTCOME_RECORD_TYPE = "run-outcome"
_DECLARATION_RECORD_TYPE = "lesson-run-declaration"
_WORD = re.compile(r"[\w-]+", re.UNICODE)


@runtime_checkable
class SnapshotEvidenceSource(Protocol):
    async def load(self, snapshot_id: str) -> LessonEvidence: ...


def _declaration_record_id(run_id: str) -> str:
    return f"lesson-run:{sha256_json({'run_id': run_id})}"


class StoredSnapshotEvidenceSource:
    """Load a complete, verified lesson bundle from one store snapshot."""

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        evidence_namespace: str,
        declaration_namespace: str,
    ) -> None:
        self._store = store
        self._evidence_namespace = validate_namespace(evidence_namespace)
        self._declaration_namespace = validate_namespace(declaration_namespace)
        if self._evidence_namespace == self._declaration_namespace:
            raise MemoryValidationError("evidence and declaration namespaces must be disjoint")

    async def load(self, snapshot_id: str) -> LessonEvidence:
        snapshot_id = validate_identifier(snapshot_id, name="snapshot_id", max_length=256)
        raw = await self._store.get_snapshot(snapshot_id=snapshot_id)
        if raw is None:
            raise MemoryConflictError("consolidation input snapshot is missing")
        try:
            snapshot = MemorySnapshot.validate_integrity(raw)
        except (TypeError, ValueError, ValidationError) as error:
            raise MemoryPermanentError("consolidation input snapshot is invalid") from error
        if snapshot.namespace != self._evidence_namespace:
            raise MemoryConflictError("consolidation snapshot belongs to another namespace")
        if len({member.record_id for member in snapshot.members}) != len(snapshot.members):
            raise MemoryPermanentError("consolidation snapshot repeats a record")

        records: list[StoredRecord] = []
        for member in snapshot.members:
            record = await self._store.get(
                namespace=snapshot.namespace,
                record_id=member.record_id,
            )
            if record is None:
                raise MemoryPermanentError("consolidation snapshot member is missing")
            try:
                owned = StoredRecord.validate_integrity(record)
            except (TypeError, ValueError, ValidationError) as error:
                raise MemoryPermanentError("consolidation snapshot member is invalid") from error
            if owned.content_hash != member.content_hash:
                raise MemoryPermanentError("consolidation snapshot member hash changed")
            if owned.record_type not in {_TRANSITION_RECORD_TYPE, _OUTCOME_RECORD_TYPE}:
                raise MemoryPermanentError("consolidation snapshot contains an unsupported record")
            try:
                if sanitize_json(owned.payload) != owned.payload:
                    raise ValueError("record payload contains credential-shaped content")
            except (TypeError, ValueError) as error:
                raise MemoryPermanentError(
                    "consolidation snapshot member payload is unsafe"
                ) from error
            try:
                if owned.record_type == _TRANSITION_RECORD_TYPE:
                    transition = ExperienceTransition.model_validate(owned.payload)
                    if transition.transition_id != owned.record_id:
                        raise ValueError("transition record ID mismatch")
                    if owned.created_at != transition.occurred_at:
                        raise ValueError("transition record timestamp mismatch")
                else:
                    outcome = RunOutcome.model_validate(owned.payload)
                    expected_id = hashlib.sha256(
                        f"run-outcome:{outcome.run_id}".encode()
                    ).hexdigest()
                    if expected_id != owned.record_id:
                        raise ValueError("outcome record ID mismatch")
                    if owned.created_at != outcome.finished_at:
                        raise ValueError("outcome record timestamp mismatch")
            except (TypeError, ValueError, ValidationError) as error:
                raise MemoryPermanentError(
                    "consolidation snapshot member payload is invalid"
                ) from error
            records.append(owned)

        run_ids = {
            record.payload.get("run_id")
            for record in records
            if isinstance(record.payload.get("run_id"), str)
        }
        declarations_by_run: dict[str, LessonRunDeclaration] = {}
        if run_ids:
            for record in await self._store.list(namespace=self._declaration_namespace):
                if record.record_type != _DECLARATION_RECORD_TYPE:
                    continue
                run_id = record.payload.get("run_id")
                if not isinstance(run_id, str) or run_id not in run_ids:
                    continue
                try:
                    owned_record = StoredRecord.validate_integrity(record)
                    declaration = LessonRunDeclaration.model_validate(owned_record.payload)
                except (TypeError, ValueError, ValidationError) as error:
                    raise MemoryPermanentError("stored run declaration is invalid") from error
                if owned_record.record_id != _declaration_record_id(declaration.run_id):
                    raise MemoryPermanentError("stored run declaration ID is invalid")
                previous = declarations_by_run.get(declaration.run_id)
                if previous is not None and previous != declaration:
                    raise MemoryPermanentError("stored run declarations conflict")
                declarations_by_run[declaration.run_id] = declaration
            missing = run_ids - declarations_by_run.keys()
            if missing and declarations_by_run:
                raise MemoryPermanentError("consolidation snapshot has partial run declarations")

        declarations = sorted(
            declarations_by_run.values(),
            key=lambda item: (item.logical_run_id, item.attempt_index, item.run_id),
        )
        return LessonEvidence(snapshot=snapshot, records=records, runs=declarations)


class ConsolidationPlan(ContractModel):
    """Immutable dry-run output bound to the complete source snapshot.

    ``created_at`` is the source snapshot timestamp used for deterministic
    plan identity.  The wall-clock time of the explicit apply operation is
    recorded separately as ``ConsolidationApply.applied_at``.
    """

    plan_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    request_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    snapshot_members: tuple[SnapshotMember, ...] = ()
    evidence_input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    settings: ConsolidationSettings
    settings_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    policy_ref: Literal[LESSON_VALIDATION_POLICY] = LESSON_VALIDATION_POLICY
    query_ref: Literal[CONSOLIDATION_QUERY_CONTRACT] = CONSOLIDATION_QUERY_CONTRACT
    replay_record_ids: tuple[str, ...] = ()
    contrast_pairs: tuple[tuple[str, str], ...] = ()
    candidate_dispositions: list[dict[str, object]] = Field(default_factory=list)
    active_items: list[dict[str, object]] = Field(default_factory=list)
    deltas: list[ConsolidationDelta] = Field(default_factory=list)
    unavailable_reason: str | None = Field(default=None, max_length=512)
    created_at: datetime
    retention_policy_ref: Literal[CONSOLIDATION_RETENTION_POLICY] = CONSOLIDATION_RETENTION_POLICY
    retention_class: Literal["project_lifetime"] = "project_lifetime"

    @model_validator(mode="after")
    def _identity_and_integrity(self) -> ConsolidationPlan:
        if self.settings_hash != sha256_json(self.settings.model_dump(mode="json")):
            raise ValueError("consolidation settings hash mismatch")
        if self.created_at.utcoffset() is None:
            raise ValueError("consolidation plan timestamp must be timezone-aware")
        expected = sha256_json(self.model_dump(mode="json", exclude={"plan_id"}))
        if self.plan_id != expected:
            raise ValueError("consolidation plan ID mismatch")
        if self.unavailable_reason is not None and (
            self.candidate_dispositions or self.active_items
        ):
            raise ValueError("unavailable consolidation plan cannot contain knowledge")
        return self


class ConsolidationApply(ContractModel):
    """Immutable acknowledgement that a validated plan was applied."""

    plan_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    request_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    evidence_input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    delta_ids: tuple[str, ...] = ()
    applied_at: datetime
    retention_policy_ref: Literal[CONSOLIDATION_RETENTION_POLICY] = CONSOLIDATION_RETENTION_POLICY
    retention_class: Literal["project_lifetime"] = "project_lifetime"


def _plan_record_id(plan_id: str) -> str:
    return f"consolidation-plan:{plan_id}"


def _apply_record_id(plan_id: str) -> str:
    return f"consolidation-apply:{plan_id}"


def _tokens(value: object) -> set[str]:
    return {token.casefold() for token in _WORD.findall(canonical_json(value)) if len(token) > 1}


def _contrast_pairs(records: list[StoredRecord], limit: int) -> tuple[tuple[str, str], ...]:
    if limit <= 0:
        return ()
    transitions: list[ExperienceTransition] = []
    for record in records:
        if record.record_type != _TRANSITION_RECORD_TYPE:
            continue
        try:
            transitions.append(ExperienceTransition.model_validate(record.payload))
        except (TypeError, ValueError, ValidationError):
            continue
    transitions.sort(key=lambda item: item.transition_id)
    pairs: list[tuple[str, str]] = []
    for index, left in enumerate(transitions):
        for right in transitions[index + 1 :]:
            same_observation = canonical_json(left.observation) == canonical_json(right.observation)
            same_action = canonical_json(left.action) == canonical_json(right.action)
            different_result = canonical_json(left.result) != canonical_json(right.result)
            if (same_observation and not same_action) or (same_action and different_result):
                pairs.append((left.transition_id, right.transition_id))
                if len(pairs) >= limit:
                    return tuple(pairs)
    return tuple(pairs)


def _candidate_item(
    kind: Literal["lesson", "world_hypothesis"],
    candidate: object,
    validated: object,
    settings: ContractModel,
) -> dict[str, object]:
    return {
        "kind": kind,
        "candidate": candidate.model_dump(mode="json"),
        "validated": validated.model_dump(mode="json"),
        "settings": settings.model_dump(mode="json"),
        "status": validated.status,
    }


class ConsolidationMemory:
    """Out-of-band consolidation participant and context contributor."""

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        namespace: str,
        evidence_namespace: str,
        declaration_namespace: str,
        settings: ConsolidationSettings,
        source: SnapshotEvidenceSource | None = None,
        module_version: str = CONSOLIDATION_MODULE_VERSION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._namespace = validate_namespace(namespace)
        if not isinstance(settings, ConsolidationSettings):
            raise MemoryValidationError("consolidation settings are invalid")
        try:
            self._settings = ConsolidationSettings.model_validate(settings.model_dump(mode="json"))
        except (TypeError, ValueError, ValidationError) as error:
            raise MemoryValidationError("consolidation settings are invalid") from error
        self._source = source or StoredSnapshotEvidenceSource(
            store,
            evidence_namespace=evidence_namespace,
            declaration_namespace=declaration_namespace,
        )
        if not isinstance(self._source, SnapshotEvidenceSource):
            raise MemoryValidationError("consolidation source must implement load")
        if not module_version or len(module_version) > 64:
            raise MemoryValidationError("consolidation module_version must contain 1-64 characters")
        self._module_version = module_version
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def settings(self) -> ConsolidationSettings:
        return self._settings.model_copy(deep=True)

    async def dry_run(
        self, snapshot_id: str, *, request_id: str, idempotency_key: str
    ) -> ConsolidationPlan:
        request_id = validate_identifier(request_id, name="request_id", max_length=256)
        idempotency_key = validate_identifier(
            idempotency_key, name="idempotency_key", max_length=256
        )
        plan = await self._build_plan(snapshot_id, request_id=request_id)
        record_id = _plan_record_id(plan.plan_id)
        existing = await self._store.get(namespace=self._namespace, record_id=record_id)
        if existing is None:
            try:
                await self._store.append(
                    RecordWrite(
                        namespace=self._namespace,
                        record_id=record_id,
                        record_type=CONSOLIDATION_PLAN_RECORD_TYPE,
                        payload=plan.model_dump(mode="json"),
                        created_at=plan.created_at,
                    ),
                    operation="consolidation-dry-run",
                    idempotency_key=idempotency_key,
                )
            except MemoryConflictError:
                existing = await self._store.get(namespace=self._namespace, record_id=record_id)
        if existing is None:
            existing = await self._store.get(namespace=self._namespace, record_id=record_id)
        if existing is not None:
            persisted = self._read_plan(existing)
            if persisted.model_dump(mode="json") != plan.model_dump(mode="json"):
                raise MemoryConflictError("consolidation plan replay conflicts")
            return persisted
        raise MemoryPermanentError("consolidation plan is missing after append")

    async def consolidate(self, request: ConsolidationRequest) -> ConsolidationResult:
        if not isinstance(request, ConsolidationRequest):
            raise MemoryValidationError("consolidation requires ConsolidationRequest")
        if request.dry_run:
            plan = await self.dry_run(
                request.snapshot_id,
                request_id=request.request_id,
                idempotency_key=request.idempotency_key,
            )
            return self._result(request, plan, applied=False)
        plans: list[ConsolidationPlan] = []
        for record in await self._store.list(namespace=self._namespace):
            if record.record_type != CONSOLIDATION_PLAN_RECORD_TYPE:
                continue
            plan = self._read_plan(record)
            if plan.request_id == request.request_id and plan.snapshot_id == request.snapshot_id:
                plans.append(plan)
        if len(plans) != 1:
            raise MemoryConflictError("consolidation apply requires one exact persisted plan")
        return await self.apply(plans[0].plan_id, idempotency_key=request.idempotency_key)

    async def apply(self, plan_id: str, *, idempotency_key: str) -> ConsolidationResult:
        plan_id = validate_identifier(plan_id, name="plan_id", max_length=64)
        idempotency_key = validate_identifier(
            idempotency_key, name="idempotency_key", max_length=256
        )
        stored = await self._store.get(
            namespace=self._namespace, record_id=_plan_record_id(plan_id)
        )
        if stored is None:
            raise MemoryConflictError("consolidation plan is missing")
        plan = self._read_plan(stored)
        fresh = await self._build_plan(plan.snapshot_id, request_id=plan.request_id)
        if fresh.model_dump(mode="json") != plan.model_dump(mode="json"):
            raise MemoryConflictError("consolidation plan input is stale")
        record_id = _apply_record_id(plan.plan_id)
        existing = await self._store.get(namespace=self._namespace, record_id=record_id)
        applied_plans = await self._validated_applied_plans()
        if existing is None:
            application_items = [
                (application, applied_plan) for application, applied_plan in applied_plans
            ]
            if application_items:
                _, latest_plan = max(
                    application_items,
                    key=lambda item: (
                        item[0].applied_at,
                        item[1].created_at,
                        item[1].plan_id,
                    ),
                )
                if not self._snapshot_member_keys(latest_plan).issubset(
                    self._snapshot_member_keys(plan)
                ):
                    raise MemoryConflictError(
                        "consolidation apply snapshot is stale or not a member superset"
                    )
            applied_at = self._clock()
            if not isinstance(applied_at, datetime) or applied_at.utcoffset() is None:
                raise MemoryValidationError(
                    "consolidation clock must return a timezone-aware datetime"
                )
            if application_items and applied_at < max(
                application.applied_at for application, _ in application_items
            ):
                raise MemoryConflictError(
                    "consolidation apply timestamp precedes an existing application"
                )
            self._check_snapshot_chain(
                application_items
                + [
                    (
                        ConsolidationApply(
                            plan_id=plan.plan_id,
                            request_id=plan.request_id,
                            snapshot_id=plan.snapshot_id,
                            snapshot_content_hash=plan.snapshot_content_hash,
                            evidence_input_hash=plan.evidence_input_hash,
                            delta_ids=tuple(self._delta_id(delta) for delta in plan.deltas),
                            applied_at=applied_at.astimezone(UTC),
                        ),
                        plan,
                    )
                ],
                permanent=False,
            )
            application = ConsolidationApply(
                plan_id=plan.plan_id,
                request_id=plan.request_id,
                snapshot_id=plan.snapshot_id,
                snapshot_content_hash=plan.snapshot_content_hash,
                evidence_input_hash=plan.evidence_input_hash,
                delta_ids=tuple(self._delta_id(delta) for delta in plan.deltas),
                applied_at=applied_at.astimezone(UTC),
            )
            try:
                await self._store.append(
                    RecordWrite(
                        namespace=self._namespace,
                        record_id=record_id,
                        record_type=CONSOLIDATION_APPLY_RECORD_TYPE,
                        payload=application.model_dump(mode="json"),
                        created_at=application.applied_at,
                    ),
                    operation="consolidation-apply",
                    idempotency_key=idempotency_key,
                )
            except MemoryConflictError:
                existing = await self._store.get(namespace=self._namespace, record_id=record_id)
        if existing is None:
            existing = await self._store.get(namespace=self._namespace, record_id=record_id)
        if existing is None:
            raise MemoryPermanentError("consolidation apply record is missing after append")
        persisted = self._read_apply(existing)
        if (
            persisted.plan_id != plan.plan_id
            or persisted.evidence_input_hash != plan.evidence_input_hash
        ):
            raise MemoryPermanentError("consolidation apply record does not match plan")
        if not applied_plans or all(
            application.plan_id != persisted.plan_id for application, _ in applied_plans
        ):
            applied_plans.append((persisted, plan))
        self._check_snapshot_chain(applied_plans, permanent=True)
        return self._result(
            ConsolidationRequest(
                request_id=plan.request_id,
                snapshot_id=plan.snapshot_id,
                idempotency_key=idempotency_key,
                dry_run=False,
            ),
            plan,
            applied=True,
        )

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        applied_records = await self._validated_applied_plans()
        self._check_snapshot_chain(applied_records, permanent=True)
        applied_plans = [(application.applied_at, plan) for application, plan in applied_records]

        if not applied_plans:
            return MemoryContribution(
                module_id=CONSOLIDATION_MODULE_ID,
                module_version=self._module_version,
            )
        # A later complete plan is authoritative for the derived view.  Using
        # every historical plan would keep an old active candidate visible
        # after a later snapshot classified it as disputed or absent.
        applied_plans.sort(key=lambda value: (value[0], value[1].created_at, value[1].plan_id))
        latest_plan = applied_plans[-1][1]
        excluded = {request.run_id}
        physical = request.context.get("physical_run_id")
        if isinstance(physical, str):
            excluded.add(physical)
        query_tokens = _tokens(request.query)
        ranked: dict[str, tuple[float, dict[str, object], int]] = {}
        for item in latest_plan.active_items:
            validated = item.get("validated")
            candidate = item.get("candidate")
            if not isinstance(validated, dict) or not isinstance(candidate, dict):
                raise MemoryPermanentError("consolidation active item is malformed")
            manifest = validated.get("manifest")
            if not isinstance(manifest, dict):
                raise MemoryPermanentError("consolidation active item manifest is malformed")
            support_ids = set(manifest.get("support_run_ids", ())) | set(
                manifest.get("support_logical_run_ids", ())
            )
            if excluded & support_ids:
                continue
            if item.get("kind") == "world_hypothesis":
                scope = candidate.get("scope")
                if not isinstance(scope, dict):
                    raise MemoryPermanentError("world consolidation item scope is malformed")
                if any(
                    (actual := request_scope_value(request.context, path)) is REQUEST_SCOPE_MISSING
                    or canonical_json(actual) != canonical_json(expected)
                    for path, expected in scope.items()
                ):
                    continue
            item_tokens = _tokens(candidate)
            overlap = len(query_tokens & item_tokens)
            if query_tokens and overlap == 0:
                continue
            item_id = str(candidate.get("candidate_id") or candidate.get("lesson_id") or "")
            if not item_id:
                raise MemoryPermanentError("consolidation candidate ID is missing")
            score = overlap / max(1, len(item_tokens))
            previous = ranked.get(item_id)
            if previous is None or (score, -overlap, item_id) > (
                previous[0],
                -previous[2],
                item_id,
            ):
                ranked[item_id] = (score, item, overlap)
        ordered = sorted(ranked.values(), key=lambda value: (-value[0], str(value[1]["candidate"])))
        if request.max_items is not None:
            ordered = ordered[: request.max_items]
        return MemoryContribution(
            module_id=CONSOLIDATION_MODULE_ID,
            module_version=self._module_version,
            items=[self._context_item(item, score, overlap) for score, item, overlap in ordered],
        )

    async def _validated_applied_plans(
        self,
    ) -> list[tuple[ConsolidationApply, ConsolidationPlan]]:
        """Load and independently revalidate every immutable apply receipt."""

        applied: list[tuple[ConsolidationApply, ConsolidationPlan]] = []
        for record in await self._store.list(namespace=self._namespace):
            if record.record_type != CONSOLIDATION_APPLY_RECORD_TYPE:
                continue
            application = self._read_apply(record)
            stored_plan = await self._store.get(
                namespace=self._namespace, record_id=_plan_record_id(application.plan_id)
            )
            if stored_plan is None:
                raise MemoryPermanentError("consolidation apply references missing plan")
            plan = self._read_plan(stored_plan)
            if (
                application.snapshot_id != plan.snapshot_id
                or application.snapshot_content_hash != plan.snapshot_content_hash
                or application.evidence_input_hash != plan.evidence_input_hash
            ):
                raise MemoryPermanentError("consolidation apply and plan disagree")
            fresh = await self._build_plan(plan.snapshot_id, request_id=plan.request_id)
            if fresh.model_dump(mode="json") != plan.model_dump(mode="json"):
                raise MemoryPermanentError("consolidation plan no longer validates")
            applied.append((application, fresh))
        return applied

    @staticmethod
    def _snapshot_member_keys(plan: ConsolidationPlan) -> frozenset[tuple[str, str]]:
        return frozenset(
            (member.record_id, member.content_hash) for member in plan.snapshot_members
        )

    @classmethod
    def _check_snapshot_chain(
        cls,
        applied: list[tuple[ConsolidationApply, ConsolidationPlan]],
        *,
        permanent: bool,
    ) -> None:
        """Require applied evidence snapshots to grow monotonically."""

        ordered = sorted(
            applied,
            key=lambda item: (item[0].applied_at, item[1].created_at, item[1].plan_id),
        )
        previous: frozenset[tuple[str, str]] | None = None
        for _, plan in ordered:
            current = cls._snapshot_member_keys(plan)
            if previous is not None and not previous.issubset(current):
                message = "consolidation applied snapshots must be monotonic nested member sets"
                if permanent:
                    raise MemoryPermanentError(message)
                raise MemoryConflictError(message)
            previous = current

    async def _build_plan(self, snapshot_id: str, *, request_id: str) -> ConsolidationPlan:
        evidence = await self._source.load(snapshot_id)
        if not isinstance(evidence, LessonEvidence):
            raise MemoryPermanentError("consolidation source returned invalid evidence")
        snapshot = MemorySnapshot.validate_integrity(evidence.snapshot)
        if not evidence.runs:
            return self._make_plan(
                request_id=request_id,
                evidence=evidence,
                snapshot=snapshot,
                replay_record_ids=tuple(record.record_id for record in evidence.records)[
                    : self._settings.max_replay_records
                ],
                contrast_pairs=(),
                candidate_dispositions=[],
                active_items=[],
                deltas=[],
                unavailable_reason="authoritative run declarations are unavailable",
            )
        try:
            owned = await verify_evidence_against_store(self._store, evidence)
            if self._settings.lesson_settings is None and self._settings.pattern_settings is None:
                raise MemoryValidationError("consolidation has no validator settings")
        except (MemoryValidationError, MemoryPermanentError):
            raise
        replay_ids = tuple(sorted(record.record_id for record in owned.records))[
            : self._settings.max_replay_records
        ]
        dispositions: list[dict[str, object]] = []
        active_items: list[dict[str, object]] = []
        deltas: list[ConsolidationDelta] = []
        if self._settings.lesson_settings is not None:
            settings = self._settings.lesson_settings
            for candidate in extract_candidates(owned, settings):
                validated = validate_candidate(candidate, owned, settings)
                item = _candidate_item("lesson", candidate, validated, settings)
                dispositions.append(item)
                if validated.status == "active":
                    active_items.append(item)
                deltas.append(
                    ConsolidationDelta(
                        artefact_type="lesson",
                        operation="create",
                        payload=item,
                    )
                )
        if self._settings.pattern_settings is not None:
            settings = self._settings.pattern_settings
            for candidate in generate_pattern_candidates(owned, settings):
                validated = validate_pattern_candidate(candidate, owned, settings)
                item = _candidate_item("world_hypothesis", candidate, validated, settings)
                dispositions.append(item)
                if validated.status == "active":
                    active_items.append(item)
                deltas.append(
                    ConsolidationDelta(
                        artefact_type="world_hypothesis",
                        operation="create",
                        payload=item,
                    )
                )
        dispositions.sort(key=lambda item: canonical_json(item["candidate"]))
        active_items.sort(key=lambda item: canonical_json(item["candidate"]))
        deltas.sort(key=lambda delta: (delta.artefact_type, canonical_json(delta.payload)))
        return self._make_plan(
            request_id=request_id,
            evidence=owned,
            snapshot=snapshot,
            replay_record_ids=replay_ids,
            contrast_pairs=_contrast_pairs(list(owned.records), self._settings.max_contrast_pairs),
            candidate_dispositions=dispositions,
            active_items=active_items,
            deltas=deltas,
            unavailable_reason=None,
        )

    def _make_plan(
        self,
        *,
        request_id: str,
        evidence: LessonEvidence,
        snapshot: MemorySnapshot,
        replay_record_ids: tuple[str, ...],
        contrast_pairs: tuple[tuple[str, str], ...],
        candidate_dispositions: list[dict[str, object]],
        active_items: list[dict[str, object]],
        deltas: list[ConsolidationDelta],
        unavailable_reason: str | None,
    ) -> ConsolidationPlan:
        settings_hash = sha256_json(self._settings.model_dump(mode="json"))
        body = {
            "schema_version": "1.0",
            "request_id": request_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_content_hash": snapshot.content_hash,
            "snapshot_members": [member.model_dump(mode="json") for member in snapshot.members],
            "evidence_input_hash": snapshot_input_hash(evidence),
            "settings": self._settings.model_dump(mode="json"),
            "settings_hash": settings_hash,
            "policy_ref": LESSON_VALIDATION_POLICY,
            "query_ref": CONSOLIDATION_QUERY_CONTRACT,
            "replay_record_ids": replay_record_ids,
            "contrast_pairs": contrast_pairs,
            "candidate_dispositions": candidate_dispositions,
            "active_items": active_items,
            "deltas": [delta.model_dump(mode="json") for delta in deltas],
            "unavailable_reason": unavailable_reason,
            "created_at": snapshot.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "retention_policy_ref": CONSOLIDATION_RETENTION_POLICY,
            "retention_class": "project_lifetime",
        }
        plan = ConsolidationPlan(plan_id=sha256_json(body), **body)
        return ConsolidationPlan.model_validate(plan.model_dump(mode="json"))

    @staticmethod
    def _delta_id(delta: ConsolidationDelta) -> str:
        return f"{delta.artefact_type}:{sha256_json(delta.payload)}"

    @staticmethod
    def _result(
        request: ConsolidationRequest, plan: ConsolidationPlan, *, applied: bool
    ) -> ConsolidationResult:
        return ConsolidationResult(
            request_id=request.request_id,
            snapshot_id=request.snapshot_id,
            applied=applied,
            deltas=plan.deltas,
        )

    def _read_plan(self, record: StoredRecord) -> ConsolidationPlan:
        try:
            owned = StoredRecord.validate_integrity(record)
            if (
                owned.namespace != self._namespace
                or owned.record_type != CONSOLIDATION_PLAN_RECORD_TYPE
            ):
                raise MemoryPermanentError("stored consolidation plan namespace or type is invalid")
            plan = ConsolidationPlan.model_validate(owned.payload)
            if owned.record_id != _plan_record_id(plan.plan_id):
                raise MemoryPermanentError("stored consolidation plan ID mismatch")
            if owned.created_at != plan.created_at:
                raise MemoryPermanentError("stored consolidation plan timestamp mismatch")
            return plan
        except MemoryPermanentError:
            raise
        except (TypeError, ValueError, ValidationError) as error:
            raise MemoryPermanentError("stored consolidation plan is invalid") from error

    def _read_apply(self, record: StoredRecord) -> ConsolidationApply:
        try:
            owned = StoredRecord.validate_integrity(record)
            if (
                owned.namespace != self._namespace
                or owned.record_type != CONSOLIDATION_APPLY_RECORD_TYPE
            ):
                raise MemoryPermanentError(
                    "stored consolidation apply namespace or type is invalid"
                )
            application = ConsolidationApply.model_validate(owned.payload)
            if owned.record_id != _apply_record_id(application.plan_id):
                raise MemoryPermanentError("stored consolidation apply ID mismatch")
            if owned.created_at != application.applied_at:
                raise MemoryPermanentError("stored consolidation apply timestamp mismatch")
            return application
        except MemoryPermanentError:
            raise
        except (TypeError, ValueError, ValidationError) as error:
            raise MemoryPermanentError("stored consolidation apply is invalid") from error

    def _context_item(self, item: dict[str, object], score: float, overlap: int) -> ContextItem:
        candidate = item["candidate"]
        validated = item["validated"]
        if not isinstance(candidate, dict) or not isinstance(validated, dict):
            raise MemoryPermanentError("consolidation context item is malformed")
        try:
            provenance = [ProvenanceRef.model_validate(value) for value in validated["provenance"]]
            item_id = str(candidate.get("candidate_id") or candidate.get("lesson_id"))
            view_data: dict[str, object] = {
                "kind": item["kind"],
                "candidate": candidate,
                "status": validated["status"],
                "confidence": validated["confidence"],
            }
            if "confidence_basis" in validated:
                view_data["confidence_basis"] = validated["confidence_basis"]
            elif "estimated_utility" in validated:
                view_data["estimated_utility"] = validated["estimated_utility"]
            view = sanitize_json(view_data)
            envelope = UntrustedMemoryEnvelope(
                item_id=item_id,
                artefact_type=str(item["kind"]),
                origin_module=CONSOLIDATION_MODULE_ID,
                origin_version=self._module_version,
                trust_classification="derived_untrusted",
                provenance=provenance,
                item=view,
            )
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise MemoryPermanentError("consolidation context item is invalid") from error
        return ContextItem(
            envelope=envelope,
            score=score,
            selection_reason=f"consolidation candidate matched; lexical overlap={overlap}",
            estimated_tokens=0,
        )


__all__ = [
    "CONSOLIDATION_APPLY_RECORD_TYPE",
    "CONSOLIDATION_MODULE_ID",
    "CONSOLIDATION_MODULE_VERSION",
    "CONSOLIDATION_PLAN_RECORD_TYPE",
    "CONSOLIDATION_QUERY_CONTRACT",
    "ConsolidationApply",
    "ConsolidationMemory",
    "ConsolidationPlan",
    "ConsolidationSettings",
    "SnapshotEvidenceSource",
    "StoredSnapshotEvidenceSource",
]
