"""Deterministic Stage 6 candidate extraction and validation.

This module intentionally operates only on generic memory contracts.  The
validator re-derives search results from the immutable evidence bundle; it does
not accept evidence claims supplied by an extractor or by a candidate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryPermanentError,
    MemoryValidationError,
    ProvenanceRef,
    RunOutcome,
)
from uptick_agent.memory.lesson_contracts import (
    LESSON_QUERY_CONTRACT,
    LESSON_VALIDATION_POLICY,
    LessonCandidate,
    LessonEvidence,
    LessonRunDeclaration,
    LessonSettings,
    LessonValidationManifest,
    ValidatedLesson,
    context_fingerprint,
    context_id,
    declaration_hash,
    snapshot_input_hash,
)
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    StoredRecord,
    canonical_json,
    sha256_json,
)
from uptick_agent.redaction import sanitize_json

_TRANSITION_RECORD_TYPE = "experience-transition"
_OUTCOME_RECORD_TYPE = "run-outcome"


def _invalid(message: str, error: BaseException | None = None) -> MemoryValidationError:
    result = MemoryValidationError(message)
    if error is not None:
        result.__cause__ = error
    return result


def _expected_leaf_refs(transition: ExperienceTransition) -> dict[str, tuple[str, str]]:
    """Recompute the two source leaves emitted by the transition assembler."""

    identity = {
        "run_id": transition.run_id,
        "iteration": transition.iteration,
        "transition_id": transition.transition_id,
    }
    observation_identity = {"label": "observation", **identity}
    result_identity = {"label": "result", **identity}
    return {
        f"observation:{sha256_json(observation_identity)}": (
            sha256_json({"pre_state": transition.pre_state, "observation": transition.observation}),
            "source",
        ),
        f"result:{sha256_json(result_identity)}": (
            sha256_json({"action": transition.action, "result": transition.result}),
            "source",
        ),
    }


def _validate_transition_record(record: StoredRecord) -> ExperienceTransition:
    if record.record_type != _TRANSITION_RECORD_TYPE:
        raise _invalid(f"unknown lesson evidence record type {record.record_type!r}")
    if record.record_id != record.payload.get("transition_id"):
        raise _invalid("transition record ID does not match its payload transition_id")
    try:
        transition = ExperienceTransition.model_validate(record.payload)
    except (TypeError, ValueError, ValidationError) as error:
        raise _invalid("stored experience transition is invalid", error) from error
    if record.created_at.utcoffset() is None or transition.occurred_at.utcoffset() is None:
        raise _invalid("lesson evidence timestamps must include a timezone")
    if record.created_at != transition.occurred_at:
        raise _invalid("transition record timestamp does not match occurred_at")
    try:
        safe = sanitize_json(record.payload)
    except (TypeError, ValueError) as error:
        raise _invalid("lesson evidence contains unsupported transition data", error) from error
    if safe != record.payload:
        raise _invalid("lesson evidence contains credential-shaped transition data")

    # Stage 6 closes provenance against the assembler's exact inline payloads.
    expected = _expected_leaf_refs(transition)
    actual = {
        ref.artefact_id: (ref.content_hash, ref.relation) for ref in transition.provenance
    }
    if len(actual) != len(transition.provenance) or actual != expected:
        raise _invalid("transition provenance is incomplete or does not close to assembler leaves")

    seen_metrics: set[tuple[str, str]] = set()
    for metric in transition.objective_metrics:
        key = (metric.name, metric.unit)
        if key in seen_metrics:
            raise _invalid("transition contains duplicate objective metric observations")
        seen_metrics.add(key)
    seen_deltas: set[tuple[str, str]] = set()
    for delta in transition.objective_deltas:
        key = (delta.name, delta.unit)
        if key in seen_deltas:
            raise _invalid("transition contains duplicate objective metric deltas")
        seen_deltas.add(key)
    return transition


def _expected_outcome_record_id(run_id: str) -> str:
    return hashlib.sha256(f"run-outcome:{run_id}".encode()).hexdigest()


def _validate_outcome_record(record: StoredRecord) -> RunOutcome:
    if record.record_type != _OUTCOME_RECORD_TYPE:
        raise _invalid(f"unknown lesson evidence record type {record.record_type!r}")
    try:
        outcome = RunOutcome.model_validate(record.payload)
    except (TypeError, ValueError, ValidationError) as error:
        raise _invalid("stored run outcome is invalid", error) from error
    if record.record_id != _expected_outcome_record_id(outcome.run_id):
        raise _invalid("run outcome record ID does not match its payload run_id")
    if record.created_at.utcoffset() is None or outcome.finished_at.utcoffset() is None:
        raise _invalid("lesson evidence timestamps must include a timezone")
    if record.created_at != outcome.finished_at:
        raise _invalid("outcome record timestamp does not match finished_at")
    try:
        safe = sanitize_json(record.payload)
    except (TypeError, ValueError) as error:
        raise _invalid("lesson evidence contains unsupported outcome data", error) from error
    if safe != record.payload:
        raise _invalid("lesson evidence contains credential-shaped outcome data")
    return outcome


def _validate_declarations(
    declarations: Iterable[LessonRunDeclaration],
) -> dict[str, LessonRunDeclaration]:
    by_run: dict[str, LessonRunDeclaration] = {}
    logical_attempts: set[tuple[str, int]] = set()
    logical_contexts: dict[str, tuple[str, str, str, str]] = {}
    named_contexts: dict[tuple[str, str], tuple[str, str]] = {}
    environment_hashes: dict[str, str] = {}
    scenario_hashes: dict[str, str] = {}
    for declaration in declarations:
        try:
            safe = sanitize_json(declaration.model_dump(mode="json"))
        except (TypeError, ValueError) as error:
            raise _invalid("lesson run declaration contains unsupported data", error) from error
        if safe != declaration.model_dump(mode="json"):
            raise _invalid("lesson run declaration contains credential-shaped data")
        if declaration.run_id in by_run:
            raise _invalid("lesson run declarations contain a duplicate run_id")
        logical_key = (declaration.logical_run_id, declaration.attempt_index)
        if logical_key in logical_attempts:
            raise _invalid("lesson run declarations contain a duplicate logical attempt")
        logical_attempts.add(logical_key)
        context_fingerprint = (
            declaration.environment_id,
            declaration.scenario_id,
            declaration.environment_content_hash,
            declaration.scenario_content_hash,
        )
        prior_logical_context = logical_contexts.get(declaration.logical_run_id)
        if prior_logical_context is not None and prior_logical_context != context_fingerprint:
            raise _invalid("attempts of one logical run must share an immutable context")
        logical_contexts[declaration.logical_run_id] = context_fingerprint
        named_context = (declaration.environment_id, declaration.scenario_id)
        prior_hashes = named_contexts.get(named_context)
        hashes = (declaration.environment_content_hash, declaration.scenario_content_hash)
        if prior_hashes is not None and prior_hashes != hashes:
            raise _invalid("an environment/scenario context has inconsistent content hashes")
        named_contexts[named_context] = hashes
        prior_environment_hash = environment_hashes.get(declaration.environment_id)
        if (
            prior_environment_hash is not None
            and prior_environment_hash != declaration.environment_content_hash
        ):
            raise _invalid("an environment ID has inconsistent content hashes")
        prior_scenario_hash = scenario_hashes.get(declaration.scenario_id)
        if (
            prior_scenario_hash is not None
            and prior_scenario_hash != declaration.scenario_content_hash
        ):
            raise _invalid("a scenario ID has inconsistent content hashes")
        environment_hashes[declaration.environment_id] = declaration.environment_content_hash
        scenario_hashes[declaration.scenario_id] = declaration.scenario_content_hash
        by_run[declaration.run_id] = declaration
    return by_run


def validate_evidence(evidence: LessonEvidence) -> LessonEvidence:
    """Validate and return an owned evidence bundle suitable for pure search.

    The snapshot member set must exactly equal the supplied verified records.
    Record payloads, IDs, timestamps, declarations, and assembler provenance
    are all checked here so callers can use this function at an input boundary.
    """

    if not isinstance(evidence, LessonEvidence):
        raise _invalid("lesson evidence requires LessonEvidence")
    try:
        owned = LessonEvidence.model_validate(
            evidence.model_dump(mode="python", round_trip=True, warnings="error")
        )
        snapshot = MemorySnapshot.validate_integrity(owned.snapshot)
    except (
        KeyError,
        PydanticSerializationError,
        TypeError,
        ValueError,
        ValidationError,
        MemoryPermanentError,
    ) as error:
        raise _invalid(
            "lesson evidence contract or snapshot integrity is invalid", error
        ) from error
    if snapshot.created_at.utcoffset() is None:
        raise _invalid("lesson evidence snapshot timestamp must include a timezone")
    try:
        snapshot_safe = sanitize_json(snapshot.model_dump(mode="json"))
    except (TypeError, ValueError) as error:
        raise _invalid("lesson evidence snapshot contains unsupported data", error) from error
    if snapshot_safe != snapshot.model_dump(mode="json"):
        raise _invalid("lesson evidence snapshot contains credential-shaped data")

    records: list[StoredRecord] = []
    record_by_id: dict[str, StoredRecord] = {}
    transitions: dict[str, ExperienceTransition] = {}
    outcomes: dict[str, RunOutcome] = {}
    for supplied_record in owned.records:
        try:
            record = StoredRecord.validate_integrity(supplied_record)
        except MemoryPermanentError as error:
            raise _invalid(
                "lesson evidence contains a record with invalid integrity", error
            ) from error
        if record.namespace != snapshot.namespace:
            raise _invalid("lesson evidence record namespace does not match snapshot")
        try:
            serialized_record = record.model_dump(mode="json")
            safe_record = sanitize_json(serialized_record)
        except (TypeError, ValueError) as error:
            raise _invalid("lesson evidence record contains unsupported data", error) from error
        if safe_record != serialized_record:
            raise _invalid("lesson evidence record contains credential-shaped data")
        if record.record_id in record_by_id:
            raise _invalid("lesson evidence contains duplicate record IDs")
        if record.created_at.utcoffset() is None:
            raise _invalid("lesson evidence record timestamp must include a timezone")
        record_by_id[record.record_id] = record
        records.append(record)
        if record.record_type == _TRANSITION_RECORD_TYPE:
            transition = _validate_transition_record(record)
            if transition.transition_id in transitions:
                raise _invalid("lesson evidence contains duplicate transition IDs")
            transitions[transition.transition_id] = transition
        elif record.record_type == _OUTCOME_RECORD_TYPE:
            outcome = _validate_outcome_record(record)
            if outcome.run_id in outcomes:
                raise _invalid("lesson evidence contains duplicate outcome run IDs")
            outcomes[outcome.run_id] = outcome
        else:
            raise _invalid(f"unknown lesson evidence record type {record.record_type!r}")

    members: dict[str, str] = {}
    for member in snapshot.members:
        if member.record_id in members:
            raise _invalid("snapshot contains duplicate member IDs")
        members[member.record_id] = member.content_hash
    if set(members) != set(record_by_id):
        raise _invalid("snapshot members do not exactly match supplied evidence records")
    for record_id, record in record_by_id.items():
        if members[record_id] != record.content_hash:
            raise _invalid("snapshot member hash does not match its stored record")

    declarations = _validate_declarations(owned.runs)
    referenced_runs = {transition.run_id for transition in transitions.values()} | set(outcomes)
    unknown_runs = referenced_runs - set(declarations)
    if unknown_runs:
        raise _invalid("lesson evidence contains records without declaration metadata")
    for transition in transitions.values():
        declaration = declarations[transition.run_id]
        if (
            transition.environment_id != declaration.environment_id
            or transition.scenario_id != declaration.scenario_id
        ):
            raise _invalid("transition context does not match its run declaration")
    return owned.model_copy(update={"snapshot": snapshot, "records": records})


def _owned_settings(settings: LessonSettings) -> LessonSettings:
    if not isinstance(settings, LessonSettings):
        raise _invalid("lesson settings require LessonSettings")
    try:
        return LessonSettings.model_validate(
            settings.model_dump(mode="python", round_trip=True, warnings="error")
        )
    except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
        raise _invalid("lesson settings are invalid", error) from error


def _owned_candidate(candidate: LessonCandidate) -> LessonCandidate:
    if not isinstance(candidate, LessonCandidate):
        raise _invalid("candidate validation requires LessonCandidate")
    try:
        return LessonCandidate.model_validate(
            candidate.model_dump(mode="python", round_trip=True, warnings="error")
        )
    except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
        raise _invalid(
            "lesson candidate is invalid or has inconsistent derived identity", error
        ) from error


def _transition_metric_delta(
    transition: ExperienceTransition, *, metric_name: str, metric_unit: str
) -> float | None:
    matches = [
        delta.delta
        for delta in transition.objective_deltas
        if delta.name == metric_name and delta.unit == metric_unit
    ]
    if len(matches) > 1:
        raise _invalid("transition has an ambiguous configured metric delta")
    return matches[0] if matches else None


def _signed_utility(delta: float, direction: str) -> float:
    return delta if direction == "maximize" else -delta


def _transition_matches(
    transition: ExperienceTransition, candidate: LessonCandidate, settings: LessonSettings
) -> float | None:
    if canonical_json(transition.action) != canonical_json(candidate.action):
        return None
    if any(key not in transition.observation for key in settings.condition_keys):
        return None
    observed_conditions = {
        key: transition.observation[key] for key in settings.condition_keys
    }
    if canonical_json(observed_conditions) != canonical_json(candidate.conditions):
        return None
    delta = _transition_metric_delta(
        transition, metric_name=settings.metric_name, metric_unit=settings.metric_unit
    )
    if delta is None:
        return None
    return _signed_utility(delta, settings.direction)


def _supporting(
    declaration: LessonRunDeclaration,
    outcome: RunOutcome | None,
    signed_utility: float,
    polarity: str,
) -> bool:
    return (
        declaration.phase == "learning"
        and declaration.eligible
        and declaration.attempt_index == 0
        and outcome is not None
        and outcome.status == "completed"
        and (
            (polarity == "positive" and signed_utility > 0)
            or (polarity == "negative" and signed_utility < 0)
        )
    )


def extract_candidates(
    evidence: LessonEvidence, settings: LessonSettings
) -> list[LessonCandidate]:
    """Extract semantic exact-match candidates in deterministic ID order."""

    owned_evidence = validate_evidence(evidence)
    owned_settings = _owned_settings(settings)
    declarations = {declaration.run_id: declaration for declaration in owned_evidence.runs}
    outcomes = {
        record.payload["run_id"]: _validate_outcome_record(record)
        for record in owned_evidence.records
        if record.record_type == _OUTCOME_RECORD_TYPE
    }
    candidates: dict[str, LessonCandidate] = {}
    for record in sorted(owned_evidence.records, key=lambda item: item.record_id):
        if record.record_type != _TRANSITION_RECORD_TYPE:
            continue
        transition = _validate_transition_record(record)
        declaration = declarations[transition.run_id]
        if declaration.phase == "frozen_evaluation":
            continue
        if declaration.phase != "learning":
            raise _invalid("unknown lesson run phase")
        if any(key not in transition.observation for key in owned_settings.condition_keys):
            continue
        delta = _transition_metric_delta(
            transition,
            metric_name=owned_settings.metric_name,
            metric_unit=owned_settings.metric_unit,
        )
        if delta is None:
            continue
        signed = _signed_utility(delta, owned_settings.direction)
        if signed == 0:
            continue
        polarity = "positive" if signed > 0 else "negative"
        if not _supporting(declaration, outcomes.get(declaration.run_id), signed, polarity):
            continue
        candidate = LessonCandidate(
            conditions={key: transition.observation[key] for key in owned_settings.condition_keys},
            action=transition.action,
            metric_name=owned_settings.metric_name,
            metric_unit=owned_settings.metric_unit,
            direction=owned_settings.direction,
            polarity=polarity,
        )
        candidates[candidate.semantic_hash] = candidate
    return [candidates[key] for key in sorted(candidates)]


def _unique_sorted(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _hashes_for(ids: tuple[str, ...], records: dict[str, StoredRecord]) -> tuple[str, ...]:
    return tuple(records[item_id].content_hash for item_id in ids)


def _context_data(
    declarations: Iterable[LessonRunDeclaration],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    by_context: dict[str, tuple[str, str]] = {}
    for declaration in declarations:
        identifier = context_id(
            environment_id=declaration.environment_id, scenario_id=declaration.scenario_id
        )
        by_context.setdefault(
            identifier,
            (
                context_fingerprint(
                    environment_content_hash=declaration.environment_content_hash,
                    scenario_content_hash=declaration.scenario_content_hash,
                ),
                declaration.environment_content_hash,
            ),
        )
    context_ids = tuple(sorted(by_context))
    return (
        context_ids,
        tuple(by_context[item_id][0] for item_id in context_ids),
        _unique_sorted(value[1] for value in by_context.values()),
        _unique_sorted(
            declaration.scenario_content_hash for declaration in declarations
        ),
    )


def validate_candidate(
    candidate: LessonCandidate, evidence: LessonEvidence, settings: LessonSettings
) -> ValidatedLesson:
    """Independently validate one semantic candidate and return its status."""

    owned_evidence = validate_evidence(evidence)
    owned_settings = _owned_settings(settings)
    owned_candidate = _owned_candidate(candidate)
    if owned_settings.policy_ref != LESSON_VALIDATION_POLICY:
        raise _invalid("unsupported lesson validation policy")
    if owned_settings.query_ref != LESSON_QUERY_CONTRACT:
        raise _invalid("unsupported lesson query contract")
    if (
        owned_candidate.metric_name != owned_settings.metric_name
        or owned_candidate.metric_unit != owned_settings.metric_unit
        or owned_candidate.direction != owned_settings.direction
        or set(owned_candidate.conditions) != set(owned_settings.condition_keys)
    ):
        raise _invalid("candidate is not grounded in the configured exact query contract")

    records = {record.record_id: record for record in owned_evidence.records}
    transitions = {
        record.record_id: _validate_transition_record(record)
        for record in owned_evidence.records
        if record.record_type == _TRANSITION_RECORD_TYPE
    }
    outcomes = {
        record.payload["run_id"]: _validate_outcome_record(record)
        for record in owned_evidence.records
        if record.record_type == _OUTCOME_RECORD_TYPE
    }
    declarations = {declaration.run_id: declaration for declaration in owned_evidence.runs}
    searched: list[tuple[str, float]] = []
    support: list[tuple[str, float]] = []
    counter: list[tuple[str, float]] = []
    for record_id in sorted(transitions):
        transition = transitions[record_id]
        declaration = declarations[transition.run_id]
        # Frozen evaluation is excluded before both support and counter search.
        if declaration.phase == "frozen_evaluation":
            continue
        if declaration.phase != "learning":
            raise _invalid("unknown lesson run phase")
        signed = _transition_matches(transition, owned_candidate, owned_settings)
        if signed is None:
            continue
        searched.append((record_id, signed))
        if _supporting(
            declaration,
            outcomes.get(declaration.run_id),
            signed,
            owned_candidate.polarity,
        ):
            support.append((record_id, signed))
        else:
            # This includes retries and failed/interrupted attempts so their
            # existence cannot be hidden from the deterministic audit result.
            counter.append((record_id, signed))

    support_run_ids = _unique_sorted(
        declarations[transitions[item_id].run_id].run_id for item_id, _ in support
    )
    support_logical_run_ids = _unique_sorted(
        declarations[transitions[item_id].run_id].logical_run_id for item_id, _ in support
    )
    support_context_ids = _unique_sorted(
        context_id(
            environment_id=declarations[transitions[item_id].run_id].environment_id,
            scenario_id=declarations[transitions[item_id].run_id].scenario_id,
        )
        for item_id, _ in support
    )
    all_context_ids, all_context_hashes, environment_hashes, scenario_hashes = _context_data(
        owned_evidence.runs
    )
    context_hash_by_id = {
        context_id(
            environment_id=declaration.environment_id,
            scenario_id=declaration.scenario_id,
        ): context_fingerprint(
            environment_content_hash=declaration.environment_content_hash,
            scenario_content_hash=declaration.scenario_content_hash,
        )
        for declaration in owned_evidence.runs
    }
    support_context_hashes = tuple(context_hash_by_id[item_id] for item_id in support_context_ids)
    searched_ids = tuple(item_id for item_id, _ in searched)
    support_ids = tuple(item_id for item_id, _ in support)
    counter_ids = tuple(item_id for item_id, _ in counter)
    contradiction_count = sum(
        signed == 0
        or (owned_candidate.polarity == "positive" and signed < 0)
        or (owned_candidate.polarity == "negative" and signed > 0)
        for _, signed in counter
    )
    all_source_refs: dict[str, ProvenanceRef] = {}
    for transition in transitions.values():
        for ref in transition.provenance:
            all_source_refs[ref.artefact_id] = ref
    source_leaf_ids = tuple(sorted(all_source_refs))
    source_leaf_hashes = tuple(
        all_source_refs[item_id].content_hash for item_id in source_leaf_ids
    )
    lesson_source_refs: dict[str, ProvenanceRef] = {}
    for item_id, _ in searched:
        for ref in transitions[item_id].provenance:
            lesson_source_refs[ref.artefact_id] = ref
    record_ids = tuple(sorted(records))
    outcome_ids = tuple(
        sorted(
            record_id
            for record_id, record in records.items()
            if record.record_type == _OUTCOME_RECORD_TYPE
        )
    )
    declaration_ids = tuple(
        f"declaration:{declaration.run_id}:{declaration.attempt_index}"
        for declaration in sorted(
            owned_evidence.runs, key=lambda item: (item.run_id, item.attempt_index)
        )
    )
    declaration_hashes = tuple(
        declaration_hash(declaration)
        for declaration in sorted(
            owned_evidence.runs, key=lambda item: (item.run_id, item.attempt_index)
        )
    )
    manifest = LessonValidationManifest(
        policy_ref=owned_settings.policy_ref,
        query_ref=owned_settings.query_ref,
        candidate_hash=owned_candidate.semantic_hash,
        input_hash=snapshot_input_hash(owned_evidence),
        snapshot_id=owned_evidence.snapshot.snapshot_id,
        snapshot_content_hash=owned_evidence.snapshot.content_hash,
        record_ids=record_ids,
        record_hashes=tuple(records[item_id].content_hash for item_id in record_ids),
        outcome_record_ids=outcome_ids,
        outcome_record_hashes=tuple(records[item_id].content_hash for item_id in outcome_ids),
        declaration_ids=declaration_ids,
        declaration_hashes=declaration_hashes,
        context_ids=all_context_ids,
        context_hashes=all_context_hashes,
        environment_content_hashes=environment_hashes,
        scenario_content_hashes=scenario_hashes,
        source_leaf_ids=source_leaf_ids,
        source_leaf_hashes=source_leaf_hashes,
        searched_evidence_ids=searched_ids,
        searched_evidence_hashes=_hashes_for(searched_ids, records),
        support_evidence_ids=support_ids,
        support_evidence_hashes=_hashes_for(support_ids, records),
        counter_evidence_ids=counter_ids,
        counter_evidence_hashes=_hashes_for(counter_ids, records),
        support_run_ids=support_run_ids,
        support_logical_run_ids=support_logical_run_ids,
        support_context_ids=support_context_ids,
        support_context_hashes=support_context_hashes,
        grounding_passed=bool(searched),
        polarity_passed=bool(searched) and any(
            (signed > 0 if owned_candidate.polarity == "positive" else signed < 0)
            for _, signed in support
        ),
        provenance_closed=bool(searched),
        counter_search_complete=True,
        support_count=len(support_logical_run_ids),
        context_count=len(set(support_context_hashes)),
        counter_count=len(counter_ids),
        unresolved_contradiction_count=contradiction_count,
        disposition=(
            "disputed"
            if contradiction_count
            else "active"
            if len(support_logical_run_ids) >= 2
            and len(set(support_context_hashes)) >= 2
            else "candidate"
        ),
    )
    utility_values = [signed for _, signed in searched]
    estimated_utility = sum(utility_values) / len(utility_values) if utility_values else 0.0
    confidence = min(1.0, len(support_logical_run_ids) / 2.0)
    if contradiction_count:
        confidence /= 1.0 + contradiction_count
    created_at = (
        min(records[item_id].created_at for item_id, _ in searched)
        if searched
        else owned_evidence.snapshot.created_at
    ).astimezone(UTC)
    last_validated_at = owned_evidence.snapshot.created_at.astimezone(UTC)
    provenance = [lesson_source_refs[item_id] for item_id in sorted(lesson_source_refs)]
    return ValidatedLesson(
        candidate=owned_candidate,
        manifest=manifest,
        confidence=confidence,
        estimated_utility=estimated_utility,
        created_at=created_at,
        last_validated_at=last_validated_at,
        provenance=provenance,
        status=manifest.disposition,
    )


__all__ = ["extract_candidates", "validate_candidate", "validate_evidence"]
