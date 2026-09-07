from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from uptick_agent.composition.handoff import ObservationHandoff, ObservationHandoffIdentity
from uptick_agent.memory.contracts import (
    ExperienceTransition,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.observation_reader import (
    ObservationReaderInvalidRequestError,
    ObservationReaderUnavailableError,
)
from uptick_agent.memory.stores import InMemoryStructuredStore, SqliteStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite, StoredRecord
from uptick_agent.runs.handoff import ObservationReadRequest
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_NAMESPACE = "episodes"
_TIME = datetime(2026, 9, 7, 6, 0, tzinfo=UTC)


def _transition(
    run_id: str,
    iteration: int = 1,
    *,
    environment_id: str | None = None,
    scenario_id: str | None = None,
) -> ExperienceTransition:
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}:{iteration}",
            run_id=run_id,
            iteration=iteration,
            occurred_at=_TIME + timedelta(minutes=iteration),
            environment_id=environment_id,
            scenario_id=scenario_id,
            trust_classification="external_untrusted",
            pre_state={"phase": "ready"},
            observation={"state": "healthy"},
            action={"kind": "inspect"},
            result={"ok": True, "summary": "historical 🙂 result", "value": iteration},
            terminal=False,
        )
    )


async def _append(store, transition: ExperienceTransition) -> StoredRecord:
    receipt = await store.append(
        RecordWrite(
            namespace=_NAMESPACE,
            record_id=transition.transition_id,
            record_type="experience-transition",
            payload=transition.model_dump(mode="json"),
            created_at=transition.occurred_at,
        ),
        operation="record-transition",
        idempotency_key=f"record:{transition.run_id}:{transition.iteration}",
    )
    return receipt.record


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _durable_identity(run_id: str) -> ObservationHandoffIdentity:
    return ObservationHandoffIdentity(
        namespace=_NAMESPACE,
        run_id=run_id,
        environment_id="environment:file-records",
        scenario_id="scenario:records-default-v1",
        environment_content_hash=hashlib.sha256(b"environment:file-records").hexdigest(),
        scenario_content_hash=hashlib.sha256(b"scenario:records-default-v1").hexdigest(),
    )


