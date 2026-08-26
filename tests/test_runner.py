import asyncio
from dataclasses import dataclass

from uptick_agent.memory import InMemoryMemory
from uptick_agent.models import (
    AgentConfig,
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
