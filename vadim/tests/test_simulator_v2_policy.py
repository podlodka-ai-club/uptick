import asyncio
from typing import Any

import pytest

from uptick_agent import cli
from uptick_agent.memory.contracts import DecisionMemoryContext, ObjectiveMetric
from uptick_agent.models import (
    AdvanceTimeStopCondition,
    AgentConfig,
    DecisionContext,
    FinishRun,
    NextStep,
    RunResult,
    RunState,
    ToolResult,
    V2AdvanceTime,
)
from uptick_agent.runner import AgentRunner
from uptick_agent.simulator.v2_policy import (
    V2_TIME_BUDGET_POLICY_ID,
    V2_TIME_BUDGET_POLICY_VERSION,
    SimulatorV2TimeBudgetPolicy,
    calculate_v2_time_budget,
)


def _context(
    remaining: object = 3_600,
    *,
    iteration: int = 1,
    max_steps: int = 5,
    operation_statuses: dict[str, str] | None = None,
    objective_metrics: list[ObjectiveMetric] | None = None,
) -> DecisionContext:
    data: dict[str, Any] = {"clock": {"remaining_seconds": remaining}}
    return DecisionContext(
        objective="uptime",
        run_id="run-1",
        seed=42,
        iteration=iteration,
        max_steps=max_steps,
        latest_result=ToolResult(
            action_kind="get_overview",
            summary="observed",
            data=data,
            objective_metrics=objective_metrics or [],
        ),
        run_state=RunState(operation_statuses=operation_statuses or {}),
    )


@pytest.mark.parametrize("remaining", [None, "3600", float("nan"), float("inf"), -1, 0])
def test_v2_time_budget_ignores_missing_or_nonfinite_clock(remaining: object) -> None:
    assert calculate_v2_time_budget(_context(remaining)) is None


def test_v2_time_budget_uses_fractional_public_clock_and_remaining_decisions() -> None:
    plan = calculate_v2_time_budget(_context(9_001.1, iteration=3, max_steps=10))

    assert plan is not None
    assert plan.remaining_seconds == 9_001.1
    assert plan.remaining_decisions == 8
    assert plan.wait_slots == 4
    assert plan.minimum_duration_seconds == 2_251


def test_v2_time_budget_can_derive_remaining_horizon_from_public_timestamps() -> None:
    context = _context(None).model_copy(
        update={
            "latest_result": ToolResult(
                action_kind="start",
                summary="started",
                data={
                    "clock": {
                        "simulation_time": "2033-03-01T00:00:00Z",
                        "simulation_ends_at": "2033-03-01T01:00:00Z",
                    }
                },
            )
        }
    )

    plan = calculate_v2_time_budget(context)

    assert plan is not None
    assert plan.remaining_seconds == 3_600


def test_v2_time_budget_uses_start_response_timestamps_without_clock_object() -> None:
    context = _context(None).model_copy(
        update={
            "latest_result": ToolResult(
                action_kind="start",
                summary="started",
                data={
                    "simulation_time": "2033-03-01T00:00:00Z",
                    "simulation_ends_at": "2033-03-01T01:00:00Z",
                },
            )
        }
    )

    plan = calculate_v2_time_budget(context)

    assert plan is not None
    assert plan.remaining_seconds == 3_600


def test_v2_policy_does_not_adjust_a_terminal_context() -> None:
    delegate = FakeDelegate(_advance_decision(duration=300))
    policy = SimulatorV2TimeBudgetPolicy(delegate)
    context = _context(9_001.1, iteration=3, max_steps=10).model_copy(
        update={
            "latest_result": ToolResult(
                action_kind="get_overview",
                summary="completed",
                data={"status": "completed", "clock": {"remaining_seconds": 1}},
                terminal=True,
            )
        }
    )

    async def scenario() -> None:
        decision = await policy.decide(context)
        assert decision.action.duration_seconds == 300
        assert "runtime-policy" not in decision.current_situation

    asyncio.run(scenario())


