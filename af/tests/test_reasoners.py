import asyncio
from types import SimpleNamespace
from typing import Any, cast

from openai import AsyncOpenAI

from tests.helpers import make_context
from uptick_agent.core.models import AgentContext, ReasonerConfig, SGREnvelope
from uptick_agent.core.sgr import CurrentSGR
from uptick_agent.reasoners.openai import OpenAIReasoner


def _context() -> AgentContext:
    return make_context(step_limit=2)


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any):
        self.calls.append(kwargs)
        parsed = SGREnvelope.model_validate(
            {
                "phase": "observe",
                "facts": ["the run is ready"],
                "competing_hypotheses": ["inspect the run"],
                "contradicting_evidence": [],
                "previous_verification": {"status": "not_applicable", "evidence": []},
                "strategy": "inspect before acting",
                "selected_action": {"name": "inspect", "arguments": {}},
                "expected_result": ["inspection returns state"],
                "verification": ["the observation contains state"],
                "task_completed": False,
            }
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=parsed.model_dump_json(), refusal=None)
                )
            ],
            usage=None,
        )


def test_openai_reasoner_uses_typed_output_without_live_model() -> None:
    async def scenario() -> None:
        completions = FakeCompletions()
        fake = SimpleNamespace(
            chat=SimpleNamespace(completions=completions),
            close=lambda: None,
        )
        reasoner = OpenAIReasoner(
            config=ReasonerConfig(
                provider="openai",
                model="fake-model",
                thread_mode="stateless",
                timeout_seconds=600,
                retries=2,
            ),
            client=cast(AsyncOpenAI, fake),
        )
        request = CurrentSGR().build_request(_context())

        result = await reasoner.reason(request)

        assert result.output["selected_action"] == {"name": "inspect", "arguments": {}}
        assert result.telemetry.provider == "openai"
        response_format = completions.calls[0]["response_format"]
        assert response_format["type"] == "json_schema"
        schema = response_format["json_schema"]["schema"]
        assert schema["properties"]["selected_action"]["anyOf"][0]["properties"]["name"] == {
            "type": "string",
            "enum": ["inspect"],
        }

    asyncio.run(scenario())
