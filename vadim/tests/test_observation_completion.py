from __future__ import annotations

import json

import pytest

from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.runs.observations import (
    MAX_EXACT_READ_RECORD_ID_BYTES,
    MAX_OBSERVATION_RECORD_BYTES,
    ObservationHistory,
)


def result() -> ToolResult:
    return ToolResult(
        action_kind="inspect",
        summary="Observed resource constraints",
        data={"padding": "界" * 220, "required": 18, "available": 6, "token": "secret"},
    )


def test_complete_evidence_is_opt_in_redacted_and_immutable() -> None:
    baseline = ObservationHistory()
    expanded = ObservationHistory(max_complete_record_bytes=2_000)
    action = {"kind": "inspect", "resource": "catalog", "api_key": "action-secret"}
    observation = result()
    for history in (baseline, expanded):
        history.record(1, action, observation)
    action["resource"] = "caller mutation"
    observation.data["required"] = 999

    assert json.loads(baseline.snapshot()[0])["_truncated"] is True
    restored = json.loads(expanded.snapshot()[0])
    assert restored["action"]["resource"] == "catalog"
    assert restored["result"]["data"]["required"] == 18
    assert restored["result"]["data"]["available"] == 6
    assert restored["result"]["data"]["token"] == "<redacted>"
    assert restored["action"]["api_key"] == "<redacted>"
    assert "secret" not in expanded.snapshot()[0]
    assert expanded.snapshot(exclude_iteration=1) == []
    assert expanded.snapshot() == expanded.snapshot()


def test_expansion_preserves_baseline_records_under_global_budget() -> None:
    baseline = ObservationHistory(max_serialized_bytes=3_000)
    expanded = ObservationHistory(max_serialized_bytes=3_000, max_complete_record_bytes=2_000)
    for iteration in range(1, 30):
        action = {"kind": "inspect", "resource": f"resource-{iteration}"}
        for history in (baseline, expanded):
            history.record(iteration, action, result())
        for excluded in (None, iteration):
            old = baseline.snapshot(exclude_iteration=excluded)
            new = expanded.snapshot(exclude_iteration=excluded)
            assert [json.loads(s)["iteration"] for s in new] == [
                json.loads(s)["iteration"] for s in old
            ]
            assert len(json.dumps(new, ensure_ascii=False, separators=(",", ":")).encode()) <= 3_000


def test_recent_complete_mode_replaces_older_previews_within_same_budget() -> None:
    baseline = ObservationHistory(max_serialized_bytes=4_500, max_complete_record_bytes=2_000)
    recent = ObservationHistory(
        max_serialized_bytes=4_500,
        max_complete_record_bytes=2_000,
        prefer_recent_complete=True,
    )
    for iteration in range(1, 9):
        action = {"kind": "inspect", "resource": f"resource-{iteration}"}
        observation = ToolResult(
            action_kind="inspect",
            summary="Observed",
            data={"padding": "界" * 300, "required": 18, "available": 6},
        )
        baseline.record(iteration, action, observation)
        recent.record(iteration, action, observation)

    baseline_payloads = [json.loads(item) for item in baseline.snapshot()]
    recent_payloads = [json.loads(item) for item in recent.snapshot()]
    assert [item["iteration"] for item in baseline_payloads] == [5, 6, 7, 8]
    assert [item["iteration"] for item in recent_payloads] == [6, 7, 8]
    assert all("_truncated" not in item for item in recent_payloads)
    assert (
        len(json.dumps(recent.snapshot(), ensure_ascii=False, separators=(",", ":")).encode())
        <= 4_500
    )


def test_recent_complete_mode_exclusion_does_not_change_recency() -> None:
    history = ObservationHistory(
        max_serialized_bytes=4_500,
        max_complete_record_bytes=2_000,
        prefer_recent_complete=True,
    )
    for iteration in range(1, 9):
        history.record(
            iteration,
            {"kind": "inspect", "resource": f"resource-{iteration}"},
            ToolResult(
                action_kind="inspect",
                summary="Observed",
                data={"padding": "界" * 300},
            ),
        )

    before = history.snapshot()
    excluded = history.snapshot(exclude_iteration=8)
    assert [json.loads(item)["iteration"] for item in excluded] == [5, 6, 7]
    assert history.snapshot() == before
    assert history.snapshot(exclude_iteration=8) == excluded


def test_recent_complete_mode_budgets_unicode_and_json_escapes() -> None:
    history = ObservationHistory(
        max_serialized_bytes=2_000,
        max_complete_record_bytes=2_000,
        prefer_recent_complete=True,
    )
    for iteration in range(1, 5):
        history.record(
            iteration,
            {"kind": "inspect", "query": '界\x00\\"' * 200},
            ToolResult(
                action_kind="inspect",
                summary='failed \x00\\" ' + "結果" * 400,
                data={"padding": "🙂" * 400},
                ok=False,
            ),
        )

    records = history.snapshot()
    assert records
    assert len(json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()) <= 2_000
    assert all(len(record.encode()) <= 2_000 for record in records)
    assert all("failed" in record for record in records)


