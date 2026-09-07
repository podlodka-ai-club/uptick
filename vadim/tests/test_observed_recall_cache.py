from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

import uptick_agent.memory.world_model as world_model_module
from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryContextRequest,
    MemoryPermanentError,
    MemoryValidationError,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.patterns import PatternQuerySettings
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite, StoredRecord, sha256_json
from uptick_agent.memory.world_model import WorldModelMemory
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_TIME = datetime(2026, 9, 7, tzinfo=UTC)
_SETTINGS = PatternQuerySettings(
    scope_paths=("observation.state",),
    action_path="action.kind",
    result_path="result.ok",
)


class _CountingStore(InMemoryStructuredStore):
    def __init__(self) -> None:
        super().__init__()
        self.list_calls: Counter[str] = Counter()
        self.snapshot_calls = 0

    async def list(self, *, namespace: str):
        self.list_calls[namespace] += 1
        return await super().list(namespace=namespace)

    async def get_snapshot(self, *, snapshot_id: str):
        self.snapshot_calls += 1
        return await super().get_snapshot(snapshot_id=snapshot_id)


def _transition(
    run_id: str,
    *,
    iteration: int = 1,
    minute: int = 1,
    ok: bool = True,
) -> ExperienceTransition:
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}:{iteration}",
            run_id=run_id,
            iteration=iteration,
            occurred_at=_TIME + timedelta(minutes=minute),
            environment_id="environment:observed-cache",
            scenario_id="scenario:observed-cache",
            trust_classification="derived_untrusted",
            pre_state={"phase": "ready"},
            observation={"state": "healthy"},
            action={"kind": "inspect"},
            result={"ok": ok},
            terminal=False,
        )
    )


async def _evidence(
    store: InMemoryStructuredStore,
    snapshot_id: str,
    *transitions: ExperienceTransition,
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
            idempotency_key=transition.transition_id,
        )
    snapshot = await store.create_snapshot(
        namespace="episodes",
        snapshot_id=snapshot_id,
        operation="freeze-episodes",
        idempotency_key=f"freeze:{snapshot_id}",
    )
    records = [
        await store.get(namespace="episodes", record_id=member.record_id)
        for member in snapshot.snapshot.members
    ]
    assert all(record is not None for record in records)
    return LessonEvidence(snapshot=snapshot.snapshot, records=records, runs=[])


def _world(
    store: InMemoryStructuredStore,
    *,
    allow_current_run_observed: bool = False,
    settings: PatternQuerySettings = _SETTINGS,
) -> WorldModelMemory:
    return WorldModelMemory(
        store,
        namespace="world",
        source=None,
        settings=settings,
        allow_observed_summaries=True,
        allow_current_run_observed=allow_current_run_observed,
    )


def _request(
    *,
    run_id: str = "current",
    cutoff: datetime | None = None,
    iteration: int | None = None,
) -> MemoryContextRequest:
    context: dict[str, object] = {"observation": {"state": "healthy"}}
    if cutoff is not None:
        context["decision_cutoff"] = cutoff.isoformat()
    if iteration is not None:
        context["iteration"] = iteration
    return MemoryContextRequest(
        request_id="request",
        run_id=run_id,
        query="inspect healthy true",
        context=context,
    )


def _replace_observed_batch(
    store: InMemoryStructuredStore,
    record: StoredRecord,
    payload: dict[str, object],
) -> None:
    store._records[(record.namespace, record.record_id)] = StoredRecord.from_write(
        RecordWrite(
            namespace=record.namespace,
            record_id=record.record_id,
            record_type=record.record_type,
            payload=payload,
            created_at=record.created_at,
        )
    )


def test_repeated_identical_retrieval_skips_deterministic_observed_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        store = _CountingStore()
        transition = _transition("historical")
        evidence = await _evidence(store, "snapshot:one", transition)
        world = _world(store)
        await world.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )

        counts = Counter()
        generate = world_model_module.generate_observed_pattern_candidates
        verify = world_model_module.verify_observed_pattern_summaries

        def counted_generate(*args, **kwargs):
            counts["generate"] += 1
            return generate(*args, **kwargs)

        def counted_verify(*args, **kwargs):
            counts["verify"] += 1
            return verify(*args, **kwargs)

        monkeypatch.setattr(
            world_model_module,
            "generate_observed_pattern_candidates",
            counted_generate,
        )
        monkeypatch.setattr(
            world_model_module,
            "verify_observed_pattern_summaries",
            counted_verify,
        )
        store.list_calls.clear()
        store.snapshot_calls = 0

        first = await world.retrieve(_request())
        first_snapshot_calls = store.snapshot_calls
        first_episode_lists = store.list_calls["episodes"]
        second = await world.retrieve(_request())

        assert second == first
        assert counts == {"generate": 1, "verify": 1}
        assert first_snapshot_calls == 1
        assert store.snapshot_calls == 2
        assert first_episode_lists == 1
        assert store.list_calls["episodes"] == 2
        assert len(world._observed_validation_cache) == 1

    asyncio.run(scenario())


