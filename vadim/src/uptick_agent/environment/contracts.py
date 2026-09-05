"""Small, environment-owned contracts used by the generic agent runtime."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

from uptick_agent.redaction import sanitize_json


@dataclass(frozen=True, slots=True)
class EnvironmentDecisionSpec:
    """The public decision surface for one environment.

    The environment owns the response envelope and its typed ``action`` field.
    The runner never discovers or authorises tools from observations, memory,
    or a fixed application-wide action union.
    """

    response_model: type[BaseModel]
    environment_briefing: str | None = None
    objective: str | None = None
    _schema_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.response_model, type) or not issubclass(
            self.response_model, BaseModel
        ):
            raise TypeError("response_model must be a Pydantic BaseModel class")
        if self.environment_briefing is not None and not isinstance(self.environment_briefing, str):
            raise TypeError("environment_briefing must be a string or None")
        if self.objective is not None and not isinstance(self.objective, str):
            raise TypeError("objective must be a string or None")
        object.__setattr__(self, "_schema_json", _schema_json(self.response_model))

    def assert_unchanged(self) -> None:
        if _schema_json(self.response_model) != self._schema_json:
            raise ValueError("environment response schema changed after startup")

    def public_input(self) -> dict[str, Any]:
        """The exact public startup input; it contains no environment instance."""
        self.assert_unchanged()
        return {
            "environment_briefing": self.environment_briefing,
            "objective": self.objective,
            "response_schema": json.loads(self._schema_json),
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.public_input(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def _schema_json(response_model: type[BaseModel]) -> str:
    return json.dumps(response_model.model_json_schema(), sort_keys=True, separators=(",", ":"))


def validate_decision(spec: EnvironmentDecisionSpec, decision: object) -> BaseModel:
    """Revalidate a model response before it can cross into ``execute``.

    Serialising first is intentional: Pydantic can return an existing model
    instance without revalidating it.  Revalidating the JSON payload catches a
    ``model_construct`` value or malformed nested action before the adapter is
    called.
    """

    if not isinstance(spec, EnvironmentDecisionSpec):
        raise TypeError("environment decision spec is required")
    spec.assert_unchanged()
    if not isinstance(decision, BaseModel):
        raise TypeError("decision model must return a Pydantic model instance")
    try:
        payload = decision.model_dump(mode="json", round_trip=True, warnings="error")
        validated = spec.response_model.model_validate(payload)
    except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
        raise ValueError("decision does not match the environment response schema") from error
    if not isinstance(validated, spec.response_model):
        raise TypeError("environment response schema returned an unexpected model")
    decision_action(validated)
    return validated


def decision_action(decision: BaseModel) -> BaseModel:
    """Return the typed action from a validated environment decision."""

    action = getattr(decision, "action", None)
    if not isinstance(action, BaseModel):
        raise ValueError("environment decision must contain a typed action model")
    return action


def public_state_payload(state: object) -> dict[str, Any]:
    """Copy adapter-owned working state into a JSON-safe context payload."""

    if isinstance(state, BaseModel):
        state = state.model_dump(mode="json", round_trip=True, warnings="error")
    if not isinstance(state, Mapping):
        raise TypeError("environment public_state must be a mapping or Pydantic model")
    safe = sanitize_json(dict(state))
    if not isinstance(safe, dict):
        raise TypeError("environment public_state must serialize to an object")
    return safe


__all__ = [
    "EnvironmentDecisionSpec",
    "decision_action",
    "public_state_payload",
    "validate_decision",
]
