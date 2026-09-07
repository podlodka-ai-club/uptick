from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime

from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.memory.contracts import ObjectiveMetric
from uptick_agent.simulator.actions import (
    AdvanceTime,
    GetMetrics,
    GetOverview,
    GetResources,
)
from uptick_agent.simulator.v2_environment import SimulatorV2Environment, SimulatorV2Session
from uptick_agent.v2_actions import ControlCommand, ServerCreateParams, ServerCreateRequest

_CLOCK_0 = "2033-03-01T00:00:00Z"
_CLOCK_1 = "2033-03-01T00:05:00Z"
_CLOCK_FRACTION_A = "2033-03-01T00:00:00.123456Z"
_CLOCK_FRACTION_B = "2033-03-01T00:00:00.1234567Z"
_CLOCK_FRACTION_C = "2033-03-01T00:00:00.1234561Z"


def _clock(value: str, *, remaining: int = 3_600) -> dict[str, object]:
    return {"simulation_time": value, "remaining_seconds": remaining}


def _session() -> SimulatorV2Session:
    started = datetime(2033, 3, 1, tzinfo=UTC)
    return SimulatorV2Session(
        run_id="run-1",
        seed=42,
        agent_id="agent",
        agent_version="1.0",
        status="running",
        simulation_time=started,
        logs_from=started,
    )


class _ViewClient:
    def __init__(self) -> None:
        self.metrics_clock = _CLOCK_0
        self.advance_clock = _CLOCK_1
        self.resource_rows: list[dict[str, object]] = [
            {"server_id": f"backend-{index}", "role": "backend", "status": "active"}
            for index in range(1_000)
        ]

    async def metrics(self, run_id: str) -> dict[str, object]:
        return {
            "clock": _clock(self.metrics_clock),
            "current": {"uptime_ratio": 0.99, "downtime_seconds": 10},
            "series": [{"timestamp": "history"}],
        }

    async def resources(self, run_id: str) -> dict[str, object]:
        return {
            "clock": _clock(_CLOCK_0),
            "active_instances": 2,
            "total_capacity_units": 200,
            "servers": copy.deepcopy(self.resource_rows),
        }

    async def overview(self, run_id: str) -> dict[str, object]:
        return {
            "clock": _clock(_CLOCK_0),
            "status": "running",
            "site_status": "healthy",
            "server_count": 2,
            "capacity_utilization": 0.2,
            "error_rate": 0,
            "availability": {"uptime_ratio": 0.99},
            "costs": {"total_cost_minor": 15},
        }

    async def advance_time(
        self, run_id: str, *, request_id: str, duration_seconds: int, stop_when: object
    ) -> dict[str, object]:
        return {"clock": _clock(self.advance_clock), "applied_advance_seconds": duration_seconds}

    async def execute_command(
        self, run_id: str, *, request_id: str, command: str, params: dict[str, object]
    ) -> dict[str, object]:
        return {"clock": _clock(_CLOCK_0), "operation_id": "operation-1", "status": "queued"}


def _view(environment: SimulatorV2Environment, session: SimulatorV2Session, kind: str):
    return environment.public_state(session)["last_observed"][kind]  # type: ignore[index]


def test_last_observed_views_keep_exact_clock_and_are_deep_copied() -> None:
    async def scenario() -> None:
        environment = SimulatorV2Environment(_ViewClient())  # type: ignore[arg-type]
        session = _session()

        await environment.execute(session, GetMetrics())
        state = environment.public_state(session)
        metrics = state["last_observed"]["get_metrics"]  # type: ignore[index]
        assert metrics["observed_at"] == _CLOCK_0
        assert metrics["freshness"] == "observed"
        assert metrics["stale"] is False
        assert metrics["data"]["current"]["uptime_ratio"] == 0.99  # type: ignore[index]
        assert "series" not in metrics["data"]  # type: ignore[operator]
        assert metrics["objective_metrics"] == [  # type: ignore[comparison-overlap]
            {"name": "uptime_ratio", "value": 0.99, "unit": "ratio"},
            {"name": "downtime_seconds", "value": 10.0, "unit": "seconds"},
        ]

        metrics["data"]["current"]["uptime_ratio"] = 0  # type: ignore[index]
        fresh_state = environment.public_state(session)
        assert (
            fresh_state["last_observed"]["get_metrics"]["data"]["current"]["uptime_ratio"] == 0.99
        )  # type: ignore[index]

    asyncio.run(scenario())


