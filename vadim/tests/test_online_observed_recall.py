import asyncio
from datetime import UTC, datetime, timedelta

from uptick_agent.memory.contracts import MemoryContextRequest, TransitionAssemblyRequest
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.observed_lessons import ObservedLessonMetric, ObservedLessonsMemory
from uptick_agent.memory.patterns import PatternQuerySettings
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite
from uptick_agent.memory.world_model import WorldModelMemory
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

TIME = datetime(2026, 9, 7, tzinfo=UTC)
SETTINGS = PatternQuerySettings(
    scope_paths=("observation.resource",),
    action_path="action.kind",
    result_path="result.delta",
)


def _transition(run_id: str, *, iteration: int, minute: int, delta: int = -5):
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}:{iteration}",
            run_id=run_id,
            iteration=iteration,
            occurred_at=TIME + timedelta(minutes=minute),
            trust_classification="derived_untrusted",
            pre_state={"phase": "ready"},
            observation={"resource": "worker"},
            action={"kind": "remove"},
            result={"delta": delta},
            terminal=False,
        )
    )


async def _evidence(store, snapshot_id: str, *transitions):
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


def _world(store=None, *, allow_current_run_observed=False):
    if store is None:
        store = InMemoryStructuredStore()
    return WorldModelMemory(
        store,
        namespace="world",
        source=None,
        settings=SETTINGS,
        allow_observed_summaries=True,
        allow_current_run_observed=allow_current_run_observed,
    )


def _request(
    *,
    run_id="current",
    cutoff=None,
    iteration=None,
):
    context = {"observation": {"resource": "worker"}}
    if cutoff is not None:
        context["decision_cutoff"] = cutoff
    if iteration is not None:
        context["iteration"] = iteration
    return MemoryContextRequest(
        request_id="request",
        run_id=run_id,
        query="remove worker -5",
        context=context,
    )


def test_opt_in_allows_same_run_summary_only_before_both_boundaries():
    async def scenario():
        store = InMemoryStructuredStore()
        transition = _transition("current", iteration=1, minute=1)
        evidence = await _evidence(store, "snapshot:one", transition)
        world = _world(store, allow_current_run_observed=True)
        await world.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )

        result = await world.retrieve(
            _request(
                cutoff=(transition.occurred_at + timedelta(minutes=1)).isoformat(),
                iteration=2,
            )
        )
        assert len(result.items) == 1
        assert result.items[0].envelope.item["observed_run_count"] == 1

    asyncio.run(scenario())


def test_same_run_observed_recall_remains_blocked_by_default():
    async def scenario():
        store = InMemoryStructuredStore()
        transition = _transition("current", iteration=1, minute=1)
        evidence = await _evidence(store, "snapshot:one", transition)
        world = _world(store)
        await world.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )

        result = await world.retrieve(
            _request(
                cutoff=(transition.occurred_at + timedelta(minutes=1)).isoformat(),
                iteration=2,
            )
        )
        assert result.items == []

    asyncio.run(scenario())


def test_opt_in_requires_cutoff_and_iteration_for_same_run_recall():
    async def scenario():
        store = InMemoryStructuredStore()
        transition = _transition("current", iteration=1, minute=1)
        evidence = await _evidence(store, "snapshot:one", transition)
        world = _world(store, allow_current_run_observed=True)
        await world.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )

        missing_cutoff = await world.retrieve(_request(iteration=2))
        missing_iteration = await world.retrieve(
            _request(cutoff=(transition.occurred_at + timedelta(minutes=1)).isoformat())
        )
        assert missing_cutoff.items == []
        assert missing_iteration.items == []

    asyncio.run(scenario())


def test_equal_or_future_same_run_records_are_blocked():
    async def scenario():
        for transition, cutoff, iteration in (
            (
                _transition("current", iteration=1, minute=1),
                TIME + timedelta(minutes=1),
                2,
            ),
            (
                _transition("current", iteration=2, minute=1),
                TIME + timedelta(minutes=2),
                2,
            ),
            (
                _transition("current", iteration=1, minute=2),
                TIME + timedelta(minutes=1),
                2,
            ),
        ):
            store = InMemoryStructuredStore()
            evidence = await _evidence(store, f"snapshot:{transition.iteration}", transition)
            world = _world(store, allow_current_run_observed=True)
            await world.record_observed(
                evidence,
                {transition.run_id: transition.occurred_at},
                idempotency_key=f"observed:{transition.iteration}:{transition.occurred_at}",
            )
            result = await world.retrieve(_request(cutoff=cutoff.isoformat(), iteration=iteration))
            assert result.items == []

    asyncio.run(scenario())


def test_future_batch_does_not_hide_older_eligible_same_run_summary():
    async def scenario():
        store = InMemoryStructuredStore()
        earlier = _transition("current", iteration=1, minute=1)
        first = await _evidence(store, "snapshot:one", earlier)
        world = _world(store, allow_current_run_observed=True)
        await world.record_observed(
            first,
            {earlier.run_id: earlier.occurred_at},
            idempotency_key="observed:first",
        )

        future = _transition("current", iteration=2, minute=2)
        second = await _evidence(store, "snapshot:two", earlier, future)
        await world.record_observed(
            second,
            {future.run_id: future.occurred_at},
            idempotency_key="observed:second",
        )

        result = await world.retrieve(
            _request(
                cutoff=(earlier.occurred_at + timedelta(seconds=30)).isoformat(),
                iteration=2,
            )
        )
        assert len(result.items) == 1
        batches = await store.list(namespace="world:observed")
        assert result.items[0].envelope.item["batch_input_hash"] in {
            batch.payload["input_hash"] for batch in batches
        }

    asyncio.run(scenario())


def test_observed_lessons_threads_opt_in_same_run_recall():
    async def scenario():
        store = InMemoryStructuredStore()
        transition = _transition("current", iteration=1, minute=1)
        evidence = await _evidence(store, "snapshot:one", transition)
        lessons = ObservedLessonsMemory(
            store,
            namespace="lessons",
            settings=SETTINGS,
            metric=ObservedLessonMetric(name="cost", unit="minor/hour", direction="minimize"),
            enabled=True,
            allow_current_run_observed=True,
        )
        await lessons.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )

        result = await lessons.retrieve(
            _request(
                cutoff=(transition.occurred_at + timedelta(minutes=1)).isoformat(),
                iteration=2,
            )
        )
        assert len(result.items) == 1
        assert result.items[0].envelope.origin_module == "observed_lessons"

    asyncio.run(scenario())


def test_historical_summary_still_retrieves_without_same_run_boundary():
    async def scenario():
        store = InMemoryStructuredStore()
        transition = _transition("historical", iteration=1, minute=1)
        evidence = await _evidence(store, "snapshot:one", transition)
        world = _world(store, allow_current_run_observed=True)
        await world.record_observed(
            evidence,
            {transition.run_id: transition.occurred_at},
            idempotency_key="observed",
        )

        result = await world.retrieve(_request(run_id="current"))
        assert len(result.items) == 1

    asyncio.run(scenario())
