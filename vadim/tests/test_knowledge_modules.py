from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryContextRequest,
    MemoryPermanentError,
    ObjectiveMetric,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.episodic import EpisodicMemory
from uptick_agent.memory.lesson_contracts import LessonEvidence, LessonRunDeclaration
from uptick_agent.memory.lesson_evidence import StoredEpisodicLessonSource
from uptick_agent.memory.patterns import (
    PATTERN_VALIDATION_POLICY,
    PatternQuerySettings,
    generate_pattern_candidates,
    validate_pattern_candidate,
)
from uptick_agent.memory.playbooks import (
    PlaybookQuerySettings,
    PlaybooksMemory,
    generate_playbook_candidates,
    validate_playbook_candidate,
)
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    StoredRecord,
    sha256_json,
)
from uptick_agent.memory.tool_knowledge import (
    ToolKnowledgeMemory,
    ToolKnowledgeQuerySettings,
    generate_tool_knowledge_candidates,
    validate_tool_knowledge_candidate,
)
from uptick_agent.memory.world_model import WorldModelMemory
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_TIME = datetime(2026, 9, 5, 12, tzinfo=UTC)
_SETTINGS = PatternQuerySettings(
    scope_paths=("observation.state.service",),
    action_path="action.kind",
    result_path="result.shape",
)


def _declaration(
    run_id: str,
    scenario: str,
    *,
    logical_run_id: str | None = None,
    attempt_index: int = 0,
    phase: str = "learning",
    eligible: bool = True,
    scenario_content: str | None = None,
) -> LessonRunDeclaration:
    environment_id = "environment:test"
    scenario_id = f"scenario:{scenario}"
    return LessonRunDeclaration(
        run_id=run_id,
        logical_run_id=logical_run_id or f"logical:{run_id}",
        attempt_index=attempt_index,
        phase=phase,
        eligible=eligible,
        environment_id=environment_id,
        scenario_id=scenario_id,
        environment_content_hash=sha256_json({"environment": "test"}),
        scenario_content_hash=sha256_json({"scenario": scenario_content or scenario}),
    )


def _transition(
    run_id: str,
    *,
    index: int,
    shape: str,
    scenario: str,
) -> ExperienceTransition:
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}",
            run_id=run_id,
            iteration=1,
            occurred_at=_TIME + timedelta(minutes=index),
            environment_id="environment:test",
            scenario_id=f"scenario:{scenario}",
            trust_classification="external_untrusted",
            pre_state={"service": "ready"},
            observation={"state": {"service": "ready"}},
            action={"kind": "inspect"},
            result={"shape": shape, "ok": True},
            before_objective_metrics=[ObjectiveMetric(name="health", value=1, unit="points")],
            after_objective_metrics=[ObjectiveMetric(name="health", value=1, unit="points")],
            terminal=True,
        )
    )


def _outcome(run_id: str, *, index: int, status: str = "completed") -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        status=status,
        finished_at=_TIME + timedelta(minutes=index, seconds=30),
        stop_reason=status,
    )


def _step(
    run_id: str,
    *,
    index: int,
    iteration: int,
    scenario: str,
    action_kind: str,
    shape: str = "healthy",
    ok_value: object | None = None,
    observation_service: str = "ready",
    include_scope: bool = True,
    include_action_kind: bool = True,
    include_target: bool = True,
    include_shape: bool = True,
    include_guard: bool = True,
) -> ExperienceTransition:
    action = {}
    if include_action_kind:
        action["kind"] = action_kind
    if include_target:
        action["target"] = "api"
    result: dict[str, object] = {}
    if include_shape:
        result["shape"] = shape
    if include_guard:
        result["ok"] = shape == "healthy" if ok_value is None else ok_value
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}:{iteration}",
            run_id=run_id,
            iteration=iteration,
            occurred_at=_TIME + timedelta(minutes=index, seconds=iteration),
            environment_id="environment:test",
            scenario_id=f"scenario:{scenario}",
            trust_classification="external_untrusted",
            pre_state={"service": "ready"},
            observation={"state": {"service": observation_service} if include_scope else {}},
            action=action,
            result=result,
            terminal=iteration == 2,
        )
    )


