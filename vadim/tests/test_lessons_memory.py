"""Contract tests for the isolated lessons module."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

import pytest

from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryPermanentError,
    ObjectiveMetric,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.episodic import EpisodicMemory
from uptick_agent.memory.lesson_contracts import (
    LessonEvidence,
    LessonRunDeclaration,
    LessonSettings,
)
from uptick_agent.memory.lesson_runtime import lessons_memory_runtime
from uptick_agent.memory.lessons import LessonsMemory
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite, StoredRecord, sha256_json
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler


def _settings() -> LessonSettings:
    return LessonSettings(
        metric_name="health",
        metric_unit="points",
        direction="maximize",
        condition_keys=("summary",),
    )


def _declaration(run_id: str, index: int) -> LessonRunDeclaration:
    return LessonRunDeclaration(
        run_id=run_id,
        logical_run_id=f"logical-{run_id}",
        attempt_index=0,
        phase="learning",
        environment_id="test-environment",
        scenario_id=f"scenario-{index}",
        environment_content_hash=sha256_json({"environment": "test"}),
        scenario_content_hash=sha256_json({"scenario": index}),
        eligible=True,
    )


def _transition(run_id: str, index: int, delta: int) -> ExperienceTransition:
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition-{index}",
            run_id=run_id,
            iteration=1,
            occurred_at=datetime(2026, 9, 4, 10 + index, tzinfo=UTC),
            trust_classification="external_untrusted",
            environment_id="test-environment",
            scenario_id=f"scenario-{index}",
            pre_state={},
            observation={"summary": "service pressure"},
            action={"kind": "apply_fix"},
            result={"ok": True},
            before_objective_metrics=[ObjectiveMetric(name="health", value=50, unit="points")],
            after_objective_metrics=[
                ObjectiveMetric(name="health", value=50 + delta, unit="points")
            ],
            terminal=True,
        )
    )


class _Source:
    def __init__(self, evidence: dict[str, LessonEvidence]):
        self.evidence = evidence
        self.calls = 0

    async def capture(self, outcome: RunOutcome, *, idempotency_key: str) -> LessonEvidence | None:
        self.calls += 1
        return self.evidence.get(outcome.run_id)


async def _prepare_one_run(*, stop_reason: str = "done"):
    store = InMemoryStructuredStore()
    episodic = EpisodicMemory(store, namespace="episodes")
    declaration = _declaration("run-1", 1)
    outcome = RunOutcome(
        run_id="run-1",
        status="completed",
        finished_at=datetime(2026, 9, 4, 11, tzinfo=UTC),
        stop_reason=stop_reason,
        objective_metrics=[ObjectiveMetric(name="health", value=60, unit="points")],
    )
    transition = _transition("run-1", 1, 10)
    await episodic.record(transition, idempotency_key="transition")
    await episodic.finalize(outcome, idempotency_key="outcome")
    records = await store.list(namespace="episodes")
    snapshot = await store.create_snapshot(
        namespace="episodes",
        snapshot_id="snapshot-1",
        operation="test-snapshot",
        idempotency_key="snapshot-1",
    )
    source = _Source(
        {
            "run-1": LessonEvidence(
                snapshot=snapshot.snapshot,
                records=records,
                runs=[declaration],
            )
        }
    )
    memory = LessonsMemory(
        store,
        namespace="lessons",
        source=source,
        settings=_settings(),
    )
    return store, memory, source, outcome


def test_lessons_require_two_runs_and_replay_without_duplicate_batches() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        episodic = EpisodicMemory(store, namespace="episodes")
        declarations = [_declaration("run-1", 1), _declaration("run-2", 2)]
        outcomes = [
            RunOutcome(
                run_id=declaration.run_id,
                status="completed",
                finished_at=datetime(2026, 9, 4, 11 + index, tzinfo=UTC),
                stop_reason="done",
                objective_metrics=[ObjectiveMetric(name="health", value=60, unit="points")],
            )
            for index, declaration in enumerate(declarations)
        ]
        all_records = []
        evidences = {}
        for index, (declaration, outcome) in enumerate(zip(declarations, outcomes, strict=True)):
            transition = _transition(declaration.run_id, index + 1, 10)
            await episodic.record(transition, idempotency_key=f"transition-{index}")
            await episodic.finalize(outcome, idempotency_key=f"outcome-{index}")
            all_records = await store.list(namespace="episodes")
            snapshot = await store.create_snapshot(
                namespace="episodes",
                snapshot_id=f"snapshot-{index}",
                operation="test-snapshot",
                idempotency_key=f"snapshot-{index}",
            )
            evidences[declaration.run_id] = LessonEvidence(
                snapshot=snapshot.snapshot,
                records=all_records,
                runs=declarations,
            )
        source = _Source(evidences)
        memory = LessonsMemory(
            store,
            namespace="lessons",
            source=source,
            settings=_settings(),
        )
        await memory.finalize(outcomes[0], idempotency_key="finalize-1")
        await memory.finalize(outcomes[0], idempotency_key="finalize-1")
        assert len(await store.list(namespace="lessons")) == 1
        assert (
            await memory.retrieve(
                MemoryContextRequest(request_id="r1", run_id="other", query="summary")
            )
        ).items == []

        await memory.finalize(outcomes[1], idempotency_key="finalize-2")
        visible = await memory.retrieve(
            MemoryContextRequest(request_id="r2", run_id="other", query="summary")
        )
        assert len(visible.items) == 1
        assert visible.items[0].envelope.trust_classification == "derived_untrusted"
        assert source.calls == 2

    asyncio.run(scenario())


def test_disabled_or_failed_capture_has_no_learning_side_effect() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        source = _Source({})
        memory = LessonsMemory(
            store,
            namespace="lessons",
            source=source,
            settings=_settings(),
        )
        await memory.finalize(
            RunOutcome(
                run_id="failed",
                status="failed",
                finished_at=datetime(2026, 9, 4, 10, tzinfo=UTC),
                stop_reason="failed",
            ),
            idempotency_key="failed",
        )
        assert source.calls == 1
        assert await store.list(namespace="lessons") == []

        configuration = MemoryConfiguration.episodic_with_lessons(lesson_settings=_settings())
        assert configuration.lesson_settings == _settings()
        assert configuration.fingerprint != MemoryConfiguration.episodic_only().fingerprint

    asyncio.run(scenario())


def test_replay_normalizes_redacted_outcome_and_conflicting_input_is_rejected() -> None:
    async def scenario() -> None:
        store, memory, source, outcome = await _prepare_one_run(stop_reason="sk-abcdefghijk done")
        await memory.finalize(outcome, idempotency_key="finalize")
        await memory.finalize(outcome, idempotency_key="finalize")
        assert source.calls == 1
        with pytest.raises(MemoryConflictError):
            await memory.finalize(
                outcome.model_copy(update={"stop_reason": "different"}),
                idempotency_key="finalize",
            )
        assert len(await store.list(namespace="lessons")) == 1

    asyncio.run(scenario())


def test_tampered_active_lesson_fails_closed_on_retrieval() -> None:
    async def scenario() -> None:
        store, memory, _source, outcome = await _prepare_one_run()
        await memory.finalize(outcome, idempotency_key="finalize")
        record = await store.get(
            namespace="lessons",
            record_id="lesson-batch-" + hashlib.sha256(b"run-1").hexdigest(),
        )
        assert record is not None
        payload = record.payload.copy()
        payload["lessons"][0]["status"] = "active"
        payload["lessons"][0]["manifest"]["disposition"] = "active"
        forged = StoredRecord.from_write(
            RecordWrite(
                namespace=record.namespace,
                record_id=record.record_id,
                record_type=record.record_type,
                payload=payload,
                created_at=record.created_at,
            )
        )
        store._records[(record.namespace, record.record_id)] = forged
        with pytest.raises(MemoryPermanentError):
            await memory.retrieve(
                MemoryContextRequest(request_id="read", run_id="other", query="summary")
            )

    asyncio.run(scenario())


def test_pre_1_1_manifest_fails_closed_until_revalidated() -> None:
    async def scenario() -> None:
        store, memory, _source, outcome = await _prepare_one_run()
        await memory.finalize(outcome, idempotency_key="finalize")
        record = await store.get(
            namespace="lessons",
            record_id="lesson-batch-" + hashlib.sha256(b"run-1").hexdigest(),
        )
        assert record is not None
        payload = record.payload.copy()
        manifest = payload["lessons"][0]["manifest"].copy()
        del manifest["authority_service_ref"]
        payload["lessons"][0] = payload["lessons"][0].copy()
        payload["lessons"][0]["manifest"] = manifest
        forged = StoredRecord.from_write(
            RecordWrite(
                namespace=record.namespace,
                record_id=record.record_id,
                record_type=record.record_type,
                payload=payload,
                created_at=record.created_at,
            )
        )
        store._records[(record.namespace, record.record_id)] = forged
        with pytest.raises(MemoryPermanentError, match="invalid"):
            await memory.retrieve(
                MemoryContextRequest(request_id="read", run_id="other", query="summary")
            )

    asyncio.run(scenario())


def test_disabled_lessons_factory_does_not_validate_or_construct_source() -> None:
    class _PoisonStore:
        def __getattr__(self, name):
            raise AssertionError(f"disabled runtime touched store via {name}")

    runtime = lessons_memory_runtime(
        _PoisonStore(),
        episodic_namespace="episodes",
        lesson_namespace="lessons",
        run_declarations="invalid-but-ignored",
        configuration=MemoryConfiguration.episodic_only(),
    )
    assert runtime is not None
