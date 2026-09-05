"""Policy-aware structured audit implementation.

Public event contracts are defined in :mod:`uptick_agent.memory.audit_contracts`;
this module retains the storage sink and compatibility reexports.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC

from pydantic import JsonValue, ValidationError
from pydantic_core import PydanticSerializationError

from uptick_agent.memory.audit_contracts import (
    AuditTraceEvent,
    AuditTraceSink,
    AuditTraceWrite,
    CaptureState,
    RawBodyCapture,
    RawBodyClass,
    RedactionOutcome,
    audit_event_id,
)
from uptick_agent.memory.config import AuditConfiguration
from uptick_agent.memory.contracts import (
    MemoryConflictError,
    MemoryPermanentError,
    MemoryTransientError,
    MemoryValidationError,
)
from uptick_agent.memory.stores.contracts import (
    RecordWrite,
    StoredRecord,
    StructuredMemoryStore,
    sha256_json,
    validate_namespace,
)
from uptick_agent.redaction import sanitize_json

_EVENT_RECORD_TYPE = "audit-trace-event"

__all__ = [
    "AuditTraceEvent",
    "AuditTraceSink",
    "AuditTraceWrite",
    "RawBodyCapture",
    "RawBodyClass",
    "CaptureState",
    "RedactionOutcome",
    "audit_event_id",
    "StructuredAuditTraceSink",
]

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
