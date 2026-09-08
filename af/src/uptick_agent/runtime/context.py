from __future__ import annotations

from typing import cast

from pydantic import Field

from uptick_agent.core.memory_models import MemoryPacket, MemoryQuery, MemoryView
from uptick_agent.core.models import (
    AgentConstraints,
    AgentContext,
    AgentWorkingState,
    CapabilityCatalog,
    ContextProgress,
    EnvironmentBrief,
    EnvironmentState,
    JsonObject,
    StrictModel,
)


class RunState(StrictModel):
    """Internal lifecycle state; never serialize this object directly into a prompt."""

    run_id: str = Field(min_length=1)
    step: int = Field(default=0, ge=0)
    step_limit: int | None = Field(default=None, ge=1)
    memory_view: MemoryView
    environment_state: EnvironmentState
    agent_working_state: AgentWorkingState = Field(default_factory=AgentWorkingState)


class ContextAssembler:
    """Build the exact bounded, model-facing projection from runtime values."""

    MEMORY_RECALL_LIMIT = 8

    def memory_query(
        self,
        *,
        objective: str,
        environment_state: EnvironmentState,
        capabilities: CapabilityCatalog,
        memory_view: MemoryView,
        memory_profile_version: str | None = None,
    ) -> MemoryQuery:
        latest_action = environment_state.latest_observation.action_kind
        relevant_capabilities = (
            [latest_action] if capabilities.find(latest_action) is not None else []
        )
        return MemoryQuery(
            objective=objective,
            environment_id=environment_state.profile.environment_id,
            environment_profile_version=memory_profile_version or environment_state.profile.version,
            view=memory_view,
            text=environment_state.latest_observation.summary,
            capability_names=relevant_capabilities,
            signals=cast(JsonObject, environment_state.decision_view.copy()),
            limit=self.MEMORY_RECALL_LIMIT,
        )

    def assemble(
        self,
        *,
        objective: str,
        environment_profile: EnvironmentBrief,
        run_state: RunState,
        capabilities: CapabilityCatalog,
        memory: MemoryPacket,
        constraints: AgentConstraints,
    ) -> AgentContext:
        working = run_state.agent_working_state.model_copy(deep=True)
        bridge = working.last_closed_episode
        if bridge is not None and any(
            record.record_id == bridge.episode_id for record in memory.records
        ):
            working = working.model_copy(update={"last_closed_episode": None}, deep=True)
        progress = (
            ContextProgress(step=run_state.step, step_limit=run_state.step_limit)
            if run_state.step_limit is not None
            else None
        )
        return AgentContext(
            objective=objective,
            environment_profile=environment_profile.model_copy(deep=True),
            environment_state=run_state.environment_state.model_copy(deep=True),
            agent_working_state=working,
            memory_brief=memory.brief.model_copy(deep=True),
            capabilities=capabilities.model_copy(deep=True),
            constraints=constraints.model_copy(deep=True),
            progress=progress,
        )
