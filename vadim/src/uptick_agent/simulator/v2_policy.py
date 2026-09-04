"""Runner-side policy for planning v2 simulation time advances.

The simulator environment executes the typed action it receives. This module
adds the deterministic horizon planning needed at the v2 CLI composition
boundary, where the public clock and runner decision budget are both available.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from uptick_agent.models import DecisionContext, NextStep, V2AdvanceTime

V2_TIME_BUDGET_POLICY_ID = "simulator-v2-time-budget"
V2_TIME_BUDGET_POLICY_VERSION = "1.0"


class DecisionModelDelegate(Protocol):
    async def decide(self, context: DecisionContext) -> NextStep: ...

    def prompt_trace(self, context: DecisionContext) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class V2TimeBudgetPlan:
    """Pure calculation used to distribute the public remaining horizon."""

    remaining_seconds: float
    remaining_decisions: int
    wait_slots: int
    minimum_duration_seconds: int

    def metadata(self, *, pending_operations: bool = False) -> dict[str, Any]:
        return {
            "policy_id": V2_TIME_BUDGET_POLICY_ID,
            "policy_version": V2_TIME_BUDGET_POLICY_VERSION,
            "time_budget": {
                "clock_remaining_seconds": self.remaining_seconds,
                "remaining_decisions": self.remaining_decisions,
                "wait_slots": self.wait_slots,
                "minimum_duration_seconds": self.minimum_duration_seconds,
                "pending_operations": pending_operations,
                "hint": (
                    "An accepted, pending, or running operation needs polling; retain the "
                    "model's proposed interval."
                    if pending_operations
                    else self.hint
                ),
            },
        }

    @property
    def hint(self) -> str:
        arithmetic = math.ceil(self.remaining_seconds / self.wait_slots)
        return (
            "For a bounded v2 advance, reserve about half the remaining decisions "
            "for investigation: use at least "
            f"ceil({self.remaining_seconds}/max(1, {self.remaining_decisions}//2))="
            f"{arithmetic} seconds, clamped to 300 seconds."
        )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _finite_remaining_seconds(context: DecisionContext) -> float | None:
    data = context.latest_result.data
    if not isinstance(data, Mapping):
        return None
    clock = data.get("clock")
    clock_values = clock if isinstance(clock, Mapping) else {}
    raw = clock_values.get("remaining_seconds")
    if raw is None:
        start = _timestamp(clock_values.get("simulation_time") or data.get("simulation_time"))
        end = _timestamp(clock_values.get("simulation_ends_at") or data.get("simulation_ends_at"))
        if start is None or end is None:
            return None
        try:
            remaining = (end - start).total_seconds()
        except TypeError:
            return None
    elif isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    else:
        try:
            remaining = float(raw)
        except (OverflowError, ValueError):
            return None
    if not math.isfinite(remaining) or remaining <= 0:
        return None
    return remaining


def calculate_v2_time_budget(context: DecisionContext) -> V2TimeBudgetPlan | None:
    """Calculate a bounded wait floor from only public clock/context values."""

    remaining_seconds = _finite_remaining_seconds(context)
    remaining_decisions = context.max_steps - context.iteration + 1
    if remaining_seconds is None or remaining_decisions <= 0:
        return None
    wait_slots = max(1, remaining_decisions // 2)
    minimum_duration_seconds = max(300, math.ceil(remaining_seconds / wait_slots))
    return V2TimeBudgetPlan(
        remaining_seconds=remaining_seconds,
        remaining_decisions=remaining_decisions,
        wait_slots=wait_slots,
        minimum_duration_seconds=minimum_duration_seconds,
    )


def _has_pending_operation(context: DecisionContext) -> bool:
    pending = {"accepted", "pending", "queued", "running"}
    return any(
        status.lower() in pending for status in context.run_state.operation_statuses.values()
    )


def _terminal_context(context: DecisionContext) -> bool:
    return context.latest_result.terminal


def _policy_context(
    context: DecisionContext,
    plan: V2TimeBudgetPlan | None,
    *,
    pending_operations: bool,
) -> DecisionContext:
    data = dict(context.latest_result.data)
    if plan is None:
        metadata: dict[str, Any] = {
            "policy_id": V2_TIME_BUDGET_POLICY_ID,
            "policy_version": V2_TIME_BUDGET_POLICY_VERSION,
            "time_budget": {
                "available": False,
                "hint": (
                    "No finite positive public clock is available; choose the typed action "
                    "unchanged."
                ),
            },
        }
    else:
        metadata = plan.metadata(pending_operations=pending_operations)
    data["runtime_policy"] = metadata
    latest_result = context.latest_result.model_copy(update={"data": data})
    return context.model_copy(update={"latest_result": latest_result})


def _runtime_note(*, proposed: int, effective: int) -> str:
    return (
        f"[runtime-policy id={V2_TIME_BUDGET_POLICY_ID} "
        f"version={V2_TIME_BUDGET_POLICY_VERSION}: "
        f"proposed_duration_seconds={proposed}; effective_duration_seconds={effective}]"
    )


def _annotate(decision: NextStep, *, proposed: int, effective: int) -> NextStep:
    note = _runtime_note(proposed=proposed, effective=effective)
    available = max(0, 1000 - len(note) - 1)
    situation = f"{decision.current_situation[:available]} {note}"
    return decision.model_copy(
        update={
            "current_situation": situation,
            "action": decision.action.model_copy(update={"duration_seconds": effective}),
        }
    )


class SimulatorV2TimeBudgetPolicy:
    """Wrap a structured v2 model with auditable horizon-aware waits."""

    policy_id = V2_TIME_BUDGET_POLICY_ID
    policy_version = V2_TIME_BUDGET_POLICY_VERSION

    def __init__(self, delegate: DecisionModelDelegate) -> None:
        self._delegate = delegate

    @property
    def model(self) -> Any:
        return getattr(self._delegate, "model", None)

    @property
    def response_model(self) -> Any:
        return getattr(self._delegate, "response_model", None)

    @property
    def system_prompt(self) -> Any:
        return getattr(self._delegate, "system_prompt", None)

    @property
    def last_telemetry(self) -> Any:
        return getattr(self._delegate, "last_telemetry", None)

    async def decide(self, context: DecisionContext) -> NextStep:
        plan = calculate_v2_time_budget(context)
        pending_operations = _has_pending_operation(context)
        decision = await self._delegate.decide(
            _policy_context(context, plan, pending_operations=pending_operations)
        )
        action = decision.action
        if (
            plan is None
            or _terminal_context(context)
            or not isinstance(action, V2AdvanceTime)
            or action.stop_when is None
            or action.duration_seconds >= plan.minimum_duration_seconds
            or pending_operations
        ):
            return decision
        return _annotate(
            decision,
            proposed=action.duration_seconds,
            effective=plan.minimum_duration_seconds,
        )

    def prompt_trace(self, context: DecisionContext) -> dict[str, Any]:
        plan = calculate_v2_time_budget(context)
        pending_operations = _has_pending_operation(context)
        trace = self._delegate.prompt_trace(
            _policy_context(context, plan, pending_operations=pending_operations)
        )
        if not isinstance(trace, dict):
            raise TypeError("decision model prompt_trace must return a JSON object")
        return dict(trace)

    async def aclose(self) -> None:
        await self._delegate.aclose()
