from __future__ import annotations

from pydantic import Field, model_validator

from uptick_agent._model_base import StrictModel, preserve_legacy_identity
from uptick_agent.memory.compatibility.contracts import MemoryMatch
from uptick_agent.memory.contracts import DecisionMemoryContext
from uptick_agent.simulator.actions import AgentAction, V1AgentAction, V2AgentAction

from .runtime import ToolResult


class NextStep(StrictModel):
    """The SGR schema: state assessment, short plan, and exactly one action."""

    current_situation: str = Field(max_length=1000)
    hypothesis: str = Field(max_length=500)
    remaining_steps: list[str] = Field(min_length=0, max_length=5)
    task_completed: bool
    action: AgentAction

    @model_validator(mode="after")
    def completion_matches_action(self) -> NextStep:
        if self.task_completed != (self.action.kind == "finish"):
            raise ValueError("task_completed must be true exactly when action.kind is finish")
        return self


class V1NextStep(NextStep):
    action: V1AgentAction


class V2NextStep(NextStep):
    action: V2AgentAction


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
    decision_id: str | None = None
    seed: int
    iteration: int
    max_steps: int
    latest_result: ToolResult
    memory_context: DecisionMemoryContext = Field(default_factory=DecisionMemoryContext)
    # Retained for callers that still construct the pre-Stage-3 context directly.
    recalled_memories: list[MemoryMatch] = Field(default_factory=list)
    recent_steps: list[RecentStep] = Field(default_factory=list, max_length=6)
    run_state: RunState = Field(default_factory=RunState)


preserve_legacy_identity(
    NextStep,
    V1NextStep,
    V2NextStep,
    ToolResult,
    RecentStep,
    RunState,
    DecisionContext,
)
