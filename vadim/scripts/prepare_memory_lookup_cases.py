"""Prepare frozen, offline memory-lookup questions from corpus08.

This is an experiment-preparation script only.  It reads the canonical JSONL
export through :func:`probe_memory_evidence.read_frozen_export`, never opens
the source SQLite database, and never calls a provider, simulator, or model.

The public case files contain a question and source provenance but no expected
answer.  Expected scalar values live in separate grading files so an executor
can prove that its prompt did not contain the target answer.  The output
directory is created with ``exist_ok=False`` to prevent replacing a frozen
experiment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

# The probe module is a sibling script rather than a package module.  Importing
# it gives this preparation step the same frozen-source verification used by
# the existing corpus diagnostics.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from probe_memory_evidence import read_frozen_export  # noqa: E402

from uptick_agent.decisions.runtime import ToolResult  # noqa: E402
from uptick_agent.runs.observations import ObservationHistory  # noqa: E402

CASE_COUNT = 4
HISTORY_CASE_COUNT = 2
ACTION_WINDOW = 24
TARGET_ACTION_KIND = "get_operation"
TARGET_PATH = "result.data.status"
# Optional second preparation profile.  It deliberately selects two public
# error-code rows and two differing status rows so a utility probe cannot pass
# by guessing the most common status.  The expected values remain in grading
# files only; this tuple is never serialized into the manifest or questions.
DIVERSE_TARGETS = (
    ("probe_page", "result.data.code", "PRODUCT_NOT_FOUND", 0.10),
    ("get_operation", "result.data.code", "OPERATION_NOT_FOUND", 0.20),
    ("advance_time_v2", "result.data.status", "queued", 0.60),
    ("get_operation", "result.data.status", "succeeded", 0.85),
)
DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = (
    DEFAULT_ROOT / "artifacts" / "paired-memory-preparation" / "corpus-08" / "corpus-manifest.json"
)
DEFAULT_RECORDS = DEFAULT_MANIFEST.with_name("corpus-records.jsonl")
DEFAULT_OUTPUT = DEFAULT_ROOT / "artifacts" / "memory-tower-preparation" / "lookup-cases-01"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _action_key(action: dict[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(action).encode("utf-8"))


def _scalar_at(payload: dict[str, Any], path: str) -> object | None:
    value: object = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    # ``None`` is deliberately not a lookup answer.  The question contract
    # requires a public scalar field with an observed value.
    if value is None or not isinstance(value, (bool, int, float, str)):
        return None
    return value


def _transition_rows(records: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        if record.record_type != "experience-transition":
            continue
        payload = dict(record.payload)
        action = payload.get("action")
        result = payload.get("result")
        iteration = payload.get("iteration")
        if not isinstance(action, dict) or not isinstance(result, dict):
            raise ValueError("canonical transition is missing action/result objects")
        if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 1:
            raise ValueError("canonical transition has an invalid iteration")
        rows.append(
            {
                "record_id": record.record_id,
                "record_hash": record.content_hash,
                "created_at": record.created_at,
                "iteration": iteration,
                "action": action,
                "result": result,
            }
        )
    rows.sort(key=lambda row: (row["created_at"], row["record_id"]))
    if not rows:
        raise ValueError("frozen export contains no experience transitions")
    if any(
        rows[index]["iteration"] > rows[index + 1]["iteration"] for index in range(len(rows) - 1)
    ):
        raise ValueError("canonical transition order is not monotonic by iteration")
    return rows


def _action_lru(rows: list[dict[str, Any]]) -> OrderedDict[str, int]:
    """Return the last distinct action keys with their last source indexes."""

    recent: OrderedDict[str, int] = OrderedDict()
    for index, row in enumerate(rows):
        key = _action_key(row["action"])
        recent.pop(key, None)
        recent[key] = index
        if len(recent) > ACTION_WINDOW:
            recent.popitem(last=False)
    return recent


def _has_earlier_completed_result(rows: list[dict[str, Any]], target_index: int) -> bool:
    """Require a completed public result in the preceding 24 distinct actions."""

    previous = _action_lru(rows[:target_index])
    for index in reversed(previous.values()):
        row = rows[index]
        if row["action"].get("kind") != TARGET_ACTION_KIND:
            continue
        if _scalar_at({"result": row["result"]}, TARGET_PATH) == "succeeded":
            return True
    return False


def _eligible_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = []
    all_keys = [_action_key(row["action"]) for row in rows]
    for index, row in enumerate(rows):
        if row["action"].get("kind") != TARGET_ACTION_KIND:
            continue
        answer = _scalar_at({"result": row["result"]}, TARGET_PATH)
        if not isinstance(answer, str):
            continue
        # A repeated exact action would cause ObservationHistory to replace
        # the old result.  Such a row cannot be an unambiguous lookup target.
        if all_keys.count(all_keys[index]) != 1:
            continue
        if not _has_earlier_completed_result(rows, index):
            continue
        if not any(later["iteration"] > row["iteration"] for later in rows[index + 1 :]):
            continue
        eligible.append({"index": index, "answer": answer, **row})
    return eligible


def _select_diverse_targets(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    selected = []
    eligible_count = 0
    all_keys = [_action_key(row["action"]) for row in rows]
    for action_kind, response_path, expected_value, anchor_fraction in DIVERSE_TARGETS:
        eligible = []
        for index, row in enumerate(rows):
            if row["action"].get("kind") != action_kind:
                continue
            answer = _scalar_at({"result": row["result"]}, response_path)
            if answer != expected_value or all_keys.count(all_keys[index]) != 1:
                continue
            if not _has_earlier_completed_result(rows, index):
                continue
            if not any(later["iteration"] > row["iteration"] for later in rows[index + 1 :]):
                continue
            eligible.append({"index": index, "answer": answer, **row})
        if not eligible:
            raise ValueError(f"no eligible diverse target for {action_kind} {response_path}")
        eligible_count += len(eligible)
        anchor = round((len(rows) - 1) * anchor_fraction)
        selected.append(min(eligible, key=lambda row: (abs(row["index"] - anchor), row["index"])))
    return selected, eligible_count


def _evenly_spaced(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if len(items) < count:
        raise ValueError(f"only {len(items)} eligible targets; need {count}")
    # Interior quantiles leave room after every target for a strictly later
    # cutoff and make the selection independent of hand-picked iterations.
    selected = []
    for slot in range(1, count + 1):
        index = round(slot * (len(items) + 1) / (count + 1)) - 1
        index = max(0, min(index, len(items) - 1))
        if selected and items[index]["record_id"] == selected[-1]["record_id"]:
            index += 1
        selected.append(items[index])
    return selected


def _cutoff_iterations(rows: list[dict[str, Any]], target: dict[str, Any]) -> list[int]:
    return sorted({row["iteration"] for row in rows if row["iteration"] > target["iteration"]})


def _history_at(rows: list[dict[str, Any]], cutoff: int) -> ObservationHistory:
    history = ObservationHistory()
    for row in rows:
        # The executor uses the inclusive source-iteration prefix.  The target
        # is still strictly earlier because every chosen cutoff is greater.
        if row["iteration"] > cutoff:
            break
        history.record(
            row["iteration"],
            row["action"],
            ToolResult.model_validate(row["result"]),
        )
    return history


def _history_contains(history: ObservationHistory, action: dict[str, Any]) -> bool:
    # ObservationHistory intentionally exposes only serialized snapshots.  Its
    # production membership key is the exact action digest; read that opaque
    # key here to classify strata without emitting or rewriting history data.
    records = getattr(history, "_records", None)
    if not isinstance(records, OrderedDict):
        raise TypeError("production ObservationHistory no longer exposes its LRU map")
    return _action_key(action) in records


def _distinct_action_distance(
    rows: list[dict[str, Any]], target: dict[str, Any], cutoff: int
) -> int:
    target_index = target["index"]
    keys: set[str] = set()
    for row in rows[target_index:]:
        if row["iteration"] > cutoff:
            break
        keys.add(_action_key(row["action"]))
    return len(keys)


def _find_cutoff(
    rows: list[dict[str, Any]], target: dict[str, Any], *, want_in_history: bool
) -> tuple[int, int]:
    for cutoff in _cutoff_iterations(rows, target):
        distance = _distinct_action_distance(rows, target, cutoff)
        if distance > ACTION_WINDOW:
            continue
        history = _history_at(rows, cutoff)
        if _history_contains(history, target["action"]) is want_in_history:
            return cutoff, distance
    stratum = "production history" if want_in_history else "outside production history"
    raise ValueError(
        f"corpus does not provide a {stratum} target within {ACTION_WINDOW} distinct actions"
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _question(case_id: str, iteration: int) -> str:
    return (
        f"[Development/stress lookup; not held-out economic evidence] At source iteration "
        f"{iteration}, for the recorded {TARGET_ACTION_KIND} action, what exact scalar value "
        f"was observed at {TARGET_PATH}? Return the recorded value only."
    )


def _question_for(iteration: int, action_kind: str, response_path: str) -> str:
    return (
        "[Development/stress lookup; not held-out economic evidence] At source iteration "
        f"{iteration}, for the recorded {action_kind} action, what exact scalar value "
        f"was observed at {response_path}? Return the recorded value only."
    )


def prepare(
    manifest_path: Path = DEFAULT_MANIFEST,
    records_path: Path = DEFAULT_RECORDS,
    output: Path = DEFAULT_OUTPUT,
    *,
    diverse: bool = False,
) -> dict[str, Any]:
    manifest_bytes, source_hash, records = read_frozen_export(manifest_path, records_path)
    rows = _transition_rows(records)
    if diverse:
        selected, eligible_count = _select_diverse_targets(rows)
    else:
        eligible = _eligible_targets(rows)
        selected = _evenly_spaced(eligible, CASE_COUNT)
        eligible_count = len(eligible)

    cases: list[dict[str, Any]] = []
    for position, target in enumerate(selected):
        want_in_history = position < HISTORY_CASE_COUNT
        cutoff, distance = _find_cutoff(rows, target, want_in_history=want_in_history)
        case_id = f"dev-stress-{position + 1:02d}"
        retained_ids = [row["record_id"] for row in rows if row["iteration"] <= cutoff]
        if diverse:
            action_kind, response_path, _expected_value, _anchor_fraction = DIVERSE_TARGETS[
                position
            ]
        else:
            action_kind, response_path = TARGET_ACTION_KIND, TARGET_PATH
        case = {
            "case_id": case_id,
            "question": _question_for(target["iteration"], action_kind, response_path),
            "cutoff_iteration": cutoff,
            "target_record_id": target["record_id"],
            "target_record_hash": target["record_hash"],
            "retained_record_ids": retained_ids,
            "stratum": (
                "production_observation_history"
                if want_in_history
                else "outside_production_history_within_24_distinct_actions"
            ),
        }
        grading = {
            "case_id": case_id,
            "expected_value": target["answer"],
            "response_path": response_path,
        }
        cases.append(
            {
                "case": case,
                "grading": grading,
                "target_index": target["index"],
                "distinct_action_distance": distance,
            }
        )

    # Never overwrite a prior preparation.  Create all parents only after the
    # source has passed integrity checks and selection has succeeded.
    output.mkdir(parents=True, exist_ok=False)
    cases_dir = output / "cases"
    grading_dir = output / "grading"
    cases_dir.mkdir()
    grading_dir.mkdir()

    manifest_cases = []
    for entry in cases:
        case = entry["case"]
        grading = entry["grading"]
        case_path = cases_dir / f"{case['case_id']}.json"
        grading_path = grading_dir / f"{case['case_id']}.json"
        _write_json(case_path, case)
        _write_json(grading_path, grading)
        manifest_cases.append(
            {
                "case_id": case["case_id"],
                "question_file": f"cases/{case_path.name}",
                "question_sha256": _sha256_bytes(case_path.read_bytes()),
                "grading_file": f"grading/{grading_path.name}",
                "grading_sha256": _sha256_bytes(grading_path.read_bytes()),
            }
        )

    selection_rule = (
        (
            "v2: sort canonical experience-transition rows by created_at then record_id; "
            "retain rows whose selected public result.data.status or result.data.code is "
            "scalar and whose exact action is unique, with an earlier completed get_operation "
            "result in the preceding 24 distinct action keys; choose the configured public "
            "value strata at deterministic source-prefix anchors; use the earliest later "
            "inclusive iteration that classifies cases 01-02 inside production "
            "ObservationHistory and cases 03-04 outside it while remaining within 24 distinct "
            "action keys"
        )
        if diverse
        else (
            "v1: sort canonical experience-transition rows by created_at then record_id; "
            "retain get_operation rows whose scalar result.data.status exists, whose exact "
            "action is unique, and which have an earlier get_operation result.data.status="
            "succeeded in the preceding 24 distinct action keys; choose four interior "
            "quantiles; use the earliest later inclusive iteration that classifies cases 01-02 "
            "inside production ObservationHistory and cases 03-04 outside it while remaining "
            "within 24 distinct action keys"
        )
    )
    manifest = {
        "source_sha256": source_hash,
        "cases": manifest_cases,
        "selection_rule": selection_rule,
    }
    _write_json(output / "manifest.json", manifest)

    source_manifest_sha256 = _sha256_bytes(manifest_bytes)
    profile = "diverse value-stratified v2" if diverse else "quantile v1"
    readme = (
        f"# {output.name}\n\n"
        f"Offline {profile} development/stress lookup preparation from the frozen public corpus08. "
        "This directory contains no provider or simulator output and is not held-out "
        "economic evidence.\n\n"
        f"Source records SHA-256: `{source_hash}`\n\n"
        f"Source manifest SHA-256: `{source_manifest_sha256}`\n\n"
        "The public case files contain questions, inclusive cutoff iterations, canonical "
        "record IDs/hashes, and retained prefix IDs. Expected values are isolated under "
        "`grading/` and must never be included in a model request. Cases 01-02 are selected "
        "where the target remains in the production bounded observation history; cases "
        "03-04 are selected after that history evicts the target while the target remains "
        "within the 24-distinct-action lookup window. The split is a deterministic stress "
        "stratification, not a representative or causal sample.\n\n"
        "To regenerate in a fresh output directory, run from `vadim/`: `uv run python "
        "scripts/prepare_memory_lookup_cases.py --output <new-directory>`. The default "
        "output is intentionally refuse-to-overwrite.\n"
    )
    (output / "README.md").write_text(readme)

    return {
        "status": "prepared_offline",
        "provider_calls": 0,
        "simulator_calls": 0,
        "model_calls": 0,
        "source_records": len(records),
        "source_transitions": len(rows),
        "eligible_targets": eligible_count,
        "cases": manifest_cases,
        "manifest_sha256": _sha256_bytes((output / "manifest.json").read_bytes()),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--diverse",
        action="store_true",
        help="select public code and status strata for a non-guessable utility probe",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(args.manifest, args.records, args.output, diverse=args.diverse), indent=2
        )
    )
