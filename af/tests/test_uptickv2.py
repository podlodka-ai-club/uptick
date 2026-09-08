import asyncio
import base64
import json

import httpx
import pytest
from jsonschema import Draft202012Validator

from tests.helpers import decode_prompt_context
from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.models import CapabilityCall, EnvironmentProfileRef, RunSpec
from uptick_agent.core.policy import DecisionPolicy
from uptick_agent.core.sgr import CurrentSGR, normalized_output_schema
from uptick_agent.core.trace_models import DecisionTracePayload
from uptick_agent.environments.discovered.schema import compile_tools, model_schema, validate
from uptick_agent.environments.discovered.session import DiscoveredRunResult
from uptick_agent.environments.programmatic import ProgrammableLauncher
from uptick_agent.environments.uptickv2 import UptickV2Environment
from uptick_agent.memory import NoMemory
from uptick_agent.reasoners.scripted import ScriptedReasoner
from uptick_agent.runtime.bootstrap import ScriptedEnvironmentBootstrapper
from uptick_agent.runtime.runner import AgentRunner
from uptick_agent.store import InMemoryRunStore

ROOT = "/v2/runs/{run_id}"
CATALOG = ROOT + "/actions"
SOURCE = f"Use /contract.yaml for OpenAPI and GET {CATALOG} with panel Basic Auth."


def object_schema(properties=None, required=None):
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def api():
    run = {"name": "run_id", "in": "path", "required": True, "schema": {"type": "string"}}
    basic = [{"Panel": []}]
    overview_response = {
        "200": {
            "content": {
                "application/json": {
                    "schema": object_schema(
                        {
                            "status": {"type": "string", "enum": ["running", "completed"]},
                            "clock": object_schema(
                                {"now": {"type": "string"}, "end": {"type": "string"}}
                            ),
                        }
                    )
                }
            }
        }
    }
    return {
        "openapi": "3.1.1",
        "info": {"description": "Keep uptime at 99% with minimal cost."},
        "components": {"securitySchemes": {"Panel": {"type": "http", "scheme": "basic"}}},
        "paths": {
            ROOT + "/status": {
                "parameters": [run],
                "get": {
                    "operationId": "inspectWorld",
                    "summary": "Read current world status",
                    "responses": overview_response,
                },
            },
            ROOT + "/mail": {
                "parameters": [run],
                "get": {"operationId": "readMail", "summary": "Read delivered messages"},
            },
            ROOT + "/access/{credential_id}": {
                "parameters": [run],
                "get": {
                    "operationId": "registerAccess",
                    "security": basic,
                    "parameters": [
                        {
                            "name": "credential_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "pattern": "^[a-z0-9-]+$"},
                        }
                    ],
                },
            },
            ROOT + "/jump": {
                "parameters": [run],
                "post": {
                    "operationId": "waitForEvent",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": object_schema(
                                    {
                                        "request_id": {"type": "string"},
                                        "seconds": {"type": "integer", "minimum": 300},
                                        "filter": {"type": "string"},
                                    },
                                    ["request_id", "seconds"],
                                )
                            }
                        }
                    },
                },
            },
            CATALOG: {
                "parameters": [run],
                "get": {"operationId": "listActions", "security": basic},
                "post": {"operationId": "runAction", "security": basic},
            },
        },
    }


def catalog():
    return {
        "commands": [
            {
                "command": "engine.inspect",
                "description": "Read engine resources",
                "params_schema": object_schema(
                    {"server_id": {"type": "string", "pattern": "^[a-z0-9-]+$"}}, ["server_id"]
                ),
                "result_schema": object_schema(),
                "target_auth_required": True,
                "execution": "synchronous",
            },
            {
                "command": "engine.prepare",
                "description": "Provision engine asynchronously",
                "params_schema": object_schema({"name": {"type": "string"}}, ["name"]),
                "result_schema": object_schema(),
                "target_auth_required": False,
                "execution": "asynchronous",
            },
        ]
    }


