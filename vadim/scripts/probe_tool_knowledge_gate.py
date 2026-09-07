"""Check the real public corpus against the unchanged strict tool-knowledge gate.

Descriptive response counts are diagnostics, not accepted tool knowledge.
No environment identity is inferred and no provider/simulator is called.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from probe_memory_evidence import read_frozen_export

from uptick_agent.memory.contracts import MemoryValidationError
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.settings import ToolKnowledgeQuerySettings
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.memory.stores.contracts import RecordWrite
from uptick_agent.memory.tool_knowledge import generate_tool_knowledge_candidates


async def probe(manifest: Path, records_path: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    _, source_hash, records = read_frozen_export(manifest, records_path)
    store = InMemoryStructuredStore()
    namespace = records[0].namespace
    assert {r.namespace for r in records} == {namespace}
    for record in records:
        await store.append(
            RecordWrite.model_validate(record.model_dump(exclude={"content_hash"})),
            operation="copy-public-record",
            idempotency_key=record.record_id,
        )
    snapshot = await store.create_snapshot(
        namespace=namespace,
        snapshot_id=f"tool-gate:{source_hash}",
        operation="freeze-diagnostic",
        idempotency_key=source_hash,
    )
    evidence = LessonEvidence(snapshot=snapshot.snapshot, records=records, runs=[])
    settings = ToolKnowledgeQuerySettings(
        # This is an explicitly unverified diagnostic label, not a claim of
        # adapter identity or a replacement for a missing world declaration.
        adapter_identity="unverified-public-corpus-projection",
        scope_paths=("observation.ok",),
        action_path="action.kind",
        input_paths=("action.request.command",),
        response_path="result.data.status",
    )
    try:
        candidates = generate_tool_knowledge_candidates(evidence, settings)
    except MemoryValidationError as error:
        gate = {"status": "rejected", "reason": str(error), "candidate_count": 0}
    else:
        gate = {"status": "passed", "candidate_count": len(candidates)}
    transitions = [r.payload for r in records if r.record_type == "experience-transition"]
    shapes = Counter()
    matched = 0
    for row in transitions:
        action, response = row["action"], row["result"]
        command = action.get("request", {}).get("command")
        status = response.get("data", {}).get("status")
        if command is not None and status is not None:
            shapes[(command, status)] += 1
            matched += 1
    report = {
        "source_sha256": source_hash,
        "records": len(records),
        "transitions": len(transitions),
        "declared_runs": len(evidence.runs),
        "unknown_environment_ids": sum(t["environment_id"] is None for t in transitions),
        "unknown_scenario_ids": sum(t["scenario_id"] is None for t in transitions),
        "strict_tool_candidate_gate": gate,
        "descriptive_response_rows": matched,
        "descriptive_shapes": [
            {"command": command, "immediate_response_status": status, "count": count}
            for (command, status), count in sorted(shapes.items())
        ],
        "accepted_tool_items_created": 0,
        "model_calls": 0,
        "simulator_calls": 0,
        "limitation": (
            "Response-shape counts do not establish command completion, causality or "
            "reliability. No source declarations or immutable world identity were invented."
        ),
    }
    (output / "query-settings.json").write_text(settings.model_dump_json(indent=2) + "\n")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.manifest, args.records, args.output))
