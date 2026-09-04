from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from uptick_agent.memory.consolidation import (
    CONSOLIDATION_APPLY_RECORD_TYPE,
    CONSOLIDATION_PLAN_RECORD_TYPE,
    ConsolidationMemory,
    ConsolidationSettings,
    StoredSnapshotEvidenceSource,
)
from uptick_agent.memory.contracts import (
    ConsolidationRequest,
    ExperienceTransition,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryPermanentError,
    ObjectiveMetric,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.lesson_contracts import (
    LessonEvidence,
    LessonRunDeclaration,
    LessonSettings,
)
from uptick_agent.memory.patterns import PatternQuerySettings
from uptick_agent.memory.stores import InMemoryStructuredStore, RecordWrite
from uptick_agent.memory.stores.contracts import sha256_json
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_TIME = datetime(2026, 9, 5, 12, tzinfo=UTC)
_EPISODIC = "consolidation-evidence"
_DECLARATIONS = "consolidation-declarations"


class _StaticSource:
    def __init__(self, evidence: dict[str, LessonEvidence]) -> None:
        self._evidence = evidence

    async def load(self, snapshot_id: str) -> LessonEvidence:
        return self._evidence[snapshot_id]


def _run(awaitable):
    return asyncio.run(awaitable)


def _declaration(run_id: str, scenario: str) -> LessonRunDeclaration:
    return LessonRunDeclaration(
        run_id=run_id,
        logical_run_id=f"logical:{run_id}",
        phase="learning",
        eligible=True,
        environment_id="environment:test",
        scenario_id=f"scenario:{scenario}",
        environment_content_hash=sha256_json({"environment": "test"}),
        scenario_content_hash=sha256_json({"scenario": scenario}),
    )


def _transition(
    run_id: str, *, scenario: str, index: int, result_shape: str = "healthy"
) -> ExperienceTransition:
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}",
            run_id=run_id,
            iteration=1,
            occurred_at=_TIME + timedelta(minutes=index),
            environment_id="environment:test",
            scenario_id=f"scenario:{scenario}",
            trust_classification="external_untrusted",
            pre_state={"service": "ready"},
            observation={"state": {"service": "ready"}, "condition": "degraded"},
            action={"kind": "restart"},
            result={"shape": result_shape, "ok": result_shape == "healthy"},
            before_objective_metrics=[ObjectiveMetric(name="health", value=1, unit="points")],
            after_objective_metrics=[ObjectiveMetric(name="health", value=3, unit="points")],
            terminal=True,
        )
    )


def _outcome(run_id: str, *, index: int, status: str = "completed") -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        status=status,
        finished_at=_TIME + timedelta(minutes=index, seconds=30),
        stop_reason=status,
    )


def _outcome_record_id(run_id: str) -> str:
    return hashlib.sha256(f"run-outcome:{run_id}".encode()).hexdigest()


async def _seed(*, with_declarations: bool = True):
    store = InMemoryStructuredStore()
    declarations = [_declaration("run-a", "a"), _declaration("run-b", "b")]
    for index, declaration in enumerate(declarations):
        transition = _transition(
            declaration.run_id,
            scenario=declaration.scenario_id.removeprefix("scenario:"),
            index=index,
        )
        await store.append(
            RecordWrite(
                namespace=_EPISODIC,
                record_id=transition.transition_id,
                record_type="experience-transition",
                payload=transition.model_dump(mode="json"),
                created_at=transition.occurred_at,
            ),
            operation="seed-transition",
            idempotency_key=f"transition:{declaration.run_id}",
        )
        outcome = _outcome(declaration.run_id, index=index)
        await store.append(
            RecordWrite(
                namespace=_EPISODIC,
                record_id=_outcome_record_id(declaration.run_id),
                record_type="run-outcome",
                payload=outcome.model_dump(mode="json"),
                created_at=outcome.finished_at,
            ),
            operation="seed-outcome",
            idempotency_key=f"outcome:{declaration.run_id}",
        )
        if with_declarations:
            await store.append(
                RecordWrite(
                    namespace=_DECLARATIONS,
                    record_id=f"lesson-run:{sha256_json({'run_id': declaration.run_id})}",
                    record_type="lesson-run-declaration",
                    payload=declaration.model_dump(mode="json"),
                    created_at=_TIME,
                ),
                operation="seed-declaration",
                idempotency_key=f"declaration:{declaration.run_id}",
            )
    snapshot = await store.create_snapshot(
        namespace=_EPISODIC,
        snapshot_id="evidence-snapshot",
        operation="seed-snapshot",
        idempotency_key="snapshot-1",
    )
    return store, snapshot.snapshot


