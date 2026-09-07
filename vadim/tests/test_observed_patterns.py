from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import uptick_agent.memory.observed_patterns as observed_patterns
from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryValidationError,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence, LessonRunDeclaration
from uptick_agent.memory.observed_patterns import (
    generate_observed_pattern_candidates,
    validate_observed_pattern,
    verify_observed_pattern_summaries,
    verify_observed_pattern_summary,
)
from uptick_agent.memory.patterns import validate_pattern_candidate
from uptick_agent.memory.settings import PatternQuerySettings
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    StoredRecord,
)
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_TIME = datetime(2026, 9, 5, 12, tzinfo=UTC)
_ASSEMBLER = DefaultExperienceTransitionAssembler()
_SETTINGS = PatternQuerySettings(
    scope_paths=("observation.state", "pre_state.phase"),
    action_path="action.kind",
    result_path="result.ok",
)


def _transition(
    run_id: str,
    iteration: int = 1,
    *,
    occurred_at: datetime | None = None,
    ok: bool = True,
    action_kind: str = "inspect",
    resource_id: str = "resource-1",
    duration_ms: int = 10,
    result: dict[str, object] | None = None,
    terminal: bool = True,
    state: str = "healthy",
    phase: str = "ready",
) -> ExperienceTransition:
    return _ASSEMBLER.assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}:{iteration}",
            run_id=run_id,
            iteration=iteration,
            occurred_at=occurred_at or (_TIME + timedelta(minutes=iteration)),
            environment_id="environment:shared",
            scenario_id="scenario:shared",
            trust_classification="external_untrusted",
            pre_state={"phase": phase},
            observation={"state": state},
            action={
                "kind": action_kind,
                "resource_id": resource_id,
                "duration_ms": duration_ms,
            },
            result=result if result is not None else {"ok": ok},
            terminal=terminal,
        )
    )


def _declaration(run_id: str, *, phase: str = "learning") -> LessonRunDeclaration:
    environment_id = "environment:shared"
    scenario_id = "scenario:shared"
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


def _record(item: ExperienceTransition | RunOutcome) -> StoredRecord:
    if isinstance(item, RunOutcome):
        record_id = hashlib.sha256(f"run-outcome:{item.run_id}".encode()).hexdigest()
        record_type = "run-outcome"
        created_at = item.finished_at
    else:
        record_id = item.transition_id
        record_type = "experience-transition"
        created_at = item.occurred_at
    return StoredRecord.from_write(
        RecordWrite(
            namespace="lessons",
            record_id=record_id,
            record_type=record_type,
            payload=item.model_dump(mode="json"),
            created_at=created_at,
        )
    )


def _evidence(
    items: list[ExperienceTransition | RunOutcome],
    *,
    runs: list[LessonRunDeclaration] | None = None,
) -> LessonEvidence:
    records = [_record(item) for item in items]
    snapshot = MemorySnapshot.create(
        snapshot_id="snapshot:observed-patterns",
        namespace="lessons",
        members=[
            SnapshotMember(record_id=record.record_id, content_hash=record.content_hash)
            for record in records
        ],
    )
    return LessonEvidence(snapshot=snapshot, records=records, runs=runs or [])


def _cutoffs(*transitions: ExperienceTransition) -> dict[str, datetime]:
    return {transition.run_id: transition.occurred_at for transition in transitions}


def _candidate(evidence: LessonEvidence, *transitions: ExperienceTransition, result: bool = True):
    return next(
        candidate
        for candidate in generate_observed_pattern_candidates(
            evidence, _SETTINGS, learning_cutoffs=_cutoffs(*transitions)
        )
        if candidate.result_value is result
    )


def test_generation_projects_only_selected_action_and_scope_fields() -> None:
    first = _transition("run-a", resource_id="database-a", duration_ms=5)
    second = _transition("run-b", resource_id="database-b", duration_ms=500)
    evidence = _evidence([first, second])

    candidates = generate_observed_pattern_candidates(
        evidence, _SETTINGS, learning_cutoffs=_cutoffs(first, second)
    )

    assert len(candidates) == 1
    assert candidates[0].scope == {
        "observation.state": "healthy",
        "pre_state.phase": "ready",
    }
    assert candidates[0].action_kind == "inspect"
    assert candidates[0].result_path == "result.ok"
    assert candidates[0].result_value is True


