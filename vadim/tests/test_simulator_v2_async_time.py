import asyncio
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from uptick_agent.simulator.actions import GetOperation, V2AdvanceTime
from uptick_agent.simulator.v2_environment import SimulatorV2Environment, SimulatorV2Session

OP = "0123456789abcdef"


def session():
    return SimulatorV2Session(
        run_id="run",
        seed=42,
        agent_id="agent",
        agent_version="test",
        status="running",
        simulation_time=datetime(2033, 1, 1, tzinfo=UTC),
        logs_from=None,
    )


def completed_result(remaining=300):
    return {
        "clock": {
            "simulation_time": "2033-01-01T00:05:00Z",
            "simulation_ends_at": "2033-01-01T00:10:00Z",
            "remaining_seconds": remaining,
            "real_elapsed_seconds": 0,
            "applied_advance_seconds": 300,
        },
        "previous_simulation_time": "2033-01-01T00:00:00Z",
        "requested_duration_seconds": 300,
        "processed_events": 8,
        "new_logs": 4,
        "stop_reason": "duration_elapsed",
    }


class Client:
    def __init__(self, status="queued", result=None):
        self.calls = []
        self.status = status
        self.result = result

    async def advance_time(self, run_id, **kwargs):
        self.calls.append(("advance", kwargs))
        return {
            "run_id": run_id,
            "request_id": kwargs["request_id"],
            "operation_id": OP,
            "status": "queued",
        }

    async def operation(self, run_id, operation_id):
        self.calls.append(("operation", operation_id))
        value = {
            "run_id": run_id,
            "request_id": "wait-001",
            "operation_id": operation_id,
            "type": "time.advance",
            "status": self.status,
            "submitted_at": "2026-09-06T00:00:00Z",
            "started_at": None,
            "completed_at": None,
            "result": deepcopy(self.result),
        }
        if self.status == "failed":
            value["error"] = {"error": "CALCULATION_FAILED", "message": "calculation failed"}
        return value


def test_acceptance_then_pending_then_success_preserves_nested_public_result():
    async def check():
        client, state = Client(), session()
        env = SimulatorV2Environment(client)
        before = state.simulation_time
        accepted = await env.execute(state, V2AdvanceTime(duration_seconds=300))
        assert (
            accepted.ok
            and not accepted.terminal
            and accepted.operation_links[0].relation == "initiated"
        )
        assert state.simulation_time == before
        pending = await env.execute(state, GetOperation(operation_id=OP))
        assert pending.ok and not pending.terminal and pending.data["result"] is None
        assert state.simulation_time == before and state.operation_statuses[OP] == "queued"
        client.status, client.result = "succeeded", completed_result()
        result = await env.execute(state, GetOperation(operation_id=OP))
        assert result.ok and not result.terminal
        assert result.data["result"] == client.result and "clock" not in result.data
        assert state.simulation_time.isoformat() == "2033-01-01T00:05:00+00:00"
        assert state.operation_statuses[OP] == "succeeded"
        assert len(client.calls) == 3  # No adapter auto-poll or implicit advance.

    asyncio.run(check())


@pytest.mark.parametrize(
    "status,result,ok,terminal",
    [
        ("failed", None, False, False),
        ("succeeded", completed_result(0), True, True),
        ("succeeded", None, False, False),
        ("running", completed_result(), False, False),
    ],
)
def test_time_operation_failure_and_horizon_semantics(status, result, ok, terminal):
    async def check():
        env, state = SimulatorV2Environment(Client(status, result)), session()
        response = await env.execute(state, GetOperation(operation_id=OP))
        assert response.ok is ok and response.terminal is terminal
        if status == "failed":
            assert response.data["error"]["error"] == "CALCULATION_FAILED"
            assert response.operation_links[0].relation == "observed"
        if not ok:
            assert state.simulation_time == session().simulation_time

    asyncio.run(check())


def test_completed_advance_marks_previously_cached_metrics_stale():
    async def check():
        state = session()
        from uptick_agent.simulator.timestamps import parse_rfc3339

        state.last_observed_at["get_metrics"] = parse_rfc3339("2033-01-01T00:00:00Z")
        state.last_observed_views["get_metrics"] = {"stale": False, "freshness": "fresh"}
        env = SimulatorV2Environment(Client("succeeded", completed_result()))
        result = await env.execute(state, GetOperation(operation_id=OP))
        assert result.ok and state.last_observed_views["get_metrics"]["stale"] is True

    asyncio.run(check())


