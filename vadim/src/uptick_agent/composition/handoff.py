"""Bounded store-backed composition of runtime observation handoff."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import Field, ValidationError
from pydantic_core import PydanticSerializationError

from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.memory.contracts import ContractModel, MemoryPermanentError
from uptick_agent.memory.observation_reader import (
    ObservationBookmark,
    ObservationReaderError,
    ObservationReaderInvalidRequestError,
    ObservationReaderUnavailableError,
    StoredObservationReader,
)
from uptick_agent.memory.stores.contracts import (
    RecordWrite,
    StoredRecord,
    StructuredMemoryStore,
    sha256_json,
    validate_namespace,
)
from uptick_agent.runs.handoff import ObservationHandoffPort, ObservationReadRequest

_DEFAULT_MAX_BOOKMARKS = 8
_DEFAULT_MAX_CONTEXT_BYTES = 8_000
_HISTORICAL_SUMMARY = "Historical observation."
_UNAVAILABLE_SUMMARY = "Historical observation unavailable."
_INDEX_RECORD_TYPE = "observation-handoff-index"
_INDEX_OPERATION = "record-observation-handoff-index"
_HEX_DIGEST = r"^[0-9a-f]{64}$"


class ObservationHandoffIdentity(ContractModel):
    """Caller-owned identity required to restore a durable handoff index."""

    namespace: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    environment_id: str = Field(min_length=1, max_length=256)
    scenario_id: str = Field(min_length=1, max_length=256)
    environment_content_hash: str | None = Field(default=None, pattern=_HEX_DIGEST)
    scenario_content_hash: str | None = Field(default=None, pattern=_HEX_DIGEST)


class _DurableHandoffIndex(ContractModel):
    """Latest bounded issued-reference view for one run."""

    index_version: int = Field(ge=1)
    identity: ObservationHandoffIdentity
    current_iteration: int = Field(ge=1)
    max_bookmarks: int = Field(ge=1)
    max_context_bytes: int = Field(ge=2)
    bookmarks: list[ObservationBookmark] = Field(default_factory=list)


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bookmark_copy(bookmark: ObservationBookmark) -> ObservationBookmark:
    return ObservationBookmark.model_validate(
        bookmark.model_dump(mode="python", round_trip=True, warnings="error")
    )


def _bookmark_json(bookmarks: list[ObservationBookmark]) -> list[dict[str, Any]]:
    return [bookmark.model_dump(mode="json") for bookmark in bookmarks]


def _serialized_context_bytes(bookmarks: list[ObservationBookmark]) -> int:
    return len(
        json.dumps(
            _bookmark_json(bookmarks),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


class ObservationHandoff(ObservationHandoffPort):
    """Maintain a run-local, bounded index of issued observation bookmarks."""

    def __init__(
        self,
        store: StructuredMemoryStore,
        namespace: str,
        *,
        max_bookmarks: int = _DEFAULT_MAX_BOOKMARKS,
        max_context_bytes: int = _DEFAULT_MAX_CONTEXT_BYTES,
        durable_index_namespace: str | None = None,
    ) -> None:
        self._store = store
        self._namespace = namespace
        self._max_bookmarks = _require_positive_int(max_bookmarks, "max_bookmarks")
        self._max_context_bytes = _require_positive_int(max_context_bytes, "max_context_bytes")
        if self._max_context_bytes < 2:
            raise ValueError("max_context_bytes must allow an empty JSON list")
        if durable_index_namespace is not None:
            durable_index_namespace = validate_namespace(durable_index_namespace)
            if durable_index_namespace == namespace:
                raise ValueError("durable handoff index must use a separate namespace")
        self._durable_index_namespace = durable_index_namespace
        self._reader: StoredObservationReader | None = None
        self._run_id: str | None = None
        self._identity: ObservationHandoffIdentity | None = None
        self._index_version = 0
        self._bookmarks: OrderedDict[str, ObservationBookmark] = OrderedDict()

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def max_bookmarks(self) -> int:
        return self._max_bookmarks

    @property
    def max_context_bytes(self) -> int:
        return self._max_context_bytes

    @property
    def durable_index_namespace(self) -> str | None:
        return self._durable_index_namespace

    def begin(self, run_id: str) -> None:
        """Start a run-local handoff scope and discard earlier issued refs."""

        reader = StoredObservationReader(
            self._store,
            namespace=self._namespace,
            run_id=run_id,
        )
        self._clear_active_scope()
        self._reader = reader
        self._run_id = reader.run_id

    def _clear_active_scope(self) -> None:
        self._reader = None
        self._run_id = None
        self._identity = None
        self._index_version = 0
        self._bookmarks.clear()

    async def initialize_new(self, identity: ObservationHandoffIdentity) -> None:
        """Initialize an explicitly opted-in new run without restoring refs."""

        self._clear_active_scope()
        if self._durable_index_namespace is None:
            raise ObservationReaderInvalidRequestError("durable handoff index is not enabled")
        identity = self._own_identity(identity)
        if identity.namespace != self._namespace:
            raise ObservationReaderInvalidRequestError("handoff identity namespace is invalid")
        try:
            if await self._latest_index(identity) is not None:
                raise ObservationReaderUnavailableError(
                    "observation handoff index already exists for this run"
                )
            self.begin(identity.run_id)
            self._identity = identity
        except ObservationReaderUnavailableError:
            self._clear_active_scope()
            raise
        except Exception:
            self._clear_active_scope()
            raise

    async def restore(
        self,
        identity: ObservationHandoffIdentity,
        *,
        current_iteration: int,
    ) -> None:
        """Restore the latest exact durable index without inventing refs.

        The caller supplies the current session identity and iteration cutoff.
        Only bookmarks present in the latest index record are restored; source
        transitions are loaded solely to revalidate those indexed bookmarks.
        """

        self._clear_active_scope()
        if self._durable_index_namespace is None:
            raise ObservationReaderInvalidRequestError("durable handoff index is not enabled")
        identity = self._own_identity(identity)
        if identity.namespace != self._namespace:
            raise ObservationReaderInvalidRequestError("handoff identity namespace is invalid")
        if isinstance(current_iteration, bool) or not isinstance(current_iteration, int):
            raise ObservationReaderInvalidRequestError("current_iteration must be an integer >= 1")
        if current_iteration < 1:
            raise ObservationReaderInvalidRequestError("current_iteration must be an integer >= 1")

        self.begin(identity.run_id)
        try:
            latest = await self._latest_index(identity)
            if latest is None:
                self._identity = identity
                return
            if latest.identity != identity:
                raise ObservationReaderUnavailableError("observation handoff identity changed")
            if (
                latest.max_bookmarks != self._max_bookmarks
                or latest.max_context_bytes != self._max_context_bytes
            ):
                raise ObservationReaderUnavailableError(
                    "observation handoff context budget changed"
                )
            if (
                len(latest.bookmarks) > self._max_bookmarks
                or _serialized_context_bytes(latest.bookmarks) > self._max_context_bytes
            ):
                raise ObservationReaderUnavailableError(
                    "observation handoff index exceeds its context budget"
                )
            if latest.current_iteration > current_iteration:
                raise ObservationReaderUnavailableError(
                    "observation handoff index is ahead of current iteration"
                )
            restored: OrderedDict[str, ObservationBookmark] = OrderedDict()
            reader = self._reader
            if reader is None:
                raise ObservationReaderUnavailableError("observation handoff is unavailable")
            for indexed in latest.bookmarks:
                if indexed.source_iteration >= current_iteration:
                    raise ObservationReaderUnavailableError(
                        "observation handoff index contains a future reference"
                    )
                try:
                    rebuilt = await reader.bookmark(
                        indexed.record_id,
                        current_iteration=current_iteration,
                    )
                except ObservationReaderError as error:
                    raise ObservationReaderUnavailableError(
                        "observation handoff source is unavailable"
                    ) from error
                if rebuilt != indexed:
                    raise ObservationReaderUnavailableError(
                        "observation handoff bookmark binding changed"
                    )
                if (
                    rebuilt.environment_id != identity.environment_id
                    or rebuilt.scenario_id != identity.scenario_id
                ):
                    raise ObservationReaderUnavailableError(
                        "observation handoff source identity changed"
                    )
                restored[rebuilt.record_id] = _bookmark_copy(rebuilt)
            self._identity = identity
            self._index_version = latest.index_version
            self._bookmarks = restored
            self._trim()
        except ObservationReaderUnavailableError:
            self._clear_active_scope()
            raise
        except (MemoryPermanentError, TypeError, ValueError, ValidationError) as error:
            self._clear_active_scope()
            raise ObservationReaderUnavailableError(
                "observation handoff index is invalid"
            ) from error
        except Exception:
            self._clear_active_scope()
            raise

    async def note_transition(
        self, transition_id: str, *, current_iteration: int
    ) -> ObservationBookmark | None:
        """Issue and retain a bookmark for an already stored prior transition."""

        reader = self._reader
        if reader is None:
            raise ObservationReaderInvalidRequestError("handoff run has not begun")
        if self._durable_index_namespace is not None and self._identity is None:
            raise ObservationReaderInvalidRequestError(
                "durable handoff requires an explicit restore identity"
            )
        try:
            issued = await reader.bookmark(transition_id, current_iteration=current_iteration)
        except ObservationReaderUnavailableError:
            # A run may have no enabled canonical episode writer. Keep the
            # runtime handoff optional while allowing transient store errors
            # and invalid caller requests to remain visible to the runner.
            return None
        owned = _bookmark_copy(issued)
        if (
            self._durable_index_namespace is not None
            and self._identity is not None
            and (
                owned.environment_id != self._identity.environment_id
                or owned.scenario_id != self._identity.scenario_id
            )
        ):
            return None
        self._bookmarks.pop(owned.record_id, None)
        self._bookmarks[owned.record_id] = owned
        self._trim()
        if self._durable_index_namespace is not None:
            await self._persist_index(current_iteration=current_iteration)
        retained = self._bookmarks.get(owned.record_id)
        return _bookmark_copy(retained) if retained is not None else None

    def snapshot(self, *, current_iteration: int) -> list[dict[str, Any]]:
        """Return only still-prior issued bookmarks as JSON-safe dictionaries."""

        if isinstance(current_iteration, bool) or not isinstance(current_iteration, int):
            raise ObservationReaderInvalidRequestError("current_iteration must be an integer >= 1")
        if current_iteration < 1:
            raise ObservationReaderInvalidRequestError("current_iteration must be an integer >= 1")
        bookmarks = [
            _bookmark_copy(bookmark)
            for bookmark in self._bookmarks.values()
            if bookmark.source_iteration < current_iteration
        ]
        return _bookmark_json(bookmarks)

    async def read(
        self,
        request: ObservationReadRequest,
        *,
        current_iteration: int,
    ) -> ToolResult:
        """Read one issued historical reference through the canonical store."""

        try:
            request = self._own_request(request)
        except (
            ObservationReaderError,
            PydanticSerializationError,
            TypeError,
            ValueError,
            ValidationError,
        ):
            return self._failure()

        reader = self._reader
        if reader is None or request.record_id not in self._bookmarks:
            return self._failure()
        bookmark = self._bookmarks[request.record_id]
        try:
            data = await reader.read(
                _bookmark_copy(bookmark),
                current_iteration=current_iteration,
                offset=request.offset,
                max_bytes=request.max_bytes,
            )
        except ObservationReaderError:
            return self._failure()
        return ToolResult(
            action_kind="memory.read",
            ok=True,
            summary=_HISTORICAL_SUMMARY,
            data=data,
            objective_metrics=[],
            operation_links=[],
            terminal=False,
        )

    @staticmethod
    def _own_request(request: ObservationReadRequest) -> ObservationReadRequest:
        if not isinstance(request, ObservationReadRequest):
            raise ObservationReaderInvalidRequestError(
                "read request must be an ObservationReadRequest"
            )
        return ObservationReadRequest.model_validate(
            request.model_dump(mode="python", round_trip=True, warnings="error")
        )

    @staticmethod
    def _own_identity(identity: ObservationHandoffIdentity) -> ObservationHandoffIdentity:
        if not isinstance(identity, ObservationHandoffIdentity):
            raise ObservationReaderInvalidRequestError("durable handoff identity is invalid")
        try:
            return ObservationHandoffIdentity.model_validate(
                identity.model_dump(mode="python", round_trip=True, warnings="error")
            )
        except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
            raise ObservationReaderInvalidRequestError(
                "durable handoff identity is invalid"
            ) from error

    async def _latest_index(
        self, identity: ObservationHandoffIdentity
    ) -> _DurableHandoffIndex | None:
        if self._durable_index_namespace is None:
            return None
        prefix = f"{_INDEX_RECORD_TYPE}:{_identity_digest(identity)}:"
        candidates: list[tuple[int, StoredRecord]] = []
        records = await self._store.list(namespace=self._durable_index_namespace)
        for record in records:
            if not record.record_id.startswith(prefix):
                continue
            suffix = record.record_id.removeprefix(prefix)
            if not suffix.isdigit() or int(suffix) < 1:
                raise ObservationReaderUnavailableError("observation handoff index ID is invalid")
            candidates.append((int(suffix), record))
        if not candidates:
            return None
        latest_version = max(version for version, _ in candidates)
        latest_records = [record for version, record in candidates if version == latest_version]
        if len(latest_records) != 1:
            raise ObservationReaderUnavailableError("observation handoff index is ambiguous")
        return _read_index(
            latest_records[0],
            expected_identity=identity,
            expected_version=latest_version,
        )

    async def _persist_index(self, *, current_iteration: int) -> None:
        if self._durable_index_namespace is None or self._identity is None:
            raise ObservationReaderInvalidRequestError(
                "durable handoff requires an explicit restore identity"
            )
        next_version = self._index_version + 1
        index = _DurableHandoffIndex(
            index_version=next_version,
            identity=self._identity,
            current_iteration=current_iteration,
            max_bookmarks=self._max_bookmarks,
            max_context_bytes=self._max_context_bytes,
            bookmarks=[_bookmark_copy(bookmark) for bookmark in self._bookmarks.values()],
        )
        record_id = _index_record_id(index.identity, index.index_version)
        await self._store.append(
            RecordWrite(
                namespace=self._durable_index_namespace,
                record_id=record_id,
                record_type=_INDEX_RECORD_TYPE,
                payload=index.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            ),
            operation=_INDEX_OPERATION,
            idempotency_key=record_id,
        )
        self._index_version = next_version

    def _trim(self) -> None:
        while self._bookmarks and (
            len(self._bookmarks) > self._max_bookmarks
            or _serialized_context_bytes(list(self._bookmarks.values())) > self._max_context_bytes
        ):
            self._bookmarks.popitem(last=False)

    @staticmethod
    def _failure() -> ToolResult:
        return ToolResult(
            action_kind="memory.read",
            ok=False,
            summary=_UNAVAILABLE_SUMMARY,
            data={},
            objective_metrics=[],
            operation_links=[],
            terminal=False,
        )


def _identity_digest(identity: ObservationHandoffIdentity) -> str:
    return sha256_json(identity.model_dump(mode="json"))


def _index_record_id(identity: ObservationHandoffIdentity, version: int) -> str:
    return f"{_INDEX_RECORD_TYPE}:{_identity_digest(identity)}:{version}"


def _read_index(
    record: StoredRecord,
    *,
    expected_identity: ObservationHandoffIdentity,
    expected_version: int,
) -> _DurableHandoffIndex:
    try:
        owned = StoredRecord.validate_integrity(record)
        if owned.record_type != _INDEX_RECORD_TYPE:
            raise ObservationReaderUnavailableError("observation handoff index type is invalid")
        index = _DurableHandoffIndex.model_validate(owned.payload)
        if (
            owned.record_id != _index_record_id(index.identity, index.index_version)
            or index.identity != expected_identity
            or index.index_version != expected_version
        ):
            raise ObservationReaderUnavailableError("observation handoff index binding changed")
        return index
    except ObservationReaderUnavailableError:
        raise
    except (MemoryPermanentError, TypeError, ValueError, ValidationError) as error:
        raise ObservationReaderUnavailableError("observation handoff index is invalid") from error


class DurableObservationHandoffStartup:
    """Adapt a caller-owned identity resolver to the generic runner startup port."""

    def __init__(
        self,
        handoff: ObservationHandoff,
        identity_resolver: Callable[[object, ToolResult], ObservationHandoffIdentity],
    ) -> None:
        if not isinstance(handoff, ObservationHandoff):
            raise TypeError("handoff must be an ObservationHandoff")
        if not callable(identity_resolver):
            raise TypeError("identity_resolver must be callable")
        self._handoff = handoff
        self._identity_resolver = identity_resolver

    async def initialize_new(self, *, session: object, initial_result: ToolResult) -> None:
        identity = self._identity_resolver(session, initial_result)
        identity = self._handoff._own_identity(identity)
        try:
            session_run_id = session.run_id
        except (AttributeError, TypeError) as error:
            raise ObservationReaderInvalidRequestError(
                "handoff startup session run_id is unavailable"
            ) from error
        if session_run_id != identity.run_id:
            raise ObservationReaderInvalidRequestError(
                "handoff startup identity does not match session run_id"
            )
        for field_name in (
            "environment_id",
            "scenario_id",
            "environment_content_hash",
            "scenario_content_hash",
        ):
            try:
                session_value = getattr(session, field_name, None)
            except (AttributeError, TypeError) as error:
                raise ObservationReaderInvalidRequestError(
                    "handoff startup session identity is unavailable"
                ) from error
            if session_value is not None and session_value != getattr(identity, field_name):
                raise ObservationReaderInvalidRequestError(
                    f"handoff startup identity does not match session {field_name}"
                )
        await self._handoff.initialize_new(identity)


__all__ = [
    "ObservationHandoff",
    "ObservationHandoffIdentity",
    "ObservationReadRequest",
    "DurableObservationHandoffStartup",
]
