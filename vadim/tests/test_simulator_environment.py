import asyncio
from datetime import UTC, datetime, timedelta

from uptick_agent.models import GetLogs
from uptick_agent.simulator.environment import SimulatorEnvironment, SimulatorSession, _result
from uptick_agent.simulator.models import (
    Clock,
    Deployment,
    DeploymentsResponse,
    EconomyResponse,
    LogsResponse,
    MetricSnapshot,
    MetricsResponse,
    OperationAcceptedResponse,
    OperationResponse,
    OverviewResponse,
    RequestLog,
)


class FakePagedLogsClient:
    def __init__(self) -> None:
        self.cursors = []
        self.calls = 0

    async def logs(self, run_id, *, from_time, status, cursor, limit=200):
        self.cursors.append(cursor)
        self.calls += 1
        now = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(minutes=self.calls)
        next_cursor = f"cursor-{self.calls}" if self.calls < 6 else None
        return LogsResponse(
            clock=Clock(
                simulation_time=now,
                simulation_ends_at=datetime(2026, 9, 1, tzinfo=UTC),
                remaining_seconds=100,
                real_elapsed_seconds=0,
                applied_advance_seconds=0,
            ),
            logs=[
                RequestLog(
                    timestamp=now,
                    request_id=f"request-{self.calls}",
                    source="visitor",
                    page="product_list",
                    status=500,
                    load_units=1,
                    error="PAGE_BUG",
                )
            ],
            next_cursor=next_cursor,
        )


def test_log_pagination_continues_without_losing_entries() -> None:
    async def scenario() -> None:
        client = FakePagedLogsClient()
        environment = SimulatorEnvironment(client)  # type: ignore[arg-type]
        start = datetime(2026, 8, 1, tzinfo=UTC)
        session = SimulatorSession(
            run_id="run",
            seed=1,
            agent_id="agent",
            agent_version="v1",
            status="running",
            simulation_time=start,
            logs_from=start,
        )

        first = await environment.execute(session, GetLogs(status=500))
        assert first.data["truncated"] is True
        assert first.data["total_logs"] == 5
        assert first.data["logs"][0]["occurrences"] == 5
        assert session.logs_from == start
        assert session.logs_cursor == "cursor-5"

        second = await environment.execute(session, GetLogs(status=500))
        assert second.data["truncated"] is False
        assert session.logs_cursor is None
        assert session.logs_from == datetime(2026, 8, 1, 0, 6, tzinfo=UTC)
        assert client.cursors == [None, "cursor-1", "cursor-2", "cursor-3", "cursor-4", "cursor-5"]

    asyncio.run(scenario())


def _clock() -> Clock:
    now = datetime(2026, 9, 4, tzinfo=UTC)
    return Clock(
        simulation_time=now,
        simulation_ends_at=now + timedelta(days=1),
        remaining_seconds=86_400,
        real_elapsed_seconds=1,
        applied_advance_seconds=0,
    )


def test_adapter_maps_generic_objective_metrics_and_operation_links() -> None:
    overview = OverviewResponse(
        clock=_clock(),
        run_id="run",
        status="running",
        site_status="healthy",
        current_deployment_id="deployment-1",
        server_count=2,
        capacity_utilization=0.5,
        error_rate=0.1,
        successful_purchases=7,
        revenue_minor=100,
        server_cost_minor=20,
        balance_minor=80,
    )
    overview_result = _result("get_overview", overview, "overview")
    assert [
        (metric.name, metric.value, metric.unit) for metric in overview_result.objective_metrics
    ] == [
        ("successful_purchases", 7, "count"),
        ("revenue_minor", 100, "minor"),
        ("server_cost_minor", 20, "minor"),
        ("balance_minor", 80, "minor"),
    ]

    metrics = MetricsResponse(
        clock=_clock(),
        current=MetricSnapshot(
            server_count=2,
            capacity_units=20,
            used_load_units=10,
            capacity_utilization=0.5,
            active_requests=3,
            responses_200=8,
            responses_500=1,
            error_rate=1 / 9,
            successful_purchases=8,
            revenue_minor=120,
            lost_revenue_minor=15,
            server_cost_minor=25,
            by_page=[],
        ),
        series=[],
    )
    metrics_result = _result("get_metrics", metrics, "metrics")
    assert [metric.name for metric in metrics_result.objective_metrics] == [
        "successful_purchases",
        "revenue_minor",
        "lost_revenue_minor",
        "server_cost_minor",
    ]

    accepted = OperationAcceptedResponse(
        clock=_clock(), operation_id="operation-1", status="accepted"
    )
    assert _result("scale_backend", accepted, "accepted").operation_links[0].model_dump(
        exclude={"schema_version"}
    ) == {"operation_id": "operation-1", "relation": "initiated"}

    observed = OperationResponse(
        clock=_clock(),
        operation_id="operation-1",
        type="scale",
        status="completed",
        progress=1,
        submitted_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    assert _result("get_operation", observed, "observed").operation_links[0].relation == (
        "observed"
    )

    deployments = DeploymentsResponse(
        clock=_clock(),
        deployments=[
            Deployment(
                deployment_id="deployment-1",
                sequence=1,
                name="current",
                status="current",
                operation_id="operation-2",
            ),
            Deployment(
                deployment_id="deployment-2",
                sequence=2,
                name="available",
                status="available",
            ),
        ],
    )
    deployment_links = _result("get_deployments", deployments, "deployments").operation_links
    assert [(link.operation_id, link.relation) for link in deployment_links] == [
        ("operation-2", "observed")
    ]


def test_final_economy_is_exposed_as_generic_objective_metrics() -> None:
    session = SimulatorSession(
        run_id="run",
        seed=7,
        agent_id="agent",
        agent_version="v1",
        status="completed",
        simulation_time=datetime(2026, 9, 4, tzinfo=UTC),
        logs_from=datetime(2026, 9, 4, tzinfo=UTC),
    )
    economy = EconomyResponse(
        clock=_clock(),
        currency="minor",
        successful_purchases=10,
        lost_purchases=2,
        revenue_minor=150,
        lost_revenue_minor=30,
        server_cost_minor=25,
        deployment_cost_minor=5,
        balance_minor=120,
    )

    result = SimulatorEnvironment._run_result(
        session,
        economy,
        "completed",
        3,
        1.5,
        "done",
    )

    assert [(metric.name, metric.value, metric.unit) for metric in result.objective_metrics] == [
        ("successful_purchases", 10, "count"),
        ("lost_purchases", 2, "count"),
        ("revenue_minor", 150, "minor"),
        ("lost_revenue_minor", 30, "minor"),
        ("server_cost_minor", 25, "minor"),
        ("deployment_cost_minor", 5, "minor"),
        ("balance_minor", 120, "minor"),
    ]