@pytest.mark.parametrize("http_status", [409, 429])
def test_busy_error_keeps_public_operation_reference_for_recovery(http_status):
    from uptick_agent.simulator.actions import GetOverview
    from uptick_agent.simulator.v2_client import SimulatorV2ApiError

    class BusyClient:
        async def overview(self, run_id):
            raise SimulatorV2ApiError(
                http_status,
                "RUN_BUSY",
                "still calculating",
                details={"operation_id": OP},
                retry_after_seconds=2,
            )

    result = asyncio.run(SimulatorV2Environment(BusyClient()).execute(session(), GetOverview()))
    assert not result.ok and not result.terminal
    assert result.data["details"] == {"operation_id": OP}
    assert result.data["retry_after_seconds"] == 2


@pytest.mark.parametrize(
    "field,value",
    [("run_id", "other"), ("operation_id", "otheroperation123"), ("status", "unknown")],
)
def test_foreign_or_invalid_time_operation_cannot_update_clock(field, value):
    class Invalid(Client):
        async def operation(self, run_id, operation_id):
            response = await super().operation(run_id, operation_id)
            response[field] = value
            return response

    state = session()
    response = asyncio.run(
        SimulatorV2Environment(Invalid("succeeded", completed_result())).execute(
            state, GetOperation(operation_id=OP)
        )
    )
    assert not response.ok and response.data["code"] == "INVALID_RESPONSE"
    assert state.simulation_time == session().simulation_time


def context(result):
    from uptick_agent.decisions.runtime import RuntimeDecisionContext

    return RuntimeDecisionContext(
        objective="public SLO",
        run_id="run",
        seed=42,
        iteration=3,
        max_steps=10,
        latest_result=result,
    )


@pytest.mark.parametrize(
    "status,expected", [("succeeded", 300), ("running", None), ("failed", None)]
)
def test_policy_reads_only_completed_time_operation_clock(status, expected):
    from uptick_agent.decisions.runtime import ToolResult
    from uptick_agent.simulator.v2_policy import calculate_v2_time_budget

    ctx = context(
        ToolResult(
            action_kind="get_operation",
            summary="public result",
            data={"type": "time.advance", "status": status, "result": completed_result()},
        )
    )
    plan = calculate_v2_time_budget(ctx)
    assert (None if plan is None else plan.remaining_seconds) == expected


@pytest.mark.parametrize(
    "kind,data",
    [
        ("advance_time_v2", {"operation_id": OP, "status": "queued"}),
        (
            "get_operation",
            {"operation_id": OP, "type": "time.advance", "status": "running", "result": None},
        ),
        ("get_overview", {"code": "RUN_BUSY", "status_code": 409, "details": {"operation_id": OP}}),
        ("get_overview", {"code": "RUN_BUSY", "status_code": 429, "details": {"operation_id": OP}}),
    ],
)
def test_policy_pending_calculation_hint_matches_new_public_protocol(kind, data):
    from uptick_agent.decisions.runtime import ToolResult
    from uptick_agent.simulator.decisions import SimulatorV2Decision
    from uptick_agent.simulator.v2_policy import SimulatorV2TimeBudgetPolicy

    class Delegate:
        async def decide(self, ctx):
            metadata = ctx.latest_result.data["runtime_policy"]["time_budget"]
            assert metadata["pending_time_operation_id"] == OP
            assert "Poll get_operation" in metadata["hint"]
            assert "get_metrics" not in metadata["hint"]
            return SimulatorV2Decision(
                current_situation="pending",
                hypothesis="poll",
                remaining_steps=[],
                task_completed=False,
                action=GetOperation(operation_id=OP),
            )

    decision = asyncio.run(
        SimulatorV2TimeBudgetPolicy(Delegate()).decide(
            context(
                ToolResult(
                    action_kind=kind, summary="public result", data=data, ok=kind != "get_overview"
                )
            )
        )
    )
    assert decision.action == GetOperation(operation_id=OP)
