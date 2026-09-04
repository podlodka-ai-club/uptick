"""Declarative Stage 1 memory-configuration contract, not a composition root."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from uptick_agent.memory.contracts import ContractModel

_AUDIT_RETENTION_POLICY_ID = "simulator-audit-retention-v1"
_AUDIT_RETENTION_POLICY_VERSION = "1.0"
_RAW_CONTENT_POLICY_ID = "simulator-raw-content-v1"
_RAW_CONTENT_POLICY_VERSION = "1.0"
_REDACTOR_ID = "credential-pattern-redactor"
_REDACTOR_VERSION = "1.0"


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


class AuditRetentionConfiguration(ContractModel):
    """Resolved Stage 5 retention declaration; execution remains a later stage."""

    policy_id: str = Field(default=_AUDIT_RETENTION_POLICY_ID, min_length=1, max_length=128)
    policy_version: str = Field(
        default=_AUDIT_RETENTION_POLICY_VERSION,
        min_length=1,
        max_length=64,
    )
    raw_content_and_snapshot_days: int = Field(default=90, ge=90)
    summaries: Literal["project_lifetime"] = "project_lifetime"
    validation_promotion_approval_rollback_records: Literal["project_lifetime"] = (
        "project_lifetime"
    )

    @property
    def reference(self) -> str:
        return f"{self.policy_id}@{self.policy_version}"


class RawContentConfiguration(ContractModel):
    """Audit-only switches for the three raw body classes admitted in Stage 5.

    These switches govern structured audit captures. Primary memory records
    keep their structured semantics and always use the shared mandatory
    sanitization boundary.
    """

    policy_id: str = Field(default=_RAW_CONTENT_POLICY_ID, min_length=1, max_length=128)
    policy_version: str = Field(default=_RAW_CONTENT_POLICY_VERSION, min_length=1, max_length=64)
    prompts: bool = True
    observations: bool = True
    decision_traces: bool = True
    retention_policy_ref: str = Field(
        default=f"{_AUDIT_RETENTION_POLICY_ID}@{_AUDIT_RETENTION_POLICY_VERSION}",
        min_length=1,
        max_length=128,
    )
    mandatory_secret_handling: Literal["redact_or_reject"] = "redact_or_reject"
    redactor_id: str = Field(default=_REDACTOR_ID, min_length=1, max_length=128)
    redactor_version: str = Field(default=_REDACTOR_VERSION, min_length=1, max_length=64)

    def captures(self, body_class: Literal["prompts", "observations", "decision_traces"]) -> bool:
        return bool(getattr(self, body_class))


class AuditConfiguration(ContractModel):
    """Resolved audit policy included in the runtime configuration fingerprint."""

    enabled: bool = False
    retention: AuditRetentionConfiguration = Field(default_factory=AuditRetentionConfiguration)
    raw_content: RawContentConfiguration = Field(default_factory=RawContentConfiguration)

    @model_validator(mode="after")
    def _require_supported_policies(self) -> AuditConfiguration:
        if self.retention.policy_id != _AUDIT_RETENTION_POLICY_ID:
            raise ValueError("unsupported audit retention policy")
        if self.retention.policy_version != _AUDIT_RETENTION_POLICY_VERSION:
            raise ValueError("unsupported audit retention policy version")
        if self.raw_content.policy_id != _RAW_CONTENT_POLICY_ID:
            raise ValueError("unsupported raw-content policy")
        if self.raw_content.policy_version != _RAW_CONTENT_POLICY_VERSION:
            raise ValueError("unsupported raw-content policy version")
        if self.raw_content.retention_policy_ref != self.retention.reference:
            raise ValueError("raw-content retention policy reference does not match")
        if self.raw_content.redactor_id != _REDACTOR_ID:
            raise ValueError("unsupported raw-content redactor")
        if self.raw_content.redactor_version != _REDACTOR_VERSION:
            raise ValueError("unsupported raw-content redactor version")
        return self

    @property
    def fingerprint(self) -> str:
        rendered = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    @classmethod
    def simulator_default(cls) -> AuditConfiguration:
        return cls(enabled=True)


class MemoryConfiguration(ContractModel):
    """Resolved feature declarations with deterministic semantic fingerprinting.

    The composition root owns module construction, approval verification,
    diagnostics and context budgeting; ``AgentRunner`` sees only ``AgentMemory``.
    """

    schema_version: str = Field(default="1.1", pattern=r"^[1-9][0-9]*\.[0-9]+$")
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
    audit: AuditConfiguration = Field(default_factory=AuditConfiguration)

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
    def legacy_baseline(
        cls, *, audit: AuditConfiguration | None = None
    ) -> MemoryConfiguration:
        return cls(audit=audit or AuditConfiguration())

    @classmethod
    def episodic_only(
        cls, *, audit: AuditConfiguration | None = None
    ) -> MemoryConfiguration:
        """Experimental Stage 4 profile; callers own its store and namespace."""

        return cls(
            profile_id="episodic-only",
            profile_kind="experiment",
            compatibility_legacy=ModuleConfig(enabled=False),
            episodic=ModuleConfig(
                enabled=True,
                version="1.0",
                max_context_items=32,
                max_context_tokens=4_000,
            ),
            audit=audit or AuditConfiguration(),
        )
