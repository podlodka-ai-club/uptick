"""Explicit, archive-preserving memory maintenance.

Maintenance is intentionally out of the online runner path.  A caller first
creates a dry-run plan from an immutable snapshot and a deterministic proposal
callback, then explicitly applies that plan.  The current store contract has
no verified deletion or update operation, so apply records an auditable
maintenance manifest in a separate namespace.  Source records remain intact;
``MaintenanceRetrievalView`` consumes the applied manifest for operational
supersession and age decay without deleting evidence.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from uptick_agent.memory.contracts import (
    ConsolidationDelta,
    ConsolidationRequest,
    ConsolidationResult,
    ContextItem,
    ContractModel,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryPermanentError,
    MemoryValidationError,
    ProvenanceRef,
    require_finite_json,
)
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

_MAINTENANCE_RECORD_TYPE = "memory-maintenance-application"
_MAINTENANCE_PLAN_RECORD_TYPE = "memory-maintenance-plan"
_AUDIT_RETENTION_POLICY_REF = "simulator-audit-retention-v1@1.0"
_RAW_RETENTION_DAYS = 90
_SUMMARY_MAX_CHARS = 512
_SUMMARY_FIELDS = (
    "run_id",
    "iteration",
    "environment_id",
    "scenario_id",
    "observation",
    "action",
    "result",
    "objective_deltas",
    "terminal",
)
_DEFAULT_LIFETIME_TYPES = (
    "summary",
    "memory-summary",
    "lesson-batch",
    "world-hypothesis-batch",
    "playbook-batch",
    "tool-knowledge-batch",
    "validation",
    "validation-manifest",
    "candidate-validation",
    "promotion",
    "promotion-manifest",
    "approval",
    "approval-record",
    "rollback",
    "rollback-record",
    "audit-trace",
    "audit-trace-event",
    "lesson-run-declaration",
    "lesson-capture-context",
    _MAINTENANCE_PLAN_RECORD_TYPE,
    _MAINTENANCE_RECORD_TYPE,
)


class RetentionHold(ContractModel):
    """An active hold supplied by the authority that owns retention policy."""

    hold_id: str = Field(min_length=1, max_length=256)
    artefact_ids: list[str] = Field(min_length=1, max_length=10_000)


class MaintenanceRetentionPolicy(ContractModel):
    """Retention policy with no boolean deletion bypass."""

    raw_minimum_days: int = Field(default=_RAW_RETENTION_DAYS, ge=_RAW_RETENTION_DAYS)
    project_lifetime_record_types: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_LIFETIME_TYPES),
        min_length=1,
    )

    @model_validator(mode="after")
    def _retain_mandated_lifetime_types(self) -> MaintenanceRetentionPolicy:
        missing = set(_DEFAULT_LIFETIME_TYPES) - set(self.project_lifetime_record_types)
        if missing:
            raise ValueError(
                "project lifetime retention cannot omit mandated record types: "
                + ", ".join(sorted(missing))
            )
        return self


class RetentionEntry(ContractModel):
    record_id: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    retention_class: Literal["raw", "project_lifetime"]
    retained_until: datetime | None = None
    protected_by: list[str] = Field(default_factory=list)


class MaintenanceDelta(ContractModel):
    """One callback-proposed, evidence-linked maintenance operation."""

    delta_id: str = Field(min_length=1, max_length=256)
    operation: Literal["link", "supersede", "summary", "index_reduction"]
    target_record_id: str = Field(min_length=1, max_length=256)
    target_record_type: str | None = Field(default=None, max_length=128)
    target_payload: dict[str, object] = Field(default_factory=dict)
    source_members: list[SnapshotMember] = Field(min_length=1, max_length=10_000)
    provenance: list[ProvenanceRef] = Field(min_length=1, max_length=10_000)

    @field_validator("target_payload", mode="before")
    @classmethod
    def _finite_payload(cls, value: object) -> object:
        return require_finite_json(value)


class MaintenancePlan(ContractModel):
    """Immutable dry-run output bound to one snapshot and its member hashes."""

    plan_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    request_id: str = Field(min_length=1, max_length=256)
    callback_id: str = Field(min_length=1, max_length=256)
    namespace: str = Field(min_length=1, max_length=256)
    maintenance_namespace: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    snapshot_members: list[SnapshotMember] = Field(default_factory=list, max_length=10_000)
    created_at: datetime
    retention_policy: MaintenanceRetentionPolicy
    snapshot_retained_until: datetime
    retention_entries: list[RetentionEntry] = Field(default_factory=list, max_length=10_000)
    protected_record_ids: list[str] = Field(default_factory=list, max_length=10_000)
    deltas: list[MaintenanceDelta] = Field(default_factory=list, max_length=10_000)
    blocked_delta_ids: list[str] = Field(default_factory=list, max_length=10_000)
    unsupported_operations: list[str] = Field(default_factory=lambda: ["physical_delete"])
    warnings: list[str] = Field(default_factory=list)


class MaintenanceApplyResult(ContractModel):
    """Result of committing an auditable manifest, not source-row mutation."""

    plan_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    application_id: str = Field(min_length=1, max_length=256)
    applied: bool
    already_applied: bool
    delta_ids: list[str] = Field(default_factory=list)
    supported_operations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


ProposalCallback = Callable[[tuple[StoredRecord, ...]], Iterable[MaintenanceDelta]]


def _now_utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise MemoryValidationError("maintenance clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _plan_hash(plan: MaintenancePlan) -> str:
    body = plan.model_dump(mode="json")
    body.pop("plan_id", None)
    return sha256_json(body)


def _member_key(member: SnapshotMember) -> tuple[str, str]:
    return member.record_id, member.content_hash


def _semantic_payload(record: StoredRecord) -> dict[str, object]:
    """Remove known top-level run metadata while preserving nested evidence."""

    if record.record_type not in {"experience-transition", "episode"}:
        return record.payload
    metadata = {"transition_id", "run_id", "occurred_at"}
    return {key: value for key, value in record.payload.items() if key.casefold() not in metadata}


def _bounded_summary_value(value: object) -> object:
    """Keep extractive summary fields bounded without inventing content."""

    rendered = canonical_json(value)
    if len(rendered) <= _SUMMARY_MAX_CHARS:
        return value
    if isinstance(value, str):
        marker = " …[omitted]"
        candidate = value[: max(0, _SUMMARY_MAX_CHARS - len(marker))] + marker
        while len(canonical_json(candidate)) > _SUMMARY_MAX_CHARS and candidate:
            candidate = candidate[:-1]
        return candidate
    return {
        "_omitted": True,
        "_content_hash": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        "_original_chars": len(rendered),
    }


def _provenance(members: list[SnapshotMember]) -> list[ProvenanceRef]:
    return [
        ProvenanceRef(artefact_id=member.record_id, content_hash=member.content_hash)
        for member in members
    ]


@dataclass(frozen=True)
class _Protection:
    by_record: dict[str, tuple[str, ...]]
    unresolved_candidate_provenance: tuple[str, ...] = ()


class MemoryMaintenance:
    """Plan and explicitly apply safe maintenance against a frozen snapshot."""

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        namespace: str,
        maintenance_namespace: str | None = None,
        retention_policy: MaintenanceRetentionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        proposal_callback: ProposalCallback | None = None,
        callback_id: str = "deterministic-maintenance-v1",
        active_holds: Iterable[RetentionHold] = (),
    ) -> None:
        self._store = store
        self._namespace = validate_namespace(namespace)
        derived = (
            f"{self._namespace}:maintenance"
            if maintenance_namespace is None
            else maintenance_namespace
        )
        self._maintenance_namespace = validate_namespace(derived)
        if self._namespace == self._maintenance_namespace:
            raise MemoryValidationError("maintenance namespace must be separate from source memory")
        policy = retention_policy if retention_policy is not None else MaintenanceRetentionPolicy()
        self._retention = MaintenanceRetentionPolicy.model_validate(policy.model_dump(mode="json"))
        self._clock = clock or (lambda: datetime.now(UTC))
        if proposal_callback is not None and not callable(proposal_callback):
            raise MemoryValidationError("maintenance proposal callback must be callable")
        self._proposal_callback = (
            self._deterministic_proposals if proposal_callback is None else proposal_callback
        )
        self._callback_id = validate_identifier(callback_id, name="callback_id", max_length=256)
        self._active_holds = tuple(active_holds)

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def maintenance_namespace(self) -> str:
        return self._maintenance_namespace

    async def create_plan(
        self,
        snapshot_id: str,
        *,
        request_id: str,
        callback_id: str | None = None,
        proposal_callback: ProposalCallback | None = None,
        active_holds: Iterable[RetentionHold] = (),
    ) -> MaintenancePlan:
        """Create an immutable dry-run plan; no source or runner state changes."""

        snapshot_id = validate_identifier(snapshot_id, name="snapshot_id", max_length=256)
        request_id = validate_identifier(request_id, name="request_id", max_length=256)
        callback_id = validate_identifier(
            self._callback_id if callback_id is None else callback_id,
            name="callback_id",
            max_length=256,
        )
        proposal_callback = (
            self._proposal_callback if proposal_callback is None else proposal_callback
        )
        if not callable(proposal_callback):
            raise MemoryValidationError("maintenance requires a proposal callback")
        snapshot, records = await self._read_snapshot(snapshot_id)
        callback_records = tuple(record.model_copy(deep=True) for record in records)
        try:
            proposals = list(proposal_callback(callback_records))
        except MemoryValidationError:
            raise
        except Exception as error:
            raise MemoryValidationError("maintenance proposal callback failed") from error
        member_map = {_member_key(member): member for member in snapshot.members}
        record_ids = {member.record_id for member in snapshot.members}
        normalized: list[MaintenanceDelta] = []
        seen_delta_ids: set[str] = set()
        for proposal in proposals:
            delta = self._validate_delta(proposal, member_map, record_ids)
            if delta.delta_id in seen_delta_ids:
                raise MemoryValidationError(f"duplicate maintenance delta {delta.delta_id}")
            seen_delta_ids.add(delta.delta_id)
            normalized.append(delta)

        protection = self._protection(records, active_holds)
        blocked: list[str] = []
        deltas: list[MaintenanceDelta] = []
        for delta in normalized:
            touched = {member.record_id for member in delta.source_members}
            touched.add(delta.target_record_id)
            if touched & protection.by_record.keys():
                blocked.append(delta.delta_id)
            else:
                deltas.append(delta)
        now = _now_utc(self._clock)
        entries = [
            self._retention_entry(record, protection, plan_created_at=now) for record in records
        ]
        manifest_only = sorted({delta.operation for delta in deltas} - {"supersede"})
        warnings = [
            "physical_delete is unsupported by StructuredMemoryStore; source archive is retained",
            "active-candidate protection is limited to provenance visible "
            "in the input snapshot; source archive is retained",
        ]
        if manifest_only:
            warnings.append(
                "manifest-only operations with the current store: " + ", ".join(manifest_only)
            )
        warnings.extend(
            "active candidate provenance is outside the input snapshot and "
            f"cannot be resolved: {record_id}"
            for record_id in protection.unresolved_candidate_provenance
        )
        warnings.extend(
            f"delta {delta_id} blocked by active retention protection" for delta_id in blocked
        )
        plan = MaintenancePlan(
            plan_id="0" * 64,
            request_id=request_id,
            callback_id=callback_id,
            namespace=self._namespace,
            maintenance_namespace=self._maintenance_namespace,
            snapshot_id=snapshot.snapshot_id,
            snapshot_content_hash=snapshot.content_hash,
            snapshot_members=snapshot.members,
            created_at=now,
            retention_policy=self._retention,
            snapshot_retained_until=(
                max(snapshot.created_at.astimezone(UTC), now)
                + timedelta(days=self._retention.raw_minimum_days)
            ),
            retention_entries=entries,
            protected_record_ids=sorted(protection.by_record),
            deltas=deltas,
            blocked_delta_ids=blocked,
            unsupported_operations=["physical_delete"],
            warnings=warnings,
        )
        return plan.model_copy(update={"plan_id": _plan_hash(plan)})

    async def dry_run(self, *args, **kwargs) -> MaintenancePlan:
        """Named alias for callers exposing an explicit dry-run command."""

        return await self.create_plan(*args, **kwargs)

    async def load_persisted_plan(
        self,
        request: ConsolidationRequest,
        *,
        active_holds: Iterable[RetentionHold] = (),
    ) -> MaintenancePlan:
        """Load an existing dry-run plan without creating a new one."""

        if not isinstance(request, ConsolidationRequest):
            raise MemoryValidationError("maintenance plan lookup requires ConsolidationRequest")
        plan = await self._load_persisted_plan(request, active_holds=active_holds)
        if plan is None:
            raise MemoryConflictError("no persisted maintenance plan matches the request")
        return plan

    async def consolidate(self, request: ConsolidationRequest) -> ConsolidationResult:
        """Implement the explicit ``ConsolidationParticipant`` capability.

        The orchestrator may call this only through its explicit
        ``consolidate`` command.  No runner/finalizer method invokes it.
        """

        if not isinstance(request, ConsolidationRequest):
            raise MemoryValidationError("maintenance consolidation requires ConsolidationRequest")
        plan = await self._load_persisted_plan(
            request,
            active_holds=self._active_holds,
        )
        if plan is None:
            if not request.dry_run:
                raise MemoryConflictError(
                    "maintenance apply requires a previously persisted dry-run plan"
                )
            plan = await self.create_plan(
                request.snapshot_id,
                request_id=request.request_id,
                callback_id=self._callback_id,
                proposal_callback=self._proposal_callback,
                active_holds=self._active_holds,
            )
            await self._persist_plan(plan)
        if not request.dry_run:
            await self.apply(
                plan,
                idempotency_key=request.idempotency_key,
                active_holds=self._active_holds,
            )
        return ConsolidationResult(
            request_id=request.request_id,
            snapshot_id=request.snapshot_id,
            applied=not request.dry_run,
            deltas=[
                ConsolidationDelta(
                    artefact_type="maintenance-plan",
                    operation="create",
                    payload={
                        "plan": plan.model_dump(mode="json"),
                        "dry_run": request.dry_run,
                    },
                )
            ],
        )

    async def _load_persisted_plan(
        self,
        request: ConsolidationRequest,
        *,
        active_holds: Iterable[RetentionHold],
    ) -> MaintenancePlan | None:
        """Load the exact dry-run plan for an out-of-band apply request."""

        for record in await self._store.list(namespace=self._maintenance_namespace):
            if record.record_type != _MAINTENANCE_PLAN_RECORD_TYPE:
                continue
            payload = record.payload.get("plan")
            if not isinstance(payload, dict):
                continue
            if (
                payload.get("request_id") != request.request_id
                or payload.get("snapshot_id") != request.snapshot_id
            ):
                continue
            try:
                plan = self._owned_plan(MaintenancePlan.model_validate(payload))
            except (
                MemoryValidationError,
                MemoryConflictError,
                PydanticValidationError,
            ) as error:
                raise MemoryPermanentError("persisted maintenance plan is invalid") from error
            self._validate_plan_binding(plan)
            _, records = await self._read_snapshot(
                plan.snapshot_id,
                expected_content_hash=plan.snapshot_content_hash,
                expected_members=plan.snapshot_members,
            )
            current_protection = self._protection(records, active_holds)
            if sorted(current_protection.by_record) != plan.protected_record_ids:
                raise MemoryConflictError("persisted maintenance plan protection is stale")
            return plan
        return None

    async def _persist_plan(self, plan: MaintenancePlan) -> None:
        record_id = (
            "plan-"
            + hashlib.sha256(
                f"{plan.request_id}:{plan.snapshot_id}:{plan.callback_id}".encode()
            ).hexdigest()
        )
        payload = {"plan": plan.model_dump(mode="json")}
        existing = await self._store.get(
            namespace=self._maintenance_namespace,
            record_id=record_id,
        )
        if existing is not None:
            owned = StoredRecord.validate_integrity(existing)
            if owned.payload != payload:
                raise MemoryConflictError("maintenance plan identity has conflicting content")
            return
        await self._store.append(
            RecordWrite(
                namespace=self._maintenance_namespace,
                record_id=record_id,
                record_type=_MAINTENANCE_PLAN_RECORD_TYPE,
                payload=payload,
                created_at=plan.created_at,
            ),
            operation="persist-maintenance-plan",
            idempotency_key=record_id,
        )

    async def apply(
        self,
        plan: MaintenancePlan,
        *,
        idempotency_key: str,
        active_holds: Iterable[RetentionHold],
    ) -> MaintenanceApplyResult:
        """Durably record a validated plan application in the archive namespace."""

        owned = self._owned_plan(plan)
        self._validate_plan_binding(owned)
        idempotency_key = validate_identifier(
            idempotency_key,
            name="idempotency_key",
            max_length=256,
        )
        snapshot, records = await self._read_snapshot(
            owned.snapshot_id,
            expected_content_hash=owned.snapshot_content_hash,
            expected_members=owned.snapshot_members,
        )
        protection = self._protection(records, active_holds)
        for delta in owned.deltas:
            touched = {member.record_id for member in delta.source_members}
            touched.add(delta.target_record_id)
            if touched & protection.by_record.keys():
                raise MemoryConflictError("maintenance plan intersects a newly active hold")
            self._validate_delta(
                delta,
                {_member_key(member): member for member in snapshot.members},
                {member.record_id for member in snapshot.members},
            )

        application_id = (
            "maintenance-"
            + hashlib.sha256(f"{owned.plan_id}:{idempotency_key}".encode()).hexdigest()
        )
        immutable_payload = {
            "plan": owned.model_dump(mode="json"),
            "source_archive": "retained",
            "physical_delete": "unsupported",
        }
        existing = await self._store.get(
            namespace=self._maintenance_namespace,
            record_id=application_id,
        )
        if existing is None:
            for record in await self._store.list(namespace=self._maintenance_namespace):
                if (
                    record.record_type == _MAINTENANCE_RECORD_TYPE
                    and record.payload.get("plan", {}).get("plan_id") == owned.plan_id
                ):
                    return self._result(owned, record.record_id, already_applied=True)
            applied_at = _now_utc(self._clock)
            expected_payload = {**immutable_payload, "applied_at": applied_at.isoformat()}
            await self._store.append(
                RecordWrite(
                    namespace=self._maintenance_namespace,
                    record_id=application_id,
                    record_type=_MAINTENANCE_RECORD_TYPE,
                    payload=expected_payload,
                    created_at=applied_at,
                ),
                operation="apply-maintenance",
                idempotency_key=idempotency_key,
            )
        else:
            owned_existing = StoredRecord.validate_integrity(existing)
            payload_without_timestamp = {
                key: value for key, value in owned_existing.payload.items() if key != "applied_at"
            }
            applied_at = owned_existing.payload.get("applied_at")
            try:
                parsed_applied_at = datetime.fromisoformat(applied_at)
            except (TypeError, ValueError) as error:
                raise MemoryConflictError("maintenance application timestamp is invalid") from error
            if (
                parsed_applied_at.utcoffset() is None
                or payload_without_timestamp != immutable_payload
            ):
                raise MemoryConflictError("maintenance application ID has conflicting content")
        return self._result(owned, application_id, already_applied=existing is not None)

    async def _read_snapshot(
        self,
        snapshot_id: str,
        *,
        expected_content_hash: str | None = None,
        expected_members: list[SnapshotMember] | None = None,
    ) -> tuple[MemorySnapshot, list[StoredRecord]]:
        raw_snapshot = await self._store.get_snapshot(snapshot_id=snapshot_id)
        if raw_snapshot is None:
            raise MemoryConflictError("maintenance input snapshot is missing")
        snapshot = MemorySnapshot.validate_integrity(raw_snapshot)
        if snapshot.namespace != self._namespace:
            raise MemoryConflictError("maintenance snapshot belongs to another namespace")
        if snapshot.created_at.utcoffset() is None:
            raise MemoryValidationError("maintenance snapshot timestamp must include a timezone")
        if expected_content_hash is not None and snapshot.content_hash != expected_content_hash:
            raise MemoryConflictError("maintenance input snapshot is stale")
        if expected_members is not None and snapshot.members != expected_members:
            raise MemoryConflictError("maintenance snapshot membership changed")
        member_ids = [member.record_id for member in snapshot.members]
        if len(member_ids) != len(set(member_ids)):
            raise MemoryPermanentError("maintenance snapshot repeats a record")
        records: list[StoredRecord] = []
        for member in snapshot.members:
            record = await self._store.get(
                namespace=self._namespace,
                record_id=member.record_id,
            )
            if record is None:
                raise MemoryConflictError(
                    f"maintenance source record {member.record_id} is missing"
                )
            owned = StoredRecord.validate_integrity(record)
            if owned.content_hash != member.content_hash:
                raise MemoryConflictError(f"maintenance source record {member.record_id} is stale")
            records.append(owned)
        return snapshot, records

    @staticmethod
    def _validate_delta(
        proposal: object,
        member_map: dict[tuple[str, str], SnapshotMember],
        record_ids: set[str],
    ) -> MaintenanceDelta:
        if not isinstance(proposal, MaintenanceDelta):
            raise MemoryValidationError("proposal callback returned a non-contract delta")
        try:
            delta = MaintenanceDelta.model_validate(proposal.model_dump(mode="json"))
        except (TypeError, ValueError) as error:
            raise MemoryValidationError("proposal callback returned an invalid delta") from error
        source_ids = [member.record_id for member in delta.source_members]
        if len(set(source_ids)) != len(source_ids):
            raise MemoryValidationError(f"delta {delta.delta_id} repeats a source record")
        if any(_member_key(member) not in member_map for member in delta.source_members):
            raise MemoryConflictError(f"delta {delta.delta_id} cites a stale source hash")
        provenance_pairs = {(ref.artefact_id, ref.content_hash) for ref in delta.provenance}
        if not all(_member_key(member) in provenance_pairs for member in delta.source_members):
            raise MemoryValidationError(
                f"delta {delta.delta_id} provenance does not cover all source members"
            )
        if delta.operation == "summary":
            if not delta.target_record_type:
                raise MemoryValidationError(f"summary delta {delta.delta_id} needs a record type")
            if delta.target_record_id in record_ids:
                raise MemoryValidationError(
                    f"summary delta {delta.delta_id} must target a new record ID"
                )
        elif delta.target_record_id not in record_ids:
            raise MemoryValidationError(
                f"delta {delta.delta_id} targets a record outside the input snapshot"
            )
        return delta

    @staticmethod
    def _deterministic_proposals(
        records: tuple[StoredRecord, ...],
    ) -> Iterable[MaintenanceDelta]:
        """Yield only extractive, evidence-linked maintenance proposals.

        Duplicate detection removes identity/timestamp fields from JSON before
        hashing. The earliest record (then the lowest ID) is the stable
        representative. Episode summaries copy a fixed field whitelist and
        never assert a new lesson or hypothesis.
        """

        groups: dict[tuple[str, str], list[StoredRecord]] = {}
        for record in records:
            semantic = hashlib.sha256(
                canonical_json(_semantic_payload(record)).encode("utf-8")
            ).hexdigest()
            groups.setdefault((record.record_type, semantic), []).append(record)
        for (record_type, semantic_hash), grouped in sorted(groups.items()):
            ordered = sorted(grouped, key=lambda record: (record.created_at, record.record_id))
            if len(ordered) > 1:
                representative = ordered[0]
                members = [
                    SnapshotMember(record_id=record.record_id, content_hash=record.content_hash)
                    for record in ordered
                ]
                provenance = _provenance(members)
                yield MaintenanceDelta(
                    delta_id=f"link-{semantic_hash}",
                    operation="link",
                    target_record_id=representative.record_id,
                    target_payload={
                        "semantic_hash": semantic_hash,
                        "duplicate_member_ids": [member.record_id for member in members],
                    },
                    source_members=members,
                    provenance=provenance,
                )
                for duplicate in ordered[1:]:
                    source_members = [
                        SnapshotMember(
                            record_id=representative.record_id,
                            content_hash=representative.content_hash,
                        ),
                        SnapshotMember(
                            record_id=duplicate.record_id,
                            content_hash=duplicate.content_hash,
                        ),
                    ]
                    yield MaintenanceDelta(
                        delta_id=f"supersede-{duplicate.record_id}",
                        operation="supersede",
                        target_record_id=duplicate.record_id,
                        target_payload={"superseded_by": representative.record_id},
                        source_members=source_members,
                        provenance=_provenance(source_members),
                    )
            if record_type in {"experience-transition", "episode"}:
                source = ordered[0]
                member = SnapshotMember(
                    record_id=source.record_id,
                    content_hash=source.content_hash,
                )
                provenance = _provenance([member])
                yield MaintenanceDelta(
                    delta_id=f"summary-{source.record_id}",
                    operation="summary",
                    target_record_id=(
                        "summary-" + hashlib.sha256(source.record_id.encode("utf-8")).hexdigest()
                    ),
                    target_record_type="memory-summary",
                    target_payload={
                        "status": "candidate",
                        "source_record_id": source.record_id,
                        "source_content_hash": source.content_hash,
                        "provenance": [
                            reference.model_dump(mode="json") for reference in provenance
                        ],
                        **{
                            field: _bounded_summary_value(source.payload[field])
                            for field in _SUMMARY_FIELDS
                            if field in source.payload
                        },
                    },
                    source_members=[member],
                    provenance=provenance,
                )

    def _protection(
        self,
        records: list[StoredRecord],
        active_holds: Iterable[RetentionHold],
    ) -> _Protection:
        by_record: dict[str, set[str]] = {}
        hold_ids: set[str] = set()
        for hold in active_holds:
            if not isinstance(hold, RetentionHold):
                raise MemoryValidationError("active_holds must contain RetentionHold values")
            validate_identifier(hold.hold_id, name="hold_id", max_length=256)
            if hold.hold_id in hold_ids:
                raise MemoryValidationError(f"duplicate active retention hold {hold.hold_id}")
            hold_ids.add(hold.hold_id)
            for record_id in hold.artefact_ids:
                validate_identifier(record_id, name="hold artefact_id", max_length=256)
                by_record.setdefault(record_id, set()).add(f"hold:{hold.hold_id}")
        snapshot_ids = {record.record_id for record in records}
        for record in records:
            if record.payload.get("status") not in {"candidate", "active"}:
                continue
            by_record.setdefault(record.record_id, set()).add(f"candidate:{record.record_id}")
            provenance = record.payload.get("provenance")
            if isinstance(provenance, list):
                for reference in provenance:
                    if isinstance(reference, dict):
                        source_id = reference.get("artefact_id")
                        if isinstance(source_id, str) and source_id in snapshot_ids:
                            by_record.setdefault(source_id, set()).add(
                                f"candidate:{record.record_id}"
                            )
        unresolved: set[str] = set()
        for record in records:
            if record.payload.get("status") not in {"candidate", "active"}:
                continue
            provenance = record.payload.get("provenance")
            if isinstance(provenance, list):
                for reference in provenance:
                    if isinstance(reference, dict):
                        source_id = reference.get("artefact_id")
                        if isinstance(source_id, str) and source_id not in snapshot_ids:
                            unresolved.add(record.record_id)
        return _Protection(
            {record_id: tuple(sorted(reasons)) for record_id, reasons in by_record.items()},
            tuple(sorted(unresolved)),
        )

    def _retention_entry(
        self,
        record: StoredRecord,
        protection: _Protection,
        *,
        plan_created_at: datetime,
    ) -> RetentionEntry:
        if record.created_at.utcoffset() is None:
            raise MemoryValidationError("source record timestamp must include a timezone")
        explicit_lifetime = (
            record.payload.get("retention_class") == "project_lifetime"
            and record.payload.get("retention_policy_ref") == _AUDIT_RETENTION_POLICY_REF
        )
        if record.record_type in self._retention.project_lifetime_record_types or explicit_lifetime:
            retention_class = "project_lifetime"
            retained_until = None
        else:
            retention_class = "raw"
            retained_until = max(record.created_at.astimezone(UTC), plan_created_at) + timedelta(
                days=self._retention.raw_minimum_days
            )
        return RetentionEntry(
            record_id=record.record_id,
            content_hash=record.content_hash,
            retention_class=retention_class,
            retained_until=retained_until,
            protected_by=list(protection.by_record.get(record.record_id, ())),
        )

    @staticmethod
    def _owned_plan(plan: MaintenancePlan) -> MaintenancePlan:
        if not isinstance(plan, MaintenancePlan):
            raise MemoryValidationError("maintenance apply requires a MaintenancePlan")
        try:
            owned = MaintenancePlan.model_validate(plan.model_dump(mode="json"))
        except (TypeError, ValueError) as error:
            raise MemoryValidationError("maintenance plan is invalid") from error
        if _plan_hash(owned) != owned.plan_id:
            raise MemoryConflictError("maintenance plan content hash mismatch")
        return owned

    def _validate_plan_binding(self, plan: MaintenancePlan) -> None:
        if plan.namespace != self._namespace:
            raise MemoryConflictError("maintenance plan belongs to another namespace")
        if plan.maintenance_namespace != self._maintenance_namespace:
            raise MemoryConflictError("maintenance plan uses another archive namespace")
        if plan.retention_policy != self._retention:
            raise MemoryConflictError("maintenance plan uses another retention policy")

    def _result(
        self,
        plan: MaintenancePlan,
        application_id: str,
        *,
        already_applied: bool,
    ) -> MaintenanceApplyResult:
        operations = sorted({delta.operation for delta in plan.deltas})
        return MaintenanceApplyResult(
            plan_id=plan.plan_id,
            application_id=application_id,
            applied=True,
            already_applied=already_applied,
            delta_ids=[delta.delta_id for delta in plan.deltas],
            supported_operations=[
                operation for operation in operations if operation == "supersede"
            ],
            warnings=plan.warnings,
        )


class MaintenanceRetrievalView:
    """Apply durable supersession and age decay to operational candidates."""

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        namespace: str,
        maintenance_namespace: str | None = None,
        decay_days: float = 30.0,
        apply_decay: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._namespace = validate_namespace(namespace)
        derived = (
            f"{self._namespace}:maintenance"
            if maintenance_namespace is None
            else maintenance_namespace
        )
        self._maintenance_namespace = validate_namespace(derived)
        if self._namespace == self._maintenance_namespace:
            raise MemoryValidationError("maintenance namespace must be separate from source memory")
        if not math.isfinite(decay_days) or decay_days <= 0:
            raise MemoryValidationError("decay_days must be finite and positive")
        self._decay_days = decay_days
        self._apply_decay = apply_decay
        self._clock = clock or (lambda: datetime.now(UTC))

    async def rank(
        self, candidates: Iterable[ContextItem], request: MemoryContextRequest
    ) -> list[ContextItem]:
        """Async retrieval-strategy entry point for orchestrator composition."""

        if not isinstance(request, MemoryContextRequest):
            raise MemoryValidationError("maintenance retrieval requires MemoryContextRequest")
        return await self.transform(candidates)

    async def transform(self, candidates: Iterable[ContextItem]) -> list[ContextItem]:
        """Filter superseded IDs and decay scores, while preserving envelopes."""

        items = list(candidates)
        if not all(isinstance(item, ContextItem) for item in items):
            raise MemoryValidationError(
                "operational retrieval candidates must be ContextItem values"
            )
        source_records = await self._store.list(namespace=self._namespace)
        created_at = {}
        for record in source_records:
            owned = StoredRecord.validate_integrity(record)
            if owned.created_at.utcoffset() is None:
                raise MemoryValidationError("source record timestamp must include a timezone")
            created_at[owned.record_id] = owned.created_at.astimezone(UTC)
        superseded: set[str] = set()
        for record in await self._store.list(namespace=self._maintenance_namespace):
            owned = StoredRecord.validate_integrity(record)
            if owned.record_type != _MAINTENANCE_RECORD_TYPE:
                continue
            raw_plan = owned.payload.get("plan")
            try:
                plan = MaintenancePlan.model_validate(raw_plan)
            except (TypeError, ValueError, PydanticValidationError) as error:
                raise MemoryPermanentError("maintenance application has an invalid plan") from error
            if (
                plan.namespace != self._namespace
                or plan.maintenance_namespace != self._maintenance_namespace
                or _plan_hash(plan) != plan.plan_id
            ):
                raise MemoryPermanentError("maintenance application plan binding is invalid")
            for delta in plan.deltas:
                if delta.operation == "supersede":
                    superseded.add(delta.target_record_id)
        now = _now_utc(self._clock)
        visible: list[ContextItem] = []
        for item in items:
            if item.envelope.item_id in superseded:
                continue
            created = created_at.get(item.envelope.item_id)
            if created is None:
                visible.append(item)
                continue
            age_days = max(0.0, (now - created).total_seconds() / 86_400)
            factor = math.exp(-age_days / self._decay_days) if self._apply_decay else 1.0
            score = item.score * factor
            if not math.isfinite(score):
                raise MemoryValidationError("maintenance age-decayed score is not finite")
            reason = (
                f"{item.selection_reason}; maintenance age_decay={factor:.6g}"
                if self._apply_decay
                else f"{item.selection_reason}; maintenance supersession_view"
            )
            visible.append(
                item.model_copy(update={"score": score, "selection_reason": reason[:512]})
            )
        visible.sort(
            key=lambda item: (
                -item.score,
                item.envelope.item_id,
                item.envelope.origin_module,
                item.envelope.origin_version,
            )
        )
        return visible
