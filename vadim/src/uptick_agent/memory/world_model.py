"""Evidence-backed observational world hypotheses.

World hypotheses are deliberately narrower than causal explanations: they
record a scoped result feature observed after an action kind.  The source
captures an immutable episodic bundle, while this module only persists and
retrieves the independently validated derived view.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, ValidationError, model_validator

from uptick_agent.memory.contracts import (
    ContextItem,
    ContractModel,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    MemoryValidationError,
    ProvenanceRef,
    RunOutcome,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.patterns import (
    REQUEST_SCOPE_MISSING,
    PatternCandidate,
    PatternQuerySettings,
    PatternValidationManifest,
    ValidatedPattern,
    generate_pattern_candidates,
    request_scope_value,
    validate_pattern_candidate,
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

WORLD_MODEL_MODULE_ID = "world_model"
WORLD_MODEL_MODULE_VERSION = "1.0"
WORLD_BATCH_RECORD_TYPE = "world-hypothesis-batch"
WORLD_BATCH_SCHEMA_VERSION = "1.0"
_RETENTION_POLICY_REF = "simulator-audit-retention-v1@1.0"
_WORD = re.compile(r"[\w-]+", re.UNICODE)


@runtime_checkable
class WorldEvidenceSource(Protocol):
    async def capture(
        self, outcome: RunOutcome, *, idempotency_key: str
    ) -> LessonEvidence | None: ...


class WorldHypothesis(ContractModel):
    """Versioned, uncertain descriptive regularity exposed as untrusted data."""

    hypothesis_id: str = Field(default="", min_length=1, max_length=256)
    version: int = Field(ge=1)
    supersedes_id: str | None = Field(default=None, max_length=256)
    candidate: PatternCandidate
    manifest: PatternValidationManifest
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    confidence_basis: str = Field(min_length=1, max_length=512)
    created_at: datetime
    last_validated_at: datetime
    provenance: list[ProvenanceRef] = Field(default_factory=list)
    trust_classification: Literal["derived_untrusted"] = "derived_untrusted"
    status: Literal["candidate", "active", "disputed"]

    @staticmethod
    def expected_id(candidate_hash: str, version: int) -> str:
        return f"world-hypothesis:{candidate_hash}:v{version}"

    @classmethod
    def from_validated(
        cls,
        validated: ValidatedPattern,
        *,
        version: int,
        supersedes_id: str | None,
    ) -> WorldHypothesis:
        return cls(
            hypothesis_id=cls.expected_id(validated.candidate.candidate_hash, version),
            version=version,
            supersedes_id=supersedes_id,
            candidate=validated.candidate,
            manifest=validated.manifest,
            confidence=validated.confidence,
            confidence_basis=validated.confidence_basis,
            created_at=validated.created_at,
            last_validated_at=validated.last_validated_at,
            provenance=validated.provenance,
            trust_classification=validated.trust_classification,
            status=validated.status,
        )

    @model_validator(mode="after")
    def _validate_hypothesis(self) -> WorldHypothesis:
        if self.hypothesis_id != self.expected_id(self.candidate.candidate_hash, self.version):
            raise ValueError("world hypothesis ID does not match candidate version")
        if self.trust_classification != "derived_untrusted":
            raise ValueError("world hypotheses must remain derived_untrusted")
        if self.status != self.manifest.disposition:
            raise ValueError("world hypothesis status does not match manifest disposition")
        if self.created_at.utcoffset() is None or self.last_validated_at.utcoffset() is None:
            raise ValueError("world hypothesis timestamps must include a timezone")
        return self


class WorldHypothesisBatch(ContractModel):
    schema_version: str = Field(
        default=WORLD_BATCH_SCHEMA_VERSION, pattern=r"^[1-9][0-9]*\.[0-9]+$"
    )
    retention_policy_ref: str = _RETENTION_POLICY_REF
    retention_class: Literal["project_lifetime"] = "project_lifetime"
    settings: PatternQuerySettings
    settings_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    outcome: RunOutcome
    evidence: LessonEvidence
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    hypotheses: list[WorldHypothesis] = Field(default_factory=list)


def _owned_outcome(outcome: RunOutcome) -> RunOutcome:
    if not isinstance(outcome, RunOutcome):
        raise MemoryValidationError("world outcome must be RunOutcome")
    try:
        serialized = sanitize_json(outcome.model_dump(mode="json"))
        owned = RunOutcome.model_validate(serialized)
    except (TypeError, ValueError, ValidationError) as error:
        raise MemoryValidationError("world outcome is invalid") from error
    if owned.finished_at.utcoffset() is None:
        raise MemoryValidationError("world outcome timestamp must include a timezone")
    return owned.model_copy(update={"finished_at": owned.finished_at.astimezone(UTC)})


def _owned_settings(settings: PatternQuerySettings) -> PatternQuerySettings:
    if not isinstance(settings, PatternQuerySettings):
        raise MemoryValidationError("world settings must be PatternQuerySettings")
    try:
        return PatternQuerySettings.model_validate(settings.model_dump(mode="json"))
    except (TypeError, ValueError, ValidationError) as error:
        raise MemoryValidationError("world settings are invalid") from error


def _tokens(value: object) -> set[str]:
    return {token.casefold() for token in _WORD.findall(canonical_json(value)) if len(token) > 1}


def _batch_id(run_id: str) -> str:
    return "world-batch-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _snapshot_rank(evidence: LessonEvidence) -> tuple[int, tuple[str, ...]]:
    return (
        len(evidence.snapshot.members),
        tuple(
            sorted(f"{item.record_id}:{item.content_hash}" for item in evidence.snapshot.members)
        ),
    )


class WorldModelMemory:
    """Persist and retrieve only independently validated world hypotheses."""

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        namespace: str,
        source: WorldEvidenceSource | None,
        settings: PatternQuerySettings,
        module_version: str = WORLD_MODEL_MODULE_VERSION,
    ) -> None:
        self._store = store
        self._namespace = validate_namespace(namespace)
        if source is not None and not isinstance(source, WorldEvidenceSource):
            raise MemoryValidationError("world_model source must implement capture")
        self._source = source
        self._settings = _owned_settings(settings)
        if not module_version or len(module_version) > 64:
            raise MemoryValidationError("world_model module_version must contain 1-64 characters")
        self._module_version = module_version

    @property
    def settings(self) -> PatternQuerySettings:
        return self._settings.model_copy(deep=True)

    async def finalize(self, outcome: RunOutcome, *, idempotency_key: str) -> None:
        owned_outcome = _owned_outcome(outcome)
        record_id = _batch_id(owned_outcome.run_id)
        existing = await self._store.get(namespace=self._namespace, record_id=record_id)
        if existing is not None:
            batch = self._read_batch(existing)
            await verify_evidence_against_store(self._store, batch.evidence)
            if batch.outcome.model_dump(mode="json") != owned_outcome.model_dump(mode="json"):
                raise MemoryConflictError("world finalization replay has conflicting outcome")
            return
        if self._source is None:
            return
        evidence = await self._source.capture(
            owned_outcome,
            idempotency_key=f"{WORLD_MODEL_MODULE_ID}:{idempotency_key}",
        )
        if evidence is None:
            return
        owned_evidence = await verify_evidence_against_store(self._store, evidence)
        candidates = generate_pattern_candidates(owned_evidence, self._settings)
        previous = await self._previous_hypotheses()
        hypotheses: list[WorldHypothesis] = []
        for candidate in candidates:
            validated = validate_pattern_candidate(candidate, owned_evidence, self._settings)
            prior = previous.get(candidate.candidate_hash)
            version = prior.version + 1 if prior is not None else 1
            hypotheses.append(
                WorldHypothesis.from_validated(
                    validated,
                    version=version,
                    supersedes_id=prior.hypothesis_id if prior is not None else None,
                )
            )
        hypotheses.sort(key=lambda item: item.hypothesis_id)
        settings = _owned_settings(self._settings)
        batch = WorldHypothesisBatch(
            settings=settings,
            settings_hash=sha256_json(settings.model_dump(mode="json")),
            outcome=owned_outcome,
            evidence=owned_evidence,
            input_hash=sha256_json(
                {
                    "snapshot": owned_evidence.snapshot.model_dump(mode="json"),
                    "records": [item.model_dump(mode="json") for item in owned_evidence.records],
                    "runs": [item.model_dump(mode="json") for item in owned_evidence.runs],
                }
            ),
            hypotheses=hypotheses,
        )
        write = RecordWrite(
            namespace=self._namespace,
            record_id=record_id,
            record_type=WORLD_BATCH_RECORD_TYPE,
            payload=batch.model_dump(mode="json"),
            created_at=owned_outcome.finished_at,
        )
        try:
            await self._store.append(
                write,
                operation="finalize-world-model",
                idempotency_key=idempotency_key,
            )
        except MemoryConflictError:
            canonical = await self._store.get(namespace=self._namespace, record_id=record_id)
            if canonical is None:
                raise
            persisted = self._read_batch(canonical)
            if persisted.outcome.model_dump(mode="json") != owned_outcome.model_dump(mode="json"):
                raise
        canonical = await self._store.get(namespace=self._namespace, record_id=record_id)
        if canonical is None:
            raise MemoryPermanentError("world hypothesis batch is missing after append")
        self._read_batch(canonical)

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        batches = await self._read_batches()
        if not batches:
            return MemoryContribution(
                module_id=WORLD_MODEL_MODULE_ID, module_version=self._module_version
            )
        self._require_nested_snapshots(batches)
        batches.sort(key=lambda item: _snapshot_rank(item.evidence))
        batch = batches[-1]
        refreshed: list[WorldHypothesis] = []
        for hypothesis in batch.hypotheses:
            current = validate_pattern_candidate(
                hypothesis.candidate,
                batch.evidence,
                batch.settings,
                decision_record_timestamp=hypothesis.manifest.decision_record_timestamp,
            )
            if current.manifest.model_dump(mode="json") != hypothesis.manifest.model_dump(
                mode="json"
            ):
                raise MemoryPermanentError("stored world validation manifest changed")
            if current.status != hypothesis.status:
                raise MemoryPermanentError("stored world hypothesis status changed")
            refreshed.append(hypothesis)

        excluded = {request.run_id}
        physical = request.context.get("physical_run_id")
        if isinstance(physical, str):
            excluded.add(physical)
        query_tokens = _tokens({"query": request.query, "context": request.context})
        ranked: list[tuple[float, WorldHypothesis, int]] = []
        for hypothesis in refreshed:
            if hypothesis.status != "active":
                continue
            if any(
                (actual := request_scope_value(request.context, path)) is REQUEST_SCOPE_MISSING
                or canonical_json(actual) != canonical_json(expected)
                for path, expected in hypothesis.candidate.scope.items()
            ):
                continue
            support_ids = set(hypothesis.manifest.support_run_ids) | set(
                hypothesis.manifest.support_logical_run_ids
            )
            if excluded & support_ids:
                continue
            item_tokens = _tokens(hypothesis.candidate.model_dump(mode="json"))
            overlap = len(query_tokens & item_tokens)
            if query_tokens and overlap == 0:
                continue
            denominator = max(len(query_tokens) * len(item_tokens), 1)
            ranked.append((overlap / math.sqrt(denominator), hypothesis, overlap))
        ranked.sort(key=lambda item: (-item[0], item[1].hypothesis_id))
        if request.max_items is not None:
            ranked = ranked[: request.max_items]
        return MemoryContribution(
            module_id=WORLD_MODEL_MODULE_ID,
            module_version=self._module_version,
            items=[self._item(hypothesis, score, overlap) for score, hypothesis, overlap in ranked],
        )

    async def _previous_hypotheses(self) -> dict[str, WorldHypothesis]:
        previous: dict[str, WorldHypothesis] = {}
        for batch in await self._read_batches():
            for hypothesis in batch.hypotheses:
                prior = previous.get(hypothesis.candidate.candidate_hash)
                if prior is None or hypothesis.version > prior.version:
                    previous[hypothesis.candidate.candidate_hash] = hypothesis
        return previous

    async def _read_batches(self) -> list[WorldHypothesisBatch]:
        batches: list[WorldHypothesisBatch] = []
        for record in await self._store.list(namespace=self._namespace):
            batch = self._read_batch(record)
            await verify_evidence_against_store(self._store, batch.evidence)
            batches.append(batch)
        return batches

    def _read_batch(self, record: StoredRecord) -> WorldHypothesisBatch:
        try:
            owned = StoredRecord.validate_integrity(record)
            if owned.namespace != self._namespace or owned.record_type != WORLD_BATCH_RECORD_TYPE:
                raise MemoryPermanentError("stored world batch namespace or type is invalid")
            batch = WorldHypothesisBatch.model_validate(owned.payload)
            if owned.record_id != _batch_id(batch.outcome.run_id):
                raise MemoryPermanentError("stored world batch ID mismatch")
            if owned.created_at != batch.outcome.finished_at:
                raise MemoryPermanentError("stored world batch timestamp mismatch")
            if batch.retention_policy_ref != _RETENTION_POLICY_REF:
                raise MemoryPermanentError("stored world retention policy is unsupported")
            if batch.settings_hash != sha256_json(batch.settings.model_dump(mode="json")):
                raise MemoryPermanentError("stored world settings hash mismatch")
            expected_input = sha256_json(
                {
                    "snapshot": batch.evidence.snapshot.model_dump(mode="json"),
                    "records": [item.model_dump(mode="json") for item in batch.evidence.records],
                    "runs": [item.model_dump(mode="json") for item in batch.evidence.runs],
                }
            )
            if batch.input_hash != expected_input:
                raise MemoryPermanentError("stored world input hash mismatch")
            if batch.settings != self._settings:
                raise MemoryPermanentError("stored world settings do not match runtime")
            for hypothesis in batch.hypotheses:
                refreshed = validate_pattern_candidate(
                    hypothesis.candidate,
                    batch.evidence,
                    batch.settings,
                    decision_record_timestamp=hypothesis.manifest.decision_record_timestamp,
                )
                if refreshed.manifest.model_dump(mode="json") != hypothesis.manifest.model_dump(
                    mode="json"
                ):
                    raise MemoryPermanentError("stored world manifest does not match evidence")
                if (
                    refreshed.confidence != hypothesis.confidence
                    or refreshed.confidence_basis != hypothesis.confidence_basis
                    or refreshed.created_at != hypothesis.created_at
                    or refreshed.last_validated_at != hypothesis.last_validated_at
                    or refreshed.provenance != hypothesis.provenance
                    or refreshed.trust_classification != hypothesis.trust_classification
                    or refreshed.status != hypothesis.status
                ):
                    raise MemoryPermanentError("stored world derived fields do not match evidence")
            return batch
        except MemoryPermanentError:
            raise
        except (MemoryValidationError, TypeError, ValueError, ValidationError) as error:
            raise MemoryPermanentError("stored world hypothesis batch is invalid") from error

    @staticmethod
    def _require_nested_snapshots(batches: list[WorldHypothesisBatch]) -> None:
        for batch in batches:
            members = {
                (item.record_id, item.content_hash) for item in batch.evidence.snapshot.members
            }
            for other in batches:
                if other is batch:
                    continue
                if other.evidence.snapshot.namespace != batch.evidence.snapshot.namespace:
                    raise MemoryPermanentError("world batches use different source namespaces")
                other_members = {
                    (item.record_id, item.content_hash) for item in other.evidence.snapshot.members
                }
                if not (members <= other_members or other_members <= members):
                    raise MemoryPermanentError("world snapshots are not nested")

    def _item(self, hypothesis: WorldHypothesis, score: float, overlap: int) -> ContextItem:
        return ContextItem(
            envelope=UntrustedMemoryEnvelope(
                item_id=hypothesis.hypothesis_id,
                artefact_type="world_hypothesis",
                origin_module=WORLD_MODEL_MODULE_ID,
                origin_version=self._module_version,
                trust_classification="derived_untrusted",
                provenance=hypothesis.provenance,
                item={
                    "hypothesis": hypothesis.candidate.model_dump(mode="json"),
                    "confidence": hypothesis.confidence,
                    "confidence_basis": hypothesis.confidence_basis,
                    "status": hypothesis.status,
                    "version": hypothesis.version,
                    "supersedes_id": hypothesis.supersedes_id,
                },
            ),
            score=score,
            selection_reason=f"world_model lexical overlap={overlap}",
            estimated_tokens=0,
        )


__all__ = [
    "WORLD_BATCH_RECORD_TYPE",
    "WORLD_MODEL_MODULE_ID",
    "WORLD_MODEL_MODULE_VERSION",
    "WorldEvidenceSource",
    "WorldHypothesis",
    "WorldHypothesisBatch",
    "WorldModelMemory",
]
