from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Literal

import pytest

from uptick_agent._model_base import StrictModel
from uptick_agent.composition.handoff import (
    DurableObservationHandoffStartup,
    ObservationHandoff,
    ObservationHandoffIdentity,
)
from uptick_agent.decisions.runtime import RuntimeDecisionContext, ToolResult
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.llm.contracts import StructuredGenerationResult
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.memory.contracts import ObjectiveMetric
from uptick_agent.memory.observation_reader import (
    ObservationReaderInvalidRequestError,
    ObservationReaderUnavailableError,
)
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.execute import AgentRunner
from uptick_agent.runs.handoff import ObservationReadRequest
from uptick_agent.runs.runtime_results import RuntimeRunResult


class ArchiveInspect(StrictModel):
    kind: Literal["inspect"] = "inspect"
    ref: str


class ArchiveRead(StrictModel):
    kind: Literal["read"] = "read"
    ref: str
    offset: int = 0
    max_bytes: int = 64


class ArchiveApply(StrictModel):
    kind: Literal["apply"] = "apply"
    ticket: int


class ArchiveDecision(StrictModel):
    current_situation: str
    hypothesis: str
    remaining_steps: list[str] = []
    task_completed: bool = False
    action: ArchiveInspect | ArchiveRead | ArchiveApply


class ArchiveBatchDecision(StrictModel):
    current_situation: str
    hypothesis: str
    remaining_steps: list[str] = []
    task_completed: bool = False
    actions: list[ArchiveInspect | ArchiveRead | ArchiveApply]


class Pointer(StrictModel):
    collection: str
    key: str


class PointerRead(StrictModel):
    operation: Literal["memory.read"] = "memory.read"
    pointer: Pointer
    byte_offset: int = 0
    byte_limit: int = 64


class PointerMutate(StrictModel):
    operation: Literal["world.mutate"] = "world.mutate"
    target: str


class PointerDecision(StrictModel):
    assessment: str
    action: PointerRead | PointerMutate


@dataclass
class Session:
    run_id: str
    seed: int
    live_value: int = 0
    environment_id: str | None = None
    scenario_id: str | None = None
    environment_content_hash: str | None = None
    scenario_content_hash: str | None = None


class RecordingMemory:
    """Minimal memory double that persists exact runner transitions canonically."""

    def __init__(self, store: InMemoryStructuredStore, namespace: str) -> None:
        self.store = store
        self.namespace = namespace
        self.transitions = []
        self.entries = []
        self.traces = []

    async def build_context(self, _request):
        from uptick_agent.memory.contracts import DecisionMemoryContext

        return DecisionMemoryContext()

    async def remember(self, entry) -> None:
        self.entries.append(entry)

    async def record_transition(self, transition) -> None:
        self.transitions.append(transition.model_copy(deep=True))
        await self.store.append(
            RecordWrite(
                namespace=self.namespace,
                record_id=transition.transition_id,
                record_type="experience-transition",
                payload=transition.model_dump(mode="json"),
                created_at=transition.occurred_at,
            ),
            operation="record-transition",
            idempotency_key=f"record:{transition.transition_id}",
        )

    async def clear(self, run_id=None) -> None:
        del run_id

    async def finalize_run(self, _outcome) -> None:
        return None

    async def record_trace(self, write):
        self.traces.append(write)
        return None

    @property
    def context_diagnostics(self):
        return {}


