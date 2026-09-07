"""Prepare public historical-detail questions and separate graders; no model calls."""

import argparse
import hashlib
import json
from pathlib import Path

from probe_memory_evidence import read_frozen_export


def prepare(args):
    _, source_hash, records = read_frozen_export(args.manifest, args.records)
    transitions = [r for r in records if r.record_type == "experience-transition"]
    by_id = {r.record_id: r for r in transitions}
    entries = []
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cases").mkdir()
    (args.output / "grading").mkdir()
    for index, target in enumerate(json.loads(args.targets.read_text()), 1):
        record = by_id[target["record_id"]]
        cutoff = target["cutoff_iteration"]
        assert record.payload["iteration"] < cutoff
        path = target["response_path"]
        assert path.startswith("result.data.")
        value = record.payload
        for key in path.split("."):
            value = value[int(key)] if isinstance(value, list) else value[key]
        assert value is not None and isinstance(value, (str, int, float, bool))
        kind = record.payload["action"]["kind"]
        iteration = record.payload["iteration"]
        assert (
            sum(
                r.payload["iteration"] == iteration and r.payload["action"]["kind"] == kind
                for r in transitions
            )
            == 1
        )
        case_id = f"detail-{index:02d}"
        case = {
            "case_id": case_id,
            "question": (
                f"[Development historical-detail lookup] At source iteration {iteration}, "
                f"for the recorded {kind} action, what exact scalar value was observed "
                f"at {path}? Dot-separated numeric path segments are zero-based array "
                "indexes. Return the recorded scalar as a string."
            ),
            "cutoff_iteration": cutoff,
            "target_record_id": record.record_id,
            "target_record_hash": record.content_hash,
            "retained_record_ids": [
                r.record_id for r in transitions if r.payload["iteration"] <= cutoff
            ],
            "stratum": "development_detail_visibility_requires_separate_preflight",
        }
        grading = {"case_id": case_id, "expected_value": value, "response_path": path}
        entry = {"case_id": case_id}
        for name, directory, data in [("question", "cases", case), ("grading", "grading", grading)]:
            relative = f"{directory}/{case_id}.json"
            raw = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
            (args.output / relative).write_bytes(raw)
            entry[name + "_file"] = relative
            entry[name + "_sha256"] = hashlib.sha256(raw).hexdigest()
        entries.append(entry)
    manifest = {
        "source_sha256": source_hash,
        "cases": entries,
        "selection_rule": "Explicit development record/path/cutoff targets; values extracted "
        "from verified public observations only into separate graders.",
        "targets_sha256": hashlib.sha256(args.targets.read_bytes()).hexdigest(),
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"cases": len(entries), "model_calls": 0, "simulator_calls": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "records", "targets", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    prepare(parser.parse_args())
