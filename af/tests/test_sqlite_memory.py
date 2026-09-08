from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from tests.helpers import inspect_catalog
from uptick_agent.core.memory_models import (
    ConsolidationQuery,
    EpisodeRecord,
    EvidenceRef,
    LessonRecord,
    LessonValidation,
    MemoryQuery,
    MemoryView,
    MemoryViewRequest,
)
from uptick_agent.core.models import (
    AgentConstraints,
    CapabilityCall,
    EnvironmentBrief,
    EnvironmentProfileRef,
    EnvironmentState,
    Observation,
    RecalledLesson,
    VerificationAssessment,
)
from uptick_agent.memory import (
    MemoryRevisionConflict,
    NoMemory,
    SQLiteMemory,
    SQLiteMemoryError,
)
from uptick_agent.runtime.context import ContextAssembler, RunState


def _episode(
    record_id: str,
    *,
    run_id: str,
    step: int = 1,
    observation: str = "capacity is healthy",
    status: str = "confirmed",
    evidence_group_id: str | None = None,
) -> EpisodeRecord:
    return EpisodeRecord(
        record_id=record_id,
        environment_id="uptick",
        environment_profile_version="profile-1",
        evidence_group_id=evidence_group_id or run_id,
        run_id=run_id,
        step=step,
        situation_summary="capacity required inspection",
        selected_action=CapabilityCall(name="get_metrics"),
        expected_result=["capacity becomes observable"],
        observation_summary=observation,
        verification=VerificationAssessment.model_validate(
            {"status": status, "evidence": [observation]}
        ),
        evidence_refs=[
            EvidenceRef(
                stream_id=f"run:{run_id}",
                sequence=step + 1,
                kind="decision_trace",
            )
        ],
    )


def _lesson(
    record_id: str,
    *,
    claim: str,
    evidence: list[str],
    supersedes: list[str] | None = None,
) -> LessonRecord:
    return LessonRecord(
        record_id=record_id,
        claim=claim,
        applies_when=["capacity is uncertain"],
        environment_id="uptick",
        environment_profile_version="profile-1",
        capability_names=["get_metrics"],
        evidence_episode_ids=evidence,
        supersedes=supersedes or [],
        validation=LessonValidation(
            validator_version="lesson-gate-v1",
            evidence_group_count=len(evidence),
        ),
    )


async def _view(memory: SQLiteMemory, revision: int | None = None) -> MemoryView:
    return await memory.resolve_view(
        MemoryViewRequest(
            environment_id="uptick",
            environment_profile_version="profile-1",
            expected_revision=revision,
        )
    )


async def _recall(memory: SQLiteMemory, view: MemoryView, text: str = "capacity"):
    return await memory.recall(
        MemoryQuery(
            objective="maximize balance",
            environment_id="uptick",
            environment_profile_version="profile-1",
            view=view,
            text=text,
            capability_names=["get_metrics"],
        )
    )


