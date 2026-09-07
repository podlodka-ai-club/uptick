from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel

from uptick_agent.llm.contracts import (
    LlmAuthenticationError,
    LlmCallTelemetry,
    LlmCapabilities,
    LlmMessage,
    LlmStructuredOutputError,
    LlmTransientError,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)
from uptick_agent.llm.recovery import TimeoutRecoveryPolicy
from uptick_agent.llm.registry import LlmProviderConfig, LlmProviderRegistry


class Answer(BaseModel):
    answer: str


def _request() -> StructuredGenerationRequest[Answer]:
    return StructuredGenerationRequest(
        response_model=Answer,
        model="requested-model",
        messages=(LlmMessage(role="user", content="same request"),),
    )


def _telemetry(*, tokens: int | None = 10) -> LlmCallTelemetry:
    return LlmCallTelemetry(
        elapsed_seconds=0,
        request_count=1,
        retry_count=0,
        input_tokens=tokens,
        output_tokens=None if tokens is None else 2,
        total_tokens=None if tokens is None else tokens + 2,
        usage_reported_requests=0 if tokens is None else 1,
    )


@dataclass
class FakeClient:
    behavior: str
    close_delay: float = 0
    telemetry_override: LlmCallTelemetry | None = None

    def __post_init__(self) -> None:
        self.model = "fake-model"
        self.capabilities = LlmCapabilities(structured_generation=True, text_generation=False)
        self.requests: list[StructuredGenerationRequest[Any]] = []
        self.last_telemetry: LlmCallTelemetry | None = None
        self.closed = False
        self.close_started = asyncio.Event()

    async def generate_structured(self, request: StructuredGenerationRequest[Any]):
        self.requests.append(request)
        if self.behavior == "hang":
            await asyncio.Event().wait()
        if self.behavior == "auth":
            raise LlmAuthenticationError("bad credentials")
        if self.behavior == "transient":
            raise LlmTransientError("temporary provider failure")
        if self.behavior == "invalid_output":
            raise LlmStructuredOutputError("invalid structured output")
        if self.behavior == "error":
            raise RuntimeError("provider failed")
        self.last_telemetry = self.telemetry_override or _telemetry()
        return StructuredGenerationResult(
            value=Answer(answer="ok"),
            provider="fake",
            model=request.model,
            telemetry=self.last_telemetry,
        )

    async def generate_text(self, request):
        del request
        raise AssertionError("text delegation is not used in this test")

    async def aclose(self) -> None:
        self.close_started.set()
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        self.closed = True


class FakeFactory:
    def __init__(self, clients: list[FakeClient]) -> None:
        self.clients = clients
        self.created: list[FakeClient] = []

    def create(self, config: LlmProviderConfig) -> FakeClient:
        del config
        client = self.clients[len(self.created)]
        if self.created:
            assert self.created[-1].closed
        self.created.append(client)
        return client


def _client(factory: FakeFactory, policy: TimeoutRecoveryPolicy | None):
    registry = LlmProviderRegistry()
    registry.register("fake", factory)
    return registry.create(LlmProviderConfig(provider="fake", timeout_recovery=policy))


def test_timeout_recovery_replaces_client_and_repeats_exact_request() -> None:
    async def scenario() -> None:
        first = FakeClient("hang")
        second = FakeClient("success")
        factory = FakeFactory([first, second])
        client = _client(
            factory,
            TimeoutRecoveryPolicy(attempt_timeout_seconds=0.01, total_timeout_seconds=1),
        )

        result = await client.generate_structured(_request())

        assert result.value.answer == "ok"
        assert first.closed
        assert factory.created == [first, second]
        assert first.requests == second.requests == [_request()]
        assert client.last_attempts[0]["outcome"] == "timeout"
        assert client.last_attempts[0]["telemetry"] is None
        assert client.last_attempts[1]["outcome"] == "result"
        assert client.last_telemetry is not None
        assert client.last_telemetry.request_count == 2
        assert client.last_telemetry.retry_count == 1
        assert client.last_telemetry.usage_reported_requests == 1
        assert client.last_telemetry.total_tokens is None
        assert result.telemetry == client.last_telemetry
        await client.aclose()
        assert second.closed

    asyncio.run(scenario())


def test_timeout_exhaustion_closes_final_client() -> None:
    async def scenario() -> None:
        client_impl = FakeClient("hang")
        client = _client(
            FakeFactory([client_impl]),
            TimeoutRecoveryPolicy(
                attempt_timeout_seconds=0.01, total_timeout_seconds=1, max_attempts=1
            ),
        )

        with pytest.raises(TimeoutError):
            await client.generate_structured(_request())

        assert client_impl.closed
        assert [item["outcome"] for item in client.last_attempts] == ["timeout"]

    asyncio.run(scenario())


