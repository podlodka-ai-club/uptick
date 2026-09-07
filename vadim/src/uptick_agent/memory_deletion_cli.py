"""Plan and explicitly apply physical deletion of low-value memory rows.

This command is intentionally separate from ``memory_maintenance_cli``.  A
dry run writes a sealed, payload-free plan.  ``--apply`` uses that exact plan;
it never recomputes candidates from the current store.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from uptick_agent.memory.contracts import (
    MemoryConflictError,
    MemoryContractError,
    MemoryValidationError,
)
from uptick_agent.memory.deletion import (
    DEFAULT_TOMBSTONE_NAMESPACE,
    DeletionPolicy,
    PhysicalDeletionManager,
    PhysicalDeletionPlan,
)
from uptick_agent.memory.deletion_contracts import RecordRef
from uptick_agent.memory.stores.sqlite import SqliteStructuredStore
from uptick_agent.redaction import sanitize_json


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m uptick_agent.memory_deletion_cli",
        description="Plan or explicitly apply policy-mediated physical memory deletion.",
    )
    parser.add_argument("--sqlite-path", "--store", dest="sqlite_path", type=Path, required=True)
    parser.add_argument("--plan-path", type=Path, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--authorization-namespace", default=None)
    parser.add_argument("--authorization-id", default=None)
    parser.add_argument("--policy-id", default="project-memory-retention")
    parser.add_argument("--policy-version", default="1.0")
    parser.add_argument("--minimum-raw-age-days", type=int, default=90)
    parser.add_argument("--tombstone-namespace", default=DEFAULT_TOMBSTONE_NAMESPACE)
    parser.add_argument("--idempotency-key", default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the exact sealed plan at --plan-path instead of creating a dry run",
    )
    return parser


def _write_sealed_plan(path: Path, plan: PhysicalDeletionPlan) -> None:
    """Publish a plan atomically without replacing a concurrent writer's file."""

    payload = (plan.model_dump_json(indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() != payload:
            raise MemoryConflictError("plan path already contains a different sealed plan")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise MemoryConflictError(
                    "plan path already contains a different sealed plan"
                ) from None
        else:
            temporary.unlink()
            temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _authorization_ref(args: argparse.Namespace, inventory) -> RecordRef:
    if not args.authorization_namespace or not args.authorization_id:
        raise MemoryValidationError(
            "dry-run requires --authorization-namespace and --authorization-id"
        )
    record = next(
        (
            item
            for item in inventory.records
            if item.namespace == args.authorization_namespace
            and item.record_id == args.authorization_id
        ),
        None,
    )
    if record is None:
        raise MemoryConflictError("owner authorization record is missing from the store")
    return RecordRef(
        namespace=record.namespace,
        record_id=record.record_id,
        content_hash=record.content_hash,
    )


async def _main(args: argparse.Namespace) -> int:
    store = SqliteStructuredStore(args.sqlite_path)
    if args.apply:
        try:
            plan = PhysicalDeletionPlan.model_validate_json(args.plan_path.read_text())
        except (OSError, ValueError, TypeError) as error:
            raise MemoryValidationError("sealed deletion plan could not be read") from error
        manager = PhysicalDeletionManager(
            store,
            policy=plan.policy,
            tombstone_namespace=plan.tombstone_namespace,
        )
        key = args.idempotency_key or f"physical-delete:{args.request_id}"
        receipt = await manager.apply(plan, idempotency_key=key)
        print(json.dumps(sanitize_json(receipt.model_dump(mode="json")), indent=2, sort_keys=True))
        return 0

    inventory = await store.deletion_inventory()
    policy = DeletionPolicy(
        policy_id=args.policy_id,
        policy_version=args.policy_version,
        minimum_raw_age_days=args.minimum_raw_age_days,
        authorization=_authorization_ref(args, inventory),
    )
    manager = PhysicalDeletionManager(
        store,
        policy=policy,
        tombstone_namespace=args.tombstone_namespace,
    )
    plan = await manager.create_plan(request_id=args.request_id)
    _write_sealed_plan(args.plan_path, plan)
    print(json.dumps(sanitize_json(plan.model_dump(mode="json")), indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except (MemoryContractError, OSError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    main()
