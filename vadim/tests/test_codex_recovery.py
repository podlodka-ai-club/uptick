from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("openai_codex")
from openai_codex import RetryLimitExceededError, ServerBusyError, TransportClosedError

from uptick_agent.llm.codex import CodexDecisionError, CodexSGRModel
from uptick_agent.llm.contracts import LlmMessage, LlmTransientError, StructuredGenerationRequest
from uptick_agent.models import V1NextStep


def _response() -> str:
    return V1NextStep.model_validate(
        {
            "current_situation": "status is known",
            "hypothesis": "overview confirms it",
            "remaining_steps": ["inspect overview"],
            "task_completed": False,
            "action": {"kind": "get_overview"},
        }
    ).model_dump_json()


def _request() -> StructuredGenerationRequest:
    return StructuredGenerationRequest(
        response_model=V1NextStep,
        messages=(LlmMessage(role="user", content="choose from the public context"),),
    )


def _usage(tokens: int = 17) -> Any:
    return SimpleNamespace(
        total=SimpleNamespace(
            input_tokens=tokens - 5,
            output_tokens=5,
            total_tokens=tokens,
            cached_input_tokens=3,
            reasoning_output_tokens=1,
        ),
        last=None,
    )


class Result:
    def __init__(self, response: str, *, usage: Any = None, items: list[Any] | None = None):
        self.status = "completed"
        self.final_response = response
        self.usage = usage
        self.items = items or []


class Thread:
    def __init__(self, value: Any):
        self.value = value
        self.calls: list[str] = []

    async def run(self, prompt: str, **kwargs: Any) -> Result:
        del kwargs
        self.calls.append(prompt)
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class Client:
    def __init__(self, values: list[Any]):
        self.values = values
        self.threads: list[Thread] = []
        self.closed = False

    async def account(self) -> Any:
        return SimpleNamespace(account=SimpleNamespace(root=SimpleNamespace(type="chatgpt")))

    async def thread_start(self, **kwargs: Any) -> Thread:
        del kwargs
        thread = Thread(self.values[len(self.threads)])
        self.threads.append(thread)
        return thread

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "failure",
    [
        ServerBusyError(-32000, "busy", {"codex_error_info": "server_overloaded"}),
        RetryLimitExceededError(-32000, "retry limit", {"codex_error_info": "server_overloaded"}),
    ],
)
def test_typed_busy_retries_once_and_returns_one_action_with_usage_gap(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    async def scenario() -> None:
        fake = Client([failure, Result(_response(), usage=_usage())])
        model = CodexSGRModel(client=fake)
        monkeypatch.setattr("uptick_agent.llm.codex._BUSY_RETRY_DELAY_SECONDS", 0)

        actions = [(await model.generate_structured(_request())).value.action.kind]

        assert actions == ["get_overview"]
        assert len(fake.threads) == 2
        assert fake.threads[0].calls == fake.threads[1].calls
        assert model.last_telemetry is not None
        assert model.last_telemetry.request_count == 2
        assert model.last_telemetry.retry_count == 1
        assert model.last_telemetry.usage_reported_requests == 1
        assert model.last_telemetry.total_tokens is None
        assert [item["outcome"] for item in model.last_attempts] == [
            "provider_error",
            "provider_result",
        ]
        assert model.last_attempts[0]["cause_type"] == type(failure).__name__
        assert model.last_attempts[1]["usage"]["total_tokens"] == 17

    asyncio.run(scenario())


def test_schema_repair_and_transient_share_the_two_attempt_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fake = Client(
            [
                Result(json.dumps({"current_situation": "incomplete"}), usage=_usage(11)),
                ServerBusyError(-32000, "busy", {"codex_error_info": "server_overloaded"}),
            ]
        )
        model = CodexSGRModel(client=fake)
        monkeypatch.setattr("uptick_agent.llm.codex._BUSY_RETRY_DELAY_SECONDS", 0)

        with pytest.raises(LlmTransientError, match="after one bounded retry") as captured:
            await model.generate_structured(_request())

        assert isinstance(captured.value.__cause__, ServerBusyError)
        assert "provider_cause_type=ServerBusyError" in str(captured.value)
        assert len(fake.threads) == 2
        assert model.last_telemetry is not None
        assert model.last_telemetry.request_count == 2
        assert model.last_telemetry.total_tokens is None

    asyncio.run(scenario())


def test_owned_transport_failure_replaces_client_in_same_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    instances: list[Client] = []

    class Owned(Client):
        def __init__(self, config: Any):
            value = TransportClosedError("closed") if not instances else Result(_response())
            super().__init__([value])
            self.config = config
            instances.append(self)

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.setattr("uptick_agent.llm.codex.AsyncCodex", Owned)
        monkeypatch.setattr(CodexSGRModel, "_create_workspace", staticmethod(lambda: workspace))
        model = CodexSGRModel()

        result = await model.generate_structured(_request())

        assert result.value.action.kind == "get_overview"
        assert len(instances) == 2
        assert instances[0].closed
        assert instances[0].config is instances[1].config
        assert instances[0].config.cwd == str(workspace)
        await model.aclose()
        assert instances[1].closed
        assert not workspace.exists()

    asyncio.run(scenario())


def test_borrowed_transport_failure_is_not_retried_or_closed() -> None:
    async def scenario() -> None:
        fake = Client([TransportClosedError("closed"), Result(_response())])
        model = CodexSGRModel(client=fake)

        with pytest.raises(LlmTransientError, match="TransportClosedError"):
            await model.generate_structured(_request())

        assert len(fake.threads) == 1
        assert not fake.closed
        assert model.last_telemetry is not None
        assert model.last_telemetry.request_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [RuntimeError("unknown"), asyncio.CancelledError()])
