"""A real local-filesystem benchmark for newline-record deduplication."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from uptick_agent._model_base import StrictModel
from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.memory.contracts import ObjectiveMetric
from uptick_agent.runs.runtime_results import RuntimeRunResult

ENVIRONMENT_ID = "file-records-dedupe-v1"
RECORDS_FILENAME = "records.txt"
SCENARIO_ID_DEFAULT = "records-default-v1"
_ADAPTER_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

_BRIEFING = (
    "The workspace contains records.txt with newline-delimited byte records. "
    "Inspect reads the file, deduplicate removes later duplicate records while "
    "preserving first-occurrence order, and noop leaves the file unchanged."
)
_OBJECTIVE = "Minimize duplicate newline records in records.txt using the public actions."
_ALGORITHM_SPEC: Mapping[str, object] = {
    "encoding": "opaque bytes",
    "record_separator": "ASCII LF (0x0a)",
    "trailing_separator": "preserve one trailing LF when present",
    "deduplication": "stable first occurrence by exact record bytes",
    "writes": "replace records.txt atomically inside the supplied workspace",
    "objective_metric": "duplicate_lines",
    "objective_direction": "minimize",
    "public_result_format": "newline_records",
}


class InspectRecords(StrictModel):
    kind: Literal["inspect"] = "inspect"


class DeduplicateRecords(StrictModel):
    kind: Literal["deduplicate"] = "deduplicate"


class NoopRecords(StrictModel):
    kind: Literal["noop"] = "noop"


class FileRecordsToolResult(ToolResult):
    """Public result subtype exposing the stable task format to memory queries."""

    format: Literal["newline_records"] = "newline_records"


RecordsAction = Annotated[
    InspectRecords | DeduplicateRecords | NoopRecords,
    Field(discriminator="kind"),
]


class FileRecordsDecision(StrictModel):
    """Environment-owned decision envelope for the filesystem task."""

    current_situation: str = Field(default="", max_length=2_000)
    hypothesis: str = Field(default="", max_length=2_000)
    remaining_steps: list[str] = Field(default_factory=list, max_length=4)
    action: RecordsAction


@dataclass(slots=True)
class FileRecordsSession:
    run_id: str
    seed: int
    agent_id: str
    agent_version: str
    scenario_id: str
    scenario_content_hash: str
    workspace: Path
    last_changed: bool = False
    environment_id: str = ENVIRONMENT_ID


def _canonical_hash(payload: object) -> str:
    rendered = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def scenario_content_hash(scenario_id: str, initial_bytes: bytes) -> str:
    """Return a deterministic identity for a scenario's exact initial bytes."""

    if not isinstance(scenario_id, str) or not scenario_id:
        raise ValueError("scenario_id must be a non-empty string")
    if not isinstance(initial_bytes, bytes):
        raise TypeError("initial_bytes must be bytes")
    return _canonical_hash(
        {
            "identity_version": "file-records-scenario-1",
            "scenario_id": scenario_id,
            "records_bytes_base64": base64.b64encode(initial_bytes).decode("ascii"),
        }
    )


def environment_content_hash() -> str:
    """Return the immutable task, action-schema, and algorithm identity."""

    spec = EnvironmentDecisionSpec(
        response_model=FileRecordsDecision,
        environment_briefing=_BRIEFING,
        objective=_OBJECTIVE,
    )
    return _canonical_hash(
        {
            "identity_version": "file-records-environment-1",
            "adapter_source_sha256": _ADAPTER_SOURCE_SHA256,
            "environment_id": ENVIRONMENT_ID,
            "records_filename": RECORDS_FILENAME,
            "decision_spec": spec.public_input(),
            "algorithm": dict(_ALGORITHM_SPEC),
        }
    )


def _records(raw: bytes) -> tuple[list[bytes], bool]:
    trailing_newline = raw.endswith(b"\n")
    if not raw:
        return [], False
    parts = raw.split(b"\n")
    if trailing_newline:
        parts.pop()
    return parts, trailing_newline


