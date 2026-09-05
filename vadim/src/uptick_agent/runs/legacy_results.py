"""Historical SRE step record retained outside the generic execution core."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from uptick_agent._model_base import StrictModel, preserve_legacy_identity
from uptick_agent.decisions.contracts import NextStep, ToolResult


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


preserve_legacy_identity(StepRecord)

__all__ = ["StepRecord"]
