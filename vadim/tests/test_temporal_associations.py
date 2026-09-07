from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from uptick_agent.memory.associations import extract_temporal_associations
from uptick_agent.memory.contracts import (
    MemoryValidationError,
    ObjectiveMetric,
    OperationLink,
    ProvenanceRef,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence, LessonRunDeclaration
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    StoredRecord,
)
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_TIME = datetime(2026, 9, 5, 12, tzinfo=UTC)
_ASSEMBLER = DefaultExperienceTransitionAssembler()


def _transition(
    run_id: str,
    iteration: int,
    value: float,
    *,
    occurred_at: datetime | None = None,
    environment_id: str | None = "environment:shared",
    scenario_id: str | None = "scenario:shared",
    action_kind: str = "observe",
    operation_links: list[OperationLink] | None = None,
    include_metric: bool = True,
):
    return _ASSEMBLER.assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}:{iteration}",
            run_id=run_id,
            iteration=iteration,
            occurred_at=occurred_at or (_TIME + timedelta(minutes=iteration)),
            environment_id=environment_id,
            scenario_id=scenario_id,
            trust_classification="external_untrusted",
            observation={"state": "ready"},
            action={"kind": action_kind},
            result={"ok": True},
            before_objective_metrics=(
                [ObjectiveMetric(name="balance", value=0, unit="minor")] if include_metric else []
            ),
            after_objective_metrics=(
                [ObjectiveMetric(name="balance", value=value, unit="minor")]
                if include_metric
                else []
            ),
            operation_links=operation_links or [],
            terminal=False,
        )
    )


def _record(transition) -> StoredRecord:
    return StoredRecord.from_write(
        RecordWrite(
            namespace="lessons",
            record_id=transition.transition_id,
            record_type="experience-transition",
            payload=transition.model_dump(mode="json"),
            created_at=transition.occurred_at,
        )
    )


def _evidence(transitions, *, runs: list[LessonRunDeclaration] | None = None) -> LessonEvidence:
    records = [_record(transition) for transition in transitions]
    snapshot = MemorySnapshot.create(
        snapshot_id="snapshot:temporal-associations",
        namespace="lessons",
        members=[
            SnapshotMember(record_id=record.record_id, content_hash=record.content_hash)
            for record in records
        ],
    )
    return LessonEvidence(snapshot=snapshot, records=records, runs=runs or [])


def _declaration(
    run_id: str,
    *,
    phase: str = "learning",
    environment_id: str = "environment:shared",
    scenario_id: str = "scenario:shared",
) -> LessonRunDeclaration:
    return LessonRunDeclaration(
        run_id=run_id,
        logical_run_id=f"logical:{run_id}",
        phase=phase,
        eligible=True,
        environment_id=environment_id,
        scenario_id=scenario_id,
        environment_content_hash=hashlib.sha256(environment_id.encode()).hexdigest(),
        scenario_content_hash=hashlib.sha256(scenario_id.encode()).hexdigest(),
    )


def _extract(evidence: LessonEvidence, cutoffs: dict[str, datetime], *, gap: int = 3):
    return extract_temporal_associations(
        evidence,
        learning_cutoffs=cutoffs,
        max_iteration_gap=gap,
    )


def test_unknown_context_candidate_keeps_complete_interval_and_is_honest() -> None:
    first = _transition("run-unknown", 1, 10, environment_id=None, scenario_id=None)
    middle = _transition(
        "run-unknown",
        2,
        10,
        environment_id=None,
        scenario_id=None,
        action_kind="deploy",
        include_metric=False,
        operation_links=[
            OperationLink(operation_id="operation-1", relation="initiated"),
            OperationLink(operation_id="operation-2", relation="observed"),
        ],
    )
    last = _transition("run-unknown", 3, 20, environment_id=None, scenario_id=None)
    evidence = _evidence([first, middle, last])

    associations = _extract(evidence, {"run-unknown": last.occurred_at})

    assert len(associations) == 1
    association = associations[0]
    assert association.run_id == "run-unknown"
    assert (association.before, association.after) == (10, 20)
    assert association.observed_from == first.occurred_at
    assert association.observed_until == last.occurred_at
    assert association.iteration_gap == 2
    assert [ref.record_id for ref in association.interval_records] == [
        first.transition_id,
        middle.transition_id,
        last.transition_id,
    ]
    records = {record.record_id: record for record in evidence.records}
    assert all(
        records[ref.record_id].content_hash == ref.content_hash
        for ref in association.interval_records
    )
    assert records[middle.transition_id].payload["action"] == {"kind": "deploy"}
    assert records[middle.transition_id].payload["operation_links"] == [
        OperationLink(operation_id="operation-1", relation="initiated").model_dump(mode="json"),
        OperationLink(operation_id="operation-2", relation="observed").model_dump(mode="json"),
    ]
    assert association.context_identity_verified is False
    assert association.status == "candidate"
    assert association.trust_classification == "derived_untrusted"
    assert association.causal_credit is False
    assert "unverified_context_identity" in association.limitations
    assert "not_validated_for_decision_use" in association.limitations


