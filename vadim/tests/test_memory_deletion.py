from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from uptick_agent.memory.contracts import (
    MemoryConflictError,
    MemoryPermanentError,
    MemoryTransientError,
    RunOutcome,
)
from uptick_agent.memory.deletion import (
    DeletionCandidate,
    DeletionPolicy,
    PhysicalDeletionManager,
    plan_hash,
)
from uptick_agent.memory.deletion_contracts import (
    DeletionAuthorization,
    DeletionHold,
    PhysicalDeletionPlan,
    RecordRef,
    SnapshotRef,
    SnapshotRetirement,
)
from uptick_agent.memory.stores import InMemoryStructuredStore, SqliteStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_DELETABLE_TYPES = ["experience-transition", "episode", "generic-evidence"]


def _authorization_payload(*, retire_snapshot_refs: list | None = None) -> dict:
    payload = {
        "policy_id": "test-retention",
        "policy_version": "1.0",
        "scope": "physical-delete",
        "minimum_raw_age_days": 90,
        "deletable_record_types": _DELETABLE_TYPES,
    }
    if retire_snapshot_refs:
        payload["retire_snapshot_refs"] = [
            ref.model_dump(mode="json") for ref in retire_snapshot_refs
        ]
        payload["no_active_external_bindings_attested"] = True
    return payload


def _ref(record) -> RecordRef:
    return RecordRef(
        namespace=record.namespace,
        record_id=record.record_id,
        content_hash=record.content_hash,
    )


def _run(awaitable):
    return asyncio.run(awaitable)


def _write(
    record_id: str,
    *,
    created_at: datetime,
    record_type: str = "generic-evidence",
    payload: dict | None = None,
    namespace: str = "raw",
) -> RecordWrite:
    return RecordWrite(
        namespace=namespace,
        record_id=record_id,
        record_type=record_type,
        payload=payload or {"value": record_id},
        created_at=created_at,
    )


def _run_outcome_write(
    run_id: str,
    *,
    finished_at: datetime,
    status: str = "completed",
    namespace: str = "raw",
) -> RecordWrite:
    outcome = RunOutcome(
        run_id=run_id,
        status=status,
        finished_at=finished_at,
        stop_reason="test terminal outcome",
    )
    return _write(
        f"run-outcome-{run_id}-{finished_at.timestamp()}",
        namespace=namespace,
        record_type="run-outcome",
        created_at=finished_at,
        payload=outcome.model_dump(mode="json"),
    )


def _store(backend: str, tmp_path):
    if backend == "memory":
        return InMemoryStructuredStore()
    return SqliteStructuredStore(tmp_path / "memory.sqlite")


async def _append(store, write: RecordWrite, key: str):
    return await store.append(write, operation="capture", idempotency_key=key)


async def _manager(store, authorization, *, now: datetime = _NOW):
    policy = DeletionPolicy(
        policy_id="test-retention",
        policy_version="1.0",
        authorization=RecordRef(
            namespace=authorization.namespace,
            record_id=authorization.record_id,
            content_hash=authorization.content_hash,
        ),
    )
    return PhysicalDeletionManager(store, policy=policy, clock=lambda: now)


def test_run_outcome_is_always_project_lifetime_protected():
    with pytest.raises(ValueError, match="overlap project lifetime"):
        DeletionPolicy(
            policy_id="test-retention",
            policy_version="1.0",
            deletable_record_types=["run-outcome"],
            authorization=RecordRef(
                namespace="controls",
                record_id="owner",
                content_hash="a" * 64,
            ),
        )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_old_orphan_is_deleted_with_tombstone_and_receipt_marker(backend: str, tmp_path):
    async def scenario():
        store = _store(backend, tmp_path)
        authorization = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=_NOW - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        old = await _append(
            store,
            _write("old", created_at=_NOW - timedelta(days=120), payload={"secret": "erase-me"}),
            "old-write",
        )
        old_two = await _append(
            store,
            _write("old-two", created_at=_NOW - timedelta(days=120)),
            "old-two-write",
        )
        await _append(
            store,
            _write("recent", created_at=_NOW - timedelta(days=1)),
            "recent-write",
        )
        manager = await _manager(store, authorization.record)

        plan = await manager.create_plan(request_id="delete-1")
        assert [item.ref.record_id for item in plan.candidates] == ["old", "old-two"]
        assert {item.ref.record_id for item in plan.blocked} == {"owner", "recent"}
        before = await store.get(namespace="raw", record_id="old")
        assert before is not None
        result = await manager.apply(plan, idempotency_key="delete-key")
        assert result.applied is True
        assert result.already_applied is False
        assert await store.get(namespace="raw", record_id="old") is None
        tombstones = await store.list(namespace=plan.tombstone_namespace)
        assert len(tombstones) == 2
        assert {item.payload["target"]["record_id"] for item in tombstones} == {
            "old",
            "old-two",
        }

        inventory = await store.deletion_inventory()
        assert {ref.record_id for ref in inventory.deleted_records} == {"old", "old-two"}
        marker = next(item for item in inventory.receipts if item.idempotency_key == "old-write")
        assert marker.receipt_kind == "deleted"
        assert marker.record_ref == _ref(old.record)
        marker_two = next(
            item for item in inventory.receipts if item.idempotency_key == "old-two-write"
        )
        assert marker_two.record_ref == _ref(old_two.record)
        with pytest.raises(MemoryConflictError, match="physically deleted"):
            await _append(
                store,
                _write("old", created_at=_NOW, payload={"new": "value"}),
                "old-recreate",
            )

        replay = await manager.apply(plan, idempotency_key="delete-key")
        assert replay.already_applied is True
        assert replay.deleted_records == result.deleted_records
        forged = plan.model_copy(update={"candidates": []})
        with pytest.raises(MemoryConflictError, match="content hash"):
            await store.apply_deletion(forged, idempotency_key="forged-key")
        with pytest.raises(MemoryConflictError, match="another idempotency key"):
            await store.apply_deletion(plan, idempotency_key="different-key")

        if backend == "sqlite":
            with sqlite3.connect(tmp_path / "memory.sqlite") as connection:
                receipt_json = connection.execute(
                    "SELECT receipt_json FROM memory_operation_receipts "
                    "WHERE idempotency_key = 'old-write'"
                ).fetchone()[0]
            assert "erase-me" not in receipt_json
            assert '"record":' not in receipt_json

    _run(scenario())


