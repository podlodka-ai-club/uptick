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
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, ValidationError, model_validator

from uptick_agent.memory.candidate_validation import (
    validate_observed_evidence,
)
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
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.observed_patterns import (
    ObservedPatternSummary,
    generate_observed_pattern_candidates,
    validate_observed_pattern,
    verify_observed_pattern_summaries,
)
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
OBSERVED_WORLD_BATCH_RECORD_TYPE = "world-observed-summary-batch"
OBSERVED_WORLD_BATCH_SCHEMA_VERSION = "1.0"
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


class ObservedWorldSummaryBatch(ContractModel):
    """Immutable, source-bound descriptive counts from selected experience."""

    schema_version: str = Field(
        default=OBSERVED_WORLD_BATCH_SCHEMA_VERSION, pattern=r"^[1-9][0-9]*\.[0-9]+$"
    )
    retention_policy_ref: str = _RETENTION_POLICY_REF
    retention_class: Literal["project_lifetime"] = "project_lifetime"
    settings: PatternQuerySettings
    settings_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    evidence: LessonEvidence
    selection_cutoffs: dict[str, datetime] = Field(default_factory=dict)
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    summaries: list[ObservedPatternSummary] = Field(default_factory=list)


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


def _owned_learning_cutoffs(value: Mapping[str, datetime]) -> dict[str, datetime]:
    if not isinstance(value, Mapping):
        raise MemoryValidationError("observed learning_cutoffs must be a mapping")
    owned: dict[str, datetime] = {}
    for run_id, cutoff in value.items():
        if not isinstance(run_id, str) or not run_id:
            raise MemoryValidationError(
                "observed learning cutoff run IDs must be non-empty strings"
            )
        if not isinstance(cutoff, datetime) or cutoff.utcoffset() is None:
            raise MemoryValidationError(
                "observed learning cutoffs require timezone-aware timestamps"
            )
        owned[run_id] = cutoff.astimezone(UTC)
    return dict(sorted(owned.items()))


def _tokens(value: object) -> set[str]:
    return {token.casefold() for token in _WORD.findall(canonical_json(value)) if len(token) > 1}


def _batch_id(run_id: str) -> str:
    return "world-batch-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _observed_batch_id(input_hash: str) -> str:
    return "world-observed-batch-" + input_hash


def _observed_input_hash(
    evidence: LessonEvidence,
    settings: PatternQuerySettings,
    selection_cutoffs: Mapping[str, datetime],
) -> str:
    return sha256_json(
        {
            "evidence": evidence.model_dump(mode="json"),
            "settings": settings.model_dump(mode="json"),
            "selection_cutoffs": {
                key: value.isoformat() for key, value in sorted(selection_cutoffs.items())
            },
        }
    )


def _snapshot_rank(evidence: LessonEvidence) -> tuple[int, tuple[str, ...]]:
    return (
        len(evidence.snapshot.members),
        tuple(
            sorted(f"{item.record_id}:{item.content_hash}" for item in evidence.snapshot.members)
        ),
    )


def _observed_selection_members(batch: ObservedWorldSummaryBatch) -> set[tuple[str, str]]:
    """Return selected records from an already validated persisted batch."""

    return {
        (record.record_id, record.content_hash)
        for record in batch.evidence.records
        if record.record_type == "experience-transition"
        and (cutoff := batch.selection_cutoffs.get(record.payload.get("run_id"))) is not None
        and record.created_at <= cutoff
    }


def _observed_batch_rank(
    batch: ObservedWorldSummaryBatch,
    selected: set[tuple[str, str]],
) -> tuple[int, tuple[str, ...], tuple[int, tuple[str, ...]], str]:
    return (
        len(selected),
        tuple(sorted(f"{record_id}:{content_hash}" for record_id, content_hash in selected)),
        _snapshot_rank(batch.evidence),
        batch.input_hash,
    )