def test_newer_server_clock_stales_old_views_and_clock_regression_cannot_refresh_them() -> None:
    async def scenario() -> None:
        client = _ViewClient()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        session = _session()

        await environment.execute(session, GetMetrics())
        await environment.execute(session, AdvanceTime(duration_seconds=300))
        stale = _view(environment, session, "get_metrics")
        assert stale["observed_at"] == _CLOCK_0
        assert stale["freshness"] == "stale"
        assert stale["stale"] is True

        client.metrics_clock = "2033-03-01T00:01:00Z"
        await environment.execute(session, GetMetrics())
        regressed = _view(environment, session, "get_metrics")
        assert regressed["observed_at"] == _CLOCK_0
        assert regressed["stale"] is True
        assert regressed["freshness"] == "stale"

    asyncio.run(scenario())


def test_clock_order_preserves_sub_microsecond_server_timestamps() -> None:
    async def scenario() -> None:
        client = _ViewClient()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        session = _session()

        client.metrics_clock = _CLOCK_FRACTION_A
        await environment.execute(session, GetMetrics())
        client.metrics_clock = _CLOCK_FRACTION_B
        await environment.execute(session, GetMetrics())
        assert _view(environment, session, "get_metrics")["observed_at"] == _CLOCK_FRACTION_B

        # All three values become the same datetime at Python's microsecond
        # precision; the wire parser must still reject the older exact value.
        client.metrics_clock = _CLOCK_FRACTION_C
        await environment.execute(session, GetMetrics())
        view = _view(environment, session, "get_metrics")
        assert view["observed_at"] == _CLOCK_FRACTION_B
        assert view["freshness"] == "observed"

    asyncio.run(scenario())


def test_out_of_order_first_view_is_explicitly_stale() -> None:
    async def scenario() -> None:
        client = _ViewClient()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        session = _session()

        await environment.execute(session, GetMetrics())
        await environment.execute(session, AdvanceTime(duration_seconds=300))
        await environment.execute(session, GetResources())

        resources = _view(environment, session, "get_resources")
        assert resources["observed_at"] == _CLOCK_0
        assert resources["freshness"] == "stale"
        assert resources["stale"] is True

    asyncio.run(scenario())


def test_successful_mutating_control_command_stales_views_without_time_advance() -> None:
    async def scenario() -> None:
        environment = SimulatorV2Environment(_ViewClient())  # type: ignore[arg-type]
        session = _session()
        await environment.execute(session, GetMetrics())
        action = ControlCommand(
            request=ServerCreateRequest(
                command="server.create",
                params=ServerCreateParams(
                    name="backend-new", role="backend", instance_type="small"
                ),
            )
        )

        result = await environment.execute(session, action)
        assert result.ok is True
        view = _view(environment, session, "get_metrics")
        assert view["observed_at"] == _CLOCK_0
        assert view["stale"] is True
        assert view["freshness"] == "stale"

    asyncio.run(scenario())


def test_successful_mutation_without_clock_stales_views() -> None:
    async def scenario() -> None:
        environment = SimulatorV2Environment(_ViewClient())  # type: ignore[arg-type]
        session = _session()
        await environment.execute(session, GetMetrics())
        action = ControlCommand(
            request=ServerCreateRequest(
                command="server.create",
                params=ServerCreateParams(
                    name="backend-new", role="backend", instance_type="small"
                ),
            )
        )

        environment._update_last_observed(
            session,
            ToolResult(action_kind="control_command", summary="accepted", data={}),
            action,
        )
        view = _view(environment, session, "get_metrics")
        assert view["stale"] is True
        assert view["freshness"] == "stale"

    asyncio.run(scenario())


def test_failed_read_with_clock_cannot_replace_last_successful_view() -> None:
    async def scenario() -> None:
        environment = SimulatorV2Environment(_ViewClient())  # type: ignore[arg-type]
        session = _session()
        await environment.execute(session, GetMetrics())

        failure = ToolResult(
            action_kind="get_metrics",
            ok=False,
            summary="read failed after a newer clock",
            data={
                "clock": _clock(_CLOCK_1),
                "current": {"uptime_ratio": 0.1, "downtime_seconds": 99},
            },
        )
        environment._update_last_observed(session, failure, GetMetrics())
        view = _view(environment, session, "get_metrics")
        assert view["observed_at"] == _CLOCK_0
        assert view["stale"] is True
        assert view["data"]["current"]["uptime_ratio"] == 0.99  # type: ignore[index]

    asyncio.run(scenario())


def test_resource_view_is_role_labelled_and_bounded_with_explicit_omission_count() -> None:
    async def scenario() -> None:
        client = _ViewClient()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        session = _session()
        await environment.execute(session, GetResources())

        view = _view(environment, session, "get_resources")
        payload = view["data"]
        assert payload["servers"][0]["role"] == "backend"  # type: ignore[index]
        assert payload["truncation"]["omitted_items"] > 0  # type: ignore[index]
        assert (
            len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= 12_000
        )

    asyncio.run(scenario())


