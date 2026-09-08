import pytest
from pydantic import ValidationError

from tests.helpers import make_bootstrap_artifact
from uptick_agent.core.models import DEFAULT_OBJECTIVE, ReasonerConfig, RunMetrics, TokenUsage
from uptick_agent.environments.discovered.session import DiscoveredRunResult
from uptick_agent.experiments import (
    AttemptTraceMetrics,
    ExperimentAttempt,
    ExperimentFailure,
    ExperimentSpec,
    FrozenSQLiteArm,
    LearningSQLiteArm,
    ReasonerRoleMetrics,
    summarize_experiment,
)


def _spec(*, seeds: list[int], repeats: int = 1) -> ExperimentSpec:
    artifact = make_bootstrap_artifact()
    return ExperimentSpec(
        experiment_id="baseline",
        environment_profile_hash=artifact.bundle.profile.profile_version,
        reasoner=ReasonerConfig(
            provider="codex",
            model="test-codex",
            effort="high",
            thread_mode="ephemeral",
            retries=1,
        ),
        seeds=seeds,
        repeats=repeats,
    )


def _result(
    seed: int,
    repeat_index: int,
    score: int,
    *,
    status: str = "completed",
) -> DiscoveredRunResult:
    value = seed + repeat_index
    completed = status == "completed"
    return DiscoveredRunResult(
        run_id=f"run-{seed}-{repeat_index}",
        status=status,
        steps=value,
        duration_seconds=value / 10,
        stop_reason="done",
        simulator_run_id=f"sim-{seed}-{repeat_index}",
        objective=DEFAULT_OBJECTIVE,
        final_state={
            "evaluation": {"score": score if completed else None},
            "costs": {"total_cost_minor": 10_000 - score},
            "availability": {
                "uptime_ratio": 1.0 if completed else None,
                "slo_passed": True if completed else None,
            },
        },
        metrics=RunMetrics(
            model_turns=1,
            capability_executions=2,
            action_counts={"inspect": 2},
            policy_rejections=repeat_index,
            simulator_calls=3,
            program_executions=1,
            program_subcalls=2,
            model_duration_seconds=0.1,
            environment_duration_seconds=0.2,
            transport_duration_seconds=0.15,
            token_usage=TokenUsage(input_tokens=value, output_tokens=1, total_tokens=value + 1),
        ),
    )


def test_experiment_spec_is_versioned_safe_and_hashes_canonical_json() -> None:
    spec = _spec(seeds=[2, 1], repeats=2)

    assert spec.schema_version == 3
    assert spec.environment == "uptickv2"
    assert spec.objective == DEFAULT_OBJECTIVE
    assert spec.max_iterations is None
    assert spec.run_keys() == [(2, 0), (2, 1), (1, 0), (1, 1)]
    assert len(spec.sha256()) == 64
    assert spec.sha256() == ExperimentSpec.model_validate_json(spec.model_dump_json()).sha256()

    unsafe = _spec(seeds=[1]).model_dump()
    unsafe["experiment_id"] = ".."
    with pytest.raises(ValidationError, match="String should match pattern"):
        ExperimentSpec.model_validate(unsafe)
    with pytest.raises(ValidationError, match="must not contain duplicates"):
        _spec(seeds=[1, 1])
    with pytest.raises(ValidationError, match="seed 0"):
        _spec(seeds=[0])

    legacy = spec.model_dump(mode="json")
    legacy["schema_version"] = 2
    with pytest.raises(ValidationError, match="Input should be 3"):
        ExperimentSpec.model_validate(legacy)


def test_memory_arms_own_reproducible_revision_and_learning_controls() -> None:
    base = _spec(seeds=[1]).model_dump(mode="json")
    frozen = ExperimentSpec.model_validate(
        {
            **base,
            "memory": {
                "mode": "frozen",
                "database_id": "memory-test",
                "revision": 7,
            },
        }
    )
    learning = ExperimentSpec.model_validate(
        {
            **base,
            "memory": {
                "mode": "learning",
                "database_id": "memory-test",
                "expected_start_revision": 7,
                "learner": base["reasoner"],
                "trigger": "after_run",
                "min_evidence_groups": 2,
            },
        }
    )

    assert isinstance(frozen.memory, FrozenSQLiteArm)
    assert frozen.memory.revision == 7
    assert isinstance(learning.memory, LearningSQLiteArm)
    assert learning.memory.learner == learning.reasoner
    assert learning.memory.min_evidence_groups == 2

    invalid = learning.model_dump(mode="json")
    invalid["memory"]["trigger"] = "after_closed_episode"
    with pytest.raises(ValidationError, match="min_evidence_groups"):
        ExperimentSpec.model_validate(invalid)