class ArchiveEnvironment:
    decision_spec = EnvironmentDecisionSpec(
        ArchiveDecision,
        environment_briefing="The environment owns inspect, read, and apply actions.",
        objective="Apply the archive fix while preserving the live metric.",
    )

    def __init__(self, run_id: str = "archive-run") -> None:
        self.run_id = run_id
        self.session: Session | None = None
        self.executed: list[ArchiveInspect | ArchiveApply] = []

    async def start(self, *, seed: int, agent_id: str, agent_version: str):
        del agent_id, agent_version
        self.session = Session(run_id=self.run_id, seed=seed)
        return self.session, ToolResult(
            action_kind="start",
            summary="Archive requires inspection.",
            data={"live_value": 0},
            objective_metrics=[ObjectiveMetric(name="live_value", value=0, unit="count")],
        )

    def public_state(self, session: Session):
        return {"live_value": session.live_value}

    async def execute(self, session: Session, action: ArchiveInspect | ArchiveApply):
        assert isinstance(action, (ArchiveInspect, ArchiveApply))
        self.executed.append(action)
        if isinstance(action, ArchiveInspect):
            session.live_value = 1
            return ToolResult(
                action_kind=action.kind,
                summary="Archive inspected.",
                data={"live_value": 1, "hidden_fact": "canonical archive fact"},
                objective_metrics=[ObjectiveMetric(name="live_value", value=1, unit="count")],
            )
        session.live_value = 2
        return ToolResult(
            action_kind=action.kind,
            summary="Archive fix applied.",
            data={"live_value": 2},
            objective_metrics=[ObjectiveMetric(name="live_value", value=2, unit="count")],
            terminal=True,
        )

    async def finish(
        self,
        session: Session,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ):
        return RuntimeRunResult(
            run_id=session.run_id,
            seed=session.seed,
            agent_id="archive-test",
            agent_version="1",
            status="completed",
            steps=steps,
            duration_seconds=duration_seconds,
            objective_metrics=[
                ObjectiveMetric(name="live_value", value=session.live_value, unit="count")
            ],
            stop_reason=stop_reason,
        )


class DurableArchiveEnvironment(ArchiveEnvironment):
    def __init__(self, run_id: str = "durable-archive-run") -> None:
        super().__init__(run_id=run_id)

    async def start(self, *, seed: int, agent_id: str, agent_version: str):
        session, result = await super().start(
            seed=seed,
            agent_id=agent_id,
            agent_version=agent_version,
        )
        session.environment_id = "environment:durable-archive"
        session.scenario_id = "scenario:durable-archive"
        session.environment_content_hash = hashlib.sha256(
            b"environment:durable-archive"
        ).hexdigest()
        session.scenario_content_hash = hashlib.sha256(b"scenario:durable-archive").hexdigest()
        return session, result


class ArchiveBatchEnvironment(ArchiveEnvironment):
    decision_spec = EnvironmentDecisionSpec(
        ArchiveBatchDecision,
        environment_briefing="The environment owns inspect, read, and apply actions.",
        objective="Apply the archive fix while preserving the live metric.",
    )

    def can_continue_batch(self, _session: Session, _result: ToolResult) -> bool:
        return True


class PointerEnvironment:
    decision_spec = EnvironmentDecisionSpec(
        PointerDecision,
        environment_briefing="The environment owns world.mutate and memory.read(pointer) actions.",
        objective="Mutate the pointed world once.",
    )

    def __init__(self, run_id: str = "pointer-run") -> None:
        self.run_id = run_id
        self.session: Session | None = None
        self.executed: list[PointerMutate] = []

    async def start(self, *, seed: int, agent_id: str, agent_version: str):
        del agent_id, agent_version
        self.session = Session(run_id=self.run_id, seed=seed)
        return self.session, ToolResult(action_kind="start", summary="Pointer world ready.")

    def public_state(self, session: Session):
        return {"live_value": session.live_value}

    async def execute(self, session: Session, action: PointerMutate):
        assert isinstance(action, PointerMutate)
        self.executed.append(action)
        session.live_value += 1
        return ToolResult(
            action_kind=action.operation,
            summary="Pointer target mutated.",
            data={"live_value": session.live_value},
            objective_metrics=[
                ObjectiveMetric(name="live_value", value=session.live_value, unit="count")
            ],
        )

    async def finish(
        self,
        session: Session,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ):
        return RuntimeRunResult(
            run_id=session.run_id,
            seed=session.seed,
            agent_id="pointer-test",
            agent_version="1",
            status="completed",
            steps=steps,
            duration_seconds=duration_seconds,
            objective_metrics=[],
            stop_reason=stop_reason,
        )


class RecordingObserver:
    def __init__(self) -> None:
        self.steps = []

    async def on_step(self, record) -> None:
        self.steps.append(record.model_copy(deep=True))

    async def on_finish(self, _result) -> None:
        return None


