import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from uptick_agent.decisions.contracts import NextStep
from uptick_agent.decisions.runtime import RuntimeDecisionContext, ToolResult
from uptick_agent.memory.contracts import ObjectiveMetric
from uptick_agent.simulator.actions import (
    AdvanceTimeStopCondition,
    GetMetrics,
    GetOverview,
    V2AdvanceTime,
)
from uptick_agent.simulator.decisions import SimulatorV2BatchDecision
from uptick_agent.simulator.v2_environment import SimulatorV2Environment, SimulatorV2Session
from uptick_agent.simulator.v2_policy import (
    V2_BOUNDED_NO_STOP_POLICY_VERSION,
    V2_TIME_BUDGET_POLICY_VERSION,
    SimulatorV2TimeBudgetPolicy,
)

RUN_ID = "bounded-public-run"
START = "2030-01-01T00:00:00Z"
END = "2030-01-08T00:00:00Z"
OMIT_RUN_ID = object()


class Delegate:
    model = "fake"
    response_model = object
    system_prompt = "fake"
    last_telemetry = None

    def __init__(self, decision: NextStep) -> None:
        self.decision = decision
        self.contexts: list[RuntimeDecisionContext] = []

    async def decide(self, context: RuntimeDecisionContext) -> NextStep:
        self.contexts.append(context)
        return self.decision

    def prompt_trace(self, context: RuntimeDecisionContext) -> dict[str, Any]:
        self.contexts.append(context)
        return {"context": context.model_dump(mode="json")}

    async def aclose(self) -> None:
        return None


def _metrics(downtime: float, observed: float) -> list[ObjectiveMetric]:
    return [
        ObjectiveMetric(name="downtime_seconds", value=downtime, unit="seconds"),
        ObjectiveMetric(name="observed_seconds", value=observed, unit="seconds"),
    ]


def _advance(
    duration: int,
    stop_when: AdvanceTimeStopCondition | None = None,
) -> NextStep:
    return NextStep(
        current_situation="Use the current public state.",
        hypothesis="The requested interval fits the public evidence.",
        remaining_steps=["Observe the next public result."],
        task_completed=False,
        action=V2AdvanceTime(duration_seconds=duration, stop_when=stop_when),
    )


def _context(
    *,
    current_at: str = START,
    current_remaining: float = 604_800,
    source_at: str | None = None,
    source_remaining: float | None = None,
    downtime: float = 0,
    observed: float = 0,
    fresh: bool = False,
    current_run_id: object = RUN_ID,
    cached_run_id: str | None = RUN_ID,
    ok: bool = True,
    statuses: dict[str, str] | None = None,
    include_end: bool = True,
) -> RuntimeDecisionContext:
    source_at = source_at or current_at
    source_remaining = current_remaining if source_remaining is None else source_remaining
    current_clock: dict[str, Any] = {
        "simulation_time": current_at,
        "remaining_seconds": current_remaining,
    }
    if include_end:
        current_clock["simulation_ends_at"] = END
    current_data: dict[str, Any] = {"clock": current_clock}
    if current_run_id is not OMIT_RUN_ID:
        current_data["run_id"] = current_run_id
    state: dict[str, Any] = {"operation_statuses": statuses or {}, "last_observed": {}}
    latest_metrics: list[ObjectiveMetric] = []
    action_kind = "get_metrics" if fresh else "get_resources"
    if fresh:
        latest_metrics = _metrics(downtime, observed)
    else:
        view: dict[str, Any] = {
            "action_kind": "get_metrics",
            "observed_at": source_at,
            "freshness": "observed" if source_at == current_at else "stale",
            "stale": source_at != current_at,
            "data": {
                "clock": {
                    "simulation_time": source_at,
                    "remaining_seconds": source_remaining,
                }
            },
            "objective_metrics": [
                metric.model_dump(mode="json") for metric in _metrics(downtime, observed)
            ],
        }
        if cached_run_id is not None:
            view["run_id"] = cached_run_id
        state["last_observed"]["get_metrics"] = view
    return RuntimeDecisionContext(
        objective="Maintain the stated public availability target.",
        run_id=RUN_ID,
        seed=42,
        iteration=10,
        max_steps=480,
        latest_result=ToolResult(
            action_kind=action_kind,
            ok=ok,
            summary="Public state observed.",
            data=current_data,
            objective_metrics=latest_metrics,
        ),
        run_state=state,
    )


