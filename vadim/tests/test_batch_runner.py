from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Literal

import pytest
from pydantic import Field

from uptick_agent._model_base import StrictModel
from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.memory.contracts import DecisionMemoryContext, ExperienceTransition
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.execute import AgentRunner
from uptick_agent.runs.runtime_results import RuntimeRunResult


class BatchAction(StrictModel):
    kind: Literal["collect", "apply"]
    marker: str


class BatchDecision(StrictModel):
    plan: str
    actions: list[BatchAction] = Field(min_length=1, max_length=4)


class SingleDecision(StrictModel):
    plan: str
    action: BatchAction


@dataclass
class BatchSession:
    run_id: str
    seed: int


class RecordingMemory:
    def __init__(self) -> None:
        self.entries = []
        self.transitions: list[ExperienceTransition] = []
        self.traces = []

    async def build_context(self, _request):
        return DecisionMemoryContext()

    async def remember(self, entry) -> None:
        self.entries.append(entry)

    async def record_transition(self, transition: ExperienceTransition) -> None:
        self.transitions.append(transition)

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


class RecordingObserver:
    def __init__(self) -> None:
        self.steps = []

    async def on_step(self, record) -> None:
        self.steps.append(record)

    async def on_finish(self, _result) -> None:
        return None


class BatchEnvironment:
    def __init__(self, response_model, *, barrier: str = "allow") -> None:
        self.decision_spec = EnvironmentDecisionSpec(
            response_model=response_model,
            objective="complete the public toy task",
        )
        self.barrier = barrier
        self.executed: list[tuple[str, str]] = []

    async def start(self, *, seed: int, agent_id: str, agent_version: str):
        del agent_id, agent_version
        return BatchSession(run_id="batch-run", seed=seed), ToolResult(
            action_kind="start", summary="ready"
        )

    def public_state(self, _session):
        return {"executed": len(self.executed)}

    async def execute(self, _session, action: BatchAction):
        self.executed.append((action.kind, action.marker))
        if self.barrier == "error" and len(self.executed) == 1:
            return ToolResult(
                action_kind=action.kind,
                summary="public action failed",
                ok=False,
                data={"marker": action.marker},
            )
        return ToolResult(
            action_kind=action.kind,
            summary=f"{action.marker} completed",
            data={"marker": action.marker},
            terminal=self.barrier == "terminal" and len(self.executed) == 1,
        )

    def can_continue_batch(self, _session, _result: ToolResult) -> bool:
        if self.barrier == "failing":
            raise RuntimeError("synthetic continuation hook failure")
        return self.barrier == "allow"

    async def finish(
        self,
        session,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ) -> RuntimeRunResult:
        return RuntimeRunResult(
            run_id=session.run_id,
            seed=session.seed,
            agent_id="batch-agent",
            agent_version="1",
            status="completed",
            steps=steps,
            duration_seconds=duration_seconds,
            stop_reason=stop_reason,
        )


class BatchModel:
    def __init__(self, response) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.calls = 0
        self.contexts = []

    def prompt_trace(self, context):
        return {"context": context.model_dump(mode="json")}

    async def decide(self, context):
        self.calls += 1
        self.contexts.append(context.model_copy(deep=True))
        return self.responses[min(self.calls - 1, len(self.responses) - 1)].model_copy(deep=True)


class NoContinuationHookEnvironment(BatchEnvironment):
    can_continue_batch = None


def _runner(environment, model, memory, *, max_actions=None, observer=None, max_steps=1):
    return AgentRunner(
        config=AgentConfig(max_steps=max_steps, memory_recall_limit=0),
        model=model,
        memory=memory,
        environment=environment,
        observer=observer,
        max_actions=max_actions,
    )


def test_opt_in_batch_records_each_result_with_distinct_audit_and_provenance() -> None:
    async def scenario() -> None:
        environment = BatchEnvironment(BatchDecision)
        memory = RecordingMemory()
        observer = RecordingObserver()
        result = await _runner(
            environment,
            BatchModel(
                BatchDecision(
                    plan="collect then apply",
                    actions=[
                        BatchAction(kind="collect", marker="blue"),
                        BatchAction(kind="apply", marker="blue"),
                    ],
                )
            ),
            memory,
            max_actions=2,
            observer=observer,
        ).run(3)

        assert environment.executed == [("collect", "blue"), ("apply", "blue")]
        # ``steps`` remains the outer decision count; action accounting is
        # represented by the two transitions and observer records.
        assert result.steps == 1
        assert result.action_count == 2
        assert [record.action_index for record in observer.steps] == [0, 1]
        assert [record.action["marker"] for record in observer.steps] == ["blue", "blue"]
        assert [transition.iteration for transition in memory.transitions] == [1, 1]
        assert len({transition.transition_id for transition in memory.transitions}) == 2
        assert (
            len(
                {
                    provenance.artefact_id
                    for transition in memory.transitions
                    for provenance in transition.provenance
                }
            )
            == 4
        )
        selected = [write for write in memory.traces if write.event_type == "decision.selected"]
        assert [write.metadata["action_index"] for write in selected] == [0, 1]
        assert len({write.event_id for write in selected}) == 2

    asyncio.run(scenario())


