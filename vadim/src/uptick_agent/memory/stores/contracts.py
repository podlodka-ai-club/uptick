"""Generic persistence boundary for structured memory records and snapshots."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Protocol

from pydantic import Field, JsonValue, ValidationError, field_validator
from pydantic_core import PydanticSerializationError

from uptick_agent.memory.contracts import (
    ContractModel,
    MemoryValidationError,
    require_finite_json,
)

_NAMESPACE_MAX_LENGTH = 256
_RECORD_ID_MAX_LENGTH = 256
_RECORD_TYPE_MAX_LENGTH = 128
_OPERATION_MAX_LENGTH = 128
_IDEMPOTENCY_KEY_MAX_LENGTH = 256
_SNAPSHOT_ID_MAX_LENGTH = 256


def canonical_json(value: object) -> str:
    """Stable JSON used for content and input fingerprints."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class RecordWrite(ContractModel):
    namespace: str = Field(min_length=1, max_length=_NAMESPACE_MAX_LENGTH)
    record_id: str = Field(min_length=1, max_length=_RECORD_ID_MAX_LENGTH)
    record_type: str = Field(min_length=1, max_length=_RECORD_TYPE_MAX_LENGTH)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("payload", mode="before")
    @classmethod
    def _require_finite_payload(cls, value: object) -> object:
        return require_finite_json(value)


class StoredRecord(RecordWrite):
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_write(cls, write: RecordWrite) -> StoredRecord:
        body = write.model_dump(mode="json")
        return cls(**write.model_dump(), content_hash=sha256_json(body))


class WriteReceipt(ContractModel):
    operation: str = Field(min_length=1, max_length=_OPERATION_MAX_LENGTH)
    idempotency_key: str = Field(min_length=1, max_length=_IDEMPOTENCY_KEY_MAX_LENGTH)
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    record: StoredRecord


class SnapshotMember(ContractModel):
    record_id: str = Field(min_length=1, max_length=_RECORD_ID_MAX_LENGTH)
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class MemorySnapshot(ContractModel):
    snapshot_id: str = Field(min_length=1, max_length=_SNAPSHOT_ID_MAX_LENGTH)
    namespace: str = Field(min_length=1, max_length=_NAMESPACE_MAX_LENGTH)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    members: list[SnapshotMember] = Field(default_factory=list)
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def create(
        cls, *, snapshot_id: str, namespace: str, members: list[SnapshotMember]
    ) -> MemorySnapshot:
        body = {"namespace": namespace, "members": [member.model_dump() for member in members]}
        return cls(
            snapshot_id=snapshot_id,
            namespace=namespace,
            members=members,
            content_hash=sha256_json(body),
        )


class SnapshotReceipt(ContractModel):
    operation: str = Field(min_length=1, max_length=_OPERATION_MAX_LENGTH)
    idempotency_key: str = Field(min_length=1, max_length=_IDEMPOTENCY_KEY_MAX_LENGTH)
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    snapshot: MemorySnapshot


class StructuredMemoryStore(Protocol):
    async def append(
        self, write: RecordWrite, *, operation: str, idempotency_key: str
    ) -> WriteReceipt: ...

    async def get(self, *, namespace: str, record_id: str) -> StoredRecord | None: ...

    async def list(self, *, namespace: str) -> list[StoredRecord]: ...

    async def create_snapshot(
        self, *, namespace: str, snapshot_id: str, operation: str, idempotency_key: str
    ) -> SnapshotReceipt: ...

    async def get_snapshot(self, *, snapshot_id: str) -> MemorySnapshot | None: ...


def validate_identifier(value: object, *, name: str, max_length: int) -> str:
    """Validate a string identifier before it reaches a store implementation."""

    if not isinstance(value, str):
        raise MemoryValidationError(f"{name} must be a string")
    if not value:
        raise MemoryValidationError(f"{name} must not be empty")
    if len(value) > max_length:
        raise MemoryValidationError(f"{name} must be at most {max_length} characters")
    return value


def validate_record_write(value: object) -> RecordWrite:
    """Round-trip a write so model_copy-bypassed invalid values cannot cross the boundary."""

    if not isinstance(value, RecordWrite):
        raise MemoryValidationError("write must be a RecordWrite")
    try:
        serialized = value.model_dump(mode="python", round_trip=True, warnings="error")
        return RecordWrite.model_validate(serialized)
    except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
        raise MemoryValidationError("write contains invalid data") from error


def validate_append_call(
    write: object, *, operation: object, idempotency_key: object
) -> tuple[RecordWrite, str, str]:
    """Validate all caller-controlled inputs to append."""

    return (
        validate_record_write(write),
        validate_identifier(operation, name="operation", max_length=_OPERATION_MAX_LENGTH),
        validate_identifier(
            idempotency_key,
            name="idempotency_key",
            max_length=_IDEMPOTENCY_KEY_MAX_LENGTH,
        ),
    )


def validate_snapshot_call(
    *,
    namespace: object,
    snapshot_id: object,
    operation: object,
    idempotency_key: object,
) -> tuple[str, str, str, str]:
    """Validate all caller-controlled inputs to create_snapshot."""

    return (
        validate_identifier(namespace, name="namespace", max_length=_NAMESPACE_MAX_LENGTH),
        validate_identifier(snapshot_id, name="snapshot_id", max_length=_SNAPSHOT_ID_MAX_LENGTH),
        validate_identifier(operation, name="operation", max_length=_OPERATION_MAX_LENGTH),
        validate_identifier(
            idempotency_key,
            name="idempotency_key",
            max_length=_IDEMPOTENCY_KEY_MAX_LENGTH,
        ),
    )


def validate_record_lookup(*, namespace: object, record_id: object) -> tuple[str, str]:
    """Validate all caller-controlled inputs to get and list-record operations."""

    return (
        validate_identifier(namespace, name="namespace", max_length=_NAMESPACE_MAX_LENGTH),
        validate_identifier(record_id, name="record_id", max_length=_RECORD_ID_MAX_LENGTH),
    )


def validate_namespace(value: object) -> str:
    return validate_identifier(value, name="namespace", max_length=_NAMESPACE_MAX_LENGTH)


def validate_snapshot_lookup(value: object) -> str:
    return validate_identifier(value, name="snapshot_id", max_length=_SNAPSHOT_ID_MAX_LENGTH)
