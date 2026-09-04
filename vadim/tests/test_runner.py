import asyncio
import hashlib
import json
from dataclasses import dataclass

import pytest

from uptick_agent.memory import InMemoryMemory, legacy_memory_runtime
from uptick_agent.memory.audit import (
    AuditTraceWrite,
    StructuredAuditTraceSink,
    audit_event_id,
)
from uptick_agent.memory.config import AuditConfiguration, MemoryConfiguration
from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryPermanentError,
    ObjectiveMetric,
    OperationLink,
    RunOutcome,
)
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.models import (
    AgentConfig,
    ApplyFix,
    FinishRun,
    GetOverview,
    MemoryEntry,
    NextStep,
    RunResult,
    ToolResult,
)
from uptick_agent.runner import AgentRunner


class ScriptedModel:
    def __init__(self) -> None:
        self.calls = 0
        self.contexts = []

    async def decide(self, context):
        self.calls += 1
        self.contexts.append(context)
        if self.calls == 1:
            return NextStep(
                current_situation="need an overview",
                hypothesis="the site may be healthy",
                remaining_steps=["inspect overview"],
                task_completed=False,
                action=GetOverview(),
            )
        return NextStep(
            current_situation="test is complete",
            hypothesis="no more work",
            remaining_steps=[],
            task_completed=True,
            action=FinishRun(reason="script finished"),
        )


@dataclass
class FakeSession:
    run_id: str = "run-123"
    seed: int = 7


class FakeEnvironment:
    async def start(self, *, seed, agent_id, agent_version):
        return FakeSession(seed=seed), ToolResult(
            action_kind="start",
            summary="started",
            objective_metrics=[ObjectiveMetric(name="balance", value=4, unit="minor")],
        )

    async def execute(self, session, action):
        if isinstance(action, GetOverview):
            return ToolResult(
                action_kind=action.kind,
                summary="site healthy",
                data={"balance": 10},
                objective_metrics=[ObjectiveMetric(name="balance", value=10, unit="minor")],
                operation_links=[OperationLink(operation_id="operation-1", relation="observed")],
            )
        return ToolResult(
            action_kind=action.kind,
            summary=action.reason,
            objective_metrics=[ObjectiveMetric(name="balance", value=12, unit="minor")],
            terminal=True,
        )

    async def finish(self, session, *, steps, duration_seconds, stop_reason):
        return RunResult(
            run_id=session.run_id,
            seed=session.seed,
            agent_id="test-agent",
            agent_version="v1",
            status="completed",
            steps=steps,
            duration_seconds=duration_seconds,
            balance_minor=10,
            objective_metrics=[ObjectiveMetric(name="balance", value=12, unit="minor")],
            stop_reason=stop_reason,
        )


class RecordingObserver:
    def __init__(self, events: list[str] | None = None) -> None:
        self.steps = []
        self.events = events

    async def on_step(self, record) -> None:
        self.steps.append(record)
        if self.events is not None:
            self.events.append("observer")

    async def on_finish(self, result) -> None:
        if self.events is not None:
            self.events.append("finish")
        return None


class TrackingMemory:
    def __init__(
        self,
        store: InMemoryMemory,
        *,
        configuration: MemoryConfiguration | None = None,
        audit_sink=None,
    ) -> None:
        self._runtime = legacy_memory_runtime(
            store,
            configuration=configuration,
            audit_sink=audit_sink,
        )
        self.events = []
        self.outcomes: list[RunOutcome] = []
        self.transitions: list[ExperienceTransition] = []

    async def build_context(self, request):
        return await self._runtime.build_context(request)

    async def remember(self, entry) -> None:
        await self._runtime.remember(entry)
        if entry.kind == "outcome":
            self.events.append("terminal-evidence")
        elif entry.kind == "experience":
            self.events.append("experience")

    async def clear(self, run_id=None) -> None:
        await self._runtime.clear(run_id)

    async def record_transition(self, transition: ExperienceTransition) -> None:
        self.events.append("transition")
        self.transitions.append(transition)
        await self._runtime.record_transition(transition)

    async def finalize_run(self, outcome: RunOutcome) -> None:
        self.events.append("finalize")
        self.outcomes.append(outcome)
        await self._runtime.finalize_run(outcome)

    async def record_trace(self, write):
        return await self._runtime.record_trace(write)

    @property
    def context_diagnostics(self):
        return self._runtime.context_diagnostics


