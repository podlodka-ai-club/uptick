from __future__ import annotations

from typing import Literal

from pydantic import Field, SkipValidation, field_serializer, model_validator

from uptick_agent.core.models import (
    CapabilityCall,
    DecisionStatement,
    FailureRecord,
    JsonObject,
    MemoryBrief,
    Observation,
    ReasoningTelemetry,
    StrictModel,
    VerificationAssessment,
)

MemoryRecordKind = Literal["episode", "lesson"]
LearningTrigger = Literal["after_run", "after_closed_episode"]


class MemoryViewRequest(StrictModel):
    environment_id: str = Field(min_length=1)
    environment_profile_version: str = Field(min_length=1)
    expected_database_id: str | None = Field(default=None, min_length=1)
    expected_revision: int | None = Field(default=None, ge=0)


class MemoryView(StrictModel):
    database_id: str | None = Field(default=None, min_length=1)
    revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_pair(self) -> MemoryView:
        if (self.database_id is None) != (self.revision is None):
            raise ValueError("memory database_id and revision must both be set or both be null")
        return self


class RecalledMemory(StrictModel):
    record_id: str = Field(min_length=1)
    kind: MemoryRecordKind
    summary: str = Field(min_length=1, max_length=1_500)
    score: float


class RetrievalDiagnostics(StrictModel):
    candidate_record_ids: list[str] = Field(default_factory=list, max_length=64)
    selected_record_ids: list[str] = Field(default_factory=list, max_length=8)
    excluded_record_ids: list[str] = Field(default_factory=list, max_length=64)
    duration_seconds: float = Field(default=0, ge=0)


class MemoryQuery(StrictModel):
    objective: str = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    environment_profile_version: str = Field(min_length=1)
    view: MemoryView
    text: str = ""
    capability_names: list[str] = Field(default_factory=list)
    signals: JsonObject = Field(default_factory=dict)
    limit: int = Field(default=8, ge=0, le=100)


class MemoryPacket(StrictModel):
    view: MemoryView
    records: list[RecalledMemory] = Field(default_factory=list, max_length=8)
    brief: MemoryBrief = Field(default_factory=MemoryBrief)
    diagnostics: RetrievalDiagnostics | None = None


class EvidenceRef(StrictModel):
    stream_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    kind: str = Field(min_length=1)


class EpisodeRecord(StrictModel):
    record_id: str = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    environment_profile_version: str = Field(min_length=1)
    evidence_group_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    step: int = Field(ge=1)
    situation_summary: str = Field(min_length=1, max_length=1_500)
    selected_action: CapabilityCall
    expected_result: list[DecisionStatement] = Field(min_length=1, max_length=4)
    observation_summary: str = Field(min_length=1, max_length=1_500)
    verification: VerificationAssessment
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=32)
    facts: list[DecisionStatement] = Field(
        default_factory=list, max_length=6, exclude_if=lambda value: not value
    )
    strategy: DecisionStatement | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def require_closed_verification(self) -> EpisodeRecord:
        if self.verification.status not in {"confirmed", "contradicted", "inconclusive"}:
            raise ValueError("EpisodeRecord requires a closed verification")
        return self


class LessonValidation(StrictModel):
    validator_version: str = Field(min_length=1)
    evidence_group_count: int = Field(ge=1)


class LessonRecord(StrictModel):
    record_id: str = Field(min_length=1)
    claim: str = Field(min_length=1, max_length=1_500)
    applies_when: list[DecisionStatement] = Field(min_length=1, max_length=8)
    exceptions: list[DecisionStatement] = Field(default_factory=list, max_length=8)
    environment_id: str = Field(min_length=1)
    environment_profile_version: str = Field(min_length=1)
    capability_names: list[str] = Field(default_factory=list)
    evidence_episode_ids: list[str] = Field(min_length=1)
    contradicting_episode_ids: list[str] = Field(default_factory=list)
    supersedes: list[str] = Field(default_factory=list)
    validation: LessonValidation


class MemoryCommit(StrictModel):
    applied: bool
    record_ids: list[str] = Field(min_length=1)
    previous_revision: int | None = Field(default=None, ge=0)
    new_revision: int | None = Field(default=None, ge=0)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_revisions(self) -> MemoryCommit:
        if (self.previous_revision is None) != (self.new_revision is None):
            raise ValueError("memory commit revisions must both be set or both be null")
        if self.applied and self.new_revision is not None:
            assert self.previous_revision is not None
            if self.new_revision <= self.previous_revision:
                raise ValueError("an applied memory commit must advance the revision")
        return self