def discovery_outputs():
    return [
        {
            "openapi_path": "/contract.yaml",
            "command_catalog_path": CATALOG,
            "command_catalog_requires_panel_auth": True,
        },
        {
            "objective": "Keep uptime >= 99%, minimize cost",
            "tools": [
                {
                    "name": name,
                    "mutates_state": mutation,
                    "role": role,
                    "notes": "Use published results.",
                }
                for name, mutation, role in [
                    ("inspectWorld", False, "overview"),
                    ("readMail", False, "inbox"),
                    ("registerAccess", False, "credentials"),
                    ("waitForEvent", True, "advance"),
                    ("engine.inspect", False, "observation"),
                    ("engine.prepare", True, "observation"),
                ]
            ],
            "status_field": "status",
            "completed_status": "completed",
            "clock_field": "clock",
            "time_field": "now",
            "end_time_field": "end",
        },
    ]


class World:
    def __init__(self):
        self.requests = []
        self.completed = False
        self.starts = 0
        self.auth_fail = False

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        clock = {
            "now": "2030-01-02T00:00:00Z" if self.completed else "2030-01-01T00:00:00Z",
            "end": "2030-01-02T00:00:00Z",
        }
        if path == "/v2/start":
            self.starts += 1
            return httpx.Response(
                201,
                json={
                    "run_id": "remote-1",
                    "commands_markdown": SOURCE,
                    "control_panel_auth": {
                        "scheme": "basic",
                        "username": "panel-user",
                        "password": "panel-secret",
                    },
                },
            )
        if path == "/contract.yaml":
            return httpx.Response(200, json=api())
        if path.endswith("/actions") or "/access/" in path:
            expected = "Basic " + base64.b64encode(b"panel-user:panel-secret").decode()
            assert request.headers["authorization"] == expected
        else:
            assert "authorization" not in request.headers
        if path.endswith("/actions"):
            if request.method == "GET":
                return httpx.Response(200, json=catalog())
            if self.auth_fail:
                return httpx.Response(
                    403, json={"error": "CREDENTIALS_EXPIRED", "message": "server-secret"}
                )
            assert body is not None
            if body["command"] == "engine.inspect":
                assert body["target_auth"] == {"username": "operator", "password": "server-secret"}
                return httpx.Response(200, json={"clock": clock, "result": {"healthy": True}})
            return httpx.Response(
                202, json={"clock": clock, "operation_id": "op1", "status": "pending"}
            )
        if "/access/" in path:
            return httpx.Response(
                200,
                json={
                    "clock": clock,
                    "credential": {
                        "credential_id": "server-v1",
                        "resource_id": "engine-1",
                        "username": "operator",
                        "password": "server-secret",
                    },
                },
            )
        if path.endswith("/mail"):
            return httpx.Response(
                200,
                json={
                    "clock": clock,
                    "messages": [
                        {"description": "credential_id=server-v1; password: not-yet-known"}
                    ],
                },
            )
        if path.endswith("/jump"):
            assert body is not None
            assert "filter" not in body
            self.completed = True
            clock["now"] = clock["end"]
        return httpx.Response(
            200,
            json={
                "clock": clock,
                "status": "completed" if self.completed else "running",
                "costs": {"total_cost_minor": 12},
                "availability": {"uptime_ratio": 1},
            },
        )


def spec():
    return RunSpec(run_id="run-v2-test", environment="uptickv2", parameters={"seed": 42})


def launcher(tmp_path, world, reasoner=None, participant_token_env=None):
    return UptickV2Environment(
        "http://world.test",
        reasoner=reasoner or ScriptedReasoner(discovery_outputs()),
        cache_directory=tmp_path,
        participant_token_env=participant_token_env,
        client=httpx.AsyncClient(
            base_url="http://world.test", transport=httpx.MockTransport(world)
        ),
    )


