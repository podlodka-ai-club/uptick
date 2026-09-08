from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.helpers import decode_prompt_context, inspect_catalog
from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.bootstrap_models import ToolMetadata, ToolRegistry
from uptick_agent.core.errors import ReasonerFailure, RunExecutionError, RunStoreFailure
from uptick_agent.core.memory_models import (
    ConsolidationBatch,
    ConsolidationOutput,
    ConsolidationQuery,
    EpisodeRecord,
    EvidenceRef,
    LessonProposal,
    LessonRecord,
    LessonValidation,
    MemoryViewRequest,
)
from uptick_agent.core.models import (
    CapabilityCall,
    Observation,
    ReasoningTelemetry,
    RunResult,
    RunSpec,
    VerificationAssessment,
    VerificationStatus,
)
from uptick_agent.core.policy import DecisionPolicy
from uptick_agent.core.sgr import CurrentSGR
from uptick_agent.core.trace_models import (
    LearningFinishedPayload,
    LessonEvaluatedPayload,
    RunStartedPayload,
    TraceEvent,
)
from uptick_agent.environments.scripted import ScriptedEnvironment
from uptick_agent.learning import LearningOrchestrator, MemoryConsolidator
from uptick_agent.learning.gates import evaluate_lesson_proposal
from uptick_agent.memory import SQLiteMemory
from uptick_agent.reasoners.scripted import ScriptedReasoner
from uptick_agent.runtime.bootstrap import ScriptedEnvironmentBootstrapper
from uptick_agent.runtime.episodes import episode_record_id, evidence_group_id
from uptick_agent.runtime.runner import AgentRunner
from uptick_agent.store import InMemoryRunStore, JsonlRunStore


def _episode(
    record_id: str,
    run_id: str,
    *,
    status: VerificationStatus = "confirmed",
    evidence_group: str | None = None,
) -> EpisodeRecord:
    return EpisodeRecord(
        record_id=record_id,
        environment_id="scripted",
        environment_profile_version="profile-1",
        evidence_group_id=evidence_group or run_id,
        run_id=run_id,
        step=1,
        situation_summary="capacity needed inspection",
        selected_action=CapabilityCall(name="inspect"),
        expected_result=["capacity becomes observable"],
        observation_summary="capacity is healthy",
        verification=VerificationAssessment(
            status=status,
            evidence=["the observation reported healthy capacity"],
        ),
        evidence_refs=[
            EvidenceRef(
                stream_id=f"run:{run_id}",
                sequence=2,
                kind="decision_trace",
            )
        ],
    )


def _proposal(*episode_ids: str) -> dict:
    return {
        "claim": "inspect capacity before changing it",
        "applies_when": ["capacity is uncertain"],
        "exceptions": [],
        "capability_names": ["inspect"],
        "evidence_episode_ids": list(episode_ids),
        "contradicting_episode_ids": [],
        "supersedes": [],
    }


def _learner_output(*episode_ids: str) -> dict:
    return {"proposal": _proposal(*episode_ids), "no_lesson": None}


def _no_lesson_output(reason: str) -> dict:
    return {"proposal": None, "no_lesson": {"reason": reason}}


def _registry() -> ToolRegistry:
    return ToolRegistry(
        items=[ToolMetadata(capability_name="inspect", purpose="inspect current capacity")]
    )


def _decision_output(
    name: str,
    *,
    completed: bool = False,
    previous: bool = False,
    pending: bool = False,
) -> dict:
    previous_status = "pending" if pending else ("confirmed" if previous else "not_applicable")
    return {
        "phase": "finish" if completed else "observe",
        "facts": ["scripted state"],
        "competing_hypotheses": ["inspection is appropriate"],
        "contradicting_evidence": [],
        "previous_verification": {
            "status": previous_status,
            "evidence": ["the prior observation arrived"] if previous or pending else [],
        },
        "strategy": "follow the scripted sequence",
        "selected_action": {
            "name": name,
            "arguments": {"reason": "done"} if name == "finish" else {},
        },
        "expected_result": ["the scripted observation arrives"],
        "verification": ["compare the following observation"],
        "task_completed": completed,
    }


def test_consolidation_output_requires_exactly_one_result() -> None:
    assert set(ConsolidationOutput.model_json_schema()["required"]) == {
        "proposal",
        "no_lesson",
    }
    with pytest.raises(ValueError, match="exactly one"):
        ConsolidationOutput.model_validate({"proposal": None, "no_lesson": None})
    with pytest.raises(ValueError, match="exactly one"):
        ConsolidationOutput.model_validate(
            {
                "proposal": _proposal("episode-1"),
                "no_lesson": {"reason": "the existing lesson already covers this evidence"},
            }
        )

    proposal = ConsolidationOutput.model_validate(_learner_output("episode-1"))
    no_lesson = ConsolidationOutput.model_validate(
        _no_lesson_output("the existing lesson already covers this evidence")
    )
    assert proposal.proposal is not None and proposal.no_lesson is None
    assert no_lesson.proposal is None and no_lesson.no_lesson is not None


async def _seed_memory(memory: SQLiteMemory, episodes: list[EpisodeRecord]):
    view = await memory.resolve_view(
        MemoryViewRequest(
            environment_id="scripted",
            environment_profile_version="profile-1",
        )
    )
    assert view.revision == 0
    for episode in episodes:
        base_revision = view.revision
        assert base_revision is not None
        commit = await memory.record_episode(episode, base_revision=base_revision)
        assert commit.new_revision is not None
        view = view.model_copy(update={"revision": commit.new_revision})
    return view


