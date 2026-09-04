"""Small, dependency-free advanced retrieval strategies.

This module operates only on ``ContextItem`` and ``MemoryContextRequest``.
The input item score is the contributor's lexical baseline. Retrieval adds
only transparent lexical and explicitly configured structured signals; a
lexical match is not evidence of causal utility.

``AdvancedRetrievalStrategy.rank`` is deliberately pure with respect to the
contributor. The composition root may apply it to an admitted contribution
while retaining that module's write/finalize/consolidation capabilities. The
orchestrator remains responsible for global context budgets.
"""

from __future__ import annotations

import inspect
import math
import re
from collections import defaultdict
from collections.abc import Awaitable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from uptick_agent.memory.contracts import (
    ContextItem,
    MemoryContextRequest,
    MemoryValidationError,
)
from uptick_agent.memory.stores.contracts import canonical_json

_WORD = re.compile(r"[\w-]+", re.UNICODE)
_MISSING = object()
_MAX_REASON_LENGTH = 512


def _tokens(value: object) -> set[str]:
    rendered = canonical_json(value)
    return {token.casefold() for token in _WORD.findall(rendered) if len(token) > 1}


def _path(path: str, name: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path.strip():
        raise MemoryValidationError(f"{name} must be a non-empty dotted path")
    parts = tuple(part.strip() for part in path.split("."))
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


@dataclass(frozen=True, slots=True)
class StructuredFeature:
    """One explicit request-to-candidate comparison.

    Paths address ``request.model_dump(mode="json")`` and
    ``item.envelope.model_dump(mode="json")`` respectively. ``exact``
    compares canonical JSON values; ``overlap`` compares shared values in two
    lists. A required feature is a hard candidate filter.
    """

    request_path: str
    candidate_path: str
    weight: float = 1.0
    operator: Literal["exact", "overlap"] = "exact"
    required: bool = False

    def __post_init__(self) -> None:
        _path(self.request_path, "request_path")
        _path(self.candidate_path, "candidate_path")
        if not math.isfinite(self.weight) or self.weight < 0:
            raise MemoryValidationError("structured feature weight must be finite and non-negative")
        if self.operator not in {"exact", "overlap"}:
            raise MemoryValidationError("structured feature operator must be exact or overlap")


@dataclass(frozen=True, slots=True)
class AdvancedRetrievalSettings:
    """Transparent scoring, diversity and optional local limits."""

    enabled: bool = True
    lexical_weight: float = 1.0
    structured_features: Sequence[StructuredFeature] = ()
    diversity_path: str | None = None
    diversity_penalty: float = 0.25
    max_per_diversity_key: int | None = None
    deduplicate: bool = True
    max_items: int | None = None
    max_estimated_tokens: int | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.lexical_weight) or self.lexical_weight < 0:
            raise MemoryValidationError("lexical_weight must be finite and non-negative")
        if not math.isfinite(self.diversity_penalty) or self.diversity_penalty < 0:
            raise MemoryValidationError("diversity_penalty must be finite and non-negative")
        if self.diversity_path is not None:
            _path(self.diversity_path, "diversity_path")
        if self.max_per_diversity_key is not None and self.max_per_diversity_key < 1:
            raise MemoryValidationError("max_per_diversity_key must be positive")
        if self.max_items is not None and self.max_items < 0:
            raise MemoryValidationError("max_items must be non-negative")
        if self.max_estimated_tokens is not None and self.max_estimated_tokens < 0:
            raise MemoryValidationError("max_estimated_tokens must be non-negative")
        features = tuple(self.structured_features)
        if not all(isinstance(feature, StructuredFeature) for feature in features):
            raise MemoryValidationError("structured_features must contain StructuredFeature values")
        object.__setattr__(self, "structured_features", features)


@dataclass(frozen=True, slots=True)
class _Scored:
    item: ContextItem
    score: float
    lexical: float
    matches: tuple[str, ...]
    diversity_key: str | None

    @property
    def tie_key(self) -> tuple[str, str, str, str, str, str, str, str]:
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


class RetrievalStrategy(Protocol):
    """Replaceable contract for ranking already-admitted candidates."""

    def rank(
        self, candidates: Iterable[ContextItem], request: MemoryContextRequest
    ) -> list[ContextItem] | Awaitable[list[ContextItem]]: ...


class ChainedRetrievalStrategy:
    """Apply declared ranking/filtering stages in order.

    Stages may be synchronous or asynchronous.  The chain is a read-side
    value transformation; module lifecycle methods remain on the registered
    module object.
    """

    def __init__(self, *strategies: RetrievalStrategy) -> None:
        if not strategies:
            raise MemoryValidationError("retrieval strategy chain must not be empty")
        self._strategies = tuple(strategies)

    async def rank(
        self, candidates: Iterable[ContextItem], request: MemoryContextRequest
    ) -> list[ContextItem]:
        current = list(candidates)
        for strategy in self._strategies:
            ranked = strategy.rank(current, request)
            if inspect.isawaitable(ranked):
                ranked = await ranked
            if not isinstance(ranked, list):
                raise MemoryValidationError("retrieval chain stage returned invalid candidates")
            current = ranked
        return current


