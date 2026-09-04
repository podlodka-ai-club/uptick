"""First-class structured episodic memory over the generic store contract."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from uptick_agent.memory.contracts import (
    ContextItem,
    ExperienceTransition,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    MemoryValidationError,
    RunOutcome,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.stores.contracts import (
    RecordWrite,
    StoredRecord,
    StructuredMemoryStore,
    canonical_json,
    validate_namespace,
)
from uptick_agent.redaction import sanitize_json

EPISODIC_MODULE_ID = "episodic"
EPISODIC_MODULE_VERSION = "1.0"
_TRANSITION_RECORD_TYPE = "experience-transition"
_OUTCOME_RECORD_TYPE = "run-outcome"
_WORD = re.compile(r"[\w-]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in _WORD.findall(text) if len(token) > 1}


def _excerpt(value: object, *, limit: int) -> str:
    rendered = canonical_json(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + f"...[{len(rendered) - limit} characters omitted]"


class EpisodicMemory:
    """Persist transitions and retrieve deterministic lexical episode views."""

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        namespace: str,
        module_version: str = EPISODIC_MODULE_VERSION,
    ) -> None:
        self._store = store
        self._namespace = validate_namespace(namespace)
        if not module_version or len(module_version) > 64:
            raise MemoryValidationError("episodic module_version must contain 1-64 characters")
        self._module_version = module_version

    async def record(
        self,
        transition: ExperienceTransition,
        *,
        idempotency_key: str,
    ) -> None:
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
        completed_run_ids: set[str] = set()
        for record in records:
            if record.record_type == _TRANSITION_RECORD_TYPE:
                transitions.append(self._transition_from_record(record))
            elif record.record_type == _OUTCOME_RECORD_TYPE:
                outcome = self._outcome_from_record(record)
                if outcome.status == "completed":
                    completed_run_ids.add(outcome.run_id)
            else:
                raise MemoryPermanentError(
                    f"episodic namespace contains unknown record type {record.record_type!r}"
                )

        query_tokens = _tokens(request.query)
        total = max(len(transitions), 1)
        candidates: list[tuple[float, ExperienceTransition, int]] = []
        for index, transition in enumerate(transitions):
            if transition.run_id != request.run_id and transition.run_id not in completed_run_ids:
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
                self._context_item(transition, score=score, overlap=overlap)
                for score, transition, overlap in candidates
            ],
        )

    def _context_item(
        self,
        transition: ExperienceTransition,
        *,
        score: float,
        overlap: int,
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
