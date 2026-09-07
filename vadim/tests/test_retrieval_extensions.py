from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.integrations.retrieval.fastembed import FastEmbedPort
from uptick_agent.integrations.retrieval.reasoned_query import (
    QueryReformulation,
    ReasonedQueryReformulator,
)
from uptick_agent.llm.contracts import (
    GenerationSettings,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)
from uptick_agent.memory.config import MemoryConfiguration, ModuleConfig, RetrievalConfig
from uptick_agent.memory.contracts import (
    ContextItem,
    MemoryContextRequest,
    MemoryValidationError,
    ProvenanceRef,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.retrieval_extensions import (
    SemanticRetrievalSettings,
    SemanticRetrievalStrategy,
)
from uptick_agent.memory.stores import InMemoryStructuredStore


def _item(
    item_id: str,
    payload: dict[str, Any],
    *,
    evidence: tuple[str, str] = ("evidence", "a" * 64),
    tokens: int = 4,
) -> ContextItem:
    artefact_id, content_hash = evidence
    return ContextItem(
        envelope=UntrustedMemoryEnvelope(
            item_id=item_id,
            artefact_type="episode",
            origin_module="fixture",
            origin_version="1.0",
            trust_classification="external_untrusted",
            provenance=[
                ProvenanceRef(
                    artefact_id=artefact_id,
                    relation="source",
                    content_hash=content_hash,
                )
            ],
            item=payload,
        ),
        score=0.0,
        selection_reason="contributor score",
        estimated_tokens=tokens,
    )


def _request(**kwargs: Any) -> MemoryContextRequest:
    return MemoryContextRequest(request_id="request", run_id="run", query="needle", **kwargs)


class FakeEmbeddings:
    dimension = 2

    def __init__(self, vectors: dict[str, tuple[float, float]]) -> None:
        self.vectors = vectors
        self.queries: list[str] = []
        self.passages: list[list[str]] = []

    def embed_query(self, text: str) -> tuple[float, float]:
        self.queries.append(text)
        return self.vectors["query"]

    def embed_passages(self, texts: list[str]) -> list[tuple[float, float]]:
        self.passages.append(texts)
        import json

        return [self.vectors[json.loads(text)["label"]] for text in texts]


def _rank(strategy: SemanticRetrievalStrategy, items: list[ContextItem], **kwargs: Any):
    return asyncio.run(strategy.rank(items, _request(**kwargs)))


def test_cosine_ranking_is_deterministic_and_preserves_untrusted_envelope() -> None:
    items = [
        _item("far", {"label": "far", "fact": "other"}),
        _item("near", {"label": "near", "fact": "matching"}),
    ]
    strategy = SemanticRetrievalStrategy(
        FakeEmbeddings({"query": (1.0, 0.0), "near": (0.9, 0.1), "far": (0.0, 1.0)})
    )

    ranked = _rank(strategy, items)
    reversed_ranked = _rank(strategy, list(reversed(items)))

    assert [item.envelope.item_id for item in ranked] == ["near", "far"]
    assert [item.envelope.item_id for item in reversed_ranked] == ["near", "far"]
    assert ranked[0].envelope == items[1].envelope
    assert ranked[0].envelope.trust_classification == "external_untrusted"
    assert ranked[0].score > ranked[1].score
    assert "semantic=" in ranked[0].selection_reason


def test_embedding_validation_rejects_bad_dimension_nonfinite_and_zero_vectors() -> None:
    item = _item("one", {"label": "one"})
    for query, passage, message in (
        ((1.0, 0.0, 1.0), (1.0, 0.0), "dimension"),
        ((1.0, 0.0), (float("nan"), 0.0), "non-finite"),
        ((1.0, 0.0), (0.0, 0.0), "non-zero"),
    ):
        strategy = SemanticRetrievalStrategy(
            FakeEmbeddings({"query": query, "one": passage})  # type: ignore[arg-type]
        )
        with pytest.raises(MemoryValidationError, match=message):
            _rank(strategy, [item])


def test_zero_budget_short_circuits_providers_and_hard_caps_items_and_tokens() -> None:
    embeddings = FakeEmbeddings({"query": (1.0, 0.0), "one": (1.0, 0.0), "two": (0.9, 0.1)})
    items = [_item("one", {"label": "one"}, tokens=3), _item("two", {"label": "two"}, tokens=3)]
    strategy = SemanticRetrievalStrategy(
        embeddings,
        settings=SemanticRetrievalSettings(max_items=1, max_estimated_tokens=3),
    )

    assert _rank(strategy, items, max_items=0) == []
    assert embeddings.queries == []
    selected = _rank(strategy, items, max_items=2, max_estimated_tokens=3)
    assert [item.envelope.item_id for item in selected] == ["one"]


def test_graph_scores_relevant_shared_evidence_with_total_neighbor_budget() -> None:
    evidence_b = ("evidence-b", "b" * 64)
    items = [
        _item("seed", {"label": "seed"}, evidence=("evidence-a", "a" * 64)),
        _item("middle", {"label": "middle"}, evidence=("evidence-a", "a" * 64)),
        _item("tail", {"label": "tail"}, evidence=evidence_b),
    ]
    # The middle and tail share a second evidence key, forming a two-hop path
    # from the semantic seed while keeping the edge metadata authoritative.
    items[1] = items[1].model_copy(
        update={
            "envelope": items[1].envelope.model_copy(
                update={
                    "provenance": [
                        *items[1].envelope.provenance,
                        ProvenanceRef(
                            artefact_id=evidence_b[0],
                            relation="supports",
                            content_hash=evidence_b[1],
                        ),
                    ]
                }
            )
        }
    )
    strategy = SemanticRetrievalStrategy(
        FakeEmbeddings(
            {
                "query": (1.0, 0.0),
                "seed": (1.0, 0.0),
                "middle": (0.0, 1.0),
                "tail": (0.0, 1.0),
            }
        ),
        settings=SemanticRetrievalSettings(graph_weight=1.0, graph_depth=2, graph_max_neighbors=1),
    )

    ranked = _rank(strategy, items)
    by_id = {item.envelope.item_id: item for item in ranked}
    assert "graph=0.5" in by_id["middle"].selection_reason
    assert "graph=0" in by_id["tail"].selection_reason
    # A second identical artefact ID with a different hash is not an edge.
    forged = _item("forged", {"label": "tail"}, evidence=(evidence_b[0], "c" * 64))
    forged_ranked = _rank(strategy, [items[0], items[1], forged])
    forged_by_id = {item.envelope.item_id: item for item in forged_ranked}
    assert "graph=0" in forged_by_id["forged"].selection_reason


def test_confidence_recency_and_owned_reasoned_request_are_explicit_signals() -> None:
    items = [
        _item("old", {"label": "old", "confidence": 0.1, "observed_at": "2026-09-01T00:00:00Z"}),
        _item("new", {"label": "new", "confidence": 0.9, "observed_at": "2026-09-05T00:00:00Z"}),
    ]

    class Rewriter:
        seen: MemoryContextRequest | None = None

        def rewrite(self, request: MemoryContextRequest) -> str:
            self.seen = request
            request.context["mutated_by_provider"] = True
            return "rewritten"

    rewriter = Rewriter()
    embeddings = FakeEmbeddings({"query": (1.0, 0.0), "old": (0.0, 1.0), "new": (0.0, 1.0)})
    strategy = SemanticRetrievalStrategy(
        embeddings,
        reasoned_query=rewriter,
        settings=SemanticRetrievalSettings(
            semantic_weight=0.0,
            confidence_path="item.confidence",
            confidence_weight=1.0,
            recency_path="item.observed_at",
            recency_weight=1.0,
            recency_reference_time=datetime(2026, 9, 5, tzinfo=UTC),
            recency_half_life_seconds=86_400,
        ),
    )
    request = _request(context={"public": "value"})

    ranked = asyncio.run(strategy.rank(items, request))

    assert [item.envelope.item_id for item in ranked] == ["new", "old"]
    assert embeddings.queries == ["rewritten"]
    assert rewriter.seen is not request
    assert "mutated_by_provider" not in request.context


def test_recency_half_life_is_explicitly_a_half_life() -> None:
    item = _item("half", {"label": "half", "observed_at": "2026-09-04T00:00:00Z"})
    strategy = SemanticRetrievalStrategy(
        FakeEmbeddings({"query": (1.0, 0.0), "half": (1.0, 0.0)}),
        settings=SemanticRetrievalSettings(
            recency_path="item.observed_at",
            recency_weight=1.0,
            recency_reference_time=datetime(2026, 9, 5, tzinfo=UTC),
            recency_half_life_seconds=86_400,
        ),
    )

    ranked = _rank(strategy, [item])

    assert "recency=0.5" in ranked[0].selection_reason


def test_reasoned_reformulator_exposes_structured_trace_without_extra_context() -> None:
    class Client:
        last_telemetry = None

        async def generate_structured(self, request: StructuredGenerationRequest[Any]):
            self.request = request
            return StructuredGenerationResult(
                value=QueryReformulation(query="public rewritten query"),
                provider="test",
                model=request.model,
            )

    client = Client()
    reformulator = ReasonedQueryReformulator(
        client, model="query-model", settings=GenerationSettings(reasoning_effort="low")
    )
    request = MemoryContextRequest(
        request_id="request",
        run_id="run",
        query="Search this failure: Authorization: Bearer fixture-private-value",
        context={"public": "only", "password": "fixture-password"},
    )

    assert asyncio.run(reformulator.rewrite(request)) == "public rewritten query"
    trace = reformulator.last_request_trace
    assert trace is not None
    assert trace["response_model"]["qualname"] == "QueryReformulation"
    assert "public" in client.request.messages[1].content
    assert "fixture-private-value" not in client.request.messages[1].content
    assert "fixture-password" not in client.request.messages[1].content
    assert "fixture-password" not in str(trace)
    assert request.context["password"] == "fixture-password"
    assert reformulator.last_result == QueryReformulation(query="public rewritten query")


def test_fastembed_adapter_is_local_and_normalizes_numpy_like_scalars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()

    class FakeTextEmbedding:
        calls: list[dict[str, object]] = []

        def __init__(self, **kwargs: object) -> None:
            self.calls.append(kwargs)

        def query_embed(self, _texts: list[str]):
            return [[1, 2]]

        def passage_embed(self, texts: list[str]):
            return [[index, index + 1] for index, _ in enumerate(texts, start=1)]

    monkeypatch.setitem(
        sys.modules,
        "fastembed",
        types.SimpleNamespace(TextEmbedding=FakeTextEmbedding),
    )
    port = FastEmbedPort(model_name="local-model", specific_model_path=model_path)

    assert port.embed_query("query") == [1.0, 2.0]
    assert port.embed_passages(["a", "b"]) == [[1.0, 2.0], [2.0, 3.0]]
    assert FakeTextEmbedding.calls == [
        {
            "model_name": "local-model",
            "specific_model_path": str(model_path),
            "local_files_only": True,
        }
    ]
    with pytest.raises(MemoryValidationError, match="local_files_only"):
        FastEmbedPort(
            model_name="local-model",
            specific_model_path=model_path,
            local_files_only=False,
        )


def test_composition_requires_real_semantic_capability_and_accepts_injected_strategy() -> None:
    configuration = MemoryConfiguration(
        profile_id="semantic-test",
        compatibility_legacy=ModuleConfig(enabled=False),
        episodic=ModuleConfig(enabled=True),
        retrieval=RetrievalConfig(lexical=False, semantic=True),
    )
    with pytest.raises(MemoryValidationError, match="semantic retrieval"):
        compose_experimental_runtime(configuration, InMemoryStructuredStore(), namespace="semantic")

    runtime = compose_experimental_runtime(
        configuration,
        InMemoryStructuredStore(),
        namespace="semantic",
        retrieval_strategy=SemanticRetrievalStrategy(
            FakeEmbeddings({"query": (1.0, 0.0), "x": (1.0, 0.0)})
        ),
    )
    assert runtime.enabled_module_ids == ("episodic",)


def test_settings_reject_nonfinite_and_non_integer_bounds() -> None:
    with pytest.raises(MemoryValidationError, match="finite"):
        SemanticRetrievalSettings(semantic_weight=float("inf"))
    with pytest.raises(MemoryValidationError, match="positive integer"):
        SemanticRetrievalSettings(graph_depth=1.5)  # type: ignore[arg-type]
    with pytest.raises(MemoryValidationError, match="non-negative integer"):
        SemanticRetrievalSettings(max_items=True)  # type: ignore[arg-type]
