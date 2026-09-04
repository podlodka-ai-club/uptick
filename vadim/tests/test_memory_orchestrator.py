from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path

import pytest

from uptick_agent.memory.config import ContextBudgetConfig, MemoryConfiguration, ModuleConfig
from uptick_agent.memory.contracts import (
    ConsolidationDelta,
    ConsolidationRequest,
    ConsolidationResult,
    ContextItem,
    ExperienceTransition,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    MemoryTransientError,
    MemoryValidationError,
    ProvenanceRef,
    RunOutcome,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.orchestrator import MemoryModuleRegistration, MemoryOrchestrator

_HASH = "a" * 64


def _async_test(function):
    @wraps(function)
    def run() -> None:
        asyncio.run(function())

    return run


def _item(item_id: str, score: float, tokens: int) -> ContextItem:
    return ContextItem(
        envelope=UntrustedMemoryEnvelope(
            item_id=item_id,
            artefact_type="episode",
            origin_module="test",
            origin_version="1.0",
            trust_classification="external_untrusted",
            provenance=[ProvenanceRef(artefact_id="source", content_hash=_HASH)],
            item={"summary": item_id},
        ),
        score=score,
        selection_reason="test",
        estimated_tokens=tokens,
    )


def _verified_tokens(item: ContextItem, module_id: str) -> int:
    normalized = item.model_copy(
        update={
            "envelope": item.envelope.model_copy(
                update={"origin_module": module_id, "origin_version": "1.0"}
            )
        }
    )
    payload = normalized.model_dump(mode="json", exclude={"estimated_tokens"})
    rendered = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return len(rendered.encode("utf-8"))


def _config(**modules: ModuleConfig) -> MemoryConfiguration:
    defaults = {
        "compatibility_legacy": ModuleConfig(enabled=False),
        "episodic": ModuleConfig(enabled=False),
        "lessons": ModuleConfig(enabled=False),
        "tool_knowledge": ModuleConfig(enabled=False),
        "consolidation": ModuleConfig(enabled=False),
    }
    defaults.update(modules)
    return MemoryConfiguration(
        **defaults,
        context_budget=ContextBudgetConfig(total_items=2, total_tokens=10_000),
    )


@dataclass
class _Contributor:
    module_id: str
    items: list[ContextItem] = field(default_factory=list)
    reads: int = 0

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        self.reads += 1
        items = [
            item.model_copy(
                update={
                    "envelope": item.envelope.model_copy(
                        update={"origin_module": self.module_id, "origin_version": "1.0"}
                    )
                }
            )
            for item in self.items
        ]
        return MemoryContribution(module_id=self.module_id, module_version="1.0", items=items)


@dataclass
class _Sink:
    writes: list[str] = field(default_factory=list)

    async def record(self, transition: ExperienceTransition, *, idempotency_key: str) -> None:
        self.writes.append(idempotency_key)


class _BrokenContributor:
    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        raise MemoryTransientError("unavailable")


@dataclass
class _Finalizer:
    keys: list[str] = field(default_factory=list)

    async def finalize(self, outcome: RunOutcome, *, idempotency_key: str) -> None:
        self.keys.append(idempotency_key)


class _Consolidator:
    async def consolidate(self, request: ConsolidationRequest) -> ConsolidationResult:
        return ConsolidationResult(
            request_id=request.request_id,
            snapshot_id=request.snapshot_id,
            applied=True,
            deltas=[
                ConsolidationDelta(
                    artefact_type="lesson",
                    operation="create",
                    payload={"summary": "stable"},
                )
            ],
        )


@_async_test
async def test_disabled_modules_are_not_constructed_or_dispatched() -> None:
    calls = 0

    def disabled_factory(config: ModuleConfig) -> object:
        nonlocal calls
        calls += 1
        return _Contributor("episodic")

    orchestrator = MemoryOrchestrator(
        _config(),
        [MemoryModuleRegistration("episodic", disabled_factory)],
    )

    context = await orchestrator.build_context(MemoryContextRequest(request_id="r", run_id="run"))
    await orchestrator.record_transition(
        ExperienceTransition(
            transition_id="transition",
            run_id="run",
            iteration=1,
            trust_classification="external_untrusted",
            provenance=[ProvenanceRef(artefact_id="source", content_hash=_HASH)],
            terminal=False,
        )
    )
    await orchestrator.finalize_run(
        RunOutcome(run_id="run", status="completed", stop_reason="done")
    )
    result = await orchestrator.consolidate(
        ConsolidationRequest(request_id="c", snapshot_id="snapshot", idempotency_key="key")
    )

    assert calls == 0
    assert context.items == []
    assert result.applied is False


def test_enabled_modules_need_a_registration_and_registered_dependencies() -> None:
    with pytest.raises(MemoryValidationError, match="no registration"):
        MemoryOrchestrator(_config(episodic=ModuleConfig(enabled=True)), [])

    with pytest.raises(MemoryValidationError, match="requires disabled modules"):
        MemoryOrchestrator(
            _config(episodic=ModuleConfig(enabled=True)),
            [
                MemoryModuleRegistration(
                    "episodic", lambda _: _Contributor("episodic"), requires=("lessons",)
                )
            ],
        )

    with pytest.raises(MemoryValidationError, match="unknown memory module"):
        MemoryOrchestrator(
            _config(),
            [MemoryModuleRegistration("unknown", lambda _: object())],
        )

    unknown_estimator = _config().model_copy(
        update={
            "context_budget": ContextBudgetConfig(
                estimator_id="not-installed",
                estimator_version="1.0",
            )
        }
    )
    with pytest.raises(MemoryValidationError, match="estimator is unavailable"):
        MemoryOrchestrator(unknown_estimator, [])


@_async_test
async def test_context_merge_is_deterministic_deduplicated_and_hard_bounded() -> None:
    episodic = _Contributor("episodic", [_item("a", 0.9, 2), _item("b", 0.1, 2)])
    lessons = _Contributor("lessons", [_item("a", 0.5, 1), _item("c", 0.8, 2)])
    config = _config(
        episodic=ModuleConfig(enabled=True, max_context_items=1, max_context_tokens=10_000),
        lessons=ModuleConfig(enabled=True),
        tool_knowledge=ModuleConfig(enabled=True),
    )
    orchestrator = MemoryOrchestrator(
        config,
        [
            MemoryModuleRegistration("lessons", lambda _: lessons),
            MemoryModuleRegistration("episodic", lambda _: episodic),
            MemoryModuleRegistration("tool_knowledge", lambda _: _BrokenContributor()),
        ],
    )

    context = await orchestrator.build_context(MemoryContextRequest(request_id="r", run_id="run"))
    diagnostics = orchestrator.last_context_diagnostics

    assert [item.envelope.item_id for item in context.items] == ["a", "c"]
    assert sum(item.estimated_tokens for item in context.items) <= 10_000
    assert diagnostics.configuration_fingerprint == config.fingerprint
    assert diagnostics.resolved_configuration == config.model_dump(mode="json")
    assert diagnostics.contributors == ["episodic", "lessons"]
    assert "episodic:b:module_item_limit" in diagnostics.truncations
    assert "lessons:a:duplicate" in diagnostics.truncations
    assert diagnostics.selection_evidence[0]["outcome"] == "selected"
    assert diagnostics.selection_evidence[0]["score"] == 0.9
    assert diagnostics.selection_evidence[0]["selection_reason"] == "test"
    assert context.warnings == ["memory.module_failed.tool_knowledge.MemoryTransientError"]


@_async_test
async def test_orchestrator_recomputes_tokens_and_enforces_request_and_type_caps() -> None:
    first = _item("a", 1, 999_999)
    second = _item("b", 0.5, 999_999)
    first_tokens = _verified_tokens(first, "episodic")
    second_tokens = _verified_tokens(second, "episodic")
    contributor = _Contributor("episodic", [first, second])
    configuration = _config(episodic=ModuleConfig(enabled=True)).model_copy(
        update={
            "context_budget": ContextBudgetConfig(
                total_items=2,
                total_tokens=first_tokens + second_tokens,
                per_type_tokens={"episode": first_tokens},
            )
        }
    )
    orchestrator = MemoryOrchestrator(
        configuration,
        [MemoryModuleRegistration("episodic", lambda _: contributor)],
    )

    context = await orchestrator.build_context(
        MemoryContextRequest(
            request_id="bounded",
            run_id="run",
            max_items=2,
            max_estimated_tokens=first_tokens + second_tokens,
        )
    )

    assert [item.envelope.item_id for item in context.items] == ["a"]
    assert context.items[0].estimated_tokens == first_tokens
    diagnostics = orchestrator.last_context_diagnostics
    assert diagnostics.effective_token_limit == first_tokens + second_tokens
    assert diagnostics.used_estimated_tokens == first_tokens
    assert diagnostics.selection_evidence[0]["estimated_tokens"] == first_tokens
    assert diagnostics.selection_evidence[1]["estimated_tokens"] == second_tokens
    assert "episodic:b:type_token_limit" in diagnostics.truncations


@_async_test
async def test_default_estimator_does_not_trust_a_zero_module_estimate() -> None:
    contributor = _Contributor("episodic", [_item("large", 1, 0)])
    configuration = _config(episodic=ModuleConfig(enabled=True)).model_copy(
        update={"context_budget": ContextBudgetConfig(total_items=1, total_tokens=1)}
    )
    orchestrator = MemoryOrchestrator(
        configuration,
        [MemoryModuleRegistration("episodic", lambda _: contributor)],
    )

    context = await orchestrator.build_context(
        MemoryContextRequest(request_id="bounded", run_id="run")
    )

    assert context.items == []
    assert "episodic:large:global_token_limit" in orchestrator.last_context_diagnostics.truncations


@_async_test
async def test_equal_score_order_is_independent_of_registration_order() -> None:
    configuration = _config(
        episodic=ModuleConfig(enabled=True),
        lessons=ModuleConfig(enabled=True),
    )

    async def selected(registrations: list[MemoryModuleRegistration]):
        orchestrator = MemoryOrchestrator(configuration, registrations)
        context = await orchestrator.build_context(
            MemoryContextRequest(request_id="tie", run_id="run", max_items=1)
        )
        return (
            [item.envelope.item_id for item in context.items],
            orchestrator.last_context_diagnostics.selection_evidence,
        )

    lessons_first = await selected(
        [
            MemoryModuleRegistration(
                "lessons", lambda _: _Contributor("lessons", [_item("l", 1, 1)])
            ),
            MemoryModuleRegistration(
                "episodic", lambda _: _Contributor("episodic", [_item("e", 1, 1)])
            ),
        ]
    )
    episodic_first = await selected(
        [
            MemoryModuleRegistration(
                "episodic", lambda _: _Contributor("episodic", [_item("e", 1, 1)])
            ),
            MemoryModuleRegistration(
                "lessons", lambda _: _Contributor("lessons", [_item("l", 1, 1)])
            ),
        ]
    )

    assert lessons_first == episodic_first
    assert lessons_first[0] == ["e"]


@_async_test
async def test_permanent_contribution_errors_are_not_silently_downgraded() -> None:
    orchestrator = MemoryOrchestrator(
        _config(episodic=ModuleConfig(enabled=True)),
        [
            MemoryModuleRegistration(
                "episodic", lambda _: _Contributor("lessons", [_item("wrong", 1, 1)])
            )
        ],
    )

    with pytest.raises(MemoryPermanentError, match="another module"):
        await orchestrator.build_context(MemoryContextRequest(request_id="r", run_id="run"))


@_async_test
async def test_zero_global_budget_does_not_call_an_enabled_contributor() -> None:
    contributor = _Contributor("episodic", [_item("a", 1, 1)])
    config = _config(
        episodic=ModuleConfig(enabled=True),
    ).model_copy(update={"context_budget": ContextBudgetConfig(total_items=0, total_tokens=4)})
    orchestrator = MemoryOrchestrator(
        config, [MemoryModuleRegistration("episodic", lambda _: contributor)]
    )

    context = await orchestrator.build_context(MemoryContextRequest(request_id="r", run_id="run"))

    assert context.items == []
    assert contributor.reads == 0


@_async_test
async def test_record_dispatches_only_the_experience_sink_contract() -> None:
    sink = _Sink()
    orchestrator = MemoryOrchestrator(
        _config(episodic=ModuleConfig(enabled=True)),
        [MemoryModuleRegistration("episodic", lambda _: sink)],
    )
    transition = ExperienceTransition(
        transition_id="transition",
        run_id="run",
        iteration=1,
        trust_classification="external_untrusted",
        provenance=[ProvenanceRef(artefact_id="source", content_hash=_HASH)],
        terminal=False,
    )

    await orchestrator.record_transition(transition)

    assert len(sink.writes) == 1
    assert sink.writes[0].startswith("record:episodic:")


@_async_test
async def test_default_modules_are_verified_before_construction_and_finalization() -> None:
    constructions = 0
    finalizer = _Finalizer()

    def factory(_: ModuleConfig) -> _Finalizer:
        nonlocal constructions
        constructions += 1
        return finalizer

    configuration = _config(
        episodic=ModuleConfig(
            enabled=True,
            status="default",
            approval_record_id="approval-1",
            version="2.0",
        )
    )
    registration = MemoryModuleRegistration("episodic", factory)

    with pytest.raises(MemoryValidationError, match="approval verifier"):
        MemoryOrchestrator(configuration, [registration])
    assert constructions == 0

    with pytest.raises(MemoryValidationError, match="approval record is invalid"):
        MemoryOrchestrator(
            configuration,
            [registration],
            approval_verifier=lambda _, module, fingerprint: (
                module.version == "1.0" and fingerprint == configuration.fingerprint
            ),
        )
    assert constructions == 0

    with pytest.raises(MemoryValidationError, match="approval record is invalid"):
        MemoryOrchestrator(
            configuration,
            [registration],
            approval_verifier=lambda _, module, fingerprint: (
                module.version == configuration.modules["episodic"].version
                and fingerprint == "0" * 64
            ),
        )
    assert constructions == 0

    orchestrator = MemoryOrchestrator(
        configuration,
        [registration],
        approval_verifier=lambda module_id, module, fingerprint: (
            module_id == "episodic" and fingerprint == configuration.fingerprint
        ),
    )
    await orchestrator.finalize_run(
        RunOutcome(run_id="run", status="completed", stop_reason="done")
    )

    assert constructions == 1
    assert finalizer.keys[0].startswith("finalize:episodic:")
    assert orchestrator.last_context_diagnostics.resolved_configuration == configuration.model_dump(
        mode="json"
    )


@_async_test
async def test_enabled_consolidation_uses_only_registered_participants() -> None:
    orchestrator = MemoryOrchestrator(
        _config(consolidation=ModuleConfig(enabled=True)),
        [MemoryModuleRegistration("consolidation", lambda _: _Consolidator())],
    )
    request = ConsolidationRequest(
        request_id="c",
        snapshot_id="snapshot",
        idempotency_key="key",
    )

    result = await orchestrator.consolidate(request)

    assert result.applied is True
    assert [delta.artefact_type for delta in result.deltas] == ["lesson"]


def test_stage_three_orchestrator_keeps_environment_and_provider_imports_out() -> None:
    source = (Path(__file__).parents[1] / "src/uptick_agent/memory/orchestrator.py").read_text()
    imports = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    forbidden = ("uptick_agent.simulator", "uptick_agent.llm")
    assert not any(name.startswith(forbidden) for name in imports)
