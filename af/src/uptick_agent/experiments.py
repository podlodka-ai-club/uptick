from __future__ import annotations

import hashlib
import json
from math import ceil
from statistics import median
from typing import Annotated, Literal, cast

from pydantic import Field, model_validator

from uptick_agent.core.memory_models import LearningTrigger
from uptick_agent.core.models import (
    DEFAULT_OBJECTIVE,
    AgentConstraints,
    JsonObject,
    JsonValue,
    ReasonerConfig,
    RunMetrics,
    StrictModel,
    TokenUsage,
)
from uptick_agent.environments.discovered.session import DiscoveredRunResult


class NoMemoryArm(StrictModel):
    mode: Literal["none"] = "none"


class FrozenSQLiteArm(StrictModel):
    mode: Literal["frozen"]
    database_id: str = Field(min_length=1)
    revision: int = Field(ge=0)


class LearningSQLiteArm(StrictModel):
    mode: Literal["learning"]
    database_id: str = Field(min_length=1)
    expected_start_revision: int = Field(ge=0)
    learner: ReasonerConfig
    trigger: LearningTrigger
    min_evidence_groups: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_inline_groups(self) -> LearningSQLiteArm:
        if self.trigger == "after_closed_episode" and self.min_evidence_groups != 1:
            raise ValueError("after_closed_episode requires min_evidence_groups=1")
        return self


type ExperimentMemoryArm = Annotated[
    NoMemoryArm | FrozenSQLiteArm | LearningSQLiteArm,
    Field(discriminator="mode"),
]


class ExperimentSpec(StrictModel):
    schema_version: Literal[3] = 3
    experiment_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    environment: Literal["uptickv2"] = "uptickv2"
    environment_profile_hash: str = Field(pattern=r"^profile-[0-9a-f]{64}$")
    reasoner: ReasonerConfig
    memory: ExperimentMemoryArm = Field(default_factory=NoMemoryArm)
    seeds: list[int] = Field(min_length=1)
    repeats: int = Field(default=1, ge=1)
    max_iterations: int | None = Field(default=None, ge=1)
    agent_id: str = Field(default="af-sgr", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    agent_version: str = Field(
        default="af-sgr-v2-0.1", pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
    )
    objective: str = DEFAULT_OBJECTIVE
    constraints: AgentConstraints = Field(default_factory=AgentConstraints)

    @model_validator(mode="after")
    def validate_experiment(self) -> ExperimentSpec:
        if self.experiment_id in {".", ".."}:
            raise ValueError("experiment_id must be a safe path component")
        if 0 in self.seeds:
            raise ValueError("simulator seed 0 is invalid")
        if len(set(self.seeds)) != len(self.seeds):
            raise ValueError("experiment seeds must not contain duplicates")
        if self.constraints.forbidden_capabilities:
            raise ValueError("ExperimentSpec v3 requires empty AgentConstraints")
        return self

    def run_keys(self) -> list[tuple[int, int]]:
        return [(seed, repeat_index) for seed in self.seeds for repeat_index in range(self.repeats)]

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def compatibility_projection(self) -> JsonObject:
        return cast(
            JsonObject,
            self.model_dump(mode="json", exclude={"experiment_id", "reasoner", "memory"}),
        )


class ReasonerRoleMetrics(StrictModel):
    turns: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0, ge=0)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


class AttemptTraceMetrics(StrictModel):
    environment_profile_version: str | None = None
    memory_database_id: str | None = None
    memory_start_revision: int | None = Field(default=None, ge=0)
    memory_end_revision: int | None = Field(default=None, ge=0)
    recall_count: int = Field(default=0, ge=0)
    selected_memory_records: int = Field(default=0, ge=0)
    memory_commit_count: int = Field(default=0, ge=0)
    memory_failure_count: int = Field(default=0, ge=0)
    context_bytes: int = Field(default=0, ge=0)
    bootstrap: ReasonerRoleMetrics = Field(default_factory=ReasonerRoleMetrics)
    decision: ReasonerRoleMetrics = Field(default_factory=ReasonerRoleMetrics)
    learner: ReasonerRoleMetrics = Field(default_factory=ReasonerRoleMetrics)


