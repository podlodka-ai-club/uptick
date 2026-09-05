from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Literal

from uptick_agent._model_base import StrictModel
from uptick_agent.decisions.runtime import (
    MAX_PREVIOUS_DECISION_BYTES,
    RuntimeDecisionContext,
    ToolResult,
    serialize_previous_decision,
)
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.llm.contracts import (
    StructuredGenerationResult,
    serialize_structured_generation_request,
)
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.memory.contracts import DecisionMemoryContext
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.execute import AgentRunner
from uptick_agent.runs.runtime_results import RuntimeRunResult


class ToyAction(StrictModel):
    kind: Literal["collect", "apply", "finish"]
    marker: str = ""


class ToyDecision(StrictModel):
    plan: str
    action: ToyAction


class SecretDecision(StrictModel):
    plan: str
    credentials: dict[str, str]
    action: ToyAction


def test_previous_decision_is_redacted_and_hard_bounded_in_utf8() -> None:
    secret = "synthetic-continuity-secret"
    decision = SecretDecision(
        plan=f"retain api_key={secret}",
        credentials={"api_key": secret},
        action=ToyAction(kind="collect", marker="blue"),
    )

    safe = serialize_previous_decision(decision)
    assert safe is not None
    assert len(safe.encode("utf-8")) <= MAX_PREVIOUS_DECISION_BYTES
    safe_payload = json.loads(safe)
    assert safe_payload["credentials"] == "<redacted>"
    assert secret not in safe

    huge = SecretDecision(
        plan="🙂" * 5_000,
        credentials={"token": secret},
        action=ToyAction(kind="collect", marker="blue"),
    )
    truncated = serialize_previous_decision(huge)
    assert truncated is not None
    assert len(truncated.encode("utf-8")) <= MAX_PREVIOUS_DECISION_BYTES
    marker = json.loads(truncated)
    assert marker["_truncated"] is True
    assert marker["_original_bytes"] > MAX_PREVIOUS_DECISION_BYTES
    assert secret not in truncated


class RecordingClient:
    model = "continuity-test-model"

    def __init__(self) -> None:
        self.requests = []

    async def generate_structured(self, request):
        self.requests.append(request)
        return StructuredGenerationResult(
            value=ToyDecision(plan="returned", action=ToyAction(kind="finish")),
            provider="test",
            model=request.model,
        )

    async def aclose(self) -> None:
        return None


def test_structured_bridge_trace_matches_compact_request() -> None:
    async def scenario() -> None:
        client = RecordingClient()
        model = StructuredDecisionModel(client, response_model=ToyDecision)
        context = RuntimeDecisionContext(
            objective="complete the toy plan",
            run_id="toy-run",
            seed=1,
            iteration=2,
            max_steps=3,
            latest_result=ToolResult(action_kind="collect", summary="collected"),
            previous_decision=serialize_previous_decision(
                ToyDecision(plan="keep blue", action=ToyAction(kind="collect", marker="blue"))
            ),
        )

        trace = model.prompt_trace(context)
        await model.decide(context)

        request = client.requests[0]
        assert trace == serialize_structured_generation_request(request)
        assert request.messages[1].content == (
            "Choose the next action from this runtime context. JSON follows:\n"
            + context.model_dump_json()
        )
        assert "\n  " not in request.messages[1].content

    asyncio.run(scenario())


@dataclass
class ToySession:
    run_id: str
    seed: int


class EmptyMemory:
    """A deliberately empty AgentMemory implementation for a generic toy world."""

    def __init__(self) -> None:
        self.traces = []

    async def build_context(self, _request):
        return DecisionMemoryContext()

    async def remember(self, _entry) -> None:
        return None

    async def record_transition(self, _transition) -> None:
        return None

    async def clear(self, _run_id=None) -> None:
        return None

    async def finalize_run(self, _outcome) -> None:
        return None

    async def record_trace(self, write):
        self.traces.append(write)
        return None

    @property
    def context_diagnostics(self):
        return {}


class ToyEnvironment:
    decision_spec = EnvironmentDecisionSpec(
        response_model=ToyDecision,
        objective="complete the toy plan",
    )

    def __init__(self) -> None:
        self.starts = 0
        self.executed: list[tuple[str, str, str]] = []

    async def start(self, *, seed: int, agent_id: str, agent_version: str):
        del agent_id, agent_version
        self.starts += 1
        session = ToySession(run_id=f"toy-run-{self.starts}", seed=seed)
        return session, ToolResult(action_kind="start", summary="ready")

    def public_state(self, session: ToySession) -> dict[str, object]:
        return {"run_id": session.run_id, "executed": len(self.executed)}

    async def execute(self, session: ToySession, action: ToyAction):
        self.executed.append((session.run_id, action.kind, action.marker))
        return ToolResult(
            action_kind=action.kind,
            summary=f"{action.kind} completed",
            data={"marker": action.marker},
            terminal=action.kind == "finish",
        )

    async def finish(
        self,
        session: ToySession,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ) -> RuntimeRunResult:
        return RuntimeRunResult(
            run_id=session.run_id,
            seed=session.seed,
            agent_id="toy-agent",
            agent_version="1",
            status="completed",
            steps=steps,
            duration_seconds=duration_seconds,
            stop_reason=stop_reason,
        )


class PlanDependentToyModel:
    def __init__(self) -> None:
        self.contexts: list[RuntimeDecisionContext] = []

    def prompt_trace(self, context: RuntimeDecisionContext) -> dict[str, object]:
        return {"context": context.model_dump(mode="json")}

    async def decide(self, context: RuntimeDecisionContext) -> ToyDecision:
        self.contexts.append(context.model_copy(deep=True))
        if context.previous_decision is None:
            return ToyDecision(
                plan="remember the blue marker for the next step",
                action=ToyAction(kind="collect", marker="blue"),
            )
        previous = json.loads(context.previous_decision)
        if previous["plan"] == "remember the blue marker for the next step":
            return ToyDecision(
                plan="apply the blue marker",
                action=ToyAction(kind="apply", marker="blue"),
            )
        return ToyDecision(plan="finish after applying the marker", action=ToyAction(kind="finish"))


def test_generic_runner_carries_one_latest_plan_without_memory_and_resets_on_reuse() -> None:
    async def scenario() -> None:
        environment = ToyEnvironment()
        memory = EmptyMemory()
        model = PlanDependentToyModel()
        runner = AgentRunner(
            config=AgentConfig(
                agent_id="toy-agent",
                agent_version="continuity-test",
                max_steps=3,
                memory_recall_limit=0,
            ),
            model=model,
            memory=memory,
            environment=environment,
        )

        first = await runner.run(7)
        second = await runner.run(8)

        assert first.steps == second.steps == 3
        assert [kind for _run, kind, _marker in environment.executed] == [
            "collect",
            "apply",
            "finish",
            "collect",
            "apply",
            "finish",
        ]
        assert model.contexts[0].previous_decision is None
        assert model.contexts[1].previous_decision is not None
        assert "remember the blue marker" in model.contexts[1].previous_decision
        assert model.contexts[2].previous_decision is not None
        assert "apply the blue marker" in model.contexts[2].previous_decision
        assert "remember the blue marker" not in model.contexts[2].previous_decision
        assert model.contexts[3].previous_decision is None

        input_traces = [write for write in memory.traces if write.event_type == "decision.input"]
        assert len(input_traces) == len(model.contexts)
        for write, context in zip(input_traces, model.contexts, strict=True):
            assert write.raw_bodies["prompts"]["context"] == context.model_dump(mode="json")

    asyncio.run(scenario())
