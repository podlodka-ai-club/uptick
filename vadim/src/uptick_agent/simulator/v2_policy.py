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
from uptick_agent.simulator.decisions import SimulatorV2BatchDecision
from uptick_agent.simulator.timestamps import TimestampOrder, parse_rfc3339

V2_TIME_BUDGET_POLICY_ID = "simulator-v2-time-budget"
V2_TIME_BUDGET_POLICY_VERSION = "1.6"
V2_BOUNDED_NO_STOP_POLICY_VERSION = "1.7-bounded-no-stop"
_SLO_DOWNTIME_FRACTION = 0.01
_PUBLIC_HORIZON_SECONDS = 604_800
_BOUNDED_NO_STOP_MIN_DURATION_SECONDS = 300
_BOUNDED_NO_STOP_RESERVE_SECONDS = 600
_PENDING_MAX_DURATION_SECONDS = 300
_PENDING_RESERVED_WAITS = 2
_PENDING_OBSERVATION_ACTIONS = frozenset({"get_metrics", "get_overview", "query_metrics"})
_CACHED_OBSERVATION_ACTIONS = frozenset(
    {"get_metrics", "get_resources", "get_overview", "query_metrics"}
)
_ACTIVE_OPERATION_STATUSES = frozenset({"accepted", "pending", "queued", "running"})
_SUPPORTED_TERMINAL_OPERATION_STATUSES = frozenset({"succeeded", "failed"})
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

    def metadata(
        self,
        *,
        policy_version: str = V2_TIME_BUDGET_POLICY_VERSION,
        unresolved_operation_evidence: bool = False,
        latest_result_reports_pending: bool = False,
    ) -> dict[str, Any]:
        return {
            "policy_id": V2_TIME_BUDGET_POLICY_ID,
            "policy_version": policy_version,
            "time_budget": {
                "clock_remaining_seconds": self.remaining_seconds,
                "remaining_decisions": self.remaining_decisions,
                "wait_slots": self.wait_slots,
                "minimum_duration_seconds": self.minimum_duration_seconds,
                "unresolved_operation_evidence": unresolved_operation_evidence,
                "latest_result_reports_pending": latest_result_reports_pending,
                "hint": _operation_hint(
                    self.hint,
                    unresolved_operation_evidence=unresolved_operation_evidence,
                    latest_result_reports_pending=latest_result_reports_pending,
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


def _clock_payload(data: object) -> object:
    """API0.8 time operations expose the clock inside their completed result."""
    if isinstance(data, Mapping) and data.get("type") == "time.advance":
        return data.get("result") if data.get("status") == "succeeded" else None
    return data


def _clock_observation(data: object) -> tuple[TimestampOrder, float] | None:
    """Return the exact public clock and finite remaining horizon."""

    data = _clock_payload(data)
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


def _clock_remaining_from_end(data: object) -> float | None:
    """Return the public remaining horizon derived from both clock endpoints."""

    data = _clock_payload(data)
    if not isinstance(data, Mapping):
        return None
    clock = data.get("clock")
    if not isinstance(clock, Mapping):
        return None
    start = _exact_timestamp(clock.get("simulation_time") or data.get("simulation_time"))
    end = _exact_timestamp(clock.get("simulation_ends_at") or data.get("simulation_ends_at"))
    if start is None or end is None:
        return None
    derived_remaining = _elapsed_seconds(end, start)
    if derived_remaining is None:
        return None
    raw_remaining = clock.get("remaining_seconds")
    if isinstance(raw_remaining, bool) or not isinstance(raw_remaining, (int, float)):
        return None
    try:
        numeric_remaining = float(raw_remaining)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(numeric_remaining) or numeric_remaining < 0:
        return None
    if not math.isclose(derived_remaining, numeric_remaining, abs_tol=1e-6, rel_tol=0.0):
        return None
    return numeric_remaining


def _cached_no_stop_observation(
    context: DecisionContext,
) -> (
    tuple[
        dict[str, float] | None,
        str | None,
        bool,
        TimestampOrder | None,
        float | None,
        float | None,
        str | None,
    ]
    | None
):
    """Select one validated metric view from the environment's same-run cache."""

    current_clock = _current_public_clock(context)
    cached = _cached_observations(context)
    if current_clock is None or cached is None:
        return None
    current_at, current_remaining = current_clock
    candidates: list[dict[str, Any]] = []
    for action_kind, raw_entry in cached.items():
        if action_kind not in _CACHED_OBSERVATION_ACTIONS:
            continue
        if not isinstance(raw_entry, Mapping):
            return (None, None, False, None, None, None, "invalid_cached_view")
        if raw_entry.get("action_kind") != action_kind:
            return (None, None, False, None, None, None, "invalid_cached_view")
        source_run_id = raw_entry.get("run_id")
        if source_run_id is not None and source_run_id != context.run_id:
            return (None, None, False, None, None, None, "wrong_run_observation")

        observed_at = _exact_timestamp(raw_entry.get("observed_at"))
        observation = _clock_observation(raw_entry.get("data"))
        if observed_at is None or observation is None:
            return (None, None, False, None, None, None, "invalid_cached_view")
        data_at, measurement_remaining = observation
        if data_at != observed_at:
            return (None, None, False, None, None, None, "timestamp_mismatch")
        if observed_at > current_at:
            return (None, None, False, None, None, None, "future_observation")

        stale = raw_entry.get("stale")
        freshness = raw_entry.get("freshness")
        if not isinstance(stale, bool) or freshness not in {"observed", "stale"}:
            return (None, None, False, None, None, None, "invalid_cached_view")
        if stale != (freshness == "stale"):
            return (None, None, False, None, None, None, "invalid_cached_view")
        if (observed_at < current_at) != stale:
            return (None, None, False, None, None, None, "stale_marker_mismatch")

        values, reason = _extract_slo_values(raw_entry.get("objective_metrics"))
        if reason != "unknown_metrics" and values is None:
            return (None, None, False, None, None, None, reason)
        candidates.append(
            {
                "action_kind": action_kind,
                "observed_at": observed_at,
                "remaining": measurement_remaining,
                "values": values,
                "reason": reason,
                "stale": stale,
            }
        )

    valid = [candidate for candidate in candidates if candidate["values"] is not None]
    if not valid:
        return (None, None, False, None, None, None, "unknown_metrics")
    valid.sort(key=lambda candidate: candidate["observed_at"], reverse=True)
    selected = valid[0]
    selected_at = selected["observed_at"]
    same_time = [candidate for candidate in valid if candidate["observed_at"] == selected_at]
    if any(candidate["values"] != selected["values"] for candidate in same_time[1:]):
        return (None, None, False, None, None, None, "ambiguous_observations")
    if any(
        candidate["observed_at"] > selected_at and candidate["values"] is None
        for candidate in candidates
    ):
        return (None, None, False, None, None, None, "newer_incompatible_observation")

    elapsed = _elapsed_seconds(current_at, selected_at)
    if elapsed is None:
        return (None, None, False, None, None, None, "future_observation")
    remaining_drop = selected["remaining"] - current_remaining
    if current_remaining > selected["remaining"]:
        return (None, None, False, None, None, None, "clock_horizon_regressed")
    if not math.isclose(remaining_drop, elapsed, abs_tol=1e-6, rel_tol=0.0):
        return (None, None, False, None, None, None, "clock_horizon_mismatch")

    values = selected["values"]
    assert isinstance(values, dict)
    source_horizon = values["observed_seconds"] + selected["remaining"]
    latest_data = _clock_payload(context.latest_result.data)
    latest_clock = latest_data.get("clock") if isinstance(latest_data, Mapping) else None
    has_end = isinstance(latest_clock, Mapping) and (
        "simulation_ends_at" in latest_clock or "simulation_ends_at" in latest_data
    )
    if has_end:
        current_remaining_from_end = _clock_remaining_from_end(latest_data)
        if current_remaining_from_end is None or not math.isclose(
            current_remaining, current_remaining_from_end, abs_tol=1e-6, rel_tol=0.0
        ):
            return (None, None, False, None, None, None, "horizon_mismatch")
    if not math.isfinite(source_horizon) or source_horizon < 0:
        return (None, None, False, None, None, None, "invalid_metrics")
    return (
        values,
        f"cached_{selected['action_kind']}",
        bool(selected["stale"] or elapsed > 0),
        selected_at,
        elapsed,
        selected["remaining"],
        None,
    )


def _cached_observations(context: DecisionContext) -> Mapping[str, Any] | None:
    state = _mapping(getattr(context, "run_state", None))
    if state is None:
        return None
    cached = state.get("last_observed")
    if cached is None:
        cached = state.get("last_observed_views")
    return cached if isinstance(cached, Mapping) else None


def _slo_headroom_eligibility(
    context: DecisionContext,
    *,
    duration_seconds: int,
    require_pending: bool,
    required_reserved_headroom_seconds: float,
    strict_current_run: bool = False,
    required_horizon_seconds: float | None = None,
    success_reason: str,
) -> dict[str, Any]:
    """Prove conservative SLO headroom from typed and environment-owned views.

    Cached observations are treated as stale evidence: every second since the
    observation is charged as downtime.  The allowance always comes from the
    observation's own horizon, while the current clock only proves that the
    requested wait still fits before the run ends.
    """

    evidence: dict[str, Any] = {
        "eligible": False,
        "reason": "unknown_metrics",
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
        "required_reserved_headroom_seconds": required_reserved_headroom_seconds,
    }
    pending = _has_unresolved_operation_evidence(context) or _latest_result_reports_pending(context)
    if require_pending and not pending:
        evidence["reason"] = "not_pending"
        return evidence
    if require_pending and duration_seconds != _PENDING_MAX_DURATION_SECONDS:
        evidence["reason"] = "duration_exceeds_pending_bound"
        return evidence
    if strict_current_run and _terminal_context(context):
        evidence["reason"] = "terminal_context"
        return evidence
    latest_result = context.latest_result
    latest_data = _mapping(getattr(latest_result, "data", None))
    if strict_current_run:
        if getattr(latest_result, "ok", True) is not True:
            evidence["reason"] = "failed_latest_result"
            return evidence
        if latest_data is None:
            evidence["reason"] = "missing_current_run_id"
            return evidence
        if "run_id" in latest_data:
            source_run_id = latest_data["run_id"]
            if not isinstance(source_run_id, str) or not source_run_id:
                evidence["reason"] = "invalid_current_run_id"
                return evidence
            if source_run_id != context.run_id:
                evidence["reason"] = "wrong_current_run"
                return evidence
        evidence["source_run_id"] = context.run_id

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

    if strict_current_run and latest_data is not None:
        clock_payload = _clock_payload(latest_data)
        clock = clock_payload.get("clock") if isinstance(clock_payload, Mapping) else None
        if isinstance(clock, Mapping) and (
            "simulation_ends_at" in clock or "simulation_ends_at" in clock_payload
        ):
            remaining_from_end = _clock_remaining_from_end(latest_data)
            if remaining_from_end is None or not math.isclose(
                current_remaining, remaining_from_end, abs_tol=1e-6, rel_tol=0.0
            ):
                evidence["reason"] = "current_clock_horizon_mismatch"
                return evidence

    measurements: list[dict[str, Any]] = []
    latest_metrics = getattr(latest_result, "objective_metrics", None)
    latest_action_kind = getattr(latest_result, "action_kind", None)
    if (
        latest_action_kind in _PENDING_OBSERVATION_ACTIONS
        and getattr(latest_result, "ok", True) is not True
    ):
        evidence["reason"] = "failed_latest_observation"
        return evidence
    latest_values, latest_reason = _extract_slo_values(latest_metrics)
    if (
        strict_current_run
        and latest_values is not None
        and latest_action_kind not in _PENDING_OBSERVATION_ACTIONS
    ):
        evidence["reason"] = "invalid_latest_observation"
        return evidence
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
            if strict_current_run and entry.get("run_id") != context.run_id:
                evidence["reason"] = (
                    "missing_cached_run_id"
                    if entry.get("run_id") is None
                    else "wrong_run_observation"
                )
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
    same_time = [item for item in measurements if item["observed_at"] == selected["observed_at"]]
    if any(
        item["remaining"] != selected["remaining"] or item["values"] != selected["values"]
        for item in same_time[1:]
    ):
        evidence["reason"] = "ambiguous_observations"
        return evidence

    observed_at = selected["observed_at"]
    elapsed = _elapsed_seconds(current_at, observed_at)
    if elapsed is None:
        evidence["reason"] = (
            "future_observation" if strict_current_run else "clock_before_observation"
        )
        return evidence
    if strict_current_run and bool(selected["stale"]) != (elapsed > 0):
        evidence["reason"] = "stale_marker_mismatch"
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
    source_horizon = observed_seconds + selected["remaining"]
    allowance = _SLO_DOWNTIME_FRACTION * source_horizon
    downtime_upper_bound = downtime_seconds + elapsed
    reserved_headroom = allowance - downtime_upper_bound
    if not all(
        math.isfinite(value) for value in (allowance, downtime_upper_bound, reserved_headroom)
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
    if required_horizon_seconds is not None and not math.isclose(
        source_horizon, required_horizon_seconds, abs_tol=1e-6, rel_tol=0.0
    ):
        evidence["source_horizon_seconds"] = source_horizon
        evidence["reason"] = "measurement_horizon_mismatch"
        return evidence
    if required_horizon_seconds is not None:
        evidence["source_horizon_seconds"] = source_horizon
    if current_remaining < duration_seconds:
        evidence["reason"] = "insufficient_clock_budget"
        return evidence
    if reserved_headroom < required_reserved_headroom_seconds:
        evidence["reason"] = "insufficient_slo_headroom"
        return evidence
    evidence["eligible"] = True
    evidence["reason"] = success_reason
    return evidence


def _pending_no_stop_eligibility(
    context: DecisionContext,
    *,
    duration_seconds: int = _PENDING_MAX_DURATION_SECONDS,
) -> dict[str, Any]:
    return _slo_headroom_eligibility(
        context,
        duration_seconds=duration_seconds,
        require_pending=True,
        required_reserved_headroom_seconds=_PENDING_RESERVED_WAITS * duration_seconds,
        success_reason="pending_slo_headroom_verified",
    )


def _bounded_no_stop_eligibility(context: DecisionContext) -> dict[str, Any]:
    evidence = _slo_headroom_eligibility(
        context,
        duration_seconds=_BOUNDED_NO_STOP_MIN_DURATION_SECONDS,
        require_pending=False,
        required_reserved_headroom_seconds=(
            _BOUNDED_NO_STOP_MIN_DURATION_SECONDS + _BOUNDED_NO_STOP_RESERVE_SECONDS
        ),
        strict_current_run=True,
        required_horizon_seconds=_PUBLIC_HORIZON_SECONDS,
        success_reason="bounded_slo_headroom_verified",
    )
    evidence.update(
        {
            "enabled": True,
            "fixed_reserve_seconds": _BOUNDED_NO_STOP_RESERVE_SECONDS,
            "minimum_duration_seconds": _BOUNDED_NO_STOP_MIN_DURATION_SECONDS,
            "maximum_duration_seconds": None,
        }
    )
    evidence.setdefault("source_run_id", None)
    if _has_unresolved_operation_evidence(context) or _latest_result_reports_pending(context):
        evidence["eligible"] = False
        evidence["reason"] = (
            "latest_operation_pending"
            if _latest_result_reports_pending(context)
            else "unresolved_operation"
        )
        evidence["maximum_duration_seconds"] = None
        return evidence
    reserved_headroom = evidence.get("reserved_headroom_seconds")
    current_remaining = evidence.get("current_remaining_seconds")
    if (
        isinstance(reserved_headroom, (int, float))
        and not isinstance(reserved_headroom, bool)
        and isinstance(current_remaining, (int, float))
        and not isinstance(current_remaining, bool)
    ):
        evidence["maximum_duration_seconds"] = max(
            0.0,
            min(
                current_remaining,
                reserved_headroom - _BOUNDED_NO_STOP_RESERVE_SECONDS,
            ),
        )
    return evidence


def _bounded_no_stop_allows(evidence: Mapping[str, Any], duration_seconds: int) -> bool:
    maximum = evidence.get("maximum_duration_seconds")
    return (
        evidence.get("eligible") is True
        and duration_seconds >= _BOUNDED_NO_STOP_MIN_DURATION_SECONDS
        and isinstance(maximum, (int, float))
        and not isinstance(maximum, bool)
        and duration_seconds <= maximum
    )


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
    data = _clock_payload(context.latest_result.data)
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

    A current typed metric result is preferred.  When the latest result is a
    non-observation, one environment-owned cached observation may be used only
    after its public timestamp and horizon reconcile with the current clock.
    Cached downtime is a lower bound: it can prove an already failed SLO, but
    it cannot prove recoverable headroom.
    """

    remaining_seconds = _finite_remaining_seconds(context, allow_zero=True)
    evidence: dict[str, Any] = {
        "eligible": False,
        "reason": "unknown_clock" if remaining_seconds is None else "unknown_metrics",
        "downtime_seconds": None,
        "observed_seconds": None,
        "remaining_seconds": remaining_seconds,
        "allowance_seconds": None,
        "source": None,
        "stale": None,
        "observed_at": None,
        "elapsed_seconds": None,
        "downtime_lower_bound_seconds": None,
        "measurement_remaining_seconds": None,
        "source_horizon_seconds": None,
    }
    if remaining_seconds is None:
        return evidence

    latest_result = context.latest_result
    latest_metrics = getattr(latest_result, "objective_metrics", ())
    latest_values, latest_reason = _extract_slo_values(latest_metrics)
    latest_action_kind = getattr(latest_result, "action_kind", None)

    # Preserve the existing fail-closed boundary for a typed observation that
    # is present but malformed or incomplete.  A cache is only a fallback for
    # an absent metric set on a non-observation result.
    if latest_values is not None:
        values = latest_values
        source = "latest_typed_metrics"
        stale = False
        observed_at = None
        elapsed = None
        allowance_remaining = remaining_seconds
    elif latest_metrics or latest_action_kind in _PENDING_OBSERVATION_ACTIONS:
        evidence["reason"] = latest_reason
        return evidence
    elif getattr(latest_result, "ok", True) is not True:
        evidence["reason"] = "failed_latest_result"
        return evidence
    elif _latest_result_reports_pending(context):
        # A cached terminal proof must not broaden the pending-operation wait
        # exception. Preserve the separate bounded pending eligibility path.
        evidence["reason"] = "latest_operation_pending"
        return evidence
    else:
        cached = _cached_no_stop_observation(context)
        if cached is None:
            evidence["reason"] = (
                "unknown_clock"
                if _cached_observations(context) is not None
                and _current_public_clock(context) is None
                else "unknown_metrics"
            )
            return evidence
        values, source, stale, observed_at, elapsed, allowance_remaining, cache_reason = cached
        if cache_reason is not None:
            evidence["reason"] = cache_reason
            return evidence
        if values is None or source is None or observed_at is None or elapsed is None:
            evidence["reason"] = "unknown_metrics"
            return evidence
        if allowance_remaining is None:
            evidence["reason"] = "unknown_metrics"
            return evidence

    downtime_seconds = values["downtime_seconds"]
    observed_seconds = values["observed_seconds"]
    total_seconds = observed_seconds + (
        allowance_remaining if latest_values is None else remaining_seconds
    )
    allowance_seconds = _SLO_DOWNTIME_FRACTION * total_seconds
    if not math.isfinite(total_seconds) or not math.isfinite(allowance_seconds):
        evidence["reason"] = "invalid_metrics"
        return evidence
    evidence.update(
        {
            "downtime_seconds": downtime_seconds,
            "observed_seconds": observed_seconds,
            "allowance_seconds": allowance_seconds,
            "source": source,
            "stale": stale,
            "observed_at": _timestamp_json(observed_at) if observed_at is not None else None,
            "elapsed_seconds": elapsed,
            "downtime_lower_bound_seconds": downtime_seconds,
            "measurement_remaining_seconds": allowance_remaining,
            "source_horizon_seconds": total_seconds,
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


def _operation_statuses(context: DecisionContext) -> Mapping[Any, Any] | None:
    state = context.run_state
    state_mapping = _mapping(state)
    statuses = (
        state_mapping.get("operation_statuses", {})
        if state_mapping is not None
        else getattr(state, "operation_statuses", {})
    )
    return statuses if isinstance(statuses, Mapping) else None


def _has_unresolved_operation_evidence(context: DecisionContext) -> bool:
    """Return whether public operation state is unresolved or unrecognised.

    ``operation_statuses`` is a last-observed environment view.  Keeping an
    accepted/active value here must not be mistaken for a fresh observation,
    but treating an unknown value as terminal would be an unsafe inference.
    """

    statuses = _operation_statuses(context)
    if statuses is None:
        return True
    return any(
        not isinstance(status, str) or status.lower() not in _SUPPORTED_TERMINAL_OPERATION_STATUSES
        for status in statuses.values()
    )


def _latest_result_reports_pending(context: DecisionContext) -> bool:
    """Return whether the latest successful result reports an active operation."""

    result = context.latest_result
    if getattr(result, "ok", True) is not True:
        return False
    data = getattr(result, "data", None)
    if not isinstance(data, Mapping):
        return False
    raw_status = data.get("status")
    if not isinstance(raw_status, str):
        return False
    status = raw_status.lower()
    links = getattr(result, "operation_links", ())
    if not isinstance(links, (list, tuple)):
        return False
    for link in links:
        relation = getattr(link, "relation", None)
        operation_id = getattr(link, "operation_id", None)
        if not isinstance(operation_id, str) or not operation_id:
            continue
        if relation in {"initiated", "observed"} and status in _ACTIVE_OPERATION_STATUSES:
            return True
    return False


def _operation_hint(
    default: str,
    *,
    unresolved_operation_evidence: bool,
    latest_result_reports_pending: bool,
) -> str:
    if latest_result_reports_pending:
        return (
            "The latest successful result reports an active operation; inspect its status "
            "before relying on completion and retain the model's proposed interval."
        )
    if unresolved_operation_evidence:
        return (
            "A prior result left an operation nonterminal or unknown, but the latest result "
            "does not report its status. Treat it as unresolved and do not claim completion; "
            "keep the model's bounded interval and use the separate SLO evidence to assess "
            "a no-stop wait."
        )
    return default


def _terminal_context(context: DecisionContext) -> bool:
    return context.latest_result.terminal


def _policy_context(
    context: DecisionContext,
    plan: V2TimeBudgetPlan | None,
    *,
    policy_version: str = V2_TIME_BUDGET_POLICY_VERSION,
    unresolved_operation_evidence: bool,
    latest_result_reports_pending: bool,
    no_stop_eligibility: dict[str, Any],
    pending_no_stop_eligibility: dict[str, Any],
    bounded_no_stop_eligibility: dict[str, Any] | None = None,
) -> DecisionContext:
    data = dict(context.latest_result.data)
    if plan is None:
        metadata: dict[str, Any] = {
            "policy_id": V2_TIME_BUDGET_POLICY_ID,
            "policy_version": policy_version,
            "time_budget": {
                "available": False,
                "unresolved_operation_evidence": unresolved_operation_evidence,
                "latest_result_reports_pending": latest_result_reports_pending,
                "hint": _operation_hint(
                    "No duration floor is available from the public clock and decision "
                    "budget; "
                    "retain the default first-new-error stop unless full-horizon SLO "
                    "evidence verifies that the run is unrecoverable.",
                    unresolved_operation_evidence=unresolved_operation_evidence,
                    latest_result_reports_pending=latest_result_reports_pending,
                ),
            },
        }
    else:
        metadata = plan.metadata(
            policy_version=policy_version,
            unresolved_operation_evidence=unresolved_operation_evidence,
            latest_result_reports_pending=latest_result_reports_pending,
        )
    metadata["no_stop_eligibility"] = no_stop_eligibility
    metadata["pending_no_stop_eligibility"] = pending_no_stop_eligibility
    if bounded_no_stop_eligibility is not None:
        metadata["bounded_no_stop_eligibility"] = bounded_no_stop_eligibility
    if latest_result_reports_pending:
        metadata["time_budget"]["hint"] = (
            "The latest result reports an active operation with verified headroom for one "
            "bounded 300-second advance with stop_when=None; inspect its result before the "
            "next decision."
            if pending_no_stop_eligibility["eligible"]
            else "The latest result reports an active operation; obtain a fresh get_metrics "
            "observation before a bounded wait and retain the default first-error stop "
            "until public SLO headroom is verified."
        )
    # API0.8 distinguishes a real server calculation from a simulated control
    # operation. The former must be polled; another advance cannot complete it.
    pending_time = data.get("status") in {"queued", "running"} and (
        data.get("type") == "time.advance"
        or (
            context.latest_result.action_kind in {"advance_time", "advance_time_v2"}
            and isinstance(data.get("operation_id"), str)
        )
    )
    if pending_time or (data.get("code") == "RUN_BUSY" and data.get("status_code") in (409, 429)):
        details = data.get("details")
        operation_id = data.get("operation_id") or (
            details.get("operation_id") if isinstance(details, Mapping) else None
        )
        metadata["time_budget"]["hint"] = (
            "A server time advance is pending. Poll get_operation for the reported "
            "operation_id; other run requests are blocked until it finishes. "
            "Do not submit another time advance or interpret acceptance as completion."
        )
        metadata["time_budget"]["pending_time_operation_id"] = operation_id
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
    policy_version: str = V2_TIME_BUDGET_POLICY_VERSION,
    proposed: int,
    effective: int,
    proposed_stop: AdvanceTimeStopCondition | None,
    effective_stop: AdvanceTimeStopCondition | None,
) -> str:
    return (
        f"[runtime-policy id={V2_TIME_BUDGET_POLICY_ID} "
        f"version={policy_version}: "
        f"proposed_duration_seconds={proposed}; effective_duration_seconds={effective}; "
        f"proposed_stop_when={_stop_label(proposed_stop)}; "
        f"effective_stop_when={_stop_label(effective_stop)}]"
    )


def _annotate(
    decision: NextStep,
    *,
    policy_version: str = V2_TIME_BUDGET_POLICY_VERSION,
    proposed: int,
    effective: int,
    proposed_stop: AdvanceTimeStopCondition | None,
    effective_stop: AdvanceTimeStopCondition | None,
) -> NextStep:
    note = _runtime_note(
        policy_version=policy_version,
        proposed=proposed,
        effective=effective,
        proposed_stop=proposed_stop,
        effective_stop=effective_stop,
    )
    available = max(0, 1000 - len(note) - 1)
    situation = f"{decision.current_situation[:available]} {note}"
    action = (
        decision.actions[0] if isinstance(decision, SimulatorV2BatchDecision) else decision.action
    )
    effective_action = action.model_copy(
        update={"duration_seconds": effective, "stop_when": effective_stop}
    )
    update = {"current_situation": situation}
    if isinstance(decision, SimulatorV2BatchDecision):
        update["actions"] = [effective_action]
    else:
        update["action"] = effective_action
    return decision.model_copy(update=update)


class SimulatorV2TimeBudgetPolicy:
    """Wrap a structured v2 model with auditable horizon-aware waits."""

    policy_id = V2_TIME_BUDGET_POLICY_ID
    policy_version = V2_TIME_BUDGET_POLICY_VERSION

    def __init__(
        self,
        delegate: DecisionModelDelegate,
        *,
        bounded_no_stop: bool = False,
    ) -> None:
        if not isinstance(bounded_no_stop, bool):
            raise TypeError("bounded_no_stop must be a boolean")
        self._delegate = delegate
        self._bounded_no_stop = bounded_no_stop
        if bounded_no_stop:
            self.policy_version = V2_BOUNDED_NO_STOP_POLICY_VERSION

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
        unresolved_operation_evidence = _has_unresolved_operation_evidence(context)
        latest_result_reports_pending = _latest_result_reports_pending(context)
        no_stop_eligibility = _no_stop_eligibility(context)
        pending_no_stop_eligibility = _pending_no_stop_eligibility(context)
        bounded_no_stop_eligibility = (
            _bounded_no_stop_eligibility(context) if self._bounded_no_stop else None
        )
        decision = await self._delegate.decide(
            _policy_context(
                context,
                plan,
                policy_version=self.policy_version,
                unresolved_operation_evidence=unresolved_operation_evidence,
                latest_result_reports_pending=latest_result_reports_pending,
                no_stop_eligibility=no_stop_eligibility,
                pending_no_stop_eligibility=pending_no_stop_eligibility,
                bounded_no_stop_eligibility=bounded_no_stop_eligibility,
            )
        )
        if isinstance(decision, SimulatorV2BatchDecision):
            # The batch schema requires time advances to stand alone, so all
            # eligibility checks still use the immediately preceding observation.
            decision = SimulatorV2BatchDecision.model_validate(decision.model_dump(mode="json"))
            if len(decision.actions) != 1:
                return decision
            action = decision.actions[0]
        else:
            action = decision.action
        if _terminal_context(context) or not isinstance(action, V2AdvanceTime):
            return decision

        effective_stop = action.stop_when
        if action.stop_when is None:
            pending_eligibility = _pending_no_stop_eligibility(
                context, duration_seconds=action.duration_seconds
            )
            legacy_allowed = bool(
                no_stop_eligibility["eligible"] or pending_eligibility["eligible"]
            )
            if self._bounded_no_stop:
                allowed = bool(
                    legacy_allowed
                    or _bounded_no_stop_allows(
                        bounded_no_stop_eligibility or {}, action.duration_seconds
                    )
                )
            else:
                allowed = legacy_allowed
            if not allowed:
                effective_stop = AdvanceTimeStopCondition()

        effective_duration = action.duration_seconds
        if (
            plan is not None
            and not unresolved_operation_evidence
            and not latest_result_reports_pending
            and effective_stop is not None
            and not (
                self._bounded_no_stop
                and action.stop_when is None
                and not unresolved_operation_evidence
            )
            and action.duration_seconds < plan.minimum_duration_seconds
        ):
            effective_duration = plan.minimum_duration_seconds

        if effective_stop == action.stop_when and effective_duration == action.duration_seconds:
            return decision
        return _annotate(
            decision,
            policy_version=self.policy_version,
            proposed=action.duration_seconds,
            effective=effective_duration,
            proposed_stop=action.stop_when,
            effective_stop=effective_stop,
        )

    def prompt_trace(self, context: DecisionContext) -> dict[str, Any]:
        plan = calculate_v2_time_budget(context)
        unresolved_operation_evidence = _has_unresolved_operation_evidence(context)
        latest_result_reports_pending = _latest_result_reports_pending(context)
        no_stop_eligibility = _no_stop_eligibility(context)
        pending_no_stop_eligibility = _pending_no_stop_eligibility(context)
        bounded_no_stop_eligibility = (
            _bounded_no_stop_eligibility(context) if self._bounded_no_stop else None
        )
        trace = self._delegate.prompt_trace(
            _policy_context(
                context,
                plan,
                policy_version=self.policy_version,
                unresolved_operation_evidence=unresolved_operation_evidence,
                latest_result_reports_pending=latest_result_reports_pending,
                no_stop_eligibility=no_stop_eligibility,
                pending_no_stop_eligibility=pending_no_stop_eligibility,
                bounded_no_stop_eligibility=bounded_no_stop_eligibility,
            )
        )
        if not isinstance(trace, dict):
            raise TypeError("decision model prompt_trace must return a JSON object")
        return dict(trace)

    async def aclose(self) -> None:
        await self._delegate.aclose()
