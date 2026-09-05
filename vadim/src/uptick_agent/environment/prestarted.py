"""Reuse a started session after composing a model from its public inputs."""

from __future__ import annotations

from typing import Any

from uptick_agent.environment.contracts import EnvironmentDecisionSpec


class PrestartedEnvironment:
    """One-shot lifecycle facade; it never starts a second physical run."""

    def __init__(self, environment: Any, session: Any, latest: Any) -> None:
        self._environment = environment
        self._session = session
        self._latest = latest
        self._consumed = False
        spec = environment.decision_spec
        if not isinstance(spec, EnvironmentDecisionSpec):
            raise TypeError("environment must publish an EnvironmentDecisionSpec")
        spec.assert_unchanged()
        self.decision_spec = spec

    async def start(self, *, seed: int, agent_id: str, agent_version: str):
        if self._consumed:
            raise RuntimeError("prestarted environment cannot be started twice")
        if seed != self._session.seed:
            raise ValueError("prestarted environment seed changed")
        self._consumed = True
        return self._session, self._latest

    async def execute(self, session, action):
        return await self._environment.execute(session, action)

    def public_state(self, session):
        return self._environment.public_state(session)

    async def finish(self, session, **kwargs):
        return await self._environment.finish(session, **kwargs)
