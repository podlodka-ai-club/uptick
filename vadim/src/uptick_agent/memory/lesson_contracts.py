"""Provider-neutral contracts for deterministic lesson extraction.

The lesson module is deliberately a small boundary layer.  It knows about the
generic transition and store contracts, but does not know about an environment,
an simulator, or an LLM.  Candidate semantics are kept separate from the
evidence and validation manifest so a validator can recompute every claim.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from uptick_agent.memory.contracts import ContractModel, ProvenanceRef
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    StoredRecord,
    canonical_json,
    sha256_json,
)

LESSON_VALIDATION_POLICY = "simulator-candidate-validation-v1@1.0"
LESSON_QUERY_CONTRACT = "exact-observation-action-metric-v1@1.0"


def context_id(*, environment_id: str, scenario_id: str) -> str:
    """Return the stable identity of an immutable environment/scenario context."""

    return sha256_json({"environment_id": environment_id, "scenario_id": scenario_id})


def context_fingerprint(
    *, environment_content_hash: str, scenario_content_hash: str
) -> str:
    """Hash immutable context content independently of display names."""

    return sha256_json(
        {
            "environment_content_hash": environment_content_hash,
            "scenario_content_hash": scenario_content_hash,
        }
    )


class LessonSettings(ContractModel):
    """The complete, fixed policy/query configuration for Stage 6."""

    metric_name: str = Field(min_length=1, max_length=128)
    metric_unit: str = Field(min_length=1, max_length=64)
    direction: Literal["maximize", "minimize"]
    condition_keys: tuple[str, ...] = Field(min_length=1, max_length=64)
    policy_ref: Literal[LESSON_VALIDATION_POLICY] = LESSON_VALIDATION_POLICY
    query_ref: Literal[LESSON_QUERY_CONTRACT] = LESSON_QUERY_CONTRACT

    @field_validator("condition_keys")
    @classmethod
    def _validate_condition_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not key or len(key) > 128 for key in value):
            raise ValueError("condition keys must contain 1-128 character names")
        if len(set(value)) != len(value):
            raise ValueError("condition keys must be unique")
        return value

    @model_validator(mode="after")
    def _reject_credential_shaped_configuration(self) -> LessonSettings:
        from uptick_agent.redaction import sanitize_json

        values = self.model_dump(mode="json")
        if sanitize_json(values) != values:
            raise ValueError("lesson settings contain credential-shaped content")
        return self


class LessonRunDeclaration(ContractModel):
    """Experiment-owned metadata used to classify physical run attempts."""

    run_id: str = Field(min_length=1, max_length=256)
    logical_run_id: str = Field(min_length=1, max_length=256)
    attempt_index: int = Field(default=0, ge=0)
    phase: Literal["learning", "frozen_evaluation"]
    environment_id: str = Field(min_length=1, max_length=256)
    scenario_id: str = Field(min_length=1, max_length=256)
    environment_content_hash: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    scenario_content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    eligible: bool = False


class LessonEvidence(ContractModel):
    """One immutable snapshot and its complete decoded evidence bundle."""

    snapshot: MemorySnapshot
    records: list[StoredRecord] = Field(default_factory=list)
    runs: list[LessonRunDeclaration] = Field(default_factory=list)


class LessonCandidate(ContractModel):
    """A semantic exact-match lesson, with deterministic derived identity."""

    conditions: dict[str, JsonValue] = Field(min_length=1)
    action: dict[str, JsonValue] = Field(min_length=1)
    metric_name: str = Field(min_length=1, max_length=128)
    metric_unit: str = Field(min_length=1, max_length=64)
    direction: Literal["maximize", "minimize"]
    polarity: Literal["positive", "negative"]
    statement: str = Field(default="", min_length=1, max_length=2_000)
    lesson_id: str = Field(default="", min_length=1, max_length=256)
    semantic_hash: str = Field(default="", min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def _derive_identity_inputs(cls, value: object) -> object:
        """Populate derived fields before strict default validation runs."""

        if not isinstance(value, dict):
            return value
        owned = dict(value)
        required = ("conditions", "action", "metric_name", "metric_unit", "direction", "polarity")
        if not all(key in owned for key in required):
            return owned
        semantic = {key: owned[key] for key in required}
        expected_hash = sha256_json(semantic)
        expected_id = f"lesson:{expected_hash}"
        expected_statement = (
            f"When observation conditions {canonical_json(owned['conditions'])} hold, action "
            f"{canonical_json(owned['action'])} tends to have {owned['polarity']} utility for "
            f"{owned['metric_name']!r} ({owned['metric_unit']!r}) when {owned['direction']}."
        )
        owned.setdefault("semantic_hash", expected_hash)
        owned.setdefault("lesson_id", expected_id)
        owned.setdefault("statement", expected_statement)
        if owned.get("semantic_hash") == "":
            owned["semantic_hash"] = expected_hash
        if owned.get("lesson_id") == "":
            owned["lesson_id"] = expected_id
        if owned.get("statement") == "":
            owned["statement"] = expected_statement
        return owned

    @model_validator(mode="after")
    def _derive_and_check_identity(self) -> LessonCandidate:
        from uptick_agent.redaction import sanitize_json

        try:
            semantic_values = {
                "conditions": self.conditions,
                "action": self.action,
                "metric_name": self.metric_name,
                "metric_unit": self.metric_unit,
                "direction": self.direction,
                "polarity": self.polarity,
            }
            safe_values = sanitize_json(semantic_values)
        except (TypeError, ValueError) as error:
            raise ValueError("lesson candidate contains unsupported or non-finite JSON") from error
        if safe_values != semantic_values:
            raise ValueError("lesson candidate contains credential-shaped content")
        semantic = self.semantic_payload()
        expected_hash = sha256_json(semantic)
        expected_id = f"lesson:{expected_hash}"
        expected_statement = (
            f"When observation conditions {canonical_json(self.conditions)} hold, action "
            f"{canonical_json(self.action)} tends to have {self.polarity} utility for "
            f"{self.metric_name!r} ({self.metric_unit!r}) when {self.direction}."
        )
        if self.semantic_hash and self.semantic_hash != expected_hash:
            raise ValueError("lesson candidate semantic hash mismatch")
        if self.lesson_id and self.lesson_id != expected_id:
            raise ValueError("lesson candidate lesson_id mismatch")
        if self.statement and self.statement != expected_statement:
            raise ValueError("lesson candidate statement mismatch")
        self.semantic_hash = expected_hash
        self.lesson_id = expected_id
        self.statement = expected_statement
        return self

    def semantic_payload(self) -> dict[str, JsonValue]:
        """Return only the fields that define semantic lesson identity."""

        return {
            "conditions": self.conditions,
            "action": self.action,
            "metric_name": self.metric_name,
            "metric_unit": self.metric_unit,
            "direction": self.direction,
            "polarity": self.polarity,
        }

class LessonValidationManifest(ContractModel):
    """Reproducible audit record emitted by the independent validator."""

    policy_ref: Literal[LESSON_VALIDATION_POLICY]
    query_ref: Literal[LESSON_QUERY_CONTRACT]
    candidate_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_content_hash: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    record_ids: tuple[str, ...] = Field(default_factory=tuple)
    record_hashes: tuple[str, ...] = Field(default_factory=tuple)
    outcome_record_ids: tuple[str, ...] = Field(default_factory=tuple)
    outcome_record_hashes: tuple[str, ...] = Field(default_factory=tuple)
    declaration_ids: tuple[str, ...] = Field(default_factory=tuple)
    declaration_hashes: tuple[str, ...] = Field(default_factory=tuple)
    context_ids: tuple[str, ...] = Field(default_factory=tuple)
    context_hashes: tuple[str, ...] = Field(default_factory=tuple)
    environment_content_hashes: tuple[str, ...] = Field(default_factory=tuple)
    scenario_content_hashes: tuple[str, ...] = Field(default_factory=tuple)
    source_leaf_ids: tuple[str, ...] = Field(default_factory=tuple)
    source_leaf_hashes: tuple[str, ...] = Field(default_factory=tuple)
    searched_evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    searched_evidence_hashes: tuple[str, ...] = Field(default_factory=tuple)
    support_evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    support_evidence_hashes: tuple[str, ...] = Field(default_factory=tuple)
    counter_evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    counter_evidence_hashes: tuple[str, ...] = Field(default_factory=tuple)
    support_run_ids: tuple[str, ...] = Field(default_factory=tuple)
    support_logical_run_ids: tuple[str, ...] = Field(default_factory=tuple)
    support_context_ids: tuple[str, ...] = Field(default_factory=tuple)
    support_context_hashes: tuple[str, ...] = Field(default_factory=tuple)

    grounding_passed: bool
    polarity_passed: bool
    provenance_closed: bool
    counter_search_complete: bool
    support_count: int = Field(ge=0)
    context_count: int = Field(ge=0)
    counter_count: int = Field(ge=0)
    unresolved_contradiction_count: int = Field(ge=0)
    disposition: Literal["candidate", "active", "disputed"]

    @model_validator(mode="after")
    def _validate_audit_lists(self) -> LessonValidationManifest:
        pairs = (
            (self.record_ids, self.record_hashes, "record"),
            (self.outcome_record_ids, self.outcome_record_hashes, "outcome record"),
            (self.declaration_ids, self.declaration_hashes, "declaration"),
            (self.source_leaf_ids, self.source_leaf_hashes, "source leaf"),
            (self.searched_evidence_ids, self.searched_evidence_hashes, "searched evidence"),
            (self.support_evidence_ids, self.support_evidence_hashes, "support evidence"),
            (self.counter_evidence_ids, self.counter_evidence_hashes, "counter evidence"),
        )
        for identifiers, hashes, label in pairs:
            if len(identifiers) != len(hashes):
                raise ValueError(f"{label} IDs and hashes must have equal lengths")
            if len(set(identifiers)) != len(identifiers):
                raise ValueError(f"{label} IDs must be unique")
        if self.support_count != len(self.support_logical_run_ids):
            raise ValueError("support_count must equal distinct logical support runs")
        if len(self.context_ids) != len(self.context_hashes):
            raise ValueError("context IDs and hashes must have equal lengths")
        if len(self.support_context_ids) != len(self.support_context_hashes):
            raise ValueError("support context IDs and hashes must have equal lengths")
        if self.context_count != len(set(self.support_context_hashes)):
            raise ValueError("context_count must equal distinct immutable support contexts")
        if self.counter_count != len(self.counter_evidence_ids):
            raise ValueError("counter_count must equal counter evidence IDs")
        return self

class ValidatedLesson(ContractModel):
    """A candidate plus a complete independent validation result."""

    candidate: LessonCandidate
    manifest: LessonValidationManifest
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    estimated_utility: float = Field(allow_inf_nan=False)
    created_at: datetime
    last_validated_at: datetime
    provenance: list[ProvenanceRef] = Field(default_factory=list)
    trust_classification: Literal["derived_untrusted"] = "derived_untrusted"
    status: Literal["candidate", "active", "disputed"]

    @model_validator(mode="after")
    def _validate_result(self) -> ValidatedLesson:
        if self.created_at.utcoffset() is None or self.last_validated_at.utcoffset() is None:
            raise ValueError("validated lesson timestamps must include a timezone")
        if self.manifest.candidate_hash != self.candidate.semantic_hash:
            raise ValueError("validated lesson candidate hash does not match manifest")
        if self.status != self.manifest.disposition:
            raise ValueError("validated lesson status does not match manifest disposition")
        return self


def declaration_hash(declaration: LessonRunDeclaration) -> str:
    """Hash one declaration in its canonical serialized form."""

    return sha256_json(declaration.model_dump(mode="json"))


def snapshot_input_hash(evidence: LessonEvidence) -> str:
    """Hash the complete immutable input bundle used by validation."""

    return sha256_json(
        {
            "snapshot": evidence.snapshot.model_dump(mode="json"),
            "records": [record.model_dump(mode="json") for record in evidence.records],
            "runs": [declaration.model_dump(mode="json") for declaration in evidence.runs],
        }
    )


__all__ = [
    "LESSON_QUERY_CONTRACT",
    "LESSON_VALIDATION_POLICY",
    "LessonCandidate",
    "LessonEvidence",
    "LessonRunDeclaration",
    "LessonSettings",
    "LessonValidationManifest",
    "ValidatedLesson",
    "context_id",
    "context_fingerprint",
    "declaration_hash",
    "snapshot_input_hash",
]
