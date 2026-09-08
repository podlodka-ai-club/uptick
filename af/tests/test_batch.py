import asyncio
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from tests.helpers import decode_prompt_context
from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.errors import RunExecutionError
from uptick_agent.core.models import (
    AgentConstraints,
    Capability,
    CapabilityCall,
    CapabilityCatalog,
    EnvironmentProfileRef,
    Observation,
    RunResult,
    RunSpec,
)
from uptick_agent.core.policy import DecisionPolicy
from uptick_agent.core.sgr import CurrentSGR, normalized_output_schema
from uptick_agent.core.trace_models import DecisionTracePayload, EpisodeClosedPayload, TraceEvent
from uptick_agent.environments.batch import BATCH_STATE_KEY, BatchExecutionError
from uptick_agent.environments.programmatic import ProgrammableEnvironment
from uptick_agent.environments.scripted import ScriptedEnvironment
from uptick_agent.memory import NoMemory
from uptick_agent.reasoners.scripted import ScriptedReasoner
from uptick_agent.runtime.bootstrap import ScriptedEnvironmentBootstrapper
from uptick_agent.runtime.runner import (
    AgentRunner,
    _episode_evidence_step,
    _episode_outcome_summary,
)
from uptick_agent.store import InMemoryRunStore


def _catalog():
    empty = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    nested = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "pattern": "^[a-z0-9-]+$"},
            "options": {
                "type": "object",
                "properties": {
                    "values": {"type": "array", "maxItems": 2, "items": {"type": "integer"}}
                },
                "required": ["values"],
                "additionalProperties": False,
            },
        },
        "required": ["key", "options"],
        "additionalProperties": False,
    }
    return CapabilityCatalog(
        items=[
            Capability(
                name="launch",
                description="Launch work; completion is asynchronous.",
                input_schema=nested,
                mutates_state=True,
            ),
            Capability(name="inspect", description="Read work results.", input_schema=nested),
            Capability(
                name="wait",
                description="Advance to completion.",
                input_schema=empty,
                mutates_state=True,
            ),
            Capability(
                name="finish",
                description="Finish after verification.",
                input_schema=empty,
                terminal=True,
            ),
            Capability(name="shell", description="Forbidden generic tool.", input_schema=empty),
        ]
    )


class RecordingEnvironment(ScriptedEnvironment):
    def __init__(
        self,
        *,
        fail_at=None,
        reduce_fail_at=None,
        error_at=None,
        terminal_at=None,
        unavailable_after=None,
    ):
        super().__init__(
            name="batch-test",
            catalog=_catalog(),
            observations=[],
            result=RunResult(
                run_id="run-batch",
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="done",
            ),
        )
        self.fail_at = fail_at
        self.reduce_fail_at = reduce_fail_at
        self.error_at = error_at
        self.terminal_at = terminal_at
        self.unavailable_after = unavailable_after
        self.reductions = []
        self.responses = []

    async def capabilities(self, state):
        assert BATCH_STATE_KEY not in state.decision_view
        catalog = await super().capabilities(state)
        if self.unavailable_after is not None and len(self.calls) >= self.unavailable_after:
            catalog.items = [x for x in catalog.items if x.name != "launch"]
        return catalog

    async def execute(self, call, state):
        assert BATCH_STATE_KEY not in state.decision_view
        assert state.decision_view.get("reduced", 0) == len(self.reductions)
        assert self._catalog.find(call.name) is not None
        self.calls.append(call.model_copy(deep=True))
        if len(self.calls) == self.fail_at:
            raise TimeoutError("do not expose transport internals")
        response = Observation(
            action_kind=call.name,
            ok=len(self.calls) != self.error_at,
            terminal=len(self.calls) == self.terminal_at or call.name == "finish",
            summary=f"{call.name}: response {len(self.calls)}",
            data={
                "key": call.arguments.get("key"),
                "values": list(range(100)),
                "text": "x" * 2200,
                "nested": {"one": {"two": {"three": list(range(60))}}},
                "state": "accepted" if call.name == "launch" else "observed",
            },
        )
        self.responses.append(response.model_copy(deep=True))
        return response

    def reduce(self, state, call, observation):
        assert BATCH_STATE_KEY not in state.decision_view
        assert self._catalog.find(call.name) is not None
        if len(self.calls) == self.reduce_fail_at:
            raise ValueError("reducer failed")
        self.reductions.append(call.name)
        # Deliberately replace the view, as discovered sessions do.
        return (
            super()
            .reduce(state, call, observation)
            .model_copy(update={"decision_view": {"reduced": len(self.reductions)}})
        )

    def telemetry(self, run_id):
        return super().telemetry(run_id).model_copy(update={"external_calls": len(self.calls)})