def test_participant_token_is_sent_at_start_but_redacted_from_discovery(tmp_path, monkeypatch):
    async def scenario():
        token = "test-participant-secret"
        monkeypatch.setenv("TEST_UPTICK_PARTICIPANT_TOKEN", token)
        world = World()

        def transport(request):
            response = world(request)
            if request.url.path == "/v2/start":
                body = response.json()
                body["commands_markdown"] += f"\nParticipant: {token}\n"
                return httpx.Response(201, json=body)
            assert token not in request.content.decode()
            return response

        env = launcher(tmp_path, transport, participant_token_env="TEST_UPTICK_PARTICIPANT_TOKEN")
        try:
            session = await env.run(spec())
            payload = json.loads(world.requests[0].content)
            assert payload["participant_token"] == token
            assert payload["request_id"] == spec().run_id
            assert world.starts == 1
            assert token not in await session.initialize()
            assert not any(token in p.read_text() for p in tmp_path.rglob("*.json"))
        finally:
            await env.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [None, "", "  "])
def test_missing_participant_token_fails_before_start(tmp_path, monkeypatch, value):
    async def scenario():
        variable = "TEST_UPTICK_PARTICIPANT_TOKEN"
        if value is None:
            monkeypatch.delenv(variable, raising=False)
        else:
            monkeypatch.setenv(variable, value)
        world = World()
        env = launcher(tmp_path, world, participant_token_env=variable)
        try:
            with pytest.raises(ValueError, match="participant token environment variable"):
                await env.run(spec())
            assert world.requests == []
        finally:
            await env.aclose()

    asyncio.run(scenario())


def test_run_first_discovery_and_gameplay_end_to_end(tmp_path):
    async def scenario():
        world = World()
        store = InMemoryRunStore()
        decision = {
            "phase": "observe",
            "facts": ["world is running"],
            "competing_hypotheses": ["wait"],
            "contradicting_evidence": [],
            "previous_verification": {"status": "not_applicable", "evidence": []},
            "strategy": "observe the full run",
            "selected_action": {
                "name": "waitForEvent",
                "arguments": {"seconds": 300, "filter": None},
            },
            "expected_result": ["world completes"],
            "verification": ["check status"],
            "task_completed": False,
        }
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=ScriptedReasoner([decision]), sgr=CurrentSGR()),
            environment=ProgrammableLauncher(launcher(tmp_path, world)),
            memory=NoMemory(),
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )
        result = await runner.run(spec())
        assert isinstance(result, DiscoveredRunResult)
        assert result.status == "completed" and result.steps == 1 and not result.forced
        assert result.final_state["costs"] == {"total_cost_minor": 12}
        assert world.starts == 1
        assert world.requests[0].url.path == "/v2/start"
        assert "participant_token" not in json.loads(world.requests[0].content)
        assert not any("panel-secret" in p.read_text() for p in tmp_path.rglob("*.json"))

    asyncio.run(scenario())


