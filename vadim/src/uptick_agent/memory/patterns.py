"""Small deterministic query and validation primitives for derived memory.

The module deliberately separates candidate generation from validation.  A
generator may suggest a regularity from transitions, while the validator
recomputes every matching support and counter query from one immutable
evidence bundle before a derived item can become decision-visible.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from uptick_agent.memory.candidate_validation import validate_evidence
from uptick_agent.memory.contracts import (
    ContractModel,
    ExperienceTransition,
    MemoryPermanentError,
    MemoryValidationError,
    ProvenanceRef,
    RunOutcome,
)
from uptick_agent.memory.lesson_contracts import (
    LESSON_VALIDATION_POLICY,
    LessonEvidence,
    LessonRunDeclaration,
    context_fingerprint,
    context_id,
    declaration_hash,
    snapshot_input_hash,
)
from uptick_agent.memory.stores.contracts import StoredRecord, StructuredMemoryStore, sha256_json
from uptick_agent.redaction import sanitize_json

PATTERN_QUERY_CONTRACT = "memory-pattern-query-v1@1.0"
# Acceptance uses the existing, independently reviewed evidence policy.  The
# pattern query is a separate descriptive projection contract.
PATTERN_VALIDATION_POLICY = LESSON_VALIDATION_POLICY
PATTERN_AUTHORITY_SERVICE = "deterministic-memory-validator@1.0"
PATTERN_RETENTION_POLICY = "simulator-audit-retention-v1@1.0"
_TRANSITION_RECORD_TYPE = "experience-transition"
_OUTCOME_RECORD_TYPE = "run-outcome"
PATTERN_MISSING = object()
REQUEST_SCOPE_MISSING = PATTERN_MISSING


class PatternQuerySettings(ContractModel):
    """Explicit, non-leaking projections used by derived-memory queries.

    Scope paths are rooted at ``observation`` or ``pre_state``.  Action and
    result paths are rooted at their corresponding transition fields, so a
    result cannot accidentally become part of the scope that predicts it.
    """

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


def _validate_dotted_path(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("projection paths must be non-empty dotted names")
    pieces = value.split(".")
    if any(not piece or not piece.replace("_", "").isalnum() for piece in pieces):
        raise ValueError("projection paths must contain only dotted field names")
    return value


def project_dotted(value: object, path: str) -> JsonValue | object:
    """Project a JSON mapping with a deliberately small dotted path."""

    current: object = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return PATTERN_MISSING
        current = current[part]
    return current


def request_scope_value(context: Mapping[str, object], path: str) -> JsonValue | object:
    """Project a request's explicit pre-action observation or state.

    Runner requests expose the latest observation as ``latest_result``.  A
    caller with a richer generic context may provide ``observation`` or
    ``pre_state`` directly.  Missing paths never turn into a match-all.
    """

    root, separator, relative = path.partition(".")
    if not separator:
        return REQUEST_SCOPE_MISSING
    if root == "observation":
        value = context.get("observation")
        if not isinstance(value, Mapping):
            value = context.get("latest_result")
        if not isinstance(value, Mapping):
            return REQUEST_SCOPE_MISSING
    elif root == "pre_state":
        value = context.get("pre_state")
        if not isinstance(value, Mapping):
            return REQUEST_SCOPE_MISSING
    else:
        return REQUEST_SCOPE_MISSING
    projected = project_dotted(value, relative)
    return REQUEST_SCOPE_MISSING if projected is PATTERN_MISSING else projected


def _project_transition_path(transition: ExperienceTransition, path: str) -> JsonValue | object:
    root, separator, relative = path.partition(".")
    if not separator:
        return PATTERN_MISSING
    source = {
        "observation": transition.observation,
        "pre_state": transition.pre_state,
        "action": transition.action,
        "result": transition.result,
    }.get(root)
    if source is None:
        return PATTERN_MISSING
    return project_dotted(source, relative)


class PatternCandidate(ContractModel):
    """A descriptive scoped regularity proposed for independent validation."""

    scope: dict[str, JsonValue] = Field(min_length=1)
    action_kind: JsonValue
    result_path: str = Field(min_length=1, max_length=128)
    result_value: JsonValue
    query_settings: dict[str, JsonValue] = Field(min_length=1)
    query_settings_hash: str = Field(
        default="", min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    candidate_hash: str = Field(default="", min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(default="", min_length=1, max_length=256)
    statement: str = Field(default="", min_length=1, max_length=2_000)

    @model_validator(mode="before")
    @classmethod
    def _derive_identity(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        owned = dict(value)
        required = (
            "scope",
            "action_kind",
            "result_path",
            "result_value",
            "query_settings",
        )
        if not all(key in owned for key in required):
            return owned
        owned.setdefault("query_settings_hash", sha256_json(owned["query_settings"]))
        semantic = {key: owned[key] for key in required}
        digest = sha256_json(semantic)
        owned.setdefault("candidate_hash", digest)
        owned.setdefault("candidate_id", f"pattern:{digest}")
        owned.setdefault(
            "statement",
            "In scope "
            f"{_canonical(owned['scope'])}, action kind {_canonical(owned['action_kind'])} "
            f"was observed with {_canonical(owned['result_path'])}="
            f"{_canonical(owned['result_value'])}; this is an uncertain observational "
            "regularity, not a causal explanation.",
        )
        return owned

    @model_validator(mode="after")
    def _check_identity(self) -> PatternCandidate:
        semantic = self.semantic_payload()
        if sanitize_json(semantic) != semantic:
            raise ValueError("pattern candidate contains credential-shaped content")
        digest = sha256_json(semantic)
        if self.candidate_hash != digest:
            raise ValueError("pattern candidate hash mismatch")
        if self.candidate_id != f"pattern:{digest}":
            raise ValueError("pattern candidate ID mismatch")
        if self.query_settings_hash != sha256_json(self.query_settings):
            raise ValueError("pattern query settings hash mismatch")
        expected = (
            "In scope "
            f"{_canonical(self.scope)}, action kind {_canonical(self.action_kind)} was observed "
            f"with {_canonical(self.result_path)}={_canonical(self.result_value)}; this is an "
            "uncertain observational regularity, not a causal explanation."
        )
        if self.statement != expected:
            raise ValueError("pattern candidate statement mismatch")
        return self

    def semantic_payload(self) -> dict[str, JsonValue]:
        return {
            "scope": self.scope,
            "action_kind": self.action_kind,
            "result_path": self.result_path,
            "result_value": self.result_value,
            "query_settings": self.query_settings,
        }


class PatternValidationManifest(ContractModel):
    """Complete immutable acceptance record for a pattern candidate."""

    policy_ref: Literal[PATTERN_VALIDATION_POLICY]
    query_ref: Literal[PATTERN_QUERY_CONTRACT]
    query_settings: dict[str, JsonValue] = Field(min_length=1)
    query_settings_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    candidate_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    record_ids: tuple[str, ...] = Field(default_factory=tuple)
    record_hashes: tuple[str, ...] = Field(default_factory=tuple)
    declaration_ids: tuple[str, ...] = Field(default_factory=tuple)
    declaration_hashes: tuple[str, ...] = Field(default_factory=tuple)
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
    source_leaf_ids: tuple[str, ...] = Field(default_factory=tuple)
    source_leaf_hashes: tuple[str, ...] = Field(default_factory=tuple)
    omitted_counter_evidence_count: int = Field(ge=0)
    grounding_passed: bool
    support_passed: bool
    context_diversity_passed: bool
    provenance_closed: bool
    counter_search_complete: bool
    support_count: int = Field(ge=0)
    context_count: int = Field(ge=0)
    counter_count: int = Field(ge=0)
    unresolved_contradiction_count: int = Field(ge=0)
    checks: dict[str, bool] = Field(min_length=1)
    authority_service_ref: str = Field(min_length=1, max_length=128)
    decision_record_ref: str = Field(min_length=1, max_length=256)
    decision_record_timestamp: datetime
    retention_policy_ref: str = Field(min_length=1, max_length=128)
    disposition: Literal["candidate", "active", "disputed"]

    @model_validator(mode="after")
    def _validate_lists_and_checks(self) -> PatternValidationManifest:
        if self.query_settings_hash != sha256_json(self.query_settings):
            raise ValueError("pattern manifest query settings hash mismatch")
        pairs = (
            (self.record_ids, self.record_hashes, "record"),
            (self.declaration_ids, self.declaration_hashes, "declaration"),
            (self.searched_evidence_ids, self.searched_evidence_hashes, "searched evidence"),
            (self.support_evidence_ids, self.support_evidence_hashes, "support evidence"),
            (self.counter_evidence_ids, self.counter_evidence_hashes, "counter evidence"),
            (self.source_leaf_ids, self.source_leaf_hashes, "source leaf"),
        )
        for identifiers, hashes, label in pairs:
            if len(identifiers) != len(hashes):
                raise ValueError(f"{label} IDs and hashes must have equal lengths")
            if len(set(identifiers)) != len(identifiers):
                raise ValueError(f"{label} IDs must be unique")
        if self.support_count != len(self.support_logical_run_ids):
            raise ValueError("support_count must equal distinct logical support runs")
        if len(self.support_context_ids) != len(self.support_context_hashes):
            raise ValueError("support context IDs and hashes must have equal lengths")
        if self.context_count != len(set(self.support_context_hashes)):
            raise ValueError("context_count must equal distinct support context hashes")
        if self.counter_count != len(self.counter_evidence_ids):
            raise ValueError("counter_count must equal counter evidence IDs")
        expected_checks = {
            "grounding": self.grounding_passed,
            "support": self.support_passed,
            "context_diversity": self.context_diversity_passed,
            "provenance": self.provenance_closed,
            "counter_search": self.counter_search_complete,
            "no_omitted_counter_evidence": self.omitted_counter_evidence_count == 0,
            "no_unresolved_contradictions": self.unresolved_contradiction_count == 0,
        }
        if self.checks != expected_checks:
            raise ValueError("pattern validation checks do not match manifest fields")
        if self.authority_service_ref != PATTERN_AUTHORITY_SERVICE:
            raise ValueError("unsupported pattern validation authority")
        if self.retention_policy_ref != PATTERN_RETENTION_POLICY:
            raise ValueError("unsupported pattern retention policy")
        if self.decision_record_timestamp.utcoffset() is None:
            raise ValueError("decision record timestamp must include a timezone")
        if self.disposition == "active" and not (
            self.grounding_passed
            and self.support_passed
            and self.context_diversity_passed
            and self.provenance_closed
            and self.counter_search_complete
            and self.omitted_counter_evidence_count == 0
            and self.unresolved_contradiction_count == 0
        ):
            raise ValueError("active pattern disposition requires every acceptance check")
        return self


class ValidatedPattern(ContractModel):
    """Pattern plus its immutable validation result and untrusted provenance."""

    candidate: PatternCandidate
    manifest: PatternValidationManifest
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    confidence_basis: str = Field(min_length=1, max_length=512)
    created_at: datetime
    last_validated_at: datetime
    provenance: list[ProvenanceRef] = Field(default_factory=list)
    trust_classification: Literal["derived_untrusted"] = "derived_untrusted"
    status: Literal["candidate", "active", "disputed"]

    @model_validator(mode="after")
    def _validate_pattern(self) -> ValidatedPattern:
        if self.manifest.candidate_hash != self.candidate.candidate_hash:
            raise ValueError("pattern manifest candidate hash mismatch")
        if self.status != self.manifest.disposition:
            raise ValueError("pattern status does not match manifest disposition")
        if self.created_at.utcoffset() is None or self.last_validated_at.utcoffset() is None:
            raise ValueError("pattern timestamps must include a timezone")
        return self


def _canonical(value: object) -> str:
    from uptick_agent.memory.stores.contracts import canonical_json

    return canonical_json(value)


def _transition_projection(
    transition: ExperienceTransition, settings: PatternQuerySettings
) -> tuple[dict[str, JsonValue], JsonValue, JsonValue] | None:
    scope: dict[str, JsonValue] = {}
    for path in settings.scope_paths:
        value = _project_transition_path(transition, path)
        if value is PATTERN_MISSING:
            return None
        scope[path] = value  # type: ignore[assignment]
    action_kind = _project_transition_path(transition, settings.action_path)
    result_value = _project_transition_path(transition, settings.result_path)
    if action_kind is PATTERN_MISSING or result_value is PATTERN_MISSING:
        return None
    return scope, action_kind, result_value  # type: ignore[return-value]


def generate_pattern_candidates(
    evidence: LessonEvidence, settings: PatternQuerySettings
) -> list[PatternCandidate]:
    """Generate unique scoped observations without deciding their validity."""

    owned_evidence = validate_evidence(evidence)
    owned_settings = PatternQuerySettings.model_validate(settings.model_dump(mode="json"))
    settings_payload = owned_settings.model_dump(mode="json")
    declarations = {item.run_id: item for item in owned_evidence.runs}
    candidates: dict[str, PatternCandidate] = {}
    for record in sorted(owned_evidence.records, key=lambda item: item.record_id):
        if record.record_type != _TRANSITION_RECORD_TYPE:
            continue
        transition = ExperienceTransition.model_validate(record.payload)
        declaration = declarations[transition.run_id]
        if declaration.phase != "learning":
            continue
        projection = _transition_projection(transition, owned_settings)
        if projection is None:
            continue
        scope, action_kind, result_value = projection
        candidate = PatternCandidate(
            scope=scope,
            action_kind=action_kind,
            result_path=owned_settings.result_path,
            result_value=result_value,
            query_settings=settings_payload,
            query_settings_hash=sha256_json(settings_payload),
        )
        candidates[candidate.candidate_hash] = candidate
    return [candidates[key] for key in sorted(candidates)]


def _outcome_record_id(run_id: str) -> str:
    return hashlib.sha256(f"run-outcome:{run_id}".encode()).hexdigest()


def _supporting(declaration: LessonRunDeclaration, outcome: RunOutcome | None) -> bool:
    return bool(
        declaration.phase == "learning"
        and declaration.eligible
        and declaration.attempt_index == 0
        and outcome is not None
        and outcome.status == "completed"
    )


def validated_evidence_parts(
    evidence: LessonEvidence,
) -> tuple[
    LessonEvidence,
    dict[str, LessonRunDeclaration],
    dict[str, RunOutcome],
    dict[str, ExperienceTransition],
    dict[str, StoredRecord],
]:
    """Return the owned evidence indexes shared by all derived modules."""

    owned = validate_evidence(evidence)
    declarations = {item.run_id: item for item in owned.runs}
    outcomes: dict[str, RunOutcome] = {}
    transitions: dict[str, ExperienceTransition] = {}
    records = {record.record_id: record for record in owned.records}
    for record in owned.records:
        if record.record_type == _TRANSITION_RECORD_TYPE:
            transition = ExperienceTransition.model_validate(record.payload)
            transitions[record.record_id] = transition
        elif record.record_type == _OUTCOME_RECORD_TYPE:
            outcome = RunOutcome.model_validate(record.payload)
            if record.record_id != _outcome_record_id(outcome.run_id):
                raise MemoryValidationError("evidence outcome ID is invalid")
            outcomes[outcome.run_id] = outcome
    return owned, declarations, outcomes, transitions, records


def is_support_run(declaration: LessonRunDeclaration, outcome: RunOutcome | None) -> bool:
    """Apply the common learning/eligibility/first-attempt support gate."""

    return _supporting(declaration, outcome)


def declaration_context(declaration: LessonRunDeclaration) -> tuple[str, str]:
    """Return stable display identity and immutable content context hashes."""

    return _context(declaration)


def _context(declaration: LessonRunDeclaration) -> tuple[str, str]:
    return (
        context_id(
            environment_id=declaration.environment_id,
            scenario_id=declaration.scenario_id,
        ),
        context_fingerprint(
            environment_content_hash=declaration.environment_content_hash,
            scenario_content_hash=declaration.scenario_content_hash,
        ),
    )


def validate_pattern_candidate(
    candidate: PatternCandidate,
    evidence: LessonEvidence,
    settings: PatternQuerySettings,
    *,
    decision_record_timestamp: datetime | None = None,
) -> ValidatedPattern:
    """Recompute all support/counter matches from a complete evidence bundle."""

    owned_evidence = validate_evidence(evidence)
    owned_candidate = PatternCandidate.model_validate(candidate.model_dump(mode="json"))
    owned_settings = PatternQuerySettings.model_validate(settings.model_dump(mode="json"))
    settings_payload = owned_settings.model_dump(mode="json")
    if (
        owned_candidate.query_settings != settings_payload
        or owned_candidate.query_settings_hash != sha256_json(settings_payload)
    ):
        raise MemoryValidationError("pattern candidate is not bound to the query settings")
    declarations = {item.run_id: item for item in owned_evidence.runs}
    outcomes: dict[str, RunOutcome] = {}
    transitions: dict[str, ExperienceTransition] = {}
    records = {record.record_id: record for record in owned_evidence.records}
    for record in owned_evidence.records:
        if record.record_type == _TRANSITION_RECORD_TYPE:
            transitions[record.record_id] = ExperienceTransition.model_validate(record.payload)
        elif record.record_type == _OUTCOME_RECORD_TYPE:
            outcome = RunOutcome.model_validate(record.payload)
            if record.record_id != _outcome_record_id(outcome.run_id):
                raise MemoryValidationError("pattern evidence outcome ID is invalid")
            outcomes[outcome.run_id] = outcome

    searched: list[str] = []
    support: list[str] = []
    counter: list[str] = []
    contradiction_count = 0
    source_refs: dict[str, ProvenanceRef] = {}
    for record_id in sorted(transitions):
        transition = transitions[record_id]
        declaration = declarations[transition.run_id]
        if declaration.phase == "frozen_evaluation":
            continue
        if declaration.phase != "learning":
            raise MemoryValidationError("unknown pattern evidence run phase")
        projection = _transition_projection(transition, owned_settings)
        if projection is None:
            continue
        scope, action_kind, result_value = projection
        if (
            _canonical(scope) != _canonical(owned_candidate.scope)
            or _canonical(action_kind) != _canonical(owned_candidate.action_kind)
            or result_value is PATTERN_MISSING
        ):
            continue
        if owned_settings.result_path != owned_candidate.result_path:
            raise MemoryValidationError("pattern candidate is not grounded in query settings")
        searched.append(record_id)
        if _supporting(declaration, outcomes.get(transition.run_id)) and _canonical(
            result_value
        ) == _canonical(owned_candidate.result_value):
            support.append(record_id)
        else:
            counter.append(record_id)
            if _canonical(result_value) != _canonical(owned_candidate.result_value):
                contradiction_count += 1
        for ref in transition.provenance:
            source_refs[ref.artefact_id] = ref

    support_logical = tuple(
        sorted({declarations[transitions[item_id].run_id].logical_run_id for item_id in support})
    )
    support_runs = tuple(sorted({transitions[item_id].run_id for item_id in support}))
    support_contexts = sorted(
        {_context(declarations[transitions[item_id].run_id]) for item_id in support}
    )
    support_context_ids = tuple(item[0] for item in support_contexts)
    support_context_hashes = tuple(item[1] for item in support_contexts)
    record_ids = tuple(sorted(records))
    declarations_sorted = tuple(
        sorted(
            owned_evidence.runs,
            key=lambda item: (item.logical_run_id, item.attempt_index, item.run_id),
        )
    )
    declaration_ids = tuple(
        f"declaration:{item.run_id}:{item.attempt_index}" for item in declarations_sorted
    )
    declaration_hashes = tuple(declaration_hash(item) for item in declarations_sorted)
    searched_ids = tuple(searched)
    support_ids = tuple(support)
    counter_ids = tuple(counter)
    source_leaf_ids = tuple(sorted(source_refs))
    source_leaf_hashes = tuple(source_refs[item_id].content_hash for item_id in source_leaf_ids)
    now = (decision_record_timestamp or owned_evidence.snapshot.created_at).astimezone(UTC)
    manifest = PatternValidationManifest(
        policy_ref=PATTERN_VALIDATION_POLICY,
        query_ref=owned_settings.query_ref,
        query_settings=settings_payload,
        query_settings_hash=sha256_json(settings_payload),
        candidate_hash=owned_candidate.candidate_hash,
        input_hash=snapshot_input_hash(owned_evidence),
        snapshot_id=owned_evidence.snapshot.snapshot_id,
        snapshot_content_hash=owned_evidence.snapshot.content_hash,
        record_ids=record_ids,
        record_hashes=tuple(records[item_id].content_hash for item_id in record_ids),
        declaration_ids=declaration_ids,
        declaration_hashes=declaration_hashes,
        searched_evidence_ids=searched_ids,
        searched_evidence_hashes=tuple(records[item_id].content_hash for item_id in searched_ids),
        support_evidence_ids=support_ids,
        support_evidence_hashes=tuple(records[item_id].content_hash for item_id in support_ids),
        counter_evidence_ids=counter_ids,
        counter_evidence_hashes=tuple(records[item_id].content_hash for item_id in counter_ids),
        support_run_ids=support_runs,
        support_logical_run_ids=support_logical,
        support_context_ids=support_context_ids,
        support_context_hashes=support_context_hashes,
        source_leaf_ids=source_leaf_ids,
        source_leaf_hashes=source_leaf_hashes,
        omitted_counter_evidence_count=0,
        grounding_passed=bool(searched),
        support_passed=len(support_logical) >= 2,
        context_diversity_passed=len(set(support_context_hashes)) >= 2,
        provenance_closed=True,
        counter_search_complete=True,
        support_count=len(support_logical),
        context_count=len(set(support_context_hashes)),
        counter_count=len(counter_ids),
        unresolved_contradiction_count=contradiction_count,
        checks={
            "grounding": bool(searched),
            "support": len(support_logical) >= 2,
            "context_diversity": len(set(support_context_hashes)) >= 2,
            "provenance": True,
            "counter_search": True,
            "no_omitted_counter_evidence": True,
            "no_unresolved_contradictions": contradiction_count == 0,
        },
        authority_service_ref=PATTERN_AUTHORITY_SERVICE,
        decision_record_ref=f"pattern-validation:{owned_candidate.candidate_hash}:{snapshot_input_hash(owned_evidence)}",
        decision_record_timestamp=now,
        retention_policy_ref=PATTERN_RETENTION_POLICY,
        disposition=(
            "disputed"
            if contradiction_count
            else "active"
            if len(support_logical) >= 2 and len(set(support_context_hashes)) >= 2
            else "candidate"
        ),
    )
    confidence_denominator = max(1, len(support) + len(counter))
    confidence = min(1.0, len(support) / confidence_denominator)
    provenance = [source_refs[item_id] for item_id in source_leaf_ids]
    return ValidatedPattern(
        candidate=owned_candidate,
        manifest=manifest,
        confidence=confidence,
        confidence_basis=(
            f"descriptive support fraction {len(support)}/{confidence_denominator}; "
            "not a causal or calibrated probability estimate"
        ),
        created_at=(
            min(records[item_id].created_at for item_id in searched_ids)
            if searched_ids
            else owned_evidence.snapshot.created_at
        ).astimezone(UTC),
        last_validated_at=now,
        provenance=provenance,
        status=manifest.disposition,
    )


async def verify_evidence_against_store(
    store: StructuredMemoryStore, evidence: LessonEvidence
) -> LessonEvidence:
    """Validate a captured bundle and confirm all nested members remain canonical."""

    owned = validate_evidence(evidence)
    snapshot = await store.get_snapshot(snapshot_id=owned.snapshot.snapshot_id)
    if snapshot is None or snapshot.model_dump(mode="json") != owned.snapshot.model_dump(
        mode="json"
    ):
        raise MemoryPermanentError("pattern evidence snapshot changed or is missing")
    records = {record.record_id: record for record in owned.records}
    for member in owned.snapshot.members:
        record = await store.get(namespace=owned.snapshot.namespace, record_id=member.record_id)
        if record is None or record.content_hash != member.content_hash:
            raise MemoryPermanentError("pattern evidence member changed or is missing")
        if record.model_dump(mode="json") != records[member.record_id].model_dump(mode="json"):
            raise MemoryPermanentError("captured pattern evidence differs from the store")
    return owned


__all__ = [
    "PATTERN_AUTHORITY_SERVICE",
    "PATTERN_QUERY_CONTRACT",
    "PATTERN_VALIDATION_POLICY",
    "PatternCandidate",
    "PatternQuerySettings",
    "PatternValidationManifest",
    "PATTERN_MISSING",
    "REQUEST_SCOPE_MISSING",
    "ValidatedPattern",
    "generate_pattern_candidates",
    "declaration_context",
    "is_support_run",
    "project_dotted",
    "request_scope_value",
    "validate_pattern_candidate",
    "validated_evidence_parts",
    "verify_evidence_against_store",
]
