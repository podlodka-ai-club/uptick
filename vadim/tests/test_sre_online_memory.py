"""SRE public evidence -> online memory -> later runner context integration."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from uptick_agent.composition.sre_memory import compose_sre_online_memory, project_sre_outcomes
from uptick_agent.memory.audit_contracts import AuditTraceWrite
from uptick_agent.memory.contracts import (
    MemoryContextRequest,
    ObjectiveMetric,
    OperationLink,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    StoredRecord,
)
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

NOW = datetime(2026, 9, 7, tzinfo=UTC)
RESOURCE = {"role": "backend", "instance_type": "backend.standard", "server_id": "public-1"}


def transitions():
    rows = [
        ({"kind": "get_overview"}, {"ok": True}, [], 100),
        (
            {
                "kind": "control_command",
                "request": {
                    "command": "server.create",
                    "params": {
                        "role": "backend",
                        "instance_type": "backend.standard",
                        "name": "test",
                    },
                },
            },
            {"ok": True, "data": {"operation_id": "public-operation", "status": "queued"}},
            [OperationLink(operation_id="public-operation", relation="initiated")],
            None,
        ),
        (
            {"kind": "get_operation"},
            {"ok": True, "data": {"operation_id": "public-operation", "status": "succeeded"}},
            [OperationLink(operation_id="public-operation", relation="observed")],
            None,
        ),
        ({"kind": "get_overview"}, {"ok": True}, [], 180),
        ({"kind": "get_resources"}, {"ok": True, "data": {"servers": [RESOURCE]}}, [], None),
    ]
    result = []
    for index, (action, observed, links, value) in enumerate(rows, 1):
        metrics = (
            []
            if value is None
            else [ObjectiveMetric(name="current_cost_per_hour_minor", value=value, unit="minor")]
        )
        result.append(
            DefaultExperienceTransitionAssembler().assemble(
                TransitionAssemblyRequest(
                    transition_id=f"public-transition-{index}",
                    run_id="online-test-run",
                    iteration=index,
                    occurred_at=NOW + timedelta(seconds=index),
                    observation={"summary": "public prior result"},
                    action=action,
                    result=observed,
                    operation_links=links,
                    after_objective_metrics=metrics,
                    trust_classification="external_untrusted",
                    terminal=False,
                )
            )
        )
    return result


def evidence(rows):
    records = [
        StoredRecord.from_write(
            RecordWrite(
                namespace="raw",
                record_id=t.transition_id,
                record_type="experience-transition",
                payload=t.model_dump(mode="json"),
                created_at=t.occurred_at,
            )
        )
        for t in rows
    ]
    snapshot = MemorySnapshot.create(
        snapshot_id=f"prefix-{len(rows)}",
        namespace="raw",
        members=[
            SnapshotMember(record_id=r.record_id, content_hash=r.content_hash) for r in records
        ],
    )
    return LessonEvidence(snapshot=snapshot, records=records, runs=[])


def test_projection_waits_for_completion_and_post_sample_and_has_stable_identity():
    rows = transitions()
    cutoffs = {"online-test-run": rows[-1].occurred_at}
    assert project_sre_outcomes(evidence(rows[:2]), cutoffs) == []
    assert project_sre_outcomes(evidence(rows[:3]), cutoffs) == []
    first = project_sre_outcomes(evidence(rows[:4]), cutoffs)
    later = project_sre_outcomes(evidence(rows), cutoffs)
    assert len(first) == 1 and first == later
    assert first[0].result["hourly_cost_delta_minor"] == 80
    assert first[0].occurred_at == rows[3].occurred_at
    assert first[0].iteration == 4
    assert first[0].result["causal_credit"] is False
    assert project_sre_outcomes(evidence(rows), {"online-test-run": rows[2].occurred_at}) == []


@pytest.mark.parametrize("mode,module", [("world", "world_model"), ("lessons", "observed_lessons")])
def test_live_lifecycle_materializes_reopens_recalls_and_passes_audit(tmp_path, mode, module):
    async def scenario():
        store = SqliteStructuredStore(tmp_path / "online.sqlite3")
        runtime = compose_sre_online_memory(
            store, namespace="test", mode=mode, every_n_transitions=2
        )
        rows = transitions()
        for transition in rows[:3]:
            await runtime.record_transition(transition)
        request = MemoryContextRequest(
            run_id="online-test-run",
            request_id="next-decision",
            query="resources",
            context={
                "iteration": 4,
                "latest_result": {
                    "ok": True,
                    "action_kind": "get_resources",
                    "data": {"servers": [RESOURCE]},
                },
            },
        )
        before = await runtime.build_context(request)
        assert not any(i.envelope.origin_module == module for i in before.items)
        await runtime.record_transition(rows[3])
        same = await runtime.build_context(request)
        assert not any(i.envelope.origin_module == module for i in same.items)
        reopened = compose_sre_online_memory(
            SqliteStructuredStore(tmp_path / "online.sqlite3"),
            namespace="test",
            mode=mode,
            every_n_transitions=2,
        )
        next_request = request.model_copy(update={"context": {**request.context, "iteration": 5}})
        recalled = await reopened.build_context(next_request)
        derived = [i for i in recalled.items if i.envelope.origin_module == module]
        assert len(derived) == 1
        assert derived[0].envelope.item["candidate"]["result_value"] == 80
        assert derived[0].envelope.item["support_count"] == 1
        await reopened.record_transition(rows[3])
        repeated = await reopened.build_context(next_request)
        fact = next(i for i in repeated.items if i.envelope.origin_module == module)
        assert fact.envelope.item["support_count"] == 1
        AuditTraceWrite(
            event_id="a" * 64,
            event_type="decision.completed",
            run_id="online-test-run",
            sequence=1,
            iteration=5,
            request_id="request-5",
            decision_id="decision-5",
            transition_id="transition-5",
            outcome_correlation_id="outcome-5",
            producer_id="test",
            producer_version="1.0",
            raw_bodies={"decision_traces": {"memory": reopened.context_diagnostics}},
        )

    asyncio.run(scenario())


def test_cli_factory_exposes_online_profile_and_rejects_unsupported_benchmark(tmp_path):
    from uptick_agent import cli

    args = cli._parser().parse_args(
        [
            "run",
            "--seed",
            "42",
            "--memory",
            "online-world",
            "--memory-database",
            str(tmp_path / "cli.sqlite3"),
        ]
    )
    assert cli._memory_factory(args)().online_learning_enabled is True
    args.command = "benchmark"
    with pytest.raises(ValueError, match="single v2 run"):
        cli._memory_factory(args)


@pytest.mark.parametrize("mode", ["world", "full"])
def test_real_runner_writes_learns_and_supplies_world_fact_to_later_model(tmp_path, mode):
    from types import SimpleNamespace

    from pydantic import BaseModel, Field

    from uptick_agent.decisions.runtime import ToolResult
    from uptick_agent.environment.contracts import EnvironmentDecisionSpec
    from uptick_agent.runs.config import AgentConfig
    from uptick_agent.runs.execute import AgentRunner
    from uptick_agent.runs.runtime_results import RuntimeRunResult

    class Action(BaseModel):
        kind: str
        request: dict = Field(default_factory=dict)

    class Decision(BaseModel):
        action: Action
        task_completed: bool = False

    rows = transitions()

    class Environment:
        decision_spec = EnvironmentDecisionSpec(
            response_model=Decision, objective="observe public cost"
        )
        executed = 0

        async def start(self, *, seed, **kwargs):
            return SimpleNamespace(run_id="online-test-run", seed=seed), ToolResult(
                action_kind="start", summary="public start"
            )

        async def execute(self, session, action):
            self.executed += 1
            if self.executed <= len(rows):
                row = rows[self.executed - 1]
                return ToolResult(
                    action_kind=action.kind,
                    summary="public operation result",
                    data=row.result.get("data", {}),
                    objective_metrics=row.objective_metrics,
                    operation_links=row.operation_links,
                )
            return ToolResult(action_kind=action.kind, summary="finished", terminal=True)

        async def finish(self, session, *, steps, duration_seconds, stop_reason):
            return RuntimeRunResult(
                run_id=session.run_id,
                seed=session.seed,
                agent_id="test",
                agent_version="1",
                status="completed",
                steps=steps,
                duration_seconds=duration_seconds,
                stop_reason=stop_reason,
            )

    class Model:
        contexts = []

        async def decide(self, context):
            self.contexts.append(context)
            index = len(self.contexts) - 1
            action = rows[index].action if index < len(rows) else {"kind": "finish"}
            return Decision(action=Action.model_validate(action))

    async def scenario():
        memory = compose_sre_online_memory(
            SqliteStructuredStore(tmp_path / "runner.sqlite3"),
            namespace="runner",
            mode=mode,
            every_n_transitions=2,
        )
        model, environment = Model(), Environment()
        result = await AgentRunner(
            model=model, memory=memory, environment=environment, config=AgentConfig(max_steps=6)
        ).run(seed=42)
        assert result.status == "completed" and environment.executed == 6
        assert all(
            not any(i.envelope.origin_module == "world_model" for i in c.memory_context.items)
            for c in model.contexts[:4]
        )
        facts = [
            i
            for i in model.contexts[5].memory_context.items
            if i.envelope.origin_module == "world_model"
        ]
        assert len(facts) == 1 and facts[0].envelope.item["candidate"]["result_value"] == 80
        assert memory.online_learning_diagnostics["materialization_count"] >= 2

    asyncio.run(scenario())


@pytest.mark.parametrize("module", ["lessons", "tool_knowledge", "consolidation", "forgetting"])
def test_composition_rejects_missing_actual_episode_dependency(module):
    from uptick_agent.composition.memory import compose_experimental_runtime
    from uptick_agent.memory.config import MemoryConfiguration
    from uptick_agent.memory.contracts import MemoryValidationError
    from uptick_agent.memory.stores import InMemoryStructuredStore

    config = MemoryConfiguration.model_validate({module: {"enabled": True}})
    with pytest.raises(MemoryValidationError, match=f"{module} requires episodic enabled"):
        compose_experimental_runtime(config, InMemoryStructuredStore(), namespace="invalid")


def test_composition_rejects_playbooks_without_actual_lesson_dependency():
    from uptick_agent.composition.memory import compose_experimental_runtime
    from uptick_agent.memory.config import MemoryConfiguration
    from uptick_agent.memory.contracts import MemoryValidationError
    from uptick_agent.memory.stores import InMemoryStructuredStore

    config = MemoryConfiguration.model_validate(
        {
            "episodic": {"enabled": True},
            "world_model": {"enabled": True},
            "playbooks": {"enabled": True},
        }
    )
    with pytest.raises(MemoryValidationError, match="playbooks requires lessons enabled"):
        compose_experimental_runtime(config, InMemoryStructuredStore(), namespace="invalid")
