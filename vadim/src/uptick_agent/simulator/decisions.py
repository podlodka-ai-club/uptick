"""Canonical v2 decision schema owned by the simulator adapter."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, model_validator

from uptick_agent._model_base import StrictModel
from uptick_agent.simulator.actions import (
    ControlCommand,
    FinishRun,
    GetControlCommands,
    GetInbox,
    GetLogs,
    GetMetrics,
    GetOperation,
    GetOverview,
    GetResources,
    QueryLogs,
    QueryMetrics,
    V2AdvanceTime,
    V2ProbePage,
)

SimulatorV2Action = Annotated[
    GetOverview
    | GetMetrics
    | QueryMetrics
    | GetLogs
    | QueryLogs
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


class SimulatorV2Decision(StrictModel):
    """The current simulator response envelope and its complete typed tool set."""

    current_situation: str = Field(max_length=1000)
    hypothesis: str = Field(max_length=500)
    remaining_steps: list[str] = Field(min_length=0, max_length=5)
    task_completed: bool
    action: SimulatorV2Action

    @model_validator(mode="after")
    def completion_matches_action(self) -> SimulatorV2Decision:
        if self.task_completed != (self.action.kind == "finish"):
            raise ValueError("task_completed must be true exactly when action.kind is finish")
        return self


__all__ = ["SimulatorV2Action", "SimulatorV2Decision"]
