import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from uptick_agent import cli
from uptick_agent.decisions.instructions import CORE_SYSTEM_PROMPT, compose_system_prompt
from uptick_agent.decisions.runtime import RuntimeDecisionContext, ToolResult
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.memory import legacy_memory_runtime
from uptick_agent.memory.contracts import OperationLink
from uptick_agent.runs.config import AgentConfig
from uptick_agent.simulator.actions import FinishRun, GetMetrics, GetOverview, V2AdvanceTime
from uptick_agent.simulator.decisions import SimulatorV2BatchDecision, SimulatorV2Decision
from uptick_agent.simulator.v2_environment import SimulatorV2Environment
from uptick_agent.simulator.v2_policy import SimulatorV2TimeBudgetPolicy


def _decision(actions, *, completed=False):
    return SimulatorV2BatchDecision(
        current_situation="Use already observed inputs.",
        hypothesis="The reads supply fresh state.",
        remaining_steps=[],
        task_completed=completed,
        actions=actions,
    )


def _context():
    return RuntimeDecisionContext(
        objective="Complete the public objective",
        run_id="test-run",
        seed=42,
        iteration=1,
        max_steps=5,
        latest_result=ToolResult(
            action_kind="get_overview",
            summary="Observed",
            data={"clock": {"remaining_seconds": 3600}},
        ),
    )


@pytest.mark.parametrize(
    "actions,completed",
    [
        ([], False),
        ([GetOverview()] * 5, False),
        ([GetMetrics(), V2AdvanceTime(duration_seconds=300)], False),
        ([V2AdvanceTime(duration_seconds=300), GetMetrics()], False),
        ([GetMetrics(), FinishRun(reason="complete")], True),
        ([FinishRun(reason="complete")], False),
        ([GetMetrics()], True),
    ],
)
def test_batch_schema_rejects_ambiguous_or_stale_time_execution(actions, completed):
    with pytest.raises(ValidationError):
        _decision(actions, completed=completed)


def test_singleton_batch_advance_keeps_the_existing_stop_and_duration_policy():
    async def scenario():
        action = V2AdvanceTime(duration_seconds=300, stop_when=None)
        batch = _decision([action])
        single = SimulatorV2Decision(
            **{key: value for key, value in batch.model_dump().items() if key != "actions"},
            action=action,
        )

        class Delegate:
            def __init__(self, value):
                self.value = value

            async def decide(self, context):
                return self.value

        batch_result = await SimulatorV2TimeBudgetPolicy(Delegate(batch)).decide(_context())
        single_result = await SimulatorV2TimeBudgetPolicy(Delegate(single)).decide(_context())
        assert batch_result.actions == [single_result.action]
        assert batch_result.actions[0].stop_when is not None
        assert batch_result.actions[0].duration_seconds == 1800
        assert batch_result.current_situation == single_result.current_situation
        # A read batch crosses no time-policy boundary and is preserved.
        reads = _decision([GetOverview(), GetMetrics()])
        assert await SimulatorV2TimeBudgetPolicy(Delegate(reads)).decide(_context()) == reads

    asyncio.run(scenario())


def test_batch_startup_and_prompt_are_opt_in_and_fingerprinted():
    class Client:
        async def start(self, **kwargs):
            return {
                "run_id": "test-run",
                "status": "running",
                "simulation_time": "2033-03-01T00:00:00Z",
                "simulation_ends_at": "2033-03-02T00:00:00Z",
                "commands_markdown": "Public commands supplied at startup.",
            }

    async def scenario():
        for batch in (False, True):
            environment = SimulatorV2Environment(Client(), action_batch=batch)
            await environment.start(seed=42, agent_id="test", agent_version="test")
            spec = environment.decision_spec
            assert spec.response_model is (
                SimulatorV2BatchDecision if batch else SimulatorV2Decision
            )
            model = StructuredDecisionModel(
                SimpleNamespace(model="fake"),
                response_model=spec.response_model,
                environment_briefing=spec.environment_briefing,
            )
            assert hashlib.sha256(
                model.system_prompt.encode()
            ).hexdigest() == cli._prompt_fingerprint(
                spec.environment_briefing,
                action_batch=batch,
            )
            if batch:
                assert "exactly one declared typed action" not in model.system_prompt
                assert "one to four declared typed actions" in model.system_prompt
                assert "unused tail is discarded" in model.system_prompt
            else:
                assert model.system_prompt == compose_system_prompt(
                    CORE_SYSTEM_PROMPT, spec.environment_briefing
                )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "options",
    [
        ["--action-batch"],
        ["--action-batch", "--max-actions", "0"],
        ["--action-batch", "--max-actions", "4", "--simulator-api-version", "v1"],
        ["--max-actions", "4"],
    ],
)
def test_cli_rejects_unbudgeted_or_unsupported_batches_before_start(options):
    args = cli._parser().parse_args(["run", "--seed", "42", *options])
    with pytest.raises(ValueError):
        asyncio.run(cli._main(args))


