from __future__ import annotations

from collections.abc import Iterable

from uptick_agent.core.models import (
    JsonObject,
    ReasonerConfig,
    ReasonerResult,
    ReasoningRequest,
    ReasoningTelemetry,
)


class ScriptedReasoner:
    """Deterministic structured-output reasoner for tests and local examples."""

    def __init__(
        self,
        outputs: Iterable[JsonObject],
        *,
        config: ReasonerConfig | None = None,
    ) -> None:
        self._outputs = list(outputs)
        self.requests: list[ReasoningRequest] = []
        self.config = config or ReasonerConfig(
            provider="scripted",
            model="scripted",
            thread_mode="stateless",
        )

    async def reason(self, request: ReasoningRequest) -> ReasonerResult:
        self.requests.append(request)
        if not self._outputs:
            raise RuntimeError("scripted reasoner has no output remaining")
        output = self._outputs.pop(0)
        parsed = request.output_model.model_validate(output)
        return ReasonerResult(
            output=parsed.model_dump(mode="json"),
            telemetry=ReasoningTelemetry(
                provider="scripted",
                requested_model=self.config.model,
                reported_model=self.config.model,
                requested_effort=self.config.effort,
                reported_effort=self.config.effort,
                thread_mode=self.config.thread_mode,
                duration_seconds=0,
                sdk_name="uptick-agent-scripted",
                sdk_version="1",
            ),
        )
