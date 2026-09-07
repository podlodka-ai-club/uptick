from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Route(Strict):
    world_id: str | None
    reason: str


class Artifact(Strict):
    path: str
    content: str


class Patch(Strict):
    path: str
    old: str = Field(min_length=1)
    new: str


class Adaptation(Strict):
    assessment: str
    files: list[Artifact] = Field(max_length=12)
    ready: bool
    patches: list[Patch] = Field(default_factory=list, max_length=24)
    pause_reason: str | None = Field(default=None, min_length=1, max_length=2000)


class Evidence(Strict):
    identity: str = Field(min_length=1, max_length=200)
    outcome: Literal["success", "failure"]
    kind: Literal["action", "operation", "completion", "effect"]
    detail: str | dict


class Observation(Strict):
    data: dict
    evidence: list[Evidence] = Field(default_factory=list, max_length=64)
    done: bool = False
    success: bool | None = None
    metrics: dict = Field(default_factory=dict)
    external_id: str | None = None
    pending: bool = False
    error: str | dict | None = None
    delivery: Literal["known", "unknown", "not_sent", "read_failed"] | None = None

    @model_validator(mode="after")
    def failed_read_is_only_diagnostic(self):
        if self.delivery == "read_failed" and (
                not self.error or self.pending or self.done or self.success is not None or self.evidence):
            raise ValueError("read_failed requires an error and cannot claim effects, completion or pending work")
        return self


class Lesson(Strict):
    key: str
    title: str
    body: str
    tags: list[str]
    procedure: list[str]
    importance: float = Field(ge=0, le=1)
    evidence_ids: list[int]
    verdict: Literal["supports", "contradicts"]
    outcome: Literal["success", "failure"]
    explanation: str


class LessonReview(Strict):
    sufficient: bool
    reason: str = Field(min_length=1, max_length=4000)
    limitations: list[str] = Field(max_length=8)
    evidence_ids: list[int] = Field(max_length=64)


class Action(Strict):
    payload_json: str
    replay_safe: bool
    wait_for_completion: bool
    repeat_count: int = Field(ge=1, le=64)


class Decision(Strict):
    mission: str
    assessment: str
    plan: str
    tags: list[str]
    used_memory_ids: list[str]
    lessons: list[Lesson] = Field(max_length=8)
    actions: list[Action] = Field(max_length=64)
    adaptation_request: str | None = None
    pause_reason: str | None = Field(default=None, min_length=1, max_length=2000)


class FailureReflection(Strict):
    assessment: str
    next_check: str
    used_memory_ids: list[str]
    lessons: list[Lesson] = Field(max_length=8)


class HistoryPreparation(Strict):
    assessment: str
    files: list[Artifact] = Field(min_length=1, max_length=2)


class HistorySource(Strict):
    id: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=4000)
    parameters: str = Field(max_length=6000)


class HistoryCatalog(Strict):
    status: Literal["supported", "unsupported", "unavailable"]
    reason: str = Field(min_length=1, max_length=4000)
    sources: list[HistorySource] = Field(max_length=16)

    @model_validator(mode="after")
    def distinct_sources(self):
        if len({s.id for s in self.sources}) != len(self.sources):
            raise ValueError("History source IDs must be distinct")
        if (self.status == "supported") != bool(self.sources):
            raise ValueError("Only supported history has sources")
        return self


class HistoryQuery(Strict):
    source_id: str
    parameters_json: str = Field(max_length=8000)

    @model_validator(mode="after")
    def object_parameters(self):
        import json
        if not isinstance(json.loads(self.parameters_json), dict):
            raise ValueError("History parameters must be a JSON object")
        return self


class HistoryPage(Strict):
    source_id: str
    data: dict
    evidence: list[Evidence] = Field(max_length=64)
    complete: bool
    next_parameters: dict | None
    error: str | None

    @model_validator(mode="after")
    def incomplete_is_not_evidence(self):
        if not self.complete or self.error:
            if self.evidence or not self.error:
                raise ValueError("Failed history reads need an error and cannot supply evidence")
        return self


class RetrospectiveDecision(Strict):
    assessment: str = Field(max_length=6000)
    notes: str = Field(max_length=8000)
    hypotheses: list[str] = Field(max_length=8)
    used_memory_ids: list[str] = Field(max_length=8)
    lessons: list[Lesson] = Field(max_length=8)
    query: HistoryQuery | None
    done: bool

    @model_validator(mode="after")
    def read_or_finish(self):
        if self.done == (self.query is not None):
            raise ValueError("Retrospective must either read one source or finish")
        if any(len(h) > 2000 for h in self.hypotheses):
            raise ValueError("Retrospective hypothesis exceeds 2000 characters")
        return self


def schema(model):
    def clean(x):
        if isinstance(x, list):
            return [clean(y) for y in x]
        if not isinstance(x, dict):
            return x
        x = {k: clean(v) for k, v in x.items() if k != "default"}
        if "properties" in x:
            x["required"] = list(x["properties"])
            x["additionalProperties"] = False
        return x
    return clean(model.model_json_schema())
