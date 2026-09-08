import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from uptick_agent.core.memory_models import (
    ConsolidationBatch,
    ConsolidationQuery,
    EpisodeRecord,
    EvidenceRef,
    LearningOperationResult,
    LessonGateResult,
    LessonProposal,
    MemoryCommit,
    MemoryView,
)
from uptick_agent.core.models import (
    CapabilityCall,
    CapabilityCatalog,
    FailureRecord,
    Observation,
    RunMetrics,
    RunResult,
    VerificationAssessment,
)
from uptick_agent.core.trace_models import (
    DecisionTracePayload,
    EpisodeClosedPayload,
    EpisodeCommittedPayload,
    LearningFailedPayload,
    LearningFinishedPayload,
    LearningStartedPayload,
    LessonActivatedPayload,
    LessonEvaluatedPayload,
    RunFailedPayload,
    RunFinishedPayload,
    RunStartedPayload,
    TraceEvent,
    TraceEventKindV5,
    TracePayloadV5,
)
from uptick_agent.store import InMemoryRunStore, JsonlRunStore

_HISTORICAL_TRACE = Path(__file__).parent / "trace_viewer" / "fixtures" / "valid-trace-v5.jsonl"


def _episode() -> EpisodeRecord:
    return EpisodeRecord(
        record_id="episode-1",
        environment_id="scripted",
        environment_profile_version="profile-1",
        evidence_group_id="run-1",
        run_id="run-1",
        step=1,
        situation_summary="a check was required",
        selected_action=CapabilityCall(name="inspect"),
        expected_result=["the state is returned"],
        observation_summary="the state was returned",
        verification=VerificationAssessment(status="confirmed", evidence=["state returned"]),
        evidence_refs=[EvidenceRef(stream_id="run:run-1", sequence=2, kind="decision_trace")],
    )


def _payloads() -> dict[TraceEventKindV5, TracePayloadV5]:
    view = MemoryView(database_id="db-1", revision=1)
    commit = MemoryCommit(
        applied=True,
        record_ids=["episode-1"],
        previous_revision=0,
        new_revision=1,
        reason="recorded",
    )
    failure = FailureRecord(stage="reasoner", error_type="RuntimeError", message="failed")
    query = ConsolidationQuery(
        trigger="after_run",
        view=view,
        environment_id="scripted",
        environment_profile_version="profile-1",
        min_evidence_groups=1,
    )
    batch = ConsolidationBatch(query=query, episodes=[_episode()])
    proposal = LessonProposal(
        claim="inspect before changing state",
        applies_when=["state is unknown"],
        evidence_episode_ids=["episode-1"],
    )
    result = RunResult(
        run_id="run-1",
        status="completed",
        steps=1,
        duration_seconds=1,
        stop_reason="done",
    )
    return {
        "run_started": RunStartedPayload(
            run_id="run-1",
            environment_id="scripted",
            environment_profile_version="profile-1",
            memory_view=view,
            initial_state={"health": "ok"},
        ),
        "decision_trace": DecisionTracePayload(
            run_id="run-1",
            iteration=1,
            context_projection={"objective": "keep healthy"},
            context_sha256="b" * 64,
            capabilities=CapabilityCatalog(items=[]),
            action=CapabilityCall(name="inspect"),
            observation=Observation(action_kind="inspect", summary="healthy"),
        ),
        "episode_closed": EpisodeClosedPayload(
            run_id="run-1",
            episode=_episode(),
            write_disposition="commit_requested",
        ),
        "episode_committed": EpisodeCommittedPayload(
            run_id="run-1",
            episode_id="episode-1",
            commit=commit,
        ),
        "run_failed": RunFailedPayload(
            run_id="run-1",
            failure_stage="reasoner",
            failure=failure,
            last_durable_sequence=2,
        ),
        "run_finished": RunFinishedPayload(
            run_id="run-1",
            result=result,
            metrics=RunMetrics(),
        ),
        "learning_started": LearningStartedPayload(
            learning_operation_id="learning-1",
            trigger="after_run",
            trigger_run_id="run-1",
            base_view=view,
        ),
        "lesson_evaluated": LessonEvaluatedPayload(
            learning_operation_id="learning-1",
            batch=batch,
            proposal=proposal,
            gate_result=LessonGateResult(accepted=True),
        ),
        "lesson_activated": LessonActivatedPayload(
            learning_operation_id="learning-1",
            lesson_id="lesson-1",
            commit=commit.model_copy(update={"record_ids": ["lesson-1"]}),
        ),
        "learning_failed": LearningFailedPayload(
            learning_operation_id="learning-1",
            failure=failure,
            last_durable_sequence=2,
        ),
        "learning_finished": LearningFinishedPayload(
            learning_operation_id="learning-1",
            result=LearningOperationResult(
                operation_id="learning-1",
                trigger="after_run",
                trigger_run_id="run-1",
                selected_run_ids=["run-1"],
                selected_evidence_group_ids=["run-1"],
                start_view=view,
                end_view=view,
                skipped_reason="no proposal",
            ),
        ),
    }


