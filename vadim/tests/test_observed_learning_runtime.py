from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.evaluation_presets import default_tool_knowledge_query_settings
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryContextRequest,
    MemoryValidationError,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence, LessonRunDeclaration
from uptick_agent.memory.patterns import PatternQuerySettings
from uptick_agent.memory.stores import InMemoryStructuredStore, SqliteStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
_SETTINGS = PatternQuerySettings(
    scope_paths=("observation.state",),
    action_path="action.kind",
    result_path="result.ok",
)


def _configuration(*, observed: bool = True, tool_knowledge: bool = False) -> MemoryConfiguration:
    payload = MemoryConfiguration.episodic_only().model_dump(mode="json")
    payload.update(
        schema_version="1.5",
        profile_kind="experiment",
        world_query_settings=_SETTINGS.model_dump(mode="json"),
    )
    payload["world_model"]["enabled"] = True
    payload["world_model"]["max_context_tokens"] = 4_000
    if observed:
        payload["observed_world_policy"] = "observed-pattern-summary-v1@1.0"
    if tool_knowledge:
        payload["tool_knowledge"]["enabled"] = True
        payload["tool_knowledge_query_settings"] = (
            default_tool_knowledge_query_settings().model_dump(mode="json")
        )
    return MemoryConfiguration.model_validate(payload)


def _declaration(run_id: str, *, phase: str = "learning") -> LessonRunDeclaration:
    return LessonRunDeclaration(
        run_id=run_id,
        logical_run_id=f"logical:{run_id}",
        phase=phase,
        environment_id="environment:observed-runtime",
        scenario_id="scenario:observed-runtime",
        environment_content_hash="a" * 64,
        scenario_content_hash="b" * 64,
        eligible=phase == "learning",
    )


def _transition(run_id: str, *, ok: bool = True, minute: int = 0) -> ExperienceTransition:
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"transition:{run_id}",
            run_id=run_id,
            iteration=1,
            occurred_at=_NOW.replace(minute=minute),
            environment_id="environment:observed-runtime",
            scenario_id="scenario:observed-runtime",
            trust_classification="external_untrusted",
            pre_state={"phase": "ready"},
            observation={"state": "healthy"},
            action={"kind": "inspect"},
            result={"ok": ok},
            terminal=True,
        )
    )


async def _evidence(
    store: InMemoryStructuredStore | SqliteStructuredStore,
    transitions: tuple[ExperienceTransition, ...],
    *,
    runs: tuple[LessonRunDeclaration, ...] = (),
    snapshot_id: str = "snapshot:observed-runtime",
) -> LessonEvidence:
    for transition in transitions:
        await store.append(
            RecordWrite(
                namespace="episodes",
                record_id=transition.transition_id,
                record_type="experience-transition",
                payload=transition.model_dump(mode="json"),
                created_at=transition.occurred_at,
            ),
            operation="record-transition",
            idempotency_key=f"record:{transition.run_id}",
        )
    snapshot = await store.create_snapshot(
        namespace="episodes",
        snapshot_id=snapshot_id,
        operation="freeze-episodes",
        idempotency_key=f"freeze:{snapshot_id}",
    )
    records = [
        await store.get(namespace="episodes", record_id=member.record_id)
        for member in snapshot.snapshot.members
    ]
    assert all(record is not None for record in records)
    return LessonEvidence(
        snapshot=snapshot.snapshot,
        records=records,
        runs=list(runs),
    )


def _request(run_id: str = "run:query") -> MemoryContextRequest:
    return MemoryContextRequest(
        request_id="request:observed-runtime",
        run_id=run_id,
        query="inspect healthy true",
        context={"observation": {"state": "healthy"}},
    )


def test_read_runtime_rejects_observed_learning_and_reports_capability() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        runtime = compose_experimental_runtime(
            _configuration(),
            store,
            namespace="runtime:read-only",
        )
        evidence = await _evidence(store, (_transition("run:learning"),))

        assert runtime.observed_learning_enabled is False
        with pytest.raises(MemoryValidationError, match="capability is disabled"):
            await runtime.record_observed_learning(
                evidence,
                learning_cutoffs={"run:learning": _NOW},
                idempotency_key="observed:read-only",
            )
        assert await store.list(namespace="runtime:read-only:world:observed") == []

    asyncio.run(scenario())