def _evidence(delegate: Delegate, index: int = -1) -> dict[str, Any]:
    return delegate.contexts[index].latest_result.data["runtime_policy"][
        "bounded_no_stop_eligibility"
    ]


def test_opt_in_fresh_proof_matches_prompt_trace_and_decide() -> None:
    delegate = Delegate(_advance(300, None))
    policy = SimulatorV2TimeBudgetPolicy(delegate, bounded_no_stop=True)
    context = _context(fresh=True)

    async def scenario() -> None:
        trace = policy.prompt_trace(context)
        decision = await policy.decide(context)
        traced = trace["context"]["latest_result"]["data"]["runtime_policy"]
        decided = delegate.contexts[1].latest_result.data["runtime_policy"]
        assert traced == decided
        assert traced["policy_version"] == V2_BOUNDED_NO_STOP_POLICY_VERSION
        assert policy.policy_version == V2_BOUNDED_NO_STOP_POLICY_VERSION
        assert decision.action.stop_when is None
        assert decision.action.duration_seconds == 300
        evidence = decided["bounded_no_stop_eligibility"]
        assert evidence["eligible"] is True
        assert evidence["reason"] == "bounded_slo_headroom_verified"
        assert evidence["source"] == "latest_typed_metrics"
        assert evidence["source_run_id"] == RUN_ID
        assert evidence["source_horizon_seconds"] == 604_800
        assert evidence["maximum_duration_seconds"] == 5_448

    asyncio.run(scenario())


def test_opt_in_accepts_api_shaped_latest_without_body_run_id() -> None:
    contexts = [_context(fresh=True, current_run_id=OMIT_RUN_ID)]
    for action_kind in ("get_resources", "query_logs", "get_operation"):
        context = _context(current_run_id=OMIT_RUN_ID)
        latest = context.latest_result
        data = latest.data
        if action_kind == "get_operation":
            data = {
                "type": "time.advance",
                "operation_id": "public-operation",
                "request_id": "public-request",
                "status": "succeeded",
                "result": data,
            }
        contexts.append(
            context.model_copy(
                update={
                    "latest_result": latest.model_copy(
                        update={"action_kind": action_kind, "data": data}
                    )
                }
            )
        )

    async def scenario() -> None:
        for context in contexts:
            delegate = Delegate(_advance(300, None))
            decision = await SimulatorV2TimeBudgetPolicy(delegate, bounded_no_stop=True).decide(
                context
            )
            assert decision.action.stop_when is None, context.latest_result.action_kind
            evidence = _evidence(delegate)
            assert evidence["eligible"] is True
            assert evidence["source_run_id"] == RUN_ID

    asyncio.run(scenario())


