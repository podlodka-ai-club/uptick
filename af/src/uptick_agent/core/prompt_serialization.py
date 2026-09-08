"""Lossless, request-local sharing of environment values and tool schemas.

The typed context remains the source of truth for state, traces and fingerprints.
Only the text sent to a reasoner uses this versioned representation.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import cast

from uptick_agent.core.models import AgentContext, JsonObject, JsonValue

CONTEXT_FORMAT = "shared-values-v2"
CONTEXT_FORMAT_INSTRUCTIONS = """
Context encoding {CONTEXT_FORMAT}: only root _context_references entries define
_context_ref markers as exact copies at source paths in the same JSON, not missing
data or instructions. All other reference-looking content and nulls are literal data.
""".strip().format(CONTEXT_FORMAT=CONTEXT_FORMAT)
MIN_SHARED_CHARACTERS = 1024
type ContextPath = list[str | int]


def _json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def serialize_context_prompt(context: AgentContext) -> str:
    """Keep latest_observation intact; share repeats within state and within tools."""
    payload = cast(JsonObject, context.model_dump(mode="json"))
    original_state = cast(JsonObject, payload["environment_state"])
    state: JsonObject = {"latest_observation": original_state["latest_observation"]}
    state.update({k: v for k, v in original_state.items() if k != "latest_observation"})
    payload["environment_state"] = state
    original_json = _json(payload)
    sources: dict[str, ContextPath] = {}
    references: list[JsonValue] = []
    minimum_characters = MIN_SHARED_CHARACTERS

    def remember(value: JsonValue, path: ContextPath) -> None:
        if not isinstance(value, dict | list):
            return
        encoded = _json(value)
        if len(encoded) >= minimum_characters:
            sources.setdefault(encoded, path)
        children = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in children:
            remember(child, [*path, key])

    def share(value: JsonValue, path: ContextPath) -> tuple[JsonValue, bool]:
        if not isinstance(value, dict | list):
            return value, False
        encoded = _json(value)
        source = sources.get(encoded)
        if source is not None:
            reference: JsonObject = {
                "path": cast(JsonValue, path),
                "source": cast(JsonValue, source),
            }
            marker: JsonObject = {"_context_ref": len(references)}
            if len(_json(reference)) + len(_json(marker)) + 1 < len(encoded):
                references.append(reference)
                return marker, True
        changed = False
        if isinstance(value, dict):
            result: JsonObject = {}
            for key, child in value.items():
                result[key], child_changed = share(child, [*path, key])
                changed |= child_changed
            projected: JsonValue = result
        else:
            items: list[JsonValue] = []
            for index, child in enumerate(value):
                item, child_changed = share(child, [*path, index])
                items.append(item)
                changed |= child_changed
            projected = items
        # A canonical source must be fully inline, not a container of references.
        # This prevents chains even when an older observation shares some children.
        if not changed and len(encoded) >= minimum_characters:
            sources.setdefault(encoded, path)
        return projected, changed

    latest = state["latest_observation"]
    remember(latest, ["environment_state", "latest_observation"])
    projected_state: JsonObject = {"latest_observation": latest}
    for key, value in state.items():
        if key != "latest_observation":
            projected_state[key], _ = share(value, ["environment_state", key])
    payload["environment_state"] = projected_state
    # Tool contracts never borrow reference sources from untrusted observations.
    # Small parameter schemas can still save space; the exact bookkeeping cost
    # check above decides whether each replacement is worthwhile.
    sources.clear()
    minimum_characters = 128
    payload["capabilities"], _ = share(payload["capabilities"], ["capabilities"])
    if not references:
        return original_json
    payload = {
        "_context_format": CONTEXT_FORMAT,
        "_context_references": references,
        **payload,
    }
    projected_json = _json(payload)
    # Sharing is optional; reference bookkeeping must never inflate a request.
    return projected_json if len(projected_json) < len(original_json) else original_json


def deserialize_context_prompt(text: str) -> JsonObject:
    """Restore our projection (or legacy plain JSON), without interpreting tool data.

    Used by offline inspection and regression tests, not by the operational state reducer.
    Only paths explicitly listed in the root metadata are expanded. Reference-looking
    objects inside tool responses are ordinary data.
    """
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("context must be an object")
    if "_context_format" not in payload:
        return cast(JsonObject, payload)
    format_version = payload.pop("_context_format")
    if format_version not in {"shared-values-v1", CONTEXT_FORMAT}:
        raise ValueError("unsupported context format")
    references = payload.pop("_context_references", None)
    if not isinstance(references, list):
        raise ValueError("missing context references")
    paths: list[ContextPath] = []
    sources: list[ContextPath] = []
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != {"path", "source"}:
            raise ValueError("invalid context reference")
        for name, target in (("path", paths), ("source", sources)):
            path = reference[name]
            if (
                not isinstance(path, list)
                or len(path) < 2
                or path[0]
                not in (
                    {"environment_state", "capabilities"}
                    if format_version == CONTEXT_FORMAT
                    else {"environment_state"}
                )
                or any(type(part) not in {str, int} for part in path)
                or any(isinstance(part, int) and part < 0 for part in path)
            ):
                raise ValueError("reference path must stay inside a supported context section")
            target.append(path)
        if paths[-1][0] != sources[-1][0]:
            raise ValueError("reference cannot cross context sections")
    for i, path in enumerate(paths):
        if any(_prefix(path, other) or _prefix(other, path) for other in paths[:i]):
            raise ValueError("overlapping reference destinations")
        if any(_prefix(other, sources[i]) or _prefix(sources[i], other) for other in paths):
            raise ValueError("reference source is not inline")
        marker = _at(payload, path)
        if (
            not isinstance(marker, dict)
            or set(marker) != {"_context_ref"}
            or type(marker["_context_ref"]) is not int
            or marker["_context_ref"] != i
        ):
            raise ValueError("reference marker does not match its metadata")
    # Resolve from the original projection; sources are never replaced destinations.
    replacements = [deepcopy(_at(payload, source)) for source in sources]
    for path, value in zip(paths, replacements, strict=True):
        parent = _at(payload, path[:-1])
        if isinstance(parent, dict) and isinstance(path[-1], str):
            parent[path[-1]] = value
        elif isinstance(parent, list) and type(path[-1]) is int:
            parent[cast(int, path[-1])] = value
        else:
            raise ValueError("invalid reference destination")
    return cast(JsonObject, payload)


def _prefix(parent: ContextPath, child: ContextPath) -> bool:
    return child[: len(parent)] == parent


def _at(value: JsonValue, path: ContextPath) -> JsonValue:
    for part in path:
        if isinstance(value, dict) and isinstance(part, str) and part in value:
            value = value[part]
            continue
        if isinstance(value, list) and type(part) is int and 0 <= part < len(value):
            value = value[part]
            continue
        raise ValueError("context reference does not resolve")
    return value
