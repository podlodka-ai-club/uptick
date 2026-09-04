from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from uptick_agent import cli
from uptick_agent.evaluation import (
    ProviderTelemetry,
    V2AttemptRecord,
    V2Condition,
    V2EnvironmentPin,
    V2EvaluationProfile,
    V2OutcomeMetrics,
    V2ProviderPin,
    V2SourcePin,
    resolved_manifest,
    sha256_json,
)
from uptick_agent.evaluation_presets import default_pattern_query_settings
from uptick_agent.evaluation_runtime import (
    DefaultEvaluationMemoryFactory,
    _provider_telemetry,
)
from uptick_agent.experimental_runtime import compose_experimental_runtime
from uptick_agent.llm.contracts import LlmCallTelemetry
from uptick_agent.memory.config import (
    LessonSettings,
    MemoryConfiguration,
    ModuleConfig,
    RetrievalConfig,
)
from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryContextRequest,
    MemoryValidationError,
    ObjectiveMetric,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.stores import InMemoryStructuredStore, RecordWrite
from uptick_agent.simulator.v2_environment import SimulatorV2Environment
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

HASH = "a" * 64
NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _run(awaitable):
    return asyncio.run(awaitable)


def _pin(*, verified: bool = False, scenario_id: str = "default") -> V2EnvironmentPin:
    return V2EnvironmentPin(
        environment_id="simulator",
        environment_version="2",
        adapter_id="simulator-v2",
        adapter_version="1",
        scenario_id=scenario_id,
        api_contract_fingerprint=HASH,
        context_identity_verified=verified,
        environment_content_hash=HASH if verified else None,
        scenario_content_hash=HASH if verified else None,
    )


def _profile(
    *,
    config: MemoryConfiguration | None = None,
    profile_id: str = "safety-profile",
    condition_ids: tuple[str, str] = ("A0", "A1"),
    environment: V2EnvironmentPin | None = None,
    training_seeds: tuple[int, ...] = (1,),
    evaluation_seeds: tuple[int, ...] = (2,),
) -> V2EvaluationProfile:
    config = config or MemoryConfiguration.legacy_baseline()
    settings = {"temperature": 0}
    return V2EvaluationProfile(
        profile_id=profile_id,
        environment=environment or _pin(),
        provider=V2ProviderPin(
            provider="fake",
            model="test-model",
            settings=settings,
            prompt_fingerprint=HASH,
            settings_fingerprint=sha256_json(settings),
            token_estimator_id=config.context_budget.estimator_id,
            token_estimator_version=config.context_budget.estimator_version,
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
            for condition_id in condition_ids
        ),
        baseline_condition_id=condition_ids[0],
        training_seeds=training_seeds,
        evaluation_seeds=evaluation_seeds,
        replicate_indices=(0,),
        budget={"max_steps": 2},
        audit_configuration=config.audit,
    )


def _attempt(manifest, *, run_id: str = "run:training") -> V2AttemptRecord:
    block = next(item for item in manifest.run_matrix if item.phase == "training")
    return V2AttemptRecord(
        manifest_id=manifest.manifest_id,
        attempt_id=f"attempt:{run_id}",
        logical_run_id=f"logical:{run_id}",
        block_id=block.block_id,
        phase="training",
        condition_id=block.conditions[0],
        environment_id=block.environment_id,
        scenario_id=block.scenario_id,
        world_seed=block.world_seed,
        replicate_index=block.replicate_index,
        status="completed",
        run_id=run_id,
        requested_at=NOW,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=2),
        outcome=V2OutcomeMetrics(
            run_status="completed",
            uptime_ratio=0.99,
            slo_passed=True,
            total_cost_minor=10,
            steps=1,
            duration_seconds=2,
        ),
    )


