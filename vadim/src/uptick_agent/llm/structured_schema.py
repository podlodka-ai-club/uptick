"""Small schema conversion shared by structured-output providers."""

from __future__ import annotations

from typing import Any


def normalize_output_schema(value: Any) -> Any:
    """Convert a Pydantic JSON Schema to the provider strict-schema subset.

    The Codex adapter historically performed these transformations locally.  Keep
    the operation deliberately structural so both providers send the same schema
    without importing either provider SDK here.
    """
    if isinstance(value, list):
        return [normalize_output_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized = {
        key: normalize_output_schema(child)
        for key, child in value.items()
        if key not in {"default", "discriminator"}
    }
    if "const" in normalized:
        normalized["enum"] = [normalized.pop("const")]
    if "oneOf" in normalized:
        normalized["anyOf"] = normalized.pop("oneOf")

    properties = normalized.get("properties")
    if isinstance(properties, dict):
        normalized["required"] = list(properties)
        normalized["additionalProperties"] = False
    return normalized


__all__ = ["normalize_output_schema"]
