from __future__ import annotations

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
