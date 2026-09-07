import asyncio
from datetime import UTC, datetime

import pytest

from uptick_agent.memory.contracts import (
    MemoryContextRequest,
    MemoryPermanentError,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.observed_lessons import ObservedLessonMetric, ObservedLessonsMemory
from uptick_agent.memory.patterns import PatternQuerySettings
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

TIME = datetime(2026, 9, 7, tzinfo=UTC)
SETTINGS = PatternQuerySettings(
    scope_paths=("observation.resource",), action_path="action.kind", result_path="result.delta"
)


async def prepare(path):
    store = SqliteStructuredStore(path)
    for run in ("source-1", "source-2"):
        transition = DefaultExperienceTransitionAssembler().assemble(
            TransitionAssemblyRequest(
                transition_id=run,
                run_id=run,
                iteration=1,
                occurred_at=TIME,
                trust_classification="derived_untrusted",
                pre_state={"projection": "test"},
                observation={"resource": "worker"},
                action={"kind": "remove"},
                result={"delta": -5},
                terminal=False,
            )
        )
        await store.append(
            RecordWrite(
                namespace="source",
                record_id=run,
                record_type="experience-transition",
                payload=transition.model_dump(mode="json"),
                created_at=TIME,
            ),
            operation="test",
            idempotency_key=run,
        )
    snapshot = await store.create_snapshot(
        namespace="source", snapshot_id="snap", operation="freeze", idempotency_key="freeze"
    )
    evidence = LessonEvidence(
        snapshot=snapshot.snapshot, records=await store.list(namespace="source"), runs=[]
    )
    await module(path, enabled=True).record_observed(
        evidence, {"source-1": TIME, "source-2": TIME}, idempotency_key="derive"
    )


def module(path, *, enabled=False, direction="minimize"):
    return ObservedLessonsMemory(
        SqliteStructuredStore(path),
        namespace="lessons",
        settings=SETTINGS,
        metric=ObservedLessonMetric(name="cost", unit="minor/hour", direction=direction),
        enabled=enabled,
    )


def request(query="remove cost", resource="worker", run="new"):
    return MemoryContextRequest(
        request_id="query", run_id=run, query=query, context={"observation": {"resource": resource}}
    )


def test_reopened_lesson_is_descriptive_with_verified_support(tmp_path):
    async def scenario():
        path = tmp_path / "memory.sqlite3"
        await prepare(path)
        result = await module(path, enabled=True).retrieve(request())
        assert len(result.items) == 1
        envelope = result.items[0].envelope
        assert envelope.origin_module == "observed_lessons"
        assert envelope.item["metric_polarity"] == "positive"
        assert envelope.item["metric_delta"] == -5
        assert envelope.item["support_count"] == 2
        assert envelope.item["active"] is False
        assert envelope.item["causal_credit"] is False
        assert envelope.provenance

    asyncio.run(scenario())


def test_default_off_and_retrieval_boundaries(tmp_path):
    async def scenario():
        path = tmp_path / "memory.sqlite3"
        await prepare(path)
        assert not (await module(path).retrieve(request())).items
        enabled = module(path, enabled=True)
        for query in (
            request(query="unrelated"),
            request(resource="unknown"),
            request(run="source-1"),
        ):
            assert not (await enabled.retrieve(query)).items

    asyncio.run(scenario())


def test_metric_direction_cannot_silently_reinterpret_persisted_lessons(tmp_path):
    async def scenario():
        path = tmp_path / "memory.sqlite3"
        await prepare(path)
        with pytest.raises(MemoryPermanentError, match="configuration differs"):
            await module(path, enabled=True, direction="maximize").retrieve(request())

    asyncio.run(scenario())
