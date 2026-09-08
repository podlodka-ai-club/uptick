from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import cast

from pydantic import BaseModel

from uptick_agent.core.models import JsonObject, TokenUsage


def distribution_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def model_json_object(value: object) -> JsonObject:
    if value is None:
        return {}
    if isinstance(value, BaseModel):
        return cast(JsonObject, value.model_dump(mode="json"))
    if isinstance(value, dict):
        return cast(JsonObject, value)
    return {}


def normalize_token_usage(value: object, *, provider: str) -> tuple[TokenUsage, JsonObject]:
    raw = model_json_object(value)
    if provider == "codex":
        total = raw.get("total")
        source = total if isinstance(total, dict) else raw
        return (
            TokenUsage(
                input_tokens=_integer(source.get("input_tokens")),
                output_tokens=_integer(source.get("output_tokens")),
                total_tokens=_integer(source.get("total_tokens")),
                cached_input_tokens=_integer(source.get("cached_input_tokens")),
                reasoning_output_tokens=_integer(source.get("reasoning_output_tokens")),
                cache_write_input_tokens=_integer(source.get("cache_write_input_tokens")),
            ),
            raw,
        )

    prompt_details = raw.get("prompt_tokens_details")
    completion_details = raw.get("completion_tokens_details")
    return (
        TokenUsage(
            input_tokens=_integer(raw.get("prompt_tokens")),
            output_tokens=_integer(raw.get("completion_tokens")),
            total_tokens=_integer(raw.get("total_tokens")),
            cached_input_tokens=(
                _integer(prompt_details.get("cached_tokens"))
                if isinstance(prompt_details, dict)
                else None
            ),
            reasoning_output_tokens=(
                _integer(completion_details.get("reasoning_tokens"))
                if isinstance(completion_details, dict)
                else None
            ),
        ),
        raw,
    )


def add_token_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    return left.plus(right)


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
