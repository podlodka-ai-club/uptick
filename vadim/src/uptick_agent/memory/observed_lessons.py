"""Opt-in descriptive lessons over verified observed metric-delta counts.

Strict LessonsMemory promotion is unchanged. This view reuses the observed
pattern store/validator and labels every returned item as an unaccepted
association, never as an active recommendation or a causal result.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import Field

from uptick_agent.memory.contracts import (
    ContextItem,
    ContractModel,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    ProvenanceRef,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.patterns import PatternQuerySettings
from uptick_agent.memory.stores.contracts import (
    RecordWrite,
    StructuredMemoryStore,
    sha256_json,
    validate_namespace,
)
from uptick_agent.memory.world_model import WorldModelMemory


class ObservedLessonMetric(ContractModel):
    """Interpret an explicitly configured numeric result as a metric delta."""

    name: str = Field(min_length=1, max_length=128)
    unit: str = Field(min_length=1, max_length=64)
    direction: Literal["minimize", "maximize"]


class ObservedLessonsMemory:
    """Persist/retrieve descriptive candidates without strict promotion."""

    def __init__(
        self,
        store: StructuredMemoryStore,
        *,
        namespace: str,
        settings: PatternQuerySettings,
        metric: ObservedLessonMetric,
        enabled: bool = False,
        allow_current_run_observed: bool = False,
    ) -> None:
        if type(enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        if type(allow_current_run_observed) is not bool:
            raise TypeError("allow_current_run_observed must be a boolean")
        self._store = store
        self._namespace = validate_namespace(namespace)
        self._settings = settings.model_copy(deep=True)
        self._metric = metric.model_copy(deep=True)
        self._enabled = enabled
        self._allow_current_run_observed = allow_current_run_observed
        self._patterns = WorldModelMemory(
            store,
            namespace=f"{namespace}:evidence",
            source=None,
            settings=settings,
            allow_observed_summaries=enabled,
            allow_current_run_observed=allow_current_run_observed,
        )

    def _configuration(self) -> dict:
        return {
            "kind": "observed-lesson-view",
            "schema_version": "1.0",
            "settings": self._settings.model_dump(mode="json"),
            "metric": self._metric.model_dump(mode="json"),
        }

    async def _metadata(self):
        record = await self._store.get(namespace=self._namespace, record_id="view-settings")
        if record is not None and (
            record.record_type != "observed-lesson-view" or record.payload != self._configuration()
        ):
            raise MemoryPermanentError("observed lesson configuration differs from persisted view")
        return record

    async def record_observed(
        self,
        evidence: LessonEvidence,
        learning_cutoffs: Mapping[str, datetime],
        *,
        idempotency_key: str,
    ) -> None:
        if not self._enabled:
            return
        if await self._metadata() is None:
            await self._store.append(
                RecordWrite(
                    namespace=self._namespace,
                    record_id="view-settings",
                    record_type="observed-lesson-view",
                    payload=self._configuration(),
                    created_at=evidence.snapshot.created_at,
                ),
                operation="configure-observed-lessons",
                idempotency_key="view-settings",
            )
        await self._patterns.record_observed(
            evidence, learning_cutoffs, idempotency_key=idempotency_key
        )

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        empty = MemoryContribution(module_id="observed_lessons", module_version="1.0")
        if not self._enabled:
            return empty
        metadata = await self._metadata()
        if metadata is None:
            return empty
        observed = await self._patterns.retrieve(request)
        items = []
        for item in observed.items:
            fact = item.envelope.item
            delta = fact["candidate"]["result_value"]
            if isinstance(delta, bool) or not isinstance(delta, (int, float)):
                raise MemoryPermanentError("observed lesson result must be a numeric metric delta")
            signed = -delta if self._metric.direction == "minimize" else delta
            polarity = "positive" if signed > 0 else "negative" if signed < 0 else "neutral"
            payload = {
                **fact,
                "status": "candidate_unaccepted",
                "active": False,
                "classification": "descriptive_metric_association",
                "causal_credit": False,
                "metric": self._metric.model_dump(mode="json"),
                "metric_delta": delta,
                "metric_polarity": polarity,
                "statement": (
                    f"Observed action {fact['candidate']['action_kind']} with delta "
                    f"{delta} {self._metric.unit} for {self._metric.name}; "
                    f"{polarity} for this metric only. " + fact["statement"]
                ),
                "limitation": (
                    "Not a recommendation; other objectives "
                    "and current preconditions are unverified."
                ),
            }
            envelope = UntrustedMemoryEnvelope(
                item_id="observed-lesson:"
                + sha256_json({"source": item.envelope.item_id, "view": metadata.content_hash}),
                artefact_type="observed_lesson_candidate",
                origin_module="observed_lessons",
                origin_version="1.0",
                trust_classification="derived_untrusted",
                item=payload,
                provenance=[
                    *item.envelope.provenance,
                    ProvenanceRef(
                        artefact_id=metadata.record_id,
                        content_hash=metadata.content_hash,
                        relation="derived_from",
                    ),
                ],
            )
            items.append(
                ContextItem(
                    envelope=envelope,
                    score=item.score,
                    estimated_tokens=0,
                    selection_reason="observed metric lesson; " + item.selection_reason,
                )
            )
        return MemoryContribution(module_id="observed_lessons", module_version="1.0", items=items)