def test_transient_recovery_replaces_client_and_repeats_exact_request() -> None:
    async def scenario() -> None:
        first = FakeClient("transient")
        second = FakeClient("success")
        factory = FakeFactory([first, second])
        client = _client(
            factory,
            TimeoutRecoveryPolicy(
                attempt_timeout_seconds=1,
                total_timeout_seconds=1,
                retry_transient_errors=True,
            ),
        )

        result = await client.generate_structured(_request())

        assert result.value.answer == "ok"
        assert first.closed
        assert factory.created == [first, second]
        assert first.requests == second.requests == [_request()]
        assert [item["outcome"] for item in client.last_attempts] == [
            "transient_error",
            "result",
        ]
        await client.aclose()

    asyncio.run(scenario())


def test_transient_recovery_exhaustion_closes_final_client() -> None:
    async def scenario() -> None:
        first = FakeClient("transient")
        second = FakeClient("transient")
        factory = FakeFactory([first, second])
        client = _client(
            factory,
            TimeoutRecoveryPolicy(
                attempt_timeout_seconds=1,
                total_timeout_seconds=1,
                retry_transient_errors=True,
            ),
        )

        with pytest.raises(LlmTransientError):
            await client.generate_structured(_request())

        assert first.closed and second.closed
        assert [item["outcome"] for item in client.last_attempts] == [
            "transient_error",
            "transient_error",
        ]

    asyncio.run(scenario())


def test_transient_recovery_is_opt_in() -> None:
    async def scenario() -> None:
        first = FakeClient("transient")
        factory = FakeFactory([first, FakeClient("success")])
        client = _client(
            factory,
            TimeoutRecoveryPolicy(attempt_timeout_seconds=1, total_timeout_seconds=1),
        )

        with pytest.raises(LlmTransientError):
            await client.generate_structured(_request())

        assert factory.created == [first]
        assert first.closed is False
        assert client.last_attempts[0]["outcome"] == "error"
        await client.aclose()

    asyncio.run(scenario())


def test_structured_output_recovery_replaces_client_and_repeats_exact_request() -> None:
    async def scenario() -> None:
        first = FakeClient("invalid_output")
        second = FakeClient("success")
        factory = FakeFactory([first, second])
        client = _client(
            factory,
            TimeoutRecoveryPolicy(
                attempt_timeout_seconds=1,
                total_timeout_seconds=1,
                retry_structured_output_errors=True,
            ),
        )

        result = await client.generate_structured(_request())

        assert result.value.answer == "ok"
        assert first.closed
        assert factory.created == [first, second]
        assert first.requests == second.requests == [_request()]
        assert [item["outcome"] for item in client.last_attempts] == [
            "invalid_output",
            "result",
        ]
        await client.aclose()

    asyncio.run(scenario())


def test_structured_output_recovery_is_opt_in() -> None:
    async def scenario() -> None:
        first = FakeClient("invalid_output")
        factory = FakeFactory([first, FakeClient("success")])
        client = _client(
            factory,
            TimeoutRecoveryPolicy(attempt_timeout_seconds=1, total_timeout_seconds=1),
        )

        with pytest.raises(LlmStructuredOutputError):
            await client.generate_structured(_request())

        assert factory.created == [first]
        assert first.closed is False
        assert client.last_attempts[0]["outcome"] == "error"
        await client.aclose()

    asyncio.run(scenario())


def test_total_deadline_covers_cleanup_and_prevents_factory_overlap() -> None:
    async def scenario() -> None:
        first = FakeClient("hang", close_delay=1)
        second = FakeClient("success")
        factory = FakeFactory([first, second])
        client = _client(
            factory,
            TimeoutRecoveryPolicy(attempt_timeout_seconds=0.01, total_timeout_seconds=0.03),
        )

        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await client.generate_structured(_request())

        assert asyncio.get_running_loop().time() - started < 0.2
        assert factory.created == [first]
        assert first.close_started.is_set()

    asyncio.run(scenario())


def test_overall_deadline_is_recorded_as_timeout_without_retry() -> None:
    async def scenario() -> None:
        first = FakeClient("hang")
        factory = FakeFactory([first, FakeClient("success")])
        client = _client(
            factory,
            TimeoutRecoveryPolicy(attempt_timeout_seconds=1, total_timeout_seconds=0.02),
        )

        with pytest.raises(TimeoutError):
            await client.generate_structured(_request())

        assert factory.created == [first]
        assert client.last_attempts[0]["outcome"] == "timeout"

    asyncio.run(scenario())


