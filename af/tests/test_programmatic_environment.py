import asyncio
import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from tests.helpers import decode_prompt_context
from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.models import (
    AgentConstraints,
    Capability,
    CapabilityCall,
    CapabilityCatalog,
    EnvironmentProfileRef,
    Observation,
    RunResult,
    RunSpec,
)
from uptick_agent.core.policy import DecisionPolicy, validate_json_schema
from uptick_agent.core.sgr import CurrentSGR, normalized_output_schema
from uptick_agent.core.trace_models import DecisionTracePayload
from uptick_agent.environments.programmatic import (
    PROGRAM_CAPABILITY_NAME,
    ProgramInvocation,
    ProgrammableEnvironment,
)
from uptick_agent.environments.scripted import ScriptedEnvironment
from uptick_agent.memory import NoMemory
from uptick_agent.reasoners.scripted import ScriptedReasoner
from uptick_agent.runtime.bootstrap import ScriptedEnvironmentBootstrapper
from uptick_agent.runtime.runner import AgentRunner
from uptick_agent.store import InMemoryRunStore


def _object_schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _catalog() -> CapabilityCatalog:
    return CapabilityCatalog(
        items=[
            Capability(
                name="advance",
                description="advance",
                input_schema=_object_schema(
                    {"seconds": {"type": "integer", "minimum": 1}},
                    ["seconds"],
                ),
                mutates_state=True,
            ),
            Capability(
                name="inspect",
                description="inspect",
                input_schema=_object_schema(
                    {"status": {"type": "integer", "enum": [500]}},
                    ["status"],
                ),
            ),
            Capability(
                name="finish",
                description="finish",
                input_schema=_object_schema({}, []),
                terminal=True,
            ),
        ]
    )


def _definition(*, followup_capability: str = "inspect") -> dict:
    return {
        "name": "advance_and_inspect",
        "description": "Advance once, then inspect only when errors appeared.",
        "primary_capability": "advance",
        "followup_capability": followup_capability,
        "condition": {
            "path": ["data", "errors"],
            "operator": "gt",
            "value": 0,
        },
        "primary_output_paths": [["data", "errors"]],
        "followup_output_paths": [["data", "messages"]],
    }


def _define_call(*, seconds: int = 300, followup_capability: str = "inspect") -> CapabilityCall:
    return CapabilityCall(
        name=PROGRAM_CAPABILITY_NAME,
        arguments={
            "mode": "define_and_execute",
            "definition": _definition(followup_capability=followup_capability),
            "program_id": None,
            "primary_arguments": [{"name": "seconds", "value": seconds}],
            "followup_arguments": [{"name": "status", "value": 500}],
        },
    )


def _execute_call(program_id: str, *, seconds: int = 600) -> CapabilityCall:
    return CapabilityCall(
        name=PROGRAM_CAPABILITY_NAME,
        arguments={
            "mode": "execute",
            "definition": None,
            "program_id": program_id,
            "primary_arguments": [{"name": "seconds", "value": seconds}],
            "followup_arguments": [{"name": "status", "value": 500}],
        },
    )


def _spec(*, forbidden: list[str] | None = None) -> RunSpec:
    return RunSpec(
        run_id="run-program",
        environment="scripted",
        environment_profile=EnvironmentProfileRef(
            environment_id="scripted",
            version="profile-test",
        ),
        constraints=AgentConstraints(forbidden_capabilities=forbidden or []),
    )


def _environment(observations: list[Observation]) -> ProgrammableEnvironment:
    return ProgrammableEnvironment(
        ScriptedEnvironment(
            name="scripted",
            catalog=_catalog(),
            observations=observations,
            result=RunResult(
                run_id="run-program",
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="done",
            ),
        )
    )


@pytest.mark.parametrize(
    ("operator", "value", "accepted"),
    [
        ("exists", True, True),
        ("exists", False, True),
        ("exists", None, False),
        ("exists", 0, False),
        ("exists", 1, False),
        ("exists", "true", False),
        ("eq", None, True),
        ("ne", "running", True),
        ("gt", 0, True),
        ("lte", 0.5, True),
        ("gt", True, False),
        ("gt", "0", False),
        ("lt", None, False),
    ],
)
def test_condition_contract_agrees_between_schema_policy_and_executor(operator, value, accepted):
    async def scenario():
        environment = _environment([])
        capability = (await environment.bootstrap_capabilities()).find(PROGRAM_CAPABILITY_NAME)
        assert capability is not None
        arguments = _define_call().model_dump(mode="json")["arguments"]
        arguments["definition"]["condition"] = {
            "path": ["data", "status"],
            "operator": operator,
            "value": value,
        }
        for schema in (
            capability.input_schema,
            normalized_output_schema(capability.input_schema),
        ):
            Draft202012Validator.check_schema(schema)
            assert Draft202012Validator(schema).is_valid(arguments) is accepted
            assert (not validate_json_schema(arguments, schema, "arguments")) is accepted
        if accepted:
            ProgramInvocation.model_validate(arguments)
        else:
            with pytest.raises(ValidationError):
                ProgramInvocation.model_validate(arguments)
            state = await environment.start(_spec())
            observation = await environment.execute(
                CapabilityCall(name=PROGRAM_CAPABILITY_NAME, arguments=arguments), state
            )
            assert not observation.ok
            assert observation.data["code"] == "INVALID_PROGRAM"
            assert environment.telemetry("run-program").program_subcalls == 0

    asyncio.run(scenario())


