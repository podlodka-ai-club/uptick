from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from uptick_agent.evaluation import (
    FrozenEvaluationBinding,
    ProviderTelemetry,
    V2AttemptRecord,
    V2Condition,
    V2EnvironmentPin,
    V2EvaluationProfile,
    V2OutcomeMetrics,
    V2PlannedContrast,
    V2ProviderPin,
    V2SnapshotRef,
    V2SourcePin,
    aggregate_report,
    freeze_evaluation_binding,
    manifest_hash,
    resolved_manifest,
    select_first_attempts,
    verify_report,
)
from uptick_agent.memory.config import AuditConfiguration, MemoryConfiguration
from uptick_agent.stage0 import CANONICAL_METRICS, AttemptRecord
from uptick_agent.stage0 import sha256_json as stage0_sha256_json

NOW = datetime(2026, 9, 5, tzinfo=UTC)
HASH = "a" * 64


def _profile(*, context_verified: bool = False) -> V2EvaluationProfile:
    configs = (MemoryConfiguration.legacy_baseline(), MemoryConfiguration.episodic_only())
    environment = V2EnvironmentPin(
        environment_id="simulator",
        environment_version="2",
        adapter_id="simulator-v2",
        adapter_version="1",
        scenario_id="default",
        api_contract_fingerprint=HASH,
        context_identity_verified=context_verified,
        environment_content_hash=HASH if context_verified else None,
        scenario_content_hash=HASH if context_verified else None,
    )
    settings = {"temperature": 0}
    return V2EvaluationProfile(
        profile_id="v2-pilot",
        environment=environment,
        provider=V2ProviderPin(
            provider="fake",
            model="test-model",
            settings=settings,
            prompt_fingerprint=HASH,
            settings_fingerprint=stage0_sha256_json(settings),
            token_estimator_id="chars",
            token_estimator_version="1",
            policy_id="provider-policy-v1",
            policy_version="1.0",
        ),
        source=V2SourcePin(
            source_revision="b" * 40,
            source_tree_hash=HASH,
            dependency_lock_hash=HASH,
        ),
        conditions=tuple(
            V2Condition(
                condition_id=condition_id,
                memory_configuration=config,
                memory_configuration_fingerprint=config.fingerprint,
            )
            for condition_id, config in zip(("A0", "A1"), configs, strict=True)
        ),
        baseline_condition_id="A0",
        training_seeds=(1,),
        evaluation_seeds=(2, 3),
        replicate_indices=(0,),
        budget={"max_steps": 10},
        audit_configuration=AuditConfiguration(),
    )


def _manifest(*, context_verified: bool = False):
    return resolved_manifest(_profile(context_verified=context_verified), created_at=NOW)


def _attempt(
    manifest,
    block_id: str,
    condition_id: str,
    *,
    attempt_index: int = 0,
    retry_of: str | None = None,
    status: str = "completed",
    slo_passed: bool = True,
    total_cost_minor: int = 20,
    attempt_id: str | None = None,
) -> V2AttemptRecord:
    block = next(item for item in manifest.run_matrix if item.block_id == block_id)
    attempt_id = attempt_id or f"{block_id}:{condition_id}:{attempt_index}"
    failed = status in {"failed", "interrupted"}
    requested_at = NOW - timedelta(seconds=20) if block.phase == "training" else NOW
    return V2AttemptRecord(
        manifest_id=manifest.manifest_id,
        attempt_id=attempt_id,
        logical_run_id=f"logical:{block_id}:{condition_id}",
        block_id=block_id,
        phase=block.phase,
        condition_id=condition_id,
        environment_id=block.environment_id,
        scenario_id=block.scenario_id,
        world_seed=block.world_seed,
        replicate_index=block.replicate_index,
        attempt_index=attempt_index,
        retry_of=retry_of,
        status=status,
        run_id=None if failed else f"run:{attempt_id}",
        failure_stage="startup" if failed else None,
        failure_class="transient" if failed else None,
        failure_reason="startup unavailable" if failed else None,
        requested_at=requested_at,
        finished_at=requested_at + timedelta(seconds=1),
        outcome=(
            None
            if failed
            else V2OutcomeMetrics(
                run_status="completed",
                uptime_ratio=0.995,
                slo_passed=slo_passed,
                total_cost_minor=total_cost_minor,
                steps=3,
                duration_seconds=1.0,
            )
        ),
    )


def _attempts(manifest, *, failed_cell: tuple[str, str] | None = None):
    attempts = []
    for block in manifest.run_matrix:
        for condition_id in block.conditions:
            if (block.block_id, condition_id) == failed_cell:
                first = _attempt(manifest, block.block_id, condition_id, status="failed")
                attempts.extend(
                    [
                        first,
                        _attempt(
                            manifest,
                            block.block_id,
                            condition_id,
                            attempt_index=1,
                            retry_of=first.attempt_id,
                            total_cost_minor=1,
                        ),
                    ]
                )
            else:
                attempts.append(_attempt(manifest, block.block_id, condition_id))
    return attempts


