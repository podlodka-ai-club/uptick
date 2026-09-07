"""Offline operation-chain probe over a validated frozen public export.

This adapter interprets only public operation status. It never promotes a
candidate, opens the source database, or sends data to a model or simulator.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from probe_memory_evidence import read_frozen_export

from uptick_agent.memory.contracts import ExperienceTransition
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.stores.contracts import MemorySnapshot, SnapshotMember


def public_operation_status(transition: ExperienceTransition, operation_id: str) -> str | None:
    result = transition.result
    data = result.get("data")
    if result.get("ok") is not True or not isinstance(data, dict):
        return None
    if data.get("operation_id") != operation_id:
        return None
    status = data.get("status")
    if status in ("succeeded", "failed"):
        return status
    if status in ("queued", "running", "pending"):
        return "pending"
    return None


def load_evidence(source: Path) -> tuple[LessonEvidence, str]:
    _, digest, records = read_frozen_export(
        source / "corpus-manifest.json", source / "corpus-records.jsonl"
    )
    snapshot = MemorySnapshot.create(
        snapshot_id=f"operation-chain-probe:{digest}",
        namespace=records[0].namespace,
        members=[
            SnapshotMember(record_id=r.record_id, content_hash=r.content_hash)
            for r in sorted(records, key=lambda r: r.record_id)
        ],
    )
    return LessonEvidence(snapshot=snapshot, records=records, runs=[]), digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cutoff", type=datetime.fromisoformat, required=True)
    parser.add_argument("--max-iteration-gap", type=int, default=32)
    args = parser.parse_args()
    # Integration follows the generic extractor API; no adapter facts enter core.
    from uptick_agent.memory.operation_chains import extract_operation_chains

    evidence, digest = load_evidence(args.source)
    chains = extract_operation_chains(
        evidence,
        learning_cutoffs={args.run_id: args.cutoff},
        max_iteration_gap=args.max_iteration_gap,
        resolve_status=public_operation_status,
    )
    args.output.mkdir(parents=True, exist_ok=False)
    payload = [c.model_dump(mode="json") for c in chains]
    (args.output / "chains.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    report = {
        "source_sha256": digest,
        "run_id": args.run_id,
        "cutoff": args.cutoff.isoformat(),
        "max_iteration_gap": args.max_iteration_gap,
        "candidate_count": len(payload),
        "causal_credit": False,
        "promoted": False,
        "model_calls": 0,
        "simulator_calls": 0,
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
