"""Optional semantic retrieval over an already admitted memory contribution."""

from __future__ import annotations

import inspect
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from numbers import Real

from uptick_agent.memory.contracts import ContextItem, MemoryContextRequest, MemoryValidationError
from uptick_agent.memory.retrieval_ports import EmbeddingPort, ReasonedQueryPort
from uptick_agent.memory.stores.contracts import canonical_json

_MISSING = object()
_MAX_REASON_LENGTH = 512


def _path(value: str, name: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        raise MemoryValidationError(f"{name} must be a non-empty dotted path")
    parts = tuple(part.strip() for part in value.split("."))
    if any(not part for part in parts):
        raise MemoryValidationError(f"{name} must not contain empty path segments")
    return parts


def _lookup(value: object, path: str, name: str) -> object:
    current = value
    for part in _path(path, name):
        if isinstance(current, dict):
            current = current.get(part, _MISSING)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else _MISSING
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current


def _finite_nonnegative(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise MemoryValidationError(f"{name} must be finite and non-negative")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise MemoryValidationError(f"{name} must be finite and non-negative")
    return converted


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MemoryValidationError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class SemanticRetrievalSettings:
    """Explicit signals and hard local caps for semantic ranking."""

    enabled: bool = True
    semantic_weight: float = 1.0
    baseline_weight: float = 0.0
    confidence_path: str | None = None
    confidence_weight: float = 0.0
    recency_path: str | None = None
    recency_weight: float = 0.0
    recency_reference_time: datetime | None = None
    recency_half_life_seconds: float = 86_400.0
    graph_weight: float = 0.0
    graph_depth: int = 1
    graph_max_neighbors: int = 16
    max_items: int | None = None
    max_estimated_tokens: int | None = None
    max_query_length: int = 16_000

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise MemoryValidationError("enabled must be boolean")
        for name in (
            "semantic_weight",
            "baseline_weight",
            "confidence_weight",
            "recency_weight",
            "graph_weight",
            "recency_half_life_seconds",
        ):
            _finite_nonnegative(getattr(self, name), name)
        if self.confidence_path is not None:
            _path(self.confidence_path, "confidence_path")
        if self.recency_path is not None:
            _path(self.recency_path, "recency_path")
        if self.confidence_weight > 0 and self.confidence_path is None:
            raise MemoryValidationError("confidence_path is required for confidence scoring")
        if self.recency_weight > 0 and self.recency_path is None:
            raise MemoryValidationError("recency_path is required for recency scoring")
        if self.recency_weight > 0 and self.recency_reference_time is None:
            raise MemoryValidationError(
                "recency_reference_time is required for deterministic recency scoring"
            )
        if self.recency_reference_time is not None and (
            self.recency_reference_time.tzinfo is None
            or self.recency_reference_time.utcoffset() is None
        ):
            raise MemoryValidationError("recency_reference_time must be timezone-aware")
        if _finite_nonnegative(
            self.recency_half_life_seconds, "recency_half_life_seconds"
        ) <= 0:
            raise MemoryValidationError("recency_half_life_seconds must be positive")
        _positive_int(self.graph_depth, "graph_depth")
        _positive_int(self.graph_max_neighbors, "graph_max_neighbors")
        for name in ("max_items", "max_estimated_tokens"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise MemoryValidationError(f"{name} must be a non-negative integer")
        _positive_int(self.max_query_length, "max_query_length")


@dataclass(frozen=True, slots=True)
class _Scored:
    item: ContextItem
    score: float
    semantic: float
    confidence: float
    recency: float
    graph: float

    @property
    def tie_key(self) -> tuple[str, ...]:
        envelope = self.item.envelope
        provenance = tuple(
            f"{ref.artefact_id}:{ref.content_hash}:{ref.relation}" for ref in envelope.provenance
        )
        return (
            envelope.item_id,
            envelope.artefact_type,
            envelope.origin_module,
            envelope.origin_version,
            envelope.trust_classification,
            self.item.selection_reason,
            "|".join(provenance),
            canonical_json(envelope.item),
        )


async def _maybe_await(value: object) -> object:
    return await value if inspect.isawaitable(value) else value


def _vector(value: object, name: str, expected: int | None = None) -> tuple[float, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise MemoryValidationError(f"{name} must be a numeric vector")
    try:
        values = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise MemoryValidationError(f"{name} must be a numeric vector") from error
    if not values:
        raise MemoryValidationError(f"{name} must not be empty")
    if expected is not None and len(values) != expected:
        raise MemoryValidationError(f"{name} dimension does not match the query")
    result: list[float] = []
    for component in values:
        if isinstance(component, bool) or not isinstance(component, Real):
            raise MemoryValidationError(f"{name} contains a non-numeric component")
        try:
            converted = float(component)
        except (TypeError, ValueError, OverflowError) as error:
            raise MemoryValidationError(f"{name} contains a non-numeric component") from error
        if not math.isfinite(converted):
            raise MemoryValidationError(f"{name} contains a non-finite component")
        result.append(converted)
    norm = math.sqrt(sum(component * component for component in result))
    if not math.isfinite(norm) or norm == 0:
        raise MemoryValidationError(f"{name} must have a finite non-zero norm")
    return tuple(result)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    value = numerator / (left_norm * right_norm)
    if not math.isfinite(value):
        raise MemoryValidationError("semantic score is not finite")
    return value


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise MemoryValidationError("recency timestamp is invalid") from error
    else:
        raise MemoryValidationError("recency timestamp must be a date-time")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MemoryValidationError("recency timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _provenance_keys(item: ContextItem) -> set[tuple[str, str]]:
    return {(ref.artefact_id, ref.content_hash) for ref in item.envelope.provenance}


def _graph_scores(
    items: Sequence[ContextItem],
    semantic_scores: Sequence[float],
    settings: SemanticRetrievalSettings,
) -> dict[int, float]:
    if settings.graph_weight == 0 or len(items) < 2:
        return {index: 0.0 for index in range(len(items))}
    keys = [_provenance_keys(item) for item in items]
    adjacency = [set() for _ in items]
    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            if keys[left] & keys[right]:
                adjacency[left].add(right)
                adjacency[right].add(left)

    # Propagate only from the single most relevant semantic seed.  This keeps
    # the graph a bounded evidence neighbourhood instead of turning a shared
    # provenance key into a popularity bonus for every candidate.
    seed = min(
        range(len(items)),
        key=lambda index: (-semantic_scores[index], items[index].envelope.item_id),
    )
    seed_strength = max(0.0, semantic_scores[seed])
    scores: dict[int, float] = {index: 0.0 for index in range(len(items))}
    frontier = {seed}
    visited = {seed}
    selected_neighbors = 0
    for depth in range(1, settings.graph_depth + 1):
        next_frontier: set[int] = set()
        for current in sorted(frontier):
            next_frontier.update(adjacency[current])
        next_frontier -= visited
        ordered = sorted(next_frontier, key=lambda index: items[index].envelope.item_id)
        remaining = settings.graph_max_neighbors - selected_neighbors
        if remaining <= 0:
            break
        chosen = ordered[:remaining]
        selected_neighbors += len(chosen)
        decay = seed_strength / (depth + 1)
        for index in chosen:
            scores[index] = decay
        visited.update(chosen)
        frontier = set(chosen)
        if not frontier:
            break
    return scores


class SemanticRetrievalStrategy:
    """Rank only the candidates supplied by a memory contributor."""

    semantic_capability = True

    def __init__(
        self,
        embeddings: EmbeddingPort,
        *,
        settings: SemanticRetrievalSettings | None = None,
        reasoned_query: ReasonedQueryPort | None = None,
    ) -> None:
        if not callable(getattr(embeddings, "embed_query", None)) or not callable(
            getattr(embeddings, "embed_passages", None)
        ):
            raise MemoryValidationError("semantic retrieval requires an embedding port")
        self.embeddings = embeddings
        self.settings = settings or SemanticRetrievalSettings()
        self.reasoned_query = reasoned_query

    async def rank(
        self, candidates: Iterable[ContextItem], request: MemoryContextRequest
    ) -> list[ContextItem]:
        if not isinstance(request, MemoryContextRequest):
            raise MemoryValidationError("semantic retrieval requires MemoryContextRequest")
        raw_source = list(candidates)
        source: list[ContextItem] = []
        for item in raw_source:
            if not isinstance(item, ContextItem):
                raise MemoryValidationError("retrieval candidates must be ContextItem values")
            try:
                source.append(
                    ContextItem.model_validate(
                        item.model_dump(mode="python", round_trip=True)
                    )
                )
            except (TypeError, ValueError) as error:
                raise MemoryValidationError("retrieval candidate is invalid") from error
        if not self.settings.enabled or not source:
            return source
        # A zero caller budget is a hard read boundary.  Do not invoke an
        # embedding or reformulation provider when no item can be admitted.
        if self._limit(self.settings.max_items, request.max_items) == 0 or self._limit(
            self.settings.max_estimated_tokens, request.max_estimated_tokens
        ) == 0:
            return []

        # Work on an owned, round-tripped request so a provider cannot mutate
        # caller-owned context or bypass Pydantic validation with a fabricated
        # model instance.
        try:
            request = MemoryContextRequest.model_validate(
                request.model_dump(mode="python", round_trip=True)
            )
        except (TypeError, ValueError) as error:
            raise MemoryValidationError("semantic retrieval request is invalid") from error
        for name in ("max_items", "max_estimated_tokens"):
            value = getattr(request, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise MemoryValidationError(f"{name} must be a non-negative integer")

        query = request.query
        if self.reasoned_query is not None:
            # Give reformulation an independent copy.  The validated request
            # below remains the authoritative source of caller budgets.
            reformulation_request = MemoryContextRequest.model_validate(
                request.model_dump(mode="python", round_trip=True)
            )
            query = await _maybe_await(self.reasoned_query.rewrite(reformulation_request))
            if not isinstance(query, str) or not query.strip():
                raise MemoryValidationError("reasoned query port returned a blank query")
            query = query.strip()
        if len(query) > self.settings.max_query_length:
            raise MemoryValidationError("semantic query exceeds max_query_length")

        query_vector = _vector(
            await _maybe_await(self.embeddings.embed_query(query)),
            "query embedding",
        )
        declared_dimension = getattr(self.embeddings, "dimension", None)
        if declared_dimension is not None and (
            isinstance(declared_dimension, bool)
            or not isinstance(declared_dimension, int)
            or declared_dimension < 1
        ):
            raise MemoryValidationError("embedding port dimension must be a positive integer")
        if declared_dimension is not None and len(query_vector) != declared_dimension:
            raise MemoryValidationError("query embedding dimension does not match the port")
        passages = [canonical_json(item.envelope.item) for item in source]
        raw_passages = await _maybe_await(self.embeddings.embed_passages(passages))
        if isinstance(raw_passages, (str, bytes, bytearray)):
            raise MemoryValidationError("passage embeddings must be a sequence of vectors")
        try:
            passage_values = list(raw_passages)  # type: ignore[arg-type]
        except TypeError as error:
            raise MemoryValidationError(
                "passage embeddings must be a sequence of vectors"
            ) from error
        if len(passage_values) != len(source):
            raise MemoryValidationError("embedding count does not match admitted candidates")
        vectors = [
            _vector(value, f"passage embedding {index}", len(query_vector))
            for index, value in enumerate(passage_values)
        ]
        semantic_scores = [_cosine(query_vector, vector) for vector in vectors]
        graph = _graph_scores(source, semantic_scores, self.settings)
        scored: list[_Scored] = []
        for index, (item, semantic) in enumerate(zip(source, semantic_scores, strict=True)):
            confidence = self._confidence(item)
            recency = self._recency(item)
            score = (
                self.settings.semantic_weight * semantic
                + self.settings.baseline_weight * item.score
                + self.settings.confidence_weight * confidence
                + self.settings.recency_weight * recency
                + self.settings.graph_weight * graph[index]
            )
            if not math.isfinite(score):
                raise MemoryValidationError("semantic retrieval score is not finite")
            scored.append(_Scored(item, score, semantic, confidence, recency, graph[index]))

        scored.sort(key=lambda item: (-item.score, item.tie_key))
        return self._select(scored, request)

    def _confidence(self, item: ContextItem) -> float:
        if self.settings.confidence_path is None:
            return 0.0
        value = _lookup(
            item.envelope.model_dump(mode="json"), self.settings.confidence_path, "confidence_path"
        )
        if value is _MISSING:
            return 0.0
        if isinstance(value, bool) or not isinstance(value, Real):
            raise MemoryValidationError("confidence signal must be finite numeric data")
        confidence = float(value)
        if not math.isfinite(confidence):
            raise MemoryValidationError("confidence signal must be finite numeric data")
        if not 0 <= confidence <= 1:
            raise MemoryValidationError("confidence signal must be between zero and one")
        return confidence

    def _recency(self, item: ContextItem) -> float:
        if self.settings.recency_path is None:
            return 0.0
        value = _lookup(
            item.envelope.model_dump(mode="json"), self.settings.recency_path, "recency_path"
        )
        if value is _MISSING:
            return 0.0
        reference = self.settings.recency_reference_time
        if reference is None:  # pragma: no cover - guarded by settings validation
            raise MemoryValidationError("recency reference time is unavailable")
        age = max(0.0, (reference.astimezone(UTC) - _timestamp(value)).total_seconds())
        result = math.exp(
            -math.log(2) * age / self.settings.recency_half_life_seconds
        )
        if not math.isfinite(result):
            raise MemoryValidationError("recency signal is not finite")
        return result

    def _select(
        self, scored: Sequence[_Scored], request: MemoryContextRequest
    ) -> list[ContextItem]:
        item_limit = self._limit(self.settings.max_items, request.max_items)
        token_limit = self._limit(self.settings.max_estimated_tokens, request.max_estimated_tokens)
        if item_limit == 0 or token_limit == 0:
            return []
        selected: list[ContextItem] = []
        used_tokens = 0
        for candidate in scored:
            if item_limit is not None and len(selected) >= item_limit:
                break
            if (
                token_limit is not None
                and used_tokens + candidate.item.estimated_tokens > token_limit
            ):
                continue
            details = [
                f"semantic={candidate.semantic:.6g}",
                f"baseline={candidate.item.score:.6g}",
                f"confidence={candidate.confidence:.6g}",
                f"recency={candidate.recency:.6g}",
                f"graph={candidate.graph:.6g}",
            ]
            selected.append(
                candidate.item.model_copy(
                    update={
                        "score": candidate.score,
                        "selection_reason": ("semantic retrieval: " + "; ".join(details))[
                            :_MAX_REASON_LENGTH
                        ],
                    }
                )
            )
            used_tokens += candidate.item.estimated_tokens
        return selected

    @staticmethod
    def _limit(strategy: int | None, request: int | None) -> int | None:
        if strategy is None:
            return request
        return strategy if request is None else min(strategy, request)


__all__ = ["SemanticRetrievalSettings", "SemanticRetrievalStrategy"]
