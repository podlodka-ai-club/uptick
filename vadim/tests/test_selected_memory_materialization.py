from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from uptick_agent.memory.config import ContextBudgetConfig, MemoryConfiguration, ModuleConfig
from uptick_agent.memory.contracts import (
    ContextItem,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    MemoryValidationError,
    ProvenanceRef,
    TransitionAssemblyRequest,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.episodic import EpisodicMemory
from uptick_agent.memory.orchestrator import (
    MemoryModuleRegistration,
    MemoryOrchestrator,
    _estimate_utf8_bytes,
)
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.memory.stores.contracts import canonical_json
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler


def item(module, *, score=1):
    return ContextItem(
        envelope=UntrustedMemoryEnvelope(
            item_id=module + "-item",
            artefact_type="episode",
            origin_module=module,
            origin_version="1.0",
            trust_classification="external_untrusted",
            provenance=[ProvenanceRef(artefact_id="public-source", content_hash="a" * 64)],
            item={"result": "public result", "nested": {"keep": True}},
        ),
        score=score,
        selection_reason="fixed",
        estimated_tokens=0,
    )


class Contributor:
    def __init__(self, module, *, enabled=True, behavior="same"):
        self.module = module
        self.context_materialization_enabled = enabled
        self.behavior = behavior
        self.allowances = []
        self.original = item(module, score=2 if module == "episodic" else 1)

    async def retrieve(self, request):
        return MemoryContribution(
            module_id=self.module, module_version="1.0", items=[self.original]
        )

    async def materialize_selected(self, items, request, *, max_estimated_tokens):
        self.allowances.append(max_estimated_tokens)
        if self.behavior == "failure":
            items[0].envelope.item["result"] = "mutated before failure"
            raise MemoryValidationError("source changed")
        if self.behavior == "count":
            return []
        if self.behavior == "identity":
            items[0].envelope.item_id = "forged"
        if self.behavior == "trust":
            items[0].envelope.trust_classification = "human_attested"
        if self.behavior == "score":
            items[0].score = 999
        if self.behavior == "overflow":
            items[0].envelope.item["result"] = "x" * 10000
        if self.behavior == "expand":
            items[0].envelope.item["result"] += "x" * 100
        return items


def memory(modules, *, global_cap=5000, module_cap=4000, type_cap=None):
    configs = {name: ModuleConfig(enabled=True, max_context_tokens=module_cap) for name in modules}
    config = MemoryConfiguration(
        **configs,
        compatibility_legacy=ModuleConfig(enabled=False),
        context_budget=ContextBudgetConfig(
            total_tokens=global_cap,
            per_type_tokens={} if type_cap is None else {"episode": type_cap},
        ),
    )
    return MemoryOrchestrator(
        config, [MemoryModuleRegistration(name, lambda _, m=m: m) for name, m in modules.items()]
    )


def request(iteration=2):
    return MemoryContextRequest(
        request_id="request", run_id="run", query="", context={"iteration": iteration}
    )


@pytest.mark.parametrize("limiting", ["global", "module", "type"])
def test_residual_budget_preserves_other_selected_items(limiting):
    a, b = Contributor("episodic"), Contributor("lessons", enabled=False)
    a_size, b_size = _estimate_utf8_bytes(a.original), _estimate_utf8_bytes(b.original)
    caps = {"global_cap": 5000, "module_cap": 4000, "type_cap": 3000}
    if limiting == "global":
        caps["global_cap"] = a_size + b_size + 47
    if limiting == "module":
        caps["module_cap"] = a_size + 47
    if limiting == "type":
        caps["type_cap"] = a_size + b_size + 47
    runtime = memory({"episodic": a, "lessons": b}, **caps)
    result = asyncio.run(runtime.build_context(request()))
    assert a.allowances == [a_size + 47] and b.allowances == []
    assert [i.envelope.item_id for i in result.items] == ["episodic-item", "lessons-item"]
    assert runtime.last_context_diagnostics.used_estimated_tokens == a_size + b_size


@pytest.mark.parametrize("behavior", ["count", "identity", "trust", "score", "overflow"])
def test_invalid_materializer_output_rejected(behavior):
    runtime = memory({"episodic": Contributor("episodic", behavior=behavior)})
    with pytest.raises(MemoryPermanentError):
        asyncio.run(runtime.build_context(request()))


def test_source_failure_keeps_admitted_view_and_reports_warning():
    module = Contributor("episodic", behavior="failure")
    runtime = memory({"episodic": module})
    result = asyncio.run(runtime.build_context(request()))
    assert result.items[0].envelope.item["result"] == "public result"
    assert module.original.envelope.item["result"] == "public result"
    assert result.warnings == ["memory.materialization_failed.episodic.MemoryValidationError"]


def test_disabled_path_exact_and_enabled_accounting_recomputed():
    module = Contributor("episodic", enabled=False, behavior="expand")
    runtime = memory({"episodic": module})
    result = asyncio.run(runtime.build_context(request()))
    expected = module.original.model_copy(
        update={"estimated_tokens": _estimate_utf8_bytes(module.original)}
    )
    assert result.items == [expected] and result.warnings == [] and module.allowances == []
    module.context_materialization_enabled = True
    expanded = asyncio.run(runtime.build_context(request()))
    assert expanded.items[0].envelope.item["result"] == "public result" + "x" * 100
    assert runtime.last_context_diagnostics.used_estimated_tokens == _estimate_utf8_bytes(
        expanded.items[0]
    )
    assert (
        runtime.last_context_diagnostics.selection_evidence[0]["materialized_estimated_tokens"]
        == expanded.items[0].estimated_tokens
    )


def source_transition():
    return DefaultExperienceTransitionAssembler().assemble(
        TransitionAssemblyRequest(
            transition_id="public-document-1",
            run_id="run",
            iteration=1,
            occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
            trust_classification="external_untrusted",
            pre_state={},
            observation={"status": "ready"},
            action={"kind": "read_document"},
            result={"body": "Public archive evidence. " * 55, "revision": "revision-one"},
            terminal=False,
        )
    )


def test_real_non_sre_episode_expansion_and_default_parity():
    async def scenario():
        store = InMemoryStructuredStore()
        transition = source_transition()
        new = EpisodicMemory(store, namespace="public", module_version="1.2")
        old = EpisodicMemory(store, namespace="public", module_version="1.1")
        await new.record(transition, idempotency_key="write")
        before = (await old.retrieve(request())).items
        selected = (await new.retrieve(request())).items
        assert before[0].envelope.item == selected[0].envelope.item
        assert not old.context_materialization_enabled and new.context_materialization_enabled
        result = await new.materialize_selected(selected, request(), max_estimated_tokens=8000)
        assert result[0].envelope.item["result"] == canonical_json(transition.result)
        assert "characters omitted" in selected[0].envelope.item["result"]
        result[0].envelope.item["query_match"] = {"tampered": True}
        assert "tampered" not in canonical_json([i.model_dump(mode="json") for i in selected])
        for bad in [request(1), request(0)]:
            with pytest.raises(MemoryValidationError, match="precede use"):
                await new.materialize_selected(selected, bad, max_estimated_tokens=8000)
        corrupt = [selected[0].model_copy(deep=True)]
        corrupt[0].envelope.provenance[0].content_hash = "b" * 64
        with pytest.raises(MemoryValidationError, match="identity changed"):
            await new.materialize_selected(corrupt, request(), max_estimated_tokens=8000)

    asyncio.run(scenario())


def test_changed_stored_tail_cannot_reuse_old_result_provenance():
    async def scenario():
        original_store = InMemoryStructuredStore()
        original_module = EpisodicMemory(original_store, namespace="public", module_version="1.2")
        transition = source_transition()
        await original_module.record(transition, idempotency_key="original")
        selected = (await original_module.retrieve(request())).items
        changed_store = InMemoryStructuredStore()
        changed_module = EpisodicMemory(changed_store, namespace="public", module_version="1.2")
        changed = transition.model_copy(deep=True)
        changed.result["revision"] = "forged-later-tail"
        await changed_module.record(changed, idempotency_key="changed")
        with pytest.raises(MemoryValidationError, match="source leaf"):
            await changed_module.materialize_selected(
                selected, request(), max_estimated_tokens=8000
            )

    asyncio.run(scenario())


def test_logical_store_can_bind_an_admitted_physical_namespace():
    async def scenario():
        store = InMemoryStructuredStore()
        archived = EpisodicMemory(store, namespace="archive", module_version="1.2")
        await archived.record(source_transition(), idempotency_key="source")

        class LogicalStore:
            async def get(self, *, namespace, record_id):
                assert namespace == "active"
                return await store.get(namespace="archive", record_id=record_id)

            async def list(self, *, namespace):
                assert namespace == "active"
                return await store.list(namespace="archive")

        module = EpisodicMemory(LogicalStore(), namespace="active", module_version="1.2")
        selected = (await module.retrieve(request())).items
        result = await module.materialize_selected(selected, request(), max_estimated_tokens=8000)
        assert result[0].envelope.item["result"] == canonical_json(source_transition().result)
        assert result[0].envelope.provenance == selected[0].envelope.provenance

    asyncio.run(scenario())
