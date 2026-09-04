from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from functools import wraps
from itertools import product
from typing import Any

import pytest

from uptick_agent.memory.audit import (
    AuditTraceWrite,
    StructuredAuditTraceSink,
    audit_event_id,
)
from uptick_agent.memory.config import (
    AuditConfiguration,
    MemoryConfiguration,
    RawContentConfiguration,
)
from uptick_agent.memory.contracts import (
    MemoryConflictError,
    MemoryPermanentError,
    MemoryTransientError,
    MemoryValidationError,
)
from uptick_agent.memory.stores import InMemoryStructuredStore, SqliteStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite, StoredRecord, WriteReceipt
from uptick_agent.redaction import sanitize_json

_RUNTIME_FINGERPRINT = "a" * 64
_BASE_TIME = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
_BODY_CLASSES = ("prompts", "observations", "decision_traces")


def _run_async(function: Any) -> Any:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _configuration(flags: tuple[bool, bool, bool] = (True, True, True)) -> AuditConfiguration:
    prompts, observations, decision_traces = flags
    return AuditConfiguration(
        enabled=True,
        raw_content=RawContentConfiguration(
            prompts=prompts,
            observations=observations,
            decision_traces=decision_traces,
        ),
    )


def _store(kind: str, tmp_path: Any) -> InMemoryStructuredStore | SqliteStructuredStore:
    if kind == "memory":
        return InMemoryStructuredStore()
    return SqliteStructuredStore(tmp_path / "audit.sqlite")


def _sink(
    store: Any,
    *,
    flags: tuple[bool, bool, bool] = (True, True, True),
    sanitizer: Any = sanitize_json,
) -> StructuredAuditTraceSink:
    return StructuredAuditTraceSink(
        store,
        namespace="audit-test",
        configuration=_configuration(flags),
        runtime_configuration_fingerprint=_RUNTIME_FINGERPRINT,
        sanitizer=sanitizer,
    )


