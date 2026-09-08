from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from uptick_agent.core.models import (
    EnvironmentBrief,
    JsonObject,
    ReasoningTelemetry,
    StrictModel,
)
from uptick_agent.core.models import EnvironmentFact as EnvironmentFact

type BootstrapStatement = Annotated[str, Field(min_length=1, max_length=1_500)]


class EnvironmentProfile(StrictModel):
    environment_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    facts: list[EnvironmentFact] = Field(min_length=1, max_length=128)


class ToolMetadata(StrictModel):
    capability_name: str = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=500)
    operational_notes: list[BootstrapStatement] = Field(default_factory=list, max_length=16)


class ToolRegistry(StrictModel):
    items: list[ToolMetadata] = Field(max_length=256)

    @model_validator(mode="after")
    def require_unique_capabilities(self) -> ToolRegistry:
        names = [item.capability_name for item in self.items]
        if len(names) != len(set(names)):
            raise ValueError("tool registry capability names must be unique")
        return self


class EnvironmentBootstrapDraft(StrictModel):
    facts: list[EnvironmentFact] = Field(min_length=1, max_length=128)
    guidance: list[BootstrapStatement] = Field(min_length=1, max_length=64)
    tools: list[ToolMetadata] = Field(max_length=256)


class EnvironmentBootstrapIdentity(StrictModel):
    environment_id: str = Field(min_length=1)
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bootstrap_prompt_version: str = Field(min_length=1)
    bootstrap_schema_version: str = Field(min_length=1)
    validator_version: str = Field(min_length=1)


class EnvironmentBootstrapBundle(StrictModel):
    profile: EnvironmentProfile
    brief: EnvironmentBrief
    registry: ToolRegistry
    capability_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def decision_brief(self) -> EnvironmentBrief:
        """Project complete facts even from artifacts cached before brief enrichment."""
        return self.brief.model_copy(update={"facts": self.profile.facts}, deep=True)

    @model_validator(mode="after")
    def validate_identity(self) -> EnvironmentBootstrapBundle:
        if self.profile.environment_id != self.brief.environment_id:
            raise ValueError("bootstrap profile and brief environment IDs must match")
        if self.profile.profile_version != self.brief.profile_version:
            raise ValueError("bootstrap profile and brief versions must match")
        return self


class BootstrapValidationResult(StrictModel):
    accepted: bool
    violations: list[str] = Field(default_factory=list)
    validator_version: str = Field(min_length=1)


class EnvironmentBootstrapArtifact(StrictModel):
    schema_version: Literal[3] = 3
    identity: EnvironmentBootstrapIdentity
    source_text: str
    bundle: EnvironmentBootstrapBundle
    validation: BootstrapValidationResult
    reasoner_telemetry: ReasoningTelemetry | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provenance: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_validated_artifact(self) -> EnvironmentBootstrapArtifact:
        if not self.validation.accepted:
            raise ValueError("bootstrap artifacts must contain an accepted validation result")
        if self.identity.environment_id != self.bundle.profile.environment_id:
            raise ValueError("bootstrap identity and bundle environment IDs must match")
        if self.identity.capability_catalog_sha256 != self.bundle.capability_catalog_sha256:
            raise ValueError("bootstrap identity and bundle catalog hashes must match")
        return self