def _bindings(manifest, attempts):
    training = {
        condition_id: next(
            item
            for item in attempts
            if item.phase == "training"
            and item.condition_id == condition_id
            and item.attempt_index == 0
        )
        for condition_id in ("A0", "A1")
    }
    bindings = tuple(
        freeze_evaluation_binding(
            manifest,
            condition_id=condition_id,
            cache_namespace=f"cache-{condition_id}",
            audit_namespace=f"audit-{condition_id}",
            snapshot_refs=()
            if condition_id == "A0"
            else (
                V2SnapshotRef(namespace="memory-a1", snapshot_id="snapshot-a1", content_hash=HASH),
            ),
            training_attempt_ids=(training[condition_id].attempt_id,),
            training_world_contexts={
                training[condition_id].world_seed: manifest.profile.environment
            },
            created_at=NOW - timedelta(seconds=10),
        )
        for condition_id in ("A0", "A1")
    )
    for index, attempt in enumerate(attempts):
        if attempt.phase == "evaluation":
            binding = next(item for item in bindings if item.condition_id == attempt.condition_id)
            attempts[index] = attempt.model_copy(update={"frozen_binding_id": binding.binding_id})
    return bindings


def test_v2_profile_is_sealed_and_keeps_v1_metrics_separate() -> None:
    manifest = _manifest()
    assert manifest.manifest_hash == manifest_hash(manifest)
    assert manifest.profile.objective_kind == "uptime_cost"
    assert "uptime_ratio" not in CANONICAL_METRICS
    with pytest.raises(ValidationError):
        AttemptRecord.model_validate(
            {
                "manifest_id": "m",
                "attempt_id": "a",
                "block_id": "b",
                "phase": "training",
                "condition": "B0",
                "world_seed": 1,
                "replicate_index": 0,
                "status": "completed",
                "metrics": {"uptime_ratio": 1},
            }
        )


def test_declared_contrasts_replace_default_baseline_comparisons() -> None:
    profile = _profile().model_copy(
        update={
            "planned_contrasts": (
                V2PlannedContrast(baseline_condition_id="A1", candidate_condition_id="A0"),
            )
        }
    )
    manifest = resolved_manifest(profile, created_at=NOW)
    attempts = _attempts(manifest)
    bindings = _bindings(manifest, attempts)

    report = aggregate_report(manifest, attempts, frozen_bindings=bindings)

    assert [
        (item.phase, item.baseline_condition_id, item.candidate_condition_id)
        for item in report.pairwise_reports
    ] == [("training", "A1", "A0"), ("evaluation", "A1", "A0")]


def test_profile_requires_resolved_settings_and_verified_context_hashes() -> None:
    with pytest.raises(ValidationError, match="settings_fingerprint"):
        V2ProviderPin(
            provider="fake",
            model="m",
            settings={"temperature": 0},
            prompt_fingerprint=HASH,
            settings_fingerprint=HASH,
            token_estimator_id="chars",
            token_estimator_version="1",
            policy_id="p",
            policy_version="1",
        )
    with pytest.raises(ValidationError, match="unverified"):
        V2EnvironmentPin(
            environment_id="e",
            environment_version="1",
            adapter_id="a",
            adapter_version="1",
            scenario_id="s",
            api_contract_fingerprint=HASH,
            environment_content_hash=HASH,
        )
    with pytest.raises(ValidationError, match="requires environment"):
        V2EnvironmentPin(
            environment_id="e",
            environment_version="1",
            adapter_id="a",
            adapter_version="1",
            scenario_id="s",
            api_contract_fingerprint=HASH,
            context_identity_verified=True,
        )


def test_first_failed_attempt_and_successful_retry_never_replace_primary_cell() -> None:
    manifest = _manifest()
    failed_cell = (manifest.run_matrix[1].block_id, "A1")
    attempts = _attempts(manifest, failed_cell=failed_cell)
    assert select_first_attempts(attempts)[failed_cell].status == "failed"
    report = aggregate_report(
        manifest, attempts, frozen_bindings=_bindings(manifest, attempts), generated_at=NOW
    )
    training_pair = next(item for item in report.pairwise_reports if item.phase == "training")
    assert training_pair.cost_eligible_pairs == 1
    assert training_pair.cost_ineligible_or_missing_pairs == 0
    evaluation_pair = next(item for item in report.pairwise_reports if item.phase == "evaluation")
    assert evaluation_pair.cost_eligible_pairs == 1
    assert evaluation_pair.cost_ineligible_or_missing_pairs == 1
    assert report.status_counts["failed"] == 1
    assert report.status_counts["completed"] == len(attempts) - 1


