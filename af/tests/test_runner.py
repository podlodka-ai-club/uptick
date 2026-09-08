import asyncio
from datetime import UTC, datetime

from tests.helpers import decode_prompt_context
from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.errors import ReasonerFailure, RunExecutionError
from uptick_agent.core.models import (
    Capability,
    CapabilityCatalog,
    Observation,
    ReasonerConfig,
    ReasoningTelemetry,
    RunMetadata,
    RunResult,
    RunSpec,
    TokenUsage,
)
from uptick_agent.core.policy import DecisionPolicy
from uptick_agent.core.sgr import CurrentSGR
from uptick_agent.core.trace_models import (
    DecisionTracePayload,
    EpisodeClosedPayload,
    RunStartedPayload,
)
from uptick_agent.environments.scripted import ScriptedEnvironment
from uptick_agent.memory import NoMemory
from uptick_agent.reasoners.scripted import ScriptedReasoner
from uptick_agent.runtime.bootstrap import ScriptedEnvironmentBootstrapper
from uptick_agent.runtime.runner import AgentRunner
from uptick_agent.store import InMemoryRunStore


def _catalog() -> CapabilityCatalog:
    empty_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    return CapabilityCatalog(
        items=[
            Capability(name="inspect", description="inspect", input_schema=empty_schema),
            Capability(
                name="finish",
                description="finish",
                input_schema=empty_schema,
                terminal=True,
            ),
        ]
    )


def _output(
    name: str,
    *,
    completed: bool = False,
    previous: bool = False,
    pending: bool = False,
) -> dict:
    previous_status = "pending" if pending else ("confirmed" if previous else "not_applicable")
    return {
        "phase": "finish" if completed else "observe",
        "facts": ["scripted test state"],
        "competing_hypotheses": ["the scripted action is appropriate"],
        "contradicting_evidence": [],
        "previous_verification": {
            "status": previous_status,
            "evidence": ["the prior scripted observation arrived"] if previous else [],
        },
        "strategy": "follow the scripted test sequence",
        "selected_action": {"name": name, "arguments": {}},
        "expected_result": ["the scripted observation is returned"],
        "verification": ["compare the next observation with the script"],
        "task_completed": completed,
    }


def test_runner_records_full_cycle_and_read_only_episode() -> None:
    async def scenario() -> None:
        class FrozenMemory(NoMemory):
            async def record_episode(self, episode, base_revision):
                del episode, base_revision
                raise AssertionError("frozen runner attempted an episode write")

            async def activate_lesson(self, lesson, base_revision):
                del lesson, base_revision
                raise AssertionError("frozen runner attempted a lesson write")

        store = InMemoryRunStore()
        environment = ScriptedEnvironment(
            name="one",
            catalog=_catalog(),
            observations=[
                Observation(action_kind="inspect", summary="healthy"),
                Observation(action_kind="finish", summary="done", terminal=True),
            ],
            result=RunResult(
                run_id="run-123",
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
        )
        reasoner = ScriptedReasoner(
            [_output("inspect"), _output("finish", completed=True, previous=True)]
        )
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
            environment=environment,
            memory=FrozenMemory(),
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )

        result = await runner.run(
            RunSpec(
                run_id="run-123",
                environment="one",
                step_limit=3,
            )
        )

        assert result.status == "completed"
        assert result.steps == 2
        assert [event.kind for event in store.events] == [
            "run_started",
            "decision_trace",
            "decision_trace",
            "episode_closed",
            "run_finished",
        ]
        first_trace = store.events[1].payload
        assert isinstance(first_trace, DecisionTracePayload)
        assert first_trace.iteration == 1
        assert isinstance(first_trace.context_projection, dict)
        assert first_trace.decision is not None
        assert first_trace.policy_result is not None
        assert first_trace.observation is not None
        closed = store.events[3].payload
        assert isinstance(closed, EpisodeClosedPayload)
        assert "one started" in closed.episode.situation_summary
        assert "healthy" not in closed.episode.situation_summary
        assert closed.episode.observation_summary == "inspect [ok] -> healthy; arguments={}"
        manifest = await store.load_manifest("run-123")
        assert manifest is not None
        assert manifest.status == "completed"
        assert manifest.system_prompt_sha256 is not None
        assert manifest.schema_sha256 is not None
        assert result.metrics.model_turns == 2
        assert result.metrics.capability_executions == 2
        assert result.metrics.action_counts == {"inspect": 1, "finish": 1}
        assert result.metrics.policy_rejections == 0

    asyncio.run(scenario())


