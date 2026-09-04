"""Reference implementation of the generic structured-store contract."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from uptick_agent.memory.contracts import MemoryConflictError
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    SnapshotReceipt,
    StoredRecord,
    WriteReceipt,
    sha256_json,
    validate_append_call,
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
        self._receipts: dict[tuple[str, str, str], WriteReceipt | SnapshotReceipt] = {}
        self._receipt_hashes: dict[tuple[str, str, str], str] = {}
        self._snapshots: dict[str, MemorySnapshot] = {}
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
