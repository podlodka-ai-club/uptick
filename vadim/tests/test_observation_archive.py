from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.redaction import sanitize_json
from uptick_agent.runs.observation_archive import (
    MAX_ACTION_KIND_BYTES,
    MAX_READ_BYTES,
    MAX_SUMMARY_BYTES,
    MIN_READ_BYTES,
    ObservationArchive,
    ObservationArchiveInvalidRequestError,
    ObservationArchiveOffsetError,
    ObservationArchiveRecordTooLargeError,
    ObservationArchiveUnavailableError,
)


def _result(*, summary: str = "catalog returned", **data: object) -> ToolResult:
    return ToolResult(action_kind="catalog", summary=summary, data=data)


def _read_all(archive: ObservationArchive, ref: str, *, limit: int = 7) -> str:
    offset = 0
    chunks: list[str] = []
    while True:
        receipt = archive.read(ref, offset=offset, max_bytes=limit)
        chunks.append(receipt["text"])
        if receipt["eof"]:
            return "".join(chunks)
        offset = receipt["next_offset"]


def test_record_and_read_reconstruct_sanitized_utf8_payload() -> None:
    archive = ObservationArchive()
    secret = "synthetic archive secret"
    result = _result(
        summary="finished 🙂",
        message='quoted: "value"',
        unicode="zażółć gęślą jaźń 🚀",
        api_key=secret,
    )

    receipt = archive.record(17, result)
    expected = json.dumps(
        sanitize_json(result.model_dump(mode="json", round_trip=True, warnings="error")),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert receipt["source_iteration"] == 17
    assert receipt["bytes"] == len(expected.encode("utf-8"))
    assert receipt["digest"] == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert _read_all(archive, receipt["ref"]) == expected
    assert secret not in _read_all(archive, receipt["ref"])

    eof = archive.read(receipt["ref"], offset=receipt["bytes"], max_bytes=MIN_READ_BYTES)
    assert eof["text"] == ""
    assert eof["eof"] is True
    assert eof["historical_evidence"] == receipt["historical_evidence"]


def test_record_snapshots_result_and_receipt_has_bounded_historical_summary() -> None:
    archive = ObservationArchive()
    secret = "summary-secret"
    result = _result(summary=("token=" + secret + " ") + "結果" * 1_000, nested={"rows": [1]})
    receipt = archive.record(3, result)

    result.data["nested"]["rows"].append(2)
    result.summary = "caller mutation"
    assert "caller mutation" not in _read_all(archive, receipt["ref"])
    assert len(receipt["summary"].encode("utf-8")) <= MAX_SUMMARY_BYTES
    assert receipt["summary_truncated"] is True
    assert secret not in json.dumps(receipt, ensure_ascii=False)
    assert receipt["historical_evidence"] == "stale_environment_observation"


def test_receipt_bounds_json_escaped_kind_and_summary() -> None:
    archive = ObservationArchive()
    result = ToolResult(
        action_kind='kind\n"🙂"' * 1_000,
        summary='summary\n"🙂"' * 1_000,
    )
    receipt = archive.record(4, result)
    status = receipt["status"]
    assert status["action_kind_truncated"] is True
    assert len(json.dumps(status["action_kind"], ensure_ascii=True).encode()) <= (
        MAX_ACTION_KIND_BYTES
    )
    assert receipt["summary_truncated"] is True
    assert len(json.dumps(receipt["summary"], ensure_ascii=True).encode()) <= (MAX_SUMMARY_BYTES)
    chunk = archive.read(receipt["ref"], max_bytes=MIN_READ_BYTES)
    assert chunk["historical_evidence"] == receipt["historical_evidence"]


def test_megabyte_scale_kind_and_summary_have_bounded_receipt_fields() -> None:
    archive = ObservationArchive(max_record_bytes=3_000_000)
    receipt = archive.record(
        5,
        ToolResult(
            action_kind="kind" * 250_000,
            summary="summary" * 250_000,
        ),
    )

    assert receipt["status"]["action_kind_truncated"] is True
    assert len(json.dumps(receipt["status"]["action_kind"], ensure_ascii=True).encode()) <= (
        MAX_ACTION_KIND_BYTES
    )
    assert receipt["summary_truncated"] is True
    assert len(json.dumps(receipt["summary"], ensure_ascii=True).encode()) <= (MAX_SUMMARY_BYTES)


def test_refs_are_opaque_run_local_and_pathlike_values_are_unavailable() -> None:
    first = ObservationArchive()
    second = ObservationArchive()
    ref = first.record(1, _result())["ref"]

    with pytest.raises(ObservationArchiveUnavailableError):
        second.read(ref)
    with pytest.raises(ObservationArchiveUnavailableError):
        first.read(Path(ref))  # type: ignore[arg-type]
    with pytest.raises(ObservationArchiveUnavailableError):
        first.read("/tmp/observation.json")


def test_quota_evicts_oldest_and_oversized_record_fails_without_a_ref() -> None:
    probe = ObservationArchive()
    first_size = probe.record(1, _result(value="first"))["bytes"]
    archive = ObservationArchive(max_total_bytes=first_size + 1, max_record_bytes=1_000)
    first = archive.record(1, _result(value="first"))
    second_result = _result(value="second")
    second = archive.record(2, second_result)

    with pytest.raises(ObservationArchiveUnavailableError):
        archive.read(first["ref"])
    expected_second = json.dumps(
        sanitize_json(second_result.model_dump(mode="json", round_trip=True, warnings="error")),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert _read_all(archive, second["ref"]) == expected_second

    too_small = ObservationArchive(max_record_bytes=64)
    with pytest.raises(ObservationArchiveRecordTooLargeError):
        too_small.record(1, _result(value="x" * 1_000))


def test_read_validates_limits_offsets_and_utf8_alignment() -> None:
    archive = ObservationArchive()
    receipt = archive.record(9, _result(text="ascii 🙂 tail"))
    ref = receipt["ref"]

    for limit in (MIN_READ_BYTES - 1, MAX_READ_BYTES + 1, True):
        with pytest.raises(ObservationArchiveInvalidRequestError):
            archive.read(ref, max_bytes=limit)  # type: ignore[arg-type]
    for offset in (-1, True):
        with pytest.raises(ObservationArchiveInvalidRequestError):
            archive.read(ref, offset=offset)  # type: ignore[arg-type]
    with pytest.raises(ObservationArchiveOffsetError):
        archive.read(ref, offset=receipt["bytes"] + 1)

    payload = _read_all(archive, ref).encode("utf-8")
    smile = payload.index("🙂".encode())
    with pytest.raises(ObservationArchiveOffsetError):
        archive.read(ref, offset=smile + 1)
    chunk = archive.read(ref, offset=smile, max_bytes=MIN_READ_BYTES)
    assert chunk["text"] == "🙂"
    assert chunk["returned_bytes"] == len("🙂".encode())


def test_find_is_literal_unicode_and_escaped_json_with_bounded_context() -> None:
    archive = ObservationArchive()
    result = _result(
        message='prefix "needle🙂" suffix needle',
        escaped='quoted: "value"',
    )
    receipt = archive.record(21, result)
    expected = _read_all(archive, receipt["ref"]).encode("utf-8")
    query = r"\"needle🙂\""

    found = archive.find(receipt["ref"], query, max_bytes=32)
    query_bytes = query.encode("utf-8")
    assert found["found"] is True
    assert found["iteration"] == receipt["source_iteration"]
    assert found["digest"] == receipt["digest"]
    assert found["match_offset"] == expected.index(query_bytes)
    assert found["match_length"] == len(query_bytes)
    assert found["search_offset"] == 0
    assert found["search_end_offset"] == found["match_offset"] + len(query_bytes)
    assert found["next_search_offset"] == found["search_end_offset"]
    assert found["search_complete"] is False
    assert query in found["text"]
    assert found["returned_bytes"] <= 32
    assert found["historical_evidence"] == receipt["historical_evidence"]

    unescaped = archive.find(receipt["ref"], '"needle🙂"', max_bytes=32)
    assert unescaped["found"] is False


def test_find_supports_multiple_matches_and_explicit_no_match_coverage() -> None:
    archive = ObservationArchive()
    receipt = archive.record(22, _result(message="needle one needle two"))
    first = archive.find(receipt["ref"], "needle", max_bytes=64)
    second = archive.find(
        receipt["ref"],
        "needle",
        offset=first["next_search_offset"],
        max_bytes=64,
    )
    assert first["found"] is second["found"] is True
    assert second["match_offset"] > first["match_offset"]
    assert second["search_offset"] == first["next_search_offset"]

    missing = archive.find(receipt["ref"], "absent", offset=first["match_offset"])
    assert missing["found"] is False
    assert missing["match_offset"] is None
    assert missing["next_search_offset"] is None
    assert missing["search_complete"] is True
    assert missing["search_offset"] == first["match_offset"]
    assert missing["search_end_offset"] == receipt["bytes"]
    assert missing["text"] == ""
    assert missing["historical_evidence"] == receipt["historical_evidence"]


def test_find_validates_query_caps_offsets_and_ref_scope_without_retention_touch() -> None:
    first_result = _result(message="prefix 🙂 needle first")
    second_result = _result(message="stable second")
    probe = ObservationArchive()
    first_size = probe.record(1, first_result)["bytes"]
    second_size = probe.record(2, second_result)["bytes"]
    archive = ObservationArchive(
        max_total_bytes=first_size + second_size + 1,
        max_record_bytes=1_000,
    )
    first = archive.record(1, first_result)
    second = archive.record(2, second_result)

    expected_first = _read_all(archive, first["ref"]).encode("utf-8")
    expected_second = _read_all(archive, second["ref"])
    found = archive.find(first["ref"], "🙂", offset=0, max_bytes=MIN_READ_BYTES)
    assert found["found"] is True
    assert found["returned_bytes"] == len("🙂".encode())
    assert found["text"] == expected_first[found["offset"] : found["next_offset"]].decode("utf-8")

    with pytest.raises(ObservationArchiveInvalidRequestError):
        archive.find(first["ref"], "")
    with pytest.raises(ObservationArchiveInvalidRequestError):
        archive.find(first["ref"], "🙂" * 65)
    with pytest.raises(ObservationArchiveInvalidRequestError):
        archive.find(first["ref"], "longer", max_bytes=4)
    for limit in (MIN_READ_BYTES - 1, MAX_READ_BYTES + 1, True):
        with pytest.raises(ObservationArchiveInvalidRequestError):
            archive.find(first["ref"], "needle", max_bytes=limit)  # type: ignore[arg-type]
    for offset in (-1, True):
        with pytest.raises(ObservationArchiveInvalidRequestError):
            archive.find(first["ref"], "needle", offset=offset)  # type: ignore[arg-type]
    with pytest.raises(ObservationArchiveOffsetError):
        archive.find(first["ref"], "needle", offset=first["bytes"] + 1)

    archive.record(3, first_result)
    with pytest.raises(ObservationArchiveUnavailableError):
        archive.find(first["ref"], "needle")
    assert _read_all(archive, second["ref"]) == expected_second
    with pytest.raises(ObservationArchiveUnavailableError):
        ObservationArchive().find(second["ref"], "needle")
