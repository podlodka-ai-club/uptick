from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

import pytest

from uptick_agent.memory.candidate_validation import extract_candidates, validate_candidate
from uptick_agent.memory.contracts import (
    MemoryContextRequest,
    ObjectiveMetric,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.episodic import EpisodicMemory
from uptick_agent.memory.lesson_contracts import (
    LessonEvidence,
    LessonRunDeclaration,
    LessonSettings,
)
from uptick_agent.memory.lessons import LessonsMemory
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.memory.stores.contracts import sha256_json
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler


def _settings(*, condition_paths: dict[str, str] | None = None) -> LessonSettings:
    return LessonSettings(
        metric_name="balance",
        metric_unit="minor",
        direction="maximize",
        condition_keys=("format", "duplicate_lines") if condition_paths is not None else ("a.b",),
        condition_paths=condition_paths,
    )


def _declaration(run_id: str, index: int) -> LessonRunDeclaration:
    return LessonRunDeclaration(
        run_id=run_id,
        logical_run_id=f"logical:{run_id}",
        phase="learning",
        eligible=True,
        environment_id=f"environment:{run_id}",
        scenario_id=f"scenario:{run_id}",
        environment_content_hash=sha256_json({"environment": index}),
        scenario_content_hash=sha256_json({"scenario": index}),
    )


def _transition(run_id: str, index: int, observation: dict[str, object], delta: int = 2):
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}",
            run_id=run_id,
            iteration=1,
            occurred_at=datetime(2026, 9, 6, 10 + index, tzinfo=UTC),
            environment_id=f"environment:{run_id}",
            scenario_id=f"scenario:{run_id}",
            trust_classification="external_untrusted",
            observation=observation,
            action={"kind": "inspect"},
            result={"ok": True},
            before_objective_metrics=[ObjectiveMetric(name="balance", value=0, unit="minor")],
            after_objective_metrics=[ObjectiveMetric(name="balance", value=delta, unit="minor")],
            terminal=True,
        )
    )


def _outcome(run_id: str, index: int) -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        status="completed",
        finished_at=datetime(2026, 9, 6, 12 + index, tzinfo=UTC),
        stop_reason="done",
    )


def test_legacy_dotted_condition_key_remains_literal_and_hash_compatible() -> None:
    settings = _settings()
    transitions = [
        _transition(
            "run-a",
            0,
            {"a.b": "literal", "a": {"b": "nested"}},
        )
    ]
    evidence = _evidence(transitions)

    candidate = extract_candidates(evidence, settings)[0]

    assert settings.model_dump(mode="json").get("condition_paths") is None
    assert candidate.conditions == {"a.b": "literal"}
    assert candidate.condition_paths is None
    assert candidate.semantic_hash == sha256_json(
        {
            "conditions": {"a.b": "literal"},
            "action": {"kind": "inspect"},
            "metric_name": "balance",
            "metric_unit": "minor",
            "direction": "maximize",
            "polarity": "positive",
        }
    )


def test_nested_opt_in_is_projected_and_bound_into_identity() -> None:
    settings = _settings(
        condition_paths={
            "format": "observation.format",
            "duplicate_lines": "observation.data.duplicate_lines",
        }
    )
    evidence = _evidence(
        [
            _transition("run-a", 0, {"format": "records", "data": {"duplicate_lines": 2}}),
            _transition("run-b", 1, {"format": "records", "data": {"duplicate_lines": 2}}),
        ]
    )

    candidate = extract_candidates(evidence, settings)[0]
    validated = validate_candidate(candidate, evidence, settings)

    assert candidate.conditions == {"format": "records", "duplicate_lines": 2}
    assert candidate.condition_paths == settings.condition_paths
    assert '"observation.data.duplicate_lines"' in candidate.statement
    assert validated.status == "active"
    legacy = candidate.model_copy(update={"condition_paths": None})
    with pytest.raises(ValueError, match="semantic hash mismatch"):
        type(candidate).model_validate(legacy.model_dump(mode="json"))