def test_default_single_action_keeps_legacy_transition_id_and_trace_shape() -> None:
    async def scenario() -> None:
        environment = BatchEnvironment(SingleDecision)
        memory = RecordingMemory()
        result = await _runner(
            environment,
            BatchModel(
                SingleDecision(
                    plan="collect once", action=BatchAction(kind="collect", marker="blue")
                )
            ),
            memory,
        ).run(3)

        assert environment.executed == [("collect", "blue")]
        assert (
            memory.transitions[0].transition_id
            == hashlib.sha256(b"experience-transition:batch-run:1").hexdigest()
        )
        selected = [write for write in memory.traces if write.event_type == "decision.selected"]
        assert len(selected) == 1
        assert "action_index" not in selected[0].metadata
        assert "action_count" not in result.model_dump(mode="json")

    asyncio.run(scenario())


@pytest.mark.parametrize("barrier", ["error", "pending"])
def test_batch_stops_before_tail_after_error_or_adapter_barrier(barrier: str) -> None:
    async def scenario() -> None:
        environment = BatchEnvironment(BatchDecision, barrier=barrier)
        memory = RecordingMemory()
        result = await _runner(
            environment,
            BatchModel(
                BatchDecision(
                    plan="stop when the environment blocks",
                    actions=[
                        BatchAction(kind="collect", marker="first"),
                        BatchAction(kind="apply", marker="must-not-run"),
                    ],
                )
            ),
            memory,
            max_actions=2,
        ).run(3)

        assert environment.executed == [("collect", "first")]
        assert len(memory.transitions) == 1
        assert result.steps == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("barrier", ["missing", "failing", "pending", "error", "terminal"])
def test_batch_barriers_control_next_decision_and_hide_unexecuted_tail(barrier: str) -> None:
    async def scenario() -> None:
        environment_class = (
            NoContinuationHookEnvironment if barrier == "missing" else BatchEnvironment
        )
        environment = environment_class(BatchDecision, barrier=barrier)
        memory = RecordingMemory()
        model = BatchModel(
            [
                BatchDecision(
                    plan="try a guarded pair",
                    actions=[
                        BatchAction(kind="collect", marker="first"),
                        BatchAction(kind="apply", marker="tail"),
                    ],
                ),
                BatchDecision(
                    plan="adapt after the first result",
                    actions=[BatchAction(kind="apply", marker="after-barrier")],
                ),
            ]
        )
        result = await _runner(
            environment,
            model,
            memory,
            max_actions=3,
            max_steps=2,
        ).run(3)

        assert environment.executed[0] == ("collect", "first")
        assert ("apply", "tail") not in environment.executed
        if barrier == "terminal":
            assert model.calls == 1
            assert len(environment.executed) == 1
            assert result.steps == 1
        else:
            assert model.calls == 2
            assert environment.executed == [
                ("collect", "first"),
                ("apply", "after-barrier"),
            ]
            next_context = model.contexts[1]
            history_results = [
                json.loads(record)["result"] for record in next_context.observation_history
            ]
            assert any(item["data"]["marker"] == "first" for item in history_results)
            assert all(item["data"]["marker"] != "tail" for item in history_results)
            assert next_context.latest_result.data["marker"] == "first"

    asyncio.run(scenario())


def test_batch_context_keeps_each_executed_result_after_batch_iteration() -> None:
    async def scenario() -> None:
        environment = BatchEnvironment(BatchDecision)
        memory = RecordingMemory()
        model = BatchModel(
            [
                BatchDecision(
                    plan="collect two facts",
                    actions=[
                        BatchAction(kind="collect", marker="first"),
                        BatchAction(kind="collect", marker="second"),
                    ],
                ),
                BatchDecision(
                    plan="continue after seeing both facts",
                    actions=[BatchAction(kind="apply", marker="third")],
                ),
            ]
        )
        await AgentRunner(
            config=AgentConfig(max_steps=2, memory_recall_limit=0),
            model=model,
            memory=memory,
            environment=environment,
            max_actions=3,
        ).run(3)

        assert environment.executed == [
            ("collect", "first"),
            ("collect", "second"),
            ("apply", "third"),
        ]
        second_context = model.contexts[1]
        assert model.contexts[0].max_actions == 3
        assert model.contexts[0].actions_executed == 0
        assert second_context.max_actions == 3
        assert second_context.actions_executed == 2
        observed_markers = {
            record["action"]["marker"]
            for record in map(json.loads, second_context.observation_history)
        }
        assert {"first", "second"}.issubset(observed_markers)
        assert second_context.latest_result.data["marker"] == "second"

    asyncio.run(scenario())


