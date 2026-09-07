from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryContextRequest,
    MemoryPermanentError,
    MemoryValidationError,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.patterns import PatternQuerySettings
from uptick_agent.memory.stores import InMemoryStructuredStore, SqliteStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite, SnapshotMember, StoredRecord
from uptick_agent.memory.world_model import WorldModelMemory
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_TIME = datetime(2026, 9, 5, 12, tzinfo=UTC)
_SETTINGS = PatternQuerySettings(
    scope_paths=("observation.state",),
    action_path="action.kind",
    result_path="result.ok",
)


class _CountingStore(InMemoryStructuredStore):
    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0
        self.list_namespaces: list[str] = []

    async def get(self, *, namespace: str, record_id: str):
        self.get_calls += 1
        return await super().get(namespace=namespace, record_id=record_id)

    async def list(self, *, namespace: str):
        self.list_namespaces.append(namespace)
        return await super().list(namespace=namespace)


def _transition(
    run_id: str,
    *,
    ok: bool = True,
    minute: int = 0,
    action_kind: str = "inspect",
) -> ExperienceTransition:
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}",
            run_id=run_id,
            iteration=1,
            occurred_at=_TIME + timedelta(minutes=minute),
            environment_id="environment:observed-world",
            scenario_id="scenario:observed-world",
            trust_classification="external_untrusted",
            pre_state={"phase": "ready"},
            observation={"state": "healthy"},
            action={"kind": action_kind},
            result={"ok": ok},
            terminal=True,
        )
    )


def _transition_without_action_kind(run_id: str, *, minute: int = 0) -> ExperienceTransition:
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}",
            run_id=run_id,
            iteration=1,
            occurred_at=_TIME + timedelta(minutes=minute),
            environment_id="environment:observed-world",
            scenario_id="scenario:observed-world",
            trust_classification="external_untrusted",
            pre_state={"phase": "ready"},
            observation={"state": "healthy"},
            action={"resource": "missing-kind"},
            result={"ok": True},
            terminal=True,
        )
    )


async def _append_transition(store, transition: ExperienceTransition) -> None:
    await store.append(
        RecordWrite(
            namespace="episodes",
            record_id=transition.transition_id,
            record_type="experience-transition",
            payload=transition.model_dump(mode="json"),
            created_at=transition.occurred_at,
        ),
        operation="record-transition",
        idempotency_key=f"record:{transition.run_id}",
    )


async def _evidence(
    store,
    *transitions: ExperienceTransition,
    outcomes: tuple[RunOutcome, ...] = (),
) -> LessonEvidence:
    for transition in transitions:
        await store.append(
            RecordWrite(
                namespace="episodes",
                record_id=transition.transition_id,
                record_type="experience-transition",
                payload=transition.model_dump(mode="json"),
                created_at=transition.occurred_at,
            ),
            operation="record-transition",
            idempotency_key=f"record:{transition.run_id}",
        )
    for outcome in outcomes:
        await store.append(
            RecordWrite(
                namespace="episodes",
                record_id=hashlib.sha256(f"run-outcome:{outcome.run_id}".encode()).hexdigest(),
                record_type="run-outcome",
                payload=outcome.model_dump(mode="json"),
                created_at=outcome.finished_at,
            ),
            operation="record-outcome",
            idempotency_key=f"outcome:{outcome.run_id}",
        )
    frozen = await store.create_snapshot(
        namespace="episodes",
        snapshot_id="snapshot:observed-world",
        operation="freeze-episodes",
        idempotency_key="freeze:observed-world",
    )
    records = [
        await store.get(namespace="episodes", record_id=member.record_id)
        for member in frozen.snapshot.members
    ]
    assert all(record is not None for record in records)
    return LessonEvidence(snapshot=frozen.snapshot, records=records, runs=[])


def _request(
    *,
    run_id: str = "new-run",
    state: str = "healthy",
    query: str = "inspect healthy true",
) -> MemoryContextRequest:
    return MemoryContextRequest(
        request_id="request",
        run_id=run_id,
        query=query,
        context={"latest_result": {"state": state}},
    )


