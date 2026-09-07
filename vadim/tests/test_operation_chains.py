from __future__ import annotations

from datetime import UTC, datetime, timedelta

from uptick_agent.memory.contracts import (
    ObjectiveMetric,
    OperationLink,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.operation_chains import extract_operation_chains
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    StoredRecord,
)
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_TIME = datetime(2026, 9, 7, 12, tzinfo=UTC)
_ASSEMBLER = DefaultExperienceTransitionAssembler()


def _transition(
    run_id: str,
    iteration: int,
    *,
    operation_links: list[OperationLink] | None = None,
    metrics: list[ObjectiveMetric] | None = None,
    occurred_at: datetime | None = None,
    environment_id: str | None = "environment:one",
    scenario_id: str | None = "scenario:one",
    transition_suffix: str = "",
) -> object:
    values = metrics or []
    return _ASSEMBLER.assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}:{iteration}{transition_suffix}",
            run_id=run_id,
            iteration=iteration,
            occurred_at=occurred_at or (_TIME + timedelta(minutes=iteration)),
            environment_id=environment_id,
            scenario_id=scenario_id,
            trust_classification="external_untrusted",
            observation={"state": "ready"},
            action={"kind": "observe"},
            result={"ok": True},
            before_objective_metrics=values,
            after_objective_metrics=values,
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


def _evidence(transitions) -> LessonEvidence:
    records = [_record(transition) for transition in transitions]
    snapshot = MemorySnapshot.create(
        snapshot_id="snapshot:operation-chains",
        namespace="lessons",
        members=[
            SnapshotMember(record_id=record.record_id, content_hash=record.content_hash)
            for record in records
        ],
    )
    return LessonEvidence(snapshot=snapshot, records=records, runs=[])


def _resolver(statuses: dict[tuple[str, str], str | None]):
    def resolve(transition, operation_id: str) -> str | None:
        return statuses.get((transition.transition_id, operation_id))

    return resolve


def _cutoff(transitions) -> dict[str, datetime]:
    return {transitions[0].run_id: max(transition.occurred_at for transition in transitions)}


