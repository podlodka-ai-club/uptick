import asyncio
import math
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from uptick_agent.memory.contracts import (
    MemoryConflictError,
    MemoryPermanentError,
    MemoryTransientError,
    MemoryValidationError,
)
from uptick_agent.memory.stores import (
    InMemoryStructuredStore,
    RecordWrite,
    SqliteStructuredStore,
    StoredRecord,
)
from uptick_agent.memory.stores.contracts import canonical_json


def _write(record_id: str, *, seconds: int = 0) -> RecordWrite:
    return RecordWrite(
        namespace="experiment-1",
        record_id=record_id,
        record_type="generic-evidence",
        payload={"record": record_id},
        created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds),
    )


def _write_in_namespace(namespace: str, record_id: str, *, seconds: int = 0) -> RecordWrite:
    return _write(record_id, seconds=seconds).model_copy(update={"namespace": namespace})


@pytest.mark.parametrize("backend", ["in-memory", "sqlite"])
@pytest.mark.parametrize(
    ("method", "argument", "value"),
    [
        ("append", "operation", ""),
        ("append", "operation", "x" * 129),
        ("append", "operation", 42),
        ("append", "idempotency_key", ""),
        ("append", "idempotency_key", "x" * 257),
        ("append", "idempotency_key", 42),
        ("append", "write", _write("candidate").model_copy(update={"record_id": ""})),
        ("append", "write", _write("candidate").model_copy(update={"record_id": 42})),
        ("append", "write", _write("candidate").model_copy(update={"namespace": "x" * 257})),
        ("append", "write", _write("candidate").model_copy(update={"payload": {"values": {1, 2}}})),
        (
            "append",
            "write",
            _write("candidate").model_copy(update={"payload": {"value": math.nan}}),
        ),
        ("create_snapshot", "namespace", ""),
        ("create_snapshot", "namespace", "x" * 257),
        ("create_snapshot", "namespace", 42),
        ("create_snapshot", "snapshot_id", ""),
        ("create_snapshot", "snapshot_id", "x" * 257),
        ("create_snapshot", "snapshot_id", 42),
        ("create_snapshot", "operation", ""),
        ("create_snapshot", "operation", "x" * 129),
        ("create_snapshot", "operation", 42),
        ("create_snapshot", "idempotency_key", ""),
        ("create_snapshot", "idempotency_key", "x" * 257),
        ("create_snapshot", "idempotency_key", 42),
        ("get", "namespace", ""),
        ("get", "record_id", "x" * 257),
        ("get", "record_id", 42),
        ("list", "namespace", 42),
        ("get_snapshot", "snapshot_id", ""),
        ("get_snapshot", "snapshot_id", "x" * 257),
        ("get_snapshot", "snapshot_id", 42),
    ],
)
def test_store_rejects_invalid_public_inputs_without_mutation(
    backend: str, method: str, argument: str, value: object, tmp_path
) -> None:
    async def scenario() -> None:
        store = (
            InMemoryStructuredStore()
            if backend == "in-memory"
            else SqliteStructuredStore(tmp_path / "memory.sqlite")
        )
        await store.append(_write("existing"), operation="append", idempotency_key="existing-key")

        if method == "append":
            write = value if argument == "write" else _write("candidate")
            operation = value if argument == "operation" else "append"
            idempotency_key = value if argument == "idempotency_key" else "candidate-key"
            call = store.append(write, operation=operation, idempotency_key=idempotency_key)
        elif method == "create_snapshot":
            namespace = value if argument == "namespace" else "experiment-1"
            snapshot_id = value if argument == "snapshot_id" else "candidate-snapshot"
            operation = value if argument == "operation" else "freeze"
            idempotency_key = value if argument == "idempotency_key" else "candidate-key"
            call = store.create_snapshot(
                namespace=namespace,
                snapshot_id=snapshot_id,
                operation=operation,
                idempotency_key=idempotency_key,
            )
        elif method == "get":
            namespace = value if argument == "namespace" else "experiment-1"
            record_id = value if argument == "record_id" else "existing"
            call = store.get(namespace=namespace, record_id=record_id)
        elif method == "list":
            call = store.list(namespace=value)
        else:
            call = store.get_snapshot(snapshot_id=value)

        with pytest.raises(MemoryValidationError):
            await call
        assert [record.record_id for record in await store.list(namespace="experiment-1")] == [
            "existing"
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["in-memory", "sqlite"])
def test_structured_store_contract_is_identical(backend: str, tmp_path) -> None:
    async def scenario() -> None:
        store = (
            InMemoryStructuredStore()
            if backend == "in-memory"
            else SqliteStructuredStore(tmp_path / "memory.sqlite")
        )
        first = await store.append(
            _write("b", seconds=1), operation="append", idempotency_key="key-1"
        )
        replay = await store.append(
            _write("b", seconds=1), operation="append", idempotency_key="key-1"
        )
        await store.append(_write("a"), operation="append", idempotency_key="key-2")

        assert replay == first
        assert (await store.get(namespace="experiment-1", record_id="b")) == first.record
        assert [record.record_id for record in await store.list(namespace="experiment-1")] == [
            "a",
            "b",
        ]

        snapshot = await store.create_snapshot(
            namespace="experiment-1",
            snapshot_id="snapshot-1",
            operation="freeze",
            idempotency_key="key-3",
        )
        await store.append(_write("c", seconds=2), operation="append", idempotency_key="key-4")
        loaded = await store.get_snapshot(snapshot_id="snapshot-1")

        assert loaded == snapshot.snapshot
        assert [member.record_id for member in loaded.members] == ["a", "b"]
        assert (
            await store.create_snapshot(
                namespace="experiment-1",
                snapshot_id="snapshot-1",
                operation="freeze",
                idempotency_key="key-3",
            )
        ) == snapshot

        with pytest.raises(MemoryConflictError, match="different input"):
            await store.append(_write("different"), operation="append", idempotency_key="key-1")

        other_namespace = await store.append(
            _write_in_namespace("experiment-2", "b", seconds=1),
            operation="append",
            idempotency_key="key-1",
        )
        assert other_namespace.record.namespace == "experiment-2"

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["in-memory", "sqlite"])
def test_structured_store_defensively_owns_records_and_snapshots(backend: str, tmp_path) -> None:
    async def scenario() -> None:
        store = (
            InMemoryStructuredStore()
            if backend == "in-memory"
            else SqliteStructuredStore(tmp_path / "memory.sqlite")
        )
        write = _write("record").model_copy(update={"payload": {"nested": {"value": "original"}}})
        receipt = await store.append(write, operation="append", idempotency_key="record-key")
        write.payload["nested"]["value"] = "caller-mutated"
        receipt.record.payload["nested"]["value"] = "receipt-mutated"

        loaded = await store.get(namespace="experiment-1", record_id="record")
        assert loaded.payload == {"nested": {"value": "original"}}
        loaded.payload["nested"]["value"] = "read-mutated"
        assert (await store.list(namespace="experiment-1"))[0].payload == {
            "nested": {"value": "original"}
        }

        frozen = await store.create_snapshot(
            namespace="experiment-1",
            snapshot_id="snapshot",
            operation="freeze",
            idempotency_key="snapshot-key",
        )
        frozen.snapshot.members.clear()
        replay = await store.create_snapshot(
            namespace="experiment-1",
            snapshot_id="snapshot",
            operation="freeze",
            idempotency_key="snapshot-key",
        )
        replay.snapshot.members.clear()
        assert len((await store.get_snapshot(snapshot_id="snapshot")).members) == 1

    asyncio.run(scenario())


