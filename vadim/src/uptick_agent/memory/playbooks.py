"""Evidence-backed reusable action sequences.

Playbooks are descriptive procedure candidates.  They contain action kinds and
an explicit guard; the decision model still chooses whether to use a playbook
and supplies the concrete action parameters for the current run.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, JsonValue, ValidationError, model_validator

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
from uptick_agent.memory.settings import PlaybookQuerySettings
from uptick_agent.memory.stores.contracts import (
    RecordWrite,
    StoredRecord,
    StructuredMemoryStore,
    canonical_json,
    sha256_json,
    validate_namespace,
)
from uptick_agent.redaction import sanitize_json

PLAYBOOKS_MODULE_ID = "playbooks"
PLAYBOOKS_MODULE_VERSION = "1.0"
PLAYBOOK_QUERY_CONTRACT = "memory-playbook-query-v1@1.0"
PLAYBOOK_AUTHORITY_SERVICE = "deterministic-memory-validator@1.0"
PLAYBOOK_RETENTION_POLICY = "simulator-audit-retention-v1@1.0"
PLAYBOOK_BATCH_RECORD_TYPE = "playbook-batch"
PLAYBOOK_BATCH_SCHEMA_VERSION = "1.0"
_RETENTION_POLICY_REF = "simulator-audit-retention-v1@1.0"
_TRANSITION_RECORD_TYPE = "experience-transition"
_WORD = re.compile(r"[\w-]+", re.UNICODE)


class PlaybookCandidate(ContractModel):
    scope: dict[str, JsonValue] = Field(min_length=1)
    action_sequence: tuple[JsonValue, ...] = Field(min_length=2, max_length=8)
    guard_path: str = Field(min_length=1, max_length=128)
    guard_value: JsonValue
    query_settings: dict[str, JsonValue] = Field(min_length=1)
    query_settings_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
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
            "action_sequence",
            "guard_path",
            "guard_value",
            "query_settings",
        )
        if not all(key in owned for key in required):
            return owned
        owned.setdefault("query_settings_hash", sha256_json(owned["query_settings"]))
        semantic = {key: owned[key] for key in required}
        digest = sha256_json(semantic)
        owned.setdefault("candidate_hash", digest)
        owned.setdefault("candidate_id", f"playbook:{digest}")
        owned.setdefault(
            "statement",
            "In scope "
            f"{canonical_json(owned['scope'])}, action kinds "
            f"{canonical_json(owned['action_sequence'])} were observed with guard "
            f"{canonical_json(owned['guard_path'])}={canonical_json(owned['guard_value'])}; "
            "this is an uncertain procedure candidate and supplies no action parameters.",
        )
        return owned

    @model_validator(mode="after")
    def _check_identity(self) -> PlaybookCandidate:
        semantic = self.semantic_payload()
        safe_semantic = sanitize_json(semantic)
        if safe_semantic != {**semantic, "action_sequence": list(self.action_sequence)}:
            raise ValueError("playbook candidate contains credential-shaped content")
        digest = sha256_json(semantic)
        if self.candidate_hash != digest or self.candidate_id != f"playbook:{digest}":
            raise ValueError("playbook candidate identity mismatch")
        if self.query_settings_hash != sha256_json(self.query_settings):
            raise ValueError("playbook query settings hash mismatch")
        expected = (
            "In scope "
            f"{canonical_json(self.scope)}, action kinds "
            f"{canonical_json(self.action_sequence)} were observed with guard "
            f"{canonical_json(self.guard_path)}={canonical_json(self.guard_value)}; "
            "this is an uncertain procedure candidate and supplies no action parameters."
        )
        if self.statement != expected:
            raise ValueError("playbook candidate statement mismatch")
        return self

    def semantic_payload(self) -> dict[str, JsonValue]:
        return {
            "scope": self.scope,
            "action_sequence": self.action_sequence,
            "guard_path": self.guard_path,
            "guard_value": self.guard_value,
            "query_settings": self.query_settings,
        }


class PlaybookValidationManifest(ContractModel):
    policy_ref: Literal[LESSON_VALIDATION_POLICY]
    query_ref: Literal[PLAYBOOK_QUERY_CONTRACT]
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
    def _checks(self) -> PlaybookValidationManifest:
        if self.query_settings_hash != sha256_json(self.query_settings):
            raise ValueError("playbook manifest query settings hash mismatch")
        pairs = (
            (self.record_ids, self.record_hashes, "record"),
            (self.declaration_ids, self.declaration_hashes, "declaration"),
            (self.searched_evidence_ids, self.searched_evidence_hashes, "searched"),
            (self.support_evidence_ids, self.support_evidence_hashes, "support"),
            (self.counter_evidence_ids, self.counter_evidence_hashes, "counter"),
            (self.source_leaf_ids, self.source_leaf_hashes, "source leaf"),
        )
        for ids, hashes, label in pairs:
            if len(ids) != len(hashes) or len(set(ids)) != len(ids):
                raise ValueError(f"{label} IDs and hashes are inconsistent")
        if self.support_count != len(self.support_logical_run_ids):
            raise ValueError("playbook support_count is inconsistent")
        if len(self.support_context_ids) != len(self.support_context_hashes):
            raise ValueError("playbook support contexts are inconsistent")
        if self.context_count != len(set(self.support_context_hashes)):
            raise ValueError("playbook context_count is inconsistent")
        if self.counter_count != len(self.counter_evidence_ids):
            raise ValueError("playbook counter_count is inconsistent")
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
            raise ValueError("playbook validation checks are inconsistent")
        if self.authority_service_ref != PLAYBOOK_AUTHORITY_SERVICE:
            raise ValueError("unsupported playbook validation authority")
        if self.retention_policy_ref != PLAYBOOK_RETENTION_POLICY:
            raise ValueError("unsupported playbook retention policy")
        if self.decision_record_timestamp.utcoffset() is None:
            raise ValueError("playbook decision timestamp must be timezone-aware")
        if self.disposition == "active" and not (
            self.grounding_passed
            and self.support_passed
            and self.context_diversity_passed
            and self.provenance_closed
            and self.counter_search_complete
            and self.omitted_counter_evidence_count == 0
            and self.unresolved_contradiction_count == 0
        ):
            raise ValueError("active playbook requires every acceptance check")
        return self


class ValidatedPlaybook(ContractModel):
    candidate: PlaybookCandidate
    manifest: PlaybookValidationManifest
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    confidence_basis: str = Field(min_length=1, max_length=512)
    created_at: datetime
    last_validated_at: datetime
    provenance: list[ProvenanceRef] = Field(default_factory=list)
    trust_classification: Literal["derived_untrusted"] = "derived_untrusted"
    status: Literal["candidate", "active", "disputed"]

    @model_validator(mode="after")
    def _valid(self) -> ValidatedPlaybook:
        if self.manifest.candidate_hash != self.candidate.candidate_hash:
            raise ValueError("playbook manifest candidate mismatch")
        if self.status != self.manifest.disposition:
            raise ValueError("playbook status mismatch")
        if self.created_at.utcoffset() is None or self.last_validated_at.utcoffset() is None:
            raise ValueError("playbook timestamps must be timezone-aware")
        return self


class Playbook(ContractModel):
    playbook_id: str = Field(min_length=1, max_length=256)
    version: int = Field(ge=1)
    supersedes_id: str | None = Field(default=None, max_length=256)
    validated: ValidatedPlaybook

    @model_validator(mode="after")
    def _identity(self) -> Playbook:
        expected = f"playbook:{self.validated.candidate.candidate_hash}:v{self.version}"
        if self.playbook_id != expected:
            raise ValueError("playbook ID does not match candidate version")
        return self


class PlaybookBatch(ContractModel):
    schema_version: str = Field(
        default=PLAYBOOK_BATCH_SCHEMA_VERSION, pattern=r"^[1-9][0-9]*\.[0-9]+$"
    )
    retention_policy_ref: str = _RETENTION_POLICY_REF
    retention_class: Literal["project_lifetime"] = "project_lifetime"
    settings: PlaybookQuerySettings
    settings_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    outcome: RunOutcome
    evidence: LessonEvidence
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    playbooks: list[Playbook] = Field(default_factory=list)


@runtime_checkable
class PlaybookEvidenceSource(Protocol):
    async def capture(
        self, outcome: RunOutcome, *, idempotency_key: str
    ) -> LessonEvidence | None: ...


def _transition_value(transition: object, path: str) -> JsonValue | object:
    root, separator, relative = path.partition(".")
    if not separator:
        return PATTERN_MISSING
    source = getattr(transition, root, None)
    if not isinstance(source, dict):
        return PATTERN_MISSING
    return project_dotted(source, relative)


def _windows(
    transitions: dict[str, ExperienceTransition],
    settings: PlaybookQuerySettings,
) -> list[
    tuple[str, list[ExperienceTransition], dict[str, JsonValue], tuple[JsonValue, ...], JsonValue]
]:
    by_run: dict[str, list[ExperienceTransition]] = defaultdict(list)
    for transition in transitions.values():
        by_run[transition.run_id].append(transition)
    result: list[
        tuple[
            str, list[ExperienceTransition], dict[str, JsonValue], tuple[JsonValue, ...], JsonValue
        ]
    ] = []
    for run_id, values in by_run.items():
        values.sort(key=lambda item: (item.iteration, item.occurred_at, item.transition_id))
        if len(values) < settings.sequence_length:
            continue
        for start in range(len(values) - settings.sequence_length + 1):
            window = values[start : start + settings.sequence_length]
            if any(
                right.iteration != left.iteration + 1
                for left, right in zip(window, window[1:], strict=False)
            ):
                continue
            scope: dict[str, JsonValue] = {}
            for path in settings.scope_paths:
                value = _transition_value(window[0], path)
                if value is PATTERN_MISSING:
                    break
                scope[path] = value  # type: ignore[assignment]
            else:
                sequence: list[JsonValue] = []
                for item in window:
                    value = _transition_value(item, settings.action_path)
                    if value is PATTERN_MISSING:
                        break
                    sequence.append(value)  # type: ignore[arg-type]
                else:
                    guard = _transition_value(window[-1], settings.guard_path)
                    if guard is not PATTERN_MISSING:
                        result.append((run_id, window, scope, tuple(sequence), guard))
    return result


def generate_playbook_candidates(
    evidence: LessonEvidence, settings: PlaybookQuerySettings
) -> list[PlaybookCandidate]:
    owned, declarations, _outcomes, transitions, _records = validated_evidence_parts(evidence)
    owned_settings = PlaybookQuerySettings.model_validate(settings.model_dump(mode="json"))
    settings_payload = owned_settings.model_dump(mode="json")
    candidates: dict[str, PlaybookCandidate] = {}
    for run_id, _window, scope, sequence, guard in _windows(transitions, owned_settings):
        declaration = declarations[run_id]
        if declaration.phase != "learning" or canonical_json(guard) != canonical_json(
            owned_settings.guard_value
        ):
            continue
        candidate = PlaybookCandidate(
            scope=scope,
            action_sequence=sequence,
            guard_path=owned_settings.guard_path,
            guard_value=owned_settings.guard_value,
            query_settings=settings_payload,
            query_settings_hash=sha256_json(settings_payload),
        )
        candidates[candidate.candidate_hash] = candidate
    return [candidates[key] for key in sorted(candidates)]


def validate_playbook_candidate(
    candidate: PlaybookCandidate,
    evidence: LessonEvidence,
    settings: PlaybookQuerySettings,
    *,
    decision_record_timestamp: datetime | None = None,
) -> ValidatedPlaybook:
    owned, declarations, outcomes, transitions, records = validated_evidence_parts(evidence)
    owned_candidate = PlaybookCandidate.model_validate(candidate.model_dump(mode="json"))
    owned_settings = PlaybookQuerySettings.model_validate(settings.model_dump(mode="json"))
    settings_payload = owned_settings.model_dump(mode="json")
    if (
        owned_candidate.query_settings != settings_payload
        or owned_candidate.query_settings_hash != sha256_json(settings_payload)
    ):
        raise MemoryValidationError("playbook candidate is not bound to query settings")
    if (
        owned_candidate.guard_path != owned_settings.guard_path
        or canonical_json(owned_candidate.guard_value) != canonical_json(owned_settings.guard_value)
        or len(owned_candidate.action_sequence) != owned_settings.sequence_length
    ):
        raise MemoryValidationError("playbook candidate is not grounded in query settings")

    searched: list[str] = []
    support: list[str] = []
    counter: list[str] = []
    contradiction_count = 0
    source_refs: dict[str, ProvenanceRef] = {}
    for run_id, window, scope, sequence, guard in _windows(transitions, owned_settings):
        if canonical_json(scope) != canonical_json(owned_candidate.scope) or canonical_json(
            sequence
        ) != canonical_json(owned_candidate.action_sequence):
            continue
        declaration = declarations[run_id]
        if declaration.phase == "frozen_evaluation":
            continue
        if declaration.phase != "learning":
            raise MemoryValidationError("playbook evidence contains unsupported phase")
        searched.extend(item.transition_id for item in window)
        for item in window:
            for ref in item.provenance:
                source_refs[ref.artefact_id] = ref
        outcome = outcomes.get(run_id)
        window_key = window[-1].transition_id
        if is_support_run(declaration, outcome) and canonical_json(guard) == canonical_json(
            owned_candidate.guard_value
        ):
            support.append(window_key)
        else:
            counter.append(window_key)
            if canonical_json(guard) != canonical_json(owned_candidate.guard_value):
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
    manifest = PlaybookValidationManifest(
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
        authority_service_ref=PLAYBOOK_AUTHORITY_SERVICE,
        decision_record_ref=f"playbook-validation:{owned_candidate.candidate_hash}:{snapshot_input_hash(owned)}",
        decision_record_timestamp=now,
        retention_policy_ref=PLAYBOOK_RETENTION_POLICY,
        disposition=disposition,
    )
    denominator = max(1, len(support) + len(counter))
    created = (
        min(records[item].created_at for item in searched_ids)
        if searched_ids
        else owned.snapshot.created_at
    ).astimezone(UTC)
    validated = ValidatedPlaybook(
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
    return validated


def _batch_id(run_id: str) -> str:
    return "playbook-batch-" + hashlib.sha256(run_id.encode()).hexdigest()


def _snapshot_rank(evidence: LessonEvidence) -> tuple[int, tuple[str, ...]]:
    return (
        len(evidence.snapshot.members),
        tuple(
            sorted(f"{item.record_id}:{item.content_hash}" for item in evidence.snapshot.members)
        ),
    )


def _tokens(value: object) -> set[str]:
    return {token.casefold() for token in _WORD.findall(canonical_json(value)) if len(token) > 1}


class PlaybooksMemory:
    """Optional finalizer/contributor for validated procedure candidates."""

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        namespace: str,
        source: PlaybookEvidenceSource | None,
        settings: PlaybookQuerySettings,
        module_version: str = PLAYBOOKS_MODULE_VERSION,
    ) -> None:
        self._store = store
        self._namespace = validate_namespace(namespace)
        if source is not None and not isinstance(source, PlaybookEvidenceSource):
            raise MemoryValidationError("playbooks source must implement capture")
        self._source = source
        self._settings = PlaybookQuerySettings.model_validate(settings.model_dump(mode="json"))
        if not module_version or len(module_version) > 64:
            raise MemoryValidationError("playbooks module_version must contain 1-64 characters")
        self._module_version = module_version

    @property
    def settings(self) -> PlaybookQuerySettings:
        return self._settings.model_copy(deep=True)

    async def finalize(self, outcome: RunOutcome, *, idempotency_key: str) -> None:
        if not isinstance(outcome, RunOutcome):
            raise MemoryValidationError("playbooks outcome must be RunOutcome")
        record_id = _batch_id(outcome.run_id)
        existing = await self._store.get(namespace=self._namespace, record_id=record_id)
        if existing is not None:
            batch = self._read_batch(existing)
            await verify_evidence_against_store(self._store, batch.evidence)
            if batch.outcome.model_dump(mode="json") != outcome.model_dump(mode="json"):
                raise MemoryConflictError("playbook finalization replay has conflicting outcome")
            return
        if self._source is None:
            return
        evidence = await self._source.capture(
            outcome, idempotency_key=f"{PLAYBOOKS_MODULE_ID}:{idempotency_key}"
        )
        if evidence is None:
            return
        owned = await verify_evidence_against_store(self._store, evidence)
        candidates = generate_playbook_candidates(owned, self._settings)
        previous = await self._previous()
        playbooks: list[Playbook] = []
        for candidate in candidates:
            validated = validate_playbook_candidate(candidate, owned, self._settings)
            prior = previous.get(candidate.candidate_hash)
            version = prior.version + 1 if prior is not None else 1
            playbooks.append(
                Playbook(
                    playbook_id=f"playbook:{candidate.candidate_hash}:v{version}",
                    version=version,
                    supersedes_id=prior.playbook_id if prior is not None else None,
                    validated=validated,
                )
            )
        settings_payload = self._settings.model_dump(mode="json")
        batch = PlaybookBatch(
            settings=self._settings,
            settings_hash=sha256_json(settings_payload),
            outcome=outcome,
            evidence=owned,
            input_hash=snapshot_input_hash(owned),
            playbooks=sorted(playbooks, key=lambda item: item.playbook_id),
        )
        try:
            await self._store.append(
                RecordWrite(
                    namespace=self._namespace,
                    record_id=record_id,
                    record_type=PLAYBOOK_BATCH_RECORD_TYPE,
                    payload=batch.model_dump(mode="json"),
                    created_at=outcome.finished_at,
                ),
                operation="finalize-playbooks",
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
            raise MemoryPermanentError("playbook batch is missing after append")
        self._read_batch(canonical)

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        batches = await self._read_batches()
        if not batches:
            return MemoryContribution(
                module_id=PLAYBOOKS_MODULE_ID, module_version=self._module_version
            )
        self._require_nested(batches)
        batch = sorted(batches, key=lambda item: _snapshot_rank(item.evidence))[-1]
        latest = []
        for item in batch.playbooks:
            current = validate_playbook_candidate(
                item.validated.candidate,
                batch.evidence,
                batch.settings,
                decision_record_timestamp=item.validated.manifest.decision_record_timestamp,
            )
            if current.manifest.model_dump(mode="json") != item.validated.manifest.model_dump(
                mode="json"
            ):
                raise MemoryPermanentError("stored playbook manifest changed")
            if current.status != item.validated.status:
                raise MemoryPermanentError("stored playbook status changed")
            latest.append(item)
        query_tokens = _tokens(request.query)
        excluded = {request.run_id}
        physical = request.context.get("physical_run_id")
        if isinstance(physical, str):
            excluded.add(physical)
        ranked: list[tuple[float, Playbook, int]] = []
        for item in latest:
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
            score = overlap / max(1, len(item_tokens))
            ranked.append((score, item, overlap))
        ranked.sort(key=lambda value: (-value[0], value[1].playbook_id))
        if request.max_items is not None:
            ranked = ranked[: request.max_items]
        return MemoryContribution(
            module_id=PLAYBOOKS_MODULE_ID,
            module_version=self._module_version,
            items=[self._item(item, score, overlap) for score, item, overlap in ranked],
        )

    async def _previous(self) -> dict[str, Playbook]:
        previous: dict[str, Playbook] = {}
        for batch in await self._read_batches():
            for item in batch.playbooks:
                prior = previous.get(item.validated.candidate.candidate_hash)
                if prior is None or item.version > prior.version:
                    previous[item.validated.candidate.candidate_hash] = item
        return previous

    async def _read_batches(self) -> list[PlaybookBatch]:
        batches = []
        for record in await self._store.list(namespace=self._namespace):
            batch = self._read_batch(record)
            await verify_evidence_against_store(self._store, batch.evidence)
            batches.append(batch)
        return batches

    def _read_batch(self, record: StoredRecord) -> PlaybookBatch:
        try:
            owned = StoredRecord.validate_integrity(record)
            if (
                owned.namespace != self._namespace
                or owned.record_type != PLAYBOOK_BATCH_RECORD_TYPE
            ):
                raise MemoryPermanentError("stored playbook batch namespace or type is invalid")
            batch = PlaybookBatch.model_validate(owned.payload)
            if owned.record_id != _batch_id(batch.outcome.run_id):
                raise MemoryPermanentError("stored playbook batch ID mismatch")
            if owned.created_at != batch.outcome.finished_at:
                raise MemoryPermanentError("stored playbook batch timestamp mismatch")
            if batch.retention_policy_ref != _RETENTION_POLICY_REF:
                raise MemoryPermanentError("stored playbook retention policy is unsupported")
            if batch.settings_hash != sha256_json(batch.settings.model_dump(mode="json")):
                raise MemoryPermanentError("stored playbook settings hash mismatch")
            if batch.input_hash != snapshot_input_hash(batch.evidence):
                raise MemoryPermanentError("stored playbook input hash mismatch")
            if batch.settings != self._settings:
                raise MemoryPermanentError("stored playbook settings do not match runtime")
            for item in batch.playbooks:
                refreshed = validate_playbook_candidate(
                    item.validated.candidate,
                    batch.evidence,
                    batch.settings,
                    decision_record_timestamp=item.validated.manifest.decision_record_timestamp,
                )
                if refreshed.model_dump(mode="json") != item.validated.model_dump(mode="json"):
                    raise MemoryPermanentError("stored playbook validation changed")
            return batch
        except MemoryPermanentError:
            raise
        except (MemoryValidationError, TypeError, ValueError, ValidationError) as error:
            raise MemoryPermanentError("stored playbook batch is invalid") from error

    @staticmethod
    def _require_nested(batches: list[PlaybookBatch]) -> None:
        for batch in batches:
            members = {
                (item.record_id, item.content_hash) for item in batch.evidence.snapshot.members
            }
            for other in batches:
                if other is batch:
                    continue
                if other.evidence.snapshot.namespace != batch.evidence.snapshot.namespace:
                    raise MemoryPermanentError("playbook snapshots use different namespaces")
                other_members = {
                    (item.record_id, item.content_hash) for item in other.evidence.snapshot.members
                }
                if not (members <= other_members or other_members <= members):
                    raise MemoryPermanentError("playbook snapshots are not nested")

    def _item(self, item: Playbook, score: float, overlap: int) -> ContextItem:
        validated = item.validated
        candidate = validated.candidate.model_dump(mode="json")
        return ContextItem(
            envelope=UntrustedMemoryEnvelope(
                item_id=item.playbook_id,
                artefact_type="playbook",
                origin_module=PLAYBOOKS_MODULE_ID,
                origin_version=self._module_version,
                trust_classification="derived_untrusted",
                provenance=validated.provenance,
                item={
                    "scope": candidate["scope"],
                    "action_sequence": candidate["action_sequence"],
                    "guard_path": candidate["guard_path"],
                    "guard_value": candidate["guard_value"],
                    "confidence": validated.confidence,
                    "confidence_basis": validated.confidence_basis,
                    "status": validated.status,
                    "version": item.version,
                    "supersedes_id": item.supersedes_id,
                    "parameters": "chosen by the current decision model",
                },
            ),
            score=score,
            selection_reason=f"playbooks scope matched; lexical overlap={overlap}",
            estimated_tokens=0,
        )


__all__ = [
    "PLAYBOOK_AUTHORITY_SERVICE",
    "PLAYBOOKS_MODULE_ID",
    "PLAYBOOKS_MODULE_VERSION",
    "PLAYBOOK_BATCH_RECORD_TYPE",
    "PLAYBOOK_QUERY_CONTRACT",
    "Playbook",
    "PlaybookBatch",
    "PlaybookCandidate",
    "PlaybookEvidenceSource",
    "PlaybookQuerySettings",
    "PlaybookValidationManifest",
    "PlaybooksMemory",
    "ValidatedPlaybook",
    "generate_playbook_candidates",
    "validate_playbook_candidate",
]
