from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from uptick_agent.memory.contracts import ExperienceTransition, TransitionAssemblyRequest
from uptick_agent.memory.observation_reader import (
    ObservationBookmark,
    ObservationReaderInvalidRequestError,
    ObservationReaderOffsetError,
    ObservationReaderUnavailableError,
    StoredObservationReader,
)
from uptick_agent.memory.stores import InMemoryStructuredStore, SqliteStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite, StoredRecord
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_NAMESPACE = "episodes"
_TIME = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)


def _transition(
    run_id: str = "run:reader",
    *,
    iteration: int = 1,
    environment_id: str | None = "environment:test",
    scenario_id: str | None = "scenario:test",
    result: dict | None = None,
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
            pre_state={"state": "ready"},
            observation={"state": "healthy"},
            action={"kind": "inspect"},
            result=result or {"ok": True, "summary": "🙂 durable result", "value": 7},
            terminal=True,
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


def _canonical_result(transition: ExperienceTransition) -> bytes:
    return json.dumps(
        transition.result,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


async def _scenario_bookmark_round_trip_and_exact_utf8_chunks() -> None:
    store = InMemoryStructuredStore()
    transition = _transition()
    record = await _append(store, transition)
    reader = StoredObservationReader(store, namespace=_NAMESPACE, run_id=transition.run_id)

    bookmark = await reader.bookmark(record.record_id, current_iteration=2)
    restored = ObservationBookmark.model_validate_json(bookmark.model_dump_json())
    expected = _canonical_result(transition)
    assert restored.content_hash == record.content_hash
    assert restored.record_hash == record.content_hash
    assert restored.result_bytes == len(expected)
    assert restored.result_digest == hashlib.sha256(expected).hexdigest()
    assert restored.environment_id == "environment:test"
    assert restored.summary == "🙂 durable result"

    chunks: list[bytes] = []
    offset = 0
    while True:
        response = await reader.read(restored, current_iteration=2, offset=offset, max_bytes=7)
        chunks.append(response["text"].encode("utf-8"))
        assert response["returned_bytes"] == len(chunks[-1])
        assert response["returned_bytes"] <= 7
        assert response["digest"] == restored.result_digest
        assert response["historical_evidence"] == "stale_environment_observation"
        if response["eof"]:
            break
        assert response["next_offset"] > offset
        offset = response["next_offset"]
    assert b"".join(chunks) == expected


async def _scenario_reopen_sqlite_reader_uses_same_bookmark_without_recording(tmp_path) -> None:
    path = tmp_path / "memory.sqlite3"
    first_store = SqliteStructuredStore(path)
    transition = _transition()
    record = await _append(first_store, transition)
    first_reader = StoredObservationReader(
        first_store, namespace=_NAMESPACE, run_id=transition.run_id
    )
    bookmark = await first_reader.bookmark(record.record_id, current_iteration=2)
    persisted = ObservationBookmark.model_validate_json(bookmark.model_dump_json())

    reopened = SqliteStructuredStore(path)
    second_reader = StoredObservationReader(
        reopened, namespace=_NAMESPACE, run_id=transition.run_id
    )
    response = await second_reader.read(persisted, current_iteration=2, max_bytes=8192)
    assert response["text"].encode("utf-8") == _canonical_result(transition)
    assert response["eof"] is True
    records = await reopened.list(namespace=_NAMESPACE)
    assert len(records) == 1
    assert records[0].content_hash == record.content_hash


async def _scenario_missing_canonical_member_is_unavailable() -> None:
    store = InMemoryStructuredStore()
    transition = _transition()
    record = await _append(store, transition)
    reader = StoredObservationReader(store, namespace=_NAMESPACE, run_id=transition.run_id)
    bookmark = await reader.bookmark(record.record_id, current_iteration=2)
    store._records.pop((_NAMESPACE, record.record_id))

    with pytest.raises(ObservationReaderUnavailableError):
        await reader.read(bookmark, current_iteration=2)


async def _scenario_content_hash_and_provenance_tamper_are_denied() -> None:
    store = InMemoryStructuredStore()
    transition = _transition()
    record = await _append(store, transition)
    reader = StoredObservationReader(store, namespace=_NAMESPACE, run_id=transition.run_id)
    bookmark = await reader.bookmark(record.record_id, current_iteration=2)

    store._records[(_NAMESPACE, record.record_id)] = record.model_copy(
        update={"content_hash": "0" * 64}
    )
    with pytest.raises(ObservationReaderUnavailableError):
        await reader.read(bookmark, current_iteration=2)

    tampered_payload = record.payload.copy()
    tampered_payload["action"] = {"kind": "tampered"}
    tampered = StoredRecord.from_write(
        RecordWrite(
            namespace=_NAMESPACE,
            record_id=record.record_id,
            record_type=record.record_type,
            payload=tampered_payload,
            created_at=record.created_at,
        )
    )
    store._records[(_NAMESPACE, record.record_id)] = tampered
    with pytest.raises(ObservationReaderUnavailableError):
        await reader.read(bookmark, current_iteration=2)


async def _scenario_wrong_namespace_run_and_bookmark_metadata_are_denied() -> None:
    store = InMemoryStructuredStore()
    transition = _transition()
    record = await _append(store, transition)
    reader = StoredObservationReader(store, namespace=_NAMESPACE, run_id=transition.run_id)
    bookmark = await reader.bookmark(record.record_id, current_iteration=2)

    other_run = StoredObservationReader(store, namespace=_NAMESPACE, run_id="run:other")
    with pytest.raises(ObservationReaderUnavailableError):
        await other_run.read(bookmark, current_iteration=2)
    other_namespace = StoredObservationReader(
        store, namespace="episodes:other", run_id=transition.run_id
    )
    with pytest.raises(ObservationReaderUnavailableError):
        await other_namespace.read(bookmark, current_iteration=2)
    with pytest.raises(ObservationReaderUnavailableError):
        await reader.read(
            bookmark.model_copy(update={"source_iteration": 99}), current_iteration=100
        )


async def _scenario_future_and_misaligned_or_oversized_reads_are_rejected() -> None:
    store = InMemoryStructuredStore()
    transition = _transition()
    record = await _append(store, transition)
    reader = StoredObservationReader(store, namespace=_NAMESPACE, run_id=transition.run_id)

    with pytest.raises(ObservationReaderInvalidRequestError):
        await reader.bookmark(record.record_id, current_iteration=transition.iteration)
    bookmark = await reader.bookmark(record.record_id, current_iteration=2)
    with pytest.raises(ObservationReaderInvalidRequestError):
        await reader.read(bookmark, current_iteration=1)
    with pytest.raises(ObservationReaderInvalidRequestError):
        await reader.read(bookmark, current_iteration=2, max_bytes=8193)

    expected = _canonical_result(transition)
    emoji_start = expected.index("🙂".encode())
    with pytest.raises(ObservationReaderOffsetError):
        await reader.read(bookmark, current_iteration=2, offset=emoji_start + 1)


async def _scenario_null_environment_and_scenario_remain_unknown() -> None:
    store = InMemoryStructuredStore()
    transition = _transition(environment_id=None, scenario_id=None)
    record = await _append(store, transition)
    reader = StoredObservationReader(store, namespace=_NAMESPACE, run_id=transition.run_id)

    bookmark = await reader.bookmark(record.record_id, current_iteration=2)
    assert bookmark.environment_id is None
    assert bookmark.scenario_id is None
    response = await reader.read(bookmark, current_iteration=2, max_bytes=8192)
    assert response["environment_id"] is None
    assert response["scenario_id"] is None


def test_bookmark_round_trip_and_exact_utf8_chunks() -> None:
    asyncio.run(_scenario_bookmark_round_trip_and_exact_utf8_chunks())


def test_reopen_sqlite_reader_uses_same_bookmark_without_recording(tmp_path) -> None:
    asyncio.run(_scenario_reopen_sqlite_reader_uses_same_bookmark_without_recording(tmp_path))


def test_missing_canonical_member_is_unavailable() -> None:
    asyncio.run(_scenario_missing_canonical_member_is_unavailable())


def test_content_hash_and_provenance_tamper_are_denied() -> None:
    asyncio.run(_scenario_content_hash_and_provenance_tamper_are_denied())


def test_wrong_namespace_run_and_bookmark_metadata_are_denied() -> None:
    asyncio.run(_scenario_wrong_namespace_run_and_bookmark_metadata_are_denied())


def test_future_and_misaligned_or_oversized_reads_are_rejected() -> None:
    asyncio.run(_scenario_future_and_misaligned_or_oversized_reads_are_rejected())


def test_null_environment_and_scenario_remain_unknown() -> None:
    asyncio.run(_scenario_null_environment_and_scenario_remain_unknown())
