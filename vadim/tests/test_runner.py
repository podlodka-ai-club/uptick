import asyncio
import hashlib
from dataclasses import dataclass

import pytest

from uptick_agent.memory import InMemoryMemory, legacy_memory_runtime
from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryPermanentError,
    ObjectiveMetric,
    OperationLink,
    RunOutcome,
)
from uptick_agent.models import (
    AgentConfig,
    ApplyFix,
    FinishRun,
    GetOverview,
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
    def __init__(self, store: InMemoryMemory) -> None:
        self._runtime = legacy_memory_runtime(store)
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

    @property
    def context_diagnostics(self):
        return self._runtime.context_diagnostics


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