def test_after_run_consolidation_activates_a_validated_lesson(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        view = await _seed_memory(
            memory,
            [_episode("episode-1", "run-1"), _episode("episode-2", "run-2")],
        )
        store = InMemoryRunStore()
        learner = ScriptedReasoner([_learner_output("episode-1", "episode-2")])
        orchestrator = LearningOrchestrator(
            memory=memory,
            run_store=store,
            consolidator=MemoryConsolidator(reasoner=learner),
            trigger="after_run",
            min_evidence_groups=2,
        )

        result = await orchestrator.consolidate(
            trigger_run_id="run-2",
            trigger_episode_id=None,
            memory_view=view,
            environment_id="scripted",
            environment_profile_version="profile-1",
            registry=_registry(),
        )

        assert result.commit is not None and result.commit.applied
        assert result.selected_run_ids == ["run-1", "run-2"]
        assert result.selected_evidence_group_ids == ["run-1", "run-2"]
        assert result.learner_telemetry is not None
        assert result.end_view.revision == 3
        assert [event.kind for event in store.events if event.stream_kind == "learning"] == [
            "learning_started",
            "lesson_evaluated",
            "lesson_activated",
            "learning_finished",
        ]
        learner_batch = json.loads(learner.requests[0].user_prompt.split("\n", 1)[1])["batch"]
        assert set(learner_batch) == {"query", "episodes", "active_lessons"}
        assert [episode["record_id"] for episode in learner_batch["episodes"]] == [
            "episode-1",
            "episode-2",
        ]
        assert "highest expected future\nvalue" in learner.requests[0].system_prompt
        assert "Do not weaken the evidence\nrequirements" in learner.requests[0].system_prompt
        current = await memory.resolve_view(
            MemoryViewRequest(
                environment_id="scripted",
                environment_profile_version="profile-1",
            )
        )
        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=current,
                environment_id="scripted",
                environment_profile_version="profile-1",
                min_evidence_groups=2,
            )
        )
        assert batch.episodes == []
        assert len(batch.active_lessons) == 1

    asyncio.run(scenario())