def test_sqlite_memory_revisioned_reads_idempotency_and_collision(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        empty = await _view(memory)
        assert empty.revision == 0
        assert empty.database_id is not None

        episode = _episode("episode-1", run_id="run-1")
        commit = await memory.record_episode(episode, base_revision=0)
        assert commit.applied
        assert (commit.previous_revision, commit.new_revision) == (0, 1)

        repeated = await memory.record_episode(episode, base_revision=0)
        assert not repeated.applied
        assert (repeated.previous_revision, repeated.new_revision) == (1, 1)

        current = await _view(memory)
        assert current.revision == 1
        assert (await _recall(memory, empty)).records == []
        packet = await _recall(memory, current)
        assert [item.record_id for item in packet.records] == ["episode-1"]
        assert packet.brief.similar_episodes
        assert packet.diagnostics is not None
        assert packet.diagnostics.selected_record_ids == ["episode-1"]

        changed = episode.model_copy(update={"observation_summary": "different"})
        with pytest.raises(SQLiteMemoryError, match="collision"):
            await memory.record_episode(changed, base_revision=1)
        with pytest.raises(SQLiteMemoryError, match="identity"):
            await memory.resolve_view(
                MemoryViewRequest(
                    environment_id="uptick",
                    environment_profile_version="profile-1",
                    expected_database_id="memory-wrong",
                )
            )

    asyncio.run(scenario())


def test_sqlite_memory_compacts_long_episode_fields_for_decision_context(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        await _view(memory)
        episode = _episode("episode-long", run_id="run-long")
        episode = EpisodeRecord.model_validate(
            {
                **episode.model_dump(mode="python"),
                "observation_summary": "capacity consequence " + ("x" * 700),
            }
        )

        await memory.record_episode(episode, base_revision=0)
        packet = await _recall(memory, await _view(memory))

        assert len(packet.brief.similar_episodes) == 1
        summary = packet.brief.similar_episodes[0]
        assert len(summary) <= 600
        assert summary.count("action=get_metrics") == 1
        assert summary.count("outcome=") == 1

    asyncio.run(scenario())


def test_sqlite_memory_reserves_relevant_contradiction_and_limits_one_run(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        await _view(memory)
        revision = 0
        for index in range(6):
            await memory.record_episode(
                _episode(
                    f"episode-confirmed-{index}",
                    run_id="run-repetitive",
                    step=index + 1,
                    observation=f"capacity sample {index}",
                ),
                revision,
            )
            revision += 1
        await memory.record_episode(
            _episode(
                "episode-contradiction",
                run_id="run-counterexample",
                observation="capacity regression",
                status="contradicted",
            ),
            revision,
        )

        packet = await _recall(memory, await _view(memory), text="capacity")
        selected = [item.record_id for item in packet.records]

        assert "episode-contradiction" in selected
        assert sum(item.startswith("episode-confirmed-") for item in selected) == 2
        assert packet.brief.contradictions

    asyncio.run(scenario())


def test_sqlite_memory_retrieves_lessons_in_a_lane_separate_from_new_episodes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        await _view(memory)
        revision = 0
        evidence_ids = ["episode-support-1", "episode-support-2"]
        for index, episode_id in enumerate(evidence_ids):
            await memory.record_episode(
                _episode(episode_id, run_id=f"run-support-{index}"),
                revision,
            )
            revision += 1

        relevant_lesson = _lesson(
            "lesson-relevant",
            claim="inspect capacity before changing it",
            evidence=evidence_ids,
        )
        await memory.activate_lesson(relevant_lesson, revision)
        revision += 1
        irrelevant_lesson = LessonRecord(
            record_id="lesson-irrelevant",
            claim="rotate certificates before expiry",
            applies_when=["certificate renewal is pending"],
            environment_id="uptick",
            environment_profile_version="profile-1",
            capability_names=["restart_service"],
            evidence_episode_ids=evidence_ids,
            validation=LessonValidation(
                validator_version="lesson-gate-v1",
                evidence_group_count=2,
            ),
        )
        await memory.activate_lesson(irrelevant_lesson, revision)
        revision += 1

        for index in range(1_000):
            await memory.record_episode(
                _episode(
                    f"episode-new-{index}",
                    run_id=f"run-new-{index}",
                    observation=f"capacity sample {index}",
                ),
                revision,
            )
            revision += 1

        packet = await _recall(memory, await _view(memory), text="capacity")
        selected = [item.record_id for item in packet.records]

        assert "lesson-relevant" in selected
        assert (
            RecalledLesson(
                claim="inspect capacity before changing it", applies_when=["capacity is uncertain"]
            )
            in packet.brief.lessons
        )
        assert "lesson-irrelevant" not in selected
        assert all(
            isinstance(item, RecalledLesson) and item.claim != "rotate certificates before expiry"
            for item in packet.brief.lessons
        )

    asyncio.run(scenario())


def test_sqlite_memory_uses_objective_and_signals_for_lexical_candidates(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        await _view(memory)
        await memory.record_episode(
            _episode(
                "episode-objective",
                run_id="run-objective",
                observation="unique_forecast_marker observed",
            ),
            0,
        )
        view = await _view(memory)
        objective_packet = await memory.recall(
            MemoryQuery(
                objective="respond to unique_forecast_marker",
                environment_id="uptick",
                environment_profile_version="profile-1",
                view=view,
            )
        )
        signal_packet = await memory.recall(
            MemoryQuery(
                objective="keep the service healthy",
                environment_id="uptick",
                environment_profile_version="profile-1",
                view=view,
                signals={"incident": "unique_forecast_marker"},
            )
        )

        assert [item.record_id for item in objective_packet.records] == ["episode-objective"]
        assert [item.record_id for item in signal_packet.records] == ["episode-objective"]

    asyncio.run(scenario())


def test_sqlite_memory_compare_and_swap_allows_only_one_writer(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "memory.sqlite"
        first = SQLiteMemory(path)
        second = SQLiteMemory(path)
        await _view(first)
        results = await asyncio.gather(
            first.record_episode(_episode("episode-a", run_id="run-a"), 0),
            second.record_episode(_episode("episode-b", run_id="run-b"), 0),
            return_exceptions=True,
        )

        assert sum(not isinstance(item, Exception) for item in results) == 1
        conflicts = [item for item in results if isinstance(item, MemoryRevisionConflict)]
        assert len(conflicts) == 1
        assert (await _view(first)).revision == 1

    asyncio.run(scenario())


def test_sqlite_lessons_supersede_atomically_and_cover_consolidation_inputs(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        await _view(memory)
        await memory.record_episode(_episode("episode-1", run_id="run-1"), 0)
        await memory.record_episode(_episode("episode-2", run_id="run-2"), 1)
        first_lesson = _lesson(
            "lesson-1",
            claim="inspect capacity before scaling",
            evidence=["episode-1", "episode-2"],
        )
        await memory.activate_lesson(first_lesson, 2)
        await memory.record_episode(_episode("episode-3", run_id="run-3"), 3)
        revision_four = await _view(memory)

        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=revision_four,
                environment_id="uptick",
                environment_profile_version="profile-1",
                min_evidence_groups=1,
            )
        )
        assert [item.record_id for item in batch.episodes] == ["episode-3"]
        assert [item.record_id for item in batch.active_lessons] == ["lesson-1"]

        replacement = _lesson(
            "lesson-2",
            claim="inspect fresh capacity evidence before scaling",
            evidence=["episode-3"],
            supersedes=["lesson-1"],
        )
        commit = await memory.activate_lesson(replacement, 4)
        assert commit.applied
        assert commit.record_ids == ["lesson-2", "lesson-1"]

        historical = await _recall(memory, revision_four)
        current = await _recall(memory, await _view(memory))
        assert historical.brief.lessons == [
            RecalledLesson(claim=first_lesson.claim, applies_when=first_lesson.applies_when)
        ]
        assert current.brief.lessons == [
            RecalledLesson(claim=replacement.claim, applies_when=replacement.applies_when)
        ]

    asyncio.run(scenario())


def test_consolidation_limit_round_robins_across_evidence_groups(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        await _view(memory)
        revision = 0
        for index in range(35):
            await memory.record_episode(
                _episode(
                    f"episode-a-{index + 1}",
                    run_id="run-a",
                    step=index + 1,
                    evidence_group_id="group-a",
                ),
                revision,
            )
            revision += 1
        for index in range(2):
            await memory.record_episode(
                _episode(
                    f"episode-b-{index + 1}",
                    run_id="run-b",
                    step=index + 1,
                    evidence_group_id="group-b",
                ),
                revision,
            )
            revision += 1

        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=await _view(memory),
                environment_id="uptick",
                environment_profile_version="profile-1",
                min_evidence_groups=2,
                episode_limit=32,
            )
        )

        assert [item.record_id for item in batch.episodes[:4]] == [
            "episode-a-1",
            "episode-b-1",
            "episode-a-2",
            "episode-b-2",
        ]
        assert {item.evidence_group_id for item in batch.episodes} == {"group-a", "group-b"}
        assert len(batch.episodes) == 32
        assert "episode-a-35" in {item.record_id for item in batch.episodes}

    asyncio.run(scenario())


def test_inline_consolidation_loads_only_the_trigger_episode(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        await _view(memory)
        await memory.record_episode(
            _episode("episode-old", run_id="run-inline", step=1),
            base_revision=0,
        )
        await memory.record_episode(
            _episode("episode-trigger", run_id="run-inline", step=2),
            base_revision=1,
        )

        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_closed_episode",
                trigger_episode_id="episode-trigger",
                view=await _view(memory),
                environment_id="uptick",
                environment_profile_version="profile-1",
                min_evidence_groups=1,
            )
        )

        assert [episode.record_id for episode in batch.episodes] == ["episode-trigger"]

    asyncio.run(scenario())


def test_sqlite_schema_fails_closed_for_unversioned_database(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    with pytest.raises(SQLiteMemoryError, match="unversioned"):
        asyncio.run(_view(SQLiteMemory(path)))


def test_sqlite_schema_v4_stores_canonical_content_without_redundant_digest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.sqlite"
    asyncio.run(_view(SQLiteMemory(path)))

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memory_records)")}
    assert "canonical_json" in columns
    assert "content_sha256" not in columns


def test_empty_sqlite_and_no_memory_have_identical_model_facing_context(tmp_path: Path) -> None:
    async def scenario() -> None:
        environment_state = EnvironmentState(
            profile=EnvironmentProfileRef(
                environment_id="uptick",
                version="profile-1",
            ),
            status="active",
            decision_view={"health": "ok"},
            latest_observation=Observation(action_kind="start", summary="ready"),
        )
        brief = EnvironmentBrief(
            environment_id="uptick",
            profile_version="profile-1",
            guidance=["inspect first"],
        )
        capabilities = inspect_catalog()
        assembler = ContextAssembler()
        sqlite = SQLiteMemory(tmp_path / "memory.sqlite")
        sqlite_view = await _view(sqlite)
        no_memory = NoMemory()
        none_view = await no_memory.resolve_view(
            MemoryViewRequest(
                environment_id="uptick",
                environment_profile_version="profile-1",
            )
        )

        async def context(memory, view):
            run_state = RunState(
                run_id="run-private",
                step=1,
                step_limit=3,
                memory_view=view,
                environment_state=environment_state,
            )
            packet = await memory.recall(
                assembler.memory_query(
                    objective="keep healthy",
                    environment_state=environment_state,
                    capabilities=capabilities,
                    memory_view=view,
                )
            )
            return assembler.assemble(
                objective="keep healthy",
                environment_profile=brief,
                run_state=run_state,
                capabilities=capabilities,
                memory=packet,
                constraints=AgentConstraints(),
            )

        sqlite_context = await context(sqlite, sqlite_view)
        none_context = await context(no_memory, none_view)
        assert sqlite_context.model_dump_json() == none_context.model_dump_json()

    asyncio.run(scenario())