@pytest.mark.parametrize(
    ("completion_age_days", "expected_candidate"),
    [(None, False), (1, False), (120, True)],
)
def test_run_records_age_from_one_authoritative_terminal_outcome(
    completion_age_days: int | None, expected_candidate: bool
):
    async def scenario():
        now = datetime.now(UTC)
        store = InMemoryStructuredStore()
        owner = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=now - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        raw = await _append(
            store,
            _write(
                "run-raw",
                created_at=now - timedelta(days=120),
                payload={"run_id": "run-1", "value": "raw"},
            ),
            "run-raw-write",
        )
        if completion_age_days is not None:
            await _append(
                store,
                _run_outcome_write("run-1", finished_at=now - timedelta(days=completion_age_days)),
                "run-outcome-write",
            )
        plan = await (await _manager(store, owner.record, now=now)).create_plan(
            request_id="run-floor"
        )
        candidate_ids = {item.ref.record_id for item in plan.candidates}
        assert (raw.record.record_id in candidate_ids) is expected_candidate
        if expected_candidate:
            return
        blocked = next(item for item in plan.blocked if item.ref.record_id == raw.record.record_id)
        reason = " ".join(blocked.reasons)
        if completion_age_days is None:
            assert "missing" in reason
        else:
            assert "retention until" in reason

    _run(scenario())


