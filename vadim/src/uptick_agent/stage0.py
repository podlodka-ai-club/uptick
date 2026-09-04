"""Offline Stage 0 experiment contracts and deterministic report helpers.

This module is deliberately independent from the simulator, model providers,
and the memory implementation. It describes experiment metadata and consumes
already-recorded attempt results; it never starts a run or makes network calls.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, variance
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_MAJOR = 1
CONDITIONS = ("B0", "B1")
PHASES = ("training", "evaluation")
ATTEMPT_STATUSES = ("requested", "running", "completed", "failed", "interrupted", "excluded")
TERMINAL_ATTEMPT_STATUSES = ("completed", "failed", "interrupted", "excluded")
RETRY_ELIGIBLE_FAILURE_CLASSES = frozenset({"transient", "interrupted"})
CANONICAL_METRICS = (
    "final_balance_minor",
    "successful_purchases",
    "lost_purchases",
    "revenue_minor",
    "lost_revenue_minor",
    "server_cost_minor",
    "deployment_cost_minor",
    "steps",
    "completion_status",
)
GUARDRAIL_METRICS = (
    "completion_status",
    "lost_purchases",
    "lost_revenue_minor",
    "server_cost_minor",
    "deployment_cost_minor",
)
PERCENTILES = ("p10", "p25", "p50", "p75", "p90")
CANONICAL_METRIC_SET = frozenset(CANONICAL_METRICS)
INTEGER_METRICS = CANONICAL_METRIC_SET - {"completion_status"}
NONNEGATIVE_INTEGER_METRICS = INTEGER_METRICS - {"final_balance_minor"}
# This is the simulator/task outcome, deliberately separate from the harness
# attempt status above.  A completed harness attempt may, for example, observe
# a simulator timeout; it must never encode that as ``attempt.status``.
COMPLETION_STATUSES = frozenset({"completed", "terminal_failure", "timeout", "cancelled"})
SELECTION_RULE = (
    "select the sole latest terminal attempt per block/condition; retries are allowed "
    "only after retry-eligible failed/interrupted attempts"
)
HEX_DIGEST = r"^[0-9a-f]{64}$"
GIT_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class Stage0Model(BaseModel):
    """Strict, major-versioned data crossing the Stage 0 artifact boundary."""

    model_config = ConfigDict(extra="forbid", validate_default=True, allow_inf_nan=False)

    schema_version: str = Field(default=SCHEMA_VERSION, pattern=r"^[1-9][0-9]*\.[0-9]+$")

    @field_validator("schema_version")
    @classmethod
    def _supported_major(cls, value: str) -> str:
        major, _minor = value.split(".", maxsplit=1)
        if int(major) != SUPPORTED_SCHEMA_MAJOR:
            raise ValueError(
                f"unsupported Stage 0 schema major {major}; expected {SUPPORTED_SCHEMA_MAJOR}"
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _sanitize_input(cls, value: Any) -> Any:
        return _sanitize_value(value)


def _sanitize_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively make persisted values safe and reject non-finite numbers."""

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite floats are not valid Stage 0 values")
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, BaseModel):
        return _sanitize_value(value.model_dump(mode="json"), key=key)
    if isinstance(value, Mapping):
        return {
            str(item_key): (
                "<redacted>"
                if _secret_key(str(item_key))
                else _sanitize_value(item_value, key=str(item_key))
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, (set, frozenset)):
        raise ValueError("sets are not valid Stage 0 artifact values; use an ordered tuple or list")
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _secret_key(key: str) -> bool:
    return bool(
        re.search(
            r"(?i)^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token|authorization)$",
            key,
        )
    )


def canonical_json(value: object) -> str:
    """Return deterministic JSON for fingerprints and content hashes."""

    value = _sanitize_value(value)
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    """Hash relative file names and contents in stable order."""

    if not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")
    members: list[dict[str, str]] = []
    ignored_parts = {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "artifacts",
    }
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in ignored_parts for part in relative.parts) or _secret_path(relative):
            continue
        members.append(
            {
                "path": relative.as_posix(),
                "sha256": sha256_file(path),
            }
        )
    return sha256_json(members)


def _secret_path(relative: Path) -> bool:
    """Keep local credential material out of code-tree provenance hashes."""

    parts = relative.parts
    name = relative.name.lower()
    return (
        any(part.lower() in {"secrets", "credentials", "private"} for part in parts)
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )


_SECRET_PATTERNS = (
    re.compile(r"(?i)authorization\s*[:=]\s*(?:basic|bearer|token)\s+[^\s,;]+"),
    re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token)"
        r"\s*[:=]\s*(?:bearer|token)\s+[^\s,;]+"
    ),
    re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(
        r"(?i)authorization\s*(?:(?:[:=]\s*)?(?:bearer|token)\s+|[:=]\s*)"
        r"[^\s,;]+"
    ),
    re.compile(r"(?i)\b(?:bearer|token)\s+[a-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def redact_text(value: str) -> str:
    """Remove common credential-shaped values before they can be persisted."""

    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("<redacted>", result)
    return result


class EnvironmentProfile(Stage0Model):
    environment_id: str = Field(min_length=1, max_length=128)
    environment_version: str = Field(min_length=1, max_length=128)
    adapter_id: str = Field(min_length=1, max_length=128)
    adapter_version: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(min_length=1, max_length=128)
    endpoint_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)


class ProviderProfile(Stage0Model):
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    prompt_hash: str | None = Field(default=None, min_length=64, max_length=64)
    token_estimator_id: str = Field(min_length=1, max_length=128)
    token_estimator_version: str = Field(min_length=1, max_length=64)
    settings_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)


class MemoryCondition(Stage0Model):
    condition_id: Literal["B0", "B1"]
    memory_mode: Literal["none", "legacy"]
    training_mode: Literal["reset", "carry"]
    evaluation_mode: Literal["empty", "frozen"]


class RawContentPolicy(Stage0Model):
    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    prompts: bool = True
    observations: bool = True
    decision_traces: bool = True
    retention_policy_ref: str = Field(min_length=1, max_length=128)
    mandatory_secret_handling: Literal["redact_or_reject"] = "redact_or_reject"


class CaptureState(Stage0Model):
    """A declaration of one external raw-content capture outcome.

    The offline harness verifies this declaration's shape and hashes only. It
    does not dereference or inspect the external body.
    """

    state: Literal["captured", "disabled", "not_emitted", "quarantined"]
    artifact_ref: str | None = Field(default=None, max_length=512)
    content_hash: str | None = Field(default=None, pattern=HEX_DIGEST)
    absence_reason: (
        Literal["before_prompt", "before_observation", "before_trace", "secret_handling_failed"]
        | None
    ) = None
    redaction_audit_hash: str | None = Field(default=None, pattern=HEX_DIGEST)

    @model_validator(mode="after")
    def _validate_capture_state(self) -> CaptureState:
        content_fields = (self.artifact_ref, self.content_hash, self.redaction_audit_hash)
        if self.state == "captured":
            if any(value is None for value in content_fields) or self.absence_reason is not None:
                raise ValueError("captured content requires ref, hash, redaction audit only")
            if self.artifact_ref != f"sha256:{self.content_hash}":
                raise ValueError("captured artifact_ref must match content_hash")
        elif self.state == "quarantined":
            if (
                self.artifact_ref is not None
                or self.content_hash is not None
                or self.redaction_audit_hash is None
                or self.absence_reason != "secret_handling_failed"
            ):
                raise ValueError(
                    "quarantined content requires only redaction audit and secret_handling_failed"
                )
        elif self.state == "disabled":
            if (
                any(value is not None for value in content_fields)
                or self.absence_reason is not None
            ):
                raise ValueError("disabled content must not carry capture metadata")
        elif any(value is not None for value in content_fields) or self.absence_reason is None:
            raise ValueError("not_emitted content requires only an absence_reason")
        return self


