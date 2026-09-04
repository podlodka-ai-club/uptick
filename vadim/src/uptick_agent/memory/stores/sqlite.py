"""SQLite-backed implementation of the Stage 1 structured-store contract."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError

from uptick_agent.memory.contracts import (
    MemoryConflictError,
    MemoryPermanentError,
    MemoryTransientError,
)
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    SnapshotReceipt,
    StoredRecord,
    WriteReceipt,
    canonical_json,
    sha256_json,
    validate_append_call,
    validate_namespace,
    validate_record_lookup,
    validate_snapshot_call,
    validate_snapshot_lookup,
)

_SCHEMA_VERSION = 3
_BUSY_TIMEOUT_MS = 100
DatabaseResult = TypeVar("DatabaseResult")


class SqliteStructuredStore:
    """Small SQLite store with transactional snapshots and durable receipts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize)
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=_BUSY_TIMEOUT_MS / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _map_database_error(
        error: Exception,
    ) -> MemoryConflictError | MemoryTransientError | MemoryPermanentError:
        if isinstance(error, sqlite3.IntegrityError):
            return MemoryConflictError("SQLite constraint conflict")
        if isinstance(error, sqlite3.OperationalError) and any(
            marker in str(error).casefold() for marker in ("locked", "busy")
        ):
            return MemoryTransientError("SQLite is locked or busy")
        return MemoryPermanentError("SQLite persistence failure")

    def _transaction(self, work: Callable[[sqlite3.Connection], DatabaseResult]) -> DatabaseResult:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            result = work(connection)
            connection.commit()
            return result
        except (MemoryConflictError, MemoryTransientError, MemoryPermanentError):
            if connection is not None:
                connection.rollback()
            raise
        except sqlite3.Error as error:
            if connection is not None:
                connection.rollback()
            raise self._map_database_error(error) from error
        except OSError as error:
            if connection is not None:
                connection.rollback()
            raise MemoryPermanentError("SQLite filesystem failure") from error
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
            if connection is not None:
                connection.rollback()
            raise MemoryPermanentError("SQLite stored data is invalid") from error
        finally:
            if connection is not None:
                connection.close()

    def _read(self, work: Callable[[sqlite3.Connection], DatabaseResult]) -> DatabaseResult:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            return work(connection)
        except (MemoryConflictError, MemoryTransientError, MemoryPermanentError):
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from error
        except OSError as error:
            raise MemoryPermanentError("SQLite filesystem failure") from error
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
            raise MemoryPermanentError("SQLite stored data is invalid") from error
        finally:
            if connection is not None:
                connection.close()

    def _initialize(self) -> None:
        def create_tables(connection: sqlite3.Connection) -> None:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_schema (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_records (
                    namespace TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    record_type TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (namespace, record_id)
                );
                CREATE TABLE IF NOT EXISTS memory_operation_receipts (
                    namespace TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    receipt_kind TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    PRIMARY KEY (namespace, operation, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS memory_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_snapshot_members (
                    snapshot_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    record_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, ordinal),
                    FOREIGN KEY (snapshot_id) REFERENCES memory_snapshots(snapshot_id)
                );
                """
            )

        # executescript() commits before its script. Run DDL in a separate
        # autocommit phase, then serialize version-row creation below.
        self._read(create_tables)

        def initialize_version(connection: sqlite3.Connection) -> None:
            current = connection.execute(
                "SELECT version FROM memory_schema WHERE singleton = 1"
            ).fetchone()
            if current is None:
                connection.execute(
                    "INSERT INTO memory_schema (singleton, version) VALUES (1, ?)",
                    (_SCHEMA_VERSION,),
                )
            elif current["version"] != _SCHEMA_VERSION:
                raise MemoryPermanentError(
                    "unsupported SQLite store schema "
                    f"{current['version']}; expected {_SCHEMA_VERSION}"
                )

        self._transaction(initialize_version)

    @staticmethod
    def _receipt_from_row(row: sqlite3.Row) -> WriteReceipt | SnapshotReceipt:
        if row["receipt_kind"] == "write":
            return WriteReceipt.model_validate_json(row["receipt_json"])
        if row["receipt_kind"] == "snapshot":
            return SnapshotReceipt.model_validate_json(row["receipt_json"])
        raise MemoryPermanentError("stored receipt has an unknown kind")

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> StoredRecord:
        return StoredRecord(
            namespace=row["namespace"],
            record_id=row["record_id"],
            record_type=row["record_type"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
            content_hash=row["content_hash"],
            schema_version=row["schema_version"],
        )

    async def append(
        self, write: RecordWrite, *, operation: str, idempotency_key: str
    ) -> WriteReceipt:
        write, operation, idempotency_key = validate_append_call(
            write, operation=operation, idempotency_key=idempotency_key
        )
        await self._ensure_initialized()
        input_hash = sha256_json({"operation": operation, "write": write.model_dump(mode="json")})
        async with self._lock:
            return await asyncio.to_thread(
                self._append, write, operation, idempotency_key, input_hash
            )

    def _append(
        self, write: RecordWrite, operation: str, idempotency_key: str, input_hash: str
    ) -> WriteReceipt:
        def append_record(connection: sqlite3.Connection) -> WriteReceipt:
            previous = connection.execute(
                """
                SELECT * FROM memory_operation_receipts
                WHERE namespace = ? AND operation = ? AND idempotency_key = ?
                """,
                (write.namespace, operation, idempotency_key),
            ).fetchone()
            if previous is not None:
                if previous["input_hash"] != input_hash:
                    raise MemoryConflictError("idempotency key was reused with different input")
                receipt = self._receipt_from_row(previous)
                if not isinstance(receipt, WriteReceipt):
                    raise MemoryConflictError(
                        "idempotency key was reused for another operation type"
                    )
                return receipt
            record = StoredRecord.from_write(write)
            duplicate = connection.execute(
                "SELECT 1 FROM memory_records WHERE namespace = ? AND record_id = ?",
                (record.namespace, record.record_id),
            ).fetchone()
            if duplicate is not None:
                raise MemoryConflictError("record_id already exists in namespace")
            receipt = WriteReceipt(
                operation=operation,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                record=record,
            )
            connection.execute(
                """
                INSERT INTO memory_records
                (
                    namespace, record_id, record_type, schema_version,
                    payload_json, created_at, content_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.namespace,
                    record.record_id,
                    record.record_type,
                    record.schema_version,
                    canonical_json(record.payload),
                    record.created_at.isoformat(),
                    record.content_hash,
                ),
            )
            connection.execute(
                """
                INSERT INTO memory_operation_receipts
                (namespace, operation, idempotency_key, input_hash, receipt_kind, receipt_json)
                VALUES (?, ?, ?, ?, 'write', ?)
                """,
                (
                    record.namespace,
                    operation,
                    idempotency_key,
                    input_hash,
                    receipt.model_dump_json(),
                ),
            )
            return receipt

        return self._transaction(append_record)

    async def get(self, *, namespace: str, record_id: str) -> StoredRecord | None:
        namespace, record_id = validate_record_lookup(namespace=namespace, record_id=record_id)
        await self._ensure_initialized()
        async with self._lock:
            return await asyncio.to_thread(self._get, namespace, record_id)

    def _get(self, namespace: str, record_id: str) -> StoredRecord | None:
        def get_record(connection: sqlite3.Connection) -> StoredRecord | None:
            row = connection.execute(
                "SELECT * FROM memory_records WHERE namespace = ? AND record_id = ?",
                (namespace, record_id),
            ).fetchone()
            return self._record_from_row(row) if row is not None else None

        return self._read(get_record)

    async def list(self, *, namespace: str) -> list[StoredRecord]:
        namespace = validate_namespace(namespace)
        await self._ensure_initialized()
        async with self._lock:
            return await asyncio.to_thread(self._list, namespace)

    def _list(self, namespace: str) -> list[StoredRecord]:
        def list_records(connection: sqlite3.Connection) -> list[StoredRecord]:
            rows = connection.execute(
                """
                SELECT * FROM memory_records WHERE namespace = ?
                ORDER BY created_at ASC, record_id ASC
                """,
                (namespace,),
            ).fetchall()
            return [self._record_from_row(row) for row in rows]

        return self._read(list_records)

    async def create_snapshot(
        self, *, namespace: str, snapshot_id: str, operation: str, idempotency_key: str
    ) -> SnapshotReceipt:
        namespace, snapshot_id, operation, idempotency_key = validate_snapshot_call(
            namespace=namespace,
            snapshot_id=snapshot_id,
            operation=operation,
            idempotency_key=idempotency_key,
        )
        await self._ensure_initialized()
        input_hash = sha256_json(
            {"operation": operation, "namespace": namespace, "snapshot_id": snapshot_id}
        )
        async with self._lock:
            return await asyncio.to_thread(
                self._create_snapshot,
                namespace,
                snapshot_id,
                operation,
                idempotency_key,
                input_hash,
            )

    def _create_snapshot(
        self,
        namespace: str,
        snapshot_id: str,
        operation: str,
        idempotency_key: str,
        input_hash: str,
    ) -> SnapshotReceipt:
        def freeze_snapshot(connection: sqlite3.Connection) -> SnapshotReceipt:
            previous = connection.execute(
                """
                SELECT * FROM memory_operation_receipts
                WHERE namespace = ? AND operation = ? AND idempotency_key = ?
                """,
                (namespace, operation, idempotency_key),
            ).fetchone()
            if previous is not None:
                if previous["input_hash"] != input_hash:
                    raise MemoryConflictError("idempotency key was reused with different input")
                receipt = self._receipt_from_row(previous)
                if not isinstance(receipt, SnapshotReceipt):
                    raise MemoryConflictError(
                        "idempotency key was reused for another operation type"
                    )
                return receipt
            duplicate = connection.execute(
                "SELECT 1 FROM memory_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if duplicate is not None:
                raise MemoryConflictError("snapshot_id already exists")
            rows = connection.execute(
                """
                SELECT record_id, content_hash FROM memory_records WHERE namespace = ?
                ORDER BY created_at ASC, record_id ASC
                """,
                (namespace,),
            ).fetchall()
            members = [
                SnapshotMember(record_id=row["record_id"], content_hash=row["content_hash"])
                for row in rows
            ]
            snapshot = MemorySnapshot.create(
                snapshot_id=snapshot_id, namespace=namespace, members=members
            )
            receipt = SnapshotReceipt(
                operation=operation,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                snapshot=snapshot,
            )
            connection.execute(
                """
                INSERT INTO memory_snapshots
                (snapshot_id, namespace, schema_version, created_at, content_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.namespace,
                    snapshot.schema_version,
                    snapshot.created_at.isoformat(),
                    snapshot.content_hash,
                ),
            )
            connection.executemany(
                """
                INSERT INTO memory_snapshot_members (snapshot_id, ordinal, record_id, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (snapshot.snapshot_id, ordinal, member.record_id, member.content_hash)
                    for ordinal, member in enumerate(snapshot.members)
                ],
            )
            connection.execute(
                """
                INSERT INTO memory_operation_receipts
                (namespace, operation, idempotency_key, input_hash, receipt_kind, receipt_json)
                VALUES (?, ?, ?, ?, 'snapshot', ?)
                """,
                (
                    namespace,
                    operation,
                    idempotency_key,
                    input_hash,
                    receipt.model_dump_json(),
                ),
            )
            return receipt

        return self._transaction(freeze_snapshot)

    async def get_snapshot(self, *, snapshot_id: str) -> MemorySnapshot | None:
        snapshot_id = validate_snapshot_lookup(snapshot_id)
        await self._ensure_initialized()
        async with self._lock:
            return await asyncio.to_thread(self._get_snapshot, snapshot_id)

    def _get_snapshot(self, snapshot_id: str) -> MemorySnapshot | None:
        def get_snapshot(connection: sqlite3.Connection) -> MemorySnapshot | None:
            row = connection.execute(
                "SELECT * FROM memory_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                return None
            member_rows = connection.execute(
                """
                SELECT record_id, content_hash FROM memory_snapshot_members
                WHERE snapshot_id = ? ORDER BY ordinal ASC
                """,
                (snapshot_id,),
            ).fetchall()
            return MemorySnapshot(
                snapshot_id=row["snapshot_id"],
                namespace=row["namespace"],
                created_at=row["created_at"],
                content_hash=row["content_hash"],
                schema_version=row["schema_version"],
                members=[
                    SnapshotMember(
                        record_id=member["record_id"], content_hash=member["content_hash"]
                    )
                    for member in member_rows
                ],
            )

        return self._read(get_snapshot)