def _transition(run_id: str, *, environment_id: str = "simulator") -> ExperienceTransition:
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}",
            run_id=run_id,
            iteration=1,
            occurred_at=NOW,
            environment_id=environment_id,
            scenario_id="default",
            trust_classification="external_untrusted",
            pre_state={"healthy": False},
            observation={"action_kind": "get_overview", "ok": True},
            action={"kind": "get_overview"},
            result={"ok": True},
            before_objective_metrics=[
                ObjectiveMetric(name="uptime_ratio", value=0.8, unit="ratio")
            ],
            after_objective_metrics=[ObjectiveMetric(name="uptime_ratio", value=0.9, unit="ratio")],
            terminal=True,
        )
    )


def _outcome(run_id: str) -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        status="completed",
        finished_at=NOW + timedelta(seconds=2),
        stop_reason="completed",
    )


@dataclass
class _StartClient:
    calls: list[dict[str, object]]

    async def start(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "run_id": "sim-run-1",
            "status": "running",
            "clock": {"simulation_time": NOW.isoformat(), "remaining_seconds": 60},
            "control_panel_auth": {"username": "secret", "password": "secret"},
        }


def test_verified_v2_attribution_reaches_real_learning_freeze_and_environment_adapter() -> None:
    async def scenario() -> None:
        client = _StartClient([])
        session, latest = await SimulatorV2Environment(client).start(
            seed=1,
            agent_id="safety-test",
            agent_version="1",
            request_id="start:safety",
        )
        assert session.run_id == "sim-run-1"
        assert latest.action_kind == "start"
        assert "control_panel_auth" not in latest.data
        assert client.calls == [
            {
                "seed": 1,
                "agent_id": "safety-test",
                "agent_version": "1",
                "request_id": "start:safety",
            }
        ]

        config = MemoryConfiguration.episodic_with_lessons(
            lesson_settings=LessonSettings(
                metric_name="uptime_ratio",
                metric_unit="ratio",
                direction="maximize",
                condition_keys=("action_kind", "ok"),
            )
        )
        manifest = resolved_manifest(
            _profile(config=config, environment=_pin(verified=True)),
            created_at=NOW,
        )
        condition = manifest.profile.conditions[0]
        attempt = _attempt(manifest)
        store = InMemoryStructuredStore()
        factory = DefaultEvaluationMemoryFactory(manifest, store=store)
        memory = await factory(
            next(item for item in manifest.run_matrix if item.phase == "training"),
            condition,
            attempt,
            attempt.run_id,
            "training",
            None,
        )
        await memory.record_transition(_transition(attempt.run_id))
        await memory.finalize_run(_outcome(attempt.run_id))
        binding = await factory.freeze_binding(condition, (attempt,))

        assert binding.training_world_contexts[1].context_identity_verified
        assert binding.training_world_contexts[1].environment_content_hash == HASH
        lesson_records = await store.list(
            namespace=f"{factory._namespace(condition.condition_id, 'training')}:lessons"
        )
        assert any(record.record_type == "lesson-batch" for record in lesson_records)

    _run(scenario())


def test_foreign_training_record_is_rejected_before_factory_freezes_snapshot() -> None:
    async def scenario() -> None:
        manifest = resolved_manifest(
            _profile(config=MemoryConfiguration.episodic_only()),
            created_at=NOW,
        )
        condition = manifest.profile.conditions[0]
        attempt = _attempt(manifest)
        store = InMemoryStructuredStore()
        factory = DefaultEvaluationMemoryFactory(manifest, store=store)
        namespace = factory._namespace(condition.condition_id, "training")
        foreign = _transition("run:foreign")
        await store.append(
            RecordWrite(
                namespace=namespace,
                record_id=foreign.transition_id,
                record_type="experience-transition",
                payload=foreign.model_dump(mode="json"),
                created_at=foreign.occurred_at,
            ),
            operation="safety-fixture",
            idempotency_key="safety-foreign",
        )

        with pytest.raises(ValueError, match="outside this training split"):
            await factory.freeze_binding(condition, (attempt,))
        assert await store.get_snapshot(snapshot_id=f"{namespace}:snapshot") is None

    _run(scenario())


