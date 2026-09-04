"""Provider-neutral contracts for language-model capabilities.

No type in this module is owned by a provider SDK.  Callers express the desired
capability and schema; adapters are solely responsible for authentication,
serialization, retries, and translating their SDK responses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

MessageRole = Literal["system", "user", "assistant"]


class LlmError(RuntimeError):
    """Base error exposed by the provider-neutral LLM boundary."""


class LlmConfigurationError(LlmError, ValueError):
    """A provider cannot be configured from the supplied neutral config."""


class LlmProviderError(LlmError):
    """A provider request, authentication check, or response failed."""


class LlmAuthenticationError(LlmProviderError):
    """Provider credentials or the selected account mode are invalid; do not retry."""


class LlmTransientError(LlmProviderError):
    """A bounded retry may succeed after a connection, timeout, limit, or server failure."""


class LlmRateLimitError(LlmTransientError):
    """The provider rejected the request because of a temporary rate limit."""


class LlmPermanentProviderError(LlmProviderError):
    """A non-retryable provider request or response failure."""


class LlmStructuredOutputError(LlmPermanentProviderError):
    """A structured response was absent, refused, malformed, or unsafe."""


class LlmUnsupportedCapabilityError(LlmError):
    """The selected provider honestly does not implement a requested capability."""


@dataclass(frozen=True, slots=True)
class LlmMessage:
    role: MessageRole
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError(f"unsupported LLM message role {self.role!r}")
        if not self.content.strip():
            raise ValueError("LLM message content must not be blank")


@dataclass(frozen=True, slots=True)
class GenerationSettings:
    """Portable generation controls supported by a provider when it can honor them."""

    temperature: float | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")


@dataclass(frozen=True, slots=True)
class StructuredGenerationRequest[T]:
    """A request for one locally validated value of ``response_model``.

    ``response_model`` must expose Pydantic's ``model_validate`` and/or
    ``model_validate_json`` methods. Keeping the schema type at this boundary
    avoids leaking a provider's JSON-schema or parsing object to callers.
    """

    messages: tuple[LlmMessage, ...]
    response_model: type[T]
    model: str | None = None
    settings: GenerationSettings = field(default_factory=GenerationSettings)

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("structured generation requires at least one message")
        required_methods = ("model_validate", "model_validate_json", "model_json_schema")
        if not isinstance(self.response_model, type) or not all(
            callable(getattr(self.response_model, name, None)) for name in required_methods
        ):
            raise ValueError("response_model must be a Pydantic model class")


def serialize_structured_generation_request(
    request: StructuredGenerationRequest[Any],
) -> dict[str, Any]:
    """Return a deterministic, provider-neutral representation of a request.

    The returned value deliberately describes the request before any provider
    adapter translates it into SDK arguments.  In particular, it contains no
    provider request/response objects and keeps the exact message order and
    content supplied by the caller.
    """
    response_model = request.response_model
    payload = {
        "messages": [
            {"role": message.role, "content": message.content}
            for message in request.messages
        ],
        "model": request.model,
        "settings": {
            "temperature": request.settings.temperature,
            "max_output_tokens": request.settings.max_output_tokens,
        },
        "response_model": {
            "module": response_model.__module__,
            "qualname": response_model.__qualname__,
        },
        "response_schema": response_model.model_json_schema(),
    }

    # JSON round-tripping both proves the boundary is JSON-safe and gives all
    # mapping keys a deterministic order without retaining SDK/model objects.
    return json.loads(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


@dataclass(frozen=True, slots=True)
class TextGenerationRequest:
    messages: tuple[LlmMessage, ...]
    model: str | None = None
    settings: GenerationSettings = field(default_factory=GenerationSettings)

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("text generation requires at least one message")


@dataclass(frozen=True, slots=True)
class StructuredGenerationResult[T]:
    value: T
    provider: str
    model: str | None


@dataclass(frozen=True, slots=True)
class TextGenerationResult:
    text: str
    provider: str
    model: str | None


@dataclass(frozen=True, slots=True)
class LlmCapabilities:
    structured_generation: bool
    text_generation: bool


class LlmClient(Protocol):
    """Capability-oriented LLM boundary consumed by reasoning and memory code."""

    @property
    def capabilities(self) -> LlmCapabilities: ...

    async def generate_structured[T](
        self, request: StructuredGenerationRequest[T]
    ) -> StructuredGenerationResult[T]: ...

    async def generate_text(self, request: TextGenerationRequest) -> TextGenerationResult: ...

    async def aclose(self) -> None: ...


def validate_structured_value[T](response_model: type[T], value: Any) -> T:
    """Locally validate a parsed provider value and fail closed otherwise."""
    validator = getattr(response_model, "model_validate", None)
    if not callable(validator):
        raise LlmStructuredOutputError(
            "structured response model must expose Pydantic model_validate"
        )
    try:
        return validator(value)
    except Exception as error:
        raise LlmStructuredOutputError(
            f"provider returned an invalid structured response: {error}"
        ) from error


def validate_structured_json[T](response_model: type[T], value: str) -> T:
    """Locally validate JSON returned by a provider without exposing its parser."""
    validator = getattr(response_model, "model_validate_json", None)
    if not callable(validator):
        raise LlmStructuredOutputError(
            "structured response model must expose Pydantic model_validate_json"
        )
    try:
        return validator(value)
    except Exception as error:
        raise LlmStructuredOutputError(
            f"provider returned an invalid structured response: {error}"
        ) from error
