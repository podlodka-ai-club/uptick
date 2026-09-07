"""Bounded, descriptive chains for generic initiated operations.

The extractor indexes an initiation and a later observed completion.  It does
not infer completion from elapsed time or result shape: the caller supplies a
small status resolver for its adapter's public operation response.  All
returned records remain untrusted candidates and retain hashes for every
transition in the bounded interval.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import Field

from uptick_agent.memory.associations import TemporalAssociation
from uptick_agent.memory.candidate_validation import select_observed_learning_transitions
from uptick_agent.memory.contracts import (
    ContractModel,
    ExperienceTransition,
    MemoryValidationError,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.stores.contracts import SnapshotMember, StoredRecord, sha256_json

OperationStatus = Literal["pending", "succeeded", "failed"]
ChainStatus = Literal["missing", "pending", "succeeded", "failed"]
StatusResolver = Callable[[ExperienceTransition, str], str | None]


class OperationChainCandidate(ContractModel):
    """An unpromoted, bounded observation chain for one operation ID."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["operation-chain-candidate"] = "operation-chain-candidate"
    status: Literal["candidate"] = "candidate"
    trust_classification: Literal["derived_untrusted"] = "derived_untrusted"
    causal_credit: Literal[False] = False
    context_identity_verified: Literal[False] = False
    candidate_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    environment_id: str | None = Field(default=None, max_length=256)
    scenario_id: str | None = Field(default=None, max_length=256)
    operation_id: str = Field(min_length=1, max_length=256)
    completion_status: ChainStatus
    initiation_record: SnapshotMember
    completion_record: SnapshotMember | None = None
    interval_records: list[SnapshotMember] = Field(min_length=1)
    iteration_gap: int | None = Field(default=None, ge=0)
    metric_relations: list[TemporalAssociation] = Field(default_factory=list)
    metric_limitations: tuple[str, ...] = ()
    evidence_snapshot_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    limitations: tuple[str, ...] = (
        "operation_chain_not_causal_credit",
        "unverified_context_identity",
        "not_validated_for_decision_use",
    )


def _record_refs(
    transitions: list[ExperienceTransition], records: dict[str, StoredRecord]
) -> list[SnapshotMember]:
    return [
        SnapshotMember(
            record_id=transition.transition_id,
            content_hash=records[transition.transition_id].content_hash,
        )
        for transition in transitions
    ]


def _metric_relations(
    transitions: list[ExperienceTransition],
    records: dict[str, StoredRecord],
    *,
    initiation_index: int,
    completion_index: int,
    operation_id: str,
    max_iteration_gap: int,
    max_metric_boundary_gap: int | None,
    evidence_snapshot_hash: str,
) -> tuple[list[TemporalAssociation], tuple[str, ...]]:
    """Join latest pre-init and first post-completion metric observations.

    The optional boundary applies to each side of the operation separately.
    The complete interval is still attached to every accepted relation.
    """

    latest_before: dict[tuple[str, str], tuple[int, float]] = {}
    for index, transition in enumerate(transitions[:initiation_index]):
        for metric in transition.objective_metrics:
            latest_before[(metric.name, metric.unit)] = (index, metric.value)

    first_after: dict[tuple[str, str], tuple[int, float]] = {}
    for index, transition in enumerate(transitions[completion_index + 1 :], completion_index + 1):
        for metric in transition.objective_metrics:
            first_after.setdefault((metric.name, metric.unit), (index, metric.value))

    relations: list[TemporalAssociation] = []
    limitations: list[str] = []
    for key in sorted(set(latest_before) & set(first_after)):
        before_index, before = latest_before[key]
        after_index, after = first_after[key]
        gap = transitions[after_index].iteration - transitions[before_index].iteration
        if max_metric_boundary_gap is None:
            if gap > max_iteration_gap:
                limitations.append(f"metric_bracket_exceeds_iteration_bound:{key[0]}:{key[1]}")
                continue
        else:
            before_gap = (
                transitions[initiation_index].iteration - transitions[before_index].iteration
            )
            after_gap = transitions[after_index].iteration - transitions[completion_index].iteration
            stale = False
            if before_gap > max_metric_boundary_gap:
                limitations.append(
                    f"metric_before_initiation_exceeds_iteration_bound:{key[0]}:{key[1]}"
                )
                stale = True
            if after_gap > max_metric_boundary_gap:
                limitations.append(
                    f"metric_after_completion_exceeds_iteration_bound:{key[0]}:{key[1]}"
                )
                stale = True
            if stale:
                continue
        interval = _record_refs(transitions[before_index : after_index + 1], records)
        metric_name, metric_unit = key
        overlapping_operation_ids = sorted(
            {
                link.operation_id
                for transition in transitions[before_index : after_index + 1]
                for link in transition.operation_links
                if link.operation_id != operation_id
            }
        )
        limitations.extend(
            f"other_operation_links_in_metric_interval:{overlap_id}"
            for overlap_id in overlapping_operation_ids
        )
        identity = {
            "kind": "temporal-metric-association",
            "schema_version": "1.0",
            "run_id": transitions[initiation_index].run_id,
            "metric_name": metric_name,
            "metric_unit": metric_unit,
            "interval_records": [ref.model_dump() for ref in interval],
        }
        relations.append(
            TemporalAssociation(
                association_id=f"association:{sha256_json(identity)}",
                run_id=transitions[initiation_index].run_id,
                metric_name=metric_name,
                metric_unit=metric_unit,
                before=before,
                after=after,
                observed_from=transitions[before_index].occurred_at,
                observed_until=transitions[after_index].occurred_at,
                iteration_gap=gap,
                interval_records=interval,
                evidence_snapshot_hash=evidence_snapshot_hash,
            )
        )
    for metric_name, metric_unit in sorted(set(latest_before) - set(first_after)):
        limitations.append(f"metric_missing_after:{metric_name}:{metric_unit}")
    for metric_name, metric_unit in sorted(set(first_after) - set(latest_before)):
        limitations.append(f"metric_missing_before:{metric_name}:{metric_unit}")
    return relations, tuple(sorted(set(limitations)))


