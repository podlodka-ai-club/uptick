import asyncio
from datetime import UTC, datetime, timedelta

from uptick_agent.composition.sre_memory import compose_sre_online_memory
from uptick_agent.memory.contracts import MemoryContextRequest, TransitionAssemblyRequest
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

NOW = datetime(2026, 9, 7, tzinfo=UTC)
LOG = {
    "error": "SERVER_CAPACITY_EXCEEDED",
    "status": 500,
    "load_units": 11,
    "request_id": "request-public",
    "timestamp": "2030-10-14T12:53:00+03:00",
    "message": "server capacity exceeded: required=11 available=8",
}


def row(iteration):
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id=f"logs-{iteration}",
            run_id="capacity-run",
            iteration=iteration,
            occurred_at=NOW + timedelta(seconds=iteration),
            observation={"summary": "public logs"},
            action={"kind": "query_logs"},
            result={"ok": True, "action_kind": "query_logs", "data": {"logs": [LOG]}},
            trust_classification="external_untrusted",
            terminal=False,
        )
    )


def request(iteration, *, unrelated=False):
    return MemoryContextRequest(
        run_id="capacity-run",
        request_id=f"request-{iteration}",
        query="capacity",
        context={
            "iteration": iteration,
            "latest_result": {
                "ok": True,
                "action_kind": "query_logs",
                "data": {"logs": [{**LOG, "error": "FIREWALL_DENIED"}] if unrelated else [LOG]},
            },
        },
    )


def facts(context):
    return [
        i
        for i in context.items
        if i.envelope.item.get("candidate", {}).get("action_kind") == "request_admission"
    ]


def test_online_capacity_is_temporal_persistent_deduplicated_and_optional(tmp_path):
    async def scenario():
        db = tmp_path / "capacity.sqlite3"
        store = SqliteStructuredStore(db)
        memory = compose_sre_online_memory(
            store, namespace="test", mode="full", every_n_transitions=1
        )
        await memory.record_transition(row(1))
        assert not facts(await memory.build_context(request(1)))
        context = await memory.build_context(request(2))
        assert len(facts(context)) == 1
        assert facts(context)[0].envelope.item["support_count"] == 1
        assert "total installed capacity" in facts(context)[0].envelope.item["statement"]
        await memory.record_transition(row(2))
        reopened = compose_sre_online_memory(
            SqliteStructuredStore(db), namespace="test", mode="full"
        )
        assert (
            facts(await reopened.build_context(request(3)))[0].envelope.item["support_count"] == 1
        )
        assert not facts(await reopened.build_context(request(3, unrelated=True)))
        assert len(await store.list(namespace="test:episodes")) == 2
        off = compose_sre_online_memory(
            SqliteStructuredStore(db), namespace="test", mode="full", learn_capacity=False
        )
        assert not facts(await off.build_context(request(3)))
        assert set(memory.context_diagnostics["online_learning_by_layer"]) == {"cost", "capacity"}

    asyncio.run(scenario())