def test_after_run_no_lesson_is_a_successful_skip_without_memory_write(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        view = await _seed_memory(
            memory,
            [_episode("episode-1", "run-1"), _episode("episode-2", "run-2")],
        )
        store = InMemoryRunStore()
        reason = "the active knowledge already covers the only durable principle"
        learner = ScriptedReasoner([_no_lesson_output(reason)])

        result = await LearningOrchestrator(
            memory=memory,
            run_store=store,
            consolidator=MemoryConsolidator(reasoner=learner),
            trigger="after_run",
            min_evidence_groups=2,
        ).consolidate(
            trigger_run_id="run-2",
            trigger_episode_id=None,
            memory_view=view,
            environment_id="scripted",
            environment_profile_version="profile-1",
            registry=_registry(),
        )

        current = await memory.resolve_view(
            MemoryViewRequest(
                environment_id="scripted",
                environment_profile_version="profile-1",
            )
        )
        assert result.skipped_reason == reason
        assert result.start_view == result.end_view == view
        assert result.proposal is None
        assert result.gate_result is None
        assert result.commit is None
        assert result.learner_telemetry is not None
        assert current.revision == view.revision
        assert [event.kind for event in store.events if event.stream_kind == "learning"] == [
            "learning_started",
            "learning_finished",
        ]

    asyncio.run(scenario())


def test_after_run_skips_insufficient_evidence_without_calling_learner(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        view = await _seed_memory(memory, [_episode("episode-1", "run-1")])
        store = InMemoryRunStore()
        learner = ScriptedReasoner([])
        result = await LearningOrchestrator(
            memory=memory,
            run_store=store,
            consolidator=MemoryConsolidator(reasoner=learner),
            trigger="after_run",
            min_evidence_groups=2,
        ).consolidate(
            trigger_run_id="run-1",
            trigger_episode_id=None,
            memory_view=view,
            environment_id="scripted",
            environment_profile_version="profile-1",
            registry=_registry(),
        )

        assert result.skipped_reason == "insufficient independent evidence groups"
        assert learner.requests == []
        assert [event.kind for event in store.events if event.stream_kind == "learning"] == [
            "learning_started",
            "learning_finished",
        ]

    asyncio.run(scenario())


def test_legacy_outcomes_roundtrip_in_trace_but_never_enter_learner_input(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        view = await _seed_memory(memory, [_episode("episode-1", "run-1")])
        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=view,
                environment_id="scripted",
                environment_profile_version="profile-1",
                min_evidence_groups=1,
            )
        )
        legacy_batch = ConsolidationBatch.model_validate(
            batch.model_dump(mode="json")
            | {
                "run_outcomes": [
                    {
                        "run_id": "run-1",
                        "status": "running",
                        "steps": 160,
                        "duration_seconds": 1,
                        "stop_reason": "legacy-limit-marker",
                        "forced": True,
                        "result": {"legacy_metric": 123},
                    }
                ]
            }
        )
        event = TraceEvent(
            stream_id="learning:legacy",
            stream_kind="learning",
            sequence=1,
            kind="lesson_evaluated",
            payload=LessonEvaluatedPayload(learning_operation_id="legacy", batch=legacy_batch),
        )
        restored = TraceEvent.model_validate_json(event.model_dump_json())
        assert restored == event
        assert isinstance(restored.payload, LessonEvaluatedPayload)
        learner = ScriptedReasoner([_no_lesson_output("insufficient new evidence")])
        await MemoryConsolidator(reasoner=learner).consolidate(restored.payload.batch, _registry())
        prompt = json.loads(learner.requests[0].user_prompt.split("\n", 1)[1])
        assert set(prompt["batch"]) == {"query", "episodes", "active_lessons"}
        assert prompt["batch"]["episodes"] == batch.model_dump(mode="json")["episodes"]
        assert "legacy-limit-marker" not in learner.requests[0].user_prompt
        assert "legacy_metric" not in learner.requests[0].user_prompt

    asyncio.run(scenario())


def test_repeated_runs_of_one_world_do_not_confirm_a_lesson(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        shared_group = "opaque-world-group"
        view = await _seed_memory(
            memory,
            [
                _episode("episode-1", "run-1", evidence_group=shared_group),
                _episode("episode-2", "run-2", evidence_group=shared_group),
            ],
        )
        store = InMemoryRunStore()
        learner = ScriptedReasoner([])

        result = await LearningOrchestrator(
            memory=memory,
            run_store=store,
            consolidator=MemoryConsolidator(reasoner=learner),
            trigger="after_run",
            min_evidence_groups=2,
        ).consolidate(
            trigger_run_id="run-2",
            trigger_episode_id=None,
            memory_view=view,
            environment_id="scripted",
            environment_profile_version="profile-1",
            registry=_registry(),
        )

        assert result.skipped_reason == "insufficient independent evidence groups"
        assert result.selected_run_ids == ["run-1", "run-2"]
        assert result.selected_evidence_group_ids == [shared_group]
        assert learner.requests == []

    asyncio.run(scenario())


def test_inline_learning_fails_closed_for_missing_or_foreign_trigger_episode(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        view = await _seed_memory(memory, [_episode("episode-1", "run-1")])
        store = InMemoryRunStore()
        learner = ScriptedReasoner([])
        orchestrator = LearningOrchestrator(
            memory=memory,
            run_store=store,
            consolidator=MemoryConsolidator(reasoner=learner),
            trigger="after_closed_episode",
            min_evidence_groups=1,
        )

        missing = await orchestrator.consolidate(
            trigger_run_id="run-1",
            trigger_episode_id="episode-missing",
            memory_view=view,
            environment_id="scripted",
            environment_profile_version="profile-1",
            registry=_registry(),
        )
        foreign = await orchestrator.consolidate(
            trigger_run_id="run-other",
            trigger_episode_id="episode-1",
            memory_view=view,
            environment_id="scripted",
            environment_profile_version="profile-1",
            registry=_registry(),
        )

        assert missing.skipped_reason == "learning failed"
        assert missing.failure is not None
        assert "exactly its trigger Episode" in missing.failure.message
        assert foreign.skipped_reason == "learning failed"
        assert foreign.failure is not None
        assert "does not belong to its trigger run" in foreign.failure.message
        assert learner.requests == []

    asyncio.run(scenario())


def test_after_run_discovers_prior_uncovered_episodes_without_scheduler_history(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        view = await _seed_memory(memory, [_episode("episode-1", "run-1")])
        store = InMemoryRunStore()
        await store.save_result(
            RunResult(
                run_id="run-1",
                status="completed",
                steps=3,
                duration_seconds=1,
                stop_reason="completed",
            )
        )
        learner = ScriptedReasoner([_learner_output("episode-1", "episode-2")])
        orchestrator = LearningOrchestrator(
            memory=memory,
            run_store=store,
            consolidator=MemoryConsolidator(reasoner=learner),
            trigger="after_run",
            min_evidence_groups=2,
        )

        first = await orchestrator.consolidate(
            trigger_run_id="run-1",
            trigger_episode_id=None,
            memory_view=view,
            environment_id="scripted",
            environment_profile_version="profile-1",
            registry=_registry(),
        )

        assert first.skipped_reason == "insufficient independent evidence groups"
        assert first.selected_run_ids == ["run-1"]
        assert learner.requests == []

        assert view.revision is not None
        commit = await memory.record_episode(
            _episode("episode-2", "run-2"),
            base_revision=view.revision,
        )
        assert commit.new_revision is not None
        current = view.model_copy(update={"revision": commit.new_revision})
        await store.save_result(
            RunResult(
                run_id="run-2",
                status="completed",
                steps=4,
                duration_seconds=1,
                stop_reason="completed",
            )
        )

        second = await orchestrator.consolidate(
            trigger_run_id="run-2",
            trigger_episode_id=None,
            memory_view=current,
            environment_id="scripted",
            environment_profile_version="profile-1",
            registry=_registry(),
        )

        assert second.commit is not None and second.commit.applied
        assert second.selected_run_ids == ["run-1", "run-2"]
        assert second.selected_evidence_group_ids == ["run-1", "run-2"]
        assert len(learner.requests) == 1

    asyncio.run(scenario())


def test_after_run_learns_from_shared_memory_without_reading_prior_trace_directories(
    tmp_path: Path,
) -> None:
    class RecordingOnlyStore(JsonlRunStore):
        async def load_stream(self, stream_id):
            raise AssertionError("learning must not read operational traces")

    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        view = await _seed_memory(
            memory,
            [_episode("episode-1", "run-1"), _episode("episode-2", "run-2")],
        )
        prior_store = JsonlRunStore(tmp_path / "world-1")
        await prior_store.save_result(
            RunResult(
                run_id="run-1",
                status="completed",
                steps=3,
                duration_seconds=1,
                stop_reason="completed",
            )
        )
        prior_trace = prior_store.path.read_bytes()
        store = RecordingOnlyStore(tmp_path / "world-2")
        learner = ScriptedReasoner([_learner_output("episode-1", "episode-2")])

        result = await LearningOrchestrator(
            memory=memory,
            run_store=store,
            consolidator=MemoryConsolidator(reasoner=learner),
            trigger="after_run",
            min_evidence_groups=2,
        ).consolidate(
            trigger_run_id="run-2",
            trigger_episode_id=None,
            memory_view=view,
            environment_id="scripted",
            environment_profile_version="profile-1",
            registry=_registry(),
        )

        assert result.failure is None and result.skipped_reason is None
        assert result.commit is not None and result.commit.applied
        assert result.end_view.revision == 3
        assert result.selected_run_ids == ["run-1", "run-2"]
        assert len(learner.requests) == 1
        assert prior_store.path.read_bytes() == prior_trace
        events = [json.loads(line) for line in store.path.read_text().splitlines()]
        assert {event["stream_kind"] for event in events} == {"learning"}
        assert [event["kind"] for event in events] == [
            "learning_started",
            "lesson_evaluated",
            "lesson_activated",
            "learning_finished",
        ]

    asyncio.run(scenario())


def test_lesson_gate_rejects_unknown_capabilities_and_secrets(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        view = await _seed_memory(
            memory,
            [
                _episode("episode-ok", "run-1"),
                _episode("episode-bad", "run-2", status="contradicted"),
            ],
        )
        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=view,
                environment_id="scripted",
                environment_profile_version="profile-1",
                min_evidence_groups=1,
            )
        )
        proposal = LessonProposal.model_validate(
            {
                **_proposal("episode-ok"),
                "claim": "send authorization: Bearer exposed",
                "capability_names": ["inspect", "unknown"],
            }
        )

        result = evaluate_lesson_proposal(
            batch=batch,
            proposal=proposal,
            registry=_registry(),
        )

        assert not result.accepted
        assert any("credential-like" in item for item in result.violations)
        assert any("unknown capabilities" in item for item in result.violations)
        assert not any("must be acknowledged" in item for item in result.violations)

    asyncio.run(scenario())


def test_same_capability_contradiction_does_not_block_a_scoped_lesson(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        supporting = _episode("episode-support", "run-1")
        unrelated = _episode("episode-unrelated", "run-2", status="contradicted")
        view = await _seed_memory(memory, [supporting, unrelated])
        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=view,
                environment_id="scripted",
                environment_profile_version="profile-1",
                min_evidence_groups=1,
            )
        )

        result = evaluate_lesson_proposal(
            batch=batch,
            proposal=LessonProposal.model_validate(_proposal("episode-support")),
            registry=_registry(),
        )

        assert result.accepted
        assert result.violations == []

    asyncio.run(scenario())


def test_contradicted_outcomes_can_support_a_corrective_lesson(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        view = await _seed_memory(
            memory,
            [
                _episode("episode-failed-a", "run-a", status="contradicted"),
                _episode("episode-failed-b", "run-b", status="contradicted"),
            ],
        )
        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=view,
                environment_id="scripted",
                environment_profile_version="profile-1",
                min_evidence_groups=2,
            )
        )

        result = evaluate_lesson_proposal(
            batch=batch,
            proposal=LessonProposal.model_validate(
                _proposal("episode-failed-a", "episode-failed-b")
            ),
            registry=_registry(),
        )

        assert result.accepted
        assert result.violations == []

    asyncio.run(scenario())


def test_confirmed_counterexample_does_not_count_as_supporting_group(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        view = await _seed_memory(
            memory,
            [
                _episode("episode-support", "run-support"),
                _episode("episode-counterexample", "run-counterexample"),
            ],
        )
        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=view,
                environment_id="scripted",
                environment_profile_version="profile-1",
                min_evidence_groups=2,
            )
        )
        proposal = LessonProposal.model_validate(
            {
                **_proposal("episode-support"),
                "contradicting_episode_ids": ["episode-counterexample"],
            }
        )

        result = evaluate_lesson_proposal(
            batch=batch,
            proposal=proposal,
            registry=_registry(),
        )

        assert not result.accepted
        assert any("has 1 evidence groups; requires 2" in item for item in result.violations)

    asyncio.run(scenario())


