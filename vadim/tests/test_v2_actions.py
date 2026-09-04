from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from uptick_agent import cli
from uptick_agent.experiments import ExperimentRunner
from uptick_agent.llm.prompts import V2_SYSTEM_PROMPT
from uptick_agent.models import (
    DecisionContext,
    GetOperation,
    NextStep,
    RecentStep,
    RunResult,
    RunState,
    StepRecord,
    ToolResult,
    V1NextStep,
    V2NextStep,
)
from uptick_agent.runner import _record_run_state
from uptick_agent.v2_actions import (
    ControlCommand,
    GetControlCommands,
    GetInbox,
    SiteStopRequest,
)


def test_v2_control_command_is_discriminated_and_keeps_secrets_out_of_schema() -> None:
    action = ControlCommand(
        request={
            "command": "site.stop",
            "params": {},
        }
    )

    assert action.request.command == "site.stop"
    assert action.request.params.model_dump(mode="json", exclude_none=True) == {}
    schema = str(ControlCommand.model_json_schema())
    assert "request_id" not in schema
    assert "target_auth" not in schema
    assert "password" not in schema
    assert "username" not in schema


def test_v2_normalized_schema_avoids_unsupported_property_count_keywords() -> None:
    schema = str(V2NextStep.model_json_schema())
    assert "minProperties" not in schema
    assert "maxProperties" not in schema

    with pytest.raises(ValidationError):
        ControlCommand(request={"command": "site.stop", "params": {"extra": True}})
    with pytest.raises(ValidationError):
        ControlCommand(
            request={
                "command": "firewall.rules.upsert",
                "params": {
                    "rule_id": "deny-empty",
                    "priority": 10,
                    "action": "deny",
                    "match": {},
                    "enabled": True,
                },
            }
        )


def test_cli_defaults_to_v2_and_structured_model_uses_exact_v2_schema_and_prompt() -> None:
    args = cli._parser().parse_args(["run", "--seed", "1"])
    assert args.simulator_api_version == "v2"
    legacy_args = cli._parser().parse_args(["run", "--seed", "1", "--simulator-api-version", "v1"])
    assert legacy_args.simulator_api_version == "v1"

    class Client:
        model = "test-model"

    model = cli.StructuredDecisionModel(
        Client(),
        response_model=V2NextStep,
        system_prompt=V2_SYSTEM_PROMPT,
    )
    trace = model.prompt_trace(
        DecisionContext(
            objective="uptime",
            run_id="run-1",
            seed=1,
            iteration=1,
            max_steps=2,
            latest_result=ToolResult(action_kind="start", summary="started"),
        )
    )
    assert trace["response_model"]["qualname"] == V2NextStep.__qualname__
    assert trace["messages"][0]["content"] == V2_SYSTEM_PROMPT
    assert "does not stop simulator billing" in V2_SYSTEM_PROMPT
    assert "Cover the full simulation horizon" in V2_SYSTEM_PROMPT
    assert "progress is genuinely impossible" not in V2_SYSTEM_PROMPT


def test_v2_commands_have_typed_params_and_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ControlCommand(
            request={
                "command": "server.create",
                "params": {
                    "name": "backend",
                    "role": "backend",
                    "instance_type": "backend.standard",
                    "desired_instances": 3,
                },
            }
        )

    with pytest.raises(ValidationError):
        ControlCommand(
            request={
                "command": "database.inspect",
                "params": {"database_id": "db-main"},
                "target_auth": {"username": "ops", "password": "secret"},
            }
        )


def test_firewall_match_requires_at_least_one_condition() -> None:
    with pytest.raises(ValidationError):
        ControlCommand(
            request={
                "command": "firewall.rules.upsert",
                "params": {
                    "rule_id": "deny-empty",
                    "priority": 10,
                    "action": "deny",
                    "match": {},
                    "enabled": True,
                },
            }
        )


def test_v2_read_action_statuses_and_readonly_probe() -> None:
    step = V2NextStep.model_validate(
        {
            "current_situation": "inspect",
            "hypothesis": "the service may be degraded",
            "remaining_steps": [],
            "task_completed": False,
            "action": {"kind": "get_logs", "status": 403},
        }
    )
    assert step.action.status == 403

    with pytest.raises(ValidationError):
        V2NextStep.model_validate(
            {
                "current_situation": "inspect",
                "hypothesis": "purchase is unavailable",
                "remaining_steps": [],
                "task_completed": False,
                "action": {"kind": "probe_page", "page": "purchase"},
            }
        )

    with pytest.raises(ValidationError):
        V1NextStep.model_validate(
            {
                "current_situation": "inspect",
                "hypothesis": "the service may be degraded",
                "remaining_steps": [],
                "task_completed": False,
                "action": {"kind": "get_logs", "status": 403},
            }
        )


