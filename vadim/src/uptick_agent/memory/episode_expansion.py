"""Bounded post-selection expansion of structured episodic result views."""

from __future__ import annotations

import json

from uptick_agent.memory.contracts import ContextItem, MemoryValidationError

_RESULT_PREFIX_CHARS = 512
_TRUNCATION_FORMAT = "...[{omitted} characters omitted]"


def _result_excerpt(full_result: str, prefix_chars: int) -> str:
    if prefix_chars >= len(full_result):
        return full_result
    omitted = len(full_result) - prefix_chars
    return full_result[:prefix_chars] + _TRUNCATION_FORMAT.format(omitted=omitted)


def _estimate_item(item: ContextItem) -> int:
    """Match MemoryOrchestrator's configured UTF-8 upper-bound estimator."""

    payload = item.model_dump(mode="json", exclude={"estimated_tokens"})
    rendered = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return max(1, len(rendered.encode("utf-8")))


def _replace_result(item: ContextItem, full_result: str, prefix_chars: int) -> ContextItem:
    candidate = item.model_copy(deep=True)
    candidate.envelope.item["result"] = _result_excerpt(full_result, prefix_chars)
    return candidate.model_copy(update={"estimated_tokens": _estimate_item(candidate)})


def expand_episode_results(
    items: list[ContextItem],
    full_results: dict[str, str],
    *,
    max_estimated_tokens: int,
) -> list[ContextItem]:
    """Expand only selected result prefixes within one shared byte budget.

    ``full_results`` is an already verified mapping of selected item IDs to
    canonical ASCII JSON.  The helper intentionally does not parse or
    canonicalize those strings again: source identity and integrity belong to
    the caller.  Every selected item receives the same additional prefix
    length (up to its own full length), which keeps expansion deterministic and
    prevents the first item from consuming all spare budget.
    """

    if not isinstance(items, list):
        raise MemoryValidationError("episode expansion items must be a list")
    if (
        isinstance(max_estimated_tokens, bool)
        or not isinstance(max_estimated_tokens, int)
        or max_estimated_tokens < 0
    ):
        raise MemoryValidationError("max_estimated_tokens must be a non-negative integer")
    if not isinstance(full_results, dict):
        raise MemoryValidationError("episode expansion full_results must be a mapping")

    item_ids: list[str] = []
    for item in items:
        if not isinstance(item, ContextItem):
            raise MemoryValidationError("episode expansion items must contain ContextItem values")
        item_id = item.envelope.item_id
        if item_id in item_ids:
            raise MemoryValidationError(f"episode expansion contains duplicate item ID {item_id!r}")
        item_ids.append(item_id)
    if set(full_results) != set(item_ids):
        raise MemoryValidationError(
            "episode expansion full_results IDs do not match selected items"
        )
    for item_id, full_result in full_results.items():
        if not isinstance(item_id, str) or not isinstance(full_result, str):
            raise MemoryValidationError("episode expansion source values must be strings")
        if not full_result.isascii():
            raise MemoryValidationError("episode expansion source results must be canonical ASCII")

    lengths: list[int] = []
    for item in items:
        full_result = full_results[item.envelope.item_id]
        ordinary = _result_excerpt(full_result, _RESULT_PREFIX_CHARS)
        if item.envelope.item.get("result") != ordinary:
            raise MemoryValidationError(
                f"episode expansion baseline result mismatch for {item.envelope.item_id!r}"
            )
        baseline = _replace_result(item, full_result, min(_RESULT_PREFIX_CHARS, len(full_result)))
        if _estimate_item(baseline) > max_estimated_tokens:
            raise MemoryValidationError(
                f"episode expansion baseline exceeds budget for {item.envelope.item_id!r}"
            )
        lengths.append(min(_RESULT_PREFIX_CHARS, len(full_result)))

    def build(extra: int) -> list[ContextItem]:
        return [
            _replace_result(
                item,
                full_results[item.envelope.item_id],
                min(length + extra, len(full_results[item.envelope.item_id])),
            )
            for item, length in zip(items, lengths, strict=True)
        ]

    baseline_items = build(0)
    if not baseline_items:
        return []
    if sum(item.estimated_tokens for item in baseline_items) > max_estimated_tokens:
        # The per-item checks above are not sufficient when the shared budget
        # is smaller than the sum of individually valid items.
        raise MemoryValidationError("episode expansion baseline exceeds shared budget")

    max_extra = max(
        (
            len(full_results[item.envelope.item_id]) - length
            for item, length in zip(items, lengths, strict=True)
        ),
        default=0,
    )
    full_items = build(max_extra)
    if sum(item.estimated_tokens for item in full_items) <= max_estimated_tokens:
        return full_items

    # The omission marker changes digit width at boundaries, so the fit
    # predicate is not perfectly monotonic. Binary search remains a bounded,
    # conservative allocator; every accepted candidate is checked again below.
    best_extra = 0
    low, high = 1, max_extra
    while low <= high:
        extra = (low + high) // 2
        candidate = build(extra)
        if sum(item.estimated_tokens for item in candidate) <= max_estimated_tokens:
            best_extra = extra
            low = extra + 1
        else:
            high = extra - 1

    accepted = build(best_extra)
    if sum(item.estimated_tokens for item in accepted) > max_estimated_tokens:
        return baseline_items
    return accepted


__all__ = ["expand_episode_results"]
