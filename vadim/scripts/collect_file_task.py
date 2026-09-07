"""Collect real filesystem transitions with a declared scripted exploration policy."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from uptick_agent.benchmarks import file_records
from uptick_agent.benchmarks.file_records import (
    ENVIRONMENT_ID,
    FileRecordsDecision,
    FileRecordsEnvironment,
    scenario_content_hash,
)
from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.memory.config import MemoryConfiguration, ModuleConfig
from uptick_agent.memory.lesson_contracts import LessonEvidence, LessonRunDeclaration
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.execute import AgentRunner


class Exploration:
    def __init__(self, final_action):
        self.final_action = final_action

    async def decide(self, context):
        return FileRecordsDecision.model_validate(
            {
                "action": {"kind": "inspect" if context.iteration == 1 else self.final_action},
                "current_situation": "Scripted exploration; observations come from actual files.",
            }
        )


async def collect(args):
    args.output.mkdir(parents=True, exist_ok=False)
    adapter_bytes = Path(file_records.__file__).read_bytes()
    (args.output / "adapter-source.py").write_bytes(adapter_bytes)
    manifest = json.loads((args.inputs / "manifest.json").read_text())
    configuration = MemoryConfiguration(
        profile_id="file-task-raw-collection",
        profile_kind="experiment",
        compatibility_legacy=ModuleConfig(enabled=False),
        episodic=ModuleConfig(enabled=True, version="1.2"),
    )
    store = SqliteStructuredStore(args.output / "memory.sqlite")
    namespace = "file-task-real-01"
    memory = compose_experimental_runtime(configuration, store, namespace=namespace)
    declarations, results = [], []
    for index, entry in enumerate(manifest["scenarios"]):
        if entry["phase"] != "learning":
            continue
        initial = (args.inputs / entry["path"]).read_bytes()
        assert hashlib.sha256(initial).hexdigest() == entry["sha256"]
        # Counter-run reuses a real scenario but has a distinct physical/logical ID.
        for action in ("deduplicate", "noop"):
            directory = (args.output / f"run-{index + 1}-{action}").resolve()
            directory.mkdir()
            (directory / "records.txt").write_bytes(initial)
            scenario_id = entry.get("scenario_id", f"file-source-{index + 1:02d}")
            environment = FileRecordsEnvironment(
                directory,
                scenario_id=scenario_id,
                run_id_suffix=action,
            )
            result = await AgentRunner(
                config=AgentConfig(agent_id="file-memory-probe", max_steps=2),
                model=Exploration(action),
                memory=memory,
                environment=environment,
            ).run(index + 1)
            declarations.append(
                LessonRunDeclaration(
                    run_id=result.run_id,
                    logical_run_id=f"{scenario_id}-{action}",
                    phase="learning",
                    eligible=True,
                    environment_id=ENVIRONMENT_ID,
                    scenario_id=scenario_id,
                    environment_content_hash=environment.environment_content_hash,
                    scenario_content_hash=scenario_content_hash(scenario_id, initial),
                )
            )
            results.append(result.model_dump(mode="json"))
            (directory / "result.json").write_text(result.model_dump_json(indent=2) + "\n")
            await environment.aclose()
    records = await store.list(namespace=namespace)
    transitions = [r for r in records if r.record_type == "experience-transition"]
    assert len(transitions) == 8 and all(
        r.payload["environment_id"] == ENVIRONMENT_ID for r in transitions
    )
    snapshot = await store.create_snapshot(
        namespace=namespace,
        snapshot_id="file-task-raw-complete",
        operation="freeze-file-task",
        idempotency_key="file-task-raw-complete",
    )
    evidence = LessonEvidence(snapshot=snapshot.snapshot, records=records, runs=declarations)
    (args.output / "evidence.json").write_text(evidence.model_dump_json(indent=2) + "\n")
    (args.output / "configuration.json").write_text(configuration.model_dump_json(indent=2) + "\n")
    report = {
        "namespace": namespace,
        "records": len(records),
        "transitions": len(transitions),
        "outcomes": results,
        "model_calls": 0,
        "simulator_calls": 0,
        "policy": "scripted inspect then deduplicate or noop; actual filesystem results",
        "scope": "controlled local-task evidence; not model utility or broad transfer",
        "input_manifest_sha256": hashlib.sha256(
            (args.inputs / "manifest.json").read_bytes()
        ).hexdigest(),
        "adapter_sha256": hashlib.sha256(adapter_bytes).hexdigest(),
    }
    assert Path(file_records.__file__).read_bytes() == adapter_bytes
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "records": len(records),
                "transitions": len(transitions),
                "statuses": [r["status"] for r in results],
                "model_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(collect(parser.parse_args()))
