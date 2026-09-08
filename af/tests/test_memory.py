import asyncio

from uptick_agent.core.memory_models import (
    ConsolidationQuery,
    EpisodeRecord,
    EvidenceRef,
    LessonRecord,
    LessonValidation,
    MemoryQuery,
    MemoryViewRequest,
)
from uptick_agent.core.models import CapabilityCall, VerificationAssessment
from uptick_agent.memory import NoMemory


def test_no_memory_implements_the_complete_read_only_contract() -> None:
    async def scenario() -> None:
        memory = NoMemory()
        view = await memory.resolve_view(
            MemoryViewRequest(
                environment_id="test",
                environment_profile_version="profile-1",
            )
        )
        packet = await memory.recall(
            MemoryQuery(
                objective="keep healthy",
                environment_id="test",
                environment_profile_version="profile-1",
                view=view,
                text="failure",
            )
        )
        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=view,
                environment_id="test",
                environment_profile_version="profile-1",
                min_evidence_groups=2,
            )
        )

        assert view.database_id is None
        assert view.revision is None
        assert packet.view == view
        assert packet.records == []
        assert packet.brief.model_dump() == {
            "lessons": [],
            "similar_episodes": [],
            "contradictions": [],
        }
        assert batch.episodes == []
        assert batch.active_lessons == []

    asyncio.run(scenario())


def test_no_memory_write_methods_are_safe_explicit_no_ops() -> None:
    async def scenario() -> None:
        memory = NoMemory()
        episode = EpisodeRecord(
            record_id="episode-1",
            environment_id="test",
            environment_profile_version="profile-1",
            evidence_group_id="run-1",
            run_id="run-1",
            step=1,
            situation_summary="capacity was insufficient",
            selected_action=CapabilityCall(name="inspect"),
            expected_result=["capacity becomes observable"],
            observation_summary="capacity is healthy",
            verification=VerificationAssessment(status="confirmed"),
            evidence_refs=[EvidenceRef(stream_id="run:run-1", sequence=2, kind="decision_trace")],
        )
        lesson = LessonRecord(
            record_id="lesson-1",
            claim="inspect before changing state",
            applies_when=["state is unknown"],
            environment_id="test",
            environment_profile_version="profile-1",
            evidence_episode_ids=[episode.record_id],
            validation=LessonValidation(
                validator_version="v1",
                evidence_group_count=1,
            ),
        )

        episode_commit = await memory.record_episode(episode, base_revision=0)
        lesson_commit = await memory.activate_lesson(lesson, base_revision=0)

        assert not episode_commit.applied
        assert episode_commit.record_ids == ["episode-1"]
        assert not lesson_commit.applied
        assert lesson_commit.record_ids == ["lesson-1"]

    asyncio.run(scenario())