class ArchiveClient:
    model = "handoff-test-model"

    def __init__(self) -> None:
        self.requests = []

    async def generate_structured(self, request):
        self.requests.append(request)
        context = json.loads(request.messages[1].content.split("JSON follows:\n", 1)[1])
        call = len(self.requests)
        if call == 1:
            decision = ArchiveDecision(
                current_situation="inspect first",
                hypothesis="the canonical record will be useful",
                action=ArchiveInspect(ref="public-archive"),
            )
        elif call == 2:
            bookmark = context["observation_bookmarks"][0]
            decision = ArchiveDecision(
                current_situation="read the issued record",
                hypothesis="the hidden archive fact is in the result",
                action=ArchiveRead(ref=bookmark["record_id"], max_bytes=64),
            )
        else:
            decision = ArchiveDecision(
                current_situation="apply after reading",
                hypothesis="the live world remains unchanged by the read",
                action=ArchiveApply(ticket=7),
            )
        return StructuredGenerationResult(value=decision, provider="fake", model=request.model)


class BatchArchiveClient(ArchiveClient):
    async def generate_structured(self, request):
        self.requests.append(request)
        context = json.loads(request.messages[1].content.split("JSON follows:\n", 1)[1])
        call = len(self.requests)
        if call == 1:
            actions = [ArchiveInspect(ref="public-archive")]
        elif call == 2:
            bookmark = context["observation_bookmarks"][0]
            actions = [
                ArchiveRead(ref=bookmark["record_id"], max_bytes=64),
                ArchiveApply(ticket=99),
            ]
        else:
            actions = [ArchiveApply(ticket=7)]
        decision = ArchiveBatchDecision(
            current_situation=f"batch decision {call}",
            hypothesis="a read must end the batch before its queued mutation",
            actions=actions,
        )
        return StructuredGenerationResult(value=decision, provider="fake", model=request.model)


class PointerModel:
    def __init__(self) -> None:
        self.contexts: list[RuntimeDecisionContext] = []

    async def decide(self, context: RuntimeDecisionContext):
        self.contexts.append(context.model_copy(deep=True))
        if context.iteration == 1:
            return PointerDecision(
                assessment="mutate first",
                action=PointerMutate(target="world-a"),
            )
        bookmark = context.observation_bookmarks[0]
        return PointerDecision(
            assessment="read the issued pointer",
            action=PointerRead(
                pointer=Pointer(collection="episodic", key=bookmark["record_id"]),
                byte_offset=0,
                byte_limit=64,
            ),
        )

    def prompt_trace(self, context: RuntimeDecisionContext):
        return {"context": context.model_dump(mode="json")}


def _archive_mapper(action):
    if isinstance(action, ArchiveRead):
        return ObservationReadRequest(
            record_id=action.ref,
            offset=action.offset,
            max_bytes=action.max_bytes,
        )
    return None


def _pointer_mapper(action):
    if isinstance(action, PointerRead):
        return ObservationReadRequest(
            record_id=action.pointer.key,
            offset=action.byte_offset,
            max_bytes=action.byte_limit,
        )
    return None