class ExperimentFailure(StrictModel):
    stage: str
    error_type: str
    message: str
    metrics: RunMetrics = Field(default_factory=RunMetrics)


class ExperimentAttempt(StrictModel):
    seed: int
    repeat_index: int = Field(default=0, ge=0)
    run_id: str | None = None
    result: DiscoveredRunResult | None = None
    failure: ExperimentFailure | None = None
    trace_metrics: AttemptTraceMetrics = Field(default_factory=AttemptTraceMetrics)

    @model_validator(mode="after")
    def validate_outcome(self) -> ExperimentAttempt:
        if (self.result is None) == (self.failure is None):
            raise ValueError("an experiment attempt needs exactly one result or failure")
        return self

    @property
    def run_key(self) -> tuple[int, int]:
        return self.seed, self.repeat_index


class SeedAggregate(StrictModel):
    seed: int
    planned_attempts: int = Field(ge=1)
    attempts_with_result: int = Field(ge=0)
    completed_attempts: int = Field(ge=0)
    scored_attempts: int = Field(ge=0)
    slo_passed_attempts: int = Field(ge=0)
    available_scores: list[int] = Field(default_factory=list)
    available_total_costs_minor: list[int] = Field(default_factory=list)
    available_uptime_ratios: list[float] = Field(default_factory=list)
    data_complete: bool
    median_score: float | None = None
    median_total_cost_minor: float | None = None
    min_uptime_ratio: float | None = None


class PublishedOutcome(StrictModel):
    score: int | None = Field(default=None, ge=0)
    total_cost_minor: int | None = Field(default=None, ge=0)
    uptime_ratio: float | None = Field(default=None, ge=0, le=1)
    slo_passed: bool | None = None


class ExperimentReport(StrictModel):
    schema_version: Literal[5] = 5
    spec: ExperimentSpec
    resolved_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_profile_version: str
    attempts: list[ExperimentAttempt]
    seed_aggregates: list[SeedAggregate]
    planned_attempts: int = Field(ge=1)
    attempts_with_result: int = Field(ge=0)
    failed_attempts: int = Field(ge=0)
    completed_attempts: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1)
    complete_seeds: int = Field(ge=0)
    partial_seeds: int = Field(ge=0)
    insufficient_data: bool
    scored_attempts: int = Field(ge=0)
    slo_passed_attempts: int = Field(ge=0)
    median_score: float | None = None
    p10_score: float | None = None
    median_total_cost_minor: float | None = None
    p90_total_cost_minor: float | None = None
    min_total_cost_minor: float | None = None
    max_total_cost_minor: float | None = None
    min_uptime_ratio: float | None = Field(default=None, ge=0, le=1)
    total_duration_seconds: float = Field(default=0, ge=0)
    model_turns: int = 0
    capability_executions: int = 0
    policy_rejections: int = 0
    action_counts: dict[str, int] = Field(default_factory=dict)
    simulator_calls: int = 0
    program_executions: int = 0
    program_subcalls: int = 0
    model_duration_seconds: float = 0
    environment_duration_seconds: float = 0
    transport_duration_seconds: float = 0
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    memory_database_id: str | None = None
    memory_start_revision: int | None = Field(default=None, ge=0)
    memory_end_revision: int | None = Field(default=None, ge=0)
    recall_count: int = Field(default=0, ge=0)
    selected_memory_records: int = Field(default=0, ge=0)
    memory_commit_count: int = Field(default=0, ge=0)
    memory_failure_count: int = Field(default=0, ge=0)
    context_bytes: int = Field(default=0, ge=0)
    bootstrap_reasoner: ReasonerRoleMetrics = Field(default_factory=ReasonerRoleMetrics)
    decision_reasoner: ReasonerRoleMetrics = Field(default_factory=ReasonerRoleMetrics)
    learner_reasoner: ReasonerRoleMetrics = Field(default_factory=ReasonerRoleMetrics)

    @property
    def has_failures(self) -> bool:
        return self.failed_attempts > 0


