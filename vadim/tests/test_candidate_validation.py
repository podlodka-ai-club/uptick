from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from uptick_agent.memory.candidate_validation import (
    extract_candidates,
    validate_candidate,
    validate_evidence,
)
from uptick_agent.memory.contracts import (
    MemoryValidationError,
    ObjectiveMetric,
    ProvenanceRef,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.lesson_contracts import (
    LessonEvidence,
    LessonRunDeclaration,
    LessonSettings,
)
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    StoredRecord,
)
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_SETTINGS = LessonSettings(
    metric_name="balance",
    metric_unit="minor",
    direction="maximize",
    condition_keys=("state",),
)
_TIME = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _transition(
    run_id: str,
    *,
    value: float,
    action: str = "inspect",
    condition_value: object = "ready",
    action_payload: dict[str, object] | None = None,
    environment_id: str | None = None,
    scenario_id: str | None = None,
):
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}",
            run_id=run_id,
            iteration=1,
            occurred_at=_TIME,
            environment_id=environment_id or f"environment:{run_id}",
            scenario_id=scenario_id or f"scenario:{run_id}",
            trust_classification="external_untrusted",
            observation={"state": condition_value},
            action=action_payload or {"kind": action},
            result={"ok": True},
            before_objective_metrics=[
                ObjectiveMetric(name="balance", value=0, unit="minor")
            ],
            after_objective_metrics=[
                ObjectiveMetric(name="balance", value=value, unit="minor")
            ],
            terminal=True,
        )
    )


def _declaration(
    run_id: str,
    *,
    logical_run_id: str | None = None,
    attempt_index: int = 0,
    eligible: bool = True,
    phase: str = "learning",
    environment_id: str | None = None,
    scenario_id: str | None = None,
) -> LessonRunDeclaration:
    environment_id = environment_id or f"environment:{run_id}"
    scenario_id = scenario_id or f"scenario:{run_id}"
    return LessonRunDeclaration(
        run_id=run_id,
        logical_run_id=logical_run_id or f"logical:{run_id}",
        attempt_index=attempt_index,
        phase=phase,
        eligible=eligible,
        environment_id=environment_id,
        scenario_id=scenario_id,
        environment_content_hash=hashlib.sha256(environment_id.encode()).hexdigest(),
        scenario_content_hash=hashlib.sha256(scenario_id.encode()).hexdigest(),
    )


def _record(transition_or_outcome):
    if isinstance(transition_or_outcome, RunOutcome):
        record_id = hashlib.sha256(
            f"run-outcome:{transition_or_outcome.run_id}".encode()
        ).hexdigest()
        record_type = "run-outcome"
        payload = transition_or_outcome.model_dump(mode="json")
        created_at = transition_or_outcome.finished_at
    else:
        record_id = transition_or_outcome.transition_id
        record_type = "experience-transition"
        payload = transition_or_outcome.model_dump(mode="json")
        created_at = transition_or_outcome.occurred_at
    return StoredRecord.from_write(
        RecordWrite(
            namespace="lessons",
            record_id=record_id,
            record_type=record_type,
            payload=payload,
            created_at=created_at,
        )
    )


def _evidence(items: list[tuple[object, LessonRunDeclaration]]) -> LessonEvidence:
    records: list[StoredRecord] = []
    declarations: list[LessonRunDeclaration] = []
    for item, declaration in items:
        records.extend([_record(item), _record(RunOutcome(
            run_id=declaration.run_id,
            status="completed",
            finished_at=_TIME,
            stop_reason="done",
        ))])
        declarations.append(declaration)
    snapshot = MemorySnapshot.create(
        snapshot_id="snapshot:lessons",
        namespace="lessons",
        members=[
            SnapshotMember(record_id=record.record_id, content_hash=record.content_hash)
            for record in records
        ],
    )
    return LessonEvidence(snapshot=snapshot, records=records, runs=declarations)


def _positive_candidate(evidence: LessonEvidence):
    candidates = extract_candidates(evidence, _SETTINGS)
    return next(candidate for candidate in candidates if candidate.polarity == "positive")


def test_two_completed_eligible_first_attempts_activate_deterministically() -> None:
    evidence = _evidence(
        [
            (_transition("run-a", value=2), _declaration("run-a")),
            (_transition("run-b", value=3), _declaration("run-b")),
        ]
    )
    candidate = _positive_candidate(evidence)

    first = validate_candidate(candidate, evidence, _SETTINGS)
    second = validate_candidate(candidate, evidence, _SETTINGS)

    assert first == second
    assert first.status == "active"
    assert first.manifest.support_logical_run_ids == ("logical:run-a", "logical:run-b")
    assert first.manifest.context_count == 2
    assert len(set(first.manifest.context_hashes)) == 2
    assert len(set(first.manifest.environment_content_hashes)) == 2
    assert len(set(first.manifest.scenario_content_hashes)) == 2
    assert first.estimated_utility == 2.5
    assert first.trust_classification == "derived_untrusted"