def _child(name="launch", key="job-1"):
    return CapabilityCall(name=name, arguments={"key": key, "options": {"values": [1, 2]}})


def _batch(items=None, *, release=None, retain=True):
    return CapabilityCall(
        name="execute_batch",
        arguments={
            "items": [
                x.model_dump(mode="json") for x in (items if items is not None else [_child()])
            ],
            "release": release or [],
            "retain_results": retain,
        },
    )


def _spec(forbidden=None):
    return RunSpec(
        run_id="run-batch",
        environment="batch-test",
        environment_profile=EnvironmentProfileRef(environment_id="batch-test", version="test"),
        constraints=AgentConstraints(forbidden_capabilities=forbidden or []),
    )


async def _step(env, state, call):
    observation = await env.execute(call, state)
    return env.reduce(state, call, observation), observation


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "terminal",
        "program",
        "batch",
        "generic",
        "forbidden",
        "pattern",
        "array",
        "too_many",
        "empty",
        "readonly",
        "bad_release",
        "duplicate_release",
        "outer_forbidden",
    ],
)
def test_preflight_rejects_entire_group_without_dispatch_or_release(case):
    async def scenario():
        base = RecordingEnvironment()
        env = ProgrammableEnvironment(base)
        forbidden = ["launch"] if case == "forbidden" else []
        state = await env.start(_spec(forbidden))
        state, first = await _step(env, state, _batch([_child("inspect")]))
        batch_id = first.data["batch_id"]
        items = [_child("inspect"), _child()]
        if case in {"unknown", "terminal", "program", "batch", "generic"}:
            items[1] = CapabilityCall(
                name={
                    "unknown": "missing",
                    "terminal": "finish",
                    "program": "execute_program",
                    "batch": "execute_batch",
                    "generic": "shell",
                }[case]
            )
        if case == "pattern":
            items[1].arguments["key"] = "INVALID!"
        if case == "array":
            items[1].arguments["options"] = {"values": [1, 2, 3]}
        if case == "too_many":
            items *= 9
        if case == "empty":
            items = []
        release = [batch_id]
        if case == "bad_release":
            release.append("batch-unknown")
        if case == "duplicate_release":
            release.append(batch_id)
        if case == "outer_forbidden":
            env._constraints.forbidden_capabilities.append("execute_batch")
        before = state.decision_view[BATCH_STATE_KEY]
        state, observation = await _step(
            env, state, _batch(items, release=release, retain=case != "readonly")
        )
        assert not observation.ok
        assert observation.data["code"] == "INVALID_BATCH"
        assert len(base.calls) == 1
        assert state.decision_view[BATCH_STATE_KEY] == before
        assert all(x["status"] == "not_executed" for x in observation.data["items"])

    asyncio.run(scenario())


def test_schema_preserves_nested_arguments_and_excludes_composites_and_terminal():
    async def scenario():
        env = ProgrammableEnvironment(RecordingEnvironment())
        capability = (await env.bootstrap_capabilities()).find("execute_batch")
        assert capability is not None
        schema = normalized_output_schema(capability.input_schema)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        assert validator.is_valid(_batch().arguments)
        assert not validator.is_valid(_batch([CapabilityCall(name="execute_program")]).arguments)
        assert not validator.is_valid(_batch([CapabilityCall(name="finish")]).arguments)
        assert not validator.is_valid(_batch([CapabilityCall(name="shell")]).arguments)
        assert not validator.is_valid(_batch([_child()] * 17).arguments)

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["error", "terminal", "execute_exception", "reduce_exception"])
def test_prefix_results_survive_failure_and_no_remainder_is_dispatched(outcome):
    async def scenario():
        base = RecordingEnvironment(
            **{
                "error": {"error_at": 2},
                "terminal": {"terminal_at": 2},
                "execute_exception": {"fail_at": 2},
                "reduce_exception": {"reduce_fail_at": 2},
            }[outcome]
        )
        env = ProgrammableEnvironment(base)
        state = await env.start(_spec())
        call = _batch([_child(key=f"job-{i}") for i in range(3)])
        observation = await env.execute(call, state)
        items = cast(list[dict], observation.data["items"])
        assert not observation.ok
        assert len(base.calls) == 2
        assert items[0]["observation"] == base.responses[0].model_dump(mode="json")
        assert items[2]["status"] == "not_executed"
        if outcome == "execute_exception":
            assert items[1]["status"] == "execution_unknown"
            assert items[1]["observation"] is None
        else:
            assert items[1]["observation"] == base.responses[1].model_dump(mode="json")
        if outcome.endswith("exception"):
            with pytest.raises(BatchExecutionError):
                env.reduce(state, call, observation)
            with pytest.raises(RuntimeError, match="cannot continue"):
                await env.execute(_child("inspect"), state)
        else:
            state = env.reduce(state, call, observation)
            assert state.latest_observation == observation
            assert state.status == ("terminal" if outcome == "terminal" else "active")
        assert len(base.reductions) == (1 if outcome.endswith("exception") else 2)

    asyncio.run(scenario())


