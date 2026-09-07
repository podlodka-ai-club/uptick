"""Bounded timeout recovery for provider neutral structured generation."""

from __future__ import annotations

import asyncio
import inspect
import math
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any

from uptick_agent.llm.contracts import (
    LlmCallTelemetry,
    LlmCapabilities,
    LlmClient,
    LlmStructuredOutputError,
    LlmTransientError,
    StructuredGenerationRequest,
    StructuredGenerationResult,
    TextGenerationRequest,
    TextGenerationResult,
)


@dataclass(frozen=True, slots=True)
class TimeoutRecoveryPolicy:
    """Limits for retrying a structured generation under bounded failures."""

    attempt_timeout_seconds: float = 45.0
    total_timeout_seconds: float = 120.0
    max_attempts: int = 2
    retry_transient_errors: bool = False
    retry_structured_output_errors: bool = False

    def __post_init__(self) -> None:
        for name in ("attempt_timeout_seconds", "total_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite positive number")
            if value <= 0 or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite positive number")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        if type(self.retry_transient_errors) is not bool:
            raise ValueError("retry_transient_errors must be a boolean")
        if type(self.retry_structured_output_errors) is not bool:
            raise ValueError("retry_structured_output_errors must be a boolean")


class TimeoutRecoveryLlmClient:
    """Retry bounded structured failures with a fresh provider client.

    The first client is supplied by the registry. Every client returned by the
    factory is owned by this wrapper and is closed before a retryable attempt is
    replaced, or when the wrapper itself is closed. Transient provider and
    structured-output errors are retryable only when enabled by the policy.
    """

    def __init__(
        self,
        client: LlmClient,
        *,
        factory: Any,
        config: Any,
        policy: TimeoutRecoveryPolicy,
    ) -> None:
        self._client = client
        self._factory = factory
        self._config = config
        self._policy = policy
        self._last_telemetry: LlmCallTelemetry | None = None
        self._last_attempts: tuple[dict[str, Any], ...] = ()
        self._current_client_closed = False
        self._closed = False

    @property
    def capabilities(self) -> LlmCapabilities:
        return self._client.capabilities

    @property
    def model(self) -> Any:
        return getattr(self._client, "model", None)

    @property
    def last_telemetry(self) -> LlmCallTelemetry | None:
        return self._last_telemetry

    @property
    def last_attempts(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                **attempt,
                "telemetry": attempt.get("telemetry"),
                "provider_telemetry": attempt.get("provider_telemetry"),
            }
            for attempt in self._last_attempts
        )

    async def generate_structured[T](
        self, request: StructuredGenerationRequest[T]
    ) -> StructuredGenerationResult[T]:
        """Run the exact request under bounded per attempt and total deadlines."""
        started = monotonic()
        self._last_telemetry = None
        self._last_attempts = ()
        completed = False
        overall: asyncio.Timeout | None = None
        try:
            async with asyncio.timeout(self._policy.total_timeout_seconds) as overall:
                for attempt_number in range(1, self._policy.max_attempts + 1):
                    current = self._client
                    before = self._telemetry(current)
                    before_attempts = self._provider_attempts(current)
                    try:
                        async with asyncio.timeout(
                            self._policy.attempt_timeout_seconds
                        ) as attempt_timeout:
                            result = await current.generate_structured(request)
                        if (
                            current_task := asyncio.current_task()
                        ) is not None and current_task.cancelling():
                            raise asyncio.CancelledError
                        if attempt_timeout.expired() or overall.expired():
                            # A provider can swallow cancellation and return a
                            # late result. The deadline still wins.
                            raise TimeoutError("structured generation attempt timed out")
                    except asyncio.CancelledError:
                        overall_timeout = overall.expired()
                        self._record_attempt(
                            attempt_number,
                            outcome="timeout" if overall_timeout else "cancelled",
                            cause_type="TimeoutError" if overall_timeout else "CancelledError",
                            telemetry=self._attempt_telemetry(current, before),
                            provider_attempts=self._attempt_attempts(current, before_attempts),
                        )
                        raise
                    except (
                        TimeoutError,
                        LlmTransientError,
                        LlmStructuredOutputError,
                    ) as error:
                        retry_enabled = (
                            isinstance(error, TimeoutError)
                            or (
                                isinstance(error, LlmTransientError)
                                and self._policy.retry_transient_errors
                            )
                            or (
                                isinstance(error, LlmStructuredOutputError)
                                and self._policy.retry_structured_output_errors
                            )
                        )
                        if not retry_enabled:
                            self._record_attempt(
                                attempt_number,
                                outcome="error",
                                cause_type=type(error).__name__,
                                telemetry=self._attempt_telemetry(current, before),
                                provider_attempts=self._attempt_attempts(current, before_attempts),
                            )
                            raise
                        if isinstance(error, TimeoutError):
                            outcome = "timeout"
                        elif isinstance(error, LlmTransientError):
                            outcome = "transient_error"
                        else:
                            outcome = "invalid_output"
                        telemetry = self._attempt_telemetry(current, before)
                        self._record_attempt(
                            attempt_number,
                            outcome=outcome,
                            cause_type=type(error).__name__,
                            telemetry=telemetry,
                            provider_attempts=self._attempt_attempts(current, before_attempts),
                        )
                        if overall.expired() or attempt_number >= self._policy.max_attempts:
                            await self._close_client(current)
                            raise
                        await self._close_client(current)
                        replacement = self._factory.create(self._config)
                        if inspect.isawaitable(replacement):
                            replacement = await replacement
                        self._client = replacement
                        self._current_client_closed = False
                        continue
                    except Exception as error:
                        self._record_attempt(
                            attempt_number,
                            outcome="error",
                            cause_type=type(error).__name__,
                            telemetry=self._attempt_telemetry(current, before),
                            provider_attempts=self._attempt_attempts(current, before_attempts),
                        )
                        raise
                    else:
                        telemetry = result.telemetry or self._attempt_telemetry(current, before)
                        self._record_attempt(
                            attempt_number,
                            outcome="result",
                            provider=getattr(result, "provider", None),
                            telemetry=telemetry,
                            provider_attempts=self._attempt_attempts(current, before_attempts),
                        )
                        aggregate = _aggregate_telemetry(started, self._last_attempts)
                        self._last_telemetry = aggregate
                        completed = True
                        return replace(result, telemetry=aggregate)
            raise AssertionError("timeout recovery exited without a result or exception")
        finally:
            if not completed:
                self._last_telemetry = _aggregate_telemetry(started, self._last_attempts)

    async def generate_text(self, request: TextGenerationRequest) -> TextGenerationResult:
        """Delegate text generation without structured timeout recovery."""
        result = await self._client.generate_text(request)
        self._last_telemetry = result.telemetry or self._telemetry(self._client)
        self._last_attempts = ()
        return result

    async def aclose(self) -> None:
        if self._closed:
            return
        await self._close_client(self._client)
        self._closed = True

    def _record_attempt(self, attempt: int, **values: Any) -> None:
        if "telemetry" in values and "provider_telemetry" not in values:
            values["provider_telemetry"] = values["telemetry"]
        self._last_attempts = (*self._last_attempts, {"attempt": attempt, **values})

    async def _close_client(self, client: LlmClient) -> None:
        if client is self._client and self._current_client_closed:
            return
        await client.aclose()
        if client is self._client:
            self._current_client_closed = True

    @staticmethod
    def _telemetry(client: LlmClient) -> LlmCallTelemetry | None:
        try:
            return client.last_telemetry
        except (AttributeError, RuntimeError):
            return None

    @classmethod
    def _attempt_telemetry(
        cls, client: LlmClient, before: LlmCallTelemetry | None
    ) -> LlmCallTelemetry | None:
        after = cls._telemetry(client)
        # Providers commonly clear/update this property in a finally block. A
        # value that was already present before the call is stale evidence.
        if after is before:
            return None
        return after

    @staticmethod
    def _provider_attempts(client: LlmClient) -> tuple[Any, ...] | None:
        attempts = getattr(client, "last_attempts", None)
        if attempts is None:
            return None
        try:
            return tuple(attempts)
        except TypeError:
            return None

    @classmethod
    def _attempt_attempts(
        cls, client: LlmClient, before: tuple[Any, ...] | None
    ) -> tuple[Any, ...] | None:
        after = cls._provider_attempts(client)
        if after == before:
            return None
        return after


