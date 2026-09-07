"""Measure observation-context representations on public development traces.

No model, simulator, training or utility inference. The compact catalogue is
a candidate representation, not an enabled runner policy.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
from collections import OrderedDict
from pathlib import Path

from uptick_agent.composition.handoff import ObservationHandoff
from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.memory.observation_reader import StoredObservationReader
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.runs.observations import ObservationHistory


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def describe(values):
    ordered = sorted(values)
    return {
        "median": statistics.median(values),
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "max": max(values),
        "sum": sum(values),
    }


async def measure(source: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    metadata = json.loads((source / "prepare.json").read_text())
    store = SqliteStructuredStore(source / "memory.sqlite3")
    records = await store.list(namespace=metadata["namespace"])
    transitions = sorted(
        (r for r in records if r.record_type == "experience-transition"),
        key=lambda r: (r.payload["iteration"], r.created_at, r.record_id),
    )
    run_ids = {r.payload["run_id"] for r in transitions}
    assert len(run_ids) == 1
    run_id = run_ids.pop()
    handoff = ObservationHandoff(store, metadata["namespace"])
    handoff.begin(run_id)
    reader = StoredObservationReader(store, namespace=metadata["namespace"], run_id=run_id)
    history = ObservationHistory()
    catalogue = OrderedDict()
    rows = []
    for index, record in enumerate(transitions):
        payload = record.payload
        iteration = payload["iteration"]
        history.record(iteration, payload["action"], ToolResult.model_validate(payload["result"]))
        await handoff.note_transition(record.record_id, current_iteration=iteration + 1)
        bookmark = await reader.bookmark(record.record_id, current_iteration=iteration + 1)
        key = hashlib.sha256(encoded(payload["action"])).hexdigest()
        catalogue.pop(key, None)
        catalogue[key] = bookmark
        while len(catalogue) > 24:
            catalogue.popitem(last=False)
        if (
            index + 1 < len(transitions)
            and transitions[index + 1].payload["iteration"] == iteration
        ):
            continue
        # Include all historical records for every representation. This is a
        # collection-size comparison, not reconstruction of an exact live prompt.
        baseline = history.snapshot()
        current = handoff.snapshot(current_iteration=iteration + 1)
        compact = [
            {
                "ref": b.record_id,
                "iteration": b.source_iteration,
                "result_bytes": b.result_bytes,
                "summary": (b.summary or "")[:96],
                "historical_evidence": b.historical_evidence,
            }
            for b in catalogue.values()
        ]
        current_iterations = {b["source_iteration"] for b in current}
        baseline_iterations = {json.loads(row)["iteration"] for row in baseline}
        rows.append(
            {
                "iteration": iteration,
                "baseline_history_bytes": len(encoded(baseline)),
                "current_full_bookmarks_bytes": len(encoded(current)),
                "additive_history_and_bookmarks_bytes": len(
                    encoded(
                        {
                            "observation_history": baseline,
                            "observation_bookmarks": current,
                        }
                    )
                ),
                "candidate_compact_catalogue_bytes": len(encoded(compact)),
                "baseline_history_records": len(baseline),
                "current_bookmarks": len(current),
                "candidate_catalogue_records": len(compact),
                "history_iterations_without_current_ref": len(
                    baseline_iterations - current_iterations
                ),
            }
        )
    metrics = {name: describe([r[name] for r in rows]) for name in rows[0] if name != "iteration"}
    report = {
        "source_run": run_id,
        "transitions": len(transitions),
        "decision_prefixes": len(rows),
        "source_members_digest": hashlib.sha256(
            encoded(
                [[r.record_id, r.content_hash] for r in sorted(records, key=lambda r: r.record_id)]
            )
        ).hexdigest(),
        "metrics": metrics,
        "model_calls": 0,
        "simulator_calls": 0,
        "limitation": (
            "UTF-8 serialized collection bytes, not tokens or utility. Candidate catalogue "
            "uses 24 distinct-action entries and 96-character summaries; not runner-enabled. "
            "Exact live latest-exclusion and memory_context costs are not reconstructed."
        ),
    }
    (output / "prefixes.json").write_text(json.dumps(rows, indent=2) + "\n")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(measure(args.source, args.output))
