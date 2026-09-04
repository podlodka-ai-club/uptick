from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from uptick_agent.memory.contracts import ObjectiveMetric, OperationLink
from uptick_agent.models import (
    AdvanceTime,
    AgentAction,
    ApplyFix,
    FinishRun,
    GetDeployments,
    GetLogs,
    GetMetrics,
    GetOperation,
    GetOverview,
    GetResources,
    ProbePage,
    RunResult,
    ScaleBackend,
    StartDeployment,
    ToolResult,
)
from uptick_agent.simulator.client import SimulatorApiError, SimulatorClient
from uptick_agent.simulator.models import (
    DeploymentsResponse,
    EconomyResponse,
    MetricsResponse,
    OperationAcceptedResponse,
    OperationResponse,
    OverviewResponse,
)


@dataclass(slots=True)
class SimulatorSession:
    run_id: str
    seed: int
    agent_id: str
    agent_version: str
    status: str
    simulation_time: datetime
    logs_from: datetime
    request_prefix: str = field(default_factory=lambda: uuid4().hex[:12])
    request_number: int = 0
    seen_log_ids: set[str] = field(default_factory=set)
    logs_cursor: str | None = None
    logs_cursor_status: int | None = None

    def next_request_id(self, kind: str) -> str:
        self.request_number += 1
        return f"uptick-{self.request_prefix}-{kind}-{self.request_number:05d}"


def _objective_metrics(value) -> list[ObjectiveMetric]:
    if isinstance(value, OverviewResponse):
        snapshot = value
        fields = (
            ("successful_purchases", "count"),
            ("revenue_minor", "minor"),
            ("server_cost_minor", "minor"),
            ("balance_minor", "minor"),
        )
    elif isinstance(value, MetricsResponse):
        snapshot = value.current
        fields = (
            ("successful_purchases", "count"),
            ("revenue_minor", "minor"),
            ("lost_revenue_minor", "minor"),
            ("server_cost_minor", "minor"),
        )
    else:
        return []
    return [
        ObjectiveMetric(name=name, value=getattr(snapshot, name), unit=unit)
        for name, unit in fields
    ]


def _operation_links(value) -> list[OperationLink]:
    if isinstance(value, OperationAcceptedResponse):
        return [OperationLink(operation_id=value.operation_id, relation="initiated")]
    if isinstance(value, OperationResponse):
        return [OperationLink(operation_id=value.operation_id, relation="observed")]
    if isinstance(value, DeploymentsResponse):
        return [
            OperationLink(operation_id=item.operation_id, relation="observed")
            for item in value.deployments
            if item.operation_id is not None
        ]
    return []


def _result(action_kind: str, value, summary: str, *, terminal: bool = False) -> ToolResult:
    return ToolResult(
        action_kind=action_kind,
        summary=summary,
        data=value.model_dump(mode="json") if value is not None else {},
        objective_metrics=_objective_metrics(value),
        operation_links=_operation_links(value),
        terminal=terminal,
    )


