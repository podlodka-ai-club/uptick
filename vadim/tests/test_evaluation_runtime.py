from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import uptick_agent.evaluation_runtime as evaluation_runtime
from uptick_agent.evaluation import (
    V2AttemptRecord,
    V2Condition,
    V2EnvironmentPin,
    V2EvaluationProfile,
    V2OutcomeMetrics,
    V2ProviderPin,
    V2SourcePin,
    freeze_evaluation_binding,
    resolved_manifest,
    sha256_json,
)
from uptick_agent.evaluation_presets import experimental_presets
from uptick_agent.evaluation_runtime import (
    DefaultEvaluationMemoryFactory,
    EvaluationJournal,
    EvaluationRuntime,
    FilesystemEvaluationArtifactStore,
    InMemoryEvaluationArtifactStore,
    _provider_telemetry,
)
from uptick_agent.memory.config import AuditConfiguration, MemoryConfiguration, ModuleConfig
from uptick_agent.memory.contracts import (
    DecisionMemoryContext,
    ExperienceTransition,
    RunOutcome,
)
from uptick_agent.memory.lesson_contracts import LessonSettings
from uptick_agent.memory.stores.contracts import RecordWrite
from uptick_agent.memory.stores.in_memory import InMemoryStructuredStore
from uptick_agent.models import ApplyFix, GetOverview, NextStep, RunResult, ToolResult

HASH = "a" * 64
NOW = datetime(2026, 9, 5, tzinfo=UTC)


def _manifest(
    *,
    config=None,
    training_seeds=(1,),
    evaluation_seeds=(2,),
    environment=None,
    world_contexts=None,
    max_wall_seconds=None,
):
    config = config or MemoryConfiguration.episodic_only()
    profile = V2EvaluationProfile(
        profile_id="runtime-test",
        environment=environment
        or V2EnvironmentPin(
            environment_id="simulator",
            environment_version="2",
            adapter_id="simulator-v2",
            adapter_version="1",
            scenario_id="default",
            api_contract_fingerprint=HASH,
        ),
        world_contexts=world_contexts or {},
        provider=V2ProviderPin(
            provider="fake",
            model="test-model",
            settings={"temperature": 0},
            prompt_fingerprint=HASH,
            settings_fingerprint=sha256_json({"temperature": 0}),
            token_estimator_id="chars",
            token_estimator_version="1",
            policy_id="test-policy",
            policy_version="1",
        ),
        source=V2SourcePin(
            source_revision="b" * 40,
            source_tree_hash=HASH,
            dependency_lock_hash=HASH,
        ),
        conditions=tuple(
            V2Condition(
                condition_id=condition_id,
                memory_configuration=config,
                memory_configuration_fingerprint=config.fingerprint,
            )
            for condition_id in ("A0", "A1")
        ),
        baseline_condition_id="A0",
        training_seeds=training_seeds,
        evaluation_seeds=evaluation_seeds,
        replicate_indices=(0,),
        budget={"max_steps": 2, "max_wall_seconds": max_wall_seconds},
        audit_configuration=AuditConfiguration(),
    )
    return resolved_manifest(profile, created_at=NOW)


@dataclass
class _Session:
    run_id: str
    seed: int
    environment_id: str = "simulator"
    scenario_id: str = "default"


class _Environment:
    def __init__(self, events: list[tuple], *, fail: bool = False, terminal: bool = True) -> None:
        self.events = events
        self.fail = fail
        self.terminal = terminal
        self.session: _Session | None = None
        self.closed = False

    async def start(self, *, seed: int, agent_id: str, agent_version: str):
        self.events.append(("environment.start", seed))
        if self.fail:
            raise RuntimeError("simulator unavailable")
        self.session = _Session(f"run-{seed}-{len(self.events)}", seed)
        return self.session, ToolResult(action_kind="start", summary="started")

    async def execute(self, session: _Session, action):
        return ToolResult(action_kind=action.kind, summary="observed", terminal=self.terminal)

    async def finish(
        self, session: _Session, *, steps: int, duration_seconds: float, stop_reason: str
    ):
        return RunResult(
            run_id=session.run_id,
            seed=session.seed,
            agent_id="uptick-v2-evaluation",
            agent_version="test",
            status="completed",
            steps=steps,
            duration_seconds=duration_seconds,
            objective_kind="uptime_cost",
            uptime_ratio=1.0,
            slo_passed=True,
            total_cost_minor=10,
            stop_reason=stop_reason,
        )

    async def aclose(self) -> None:
        self.closed = True