def test_catalog_is_rechecked_after_each_reduction():
    async def scenario():
        base = RecordingEnvironment(unavailable_after=1)
        env = ProgrammableEnvironment(base)
        state = await env.start(_spec())
        state, observation = await _step(env, state, _batch([_child(), _child(key="job-2")]))
        assert len(base.calls) == 1
        assert observation.data["code"] == "BATCH_ITEM_UNAVAILABLE"
        assert observation.data["items"][1]["status"] == "not_executed"

    asyncio.run(scenario())


def test_retention_full_storage_transient_reads_and_program_coexistence():
    async def scenario():
        base = RecordingEnvironment()
        env = ProgrammableEnvironment(base)
        state = await env.start(_spec())
        originals = []
        for i in range(4):
            state, obs = await _step(env, state, _batch([_child(key=f"job-{i}")]))
            originals.append(obs.model_dump(mode="json"))
        for _ in range(7):
            state, _ = await _step(env, state, _child("inspect"))
        assert cast(dict, state.decision_view[BATCH_STATE_KEY])["retained"] == originals
        count = len(base.calls)
        state, obs = await _step(env, state, _batch())
        assert obs.data["code"] == "BATCH_LEDGER_FULL"
        assert len(base.calls) == count
        state, first_read = await _step(env, state, _batch([_child("inspect")], retain=False))
        assert first_read.ok
        state, _ = await _step(env, state, CapabilityCall(name="wait"))
        assert state.decision_view[BATCH_STATE_KEY]["transient"] == first_read.model_dump(
            mode="json"
        )
        state, next_read = await _step(env, state, _batch([_child("inspect")], retain=False))
        assert next_read.data["replaced_transient"] == first_read.data["batch_id"]
        assert state.decision_view[BATCH_STATE_KEY]["retained"] == originals
        state, replacement = await _step(env, state, _batch(release=["batch-1"]))
        assert replacement.ok
        assert len(state.decision_view[BATCH_STATE_KEY]["retained"]) == 4
        assert "batch-1" not in [
            x["data"]["batch_id"] for x in state.decision_view[BATCH_STATE_KEY]["retained"]
        ]
        program = CapabilityCall(
            name="execute_program",
            arguments={
                "mode": "define_and_execute",
                "program_id": None,
                "definition": {
                    "name": "wait_once",
                    "description": "Wait once.",
                    "primary_capability": "wait",
                    "followup_capability": None,
                    "condition": None,
                    "primary_output_paths": [],
                    "followup_output_paths": [],
                },
                "primary_arguments": [],
                "followup_arguments": [],
            },
        )
        retained = state.decision_view[BATCH_STATE_KEY]
        state, result = await _step(env, state, program)
        assert result.ok
        assert state.decision_view[BATCH_STATE_KEY] == retained
        assert state.latest_observation.action_kind == "execute_program"
        cast(dict, program.arguments["definition"])["primary_capability"] = "execute_batch"
        count = len(base.calls)
        state, result = await _step(env, state, program)
        assert not result.ok
        assert len(base.calls) == count
        # Model-facing values and returned observations cannot mutate the ledger.
        state.decision_view[BATCH_STATE_KEY]["retained"][0]["data"]["items"].clear()
        replacement.data["items"].clear()
        state, _ = await _step(env, state, _child("inspect"))
        assert all(x["data"]["items"] for x in state.decision_view[BATCH_STATE_KEY]["retained"])

    asyncio.run(scenario())


