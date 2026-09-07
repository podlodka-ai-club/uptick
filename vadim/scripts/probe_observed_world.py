"""Prepare real observed summaries, then read them in a separate offline process.

The query is a mechanical fixture, not a live decision or a held-out utility
measurement. Source experience is the unchanged frozen raw export.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from probe_memory_evidence import read_frozen_export

from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.contracts import MemoryContextRequest
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.settings import PatternQuerySettings
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite


async def prepare(args) -> None:
    _, source_hash, records = read_frozen_export(args.manifest, args.records)
    settings = PatternQuerySettings.model_validate_json(args.settings.read_text())
    config = json.loads(args.raw_configuration.read_text())["configuration"]
    raw_fingerprint = MemoryConfiguration.model_validate(config).fingerprint
    config.update(
        schema_version="1.5",
        profile_id="raw-plus-observed-world-stage03",
        profile_kind="experiment",
        world_query_settings=settings.model_dump(mode="json"),
        observed_world_policy="observed-pattern-summary-v1@1.0",
    )
    config["world_model"]["enabled"] = True
    # Allocate inside the unchanged total context budget, not above it.
    config["world_model"]["max_context_tokens"] = 3_000
    config["world_model"]["max_context_items"] = 3
    configuration = MemoryConfiguration.model_validate(config)
    args.output.mkdir(parents=True, exist_ok=False)
    store = SqliteStructuredStore(args.output / "memory.sqlite3")
    namespace = records[0].namespace
    for record in records:
        await store.append(
            RecordWrite.model_validate(record.model_dump(exclude={"content_hash"})),
            operation="import-frozen-observation",
            idempotency_key=record.record_id,
        )
    receipt = await store.create_snapshot(
        namespace=namespace,
        snapshot_id=f"observed-source:{source_hash}",
        operation="freeze-observed-source",
        idempotency_key=source_hash,
    )
    evidence = LessonEvidence(snapshot=receipt.snapshot, records=records, runs=[])
    runtime = compose_experimental_runtime(
        configuration,
        store,
        namespace=namespace,
        allow_observed_learning=True,
    )
    await runtime.record_observed_learning(
        evidence,
        learning_cutoffs={args.learning_run_id: args.observed_through},
        idempotency_key="stage03-observed-summaries",
    )
    (args.output / "configuration.json").write_text(configuration.canonical_json() + "\n")
    (args.output / "prepare.json").write_text(
        json.dumps(
            {
                "source_sha256": source_hash,
                "namespace": namespace,
                "source_raw_configuration_fingerprint": raw_fingerprint,
                "observed_configuration_fingerprint": configuration.fingerprint,
                "source_run_id": args.learning_run_id,
                "observed_through": args.observed_through.isoformat(),
                "observed_learning_admitted": runtime.observed_learning_enabled,
                "ingestion_boundary": "ExperimentalMemoryRuntime.record_observed_learning",
                "claim": "real-data preparation only; read must run in a new process",
            },
            indent=2,
        )
        + "\n"
    )


async def read(args) -> None:
    preparation = json.loads((args.output / "prepare.json").read_text())
    config = MemoryConfiguration.model_validate_json(
        (args.output / "configuration.json").read_text()
    )
    # Preserve every prior observation, including failures and warnings.
    destination = args.read_output or args.output
    if args.read_output is not None:
        destination.mkdir(parents=True, exist_ok=False)
    elif any(
        (destination / name).exists()
        for name in (
            "read-results.json",
            "context-off.json",
            "context-on.json",
            "context-same_run.json",
        )
    ):
        raise FileExistsError("read evidence already exists; choose a new --read-output")
    query = MemoryContextRequest(
        request_id="observed-world-offline-query",
        run_id="offline-observed-world-probe",
        query="server.create running",
        context={"observation": {"ok": True}, "latest_result": {"ok": True}},
    )
    source_store = SqliteStructuredStore(args.output / "memory.sqlite3")
    source_records = await source_store.list(namespace=preparation["namespace"])
    source_iterations = [
        record.payload["iteration"]
        for record in source_records
        if record.payload.get("run_id") == preparation["source_run_id"]
        and isinstance(record.payload.get("iteration"), int)
    ]
    if not source_iterations:
        raise ValueError("source run has no recorded iterations")
    # A same-run recall query must name when it occurs. Omitting this field
    # correctly fails episodic freshness checks and confounds world exclusion.
    same_run_query = query.model_copy(
        update={
            "run_id": preparation["source_run_id"],
            "context": {**query.context, "iteration": max(source_iterations) + 1},
        }
    )
    result = {}
    for label, enabled, request in (
        ("off", False, query),
        ("on", True, query),
        ("same_run", True, same_run_query),
    ):
        payload = config.model_dump(mode="json")
        if not enabled:
            payload.pop("observed_world_policy")
        profile = MemoryConfiguration.model_validate(payload)
        store = SqliteStructuredStore(args.output / "memory.sqlite3")
        runtime = compose_experimental_runtime(profile, store, namespace=preparation["namespace"])
        started = time.monotonic()
        context = await runtime.build_context(request)
        elapsed = time.monotonic() - started
        observed = [item for item in context.items if item.envelope.origin_module == "world_model"]
        (destination / f"context-{label}.json").write_text(context.model_dump_json(indent=2) + "\n")
        result[label] = {
            "observed_learning_admitted": runtime.observed_learning_enabled,
            "request": request.model_dump(mode="json"),
            "observed_items": len(observed),
            "total_items": len(context.items),
            "estimated_tokens": sum(item.estimated_tokens for item in context.items),
            "elapsed_seconds": elapsed,
            "warnings": context.warnings,
            "diagnostics": runtime.context_diagnostics,
        }
    (destination / "read-results.json").write_text(json.dumps(result, indent=2) + "\n")
    assert result["off"]["observed_items"] == 0, "observed data leaked without opt-in"
    assert result["on"]["observed_items"] > 0, "observed summaries did not reach composed context"
    assert result["same_run"]["observed_items"] == 0, (
        "current-run evidence leaked into observed facts"
    )
    assert result["on"]["estimated_tokens"] <= config.context_budget.total_tokens
    print(
        json.dumps(
            {
                key: {k: v for k, v in val.items() if k != "diagnostics"}
                for key, val in result.items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "read"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--read-output", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--records", type=Path)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--raw-configuration", type=Path)
    parser.add_argument("--learning-run-id")
    parser.add_argument("--observed-through", type=datetime.fromisoformat)
    args = parser.parse_args()
    if args.phase == "prepare" and any(
        getattr(args, name) is None
        for name in (
            "manifest",
            "records",
            "settings",
            "raw_configuration",
            "learning_run_id",
            "observed_through",
        )
    ):
        parser.error(
            "prepare requires source, settings, raw configuration and explicit learning cutoff"
        )
    asyncio.run(prepare(args) if args.phase == "prepare" else read(args))