class _LessonEnvironment(_Environment):
    def __init__(self, events: list[tuple], *, terminal: bool = True) -> None:
        super().__init__(events, terminal=terminal)
        self.environment_id = "simulator"
        self.scenario_id = "default"

    async def start(self, *, seed: int, agent_id: str, agent_version: str):
        session, _ = await super().start(seed=seed, agent_id=agent_id, agent_version=agent_version)
        session.environment_id = self.environment_id
        session.scenario_id = self.scenario_id
        return session, ToolResult(
            action_kind="start",
            summary="service pressure",
            objective_metrics=[{"name": "health", "value": 50, "unit": "points"}],
        )

    async def execute(self, session: _Session, action):
        return ToolResult(
            action_kind=action.kind,
            summary="service pressure",
            data={"applied": True},
            objective_metrics=[{"name": "health", "value": 60, "unit": "points"}],
            terminal=True,
        )


class _Model:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events
        self.last_telemetry = {
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "elapsed_seconds": 0.1,
            "request_count": 1,
            "usage_reported_requests": 1,
            "cost_currency": "USD",
            "cost_minor": 1,
        }

    async def decide(self, context):
        self.events.append(("model.decide", context.run_id))
        return NextStep(
            current_situation="healthy",
            hypothesis="observe",
            remaining_steps=[],
            task_completed=False,
            action=GetOverview(),
        )


class _SlowModel(_Model):
    def __init__(self, events: list[tuple], delay: float) -> None:
        super().__init__(events)
        self.delay = delay

    async def decide(self, context):
        await asyncio.sleep(self.delay)
        return await super().decide(context)


class _LessonModel(_Model):
    def __init__(self, events: list[tuple], lesson_counts: list[int]) -> None:
        super().__init__(events)
        self.lesson_counts = lesson_counts
        self.run_ids: list[str] = []
        self.contexts: list[DecisionMemoryContext] = []

    async def decide(self, context):
        self.run_ids.append(context.run_id)
        self.contexts.append(context.memory_context)
        self.lesson_counts.append(
            sum(item.envelope.origin_module == "lessons" for item in context.memory_context.items)
        )
        self.events.append(("model.decide", context.run_id))
        return NextStep(
            current_situation="service pressure",
            hypothesis="apply the known remediation",
            remaining_steps=[],
            task_completed=False,
            action=ApplyFix(message="restore capacity"),
        )


class _ContextModel(_Model):
    def __init__(self, events: list[tuple], contexts: list[tuple]) -> None:
        super().__init__(events)
        self.contexts = contexts

    async def decide(self, context):
        self.contexts.append((context.run_id, len(context.memory_context.items)))
        return await super().decide(context)


class _Memory:
    def __init__(self, events: list[tuple], binding_seen: list[object]) -> None:
        self.events = events
        self.binding_seen = binding_seen
        self.entries: list[str] = []
        self._diagnostics = {"used_items": 0, "used_estimated_tokens": 0}

    async def build_context(self, request):
        self.events.append(("memory.read", request.run_id))
        return DecisionMemoryContext()

    async def remember(self, entry):
        self.entries.append(entry.id)

    async def record_transition(self, transition: ExperienceTransition):
        return None

    async def clear(self, run_id=None):
        return None

    async def finalize_run(self, outcome: RunOutcome):
        return None

    async def record_trace(self, write):
        return None

    @property
    def context_diagnostics(self):
        return self._diagnostics


def _run(runtime: EvaluationRuntime):
    return asyncio.run(runtime.run())


def _binding_factory(manifest, seen):
    async def factory(condition, training_attempts):
        seen.append((condition.condition_id, tuple(item.attempt_id for item in training_attempts)))
        return freeze_evaluation_binding(
            manifest,
            condition_id=condition.condition_id,
            cache_namespace=f"cache-{condition.condition_id}",
            audit_namespace=f"audit-{condition.condition_id}",
            training_attempt_ids=(item.attempt_id for item in training_attempts),
            training_world_contexts={
                item.world_seed: manifest.profile.environment for item in training_attempts
            },
        )

    return factory


