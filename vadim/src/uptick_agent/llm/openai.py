from __future__ import annotations

from time import monotonic
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    PermissionDeniedError,
)
from openai import (
    AuthenticationError as OpenAIAuthenticationError,
)
from openai import (
    RateLimitError as OpenAIRateLimitError,
)

from uptick_agent.llm.contracts import (
    LlmAuthenticationError,
    LlmCallTelemetry,
    LlmCapabilities,
    LlmConfigurationError,
    LlmMessage,
    LlmPermanentProviderError,
    LlmProviderError,
    LlmRateLimitError,
    LlmStructuredOutputError,
    LlmTransientError,
    StructuredGenerationRequest,
    StructuredGenerationResult,
    TextGenerationRequest,
    TextGenerationResult,
    serialize_structured_generation_request,
    validate_structured_json,
)
from uptick_agent.llm.prompts import DEFAULT_SYSTEM_PROMPT
from uptick_agent.llm.registry import LlmProviderConfig
from uptick_agent.llm.structured_schema import normalize_output_schema
from uptick_agent.models import DecisionContext, V1NextStep


def _openai_provider_error(operation: str, error: Exception) -> LlmProviderError:
    if isinstance(error, (OpenAIAuthenticationError, PermissionDeniedError)):
        return LlmAuthenticationError(f"OpenAI {operation} authentication failed")
    if isinstance(error, OpenAIRateLimitError):
        return LlmRateLimitError(f"OpenAI {operation} was rate limited")
    if isinstance(error, APIConnectionError):
        return LlmTransientError(f"OpenAI {operation} connection failed")
    if isinstance(error, APIStatusError):
        if error.status_code in {408, 409, 429} or error.status_code >= 500:
            return LlmTransientError(f"OpenAI {operation} failed transiently")
        return LlmPermanentProviderError(f"OpenAI {operation} request was rejected")
    return LlmPermanentProviderError(f"OpenAI {operation} failed")


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _reported_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _openai_usage(completion: Any) -> dict[str, int | None] | None:
    usage = _field(completion, "usage")
    if usage is None:
        return None
    prompt_details = _field(usage, "prompt_tokens_details")
    completion_details = _field(usage, "completion_tokens_details")
    return {
        "input_tokens": _reported_int(_field(usage, "prompt_tokens")),
        "output_tokens": _reported_int(_field(usage, "completion_tokens")),
        "total_tokens": _reported_int(_field(usage, "total_tokens")),
        "cached_tokens": _reported_int(_field(prompt_details, "cached_tokens")),
        "reasoning_tokens": _reported_int(_field(completion_details, "reasoning_tokens")),
        # Provider pricing is not part of the OpenAI usage contract. Keep it
        # unavailable rather than presenting an amount without a currency.
        "cost_minor": None,
    }


def _telemetry(
    started: float,
    *,
    request_count: int,
    retry_count: int,
    usage: dict[str, int | None] | None,
) -> LlmCallTelemetry:
    values = usage or {}
    return LlmCallTelemetry(
        elapsed_seconds=max(0.0, monotonic() - started),
        request_count=request_count,
        retry_count=retry_count,
        usage_reported_requests=1 if usage is not None else 0,
        input_tokens=values.get("input_tokens"),
        output_tokens=values.get("output_tokens"),
        total_tokens=values.get("total_tokens"),
        cached_tokens=values.get("cached_tokens"),
        reasoning_tokens=values.get("reasoning_tokens"),
        cost_minor=values.get("cost_minor"),
    )


