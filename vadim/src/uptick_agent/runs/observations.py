"""Bounded, redacted, run-local action/result observations."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from uptick_agent.decisions.runtime import ToolResult, serialize_bounded_json
from uptick_agent.redaction import sanitize_json

MAX_OBSERVATION_RECORD_BYTES = 1_000
MAX_OBSERVATION_HISTORY_BYTES = 8_000
MAX_OBSERVATION_HISTORY_RECORDS = 24
MAX_OBSERVATION_RESULT_SUMMARY_BYTES = 256
MAX_EXACT_READ_RECORD_ID_BYTES = 256
_TRUNCATED_SUMMARY_SUFFIX = "…[truncated]"


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _serialized_history_bytes(records: list[str]) -> int:
    return len(_compact_json(records).encode("utf-8"))


def _summary_excerpt(
    summary: str, *, max_bytes: int = MAX_OBSERVATION_RESULT_SUMMARY_BYTES
) -> tuple[str, bool]:
    encoded = summary.encode("utf-8")
    # Budget serialized bytes, including escaping (a control character can
    # occupy six JSON bytes). Metadata must leave room for the record body.
    if len(_compact_json(summary).encode("utf-8")) <= max_bytes:
        return summary, False
    suffix = _TRUNCATED_SUMMARY_SUFFIX.encode("utf-8")
    prefix = encoded[: max_bytes - len(suffix) - 2].decode("utf-8", errors="ignore")
    excerpt = prefix + _TRUNCATED_SUMMARY_SUFFIX
    while len(_compact_json(excerpt).encode("utf-8")) > max_bytes and prefix:
        prefix = prefix[:-1]
        excerpt = prefix + _TRUNCATED_SUMMARY_SUFFIX
    return excerpt, True


def _validate_exact_read_record_id(record_id: object) -> str:
    if not isinstance(record_id, str) or not record_id:
        raise ValueError("record_id must be a nonempty string")
    if len(record_id.encode("utf-8")) > MAX_EXACT_READ_RECORD_ID_BYTES:
        raise ValueError(f"record_id must be at most {MAX_EXACT_READ_RECORD_ID_BYTES} UTF-8 bytes")
    safe_record_id = sanitize_json(record_id)
    if safe_record_id != record_id:
        raise ValueError("record_id must not contain credential-shaped content")
    return record_id


def _attach_record_id(record: str, *, record_id: str, max_bytes: int) -> str | None:
    """Add a read reference while retaining the existing bounded prefix."""

    try:
        marker = json.loads(record)
    except (TypeError, ValueError):
        return None
    if not isinstance(marker, dict) or marker.get("_truncated") is not True:
        # Complete records already carry the exact result and do not need a
        # second access path.
        return None
    prefix = marker.get("_prefix")
    if not isinstance(prefix, str):
        return None
    marker["exact_read_record_id"] = record_id

    while True:
        marker["_prefix"] = prefix
        rendered = _compact_json(marker)
        if len(rendered.encode("utf-8")) <= max_bytes:
            return rendered
        if not prefix:
            return None
        prefix = prefix[:-1]


class ObservationHistory:
    """Keep one bounded record for each exact action payload.

    Recency changes only in ``record``, which the runner calls immediately
    after ``Environment.execute`` returns. Repeating an action therefore
    replaces its prior result and moves it to the observation tail. Attaching
    an exact-read reference may update or trim a value but never changes that
    recency; merely reading a snapshot never changes it either.
    """

    def __init__(
        self,
        *,
        max_record_bytes: int = MAX_OBSERVATION_RECORD_BYTES,
        max_serialized_bytes: int = MAX_OBSERVATION_HISTORY_BYTES,
        max_records: int = MAX_OBSERVATION_HISTORY_RECORDS,
        max_complete_record_bytes: int | None = None,
        prefer_recent_complete: bool = False,
    ) -> None:
        if (
            isinstance(max_record_bytes, bool)
            or not isinstance(max_record_bytes, int)
            or max_record_bytes < 1
        ):
            raise ValueError("max_record_bytes must be a positive integer")
        if (
            isinstance(max_serialized_bytes, bool)
            or not isinstance(max_serialized_bytes, int)
            or max_serialized_bytes < 1
        ):
            raise ValueError("max_serialized_bytes must be a positive integer")
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        if max_complete_record_bytes is None:
            max_complete_record_bytes = max_record_bytes
        if (
            isinstance(max_complete_record_bytes, bool)
            or not isinstance(max_complete_record_bytes, int)
            or max_complete_record_bytes < max_record_bytes
        ):
            raise ValueError("max_complete_record_bytes must be at least max_record_bytes")
        if not isinstance(prefer_recent_complete, bool):
            raise ValueError("prefer_recent_complete must be a boolean")
        self.max_record_bytes = max_record_bytes
        self.max_serialized_bytes = max_serialized_bytes
        self.max_records = max_records
        self.max_complete_record_bytes = max_complete_record_bytes
        self.prefer_recent_complete = prefer_recent_complete
        self._records: OrderedDict[str, tuple[int, str, str | None]] = OrderedDict()

    def record(
        self,
        iteration: int,
        action_payload: Mapping[str, Any],
        result: ToolResult,
    ) -> None:
        """Record one environment observation using immutable input snapshots."""

        if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 1:
            raise ValueError("iteration must be a positive integer")
        if not isinstance(action_payload, Mapping):
            raise TypeError("action_payload must be a mapping")
        if not isinstance(result, ToolResult):
            raise TypeError("result must be a ToolResult")

        # Materialise and redact the record; exact identity is hashed separately
        # below. The caller can safely mutate its models afterwards.
        safe_action = sanitize_json(dict(action_payload))
        if not isinstance(safe_action, dict):
            raise TypeError("action_payload must serialize to an object")
        safe_result = sanitize_json(
            result.model_dump(mode="json", round_trip=True, warnings="error")
        )
        if not isinstance(safe_result, dict):
            raise TypeError("result must serialize to an object")
        summary_excerpt, summary_truncated = _summary_excerpt(safe_result["summary"])
        kind_excerpt, kind_truncated = _summary_excerpt(safe_result["action_kind"], max_bytes=96)
        record_payload = {
            "action": safe_action,
            "iteration": iteration,
            "provenance": "environment.execute",
            "result": safe_result,
        }
        record = serialize_bounded_json(
            record_payload,
            max_bytes=self.max_record_bytes,
            truncation_metadata={
                "iteration": iteration,
                "provenance": "environment.execute",
                "result_action_kind": kind_excerpt,
                "result_action_kind_truncated": kind_truncated,
                "result_ok": safe_result["ok"],
                "result_summary": summary_excerpt,
                "result_summary_truncated": summary_truncated,
                "result_terminal": safe_result["terminal"],
            },
        )
        # Identity is based on the exact validated action, before redaction.
        # Retain only a fixed-size digest so credential-shaped parameters do
        # not collapse distinct actions or remain in process state.
        action_key = hashlib.sha256(_compact_json(action_payload).encode("utf-8")).hexdigest()
        complete = None
        if self.max_complete_record_bytes > self.max_record_bytes:
            candidate = _compact_json(record_payload)
            if (
                self.max_record_bytes
                < len(candidate.encode("utf-8"))
                <= (self.max_complete_record_bytes)
            ):
                complete = candidate
        self._records.pop(action_key, None)
        self._records[action_key] = (iteration, record, complete)
        self._trim()

    def attach_exact_read_reference(
        self,
        iteration: int,
        action_payload: Mapping[str, Any],
        record_id: str,
    ) -> bool:
        """Bind an already-authorized canonical record to a truncated preview.

        The history does not grant read access or resolve the identifier. The
        caller must obtain ``record_id`` from its run-local handoff authority;
        this method only carries that opaque identifier beside the bounded
        preview so a later decision can request the exact read.
        """

        if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 1:
            raise ValueError("iteration must be a positive integer")
        if not isinstance(action_payload, Mapping):
            raise TypeError("action_payload must be a mapping")
        record_id = _validate_exact_read_record_id(record_id)
        action_key = hashlib.sha256(_compact_json(action_payload).encode("utf-8")).hexdigest()
        current = self._records.get(action_key)
        if current is None or current[0] != iteration:
            return False
        attached = _attach_record_id(
            current[1], record_id=record_id, max_bytes=self.max_record_bytes
        )
        if attached is None:
            return False
        self._records[action_key] = (iteration, attached, current[2])
        self._trim()
        retained = self._records.get(action_key)
        return retained is not None and retained[1] == attached

    def snapshot(self, *, exclude_iteration: int | None = None) -> list[str]:
        """Return insertion/LRU order without exposing mutable internal state."""

        selected = [row for row in self._records.values() if row[0] != exclude_iteration]
        records = [record for _iteration, record, _complete in selected]
        if self.prefer_recent_complete:
            chosen: list[str] = []
            for _iteration, preview, complete in reversed(selected):
                preferred = complete or preview
                candidate = [*chosen, preferred]
                if (
                    len(candidate) <= self.max_records
                    and _serialized_history_bytes(candidate) <= self.max_serialized_bytes
                ):
                    chosen.append(preferred)
                    continue
                candidate = [*chosen, preview]
                if (
                    len(candidate) <= self.max_records
                    and _serialized_history_bytes(candidate) <= self.max_serialized_bytes
                ):
                    chosen.append(preview)
                    continue
                break
            chosen.reverse()
            return chosen
        # Spend only spare context budget on complete, already-redacted evidence.
        # Baseline selection and order remain unchanged; no record is evicted
        # to expand another, and a larger arbitrary prefix is never substituted.
        for index in reversed(range(len(selected))):
            complete = selected[index][2]
            if complete is None:
                continue
            baseline = records[index]
            records[index] = complete
            if _serialized_history_bytes(records) > self.max_serialized_bytes:
                records[index] = baseline
        return records

    def _trim(self) -> None:
        while self._records and (
            len(self._records) > self.max_records
            or _serialized_history_bytes(
                [record for _iteration, record, _complete in self._records.values()]
            )
            > self.max_serialized_bytes
        ):
            self._records.popitem(last=False)


__all__ = [
    "MAX_OBSERVATION_HISTORY_BYTES",
    "MAX_OBSERVATION_HISTORY_RECORDS",
    "MAX_OBSERVATION_RECORD_BYTES",
    "MAX_OBSERVATION_RESULT_SUMMARY_BYTES",
    "MAX_EXACT_READ_RECORD_ID_BYTES",
    "ObservationHistory",
]