def test_lesson_gate_still_rejects_an_identical_active_lesson(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        view = await _seed_memory(
            memory,
            [_episode("episode-old-a", "run-old-a"), _episode("episode-old-b", "run-old-b")],
        )
        assert view.revision is not None
        await memory.activate_lesson(
            LessonRecord(
                record_id="lesson-active",
                claim="inspect capacity before changing it",
                applies_when=["capacity is uncertain"],
                environment_id="scripted",
                environment_profile_version="profile-1",
                capability_names=["inspect"],
                evidence_episode_ids=["episode-old-a", "episode-old-b"],
                validation=LessonValidation(
                    validator_version="lesson-gate-v3",
                    evidence_group_count=2,
                ),
            ),
            view.revision,
        )
        await memory.record_episode(_episode("episode-new-a", "run-new-a"), view.revision + 1)
        await memory.record_episode(_episode("episode-new-b", "run-new-b"), view.revision + 2)
        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=await memory.resolve_view(
                    MemoryViewRequest(
                        environment_id="scripted",
                        environment_profile_version="profile-1",
                    )
                ),
                environment_id="scripted",
                environment_profile_version="profile-1",
                min_evidence_groups=2,
            )
        )

        result = evaluate_lesson_proposal(
            batch=batch,
            proposal=LessonProposal.model_validate(_proposal("episode-new-a", "episode-new-b")),
            registry=_registry(),
        )

        assert not result.accepted
        assert "an identical active lesson already exists" in result.violations

    asyncio.run(scenario())