def test_v2_policy_does_not_treat_failed_operation_as_terminal_run() -> None:
    delegate = FakeDelegate(_advance_decision(duration=300))
    policy = SimulatorV2TimeBudgetPolicy(delegate)
    context = _context(9_001.1, iteration=3, max_steps=10).model_copy(
        update={
            "latest_result": ToolResult(
                action_kind="get_operation",
                summary="operation failed",
                data={"status": "failed", "clock": {"remaining_seconds": 9_001.1}},
                terminal=False,
            ),
            "run_state": RunState(operation_statuses={"op-1": "failed"}),
        }
    )

    async def scenario() -> None:
        decision = await policy.decide(context)
        assert decision.action.duration_seconds == 2_251
        assert "runtime-policy" in decision.current_situation

    asyncio.run(scenario())


class FakeDelegate:
    model = "fake-model"
    response_model = object
    system_prompt = "fake-prompt"

    def __init__(self, decision) -> None:
        self.decision = decision
        self.contexts: list[DecisionContext] = []
        self.closed = False

    async def decide(self, context: DecisionContext):
        self.contexts.append(context)
        return self.decision

    def prompt_trace(self, context: DecisionContext) -> dict[str, Any]:
        self.contexts.append(context)
        return {"delegate_context": context.model_dump(mode="json")}

    async def aclose(self) -> None:
        self.closed = True


def _advance_decision(*, duration: int = 300, stop_when: object = ...):
    if stop_when is ...:
        action = V2AdvanceTime(duration_seconds=duration)
    else:
        action = V2AdvanceTime(duration_seconds=duration, stop_when=stop_when)
    return NextStep(
        current_situation="the service is healthy",
        hypothesis="a bounded wait will reveal any new errors",
        remaining_steps=["observe after the wait"],
        task_completed=False,
        action=action,
    )


def test_v2_policy_adjustment_is_typed_auditable_and_prompt_trace_contains_same_math() -> None:
    delegate = FakeDelegate(_advance_decision(duration=300))
    policy = SimulatorV2TimeBudgetPolicy(delegate)
    context = _context(9_001.1, iteration=3, max_steps=10)

    async def scenario() -> None:
        trace = policy.prompt_trace(context)
        decision = await policy.decide(context)

        assert decision.action.duration_seconds == 2_251
        assert "runtime-policy" in decision.current_situation
        assert "proposed_duration_seconds=300" in decision.current_situation
        assert "effective_duration_seconds=2251" in decision.current_situation
        delegated = delegate.contexts[0].latest_result.data["runtime_policy"]
        trace_context = trace["delegate_context"]["latest_result"]["data"]["runtime_policy"]
        assert delegated["policy_id"] == V2_TIME_BUDGET_POLICY_ID
        assert delegated["policy_version"] == V2_TIME_BUDGET_POLICY_VERSION
        assert delegated["time_budget"]["minimum_duration_seconds"] == 2_251
        assert delegated["no_stop_eligibility"]["eligible"] is False
        assert delegated["no_stop_eligibility"]["reason"] == "unknown_metrics"
        assert delegated == trace_context
        assert "ceil(9001.1/max(1, 8//2))=2251" in delegated["time_budget"]["hint"]
        assert policy.model == "fake-model"
        assert policy.response_model is object
        assert policy.system_prompt == "fake-prompt"
        await policy.aclose()
        assert delegate.closed is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("action", "operation_statuses", "expected_duration", "expected_stop", "annotated"),
    [
        (_advance_decision(duration=2_251), {}, 2_251, "default", False),
        (_advance_decision(duration=300, stop_when=None), {}, 2_251, "default", True),
        (_advance_decision(duration=300), {"op-1": "accepted"}, 300, "default", False),
        (_advance_decision(duration=300), {"op-1": "pending"}, 300, "default", False),
        (_advance_decision(duration=300), {"op-1": "running"}, 300, "default", False),
    ],
)
def test_v2_policy_guards_unbounded_waits_and_preserves_pending_durations(
    action, operation_statuses, expected_duration, expected_stop, annotated
) -> None:
    delegate = FakeDelegate(action)
    policy = SimulatorV2TimeBudgetPolicy(delegate)

    async def scenario() -> None:
        decision = await policy.decide(
            _context(9_001.1, iteration=3, max_steps=10, operation_statuses=operation_statuses)
        )
        assert decision.action.duration_seconds == expected_duration
        assert ("None" if decision.action.stop_when is None else "default") == expected_stop
        assert ("runtime-policy" in decision.current_situation) is annotated

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "context",
    [
        _context(
            9_001.1,
            iteration=3,
            max_steps=10,
            objective_metrics=[ObjectiveMetric(name="uptime_ratio", value=0.999, unit="ratio")],
        ),
        _context(
            None,
            iteration=3,
            max_steps=10,
            objective_metrics=[
                ObjectiveMetric(name="downtime_seconds", value=500, unit="seconds"),
                ObjectiveMetric(name="observed_seconds", value=20_000, unit="seconds"),
            ],
        ),
    ],
)
def test_v2_policy_restores_default_stop_for_unknown_metrics_or_clock(context) -> None:
    delegate = FakeDelegate(_advance_decision(duration=300, stop_when=None))
    policy = SimulatorV2TimeBudgetPolicy(delegate)

    async def scenario() -> None:
        decision = await policy.decide(context)
        assert decision.action.stop_when is not None
        assert decision.action.stop_when == AdvanceTimeStopCondition()

    asyncio.run(scenario())


