"""Versioned, v2-only evaluation contracts and deterministic reports.

Stage 0 remains the compatibility baseline for the original balance task.  This
module owns the different shape needed by the uptime/cost simulator: arbitrary
named conditions, explicit train/evaluation bindings, and a first-attempt-only
primary analysis that never lets a successful retry replace a failed attempt.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from statistics import fmean, variance
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from uptick_agent.memory.config import AuditConfiguration, MemoryConfiguration
from uptick_agent.redaction import redact_text
from uptick_agent.stage0 import sha256_json

EVALUATION_SCHEMA_VERSION = "1.0"
V2_OBJECTIVE_KIND = "uptime_cost"
V2_API_VERSION = "v2"
V2_METRICS = (
    "uptime_ratio",
    "slo_passed",
    "total_cost_minor",
    "steps",
    "duration_seconds",
)
V2_MEMORY_METRICS = (
    "memory_context_items",
    "memory_context_tokens",
    "memory_stored_artifacts",
    "memory_snapshot_members",
)
V2_PROVIDER_METRICS = (
    "provider_input_tokens",
    "provider_cached_input_tokens",
    "provider_output_tokens",
    "provider_total_tokens",
    "provider_time_seconds",
    "provider_cost_minor",
)
V2_STATUSES = ("requested", "running", "completed", "failed", "interrupted", "excluded")
V2_TERMINAL_STATUSES = ("completed", "failed", "interrupted", "excluded")
_HASH_PATTERN = r"^[0-9a-f]{64}$"


class EvaluationModel(BaseModel):
    """Strict major-versioned data crossing the Stage 7 artifact boundary."""

    model_config = ConfigDict(extra="forbid", validate_default=True, allow_inf_nan=False)

    schema_version: str = Field(
        default=EVALUATION_SCHEMA_VERSION,
        pattern=r"^[1-9][0-9]*\.[0-9]+$",
    )

    @field_validator("schema_version")
    @classmethod
    def _supported_major(cls, value: str) -> str:
        major, _minor = value.split(".", maxsplit=1)
        if int(major) != 1:
            raise ValueError("unsupported evaluation schema major; expected 1")
        return value


class V2EnvironmentPin(EvaluationModel):
    environment_id: str = Field(min_length=1, max_length=128)
    environment_version: str = Field(min_length=1, max_length=128)
    adapter_id: str = Field(min_length=1, max_length=128)
    adapter_version: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(min_length=1, max_length=128)
    api_contract_fingerprint: str = Field(pattern=_HASH_PATTERN)
    endpoint_fingerprint: str | None = Field(default=None, pattern=_HASH_PATTERN)
    context_identity_verified: bool = False
    environment_content_hash: str | None = Field(default=None, pattern=_HASH_PATTERN)
    scenario_content_hash: str | None = Field(default=None, pattern=_HASH_PATTERN)

    @model_validator(mode="after")
    def _validate_context_identity(self) -> V2EnvironmentPin:
        hashes_present = (
            self.environment_content_hash is not None or self.scenario_content_hash is not None
        )
        if self.context_identity_verified and (
            self.environment_content_hash is None or self.scenario_content_hash is None
        ):
            raise ValueError("verified context identity requires environment and scenario hashes")
        if not self.context_identity_verified and hashes_present:
            raise ValueError("unverified context identity must not carry content hashes")
        return self


class V2ProviderPin(EvaluationModel):
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    settings: dict[str, JsonValue]
    prompt_fingerprint: str = Field(pattern=_HASH_PATTERN)
    settings_fingerprint: str = Field(pattern=_HASH_PATTERN)
    token_estimator_id: str = Field(min_length=1, max_length=128)
    token_estimator_version: str = Field(min_length=1, max_length=64)
    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _settings_fingerprint_matches(self) -> V2ProviderPin:
        if self.settings_fingerprint != sha256_json(self.settings):
            raise ValueError("settings_fingerprint does not match resolved settings")
        return self


class V2SourcePin(EvaluationModel):
    source_revision: str = Field(
        pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$",
        description="Full 40- or 64-character git revision.",
    )
    source_tree_hash: str = Field(pattern=_HASH_PATTERN)
    dependency_lock_hash: str = Field(pattern=_HASH_PATTERN)
    runtime_fingerprint: str | None = Field(default=None, pattern=_HASH_PATTERN)
    source_dirty: bool | None = None


class V2Budget(EvaluationModel):
    max_steps: int = Field(ge=1)
    max_wall_seconds: float | None = Field(default=None, gt=0)
    max_context_items: int | None = Field(default=None, ge=0)
    max_context_tokens: int | None = Field(default=None, ge=0)


class V2FailurePolicy(EvaluationModel):
    retained_statuses: tuple[
        Literal["requested", "running", "completed", "failed", "interrupted", "excluded"], ...
    ] = V2_STATUSES
    retryable_failure_classes: tuple[Literal["transient", "interrupted"], ...] = (
        "transient",
        "interrupted",
    )
    max_attempts_per_cell: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def _validate_status_policy(self) -> V2FailurePolicy:
        if self.retained_statuses != V2_STATUSES:
            raise ValueError("retained_statuses must list every lifecycle status in order")
        if len(set(self.retryable_failure_classes)) != len(self.retryable_failure_classes):
            raise ValueError("retryable failure classes must be unique")
        return self


class V2Condition(EvaluationModel):
    condition_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    memory_configuration: MemoryConfiguration
    memory_configuration_fingerprint: str = Field(pattern=_HASH_PATTERN)

    @model_validator(mode="after")
    def _fingerprint_matches_configuration(self) -> V2Condition:
        if self.memory_configuration_fingerprint != self.memory_configuration.fingerprint:
            raise ValueError(
                "memory configuration fingerprint does not match resolved configuration"
            )
        return self


class V2RunMatrixBlock(EvaluationModel):
    block_id: str = Field(min_length=1, max_length=256)
    phase: Literal["training", "evaluation"]
    environment_id: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(min_length=1, max_length=128)
    world_seed: int
    replicate_index: int = Field(ge=0)
    conditions: tuple[str, ...] = Field(min_length=1)

    @field_validator("conditions")
    @classmethod
    def _unique_conditions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("matrix block conditions must be unique")
        return value

    @model_validator(mode="after")
    def _valid_seed(self) -> V2RunMatrixBlock:
        if self.world_seed == 0:
            raise ValueError("world seed 0 is invalid")
        return self


class V2PlannedContrast(EvaluationModel):
    """A declared directed comparison between two condition IDs."""

    baseline_condition_id: str = Field(min_length=1, max_length=64)
    candidate_condition_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _distinct_conditions(self) -> V2PlannedContrast:
        if self.baseline_condition_id == self.candidate_condition_id:
            raise ValueError("planned contrast conditions must be distinct")
        return self


class V2EvaluationProfile(EvaluationModel):
    """Immutable v2 evaluation declaration, before any run evidence exists."""

    profile_id: str = Field(min_length=1, max_length=128)
    simulator_api_version: Literal["v2"] = "v2"
    objective_kind: Literal["uptime_cost"] = "uptime_cost"
    environment: V2EnvironmentPin
    world_contexts: dict[int, V2EnvironmentPin] = Field(default_factory=dict)
    provider: V2ProviderPin
    source: V2SourcePin
    conditions: tuple[V2Condition, ...] = Field(min_length=2)
    baseline_condition_id: str = Field(min_length=1, max_length=64)
    training_seeds: tuple[int, ...] = Field(min_length=1)
    evaluation_seeds: tuple[int, ...] = Field(min_length=1)
    replicate_indices: tuple[int, ...] = Field(min_length=1)
    planned_contrasts: tuple[V2PlannedContrast, ...] = ()
    budget: V2Budget
    failure_policy: V2FailurePolicy = Field(default_factory=V2FailurePolicy)
    audit_configuration: AuditConfiguration
    canonical_metrics: tuple[str, ...] = V2_METRICS
    guardrail_metrics: tuple[str, ...] = ("slo_passed", "completion_status")
    promotion_profile_ref: str | None = Field(default=None, max_length=512)
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_profile(self) -> V2EvaluationProfile:
        condition_ids = tuple(condition.condition_id for condition in self.conditions)
        if len(set(condition_ids)) != len(condition_ids):
            raise ValueError("evaluation condition IDs must be unique")
        if self.baseline_condition_id not in condition_ids:
            raise ValueError("baseline condition must be declared")
        for contrast in self.planned_contrasts:
            if (
                contrast.baseline_condition_id not in condition_ids
                or contrast.candidate_condition_id not in condition_ids
            ):
                raise ValueError("planned contrast must use declared conditions")
        contrast_pairs = tuple(
            (item.baseline_condition_id, item.candidate_condition_id)
            for item in self.planned_contrasts
        )
        if len(set(contrast_pairs)) != len(contrast_pairs):
            raise ValueError("planned contrasts must be unique")
        if any(seed == 0 for seed in (*self.training_seeds, *self.evaluation_seeds)):
            raise ValueError("seed 0 is invalid")
        declared_seeds = set(self.training_seeds) | set(self.evaluation_seeds)
        if any(seed == 0 or seed not in declared_seeds for seed in self.world_contexts):
            raise ValueError("world context pins must use declared non-zero seeds")
        for seed, context in self.world_contexts.items():
            if context.api_contract_fingerprint != self.environment.api_contract_fingerprint:
                raise ValueError(f"world context {seed} uses another API contract")
        identities: dict[tuple[str, str], tuple[str | None, str | None]] = {}
        for context in (self.environment, *self.world_contexts.values()):
            identity = (context.environment_id, context.scenario_id)
            hashes = (context.environment_content_hash, context.scenario_content_hash)
            previous = identities.get(identity)
            if (
                previous is not None
                and all(value is not None for value in (*previous, *hashes))
                and previous != hashes
            ):
                raise ValueError("the same environment/scenario identity has conflicting hashes")
            identities[identity] = tuple(
                current if current is not None else old
                for old, current in zip(previous or (None, None), hashes, strict=True)
            )
        if set(self.training_seeds) & set(self.evaluation_seeds):
            raise ValueError("training and evaluation seeds must be disjoint")
        if len(set(self.training_seeds)) != len(self.training_seeds):
            raise ValueError("training seeds must be unique")
        if len(set(self.evaluation_seeds)) != len(self.evaluation_seeds):
            raise ValueError("evaluation seeds must be unique")
        if any(index < 0 for index in self.replicate_indices):
            raise ValueError("replicate indices must be non-negative")
        if len(set(self.replicate_indices)) != len(self.replicate_indices):
            raise ValueError("replicate indices must be unique")
        if self.canonical_metrics != V2_METRICS:
            raise ValueError("canonical_metrics must match the v2 uptime/cost whitelist")
        if set(self.guardrail_metrics) != {"slo_passed", "completion_status"}:
            raise ValueError("guardrail_metrics must contain v2 completion and SLO guardrails")
        for condition in self.conditions:
            context_budget = condition.memory_configuration.context_budget
            if (
                self.budget.max_context_items is not None
                and context_budget.total_items > self.budget.max_context_items
            ):
                raise ValueError(
                    f"condition {condition.condition_id} exceeds the declared context item budget"
                )
            if (
                self.budget.max_context_tokens is not None
                and context_budget.total_tokens > self.budget.max_context_tokens
            ):
                raise ValueError(
                    f"condition {condition.condition_id} exceeds the declared context token budget"
                )
        return self


class V2Manifest(EvaluationModel):
    """Sealed preregistration manifest; frozen bindings are separate artifacts."""

    manifest_id: str = Field(min_length=1, max_length=256)
    profile_hash: str = Field(pattern=_HASH_PATTERN)
    manifest_hash: str = Field(pattern=_HASH_PATTERN)
    created_at: datetime
    evidence_status: Literal["preregistered"] = "preregistered"
    profile: V2EvaluationProfile
    run_matrix: tuple[V2RunMatrixBlock, ...]

    @model_validator(mode="after")
    def _validate_manifest(self) -> V2Manifest:
        if self.profile_hash != profile_hash(self.profile):
            raise ValueError("profile_hash does not match the profile")
        expected_id = f"{self.profile.profile_id}-{self.profile_hash[:16]}"
        if self.manifest_id != expected_id:
            raise ValueError("manifest_id does not match profile hash")
        if self.run_matrix != build_run_matrix(self.profile):
            raise ValueError("run_matrix does not match the ordered profile matrix")
        if self.manifest_hash != manifest_hash(self):
            raise ValueError("manifest_hash does not match manifest content")
        return self


class V2SnapshotRef(EvaluationModel):
    """One immutable memory-module snapshot used as an evaluation input."""

    namespace: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(pattern=_HASH_PATTERN)


class FrozenEvaluationBinding(EvaluationModel):
    """Post-training, pre-evaluation binding to an immutable input snapshot."""

    binding_id: str = Field(min_length=1, max_length=256)
    manifest_id: str = Field(min_length=1, max_length=256)
    manifest_hash: str = Field(pattern=_HASH_PATTERN)
    condition_id: str = Field(min_length=1, max_length=64)
    environment_id: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(min_length=1, max_length=128)
    cache_namespace: str = Field(min_length=1, max_length=256)
    audit_namespace: str = Field(min_length=1, max_length=256)
    training_world_contexts: dict[int, V2EnvironmentPin] = Field(default_factory=dict)
    snapshot_refs: tuple[V2SnapshotRef, ...] = ()
    training_attempt_ids: tuple[str, ...] = Field(min_length=1)
    created_at: datetime
    binding_hash: str = Field(pattern=_HASH_PATTERN)

    @model_validator(mode="after")
    def _validate_binding(self) -> FrozenEvaluationBinding:
        namespaces = tuple(item.namespace for item in self.snapshot_refs)
        if len(set(namespaces)) != len(namespaces):
            raise ValueError("snapshot namespaces must be unique per binding")
        if len(set(self.training_attempt_ids)) != len(self.training_attempt_ids):
            raise ValueError("training attempt IDs must be unique")
        if self.binding_hash != frozen_binding_hash(self):
            raise ValueError("binding_hash does not match binding content")
        return self


class ProviderTelemetry(EvaluationModel):
    """Provider usage with explicit unavailable/partial states."""

    status: Literal["available", "partial", "unavailable"] = "unavailable"
    source: Literal["provider", "estimator", "measured", "mixed", "unavailable"] = "unavailable"
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    time_seconds: float | None = Field(default=None, ge=0)
    cost_minor: int | None = Field(default=None, ge=0)
    request_count: int | None = Field(default=None, ge=0)
    retry_count: int | None = Field(default=None, ge=0)
    usage_reported_requests: int | None = Field(default=None, ge=0)
    cost_currency: str | None = Field(default=None, min_length=3, max_length=3)

    @model_validator(mode="after")
    def _honest_availability(self) -> ProviderTelemetry:
        values = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_tokens,
            self.total_tokens,
            self.time_seconds,
            self.cost_minor,
            self.request_count,
            self.retry_count,
            self.usage_reported_requests,
            self.cost_currency,
        )
        if self.status == "unavailable":
            if any(value is not None for value in values) or self.source != "unavailable":
                raise ValueError("unavailable telemetry must not carry measurements")
        elif not any(value is not None for value in values):
            raise ValueError("available telemetry must carry at least one measurement")
        if self.source == "unavailable" and self.status != "unavailable":
            raise ValueError("measured telemetry requires a source")
        if self.cost_currency is not None and self.cost_minor is None:
            raise ValueError("cost_currency requires a measured cost")
        if self.cost_minor is not None and self.cost_currency is None:
            raise ValueError("measured cost requires a cost_currency")
        if self.input_tokens is not None and self.output_tokens is not None:
            expected = self.input_tokens + self.output_tokens
            if self.total_tokens is not None and self.total_tokens != expected:
                raise ValueError("total_tokens must equal input_tokens plus output_tokens")
        return self


class MemoryTelemetry(EvaluationModel):
    """Optional memory-size and module lifecycle measurements per attempt."""

    status: Literal["available", "unavailable"] = "unavailable"
    context_items: int | None = Field(default=None, ge=0)
    context_tokens: int | None = Field(default=None, ge=0)
    stored_artifacts: int | None = Field(default=None, ge=0)
    snapshot_members: int | None = Field(default=None, ge=0)
    module_construction_events: int | None = Field(default=None, ge=0)
    module_read_events: int | None = Field(default=None, ge=0)
    module_write_events: int | None = Field(default=None, ge=0)
    module_consolidation_events: int | None = Field(default=None, ge=0)
    module_contribution_events: int | None = Field(default=None, ge=0)
    module_ids: tuple[str, ...] = ()
    module_versions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _honest_availability(self) -> MemoryTelemetry:
        numeric = (
            self.context_items,
            self.context_tokens,
            self.stored_artifacts,
            self.snapshot_members,
            self.module_construction_events,
            self.module_read_events,
            self.module_write_events,
            self.module_consolidation_events,
            self.module_contribution_events,
        )
        if self.status == "unavailable":
            if (
                any(value is not None for value in numeric)
                or self.module_ids
                or self.module_versions
            ):
                raise ValueError("unavailable memory telemetry must not carry measurements")
        elif not any(value is not None for value in numeric):
            raise ValueError("available memory telemetry must carry a measurement")
        return self


class V2OutcomeMetrics(EvaluationModel):
    run_status: Literal["completed", "failed", "interrupted", "running"]
    uptime_ratio: float | None = Field(default=None, ge=0, le=1)
    slo_passed: bool | None = None
    total_cost_minor: int | None = Field(default=None, ge=0)
    steps: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _completed_has_v2_metrics(self) -> V2OutcomeMetrics:
        if self.run_status == "completed" and any(
            value is None for value in (self.uptime_ratio, self.slo_passed, self.total_cost_minor)
        ):
            raise ValueError("completed v2 outcomes require uptime, SLO, and cost metrics")
        return self


class V2AttemptRecord(EvaluationModel):
    """One physical attempt; startup failures may have no simulator run ID."""

    manifest_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    logical_run_id: str = Field(min_length=1, max_length=256)
    block_id: str = Field(min_length=1, max_length=256)
    phase: Literal["training", "evaluation"]
    condition_id: str = Field(min_length=1, max_length=64)
    environment_id: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(min_length=1, max_length=128)
    world_seed: int
    replicate_index: int = Field(ge=0)
    attempt_index: int = Field(default=0, ge=0)
    retry_of: str | None = Field(default=None, min_length=1, max_length=256)
    status: Literal["requested", "running", "completed", "failed", "interrupted", "excluded"]
    run_id: str | None = Field(default=None, min_length=1, max_length=256)
    failure_stage: Literal["startup", "execution", "finalization"] | None = None
    failure_class: (
        Literal["validation", "transient", "permanent", "interrupted", "excluded"] | None
    ) = None
    failure_reason: str | None = Field(default=None, max_length=2_000)
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome: V2OutcomeMetrics | None = None
    provider_telemetry: ProviderTelemetry = Field(default_factory=ProviderTelemetry)
    memory_telemetry: MemoryTelemetry = Field(default_factory=MemoryTelemetry)
    memory_namespace: str | None = Field(default=None, min_length=1, max_length=256)
    cache_namespace: str | None = Field(default=None, min_length=1, max_length=256)
    audit_namespace: str | None = Field(default=None, min_length=1, max_length=256)
    frozen_binding_id: str | None = Field(default=None, min_length=1, max_length=256)
    result_hash: str | None = Field(default=None, pattern=_HASH_PATTERN)
    trace_hash: str | None = Field(default=None, pattern=_HASH_PATTERN)

    @field_validator("failure_reason")
    @classmethod
    def _redact_failure_reason(cls, value: str | None) -> str | None:
        return redact_text(value) if value is not None else None

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> V2AttemptRecord:
        if self.attempt_index == 0 and self.retry_of is not None:
            raise ValueError("initial attempts cannot set retry_of")
        if self.attempt_index > 0 and self.retry_of is None:
            raise ValueError("retry attempts require retry_of")
        if self.status in {"failed", "interrupted"} and self.failure_class is None:
            raise ValueError("failed and interrupted attempts require failure_class")
        if self.status in {"failed", "interrupted"} and not self.failure_reason:
            raise ValueError("failed and interrupted attempts require failure_reason")
        if self.status not in {"failed", "interrupted"} and self.failure_class is not None:
            raise ValueError("only failed/interrupted attempts may set failure_class")
        if self.status == "completed" and self.outcome is None:
            raise ValueError("completed attempts require an outcome")
        if (
            self.status == "completed"
            and self.outcome is not None
            and self.outcome.run_status != "completed"
        ):
            raise ValueError("completed attempts require a completed simulator outcome")
        if self.status == "excluded" and not self.failure_reason:
            raise ValueError("excluded attempts require failure_reason")
        if self.failure_stage == "startup" and ((self.run_id is None) != (self.started_at is None)):
            raise ValueError("startup failures must pair a simulator run and start time")
        if self.failure_stage is not None and self.status not in {"failed", "interrupted"}:
            raise ValueError("failure_stage is only valid for failed/interrupted attempts")
        if self.finished_at is not None and self.finished_at < self.requested_at:
            raise ValueError("finished_at must not precede requested_at")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at must not precede started_at")
        return self


class V2Coverage(EvaluationModel):
    block_id: str
    phase: Literal["training", "evaluation"]
    condition_id: str
    world_seed: int
    replicate_index: int
    expected_attempts: int = 1
    observed_attempts: int = Field(ge=0)
    terminal_attempts: int = Field(ge=0)
    retry_attempts: int = Field(ge=0)
    observed_attempt_ids: tuple[str, ...]
    first_attempt_id: str | None = None
    first_attempt_status: str | None = None


class V2MetricDistribution(EvaluationModel):
    phase: Literal["training", "evaluation"]
    condition_id: str
    metric_id: str
    expected_cells: int = Field(ge=0)
    observed_first_cells: int = Field(ge=0)
    completed_first_cells: int = Field(ge=0)
    count: int = Field(ge=0)
    mean: float | None = None
    variance: float | None = None
    percentiles: dict[str, float] = Field(default_factory=dict)


class V2ConditionReport(EvaluationModel):
    phase: Literal["training", "evaluation"]
    condition_id: str
    expected_cells: int = Field(ge=0)
    observed_first_attempts: int = Field(ge=0)
    terminal_first_attempts: int = Field(ge=0)
    retry_attempts: int = Field(ge=0)
    completed_first_attempts: int = Field(ge=0)
    slo_passed_first_attempts: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1)
    slo_pass_rate: float = Field(ge=0, le=1)
    metric_distributions: tuple[V2MetricDistribution, ...]


class V2PairwiseReport(EvaluationModel):
    phase: Literal["training", "evaluation"]
    baseline_condition_id: str
    candidate_condition_id: str
    total_blocks: int = Field(ge=0)
    complete_blocks: int = Field(ge=0)
    incomplete_blocks: int = Field(ge=0)
    baseline_completion_rate: float = Field(ge=0, le=1)
    candidate_completion_rate: float = Field(ge=0, le=1)
    completion_rate_delta: float
    baseline_slo_pass_rate: float = Field(ge=0, le=1)
    candidate_slo_pass_rate: float = Field(ge=0, le=1)
    slo_pass_rate_delta: float
    cost_eligible_pairs: int = Field(ge=0)
    cost_ineligible_or_missing_pairs: int = Field(ge=0)
    cost_delta_values: tuple[float, ...] = ()
    cost_delta_mean: float | None = None
    cost_delta_variance: float | None = None
    cost_delta_percentiles: dict[str, float] = Field(default_factory=dict)
    cost_denominator: Literal["both_completed_and_slo_passed_first_attempts"] = (
        "both_completed_and_slo_passed_first_attempts"
    )


class V2Report(EvaluationModel):
    manifest_id: str
    manifest_hash: str = Field(pattern=_HASH_PATTERN)
    generated_at: datetime
    attempts_hash: str = Field(pattern=_HASH_PATTERN)
    total_attempts: int = Field(ge=0)
    status_counts: dict[str, int]
    retained_attempt_ids: tuple[str, ...]
    retained_attempts: tuple[V2AttemptRecord, ...]
    attempt_hashes: dict[str, str]
    frozen_bindings: tuple[FrozenEvaluationBinding, ...] = ()
    coverage: tuple[V2Coverage, ...]
    condition_reports: tuple[V2ConditionReport, ...]
    pairwise_reports: tuple[V2PairwiseReport, ...]
    coverage_complete: bool
    evidence_incompleteness_reasons: tuple[str, ...] = ()
    exploratory: Literal[True] = True

    @model_validator(mode="after")
    def _integrity(self) -> V2Report:
        if self.total_attempts != len(self.retained_attempts):
            raise ValueError("total_attempts does not match retained attempts")
        if self.retained_attempt_ids != tuple(item.attempt_id for item in self.retained_attempts):
            raise ValueError("retained_attempt_ids do not match retained attempts")
        expected_statuses = Counter({status: 0 for status in V2_STATUSES})
        expected_statuses.update(item.status for item in self.retained_attempts)
        if self.status_counts != dict(expected_statuses):
            raise ValueError("status_counts do not match retained attempts")
        if self.attempts_hash != sha256_json(
            [item.model_dump(mode="json") for item in self.retained_attempts]
        ):
            raise ValueError("attempts_hash does not match retained attempts")
        expected_hashes = {item.attempt_id: attempt_hash(item) for item in self.retained_attempts}
        if self.attempt_hashes != expected_hashes:
            raise ValueError("attempt_hashes do not match retained attempts")
        return self


def profile_hash(profile: V2EvaluationProfile | Mapping[str, object]) -> str:
    payload = profile.model_dump(mode="json") if isinstance(profile, BaseModel) else dict(profile)
    return sha256_json(payload)


def manifest_hash(manifest: V2Manifest | Mapping[str, object]) -> str:
    payload = (
        manifest.model_dump(mode="json") if isinstance(manifest, BaseModel) else dict(manifest)
    )
    payload.pop("manifest_hash", None)
    return sha256_json(payload)


def frozen_binding_hash(binding: FrozenEvaluationBinding | Mapping[str, object]) -> str:
    payload = binding.model_dump(mode="json") if isinstance(binding, BaseModel) else dict(binding)
    payload.pop("binding_hash", None)
    return sha256_json(payload)


def attempt_hash(attempt: V2AttemptRecord | Mapping[str, object]) -> str:
    payload = attempt.model_dump(mode="json") if isinstance(attempt, BaseModel) else dict(attempt)
    return sha256_json(payload)


def build_run_matrix(profile: V2EvaluationProfile) -> tuple[V2RunMatrixBlock, ...]:
    """Build the ordered paired matrix from the declared seed/replicate order."""

    blocks: list[V2RunMatrixBlock] = []
    for phase, seeds in (
        ("training", profile.training_seeds),
        ("evaluation", profile.evaluation_seeds),
    ):
        for seed in seeds:
            environment = environment_pin_for_seed(profile, seed)
            for replicate in profile.replicate_indices:
                blocks.append(
                    V2RunMatrixBlock(
                        block_id=(
                            f"{phase}:{environment.environment_id}:"
                            f"{environment.scenario_id}:{seed}:{replicate}"
                        ),
                        phase=phase,
                        environment_id=environment.environment_id,
                        scenario_id=environment.scenario_id,
                        world_seed=seed,
                        replicate_index=replicate,
                        conditions=tuple(item.condition_id for item in profile.conditions),
                    )
                )
    return tuple(blocks)


def environment_pin_for_seed(profile: V2EvaluationProfile, seed: int) -> V2EnvironmentPin:
    """Resolve the immutable world identity selected for one declared seed."""

    return profile.world_contexts.get(seed, profile.environment)


def resolved_manifest(
    profile: V2EvaluationProfile,
    *,
    created_at: datetime | None = None,
) -> V2Manifest:
    """Seal a profile before any attempt is requested."""

    owned_profile = V2EvaluationProfile.model_validate(profile.model_dump(mode="json"))
    profile_digest = profile_hash(owned_profile)
    payload = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "manifest_id": f"{owned_profile.profile_id}-{profile_digest[:16]}",
        "profile_hash": profile_digest,
        "created_at": created_at or datetime.now(UTC),
        "evidence_status": "preregistered",
        "profile": owned_profile.model_dump(mode="json"),
        "run_matrix": [item.model_dump(mode="json") for item in build_run_matrix(owned_profile)],
    }
    payload["manifest_hash"] = manifest_hash(payload)
    return V2Manifest.model_validate(payload)


def freeze_evaluation_binding(
    manifest: V2Manifest,
    *,
    condition_id: str,
    cache_namespace: str,
    audit_namespace: str,
    snapshot_refs: Iterable[V2SnapshotRef] = (),
    training_attempt_ids: Iterable[str],
    training_world_contexts: Mapping[int, V2EnvironmentPin] | None = None,
    created_at: datetime | None = None,
) -> FrozenEvaluationBinding:
    """Create a separately hashed binding after training and before evaluation."""

    if condition_id not in {item.condition_id for item in manifest.profile.conditions}:
        raise ValueError("binding condition is not in the manifest")
    refs = tuple(
        V2SnapshotRef.model_validate(item.model_dump(mode="json")) for item in snapshot_refs
    )
    attempts = tuple(training_attempt_ids)
    if not attempts:
        raise ValueError("at least one training attempt must bind evaluation input")
    contexts = {
        int(seed): V2EnvironmentPin.model_validate(pin.model_dump(mode="json"))
        for seed, pin in (training_world_contexts or {}).items()
    }
    binding_id = f"{manifest.manifest_id}-{manifest.manifest_hash[:16]}:evaluation:{condition_id}"
    payload = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "binding_id": binding_id,
        "manifest_id": manifest.manifest_id,
        "manifest_hash": manifest.manifest_hash,
        "condition_id": condition_id,
        "environment_id": manifest.profile.environment.environment_id,
        "scenario_id": manifest.profile.environment.scenario_id,
        "cache_namespace": cache_namespace,
        "audit_namespace": audit_namespace,
        "training_world_contexts": {
            str(seed): pin.model_dump(mode="json") for seed, pin in sorted(contexts.items())
        },
        "snapshot_refs": [item.model_dump(mode="json") for item in refs],
        "training_attempt_ids": list(attempts),
        "created_at": created_at or datetime.now(UTC),
    }
    payload["binding_hash"] = frozen_binding_hash(payload)
    return FrozenEvaluationBinding.model_validate(payload)


def select_first_attempts(
    attempts: Iterable[V2AttemptRecord],
) -> dict[tuple[str, str], V2AttemptRecord]:
    """Select attempt index zero; retries remain diagnostics only."""

    selected: dict[tuple[str, str], V2AttemptRecord] = {}
    for attempt in attempts:
        if attempt.attempt_index != 0:
            continue
        key = (attempt.block_id, attempt.condition_id)
        if key in selected:
            raise ValueError(f"duplicate first attempt for {key[0]} / {key[1]}")
        selected[key] = attempt
    return selected


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    result: dict[str, float] = {}
    for name, quantile in (
        ("p10", 0.10),
        ("p25", 0.25),
        ("p50", 0.50),
        ("p75", 0.75),
        ("p90", 0.90),
    ):
        position = (len(ordered) - 1) * quantile
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            result[name] = ordered[lower]
        else:
            result[name] = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return result


def _distribution(
    *,
    phase: Literal["training", "evaluation"],
    condition_id: str,
    metric_id: str,
    expected_cells: int,
    attempts: list[V2AttemptRecord],
) -> V2MetricDistribution:
    values: list[float] = []
    mixed_provider_cost_currencies = False
    if metric_id == "provider_cost_minor":
        currencies = {
            attempt.provider_telemetry.cost_currency
            for attempt in attempts
            if attempt.provider_telemetry.status != "unavailable"
            and attempt.provider_telemetry.cost_minor is not None
        }
        mixed_provider_cost_currencies = len(currencies) > 1
    for attempt in attempts:
        value: float | None
        if metric_id in V2_MEMORY_METRICS:
            if attempt.memory_telemetry.status != "available":
                continue
            raw = getattr(
                attempt.memory_telemetry,
                metric_id.removeprefix("memory_"),
                None,
            )
            value = float(raw) if raw is not None else None
        elif metric_id in V2_PROVIDER_METRICS:
            if mixed_provider_cost_currencies and metric_id == "provider_cost_minor":
                continue
            if attempt.provider_telemetry.status == "unavailable":
                continue
            raw = getattr(
                attempt.provider_telemetry,
                metric_id.removeprefix("provider_"),
                None,
            )
            value = float(raw) if raw is not None else None
        else:
            if attempt.outcome is None:
                continue
            if metric_id == "total_cost_minor" and not (
                attempt.status == "completed" and attempt.outcome.slo_passed is True
            ):
                continue
            if metric_id == "slo_passed":
                value = (
                    float(attempt.outcome.slo_passed)
                    if attempt.outcome.slo_passed is not None
                    else None
                )
            else:
                raw = getattr(attempt.outcome, metric_id, None)
                value = float(raw) if raw is not None else None
        if value is not None:
            values.append(value)
    return V2MetricDistribution(
        phase=phase,
        condition_id=condition_id,
        metric_id=metric_id,
        expected_cells=expected_cells,
        observed_first_cells=len(attempts),
        completed_first_cells=sum(item.status == "completed" for item in attempts),
        count=len(values),
        mean=fmean(values) if values else None,
        variance=variance(values) if len(values) > 1 else None,
        percentiles=_percentiles(values),
    )


def _condition_report(
    *,
    phase: Literal["training", "evaluation"],
    condition_id: str,
    expected_cells: int,
    first_attempts: list[V2AttemptRecord],
    retry_attempts: int,
) -> V2ConditionReport:
    completed = sum(item.status == "completed" for item in first_attempts)
    slo_passed = sum(
        item.status == "completed" and item.outcome is not None and item.outcome.slo_passed is True
        for item in first_attempts
    )
    return V2ConditionReport(
        phase=phase,
        condition_id=condition_id,
        expected_cells=expected_cells,
        observed_first_attempts=len(first_attempts),
        terminal_first_attempts=sum(item.status in V2_TERMINAL_STATUSES for item in first_attempts),
        retry_attempts=retry_attempts,
        completed_first_attempts=completed,
        slo_passed_first_attempts=slo_passed,
        completion_rate=completed / expected_cells if expected_cells else 0,
        slo_pass_rate=slo_passed / expected_cells if expected_cells else 0,
        metric_distributions=tuple(
            _distribution(
                phase=phase,
                condition_id=condition_id,
                metric_id=metric,
                expected_cells=expected_cells,
                attempts=first_attempts,
            )
            for metric in (*V2_METRICS, *V2_MEMORY_METRICS, *V2_PROVIDER_METRICS)
        ),
    )


def _pairwise_report(
    *,
    phase: Literal["training", "evaluation"],
    baseline_condition_id: str,
    candidate_condition_id: str,
    blocks: list[V2RunMatrixBlock],
    first: dict[tuple[str, str], V2AttemptRecord],
) -> V2PairwiseReport:
    total = len(blocks)
    complete = 0
    cost_deltas: list[float] = []
    baseline_completed = 0
    candidate_completed = 0
    baseline_slo = 0
    candidate_slo = 0
    for block in blocks:
        baseline = first.get((block.block_id, baseline_condition_id))
        candidate = first.get((block.block_id, candidate_condition_id))
        if baseline is not None and baseline.status == "completed":
            baseline_completed += 1
        if candidate is not None and candidate.status == "completed":
            candidate_completed += 1
        if (
            baseline is not None
            and baseline.status == "completed"
            and baseline.outcome is not None
            and baseline.outcome.slo_passed is True
        ):
            baseline_slo += 1
        if (
            candidate is not None
            and candidate.status == "completed"
            and candidate.outcome is not None
            and candidate.outcome.slo_passed is True
        ):
            candidate_slo += 1
        if baseline is not None and candidate is not None:
            if baseline.status == "completed" and candidate.status == "completed":
                complete += 1
            eligible = (
                baseline.status == "completed"
                and candidate.status == "completed"
                and baseline.outcome is not None
                and candidate.outcome is not None
                and baseline.outcome.slo_passed is True
                and candidate.outcome.slo_passed is True
                and baseline.outcome.total_cost_minor is not None
                and candidate.outcome.total_cost_minor is not None
            )
            if eligible:
                cost_deltas.append(
                    float(candidate.outcome.total_cost_minor - baseline.outcome.total_cost_minor)
                )
    baseline_completion_rate = baseline_completed / total if total else 0
    candidate_completion_rate = candidate_completed / total if total else 0
    baseline_slo_rate = baseline_slo / total if total else 0
    candidate_slo_rate = candidate_slo / total if total else 0
    return V2PairwiseReport(
        phase=phase,
        baseline_condition_id=baseline_condition_id,
        candidate_condition_id=candidate_condition_id,
        total_blocks=total,
        complete_blocks=complete,
        incomplete_blocks=total - complete,
        baseline_completion_rate=baseline_completion_rate,
        candidate_completion_rate=candidate_completion_rate,
        completion_rate_delta=candidate_completion_rate - baseline_completion_rate,
        baseline_slo_pass_rate=baseline_slo_rate,
        candidate_slo_pass_rate=candidate_slo_rate,
        slo_pass_rate_delta=candidate_slo_rate - baseline_slo_rate,
        cost_eligible_pairs=len(cost_deltas),
        cost_ineligible_or_missing_pairs=total - len(cost_deltas),
        cost_delta_values=tuple(cost_deltas),
        cost_delta_mean=fmean(cost_deltas) if cost_deltas else None,
        cost_delta_variance=variance(cost_deltas) if len(cost_deltas) > 1 else None,
        cost_delta_percentiles=_percentiles(cost_deltas),
    )


def _validate_attempt_bindings(
    manifest: V2Manifest,
    attempts: list[V2AttemptRecord],
    bindings: tuple[FrozenEvaluationBinding, ...],
) -> None:
    matrix = {block.block_id: block for block in manifest.run_matrix}
    if len({attempt.attempt_id for attempt in attempts}) != len(attempts):
        raise ValueError("attempt IDs must be unique")
    by_id = {attempt.attempt_id: attempt for attempt in attempts}
    cell_counts = Counter((attempt.block_id, attempt.condition_id) for attempt in attempts)
    if any(
        count > manifest.profile.failure_policy.max_attempts_per_cell
        for count in cell_counts.values()
    ):
        raise ValueError("attempt count exceeds the declared per-cell retry budget")
    for attempt in attempts:
        if attempt.manifest_id != manifest.manifest_id:
            raise ValueError(f"attempt {attempt.attempt_id} does not match manifest")
        block = matrix.get(attempt.block_id)
        if block is None:
            raise ValueError(f"attempt {attempt.attempt_id} references an unknown block")
        if (
            attempt.phase != block.phase
            or attempt.environment_id != block.environment_id
            or attempt.scenario_id != block.scenario_id
            or attempt.world_seed != block.world_seed
            or attempt.replicate_index != block.replicate_index
            or attempt.condition_id not in block.conditions
        ):
            raise ValueError(f"attempt {attempt.attempt_id} does not match its matrix cell")
        if attempt.attempt_index > 0:
            previous = by_id.get(attempt.retry_of or "")
            if previous is None or previous.attempt_index != attempt.attempt_index - 1:
                raise ValueError(f"attempt {attempt.attempt_id} has an invalid retry reference")
            if (
                previous.logical_run_id != attempt.logical_run_id
                or previous.block_id != attempt.block_id
                or previous.condition_id != attempt.condition_id
                or previous.status not in {"failed", "interrupted"}
                or previous.failure_class
                not in manifest.profile.failure_policy.retryable_failure_classes
            ):
                raise ValueError(f"attempt {attempt.attempt_id} retries an incompatible attempt")
    binding_ids = {binding.binding_id for binding in bindings}
    if len(binding_ids) != len(bindings):
        raise ValueError("frozen binding IDs must be unique")
    bound_conditions = [binding.condition_id for binding in bindings]
    if len(bound_conditions) != len(set(bound_conditions)):
        raise ValueError("one frozen binding is required per evaluation condition")
    binding_by_condition = {binding.condition_id: binding for binding in bindings}
    for attempt in attempts:
        if attempt.phase == "training" and attempt.frozen_binding_id is not None:
            raise ValueError("training attempts cannot reference an evaluation binding")
        if attempt.phase != "evaluation":
            continue
        binding = binding_by_condition.get(attempt.condition_id)
        if binding is None:
            if (
                attempt.status == "failed"
                and attempt.failure_stage == "startup"
                and attempt.frozen_binding_id is None
            ):
                continue
            raise ValueError("evaluation attempt has no condition binding")
        if attempt.frozen_binding_id != binding.binding_id and not (
            attempt.status == "failed"
            and attempt.failure_stage == "startup"
            and attempt.frozen_binding_id is None
        ):
            raise ValueError("evaluation attempt does not reference its frozen binding")
    for binding in bindings:
        if (
            binding.manifest_id != manifest.manifest_id
            or binding.manifest_hash != manifest.manifest_hash
        ):
            raise ValueError("frozen binding does not match manifest")
        if binding.condition_id not in {
            condition.condition_id for condition in manifest.profile.conditions
        }:
            raise ValueError("frozen binding condition is not declared")
        training_attempts = [by_id.get(attempt_id) for attempt_id in binding.training_attempt_ids]
        if any(source is None for source in training_attempts):
            raise ValueError("frozen binding references an unknown training attempt")
        declared_training_seeds = {source.world_seed for source in training_attempts if source}
        if set(binding.training_world_contexts) != declared_training_seeds:
            raise ValueError("frozen binding must pin every training world context exactly once")
        for attempt_id in binding.training_attempt_ids:
            source = by_id.get(attempt_id)
            if (
                source is None
                or source.phase != "training"
                or source.status not in V2_TERMINAL_STATUSES
            ):
                raise ValueError("frozen binding references a non-terminal training attempt")
            context = binding.training_world_contexts[source.world_seed]
            expected_context = environment_pin_for_seed(manifest.profile, source.world_seed)
            if context.model_dump(mode="json") != expected_context.model_dump(mode="json"):
                raise ValueError("frozen binding world context does not match the manifest")
            if (
                source.condition_id != binding.condition_id
                or source.environment_id != context.environment_id
                or source.scenario_id != context.scenario_id
                or source.attempt_index != 0
            ):
                raise ValueError("frozen binding must reference matching first training attempts")
        training_attempts = [by_id[attempt_id] for attempt_id in binding.training_attempt_ids]
        if any(
            source.finished_at is None or binding.created_at <= source.finished_at
            for source in training_attempts
        ):
            raise ValueError("frozen binding must be created after its training attempts")
        evaluation_requests = [
            attempt.requested_at
            for attempt in attempts
            if attempt.phase == "evaluation" and attempt.condition_id == binding.condition_id
        ]
        if evaluation_requests and binding.created_at >= min(evaluation_requests):
            raise ValueError("frozen binding must be created before evaluation attempts")


def aggregate_report(
    manifest: V2Manifest,
    attempts: Iterable[V2AttemptRecord],
    *,
    frozen_bindings: Iterable[FrozenEvaluationBinding] = (),
    generated_at: datetime | None = None,
) -> V2Report:
    """Retain all attempts and aggregate only first attempts for primary metrics."""

    owned_manifest = V2Manifest.model_validate(manifest.model_dump(mode="json"))
    owned_attempts = [
        V2AttemptRecord.model_validate(item.model_dump(mode="json")) for item in attempts
    ]
    owned_bindings = tuple(
        FrozenEvaluationBinding.model_validate(item.model_dump(mode="json"))
        for item in frozen_bindings
    )
    _validate_attempt_bindings(owned_manifest, owned_attempts, owned_bindings)
    first = select_first_attempts(owned_attempts)
    matrix = owned_manifest.run_matrix
    reasons: list[str] = []
    if any(
        not environment_pin_for_seed(owned_manifest.profile, seed).context_identity_verified
        for seed in (
            *owned_manifest.profile.training_seeds,
            *owned_manifest.profile.evaluation_seeds,
        )
    ):
        reasons.append("world_context_identity_unverified")
    for block in matrix:
        for condition in block.conditions:
            attempt = first.get((block.block_id, condition))
            if attempt is None:
                reasons.append(f"missing_first_attempt:{block.block_id}:{condition}")
            elif attempt.status not in V2_TERMINAL_STATUSES:
                reasons.append(f"nonterminal_first_attempt:{block.block_id}:{condition}")
    expected_eval_bindings = {
        (block.phase, condition)
        for block in matrix
        if block.phase == "evaluation"
        for condition in block.conditions
    }
    actual_eval_bindings = {("evaluation", binding.condition_id) for binding in owned_bindings}
    missing_bindings = sorted(expected_eval_bindings - actual_eval_bindings)
    reasons.extend(f"missing_frozen_binding:{condition}" for _phase, condition in missing_bindings)
    if missing_bindings:
        reasons.append("frozen_evaluation_bindings_incomplete")

    status_counts = Counter({status: 0 for status in V2_STATUSES})
    status_counts.update(item.status for item in owned_attempts)
    condition_reports: list[V2ConditionReport] = []
    pairwise_reports: list[V2PairwiseReport] = []
    planned_pairs = tuple(
        (item.baseline_condition_id, item.candidate_condition_id)
        for item in owned_manifest.profile.planned_contrasts
    )
    if not planned_pairs:
        planned_pairs = tuple(
            (
                owned_manifest.profile.baseline_condition_id,
                condition.condition_id,
            )
            for condition in owned_manifest.profile.conditions
            if condition.condition_id != owned_manifest.profile.baseline_condition_id
        )
    for phase in ("training", "evaluation"):
        phase_blocks = [block for block in matrix if block.phase == phase]
        expected_cells = len(phase_blocks)
        for condition in owned_manifest.profile.conditions:
            first_attempts = [
                first[(block.block_id, condition.condition_id)]
                for block in phase_blocks
                if (block.block_id, condition.condition_id) in first
            ]
            retry_count = sum(
                attempt.phase == phase
                and attempt.condition_id == condition.condition_id
                and attempt.attempt_index > 0
                for attempt in owned_attempts
            )
            condition_reports.append(
                _condition_report(
                    phase=phase,
                    condition_id=condition.condition_id,
                    expected_cells=expected_cells,
                    first_attempts=first_attempts,
                    retry_attempts=retry_count,
                )
            )
        for baseline_condition_id, candidate_condition_id in planned_pairs:
            pairwise_reports.append(
                _pairwise_report(
                    phase=phase,
                    baseline_condition_id=baseline_condition_id,
                    candidate_condition_id=candidate_condition_id,
                    blocks=phase_blocks,
                    first=first,
                )
            )

    coverage: list[V2Coverage] = []
    for block in matrix:
        for condition in block.conditions:
            cells = [
                attempt
                for attempt in owned_attempts
                if attempt.block_id == block.block_id and attempt.condition_id == condition
            ]
            first_attempt = first.get((block.block_id, condition))
            coverage.append(
                V2Coverage(
                    block_id=block.block_id,
                    phase=block.phase,
                    condition_id=condition,
                    world_seed=block.world_seed,
                    replicate_index=block.replicate_index,
                    observed_attempts=len(cells),
                    terminal_attempts=sum(item.status in V2_TERMINAL_STATUSES for item in cells),
                    retry_attempts=sum(item.attempt_index > 0 for item in cells),
                    observed_attempt_ids=tuple(item.attempt_id for item in cells),
                    first_attempt_id=first_attempt.attempt_id if first_attempt else None,
                    first_attempt_status=first_attempt.status if first_attempt else None,
                )
            )
    coverage_complete = not any(
        item.first_attempt_id is None or item.first_attempt_status not in V2_TERMINAL_STATUSES
        for item in coverage
    )
    if not coverage_complete:
        reasons.append("coverage_incomplete")
    # Promotion is deliberately not inferred from a report. A future promotion
    # command must verify the locked holdout and human decision record.
    return V2Report(
        manifest_id=owned_manifest.manifest_id,
        manifest_hash=owned_manifest.manifest_hash,
        generated_at=generated_at or datetime.now(UTC),
        attempts_hash=sha256_json([item.model_dump(mode="json") for item in owned_attempts]),
        total_attempts=len(owned_attempts),
        status_counts=dict(status_counts),
        retained_attempt_ids=tuple(item.attempt_id for item in owned_attempts),
        retained_attempts=tuple(owned_attempts),
        attempt_hashes={item.attempt_id: attempt_hash(item) for item in owned_attempts},
        frozen_bindings=owned_bindings,
        coverage=tuple(coverage),
        condition_reports=tuple(condition_reports),
        pairwise_reports=tuple(pairwise_reports),
        coverage_complete=coverage_complete,
        evidence_incompleteness_reasons=tuple(dict.fromkeys(reasons)),
    )


def verify_report(
    manifest: V2Manifest,
    report: V2Report,
    *,
    frozen_bindings: Iterable[FrozenEvaluationBinding] | None = None,
) -> None:
    """Recompute report integrity against the sealed manifest and source attempts."""

    if report.manifest_id != manifest.manifest_id or report.manifest_hash != manifest.manifest_hash:
        raise ValueError("report does not reference the supplied manifest")
    bindings = report.frozen_bindings if frozen_bindings is None else tuple(frozen_bindings)
    recomputed = aggregate_report(
        manifest,
        report.retained_attempts,
        frozen_bindings=bindings,
        generated_at=report.generated_at,
    )
    if recomputed.model_dump(mode="json") != report.model_dump(mode="json"):
        raise ValueError("report does not match recomputed attempts and bindings")


__all__ = [
    "EVALUATION_SCHEMA_VERSION",
    "V2_API_VERSION",
    "V2_METRICS",
    "V2_MEMORY_METRICS",
    "V2_PROVIDER_METRICS",
    "V2_OBJECTIVE_KIND",
    "EvaluationModel",
    "V2EnvironmentPin",
    "V2ProviderPin",
    "V2SourcePin",
    "V2Budget",
    "V2FailurePolicy",
    "V2Condition",
    "V2RunMatrixBlock",
    "V2PlannedContrast",
    "V2EvaluationProfile",
    "V2Manifest",
    "V2SnapshotRef",
    "FrozenEvaluationBinding",
    "ProviderTelemetry",
    "MemoryTelemetry",
    "V2OutcomeMetrics",
    "V2AttemptRecord",
    "V2Coverage",
    "V2MetricDistribution",
    "V2ConditionReport",
    "V2PairwiseReport",
    "V2Report",
    "profile_hash",
    "manifest_hash",
    "frozen_binding_hash",
    "attempt_hash",
    "build_run_matrix",
    "resolved_manifest",
    "freeze_evaluation_binding",
    "select_first_attempts",
    "aggregate_report",
    "verify_report",
]
