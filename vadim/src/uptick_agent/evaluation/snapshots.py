"""Read-only frozen memory views used by evaluation cells."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel

from uptick_agent.evaluation.contracts import V2SnapshotRef
from uptick_agent.memory.compatibility.contracts import MemoryEntry
from uptick_agent.memory.contracts import (
    DecisionMemoryContext,
    ExperienceTransition,
    MemoryContextRequest,
    RunOutcome,
)
from uptick_agent.memory.stores.contracts import StoredRecord, StructuredMemoryStore
from uptick_agent.ports import AgentMemory


class SnapshotReadStore:
    """Strict read-only view containing only verified frozen members."""

    def __init__(self, base: StructuredMemoryStore, refs: Sequence[V2SnapshotRef]) -> None:
        self._base = base
        self._refs = tuple(refs)
        self._records: dict[tuple[str, str], StoredRecord] = {}
        self._snapshots: dict[str, object] = {}
        self._members: dict[str, dict[str, str]] = {}
        self._loaded = False

    async def load(self) -> None:
        if self._loaded:
            return
        from uptick_agent.memory.stores.contracts import MemorySnapshot

        for ref in self._refs:
            snapshot = await self._base.get_snapshot(snapshot_id=ref.snapshot_id)
            if snapshot is None:
                raise ValueError("frozen memory snapshot is missing")
            snapshot = MemorySnapshot.validate_integrity(snapshot)
            if snapshot.namespace != ref.namespace or snapshot.content_hash != ref.content_hash:
                raise ValueError("frozen memory snapshot hash changed")
            self._snapshots[ref.snapshot_id] = snapshot.model_copy(deep=True)
            admitted = self._members.setdefault(ref.namespace, {})
            for member in snapshot.members:
                previous = admitted.get(member.record_id)
                if previous is not None and previous != member.content_hash:
                    raise ValueError("frozen snapshot namespaces disagree about a member")
                admitted[member.record_id] = member.content_hash
                record = await self._base.get(namespace=ref.namespace, record_id=member.record_id)
                if record is None:
                    raise ValueError("frozen memory snapshot member is missing")
                record = StoredRecord.validate_integrity(record)
                if record.content_hash != member.content_hash:
                    raise ValueError("frozen memory snapshot member hash changed")
                self._records[(ref.namespace, member.record_id)] = record.model_copy(deep=True)
        self._loaded = True

    async def append(self, *args: object, **kwargs: object) -> None:
        raise ValueError("evaluation read store is immutable")

    async def get(self, *, namespace: str, record_id: str) -> StoredRecord | None:
        self._require_loaded()
        record = self._records.get((namespace, record_id))
        return record.model_copy(deep=True) if record is not None else None

    async def list(self, *, namespace: str) -> list[StoredRecord]:
        self._require_loaded()
        records = [
            record.model_copy(deep=True)
            for (record_namespace, _), record in self._records.items()
            if record_namespace == namespace
        ]
        return sorted(records, key=lambda item: (item.created_at, item.record_id))

    async def create_snapshot(self, *args: object, **kwargs: object) -> None:
        raise ValueError("evaluation read store cannot create snapshots")

    async def get_snapshot(self, *, snapshot_id: str):
        self._require_loaded()
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is not None:
            return snapshot.model_copy(deep=True)
        from uptick_agent.memory.stores.contracts import MemorySnapshot

        nested = await self._base.get_snapshot(snapshot_id=snapshot_id)
        if nested is None:
            return None
        nested = MemorySnapshot.validate_integrity(nested)
        admitted = self._members.get(nested.namespace)
        if admitted is None:
            raise ValueError("nested snapshot namespace is outside the frozen input")
        if any(admitted.get(member.record_id) != member.content_hash for member in nested.members):
            raise ValueError("nested snapshot contains post-freeze or changed members")
        self._snapshots[snapshot_id] = nested.model_copy(deep=True)
        return nested.model_copy(deep=True)

    @property
    def member_count(self) -> int:
        return sum(len(members) for members in self._members.values())

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("snapshot store must be loaded before reads")


class EvaluationMemoryFacade:
    """Reads from frozen training memory and writes to an isolated runtime."""

    def __init__(
        self,
        read_runtime: AgentMemory,
        write_runtime: AgentMemory,
        *,
        frozen_snapshot_members: int | None = None,
    ) -> None:
        self._read = read_runtime
        self._write = write_runtime
        self.frozen_snapshot_members = frozen_snapshot_members

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext:
        return await self._read.build_context(request)

    async def remember(self, entry: MemoryEntry) -> None:
        await self._write.remember(entry)

    async def record_transition(self, transition: ExperienceTransition) -> None:
        await self._write.record_transition(transition)

    async def clear(self, run_id: str | None = None) -> None:
        await self._write.clear(run_id)

    async def finalize_run(self, outcome: RunOutcome) -> None:
        await self._write.finalize_run(outcome)

    async def record_trace(self, write: object) -> object:
        return await self._write.record_trace(write)  # type: ignore[arg-type]

    @property
    def context_diagnostics(self) -> dict[str, object]:
        return self._read.context_diagnostics

    @property
    def module_telemetry(self) -> dict[str, dict[str, object]] | None:
        """Merge observed module calls from the frozen reader and eval writer."""

        counters = (
            "construction_events",
            "read_events",
            "contribution_events",
            "write_events",
            "finalization_events",
            "consolidation_events",
        )
        merged: dict[str, dict[str, object]] = {}
        telemetry_sets = [
            getattr(runtime, "module_telemetry", None) for runtime in (self._read, self._write)
        ]
        if not all(isinstance(telemetry, Mapping) for telemetry in telemetry_sets):
            return None
        for telemetry in telemetry_sets:
            for module_id, value in telemetry.items():
                if isinstance(value, BaseModel):
                    value = value.model_dump(mode="json")
                if not isinstance(module_id, str) or not isinstance(value, Mapping):
                    return None
                version = value.get("module_version")
                if not isinstance(version, str):
                    return None
                current = merged.setdefault(
                    module_id,
                    {"module_id": module_id, "module_version": version},
                )
                if current["module_version"] != version:
                    current["module_version"] = "mixed"
                for field in counters:
                    count = value.get(field)
                    valid = isinstance(count, int) and not isinstance(count, bool) and count >= 0
                    if field not in current:
                        current[field] = count if valid else None
                    elif current[field] is not None:
                        if valid:
                            current[field] = int(current[field]) + count
                        else:
                            current[field] = None
        return merged