def _write(
    *,
    event_type: str = "decision.completed",
    occurred_at: datetime = _BASE_TIME,
    run_id: str = "run-1",
    sequence: int = 1,
    iteration: int | None = 1,
    request_id: str | None = "request-1",
    decision_id: str | None = "decision-1",
    transition_id: str | None = "transition-1",
    outcome_correlation_id: str | None = "outcome-1",
    raw_bodies: dict[str, dict[str, Any]] | None = None,
) -> AuditTraceWrite:
    if raw_bodies is None:
        raw_bodies = {
            "prompts": {"messages": [{"role": "user", "content": "inspect service"}]},
            "observations": {"latest": {"service": "api", "status": "healthy"}},
            "decision_traces": {
                "selected_item_ids": ["item-1"],
                "effective_token_limit": 128,
            },
        }
    return AuditTraceWrite(
        event_id=audit_event_id(
            event_type, run_id, sequence, decision_id, transition_id, outcome_correlation_id
        ),
        event_type=event_type,
        run_id=run_id,
        sequence=sequence,
        occurred_at=occurred_at,
        iteration=iteration,
        request_id=request_id,
        decision_id=decision_id,
        transition_id=transition_id,
        outcome_correlation_id=outcome_correlation_id,
        producer_id="test.audit",
        producer_version="1.0",
        metadata={
            "candidate_item_ids": ["item-1", "item-2"],
            "selected_item_ids": ["item-1"],
            "effective_token_limit": 128,
            "estimated_tokens": 37,
            "action": "restart",
            "outcome": "accepted",
        },
        raw_bodies=raw_bodies,
    )


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@pytest.mark.parametrize("raw_flags", list(product((False, True), repeat=3)))
@_run_async
async def test_raw_capture_matrix_is_policy_bounded_and_integrity_checked(
    store_kind: str,
    raw_flags: tuple[bool, bool, bool],
    tmp_path: Any,
) -> None:
    store = _store(store_kind, tmp_path)
    sink = _sink(store, flags=raw_flags)

    event = await sink.record(_write())

    assert event.event_id == _write().event_id
    assert event.runtime_configuration_fingerprint == _RUNTIME_FINGERPRINT
    assert event.audit_configuration_fingerprint == sink.audit_configuration_fingerprint
    assert event.raw_content_policy_ref == "simulator-raw-content-v1@1.0"
    assert event.retention_policy_ref == "simulator-audit-retention-v1@1.0"
    assert event.redactor_ref == "credential-pattern-redactor@1.0"
    assert {capture.body_class for capture in event.captures} == set(_BODY_CLASSES)

    for body_class, enabled in zip(_BODY_CLASSES, raw_flags, strict=True):
        capture = next(item for item in event.captures if item.body_class == body_class)
        if enabled:
            assert capture.state == "captured"
            assert capture.body
            assert capture.content_hash is not None
            assert capture.redaction_audit_hash is not None
            assert capture.redaction_outcome == "not_detected"
        else:
            assert capture.state == "disabled"
            assert capture.body is None
            assert capture.content_hash is None
            assert capture.redaction_audit_hash is None
            assert capture.redaction_outcome == "disabled"

    records = await store.list(namespace="audit-test")
    assert len(records) == 1
    assert records[0].record_id == event.event_id
    assert records[0].record_type == "audit-trace-event"
    assert await sink.list_events() == [event]


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@_run_async
async def test_redaction_keeps_secrets_out_of_event_and_store(
    store_kind: str,
    tmp_path: Any,
) -> None:
    store = _store(store_kind, tmp_path)
    sink = _sink(store)
    secret = "topsecret-credential"
    write = _write(
        raw_bodies={
            "prompts": {"content": f"token={secret}"},
            "observations": {"nested": {"password": secret}},
            "decision_traces": {"authorization": f"Bearer {secret}"},
        }
    )

    event = await sink.record(write)
    serialized_event = json.dumps(event.model_dump(mode="json"), sort_keys=True)
    serialized_store = json.dumps(
        (await store.list(namespace="audit-test"))[0].model_dump(mode="json"),
        sort_keys=True,
    )

    assert secret not in serialized_event
    assert secret not in serialized_store
    assert all(capture.state == "captured" for capture in event.captures)
    assert all(capture.redaction_outcome == "redacted" for capture in event.captures)
    assert all(capture.body is not None for capture in event.captures)


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@_run_async
async def test_redaction_failure_quarantines_body_without_persisting_it(
    store_kind: str,
    tmp_path: Any,
) -> None:
    def identity_redactor(value: object) -> object:
        return value

    store = _store(store_kind, tmp_path)
    sink = _sink(store, sanitizer=identity_redactor)
    event = await sink.record(
        _write(
            raw_bodies={
                "prompts": {"content": "token=must-not-cross-boundary"},
                "observations": {"status": "healthy"},
                "decision_traces": {"action": "restart"},
            }
        )
    )

    captures = {capture.body_class: capture for capture in event.captures}
    assert captures["prompts"].state == "quarantined"
    assert captures["prompts"].redaction_outcome == "failed"
    assert captures["prompts"].body is None
    assert captures["prompts"].content_hash is None
    assert captures["prompts"].redaction_audit_hash is not None
    for body_class in ("observations", "decision_traces"):
        assert captures[body_class].state == "captured"
        assert captures[body_class].redaction_outcome == "not_detected"
        assert captures[body_class].body is not None
    stored = (await store.list(namespace="audit-test"))[0]
    assert "must-not-cross-boundary" not in json.dumps(stored.model_dump(mode="json"))
    assert await sink.list_events() == [event]


