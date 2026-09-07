from __future__ import annotations

from pydantic import Field

from uptick_agent._model_base import StrictModel, preserve_legacy_identity


class AgentConfig(StrictModel):
    agent_id: str = Field(default="uptick-sgr", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    agent_version: str = Field(
        default="baseline-0.1", pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
    )
    max_steps: int = Field(default=160, ge=1)
    # ``None`` preserves the legacy one-action-per-decision behavior.  A
    # positive value explicitly bounds total environment actions for a run;
    # the decision envelope has its own hard four-action cap.
    max_actions: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )
    memory_recall_limit: int = Field(default=8, ge=0, le=100)
    objective: str = (
        "Keep the e-commerce site available and maximize final balance. "
        "Investigate failures, apply exact fixes, scale economically, and deploy carefully."
    )


preserve_legacy_identity(AgentConfig)
