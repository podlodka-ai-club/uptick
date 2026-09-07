from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from uptick_agent.composition.sre_capacity import (
    CAPACITY_SETTINGS,
    project_capacity_observations,
)
from uptick_agent.memory.candidate_validation import validate_observed_evidence
from uptick_agent.memory.contracts import TransitionAssemblyRequest
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    StoredRecord,
)
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

NOW = datetime(2033, 3, 1, tzinfo=UTC)
_ASSEMBLER = DefaultExperienceTransitionAssembler()


def _log(
    *,
    request_id: str = "request-1",
    timestamp: str = "2033-03-01T00:00:01Z",
    error: str = "SERVER_CAPACITY_EXCEEDED",
    message: str = "server capacity exceeded: required=11 available=8",
    load_units: int | float = 11,
    status: int = 500,
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "timestamp": timestamp,
        "error": error,
        "message": message,
        "load_units": load_units,
        "status": status,
    }


def _source(
    iteration: int,
    logs: list[dict[str, object]],
    *,
    run_id: str = "run-capacity",
    occurred_at: datetime | None = None,
):
    return _ASSEMBLER.assemble(
        TransitionAssemblyRequest(
            transition_id=f"logs:{run_id}:{iteration}",
            run_id=run_id,
            iteration=iteration,
            occurred_at=occurred_at or NOW + timedelta(minutes=iteration),
            trust_classification="external_untrusted",
            observation={"summary": "public query result"},
            action={"kind": "query_logs"},
            result={"ok": True, "data": {"logs": logs, "next_cursor": None}},
            terminal=False,
        )
    )


def _evidence(transitions) -> LessonEvidence:
    records = [
        StoredRecord.from_write(
            RecordWrite(
                namespace="raw",
                record_id=transition.transition_id,
                record_type="experience-transition",
                payload=transition.model_dump(mode="json"),
                created_at=transition.occurred_at,
            )
        )
        for transition in transitions
    ]
    snapshot = MemorySnapshot.create(
        snapshot_id="snapshot:capacity",
        namespace="raw",
        members=[
            SnapshotMember(record_id=record.record_id, content_hash=record.content_hash)
            for record in records
        ],
    )
    return LessonEvidence(snapshot=snapshot, records=records, runs=[])


def test_projects_only_agreed_finite_capacity_numbers_and_keeps_source_proof() -> None:
    source = _source(1, [_log()])

    projected = project_capacity_observations(
        _evidence([source]), {source.run_id: source.occurred_at}
    )

    assert len(projected) == 1
    transition = projected[0]
    assert transition.observation == {
        "error_code": "SERVER_CAPACITY_EXCEEDED",
        "relation": "request_exceeds_remaining_capacity",
    }
    assert transition.action == {"kind": "request_admission"}
    assert transition.result["rejected"] is True
    assert transition.result["required_units"] == 11
    assert transition.result["available_units"] == 8
    assert transition.result["capacity_claim"] == "remaining_not_total"
    assert transition.result["source_record_hash"] == _evidence([source]).records[0].content_hash
    assert transition.occurred_at == source.occurred_at
    assert transition.iteration == source.iteration
    validate_observed_evidence(_evidence([transition]))


@pytest.mark.parametrize(
    "changes",
    [
        {"load_units": None},
        {"load_units": "nan"},
        {"load_units": -1},
        {"load_units": 10},
        {"request_id": ""},
        {"timestamp": ""},
        {"timestamp": "2033-03-01T00:00:01"},
        {"message": "server capacity exceeded: required=10 available=8"},
        {"message": "server capacity exceeded: required=11 available=11"},
    ],
)
def test_malformed_or_mismatched_logs_produce_no_projection(changes: dict[str, object]) -> None:
    row = _log()
    row.update(changes)
    source = _source(1, [row])

    assert (
        project_capacity_observations(_evidence([source]), {source.run_id: source.occurred_at})
        == []
    )


def test_overlapping_pages_keep_one_support_and_earliest_public_observation() -> None:
    event = _log()
    earlier = _source(1, [event])
    later = _source(2, [event])

    projected = project_capacity_observations(
        _evidence([later, earlier]), {earlier.run_id: later.occurred_at}
    )

    assert len(projected) == 1
    assert projected[0].iteration == earlier.iteration
    assert projected[0].result["source_transition_id"] == earlier.transition_id


def test_cutoff_excludes_later_public_log_pages_before_reading_rows() -> None:
    first = _source(1, [_log(request_id="first")])
    future = _source(2, [_log(request_id="future")])

    projected = project_capacity_observations(
        _evidence([first, future]), {first.run_id: first.occurred_at}
    )

    assert [item.result["request_id"] for item in projected] == ["first"]


def test_settings_project_descriptive_scope_and_rejected_result() -> None:
    assert CAPACITY_SETTINGS.scope_paths == (
        "observation.error_code",
        "observation.relation",
    )
    assert CAPACITY_SETTINGS.action_path == "action.kind"
    assert CAPACITY_SETTINGS.result_path == "result.rejected"
