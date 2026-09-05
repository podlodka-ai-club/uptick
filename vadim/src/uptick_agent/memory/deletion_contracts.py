"""Neutral contracts for the physical deletion store extension."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from uptick_agent.memory.contracts import ContractModel, MemoryPermanentError
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    SnapshotMember,
    SnapshotReceipt,
    StoredRecord,
    WriteReceipt,
    sha256_json,
)

DELETION_TOMBSTONE_RECORD_TYPE = "memory-deletion-tombstone"
DELETED_RECEIPT_KIND = "deleted"
DEFAULT_TOMBSTONE_NAMESPACE = "memory:deletion:tombstones"
DELETION_OPERATION = "physical-delete-v1"
_HEX = r"^[0-9a-f]{64}$"

_LIFETIME_RECORD_TYPES = frozenset(
    {
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
        "consolidation-plan",
        "consolidation-apply",
        "retention-authorization",
        "retention-hold",
        "run-outcome",
        DELETION_TOMBSTONE_RECORD_TYPE,
    }
)


class RecordRef(ContractModel):
    """A content-addressed record reference, including its namespace."""

    namespace: str = Field(min_length=1, max_length=256)
    record_id: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(min_length=64, max_length=64, pattern=_HEX)


class SnapshotRef(ContractModel):
    """Immutable snapshot identity and membership metadata for retirement."""

    namespace: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    created_at: datetime
    content_hash: str = Field(min_length=64, max_length=64, pattern=_HEX)
    members: list[SnapshotMember] = Field(default_factory=list, max_length=100_000)

    @field_validator("created_at")
    @classmethod
    def _aware_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("snapshot reference timestamp must be timezone-aware")
        return value


class DeletionAuthorization(ContractModel):
    """Owner approval bound to the exact policy and snapshot scope.

    Snapshot retirement additionally relies on an operator attestation that
    active external bindings were checked outside this store.
    """

    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    scope: Literal["physical-delete"] = "physical-delete"
    minimum_raw_age_days: int = Field(ge=90)
    deletable_record_types: list[str] = Field(min_length=1, max_length=128)
    retire_snapshot_refs: list[SnapshotRef] = Field(default_factory=list, max_length=10_000)
    no_active_external_bindings_attested: bool = False

    @model_validator(mode="after")
    def _unique_types(self) -> DeletionAuthorization:
        if len(set(self.deletable_record_types)) != len(self.deletable_record_types):
            raise ValueError("authorization deletable record types must be unique")
        snapshot_keys = [(ref.namespace, ref.snapshot_id) for ref in self.retire_snapshot_refs]
        if len(snapshot_keys) != len(set(snapshot_keys)):
            raise ValueError("authorization repeats a snapshot reference")
        if self.retire_snapshot_refs and not self.no_active_external_bindings_attested:
            raise ValueError(
                "snapshot retirement requires an explicit no-active-external-bindings attestation"
            )
        return self


class DeletionHold(ContractModel):
    """Durable hold payload stored as a ``retention-hold`` record."""

    hold_id: str = Field(min_length=1, max_length=256)
    target_refs: list[RecordRef] = Field(default_factory=list, max_length=10_000)
    snapshot_refs: list[SnapshotRef] = Field(default_factory=list, max_length=10_000)
    reason: str = Field(min_length=1, max_length=512)
    active_until: datetime | None = None

    @model_validator(mode="after")
    def _validate_targets(self) -> DeletionHold:
        keys = [(ref.namespace, ref.record_id, ref.content_hash) for ref in self.target_refs]
        if len(keys) != len(set(keys)):
            raise ValueError("retention hold repeats a target reference")
        snapshot_keys = [(ref.namespace, ref.snapshot_id) for ref in self.snapshot_refs]
        if len(snapshot_keys) != len(set(snapshot_keys)):
            raise ValueError("retention hold repeats a snapshot reference")
        if not self.target_refs and not self.snapshot_refs:
            raise ValueError("retention hold must reference a record or snapshot")
        if self.active_until is not None and self.active_until.utcoffset() is None:
            raise ValueError("retention hold active_until must be timezone-aware")
        return self

    def is_active(self, now: datetime) -> bool:
        return self.active_until is None or self.active_until.astimezone(UTC) > now


class DeletionPolicy(ContractModel):
    """Versioned policy and owner authorization for physical deletion."""

    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    scope: Literal["physical-delete"] = "physical-delete"
    minimum_raw_age_days: int = Field(default=90, ge=90)
    deletable_record_types: list[str] = Field(
        default_factory=lambda: ["experience-transition", "episode", "generic-evidence"],
        min_length=1,
        max_length=128,
    )
    project_lifetime_record_types: list[str] = Field(
        default_factory=lambda: sorted(_LIFETIME_RECORD_TYPES),
        min_length=1,
        max_length=256,
    )
    authorization: RecordRef

    @model_validator(mode="after")
    def _validate_policy(self) -> DeletionPolicy:
        if len(set(self.deletable_record_types)) != len(self.deletable_record_types):
            raise ValueError("deletable record types must be unique")
        if len(set(self.project_lifetime_record_types)) != len(self.project_lifetime_record_types):
            raise ValueError("project lifetime record types must be unique")
        missing = _LIFETIME_RECORD_TYPES - set(self.project_lifetime_record_types)
        if missing:
            raise ValueError(
                "deletion policy cannot omit protected record types: " + ", ".join(sorted(missing))
            )
        overlap = set(self.deletable_record_types) & set(self.project_lifetime_record_types)
        if overlap:
            raise ValueError("deletable record types overlap project lifetime protection")
        return self


class DeletionCandidate(ContractModel):
    ref: RecordRef
    record_type: str = Field(min_length=1, max_length=128)
    eligible_at: datetime
    reason: str = Field(min_length=1, max_length=512)

    @field_validator("eligible_at")
    @classmethod
    def _aware_eligibility(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("deletion eligibility timestamp must be timezone-aware")
        return value


class DeletionBlock(ContractModel):
    ref: RecordRef
    reasons: list[str] = Field(min_length=1, max_length=32)


class SnapshotDeletionBlock(ContractModel):
    snapshot: SnapshotRef
    reasons: list[str] = Field(min_length=1, max_length=32)


class ReceiptEntry(ContractModel):
    """Receipt identity without retaining the receipt's raw payload."""

    namespace: str = Field(min_length=1, max_length=256)
    operation: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    input_hash: str = Field(min_length=64, max_length=64, pattern=_HEX)
    receipt_kind: Literal["write", "snapshot", "deleted", "deleted_snapshot"]
    record_ref: RecordRef | None = None
    snapshot_ref: SnapshotRef | None = None

    @model_validator(mode="after")
    def _reference_matches_kind(self) -> ReceiptEntry:
        if self.receipt_kind in {"write", "deleted"}:
            if self.record_ref is None or self.snapshot_ref is not None:
                raise ValueError("record receipt must contain only a record reference")
        elif self.receipt_kind in {"snapshot", "deleted_snapshot"} and (
            self.snapshot_ref is None or self.record_ref is not None
        ):
            raise ValueError("snapshot receipt must contain only a snapshot reference")
        return self