def test_three_cached_views_fit_the_total_bound() -> None:
    async def scenario() -> None:
        client = _ViewClient()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        session = _session()
        await environment.execute(session, GetMetrics())
        await environment.execute(session, GetResources())
        await environment.execute(session, GetOverview())

        views = environment.public_state(session)["last_observed"]
        assert len(views) == 3  # type: ignore[arg-type]
        assert len(json.dumps(views, ensure_ascii=False, separators=(",", ":")).encode()) <= 12_000

    asyncio.run(scenario())


def test_near_bound_views_keep_full_metrics_and_explicit_fallbacks() -> None:
    async def scenario() -> None:
        environment = SimulatorV2Environment(_ViewClient())  # type: ignore[arg-type]
        session = _session()
        metric_names = (
            "uptime_ratio",
            "downtime_seconds",
            "observed_seconds",
            "available_seconds",
            "total_cost_minor",
            "server_cost_minor",
            "backup_storage_cost_minor",
            "current_cost_per_hour_minor",
        )
        metric_data = {
            "clock": _clock(_CLOCK_0),
            "current": {name: "m" * 1_000 for name in metric_names},
        }
        objective_metrics = [
            ObjectiveMetric(name=name, value=float(index), unit="units")
            for index, name in enumerate(metric_names, start=1)
        ]
        environment._update_last_observed(
            session,
            ToolResult(
                action_kind="get_metrics",
                summary="large metrics",
                data=metric_data,
                objective_metrics=objective_metrics,
            ),
            GetMetrics(),
        )
        environment._update_last_observed(
            session,
            ToolResult(
                action_kind="get_resources",
                summary="large resources",
                data={
                    "clock": _clock(_CLOCK_0),
                    "active_instances": 2,
                    "total_capacity_units": 200,
                    "servers": [
                        {
                            "server_id": "server-" + "s" * 1_000,
                            "role": "backend",
                            "status": "active",
                            "instance_type": "small",
                        }
                    ],
                },
            ),
            GetResources(),
        )
        environment._update_last_observed(
            session,
            ToolResult(
                action_kind="get_overview",
                summary="large overview",
                data={
                    "clock": _clock(_CLOCK_0),
                    "status": "s" * 1_000,
                    "site_status": "s" * 1_000,
                    "availability": {"uptime_ratio": "a" * 1_000},
                    "costs": {"total_cost_minor": "c" * 1_000},
                },
            ),
            GetOverview(),
        )

        views = environment.public_state(session)["last_observed"]
        assert len(views) == 3  # type: ignore[arg-type]
        assert len(json.dumps(views, ensure_ascii=False, separators=(",", ":")).encode()) <= 12_000
        for view in views.values():  # type: ignore[union-attr]
            assert (
                len(json.dumps(view, ensure_ascii=False, separators=(",", ":")).encode()) <= 3_900
            )
        assert views["get_metrics"]["objective_metrics"] == [  # type: ignore[index]
            {"name": name, "value": float(index), "unit": "units"}
            for index, name in enumerate(metric_names, start=1)
        ]
        assert views["get_metrics"]["data"]["truncation"]["bounded_fallback"] is True  # type: ignore[index]

    asyncio.run(scenario())


def test_omitted_fields_are_counted_once_per_layer() -> None:
    async def scenario() -> None:
        environment = SimulatorV2Environment(_ViewClient())  # type: ignore[arg-type]
        session = _session()
        environment._update_last_observed(
            session,
            ToolResult(
                action_kind="get_metrics",
                summary="extra fields",
                data={
                    "clock": {**_clock(_CLOCK_0), "server_extra": True},
                    "current": {"uptime_ratio": 0.9, "current_extra": True},
                    "top_extra": True,
                },
            ),
            GetMetrics(),
        )
        payload = _view(environment, session, "get_metrics")["data"]
        assert payload["truncation"]["omitted_fields"] == 3  # type: ignore[index]

    asyncio.run(scenario())


def test_different_view_does_not_promote_metrics_to_fresh_and_invalid_clock_is_not_cached() -> None:
    async def scenario() -> None:
        client = _ViewClient()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        session = _session()
        await environment.execute(session, GetMetrics())
        await environment.execute(session, GetOverview())
        metrics = _view(environment, session, "get_metrics")
        assert metrics["freshness"] == "observed"
        assert metrics["stale"] is False

        async def invalid_metrics(run_id: str) -> dict[str, object]:
            return {
                "clock": {
                    "simulation_time": "2033-03-01T00:00:00",
                    "remaining_seconds": 3_600,
                },
                "current": {"uptime_ratio": 0.5},
            }

        client.metrics = invalid_metrics  # type: ignore[method-assign]
        await environment.execute(session, GetMetrics())
        assert set(environment.public_state(session)["last_observed"]) == {  # type: ignore[arg-type]
            "get_metrics",
            "get_overview",
        }
        assert _view(environment, session, "get_metrics")["observed_at"] == _CLOCK_0

    asyncio.run(scenario())
