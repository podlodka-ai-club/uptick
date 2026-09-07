"""Configuration and factory seam for selecting an LLM provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from uptick_agent.llm.contracts import LlmClient, LlmConfigurationError
from uptick_agent.llm.recovery import TimeoutRecoveryLlmClient, TimeoutRecoveryPolicy


@dataclass(frozen=True, slots=True)
class LlmProviderConfig:
    provider: str
    model: str | None = None
    timeout_recovery: TimeoutRecoveryPolicy | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("LLM provider name must not be blank")
        if self.timeout_recovery is not None and not isinstance(
            self.timeout_recovery, TimeoutRecoveryPolicy
        ):
            raise TypeError("timeout_recovery must be a TimeoutRecoveryPolicy or None")


class LlmProviderFactory(Protocol):
    def create(self, config: LlmProviderConfig) -> LlmClient: ...


class LlmProviderRegistry:
    """Explicit registry; application composition owns which providers are enabled."""

    def __init__(self) -> None:
        self._factories: dict[str, LlmProviderFactory] = {}

    def register(self, provider: str, factory: LlmProviderFactory) -> None:
        normalized = provider.strip().lower()
        if not normalized:
            raise ValueError("LLM provider name must not be blank")
        if normalized in self._factories:
            raise LlmConfigurationError(f"LLM provider {normalized!r} is already registered")
        self._factories[normalized] = factory

    def create(self, config: LlmProviderConfig) -> LlmClient:
        provider = config.provider.strip().lower()
        try:
            factory = self._factories[provider]
        except KeyError as error:
            choices = ", ".join(sorted(self._factories)) or "none"
            raise LlmConfigurationError(
                f"LLM provider {config.provider!r} is not registered (available: {choices})"
            ) from error
        client = factory.create(config)
        if config.timeout_recovery is None:
            return client
        return TimeoutRecoveryLlmClient(
            client,
            factory=factory,
            config=config,
            policy=config.timeout_recovery,
        )
