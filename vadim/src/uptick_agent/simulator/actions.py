"""Typed action surface owned by the simulator adapter."""

from __future__ import annotations

from datetime import datetime
from ipaddress import IPv4Address, IPv6Address, ip_network
from typing import Annotated, Literal

from pydantic import Field, model_validator

from uptick_agent._model_base import StrictModel, preserve_legacy_identity
from uptick_agent.v2_actions import ControlCommand, GetControlCommands, GetInbox


class GetOverview(StrictModel):
    kind: Literal["get_overview"] = "get_overview"


class GetMetrics(StrictModel):
    kind: Literal["get_metrics"] = "get_metrics"


class GetLogs(StrictModel):
    kind: Literal["get_logs"] = "get_logs"
    status: Literal[200, 403, 500, 503] | None = Field(
        default=500,
        description="Filter by HTTP status. Null returns all supported statuses.",
    )


PageType = Literal["product_list", "product_page"]
PageRequestStatus = Literal[200, 403, 500, 503]
RequestFailureCode = Literal[
    "SERVER_CAPACITY_EXCEEDED",
    "DB_CONNECTION_LIMIT_EXCEEDED",
    "DISK_FULL",
    "SITE_UNAVAILABLE",
    "DB_UNAVAILABLE",
    "FIREWALL_DENIED",
]
MetricName = Literal[
    "server_count",
    "capacity_units",
    "used_load_units",
    "capacity_utilization",
    "active_requests",
    "database_active_connections",
    "database_connection_limit",
    "disk_total_bytes",
    "disk_system_bytes",
    "disk_database_bytes",
    "disk_logs_bytes",
    "disk_free_bytes",
    "requests_total",
    "responses_200",
    "responses_500",
    "responses_403",
    "responses_503",
    "error_rate",
    "latency_p50_ms",
    "latency_p95_ms",
    "server_cost_minor",
    "backup_storage_cost_minor",
    "total_cost_minor",
    "current_cost_per_hour_minor",
    "observed_seconds",
    "available_seconds",
    "downtime_seconds",
    "uptime_ratio",
]
IPAddress = IPv4Address | IPv6Address


class QueryLogs(StrictModel):
    """Read an explicit log window without touching incremental cursors."""

    kind: Literal["query_logs"] = "query_logs"
    from_time: datetime | None = Field(default=None, alias="from")
    to_time: datetime | None = Field(default=None, alias="to")
    page: PageType | None = None
    status: PageRequestStatus | None = None
    has_error: bool | None = None
    error: RequestFailureCode | None = None
    source_ip: IPAddress | None = None
    source_cidr: str | None = Field(
        default=None,
        min_length=3,
        max_length=49,
        pattern=r"^[0-9A-Fa-f:.]+/[0-9]{1,3}$",
    )
    user_agent: str | None = Field(default=None, min_length=1, max_length=2048)
    region_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    firewall_rule_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=100, ge=1, le=1000)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_window_and_filters(self) -> QueryLogs:
        _validate_time_window(self.from_time, self.to_time, require_pair=False)
        if self.has_error is False and self.error is not None:
            raise ValueError("error is incompatible with has_error=false")
        if self.source_cidr is not None:
            try:
                ip_network(self.source_cidr, strict=True)
            except ValueError:
                raise ValueError("source_cidr must be a canonical IPv4 or IPv6 network") from None
        return self


class QueryMetrics(StrictModel):
    """Read an explicit metric window without changing snapshot state."""

    kind: Literal["query_metrics"] = "query_metrics"
    from_time: datetime | None = Field(default=None, alias="from")
    to_time: datetime | None = Field(default=None, alias="to")
    step_seconds: int = Field(default=60, ge=1)
    names: list[MetricName] | None = Field(default=None, min_length=1, max_length=32)
    page: PageType | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_window_and_names(self) -> QueryMetrics:
        _validate_time_window(self.from_time, self.to_time, require_pair=True)
        if self.names is not None and len(set(self.names)) != len(self.names):
            raise ValueError("names must be unique")
        return self


def _validate_time_window(
    from_time: datetime | None, to_time: datetime | None, *, require_pair: bool
) -> None:
    if require_pair and (from_time is None) != (to_time is None):
        raise ValueError("from and to must be supplied together")
    for bound in (from_time, to_time):
        if bound is not None and (bound.tzinfo is None or bound.utcoffset() is None):
            raise ValueError("from and to must be timezone-aware")
    if from_time is None or to_time is None:
        return
    if from_time > to_time:
        raise ValueError("from must not be later than to")


