from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from uptick_agent.memory.contracts import (
    DecisionMemoryContext,
    ExperienceTransition,
    MemoryContextRequest,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.episodic import EpisodicMemory
from uptick_agent.memory.online_learning import OnlineLearningMemory
from uptick_agent.memory.patterns import PatternQuerySettings
from uptick_agent.memory.stores import InMemoryStructuredStore, SqliteStructuredStore
from uptick_agent.memory.world_model import WorldModelMemory
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
SETTINGS = PatternQuerySettings(
    scope_paths=("observation.state",),
    action_path="action.kind",
    result_path="result.ok",
)


def transition(run_id: str, iteration: int = 1, *, ok: bool = True) -> ExperienceTransition:
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}:{iteration}",
            run_id=run_id,
            iteration=iteration,
            occurred_at=NOW + timedelta(minutes=iteration),
            environment_id="environment:test",
            scenario_id="scenario:test",
            trust_classification="external_untrusted",
            pre_state={"phase": "ready"},
            observation={"state": "healthy"},
            action={"kind": "inspect"},
            result={"ok": ok},
            terminal=iteration == 3,
        )
    )


class Runtime:
    def __init__(self, store, *, events: list[str]) -> None:
        self.events = events
        self.episodic = EpisodicMemory(store, namespace="episodes")

    async def record_transition(self, value: ExperienceTransition) -> None:
        self.events.append("base.record_transition")
        await self.episodic.record(value, idempotency_key=f"record:{value.transition_id}")

    async def finalize_run(self, value: RunOutcome) -> None:
        self.events.append("base.finalize_run")
        await self.episodic.finalize(value, idempotency_key=f"outcome:{value.run_id}")

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext:
        contribution = await self.episodic.retrieve(request)
        return DecisionMemoryContext(items=contribution.items)


class Writer:
    def __init__(self, world, events: list[str]) -> None:
        self.world = world
        self.events = events

    async def record_observed(self, evidence, cutoffs, *, idempotency_key: str) -> None:
        self.events.append("writer.record_observed")
        await self.world.record_observed(evidence, cutoffs, idempotency_key=idempotency_key)


def make_bridge(
    store, *, events: list[str], every: int = 1
) -> tuple[OnlineLearningMemory, WorldModelMemory]:
    world = WorldModelMemory(
        store,
        namespace="world",
        source=None,
        settings=SETTINGS,
        allow_observed_summaries=True,
    )
    bridge = OnlineLearningMemory(
        Runtime(store, events=events),
        store,
        source_namespace="episodes",
        derived_namespace="online",
        writers=(Writer(world, events),),
        pattern_settings=SETTINGS,
        every_n_transitions=every,
    )
    return bridge, world


def request() -> MemoryContextRequest:
    return MemoryContextRequest(
        request_id="request",
        run_id="reader",
        query="inspect healthy true",
        context={"observation": {"state": "healthy"}},
    )


def outcome(run_id: str) -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        status="completed",
        finished_at=NOW + timedelta(hours=1),
        stop_reason="done",
    )


def test_record_persists_before_observed_summary_and_diagnostics_are_json_safe() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        events: list[str] = []
        bridge, world = make_bridge(store, events=events)

        await bridge.record_transition(transition("run-1"))

        assert events == ["base.record_transition", "writer.record_observed"]
        assert len(await store.list(namespace="episodes")) == 1
        assert len(await store.list(namespace="world:observed")) == 1
        json.dumps(bridge.online_learning_diagnostics)
        assert bridge.online_learning_diagnostics["materialization_count"] == 1
        assert len((await world.retrieve(request())).items) == 1

    asyncio.run(scenario())


def test_every_n_transitions_and_finalize_flush_only_new_prefix() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        events: list[str] = []
        bridge, world = make_bridge(store, events=events, every=2)

        await bridge.record_transition(transition("run-2", 1))
        assert await store.list(namespace="world:observed") == []
        await bridge.record_transition(transition("run-2", 2))
        assert len(await store.list(namespace="world:observed")) == 1
        await bridge.record_transition(transition("run-2", 3))
        await bridge.finalize_run(outcome("run-2"))

        assert len(await store.list(namespace="world:observed")) == 2
        finalize_index = events.index("base.finalize_run")
        assert finalize_index < max(
            index for index, event in enumerate(events) if event == "writer.record_observed"
        )
        assert bridge.online_learning_diagnostics["pending_transition_count"] == 0
        assert len((await world.retrieve(request())).items) == 1

    asyncio.run(scenario())


def test_duplicate_finalize_and_reopened_runtime_do_not_duplicate_summary(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "online.sqlite3"
        store = SqliteStructuredStore(path)
        events: list[str] = []
        bridge, _ = make_bridge(store, events=events)
        value = transition("run-3")
        final = outcome("run-3")

        await bridge.record_transition(value)
        await bridge.finalize_run(final)
        count = len(await store.list(namespace="world:observed"))
        await bridge.finalize_run(final)
        assert len(await store.list(namespace="world:observed")) == count

        reopened_store = SqliteStructuredStore(path)
        reopened_events: list[str] = []
        reopened, world = make_bridge(reopened_store, events=reopened_events)
        await reopened.finalize_run(final)
        assert len(await reopened_store.list(namespace="world:observed")) == count
        assert len((await world.retrieve(request())).items) == 1
        assert reopened.online_learning_diagnostics["skipped_duplicate_count"] >= 1

    asyncio.run(scenario())


def test_later_run_keeps_prior_trusted_cutoffs_in_cumulative_summary() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        bridge, world = make_bridge(store, events=[])

        await bridge.record_transition(transition("run-a"))
        await bridge.finalize_run(outcome("run-a"))
        await bridge.record_transition(transition("run-b"))
        await bridge.finalize_run(outcome("run-b"))

        observed = await world.retrieve(request())
        assert len(observed.items) == 1
        assert observed.items[0].envelope.item["observed_run_count"] == 2

    asyncio.run(scenario())


def test_bootstrap_admits_multiple_persisted_runs_without_fabricated_outcomes():
    async def scenario():
        store = InMemoryStructuredStore()
        base = Runtime(store, events=[])
        await base.record_transition(transition("prior-a"))
        await base.record_transition(transition("prior-b", ok=False))
        bridge, world = make_bridge(store, events=[])
        await bridge.learn_persisted_run("prior-a")
        await bridge.learn_persisted_run("prior-b")
        result = await world.retrieve(request())
        assert result.items
        fact = result.items[0].envelope.item
        assert fact["observed_run_count"] == 2
        assert fact["support_count"] == 1 and fact["counter_count"] == 1
        before = len(await store.list(namespace="world:observed"))
        await bridge.learn_persisted_run("prior-b")
        assert len(await store.list(namespace="world:observed")) == before
        assert all(r.record_type != "run-outcome" for r in await store.list(namespace="episodes"))

    asyncio.run(scenario())