class RecordingAuditSink:
    def __init__(self, configuration: MemoryConfiguration) -> None:
        self.runtime_configuration_fingerprint = configuration.fingerprint
        self.audit_configuration_fingerprint = configuration.audit.fingerprint
        self.writes: list[AuditTraceWrite] = []

    async def record(self, write: AuditTraceWrite):
        self.writes.append(write)
        return None


def _audited_configuration() -> MemoryConfiguration:
    return MemoryConfiguration.legacy_baseline(audit=AuditConfiguration.simulator_default())


def test_runner_uses_memory_as_the_context_boundary() -> None:
    async def scenario() -> None:
        memory = InMemoryMemory()
        memory_runtime = TrackingMemory(memory)
        model = ScriptedModel()
        observer = RecordingObserver(memory_runtime.events)
        runner = AgentRunner(
            config=AgentConfig(agent_id="test-agent", agent_version="v1", max_steps=3),
            model=model,
            memory=memory_runtime,
            environment=FakeEnvironment(),
            observer=observer,
        )

        result = await runner.run(7)

        assert result.status == "completed"
        assert result.steps == 2
        assert [item.kind for item in memory.entries] == [
            "observation",
            "experience",
            "experience",
            "outcome",
        ]
        assert memory.entries[-1].importance == 1.0
        assert model.contexts[0].recalled_memories == []
        assert model.contexts[0].memory_context.items[0].envelope.origin_module == (
            "compatibility.legacy"
        )
        assert observer.steps[0].memory_diagnostics["configuration_fingerprint"]
        assert len(observer.steps[0].memory_diagnostics["request_id"]) == 64
        assert memory_runtime.events == [
            "transition",
            "experience",
            "observer",
            "transition",
            "experience",
            "observer",
            "terminal-evidence",
            "finalize",
            "finish",
        ]
        assert len(memory_runtime.transitions) == 2
        first, terminal = memory_runtime.transitions
        assert first.transition_id == hashlib.sha256(b"experience-transition:run-123:1").hexdigest()
        assert first.action["kind"] == "get_overview"
        assert first.pre_state["operation_statuses"] == {}
        assert first.objective_deltas[0].before == 4
        assert first.objective_deltas[0].after == 10
        assert first.objective_deltas[0].delta == 6
        assert first.operation_links == [
            OperationLink(operation_id="operation-1", relation="observed")
        ]
        assert terminal.terminal is True
        assert terminal.objective_deltas[0].delta == 2
        assert memory_runtime.outcomes[0].status == "completed"
        assert memory_runtime.outcomes[0].objective_metrics == [
            ObjectiveMetric(name="balance", value=12, unit="minor")
        ]

    asyncio.run(scenario())


def test_runner_audit_correlations_and_event_order_are_deterministic() -> None:
    async def scenario() -> None:
        configuration = _audited_configuration()
        audit = RecordingAuditSink(configuration)
        memory = TrackingMemory(
            InMemoryMemory(), configuration=configuration, audit_sink=audit
        )
        runner = AgentRunner(
            config=AgentConfig(agent_id="test-agent", agent_version="v1", max_steps=3),
            model=ScriptedModel(),
            memory=memory,
            environment=FakeEnvironment(),
        )

        await runner.run(7)

        assert [write.event_type for write in audit.writes] == [
            "memory.context_selected",
            "decision.input",
            "decision.selected",
            "decision.completed",
            "memory.context_selected",
            "decision.input",
            "decision.selected",
            "decision.completed",
            "run.outcome",
        ]
        outcome = audit.writes[-1]
        assert outcome.outcome_correlation_id == audit_event_id("run.outcome", "run-123")
        assert outcome.event_id != outcome.outcome_correlation_id
        for offset in (0, 4):
            context, input_event, selected, completed = audit.writes[offset : offset + 4]
            assert context.request_id == input_event.request_id
            assert input_event.request_id == selected.request_id == completed.request_id
            assert input_event.decision_id == selected.decision_id == completed.decision_id
            assert (
                input_event.outcome_correlation_id
                == selected.outcome_correlation_id
                == completed.outcome_correlation_id
                == outcome.outcome_correlation_id
            )
            assert context.decision_id is None
            assert completed.transition_id
            assert len({
                context.event_id,
                input_event.event_id,
                selected.event_id,
                completed.event_id,
            }) == 4

    asyncio.run(scenario())


