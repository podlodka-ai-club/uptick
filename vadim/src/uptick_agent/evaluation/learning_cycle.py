"""Neutral contracts and accounting for the controlled learning cycle.

This module deliberately knows nothing about the fixture, memory providers,
SQLite, or an LLM. Composition code supplies those dependencies and records
the returned evidence here as plain JSON data.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class LearningCycleManifest:
    experiment_id: str
    model: str
    provider: str
    generation_settings: dict[str, object]
    prompt: str
    source_revision: str
    source_tree_hash: str
    dependency_lock_hash: str
    source_capsule_hash: str
    source_dirty: bool
    fixture_spec_hash: str
    adapter_hash: str
    training_seeds: tuple[int, ...]
    training_cases: tuple[dict[str, object], ...]
    evaluation_cases: tuple[dict[str, object], ...]
    conditions: tuple[str, ...]
    memory_configurations: dict[str, dict[str, object]]
    training_max_steps: int
    evaluation_max_steps: int
    training_timeout_seconds: float
    evaluation_timeout_seconds: float
    manifest_hash: str = ""

    def payload(self) -> dict[str, object]:
        return asdict(self)

    def seal(self) -> LearningCycleManifest:
        payload = self.payload()
        payload.pop("manifest_hash", None)
        return replace(self, manifest_hash=content_hash(payload))

    def verify(self) -> None:
        payload = self.payload()
        expected = payload.pop("manifest_hash", None)
        if expected != content_hash(payload):
            raise ValueError("learning-cycle manifest hash does not match content")


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    attempt_id: str
    phase: Literal["training", "evaluation"]
    condition_id: str
    seed: int
    variant: str
    status: Literal["started", "completed", "failed", "interrupted"]
    started_at: str
    finished_at: str | None = None
    run_id: str | None = None
    selected_actions: tuple[dict[str, object], ...] = ()
    recovered: bool | None = None
    outcome_status: str | None = None
    memory_item_ids: tuple[str, ...] = ()
    prompt_records: tuple[dict[str, object], ...] = ()
    step_records: tuple[dict[str, object], ...] = ()
    failure: str | None = None
    cleanup_errors: tuple[str, ...] = ()

    def payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LearningCycleReport:
    manifest_id: str
    manifest_hash: str
    expected_attempts: int
    attempts: tuple[AttemptEvidence, ...]
    frozen_bindings: dict[str, dict[str, object]]
    reopened_before_evaluation: bool
    report_hash: str = ""

    def seal(self) -> LearningCycleReport:
        payload = self.payload()
        payload.pop("report_hash", None)
        return replace(self, report_hash=content_hash(payload))

    def payload(self) -> dict[str, object]:
        return {
            "manifest_id": self.manifest_id,
            "manifest_hash": self.manifest_hash,
            "expected_attempts": self.expected_attempts,
            "attempts": [item.payload() for item in self.attempts],
            "frozen_bindings": self.frozen_bindings,
            "reopened_before_evaluation": self.reopened_before_evaluation,
            "report_hash": self.report_hash,
        }

    @property
    def completed(self) -> int:
        return sum(item.status == "completed" for item in self.attempts)

    @property
    def failed_or_interrupted(self) -> int:
        return sum(item.status in {"failed", "interrupted"} for item in self.attempts)


class LearningCycleJournal:
    """Durable started/final attempt markers with retained failure rows."""

    def __init__(self, root: Path, manifest: LearningCycleManifest) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = manifest
        self._attempts: dict[str, AttemptEvidence] = {}
        manifest_path = root / "manifest.json"
        attempts_path = root / "attempts.jsonl"
        if manifest_path.exists() or attempts_path.exists():
            raise ValueError("learning-cycle output already exists; reuse is refused")
        _atomic_json(manifest_path, manifest.payload())

    @property
    def attempts(self) -> tuple[AttemptEvidence, ...]:
        return tuple(self._attempts.values())

    def start(
        self,
        *,
        attempt_id: str,
        phase: Literal["training", "evaluation"],
        condition_id: str,
        seed: int,
        variant: str,
    ) -> AttemptEvidence:
        if attempt_id in self._attempts:
            raise ValueError(f"duplicate attempt {attempt_id}")
        row = AttemptEvidence(
            attempt_id=attempt_id,
            phase=phase,
            condition_id=condition_id,
            seed=seed,
            variant=variant,
            status="started",
            started_at=_utc_now(),
        )
        self._attempts[attempt_id] = row
        self._append(row)
        return row

    def finish(self, row: AttemptEvidence, **updates: object) -> AttemptEvidence:
        current = self._attempts.get(row.attempt_id)
        if current is None or current.status != "started":
            raise ValueError("attempt must have one started marker before finalization")
        finished = current.__class__(
            **{
                **current.payload(),
                **updates,
                "finished_at": updates.get("finished_at", _utc_now()),
            }
        )
        self._attempts[row.attempt_id] = finished
        self._append(finished)
        return finished

    def setup_failure(
        self,
        *,
        attempt_id: str,
        phase: Literal["training", "evaluation"],
        condition_id: str,
        seed: int,
        variant: str,
        failure: str,
    ) -> AttemptEvidence:
        row = self.start(
            attempt_id=attempt_id,
            phase=phase,
            condition_id=condition_id,
            seed=seed,
            variant=variant,
        )
        return self.finish(row, status="failed", failure=failure)

    def report(
        self,
        *,
        frozen_bindings: Mapping[str, Mapping[str, object]],
        reopened_before_evaluation: bool,
    ) -> LearningCycleReport:
        report = LearningCycleReport(
            manifest_id=self.manifest.experiment_id,
            manifest_hash=self.manifest.manifest_hash,
            expected_attempts=len(self.manifest.training_seeds)
            + len(self.manifest.evaluation_cases) * len(self.manifest.conditions),
            attempts=self.attempts,
            frozen_bindings={key: dict(value) for key, value in frozen_bindings.items()},
            reopened_before_evaluation=reopened_before_evaluation,
        ).seal()
        _atomic_json(self.root / "report.json", report.payload())
        return report

    def record_prompt(self, attempt_id: str, record: Mapping[str, object]) -> None:
        """Persist a request before its provider await begins."""

        path = self.root / "raw-requests.jsonl"
        payload = {"attempt_id": attempt_id, **dict(record)}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(_canonical(payload) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def write_binding(self, condition_id: str, binding: Mapping[str, object]) -> None:
        """Persist the frozen input before any evaluation request."""

        _atomic_json(self.root / f"frozen-binding-{condition_id}.json", binding)

    def _append(self, row: AttemptEvidence) -> None:
        path = self.root / "attempts.jsonl"
        rendered = _canonical(row.payload()) + "\n"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


__all__ = [
    "AttemptEvidence",
    "LearningCycleJournal",
    "LearningCycleManifest",
    "LearningCycleReport",
    "content_hash",
]