class ConsolidationQuery(StrictModel):
    trigger: LearningTrigger
    view: MemoryView
    environment_id: str = Field(min_length=1)
    environment_profile_version: str = Field(min_length=1)
    trigger_episode_id: str | None = Field(default=None, min_length=1)
    min_evidence_groups: int = Field(ge=1)
    episode_limit: int = Field(default=32, ge=1, le=256)
    lesson_limit: int = Field(default=16, ge=0, le=128)

    @model_validator(mode="after")
    def validate_trigger_episode(self) -> ConsolidationQuery:
        if self.trigger == "after_closed_episode" and self.trigger_episode_id is None:
            raise ValueError("after_closed_episode requires trigger_episode_id")
        if self.trigger == "after_run" and self.trigger_episode_id is not None:
            raise ValueError("after_run must not set trigger_episode_id")
        if self.episode_limit < self.min_evidence_groups:
            raise ValueError("episode_limit must cover every required evidence group")
        return self


class RunOutcomeEvidence(StrictModel):
    """Legacy evidence retained for lossless reading of historical v5 learning traces."""

    run_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    steps: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    stop_reason: str
    forced: bool
    result: SkipValidation[JsonObject]
    final_observation: Observation | None = None

    @field_serializer("result")
    def serialize_result(self, value: JsonObject) -> object:
        return value


class ConsolidationBatch(StrictModel):
    query: ConsolidationQuery
    episodes: list[EpisodeRecord] = Field(default_factory=list)
    active_lessons: list[LessonRecord] = Field(default_factory=list)
    # Historical trace compatibility only; never populated or sent to the learner.
    run_outcomes: list[RunOutcomeEvidence] = Field(default_factory=list)


class LessonProposal(StrictModel):
    claim: str = Field(min_length=1, max_length=1_500)
    applies_when: list[DecisionStatement] = Field(min_length=1, max_length=8)
    exceptions: list[DecisionStatement] = Field(default_factory=list, max_length=8)
    capability_names: list[str] = Field(default_factory=list)
    evidence_episode_ids: list[str] = Field(min_length=1)
    contradicting_episode_ids: list[str] = Field(default_factory=list)
    supersedes: list[str] = Field(default_factory=list)


class NoLesson(StrictModel):
    reason: DecisionStatement


class ConsolidationOutput(StrictModel):
    proposal: LessonProposal | None
    no_lesson: NoLesson | None

    @model_validator(mode="after")
    def require_exactly_one_result(self) -> ConsolidationOutput:
        if (self.proposal is None) == (self.no_lesson is None):
            raise ValueError("exactly one of proposal and no_lesson must be provided")
        return self


class LessonGateResult(StrictModel):
    accepted: bool
    violations: list[str] = Field(default_factory=list)


class LearningOperationResult(StrictModel):
    operation_id: str = Field(min_length=1)
    trigger: LearningTrigger
    trigger_run_id: str = Field(min_length=1)
    trigger_episode_id: str | None = Field(default=None, min_length=1)
    selected_run_ids: list[str] = Field(default_factory=list, max_length=256)
    selected_evidence_group_ids: list[str] = Field(default_factory=list, max_length=256)
    start_view: MemoryView
    end_view: MemoryView
    proposal: LessonProposal | None = None
    learner_telemetry: ReasoningTelemetry | None = None
    gate_result: LessonGateResult | None = None
    commit: MemoryCommit | None = None
    failure: FailureRecord | None = None
    skipped_reason: str | None = None

    @model_validator(mode="after")
    def validate_trigger_episode(self) -> LearningOperationResult:
        if self.trigger == "after_closed_episode" and self.trigger_episode_id is None:
            raise ValueError("after_closed_episode requires trigger_episode_id")
        if self.trigger == "after_run" and self.trigger_episode_id is not None:
            raise ValueError("after_run must not set trigger_episode_id")
        if len(set(self.selected_run_ids)) != len(self.selected_run_ids):
            raise ValueError("selected run IDs must be unique")
        if len(set(self.selected_evidence_group_ids)) != len(self.selected_evidence_group_ids):
            raise ValueError("selected evidence group IDs must be unique")
        return self