def test_run_record_with_ambiguous_or_naive_outcome_is_retained():
    async def scenario():
        now = datetime.now(UTC)
        store = InMemoryStructuredStore()
        owner = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=now - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        raw = await _append(
            store,
            _write(
                "run-raw",
                created_at=now - timedelta(days=120),
                payload={"run_id": "run-ambiguous", "value": "raw"},
            ),
            "run-raw-write",
        )
        await _append(
            store,
            _run_outcome_write("run-ambiguous", finished_at=now - timedelta(days=120)),
            "outcome-one-write",
        )
        await _append(
            store,
            _run_outcome_write("run-ambiguous", finished_at=now - timedelta(days=119)),
            "outcome-two-write",
        )
        plan = await (await _manager(store, owner.record, now=now)).create_plan(
            request_id="ambiguous-floor"
        )
        blocked = next(item for item in plan.blocked if item.ref.record_id == raw.record.record_id)
        assert "ambiguous" in " ".join(blocked.reasons)

        naive_store = InMemoryStructuredStore()
        naive_owner = await _append(
            naive_store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=now - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        naive_raw = await _append(
            naive_store,
            _write(
                "naive-raw",
                created_at=now - timedelta(days=120),
                payload={"run_id": "run-naive", "value": "raw"},
            ),
            "naive-raw-write",
        )
        naive_outcome = _run_outcome_write("run-naive", finished_at=now - timedelta(days=120))
        naive_payload = {
            "schema_version": "1.0",
            "run_id": "run-naive",
            "status": "completed",
            "finished_at": "2025-01-01T00:00:00",
            "stop_reason": "naive outcome",
            "objective_metrics": [],
            "terminal": True,
        }
        naive_outcome = naive_outcome.model_copy(update={"payload": naive_payload})
        await _append(naive_store, naive_outcome, "naive-outcome-write")
        naive_plan = await (await _manager(naive_store, naive_owner.record, now=now)).create_plan(
            request_id="naive-floor"
        )
        naive_block = next(
            item for item in naive_plan.blocked if item.ref.record_id == naive_raw.record.record_id
        )
        assert "naive" in " ".join(naive_block.reasons)

    _run(scenario())


def test_experiment_association_blocks_even_with_run_outcome():
    async def scenario():
        now = datetime.now(UTC)
        store = InMemoryStructuredStore()
        owner = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=now - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        raw = await _append(
            store,
            _write(
                "experiment-raw",
                created_at=now - timedelta(days=120),
                payload={"experiment_id": "exp-1", "run_id": "run-1"},
            ),
            "experiment-raw-write",
        )
        await _append(
            store,
            _run_outcome_write("run-1", finished_at=now - timedelta(days=120)),
            "outcome-write",
        )
        plan = await (await _manager(store, owner.record, now=now)).create_plan(
            request_id="experiment-floor"
        )
        blocked = next(item for item in plan.blocked if item.ref.record_id == raw.record.record_id)
        assert "experiment-associated" in " ".join(blocked.reasons)

    _run(scenario())


def test_store_rejects_resealed_candidate_before_completion_floor():
    async def scenario():
        now = datetime.now(UTC)
        store = InMemoryStructuredStore()
        owner = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=now - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        raw = await _append(
            store,
            _write(
                "run-raw",
                created_at=now - timedelta(days=120),
                payload={"run_id": "run-early", "value": "raw"},
            ),
            "run-raw-write",
        )
        await _append(
            store,
            _run_outcome_write("run-early", finished_at=now - timedelta(days=1)),
            "run-outcome-write",
        )
        manager = await _manager(store, owner.record, now=now)
        plan = await manager.create_plan(request_id="early-plan")
        blocked = [item for item in plan.blocked if item.ref.record_id != raw.record.record_id]
        receipt = next(
            item
            for item in (await store.deletion_inventory()).receipts
            if item.record_ref is not None and item.record_ref.record_id == raw.record.record_id
        )
        forged = plan.model_copy(
            update={
                "candidates": [
                    DeletionCandidate(
                        ref=_ref(raw.record),
                        record_type=raw.record.record_type,
                        eligible_at=raw.record.created_at + timedelta(days=90),
                        reason="forged from row timestamp",
                    )
                ],
                "blocked": blocked,
                "purge_receipts": [receipt],
            }
        )
        forged = forged.model_copy(update={"plan_id": plan_hash(forged)})
        with pytest.raises(MemoryConflictError, match="not retention-eligible"):
            await store.apply_deletion(forged, idempotency_key="early-forged")
        assert await store.get(namespace="raw", record_id="run-raw") is not None

    _run(scenario())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_snapshot_retirement_inherits_member_completion_floor(backend: str, tmp_path):
    async def scenario():
        now = datetime.now(UTC)
        store = _store(backend, tmp_path)
        await _append(
            store,
            _write(
                "snapshot-run-raw",
                created_at=now - timedelta(days=120),
                payload={"run_id": "snapshot-run", "value": "raw"},
            ),
            "snapshot-run-raw-write",
        )
        await _append(
            store,
            _run_outcome_write("snapshot-run", finished_at=now - timedelta(days=1)),
            "snapshot-run-outcome-write",
        )
        frozen = await store.create_snapshot(
            namespace="raw",
            snapshot_id="recent-member-freeze",
            operation="freeze",
            idempotency_key="recent-member-freeze-key",
        )
        aged_snapshot = frozen.snapshot.model_copy(update={"created_at": now - timedelta(days=120)})
        if backend == "memory":
            store._snapshots[aged_snapshot.snapshot_id] = aged_snapshot
            store._receipts[("raw", "freeze", "recent-member-freeze-key")] = frozen.model_copy(
                update={"snapshot": aged_snapshot}
            )
        else:
            with sqlite3.connect(tmp_path / "memory.sqlite") as connection:
                connection.execute(
                    "UPDATE memory_snapshots SET created_at = ? WHERE snapshot_id = ?",
                    (aged_snapshot.created_at.isoformat(), aged_snapshot.snapshot_id),
                )
                connection.execute(
                    "UPDATE memory_operation_receipts SET receipt_json = ? "
                    "WHERE namespace = 'raw' AND operation = 'freeze' "
                    "AND idempotency_key = 'recent-member-freeze-key'",
                    (frozen.model_copy(update={"snapshot": aged_snapshot}).model_dump_json(),),
                )
        snapshot_ref = SnapshotRef(
            namespace=aged_snapshot.namespace,
            snapshot_id=aged_snapshot.snapshot_id,
            created_at=aged_snapshot.created_at,
            content_hash=aged_snapshot.content_hash,
            members=aged_snapshot.members,
        )
        owner = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=now - timedelta(days=300),
                payload=_authorization_payload(retire_snapshot_refs=[snapshot_ref]),
            ),
            "owner-write",
        )
        plan = await (await _manager(store, owner.record, now=now)).create_plan(
            request_id="snapshot-member-floor"
        )
        assert not plan.retired_snapshots
        blocked = next(
            item
            for item in plan.blocked_snapshots
            if item.snapshot.snapshot_id == aged_snapshot.snapshot_id
        )
        assert "retention until" in " ".join(blocked.reasons)
        deadline = datetime.fromisoformat(blocked.reasons[0].removeprefix("retention until "))
        assert deadline == now + timedelta(days=89)

    _run(scenario())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_snapshot_hold_and_provenance_are_protected(backend: str, tmp_path):
    async def scenario():
        store = _store(backend, tmp_path)
        authorization = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=_NOW - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        await _append(
            store,
            _write("snap", created_at=_NOW - timedelta(days=120)),
            "snap-write",
        )
        old = await _append(
            store,
            _write("held", created_at=_NOW - timedelta(days=120)),
            "held-write",
        )
        await store.create_snapshot(
            namespace="raw",
            snapshot_id="raw-freeze",
            operation="freeze",
            idempotency_key="freeze-key",
        )
        hold = DeletionHold(
            hold_id="incident-1",
            target_refs=[_ref(old.record)],
            reason="incident investigation",
        )
        await _append(
            store,
            _write(
                "hold",
                namespace="controls",
                record_type="retention-hold",
                created_at=_NOW - timedelta(days=1),
                payload=hold.model_dump(mode="json"),
            ),
            "hold-write",
        )
        referenced = await _append(
            store,
            _write("referenced", created_at=_NOW - timedelta(days=120)),
            "referenced-write",
        )
        source = await _append(
            store,
            _write(
                "source",
                namespace="derived",
                created_at=_NOW - timedelta(days=1),
                payload={
                    "provenance": [
                        {
                            "artefact_id": referenced.record.record_id,
                            "content_hash": referenced.record.content_hash,
                        }
                    ]
                },
            ),
            "source-write",
        )
        del source
        manager = await _manager(store, authorization.record)
        plan = await manager.create_plan(request_id="protect")
        assert not plan.candidates
        blocked = {item.ref.record_id: " ".join(item.reasons) for item in plan.blocked}
        assert "snapshot member" in blocked["snap"]
        assert "active retention hold" in blocked["held"]
        assert "provenance" in blocked["referenced"]

    _run(scenario())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_provenance_chain_blocks_transitive_raw_candidates(backend: str, tmp_path):
    async def scenario():
        store = _store(backend, tmp_path)
        authorization = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=_NOW - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        base = await _append(
            store,
            _write("base", created_at=_NOW - timedelta(days=120)),
            "base-write",
        )
        derived = await _append(
            store,
            _write(
                "derived",
                created_at=_NOW - timedelta(days=120),
                payload={
                    "provenance": [
                        {
                            "record_id": base.record.record_id,
                            "content_hash": base.record.content_hash,
                        }
                    ]
                },
            ),
            "derived-write",
        )
        await _append(
            store,
            _write(
                "live-source",
                created_at=_NOW - timedelta(days=1),
                namespace="derived",
                payload={
                    "provenance": [
                        {
                            "record_id": derived.record.record_id,
                            "content_hash": derived.record.content_hash,
                        }
                    ]
                },
            ),
            "live-source-write",
        )
        await _append(
            store,
            _write("unknown-hash", created_at=_NOW - timedelta(days=120)),
            "unknown-hash-write",
        )
        await _append(
            store,
            _write(
                "unknown-source",
                created_at=_NOW - timedelta(days=1),
                namespace="derived",
                payload={"record_id": "unknown-hash"},
            ),
            "unknown-source-write",
        )
        manager = await _manager(store, authorization.record)
        plan = await manager.create_plan(request_id="chain")
        assert not plan.candidates
        blocked = {item.ref.record_id: " ".join(item.reasons) for item in plan.blocked}
        assert "provenance" in blocked["base"]
        assert "provenance" in blocked["derived"]
        assert "ambiguous-provenance" in blocked["unknown-hash"]

    _run(scenario())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_sealed_plan_rejects_stale_inventory_before_mutation(backend: str, tmp_path):
    async def scenario():
        store = _store(backend, tmp_path)
        authorization = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=_NOW - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        old = await _append(
            store,
            _write("old", created_at=_NOW - timedelta(days=120)),
            "old-write",
        )
        manager = await _manager(store, authorization.record)
        plan = await manager.create_plan(request_id="stale")
        await _append(
            store,
            _write("new-old", created_at=_NOW - timedelta(days=120)),
            "new-old-write",
        )
        with pytest.raises(MemoryConflictError, match="stale"):
            await manager.apply(plan, idempotency_key="stale-key")
        assert await store.get(namespace="raw", record_id="old") == old.record

    _run(scenario())