class V1GetLogs(GetLogs):
    status: Literal[200, 500] | None = Field(
        default=500,
        description="Filter by HTTP status. Null returns both successful and failed requests.",
    )


class GetResources(StrictModel):
    kind: Literal["get_resources"] = "get_resources"


class GetDeployments(StrictModel):
    kind: Literal["get_deployments"] = "get_deployments"


class ScaleBackend(StrictModel):
    kind: Literal["scale_backend"] = "scale_backend"
    desired_instances: int = Field(ge=0, le=1000)


class ApplyFix(StrictModel):
    kind: Literal["apply_fix"] = "apply_fix"
    message: str = Field(min_length=1, max_length=4096)


class StartDeployment(StrictModel):
    kind: Literal["start_deployment"] = "start_deployment"
    deployment_id: str = Field(min_length=1, max_length=128)


class GetOperation(StrictModel):
    kind: Literal["get_operation"] = "get_operation"
    operation_id: str = Field(min_length=1, max_length=128)


class ProbePage(StrictModel):
    kind: Literal["probe_page"] = "probe_page"
    page: Literal["product_list", "product_page", "purchase"]
    product_id: str | None = None

    @model_validator(mode="after")
    def product_matches_page(self) -> ProbePage:
        needs_product = self.page != "product_list"
        if needs_product != (self.product_id is not None):
            raise ValueError("product_id is required except for product_list")
        return self


class AdvanceTime(StrictModel):
    kind: Literal["advance_time"] = "advance_time"
    duration_seconds: int = Field(default=86_400, ge=300)


class AdvanceTimeStopCondition(StrictModel):
    new_log_errors: Literal[1] = 1
    error_codes: (
        list[
            Literal[
                "SERVER_CAPACITY_EXCEEDED",
                "DB_CONNECTION_LIMIT_EXCEEDED",
                "DISK_FULL",
                "SITE_UNAVAILABLE",
                "DB_UNAVAILABLE",
                "FIREWALL_DENIED",
            ]
        ]
        | None
    ) = Field(default=None, min_length=1, max_length=6)

    @model_validator(mode="after")
    def unique_error_codes(self) -> AdvanceTimeStopCondition:
        if self.error_codes is not None and len(set(self.error_codes)) != len(self.error_codes):
            raise ValueError("error_codes must be unique")
        return self


class V2ProbePage(ProbePage):
    page: Literal["product_list", "product_page"]

    @model_validator(mode="after")
    def product_matches_page(self) -> V2ProbePage:
        if (self.page == "product_page") != (self.product_id is not None):
            raise ValueError("product_id is required exactly for product_page")
        return self


class V2AdvanceTime(StrictModel):
    kind: Literal["advance_time_v2"] = "advance_time_v2"
    duration_seconds: int = Field(default=86_400, ge=300)
    stop_when: AdvanceTimeStopCondition | None = Field(default_factory=AdvanceTimeStopCondition)


class FinishRun(StrictModel):
    kind: Literal["finish"] = "finish"
    reason: str = Field(min_length=1, max_length=1000)


AgentAction = Annotated[
    GetOverview
    | GetMetrics
    | GetLogs
    | GetResources
    | GetDeployments
    | ScaleBackend
    | ApplyFix
    | StartDeployment
    | GetOperation
    | ProbePage
    | AdvanceTime
    | V2AdvanceTime
    | FinishRun
    | GetInbox
    | GetControlCommands
    | ControlCommand,
    Field(discriminator="kind"),
]


V1AgentAction = Annotated[
    GetOverview
    | GetMetrics
    | V1GetLogs
    | GetResources
    | GetDeployments
    | ScaleBackend
    | ApplyFix
    | StartDeployment
    | GetOperation
    | ProbePage
    | AdvanceTime
    | FinishRun,
    Field(discriminator="kind"),
]


V2AgentAction = Annotated[
    GetOverview
    | GetMetrics
    | GetLogs
    | GetResources
    | GetOperation
    | V2ProbePage
    | V2AdvanceTime
    | FinishRun
    | GetInbox
    | GetControlCommands
    | ControlCommand,
    Field(discriminator="kind"),
]


preserve_legacy_identity(
    GetOverview,
    GetMetrics,
    GetLogs,
    V1GetLogs,
    GetResources,
    GetDeployments,
    ScaleBackend,
    ApplyFix,
    StartDeployment,
    GetOperation,
    ProbePage,
    AdvanceTime,
    AdvanceTimeStopCondition,
    V2ProbePage,
    V2AdvanceTime,
    FinishRun,
)