def _settings() -> ConsolidationSettings:
    return ConsolidationSettings(
        lesson_settings=LessonSettings(
            metric_name="health",
            metric_unit="points",
            direction="maximize",
            condition_keys=("condition",),
        ),
        pattern_settings=PatternQuerySettings(
            scope_paths=("observation.state.service",),
            action_path="action.kind",
            result_path="result.shape",
        ),
    )


def test_store_backed_source_loads_verified_snapshot_and_fails_closed_on_missing_declarations():
    async def scenario() -> None:
        store, snapshot = await _seed()
        source = StoredSnapshotEvidenceSource(
            store,
            evidence_namespace=_EPISODIC,
            declaration_namespace=_DECLARATIONS,
        )
        evidence = await source.load(snapshot.snapshot_id)
        assert {run.run_id for run in evidence.runs} == {"run-a", "run-b"}
        assert len(evidence.records) == 4

        incomplete_store, incomplete_snapshot = await _seed(with_declarations=False)
        incomplete = await StoredSnapshotEvidenceSource(
            incomplete_store,
            evidence_namespace=_EPISODIC,
            declaration_namespace=_DECLARATIONS,
        ).load(incomplete_snapshot.snapshot_id)
        assert incomplete.runs == []
        assert len(incomplete.records) == 4

    _run(scenario())


def test_dry_run_apply_and_retrieval_revalidate_the_same_plan():
    async def scenario() -> None:
        store, snapshot = await _seed()
        memory = ConsolidationMemory(
            store,
            namespace="consolidation",
            evidence_namespace=_EPISODIC,
            declaration_namespace=_DECLARATIONS,
            settings=_settings(),
        )
        request = ConsolidationRequest(
            request_id="consolidate-1",
            snapshot_id=snapshot.snapshot_id,
            idempotency_key="dry-1",
            dry_run=True,
        )
        dry = await memory.consolidate(request)
        assert dry.applied is False
        plans = await store.list(namespace="consolidation")
        assert [record.record_type for record in plans] == [CONSOLIDATION_PLAN_RECORD_TYPE]
        assert len(dry.deltas) >= 2
        assert {delta.operation for delta in dry.deltas} == {"create"}

        applied = await memory.consolidate(request.model_copy(update={"dry_run": False}))
        repeated = await memory.consolidate(request.model_copy(update={"dry_run": False}))
        assert applied.applied is True
        assert repeated == applied
        records = await store.list(namespace="consolidation")
        assert [record.record_type for record in records] == [
            CONSOLIDATION_PLAN_RECORD_TYPE,
            CONSOLIDATION_APPLY_RECORD_TYPE,
        ]

        contribution = await memory.retrieve(
            MemoryContextRequest(
                request_id="read-1",
                run_id="new-run",
                query="restart healthy",
                context={"latest_result": {"state": {"service": "ready"}}},
            )
        )
        assert contribution.items
        assert all(
            item.envelope.trust_classification == "derived_untrusted" for item in contribution.items
        )

    _run(scenario())


def test_unavailable_input_is_recorded_without_synthesizing_knowledge():
    async def scenario() -> None:
        store, snapshot = await _seed(with_declarations=False)
        memory = ConsolidationMemory(
            store,
            namespace="consolidation",
            evidence_namespace=_EPISODIC,
            declaration_namespace=_DECLARATIONS,
            settings=_settings(),
        )
        plan = await memory.dry_run(
            snapshot.snapshot_id, request_id="unavailable", idempotency_key="unavailable-1"
        )
        assert plan.unavailable_reason
        assert plan.active_items == []
        assert plan.deltas == []

    _run(scenario())


