from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from uptick_agent.memory.stores import RecordWrite
from uptick_agent.memory.stores.sqlite import SqliteStructuredStore
from uptick_agent.memory_maintenance_cli import _main, _parser


def _run(awaitable):
    return asyncio.run(awaitable)


def test_cli_dry_run_then_applies_the_persisted_plan_by_plan_id(tmp_path, capsys) -> None:
    path = tmp_path / "memory.sqlite"
    store = SqliteStructuredStore(path)
    _run(
        store.append(
            RecordWrite(
                namespace="toy",
                record_id="episode",
                record_type="experience-transition",
                payload={"observation": {"status": "ok"}},
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            operation="seed",
            idempotency_key="seed-1",
        )
    )
    snapshot = _run(
        store.create_snapshot(
            namespace="toy",
            snapshot_id="snapshot-1",
            operation="freeze",
            idempotency_key="freeze-1",
        )
    ).snapshot
    common = [
        "--sqlite-path",
        str(path),
        "--namespace",
        "toy",
        "--snapshot-id",
        snapshot.snapshot_id,
        "--request-id",
        "maintenance-1",
        "--idempotency-key",
        "apply-1",
    ]

    _run(_main(_parser().parse_args(common)))
    dry_run = json.loads(capsys.readouterr().out)
    plan_id = dry_run["deltas"][0]["payload"]["plan"]["plan_id"]
    assert dry_run["applied"] is False

    _run(_main(_parser().parse_args([*common, "--apply", "--plan-id", plan_id])))
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True
    assert applied["deltas"][0]["payload"]["plan"]["plan_id"] == plan_id