def test_validation_counts_false_results_and_interrupted_samples_without_declarations() -> None:
    support = _transition("run-support", resource_id="db-a")
    counter = _transition("run-counter", ok=False, resource_id="db-b")
    interrupted = _transition("run-interrupted", ok=False, resource_id="db-c", terminal=False)
    evidence = _evidence(
        [
            support,
            counter,
            interrupted,
            RunOutcome(
                run_id="run-interrupted",
                status="interrupted",
                finished_at=interrupted.occurred_at,
                stop_reason="stopped by harness",
            ),
        ]
    )
    candidate = _candidate(evidence, support, counter, interrupted)

    summary = validate_observed_pattern(
        candidate,
        evidence,
        _SETTINGS,
        learning_cutoffs=_cutoffs(support, counter, interrupted),
    )

    assert summary.status == "verified_summary"
    assert len(summary.support_records) == 1
    assert len(summary.counter_records) == 2
    assert summary.unknown_result_records == []
    assert summary.observed_run_ids == ("run-counter", "run-interrupted", "run-support")
    assert summary.causal_credit is False
    assert summary.independent_worlds_verified is False
    assert "3 records" in summary.statement
    assert "2 had a different observed result" in summary.statement
    assert "not a causal claim" in summary.statement


def test_validation_counts_missing_result_as_unknown() -> None:
    support = _transition("run-support")
    missing_result = _transition("run-missing", result={"message": "result was not reported"})
    evidence = _evidence([support, missing_result])
    candidate = _candidate(evidence, support, missing_result)

    summary = validate_observed_pattern(
        candidate,
        evidence,
        _SETTINGS,
        learning_cutoffs=_cutoffs(support, missing_result),
    )

    assert len(summary.support_records) == 1
    assert summary.counter_records == []
    assert [ref.record_id for ref in summary.unknown_result_records] == [
        missing_result.transition_id
    ]
    assert verify_observed_pattern_summaries([summary], evidence) == [summary]
    assert "2 records" in summary.statement
    assert "1 lacked that result field" in summary.statement


def test_explicit_cutoff_excludes_future_observations() -> None:
    at_cutoff = _transition("run-cutoff", iteration=1, ok=True)
    future = _transition("run-cutoff", iteration=2, ok=False)
    evidence = _evidence([at_cutoff, future])
    cutoffs = {"run-cutoff": at_cutoff.occurred_at}

    candidates = generate_observed_pattern_candidates(evidence, _SETTINGS, learning_cutoffs=cutoffs)
    summary = validate_observed_pattern(
        candidates[0], evidence, _SETTINGS, learning_cutoffs=cutoffs
    )

    assert len(candidates) == 1
    assert [ref.record_id for ref in summary.selected_records] == [at_cutoff.transition_id]
    assert [ref.record_id for ref in summary.support_records] == [at_cutoff.transition_id]
    assert summary.counter_records == []
    assert summary.unknown_result_records == []


def test_persisted_summary_with_forged_counts_or_statement_is_rejected() -> None:
    support = _transition("run-support")
    counter = _transition("run-counter", ok=False)
    evidence = _evidence([support, counter])
    candidate = _candidate(evidence, support, counter)
    summary = validate_observed_pattern(
        candidate,
        evidence,
        _SETTINGS,
        learning_cutoffs=_cutoffs(support, counter),
    )
    forged = summary.model_copy(
        update={"support_records": [], "statement": "fabricated verified count"}
    )

    with pytest.raises(MemoryValidationError, match="does not match source evidence"):
        verify_observed_pattern_summary(forged, evidence)
    with pytest.raises(MemoryValidationError, match="does not match source evidence"):
        verify_observed_pattern_summaries([forged], evidence)


def test_batch_verification_rejects_a_mutated_selection_cutoff() -> None:
    support = _transition("run-support")
    counter = _transition("run-counter", ok=False, occurred_at=_TIME + timedelta(minutes=3))
    evidence = _evidence([support, counter])
    candidate = _candidate(evidence, support, counter)
    summary = validate_observed_pattern(
        candidate,
        evidence,
        _SETTINGS,
        learning_cutoffs=_cutoffs(support, counter),
    )
    forged = summary.model_copy(
        update={
            "selection_cutoffs": {
                "run-support": support.occurred_at.isoformat(),
                "run-counter": support.occurred_at.isoformat(),
            }
        }
    )

    with pytest.raises(MemoryValidationError, match="does not match source evidence"):
        verify_observed_pattern_summaries([forged], evidence)