class ComparisonPair(StrictModel):
    seed: int
    data_complete: bool
    baseline_slo_pass_rate: float | None = Field(default=None, ge=0, le=1)
    candidate_slo_pass_rate: float | None = Field(default=None, ge=0, le=1)
    baseline_median_score: float | None = None
    candidate_median_score: float | None = None
    candidate_minus_baseline_score: float | None = None
    baseline_median_total_cost_minor: float | None = None
    candidate_median_total_cost_minor: float | None = None
    candidate_minus_baseline_cost_minor: float | None = None
    winner: Literal["baseline", "candidate", "tie"] | None = None


class WorstRegression(StrictModel):
    seed: int
    baseline_slo_pass_rate: float = Field(ge=0, le=1)
    candidate_slo_pass_rate: float = Field(ge=0, le=1)
    candidate_minus_baseline_score: float
    candidate_minus_baseline_cost_minor: float


ComparisonMode = Literal["offline_uncontrolled"]


class ComparisonReport(StrictModel):
    schema_version: Literal[4] = 4
    comparison_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    comparison_mode: ComparisonMode
    baseline: ExperimentReport
    candidate: ExperimentReport
    spec_diff: JsonObject = Field(default_factory=dict)
    pairs: list[ComparisonPair]
    planned_paired_attempts: int = Field(ge=1)
    complete_paired_seeds: int = Field(ge=0)
    partial_paired_seeds: int = Field(ge=0)
    candidate_wins: int = Field(ge=0)
    candidate_losses: int = Field(ge=0)
    ties: int = Field(ge=0)
    median_candidate_minus_baseline_score: float | None = None
    median_candidate_minus_baseline_cost_minor: float | None = None
    worst_regression: WorstRegression | None = None
    insufficient_data: bool

    @property
    def has_failures(self) -> bool:
        return self.baseline.has_failures or self.candidate.has_failures or self.insufficient_data