@pytest.mark.parametrize(
    "condition_paths",
    [
        {"format": "observation.format"},
        {"format": "observation.format", "duplicate_lines": "observation.format"},
        {"format": "result.format", "duplicate_lines": "observation.data.duplicate_lines"},
    ],
)
def test_nested_condition_paths_are_explicit_and_unambiguous(
    condition_paths: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        _settings(condition_paths=condition_paths)


def test_nested_scope_requires_matching_latest_result() -> None:
    async def scenario() -> None:
        settings = _settings(
            condition_paths={
                "format": "observation.format",
                "duplicate_lines": "observation.data.duplicate_lines",
            }
        )
        store = InMemoryStructuredStore()
        episodic = EpisodicMemory(store, namespace="episodes")
        declarations = [_declaration("run-a", 0), _declaration("run-b", 1)]
        outcomes = [_outcome("run-a", 0), _outcome("run-b", 1)]
        for index, (declaration, outcome) in enumerate(zip(declarations, outcomes, strict=True)):
            await episodic.record(
                _transition(
                    declaration.run_id,
                    index,
                    {"format": "records", "data": {"duplicate_lines": 2}},
                ),
                idempotency_key=f"record-{index}",
            )
            await episodic.finalize(outcome, idempotency_key=f"outcome-{index}")
        records = await store.list(namespace="episodes")
        snapshot = await store.create_snapshot(
            namespace="episodes",
            snapshot_id="snapshot:nested-lessons",
            operation="test-nested-lessons",
            idempotency_key="snapshot:nested-lessons",
        )
        evidence = LessonEvidence(
            snapshot=snapshot.snapshot,
            records=records,
            runs=declarations,
        )

        class Source:
            async def capture(self, outcome: RunOutcome, *, idempotency_key: str):
                return evidence

        memory = LessonsMemory(
            store,
            namespace="lessons",
            source=Source(),
            settings=settings,
        )
        await memory.finalize(outcomes[-1], idempotency_key="lesson-finalize")

        matching = await memory.retrieve(
            MemoryContextRequest(
                request_id="match",
                run_id="new-run",
                query="records duplicate_lines",
                context={
                    "latest_result": {
                        "format": "records",
                        "data": {"duplicate_lines": 2},
                    }
                },
            )
        )
        wrong_value = await memory.retrieve(
            MemoryContextRequest(
                request_id="wrong-value",
                run_id="new-run",
                query="records duplicate_lines",
                context={
                    "latest_result": {
                        "format": "records",
                        "data": {"duplicate_lines": 0},
                    }
                },
            )
        )
        missing = await memory.retrieve(
            MemoryContextRequest(
                request_id="missing",
                run_id="new-run",
                query="records duplicate_lines",
                context={"latest_result": {"format": "records"}},
            )
        )

        assert len(matching.items) == 1
        assert wrong_value.items == []
        assert missing.items == []

    asyncio.run(scenario())


def _evidence(transitions) -> LessonEvidence:
    from uptick_agent.memory.stores.contracts import (
        MemorySnapshot,
        RecordWrite,
        SnapshotMember,
        StoredRecord,
    )

    records = []
    declarations = []
    for index, transition in enumerate(transitions):
        declaration = _declaration(transition.run_id, index)
        outcome = _outcome(transition.run_id, index)
        records.extend(
            [
                StoredRecord.from_write(
                    RecordWrite(
                        namespace="lessons",
                        record_id=transition.transition_id,
                        record_type="experience-transition",
                        payload=transition.model_dump(mode="json"),
                        created_at=transition.occurred_at,
                    )
                ),
                StoredRecord.from_write(
                    RecordWrite(
                        namespace="lessons",
                        record_id=hashlib.sha256(
                            f"run-outcome:{outcome.run_id}".encode()
                        ).hexdigest(),
                        record_type="run-outcome",
                        payload=outcome.model_dump(mode="json"),
                        created_at=outcome.finished_at,
                    )
                ),
            ]
        )
        declarations.append(declaration)
    snapshot = MemorySnapshot.create(
        snapshot_id="snapshot:nested-test",
        namespace="lessons",
        members=[
            SnapshotMember(record_id=record.record_id, content_hash=record.content_hash)
            for record in records
        ],
    )
    return LessonEvidence(snapshot=snapshot, records=records, runs=declarations)
