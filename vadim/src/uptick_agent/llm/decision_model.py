"""Structured decision bridge shared by CLI and local experiment runners."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from uptick_agent.decisions.instructions import CORE_SYSTEM_PROMPT, compose_system_prompt
from uptick_agent.decisions.runtime import RuntimeDecisionContext
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.llm.contracts import (
    GenerationSettings,
    LlmClient,
    LlmMessage,
    StructuredGenerationRequest,
    serialize_structured_generation_request,
)


class StructuredDecisionModel:
    """Compatibility bridge from the neutral LLM boundary to the runner."""

    def __init__(
        self,
        client: LlmClient,
        *,
        response_model: type[BaseModel],
        system_prompt: str | None = None,
        environment_briefing: str | None = None,
        settings: GenerationSettings | None = None,
    ) -> None:
        self._client = client
        self.model = getattr(client, "model", None)
        self._spec = EnvironmentDecisionSpec(response_model, environment_briefing)
        self._system_prompt = compose_system_prompt(
            CORE_SYSTEM_PROMPT if system_prompt is None else system_prompt,
            environment_briefing,
        )
        self.settings = settings or GenerationSettings()

    @property
    def response_model(self) -> type[BaseModel]:
        return self._spec.response_model

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def last_telemetry(self):
        return getattr(self._client, "last_telemetry", None)

    async def decide(self, context: RuntimeDecisionContext) -> BaseModel:
        result = await self._client.generate_structured(self._build_request(context))
        return result.value

    def prompt_trace(self, context: RuntimeDecisionContext) -> dict[str, Any]:
        """Serialize the exact neutral request submitted by ``decide``."""
        return serialize_structured_generation_request(self._build_request(context))

    def _build_request(
        self, context: RuntimeDecisionContext
    ) -> StructuredGenerationRequest[BaseModel]:
        self._spec.assert_unchanged()
        return StructuredGenerationRequest(
            model=self.model,
            settings=self.settings,
            response_model=self.response_model,
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
        await self._client.aclose()


__all__ = ["StructuredDecisionModel"]
