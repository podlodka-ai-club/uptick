"""Declarative Stage 1 memory-configuration contract, not a composition root."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from uptick_agent.memory.contracts import ContractModel


class ModuleConfig(ContractModel):
    """Resolved declaration for one optional memory module.

    The limits are hard limits owned by the composition root.  They are not
    retrieval hints a module may elect to ignore.
    """

    schema_version: str = Field(default="1.1", pattern=r"^[1-9][0-9]*\.[0-9]+$")
    enabled: bool = False
    version: str = Field(default="1.0", min_length=1, max_length=64)
    status: Literal["experimental", "default"] = "experimental"
    approval_record_id: str | None = Field(default=None, max_length=256)
    max_context_items: int = Field(default=32, ge=0)
    max_context_tokens: int = Field(default=1_000, ge=0)

    @model_validator(mode="after")
    def _default_requires_approval(self) -> ModuleConfig:
        if self.status == "default" and not self.approval_record_id:
            raise ValueError("status 'default' requires approval_record_id")
        return self


class RetrievalConfig(ContractModel):
    lexical: bool = True
    structured: bool = False
    semantic: bool = False


class ContextBudgetConfig(ContractModel):
    schema_version: str = Field(default="1.1", pattern=r"^[1-9][0-9]*\.[0-9]+$")
    total_items: int = Field(default=128, ge=0)
    total_tokens: int = Field(default=4_000, ge=0)
    per_type_tokens: dict[str, int] = Field(default_factory=dict)
    estimator_id: str = Field(default="utf8-byte-upper-bound", min_length=1, max_length=128)
    estimator_version: str = Field(default="1.0", min_length=1, max_length=64)

    @model_validator(mode="after")
    def _non_negative_type_caps(self) -> ContextBudgetConfig:
        if any(value < 0 for value in self.per_type_tokens.values()):
            raise ValueError("per_type_tokens values must be non-negative")
        return self


class MemoryConfiguration(ContractModel):
    """Resolved feature declarations with deterministic semantic fingerprinting.

    The composition root owns module construction, approval verification,
    diagnostics and context budgeting; ``AgentRunner`` sees only ``AgentMemory``.
    """

    profile_id: str = Field(default="legacy-baseline", min_length=1, max_length=128)
    profile_kind: Literal["development", "experiment", "default"] = "development"
    compatibility_legacy: ModuleConfig = Field(
        default_factory=lambda: ModuleConfig(
            enabled=True,
            version="legacy-1.0",
            max_context_items=128,
            max_context_tokens=4_000,
        )
    )
    episodic: ModuleConfig = Field(default_factory=ModuleConfig)
    lessons: ModuleConfig = Field(default_factory=ModuleConfig)
    world_model: ModuleConfig = Field(default_factory=ModuleConfig)
    playbooks: ModuleConfig = Field(default_factory=ModuleConfig)
    tool_knowledge: ModuleConfig = Field(default_factory=ModuleConfig)
    consolidation: ModuleConfig = Field(default_factory=ModuleConfig)
    forgetting: ModuleConfig = Field(default_factory=ModuleConfig)
    context_budget: ContextBudgetConfig = Field(default_factory=ContextBudgetConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)

    @model_validator(mode="after")
    def _validate_dependencies_and_profile(self) -> MemoryConfiguration:
        if self.world_model.enabled and not (self.episodic.enabled or self.lessons.enabled):
            raise ValueError("world_model requires episodic or lessons")
        if self.playbooks.enabled and not (self.lessons.enabled or self.world_model.enabled):
            raise ValueError("playbooks requires lessons or world_model")
        if self.profile_kind == "default":
            for name, module in self._modules().items():
                if module.enabled and module.status != "default":
                    raise ValueError(f"default profile cannot enable experimental module {name}")
        return self

    def _modules(self) -> dict[str, ModuleConfig]:
        return {
            "compatibility.legacy": self.compatibility_legacy,
            "episodic": self.episodic,
            "lessons": self.lessons,
            "world_model": self.world_model,
            "playbooks": self.playbooks,
            "tool_knowledge": self.tool_knowledge,
            "consolidation": self.consolidation,
            "forgetting": self.forgetting,
        }

    @property
    def modules(self) -> dict[str, ModuleConfig]:
        """A stable copy of resolved module declarations keyed by module ID."""

        return self._modules().copy()

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def legacy_baseline(cls) -> MemoryConfiguration:
        return cls()