def test_max_length_profile_and_condition_ids_survive_manifest_and_freeze() -> None:
    condition_ids = ("A" + "x" * 63, "B" + "y" * 63)
    config = MemoryConfiguration.legacy_baseline().model_copy(update={"profile_id": "p" * 128})
    manifest = resolved_manifest(
        _profile(
            config=config,
            profile_id="p" * 128,
            condition_ids=condition_ids,
        ),
        created_at=NOW,
    )
    condition = manifest.profile.conditions[0]
    attempt = _attempt(manifest)
    factory = DefaultEvaluationMemoryFactory(manifest, store=InMemoryStructuredStore())
    binding = _run(factory.freeze_binding(condition, (attempt,)))
    assert binding.condition_id == condition_ids[0]
    assert len(binding.snapshot_refs) == 1
    assert len(manifest.profile.profile_id) == 128


def test_manifest_hash_isolation_prevents_cross_experiment_training_retrieval() -> None:
    async def scenario() -> None:
        config = MemoryConfiguration.episodic_only()
        profile = _profile(config=config, profile_id="same-profile")
        first = resolved_manifest(profile, created_at=NOW)
        second = resolved_manifest(profile, created_at=NOW + timedelta(hours=1))
        assert first.manifest_id == second.manifest_id
        assert first.manifest_hash != second.manifest_hash

        store = InMemoryStructuredStore()
        first_factory = DefaultEvaluationMemoryFactory(first, store=store)
        first_condition = first.profile.conditions[0]
        first_attempt = _attempt(first, run_id="run:first-experiment")
        first_block = next(item for item in first.run_matrix if item.phase == "training")
        first_memory = await first_factory(
            first_block,
            first_condition,
            first_attempt,
            first_attempt.run_id,
            "training",
            None,
        )
        await first_memory.record_transition(_transition(first_attempt.run_id))
        first_namespace = first_factory.memory_metadata(first_condition, first_attempt, "training")[
            "memory_namespace"
        ]

        second_factory = DefaultEvaluationMemoryFactory(second, store=store)
        second_condition = second.profile.conditions[0]
        second_attempt = _attempt(second, run_id="run:second-experiment")
        second_block = next(item for item in second.run_matrix if item.phase == "training")
        second_memory = await second_factory(
            second_block,
            second_condition,
            second_attempt,
            second_attempt.run_id,
            "training",
            None,
        )
        second_context = await second_memory.build_context(
            MemoryContextRequest(
                request_id="isolation-check",
                run_id=second_attempt.run_id,
                query="get overview",
            )
        )
        second_namespace = second_factory.memory_metadata(
            second_condition, second_attempt, "training"
        )["memory_namespace"]

        assert first_namespace != second_namespace
        assert await store.list(namespace=first_namespace)
        assert await store.list(namespace=second_namespace) == []
        assert second_context.items == []

    _run(scenario())


def test_same_sealed_manifest_replay_refuses_preexisting_training_namespace() -> None:
    async def scenario() -> None:
        manifest = resolved_manifest(
            _profile(config=MemoryConfiguration.episodic_only()),
            created_at=NOW,
        )
        store = InMemoryStructuredStore()
        first_factory = DefaultEvaluationMemoryFactory(manifest, store=store)
        await first_factory.prepare()
        condition = manifest.profile.conditions[0]
        block = next(item for item in manifest.run_matrix if item.phase == "training")
        attempt = _attempt(manifest, run_id="run:first-execution")
        memory = await first_factory(
            block,
            condition,
            attempt,
            attempt.run_id,
            "training",
            None,
        )
        await memory.record_transition(_transition(attempt.run_id))

        replay_factory = DefaultEvaluationMemoryFactory(manifest, store=store)
        with pytest.raises(ValueError, match="resume/replay is unsupported"):
            await replay_factory.prepare()

    _run(scenario())


