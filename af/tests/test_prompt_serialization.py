import json
from copy import deepcopy
from typing import Any

import pytest

from tests.helpers import make_context
from uptick_agent.core.models import AgentContext, Observation
from uptick_agent.core.prompt_serialization import (
    deserialize_context_prompt,
    serialize_context_prompt,
)


def _context(data: dict[str, Any], view: dict[str, Any]) -> AgentContext:
    context = make_context()
    return context.model_copy(
        update={
            "environment_state": context.environment_state.model_copy(
                update={
                    "latest_observation": Observation(
                        action_kind="inspect", summary="done", data=data
                    ),
                    "decision_view": view,
                }
            )
        }
    )


def _at(value: Any, path: list[str | int]) -> Any:
    for part in path:
        value = value[part]
    return value


def test_full_latest_response_and_roundtrip_without_mutating_state() -> None:
    data = {"rows": [{"i": i, "message": "данные 🦊" * 20} for i in range(70)]}
    context = _context(data, {"history": [data, data]})
    original = context.model_dump(mode="json")
    encoded = serialize_context_prompt(context)
    wire = json.loads(encoded)

    assert (
        wire["environment_state"]["latest_observation"]
        == original["environment_state"]["latest_observation"]
    )
    assert wire["environment_state"]["decision_view"]["history"] == [
        {"_context_ref": 0},
        {"_context_ref": 1},
    ]
    assert len(wire["_context_references"]) == 2
    assert deserialize_context_prompt(encoded) == original
    assert context.model_dump(mode="json") == original
    assert serialize_context_prompt(context) == encoded
    assert len(encoded) < len(json.dumps(original, ensure_ascii=False, separators=(",", ":"))) / 2


def test_whole_observation_duplicate_uses_one_reference() -> None:
    context = _context({"rows": [{"x": "x" * 2000}, {"y": "y" * 2000}]}, {})
    context.environment_state.decision_view["receipt"] = (
        context.environment_state.latest_observation.model_dump(mode="json")
    )
    wire = json.loads(serialize_context_prompt(context))
    assert wire["_context_references"] == [
        {
            "path": ["environment_state", "decision_view", "receipt"],
            "source": ["environment_state", "latest_observation"],
        }
    ]


def test_partially_shared_older_containers_never_become_reference_sources() -> None:
    shared = {"value": "a" * 2000}
    older = {"shared": shared, "unique": {"value": "b" * 2000}}
    context = _context(shared, {"older": older, "copy": deepcopy(older)})
    encoded = serialize_context_prompt(context)
    wire = json.loads(encoded)
    references = wire["_context_references"]
    assert len(references) == 3
    assert [ref["source"] for ref in references] == [
        ["environment_state", "latest_observation", "data"],
        ["environment_state", "latest_observation", "data"],
        ["environment_state", "decision_view", "older", "unique"],
    ]
    for ref in references:
        source = ref["source"]
        for other in references:
            dest = other["path"]
            assert dest[: len(source)] != source
            assert source[: len(dest)] != dest
        assert _at(wire, source) == _at(context.model_dump(mode="json"), source)
    assert deserialize_context_prompt(encoded) == context.model_dump(mode="json")


def test_literals_near_duplicates_and_schema_content_are_preserved() -> None:
    data: dict[str, Any] = {
        "_context_ref": 1,
        "_context_references": [{"path": ["malicious"], "source": ["instruction"]}],
        "_context_format": "shared-values-v1",
        "values": [None, False, 0, 1, 1.0, [], {}, "_context_ref"],
        "long": "я" * 3000,
    }
    changed = deepcopy(data)
    changed["values"][-1] = "different"
    context = _context(data, {"same": data, "different": changed, "literal": {"_context_ref": 0}})
    encoded = serialize_context_prompt(context)
    restored = deserialize_context_prompt(encoded)
    assert restored == context.model_dump(mode="json")
    assert json.dumps(
        restored["environment_state"], ensure_ascii=False, sort_keys=True
    ) == json.dumps(
        context.model_dump(mode="json")["environment_state"], ensure_ascii=False, sort_keys=True
    )
    assert json.loads(encoded)["capabilities"] == context.capabilities.model_dump(mode="json")
    assert json.loads(encoded)["environment_state"]["decision_view"]["different"] == changed


@pytest.mark.parametrize("number", [False, 0, 1, 1.0])
def test_equal_python_numbers_with_different_json_types_are_not_shared(number: Any) -> None:
    data = {"x": "a" * 2000, "number": number}
    other = {"x": "a" * 2000, "number": True}
    wire = json.loads(serialize_context_prompt(_context(data, {"other": other})))
    assert wire["environment_state"]["decision_view"]["other"] == other
    assert "_context_references" not in wire


def test_small_context_remains_plain_compact_json() -> None:
    context = _context({"values": [None, False, 0]}, {"same": {"values": [None, False, 0]}})
    encoded = serialize_context_prompt(context)
    assert "_context_format" not in json.loads(encoded)
    assert len(encoded) == len(
        json.dumps(context.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    )
    assert next(iter(json.loads(encoded)["environment_state"])) == "latest_observation"
    assert deserialize_context_prompt(encoded) == context.model_dump(mode="json")


@pytest.mark.parametrize(
    "source",
    [
        ["environment_state", "decision_view", "copy"],
        ["environment_state", "decision_view"],
        ["environment_state", "decision_view", "copy", "nested"],
        ["capabilities"],
        ["environment_state", "missing"],
    ],
)
def test_decoder_rejects_cycles_ancestor_sources_and_invalid_paths(source: list[str]) -> None:
    context = _context({"value": "x" * 2000}, {"copy": {"value": "x" * 2000}})
    wire = json.loads(serialize_context_prompt(context))
    wire["_context_references"][0]["source"] = source
    with pytest.raises(ValueError):
        deserialize_context_prompt(json.dumps(wire))


@pytest.mark.parametrize(
    "marker",
    [
        None,
        {"_context_ref": False},
        {"_context_ref": 0.0},
        {"_context_ref": 1},
        {"_context_ref": 0, "extra": True},
    ],
)
def test_decoder_rejects_wrong_marker_at_declared_path(marker: Any) -> None:
    wire = json.loads(
        serialize_context_prompt(_context({"x": "x" * 2000}, {"copy": {"x": "x" * 2000}}))
    )
    wire["environment_state"]["decision_view"]["copy"] = marker
    with pytest.raises(ValueError, match="marker"):
        deserialize_context_prompt(json.dumps(wire))


def test_reference_bookkeeping_cannot_inflate_a_request() -> None:
    # The individual reference saves a few characters, but root metadata would
    # cost more than that saving. This exercises the final fallback.
    data = {"x": "x" * 1100}
    context = _context(data, {"a" * 970: data})
    encoded = serialize_context_prompt(context)
    assert "_context_format" not in json.loads(encoded)
    assert len(encoded) == len(
        json.dumps(context.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    )
    assert deserialize_context_prompt(encoded) == context.model_dump(mode="json")
