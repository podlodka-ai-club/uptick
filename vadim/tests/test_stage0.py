import math
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from uptick_agent.stage0 import (
    AttemptRecord,
    CaptureState,
    EnvironmentProfile,
    FailurePolicy,
    MemoryCondition,
    MemorySnapshotRef,
    PolicyReferences,
    ProviderProfile,
    RawContentCapture,
    RawContentPolicy,
    Stage0Manifest,
    Stage0Profile,
    aggregate_report,
    canonical_json,
    manifest_hash,
    redact_text,
    resolved_manifest,
    sha256_json,
    sha256_tree,
    verify_report,
)

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 9, 4, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _profile(
    *, pinned: bool = False, raw: tuple[bool, bool, bool] = (True, True, True)
) -> Stage0Profile:
    return Stage0Profile(
        profile_id="stage0-test",
        environment=EnvironmentProfile(
            environment_id="synthetic",
            environment_version="1",
            adapter_id="adapter",
            adapter_version="1",
            scenario_id="default",
            endpoint_fingerprint=HASH_A if pinned else None,
        ),
        provider=ProviderProfile(
            provider="fake",
            model="fake",
            token_estimator_id="chars",
            token_estimator_version="1",
            prompt_hash=HASH_B if pinned else None,
            settings_fingerprint=HASH_C if pinned else None,
        ),
        memory_conditions=(
            MemoryCondition(
                condition_id="B0",
                memory_mode="none",
                training_mode="reset",
                evaluation_mode="empty",
            ),
            MemoryCondition(
                condition_id="B1",
                memory_mode="legacy",
                training_mode="carry",
                evaluation_mode="frozen",
            ),
        ),
        training_seeds=(1, 2),
        evaluation_seeds=(3, 4),
        replicate_indices=(0,),
        failure_policy=FailurePolicy(),
        policy_references=PolicyReferences(
            candidate_validation_policy="candidate@1",
            audit_retention_policy="retention@1",
            raw_content=RawContentPolicy(
                policy_id="raw-content-test",
                policy_version="1.0",
                prompts=raw[0],
                observations=raw[1],
                decision_traces=raw[2],
                retention_policy_ref="retention@1",
            ),
        ),
    )


def _manifest(
    *, pinned: bool = False, raw: tuple[bool, bool, bool] = (True, True, True)
) -> Stage0Manifest:
    profile = _profile(pinned=pinned, raw=raw)
    return resolved_manifest(
        profile,
        source_revision="f" * 40,
        source_dirty=False,
        source_tree_hash=HASH_A,
        dependency_lock_hash=HASH_B,
        runtime_fingerprint=HASH_C,
        project_fingerprint=HASH_D,
        planner_fingerprint="e" * 64,
        created_at=NOW,
    )


def _seal(payload: dict[object, object]) -> Stage0Manifest:
    payload["manifest_hash"] = manifest_hash(payload)
    return Stage0Manifest.model_validate(payload)


def _snapshotted_manifest(*, raw: tuple[bool, bool, bool] = (True, True, True)) -> Stage0Manifest:
    manifest = _manifest(pinned=True, raw=raw)
    initial = MemorySnapshotRef(
        snapshot_id="training-final-r0",
        phase="training",
        condition="B1",
        world_seed=2,
        replicate_index=0,
        memory_namespace="b1-r0",
        memory_operation_order=1,
        content_hash=HASH_B,
        immutable=True,
        captured_at=NOW,
    )
    frozen = [
        MemorySnapshotRef(
            snapshot_id="frozen-r0",
            phase="evaluation",
            condition="B1",
            world_seed=seed,
            replicate_index=0,
            memory_namespace="b1-r0",
            memory_operation_order=1,
            content_hash=HASH_B,
            immutable=True,
            captured_at=NOW,
            ancestry_snapshot_id=initial.snapshot_id,
            ancestry_content_hash=initial.content_hash,
            quarantine_namespace=f"quarantine-r0-s{seed}",
            quarantine_provenance_ref=f"overlay-record-s{seed}",
            quarantine_audit_hash=HASH_C,
        )
        for seed in (3, 4)
    ]
    payload = manifest.model_dump(mode="json")
    payload["evidence_status"] = "evidence_collected"
    payload["initial_memory_snapshots"] = [initial.model_dump(mode="json")]
    payload["frozen_memory_snapshots"] = [item.model_dump(mode="json") for item in frozen]
    return _seal(payload)


