"""Provider- and environment-neutral Stage 1 memory contracts.

These types deliberately describe data crossing the memory boundary.  They do
not describe SQLite rows, simulator responses, or LLM-provider objects.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_MAJOR = 1


def require_finite_json(value: object) -> object:
    """Reject NaN and infinity anywhere inside a JSON-valued contract field."""

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON values must not contain NaN or infinity")
    if isinstance(value, dict):
        for nested in value.values():
            require_finite_json(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            require_finite_json(nested)
    return value


class MemoryContractError(Exception):
    """Base class for failures surfaced by a memory boundary."""


class MemoryValidationError(MemoryContractError):
    """The caller supplied invalid data or configuration; do not retry."""


class MemoryConflictError(MemoryContractError):
    """A concurrent write or idempotency-key replay conflicts; do not retry."""


class MemoryTransientError(MemoryContractError):
    """Temporary infrastructure failure; a caller may apply a bounded retry."""


class MemoryPermanentError(MemoryContractError):
    """A non-retryable infrastructure or compatibility failure."""


class ContractModel(BaseModel):
    """Strict, versioned data that may cross a public memory boundary."""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    schema_version: str = Field(default=SCHEMA_VERSION, pattern=r"^[1-9][0-9]*\.[0-9]+$")

    @model_validator(mode="before")
    @classmethod
    def _ignore_additive_forward_minor_fields(cls, value: object) -> object:
        """Accept only additive forward-minor payloads from the supported major.

        The authored/current 1.0 shape remains strict. A reader receiving a
        1.x payload may ignore fields that did not exist when this reader was
        built, which is the compatibility promise for a minor increment.
        """

        if not isinstance(value, dict):
            return value
        version = value.get("schema_version", SCHEMA_VERSION)
        if not isinstance(version, str) or "." not in version:
            return value
        major_text, minor_text = version.split(".", maxsplit=1)
        if not (major_text.isdigit() and minor_text.isdigit()):
            return value
        current_major, current_minor = (int(part) for part in SCHEMA_VERSION.split("."))
        if int(major_text) == current_major and int(minor_text) > current_minor:
            return {key: item for key, item in value.items() if key in cls.model_fields}
        return value

    @field_validator("schema_version")
    @classmethod
    def _require_supported_major(cls, value: str) -> str:
        major_text, _minor_text = value.split(".", maxsplit=1)
        if int(major_text) != SUPPORTED_SCHEMA_MAJOR:
            raise ValueError(
                f"unsupported schema major {major_text}; expected {SUPPORTED_SCHEMA_MAJOR}"
            )
        return value


class ProvenanceRef(ContractModel):
    artefact_id: str = Field(min_length=1, max_length=256)
    relation: Literal["source", "derived_from", "supports", "contradicts"] = "source"
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class UntrustedMemoryEnvelope(ContractModel):
    """Prompt-facing memory data, always data rather than instruction/policy."""

    item_id: str = Field(min_length=1, max_length=256)
    artefact_type: str = Field(min_length=1, max_length=128)
    origin_module: str = Field(min_length=1, max_length=128)
    origin_version: str = Field(min_length=1, max_length=64)
    trust_classification: Literal["external_untrusted", "derived_untrusted", "human_attested"]
    provenance: list[ProvenanceRef] = Field(min_length=1)
    item: dict[str, JsonValue] = Field(min_length=1)

    @field_validator("item", mode="before")
    @classmethod
    def _require_finite_item(cls, value: object) -> object:
        return require_finite_json(value)


class ObjectiveMetric(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=64)


class ExperienceTransition(ContractModel):
    """A generic record of one observed action/result transition.

    The runner will construct this in Stage 4 through the assembler contract;
    Stage 1 only freezes its shape.
    """

    transition_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    iteration: int = Field(ge=1)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    environment_id: str | None = Field(default=None, max_length=256)
    scenario_id: str | None = Field(default=None, max_length=256)
    trust_classification: Literal["external_untrusted", "derived_untrusted", "human_attested"]
    pre_state: dict[str, JsonValue] = Field(default_factory=dict)
    observation: dict[str, JsonValue] = Field(default_factory=dict)
    action: dict[str, JsonValue] = Field(default_factory=dict)
    result: dict[str, JsonValue] = Field(default_factory=dict)
    objective_metrics: list[ObjectiveMetric] = Field(default_factory=list)
    provenance: list[ProvenanceRef] = Field(min_length=1)
    terminal: bool

    @field_validator("pre_state", "observation", "action", "result", mode="before")
    @classmethod
    def _require_finite_transition_json(cls, value: object) -> object:
        return require_finite_json(value)


class RunOutcome(ContractModel):
    run_id: str = Field(min_length=1, max_length=256)
    status: Literal["completed", "failed", "interrupted", "excluded"]
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    stop_reason: str = Field(min_length=1, max_length=2_000)
    objective_metrics: list[ObjectiveMetric] = Field(default_factory=list)
    terminal: Literal[True] = True


class MemoryContextRequest(ContractModel):
    request_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    query: str = Field(default="", max_length=16_000)
    context: dict[str, JsonValue] = Field(default_factory=dict)
    max_items: int | None = Field(default=None, ge=0)
    max_estimated_tokens: int | None = Field(default=None, ge=0)

    @field_validator("context", mode="before")
    @classmethod
    def _require_finite_context_json(cls, value: object) -> object:
        return require_finite_json(value)


class ContextItem(ContractModel):
    envelope: UntrustedMemoryEnvelope
    score: float = Field(allow_inf_nan=False)
    selection_reason: str = Field(min_length=1, max_length=512)
    estimated_tokens: int = Field(ge=0)


class DecisionMemoryContext(ContractModel):
    """Stable normalized read model; it never exposes persistence entities."""

    items: list[ContextItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MemoryContribution(ContractModel):
    module_id: str = Field(min_length=1, max_length=128)
    module_version: str = Field(min_length=1, max_length=64)
    items: list[ContextItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TransitionAssemblyRequest(ContractModel):
    transition_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    iteration: int = Field(ge=1)
    pre_state: dict[str, JsonValue] = Field(default_factory=dict)
    observation: dict[str, JsonValue] = Field(default_factory=dict)
    action: dict[str, JsonValue] = Field(default_factory=dict)
    result: dict[str, JsonValue] = Field(default_factory=dict)
    terminal: bool

    @field_validator("pre_state", "observation", "action", "result", mode="before")
    @classmethod
    def _require_finite_assembly_json(cls, value: object) -> object:
        return require_finite_json(value)


class ConsolidationRequest(ContractModel):
    request_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=256)
    dry_run: bool = True


class ConsolidationDelta(ContractModel):
    artefact_type: str = Field(min_length=1, max_length=128)
    operation: Literal["create", "update", "supersede"]
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("payload", mode="before")
    @classmethod
    def _require_finite_delta_json(cls, value: object) -> object:
        return require_finite_json(value)


class ConsolidationResult(ContractModel):
    request_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    applied: bool
    deltas: list[ConsolidationDelta] = Field(default_factory=list)


@runtime_checkable
class ExperienceTransitionAssembler(Protocol):
    def assemble(self, request: TransitionAssemblyRequest) -> ExperienceTransition: ...


@runtime_checkable
class ExperienceSink(Protocol):
    async def record(self, transition: ExperienceTransition, *, idempotency_key: str) -> None: ...


@runtime_checkable
class ContextContributor(Protocol):
    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution: ...


@runtime_checkable
class ConsolidationParticipant(Protocol):
    async def consolidate(self, request: ConsolidationRequest) -> ConsolidationResult: ...


@runtime_checkable
class RunFinalizer(Protocol):
    async def finalize(self, outcome: RunOutcome, *, idempotency_key: str) -> None: ...
