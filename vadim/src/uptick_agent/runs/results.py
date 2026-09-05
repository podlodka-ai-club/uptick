from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from uptick_agent._model_base import StrictModel, preserve_legacy_identity
from uptick_agent.decisions.contracts import NextStep, ToolResult
from uptick_agent.memory.contracts import ObjectiveMetric


class StepRecord(StrictModel):
    run_id: str
    decision_id: str
    transition_id: str
    iteration: int
    decision: NextStep
    result: ToolResult
    memory_diagnostics: dict[str, Any] = Field(default_factory=dict)
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
    objective_kind: Literal["balance", "uptime_cost"] = "balance"
    uptime_ratio: float | None = Field(default=None, ge=0, le=1)
    slo_passed: bool | None = None
    total_cost_minor: int | None = Field(default=None, ge=0)
    objective_metrics: list[ObjectiveMetric] = Field(default_factory=list)
    stop_reason: str


class ExperimentResult(StrictModel):
    name: str
    runs: list[RunResult]
    objective_kind: Literal["balance", "uptime_cost"] = "balance"
    mean_balance_minor: float | None = None
    median_balance_minor: float | None = None
    min_balance_minor: int | None = None
    max_balance_minor: int | None = None
    completed_runs: int = 0
    slo_passed_runs: int = 0
    mean_successful_total_cost_minor: float | None = None


preserve_legacy_identity(StepRecord, RunResult, ExperimentResult)
