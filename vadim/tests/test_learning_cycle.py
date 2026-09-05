"""Controlled learning-cycle contract and integration tests using doubles."""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import uptick_agent.composition.learning_cycle as learning_cycle_module
from uptick_agent.benchmarks.incidents import (
    DEFAULT_REPAIR_MAPPING,
    TRAINING_SEEDS,
    ControlledIncidentEnvironment,
    evaluation_case_for_seed,
    fixture_spec,
    training_case_for_seed,
)
from uptick_agent.composition.learning_cycle import (
    hypothesis_configuration,
    no_memory_configuration,
    run_learning_cycle,
)
from uptick_agent.decisions.actions import ApplyFix
from uptick_agent.decisions.contracts import DecisionContext, NextStep
from uptick_agent.evaluation.learning_cycle import (
    LearningCycleJournal,
    LearningCycleManifest,
    content_hash,
)
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.stage0 import sha256_file


def _manifest() -> LearningCycleManifest:
    configs = {
        "none": no_memory_configuration(),
        "hypothesis": hypothesis_configuration(),
    }
    return LearningCycleManifest(
        experiment_id="test-controlled-learning",
        provider="double",
        model="double",
        generation_settings={
            "temperature": None,
            "max_output_tokens": None,
            "reasoning_effort": "low",
        },
        prompt="Choose a typed public remediation action.",
        source_revision="a" * 40,
        source_tree_hash="b" * 64,
        dependency_lock_hash="c" * 64,
        source_capsule_hash="d" * 64,
        source_dirty=False,
        fixture_spec_hash=content_hash(fixture_spec()),
        adapter_hash=sha256_file(
            Path(__file__).parents[1] / "src/uptick_agent/benchmarks/incidents.py"
        ),
        training_seeds=TRAINING_SEEDS,
        training_cases=tuple(
            {
                "seed": seed,
                "incident_code": training_case_for_seed(seed).incident_code,
                "variant": training_case_for_seed(seed).variant,
            }
            for seed in TRAINING_SEEDS
        ),
        evaluation_cases=tuple(
            {
                "seed": seed,
                "incident_code": evaluation_case_for_seed(seed).incident_code,
                "variant": evaluation_case_for_seed(seed).variant,
            }
            for seed in range(101, 109)
        ),
        conditions=("none", "hypothesis"),
        memory_configurations={
            key: {**config.model_dump(mode="json"), "fingerprint": config.fingerprint}
            for key, config in configs.items()
        },
        training_max_steps=3,
        evaluation_max_steps=1,
        training_timeout_seconds=120.0,
        evaluation_timeout_seconds=60.0,
    ).seal()


class _CaptureClient:
    model = "double"

    async def aclose(self) -> None:
        return None


class _CycleModel:
    def __init__(self, phase: str, condition_id: str) -> None:
        self.phase = phase
        self.condition_id = condition_id

    async def decide(self, context: DecisionContext) -> NextStep:
        code = context.latest_result.data["incident_code"]
        available = context.latest_result.data["available_repairs"]
        message = available[0]
        if self.condition_id == "hypothesis":
            for item in context.memory_context.items:
                hypothesis = item.envelope.item.get("hypothesis")
                if (
                    isinstance(hypothesis, dict)
                    and hypothesis.get("result_value") is True
                    and hypothesis.get("scope", {}).get("observation.data.incident_code") == code
                ):
                    message = hypothesis["action_kind"]
                    break
        if self.phase == "training" and context.recent_steps:
            last = context.recent_steps[-1]
            if not last.result_ok:
                message = next(repair for repair in available if repair != last.action.message)
        return NextStep(
            current_situation="public incident evidence",
            hypothesis="typed repair may recover it",
            remaining_steps=[],
            task_completed=False,
            action=ApplyFix(message=message),
        )

    def prompt_trace(self, context: DecisionContext) -> dict[str, object]:
        return {"context": context.model_dump(mode="json")}

    async def aclose(self) -> None:
        return None


class _CloseErrorModel(_CycleModel):
    async def aclose(self) -> None:
        raise RuntimeError("model cleanup unavailable")


class _CancelledCloseModel(_CycleModel):
    async def aclose(self) -> None:
        raise asyncio.CancelledError()


class _BlockingModel:
    model = "double"

    async def decide(self, _context: DecisionContext) -> NextStep:
        await asyncio.sleep(10)
        raise AssertionError("the blocking decision should be cancelled")

    def prompt_trace(self, context: DecisionContext) -> dict[str, object]:
        return {"context": context.model_dump(mode="json")}

    async def aclose(self) -> None:
        return None


