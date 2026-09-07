"""First-class structured episodic memory over the generic store contract."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from uptick_agent.memory.contracts import (
    ContextItem,
    CreatedMemoryItem,
    ExperienceTransition,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    MemoryValidationError,
    RunOutcome,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.settings import EPISODIC_RECALL_POLICY, EpisodicRecallSettings
from uptick_agent.memory.stores.contracts import (
    RecordWrite,
    StoredRecord,
    StructuredMemoryStore,
    canonical_json,
    sha256_json,
    validate_namespace,
)
from uptick_agent.redaction import sanitize_json

EPISODIC_MODULE_ID = "episodic"
EPISODIC_MODULE_VERSION = "1.0"
# Version 1.1 is an explicit opt-in for the bounded query-match excerpt below.
# Version 1.2 adds selected-result expansion; other versions remain prefix-only.
EPISODIC_QUERY_EXCERPT_MODULE_VERSION = "1.1"
EPISODIC_SELECTED_EXPANSION_MODULE_VERSION = "1.2"
_TRANSITION_RECORD_TYPE = "experience-transition"
_OUTCOME_RECORD_TYPE = "run-outcome"
_RAW_RECALL_STATUSES = frozenset(("completed", "failed", "interrupted"))
_RAW_RECALL_STOP_REASON_MAX_CHARS = 256
_RAW_RECALL_METRICS_MAX_SERIALIZED_BYTES = 1_024
_WORD = re.compile(r"[\w-]+", re.UNICODE)
_QUERY_MATCH_FIELDS = (("result", 512), ("observation", 384), ("action", 384))
_QUERY_MATCH_MAX_SERIALIZED_BYTES = 600
_QUERY_MATCH_MAX_CONTEXT_CHARS = 80
_QUERY_MATCH_MAX_TOKEN_CHARS = 160


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in _WORD.findall(text) if len(token) > 1}


def _excerpt(value: object, *, limit: int) -> str:
    rendered = canonical_json(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + f"...[{len(rendered) - limit} characters omitted]"


def _bounded_objective_metrics(outcome: RunOutcome) -> tuple[list[dict[str, object]], int]:
    """Keep an ordered, exact metric prefix within the raw-recall byte cap."""

    selected: list[dict[str, object]] = []
    for metric in outcome.objective_metrics:
        candidate = [
            *selected,
            {"name": metric.name, "value": metric.value, "unit": metric.unit},
        ]
        if (
            len(canonical_json(candidate).encode("utf-8"))
            > _RAW_RECALL_METRICS_MAX_SERIALIZED_BYTES
        ):
            break
        selected.append(candidate[-1])
    return selected, len(outcome.objective_metrics) - len(selected)


def _query_match_excerpt(
    query_tokens: set[str],
    *,
    field_name: str,
    value: object,
    ordinary_limit: int,
) -> dict[str, object] | None:
    """Return one bounded exact window for a query token hidden by a prefix.

    Offsets address characters in the same canonical JSON text used by
    ``_excerpt``. The window is evidence from the stored transition only;
    ``complete=False`` prevents it from being mistaken for a full field.
    """

    if not query_tokens:
        return None
    rendered = canonical_json(value)
    visible_tokens = {
        match.group(0).casefold() for match in _WORD.finditer(rendered[:ordinary_limit])
    }
    match = next(
        (
            candidate
            for candidate in _WORD.finditer(rendered)
            if candidate.group(0).casefold() in query_tokens
            and len(candidate.group(0)) <= _QUERY_MATCH_MAX_TOKEN_CHARS
            and candidate.end() > ordinary_limit
            and candidate.group(0).casefold() not in visible_tokens
        ),
        None,
    )
    if match is None:
        return None

    def candidate(radius: int) -> dict[str, object]:
        start = max(0, match.start() - radius)
        end = min(len(rendered), match.end() + radius)
        return {
            "char_start": start,
            "char_end": end,
            "complete": False,
            "source_field": field_name,
            "text": rendered[start:end],
        }

    # The search is over a constant-size window, rather than trimming a large
    # source field one character at a time. Canonical JSON is ASCII, so this
    # also makes the serialized cap deterministic for escaped Unicode/control
    # characters in the source.
    best: dict[str, object] | None = None
    low, high = 0, _QUERY_MATCH_MAX_CONTEXT_CHARS
    while low <= high:
        radius = (low + high) // 2
        possible = candidate(radius)
        encoded_size = len(
            json.dumps(
                possible,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        if encoded_size <= _QUERY_MATCH_MAX_SERIALIZED_BYTES:
            best = possible
            low = radius + 1
        else:
            high = radius - 1
    return best


class EpisodicMemory:
    """Persist transitions and retrieve deterministic lexical episode views.

    ``module_version='1.1'`` explicitly opts into one bounded query-match
    supplement. ``1.2`` additionally permits bounded expansion after selection;
    the default ``1.0`` and other versions retain the prefix-only view.
    """

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        namespace: str,
        module_version: str = EPISODIC_MODULE_VERSION,
        episodic_recall: EpisodicRecallSettings | None = None,
    ) -> None:
        self._store = store
        self._namespace = validate_namespace(namespace)
        if not module_version or len(module_version) > 64:
            raise MemoryValidationError("episodic module_version must contain 1-64 characters")
        if episodic_recall is not None:
            if not isinstance(episodic_recall, EpisodicRecallSettings):
                raise MemoryValidationError("episodic_recall requires EpisodicRecallSettings")
            if episodic_recall.policy_ref != EPISODIC_RECALL_POLICY:
                raise MemoryValidationError("episodic_recall declares an unsupported policy")
        self._module_version = module_version
        self._query_excerpt_enabled = module_version in (
            EPISODIC_QUERY_EXCERPT_MODULE_VERSION,
            EPISODIC_SELECTED_EXPANSION_MODULE_VERSION,
        )
        self._raw_recall_enabled = episodic_recall is not None

    @property
    def context_materialization_enabled(self) -> bool:
        return self._module_version == EPISODIC_SELECTED_EXPANSION_MODULE_VERSION

    async def materialize_selected(
        self,
        items: list[ContextItem],
        request: MemoryContextRequest,
        *,
        max_estimated_tokens: int,
    ) -> list[ContextItem]:
        """Read only selected records; bind the added text to its original result leaf."""
        from uptick_agent.memory.episode_expansion import expand_episode_results

        if not self.context_materialization_enabled:
            return items
        results: dict[str, str] = {}
        for item in items:
            envelope = item.envelope
            record = await self._store.get(namespace=self._namespace, record_id=envelope.item_id)
            if record is None:
                raise MemoryValidationError("selected episode source is unavailable")
            record = StoredRecord.validate_integrity(record)
            transition = self._validate_transition(self._transition_from_record(record))
            view = envelope.item
            # A logical store may expose admitted historical records from other
            # physical namespaces. Its get() boundary owns that mapping.
            identity_matches = (
                record.record_id == transition.transition_id == envelope.item_id
                and record.created_at == transition.occurred_at
                and transition.run_id == view.get("run_id")
                and transition.iteration == view.get("iteration")
                and transition.occurred_at.isoformat() == view.get("occurred_at")
                and transition.trust_classification == envelope.trust_classification
                and transition.provenance == envelope.provenance
            )
            if not identity_matches:
                raise MemoryValidationError("selected episode source identity changed")
            if transition.run_id == request.run_id:
                iteration = request.context.get("iteration")
                if (
                    not isinstance(iteration, int)
                    or isinstance(iteration, bool)
                    or transition.iteration >= iteration
                ):
                    raise MemoryValidationError("selected current episode must precede use")
            result_id = "result:" + sha256_json(
                {
                    "label": "result",
                    "run_id": transition.run_id,
                    "iteration": transition.iteration,
                    "transition_id": transition.transition_id,
                }
            )
            result_hash = sha256_json({"action": transition.action, "result": transition.result})
            if not any(
                ref.artefact_id == result_id
                and ref.relation == "source"
                and ref.content_hash == result_hash
                for ref in envelope.provenance
            ):
                raise MemoryValidationError("selected result does not bind to its source leaf")
            results[envelope.item_id] = canonical_json(transition.result)
        return expand_episode_results(items, results, max_estimated_tokens=max_estimated_tokens)

    async def record(
        self,
        transition: ExperienceTransition,
        *,
        idempotency_key: str,
    ) -> list[CreatedMemoryItem]:
        owned = self._validate_transition(transition)
        await self._store.append(
            RecordWrite(
                namespace=self._namespace,
                record_id=owned.transition_id,
                record_type=_TRANSITION_RECORD_TYPE,
                payload=owned.model_dump(mode="json"),
                created_at=owned.occurred_at,
            ),
            operation="record-transition",
            idempotency_key=idempotency_key,
        )
        stored_record = await self._store.get(
            namespace=self._namespace,
            record_id=owned.transition_id,
        )
        if stored_record is None:
            raise MemoryPermanentError("episodic transition is missing after append")
        stored = self._transition_from_record(StoredRecord.validate_integrity(stored_record))
        return [
            CreatedMemoryItem(
                item_id=stored.transition_id,
                artefact_type="episode",
                provenance=stored.provenance,
            )
        ]

    async def finalize(self, outcome: RunOutcome, *, idempotency_key: str) -> None:
        owned = self._validate_outcome(outcome)
        record_id = hashlib.sha256(f"run-outcome:{owned.run_id}".encode()).hexdigest()
        await self._store.append(
            RecordWrite(
                namespace=self._namespace,
                record_id=record_id,
                record_type=_OUTCOME_RECORD_TYPE,
                payload=owned.model_dump(mode="json"),
                created_at=owned.finished_at,
            ),
            operation="finalize-run",
            idempotency_key=idempotency_key,
        )

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        records = await self._store.list(namespace=self._namespace)
        transitions: list[ExperienceTransition] = []
        run_outcomes: dict[str, RunOutcome] = {}
        for record in records:
            if record.record_type == _TRANSITION_RECORD_TYPE:
                transitions.append(self._transition_from_record(record))
            elif record.record_type == _OUTCOME_RECORD_TYPE:
                outcome = self._outcome_from_record(record)
                run_outcomes[outcome.run_id] = outcome
            else:
                raise MemoryPermanentError(
                    f"episodic namespace contains unknown record type {record.record_type!r}"
                )

        query_tokens = _tokens(request.query)
        total = max(len(transitions), 1)
        candidates: list[tuple[float, ExperienceTransition, int]] = []
        for index, transition in enumerate(transitions):
            source_outcome = run_outcomes.get(transition.run_id)
            if transition.run_id != request.run_id:
                if source_outcome is None:
                    continue
                if self._raw_recall_enabled:
                    if source_outcome.status not in _RAW_RECALL_STATUSES:
                        continue
                elif source_outcome.status != "completed":
                    continue
            transition_tokens = _tokens(canonical_json(transition.model_dump(mode="json")))
            overlap = len(query_tokens & transition_tokens)
            if query_tokens and overlap == 0:
                continue
            lexical = overlap / math.sqrt(max(len(query_tokens) * len(transition_tokens), 1))
            same_run = 1.0 if transition.run_id == request.run_id else 0.0
            recency = (index + 1) / total
            score = lexical * 0.7 + same_run * 0.2 + recency * 0.1
            candidates.append((score, transition, overlap))

        candidates.sort(key=lambda item: (-item[0], item[1].transition_id))
        return MemoryContribution(
            module_id=EPISODIC_MODULE_ID,
            module_version=self._module_version,
            items=[
                self._context_item(
                    transition,
                    score=score,
                    overlap=overlap,
                    query_tokens=query_tokens,
                    source_outcome=run_outcomes.get(transition.run_id),
                )
                for score, transition, overlap in candidates
            ],
        )

    def _context_item(
        self,
        transition: ExperienceTransition,
        *,
        score: float,
        overlap: int,
        query_tokens: set[str] | None = None,
        source_outcome: RunOutcome | None = None,
    ) -> ContextItem:
        view = {
            "run_id": transition.run_id,
            "iteration": transition.iteration,
            "occurred_at": transition.occurred_at.isoformat(),
            "environment_id": transition.environment_id,
            "scenario_id": transition.scenario_id,
            "observation": _excerpt(transition.observation, limit=384),
            "action": _excerpt(transition.action, limit=384),
            "result": _excerpt(transition.result, limit=512),
            "objective_deltas": [
                metric.model_dump(mode="json") for metric in transition.objective_deltas
            ],
            "operation_links": [
                link.model_dump(mode="json") for link in transition.operation_links
            ],
            "terminal": transition.terminal,
        }
        if self._raw_recall_enabled:
            if source_outcome is None:
                # Same-run working memory is intentionally usable before
                # finalization, but it must be visibly distinguished from a
                # finalized outcome at the prompt boundary.
                view["source_run_outcome"] = {
                    "status": "not_finalized",
                    "status_scope": "execution",
                    "terminal": None,
                    "objective_metrics": [],
                    "objective_metrics_omitted_count": 0,
                }
            else:
                metrics, omitted_count = _bounded_objective_metrics(source_outcome)
                stop_reason_truncated = len(source_outcome.stop_reason) > (
                    _RAW_RECALL_STOP_REASON_MAX_CHARS
                )
                view["source_run_outcome"] = {
                    "status": source_outcome.status,
                    "status_scope": "execution",
                    "terminal": source_outcome.terminal,
                    "finished_at": source_outcome.finished_at.isoformat(),
                    "stop_reason": source_outcome.stop_reason[:_RAW_RECALL_STOP_REASON_MAX_CHARS],
                    "objective_metrics": metrics,
                    "objective_metrics_omitted_count": omitted_count,
                }
                if stop_reason_truncated:
                    view["source_run_outcome"]["stop_reason_truncated"] = True
        if self._query_excerpt_enabled and query_tokens is not None:
            for field_name, ordinary_limit in _QUERY_MATCH_FIELDS:
                match_excerpt = _query_match_excerpt(
                    query_tokens,
                    field_name=field_name,
                    value=getattr(transition, field_name),
                    ordinary_limit=ordinary_limit,
                )
                if match_excerpt is not None:
                    view["query_match"] = match_excerpt
                    break
        return ContextItem(
            envelope=UntrustedMemoryEnvelope(
                item_id=transition.transition_id,
                artefact_type="episode",
                origin_module=EPISODIC_MODULE_ID,
                origin_version=self._module_version,
                trust_classification=transition.trust_classification,
                provenance=transition.provenance,
                item=view,
            ),
            score=score,
            selection_reason=f"episodic lexical overlap={overlap}",
            estimated_tokens=0,
        )

    @staticmethod
    def _validate_transition(transition: object) -> ExperienceTransition:
        if not isinstance(transition, ExperienceTransition):
            raise MemoryValidationError("episodic record requires ExperienceTransition")
        try:
            owned = ExperienceTransition.model_validate(
                transition.model_dump(mode="python", round_trip=True, warnings="error")
            )
        except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
            raise MemoryValidationError("experience transition contains invalid data") from error
        if owned.occurred_at.utcoffset() is None:
            raise MemoryValidationError("experience transition timestamp must include a timezone")
        try:
            serialized = owned.model_dump(mode="json")
            if sanitize_json(serialized) != serialized:
                raise MemoryValidationError(
                    "experience transition contains unredacted credential-shaped content"
                )
        except (TypeError, ValueError) as error:
            raise MemoryValidationError(
                "experience transition could not cross the persistence redaction boundary"
            ) from error
        return owned.model_copy(update={"occurred_at": owned.occurred_at.astimezone(UTC)})

    @staticmethod
    def _validate_outcome(outcome: object) -> RunOutcome:
        if not isinstance(outcome, RunOutcome):
            raise MemoryValidationError("episodic finalization requires RunOutcome")
        try:
            owned = RunOutcome.model_validate(
                outcome.model_dump(mode="python", round_trip=True, warnings="error")
            )
        except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
            raise MemoryValidationError("run outcome contains invalid data") from error
        if owned.finished_at.utcoffset() is None:
            raise MemoryValidationError("run outcome timestamp must include a timezone")
        serialized = owned.model_dump(mode="json")
        try:
            safe = sanitize_json(serialized)
        except (TypeError, ValueError) as error:
            raise MemoryValidationError(
                "run outcome could not cross the persistence redaction boundary"
            ) from error
        if not isinstance(safe, dict):
            raise MemoryValidationError("run outcome must remain a JSON object")
        if safe.get("run_id") != serialized["run_id"]:
            raise MemoryValidationError("run outcome ID contains credential-shaped content")
        try:
            owned = RunOutcome.model_validate(safe)
        except (TypeError, ValueError, ValidationError) as error:
            raise MemoryValidationError("redacted run outcome is invalid") from error
        return owned.model_copy(update={"finished_at": owned.finished_at.astimezone(UTC)})

    @staticmethod
    def _transition_from_record(record: StoredRecord) -> ExperienceTransition:
        if record.record_type != _TRANSITION_RECORD_TYPE:
            raise MemoryPermanentError("stored episodic transition has an invalid record type")
        try:
            return ExperienceTransition.model_validate(record.payload)
        except (TypeError, ValueError, ValidationError) as error:
            raise MemoryPermanentError("stored episodic transition is invalid") from error

    @staticmethod
    def _outcome_from_record(record: StoredRecord) -> RunOutcome:
        try:
            return RunOutcome.model_validate(record.payload)
        except (TypeError, ValueError, ValidationError) as error:
            raise MemoryPermanentError("stored episodic outcome is invalid") from error