def test_cached_stale_proof_charges_elapsed_and_repeated_wait_without_refresh() -> None:
    allowed_context = _context(
        current_at="2030-01-02T04:03:20Z",
        current_remaining=503_800,
        source_at="2030-01-02T03:46:40Z",
        source_remaining=504_800,
        downtime=4_140,
        observed=100_000,
    )
    later_context = _context(
        current_at="2030-01-02T04:03:29Z",
        current_remaining=503_791,
        source_at="2030-01-02T03:46:40Z",
        source_remaining=504_800,
        downtime=4_140,
        observed=100_000,
    )

    async def scenario() -> None:
        first_delegate = Delegate(_advance(300, None))
        first = await SimulatorV2TimeBudgetPolicy(first_delegate, bounded_no_stop=True).decide(
            allowed_context
        )
        assert first.action.stop_when is None
        assert _evidence(first_delegate)["elapsed_seconds"] == 1_000
        assert _evidence(first_delegate)["maximum_duration_seconds"] == 308

        second_delegate = Delegate(_advance(300, None))
        second = await SimulatorV2TimeBudgetPolicy(second_delegate, bounded_no_stop=True).decide(
            later_context
        )
        assert second.action.stop_when == AdvanceTimeStopCondition()
        assert second.action.duration_seconds == 300
        assert _evidence(second_delegate)["elapsed_seconds"] == 1_009
        assert _evidence(second_delegate)["maximum_duration_seconds"] == 299

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["accepted", "unknown"])
def test_opt_in_preserves_existing_pending_300_second_exception(status: str) -> None:
    async def scenario() -> None:
        delegate = Delegate(_advance(300, None))
        policy = SimulatorV2TimeBudgetPolicy(delegate, bounded_no_stop=True)
        context = _context(fresh=True, statuses={"operation-public": status})
        trace = policy.prompt_trace(context)
        decision = await policy.decide(context)
        traced = trace["context"]["latest_result"]["data"]["runtime_policy"]
        decided = delegate.contexts[1].latest_result.data["runtime_policy"]
        assert traced == decided
        assert decision.action.stop_when is None
        assert decision.action.duration_seconds == 300
        assert decided["pending_no_stop_eligibility"]["eligible"] is True
        assert decided["bounded_no_stop_eligibility"]["eligible"] is False
        assert decided["bounded_no_stop_eligibility"]["reason"] == "unresolved_operation"

        over_bound_delegate = Delegate(_advance(301, None))
        over_bound = await SimulatorV2TimeBudgetPolicy(
            over_bound_delegate, bounded_no_stop=True
        ).decide(context)
        assert over_bound.action.stop_when == AdvanceTimeStopCondition()
        assert over_bound.action.duration_seconds == 301

    asyncio.run(scenario())


def test_exact_duration_reserve_threshold_and_no_duration_increase() -> None:
    context = _context(
        current_at="2030-01-02T03:46:40Z",
        current_remaining=504_800,
        source_at="2030-01-02T03:46:40Z",
        source_remaining=504_800,
        downtime=4_148,
        observed=100_000,
    )

    async def scenario() -> None:
        allowed_delegate = Delegate(_advance(1_300, None))
        allowed = await SimulatorV2TimeBudgetPolicy(allowed_delegate, bounded_no_stop=True).decide(
            context
        )
        assert allowed.action.stop_when is None
        assert _evidence(allowed_delegate)["maximum_duration_seconds"] == 1_300

        rejected_delegate = Delegate(_advance(1_301, None))
        rejected = await SimulatorV2TimeBudgetPolicy(
            rejected_delegate, bounded_no_stop=True
        ).decide(context)
        assert rejected.action.stop_when == AdvanceTimeStopCondition()
        assert rejected.action.duration_seconds == 1_301

        end_context = _context(
            current_at="2030-01-07T23:55:00Z",
            current_remaining=300,
            downtime=0,
            observed=604_500,
            fresh=True,
        )
        end_delegate = Delegate(_advance(301, None))
        beyond_end = await SimulatorV2TimeBudgetPolicy(end_delegate, bounded_no_stop=True).decide(
            end_context
        )
        assert _evidence(end_delegate)["maximum_duration_seconds"] == 300
        assert beyond_end.action.stop_when == AdvanceTimeStopCondition()
        assert beyond_end.action.duration_seconds == 301

    asyncio.run(scenario())


def test_opt_in_fails_closed_for_identity_operations_clocks_and_horizon() -> None:
    conflict = _context(
        current_at="2030-01-02T03:46:40Z",
        current_remaining=504_800,
        source_at="2030-01-02T03:46:40Z",
        source_remaining=504_800,
        observed=100_000,
    )
    conflict_state = deepcopy(conflict.run_state)
    conflict_state["last_observed"]["get_overview"] = {
        **deepcopy(conflict_state["last_observed"]["get_metrics"]),
        "action_kind": "get_overview",
        "objective_metrics": [metric.model_dump(mode="json") for metric in _metrics(1, 100_000)],
    }
    conflict = conflict.model_copy(update={"run_state": conflict_state})
    cases = [
        (_context(current_run_id=None, fresh=True), "invalid_current_run_id"),
        (_context(current_run_id=7, fresh=True), "invalid_current_run_id"),
        (_context(current_run_id="other", fresh=True), "wrong_current_run"),
        (_context(ok=False, fresh=True), "failed_latest_result"),
        (_context(cached_run_id=None), "missing_cached_run_id"),
        (_context(cached_run_id="other"), "wrong_run_observation"),
        (
            _context(
                current_at="2030-01-02T03:46:39Z",
                current_remaining=504_801,
                source_at="2030-01-02T03:46:40Z",
                source_remaining=504_800,
                observed=100_000,
            ),
            "future_observation",
        ),
        (
            _context(
                current_at="2030-01-02T04:03:20Z",
                current_remaining=503_801,
                source_at="2030-01-02T03:46:40Z",
                source_remaining=504_800,
                observed=100_000,
                include_end=False,
            ),
            "clock_horizon_mismatch",
        ),
        (
            _context(
                current_remaining=604_799,
                source_remaining=604_799,
                include_end=False,
            ),
            "measurement_horizon_mismatch",
        ),
        (conflict, "ambiguous_observations"),
    ]

    async def scenario() -> None:
        for context, reason in cases:
            delegate = Delegate(_advance(300, None))
            decision = await SimulatorV2TimeBudgetPolicy(delegate, bounded_no_stop=True).decide(
                context
            )
            assert decision.action.stop_when == AdvanceTimeStopCondition(), reason
            assert decision.action.duration_seconds == 300, reason
            assert _evidence(delegate)["eligible"] is False, reason
            assert _evidence(delegate)["reason"] == reason

    asyncio.run(scenario())


