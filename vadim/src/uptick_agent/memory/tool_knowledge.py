"""Evidence-backed observations about generic tool input and responses.

Tool knowledge is intentionally separate from world hypotheses and playbooks:
it records response shape or error behaviour for an action/input projection,
without recommending an action or asserting why the response occurred.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, JsonValue, ValidationError, field_validator, model_validator

from uptick_agent.memory.contracts import (
    ContextItem,
    ContractModel,
    ExperienceTransition,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    MemoryValidationError,
    ProvenanceRef,
    RunOutcome,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.lesson_contracts import (
    LESSON_VALIDATION_POLICY,
    LessonEvidence,
    declaration_hash,
    snapshot_input_hash,
)
from uptick_agent.memory.patterns import (
    PATTERN_MISSING,
    REQUEST_SCOPE_MISSING,
    declaration_context,
    is_support_run,
    project_dotted,
    request_scope_value,
    validated_evidence_parts,
    verify_evidence_against_store,
)
from uptick_agent.memory.stores.contracts import (
    RecordWrite,
    StoredRecord,
    StructuredMemoryStore,
    canonical_json,
    sha256_json,
    validate_namespace,
)
from uptick_agent.redaction import sanitize_json

TOOL_KNOWLEDGE_MODULE_ID = "tool_knowledge"
TOOL_KNOWLEDGE_MODULE_VERSION = "1.0"
TOOL_KNOWLEDGE_QUERY_CONTRACT = "memory-tool-knowledge-query-v1@1.0"
TOOL_KNOWLEDGE_AUTHORITY_SERVICE = "deterministic-memory-validator@1.0"
TOOL_KNOWLEDGE_RETENTION_POLICY = "simulator-audit-retention-v1@1.0"
TOOL_KNOWLEDGE_BATCH_RECORD_TYPE = "tool-knowledge-batch"
TOOL_KNOWLEDGE_BATCH_SCHEMA_VERSION = "1.0"
_RETENTION_POLICY_REF = "simulator-audit-retention-v1@1.0"
_WORD = re.compile(r"[\w-]+", re.UNICODE)


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("projection paths must be non-empty dotted names")
    pieces = value.split(".")
    if any(not piece or not piece.replace("_", "").isalnum() for piece in pieces):
        raise ValueError("projection paths must contain only dotted field names")
    return value


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
            _path(path)
        return value

    @field_validator("action_path", "response_path")
    @classmethod
    def _path_values(cls, value: str) -> str:
        return _path(value)

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


class ToolKnowledgeCandidate(ContractModel):
    scope: dict[str, JsonValue] = Field(min_length=1)
    action_kind: JsonValue
    input_features: dict[str, JsonValue] = Field(min_length=1)
    response_path: str = Field(min_length=1, max_length=128)
    response_value: JsonValue
    query_settings: dict[str, JsonValue] = Field(min_length=1)
    query_settings_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    candidate_hash: str = Field(default="", min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(default="", min_length=1, max_length=256)
    statement: str = Field(default="", min_length=1, max_length=2_000)

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        owned = dict(value)
        required = (
            "scope",
            "action_kind",
            "input_features",
            "response_path",
            "response_value",
            "query_settings",
        )
        if not all(key in owned for key in required):
            return owned
        owned.setdefault("query_settings_hash", sha256_json(owned["query_settings"]))
        digest = sha256_json({key: owned[key] for key in required})
        owned.setdefault("candidate_hash", digest)
        owned.setdefault("candidate_id", f"tool-knowledge:{digest}")
        owned.setdefault(
            "statement",
            "For action/input feature "
            f"{canonical_json(owned['action_kind'])}/{canonical_json(owned['input_features'])} "
            f"in scope {canonical_json(owned['scope'])}, response "
            f"{canonical_json(owned['response_path'])} was observed as "
            f"{canonical_json(owned['response_value'])}; this is an uncertain "
            "response-shape observation, not a recommendation or causal explanation.",
        )
        return owned

    @model_validator(mode="after")
    def _identity(self) -> ToolKnowledgeCandidate:
        semantic = self.semantic_payload()
        if sanitize_json(semantic) != semantic:
            raise ValueError("tool knowledge candidate contains credential-shaped content")
        digest = sha256_json(semantic)
        if self.candidate_hash != digest or self.candidate_id != f"tool-knowledge:{digest}":
            raise ValueError("tool knowledge candidate identity mismatch")
        if self.query_settings_hash != sha256_json(self.query_settings):
            raise ValueError("tool knowledge query settings hash mismatch")
        expected = (
            "For action/input feature "
            f"{canonical_json(self.action_kind)}/{canonical_json(self.input_features)} "
            f"in scope {canonical_json(self.scope)}, response "
            f"{canonical_json(self.response_path)} was observed as "
            f"{canonical_json(self.response_value)}; this is an uncertain "
            "response-shape observation, not a recommendation or causal explanation."
        )
        if self.statement != expected:
            raise ValueError("tool knowledge candidate statement mismatch")
        return self

    def semantic_payload(self) -> dict[str, JsonValue]:
        return {
            "scope": self.scope,
            "action_kind": self.action_kind,
            "input_features": self.input_features,
            "response_path": self.response_path,
            "response_value": self.response_value,
            "query_settings": self.query_settings,
        }


class ToolKnowledgeValidationManifest(ContractModel):
    policy_ref: Literal[LESSON_VALIDATION_POLICY]
    query_ref: Literal[TOOL_KNOWLEDGE_QUERY_CONTRACT]
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
    def _checks(self) -> ToolKnowledgeValidationManifest:
        if self.query_settings_hash != sha256_json(self.query_settings):
            raise ValueError("tool knowledge manifest settings hash mismatch")
        pairs = (
            (self.record_ids, self.record_hashes),
            (self.declaration_ids, self.declaration_hashes),
            (self.searched_evidence_ids, self.searched_evidence_hashes),
            (self.support_evidence_ids, self.support_evidence_hashes),
            (self.counter_evidence_ids, self.counter_evidence_hashes),
            (self.source_leaf_ids, self.source_leaf_hashes),
        )
        if any(len(ids) != len(hashes) or len(set(ids)) != len(ids) for ids, hashes in pairs):
            raise ValueError("tool knowledge manifest IDs and hashes are inconsistent")
        if self.support_count != len(self.support_logical_run_ids):
            raise ValueError("tool knowledge support_count is inconsistent")
        if len(self.support_context_ids) != len(self.support_context_hashes):
            raise ValueError("tool knowledge support contexts are inconsistent")
        if self.context_count != len(set(self.support_context_hashes)):
            raise ValueError("tool knowledge context_count is inconsistent")
        if self.counter_count != len(self.counter_evidence_ids):
            raise ValueError("tool knowledge counter_count is inconsistent")
        expected = {
            "grounding": self.grounding_passed,
            "support": self.support_passed,
            "context_diversity": self.context_diversity_passed,
            "provenance": self.provenance_closed,
            "counter_search": self.counter_search_complete,
            "no_omitted_counter_evidence": self.omitted_counter_evidence_count == 0,
            "no_unresolved_contradictions": self.unresolved_contradiction_count == 0,
        }
        if self.checks != expected:
            raise ValueError("tool knowledge validation checks are inconsistent")
        if self.authority_service_ref != TOOL_KNOWLEDGE_AUTHORITY_SERVICE:
            raise ValueError("unsupported tool knowledge authority")
        if self.retention_policy_ref != TOOL_KNOWLEDGE_RETENTION_POLICY:
            raise ValueError("unsupported tool knowledge retention policy")
        if self.decision_record_timestamp.utcoffset() is None:
            raise ValueError("tool knowledge decision timestamp must be timezone-aware")
        if self.disposition == "active" and not (
            self.grounding_passed
            and self.support_passed
            and self.context_diversity_passed
            and self.provenance_closed
            and self.counter_search_complete
            and self.omitted_counter_evidence_count == 0
            and self.unresolved_contradiction_count == 0
        ):
            raise ValueError("active tool knowledge requires every acceptance check")
        return self


class ValidatedToolKnowledge(ContractModel):
    candidate: ToolKnowledgeCandidate
    manifest: ToolKnowledgeValidationManifest
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    confidence_basis: str = Field(min_length=1, max_length=512)
    created_at: datetime
    last_validated_at: datetime
    provenance: list[ProvenanceRef] = Field(default_factory=list)
    trust_classification: Literal["derived_untrusted"] = "derived_untrusted"
    status: Literal["candidate", "active", "disputed"]

    @model_validator(mode="after")
    def _valid(self) -> ValidatedToolKnowledge:
        if self.manifest.candidate_hash != self.candidate.candidate_hash:
            raise ValueError("tool knowledge manifest candidate mismatch")
        if self.status != self.manifest.disposition:
            raise ValueError("tool knowledge status mismatch")
        if self.created_at.utcoffset() is None or self.last_validated_at.utcoffset() is None:
            raise ValueError("tool knowledge timestamps must be timezone-aware")
        return self


class ToolKnowledge(ContractModel):
    item_id: str = Field(min_length=1, max_length=256)
    version: int = Field(ge=1)
    supersedes_id: str | None = Field(default=None, max_length=256)
    validated: ValidatedToolKnowledge

    @model_validator(mode="after")
    def _identity(self) -> ToolKnowledge:
        expected = f"tool-knowledge:{self.validated.candidate.candidate_hash}:v{self.version}"
        if self.item_id != expected:
            raise ValueError("tool knowledge ID does not match candidate version")
        return self


class ToolKnowledgeBatch(ContractModel):
    schema_version: str = Field(
        default=TOOL_KNOWLEDGE_BATCH_SCHEMA_VERSION, pattern=r"^[1-9][0-9]*\.[0-9]+$"
    )
    retention_policy_ref: str = _RETENTION_POLICY_REF
    retention_class: Literal["project_lifetime"] = "project_lifetime"
    settings: ToolKnowledgeQuerySettings
    settings_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    outcome: RunOutcome
    evidence: LessonEvidence
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    items: list[ToolKnowledge] = Field(default_factory=list)


@runtime_checkable
class ToolKnowledgeEvidenceSource(Protocol):
    async def capture(
        self, outcome: RunOutcome, *, idempotency_key: str
    ) -> LessonEvidence | None: ...


def _transition_value(transition: ExperienceTransition, path: str) -> JsonValue | object:
    root, separator, relative = path.partition(".")
    if not separator:
        return PATTERN_MISSING
    source = getattr(transition, root, None)
    if not isinstance(source, dict):
        return PATTERN_MISSING
    return project_dotted(source, relative)


def generate_tool_knowledge_candidates(
    evidence: LessonEvidence, settings: ToolKnowledgeQuerySettings
) -> list[ToolKnowledgeCandidate]:
    owned, declarations, _outcomes, transitions, _records = validated_evidence_parts(evidence)
    owned_settings = ToolKnowledgeQuerySettings.model_validate(settings.model_dump(mode="json"))
    settings_payload = owned_settings.model_dump(mode="json")
    candidates: dict[str, ToolKnowledgeCandidate] = {}
    for transition in sorted(transitions.values(), key=lambda item: item.transition_id):
        declaration = declarations[transition.run_id]
        if declaration.phase != "learning":
            continue
        scope: dict[str, JsonValue] = {}
        for path in owned_settings.scope_paths:
            value = _transition_value(transition, path)
            if value is PATTERN_MISSING:
                break
            scope[path] = value  # type: ignore[assignment]
        else:
            action_kind = _transition_value(transition, owned_settings.action_path)
            features: dict[str, JsonValue] = {}
            for path in owned_settings.input_paths:
                value = _transition_value(transition, path)
                if value is PATTERN_MISSING:
                    break
                features[path] = value  # type: ignore[assignment]
            else:
                response = _transition_value(transition, owned_settings.response_path)
                if action_kind is not PATTERN_MISSING and response is not PATTERN_MISSING:
                    candidate = ToolKnowledgeCandidate(
                        scope=scope,
                        action_kind=action_kind,
                        input_features=features,
                        response_path=owned_settings.response_path,
                        response_value=response,
                        query_settings=settings_payload,
                        query_settings_hash=sha256_json(settings_payload),
                    )
                    candidates[candidate.candidate_hash] = candidate
    return [candidates[key] for key in sorted(candidates)]


def validate_tool_knowledge_candidate(
    candidate: ToolKnowledgeCandidate,
    evidence: LessonEvidence,
    settings: ToolKnowledgeQuerySettings,
    *,
    decision_record_timestamp: datetime | None = None,
) -> ValidatedToolKnowledge:
    owned, declarations, outcomes, transitions, records = validated_evidence_parts(evidence)
    owned_candidate = ToolKnowledgeCandidate.model_validate(candidate.model_dump(mode="json"))
    owned_settings = ToolKnowledgeQuerySettings.model_validate(settings.model_dump(mode="json"))
    settings_payload = owned_settings.model_dump(mode="json")
    if (
        owned_candidate.query_settings != settings_payload
        or owned_candidate.query_settings_hash != sha256_json(settings_payload)
    ):
        raise MemoryValidationError("tool knowledge candidate is not bound to query settings")
    if (
        owned_candidate.response_path != owned_settings.response_path
        or owned_candidate.action_kind is None
        or len(owned_candidate.input_features) != len(owned_settings.input_paths)
    ):
        raise MemoryValidationError("tool knowledge candidate is not grounded in query settings")

    searched: list[str] = []
    support: list[str] = []
    counter: list[str] = []
    contradiction_count = 0
    source_refs: dict[str, ProvenanceRef] = {}
    for transition in sorted(transitions.values(), key=lambda item: item.transition_id):
        scope: dict[str, JsonValue] = {}
        for path in owned_settings.scope_paths:
            value = _transition_value(transition, path)
            if value is PATTERN_MISSING:
                break
            scope[path] = value  # type: ignore[assignment]
        else:
            action_kind = _transition_value(transition, owned_settings.action_path)
            features: dict[str, JsonValue] = {}
            for path in owned_settings.input_paths:
                value = _transition_value(transition, path)
                if value is PATTERN_MISSING:
                    break
                features[path] = value  # type: ignore[assignment]
            else:
                response = _transition_value(transition, owned_settings.response_path)
                if action_kind is PATTERN_MISSING or response is PATTERN_MISSING:
                    continue
                if (
                    canonical_json(scope) != canonical_json(owned_candidate.scope)
                    or canonical_json(action_kind) != canonical_json(owned_candidate.action_kind)
                    or canonical_json(features) != canonical_json(owned_candidate.input_features)
                ):
                    continue
                declaration = declarations[transition.run_id]
                if declaration.phase == "frozen_evaluation":
                    continue
                if declaration.phase != "learning":
                    raise MemoryValidationError(
                        "tool knowledge evidence contains unsupported phase"
                    )
                searched.append(transition.transition_id)
                for ref in transition.provenance:
                    source_refs[ref.artefact_id] = ref
                if is_support_run(declaration, outcomes.get(transition.run_id)) and canonical_json(
                    response
                ) == canonical_json(owned_candidate.response_value):
                    support.append(transition.transition_id)
                else:
                    counter.append(transition.transition_id)
                    if canonical_json(response) != canonical_json(owned_candidate.response_value):
                        contradiction_count += 1

    support_runs = tuple(sorted({transitions[item].run_id for item in support}))
    support_logical = tuple(
        sorted({declarations[transitions[item].run_id].logical_run_id for item in support})
    )
    support_contexts = sorted(
        {declaration_context(declarations[transitions[item].run_id]) for item in support}
    )
    declaration_values = tuple(
        sorted(owned.runs, key=lambda item: (item.logical_run_id, item.attempt_index, item.run_id))
    )
    record_ids = tuple(sorted(records))
    searched_ids = tuple(sorted(set(searched)))
    support_ids = tuple(sorted(set(support)))
    counter_ids = tuple(sorted(set(counter)))
    source_leaf_ids = tuple(sorted(source_refs))
    now = (decision_record_timestamp or owned.snapshot.created_at).astimezone(UTC)
    support_context_ids = tuple(item[0] for item in support_contexts)
    support_context_hashes = tuple(item[1] for item in support_contexts)
    support_passed = len(support_logical) >= 2
    context_diversity_passed = len(set(support_context_hashes)) >= 2
    checks = {
        "grounding": bool(searched_ids),
        "support": support_passed,
        "context_diversity": context_diversity_passed,
        "provenance": True,
        "counter_search": True,
        "no_omitted_counter_evidence": True,
        "no_unresolved_contradictions": contradiction_count == 0,
    }
    disposition: Literal["candidate", "active", "disputed"] = (
        "disputed"
        if contradiction_count
        else "active"
        if support_passed and context_diversity_passed
        else "candidate"
    )
    manifest = ToolKnowledgeValidationManifest(
        policy_ref=LESSON_VALIDATION_POLICY,
        query_ref=owned_settings.query_ref,
        query_settings=settings_payload,
        query_settings_hash=sha256_json(settings_payload),
        candidate_hash=owned_candidate.candidate_hash,
        input_hash=snapshot_input_hash(owned),
        snapshot_id=owned.snapshot.snapshot_id,
        snapshot_content_hash=owned.snapshot.content_hash,
        record_ids=record_ids,
        record_hashes=tuple(records[item].content_hash for item in record_ids),
        declaration_ids=tuple(
            f"declaration:{item.run_id}:{item.attempt_index}" for item in declaration_values
        ),
        declaration_hashes=tuple(declaration_hash(item) for item in declaration_values),
        searched_evidence_ids=searched_ids,
        searched_evidence_hashes=tuple(records[item].content_hash for item in searched_ids),
        support_evidence_ids=support_ids,
        support_evidence_hashes=tuple(records[item].content_hash for item in support_ids),
        counter_evidence_ids=counter_ids,
        counter_evidence_hashes=tuple(records[item].content_hash for item in counter_ids),
        support_run_ids=support_runs,
        support_logical_run_ids=support_logical,
        support_context_ids=support_context_ids,
        support_context_hashes=support_context_hashes,
        source_leaf_ids=source_leaf_ids,
        source_leaf_hashes=tuple(source_refs[item].content_hash for item in source_leaf_ids),
        omitted_counter_evidence_count=0,
        grounding_passed=bool(searched_ids),
        support_passed=support_passed,
        context_diversity_passed=context_diversity_passed,
        provenance_closed=True,
        counter_search_complete=True,
        support_count=len(support_logical),
        context_count=len(set(support_context_hashes)),
        counter_count=len(counter_ids),
        unresolved_contradiction_count=contradiction_count,
        checks=checks,
        authority_service_ref=TOOL_KNOWLEDGE_AUTHORITY_SERVICE,
        decision_record_ref=f"tool-knowledge-validation:{owned_candidate.candidate_hash}:{snapshot_input_hash(owned)}",
        decision_record_timestamp=now,
        retention_policy_ref=TOOL_KNOWLEDGE_RETENTION_POLICY,
        disposition=disposition,
    )
    denominator = max(1, len(support) + len(counter))
    created = (
        min(records[item].created_at for item in searched_ids)
        if searched_ids
        else owned.snapshot.created_at
    ).astimezone(UTC)
    return ValidatedToolKnowledge(
        candidate=owned_candidate,
        manifest=manifest,
        confidence=min(1.0, len(support) / denominator),
        confidence_basis=(
            f"descriptive support fraction {len(support)}/{denominator}; "
            "not a causal or calibrated probability estimate"
        ),
        created_at=created,
        last_validated_at=now,
        provenance=[source_refs[item] for item in source_leaf_ids],
        status=disposition,
    )


def _batch_id(run_id: str) -> str:
    return "tool-knowledge-batch-" + hashlib.sha256(run_id.encode()).hexdigest()


def _snapshot_rank(evidence: LessonEvidence) -> tuple[int, tuple[str, ...]]:
    return (
        len(evidence.snapshot.members),
        tuple(
            sorted(f"{item.record_id}:{item.content_hash}" for item in evidence.snapshot.members)
        ),
    )


class ToolKnowledgeMemory:
    """Optional finalizer/contributor for response-shape observations."""

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        namespace: str,
        source: ToolKnowledgeEvidenceSource | None,
        settings: ToolKnowledgeQuerySettings,
        module_version: str = TOOL_KNOWLEDGE_MODULE_VERSION,
    ) -> None:
        self._store = store
        self._namespace = validate_namespace(namespace)
        if source is not None and not isinstance(source, ToolKnowledgeEvidenceSource):
            raise MemoryValidationError("tool knowledge source must implement capture")
        self._source = source
        self._settings = ToolKnowledgeQuerySettings.model_validate(settings.model_dump(mode="json"))
        if not module_version or len(module_version) > 64:
            raise MemoryValidationError(
                "tool knowledge module_version must contain 1-64 characters"
            )
        self._module_version = module_version

    @property
    def settings(self) -> ToolKnowledgeQuerySettings:
        return self._settings.model_copy(deep=True)

    async def finalize(self, outcome: RunOutcome, *, idempotency_key: str) -> None:
        if not isinstance(outcome, RunOutcome):
            raise MemoryValidationError("tool knowledge outcome must be RunOutcome")
        record_id = _batch_id(outcome.run_id)
        existing = await self._store.get(namespace=self._namespace, record_id=record_id)
        if existing is not None:
            batch = self._read_batch(existing)
            await verify_evidence_against_store(self._store, batch.evidence)
            if batch.outcome.model_dump(mode="json") != outcome.model_dump(mode="json"):
                raise MemoryConflictError("tool knowledge replay has conflicting outcome")
            return
        if self._source is None:
            return
        evidence = await self._source.capture(
            outcome, idempotency_key=f"{TOOL_KNOWLEDGE_MODULE_ID}:{idempotency_key}"
        )
        if evidence is None:
            return
        owned = await verify_evidence_against_store(self._store, evidence)
        candidates = generate_tool_knowledge_candidates(owned, self._settings)
        previous = await self._previous()
        items: list[ToolKnowledge] = []
        for candidate in candidates:
            validated = validate_tool_knowledge_candidate(candidate, owned, self._settings)
            prior = previous.get(candidate.candidate_hash)
            version = prior.version + 1 if prior is not None else 1
            items.append(
                ToolKnowledge(
                    item_id=f"tool-knowledge:{candidate.candidate_hash}:v{version}",
                    version=version,
                    supersedes_id=prior.item_id if prior is not None else None,
                    validated=validated,
                )
            )
        settings_payload = self._settings.model_dump(mode="json")
        batch = ToolKnowledgeBatch(
            settings=self._settings,
            settings_hash=sha256_json(settings_payload),
            outcome=outcome,
            evidence=owned,
            input_hash=snapshot_input_hash(owned),
            items=sorted(items, key=lambda item: item.item_id),
        )
        try:
            await self._store.append(
                RecordWrite(
                    namespace=self._namespace,
                    record_id=record_id,
                    record_type=TOOL_KNOWLEDGE_BATCH_RECORD_TYPE,
                    payload=batch.model_dump(mode="json"),
                    created_at=outcome.finished_at,
                ),
                operation="finalize-tool-knowledge",
                idempotency_key=idempotency_key,
            )
        except MemoryConflictError:
            canonical = await self._store.get(namespace=self._namespace, record_id=record_id)
            if canonical is None:
                raise
            replay = self._read_batch(canonical)
            if replay.outcome.model_dump(mode="json") != outcome.model_dump(mode="json"):
                raise
        canonical = await self._store.get(namespace=self._namespace, record_id=record_id)
        if canonical is None:
            raise MemoryPermanentError("tool knowledge batch is missing after append")
        self._read_batch(canonical)

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        batches = await self._read_batches()
        if not batches:
            return MemoryContribution(
                module_id=TOOL_KNOWLEDGE_MODULE_ID, module_version=self._module_version
            )
        self._require_nested(batches)
        batch = sorted(batches, key=lambda item: _snapshot_rank(item.evidence))[-1]
        query_tokens = _tokens(request.query)
        excluded = {request.run_id}
        physical = request.context.get("physical_run_id")
        if isinstance(physical, str):
            excluded.add(physical)
        ranked: list[tuple[float, ToolKnowledge, int]] = []
        for item in batch.items:
            validated = item.validated
            if validated.status != "active":
                continue
            if any(
                (actual := request_scope_value(request.context, path)) is REQUEST_SCOPE_MISSING
                or canonical_json(actual) != canonical_json(expected)
                for path, expected in validated.candidate.scope.items()
            ):
                continue
            support_ids = set(validated.manifest.support_run_ids) | set(
                validated.manifest.support_logical_run_ids
            )
            if excluded & support_ids:
                continue
            item_tokens = _tokens(validated.candidate.model_dump(mode="json"))
            overlap = len(query_tokens & item_tokens)
            if request.query and overlap == 0:
                continue
            ranked.append((overlap / max(1, len(item_tokens)), item, overlap))
        ranked.sort(key=lambda value: (-value[0], value[1].item_id))
        if request.max_items is not None:
            ranked = ranked[: request.max_items]
        return MemoryContribution(
            module_id=TOOL_KNOWLEDGE_MODULE_ID,
            module_version=self._module_version,
            items=[self._item(item, score, overlap) for score, item, overlap in ranked],
        )

    async def _previous(self) -> dict[str, ToolKnowledge]:
        previous: dict[str, ToolKnowledge] = {}
        for batch in await self._read_batches():
            for item in batch.items:
                prior = previous.get(item.validated.candidate.candidate_hash)
                if prior is None or item.version > prior.version:
                    previous[item.validated.candidate.candidate_hash] = item
        return previous

    async def _read_batches(self) -> list[ToolKnowledgeBatch]:
        batches = []
        for record in await self._store.list(namespace=self._namespace):
            batch = self._read_batch(record)
            await verify_evidence_against_store(self._store, batch.evidence)
            batches.append(batch)
        return batches

    def _read_batch(self, record: StoredRecord) -> ToolKnowledgeBatch:
        try:
            owned = StoredRecord.validate_integrity(record)
            if (
                owned.namespace != self._namespace
                or owned.record_type != TOOL_KNOWLEDGE_BATCH_RECORD_TYPE
            ):
                raise MemoryPermanentError("stored tool knowledge batch type is invalid")
            batch = ToolKnowledgeBatch.model_validate(owned.payload)
            if owned.record_id != _batch_id(batch.outcome.run_id):
                raise MemoryPermanentError("stored tool knowledge batch ID mismatch")
            if owned.created_at != batch.outcome.finished_at:
                raise MemoryPermanentError("stored tool knowledge batch timestamp mismatch")
            if batch.retention_policy_ref != _RETENTION_POLICY_REF:
                raise MemoryPermanentError("stored tool knowledge retention policy is unsupported")
            if batch.settings_hash != sha256_json(batch.settings.model_dump(mode="json")):
                raise MemoryPermanentError("stored tool knowledge settings hash mismatch")
            if batch.input_hash != snapshot_input_hash(batch.evidence):
                raise MemoryPermanentError("stored tool knowledge input hash mismatch")
            if batch.settings != self._settings:
                raise MemoryPermanentError("stored tool knowledge settings do not match runtime")
            for item in batch.items:
                refreshed = validate_tool_knowledge_candidate(
                    item.validated.candidate,
                    batch.evidence,
                    batch.settings,
                    decision_record_timestamp=item.validated.manifest.decision_record_timestamp,
                )
                if refreshed.model_dump(mode="json") != item.validated.model_dump(mode="json"):
                    raise MemoryPermanentError("stored tool knowledge validation changed")
            return batch
        except MemoryPermanentError:
            raise
        except (MemoryValidationError, TypeError, ValueError, ValidationError) as error:
            raise MemoryPermanentError("stored tool knowledge batch is invalid") from error

    @staticmethod
    def _require_nested(batches: list[ToolKnowledgeBatch]) -> None:
        for batch in batches:
            members = {
                (item.record_id, item.content_hash) for item in batch.evidence.snapshot.members
            }
            for other in batches:
                if other is batch:
                    continue
                if other.evidence.snapshot.namespace != batch.evidence.snapshot.namespace:
                    raise MemoryPermanentError("tool knowledge snapshots use different namespaces")
                other_members = {
                    (item.record_id, item.content_hash) for item in other.evidence.snapshot.members
                }
                if not (members <= other_members or other_members <= members):
                    raise MemoryPermanentError("tool knowledge snapshots are not nested")

    def _item(self, item: ToolKnowledge, score: float, overlap: int) -> ContextItem:
        candidate = item.validated.candidate
        return ContextItem(
            envelope=UntrustedMemoryEnvelope(
                item_id=item.item_id,
                artefact_type="tool_knowledge",
                origin_module=TOOL_KNOWLEDGE_MODULE_ID,
                origin_version=self._module_version,
                trust_classification="derived_untrusted",
                provenance=item.validated.provenance,
                item={
                    "adapter_identity": item.validated.candidate.query_settings.get(
                        "adapter_identity", ""
                    ),
                    "scope": candidate.scope,
                    "action_kind": candidate.action_kind,
                    "input_features": candidate.input_features,
                    "response_path": candidate.response_path,
                    "response_value": candidate.response_value,
                    "confidence": item.validated.confidence,
                    "confidence_basis": item.validated.confidence_basis,
                    "status": item.validated.status,
                    "version": item.version,
                    "supersedes_id": item.supersedes_id,
                },
            ),
            score=score,
            selection_reason=f"tool knowledge scope matched; lexical overlap={overlap}",
            estimated_tokens=0,
        )


def _tokens(value: object) -> set[str]:
    return {token.casefold() for token in _WORD.findall(canonical_json(value)) if len(token) > 1}


__all__ = [
    "TOOL_KNOWLEDGE_AUTHORITY_SERVICE",
    "TOOL_KNOWLEDGE_BATCH_RECORD_TYPE",
    "TOOL_KNOWLEDGE_MODULE_ID",
    "TOOL_KNOWLEDGE_MODULE_VERSION",
    "TOOL_KNOWLEDGE_QUERY_CONTRACT",
    "ToolKnowledge",
    "ToolKnowledgeBatch",
    "ToolKnowledgeCandidate",
    "ToolKnowledgeEvidenceSource",
    "ToolKnowledgeMemory",
    "ToolKnowledgeQuerySettings",
    "ToolKnowledgeValidationManifest",
    "ValidatedToolKnowledge",
    "generate_tool_knowledge_candidates",
    "validate_tool_knowledge_candidate",
]
