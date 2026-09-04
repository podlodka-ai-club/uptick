from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryConflictError,
    MemoryPermanentError,
    MemoryValidationError,
    ObjectiveMetric,
    ProvenanceRef,
    RunOutcome,
)
from uptick_agent.memory.episodic import EpisodicMemory
from uptick_agent.memory.lesson_contracts import LessonRunDeclaration, snapshot_input_hash
from uptick_agent.memory.lesson_evidence import StoredEpisodicLessonSource
from uptick_agent.memory.stores import InMemoryStructuredStore, SqliteStructuredStore


def _transition(*, run_id: str, transition_id: str, iteration: int = 1) -> ExperienceTransition:
    return ExperienceTransition(
        transition_id=transition_id,
        run_id=run_id,
        iteration=iteration,
        occurred_at=datetime(2026, 9, 4, 10, iteration, tzinfo=UTC),
        trust_classification="external_untrusted",
        pre_state={"service": "healthy"},
        observation={"condition": "degraded", "metric": 1},
        action={"kind": "restart"},
        result={"ok": True, "metric": 3},
        objective_metrics=[ObjectiveMetric(name="balance", value=3, unit="minor")],
        objective_deltas=[],
        provenance=[
            ProvenanceRef(
                artefact_id=f"observation:{transition_id}",
                content_hash="a" * 64,
            )
        ],
        terminal=True,
    )


def _outcome(run_id: str, *, status: str = "completed") -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        status=status,
        finished_at=datetime(2026, 9, 4, 11, tzinfo=UTC),
        stop_reason="completed",
    )


def _declaration(
    run_id: str,
    *,
    logical_run_id: str | None = None,
    attempt_index: int = 0,
    phase: str = "learning",
    eligible: bool = True,
    environment_id: str = "env-a",
    scenario_id: str = "scenario-a",
) -> LessonRunDeclaration:
    return LessonRunDeclaration(
        run_id=run_id,
        logical_run_id=logical_run_id or f"logical-{run_id}",
        attempt_index=attempt_index,
        phase=phase,
        eligible=eligible,
        environment_id=environment_id,
        scenario_id=scenario_id,
        environment_content_hash="b" * 64,
        scenario_content_hash="c" * 64,
    )


async def _seed(store, *, run_id: str = "run-1") -> tuple[EpisodicMemory, RunOutcome]:
    memory = EpisodicMemory(store, namespace="episodic")
    await memory.record(
        _transition(run_id=run_id, transition_id=f"transition-{run_id}"),
        idempotency_key=f"transition-{run_id}",
    )
    outcome = _outcome(run_id)
    await memory.finalize(outcome, idempotency_key=f"outcome-{run_id}")
    return memory, outcome


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_capture_reopens_and_replays_the_frozen_membership_after_append(
    backend: str, tmp_path: Path
) -> None:
    async def scenario() -> None:
        path = tmp_path / "lessons.sqlite"
        store = InMemoryStructuredStore() if backend == "memory" else SqliteStructuredStore(path)
        memory, outcome = await _seed(store)
        source = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodic",
            declaration_namespace="lesson-declarations",
            run_declarations=[_declaration("run-1")],
        )

        first = await source.capture(outcome, idempotency_key="finalize-run-1")
        assert first is not None
        assert len(first.records) == 2
        first_hash = snapshot_input_hash(first)

        await memory.record(
            _transition(run_id="later", transition_id="transition-later"),
            idempotency_key="transition-later",
        )
        if backend == "sqlite":
            store = SqliteStructuredStore(path)
        replay_source = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodic",
            declaration_namespace="lesson-declarations",
            run_declarations=[_declaration("run-1"), _declaration("later")],
        )
        replay = await replay_source.capture(outcome, idempotency_key="new-replay-key")

        assert replay is not None
        assert replay.snapshot == first.snapshot
        assert [record.record_id for record in replay.records] == [
            record.record_id for record in first.records
        ]
        assert [run.run_id for run in replay.runs] == ["run-1"]
        assert snapshot_input_hash(replay) == first_hash

    asyncio.run(scenario())


def test_capture_ignores_forged_receipts_and_reads_canonical_records(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "forged-receipts.sqlite"
        store = SqliteStructuredStore(path)
        _, outcome = await _seed(store)
        source = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodic",
            declaration_namespace="lesson-declarations",
            run_declarations=[_declaration("run-1")],
        )
        first = await source.capture(outcome, idempotency_key="first")
        assert first is not None

        with sqlite3.connect(path) as connection:
            rows = connection.execute(
                "SELECT namespace, operation, idempotency_key, receipt_json "
                "FROM memory_operation_receipts"
            ).fetchall()
            for namespace, operation, key, receipt_json in rows:
                forged = json.loads(receipt_json)
                if "record" in forged:
                    forged["record"]["payload"] = {"forged": True}
                if "snapshot" in forged:
                    forged["snapshot"]["members"] = []
                connection.execute(
                    "UPDATE memory_operation_receipts SET receipt_json = ? "
                    "WHERE namespace = ? AND operation = ? AND idempotency_key = ?",
                    (json.dumps(forged), namespace, operation, key),
                )

        replay = await source.capture(outcome, idempotency_key="second")
        assert replay == first

    asyncio.run(scenario())