def _record(value: ExperienceTransition | RunOutcome) -> StoredRecord:
    if isinstance(value, RunOutcome):
        record_id = hashlib.sha256(f"run-outcome:{value.run_id}".encode()).hexdigest()
        record_type = "run-outcome"
        created_at = value.finished_at
    else:
        record_id = value.transition_id
        record_type = "experience-transition"
        created_at = value.occurred_at
    return StoredRecord.from_write(
        RecordWrite(
            namespace="episodes",
            record_id=record_id,
            record_type=record_type,
            payload=value.model_dump(mode="json"),
            created_at=created_at,
        )
    )


def _evidence(
    declarations: list[LessonRunDeclaration],
    values: list[tuple[ExperienceTransition, RunOutcome]],
) -> LessonEvidence:
    records: list[StoredRecord] = []
    seen_outcomes: set[str] = set()
    for transition, outcome in values:
        records.append(_record(transition))
        if outcome.run_id not in seen_outcomes:
            records.append(_record(outcome))
            seen_outcomes.add(outcome.run_id)
    snapshot = MemorySnapshot.create(
        snapshot_id="snapshot:test",
        namespace="episodes",
        members=[
            SnapshotMember(record_id=record.record_id, content_hash=record.content_hash)
            for record in records
        ],
    )
    return LessonEvidence(snapshot=snapshot, records=records, runs=declarations)


async def _world_with_runs(
    declarations: list[LessonRunDeclaration],
    shapes: list[str],
    *,
    settings: PatternQuerySettings = _SETTINGS,
) -> tuple[InMemoryStructuredStore, WorldModelMemory, list[RunOutcome]]:
    store = InMemoryStructuredStore()
    episodic = EpisodicMemory(store, namespace="episodes")
    source = StoredEpisodicLessonSource(
        store,
        episodic_namespace="episodes",
        declaration_namespace="lesson-declarations",
        run_declarations=declarations,
    )
    world = WorldModelMemory(
        store,
        namespace="world",
        source=source,
        settings=settings,
    )
    outcomes: list[RunOutcome] = []
    for index, (declaration, shape) in enumerate(zip(declarations, shapes, strict=True)):
        transition = _transition(
            declaration.run_id,
            index=index,
            shape=shape,
            scenario=declaration.scenario_id.removeprefix("scenario:"),
        )
        outcome = _outcome(declaration.run_id, index=index)
        await episodic.record(transition, idempotency_key=f"record:{declaration.run_id}")
        await episodic.finalize(outcome, idempotency_key=f"outcome:{declaration.run_id}")
        await world.finalize(outcome, idempotency_key=f"world:{declaration.run_id}")
        outcomes.append(outcome)
    return store, world, outcomes


def test_pattern_query_requires_safe_roots_and_binds_settings() -> None:
    with pytest.raises(ValueError):
        PatternQuerySettings(
            scope_paths=("result.shape",),
            action_path="action.kind",
            result_path="result.shape",
        )

    declarations = [_declaration("run-a", "a"), _declaration("run-b", "b")]
    values = [
        (
            _transition("run-a", index=0, shape="healthy", scenario="a"),
            _outcome("run-a", index=0),
        ),
        (
            _transition("run-b", index=1, shape="healthy", scenario="b"),
            _outcome("run-b", index=1),
        ),
    ]
    evidence = _evidence(declarations, values)
    candidate = generate_pattern_candidates(evidence, _SETTINGS)[0]
    assert candidate.query_settings_hash == sha256_json(_SETTINGS.model_dump(mode="json"))
    assert validate_pattern_candidate(candidate, evidence, _SETTINGS).manifest.policy_ref == (
        PATTERN_VALIDATION_POLICY
    )
    other_settings = PatternQuerySettings(
        scope_paths=("observation.state.service",),
        action_path="action.kind",
        result_path="result.ok",
    )
    with pytest.raises(Exception, match="query settings"):
        validate_pattern_candidate(candidate, evidence, other_settings)


