"""Durable, bounded reads of canonical recorded observations.

The reader is deliberately a thin adapter over ``StructuredMemoryStore``.
Bookmarks contain only immutable record metadata; the result bytes are loaded
again from the canonical store after a process restart and are never copied to
a second byte store.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, ValidationError
from pydantic_core import PydanticSerializationError

from uptick_agent.memory.candidate_validation import validate_transition_record
from uptick_agent.memory.contracts import (
    ContractModel,
    ExperienceTransition,
    MemoryPermanentError,
    MemoryValidationError,
)
from uptick_agent.memory.stores.contracts import (
    StoredRecord,
    StructuredMemoryStore,
    validate_identifier,
    validate_namespace,
)
from uptick_agent.redaction import sanitize_json

MIN_READ_BYTES = 4
MAX_READ_BYTES = 8 * 1024
MAX_SUMMARY_BYTES = 512
_HISTORICAL_EVIDENCE = "stale_environment_observation"


class ObservationReaderError(Exception):
    """Base class for safe durable-observation reader failures."""


class ObservationReaderUnavailableError(ObservationReaderError):
    """The canonical observation is absent, stale, or fails an integrity gate."""


class ObservationReaderInvalidRequestError(ObservationReaderError):
    """A reader or read argument is invalid."""


class ObservationReaderOffsetError(ObservationReaderInvalidRequestError):
    """A requested offset is outside the result or splits a UTF-8 codepoint."""


class ObservationBookmark(ContractModel):
    """A restart-safe, canonical binding for one historical result."""

    record_id: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    namespace: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    source_iteration: int = Field(ge=1)
    source_time: datetime
    environment_id: str | None = Field(default=None, max_length=256)
    scenario_id: str | None = Field(default=None, max_length=256)
    result_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    result_bytes: int = Field(ge=0)
    summary: str | None = Field(default=None, max_length=512)
    historical_evidence: Literal[_HISTORICAL_EVIDENCE] = _HISTORICAL_EVIDENCE

    @property
    def record_hash(self) -> str:
        """Compatibility name for callers that call the canonical hash a record hash."""

        return self.content_hash

    @property
    def result_byte_length(self) -> int:
        """Compatibility name for callers that spell out the result length."""

        return self.result_bytes


def _require_int(value: object, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ObservationReaderInvalidRequestError(f"{name} must be an integer >= {minimum}")
    return value


def _canonical_result_bytes(transition: ExperienceTransition) -> bytes:
    """Render the validated result with the archive's stable UTF-8 encoding."""

    try:
        safe_result = sanitize_json(transition.result)
        if safe_result != transition.result:
            raise ValueError("result contains credential-shaped content")
        rendered = json.dumps(
            safe_result,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return rendered.encode("utf-8")
    except (PydanticSerializationError, TypeError, ValueError, UnicodeError) as error:
        raise ObservationReaderUnavailableError(
            "observation result cannot be serialized safely"
        ) from error


def _summary_excerpt(result: dict[str, Any]) -> str | None:
    """Retain an optional bounded textual summary without inventing identity."""

    summary = result.get("summary")
    if not isinstance(summary, str):
        return None
    encoded = summary.encode("utf-8")
    if len(encoded) <= MAX_SUMMARY_BYTES:
        return summary
    suffix = "…[truncated]"
    available = max(0, MAX_SUMMARY_BYTES - len(suffix.encode("utf-8")))
    excerpt = encoded[:available].decode("utf-8", errors="ignore") + suffix
    while len(excerpt.encode("utf-8")) > MAX_SUMMARY_BYTES and excerpt:
        excerpt = excerpt[:-1]
    return excerpt


class StoredObservationReader:
    """Read one run's immutable observations through a structured store."""

    def __init__(self, store: StructuredMemoryStore, *, namespace: str, run_id: str) -> None:
        self._store = store
        try:
            self._namespace = validate_namespace(namespace)
            self._run_id = validate_identifier(run_id, name="run_id", max_length=256)
        except MemoryValidationError as error:
            raise ObservationReaderInvalidRequestError(str(error)) from error

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def run_id(self) -> str:
        return self._run_id

    async def bookmark(self, record_id: str, *, current_iteration: int) -> ObservationBookmark:
        """Bind one prior canonical transition to a restart-safe bookmark."""

        _require_int(current_iteration, "current_iteration", minimum=1)
        try:
            record_id = validate_identifier(record_id, name="record_id", max_length=256)
        except MemoryValidationError as error:
            raise ObservationReaderInvalidRequestError(str(error)) from error

        record, transition = await self._load(record_id)
        self._require_prior(transition, current_iteration)
        result = _canonical_result_bytes(transition)
        return ObservationBookmark(
            record_id=record.record_id,
            content_hash=record.content_hash,
            namespace=record.namespace,
            run_id=transition.run_id,
            source_iteration=transition.iteration,
            source_time=transition.occurred_at,
            # ``None`` remains unknown.  The reader's run or namespace is not
            # an environment identity and must not be substituted here.
            environment_id=transition.environment_id,
            scenario_id=transition.scenario_id,
            result_digest=hashlib.sha256(result).hexdigest(),
            result_bytes=len(result),
            summary=_summary_excerpt(transition.result),
        )

    async def read(
        self,
        bookmark: ObservationBookmark,
        *,
        current_iteration: int,
        offset: int = 0,
        max_bytes: int = 4096,
    ) -> dict[str, Any]:
        """Read one UTF-8-aligned result chunk after revalidating its binding."""

        _require_int(current_iteration, "current_iteration", minimum=1)
        _require_int(offset, "offset", minimum=0)
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not MIN_READ_BYTES <= max_bytes <= MAX_READ_BYTES
        ):
            raise ObservationReaderInvalidRequestError(
                f"max_bytes must be between {MIN_READ_BYTES} and {MAX_READ_BYTES}"
            )
        owned = self._own_bookmark(bookmark)
        if owned.namespace != self._namespace or owned.run_id != self._run_id:
            raise ObservationReaderUnavailableError("observation unavailable")
        if owned.historical_evidence != _HISTORICAL_EVIDENCE:
            raise ObservationReaderUnavailableError("observation bookmark marker is invalid")
        if owned.source_iteration >= current_iteration:
            raise ObservationReaderInvalidRequestError(
                "historical observation must precede current_iteration"
            )

        record, transition = await self._load(owned.record_id)
        self._require_prior(transition, current_iteration)
        self._check_binding(owned, record, transition)
        payload = _canonical_result_bytes(transition)
        if owned.result_bytes != len(payload):
            raise ObservationReaderUnavailableError("observation result length changed")
        if offset > len(payload):
            raise ObservationReaderOffsetError("offset is outside the observation")
        if offset < len(payload) and payload[offset] & 0xC0 == 0x80:
            raise ObservationReaderOffsetError("offset is not UTF-8 aligned")

        end = min(offset + max_bytes, len(payload))
        if end == offset:
            text = ""
        else:
            while end > offset:
                try:
                    text = payload[offset:end].decode("utf-8")
                    break
                except UnicodeDecodeError:
                    end -= 1
            else:
                raise ObservationReaderOffsetError("read limit cannot contain a UTF-8 codepoint")
        returned = end - offset
        return {
            "ref": owned.record_id,
            "record_id": owned.record_id,
            "namespace": owned.namespace,
            "run_id": owned.run_id,
            "environment_id": owned.environment_id,
            "scenario_id": owned.scenario_id,
            "iteration": owned.source_iteration,
            "source_time": owned.source_time,
            "digest": owned.result_digest,
            "text": text,
            "offset": offset,
            "returned_bytes": returned,
            "next_offset": end,
            "eof": end == len(payload),
            "historical_evidence": _HISTORICAL_EVIDENCE,
        }

    async def _load(self, record_id: str) -> tuple[StoredRecord, ExperienceTransition]:
        try:
            record = await self._store.get(namespace=self._namespace, record_id=record_id)
        except MemoryPermanentError as error:
            raise ObservationReaderUnavailableError("observation unavailable") from error
        if record is None:
            raise ObservationReaderUnavailableError("observation unavailable")
        try:
            owned = StoredRecord.validate_integrity(record)
            transition = validate_transition_record(owned)
        except (MemoryPermanentError, MemoryValidationError, TypeError, ValueError) as error:
            raise ObservationReaderUnavailableError("observation unavailable") from error
        if owned.namespace != self._namespace or owned.record_id != record_id:
            raise ObservationReaderUnavailableError("observation provenance changed")
        if transition.run_id != self._run_id:
            raise ObservationReaderUnavailableError("observation belongs to another run")
        return owned, transition

    @staticmethod
    def _require_prior(transition: ExperienceTransition, current_iteration: int) -> None:
        if transition.iteration >= current_iteration:
            raise ObservationReaderInvalidRequestError(
                "historical observation must precede current_iteration"
            )

    @staticmethod
    def _own_bookmark(bookmark: ObservationBookmark) -> ObservationBookmark:
        if not isinstance(bookmark, ObservationBookmark):
            raise ObservationReaderInvalidRequestError("bookmark must be an ObservationBookmark")
        try:
            return ObservationBookmark.model_validate(
                bookmark.model_dump(mode="python", round_trip=True, warnings="error")
            )
        except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
            raise ObservationReaderUnavailableError("observation bookmark is invalid") from error

    @staticmethod
    def _check_binding(
        bookmark: ObservationBookmark,
        record: StoredRecord,
        transition: ExperienceTransition,
    ) -> None:
        if (
            record.namespace != bookmark.namespace
            or record.record_id != bookmark.record_id
            or record.content_hash != bookmark.content_hash
            or transition.run_id != bookmark.run_id
            or transition.iteration != bookmark.source_iteration
            or transition.occurred_at != bookmark.source_time
            or transition.environment_id != bookmark.environment_id
            or transition.scenario_id != bookmark.scenario_id
        ):
            raise ObservationReaderUnavailableError("observation bookmark does not match source")
        payload = _canonical_result_bytes(transition)
        if (
            hashlib.sha256(payload).hexdigest() != bookmark.result_digest
            or len(payload) != bookmark.result_bytes
            or _summary_excerpt(transition.result) != bookmark.summary
        ):
            raise ObservationReaderUnavailableError("observation result binding changed")


__all__ = [
    "MAX_READ_BYTES",
    "MAX_SUMMARY_BYTES",
    "MIN_READ_BYTES",
    "ObservationBookmark",
    "ObservationReaderError",
    "ObservationReaderInvalidRequestError",
    "ObservationReaderOffsetError",
    "ObservationReaderUnavailableError",
    "StoredObservationReader",
]
