"""Reference implementation of the generic structured-store contract."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from pydantic import BaseModel

from uptick_agent.memory.contracts import MemoryConflictError
from uptick_agent.memory.deletion import (
    validate_deletion_plan,
    validate_plan_against_inventory,
)
from uptick_agent.memory.deletion_contracts import (
    DELETION_OPERATION,
    DELETION_TOMBSTONE_RECORD_TYPE,
    DeletedReceipt,
    DeletedSnapshotReceipt,
    PhysicalDeletionPlan,
    PhysicalDeletionReceipt,
    RecordRef,
    SnapshotRef,
    StoreInventory,
    make_inventory,
    receipt_entry,
)
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    SnapshotReceipt,
    StoredRecord,
    WriteReceipt,
    sha256_json,
    validate_append_call,
    validate_identifier,
    validate_namespace,
    validate_record_lookup,
    validate_snapshot_call,
    validate_snapshot_lookup,
)


def _copy_contract[ContractValue: BaseModel](value: ContractValue) -> ContractValue:
    """Round-trip mutable Pydantic containers at every ownership boundary."""

    return type(value).model_validate(value.model_dump(mode="json"))


class InMemoryStructuredStore:
    """Lock-protected reference store; snapshots hold immutable record hashes."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], StoredRecord] = {}
        self._receipts: dict[
            tuple[str, str, str],
            WriteReceipt | SnapshotReceipt | DeletedReceipt | DeletedSnapshotReceipt,
        ] = {}
        self._receipt_hashes: dict[tuple[str, str, str], str] = {}
        self._snapshots: dict[str, MemorySnapshot] = {}
        self._deletion_receipts: dict[str, PhysicalDeletionReceipt] = {}
        self._deletion_receipt_hashes: dict[str, str] = {}
        self._deleted_record_keys: dict[tuple[str, str], RecordRef] = {}
        self._deleted_snapshot_ids: dict[str, SnapshotRef] = {}
        self._lock = asyncio.Lock()

    async def append(
        self, write: RecordWrite, *, operation: str, idempotency_key: str
    ) -> WriteReceipt:
        owned_write, operation, idempotency_key = validate_append_call(
            write, operation=operation, idempotency_key=idempotency_key
        )
        input_hash = sha256_json(
            {"operation": operation, "write": owned_write.model_dump(mode="json")}
        )
        receipt_key = (owned_write.namespace, operation, idempotency_key)
        async with self._lock:
            previous = self._receipts.get(receipt_key)
            if previous is not None:
                if self._receipt_hashes[receipt_key] != input_hash:
                    raise MemoryConflictError("idempotency key was reused with different input")
                if not isinstance(previous, WriteReceipt):
                    raise MemoryConflictError(
                        "idempotency key was reused for another operation type"
                    )
                return _copy_contract(previous)
            record = StoredRecord.from_write(owned_write)
            key = (record.namespace, record.record_id)
            if key in self._deleted_record_keys:
                raise MemoryConflictError("record was physically deleted and cannot be recreated")
            if key in self._records:
                raise MemoryConflictError("record_id already exists in namespace")
            receipt = WriteReceipt(
                operation=operation,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                record=record,
            )
            self._records[key] = _copy_contract(record)
            self._receipts[receipt_key] = _copy_contract(receipt)
            self._receipt_hashes[receipt_key] = input_hash
            return _copy_contract(receipt)

    async def get(self, *, namespace: str, record_id: str) -> StoredRecord | None:
        namespace, record_id = validate_record_lookup(namespace=namespace, record_id=record_id)
        async with self._lock:
            record = self._records.get((namespace, record_id))
            if record is None:
                return None
            return _copy_contract(StoredRecord.validate_integrity(record))

    async def list(self, *, namespace: str) -> list[StoredRecord]:
        namespace = validate_namespace(namespace)
        async with self._lock:
            records = [
                StoredRecord.validate_integrity(record)
                for record in self._records.values()
                if record.namespace == namespace
            ]
            records.sort(key=lambda record: (record.created_at, record.record_id))
            return [_copy_contract(record) for record in records]

    async def create_snapshot(
        self, *, namespace: str, snapshot_id: str, operation: str, idempotency_key: str
    ) -> SnapshotReceipt:
        namespace, snapshot_id, operation, idempotency_key = validate_snapshot_call(
            namespace=namespace,
            snapshot_id=snapshot_id,
            operation=operation,
            idempotency_key=idempotency_key,
        )
        input_hash = sha256_json(
            {"operation": operation, "namespace": namespace, "snapshot_id": snapshot_id}
        )
        receipt_key = (namespace, operation, idempotency_key)
        async with self._lock:
            previous = self._receipts.get(receipt_key)
            if previous is not None:
                if self._receipt_hashes[receipt_key] != input_hash:
                    raise MemoryConflictError("idempotency key was reused with different input")
                if not isinstance(previous, SnapshotReceipt):
                    raise MemoryConflictError(
                        "idempotency key was reused for another operation type"
                    )
                return _copy_contract(previous)
            if snapshot_id in self._deleted_snapshot_ids:
                raise MemoryConflictError("snapshot was retired and cannot be recreated")
            if snapshot_id in self._snapshots:
                raise MemoryConflictError("snapshot_id already exists")
            records = sorted(
                (record for record in self._records.values() if record.namespace == namespace),
                key=lambda record: (record.created_at, record.record_id),
            )
            snapshot = MemorySnapshot.create(
                snapshot_id=snapshot_id,
                namespace=namespace,
                members=[
                    SnapshotMember(record_id=record.record_id, content_hash=record.content_hash)
                    for record in records
                ],
            )
            receipt = SnapshotReceipt(
                operation=operation,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                snapshot=snapshot,
            )
            self._snapshots[snapshot_id] = _copy_contract(snapshot)
            self._receipts[receipt_key] = _copy_contract(receipt)
            self._receipt_hashes[receipt_key] = input_hash
            return _copy_contract(receipt)

    async def get_snapshot(self, *, snapshot_id: str) -> MemorySnapshot | None:
        snapshot_id = validate_snapshot_lookup(snapshot_id)
        async with self._lock:
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                return None
            return _copy_contract(MemorySnapshot.validate_integrity(snapshot))

    async def deletion_inventory(self) -> StoreInventory:
        """Return a complete defensive inventory for the deletion planner."""

        async with self._lock:
            records = [_copy_contract(record) for record in self._records.values()]
            snapshots = [_copy_contract(snapshot) for snapshot in self._snapshots.values()]
            receipts = [
                receipt_entry(
                    namespace=namespace,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    receipt=_copy_contract(receipt),
                )
                for (namespace, operation, idempotency_key), receipt in self._receipts.items()
            ]
            deleted = [_copy_contract(ref) for ref in self._deleted_record_keys.values()]
            deleted_snapshots = [_copy_contract(ref) for ref in self._deleted_snapshot_ids.values()]
            return make_inventory(records, snapshots, receipts, deleted, deleted_snapshots)

    async def apply_deletion(
        self, plan: PhysicalDeletionPlan, *, idempotency_key: str
    ) -> PhysicalDeletionReceipt:
        """Apply one sealed deletion plan atomically under the store lock."""

        if not isinstance(plan, PhysicalDeletionPlan):
            raise MemoryConflictError("physical deletion requires a deletion plan")
        plan = validate_deletion_plan(plan)
        idempotency_key = validate_identifier(
            idempotency_key, name="idempotency_key", max_length=256
        )
        input_hash = sha256_json({"operation": DELETION_OPERATION, "plan_id": plan.plan_id})
        async with self._lock:
            previous = self._deletion_receipts.get(idempotency_key)
            if previous is not None:
                if self._deletion_receipt_hashes[idempotency_key] != input_hash:
                    raise MemoryConflictError(
                        "deletion idempotency key was reused with different input"
                    )
                return _copy_contract(previous).model_copy(update={"already_applied": True})
            for previous in self._deletion_receipts.values():
                if previous.plan_id == plan.plan_id:
                    raise MemoryConflictError(
                        "deletion plan was already applied with another idempotency key"
                    )
            inventory = make_inventory(
                [_copy_contract(record) for record in self._records.values()],
                [_copy_contract(snapshot) for snapshot in self._snapshots.values()],
                [
                    receipt_entry(
                        namespace=namespace,
                        operation=operation,
                        idempotency_key=receipt_key,
                        receipt=_copy_contract(receipt),
                    )
                    for (namespace, operation, receipt_key), receipt in self._receipts.items()
                ],
                [_copy_contract(ref) for ref in self._deleted_record_keys.values()],
                [_copy_contract(ref) for ref in self._deleted_snapshot_ids.values()],
            )
            _records, receipt_entries = validate_plan_against_inventory(plan, inventory)
            applied_at = datetime.now(UTC)
            tombstones: list[str] = []
            snapshot_tombstones: list[str] = []
            tombstone_records: list[StoredRecord] = []
            for candidate in plan.candidates:
                tombstone_id = "tombstone-" + sha256_json(
                    {"plan_id": plan.plan_id, "record": candidate.ref.model_dump(mode="json")}
                )
                if (plan.tombstone_namespace, tombstone_id) in self._records:
                    raise MemoryConflictError("deletion tombstone ID already exists")
                tombstones.append(tombstone_id)
                tombstone_records.append(
                    StoredRecord.from_write(
                        RecordWrite(
                            namespace=plan.tombstone_namespace,
                            record_id=tombstone_id,
                            record_type=DELETION_TOMBSTONE_RECORD_TYPE,
                            payload={
                                "target": candidate.ref.model_dump(mode="json"),
                                "deleted_at": applied_at.isoformat(),
                                "plan_id": plan.plan_id,
                                "reason": candidate.reason,
                            },
                            created_at=applied_at,
                        )
                    )
                )
            for retirement in plan.retired_snapshots:
                key = (retirement.snapshot.namespace, retirement.snapshot.snapshot_id)
                current = self._snapshots.get(retirement.snapshot.snapshot_id)
                if current is None or current.namespace != retirement.snapshot.namespace:
                    raise MemoryConflictError(
                        f"deletion snapshot {retirement.snapshot.snapshot_id} vanished"
                    )
                if retirement.snapshot.snapshot_id in self._deleted_snapshot_ids:
                    raise MemoryConflictError("snapshot tombstone already exists")
                tombstone_id = "snapshot-tombstone-" + sha256_json(
                    {
                        "plan_id": plan.plan_id,
                        "snapshot": retirement.snapshot.model_dump(mode="json"),
                    }
                )
                if (plan.tombstone_namespace, tombstone_id) in self._records:
                    raise MemoryConflictError("deletion snapshot tombstone ID already exists")
                snapshot_tombstones.append(tombstone_id)
                tombstone_records.append(
                    StoredRecord.from_write(
                        RecordWrite(
                            namespace=plan.tombstone_namespace,
                            record_id=tombstone_id,
                            record_type=DELETION_TOMBSTONE_RECORD_TYPE,
                            payload={
                                "snapshot": retirement.snapshot.model_dump(mode="json"),
                                "deleted_at": applied_at.isoformat(),
                                "plan_id": plan.plan_id,
                                "reason": retirement.reason,
                            },
                            created_at=applied_at,
                        )
                    )
                )
            for receipt in receipt_entries:
                current_receipt = self._receipts.get(
                    (receipt.namespace, receipt.operation, receipt.idempotency_key)
                )
                expected_type = (
                    SnapshotReceipt if receipt.snapshot_ref is not None else WriteReceipt
                )
                if not isinstance(current_receipt, expected_type):
                    raise MemoryConflictError(
                        "deletion receipt purge target is not a write receipt"
                    )
            # All checks above happen before mutating any in-memory row.
            for candidate in plan.candidates:
                key = (candidate.ref.namespace, candidate.ref.record_id)
                del self._records[key]
                self._deleted_record_keys[key] = candidate.ref
            for retirement in plan.retired_snapshots:
                key = (retirement.snapshot.namespace, retirement.snapshot.snapshot_id)
                del self._snapshots[retirement.snapshot.snapshot_id]
                self._deleted_snapshot_ids[retirement.snapshot.snapshot_id] = retirement.snapshot
            for receipt in receipt_entries:
                key = (receipt.namespace, receipt.operation, receipt.idempotency_key)
                previous_receipt = self._receipts.get(key)
                if receipt.snapshot_ref is not None:
                    assert isinstance(previous_receipt, SnapshotReceipt)
                    self._receipts[key] = DeletedSnapshotReceipt(
                        operation=receipt.operation,
                        idempotency_key=receipt.idempotency_key,
                        input_hash=receipt.input_hash,
                        deleted_snapshot=receipt.snapshot_ref,
                        deleted_at=applied_at,
                        plan_id=plan.plan_id,
                    )
                else:
                    assert isinstance(previous_receipt, WriteReceipt)
                    self._receipts[key] = DeletedReceipt(
                        operation=receipt.operation,
                        idempotency_key=receipt.idempotency_key,
                        input_hash=receipt.input_hash,
                        deleted_record=receipt.record_ref,
                        deleted_at=applied_at,
                        plan_id=plan.plan_id,
                    )
            for tombstone in tombstone_records:
                self._records[(tombstone.namespace, tombstone.record_id)] = tombstone
            result = PhysicalDeletionReceipt(
                plan_id=plan.plan_id,
                idempotency_key=idempotency_key,
                applied=True,
                already_applied=False,
                deleted_records=[candidate.ref for candidate in plan.candidates],
                tombstone_ids=tombstones,
                purged_receipts=[
                    entry.model_copy(update={"receipt_kind": "deleted"})
                    if entry.record_ref is not None
                    else entry.model_copy(update={"receipt_kind": "deleted_snapshot"})
                    for entry in receipt_entries
                ],
                retired_snapshots=[item.snapshot for item in plan.retired_snapshots],
                snapshot_tombstone_ids=snapshot_tombstones,
            )
            self._deletion_receipts[idempotency_key] = _copy_contract(result)
            self._deletion_receipt_hashes[idempotency_key] = input_hash
            return _copy_contract(result)
