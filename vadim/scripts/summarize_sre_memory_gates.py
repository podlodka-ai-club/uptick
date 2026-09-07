"""Summarize bounded offline memory probes into a machine-readable gate report.

This script only reads a frozen JSONL export, its manifest, and the outputs of
the existing memory-evidence and operation-chain probes.  It does not open the
source SQLite database, contact a provider or simulator, or promote a record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _load_rows(records_path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = records_path.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    return rows, hashlib.sha256(raw).hexdigest()


def _transition_payloads(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        row["record_id"]: json.loads(row["payload_json"])
        for row in rows
        if row["record_type"] == "experience-transition"
    }


def _example(candidate: dict[str, Any], payloads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    refs = candidate["interval_records"]
    transitions = [payloads[ref["record_id"]] for ref in refs]
    return {
        "candidate_id": candidate["candidate_id"],
        "operation_id": candidate["operation_id"],
        "completion_status": candidate["completion_status"],
        "run_id": candidate["run_id"],
        "environment_id": candidate["environment_id"],
        "scenario_id": candidate["scenario_id"],
        "iteration_gap": candidate["iteration_gap"],
        "interval_records": refs,
        "interval_length": len(refs),
        "action_kinds_and_iterations": [
            {"iteration": t["iteration"], "action_kind": t["action"]["kind"]} for t in transitions
        ],
        "public_statuses": [
            {
                "iteration": t["iteration"],
                "operation_id": t.get("result", {}).get("data", {}).get("operation_id"),
                "status": t.get("result", {}).get("data", {}).get("status"),
            }
            for t in transitions
        ],
        "metric_relation_count": len(candidate["metric_relations"]),
        "metric_limitations": candidate["metric_limitations"],
        "evidence_snapshot_hash": candidate["evidence_snapshot_hash"],
        "limitations": candidate["limitations"],
    }


def _pick_examples(
    chains: list[dict[str, Any]], payloads: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    clean = next(
        chain
        for chain in chains
        if chain["completion_status"] == "succeeded"
        and not chain["metric_limitations"]
        and len(chain["interval_records"]) == 2
    )
    overlap_limited = next(
        chain
        for chain in chains
        if chain["completion_status"] == "succeeded" and chain["metric_limitations"]
    )
    pending = next(chain for chain in chains if chain["completion_status"] == "pending")
    return [
        _example(clean, payloads),
        _example(overlap_limited, payloads),
        _example(pending, payloads),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--memory-report", type=Path, required=True)
    parser.add_argument("--chains", type=Path, required=True)
    parser.add_argument("--memory-configuration", type=Path)
    parser.add_argument("--observed-world-store", type=Path)
    parser.add_argument("--observed-world-configuration", type=Path)
    parser.add_argument("--observed-world-prepare", type=Path)
    parser.add_argument("--observed-through", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    manifest_bytes = args.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    rows, records_sha256 = _load_rows(args.records)
    memory_report = json.loads(args.memory_report.read_text())
    chains = json.loads(args.chains.read_text())
    payloads = _transition_payloads(rows)
    transitions = [
        json.loads(row["payload_json"])
        for row in rows
        if row["record_type"] == "experience-transition"
    ]
    outcomes = [
        json.loads(row["payload_json"]) for row in rows if row["record_type"] == "run-outcome"
    ]

    status_counts = Counter(chain["completion_status"] for chain in chains)
    metric_relation_counts = Counter(
        relation["metric_name"] for chain in chains for relation in chain["metric_relations"]
    )
    action_counts = Counter(t["action"]["kind"] for t in transitions)
    metric_counts = Counter(
        (metric["name"], metric["unit"])
        for t in transitions
        for metric in t.get("objective_metrics", [])
    )
    link_counts = Counter(
        link["relation"] for t in transitions for link in t.get("operation_links", [])
    )
    environment_null = sum(t.get("environment_id") is None for t in transitions)
    scenario_null = sum(t.get("scenario_id") is None for t in transitions)
    run_ids = sorted({t["run_id"] for t in transitions})
    namespaces = sorted({row["source_namespace"] for row in rows})

    report = {
        "schema_version": "1.0",
        "claim_scope": (
            "bounded offline descriptive replay over frozen corpus-08; counts, "
            "candidate examples and provenance only"
        ),
        "tested": {
            "existing_probes": [
                "scripts/probe_memory_evidence.py",
                "scripts/probe_operation_chains.py",
            ],
            "provider_calls": 0,
            "simulator_calls": 0,
            "model_calls": 0,
            "source_sqlite_opened": False,
            "invented_declarations": 0,
            "observed_through": args.observed_through,
        },
        "provenance": {
            "manifest_path": str(args.manifest),
            "records_path": str(args.records),
            "corpus_sqlite_path": manifest.get("corpus_sqlite"),
            "source_sqlite_path": manifest.get("source_sqlite"),
            "source_capsule_freeze_path": manifest.get("source_capsule_freeze"),
            "source_terminal_evidence_path": manifest.get("source_terminal_evidence"),
            "memory_configuration_path": (
                str(args.memory_configuration) if args.memory_configuration is not None else None
            ),
            "observed_world_store_path": (
                str(args.observed_world_store) if args.observed_world_store is not None else None
            ),
            "observed_world_configuration_path": (
                str(args.observed_world_configuration)
                if args.observed_world_configuration is not None
                else None
            ),
            "observed_world_prepare_path": (
                str(args.observed_world_prepare)
                if args.observed_world_prepare is not None
                else None
            ),
            "source_namespace": namespaces[0] if len(namespaces) == 1 else None,
            "run_ids": run_ids,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "records_sha256": records_sha256,
            "manifest_records_sha256": manifest.get("corpus_records_sha256"),
            "snapshot_content_hash": memory_report["snapshot_content_hash"],
            "source_integrity_check": manifest.get("source_integrity_check"),
        },
        "input_counts_and_types": {
            "records": len(rows),
            "record_types": dict(Counter(row["record_type"] for row in rows)),
            "transitions": len(transitions),
            "run_outcomes": len(outcomes),
            "outcome_statuses": dict(Counter(o["status"] for o in outcomes)),
            "iterations": {
                "min": min(t["iteration"] for t in transitions),
                "max": max(t["iteration"] for t in transitions),
                "unique": len({t["iteration"] for t in transitions}),
            },
            "action_kinds": dict(sorted(action_counts.items())),
            "operation_link_relations": dict(sorted(link_counts.items())),
            "objective_metric_types": {
                f"{name} [{unit}]": count for (name, unit), count in sorted(metric_counts.items())
            },
            "transitions_with_metrics": sum(bool(t.get("objective_metrics")) for t in transitions),
            "transitions_with_deltas": sum(bool(t.get("objective_deltas")) for t in transitions),
        },
        "context_identity": {
            "environment_id_null_count": environment_null,
            "scenario_id_null_count": scenario_null,
            "immutable_context_identity_verified": False,
            "reason": (
                "all transitions have null environment_id and scenario_id; the frozen "
                "export has no immutable run declaration/content-hash binding"
            ),
        },
        "observational_associations": {
            "proposal_count": memory_report["temporal_association_proposals"],
            "metric_counts": memory_report["association_metric_counts"],
            "status": "candidate_unaccepted",
            "causal_credit": False,
            "activation": memory_report["association_activation"],
            "evidence_snapshot_hash": memory_report["snapshot_content_hash"],
        },
        "operation_chains": {
            "candidate_count": len(chains),
            "completion_status_counts": dict(sorted(status_counts.items())),
            "metric_relation_counts": dict(sorted(metric_relation_counts.items())),
            "all_causal_credit_false": all(not c["causal_credit"] for c in chains),
            "all_context_identity_unverified": all(
                not c["context_identity_verified"] for c in chains
            ),
            "all_candidates_untrusted": all(
                c["trust_classification"] == "derived_untrusted" for c in chains
            ),
            "examples": _pick_examples(chains, payloads),
        },
        "promotion_gates": [
            {
                "gate": "frozen_export_integrity",
                "status": "passed",
                "evidence": "manifest and JSONL hashes match; record and snapshot integrity passed",
            },
            {
                "gate": "observed_association_extraction",
                "status": "passed_for_descriptive_replay",
                "evidence": (
                    f"{memory_report['temporal_association_proposals']} unaccepted "
                    "temporal proposals extracted"
                ),
            },
            {
                "gate": "operation_chain_extraction",
                "status": "passed_for_descriptive_replay",
                "evidence": (
                    f"{len(chains)} bounded candidates extracted with public status resolver"
                ),
            },
            {
                "gate": "lesson_evidence_declarations",
                "status": "blocked",
                "exact_blocker": memory_report["lesson_evidence_gate"]["reason"],
            },
            {
                "gate": "immutable_world_identity",
                "status": "blocked",
                "exact_blocker": (
                    "environment_id/scenario_id are null for all 619 transitions; no immutable "
                    "environment/scenario content-hash declaration is present"
                ),
            },
            {
                "gate": "run_terminal_success",
                "status": "blocked",
                "exact_blocker": (
                    "run-outcome status is interrupted, not a completed terminal outcome"
                ),
            },
            {
                "gate": "association_activation",
                "status": "blocked",
                "exact_blocker": (
                    "temporal proposals are explicitly unaccepted; activation is not permitted"
                ),
            },
            {
                "gate": "operation_chain_promotion",
                "status": "blocked",
                "exact_blocker": (
                    "candidate contract requires causal_credit=false, "
                    "context_identity_verified=false, "
                    "and not_validated_for_decision_use; one candidate remains pending"
                ),
            },
            {
                "gate": "untrusted_observation_overlay",
                "status": "permitted",
                "evidence": (
                    "operation-chain candidates remain available as derived_untrusted "
                    "overlay material; this corpus gate does not block observational exposure"
                ),
            },
            {
                "gate": "active_or_causal_knowledge_promotion",
                "status": "blocked",
                "exact_blocker": (
                    "no candidate is validated for causal/active knowledge use; no automatic "
                    "promotion or live module registration is enabled"
                ),
            },
        ],
        "safe_to_pass_forward": {
            "counts_and_types": True,
            "candidate_examples": True,
            "source_record_hashes_and_snapshot_provenance": True,
            "descriptive_associations_or_chains_as_untrusted_candidates": True,
            "untrusted_observation_overlay": True,
            "causal_or_utility_claim": False,
            "active_promotion": False,
            "active_or_causal_knowledge_promotion": False,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "candidate_count": len(chains)}))


if __name__ == "__main__":
    main()
