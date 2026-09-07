"""Maintenance manifests and reversible score decay on copied public records.

Run prepare and read in separate processes. No physical deletion, provider,
simulator or newly accepted learning is involved.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.contracts import MemoryContextRequest
from uptick_agent.memory.maintenance import MemoryMaintenance
from uptick_agent.memory.stores import SqliteStructuredStore


def members_digest(records):
    return hashlib.sha256(
        json.dumps(
            sorted((r.record_id, r.content_hash) for r in records), separators=(",", ":")
        ).encode()
    ).hexdigest()


async def prepare(args):
    args.output.mkdir(parents=True, exist_ok=False)
    metadata = json.loads((args.source / "prepare.json").read_text())
    with (
        sqlite3.connect(
            f"file:{(args.source / 'memory.sqlite3').resolve()}?mode=ro", uri=True
        ) as src,
        sqlite3.connect(args.output / "memory.sqlite3") as dst,
    ):
        src.backup(dst)
    store = SqliteStructuredStore(args.output / "memory.sqlite3")
    namespace = metadata["namespace"]
    records = await store.list(namespace=namespace)
    digest = members_digest(records)
    instant = max(r.created_at for r in records) + timedelta(days=30)
    snapshot = await store.create_snapshot(
        namespace=namespace,
        snapshot_id="stage07-maintenance-source",
        operation="freeze-maintenance-probe",
        idempotency_key="stage07-freeze",
    )
    maintenance = MemoryMaintenance(store, namespace=namespace, clock=lambda: instant)
    plan = await maintenance.create_plan(snapshot.snapshot.snapshot_id, request_id="stage07-plan")
    assert members_digest(await store.list(namespace=namespace)) == digest
    applied = await maintenance.apply(plan, idempotency_key="stage07-apply", active_holds=())
    repeated = await maintenance.apply(plan, idempotency_key="stage07-apply", active_holds=())
    assert repeated.already_applied
    assert members_digest(await store.list(namespace=namespace)) == digest
    config = json.loads(args.configuration.read_text())["configuration"]
    (args.output / "configuration.json").write_text(json.dumps(config, indent=2) + "\n")
    (args.output / "plan.json").write_text(plan.model_dump_json(indent=2) + "\n")
    (args.output / "application.json").write_text(applied.model_dump_json(indent=2) + "\n")
    report = {
        "namespace": namespace,
        "source_members_digest": digest,
        "source_records": len(records),
        "clock": instant.isoformat(),
        "clock_policy": "fixed latest source record timestamp plus 30 days; diagnostic ageing",
        "plan_id": plan.plan_id,
        "operations": dict(Counter(d.operation for d in plan.deltas)),
        "blocked_deltas": len(plan.blocked_delta_ids),
        "warnings": plan.warnings,
        "source_unchanged_after_plan_and_apply": True,
        "idempotent_apply": True,
        "physical_deletions": 0,
        "model_calls": 0,
        "simulator_calls": 0,
    }
    (args.output / "prepare.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


async def read(args):
    destination = args.output / "read-report.json"
    if destination.exists():
        raise FileExistsError("read evidence already exists")
    metadata = json.loads((args.output / "prepare.json").read_text())
    raw = json.loads((args.output / "configuration.json").read_text())
    store = SqliteStructuredStore(args.output / "memory.sqlite3")
    instant = datetime.fromisoformat(metadata["clock"])
    namespace = metadata["namespace"]
    summaries = {}
    selections = {}
    for label, enabled, decay in (
        ("off", False, False),
        ("view", True, False),
        ("decay", True, True),
        ("restored", False, False),
    ):
        payload = json.loads(json.dumps(raw))
        payload["profile_kind"] = "experiment"
        payload["profile_id"] = "stage07-" + label
        payload["forgetting"]["enabled"] = enabled
        payload["forgetting_settings"] = {"decay_days": 30.0, "apply_decay": decay}
        config = MemoryConfiguration.model_validate(payload)
        runtime = compose_experimental_runtime(
            config,
            store,
            namespace=namespace,
            clock=lambda: instant,
        )
        context = await runtime.build_context(
            MemoryContextRequest(
                request_id="maintenance-probe-" + label,
                run_id="offline-maintenance-query",
                query="server.create running",
                context={"iteration": 1, "latest_result": {"ok": True}},
            )
        )
        (args.output / ("context-" + label + ".json")).write_text(
            context.model_dump_json(indent=2) + "\n"
        )
        selections[label] = [
            (item.envelope.item_id, item.score, item.envelope.model_dump(mode="json"))
            for item in context.items
        ]
        summaries[label] = {
            "items": len(context.items),
            "warnings": context.warnings,
            "scores": [item.score for item in context.items],
            "ids": [item.envelope.item_id for item in context.items],
            "configuration_fingerprint": config.fingerprint,
        }
    assert selections["off"], "empty retrieval does not prove reversible decay"
    assert selections["off"] == selections["restored"]
    actual_digest = members_digest(await store.list(namespace=namespace))
    assert actual_digest == metadata["source_members_digest"]
    report = {
        "separate_process_read": True,
        "arms": summaries,
        "source_unchanged": True,
        "disabled_restores_exact_items_scores_envelopes": True,
        "maintenance_view_preserves_items_scores_envelopes": (
            selections["off"] == selections["view"]
        ),
        "decay_changes_items_or_scores": selections["off"] != selections["decay"],
        "physical_deletions": 0,
        "model_calls": 0,
        "simulator_calls": 0,
        "limitation": (
            "Retrieval mechanics at a fixed diagnostic age; no claim of utility or compression. "
            "Manifest summary operations are not assumed to materialize retrievable summaries."
        ),
    }
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "read"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--configuration", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "prepare" and (args.source is None or args.configuration is None):
        parser.error("prepare requires source and configuration")
    asyncio.run(prepare(args) if args.phase == "prepare" else read(args))
