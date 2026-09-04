from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentConfig(StrictModel):
    agent_id: str = Field(default="uptick-sgr", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    agent_version: str = Field(
        default="baseline-0.1", pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
    )
    max_steps: int = Field(default=160, ge=1)
    memory_recall_limit: int = Field(default=8, ge=0, le=100)
    objective: str = (
        "Keep the e-commerce site available and maximize final balance. "
        "Investigate failures, apply exact fixes, scale economically, and deploy carefully."
    )


class GetOverview(StrictModel):
    kind: Literal["get_overview"] = "get_overview"


class GetMetrics(StrictModel):
    kind: Literal["get_metrics"] = "get_metrics"


class GetLogs(StrictModel):
    kind: Literal["get_logs"] = "get_logs"
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
    | FinishRun,
    Field(discriminator="kind"),
]


class NextStep(StrictModel):
    """The SGR schema: state assessment, short plan, and exactly one action."""

    current_situation: str = Field(max_length=1000)
    hypothesis: str = Field(max_length=500)
    remaining_steps: list[str] = Field(min_length=0, max_length=5)
    task_completed: bool
    action: AgentAction

    @model_validator(mode="after")
    def completion_matches_action(self) -> NextStep:
        if self.task_completed != isinstance(self.action, FinishRun):
            raise ValueError("task_completed must be true exactly when action.kind is finish")
        return self


class ToolResult(StrictModel):
    action_kind: str
    ok: bool = True
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    terminal: bool = False


class MemoryEntry(StrictModel):
    id: str
    run_id: str | None = None
    kind: Literal["observation", "experience", "outcome", "lesson"]
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    importance: float = Field(default=0.5, ge=0, le=1)
    tags: set[str] = Field(default_factory=set)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryQuery(StrictModel):
    text: str = ""
    run_id: str | None = None
    include_other_runs: bool = True
    kinds: set[Literal["observation", "experience", "outcome", "lesson"]] | None = None
    tags: set[str] = Field(default_factory=set)
    limit: int = Field(default=10, ge=0, le=100)


class MemoryMatch(StrictModel):
    entry: MemoryEntry
    score: float


class RecentStep(StrictModel):
    iteration: int = Field(ge=1)
    action: AgentAction
    result_action_kind: str
    result_ok: bool
    result_summary: str
    result_terminal: bool


class RunState(StrictModel):
    applied_fix_messages: list[str] = Field(default_factory=list)
    started_deployment_ids: list[str] = Field(default_factory=list)
    operation_statuses: dict[str, str] = Field(default_factory=dict)
    desired_backend_instances: int | None = Field(default=None, ge=0, le=1000)


class DecisionContext(StrictModel):
    objective: str
    run_id: str
    seed: int
    iteration: int
    max_steps: int
    latest_result: ToolResult
    recalled_memories: list[MemoryMatch] = Field(default_factory=list)
    recent_steps: list[RecentStep] = Field(default_factory=list, max_length=6)
    run_state: RunState = Field(default_factory=RunState)


class StepRecord(StrictModel):
    run_id: str
    iteration: int
    decision: NextStep
    result: ToolResult
    started_at: datetime
    duration_seconds: float = Field(ge=0)


class RunResult(StrictModel):
    run_id: str
    seed: int
    agent_id: str
    agent_version: str
    status: str
    steps: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    successful_purchases: int = 0
    lost_purchases: int = 0
    revenue_minor: int = 0
    lost_revenue_minor: int = 0
    server_cost_minor: int = 0
    deployment_cost_minor: int = 0
    balance_minor: int = 0
    stop_reason: str


class ExperimentResult(StrictModel):
    name: str
    runs: list[RunResult]
    mean_balance_minor: float
    median_balance_minor: float
    min_balance_minor: int
    max_balance_minor: int
    completed_runs: int
