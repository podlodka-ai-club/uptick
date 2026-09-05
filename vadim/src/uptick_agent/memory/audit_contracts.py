"""Public audit event contracts and pure correlation helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, JsonValue, field_validator, model_validator

from uptick_agent.memory.contracts import (
    ContractModel,
    require_finite_json,
)

RawBodyClass = Literal["prompts", "observations", "decision_traces"]
AuditEventType = Literal[
    "memory.context_selected",
    "memory.item_created",
    "decision.input",
    "decision.selected",
    "decision.completed",
    "run.outcome",
]
CaptureState = Literal["captured", "disabled", "quarantined"]
RedactionOutcome = Literal["not_detected", "redacted", "failed", "disabled"]

_EVENT_RECORD_TYPE = "audit-trace-event"
_HEX_DIGEST = r"^[0-9a-f]{64}$"


def audit_event_id(event_type: AuditEventType, *identity: object) -> str:
    """Build a stable event ID from non-body correlation data only."""

    rendered = json.dumps(
        {"event_type": event_type, "identity": identity},
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class RawBodyCapture(ContractModel):
    body_class: RawBodyClass
    state: CaptureState
    redaction_outcome: RedactionOutcome
    body: dict[str, JsonValue] | None = None
    content_hash: str | None = Field(default=None, pattern=_HEX_DIGEST)
    redaction_audit_hash: str | None = Field(default=None, pattern=_HEX_DIGEST)

    @field_validator("body", mode="before")
    @classmethod
    def _require_finite_body(cls, value: object) -> object:
        return require_finite_json(value)

    @model_validator(mode="after")
    def _state_matches_content(self) -> RawBodyCapture:
        if self.state == "captured":
            if (
                self.body is None
                or self.content_hash is None
                or self.redaction_audit_hash is None
                or self.redaction_outcome not in {"not_detected", "redacted"}
            ):
                raise ValueError("captured trace body requires safe body, hashes, and outcome")
        elif self.state == "quarantined":
            if (
                self.body is not None
                or self.content_hash is not None
                or self.redaction_audit_hash is None
                or self.redaction_outcome != "failed"
            ):
                raise ValueError("quarantined trace body must contain audit metadata only")
        elif (
            self.body is not None
            or self.content_hash is not None
            or self.redaction_audit_hash is not None
            or self.redaction_outcome != "disabled"
        ):
            raise ValueError("disabled trace body must not contain body or hashes")
        return self


class AuditTraceWrite(ContractModel):
    """One caller-owned event before policy-controlled body capture."""

    event_id: str = Field(min_length=64, max_length=64, pattern=_HEX_DIGEST)
    event_type: AuditEventType
    run_id: str = Field(min_length=1, max_length=256)
    sequence: int = Field(ge=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    iteration: int | None = Field(default=None, ge=1)
    request_id: str | None = Field(default=None, min_length=1, max_length=256)
    decision_id: str | None = Field(default=None, min_length=1, max_length=256)
    transition_id: str | None = Field(default=None, min_length=1, max_length=256)
    outcome_correlation_id: str | None = Field(default=None, min_length=1, max_length=256)
    producer_id: str = Field(min_length=1, max_length=128)
    producer_version: str = Field(min_length=1, max_length=64)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    raw_bodies: dict[RawBodyClass, dict[str, JsonValue]] = Field(min_length=1, max_length=3)

    @field_validator("metadata", "raw_bodies", mode="before")
    @classmethod
    def _require_finite_json(cls, value: object) -> object:
        return require_finite_json(value)

    @model_validator(mode="after")
    def _require_correlations(self) -> AuditTraceWrite:
        _validate_event_correlations(
            event_type=self.event_type,
            iteration=self.iteration,
            request_id=self.request_id,
            decision_id=self.decision_id,
            transition_id=self.transition_id,
            outcome_correlation_id=self.outcome_correlation_id,
        )
        return self


class AuditTraceEvent(ContractModel):
    """Immutable event after redaction, hashing, and raw-body policy application."""

    event_id: str = Field(min_length=64, max_length=64, pattern=_HEX_DIGEST)
    event_type: AuditEventType
    run_id: str = Field(min_length=1, max_length=256)
    sequence: int = Field(ge=0)
    occurred_at: datetime
    iteration: int | None = Field(default=None, ge=1)
    request_id: str | None = Field(default=None, min_length=1, max_length=256)
    decision_id: str | None = Field(default=None, min_length=1, max_length=256)
    transition_id: str | None = Field(default=None, min_length=1, max_length=256)
    outcome_correlation_id: str | None = Field(default=None, min_length=1, max_length=256)
    producer_id: str = Field(min_length=1, max_length=128)
    producer_version: str = Field(min_length=1, max_length=64)
    runtime_configuration_fingerprint: str = Field(pattern=_HEX_DIGEST)
    audit_configuration_fingerprint: str = Field(pattern=_HEX_DIGEST)
    raw_content_policy_ref: str = Field(min_length=1, max_length=128)
    retention_policy_ref: str = Field(min_length=1, max_length=128)
    redactor_ref: str = Field(min_length=1, max_length=192)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    captures: list[RawBodyCapture] = Field(min_length=1, max_length=3)

    @field_validator("metadata", mode="before")
    @classmethod
    def _require_finite_metadata(cls, value: object) -> object:
        return require_finite_json(value)

    @model_validator(mode="after")
    def _require_unique_capture_classes(self) -> AuditTraceEvent:
        classes = [capture.body_class for capture in self.captures]
        if len(classes) != len(set(classes)):
            raise ValueError("audit event capture classes must be unique")
        _validate_event_correlations(
            event_type=self.event_type,
            iteration=self.iteration,
            request_id=self.request_id,
            decision_id=self.decision_id,
            transition_id=self.transition_id,
            outcome_correlation_id=self.outcome_correlation_id,
        )
        return self


@runtime_checkable
class AuditTraceSink(Protocol):
    @property
    def runtime_configuration_fingerprint(self) -> str: ...

    @property
    def audit_configuration_fingerprint(self) -> str: ...

    async def record(self, write: AuditTraceWrite) -> AuditTraceEvent: ...


def _validate_event_correlations(
    *,
    event_type: AuditEventType,
    iteration: int | None,
    request_id: str | None,
    decision_id: str | None,
    transition_id: str | None,
    outcome_correlation_id: str | None,
) -> None:
    """Enforce only the correlations needed to join related trace events."""

    decision_events = {
        "decision.input",
        "decision.selected",
        "decision.completed",
    }
    if (event_type == "memory.context_selected" or event_type in decision_events) and (
        request_id is None
    ):
        raise ValueError(f"{event_type} trace requires request_id")
    if event_type in decision_events and (iteration is None or decision_id is None):
        raise ValueError("decision trace events require iteration and decision_id")
    if event_type == "decision.completed":
        if transition_id is None:
            raise ValueError("completed decision trace requires transition_id")
        if outcome_correlation_id is None:
            raise ValueError("completed decision trace requires outcome_correlation_id")
    if event_type == "memory.item_created" and transition_id is None:
        raise ValueError("created memory item trace requires transition_id")
    if event_type == "run.outcome" and outcome_correlation_id is None:
        raise ValueError("run outcome trace requires outcome_correlation_id")


for _model in (RawBodyCapture, AuditTraceWrite, AuditTraceEvent):
    _model.__module__ = "uptick_agent.memory.audit"