def _metrics() -> dict[str, int | str]:
    return {
        "final_balance_minor": 10,
        "successful_purchases": 1,
        "lost_purchases": 0,
        "revenue_minor": 10,
        "lost_revenue_minor": 0,
        "server_cost_minor": 1,
        "deployment_cost_minor": 1,
        "steps": 1,
        "completion_status": "completed",
    }


def _capture(manifest: Stage0Manifest, attempt_id: str) -> RawContentCapture:
    policy = manifest.policy_references.raw_content

    def state(enabled: bool, kind: str) -> CaptureState:
        if not enabled:
            return CaptureState(state="disabled")
        content_hash = sha256_json({"attempt_id": attempt_id, "kind": kind})
        return CaptureState(
            state="captured",
            artifact_ref=f"sha256:{content_hash}",
            content_hash=content_hash,
            redaction_audit_hash=sha256_json({"attempt_id": attempt_id, "redaction": kind}),
        )

    audit_hash = sha256_json({"attempt_id": attempt_id, "kind": "raw-capture-audit"})
    return RawContentCapture(
        manifest_id=manifest.manifest_id,
        attempt_id=attempt_id,
        audit_ref=f"sha256:{audit_hash}",
        audit_hash=audit_hash,
        policy_fingerprint=sha256_json(policy),
        prompts=state(policy.prompts, "prompts"),
        observations=state(policy.observations, "observations"),
        decision_traces=state(policy.decision_traces, "decision-traces"),
    )


def _attempt(
    manifest: Stage0Manifest,
    block_id: str,
    condition: str,
    *,
    index: int = 0,
    status: str = "completed",
    retry_of: str | None = None,
    failure_class: str | None = None,
) -> AttemptRecord:
    block = next(item for item in manifest.run_matrix if item.block_id == block_id)
    attempt_id = f"{block_id}-{condition}-{index}"
    result_hash = sha256_json({"attempt_id": attempt_id, "kind": "result"})
    capture = _capture(manifest, attempt_id)
    values: dict[str, object] = {
        "manifest_id": manifest.manifest_id,
        "attempt_id": attempt_id,
        "block_id": block_id,
        "phase": block.phase,
        "condition": condition,
        "world_seed": block.world_seed,
        "replicate_index": block.replicate_index,
        "attempt_index": index,
        "retry_of": retry_of,
        "status": status,
        "failure_class": failure_class,
        "runtime_metadata_hash": HASH_D,
        "result_ref": f"sha256:{result_hash}",
        "result_hash": result_hash,
        "metrics": _metrics() if status == "completed" else {},
        "raw_content_capture": capture,
    }
    values["trace_ref"] = capture.decision_traces.artifact_ref
    if condition == "B0":
        values["no_memory_audit_hash"] = HASH_C
    else:
        order = 0 if block.phase == "training" and block.world_seed == 1 else 1
        values |= {"memory_namespace": "b1-r0", "memory_operation_order": order}
        if block.phase == "evaluation":
            values |= {
                "memory_snapshot_id": "frozen-r0",
                "memory_snapshot_hash": HASH_B,
                "quarantine_namespace": f"quarantine-r0-s{block.world_seed}",
                "quarantine_provenance_ref": f"overlay-record-s{block.world_seed}",
                "quarantine_audit_hash": HASH_C,
            }
    return AttemptRecord(**values)


def _complete_attempts(manifest: Stage0Manifest) -> list[AttemptRecord]:
    return [
        _attempt(manifest, block.block_id, condition)
        for block in manifest.run_matrix
        for condition in ("B0", "B1")
    ]