def summarize_experiment(
    spec: ExperimentSpec,
    attempts: list[ExperimentAttempt],
) -> ExperimentReport:
    expected_keys = spec.run_keys()
    actual_keys = [attempt.run_key for attempt in attempts]
    if actual_keys != expected_keys:
        raise ValueError("experiment attempts must match ordered (seed, repeat_index) run keys")

    by_seed: dict[int, list[ExperimentAttempt]] = {seed: [] for seed in spec.seeds}
    for attempt in attempts:
        by_seed[attempt.seed].append(attempt)

    seed_aggregates: list[SeedAggregate] = []
    complete_scores: list[float] = []
    complete_costs: list[float] = []
    complete_uptime_ratios: list[float] = []
    for seed in spec.seeds:
        seed_attempts = by_seed[seed]
        results = [attempt.result for attempt in seed_attempts if attempt.result is not None]
        outcomes = [_published_outcome(result) for result in results]
        terminal = [
            outcome
            for result, outcome in zip(results, outcomes, strict=True)
            if result.status == "completed"
            and outcome.score is not None
            and outcome.total_cost_minor is not None
            and outcome.uptime_ratio is not None
            and outcome.slo_passed is not None
        ]
        data_complete = len(terminal) == spec.repeats
        scores = [cast(int, outcome.score) for outcome in terminal]
        costs = [cast(int, outcome.total_cost_minor) for outcome in terminal]
        uptime_ratios = [cast(float, outcome.uptime_ratio) for outcome in terminal]
        seed_median_score = float(median(scores)) if data_complete else None
        seed_median_cost = float(median(costs)) if data_complete else None
        seed_min_uptime = min(uptime_ratios) if data_complete else None
        if seed_median_score is not None:
            complete_scores.append(seed_median_score)
            complete_costs.append(cast(float, seed_median_cost))
            complete_uptime_ratios.append(cast(float, seed_min_uptime))
        seed_aggregates.append(
            SeedAggregate(
                seed=seed,
                planned_attempts=spec.repeats,
                attempts_with_result=len(results),
                completed_attempts=sum(result.status == "completed" for result in results),
                scored_attempts=len(terminal),
                slo_passed_attempts=sum(outcome.slo_passed is True for outcome in terminal),
                available_scores=scores,
                available_total_costs_minor=costs,
                available_uptime_ratios=uptime_ratios,
                data_complete=data_complete,
                median_score=seed_median_score,
                median_total_cost_minor=seed_median_cost,
                min_uptime_ratio=seed_min_uptime,
            )
        )

    results = [attempt.result for attempt in attempts if attempt.result is not None]
    completed = sum(result.status == "completed" for result in results)
    aggregate_metrics = _sum_metrics([_attempt_metrics(attempt) for attempt in attempts])
    trace_metrics = _sum_trace_metrics([attempt.trace_metrics for attempt in attempts])
    sorted_scores = sorted(complete_scores)
    sorted_costs = sorted(complete_costs)
    complete_seeds = len(sorted_scores)
    planned_attempts = len(expected_keys)

    return ExperimentReport(
        spec=spec,
        resolved_spec_sha256=spec.sha256(),
        environment_profile_version=spec.environment_profile_hash,
        attempts=attempts,
        seed_aggregates=seed_aggregates,
        planned_attempts=planned_attempts,
        attempts_with_result=len(results),
        failed_attempts=planned_attempts - len(results),
        completed_attempts=completed,
        completion_rate=completed / planned_attempts,
        complete_seeds=complete_seeds,
        partial_seeds=len(spec.seeds) - complete_seeds,
        insufficient_data=complete_seeds != len(spec.seeds),
        scored_attempts=sum(item.scored_attempts for item in seed_aggregates),
        slo_passed_attempts=sum(item.slo_passed_attempts for item in seed_aggregates),
        median_score=(float(median(sorted_scores)) if sorted_scores else None),
        p10_score=_nearest_rank(sorted_scores, 0.1),
        median_total_cost_minor=(float(median(sorted_costs)) if sorted_costs else None),
        p90_total_cost_minor=_nearest_rank(sorted_costs, 0.9),
        min_total_cost_minor=(min(sorted_costs) if sorted_costs else None),
        max_total_cost_minor=(max(sorted_costs) if sorted_costs else None),
        min_uptime_ratio=(min(complete_uptime_ratios) if complete_uptime_ratios else None),
        total_duration_seconds=sum(result.duration_seconds for result in results),
        model_turns=aggregate_metrics.model_turns,
        capability_executions=aggregate_metrics.capability_executions,
        policy_rejections=aggregate_metrics.policy_rejections,
        action_counts=aggregate_metrics.action_counts,
        simulator_calls=aggregate_metrics.simulator_calls,
        program_executions=aggregate_metrics.program_executions,
        program_subcalls=aggregate_metrics.program_subcalls,
        model_duration_seconds=aggregate_metrics.model_duration_seconds,
        environment_duration_seconds=aggregate_metrics.environment_duration_seconds,
        transport_duration_seconds=aggregate_metrics.transport_duration_seconds,
        token_usage=aggregate_metrics.token_usage,
        memory_database_id=_single_optional(
            [attempt.trace_metrics.memory_database_id for attempt in attempts]
        ),
        memory_start_revision=_first_optional(
            [attempt.trace_metrics.memory_start_revision for attempt in attempts]
        ),
        memory_end_revision=_last_optional(
            [attempt.trace_metrics.memory_end_revision for attempt in attempts]
        ),
        recall_count=trace_metrics.recall_count,
        selected_memory_records=trace_metrics.selected_memory_records,
        memory_commit_count=trace_metrics.memory_commit_count,
        memory_failure_count=trace_metrics.memory_failure_count,
        context_bytes=trace_metrics.context_bytes,
        bootstrap_reasoner=trace_metrics.bootstrap,
        decision_reasoner=trace_metrics.decision,
        learner_reasoner=trace_metrics.learner,
    )