def test_default_and_explicit_stops_and_batch_contract_remain_independent() -> None:
    default_delegate = Delegate(_advance(300, AdvanceTimeStopCondition()))
    default_policy = SimulatorV2TimeBudgetPolicy(default_delegate)
    custom_stop = AdvanceTimeStopCondition(error_codes=["DISK_FULL"])
    explicit_delegate = Delegate(_advance(3_000, custom_stop))

    async def scenario() -> None:
        default = await default_policy.decide(
            RuntimeDecisionContext(
                objective="public objective",
                run_id=RUN_ID,
                seed=42,
                iteration=1,
                max_steps=5,
                latest_result=ToolResult(
                    action_kind="get_overview",
                    summary="public",
                    data={"clock": {"remaining_seconds": 3_600}},
                ),
            )
        )
        assert default_policy.policy_version == V2_TIME_BUDGET_POLICY_VERSION
        assert default.action.duration_seconds == 1_800
        assert default.action.stop_when == AdvanceTimeStopCondition()
        metadata = default_delegate.contexts[0].latest_result.data["runtime_policy"]
        assert "bounded_no_stop_eligibility" not in metadata

        explicit = await SimulatorV2TimeBudgetPolicy(
            explicit_delegate, bounded_no_stop=True
        ).decide(_context(fresh=True))
        assert explicit.action.duration_seconds == 3_000
        assert explicit.action.stop_when == custom_stop
        assert explicit.action.stop_when.new_log_errors == 1

    asyncio.run(scenario())

    with pytest.raises(ValidationError):
        SimulatorV2BatchDecision(
            current_situation="Use public state.",
            hypothesis="Exercise the production boundary.",
            remaining_steps=[],
            task_completed=False,
            actions=[V2AdvanceTime(duration_seconds=300), GetOverview()],
        )

    with pytest.raises(TypeError, match="bounded_no_stop must be a boolean"):
        SimulatorV2TimeBudgetPolicy(Delegate(_advance(300)), bounded_no_stop=1)  # type: ignore[arg-type]


def test_environment_owned_cache_carries_public_run_identity() -> None:
    session = SimulatorV2Session(
        run_id=RUN_ID,
        seed=42,
        agent_id="test",
        agent_version="1",
        status="running",
        simulation_time=datetime(2030, 1, 1, tzinfo=UTC),
        logs_from=datetime(2030, 1, 1, tzinfo=UTC),
    )
    environment = SimulatorV2Environment(object())  # type: ignore[arg-type]
    result = ToolResult(
        action_kind="get_metrics",
        summary="Public metrics.",
        data={
            "run_id": RUN_ID,
            "clock": {
                "simulation_time": START,
                "simulation_ends_at": END,
                "remaining_seconds": 604_800,
            },
            "current": {},
        },
        objective_metrics=_metrics(0, 0),
    )

    environment._update_last_observed(session, result, GetMetrics())

    view = environment.public_state(session)["last_observed"]["get_metrics"]
    assert view["run_id"] == RUN_ID
