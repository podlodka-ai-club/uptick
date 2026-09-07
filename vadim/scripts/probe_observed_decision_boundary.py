"""Capture actual decision-bridge requests offline; never invoke a provider.

Uses persisted composed contexts and a mechanical current-observation fixture.
This proves serialization through decide(), not model behavior or usefulness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from uptick_agent.decisions.contracts import NextStep
from uptick_agent.decisions.runtime import RuntimeDecisionContext, ToolResult
from uptick_agent.llm.contracts import serialize_structured_generation_request
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.memory.contracts import DecisionMemoryContext


class RequestCaptured(Exception):
    """Stop at the client boundary without producing a fake model decision."""


class CaptureClient:
    model = "offline-capture-no-provider"

    async def generate_structured(self, request):
        self.request = serialize_structured_generation_request(request)
        raise RequestCaptured


async def probe(source: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    retrieval = json.loads((source / "read-results.json").read_text())
    result = {}
    for label in ("off", "on", "same_run"):
        memory = DecisionMemoryContext.model_validate_json(
            (source / f"context-{label}.json").read_text()
        )
        query = retrieval[label].get("request", {})
        iteration = query.get("context", {}).get("iteration", 1)
        context = RuntimeDecisionContext(
            objective="Offline boundary fixture: inspect recorded operation response evidence.",
            run_id=query.get("run_id", "offline-observed-world-probe"),
            seed=0,
            iteration=iteration,
            max_steps=iteration,
            latest_result=ToolResult(action_kind="fixture", summary="No environment call."),
            memory_context=memory,
        )
        client = CaptureClient()
        bridge = StructuredDecisionModel(client, response_model=NextStep)
        try:
            await bridge.decide(context)
        except RequestCaptured:
            pass
        else:
            raise AssertionError("capture client did not stop at the boundary")
        request = client.request
        user_message = request["messages"][1]["content"]
        submitted = json.loads(user_message.split("JSON follows:\n", 1)[1])
        assert submitted["memory_context"] == memory.model_dump(mode="json")
        facts = [
            item
            for item in submitted["memory_context"]["items"]
            if item["envelope"]["artefact_type"] == "observed_world_fact"
        ]
        assert bool(facts) == (label == "on")
        (output / f"request-{label}.json").write_text(json.dumps(request, indent=2) + "\n")
        result[label] = {"observed_facts_submitted": len(facts), "provider_calls": 0}
    (output / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.source, args.output))