def test_swapping_hidden_mapping_leaves_public_first_request_identical() -> None:
    async def run(mapping):
        environment = ControlledIncidentEnvironment(training_case_for_seed(11), mapping)
        _session, latest = await environment.start(seed=11, agent_id="test", agent_version="1")
        context = DecisionContext(
            objective="Recover the incident through public evidence.",
            run_id="same-run",
            seed=11,
            iteration=1,
            max_steps=3,
            latest_result=latest,
        )
        trace = StructuredDecisionModel(
            _CaptureClient(),
            system_prompt="Choose one typed repair from public evidence.",
        ).prompt_trace(context)
        return latest.model_dump(mode="json"), trace

    swapped = {
        "q7m": "ivory",
        "k2p": "ivory",
        "r4x": "lumen",
        "v9n": "lumen",
    }
    public_one, request_one = asyncio.run(run(DEFAULT_REPAIR_MAPPING))
    public_two, request_two = asyncio.run(run(swapped))
    assert public_one == public_two
    assert request_one == request_two
    rendered = json.dumps(request_one, sort_keys=True)
    assert "mapping_digest" not in rendered
    assert '"q7m": "lumen"' not in rendered


def test_learning_contract_import_has_no_composition_or_provider_side_effect() -> None:
    code = (
        "import json, sys; import uptick_agent.evaluation.learning_cycle; "
        "print(json.dumps(sorted(name for name in sys.modules if "
        "name.startswith('uptick_agent.composition') or name.startswith('uptick_agent.llm'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == []


def test_wrong_repair_keeps_incident_active_until_a_later_action() -> None:
    async def run():
        environment = ControlledIncidentEnvironment(
            training_case_for_seed(13), DEFAULT_REPAIR_MAPPING
        )
        session, _latest = await environment.start(seed=13, agent_id="test", agent_version="1")
        wrong = await environment.execute(session, ApplyFix(message="lumen"))
        right = await environment.execute(session, ApplyFix(message="ivory"))
        return wrong, right

    wrong, right = asyncio.run(run())
    assert wrong.data["recovered"] is False
    assert wrong.terminal is False
    assert right.data["recovered"] is True
    assert right.terminal is True
    assert wrong.objective_metrics[0].name == "incident_recovered"
    assert wrong.objective_metrics[0].unit == "boolean"


def test_learning_cycle_uses_observed_hypotheses_and_reopened_isolated_eval(tmp_path: Path) -> None:
    def model_factory(phase: str, condition_id: str, _seed: int):
        return _CycleModel(phase, condition_id)

    report = asyncio.run(
        run_learning_cycle(
            _manifest(),
            output=tmp_path / "cycle",
            sqlite_path=tmp_path / "cycle.sqlite",
            model_factory=model_factory,
        )
    )
    assert report.expected_attempts == 24
    assert len(report.attempts) == 24
    assert report.completed == 20
    assert report.failed_or_interrupted == 4
    assert report.reopened_before_evaluation is True
    assert set(report.frozen_bindings) == {"hypothesis"}
    assert report.report_hash

    training = [row for row in report.attempts if row.phase == "training"]
    hypothesis_eval = [
        row
        for row in report.attempts
        if row.phase == "evaluation" and row.condition_id == "hypothesis"
    ]
    baseline_eval = [
        row for row in report.attempts if row.phase == "evaluation" and row.condition_id == "none"
    ]
    assert len(training) == 8
    assert all(row.status == "completed" for row in training)
    assert all(row.status == "completed" and row.recovered for row in hypothesis_eval)
    assert sum(row.status == "failed" for row in baseline_eval) == 4
    assert all(row.memory_item_ids for row in hypothesis_eval)
    assert all(not row.memory_item_ids for row in baseline_eval)
    assert all(
        any(
            isinstance(item.get("context"), dict)
            and item["context"].get("memory_context", {}).get("items")
            for item in row.prompt_records
        )
        for row in hypothesis_eval
    )

    binding = report.frozen_bindings["hypothesis"]
    namespaces = {item["namespace"] for item in binding["snapshot_refs"]}
    assert all(":evaluation:" not in namespace for namespace in namespaces)
    assert (tmp_path / "cycle" / "frozen-binding-hypothesis.json").exists()
    assert (tmp_path / "cycle" / "raw-requests.jsonl").exists()
    lines = (tmp_path / "cycle" / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 48  # started and final row for every retained attempt


def test_tampered_memory_body_is_rejected_before_model_factory(tmp_path: Path) -> None:
    manifest = _manifest()
    memory_configurations = copy.deepcopy(manifest.memory_configurations)
    memory_configurations["hypothesis"]["context_budget"]["total_tokens"] = 1
    manifest = replace(manifest, memory_configurations=memory_configurations).seal()
    calls: list[tuple[object, ...]] = []

    def model_factory(*args):
        calls.append(args)
        raise AssertionError("tampered configuration must fail before model startup")

    with pytest.raises(ValueError, match="memory configuration changed"):
        asyncio.run(
            run_learning_cycle(
                manifest,
                output=tmp_path / "cycle",
                sqlite_path=tmp_path / "cycle.sqlite",
                model_factory=model_factory,
            )
        )
    assert calls == []


def test_timeout_retains_started_run_id_and_inflight_request(tmp_path: Path) -> None:
    manifest = _manifest()
    output = tmp_path / "timeout"
    journal = LearningCycleJournal(output, manifest)
    store = learning_cycle_module.SqliteStructuredStore(tmp_path / "timeout.sqlite")
    memory = learning_cycle_module.compose_experimental_runtime(
        no_memory_configuration(),
        store,
        namespace="timeout",
        condition_id="none",
    )

    row = asyncio.run(
        learning_cycle_module._run_attempt(
            journal=journal,
            manifest=manifest,
            phase="evaluation",
            condition_id="none",
            seed=101,
            variant="evaluation-1",
            case=evaluation_case_for_seed(101),
            mapping=DEFAULT_REPAIR_MAPPING,
            run_id_suffix="none",
            memory=memory,
            model_factory=lambda *_args: _BlockingModel(),
            timeout_seconds=0.5,
        )
    )

    assert row.status == "interrupted"
    assert row.run_id == "controlled:evaluation-1:q7m:101:none"
    assert len(row.prompt_records) == 1
    assert row.prompt_records[0]["status"] == "requested"
    durable_request = json.loads((output / "raw-requests.jsonl").read_text().splitlines()[0])
    assert durable_request["status"] == "requested"


def test_cleanup_error_is_retained_without_stopping_later_attempts(tmp_path: Path) -> None:
    def model_factory(phase: str, condition_id: str, _seed: int):
        return _CloseErrorModel(phase, condition_id)

    report = asyncio.run(
        run_learning_cycle(
            _manifest(),
            output=tmp_path / "cycle",
            sqlite_path=tmp_path / "cycle.sqlite",
            model_factory=model_factory,
        )
    )

    assert len(report.attempts) == 24
    assert report.completed == 20
    assert all(row.cleanup_errors for row in report.attempts)
    assert all("model cleanup unavailable" in row.cleanup_errors[0] for row in report.attempts)
    assert report.attempts[-1].status in {"completed", "failed"}


def test_cleanup_cancellation_finalizes_current_attempt_and_stops_cycle(tmp_path: Path) -> None:
    calls: list[tuple[str, str, int]] = []

    def model_factory(phase: str, condition_id: str, seed: int):
        calls.append((phase, condition_id, seed))
        return _CancelledCloseModel(phase, condition_id)

    output = tmp_path / "cycle"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_learning_cycle(
                _manifest(),
                output=output,
                sqlite_path=tmp_path / "cycle.sqlite",
                model_factory=model_factory,
            )
        )

    assert calls == [("training", "hypothesis", 11)]
    journal_rows = [
        json.loads(line)
        for line in (output / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(journal_rows) == 2
    assert journal_rows[0]["status"] == "started"
    final = journal_rows[1]
    assert final["status"] == "interrupted"
    assert final["run_id"] == "controlled:training-1:q7m:11"
    assert final["failure"] == "attempt cancelled during cleanup"
    assert final["cleanup_errors"] == ["cleanup cancelled: CancelledError: "]


def test_freeze_failure_retains_all_evaluation_denominator_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_freeze(*_args, **_kwargs):
        raise RuntimeError("snapshot unavailable")

    monkeypatch.setattr(learning_cycle_module, "_freeze", fail_freeze)

    def model_factory(*_args):
        raise AssertionError("evaluation models must not start after freeze failure")

    report = asyncio.run(
        run_learning_cycle(
            _manifest(),
            output=tmp_path / "cycle",
            sqlite_path=tmp_path / "cycle.sqlite",
            model_factory=model_factory,
        )
    )

    assert report.reopened_before_evaluation is False
    assert len(report.attempts) == report.expected_attempts == 24
    assert sum(row.phase == "training" for row in report.attempts) == 8
    evaluation = [row for row in report.attempts if row.phase == "evaluation"]
    assert len(evaluation) == 16
    assert all(row.status == "failed" for row in evaluation)
    assert all("snapshot unavailable" in (row.failure or "") for row in evaluation)