class _FailOnceStore:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, str]] = []

    async def append(
        self,
        write: RecordWrite,
        *,
        operation: str,
        idempotency_key: str,
    ) -> WriteReceipt:
        self.calls.append((operation, idempotency_key))
        if len(self.calls) == 1:
            raise MemoryTransientError("temporary audit sink failure")
        return await self.delegate.append(
            write,
            operation=operation,
            idempotency_key=idempotency_key,
        )

    async def get(self, *, namespace: str, record_id: str) -> StoredRecord | None:
        return await self.delegate.get(namespace=namespace, record_id=record_id)

    async def list(self, *, namespace: str) -> list[StoredRecord]:
        return await self.delegate.list(namespace=namespace)


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@_run_async
async def test_transient_write_is_retried_once_and_remains_idempotent(
    store_kind: str,
    tmp_path: Any,
) -> None:
    delegate = _store(store_kind, tmp_path)
    store = _FailOnceStore(delegate)
    sink = _sink(store)
    write = _write()

    first = await sink.record(write)
    second = await sink.record(write)

    assert first == second
    assert len(store.calls) == 2
    assert store.calls[0] == store.calls[1]
    records = await delegate.list(namespace="audit-test")
    assert len(records) == 1
    assert records[0].record_id == write.event_id


class _CommitThenFailStore:
    def __init__(self, delegate: Any, error: Exception) -> None:
        self.delegate = delegate
        self.error = error
        self.append_calls = 0
        self.get_calls = 0

    async def append(
        self,
        write: RecordWrite,
        *,
        operation: str,
        idempotency_key: str,
    ) -> WriteReceipt:
        self.append_calls += 1
        receipt = await self.delegate.append(
            write,
            operation=operation,
            idempotency_key=idempotency_key,
        )
        if self.append_calls == 1:
            raise self.error
        return receipt

    async def get(self, *, namespace: str, record_id: str) -> StoredRecord | None:
        self.get_calls += 1
        return await self.delegate.get(namespace=namespace, record_id=record_id)

    async def list(self, *, namespace: str) -> list[StoredRecord]:
        return await self.delegate.list(namespace=namespace)


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "error",
    [MemoryTransientError("after commit"), MemoryConflictError("race")],
)
@_run_async
async def test_append_failure_after_commit_is_resolved_by_validated_replay(
    store_kind: str,
    error: Exception,
    tmp_path: Any,
) -> None:
    delegate = _store(store_kind, tmp_path)
    store = _CommitThenFailStore(delegate, error)
    sink = _sink(store)
    write = _write()

    event = await sink.record(write)

    assert event.event_id == write.event_id
    assert store.append_calls == 1
    assert store.get_calls == 2
    assert len(await delegate.list(namespace="audit-test")) == 1