def _candidate(
    *,
    run_id: str,
    operation_id: str,
    initiation: ExperienceTransition,
    completion: ExperienceTransition | None,
    completion_status: ChainStatus,
    interval: list[ExperienceTransition],
    metric_relations: list[TemporalAssociation],
    metric_limitations: tuple[str, ...],
    records: dict[str, StoredRecord],
    evidence_snapshot_hash: str,
) -> OperationChainCandidate:
    interval_records = _record_refs(interval, records)
    initiation_record = SnapshotMember(
        record_id=initiation.transition_id,
        content_hash=records[initiation.transition_id].content_hash,
    )
    completion_record = (
        SnapshotMember(
            record_id=completion.transition_id,
            content_hash=records[completion.transition_id].content_hash,
        )
        if completion is not None
        else None
    )
    iteration_gap = completion.iteration - initiation.iteration if completion is not None else None
    identity = {
        "kind": "operation-chain-candidate",
        "schema_version": "1.0",
        "run_id": run_id,
        "operation_id": operation_id,
        "completion_status": completion_status,
        "initiation_record": initiation_record.model_dump(),
        "completion_record": completion_record.model_dump() if completion_record else None,
        "interval_records": [ref.model_dump() for ref in interval_records],
        "metric_relations": [relation.model_dump(mode="json") for relation in metric_relations],
        "metric_limitations": list(metric_limitations),
        "evidence_snapshot_hash": evidence_snapshot_hash,
    }
    limitations = [
        "operation_chain_not_causal_credit",
        "unverified_context_identity",
        "not_validated_for_decision_use",
    ]
    if completion_status in ("missing", "pending"):
        limitations.append("completion_not_observed_within_bound")
    if completion_status == "failed":
        limitations.append("operation_reported_failed")
    return OperationChainCandidate(
        candidate_id=f"operation-chain:{sha256_json(identity)}",
        run_id=run_id,
        environment_id=initiation.environment_id,
        scenario_id=initiation.scenario_id,
        operation_id=operation_id,
        completion_status=completion_status,
        initiation_record=initiation_record,
        completion_record=completion_record,
        interval_records=interval_records,
        iteration_gap=iteration_gap,
        metric_relations=metric_relations,
        metric_limitations=metric_limitations,
        evidence_snapshot_hash=evidence_snapshot_hash,
        limitations=tuple(limitations),
    )


