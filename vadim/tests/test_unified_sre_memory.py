"""Focused full-profile SRE memory integration checks."""

import asyncio
from datetime import UTC, datetime, timedelta

from uptick_agent.composition.sre_memory import compose_sre_online_memory
from uptick_agent.memory.contracts import (
    MemoryContextRequest,
    ObjectiveMetric,
    OperationLink,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

RESOURCE = {"role": "backend", "instance_type": "backend.standard", "server_id": "public-1"}


def transitions():
    rows = [
        ({"kind": "get_overview"}, {"ok": True}, [], 100),
        (
            {
                "kind": "control_command",
                "request": {
                    "command": "server.create",
                    "params": {"role": "backend", "instance_type": "backend.standard"},
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
    now = datetime(2026, 9, 7, tzinfo=UTC)
    assembler = DefaultExperienceTransitionAssembler()
    return [
        assembler.assemble(
            TransitionAssemblyRequest(
                transition_id=f"full-transition-{index}",
                run_id="full-run",
                iteration=index,
                occurred_at=now + timedelta(seconds=index),
                observation={"summary": "public prior result"},
                action=action,
                result=result,
                operation_links=links,
                after_objective_metrics=(
                    []
                    if value is None
                    else [
                        ObjectiveMetric(
                            name="current_cost_per_hour_minor", value=value, unit="minor"
                        )
                    ]
                ),
                trust_classification="external_untrusted",
                terminal=False,
            )
        )
        for index, (action, result, links, value) in enumerate(rows, 1)
    ]


def test_full_profile_balances_observed_views_and_runs_real_lifecycle(tmp_path):
    async def scenario():
        store = SqliteStructuredStore(tmp_path / "full.sqlite3")
        memory = compose_sre_online_memory(
            store,
            namespace="sre-full",
            mode="full",
            every_n_transitions=4,
        )
        rows = transitions()
        for row in rows[:4]:
            await memory.record_transition(row)

        context = await memory.build_context(
            MemoryContextRequest(
                run_id="full-run",
                request_id="full-context",
                query="resources",
                context={
                    "iteration": 5,
                    "latest_result": {
                        "ok": True,
                        "action_kind": "get_resources",
                        "data": {"servers": [RESOURCE]},
                    },
                },
            )
        )
        observed = {
            item.envelope.origin_module
            for item in context.items
            if item.envelope.origin_module in {"world_model", "observed_lessons"}
        }
        assert observed == {"world_model", "observed_lessons"}
        assert len(context.items) <= 3
        assert memory.configuration.profile_id == "A9"
        assert {
            "episodic",
            "lessons",
            "world_model",
            "playbooks",
            "tool_knowledge",
            "consolidation",
            "forgetting",
        } <= set(memory.enabled_module_ids)

        outcome = RunOutcome(
            run_id="full-run",
            status="completed",
            finished_at=rows[-1].occurred_at,
            stop_reason="finished",
        )
        await memory.finalize_run(outcome)

        telemetry = memory.module_telemetry
        assert all(
            telemetry[name].finalization_events == 1
            for name in ("episodic", "lessons", "playbooks", "tool_knowledge", "world_model")
        )
        assert telemetry["consolidation"].consolidation_events == 2
        assert memory.context_diagnostics["consolidation"]["applied"] is True
        assert memory.context_diagnostics["sre_capabilities"]["gated"]

        world_rows = await store.list(namespace="sre-full:world:observed")
        lesson_rows = await store.list(namespace="sre-full:lessons:evidence:observed")
        assert world_rows and lesson_rows

    asyncio.run(scenario())


def test_cli_exposes_online_full_profile(tmp_path):
    from uptick_agent import cli

    args = cli._parser().parse_args(
        [
            "run",
            "--seed",
            "42",
            "--memory",
            "online-full",
            "--memory-database",
            str(tmp_path / "cli.sqlite3"),
        ]
    )
    memory = cli._memory_factory(args)()
    assert memory.configuration.profile_id == "A9"
    assert {"playbooks", "tool_knowledge", "consolidation", "forgetting"} <= set(
        memory.enabled_module_ids
    )