def test_unknown_failure_and_cancellation_are_not_retried(failure: BaseException) -> None:
    async def scenario() -> None:
        fake = Client([failure, Result(_response())])
        model = CodexSGRModel(client=fake)

        expected = (
            asyncio.CancelledError
            if isinstance(failure, asyncio.CancelledError)
            else LlmTransientError
        )
        with pytest.raises(expected):
            await model.generate_structured(_request())

        assert len(fake.threads) == 1
        assert not fake.closed

    asyncio.run(scenario())


def test_outer_timeout_after_schema_retry_marks_aggregate_usage_incomplete() -> None:
    class HangingThread(Thread):
        async def run(self, prompt: str, **kwargs: Any) -> Result:
            del prompt, kwargs
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    class HangingSecondClient(Client):
        async def thread_start(self, **kwargs: Any) -> Thread:
            del kwargs
            if self.threads:
                thread = HangingThread(None)
            else:
                thread = Thread(
                    Result(json.dumps({"current_situation": "incomplete"}), usage=_usage(11))
                )
            self.threads.append(thread)
            return thread

    async def scenario() -> None:
        fake = HangingSecondClient([])
        model = CodexSGRModel(client=fake)

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await model.generate_structured(_request())

        assert len(fake.threads) == 2
        assert model.last_telemetry is not None
        assert model.last_telemetry.request_count == 2
        assert model.last_telemetry.retry_count == 1
        assert model.last_telemetry.usage_reported_requests == 1
        assert model.last_telemetry.input_tokens is None
        assert model.last_telemetry.total_tokens is None
        assert model.last_attempts[0]["usage"]["total_tokens"] == 11
        assert model.last_attempts[1]["outcome"] == "cancelled"

    asyncio.run(scenario())


def test_unsafe_tool_output_is_not_retried() -> None:
    async def scenario() -> None:
        event = SimpleNamespace(type="commandExecution")
        fake = Client([Result(_response(), items=[event]), Result(_response())])
        model = CodexSGRModel(client=fake)

        with pytest.raises(CodexDecisionError, match="forbidden tool-use"):
            await model.generate_structured(_request())

        assert len(fake.threads) == 1

    asyncio.run(scenario())
