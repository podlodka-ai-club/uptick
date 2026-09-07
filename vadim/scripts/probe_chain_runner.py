"""Actual runner capture with historical chain overlay; no model/environment calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from probe_chain_retrieval import build_chain_item, rendered
from probe_observed_decision_boundary import CaptureClient, RequestCaptured
from probe_observed_runner import RecordedStart
from probe_operation_chains import load_evidence, public_operation_status

from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.decisions.contracts import NextStep
from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.evaluation.snapshots import EvaluationMemoryFacade
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.memory.candidate_validation import validate_transition_record
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.contracts import DecisionMemoryContext
from uptick_agent.memory.operation_chains import extract_operation_chains
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.memory.stores.contracts import StoredRecord
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.execute import AgentRunner


class FrozenStore:
    def __init__(self, records):
        self.records = {r.record_id: r for r in records}

    async def list(self, *, namespace):
        return [
            r.model_copy(deep=True)
            for r in sorted(self.records.values(), key=lambda r: (r.created_at, r.record_id))
            if r.namespace == namespace
        ]

    async def get(self, *, namespace, record_id):
        r = self.records.get(record_id)
        return r.model_copy(deep=True) if r and r.namespace == namespace else None

    async def append(self, *args, **kwargs):
        raise ValueError("frozen source is read-only")


class ChainOverlay:
    def __init__(self, base, chains, records, *, enabled, budget=8000):
        self.base, self.chains, self.records = base, chains, records
        self.enabled, self.budget, self.receipt = enabled, budget, {}

    def __getattr__(self, name):
        return getattr(self.base, name)

    async def build_context(self, request):
        baseline = await self.base.build_context(request)
        data = request.context.get("latest_result", {}).get("data", {})
        command = data.get("command") if isinstance(data, dict) else None
        selected = []
        if self.enabled and isinstance(command, str):
            matching = [
                c
                for c in self.chains
                if c.run_id != request.run_id
                and self.records[c.initiation_record.record_id]
                .payload["action"]
                .get("request", {})
                .get("command")
                == command
            ]
            matching.sort(
                key=lambda c: self.records[c.initiation_record.record_id].created_at, reverse=True
            )
            for chain in matching:
                item = build_chain_item(chain, command)
                candidate = DecisionMemoryContext(
                    items=baseline.items + [item], warnings=baseline.warnings
                )
                if (request.max_items is None or len(candidate.items) <= request.max_items) and len(
                    rendered(candidate.model_dump(mode="json")).encode()
                ) <= self.budget:
                    selected = [item]
                    break
        result = DecisionMemoryContext(items=baseline.items + selected, warnings=baseline.warnings)
        self.receipt = {
            "request": request.model_dump(mode="json"),
            "baseline_ids": [i.envelope.item_id for i in baseline.items],
            "selected_chain_ids": [i.envelope.item_id for i in selected],
            "memory_json_bytes": len(rendered(result.model_dump(mode="json")).encode()),
            "shared_budget_bytes": self.budget,
        }
        if self.receipt["memory_json_bytes"] > self.budget:
            raise ValueError("baseline itself exceeds shared serialized context budget")
        return result


def recorded_case(database, command):
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        if command:
            criterion, value = "$.action.request.command", command
        else:
            criterion, value = "$.action.kind", "get_overview"
        row = connection.execute(
            "SELECT * FROM memory_records WHERE record_type='experience-transition' "
            "AND json_extract(payload_json, ?) = ? ORDER BY created_at LIMIT 1",
            (criterion, value),
        ).fetchone()
    if row is None:
        raise ValueError("recorded case not found")
    record = StoredRecord(
        schema_version=row["schema_version"],
        namespace=row["namespace"],
        record_id=row["record_id"],
        record_type=row["record_type"],
        payload=json.loads(row["payload_json"]),
        created_at=row["created_at"],
        content_hash=row["content_hash"],
    )
    transition = validate_transition_record(StoredRecord.validate_integrity(record))
    return record, transition


async def probe(args):
    evidence, digest = load_evidence(args.source)
    if any(r.created_at > args.cutoff for r in evidence.records):
        raise ValueError("historical source exceeds declared freeze cutoff")
    chains = extract_operation_chains(
        evidence,
        learning_cutoffs={args.source_run: args.cutoff},
        max_iteration_gap=32,
        resolve_status=public_operation_status,
    )
    configuration = MemoryConfiguration.model_validate(
        json.loads(args.configuration.read_text())["configuration"]
    )
    frozen = FrozenStore(evidence.records)
    args.output.mkdir(parents=True, exist_ok=False)
    report = {
        "source_sha256": digest,
        "provider_calls": 0,
        "environment_actions": 0,
        "cases": {},
        "scope": "development recorded-start capture, not run resume or utility",
    }
    for label, command in (("create", "server.create"), ("overview", None)):
        record, transition = recorded_case(args.observations, command)
        if transition.run_id == args.source_run or record.created_at <= args.cutoff:
            raise ValueError("case overlaps historical learning run or freeze time")
        observation = ToolResult.model_validate(transition.result)
        contexts = {}
        for enabled in (False, True):
            reader = compose_experimental_runtime(
                configuration, frozen, namespace=evidence.snapshot.namespace
            )
            writer = compose_experimental_runtime(
                configuration, InMemoryStructuredStore(), namespace="isolated-chain-capture"
            )
            memory = ChainOverlay(
                EvaluationMemoryFacade(reader, writer), chains, frozen.records, enabled=enabled
            )
            client = CaptureClient()
            runner = AgentRunner(
                config=AgentConfig(max_steps=1, memory_recall_limit=3),
                model=StructuredDecisionModel(client, response_model=NextStep),
                memory=memory,
                environment=RecordedStart(observation, transition.run_id),
            )
            try:
                await runner.run(seed=45)
            except RequestCaptured:
                pass
            else:
                raise AssertionError("runner did not reach model request boundary")
            submitted = json.loads(
                client.request["messages"][1]["content"].split("JSON follows:\n", 1)[1]
            )
            if submitted["latest_result"] != observation.model_dump(mode="json"):
                raise AssertionError("current observation changed")
            contexts[enabled] = submitted
            (args.output / f"{label}-{enabled}.json").write_text(
                json.dumps(
                    {"provider_request": client.request, "retrieval": memory.receipt}, indent=2
                )
                + "\n"
            )
        before, after = contexts[False], contexts[True]
        added = [
            i
            for i in after["memory_context"]["items"]
            if i["envelope"]["artefact_type"] == "observed_operation_chain"
        ]
        after["memory_context"]["items"] = [
            i for i in after["memory_context"]["items"] if i not in added
        ]
        if before != after or bool(added) != bool(command):
            raise AssertionError("overlay changed baseline or failed applicability control")
        report["cases"][label] = {
            "observation_id": record.record_id,
            "observation_hash": record.content_hash,
            "observed_at": record.created_at.isoformat(),
            "baseline_preserved": True,
            "added_chains": len(added),
        }
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "observations", "configuration", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--cutoff", type=datetime.fromisoformat, required=True)
    asyncio.run(probe(parser.parse_args()))