def test_v2_policy_keeps_stop_for_recoverable_low_current_uptime() -> None:
    delegate = FakeDelegate(_advance_decision(duration=300, stop_when=None))
    policy = SimulatorV2TimeBudgetPolicy(delegate)
    context = _context(
        10_000,
        objective_metrics=[
            ObjectiveMetric(name="downtime_seconds", value=2, unit="seconds"),
            ObjectiveMetric(name="observed_seconds", value=100, unit="seconds"),
        ],
    )

    async def scenario() -> None:
        decision = await policy.decide(context)
        assert decision.action.stop_when == AdvanceTimeStopCondition()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("downtime", "observed", "expected_allowance", "expected_eligible"),
    [(500, 20_000, 209, True), (10, 100, 10, False)],
)
def test_v2_policy_allows_no_stop_only_after_full_horizon_slo_proof(
    downtime, observed, expected_allowance, expected_eligible
) -> None:
    delegate = FakeDelegate(_advance_decision(duration=300, stop_when=None))
    policy = SimulatorV2TimeBudgetPolicy(delegate)
    context = _context(
        900,
        objective_metrics=[
            ObjectiveMetric(name="downtime_seconds", value=downtime, unit="seconds"),
            ObjectiveMetric(name="observed_seconds", value=observed, unit="seconds"),
        ],
    )

    async def scenario() -> None:
        trace = policy.prompt_trace(context)
        decision = await policy.decide(context)
        eligibility = trace["delegate_context"]["latest_result"]["data"]["runtime_policy"][
            "no_stop_eligibility"
        ]
        assert eligibility["eligible"] is expected_eligible
        assert eligibility["allowance_seconds"] == expected_allowance
        if expected_eligible:
            assert decision.action.stop_when is None
            assert "runtime-policy" not in decision.current_situation
        else:
            assert decision.action.stop_when == AdvanceTimeStopCondition()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "objective_metrics",
    [
        [
            ObjectiveMetric(name="downtime_seconds", value=500, unit="milliseconds"),
            ObjectiveMetric(name="observed_seconds", value=20_000, unit="seconds"),
        ],
        [
            ObjectiveMetric(name="downtime_seconds", value=500, unit="seconds"),
            ObjectiveMetric(name="downtime_seconds", value=500, unit="seconds"),
            ObjectiveMetric(name="observed_seconds", value=20_000, unit="seconds"),
        ],
        [
            ObjectiveMetric(name="downtime_seconds", value=20_001, unit="seconds"),
            ObjectiveMetric(name="observed_seconds", value=20_000, unit="seconds"),
        ],
    ],
)
def test_v2_policy_rejects_invalid_or_ambiguous_no_stop_counters(objective_metrics) -> None:
    delegate = FakeDelegate(_advance_decision(duration=300, stop_when=None))
    policy = SimulatorV2TimeBudgetPolicy(delegate)

    async def scenario() -> None:
        decision = await policy.decide(_context(1_000, objective_metrics=objective_metrics))
        assert decision.action.stop_when == AdvanceTimeStopCondition()

    asyncio.run(scenario())