@pytest.mark.parametrize("payload_kind", ["array", "string", "nested", "large_object"])
def test_full_tool_responses_reach_model_context(tmp_path, payload_kind):
    async def scenario():
        payload = {
            "array": [{"timestamp": i, "value": i * 2} for i in range(172)],
            "string": "Данные " * 500 + "END",
            "large_object": {f"field_{i}": "x" * 1500 for i in range(20)},
        }
        nested = {"leaf": "deep evidence"}
        for _ in range(10):
            nested = {"child": nested}
        payload["nested"] = nested
        world = World()
        responses = {}

        def transport(request):
            response = world(request)
            if request.url.path.endswith(("/status", "/mail")):
                body = response.json()
                body["evidence"] = payload[payload_kind]
                responses.setdefault(request.url.path.rsplit("/", 1)[-1], body)
                return httpx.Response(200, json=body)
            return response

        reasoner = ScriptedReasoner(
            [
                {
                    "phase": "observe",
                    "facts": ["Read the world response."],
                    "competing_hypotheses": ["The world can advance."],
                    "contradicting_evidence": [],
                    "previous_verification": {
                        "status": "not_applicable" if step == 0 else "confirmed",
                        "evidence": [] if step == 0 else ["Mail was returned."],
                    },
                    "strategy": "Read evidence, then advance.",
                    "selected_action": call,
                    "expected_result": ["Receive the requested response."],
                    "verification": ["Inspect the response."],
                    "task_completed": False,
                }
                for step, call in enumerate(
                    [
                        {"name": "readMail", "arguments": {}},
                        {"name": "waitForEvent", "arguments": {"seconds": 300}},
                    ]
                )
            ]
        )
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
            environment=ProgrammableLauncher(launcher(tmp_path, transport)),
            memory=NoMemory(),
            run_store=InMemoryRunStore(),
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )
        result = await runner.run(spec())
        assert result.status == "completed" and result.steps == 2
        contexts = [decode_prompt_context(r.user_prompt) for r in reasoner.requests]
        initial = contexts[0]["environment_state"]["latest_observation"]["data"]
        assert initial == responses["status"]
        state = contexts[1]["environment_state"]
        # The entire sanitized response, including its tail, must reach the model.
        expected = responses["mail"] | {
            "messages": [{"description": "credential_id=server-v1; password: [private]"}]
        }
        assert state["latest_observation"]["data"] == expected
        assert state["decision_view"]["recent_observations"]["readMail"]["observation"] == expected
        assert "not-yet-known" not in reasoner.requests[1].user_prompt

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "condition",
    [None, {"path": ["data", "clock", "now"], "operator": "exists", "value": True}],
    ids=["unconditional", "exists_at_observation_root"],
)
@pytest.mark.parametrize("structured_stop", [False, True], ids=["scalar_args", "nested_args"])
def test_program_advances_then_verifies_operation_in_next_model_context(
    tmp_path, condition, structured_stop
):
    async def scenario():
        world = World()
        document = api()
        stop = {"new_log_errors": 1, "error_codes": None}
        document["components"]["schemas"] = {
            "StopCondition": object_schema(
                {
                    "new_log_errors": {"type": "integer", "minimum": 1},
                    "error_codes": {
                        "anyOf": [
                            {"type": "array", "items": {"type": "string"}, "minItems": 1},
                            {"type": "null"},
                        ]
                    },
                },
                ["new_log_errors", "error_codes"],
            )
        }
        jump_schema = document["paths"][ROOT + "/jump"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        jump_schema["properties"]["stop_when"] = {"$ref": "#/components/schemas/StopCondition"}
        document["paths"][ROOT + "/operation"] = {
            "parameters": document["paths"][ROOT + "/status"]["parameters"],
            "get": {
                "operationId": "inspectOperation",
                "parameters": [
                    {
                        "name": "operation_id",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
            },
        }
        discovery = discovery_outputs()
        discovery[1]["tools"].append(
            {
                "name": "inspectOperation",
                "mutates_state": False,
                "role": "observation",
                "notes": "Check status by operation_id after the wait.",
            }
        )
        calls = []
        clock = {"now": "2030-01-01T00:05:00Z", "end": "2030-01-02T00:00:00Z"}

        def transport(request):
            if request.url.path == "/contract.yaml":
                return httpx.Response(200, json=document)
            if request.url.path.endswith("/jump") and not calls:
                body = json.loads(request.content)
                assert body["seconds"] == 300
                if structured_stop:
                    assert body["stop_when"] == stop
                else:
                    assert "stop_when" not in body
                calls.append("advance")
                return httpx.Response(200, json={"clock": clock, "status": "running"})
            if request.url.path.endswith("/operation"):
                assert calls == ["advance"]
                assert request.url.params["operation_id"] == "op1"
                calls.append("inspect")
                return httpx.Response(
                    200, json={"clock": clock, "operation_id": "op1", "status": "succeeded"}
                )
            return world(request)

        program = {
            "name": "execute_program",
            "arguments": {
                "mode": "define_and_execute",
                "program_id": None,
                "definition": {
                    "name": "WaitAndVerify",
                    "description": "Advance to readiness and verify the operation.",
                    "primary_capability": "waitForEvent",
                    "followup_capability": "inspectOperation",
                    "condition": condition,
                    "primary_output_paths": [["data", "clock", "now"]],
                    "followup_output_paths": [["data", "status"], ["data", "operation_id"]],
                },
                "primary_arguments": [{"name": "seconds", "value": 300}],
                "followup_arguments": [{"name": "operation_id", "value": "op1"}],
            },
        }
        if structured_stop:
            program["arguments"]["primary_arguments"].append({"name": "stop_when", "value": stop})
        reasoner = ScriptedReasoner(
            [
                {
                    "phase": "verify",
                    "facts": ["Prepare the resource and verify its operation."],
                    "competing_hypotheses": ["The operation can complete after the wait."],
                    "contradicting_evidence": [],
                    "previous_verification": {
                        "status": "not_applicable" if step == 0 else "confirmed",
                        "evidence": [] if step == 0 else ["The preceding call succeeded."],
                    },
                    "strategy": "Prepare, wait, verify, then complete the world.",
                    "selected_action": action,
                    "expected_result": ["Return the documented result."],
                    "verification": ["Inspect the operation status."],
                    "task_completed": False,
                }
                for step, action in enumerate(
                    [
                        {"name": "engine.prepare", "arguments": {"name": "new"}},
                        program,
                        {"name": "waitForEvent", "arguments": {"seconds": 300}},
                    ]
                )
            ]
        )
        store = InMemoryRunStore()
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
            environment=ProgrammableLauncher(
                launcher(tmp_path, transport, ScriptedReasoner(discovery))
            ),
            memory=NoMemory(),
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )
        result = await runner.run(spec())
        assert result.status == "completed" and result.steps == 3
        decision = next(
            e.payload.decision
            for e in store.events
            if isinstance(e.payload, DecisionTracePayload) and e.payload.iteration == 2
        )
        assert decision is not None
        assert Draft202012Validator(
            normalized_output_schema(reasoner.requests[1].output_schema)
        ).is_valid(decision.envelope.model_dump(mode="json"))
        assert calls == ["advance", "inspect"]
        assert result.metrics.program_executions == 1 and result.metrics.program_subcalls == 2
        context = decode_prompt_context(reasoner.requests[2].user_prompt)
        observation = context["environment_state"]["latest_observation"]
        assert observation["ok"] and not observation["data"]["skipped_followup"]
        primary, followup = observation["data"]["results"]
        assert primary["selected"] == {"data.clock.now": clock["now"]}
        assert followup["selected"] == {"data.status": "succeeded", "data.operation_id": "op1"}
        assert primary["missing_paths"] == followup["missing_paths"] == []

    asyncio.run(scenario())


def test_batch_uses_discovered_http_contract_and_preserves_all_sanitized_results(tmp_path):
    async def scenario():
        world = World()
        submissions = []

        def transport(request):
            response = world(request)
            if request.method == "POST" and request.url.path.endswith("/actions"):
                body = json.loads(request.content)
                if body["command"] == "engine.prepare":
                    submissions.append(body)
                    number = len(submissions)
                    payload = response.json() | {
                        "operation_id": f"operation-{number}",
                        "evidence": [{"sample": i} for i in range(172)],
                        "credential": {
                            "credential_id": f"engine-v{number}",
                            "username": "operator",
                            "password": "server-secret",
                        },
                    }
                    return httpx.Response(202, json=payload)
            return response

        wrapper = ProgrammableLauncher(launcher(tmp_path, transport))
        session = await wrapper.run(spec())
        state = await session.start(
            spec().model_copy(
                update={
                    "environment_profile": EnvironmentProfileRef(
                        environment_id="uptickv2", version="test"
                    )
                }
            )
        )
        # Identical actions are still distinct requests, not implicitly deduplicated.
        call = CapabilityCall(
            name="execute_batch",
            arguments={
                "items": [
                    {"name": "engine.prepare", "arguments": {"name": "new"}} for _ in range(9)
                ],
                "release": [],
                "retain_results": True,
            },
        )
        observation = await session.execute(call, state)
        state = session.reduce(state, call, observation)
        assert observation.ok
        assert len(submissions) == 9
        assert len({x["request_id"] for x in submissions}) == 9
        assert not any(x.url.path.endswith("/jump") for x in world.requests)
        records = observation.model_dump(mode="json")["data"]["items"]
        for i, record in enumerate(records, 1):
            data = record["observation"]["data"]
            assert data["operation_id"] == f"operation-{i}"
            assert len(data["evidence"]) == 172
            assert data["credential"] == {
                "credential_id": f"engine-v{i}",
                "credential_ref": f"engine-v{i}",
            }
        assert "server-secret" not in observation.model_dump_json()
        assert "panel-secret" not in state.model_dump_json()
        read = CapabilityCall(
            name="execute_batch",
            arguments={
                "items": [
                    {
                        "name": "engine.inspect",
                        "arguments": {"server_id": "engine-1", "credential_ref": "engine-v1"},
                    }
                ],
                "release": [],
                "retain_results": False,
            },
        )
        response = await session.execute(read, state)
        state = session.reduce(state, read, response)
        assert response.ok
        retained = state.model_dump(mode="json")["decision_view"]["batch_results"]["retained"]
        assert retained == [observation.model_dump(mode="json")]
        assert "server-secret" not in state.model_dump_json()
        await wrapper.aclose()

    asyncio.run(scenario())


def test_credentials_are_handles_and_validation_precedes_http(tmp_path):
    async def scenario():
        world = World()
        session = await launcher(tmp_path, world).run(spec())
        state = await session.start(
            spec().model_copy(
                update={
                    "environment_profile": EnvironmentProfileRef(
                        environment_id="uptickv2", version="test"
                    )
                }
            )
        )
        mail = await session.execute(CapabilityCall(name="readMail"), state)
        assert "not-yet-known" not in mail.model_dump_json()
        registered = await session.execute(
            CapabilityCall(name="registerAccess", arguments={"credential_id": "server-v1"}), state
        )
        credential = registered.data["credential"]
        assert isinstance(credential, dict) and credential["credential_ref"] == "server-v1"
        assert "server-secret" not in registered.model_dump_json()
        good = CapabilityCall(
            name="engine.inspect",
            arguments={"server_id": "engine-1", "credential_ref": "server-v1"},
        )
        assert (await session.execute(good, state)).ok
        before = len(world.requests)
        rejected = await session.execute(
            CapabilityCall(
                name="engine.inspect",
                arguments={"server_id": "../escape", "credential_ref": "server-v1"},
            ),
            state,
        )
        assert not rejected.ok and len(world.requests) == before
        world.auth_fail = True
        error = await session.execute(good, state)
        assert not error.ok and error.data["error"] == "CREDENTIALS_EXPIRED"
        assert "server-secret" not in error.model_dump_json()
        pending = await session.execute(
            CapabilityCall(name="engine.prepare", arguments={"name": "new"}), state
        )
        assert not pending.terminal

    asyncio.run(scenario())


def test_schema_compiler_preserves_server_constraints_and_optional_nulls(tmp_path):
    async def scenario():
        session = await launcher(tmp_path, World()).run(spec())
        capabilities = await session.bootstrap_capabilities()
        tool = capabilities.find("waitForEvent")
        assert tool is not None
        validate({"seconds": 300, "filter": None}, tool.input_schema)
        strict = normalized_output_schema(tool.input_schema)
        required = strict["required"]
        assert isinstance(required, list) and set(required) == {"seconds", "filter"}
        assert "engine.prepare" in [c.name for c in capabilities.items]
        with pytest.raises(ValueError):
            validate({"seconds": 1}, tool.input_schema)

    asyncio.run(scenario())


def test_cache_reuses_meanings_but_launches_and_checks_each_world(tmp_path):
    async def scenario():
        world = World()
        await launcher(tmp_path, world).run(spec())
        no_model = ScriptedReasoner([])
        await launcher(tmp_path, world, no_model).run(spec())
        assert world.starts == 2 and not no_model.requests
        assert sum(r.url.path == "/contract.yaml" for r in world.requests) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path", ["http://outside.test/spec", "//outside.test/spec", "/../spec", "/%2e%2e/spec"]
)
def test_discovery_rejects_paths_outside_origin_before_request(tmp_path, path):
    async def scenario():
        world = World()
        outputs = discovery_outputs()
        outputs[0]["openapi_path"] = path
        with pytest.raises(ValueError):
            await launcher(tmp_path, world, ScriptedReasoner(outputs)).run(spec())
        assert len(world.requests) == 1

    asyncio.run(scenario())


def test_external_schema_references_are_not_fetched():
    document = api()
    document["paths"][ROOT + "/status"]["get"]["parameters"] = [{"$ref": "http://outside.test/ref"}]
    with pytest.raises(ValueError, match="local"):
        compile_tools(document, catalog(), CATALOG)


def test_model_schema_projection_keeps_full_validation_at_execution_boundary():
    schema = object_schema(
        {
            "match": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": False,
                "properties": {"region": {"type": "string", "pattern": "^[A-Z]{2}$"}},
            }
        },
        ["match"],
    )
    projected = model_schema(schema)
    assert "minProperties" not in json.dumps(projected)
    with pytest.raises(ValueError, match="minProperties"):
        validate({"match": {}}, schema)
    with pytest.raises(ValueError, match="pattern"):
        validate({"match": {"region": "invalid"}}, schema)