class _FailOnceReadStore:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.get_calls = 0
        self.append_calls = 0

    async def get(self, *, namespace: str, record_id: str) -> StoredRecord | None:
        self.get_calls += 1
        if self.get_calls == 1:
            raise MemoryTransientError("temporary audit read failure")
        return await self.delegate.get(namespace=namespace, record_id=record_id)

    async def append(
        self,
        write: RecordWrite,
        *,
        operation: str,
        idempotency_key: str,
    ) -> WriteReceipt:
        self.append_calls += 1
        return await self.delegate.append(
            write,
            operation=operation,
            idempotency_key=idempotency_key,
        )

    async def list(self, *, namespace: str) -> list[StoredRecord]:
        return await self.delegate.list(namespace=namespace)


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@_run_async
async def test_transient_read_is_retried_once_before_append(
    store_kind: str,
    tmp_path: Any,
) -> None:
    delegate = _store(store_kind, tmp_path)
    store = _FailOnceReadStore(delegate)
    sink = _sink(store)

    event = await sink.record(_write())

    assert event.event_id
    assert store.get_calls == 2
    assert store.append_calls == 1


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@_run_async
async def test_replay_with_new_generated_timestamp_returns_original_event(
    store_kind: str,
    tmp_path: Any,
) -> None:
    store = _store(store_kind, tmp_path)
    sink = _sink(store)

    original = await sink.record(_write(occurred_at=_BASE_TIME))
    replay = await sink.record(_write(occurred_at=_BASE_TIME + timedelta(hours=1)))

    assert replay == original
    assert replay.occurred_at == _BASE_TIME
    assert len(await store.list(namespace="audit-test")) == 1


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@pytest.mark.parametrize("change", ["metadata", "body"])
@_run_async
async def test_replay_with_changed_safe_fields_conflicts(
    store_kind: str,
    change: str,
    tmp_path: Any,
) -> None:
    store = _store(store_kind, tmp_path)
    sink = _sink(store)
    original = _write()
    await sink.record(original)
    if change == "metadata":
        changed_metadata = {**original.metadata, "changed": True}
        changed = original.model_copy(update={"metadata": changed_metadata})
    else:
        changed_bodies = {
            **original.raw_bodies,
            "prompts": {"messages": [{"role": "user", "content": "changed"}]},
        }
        changed = original.model_copy(update={"raw_bodies": changed_bodies})

    with pytest.raises(MemoryConflictError, match="replay conflicts"):
        await sink.record(changed)


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@_run_async
async def test_sink_preserves_event_correlations_and_rejects_tampered_record(
    store_kind: str,
    tmp_path: Any,
) -> None:
    store = _store(store_kind, tmp_path)
    sink = _sink(store)
    common = {
        "run_id": "run-correlated",
        "raw_bodies": {
            "prompts": {"prompt": "choose action"},
            "observations": {"observation": "service degraded"},
            "decision_traces": {"candidate_item_ids": ["item-1"]},
        },
    }
    writes = [
        _write(
            **common,
            event_type="memory.context_selected",
            sequence=0,
            iteration=1,
            request_id="request-7",
            decision_id="decision-7",
            transition_id=None,
            outcome_correlation_id=None,
            occurred_at=_BASE_TIME,
        ),
        _write(
            **common,
            event_type="decision.input",
            sequence=1,
            iteration=1,
            request_id="request-7",
            decision_id="decision-7",
            transition_id=None,
            outcome_correlation_id=None,
            occurred_at=_BASE_TIME + timedelta(seconds=1),
        ),
        _write(
            **common,
            event_type="decision.completed",
            sequence=2,
            iteration=1,
            request_id="request-7",
            decision_id="decision-7",
            transition_id="transition-7",
            outcome_correlation_id="outcome-7",
            occurred_at=_BASE_TIME + timedelta(seconds=2),
        ),
        _write(
            **common,
            event_type="decision.selected",
            sequence=3,
            iteration=1,
            request_id="request-7",
            decision_id="decision-7",
            transition_id=None,
            outcome_correlation_id=None,
            occurred_at=_BASE_TIME + timedelta(seconds=3),
        ),
        _write(
            **common,
            event_type="memory.item_created",
            sequence=4,
            iteration=None,
            request_id=None,
            decision_id=None,
            transition_id="transition-7",
            outcome_correlation_id=None,
            occurred_at=_BASE_TIME + timedelta(seconds=4),
        ),
        _write(
            **common,
            event_type="run.outcome",
            sequence=5,
            iteration=None,
            request_id=None,
            decision_id=None,
            transition_id=None,
            outcome_correlation_id="outcome-7",
            occurred_at=_BASE_TIME + timedelta(seconds=5),
        ),
    ]

    events = [await sink.record(write) for write in writes]
    listed = await sink.list_events()

    assert [event.event_type for event in listed] == [write.event_type for write in writes]
    assert [event.event_id for event in listed] == [write.event_id for write in writes]
    assert [event.sequence for event in listed] == list(range(6))
    assert listed[0].decision_id == "decision-7"
    assert listed[1].iteration == 1 and listed[1].decision_id == "decision-7"
    assert listed[2].outcome_correlation_id == "outcome-7"
    assert listed[3].decision_id == "decision-7"
    assert listed[4].transition_id == "transition-7"
    assert listed[5].outcome_correlation_id == "outcome-7"
    assert listed == events

    records = await store.list(namespace="audit-test")
    tampered = records[0].model_copy(update={"content_hash": "b" * 64})

    class _TamperedReadStore:
        async def append(
            self,
            write: RecordWrite,
            *,
            operation: str,
            idempotency_key: str,
        ) -> WriteReceipt:
            return await store.append(
                write,
                operation=operation,
                idempotency_key=idempotency_key,
            )

        async def list(self, *, namespace: str) -> list[StoredRecord]:
            return [tampered]

        async def get(self, *, namespace: str, record_id: str) -> StoredRecord | None:
            return tampered

    tampered_sink = _sink(_TamperedReadStore())
    with pytest.raises(MemoryPermanentError, match="content hash"):
        await tampered_sink.list_events()


