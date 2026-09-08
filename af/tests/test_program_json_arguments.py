import asyncio
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from tests.helpers import make_context
from tests.test_models import _decision
from tests.test_programmatic_environment import _define_call, _execute_call, _object_schema, _spec
from tests.test_runner import _output
from uptick_agent.core.models import Capability, CapabilityCatalog, Observation, RunResult
from uptick_agent.core.policy import DecisionPolicy
from uptick_agent.core.sgr import CurrentSGR, normalized_output_schema
from uptick_agent.environments.programmatic import ProgramInvocation, ProgrammableEnvironment
from uptick_agent.environments.scripted import ScriptedEnvironment


def _catalog():
    stop = _object_schema(
        {
            "new_errors": {"type": "integer", "minimum": 1},
            "codes": {
                "anyOf": [
                    {"type": "array", "items": {"type": "string", "pattern": "^[A-Z]+$"}},
                    {"type": "null"},
                ]
            },
        },
        ["new_errors", "codes"],
    )
    filters = {
        "type": "array",
        "maxItems": 80,
        "items": _object_schema(
            {"labels": {"type": "array", "items": {"type": "string"}}}, ["labels"]
        ),
    }
    return CapabilityCatalog(
        items=[
            Capability(
                name="advance",
                description="Advance, stopping on the supplied event condition.",
                input_schema=_object_schema(
                    {
                        "seconds": {"type": "integer", "minimum": 1},
                        "stop_when": {"anyOf": [stop, {"type": "null"}]},
                    },
                    ["seconds", "stop_when"],
                ),
                mutates_state=True,
            ),
            Capability(
                name="inspect",
                description="Inspect with nested filters.",
                input_schema=_object_schema({"filters": filters}, ["filters"]),
            ),
        ]
    )


class EventEnvironment(ScriptedEnvironment):
    def __init__(self):
        super().__init__(
            name="scripted",
            catalog=_catalog(),
            observations=[],
            result=RunResult(
                run_id="unused", status="completed", steps=0, duration_seconds=0, stop_reason=""
            ),
        )

    async def execute(self, call, state):
        self.calls.append(call.model_copy(deep=True))
        if call.name == "advance":
            stopped = call.arguments.get("stop_when") is not None
            elapsed = 17 if stopped else call.arguments["seconds"]
            return Observation(
                action_kind=call.name,
                summary="event" if stopped else "deadline",
                data={"errors": 1, "elapsed": elapsed},
            )
        return Observation(action_kind=call.name, summary="inspected", data={"messages": []})


def _arguments(call, condition):
    call.arguments["primary_arguments"] = [
        {"name": "seconds", "value": 523574},
        {"name": "stop_when", "value": deepcopy(condition)},
    ]
    # Exercise >64 items and objects containing nested arrays through the follow-up too.
    call.arguments["followup_arguments"] = [
        {"name": "filters", "value": [{"labels": ["capacity", "service"]} for _ in range(65)]}
    ]
    return call


@pytest.mark.parametrize("condition", [{"new_errors": 1, "codes": None}, None])
@pytest.mark.parametrize("root_keyword", [None, "anyOf", "oneOf", "allOf"])
def test_nested_arguments_reach_native_calls_on_definition_and_reuse(condition, root_keyword):
    async def scenario():
        base = EventEnvironment()
        if root_keyword is not None:
            for capability in base._catalog.items:
                capability.input_schema = {root_keyword: [capability.input_schema]}
        environment = ProgrammableEnvironment(base)
        state = await environment.start(_spec())
        original_catalog = (await base.bootstrap_capabilities()).model_dump(mode="json")
        for call in [
            _arguments(_define_call(), condition),
            _arguments(_execute_call("advance_and_inspect@1"), condition),
        ]:
            before = call.model_dump(mode="json")
            catalog = await environment.capabilities(state)
            context = make_context(capabilities=catalog)
            request = CurrentSGR().build_request(context)
            envelope = _output("execute_program")
            envelope["selected_action"] = before
            schema = normalized_output_schema(request.output_schema)
            Draft202012Validator.check_schema(schema)
            assert Draft202012Validator(schema).is_valid(envelope)
            assert (
                DecisionPolicy()
                .validate(context, _decision(call.name, call.arguments, completed=False))
                .accepted
            )
            assert (
                ProgramInvocation.model_validate(call.arguments).model_dump(mode="json")
                == call.arguments
            )

            observation = await environment.execute(call, state)
            state = environment.reduce(state, call, observation)
            assert observation.ok
            assert base.calls[-2].arguments == {"seconds": 523574, "stop_when": condition}
            assert base.calls[-1].arguments == {
                "filters": [{"labels": ["capacity", "service"]} for _ in range(65)]
            }
            trace = observation.model_dump(mode="json")
            assert trace["data"]["subcalls"][0]["observation"]["data"]["elapsed"] == (
                17 if condition is not None else 523574
            )
            assert call.model_dump(mode="json") == before
        assert (await base.bootstrap_capabilities()).model_dump(mode="json") == original_catalog

    asyncio.run(scenario())


def test_native_enum_object_is_data_not_a_program_schema_reference():
    async def scenario():
        base = EventEnvironment()
        capability = base._catalog.find("advance")
        assert capability is not None
        literal = {"$ref": "literal-data"}
        literal_schema = _object_schema({"$ref": {"type": "string"}}, ["$ref"])
        literal_schema["enum"] = [literal]
        capability.input_schema = _object_schema(
            {"seconds": {"type": "integer"}, "stop_when": literal_schema},
            ["seconds", "stop_when"],
        )
        environment = ProgrammableEnvironment(base)
        state = await environment.start(_spec())
        catalog = await environment.capabilities(state)
        request = CurrentSGR().build_request(make_context(capabilities=catalog))
        call = _arguments(_define_call(), literal)
        envelope = _output("execute_program")
        envelope["selected_action"] = call.model_dump(mode="json")
        assert Draft202012Validator(normalized_output_schema(request.output_schema)).is_valid(
            envelope
        )
        observation = await environment.execute(call, state)
        assert observation.ok
        assert base.calls[0].arguments["stop_when"] == literal

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "condition",
    [
        {"new_errors": 0, "codes": None},
        {"new_errors": "1", "codes": None},
        {"new_errors": True, "codes": None},
        {"new_errors": 1},
        {"new_errors": 1, "codes": None, "extra": True},
        {"new_errors": 1, "codes": ["not-an-allowed-code"]},
    ],
)
def test_invalid_nested_argument_is_rejected_before_native_io(condition):
    async def scenario():
        base = EventEnvironment()
        environment = ProgrammableEnvironment(base)
        state = await environment.start(_spec())
        observation = await environment.execute(_arguments(_define_call(), condition), state)
        assert not observation.ok
        assert base.calls == []
        assert environment.telemetry("run-program").program_subcalls == 0

    asyncio.run(scenario())


def test_invalid_followup_array_is_rejected_before_followup_io():
    async def scenario():
        base = EventEnvironment()
        environment = ProgrammableEnvironment(base)
        state = await environment.start(_spec())
        call = _arguments(_define_call(), {"new_errors": 1, "codes": None})
        call.arguments["followup_arguments"] = [
            {"name": "filters", "value": [{"labels": []} for _ in range(81)]}
        ]
        observation = await environment.execute(call, state)
        assert not observation.ok
        assert [call.name for call in base.calls] == ["advance"]

    asyncio.run(scenario())
