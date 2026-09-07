from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from uptick_agent.memory.config import MemoryConfiguration, ModuleConfig
from uptick_agent.memory.contracts import (
    ConsolidationRequest,
    ContextItem,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryContribution,
    ProvenanceRef,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.maintenance import (
    MaintenanceDelta,
    MaintenanceRetrievalView,
    MemoryMaintenance,
    RetentionHold,
)
from uptick_agent.memory.orchestrator import MemoryModuleRegistration, MemoryOrchestrator
from uptick_agent.memory.stores import InMemoryStructuredStore, RecordWrite
from uptick_agent.memory.stores.contracts import SnapshotMember, StoredRecord

_START = datetime(2026, 1, 1, tzinfo=UTC)
_NOW = datetime(2026, 4, 2, tzinfo=UTC)


def _run(awaitable):
    return asyncio.run(awaitable)


def _store() -> InMemoryStructuredStore:
    return InMemoryStructuredStore()


def _write(
    record_id: str,
    *,
    record_type: str = "generic-evidence",
    payload: dict[str, object] | None = None,
    created_at: datetime = _START,
) -> RecordWrite:
    return RecordWrite(
        namespace="toy",
        record_id=record_id,
        record_type=record_type,
        payload=payload or {"value": "same"},
        created_at=created_at,
    )


def _member(record: StoredRecord) -> SnapshotMember:
    return SnapshotMember(record_id=record.record_id, content_hash=record.content_hash)


def _custom_link(records: tuple[StoredRecord, ...]):
    members = [_member(record) for record in records]
    yield MaintenanceDelta(
        delta_id="link-all",
        operation="link",
        target_record_id=records[0].record_id,
        target_payload={"reason": "same toy value"},
        source_members=members,
        provenance=[
            ProvenanceRef(artefact_id=member.record_id, content_hash=member.content_hash)
            for member in members
        ],
    )


def _snapshot(store: InMemoryStructuredStore, *, records: list[RecordWrite]) -> object:
    for index, write in enumerate(records):
        _run(store.append(write, operation="seed", idempotency_key=f"seed-{index}"))
    return _run(
        store.create_snapshot(
            namespace="toy",
            snapshot_id="snapshot-1",
            operation="freeze",
            idempotency_key="freeze-1",
        )
    ).snapshot


def test_default_plan_is_deterministic_and_extractive_for_generic_toy_data() -> None:
    store = _store()
    snapshot = _snapshot(
        store,
        records=[
            _write("older", payload={"value": "same"}),
            _write(
                "newer",
                payload={"value": "same"},
                created_at=_START + timedelta(seconds=1),
            ),
            _write(
                "episode",
                record_type="experience-transition",
                payload={
                    "run_id": "run-1",
                    "iteration": 1,
                    "observation": {"status": "ok", "blob": "x" * 2_000},
                    "action": {"kind": "inspect"},
                    "result": {"ok": True},
                    "terminal": False,
                    "private_model_claim": "must not appear in summary",
                },
            ),
        ],
    )
    maintenance = MemoryMaintenance(store, namespace="toy", clock=lambda: _NOW)
    first = _run(maintenance.create_plan(snapshot.snapshot_id, request_id="dry-run"))
    second = _run(maintenance.create_plan(snapshot.snapshot_id, request_id="dry-run"))

    assert first == second
    assert first.snapshot_content_hash == snapshot.content_hash
    assert first.snapshot_members == snapshot.members
    assert {delta.operation for delta in first.deltas} >= {"link", "supersede", "summary"}
    link = next(delta for delta in first.deltas if delta.operation == "link")
    assert link.target_record_id == "older"
    summary = next(delta for delta in first.deltas if delta.operation == "summary")
    assert summary.target_payload["source_record_id"] == "episode"
    assert "private_model_claim" not in summary.target_payload
    assert summary.target_payload["observation"]["_omitted"] is True
    assert len(str(summary.target_payload["observation"])) < 200
    assert summary.target_payload["provenance"][0]["artefact_id"] == "episode"
    assert first.unsupported_operations == ["physical_delete"]


def test_dedup_preserves_nested_toy_environment_identity() -> None:
    store = _store()
    snapshot = _snapshot(
        store,
        records=[
            _write("server-a", payload={"action": {"target": {"id": "server-A"}}}),
            _write(
                "server-b",
                payload={"action": {"target": {"id": "server-B"}}},
                created_at=_START + timedelta(seconds=1),
            ),
        ],
    )
    maintenance = MemoryMaintenance(store, namespace="toy", clock=lambda: _NOW)

    plan = _run(maintenance.create_plan(snapshot.snapshot_id, request_id="nested-identity"))

    assert plan.deltas == []


def test_custom_callback_proposals_are_hash_bound_to_snapshot_and_provenance() -> None:
    store = _store()
    snapshot = _snapshot(
        store,
        records=[_write("a"), _write("b", created_at=_START + timedelta(seconds=1))],
    )
    maintenance = MemoryMaintenance(store, namespace="toy", clock=lambda: _NOW)
    plan = _run(
        maintenance.create_plan(
            snapshot.snapshot_id,
            request_id="custom",
            callback_id="toy-link-v1",
            proposal_callback=_custom_link,
        )
    )
    assert len(plan.deltas) == 1
    assert {ref.artefact_id for ref in plan.deltas[0].provenance} == {"a", "b"}
    assert plan.deltas[0].source_members == snapshot.members


def test_apply_is_separate_idempotent_and_keeps_source_archive() -> None:
    store = _store()
    snapshot = _snapshot(store, records=[_write("a"), _write("b")])
    maintenance = MemoryMaintenance(store, namespace="toy", clock=lambda: _NOW)
    plan = _run(maintenance.create_plan(snapshot.snapshot_id, request_id="apply"))

    first = _run(maintenance.apply(plan, idempotency_key="apply-1", active_holds=()))
    repeat = _run(maintenance.apply(plan, idempotency_key="apply-1", active_holds=()))
    assert first.applied is True
    assert first.already_applied is False
    assert repeat.already_applied is True
    assert first.application_id == repeat.application_id
    assert first.supported_operations == ["supersede"]
    assert any("manifest-only operations" in warning for warning in first.warnings)
    assert len(_run(store.list(namespace="toy"))) == 2
    assert len(_run(store.list(namespace=maintenance.maintenance_namespace))) == 1


def test_consolidation_participant_persists_dry_run_then_applies_that_exact_plan() -> None:
    store = _store()
    snapshot = _snapshot(store, records=[_write("a"), _write("b")])
    maintenance = MemoryMaintenance(store, namespace="toy", clock=lambda: _NOW)
    dry_run_request = ConsolidationRequest(
        request_id="consolidate",
        snapshot_id=snapshot.snapshot_id,
        idempotency_key="consolidate-1",
        dry_run=True,
    )
    dry_run = _run(maintenance.consolidate(dry_run_request))
    assert dry_run.applied is False
    persisted = _run(store.list(namespace=maintenance.maintenance_namespace))
    assert [record.record_type for record in persisted] == ["memory-maintenance-plan"]

    apply_request = dry_run_request.model_copy(update={"dry_run": False})
    applied = _run(maintenance.consolidate(apply_request))
    repeated = _run(maintenance.consolidate(apply_request))
    assert applied.applied is True
    assert repeated.applied is True
    assert len(_run(store.list(namespace=maintenance.maintenance_namespace))) == 2


def test_stale_source_is_rejected_before_apply() -> None:
    store = _store()
    snapshot = _snapshot(store, records=[_write("a"), _write("b")])
    maintenance = MemoryMaintenance(store, namespace="toy", clock=lambda: _NOW)
    plan = _run(maintenance.create_plan(snapshot.snapshot_id, request_id="stale"))
    store._records[("toy", "a")] = StoredRecord.from_write(
        _write("a", payload={"value": "changed"})
    )

    with pytest.raises(MemoryConflictError, match="stale"):
        _run(maintenance.apply(plan, idempotency_key="stale-1", active_holds=()))


def test_holds_and_active_candidate_provenance_block_deltas_and_protect_retention() -> None:
    store = _store()
    first_receipt = _run(store.append(_write("source"), operation="seed", idempotency_key="source"))
    source = first_receipt.record
    _run(
        store.append(
            _write(
                "candidate",
                record_type="lesson",
                payload={
                    "status": "candidate",
                    "provenance": [
                        {
                            "artefact_id": source.record_id,
                            "content_hash": source.content_hash,
                        }
                    ],
                },
            ),
            operation="seed",
            idempotency_key="candidate",
        )
    )
    snapshot = _run(
        store.create_snapshot(
            namespace="toy",
            snapshot_id="snapshot-1",
            operation="freeze",
            idempotency_key="freeze-1",
        )
    ).snapshot
    maintenance = MemoryMaintenance(store, namespace="toy", clock=lambda: _NOW)

    def callback(records):
        member = _member(records[0])
        yield MaintenanceDelta(
            delta_id="source-link",
            operation="link",
            target_record_id=member.record_id,
            target_payload={},
            source_members=[member],
            provenance=[
                ProvenanceRef(
                    artefact_id=member.record_id,
                    content_hash=member.content_hash,
                )
            ],
        )

    plan = _run(
        maintenance.create_plan(
            snapshot.snapshot_id,
            request_id="hold",
            proposal_callback=callback,
            active_holds=[RetentionHold(hold_id="incident-1", artefact_ids=["candidate"])],
        )
    )
    source_entry = next(entry for entry in plan.retention_entries if entry.record_id == "source")
    assert plan.deltas == []
    assert plan.blocked_delta_ids == ["source-link"]
    assert "candidate:candidate" in source_entry.protected_by
    assert "active candidate provenance is outside" not in " ".join(plan.warnings)


def test_retention_and_snapshot_expiration_use_injected_clock() -> None:
    store = _store()
    snapshot = _snapshot(
        store,
        records=[
            _write("raw"),
            _write("summary", record_type="summary"),
            _write("memory-summary", record_type="memory-summary"),
            _write(
                "world-batch",
                record_type="future-batch",
                payload={
                    "retention_class": "project_lifetime",
                    "retention_policy_ref": "simulator-audit-retention-v1@1.0",
                },
            ),
        ],
    )
    maintenance = MemoryMaintenance(store, namespace="toy", clock=lambda: _NOW)
    plan = _run(maintenance.create_plan(snapshot.snapshot_id, request_id="retention"))

    raw = next(entry for entry in plan.retention_entries if entry.record_id == "raw")
    lifetime = next(entry for entry in plan.retention_entries if entry.record_id == "summary")
    memory_lifetime = next(
        entry for entry in plan.retention_entries if entry.record_id == "memory-summary"
    )
    assert raw.retained_until == _NOW + timedelta(days=90)
    assert raw.retained_until > _NOW
    assert lifetime.retained_until is None
    assert memory_lifetime.retained_until is None
    world_lifetime = next(
        entry for entry in plan.retention_entries if entry.record_id == "world-batch"
    )
    assert world_lifetime.retained_until is None
    assert plan.snapshot_retained_until == max(snapshot.created_at, _NOW) + timedelta(days=90)


def test_apply_timestamp_is_actual_and_stable_across_replay() -> None:
    store = _store()
    snapshot = _snapshot(store, records=[_write("old")])
    clock_value = [_NOW]
    maintenance = MemoryMaintenance(store, namespace="toy", clock=lambda: clock_value[0])
    plan = _run(maintenance.create_plan(snapshot.snapshot_id, request_id="apply-time"))

    apply_time = _NOW + timedelta(days=1)
    clock_value[0] = apply_time
    first = _run(maintenance.apply(plan, idempotency_key="apply-time-1", active_holds=()))
    assert not first.already_applied
    record = _run(store.get(namespace="toy:maintenance", record_id=first.application_id))
    assert record is not None
    assert record.payload["applied_at"] == apply_time.isoformat()

    clock_value[0] = _NOW + timedelta(days=2)
    replay = _run(maintenance.apply(plan, idempotency_key="apply-time-1", active_holds=()))
    assert replay.already_applied
    replay_record = _run(store.get(namespace="toy:maintenance", record_id=replay.application_id))
    assert replay_record is not None
    assert replay_record.payload["applied_at"] == apply_time.isoformat()


def test_operational_view_hides_applied_supersession_and_decays_age() -> None:
    store = _store()
    snapshot = _snapshot(
        store,
        records=[_write("older"), _write("duplicate", created_at=_START + timedelta(seconds=1))],
    )
    maintenance = MemoryMaintenance(store, namespace="toy", clock=lambda: _NOW)
    plan = _run(maintenance.create_plan(snapshot.snapshot_id, request_id="view"))
    _run(maintenance.apply(plan, idempotency_key="view-1", active_holds=()))

    def item(record_id: str, score: float) -> ContextItem:
        return ContextItem(
            envelope=UntrustedMemoryEnvelope(
                item_id=record_id,
                artefact_type="toy-record",
                origin_module="toy",
                origin_version="1.0",
                trust_classification="external_untrusted",
                provenance=[ProvenanceRef(artefact_id=record_id, content_hash="a" * 64)],
                item={"value": "same"},
            ),
            score=score,
            selection_reason="baseline",
            estimated_tokens=1,
        )

    view = MaintenanceRetrievalView(
        store,
        namespace="toy",
        clock=lambda: _NOW,
        decay_days=90,
    )
    result = _run(view.transform([item("duplicate", 1.0), item("older", 1.0)]))
    assert [candidate.envelope.item_id for candidate in result] == ["older"]
    assert result[0].score < 1.0
    assert result[0].envelope.trust_classification == "external_untrusted"


@dataclass
class _SourceContributor:
    contribution: MemoryContribution

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        return self.contribution


def test_applied_supersession_changes_orchestrated_decision_context() -> None:
    store = _store()
    snapshot = _snapshot(
        store,
        records=[
            _write("older", payload={"value": "same"}),
            _write(
                "duplicate",
                payload={"value": "same"},
                created_at=_START + timedelta(seconds=1),
            ),
        ],
    )

    def item(record_id: str) -> ContextItem:
        return ContextItem(
            envelope=UntrustedMemoryEnvelope(
                item_id=record_id,
                artefact_type="toy-record",
                origin_module="episodic",
                origin_version="1.0",
                trust_classification="external_untrusted",
                provenance=[ProvenanceRef(artefact_id=record_id, content_hash="a" * 64)],
                item={"value": "same"},
            ),
            score=1.0,
            selection_reason="source",
            estimated_tokens=1,
        )

    contributor = _SourceContributor(
        MemoryContribution(
            module_id="episodic",
            module_version="1.0",
            items=[item("older"), item("duplicate")],
        )
    )
    maintenance = MemoryMaintenance(store, namespace="toy", clock=lambda: _NOW)
    view = MaintenanceRetrievalView(store, namespace="toy", clock=lambda: _NOW)
    configuration = MemoryConfiguration(
        compatibility_legacy=ModuleConfig(enabled=False),
        episodic=ModuleConfig(enabled=True),
        consolidation=ModuleConfig(enabled=True),
    )
    orchestrator = MemoryOrchestrator(
        configuration,
        [
            MemoryModuleRegistration("episodic", lambda _: contributor, retrieval_strategy=view),
            MemoryModuleRegistration("consolidation", lambda _: maintenance),
        ],
    )
    request = MemoryContextRequest(request_id="context", run_id="run")

    before = _run(orchestrator.build_context(request))
    assert {candidate.envelope.item_id for candidate in before.items} == {"older", "duplicate"}

    consolidation_request = ConsolidationRequest(
        request_id="maintenance",
        snapshot_id=snapshot.snapshot_id,
        idempotency_key="maintenance-1",
        dry_run=True,
    )
    _run(orchestrator.consolidate(consolidation_request))
    _run(orchestrator.consolidate(consolidation_request.model_copy(update={"dry_run": False})))

    after = _run(orchestrator.build_context(request))
    assert [candidate.envelope.item_id for candidate in after.items] == ["older"]


def test_retention_policy_cannot_remove_mandated_lifetime_types() -> None:
    from uptick_agent.memory.maintenance import MaintenanceRetentionPolicy

    with pytest.raises(ValueError, match="cannot omit mandated"):
        MaintenanceRetentionPolicy(project_lifetime_record_types=["summary"])