@pytest.mark.parametrize(
    ("event_type", "updates", "message"),
    [
        (
            "memory.context_selected",
            {"request_id": None, "decision_id": "decision-1"},
            "request_id",
        ),
        (
            "decision.input",
            {"request_id": None},
            "request_id",
        ),
        (
            "decision.selected",
            {"iteration": None},
            "iteration and decision_id",
        ),
        (
            "decision.completed",
            {"outcome_correlation_id": None},
            "outcome_correlation_id",
        ),
    ],
)
def test_decision_and_context_correlations_are_explicit(
    event_type: str,
    updates: dict[str, Any],
    message: str,
) -> None:
    base = _write(event_type=event_type)
    payload = base.model_dump(mode="python")
    payload.update(updates)
    with pytest.raises(ValueError, match=message):
        AuditTraceWrite.model_validate(payload)


def test_audit_configuration_fingerprint_covers_raw_policy_and_composes_into_runtime() -> None:
    enabled = _configuration()
    disabled = _configuration((False, True, True))
    changed_retention = AuditConfiguration(
        enabled=True,
        retention={"raw_content_and_snapshot_days": 120},
    )

    assert enabled.fingerprint != disabled.fingerprint
    assert enabled.fingerprint != changed_retention.fingerprint
    runtime = MemoryConfiguration.legacy_baseline(audit=enabled)
    assert runtime.audit == enabled
    assert runtime.fingerprint != MemoryConfiguration.legacy_baseline(audit=disabled).fingerprint

    with pytest.raises(ValueError, match="retention policy reference"):
        AuditConfiguration(
            enabled=True,
            raw_content={"retention_policy_ref": "other-policy@1.0"},
        )

    bypassed = enabled.model_copy(
        update={
            "raw_content": enabled.raw_content.model_copy(
                update={"policy_id": "unsupported-policy"}
            )
        }
    )
    with pytest.raises(MemoryValidationError, match="configuration is invalid"):
        StructuredAuditTraceSink(
            InMemoryStructuredStore(),
            namespace="audit-test",
            configuration=bypassed,
            runtime_configuration_fingerprint="b" * 64,
        )


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@_run_async
async def test_quoted_json_credentials_are_redacted_before_hashing_and_storage(
    store_kind: str,
    tmp_path: Any,
) -> None:
    store = _store(store_kind, tmp_path)
    sink = _sink(store)
    secret = "synthetic-demo-credential"
    prompt = json.dumps({"api_key": secret, "operation": "inspect"})

    event = await sink.record(
        _write(
            raw_bodies={
                "prompts": {"messages": [{"role": "user", "content": prompt}]},
                "observations": {"status": "healthy"},
                "decision_traces": {"action": "inspect"},
            }
        )
    )

    serialized_event = json.dumps(event.model_dump(mode="json"), sort_keys=True)
    serialized_store = json.dumps(
        (await store.list(namespace="audit-test"))[0].model_dump(mode="json"),
        sort_keys=True,
    )
    prompt_capture = next(capture for capture in event.captures if capture.body_class == "prompts")
    assert secret not in serialized_event
    assert secret not in serialized_store
    assert prompt_capture.redaction_outcome == "redacted"
    assert prompt_capture.content_hash
    assert prompt_capture.redaction_audit_hash
