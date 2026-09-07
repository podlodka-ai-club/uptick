"""Offline descriptive chain selection and existing reader/request integration.

Exact action-query matching is an explicit experimental adapter policy, not a
learned relevance model. No provider calls or active memory-profile changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from probe_observed_decision_boundary import CaptureClient
from probe_operation_chains import load_evidence, public_operation_status

from uptick_agent.decisions.contracts import NextStep
from uptick_agent.decisions.runtime import RuntimeDecisionContext, ToolResult
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.memory.contracts import (
    ContextItem,
    DecisionMemoryContext,
    MemoryContextRequest,
    ProvenanceRef,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.episodic import EpisodicMemory
from uptick_agent.memory.observation_reader import StoredObservationReader
from uptick_agent.memory.operation_chains import extract_operation_chains


def rendered(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def build_chain_item(chain, action_name):
    refs = {r.record_id: r for r in chain.interval_records}
    for metric in chain.metric_relations:
        refs.update({r.record_id: r for r in metric.interval_records})
    payload = {
        "candidate_id": chain.candidate_id,
        "source_run_id": chain.run_id,
        "evidence_snapshot_hash": chain.evidence_snapshot_hash,
        "operation_id": chain.operation_id,
        "initiation_record_id": chain.initiation_record.record_id,
        "completion_record_id": chain.completion_record.record_id
        if chain.completion_record
        else None,
        "action": action_name,
        "completion_status": chain.completion_status,
        "causal_credit": False,
        "historical_evidence": "stale_environment_observation",
        "limitations": list(chain.limitations) + list(chain.metric_limitations),
        "source_count": len(refs),
        "metric_relations": [
            {
                "name": m.metric_name,
                "unit": m.metric_unit,
                "before": m.before,
                "after": m.after,
                "observed_from": m.observed_from.isoformat(),
                "observed_until": m.observed_until.isoformat(),
            }
            for m in chain.metric_relations
        ],
        "source_records": [r.model_dump(mode="json") for r in refs.values()],
    }
    item = ContextItem(
        envelope=UntrustedMemoryEnvelope(
            item_id=chain.candidate_id,
            artefact_type="observed_operation_chain",
            origin_module="offline-chain-probe",
            origin_version="0.1",
            trust_classification="derived_untrusted",
            provenance=[
                ProvenanceRef(
                    artefact_id=chain.initiation_record.record_id,
                    content_hash=chain.initiation_record.content_hash,
                )
            ],
            item=payload,
        ),
        score=1,
        selection_reason="Exact action query; newest observed initiation first",
        estimated_tokens=0,
    )
    item.estimated_tokens = len(
        rendered(item.model_dump(mode="json", exclude={"estimated_tokens"})).encode()
    )
    return item


class PrefixReaderStore:
    """Only expose the selected public prefix to the existing canonical reader."""

    def __init__(self, records, run_id, cutoff):
        self.records = {
            r.record_id: r
            for r in records
            if r.record_type == "experience-transition"
            and r.payload["run_id"] == run_id
            and r.created_at <= cutoff
        }

    async def get(self, *, namespace, record_id):
        record = self.records.get(record_id)
        return record.model_copy(deep=True) if record and record.namespace == namespace else None

    async def list(self, *, namespace):
        return [
            r.model_copy(deep=True)
            for r in sorted(self.records.values(), key=lambda r: (r.created_at, r.record_id))
            if r.namespace == namespace
        ]


async def probe(args):
    evidence, digest = load_evidence(args.source)
    chains = extract_operation_chains(
        evidence,
        learning_cutoffs={args.run_id: args.cutoff},
        max_iteration_gap=32,
        resolve_status=public_operation_status,
    )
    store = PrefixReaderStore(evidence.records, args.run_id, args.cutoff)
    records = store.records
    episodic = EpisodicMemory(store, namespace=evidence.snapshot.namespace, module_version="1.2")
    current_iteration = max(r.payload["iteration"] for r in records.values()) + 1
    reader = StoredObservationReader(
        store,
        namespace=evidence.snapshot.namespace,
        run_id=args.run_id,
    )
    args.output.mkdir(parents=True, exist_ok=False)
    report = {
        "source_sha256": digest,
        "cutoff": args.cutoff.isoformat(),
        "provider_calls": 0,
        "simulator_calls": 0,
        "reader_calls_are_offline_not_charged_runner_actions": True,
        "queries": {},
    }
    for query, budget in [
        ("server.create", 8000),
        ("server.delete", 8000),
        ("database.restore", 8000),
        ("unseen.action", 8000),
        ("server.create", 0),
    ]:
        label = f"{query}-{budget}"
        request = MemoryContextRequest(
            request_id=label,
            run_id=args.run_id,
            query=query,
            max_items=2,
            max_estimated_tokens=budget,
        )
        matching = []
        for chain in chains:
            action = records[chain.initiation_record.record_id].payload["action"]
            action_name = action.get("request", {}).get("command", action.get("kind"))
            if action_name == request.query:
                matching.append(chain)
        matching.sort(key=lambda c: records[c.initiation_record.record_id].created_at, reverse=True)
        items, reads, selected_bookmarks = [], [], {}
        dropped = []
        for chain in matching:
            item = build_chain_item(chain, request.query)
            proposed = DecisionMemoryContext(items=items + [item])
            if (
                len(items) >= request.max_items
                or len(rendered(proposed.model_dump(mode="json")).encode()) > budget
            ):
                dropped.append(chain.candidate_id)
                continue
            items.append(item)
            # Exercise the canonical reader only for a source actually selected.
            endpoint = chain.completion_record or chain.initiation_record
            bookmark = await reader.bookmark(
                endpoint.record_id, current_iteration=current_iteration
            )
            chunk = await reader.read(bookmark, current_iteration=current_iteration, max_bytes=512)
            if chunk["returned_bytes"] > 512:
                raise AssertionError("reader exceeded byte bound")
            selected_bookmarks[chain.candidate_id] = bookmark.model_dump(mode="json")
            reads.append({k: chunk[k] for k in ("record_id", "digest", "returned_bytes", "eof")})
        memory = DecisionMemoryContext(items=items)
        trace = StructuredDecisionModel(CaptureClient(), response_model=NextStep).prompt_trace(
            RuntimeDecisionContext(
                objective="Offline probe: inspect historical operation consequences.",
                run_id=args.run_id,
                seed=0,
                iteration=current_iteration,
                max_steps=current_iteration,
                latest_result=ToolResult(action_kind="offline_probe", summary=request.query),
                memory_context=memory,
            )
        )
        submitted = json.loads(trace["messages"][1]["content"].split("JSON follows:\n", 1)[1])
        if submitted["memory_context"] != memory.model_dump(mode="json"):
            raise AssertionError("decision request changed memory")
        if (query == "unseen.action" or budget == 0) and items:
            raise AssertionError("negative query/budget admitted context")
        contribution = await episodic.retrieve(request)
        baseline = DecisionMemoryContext()
        for episode in contribution.items:
            proposed = DecisionMemoryContext(items=baseline.items + [episode])
            if (
                len(baseline.items) < request.max_items
                and len(rendered(proposed.model_dump(mode="json")).encode()) <= budget
            ):
                baseline.items.append(episode)
        (args.output / f"{label}.json").write_text(
            json.dumps(
                {
                    "request": trace,
                    "bookmarks": selected_bookmarks,
                    "reader_receipts": reads,
                    "episodic_context": baseline.model_dump(mode="json"),
                },
                indent=2,
            )
            + "\n"
        )
        report["queries"][label] = {
            "matches": len(matching),
            "selected": len(items),
            "dropped": len(dropped),
            "reader_calls": len(reads),
            "memory_json_bytes": len(rendered(memory.model_dump(mode="json")).encode()),
            "budget_bytes": budget,
            "episodic_items": len(baseline.items),
            "episodic_json_bytes": len(rendered(baseline.model_dump(mode="json")).encode()),
        }
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cutoff", type=datetime.fromisoformat, required=True)
    asyncio.run(probe(parser.parse_args()))