def test_complete_chain_retains_hashed_interval_and_metric_brackets() -> None:
    op = "operation-1"
    transitions = [
        _transition(
            "run-complete",
            1,
            metrics=[
                ObjectiveMetric(name="balance", value=10, unit="points"),
                ObjectiveMetric(name="latency", value=5, unit="seconds"),
            ],
        ),
        _transition(
            "run-complete",
            2,
            operation_links=[OperationLink(operation_id=op, relation="initiated")],
        ),
        _transition(
            "run-complete",
            3,
            operation_links=[OperationLink(operation_id=op, relation="observed")],
        ),
        _transition(
            "run-complete",
            4,
            operation_links=[OperationLink(operation_id=op, relation="observed")],
        ),
        _transition(
            "run-complete",
            5,
            metrics=[
                ObjectiveMetric(name="balance", value=12, unit="points"),
                ObjectiveMetric(name="latency", value=3, unit="seconds"),
            ],
        ),
        _transition(
            "run-complete",
            6,
            metrics=[ObjectiveMetric(name="balance", value=14, unit="points")],
        ),
    ]
    records = {transition.transition_id: _record(transition) for transition in transitions}
    statuses = {
        (transitions[2].transition_id, op): "pending",
        (transitions[3].transition_id, op): "succeeded",
    }

    candidates = extract_operation_chains(
        _evidence(transitions),
        learning_cutoffs=_cutoff(transitions),
        max_iteration_gap=4,
        resolve_status=_resolver(statuses),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.completion_status == "succeeded"
    assert candidate.initiation_record.record_id == transitions[1].transition_id
    assert candidate.completion_record.record_id == transitions[3].transition_id
    assert [ref.record_id for ref in candidate.interval_records] == [
        transition.transition_id for transition in transitions[1:4]
    ]
    assert all(
        ref.content_hash == records[ref.record_id].content_hash
        for ref in candidate.interval_records
    )
    assert candidate.iteration_gap == 2
    assert candidate.causal_credit is False
    assert candidate.context_identity_verified is False
    assert {(item.metric_name, item.metric_unit) for item in candidate.metric_relations} == {
        ("balance", "points"),
        ("latency", "seconds"),
    }
    balance = next(item for item in candidate.metric_relations if item.metric_name == "balance")
    assert (balance.before, balance.after) == (10, 12)
    assert [ref.record_id for ref in balance.interval_records] == [
        transition.transition_id for transition in transitions[:5]
    ]


def test_cutoff_excludes_future_completion_and_keeps_explicit_missing_state() -> None:
    op = "operation-future"
    transitions = [
        _transition(
            "run-future",
            1,
            metrics=[ObjectiveMetric(name="balance", value=1, unit="points")],
        ),
        _transition(
            "run-future",
            2,
            operation_links=[OperationLink(operation_id=op, relation="initiated")],
        ),
        _transition("run-future", 3),
        _transition(
            "run-future",
            4,
            operation_links=[OperationLink(operation_id=op, relation="observed")],
        ),
    ]
    statuses = {(transitions[3].transition_id, op): "succeeded"}

    candidates = extract_operation_chains(
        _evidence(transitions),
        learning_cutoffs={"run-future": transitions[2].occurred_at},
        max_iteration_gap=4,
        resolve_status=_resolver(statuses),
    )

    assert len(candidates) == 1
    assert candidates[0].completion_status == "missing"
    assert candidates[0].completion_record is None
    assert [ref.record_id for ref in candidates[0].interval_records] == [
        transitions[1].transition_id,
        transitions[2].transition_id,
    ]
    assert candidates[0].metric_relations == []


def test_same_operation_id_is_scoped_to_each_run() -> None:
    op = "operation-shared"
    transitions = [
        _transition("run-a", 1),
        _transition(
            "run-a", 2, operation_links=[OperationLink(operation_id=op, relation="initiated")]
        ),
        _transition(
            "run-a", 3, operation_links=[OperationLink(operation_id=op, relation="observed")]
        ),
        _transition("run-b", 1),
        _transition(
            "run-b", 2, operation_links=[OperationLink(operation_id=op, relation="initiated")]
        ),
        _transition(
            "run-b", 3, operation_links=[OperationLink(operation_id=op, relation="observed")]
        ),
    ]
    statuses = {
        (transitions[2].transition_id, op): "succeeded",
        (transitions[5].transition_id, op): "failed",
    }

    candidates = extract_operation_chains(
        _evidence(transitions),
        learning_cutoffs={
            "run-a": transitions[2].occurred_at,
            "run-b": transitions[5].occurred_at,
        },
        max_iteration_gap=2,
        resolve_status=_resolver(statuses),
    )

    assert {
        (candidate.run_id, candidate.operation_id, candidate.completion_status)
        for candidate in candidates
    } == {
        ("run-a", op, "succeeded"),
        ("run-b", op, "failed"),
    }


def test_duplicate_initiation_is_rejected() -> None:
    op = "operation-duplicate"
    transitions = [
        _transition(
            "run-duplicate",
            1,
            operation_links=[OperationLink(operation_id=op, relation="initiated")],
        ),
        _transition(
            "run-duplicate",
            2,
            operation_links=[OperationLink(operation_id=op, relation="initiated")],
        ),
        _transition(
            "run-duplicate",
            3,
            operation_links=[OperationLink(operation_id=op, relation="observed")],
        ),
    ]

    assert (
        extract_operation_chains(
            _evidence(transitions),
            learning_cutoffs=_cutoff(transitions),
            max_iteration_gap=3,
            resolve_status=_resolver({(transitions[2].transition_id, op): "succeeded"}),
        )
        == []
    )


def test_same_iteration_actions_with_increasing_time_are_valid() -> None:
    op = "operation-same-iteration"
    transitions = [
        _transition(
            "run-same-iteration",
            4,
            operation_links=[OperationLink(operation_id=op, relation="initiated")],
            occurred_at=_TIME + timedelta(minutes=4),
        ),
        _transition(
            "run-same-iteration",
            4,
            operation_links=[OperationLink(operation_id=op, relation="observed")],
            occurred_at=_TIME + timedelta(minutes=4, seconds=1),
            transition_suffix=":observed",
        ),
    ]

    candidates = extract_operation_chains(
        _evidence(transitions),
        learning_cutoffs=_cutoff(transitions),
        max_iteration_gap=0,
        resolve_status=_resolver({(transitions[1].transition_id, op): "succeeded"}),
    )

    assert len(candidates) == 1
    assert candidates[0].completion_status == "succeeded"
    assert candidates[0].iteration_gap == 0


def test_pending_and_failed_statuses_are_explicit() -> None:
    pending_op = "operation-pending"
    failed_op = "operation-failed"
    transitions = [
        _transition(
            "run-pending",
            1,
            operation_links=[OperationLink(operation_id=pending_op, relation="initiated")],
        ),
        _transition(
            "run-pending",
            2,
            operation_links=[OperationLink(operation_id=pending_op, relation="observed")],
        ),
        _transition(
            "run-failed",
            1,
            operation_links=[OperationLink(operation_id=failed_op, relation="initiated")],
        ),
        _transition(
            "run-failed",
            2,
            operation_links=[OperationLink(operation_id=failed_op, relation="observed")],
        ),
    ]
    statuses = {
        (transitions[1].transition_id, pending_op): "pending",
        (transitions[3].transition_id, failed_op): "failed",
    }

    candidates = extract_operation_chains(
        _evidence(transitions),
        learning_cutoffs={
            "run-pending": transitions[1].occurred_at,
            "run-failed": transitions[3].occurred_at,
        },
        max_iteration_gap=1,
        resolve_status=_resolver(statuses),
    )

    by_status = {candidate.completion_status: candidate for candidate in candidates}
    assert by_status["pending"].completion_record is None
    assert by_status["pending"].metric_relations == []
    assert by_status["pending"].metric_limitations == (
        "metric_relations_require_completed_operation",
    )
    assert by_status["failed"].completion_record is not None
    assert "operation_reported_failed" in by_status["failed"].limitations


def test_context_switch_or_nonmonotonic_time_rejects_chain() -> None:
    op = "operation-invalid-context"
    changed_context = [
        _transition(
            "run-context",
            1,
            operation_links=[OperationLink(operation_id=op, relation="initiated")],
        ),
        _transition(
            "run-context",
            2,
            operation_links=[OperationLink(operation_id=op, relation="observed")],
            environment_id="environment:other",
            scenario_id="scenario:other",
        ),
    ]
    reversed_time = [
        _transition(
            "run-time",
            1,
            operation_links=[OperationLink(operation_id=op, relation="initiated")],
            occurred_at=_TIME + timedelta(minutes=2),
        ),
        _transition(
            "run-time",
            2,
            operation_links=[OperationLink(operation_id=op, relation="observed")],
            occurred_at=_TIME + timedelta(minutes=1),
        ),
    ]
    evidence = _evidence(changed_context + reversed_time)
    statuses = {
        (changed_context[1].transition_id, op): "succeeded",
        (reversed_time[1].transition_id, op): "succeeded",
    }

    candidates = extract_operation_chains(
        evidence,
        learning_cutoffs={
            "run-context": changed_context[1].occurred_at,
            "run-time": reversed_time[0].occurred_at,
        },
        max_iteration_gap=2,
        resolve_status=_resolver(statuses),
    )

    assert candidates == []


def test_metric_relations_use_matching_name_and_unit_only() -> None:
    op = "operation-metrics"
    transitions = [
        _transition(
            "run-metrics",
            1,
            metrics=[
                ObjectiveMetric(name="balance", value=10, unit="points"),
                ObjectiveMetric(name="balance", value=0.9, unit="ratio"),
                ObjectiveMetric(name="before_only", value=1, unit="count"),
            ],
        ),
        _transition(
            "run-metrics",
            2,
            operation_links=[OperationLink(operation_id=op, relation="initiated")],
        ),
        _transition(
            "run-metrics",
            3,
            operation_links=[
                OperationLink(operation_id=op, relation="observed"),
                OperationLink(operation_id="operation-other", relation="observed"),
            ],
        ),
        _transition(
            "run-metrics",
            4,
            metrics=[
                ObjectiveMetric(name="balance", value=12, unit="points"),
                ObjectiveMetric(name="balance", value=0.95, unit="ratio"),
                ObjectiveMetric(name="after_only", value=1, unit="count"),
            ],
        ),
    ]
    candidates = extract_operation_chains(
        _evidence(transitions),
        learning_cutoffs=_cutoff(transitions),
        max_iteration_gap=3,
        resolve_status=_resolver({(transitions[2].transition_id, op): "succeeded"}),
    )

    assert len(candidates) == 1
    assert {
        (relation.metric_name, relation.metric_unit) for relation in candidates[0].metric_relations
    } == {
        ("balance", "points"),
        ("balance", "ratio"),
    }
    assert candidates[0].metric_limitations == (
        "metric_missing_after:before_only:count",
        "metric_missing_before:after_only:count",
        "other_operation_links_in_metric_interval:operation-other",
    )


def test_opt_in_long_operation_lifetime_keeps_fresh_metric_and_full_interval() -> None:
    op = "operation-long"
    transitions = [
        _transition(
            "run-long",
            1,
            metrics=[ObjectiveMetric(name="balance", value=10, unit="points")],
        ),
        _transition(
            "run-long",
            2,
            operation_links=[OperationLink(operation_id=op, relation="initiated")],
        ),
    ]
    statuses = {}
    for iteration in range(3, 41):
        links = [OperationLink(operation_id=op, relation="observed")]
        if iteration == 20:
            links.append(OperationLink(operation_id="operation-other", relation="observed"))
        transitions.append(_transition("run-long", iteration, operation_links=links))
        statuses[(transitions[-1].transition_id, op)] = (
            "succeeded" if iteration == 40 else "pending"
        )
    transitions.extend(
        [
            _transition("run-long", 41),
            _transition(
                "run-long",
                42,
                metrics=[ObjectiveMetric(name="balance", value=12, unit="points")],
            ),
        ]
    )

    candidates = extract_operation_chains(
        _evidence(transitions),
        learning_cutoffs=_cutoff(transitions),
        max_iteration_gap=32,
        max_operation_iteration_gap=64,
        max_metric_boundary_gap=4,
        resolve_status=_resolver(statuses),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.completion_status == "succeeded"
    assert candidate.iteration_gap == 38
    relation = candidate.metric_relations[0]
    assert relation.iteration_gap == 41
    assert [ref.record_id for ref in relation.interval_records] == [
        transition.transition_id for transition in transitions
    ]
    assert "other_operation_links_in_metric_interval:operation-other" in (
        candidate.metric_limitations
    )


def test_opt_in_long_lifetime_without_completion_stays_uncompleted() -> None:
    op = "operation-long-pending"
    transitions = [
        _transition(
            "run-long-pending",
            1,
            metrics=[ObjectiveMetric(name="balance", value=10, unit="points")],
        ),
        _transition(
            "run-long-pending",
            2,
            operation_links=[OperationLink(operation_id=op, relation="initiated")],
        ),
    ]
    statuses = {}
    for iteration in range(3, 41):
        transition = _transition(
            "run-long-pending",
            iteration,
            operation_links=[OperationLink(operation_id=op, relation="observed")],
        )
        transitions.append(transition)
        statuses[(transition.transition_id, op)] = "pending"
    transitions.append(
        _transition(
            "run-long-pending",
            42,
            metrics=[ObjectiveMetric(name="balance", value=12, unit="points")],
        )
    )

    candidates = extract_operation_chains(
        _evidence(transitions),
        learning_cutoffs=_cutoff(transitions),
        max_iteration_gap=32,
        max_operation_iteration_gap=64,
        max_metric_boundary_gap=4,
        resolve_status=_resolver(statuses),
    )

    assert len(candidates) == 1
    assert candidates[0].completion_status == "pending"
    assert candidates[0].completion_record is None
    assert candidates[0].metric_relations == []
    assert [ref.record_id for ref in candidates[0].interval_records] == [
        transition.transition_id for transition in transitions[1:]
    ]


def test_opt_in_operation_lifetime_rejects_completion_after_finite_bound() -> None:
    op = "operation-expired"
    transitions = [
        _transition(
            "run-expired",
            1,
            metrics=[ObjectiveMetric(name="balance", value=10, unit="points")],
        ),
        _transition(
            "run-expired",
            2,
            operation_links=[OperationLink(operation_id=op, relation="initiated")],
        ),
    ]
    statuses = {}
    for iteration in range(3, 41):
        transition = _transition(
            "run-expired",
            iteration,
            operation_links=[OperationLink(operation_id=op, relation="observed")],
        )
        transitions.append(transition)
        statuses[(transition.transition_id, op)] = "succeeded" if iteration == 40 else "pending"

    candidates = extract_operation_chains(
        _evidence(transitions),
        learning_cutoffs=_cutoff(transitions),
        max_iteration_gap=32,
        max_operation_iteration_gap=8,
        resolve_status=_resolver(statuses),
    )

    assert len(candidates) == 1
    assert candidates[0].completion_status == "pending"
    assert candidates[0].completion_record is None
    assert max(int(ref.record_id.rsplit(":", 1)[1]) for ref in candidates[0].interval_records) == 10


def test_opt_in_metric_bound_rejects_stale_before_or_after_sample() -> None:
    op_before = "operation-stale-before"
    before_rows = [
        _transition(
            "run-stale-before",
            1,
            metrics=[ObjectiveMetric(name="balance", value=10, unit="points")],
        ),
        _transition(
            "run-stale-before",
            8,
            operation_links=[OperationLink(operation_id=op_before, relation="initiated")],
        ),
        _transition(
            "run-stale-before",
            9,
            operation_links=[OperationLink(operation_id=op_before, relation="observed")],
        ),
        _transition(
            "run-stale-before",
            10,
            metrics=[ObjectiveMetric(name="balance", value=12, unit="points")],
        ),
    ]
    op_after = "operation-stale-after"
    after_rows = [
        _transition(
            "run-stale-after",
            1,
            metrics=[ObjectiveMetric(name="balance", value=10, unit="points")],
        ),
        _transition(
            "run-stale-after",
            2,
            operation_links=[OperationLink(operation_id=op_after, relation="initiated")],
        ),
        _transition(
            "run-stale-after",
            3,
            operation_links=[OperationLink(operation_id=op_after, relation="observed")],
        ),
        _transition(
            "run-stale-after",
            10,
            metrics=[ObjectiveMetric(name="balance", value=12, unit="points")],
        ),
    ]
    statuses = {
        (before_rows[2].transition_id, op_before): "succeeded",
        (after_rows[2].transition_id, op_after): "succeeded",
    }

    candidates = extract_operation_chains(
        _evidence(before_rows + after_rows),
        learning_cutoffs={
            "run-stale-before": before_rows[-1].occurred_at,
            "run-stale-after": after_rows[-1].occurred_at,
        },
        max_iteration_gap=32,
        max_operation_iteration_gap=32,
        max_metric_boundary_gap=2,
        resolve_status=_resolver(statuses),
    )

    assert len(candidates) == 2
    by_run = {candidate.run_id: candidate for candidate in candidates}
    assert by_run["run-stale-before"].metric_relations == []
    assert by_run["run-stale-before"].metric_limitations == (
        "metric_before_initiation_exceeds_iteration_bound:balance:points",
    )
    assert by_run["run-stale-after"].metric_relations == []
    assert by_run["run-stale-after"].metric_limitations == (
        "metric_after_completion_exceeds_iteration_bound:balance:points",
    )
