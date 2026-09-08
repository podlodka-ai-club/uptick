import asyncio

from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.models import (
    Capability,
    CapabilityCatalog,
    Observation,
    RunResult,
    RunSpec,
)
from uptick_agent.core.policy import DecisionPolicy
from uptick_agent.core.sgr import CurrentSGR
from uptick_agent.environments.scripted import ScriptedEnvironment
from uptick_agent.memory import NoMemory
from uptick_agent.reasoners.scripted import ScriptedReasoner
from uptick_agent.runtime.bootstrap import ScriptedEnvironmentBootstrapper
from uptick_agent.runtime.runner import AgentRunner
from uptick_agent.store import InMemoryRunStore


def _finish_output(label: str) -> dict:
    return {
        "phase": "finish",
        "facts": [label],
        "competing_hypotheses": ["the run is done"],
        "contradicting_evidence": [],
        "previous_verification": {"status": "not_applicable", "evidence": []},
        "strategy": "finish the completed run",
        "selected_action": {"name": "finish", "arguments": {}},
        "expected_result": ["the run becomes terminal"],
        "verification": ["the observation is terminal"],
        "task_completed": True,
    }


def _environment(name: str) -> ScriptedEnvironment:
    return ScriptedEnvironment(
        name=name,
        catalog=CapabilityCatalog(
            items=[
                Capability(
                    name="finish",
                    description="finish",
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    terminal=True,
                )
            ]
        ),
        observations=[Observation(action_kind="finish", summary="done", terminal=True)],
        result=RunResult(
            run_id=f"run-{name}",
            status="completed",
            steps=0,
            duration_seconds=0,
            stop_reason="",
        ),
    )


def test_core_runner_reasoner_environment_and_memory_are_replaceable() -> None:
    async def scenario() -> None:
        shared_core = AgentCore(
            reasoner=ScriptedReasoner([_finish_output("alpha"), _finish_output("beta")]),
            sgr=CurrentSGR(),
        )
        results = []
        for environment in (_environment("alpha"), _environment("beta")):
            runner = AgentRunner(
                agent_core=shared_core,
                environment=environment,
                memory=NoMemory(),
                run_store=InMemoryRunStore(),
                policy=DecisionPolicy(),
                bootstrapper=ScriptedEnvironmentBootstrapper(),
            )
            results.append(
                await runner.run(
                    RunSpec(run_id=f"run-{environment.name}", environment=environment.name)
                )
            )

        assert [result.run_id for result in results] == ["run-alpha", "run-beta"]

        for label in ("first reasoner", "second reasoner"):
            runner = AgentRunner(
                agent_core=AgentCore(
                    reasoner=ScriptedReasoner([_finish_output(label)]),
                    sgr=CurrentSGR(),
                ),
                environment=_environment(label.replace(" ", "-")),
                memory=NoMemory(),
                run_store=InMemoryRunStore(),
                policy=DecisionPolicy(),
                bootstrapper=ScriptedEnvironmentBootstrapper(),
            )
            run_id = "run-" + label.replace(" ", "-")
            assert (
                await runner.run(RunSpec(run_id=run_id, environment="portable"))
            ).status == "completed"

    asyncio.run(scenario())