def test_authorization_payload_is_policy_bound_and_retained_until_is_honored():
    async def scenario():
        store = InMemoryStructuredStore()
        authorization = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=_NOW - timedelta(days=300),
                payload={"approved": True},
            ),
            "owner-write",
        )
        await _append(
            store,
            _write(
                "retained",
                created_at=_NOW - timedelta(days=120),
                payload={"retained_until": (_NOW + timedelta(days=1)).isoformat()},
            ),
            "retained-write",
        )
        manager = await _manager(store, authorization.record)
        with pytest.raises(MemoryPermanentError, match="authorization payload"):
            await manager.create_plan(request_id="bad-owner")

        valid_store = InMemoryStructuredStore()
        valid_owner = await _append(
            valid_store,
            _write(
                "owner-2",
                namespace="controls",
                record_type="retention-authorization",
                created_at=_NOW - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-2-write",
        )
        manager = await _manager(valid_store, valid_owner.record)
        await _append(
            valid_store,
            _write(
                "retained",
                created_at=_NOW - timedelta(days=120),
                payload={"retained_until": (_NOW + timedelta(days=1)).isoformat()},
            ),
            "retained-write",
        )
        plan = await manager.create_plan(request_id="retained")
        assert [item.ref.record_id for item in plan.candidates] == []
        assert any(
            item.ref.record_id == "retained"
            and any(reason.startswith("retained until") for reason in item.reasons)
            for item in plan.blocked
        )

    _run(scenario())


def test_snapshot_retirement_requires_external_binding_attestation_and_holds_allow_empty_records():
    snapshot = SnapshotRef(
        namespace="raw",
        snapshot_id="freeze",
        created_at=_NOW - timedelta(days=120),
        content_hash="a" * 64,
        members=[],
    )
    payload = _authorization_payload(retire_snapshot_refs=[snapshot])
    payload.pop("no_active_external_bindings_attested")
    with pytest.raises(ValueError, match="external-bindings attestation"):
        DeletionAuthorization.model_validate(payload)
    hold = DeletionHold(
        hold_id="snapshot-only",
        target_refs=[],
        snapshot_refs=[snapshot],
        reason="retain frozen export",
    )
    assert hold.target_refs == []


@pytest.mark.parametrize("control", ["hold", "authorization", "tombstone"])
def test_nested_forward_minor_control_refs_are_not_discarded(control: str):
    async def scenario():
        store = InMemoryStructuredStore()
        owner = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=_NOW - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        hidden = await _append(
            store,
            _write("hidden", created_at=_NOW - timedelta(days=120)),
            "hidden-write",
        )
        hidden_ref = {
            "record_id": hidden.record.record_id,
            "content_hash": hidden.record.content_hash,
        }
        if control == "hold":
            anchor = await _append(
                store,
                _write("anchor", created_at=_NOW - timedelta(days=120)),
                "anchor-write",
            )
            hold = DeletionHold(
                hold_id="nested-hold",
                target_refs=[_ref(anchor.record)],
                reason="nested forward-minor regression",
            ).model_dump(mode="json")
            hold["target_refs"][0]["schema_version"] = "1.1"
            hold["target_refs"][0]["hidden_ref"] = hidden_ref
            payload = hold
            record_type = "retention-hold"
            record_id = "nested-hold"
        elif control == "authorization":
            snapshot = SnapshotRef(
                namespace="raw",
                snapshot_id="nested-freeze",
                created_at=_NOW - timedelta(days=120),
                content_hash="a" * 64,
                members=[],
            ).model_dump(mode="json")
            snapshot["schema_version"] = "1.1"
            snapshot["hidden_ref"] = hidden_ref
            payload = _authorization_payload()
            payload["retire_snapshot_refs"] = [snapshot]
            payload["no_active_external_bindings_attested"] = True
            record_type = "retention-authorization"
            record_id = "nested-authorization"
        else:
            payload = {
                "target": {
                    "namespace": "raw",
                    "record_id": "retired",
                    "content_hash": "b" * 64,
                    "schema_version": "1.1",
                    "hidden_ref": hidden_ref,
                },
                "deleted_at": _NOW.isoformat(),
                "plan_id": "c" * 64,
                "reason": "nested forward-minor regression",
            }
            record_type = "memory-deletion-tombstone"
            record_id = "nested-tombstone"
        await _append(
            store,
            _write(
                record_id,
                namespace="controls",
                record_type=record_type,
                created_at=_NOW - timedelta(days=1),
                payload=payload,
            ),
            f"{record_id}-write",
        )
        manager = await _manager(store, owner.record)
        if control in {"hold", "authorization", "tombstone"}:
            with pytest.raises(MemoryPermanentError, match="invalid"):
                await manager.create_plan(request_id=f"nested-{control}")
        else:
            plan = await manager.create_plan(request_id=f"nested-{control}")
            blocked = {item.ref.record_id: " ".join(item.reasons) for item in plan.blocked}
            assert "hidden" in blocked
            assert "provenance" in blocked["hidden"]

    _run(scenario())


def test_deletion_cli_seals_then_applies_the_saved_plan(tmp_path, capsys):
    from uptick_agent.memory_deletion_cli import _main, _parser, _write_sealed_plan

    async def seed():
        store = SqliteStructuredStore(tmp_path / "cli.sqlite")
        await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=_NOW - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        await _append(
            store,
            _write("old", created_at=_NOW - timedelta(days=120)),
            "old-write",
        )

    _run(seed())
    plan_path = tmp_path / "sealed-plan.json"
    dry_args = _parser().parse_args(
        [
            "--sqlite-path",
            str(tmp_path / "cli.sqlite"),
            "--plan-path",
            str(plan_path),
            "--request-id",
            "cli-request",
            "--authorization-namespace",
            "controls",
            "--authorization-id",
            "owner",
            "--policy-id",
            "test-retention",
        ]
    )
    _run(_main(dry_args))
    assert plan_path.exists()
    assert "secret" not in plan_path.read_text()
    sealed = PhysicalDeletionPlan.model_validate_json(plan_path.read_text())
    changed = sealed.model_copy(update={"request_id": "different-request"})
    changed = changed.model_copy(update={"plan_id": plan_hash(changed)})
    with pytest.raises(MemoryConflictError, match="different sealed plan"):
        _write_sealed_plan(plan_path, changed)
    assert PhysicalDeletionPlan.model_validate_json(plan_path.read_text()) == sealed
    apply_args = _parser().parse_args(
        [
            "--sqlite-path",
            str(tmp_path / "cli.sqlite"),
            "--plan-path",
            str(plan_path),
            "--request-id",
            "cli-request",
            "--apply",
        ]
    )
    _run(_main(apply_args))
    reopened = SqliteStructuredStore(tmp_path / "cli.sqlite")
    assert _run(reopened.get(namespace="raw", record_id="old")) is None
    assert capsys.readouterr().out


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_explicit_snapshot_retirement_preserves_metadata_and_blocks_recreation(
    backend: str, tmp_path
):
    async def scenario():
        store = _store(backend, tmp_path)
        await _append(
            store,
            _write("old", created_at=_NOW),
            "old-write",
        )
        frozen = await store.create_snapshot(
            namespace="raw",
            snapshot_id="raw-freeze",
            operation="freeze",
            idempotency_key="freeze-key",
        )
        # Snapshot creation uses the store clock.  Age the immutable metadata
        # in this isolated fixture so the real 90-day policy can be exercised
        # without waiting; membership and its content hash remain unchanged.
        retire_now = datetime.now(UTC)
        aged_at = retire_now - timedelta(days=120)
        aged_snapshot = frozen.snapshot.model_copy(update={"created_at": aged_at})
        if backend == "memory":
            store._snapshots[aged_snapshot.snapshot_id] = aged_snapshot
            receipt_key = ("raw", "freeze", "freeze-key")
            store._receipts[receipt_key] = frozen.model_copy(update={"snapshot": aged_snapshot})
        else:
            with sqlite3.connect(tmp_path / "memory.sqlite") as connection:
                connection.execute(
                    "UPDATE memory_snapshots SET created_at = ? WHERE snapshot_id = ?",
                    (aged_at.isoformat(), aged_snapshot.snapshot_id),
                )
                connection.execute(
                    "UPDATE memory_operation_receipts SET receipt_json = ? "
                    "WHERE namespace = 'raw' AND operation = 'freeze' "
                    "AND idempotency_key = 'freeze-key'",
                    (frozen.model_copy(update={"snapshot": aged_snapshot}).model_dump_json(),),
                )
        snapshot_ref = SnapshotRef(
            namespace=aged_snapshot.namespace,
            snapshot_id=aged_snapshot.snapshot_id,
            created_at=aged_snapshot.created_at,
            content_hash=aged_snapshot.content_hash,
            members=aged_snapshot.members,
        )
        authorization = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=_NOW,
                payload=_authorization_payload(retire_snapshot_refs=[snapshot_ref]),
            ),
            "owner-write",
        )
        manager = await _manager(store, authorization.record, now=retire_now)
        plan = await manager.create_plan(request_id="retire")
        assert [item.snapshot.snapshot_id for item in plan.retired_snapshots] == ["raw-freeze"]
        assert [item.ref.record_id for item in plan.candidates] == ["old"]
        result = await manager.apply(plan, idempotency_key="retire-key")
        assert result.retired_snapshots == [snapshot_ref]
        assert len(result.snapshot_tombstone_ids) == 1
        assert await store.get_snapshot(snapshot_id="raw-freeze") is None
        assert await store.get(namespace="raw", record_id="old") is None
        inventory = await store.deletion_inventory()
        assert inventory.deleted_snapshots == (snapshot_ref,)
        snapshot_marker = next(
            entry for entry in inventory.receipts if entry.idempotency_key == "freeze-key"
        )
        assert snapshot_marker.receipt_kind == "deleted_snapshot"
        assert snapshot_marker.snapshot_ref == snapshot_ref
        with pytest.raises(MemoryConflictError, match="snapshot was retired"):
            await store.create_snapshot(
                namespace="raw",
                snapshot_id="raw-freeze",
                operation="freeze",
                idempotency_key="freeze-recreated",
            )
        with pytest.raises(MemoryConflictError, match="snapshot was retired"):
            await store.create_snapshot(
                namespace="other",
                snapshot_id="raw-freeze",
                operation="freeze",
                idempotency_key="freeze-other-namespace",
            )
        replay = await manager.apply(plan, idempotency_key="retire-key")
        assert replay.already_applied is True
        with pytest.raises(MemoryConflictError, match="another idempotency key"):
            await store.apply_deletion(plan, idempotency_key="retire-again")
        if backend == "sqlite":
            reopened = SqliteStructuredStore(tmp_path / "memory.sqlite")
            assert await reopened.get_snapshot(snapshot_id="raw-freeze") is None
            reopened_inventory = await reopened.deletion_inventory()
            assert reopened_inventory.deleted_snapshots == (snapshot_ref,)
            assert await reopened.get(namespace="raw", record_id="old") is None

    _run(scenario())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_store_rejects_resealed_retirement_with_live_snapshot_binding(backend: str, tmp_path):
    async def scenario():
        store = _store(backend, tmp_path)
        frozen = await store.create_snapshot(
            namespace="raw",
            snapshot_id="bound-freeze",
            operation="freeze",
            idempotency_key="bound-freeze-key",
        )
        now = datetime.now(UTC)
        aged_snapshot = frozen.snapshot.model_copy(update={"created_at": now - timedelta(days=120)})
        if backend == "memory":
            store._snapshots[aged_snapshot.snapshot_id] = aged_snapshot
            store._receipts[("raw", "freeze", "bound-freeze-key")] = frozen.model_copy(
                update={"snapshot": aged_snapshot}
            )
        else:
            with sqlite3.connect(tmp_path / "memory.sqlite") as connection:
                connection.execute(
                    "UPDATE memory_snapshots SET created_at = ? WHERE snapshot_id = ?",
                    (aged_snapshot.created_at.isoformat(), aged_snapshot.snapshot_id),
                )
                connection.execute(
                    "UPDATE memory_operation_receipts SET receipt_json = ? "
                    "WHERE namespace = 'raw' AND operation = 'freeze' "
                    "AND idempotency_key = 'bound-freeze-key'",
                    (frozen.model_copy(update={"snapshot": aged_snapshot}).model_dump_json(),),
                )
        snapshot_ref = SnapshotRef(
            namespace=aged_snapshot.namespace,
            snapshot_id=aged_snapshot.snapshot_id,
            created_at=aged_snapshot.created_at,
            content_hash=aged_snapshot.content_hash,
            members=aged_snapshot.members,
        )
        authorization = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=now - timedelta(days=120),
                payload=_authorization_payload(retire_snapshot_refs=[snapshot_ref]),
            ),
            "bound-owner-write",
        )
        await _append(
            store,
            _write(
                "live-binding",
                namespace="derived",
                created_at=now - timedelta(days=1),
                payload={"snapshot_ref": snapshot_ref.model_dump(mode="json")},
            ),
            "live-binding-write",
        )
        manager = await _manager(store, authorization.record, now=now)
        plan = await manager.create_plan(request_id="bound")
        assert not plan.retired_snapshots
        assert plan.blocked_snapshots
        snapshot_entry = next(
            entry
            for entry in (await store.deletion_inventory()).receipts
            if entry.idempotency_key == "bound-freeze-key"
        )
        retirement = SnapshotRetirement(
            snapshot=snapshot_ref,
            eligible_at=aged_snapshot.created_at + timedelta(days=90),
            reason="forged resealed retirement",
        )
        forged = plan.model_copy(
            update={
                "blocked_snapshots": [],
                "retired_snapshots": [retirement],
                "purge_receipts": [snapshot_entry],
            }
        )
        forged = forged.model_copy(update={"plan_id": plan_hash(forged)})
        with pytest.raises(MemoryConflictError, match="live or ambiguous payload"):
            await store.apply_deletion(forged, idempotency_key="bound-forged-key")
        assert await store.get_snapshot(snapshot_id="bound-freeze") is not None

    _run(scenario())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_retired_snapshot_metadata_does_not_protect_later_other_namespace_raw_record(
    backend: str, tmp_path
):
    async def scenario():
        store = _store(backend, tmp_path)
        frozen = await store.create_snapshot(
            namespace="raw",
            snapshot_id="old-freeze",
            operation="freeze",
            idempotency_key="old-freeze-key",
        )
        now = datetime.now(UTC)
        aged_snapshot = frozen.snapshot.model_copy(update={"created_at": now - timedelta(days=120)})
        if backend == "memory":
            store._snapshots[aged_snapshot.snapshot_id] = aged_snapshot
            store._receipts[("raw", "freeze", "old-freeze-key")] = frozen.model_copy(
                update={"snapshot": aged_snapshot}
            )
        else:
            with sqlite3.connect(tmp_path / "memory.sqlite") as connection:
                connection.execute(
                    "UPDATE memory_snapshots SET created_at = ? WHERE snapshot_id = ?",
                    (aged_snapshot.created_at.isoformat(), aged_snapshot.snapshot_id),
                )
                connection.execute(
                    "UPDATE memory_operation_receipts SET receipt_json = ? "
                    "WHERE namespace = 'raw' AND operation = 'freeze' "
                    "AND idempotency_key = 'old-freeze-key'",
                    (frozen.model_copy(update={"snapshot": aged_snapshot}).model_dump_json(),),
                )
        snapshot_ref = SnapshotRef(
            namespace=aged_snapshot.namespace,
            snapshot_id=aged_snapshot.snapshot_id,
            created_at=aged_snapshot.created_at,
            content_hash=aged_snapshot.content_hash,
            members=aged_snapshot.members,
        )
        first_owner = await _append(
            store,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=now - timedelta(days=120),
                payload=_authorization_payload(retire_snapshot_refs=[snapshot_ref]),
            ),
            "first-owner-write",
        )
        manager = await _manager(store, first_owner.record, now=now)
        plan = await manager.create_plan(request_id="retire-old")
        await manager.apply(plan, idempotency_key="retire-old-key")

        second_owner = await _append(
            store,
            _write(
                "second-owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=now - timedelta(days=120),
                payload=_authorization_payload(),
            ),
            "second-owner-write",
        )
        later = await _append(
            store,
            _write(
                "old",
                namespace="other",
                created_at=now - timedelta(days=120),
            ),
            "later-write",
        )
        later_plan = await (await _manager(store, second_owner.record, now=now)).create_plan(
            request_id="retire-later"
        )
        assert [item.ref.record_id for item in later_plan.candidates] == [later.record.record_id]
        await (await _manager(store, second_owner.record, now=now)).apply(
            later_plan, idempotency_key="retire-later-key"
        )
        assert await store.get(namespace="other", record_id="old") is None

    _run(scenario())


