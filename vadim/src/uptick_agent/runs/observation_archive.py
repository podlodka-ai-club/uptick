"""Small run-local archive for bounded, historical tool-result reads."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.redaction import sanitize_json

DEFAULT_MAX_TOTAL_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_RECORD_BYTES = 1 * 1024 * 1024
MIN_READ_BYTES = 4
MAX_READ_BYTES = 8 * 1024
MAX_SUMMARY_BYTES = 512
MAX_ACTION_KIND_BYTES = 96
_TRUNCATED_SUFFIX = "…[truncated]"
_HISTORICAL_EVIDENCE = "stale_environment_observation"


class ObservationArchiveError(Exception):
    """Base class for safe archive failures."""


class ObservationArchiveUnavailableError(ObservationArchiveError):
    """The reference is absent, expired, or belongs to another archive."""


class ObservationArchiveInvalidRequestError(ObservationArchiveError):
    """A record or read argument is invalid."""


class ObservationArchiveOffsetError(ObservationArchiveInvalidRequestError):
    """The requested byte offset is out of range or not UTF-8 aligned."""


class ObservationArchiveRecordTooLargeError(ObservationArchiveError):
    """A sanitized record exceeds the configured per-record limit."""


class ObservationArchiveStorageError(ObservationArchiveError):
    """A record could not be serialized or admitted to the archive."""


@dataclass(frozen=True, slots=True)
class _Entry:
    iteration: int
    payload: bytes
    digest: str


def _require_int(value: object, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ObservationArchiveInvalidRequestError(f"{name} must be an integer >= {minimum}")
    return value


def _bounded_json_text(value: str, max_bytes: int) -> tuple[str, bool]:
    def encoded_size(text: str) -> int:
        return len(json.dumps(text, ensure_ascii=True, separators=(",", ":")).encode())

    if encoded_size(value) <= max_bytes:
        return value, False
    # The escaped representation can only grow from the source text. Start
    # with a small candidate so a megabyte-scale field cannot trigger a
    # character-at-a-time trim loop.
    prefix = value[:max_bytes]
    excerpt = prefix + _TRUNCATED_SUFFIX
    while encoded_size(excerpt) > max_bytes and prefix:
        prefix = prefix[:-1]
        excerpt = prefix + _TRUNCATED_SUFFIX
    return excerpt, True


def _utf8_start(payload: bytes, offset: int) -> int:
    while offset < len(payload) and payload[offset] & 0xC0 == 0x80:
        offset += 1
    return offset


class ObservationArchive:
    """An in-memory archive whose references cannot address external storage."""

    def __init__(
        self,
        *,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
    ) -> None:
        self.max_total_bytes = _require_int(max_total_bytes, "max_total_bytes")
        self.max_record_bytes = _require_int(max_record_bytes, "max_record_bytes")
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._total_bytes = 0

    def record(self, iteration: int, result: ToolResult) -> dict[str, Any]:
        """Sanitize and retain one immutable result, returning an opaque receipt."""

        iteration = _require_int(iteration, "iteration")
        if not isinstance(result, ToolResult):
            raise ObservationArchiveInvalidRequestError("result must be a ToolResult")
        try:
            payload = sanitize_json(
                result.model_dump(mode="json", round_trip=True, warnings="error")
            )
            rendered = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            encoded = rendered.encode("utf-8")
        except Exception as error:
            raise ObservationArchiveStorageError("result could not be serialized safely") from error
        if len(encoded) > self.max_record_bytes:
            raise ObservationArchiveRecordTooLargeError("sanitized result exceeds record limit")
        if len(encoded) > self.max_total_bytes:
            raise ObservationArchiveStorageError("sanitized result exceeds archive budget")

        while self._entries and self._total_bytes + len(encoded) > self.max_total_bytes:
            _ref, evicted = self._entries.popitem(last=False)
            self._total_bytes -= len(evicted.payload)
        ref = self._new_ref()
        digest = hashlib.sha256(encoded).hexdigest()
        self._entries[ref] = _Entry(iteration, encoded, digest)
        self._total_bytes += len(encoded)
        action_kind, action_kind_truncated = _bounded_json_text(
            payload["action_kind"], MAX_ACTION_KIND_BYTES
        )
        summary, summary_truncated = _bounded_json_text(payload["summary"], MAX_SUMMARY_BYTES)
        return {
            "ref": ref,
            "source_iteration": iteration,
            "bytes": len(encoded),
            "digest": digest,
            "summary": summary,
            "summary_truncated": summary_truncated,
            "status": {
                "action_kind": action_kind,
                "action_kind_truncated": action_kind_truncated,
                "ok": payload["ok"],
                "terminal": payload["terminal"],
            },
            "historical_evidence": _HISTORICAL_EVIDENCE,
        }

    def read(
        self,
        ref: str,
        offset: int = 0,
        max_bytes: int = 4096,
    ) -> dict[str, Any]:
        """Read a UTF-8-aligned chunk without changing retention order."""

        if not isinstance(ref, str):
            raise ObservationArchiveUnavailableError("observation unavailable")
        offset = _require_int(offset, "offset", minimum=0)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
            raise ObservationArchiveInvalidRequestError("max_bytes must be an integer")
        if not MIN_READ_BYTES <= max_bytes <= MAX_READ_BYTES:
            raise ObservationArchiveInvalidRequestError(
                f"max_bytes must be between {MIN_READ_BYTES} and {MAX_READ_BYTES}"
            )
        entry = self._entries.get(ref)
        if entry is None:
            raise ObservationArchiveUnavailableError("observation unavailable")
        if offset > len(entry.payload):
            raise ObservationArchiveOffsetError("offset is outside the observation")
        if offset < len(entry.payload) and entry.payload[offset] & 0xC0 == 0x80:
            raise ObservationArchiveOffsetError("offset is not UTF-8 aligned")
        end = min(offset + max_bytes, len(entry.payload))
        if end == offset:
            return {
                "ref": ref,
                "iteration": entry.iteration,
                "digest": entry.digest,
                "text": "",
                "offset": offset,
                "returned_bytes": 0,
                "next_offset": offset,
                "eof": True,
                "historical_evidence": _HISTORICAL_EVIDENCE,
            }
        while end > offset:
            try:
                text = entry.payload[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            raise ObservationArchiveOffsetError("read limit cannot contain a UTF-8 codepoint")
        returned = end - offset
        return {
            "ref": ref,
            "iteration": entry.iteration,
            "digest": entry.digest,
            "text": text,
            "offset": offset,
            "returned_bytes": returned,
            "next_offset": end,
            "eof": end == len(entry.payload),
            "historical_evidence": _HISTORICAL_EVIDENCE,
        }

    def find(
        self,
        ref: str,
        text: str,
        offset: int = 0,
        max_bytes: int = 4096,
    ) -> dict[str, Any]:
        """Find a literal UTF-8 byte sequence without changing retention."""

        if not isinstance(ref, str):
            raise ObservationArchiveUnavailableError("observation unavailable")
        if not isinstance(text, str) or not text:
            raise ObservationArchiveInvalidRequestError("text must be a nonempty string")
        try:
            query = text.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ObservationArchiveInvalidRequestError("text must be valid UTF-8") from error
        if len(query) > 256:
            raise ObservationArchiveInvalidRequestError("text must be at most 256 UTF-8 bytes")
        offset = _require_int(offset, "offset", minimum=0)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
            raise ObservationArchiveInvalidRequestError("max_bytes must be an integer")
        if not MIN_READ_BYTES <= max_bytes <= MAX_READ_BYTES:
            raise ObservationArchiveInvalidRequestError(
                f"max_bytes must be between {MIN_READ_BYTES} and {MAX_READ_BYTES}"
            )
        if len(query) > max_bytes:
            raise ObservationArchiveInvalidRequestError(
                "text must fit within the requested read limit"
            )
        entry = self._entries.get(ref)
        if entry is None:
            raise ObservationArchiveUnavailableError("observation unavailable")
        if offset > len(entry.payload):
            raise ObservationArchiveOffsetError("offset is outside the observation")
        if offset < len(entry.payload) and entry.payload[offset] & 0xC0 == 0x80:
            raise ObservationArchiveOffsetError("offset is not UTF-8 aligned")

        match_offset = entry.payload.find(query, offset)
        if match_offset < 0:
            return self._find_result(
                entry,
                ref=ref,
                found=False,
                text="",
                chunk_offset=offset,
                chunk_end=offset,
                match_offset=None,
                match_length=0,
                search_offset=offset,
                search_end_offset=len(entry.payload),
                next_search_offset=None,
            )

        match_end = match_offset + len(query)
        chunk_offset = max(0, match_offset - 128)
        chunk_offset = _utf8_start(entry.payload, chunk_offset)
        if match_end > chunk_offset + max_bytes:
            chunk_offset = match_end - max_bytes
            chunk_offset = _utf8_start(entry.payload, chunk_offset)
        chunk_end = min(len(entry.payload), chunk_offset + max_bytes)
        while chunk_end > match_end:
            try:
                chunk_text = entry.payload[chunk_offset:chunk_end].decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk_end -= 1
        else:
            chunk_text = entry.payload[chunk_offset:match_end].decode("utf-8")
            chunk_end = match_end
        return self._find_result(
            entry,
            ref=ref,
            found=True,
            text=chunk_text,
            chunk_offset=chunk_offset,
            chunk_end=chunk_end,
            match_offset=match_offset,
            match_length=len(query),
            search_offset=offset,
            search_end_offset=match_end,
            next_search_offset=match_end,
        )

    @staticmethod
    def _find_result(
        entry: _Entry,
        *,
        ref: str,
        found: bool,
        text: str,
        chunk_offset: int,
        chunk_end: int,
        match_offset: int | None,
        match_length: int,
        search_offset: int,
        search_end_offset: int,
        next_search_offset: int | None,
    ) -> dict[str, Any]:
        return {
            "ref": ref,
            "iteration": entry.iteration,
            "digest": entry.digest,
            "found": found,
            "text": text,
            "offset": chunk_offset,
            "returned_bytes": len(text.encode("utf-8")),
            "next_offset": chunk_end,
            "eof": chunk_end == len(entry.payload),
            "match_offset": match_offset,
            "match_length": match_length,
            "search_offset": search_offset,
            "search_end_offset": search_end_offset,
            "next_search_offset": next_search_offset,
            "search_complete": not found,
            "historical_evidence": _HISTORICAL_EVIDENCE,
        }

    def _new_ref(self) -> str:
        while True:
            ref = secrets.token_urlsafe(24)
            if ref not in self._entries:
                return ref


__all__ = [
    "DEFAULT_MAX_RECORD_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "MAX_READ_BYTES",
    "MAX_ACTION_KIND_BYTES",
    "MAX_SUMMARY_BYTES",
    "MIN_READ_BYTES",
    "ObservationArchive",
    "ObservationArchiveError",
    "ObservationArchiveInvalidRequestError",
    "ObservationArchiveOffsetError",
    "ObservationArchiveRecordTooLargeError",
    "ObservationArchiveStorageError",
    "ObservationArchiveUnavailableError",
]
