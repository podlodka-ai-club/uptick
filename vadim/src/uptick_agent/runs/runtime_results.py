"""Opaque result records for the generic execution core."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, JsonValue, SerializeAsAny

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
    # Populated only when the caller opts into action-budget accounting.
    action_count: int | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)


class RuntimeStepRecord(StrictModel):
    run_id: str
    decision_id: str
    # Local memory reads have an audit event, but no world experience transition.
    transition_id: str | None
    iteration: int
    decision: SerializeAsAny[BaseModel]
    result: ToolResult
    memory_diagnostics: dict[str, object] = Field(default_factory=dict)
    started_at: datetime
    duration_seconds: float = Field(ge=0)
    # Present for batch records so consumers can join one result to its
    # position and exact environment-owned action without parsing the whole
    # decision envelope.  Excluded when absent to preserve legacy output.
    action_index: int | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)
    action: dict[str, JsonValue] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


__all__ = ["RuntimeRunResult", "RuntimeStepRecord"]
