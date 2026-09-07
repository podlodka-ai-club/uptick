"""Matched model decisions on a reserved real file task and frozen training memory."""

import argparse
import asyncio
import dataclasses
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from uptick_agent.benchmarks import file_records
from uptick_agent.benchmarks.file_records import FileRecordsEnvironment
from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.evaluation.contracts import V2SnapshotRef
from uptick_agent.evaluation.snapshots import EvaluationMemoryFacade, SnapshotReadStore
from uptick_agent.llm.codex import CodexLlmClient
from uptick_agent.llm.contracts import GenerationSettings
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.stores import InMemoryStructuredStore, SqliteStructuredStore
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.execute import AgentRunner

ARMS = {
    "none": (),
    "episodic": (),
    "lessons": ("lessons",),
    "world": ("world_model",),
    "tools": ("tool_knowledge",),
    "playbooks": ("lessons", "playbooks"),
    "full": ("lessons", "world_model", "tool_knowledge", "playbooks"),
}
MODEL = "gpt-5.6-terra"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class Prepared(Exception):
    pass


class TracedModel:
    def __init__(self, client, spec, output, execute):
        self.client, self.output, self.execute = client, output, execute
        self.model = StructuredDecisionModel(
            client,
            response_model=spec.response_model,
            environment_briefing=spec.environment_briefing,
            settings=GenerationSettings(reasoning_effort="medium"),
        )
        self.calls = []

    @property
    def last_telemetry(self):
        return getattr(self.client, "last_telemetry", None)

    def prompt_trace(self, context):
        return self.model.prompt_trace(context)

    async def decide(self, context):
        request = self.prompt_trace(context)
        if len(json.dumps(request).encode()) > 60000:
            raise ValueError("serialized request exceeds experiment budget")
        save(self.output / f"request-{context.iteration}.json", request)
        save(self.output / f"context-{context.iteration}.json", context.model_dump(mode="json"))
        if not self.execute:
            raise Prepared()
        try:
            async with asyncio.timeout(120):
                decision = await self.model.decide(context)
            self.calls.append({"decision": decision.model_dump(mode="json")})
            return decision
        except Exception as error:
            self.calls.append({"error_type": type(error).__name__})
            raise
        finally:
            telemetry = self.last_telemetry
            self.calls[-1]["telemetry"] = dataclasses.asdict(telemetry) if telemetry else None
            self.calls[-1]["provider_attempts"] = list(self.client.last_attempts)
            save(self.output / "calls.json", self.calls)


async def cell(args, label, initial, configuration, evidence, store, refs, *, execute):
    output = args.output / ("execution" if execute else "preflight") / label
    output.mkdir(parents=True, exist_ok=False)
    workspace = (output / "workspace").resolve()
    workspace.mkdir()
    (workspace / "records.txt").write_bytes(initial)
    environment = FileRecordsEnvironment(
        workspace, scenario_id="file-source-03", run_id_suffix=label
    )
    payload = configuration.model_dump(mode="json")
    payload["profile_id"] = "file-evaluation-" + label
    for name in ("lessons", "world_model", "tool_knowledge", "playbooks"):
        payload[name]["enabled"] = name in ARMS[label]
    payload["episodic"]["enabled"] = label != "none"
    arm_configuration = MemoryConfiguration.model_validate(payload)
    save(output / "configuration.json", arm_configuration.model_dump(mode="json"))
    readonly = SnapshotReadStore(store, refs)
    await readonly.load()
    reader = compose_experimental_runtime(
        arm_configuration, readonly, namespace="file-task-real-01", run_declarations=evidence.runs
    )
    writer_configuration = configuration.model_dump(mode="json")
    for name in ("lessons", "world_model", "tool_knowledge", "playbooks"):
        writer_configuration[name]["enabled"] = False
    writer = compose_experimental_runtime(
        MemoryConfiguration.model_validate(writer_configuration),
        InMemoryStructuredStore(),
        namespace="isolated-file-evaluation",
    )
    memory = EvaluationMemoryFacade(reader, writer, frozen_snapshot_members=readonly.member_count)
    client = None
    try:
        client = CodexLlmClient(model=MODEL) if execute else SimpleNamespace(model=MODEL)
        model = TracedModel(client, environment.decision_spec, output, execute)
        result = await AgentRunner(
            config=AgentConfig(
                agent_id="file-memory-evaluation",
                max_steps=3,
                max_actions=3,
                memory_recall_limit=32,
            ),
            model=model,
            memory=memory,
            environment=environment,
        ).run(3)
        report = {"arm": label, "result": result.model_dump(mode="json")}
    except Prepared:
        report = {"arm": label, "prepared": True, "model_calls": 0}
    except Exception as error:
        report = {"arm": label, "error_type": type(error).__name__}
    finally:
        if execute and client is not None:
            await client.aclose()
        await environment.aclose()
    save(output / "result.json", report)
    print(json.dumps(report), flush=True)
    return report