def test_runtime_seals_manifest_before_start_and_passes_physical_run_id_to_factories() -> None:
    manifest = _manifest()
    events: list[tuple] = []
    bindings: list[object] = []
    envs: list[_Environment] = []
    memories: list[_Memory] = []

    def environment_factory(block, condition, attempt):
        env = _Environment(events)
        envs.append(env)
        return env

    def model_factory(block, condition, attempt, run_id):
        assert run_id.startswith("run-")
        events.append(("model.factory", run_id))
        return _Model(events)

    def memory_factory(block, condition, attempt, run_id, phase, binding):
        assert run_id.startswith("run-")
        assert (phase == "evaluation") == (binding is not None)
        events.append(("memory.factory", run_id, phase))
        memory = _Memory(events, bindings)
        memories.append(memory)
        return memory

    artifacts = InMemoryEvaluationArtifactStore()
    runtime = EvaluationRuntime(
        manifest,
        environment_factory=environment_factory,
        model_factory=model_factory,
        memory_factory=memory_factory,
        binding_factory=_binding_factory(manifest, bindings),
        journal=EvaluationJournal(manifest, artifacts=artifacts),
    )
    report = _run(runtime)

    assert artifacts.manifest is not None
    assert artifacts.manifest.manifest_hash == manifest.manifest_hash
    assert events[0][0] == "environment.start"
    assert all(item.status == "completed" for item in report.retained_attempts)
    assert all(env.closed for env in envs)
    assert len(bindings) == 2
    expected_cells = sum(len(block.conditions) for block in manifest.run_matrix)
    assert len(artifacts.lifecycle) == expected_cells * 3


def test_start_failure_is_terminal_without_run_id_and_does_not_abort_other_cells() -> None:
    manifest = _manifest()
    created: list[_Environment] = []

    def environment_factory(block, condition, attempt):
        failed = block.world_seed == 1 and condition.condition_id == "A0"
        env = _Environment([], fail=failed)
        created.append(env)
        return env

    def model_factory(block, condition, attempt, run_id):
        return _Model([])

    def memory_factory(block, condition, attempt, run_id, phase, binding):
        return _Memory([], [])

    report = _run(
        EvaluationRuntime(
            manifest,
            environment_factory=environment_factory,
            model_factory=model_factory,
            memory_factory=memory_factory,
            binding_factory=_binding_factory(manifest, []),
        )
    )
    failed = next(
        item
        for item in report.retained_attempts
        if item.condition_id == "A0" and item.phase == "training"
    )
    assert failed.status == "failed"
    assert failed.run_id is None
    assert failed.failure_stage == "startup"
    assert sum(item.status == "completed" for item in report.retained_attempts) == 3


def test_wall_budget_interrupts_cell_and_preserves_partial_trace() -> None:
    manifest = _manifest(max_wall_seconds=0.01)
    events: list[tuple] = []

    report = _run(
        EvaluationRuntime(
            manifest,
            environment_factory=lambda *_: _Environment(events),
            model_factory=lambda *_: _SlowModel(events, 0.1),
            memory_factory=lambda *_: _Memory(events, []),
            binding_factory=_binding_factory(manifest, []),
        )
    )

    assert report.retained_attempts
    assert all(item.status == "interrupted" for item in report.retained_attempts)
    assert all(item.trace_hash is not None for item in report.retained_attempts)
    assert all(
        item.failure_reason == "per-attempt wall time budget exceeded"
        for item in report.retained_attempts
    )


def test_user_cancellation_propagates_without_starting_following_cells() -> None:
    manifest = _manifest(max_wall_seconds=1.0)
    events: list[tuple] = []
    runtime = EvaluationRuntime(
        manifest,
        environment_factory=lambda *_: _Environment(events),
        model_factory=lambda *_: _SlowModel(events, 0.1),
        memory_factory=lambda *_: _Memory(events, []),
        binding_factory=_binding_factory(manifest, []),
    )

    async def scenario() -> None:
        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert [event for event in events if event[0] == "environment.start"] == [
        ("environment.start", 1)
    ]
    attempts = runtime.journal.reduce_attempts()
    assert len(attempts) == 1
    assert attempts[0].status == "interrupted"
    assert attempts[0].failure_reason == "evaluation task cancelled"
    assert attempts[0].trace_hash is not None


