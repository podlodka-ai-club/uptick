import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from uptick_agent import cli
from uptick_agent.llm import (
    GenerationSettings,
    LlmCallTelemetry,
    LlmMessage,
    LlmPermanentProviderError,
    LlmStructuredOutputError,
    LlmTransientError,
    StructuredGenerationRequest,
    TextGenerationRequest,
    serialize_structured_generation_request,
)
from uptick_agent.llm.openai import OpenAILlmClient
from uptick_agent.models import DecisionContext, ToolResult, V1NextStep
from uptick_agent.simulator.v2_policy import SimulatorV2TimeBudgetPolicy


def _valid_response() -> str:
    return V1NextStep.model_validate(
        {
            "current_situation": "the run has started",
            "hypothesis": "an overview will establish the baseline",
            "remaining_steps": ["inspect the overview"],
            "task_completed": False,
            "action": {"kind": "get_overview"},
        }
    ).model_dump_json()


def _request(*, settings: GenerationSettings | None = None) -> StructuredGenerationRequest:
    return StructuredGenerationRequest(
        response_model=V1NextStep,
        messages=(LlmMessage(role="user", content="choose"),),
        settings=settings or GenerationSettings(),
    )


def _codex_model(client: Any) -> Any:
    pytest.importorskip("openai_codex")
    from uptick_agent.llm.codex import CodexSGRModel

    return CodexSGRModel(client=client)


def _openai_completion(
    content: str,
    *,
    usage: Any = None,
    include_usage: bool = True,
    finish_reason: str = "stop",
) -> Any:
    message = SimpleNamespace(
        content=content,
        refusal=None,
        tool_calls=None,
        function_call=None,
    )
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)]
    )
    if include_usage:
        completion.usage = usage
    return completion


class FakeCompletions:
    def __init__(self, responses: list[Any] | None = None, error: Exception | None = None) -> None:
        self.responses = responses or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


class FakeOpenAI:
    def __init__(self, responses: list[Any] | None = None, error: Exception | None = None) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(responses, error))


def _openai_usage() -> Any:
    return SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=7,
        total_tokens=18,
        prompt_tokens_details=SimpleNamespace(cached_tokens=3),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )


def test_generation_settings_reasoning_effort_is_validated_serialized_and_sent() -> None:
    async def scenario() -> None:
        fake = FakeOpenAI(
            [
                _openai_completion(
                    _valid_response(),
                    usage=_openai_usage(),
                )
            ]
        )
        client = OpenAILlmClient(model="test", client=fake)
        request = _request(settings=GenerationSettings(reasoning_effort="low"))

        result = await client.generate_structured(request)

        assert fake.chat.completions.calls[0]["reasoning_effort"] == "low"
        assert result.telemetry is not None
        assert result.telemetry.input_tokens == 11
        assert result.telemetry.output_tokens == 7
        assert result.telemetry.total_tokens == 18
        assert result.telemetry.cached_tokens == 3
        assert result.telemetry.reasoning_tokens == 2
        assert result.telemetry.cost_minor is None
        assert result.telemetry.cost_currency is None
        assert result.telemetry.request_count == 1
        assert result.telemetry.retry_count == 0
        assert result.telemetry.usage_reported_requests == 1
        assert client.last_telemetry == result.telemetry

        trace = serialize_structured_generation_request(request)
        assert trace["settings"]["reasoning_effort"] == "low"

    asyncio.run(scenario())


@pytest.mark.parametrize("effort", [True, "unsupported", 3])
def test_generation_settings_rejects_unknown_reasoning_effort(effort: Any) -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        GenerationSettings(reasoning_effort=effort)  # type: ignore[arg-type]


def test_openai_missing_usage_is_nullable_and_validation_failure_keeps_usage() -> None:
    async def scenario() -> None:
        missing_usage_client = OpenAILlmClient(
            model="test",
            client=FakeOpenAI([_openai_completion(_valid_response(), include_usage=False)]),
        )
        missing = await missing_usage_client.generate_structured(_request())
        assert missing.telemetry is not None
        assert missing.telemetry.input_tokens is None
        assert missing.telemetry.output_tokens is None
        assert missing.telemetry.total_tokens is None
        assert missing.telemetry.usage_reported_requests == 0

        invalid_client = OpenAILlmClient(
            model="test",
            client=FakeOpenAI(
                [_openai_completion("{}", usage=_openai_usage())],
            ),
        )
        with pytest.raises(LlmStructuredOutputError, match="invalid structured JSON"):
            await invalid_client.generate_structured(_request())
        assert invalid_client.last_telemetry is not None
        assert invalid_client.last_telemetry.input_tokens == 11
        assert invalid_client.last_telemetry.output_tokens == 7
        assert invalid_client.last_telemetry.request_count == 1

    asyncio.run(scenario())


