"""Development-only revision probe on real recorded public observations.

The deliberately broad action projection groups different probe pages. Counts
describe recorded responses, not a causal rule or reliability estimate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from uptick_agent.memory.contracts import MemoryContextRequest
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.settings import PatternQuerySettings
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.memory.world_model import WorldModelMemory


async def probe(source: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    preparation = json.loads((source / "prepare.json").read_text())
    database = output / "memory.sqlite3"
    with (
        sqlite3.connect(f"file:{(source / 'memory.sqlite3').resolve()}?mode=ro", uri=True) as src,
        sqlite3.connect(database) as dst,
    ):
        src.backup(dst)
    settings = PatternQuerySettings(
        scope_paths=("observation.ok",), action_path="action.kind", result_path="result.ok"
    )
    store = SqliteStructuredStore(database)
    records = await store.list(namespace=preparation["namespace"])
    snapshot = await store.get_snapshot(
        snapshot_id=f"observed-source:{preparation['source_sha256']}"
    )
    assert snapshot is not None
    evidence = LessonEvidence(snapshot=snapshot, records=records, runs=[])
    matching = sorted(
        [
            r
            for r in records
            if r.record_type == "experience-transition"
            and r.payload["run_id"] == preparation["source_run_id"]
            and r.payload["action"]["kind"] == "probe_page"
            and r.payload["observation"].get("ok") is True
        ],
        key=lambda r: r.created_at,
    )
    first = matching[0]
    counter = next(r for r in matching if r.payload["result"]["ok"] is False)
    assert first.created_at < counter.created_at and first.payload["result"]["ok"] is True
    phases = (
        ("prefix", first.created_at),
        ("counter", counter.created_at),
        ("full", datetime.fromisoformat(preparation["observed_through"])),
    )
    report = {}
    previous_ids = set()
    for label, cutoff in phases:
        writer = WorldModelMemory(
            SqliteStructuredStore(database),
            namespace="revision-world",
            source=None,
            settings=settings,
            allow_observed_summaries=True,
        )
        await writer.record_observed(
            evidence, {preparation["source_run_id"]: cutoff}, idempotency_key=label
        )
        reader = WorldModelMemory(
            SqliteStructuredStore(database),
            namespace="revision-world",
            source=None,
            settings=settings,
            allow_observed_summaries=True,
        )
        contribution = await reader.retrieve(
            MemoryContextRequest(
                request_id=f"revision-{label}",
                run_id="offline-revision-probe",
                query="probe_page",
                context={"observation": {"ok": True}},
            )
        )
        items = [
            i
            for i in contribution.items
            if i.envelope.item["candidate"]["action_kind"] == "probe_page"
        ]
        selected = [r for r in matching if r.created_at <= cutoff]
        counts = {
            value: sum(r.payload["result"]["ok"] is value for r in selected)
            for value in (True, False)
        }
        assert len(items) == sum(count > 0 for count in counts.values())
        ids = {i.envelope.item_id for i in items}
        assert not ids & previous_ids, "superseded count summaries remained visible"
        previous_ids = ids
        for item in items:
            payload = item.envelope.item
            result = payload["candidate"]["result_value"]
            assert payload["support_count"] == counts[result]
            assert payload["counter_count"] == counts[not result]
            assert payload["unknown_result_count"] == 0
        (output / f"context-{label}.json").write_text(contribution.model_dump_json(indent=2) + "\n")
        report[label] = {
            "cutoff": cutoff.isoformat(),
            "observed_successes": counts[True],
            "observed_failures": counts[False],
            "visible_probe_summaries": len(items),
            "superseded_summary_ids_excluded": True,
        }
    report["limitations"] = [
        "Development corpus and query selected after inspection; not held-out.",
        "Different pages/resources are grouped; no causal or universal reliability claim.",
        "SQLite handles reopened within one process; no provider or simulator called.",
    ]
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.source, args.output))