def test_two_completed_contexts_activate_and_scope_is_required() -> None:
    async def scenario() -> None:
        declarations = [_declaration("run-a", "a"), _declaration("run-b", "b")]
        _store, world, outcomes = await _world_with_runs(declarations, ["healthy", "healthy"])
        request = MemoryContextRequest(
            request_id="read",
            run_id="new-run",
            query="healthy",
            context={"latest_result": {"state": {"service": "ready"}}},
        )
        contribution = await world.retrieve(request)
        assert len(contribution.items) == 1
        assert contribution.items[0].envelope.item["status"] == "active"

        no_observation = request.model_copy(update={"context": {}})
        assert (await world.retrieve(no_observation)).items == []
        same_run = request.model_copy(update={"run_id": outcomes[0].run_id})
        assert (await world.retrieve(same_run)).items == []

    asyncio.run(scenario())


def test_world_retrieval_supports_explicit_pre_state_scope() -> None:
    async def scenario() -> None:
        settings = PatternQuerySettings(
            scope_paths=("pre_state.service",),
            action_path="action.kind",
            result_path="result.shape",
        )
        declarations = [_declaration("run-a", "a"), _declaration("run-b", "b")]
        _store, world, _outcomes = await _world_with_runs(
            declarations, ["healthy", "healthy"], settings=settings
        )
        contribution = await world.retrieve(
            MemoryContextRequest(
                request_id="read",
                run_id="new-run",
                query="healthy",
                context={"pre_state": {"service": "ready"}},
            )
        )
        assert len(contribution.items) == 1

    asyncio.run(scenario())


def test_stage11_validation_excludes_eval_retries_and_non_boolean_guards() -> None:
    settings = PlaybookQuerySettings(
        scope_paths=("observation.state.service",),
        action_path="action.kind",
        sequence_length=2,
        guard_path="result.ok",
        guard_value=True,
    )
    declarations = [
        _declaration("run-a", "a", logical_run_id="logical:a"),
        _declaration("run-b", "b", logical_run_id="logical:b"),
        _declaration(
            "run-a-retry",
            "a",
            logical_run_id="logical:a",
            attempt_index=1,
        ),
        _declaration("eval", "eval", phase="frozen_evaluation", eligible=False),
        _declaration("integer-guard", "integer"),
    ]
    values: list[tuple[ExperienceTransition, RunOutcome]] = []
    for index, declaration in enumerate(declarations[:2]):
        scenario = declaration.scenario_id.removeprefix("scenario:")
        outcome = _outcome(declaration.run_id, index=index)
        values.extend(
            [
                (
                    _step(
                        declaration.run_id,
                        index=index,
                        iteration=1,
                        scenario=scenario,
                        action_kind="inspect",
                    ),
                    outcome,
                ),
                (
                    _step(
                        declaration.run_id,
                        index=index,
                        iteration=2,
                        scenario=scenario,
                        action_kind="repair",
                    ),
                    outcome,
                ),
            ]
        )
    retry = declarations[2]
    retry_scenario = retry.scenario_id.removeprefix("scenario:")
    retry_outcome = _outcome(retry.run_id, index=2, status="failed")
    values.extend(
        [
            (
                _step(
                    retry.run_id,
                    index=2,
                    iteration=1,
                    scenario=retry_scenario,
                    action_kind="inspect",
                ),
                retry_outcome,
            ),
            (
                _step(
                    retry.run_id,
                    index=2,
                    iteration=2,
                    scenario=retry_scenario,
                    action_kind="repair",
                ),
                retry_outcome,
            ),
        ]
    )
    evaluation = declarations[3]
    evaluation_scenario = evaluation.scenario_id.removeprefix("scenario:")
    evaluation_outcome = _outcome(evaluation.run_id, index=3)
    values.extend(
        [
            (
                _step(
                    evaluation.run_id,
                    index=3,
                    iteration=1,
                    scenario=evaluation_scenario,
                    action_kind="inspect",
                    shape="degraded",
                ),
                evaluation_outcome,
            ),
            (
                _step(
                    evaluation.run_id,
                    index=3,
                    iteration=2,
                    scenario=evaluation_scenario,
                    action_kind="repair",
                    shape="degraded",
                ),
                evaluation_outcome,
            ),
        ]
    )
    integer_guard = declarations[4]
    integer_scenario = integer_guard.scenario_id.removeprefix("scenario:")
    integer_outcome = _outcome(integer_guard.run_id, index=4)
    values.extend(
        [
            (
                _step(
                    integer_guard.run_id,
                    index=4,
                    iteration=1,
                    scenario=integer_scenario,
                    action_kind="inspect",
                    ok_value=1,
                ),
                integer_outcome,
            ),
            (
                _step(
                    integer_guard.run_id,
                    index=4,
                    iteration=2,
                    scenario=integer_scenario,
                    action_kind="repair",
                    ok_value=1,
                ),
                integer_outcome,
            ),
        ]
    )
    evidence = _evidence(declarations, values)
    candidates = generate_playbook_candidates(evidence, settings)
    assert len(candidates) == 1
    validated = validate_playbook_candidate(candidates[0], evidence, settings)
    manifest = validated.manifest
    assert manifest.support_logical_run_ids == ("logical:a", "logical:b")
    assert manifest.counter_count == 2
    assert manifest.unresolved_contradiction_count == 1
    assert validated.status == "disputed"
    assert not any(item.startswith("transition:eval:") for item in manifest.searched_evidence_ids)
    assert any(item.startswith("transition:run-a-retry:") for item in manifest.counter_evidence_ids)


