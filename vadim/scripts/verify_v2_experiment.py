"""Verify one persisted v2 experiment without starting any external client."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from uptick_agent.evaluation import (
    V2AttemptRecord,
    V2Manifest,
    V2Report,
    sha256_json,
    verify_report,
)
from uptick_agent.evaluation_runtime import (
    EvaluationJournal,
    InMemoryEvaluationArtifactStore,
    LifecycleEvent,
    SnapshotReadStore,
)
from uptick_agent.memory.contracts import MemoryContractError
from uptick_agent.memory.stores.sqlite import SqliteStructuredStore
from uptick_agent.redaction import sanitize_json


def _require_artifact_root(root: Path) -> None:
    if not root.exists() or not root.is_dir():
        raise ValueError(f"experiment artifact directory is missing: {root}")
    required_files = ("manifest.json", "report.json", "lifecycle.jsonl", "memory.sqlite3")
    for name in required_files:
        path = root / name
        if not path.is_file():
            raise ValueError(f"experiment artifact is missing: {path}")
    if (root / "memory.sqlite3").stat().st_size == 0:
        raise ValueError("experiment memory database is empty")
    artifacts = root / "artifacts"
    if not artifacts.is_dir():
        raise ValueError(f"experiment artifact directory is missing: {artifacts}")


def _read_artifacts(root: Path) -> dict[tuple[str, str], dict[str, object]]:
    artifacts_root = root / "artifacts"
    found: dict[tuple[str, str], dict[str, object]] = {}
    for kind_dir in sorted(artifacts_root.iterdir(), key=lambda path: path.name):
        if not kind_dir.is_dir():
            continue
        for path in sorted(kind_dir.glob("*.json"), key=lambda item: item.name):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError(f"invalid artifact JSON: {path}") from error
            if not isinstance(payload, dict) or set(payload) != {"artifact_id", "hash", "value"}:
                raise ValueError(f"invalid artifact wrapper: {path}")
            artifact_id = payload["artifact_id"]
            digest = payload["hash"]
            value = payload["value"]
            if not isinstance(artifact_id, str) or not isinstance(digest, str):
                raise ValueError(f"artifact identity is invalid: {path}")
            expected_name = f"{sha256_json({'id': artifact_id})}.json"
            if path.name != expected_name:
                raise ValueError(f"artifact filename does not match its ID: {path}")
            if not isinstance(value, dict) or sha256_json(value) != digest:
                raise ValueError(f"artifact hash does not match its value: {path}")
            key = (kind_dir.name, artifact_id)
            if key in found:
                raise ValueError(f"duplicate artifact identity: {kind_dir.name}/{artifact_id}")
            found[key] = payload
    return found


def _verify_lifecycle(
    root: Path, manifest: V2Manifest, report: V2Report
) -> tuple[LifecycleEvent, ...]:
    try:
        lines = (root / "lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError("cannot read lifecycle journal") from error
    if not lines:
        raise ValueError("lifecycle journal is empty")
    try:
        events = tuple(LifecycleEvent.model_validate_json(line) for line in lines)
    except ValueError as error:
        raise ValueError("lifecycle journal contains an invalid event") from error
    if tuple(event.sequence for event in events) != tuple(range(len(events))):
        raise ValueError("lifecycle journal sequence is not contiguous")
    if any(event.attempt.manifest_id != manifest.manifest_id for event in events):
        raise ValueError("lifecycle journal contains an attempt from another manifest")

    replay = EvaluationJournal(manifest, artifacts=InMemoryEvaluationArtifactStore())
    try:
        for event in events:
            replay.append(event.attempt)
    except (MemoryContractError, ValueError) as error:
        raise ValueError("lifecycle journal contains an invalid state transition") from error

    latest = {event.attempt.attempt_id: event.attempt for event in events}
    expected = {attempt.attempt_id: attempt for attempt in report.retained_attempts}
    if len(expected) != len(report.retained_attempts) or latest.keys() != expected.keys():
        raise ValueError("lifecycle final attempts do not match the report")
    if any(
        latest[attempt_id].model_dump(mode="json") != attempt.model_dump(mode="json")
        for attempt_id, attempt in expected.items()
    ):
        raise ValueError("lifecycle final attempt state does not match the report")
    return events


def _require_artifact(
    artifacts: dict[tuple[str, str], dict[str, object]],
    *,
    kind: str,
    artifact_id: str,
    expected_hash: str,
) -> dict[str, object]:
    artifact = artifacts.get((kind, artifact_id))
    if artifact is None:
        raise ValueError(f"report references missing {kind} artifact for {artifact_id}")
    if artifact["hash"] != expected_hash:
        raise ValueError(f"report {kind} hash does not match its durable artifact: {artifact_id}")
    return artifact


def _verify_artifact_links(
    artifacts: dict[tuple[str, str], dict[str, object]],
    manifest: V2Manifest,
    report: V2Report,
) -> None:
    report_artifact = artifacts.get(("report", manifest.manifest_id))
    if report_artifact is not None and report_artifact["value"] != report.model_dump(mode="json"):
        raise ValueError("durable report artifact does not match report.json")
    for attempt in report.retained_attempts:
        if attempt.result_hash is not None:
            _require_artifact(
                artifacts,
                kind="run_result",
                artifact_id=attempt.attempt_id,
                expected_hash=attempt.result_hash,
            )
        if attempt.trace_hash is not None:
            trace = _require_artifact(
                artifacts,
                kind="trace",
                artifact_id=attempt.attempt_id,
                expected_hash=attempt.trace_hash,
            )
            startup = trace["value"].get("startup_artifacts", {})
            if not isinstance(startup, dict):
                raise ValueError("invalid startup artifact links in trace")
            for kind, digest in startup.items():
                if kind not in {"startup_observation", "startup_spec"}:
                    raise ValueError("unknown startup artifact kind")
                _require_artifact(
                    artifacts,
                    kind=kind,
                    artifact_id=attempt.attempt_id,
                    expected_hash=digest,
                )
    for binding in report.frozen_bindings:
        artifact = _require_artifact(
            artifacts,
            kind="binding",
            artifact_id=binding.binding_id,
            expected_hash=sha256_json(binding.model_dump(mode="json")),
        )
        if artifact["value"] != binding.model_dump(mode="json"):
            raise ValueError(
                f"durable binding artifact does not match report: {binding.binding_id}"
            )


def _check_sqlite_schema(path: Path) -> None:
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
    except sqlite3.Error as error:
        raise ValueError(f"experiment memory database cannot be read: {path}") from error
    required = {
        "memory_schema",
        "memory_records",
        "memory_snapshots",
        "memory_snapshot_members",
    }
    if not required.issubset(tables):
        raise ValueError("experiment memory database has no initialized structured-memory schema")


async def _snapshot_counts(root: Path, report: V2Report) -> dict[str, dict[str, object]]:
    database = root / "memory.sqlite3"
    _check_sqlite_schema(database)
    store = SqliteStructuredStore(database)
    counts: dict[str, dict[str, object]] = {}
    binding_ids = {binding.binding_id for binding in report.frozen_bindings}
    if len(binding_ids) != len(report.frozen_bindings):
        raise ValueError("report contains duplicate frozen binding IDs")
    for binding in report.frozen_bindings:
        reader = SnapshotReadStore(store, binding.snapshot_refs)
        await reader.load()
        actual = reader.member_count
        reports = {
            attempt.attempt_id: attempt.memory_telemetry.snapshot_members
            for attempt in report.retained_attempts
            if attempt.frozen_binding_id == binding.binding_id
        }
        counts[binding.binding_id] = {"actual": actual, "reported": reports}

    for attempt in report.retained_attempts:
        reported = attempt.memory_telemetry.snapshot_members
        if reported is None:
            continue
        binding_id = attempt.frozen_binding_id
        if binding_id is None or binding_id not in counts:
            raise ValueError(
                f"attempt {attempt.attempt_id} reports snapshot members without a binding"
            )
        actual = counts[binding_id]["actual"]
        if reported != actual:
            raise ValueError(
                f"attempt {attempt.attempt_id} reports {reported} snapshot members; "
                f"the frozen binding contains {actual}"
            )
    return counts


def _attempt_summary(attempt: V2AttemptRecord) -> dict[str, object]:
    outcome = attempt.outcome
    return {
        "condition": attempt.condition_id,
        "phase": attempt.phase,
        "seed": attempt.world_seed,
        "run_id": attempt.run_id,
        "status": attempt.status,
        "steps": outcome.steps if outcome else None,
        "uptime": outcome.uptime_ratio if outcome else None,
        "slo": outcome.slo_passed if outcome else None,
        "context_items": attempt.memory_telemetry.context_items,
        "context_units": attempt.memory_telemetry.context_tokens,
        "reported_snapshot_members": attempt.memory_telemetry.snapshot_members,
        "provider_requests": attempt.provider_telemetry.request_count,
        "provider_tokens": attempt.provider_telemetry.total_tokens,
        "failure_reason": attempt.failure_reason,
    }


def verify_experiment(root: Path, *, include_attempts: bool = False) -> dict[str, object]:
    """Verify persisted evidence and return a redacted compact summary."""

    root = root.resolve()
    _require_artifact_root(root)
    try:
        manifest = V2Manifest.model_validate_json(
            (root / "manifest.json").read_text(encoding="utf-8")
        )
        report = V2Report.model_validate_json((root / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("manifest or report is invalid") from error
    verify_report(manifest, report)
    events = _verify_lifecycle(root, manifest, report)
    artifacts = _read_artifacts(root)
    _verify_artifact_links(artifacts, manifest, report)
    try:
        snapshot_counts = asyncio.run(_snapshot_counts(root, report))
    except (MemoryContractError, OSError, ValueError, RuntimeError) as error:
        raise ValueError("frozen memory snapshots could not be verified") from error

    summary: dict[str, object] = {
        "manifest_id": manifest.manifest_id,
        "manifest_hash": manifest.manifest_hash,
        "source_revision": manifest.profile.source.source_revision,
        "source_dirty": manifest.profile.source.source_dirty,
        "conditions": len(manifest.profile.conditions),
        "declared_cells": sum(len(block.conditions) for block in manifest.run_matrix),
        "total_attempts": report.total_attempts,
        "coverage_complete": report.coverage_complete,
        "status_counts": report.status_counts,
        "slo_passes": sum(
            attempt.outcome is not None
            and attempt.status == "completed"
            and attempt.outcome.slo_passed is True
            for attempt in report.retained_attempts
        ),
        "lifecycle_events": len(events),
        "verified_artifacts": len(artifacts),
        "snapshot_member_counts": snapshot_counts,
        "evidence_incompleteness_reasons": report.evidence_incompleteness_reasons,
    }
    if include_attempts:
        summary["per_attempt"] = [_attempt_summary(attempt) for attempt in report.retained_attempts]
    safe = sanitize_json(summary)
    if not isinstance(safe, dict):
        raise ValueError("verification summary is not a JSON object")
    return safe


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_directory", type=Path)
    parser.add_argument(
        "--per-attempt",
        action="store_true",
        help="include one compact record for each retained attempt",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        summary = verify_experiment(args.artifact_directory, include_attempts=args.per_attempt)
    except (MemoryContractError, OSError, TypeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(summary, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
