from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import uptick_agent.evaluation.execution as evaluation_execution
from uptick_agent.evaluation_runtime import (
    EvaluationMemoryFacade,
    EvaluationRuntime,
    _memory_telemetry,
    _MemoryAdapter,
)
from uptick_agent.experimental_runtime import compose_experimental_runtime
from uptick_agent.memory.config import ContextBudgetConfig, MemoryConfiguration, ModuleConfig
from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryContextRequest,
    MemoryContribution,
    MemoryTransientError,
    ObjectiveMetric,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.orchestrator import MemoryModuleRegistration, MemoryOrchestrator
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler


def _transition(run_id: str) -> ExperienceTransition:
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}",
            run_id=run_id,
            iteration=1,
            occurred_at=datetime(2026, 9, 5, tzinfo=UTC),
            environment_id="simulator",
            scenario_id="default",
            trust_classification="external_untrusted",
            pre_state={"capacity": 1},
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


def _empty_configuration() -> MemoryConfiguration:
    return MemoryConfiguration(
        compatibility_legacy=ModuleConfig(enabled=False),
        context_budget=ContextBudgetConfig(total_items=4, total_tokens=4_000),
    )


class _FailingContributor:
    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        raise MemoryTransientError("temporary read failure")


def test_disabled_and_failed_modules_are_reported_without_inferred_activity() -> None:
    async def scenario() -> None:
        empty = compose_experimental_runtime(
            _empty_configuration(),
            InMemoryStructuredStore(),
            namespace="telemetry:empty",
        )
        await empty.build_context(MemoryContextRequest(request_id="empty", run_id="run"))
        assert empty.module_telemetry == {}

        configuration = _empty_configuration().model_copy(
            update={"episodic": ModuleConfig(enabled=True)}
        )
        orchestrator = MemoryOrchestrator(
            configuration,
            [MemoryModuleRegistration("episodic", lambda _: _FailingContributor())],
        )
        context = await orchestrator.build_context(
            MemoryContextRequest(request_id="failed", run_id="run")
        )
        telemetry = orchestrator.module_telemetry["episodic"]
        assert context.items == []
        assert telemetry.construction_events == 1
        assert telemetry.read_events == 1
        assert telemetry.contribution_events == 0
        assert telemetry.write_events == 0

    asyncio.run(scenario())


def test_facade_sums_real_reader_and_writer_module_lifecycle_calls() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        reader = compose_experimental_runtime(
            MemoryConfiguration.episodic_only(), store, namespace="telemetry:reader"
        )
        writer = compose_experimental_runtime(
            MemoryConfiguration.episodic_only(), store, namespace="telemetry:writer"
        )
        await reader.record_transition(_transition("training"))
        await reader.finalize_run(
            RunOutcome(run_id="training", status="completed", stop_reason="finished")
        )
        await reader.build_context(MemoryContextRequest(request_id="read", run_id="eval"))
        await writer.record_transition(_transition("evaluation"))
        await writer.finalize_run(
            RunOutcome(run_id="evaluation", status="completed", stop_reason="finished")
        )

        facade = EvaluationMemoryFacade(reader, writer, frozen_snapshot_members=1)
        telemetry = facade.module_telemetry
        assert telemetry is not None
        assert telemetry["episodic"]["construction_events"] == 2
        assert telemetry["episodic"]["read_events"] == 1
        assert telemetry["episodic"]["contribution_events"] == 1
        assert telemetry["episodic"]["write_events"] == 2
        assert telemetry["episodic"]["finalization_events"] == 2

        adapter = _MemoryAdapter(facade)
        measured = _memory_telemetry(adapter, None)
        assert measured.status == "available"
        assert measured.snapshot_members == 1
        assert measured.module_ids == ("episodic",)
        assert measured.module_construction_events == 2
        assert measured.module_read_events == 1
        assert measured.module_write_events == 2
        assert measured.module_contribution_events == 1

    asyncio.run(scenario())


def test_finalize_cancellation_is_not_relabelled_as_memory_failure() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class Memory:
            async def finalize_run(self, outcome: RunOutcome) -> None:
                started.set()
                await release.wait()

        adapter = _MemoryAdapter(Memory())
        task = asyncio.create_task(
            adapter.finalize_run(
                RunOutcome(run_id="run", status="completed", stop_reason="finished")
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_empty_and_partial_module_measurements_remain_truthful(monkeypatch) -> None:
    class EmptyMemory:
        @property
        def context_diagnostics(self):
            return {}

        module_telemetry = {}

    class PartialMemory:
        @property
        def context_diagnostics(self):
            return {}

        module_telemetry = {
            "episodic": {
                "module_version": "1.0",
                "construction_events": 1,
                # A missing read counter is unknown, not zero.
                "write_events": 2,
            }
        }

    empty = _memory_telemetry(_MemoryAdapter(EmptyMemory()), None)
    assert empty.status == "available"
    assert empty.module_construction_events == 0
    assert empty.module_read_events == 0
    assert empty.module_ids == ()

    partial = _memory_telemetry(_MemoryAdapter(PartialMemory()), None)
    assert partial.status == "available"
    assert partial.module_ids == ("episodic",)
    assert partial.module_construction_events == 1
    assert partial.module_read_events is None
    assert partial.module_write_events == 2

    class MalformedMemory:
        @property
        def context_diagnostics(self):
            return {}

        module_telemetry = {
            "episodic": {
                "module_version": "1.0",
                "construction_events": 1,
                "read_events": 1,
                "write_events": 1,
                "contribution_events": 0,
                "finalization_events": 0,
                "consolidation_events": 0,
            },
            "foreign": object(),
        }

    malformed = _memory_telemetry(_MemoryAdapter(MalformedMemory()), None)
    assert malformed.status == "available"
    assert malformed.module_ids == ()
    assert malformed.module_construction_events is None

    reader = SimpleNamespace(module_telemetry=PartialMemory.module_telemetry)
    writer = SimpleNamespace(
        module_telemetry={
            "episodic": {
                "module_version": "1.0",
                "construction_events": 1,
                "read_events": 1,
                "write_events": 1,
                "contribution_events": 0,
                "finalization_events": 1,
                "consolidation_events": 0,
            }
        }
    )
    merged = EvaluationMemoryFacade(reader, writer).module_telemetry
    assert merged is not None
    assert merged["episodic"]["read_events"] is None

    monkeypatch.setattr(evaluation_execution, "_STORED_ARTIFACT_COUNT_TIMEOUT_SECONDS", 0.01)

    class HangingFactory:
        async def stored_artifact_count(self, condition, attempt, phase):
            await asyncio.sleep(10)

    runtime = object.__new__(EvaluationRuntime)
    runtime.memory_factory = HangingFactory()
    adapter = _MemoryAdapter(EmptyMemory())
    asyncio.run(
        runtime._refresh_stored_artifact_count(
            adapter,
            SimpleNamespace(),
            SimpleNamespace(phase="evaluation"),
        )
    )
    assert adapter.stored_artifacts is None