def test_playbook_windows_require_consecutive_iterations() -> None:
    settings = PlaybookQuerySettings(
        scope_paths=("observation.state.service",),
        action_path="action.kind",
        sequence_length=2,
        guard_path="result.ok",
        guard_value=True,
    )
    declaration = _declaration("gapped", "gapped")
    outcome = _outcome("gapped", index=0)
    evidence = _evidence(
        [declaration],
        [
            (
                _step(
                    "gapped",
                    index=0,
                    iteration=1,
                    scenario="gapped",
                    action_kind="inspect",
                ),
                outcome,
            ),
            (
                _step(
                    "gapped",
                    index=0,
                    iteration=3,
                    scenario="gapped",
                    action_kind="repair",
                ),
                outcome,
            ),
        ],
    )
    assert generate_playbook_candidates(evidence, settings) == []


@pytest.mark.parametrize("missing", ["scope", "action", "guard"])
def test_playbook_missing_nested_projection_is_ignored(missing: str) -> None:
    settings = PlaybookQuerySettings(
        scope_paths=("observation.state.service",),
        action_path="action.kind",
        sequence_length=2,
        guard_path="result.ok",
        guard_value=True,
    )
    declaration = _declaration(f"missing-playbook-{missing}", missing)
    run_id = declaration.run_id
    first = _step(
        run_id,
        index=0,
        iteration=1,
        scenario=missing,
        action_kind="inspect",
        include_scope=missing != "scope",
    )
    second = _step(
        run_id,
        index=0,
        iteration=2,
        scenario=missing,
        action_kind="repair",
        include_action_kind=missing != "action",
        include_guard=missing != "guard",
    )
    evidence = _evidence(
        [declaration],
        [(first, _outcome(run_id, index=0)), (second, _outcome(run_id, index=0))],
    )
    assert generate_playbook_candidates(evidence, settings) == []


@pytest.mark.parametrize("missing", ["scope", "action", "input", "response"])
def test_tool_missing_nested_projection_is_ignored(missing: str) -> None:
    settings = ToolKnowledgeQuerySettings(
        adapter_identity="test-adapter@1",
        scope_paths=("observation.state.service",),
        action_path="action.kind",
        input_paths=("action.target",),
        response_path="result.shape",
    )
    declaration = _declaration(f"missing-tool-{missing}", missing)
    transition = _step(
        declaration.run_id,
        index=0,
        iteration=1,
        scenario=missing,
        action_kind="inspect",
        include_scope=missing != "scope",
        include_action_kind=missing != "action",
        include_target=missing != "input",
        include_shape=missing != "response",
    )
    evidence = _evidence(
        [declaration],
        [(transition, _outcome(declaration.run_id, index=0))],
    )
    assert generate_tool_knowledge_candidates(evidence, settings) == []


