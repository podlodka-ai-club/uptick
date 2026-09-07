"""Grade an existing offline memory-lookup execution.

This is a post-hoc QA grader for the development lookup experiment.  It reads
only the frozen case manifest/grading files and an already-created execution
directory.  It never creates a provider client, opens a simulator, reads
request bodies, or treats a missing/failed arm as a successful omission.

The output directory refuses replacement.  No causal, economic, or held-out
claim is made by this artifact; it reports exact scalar lookup agreement and
runtime accounting only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from uptick_agent.llm.contracts import LlmCallTelemetry

ARM_NAMES = ("no_evidence", "baseline", "compact")
TELEMETRY_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "cost_minor",
)
RESPONSE_PATH = re.compile(r"result\.data(?:\.[A-Za-z0-9_]+)+")


class GradingInputError(ValueError):
    """The selected case or execution input cannot be graded safely."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise GradingInputError(f"cannot read JSON input {path}: {error}") from error


def _require_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GradingInputError(f"{label} must be a JSON object")
    return value


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GradingInputError(f"{label} must be a non-empty string")
    return value


def _load_cases(cases_dir: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest_path = cases_dir / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = _require_dict(_load_json(manifest_path), "case manifest")
    source_sha256 = _require_string(manifest.get("source_sha256"), "manifest.source_sha256")
    if len(source_sha256) != 64:
        raise GradingInputError("manifest.source_sha256 must be a SHA-256 hex digest")
    entries = manifest.get("cases")
    if not isinstance(entries, list) or not entries:
        raise GradingInputError("manifest.cases must be a non-empty list")
    cases: dict[str, dict[str, Any]] = {}
    for entry_index, raw_entry in enumerate(entries):
        entry = _require_dict(raw_entry, f"manifest.cases[{entry_index}]")
        case_id = _require_string(entry.get("case_id"), "manifest case_id")
        if case_id in cases:
            raise GradingInputError(f"duplicate case_id {case_id!r}")
        question_rel = _require_string(entry.get("question_file"), f"{case_id}.question_file")
        grading_rel = _require_string(entry.get("grading_file"), f"{case_id}.grading_file")
        question_path = cases_dir / question_rel
        grading_path = cases_dir / grading_rel
        if _sha256(question_path) != entry.get("question_sha256"):
            raise GradingInputError(f"question hash mismatch for {case_id}")
        if _sha256(grading_path) != entry.get("grading_sha256"):
            raise GradingInputError(f"grading hash mismatch for {case_id}")
        case = _require_dict(_load_json(question_path), f"public case {case_id}")
        grading = _require_dict(_load_json(grading_path), f"grading case {case_id}")
        if case.get("case_id") != case_id or grading.get("case_id") != case_id:
            raise GradingInputError(f"case ID mismatch in files for {case_id}")
        _require_string(case.get("question"), f"{case_id}.question")
        expected = grading.get("expected_value")
        if expected is None or not isinstance(expected, (str, int, float, bool)):
            raise GradingInputError(f"{case_id}.expected_value must be a scalar")
        response_path = _require_string(grading.get("response_path"), f"{case_id}.response_path")
        if not RESPONSE_PATH.fullmatch(response_path):
            raise GradingInputError(f"unsupported response path for {case_id}: {response_path}")
        # Reject explicit answer fields and verbatim string answers in the
        # question. Numeric coincidences (e.g. iteration 12, target value 12)
        # cannot establish leakage; prefix/context isolation is checked separately.
        if isinstance(expected, str) and expected in case["question"]:
            raise GradingInputError(f"public question leaks the expected value for {case_id}")
        if "expected_value" in case:
            raise GradingInputError(f"public case contains a grading field for {case_id}")
        cases[case_id] = {
            "case_id": case_id,
            "expected_value": expected,
            "response_path": response_path,
            "question_file": question_rel,
            "grading_file": grading_rel,
        }
    return {
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "source_sha256": source_sha256,
        "case_count": len(cases),
        "selection_rule": manifest.get("selection_rule"),
    }, cases


def _parse_telemetry(value: object, label: str) -> tuple[LlmCallTelemetry | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, f"{label}.telemetry must be an object or null"
    try:
        return LlmCallTelemetry(**value), None
    except (TypeError, ValueError) as error:
        return None, f"{label}.telemetry violates LlmCallTelemetry: {error}"


def _usage_summary(telemetries: list[LlmCallTelemetry], missing_count: int) -> dict[str, Any]:
    values: dict[str, list[int | None]] = {
        field: [getattr(telemetry, field) for telemetry in telemetries]
        for field in TELEMETRY_FIELDS
    }
    totals = {
        field: (
            sum(value for value in field_values if value is not None)
            if telemetries
            and missing_count == 0
            and all(value is not None for value in field_values)
            else None
        )
        for field, field_values in values.items()
    }
    nullable_fields = {
        field: sum(value is None for value in field_values)
        for field, field_values in values.items()
    }
    return {
        "calls_with_telemetry": len(telemetries),
        "calls_without_telemetry": missing_count,
        "nullable_field_counts": nullable_fields,
        "token_and_cost_totals": totals,
        "total_elapsed_seconds": sum(telemetry.elapsed_seconds for telemetry in telemetries),
        "request_count": sum(telemetry.request_count for telemetry in telemetries),
        "retry_count": sum(telemetry.retry_count for telemetry in telemetries),
        "usage_reported_requests": sum(
            telemetry.usage_reported_requests for telemetry in telemetries
        ),
        "totals_are_complete": bool(telemetries)
        and missing_count == 0
        and all(
            not any(value is None for value in field_values) for field_values in values.values()
        ),
    }


def _read_calls(
    calls_path: Path, label: str
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    if not calls_path.exists():
        return (
            [],
            {
                "present": False,
                "logical_decisions": 0,
                "read_choices": 0,
                "provider_attempts": 0,
                "provider_attempt_outcomes": {},
                "telemetries": [],
                "telemetry_missing": 0,
                "attempts_with_usage": 0,
                "attempts_without_usage": 0,
            },
            [],
        )
    raw = _load_json(calls_path)
    if not isinstance(raw, list):
        return [], {}, [f"{label}.calls.json must be a list"]
    errors: list[str] = []
    telemetries: list[LlmCallTelemetry] = []
    telemetry_missing = 0
    provider_attempts = 0
    attempts_with_usage = 0
    attempts_without_usage = 0
    attempt_outcomes: Counter[str] = Counter()
    read_choices = 0
    for index, raw_call in enumerate(raw):
        call = _require_dict(raw_call, f"{label}.calls[{index}]")
        choice = call.get("choice")
        if isinstance(choice, dict) and choice.get("kind") == "read":
            read_choices += 1
            if not isinstance(choice.get("ref"), str) or not choice["ref"]:
                errors.append(f"{label}.calls[{index}] read choice has no ref")
        telemetry, telemetry_error = _parse_telemetry(
            call.get("telemetry"), f"{label}.calls[{index}]"
        )
        if telemetry_error:
            errors.append(telemetry_error)
        elif telemetry is None:
            telemetry_missing += 1
        else:
            telemetries.append(telemetry)
        attempts = call.get("provider_attempts")
        if not isinstance(attempts, list):
            errors.append(f"{label}.calls[{index}].provider_attempts must be a list")
            continue
        provider_attempts += len(attempts)
        for attempt_index, raw_attempt in enumerate(attempts):
            attempt = _require_dict(
                raw_attempt, f"{label}.calls[{index}].provider_attempts[{attempt_index}]"
            )
            outcome = attempt.get("outcome")
            if isinstance(outcome, str) and outcome:
                attempt_outcomes[outcome] += 1
            else:
                errors.append(
                    f"{label}.calls[{index}].provider_attempts[{attempt_index}] has no outcome"
                )
            usage = attempt.get("usage")
            if usage is None:
                attempts_without_usage += 1
            elif isinstance(usage, dict):
                attempts_with_usage += 1
            else:
                errors.append(
                    f"{label}.calls[{index}].provider_attempts[{attempt_index}].usage is invalid"
                )
    accounting = {
        "present": True,
        "logical_decisions": len(raw),
        "read_choices": read_choices,
        "provider_attempts": provider_attempts,
        "provider_attempt_outcomes": dict(sorted(attempt_outcomes.items())),
        "telemetries": telemetries,
        "telemetry_missing": telemetry_missing,
        "attempts_with_usage": attempts_with_usage,
        "attempts_without_usage": attempts_without_usage,
    }
    return raw, accounting, errors


def _grade_answer(result: dict[str, Any], expected: object) -> str:
    status = result.get("status")
    answer = result.get("answer")
    if status == "failed":
        return "failed"
    if status == "budget_exhausted":
        return "unknown"
    if status != "answered":
        return "failed"
    if answer is None:
        return "unknown"
    if isinstance(answer, str) and answer.strip().lower() == "unknown":
        return "unknown"
    scalar_text = expected if isinstance(expected, str) else json.dumps(expected, allow_nan=False)
    return "correct" if answer == scalar_text else "incorrect"


def _grade_arm(
    *,
    run_dir: Path,
    case: dict[str, Any],
    arm: str,
    reported: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    case_id = case["case_id"]
    label = f"{case_id}/{arm}"
    arm_dir = run_dir / case_id / arm
    result_path = arm_dir / "result.json"
    errors: list[str] = []
    if not result_path.exists():
        return {
            "case_id": case_id,
            "arm": arm,
            "grade": "missing",
            "run_status": None,
            "logical_decisions": 0,
            "logical_decisions_reported": None,
            "read_choices": 0,
            "provider_attempts": 0,
            "telemetry": _usage_summary([], 0),
            "provider_attempt_usage": {"with_usage": 0, "without_usage": 0},
        }, [f"missing result.json for {label}"]
    result = _require_dict(_load_json(result_path), label + ".result.json")
    calls, accounting, call_errors = _read_calls(arm_dir / "calls.json", label)
    errors.extend(call_errors)
    if result.get("case_id") != case_id:
        errors.append(f"{label} result.case_id does not match its path")
    if result.get("arm") != arm:
        errors.append(f"{label} result.arm does not match its path")
    reported_decisions = result.get("decisions")
    if isinstance(reported_decisions, bool) or not isinstance(reported_decisions, int):
        errors.append(f"{label}.result.decisions must be an integer")
        reported_decisions = None
    if accounting.get("present") and reported_decisions != len(calls):
        errors.append(
            f"{label} decisions mismatch: result={reported_decisions}, calls={len(calls)}"
        )
    if not accounting.get("present") and reported_decisions not in (None, 0):
        errors.append(f"{label} has decisions but no calls.json")
    if reported is None:
        errors.append(f"missing report.json entry for {label}")
    else:
        for field in ("status", "decisions", "answer"):
            if reported.get(field) != result.get(field):
                errors.append(f"{label} report/result mismatch in {field}")
    status = result.get("status")
    if status == "answered":
        if not calls:
            errors.append(f"{label} answered result has no decision call")
        else:
            last_choice = calls[-1].get("choice")
            if not isinstance(last_choice, dict) or last_choice.get("kind") != "answer":
                errors.append(f"{label} answered result has no final answer choice")
            elif last_choice.get("value") != result.get("answer"):
                errors.append(f"{label} final answer choice differs from result.answer")
    grade = _grade_answer(result, case["expected_value"])
    if errors and grade == "correct":
        # A structurally inconsistent run cannot be counted as correct merely
        # because one copied field happens to equal the grader value.
        grade = "failed"
    return {
        "case_id": case_id,
        "arm": arm,
        "grade": grade,
        "run_status": status,
        "logical_decisions": accounting.get("logical_decisions", 0),
        "logical_decisions_reported": reported_decisions,
        "read_choices": accounting.get("read_choices", 0),
        "provider_attempts": accounting.get("provider_attempts", 0),
        "telemetry": _usage_summary(
            accounting.get("telemetries", []), accounting.get("telemetry_missing", 0)
        ),
        "provider_attempt_usage": {
            "with_usage": accounting.get("attempts_with_usage", 0),
            "without_usage": accounting.get("attempts_without_usage", 0),
        },
    }, errors


def _validate_protocol(
    cases_meta: dict[str, Any], run_dir: Path, cases_dir: Path
) -> dict[str, Any]:
    protocol_path = run_dir / "protocol.json"
    if not protocol_path.exists():
        raise GradingInputError(f"missing execution protocol: {protocol_path}")
    protocol = _require_dict(_load_json(protocol_path), "execution protocol")
    if protocol.get("execute") is not True:
        raise GradingInputError("execution protocol is a no-provider preflight, not an actual run")
    if protocol.get("grading_files_read") is not False:
        raise GradingInputError("execution protocol does not prove grading files were excluded")
    expected_manifest_hash = cases_meta["manifest_sha256"]
    if protocol.get("cases_manifest_sha256") != expected_manifest_hash:
        raise GradingInputError("execution protocol cases manifest hash does not match --cases")
    if protocol.get("arms") != list(ARM_NAMES):
        raise GradingInputError("execution protocol arm set does not match the lookup protocol")
    return {
        "protocol_sha256": _sha256(protocol_path),
        "cases_manifest_sha256": expected_manifest_hash,
        "cases_dir": str(cases_dir),
        "execute": True,
        "grading_files_read": False,
        "arms": list(ARM_NAMES),
    }


def grade(cases_dir: Path, run_dir: Path, output: Path) -> dict[str, Any]:
    cases_meta, cases = _load_cases(cases_dir)
    protocol_meta = _validate_protocol(cases_meta, run_dir, cases_dir)
    report_path = run_dir / "report.json"
    reported_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    report_errors: list[str] = []
    if report_path.exists():
        raw_report = _load_json(report_path)
        if not isinstance(raw_report, list):
            report_errors.append("execution report must be a list")
        else:
            for index, raw_entry in enumerate(raw_report):
                entry = _require_dict(raw_entry, f"execution report[{index}]")
                case_id = entry.get("case_id")
                arm = entry.get("arm")
                key = (case_id, arm)
                if not isinstance(case_id, str) or not isinstance(arm, str):
                    report_errors.append(f"execution report[{index}] has invalid case/arm")
                    continue
                if key in reported_by_key:
                    report_errors.append(f"duplicate execution report entry for {case_id}/{arm}")
                reported_by_key[key] = entry
    else:
        report_errors.append("missing execution report.json")

    results: list[dict[str, Any]] = []
    errors = [*report_errors]
    for case in cases.values():
        for arm in ARM_NAMES:
            item, item_errors = _grade_arm(
                run_dir=run_dir,
                case=case,
                arm=arm,
                reported=reported_by_key.get((case["case_id"], arm)),
            )
            results.append(item)
            errors.extend(item_errors)
    expected_keys = {(case_id, arm) for case_id in cases for arm in ARM_NAMES}
    extra_keys = sorted(set(reported_by_key) - expected_keys)
    if extra_keys:
        errors.extend(
            f"unexpected execution report entry for {case_id}/{arm}" for case_id, arm in extra_keys
        )

    arms: dict[str, dict[str, Any]] = {}
    for arm in ARM_NAMES:
        rows = [row for row in results if row["arm"] == arm]
        grades = Counter(row["grade"] for row in rows)
        telemetry_rows = [row["telemetry"] for row in rows]
        arms[arm] = {
            "correct": grades["correct"],
            "total": len(rows),
            "unknown": grades["unknown"],
            "incorrect": grades["incorrect"],
            "failed": grades["failed"],
            "missing": grades["missing"],
            "budget_exhausted": sum(row["run_status"] == "budget_exhausted" for row in rows),
            "logical_decisions": sum(row["logical_decisions"] for row in rows),
            "logical_decisions_reported": sum(
                row["logical_decisions_reported"] or 0 for row in rows
            ),
            "read_choices": sum(row["read_choices"] for row in rows),
            "provider_attempts": sum(row["provider_attempts"] for row in rows),
            "provider_attempt_usage": {
                "with_usage": sum(row["provider_attempt_usage"]["with_usage"] for row in rows),
                "without_usage": sum(
                    row["provider_attempt_usage"]["without_usage"] for row in rows
                ),
            },
            "telemetry": {
                "calls_with_telemetry": sum(row["calls_with_telemetry"] for row in telemetry_rows),
                "calls_without_telemetry": sum(
                    row["calls_without_telemetry"] for row in telemetry_rows
                ),
                "nullable_field_counts": {
                    field: sum(row["nullable_field_counts"][field] for row in telemetry_rows)
                    for field in TELEMETRY_FIELDS
                },
                "total_elapsed_seconds": sum(
                    row["total_elapsed_seconds"] for row in telemetry_rows
                ),
                "request_count": sum(row["request_count"] for row in telemetry_rows),
                "retry_count": sum(row["retry_count"] for row in telemetry_rows),
                "usage_reported_requests": sum(
                    row["usage_reported_requests"] for row in telemetry_rows
                ),
                "token_and_cost_totals": {
                    field: (
                        sum(
                            row["token_and_cost_totals"][field]
                            for row in telemetry_rows
                            if row["token_and_cost_totals"][field] is not None
                        )
                        if all(
                            row["token_and_cost_totals"][field] is not None
                            for row in telemetry_rows
                        )
                        else None
                    )
                    for field in TELEMETRY_FIELDS
                },
            },
            "cases": [
                {
                    key: row[key]
                    for key in (
                        "case_id",
                        "grade",
                        "run_status",
                        "logical_decisions",
                        "read_choices",
                        "provider_attempts",
                    )
                }
                for row in rows
            ],
        }

    output.mkdir(parents=True, exist_ok=False)
    result = {
        "status": "graded" if not errors else "partial_with_input_errors",
        "evaluation_scope": "QA development-only factual lookup; no causal or economic claim",
        "provider_calls": 0,
        "simulator_calls": 0,
        "model_calls": 0,
        "cases_manifest_sha256": cases_meta["manifest_sha256"],
        "source_sha256": cases_meta["source_sha256"],
        "protocol": protocol_meta,
        "case_count": len(cases),
        "arm_count": len(ARM_NAMES),
        "arms": arms,
        "errors": errors,
    }
    (output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    (output / "protocol.json").write_text(
        json.dumps(
            {
                "status": result["status"],
                "cases_manifest_sha256": cases_meta["manifest_sha256"],
                "run_protocol_sha256": protocol_meta["protocol_sha256"],
                "grading_files_verified": True,
                "execution_results_only": True,
                "evaluation_scope": result["evaluation_scope"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(grade(args.cases, args.run, args.output), ensure_ascii=False, indent=2))
    except (GradingInputError, OSError, TypeError, ValueError) as error:
        print(f"grade input rejected: {error}", file=sys.stderr)
        raise SystemExit(2) from error