def test_manifest_hash_profile_hash_config_and_matrix_are_self_verifying() -> None:
    manifest = _manifest()
    assert manifest.manifest_hash == manifest_hash(manifest)
    payload = manifest.model_dump(mode="json")
    payload["source_tree_hash"] = HASH_B
    with pytest.raises(ValidationError, match="resolved_config_fingerprint"):
        Stage0Manifest.model_validate(payload)
    payload = manifest.model_dump(mode="json")
    payload["run_matrix"][0]["block_id"] = "invented"
    payload["manifest_hash"] = manifest_hash(payload)
    with pytest.raises(ValidationError, match="run_matrix"):
        Stage0Manifest.model_validate(payload)


def test_profile_pins_and_evidence_source_revision_are_strict() -> None:
    profile = _profile(pinned=True)
    with pytest.raises(ValidationError, match="profile pin"):
        resolved_manifest(
            profile,
            source_revision="f" * 40,
            source_dirty=False,
            source_tree_hash=HASH_A,
            dependency_lock_hash=HASH_B,
            runtime_fingerprint=HASH_C,
            project_fingerprint=HASH_D,
            planner_fingerprint="e" * 64,
            resolved_prompt_fingerprint="f" * 64,
        )
    manifest = resolved_manifest(
        profile,
        source_revision="unavailable",
        source_dirty=False,
        source_tree_hash=HASH_A,
        dependency_lock_hash=HASH_B,
        runtime_fingerprint=HASH_C,
        project_fingerprint=HASH_D,
        planner_fingerprint="e" * 64,
    ).model_dump(mode="json")
    snapshot_template = _snapshotted_manifest()
    manifest["initial_memory_snapshots"] = [
        item.model_dump(mode="json") for item in snapshot_template.initial_memory_snapshots
    ]
    manifest["frozen_memory_snapshots"] = [
        item.model_dump(mode="json") for item in snapshot_template.frozen_memory_snapshots
    ]
    manifest["evidence_status"] = "evidence_collected"
    manifest["manifest_hash"] = manifest_hash(manifest)
    with pytest.raises(ValidationError, match="git revision"):
        Stage0Manifest.model_validate(manifest)


def test_unpinned_profile_can_be_resolved_but_evidence_requires_snapshots() -> None:
    profile = _profile()
    manifest = resolved_manifest(
        profile,
        source_revision="f" * 40,
        source_dirty=False,
        source_tree_hash=HASH_A,
        dependency_lock_hash=HASH_B,
        runtime_fingerprint=HASH_C,
        project_fingerprint=HASH_D,
        planner_fingerprint="e" * 64,
        resolved_prompt_fingerprint=HASH_A,
        resolved_settings_fingerprint=HASH_B,
        resolved_endpoint_fingerprint=HASH_C,
        created_at=NOW,
    )
    payload = manifest.model_dump(mode="json")
    payload["evidence_status"] = "evidence_collected"
    with pytest.raises(ValidationError, match="complete initial and frozen snapshots"):
        _seal(payload)

    snapshot_template = _snapshotted_manifest()
    payload["initial_memory_snapshots"] = [
        item.model_dump(mode="json") for item in snapshot_template.initial_memory_snapshots
    ]
    payload["frozen_memory_snapshots"] = [
        item.model_dump(mode="json") for item in snapshot_template.frozen_memory_snapshots
    ]
    resolved = _seal(payload)
    assert resolved.evidence_status == "evidence_collected"


def test_profile_rejects_seed_overlap_and_any_other_condition_modes() -> None:
    payload = _profile().model_dump(mode="json")
    payload["evaluation_seeds"] = [1, 4]
    with pytest.raises(ValidationError, match="disjoint"):
        Stage0Profile.model_validate(payload)

    payload = _profile().model_dump(mode="json")
    payload["memory_conditions"][0]["training_mode"] = "carry"
    with pytest.raises(ValidationError, match="exact B0"):
        Stage0Profile.model_validate(payload)