def test_v2_policy_leaves_other_actions_unchanged() -> None:
    decision = NextStep(
        current_situation="inspect before waiting",
        hypothesis="the run may be complete",
        remaining_steps=[],
        task_completed=True,
        action=FinishRun(reason="the model believes the run is complete"),
    )
    delegate = FakeDelegate(decision)
    policy = SimulatorV2TimeBudgetPolicy(delegate)

    async def scenario() -> None:
        result = await policy.decide(_context(9_001.1, iteration=3, max_steps=10))
        assert result == decision

    asyncio.run(scenario())


def test_cli_wraps_only_v2_structured_models(monkeypatch) -> None:
    class Client:
        model = "fake-model"

    class Factory:
        def __init__(self, **kwargs) -> None:
            pass

        def create(self, config):
            return Client()

    monkeypatch.setattr(cli, "OpenAIProviderFactory", Factory)
    v1_args = cli._parser().parse_args(["run", "--seed", "1", "--simulator-api-version", "v1"])
    v2_args = cli._parser().parse_args(["run", "--seed", "1"])

    assert isinstance(cli._decision_model(v1_args), cli.StructuredDecisionModel)
    assert isinstance(cli._decision_model(v2_args), SimulatorV2TimeBudgetPolicy)


def test_v2_policy_runner_reaches_horizon_with_scripted_short_waits() -> None:
    class Session:
        run_id = "run-1"
        seed = 42

    class Environment:
        def __init__(self) -> None:
            self.remaining = 3_600
            self.durations: list[int] = []

        async def start(self, *, seed: int, agent_id: str, agent_version: str):
            return Session(), ToolResult(
                action_kind="start",
                summary="started",
                data={"clock": {"remaining_seconds": self.remaining}},
            )

        async def execute(self, session, action):
            self.durations.append(action.duration_seconds)
            self.remaining = max(0, self.remaining - action.duration_seconds)
            return ToolResult(
                action_kind=action.kind,
                summary="advanced",
                data={"clock": {"remaining_seconds": self.remaining}},
                terminal=self.remaining == 0,
            )

        async def finish(self, session, *, steps: int, duration_seconds: float, stop_reason: str):
            return RunResult(
                run_id=session.run_id,
                seed=session.seed,
                agent_id="agent",
                agent_version="v2",
                status="completed" if self.remaining == 0 else "running",
                steps=steps,
                duration_seconds=duration_seconds,
                objective_kind="uptime_cost",
                uptime_ratio=1.0 if self.remaining == 0 else None,
                slo_passed=True if self.remaining == 0 else None,
                total_cost_minor=0,
                stop_reason=stop_reason,
            )

    class Memory:
        async def build_context(self, request):
            return DecisionMemoryContext()

        async def remember(self, entry):
            return None

        async def record_transition(self, transition):
            return None

        async def clear(self, run_id=None):
            return None

        async def finalize_run(self, outcome):
            return None

        async def record_trace(self, write):
            return None

        @property
        def context_diagnostics(self):
            return {}

    delegate = FakeDelegate(_advance_decision(duration=300))
    environment = Environment()

    async def scenario() -> None:
        result = await AgentRunner(
            config=AgentConfig(agent_id="agent", agent_version="v2", max_steps=5),
            model=SimulatorV2TimeBudgetPolicy(delegate),
            memory=Memory(),
            environment=environment,
        ).run(42)
        assert result.status == "completed"
        assert result.steps == 3
        assert environment.durations == [1_800, 900, 900]

    asyncio.run(scenario())