def test_negative_association_is_an_anti_lesson_and_minimize_flips_sign() -> None:
    evidence = _evidence(
        [
            (_transition("run-a", value=-2), _declaration("run-a")),
            (_transition("run-b", value=-3), _declaration("run-b")),
        ]
    )
    candidate = next(
        item for item in extract_candidates(evidence, _SETTINGS) if item.polarity == "negative"
    )
    result = validate_candidate(candidate, evidence, _SETTINGS)
    assert result.status == "active"
    assert result.estimated_utility == -2.5

    minimize = LessonSettings(
        metric_name="balance",
        metric_unit="minor",
        direction="minimize",
        condition_keys=("state",),
    )
    minimize_candidates = extract_candidates(evidence, minimize)
    assert {item.polarity for item in minimize_candidates} == {"positive"}


def test_one_run_retry_and_one_context_do_not_activate() -> None:
    first_transition = _transition("run-a", value=2)
    retry_transition = _transition(
        "run-a-retry",
        value=3,
        environment_id="environment:run-a",
        scenario_id="scenario:run-a",
    )
    evidence = _evidence(
        [
            (first_transition, _declaration("run-a", logical_run_id="logical-a")),
            (
                retry_transition,
                _declaration(
                    "run-a-retry",
                    logical_run_id="logical-a",
                    attempt_index=1,
                    environment_id="environment:run-a",
                    scenario_id="scenario:run-a",
                ),
            ),
        ]
    )
    candidate = _positive_candidate(evidence)
    result = validate_candidate(candidate, evidence, _SETTINGS)
    assert result.status == "candidate"
    assert result.manifest.support_count == 1
    assert result.manifest.support_context_ids == (
        next(iter(result.manifest.support_context_ids)),
    )
    assert result.manifest.counter_evidence_ids == ("transition:run-a-retry",)


def test_frozen_evaluation_is_excluded_from_support_and_counter_search() -> None:
    evidence = _evidence(
        [
            (_transition("run-a", value=2), _declaration("run-a")),
            (
                _transition("eval", value=-100),
                _declaration("eval", phase="frozen_evaluation", eligible=False),
            ),
        ]
    )
    candidate = _positive_candidate(evidence)
    result = validate_candidate(candidate, evidence, _SETTINGS)
    assert result.manifest.searched_evidence_ids == ("transition:run-a",)
    assert "transition:eval" not in result.manifest.counter_evidence_ids
    assert all("eval" not in ref.artefact_id for ref in result.provenance)


def test_opposite_or_zero_counter_makes_candidate_disputed() -> None:
    first = _transition("run-a", value=2)
    second = _transition("run-b", value=3)
    opposite = _transition("run-c", value=-1)
    zero = _transition("run-d", value=0)
    evidence = _evidence(
        [
            (first, _declaration("run-a")),
            (second, _declaration("run-b")),
            (opposite, _declaration("run-c")),
            (zero, _declaration("run-d")),
        ]
    )
    candidate = _positive_candidate(evidence)
    result = validate_candidate(candidate, evidence, _SETTINGS)
    assert result.status == "disputed"
    assert result.manifest.unresolved_contradiction_count == 2
    assert result.manifest.counter_count == 2


def test_evidence_boundary_rejects_tampered_hash_and_hostile_provenance() -> None:
    evidence = _evidence(
        [
            (_transition("run-a", value=2), _declaration("run-a")),
            (_transition("run-b", value=3), _declaration("run-b")),
        ]
    )
    tampered_record = evidence.records[0].model_copy(update={"content_hash": "f" * 64})
    with pytest.raises(MemoryValidationError, match="integrity"):
        validate_evidence(
            evidence.model_copy(update={"records": [tampered_record, *evidence.records[1:]]})
        )

    transition = _transition("run-a", value=2).model_copy(
        update={
            "provenance": [
                ProvenanceRef(artefact_id="unknown", content_hash="a" * 64),
            ]
        }
    )
    hostile = _evidence(
        [
            (transition, _declaration("run-a")),
            (_transition("run-b", value=3), _declaration("run-b")),
        ]
    )
    with pytest.raises(MemoryValidationError, match="provenance"):
        validate_evidence(hostile)