def test_playbook_counter_search_keeps_changed_intermediate_observation() -> None:
    settings = PlaybookQuerySettings(
        scope_paths=("observation.state.service",),
        action_path="action.kind",
        sequence_length=2,
        guard_path="result.ok",
        guard_value=True,
    )
    declarations = [_declaration("scope-a", "a"), _declaration("scope-b", "b")]
    values: list[tuple[ExperienceTransition, RunOutcome]] = []
    for index, declaration in enumerate(declarations):
        scenario = declaration.scenario_id.removeprefix("scenario:")
        outcome = _outcome(declaration.run_id, index=index)
        values.extend(
            [
                (
                    _step(
                        declaration.run_id,
                        index=index,
                        iteration=1,
                        scenario=scenario,
                        action_kind="inspect",
                    ),
                    outcome,
                ),
                (
                    _step(
                        declaration.run_id,
                        index=index,
                        iteration=2,
                        scenario=scenario,
                        action_kind="repair",
                    ),
                    outcome,
                ),
            ]
        )
    counter = _declaration("scope-counter", "counter")
    counter_outcome = _outcome(counter.run_id, index=2)
    values.extend(
        [
            (
                _step(
                    counter.run_id,
                    index=2,
                    iteration=1,
                    scenario="counter",
                    action_kind="inspect",
                ),
                counter_outcome,
            ),
            (
                _step(
                    counter.run_id,
                    index=2,
                    iteration=2,
                    scenario="counter",
                    action_kind="repair",
                    shape="degraded",
                    observation_service="changed",
                ),
                counter_outcome,
            ),
        ]
    )
    evidence = _evidence([*declarations, counter], values)
    candidate = generate_playbook_candidates(evidence, settings)[0]
    result = validate_playbook_candidate(candidate, evidence, settings)
    assert result.manifest.counter_count == 1
    assert result.manifest.unresolved_contradiction_count == 1
    assert "transition:scope-counter:2" in result.manifest.counter_evidence_ids


def test_same_content_with_renamed_context_does_not_activate() -> None:
    async def scenario() -> None:
        declarations = [
            _declaration("run-a", "renamed-a", scenario_content="same"),
            _declaration("run-b", "renamed-b", scenario_content="same"),
        ]
        _store, world, _outcomes = await _world_with_runs(declarations, ["healthy", "healthy"])
        contribution = await world.retrieve(
            MemoryContextRequest(
                request_id="read",
                run_id="new-run",
                query="healthy",
                context={"latest_result": {"state": {"service": "ready"}}},
            )
        )
        assert contribution.items == []

    asyncio.run(scenario())


def test_counterexample_demotes_prior_active_hypothesis_and_preserves_history() -> None:
    async def scenario() -> None:
        declarations = [
            _declaration("run-a", "a"),
            _declaration("run-b", "b"),
            _declaration("run-c", "c"),
        ]
        store, world, _outcomes = await _world_with_runs(
            declarations, ["healthy", "healthy", "degraded"]
        )
        contribution = await world.retrieve(
            MemoryContextRequest(
                request_id="read",
                run_id="new-run",
                query="healthy",
                context={"latest_result": {"state": {"service": "ready"}}},
            )
        )
        assert contribution.items == []
        batches = await store.list(namespace="world")
        assert len(batches) == 3
        assert any(item.payload["hypotheses"] for item in batches[:2])
        assert all(
            item.payload["hypotheses"][0]["status"] == "disputed"
            for item in batches[-1:]
            if item.payload["hypotheses"]
        )

    asyncio.run(scenario())