def test_b0_rejects_memory_metadata_and_evidence_requires_no_memory_audit() -> None:
    manifest = _snapshotted_manifest()
    block = manifest.run_matrix[0]
    with pytest.raises(ValidationError, match="B0 no-memory"):
        AttemptRecord.model_validate(
            _attempt(manifest, block.block_id, "B0").model_dump() | {"memory_namespace": "leak"}
        )
    attempts = _complete_attempts(manifest)
    attempts[0] = AttemptRecord.model_validate(
        attempts[0].model_dump() | {"no_memory_audit_hash": None}
    )
    report = aggregate_report(manifest, attempts, generated_at=NOW)
    assert "b0_terminal_attempt_missing_no_memory_audit" in report.evidence_incompleteness_reasons


def test_b1_training_rejects_frozen_evaluation_metadata() -> None:
    manifest = _snapshotted_manifest()
    block = next(item for item in manifest.run_matrix if item.phase == "training")
    attempt = _attempt(manifest, block.block_id, "B1")
    with pytest.raises(ValidationError, match="must not carry frozen evaluation metadata"):
        AttemptRecord.model_validate(
            attempt.model_dump(mode="json")
            | {
                "memory_snapshot_id": "frozen-r0",
                "memory_snapshot_hash": HASH_B,
                "quarantine_namespace": "training-overlay",
                "quarantine_provenance_ref": "training-overlay-record",
                "quarantine_audit_hash": HASH_C,
            }
        )


def test_aggregation_revalidates_models_copied_without_validation() -> None:
    manifest = _snapshotted_manifest()
    invalid_manifest = manifest.model_copy(update={"source_dirty": None})
    with pytest.raises(ValidationError, match="resolved_config_fingerprint|source_dirty"):
        aggregate_report(invalid_manifest, _complete_attempts(manifest), generated_at=NOW)

    attempts = _complete_attempts(manifest)
    invalid_attempt = attempts[0].model_copy(update={"memory_namespace": "b0-leak"})
    with pytest.raises(ValidationError, match="B0 no-memory"):
        aggregate_report(manifest, [invalid_attempt, *attempts[1:]], generated_at=NOW)


def test_complete_evidence_requires_one_namespace_and_one_frozen_input() -> None:
    manifest = _snapshotted_manifest()
    report = aggregate_report(manifest, _complete_attempts(manifest), generated_at=NOW)
    assert report.evidence_complete
    assert report.manifest_hash == manifest.manifest_hash
    payload = manifest.model_dump(mode="json")
    payload["frozen_memory_snapshots"][1]["memory_namespace"] = "other"
    payload["manifest_hash"] = manifest_hash(payload)
    with pytest.raises(ValidationError, match="same-replicate final training ancestry"):
        Stage0Manifest.model_validate(payload)


def test_terminal_attempts_require_result_and_raw_capture_declarations() -> None:
    manifest = _snapshotted_manifest()
    attempts = _complete_attempts(manifest)

    with pytest.raises(ValidationError, match="trace_ref requires"):
        AttemptRecord.model_validate(
            attempts[0].model_dump(mode="json") | {"raw_content_capture": None}
        )
    with pytest.raises(ValidationError, match="bind this manifest and attempt"):
        AttemptRecord.model_validate(
            attempts[1].model_dump(mode="json")
            | {"raw_content_capture": attempts[0].raw_content_capture.model_dump(mode="json")}
        )
    with pytest.raises(ValidationError, match="bind this manifest and attempt"):
        AttemptRecord.model_validate(
            attempts[0].model_dump(mode="json")
            | {
                "raw_content_capture": attempts[0]
                .raw_content_capture.model_copy(update={"manifest_id": "wrong-manifest"})
                .model_dump(mode="json")
            }
        )

    attempts[0] = AttemptRecord.model_validate(
        attempts[0].model_dump(mode="json") | {"raw_content_capture": None, "trace_ref": None}
    )
    report = aggregate_report(manifest, attempts, generated_at=NOW)
    assert not report.evidence_complete
    assert "terminal_attempt_missing_raw_capture_audit" in report.evidence_incompleteness_reasons

    attempts = _complete_attempts(manifest)
    attempts[0] = AttemptRecord.model_validate(
        attempts[0].model_dump(mode="json") | {"result_ref": None, "result_hash": None}
    )
    report = aggregate_report(manifest, attempts, generated_at=NOW)
    assert not report.evidence_complete
    assert "terminal_attempt_missing_result_evidence" in report.evidence_incompleteness_reasons