def test_repeats_are_aggregated_to_seed_medians_before_primary_distribution() -> None:
    spec = _spec(seeds=list(range(1, 11)), repeats=2)
    attempts = [
        ExperimentAttempt(
            seed=seed,
            repeat_index=repeat_index,
            run_id=f"run-{seed}-{repeat_index}",
            result=_result(
                seed,
                repeat_index,
                seed * 10 + repeat_index * 2,
            ),
        )
        for seed, repeat_index in spec.run_keys()
    ]

    report = summarize_experiment(spec, attempts)

    assert report.schema_version == 5
    assert report.planned_attempts == 20
    assert report.attempts_with_result == 20
    assert report.completed_attempts == 20
    assert report.completion_rate == 1
    assert report.complete_seeds == 10
    assert report.partial_seeds == 0
    assert not report.insufficient_data
    assert report.median_score == 56
    assert report.p10_score == 11
    assert report.median_total_cost_minor == 9_944
    assert report.p90_total_cost_minor == 9_979
    assert report.min_total_cost_minor == 9_899
    assert report.max_total_cost_minor == 9_989
    assert report.min_uptime_ratio == 1
    assert report.slo_passed_attempts == 20
    assert report.capability_executions == 40
    assert report.action_counts == {"inspect": 40}
    assert report.policy_rejections == 10
    assert report.simulator_calls == 60
    assert report.program_executions == 20
    assert report.program_subcalls == 40


def test_partial_seed_is_visible_but_excluded_from_primary_distribution() -> None:
    spec = _spec(seeds=[1, 2], repeats=2)
    attempts = [
        ExperimentAttempt(
            seed=1,
            repeat_index=0,
            run_id="run-1-0",
            result=_result(1, 0, 10, status="stopped"),
        ),
        ExperimentAttempt(
            seed=1,
            repeat_index=1,
            run_id="run-1-1",
            result=_result(1, 1, 20),
        ),
        ExperimentAttempt(
            seed=2,
            repeat_index=0,
            run_id="run-2-0",
            result=_result(2, 0, 1_000),
        ),
        ExperimentAttempt(
            seed=2,
            repeat_index=1,
            failure=ExperimentFailure(
                stage="reasoner",
                error_type="ReasonerFailure",
                message="failed",
                metrics=RunMetrics(model_turns=2, model_duration_seconds=3),
            ),
        ),
    ]

    report = summarize_experiment(spec, attempts)

    assert report.attempts_with_result == 3
    assert report.failed_attempts == 1
    assert report.completed_attempts == 2
    assert report.completion_rate == 0.5
    assert report.complete_seeds == 0
    assert report.partial_seeds == 2
    assert report.insufficient_data
    assert not report.seed_aggregates[0].data_complete
    assert report.seed_aggregates[0].median_score is None
    assert not report.seed_aggregates[1].data_complete
    assert report.seed_aggregates[1].available_scores == [1_000]
    assert report.seed_aggregates[1].median_score is None
    assert report.median_score is None
    assert report.p10_score is None
    assert report.model_turns == 5


def test_nonterminal_partial_score_and_slo_do_not_count_as_published_outcome() -> None:
    spec = _spec(seeds=[1])
    result = DiscoveredRunResult(
        run_id="run-1-0",
        status="stopped",
        steps=1,
        duration_seconds=0,
        stop_reason="step limit",
        forced=True,
        simulator_run_id="sim-1-0",
        objective=DEFAULT_OBJECTIVE,
        final_state={
            "evaluation": {"score": 75},
            "availability": {"uptime_ratio": 0.995, "slo_passed": True},
        },
    )

    report = summarize_experiment(spec, [ExperimentAttempt(seed=1, result=result)])

    assert report.attempts_with_result == 1
    assert report.scored_attempts == 0
    assert report.slo_passed_attempts == 0
    assert report.scored_attempts == sum(item.scored_attempts for item in report.seed_aggregates)
    assert report.slo_passed_attempts == sum(
        item.slo_passed_attempts for item in report.seed_aggregates
    )
    assert report.seed_aggregates[0].available_scores == []
    assert report.complete_seeds == 0
    assert report.insufficient_data


def test_report_joins_role_metrics_and_deduplicates_shared_bootstrap() -> None:
    spec = _spec(seeds=[1], repeats=2)
    attempts = [
        ExperimentAttempt(
            seed=1,
            repeat_index=index,
            run_id=f"run-1-{index}",
            result=_result(1, index, 10 + index),
            trace_metrics=AttemptTraceMetrics(
                environment_profile_version=spec.environment_profile_hash,
                recall_count=2,
                selected_memory_records=index,
                memory_commit_count=1,
                context_bytes=100,
                bootstrap=ReasonerRoleMetrics(turns=1, duration_seconds=2),
                decision=ReasonerRoleMetrics(turns=2, duration_seconds=3),
                learner=ReasonerRoleMetrics(turns=index, duration_seconds=index),
            ),
        )
        for index in range(2)
    ]

    report = summarize_experiment(spec, attempts)

    assert report.bootstrap_reasoner.turns == 1
    assert report.decision_reasoner.turns == 4
    assert report.learner_reasoner.turns == 1
    assert report.recall_count == 4
    assert report.selected_memory_records == 1
    assert report.memory_commit_count == 2
    assert report.context_bytes == 200
