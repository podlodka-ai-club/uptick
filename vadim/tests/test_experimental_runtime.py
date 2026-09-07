from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from uptick_agent.evaluation_presets import experimental_presets
from uptick_agent.experimental_runtime import (
    compose_experimental_runtime,
    fixed_evaluation_clock,
    offline_smoke,
)
from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryContextRequest,
    MemoryValidationError,
    ObjectiveMetric,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.lesson_contracts import LessonRunDeclaration
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.memory.stores.contracts import MemorySnapshot
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _transition(
    run_id: str = "run:runtime",
    *,
    scenario_id: str = "default",
    index: int = 0,
) -> ExperienceTransition:
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}",
            run_id=run_id,
            iteration=1,
            occurred_at=_NOW + timedelta(minutes=index),
            environment_id="simulator",
            scenario_id=scenario_id,
            trust_classification="external_untrusted",
            pre_state={"desired_backend_instances": None},
            observation={"action_kind": "get_overview", "ok": True},
            action={"kind": "get_overview"},
            result={"ok": True},
            before_objective_metrics=[
                ObjectiveMetric(name="uptime_ratio", value=0.1, unit="ratio")
            ],
            after_objective_metrics=[ObjectiveMetric(name="uptime_ratio", value=0.2, unit="ratio")],
            terminal=True,
        )
    )


def _outcome(run_id: str = "run:runtime", *, index: int = 0) -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        status="completed",
        finished_at=_NOW + timedelta(minutes=index),
        stop_reason="finished",
    )


def test_offline_smoke_composes_supported_matrix_without_simulator_calls() -> None:
    async def scenario() -> None:
        results = await offline_smoke()
        assert len(results) == 15
        assert all(
            item.status == "ok"
            for item in results
            if item.condition_id != "A6-minus-contradiction-tracking"
        )
        contradiction = next(
            item for item in results if item.condition_id == "A6-minus-contradiction-tracking"
        )
        assert contradiction.status == "unsupported"

    asyncio.run(scenario())


def test_a5_consolidation_is_explicit_and_uses_persisted_plan() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        runtime = compose_experimental_runtime(
            "A5",
            store,
            namespace="runtime:a5",
            clock=lambda: _NOW,
        )
        await runtime.record_transition(_transition())
        assert await store.list(namespace="runtime:a5:maintenance") == []
        receipt = await store.create_snapshot(
            namespace="runtime:a5",
            snapshot_id="training-final",
            operation="freeze-training",
            idempotency_key="snapshot:training-final",
        )
        assert isinstance(receipt.snapshot, MemorySnapshot)
        dry_run = await runtime.consolidate_before_freeze(
            "training-final",
            request_id="consolidate:runtime",
            idempotency_key="consolidate:dry-run",
        )
        assert not dry_run.applied
        applied = await runtime.consolidate_before_freeze(
            "training-final",
            request_id="consolidate:runtime",
            idempotency_key="consolidate:apply",
            apply=True,
        )
        assert applied.applied
        assert await store.list(namespace="runtime:a5:maintenance")

    asyncio.run(scenario())


def test_a2_retrieves_real_shaped_episode_with_preset_budget() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        runtime = compose_experimental_runtime(
            "A2",
            store,
            namespace="runtime:a2-budget",
            clock=lambda: _NOW,
        )
        await runtime.record_transition(_transition())
        await runtime.finalize_run(_outcome())

        context = await runtime.build_context(
            MemoryContextRequest(
                request_id="a2-budget-check",
                run_id="run:other",
                query="get_overview",
            )
        )

        assert context.items
        assert context.items[0].envelope.item_id == "transition:run:runtime"
        assert context.items[0].estimated_tokens > 1_000
        assert runtime.configuration.episodic.max_context_tokens == 4_000
        assert runtime.configuration.context_budget.total_tokens == 16_000

    asyncio.run(scenario())