def test_batch_barrier_distinguishes_running_run_from_unresolved_operation():
    environment = SimulatorV2Environment(object(), action_batch=True)
    session = SimpleNamespace(operation_statuses={})
    overview = ToolResult(
        action_kind="get_overview", summary="Observed", data={"status": "running"}
    )
    assert environment.can_continue_batch(session, overview)
    pending = ToolResult(
        action_kind="control_command",
        summary="Accepted",
        data={"operation_id": "op-1", "status": "accepted"},
        operation_links=[OperationLink(operation_id="op-1", relation="initiated")],
    )
    assert not environment.can_continue_batch(session, pending)
    # Absence of a status is not proof that an accepted operation completed.
    assert not environment.can_continue_batch(
        session, pending.model_copy(update={"data": {"operation_id": "op-1"}})
    )
    session.operation_statuses["op-1"] = "succeeded"
    completed = pending.model_copy(update={"data": {"operation_id": "op-1", "status": "succeeded"}})
    assert environment.can_continue_batch(session, completed)
    for field in ({"ok": False}, {"terminal": True}):
        assert not environment.can_continue_batch(session, completed.model_copy(update=field))


def test_cli_executes_two_actions_from_one_request_and_preserves_each_result(
    monkeypatch,
    tmp_path,
    capsys,
):
    from test_simulator_v2_environment import FakeV2Client

    class Client(FakeV2Client):
        async def aclose(self):
            pass

    class Provider:
        model = "fake"

        def __init__(self):
            self.requests = []

        async def generate_structured(self, request):
            self.requests.append(request)
            return SimpleNamespace(value=_decision([GetOverview(), GetMetrics()]))

        async def aclose(self):
            pass

    client, provider = Client(), Provider()
    monkeypatch.setattr(cli, "SimulatorV2Client", lambda _url, *, participant_token=None: client)
    monkeypatch.setattr(
        cli,
        "_decision_model",
        lambda args, spec: SimulatorV2TimeBudgetPolicy(
            StructuredDecisionModel(
                provider,
                response_model=spec.response_model,
                environment_briefing=spec.environment_briefing,
            )
        ),
    )
    args = cli._parser().parse_args(
        [
            "run",
            "--seed",
            "42",
            "--action-batch",
            "--max-actions",
            "2",
            "--max-steps",
            "3",
            "--memory",
            "none",
            "--artifacts",
            str(tmp_path),
        ]
    )
    result = asyncio.run(
        cli._run_seed(
            args,
            AgentConfig(max_steps=3, max_actions=2),
            legacy_memory_runtime(None),
            42,
        )
    )
    assert result.steps == 1
    assert result.action_count == 2
    assert result.stop_reason == "action budget exhausted"
    assert len(provider.requests) == 1
    rows = [
        json.loads(line) for line in (tmp_path / "seed-42/trace.jsonl").read_text().splitlines()
    ]
    steps = [row["data"] for row in rows if row["event"] == "step"]
    assert [row["action_index"] for row in steps] == [0, 1]
    assert [row["action"]["kind"] for row in steps] == ["get_overview", "get_metrics"]
    assert len({row["transition_id"] for row in steps}) == 2
    assert len({row["decision_id"] for row in steps}) == 1
    output = capsys.readouterr().out
    assert "action_index=0 action=get_overview" in output
    assert "action_index=1 action=get_metrics" in output
