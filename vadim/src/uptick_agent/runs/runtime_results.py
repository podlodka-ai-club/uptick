"""Opaque result records for the generic execution core."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, SerializeAsAny

from uptick_agent._model_base import StrictModel
from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.memory.contracts import ObjectiveMetric


class RuntimeRunResult(StrictModel):
    """Environment-neutral outcome returned by the generic runner."""

    run_id: str
    seed: int
    agent_id: str
    agent_version: str
    status: str
    steps: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    objective_metrics: list[ObjectiveMetric] = Field(default_factory=list)
    stop_reason: str


class RuntimeStepRecord(StrictModel):
    run_id: str
    decision_id: str
    transition_id: str
    iteration: int
    decision: SerializeAsAny[BaseModel]
    result: ToolResult
    memory_diagnostics: dict[str, object] = Field(default_factory=dict)
    started_at: datetime
    duration_seconds: float = Field(ge=0)


__all__ = ["RuntimeRunResult", "RuntimeStepRecord"]