def test_interrupted_cleanup_can_be_completed_by_aclose() -> None:
    async def scenario() -> None:
        first = FakeClient("hang", close_delay=1)
        factory = FakeFactory([first])
        client = _client(
            factory,
            TimeoutRecoveryPolicy(
                attempt_timeout_seconds=0.01, total_timeout_seconds=1, max_attempts=1
            ),
        )
        task = asyncio.create_task(client.generate_structured(_request()))
        await first.close_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        first.close_delay = 0
        await client.aclose()
        assert first.closed

    asyncio.run(scenario())


def test_late_result_after_cancelled_attempt_is_discarded() -> None:
    async def scenario() -> None:
        class LateClient(FakeClient):
            async def generate_structured(self, request):
                self.requests.append(request)
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.last_telemetry = _telemetry()
                    return StructuredGenerationResult(
                        value=Answer(answer="late"),
                        provider="fake",
                        model=request.model,
                        telemetry=self.last_telemetry,
                    )

        first = LateClient("success")
        second = FakeClient("success")
        factory = FakeFactory([first, second])
        client = _client(
            factory,
            TimeoutRecoveryPolicy(attempt_timeout_seconds=0.01, total_timeout_seconds=1),
        )

        result = await client.generate_structured(_request())

        assert result.value.answer == "ok"
        assert [item["outcome"] for item in client.last_attempts] == ["timeout", "result"]
        assert first.closed

    asyncio.run(scenario())


def test_parent_cancellation_propagates_without_retry() -> None:
    async def scenario() -> None:
        first = FakeClient("hang")
        factory = FakeFactory([first, FakeClient("success")])
        client = _client(
            factory,
            TimeoutRecoveryPolicy(attempt_timeout_seconds=1, total_timeout_seconds=2),
        )
        task = asyncio.create_task(client.generate_structured(_request()))
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert factory.created == [first]
        assert client.last_attempts[0]["outcome"] == "cancelled"

    asyncio.run(scenario())


@pytest.mark.parametrize("behavior", ["auth", "error"])
def test_permanent_provider_failure_is_not_retried(behavior: str) -> None:
    async def scenario() -> None:
        first = FakeClient(behavior)
        factory = FakeFactory([first, FakeClient("success")])
        client = _client(
            factory,
            TimeoutRecoveryPolicy(
                attempt_timeout_seconds=0.01,
                total_timeout_seconds=1,
                retry_transient_errors=True,
            ),
        )

        expected = LlmAuthenticationError if behavior == "auth" else RuntimeError
        with pytest.raises(expected):
            await client.generate_structured(_request())
        assert factory.created == [first]
        assert client.last_attempts[0]["outcome"] == "error"

    asyncio.run(scenario())


def test_legacy_registry_path_returns_factory_client_unchanged() -> None:
    first = FakeClient("success")
    factory = FakeFactory([first])
    client = _client(factory, None)
    assert client is first
    assert factory.created == [first]


def test_partial_provider_usage_is_unknown_when_request_coverage_has_a_gap() -> None:
    async def scenario() -> None:
        partial = LlmCallTelemetry(
            elapsed_seconds=0,
            request_count=2,
            retry_count=1,
            input_tokens=10,
            output_tokens=2,
            total_tokens=12,
            usage_reported_requests=1,
        )
        provider = FakeClient("success", telemetry_override=partial)
        client = _client(
            FakeFactory([provider]),
            TimeoutRecoveryPolicy(attempt_timeout_seconds=1, total_timeout_seconds=1),
        )

        result = await client.generate_structured(_request())

        assert result.telemetry is not None
        assert result.telemetry.request_count == 2
        assert result.telemetry.retry_count == 1
        assert result.telemetry.usage_reported_requests == 1
        assert result.telemetry.total_tokens is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt_timeout_seconds", 0),
        ("attempt_timeout_seconds", float("inf")),
        ("total_timeout_seconds", float("nan")),
        ("max_attempts", 0),
        ("retry_transient_errors", 1),
        ("retry_structured_output_errors", 1),
    ],
)
def test_timeout_policy_requires_finite_positive_limits(field: str, value: Any) -> None:
    values = {field: value}
    with pytest.raises(ValueError):
        TimeoutRecoveryPolicy(**values)
