import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIConnectionError, AsyncOpenAI, AuthenticationError

from uptick_agent.llm import (
    GenerationSettings,
    LlmAuthenticationError,
    LlmConfigurationError,
    LlmMessage,
    LlmProviderConfig,
    LlmProviderError,
    LlmProviderRegistry,
    LlmStructuredOutputError,
    LlmTransientError,
    StructuredGenerationRequest,
    TextGenerationRequest,
    serialize_structured_generation_request,
)
from uptick_agent.llm.openai import OpenAILlmClient, OpenAISGRModel
from uptick_agent.models import DecisionContext, NextStep, ToolResult, V1NextStep, V2NextStep


def _decision() -> NextStep:
    return NextStep.model_validate(
        {
            "current_situation": "the run has started",
            "hypothesis": "an overview will establish the baseline",
            "remaining_steps": ["inspect the overview"],
            "task_completed": False,
            "action": {"kind": "get_overview"},
        }
    )


def _v1_decision() -> V1NextStep:
    return V1NextStep.model_validate(_decision().model_dump())


def _decision_message(**overrides: Any) -> Any:
    values = {
        "content": _decision().model_dump_json(),
        "refusal": None,
        "tool_calls": None,
        "function_call": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeCompletions:
    def __init__(self, message: Any) -> None:
        self.message = message
        self.finish_reason = "stop"
        self.create_calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=self.message, finish_reason=self.finish_reason)]
        )


class FakeOpenAI:
    def __init__(self, message: Any) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(message))


