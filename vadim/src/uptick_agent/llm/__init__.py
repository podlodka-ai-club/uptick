"""Lazy compatibility facade for provider-neutral LLM contracts."""

from importlib import import_module

__all__ = [
    "GenerationSettings",
    "LlmCapabilities",
    "LlmClient",
    "LlmConfigurationError",
    "LlmError",
    "LlmAuthenticationError",
    "LlmCallTelemetry",
    "LlmMessage",
    "LlmProviderConfig",
    "LlmProviderError",
    "LlmPermanentProviderError",
    "LlmRateLimitError",
    "ReasoningEffort",
    "LlmProviderFactory",
    "LlmProviderRegistry",
    "LlmStructuredOutputError",
    "LlmUnsupportedCapabilityError",
    "LlmTransientError",
    "OpenAIProviderFactory",
    "OpenAISGRModel",
    "StructuredGenerationRequest",
    "StructuredGenerationResult",
    "TextGenerationRequest",
    "TextGenerationResult",
    "serialize_structured_generation_request",
]

_EXPORTS = {
    **{
        name: ("contracts", name)
        for name in (
            "GenerationSettings",
            "LlmCapabilities",
            "LlmClient",
            "LlmConfigurationError",
            "LlmError",
            "LlmAuthenticationError",
            "LlmCallTelemetry",
            "LlmMessage",
            "LlmProviderError",
            "LlmPermanentProviderError",
            "LlmRateLimitError",
            "ReasoningEffort",
            "LlmStructuredOutputError",
            "LlmUnsupportedCapabilityError",
            "LlmTransientError",
            "StructuredGenerationRequest",
            "StructuredGenerationResult",
            "TextGenerationRequest",
            "TextGenerationResult",
            "serialize_structured_generation_request",
        )
    },
    "LlmProviderConfig": ("registry", "LlmProviderConfig"),
    "LlmProviderFactory": ("registry", "LlmProviderFactory"),
    "LlmProviderRegistry": ("registry", "LlmProviderRegistry"),
    "OpenAIProviderFactory": ("openai", "OpenAIProviderFactory"),
    "OpenAISGRModel": ("openai", "OpenAISGRModel"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute_name = target
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value