class DeletedReceipt(ContractModel):
    """Replacement for a write receipt after its source row is deleted."""

    operation: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    input_hash: str = Field(min_length=64, max_length=64, pattern=_HEX)
    deleted_record: RecordRef
    deleted_at: datetime
    plan_id: str = Field(min_length=64, max_length=64, pattern=_HEX)

    @field_validator("deleted_at")
    @classmethod
    def _aware_deleted_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("deleted receipt timestamp must be timezone-aware")
        return value


class DeletedSnapshotReceipt(ContractModel):
    """Replacement for a snapshot receipt after snapshot retirement."""

    operation: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    input_hash: str = Field(min_length=64, max_length=64, pattern=_HEX)
    deleted_snapshot: SnapshotRef
    deleted_at: datetime
    plan_id: str = Field(min_length=64, max_length=64, pattern=_HEX)

    @field_validator("deleted_at")
    @classmethod
    def _aware_deleted_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("deleted snapshot receipt timestamp must be timezone-aware")
        return value


class SnapshotRetirement(ContractModel):
    """One explicitly owner-attested, expired snapshot retirement."""

    snapshot: SnapshotRef
    eligible_at: datetime
    reason: str = Field(min_length=1, max_length=512)

    @field_validator("eligible_at")
    @classmethod
    def _aware_eligibility(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("snapshot retirement eligibility must be timezone-aware")
        return value


class DeletionTombstone(ContractModel):
    """Exact metadata payload retained after a raw row is physically deleted."""

    target: RecordRef | None = None
    snapshot: SnapshotRef | None = None
    deleted_at: datetime
    plan_id: str = Field(min_length=64, max_length=64, pattern=_HEX)
    reason: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _one_target(self) -> DeletionTombstone:
        if (self.target is None) == (self.snapshot is None):
            raise ValueError("deletion tombstone must target exactly one record or snapshot")
        if self.deleted_at.utcoffset() is None:
            raise ValueError("deletion tombstone timestamp must be timezone-aware")
        return self


class PhysicalDeletionPlan(ContractModel):
    """Immutable dry-run output; ``plan_id`` hashes every other field."""

    plan_id: str = Field(min_length=64, max_length=64, pattern=_HEX)
    request_id: str = Field(min_length=1, max_length=256)
    created_at: datetime
    as_of: datetime
    inventory_hash: str = Field(min_length=64, max_length=64, pattern=_HEX)
    policy: DeletionPolicy
    candidates: list[DeletionCandidate] = Field(default_factory=list, max_length=100_000)
    blocked: list[DeletionBlock] = Field(default_factory=list, max_length=100_000)
    blocked_snapshots: list[SnapshotDeletionBlock] = Field(default_factory=list, max_length=10_000)
    purge_receipts: list[ReceiptEntry] = Field(default_factory=list, max_length=100_000)
    retired_snapshots: list[SnapshotRetirement] = Field(default_factory=list, max_length=10_000)
    tombstone_namespace: str = Field(min_length=1, max_length=256)
    warnings: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def _aware_times(self) -> PhysicalDeletionPlan:
        if self.created_at.utcoffset() is None or self.as_of.utcoffset() is None:
            raise ValueError("deletion plan timestamps must be timezone-aware")
        if self.as_of < self.created_at:
            raise ValueError("deletion plan as_of cannot precede created_at")
        ids = [(item.ref.namespace, item.ref.record_id) for item in self.candidates]
        blocked = [(item.ref.namespace, item.ref.record_id) for item in self.blocked]
        if len(ids) != len(set(ids)) or set(ids) & set(blocked):
            raise ValueError("deletion plan has duplicate or both candidate and blocked records")
        snapshot_ids = [
            (item.snapshot.namespace, item.snapshot.snapshot_id) for item in self.retired_snapshots
        ]
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("deletion plan repeats a snapshot retirement")
        blocked_snapshot_ids = [
            (item.snapshot.namespace, item.snapshot.snapshot_id) for item in self.blocked_snapshots
        ]
        if len(blocked_snapshot_ids) != len(set(blocked_snapshot_ids)):
            raise ValueError("deletion plan repeats a blocked snapshot")
        if set(snapshot_ids) & set(blocked_snapshot_ids):
            raise ValueError("deletion plan both retires and blocks a snapshot")
        return self


class PhysicalDeletionReceipt(ContractModel):
    plan_id: str = Field(min_length=64, max_length=64, pattern=_HEX)
    idempotency_key: str = Field(min_length=1, max_length=256)
    applied: bool
    already_applied: bool
    deleted_records: list[RecordRef] = Field(default_factory=list)
    tombstone_ids: list[str] = Field(default_factory=list)
    purged_receipts: list[ReceiptEntry] = Field(default_factory=list)
    retired_snapshots: list[SnapshotRef] = Field(default_factory=list)
    snapshot_tombstone_ids: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class StoreInventory:
    records: tuple[StoredRecord, ...]
    snapshots: tuple[MemorySnapshot, ...]
    receipts: tuple[ReceiptEntry, ...]
    deleted_records: tuple[RecordRef, ...]
    content_hash: str
    deleted_snapshots: tuple[SnapshotRef, ...] = ()


def make_inventory(
    records: Iterable[StoredRecord],
    snapshots: Iterable[MemorySnapshot],
    receipts: Iterable[ReceiptEntry],
    deleted_records: Iterable[RecordRef] = (),
    deleted_snapshots: Iterable[SnapshotRef] = (),
) -> StoreInventory:
    owned_records = tuple(
        sorted(
            (StoredRecord.validate_integrity(record) for record in records),
            key=lambda value: (value.namespace, value.created_at, value.record_id),
        )
    )
    owned_snapshots = tuple(
        sorted(
            (MemorySnapshot.validate_integrity(snapshot) for snapshot in snapshots),
            key=lambda value: value.snapshot_id,
        )
    )
    owned_receipts = tuple(
        sorted(
            receipts, key=lambda value: (value.namespace, value.operation, value.idempotency_key)
        )
    )
    owned_deleted = tuple(
        sorted(deleted_records, key=lambda value: (value.namespace, value.record_id))
    )
    owned_deleted_snapshots = tuple(
        sorted(
            deleted_snapshots,
            key=lambda value: (value.namespace, value.snapshot_id),
        )
    )
    fingerprint = sha256_json(
        {
            "records": [record.model_dump(mode="json") for record in owned_records],
            "snapshots": [snapshot.model_dump(mode="json") for snapshot in owned_snapshots],
            "receipts": [receipt.model_dump(mode="json") for receipt in owned_receipts],
            "deleted_records": [ref.model_dump(mode="json") for ref in owned_deleted],
            "deleted_snapshots": [ref.model_dump(mode="json") for ref in owned_deleted_snapshots],
        }
    )
    return StoreInventory(
        records=owned_records,
        snapshots=owned_snapshots,
        receipts=owned_receipts,
        deleted_records=owned_deleted,
        content_hash=fingerprint,
        deleted_snapshots=owned_deleted_snapshots,
    )


def _record_ref(record: StoredRecord) -> RecordRef:
    return RecordRef(
        namespace=record.namespace,
        record_id=record.record_id,
        content_hash=record.content_hash,
    )


def receipt_entry(
    *,
    namespace: str,
    operation: str,
    idempotency_key: str,
    receipt: WriteReceipt | SnapshotReceipt | DeletedReceipt | DeletedSnapshotReceipt,
) -> ReceiptEntry:
    """Project a store receipt to identity metadata without copying payloads."""

    if isinstance(receipt, WriteReceipt):
        record = StoredRecord.validate_integrity(receipt.record)
        return ReceiptEntry(
            namespace=namespace,
            operation=operation,
            idempotency_key=idempotency_key,
            input_hash=receipt.input_hash,
            receipt_kind="write",
            record_ref=_record_ref(record),
        )
    if isinstance(receipt, DeletedReceipt):
        return ReceiptEntry(
            namespace=namespace,
            operation=operation,
            idempotency_key=idempotency_key,
            input_hash=receipt.input_hash,
            receipt_kind="deleted",
            record_ref=receipt.deleted_record,
        )
    if isinstance(receipt, DeletedSnapshotReceipt):
        return ReceiptEntry(
            namespace=namespace,
            operation=operation,
            idempotency_key=idempotency_key,
            input_hash=receipt.input_hash,
            receipt_kind="deleted_snapshot",
            snapshot_ref=receipt.deleted_snapshot,
        )
    if isinstance(receipt, SnapshotReceipt):
        return ReceiptEntry(
            namespace=namespace,
            operation=operation,
            idempotency_key=idempotency_key,
            input_hash=receipt.input_hash,
            receipt_kind="snapshot",
            snapshot_ref=SnapshotRef(
                namespace=receipt.snapshot.namespace,
                snapshot_id=receipt.snapshot.snapshot_id,
                created_at=receipt.snapshot.created_at,
                content_hash=receipt.snapshot.content_hash,
                members=receipt.snapshot.members,
            ),
        )
    raise MemoryPermanentError("store receipt has an unsupported kind")
