from __future__ import annotations

# ruff: noqa: F822
from typing import Literal

from pydantic import Field

from uptick_agent._model_base import StrictModel, preserve_legacy_identity
from uptick_agent.runs.runtime_results import RuntimeRunResult


class RunResult(RuntimeRunResult):
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


preserve_legacy_identity(RunResult, ExperimentResult)


def __getattr__(name: str):
    if name != "StepRecord":
        raise AttributeError(name)
    from uptick_agent.runs.legacy_results import StepRecord

    globals()[name] = StepRecord
    return StepRecord


__all__ = ["StepRecord", "RunResult", "ExperimentResult"]