def test_runner_keeps_structured_facts_when_decision_traces_are_disabled() -> None:
    narrative = "NARRATIVE-MUST-NOT-BE-STORED"

    class NarrativeModel(ScriptedModel):
        async def decide(self, context):
            decision = await super().decide(context)
            return decision.model_copy(
                update={
                    "current_situation": narrative,
                    "hypothesis": "NARRATIVE-HYPOTHESIS-MUST-NOT-BE-STORED",
                }
            )

    async def scenario() -> None:
        audit_configuration = AuditConfiguration.simulator_default()
        audit_configuration.raw_content.prompts = False
        audit_configuration.raw_content.observations = False
        audit_configuration.raw_content.decision_traces = False
        configuration = MemoryConfiguration.legacy_baseline(audit=audit_configuration)
        store = InMemoryStructuredStore()
        audit = StructuredAuditTraceSink(
            store,
            namespace="runner-raw-disabled",
            configuration=configuration.audit,
            runtime_configuration_fingerprint=configuration.fingerprint,
        )
        memory = TrackingMemory(
            InMemoryMemory(
                [
                    MemoryEntry(
                        id="known-item",
                        kind="lesson",
                        content="started service is healthy",
                        importance=0.9,
                    )
                ]
            ),
            configuration=configuration,
            audit_sink=audit,
        )
        runner = AgentRunner(
            config=AgentConfig(agent_id="test-agent", agent_version="v1", max_steps=3),
            model=NarrativeModel(),
            memory=memory,
            environment=FakeEnvironment(),
        )

        await runner.run(7)

        events = await audit.list_events()
        assert all(
            capture.body is None
            for event in events
            for capture in event.captures
            if capture.body_class == "decision_traces"
        )
        assert narrative not in json.dumps(
            [event.model_dump(mode="json") for event in events], sort_keys=True
        )

        context = next(event for event in events if event.event_type == "memory.context_selected")
        assert context.metadata["selected_item_ids"]
        assert context.metadata["selection_evidence"][0]["score"]
        assert context.metadata["selection_evidence"][0]["selection_reason"]
        assert context.metadata["effective_item_limit"] == 8
        assert context.metadata["effective_token_limit"] == 4_000
        assert context.metadata["estimator_id"] == "utf8-byte-upper-bound"

        selected = next(event for event in events if event.event_type == "decision.selected")
        assert selected.metadata["action_kind"] == "get_overview"
        assert selected.metadata["action"] == {"kind": "get_overview"}

        completed = next(event for event in events if event.event_type == "decision.completed")
        assert completed.metadata["prompt_included_item_ids"]
        assert completed.metadata["action_kind"] == "get_overview"
        assert completed.metadata["ok"] is True
        assert completed.metadata["terminal"] is False
        assert completed.metadata["objective_metrics"] == [
            {
                "schema_version": "1.0",
                "name": "balance",
                "value": 10.0,
                "unit": "minor",
            }
        ]
        assert completed.metadata["operation_links"] == [
            {
                "schema_version": "1.1",
                "operation_id": "operation-1",
                "relation": "observed",
            }
        ]

        outcome = next(event for event in events if event.event_type == "run.outcome")
        assert outcome.metadata["status"] == "completed"
        assert outcome.metadata["objective_metrics"] == [
            {
                "schema_version": "1.0",
                "name": "balance",
                "value": 12.0,
                "unit": "minor",
            }
        ]
        assert outcome.metadata["outcome_semantics"] == (
            "runner-observed-before-module-finalizers"
        )

    asyncio.run(scenario())


def test_runner_records_selected_action_before_execution_failure() -> None:
    class FailingEnvironment(FakeEnvironment):
        async def execute(self, session, action):
            raise RuntimeError("environment failed")

    async def scenario() -> None:
        configuration = _audited_configuration()
        audit = RecordingAuditSink(configuration)
        memory = TrackingMemory(
            InMemoryMemory(), configuration=configuration, audit_sink=audit
        )
        runner = AgentRunner(
            config=AgentConfig(agent_id="test-agent", agent_version="v1", max_steps=1),
            model=ScriptedModel(),
            memory=memory,
            environment=FailingEnvironment(),
        )

        with pytest.raises(RuntimeError, match="environment failed"):
            await runner.run(7)

        assert [write.event_type for write in audit.writes] == [
            "memory.context_selected",
            "decision.input",
            "decision.selected",
            "run.outcome",
        ]
        selected, outcome = audit.writes[2:]
        assert selected.raw_bodies["decision_traces"]["decision"]["action"]["kind"] == (
            "get_overview"
        )
        assert selected.outcome_correlation_id == outcome.outcome_correlation_id
        assert outcome.event_id != outcome.outcome_correlation_id
        assert outcome.raw_bodies["decision_traces"]["status"] == "failed"

    asyncio.run(scenario())