def test_every_trace_v5_payload_round_trips_with_its_kind() -> None:
    for kind, payload in _payloads().items():
        stream_kind = (
            "run"
            if kind.startswith("run_") or kind.startswith("episode_") or kind == "decision_trace"
            else "learning"
        )
        stream_id = "run:run-1" if stream_kind == "run" else "learning:learning-1"
        event = TraceEvent(
            schema_version=5,
            stream_id=stream_id,
            stream_kind=stream_kind,
            sequence=1,
            kind=kind,
            payload=payload,
        )
        restored = TraceEvent.model_validate_json(event.model_dump_json())
        assert restored.schema_version == 5
        if kind == "run_finished":
            assert isinstance(restored.payload, RunFinishedPayload)
            assert isinstance(payload, RunFinishedPayload)
            assert restored.payload.result == payload.result
            assert restored.payload.result_details == payload.result.model_dump(mode="json")
        else:
            assert restored == event


def test_jsonl_store_reads_actual_v5_environment_specific_result(tmp_path) -> None:
    (tmp_path / "trace.jsonl").write_text(
        _HISTORICAL_TRACE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    store = JsonlRunStore(tmp_path)

    events = asyncio.run(store.load_stream("run:run-a"))

    finished = events[-1].payload
    assert isinstance(finished, RunFinishedPayload)
    assert finished.result.run_id == "run-a"
    assert finished.result_details["balance_minor"] == 1_100
    assert finished.result_details["lost_revenue_minor"] == 300
    with pytest.raises(ValueError, match="legacy trace is read-only"):
        store.ensure_writable()


def test_v5_result_normalization_still_validates_common_result_fields() -> None:
    event = TraceEvent(
        schema_version=5,
        stream_id="run:run-1",
        stream_kind="run",
        sequence=1,
        kind="run_finished",
        payload=_payloads()["run_finished"],
    )
    raw = json.loads(event.model_dump_json())
    del raw["payload"]["result"]["duration_seconds"]
    raw["payload"]["result"]["balance_minor"] = 100

    with pytest.raises(ValidationError, match="duration_seconds"):
        TraceEvent.model_validate(raw)


def test_v5_result_normalization_rejects_conflicting_saved_details() -> None:
    event = TraceEvent(
        schema_version=5,
        stream_id="run:run-1",
        stream_kind="run",
        sequence=1,
        kind="run_finished",
        payload=_payloads()["run_finished"],
    )
    raw = json.loads(event.model_dump_json())
    raw["payload"]["result"]["balance_minor"] = 100
    raw["payload"]["result_details"] = {"balance_minor": 200}

    with pytest.raises(ValidationError, match="conflicts with result"):
        TraceEvent.model_validate(raw)


def test_v6_result_rejects_legacy_environment_specific_fields() -> None:
    event = TraceEvent(
        stream_id="run:run-1",
        stream_kind="run",
        sequence=1,
        kind="run_finished",
        payload=_payloads()["run_finished"],
    )
    raw = json.loads(event.model_dump_json())
    raw["payload"]["result"]["balance_minor"] = 100

    with pytest.raises(ValidationError, match="balance_minor"):
        TraceEvent.model_validate(raw)


def test_trace_v5_rejects_wrong_payload_and_stream_kind() -> None:
    payload = _payloads()["run_started"]
    with pytest.raises(ValidationError, match="does not match kind"):
        TraceEvent(
            stream_id="run:run-1",
            stream_kind="run",
            sequence=1,
            kind="run_failed",
            payload=payload,
        )
    with pytest.raises(ValidationError, match="requires 'run' stream"):
        TraceEvent(
            stream_id="learning:wrong",
            stream_kind="learning",
            sequence=1,
            kind="run_started",
            payload=payload,
        )


def test_learning_stream_can_follow_finished_run_without_extending_run_stream() -> None:
    async def scenario() -> None:
        store = InMemoryRunStore()
        payloads = _payloads()
        await store.record(
            TraceEvent(
                stream_id="run:run-1",
                stream_kind="run",
                sequence=1,
                kind="run_started",
                payload=payloads["run_started"],
            )
        )
        finished = payloads["run_finished"]
        assert isinstance(finished, RunFinishedPayload)
        await store.save_result(finished.result)
        await store.record(
            TraceEvent(
                stream_id="learning:learning-1",
                stream_kind="learning",
                sequence=1,
                kind="learning_started",
                payload=payloads["learning_started"],
            )
        )

        run_events = await store.load_stream("run:run-1")
        learning_events = await store.load_stream("learning:learning-1")
        assert [event.kind for event in run_events] == ["run_started", "run_finished"]
        assert [event.kind for event in learning_events] == ["learning_started"]

    asyncio.run(scenario())
