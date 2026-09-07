"""Offline scripted handoff through the real runner and decision request bridge.

The client returns two declared fixture actions, then captures the third request.
No provider or simulator is invoked. This is transport/accounting evidence only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from pydantic import Field

from uptick_agent._model_base import StrictModel
from uptick_agent.composition.handoff import ObservationHandoff
from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.llm.contracts import serialize_structured_generation_request
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.execute import AgentRunner
from uptick_agent.runs.handoff import ObservationReadRequest


class Observe(StrictModel):
    kind: Literal["recorded_observation"] = "recorded_observation"


class Read(StrictModel):
    kind: Literal["read_recorded_observation"] = "read_recorded_observation"
    ref: str


class Decision(StrictModel):
    action: Observe | Read = Field(discriminator="kind")


class Captured(Exception):
    pass


class ScriptedClient:
    model = "offline-scripted-fixture-no-provider"

    def __init__(self):
        self.requests = []

    async def generate_structured(self, request):
        serialized = serialize_structured_generation_request(request)
        self.requests.append(serialized)
        context = json.loads(serialized["messages"][1]["content"].split("JSON follows:\n")[1])
        if len(self.requests) == 1:
            return SimpleNamespace(value=Decision(action=Observe()))
        if len(self.requests) == 2:
            return SimpleNamespace(
                value=Decision(action=Read(ref=context["observation_bookmarks"][0]["record_id"]))
            )
        raise Captured


class RecordedEnvironment:
    decision_spec = EnvironmentDecisionSpec(
        Decision, objective="Scripted offline handoff transport fixture."
    )

    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def start(self, **kwargs):
        return SimpleNamespace(run_id="offline-handoff-fixture"), ToolResult(
            action_kind="fixture.start", summary="Offline fixture; no simulated state."
        )

    async def execute(self, session, action):
        assert isinstance(action, Observe)
        self.calls += 1
        return self.result.model_copy(deep=True)

    async def finish(self, *args, **kwargs):
        raise AssertionError("third request must capture before finish")


async def probe(source: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    prepared = json.loads((source / "prepare.json").read_text())
    source_store = SqliteStructuredStore(source / "memory.sqlite3")
    record = await source_store.get(
        namespace=prepared["namespace"], record_id=prepared["source_record_id"]
    )
    assert record is not None and record.content_hash == prepared["source_record_hash"]
    observation = ToolResult.model_validate(record.payload["result"])
    store = SqliteStructuredStore(output / "memory.sqlite3")
    namespace = "offline-handoff-fixture"
    config = MemoryConfiguration.episodic_only()
    runtime = compose_experimental_runtime(config, store, namespace=namespace)
    client = ScriptedClient()
    environment = RecordedEnvironment(observation)
    runner = AgentRunner(
        config=AgentConfig(max_steps=3),
        max_actions=3,
        model=StructuredDecisionModel(client, response_model=Decision),
        memory=runtime,
        environment=environment,
        observation_handoff=ObservationHandoff(store, namespace),
        observation_read_action=lambda action: (
            ObservationReadRequest(record_id=action.ref, max_bytes=8192)
            if isinstance(action, Read)
            else None
        ),
    )
    try:
        await runner.run(seed=0)
    except Captured:
        pass
    else:
        raise AssertionError("third request was not captured")
    context = json.loads(client.requests[-1]["messages"][1]["content"].split("JSON follows:\n")[1])
    assert context["actions_executed"] == 2
    assert environment.calls == 1
    assert context["latest_result"] == observation.model_dump(mode="json")
    receipt = context["memory_read_result"]
    assert receipt["ok"] and receipt["objective_metrics"] == []
    assert receipt["data"]["eof"]
    assert json.loads(receipt["data"]["text"]) == observation.model_dump(mode="json")
    episodes = [
        r for r in await store.list(namespace=namespace) if r.record_type == "experience-transition"
    ]
    assert len(episodes) == len(context["observation_bookmarks"]) == 1
    report = {
        "provider_calls": 0,
        "simulator_calls": 0,
        "scripted_actions": 2,
        "charged_actions": context["actions_executed"],
        "fixture_environment_calls": 1,
        "canonical_episodes": len(episodes),
        "live_latest_preserved": True,
        "historical_response_exact": True,
        "source_record_id": record.record_id,
        "source_record_hash": record.content_hash,
        "response_digest": hashlib.sha256(receipt["data"]["text"].encode()).hexdigest(),
        "configuration_fingerprint": runtime.configuration.fingerprint,
        "limitation": (
            "Scripted transport; no model utility or economic comparison. "
            "Capture creates a labelled failed fixture outcome."
        ),
    }
    (output / "requests.json").write_text(json.dumps(client.requests, indent=2) + "\n")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.source, args.output))
