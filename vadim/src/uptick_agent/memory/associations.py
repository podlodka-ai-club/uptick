"""Temporal metric associations from observed experience, never causal lessons.

An association retains the complete observed interval, not a chosen action to
credit. It is an unaccepted proposal and is not a decision-context contribution.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from uptick_agent.memory.candidate_validation import select_observed_learning_transitions
from uptick_agent.memory.contracts import ContractModel, ExperienceTransition, MemoryValidationError
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.stores.contracts import SnapshotMember, sha256_json


class TemporalAssociation(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["temporal-metric-association"] = "temporal-metric-association"
    status: Literal["candidate"] = "candidate"
    trust_classification: Literal["derived_untrusted"] = "derived_untrusted"
    causal_credit: Literal[False] = False
    association_id: str
    run_id: str
    metric_name: str
    metric_unit: str
    before: float = Field(allow_inf_nan=False)
    after: float = Field(allow_inf_nan=False)
    observed_from: datetime
    observed_until: datetime
    iteration_gap: int = Field(ge=0)
    interval_records: list[SnapshotMember] = Field(min_length=2)
    evidence_snapshot_hash: str
    context_identity_verified: Literal[False] = False
    limitations: tuple[str, ...] = (
        "temporal_association_not_causal_credit",
        "unverified_context_identity",
        "not_validated_for_decision_use",
    )


def extract_temporal_associations(
    evidence: LessonEvidence,
    *,
    learning_cutoffs: dict[str, datetime],
    max_iteration_gap: int,
) -> list[TemporalAssociation]:
    """Propose from explicitly selected learning runs through each cutoff.

    The experiment owner supplies run classification; absence is exclusion.
    A frozen-evaluation declaration overrides inclusion and causes rejection.
    Metrics are paired only within one run and unchanged observed context.
    All observed transitions between endpoints remain attached as evidence.
    """

    if max_iteration_gap < 1:
        raise MemoryValidationError("max_iteration_gap must be positive")
    owned, selected = select_observed_learning_transitions(evidence, learning_cutoffs)
    by_run: dict[str, list[ExperienceTransition]] = {}
    records = {record.record_id: record for record in owned.records}
    for transition in selected:
        by_run.setdefault(transition.run_id, []).append(transition)
    result = []
    for run_id, transitions in sorted(by_run.items()):
        transitions.sort(key=lambda t: (t.iteration, t.occurred_at, t.transition_id))
        last: dict[tuple[str, str], tuple[int, float]] = {}
        previous_context = None
        previous_time = None
        for index, transition in enumerate(transitions):
            context = (transition.environment_id, transition.scenario_id)
            if previous_time is not None and transition.occurred_at <= previous_time:
                # No invented ordering across ambiguous or reversed timestamps.
                last.clear()
            if context != previous_context:
                last.clear()
            previous_context, previous_time = context, transition.occurred_at
            for metric in transition.objective_metrics:
                key = (metric.name, metric.unit)
                earlier = last.get(key)
                last[key] = (index, metric.value)
                if earlier is None or earlier[1] == metric.value:
                    continue
                start, before = earlier
                first = transitions[start]
                gap = transition.iteration - first.iteration
                if gap > max_iteration_gap:
                    continue
                interval = [
                    SnapshotMember(
                        record_id=t.transition_id,
                        content_hash=records[t.transition_id].content_hash,
                    )
                    for t in transitions[start : index + 1]
                ]
                identity = {
                    "kind": "temporal-metric-association",
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "metric_name": metric.name,
                    "metric_unit": metric.unit,
                    "interval_records": [ref.model_dump() for ref in interval],
                }
                result.append(
                    TemporalAssociation(
                        association_id=f"association:{sha256_json(identity)}",
                        run_id=run_id,
                        metric_name=metric.name,
                        metric_unit=metric.unit,
                        before=before,
                        after=metric.value,
                        observed_from=first.occurred_at,
                        observed_until=transition.occurred_at,
                        iteration_gap=gap,
                        interval_records=interval,
                        evidence_snapshot_hash=owned.snapshot.content_hash,
                    )
                )
    return sorted(result, key=lambda item: item.association_id)