def test_openai_provider_failure_keeps_measured_call_without_inventing_usage() -> None:
    async def scenario() -> None:
        client = OpenAILlmClient(
            model="test",
            client=FakeOpenAI(error=RuntimeError("transport unavailable")),
        )
        with pytest.raises(LlmPermanentProviderError):
            await client.generate_structured(_request())
        assert client.last_telemetry is not None
        assert client.last_telemetry.request_count == 1
        assert client.last_telemetry.input_tokens is None
        assert client.last_telemetry.usage_reported_requests == 0

    asyncio.run(scenario())


def test_openai_text_result_also_carries_telemetry() -> None:
    async def scenario() -> None:
        client = OpenAILlmClient(
            model="test",
            client=FakeOpenAI(
                [_openai_completion("plain text", usage=_openai_usage())],
            ),
        )
        result = await client.generate_text(
            TextGenerationRequest(messages=(LlmMessage(role="user", content="write"),))
        )
        assert result.text == "plain text"
        assert result.telemetry is not None
        assert result.telemetry.total_tokens == 18

    asyncio.run(scenario())


def _codex_usage(*, total: dict[str, Any], last: dict[str, Any] | None = None) -> Any:
    fallback = last or total
    return SimpleNamespace(total=SimpleNamespace(**total), last=SimpleNamespace(**fallback))


class FakeCodexResult:
    def __init__(
        self, response: str | None, *, usage: Any = None, status: str = "completed"
    ) -> None:
        self.status = status
        self.final_response = response
        self.items: list[Any] = []
        self.usage = usage


class FakeCodexThread:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def run(self, prompt: str, **kwargs: Any) -> Any:
        del prompt
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeCodex:
    def __init__(self, results: list[Any], *, account_type: str = "chatgpt") -> None:
        self.results = results
        self.account_type = account_type
        self.threads: list[FakeCodexThread] = []

    async def account(self) -> Any:
        return SimpleNamespace(
            account=SimpleNamespace(root=SimpleNamespace(type=self.account_type))
        )

    async def thread_start(self, **kwargs: Any) -> FakeCodexThread:
        del kwargs
        thread = FakeCodexThread(self.results[len(self.threads)])
        self.threads.append(thread)
        return thread


def test_codex_reasoning_effort_is_sent_as_run_effort_and_usage_is_reported() -> None:
    async def scenario() -> None:
        fake = FakeCodex(
            [
                FakeCodexResult(
                    _valid_response(),
                    usage=_codex_usage(
                        total={
                            "input_tokens": 13,
                            "output_tokens": 5,
                            "total_tokens": 18,
                            "cached_input_tokens": 2,
                            "reasoning_output_tokens": 1,
                        },
                        last={
                            "input_tokens": 100,
                            "output_tokens": 100,
                            "total_tokens": 200,
                            "cached_input_tokens": 100,
                            "reasoning_output_tokens": 100,
                        },
                    ),
                )
            ]
        )
        model = _codex_model(fake)
        result = await model.generate_structured(
            _request(settings=GenerationSettings(reasoning_effort="low"))
        )

        assert fake.threads[0].calls[0]["effort"] == "low"
        assert result.telemetry is not None
        assert result.telemetry.input_tokens == 13
        assert result.telemetry.output_tokens == 5
        assert result.telemetry.total_tokens == 18
        assert result.telemetry.cached_tokens == 2
        assert result.telemetry.reasoning_tokens == 1
        assert result.telemetry.request_count == 1
        assert result.telemetry.usage_reported_requests == 1

    asyncio.run(scenario())