class AdvancedRetrievalStrategy:
    """Lexical-plus-structured ranking with deterministic diversity and dedup."""

    def __init__(self, settings: AdvancedRetrievalSettings | None = None) -> None:
        self.settings = settings or AdvancedRetrievalSettings()

    def rank(
        self, candidates: Iterable[ContextItem], request: MemoryContextRequest
    ) -> list[ContextItem]:
        if not isinstance(request, MemoryContextRequest):
            raise MemoryValidationError("advanced retrieval requires MemoryContextRequest")
        source = list(candidates)
        if not self.settings.enabled:
            return source
        if not all(isinstance(item, ContextItem) for item in source):
            raise MemoryValidationError("retrieval candidates must be ContextItem values")

        scored = [
            candidate for item in source if (candidate := self._score(item, request)) is not None
        ]
        if self.settings.deduplicate:
            scored = self._deduplicate(scored)
        scored.sort(key=lambda candidate: (-candidate.score, candidate.tie_key))

        item_limit = self._limit(self.settings.max_items, request.max_items)
        token_limit = self._limit(
            self.settings.max_estimated_tokens,
            request.max_estimated_tokens,
        )
        if item_limit == 0 or token_limit == 0:
            return []

        selected: list[ContextItem] = []
        counts: defaultdict[str, int] = defaultdict(int)
        pending = list(scored)
        used_tokens = 0
        while pending and (item_limit is None or len(selected) < item_limit):
            eligible: list[tuple[float, _Scored]] = []
            for candidate in pending:
                group = candidate.diversity_key
                if (
                    group is not None
                    and self.settings.max_per_diversity_key is not None
                    and counts[group] >= self.settings.max_per_diversity_key
                ):
                    continue
                if (
                    token_limit is not None
                    and used_tokens + candidate.item.estimated_tokens > token_limit
                ):
                    continue
                adjustment = (
                    self.settings.diversity_penalty * counts[group] if group is not None else 0.0
                )
                adjusted = candidate.score - adjustment
                if not math.isfinite(adjusted):
                    raise MemoryValidationError("retrieval score is not finite")
                eligible.append((adjusted, candidate))
            if not eligible:
                break
            adjusted, chosen = min(eligible, key=lambda pair: (-pair[0], pair[1].tie_key))
            pending.remove(chosen)
            selected.append(self._render(chosen, adjusted))
            used_tokens += chosen.item.estimated_tokens
            if chosen.diversity_key is not None:
                counts[chosen.diversity_key] += 1
        return selected

    @staticmethod
    def _limit(strategy: int | None, request: int | None) -> int | None:
        if strategy is None:
            return request
        return strategy if request is None else min(strategy, request)

    def _score(self, item: ContextItem, request: MemoryContextRequest) -> _Scored | None:
        query_tokens = _tokens(request.query)
        item_tokens = _tokens(item.envelope.item)
        lexical = (
            len(query_tokens & item_tokens) / math.sqrt(len(query_tokens) * len(item_tokens))
            if query_tokens and item_tokens
            else 0.0
        )
        score = item.score + self.settings.lexical_weight * lexical
        matches: list[str] = []
        request_data = request.model_dump(mode="json")
        candidate_data = item.envelope.model_dump(mode="json")
        for feature in self.settings.structured_features:
            left = _lookup(request_data, feature.request_path, "request_path")
            right = _lookup(candidate_data, feature.candidate_path, "candidate_path")
            matched = self._matches(left, right, feature.operator)
            if feature.required and not matched:
                return None
            if matched:
                score += feature.weight
                matches.append(f"{feature.request_path}={feature.candidate_path}")
        if not math.isfinite(score):
            raise MemoryValidationError("retrieval score is not finite")
        diversity_key = None
        if self.settings.diversity_path is not None:
            value = _lookup(candidate_data, self.settings.diversity_path, "diversity_path")
            diversity_key = canonical_json(value) if value is not _MISSING else "<missing>"
        return _Scored(item, score, lexical, tuple(matches), diversity_key)

    @staticmethod
    def _matches(left: object, right: object, operator: str) -> bool:
        if left is _MISSING or right is _MISSING:
            return False
        if operator == "exact":
            return canonical_json(left) == canonical_json(right)
        if not isinstance(left, (list, tuple, set)) or not isinstance(right, (list, tuple, set)):
            return False
        right_values = {canonical_json(value) for value in right}
        return any(canonical_json(value) in right_values for value in left)

    @staticmethod
    def _deduplicate(candidates: list[_Scored]) -> list[_Scored]:
        winners: dict[str, _Scored] = {}
        for candidate in candidates:
            item_id = candidate.item.envelope.item_id
            incumbent = winners.get(item_id)
            if incumbent is None or (-candidate.score, candidate.tie_key) < (
                -incumbent.score,
                incumbent.tie_key,
            ):
                winners[item_id] = candidate
        return list(winners.values())

    @staticmethod
    def _render(candidate: _Scored, adjusted: float) -> ContextItem:
        details = [
            f"baseline={candidate.item.score:.6g}",
            f"lexical={candidate.lexical:.6g}",
        ]
        if candidate.matches:
            details.append("structured=" + ",".join(candidate.matches))
        if adjusted != candidate.score:
            details.append(f"diversity_adjustment={adjusted - candidate.score:.6g}")
        reason = ("advanced retrieval: " + "; ".join(details))[:_MAX_REASON_LENGTH]
        return candidate.item.model_copy(update={"score": adjusted, "selection_reason": reason})