def test_explicit_observed_learning_writes_and_reopens_for_retrieval(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "observed-runtime.sqlite"
        declaration = _declaration("run:learning")
        store = SqliteStructuredStore(path)
        runtime = compose_experimental_runtime(
            _configuration(),
            store,
            namespace="runtime:writer",
            run_declarations=(declaration,),
            allow_observed_learning=True,
        )
        evidence = await _evidence(store, (_transition("run:learning"),))

        assert runtime.observed_learning_enabled is True
        configuration_fingerprint = runtime.configuration.fingerprint
        await runtime.record_observed_learning(
            evidence,
            learning_cutoffs={"run:learning": _NOW},
            idempotency_key="observed:write",
        )
        assert runtime.configuration.fingerprint == configuration_fingerprint
        assert len(await store.list(namespace="runtime:writer:world:observed")) == 1

        reopened = compose_experimental_runtime(
            _configuration(),
            SqliteStructuredStore(path),
            namespace="runtime:writer",
            run_declarations=(declaration,),
        )
        context = await reopened.build_context(_request())
        observed = [
            item for item in context.items if item.envelope.artefact_type == "observed_world_fact"
        ]
        assert len(observed) == 1
        assert observed[0].envelope.item["observed_run_count"] == 1

    asyncio.run(scenario())


def test_observed_learning_replay_is_idempotent() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        runtime = compose_experimental_runtime(
            _configuration(),
            store,
            namespace="runtime:idempotent",
            run_declarations=(_declaration("run:learning"),),
            allow_observed_learning=True,
        )
        evidence = await _evidence(store, (_transition("run:learning"),))
        cutoffs = {"run:learning": _NOW}
        await runtime.record_observed_learning(
            evidence, learning_cutoffs=cutoffs, idempotency_key="observed:replay"
        )
        await runtime.record_observed_learning(
            evidence, learning_cutoffs=cutoffs, idempotency_key="observed:replay"
        )

        assert len(await store.list(namespace="runtime:idempotent:world:observed")) == 1

    asyncio.run(scenario())


def test_composition_known_frozen_declaration_is_excluded_when_evidence_omits_runs() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        learning = _transition("run:learning", ok=True)
        frozen = _transition("run:frozen", ok=False, minute=1)
        runtime = compose_experimental_runtime(
            _configuration(),
            store,
            namespace="runtime:frozen",
            run_declarations=(
                _declaration("run:learning"),
                _declaration("run:frozen", phase="frozen_evaluation"),
            ),
            allow_observed_learning=True,
        )
        evidence = await _evidence(store, (learning, frozen))

        await runtime.record_observed_learning(
            evidence,
            learning_cutoffs={"run:learning": learning.occurred_at},
            idempotency_key="observed:frozen-exclusion",
        )
        context = await runtime.build_context(_request())
        observed = [
            item for item in context.items if item.envelope.artefact_type == "observed_world_fact"
        ]
        assert len(observed) == 1
        assert observed[0].envelope.item["observed_run_count"] == 1
        assert observed[0].envelope.item["support_count"] == 1

        with pytest.raises(MemoryValidationError, match="frozen evaluation"):
            await runtime.record_observed_learning(
                evidence,
                learning_cutoffs={"run:frozen": frozen.occurred_at},
                idempotency_key="observed:frozen-rejected",
            )

    asyncio.run(scenario())


def test_composition_keeps_world_writer_when_another_module_rebinds_settings() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        runtime = compose_experimental_runtime(
            _configuration(tool_knowledge=True),
            store,
            namespace="runtime:world-and-tool",
            run_declarations=(_declaration("run:learning"),),
            allow_observed_learning=True,
        )
        evidence = await _evidence(store, (_transition("run:learning"),))

        await runtime.record_observed_learning(
            evidence,
            learning_cutoffs={"run:learning": _NOW},
            idempotency_key="observed:world-and-tool",
        )
        assert len(await store.list(namespace="runtime:world-and-tool:world:observed")) == 1
        assert "tool_knowledge" in runtime.enabled_module_ids

    asyncio.run(scenario())


def test_composition_owns_declarations_against_later_caller_mutation() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        frozen = _declaration("run:frozen", phase="frozen_evaluation")
        runtime = compose_experimental_runtime(
            _configuration(),
            store,
            namespace="runtime:owned-declaration",
            run_declarations=(frozen,),
            allow_observed_learning=True,
        )
        frozen.phase = "learning"
        evidence = await _evidence(store, (_transition("run:frozen"),))

        with pytest.raises(MemoryValidationError, match="frozen evaluation"):
            await runtime.record_observed_learning(
                evidence,
                learning_cutoffs={"run:frozen": _NOW},
                idempotency_key="observed:mutated-declaration",
            )

    asyncio.run(scenario())


def test_duplicate_evidence_declarations_cannot_hide_frozen_source() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        runtime = compose_experimental_runtime(
            _configuration(),
            store,
            namespace="runtime:duplicate-declarations",
            run_declarations=(_declaration("run:learning"),),
            allow_observed_learning=True,
        )
        learning = _declaration("run:learning")
        frozen = _declaration("run:learning", phase="frozen_evaluation")
        evidence = await _evidence(
            store,
            (_transition("run:learning"),),
            runs=(frozen, learning),
        )

        with pytest.raises(MemoryValidationError, match="unique run IDs"):
            await runtime.record_observed_learning(
                evidence,
                learning_cutoffs={"run:learning": _NOW},
                idempotency_key="observed:duplicate-declarations",
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("flag", [True, "true", 1, None])
def test_observed_learning_flag_requires_policy_and_strict_boolean(flag: object) -> None:
    with pytest.raises(MemoryValidationError):
        compose_experimental_runtime(
            _configuration(observed=False),
            InMemoryStructuredStore(),
            namespace="runtime:flag-dependency",
            allow_observed_learning=flag,  # type: ignore[arg-type]
        )