def test_hallucinated_lifecycle_fields_are_rejected(tmp_path):
    async def scenario():
        outputs = discovery_outputs()
        outputs[1]["time_field"] = "invented_time"
        world = World()
        with pytest.raises(ValueError, match="lifecycle"):
            await launcher(tmp_path, world, ScriptedReasoner(outputs)).run(spec())
        assert not any(r.url.path.endswith("/status") for r in world.requests)
        receipt = json.loads((tmp_path / "launches" / "run-v2-test.json").read_text())
        assert receipt["status"] == "discovery_failed"
        assert receipt["simulator_run_id"] == "remote-1"
        assert "panel-secret" not in json.dumps(receipt)

    asyncio.run(scenario())


def test_ambiguous_mutation_reuses_request_id_but_new_reads_do_not(tmp_path):
    async def scenario():
        world = World()
        original = world.__call__
        failed = False
        attempts = []

        def transport(request):
            nonlocal failed
            if request.method == "POST" and request.url.path.endswith("/actions"):
                attempts.append(json.loads(request.content)["request_id"])
                if not failed:
                    failed = True
                    raise httpx.ReadTimeout("ambiguous write", request=request)
            return original(request)

        session = await launcher(tmp_path, transport).run(spec())
        state = await session.start(
            spec().model_copy(
                update={
                    "environment_profile": EnvironmentProfileRef(
                        environment_id="uptickv2", version="test"
                    )
                }
            )
        )
        call = CapabilityCall(name="engine.prepare", arguments={"name": "new"})
        assert not (await session.execute(call, state)).ok
        assert (await session.execute(call, state)).ok
        assert (await session.execute(call, state)).ok
        assert attempts[0] == attempts[1] and attempts[1] != attempts[2]

    asyncio.run(scenario())