def summarize_comparison(
    *,
    comparison_id: str,
    baseline: ExperimentReport,
    candidate: ExperimentReport,
    mode: ComparisonMode,
) -> ComparisonReport:
    _validate_report_compatibility(baseline, candidate)
    baseline_by_seed = {item.seed: item for item in baseline.seed_aggregates}
    candidate_by_seed = {item.seed: item for item in candidate.seed_aggregates}

    pairs: list[ComparisonPair] = []
    score_deltas: list[float] = []
    cost_deltas: list[float] = []
    regressions: list[tuple[tuple[float, float, float], WorstRegression]] = []
    candidate_wins = candidate_losses = ties = 0
    for seed in baseline.spec.seeds:
        baseline_seed = baseline_by_seed[seed]
        candidate_seed = candidate_by_seed[seed]
        data_complete = baseline_seed.data_complete and candidate_seed.data_complete
        score_delta = None
        cost_delta = None
        winner: Literal["baseline", "candidate", "tie"] | None = None
        baseline_slo_pass_rate = None
        candidate_slo_pass_rate = None
        if data_complete:
            baseline_score = baseline_seed.median_score
            candidate_score = candidate_seed.median_score
            baseline_cost = baseline_seed.median_total_cost_minor
            candidate_cost = candidate_seed.median_total_cost_minor
            if None in {baseline_score, candidate_score, baseline_cost, candidate_cost}:
                raise AssertionError("data-complete seed must have score and cost medians")
            baseline_slo_pass_rate = baseline_seed.slo_passed_attempts / baseline.spec.repeats
            candidate_slo_pass_rate = candidate_seed.slo_passed_attempts / candidate.spec.repeats
            score_delta = cast(float, candidate_score) - cast(float, baseline_score)
            cost_delta = cast(float, candidate_cost) - cast(float, baseline_cost)
            score_deltas.append(score_delta)
            cost_deltas.append(cost_delta)
            baseline_key = (
                baseline_slo_pass_rate,
                cast(float, baseline_score),
                -cast(float, baseline_cost),
            )
            candidate_key = (
                candidate_slo_pass_rate,
                cast(float, candidate_score),
                -cast(float, candidate_cost),
            )
            if candidate_key > baseline_key:
                candidate_wins += 1
                winner = "candidate"
            elif candidate_key < baseline_key:
                candidate_losses += 1
                winner = "baseline"
                regression = WorstRegression(
                    seed=seed,
                    baseline_slo_pass_rate=baseline_slo_pass_rate,
                    candidate_slo_pass_rate=candidate_slo_pass_rate,
                    candidate_minus_baseline_score=score_delta,
                    candidate_minus_baseline_cost_minor=cost_delta,
                )
                regressions.append(
                    (
                        (
                            candidate_slo_pass_rate - baseline_slo_pass_rate,
                            score_delta,
                            -cost_delta,
                        ),
                        regression,
                    )
                )
            else:
                ties += 1
                winner = "tie"
        pairs.append(
            ComparisonPair(
                seed=seed,
                data_complete=data_complete,
                baseline_slo_pass_rate=baseline_slo_pass_rate,
                candidate_slo_pass_rate=candidate_slo_pass_rate,
                baseline_median_score=baseline_seed.median_score,
                candidate_median_score=candidate_seed.median_score,
                candidate_minus_baseline_score=score_delta,
                baseline_median_total_cost_minor=baseline_seed.median_total_cost_minor,
                candidate_median_total_cost_minor=candidate_seed.median_total_cost_minor,
                candidate_minus_baseline_cost_minor=cost_delta,
                winner=winner,
            )
        )

    complete = len(score_deltas)
    worst = min(regressions, key=lambda item: item[0])[1] if regressions else None
    return ComparisonReport(
        comparison_id=comparison_id,
        comparison_mode=mode,
        baseline=baseline,
        candidate=candidate,
        spec_diff=experiment_spec_diff(baseline.spec, candidate.spec),
        pairs=pairs,
        planned_paired_attempts=len(baseline.spec.run_keys()),
        complete_paired_seeds=complete,
        partial_paired_seeds=len(baseline.spec.seeds) - complete,
        candidate_wins=candidate_wins,
        candidate_losses=candidate_losses,
        ties=ties,
        median_candidate_minus_baseline_score=(
            float(median(score_deltas)) if score_deltas else None
        ),
        median_candidate_minus_baseline_cost_minor=(
            float(median(cost_deltas)) if cost_deltas else None
        ),
        worst_regression=worst,
        insufficient_data=complete != len(baseline.spec.seeds),
    )