def _decision(call, status="pending", *, terminal=False):
    return {
        "previous_verification": {
            "status": status,
            "evidence": ["all work verified"] if status == "confirmed" else [],
        },
        "facts": ["multiple independent jobs require processing"],
        "competing_hypotheses": [],
        "contradicting_evidence": [],
        "strategy": "Launch all jobs, wait once, verify each result and combined effect.",
        "phase": "finish" if terminal else "mitigate",
        "selected_action": call.model_dump(mode="json"),
        "expected_result": ["all nine jobs complete"],
        "verification": ["read every job's result after waiting"],
        "task_completed": terminal,
    }


def _runner(base, decisions, store):
    reasoner = ScriptedReasoner(decisions)
    return AgentRunner(
        agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
        environment=ProgrammableEnvironment(base),
        memory=NoMemory(),
        run_store=store,
        policy=DecisionPolicy(),
        bootstrapper=ScriptedEnvironmentBootstrapper(),
    ), reasoner


def test_runner_launches_nine_jobs_before_wait_and_preserves_full_results_and_episode():
    async def scenario():
        launches = [_child(key=f"job-{i}") for i in range(9)]
        checks = [_child("inspect", key=f"job-{i}") for i in range(9)]
        decisions = [
            _decision(_batch(launches), "not_applicable"),
            _decision(CapabilityCall(name="wait")),
            _decision(_batch(checks)),
            _decision(CapabilityCall(name="finish"), "confirmed", terminal=True),
        ]
        base, store = RecordingEnvironment(), InMemoryRunStore()
        runner, reasoner = _runner(base, decisions, store)
        result = await runner.run(RunSpec(run_id="run-batch", environment="batch-test"))
        assert [x.name for x in base.calls] == ["launch"] * 9 + ["wait"] + ["inspect"] * 9 + [
            "finish"
        ]
        assert result.metrics.model_turns == 4
        assert result.metrics.simulator_calls == 20
        assert result.metrics.program_subcalls == 0
        for event in store.events:
            TraceEvent.model_validate_json(event.model_dump_json())
        context = decode_prompt_context(reasoner.requests[3].user_prompt)
        groups = context["environment_state"]["decision_view"][BATCH_STATE_KEY]["retained"]
        assert len(groups) == 2
        for i in range(9):
            assert groups[0]["data"]["items"][i]["observation"] == base.responses[i].model_dump(
                mode="json"
            )
            assert groups[1]["data"]["items"][i]["observation"] == base.responses[
                10 + i
            ].model_dump(mode="json")
        episode = next(
            x.payload.episode for x in store.events if isinstance(x.payload, EpisodeClosedPayload)
        )
        assert episode.selected_action == _batch(launches)
        assert "Batch batch-2" in episode.observation_summary
        assert "responses=[0, 1, 2, 3, 4, 5, 6, 7, 8]" in episode.observation_summary
        assert "execute_batch [ok]" in episode.observation_summary
        assert episode.verification.status == "confirmed"

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["execute", "reduce"])
def test_runner_failure_trace_preserves_received_prefix(failure):
    async def scenario():
        base = RecordingEnvironment(
            **({"fail_at": 2} if failure == "execute" else {"reduce_fail_at": 2})
        )
        store = InMemoryRunStore()
        runner, _ = _runner(base, [_decision(_batch([_child()] * 3), "not_applicable")], store)
        with pytest.raises(RunExecutionError):
            await runner.run(RunSpec(run_id="run-batch", environment="batch-test"))
        trace = next(x.payload for x in store.events if isinstance(x.payload, DecisionTracePayload))
        assert trace.observation is not None
        items = cast(list[dict], trace.observation.data["items"])
        assert items[0]["observation"] == base.responses[0].model_dump(mode="json")
        assert items[2]["status"] == "not_executed"
        assert len(base.calls) == 2

    asyncio.run(scenario())


def test_episode_summary_preserves_outcome_before_long_arguments_and_latest_evidence():
    observation = Observation(action_kind="execute_batch", summary="eight accepted, one refused")
    evidence = _episode_evidence_step(_batch([_child(key="a" * 250)] * 9), observation)
    assert observation.summary in evidence
    assert "[ok]" in evidence
    assert len(evidence) <= 500
    summary = _episode_outcome_summary(["old" * 200, evidence], fallback="fallback")
    assert summary.endswith(evidence)
    assert len(summary) <= 900