def test_read_request_requires_strict_bounded_integers() -> None:
    with pytest.raises(ValidationError):
        ObservationReadRequest(record_id="ref", offset=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ObservationReadRequest(record_id="ref", max_bytes="8")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ObservationReadRequest(record_id="ref", max_bytes=3)
    with pytest.raises(ValidationError):
        ObservationReadRequest(record_id="ref", max_bytes=8193)


async def _scenario_read_issued_observation() -> None:
    store = InMemoryStructuredStore()
    transition = _transition("run:handoff")
    record = await _append(store, transition)
    handoff = ObservationHandoff(store, _NAMESPACE)
    handoff.begin(transition.run_id)
    assert await handoff.note_transition("missing-record", current_iteration=2) is None
    bookmark = await handoff.note_transition(record.record_id, current_iteration=2)

    assert bookmark is not None
    assert handoff.snapshot(current_iteration=2) == [bookmark.model_dump(mode="json")]
    request = ObservationReadRequest(record_id=record.record_id, max_bytes=8192)
    result = await handoff.read(request, current_iteration=2)
    expected = _canonical(transition.result)
    assert result.action_kind == "memory.read"
    assert result.ok is True
    assert result.summary == "Historical observation."
    assert result.data["text"].encode("utf-8") == expected
    assert result.data["historical_evidence"] == "stale_environment_observation"
    assert result.objective_metrics == []
    assert result.operation_links == []
    assert result.terminal is False
    assert json.loads(result.model_dump_json())["data"]["text"] == result.data["text"]
    assert result.data["environment_id"] is None
    assert result.data["scenario_id"] is None


def test_read_issued_observation_has_historical_safe_result() -> None:
    asyncio.run(_scenario_read_issued_observation())


async def _scenario_bounds_issued_context() -> None:
    store = InMemoryStructuredStore()
    transitions = [_transition("run:bounded", iteration) for iteration in range(1, 5)]
    records = [await _append(store, transition) for transition in transitions]
    handoff = ObservationHandoff(store, _NAMESPACE, max_bookmarks=2, max_context_bytes=8_000)
    handoff.begin("run:bounded")
    for iteration, record in enumerate(records, start=1):
        await handoff.note_transition(record.record_id, current_iteration=iteration + 1)

    snapshot = handoff.snapshot(current_iteration=5)
    assert len(snapshot) == 2
    assert [item["record_id"] for item in snapshot] == [
        records[-2].record_id,
        records[-1].record_id,
    ]
    assert len(_canonical(snapshot)) <= 8_000

    # A tiny context budget cannot retain a bookmark, so the issued reference
    # is not accepted after the controller drops it.
    constrained = ObservationHandoff(store, _NAMESPACE, max_bookmarks=8, max_context_bytes=2)
    constrained.begin("run:bounded")
    assert await constrained.note_transition(records[0].record_id, current_iteration=2) is None
    assert constrained.snapshot(current_iteration=5) == []


def test_handoff_drops_oldest_and_respects_serialized_budget() -> None:
    asyncio.run(_scenario_bounds_issued_context())


async def _scenario_unknown_future_and_tampered_refs_fail_safely() -> None:
    store = InMemoryStructuredStore()
    transition = _transition("run:safe")
    record = await _append(store, transition)
    handoff = ObservationHandoff(store, _NAMESPACE)
    handoff.begin(transition.run_id)
    bookmark = await handoff.note_transition(record.record_id, current_iteration=2)
    assert bookmark is not None

    unknown = await handoff.read(
        ObservationReadRequest(record_id="never-issued"), current_iteration=2
    )
    assert unknown.ok is False
    assert unknown.summary == "Historical observation unavailable."
    assert "never-issued" not in unknown.model_dump_json()

    future = await handoff.read(
        ObservationReadRequest(record_id=record.record_id), current_iteration=1
    )
    assert future.ok is False
    assert future.objective_metrics == []

    store._records[(_NAMESPACE, record.record_id)] = record.model_copy(
        update={"content_hash": "0" * 64}
    )
    tampered = await handoff.read(
        ObservationReadRequest(record_id=record.record_id), current_iteration=2
    )
    assert tampered.ok is False
    assert "content hash" not in tampered.model_dump_json().casefold()


def test_unknown_future_and_tampered_refs_return_safe_failures() -> None:
    asyncio.run(_scenario_unknown_future_and_tampered_refs_fail_safely())


async def _scenario_begin_resets_cross_run_refs() -> None:
    store = InMemoryStructuredStore()
    first = _transition("run:first")
    second = _transition("run:second")
    first_record = await _append(store, first)
    second_record = await _append(store, second)
    handoff = ObservationHandoff(store, _NAMESPACE)

    handoff.begin(first.run_id)
    first_bookmark = await handoff.note_transition(first_record.record_id, current_iteration=2)
    assert first_bookmark is not None
    assert len(handoff.snapshot(current_iteration=2)) == 1

    handoff.begin(second.run_id)
    assert handoff.snapshot(current_iteration=2) == []
    old_ref = await handoff.read(
        ObservationReadRequest(record_id=first_record.record_id), current_iteration=2
    )
    assert old_ref.ok is False
    second_bookmark = await handoff.note_transition(second_record.record_id, current_iteration=2)
    assert second_bookmark is not None
    assert [item["record_id"] for item in handoff.snapshot(current_iteration=2)] == [
        second_record.record_id
    ]


def test_begin_resets_run_local_refs() -> None:
    asyncio.run(_scenario_begin_resets_cross_run_refs())


async def _scenario_durable_index_restores_from_fresh_sqlite(tmp_path) -> None:
    path = tmp_path / "handoff.sqlite3"
    transition = _transition(
        "run:durable",
        environment_id="environment:file-records",
        scenario_id="scenario:records-default-v1",
    )
    first_store = SqliteStructuredStore(path)
    record = await _append(first_store, transition)
    identity = _durable_identity(transition.run_id)
    first = ObservationHandoff(
        first_store,
        _NAMESPACE,
        durable_index_namespace="handoff-index",
    )
    await first.restore(identity, current_iteration=2)
    bookmark = await first.note_transition(record.record_id, current_iteration=2)
    assert bookmark is not None

    reopened = SqliteStructuredStore(path)
    second = ObservationHandoff(
        reopened,
        _NAMESPACE,
        durable_index_namespace="handoff-index",
    )
    await second.restore(identity, current_iteration=2)
    assert second.snapshot(current_iteration=2) == [bookmark.model_dump(mode="json")]
    result = await second.read(
        ObservationReadRequest(record_id=record.record_id, max_bytes=8192),
        current_iteration=2,
    )
    assert result.ok is True
    assert result.data["record_id"] == record.record_id
    assert len(await reopened.list(namespace=_NAMESPACE)) == 1
    assert len(await reopened.list(namespace="handoff-index")) == 1


def test_durable_index_restores_after_fresh_sqlite_object(tmp_path) -> None:
    asyncio.run(_scenario_durable_index_restores_from_fresh_sqlite(tmp_path))


async def _scenario_durable_index_does_not_resurrect_evicted_refs() -> None:
    store = InMemoryStructuredStore()
    transitions = [
        _transition(
            "run:eviction",
            iteration,
            environment_id="environment:file-records",
            scenario_id="scenario:records-default-v1",
        )
        for iteration in (1, 2)
    ]
    records = [await _append(store, transition) for transition in transitions]
    identity = _durable_identity(transitions[0].run_id)
    handoff = ObservationHandoff(
        store,
        _NAMESPACE,
        max_bookmarks=1,
        durable_index_namespace="handoff-index",
    )
    await handoff.restore(identity, current_iteration=2)
    assert await handoff.note_transition(records[0].record_id, current_iteration=2)
    second = await handoff.note_transition(records[1].record_id, current_iteration=3)
    assert second is not None

    restored = ObservationHandoff(
        store,
        _NAMESPACE,
        max_bookmarks=1,
        durable_index_namespace="handoff-index",
    )
    await restored.restore(identity, current_iteration=3)
    assert [item["record_id"] for item in restored.snapshot(current_iteration=3)] == [
        records[1].record_id
    ]
    old_read = await restored.read(
        ObservationReadRequest(record_id=records[0].record_id),
        current_iteration=3,
    )
    assert old_read.ok is False

    empty_identity = _durable_identity("run:empty-index")
    empty_transition = _transition(
        empty_identity.run_id,
        environment_id=empty_identity.environment_id,
        scenario_id=empty_identity.scenario_id,
    )
    empty_record = await _append(store, empty_transition)
    empty = ObservationHandoff(
        store,
        _NAMESPACE,
        max_context_bytes=2,
        durable_index_namespace="handoff-index",
    )
    await empty.restore(empty_identity, current_iteration=2)
    assert await empty.note_transition(empty_record.record_id, current_iteration=2) is None
    empty_reopen = ObservationHandoff(
        store,
        _NAMESPACE,
        max_context_bytes=2,
        durable_index_namespace="handoff-index",
    )
    await empty_reopen.restore(empty_identity, current_iteration=2)
    assert empty_reopen.snapshot(current_iteration=2) == []


def test_durable_index_preserves_eviction_and_empty_latest_state() -> None:
    asyncio.run(_scenario_durable_index_does_not_resurrect_evicted_refs())


async def _scenario_durable_index_fails_closed() -> None:
    identity = _durable_identity("run:durable-fail-closed")
    transition = _transition(
        identity.run_id,
        environment_id=identity.environment_id,
        scenario_id=identity.scenario_id,
    )
    store = InMemoryStructuredStore()
    record = await _append(store, transition)
    handoff = ObservationHandoff(
        store,
        _NAMESPACE,
        durable_index_namespace="handoff-index",
    )
    await handoff.restore(identity, current_iteration=2)
    assert await handoff.note_transition(record.record_id, current_iteration=2)

    wrong = ObservationHandoff(
        store,
        _NAMESPACE,
        durable_index_namespace="handoff-index",
    )
    wrong_identity = identity.model_copy(update={"scenario_id": "scenario:other"})
    await wrong.restore(wrong_identity, current_iteration=2)
    assert wrong.snapshot(current_iteration=2) == []

    with pytest.raises(ObservationReaderUnavailableError):
        await ObservationHandoff(
            store,
            _NAMESPACE,
            durable_index_namespace="handoff-index",
        ).restore(identity, current_iteration=1)

    index_records = await store.list(namespace="handoff-index")
    index_record = index_records[-1]
    store._records[("handoff-index", index_record.record_id)] = index_record.model_copy(
        update={"content_hash": "0" * 64}
    )
    with pytest.raises(ObservationReaderUnavailableError):
        await ObservationHandoff(
            store,
            _NAMESPACE,
            durable_index_namespace="handoff-index",
        ).restore(identity, current_iteration=2)


def test_durable_index_rejects_wrong_identity_future_cutoff_and_corruption() -> None:
    asyncio.run(_scenario_durable_index_fails_closed())


async def _scenario_restore_clears_reused_state_and_owns_identity() -> None:
    store = InMemoryStructuredStore()
    identity = _durable_identity("run:restore-state")
    transition = _transition(
        identity.run_id,
        environment_id=identity.environment_id,
        scenario_id=identity.scenario_id,
    )
    record = await _append(store, transition)
    handoff = ObservationHandoff(
        store,
        _NAMESPACE,
        durable_index_namespace="handoff-index",
    )
    await handoff.restore(identity, current_iteration=2)
    assert await handoff.note_transition(record.record_id, current_iteration=2)

    wrong_namespace = identity.model_copy(update={"namespace": "episodes:other"})
    with pytest.raises(ObservationReaderInvalidRequestError):
        await handoff.restore(wrong_namespace, current_iteration=2)
    assert handoff.snapshot(current_iteration=2) == []
    assert handoff.run_id is None

    await handoff.restore(identity, current_iteration=2)
    identity.scenario_id = "scenario:caller-mutated"
    assert await handoff.note_transition(record.record_id, current_iteration=2) is not None

    with pytest.raises(ObservationReaderUnavailableError):
        await handoff.restore(_durable_identity(identity.run_id), current_iteration=1)
    assert handoff.snapshot(current_iteration=2) == []
    assert handoff.run_id is None

    await handoff.restore(_durable_identity(identity.run_id), current_iteration=2)
    with pytest.raises(ObservationReaderInvalidRequestError):
        await handoff.restore(object(), current_iteration=2)  # type: ignore[arg-type]
    assert handoff.snapshot(current_iteration=2) == []
    assert handoff.run_id is None


def test_restore_clears_reused_state_and_owns_identity() -> None:
    asyncio.run(_scenario_restore_clears_reused_state_and_owns_identity())