class RawContentCapture(Stage0Model):
    """Per-attempt external-capture declaration bound to the resolved policy."""

    manifest_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    audit_ref: str = Field(min_length=71, max_length=512)
    audit_hash: str = Field(pattern=HEX_DIGEST)
    policy_fingerprint: str = Field(pattern=HEX_DIGEST)
    prompts: CaptureState
    observations: CaptureState
    decision_traces: CaptureState

    @model_validator(mode="after")
    def _validate_capture(self) -> RawContentCapture:
        if self.audit_ref != f"sha256:{self.audit_hash}":
            raise ValueError("raw capture audit_ref must match audit_hash")
        expected_absences = {
            "prompts": "before_prompt",
            "observations": "before_observation",
            "decision_traces": "before_trace",
        }
        for name, expected in expected_absences.items():
            state = getattr(self, name)
            if state.state == "not_emitted" and state.absence_reason != expected:
                raise ValueError(f"{name} not_emitted requires absence_reason {expected}")
        return self


class PolicyReferences(Stage0Model):
    candidate_validation_policy: str = Field(min_length=1, max_length=128)
    audit_retention_policy: str = Field(min_length=1, max_length=128)
    raw_content: RawContentPolicy


class FailurePolicy(Stage0Model):
    retained_statuses: tuple[
        Literal["requested", "running", "completed", "failed", "interrupted", "excluded"], ...
    ] = ATTEMPT_STATUSES
    distribution_denominator: Literal["completed_attempts_with_metric"] = (
        "completed_attempts_with_metric"
    )
    paired_denominator: Literal["complete_blocks"] = "complete_blocks"
    excluded_attempts_are_not_retried_for_promotion: bool = True
    max_attempts_per_cell: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def _validate_failure_policy(self) -> FailurePolicy:
        if self.retained_statuses != ATTEMPT_STATUSES:
            raise ValueError("failure_policy.retained_statuses must exactly list every status")
        if not self.excluded_attempts_are_not_retried_for_promotion:
            raise ValueError("excluded attempts may never be retried for promotion")
        return self


class Stage0Profile(Stage0Model):
    """Checked-in preregistration input; it contains no run evidence."""

    profile_id: str = Field(min_length=1, max_length=128)
    environment: EnvironmentProfile
    provider: ProviderProfile
    memory_conditions: tuple[MemoryCondition, ...]
    training_seeds: tuple[int, ...]
    evaluation_seeds: tuple[int, ...]
    replicate_indices: tuple[int, ...]
    canonical_metrics: tuple[str, ...] = CANONICAL_METRICS
    guardrail_metrics: tuple[str, ...] = GUARDRAIL_METRICS
    failure_policy: FailurePolicy
    policy_references: PolicyReferences
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_profile(self) -> Stage0Profile:
        _validate_seed_sets(self.training_seeds, self.evaluation_seeds)
        if not self.replicate_indices or any(index < 0 for index in self.replicate_indices):
            raise ValueError("replicate_indices must contain at least one non-negative index")
        if len(set(self.replicate_indices)) != len(self.replicate_indices):
            raise ValueError("replicate_indices must be unique")
        _validate_conditions(self.memory_conditions)
        _validate_metrics(self.canonical_metrics, self.guardrail_metrics)
        return self


class RunMatrixBlock(Stage0Model):
    block_id: str = Field(min_length=1, max_length=256)
    phase: Literal["training", "evaluation"]
    environment_id: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(min_length=1, max_length=128)
    world_seed: int
    replicate_index: int = Field(ge=0)
    conditions: tuple[Literal["B0", "B1"], ...] = CONDITIONS

    @model_validator(mode="after")
    def _validate_block(self) -> RunMatrixBlock:
        if self.world_seed == 0:
            raise ValueError("world_seed 0 is invalid")
        if self.conditions != CONDITIONS:
            raise ValueError("each Stage 0 comparison block must contain exactly B0 and B1")
        return self


class MemorySnapshotRef(Stage0Model):
    snapshot_id: str = Field(min_length=1, max_length=256)
    phase: Literal["training", "evaluation"]
    condition: Literal["B0", "B1"]
    world_seed: int
    replicate_index: int = Field(ge=0)
    memory_namespace: str = Field(min_length=1, max_length=256)
    memory_operation_order: int = Field(ge=0)
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    immutable: bool
    captured_at: datetime
    ancestry_snapshot_id: str | None = Field(default=None, max_length=256)
    ancestry_content_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    quarantine_namespace: str | None = Field(default=None, max_length=256)
    quarantine_provenance_ref: str | None = Field(default=None, max_length=512)
    quarantine_audit_hash: str | None = Field(default=None, pattern=HEX_DIGEST)

    @model_validator(mode="after")
    def _validate_snapshot(self) -> MemorySnapshotRef:
        if self.world_seed == 0:
            raise ValueError("world_seed 0 is invalid")
        if self.condition == "B1" and not self.immutable:
            raise ValueError("B1 memory snapshots must be immutable")
        if (self.ancestry_snapshot_id is None) != (self.ancestry_content_hash is None):
            raise ValueError("snapshot ancestry id and hash must be supplied together")
        if self.phase == "evaluation" and self.condition == "B1":
            if self.ancestry_snapshot_id is None:
                raise ValueError("frozen B1 snapshots require immutable ancestry")
            if (
                not self.quarantine_namespace
                or not self.quarantine_provenance_ref
                or self.quarantine_audit_hash is None
            ):
                raise ValueError("frozen B1 snapshots require quarantine provenance")
            if self.quarantine_namespace == self.memory_namespace:
                raise ValueError("quarantine namespace must differ from decision namespace")
        return self


