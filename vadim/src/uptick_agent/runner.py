"""Legacy runner facade.

The generic execution implementation lives in :mod:`uptick_agent.runs.execute`.
This module keeps the historical SRE application port usable by adapting old
environments that have not published a decision specification yet.
"""

from __future__ import annotations

import inspect

from pydantic import BaseModel

from uptick_agent.decisions.contracts import NextStep, RunState
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.runs.execute import (
    _DEFAULT_RUNTIME_OBJECTIVE,
    _memory_text,
    _prompt_trace,
)
from uptick_agent.runs.execute import (
    AgentRunner as _CanonicalAgentRunner,
)
from uptick_agent.simulator.legacy_state import record_run_state


class _LegacyEnvironmentAdapter:
    """Bridge pre-spec environments without putting SRE logic in core."""

    def __init__(self, environment: object, model: object, *, objective: str) -> None:
        self._environment = environment
        response_model = getattr(model, "response_model", NextStep)
        if not isinstance(response_model, type) or not issubclass(response_model, BaseModel):
            response_model = NextStep
        self.decision_spec = EnvironmentDecisionSpec(
            response_model=response_model,
            objective=objective,
        )
        self._run_state = RunState()

    async def start(self, **kwargs):
        self._run_state = RunState()
        return await self._environment.start(**kwargs)

    async def execute(self, session, action: BaseModel):
        result = await self._environment.execute(session, action)
        record_run_state(self._run_state, action, result)
        return result

    def public_state(self, session) -> RunState:
        return self._run_state

    async def finish(self, session, **kwargs):
        return await self._environment.finish(session, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._environment, name)


class _LegacyModelAdapter:
    """Give historical models their typed state view at the compatibility edge."""

    def __init__(self, model: object) -> None:
        self._model = model

    async def decide(self, context):
        return await self._model.decide(self._legacy_context(context))

    def prompt_trace(self, context):
        builder = getattr(self._model, "prompt_trace", None)
        if callable(builder):
            return builder(self._legacy_context(context))
        return {"decision_context": self._legacy_context(context).model_dump(mode="json")}

    @staticmethod
    def _legacy_context(context):
        if isinstance(context.run_state, dict):
            context = context.model_copy(
                update={"run_state": RunState.model_validate(context.run_state)}
            )
        return context

    def __getattr__(self, name: str) -> object:
        return getattr(self._model, name)


class AgentRunner(_CanonicalAgentRunner):
    """Compatibility facade for old callers; new callers use ``runs.execute``."""

    def __init__(self, *, environment, model, **kwargs) -> None:
        if not _publishes_environment_spec(environment):
            config = kwargs.get("config")
            objective = getattr(config, "objective", _DEFAULT_RUNTIME_OBJECTIVE)
            environment = _LegacyEnvironmentAdapter(environment, model, objective=objective)
            model = _LegacyModelAdapter(model)
        super().__init__(environment=environment, model=model, **kwargs)


_record_run_state = record_run_state


def _publishes_environment_spec(environment: object) -> bool:
    """Detect a startup-bound spec without evaluating its property early."""

    declared = inspect.getattr_static(environment, "decision_spec", None)
    if isinstance(declared, (property, EnvironmentDecisionSpec)):
        return True
    try:
        return isinstance(environment.decision_spec, EnvironmentDecisionSpec)
    except (AttributeError, RuntimeError):
        return False


__all__ = ["AgentRunner", "_memory_text", "_prompt_trace", "_record_run_state"]
