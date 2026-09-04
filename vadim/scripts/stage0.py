#!/usr/bin/env python3
"""Plan and report commands for the offline Stage 0 harness."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from uptick_agent.stage0 import (
    AttemptRecord,
    Stage0Manifest,
    Stage0Profile,
    aggregate_report,
    resolved_manifest,
    sha256_file,
    sha256_json,
    sha256_tree,
)


def _workspace() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_profile() -> Path:
    return _workspace() / "experiments" / "stage0" / "profile.json"


def _git_revision(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else "unavailable"


def _git_source_dirty(workspace: Path) -> bool | None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            ".",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _stage0_artifact_root(workspace: Path) -> Path:
    return (workspace / "artifacts" / "stage0").resolve()


def _write_json(
    path: Path,
    payload: object,
    *,
    workspace: Path,
    inputs: tuple[Path, ...] = (),
    force: bool = False,
) -> None:
    target = path.resolve()
    input_paths = {item.resolve() for item in inputs}
    artifact_root = _stage0_artifact_root(workspace)
    try:
        target.relative_to(artifact_root)
    except ValueError as error:
        raise ValueError(f"output must be inside {artifact_root}") from error
    if target in input_paths:
        raise ValueError("output path must differ from every input path")
    protected = (workspace / "src").resolve(), (workspace / "uv.lock").resolve()
    if target == protected[1] or target.is_relative_to(protected[0]):
        raise ValueError("refusing to overwrite workspace source or dependency lock")
    if target.exists() and not force:
        raise ValueError("output already exists; pass --force to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(
                json.dumps(payload, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
            )
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _plan(args: argparse.Namespace) -> int:
    workspace = _workspace()
    profile_path = args.profile.resolve()
    profile = Stage0Profile.model_validate_json(profile_path.read_text(encoding="utf-8"))
    lock_path = workspace / "uv.lock"
    manifest = resolved_manifest(
        profile,
        source_revision=_git_revision(workspace),
        source_dirty=_git_source_dirty(workspace),
        source_tree_hash=sha256_tree(workspace / "src"),
        dependency_lock_hash=sha256_file(lock_path),
        runtime_fingerprint=sha256_json(
            {
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "platform": platform.platform(),
            }
        ),
        project_fingerprint=sha256_tree(workspace),
        planner_fingerprint=sha256_file(Path(__file__).resolve()),
        resolved_prompt_fingerprint=profile.provider.prompt_hash,
        resolved_settings_fingerprint=profile.provider.settings_fingerprint,
        resolved_endpoint_fingerprint=profile.environment.endpoint_fingerprint,
        created_at=datetime.now(UTC),
    )
    _write_json(
        args.output,
        manifest.model_dump(mode="json"),
        workspace=workspace,
        inputs=(profile_path,),
        force=args.force,
    )
    return 0


def _load_attempts(path: Path) -> list[AttemptRecord]:
    attempts: list[AttemptRecord] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                attempts.append(AttemptRecord.model_validate_json(line))
            except ValueError as error:
                raise ValueError(f"invalid Stage 0 attempt at {path}:{line_number}") from error
    return attempts


def _report(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    attempts_path = args.attempts.resolve()
    manifest = Stage0Manifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    report = aggregate_report(
        manifest, _load_attempts(attempts_path), generated_at=datetime.now(UTC)
    )
    _write_json(
        args.output,
        report.model_dump(mode="json"),
        workspace=_workspace(),
        inputs=(manifest_path, attempts_path),
        force=args.force,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stage0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="resolve local fingerprints into a manifest")
    plan.add_argument("--profile", type=Path, default=_default_profile())
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--force", action="store_true", help="replace an existing Stage 0 artifact")
    plan.set_defaults(handler=_plan)

    report = subparsers.add_parser("report", help="aggregate retained local attempts")
    report.add_argument("--manifest", type=Path, required=True)
    report.add_argument("--attempts", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--force", action="store_true", help="replace an existing Stage 0 artifact")
    report.set_defaults(handler=_report)
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