def test_runner_keeps_short_term_context_with_no_memory() -> None:
    async def scenario() -> None:
        reasoner = ScriptedReasoner(
            [_output("inspect"), _output("finish", completed=True, previous=True)]
        )
        environment = ScriptedEnvironment(
            name="short-term",
            catalog=_catalog(),
            observations=[
                Observation(action_kind="inspect", summary="evidence"),
                Observation(action_kind="finish", summary="done", terminal=True),
            ],
            result=RunResult(
                run_id="run-short",
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
        )
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
            environment=environment,
            memory=NoMemory(),
            run_store=InMemoryRunStore(),
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )

        await runner.run(RunSpec(run_id="run-short", environment="short-term"))

        second_context = decode_prompt_context(reasoner.requests[1].user_prompt)
        assert isinstance(second_context["memory_brief"], dict)
        assert second_context["environment_state"]["latest_observation"]["summary"] == "evidence"
        working = second_context["agent_working_state"]
        assert working["active_strategy"] == "follow the scripted test sequence"
        assert working["open_decision"]["step"] == 1
        situation = second_context["agent_working_state"]["open_decision"]["situation_summary"]
        assert "short-term started" in situation
        assert len(situation) <= 500

    asyncio.run(scenario())


def test_runner_closes_previous_verification_into_one_compact_bridge() -> None:
    async def scenario() -> None:
        reasoner = ScriptedReasoner(
            [
                _output("inspect"),
                _output("inspect", previous=True),
                _output("finish", completed=True, previous=True),
            ]
        )
        environment = ScriptedEnvironment(
            name="verification",
            catalog=_catalog(),
            observations=[
                Observation(action_kind="inspect", summary="first evidence"),
                Observation(action_kind="inspect", summary="second evidence"),
                Observation(action_kind="finish", summary="done", terminal=True),
            ],
            result=RunResult(
                run_id="run-verification",
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
        )
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
            environment=environment,
            memory=NoMemory(),
            run_store=InMemoryRunStore(),
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )

        await runner.run(RunSpec(run_id="run-verification", environment="verification"))

        second_context = decode_prompt_context(reasoner.requests[1].user_prompt)
        second_state = second_context["agent_working_state"]
        assert second_state["strategy_started_step"] == 1
        assert second_state["open_decision"]["step"] == 1
        assert second_state["last_closed_episode"] is None

        third_context = decode_prompt_context(reasoner.requests[2].user_prompt)
        third_state = third_context["agent_working_state"]
        assert third_state["strategy_started_step"] == 1
        assert third_state["open_decision"]["step"] == 2
        assert third_state["last_closed_episode"]["assessment"]["status"] == "confirmed"
        assert third_state["last_closed_episode"]["selected_action"]["name"] == "inspect"

    asyncio.run(scenario())


def test_runner_pending_verification_preserves_the_original_open_decision() -> None:
    async def scenario() -> None:
        reasoner = ScriptedReasoner(
            [_output("inspect"), *[_output("inspect", pending=True) for _ in range(6)]]
            + [_output("finish", completed=True, previous=True)]
        )
        environment = ScriptedEnvironment(
            name="bounded-decisions",
            catalog=_catalog(),
            observations=[
                Observation(action_kind="inspect", summary=f"evidence {index}")
                for index in range(7)
            ]
            + [Observation(action_kind="finish", summary="done", terminal=True)],
            result=RunResult(
                run_id="run-bounded-decisions",
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
        )
        store = InMemoryRunStore()
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
            environment=environment,
            memory=NoMemory(),
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )

        await runner.run(
            RunSpec(
                run_id="run-repeated",
                environment="bounded-decisions",
                step_limit=8,
            )
        )

        final_context = decode_prompt_context(reasoner.requests[-1].user_prompt)
        working = final_context["agent_working_state"]
        assert working["open_decision"]["step"] == 1
        assert working["strategy_started_step"] == 1
        assert working["last_closed_episode"] is None
        closed = next(
            event.payload
            for event in store.events
            if isinstance(event.payload, EpisodeClosedPayload)
        )
        assert "evidence 0" in closed.episode.observation_summary
        assert "evidence 6" in closed.episode.observation_summary
        assert "done" not in closed.episode.observation_summary
        assert len(closed.episode.observation_summary) <= 900

    asyncio.run(scenario())


