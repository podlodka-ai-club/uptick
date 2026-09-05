"""Structured decision bridge shared by CLI and local experiment runners."""

from __future__ import annotations

from typing import Any

from uptick_agent.decisions.contracts import DecisionContext, NextStep
from uptick_agent.llm.contracts import (
    GenerationSettings,
    LlmClient,
    LlmMessage,
    StructuredGenerationRequest,
    serialize_structured_generation_request,
)
from uptick_agent.llm.prompts import DEFAULT_SYSTEM_PROMPT


class StructuredDecisionModel:
    """Compatibility bridge from the neutral LLM boundary to the runner."""

    def __init__(
        self,
        client: LlmClient,
        *,
        response_model: type[NextStep] = NextStep,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        settings: GenerationSettings | None = None,
    ) -> None:
        self._client = client
        self.model = getattr(client, "model", None)
        self.response_model = response_model
        self.system_prompt = system_prompt
        self.settings = settings or GenerationSettings()

    @property
    def last_telemetry(self):
        return getattr(self._client, "last_telemetry", None)

    async def decide(self, context: DecisionContext) -> NextStep:
        result = await self._client.generate_structured(self._build_request(context))
        return result.value

    def prompt_trace(self, context: DecisionContext) -> dict[str, Any]:
        """Serialize the exact neutral request submitted by ``decide``."""
        return serialize_structured_generation_request(self._build_request(context))

    def _build_request(self, context: DecisionContext) -> StructuredGenerationRequest[NextStep]:
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