def test_runner_handoff_uses_real_issued_bookmark_and_preserves_live_result() -> None:
    async def scenario() -> None:
        namespace = "handoff-archive"
        store = InMemoryStructuredStore()
        memory = RecordingMemory(store, namespace)
        environment = ArchiveEnvironment()
        client = ArchiveClient()
        observer = RecordingObserver()
        model = StructuredDecisionModel(
            client,
            response_model=ArchiveDecision,
            environment_briefing=environment.decision_spec.environment_briefing,
        )
        handoff = ObservationHandoff(store, namespace)
        result = await AgentRunner(
            config=AgentConfig(max_steps=3, memory_recall_limit=0),
            model=model,
            memory=memory,
            environment=environment,
            observer=observer,
            max_actions=3,
            observation_handoff=handoff,
            observation_read_action=_archive_mapper,
        ).run(seed=7)

        assert result.status == "completed"
        assert result.action_count == 3
        assert [type(action) for action in environment.executed] == [ArchiveInspect, ArchiveApply]
        assert len(memory.transitions) == 2
        assert len(observer.steps) == 3
        assert observer.steps[1].transition_id is None
        assert observer.steps[1].result.action_kind == "memory.read"
        assert observer.steps[1].result.ok is True

        second_context = json.loads(
            client.requests[1].messages[1].content.split("JSON follows:\n", 1)[1]
        )
        third_context = json.loads(
            client.requests[2].messages[1].content.split("JSON follows:\n", 1)[1]
        )
        bookmarks = second_context["observation_bookmarks"]
        assert len(bookmarks) == 1
        assert bookmarks[0]["record_id"] == memory.transitions[0].transition_id
        assert bookmarks[0]["historical_evidence"] == "stale_environment_observation"
        assert third_context["latest_result"]["data"]["live_value"] == 1
        assert third_context["latest_result"]["objective_metrics"][0]["value"] == 1
        assert third_context["memory_read_result"]["action_kind"] == "memory.read"
        assert third_context["memory_read_result"]["data"]["record_id"] == bookmarks[0]["record_id"]
        assert memory.transitions[1].objective_deltas[0].before == 1
        assert memory.transitions[1].objective_deltas[0].after == 2

        trace_types = [trace.event_type for trace in memory.traces]
        assert trace_types.count("decision.selected") == 3
        assert trace_types.count("decision.completed") == 2
        assert trace_types.count("decision.memory_read_completed") == 1

    asyncio.run(scenario())


def test_truncated_history_reference_recovers_omitted_detail_through_counted_read() -> None:
    """Transport integration with a scripted client, not an LLM utility claim."""

    class LargeEnvironment(ArchiveEnvironment):
        async def execute(self, session, action):
            result = await super().execute(session, action)
            if isinstance(action, ArchiveInspect) and action.ref == "large":
                result.data = {"aaa_padding": "x" * 1800, "zzz_ticket": 729}
            return result

    class ExactClient(ArchiveClient):
        async def generate_structured(self, request):
            self.requests.append(request)
            context = json.loads(request.messages[1].content.split("JSON follows:\n", 1)[1])
            call = len(self.requests)
            if call <= 2:
                action = ArchiveInspect(ref="large" if call == 1 else "small")
            elif call == 3:
                history = context["observation_history"]
                record = json.loads(history[0])
                assert record["_truncated"] is True
                assert "zzz_ticket" not in history[0]
                assert len(history[0].encode()) <= 1000
                ref = record["exact_read_record_id"]
                assert ref in {b["record_id"] for b in context["observation_bookmarks"]}
                action = ArchiveRead(ref=ref, max_bytes=8192)
            else:
                read = context["memory_read_result"]
                assert read["ok"] is True
                exact = json.loads(read["data"]["text"])
                action = ArchiveApply(ticket=exact["data"]["zzz_ticket"])
            decision = ArchiveDecision(
                current_situation="recover an omitted historical detail",
                hypothesis="read the issued reference",
                action=action,
            )
            return StructuredGenerationResult(value=decision, provider="fake", model=request.model)

    async def scenario():
        store = InMemoryStructuredStore()
        memory = RecordingMemory(store, "exact-history")
        environment = LargeEnvironment()
        client = ExactClient()
        result = await AgentRunner(
            config=AgentConfig(max_steps=4, memory_recall_limit=0),
            model=StructuredDecisionModel(client, response_model=ArchiveDecision),
            memory=memory,
            environment=environment,
            max_actions=4,
            observation_handoff=ObservationHandoff(store, "exact-history"),
            observation_read_action=_archive_mapper,
        ).run(seed=7)
        assert result.action_count == 4
        assert len(environment.executed) == 3
        assert environment.executed[-1].ticket == 729
        assert len(memory.transitions) == 3
        assert sum(t.event_type == "decision.memory_read_completed" for t in memory.traces) == 1

    asyncio.run(scenario())