def test_runner_obeys_step_limit_and_environment_owns_result() -> None:
    async def scenario() -> None:
        environment = ScriptedEnvironment(
            name="budget",
            catalog=_catalog(),
            observations=[Observation(action_kind="inspect", summary="not terminal")],
            result=RunResult(
                run_id="run-budget",
                status="stopped",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
        )
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=ScriptedReasoner([_output("inspect")]), sgr=CurrentSGR()),
            environment=environment,
            memory=NoMemory(),
            run_store=InMemoryRunStore(),
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )

        result = await runner.run(
            RunSpec(
                run_id="run-budget",
                environment="budget",
                step_limit=1,
            )
        )

        assert result.steps == 1
        assert result.stop_reason == "step limit reached"
        assert result.forced

    asyncio.run(scenario())


def test_unlimited_run_passes_160_steps_and_hides_progress_budget() -> None:
    async def scenario() -> None:
        reads = 161
        environment = ScriptedEnvironment(
            name="continuous",
            catalog=_catalog(),
            observations=[
                *[Observation(action_kind="inspect", summary="still active") for _ in range(reads)],
                Observation(action_kind="finish", summary="done", terminal=True),
            ],
            result=RunResult(
                run_id="run-unlimited",
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
        )
        reasoner = ScriptedReasoner(
            [
                *[_output("inspect", previous=i > 0) for i in range(reads)],
                _output("finish", completed=True, previous=True),
            ]
        )
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
            environment=environment,
            memory=NoMemory(),
            run_store=InMemoryRunStore(),
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )

        result = await runner.run(
            RunSpec(run_id="run-unlimited", environment="continuous", step_limit=None)
        )

        assert result.status == "completed"
        assert result.steps == reads + 1
        assert not result.forced
        assert all(
            decode_prompt_context(request.user_prompt).get("progress") is None
            for request in reasoner.requests
        )

    asyncio.run(scenario())


def test_policy_rejection_precedes_execute_and_is_recorded_as_failure() -> None:
    async def scenario() -> None:
        store = InMemoryRunStore()
        environment = ScriptedEnvironment(
            name="policy",
            catalog=_catalog(),
            observations=[],
            result=RunResult(
                run_id="run-policy",
                status="failed",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
        )
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=ScriptedReasoner([_output("unknown")]), sgr=CurrentSGR()),
            environment=environment,
            memory=NoMemory(),
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )

        try:
            await runner.run(RunSpec(run_id="run-policy", environment="policy"))
        except RuntimeError as error:
            assert "unknown capability" in str(error)
        else:  # pragma: no cover - the policy must fail closed
            raise AssertionError("policy rejection did not fail the run")

        assert environment.calls == []
        assert [event.kind for event in store.events][-2:] == [
            "decision_trace",
            "run_failed",
        ]
        rejected_trace = store.events[-2].payload
        assert isinstance(rejected_trace, DecisionTracePayload)
        assert rejected_trace.failure is not None
        manifest = await store.load_manifest("run-policy")
        assert manifest is not None and manifest.status == "failed"
        assert manifest.metrics.policy_rejections == 1
        assert manifest.metrics.capability_executions == 0
        assert manifest.metrics.action_counts == {}

    asyncio.run(scenario())


