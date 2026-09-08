from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

DEFAULT_OBJECTIVE = "Fulfil the objective and completion conditions supplied by the environment."


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentConstraints(StrictModel):
    forbidden_capabilities: list[str] = Field(default_factory=list)


ReasoningEffortName = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
ThreadMode = Literal["stateless", "ephemeral"]
MemoryMode = Literal["none", "sqlite", "frozen", "learning"]
DecisionPhase = Literal["observe", "diagnose", "mitigate", "verify", "optimize", "finish"]
VerificationStatus = Literal[
    "not_applicable",
    "pending",
    "confirmed",
    "contradicted",
    "inconclusive",
]
type DecisionStatement = Annotated[str, Field(min_length=1, max_length=500)]
type MemoryStatement = Annotated[str, Field(min_length=1, max_length=1_500)]


class ReasonerConfig(StrictModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    effort: ReasoningEffortName | None = None
    thread_mode: ThreadMode
    timeout_seconds: float | None = Field(default=None, gt=0)
    retries: int = Field(default=0, ge=0)


class RunMetadata(StrictModel):
    reasoner: ReasonerConfig = Field(
        default_factory=lambda: ReasonerConfig(
            provider="unconfigured",
            model="unconfigured",
            thread_mode="stateless",
        )
    )
    memory_mode: MemoryMode | Literal["unknown"] = "unknown"
    carry_memory: bool = False
    memory_database_id: str | None = Field(default=None, min_length=1)
    memory_start_revision: int | None = Field(default=None, ge=0)
    git_revision: str | None = None
    git_dirty: bool | None = None
    experiment_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    repeat_index: int | None = Field(default=None, ge=0)
    resolved_spec_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    uv_lock_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    python_version: str | None = None
    simulator_endpoint_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    agent_config_source: str | None = None
    agent_config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    operator_guidance: str | None = None
    provenance_reasons: list[str] = Field(default_factory=list)


class EnvironmentProfileRef(StrictModel):
    environment_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class RunSpec(StrictModel):
    run_id: str = Field(pattern=r"^run-[A-Za-z0-9._-]{1,128}$")
    agent_id: str = Field(default="af-sgr", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    agent_version: str = Field(
        default="af-sgr-v2-0.1", pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
    )
    objective: str = DEFAULT_OBJECTIVE
    environment: str = "generic"
    world_id: str | None = None
    parameters: JsonObject = Field(default_factory=dict)
    constraints: AgentConstraints = Field(default_factory=AgentConstraints)
    step_limit: int | None = Field(default=None, ge=1)
    environment_profile: EnvironmentProfileRef | None = None
    metadata: RunMetadata = Field(default_factory=RunMetadata)


class Capability(StrictModel):
    name: str
    description: str
    input_schema: JsonObject
    mutates_state: bool = False
    terminal: bool = False


class CapabilityCatalog(StrictModel):
    items: list[Capability]

    def find(self, name: str) -> Capability | None:
        return next((item for item in self.items if item.name == name), None)


class CapabilityCall(StrictModel):
    name: str
    arguments: JsonObject = Field(default_factory=dict)


class Observation(StrictModel):
    action_kind: str
    ok: bool = True
    summary: str
    data: JsonObject = Field(default_factory=dict)
    terminal: bool = False


EnvironmentStatus = Literal["active", "terminal"]


class EnvironmentState(StrictModel):
    profile: EnvironmentProfileRef
    status: EnvironmentStatus
    decision_view: JsonObject = Field(default_factory=dict)
    latest_observation: Observation

    @model_validator(mode="after")
    def validate_terminal_status(self) -> EnvironmentState:
        if self.status == "terminal" and not self.latest_observation.terminal:
            raise ValueError("terminal EnvironmentState requires a terminal observation")
        return self


class VerificationAssessment(StrictModel):
    status: VerificationStatus
    evidence: list[DecisionStatement] = Field(default_factory=list, max_length=4)


class OpenDecision(StrictModel):
    step: int = Field(ge=1)
    phase: DecisionPhase
    strategy: DecisionStatement
    situation_summary: DecisionStatement
    selected_action: CapabilityCall
    expected_result: list[DecisionStatement] = Field(min_length=1, max_length=4)
    verification: list[DecisionStatement] = Field(min_length=1, max_length=4)
    facts: list[DecisionStatement] = Field(
        default_factory=list, max_length=6, exclude_if=lambda value: not value
    )


class ClosedEpisodeBridge(StrictModel):
    episode_id: str = Field(min_length=1)
    selected_action: CapabilityCall
    observation_summary: str = Field(min_length=1, max_length=1_500)
    assessment: VerificationAssessment

    @model_validator(mode="after")
    def require_closed_assessment(self) -> ClosedEpisodeBridge:
        if self.assessment.status not in {"confirmed", "contradicted", "inconclusive"}:
            raise ValueError("closed episode bridge requires a closed assessment")
        return self


class AgentWorkingState(StrictModel):
    active_strategy: DecisionStatement | None = None
    strategy_started_step: int | None = Field(default=None, ge=1)
    open_decision: OpenDecision | None = None
    last_closed_episode: ClosedEpisodeBridge | None = None

    @model_validator(mode="after")
    def validate_strategy_pair(self) -> AgentWorkingState:
        if (self.active_strategy is None) != (self.strategy_started_step is None):
            raise ValueError("active strategy and strategy_started_step must be set together")
        return self


class ContextProgress(StrictModel):
    step: int = Field(ge=0)
    step_limit: int | None = Field(default=None, ge=1)


class EnvironmentFact(StrictModel):
    category: str = Field(min_length=1, max_length=100)
    statement: str = Field(min_length=1, max_length=1_500)


class EnvironmentBrief(StrictModel):
    environment_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    guidance: list[Annotated[str, Field(min_length=1, max_length=1_500)]] = Field(
        min_length=1,
        max_length=64,
    )
    facts: list[EnvironmentFact] = Field(
        default_factory=list, max_length=128, exclude_if=lambda value: not value
    )


class RecalledLesson(StrictModel):
    claim: MemoryStatement
    applies_when: list[DecisionStatement] = Field(min_length=1, max_length=8)
    exceptions: list[DecisionStatement] = Field(default_factory=list, max_length=8)


class MemoryBrief(StrictModel):
    # Strings remain readable in historical trace contexts; new recall emits full conditions.
    lessons: list[RecalledLesson | MemoryStatement] = Field(default_factory=list, max_length=4)
    similar_episodes: list[MemoryStatement] = Field(default_factory=list, max_length=4)
    contradictions: list[MemoryStatement] = Field(default_factory=list, max_length=4)


class AgentContext(StrictModel):
    objective: str
    environment_profile: EnvironmentBrief
    environment_state: EnvironmentState
    agent_working_state: AgentWorkingState
    memory_brief: MemoryBrief
    capabilities: CapabilityCatalog
    constraints: AgentConstraints
    progress: ContextProgress | None = None


@dataclass(frozen=True, slots=True)
class ReasoningRequest:
    system_prompt: str
    user_prompt: str
    output_model: type[BaseModel]
    output_schema: JsonObject


class TokenUsage(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    cache_write_input_tokens: int | None = Field(default=None, ge=0)

    def plus(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=_sum_optional(self.input_tokens, other.input_tokens),
            output_tokens=_sum_optional(self.output_tokens, other.output_tokens),
            total_tokens=_sum_optional(self.total_tokens, other.total_tokens),
            cached_input_tokens=_sum_optional(self.cached_input_tokens, other.cached_input_tokens),
            reasoning_output_tokens=_sum_optional(
                self.reasoning_output_tokens, other.reasoning_output_tokens
            ),
            cache_write_input_tokens=_sum_optional(
                self.cache_write_input_tokens, other.cache_write_input_tokens
            ),
        )


class ReasoningTelemetry(StrictModel):
    provider: str
    requested_model: str
    reported_model: str | None = None
    requested_effort: ReasoningEffortName | None = None
    reported_effort: ReasoningEffortName | None = None
    thread_mode: ThreadMode
    attempts: int = Field(default=1, ge=0)
    duration_seconds: float = Field(ge=0)
    sdk_name: str
    sdk_version: str
    provider_runtime: JsonObject = Field(default_factory=dict)
    provider_instructions_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    raw_usage: JsonObject = Field(default_factory=dict)


class ReasonerResult(StrictModel):
    output: JsonObject
    telemetry: ReasoningTelemetry


class SGREnvelope(StrictModel):
    previous_verification: VerificationAssessment
    facts: list[DecisionStatement] = Field(min_length=1, max_length=6)
    competing_hypotheses: list[DecisionStatement] = Field(max_length=3)
    contradicting_evidence: list[DecisionStatement] = Field(default_factory=list, max_length=4)
    strategy: DecisionStatement
    phase: DecisionPhase
    selected_action: CapabilityCall
    expected_result: list[DecisionStatement] = Field(min_length=1, max_length=4)
    verification: list[DecisionStatement] = Field(min_length=1, max_length=4)
    task_completed: bool


class SGRDecision(StrictModel):
    envelope: SGREnvelope
    telemetry: ReasoningTelemetry


class DecisionInputFingerprint(StrictModel):
    system_prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PolicyResult(StrictModel):
    accepted: bool
    violations: list[str] = Field(default_factory=list)


class EnvironmentTelemetry(StrictModel):
    external_calls: int = Field(default=0, ge=0)
    transport_duration_seconds: float = Field(default=0, ge=0)
    program_executions: int = Field(default=0, ge=0)
    program_subcalls: int = Field(default=0, ge=0)


class RunMetrics(StrictModel):
    model_turns: int = Field(default=0, ge=0)
    capability_executions: int = Field(default=0, ge=0)
    policy_rejections: int = Field(default=0, ge=0)
    context_bytes: int = Field(default=0, ge=0)
    action_counts: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    simulator_calls: int = Field(default=0, ge=0)
    program_executions: int = Field(default=0, ge=0)
    program_subcalls: int = Field(default=0, ge=0)
    model_duration_seconds: float = Field(default=0, ge=0)
    environment_duration_seconds: float = Field(default=0, ge=0)
    transport_duration_seconds: float = Field(default=0, ge=0)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


class RunManifest(StrictModel):
    run_id: str
    seed: int | None = None
    agent_id: str
    agent_version: str
    reasoner: ReasonerConfig
    git_revision: str | None = None
    git_dirty: bool | None = None
    experiment_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    repeat_index: int | None = Field(default=None, ge=0)
    resolved_spec_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    uv_lock_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    python_version: str | None = None
    simulator_endpoint_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    agent_config_source: str | None = None
    agent_config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    operator_guidance: str | None = None
    system_prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_instructions_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    environment_profile_version: str | None = None
    memory_profile_version: str | None = Field(default=None, exclude_if=lambda value: value is None)
    bootstrap_cache_hit: bool | None = None
    bootstrap_reasoner: ReasoningTelemetry | None = None
    memory_mode: MemoryMode | Literal["unknown"]
    carry_memory: bool
    memory_database_id: str | None = Field(default=None, min_length=1)
    memory_start_revision: int | None = Field(default=None, ge=0)
    memory_end_revision: int | None = Field(default=None, ge=0)
    environment: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "running"
    failure_stage: str | None = None
    failure_type: str | None = None
    failure_message: str | None = None
    reproducible: bool = True
    non_reproducible_reasons: list[str] = Field(default_factory=list)
    baseline_profile: Literal["no-memory-v1"] | None = None
    metrics: RunMetrics = Field(default_factory=RunMetrics)


class FailureRecord(StrictModel):
    stage: str
    error_type: str
    message: str
    category: str | None = None
    telemetry: ReasoningTelemetry | None = None


class RunCompletion(StrictModel):
    steps: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    stop_reason: str
    forced: bool = False


class RunResult(StrictModel):
    run_id: str
    status: str
    steps: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    stop_reason: str
    forced: bool = False
    summary: str = Field(default="", exclude=True)
    metrics: RunMetrics = Field(default_factory=RunMetrics)


def _sum_optional(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)