def _aggregate_telemetry(
    started: float,
    attempts: tuple[dict[str, Any], ...],
) -> LlmCallTelemetry:
    request_count = 0
    provider_retry_count = 0
    reported_requests = 0
    fields = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "cost_minor",
    )
    totals = {field: 0 for field in fields}
    known = {field: True for field in fields}
    seen_usage = False
    currencies: list[str] = []

    for attempt in attempts:
        telemetry = attempt.get("telemetry")
        # The wrapper has made one underlying call even when cancellation
        # occurred before the provider could publish telemetry.
        request_count += max(1, telemetry.request_count) if telemetry is not None else 1
        provider_retry_count += telemetry.retry_count if telemetry is not None else 0
        if telemetry is None:
            for field in fields:
                known[field] = False
            continue
        reported_requests += telemetry.usage_reported_requests
        if telemetry.cost_currency is not None:
            currencies.append(telemetry.cost_currency)
        if telemetry.usage_reported_requests < max(1, telemetry.request_count):
            for field in fields:
                known[field] = False
            continue
        seen_usage = True
        for field in fields:
            value = getattr(telemetry, field)
            if value is None:
                known[field] = False
            elif known[field]:
                totals[field] += value

    values: dict[str, int | None] = {}
    for field in fields:
        values[field] = totals[field] if seen_usage and known[field] else None
    cost_currencies = set(currencies)
    cost_currency = next(iter(cost_currencies)) if len(cost_currencies) == 1 else None
    if values["cost_minor"] is None or len(cost_currencies) > 1:
        values["cost_minor"] = None
        cost_currency = None

    return LlmCallTelemetry(
        elapsed_seconds=max(0.0, monotonic() - started),
        request_count=request_count,
        retry_count=min(request_count, provider_retry_count + max(0, len(attempts) - 1)),
        usage_reported_requests=reported_requests,
        input_tokens=values["input_tokens"],
        output_tokens=values["output_tokens"],
        total_tokens=values["total_tokens"],
        cached_tokens=values["cached_tokens"],
        reasoning_tokens=values["reasoning_tokens"],
        cost_minor=values["cost_minor"],
        cost_currency=cost_currency,
    )
