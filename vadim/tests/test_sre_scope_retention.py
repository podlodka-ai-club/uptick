"""Public type applicability survives aggregate reads, with bounded provenance."""

import asyncio
from datetime import timedelta

import pytest
from test_sre_online_memory import RESOURCE, transitions

from uptick_agent.composition.sre_memory import compose_sre_online_memory
from uptick_agent.memory.contracts import MemoryContextRequest, TransitionAssemblyRequest
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler


def request(iteration=6, run_id="online-test-run", kind="get_metrics", data=None):
    return MemoryContextRequest(
        run_id=run_id,
        request_id=f"scope-{iteration}",
        query="current capacity and cost",
        context={
            "iteration": iteration,
            "latest_result": {
                "ok": True,
                "action_kind": kind,
                "data": {} if data is None else data,
            },
        },
    )


def derived(context):
    return [
        i for i in context.items if i.envelope.origin_module in {"world_model", "observed_lessons"}
    ]


def test_scope_survives_reopen_and_metrics_with_source_provenance(tmp_path):
    async def scenario():
        path = tmp_path / "scope.sqlite3"
        memory = compose_sre_online_memory(
            SqliteStructuredStore(path), namespace="scope", mode="full", every_n_transitions=2
        )
        for row in transitions():
            await memory.record_transition(row)
        reopened = compose_sre_online_memory(
            SqliteStructuredStore(path), namespace="scope", mode="full"
        )
        context = await reopened.build_context(request())
        assert len(derived(context)) == 2
        proof = reopened.context_diagnostics["online_recall"]["scope_evidence"]
        assert proof["transition_id"] == transitions()[-1].transition_id
        assert proof["age_iterations"] == 1 and proof["content_hash"]
        assert proof["run_id"] == "online-test-run"
        assert len(context.items) <= 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "query",
    [
        request(run_id="other-run"),
        request(iteration=5),
        request(iteration=134),
        request(kind="query_logs"),
        request(kind="get_operation", data={"command": "firewall.add"}),
        request(kind="get_resources", data={"servers": []}),
        request(kind="get_resources", data={"servers": [{**RESOURCE, "instance_type": "unseen"}]}),
    ],
)
def test_retained_scope_rejects_wrong_run_future_stale_unrelated_and_replaced(tmp_path, query):
    async def scenario():
        memory = compose_sre_online_memory(
            SqliteStructuredStore(tmp_path / "scope.sqlite3"),
            namespace="scope",
            mode="full",
            every_n_transitions=2,
        )
        for row in transitions():
            await memory.record_transition(row)
        assert not derived(await memory.build_context(query))

    asyncio.run(scenario())


def test_new_empty_inventory_invalidates_previous_scope_after_another_read(tmp_path):
    async def scenario():
        memory = compose_sre_online_memory(
            SqliteStructuredStore(tmp_path / "scope.sqlite3"),
            namespace="scope",
            mode="full",
            every_n_transitions=2,
        )
        rows = transitions()
        for row in rows:
            await memory.record_transition(row)
        assert derived(await memory.build_context(request()))
        empty = DefaultExperienceTransitionAssembler().assemble(
            TransitionAssemblyRequest(
                transition_id="empty-inventory",
                run_id=rows[-1].run_id,
                iteration=6,
                occurred_at=rows[-1].occurred_at + timedelta(seconds=1),
                observation={"summary": "public inventory refresh"},
                action={"kind": "get_resources"},
                result={"ok": True, "data": {"servers": []}},
                terminal=False,
                trust_classification="external_untrusted",
            )
        )
        await memory.record_transition(empty)
        assert not derived(await memory.build_context(request(iteration=7)))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,after_iteration,expected",
    [
        ("succeeded", 81, True),
        ("running", 81, False),
        ("failed", 81, False),
        ("succeeded", 114, False),
    ],
)
def test_sre_long_operation_projects_only_explicit_completion_with_fresh_metrics(
    tmp_path, status, after_iteration, expected
):
    from test_sre_online_memory import evidence

    from uptick_agent.composition.sre_memory import project_sre_outcomes

    async def scenario():
        originals = transitions()
        rows = []
        for original, iteration in zip(originals[:4], [1, 2, 80, after_iteration], strict=True):
            result = original.result
            if iteration == 80:
                result = {**result, "data": {**result["data"], "status": status}}
            rows.append(
                DefaultExperienceTransitionAssembler().assemble(
                    TransitionAssemblyRequest(
                        transition_id=f"long-{iteration}",
                        run_id=original.run_id,
                        iteration=iteration,
                        occurred_at=originals[0].occurred_at + timedelta(seconds=iteration),
                        observation=original.observation,
                        action=original.action,
                        result=result,
                        operation_links=original.operation_links,
                        after_objective_metrics=original.objective_metrics,
                        trust_classification="external_untrusted",
                        terminal=False,
                    )
                )
            )
        projected = project_sre_outcomes(evidence(rows), {rows[0].run_id: rows[-1].occurred_at})
        assert bool(projected) is expected
        memory = compose_sre_online_memory(
            SqliteStructuredStore(tmp_path / "long.sqlite3"),
            namespace="long",
            mode="full",
            every_n_transitions=2,
        )
        for row in rows:
            await memory.record_transition(row)
        context = await memory.build_context(
            request(
                iteration=after_iteration + 1, kind="get_resources", data={"servers": [RESOURCE]}
            )
        )
        assert bool(derived(context)) is expected
        if expected:
            assert projected[0].result["causal_credit"] is False
            assert {r["record_id"] for r in projected[0].result["source_interval"]} == {
                r.transition_id for r in rows
            }

    asyncio.run(scenario())