def test_world_evidence_group_is_stable_across_runs_and_opaque() -> None:
    shared = evidence_group_id(
        environment_id="uptick",
        environment_profile_version="profile-1",
        world_id="seed-1",
        run_id="run-1",
    )
    repeated = evidence_group_id(
        environment_id="uptick",
        environment_profile_version="profile-1",
        world_id="seed-1",
        run_id="run-2",
    )
    different = evidence_group_id(
        environment_id="uptick",
        environment_profile_version="profile-1",
        world_id="seed-2",
        run_id="run-3",
    )

    assert shared == repeated
    assert shared != different
    assert "seed-1" not in shared


def test_lesson_gate_rejects_world_specific_prose(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        episode = _episode("episode-specific", "run-specific")
        episode = episode.model_copy(
            update={
                "evidence_group_id": "world-specific",
                "selected_action": CapabilityCall(
                    name="inspect",
                    arguments={"operation_id": "operation-specific"},
                ),
            },
            deep=True,
        )
        view = await _seed_memory(memory, [episode])
        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=view,
                environment_id="scripted",
                environment_profile_version="profile-1",
                min_evidence_groups=1,
            )
        )
        proposal = LessonProposal.model_validate(
            {
                **_proposal("episode-specific"),
                "claim": (
                    "For run-specific repeat operation-specific at "
                    "2030-03-01T00:00:00 and apply MITIGATE-ABC123"
                ),
            }
        )

        result = evaluate_lesson_proposal(
            batch=batch,
            proposal=proposal,
            registry=_registry(),
        )

        assert not result.accepted
        assert any("timestamp" in item for item in result.violations)
        assert any("remediation token" in item for item in result.violations)
        assert any("evidence, run, or world identifier" in item for item in result.violations)
        assert any("world-specific capability argument" in item for item in result.violations)

    asyncio.run(scenario())


def test_runner_commits_episodes_and_advances_the_pinned_view(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        store = InMemoryRunStore()
        learner = ScriptedReasoner([])
        learning = LearningOrchestrator(
            memory=memory,
            run_store=store,
            consolidator=MemoryConsolidator(reasoner=learner),
            trigger="after_run",
            min_evidence_groups=2,
        )
        decision_reasoner = ScriptedReasoner(
            [
                _decision_output("inspect"),
                _decision_output("inspect", previous=True),
                _decision_output("finish", completed=True, previous=True),
            ]
        )
        environment = ScriptedEnvironment(
            name="scripted",
            catalog=inspect_catalog(terminal_finish=True),
            observations=[
                Observation(action_kind="inspect", summary="first evidence"),
                Observation(action_kind="inspect", summary="second evidence"),
                Observation(action_kind="finish", summary="done", terminal=True),
            ],
            result=RunResult(
                run_id="run-learning",
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
        )
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=decision_reasoner, sgr=CurrentSGR()),
            environment=environment,
            memory=memory,
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
            learning=learning,
        )

        result = await runner.run(RunSpec(run_id="run-learning", environment="scripted"))

        assert result.status == "completed"
        run_events = [event for event in store.events if event.stream_kind == "run"]
        assert [event.kind for event in run_events] == [
            "run_started",
            "decision_trace",
            "decision_trace",
            "episode_closed",
            "episode_committed",
            "decision_trace",
            "episode_closed",
            "episode_committed",
            "run_finished",
        ]
        assert isinstance(
            decode_prompt_context(decision_reasoner.requests[2].user_prompt)["memory_brief"][
                "similar_episodes"
            ],
            list,
        )
        assert "first evidence" in decision_reasoner.requests[2].user_prompt
        started = run_events[0].payload
        assert isinstance(started, RunStartedPayload)
        final_view = await memory.resolve_view(
            MemoryViewRequest(
                environment_id="scripted",
                environment_profile_version=started.environment_profile_version,
            )
        )
        assert final_view.revision == 2
        assert learner.requests == []
        manifest = await store.load_manifest("run-learning")
        assert manifest is not None and manifest.status == "completed"

    asyncio.run(scenario())