def test_cancellation_during_memory_finalization_propagates_without_next_cell() -> None:
    manifest = _manifest(max_wall_seconds=1.0)
    events: list[tuple] = []
    finalization_started = asyncio.Event()

    class _BlockingFinalizationMemory(_Memory):
        async def finalize_run(self, outcome: RunOutcome):
            finalization_started.set()
            await asyncio.Event().wait()

    runtime = EvaluationRuntime(
        manifest,
        environment_factory=lambda *_: _Environment(events),
        model_factory=lambda *_: _Model(events),
        memory_factory=lambda *_: _BlockingFinalizationMemory(events, []),
        binding_factory=_binding_factory(manifest, []),
    )

    async def scenario() -> None:
        task = asyncio.create_task(runtime.run())
        await finalization_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert [event for event in events if event[0] == "environment.start"] == [
        ("environment.start", 1)
    ]
    attempts = runtime.journal.reduce_attempts()
    assert len(attempts) == 1
    assert attempts[0].status == "interrupted"
    assert attempts[0].failure_reason == "evaluation task cancelled"


def test_hanging_memory_artifact_measurement_does_not_change_completed_outcomes(
    monkeypatch,
) -> None:
    monkeypatch.setattr(evaluation_runtime, "_STORED_ARTIFACT_COUNT_TIMEOUT_SECONDS", 0.01)
    manifest = _manifest()
    events: list[tuple] = []

    class _HangingMemoryFactory:
        def __call__(self, *args):
            return _Memory(events, [])

        async def stored_artifact_count(self, condition, attempt, phase):
            await asyncio.sleep(10)

    report = _run(
        EvaluationRuntime(
            manifest,
            environment_factory=lambda *_: _Environment(events),
            model_factory=lambda *_: _Model(events),
            memory_factory=_HangingMemoryFactory(),
            binding_factory=_binding_factory(manifest, []),
        )
    )
    assert report.retained_attempts
    assert all(item.status == "completed" for item in report.retained_attempts)
    assert all(item.memory_telemetry.stored_artifacts is None for item in report.retained_attempts)


def test_evaluation_binding_is_frozen_before_any_evaluation_start() -> None:
    manifest = _manifest()
    events: list[tuple] = []
    binding_calls: list[object] = []

    def environment_factory(block, condition, attempt):
        events.append(("binding-freeze-check", block.phase))
        return _Environment(events)

    def model_factory(block, condition, attempt, run_id):
        return _Model(events)

    def memory_factory(block, condition, attempt, run_id, phase, binding):
        return _Memory(events, [])

    report = _run(
        EvaluationRuntime(
            manifest,
            environment_factory=environment_factory,
            model_factory=model_factory,
            memory_factory=memory_factory,
            binding_factory=_binding_factory(manifest, binding_calls),
        )
    )
    first_eval_start = next(
        index for index, event in enumerate(events) if event == ("environment.start", 2)
    )
    assert binding_calls == [
        ("A0", (report.retained_attempts[0].attempt_id,)),
        ("A1", (report.retained_attempts[1].attempt_id,)),
    ]
    assert ("environment.start", 2) not in events[:first_eval_start]


def test_binding_failure_is_reported_without_starting_unbound_evaluation_cells() -> None:
    manifest = _manifest()
    events: list[tuple] = []
    artifacts = InMemoryEvaluationArtifactStore()

    async def binding_factory(condition, training_attempts):
        raise ValueError(f"freeze rejected {condition.condition_id}: secret=redacted")

    report = _run(
        EvaluationRuntime(
            manifest,
            environment_factory=lambda *_: _Environment(events),
            model_factory=lambda *_: _Model(events),
            memory_factory=lambda *_: _Memory(events, []),
            binding_factory=binding_factory,
            journal=EvaluationJournal(manifest, artifacts=artifacts),
        )
    )

    assert report.coverage_complete
    assert all(
        item.status == "failed" for item in report.retained_attempts if item.phase == "evaluation"
    )
    assert not [event for event in events if event[0] == "environment.start" and event[1] == 2]
    binding_errors = [
        artifact["value"]
        for (kind, _artifact_id), artifact in artifacts.artifacts.items()
        if kind == "binding-error"
    ]
    assert len(binding_errors) == 2
    assert all("<redacted>" in value["error"] for value in binding_errors)
    assert all("secret=" not in value["error"] for value in binding_errors)