class AttemptRecord(Stage0Model):
    manifest_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    block_id: str = Field(min_length=1, max_length=256)
    phase: Literal["training", "evaluation"]
    condition: Literal["B0", "B1"]
    world_seed: int
    replicate_index: int = Field(ge=0)
    attempt_index: int = Field(default=0, ge=0)
    retry_of: str | None = Field(default=None, min_length=1, max_length=256)
    status: Literal["requested", "running", "completed", "failed", "interrupted", "excluded"]
    run_id: str | None = Field(default=None, min_length=1, max_length=256)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    metrics: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    result_ref: str | None = Field(default=None, max_length=512)
    result_hash: str | None = Field(default=None, pattern=HEX_DIGEST)
    trace_ref: str | None = Field(default=None, max_length=512)
    raw_content_capture: RawContentCapture | None = None
    memory_namespace: str | None = Field(default=None, max_length=256)
    memory_operation_order: int | None = Field(default=None, ge=0)
    memory_snapshot_id: str | None = Field(default=None, max_length=256)
    memory_snapshot_hash: str | None = Field(default=None, pattern=HEX_DIGEST)
    quarantine_namespace: str | None = Field(default=None, max_length=256)
    quarantine_provenance_ref: str | None = Field(default=None, max_length=512)
    quarantine_audit_hash: str | None = Field(default=None, pattern=HEX_DIGEST)
    runtime_metadata_hash: str | None = Field(default=None, pattern=HEX_DIGEST)
    no_memory_audit_hash: str | None = Field(default=None, pattern=HEX_DIGEST)
    failure_class: (
        Literal["validation", "transient", "permanent", "interrupted", "excluded"] | None
    ) = None
    failure_reason: str | None = Field(default=None, max_length=2_000)
    exclusion_reason: str | None = Field(default=None, max_length=2_000)

    @field_validator("metrics")
    @classmethod
    def _validate_metrics(
        cls, value: dict[str, float | int | str | bool | None]
    ) -> dict[str, float | int | str | bool | None]:
        unknown = set(value) - CANONICAL_METRIC_SET
        if unknown:
            raise ValueError(f"attempt contains non-canonical metrics: {sorted(unknown)}")
        for metric_id, metric_value in value.items():
            if metric_value is None:
                continue
            if metric_id == "completion_status":
                if not isinstance(metric_value, str) or metric_value not in COMPLETION_STATUSES:
                    raise ValueError(
                        "completion_status must be one of the declared task completion outcomes"
                    )
                continue
            if metric_id in INTEGER_METRICS and (
                isinstance(metric_value, bool) or not isinstance(metric_value, int)
            ):
                raise ValueError(f"metric {metric_id} must be an integer")
            if metric_id in NONNEGATIVE_INTEGER_METRICS and metric_value < 0:
                raise ValueError(f"metric {metric_id} must be non-negative")
        return value

    @field_validator("failure_reason", "exclusion_reason")
    @classmethod
    def _redact_failure_text(cls, value: str | None) -> str | None:
        return redact_text(value) if value is not None else None

    @model_validator(mode="after")
    def _validate_status_fields(self) -> AttemptRecord:
        if self.world_seed == 0:
            raise ValueError("world_seed 0 is invalid")
        if self.status == "completed" and self.failure_class is not None:
            raise ValueError("completed attempts cannot have failure_class")
        if self.attempt_index == 0 and self.retry_of is not None:
            raise ValueError("initial attempts cannot set retry_of")
        if self.attempt_index > 0 and self.retry_of is None:
            raise ValueError("retry attempts require retry_of")
        if self.status == "excluded" and not self.exclusion_reason:
            raise ValueError("excluded attempts require exclusion_reason")
        if self.status in {"failed", "interrupted"} and self.failure_class is None:
            raise ValueError(f"{self.status} attempts require failure_class")
        if self.status not in {"failed", "interrupted"} and self.failure_class is not None:
            raise ValueError("only failed/interrupted attempts may set failure_class")
        if (
            self.finished_at is not None
            and self.started_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at must not precede started_at")
        if self.status == "completed":
            if set(self.metrics) != CANONICAL_METRIC_SET or any(
                self.metrics[metric_id] is None for metric_id in CANONICAL_METRICS
            ):
                raise ValueError("completed attempts require all non-null canonical metrics")
        elif "completion_status" in self.metrics:
            raise ValueError("only completed attempts may record a task completion_status")
        if (self.result_ref is None) != (self.result_hash is None):
            raise ValueError("result_ref and result_hash must be supplied together")
        if self.result_ref is not None and self.result_ref != f"sha256:{self.result_hash}":
            raise ValueError("result_ref must match result_hash")
        if self.raw_content_capture is not None:
            if (
                self.raw_content_capture.manifest_id != self.manifest_id
                or self.raw_content_capture.attempt_id != self.attempt_id
            ):
                raise ValueError("raw content capture must bind this manifest and attempt")
            trace_capture = self.raw_content_capture.decision_traces
            expected_trace_ref = (
                trace_capture.artifact_ref if trace_capture.state == "captured" else None
            )
            if self.trace_ref != expected_trace_ref:
                raise ValueError("trace_ref must agree with decision-trace capture")
        elif self.trace_ref is not None:
            raise ValueError("trace_ref requires a decision-trace capture declaration")

        memory_fields = (
            self.memory_namespace,
            self.memory_operation_order,
            self.memory_snapshot_id,
            self.memory_snapshot_hash,
            self.quarantine_namespace,
            self.quarantine_provenance_ref,
            self.quarantine_audit_hash,
        )
        if self.condition == "B0":
            if any(value is not None for value in memory_fields):
                raise ValueError("B0 no-memory attempts must not carry memory metadata")
        else:
            if self.no_memory_audit_hash is not None:
                raise ValueError("B1 attempts must not claim a no-memory audit")
            if self.memory_namespace is None or self.memory_operation_order is None:
                raise ValueError("B1 attempts require a memory namespace and operation order")
            frozen_fields = (
                self.memory_snapshot_id,
                self.memory_snapshot_hash,
                self.quarantine_namespace,
                self.quarantine_provenance_ref,
                self.quarantine_audit_hash,
            )
            if self.phase == "training" and any(value is not None for value in frozen_fields):
                raise ValueError("B1 training attempts must not carry frozen evaluation metadata")
            if self.phase == "evaluation" and any(value is None for value in frozen_fields):
                raise ValueError(
                    "B1 evaluation attempts require frozen input and quarantine metadata"
                )
        return self


class MetricDistribution(Stage0Model):
    phase: Literal["training", "evaluation"]
    condition: Literal["B0", "B1"]
    metric_id: str = Field(min_length=1, max_length=128)
    attempts_total: int = Field(ge=0)
    selected_attempts: int = Field(ge=0)
    completed_attempts: int = Field(ge=0)
    count: int = Field(ge=0)
    mean: float | None = None
    variance: float | None = None
    variance_method: Literal["sample"] = "sample"
    percentiles: dict[str, float] = Field(default_factory=dict)
    categorical_counts: dict[str, int] = Field(default_factory=dict)

    @field_validator("percentiles")
    @classmethod
    def _validate_percentiles(cls, value: dict[str, float]) -> dict[str, float]:
        if set(value) - set(PERCENTILES):
            raise ValueError("percentiles contain an unsupported key")
        if any(not math.isfinite(item) for item in value.values()):
            raise ValueError("percentiles must contain finite floats")
        return value


class BlockConditionCoverage(Stage0Model):
    """One explicit expected block/condition cell and its retained attempts."""

    block_id: str = Field(min_length=1, max_length=256)
    phase: Literal["training", "evaluation"]
    condition: Literal["B0", "B1"]
    world_seed: int
    replicate_index: int = Field(ge=0)
    expected_attempts: int = Field(ge=1)
    observed_attempts: int = Field(ge=0)
    terminal_attempts: int = Field(ge=0)
    observed_attempt_ids: tuple[str, ...]
    selected_terminal_attempt_id: str | None = None


class MemoryProvenanceReport(Stage0Model):
    b1_namespace_complete: bool
    frozen_snapshot_complete: bool
    invalid_namespace_cells: tuple[str, ...]
    missing_snapshot_cells: tuple[str, ...]
    invalid_snapshot_cells: tuple[str, ...]


class ConditionReport(Stage0Model):
    phase: Literal["training", "evaluation"]
    condition: Literal["B0", "B1"]
    attempts_total: int = Field(ge=0)
    terminal_attempts: int = Field(ge=0)
    selected_attempt_ids: tuple[str, ...]
    status_counts: dict[str, int]
    metric_distributions: list[MetricDistribution]


class PairedDelta(Stage0Model):
    block_id: str = Field(min_length=1, max_length=256)
    phase: Literal["training", "evaluation"]
    world_seed: int
    replicate_index: int = Field(ge=0)
    baseline_condition: Literal["B0"] = "B0"
    candidate_condition: Literal["B1"] = "B1"
    deltas: dict[str, float]

    @field_validator("deltas")
    @classmethod
    def _validate_deltas(cls, value: dict[str, float]) -> dict[str, float]:
        unknown = set(value) - CANONICAL_METRIC_SET
        if unknown:
            raise ValueError(f"paired delta contains non-canonical metrics: {sorted(unknown)}")
        if any(not math.isfinite(item) for item in value.values()):
            raise ValueError("paired deltas must be finite")
        return value


class PairedReport(Stage0Model):
    phase: Literal["training", "evaluation"]
    baseline_condition: Literal["B0"] = "B0"
    candidate_condition: Literal["B1"] = "B1"
    total_blocks: int = Field(ge=0)
    complete_blocks: int = Field(ge=0)
    incomplete_block_ids: list[str]
    deltas: list[PairedDelta]
    delta_distributions: dict[str, MetricDistribution]


class Stage0Manifest(Stage0Model):
    """Resolved preregistration manifest; ``evidence_status`` stays explicit."""

    manifest_id: str = Field(min_length=1, max_length=256)
    manifest_hash: str = Field(pattern=HEX_DIGEST)
    profile_id: str = Field(min_length=1, max_length=128)
    created_at: datetime
    evidence_status: Literal["preregistration_only", "evidence_collected"] = "preregistration_only"
    source_revision: str = Field(min_length=1, max_length=128)
    source_dirty: bool | None = None
    source_tree_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    dependency_lock_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    runtime_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    project_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    planner_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    resolved_prompt_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    resolved_settings_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    resolved_endpoint_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    profile_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    resolved_config_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    environment: EnvironmentProfile
    provider: ProviderProfile
    memory_conditions: tuple[MemoryCondition, ...]
    training_seeds: tuple[int, ...]
    evaluation_seeds: tuple[int, ...]
    replicate_indices: tuple[int, ...]
    canonical_metrics: tuple[str, ...]
    guardrail_metrics: tuple[str, ...]
    failure_policy: FailurePolicy
    policy_references: PolicyReferences
    notes: tuple[str, ...] = ()
    run_matrix: tuple[RunMatrixBlock, ...]
    initial_memory_snapshots: tuple[MemorySnapshotRef, ...] = ()
    frozen_memory_snapshots: tuple[MemorySnapshotRef, ...] = ()

    @model_validator(mode="after")
    def _validate_manifest(self) -> Stage0Manifest:
        _validate_seed_sets(self.training_seeds, self.evaluation_seeds)
        _validate_conditions(self.memory_conditions)
        _validate_metrics(self.canonical_metrics, self.guardrail_metrics)
        profile = _profile_from_manifest(self)
        if self.profile_hash != sha256_json(profile):
            raise ValueError("profile_hash does not match manifest profile fields")
        fingerprint_payload = _fingerprint_payload(
            profile_hash=self.profile_hash,
            source_revision=self.source_revision,
            source_dirty=self.source_dirty,
            source_tree_hash=self.source_tree_hash,
            dependency_lock_hash=self.dependency_lock_hash,
            runtime_fingerprint=self.runtime_fingerprint,
            project_fingerprint=self.project_fingerprint,
            planner_fingerprint=self.planner_fingerprint,
            resolved_prompt_fingerprint=self.resolved_prompt_fingerprint,
            resolved_settings_fingerprint=self.resolved_settings_fingerprint,
            resolved_endpoint_fingerprint=self.resolved_endpoint_fingerprint,
        )
        expected_config_fingerprint = sha256_json(fingerprint_payload)
        if self.resolved_config_fingerprint != expected_config_fingerprint:
            raise ValueError("resolved_config_fingerprint does not match resolved manifest")
        expected_manifest_id = f"{self.profile_id}-{expected_config_fingerprint[:16]}"
        if self.manifest_id != expected_manifest_id:
            raise ValueError("manifest_id does not match resolved config fingerprint")
        if self.run_matrix != build_run_matrix(profile):
            raise ValueError("run_matrix does not exactly match the resolved profile")
        if self.manifest_hash != manifest_hash(self):
            raise ValueError("manifest_hash does not match manifest content")
        _validate_resolved_pins(self)
        _validate_snapshot_sets(self)
        if self.evidence_status == "evidence_collected":
            missing = _missing_provenance(self)
            if missing:
                raise ValueError(
                    "evidence_collected manifest lacks resolved provenance: " + ", ".join(missing)
                )
            if not GIT_REVISION.fullmatch(self.source_revision):
                raise ValueError("evidence_collected requires a 40- or 64-character git revision")
            if self.source_dirty is None:
                raise ValueError("evidence_collected requires known scoped source_dirty status")
        return self


class Stage0Report(Stage0Model):
    manifest_id: str = Field(min_length=1, max_length=256)
    manifest_hash: str = Field(pattern=HEX_DIGEST)
    attempts_hash: str = Field(pattern=HEX_DIGEST)
    generated_at: datetime
    total_attempts: int = Field(ge=0)
    status_counts: dict[str, int]
    retained_attempt_ids: tuple[str, ...]
    retained_attempts: tuple[AttemptRecord, ...]
    selection_rule: str = SELECTION_RULE
    selected_terminal_attempt_ids: dict[str, str]
    expected_block_count: int = Field(ge=0)
    observed_block_count: int = Field(ge=0)
    expected_block_condition_count: int = Field(ge=0)
    observed_block_condition_count: int = Field(ge=0)
    missing_block_ids: tuple[str, ...]
    missing_block_conditions: tuple[str, ...]
    incomplete_block_conditions: tuple[str, ...]
    coverage: tuple[BlockConditionCoverage, ...]
    evidence_complete: bool
    evidence_incompleteness_reasons: tuple[str, ...]
    memory_provenance: MemoryProvenanceReport
    condition_reports: list[ConditionReport]
    paired_reports: list[PairedReport]

    @model_validator(mode="after")
    def _validate_report_integrity(self) -> Stage0Report:
        attempts = list(self.retained_attempts)
        if self.total_attempts != len(attempts):
            raise ValueError("report total_attempts does not match retained attempts")
        if self.retained_attempt_ids != tuple(attempt.attempt_id for attempt in attempts):
            raise ValueError("report retained_attempt_ids do not match retained attempts")
        expected_statuses = Counter({status: 0 for status in ATTEMPT_STATUSES})
        expected_statuses.update(attempt.status for attempt in attempts)
        if self.status_counts != dict(expected_statuses):
            raise ValueError("report status_counts do not match retained attempts")
        attempt_payload = [attempt.model_dump(mode="json") for attempt in attempts]
        if self.attempts_hash != sha256_json(attempt_payload):
            raise ValueError("report attempts_hash does not match retained attempts")
        if self.evidence_complete != (not self.evidence_incompleteness_reasons):
            raise ValueError("report evidence_complete conflicts with incompleteness reasons")
        return self


def _validate_seed_sets(training: Iterable[int], evaluation: Iterable[int]) -> None:
    train = tuple(training)
    eval_ = tuple(evaluation)
    if not train or not eval_:
        raise ValueError("training_seeds and evaluation_seeds must both be non-empty")
    if any(seed == 0 for seed in train + eval_):
        raise ValueError("seed 0 is invalid")
    if set(train) & set(eval_):
        raise ValueError("training_seeds and evaluation_seeds must be disjoint")
    if len(set(train)) != len(train) or len(set(eval_)) != len(eval_):
        raise ValueError("seed sets must not contain duplicates")


def _validate_conditions(conditions: Iterable[MemoryCondition]) -> None:
    values = tuple(conditions)
    if tuple(item.condition_id for item in values) != CONDITIONS:
        raise ValueError("memory_conditions must list B0 then B1 exactly once")
    b0, b1 = values
    expected = (
        ("B0", "none", "reset", "empty"),
        ("B1", "legacy", "carry", "frozen"),
    )
    actual = tuple(
        (item.condition_id, item.memory_mode, item.training_mode, item.evaluation_mode)
        for item in values
    )
    if actual != expected:
        raise ValueError(
            "Stage 0 requires exact B0 no-memory/reset/empty and B1 legacy/carry/frozen modes"
        )


def _validate_metrics(metrics: Iterable[str], guardrails: Iterable[str]) -> None:
    metric_values = tuple(metrics)
    metric_set = set(metric_values)
    if metric_values != CANONICAL_METRICS:
        missing = set(CANONICAL_METRICS) - metric_set
        unknown = metric_set - CANONICAL_METRIC_SET
        raise ValueError(
            "canonical_metrics must exactly match the whitelist; "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    if tuple(guardrails) != GUARDRAIL_METRICS:
        raise ValueError("guardrail_metrics must exactly match the canonical ordered guardrails")


def _missing_provenance(manifest: Stage0Manifest) -> list[str]:
    fields = (
        "runtime_fingerprint",
        "project_fingerprint",
        "planner_fingerprint",
        "resolved_prompt_fingerprint",
        "resolved_settings_fingerprint",
        "resolved_endpoint_fingerprint",
    )
    missing = [field for field in fields if getattr(manifest, field) is None]
    if manifest.source_dirty is None:
        missing.append("source_dirty")
    return missing


def _profile_from_manifest(manifest: Stage0Manifest) -> Stage0Profile:
    """Reconstruct the checked-in profile payload that its hash commits to."""

    return Stage0Profile(
        schema_version=manifest.schema_version,
        profile_id=manifest.profile_id,
        environment=manifest.environment,
        provider=manifest.provider,
        memory_conditions=manifest.memory_conditions,
        training_seeds=manifest.training_seeds,
        evaluation_seeds=manifest.evaluation_seeds,
        replicate_indices=manifest.replicate_indices,
        canonical_metrics=manifest.canonical_metrics,
        guardrail_metrics=manifest.guardrail_metrics,
        failure_policy=manifest.failure_policy,
        policy_references=manifest.policy_references,
        notes=manifest.notes,
    )


def _fingerprint_payload(
    *,
    profile_hash: str,
    source_revision: str,
    source_dirty: bool | None,
    source_tree_hash: str,
    dependency_lock_hash: str,
    runtime_fingerprint: str | None,
    project_fingerprint: str | None,
    planner_fingerprint: str | None,
    resolved_prompt_fingerprint: str | None,
    resolved_settings_fingerprint: str | None,
    resolved_endpoint_fingerprint: str | None,
) -> dict[str, str | bool | None]:
    return {
        "profile_hash": profile_hash,
        "source_revision": source_revision,
        "source_dirty": source_dirty,
        "source_tree_hash": source_tree_hash,
        "dependency_lock_hash": dependency_lock_hash,
        "runtime_fingerprint": runtime_fingerprint,
        "project_fingerprint": project_fingerprint,
        "planner_fingerprint": planner_fingerprint,
        "resolved_prompt_fingerprint": resolved_prompt_fingerprint,
        "resolved_settings_fingerprint": resolved_settings_fingerprint,
        "resolved_endpoint_fingerprint": resolved_endpoint_fingerprint,
    }


def manifest_hash(manifest: Stage0Manifest | Mapping[str, Any]) -> str:
    """Hash every persisted manifest field except the self-referential hash."""

    payload = (
        manifest.model_dump(mode="json") if isinstance(manifest, BaseModel) else dict(manifest)
    )
    payload.pop("manifest_hash", None)
    return sha256_json(payload)


def _validate_resolved_pins(manifest: Stage0Manifest) -> None:
    pairs = (
        ("prompt", manifest.provider.prompt_hash, manifest.resolved_prompt_fingerprint),
        (
            "settings",
            manifest.provider.settings_fingerprint,
            manifest.resolved_settings_fingerprint,
        ),
        (
            "endpoint",
            manifest.environment.endpoint_fingerprint,
            manifest.resolved_endpoint_fingerprint,
        ),
    )
    for label, pinned, resolved in pairs:
        if pinned is not None and pinned != resolved:
            raise ValueError(f"resolved {label} fingerprint differs from profile pin")


def _snapshot_key(snapshot: MemorySnapshotRef) -> tuple[str, int, int, str]:
    return (snapshot.phase, snapshot.world_seed, snapshot.replicate_index, snapshot.condition)


def _validate_snapshot_sets(manifest: Stage0Manifest) -> None:
    snapshots = manifest.initial_memory_snapshots + manifest.frozen_memory_snapshots
    keys = [_snapshot_key(snapshot) for snapshot in snapshots]
    if len(keys) != len(set(keys)):
        raise ValueError("memory snapshots must be unique per phase/seed/replicate/condition")
    matrix_keys = {
        (block.phase, block.world_seed, block.replicate_index, condition)
        for block in manifest.run_matrix
        for condition in block.conditions
    }
    for snapshot in snapshots:
        if _snapshot_key(snapshot) not in matrix_keys:
            raise ValueError("memory snapshot does not identify a matrix block/condition")
    if any(
        snapshot.phase != "evaluation" or snapshot.condition != "B1"
        for snapshot in manifest.frozen_memory_snapshots
    ):
        raise ValueError("frozen_memory_snapshots may contain only evaluation B1 snapshots")
    frozen_keys = {_snapshot_key(snapshot) for snapshot in manifest.frozen_memory_snapshots}
    expected_frozen = {
        (block.phase, block.world_seed, block.replicate_index, "B1")
        for block in manifest.run_matrix
        if block.phase == "evaluation"
    }
    if frozen_keys and frozen_keys != expected_frozen:
        raise ValueError("frozen_memory_snapshots must cover every evaluation B1 cell")
    if not manifest.initial_memory_snapshots and manifest.frozen_memory_snapshots:
        raise ValueError("frozen snapshots require final training ancestry snapshots")

    expected_initial = {
        ("training", manifest.training_seeds[-1], replicate, "B1")
        for replicate in manifest.replicate_indices
    }
    initial_keys = {_snapshot_key(snapshot) for snapshot in manifest.initial_memory_snapshots}
    if initial_keys and initial_keys != expected_initial:
        raise ValueError("initial snapshots must be one final B1 training snapshot per replicate")
    if manifest.frozen_memory_snapshots and initial_keys != expected_initial:
        raise ValueError("frozen snapshots require every final B1 training snapshot")
    if manifest.evidence_status == "evidence_collected" and (
        initial_keys != expected_initial or frozen_keys != expected_frozen
    ):
        raise ValueError("evidence_collected requires complete initial and frozen snapshots")

    initial_ids = [snapshot.snapshot_id for snapshot in manifest.initial_memory_snapshots]
    if len(initial_ids) != len(set(initial_ids)):
        raise ValueError("final training snapshot IDs must be unique across replicates")
    initials_by_replicate = {
        snapshot.replicate_index: snapshot for snapshot in manifest.initial_memory_snapshots
    }
    for snapshot in manifest.initial_memory_snapshots:
        if (
            not snapshot.immutable
            or snapshot.ancestry_snapshot_id is not None
            or snapshot.quarantine_namespace is not None
            or snapshot.quarantine_provenance_ref is not None
            or snapshot.quarantine_audit_hash is not None
        ):
            raise ValueError("initial B1 snapshots must be immutable final training snapshots")

    frozen_by_replicate: dict[int, tuple[object, ...]] = {}
    frozen_snapshot_owners: dict[str, int] = {}
    quarantine_namespaces: set[str] = set()
    for snapshot in manifest.frozen_memory_snapshots:
        ancestry = initials_by_replicate.get(snapshot.replicate_index)
        if ancestry is None or (
            snapshot.memory_namespace != ancestry.memory_namespace
            or snapshot.memory_operation_order != ancestry.memory_operation_order
            or snapshot.ancestry_snapshot_id != ancestry.snapshot_id
            or snapshot.ancestry_content_hash != ancestry.content_hash
            or snapshot.content_hash != ancestry.content_hash
        ):
            raise ValueError("frozen snapshot must preserve same-replicate final training ancestry")
        signature = (
            snapshot.snapshot_id,
            snapshot.content_hash,
            snapshot.memory_namespace,
            snapshot.memory_operation_order,
            snapshot.ancestry_snapshot_id,
            snapshot.ancestry_content_hash,
            snapshot.captured_at,
        )
        existing = frozen_by_replicate.setdefault(snapshot.replicate_index, signature)
        if existing != signature:
            raise ValueError("every evaluation seed in a replicate must use the same frozen input")
        owner = frozen_snapshot_owners.setdefault(snapshot.snapshot_id, snapshot.replicate_index)
        if owner != snapshot.replicate_index:
            raise ValueError("frozen snapshot IDs must not cross replicates")
        if snapshot.quarantine_namespace in quarantine_namespaces:
            raise ValueError("each evaluation cell requires a distinct quarantine namespace")
        quarantine_namespaces.add(snapshot.quarantine_namespace)


def _expected_blocks(manifest: Stage0Manifest | Stage0Profile) -> set[tuple[str, int, int]]:
    return {
        (phase, seed, replicate)
        for phase, seeds in (
            ("training", manifest.training_seeds),
            ("evaluation", manifest.evaluation_seeds),
        )
        for seed in seeds
        for replicate in manifest.replicate_indices
    }


def build_run_matrix(profile: Stage0Profile) -> tuple[RunMatrixBlock, ...]:
    """Build a paired B0/B1 matrix without executing anything."""

    blocks: list[RunMatrixBlock] = []
    for phase, seeds in (
        ("training", profile.training_seeds),
        ("evaluation", profile.evaluation_seeds),
    ):
        for seed in seeds:
            for replicate in profile.replicate_indices:
                block_id = f"{phase}:{profile.environment.scenario_id}:{seed}:r{replicate}"
                blocks.append(
                    RunMatrixBlock(
                        block_id=block_id,
                        phase=phase,
                        environment_id=profile.environment.environment_id,
                        scenario_id=profile.environment.scenario_id,
                        world_seed=seed,
                        replicate_index=replicate,
                    )
                )
    return tuple(blocks)


def resolved_manifest(
    profile: Stage0Profile,
    *,
    source_revision: str,
    source_dirty: bool | None = None,
    source_tree_hash: str,
    dependency_lock_hash: str,
    runtime_fingerprint: str | None = None,
    project_fingerprint: str | None = None,
    planner_fingerprint: str | None = None,
    resolved_prompt_fingerprint: str | None = None,
    resolved_settings_fingerprint: str | None = None,
    resolved_endpoint_fingerprint: str | None = None,
    created_at: datetime | None = None,
) -> Stage0Manifest:
    """Resolve local fingerprints into a preregistration-only manifest."""

    resolved_prompt_fingerprint = (
        profile.provider.prompt_hash
        if resolved_prompt_fingerprint is None
        else resolved_prompt_fingerprint
    )
    resolved_settings_fingerprint = (
        profile.provider.settings_fingerprint
        if resolved_settings_fingerprint is None
        else resolved_settings_fingerprint
    )
    resolved_endpoint_fingerprint = (
        profile.environment.endpoint_fingerprint
        if resolved_endpoint_fingerprint is None
        else resolved_endpoint_fingerprint
    )
    profile_hash = sha256_json(profile)
    fingerprint_payload = _fingerprint_payload(
        profile_hash=profile_hash,
        source_revision=source_revision,
        source_dirty=source_dirty,
        source_tree_hash=source_tree_hash,
        dependency_lock_hash=dependency_lock_hash,
        runtime_fingerprint=runtime_fingerprint,
        project_fingerprint=project_fingerprint,
        planner_fingerprint=planner_fingerprint,
        resolved_prompt_fingerprint=resolved_prompt_fingerprint,
        resolved_settings_fingerprint=resolved_settings_fingerprint,
        resolved_endpoint_fingerprint=resolved_endpoint_fingerprint,
    )
    config_fingerprint = sha256_json(fingerprint_payload)
    manifest_id = f"{profile.profile_id}-{sha256_json(fingerprint_payload)[:16]}"
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": manifest_id,
        "profile_id": profile.profile_id,
        "created_at": created_at or datetime.now(UTC),
        "evidence_status": "preregistration_only",
        "source_revision": source_revision,
        "source_dirty": source_dirty,
        "source_tree_hash": source_tree_hash,
        "dependency_lock_hash": dependency_lock_hash,
        "runtime_fingerprint": runtime_fingerprint,
        "project_fingerprint": project_fingerprint,
        "planner_fingerprint": planner_fingerprint,
        "resolved_prompt_fingerprint": resolved_prompt_fingerprint,
        "resolved_settings_fingerprint": resolved_settings_fingerprint,
        "resolved_endpoint_fingerprint": resolved_endpoint_fingerprint,
        "profile_hash": profile_hash,
        "resolved_config_fingerprint": config_fingerprint,
        "environment": profile.environment,
        "provider": profile.provider,
        "memory_conditions": profile.memory_conditions,
        "training_seeds": profile.training_seeds,
        "evaluation_seeds": profile.evaluation_seeds,
        "replicate_indices": profile.replicate_indices,
        "canonical_metrics": profile.canonical_metrics,
        "guardrail_metrics": profile.guardrail_metrics,
        "failure_policy": profile.failure_policy,
        "policy_references": profile.policy_references,
        "run_matrix": build_run_matrix(profile),
        "initial_memory_snapshots": (),
        "frozen_memory_snapshots": (),
        "notes": profile.notes,
    }
    payload["manifest_hash"] = manifest_hash(payload)
    return Stage0Manifest.model_validate(payload)


def _numeric(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile of an empty list")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _distribution(
    *,
    phase: Literal["training", "evaluation"],
    condition: Literal["B0", "B1"],
    metric_id: str,
    attempts: list[AttemptRecord],
    selected_attempts: list[AttemptRecord] | None = None,
) -> MetricDistribution:
    selected = attempts if selected_attempts is None else selected_attempts
    completed = [attempt for attempt in selected if attempt.status == "completed"]
    raw_values = [attempt.metrics.get(metric_id) for attempt in completed]
    values = [value for raw_value in raw_values if (value := _numeric(raw_value)) is not None]
    return MetricDistribution(
        phase=phase,
        condition=condition,
        metric_id=metric_id,
        attempts_total=len(attempts),
        selected_attempts=len(selected),
        completed_attempts=len(completed),
        count=len(values),
        mean=fmean(values) if values else None,
        variance=variance(values) if len(values) >= 2 else None,
        percentiles=(
            {
                label: _percentile(values, quantile)
                for label, quantile in zip(PERCENTILES, (0.10, 0.25, 0.50, 0.75, 0.90), strict=True)
            }
            if values
            else {}
        ),
        categorical_counts=dict(Counter(value for value in raw_values if isinstance(value, str))),
    )


def _paired_report(
    manifest: Stage0Manifest,
    phase: Literal["training", "evaluation"],
    attempts_by_key: Mapping[tuple[str, str], AttemptRecord],
) -> PairedReport:
    phase_blocks = [block for block in manifest.run_matrix if block.phase == phase]
    deltas: list[PairedDelta] = []
    incomplete: list[str] = []
    for block in phase_blocks:
        baseline = attempts_by_key.get((block.block_id, "B0"))
        candidate = attempts_by_key.get((block.block_id, "B1"))
        if (
            baseline is None
            or candidate is None
            or baseline.status != "completed"
            or candidate.status != "completed"
        ):
            incomplete.append(block.block_id)
            continue
        block_deltas: dict[str, float] = {}
        for metric_id in manifest.canonical_metrics:
            left = _numeric(baseline.metrics.get(metric_id))
            right = _numeric(candidate.metrics.get(metric_id))
            if left is not None and right is not None:
                block_deltas[metric_id] = right - left
        deltas.append(
            PairedDelta(
                block_id=block.block_id,
                phase=phase,
                world_seed=block.world_seed,
                replicate_index=block.replicate_index,
                deltas=block_deltas,
            )
        )

    delta_distributions: dict[str, MetricDistribution] = {}
    for metric_id in manifest.canonical_metrics:
        values = [delta.deltas[metric_id] for delta in deltas if metric_id in delta.deltas]
        delta_distributions[metric_id] = MetricDistribution(
            phase=phase,
            condition="B1",
            metric_id=f"paired_delta:{metric_id}",
            attempts_total=len(deltas),
            selected_attempts=len(deltas),
            completed_attempts=len(deltas),
            count=len(values),
            mean=fmean(values) if values else None,
            variance=variance(values) if len(values) >= 2 else None,
            percentiles=(
                {
                    label: _percentile(values, quantile)
                    for label, quantile in zip(
                        PERCENTILES, (0.10, 0.25, 0.50, 0.75, 0.90), strict=True
                    )
                }
                if values
                else {}
            ),
        )
    return PairedReport(
        phase=phase,
        total_blocks=len(phase_blocks),
        complete_blocks=len(deltas),
        incomplete_block_ids=incomplete,
        deltas=deltas,
        delta_distributions=delta_distributions,
    )


def _cell_id(block_id: str, condition: str) -> str:
    return f"{block_id}::{condition}"


def _select_terminal_attempts(
    attempts: list[AttemptRecord],
    *,
    max_attempts_per_cell: int,
) -> tuple[dict[tuple[str, str], AttemptRecord], dict[tuple[str, str], list[AttemptRecord]]]:
    """Retain every attempt but select only the highest-index terminal attempt per cell."""

    by_id: dict[str, AttemptRecord] = {}
    by_cell: dict[tuple[str, str], list[AttemptRecord]] = {}
    for attempt in attempts:
        if attempt.attempt_id in by_id:
            raise ValueError(f"attempt_id must be globally unique: {attempt.attempt_id}")
        by_id[attempt.attempt_id] = attempt
        by_cell.setdefault((attempt.block_id, attempt.condition), []).append(attempt)

    for attempt in attempts:
        if attempt.retry_of is None:
            continue
        parent = by_id.get(attempt.retry_of)
        if parent is None:
            raise ValueError(f"attempt {attempt.attempt_id} retry_of is unknown")
        if (parent.block_id, parent.condition) != (attempt.block_id, attempt.condition):
            raise ValueError(f"attempt {attempt.attempt_id} retry_of crosses cells")
        if parent.attempt_index != attempt.attempt_index - 1:
            raise ValueError(f"attempt {attempt.attempt_id} retry_of is not the previous attempt")
        if (
            parent.status not in {"failed", "interrupted"}
            or parent.failure_class not in RETRY_ELIGIBLE_FAILURE_CLASSES
        ):
            raise ValueError(
                f"attempt {attempt.attempt_id} retries a non-retry-eligible {parent.status} attempt"
            )

    selected: dict[tuple[str, str], AttemptRecord] = {}
    for key, cell_attempts in by_cell.items():
        if len(cell_attempts) > max_attempts_per_cell:
            raise ValueError(
                f"block/condition {key} exceeds max_attempts_per_cell={max_attempts_per_cell}"
            )
        indexes = [attempt.attempt_index for attempt in cell_attempts]
        if len(indexes) != len(set(indexes)):
            raise ValueError(f"duplicate attempt_index in block/condition {key}")
        if set(indexes) != set(range(max(indexes) + 1)):
            raise ValueError(f"attempt indexes must be contiguous in block/condition {key}")
        latest = max(cell_attempts, key=lambda attempt: attempt.attempt_index)
        if latest.status in TERMINAL_ATTEMPT_STATUSES:
            selected[key] = latest
    return selected, by_cell


def _memory_provenance(
    manifest: Stage0Manifest,
    selected: Mapping[tuple[str, str], AttemptRecord],
    by_cell: Mapping[tuple[str, str], list[AttemptRecord]],
) -> MemoryProvenanceReport:
    expected = {
        (block.phase, block.world_seed, block.replicate_index, "B1")
        for block in manifest.run_matrix
        if block.phase == "evaluation"
    }
    block_by_triplet = {
        (block.phase, block.world_seed, block.replicate_index): block
        for block in manifest.run_matrix
    }
    b1_cells = {(block.block_id, "B1"): block for block in manifest.run_matrix}
    b1_attempts = [
        attempt
        for (block_id, condition), cell_attempts in by_cell.items()
        if condition == "B1" and (block_id, "B1") in b1_cells
        for attempt in cell_attempts
    ]
    invalid_namespaces: set[str] = set()
    namespace_by_replicate: dict[int, str] = {}
    for (block_id, condition), cell_attempts in by_cell.items():
        if condition != "B1" or (block_id, "B1") not in b1_cells:
            continue
        block = b1_cells[(block_id, "B1")]
        namespaces = {attempt.memory_namespace for attempt in cell_attempts}
        orders = {attempt.memory_operation_order for attempt in cell_attempts}
        if len(namespaces) != 1 or None in namespaces or len(orders) != 1 or None in orders:
            invalid_namespaces.add(_cell_id(block_id, "B1"))
            continue
        namespace = next(iter(namespaces))
        expected_namespace = namespace_by_replicate.setdefault(block.replicate_index, namespace)
        if namespace != expected_namespace:
            invalid_namespaces.add(_cell_id(block_id, "B1"))

    namespace_owners: dict[str, int] = {}
    for replicate, namespace in namespace_by_replicate.items():
        owner = namespace_owners.setdefault(namespace, replicate)
        if owner != replicate:
            for (block_id, condition), block in b1_cells.items():
                if condition == "B1" and block.replicate_index in {owner, replicate}:
                    invalid_namespaces.add(_cell_id(block_id, condition))

    # Each training seed is one logical carry operation. Retries must retain the
    # same operation order, and all evaluation attempts read the final order.
    for replicate in manifest.replicate_indices:
        final_order = len(manifest.training_seeds) - 1
        expected_namespace = namespace_by_replicate.get(replicate)
        for order, seed in enumerate(manifest.training_seeds):
            block = block_by_triplet[("training", seed, replicate)]
            cell = (block.block_id, "B1")
            attempts = by_cell.get(cell, [])
            if any(
                attempt.memory_operation_order != order
                or attempt.memory_namespace != expected_namespace
                for attempt in attempts
            ):
                invalid_namespaces.add(_cell_id(block.block_id, "B1"))
        for seed in manifest.evaluation_seeds:
            block = block_by_triplet[("evaluation", seed, replicate)]
            cell = (block.block_id, "B1")
            attempts = by_cell.get(cell, [])
            if any(
                attempt.memory_operation_order != final_order
                or attempt.memory_namespace != expected_namespace
                for attempt in attempts
            ):
                invalid_namespaces.add(_cell_id(block.block_id, "B1"))

    snapshots = {
        (
            snapshot.phase,
            snapshot.world_seed,
            snapshot.replicate_index,
            snapshot.condition,
        ): snapshot
        for snapshot in manifest.frozen_memory_snapshots
    }
    missing = sorted(
        _cell_id(block_by_triplet[(phase, seed, replicate)].block_id, condition)
        for phase, seed, replicate, condition in expected - set(snapshots)
    )
    invalid_snapshots: list[str] = []
    initial_snapshots = {
        snapshot.snapshot_id: snapshot for snapshot in manifest.initial_memory_snapshots
    }
    for key in expected & set(snapshots):
        snapshot = snapshots[key]
        block = block_by_triplet[key[:3]]
        cell = (block.block_id, "B1")
        cell_name = _cell_id(block.block_id, "B1")
        if snapshot.condition != "B1" or not snapshot.immutable:
            invalid_snapshots.append(cell_name)
        ancestry = initial_snapshots.get(snapshot.ancestry_snapshot_id)
        if ancestry is None or (
            ancestry.content_hash != snapshot.ancestry_content_hash
            or ancestry.replicate_index != snapshot.replicate_index
            or ancestry.memory_namespace != snapshot.memory_namespace
            or ancestry.memory_operation_order != len(manifest.training_seeds) - 1
        ):
            invalid_snapshots.append(cell_name)
        for attempt in by_cell.get(cell, []):
            if (
                attempt.memory_namespace != snapshot.memory_namespace
                or attempt.memory_snapshot_id != snapshot.snapshot_id
                or attempt.memory_snapshot_hash != snapshot.content_hash
                or attempt.memory_operation_order is None
                or attempt.quarantine_namespace != snapshot.quarantine_namespace
                or attempt.quarantine_provenance_ref != snapshot.quarantine_provenance_ref
                or attempt.quarantine_audit_hash != snapshot.quarantine_audit_hash
            ):
                invalid_snapshots.append(cell_name)
    namespace_complete = bool(b1_attempts) and all(
        attempt.memory_namespace is not None and attempt.memory_operation_order is not None
        for attempt in b1_attempts
    )
    return MemoryProvenanceReport(
        b1_namespace_complete=namespace_complete and not invalid_namespaces,
        frozen_snapshot_complete=not missing and not invalid_snapshots,
        invalid_namespace_cells=tuple(sorted(invalid_namespaces)),
        missing_snapshot_cells=tuple(missing),
        invalid_snapshot_cells=tuple(sorted(set(invalid_snapshots))),
    )


def _raw_capture_incompleteness(
    manifest: Stage0Manifest, attempts: Iterable[AttemptRecord]
) -> list[str]:
    """Return declaration failures for every retained terminal attempt.

    This binds attempt declarations to the sealed raw-content policy.  It does
    not dereference a result or capture artifact, so it cannot attest to the
    external body's existence or contents.
    """

    policy = manifest.policy_references.raw_content
    expected_policy_fingerprint = sha256_json(policy)
    classes = {
        "prompts": policy.prompts,
        "observations": policy.observations,
        "decision_traces": policy.decision_traces,
    }
    reasons: list[str] = []
    terminal_audit_refs: set[str] = set()
    for attempt in attempts:
        if attempt.status not in TERMINAL_ATTEMPT_STATUSES:
            continue
        if attempt.result_ref is None or attempt.result_hash is None:
            reasons.append("terminal_attempt_missing_result_evidence")
        capture = attempt.raw_content_capture
        if capture is None:
            reasons.append("terminal_attempt_missing_raw_capture_audit")
            continue
        if capture.audit_ref in terminal_audit_refs:
            reasons.append("terminal_raw_capture_audit_reused")
        terminal_audit_refs.add(capture.audit_ref)
        if capture.policy_fingerprint != expected_policy_fingerprint:
            reasons.append("terminal_attempt_raw_capture_policy_mismatch")
        for name, enabled in classes.items():
            state = getattr(capture, name).state
            if state == "quarantined":
                reasons.append("terminal_attempt_raw_content_quarantined")
            if enabled:
                if state == "disabled":
                    reasons.append("enabled_raw_content_class_disabled")
                elif attempt.status == "completed" and state != "captured":
                    reasons.append("completed_attempt_missing_enabled_raw_capture")
                elif attempt.status != "completed" and state not in {
                    "captured",
                    "not_emitted",
                }:
                    reasons.append("terminal_attempt_invalid_enabled_raw_capture")
            elif state != "disabled":
                reasons.append("disabled_raw_content_class_not_disabled")
    return reasons


def aggregate_report(
    manifest: Stage0Manifest,
    attempts: Iterable[AttemptRecord],
    *,
    generated_at: datetime | None = None,
) -> Stage0Report:
    """Aggregate every retry and use one explicit terminal selection for analytics."""

    # Pydantic's model_copy(update=...) intentionally skips validation. Re-enter
    # every artifact through its serialized boundary before it can influence an
    # evidence verdict, including artifacts supplied by in-process callers.
    manifest = Stage0Manifest.model_validate(manifest.model_dump(mode="json"))
    attempt_list = [
        AttemptRecord.model_validate(attempt.model_dump(mode="json")) for attempt in attempts
    ]
    matrix_by_id = {block.block_id: block for block in manifest.run_matrix}
    for attempt in attempt_list:
        if attempt.manifest_id != manifest.manifest_id:
            raise ValueError(f"attempt {attempt.attempt_id} does not match manifest")
        block = matrix_by_id.get(attempt.block_id)
        if block is None:
            raise ValueError(
                f"attempt {attempt.attempt_id} references unknown block {attempt.block_id}"
            )
        if (
            attempt.phase != block.phase
            or attempt.world_seed != block.world_seed
            or attempt.replicate_index != block.replicate_index
            or attempt.condition not in block.conditions
        ):
            raise ValueError(f"attempt {attempt.attempt_id} does not match its matrix block")

    selected, by_cell = _select_terminal_attempts(
        attempt_list,
        max_attempts_per_cell=manifest.failure_policy.max_attempts_per_cell,
    )
    status_counts = Counter({status: 0 for status in ATTEMPT_STATUSES})
    status_counts.update(attempt.status for attempt in attempt_list)
    selected_by_cell = {key: attempt for key, attempt in selected.items()}

    coverage: list[BlockConditionCoverage] = []
    missing_cells: list[str] = []
    incomplete_cells: list[str] = []
    for block in manifest.run_matrix:
        for condition in block.conditions:
            key = (block.block_id, condition)
            cell_attempts = by_cell.get(key, [])
            terminal = [
                attempt for attempt in cell_attempts if attempt.status in TERMINAL_ATTEMPT_STATUSES
            ]
            selected_attempt = selected.get(key)
            cell_name = _cell_id(block.block_id, condition)
            if selected_attempt is None:
                missing_cells.append(cell_name)
                incomplete_cells.append(cell_name)
            elif selected_attempt.status != "completed":
                incomplete_cells.append(cell_name)
            coverage.append(
                BlockConditionCoverage(
                    block_id=block.block_id,
                    phase=block.phase,
                    condition=condition,
                    world_seed=block.world_seed,
                    replicate_index=block.replicate_index,
                    expected_attempts=1,
                    observed_attempts=len(cell_attempts),
                    terminal_attempts=len(terminal),
                    observed_attempt_ids=tuple(attempt.attempt_id for attempt in cell_attempts),
                    selected_terminal_attempt_id=(
                        selected_attempt.attempt_id if selected_attempt is not None else None
                    ),
                )
            )

    condition_reports: list[ConditionReport] = []
    for phase in PHASES:
        for condition in CONDITIONS:
            selected_cells = {
                key: attempt
                for key, attempt in selected_by_cell.items()
                if attempt.phase == phase and attempt.condition == condition
            }
            retained = [
                attempt
                for attempt in attempt_list
                if attempt.phase == phase and attempt.condition == condition
            ]
            terminal_count = sum(
                attempt.status in TERMINAL_ATTEMPT_STATUSES for attempt in retained
            )
            selected_attempts = list(selected_cells.values())
            condition_statuses = Counter({status: 0 for status in ATTEMPT_STATUSES})
            condition_statuses.update(attempt.status for attempt in retained)
            condition_reports.append(
                ConditionReport(
                    phase=phase,
                    condition=condition,
                    attempts_total=len(retained),
                    terminal_attempts=terminal_count,
                    selected_attempt_ids=tuple(attempt.attempt_id for attempt in selected_attempts),
                    status_counts=dict(condition_statuses),
                    metric_distributions=[
                        _distribution(
                            phase=phase,
                            condition=condition,
                            metric_id=metric_id,
                            attempts=retained,
                            selected_attempts=selected_attempts,
                        )
                        for metric_id in manifest.canonical_metrics
                    ],
                )
            )

    memory_provenance = _memory_provenance(manifest, selected_by_cell, by_cell)
    reasons: list[str] = []
    missing_provenance = _missing_provenance(manifest)
    if manifest.evidence_status != "evidence_collected":
        reasons.append("manifest_not_evidence_collected")
    if missing_provenance:
        reasons.append("missing_resolved_provenance")
    if missing_cells:
        reasons.append("missing_terminal_attempts")
    if incomplete_cells:
        reasons.append("incomplete_block_conditions")
    if not memory_provenance.b1_namespace_complete:
        reasons.append("missing_b1_memory_namespace_or_order")
    if not memory_provenance.frozen_snapshot_complete:
        reasons.append("missing_or_invalid_frozen_memory_snapshots")
    for attempt in attempt_list:
        if attempt.status in TERMINAL_ATTEMPT_STATUSES and attempt.runtime_metadata_hash is None:
            reasons.append("terminal_attempt_missing_runtime_metadata")
            break
    for attempt in attempt_list:
        if (
            attempt.condition == "B0"
            and attempt.status in TERMINAL_ATTEMPT_STATUSES
            and (attempt.no_memory_audit_hash is None)
        ):
            reasons.append("b0_terminal_attempt_missing_no_memory_audit")
            break
    reasons.extend(_raw_capture_incompleteness(manifest, attempt_list))

    observed_block_ids = {block_id for block_id, condition in by_cell if condition in CONDITIONS}
    expected_block_ids = {block.block_id for block in manifest.run_matrix}
    return Stage0Report(
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        attempts_hash=sha256_json([attempt.model_dump(mode="json") for attempt in attempt_list]),
        generated_at=generated_at or datetime.now(UTC),
        total_attempts=len(attempt_list),
        status_counts=dict(status_counts),
        retained_attempt_ids=tuple(attempt.attempt_id for attempt in attempt_list),
        retained_attempts=tuple(attempt_list),
        selected_terminal_attempt_ids={
            _cell_id(block_id, condition): attempt.attempt_id
            for (block_id, condition), attempt in selected_by_cell.items()
        },
        expected_block_count=len(expected_block_ids),
        observed_block_count=len(expected_block_ids & observed_block_ids),
        expected_block_condition_count=len(coverage),
        observed_block_condition_count=sum(item.observed_attempts > 0 for item in coverage),
        missing_block_ids=tuple(
            block_id
            for block_id in (block.block_id for block in manifest.run_matrix)
            if block_id not in observed_block_ids
        ),
        missing_block_conditions=tuple(missing_cells),
        incomplete_block_conditions=tuple(incomplete_cells),
        coverage=tuple(coverage),
        evidence_complete=not reasons,
        evidence_incompleteness_reasons=tuple(dict.fromkeys(reasons)),
        memory_provenance=memory_provenance,
        condition_reports=condition_reports,
        paired_reports=[_paired_report(manifest, phase, selected_by_cell) for phase in PHASES],
    )


def verify_report(manifest: Stage0Manifest, report: Stage0Report) -> None:
    """Recompute a report against its sealed manifest and retained attempts.

    A report is not independently authoritative: consumers must load the
    referenced manifest and use this check before treating ``evidence_complete``
    as evidence.
    """

    if report.manifest_id != manifest.manifest_id or report.manifest_hash != manifest.manifest_hash:
        raise ValueError("report does not reference the supplied sealed manifest")
    recomputed = aggregate_report(
        manifest,
        report.retained_attempts,
        generated_at=report.generated_at,
    )
    if recomputed.model_dump(mode="json") != report.model_dump(mode="json"):
        raise ValueError("report contents do not match recomputed evidence")