def extract_operation_chains(
    evidence: LessonEvidence,
    *,
    learning_cutoffs: dict[str, datetime],
    max_iteration_gap: int,
    resolve_status: StatusResolver,
    max_operation_iteration_gap: int | None = None,
    max_metric_boundary_gap: int | None = None,
) -> list[OperationChainCandidate]:
    """Extract bounded descriptive operation chains from selected evidence.

    ``resolve_status`` is the adapter-owned completion interpretation.  It is
    called only for an ``observed`` link and receives that transition plus the
    opaque operation ID.  ``None`` means no public status was available.

    A run with a context switch or non-increasing observed timestamp is
    rejected as ambiguous.  Multiple actions may share one decision iteration
    when their timestamps strictly increase.  A duplicate initiation for one
    operation is also rejected.  Incomplete chains remain explicit candidates
    with no metric relation; no completion is inferred from a cutoff or
    elapsed iterations.  Missing or over-bound metric brackets are named in
    ``metric_limitations`` rather than inferred from omitted output. When
    ``max_operation_iteration_gap`` is provided, it bounds the full lifetime
    independently of ``max_iteration_gap``. When ``max_metric_boundary_gap``
    is provided, metric freshness is checked separately before initiation and
    after completion; the whole intervening interval remains evidence.
    """

    if max_iteration_gap < 0:
        raise MemoryValidationError("max_iteration_gap must be non-negative")
    if max_operation_iteration_gap is not None and max_operation_iteration_gap < 0:
        raise MemoryValidationError("max_operation_iteration_gap must be non-negative")
    if max_metric_boundary_gap is not None and max_metric_boundary_gap < 0:
        raise MemoryValidationError("max_metric_boundary_gap must be non-negative")
    if not callable(resolve_status):
        raise MemoryValidationError("resolve_status must be callable")
    owned, selected = select_observed_learning_transitions(evidence, learning_cutoffs)
    records = {record.record_id: record for record in owned.records}
    by_run: dict[str, list[ExperienceTransition]] = {}
    for transition in selected:
        by_run.setdefault(transition.run_id, []).append(transition)

    candidates: list[OperationChainCandidate] = []
    for run_id, run_transitions in sorted(by_run.items()):
        transitions = sorted(
            run_transitions, key=lambda item: (item.iteration, item.occurred_at, item.transition_id)
        )
        if any(
            current.occurred_at <= previous.occurred_at
            for previous, current in zip(transitions, transitions[1:], strict=False)
        ):
            continue
        contexts = {
            (transition.environment_id, transition.scenario_id) for transition in transitions
        }
        if len(contexts) != 1:
            continue

        initiations: dict[str, list[int]] = {}
        for index, transition in enumerate(transitions):
            for link in transition.operation_links:
                if link.relation == "initiated":
                    initiations.setdefault(link.operation_id, []).append(index)

        for operation_id, initiation_indices in sorted(initiations.items()):
            if len(initiation_indices) != 1:
                continue
            initiation_index = initiation_indices[0]
            initiation = transitions[initiation_index]
            operation_gap = (
                max_iteration_gap
                if max_operation_iteration_gap is None
                else max_operation_iteration_gap
            )
            bound = initiation.iteration + operation_gap
            bounded_indices = [
                index
                for index, transition in enumerate(transitions[initiation_index:], initiation_index)
                if transition.iteration <= bound
            ]
            if not bounded_indices:
                continue
            bounded_end = bounded_indices[-1]
            completion_index: int | None = None
            completion_status: ChainStatus = "missing"
            saw_pending = False
            for index in bounded_indices:
                transition = transitions[index]
                if not any(
                    link.operation_id == operation_id and link.relation == "observed"
                    for link in transition.operation_links
                ):
                    continue
                status = resolve_status(transition, operation_id)
                if status not in (None, "pending", "succeeded", "failed"):
                    raise MemoryValidationError(
                        f"completion resolver returned unsupported status {status!r}"
                    )
                if status == "pending":
                    saw_pending = True
                elif status in ("succeeded", "failed"):
                    completion_index = index
                    completion_status = status
                    break
            if completion_index is None:
                completion_status = "pending" if saw_pending else "missing"
                interval = transitions[initiation_index : bounded_end + 1]
                candidates.append(
                    _candidate(
                        run_id=run_id,
                        operation_id=operation_id,
                        initiation=initiation,
                        completion=None,
                        completion_status=completion_status,
                        interval=interval,
                        metric_relations=[],
                        metric_limitations=("metric_relations_require_completed_operation",),
                        records=records,
                        evidence_snapshot_hash=owned.snapshot.content_hash,
                    )
                )
                continue

            completion = transitions[completion_index]
            interval = transitions[initiation_index : completion_index + 1]
            relations, metric_limitations = _metric_relations(
                transitions,
                records,
                initiation_index=initiation_index,
                completion_index=completion_index,
                operation_id=operation_id,
                max_iteration_gap=max_iteration_gap,
                max_metric_boundary_gap=max_metric_boundary_gap,
                evidence_snapshot_hash=owned.snapshot.content_hash,
            )
            candidates.append(
                _candidate(
                    run_id=run_id,
                    operation_id=operation_id,
                    initiation=initiation,
                    completion=completion,
                    completion_status=completion_status,
                    interval=interval,
                    metric_relations=relations,
                    metric_limitations=metric_limitations,
                    records=records,
                    evidence_snapshot_hash=owned.snapshot.content_hash,
                )
            )
    return sorted(candidates, key=lambda item: item.candidate_id)


__all__ = [
    "ChainStatus",
    "OperationChainCandidate",
    "OperationStatus",
    "StatusResolver",
    "extract_operation_chains",
]
