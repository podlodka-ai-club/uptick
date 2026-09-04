import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIConnectionError, AuthenticationError

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
)
from uptick_agent.llm.openai import OpenAILlmClient, OpenAISGRModel
from uptick_agent.models import DecisionContext, NextStep, ToolResult


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


class FakeCompletions:
    def __init__(self, message: Any) -> None:
        self.message = message
        self.parse_calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.parse_calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)])

    async def create(self, **kwargs: Any) -> Any:
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)])


class FakeOpenAI:
    def __init__(self, message: Any) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(message))


def test_openai_client_returns_neutral_result_and_rejects_refusals() -> None:
    async def scenario() -> None:
        fake = FakeOpenAI(SimpleNamespace(parsed=_decision(), refusal=None))
        client = OpenAILlmClient(model="test", client=fake)
        request = StructuredGenerationRequest(
            response_model=NextStep,
            messages=(LlmMessage(role="user", content="choose"),),
        )

        result = await client.generate_structured(request)

        assert result.value == _decision()
        assert result.provider == "openai"
        assert fake.chat.completions.parse_calls[0]["messages"] == [
            {"role": "user", "content": "choose"}
        ]
        assert client.capabilities.structured_generation
        assert client.capabilities.text_generation

        fake.chat.completions.message = SimpleNamespace(parsed=None, refusal="policy")
        with pytest.raises(LlmStructuredOutputError, match="refused"):
            await client.generate_structured(request)

        fake.chat.completions.message = SimpleNamespace(
            content="ignored", refusal=None, tool_calls=[object()], function_call=None
        )
        with pytest.raises(LlmProviderError, match="tool use"):
            await client.generate_text(TextGenerationRequest(messages=request.messages))

    asyncio.run(scenario())


def test_openai_decision_facade_preserves_decision_model_behavior() -> None:
    async def scenario() -> None:
        fake = FakeOpenAI(SimpleNamespace(parsed=_decision(), refusal=None))
        model = OpenAISGRModel(model="test", client=fake)
        context = DecisionContext(
            objective="keep healthy",
            run_id="run-1",
            seed=1,
            iteration=1,
            max_steps=2,
            latest_result=ToolResult(action_kind="start", summary="started"),
        )

        assert await model.decide(context) == _decision()

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

        async def parse(self, **kwargs):
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
