from __future__ import annotations

from datetime import datetime

from pydantic import Field

from uptick_agent.models import StrictModel


class Clock(StrictModel):
    simulation_time: datetime
    simulation_ends_at: datetime
    remaining_seconds: float = Field(ge=0)
    real_elapsed_seconds: float = Field(ge=0)
    applied_advance_seconds: float = Field(ge=0)


class StartRunResponse(StrictModel):
    run_id: str
    seed: int
    agent_id: str
    agent_version: str
    status: str
    simulation_time: datetime
    simulation_ends_at: datetime


class OverviewResponse(StrictModel):
    clock: Clock
    run_id: str
    status: str
    site_status: str
    current_deployment_id: str | None
    server_count: int
    capacity_utilization: float
    error_rate: float
    successful_purchases: int = 0
    revenue_minor: int = 0
    server_cost_minor: int = 0
    balance_minor: int = 0


class PageMetrics(StrictModel):
    page: str
    active_requests: int
    used_load_units: float
    responses_200: int
    responses_500: int
    error_rate: float


class MetricSnapshot(StrictModel):
    server_count: int
    capacity_units: float
    used_load_units: float
    capacity_utilization: float
    active_requests: int
    responses_200: int
    responses_500: int
    error_rate: float
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    successful_purchases: int
    revenue_minor: int
    lost_revenue_minor: int
    server_cost_minor: int
    by_page: list[PageMetrics]


class TimeWindow(StrictModel):
    from_: datetime = Field(alias="from")
    to: datetime


class MetricPoint(StrictModel):
    timestamp: datetime
    name: str
    page: str | None = None
    value: float


class MetricsResponse(StrictModel):
    clock: Clock
    window: TimeWindow | None = None
    current: MetricSnapshot
    series: list[MetricPoint]


class RequestLog(StrictModel):
    timestamp: datetime
    request_id: str
    source: str
    visitor_id: str | None = None
    page: str
    product_id: str | None = None
    status: int
    latency_ms: float = 0
    load_units: float
    server_id: str | None = None
    error: str | None = None
    message: str | None = None


class LogsResponse(StrictModel):
    clock: Clock
    logs: list[RequestLog]
    next_cursor: str | None


class ServerResource(StrictModel):
    server_id: str
    status: str
    capacity_units: float
    used_load_units: float
    cost_per_hour_minor: int


class ResourcesResponse(StrictModel):
    clock: Clock
    desired_instances: int
    active_instances: int
    total_capacity_units: float
    used_load_units: float
    total_cost_per_hour_minor: int
    servers: list[ServerResource]


class OperationAcceptedResponse(StrictModel):
    clock: Clock
    operation_id: str
    status: str
    estimated_complete_at: datetime | None = None


class ApplyFixResponse(StrictModel):
    clock: Clock
    applied: bool
    fixed_bug: str | None = None
    fixed_attack: str | None = None
    message: str = ""


class Deployment(StrictModel):
    deployment_id: str
    sequence: int
    name: str
    description: str = ""
    status: str
    operation_id: str | None = None


class DeploymentsResponse(StrictModel):
    clock: Clock
    deployments: list[Deployment]


class ErrorBody(StrictModel):
    error: str
    message: str
    details: dict = Field(default_factory=dict)


class OperationResponse(StrictModel):
    clock: Clock
    operation_id: str
    type: str
    status: str
    progress: float
    submitted_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: ErrorBody | None = None


class ProbeResponse(StrictModel):
    clock: Clock
    request_id: str
    page: str
    product_id: str | None = None
    status: int
    latency_ms: float
    load_units: float
    error: str | None = None
    message: str | None = None


class EconomyResponse(StrictModel):
    clock: Clock
    currency: str
    successful_purchases: int
    lost_purchases: int
    revenue_minor: int
    lost_revenue_minor: int
    server_cost_minor: int
    deployment_cost_minor: int
    balance_minor: int


class AdvanceTimeResponse(StrictModel):
    clock: Clock
    previous_simulation_time: datetime
    requested_duration_seconds: int
    processed_events: int
    new_logs: int
    logs_cursor: str | None = None
