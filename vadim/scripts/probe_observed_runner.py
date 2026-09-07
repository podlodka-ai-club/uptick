"""Offline runner-to-client integration using an unchanged recorded observation.

Stops before a model response or environment action. This is plumbing evidence,
not a simulated decision, held-out evaluation, or measurement of utility.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from probe_observed_decision_boundary import CaptureClient, RequestCaptured

from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.decisions.contracts import NextStep
from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.execute import AgentRunner


class RecordedStart:
    decision_spec = EnvironmentDecisionSpec(
        NextStep, objective="Offline recorded-observation integration probe."
    )

    def __init__(self, observation: ToolResult, run_id: str):
        self.observation = observation
        self.run_id = run_id

    async def start(self, **kwargs):
        return SimpleNamespace(run_id=self.run_id), self.observation.model_copy(deep=True)

    async def execute(self, *args, **kwargs):
        raise AssertionError("No action is permitted in this offline capture")

    async def finish(self, *args, **kwargs):
        raise AssertionError("Capture must stop before environment finalization")


async def probe(source: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    preparation = json.loads((source / "prepare.json").read_text())
    config = json.loads((source / "configuration.json").read_text())
    report = {}
    for label in ("off", "on"):
        database = output / f"{label}.sqlite3"
        with (
            sqlite3.connect(
                f"file:{(source / 'memory.sqlite3').resolve()}?mode=ro", uri=True
            ) as src,
            sqlite3.connect(database) as dst,
        ):
            src.backup(dst)
        store = SqliteStructuredStore(database)
        records = await store.list(namespace=preparation["namespace"])
        record = next(
            r
            for r in records
            if r.record_type == "experience-transition"
            and r.payload["action"].get("request", {}).get("command") == "server.create"
        )
        observation = ToolResult.model_validate(record.payload["result"])
        payload = dict(config)
        if label == "off":
            payload.pop("observed_world_policy")
        runtime = compose_experimental_runtime(
            MemoryConfiguration.model_validate(payload),
            store,
            namespace=preparation["namespace"],
        )
        client = CaptureClient()
        runner = AgentRunner(
            config=AgentConfig(max_steps=1),
            model=StructuredDecisionModel(client, response_model=NextStep),
            memory=runtime,
            environment=RecordedStart(observation, f"offline-runner-capture-{label}"),
        )
        try:
            await runner.run(seed=0)
        except RequestCaptured:
            pass
        else:
            raise AssertionError("runner did not reach the capture boundary")
        submitted = json.loads(client.request["messages"][1]["content"].split("JSON follows:\n")[1])
        assert submitted["latest_result"] == observation.model_dump(mode="json")
        facts = [
            i
            for i in submitted["memory_context"]["items"]
            if i["envelope"]["artefact_type"] == "observed_world_fact"
        ]
        assert bool(facts) == (label == "on")
        assert not submitted["memory_context"]["warnings"]
        (output / f"request-{label}.json").write_text(json.dumps(client.request, indent=2) + "\n")
        report[label] = {
            "observed_facts": len(facts),
            "provider_calls": 0,
            "environment_actions": 0,
            "observation_source_id": record.record_id,
            "observation_source_hash": record.content_hash,
            "configuration_fingerprint": runtime.configuration.fingerprint,
            "capture_stop": "RequestCaptured; runner records a failed local fixture outcome",
        }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.source, args.output))