def test_completed_slo_failure_is_observed_but_excluded_from_cost_denominator() -> None:
    manifest = _manifest()
    attempts = _attempts(manifest)
    target = next(
        item for item in attempts if item.phase == "evaluation" and item.condition_id == "A1"
    )
    replacement = target.model_copy(
        update={
            "outcome": V2OutcomeMetrics(
                run_status="completed",
                uptime_ratio=0.7,
                slo_passed=False,
                total_cost_minor=0,
            )
        }
    )
    attempts[attempts.index(target)] = replacement
    report = aggregate_report(
        manifest, attempts, frozen_bindings=_bindings(manifest, attempts), generated_at=NOW
    )
    pair = next(item for item in report.pairwise_reports if item.phase == "evaluation")
    assert pair.candidate_completion_rate == 1
    assert pair.candidate_slo_pass_rate == 0.5
    assert pair.cost_eligible_pairs == 1
    assert pair.cost_delta_values == (0.0,)


def test_startup_failure_has_no_run_id_and_is_retained_as_terminal_attempt() -> None:
    manifest = _manifest()
    attempt = _attempt(manifest, manifest.run_matrix[0].block_id, "A0", status="failed")
    assert attempt.run_id is None
    assert attempt.started_at is None
    assert attempt.failure_stage == "startup"


def test_missing_first_cell_is_incomplete_and_rates_use_declared_denominator() -> None:
    manifest = _manifest()
    attempts = _attempts(manifest)
    missing = attempts.pop()
    bindings = _bindings(manifest, attempts)
    report = aggregate_report(manifest, attempts, frozen_bindings=bindings, generated_at=NOW)
    assert report.coverage_complete is False
    assert any(
        "missing_first_attempt" in reason for reason in report.evidence_incompleteness_reasons
    )
    condition = next(
        item
        for item in report.condition_reports
        if item.phase == missing.phase and item.condition_id == missing.condition_id
    )
    assert condition.expected_cells == 2
    assert condition.completion_rate == 0.5


def test_frozen_bindings_are_separate_hashed_artifacts_and_stale_hashes_fail() -> None:
    manifest = _manifest()
    attempts = _attempts(manifest)
    binding = _bindings(manifest, attempts)[1]
    assert binding.manifest_hash == manifest.manifest_hash
    assert manifest.manifest_hash == manifest_hash(manifest)
    tampered = binding.model_copy(
        update={
            "snapshot_refs": (
                V2SnapshotRef(
                    namespace="memory-a1", snapshot_id="snapshot-a1", content_hash="b" * 64
                ),
            )
        }
    )
    with pytest.raises(ValidationError, match="binding_hash"):
        FrozenEvaluationBinding.model_validate(tampered.model_dump(mode="json"))


def test_report_verification_detects_tampered_source_attempt_hash() -> None:
    manifest = _manifest()
    attempts = _attempts(manifest)
    bindings = _bindings(manifest, attempts)
    report = aggregate_report(manifest, attempts, frozen_bindings=bindings, generated_at=NOW)
    tampered = report.retained_attempts[0].model_copy(
        update={
            "outcome": report.retained_attempts[0].outcome.model_copy(
                update={"total_cost_minor": 999}
            )
        }
    )
    broken = report.model_copy(
        update={"retained_attempts": (tampered, *report.retained_attempts[1:])}
    )
    with pytest.raises(ValidationError, match="attempts_hash"):
        type(report).model_validate(broken.model_dump(mode="json"))
    with pytest.raises(ValueError, match="report does not match"):
        verify_report(manifest, broken, frozen_bindings=bindings)


def test_provider_telemetry_requires_explicit_measurement_state() -> None:
    assert ProviderTelemetry().status == "unavailable"
    telemetry = ProviderTelemetry(
        status="available",
        source="provider",
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        time_seconds=0.2,
    )
    assert telemetry.total_tokens == 15
    with pytest.raises(ValidationError, match="unavailable telemetry"):
        ProviderTelemetry(status="unavailable", source="unavailable", input_tokens=1)


def test_report_suppresses_provider_costs_with_mixed_currencies() -> None:
    manifest = _manifest()
    attempts = _attempts(manifest)
    evaluation_index = 0
    for index, attempt in enumerate(attempts):
        if attempt.phase != "evaluation":
            continue
        attempts[index] = attempt.model_copy(
            update={
                "provider_telemetry": ProviderTelemetry(
                    status="available",
                    source="provider",
                    input_tokens=1,
                    output_tokens=1,
                    total_tokens=2,
                    request_count=1,
                    usage_reported_requests=1,
                    cost_minor=1,
                    cost_currency="USD" if evaluation_index == 0 else "EUR",
                )
            }
        )
        evaluation_index += 1
    bindings = _bindings(manifest, attempts)

    report = aggregate_report(manifest, attempts, frozen_bindings=bindings)

    distribution = next(
        item
        for item in report.condition_reports
        if item.phase == "evaluation" and item.condition_id == "A0"
        for item in item.metric_distributions
        if item.metric_id == "provider_cost_minor"
    )
    assert distribution.count == 0
    assert distribution.mean is None
