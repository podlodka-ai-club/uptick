from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, SkipValidation, field_serializer, model_validator

from uptick_agent.core.memory_models import (
    ConsolidationBatch,
    EpisodeRecord,
    LearningOperationResult,
    LessonGateResult,
    LessonProposal,
    MemoryCommit,
    MemoryView,
    RetrievalDiagnostics,
)
from uptick_agent.core.models import (
    CapabilityCall,
    CapabilityCatalog,
    FailureRecord,
    JsonObject,
    Observation,
    PolicyResult,
    RunMetrics,
    RunResult,
    SGRDecision,
    StrictModel,
)

RunEventKindV5 = Literal[
    "run_started",
    "decision_trace",
    "episode_closed",
    "episode_committed",
    "run_failed",
    "run_finished",
]
LearningEventKindV5 = Literal[
    "learning_started",
    "lesson_evaluated",
    "lesson_activated",
    "learning_failed",
    "learning_finished",
]
TraceEventKindV5 = RunEventKindV5 | LearningEventKindV5
FailureStage = Literal[
    "bootstrap",
    "memory_view",
    "memory_recall",
    "capabilities",
    "memory_index",
    "reasoner",
    "policy",
    "environment_execute",
    "environment_reduce",
    "episode_write",
    "environment_result",
    "run_store",
    "learning",
]


class RunStartedPayload(StrictModel):
    run_id: str = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    environment_profile_version: str = Field(min_length=1)
    memory_view: MemoryView | None = None
    initial_state: JsonObject
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DecisionTracePayload(StrictModel):
    run_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    context_projection: JsonObject
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capabilities: CapabilityCatalog
    decision: SGRDecision | None = None
    policy_result: PolicyResult | None = None
    action: CapabilityCall | None = None
    observation: Observation | None = None
    retrieval_diagnostics: RetrievalDiagnostics | None = None
    model_duration_seconds: float = Field(default=0, ge=0)
    environment_duration_seconds: float = Field(default=0, ge=0)
    failure_stage: FailureStage | None = None
    failure: FailureRecord | None = None


EpisodeWriteDisposition = Literal[
    "pending",
    "skipped_read_only",
    "commit_requested",
]


class EpisodeClosedPayload(StrictModel):
    run_id: str = Field(min_length=1)
    episode: EpisodeRecord
    write_disposition: EpisodeWriteDisposition


class EpisodeCommittedPayload(StrictModel):
    run_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    commit: MemoryCommit


class RunFailedPayload(StrictModel):
    run_id: str = Field(min_length=1)
    failure_stage: FailureStage
    failure: FailureRecord
    last_durable_sequence: int = Field(ge=0)


class RunFinishedPayload(StrictModel):
    run_id: str = Field(min_length=1)
    result: RunResult
    result_details: SkipValidation[JsonObject] = Field(default_factory=dict)
    metrics: RunMetrics
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_serializer("result_details")
    def serialize_result_details(self, value: JsonObject) -> object:
        return value


class LearningStartedPayload(StrictModel):
    learning_operation_id: str = Field(min_length=1)
    trigger: Literal["after_run", "after_closed_episode"]
    trigger_run_id: str = Field(min_length=1)
    trigger_episode_id: str | None = Field(default=None, min_length=1)
    base_view: MemoryView
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_trigger_episode(self) -> LearningStartedPayload:
        if self.trigger == "after_closed_episode" and self.trigger_episode_id is None:
            raise ValueError("after_closed_episode requires trigger_episode_id")
        if self.trigger == "after_run" and self.trigger_episode_id is not None:
            raise ValueError("after_run must not set trigger_episode_id")
        return self


class LessonEvaluatedPayload(StrictModel):
    learning_operation_id: str = Field(min_length=1)
    batch: ConsolidationBatch
    proposal: LessonProposal | None = None
    gate_result: LessonGateResult | None = None
    failure: FailureRecord | None = None


class LessonActivatedPayload(StrictModel):
    learning_operation_id: str = Field(min_length=1)
    lesson_id: str = Field(min_length=1)
    commit: MemoryCommit