def test_raw_capture_policy_and_states_fail_closed() -> None:
    manifest = _snapshotted_manifest()
    attempts = _complete_attempts(manifest)
    capture = attempts[0].raw_content_capture
    assert capture is not None

    wrong_policy = capture.model_copy(update={"policy_fingerprint": HASH_B})
    attempts[0] = AttemptRecord.model_validate(
        attempts[0].model_dump(mode="json")
        | {"raw_content_capture": wrong_policy.model_dump(mode="json")}
    )
    report = aggregate_report(manifest, attempts, generated_at=NOW)
    assert "terminal_attempt_raw_capture_policy_mismatch" in report.evidence_incompleteness_reasons

    attempts = _complete_attempts(manifest)
    capture = attempts[0].raw_content_capture
    assert capture is not None
    not_emitted = CaptureState(state="not_emitted", absence_reason="before_prompt")
    attempts[0] = AttemptRecord.model_validate(
        attempts[0].model_dump(mode="json")
        | {
            "raw_content_capture": capture.model_copy(update={"prompts": not_emitted}).model_dump(
                mode="json"
            )
        }
    )
    report = aggregate_report(manifest, attempts, generated_at=NOW)
    assert "completed_attempt_missing_enabled_raw_capture" in report.evidence_incompleteness_reasons

    attempts = _complete_attempts(manifest)
    capture = attempts[0].raw_content_capture
    assert capture is not None
    quarantined = CaptureState(
        state="quarantined",
        redaction_audit_hash=HASH_D,
        absence_reason="secret_handling_failed",
    )
    attempts[0] = AttemptRecord.model_validate(
        attempts[0].model_dump(mode="json")
        | {
            "raw_content_capture": capture.model_copy(update={"prompts": quarantined}).model_dump(
                mode="json"
            )
        }
    )
    report = aggregate_report(manifest, attempts, generated_at=NOW)
    assert "terminal_attempt_raw_content_quarantined" in report.evidence_incompleteness_reasons

    disabled_manifest = _snapshotted_manifest(raw=(False, True, True))
    attempts = _complete_attempts(disabled_manifest)
    capture = attempts[0].raw_content_capture
    assert capture is not None
    captured = CaptureState(
        state="captured",
        artifact_ref=f"sha256:{HASH_B}",
        content_hash=HASH_B,
        redaction_audit_hash=HASH_D,
    )
    attempts[0] = AttemptRecord.model_validate(
        attempts[0].model_dump(mode="json")
        | {
            "raw_content_capture": capture.model_copy(update={"prompts": captured}).model_dump(
                mode="json"
            )
        }
    )
    report = aggregate_report(disabled_manifest, attempts, generated_at=NOW)
    assert "disabled_raw_content_class_not_disabled" in report.evidence_incompleteness_reasons

    manifest = _snapshotted_manifest()
    attempts = _complete_attempts(manifest)
    first_capture = attempts[0].raw_content_capture
    second_capture = attempts[1].raw_content_capture
    assert first_capture is not None and second_capture is not None
    attempts[1] = AttemptRecord.model_validate(
        attempts[1].model_dump(mode="json")
        | {
            "raw_content_capture": second_capture.model_copy(
                update={
                    "audit_ref": first_capture.audit_ref,
                    "audit_hash": first_capture.audit_hash,
                }
            ).model_dump(mode="json")
        }
    )
    report = aggregate_report(manifest, attempts, generated_at=NOW)
    assert "terminal_raw_capture_audit_reused" in report.evidence_incompleteness_reasons