def test_program_is_registered_reused_and_compacted_between_decisions() -> None:
    async def scenario() -> None:
        environment = _environment(
            [
                Observation(
                    action_kind="advance",
                    summary="advanced with errors",
                    data={"errors": 2, "large": [1, 2, 3]},
                ),
                Observation(
                    action_kind="inspect",
                    summary="two error groups",
                    data={"messages": ["capacity", "bug"], "large": [4, 5, 6]},
                ),
                Observation(
                    action_kind="advance",
                    summary="advanced again",
                    data={"errors": 1},
                ),
                Observation(
                    action_kind="inspect",
                    summary="one error group",
                    data={"messages": ["capacity"]},
                ),
            ]
        )
        state = await environment.start(_spec())
        bootstrap = await environment.bootstrap_capabilities()
        program_capability = bootstrap.find(PROGRAM_CAPABILITY_NAME)
        assert program_capability is not None
        assert "$ref" not in json.dumps(program_capability.input_schema)
        assert (
            validate_json_schema(
                _define_call().arguments,
                program_capability.input_schema,
                "arguments",
            )
            == []
        )

        first_call = _define_call()
        first_trace = await environment.execute(first_call, state)
        first_state = environment.reduce(state, first_call, first_trace)

        assert first_trace.ok
        first_subcalls = first_trace.data["subcalls"]
        assert isinstance(first_subcalls, list)
        assert len(first_subcalls) == 2
        assert "subcalls" not in first_state.latest_observation.data
        first_results = first_state.latest_observation.data["results"]
        assert isinstance(first_results, list)
        first_result = first_results[0]
        second_result = first_results[1]
        assert isinstance(first_result, dict)
        assert isinstance(second_result, dict)
        assert first_result["selected"] == {"data.errors": 2}
        assert second_result["selected"] == {"data.messages": ["capacity", "bug"]}
        program_id = first_state.latest_observation.data["program_id"]
        assert program_id == "advance_and_inspect@1"
        assert isinstance(program_id, str)
        current_catalog = await environment.capabilities(first_state)
        current_program_capability = current_catalog.find(PROGRAM_CAPABILITY_NAME)
        assert current_program_capability is not None
        description = current_program_capability.description
        assert program_id in description
        assert '"primary":"advance"' in description
        assert '"followup":"inspect"' in description

        second_call = _execute_call(program_id)
        second_trace = await environment.execute(second_call, first_state)
        second_state = environment.reduce(first_state, second_call, second_trace)

        assert second_state.latest_observation.data["created"] is False
        second_subcalls = second_trace.data["subcalls"]
        assert isinstance(second_subcalls, list)
        nested_calls = []
        for item in [*first_subcalls, *second_subcalls]:
            assert isinstance(item, dict)
            nested_calls.append(item["call"])
        assert nested_calls == [
            {"name": "advance", "arguments": {"seconds": 300}},
            {"name": "inspect", "arguments": {"status": 500}},
            {"name": "advance", "arguments": {"seconds": 600}},
            {"name": "inspect", "arguments": {"status": 500}},
        ]
        telemetry = environment.telemetry("run-program")
        assert telemetry.program_executions == 2
        assert telemetry.program_subcalls == 4

    asyncio.run(scenario())