class OpenAILlmClient:
    """OpenAI implementation of the neutral LLM capability boundary.

    SDK request/response objects are contained in this adapter. ``client`` is an
    adapter-construction detail retained to support local tests and custom OpenAI
    transports; it is never accepted or returned by the neutral contracts.
    """

    provider_name = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        client: AsyncOpenAI | None = None,
        request_options: dict[str, Any] | None = None,
    ) -> None:
        if not model.strip():
            raise LlmConfigurationError("OpenAI model must not be blank")
        self.model = model
        self._request_options = dict(request_options or {})
        self._last_telemetry: LlmCallTelemetry | None = None
        self._owns_client = client is None
        if client is None:
            try:
                self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            except Exception as error:
                raise LlmConfigurationError("Could not initialize the OpenAI provider") from error
        else:
            self._client = client

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(structured_generation=True, text_generation=True)

    @property
    def last_telemetry(self) -> LlmCallTelemetry | None:
        return self._last_telemetry

    async def generate_structured[T](
        self, request: StructuredGenerationRequest[T]
    ) -> StructuredGenerationResult[T]:
        started = monotonic()
        request_count = 0
        usage: dict[str, int | None] | None = None
        completed = False
        self._last_telemetry = None
        try:
            model = request.model or self.model
            kwargs = self._request_kwargs(
                request.messages,
                request.settings.temperature,
                request.settings.max_output_tokens,
                request.settings.reasoning_effort,
            )
            schema = normalize_output_schema(request.response_model.model_json_schema())
            if not isinstance(schema, dict):
                raise LlmStructuredOutputError(
                    "OpenAI response schema must be an object; no value was accepted"
                )
            kwargs.update(
                {
                    "model": model,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": request.response_model.__name__,
                            "strict": True,
                            "schema": schema,
                        },
                    },
                }
            )
            request_count += 1
            try:
                completion = await self._client.chat.completions.create(**kwargs)
            except Exception as error:
                raise _openai_provider_error("structured generation", error) from error
            usage = _openai_usage(completion)
            try:
                message = completion.choices[0].message
                choice = completion.choices[0]
            except (AttributeError, IndexError, KeyError, TypeError):
                raise LlmStructuredOutputError(
                    "OpenAI returned a malformed structured response; no value was accepted"
                ) from None

            refusal = getattr(message, "refusal", None)
            if refusal:
                raise LlmStructuredOutputError("OpenAI refused structured generation")
            if getattr(message, "tool_calls", None) or getattr(message, "function_call", None):
                raise LlmStructuredOutputError(
                    "OpenAI returned tool use for a structured request; no value was accepted"
                )
            if getattr(choice, "finish_reason", None) != "stop":
                raise LlmStructuredOutputError(
                    "OpenAI returned an incomplete structured response; no value was accepted"
                )
            content = getattr(message, "content", None)
            if not isinstance(content, str) or not content.strip():
                raise LlmStructuredOutputError("OpenAI returned no structured response")
            try:
                value = validate_structured_json(request.response_model, content)
            except Exception:
                raise LlmStructuredOutputError(
                    "OpenAI returned invalid structured JSON; no value was accepted"
                ) from None
            telemetry = _telemetry(started, request_count=request_count, retry_count=0, usage=usage)
            self._last_telemetry = telemetry
            completed = True
            return StructuredGenerationResult(
                value=value,
                provider=self.provider_name,
                model=model,
                telemetry=telemetry,
            )
        finally:
            if not completed:
                self._last_telemetry = _telemetry(
                    started, request_count=request_count, retry_count=0, usage=usage
                )

    async def generate_text(self, request: TextGenerationRequest) -> TextGenerationResult:
        started = monotonic()
        request_count = 0
        usage: dict[str, int | None] | None = None
        completed = False
        self._last_telemetry = None
        try:
            model = request.model or self.model
            kwargs = self._request_kwargs(
                request.messages,
                request.settings.temperature,
                request.settings.max_output_tokens,
                request.settings.reasoning_effort,
            )
            kwargs["model"] = model
            request_count += 1
            try:
                completion = await self._client.chat.completions.create(**kwargs)
            except Exception as error:
                raise _openai_provider_error("text generation", error) from error
            usage = _openai_usage(completion)
            try:
                message = completion.choices[0].message
            except (AttributeError, IndexError, KeyError, TypeError) as error:
                raise LlmPermanentProviderError(
                    "OpenAI returned a malformed text response"
                ) from error

            if getattr(message, "refusal", None):
                raise LlmPermanentProviderError("OpenAI refused text generation")
            if getattr(message, "tool_calls", None) or getattr(message, "function_call", None):
                raise LlmPermanentProviderError("OpenAI returned tool use for a text-only request")
            text = getattr(message, "content", None)
            if not isinstance(text, str) or not text.strip():
                raise LlmPermanentProviderError("OpenAI returned no text response")
            telemetry = _telemetry(started, request_count=request_count, retry_count=0, usage=usage)
            self._last_telemetry = telemetry
            completed = True
            return TextGenerationResult(
                text=text,
                provider=self.provider_name,
                model=model,
                telemetry=telemetry,
            )
        finally:
            if not completed:
                self._last_telemetry = _telemetry(
                    started, request_count=request_count, retry_count=0, usage=usage
                )

    async def aclose(self) -> None:
        if self._owns_client:
            try:
                await self._client.close()
            except Exception as error:
                raise _openai_provider_error("client close", error) from error

    def _request_kwargs(
        self,
        messages: tuple[LlmMessage, ...],
        temperature: float | None,
        max_output_tokens: int | None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        kwargs = dict(self._request_options)
        kwargs["messages"] = [{"role": item.role, "content": item.content} for item in messages]
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_output_tokens is not None:
            kwargs["max_completion_tokens"] = max_output_tokens
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        return kwargs


class OpenAIProviderFactory:
    """Factory configurable with OpenAI transport details at composition time."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        request_options: dict[str, Any] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._request_options = request_options

    def create(self, config: LlmProviderConfig) -> OpenAILlmClient:
        if config.model is None:
            raise LlmConfigurationError("OpenAI provider configuration requires a model")
        return OpenAILlmClient(
            model=config.model,
            api_key=self._api_key,
            base_url=self._base_url,
            request_options=self._request_options,
        )


class OpenAISGRModel:
    """Compatibility facade retaining the ``DecisionModel`` contract for the runner."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        client: AsyncOpenAI | None = None,
        request_options: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.request_options = request_options or {}
        self._llm = OpenAILlmClient(
            model=model,
            api_key=api_key,
            base_url=base_url,
            client=client,
            request_options=self.request_options,
        )

    @property
    def last_telemetry(self) -> LlmCallTelemetry | None:
        return self._llm.last_telemetry

    async def decide(self, context: DecisionContext) -> V1NextStep:
        result = await self._llm.generate_structured(self._build_request(context))
        return result.value

    def prompt_trace(self, context: DecisionContext) -> dict[str, Any]:
        """Serialize the exact neutral request submitted by ``decide``."""
        return serialize_structured_generation_request(self._build_request(context))

    def _build_request(self, context: DecisionContext) -> StructuredGenerationRequest[V1NextStep]:
        return StructuredGenerationRequest(
            model=self.model,
            response_model=V1NextStep,
            messages=(
                LlmMessage(role="system", content=self.system_prompt),
                LlmMessage(
                    role="user",
                    content=(
                        "Choose the next action from this runtime context. JSON follows:\n"
                        + context.model_dump_json(indent=2)
                    ),
                ),
            ),
        )

    async def aclose(self) -> None:
        await self._llm.aclose()
