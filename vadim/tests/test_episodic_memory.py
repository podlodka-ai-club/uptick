from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from uptick_agent.memory import episodic_memory_runtime
from uptick_agent.memory.contracts import (
    MemoryContextRequest,
    MemoryPermanentError,
    MemoryValidationError,
    ObjectiveMetric,
    OperationLink,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.episodic import EpisodicMemory
from uptick_agent.memory.stores import (
    InMemoryStructuredStore,
    RecordWrite,
    SqliteStructuredStore,
)
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler


def _transition(*, run_id: str = "run-1", transition_id: str = "transition-1"):
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=transition_id,
            run_id=run_id,
            iteration=1,
            occurred_at=datetime(2026, 9, 4, 10, tzinfo=UTC),
            trust_classification="external_untrusted",
            pre_state={"operations": {}},
            observation={"summary": "site healthy", "detail": "x" * 700},
            action={"kind": "get_overview"},
            result={"ok": True, "summary": "balance improved"},
            before_objective_metrics=[ObjectiveMetric(name="balance", value=1, unit="minor")],
            after_objective_metrics=[ObjectiveMetric(name="balance", value=3, unit="minor")],
            operation_links=[OperationLink(operation_id="operation-1", relation="observed")],
            terminal=False,
        )
    )


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_episodic_record_finalize_and_retrieve_across_store_reopen(
    store_kind: str, tmp_path: Path
) -> None:
    async def scenario() -> None:
        database = tmp_path / "episodic.sqlite"
        store = (
            InMemoryStructuredStore() if store_kind == "memory" else SqliteStructuredStore(database)
        )
        memory = EpisodicMemory(store, namespace="experiment-1")
        transition = _transition()

        await memory.record(transition, idempotency_key="transition-key")
        await memory.record(transition, idempotency_key="transition-key")

        records = await store.list(namespace="experiment-1")
        assert len(records) == 1
        assert records[0].record_type == "experience-transition"
        assert records[0].payload == transition.model_dump(mode="json")

        same_run = await memory.retrieve(
            MemoryContextRequest(request_id="same", run_id="run-1", query="healthy")
        )
        assert [item.envelope.item_id for item in same_run.items] == ["transition-1"]

        unfinished_other_run = await memory.retrieve(
            MemoryContextRequest(request_id="other", run_id="run-2", query="healthy")
        )
        assert unfinished_other_run.items == []

        outcome = RunOutcome(
            run_id="run-1",
            status="completed",
            finished_at=datetime(2026, 9, 4, 11, tzinfo=UTC),
            stop_reason="token=topsecret done",
            objective_metrics=[ObjectiveMetric(name="balance", value=3, unit="minor")],
        )
        await memory.finalize(outcome, idempotency_key="outcome-key")
        await memory.finalize(outcome, idempotency_key="outcome-key")

        persisted_outcomes = [
            record
            for record in await store.list(namespace="experiment-1")
            if record.record_type == "run-outcome"
        ]
        assert persisted_outcomes[0].payload["stop_reason"] == "<redacted> done"

        if store_kind == "sqlite":
            store = SqliteStructuredStore(database)
            memory = EpisodicMemory(store, namespace="experiment-1")

        historical = await memory.retrieve(
            MemoryContextRequest(request_id="historical", run_id="run-2", query="healthy")
        )
        repeated = await memory.retrieve(
            MemoryContextRequest(request_id="repeat", run_id="run-2", query="healthy")
        )

        assert historical == repeated
        assert historical.module_id == "episodic"
        assert historical.module_version == "1.0"
        assert len(historical.items) == 1
        item = historical.items[0]
        assert item.envelope.trust_classification == "external_untrusted"
        assert item.envelope.provenance == transition.provenance
        assert isinstance(item.envelope.item["observation"], str)
        assert "characters omitted" in item.envelope.item["observation"]
        assert item.envelope.item["objective_deltas"][0]["delta"] == 2
        assert item.envelope.item["operation_links"][0]["operation_id"] == "operation-1"

        isolated = EpisodicMemory(store, namespace="experiment-2")
        empty = await isolated.retrieve(
            MemoryContextRequest(request_id="isolated", run_id="run-2", query="healthy")
        )
        assert empty.items == []

    asyncio.run(scenario())


def test_failed_historical_run_is_not_retrieved() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        memory = EpisodicMemory(store, namespace="experiment")
        await memory.record(_transition(run_id="failed"), idempotency_key="transition")
        await memory.finalize(
            RunOutcome(
                run_id="failed",
                status="failed",
                finished_at=datetime(2026, 9, 4, 11, tzinfo=UTC),
                stop_reason="failed",
            ),
            idempotency_key="outcome",
        )

        contribution = await memory.retrieve(
            MemoryContextRequest(request_id="query", run_id="other", query="healthy")
        )

        assert contribution.items == []

    asyncio.run(scenario())


def test_episodic_write_rejects_an_unredacted_transition_bypass() -> None:
    async def scenario() -> None:
        memory = EpisodicMemory(InMemoryStructuredStore(), namespace="experiment")
        unsafe = _transition().model_copy(update={"transition_id": "sk-abcdefghijk"})

        with pytest.raises(MemoryValidationError, match="unredacted credential"):
            await memory.record(unsafe, idempotency_key="unsafe")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("record_type", "payload", "message"),
    [
        ("unknown", {"value": True}, "unknown record type"),
        ("experience-transition", {"invalid": True}, "transition is invalid"),
    ],
)
def test_episodic_retrieval_fails_closed_on_invalid_stored_records(
    record_type: str, payload: dict, message: str
) -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        await store.append(
            RecordWrite(
                namespace="experiment",
                record_id="invalid",
                record_type=record_type,
                payload=payload,
                created_at=datetime(2026, 9, 4, tzinfo=UTC),
            ),
            operation="test",
            idempotency_key="invalid",
        )
        memory = EpisodicMemory(store, namespace="experiment")

        with pytest.raises(MemoryPermanentError, match=message):
            await memory.retrieve(
                MemoryContextRequest(request_id="query", run_id="run", query="healthy")
            )

    asyncio.run(scenario())


def test_public_episodic_runtime_composes_the_module_without_legacy_writes() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        runtime = episodic_memory_runtime(store, namespace="programmatic")
        transition = _transition()

        await runtime.record_transition(transition)
        context = await runtime.build_context(
            MemoryContextRequest(request_id="query", run_id="run-1", query="healthy")
        )

        assert [item.envelope.item_id for item in context.items] == ["transition-1"]
        assert runtime.context_diagnostics["resolved_configuration"]["profile_id"] == (
            "episodic-only"
        )
        with pytest.raises(MemoryPermanentError, match="fresh namespace"):
            await runtime.clear()

    asyncio.run(scenario())


def test_episodic_module_has_no_environment_or_provider_imports() -> None:
    source = (Path(__file__).parents[1] / "src/uptick_agent/memory/episodic.py").read_text()
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)

    forbidden = (
        "uptick_agent.simulator",
        "uptick_agent.llm",
        "uptick_agent.memory.compatibility",
    )
    assert not any(name.startswith(forbidden) for name in imports)
