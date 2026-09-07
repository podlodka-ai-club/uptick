"""Compare explicit condition projections on the same verified real evidence."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.patterns import generate_pattern_candidates, validate_pattern_candidate
from uptick_agent.memory.playbooks import generate_playbook_candidates, validate_playbook_candidate
from uptick_agent.memory.settings import (
    PatternQuerySettings,
    PlaybookQuerySettings,
    ToolKnowledgeQuerySettings,
)
from uptick_agent.memory.tool_knowledge import (
    generate_tool_knowledge_candidates,
    validate_tool_knowledge_candidate,
)


def probe(args):
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.evidence.read_bytes()
    evidence = LessonEvidence.model_validate_json(source)
    variants = {
        "format_only": ("observation.format",),
        "format_and_duplicate_count": ("observation.format", "observation.data.duplicate_lines"),
        "format_and_exact_content": ("observation.format", "observation.data.content_hash"),
    }
    report = {}
    for label, paths in variants.items():
        world = PatternQuerySettings(
            scope_paths=paths, action_path="action.kind", result_path="result.data.changed"
        )
        tools = ToolKnowledgeQuerySettings(
            adapter_identity=evidence.runs[0].environment_content_hash,
            scope_paths=paths,
            action_path="action.kind",
            input_paths=("action.kind",),
            response_path="result.data.changed",
        )
        playbook = PlaybookQuerySettings(
            scope_paths=paths,
            action_path="action.kind",
            sequence_length=2,
            guard_path="result.data.duplicate_lines",
            guard_value=0,
        )
        report[label] = {}
        for module, settings, generate, validate in [
            ("world", world, generate_pattern_candidates, validate_pattern_candidate),
            ("tools", tools, generate_tool_knowledge_candidates, validate_tool_knowledge_candidate),
            ("playbooks", playbook, generate_playbook_candidates, validate_playbook_candidate),
        ]:
            items = [validate(c, evidence, settings) for c in generate(evidence, settings)]
            report[label][module] = {
                "candidates": len(items),
                "statuses": dict(Counter(x.status for x in items)),
            }
            (args.output / f"{label}-{module}.json").write_text(
                json.dumps(
                    {
                        "settings": settings.model_dump(mode="json"),
                        "items": [x.model_dump(mode="json") for x in items],
                    },
                    indent=2,
                )
                + "\n"
            )
    if args.evidence.read_bytes() != source:
        raise ValueError("source evidence changed during comparison")
    result = {
        "variants": report,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "model_calls": 0,
        "source_modified": False,
        "scope": "analyst-configured condition comparison; no automatic discovery or utility",
    }
    (args.output / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    probe(parser.parse_args())
