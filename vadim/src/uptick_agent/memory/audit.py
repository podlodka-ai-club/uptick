"""Versioned, policy-aware Stage 5 evidence trace over the generic store."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, JsonValue, ValidationError, field_validator, model_validator
from pydantic_core import PydanticSerializationError

from uptick_agent.memory.config import AuditConfiguration
from uptick_agent.memory.contracts import (
    ContractModel,
    MemoryConflictError,
    MemoryPermanentError,
    MemoryTransientError,
    MemoryValidationError,
    require_finite_json,
)
from uptick_agent.memory.stores.contracts import (
    RecordWrite,
    StoredRecord,
    StructuredMemoryStore,
    sha256_json,
    validate_namespace,
)
from uptick_agent.redaction import sanitize_json

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


def _capture_audit_hash(
    *,
    body_class: RawBodyClass,
    state: CaptureState,
    redaction_outcome: RedactionOutcome,
    content_hash: str | None,
    configuration: AuditConfiguration,
) -> str:
    return sha256_json(
        {
            "body_class": body_class,
            "state": state,
            "redaction_outcome": redaction_outcome,
            "content_hash": content_hash,
            "policy": configuration.raw_content.model_dump(mode="json"),
        }
    )


class StructuredAuditTraceSink:
    """Persist safe trace events atomically through ``StructuredMemoryStore``."""

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        namespace: str,
        configuration: AuditConfiguration,
        runtime_configuration_fingerprint: str,
        sanitizer: Callable[[object], object] = sanitize_json,
    ) -> None:
        if not isinstance(configuration, AuditConfiguration):
            raise MemoryValidationError("audit configuration is invalid")
        try:
            owned_configuration = AuditConfiguration.model_validate(
                configuration.model_dump(mode="python", round_trip=True, warnings="error")
            )
        except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
            raise MemoryValidationError("audit configuration is invalid") from error
        if not owned_configuration.enabled:
            raise MemoryValidationError("structured audit trace requires enabled configuration")
        if len(runtime_configuration_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in runtime_configuration_fingerprint
        ):
            raise MemoryValidationError(
                "runtime configuration fingerprint must be lowercase SHA-256"
            )
        self._store = store
        self._namespace = validate_namespace(namespace)
        self._configuration = owned_configuration
        self._runtime_configuration_fingerprint = runtime_configuration_fingerprint
        self._sanitizer = sanitizer
        self._raw_content_policy_ref = (
            f"{owned_configuration.raw_content.policy_id}@"
            f"{owned_configuration.raw_content.policy_version}"
        )
        self._redactor_ref = (
            f"{owned_configuration.raw_content.redactor_id}@"
            f"{owned_configuration.raw_content.redactor_version}"
        )

    @property
    def runtime_configuration_fingerprint(self) -> str:
        return self._runtime_configuration_fingerprint

    @property
    def audit_configuration_fingerprint(self) -> str:
        return self._configuration.fingerprint

    async def record(self, write: AuditTraceWrite) -> AuditTraceEvent:
        owned = self._validate_write(write)
        metadata = self._safe_metadata(owned.metadata)
        captures = [
            self._capture(body_class, owned.raw_bodies[body_class])
            for body_class in sorted(owned.raw_bodies)
        ]
        event = AuditTraceEvent(
            **owned.model_dump(
                exclude={"raw_bodies", "metadata", "occurred_at"},
            ),
            occurred_at=owned.occurred_at.astimezone(UTC),
            runtime_configuration_fingerprint=self.runtime_configuration_fingerprint,
            audit_configuration_fingerprint=self.audit_configuration_fingerprint,
            raw_content_policy_ref=self._raw_content_policy_ref,
            retention_policy_ref=self._configuration.retention.reference,
            redactor_ref=self._redactor_ref,
            metadata=metadata,
            captures=captures,
        )
        serialized = event.model_dump(mode="json")
        if sanitize_json(serialized) != serialized:
            raise MemoryValidationError("audit event contains unsafe metadata after capture")
        record = RecordWrite(
            namespace=self._namespace,
            record_id=event.event_id,
            record_type=_EVENT_RECORD_TYPE,
            payload=serialized,
            created_at=event.occurred_at,
        )
        for attempt in range(2):
            try:
                existing = await self._store.get(
                    namespace=self._namespace,
                    record_id=event.event_id,
                )
                if existing is not None:
                    persisted = self._event_from_record(existing)
                    self._require_replay_match(persisted, event)
                    return persisted
                try:
                    await self._store.append(
                        record,
                        operation="record-audit-trace",
                        idempotency_key=f"audit:{event.event_id}",
                    )
                except MemoryConflictError:
                    # A competing writer may have committed between get() and
                    # append(). Resolve that race by the same semantic replay
                    # check instead of treating an identical event as a loss.
                    raced = await self._store.get(
                        namespace=self._namespace,
                        record_id=event.event_id,
                    )
                    if raced is None:
                        raise
                    persisted = self._event_from_record(raced)
                    self._require_replay_match(persisted, event)
                    return persisted
                return event
            except MemoryTransientError:
                if attempt == 1:
                    raise
        raise AssertionError("unreachable audit retry state")

    @staticmethod
    def _require_replay_match(
        persisted: AuditTraceEvent,
        candidate: AuditTraceEvent,
    ) -> None:
        """Reject a stable-ID rewrite while allowing generated timestamp drift."""

        persisted_fields = persisted.model_dump(mode="json", exclude={"occurred_at"})
        candidate_fields = candidate.model_dump(mode="json", exclude={"occurred_at"})
        if persisted_fields != candidate_fields:
            raise MemoryConflictError("audit event replay conflicts with persisted event")

    async def list_events(self) -> list[AuditTraceEvent]:
        records = await self._store.list(namespace=self._namespace)
        events = [self._event_from_record(record) for record in records]
        return sorted(events, key=lambda event: (event.sequence, event.event_id))

    @staticmethod
    def _validate_write(write: object) -> AuditTraceWrite:
        if not isinstance(write, AuditTraceWrite):
            raise MemoryValidationError("audit trace requires AuditTraceWrite")
        try:
            owned = AuditTraceWrite.model_validate(
                write.model_dump(mode="python", round_trip=True, warnings="error")
            )
        except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
            raise MemoryValidationError("audit trace write contains invalid data") from error
        if owned.occurred_at.utcoffset() is None:
            raise MemoryValidationError("audit trace timestamp must include a timezone")
        return owned

    def _safe_metadata(self, metadata: dict[str, JsonValue]) -> dict[str, JsonValue]:
        try:
            safe = sanitize_json(self._sanitizer(metadata))
        except Exception as error:
            raise MemoryValidationError("audit event metadata redaction failed") from error
        if not isinstance(safe, dict):
            raise MemoryValidationError("audit event metadata must remain a JSON object")
        return safe

    def _capture(
        self,
        body_class: RawBodyClass,
        body: dict[str, JsonValue],
    ) -> RawBodyCapture:
        if not self._configuration.raw_content.captures(body_class):
            return RawBodyCapture(
                body_class=body_class,
                state="disabled",
                redaction_outcome="disabled",
            )
        try:
            safe = self._sanitizer(body)
            if not isinstance(safe, dict):
                raise ValueError("redactor did not return a JSON object")
            if sanitize_json(safe) != safe:
                raise ValueError("redactor did not remove credential-shaped content")
            content_hash = sha256_json(safe)
            outcome: RedactionOutcome = "redacted" if safe != body else "not_detected"
            return RawBodyCapture(
                body_class=body_class,
                state="captured",
                redaction_outcome=outcome,
                body=safe,
                content_hash=content_hash,
                redaction_audit_hash=_capture_audit_hash(
                    body_class=body_class,
                    state="captured",
                    redaction_outcome=outcome,
                    content_hash=content_hash,
                    configuration=self._configuration,
                ),
            )
        except Exception:
            return RawBodyCapture(
                body_class=body_class,
                state="quarantined",
                redaction_outcome="failed",
                redaction_audit_hash=_capture_audit_hash(
                    body_class=body_class,
                    state="quarantined",
                    redaction_outcome="failed",
                    content_hash=None,
                    configuration=self._configuration,
                ),
            )

    def _event_from_record(self, record: StoredRecord) -> AuditTraceEvent:
        record = StoredRecord.validate_integrity(record)
        if record.record_type != _EVENT_RECORD_TYPE:
            raise MemoryPermanentError("audit namespace contains an unknown record type")
        try:
            event = AuditTraceEvent.model_validate(record.payload)
        except (TypeError, ValueError, ValidationError) as error:
            raise MemoryPermanentError("stored audit trace event is invalid") from error
        if record.record_id != event.event_id or record.created_at != event.occurred_at:
            raise MemoryPermanentError("stored audit record metadata does not match its event")
        if (
            event.runtime_configuration_fingerprint
            != self.runtime_configuration_fingerprint
            or event.audit_configuration_fingerprint
            != self.audit_configuration_fingerprint
        ):
            raise MemoryPermanentError("stored audit trace uses another resolved configuration")
        if (
            event.raw_content_policy_ref
            != self._raw_content_policy_ref
            or event.retention_policy_ref != self._configuration.retention.reference
            or event.redactor_ref != self._redactor_ref
        ):
            raise MemoryPermanentError("stored audit trace uses unsupported policy references")
        for capture in event.captures:
            content_hash = (
                sha256_json(capture.body) if capture.state == "captured" else None
            )
            if content_hash != capture.content_hash:
                raise MemoryPermanentError("stored audit body content hash does not match")
            expected_audit_hash = (
                None
                if capture.state == "disabled"
                else _capture_audit_hash(
                    body_class=capture.body_class,
                    state=capture.state,
                    redaction_outcome=capture.redaction_outcome,
                    content_hash=capture.content_hash,
                    configuration=self._configuration,
                )
            )
            if capture.redaction_audit_hash != expected_audit_hash:
                raise MemoryPermanentError("stored redaction audit hash does not match")
        return event