def _observed_run_outcome_statuses(
    batch: ObservedWorldSummaryBatch, summary: ObservedPatternSummary
) -> tuple[dict[str, str], dict[str, int]]:
    outcomes = {
        record.payload["run_id"]: RunOutcome.model_validate(record.payload)
        for record in batch.evidence.records
        if record.record_type == "run-outcome"
    }
    statuses: dict[str, str] = {}
    for run_id in summary.observed_run_ids:
        outcome = outcomes.get(run_id)
        cutoff_text = summary.selection_cutoffs.get(run_id)
        if outcome is None or cutoff_text is None:
            statuses[run_id] = "unobserved_at_cutoff"
            continue
        cutoff = datetime.fromisoformat(cutoff_text)
        statuses[run_id] = (
            outcome.status if outcome.finished_at <= cutoff else "unobserved_at_cutoff"
        )
    counts: dict[str, int] = {}
    for status in statuses.values():
        counts[status] = counts.get(status, 0) + 1
    bounded = dict(sorted(statuses.items())[:16])
    return bounded, dict(sorted(counts.items()))


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
        allow_observed_summaries: bool = False,
        allow_current_run_observed: bool = False,
    ) -> None:
        self._store = store
        self._namespace = validate_namespace(namespace)
        if source is not None and not isinstance(source, WorldEvidenceSource):
            raise MemoryValidationError("world_model source must implement capture")
        self._source = source
        self._settings = _owned_settings(settings)
        if not isinstance(allow_observed_summaries, bool):
            raise MemoryValidationError("allow_observed_summaries must be a boolean")
        self._allow_observed_summaries = allow_observed_summaries
        if not isinstance(allow_current_run_observed, bool):
            raise MemoryValidationError("allow_current_run_observed must be a boolean")
        self._allow_current_run_observed = allow_current_run_observed
        # Keep the strict constructor path valid for the full namespace
        # length accepted by the existing world model.  Store calls validate
        # the derived namespace when observed data is explicitly used.
        self._observed_namespace = f"{self._namespace}:observed"
        if not module_version or len(module_version) > 64:
            raise MemoryValidationError("world_model module_version must contain 1-64 characters")
        self._module_version = module_version
        # Only deterministic validation is cached. Authoritative source data and
        # per-request applicability/time boundaries are checked on every read.
        self._observed_validation_cache: dict[tuple[str, str, str], ObservedWorldSummaryBatch] = {}

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

    async def record_observed(
        self,
        evidence: LessonEvidence,
        learning_cutoffs: Mapping[str, datetime],
        *,
        idempotency_key: str,
    ) -> None:
        """Persist selected observed counts without promoting a world hypothesis."""

        if not self._allow_observed_summaries:
            return
        owned_cutoffs = _owned_learning_cutoffs(learning_cutoffs)
        owned_evidence = await self._verify_observed_evidence_against_store(evidence)
        settings = _owned_settings(self._settings)
        candidates = generate_observed_pattern_candidates(
            owned_evidence,
            settings,
            learning_cutoffs=owned_cutoffs,
        )
        summaries = [
            validate_observed_pattern(
                candidate,
                owned_evidence,
                settings,
                learning_cutoffs=owned_cutoffs,
            )
            for candidate in candidates
        ]
        summaries.sort(key=lambda summary: summary.candidate.candidate_hash)
        input_hash = _observed_input_hash(owned_evidence, settings, owned_cutoffs)
        batch = ObservedWorldSummaryBatch(
            settings=settings,
            settings_hash=sha256_json(settings.model_dump(mode="json")),
            evidence=owned_evidence,
            selection_cutoffs=owned_cutoffs,
            input_hash=input_hash,
            summaries=summaries,
        )
        record_id = _observed_batch_id(input_hash)
        existing = await self._store.get(
            namespace=self._observed_namespace,
            record_id=record_id,
        )
        if existing is not None:
            persisted = self._read_observed_batch(existing)
            if persisted.model_dump(mode="json") != batch.model_dump(mode="json"):
                raise MemoryConflictError("observed world batch replay has conflicting input")
            await self._verify_observed_evidence_against_store(persisted.evidence)
            return

        write = RecordWrite(
            namespace=self._observed_namespace,
            record_id=record_id,
            record_type=OBSERVED_WORLD_BATCH_RECORD_TYPE,
            payload=batch.model_dump(mode="json"),
            created_at=owned_evidence.snapshot.created_at,
        )
        try:
            await self._store.append(
                write,
                operation="record-observed-world-model",
                idempotency_key=idempotency_key,
            )
        except MemoryConflictError:
            canonical = await self._store.get(
                namespace=self._observed_namespace,
                record_id=record_id,
            )
            if canonical is None:
                raise
            persisted = self._read_observed_batch(canonical)
            if persisted.model_dump(mode="json") != batch.model_dump(mode="json"):
                raise
        canonical = await self._store.get(
            namespace=self._observed_namespace,
            record_id=record_id,
        )
        if canonical is None:
            raise MemoryPermanentError("observed world batch is missing after append")
        persisted = self._read_observed_batch(canonical)
        await self._verify_observed_evidence_against_store(persisted.evidence)

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        batches = await self._read_batches()
        strict_items: list[ContextItem] = []
        if batches:
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
            strict_items = [
                self._item(hypothesis, score, overlap) for score, hypothesis, overlap in ranked
            ]

        if not self._allow_observed_summaries:
            return MemoryContribution(
                module_id=WORLD_MODEL_MODULE_ID,
                module_version=self._module_version,
                items=strict_items,
            )

        observed_items = await self._retrieve_observed(request)
        items = strict_items + observed_items
        if request.max_items is not None:
            items = items[: request.max_items]
        return MemoryContribution(
            module_id=WORLD_MODEL_MODULE_ID,
            module_version=self._module_version,
            items=items,
        )

    async def _retrieve_observed(self, request: MemoryContextRequest) -> list[ContextItem]:
        stored_batches = await self._read_observed_batches()
        if not stored_batches:
            return []
        self._require_nested_observed_snapshots(stored_batches)
        stored_batches.sort(key=lambda item: _observed_batch_rank(item[1], item[2]))

        boundary = self._current_run_observed_boundary(request)
        excluded = set()
        if not self._allow_current_run_observed or boundary is None:
            excluded.add(request.run_id)
        physical = request.context.get("physical_run_id")
        if isinstance(physical, str):
            excluded.add(physical)
        query_tokens = _tokens({"query": request.query, "context": request.context})

        batch_candidates = [stored_batches[-1]]
        if self._allow_current_run_observed:
            # A newer snapshot can contain a same-run record from after this
            # decision.  Its summaries are filtered below; if all of them are
            # filtered, an older nested batch may still be eligible.
            batch_candidates = list(reversed(stored_batches))
        for batch_record, batch, _selected in batch_candidates:
            ranked: list[tuple[float, ObservedPatternSummary, int]] = []
            for summary in batch.summaries:
                if summary.status != "verified_summary":
                    continue
                if any(
                    (actual := request_scope_value(request.context, path)) is REQUEST_SCOPE_MISSING
                    or canonical_json(actual) != canonical_json(expected)
                    for path, expected in summary.candidate.scope.items()
                ):
                    continue
                if excluded & set(summary.observed_run_ids):
                    continue
                if (
                    self._allow_current_run_observed
                    and request.run_id in summary.observed_run_ids
                    and not self._observed_summary_is_before_boundary(
                        batch, summary, request.run_id, boundary
                    )
                ):
                    continue
                action_tokens = _tokens(summary.candidate.action_kind)
                if action_tokens and not action_tokens <= query_tokens:
                    continue
                item_tokens = _tokens(summary.candidate.model_dump(mode="json"))
                overlap = len(query_tokens & item_tokens)
                if query_tokens and overlap == 0:
                    continue
                denominator = max(len(query_tokens) * len(item_tokens), 1)
                ranked.append((overlap / math.sqrt(denominator), summary, overlap))
            ranked.sort(key=lambda item: (-item[0], item[1].candidate.candidate_hash))
            if request.max_items is not None:
                ranked = ranked[: request.max_items]
            if ranked or not self._allow_current_run_observed:
                return [
                    self._observed_item(batch_record, batch, summary, score, overlap)
                    for score, summary, overlap in ranked
                ]
        return []

    def _current_run_observed_boundary(
        self, request: MemoryContextRequest
    ) -> tuple[datetime, int] | None:
        """Return the trusted caller boundary required for same-run recall."""

        if not self._allow_current_run_observed:
            return None
        cutoff_value = request.context.get("decision_cutoff")
        iteration = request.context.get("iteration")
        if not isinstance(cutoff_value, str):
            return None
        if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 1:
            return None
        try:
            cutoff = datetime.fromisoformat(cutoff_value)
        except (TypeError, ValueError):
            return None
        if cutoff.utcoffset() is None:
            return None
        return cutoff.astimezone(UTC), iteration

    @staticmethod
    def _observed_summary_is_before_boundary(
        batch: ObservedWorldSummaryBatch,
        summary: ObservedPatternSummary,
        current_run_id: str,
        boundary: tuple[datetime, int] | None,
    ) -> bool:
        if boundary is None:
            return False
        cutoff, iteration = boundary
        records = {record.record_id: record for record in batch.evidence.records}
        saw_current_run = False
        for member in summary.selected_records:
            record = records.get(member.record_id)
            if record is None or record.content_hash != member.content_hash:
                raise MemoryPermanentError("observed summary selected record is missing or changed")
            if record.record_type != "experience-transition":
                continue
            try:
                transition = ExperienceTransition.model_validate(record.payload)
            except (TypeError, ValueError, ValidationError) as error:
                raise MemoryPermanentError("observed summary transition is invalid") from error
            if transition.run_id != current_run_id:
                continue
            saw_current_run = True
            if transition.occurred_at.utcoffset() is None:
                return False
            if (
                transition.occurred_at.astimezone(UTC) >= cutoff
                or transition.iteration >= iteration
            ):
                return False
        return saw_current_run

    async def _verify_observed_evidence_against_store(
        self,
        evidence: LessonEvidence,
        *,
        already_validated: bool = False,
        canonical_namespaces: dict[str, dict[str, StoredRecord]] | None = None,
    ) -> LessonEvidence:
        """Validate observed evidence and reread every claimed source member."""

        owned = evidence if already_validated else validate_observed_evidence(evidence)
        authoritative_snapshot = await self._store.get_snapshot(
            snapshot_id=owned.snapshot.snapshot_id
        )
        if authoritative_snapshot is None or authoritative_snapshot.model_dump(
            mode="json"
        ) != owned.snapshot.model_dump(mode="json"):
            raise MemoryPermanentError("observed evidence snapshot changed or is missing")

        supplied = {record.record_id: record for record in owned.records}
        if canonical_namespaces is None:
            canonical_namespaces = {}
        namespace = owned.snapshot.namespace
        canonical_by_id = canonical_namespaces.get(namespace)
        if canonical_by_id is None:
            canonical_by_id = {}
            for current in await self._store.list(namespace=namespace):
                current = StoredRecord.validate_integrity(current)
                if current.namespace != namespace:
                    raise MemoryPermanentError("observed evidence store returned another namespace")
                if current.record_id in canonical_by_id:
                    raise MemoryPermanentError("observed evidence store returned duplicate records")
                canonical_by_id[current.record_id] = current
            canonical_namespaces[namespace] = canonical_by_id

        for member in owned.snapshot.members:
            current = canonical_by_id.get(member.record_id)
            if current is None or current.content_hash != member.content_hash:
                raise MemoryPermanentError("observed evidence member changed or is missing")
            if current.model_dump(mode="json") != supplied[member.record_id].model_dump(
                mode="json"
            ):
                raise MemoryPermanentError("observed evidence differs from the store")
        canonical_records = [supplied[member.record_id] for member in owned.snapshot.members]
        canonical_runs = sorted(
            owned.runs,
            key=lambda run: (run.logical_run_id, run.attempt_index, run.run_id),
        )
        return owned.model_copy(update={"records": canonical_records, "runs": canonical_runs})

    async def _read_observed_batches(
        self,
    ) -> list[tuple[StoredRecord, ObservedWorldSummaryBatch, set[tuple[str, str]]]]:
        batches: list[tuple[StoredRecord, ObservedWorldSummaryBatch, set[tuple[str, str]]]] = []
        canonical_namespaces: dict[str, dict[str, StoredRecord]] = {}
        next_cache: dict[tuple[str, str, str], ObservedWorldSummaryBatch] = {}
        settings_hash = sha256_json(self._settings.model_dump(mode="json"))
        for record in await self._store.list(namespace=self._observed_namespace):
            try:
                record = StoredRecord.validate_integrity(record)
                key = (record.record_id, record.content_hash, settings_hash)
                cached = self._observed_validation_cache.get(key)
                batch = cached if cached is not None else self._read_observed_batch(record)
                evidence = await self._verify_observed_evidence_against_store(
                    batch.evidence,
                    already_validated=cached is not None,
                    canonical_namespaces=canonical_namespaces,
                )
            except MemoryPermanentError:
                raise
            except (MemoryValidationError, TypeError, ValueError, ValidationError) as error:
                raise MemoryPermanentError("stored observed world evidence is invalid") from error
            if evidence.model_dump(mode="json") != batch.evidence.model_dump(mode="json"):
                raise MemoryPermanentError("stored observed evidence changed while reading")
            if cached is None:
                try:
                    refreshed_summaries = verify_observed_pattern_summaries(
                        batch.summaries, evidence
                    )
                except (MemoryValidationError, TypeError, ValueError, ValidationError) as error:
                    raise MemoryPermanentError(
                        "stored observed world summary does not match evidence"
                    ) from error
                for summary, refreshed in zip(batch.summaries, refreshed_summaries, strict=True):
                    if refreshed.model_dump(mode="json") != summary.model_dump(mode="json"):
                        raise MemoryPermanentError("stored observed world summary changed")
            next_cache[key] = batch
            if len(next_cache) > 16:
                del next_cache[next(iter(next_cache))]
            batches.append((record, batch, _observed_selection_members(batch)))
        self._observed_validation_cache = next_cache

        return batches

    def _read_observed_batch(self, record: StoredRecord) -> ObservedWorldSummaryBatch:
        try:
            owned = StoredRecord.validate_integrity(record)
            if (
                owned.namespace != self._observed_namespace
                or owned.record_type != OBSERVED_WORLD_BATCH_RECORD_TYPE
            ):
                raise MemoryPermanentError(
                    "stored observed world batch namespace or type is invalid"
                )
            batch = ObservedWorldSummaryBatch.model_validate(owned.payload)
            if owned.record_id != _observed_batch_id(batch.input_hash):
                raise MemoryPermanentError("stored observed world batch ID mismatch")
            if owned.created_at != batch.evidence.snapshot.created_at:
                raise MemoryPermanentError("stored observed world batch timestamp mismatch")
            if batch.retention_policy_ref != _RETENTION_POLICY_REF:
                raise MemoryPermanentError("stored observed world retention policy is unsupported")
            if batch.settings_hash != sha256_json(batch.settings.model_dump(mode="json")):
                raise MemoryPermanentError("stored observed world settings hash mismatch")
            cutoffs = _owned_learning_cutoffs(batch.selection_cutoffs)
            if cutoffs != batch.selection_cutoffs:
                raise MemoryPermanentError(
                    "stored observed world selection cutoffs are not canonical"
                )
            expected_input = _observed_input_hash(batch.evidence, batch.settings, cutoffs)
            if batch.input_hash != expected_input:
                raise MemoryPermanentError("stored observed world input hash mismatch")
            if batch.settings != self._settings:
                raise MemoryPermanentError("stored observed world settings do not match runtime")
            expected_candidates = generate_observed_pattern_candidates(
                batch.evidence,
                batch.settings,
                learning_cutoffs=cutoffs,
            )
            expected_hashes = {candidate.candidate_hash for candidate in expected_candidates}
            actual_hashes = [summary.candidate.candidate_hash for summary in batch.summaries]
            if (
                len(actual_hashes) != len(set(actual_hashes))
                or set(actual_hashes) != expected_hashes
            ):
                raise MemoryPermanentError(
                    "stored observed world summary catalog does not match evidence"
                )
            expected_selection = {key: value.isoformat() for key, value in sorted(cutoffs.items())}
            if any(summary.selection_cutoffs != expected_selection for summary in batch.summaries):
                raise MemoryPermanentError(
                    "stored observed world summary selection does not match batch"
                )
            return batch
        except MemoryPermanentError:
            raise
        except (MemoryValidationError, TypeError, ValueError, ValidationError) as error:
            raise MemoryPermanentError("stored observed world batch is invalid") from error

    @staticmethod
    def _require_nested_observed_snapshots(
        batches: list[tuple[StoredRecord, ObservedWorldSummaryBatch, set[tuple[str, str]]]],
    ) -> None:
        for _, batch, members in batches:
            for _, other, other_members in batches:
                if other is batch:
                    continue
                if other.evidence.snapshot.namespace != batch.evidence.snapshot.namespace:
                    raise MemoryPermanentError(
                        "observed world batches use different source namespaces"
                    )
                if not (members <= other_members or other_members <= members):
                    raise MemoryPermanentError("observed world selections are not nested")

    def _observed_item(
        self,
        batch_record: StoredRecord,
        batch: ObservedWorldSummaryBatch,
        summary: ObservedPatternSummary,
        score: float,
        overlap: int,
    ) -> ContextItem:
        candidate = {
            "scope": summary.candidate.scope,
            "action_kind": summary.candidate.action_kind,
            "result_path": summary.candidate.result_path,
            "result_value": summary.candidate.result_value,
        }
        provenance = [
            ProvenanceRef(
                artefact_id=batch_record.record_id,
                relation="derived_from",
                content_hash=batch_record.content_hash,
            )
        ]
        provenance.append(
            ProvenanceRef(
                artefact_id=batch.evidence.snapshot.snapshot_id,
                relation="source",
                content_hash=batch.evidence.snapshot.content_hash,
            )
        )
        provenance.extend(
            ProvenanceRef(
                artefact_id=member.record_id,
                relation="supports",
                content_hash=member.content_hash,
            )
            for member in summary.support_records[:4]
        )
        provenance.extend(
            ProvenanceRef(
                artefact_id=member.record_id,
                relation="contradicts",
                content_hash=member.content_hash,
            )
            for member in summary.counter_records[:4]
        )
        item_id = "observed-world-fact:" + sha256_json(
            {
                "snapshot": batch.evidence.snapshot.content_hash,
                "candidate": summary.candidate.candidate_hash,
                "batch": batch.input_hash,
            }
        )
        outcome_statuses, outcome_status_counts = _observed_run_outcome_statuses(batch, summary)
        return ContextItem(
            envelope=UntrustedMemoryEnvelope(
                item_id=item_id,
                artefact_type="observed_world_fact",
                origin_module=WORLD_MODEL_MODULE_ID,
                origin_version=self._module_version,
                trust_classification="derived_untrusted",
                provenance=provenance,
                item={
                    "classification": "descriptive_counts_only",
                    "policy_ref": summary.policy_ref,
                    "claim_scope": summary.claim_scope,
                    "status": summary.status,
                    "statement": summary.statement,
                    "candidate": candidate,
                    "support_count": len(summary.support_records),
                    "counter_count": len(summary.counter_records),
                    "unknown_result_count": len(summary.unknown_result_records),
                    "observed_run_count": len(summary.observed_run_ids),
                    "source_run_outcome_statuses": outcome_statuses,
                    "source_run_outcome_status_counts": outcome_status_counts,
                    "evidence_snapshot_hash": summary.evidence_snapshot_hash,
                    "batch_input_hash": batch.input_hash,
                },
            ),
            score=score,
            selection_reason=(
                f"world_model observed descriptive counts; lexical overlap={overlap}"
            ),
            estimated_tokens=0,
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
    "OBSERVED_WORLD_BATCH_RECORD_TYPE",
    "OBSERVED_WORLD_BATCH_SCHEMA_VERSION",
    "WORLD_BATCH_RECORD_TYPE",
    "WORLD_MODEL_MODULE_ID",
    "WORLD_MODEL_MODULE_VERSION",
    "WorldEvidenceSource",
    "WorldHypothesis",
    "WorldHypothesisBatch",
    "ObservedWorldSummaryBatch",
    "WorldModelMemory",
]