def test_codex_retry_sums_fresh_thread_totals_and_not_last_totals() -> None:
    invalid = '{"current_situation":"missing required fields"}'
    first_usage = _codex_usage(
        total={
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "cached_input_tokens": 1,
            "reasoning_output_tokens": 2,
        },
        last={
            "input_tokens": 100,
            "output_tokens": 100,
            "total_tokens": 200,
            "cached_input_tokens": 100,
            "reasoning_output_tokens": 100,
        },
    )
    second_usage = _codex_usage(
        total={
            "input_tokens": 20,
            "output_tokens": 6,
            "total_tokens": 26,
            "cached_input_tokens": 3,
            "reasoning_output_tokens": 4,
        },
        last={
            "input_tokens": 200,
            "output_tokens": 200,
            "total_tokens": 400,
            "cached_input_tokens": 200,
            "reasoning_output_tokens": 200,
        },
    )

    async def scenario() -> None:
        fake = FakeCodex(
            [
                FakeCodexResult(invalid, usage=first_usage),
                FakeCodexResult(_valid_response(), usage=second_usage),
            ]
        )
        model = _codex_model(fake)
        result = await model.generate_structured(_request())

        assert result.telemetry is not None
        assert result.telemetry.input_tokens == 30
        assert result.telemetry.output_tokens == 10
        assert result.telemetry.total_tokens == 40
        assert result.telemetry.cached_tokens == 4
        assert result.telemetry.reasoning_tokens == 6
        assert result.telemetry.request_count == 2
        assert result.telemetry.retry_count == 1
        assert result.telemetry.usage_reported_requests == 2

    asyncio.run(scenario())


def test_codex_missing_usage_and_provider_failure_keep_nullable_telemetry() -> None:
    async def scenario() -> None:
        missing_model = _codex_model(FakeCodex([FakeCodexResult(_valid_response())]))
        result = await missing_model.generate_structured(_request())
        assert result.telemetry is not None
        assert result.telemetry.total_tokens is None
        assert result.telemetry.usage_reported_requests == 0

        failing_model = _codex_model(FakeCodex([RuntimeError("thread failed")]))
        with pytest.raises(LlmTransientError):
            await failing_model.generate_structured(_request())
        assert failing_model.last_telemetry is not None
        assert failing_model.last_telemetry.request_count == 1
        assert failing_model.last_telemetry.total_tokens is None

    asyncio.run(scenario())


def test_codex_cancellation_after_first_attempt_preserves_known_usage() -> None:
    async def scenario() -> None:
        usage = _codex_usage(
            total={
                "input_tokens": 8,
                "output_tokens": 3,
                "total_tokens": 11,
                "cached_input_tokens": 1,
                "reasoning_output_tokens": 0,
            }
        )
        fake = FakeCodex(
            [
                FakeCodexResult('{"current_situation":"missing required fields"}', usage=usage),
                asyncio.CancelledError(),
            ]
        )
        model = _codex_model(fake)

        with pytest.raises(asyncio.CancelledError):
            await model.generate_structured(_request())
        assert model.last_telemetry is not None
        assert model.last_telemetry.request_count == 2
        assert model.last_telemetry.retry_count == 1
        assert model.last_telemetry.input_tokens == 8
        assert model.last_telemetry.usage_reported_requests == 1

    asyncio.run(scenario())


def test_telemetry_rejects_non_integer_measurements() -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        LlmCallTelemetry(elapsed_seconds=0.1, request_count=1, retry_count=0, input_tokens=1.5)
    with pytest.raises(ValueError, match="finite"):
        LlmCallTelemetry(elapsed_seconds=float("nan"), request_count=0, retry_count=0)


def test_cli_reasoning_effort_reaches_v2_neutral_request(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    args = cli._parser().parse_args(
        ["run", "--seed", "1", "--decision-provider", "openai", "--reasoning-effort", "low"]
    )
    model = cli._decision_model(args)
    request = model._delegate._build_request(  # type: ignore[attr-defined]
        DecisionContext(
            objective="test",
            run_id="run",
            seed=1,
            iteration=1,
            max_steps=1,
            latest_result=ToolResult(action_kind="start", summary="started"),
        )
    )
    assert request.settings.reasoning_effort == "low"

    asyncio.run(model.aclose())


def test_cli_v2_wrapper_forwards_provider_last_telemetry() -> None:
    marker = object()

    class Client:
        model = "test"
        last_telemetry = marker

        async def aclose(self) -> None:
            return None

    structured = cli.StructuredDecisionModel(Client())
    policy = SimulatorV2TimeBudgetPolicy(structured)
    assert structured.last_telemetry is marker
    assert policy.last_telemetry is marker
