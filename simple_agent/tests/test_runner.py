import asyncio
from dataclasses import dataclass

from uptick_agent.memory import InMemoryMemory, NullMemory
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

    async def decide(self, context):
        self.calls += 1
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
        return FakeSession(seed=seed), ToolResult(action_kind="start", summary="started")

    async def execute(self, session, action):
        if isinstance(action, GetOverview):
            return ToolResult(action_kind=action.kind, summary="site healthy", data={"balance": 10})
        return ToolResult(action_kind=action.kind, summary=action.reason, terminal=True)

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
            stop_reason=stop_reason,
        )


def test_runner_uses_memory_as_the_context_boundary() -> None:
    async def scenario() -> None:
        memory = InMemoryMemory()
        runner = AgentRunner(
            config=AgentConfig(agent_id="test-agent", agent_version="v1", max_steps=3),
            model=ScriptedModel(),
            memory=memory,
            environment=FakeEnvironment(),
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
            memory=NullMemory(),
            environment=FixEnvironment(),
        )

        await runner.run(7)

        first, second = model.contexts
        assert first.recalled_memories == []
        assert first.recent_steps == []
        assert second.recalled_memories == []
        assert len(second.recent_steps) == 1
        assert second.recent_steps[0].action == ApplyFix(message="FIX-123")
        assert second.recent_steps[0].result_summary == "Fix applied: FIX-123"
        assert second.run_state.applied_fix_messages == ["FIX-123"]

    asyncio.run(scenario())