def test_two_ad_hoc_runners_learn_from_shared_memory_without_scheduler_history(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        store = InMemoryRunStore()
        catalog = inspect_catalog(terminal_finish=True)
        bootstrapper = ScriptedEnvironmentBootstrapper()
        source = "Environment scripted exposes scripted tools."
        artifact = await bootstrapper.build(
            source=source,
            environment_id="scripted",
            capabilities=catalog,
        )
        profile_version = artifact.bundle.profile.profile_version
        first_episode_id = episode_record_id(
            environment_id="scripted",
            environment_profile_version=profile_version,
            run_id="run-ad-hoc-1",
            step=1,
        )
        second_episode_id = episode_record_id(
            environment_id="scripted",
            environment_profile_version=profile_version,
            run_id="run-ad-hoc-2",
            step=1,
        )
        learner = ScriptedReasoner([_learner_output(first_episode_id, second_episode_id)])

        async def run_once(run_id: str) -> RunResult:
            runner = AgentRunner(
                agent_core=AgentCore(
                    reasoner=ScriptedReasoner(
                        [
                            _decision_output("inspect"),
                            _decision_output("finish", completed=True, previous=True),
                        ]
                    ),
                    sgr=CurrentSGR(),
                ),
                environment=ScriptedEnvironment(
                    name="scripted",
                    catalog=catalog,
                    observations=[
                        Observation(action_kind="inspect", summary="evidence"),
                        Observation(action_kind="finish", summary="done", terminal=True),
                    ],
                    result=RunResult(
                        run_id=run_id,
                        status="completed",
                        steps=0,
                        duration_seconds=0,
                        stop_reason="",
                    ),
                    bootstrap_text=source,
                ),
                memory=memory,
                run_store=store,
                policy=DecisionPolicy(),
                bootstrapper=bootstrapper,
                bootstrap_artifact=artifact,
                learning=LearningOrchestrator(
                    memory=memory,
                    run_store=store,
                    consolidator=MemoryConsolidator(reasoner=learner),
                    trigger="after_run",
                    min_evidence_groups=2,
                ),
            )
            return await runner.run(RunSpec(run_id=run_id, environment="scripted"))

        first = await run_once("run-ad-hoc-1")
        assert first.status == "completed"
        assert learner.requests == []

        second = await run_once("run-ad-hoc-2")
        assert second.status == "completed"
        assert len(learner.requests) == 1
        assert first_episode_id in learner.requests[0].user_prompt
        assert second_episode_id in learner.requests[0].user_prompt

        current = await memory.resolve_view(
            MemoryViewRequest(
                environment_id="scripted",
                environment_profile_version=profile_version,
            )
        )
        assert current.revision == 3
        learning_finishes = [
            event.payload.result
            for event in store.events
            if isinstance(event.payload, LearningFinishedPayload)
        ]
        assert learning_finishes[0].skipped_reason == ("insufficient independent evidence groups")
        assert learning_finishes[1].commit is not None
        assert learning_finishes[1].selected_run_ids == ["run-ad-hoc-1", "run-ad-hoc-2"]

    asyncio.run(scenario())


def test_learner_failure_does_not_rewrite_a_successful_run(tmp_path: Path) -> None:
    class FailingLearner:
        async def reason(self, request):
            del request
            raise ReasonerFailure(
                "safe learner failure",
                category="transient",
                telemetry=ReasoningTelemetry(
                    provider="fake",
                    requested_model="learner",
                    thread_mode="stateless",
                    attempts=2,
                    duration_seconds=1,
                    sdk_name="fake",
                    sdk_version="1",
                ),
            )

    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        store = InMemoryRunStore()
        environment = ScriptedEnvironment(
            name="scripted",
            catalog=inspect_catalog(terminal_finish=True),
            observations=[
                Observation(action_kind="inspect", summary="evidence"),
                Observation(action_kind="finish", summary="done", terminal=True),
            ],
            result=RunResult(
                run_id="run-nonfatal",
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
        )
        runner = AgentRunner(
            agent_core=AgentCore(
                reasoner=ScriptedReasoner(
                    [
                        _decision_output("inspect"),
                        _decision_output("finish", completed=True, previous=True),
                    ]
                ),
                sgr=CurrentSGR(),
            ),
            environment=environment,
            memory=memory,
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
            learning=LearningOrchestrator(
                memory=memory,
                run_store=store,
                consolidator=MemoryConsolidator(reasoner=FailingLearner()),
                trigger="after_run",
                min_evidence_groups=1,
            ),
        )

        result = await runner.run(RunSpec(run_id="run-nonfatal", environment="scripted"))

        assert result.status == "completed"
        assert [event.kind for event in store.events if event.stream_kind == "run"][-1] == (
            "run_finished"
        )
        assert [event.kind for event in store.events if event.stream_kind == "learning"] == [
            "learning_started",
            "lesson_evaluated",
            "learning_failed",
            "learning_finished",
        ]
        manifest = await store.load_manifest("run-nonfatal")
        assert manifest is not None and manifest.status == "completed"

    asyncio.run(scenario())


def test_episode_memory_failure_is_operationally_fatal(tmp_path: Path) -> None:
    class FailingEpisodeMemory(SQLiteMemory):
        async def record_episode(self, episode, base_revision):
            del episode, base_revision
            raise RuntimeError("episode persistence failed")

    async def scenario() -> None:
        memory = FailingEpisodeMemory(tmp_path / "memory.sqlite")
        store = InMemoryRunStore()
        runner = AgentRunner(
            agent_core=AgentCore(
                reasoner=ScriptedReasoner(
                    [
                        _decision_output("inspect"),
                        _decision_output("finish", completed=True, previous=True),
                    ]
                ),
                sgr=CurrentSGR(),
            ),
            environment=ScriptedEnvironment(
                name="scripted",
                catalog=inspect_catalog(terminal_finish=True),
                observations=[
                    Observation(action_kind="inspect", summary="evidence"),
                    Observation(action_kind="finish", summary="done", terminal=True),
                ],
                result=RunResult(
                    run_id="run-write-failure",
                    status="completed",
                    steps=0,
                    duration_seconds=0,
                    stop_reason="",
                ),
            ),
            memory=memory,
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
            learning=LearningOrchestrator(
                memory=memory,
                run_store=store,
                consolidator=MemoryConsolidator(reasoner=ScriptedReasoner([])),
                trigger="after_run",
                min_evidence_groups=1,
            ),
        )

        with pytest.raises(RunExecutionError) as captured:
            await runner.run(RunSpec(run_id="run-write-failure", environment="scripted"))

        assert captured.value.stage == "episode_write"
        assert [event.kind for event in store.events] == [
            "run_started",
            "decision_trace",
            "decision_trace",
            "episode_closed",
            "run_failed",
        ]
        manifest = await store.load_manifest("run-write-failure")
        assert manifest is not None and manifest.status == "failed"
        assert manifest.failure_stage == "episode_write"

    asyncio.run(scenario())


def test_execute_failure_still_commits_the_verified_prior_episode(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        store = InMemoryRunStore()
        runner = AgentRunner(
            agent_core=AgentCore(
                reasoner=ScriptedReasoner(
                    [
                        _decision_output("inspect"),
                        _decision_output("inspect", previous=True),
                    ]
                ),
                sgr=CurrentSGR(),
            ),
            environment=ScriptedEnvironment(
                name="scripted",
                catalog=inspect_catalog(),
                observations=[Observation(action_kind="inspect", summary="evidence")],
                result=RunResult(
                    run_id="run-execute-failure",
                    status="failed",
                    steps=0,
                    duration_seconds=0,
                    stop_reason="",
                ),
            ),
            memory=memory,
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
            learning=LearningOrchestrator(
                memory=memory,
                run_store=store,
                consolidator=MemoryConsolidator(reasoner=ScriptedReasoner([])),
                trigger="after_run",
                min_evidence_groups=1,
            ),
        )

        with pytest.raises(RunExecutionError) as captured:
            await runner.run(RunSpec(run_id="run-execute-failure", environment="scripted"))

        assert captured.value.stage == "environment_execute"
        assert [event.kind for event in store.events] == [
            "run_started",
            "decision_trace",
            "decision_trace",
            "episode_closed",
            "episode_committed",
            "run_failed",
        ]
        started = store.events[0].payload
        assert isinstance(started, RunStartedPayload)
        view = await memory.resolve_view(
            MemoryViewRequest(
                environment_id="scripted",
                environment_profile_version=started.environment_profile_version,
            )
        )
        assert view.revision == 1

    asyncio.run(scenario())


def test_learning_store_failure_is_not_recorded_recursively(tmp_path: Path) -> None:
    class FailingStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.record_calls = 0

        async def record(self, event):
            self.record_calls += 1
            if self.record_calls == 2:
                raise RuntimeError("store unavailable")
            await super().record(event)

    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        view = await _seed_memory(
            memory,
            [_episode("episode-1", "run-1"), _episode("episode-2", "run-2")],
        )
        store = FailingStore()
        orchestrator = LearningOrchestrator(
            memory=memory,
            run_store=store,
            consolidator=MemoryConsolidator(
                reasoner=ScriptedReasoner([_learner_output("episode-1", "episode-2")])
            ),
            trigger="after_run",
            min_evidence_groups=2,
        )

        with pytest.raises(RunStoreFailure, match="learning trace persistence failed"):
            await orchestrator.consolidate(
                trigger_run_id="run-2",
                trigger_episode_id=None,
                memory_view=view,
                environment_id="scripted",
                environment_profile_version="profile-1",
                registry=_registry(),
            )

        assert store.record_calls == 2
        assert [event.kind for event in store.events if event.stream_kind == "learning"] == [
            "learning_started"
        ]

    asyncio.run(scenario())


def test_inline_learning_makes_an_activated_lesson_visible_to_the_next_decision(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        store = InMemoryRunStore()
        catalog = inspect_catalog(terminal_finish=True)
        bootstrapper = ScriptedEnvironmentBootstrapper()
        source = "Environment scripted exposes scripted tools."
        artifact = await bootstrapper.build(
            source=source,
            environment_id="scripted",
            capabilities=catalog,
        )
        profile_version = artifact.bundle.profile.profile_version
        first_episode_id = episode_record_id(
            environment_id="scripted",
            environment_profile_version=profile_version,
            run_id="run-inline",
            step=1,
        )
        second_episode_id = episode_record_id(
            environment_id="scripted",
            environment_profile_version=profile_version,
            run_id="run-inline",
            step=2,
        )
        learner = ScriptedReasoner(
            [_learner_output(first_episode_id), _learner_output(second_episode_id)]
        )
        decision_reasoner = ScriptedReasoner(
            [
                _decision_output("inspect"),
                _decision_output("inspect", previous=True),
                _decision_output("finish", completed=True, previous=True),
            ]
        )
        environment = ScriptedEnvironment(
            name="scripted",
            catalog=catalog,
            observations=[
                Observation(action_kind="inspect", summary="first evidence"),
                Observation(action_kind="inspect", summary="second evidence"),
                Observation(action_kind="finish", summary="done", terminal=True),
            ],
            result=RunResult(
                run_id="run-inline",
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
            bootstrap_text=source,
        )
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=decision_reasoner, sgr=CurrentSGR()),
            environment=environment,
            memory=memory,
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=bootstrapper,
            bootstrap_artifact=artifact,
            learning=LearningOrchestrator(
                memory=memory,
                run_store=store,
                consolidator=MemoryConsolidator(reasoner=learner),
                trigger="after_closed_episode",
                min_evidence_groups=1,
            ),
        )

        result = await runner.run(RunSpec(run_id="run-inline", environment="scripted"))

        assert result.status == "completed"
        assert isinstance(
            decode_prompt_context(decision_reasoner.requests[2].user_prompt)["memory_brief"][
                "lessons"
            ],
            list,
        )
        assert "inspect capacity before changing it" in decision_reasoner.requests[2].user_prompt
        assert len(learner.requests) == 2
        assert [event.kind for event in store.events if event.stream_kind == "run"][-1] == (
            "run_finished"
        )

    asyncio.run(scenario())


def test_pending_continuation_closes_and_learns_one_original_episode(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        store = InMemoryRunStore()
        catalog = inspect_catalog(terminal_finish=True)
        bootstrapper = ScriptedEnvironmentBootstrapper()
        source = "Environment scripted exposes scripted tools."
        artifact = await bootstrapper.build(
            source=source,
            environment_id="scripted",
            capabilities=catalog,
        )
        original_episode_id = episode_record_id(
            environment_id="scripted",
            environment_profile_version=artifact.bundle.profile.profile_version,
            run_id="run-pending-inline",
            step=1,
        )
        learner = ScriptedReasoner([_learner_output(original_episode_id)])
        runner = AgentRunner(
            agent_core=AgentCore(
                reasoner=ScriptedReasoner(
                    [
                        _decision_output("inspect"),
                        _decision_output("inspect", pending=True),
                        _decision_output("finish", completed=True, previous=True),
                    ]
                ),
                sgr=CurrentSGR(),
            ),
            environment=ScriptedEnvironment(
                name="scripted",
                catalog=catalog,
                observations=[
                    Observation(action_kind="inspect", summary="operation pending"),
                    Observation(action_kind="inspect", summary="still pending"),
                    Observation(action_kind="finish", summary="confirmed", terminal=True),
                ],
                result=RunResult(
                    run_id="run-pending-inline",
                    status="completed",
                    steps=0,
                    duration_seconds=0,
                    stop_reason="",
                ),
                bootstrap_text=source,
            ),
            memory=memory,
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=bootstrapper,
            bootstrap_artifact=artifact,
            learning=LearningOrchestrator(
                memory=memory,
                run_store=store,
                consolidator=MemoryConsolidator(reasoner=learner),
                trigger="after_closed_episode",
                min_evidence_groups=1,
            ),
        )

        result = await runner.run(RunSpec(run_id="run-pending-inline", environment="scripted"))

        assert result.status == "completed"
        run_kinds = [event.kind for event in store.events if event.stream_kind == "run"]
        assert run_kinds.count("episode_closed") == 1
        assert run_kinds.count("episode_committed") == 1
        assert len(learner.requests) == 1

    asyncio.run(scenario())


def test_inline_learner_failure_is_nonfatal_and_the_run_continues(tmp_path: Path) -> None:
    class FailingLearner:
        def __init__(self) -> None:
            self.calls = 0

        async def reason(self, request):
            del request
            self.calls += 1
            raise ReasonerFailure(
                "inline learner unavailable",
                category="transient",
                telemetry=ReasoningTelemetry(
                    provider="fake",
                    requested_model="learner",
                    thread_mode="stateless",
                    duration_seconds=0,
                    sdk_name="fake",
                    sdk_version="1",
                ),
            )

    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        store = InMemoryRunStore()
        learner = FailingLearner()
        environment = ScriptedEnvironment(
            name="scripted",
            catalog=inspect_catalog(terminal_finish=True),
            observations=[
                Observation(action_kind="inspect", summary="first"),
                Observation(action_kind="inspect", summary="second"),
                Observation(action_kind="finish", summary="done", terminal=True),
            ],
            result=RunResult(
                run_id="run-inline-failure",
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
        )
        runner = AgentRunner(
            agent_core=AgentCore(
                reasoner=ScriptedReasoner(
                    [
                        _decision_output("inspect"),
                        _decision_output("inspect", previous=True),
                        _decision_output("finish", completed=True, previous=True),
                    ]
                ),
                sgr=CurrentSGR(),
            ),
            environment=environment,
            memory=memory,
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
            learning=LearningOrchestrator(
                memory=memory,
                run_store=store,
                consolidator=MemoryConsolidator(reasoner=learner),
                trigger="after_closed_episode",
                min_evidence_groups=1,
            ),
        )

        result = await runner.run(RunSpec(run_id="run-inline-failure", environment="scripted"))

        assert result.status == "completed"
        assert len(environment.calls) == 3
        assert learner.calls == 2
        assert [event.kind for event in store.events if event.stream_kind == "run"][-1] == (
            "run_finished"
        )
        assert sum(event.kind == "learning_failed" for event in store.events) == 2

    asyncio.run(scenario())