def test_capture_fails_closed_when_snapshot_member_is_tampered() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        _, outcome = await _seed(store)
        source = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodic",
            declaration_namespace="lesson-declarations",
            run_declarations=[_declaration("run-1")],
        )
        evidence = await source.capture(outcome, idempotency_key="first")
        assert evidence is not None
        record_id = evidence.records[0].record_id
        record = store._records[("episodic", record_id)]
        store._records[("episodic", record_id)] = record.model_copy(
            update={"content_hash": "d" * 64}
        )

        with pytest.raises(MemoryPermanentError, match="content hash mismatch"):
            await source.capture(outcome, idempotency_key="replay")

    asyncio.run(scenario())


def test_declaration_is_immutable_across_reopen_and_context_changes_conflict() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        _, outcome = await _seed(store)
        first_source = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodic",
            declaration_namespace="lesson-declarations",
            run_declarations=[_declaration("run-1")],
        )
        assert await first_source.capture(outcome, idempotency_key="first") is not None
        changed = _declaration("run-1", scenario_id="scenario-changed")
        changed_source = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodic",
            declaration_namespace="lesson-declarations",
            run_declarations=[changed],
        )

        with pytest.raises(MemoryConflictError, match="immutable"):
            await changed_source.capture(outcome, idempotency_key="replay")

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted", "excluded"])
@pytest.mark.parametrize(
    "declaration_kwargs",
    [
        {},
        {"eligible": False},
        {"attempt_index": 1},
    ],
)
def test_declared_learning_finalizations_capture_for_revalidation(
    status: str, declaration_kwargs: dict
) -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        memory = EpisodicMemory(store, namespace="episodic")
        await memory.record(
            _transition(run_id="run-1", transition_id="transition-run-1"),
            idempotency_key="transition-run-1",
        )
        outcome = _outcome("run-1", status=status)
        await memory.finalize(outcome, idempotency_key="outcome-run-1")
        source = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodic",
            declaration_namespace="lesson-declarations",
            run_declarations=[_declaration("run-1", **declaration_kwargs)],
        )
        evidence = await source.capture(outcome, idempotency_key="key")
        assert evidence is not None
        assert len(evidence.records) == 2

    asyncio.run(scenario())


def test_frozen_evaluation_has_zero_store_side_effects() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        source = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodic",
            declaration_namespace="lesson-declarations",
            run_declarations=[_declaration("run-1", phase="frozen_evaluation")],
        )
        before_records = dict(store._records)
        before_snapshots = dict(store._snapshots)

        assert await source.capture(_outcome("run-1"), idempotency_key="key") is None
        assert store._records == before_records
        assert store._snapshots == before_snapshots

    asyncio.run(scenario())


def test_success_without_declaration_has_zero_store_side_effects() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        source = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodic",
            declaration_namespace="lesson-declarations",
            run_declarations=[],
        )
        assert await source.capture(_outcome("missing"), idempotency_key="key") is None
        assert store._records == {}
        assert store._snapshots == {}

    asyncio.run(scenario())


def test_namespaces_must_be_disjoint_before_any_store_access() -> None:
    with pytest.raises(MemoryValidationError, match="disjoint"):
        StoredEpisodicLessonSource(
            InMemoryStructuredStore(),
            episodic_namespace="same",
            declaration_namespace="same",
            run_declarations=[],
        )


def test_snapshot_ids_include_namespaces_when_one_store_holds_two_experiments() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        _, outcome = await _seed(store)
        await EpisodicMemory(store, namespace="episodic-other").record(
            _transition(run_id="run-1", transition_id="transition-other"),
            idempotency_key="transition-other",
        )
        await EpisodicMemory(store, namespace="episodic-other").finalize(
            outcome, idempotency_key="outcome-other"
        )
        first = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodic",
            declaration_namespace="lesson-declarations",
            run_declarations=[_declaration("run-1")],
        )
        second = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodic-other",
            declaration_namespace="other-declarations",
            run_declarations=[_declaration("run-1")],
        )

        first_evidence = await first.capture(outcome, idempotency_key="first")
        second_evidence = await second.capture(outcome, idempotency_key="second")
        assert first_evidence is not None
        assert second_evidence is not None
        assert first_evidence.snapshot.snapshot_id != second_evidence.snapshot.snapshot_id
        assert [record.record_id for record in first_evidence.records] != [
            record.record_id for record in second_evidence.records
        ]

    asyncio.run(scenario())


def test_snapshot_requires_the_current_outcome_record() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        transition = _transition(run_id="run-1", transition_id="transition-run-1")
        memory = EpisodicMemory(store, namespace="episodic")
        await memory.record(transition, idempotency_key="transition")
        source = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodic",
            declaration_namespace="lesson-declarations",
            run_declarations=[_declaration("run-1")],
        )

        with pytest.raises(MemoryPermanentError, match="current run outcome"):
            await source.capture(_outcome("run-1"), idempotency_key="finalize")

    asyncio.run(scenario())
