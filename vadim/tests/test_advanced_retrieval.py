from __future__ import annotations

from dataclasses import dataclass

import pytest

from uptick_agent.memory.config import MemoryConfiguration, ModuleConfig
from uptick_agent.memory.contracts import (
    ContextItem,
    CreatedMemoryItem,
    ExperienceTransition,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    ProvenanceRef,
    RunOutcome,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.orchestrator import MemoryModuleRegistration, MemoryOrchestrator
from uptick_agent.memory.retrieval import (
    AdvancedRetrievalSettings,
    AdvancedRetrievalStrategy,
    ChainedRetrievalStrategy,
    StructuredFeature,
)

_HASH = "a" * 64


def _item(
    item_id: str,
    score: float,
    payload: dict[str, object],
    *,
    group: str | None = None,
    trust: str = "external_untrusted",
    content_hash: str = _HASH,
    estimated_tokens: int = 1,
) -> ContextItem:
    item = dict(payload)
    if group is not None:
        item["group"] = group
    return ContextItem(
        envelope=UntrustedMemoryEnvelope(
            item_id=item_id,
            artefact_type="toy-record",
            origin_module="toy",
            origin_version="7.2",
            trust_classification=trust,
            provenance=[
                ProvenanceRef(
                    artefact_id=f"source-{item_id}",
                    content_hash=content_hash,
                )
            ],
            item=item,
        ),
        score=score,
        selection_reason="source score",
        estimated_tokens=estimated_tokens,
    )


def _request(**kwargs: object) -> MemoryContextRequest:
    return MemoryContextRequest(request_id="request", run_id="current", **kwargs)


def test_disabled_strategy_is_a_no_op_and_enabled_changes_ranking() -> None:
    candidates = [
        _item("plain", 0.5, {"text": "unrelated"}),
        _item("match", 0.5, {"text": "backup outage"}),
    ]
    baseline = AdvancedRetrievalStrategy(AdvancedRetrievalSettings(enabled=False)).rank(
        candidates, _request(query="outage")
    )
    assert baseline == candidates

    ranked = AdvancedRetrievalStrategy(AdvancedRetrievalSettings()).rank(
        candidates,
        _request(query="outage"),
    )
    assert [item.envelope.item_id for item in ranked] == ["match", "plain"]


def test_structured_features_are_explicit_and_can_filter_candidates() -> None:
    candidates = [
        _item("warehouse", 0.2, {"kind": "ticket", "queue": "warehouse"}),
        _item("billing", 0.9, {"kind": "ticket", "queue": "billing"}),
    ]
    strategy = AdvancedRetrievalStrategy(
        AdvancedRetrievalSettings(
            lexical_weight=0,
            structured_features=(
                StructuredFeature(
                    request_path="context.queue",
                    candidate_path="item.queue",
                    weight=2,
                    required=True,
                ),
            ),
        )
    )
    result = strategy.rank(candidates, _request(context={"queue": "warehouse"}))
    assert [item.envelope.item_id for item in result] == ["warehouse"]
    assert result[0].score == 2.2
    assert "structured=context.queue=item.queue" in result[0].selection_reason


def test_structured_overlap_and_generic_toy_environment_data() -> None:
    # These records intentionally use a second, unrelated toy domain.  The
    # retrieval strategy only sees the explicitly named generic fields.
    candidates = [
        _item("recipe", 0.1, {"tags": ["vegan", "quick"], "source": "cookbook"}),
        _item("repair", 0.8, {"tags": ["electrical", "urgent"], "source": "workshop"}),
    ]
    strategy = AdvancedRetrievalStrategy(
        AdvancedRetrievalSettings(
            lexical_weight=0,
            structured_features=(
                StructuredFeature(
                    request_path="context.tags",
                    candidate_path="item.tags",
                    operator="overlap",
                    weight=1.5,
                ),
            ),
        )
    )
    result = strategy.rank(candidates, _request(context={"tags": ["urgent", "safety"]}))
    assert [item.envelope.item_id for item in result] == ["repair", "recipe"]
    assert result[0].score == 2.3


def test_equal_scores_and_duplicate_ids_have_deterministic_winners() -> None:
    first = _item("same", 0.5, {"text": "one"}, trust="external_untrusted")
    second = _item(
        "same",
        0.5,
        {"text": "two"},
        trust="human_attested",
        content_hash="b" * 64,
    )
    alpha = _item("alpha", 0.5, {"text": "same"})
    beta = _item("beta", 0.5, {"text": "same"})
    strategy = AdvancedRetrievalStrategy(AdvancedRetrievalSettings(lexical_weight=0, max_items=3))
    result = strategy.rank([beta, second, alpha, first], _request())
    assert [item.envelope.item_id for item in result] == ["alpha", "beta", "same"]
    # The duplicate winner is selected by the stable provenance/tie fields;
    # its envelope is copied unchanged, including trust and provenance.
    assert result[-1].envelope.trust_classification == "external_untrusted"
    assert result[-1].envelope.provenance == first.envelope.provenance
    repeat = strategy.rank([first, alpha, second, beta], _request())
    assert [item.envelope.item_id for item in repeat] == [item.envelope.item_id for item in result]
    assert repeat[-1].envelope.provenance == result[-1].envelope.provenance


def test_diversity_penalty_selects_distinct_groups_before_repeating_one() -> None:
    candidates = [
        _item("a1", 1.0, {"text": "x"}, group="a"),
        _item("a2", 0.95, {"text": "x"}, group="a"),
        _item("b1", 0.8, {"text": "x"}, group="b"),
    ]
    strategy = AdvancedRetrievalStrategy(
        AdvancedRetrievalSettings(
            lexical_weight=0,
            diversity_path="item.group",
            diversity_penalty=0.25,
            max_items=3,
        )
    )
    result = strategy.rank(candidates, _request())
    assert [item.envelope.item_id for item in result] == ["a1", "b1", "a2"]
    assert "diversity_adjustment=-0.25" in result[-1].selection_reason


def test_missing_diversity_path_does_not_penalize_unrelated_candidates() -> None:
    candidates = [
        _item("top", 1.0, {"text": "x"}),
        _item("next", 0.9, {"text": "x"}),
    ]
    result = AdvancedRetrievalStrategy(
        AdvancedRetrievalSettings(
            lexical_weight=0,
            diversity_penalty=0.25,
            max_items=2,
        )
    ).rank(candidates, _request())
    assert [item.envelope.item_id for item in result] == ["top", "next"]
    assert "diversity_adjustment" not in result[1].selection_reason


def test_strategy_obeys_request_item_and_token_bounds_without_changing_metadata() -> None:
    candidates = [
        _item("large", 1.0, {"text": "top"}, estimated_tokens=4),
        _item("medium", 0.9, {"text": "next"}, estimated_tokens=4),
        _item("small", 0.8, {"text": "fits"}, estimated_tokens=1),
    ]
    request = _request(max_items=2, max_estimated_tokens=5)
    strategy = AdvancedRetrievalStrategy(AdvancedRetrievalSettings(lexical_weight=0))
    result = strategy.rank(candidates, request)
    assert [item.envelope.item_id for item in result] == ["large", "small"]
    assert sum(item.estimated_tokens for item in result) <= request.max_estimated_tokens
    assert result[0].envelope.origin_module == candidates[0].envelope.origin_module
    assert result[0].envelope.origin_version == candidates[0].envelope.origin_version
    assert result[0].envelope.trust_classification == candidates[0].envelope.trust_classification
    assert result[0].envelope.provenance == candidates[0].envelope.provenance


def test_declared_chain_composes_sync_and_async_read_side_strategies() -> None:
    candidates = [
        _item("first", 1.0, {"text": "first"}),
        _item("second", 0.5, {"text": "second"}),
    ]

    class _AsyncKeepFirst:
        async def rank(self, values, request):
            return list(values)[:1]

    result = _run(
        ChainedRetrievalStrategy(
            AdvancedRetrievalStrategy(AdvancedRetrievalSettings(lexical_weight=0)),
            _AsyncKeepFirst(),
        ).rank(candidates, _request())
    )
    assert [item.envelope.item_id for item in result] == ["first"]


def test_orchestrator_applies_strategy_without_dropping_module_lifecycle() -> None:
    module = _LifecycleContributor(
        MemoryContribution(
            module_id="episodic",
            module_version="1.0",
            items=[
                _item("first", 0.1, {"text": "one"}).model_copy(
                    update={
                        "envelope": _item("first", 0.1, {"text": "one"}).envelope.model_copy(
                            update={"origin_module": "episodic", "origin_version": "1.0"}
                        )
                    }
                ),
                _item("second", 0.2, {"text": "two"}).model_copy(
                    update={
                        "envelope": _item("second", 0.2, {"text": "two"}).envelope.model_copy(
                            update={"origin_module": "episodic", "origin_version": "1.0"}
                        )
                    }
                ),
            ],
        )
    )
    strategy = _ReverseStrategy()
    configuration = MemoryConfiguration(
        compatibility_legacy=ModuleConfig(enabled=False),
        episodic=ModuleConfig(enabled=True),
    )
    orchestrator = MemoryOrchestrator(
        configuration,
        [
            MemoryModuleRegistration(
                "episodic",
                lambda _: module,
                retrieval_strategy=strategy,
            )
        ],
    )

    context = _run(orchestrator.build_context(_request()))
    assert [item.envelope.item_id for item in context.items] == ["second", "first"]
    assert strategy.calls == 1
    _run(orchestrator.record_transition(_transition()))
    _run(
        orchestrator.finalize_run(
            RunOutcome(run_id="current", status="completed", stop_reason="done")
        )
    )
    assert module.writes == 1
    assert module.finalized == 1


def test_orchestrator_with_no_strategy_makes_no_retrieval_strategy_call() -> None:
    module = _LifecycleContributor(
        MemoryContribution(
            module_id="episodic",
            module_version="1.0",
            items=[
                _item("plain", 0.5, {"text": "plain"}).model_copy(
                    update={
                        "envelope": _item("plain", 0.5, {"text": "plain"}).envelope.model_copy(
                            update={"origin_module": "episodic", "origin_version": "1.0"}
                        )
                    }
                )
            ],
        )
    )
    strategy = _ReverseStrategy()
    configuration = MemoryConfiguration(
        compatibility_legacy=ModuleConfig(enabled=False),
        episodic=ModuleConfig(enabled=True),
    )
    orchestrator = MemoryOrchestrator(
        configuration,
        [MemoryModuleRegistration("episodic", lambda _: module)],
    )
    _run(orchestrator.build_context(_request()))
    assert strategy.calls == 0


def test_orchestrator_rejects_strategy_that_mutates_an_admitted_envelope() -> None:
    original = _item("plain", 0.5, {"text": "plain"})
    module = _LifecycleContributor(
        MemoryContribution(
            module_id="episodic",
            module_version="1.0",
            items=[
                original.model_copy(
                    update={
                        "envelope": original.envelope.model_copy(
                            update={"origin_module": "episodic", "origin_version": "1.0"}
                        )
                    }
                )
            ],
        )
    )
    configuration = MemoryConfiguration(
        compatibility_legacy=ModuleConfig(enabled=False),
        episodic=ModuleConfig(enabled=True),
    )
    orchestrator = MemoryOrchestrator(
        configuration,
        [
            MemoryModuleRegistration(
                "episodic",
                lambda _: module,
                retrieval_strategy=_MutatingStrategy(),
            )
        ],
    )

    with pytest.raises(MemoryPermanentError, match="changed or invented an envelope"):
        _run(orchestrator.build_context(_request()))


def test_orchestrator_supplies_authoritative_estimates_before_strategy_caps() -> None:
    item = _item("large", 1.0, {"text": "x" * 120}, estimated_tokens=0).model_copy(
        update={
            "envelope": _item("large", 1.0, {"text": "x" * 120}).envelope.model_copy(
                update={"origin_module": "episodic", "origin_version": "1.0"}
            )
        }
    )
    module = _LifecycleContributor(
        MemoryContribution(module_id="episodic", module_version="1.0", items=[item])
    )
    strategy = AdvancedRetrievalStrategy(
        AdvancedRetrievalSettings(lexical_weight=0, max_estimated_tokens=1)
    )
    orchestrator = MemoryOrchestrator(
        MemoryConfiguration(
            compatibility_legacy=ModuleConfig(enabled=False),
            episodic=ModuleConfig(enabled=True),
        ),
        [MemoryModuleRegistration("episodic", lambda _: module, retrieval_strategy=strategy)],
    )
    context = _run(orchestrator.build_context(_request(max_estimated_tokens=10_000)))
    assert context.items == []


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)


@dataclass
class _ReverseStrategy:
    calls: int = 0

    def rank(self, candidates, request):
        self.calls += 1
        return list(reversed(list(candidates)))


@dataclass
class _MutatingStrategy:
    def rank(self, candidates, request):
        candidates[0].envelope.item["forged"] = "unadmitted"
        return list(candidates)


@dataclass
class _LifecycleContributor:
    contribution: MemoryContribution
    writes: int = 0
    finalized: int = 0

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        return self.contribution

    async def record(
        self, transition: ExperienceTransition, *, idempotency_key: str
    ) -> list[CreatedMemoryItem] | None:
        self.writes += 1
        return None

    async def finalize(self, outcome: RunOutcome, *, idempotency_key: str) -> None:
        self.finalized += 1


def _transition() -> ExperienceTransition:
    return ExperienceTransition(
        transition_id="transition",
        run_id="current",
        iteration=1,
        trust_classification="external_untrusted",
        provenance=[ProvenanceRef(artefact_id="transition", content_hash=_HASH)],
        terminal=False,
    )