def test_cached_observed_recall_isolated_from_returned_item_mutation() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        transition = _transition("historical")
        evidence = await _evidence(store, "snapshot:one", transition)
        world = _world(store)
        await world.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )

        first = await world.retrieve(_request())
        first_candidate = first.items[0].envelope.item["candidate"]
        first_candidate["scope"]["observation.state"] = "poisoned"
        first_candidate["result_value"] = False

        second = await world.retrieve(_request())

        assert second.items[0].envelope.item["candidate"] == {
            "scope": {"observation.state": "healthy"},
            "action_kind": "inspect",
            "result_path": "result.ok",
            "result_value": True,
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["tampered", "deleted"])
def test_cached_observed_recall_still_rejects_changed_or_missing_source(
    mutation: str,
) -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        transition = _transition("historical")
        evidence = await _evidence(store, "snapshot:one", transition)
        world = _world(store)
        await world.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )
        await world.retrieve(_request())

        source_key = ("episodes", transition.transition_id)
        if mutation == "tampered":
            store._records[source_key] = store._records[source_key].model_copy(
                update={"content_hash": "0" * 64}
            )
        else:
            store._records.pop(source_key)

        with pytest.raises(MemoryPermanentError):
            await world.retrieve(_request())

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["invalid_input_hash", "mismatched_settings"])
def test_cached_observed_recall_revalidates_changed_batch(
    mutation: str,
) -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        transition = _transition("historical")
        evidence = await _evidence(store, "snapshot:one", transition)
        world = _world(store)
        await world.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )
        await world.retrieve(_request())

        persisted = (await store.list(namespace="world:observed"))[0]
        payload = dict(persisted.payload)
        if mutation == "invalid_input_hash":
            payload["input_hash"] = "0" * 64
        else:
            mismatched = PatternQuerySettings(
                scope_paths=("observation.other",),
                action_path=_SETTINGS.action_path,
                result_path=_SETTINGS.result_path,
            )
            payload["settings"] = mismatched.model_dump(mode="json")
            payload["settings_hash"] = sha256_json(payload["settings"])
            payload["input_hash"] = sha256_json(
                {
                    "evidence": payload["evidence"],
                    "settings": payload["settings"],
                    "selection_cutoffs": {
                        key: value for key, value in sorted(payload["selection_cutoffs"].items())
                    },
                }
            )
        _replace_observed_batch(store, persisted, payload)

        with pytest.raises(MemoryPermanentError):
            await world.retrieve(_request())

    asyncio.run(scenario())


def test_observed_recall_cache_does_not_freeze_batches_or_same_run_boundaries() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        first_transition = _transition("current", iteration=1, minute=1)
        first_evidence = await _evidence(store, "snapshot:one", first_transition)
        world = _world(store, allow_current_run_observed=True)
        await world.record_observed(
            first_evidence,
            {first_transition.run_id: first_transition.occurred_at},
            idempotency_key="observed:first",
        )

        first = await world.retrieve(
            _request(
                cutoff=first_transition.occurred_at + timedelta(minutes=1),
                iteration=2,
            )
        )
        assert len(first.items) == 1
        first_hash = first.items[0].envelope.item["batch_input_hash"]
        first_count = first.items[0].envelope.item["observed_run_count"]

        second_transition = _transition("current", iteration=2, minute=2)
        second_evidence = await _evidence(
            store,
            "snapshot:two",
            first_transition,
            second_transition,
        )
        await world.record_observed(
            second_evidence,
            {"current": second_transition.occurred_at},
            idempotency_key="observed:second",
        )

        before_second = await world.retrieve(
            _request(
                cutoff=first_transition.occurred_at + timedelta(seconds=30),
                iteration=2,
            )
        )
        after_second = await world.retrieve(
            _request(
                cutoff=second_transition.occurred_at + timedelta(minutes=1),
                iteration=3,
            )
        )

        assert len(before_second.items) == 1
        assert before_second.items[0].envelope.item["batch_input_hash"] == first_hash
        assert before_second.items[0].envelope.item["observed_run_count"] == first_count
        assert len(after_second.items) == 1
        assert after_second.items[0].envelope.item["batch_input_hash"] != first_hash
        assert after_second.items[0].envelope.item["observed_run_count"] == 1

    asyncio.run(scenario())


def test_observed_recall_validation_cache_is_bounded() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        world = _world(store)
        cutoffs: dict[str, datetime] = {}
        transitions: list[ExperienceTransition] = []

        for index in range(17):
            transition = _transition(
                f"historical-{index}",
                minute=index + 1,
            )
            transitions.append(transition)
            cutoffs[transition.run_id] = transition.occurred_at
            evidence = await _evidence(store, f"snapshot:{index}", *transitions)
            await world.record_observed(
                evidence,
                cutoffs,
                idempotency_key=f"observed:{index}",
            )

        result = await world.retrieve(_request())

        assert len(result.items) == 1
        assert len(world._observed_validation_cache) == 16

    asyncio.run(scenario())


def test_empty_maintenance_contribution_does_not_scan_memory():
    from uptick_agent.memory.maintenance import MaintenanceRetrievalView

    async def scenario():
        store = _CountingStore()
        view = MaintenanceRetrievalView(store, namespace="episodes")
        assert await view.rank([], _request()) == []
        assert not store.list_calls
        with pytest.raises(MemoryValidationError):
            await view.rank([object()], _request())

    asyncio.run(scenario())