def test_batch_requires_explicit_budget_and_rejects_over_budget_before_execute() -> None:
    async def scenario() -> None:
        response = BatchDecision(
            plan="two actions", actions=[BatchAction(kind="collect", marker="a")] * 2
        )
        no_budget_environment = BatchEnvironment(BatchDecision)
        with pytest.raises(ValueError, match="explicit max_actions"):
            await _runner(
                no_budget_environment,
                BatchModel(response),
                RecordingMemory(),
            ).run(3)
        assert no_budget_environment.executed == []

        over_budget_environment = BatchEnvironment(BatchDecision)
        over_budget_result = await _runner(
            over_budget_environment,
            BatchModel(response),
            RecordingMemory(),
            max_actions=1,
        ).run(3)
        assert over_budget_environment.executed == [("collect", "a")]
        assert over_budget_result.steps == 1
        assert over_budget_result.stop_reason == "action budget exhausted"

    asyncio.run(scenario())


def test_batch_schema_rejects_more_than_four_actions_before_execute() -> None:
    async def scenario() -> None:
        response = BatchDecision.model_construct(
            plan="too many", actions=[BatchAction(kind="collect", marker=str(i)) for i in range(5)]
        )
        environment = BatchEnvironment(BatchDecision)
        with pytest.raises(ValueError, match="does not match"):
            await _runner(
                environment,
                BatchModel(response),
                RecordingMemory(),
                max_actions=4,
            ).run(3)
        assert environment.executed == []

    asyncio.run(scenario())


@pytest.mark.parametrize("malformed_kind", ["forged-kind", "missing-marker"])
def test_batch_revalidates_nested_actions_before_environment_execute(malformed_kind: str) -> None:
    async def scenario() -> None:
        if malformed_kind == "forged-kind":
            forged_action = BatchAction.model_construct(kind="delete", marker="forged")
        else:
            forged_action = {"kind": "collect"}
        response = BatchDecision.model_construct(plan="untrusted", actions=[forged_action])
        environment = BatchEnvironment(BatchDecision)
        with pytest.raises(ValueError, match="does not match"):
            await _runner(
                environment,
                BatchModel(response),
                RecordingMemory(),
                max_actions=1,
            ).run(3)
        assert environment.executed == []

    asyncio.run(scenario())


def test_timeout_recovery_runs_one_environment_action_after_two_provider_attempts():
    from uptick_agent.llm.contracts import LlmCapabilities, StructuredGenerationResult
    from uptick_agent.llm.decision_model import StructuredDecisionModel
    from uptick_agent.llm.recovery import TimeoutRecoveryPolicy
    from uptick_agent.llm.registry import LlmProviderConfig, LlmProviderRegistry

    async def scenario():
        requests = []
        clients = []

        class Client:
            model = "local-test"
            capabilities = LlmCapabilities(structured_generation=True, text_generation=False)
            last_telemetry = None

            def __init__(self, hung):
                self.hung = hung
                self.closed = False

            async def generate_structured(self, request):
                requests.append(request)
                if self.hung:
                    await asyncio.Event().wait()
                return StructuredGenerationResult(
                    value=BatchDecision(
                        plan="one action",
                        actions=[BatchAction(kind="apply", marker="once")],
                    ),
                    provider="fake",
                    model=self.model,
                )

            async def aclose(self):
                self.closed = True

        class Factory:
            def create(self, config):
                if clients:
                    assert clients[-1].closed
                client = Client(hung=not clients)
                clients.append(client)
                return client

        registry = LlmProviderRegistry()
        registry.register("fake", Factory())
        client = registry.create(
            LlmProviderConfig(
                provider="fake",
                timeout_recovery=TimeoutRecoveryPolicy(
                    attempt_timeout_seconds=0.02,
                    total_timeout_seconds=1,
                    max_attempts=2,
                ),
            )
        )
        environment = BatchEnvironment(BatchDecision)
        memory = RecordingMemory()
        try:
            result = await AgentRunner(
                config=AgentConfig(max_steps=1),
                model=StructuredDecisionModel(client, response_model=BatchDecision),
                memory=memory,
                environment=environment,
                max_actions=1,
            ).run(seed=1)
        finally:
            await client.aclose()
        assert len(clients) == 2
        assert requests[0] == requests[1]
        assert environment.executed == [("apply", "once")]
        assert len(memory.transitions) == 1
        assert result.steps == 1
        assert result.action_count == 1

    asyncio.run(scenario())
