"""Neutral trace and telemetry normalization for evaluation artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pydantic import BaseModel

from uptick_agent.evaluation.artifacts import _as_json_mapping
from uptick_agent.evaluation.contracts import (
    FrozenEvaluationBinding,
    MemoryTelemetry,
    ProviderTelemetry,
    V2OutcomeMetrics,
)
from uptick_agent.evaluation.lifecycle import EvaluationJournal
from uptick_agent.evaluation.runtime_adapters import _TelemetryModelAdapter, _TraceObserver
from uptick_agent.ports import AgentMemory, DecisionModel
from uptick_agent.runs.results import RunResult


def _outcome(result: RunResult) -> V2OutcomeMetrics:
    status = (
        result.status
        if result.status in {"completed", "failed", "interrupted", "running"}
        else "failed"
    )
    return V2OutcomeMetrics(
        run_status=status,
        uptime_ratio=result.uptime_ratio,
        slo_passed=result.slo_passed,
        total_cost_minor=result.total_cost_minor,
        steps=result.steps,
        duration_seconds=result.duration_seconds,
    )


def _trace_payload(observer: _TraceObserver, model: DecisionModel | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "steps": [item.model_dump(mode="json") for item in observer.steps],
        "finish": observer.result.model_dump(mode="json") if observer.result else None,
    }
    if not observer.steps and observer.result is None:
        payload["trace_status"] = "unavailable"
        payload["model_type"] = type(model).__name__ if model else None
    return payload


def _try_trace_artifact(
    journal: EvaluationJournal,
    attempt_id: str,
    observer: _TraceObserver,
    model: DecisionModel | None,
) -> str | None:
    try:
        return journal.artifacts.put("trace", attempt_id, _trace_payload(observer, model))
    except Exception:
        return None


def _provider_telemetry(
    telemetry_model: _TelemetryModelAdapter | None, model: DecisionModel | None
) -> ProviderTelemetry:
    samples = list(telemetry_model.samples) if telemetry_model is not None else []
    if not samples and model is not None:
        value = getattr(model, "last_telemetry", None)
        if value is not None:
            samples.append(value)
    normalized = [_provider_sample(item) for item in samples]
    normalized = [item for item in normalized if item is not None]
    if not normalized:
        return ProviderTelemetry()
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "time_seconds",
        "cost_minor",
        "request_count",
        "retry_count",
        "usage_reported_requests",
    )
    sums = {
        field: (
            _sum_complete(normalized, field)
            if field
            in {
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
                "cost_minor",
            }
            else _sum_optional(item.get(field) for item in normalized)
        )
        for field in fields
    }
    requests = _sum_complete(normalized, "request_count")
    reported = _sum_complete(normalized, "usage_reported_requests")
    complete = requests is not None and reported is not None and reported >= requests
    currencies = {item.get("cost_currency") for item in normalized}
    cost_currency = next(iter(currencies)) if len(currencies) == 1 else None
    if len(currencies) != 1 or cost_currency is None or sums["cost_minor"] is None:
        sums["cost_minor"] = None
        cost_currency = None
    if not complete:
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "cost_minor",
        ):
            sums[field] = None
        cost_currency = None
    sources = {str(item["source"]) for item in normalized}
    source = next(iter(sources)) if len(sources) == 1 else "mixed"
    if source == "unavailable":
        source = "measured"
    return ProviderTelemetry(
        status="available" if complete else "partial",
        source=source,
        cost_currency=cost_currency,
        **sums,
    )


def _provider_sample(value: object) -> dict[str, object] | None:
    try:
        payload = _as_json_mapping(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, ProviderTelemetry):
        payload.setdefault("source", value.source)
    else:
        payload.setdefault("source", "measured")
    if "elapsed_seconds" in payload and "time_seconds" not in payload:
        payload["time_seconds"] = payload["elapsed_seconds"]
    if "cached_tokens" in payload and "cached_input_tokens" not in payload:
        payload["cached_input_tokens"] = payload["cached_tokens"]
    measurement_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "time_seconds",
        "cost_minor",
        "request_count",
        "retry_count",
        "usage_reported_requests",
    )
    if not any(payload.get(key) is not None for key in measurement_fields):
        return None
    return {
        key: payload.get(key)
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "time_seconds",
            "cost_minor",
            "request_count",
            "retry_count",
            "usage_reported_requests",
            "cost_currency",
            "source",
        )
    }


def _sum_optional(values: Iterable[object]) -> int | float | None:
    collected = [value for value in values if isinstance(value, (int, float))]
    return sum(collected) if collected else None


def _sum_complete(samples: Iterable[Mapping[str, object]], field: str) -> int | float | None:
    values = [sample.get(field) for sample in samples]
    if not values or not all(isinstance(value, (int, float)) for value in values):
        return None
    return sum(values)


def _memory_telemetry(
    memory: AgentMemory | None, binding: FrozenEvaluationBinding | None
) -> MemoryTelemetry:
    diagnostics = getattr(memory, "context_diagnostics", {}) if memory is not None else {}
    if not isinstance(diagnostics, Mapping):
        diagnostics = {}
    values = {
        "context_items": diagnostics.get("used_items"),
        "context_tokens": diagnostics.get("used_estimated_tokens"),
        "stored_artifacts": diagnostics.get("stored_artifacts"),
        "snapshot_members": diagnostics.get("snapshot_members"),
    }
    totals = getattr(memory, "telemetry_totals", {}) if memory is not None else {}
    if isinstance(totals, Mapping):
        values.update(
            {
                "context_items": totals.get("context_items"),
                "context_tokens": totals.get("context_tokens"),
            }
        )
    stored_artifacts = getattr(memory, "stored_artifacts", None)
    if (
        isinstance(stored_artifacts, int)
        and not isinstance(stored_artifacts, bool)
        and stored_artifacts >= 0
    ):
        values["stored_artifacts"] = stored_artifacts
    frozen_members = getattr(memory, "frozen_snapshot_members", None)
    if isinstance(frozen_members, int) and frozen_members >= 0:
        values["snapshot_members"] = frozen_members
    module_telemetry = getattr(memory, "module_telemetry", None)
    if isinstance(module_telemetry, Mapping):
        counters = {
            "module_construction_events": "construction_events",
            "module_read_events": "read_events",
            "module_write_events": "write_events",
            "module_consolidation_events": "consolidation_events",
            "module_contribution_events": "contribution_events",
        }
        module_ids: list[str] = []
        module_versions: dict[str, str] = {}
        module_values: dict[str, list[int | None]] = {field: [] for field in counters}
        malformed = False
        if not module_telemetry:
            values.update({field: 0 for field in counters})
        else:
            for module_id, telemetry in module_telemetry.items():
                if isinstance(telemetry, BaseModel):
                    telemetry = telemetry.model_dump(mode="json")
                if not isinstance(module_id, str) or not isinstance(telemetry, Mapping):
                    malformed = True
                    continue
                module_ids.append(module_id)
                version = telemetry.get("module_version")
                if not isinstance(version, str):
                    malformed = True
                    continue
                module_versions[module_id] = version
                for output_field, input_field in counters.items():
                    value = telemetry.get(input_field)
                    module_values[output_field].append(
                        value
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                        else None
                    )
            if module_ids and not malformed:
                values.update(
                    {
                        field: sum(field_values)
                        if field_values and all(value is not None for value in field_values)
                        else None
                        for field, field_values in module_values.items()
                    }
                )
        if module_ids and not malformed:
            values["module_ids"] = tuple(sorted(set(module_ids)))
            values["module_versions"] = module_versions
    numeric_values = {
        key: value for key, value in values.items() if isinstance(value, (int, float))
    }
    structured_values = {
        key: value for key, value in values.items() if key in {"module_ids", "module_versions"}
    }
    values = {**numeric_values, **structured_values}
    return MemoryTelemetry(status="available", **values) if values else MemoryTelemetry()