def _render(records: list[bytes], trailing_newline: bool) -> bytes:
    rendered = b"\n".join(records)
    if trailing_newline and records:
        rendered += b"\n"
    return rendered


def _snapshot(raw: bytes, *, changed: bool) -> dict[str, object]:
    records, trailing_newline = _records(raw)
    unique_count = len(set(records))
    return {
        "file_name": RECORDS_FILENAME,
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "line_count": len(records),
        "unique_lines": unique_count,
        "duplicate_lines": len(records) - unique_count,
        "trailing_newline": trailing_newline,
        "changed": changed,
    }


def _metrics(snapshot: Mapping[str, object]) -> list[ObjectiveMetric]:
    return [
        ObjectiveMetric(
            name="duplicate_lines",
            value=float(snapshot["duplicate_lines"]),
            unit="lines",
        ),
        ObjectiveMetric(name="line_count", value=float(snapshot["line_count"]), unit="lines"),
        ObjectiveMetric(
            name="unique_lines",
            value=float(snapshot["unique_lines"]),
            unit="lines",
        ),
    ]


class FileRecordsEnvironment:
    """Environment whose only world effect is an atomic rewrite of records.txt."""

    def __init__(
        self,
        workspace: Path,
        *,
        scenario_id: str = SCENARIO_ID_DEFAULT,
        run_id_suffix: str = "",
    ) -> None:
        if not isinstance(workspace, Path):
            workspace = Path(workspace)
        if not workspace.is_absolute():
            raise ValueError("workspace must be an absolute path")
        if workspace.is_symlink():
            raise ValueError("workspace must not be a symlink")
        try:
            resolved = workspace.resolve(strict=True)
        except FileNotFoundError as error:
            raise ValueError("workspace must be an existing directory") from error
        if not resolved.is_dir():
            raise ValueError("workspace must be an existing directory")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ValueError("scenario_id must be a non-empty string")
        if len(scenario_id) > 128:
            raise ValueError("scenario_id is too long")
        if not isinstance(run_id_suffix, str) or len(run_id_suffix) > 128:
            raise ValueError("run_id_suffix must be a string of at most 128 characters")
        self._workspace = resolved
        self.scenario_id = scenario_id
        self._run_id_suffix = run_id_suffix

    @property
    def environment_content_hash(self) -> str:
        return environment_content_hash()

    @property
    def decision_spec(self) -> EnvironmentDecisionSpec:
        return EnvironmentDecisionSpec(
            response_model=FileRecordsDecision,
            environment_briefing=_BRIEFING,
            objective=_OBJECTIVE,
        )

    def public_state(self, session: FileRecordsSession) -> dict[str, object]:
        self._validate_session(session)
        raw = self._read_records()
        return self._public_snapshot(session, raw, changed=session.last_changed)

    async def start(
        self, *, seed: int, agent_id: str, agent_version: str
    ) -> tuple[FileRecordsSession, ToolResult]:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        if not isinstance(agent_id, str) or not isinstance(agent_version, str):
            raise TypeError("agent_id and agent_version must be strings")
        initial_bytes = self._read_records()
        scenario_hash = scenario_content_hash(self.scenario_id, initial_bytes)
        suffix = f":{self._run_id_suffix}" if self._run_id_suffix else ""
        session = FileRecordsSession(
            run_id=(f"{ENVIRONMENT_ID}:{self.scenario_id}:{seed}:{scenario_hash}{suffix}"),
            seed=seed,
            agent_id=agent_id,
            agent_version=agent_version,
            scenario_id=self.scenario_id,
            scenario_content_hash=scenario_hash,
            workspace=self._workspace,
        )
        return session, self._result(session, "start", initial_bytes, changed=False)

    async def execute(self, session: FileRecordsSession, action: object) -> ToolResult:
        self._validate_session(session)
        if isinstance(action, InspectRecords):
            raw = self._read_records()
            session.last_changed = False
            return self._result(session, action.kind, raw, changed=False)
        if isinstance(action, DeduplicateRecords):
            before = self._read_records()
            records, trailing_newline = _records(before)
            deduplicated = list(dict.fromkeys(records))
            after = _render(deduplicated, trailing_newline)
            changed = after != before
            if changed:
                self._atomic_write(after)
            # Read the result from disk so the response reflects the world,
            # rather than the bytes the adapter intended to write.
            observed = self._read_records()
            session.last_changed = changed
            return self._result(session, action.kind, observed, changed=changed)
        if isinstance(action, NoopRecords):
            raw = self._read_records()
            session.last_changed = False
            return self._result(session, action.kind, raw, changed=False)
        raise TypeError("file-records environment received an unsupported action")

    async def finish(
        self,
        session: FileRecordsSession,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ) -> RuntimeRunResult:
        self._validate_session(session)
        raw = self._read_records()
        snapshot = _snapshot(raw, changed=session.last_changed)
        status = "completed" if snapshot["duplicate_lines"] == 0 else "failed"
        return RuntimeRunResult(
            run_id=session.run_id,
            seed=session.seed,
            agent_id=session.agent_id,
            agent_version=session.agent_version,
            status=status,
            steps=steps,
            duration_seconds=max(0.0, duration_seconds),
            objective_metrics=_metrics(snapshot),
            stop_reason=stop_reason,
        )

    async def aclose(self) -> None:
        return None

    def _validate_session(self, session: object) -> None:
        if not isinstance(session, FileRecordsSession) or session.workspace != self._workspace:
            raise TypeError("file-records environment received another session")

    def _records_path(self) -> Path:
        path = self._workspace / RECORDS_FILENAME
        if path.is_symlink():
            raise ValueError("records.txt must not be a symlink")
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self._workspace)
        except (FileNotFoundError, ValueError) as error:
            raise ValueError("records.txt must be an existing file inside workspace") from error
        if not resolved.is_file():
            raise ValueError("records.txt must be an existing file inside workspace")
        return resolved

    def _read_records(self) -> bytes:
        return self._records_path().read_bytes()

    def _atomic_write(self, raw: bytes) -> None:
        target = self._records_path()
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._workspace,
                prefix=".records-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(raw)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, target)
        finally:
            if temporary_name is not None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary_name)

    def _public_snapshot(
        self, session: FileRecordsSession, raw: bytes, *, changed: bool
    ) -> dict[str, object]:
        return {
            "environment_id": ENVIRONMENT_ID,
            "environment_content_hash": self.environment_content_hash,
            "scenario_id": session.scenario_id,
            "scenario_content_hash": session.scenario_content_hash,
            **_snapshot(raw, changed=changed),
        }

    def _result(
        self,
        session: FileRecordsSession,
        action_kind: str,
        raw: bytes,
        *,
        changed: bool,
    ) -> ToolResult:
        snapshot = self._public_snapshot(session, raw, changed=changed)
        duplicate_lines = snapshot["duplicate_lines"]
        if action_kind == "start":
            summary = f"Started with {duplicate_lines} duplicate lines in records.txt."
        elif action_kind == "inspect":
            summary = f"Inspected records.txt: {duplicate_lines} duplicate lines."
        elif action_kind == "deduplicate":
            summary = f"Deduplicated records.txt; {duplicate_lines} duplicate lines remain."
        else:
            summary = f"Left records.txt unchanged; {duplicate_lines} duplicate lines remain."
        return FileRecordsToolResult(
            action_kind=action_kind,
            ok=True,
            summary=summary,
            data=snapshot,
            objective_metrics=_metrics(snapshot),
            terminal=action_kind in {"deduplicate", "noop"},
        )


__all__ = [
    "ENVIRONMENT_ID",
    "RECORDS_FILENAME",
    "SCENARIO_ID_DEFAULT",
    "DeduplicateRecords",
    "FileRecordsDecision",
    "FileRecordsEnvironment",
    "FileRecordsSession",
    "FileRecordsToolResult",
    "InspectRecords",
    "NoopRecords",
    "RecordsAction",
    "environment_content_hash",
    "scenario_content_hash",
]