def test_runner_finalizes_failure_manifest_and_partial_decision_trace() -> None:
    class FailingReasoner:
        async def reason(self, request):
            del request
            raise ReasonerFailure(
                "safe provider failure",
                category="transient",
                telemetry=ReasoningTelemetry(
                    provider="fake",
                    requested_model="fake-v1",
                    thread_mode="stateless",
                    attempts=2,
                    duration_seconds=1.25,
                    sdk_name="fake-sdk",
                    sdk_version="1",
                    provider_instructions_sha256="a" * 64,
                    token_usage=TokenUsage(input_tokens=7, total_tokens=7),
                ),
            )

    async def scenario() -> None:
        store = InMemoryRunStore()
        environment = ScriptedEnvironment(
            name="failure",
            catalog=_catalog(),
            observations=[],
            result=RunResult(
                run_id="run-failure",
                status="failed",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
        )
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=FailingReasoner(), sgr=CurrentSGR()),
            environment=environment,
            memory=NoMemory(),
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )
        spec = RunSpec(
            run_id="run-failure",
            environment="failure",
            parameters={"seed": 9},
            metadata=RunMetadata(
                reasoner=ReasonerConfig(
                    provider="fake", model="fake-v1", thread_mode="stateless", retries=1
                ),
                memory_mode="none",
                git_revision="b" * 40,
                git_dirty=False,
                agent_config_source="agent.yaml",
                agent_config_sha256="c" * 64,
            ),
        )

        try:
            await runner.run(spec)
        except RunExecutionError as error:
            assert error.stage == "reasoner"
        else:  # pragma: no cover
            raise AssertionError("provider failure did not fail the run")

        assert [event.kind for event in store.events] == [
            "run_started",
            "decision_trace",
            "run_failed",
        ]
        trace = store.events[1].payload
        assert isinstance(trace, DecisionTracePayload)
        assert trace.decision is None
        assert trace.failure is not None
        assert trace.failure.stage == "reasoner"
        assert "metadata" not in trace.context_projection
        started = store.events[0].payload
        assert isinstance(started, RunStartedPayload)
        assert started.run_id == "run-failure"
        manifest = await store.load_manifest("run-failure")
        assert manifest is not None
        assert manifest.agent_config_source == "agent.yaml"
        assert manifest.agent_config_sha256 == "c" * 64
        assert manifest.status == "failed"
        assert manifest.failure_stage == "reasoner"
        assert manifest.metrics.model_turns == 2
        assert manifest.metrics.model_duration_seconds == 1.25
        assert manifest.metrics.token_usage.input_tokens == 7
        assert manifest.baseline_profile == "no-memory-v1"
        assert manifest.provider_instructions_sha256 == "a" * 64
        assert manifest.agent_config_source == "agent.yaml"
        assert manifest.agent_config_sha256 == "c" * 64
        assert manifest.reproducible
        assert manifest.non_reproducible_reasons == []

    asyncio.run(scenario())


def test_runner_measures_environment_ports_without_changing_scripted_behavior() -> None:
    class IncrementingClock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            current = self.value
            self.value += 1
            return current

    async def scenario() -> None:
        store = InMemoryRunStore()
        environment = ScriptedEnvironment(
            name="timed",
            catalog=_catalog(),
            observations=[Observation(action_kind="finish", summary="done", terminal=True)],
            result=RunResult(
                run_id="run-timed",
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="",
            ),
        )
        times = iter(
            [
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            ]
        )
        runner = AgentRunner(
            agent_core=AgentCore(
                reasoner=ScriptedReasoner([_output("finish", completed=True)]),
                sgr=CurrentSGR(),
            ),
            environment=environment,
            memory=NoMemory(),
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
            monotonic_fn=IncrementingClock(),
            utcnow_fn=lambda: next(times),
        )

        result = await runner.run(RunSpec(run_id="run-timed", environment="timed"))

        assert result.metrics.environment_duration_seconds == 7
        assert result.metrics.capability_executions == 1
        assert result.metrics.action_counts == {"finish": 1}
        assert result.metrics.capability_executions == sum(result.metrics.action_counts.values())
        assert result.metrics.model_turns == 1
        trace = store.events[1].payload
        assert isinstance(trace, DecisionTracePayload)
        assert trace.environment_duration_seconds == 2
        manifest = await store.load_manifest("run-timed")
        assert manifest is not None
        assert manifest.started_at == datetime(2026, 1, 1, tzinfo=UTC)
        assert manifest.finished_at == datetime(2026, 1, 1, 0, 1, tzinfo=UTC)

    asyncio.run(scenario())