def test_tampered_persisted_plan_is_rejected_before_retrieval():
    async def scenario() -> None:
        store, snapshot = await _seed()
        memory = ConsolidationMemory(
            store,
            namespace="consolidation",
            evidence_namespace=_EPISODIC,
            declaration_namespace=_DECLARATIONS,
            settings=_settings(),
        )
        plan = await memory.dry_run(
            snapshot.snapshot_id, request_id="tamper", idempotency_key="tamper-1"
        )
        await memory.apply(plan.plan_id, idempotency_key="apply-tamper")
        stored = store._records[("consolidation", f"consolidation-plan:{plan.plan_id}")]
        stored.payload["request_id"] = "forged"
        with pytest.raises(MemoryPermanentError):
            await memory.retrieve(
                MemoryContextRequest(request_id="read", run_id="new", query="restart")
            )

    _run(scenario())


def test_latest_complete_plan_supersedes_an_older_active_candidate():
    async def scenario() -> None:
        store, first_snapshot = await _seed()
        run_c = _declaration("run-c", "c")
        transition_c = _transition(
            run_c.run_id,
            scenario="c",
            index=2,
            result_shape="unhealthy",
        )
        await store.append(
            RecordWrite(
                namespace=_EPISODIC,
                record_id=transition_c.transition_id,
                record_type="experience-transition",
                payload=transition_c.model_dump(mode="json"),
                created_at=transition_c.occurred_at,
            ),
            operation="seed-later-transition",
            idempotency_key="later:transition",
        )
        outcome_c = _outcome(run_c.run_id, index=2, status="failed")
        await store.append(
            RecordWrite(
                namespace=_EPISODIC,
                record_id=_outcome_record_id(run_c.run_id),
                record_type="run-outcome",
                payload=outcome_c.model_dump(mode="json"),
                created_at=outcome_c.finished_at,
            ),
            operation="seed-later-outcome",
            idempotency_key="later:outcome",
        )
        await store.append(
            RecordWrite(
                namespace=_DECLARATIONS,
                record_id=f"lesson-run:{sha256_json({'run_id': run_c.run_id})}",
                record_type="lesson-run-declaration",
                payload=run_c.model_dump(mode="json"),
                created_at=_TIME,
            ),
            operation="seed-later-declaration",
            idempotency_key="later:declaration",
        )
        later_snapshot = await store.create_snapshot(
            namespace=_EPISODIC,
            snapshot_id="later-snapshot",
            operation="seed-later-snapshot",
            idempotency_key="later-snapshot",
        )
        ticks = iter(
            [
                datetime(2026, 9, 5, 13, tzinfo=UTC),
                datetime(2026, 9, 5, 14, tzinfo=UTC),
            ]
        )
        memory = ConsolidationMemory(
            store,
            namespace="consolidation",
            evidence_namespace=_EPISODIC,
            declaration_namespace=_DECLARATIONS,
            settings=ConsolidationSettings(
                pattern_settings=PatternQuerySettings(
                    scope_paths=("observation.state.service",),
                    action_path="action.kind",
                    result_path="result.shape",
                )
            ),
            clock=lambda: next(ticks),
        )
        first = await memory.dry_run(
            first_snapshot.snapshot_id, request_id="first", idempotency_key="first-dry"
        )
        await memory.apply(first.plan_id, idempotency_key="first-apply")
        assert first.active_items
        second = await memory.dry_run(
            later_snapshot.snapshot.snapshot_id,
            request_id="second",
            idempotency_key="second-dry",
        )
        assert second.active_items == []
        await memory.apply(second.plan_id, idempotency_key="second-apply")

        contribution = await memory.retrieve(
            MemoryContextRequest(
                request_id="read",
                run_id="new-run",
                query="restart healthy",
                context={"latest_result": {"state": {"service": "ready"}}},
            )
        )
        assert contribution.items == []

        # Reapplying the older plan is idempotent and must not make its
        # superseded active item visible again or advance the apply clock.
        await memory.apply(first.plan_id, idempotency_key="first-reapply")
        contribution = await memory.retrieve(
            MemoryContextRequest(
                request_id="read-again",
                run_id="new-run",
                query="restart healthy",
                context={"latest_result": {"state": {"service": "ready"}}},
            )
        )
        assert contribution.items == []

        stale = await memory.dry_run(
            first_snapshot.snapshot_id,
            request_id="stale",
            idempotency_key="stale-dry",
        )
        with pytest.raises(MemoryConflictError):
            await memory.apply(stale.plan_id, idempotency_key="stale-apply")

    _run(scenario())
