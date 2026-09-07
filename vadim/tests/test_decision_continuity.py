from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Literal

import pytest

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
from uptick_agent.runs.observations import (
    MAX_OBSERVATION_HISTORY_BYTES,
    MAX_OBSERVATION_HISTORY_RECORDS,
    MAX_OBSERVATION_RECORD_BYTES,
    ObservationHistory,
)
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


def test_observation_metadata_cannot_overflow_on_long_kind_or_json_escapes() -> None:
    history = ObservationHistory()
    history.record(
        1,
        {"kind": "catalog", "query": "large action" * 500},
        ToolResult(
            action_kind="custom-environment-action" * 500,
            summary="failed: " + '\x00\n\\"' * 500,
            ok=False,
        ),
    )
    record = history.snapshot()[0]
    metadata = json.loads(record)
    assert len(record.encode("utf-8")) <= MAX_OBSERVATION_RECORD_BYTES
    assert metadata["result_ok"] is False
    assert metadata["result_summary"].startswith("failed: ")
    assert metadata["result_summary_truncated"] is True
    assert metadata["result_action_kind_truncated"] is True
    assert metadata["iteration"] == 1


def test_observation_history_replaces_exact_actions_and_preserves_variants() -> None:
    history = ObservationHistory()
    first_action = {"kind": "catalog", "token": "first-secret", "parameters": {"page": 1}}
    second_action = {"kind": "catalog", "token": "second-secret", "parameters": {"page": 1}}
    first_result = ToolResult(
        action_kind="catalog",
        summary="first failed",
        ok=False,
        data={"rows": ["first"], "api_key": "first-secret"},
    )
    second_result = ToolResult(
        action_kind="catalog",
        summary="second failed",
        ok=False,
        data={"rows": ["second"], "api_key": "second-secret"},
    )
    history.record(1, first_action, first_result)
    history.record(2, second_action, second_result)

    # A repeated exact action replaces its observation and moves to the tail;
    # a parameter/credential variant remains a separate observation.
    first_result.data["rows"].append("caller mutation")
    first_action["parameters"]["page"] = 99
    history.record(
        3,
        {"kind": "catalog", "token": "first-secret", "parameters": {"page": 1}},
        ToolResult(
            action_kind="catalog",
            summary="first recovered",
            data={"rows": ["recovered"]},
        ),
    )

    records = [json.loads(item) for item in history.snapshot()]
    assert len(records) == 2
    assert [item["iteration"] for item in records] == [2, 3]
    assert [item["result"]["summary"] for item in records] == [
        "second failed",
        "first recovered",
    ]
    assert all(item["result"]["ok"] is False for item in records[:1])
    assert all(item["action"]["token"] == "<redacted>" for item in records)
    assert all(item["result"]["data"]["api_key"] == "<redacted>" for item in records[:1])
    assert "first-secret" not in "".join(history.snapshot())
    assert "second-secret" not in "".join(history.snapshot())


def test_observation_history_is_bounded_in_utf8_and_marks_truncation() -> None:
    history = ObservationHistory()
    secret = "history-secret"
    for iteration in range(1, MAX_OBSERVATION_HISTORY_RECORDS + 8):
        history.record(
            iteration,
            {"kind": "catalog", "page": iteration, "query": "🙂" * 1_000},
            ToolResult(
                action_kind="catalog",
                summary=f"token={secret} " + "結果" * 1_000,
                data={"payload": "🚀" * 1_000},
            ),
        )

    records = history.snapshot()
    assert len(records) <= MAX_OBSERVATION_HISTORY_RECORDS
    assert len(json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()) <= (
        MAX_OBSERVATION_HISTORY_BYTES
    )
    assert all(len(record.encode("utf-8")) <= MAX_OBSERVATION_RECORD_BYTES for record in records)
    payloads = [json.loads(record) for record in records]
    assert any(payload.get("_truncated") is True for payload in payloads)
    truncated = next(payload for payload in payloads if payload.get("_truncated") is True)
    assert truncated["provenance"] == "environment.execute"
    assert isinstance(truncated["iteration"], int)
    assert secret not in "".join(records)


def test_truncated_large_action_keeps_result_failure_metadata() -> None:
    history = ObservationHistory()
    history.record(
        12,
        {"kind": "catalog", "query": "🙂" * 5_000},
        ToolResult(
            action_kind="catalog",
            ok=False,
            summary="catalog failed: permission denied " + "x" * 1_000,
            data={"rows": []},
        ),
    )

    payload = json.loads(history.snapshot()[0])
    assert payload["_truncated"] is True
    assert payload["iteration"] == 12
    assert payload["provenance"] == "environment.execute"
    assert payload["result_action_kind"] == "catalog"
    assert payload["result_ok"] is False
    assert payload["result_terminal"] is False
    assert payload["result_summary"].startswith("catalog failed: permission denied")
    assert payload["result_summary_truncated"] is True


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


class HistoryDependentToyModel:
    def __init__(self) -> None:
        self.contexts: list[RuntimeDecisionContext] = []

    def prompt_trace(self, context: RuntimeDecisionContext) -> dict[str, object]:
        return {"context": context.model_dump(mode="json")}

    async def decide(self, context: RuntimeDecisionContext) -> ToyDecision:
        self.contexts.append(context.model_copy(deep=True))
        if context.iteration == 1:
            return ToyDecision(
                plan="collect the blue catalog",
                action=ToyAction(kind="collect", marker="blue"),
            )
        if context.iteration <= 8:
            return ToyDecision(
                plan="collect an unrelated catalog",
                action=ToyAction(kind="collect", marker=f"noise-{context.iteration}"),
            )
        if context.iteration == 9:
            for record in context.observation_history:
                payload = json.loads(record)
                if (
                    payload.get("action", {}).get("marker") == "blue"
                    and payload.get("result", {}).get("data", {}).get("marker") == "blue"
                ):
                    return ToyDecision(
                        plan="apply the observed blue marker",
                        action=ToyAction(kind="apply", marker="blue"),
                    )
            raise AssertionError("the earlier catalog result was not available")
        return ToyDecision(plan="finish after applying the marker", action=ToyAction(kind="finish"))