def test_terminal_retry_capture_is_checked_even_when_retry_is_selected() -> None:
    manifest = _snapshotted_manifest()
    attempts = _complete_attempts(manifest)
    original = attempts.pop(0)
    failed = AttemptRecord.model_validate(
        original.model_dump(mode="json")
        | {
            "status": "failed",
            "failure_class": "transient",
            "metrics": {},
            "raw_content_capture": None,
            "trace_ref": None,
        }
    )
    retry = _attempt(
        manifest,
        original.block_id,
        "B0",
        index=1,
        retry_of=failed.attempt_id,
    )
    report = aggregate_report(manifest, [failed, retry, *attempts], generated_at=NOW)
    assert report.selected_terminal_attempt_ids[f"{original.block_id}::B0"] == retry.attempt_id
    assert not report.evidence_complete
    assert "terminal_attempt_missing_raw_capture_audit" in report.evidence_incompleteness_reasons


def test_frozen_input_requires_quarantine_and_exact_attempt_linkage() -> None:
    payload = _snapshotted_manifest().model_dump(mode="json")
    payload["frozen_memory_snapshots"][0]["quarantine_namespace"] = "b1-r0"
    payload["manifest_hash"] = manifest_hash(payload)
    with pytest.raises(ValidationError, match="quarantine namespace"):
        Stage0Manifest.model_validate(payload)
    manifest = _snapshotted_manifest()
    attempts = _complete_attempts(manifest)
    index = next(
        i
        for i, item in enumerate(attempts)
        if item.phase == "evaluation" and item.condition == "B1"
    )
    attempts[index] = AttemptRecord.model_validate(
        attempts[index].model_dump() | {"quarantine_audit_hash": HASH_D}
    )
    report = aggregate_report(manifest, attempts, generated_at=NOW)
    assert "missing_or_invalid_frozen_memory_snapshots" in report.evidence_incompleteness_reasons


def test_retry_can_follow_only_retry_eligible_failure_and_latest_running_is_incomplete() -> None:
    manifest = _snapshotted_manifest()
    block = manifest.run_matrix[0]
    completed = _attempt(manifest, block.block_id, "B0")
    retry = _attempt(manifest, block.block_id, "B0", index=1, retry_of=completed.attempt_id)
    with pytest.raises(ValueError, match="non-retry-eligible"):
        aggregate_report(manifest, [completed, retry], generated_at=NOW)
    failed = _attempt(manifest, block.block_id, "B0", status="failed", failure_class="transient")
    running = _attempt(
        manifest, block.block_id, "B0", index=1, status="running", retry_of=failed.attempt_id
    )
    report = aggregate_report(manifest, [failed, running], generated_at=NOW)
    assert report.selected_terminal_attempt_ids == {}


def test_retry_count_and_attempt_ids_are_bounded_and_global() -> None:
    manifest = _snapshotted_manifest()
    block = manifest.run_matrix[0]
    attempts: list[AttemptRecord] = []
    previous_id: str | None = None
    for index in range(4):
        attempt = _attempt(
            manifest,
            block.block_id,
            "B0",
            index=index,
            status="failed",
            retry_of=previous_id,
            failure_class="transient",
        )
        attempts.append(attempt)
        previous_id = attempt.attempt_id
    with pytest.raises(ValueError, match="max_attempts_per_cell"):
        aggregate_report(manifest, attempts, generated_at=NOW)

    duplicate = _attempt(manifest, block.block_id, "B1")
    assert duplicate.raw_content_capture is not None
    duplicate = duplicate.model_copy(
        update={
            "attempt_id": attempts[0].attempt_id,
            "raw_content_capture": duplicate.raw_content_capture.model_copy(
                update={"attempt_id": attempts[0].attempt_id}
            ),
        }
    )
    with pytest.raises(ValueError, match="globally unique"):
        aggregate_report(manifest, [attempts[0], duplicate], generated_at=NOW)


