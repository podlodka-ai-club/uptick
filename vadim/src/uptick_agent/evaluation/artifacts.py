"""Immutable evaluation artifact storage and JSON evidence normalization."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from pydantic import BaseModel

from uptick_agent.evaluation.contracts import V2Manifest, sha256_json
from uptick_agent.redaction import sanitize_json

if TYPE_CHECKING:
    from uptick_agent.evaluation.lifecycle import LifecycleEvent

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")


def _json_value(value: object) -> object:
    """Return a redacted, finite JSON-compatible value for evidence storage."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif is_dataclass(value):
        value = asdict(value)
    return sanitize_json(value)


def _as_json_mapping(value: object) -> dict[str, object]:
    safe = _json_value(value)
    if not isinstance(safe, dict):
        raise TypeError("artifact value must be a JSON object")
    return safe


class EvaluationArtifactStore(Protocol):
    """Durable boundary for immutable evaluation artifacts."""

    def write_manifest(self, manifest: V2Manifest) -> str: ...

    def put(self, kind: str, artifact_id: str, value: object) -> str: ...

    def append_lifecycle(self, event: LifecycleEvent) -> None: ...


class InMemoryEvaluationArtifactStore:
    """Small deterministic store for unit tests and embedded callers."""

    def __init__(self) -> None:
        self.manifest: V2Manifest | None = None
        self.artifacts: dict[tuple[str, str], dict[str, object]] = {}
        self.lifecycle: list[LifecycleEvent] = []

    def write_manifest(self, manifest: V2Manifest) -> str:
        if self.manifest is not None and self.manifest.manifest_hash != manifest.manifest_hash:
            raise ValueError("evaluation manifest is immutable")
        self.manifest = V2Manifest.model_validate(manifest.model_dump(mode="json"))
        return manifest.manifest_hash

    def put(self, kind: str, artifact_id: str, value: object) -> str:
        _validate_artifact_key(kind, artifact_id)
        safe = _as_json_mapping(value)
        digest = sha256_json(safe)
        key = (kind, artifact_id)
        previous = self.artifacts.get(key)
        if previous is not None and previous["hash"] != digest:
            raise ValueError("evaluation artifact is immutable")
        self.artifacts[key] = {"hash": digest, "value": safe}
        return digest

    def append_lifecycle(self, event: LifecycleEvent) -> None:
        self.lifecycle.append(type(event).model_validate(event.model_dump(mode="json")))


class FilesystemEvaluationArtifactStore:
    """Filesystem-backed immutable manifest, artifact, and journal storage."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "artifacts").mkdir(exist_ok=True)

    def write_manifest(self, manifest: V2Manifest) -> str:
        payload = _as_json_mapping(manifest)
        path = self.root / "manifest.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if sha256_json(existing) != sha256_json(payload):
                raise ValueError("evaluation manifest is immutable")
            return manifest.manifest_hash
        _atomic_write(path, payload)
        return manifest.manifest_hash

    def put(self, kind: str, artifact_id: str, value: object) -> str:
        _validate_artifact_key(kind, artifact_id)
        payload = _as_json_mapping(value)
        digest = sha256_json(payload)
        directory = self.root / "artifacts" / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{sha256_json({'id': artifact_id})}.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("hash") != digest:
                raise ValueError("evaluation artifact is immutable")
            return digest
        _atomic_write(path, {"artifact_id": artifact_id, "hash": digest, "value": payload})
        return digest

    def append_lifecycle(self, event: LifecycleEvent) -> None:
        path = self.root / "lifecycle.jsonl"
        rendered = json.dumps(
            _as_json_mapping(event),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def has_lifecycle(self) -> bool:
        """Return whether this artifact directory already contains a journal."""

        path = self.root / "lifecycle.jsonl"
        return path.exists() and path.stat().st_size > 0


def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    rendered = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _validate_artifact_key(kind: str, artifact_id: str) -> None:
    if not _ID.fullmatch(kind) or not _ID.fullmatch(artifact_id):
        raise ValueError("artifact keys must be bounded identifiers")


def _stable_run_identifier(manifest_hash: str, *, block_id: str, condition_id: str) -> str:
    """Keep physical IDs bounded even when user-facing profile IDs are long."""

    digest = sha256_json(
        {"manifest_hash": manifest_hash, "block_id": block_id, "condition_id": condition_id}
    )
    return f"run:{manifest_hash[:16]}:{digest[:48]}"