def test_missing_run_declaration_and_unknown_policy_fail_closed() -> None:
    evidence = _evidence(
        [
            (_transition("run-a", value=2), _declaration("run-a")),
            (_transition("run-b", value=3), _declaration("run-b")),
        ]
    )
    with pytest.raises(MemoryValidationError, match="declaration"):
        validate_evidence(evidence.model_copy(update={"runs": evidence.runs[:1]}))
    with pytest.raises(ValueError):
        LessonSettings(
            metric_name="balance",
            metric_unit="minor",
            direction="maximize",
            condition_keys=("state",),
            policy_ref="unknown@9.9",
        )


def test_counter_search_keeps_ineligible_retry_evidence() -> None:
    items = [
        (_transition("run-a", value=2), _declaration("run-a")),
        (_transition("run-b", value=3), _declaration("run-b")),
        (
            _transition(
                "run-a-retry",
                value=-4,
                environment_id="environment:run-a",
                scenario_id="scenario:run-a",
            ),
            _declaration(
                "run-a-retry",
                logical_run_id="logical:run-a",
                attempt_index=1,
                eligible=False,
                environment_id="environment:run-a",
                scenario_id="scenario:run-a",
            ),
        ),
    ]
    evidence = _evidence(items)
    result = validate_candidate(_positive_candidate(evidence), evidence, _SETTINGS)
    assert result.status == "disputed"
    assert result.manifest.counter_evidence_ids == ("transition:run-a-retry",)


def test_context_count_uses_immutable_content_and_binds_transition_metadata() -> None:
    shared_environment_hash = "a" * 64
    shared_scenario_hash = "b" * 64
    first_declaration = _declaration("run-a").model_copy(
        update={
            "environment_content_hash": shared_environment_hash,
            "scenario_content_hash": shared_scenario_hash,
        }
    )
    second_declaration = _declaration("run-b").model_copy(
        update={
            "environment_content_hash": shared_environment_hash,
            "scenario_content_hash": shared_scenario_hash,
        }
    )
    evidence = _evidence(
        [
            (_transition("run-a", value=2), first_declaration),
            (_transition("run-b", value=3), second_declaration),
        ]
    )
    result = validate_candidate(_positive_candidate(evidence), evidence, _SETTINGS)
    assert result.status == "candidate"
    assert result.manifest.support_count == 2
    assert result.manifest.context_count == 1
    assert len(set(result.manifest.support_context_hashes)) == 1

    mismatched_context = _declaration("run-b", environment_id="environment:run-a")
    with pytest.raises(MemoryValidationError, match="environment ID"):
        validate_evidence(
            _evidence(
                [
                    (_transition("run-a", value=2), first_declaration),
                    (_transition("run-b", value=3), mismatched_context),
                ]
            )
        )


@pytest.mark.parametrize(
    "variation",
    ["condition", "action"],
)
def test_exact_json_matching_does_not_merge_boolean_and_numeric_values(variation: str) -> None:
    first_kwargs = {"condition_value": True} if variation == "condition" else {
        "action_payload": {"kind": True}
    }
    second_kwargs = {"condition_value": 1} if variation == "condition" else {
        "action_payload": {"kind": 1}
    }
    evidence = _evidence(
        [
            (_transition("run-a", value=2, **first_kwargs), _declaration("run-a")),
            (_transition("run-b", value=3, **second_kwargs), _declaration("run-b")),
        ]
    )
    candidates = extract_candidates(evidence, _SETTINGS)
    assert len(candidates) == 2
    assert all(
        validate_candidate(item, evidence, _SETTINGS).status == "candidate"
        for item in candidates
    )


def test_exact_json_matching_keeps_canonical_object_key_reordering_equivalent() -> None:
    evidence = _evidence(
        [
            (
                _transition(
                    "run-a",
                    value=2,
                    condition_value={"a": 1, "b": 2},
                    action_payload={"kind": "inspect", "args": {"a": 1, "b": 2}},
                ),
                _declaration("run-a"),
            ),
            (
                _transition(
                    "run-b",
                    value=3,
                    condition_value={"b": 2, "a": 1},
                    action_payload={"args": {"b": 2, "a": 1}, "kind": "inspect"},
                ),
                _declaration("run-b"),
            ),
        ]
    )
    settings = _SETTINGS.model_copy(update={"condition_keys": ("state",)})
    candidates = extract_candidates(evidence, settings)
    assert len(candidates) == 1
    assert validate_candidate(candidates[0], evidence, settings).status == "active"