def test_unverified_world_context_does_not_activate_learning_declaration() -> None:
    async def scenario() -> None:
        config = MemoryConfiguration(
            profile_id="unknown-world",
            profile_kind="experiment",
            compatibility_legacy=ModuleConfig(enabled=False),
            episodic=ModuleConfig(enabled=True),
            lessons=ModuleConfig(enabled=True),
            lesson_settings=LessonSettings(
                metric_name="uptime_ratio",
                metric_unit="ratio",
                direction="maximize",
                condition_keys=("action_kind", "ok"),
            ),
            world_model=ModuleConfig(enabled=True),
            world_query_settings=default_pattern_query_settings(),
        )
        manifest = resolved_manifest(_profile(config=config), created_at=NOW)
        condition = manifest.profile.conditions[0]
        block = next(item for item in manifest.run_matrix if item.phase == "training")
        attempt = _attempt(manifest)
        store = InMemoryStructuredStore()
        factory = DefaultEvaluationMemoryFactory(manifest, store=store)
        assert factory._declaration(block, attempt, "training") is None
        memory = await factory(block, condition, attempt, attempt.run_id, "training", None)
        await memory.record_transition(_transition(attempt.run_id))
        await memory.finalize_run(_outcome(attempt.run_id))
        lesson_namespace = f"{factory._namespace(condition.condition_id, 'training')}:lessons"
        assert await store.list(namespace=lesson_namespace) == []

    _run(scenario())


def test_actual_dataclass_provider_telemetry_sums_and_incomplete_usage_stays_partial() -> None:
    complete = SimpleNamespace(
        samples=[
            LlmCallTelemetry(
                elapsed_seconds=0.1,
                request_count=1,
                retry_count=0,
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                cached_tokens=1,
                cost_minor=4,
                cost_currency="USD",
                usage_reported_requests=1,
            ),
            LlmCallTelemetry(
                elapsed_seconds=0.2,
                request_count=1,
                retry_count=1,
                input_tokens=5,
                output_tokens=4,
                total_tokens=9,
                cached_tokens=2,
                cost_minor=2,
                cost_currency="USD",
                usage_reported_requests=1,
            ),
        ]
    )
    telemetry = _provider_telemetry(complete, None)
    assert telemetry == ProviderTelemetry(
        status="available",
        source="measured",
        input_tokens=8,
        cached_input_tokens=3,
        output_tokens=6,
        total_tokens=14,
        time_seconds=0.30000000000000004,
        cost_minor=6,
        cost_currency="USD",
        request_count=2,
        retry_count=1,
        usage_reported_requests=2,
    )

    incomplete = SimpleNamespace(
        samples=[
            LlmCallTelemetry(
                elapsed_seconds=0.1,
                request_count=1,
                retry_count=0,
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                cost_minor=1,
                cost_currency="USD",
                usage_reported_requests=0,
            )
        ]
    )
    partial = _provider_telemetry(incomplete, None)
    assert partial.status == "partial"
    assert partial.input_tokens is None
    assert partial.total_tokens is None
    assert partial.cost_minor is None
    assert partial.request_count == 1
    assert partial.usage_reported_requests == 0


def test_semantic_retrieval_flag_is_rejected_without_an_implementation() -> None:
    configuration = MemoryConfiguration.episodic_only().model_copy(
        update={"retrieval": RetrievalConfig(semantic=True)}
    )
    with pytest.raises(MemoryValidationError, match="semantic retrieval"):
        compose_experimental_runtime(
            configuration,
            InMemoryStructuredStore(),
            namespace="safety:semantic",
            condition_id="custom-semantic",
        )


def test_cli_rejects_source_pin_before_constructing_external_clients(tmp_path, monkeypatch) -> None:
    profile = _profile(profile_id="cli-safety")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")

    def unexpected(*args, **kwargs):
        raise AssertionError("external client construction must follow pin verification")

    monkeypatch.setattr(cli, "SimulatorV2Client", unexpected)
    monkeypatch.setattr(cli, "_v2_model_factory", unexpected)
    args = SimpleNamespace(
        profile=profile_path,
        simulator_url="http://simulator.invalid",
        openai_base_url=None,
        artifacts=tmp_path / "artifacts",
        source_root=Path(__file__).resolve().parents[1],
    )

    with pytest.raises(ValueError, match="source revision mismatch"):
        _run(cli._evaluate_v2(args))
    assert not (args.artifacts / "memory.sqlite3").exists()