def test_independent_nested_pointer_schema_maps_only_its_own_read_action() -> None:
    async def scenario() -> None:
        namespace = "handoff-pointer"
        store = InMemoryStructuredStore()
        memory = RecordingMemory(store, namespace)
        environment = PointerEnvironment()
        model = PointerModel()
        handoff = ObservationHandoff(store, namespace)
        result = await AgentRunner(
            config=AgentConfig(max_steps=3, memory_recall_limit=0),
            model=model,
            memory=memory,
            environment=environment,
            max_actions=3,
            observation_handoff=handoff,
            observation_read_action=_pointer_mapper,
        ).run(seed=9)

        assert result.action_count == 3
        assert len(environment.executed) == 1
        assert isinstance(environment.executed[0], PointerMutate)
        assert len(memory.transitions) == 1
        assert len(model.contexts[1].observation_bookmarks) == 1
        assert model.contexts[2].memory_read_result is not None
        assert model.contexts[2].memory_read_result.ok is True
        assert model.contexts[2].memory_read_result.data["record_id"] == (
            memory.transitions[0].transition_id
        )

        schema = environment.decision_spec.public_input()["response_schema"]
        schema_text = json.dumps(schema, sort_keys=True)
        assert "ArchiveRead" not in schema_text
        assert "PointerRead" in schema_text
        assert "pointer" in schema_text
        assert "collection" in schema_text

    asyncio.run(scenario())


def test_unknown_handoff_reference_is_a_counted_failure_and_budget_stops_tail() -> None:
    async def scenario() -> None:
        namespace = "handoff-unknown"
        store = InMemoryStructuredStore()
        memory = RecordingMemory(store, namespace)
        environment = ArchiveEnvironment(run_id="unknown-run")
        observer = RecordingObserver()

        class UnknownReferenceModel:
            def __init__(self) -> None:
                self.calls = 0

            async def decide(self, context):
                self.calls += 1
                if self.calls == 1:
                    return ArchiveDecision(
                        current_situation="inspect",
                        hypothesis="issue a bookmark",
                        action=ArchiveInspect(ref="public-archive"),
                    )
                if self.calls == 2:
                    return ArchiveDecision(
                        current_situation="try an unissued ref",
                        hypothesis="the runner must reject it",
                        action=ArchiveRead(ref="forged-record-id"),
                    )
                return ArchiveDecision(
                    current_situation="tail must not run",
                    hypothesis="budget is exhausted",
                    action=ArchiveApply(ticket=99),
                )

            def prompt_trace(self, context):
                return {"context": context.model_dump(mode="json")}

        model = UnknownReferenceModel()
        result = await AgentRunner(
            config=AgentConfig(max_steps=3, memory_recall_limit=0),
            model=model,
            memory=memory,
            environment=environment,
            observer=observer,
            max_actions=2,
            observation_handoff=ObservationHandoff(store, namespace),
            observation_read_action=_archive_mapper,
        ).run(seed=3)

        assert result.action_count == 2
        assert result.stop_reason == "action budget exhausted"
        assert model.calls == 2
        assert len(environment.executed) == 1
        assert len(memory.transitions) == 1
        assert observer.steps[1].transition_id is None
        assert observer.steps[1].result.action_kind == "memory.read"
        assert observer.steps[1].result.ok is False

    asyncio.run(scenario())


def test_batch_read_is_a_barrier_before_queued_tail_and_next_decision_runs() -> None:
    async def scenario() -> None:
        namespace = "handoff-batch"
        store = InMemoryStructuredStore()
        memory = RecordingMemory(store, namespace)
        environment = ArchiveBatchEnvironment(run_id="batch-run")
        client = BatchArchiveClient()
        observer = RecordingObserver()
        model = StructuredDecisionModel(
            client,
            response_model=ArchiveBatchDecision,
            environment_briefing=environment.decision_spec.environment_briefing,
        )
        handoff = ObservationHandoff(store, namespace)
        result = await AgentRunner(
            config=AgentConfig(max_steps=3, memory_recall_limit=0),
            model=model,
            memory=memory,
            environment=environment,
            observer=observer,
            max_actions=3,
            observation_handoff=handoff,
            observation_read_action=_archive_mapper,
        ).run(seed=11)

        assert result.action_count == 3
        assert [type(action) for action in environment.executed] == [
            ArchiveInspect,
            ArchiveApply,
        ]
        assert environment.executed[1].ticket == 7
        assert len(memory.transitions) == 2
        assert len(observer.steps) == 3
        assert observer.steps[1].transition_id is None
        assert observer.steps[1].result.action_kind == "memory.read"
        assert observer.steps[1].result.ok is True

        third_context = json.loads(
            client.requests[2].messages[1].content.split("JSON follows:\n", 1)[1]
        )
        assert third_context["latest_result"]["data"]["live_value"] == 1
        assert third_context["memory_read_result"]["ok"] is True
        assert third_context["memory_read_result"]["data"]["record_id"] == (
            memory.transitions[0].transition_id
        )

        trace_types = [trace.event_type for trace in memory.traces]
        assert trace_types.count("decision.memory_read_completed") == 1
        assert trace_types.count("decision.completed") == 2

    asyncio.run(scenario())


