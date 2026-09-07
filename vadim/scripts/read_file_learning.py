"""Cold-read strict memory using an actual reserved filesystem task observation."""

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path

from uptick_agent.benchmarks.file_records import FileRecordsEnvironment
from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.contracts import MemoryContextRequest
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.stores import SqliteStructuredStore


async def read(args):
    args.output.mkdir(parents=True, exist_ok=False)
    evidence = LessonEvidence.model_validate_json((args.collection / "evidence.json").read_text())
    configuration = MemoryConfiguration.model_validate_json(
        (args.learning / "configuration.json").read_text()
    )
    store = SqliteStructuredStore(args.learning / "memory.sqlite")
    runtime = compose_experimental_runtime(
        configuration, store, namespace="file-task-real-01", run_declarations=evidence.runs
    )
    manifest = json.loads((args.inputs / "manifest.json").read_text())
    (entry,) = [x for x in manifest["scenarios"] if x["phase"] == "reserved_evaluation"]
    initial = (args.inputs / entry["path"]).read_bytes()
    assert hashlib.sha256(initial).hexdigest() == entry["sha256"]
    workspace = (args.output / "reserved-task").resolve()
    workspace.mkdir()
    (workspace / "records.txt").write_bytes(initial)
    environment = FileRecordsEnvironment(workspace, scenario_id="file-source-03")
    session, observation = await environment.start(seed=3, agent_id="read-probe", agent_version="1")
    assert session.run_id not in {r.run_id for r in evidence.runs}
    (args.output / "public-observation.json").write_text(
        observation.model_dump_json(indent=2) + "\n"
    )
    reports = {}
    contexts = [
        ("missing_scope", {}),
        ("relevant_scope", {"latest_result": observation.model_dump(mode="json")}),
    ]
    for label, context in contexts:
        result = await runtime.build_context(
            MemoryContextRequest(
                request_id="file-read-" + label,
                run_id=session.run_id,
                query="deduplicate inspect duplicate_lines changed",
                context=context,
            )
        )
        (args.output / f"{label}.json").write_text(result.model_dump_json(indent=2) + "\n")
        reports[label] = {
            "modules": dict(Counter(x.envelope.origin_module for x in result.items)),
            "warnings": result.warnings,
        }
    canonical = await store.list(namespace="file-task-real-01")
    assert {r.record_id: r.content_hash for r in canonical} == {
        r.record_id: r.content_hash for r in evidence.records
    }
    await environment.aclose()
    report = {
        "contexts": reports,
        "canonical_records_unchanged": True,
        "reserved_task_added_to_training": False,
        "model_calls": 0,
        "scope": "cold scoped retrieval from real reserved observation, no model decision",
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("learning", "collection", "inputs", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    asyncio.run(read(parser.parse_args()))
