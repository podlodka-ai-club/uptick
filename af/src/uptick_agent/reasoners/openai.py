from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from time import monotonic
from typing import Any, cast

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import ValidationError

from uptick_agent.config.models import OpenAIDecisionReasonerConfig
from uptick_agent.core.errors import ReasonerFailure
from uptick_agent.core.models import (
    JsonObject,
    ReasonerConfig,
    ReasonerResult,
    ReasoningEffortName,
    ReasoningRequest,
    ReasoningTelemetry,
    TokenUsage,
)
from uptick_agent.core.sgr import normalized_output_schema
from uptick_agent.reasoners._telemetry import (
    add_token_usage,
    distribution_version,
    normalize_token_usage,
)


class OpenAIReasoner:
    """OpenAI-compatible structured-output adapter with explicit retry accounting."""

    def __init__(
        self,
        *,
        config: ReasonerConfig,
        api_key: str | None = None,
        base_url: str | None = None,
        client: AsyncOpenAI | None = None,
        request_options: dict[str, Any] | None = None,
        monotonic_fn=monotonic,
    ) -> None:
        if config.provider != "openai" or config.thread_mode != "stateless":
            raise ValueError(
                "OpenAIReasoner requires provider='openai' and thread_mode='stateless'"
            )
        self.config = config
        self.request_options = request_options or {}
        self._monotonic = monotonic_fn
        self._owns_client = client is None
        self.client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
            timeout=config.timeout_seconds or 600.0,
        )

    async def reason(self, request: ReasoningRequest) -> ReasonerResult:
        started = self._monotonic()
        token_usage = TokenUsage()
        raw_attempts: list[JsonObject] = []
        last_category = "provider"
        last_message = "OpenAI decision request failed"

        for attempt in range(1, self.config.retries + 2):
            try:
                completion = await self._create_completion(request)
                usage, raw = normalize_token_usage(
                    getattr(completion, "usage", None), provider="openai"
                )
                token_usage = add_token_usage(token_usage, usage)
                raw_attempts.append(raw)
                choices = getattr(completion, "choices", None)
                if not isinstance(choices, list) or not choices:
                    last_category = "invalid_output"
                    last_message = "structured decision failed: model returned no decision"
                    if attempt <= self.config.retries:
                        continue
                    raise self._failure(
                        last_message,
                        category=last_category,
                        attempts=attempt,
                        started=started,
                        token_usage=token_usage,
                        raw_attempts=raw_attempts,
                    )
                message = choices[0].message
                refusal = getattr(message, "refusal", None)
                if refusal:
                    raise self._failure(
                        f"structured decision failed: {refusal}",
                        category="refusal",
                        attempts=attempt,
                        started=started,
                        token_usage=token_usage,
                        raw_attempts=raw_attempts,
                    )
                content = getattr(message, "content", None)
                if not isinstance(content, str) or not content.strip():
                    last_category = "invalid_output"
                    last_message = "structured decision failed: model returned no decision"
                    if attempt <= self.config.retries:
                        continue
                    raise self._failure(
                        last_message,
                        category=last_category,
                        attempts=attempt,
                        started=started,
                        token_usage=token_usage,
                        raw_attempts=raw_attempts,
                    )
                try:
                    parsed = request.output_model.model_validate_json(content)
                except (TypeError, ValueError, ValidationError) as error:
                    last_category = "invalid_output"
                    last_message = "structured decision failed: model returned invalid JSON"
                    if attempt <= self.config.retries:
                        continue
                    raise self._failure(
                        last_message,
                        category=last_category,
                        attempts=attempt,
                        started=started,
                        token_usage=token_usage,
                        raw_attempts=raw_attempts,
                    ) from error

                reported_model = getattr(completion, "model", None)
                reported_effort = _reported_effort(completion)
                return ReasonerResult(
                    output=parsed.model_dump(mode="json"),
                    telemetry=self._telemetry(
                        attempts=attempt,
                        started=started,
                        token_usage=token_usage,
                        raw_attempts=raw_attempts,
                        reported_model=reported_model if isinstance(reported_model, str) else None,
                        reported_effort=reported_effort,
                        provider_runtime=_provider_runtime(completion),
                    ),
                )
            except ReasonerFailure:
                raise
            except Exception as error:
                retryable = _is_transient(error)
                last_category = "transient" if retryable else "provider"
                last_message = f"OpenAI decision request failed: {type(error).__name__}"
                if retryable and attempt <= self.config.retries:
                    continue
                raise self._failure(
                    last_message,
                    category=last_category,
                    attempts=attempt,
                    started=started,
                    token_usage=token_usage,
                    raw_attempts=raw_attempts,
                ) from error

        raise self._failure(
            last_message,
            category=last_category,
            attempts=self.config.retries + 1,
            started=started,
            token_usage=token_usage,
            raw_attempts=raw_attempts,
        )

    async def _create_completion(self, request: ReasoningRequest):
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "response_format": cast(
                Any,
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "sgr_envelope",
                        "strict": True,
                        "schema": normalized_output_schema(request.output_schema),
                    },
                },
            ),
            **self.request_options,
        }
        if self.config.effort is not None:
            kwargs["reasoning_effort"] = self.config.effort
        if self.config.timeout_seconds is None:
            return await self.client.chat.completions.create(**kwargs)
        async with asyncio.timeout(self.config.timeout_seconds):
            return await self.client.chat.completions.create(**kwargs)

    def _telemetry(
        self,
        *,
        attempts: int,
        started: float,
        token_usage: TokenUsage,
        raw_attempts: list[JsonObject],
        reported_model: str | None = None,
        reported_effort: ReasoningEffortName | None = None,
        provider_runtime: JsonObject | None = None,
    ) -> ReasoningTelemetry:
        return ReasoningTelemetry(
            provider="openai",
            requested_model=self.config.model,
            reported_model=reported_model,
            requested_effort=self.config.effort,
            reported_effort=reported_effort,
            thread_mode="stateless",
            attempts=attempts,
            duration_seconds=max(0.0, self._monotonic() - started),
            sdk_name="openai",
            sdk_version=distribution_version("openai"),
            provider_runtime=provider_runtime or {},
            token_usage=token_usage,
            raw_usage=cast(JsonObject, {"attempts": raw_attempts}),
        )

    def _failure(
        self,
        message: str,
        *,
        category: str,
        attempts: int,
        started: float,
        token_usage: TokenUsage,
        raw_attempts: list[JsonObject],
    ) -> ReasonerFailure:
        return ReasonerFailure(
            message,
            category=category,
            telemetry=self._telemetry(
                attempts=attempts,
                started=started,
                token_usage=token_usage,
                raw_attempts=raw_attempts,
            ),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.close()


def build_openai_reasoner(
    config: OpenAIDecisionReasonerConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> OpenAIReasoner:
    """Resolve this adapter's SecretRef without retaining the secret in config state."""

    source = os.environ if environ is None else environ
    variable = config.api_key.from_env
    api_key = source.get(variable)
    if not api_key:
        raise ValueError(f"OpenAI secret environment variable {variable} is not set")
    try:
        return OpenAIReasoner(
            config=ReasonerConfig(
                provider="openai",
                model=config.model,
                effort=config.effort,
                thread_mode="stateless",
                timeout_seconds=config.timeout_seconds,
                retries=config.retries,
            ),
            api_key=api_key,
            base_url=config.base_url,
        )
    except Exception:
        raise RuntimeError("OpenAI adapter initialization failed") from None


def _is_transient(error: Exception) -> bool:
    if isinstance(
        error,
        (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError, TimeoutError),
    ):
        return True
    return isinstance(error, APIStatusError) and error.status_code >= 500


def _reported_effort(value: object) -> ReasoningEffortName | None:
    effort = getattr(value, "reasoning_effort", None)
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    return cast(ReasoningEffortName, effort) if effort in allowed else None


def _provider_runtime(value: object) -> JsonObject:
    runtime: JsonObject = {}
    fingerprint = getattr(value, "system_fingerprint", None)
    service_tier = getattr(value, "service_tier", None)
    if isinstance(fingerprint, str):
        runtime["system_fingerprint"] = fingerprint
    if isinstance(service_tier, str):
        runtime["service_tier"] = service_tier
    return runtime