def test_retry_and_frozen_evaluation_are_never_support() -> None:
    declarations = [
        _declaration("run-a", "a"),
        _declaration(
            "run-a-retry",
            "a",
            logical_run_id="logical:a",
            attempt_index=1,
        ),
        _declaration("eval", "eval", phase="frozen_evaluation", eligible=False),
    ]
    values = [
        (
            _transition("run-a", index=0, shape="healthy", scenario="a"),
            _outcome("run-a", index=0),
        ),
        (
            _transition("run-a-retry", index=1, shape="healthy", scenario="a"),
            _outcome("run-a-retry", index=1),
        ),
        (
            _transition("eval", index=2, shape="healthy", scenario="eval"),
            _outcome("eval", index=2),
        ),
    ]
    evidence = _evidence(declarations, values)
    candidate = generate_pattern_candidates(evidence, _SETTINGS)[0]
    validated = validate_pattern_candidate(candidate, evidence, _SETTINGS)
    assert validated.manifest.support_count == 1
    assert validated.manifest.counter_count == 1
    assert "transition:eval" not in validated.manifest.searched_evidence_ids


def test_tampered_store_evidence_is_rejected_on_retrieval() -> None:
    async def scenario() -> None:
        declarations = [_declaration("run-a", "a"), _declaration("run-b", "b")]
        store, world, _outcomes = await _world_with_runs(declarations, ["healthy", "healthy"])
        record = next(item for item in store._records.values() if item.namespace == "episodes")
        store._records[(record.namespace, record.record_id)] = record.model_copy(
            update={"content_hash": "0" * 64}
        )
        with pytest.raises(MemoryPermanentError):
            await world.retrieve(
                MemoryContextRequest(
                    request_id="read",
                    run_id="new-run",
                    query="healthy",
                    context={"latest_result": {"state": {"service": "ready"}}},
                )
            )

    asyncio.run(scenario())


def test_finalization_is_idempotent_and_none_source_is_quiet() -> None:
    async def scenario() -> None:
        declarations = [_declaration("run-a", "a"), _declaration("run-b", "b")]
        store, world, outcomes = await _world_with_runs(declarations, ["healthy", "healthy"])
        await world.finalize(outcomes[-1], idempotency_key="replay")
        assert len(await store.list(namespace="world")) == 2

        quiet = WorldModelMemory(
            store,
            namespace="disabled-world",
            source=None,
            settings=_SETTINGS,
        )
        await quiet.finalize(outcomes[-1], idempotency_key="disabled")
        assert await store.list(namespace="disabled-world") == []
        assert (
            await quiet.retrieve(
                MemoryContextRequest(request_id="read", run_id="new", query="healthy")
            )
        ).items == []

    asyncio.run(scenario())


def test_playbook_and_tool_knowledge_are_distinct_derived_modules() -> None:
    playbook_settings = PlaybookQuerySettings(
        scope_paths=("observation.state.service",),
        action_path="action.kind",
        sequence_length=2,
        guard_path="result.ok",
        guard_value=True,
    )
    tool_settings = ToolKnowledgeQuerySettings(
        adapter_identity="test-adapter@1",
        scope_paths=("observation.state.service",),
        action_path="action.kind",
        input_paths=("action.target",),
        response_path="result.shape",
    )
    declarations = [_declaration("run-a", "a"), _declaration("run-b", "b")]
    # A playbook requires two ordered actions; direct tool knowledge can learn
    # the response projection from either action independently.
    sequence_values = []
    for index, declaration in enumerate(declarations):
        outcome = _outcome(declaration.run_id, index=index)
        first = _step(
            declaration.run_id,
            index=index,
            iteration=1,
            scenario=declaration.scenario_id.removeprefix("scenario:"),
            action_kind="inspect",
        )
        second = _step(
            declaration.run_id,
            index=index,
            iteration=2,
            scenario=declaration.scenario_id.removeprefix("scenario:"),
            action_kind="repair",
        )
        sequence_values.extend([(first, outcome), (second, outcome)])
    evidence = _evidence(
        declarations,
        sequence_values,
    )
    playbook_candidates = generate_playbook_candidates(evidence, playbook_settings)
    tool_candidates = generate_tool_knowledge_candidates(evidence, tool_settings)
    assert len(playbook_candidates) == 1
    assert {candidate.action_kind for candidate in tool_candidates} == {"inspect", "repair"}
    playbook = validate_playbook_candidate(playbook_candidates[0], evidence, playbook_settings)
    tool = validate_tool_knowledge_candidate(tool_candidates[0], evidence, tool_settings)
    assert playbook.status == "active"
    assert tool.status == "active"
    assert playbook.candidate.action_sequence == ("inspect", "repair")
    assert tool.candidate.input_features == {"action.target": "api"}