def test_batch_verification_selects_once_per_identical_settings_and_cutoffs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _transition("run-support")
    counter = _transition("run-counter", ok=False)
    evidence = _evidence([support, counter])
    cutoffs = _cutoffs(support, counter)
    summaries = [
        validate_observed_pattern(candidate, evidence, _SETTINGS, learning_cutoffs=cutoffs)
        for candidate in generate_observed_pattern_candidates(
            evidence, _SETTINGS, learning_cutoffs=cutoffs
        )
    ]
    original = observed_patterns.select_observed_learning_transitions
    calls = 0

    def counted_selector(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        observed_patterns,
        "select_observed_learning_transitions",
        counted_selector,
    )
    verified = verify_observed_pattern_summaries(summaries, evidence)

    assert verified == summaries
    assert calls == 1


def test_batch_verification_does_not_consult_candidate_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _transition("run-observed")
    evidence = _evidence([observed])
    cutoffs = _cutoffs(observed)
    candidate = _candidate(evidence, observed)
    summary = validate_observed_pattern(
        candidate,
        evidence,
        _SETTINGS,
        learning_cutoffs=cutoffs,
    )

    def forbidden_generator(*args: object, **kwargs: object):
        raise AssertionError("summary verification must not consult the generator")

    monkeypatch.setattr(
        observed_patterns,
        "generate_observed_pattern_candidates",
        forbidden_generator,
    )

    assert verify_observed_pattern_summaries([summary], evidence) == [summary]


def test_supplied_candidate_with_no_support_stays_unsupported() -> None:
    observed = _transition("run-observed", ok=True)
    evidence = _evidence([observed])
    supported_candidate = _candidate(evidence, observed)
    candidate = type(supported_candidate)(
        scope=supported_candidate.scope,
        action_kind=supported_candidate.action_kind,
        result_path=supported_candidate.result_path,
        result_value=False,
        query_settings=supported_candidate.query_settings,
    )

    summary = validate_observed_pattern(
        candidate,
        evidence,
        _SETTINGS,
        learning_cutoffs=_cutoffs(observed),
    )

    assert summary.status == "unsupported"
    assert summary.support_records == []
    assert [ref.record_id for ref in summary.counter_records] == [observed.transition_id]


def test_frozen_evaluation_is_excluded_by_observed_selection() -> None:
    learning = _transition("run-learning", ok=True)
    evaluation = _transition("run-evaluation", ok=False)
    evidence = _evidence(
        [learning, evaluation],
        runs=[
            _declaration("run-learning"),
            _declaration("run-evaluation", phase="frozen_evaluation"),
        ],
    )
    cutoffs = {"run-learning": learning.occurred_at}

    candidates = generate_observed_pattern_candidates(evidence, _SETTINGS, learning_cutoffs=cutoffs)
    summary = validate_observed_pattern(
        candidates[0], evidence, _SETTINGS, learning_cutoffs=cutoffs
    )

    assert [ref.record_id for ref in summary.selected_records] == [learning.transition_id]
    assert summary.counter_records == []
    assert summary.observed_run_ids == ("run-learning",)


def test_strict_pattern_validation_rejects_unknown_candidate_metadata() -> None:
    transition = _transition("run-strict")
    outcome = RunOutcome(
        run_id="run-strict",
        status="completed",
        finished_at=transition.occurred_at,
        stop_reason="done",
    )
    evidence = _evidence([transition, outcome], runs=[_declaration("run-strict")])
    candidate = generate_observed_pattern_candidates(
        evidence, _SETTINGS, learning_cutoffs=_cutoffs(transition)
    )[0]

    class CandidateWithUnknownMetadata:
        def model_dump(self, **kwargs: object) -> dict[str, object]:
            return {
                **candidate.model_dump(**kwargs),
                "metadata": {"source": "untrusted"},
            }

    with pytest.raises(ValidationError):
        validate_pattern_candidate(CandidateWithUnknownMetadata(), evidence, _SETTINGS)  # type: ignore[arg-type]