def test_handoff_requires_both_opt_in_hooks_and_legacy_context_omits_fields() -> None:
    store = InMemoryStructuredStore()
    handoff = ObservationHandoff(store, "handoff-guard")
    environment = ArchiveEnvironment()
    memory = RecordingMemory(store, "handoff-guard")

    with pytest.raises(ValueError, match="read action mapper"):
        AgentRunner(
            config=AgentConfig(max_steps=1),
            model=PointerModel(),
            memory=memory,
            environment=environment,
            observation_handoff=handoff,
        )
    with pytest.raises(ValueError, match="explicit max_actions"):
        AgentRunner(
            config=AgentConfig(max_steps=1),
            model=PointerModel(),
            memory=memory,
            environment=environment,
            observation_handoff=handoff,
            observation_read_action=_archive_mapper,
        )

    legacy_context = RuntimeDecisionContext(
        objective="legacy",
        run_id="legacy-run",
        seed=1,
        iteration=1,
        max_steps=1,
        latest_result=ToolResult(action_kind="start", summary="ready"),
    ).model_dump(mode="json")
    assert "observation_bookmarks" not in legacy_context
    assert "memory_read_result" not in legacy_context


class SingleInspectModel:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, _context):
        self.calls += 1
        return ArchiveDecision(
            current_situation="inspect once",
            hypothesis="the canonical transition becomes an issued bookmark",
            action=ArchiveInspect(ref="public-archive"),
        )


def _identity_from_session(
    namespace: str,
    session: Session,
    initial_result: ToolResult,
) -> ObservationHandoffIdentity:
    assert initial_result.action_kind == "start"
    assert session.environment_id is not None
    assert session.scenario_id is not None
    return ObservationHandoffIdentity(
        namespace=namespace,
        run_id=session.run_id,
        environment_id=session.environment_id,
        scenario_id=session.scenario_id,
        environment_content_hash=session.environment_content_hash,
        scenario_content_hash=session.scenario_content_hash,
    )


def _durable_startup(
    handoff: ObservationHandoff,
    namespace: str,
) -> DurableObservationHandoffStartup:
    return DurableObservationHandoffStartup(
        handoff,
        lambda session, initial_result: _identity_from_session(
            namespace,
            session,
            initial_result,
        ),
    )


def test_runner_startup_initializer_persists_and_fresh_controller_restores_index() -> None:
    async def scenario() -> None:
        namespace = "durable-run"
        index_namespace = "durable-run-index"
        store = InMemoryStructuredStore()
        memory = RecordingMemory(store, namespace)
        environment = DurableArchiveEnvironment()
        handoff = ObservationHandoff(
            store,
            namespace,
            durable_index_namespace=index_namespace,
        )
        model = SingleInspectModel()
        result = await AgentRunner(
            config=AgentConfig(max_steps=1, memory_recall_limit=0),
            model=model,
            memory=memory,
            environment=environment,
            max_actions=1,
            observation_handoff=handoff,
            observation_read_action=_archive_mapper,
            observation_handoff_startup=_durable_startup(handoff, namespace),
        ).run(seed=5)

        assert result.status == "completed"
        assert model.calls == 1
        assert len(memory.transitions) == 1
        session = environment.session
        assert session is not None
        identity = ObservationHandoffIdentity(
            namespace=namespace,
            run_id=session.run_id,
            environment_id=session.environment_id or "",
            scenario_id=session.scenario_id or "",
            environment_content_hash=session.environment_content_hash,
            scenario_content_hash=session.scenario_content_hash,
        )
        transition_id = memory.transitions[0].transition_id
        fresh = ObservationHandoff(
            store,
            namespace,
            durable_index_namespace=index_namespace,
        )
        await fresh.restore(identity, current_iteration=2)
        assert [item["record_id"] for item in fresh.snapshot(current_iteration=2)] == [
            transition_id
        ]
        read = await fresh.read(
            ObservationReadRequest(record_id=transition_id, max_bytes=8192),
            current_iteration=2,
        )
        assert read.ok is True
        assert read.data["record_id"] == transition_id

    asyncio.run(scenario())


