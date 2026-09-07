"""Derive strict memory from completed real filesystem runs on a copied store."""

import argparse
import asyncio
import json
import sqlite3
from collections import Counter
from pathlib import Path

from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.memory.candidate_validation import extract_candidates, validate_candidate
from uptick_agent.memory.config import ContextBudgetConfig, MemoryConfiguration, ModuleConfig
from uptick_agent.memory.contracts import MemoryContextRequest, RunOutcome
from uptick_agent.memory.lesson_contracts import LessonEvidence, LessonSettings
from uptick_agent.memory.patterns import generate_pattern_candidates, validate_pattern_candidate
from uptick_agent.memory.playbooks import generate_playbook_candidates, validate_playbook_candidate
from uptick_agent.memory.settings import (
    PatternQuerySettings,
    PlaybookQuerySettings,
    ToolKnowledgeQuerySettings,
)
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.memory.tool_knowledge import (
    generate_tool_knowledge_candidates,
    validate_tool_knowledge_candidate,
)


async def probe(args):
    args.output.mkdir(parents=True, exist_ok=False)
    evidence = LessonEvidence.model_validate_json((args.source / "evidence.json").read_text())
    source_report = json.loads((args.source / "report.json").read_text())
    namespace = source_report["namespace"]
    with (
        sqlite3.connect(args.source / "memory.sqlite") as source,
        sqlite3.connect(args.output / "memory.sqlite") as target,
    ):
        source.backup(target)
    store = SqliteStructuredStore(args.output / "memory.sqlite")
    stored = await store.list(namespace=namespace)
    assert {r.record_id: r.content_hash for r in stored} == {
        r.record_id: r.content_hash for r in evidence.records
    }
    lessons = LessonSettings(
        metric_name="duplicate_lines",
        metric_unit="lines",
        direction="minimize",
        condition_keys=("format",),
    )
    scope_paths = tuple(args.scope_path or ("observation.format",))
    world = PatternQuerySettings(
        scope_paths=scope_paths, action_path="action.kind", result_path="result.data.changed"
    )
    tools = ToolKnowledgeQuerySettings(
        adapter_identity=evidence.runs[0].environment_content_hash,
        scope_paths=scope_paths,
        action_path="action.kind",
        input_paths=("action.kind",),
        response_path="result.data.changed",
    )
    playbooks = PlaybookQuerySettings(
        scope_paths=scope_paths,
        action_path="action.kind",
        sequence_length=2,
        guard_path="result.data.duplicate_lines",
        guard_value=0,
    )
    reports = {}
    for name, settings, generate, validate in [
        ("lessons", lessons, extract_candidates, validate_candidate),
        ("world", world, generate_pattern_candidates, validate_pattern_candidate),
        ("tools", tools, generate_tool_knowledge_candidates, validate_tool_knowledge_candidate),
        ("playbooks", playbooks, generate_playbook_candidates, validate_playbook_candidate),
    ]:
        candidates = generate(evidence, settings)
        validated = [validate(candidate, evidence, settings) for candidate in candidates]
        reports[name] = {
            "candidates": len(candidates),
            "statuses": dict(Counter(v.status for v in validated)),
        }
        (args.output / f"{name}-validation.json").write_text(
            json.dumps([v.model_dump(mode="json") for v in validated], indent=2) + "\n"
        )
    enabled = ModuleConfig(enabled=True, max_context_items=8, max_context_tokens=8000)
    configuration = MemoryConfiguration(
        profile_id="file-task-strict-learning",
        profile_kind="experiment",
        compatibility_legacy=ModuleConfig(enabled=False),
        episodic=enabled,
        lessons=enabled,
        world_model=enabled,
        tool_knowledge=enabled,
        playbooks=enabled,
        lesson_settings=lessons,
        world_query_settings=world,
        tool_knowledge_query_settings=tools,
        playbook_query_settings=playbooks,
        context_budget=ContextBudgetConfig(total_items=32, total_tokens=32000),
    )
    runtime = compose_experimental_runtime(
        configuration, store, namespace=namespace, run_declarations=evidence.runs
    )
    # Finalize the exact stored outcomes; no fabricated transitions or results.
    for record in evidence.records:
        if record.record_type == "run-outcome":
            await runtime.finalize_run(RunOutcome.model_validate(record.payload))
    context = await runtime.build_context(
        MemoryContextRequest(
            request_id="file-strict-read-01",
            run_id="file-unseen-read-probe",
            query="deduplicate inspect duplicate_lines changed",
        )
    )
    (args.output / "context.json").write_text(context.model_dump_json(indent=2) + "\n")
    (args.output / "configuration.json").write_text(configuration.model_dump_json(indent=2) + "\n")
    report = {
        "modules": reports,
        "context_items": len(context.items),
        "warnings": context.warnings,
        "model_calls": 0,
        "scope": "real local task strict extraction/persistence/retrieval; no model utility",
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--scope-path",
        action="append",
        help="Observed scope path for world/tools/playbooks; repeat for conjunction",
    )
    asyncio.run(probe(parser.parse_args()))
