from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from uptick_agent.memory import episodic_memory_runtime
from uptick_agent.memory.audit import StructuredAuditTraceSink
from uptick_agent.memory.config import (
    AuditConfiguration,
    MemoryConfiguration,
    ModuleConfig,
    RawContentConfiguration,
)
from uptick_agent.memory.contracts import (
    CreatedMemoryItem,
    ExperienceTransition,
    MemoryContextRequest,
    MemoryPermanentError,
    MemoryValidationError,
    ObjectiveMetric,
    OperationLink,
    ProvenanceRef,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.episodic import (
    EPISODIC_MODULE_VERSION,
    EPISODIC_QUERY_EXCERPT_MODULE_VERSION,
    EpisodicMemory,
)
from uptick_agent.memory.settings import EpisodicRecallSettings
from uptick_agent.memory.stores import (
    InMemoryStructuredStore,
    RecordWrite,
    SqliteStructuredStore,
)
from uptick_agent.memory.stores.contracts import canonical_json
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


def _query_tail_transition(*, run_id: str = "tail-run", transition_id: str = "tail-transition"):
    return ExperienceTransition(
        transition_id=transition_id,
        run_id=run_id,
        iteration=7,
        occurred_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
        environment_id="public-synthetic",
        scenario_id="public-excerpt-fixture",
        trust_classification="external_untrusted",
        pre_state={"status": "degraded"},
        observation={"summary": "public observation"},
        action={"kind": "inspect_public_status"},
        result={
            "detail": "D" * 900 + '"quoted 🌍"\n',
            "ok": False,
            "summary": "public result contains exclusive-public-result-needle",
        },
        provenance=[
            # This is a synthetic public source identity, not a credential or
            # evaluator label.
            ProvenanceRef(
                artefact_id="public-fixture-source",
                relation="source",
                content_hash="a" * 64,
            )
        ],
        terminal=False,
    )


def _raw_recall_configuration(*, episodic_version: str = EPISODIC_MODULE_VERSION):
    return MemoryConfiguration(
        schema_version="1.4",
        profile_id="episodic-raw-recall",
        profile_kind="experiment",
        compatibility_legacy=ModuleConfig(enabled=False),
        episodic=ModuleConfig(
            enabled=True,
            version=episodic_version,
            max_context_items=32,
            max_context_tokens=4_000,
        ),
        episodic_recall=EpisodicRecallSettings(),
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

        first_receipt = await memory.record(transition, idempotency_key="transition-key")
        second_receipt = await memory.record(transition, idempotency_key="transition-key")

        assert first_receipt == second_receipt
        assert first_receipt == [
            CreatedMemoryItem(
                item_id="transition-1",
                artefact_type="episode",
                provenance=transition.provenance,
            )
        ]

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


def test_query_match_excerpt_is_explicit_and_preserves_default_view() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        transition = _query_tail_transition()
        legacy = EpisodicMemory(store, namespace="query-excerpt")
        opted_in = EpisodicMemory(
            store,
            namespace="query-excerpt",
            module_version=EPISODIC_QUERY_EXCERPT_MODULE_VERSION,
        )
        custom_version = EpisodicMemory(
            store,
            namespace="query-excerpt",
            module_version="custom-version",
        )
        await legacy.record(transition, idempotency_key="record-tail")
        await legacy.finalize(
            RunOutcome(
                run_id=transition.run_id,
                status="completed",
                finished_at=datetime(2026, 9, 5, 12, 1, tzinfo=UTC),
                stop_reason="public synthetic fixture completed",
            ),
            idempotency_key="outcome-tail",
        )
        request = MemoryContextRequest(
            request_id="query-tail",
            run_id="different-run",
            # The large detail token and the more specific token are both
            # retrieval evidence; only the latter is suitable for the bounded
            # supplementary window.
            query="D" * 900 + " exclusive-public-result-needle",
        )

        default_item = (await legacy.retrieve(request)).items[0]
        opted_in_item = (await opted_in.retrieve(request)).items[0]
        custom_item = (await custom_version.retrieve(request)).items[0]
        assert EPISODIC_MODULE_VERSION == "1.0"
        assert custom_item.envelope.item == default_item.envelope.item
        assert custom_item.envelope.origin_version == "custom-version"
        assert default_item.envelope.item == {
            key: value for key, value in opted_in_item.envelope.item.items() if key != "query_match"
        }
        assert default_item.score == opted_in_item.score
        assert default_item.selection_reason == opted_in_item.selection_reason
        assert opted_in_item.selection_reason == "episodic lexical overlap=2"
        assert default_item.envelope.item_id == opted_in_item.envelope.item_id
        assert default_item.envelope.provenance == opted_in_item.envelope.provenance
        assert default_item.envelope.trust_classification == "external_untrusted"

        match = opted_in_item.envelope.item["query_match"]
        assert isinstance(match, dict)
        assert match["source_field"] == "result"
        assert match["complete"] is False
        assert isinstance(match["char_start"], int)
        assert isinstance(match["char_end"], int)
        rendered_result = canonical_json(transition.result)
        assert match["text"] == rendered_result[match["char_start"] : match["char_end"]]
        assert "exclusive-public-result-needle" in match["text"]
        assert '"ok":false' in match["text"]
        assert '"summary"' in match["text"]
        assert '\\"quoted' in match["text"]
        assert "\\ud83c\\udf0d" in match["text"]
        assert "\\n" in match["text"]
        assert match["char_start"] >= 512
        assert (
            len(
                json.dumps(match, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
            )
            <= 600
        )

        stored = await store.get(namespace="query-excerpt", record_id=transition.transition_id)
        assert stored is not None
        assert stored.payload == transition.model_dump(mode="json")

    asyncio.run(scenario())


def test_query_match_excerpt_has_no_match_fallback_and_preserves_run_eligibility() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        memory = EpisodicMemory(
            store,
            namespace="query-excerpt-fallback",
            module_version=EPISODIC_QUERY_EXCERPT_MODULE_VERSION,
        )
        visible = _transition(run_id="visible-run", transition_id="visible-transition")
        await memory.record(visible, idempotency_key="record-visible")
        visible_item = (
            await memory.retrieve(
                MemoryContextRequest(
                    request_id="visible-query",
                    run_id="other-run",
                    query="healthy",
                )
            )
        ).items
        assert visible_item == []

        # The same-run path is eligible, but the matching token is already in
        # the ordinary observation prefix, so it receives the default view.
        same_run = (
            await memory.retrieve(
                MemoryContextRequest(
                    request_id="same-run-query",
                    run_id="visible-run",
                    query="get_overview",
                )
            )
        ).items
        assert len(same_run) == 1
        assert "query_match" not in same_run[0].envelope.item

        failed = _query_tail_transition(run_id="failed-run", transition_id="failed-transition")
        await memory.record(failed, idempotency_key="record-failed")
        await memory.finalize(
            RunOutcome(
                run_id=failed.run_id,
                status="failed",
                finished_at=datetime(2026, 9, 5, 12, 2, tzinfo=UTC),
                stop_reason="public synthetic failure",
            ),
            idempotency_key="outcome-failed",
        )
        unfinished = _query_tail_transition(
            run_id="unfinished-run", transition_id="unfinished-transition"
        )
        await memory.record(unfinished, idempotency_key="record-unfinished")
        distant = (
            await memory.retrieve(
                MemoryContextRequest(
                    request_id="distant-query",
                    run_id="other-run",
                    query="exclusive-public-result-needle",
                )
            )
        ).items
        assert distant == []

    asyncio.run(scenario())


def test_query_match_excerpt_opt_in_flows_through_episodic_runtime() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        base_configuration = MemoryConfiguration.episodic_only()
        configuration = base_configuration.model_copy(
            update={
                "episodic": ModuleConfig(
                    enabled=True,
                    version=EPISODIC_QUERY_EXCERPT_MODULE_VERSION,
                    max_context_items=32,
                    max_context_tokens=4_000,
                )
            }
        )
        runtime = episodic_memory_runtime(
            store,
            namespace="query-excerpt-runtime",
            configuration=configuration,
        )
        transition = _query_tail_transition(
            run_id="runtime-tail-run", transition_id="runtime-tail-transition"
        )
        await runtime.record_transition(transition)
        await runtime.finalize_run(
            RunOutcome(
                run_id=transition.run_id,
                status="completed",
                finished_at=datetime(2026, 9, 5, 12, 3, tzinfo=UTC),
                stop_reason="public synthetic fixture completed",
            )
        )

        context = await runtime.build_context(
            MemoryContextRequest(
                request_id="runtime-tail-query",
                run_id="different-runtime-run",
                query="D" * 900 + " exclusive-public-result-needle",
            )
        )
        assert len(context.items) == 1
        item = context.items[0]
        assert item.envelope.origin_version == EPISODIC_QUERY_EXCERPT_MODULE_VERSION
        assert item.envelope.item["query_match"]["source_field"] == "result"
        assert "exclusive-public-result-needle" in item.envelope.item["query_match"]["text"]
        assert item.envelope.provenance == transition.provenance
        assert item.envelope.trust_classification == "external_untrusted"

    asyncio.run(scenario())


def test_episodic_replay_derives_receipt_from_authoritative_sqlite_record(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "episodic-receipt-replay.sqlite"
        store = SqliteStructuredStore(database)
        memory = EpisodicMemory(store, namespace="receipt-replay")
        transition = _transition()

        first = await memory.record(transition, idempotency_key="transition-key")
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                """
                SELECT receipt_json
                FROM memory_operation_receipts
                WHERE namespace = ? AND operation = ? AND idempotency_key = ?
                """,
                ("receipt-replay", "record-transition", "transition-key"),
            ).fetchone()
            assert row is not None
            forged = json.loads(row[0])
            forged["record"]["payload"]["transition_id"] = "forged-transition"
            connection.execute(
                """
                UPDATE memory_operation_receipts
                SET receipt_json = ?
                WHERE namespace = ? AND operation = ? AND idempotency_key = ?
                """,
                (
                    json.dumps(forged),
                    "receipt-replay",
                    "record-transition",
                    "transition-key",
                ),
            )

        replay = await memory.record(transition, idempotency_key="transition-key")

        assert replay == first
        assert replay[0].item_id == transition.transition_id
        stored = await store.get(namespace="receipt-replay", record_id=transition.transition_id)
        assert stored is not None
        assert stored.payload["transition_id"] == transition.transition_id

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


def test_raw_recall_configuration_is_explicit_and_does_not_change_default_fingerprint() -> None:
    default = MemoryConfiguration.episodic_only()
    assert "episodic_recall" not in default.model_dump(mode="json")
    assert default.fingerprint == MemoryConfiguration.episodic_only().fingerprint

    opted_in = _raw_recall_configuration()
    assert opted_in.schema_version == "1.4"
    assert opted_in.episodic_recall == EpisodicRecallSettings()
    assert opted_in.fingerprint != default.fingerprint

    with pytest.raises(ValueError, match="schema_version 1.4"):
        MemoryConfiguration(
            compatibility_legacy=ModuleConfig(enabled=False),
            episodic=ModuleConfig(enabled=True),
            episodic_recall=EpisodicRecallSettings(),
        )
    with pytest.raises(ValueError, match="requires episodic enabled"):
        MemoryConfiguration(
            schema_version="1.4",
            compatibility_legacy=ModuleConfig(enabled=False),
            episodic_recall=EpisodicRecallSettings(),
        )
    with pytest.raises(ValueError, match="cannot be enabled by default"):
        MemoryConfiguration(
            schema_version="1.4",
            profile_kind="default",
            compatibility_legacy=ModuleConfig(enabled=False),
            episodic=ModuleConfig(
                enabled=True,
                status="default",
                approval_record_id="approved-raw-recall",
            ),
            episodic_recall=EpisodicRecallSettings(),
        )


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_opted_in_raw_recall_exposes_finalized_failure_statuses_with_boundaries(
    store_kind: str, tmp_path: Path
) -> None:
    async def scenario() -> None:
        database = tmp_path / "raw-recall.sqlite"
        store = (
            InMemoryStructuredStore() if store_kind == "memory" else SqliteStructuredStore(database)
        )
        settings = EpisodicRecallSettings()
        memory = EpisodicMemory(
            store,
            namespace="raw-recall",
            episodic_recall=settings,
        )

        transitions = {
            status: _transition(run_id=f"{status}-run", transition_id=f"{status}-transition")
            for status in ("completed", "failed", "interrupted", "excluded")
        }
        transitions["missing"] = _transition(
            run_id="missing-run", transition_id="missing-transition"
        )
        for transition in transitions.values():
            await memory.record(transition, idempotency_key=f"record-{transition.run_id}")
        for status in ("completed", "failed", "interrupted", "excluded"):
            await memory.finalize(
                RunOutcome(
                    run_id=transitions[status].run_id,
                    status=status,
                    finished_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
                    stop_reason=(f"{status} " + "x" * 500),
                    objective_metrics=(
                        [
                            ObjectiveMetric(name="uptime_ratio", value=0.22212, unit="ratio"),
                            ObjectiveMetric(name="observed_seconds", value=604800, unit="seconds"),
                        ]
                        if status == "completed"
                        else []
                    ),
                ),
                idempotency_key=f"outcome-{status}",
            )

        if store_kind == "sqlite":
            store = SqliteStructuredStore(database)
            memory = EpisodicMemory(
                store,
                namespace="raw-recall",
                episodic_recall=settings,
            )

        historical = await memory.retrieve(
            MemoryContextRequest(request_id="historical", run_id="reader", query="healthy")
        )
        historical_ids = {item.envelope.item_id for item in historical.items}
        assert historical_ids == {
            "completed-transition",
            "failed-transition",
            "interrupted-transition",
        }
        by_id = {item.envelope.item_id: item for item in historical.items}
        for status in ("completed", "failed", "interrupted"):
            item = by_id[f"{status}-transition"]
            assert item.envelope.item["source_run_outcome"]["status"] == status
            assert item.envelope.item["source_run_outcome"]["terminal"] is True
            assert len(item.envelope.item["source_run_outcome"]["stop_reason"]) == 256
            assert item.envelope.item["source_run_outcome"]["status_scope"] == "execution"
            assert item.envelope.item["source_run_outcome"]["stop_reason_truncated"] is True
            assert item.envelope.provenance == transitions[status].provenance
            assert item.envelope.trust_classification == "external_untrusted"
            assert item.envelope.item["run_id"] == transitions[status].run_id
        assert by_id["completed-transition"].envelope.item["source_run_outcome"][
            "objective_metrics"
        ] == [
            {"name": "uptime_ratio", "value": 0.22212, "unit": "ratio"},
            {"name": "observed_seconds", "value": 604800.0, "unit": "seconds"},
        ]
        assert (
            by_id["completed-transition"].envelope.item["source_run_outcome"][
                "objective_metrics_omitted_count"
            ]
            == 0
        )

        # Same-run working memory remains available before finalization, but
        # the prompt-facing item says explicitly that no outcome exists yet.
        unfinished = await memory.retrieve(
            MemoryContextRequest(
                request_id="same-missing", run_id="missing-run", query="missing-transition"
            )
        )
        assert [item.envelope.item_id for item in unfinished.items] == ["missing-transition"]
        assert unfinished.items[0].envelope.item["source_run_outcome"] == {
            "status": "not_finalized",
            "status_scope": "execution",
            "terminal": None,
            "objective_metrics": [],
            "objective_metrics_omitted_count": 0,
        }

        excluded_same_run = await memory.retrieve(
            MemoryContextRequest(
                request_id="same-excluded", run_id="excluded-run", query="excluded-transition"
            )
        )
        assert [item.envelope.item_id for item in excluded_same_run.items] == [
            "excluded-transition"
        ]
        assert excluded_same_run.items[0].envelope.item["source_run_outcome"]["status"] == (
            "excluded"
        )

        # A non-opted-in module sharing the store retains the completed-only
        # view and has no new outcome metadata.
        default_memory = EpisodicMemory(store, namespace="raw-recall")
        default_historical = await default_memory.retrieve(
            MemoryContextRequest(request_id="default", run_id="reader", query="healthy")
        )
        assert [item.envelope.item_id for item in default_historical.items] == [
            "completed-transition"
        ]
        assert "source_run_outcome" not in default_historical.items[0].envelope.item

    asyncio.run(scenario())


def test_raw_recall_outcome_metrics_are_exact_ordered_prefix_with_bounded_bytes() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        memory = EpisodicMemory(
            store,
            namespace="raw-recall-metrics",
            episodic_recall=EpisodicRecallSettings(),
        )
        transition = _transition(run_id="metrics-run", transition_id="metrics-transition")
        await memory.record(transition, idempotency_key="record-metrics")
        metrics = [
            ObjectiveMetric(name=f"metric-{index}-" + "x" * 80, value=index + 0.25, unit="units")
            for index in range(40)
        ]
        await memory.finalize(
            RunOutcome(
                run_id=transition.run_id,
                status="completed",
                finished_at=datetime(2026, 9, 5, 14, tzinfo=UTC),
                stop_reason="execution completed",
                objective_metrics=metrics,
            ),
            idempotency_key="outcome-metrics",
        )

        context = await memory.retrieve(
            MemoryContextRequest(request_id="metrics-query", run_id="reader", query="healthy")
        )
        metadata = context.items[0].envelope.item["source_run_outcome"]
        selected = metadata["objective_metrics"]
        assert selected == [
            {"name": metric.name, "value": metric.value, "unit": metric.unit}
            for metric in metrics[: len(selected)]
        ]
        assert len(canonical_json(selected).encode("utf-8")) <= 1_024
        assert metadata["objective_metrics_omitted_count"] == len(metrics) - len(selected)
        assert metadata["status_scope"] == "execution"
        assert metadata["terminal"] is True
        assert "stop_reason_truncated" not in metadata
        assert context.items[0].envelope.trust_classification == "external_untrusted"
        assert context.items[0].envelope.provenance == transition.provenance

    asyncio.run(scenario())


def test_raw_recall_setting_reaches_both_public_composition_roots() -> None:
    async def scenario() -> None:
        from uptick_agent.composition.memory import compose_experimental_runtime

        transition = _query_tail_transition(
            run_id="composition-failed", transition_id="composition-transition"
        )
        outcome = RunOutcome(
            run_id=transition.run_id,
            status="failed",
            finished_at=datetime(2026, 9, 5, 13, tzinfo=UTC),
            stop_reason="public composition failure",
        )
        configuration = _raw_recall_configuration(
            episodic_version=EPISODIC_QUERY_EXCERPT_MODULE_VERSION
        )

        compatibility_store = InMemoryStructuredStore()
        compatibility_runtime = episodic_memory_runtime(
            compatibility_store,
            namespace="raw-recall-compatibility",
            configuration=configuration,
        )
        composition_store = InMemoryStructuredStore()
        composition_runtime = compose_experimental_runtime(
            configuration,
            composition_store,
            namespace="raw-recall-composition",
        )
        for runtime, namespace in (
            (compatibility_runtime, "raw-recall-compatibility"),
            (composition_runtime, "raw-recall-composition"),
        ):
            await runtime.record_transition(transition)
            await runtime.finalize_run(outcome)
            context = await runtime.build_context(
                MemoryContextRequest(
                    request_id=f"query-{namespace}",
                    run_id="reader",
                    query="D" * 900 + " exclusive-public-result-needle",
                )
            )
            assert [item.envelope.item_id for item in context.items] == ["composition-transition"]
            assert context.items[0].envelope.item["source_run_outcome"]["status"] == "failed"
            assert context.items[0].envelope.item["query_match"]["source_field"] == "result"
            assert (
                "exclusive-public-result-needle"
                in context.items[0].envelope.item["query_match"]["text"]
            )

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


def test_episodic_primary_records_ignore_disabled_audit_raw_flags() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        configuration = MemoryConfiguration.episodic_only(
            audit=AuditConfiguration(
                enabled=True,
                raw_content=RawContentConfiguration(
                    prompts=False,
                    observations=False,
                    decision_traces=False,
                ),
            )
        )
        audit_sink = StructuredAuditTraceSink(
            store,
            namespace="raw-flags-disabled-audit",
            configuration=configuration.audit,
            runtime_configuration_fingerprint=configuration.fingerprint,
        )
        runtime = episodic_memory_runtime(
            store,
            namespace="raw-flags-disabled",
            configuration=configuration,
            audit_sink=audit_sink,
        )
        transition = _transition()
        outcome = RunOutcome(
            run_id=transition.run_id,
            status="completed",
            finished_at=datetime(2026, 9, 4, 11, tzinfo=UTC),
            stop_reason="finished after validation",
        )

        await runtime.record_transition(transition)
        await runtime.finalize_run(outcome)

        records = await store.list(namespace="raw-flags-disabled")
        persisted_transition = next(
            record for record in records if record.record_type == "experience-transition"
        )
        persisted_outcome = next(
            record for record in records if record.record_type == "run-outcome"
        )
        assert persisted_transition.payload == transition.model_dump(mode="json")
        assert persisted_outcome.payload["stop_reason"] == outcome.stop_reason

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
