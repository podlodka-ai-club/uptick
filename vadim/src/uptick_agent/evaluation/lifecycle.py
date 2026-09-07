"""Append-only lifecycle journal for evaluation attempts."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field, model_validator

from uptick_agent.evaluation.artifacts import (
    EvaluationArtifactStore,
    InMemoryEvaluationArtifactStore,
)
from uptick_agent.evaluation.contracts import V2AttemptRecord, V2Manifest, sha256_json


class LifecycleEvent(BaseModel):
    """One immutable journal event containing a complete attempt snapshot."""

    model_config = {"extra": "forbid", "validate_default": True}

    sequence: int = Field(ge=0)
    recorded_at: datetime
    attempt: V2AttemptRecord
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _hash_matches(self) -> LifecycleEvent:
        payload = self.model_dump(mode="json")
        payload.pop("event_hash", None)
        if self.event_hash != sha256_json(payload):
            raise ValueError("lifecycle event hash does not match content")
        return self


class EvaluationJournal:
    """Append-only attempt journal with explicit lifecycle transition checks."""

    def __init__(
        self,
        manifest: V2Manifest,
        *,
        artifacts: EvaluationArtifactStore | None = None,
    ) -> None:
        self.manifest = V2Manifest.model_validate(manifest.model_dump(mode="json"))
        self.artifacts = artifacts or InMemoryEvaluationArtifactStore()
        self.artifacts.write_manifest(self.manifest)
        if getattr(self.artifacts, "has_lifecycle", lambda: False)():
            raise ValueError(
                "evaluation artifact directory already contains a lifecycle journal; "
                "resume/replay is not implemented"
            )
        self._events: list[LifecycleEvent] = []
        self._last_status: dict[str, str] = {}
        self._identity: dict[str, tuple[object, ...]] = {}

    @property
    def events(self) -> tuple[LifecycleEvent, ...]:
        return tuple(
            LifecycleEvent.model_validate(event.model_dump(mode="json")) for event in self._events
        )

    def append(self, attempt: V2AttemptRecord) -> LifecycleEvent:
        owned = V2AttemptRecord.model_validate(attempt.model_dump(mode="json"))
        if owned.manifest_id != self.manifest.manifest_id:
            raise ValueError("attempt does not belong to the sealed manifest")
        previous = self._last_status.get(owned.attempt_id)
        if previous is None and owned.status != "requested":
            raise ValueError("an attempt must start with a requested event")
        if previous is not None and not _allowed_transition(previous, owned.status):
            raise ValueError(f"invalid lifecycle transition {previous!r} -> {owned.status!r}")
        identity = (
            owned.manifest_id,
            owned.logical_run_id,
            owned.block_id,
            owned.phase,
            owned.condition_id,
            owned.environment_id,
            owned.scenario_id,
            owned.world_seed,
            owned.replicate_index,
            owned.attempt_index,
            owned.retry_of,
        )
        if previous is None:
            self._identity[owned.attempt_id] = identity
        elif self._identity[owned.attempt_id] != identity:
            raise ValueError("attempt identity changed across lifecycle events")
        payload = {
            "sequence": len(self._events),
            "recorded_at": datetime.now(UTC),
            "attempt": owned.model_dump(mode="json"),
        }
        event = LifecycleEvent(**payload, event_hash=sha256_json(payload))
        self._events.append(event)
        self._last_status[owned.attempt_id] = owned.status
        self.artifacts.append_lifecycle(event)
        return event

    def reduce_attempts(self) -> tuple[V2AttemptRecord, ...]:
        """Reduce immutable history to the latest snapshot per physical attempt."""

        latest: dict[str, V2AttemptRecord] = {}
        for event in self._events:
            latest[event.attempt.attempt_id] = event.attempt
        return tuple(latest.values())


def _allowed_transition(previous: str, current: str) -> bool:
    if previous in {"completed", "failed", "interrupted", "excluded"}:
        return False
    if previous == "requested":
        return current in {"running", "completed", "failed", "interrupted", "excluded"}
    return current in {"completed", "failed", "interrupted", "excluded"}