def test_completed_attempts_require_closed_non_null_task_metrics() -> None:
    manifest = _manifest()
    block = manifest.run_matrix[0]
    with pytest.raises(ValidationError, match="all non-null canonical"):
        AttemptRecord(
            manifest_id=manifest.manifest_id,
            attempt_id="bad",
            block_id=block.block_id,
            phase=block.phase,
            condition="B0",
            world_seed=block.world_seed,
            replicate_index=0,
            status="completed",
            metrics={"final_balance_minor": None},
        )
    with pytest.raises(ValidationError, match="completion_status"):
        AttemptRecord(
            manifest_id=manifest.manifest_id,
            attempt_id="bad-status",
            block_id=block.block_id,
            phase=block.phase,
            condition="B0",
            world_seed=block.world_seed,
            replicate_index=0,
            status="completed",
            metrics=_metrics() | {"completion_status": "failed"},
        )


def test_report_hashes_and_internal_counts_cannot_be_lied_about() -> None:
    manifest = _snapshotted_manifest()
    report = aggregate_report(manifest, _complete_attempts(manifest), generated_at=NOW)
    payload = report.model_dump(mode="json")
    payload["total_attempts"] = 999
    with pytest.raises(ValidationError, match="total_attempts"):
        type(report).model_validate(payload)
    payload = report.model_dump(mode="json")
    first_cell = next(iter(payload["selected_terminal_attempt_ids"]))
    payload["selected_terminal_attempt_ids"][first_cell] = "invented-attempt"
    tampered = type(report).model_validate(payload)
    with pytest.raises(ValueError, match="recomputed evidence"):
        verify_report(manifest, tampered)

    verify_report(manifest, report)
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json({"value": math.nan})
    with pytest.raises(ValueError, match="sets are not valid"):
        canonical_json({"value": {"unordered", "set"}})


def test_secret_variants_are_redacted_and_paired_delta_is_b1_minus_b0() -> None:
    secret_text = (
        "token=topsecret Authorization Token second "
        "Authorization: Basic dXNlcjpwYXNz sk-abcdefghijk"
    )
    redacted = redact_text(secret_text)
    assert "topsecret" not in redacted
    assert "second" not in redacted
    assert "dXNlcjpwYXNz" not in redacted
    assert "sk-abcdefghijk" not in redacted
    assert "nested-secret" not in canonical_json({"token": ["nested-secret"]})

    manifest = _snapshotted_manifest()
    attempts = _complete_attempts(manifest)
    target_index = next(
        index
        for index, attempt in enumerate(attempts)
        if attempt.phase == "evaluation" and attempt.condition == "B1"
    )
    target = attempts[target_index]
    attempts[target_index] = AttemptRecord.model_validate(
        target.model_dump(mode="json") | {"metrics": target.metrics | {"final_balance_minor": 15}}
    )
    report = aggregate_report(manifest, attempts, generated_at=NOW)
    evaluation = next(item for item in report.paired_reports if item.phase == "evaluation")
    assert evaluation.deltas[0].deltas["final_balance_minor"] == 5


def test_tree_hash_excludes_secret_files_and_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "kept.py").write_text("x = 1\n", encoding="utf-8")
    (source / ".env").write_text("TOKEN=first\n", encoding="utf-8")
    (source / "artifacts").mkdir()
    (source / "artifacts" / "result.json").write_text("first", encoding="utf-8")
    before = sha256_tree(source)
    (source / ".env").write_text("TOKEN=second\n", encoding="utf-8")
    (source / "artifacts" / "result.json").write_text("second", encoding="utf-8")
    assert sha256_tree(source) == before


def test_cli_refuses_outputs_outside_stage0_artifacts(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "scripts/stage0.py", "plan", "--output", str(tmp_path / "manifest.json")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "output must be inside" in result.stderr