def test_runner_startup_initializer_rejects_existing_index_before_model_decision() -> None:
    async def scenario() -> None:
        namespace = "durable-reject"
        index_namespace = "durable-reject-index"
        store = InMemoryStructuredStore()
        first_memory = RecordingMemory(store, namespace)
        first_environment = DurableArchiveEnvironment(run_id="durable-reject-run")
        first_handoff = ObservationHandoff(
            store,
            namespace,
            durable_index_namespace=index_namespace,
        )
        await AgentRunner(
            config=AgentConfig(max_steps=1, memory_recall_limit=0),
            model=SingleInspectModel(),
            memory=first_memory,
            environment=first_environment,
            max_actions=1,
            observation_handoff=first_handoff,
            observation_read_action=_archive_mapper,
            observation_handoff_startup=_durable_startup(first_handoff, namespace),
        ).run(seed=5)

        second_memory = RecordingMemory(store, namespace)
        second_environment = DurableArchiveEnvironment(run_id="durable-reject-run")
        second_handoff = ObservationHandoff(
            store,
            namespace,
            durable_index_namespace=index_namespace,
        )
        second_model = SingleInspectModel()
        with pytest.raises(ObservationReaderUnavailableError):
            await AgentRunner(
                config=AgentConfig(max_steps=1, memory_recall_limit=0),
                model=second_model,
                memory=second_memory,
                environment=second_environment,
                max_actions=1,
                observation_handoff=second_handoff,
                observation_read_action=_archive_mapper,
                observation_handoff_startup=_durable_startup(second_handoff, namespace),
            ).run(seed=5)
        assert second_model.calls == 0
        assert second_memory.transitions == []

    asyncio.run(scenario())


def test_runner_startup_initializer_rejects_identity_mismatch_before_index_write() -> None:
    async def scenario() -> None:
        for update in (
            {"run_id": "wrong-run"},
            {"scenario_id": "scenario:other"},
            {"environment_content_hash": "0" * 64},
            {"scenario_content_hash": "1" * 64},
        ):
            namespace = f"mismatch-{next(iter(update))[:8]}"
            index_namespace = f"{namespace}-index"
            store = InMemoryStructuredStore()
            memory = RecordingMemory(store, namespace)
            environment = DurableArchiveEnvironment(run_id=f"{namespace}-run")
            handoff = ObservationHandoff(
                store,
                namespace,
                durable_index_namespace=index_namespace,
            )

            def resolve(
                session: Session,
                initial_result: ToolResult,
                *,
                bound_namespace: str = namespace,
                bound_update: dict[str, str] = update,
            ) -> ObservationHandoffIdentity:
                base = _identity_from_session(
                    bound_namespace,
                    session,
                    initial_result,
                )
                return base.model_copy(update=bound_update)

            model = SingleInspectModel()
            with pytest.raises(ObservationReaderInvalidRequestError):
                await AgentRunner(
                    config=AgentConfig(max_steps=1, memory_recall_limit=0),
                    model=model,
                    memory=memory,
                    environment=environment,
                    max_actions=1,
                    observation_handoff=handoff,
                    observation_read_action=_archive_mapper,
                    observation_handoff_startup=DurableObservationHandoffStartup(
                        handoff,
                        resolve,
                    ),
                ).run(seed=5)
            assert model.calls == 0
            assert memory.transitions == []
            assert await store.list(namespace=index_namespace) == []

    asyncio.run(scenario())
