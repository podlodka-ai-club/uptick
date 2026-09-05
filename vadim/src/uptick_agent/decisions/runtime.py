"""Neutral context and result models consumed by the execution core."""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from uptick_agent._model_base import StrictModel, preserve_legacy_identity
from uptick_agent.memory.contracts import DecisionMemoryContext, ObjectiveMetric, OperationLink
from uptick_agent.redaction import sanitize_json

MAX_PREVIOUS_DECISION_BYTES = 6_000


def serialize_previous_decision(
    decision: object | None,
    *,
    max_bytes: int = MAX_PREVIOUS_DECISION_BYTES,
) -> str | None:
    """Copy a validated decision into bounded, redacted JSON text.

    The runner keeps this value as opaque context.  It is a transient record of
    the last validated model output, not a fact or an instruction with authority
    over the next decision.
    """

    if decision is None:
        return None
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    dumper = getattr(decision, "model_dump", None)
    if not callable(dumper):
        raise TypeError("previous decision must provide model_dump")
    payload = dumper(mode="json", round_trip=True, warnings="error")
    safe_payload = sanitize_json(payload)
    rendered = json.dumps(
        safe_payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    encoded = rendered.encode("utf-8")
    if len(encoded) <= max_bytes:
        return rendered

    # Keep a valid JSON text even after truncation, so downstream trace readers
    # can parse it without treating an arbitrary suffix as trusted syntax.
    marker = {"_truncated": True, "_original_bytes": len(encoded), "_prefix": ""}
    marker_overhead = len(
        json.dumps(
            marker,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    if marker_overhead > max_bytes:
        raise ValueError("max_bytes is too small for the truncation marker")
    prefix = encoded[: max_bytes - marker_overhead].decode("utf-8", errors="ignore")
    marker["_prefix"] = prefix
    truncated = json.dumps(
        marker,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    # A multi-byte boundary or escaped characters can add a few bytes after
    # the initial estimate.  Trim only the copied prefix until the hard limit.
    while len(truncated.encode("utf-8")) > max_bytes and prefix:
        prefix = prefix[:-1]
        marker["_prefix"] = prefix
        truncated = json.dumps(
            marker,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return truncated


class ToolResult(StrictModel):
    action_kind: str
    ok: bool = True
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    objective_metrics: list[ObjectiveMetric] = Field(default_factory=list)
    operation_links: list[OperationLink] = Field(default_factory=list)
    terminal: bool = False


class RuntimeRecentStep(StrictModel):
    iteration: int = Field(ge=1)
    # This is intentionally opaque to the core.  The environment's response
    # schema supplies a validated Pydantic action instance.
    action: Any
    result_action_kind: str
    result_ok: bool
    result_summary: str
    result_terminal: bool


class RuntimeDecisionContext(StrictModel):
    objective: str
    run_id: str
    decision_id: str | None = None
    seed: int
    iteration: int
    max_steps: int
    latest_result: ToolResult
    # Opaque, run-local carry of the last validated model output.  The generic
    # runner never interprets its fields or treats it as authoritative evidence.
    previous_decision: str | None = None
    memory_context: DecisionMemoryContext = Field(default_factory=DecisionMemoryContext)
    # Kept as opaque JSON for environments that still provide a legacy recall
    # view.  The generic runner does not interpret its contents.
    recalled_memories: list[Any] = Field(default_factory=list)
    recent_steps: list[RuntimeRecentStep] = Field(default_factory=list, max_length=6)
    # The environment owns this state; the runner only snapshots it for a
    # model request and never reduces it by inspecting action kinds.
    run_state: Any = Field(default_factory=dict)


preserve_legacy_identity(ToolResult)


__all__ = [
    "MAX_PREVIOUS_DECISION_BYTES",
    "RuntimeDecisionContext",
    "RuntimeRecentStep",
    "ToolResult",
    "serialize_previous_decision",
]