def test_runner_opt_in_complete_observations_preserves_defaults_and_run_isolation() -> None:
    class LargeObservationEnvironment(ToyEnvironment):
        async def execute(self, session: ToySession, action: ToyAction):
            observed = await super().execute(session, action)
            if action.kind == "collect":
                observed.data.update(padding="界" * 300, run=session.run_id, token="private-secret")
            return observed

    async def exercise(option: int | None) -> list[RuntimeDecisionContext]:
        environment = LargeObservationEnvironment()
        model = PlanDependentToyModel()
        runner = AgentRunner(
            config=AgentConfig(max_steps=3, memory_recall_limit=0),
            model=model,
            memory=EmptyMemory(),
            environment=environment,
            observation_complete_record_bytes=option,
        )
        await runner.run(7)
        await runner.run(8)
        assert model.contexts[0].observation_history == []
        assert model.contexts[3].observation_history == []
        return model.contexts

    async def scenario() -> None:
        default = await exercise(None)
        explicit_baseline = await exercise(1_000)
        expanded = await exercise(2_000)
        for old, explicit in zip(default, explicit_baseline, strict=True):
            assert old.observation_history == explicit.observation_history
        for index, run in ((2, "toy-run-1"), (5, "toy-run-2")):
            baseline = json.loads(default[index].observation_history[0])
            restored = json.loads(expanded[index].observation_history[0])
            assert baseline["_truncated"] is True
            assert restored["iteration"] == baseline["iteration"] == 1
            assert restored["result"]["data"]["run"] == run
            assert restored["result"]["data"]["token"] == "<redacted>"
            assert "private-secret" not in expanded[index].observation_history[0]
            assert (
                len(json.dumps(expanded[index].observation_history, ensure_ascii=False).encode())
                <= MAX_OBSERVATION_HISTORY_BYTES
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", [True, 999])
def test_runner_rejects_invalid_complete_view_limit_before_starting_world(invalid: object) -> None:
    environment = ToyEnvironment()
    runner = AgentRunner(
        config=AgentConfig(max_steps=3),
        model=PlanDependentToyModel(),
        memory=EmptyMemory(),
        environment=environment,
        observation_complete_record_bytes=invalid,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError):
        asyncio.run(runner.run(7))
    assert environment.starts == 0
    assert environment.executed == []


def test_generic_runner_retains_observations_past_recent_steps_and_resets_on_reuse() -> None:
    async def scenario() -> None:
        environment = ToyEnvironment()
        memory = EmptyMemory()
        model = HistoryDependentToyModel()
        runner = AgentRunner(
            config=AgentConfig(
                agent_id="toy-agent",
                agent_version="observation-continuity-test",
                max_steps=10,
                memory_recall_limit=0,
            ),
            model=model,
            memory=memory,
            environment=environment,
        )

        first = await runner.run(7)
        second = await runner.run(8)

        assert first.steps == second.steps == 10
        ninth = model.contexts[8]
        assert all(step.action.marker != "blue" for step in ninth.recent_steps)
        assert any(
            json.loads(item)["action"]["marker"] == "blue" for item in ninth.observation_history
        )
        assert model.contexts[10].observation_history == []
        assert [kind for _run, kind, _marker in environment.executed].count("apply") == 2

    asyncio.run(scenario())


class SingleCollectionModel:
    def __init__(self) -> None:
        self.contexts: list[RuntimeDecisionContext] = []

    async def decide(self, context: RuntimeDecisionContext) -> ToyDecision:
        self.contexts.append(context.model_copy(deep=True))
        if context.iteration == 1:
            return ToyDecision(
                plan="collect blue",
                action=ToyAction(kind="collect", marker="blue"),
            )
        if context.iteration == 2:
            return ToyDecision(
                plan="collect green",
                action=ToyAction(kind="collect", marker="green"),
            )
        return ToyDecision(plan="finish", action=ToyAction(kind="finish"))


class MutatingToyEnvironment(ToyEnvironment):
    async def execute(self, session: ToySession, action: ToyAction):
        action.marker = "changed by environment"
        result = await super().execute(session, action)
        result.data["marker"] = "changed in result"
        return result


class MutatingObserver:
    async def on_step(self, record) -> None:
        record.decision.action.marker = "changed by observer"
        record.result.data["marker"] = "changed by observer"

    async def on_finish(self, _result) -> None:
        return None


def test_runner_snapshots_action_and_result_before_mutable_boundaries() -> None:
    async def scenario() -> None:
        environment = MutatingToyEnvironment()
        model = SingleCollectionModel()
        runner = AgentRunner(
            config=AgentConfig(
                agent_id="toy-agent",
                agent_version="observation-mutation-test",
                max_steps=3,
                memory_recall_limit=0,
            ),
            model=model,
            memory=EmptyMemory(),
            environment=environment,
            observer=MutatingObserver(),
        )

        await runner.run(7)

        assert environment.executed[0][2] == "changed by environment"
        assert model.contexts[1].observation_history == []
        third_context = model.contexts[2]
        observed = json.loads(third_context.observation_history[0])
        assert observed["action"]["marker"] == "blue"
        assert observed["result"]["data"]["marker"] == "changed in result"
        assert third_context.latest_result.data["marker"] == "changed in result"

    asyncio.run(scenario())
