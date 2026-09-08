import asyncio
import hashlib
from datetime import UTC, datetime

import pytest

from uptick_agent.core.models import CapabilityCatalog, ReasonerConfig, RunManifest, RunResult
from uptick_agent.core.trace_models import (
    DecisionTracePayload,
    RunFinishedPayload,
    RunStartedPayload,
    TraceEvent,
    run_stream_id,
)
from uptick_agent.environments.discovered.session import DiscoveredRunResult
from uptick_agent.store import InMemoryRunStore, JsonlRunStore


def test_jsonl_run_store_preserves_versioned_order_and_readback(tmp_path) -> None:
    async def scenario() -> None:
        store = JsonlRunStore(tmp_path)
        stream_id = run_stream_id("run")
        await store.record(
            TraceEvent(
                stream_id=stream_id,
                stream_kind="run",
                sequence=1,
                kind="run_started",
                payload=RunStartedPayload(
                    run_id="run",
                    environment_id="scripted",
                    environment_profile_version="profile-1",
                    initial_state={},
                ),
            )
        )
        await store.record(
            TraceEvent(
                stream_id=stream_id,
                stream_kind="run",
                sequence=2,
                kind="decision_trace",
                payload=DecisionTracePayload(
                    run_id="run",
                    iteration=1,
                    context_projection={},
                    context_sha256="b" * 64,
                    capabilities=CapabilityCatalog(items=[]),
                ),
            )
        )
        await store.save_result(
            RunResult(
                run_id="run",
                status="completed",
                steps=1,
                duration_seconds=0.1,
                stop_reason="done",
            )
        )

        recreated = JsonlRunStore(tmp_path)
        events = await recreated.load_stream(stream_id)
        assert [event.kind for event in events] == [
            "run_started",
            "decision_trace",
            "run_finished",
        ]
        assert [event.sequence for event in events] == [1, 2, 3]
        assert all(event.schema_version == 6 for event in events)

        manifest = RunManifest(
            run_id="run",
            agent_id="af-sgr",
            agent_version="af-baseline-0.1",
            reasoner=ReasonerConfig(provider="scripted", model="scripted", thread_mode="stateless"),
            memory_mode="none",
            carry_memory=False,
            environment="scripted",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await store.save_manifest(manifest)
        assert await recreated.load_manifest("run") == manifest
        manifest_files = list((tmp_path / "manifests").glob("*.json"))
        assert [path.name for path in manifest_files] == [
            hashlib.sha256(b"run").hexdigest() + ".json"
        ]
        assert not list((tmp_path / "manifests").glob("*.tmp"))

    asyncio.run(scenario())


def test_jsonl_run_store_preserves_environment_specific_result_details(tmp_path) -> None:
    async def scenario() -> None:
        store = JsonlRunStore(tmp_path)
        await store.save_result(
            DiscoveredRunResult(
                run_id="run-economics",
                simulator_run_id="remote-run",
                objective="Meet the published availability target and minimize costs.",
                status="running",
                steps=2,
                duration_seconds=2,
                stop_reason="step limit reached",
                forced=True,
                final_state={
                    "costs": {"total_cost_minor": 42},
                    "availability": {"uptime_ratio": 0.997},
                    "clock": {"remaining_seconds": 86_400},
                    "evaluation": {"score": None},
                },
            )
        )

        events = await JsonlRunStore(tmp_path).load_stream("run:run-economics")
        payload = events[-1].payload
        assert isinstance(payload, RunFinishedPayload)
        assert payload.result.forced
        assert payload.result_details["forced"] is True
        assert payload.result_details["simulator_run_id"] == "remote-run"
        assert payload.result_details["final_state"] == {
            "costs": {"total_cost_minor": 42},
            "availability": {"uptime_ratio": 0.997},
            "clock": {"remaining_seconds": 86_400},
            "evaluation": {"score": None},
        }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "contents",
    [
        '{"schema_version":1,"run_id":"old"}\n',
        '{"schema_version":2,"run_id":"old"}\n',
        '{"schema_version":3,"run_id":"old"}\n',
        ('{"schema_version":1,"run_id":"old"}\n{"schema_version":2,"run_id":"new"}\n'),
    ],
)
def test_jsonl_run_store_rejects_legacy_or_mixed_trace_before_append(
    tmp_path,
    contents: str,
) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported or mixed"):
        JsonlRunStore(tmp_path)


def test_run_store_rejects_out_of_order_stream_sequence() -> None:
    async def scenario() -> None:
        store = InMemoryRunStore()
        with pytest.raises(ValueError, match="expected sequence 1"):
            await store.record(
                TraceEvent(
                    stream_id="run:run",
                    stream_kind="run",
                    sequence=2,
                    kind="run_started",
                    payload=RunStartedPayload(
                        run_id="run",
                        environment_id="scripted",
                        environment_profile_version="profile-1",
                        initial_state={},
                    ),
                )
            )

    asyncio.run(scenario())


def test_v5_is_readable_but_cannot_be_appended_or_mixed_with_v6(tmp_path) -> None:
    legacy = TraceEvent(
        schema_version=5,
        stream_id="run:old",
        stream_kind="run",
        sequence=1,
        kind="run_started",
        payload=RunStartedPayload(
            run_id="old",
            environment_id="fake",
            environment_profile_version="profile-1",
            initial_state={},
        ),
    )
    path = tmp_path / "trace.jsonl"
    original = legacy.model_dump_json() + "\n"
    path.write_text(original)
    store = JsonlRunStore(tmp_path)

    async def scenario() -> None:
        assert await store.load_stream("run:old") == [legacy]
        with pytest.raises(ValueError, match="legacy trace is read-only"):
            await store.record(legacy.model_copy(update={"schema_version": 6, "sequence": 2}))

    asyncio.run(scenario())
    assert path.read_text() == original
    current = legacy.model_copy(update={"schema_version": 6, "sequence": 2})
    path.write_text(original + current.model_dump_json() + "\n")
    with pytest.raises(ValueError, match="mixed schema versions"):
        JsonlRunStore(tmp_path)


def test_in_memory_run_store_is_not_a_memory_backend() -> None:
    store = InMemoryRunStore()
    assert not hasattr(store, "recall")
    assert not hasattr(store, "index")