def experiment_spec_diff(baseline: ExperimentSpec, candidate: ExperimentSpec) -> JsonObject:
    left = cast(JsonObject, baseline.model_dump(mode="json"))
    right = cast(JsonObject, candidate.model_dump(mode="json"))
    return _mapping_diff(left, right)


def _published_outcome(result: DiscoveredRunResult) -> PublishedOutcome:
    try:
        raw_costs = result.final_state.get("costs")
        raw_availability = result.final_state.get("availability")
        if result.status == "completed":
            costs = _required_object(result.final_state, "costs")
            availability = _required_object(result.final_state, "availability")
        else:
            costs = _optional_object(raw_costs, key="costs")
            availability = _optional_object(raw_availability, key="availability")
        raw_evaluation = result.final_state.get("evaluation")
        if raw_evaluation is None and result.status != "completed":
            score: JsonValue = None
        elif isinstance(raw_evaluation, dict):
            score = cast(JsonObject, raw_evaluation)["score"]
        else:
            raise ValueError("completed final_state field 'evaluation' must be an object")
        values = {
            "score": score,
            "total_cost_minor": costs.get("total_cost_minor"),
            "uptime_ratio": availability.get("uptime_ratio"),
            "slo_passed": availability.get("slo_passed"),
        }
    except KeyError as exc:
        raise ValueError(f"published final_state is missing {exc.args[0]!r}") from exc
    return PublishedOutcome.model_validate(values)


def _required_object(value: JsonObject, key: str) -> JsonObject:
    item = value[key]
    if not isinstance(item, dict):
        raise ValueError(f"published final_state field {key!r} must be an object")
    return cast(JsonObject, item)


def _optional_object(value: JsonValue | None, *, key: str) -> JsonObject:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"published final_state field {key!r} must be an object")
    return cast(JsonObject, value)


def _validate_report_compatibility(
    baseline: ExperimentReport,
    candidate: ExperimentReport,
) -> None:
    if baseline.schema_version != 5 or candidate.schema_version != 5:
        raise ValueError("offline comparison requires ExperimentReport schema v5")
    if baseline.spec.compatibility_projection() != candidate.spec.compatibility_projection():
        raise ValueError("baseline and candidate experiment controls are not compatible")
    validate_comparison_axes(baseline.spec, candidate.spec)
    if [attempt.run_key for attempt in baseline.attempts] != baseline.spec.run_keys():
        raise ValueError("baseline report does not contain the planned run keys")
    if [attempt.run_key for attempt in candidate.attempts] != candidate.spec.run_keys():
        raise ValueError("candidate report does not contain the planned run keys")


def _mapping_diff(left: JsonObject, right: JsonObject, *, prefix: str = "") -> JsonObject:
    differences: JsonObject = {}
    for key in sorted(left.keys() | right.keys()):
        path = f"{prefix}.{key}" if prefix else key
        left_value = left.get(key)
        right_value = right.get(key)
        if isinstance(left_value, dict) and isinstance(right_value, dict):
            differences.update(
                _mapping_diff(
                    cast(JsonObject, left_value),
                    cast(JsonObject, right_value),
                    prefix=path,
                )
            )
        elif left_value != right_value:
            differences[path] = cast(
                JsonValue,
                {"baseline": left_value, "candidate": right_value},
            )
    return differences