def test_record_content_hash_includes_schema_version() -> None:
    first = _write("record")
    forward_minor = first.model_copy(update={"schema_version": "1.1"})

    assert (
        StoredRecord.from_write(first).content_hash
        != StoredRecord.from_write(forward_minor).content_hash
    )


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_store_contract_rejects_non_finite_payloads_and_canonical_json(invalid: float) -> None:
    with pytest.raises(ValidationError, match="NaN or infinity"):
        RecordWrite(
            namespace="experiment-1",
            record_id="record",
            record_type="generic-evidence",
            payload={"nested": [invalid]},
        )
    with pytest.raises(ValueError):
        canonical_json({"nested": [invalid]})


def test_sqlite_cross_instance_idempotency_and_busy_mapping(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "memory.sqlite"
        first_store = SqliteStructuredStore(path)
        second_store = SqliteStructuredStore(path)
        write = _write("record")

        first, second = await asyncio.gather(
            first_store.append(write, operation="append", idempotency_key="same-key"),
            second_store.append(write, operation="append", idempotency_key="same-key"),
        )
        assert first == second

        reopened = SqliteStructuredStore(path)
        assert await reopened.append(write, operation="append", idempotency_key="same-key") == first
        with pytest.raises(MemoryConflictError, match="different input"):
            await reopened.append(_write("other"), operation="append", idempotency_key="same-key")

        lock_connection = sqlite3.connect(path, isolation_level=None)
        try:
            lock_connection.execute("BEGIN EXCLUSIVE")
            blocked = SqliteStructuredStore(path)
            with pytest.raises(MemoryTransientError, match="locked or busy"):
                await blocked.append(
                    _write("blocked"), operation="append", idempotency_key="blocked"
                )
        finally:
            lock_connection.rollback()
            lock_connection.close()

    asyncio.run(scenario())


def test_sqlite_fresh_initialization_is_cross_instance_safe(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "fresh.sqlite"
        first = SqliteStructuredStore(path)
        second = SqliteStructuredStore(path)

        assert await asyncio.gather(
            first.list(namespace="experiment-1"), second.list(namespace="experiment-1")
        ) == [[], []]

        with sqlite3.connect(path) as connection:
            rows = connection.execute("SELECT singleton, version FROM memory_schema").fetchall()
        assert rows == [(1, 3)]

    asyncio.run(scenario())


def test_sqlite_filesystem_os_error_is_permanent(monkeypatch, tmp_path) -> None:
    def failing_mkdir(*args, **kwargs) -> None:
        raise PermissionError("deterministic test failure")

    monkeypatch.setattr("uptick_agent.memory.stores.sqlite.Path.mkdir", failing_mkdir)

    async def scenario() -> None:
        store = SqliteStructuredStore(tmp_path / "cannot-create" / "memory.sqlite")
        with pytest.raises(MemoryPermanentError, match="filesystem failure"):
            await store.append(_write("record"), operation="append", idempotency_key="key")

    asyncio.run(scenario())


def test_sqlite_store_reopens_with_the_same_records_and_snapshots(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "memory.sqlite"
        first = SqliteStructuredStore(path)
        await first.append(_write("record"), operation="append", idempotency_key="key")
        frozen = await first.create_snapshot(
            namespace="experiment-1",
            snapshot_id="snapshot",
            operation="freeze",
            idempotency_key="freeze-key",
        )

        reopened = SqliteStructuredStore(path)
        assert (
            await reopened.get(namespace="experiment-1", record_id="record")
        ).record_id == "record"
        assert await reopened.get_snapshot(snapshot_id="snapshot") == frozen.snapshot

    asyncio.run(scenario())