def test_observed_world_is_explicitly_opted_in_and_keeps_strict_namespace_isolated() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        evidence = await _evidence(store, _transition("run-support"))
        disabled = WorldModelMemory(store, namespace="world", source=None, settings=_SETTINGS)
        await disabled.record_observed(
            evidence, {"run-support": _TIME + timedelta(minutes=1)}, idempotency_key="observed"
        )
        assert await store.list(namespace="world") == []
        assert await store.list(namespace="world:observed") == []
        assert (await disabled.retrieve(_request())).items == []

        enabled = WorldModelMemory(
            store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        await enabled.record_observed(
            evidence, {"run-support": _TIME + timedelta(minutes=1)}, idempotency_key="observed"
        )
        assert len(await store.list(namespace="world:observed")) == 1
        contribution = await enabled.retrieve(_request())
        assert len(contribution.items) == 1
        item = contribution.items[0]
        assert item.envelope.artefact_type == "observed_world_fact"
        assert item.envelope.item["classification"] == "descriptive_counts_only"
        assert item.envelope.item["claim_scope"] == "selected_recorded_experience_only"
        assert item.envelope.item["status"] == "verified_summary"
        assert item.envelope.item["status"] != "active"

    asyncio.run(scenario())


def test_observed_world_requires_action_projection_tokens_for_retrieval() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        evidence = await _evidence(store, _transition("run-inspect"))
        world = WorldModelMemory(
            store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        await world.record_observed(
            evidence,
            {"run-inspect": _TIME + timedelta(minutes=1)},
            idempotency_key="observed",
        )

        assert (await world.retrieve(_request(query="healthy true"))).items == []
        assert len((await world.retrieve(_request(query="inspect healthy true"))).items) == 1

    asyncio.run(scenario())


def test_observed_world_requires_all_multi_token_action_projection_tokens() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        transition = _transition("run-delete", action_kind="server.delete")
        evidence = await _evidence(store, transition)
        world = WorldModelMemory(
            store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        await world.record_observed(
            evidence,
            {transition.run_id: _TIME + timedelta(minutes=1)},
            idempotency_key="observed",
        )

        assert (await world.retrieve(_request(query="server healthy true"))).items == []
        assert len((await world.retrieve(_request(query="server delete healthy true"))).items) == 1

    asyncio.run(scenario())


def test_observed_world_replay_scope_and_same_run_filters_survive_reopen(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "observed-world.sqlite"
        store = SqliteStructuredStore(path)
        support = _transition("run-support")
        counter = _transition("run-counter", ok=False, minute=1)
        evidence = await _evidence(store, support, counter)
        world = WorldModelMemory(
            store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        cutoffs = {
            "run-support": support.occurred_at,
            "run-counter": counter.occurred_at,
        }
        await world.record_observed(evidence, cutoffs, idempotency_key="observed")
        await world.record_observed(evidence, cutoffs, idempotency_key="observed")
        assert len(await store.list(namespace="world:observed")) == 1

        reopened = WorldModelMemory(
            SqliteStructuredStore(path),
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        assert len((await reopened.retrieve(_request())).items) == 2
        assert (await reopened.retrieve(_request(run_id="run-support"))).items == []
        assert (await reopened.retrieve(_request(state="degraded"))).items == []

    asyncio.run(scenario())


def test_observed_world_rejects_non_nested_selected_batch_without_projected_summary() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        first = _transition("run-first")
        first_evidence = await _evidence(store, first)
        second = _transition_without_action_kind("run-second", minute=1)
        await _append_transition(store, second)
        second_snapshot = await store.create_snapshot(
            namespace="episodes",
            snapshot_id="snapshot:observed-world-second",
            operation="freeze-episodes",
            idempotency_key="freeze:observed-world-second",
        )
        second_records = [
            await store.get(namespace="episodes", record_id=member.record_id)
            for member in second_snapshot.snapshot.members
        ]
        assert all(record is not None for record in second_records)
        second_evidence = LessonEvidence(
            snapshot=second_snapshot.snapshot,
            records=second_records,
            runs=[],
        )
        world = WorldModelMemory(
            store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        await world.record_observed(
            first_evidence,
            {first.run_id: first.occurred_at},
            idempotency_key="observed-first",
        )
        await world.record_observed(
            second_evidence,
            {second.run_id: second.occurred_at},
            idempotency_key="observed-second",
        )

        with pytest.raises(MemoryPermanentError, match="selections are not nested"):
            await world.retrieve(_request())

    asyncio.run(scenario())


def test_observed_world_prefers_full_selected_coverage_over_larger_raw_snapshot() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        first = _transition("run-first")
        second = _transition("run-second", minute=1)
        full_evidence = await _evidence(store, first, second)
        third = _transition("run-third", minute=2)
        await _append_transition(store, third)
        extended_snapshot = await store.create_snapshot(
            namespace="episodes",
            snapshot_id="snapshot:observed-world-extended",
            operation="freeze-episodes",
            idempotency_key="freeze:observed-world-extended",
        )
        extended_records = [
            await store.get(namespace="episodes", record_id=member.record_id)
            for member in extended_snapshot.snapshot.members
        ]
        assert all(record is not None for record in extended_records)
        extended_evidence = LessonEvidence(
            snapshot=extended_snapshot.snapshot,
            records=extended_records,
            runs=[],
        )
        world = WorldModelMemory(
            store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        await world.record_observed(
            full_evidence,
            {first.run_id: first.occurred_at, second.run_id: second.occurred_at},
            idempotency_key="observed-full",
        )
        await world.record_observed(
            extended_evidence,
            {first.run_id: first.occurred_at},
            idempotency_key="observed-subset",
        )

        item = (await world.retrieve(_request())).items[0]
        batches = await store.list(namespace="world:observed")
        full_batch = next(
            batch for batch in batches if len(batch.payload["evidence"]["snapshot"]["members"]) == 2
        )
        assert item.envelope.item["observed_run_count"] == 2
        assert item.envelope.item["batch_input_hash"] == full_batch.payload["input_hash"]

    asyncio.run(scenario())


def test_observed_world_item_reports_only_cutoff_visible_outcome_status() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        transition = _transition("run-interrupted")
        outcome = RunOutcome(
            run_id=transition.run_id,
            status="interrupted",
            finished_at=transition.occurred_at + timedelta(minutes=1),
            stop_reason="stopped",
        )
        evidence = await _evidence(store, transition, outcomes=(outcome,))
        world = WorldModelMemory(
            store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        await world.record_observed(
            evidence,
            {transition.run_id: outcome.finished_at},
            idempotency_key="observed",
        )
        item = (await world.retrieve(_request())).items[0]
        assert item.envelope.item["source_run_outcome_statuses"] == {
            transition.run_id: "interrupted"
        }

        before_outcome = WorldModelMemory(
            store,
            namespace="world-before",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        await before_outcome.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed-before-outcome",
        )
        item = (await before_outcome.retrieve(_request())).items[0]
        assert item.envelope.item["source_run_outcome_statuses"] == {
            transition.run_id: "unobserved_at_cutoff"
        }

    asyncio.run(scenario())


def test_observed_world_requires_canonical_raw_snapshot_and_rejects_tampering() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        transition = _transition("run-support")
        evidence = await _evidence(store, transition)
        world = WorldModelMemory(
            store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        await world.record_observed(
            evidence, {transition.run_id: transition.occurred_at}, idempotency_key="observed"
        )

        raw_key = ("episodes", transition.transition_id)
        store._records[raw_key] = store._records[raw_key].model_copy(
            update={"content_hash": "0" * 64}
        )
        with pytest.raises(MemoryPermanentError):
            await world.retrieve(_request())

        clean_store = InMemoryStructuredStore()
        clean_evidence = await _evidence(clean_store, transition)
        clean_world = WorldModelMemory(
            clean_store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        await clean_world.record_observed(
            clean_evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )
        clean_store._snapshots.pop(clean_evidence.snapshot.snapshot_id)
        with pytest.raises(MemoryPermanentError):
            await clean_world.retrieve(_request())

    asyncio.run(scenario())


def test_observed_world_uses_batched_canonical_read_and_resolvable_batch_provenance() -> None:
    async def scenario() -> None:
        store = _CountingStore()
        transition = _transition("run-support")
        evidence = await _evidence(store, transition)
        world = WorldModelMemory(
            store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        await world.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )
        store.get_calls = 0
        store.list_namespaces.clear()

        item = (await world.retrieve(_request())).items[0]
        batch_record = (await store.list(namespace="world:observed"))[0]
        derived_from = item.envelope.provenance[0]
        assert derived_from.artefact_id == batch_record.record_id
        assert derived_from.content_hash == batch_record.content_hash
        assert item.envelope.item["batch_input_hash"] == batch_record.payload["input_hash"]
        assert store.get_calls == 0
        assert "episodes" in store.list_namespaces

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_observed_world_batched_canonical_read_rejects_missing_or_tampered_member(
    mutation: str,
) -> None:
    async def scenario() -> None:
        store = _CountingStore()
        transition = _transition("run-support")
        evidence = await _evidence(store, transition)
        world = WorldModelMemory(
            store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        await world.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )
        store.get_calls = 0
        store.list_namespaces.clear()

        raw_key = ("episodes", transition.transition_id)
        if mutation == "missing":
            store._records.pop(raw_key)
        else:
            store._records[raw_key] = store._records[raw_key].model_copy(
                update={"content_hash": "0" * 64}
            )

        with pytest.raises(MemoryPermanentError):
            await world.retrieve(_request())
        assert store.get_calls == 0
        assert "episodes" in store.list_namespaces

    asyncio.run(scenario())


def test_observed_world_rejects_an_omitted_persisted_summary() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        transition = _transition("run-support")
        evidence = await _evidence(store, transition)
        world = WorldModelMemory(
            store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        await world.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )
        persisted = (await store.list(namespace="world:observed"))[0]
        store._records[(persisted.namespace, persisted.record_id)] = StoredRecord.from_write(
            RecordWrite(
                namespace=persisted.namespace,
                record_id=persisted.record_id,
                record_type=persisted.record_type,
                payload={**persisted.payload, "summaries": []},
                created_at=persisted.created_at,
            )
        )
        with pytest.raises(MemoryPermanentError):
            await world.retrieve(_request())

    asyncio.run(scenario())


def test_observed_world_rejects_evidence_that_declares_noncanonical_members() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        transition = _transition("run-support")
        evidence = await _evidence(store, transition)
        forged_member = SnapshotMember(
            record_id=transition.transition_id,
            content_hash="0" * 64,
        )
        forged_snapshot = evidence.snapshot.model_copy(update={"members": [forged_member]})
        forged = evidence.model_copy(update={"snapshot": forged_snapshot})
        world = WorldModelMemory(
            store,
            namespace="world",
            source=None,
            settings=_SETTINGS,
            allow_observed_summaries=True,
        )
        with pytest.raises(MemoryValidationError):
            await world.record_observed(
                forged,
                {transition.run_id: transition.occurred_at},
                idempotency_key="observed",
            )

    asyncio.run(scenario())