def test_sqlite_deletion_transaction_rolls_back_after_injected_late_failure(tmp_path):
    class FailingStore(SqliteStructuredStore):
        inject_failure = False

        def _transaction(self, work):
            if not self.inject_failure:
                return super()._transaction(work)

            def fail_after_work(connection):
                work(connection)
                raise MemoryPermanentError("injected post-mutation failure")

            return super()._transaction(fail_after_work)

    async def scenario():
        path = tmp_path / "rollback.sqlite"
        seed = SqliteStructuredStore(path)
        authorization = await _append(
            seed,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=_NOW - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        old = await _append(
            seed,
            _write("old", created_at=_NOW - timedelta(days=120)),
            "old-write",
        )
        failing = FailingStore(path)
        await failing._ensure_initialized()
        failing.inject_failure = True
        manager = await _manager(failing, authorization.record)
        plan = await manager.create_plan(request_id="rollback")
        with pytest.raises(MemoryPermanentError, match="injected"):
            await manager.apply(plan, idempotency_key="rollback-key")
        reopened = SqliteStructuredStore(path)
        assert await reopened.get(namespace="raw", record_id="old") == old.record
        assert not (await reopened.deletion_inventory()).deleted_records

    _run(scenario())


def test_sqlite_concurrent_deletion_applies_are_serialized(tmp_path):
    async def scenario():
        path = tmp_path / "concurrent.sqlite"
        seed = SqliteStructuredStore(path)
        authorization = await _append(
            seed,
            _write(
                "owner",
                namespace="controls",
                record_type="retention-authorization",
                created_at=_NOW - timedelta(days=300),
                payload=_authorization_payload(),
            ),
            "owner-write",
        )
        await _append(
            seed,
            _write("old", created_at=_NOW - timedelta(days=120)),
            "old-write",
        )
        plan = await (await _manager(seed, authorization.record)).create_plan(
            request_id="concurrent"
        )
        first = SqliteStructuredStore(path)
        second = SqliteStructuredStore(path)
        outcomes = await asyncio.gather(
            first.apply_deletion(plan, idempotency_key="concurrent-first"),
            second.apply_deletion(plan, idempotency_key="concurrent-second"),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if not isinstance(item, Exception)]
        failures = [item for item in outcomes if isinstance(item, Exception)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], (MemoryConflictError, MemoryTransientError))
        reopened = SqliteStructuredStore(path)
        assert await reopened.get(namespace="raw", record_id="old") is None
        assert len((await reopened.deletion_inventory()).deleted_records) == 1

    _run(scenario())
