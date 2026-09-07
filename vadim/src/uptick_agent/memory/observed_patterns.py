"""Descriptive summaries of observed experience, distinct from world promotion.

Reuses pattern semantics/projections, but verifies a narrower claim: counts in
an explicitly selected observed dataset. No independent-world or causal claim
is made. This policy cannot produce an active PatternValidationManifest.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from pydantic import Field

from uptick_agent.memory.candidate_validation import select_observed_learning_transitions
from uptick_agent.memory.contracts import (
    ContractModel,
    ExperienceTransition,
    MemoryValidationError,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.patterns import (
    PATTERN_MISSING,
    PatternCandidate,
    project_dotted,
    project_pattern_transition,
)
from uptick_agent.memory.settings import PatternQuerySettings
from uptick_agent.memory.stores.contracts import SnapshotMember, canonical_json, sha256_json


class ObservedPatternSummary(ContractModel):
    policy_ref: Literal["observed-pattern-summary-v1@1.0"] = "observed-pattern-summary-v1@1.0"
    claim_scope: Literal["selected_recorded_experience_only"] = "selected_recorded_experience_only"
    status: Literal["verified_summary", "unsupported"]
    candidate: PatternCandidate
    selection_cutoffs: dict[str, str]
    evidence_snapshot_hash: str
    selected_records: list[SnapshotMember]
    support_records: list[SnapshotMember]
    counter_records: list[SnapshotMember]
    unknown_result_records: list[SnapshotMember]
    observed_run_ids: tuple[str, ...]
    causal_credit: Literal[False] = False
    independent_worlds_verified: Literal[False] = False
    trust_classification: Literal["derived_untrusted"] = "derived_untrusted"
    statement: str = Field(min_length=1)


def generate_observed_pattern_candidates(
    evidence: LessonEvidence,
    settings: PatternQuerySettings,
    *,
    learning_cutoffs: dict[str, datetime],
) -> list[PatternCandidate]:
    """Project real observations without resource IDs unless explicitly selected."""

    _, transitions = select_observed_learning_transitions(evidence, learning_cutoffs)
    owned_settings = PatternQuerySettings.model_validate(settings.model_dump(mode="json"))
    payload = owned_settings.model_dump(mode="json")
    candidates = {}
    for transition in transitions:
        projected = project_pattern_transition(transition, owned_settings)
        if projected is None:
            continue
        scope, action_kind, result_value = projected
        candidate = PatternCandidate(
            scope=scope,
            action_kind=action_kind,
            result_path=owned_settings.result_path,
            result_value=result_value,
            query_settings=payload,
        )
        candidates[candidate.candidate_hash] = candidate
    return [candidates[key] for key in sorted(candidates)]


def validate_observed_pattern(
    candidate: PatternCandidate,
    evidence: LessonEvidence,
    settings: PatternQuerySettings,
    *,
    learning_cutoffs: dict[str, datetime],
) -> ObservedPatternSummary:
    """Independently rescan support and exceptions; never call the generator."""

    owned, transitions = select_observed_learning_transitions(evidence, learning_cutoffs)
    return _validate_observed_pattern_from_selected(
        candidate,
        owned,
        transitions,
        settings,
        learning_cutoffs,
    )


def _validate_observed_pattern_from_selected(
    candidate: PatternCandidate,
    owned: LessonEvidence,
    transitions: list[ExperienceTransition],
    settings: PatternQuerySettings,
    learning_cutoffs: dict[str, datetime],
) -> ObservedPatternSummary:
    """Validate one candidate against already-owned, selected transitions.

    The caller must obtain ``owned`` and ``transitions`` from
    ``select_observed_learning_transitions``.  This helper still validates the
    candidate and settings and independently rescans every selected transition;
    it only avoids repeating the evidence ownership and cutoff selection work.
    """

    candidate = PatternCandidate.model_validate(candidate.model_dump(mode="json"))
    settings = PatternQuerySettings.model_validate(settings.model_dump(mode="json"))
    if candidate.query_settings != settings.model_dump(mode="json"):
        raise MemoryValidationError("observed candidate query settings mismatch")
    if candidate.result_path != settings.result_path:
        raise MemoryValidationError("observed candidate result projection mismatch")
    records = {record.record_id: record for record in owned.records}
    support, counter, unknown = [], [], []
    observed_runs = set()
    for transition in transitions:
        payload = transition.model_dump(mode="json")
        scope = {path: project_dotted(payload, path) for path in settings.scope_paths}
        action_kind = project_dotted(payload, settings.action_path)
        if (
            any(value is PATTERN_MISSING for value in scope.values())
            or action_kind is PATTERN_MISSING
        ):
            continue
        if canonical_json(scope) != canonical_json(candidate.scope):
            continue
        if canonical_json(action_kind) != canonical_json(candidate.action_kind):
            continue
        observed_runs.add(transition.run_id)
        ref = SnapshotMember(
            record_id=transition.transition_id,
            content_hash=records[transition.transition_id].content_hash,
        )
        result_value = project_dotted(payload, settings.result_path)
        if result_value is PATTERN_MISSING:
            unknown.append(ref)
        elif canonical_json(result_value) == canonical_json(candidate.result_value):
            support.append(ref)
        else:
            counter.append(ref)
    count = len(support) + len(counter) + len(unknown)
    statement = (
        f"In the selected recorded experience, scope {canonical_json(candidate.scope)} "
        f"and action projection {canonical_json(candidate.action_kind)} matched {count} records "
        f"from {len(observed_runs)} recorded runs. Result {candidate.result_path}="
        f"{canonical_json(candidate.result_value)} occurred in {len(support)} records; "
        f"{len(counter)} had a different observed result; {len(unknown)} lacked that result field. "
        "These are dataset counts, "
        "not a causal claim or a guarantee in a new environment. World identity is unverified."
    )
    return ObservedPatternSummary(
        status="verified_summary" if support else "unsupported",
        candidate=candidate,
        selection_cutoffs={
            key: value.isoformat() for key, value in sorted(learning_cutoffs.items())
        },
        evidence_snapshot_hash=owned.snapshot.content_hash,
        selected_records=[
            SnapshotMember(
                record_id=t.transition_id, content_hash=records[t.transition_id].content_hash
            )
            for t in transitions
        ],
        support_records=support,
        counter_records=counter,
        unknown_result_records=unknown,
        observed_run_ids=tuple(sorted(observed_runs)),
        statement=statement,
    )


def verify_observed_pattern_summaries(
    summaries: Iterable[ObservedPatternSummary], evidence: LessonEvidence
) -> list[ObservedPatternSummary]:
    """Reverify persisted summaries, sharing selection work per input group.

    Each summary still receives an independent count scan.  Only the common
    evidence ownership and cutoff selection are shared for summaries with the
    same query settings and normalized cutoffs.
    """

    owned_summaries = [
        ObservedPatternSummary.model_validate(summary.model_dump(mode="json"))
        for summary in summaries
    ]
    if not owned_summaries:
        return []

    groups: dict[
        str,
        tuple[
            PatternQuerySettings,
            dict[str, datetime],
            list[tuple[int, ObservedPatternSummary]],
        ],
    ] = {}
    for index, summary in enumerate(owned_summaries):
        settings = PatternQuerySettings.model_validate(summary.candidate.query_settings)
        cutoffs = {
            key: datetime.fromisoformat(value) for key, value in summary.selection_cutoffs.items()
        }
        group_key = canonical_json(
            {
                "settings": settings.model_dump(mode="json"),
                "selection_cutoffs": {
                    key: value.isoformat() for key, value in sorted(cutoffs.items())
                },
            }
        )
        group = groups.get(group_key)
        if group is None:
            groups[group_key] = (settings, cutoffs, [(index, summary)])
        else:
            group[2].append((index, summary))

    verified: dict[int, ObservedPatternSummary] = {}
    for settings, cutoffs, members in groups.values():
        owned, transitions = select_observed_learning_transitions(evidence, cutoffs)
        for index, summary in members:
            expected = _validate_observed_pattern_from_selected(
                summary.candidate,
                owned,
                transitions,
                settings,
                cutoffs,
            )
            if sha256_json(expected.model_dump(mode="json")) != sha256_json(
                summary.model_dump(mode="json")
            ):
                raise MemoryValidationError(
                    "persisted observed summary does not match source evidence"
                )
            verified[index] = expected
    return [verified[index] for index in range(len(owned_summaries))]


def verify_observed_pattern_summary(
    summary: ObservedPatternSummary, evidence: LessonEvidence
) -> ObservedPatternSummary:
    """Reverify one persisted summary against source observations before use."""

    return verify_observed_pattern_summaries([summary], evidence)[0]