class LearningFailedPayload(StrictModel):
    learning_operation_id: str = Field(min_length=1)
    failure: FailureRecord
    last_durable_sequence: int = Field(ge=0)


class LearningFinishedPayload(StrictModel):
    learning_operation_id: str = Field(min_length=1)
    result: LearningOperationResult
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


type TracePayloadV5 = (
    RunStartedPayload
    | DecisionTracePayload
    | EpisodeClosedPayload
    | EpisodeCommittedPayload
    | RunFailedPayload
    | RunFinishedPayload
    | LearningStartedPayload
    | LessonEvaluatedPayload
    | LessonActivatedPayload
    | LearningFailedPayload
    | LearningFinishedPayload
)


TRACE_PAYLOAD_MODELS: dict[TraceEventKindV5, type[StrictModel]] = {
    "run_started": RunStartedPayload,
    "decision_trace": DecisionTracePayload,
    "episode_closed": EpisodeClosedPayload,
    "episode_committed": EpisodeCommittedPayload,
    "run_failed": RunFailedPayload,
    "run_finished": RunFinishedPayload,
    "learning_started": LearningStartedPayload,
    "lesson_evaluated": LessonEvaluatedPayload,
    "lesson_activated": LessonActivatedPayload,
    "learning_failed": LearningFailedPayload,
    "learning_finished": LearningFinishedPayload,
}


TRACE_SCHEMA_VERSION = 6


class TraceEvent(StrictModel):
    # v5 streams remain readable; writers emit v6 for enriched context/episode payloads.
    schema_version: Literal[5, 6] = TRACE_SCHEMA_VERSION
    stream_id: str = Field(min_length=1)
    stream_kind: Literal["run", "learning"]
    sequence: int = Field(ge=1)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: TraceEventKindV5
    payload: TracePayloadV5

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_run_result(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        if value.get("schema_version") != 5 or value.get("kind") != "run_finished":
            return value
        payload = value.get("payload")
        if not isinstance(payload, dict):
            return value
        result = payload.get("result")
        if not isinstance(result, dict):
            return value

        existing_details = payload.get("result_details", {})
        if not isinstance(existing_details, dict):
            raise ValueError("legacy run_finished result_details must be an object")
        normalized_payload = dict(payload)
        normalized_payload["result"] = {
            key: item for key, item in result.items() if key in RunResult.model_fields
        }
        normalized_payload["result_details"] = _merge_legacy_result_details(
            result,
            existing_details,
        )
        return {**value, "payload": normalized_payload}

    @model_validator(mode="after")
    def validate_kind_and_stream(self) -> TraceEvent:
        expected = TRACE_PAYLOAD_MODELS[self.kind]
        if not isinstance(self.payload, expected):
            raise ValueError(f"trace payload does not match kind {self.kind!r}")
        expected_stream = "run" if self.kind in _RUN_KINDS else "learning"
        if self.stream_kind != expected_stream:
            raise ValueError(f"trace kind {self.kind!r} requires {expected_stream!r} stream")
        return self


def _merge_legacy_result_details(
    result: JsonObject,
    existing: JsonObject,
    *,
    path: str = "result_details",
) -> JsonObject:
    merged = dict(result)
    for key, value in existing.items():
        if key not in merged:
            merged[key] = value
            continue
        current = merged[key]
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_legacy_result_details(
                current,
                value,
                path=f"{path}.{key}",
            )
        elif current != value:
            raise ValueError(f"legacy {path}.{key} conflicts with result")
    return merged


_RUN_KINDS: frozenset[RunEventKindV5] = frozenset(
    {
        "run_started",
        "decision_trace",
        "episode_closed",
        "episode_committed",
        "run_failed",
        "run_finished",
    }
)


def run_stream_id(run_id: str) -> str:
    return f"run:{run_id}"


def learning_stream_id(learning_operation_id: str) -> str:
    return f"learning:{learning_operation_id}"