def test_binding_is_not_usable_when_its_artifact_cannot_be_persisted() -> None:
    manifest = _manifest()
    events: list[tuple] = []

    class _RejectBindingArtifactStore(InMemoryEvaluationArtifactStore):
        def put(self, kind, artifact_id, value):
            if kind == "binding":
                raise OSError("binding artifact is unavailable")
            return super().put(kind, artifact_id, value)

    artifacts = _RejectBindingArtifactStore()

    async def binding_factory(condition, training_attempts):
        return freeze_evaluation_binding(
            manifest,
            condition_id=condition.condition_id,
            cache_namespace=f"cache-{condition.condition_id}",
            audit_namespace=f"audit-{condition.condition_id}",
            training_attempt_ids=(item.attempt_id for item in training_attempts),
            training_world_contexts={
                item.world_seed: manifest.profile.environment for item in training_attempts
            },
        )

    report = _run(
        EvaluationRuntime(
            manifest,
            environment_factory=lambda *_: _Environment(events),
            model_factory=lambda *_: _Model(events),
            memory_factory=lambda *_: _Memory(events, []),
            binding_factory=binding_factory,
            journal=EvaluationJournal(manifest, artifacts=artifacts),
        )
    )

    assert all(
        item.status == "failed" for item in report.retained_attempts if item.phase == "evaluation"
    )
    assert not [event for event in events if event[0] == "environment.start" and event[1] == 2]


def test_journal_keeps_retry_diagnostic_and_first_attempt_primary() -> None:
    manifest = _manifest()
    journal = EvaluationJournal(manifest)
    block = manifest.run_matrix[0]
    base = dict(
        manifest_id=manifest.manifest_id,
        logical_run_id="logical",
        block_id=block.block_id,
        phase=block.phase,
        condition_id="A0",
        environment_id=block.environment_id,
        scenario_id=block.scenario_id,
        world_seed=block.world_seed,
        replicate_index=block.replicate_index,
        requested_at=NOW,
    )
    first = V2AttemptRecord(attempt_id="first", status="requested", **base)
    journal.append(first)
    journal.append(
        first.model_copy(update={"status": "running", "run_id": "run-first", "started_at": NOW})
    )
    failed = first.model_copy(
        update={
            "status": "failed",
            "run_id": "run-first",
            "started_at": NOW,
            "finished_at": NOW,
            "failure_stage": "execution",
            "failure_class": "transient",
            "failure_reason": "temporary",
        }
    )
    journal.append(failed)
    retry = V2AttemptRecord(
        attempt_id="retry",
        attempt_index=1,
        retry_of="first",
        status="requested",
        **base,
    )
    journal.append(retry)
    journal.append(
        retry.model_copy(update={"status": "running", "run_id": "run-retry", "started_at": NOW})
    )
    completed = retry.model_copy(
        update={
            "status": "completed",
            "run_id": "run-retry",
            "started_at": NOW,
            "finished_at": NOW,
            "outcome": V2OutcomeMetrics(
                run_status="completed",
                uptime_ratio=1,
                slo_passed=True,
                total_cost_minor=1,
            ),
        }
    )
    journal.append(completed)
    reduced = journal.reduce_attempts()
    assert tuple(item.attempt_id for item in reduced) == ("first", "retry")
    assert reduced[0].status == "failed"
    assert reduced[1].status == "completed"


