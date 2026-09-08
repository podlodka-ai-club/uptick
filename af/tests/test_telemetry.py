import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai import AsyncOpenAI

from tests.helpers import make_context
from uptick_agent.core.errors import ReasonerFailure
from uptick_agent.core.models import (
    AgentContext,
    CapabilityCall,
    ReasonerConfig,
    SGREnvelope,
    VerificationAssessment,
)
from uptick_agent.core.sgr import CurrentSGR
from uptick_agent.reasoners._telemetry import normalize_token_usage
from uptick_agent.reasoners.openai import OpenAIReasoner


def _context() -> AgentContext:
    return make_context(step_limit=2)


def _valid_output() -> str:
    return SGREnvelope(
        phase="observe",
        facts=["the run is ready"],
        competing_hypotheses=["inspect the run"],
        contradicting_evidence=[],
        previous_verification=VerificationAssessment(status="not_applicable"),
        strategy="inspect before acting",
        selected_action=CapabilityCall(name="inspect"),
        expected_result=["inspection returns state"],
        verification=["the observation contains state"],
        task_completed=False,
    ).model_dump_json()


class SequencedCompletions:
    def __init__(self, contents: list[str | None]) -> None:
        self.contents = contents
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        content = self.contents.pop(0)
        choices = (
            []
            if content is None
            else [SimpleNamespace(message=SimpleNamespace(content=content, refusal=None))]
        )
        return SimpleNamespace(
            model="reported-model",
            reasoning_effort="high",
            system_fingerprint="runtime-1",
            service_tier="default",
            choices=choices,
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 3},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        )


@pytest.mark.parametrize("first_content", ["not-json", None])
def test_openai_metadata_usage_and_invalid_output_retry_are_accounted(
    first_content: str | None,
) -> None:
    async def scenario() -> None:
        completions = SequencedCompletions([first_content, _valid_output()])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        ticks = iter([10.0, 12.5])
        reasoner = OpenAIReasoner(
            config=ReasonerConfig(
                provider="openai",
                model="requested-model",
                effort="high",
                thread_mode="stateless",
                timeout_seconds=30,
                retries=1,
            ),
            client=cast(AsyncOpenAI, client),
            monotonic_fn=lambda: next(ticks),
        )

        result = await reasoner.reason(CurrentSGR().build_request(_context()))

        telemetry = result.telemetry
        assert telemetry.requested_model == "requested-model"
        assert telemetry.reported_model == "reported-model"
        assert telemetry.requested_effort == "high"
        assert telemetry.reported_effort == "high"
        assert telemetry.thread_mode == "stateless"
        assert telemetry.attempts == 2
        assert telemetry.duration_seconds == 2.5
        assert telemetry.provider_runtime == {
            "system_fingerprint": "runtime-1",
            "service_tier": "default",
        }
        assert telemetry.token_usage.input_tokens == 20
        assert telemetry.token_usage.output_tokens == 8
        assert telemetry.token_usage.total_tokens == 28
        assert telemetry.token_usage.cached_input_tokens == 6
        assert telemetry.token_usage.reasoning_output_tokens == 4
        assert len(cast(list[object], telemetry.raw_usage["attempts"])) == 2
        assert all(call["reasoning_effort"] == "high" for call in completions.calls)

    asyncio.run(scenario())


def test_openai_timeout_retries_then_raises_typed_failure() -> None:
    class NeverCompletes:
        async def create(self, **kwargs: Any) -> Any:
            del kwargs
            await asyncio.Event().wait()

    async def scenario() -> None:
        client = SimpleNamespace(chat=SimpleNamespace(completions=NeverCompletes()))
        reasoner = OpenAIReasoner(
            config=ReasonerConfig(
                provider="openai",
                model="timeout-model",
                thread_mode="stateless",
                timeout_seconds=0.001,
                retries=1,
            ),
            client=cast(AsyncOpenAI, client),
        )

        with pytest.raises(ReasonerFailure) as captured:
            await reasoner.reason(CurrentSGR().build_request(_context()))

        assert captured.value.category == "transient"
        assert captured.value.telemetry.attempts == 2
        assert captured.value.telemetry.duration_seconds > 0

    asyncio.run(scenario())


def test_token_normalization_covers_codex_and_openai_shapes() -> None:
    codex, codex_raw = normalize_token_usage(
        {
            "last": {"input_tokens": 99},
            "total": {
                "input_tokens": 8,
                "output_tokens": 5,
                "total_tokens": 13,
                "cached_input_tokens": 2,
                "reasoning_output_tokens": 3,
                "cache_write_input_tokens": 1,
            },
        },
        provider="codex",
    )
    openai, _ = normalize_token_usage(
        {
            "prompt_tokens": 7,
            "completion_tokens": 2,
            "total_tokens": 9,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
        provider="openai",
    )

    assert codex.model_dump() == {
        "input_tokens": 8,
        "output_tokens": 5,
        "total_tokens": 13,
        "cached_input_tokens": 2,
        "reasoning_output_tokens": 3,
        "cache_write_input_tokens": 1,
    }
    assert json.loads(json.dumps(codex_raw))["total"]["total_tokens"] == 13
    assert openai.total_tokens == 9
    assert openai.cached_input_tokens == 4
    assert openai.reasoning_output_tokens == 1