def test_openai_client_returns_neutral_result_and_rejects_refusals() -> None:
    async def scenario() -> None:
        fake = FakeOpenAI(_decision_message())
        client = OpenAILlmClient(model="test", client=fake)
        request = StructuredGenerationRequest(
            response_model=NextStep,
            messages=(LlmMessage(role="user", content="choose"),),
        )

        result = await client.generate_structured(request)

        assert result.value == _decision()
        assert result.provider == "openai"
        assert fake.chat.completions.create_calls[0]["messages"] == [
            {"role": "user", "content": "choose"}
        ]
        response_format = fake.chat.completions.create_calls[0]["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["name"] == "NextStep"
        assert response_format["json_schema"]["strict"] is True
        assert "anyOf" in response_format["json_schema"]["schema"]["properties"]["action"]
        assert client.capabilities.structured_generation
        assert client.capabilities.text_generation

        fake.chat.completions.message = _decision_message(refusal="policy")
        with pytest.raises(LlmStructuredOutputError, match="refused"):
            await client.generate_structured(request)

        fake.chat.completions.message = _decision_message(tool_calls=[object()], content="ignored")
        with pytest.raises(LlmProviderError, match="tool use"):
            await client.generate_text(TextGenerationRequest(messages=request.messages))

    asyncio.run(scenario())


def test_openai_decision_facade_preserves_decision_model_behavior() -> None:
    async def scenario() -> None:
        fake = FakeOpenAI(_decision_message())
        model = OpenAISGRModel(model="test", client=fake)
        context = DecisionContext(
            objective="keep healthy",
            run_id="run-1",
            seed=1,
            iteration=1,
            max_steps=2,
            latest_result=ToolResult(action_kind="start", summary="started"),
        )

        assert await model.decide(context) == _v1_decision()

    asyncio.run(scenario())


def test_openai_structured_v2_uses_normalized_wire_schema_and_validates_content() -> None:
    async def scenario() -> None:
        response_value = V2NextStep.model_validate(
            {
                "current_situation": "inspect the inbox",
                "hypothesis": "the run may have operator messages",
                "remaining_steps": [],
                "task_completed": False,
                "action": {"kind": "get_inbox"},
            }
        )
        received: list[dict[str, Any]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            received.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": response_value.model_dump_json(),
                                "refusal": None,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        sdk_http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.openai.com/v1"
        )
        sdk_client = AsyncOpenAI(api_key="test-key", http_client=sdk_http_client)
        client = OpenAILlmClient(model="test", client=sdk_client)
        request = StructuredGenerationRequest(
            response_model=V2NextStep,
            messages=(LlmMessage(role="user", content="choose"),),
        )

        result = await client.generate_structured(request)

        assert isinstance(result.value, V2NextStep)
        assert result.value.action.kind == "get_inbox"
        assert len(received) == 1
        response_format = received[0]["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["name"] == "V2NextStep"
        schema = response_format["json_schema"]["schema"]
        schema_nodes = json.dumps(schema)
        assert '"oneOf"' not in schema_nodes
        assert '"discriminator"' not in schema_nodes
        assert '"default"' not in schema_nodes
        assert "anyOf" in schema["properties"]["action"]

        await sdk_client.close()

    asyncio.run(scenario())


def test_openai_structured_rejects_incomplete_sdk_results_before_validation() -> None:
    async def scenario() -> None:
        fake = FakeOpenAI(_decision_message())
        client = OpenAILlmClient(model="test", client=fake)
        request = StructuredGenerationRequest(
            response_model=NextStep,
            messages=(LlmMessage(role="user", content="choose"),),
        )

        fake.chat.completions.finish_reason = "length"
        with pytest.raises(LlmStructuredOutputError, match="incomplete"):
            await client.generate_structured(request)

    asyncio.run(scenario())


def test_structured_request_serialization_is_json_safe_and_deterministic() -> None:
    request = StructuredGenerationRequest(
        model="requested-model",
        response_model=NextStep,
        settings=GenerationSettings(temperature=0, max_output_tokens=123),
        messages=(
            LlmMessage(role="system", content="system instructions"),
            LlmMessage(role="user", content="user evidence"),
        ),
    )

    first = serialize_structured_generation_request(request)
    second = serialize_structured_generation_request(request)

    assert first == second
    assert first["messages"] == [
        {"role": "system", "content": "system instructions"},
        {"role": "user", "content": "user evidence"},
    ]
    assert first["model"] == "requested-model"
    assert first["settings"] == {
        "temperature": 0,
        "max_output_tokens": 123,
        "reasoning_effort": None,
    }
    assert first["response_model"] == {
        "module": NextStep.__module__,
        "qualname": NextStep.__qualname__,
    }
    assert first["response_schema"] == NextStep.model_json_schema()


def test_openai_decision_prompt_trace_matches_the_neutral_request_sent_to_client() -> None:
    async def scenario() -> None:
        fake = FakeOpenAI(_decision_message())
        model = OpenAISGRModel(model="test", client=fake)
        context = DecisionContext(
            objective="keep healthy",
            run_id="run-1",
            seed=1,
            iteration=1,
            max_steps=2,
            latest_result=ToolResult(action_kind="start", summary="started"),
        )
        captured: list[StructuredGenerationRequest[Any]] = []
        original = model._llm.generate_structured

        async def capture(request: StructuredGenerationRequest[Any]):
            captured.append(request)
            return await original(request)

        model._llm.generate_structured = capture
        trace = model.prompt_trace(context)
        await model.decide(context)

        assert captured
        assert trace == serialize_structured_generation_request(captured[0])
        assert trace["messages"][1]["content"].endswith(context.model_dump_json(indent=2))

    asyncio.run(scenario())


def test_registry_is_explicit() -> None:
    registry = LlmProviderRegistry()
    with pytest.raises(LlmConfigurationError, match="not registered"):
        registry.create(LlmProviderConfig(provider="missing"))


def test_neutral_requests_reject_invalid_roles_messages_and_settings() -> None:
    with pytest.raises(ValueError, match="role"):
        LlmMessage(role="tool", content="not allowed")
    with pytest.raises(ValueError, match="blank"):
        LlmMessage(role="user", content="  ")
    with pytest.raises(ValueError, match="at least one message"):
        StructuredGenerationRequest(response_model=NextStep, messages=())
    with pytest.raises(ValueError, match="temperature"):
        GenerationSettings(temperature=3)
    with pytest.raises(ValueError, match="Pydantic model"):
        StructuredGenerationRequest(response_model=int, messages=(LlmMessage("user", "x"),))


def test_openai_translates_sdk_failures_into_neutral_retry_taxonomy() -> None:
    class FailingCompletions:
        def __init__(self, error: Exception) -> None:
            self.error = error

        async def create(self, **kwargs):
            raise self.error

    async def scenario() -> None:
        request = StructuredGenerationRequest(
            response_model=NextStep,
            messages=(LlmMessage(role="user", content="choose"),),
        )
        http_request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

        connection_client = OpenAILlmClient(
            model="test",
            client=SimpleNamespace(
                chat=SimpleNamespace(
                    completions=FailingCompletions(APIConnectionError(request=http_request))
                )
            ),
        )
        with pytest.raises(LlmTransientError, match="connection"):
            await connection_client.generate_structured(request)

        response = httpx.Response(401, request=http_request)
        authentication_client = OpenAILlmClient(
            model="test",
            client=SimpleNamespace(
                chat=SimpleNamespace(
                    completions=FailingCompletions(
                        AuthenticationError("unauthorized", response=response, body=None)
                    )
                )
            ),
        )
        with pytest.raises(LlmAuthenticationError, match="authentication"):
            await authentication_client.generate_structured(request)

    asyncio.run(scenario())


def test_openai_translates_client_construction_failure(monkeypatch) -> None:
    def fail_client(**kwargs):
        raise RuntimeError("invalid local OpenAI configuration")

    monkeypatch.setattr("uptick_agent.llm.openai.AsyncOpenAI", fail_client)

    with pytest.raises(LlmConfigurationError, match="initialize the OpenAI") as captured:
        OpenAILlmClient(model="test")

    assert isinstance(captured.value.__cause__, RuntimeError)
