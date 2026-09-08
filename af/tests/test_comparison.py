import pytest
from pydantic import ValidationError

from tests.helpers import make_bootstrap_artifact
from uptick_agent.core.models import DEFAULT_OBJECTIVE, ReasonerConfig
from uptick_agent.environments.discovered.session import DiscoveredRunResult
from uptick_agent.experiments import (
    ExperimentAttempt,
    ExperimentFailure,
    ExperimentReport,
    ExperimentSpec,
    summarize_comparison,
    summarize_experiment,
    validate_comparison_axes,
)


def _spec(experiment_id: str, *, seeds: list[int]) -> ExperimentSpec:
    artifact = make_bootstrap_artifact()
    return ExperimentSpec(
        experiment_id=experiment_id,
        environment_profile_hash=artifact.bundle.profile.profile_version,
        reasoner=ReasonerConfig(
            provider="codex",
            model=f"model-{experiment_id}",
            effort="high",
            thread_mode="ephemeral",
            retries=1,
        ),
        seeds=seeds,
    )


def _result(
    side: str,
    seed: int,
    *,
    score: int,
    cost: int,
    slo_passed: bool = True,
) -> DiscoveredRunResult:
    return DiscoveredRunResult(
        run_id=f"{side}-{seed}",
        status="completed",
        steps=1,
        duration_seconds=0,
        stop_reason="done",
        simulator_run_id=f"sim-{side}-{seed}",
        objective=DEFAULT_OBJECTIVE,
        final_state={
            "evaluation": {"score": score},
            "costs": {"total_cost_minor": cost},
            "availability": {
                "uptime_ratio": 1.0 if slo_passed else 0.98,
                "slo_passed": slo_passed,
            },
        },
    )


def _report(
    spec: ExperimentSpec,
    values: list[tuple[int, int, bool] | None],
) -> ExperimentReport:
    attempts: list[ExperimentAttempt] = []
    for seed, value in zip(spec.seeds, values, strict=True):
        if value is None:
            attempts.append(
                ExperimentAttempt(
                    seed=seed,
                    failure=ExperimentFailure(
                        stage="reasoner", error_type="ReasonerFailure", message="failed"
                    ),
                )
            )
        else:
            score, cost, slo_passed = value
            result = _result(
                spec.experiment_id,
                seed,
                score=score,
                cost=cost,
                slo_passed=slo_passed,
            )
            attempts.append(ExperimentAttempt(seed=seed, run_id=result.run_id, result=result))
    return summarize_experiment(spec, attempts)


def test_offline_comparison_uses_slo_then_published_score_then_cost() -> None:
    baseline_spec = _spec("baseline", seeds=[1, 2, 3, 4])
    candidate_spec = baseline_spec.model_copy(
        update={
            "experiment_id": "candidate",
            "reasoner": baseline_spec.reasoner.model_copy(update={"model": "candidate"}),
        }
    )
    baseline = _report(
        baseline_spec,
        [(90, 100, True), (80, 100, True), (80, 100, True), (80, 100, True)],
    )
    candidate = _report(
        candidate_spec,
        [
            (100, 50, False),
            (90, 200, True),
            (80, 90, True),
            (80, 100, True),
        ],
    )

    report = summarize_comparison(
        comparison_id="offline",
        baseline=baseline,
        candidate=candidate,
        mode="offline_uncontrolled",
    )

    assert report.schema_version == 4
    assert [pair.winner for pair in report.pairs] == ["baseline", "candidate", "candidate", "tie"]
    assert report.candidate_wins == 2
    assert report.candidate_losses == 1
    assert report.ties == 1
    assert report.median_candidate_minus_baseline_score == 5
    assert report.median_candidate_minus_baseline_cost_minor == -5
    assert report.worst_regression is not None
    assert report.worst_regression.seed == 1
    assert report.spec_diff["reasoner.model"] == {
        "baseline": "model-baseline",
        "candidate": "candidate",
    }


def test_offline_comparison_marks_missing_seed_insufficient() -> None:
    baseline_spec = _spec("baseline", seeds=[1, 2])
    candidate_spec = baseline_spec.model_copy(
        update={
            "experiment_id": "candidate",
            "reasoner": baseline_spec.reasoner.model_copy(update={"model": "candidate"}),
        }
    )
    report = summarize_comparison(
        comparison_id="partial",
        baseline=_report(baseline_spec, [(80, 100, True), (80, 100, True)]),
        candidate=_report(candidate_spec, [None, (81, 100, True)]),
        mode="offline_uncontrolled",
    )

    assert report.insufficient_data
    assert report.complete_paired_seeds == 1
    assert report.partial_paired_seeds == 1
    assert report.pairs[0].winner is None
    assert report.pairs[0].candidate_minus_baseline_score is None


def test_comparison_requires_exactly_one_reasoner_or_memory_axis() -> None:
    baseline = _spec("baseline", seeds=[1])
    same = baseline.model_copy(update={"experiment_id": "same"})
    with pytest.raises(ValueError, match="exactly one"):
        validate_comparison_axes(baseline, same)

    frozen = ExperimentSpec.model_validate(
        {
            **baseline.model_dump(mode="json"),
            "experiment_id": "frozen",
            "memory": {"mode": "frozen", "database_id": "memory-test", "revision": 3},
        }
    )
    validate_comparison_axes(baseline, frozen)

    changed_both = frozen.model_copy(
        update={"reasoner": frozen.reasoner.model_copy(update={"model": "different"})}
    )
    with pytest.raises(ValueError, match="exactly one"):
        validate_comparison_axes(baseline, changed_both)

    report = summarize_experiment(
        baseline,
        [ExperimentAttempt(seed=1, result=_result("baseline", 1, score=80, cost=100))],
    )
    legacy = report.model_dump(mode="json")
    legacy["schema_version"] = 4
    with pytest.raises(ValidationError, match="Input should be 5"):
        ExperimentReport.model_validate(legacy)