def test_playbook_and_tool_modules_persist_and_retrieve_separately() -> None:
    async def scenario() -> None:
        playbook_settings = PlaybookQuerySettings(
            scope_paths=("pre_state.service",),
            action_path="action.kind",
            sequence_length=2,
            guard_path="result.ok",
            guard_value=True,
        )
        tool_settings = ToolKnowledgeQuerySettings(
            adapter_identity="test-adapter@1",
            scope_paths=("pre_state.service",),
            action_path="action.kind",
            input_paths=("action.target",),
            response_path="result.shape",
        )
        declarations = [_declaration("run-a", "a"), _declaration("run-b", "b")]
        store = InMemoryStructuredStore()
        episodic = EpisodicMemory(store, namespace="episodes")
        source = StoredEpisodicLessonSource(
            store,
            episodic_namespace="episodes",
            declaration_namespace="lesson-declarations",
            run_declarations=declarations,
        )
        playbooks = PlaybooksMemory(
            store,
            namespace="playbooks",
            source=source,
            settings=playbook_settings,
        )
        tool_knowledge = ToolKnowledgeMemory(
            store,
            namespace="tool-knowledge",
            source=source,
            settings=tool_settings,
        )
        for index, declaration in enumerate(declarations):
            scenario_name = declaration.scenario_id.removeprefix("scenario:")
            outcome = _outcome(declaration.run_id, index=index)
            await episodic.record(
                _step(
                    declaration.run_id,
                    index=index,
                    iteration=1,
                    scenario=scenario_name,
                    action_kind="inspect",
                ),
                idempotency_key=f"transition:{declaration.run_id}:1",
            )
            await episodic.record(
                _step(
                    declaration.run_id,
                    index=index,
                    iteration=2,
                    scenario=scenario_name,
                    action_kind="repair",
                ),
                idempotency_key=f"transition:{declaration.run_id}:2",
            )
            await episodic.finalize(outcome, idempotency_key=f"episodic:{declaration.run_id}")
            await playbooks.finalize(outcome, idempotency_key=f"playbook:{declaration.run_id}")
            await tool_knowledge.finalize(outcome, idempotency_key=f"tool:{declaration.run_id}")

        request = MemoryContextRequest(
            request_id="read",
            run_id="new-run",
            query="repair healthy",
            context={"pre_state": {"service": "ready"}},
        )
        playbook_items = (await playbooks.retrieve(request)).items
        tool_items = (await tool_knowledge.retrieve(request)).items
        assert playbook_items
        assert tool_items
        assert all(item.envelope.artefact_type == "playbook" for item in playbook_items)
        assert all(item.envelope.artefact_type == "tool_knowledge" for item in tool_items)
        assert playbook_items[0].envelope.item["action_sequence"] == ["inspect", "repair"]
        assert "input_features" in tool_items[0].envelope.item

        pre_state_request = request.model_copy(
            update={"context": {"pre_state": {"service": "ready"}}}
        )
        assert (await playbooks.retrieve(pre_state_request)).items
        assert (await tool_knowledge.retrieve(pre_state_request)).items

        physical_run_request = request.model_copy(
            update={
                "context": {
                    "latest_result": {"state": {"service": "ready"}},
                    "pre_state": {"service": "ready"},
                    "physical_run_id": "run-a",
                }
            }
        )
        assert (await playbooks.retrieve(physical_run_request)).items == []
        assert (await tool_knowledge.retrieve(physical_run_request)).items == []

    asyncio.run(scenario())