def _nearest_rank(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    return values[ceil(quantile * len(values)) - 1]


def _sum_metrics(values: list[RunMetrics]) -> RunMetrics:
    token_usage = TokenUsage()
    action_counts: dict[str, int] = {}
    for value in values:
        token_usage = token_usage.plus(value.token_usage)
        for name, count in value.action_counts.items():
            action_counts[name] = action_counts.get(name, 0) + count
    return RunMetrics(
        model_turns=sum(value.model_turns for value in values),
        capability_executions=sum(value.capability_executions for value in values),
        policy_rejections=sum(value.policy_rejections for value in values),
        context_bytes=sum(value.context_bytes for value in values),
        action_counts=action_counts,
        simulator_calls=sum(value.simulator_calls for value in values),
        program_executions=sum(value.program_executions for value in values),
        program_subcalls=sum(value.program_subcalls for value in values),
        model_duration_seconds=sum(value.model_duration_seconds for value in values),
        environment_duration_seconds=sum(value.environment_duration_seconds for value in values),
        transport_duration_seconds=sum(value.transport_duration_seconds for value in values),
        token_usage=token_usage,
    )


def _attempt_metrics(attempt: ExperimentAttempt) -> RunMetrics:
    if attempt.result is not None:
        return attempt.result.metrics
    if attempt.failure is not None:
        return attempt.failure.metrics
    raise AssertionError("validated experiment attempt has no outcome")


def validate_comparison_axes(baseline: ExperimentSpec, candidate: ExperimentSpec) -> None:
    if baseline.compatibility_projection() != candidate.compatibility_projection():
        raise ValueError("baseline and candidate experiment controls are not compatible")
    if baseline.memory.mode == "learning" or candidate.memory.mode == "learning":
        raise ValueError("paired comparison forbids learning memory arms")
    reasoner_changed = baseline.reasoner != candidate.reasoner
    memory_changed = baseline.memory != candidate.memory
    if reasoner_changed == memory_changed:
        raise ValueError("paired comparison must change exactly one Reasoner or Memory axis")


def _sum_role_metrics(values: list[ReasonerRoleMetrics]) -> ReasonerRoleMetrics:
    usage = TokenUsage()
    for value in values:
        usage = usage.plus(value.token_usage)
    return ReasonerRoleMetrics(
        turns=sum(value.turns for value in values),
        duration_seconds=sum(value.duration_seconds for value in values),
        token_usage=usage,
    )


def _sum_trace_metrics(values: list[AttemptTraceMetrics]) -> AttemptTraceMetrics:
    bootstrap_by_profile: dict[str, ReasonerRoleMetrics] = {}
    for value in values:
        if value.environment_profile_version is not None:
            bootstrap_by_profile.setdefault(value.environment_profile_version, value.bootstrap)
    return AttemptTraceMetrics(
        recall_count=sum(value.recall_count for value in values),
        selected_memory_records=sum(value.selected_memory_records for value in values),
        memory_commit_count=sum(value.memory_commit_count for value in values),
        memory_failure_count=sum(value.memory_failure_count for value in values),
        context_bytes=sum(value.context_bytes for value in values),
        bootstrap=_sum_role_metrics(list(bootstrap_by_profile.values())),
        decision=_sum_role_metrics([value.decision for value in values]),
        learner=_sum_role_metrics([value.learner for value in values]),
    )


def _single_optional(values: list[str | None]) -> str | None:
    present = {value for value in values if value is not None}
    return next(iter(present)) if len(present) == 1 else None


def _first_optional(values: list[int | None]) -> int | None:
    return next((value for value in values if value is not None), None)


def _last_optional(values: list[int | None]) -> int | None:
    return next((value for value in reversed(values) if value is not None), None)