def test_recent_complete_mode_default_is_byte_for_byte_legacy_path() -> None:
    implicit = ObservationHistory(max_serialized_bytes=4_500, max_complete_record_bytes=2_000)
    explicit = ObservationHistory(
        max_serialized_bytes=4_500,
        max_complete_record_bytes=2_000,
        prefer_recent_complete=False,
    )
    for iteration in range(1, 9):
        action = {"kind": "inspect", "resource": f"resource-{iteration}"}
        observation = ToolResult(
            action_kind="inspect",
            summary="Observed",
            data={"padding": "界" * 300},
        )
        implicit.record(iteration, action, observation)
        explicit.record(iteration, action, observation)
    assert implicit.snapshot() == explicit.snapshot()
    assert implicit.snapshot(exclude_iteration=8) == explicit.snapshot(exclude_iteration=8)


def test_repeated_action_replaces_complete_view_and_oversize_result_stays_truncated() -> None:
    history = ObservationHistory(max_complete_record_bytes=2_000, max_records=2)
    action = {"kind": "inspect"}
    history.record(1, action, result())
    history.record(2, {"kind": "other"}, result())
    history.record(
        3, action, ToolResult(action_kind="inspect", summary="Observed", data={"value": "new"})
    )
    records = [json.loads(s) for s in history.snapshot()]
    assert [s["iteration"] for s in records] == [2, 3]
    assert records[-1]["result"]["data"] == {"value": "new"}
    history.record(
        4,
        action,
        ToolResult(action_kind="inspect", summary="Observed", data={"value": "x" * 3_000}),
    )
    assert json.loads(history.snapshot()[-1])["_truncated"] is True
    assert len(history.snapshot()[-1].encode()) <= 1_000


def test_truncated_observation_carries_attached_exact_read_record_id() -> None:
    history = ObservationHistory(max_record_bytes=900)
    action = {"kind": "inspect", "resource": "catalog"}
    history.record(1, action, result())

    assert history.attach_exact_read_reference(1, action, "transition:run:1") is True

    payload = json.loads(history.snapshot()[0])
    assert payload["_truncated"] is True
    assert payload["exact_read_record_id"] == "transition:run:1"
    assert payload["result_summary"] == "Observed resource constraints"
    assert len(history.snapshot()[0].encode("utf-8")) <= MAX_OBSERVATION_RECORD_BYTES


def test_exact_read_attachment_is_scoped_to_current_exact_action_and_iteration() -> None:
    history = ObservationHistory(max_record_bytes=900)
    action = {"kind": "inspect", "resource": "catalog"}
    history.record(1, action, result())

    assert history.attach_exact_read_reference(2, action, "transition:old") is False
    history.record(2, action, result())
    assert history.attach_exact_read_reference(1, action, "transition:old") is False
    assert history.attach_exact_read_reference(2, action, "transition:current") is True
    assert json.loads(history.snapshot()[0])["exact_read_record_id"] == "transition:current"

    # A later replacement must not inherit the previous transition's authority.
    history.record(3, action, result())
    payload = json.loads(history.snapshot()[0])
    assert payload["iteration"] == 3
    assert payload["_truncated"] is True
    assert "exact_read_record_id" not in payload


def test_exact_read_attachment_retrims_escaped_history_to_aggregate_budget() -> None:
    history = ObservationHistory(max_record_bytes=900, max_serialized_bytes=1_980)
    first_action = {"kind": "inspect", "resource": "catalog-1"}
    second_action = {"kind": "inspect", "resource": "catalog-2"}
    history.record(1, first_action, result())
    history.record(2, second_action, result())
    before = history.snapshot()
    assert len(json.dumps(before, ensure_ascii=False, separators=(",", ":")).encode()) <= 1_980

    escaped_record_id = '"\\' * 100
    assert history.attach_exact_read_reference(2, second_action, escaped_record_id) is True

    records = history.snapshot()
    assert len(json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()) <= 1_980
    assert [json.loads(item)["iteration"] for item in records] == [2]
    assert json.loads(records[0])["exact_read_record_id"] == escaped_record_id


@pytest.mark.parametrize(
    "record_id",
    ["", "token=secret", "x" * (MAX_EXACT_READ_RECORD_ID_BYTES + 1)],
)
def test_exact_read_attachment_rejects_unbounded_or_credential_shaped_record_id(
    record_id: str,
) -> None:
    history = ObservationHistory()
    action = {"kind": "inspect"}
    history.record(1, action, result())
    with pytest.raises(ValueError):
        history.attach_exact_read_reference(1, action, record_id)


@pytest.mark.parametrize("invalid", [True, 999, 1_000.5, "2000"])
def test_complete_view_bound_rejects_invalid_values(invalid: object) -> None:
    with pytest.raises(ValueError):
        ObservationHistory(max_complete_record_bytes=invalid)  # type: ignore[arg-type]