def test_a5_composes_applied_consolidation_knowledge_into_context() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        runtime = compose_experimental_runtime(
            "A5",
            store,
            namespace="runtime:a5-consolidation",
            run_declarations=(
                LessonRunDeclaration(
                    run_id="run:runtime",
                    logical_run_id="logical:runtime",
                    phase="learning",
                    environment_id="simulator",
                    scenario_id="default",
                    environment_content_hash="a" * 64,
                    scenario_content_hash="b" * 64,
                    eligible=True,
                ),
                LessonRunDeclaration(
                    run_id="run:runtime-2",
                    logical_run_id="logical:runtime-2",
                    phase="learning",
                    environment_id="simulator",
                    scenario_id="alternate",
                    environment_content_hash="a" * 64,
                    scenario_content_hash="c" * 64,
                    eligible=True,
                ),
            ),
            clock=lambda: _NOW,
        )
        await runtime.record_transition(_transition())
        await runtime.record_transition(
            _transition("run:runtime-2", scenario_id="alternate", index=1)
        )
        await runtime.finalize_run(_outcome())
        await runtime.finalize_run(_outcome("run:runtime-2", index=1))
        await store.create_snapshot(
            namespace="runtime:a5-consolidation",
            snapshot_id="training-final",
            operation="freeze-training",
            idempotency_key="snapshot:training-final",
        )
        await runtime.consolidate_before_freeze(
            "training-final",
            request_id="consolidate:runtime:knowledge",
            idempotency_key="consolidate:dry-run",
        )
        applied = await runtime.consolidate_before_freeze(
            "training-final",
            request_id="consolidate:runtime:knowledge",
            idempotency_key="consolidate:apply",
            apply=True,
        )

        assert applied.applied
        consolidation_apply = next(
            record
            for record in await store.list(namespace="runtime:a5-consolidation:consolidation")
            if record.record_type == "consolidation-apply"
        )
        assert consolidation_apply.created_at == _NOW
        assert datetime.fromisoformat(consolidation_apply.payload["applied_at"]) == _NOW
        context = await runtime.build_context(
            MemoryContextRequest(
                request_id="consolidated-context",
                run_id="run:other",
                query="uncertain observational regularity",
                context={"observation": {"action_kind": "get_overview", "ok": True}},
            )
        )
        assert any(item.envelope.origin_module == "consolidation" for item in context.items)
        assert await store.list(namespace="runtime:a5-consolidation:consolidation")

    asyncio.run(scenario())


def test_a6_applies_advanced_retrieval_to_consolidation_contribution() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        runtime = compose_experimental_runtime(
            "A6",
            store,
            namespace="runtime:a6-consolidation-retrieval",
            run_declarations=tuple(
                LessonRunDeclaration(
                    run_id=run_id,
                    logical_run_id=f"logical:{run_id}",
                    phase="learning",
                    environment_id="simulator",
                    scenario_id=scenario_id,
                    environment_content_hash="a" * 64,
                    scenario_content_hash=scenario_hash,
                    eligible=True,
                )
                for run_id, scenario_id, scenario_hash in (
                    ("run:advanced-a", "scenario-advanced-a", "b" * 64),
                    ("run:advanced-b", "scenario-advanced-b", "c" * 64),
                )
            ),
            clock=lambda: _NOW,
        )
        for index, run_id in enumerate(("run:advanced-a", "run:advanced-b")):
            await runtime.record_transition(
                _transition(
                    run_id, scenario_id=f"scenario-{run_id.removeprefix('run:')}", index=index
                )
            )
            await runtime.finalize_run(_outcome(run_id, index=index))
        await store.create_snapshot(
            namespace="runtime:a6-consolidation-retrieval",
            snapshot_id="training-final",
            operation="freeze-training",
            idempotency_key="snapshot:training-final",
        )
        await runtime.consolidate_before_freeze(
            "training-final",
            request_id="consolidate:a6",
            idempotency_key="consolidate:a6:dry-run",
        )
        await runtime.consolidate_before_freeze(
            "training-final",
            request_id="consolidate:a6",
            idempotency_key="consolidate:a6:apply",
            apply=True,
        )

        context = await runtime.build_context(
            MemoryContextRequest(
                request_id="a6-consolidated-context",
                run_id="run:other",
                query="uptime_ratio get_overview",
                context={"observation": {"action_kind": "get_overview", "ok": True}},
            )
        )
        consolidated = [
            item for item in context.items if item.envelope.origin_module == "consolidation"
        ]
        assert consolidated
        assert all(item.selection_reason.startswith("advanced retrieval:") for item in consolidated)

    asyncio.run(scenario())


def test_a9_composes_tool_knowledge_and_forgetting_as_real_modules() -> None:
    runtime = compose_experimental_runtime(
        "A9",
        InMemoryStructuredStore(),
        namespace="runtime:a9",
        clock=lambda: _NOW,
    )
    assert {"tool_knowledge", "forgetting", "consolidation"} <= set(runtime.enabled_module_ids)
    assert runtime.configuration.tool_knowledge_query_settings is not None
    assert runtime.configuration.tool_knowledge_query_settings.adapter_identity == (
        "generic-tool-result-v1"
    )


def test_resolved_arbitrary_configuration_is_accepted_without_preset_lookup() -> None:
    configuration = experimental_presets()[2].configuration.model_copy(
        update={"profile_id": "custom-v2-condition"}
    )
    runtime = compose_experimental_runtime(
        configuration,
        InMemoryStructuredStore(),
        namespace="runtime:custom",
        condition_id="custom.v2-condition",
    )
    assert runtime.preset.condition_id == "custom.v2-condition"


def test_enabled_world_module_requires_explicit_query_settings() -> None:
    configuration = experimental_presets()[4].configuration.model_copy(
        update={"world_query_settings": None}
    )
    with pytest.raises(MemoryValidationError, match="explicit query settings"):
        compose_experimental_runtime(
            configuration,
            InMemoryStructuredStore(),
            namespace="runtime:missing-world-settings",
            condition_id="missing-world-settings",
        )


def test_fixed_evaluation_clock_binds_created_at() -> None:
    clock = fixed_evaluation_clock(_NOW)
    assert clock() == _NOW
