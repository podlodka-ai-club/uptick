from __future__ import annotations

import copy
import json

import pytest

from uptick_agent.memory.contracts import (
    ContextItem,
    MemoryValidationError,
    ProvenanceRef,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.episode_expansion import expand_episode_results

_HASH = "a" * 64


def _item(item_id: str, full_result: str, *, score: float = 1.25) -> ContextItem:
    prefix = full_result[:512]
    if len(full_result) > 512:
        prefix += f"...[{len(full_result) - 512} characters omitted]"
    return ContextItem(
        envelope=UntrustedMemoryEnvelope(
            item_id=item_id,
            artefact_type="episode",
            origin_module="episodic",
            origin_version="1.1",
            trust_classification="external_untrusted",
            provenance=[ProvenanceRef(artefact_id=f"source-{item_id}", content_hash=_HASH)],
            item={
                "run_id": "run",
                "result": prefix,
                "query_match": {"source_field": "result", "text": "needle"},
            },
        ),
        score=score,
        selection_reason="episodic lexical overlap=2",
        estimated_tokens=0,
    )


def _estimate(item: ContextItem) -> int:
    payload = item.model_dump(mode="json", exclude={"estimated_tokens"})
    rendered = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return max(1, len(rendered.encode("utf-8")))


def test_expands_selected_items_balanced_and_preserves_selection_metadata() -> None:
    full = {
        "a": json.dumps(
            {"detail": "A" * 1_000, "ok": False},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "b": json.dumps(
            {"detail": "B" * 1_000, "ok": True},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
    }
    items = [_item("a", full["a"]), _item("b", full["b"], score=0.5)]
    before = copy.deepcopy(items)
    baseline_total = sum(_estimate(item) for item in items)
    expanded = expand_episode_results(
        items,
        full,
        max_estimated_tokens=baseline_total + 350,
    )

    assert [item.envelope.item_id for item in expanded] == ["a", "b"]
    assert [item.score for item in expanded] == [1.25, 0.5]
    assert [item.selection_reason for item in expanded] == [
        "episodic lexical overlap=2",
        "episodic lexical overlap=2",
    ]
    for index, item in enumerate(expanded):
        assert item.envelope.item["query_match"] == before[index].envelope.item["query_match"]
        assert item.envelope.provenance == before[index].envelope.provenance
        result = item.envelope.item["result"]
        assert len(result) > len(before[index].envelope.item["result"])
        marker_index = result.find("...[")
        if marker_index < 0:
            assert result == full[item.envelope.item_id]
        else:
            assert result[:marker_index] == full[item.envelope.item_id][:marker_index]
            assert result[marker_index:] == (
                f"...[{len(full[item.envelope.item_id]) - marker_index} characters omitted]"
            )
    assert items == before
    assert sum(item.estimated_tokens for item in expanded) <= baseline_total + 350


def test_full_results_fit_and_escaped_json_are_returned_verbatim() -> None:
    full_result = json.dumps(
        {
            "detail": 'quoted "text" \\ and \ud83c\udf0d' + "x" * 700,
            "ok": True,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    item = _item("one", full_result)
    expanded = expand_episode_results(
        [item],
        {"one": full_result},
        max_estimated_tokens=_estimate(item) + 10_000,
    )
    assert expanded[0].envelope.item["result"] == full_result
    assert expanded[0].estimated_tokens == _estimate(expanded[0])


@pytest.mark.parametrize(
    ("items", "full_results", "budget", "message"),
    [
        ([_item("one", "{}")], {}, 1_000, "IDs"),
        ([_item("one", "{}")], {"one": "{}", "extra": "{}"}, 1_000, "IDs"),
        ([_item("one", "{}")], {"one": '{"new":1}'}, 1_000, "baseline"),
    ],
)
def test_rejects_source_boundaries(
    items: list[ContextItem], full_results: dict[str, str], budget: int, message: str
) -> None:
    with pytest.raises(MemoryValidationError, match=message):
        expand_episode_results(items, full_results, max_estimated_tokens=budget)


def test_rejects_duplicate_ids_and_baseline_over_budget() -> None:
    full = "{}"
    duplicate = _item("same", full)
    with pytest.raises(MemoryValidationError, match="duplicate"):
        expand_episode_results([duplicate, duplicate], {"same": full}, max_estimated_tokens=10_000)

    item = _item("large", full)
    with pytest.raises(MemoryValidationError, match="baseline"):
        expand_episode_results([item], {"large": full}, max_estimated_tokens=1)


def test_empty_selection_is_safe() -> None:
    assert expand_episode_results([], {}, max_estimated_tokens=0) == []


def test_returned_nested_view_does_not_alias_input():
    full = json.dumps({"body": "x" * 800}, sort_keys=True, separators=(",", ":"))
    original = _item("copy", full)
    result = expand_episode_results([original], {"copy": full}, max_estimated_tokens=5000)
    result[0].envelope.item["query_match"]["text"] = "changed"
    assert original.envelope.item["query_match"]["text"] == "needle"