def test_runner_cancellation_keeps_input_correlation_and_records_interrupted_outcome() -> None:
    class CancellingModel:
        async def decide(self, context):
            raise asyncio.CancelledError()

    async def scenario() -> None:
        configuration = _audited_configuration()
        audit = RecordingAuditSink(configuration)
        memory = TrackingMemory(
            InMemoryMemory(), configuration=configuration, audit_sink=audit
        )
        runner = AgentRunner(
            config=AgentConfig(agent_id="test-agent", agent_version="v1", max_steps=1),
            model=CancellingModel(),
            memory=memory,
            environment=FakeEnvironment(),
        )

        with pytest.raises(asyncio.CancelledError):
            await runner.run(7)

        assert [write.event_type for write in audit.writes] == [
            "memory.context_selected",
            "decision.input",
            "run.outcome",
        ]
        input_event, outcome = audit.writes[1:]
        assert input_event.outcome_correlation_id == outcome.outcome_correlation_id
        assert input_event.decision_id
        assert outcome.raw_bodies["decision_traces"]["status"] == "interrupted"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "status"),
    [(RuntimeError("decision failed"), "failed"), (asyncio.CancelledError(), "interrupted")],
)
def test_runner_records_and_finalizes_an_aborted_run_without_masking_the_error(
    error: BaseException, status: str
) -> None:
    class FailingModel:
        async def decide(self, context):
            raise error

    async def scenario() -> None:
        store = InMemoryMemory()
        memory = TrackingMemory(store)
        runner = AgentRunner(
            config=AgentConfig(agent_id="test-agent", agent_version="v1", max_steps=1),
            model=FailingModel(),
            memory=memory,
            environment=FakeEnvironment(),
        )

        with pytest.raises(type(error)):
            await runner.run(7)

        assert [entry.kind for entry in store.entries] == ["observation", "outcome"]
        assert memory.events == ["terminal-evidence", "finalize"]
        assert memory.transitions == []
        assert memory.outcomes[0].status == status
        assert type(error).__name__ in memory.outcomes[0].stop_reason

    asyncio.run(scenario())


def test_completed_run_surfaces_legacy_outcome_error_after_typed_finalization() -> None:
    legacy_error = RuntimeError("legacy outcome evidence failed")

    class FailingOutcomeEvidenceMemory(TrackingMemory):
        async def remember(self, entry) -> None:
            if entry.kind == "outcome":
                raise legacy_error
            await super().remember(entry)

    async def scenario() -> None:
        memory = FailingOutcomeEvidenceMemory(InMemoryMemory())
        runner = AgentRunner(
            config=AgentConfig(agent_id="test-agent", agent_version="v1", max_steps=3),
            model=ScriptedModel(),
            memory=memory,
            environment=FakeEnvironment(),
        )

        with pytest.raises(RuntimeError) as raised:
            await runner.run(7)

        assert raised.value is legacy_error
        assert [outcome.status for outcome in memory.outcomes] == ["completed"]
        assert memory.events[-1] == "finalize"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error_type", "status"),
    [(RuntimeError, "failed"), (asyncio.CancelledError, "interrupted")],
)
def test_aborted_run_preserves_original_error_when_both_outcome_paths_fail(
    error_type: type[BaseException], status: str
) -> None:
    async def scenario() -> None:
        original_error = error_type("model stopped")
        evidence_error = RuntimeError("legacy outcome evidence failed")
        finalization_error = MemoryPermanentError("typed finalizer failed")

        class FailingOutcomePathsMemory(TrackingMemory):
            async def remember(self, entry) -> None:
                if entry.kind == "outcome":
                    raise evidence_error
                await super().remember(entry)

            async def finalize_run(self, outcome: RunOutcome) -> None:
                self.events.append("finalize")
                self.outcomes.append(outcome)
                raise finalization_error

        class FailingModel:
            async def decide(self, context):
                raise original_error

        memory = FailingOutcomePathsMemory(InMemoryMemory())
        runner = AgentRunner(
            config=AgentConfig(agent_id="test-agent", agent_version="v1", max_steps=1),
            model=FailingModel(),
            memory=memory,
            environment=FakeEnvironment(),
        )

        with pytest.raises(error_type) as raised:
            await runner.run(7)

        assert raised.value is original_error
        assert memory.events == ["finalize"]
        assert [outcome.status for outcome in memory.outcomes] == [status]
        assert raised.value.__notes__ == [
            "Memory outcome evidence also failed with RuntimeError.",
            "Memory finalization also failed with MemoryPermanentError.",
        ]

    asyncio.run(scenario())


