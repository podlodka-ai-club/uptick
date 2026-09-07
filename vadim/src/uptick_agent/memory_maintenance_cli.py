"""Explicit command for planning and applying structured-memory maintenance.

The command is deliberately separate from the agent runner.  It creates a
snapshot-bound dry-run manifest by default; ``--apply`` loads that persisted
manifest by request and snapshot identity and applies it idempotently.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from uptick_agent.memory.contracts import (
    ConsolidationRequest,
    MemoryConflictError,
    MemoryContractError,
    MemoryValidationError,
)
from uptick_agent.memory.maintenance import MemoryMaintenance
from uptick_agent.memory.stores.sqlite import SqliteStructuredStore
from uptick_agent.redaction import sanitize_json


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m uptick_agent.memory_maintenance_cli",
        description="Plan or explicitly apply archive-preserving memory maintenance.",
    )
    parser.add_argument(
        "--sqlite-path",
        "--store",
        dest="sqlite_path",
        type=Path,
        required=True,
        help="SQLite structured-memory store path",
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument(
        "--idempotency-key",
        default=None,
        help="Apply idempotency key; defaults to maintenance:<request-id>",
    )
    parser.add_argument("--maintenance-namespace", default=None)
    parser.add_argument(
        "--plan-id",
        default=None,
        help="Optional persisted plan hash to verify before --apply",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the exact persisted dry-run plan instead of creating a dry run",
    )
    return parser


async def _main(args: argparse.Namespace) -> int:
    if args.plan_id is not None and not args.apply:
        raise MemoryValidationError("--plan-id requires --apply")
    key = f"maintenance:{args.request_id}" if args.idempotency_key is None else args.idempotency_key
    request = ConsolidationRequest(
        request_id=args.request_id,
        snapshot_id=args.snapshot_id,
        idempotency_key=key,
        dry_run=not args.apply,
    )
    store = SqliteStructuredStore(args.sqlite_path)
    maintenance = MemoryMaintenance(
        store,
        namespace=args.namespace,
        maintenance_namespace=args.maintenance_namespace,
    )
    if args.apply and args.plan_id is not None:
        persisted = await maintenance.load_persisted_plan(request)
        if persisted.plan_id != args.plan_id:
            raise MemoryConflictError("persisted maintenance plan ID does not match --plan-id")
    result = await maintenance.consolidate(request)
    print(json.dumps(sanitize_json(result.model_dump(mode="json")), indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except (MemoryContractError, OSError, ValueError) as error:
        _parser().error(str(error))
    return 2


if __name__ == "__main__":
    main()
