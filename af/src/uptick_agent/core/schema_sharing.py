"""Share repeated call schemas without changing executable capability contracts."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from copy import deepcopy

from uptick_agent.core.models import JsonObject, JsonValue

_SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_SCHEMA_LISTS = {"allOf", "anyOf", "oneOf", "prefixItems"}
_SCHEMA_VALUES = {
    "items",
    "additionalItems",
    "additionalProperties",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "propertyNames",
    "unevaluatedProperties",
    "unevaluatedItems",
}
_SCOPED = {"$ref", "$dynamicRef", "$id", "$anchor", "$dynamicAnchor", "$defs", "definitions"}


def _children(schema: JsonObject) -> Iterator[JsonObject]:
    # Do not interpret enum/const/default/examples (or property names) as schemas.
    for key, value in schema.items():
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            yield from (child for child in value.values() if isinstance(child, dict))
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            yield from (child for child in value if isinstance(child, dict))
        elif key in _SCHEMA_VALUES:
            if isinstance(value, dict):
                yield value
            elif key == "items" and isinstance(value, list):
                yield from (child for child in value if isinstance(child, dict))


def _walk(schema: JsonObject) -> Iterator[JsonObject]:
    yield schema
    for child in _children(schema):
        yield from _walk(child)


def _json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def share_call_schemas(schema: JsonObject, call_schemas: list[JsonObject]) -> JsonObject:
    """Use root definitions only for exact repeated calls, and only when smaller.

    Existing reference scopes are left untouched. Output field order and literal
    values are preserved; the typed catalog and its executor schemas are not modified.
    """
    nodes = list(_walk(schema))
    if any(_SCOPED.intersection(node) for node in nodes):
        return schema
    counts = Counter(_json(node) for node in nodes)
    lookup: dict[str, str] = {}
    definitions: JsonObject = {}
    for index, call in enumerate(call_schemas):
        encoded = _json(call)
        if counts[encoded] < 2 or encoded in lookup:
            continue
        name = f"tool_call_{index + 1}"
        lookup[encoded] = name
        definitions[name] = deepcopy(call)
    if not definitions:
        return schema

    def replace(node: JsonObject) -> None:
        name = lookup.get(_json(node))
        if name is not None:
            node.clear()
            node["$ref"] = f"#/$defs/{name}"
            return
        for child in _children(node):
            replace(child)

    result = deepcopy(schema)
    replace(result)
    result["$defs"] = definitions
    return result if len(_json(result)) < len(_json(schema)) else schema