def test_transition_persistence_failure_aborts_before_legacy_experience_and_observer() -> None:
    class FailingTransitionMemory(TrackingMemory):
        async def record_transition(self, transition: ExperienceTransition) -> None:
            self.events.append("transition")
            raise MemoryPermanentError("episodic write failed")

    async def scenario() -> None:
        store = InMemoryMemory()
        memory = FailingTransitionMemory(store)
        observer = RecordingObserver(memory.events)
        runner = AgentRunner(
            config=AgentConfig(agent_id="test-agent", agent_version="v1", max_steps=1),
            model=ScriptedModel(),
            memory=memory,
            environment=FakeEnvironment(),
            observer=observer,
        )

        with pytest.raises(MemoryPermanentError, match="episodic write failed"):
            await runner.run(7)

        assert [entry.kind for entry in store.entries] == ["observation", "outcome"]
        assert observer.steps == []
        assert memory.events == ["transition", "terminal-evidence", "finalize"]
        assert memory.outcomes[0].status == "failed"

    asyncio.run(scenario())


def test_completed_world_run_is_not_rewritten_when_final_memory_write_fails() -> None:
    class FailingFinalizerMemory(TrackingMemory):
        async def finalize_run(self, outcome: RunOutcome) -> None:
            self.events.append("finalize")
            self.outcomes.append(outcome)
            raise MemoryPermanentError("outcome write failed")

    async def scenario() -> None:
        store = InMemoryMemory()
        memory = FailingFinalizerMemory(store)
        runner = AgentRunner(
            config=AgentConfig(agent_id="test-agent", agent_version="v1", max_steps=3),
            model=ScriptedModel(),
            memory=memory,
            environment=FakeEnvironment(),
        )

        with pytest.raises(MemoryPermanentError, match="outcome write failed"):
            await runner.run(7)

        assert [outcome.status for outcome in memory.outcomes] == ["completed"]
        assert memory.events[-2:] == ["terminal-evidence", "finalize"]
        assert [entry.kind for entry in store.entries][-1] == "outcome"

    asyncio.run(scenario())


def test_runner_keeps_short_term_context_when_long_term_memory_is_disabled() -> None:
    class FixThenFinishModel:
        def __init__(self) -> None:
            self.contexts = []

        async def decide(self, context):
            self.contexts.append(context)
            if len(self.contexts) == 1:
                return NextStep(
                    current_situation="an exact fix is known",
                    hypothesis="applying it will restore the service",
                    remaining_steps=["apply the fix"],
                    task_completed=False,
                    action=ApplyFix(message="FIX-123"),
                )
            return NextStep(
                current_situation="the fix was applied",
                hypothesis="the test can finish",
                remaining_steps=[],
                task_completed=True,
                action=FinishRun(reason="script finished"),
            )

    class FixEnvironment(FakeEnvironment):
        async def execute(self, session, action):
            if isinstance(action, ApplyFix):
                return ToolResult(
                    action_kind=action.kind,
                    summary="Fix applied: FIX-123",
                    data={"applied": True, "message": action.message},
                )
            return await super().execute(session, action)

    async def scenario() -> None:
        model = FixThenFinishModel()
        runner = AgentRunner(
            config=AgentConfig(agent_id="test-agent", agent_version="v1", max_steps=3),
            model=model,
            memory=legacy_memory_runtime(None),
            environment=FixEnvironment(),
        )

        await runner.run(7)

        first, second = model.contexts
        assert first.recalled_memories == []
        assert first.memory_context.items == []
        assert first.recent_steps == []
        assert second.recalled_memories == []
        assert len(second.recent_steps) == 1
        assert second.recent_steps[0].action == ApplyFix(message="FIX-123")
        assert second.recent_steps[0].result_summary == "Fix applied: FIX-123"
        assert second.run_state.applied_fix_messages == ["FIX-123"]

    asyncio.run(scenario())
