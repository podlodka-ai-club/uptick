"""Offline evidence diagnostics; never invent run declarations or learning outcomes.

Reads the frozen raw JSONL export and its manifest. Writes a new diagnostic
snapshot, counts and explicitly selected unaccepted temporal proposals. A
snapshot identifies observed bytes, not the hidden environment. No simulator,
model, or SQLite connection is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from uptick_agent.memory.associations import extract_temporal_associations
from uptick_agent.memory.candidate_validation import (
    _validate_outcome_record,
    _validate_transition_record,
    validate_evidence,
)
from uptick_agent.memory.contracts import MemoryValidationError
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.observed_patterns import (
    ObservedPatternSummary,
    generate_observed_pattern_candidates,
    validate_observed_pattern,
    verify_observed_pattern_summary,
)
from uptick_agent.memory.settings import PatternQuerySettings
from uptick_agent.memory.stores.contracts import MemorySnapshot, SnapshotMember, StoredRecord


def read_frozen_export(
    manifest_path: Path, records_path: Path
) -> tuple[bytes, str, list[StoredRecord]]:
    """Verify the frozen export once; never open its source SQLite database."""
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    raw = records_path.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    if source_hash != manifest["corpus_records_sha256"]:
        raise ValueError("raw export does not match the frozen manifest")
    records = []
    for line in raw.splitlines():
        row = json.loads(line)
        record = StoredRecord(
            schema_version=row["schema_version"],
            namespace=row["source_namespace"],
            record_id=row["record_id"],
            record_type=row["record_type"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
            content_hash=row["content_hash"],
        )
        records.append(StoredRecord.validate_integrity(record))
    if len(records) != manifest["record_count"]:
        raise ValueError("record count does not match the frozen manifest")
    namespaces = {record.namespace for record in records}
    if len(namespaces) != 1:
        raise ValueError("probe requires one explicit source namespace")
    if len({record.record_id for record in records}) != len(records):
        raise ValueError("duplicate source record ID")
    return manifest_bytes, source_hash, records


def probe(
    manifest_path: Path,
    records_path: Path,
    output: Path,
    *,
    learning_cutoffs: dict[str, datetime] | None = None,
    max_iteration_gap: int = 32,
    pattern_settings: PatternQuerySettings | None = None,
) -> dict:
    manifest_bytes, source_hash, records = read_frozen_export(manifest_path, records_path)
    transitions, outcomes = [], []
    for record in records:
        if record.record_type == "experience-transition":
            transitions.append(_validate_transition_record(record))
        elif record.record_type == "run-outcome":
            outcomes.append(_validate_outcome_record(record))
        else:
            raise ValueError("unexpected source record type")
    snapshot = MemorySnapshot.create(
        snapshot_id=f"offline-evidence:{source_hash}",
        namespace=records[0].namespace,
        members=[
            SnapshotMember(record_id=r.record_id, content_hash=r.content_hash)
            for r in sorted(records, key=lambda r: r.record_id)
        ],
    )
    MemorySnapshot.validate_integrity(snapshot)
    evidence = LessonEvidence(snapshot=snapshot, records=records, runs=[])
    associations = extract_temporal_associations(
        evidence,
        learning_cutoffs=learning_cutoffs or {},
        max_iteration_gap=max_iteration_gap,
    )
    summaries = []
    if pattern_settings is not None:
        candidates = generate_observed_pattern_candidates(
            evidence, pattern_settings, learning_cutoffs=learning_cutoffs or {}
        )
        summaries = [
            validate_observed_pattern(
                candidate, evidence, pattern_settings, learning_cutoffs=learning_cutoffs or {}
            )
            for candidate in candidates
        ]
    try:
        validate_evidence(evidence)
    except MemoryValidationError as error:
        gate = {"status": "rejected", "reason": str(error)}
    else:
        gate = {"status": "accepted", "reason": None}
    metrics = Counter()
    deltas = Counter()
    for transition in transitions:
        metrics.update(f"{m.name} [{m.unit}]" for m in transition.objective_metrics)
        deltas.update(f"{m.name} [{m.unit}]" for m in transition.objective_deltas)
    report = {
        "claim_scope": "offline input integrity and gate diagnosis; no utility claim",
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "records_sha256": source_hash,
        "records": len(records),
        "transitions": len(transitions),
        "outcomes": dict(Counter(o.status for o in outcomes)),
        "record_integrity_and_source_provenance": "passed",
        "snapshot_integrity": "passed",
        "snapshot_content_hash": snapshot.content_hash,
        "snapshot_is_hidden_world_identity": False,
        "invented_declarations": 0,
        "lesson_evidence_gate": gate,
        "null_environment_ids": sum(t.environment_id is None for t in transitions),
        "null_scenario_ids": sum(t.scenario_id is None for t in transitions),
        "transitions_with_metrics": sum(bool(t.objective_metrics) for t in transitions),
        "transitions_with_deltas": sum(bool(t.objective_deltas) for t in transitions),
        "metrics": dict(sorted(metrics.items())),
        "deltas": dict(sorted(deltas.items())),
        "temporal_association_proposals": len(associations),
        "association_learning_cutoffs": {
            run_id: cutoff.isoformat() for run_id, cutoff in (learning_cutoffs or {}).items()
        },
        "association_max_iteration_gap": max_iteration_gap,
        "association_metric_counts": dict(
            sorted(Counter(a.metric_name for a in associations).items())
        ),
        "association_activation": "not permitted; unaccepted temporal proposals only",
        "observed_pattern_settings": (
            pattern_settings.model_dump(mode="json") if pattern_settings is not None else None
        ),
        "observed_pattern_summary_count": len(summaries),
        "observed_pattern_statuses": dict(Counter(s.status for s in summaries)),
        "world_pattern_activation": "unchanged strict policy; descriptive summaries are separate",
    }
    # A new directory prevents overwriting the inputs or a previous result.
    output.mkdir(parents=True, exist_ok=False)
    (output / "snapshot.json").write_text(snapshot.model_dump_json(indent=2) + "\n")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "associations.jsonl").write_text(
        "".join(association.model_dump_json() + "\n" for association in associations)
    )
    summary_path = output / "observed-pattern-summaries.jsonl"
    summary_path.write_text("".join(summary.model_dump_json() + "\n" for summary in summaries))
    if summaries:
        reopened = ObservedPatternSummary.model_validate_json(
            summary_path.read_text().splitlines()[0]
        )
        verify_observed_pattern_summary(reopened, evidence)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--learning-run-id", action="append", default=[])
    parser.add_argument("--observed-through", type=datetime.fromisoformat)
    parser.add_argument("--max-iteration-gap", type=int, default=32)
    parser.add_argument("--pattern-settings", type=Path)
    args = parser.parse_args()
    if bool(args.learning_run_id) != bool(args.observed_through):
        parser.error("learning-run-id and observed-through must be supplied together")
    print(
        json.dumps(
            probe(
                args.manifest,
                args.records,
                args.output,
                learning_cutoffs={run: args.observed_through for run in args.learning_run_id},
                max_iteration_gap=args.max_iteration_gap,
                pattern_settings=(
                    PatternQuerySettings.model_validate_json(args.pattern_settings.read_text())
                    if args.pattern_settings
                    else None
                ),
            ),
            indent=2,
        )
    )
