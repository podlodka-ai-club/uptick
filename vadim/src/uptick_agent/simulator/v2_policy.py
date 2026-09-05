"""Runner-side policy for planning v2 simulation time advances.

The simulator environment executes the typed action it receives. This module
adds the deterministic horizon planning needed at the v2 CLI composition
boundary, where the public clock and runner decision budget are both available.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from uptick_agent.decisions.contracts import DecisionContext, NextStep
from uptick_agent.simulator.actions import AdvanceTimeStopCondition, V2AdvanceTime
from uptick_agent.simulator.timestamps import TimestampOrder, parse_rfc3339

V2_TIME_BUDGET_POLICY_ID = "simulator-v2-time-budget"
V2_TIME_BUDGET_POLICY_VERSION = "1.2"
_SLO_DOWNTIME_FRACTION = 0.01
_PENDING_MAX_DURATION_SECONDS = 300
_PENDING_RESERVED_WAITS = 2
_PENDING_OBSERVATION_ACTIONS = frozenset({"get_metrics", "get_overview", "query_metrics"})
_SLO_COUNTER_UNITS = {
    "downtime_seconds": "seconds",
    "observed_seconds": "seconds",
}


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


def _exact_timestamp(value: object) -> TimestampOrder | None:
    """Parse a public timestamp without discarding sub-microsecond precision."""

    try:
        return parse_rfc3339(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _elapsed_seconds(current: TimestampOrder, observed: TimestampOrder) -> float | None:
    if current < observed:
        return None
    try:
        elapsed = float(current[0] - observed[0] + current[1] - observed[1])
    except (OverflowError, ValueError):
        return None
    return elapsed if math.isfinite(elapsed) and elapsed >= 0 else None


def _clock_observation(data: object) -> tuple[TimestampOrder, float] | None:
    """Return the exact public clock and finite remaining horizon."""

    if not isinstance(data, Mapping):
        return None
    clock = data.get("clock")
    if not isinstance(clock, Mapping):
        return None
    observed_at = _exact_timestamp(clock.get("simulation_time"))
    raw_remaining = clock.get("remaining_seconds")
    if observed_at is None or isinstance(raw_remaining, bool):
        return None
    if not isinstance(raw_remaining, (int, float)):
        return None
    try:
        remaining = float(raw_remaining)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(remaining) or remaining < 0:
        return None
    return observed_at, remaining


def _mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        return None
    try:
        dumped = model_dump(mode="json")
    except Exception:
        return None
    return dumped if isinstance(dumped, Mapping) else None


def _extract_slo_values(metrics: object) -> tuple[dict[str, float] | None, str]:
    """Validate the two counters needed for a conservative SLO bound."""

    if not isinstance(metrics, (list, tuple)):
        return None, "unknown_metrics"
    selected: dict[str, list[object]] = {name: [] for name in _SLO_COUNTER_UNITS}
    for metric in metrics:
        metric_mapping = _mapping(metric)
        name = (
            metric_mapping.get("name")
            if metric_mapping is not None
            else getattr(metric, "name", None)
        )
        if isinstance(name, str) and name in selected:
            selected[name].append(metric)

    if any(len(items) != 1 for items in selected.values()):
        if any(len(items) > 1 for items in selected.values()):
            return None, "ambiguous_metrics"
        return None, "unknown_metrics"

    values: dict[str, float] = {}
    for name, expected_unit in _SLO_COUNTER_UNITS.items():
        metric = selected[name][0]
        metric_mapping = _mapping(metric)
        unit = (
            metric_mapping.get("unit")
            if metric_mapping is not None
            else getattr(metric, "unit", None)
        )
        if unit != expected_unit:
            return None, "invalid_metric_units"
        value = (
            metric_mapping.get("value")
            if metric_mapping is not None
            else getattr(metric, "value", None)
        )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "invalid_metrics"
        try:
            numeric = float(value)
        except (OverflowError, ValueError):
            return None, "invalid_metrics"
        if not math.isfinite(numeric) or numeric < 0:
            return None, "invalid_metrics"
        values[name] = numeric

    if values["downtime_seconds"] > values["observed_seconds"]:
        return None, "inconsistent_metrics"
    return values, "ok"


def _current_public_clock(context: DecisionContext) -> tuple[TimestampOrder, float] | None:
    return _clock_observation(context.latest_result.data)


def _cached_observations(context: DecisionContext) -> Mapping[str, Any] | None:
    state = _mapping(getattr(context, "run_state", None))
    if state is None:
        return None
    cached = state.get("last_observed")
    if cached is None:
        cached = state.get("last_observed_views")
    return cached if isinstance(cached, Mapping) else None


def _pending_no_stop_eligibility(
    context: DecisionContext,
    *,
    duration_seconds: int = _PENDING_MAX_DURATION_SECONDS,
) -> dict[str, Any]:
    """Prove a single bounded pending wait has conservative SLO headroom.

    Cached observations are treated as stale evidence: every second since the
    observation is charged as downtime.  The allowance always comes from the
    observation's own horizon, while the current clock only proves that the
    requested wait still fits before the run ends.
    """

    evidence: dict[str, Any] = {
        "eligible": False,
        "reason": "not_pending" if not _has_pending_operation(context) else "unknown_metrics",
        "source": None,
        "stale": None,
        "observed_at": None,
        "current_at": None,
        "elapsed_seconds": None,
        "requested_duration_seconds": duration_seconds,
        "downtime_seconds": None,
        "observed_seconds": None,
        "measurement_remaining_seconds": None,
        "current_remaining_seconds": None,
        "allowance_seconds": None,
        "downtime_upper_bound_seconds": None,
        "reserved_headroom_seconds": None,
        "required_reserved_headroom_seconds": _PENDING_RESERVED_WAITS * duration_seconds,
    }
    if not _has_pending_operation(context):
        return evidence
    if duration_seconds != _PENDING_MAX_DURATION_SECONDS:
        evidence["reason"] = "duration_exceeds_pending_bound"
        return evidence

    current_clock = _current_public_clock(context)
    if current_clock is None:
        evidence["reason"] = "unknown_clock"
        return evidence
    current_at, current_remaining = current_clock
    evidence.update(
        {
            "current_at": _timestamp_json(current_at),
            "current_remaining_seconds": current_remaining,
        }
    )

    measurements: list[dict[str, Any]] = []
    latest_result = context.latest_result
    latest_metrics = getattr(latest_result, "objective_metrics", None)
    latest_action_kind = getattr(latest_result, "action_kind", None)
    if (
        latest_action_kind in _PENDING_OBSERVATION_ACTIONS
        and getattr(latest_result, "ok", True) is not True
    ):
        evidence["reason"] = "failed_latest_observation"
        return evidence
    latest_values, latest_reason = _extract_slo_values(latest_metrics)
    if latest_values is not None:
        latest_observation = _clock_observation(latest_result.data)
        if latest_observation is None:
            evidence["reason"] = "unknown_clock"
            return evidence
        observed_at, remaining = latest_observation
        measurements.append(
            {
                "source": "latest_typed_metrics",
                "stale": False,
                "observed_at": observed_at,
                "remaining": remaining,
                "values": latest_values,
            }
        )
    elif latest_action_kind in _PENDING_OBSERVATION_ACTIONS:
        # A typed observation that is absent or malformed must not silently
        # fall back to an older cache.
        evidence["reason"] = latest_reason
        return evidence

    cached = _cached_observations(context)
    if cached is not None:
        for key in _PENDING_OBSERVATION_ACTIONS:
            if key not in cached:
                continue
            entry = cached[key]
            if not isinstance(entry, Mapping):
                evidence["reason"] = "invalid_cached_view"
                return evidence
            if entry.get("action_kind") != key:
                evidence["reason"] = "invalid_cached_view"
                return evidence
            observed_at = _exact_timestamp(entry.get("observed_at"))
            data = entry.get("data")
            observation = _clock_observation(data)
            if observed_at is None or observation is None:
                evidence["reason"] = "invalid_cached_view"
                return evidence
            data_at, remaining = observation
            if data_at != observed_at:
                evidence["reason"] = "timestamp_mismatch"
                return evidence
            stale = entry.get("stale")
            freshness = entry.get("freshness")
            if not isinstance(stale, bool) or freshness not in {"observed", "stale"}:
                evidence["reason"] = "invalid_cached_view"
                return evidence
            if stale != (freshness == "stale"):
                evidence["reason"] = "invalid_cached_view"
                return evidence
            values, reason = _extract_slo_values(entry.get("objective_metrics"))
            if values is None:
                evidence["reason"] = reason
                return evidence
            measurements.append(
                {
                    "source": f"cached_{key}",
                    "stale": stale,
                    "observed_at": observed_at,
                    "remaining": remaining,
                    "values": values,
                }
            )

    if not measurements:
        evidence["reason"] = "unknown_metrics"
        return evidence

    measurements.sort(key=lambda item: item["observed_at"], reverse=True)
    selected = measurements[0]
    same_time = [
        item for item in measurements if item["observed_at"] == selected["observed_at"]
    ]
    if any(
        item["remaining"] != selected["remaining"] or item["values"] != selected["values"]
        for item in same_time[1:]
    ):
        evidence["reason"] = "ambiguous_observations"
        return evidence

    observed_at = selected["observed_at"]
    elapsed = _elapsed_seconds(current_at, observed_at)
    if elapsed is None:
        evidence["reason"] = "clock_before_observation"
        return evidence
    remaining_drop = selected["remaining"] - current_remaining
    if current_remaining > selected["remaining"]:
        evidence["reason"] = "clock_horizon_regressed"
        return evidence
    if not math.isclose(remaining_drop, elapsed, abs_tol=1e-6, rel_tol=0.0):
        evidence["reason"] = "clock_horizon_mismatch"
        return evidence

    values = selected["values"]
    downtime_seconds = values["downtime_seconds"]
    observed_seconds = values["observed_seconds"]
    allowance = _SLO_DOWNTIME_FRACTION * (observed_seconds + selected["remaining"])
    downtime_upper_bound = downtime_seconds + elapsed
    reserved_headroom = allowance - downtime_upper_bound
    if not all(
        math.isfinite(value)
        for value in (allowance, downtime_upper_bound, reserved_headroom)
    ):
        evidence["reason"] = "invalid_metrics"
        return evidence
    evidence.update(
        {
            "source": selected["source"],
            "stale": bool(selected["stale"] or elapsed > 0),
            "observed_at": _timestamp_json(observed_at),
            "elapsed_seconds": elapsed,
            "downtime_seconds": downtime_seconds,
            "observed_seconds": observed_seconds,
            "measurement_remaining_seconds": selected["remaining"],
            "allowance_seconds": allowance,
            "downtime_upper_bound_seconds": downtime_upper_bound,
            "reserved_headroom_seconds": reserved_headroom,
        }
    )
    if current_remaining < duration_seconds:
        evidence["reason"] = "insufficient_clock_budget"
        return evidence
    if reserved_headroom < _PENDING_RESERVED_WAITS * duration_seconds:
        evidence["reason"] = "insufficient_slo_headroom"
        return evidence
    evidence["eligible"] = True
    evidence["reason"] = "pending_slo_headroom_verified"
    return evidence


def _timestamp_json(value: TimestampOrder) -> str:
    """Render exact timestamp evidence using a stable JSON-safe UTC value."""

    seconds, fraction = value
    # Metadata only needs an unambiguous instant; avoid datetime's six-digit
    # limit by retaining the exact decimal fraction ourselves.
    base = datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
    if not fraction:
        return f"{base}Z"
    denominator = fraction.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        return f"{base}.{fraction}Z"
    digits = max(twos, fives)
    scaled = fraction.numerator * (2 ** (digits - twos)) * (5 ** (digits - fives))
    fraction_text = f"{scaled:0{digits}d}".rstrip("0")
    return f"{base}.{fraction_text}Z"


def _finite_remaining_seconds(
    context: DecisionContext, *, allow_zero: bool = False
) -> float | None:
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
    if not math.isfinite(remaining) or remaining < 0 or (remaining == 0 and not allow_zero):
        return None
    return remaining


def _no_stop_eligibility(context: DecisionContext) -> dict[str, Any]:
    """Return auditable evidence for disabling the default error stop.

    Only typed metrics and the clock from the same latest response qualify.  A
    missing, malformed, unit-mismatched, or ambiguous counter leaves the
    default stop condition in force.
    """

    remaining_seconds = _finite_remaining_seconds(context, allow_zero=True)
    evidence: dict[str, Any] = {
        "eligible": False,
        "reason": "unknown_clock" if remaining_seconds is None else "unknown_metrics",
        "downtime_seconds": None,
        "observed_seconds": None,
        "remaining_seconds": remaining_seconds,
        "allowance_seconds": None,
    }
    if remaining_seconds is None:
        return evidence

    metrics = context.latest_result.objective_metrics
    selected: dict[str, list[object]] = {name: [] for name in _SLO_COUNTER_UNITS}
    for metric in metrics:
        name = getattr(metric, "name", None)
        if name in selected:
            selected[name].append(metric)

    if any(len(items) != 1 for items in selected.values()):
        if any(len(items) > 1 for items in selected.values()):
            evidence["reason"] = "ambiguous_metrics"
        return evidence

    values: dict[str, float] = {}
    for name, expected_unit in _SLO_COUNTER_UNITS.items():
        metric = selected[name][0]
        if getattr(metric, "unit", None) != expected_unit:
            evidence["reason"] = "invalid_metric_units"
            return evidence
        value = getattr(metric, "value", None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            evidence["reason"] = "invalid_metrics"
            return evidence
        try:
            numeric = float(value)
        except (OverflowError, ValueError):
            evidence["reason"] = "invalid_metrics"
            return evidence
        if not math.isfinite(numeric) or numeric < 0:
            evidence["reason"] = "invalid_metrics"
            return evidence
        values[name] = numeric

    downtime_seconds = values["downtime_seconds"]
    observed_seconds = values["observed_seconds"]
    total_seconds = observed_seconds + remaining_seconds
    allowance_seconds = _SLO_DOWNTIME_FRACTION * total_seconds
    if not math.isfinite(total_seconds) or not math.isfinite(allowance_seconds):
        evidence["reason"] = "invalid_metrics"
        return evidence
    evidence.update(
        {
            "downtime_seconds": downtime_seconds,
            "observed_seconds": observed_seconds,
            "allowance_seconds": allowance_seconds,
        }
    )
    if downtime_seconds > observed_seconds:
        evidence["reason"] = "inconsistent_metrics"
        return evidence
    if downtime_seconds <= allowance_seconds:
        evidence["reason"] = "slo_recoverable"
        return evidence
    evidence["eligible"] = True
    evidence["reason"] = "verified_unrecoverable_slo"
    return evidence


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
    state = context.run_state
    state_mapping = _mapping(state)
    statuses = (
        state_mapping.get("operation_statuses", {})
        if state_mapping is not None
        else getattr(state, "operation_statuses", {})
    )
    if not isinstance(statuses, Mapping):
        return False
    return any(
        isinstance(status, str) and status.lower() in pending for status in statuses.values()
    )


def _terminal_context(context: DecisionContext) -> bool:
    return context.latest_result.terminal


def _policy_context(
    context: DecisionContext,
    plan: V2TimeBudgetPlan | None,
    *,
    pending_operations: bool,
    no_stop_eligibility: dict[str, Any],
    pending_no_stop_eligibility: dict[str, Any],
) -> DecisionContext:
    data = dict(context.latest_result.data)
    if plan is None:
        metadata: dict[str, Any] = {
            "policy_id": V2_TIME_BUDGET_POLICY_ID,
            "policy_version": V2_TIME_BUDGET_POLICY_VERSION,
            "time_budget": {
                "available": False,
                "hint": (
                    "No duration floor is available from the public clock and decision "
                    "budget; "
                    "retain the default first-new-error stop unless full-horizon SLO "
                    "evidence verifies that the run is unrecoverable."
                ),
            },
        }
    else:
        metadata = plan.metadata(pending_operations=pending_operations)
    metadata["no_stop_eligibility"] = no_stop_eligibility
    metadata["pending_no_stop_eligibility"] = pending_no_stop_eligibility
    if pending_operations:
        metadata["time_budget"]["hint"] = (
            "This pending operation has verified headroom for one bounded 300-second "
            "advance with stop_when=None; inspect its result before the next decision."
            if pending_no_stop_eligibility["eligible"]
            else "Obtain a fresh get_metrics observation before a bounded "
            "pending-operation wait; retain the default first-error stop until public "
            "SLO headroom is verified."
        )
    data["runtime_policy"] = metadata
    latest_result = context.latest_result.model_copy(update={"data": data})
    return context.model_copy(update={"latest_result": latest_result})


def _stop_label(stop_when: AdvanceTimeStopCondition | None) -> str:
    if stop_when is None:
        return "None"
    if stop_when == AdvanceTimeStopCondition():
        return "default"
    return str(stop_when.model_dump(mode="json", exclude_none=True))


def _runtime_note(
    *,
    proposed: int,
    effective: int,
    proposed_stop: AdvanceTimeStopCondition | None,
    effective_stop: AdvanceTimeStopCondition | None,
) -> str:
    return (
        f"[runtime-policy id={V2_TIME_BUDGET_POLICY_ID} "
        f"version={V2_TIME_BUDGET_POLICY_VERSION}: "
        f"proposed_duration_seconds={proposed}; effective_duration_seconds={effective}; "
        f"proposed_stop_when={_stop_label(proposed_stop)}; "
        f"effective_stop_when={_stop_label(effective_stop)}]"
    )


def _annotate(
    decision: NextStep,
    *,
    proposed: int,
    effective: int,
    proposed_stop: AdvanceTimeStopCondition | None,
    effective_stop: AdvanceTimeStopCondition | None,
) -> NextStep:
    note = _runtime_note(
        proposed=proposed,
        effective=effective,
        proposed_stop=proposed_stop,
        effective_stop=effective_stop,
    )
    available = max(0, 1000 - len(note) - 1)
    situation = f"{decision.current_situation[:available]} {note}"
    return decision.model_copy(
        update={
            "current_situation": situation,
            "action": decision.action.model_copy(
                update={
                    "duration_seconds": effective,
                    "stop_when": effective_stop,
                }
            ),
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
        no_stop_eligibility = _no_stop_eligibility(context)
        pending_no_stop_eligibility = _pending_no_stop_eligibility(context)
        decision = await self._delegate.decide(
            _policy_context(
                context,
                plan,
                pending_operations=pending_operations,
                no_stop_eligibility=no_stop_eligibility,
                pending_no_stop_eligibility=pending_no_stop_eligibility,
            )
        )
        action = decision.action
        if _terminal_context(context) or not isinstance(action, V2AdvanceTime):
            return decision

        effective_stop = action.stop_when
        if action.stop_when is None:
            pending_eligibility = _pending_no_stop_eligibility(
                context, duration_seconds=action.duration_seconds
            )
            if not (no_stop_eligibility["eligible"] or pending_eligibility["eligible"]):
                effective_stop = AdvanceTimeStopCondition()

        effective_duration = action.duration_seconds
        if (
            plan is not None
            and not pending_operations
            and effective_stop is not None
            and action.duration_seconds < plan.minimum_duration_seconds
        ):
            effective_duration = plan.minimum_duration_seconds

        if effective_stop == action.stop_when and effective_duration == action.duration_seconds:
            return decision
        return _annotate(
            decision,
            proposed=action.duration_seconds,
            effective=effective_duration,
            proposed_stop=action.stop_when,
            effective_stop=effective_stop,
        )

    def prompt_trace(self, context: DecisionContext) -> dict[str, Any]:
        plan = calculate_v2_time_budget(context)
        pending_operations = _has_pending_operation(context)
        no_stop_eligibility = _no_stop_eligibility(context)
        pending_no_stop_eligibility = _pending_no_stop_eligibility(context)
        trace = self._delegate.prompt_trace(
            _policy_context(
                context,
                plan,
                pending_operations=pending_operations,
                no_stop_eligibility=no_stop_eligibility,
                pending_no_stop_eligibility=pending_no_stop_eligibility,
            )
        )
        if not isinstance(trace, dict):
            raise TypeError("decision model prompt_trace must return a JSON object")
        return dict(trace)

    async def aclose(self) -> None:
        await self._delegate.aclose()