def test_filesystem_artifacts_flush_manifest_and_lifecycle(tmp_path: Path) -> None:
    manifest = _manifest()
    store = FilesystemEvaluationArtifactStore(tmp_path)
    journal = EvaluationJournal(manifest, artifacts=store)
    assert (tmp_path / "manifest.json").exists()
    block = manifest.run_matrix[0]
    attempt = V2AttemptRecord(
        manifest_id=manifest.manifest_id,
        attempt_id="filesystem",
        logical_run_id="logical",
        block_id=block.block_id,
        phase=block.phase,
        condition_id="A0",
        environment_id=block.environment_id,
        scenario_id=block.scenario_id,
        world_seed=block.world_seed,
        replicate_index=block.replicate_index,
        status="requested",
        requested_at=NOW,
    )
    journal.append(attempt)
    assert (tmp_path / "lifecycle.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_filesystem_journal_refuses_reusing_output_before_external_calls(tmp_path: Path) -> None:
    manifest = _manifest()
    artifacts = FilesystemEvaluationArtifactStore(tmp_path)
    journal = EvaluationJournal(manifest, artifacts=artifacts)
    block = manifest.run_matrix[0]
    journal.append(
        V2AttemptRecord(
            manifest_id=manifest.manifest_id,
            attempt_id="existing",
            logical_run_id="logical",
            block_id=block.block_id,
            phase=block.phase,
            condition_id="A0",
            environment_id=block.environment_id,
            scenario_id=block.scenario_id,
            world_seed=block.world_seed,
            replicate_index=block.replicate_index,
            status="requested",
            requested_at=NOW,
        )
    )
    calls: list[str] = []

    def environment_factory(block, condition, attempt):
        calls.append("environment")
        return _Environment([])

    with pytest.raises(ValueError, match="already contains a lifecycle journal"):
        EvaluationRuntime(
            manifest,
            environment_factory=environment_factory,
            model_factory=lambda *_: _Model([]),
            journal=EvaluationJournal(manifest, artifacts=artifacts),
        )
    assert calls == []


def test_default_factory_refuses_existing_training_namespace_before_start() -> None:
    manifest = _manifest()
    store = InMemoryStructuredStore()
    factory = DefaultEvaluationMemoryFactory(manifest, store=store)
    namespace = factory._namespace("A0", "training")
    asyncio.run(
        store.append(
            RecordWrite(
                namespace=namespace,
                record_id="old-record",
                record_type="old-record",
                payload={},
                created_at=NOW,
            ),
            operation="test-existing-training-record",
            idempotency_key="test-existing-training-record",
        )
    )
    started: list[str] = []

    def environment_factory(*_):
        started.append("start")
        return _Environment([])

    runtime = EvaluationRuntime(
        manifest,
        environment_factory=environment_factory,
        model_factory=lambda *_: _Model([]),
        memory_factory=factory,
        binding_factory=factory.freeze_binding,
    )
    with pytest.raises(ValueError, match="training namespace is nonempty"):
        _run(runtime)
    assert started == []


def test_default_memory_factory_reads_frozen_training_and_excludes_eval_writes() -> None:
    manifest = _manifest()
    contexts: list[tuple] = []

    def environment_factory(block, condition, attempt):
        return _Environment([], terminal=False)

    def model_factory(block, condition, attempt, run_id):
        return _ContextModel([], contexts)

    report = _run(
        EvaluationRuntime(
            manifest,
            environment_factory=environment_factory,
            model_factory=model_factory,
        )
    )
    assert report.coverage_complete
    # Two training decisions per condition establish non-empty snapshots.
    evaluation_contexts = [count for run_id, count in contexts if run_id.startswith("run-2-")]
    assert evaluation_contexts
    assert all(count > 0 for count in evaluation_contexts)
    assert len(set(evaluation_contexts)) == 1
    evaluation_attempts = [item for item in report.retained_attempts if item.phase == "evaluation"]
    assert evaluation_attempts
    assert all(
        item.memory_telemetry.snapshot_members is not None
        and item.memory_telemetry.snapshot_members > 0
        for item in evaluation_attempts
    )
    assert all(
        item.memory_telemetry.stored_artifacts is not None
        and item.memory_telemetry.stored_artifacts > 0
        for item in evaluation_attempts
    )
    assert all(item.memory_telemetry.module_ids == ("episodic",) for item in evaluation_attempts)
    assert all(
        item.memory_telemetry.module_construction_events is not None
        and item.memory_telemetry.module_construction_events >= 2
        and item.memory_telemetry.module_read_events is not None
        and item.memory_telemetry.module_read_events >= 1
        and item.memory_telemetry.module_write_events is not None
        and item.memory_telemetry.module_write_events >= 1
        and item.memory_telemetry.module_contribution_events is not None
        and item.memory_telemetry.module_contribution_events >= 1
        for item in evaluation_attempts
    )


def test_default_memory_factory_freezes_nonempty_lessons_for_evaluation() -> None:
    configuration = MemoryConfiguration.episodic_with_lessons(
        lesson_settings=LessonSettings(
            metric_name="health",
            metric_unit="points",
            direction="maximize",
            condition_keys=("summary",),
        )
    )
    configuration.episodic.max_context_items = 0
    configuration.lessons.max_context_tokens = 16_000
    environment = V2EnvironmentPin(
        environment_id="simulator",
        environment_version="2",
        adapter_id="simulator-v2",
        adapter_version="1",
        scenario_id="default",
        api_contract_fingerprint=HASH,
        context_identity_verified=True,
        environment_content_hash=HASH,
        scenario_content_hash=HASH,
    )
    alternate_environment = environment.model_copy(
        update={"scenario_id": "scenario-4", "scenario_content_hash": "b" * 64}
    )
    manifest = _manifest(
        config=configuration,
        training_seeds=(1, 4),
        evaluation_seeds=(2,),
        environment=environment,
        world_contexts={4: alternate_environment},
    )
    store = InMemoryStructuredStore()
    memory_factory = DefaultEvaluationMemoryFactory(manifest, store=store)
    lesson_counts: list[int] = []
    lesson_runs: list[str] = []
    models: list[_LessonModel] = []

    def environment_factory(block, condition, attempt):
        environment = _LessonEnvironment([], terminal=True)
        environment.environment_id = block.environment_id
        environment.scenario_id = block.scenario_id
        return environment

    def model_factory(block, condition, attempt, run_id):
        model = _LessonModel([], lesson_counts)
        models.append(model)
        return model

    runtime = EvaluationRuntime(
        manifest,
        environment_factory=environment_factory,
        model_factory=model_factory,
        memory_factory=memory_factory,
        binding_factory=memory_factory.freeze_binding,
    )
    report = _run(runtime)
    lesson_runs.extend(run_id for model in models for run_id in model.run_ids)
    assert report.coverage_complete
    assert any(count > 0 for count in lesson_counts), [
        (model.run_ids, context.warnings, [item.envelope.origin_module for item in context.items])
        for model in models
        for context in model.contexts
    ]
    assert any(run_id.startswith("run-2-") for run_id in lesson_runs)
    eval_records = asyncio.run(store.list(namespace=f"{manifest.manifest_id}:A0:evaluation"))
    assert eval_records == []


@pytest.mark.parametrize("preset_index", [5, 9])
def test_default_factory_applies_verified_consolidation_for_a5_and_a9(
    preset_index: int,
) -> None:
    configuration = experimental_presets()[preset_index].configuration
    environment = V2EnvironmentPin(
        environment_id="simulator",
        environment_version="2",
        adapter_id="simulator-v2",
        adapter_version="1",
        scenario_id="default",
        api_contract_fingerprint=HASH,
        context_identity_verified=True,
        environment_content_hash=HASH,
        scenario_content_hash=HASH,
    )
    alternate_environment = environment.model_copy(
        update={"scenario_id": "scenario-4", "scenario_content_hash": "b" * 64}
    )
    manifest = _manifest(
        config=configuration,
        training_seeds=(1, 4),
        evaluation_seeds=(2,),
        environment=environment,
        world_contexts={4: alternate_environment},
    )
    store = InMemoryStructuredStore()
    memory_factory = DefaultEvaluationMemoryFactory(manifest, store=store)
    models: list[_LessonModel] = []

    def environment_factory(block, condition, attempt):
        value = _LessonEnvironment([])
        value.environment_id = block.environment_id
        value.scenario_id = block.scenario_id
        return value

    def model_factory(*_):
        model = _LessonModel([], [])
        models.append(model)
        return model

    report = _run(
        EvaluationRuntime(
            manifest,
            environment_factory=environment_factory,
            model_factory=model_factory,
            memory_factory=memory_factory,
            binding_factory=memory_factory.freeze_binding,
        )
    )

    assert report.coverage_complete
    assert all(item.status == "completed" for item in report.retained_attempts)
    assert any(
        item.envelope.origin_module == "consolidation"
        for model in models
        for context in model.contexts
        for item in context.items
    )
    consolidation_records = asyncio.run(
        store.list(namespace=memory_factory._namespace("A0", "training") + ":consolidation")
    )
    plan_records = [
        record for record in consolidation_records if record.record_type == "consolidation-plan"
    ]
    assert plan_records
    assert any(record.payload["active_items"] for record in plan_records)


def test_default_factory_keeps_unknown_context_consolidation_explicitly_unavailable() -> None:
    configuration = experimental_presets()[5].configuration
    manifest = _manifest(config=configuration)
    store = InMemoryStructuredStore()
    memory_factory = DefaultEvaluationMemoryFactory(manifest, store=store)

    report = _run(
        EvaluationRuntime(
            manifest,
            environment_factory=lambda *_: _LessonEnvironment([]),
            model_factory=lambda *_: _LessonModel([], []),
            memory_factory=memory_factory,
            binding_factory=memory_factory.freeze_binding,
        )
    )

    assert report.coverage_complete
    consolidation_records = asyncio.run(
        store.list(namespace=memory_factory._namespace("A0", "training") + ":consolidation")
    )
    plans = [
        record for record in consolidation_records if record.record_type == "consolidation-plan"
    ]
    assert plans
    assert all(record.payload["unavailable_reason"] for record in plans)
    assert all(not record.payload["active_items"] for record in plans)


def test_default_factory_freezes_world_evidence_without_lesson_module() -> None:
    configuration = experimental_presets()[4].configuration.model_copy(
        update={
            "lessons": ModuleConfig(
                enabled=False,
                version="1.0",
                status="experimental",
                max_context_items=32,
                max_context_tokens=4_000,
            ),
            "lesson_settings": None,
        }
    )
    environment = V2EnvironmentPin(
        environment_id="simulator",
        environment_version="2",
        adapter_id="simulator-v2",
        adapter_version="1",
        scenario_id="default",
        api_contract_fingerprint=HASH,
        context_identity_verified=True,
        environment_content_hash=HASH,
        scenario_content_hash=HASH,
    )
    alternate_environment = environment.model_copy(
        update={"scenario_id": "scenario-4", "scenario_content_hash": "b" * 64}
    )
    manifest = _manifest(
        config=configuration,
        training_seeds=(1, 4),
        evaluation_seeds=(2,),
        environment=environment,
        world_contexts={4: alternate_environment},
    )
    store = InMemoryStructuredStore()
    memory_factory = DefaultEvaluationMemoryFactory(manifest, store=store)

    def environment_factory(block, condition, attempt):
        value = _LessonEnvironment([])
        value.environment_id = block.environment_id
        value.scenario_id = block.scenario_id
        return value

    report = _run(
        EvaluationRuntime(
            manifest,
            environment_factory=environment_factory,
            model_factory=lambda *_: _LessonModel([], []),
            memory_factory=memory_factory,
            binding_factory=memory_factory.freeze_binding,
        )
    )

    assert report.coverage_complete
    assert all(item.status == "completed" for item in report.retained_attempts)
    declaration_namespace = memory_factory._namespace("A0", "training") + ":lessons:declarations"
    assert asyncio.run(store.list(namespace=declaration_namespace))


def test_provider_telemetry_keeps_missing_fields_and_currencies_unknown() -> None:
    telemetry_model = SimpleNamespace(
        samples=[
            {
                "input_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 5,
                "request_count": 1,
                "usage_reported_requests": 1,
                "cost_minor": 1,
                "cost_currency": "USD",
            },
            {
                "input_tokens": None,
                "output_tokens": 2,
                "total_tokens": None,
                "request_count": 1,
                "usage_reported_requests": 1,
                "cost_minor": 2,
                "cost_currency": "USD",
            },
        ]
    )

    telemetry = _provider_telemetry(telemetry_model, None)

    assert telemetry.status == "available"
    assert telemetry.input_tokens is None
    assert telemetry.output_tokens == 4
    assert telemetry.total_tokens is None
    assert telemetry.cost_minor == 3
    assert telemetry.cost_currency == "USD"

    telemetry_model.samples[1]["cost_currency"] = "EUR"
    mixed = _provider_telemetry(telemetry_model, None)
    assert mixed.cost_minor is None
    assert mixed.cost_currency is None