def test_context_change_breaks_pairing_within_one_run() -> None:
    first = _transition("run-context", 1, 10)
    changed_context = _transition(
        "run-context",
        2,
        20,
        environment_id="environment:other",
        scenario_id="scenario:other",
    )
    final = _transition("run-context", 3, 30)
    evidence = _evidence([first, changed_context, final])

    assert _extract(evidence, {"run-context": final.occurred_at}) == []


def test_only_explicit_cutoff_runs_are_used_even_when_declarations_are_absent() -> None:
    run_a_first = _transition("run-a", 1, 1)
    run_a_last = _transition("run-a", 2, 2)
    run_b_first = _transition("run-b", 1, 100)
    run_b_last = _transition("run-b", 2, 200)
    evidence = _evidence([run_a_first, run_a_last, run_b_first, run_b_last])

    associations = _extract(evidence, {"run-a": run_a_last.occurred_at})

    assert len(associations) == 1
    assert associations[0].run_id == "run-a"


def test_cutoff_excludes_future_observations() -> None:
    first = _transition("run-cutoff", 1, 1)
    at_cutoff = _transition("run-cutoff", 2, 2)
    future = _transition("run-cutoff", 3, 3)
    evidence = _evidence([first, at_cutoff, future])

    associations = _extract(evidence, {"run-cutoff": at_cutoff.occurred_at})

    assert len(associations) == 1
    assert associations[0].after == 2
    assert all(ref.record_id != future.transition_id for ref in associations[0].interval_records)
    assert associations[0].observed_until <= at_cutoff.occurred_at


def test_associations_never_mix_runs() -> None:
    a_first = _transition("run-a", 1, 1)
    a_last = _transition("run-a", 2, 2)
    b_first = _transition("run-b", 1, 10)
    b_last = _transition("run-b", 2, 20)
    evidence = _evidence([a_first, b_first, a_last, b_last])

    associations = _extract(
        evidence,
        {"run-a": a_last.occurred_at, "run-b": b_last.occurred_at},
    )

    assert {association.run_id for association in associations} == {"run-a", "run-b"}
    for association in associations:
        assert all(
            ref.record_id.startswith(f"transition:{association.run_id}:")
            for ref in association.interval_records
        )


def test_frozen_evaluation_run_is_rejected() -> None:
    first = _transition("run-eval", 1, 1)
    last = _transition("run-eval", 2, 2)
    evidence = _evidence(
        [first, last],
        runs=[_declaration("run-eval", phase="frozen_evaluation")],
    )

    with pytest.raises(MemoryValidationError, match="frozen evaluation"):
        _extract(evidence, {"run-eval": last.occurred_at})


def test_corrupted_hash_or_provenance_is_rejected_before_extraction() -> None:
    first = _transition("run-corrupt", 1, 1)
    last = _transition("run-corrupt", 2, 2)
    evidence = _evidence([first, last])
    corrupted_record = evidence.records[0].model_copy(update={"content_hash": "f" * 64})

    with pytest.raises(MemoryValidationError, match="integrity"):
        _extract(
            evidence.model_copy(update={"records": [corrupted_record, evidence.records[1]]}),
            {},
        )

    corrupted_transition = first.model_copy(
        update={
            "provenance": [
                ProvenanceRef(artefact_id="unknown-source", content_hash="a" * 64),
            ]
        }
    )
    corrupted_evidence = _evidence([corrupted_transition, last])
    with pytest.raises(MemoryValidationError, match="provenance"):
        _extract(corrupted_evidence, {"run-corrupt": last.occurred_at})


def test_iteration_gap_limit_excludes_distant_metric_changes() -> None:
    first = _transition("run-gap", 1, 1)
    distant = _transition("run-gap", 4, 2)
    evidence = _evidence([first, distant])

    assert _extract(evidence, {"run-gap": distant.occurred_at}, gap=2) == []
    associations = _extract(evidence, {"run-gap": distant.occurred_at}, gap=3)
    assert len(associations) == 1
    assert associations[0].iteration_gap == 3
