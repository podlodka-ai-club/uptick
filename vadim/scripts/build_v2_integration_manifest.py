"""Build a sealed, offline-only v2 integration manifest.

This command declares an evaluation matrix.  It does not construct a runner,
contact a simulator or provider, or request an evaluation attempt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from uptick_agent.evaluation import (
    V2Budget,
    V2Condition,
    V2EnvironmentPin,
    V2EvaluationProfile,
    V2FailurePolicy,
    V2Manifest,
    V2PlannedContrast,
    V2ProviderPin,
    V2SourcePin,
    resolved_manifest,
)
from uptick_agent.evaluation_presets import all_experimental_presets
from uptick_agent.memory.config import AuditConfiguration
from uptick_agent.simulator.briefings import V2_SYSTEM_PROMPT
from uptick_agent.simulator.v2_policy import SimulatorV2TimeBudgetPolicy
from uptick_agent.stage0 import sha256_file, sha256_json, sha256_tree

_DEFAULT_SIMULATOR_URL = "http://81.176.229.58:8080"
_API_CONTRACT_FINGERPRINT = "452b622ebf8e1734cfd630ff2dfe4cb1c25350f0e9b67d5ff5cf3e64e9cd1dc0"
_DEFAULT_PROFILE_ID = "memory-v2-integration-20260905-matrix"
_DEFAULT_TRAINING_SEEDS = (51, 52)
_DEFAULT_EVALUATION_SEEDS = (53,)
_DEFAULT_REPLICATES = (0,)


def _git(root: Path, *arguments: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *arguments],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"cannot read source revision from {root}: {exc}") from exc


def _conditions(
    *, audit: AuditConfiguration, smoke: bool
) -> tuple[tuple[V2Condition, ...], tuple[V2PlannedContrast, ...]]:
    presets = [preset for preset in all_experimental_presets() if preset.supported]
    if smoke:
        presets = [preset for preset in presets if preset.condition_id in {"A0", "A3"}]

    conditions_list: list[V2Condition] = []
    for preset in presets:
        configuration = preset.configuration.model_copy(update={"audit": audit}, deep=True)
        conditions_list.append(
            V2Condition(
                condition_id=preset.condition_id,
                memory_configuration=configuration,
                memory_configuration_fingerprint=configuration.fingerprint,
            )
        )
    conditions = tuple(conditions_list)
    ids = {condition.condition_id for condition in conditions}
    pairs = [
        ("A0", "A1"),
        ("A0", "A2"),
        ("A2", "A3"),
        ("A3", "A4"),
        ("A4", "A5"),
        ("A5", "A6"),
        ("A6", "A7"),
        ("A7", "A8"),
        ("A8", "A9"),
    ]
    pairs.extend(
        (preset.ablation_of, preset.condition_id)
        for preset in all_experimental_presets()
        if preset.supported and preset.ablation_of
    )
    if smoke:
        pairs = [("A0", "A3")]
    contrasts = tuple(
        V2PlannedContrast(baseline_condition_id=baseline, candidate_condition_id=candidate)
        for baseline, candidate in pairs
        if baseline in ids and candidate in ids
    )
    return conditions, contrasts


def build_manifest(
    source_root: Path,
    *,
    profile_id: str = _DEFAULT_PROFILE_ID,
    simulator_url: str = _DEFAULT_SIMULATOR_URL,
    training_seeds: tuple[int, ...] = _DEFAULT_TRAINING_SEEDS,
    evaluation_seeds: tuple[int, ...] = _DEFAULT_EVALUATION_SEEDS,
    replicate_indices: tuple[int, ...] = _DEFAULT_REPLICATES,
    max_steps: int = 8,
    max_wall_seconds: float = 120.0,
    max_context_items: int = 128,
    max_context_tokens: int = 16_000,
    smoke: bool = False,
) -> V2Manifest:
    """Return a sealed declaration without performing any evaluation work."""

    root = source_root.resolve()
    source_dir = root / "src"
    if not source_dir.is_dir():
        raise ValueError(f"source root must contain src/: {root}")
    pyproject = root / "pyproject.toml"
    lockfile = root / "uv.lock"
    if not pyproject.is_file() or not lockfile.is_file():
        raise ValueError("source root must contain pyproject.toml and uv.lock")

    audit = AuditConfiguration.simulator_default()
    conditions, contrasts = _conditions(audit=audit, smoke=smoke)
    source_hash = sha256_tree(source_dir)
    normalized_simulator_url = simulator_url.rstrip("/")
    settings = {"reasoning_effort": "low"}
    profile = V2EvaluationProfile(
        profile_id=profile_id,
        environment=V2EnvironmentPin(
            environment_id="uptick-simulator-v2",
            environment_version="public-api-0.5.0-world-unknown",
            adapter_id="uptick-v2",
            adapter_version="1.0",
            scenario_id="generated-public-world",
            api_contract_fingerprint=_API_CONTRACT_FINGERPRINT,
            endpoint_fingerprint=hashlib.sha256(normalized_simulator_url.encode()).hexdigest(),
            context_identity_verified=False,
        ),
        provider=V2ProviderPin(
            provider="codex",
            model="gpt-5.6-sol",
            settings=settings,
            prompt_fingerprint=hashlib.sha256(V2_SYSTEM_PROMPT.encode()).hexdigest(),
            settings_fingerprint=sha256_json(settings),
            token_estimator_id="utf8-byte-upper-bound",
            token_estimator_version="1.0",
            policy_id=SimulatorV2TimeBudgetPolicy.policy_id,
            policy_version=SimulatorV2TimeBudgetPolicy.policy_version,
        ),
        source=V2SourcePin(
            source_revision=_git(root, "rev-parse", "HEAD"),
            source_tree_hash=source_hash,
            dependency_lock_hash=sha256_file(lockfile),
            source_dirty=bool(
                _git(
                    root,
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--",
                    "src",
                    "pyproject.toml",
                    "uv.lock",
                )
            ),
            runtime_fingerprint=sha256_json(
                {"source_tree_hash": source_hash, "pyproject_hash": sha256_file(pyproject)}
            ),
        ),
        conditions=conditions,
        baseline_condition_id="A0",
        training_seeds=(43,) if smoke else training_seeds,
        evaluation_seeds=(44,) if smoke else evaluation_seeds,
        replicate_indices=replicate_indices,
        planned_contrasts=contrasts,
        budget=V2Budget(
            max_steps=2 if smoke else max_steps,
            max_wall_seconds=max_wall_seconds,
            max_context_items=max_context_items,
            max_context_tokens=max_context_tokens,
        ),
        failure_policy=V2FailurePolicy(max_attempts_per_cell=1),
        audit_configuration=audit,
        notes=(
            "Preregistered integration exercise, not a promotion or learning-utility experiment.",
            "Small fixed decision budget tests lifecycle/transport/memory composition; "
            "incomplete horizons are failures, never SLO successes.",
            "World content hashes and causal-family identity are unavailable; new derived "
            "knowledge cannot qualify for activation from these runs.",
            "Seed 42 was used for development. Matrix seeds are distinct from development "
            "and smoke seeds, but no causal-family holdout claim is made.",
            "All first attempts are retained, with no retries or pass-at-k selection.",
            "The unsafe minus-contradiction-tracking condition is unsupported; required "
            "activation validation remains enabled.",
        ),
    )
    return resolved_manifest(profile)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--simulator-url", default=_DEFAULT_SIMULATOR_URL)
    parser.add_argument("--profile-id", default=_DEFAULT_PROFILE_ID)
    parser.add_argument("--training-seeds", nargs="+", type=int, default=_DEFAULT_TRAINING_SEEDS)
    parser.add_argument(
        "--evaluation-seeds", nargs="+", type=int, default=_DEFAULT_EVALUATION_SEEDS
    )
    parser.add_argument("--replicate-indices", nargs="+", type=int, default=_DEFAULT_REPLICATES)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-wall-seconds", type=float, default=120.0)
    parser.add_argument("--max-context-items", type=int, default=128)
    parser.add_argument("--max-context-tokens", type=int, default=16_000)
    parser.add_argument("--smoke", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"refusing to overwrite existing manifest: {args.output}")
    try:
        manifest = build_manifest(
            args.source_root,
            profile_id=args.profile_id,
            simulator_url=args.simulator_url,
            training_seeds=tuple(args.training_seeds),
            evaluation_seeds=tuple(args.evaluation_seeds),
            replicate_indices=tuple(args.replicate_indices),
            max_steps=args.max_steps,
            max_wall_seconds=args.max_wall_seconds,
            max_context_items=args.max_context_items,
            max_context_tokens=args.max_context_tokens,
            smoke=args.smoke,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(manifest.model_dump_json(indent=2) + "\n")
    print(
        json.dumps(
            {
                "manifest_id": manifest.manifest_id,
                "manifest_hash": manifest.manifest_hash,
                "conditions": sorted(
                    condition.condition_id for condition in manifest.profile.conditions
                ),
                "cells": sum(len(block.conditions) for block in manifest.run_matrix),
                "output": str(args.output),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