def test_program_skips_followup_and_rejects_mutating_or_forbidden_delegates() -> None:
    async def scenario() -> None:
        environment = _environment(
            [Observation(action_kind="advance", summary="quiet", data={"errors": 0})]
        )
        state = await environment.start(_spec())
        call = _define_call()
        trace = await environment.execute(call, state)
        reduced = environment.reduce(state, call, trace)

        assert trace.ok
        assert trace.data["executed_calls"] == 1
        assert trace.data["skipped_followup"] is True
        results = reduced.latest_observation.data["results"]
        assert isinstance(results, list)
        first_result = results[0]
        assert isinstance(first_result, dict)
        assert first_result["selected"] == {"data.errors": 0}

        mutating = _define_call(followup_capability="advance")
        rejected = await environment.execute(mutating, reduced)
        assert not rejected.ok
        assert rejected.data["code"] == "INVALID_PROGRAM"
        assert "must be read-only" in rejected.summary

        constrained = _environment([])
        constrained_state = await constrained.start(_spec(forbidden=["inspect"]))
        forbidden = await constrained.execute(_define_call(), constrained_state)
        assert not forbidden.ok
        assert "run-forbidden capability 'inspect'" in forbidden.summary

    asyncio.run(scenario())


def test_program_keeps_large_subcall_in_trace_but_rejects_large_context_output() -> None:
    async def scenario() -> None:
        environment = _environment(
            [
                Observation(
                    action_kind="advance",
                    summary="large result",
                    data={"payload": "x" * 40_000},
                )
            ]
        )
        state = await environment.start(_spec())
        definition = _definition()
        definition["followup_capability"] = None
        definition["condition"] = None
        definition["primary_output_paths"] = [["data", "payload"]]
        definition["followup_output_paths"] = []
        call = CapabilityCall(
            name=PROGRAM_CAPABILITY_NAME,
            arguments={
                "mode": "define_and_execute",
                "definition": definition,
                "program_id": None,
                "primary_arguments": [{"name": "seconds", "value": 300}],
                "followup_arguments": [],
            },
        )

        trace = await environment.execute(call, state)
        reduced = environment.reduce(state, call, trace)

        subcalls = trace.data["subcalls"]
        assert isinstance(subcalls, list)
        assert "x" * 40_000 in json.dumps(subcalls)
        assert not reduced.latest_observation.ok
        assert reduced.latest_observation.data["code"] == "PROGRAM_OUTPUT_TOO_LARGE"
        assert "request fewer paths" in reduced.latest_observation.summary

    asyncio.run(scenario())


def test_runner_keeps_program_subcalls_in_trace_but_not_next_context() -> None:
    async def scenario() -> None:
        program_action = _define_call().model_dump(mode="json")
        reasoner = ScriptedReasoner(
            [
                {
                    "phase": "observe",
                    "facts": ["a bounded observation window is needed"],
                    "competing_hypotheses": ["errors may occur"],
                    "contradicting_evidence": [],
                    "previous_verification": {
                        "status": "not_applicable",
                        "evidence": [],
                    },
                    "strategy": "advance and inspect mechanically",
                    "selected_action": program_action,
                    "expected_result": ["the interval and any errors are returned"],
                    "verification": ["inspect the compact program results"],
                    "task_completed": False,
                },
                {
                    "phase": "finish",
                    "facts": ["the program returned the required evidence"],
                    "competing_hypotheses": ["the scripted run can finish"],
                    "contradicting_evidence": [],
                    "previous_verification": {
                        "status": "confirmed",
                        "evidence": ["the compact results contain grouped errors"],
                    },
                    "strategy": "finish",
                    "selected_action": {"name": "finish", "arguments": {}},
                    "expected_result": ["the run becomes terminal"],
                    "verification": ["the terminal observation confirms completion"],
                    "task_completed": True,
                },
            ]
        )
        environment = _environment(
            [
                Observation(
                    action_kind="advance",
                    summary="advanced",
                    data={"errors": 1, "large": list(range(100))},
                ),
                Observation(
                    action_kind="inspect",
                    summary="one group",
                    data={"messages": ["bug"], "large": list(range(100))},
                ),
                Observation(action_kind="finish", summary="done", terminal=True),
            ]
        )
        store = InMemoryRunStore()
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
            environment=environment,
            memory=NoMemory(),
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )

        result = await runner.run(RunSpec(run_id="run-program", environment="scripted"))

        traces = [
            event.payload
            for event in store.events
            if isinstance(event.payload, DecisionTracePayload)
        ]
        assert traces[0].observation is not None
        subcalls = traces[0].observation.data["subcalls"]
        assert isinstance(subcalls, list)
        assert len(subcalls) == 2
        second_context = decode_prompt_context(reasoner.requests[1].user_prompt)
        latest = second_context["environment_state"]["latest_observation"]
        assert "subcalls" not in latest["data"]
        assert latest["data"]["results"][1]["selected"] == {"data.messages": ["bug"]}
        assert "advance_and_inspect@1" in reasoner.requests[1].user_prompt
        assert result.metrics.program_executions == 1
        assert result.metrics.program_subcalls == 2
        assert result.metrics.capability_executions == 2
        assert result.metrics.action_counts == {"execute_program": 1, "finish": 1}

    asyncio.run(scenario())
