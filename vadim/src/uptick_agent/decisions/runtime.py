"""Neutral context and result models consumed by the execution core."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from uptick_agent._model_base import StrictModel, preserve_legacy_identity
from uptick_agent.memory.contracts import DecisionMemoryContext, ObjectiveMetric, OperationLink


class ToolResult(StrictModel):
    action_kind: str
    ok: bool = True
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    objective_metrics: list[ObjectiveMetric] = Field(default_factory=list)
    operation_links: list[OperationLink] = Field(default_factory=list)
    terminal: bool = False


class RuntimeRecentStep(StrictModel):
    iteration: int = Field(ge=1)
    # This is intentionally opaque to the core.  The environment's response
    # schema supplies a validated Pydantic action instance.
    action: Any
    result_action_kind: str
    result_ok: bool
    result_summary: str
    result_terminal: bool


class RuntimeDecisionContext(StrictModel):
    objective: str
    run_id: str
    decision_id: str | None = None
    seed: int
    iteration: int
    max_steps: int
    latest_result: ToolResult
    memory_context: DecisionMemoryContext = Field(default_factory=DecisionMemoryContext)
    # Kept as opaque JSON for environments that still provide a legacy recall
    # view.  The generic runner does not interpret its contents.
    recalled_memories: list[Any] = Field(default_factory=list)
    recent_steps: list[RuntimeRecentStep] = Field(default_factory=list, max_length=6)
    # The environment owns this state; the runner only snapshots it for a
    # model request and never reduces it by inspecting action kinds.
    run_state: Any = Field(default_factory=dict)


preserve_legacy_identity(ToolResult)


__all__ = ["RuntimeDecisionContext", "RuntimeRecentStep", "ToolResult"]
