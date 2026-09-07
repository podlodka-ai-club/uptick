from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from uptick_agent.memory.contracts import (
    MemoryValidationError,
    ObjectiveMetric,
    OperationLink,
    TransitionAssemblyRequest,
)
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _request(**updates: object) -> TransitionAssemblyRequest:
    values = {
        "transition_id": "transition-1",
        "run_id": "run-1",
        "iteration": 2,
        "occurred_at": datetime(2026, 9, 4, 15, tzinfo=timezone(timedelta(hours=5))),
        "trust_classification": "external_untrusted",
        "pre_state": {"operations": {"operation-1": "accepted"}},
        "observation": {"summary": "site healthy", "balance": 10},
        "action": {"kind": "get_metrics"},
        "result": {"ok": True, "summary": "balance improved"},
        "before_objective_metrics": [
            ObjectiveMetric(name="balance", value=10, unit="minor"),
            ObjectiveMetric(name="errors", value=2, unit="count"),
            ObjectiveMetric(name="latency", value=3, unit="seconds"),
        ],
        "after_objective_metrics": [
            ObjectiveMetric(name="errors", value=1, unit="count"),
            ObjectiveMetric(name="balance", value=14, unit="minor"),
            ObjectiveMetric(name="latency", value=3000, unit="milliseconds"),
        ],
        "operation_links": [
            OperationLink(operation_id="operation-2", relation="observed"),
            OperationLink(operation_id="operation-1", relation="initiated"),
            OperationLink(operation_id="operation-2", relation="observed"),
        ],
        "terminal": False,
    }
    values.update(updates)
    return TransitionAssemblyRequest.model_validate(values)


def test_assembler_is_deterministic_and_derives_only_observed_facts() -> None:
    assembler = DefaultExperienceTransitionAssembler()
    request = _request()

    first = assembler.assemble(request)
    second = assembler.assemble(request)

    assert first == second
    assert first.schema_version == "1.1"
    assert first.occurred_at == datetime(2026, 9, 4, 10, tzinfo=UTC)
    assert first.objective_metrics == request.after_objective_metrics
    assert [
        (item.name, item.before, item.after, item.delta) for item in first.objective_deltas
    ] == [
        ("balance", 10, 14, 4),
        ("errors", 2, 1, -1),
    ]
    assert first.operation_links == [
        OperationLink(operation_id="operation-1", relation="initiated"),
        OperationLink(operation_id="operation-2", relation="observed"),
    ]
    assert [item.content_hash for item in first.provenance] == [
        _sha256_json(
            {
                "pre_state": request.pre_state,
                "observation": request.observation,
            }
        ),
        _sha256_json({"action": request.action, "result": request.result}),
    ]


def test_current_assembly_request_requires_time_and_trust_but_reads_legacy_shape() -> None:
    with pytest.raises(ValidationError, match="requires occurred_at"):
        _request(occurred_at=None)
    with pytest.raises(ValidationError, match="requires trust_classification"):
        _request(trust_classification=None)

    legacy = TransitionAssemblyRequest(
        schema_version="1.0",
        transition_id="legacy",
        run_id="run",
        iteration=1,
        terminal=False,
    )
    assert legacy.occurred_at is None
    assert legacy.trust_classification is None


@pytest.mark.parametrize("empty_field", ["observation", "action", "result"])
def test_assembler_rejects_missing_payload_facts(empty_field: str) -> None:
    request = _request(**{empty_field: {}})

    with pytest.raises(MemoryValidationError, match="requires observation, action, and result"):
        DefaultExperienceTransitionAssembler().assemble(request)


def test_assembler_rejects_naive_time_and_duplicate_metric_observations() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        _request(occurred_at=datetime(2026, 9, 4, 10))

    request = _request(
        before_objective_metrics=[
            ObjectiveMetric(name="balance", value=1, unit="minor"),
            ObjectiveMetric(name="balance", value=2, unit="minor"),
        ]
    )
    with pytest.raises(MemoryValidationError, match="duplicate objective metric"):
        DefaultExperienceTransitionAssembler().assemble(request)


def test_assembler_redacts_credentials_before_hashing_and_persistence() -> None:
    transition = DefaultExperienceTransitionAssembler().assemble(
        _request(
            observation={
                "credentials": "topsecret",
                "summary": "token=second site healthy",
            }
        )
    )

    assert transition.observation == {
        "credentials": "<redacted>",
        "summary": "<redacted> site healthy",
    }
    assert transition.provenance[0].content_hash == _sha256_json(
        {
            "pre_state": transition.pre_state,
            "observation": transition.observation,
        }
    )

    with pytest.raises(MemoryValidationError, match="metadata contains credential"):
        DefaultExperienceTransitionAssembler().assemble(_request(transition_id="sk-abcdefghijk"))


def test_transition_assembler_has_no_environment_provider_or_module_imports() -> None:
    source = (Path(__file__).parents[1] / "src/uptick_agent/transition_assembly.py").read_text()
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)

    forbidden = ("uptick_agent.simulator", "uptick_agent.llm", "uptick_agent.memory.episodic")
    assert not any(name.startswith(forbidden) for name in imports)


def test_runner_does_not_import_an_environment_or_episodic_implementation() -> None:
    source = (Path(__file__).parents[1] / "src/uptick_agent/runs/execute.py").read_text()
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)

    forbidden = ("uptick_agent.simulator", "uptick_agent.memory.episodic")
    assert not any(name.startswith(forbidden) for name in imports)