async def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    adapter = Path(file_records.__file__).read_bytes()
    if adapter != (args.collection / "adapter-source.py").read_bytes():
        raise ValueError("adapter differs from frozen learning implementation")
    evidence = LessonEvidence.model_validate_json((args.collection / "evidence.json").read_text())
    configuration = MemoryConfiguration.model_validate_json(
        (args.learning / "configuration.json").read_text()
    )
    manifest = json.loads((args.inputs / "manifest.json").read_text())
    (entry,) = [x for x in manifest["scenarios"] if x["phase"] == "reserved_evaluation"]
    initial = (args.inputs / entry["path"]).read_bytes()
    if hashlib.sha256(initial).hexdigest() != entry["sha256"]:
        raise ValueError("reserved input changed")
    with (
        sqlite3.connect(args.learning / "memory.sqlite") as source,
        sqlite3.connect(args.output / "frozen.sqlite") as target,
    ):
        source.backup(target)
    store = SqliteStructuredStore(args.output / "frozen.sqlite")
    refs = []
    for suffix in (
        "",
        ":lessons",
        ":world",
        ":playbooks",
        ":tool-knowledge",
        ":lessons:declarations",
    ):
        namespace = "file-task-real-01" + suffix
        receipt = await store.create_snapshot(
            namespace=namespace,
            snapshot_id="eval:" + namespace,
            operation="freeze-evaluation",
            idempotency_key=namespace,
        )
        refs.append(
            V2SnapshotRef(
                namespace=namespace,
                snapshot_id=receipt.snapshot.snapshot_id,
                content_hash=receipt.snapshot.content_hash,
            )
        )
    protocol = {
        "model": MODEL,
        "effort": "medium",
        "max_decisions": 3,
        "timeout_per_decision": 120,
        "concurrency": 2,
        "max_request_bytes": 60000,
        "adapter_max_attempts_per_decision": 2,
        "hard_output_token_cap": None,
        "adapter_sha256": hashlib.sha256(adapter).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "initial_bytes_sha256": entry["sha256"],
        "arms": ARMS,
        "snapshots": [x.model_dump(mode="json") for x in refs],
        "execute": args.execute,
        "scope": "controlled independent local task, one scenario",
    }
    save(args.output / "protocol.json", protocol)
    for label in ARMS:
        report = await cell(
            args, label, initial, configuration, evidence, store, refs, execute=False
        )
        if not report.get("prepared"):
            raise ValueError("preflight failed; no provider calls authorized by this script")
    summaries = {}
    baseline_episodes = None
    for label in ARMS:
        context = json.loads((args.output / "preflight" / label / "context-1.json").read_text())
        summaries[label] = dict(
            Counter(x["envelope"]["origin_module"] for x in context["memory_context"]["items"])
        )
        expected_modules = set(ARMS[label]) | ({"episodic"} if label != "none" else set())
        if set(summaries[label]) != expected_modules:
            raise ValueError("prepared module visibility differs from the declared arm")
        if context["memory_context"]["warnings"]:
            raise ValueError("memory warning invalidates matched evaluation")
        episodes = [
            x
            for x in context["memory_context"]["items"]
            if x["envelope"]["origin_module"] == "episodic"
        ]
        if label == "episodic":
            baseline_episodes = episodes
        elif label != "none" and episodes != baseline_episodes:
            raise ValueError("episodic evidence differs across derived-memory arms")
    save(args.output / "preflight-modules.json", summaries)
    if args.execute:
        semaphore = asyncio.Semaphore(2)

        async def bounded(label):
            async with semaphore:
                return await cell(
                    args, label, initial, configuration, evidence, store, refs, execute=True
                )

        reports = await asyncio.gather(*(bounded(label) for label in ARMS))
        save(args.output / "report.json", reports)
    if Path(file_records.__file__).read_bytes() != adapter:
        raise ValueError("adapter changed during experiment")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("learning", "collection", "inputs", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    asyncio.run(main(parser.parse_args()))
