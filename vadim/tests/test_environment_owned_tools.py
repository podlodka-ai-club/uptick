"""End-to-end proof that tool schemas and state belong to the environment."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Literal

import pytest
from pydantic import Field

from uptick_agent._model_base import StrictModel
from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.decisions.runtime import RuntimeDecisionContext, ToolResult
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.llm.contracts import (
    StructuredGenerationResult,
    serialize_structured_generation_request,
)
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.contracts import ObjectiveMetric
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.execute import AgentRunner
from uptick_agent.runs.runtime_results import RuntimeRunResult


class SetThermostat(StrictModel):
    kind: Literal["set_thermostat"] = "set_thermostat"
    target_celsius: int = Field(ge=10, le=30)


class ThermostatDecision(StrictModel):
    current_situation: str
    hypothesis: str
    remaining_steps: list[str]
    task_completed: bool = False
    action: SetThermostat


class OtherTool(StrictModel):
    kind: Literal["other_tool"] = "other_tool"
    value: str


class OtherDecision(StrictModel):
    current_situation: str
    hypothesis: str
    remaining_steps: list[str]
    task_completed: bool = False
    action: OtherTool


@dataclass
class ThermostatSession:
    run_id: str = "thermostat-run"
    seed: int = 7
    target_celsius: int = 18


class ThermostatEnvironment:
    decision_spec = EnvironmentDecisionSpec(
        response_model=ThermostatDecision,
        environment_briefing="The environment exposes only set_thermostat(target_celsius).",
        objective="Reach the requested temperature through the declared tool.",
    )

    def __init__(self) -> None:
        self.session: ThermostatSession | None = None
        self.executed: list[SetThermostat] = []

    async def start(self, *, seed: int, agent_id: str, agent_version: str):
        self.session = ThermostatSession(seed=seed)
        return self.session, ToolResult(
            action_kind="start",
            summary="Thermostat needs adjustment.",
            data={"target_celsius": self.session.target_celsius},
        )

    def public_state(self, session: ThermostatSession) -> dict[str, object]:
        return {"target_celsius": session.target_celsius}

    async def execute(self, session: ThermostatSession, action: SetThermostat):
        self.executed.append(action)
        session.target_celsius = action.target_celsius
        terminal = len(self.executed) == 2
        return ToolResult(
            action_kind=action.kind,
            summary="Temperature updated.",
            data={"target_celsius": action.target_celsius},
            objective_metrics=[ObjectiveMetric(name="temperature_set", value=1, unit="boolean")],
            terminal=terminal,
        )

    async def finish(
        self,
        session: ThermostatSession,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ):
        return RuntimeRunResult(
            run_id=session.run_id,
            seed=session.seed,
            agent_id="thermostat-test",
            agent_version="1",
            status="completed",
            steps=steps,
            duration_seconds=duration_seconds,
            objective_metrics=[ObjectiveMetric(name="temperature_set", value=1, unit="boolean")],
            stop_reason=stop_reason,
        )


class RecordingClient:
    model = "third-world-model"

    def __init__(self) -> None:
        self.requests = []
        self._responses = (21, 22)

    async def generate_structured(self, request):
        self.requests.append(request)
        value = ThermostatDecision(
            current_situation="public thermostat evidence",
            hypothesis="setting the declared value updates the room",
            remaining_steps=[],
            action=SetThermostat(target_celsius=self._responses[len(self.requests) - 1]),
        )
        return StructuredGenerationResult(
            value=value,
            provider="test",
            model=request.model,
        )

    async def aclose(self) -> None:
        return None


def test_environment_owned_tool_round_trips_typed_action_and_transition(tmp_path):
    async def scenario() -> None:
        class RecordingObserver:
            def __init__(self) -> None:
                self.steps = []

            async def on_step(self, record) -> None:
                self.steps.append(record)

            async def on_finish(self, _result) -> None:
                return None

        client = RecordingClient()
        model = StructuredDecisionModel(
            client,
            response_model=ThermostatDecision,
            environment_briefing=ThermostatEnvironment.decision_spec.environment_briefing,
        )
        store_path = tmp_path / "thermostat.sqlite"
        store = SqliteStructuredStore(store_path)
        memory = compose_experimental_runtime(
            MemoryConfiguration.episodic_only(),
            store,
            namespace="third-world",
        )
        environment = ThermostatEnvironment()
        observer = RecordingObserver()
        result = await AgentRunner(
            config=AgentConfig(
                agent_id="thermostat-test",
                agent_version="1",
                max_steps=2,
                objective="Reach the requested temperature.",
            ),
            model=model,
            memory=memory,
            environment=environment,
            observer=observer,
        ).run(7)

        assert result.status == "completed"
        assert [item.target_celsius for item in environment.executed] == [21, 22]
        assert client.requests[0].response_model is ThermostatDecision
        assert (
            client.requests[0]
            .response_model.model_json_schema()["properties"]["action"]["$ref"]
            .endswith("SetThermostat")
        )
        assert observer.steps[0].model_dump(mode="json")["decision"]["action"] == {
            "kind": "set_thermostat",
            "target_celsius": 21,
        }
        second_user_message = client.requests[1].messages[1].content
        assert '"kind": "set_thermostat"' in second_user_message
        assert '"target_celsius": 21' in second_user_message
        assert "GetOverview" not in json.dumps(
            serialize_structured_generation_request(client.requests[0]), sort_keys=True
        )

        reopened = SqliteStructuredStore(store_path)
        records = await reopened.list(namespace="third-world")
        transitions = [
            record for record in records if record.record_type == "experience-transition"
        ]
        assert len(transitions) == 2
        assert transitions[-1].payload["action"] == {
            "kind": "set_thermostat",
            "target_celsius": 22,
        }
        assert transitions[-1].payload["result"]["data"]["target_celsius"] == 22

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "decision",
    [
        OtherDecision(
            current_situation="bad tool",
            hypothesis="the undeclared tool might work",
            remaining_steps=[],
            action=OtherTool(value="unexpected"),
        ),
        ThermostatDecision.model_construct(
            current_situation="bad argument",
            hypothesis="the malformed value might work",
            remaining_steps=[],
            action=SetThermostat.model_construct(target_celsius="invalid"),
        ),
    ],
)
def test_undeclared_or_malformed_action_is_rejected_before_environment_execute(tmp_path, decision):
    async def scenario() -> None:
        class BadModel:
            response_model = ThermostatDecision

            async def decide(self, _context: RuntimeDecisionContext):
                return decision

            def prompt_trace(self, context):
                return {"context": context.model_dump(mode="json")}

        environment = ThermostatEnvironment()
        memory = compose_experimental_runtime(
            MemoryConfiguration.episodic_only(),
            SqliteStructuredStore(tmp_path / "reject.sqlite"),
            namespace="reject",
        )
        with pytest.raises(ValueError, match="decision does not match"):
            await AgentRunner(
                config=AgentConfig(
                    agent_id="reject-test", agent_version="1", max_steps=1, objective="test"
                ),
                model=BadModel(),
                memory=memory,
                environment=environment,
            ).run(7)
        assert environment.executed == []

    asyncio.run(scenario())


def test_generic_runner_import_does_not_load_sre_actions():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import uptick_agent.runs.execute; "
            "assert 'uptick_agent.decisions.actions' not in sys.modules; "
            "assert 'uptick_agent.decisions.contracts' not in sys.modules; "
            "assert 'uptick_agent.simulator.actions' not in sys.modules",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == ""
