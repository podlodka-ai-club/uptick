"""Provider-neutral settings shared by memory modules and composition roots.

Keeping these small declarations in a dependency-free module prevents the
configuration contract from importing concrete memory implementations merely
to resolve its field types.  The defining modules re-export the classes for
backwards compatibility.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from uptick_agent.memory.contracts import ContractModel
from uptick_agent.memory.lesson_contracts import LessonSettings

PATTERN_QUERY_CONTRACT = "memory-pattern-query-v1@1.0"
PLAYBOOK_QUERY_CONTRACT = "memory-playbook-query-v1@1.0"
TOOL_KNOWLEDGE_QUERY_CONTRACT = "memory-tool-knowledge-query-v1@1.0"


def _validate_dotted_path(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("projection paths must be non-empty dotted names")
    pieces = value.split(".")
    if any(not piece or not piece.replace("_", "").isalnum() for piece in pieces):
        raise ValueError("projection paths must contain only dotted field names")
    return value


class PatternQuerySettings(ContractModel):
    """Explicit projections used by derived-memory queries."""

    query_ref: Literal[PATTERN_QUERY_CONTRACT] = PATTERN_QUERY_CONTRACT
    scope_paths: tuple[str, ...] = Field(min_length=1, max_length=32)
    action_path: str = Field(min_length=1, max_length=128)
    result_path: str = Field(min_length=1, max_length=128)

    @field_validator("scope_paths")
    @classmethod
    def _validate_scope_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("scope_paths must be unique")
        for path in value:
            _validate_dotted_path(path)
        return value

    @field_validator("action_path", "result_path")
    @classmethod
    def _validate_paths(cls, value: str) -> str:
        return _validate_dotted_path(value)

    @model_validator(mode="after")
    def _require_transition_roots(self) -> PatternQuerySettings:
        if any(not path.startswith(("observation.", "pre_state.")) for path in self.scope_paths):
            raise ValueError("scope paths must be rooted at observation or pre_state")
        if not self.action_path.startswith("action."):
            raise ValueError("action_path must be rooted at action")
        if not self.result_path.startswith("result."):
            raise ValueError("result_path must be rooted at result")
        return self


class PlaybookQuerySettings(ContractModel):
    """Resolved playbook projections and the explicit successful-run guard."""

    query_ref: Literal[PLAYBOOK_QUERY_CONTRACT] = PLAYBOOK_QUERY_CONTRACT
    scope_paths: tuple[str, ...] = Field(min_length=1, max_length=32)
    action_path: str = Field(min_length=1, max_length=128)
    sequence_length: int = Field(default=2, ge=2, le=8)
    guard_path: str = Field(default="result.ok", min_length=1, max_length=128)
    guard_value: JsonValue = True

    @field_validator("scope_paths")
    @classmethod
    def _scope_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("scope_paths must be unique")
        for path in value:
            _validate_dotted_path(path)
        return value

    @field_validator("action_path", "guard_path")
    @classmethod
    def _paths(cls, value: str) -> str:
        return _validate_dotted_path(value)

    @model_validator(mode="after")
    def _roots(self) -> PlaybookQuerySettings:
        if any(not path.startswith(("observation.", "pre_state.")) for path in self.scope_paths):
            raise ValueError("scope paths must be rooted at observation or pre_state")
        if not self.action_path.startswith("action."):
            raise ValueError("action_path must be rooted at action")
        if not self.guard_path.startswith("result."):
            raise ValueError("guard_path must be rooted at result")
        return self


class ToolKnowledgeQuerySettings(ContractModel):
    """Resolved action/input/response projections for one adapter namespace."""

    query_ref: Literal[TOOL_KNOWLEDGE_QUERY_CONTRACT] = TOOL_KNOWLEDGE_QUERY_CONTRACT
    adapter_identity: str = Field(min_length=1, max_length=256)
    scope_paths: tuple[str, ...] = Field(min_length=1, max_length=32)
    action_path: str = Field(min_length=1, max_length=128)
    input_paths: tuple[str, ...] = Field(min_length=1, max_length=32)
    response_path: str = Field(min_length=1, max_length=128)

    @field_validator("scope_paths", "input_paths")
    @classmethod
    def _paths_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("projection paths must be unique")
        for path in value:
            _validate_dotted_path(path)
        return value

    @field_validator("action_path", "response_path")
    @classmethod
    def _path_values(cls, value: str) -> str:
        return _validate_dotted_path(value)

    @model_validator(mode="after")
    def _roots(self) -> ToolKnowledgeQuerySettings:
        if any(not path.startswith(("observation.", "pre_state.")) for path in self.scope_paths):
            raise ValueError("scope paths must be rooted at observation or pre_state")
        if not self.action_path.startswith("action."):
            raise ValueError("action_path must be rooted at action")
        if any(not path.startswith("action.") for path in self.input_paths):
            raise ValueError("input paths must be rooted at action")
        if not self.response_path.startswith("result."):
            raise ValueError("response_path must be rooted at result")
        return self


class ConsolidationSettings(ContractModel):
    """Resolved validator settings and deterministic planning bounds."""

    lesson_settings: LessonSettings | None = None
    pattern_settings: PatternQuerySettings | None = None
    max_replay_records: int = Field(default=200, ge=1, le=10_000)
    max_contrast_pairs: int = Field(default=100, ge=0, le=10_000)

    @model_validator(mode="after")
    def _require_knowledge_settings(self) -> ConsolidationSettings:
        if self.lesson_settings is None and self.pattern_settings is None:
            raise ValueError("consolidation requires lesson or pattern settings")
        return self


__all__ = [
    "ConsolidationSettings",
    "PatternQuerySettings",
    "PlaybookQuerySettings",
    "ToolKnowledgeQuerySettings",
]