def test_generic_trace_actions_include_v2_inbox_and_control_actions() -> None:
    assert GetInbox().kind == "get_inbox"
    assert GetControlCommands().kind == "get_control_commands"
    assert SiteStopRequest(command="site.stop", params={}).params.model_dump() == {}


def test_v2_advance_time_preserves_stop_condition_and_explicit_null() -> None:
    with_stop = V2NextStep.model_validate(
        {
            "current_situation": "wait",
            "hypothesis": "the service is stable",
            "remaining_steps": [],
            "task_completed": False,
            "action": {
                "kind": "advance_time_v2",
                "duration_seconds": 600,
                "stop_when": {
                    "new_log_errors": 1,
                    "error_codes": ["SERVER_CAPACITY_EXCEEDED"],
                },
            },
        }
    )
    assert with_stop.model_dump(mode="json")["action"]["stop_when"] == {
        "new_log_errors": 1,
        "error_codes": ["SERVER_CAPACITY_EXCEEDED"],
    }

    without_stop = V2NextStep.model_validate(
        {
            "current_situation": "wait",
            "hypothesis": "a full interval is safe",
            "remaining_steps": [],
            "task_completed": False,
            "action": {"kind": "advance_time_v2", "stop_when": None},
        }
    )
    assert without_stop.action.stop_when is None

    decision = NextStep.model_validate(with_stop.model_dump(mode="json"))
    record = StepRecord(
        run_id="run-1",
        decision_id="decision-1",
        transition_id="transition-1",
        iteration=1,
        decision=decision,
        result=ToolResult(action_kind="advance_time_v2", summary="advanced"),
        started_at=datetime.now(UTC),
        duration_seconds=0,
    )
    restored = StepRecord.model_validate(record.model_dump(mode="json"))
    recent = RecentStep(
        iteration=1,
        action=restored.decision.action,
        result_action_kind="advance_time_v2",
        result_ok=True,
        result_summary="advanced",
        result_terminal=False,
    )
    assert restored.decision.action.stop_when.error_codes == ["SERVER_CAPACITY_EXCEEDED"]
    assert recent.action.kind == "advance_time_v2"


def test_operation_links_update_generic_state_even_for_failed_observations() -> None:
    state = RunState()
    _record_run_state(
        state,
        GetOperation(operation_id="operation-1"),
        ToolResult(
            action_kind="get_operation",
            ok=False,
            summary="operation failed",
            data={"status": "failed"},
            operation_links=[
                {"operation_id": "operation-1", "relation": "observed"},
            ],
        ),
    )
    assert state.operation_statuses == {"operation-1": "failed"}


def test_v2_experiment_cost_average_ignores_non_slo_runs() -> None:
    class Memory:
        async def clear(self):
            return None

    class Runner:
        def __init__(self, result):
            self.result = result
            self.memory = Memory()

        async def run(self, seed):
            return self.result.model_copy(update={"seed": seed})

    results = [
        RunResult(
            run_id="run-1",
            seed=1,
            agent_id="agent",
            agent_version="v2",
            status="completed",
            steps=1,
            duration_seconds=1,
            objective_kind="uptime_cost",
            uptime_ratio=0.999,
            slo_passed=True,
            total_cost_minor=20,
            stop_reason="done",
        ),
        RunResult(
            run_id="run-2",
            seed=2,
            agent_id="agent",
            agent_version="v2",
            status="completed",
            steps=1,
            duration_seconds=1,
            objective_kind="uptime_cost",
            uptime_ratio=0.98,
            slo_passed=False,
            total_cost_minor=1,
            stop_reason="done",
        ),
        RunResult(
            run_id="run-3",
            seed=3,
            agent_id="agent",
            agent_version="v2",
            status="running",
            steps=1,
            duration_seconds=1,
            objective_kind="uptime_cost",
            uptime_ratio=None,
            slo_passed=None,
            total_cost_minor=0,
            stop_reason="step limit",
        ),
    ]

    async def scenario() -> None:
        queue = iter(results)
        experiment = await ExperimentRunner(lambda: Runner(next(queue))).run(
            name="v2",
            seeds=[1, 2, 3],
        )
        assert experiment.objective_kind == "uptime_cost"
        assert experiment.completed_runs == 2
        assert experiment.slo_passed_runs == 1
        assert experiment.mean_successful_total_cost_minor == 20
        assert experiment.mean_balance_minor is None

    import asyncio

    asyncio.run(scenario())