class SimulatorEnvironment:
    def __init__(self, client: SimulatorClient) -> None:
        self.client = client

    async def start(
        self, *, seed: int, agent_id: str, agent_version: str
    ) -> tuple[SimulatorSession, ToolResult]:
        prefix = uuid4().hex[:12]
        started = await self.client.start(
            seed=seed,
            agent_id=agent_id,
            agent_version=agent_version,
            request_id=f"uptick-{prefix}-start",
        )
        session = SimulatorSession(
            run_id=started.run_id,
            seed=seed,
            agent_id=agent_id,
            agent_version=agent_version,
            status=started.status,
            simulation_time=started.simulation_time,
            logs_from=started.simulation_time,
            request_prefix=prefix,
        )
        return session, _result(
            "start",
            started,
            f"Run {started.run_id} started at {started.simulation_time.isoformat()}",
        )

    async def execute(self, session: SimulatorSession, action: AgentAction) -> ToolResult:
        try:
            return await self._execute(session, action)
        except SimulatorApiError as error:
            if error.code == "RUN_COMPLETED":
                session.status = "completed"
                return ToolResult(
                    action_kind=action.kind,
                    ok=True,
                    terminal=True,
                    summary="The simulator reports that the run is completed.",
                    data={"code": error.code, "message": error.message},
                )
            return ToolResult(
                action_kind=action.kind,
                ok=False,
                summary=str(error),
                data={
                    "status_code": error.status_code,
                    "code": error.code,
                    "message": error.message,
                },
            )

    async def _execute(self, session: SimulatorSession, action: AgentAction) -> ToolResult:
        if isinstance(action, FinishRun):
            return ToolResult(
                action_kind=action.kind,
                summary=action.reason,
                terminal=True,
            )
        if isinstance(action, GetOverview):
            value = await self.client.overview(session.run_id)
            session.status = value.status
            session.simulation_time = value.clock.simulation_time
            return _result(
                action.kind,
                value,
                f"Site is {value.site_status}; balance={value.balance_minor}; "
                f"error_rate={value.error_rate:.3f}; servers={value.server_count}.",
                terminal=value.status != "running",
            )
        if isinstance(action, GetMetrics):
            value = await self.client.metrics(session.run_id)
            session.simulation_time = value.clock.simulation_time
            current = value.current
            return _result(
                action.kind,
                value,
                f"utilization={current.capacity_utilization:.3f}; "
                f"error_rate={current.error_rate:.3f}; "
                f"lost_revenue={current.lost_revenue_minor}.",
                terminal=value.clock.remaining_seconds <= 0,
            )
        if isinstance(action, GetLogs):
            return await self._get_logs(session, action)
        if isinstance(action, GetResources):
            value = await self.client.resources(session.run_id)
            session.simulation_time = value.clock.simulation_time
            return _result(
                action.kind,
                value,
                f"desired={value.desired_instances}; active={value.active_instances}; "
                f"used={value.used_load_units}/{value.total_capacity_units}; "
                f"hourly_cost={value.total_cost_per_hour_minor}.",
                terminal=value.clock.remaining_seconds <= 0,
            )
        if isinstance(action, GetDeployments):
            value = await self.client.deployments(session.run_id)
            session.simulation_time = value.clock.simulation_time
            available = [
                item.deployment_id for item in value.deployments if item.status == "available"
            ]
            return _result(
                action.kind,
                value,
                f"Available deployments: {available or 'none'}.",
                terminal=value.clock.remaining_seconds <= 0,
            )
        if isinstance(action, ScaleBackend):
            value = await self.client.scale(
                session.run_id,
                request_id=session.next_request_id("scale"),
                desired_instances=action.desired_instances,
            )
            session.simulation_time = value.clock.simulation_time
            return _result(
                action.kind,
                value,
                f"Scaling to {action.desired_instances} accepted as "
                f"operation {value.operation_id}.",
                terminal=value.clock.remaining_seconds <= 0,
            )
        if isinstance(action, ApplyFix):
            value = await self.client.apply_fix(
                session.run_id,
                request_id=session.next_request_id("fix"),
                message=action.message,
            )
            session.simulation_time = value.clock.simulation_time
            return _result(
                action.kind,
                value,
                f"Fix {'applied' if value.applied else 'rejected'}: {value.message}",
                terminal=value.clock.remaining_seconds <= 0,
            )
        if isinstance(action, StartDeployment):
            value = await self.client.start_deployment(
                session.run_id,
                request_id=session.next_request_id("deploy"),
                deployment_id=action.deployment_id,
            )
            session.simulation_time = value.clock.simulation_time
            return _result(
                action.kind,
                value,
                f"Deployment {action.deployment_id} accepted as operation {value.operation_id}.",
                terminal=value.clock.remaining_seconds <= 0,
            )
        if isinstance(action, GetOperation):
            value = await self.client.operation(session.run_id, action.operation_id)
            session.simulation_time = value.clock.simulation_time
            return _result(
                action.kind,
                value,
                f"Operation {value.operation_id} is {value.status} ({value.progress:.0%}).",
                terminal=value.clock.remaining_seconds <= 0,
            )
        if isinstance(action, ProbePage):
            value = await self.client.probe(
                session.run_id,
                request_id=session.next_request_id("probe"),
                page=action.page,
                product_id=action.product_id,
            )
            session.simulation_time = value.clock.simulation_time
            return _result(
                action.kind,
                value,
                f"Probe {action.page} returned HTTP {value.status}; error={value.error}.",
                terminal=value.clock.remaining_seconds <= 0,
            )
        if isinstance(action, AdvanceTime):
            value = await self.client.advance(
                session.run_id,
                request_id=session.next_request_id("advance"),
                duration_seconds=action.duration_seconds,
            )
            session.simulation_time = value.clock.simulation_time
            terminal = value.clock.remaining_seconds <= 0
            if terminal:
                session.status = "completed"
            return _result(
                action.kind,
                value,
                f"Advanced {action.duration_seconds}s; processed={value.processed_events}; "
                f"new_logs={value.new_logs}.",
                terminal=terminal,
            )
        raise TypeError(f"unsupported action: {type(action).__name__}")

    async def _get_logs(self, session: SimulatorSession, action: GetLogs) -> ToolResult:
        cursor = session.logs_cursor if session.logs_cursor_status == action.status else None
        collected = []
        latest_clock = None
        for _ in range(5):
            page = await self.client.logs(
                session.run_id,
                from_time=session.logs_from.isoformat(),
                status=action.status,
                cursor=cursor,
            )
            latest_clock = page.clock
            collected.extend(
                item for item in page.logs if item.request_id not in session.seen_log_ids
            )
            session.seen_log_ids.update(item.request_id for item in page.logs)
            cursor = page.next_cursor
            if cursor is None:
                break
        if latest_clock is not None:
            session.simulation_time = latest_clock.simulation_time
            if cursor is None:
                session.logs_from = latest_clock.simulation_time
        session.logs_cursor = cursor
        session.logs_cursor_status = action.status if cursor is not None else None
        error_counts: dict[str, int] = {}
        grouped_logs: dict[tuple, dict] = {}
        for item in collected:
            if item.error:
                error_counts[item.error] = error_counts.get(item.error, 0) + 1
            group_key = (item.status, item.error, item.message, item.page)
            if group_key not in grouped_logs:
                grouped_logs[group_key] = item.model_dump(mode="json") | {"occurrences": 0}
            grouped_logs[group_key]["occurrences"] += 1
        data = {
            "clock": latest_clock.model_dump(mode="json") if latest_clock else None,
            "total_logs": len(collected),
            "logs": list(grouped_logs.values()),
            "truncated": cursor is not None,
        }
        terminal = bool(latest_clock and latest_clock.remaining_seconds <= 0)
        return ToolResult(
            action_kind=action.kind,
            summary=f"Read {len(collected)} new logs; errors={error_counts or 'none'}.",
            data=data,
            terminal=terminal,
        )

    async def finish(
        self,
        session: SimulatorSession,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ) -> RunResult:
        economy = await self.client.economy(session.run_id)
        status = session.status
        try:
            overview = await self.client.overview(session.run_id)
            status = overview.status
        except SimulatorApiError:
            pass
        return self._run_result(session, economy, status, steps, duration_seconds, stop_reason)

    @staticmethod
    def _run_result(
        session: SimulatorSession,
        economy: EconomyResponse,
        status: str,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ) -> RunResult:
        return RunResult(
            run_id=session.run_id,
            seed=session.seed,
            agent_id=session.agent_id,
            agent_version=session.agent_version,
            status=status,
            steps=steps,
            duration_seconds=duration_seconds,
            successful_purchases=economy.successful_purchases,
            lost_purchases=economy.lost_purchases,
            revenue_minor=economy.revenue_minor,
            lost_revenue_minor=economy.lost_revenue_minor,
            server_cost_minor=economy.server_cost_minor,
            deployment_cost_minor=economy.deployment_cost_minor,
            balance_minor=economy.balance_minor,
            objective_metrics=[
                ObjectiveMetric(
                    name="successful_purchases", value=economy.successful_purchases, unit="count"
                ),
                ObjectiveMetric(name="lost_purchases", value=economy.lost_purchases, unit="count"),
                ObjectiveMetric(name="revenue_minor", value=economy.revenue_minor, unit="minor"),
                ObjectiveMetric(
                    name="lost_revenue_minor", value=economy.lost_revenue_minor, unit="minor"
                ),
                ObjectiveMetric(
                    name="server_cost_minor", value=economy.server_cost_minor, unit="minor"
                ),
                ObjectiveMetric(
                    name="deployment_cost_minor",
                    value=economy.deployment_cost_minor,
                    unit="minor",
                ),
                ObjectiveMetric(name="balance_minor", value=economy.balance_minor, unit="minor"),
            ],
            stop_reason=stop_reason,
        )
