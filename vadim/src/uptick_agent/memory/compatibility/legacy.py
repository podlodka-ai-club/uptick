"""Explicit adapter for the baseline ``Memory`` protocol and JSONL exchange."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

from uptick_agent.memory.audit_contracts import AuditTraceEvent, AuditTraceSink, AuditTraceWrite
from uptick_agent.memory.compatibility.contracts import MemoryEntry, MemoryMatch, MemoryQuery
from uptick_agent.memory.config import MemoryConfiguration, ModuleConfig
from uptick_agent.memory.contracts import (
    ContextItem,
    DecisionMemoryContext,
    ExperienceTransition,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    ProvenanceRef,
    RunOutcome,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.orchestrator import (
    MemoryContextDiagnostics,
    MemoryModuleRegistration,
    MemoryOrchestrator,
)
from uptick_agent.memory.stores.contracts import StructuredMemoryStore
from uptick_agent.ports import Memory
from uptick_agent.redaction import sanitize_json

_LEGACY_MODULE_ID = "compatibility.legacy"


class LegacyMemoryAdapter:
    """Preserves baseline lexical-memory semantics without making JSONL a store.

    JSONL is limited to legacy import/export.  Structured records and snapshots
    use the separate Stage 1 store contract.
    """

    def __init__(
        self,
        delegate: Memory,
        *,
        module_version: str = "legacy-1.0",
    ) -> None:
        self._delegate = delegate
        self._module_version = module_version

    async def remember(self, entry: MemoryEntry) -> None:
        safe_entry = MemoryEntry.model_validate(sanitize_json(entry.model_dump(mode="json")))
        await self._delegate.remember(safe_entry)

    async def recall(self, query: MemoryQuery) -> list[MemoryMatch]:
        return await self._delegate.recall(query)

    async def clear(self, run_id: str | None = None) -> None:
        await self._delegate.clear(run_id)

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        limit = 100 if request.max_items is None else min(request.max_items, 100)
        matches = await self.recall(
            MemoryQuery(
                text=request.query,
                run_id=request.run_id,
                include_other_runs=True,
                limit=limit,
            )
        )
        return MemoryContribution(
            module_id=_LEGACY_MODULE_ID,
            module_version=self._module_version,
            items=[self._context_item(match) for match in matches],
        )

    def _context_item(self, match: MemoryMatch) -> ContextItem:
        payload = match.entry.model_dump(mode="json")
        payload["tags"] = sorted(match.entry.tags)
        canonical = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        estimated_tokens = max(1, len(canonical.encode("utf-8")))
        return ContextItem(
            envelope=UntrustedMemoryEnvelope(
                item_id=match.entry.id,
                artefact_type=match.entry.kind,
                origin_module=_LEGACY_MODULE_ID,
                origin_version=self._module_version,
                trust_classification="external_untrusted",
                provenance=[
                    ProvenanceRef(
                        artefact_id=match.entry.id,
                        content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    )
                ],
                item=payload,
            ),
            score=match.score,
            selection_reason="legacy lexical recall",
            estimated_tokens=estimated_tokens,
        )

    async def import_jsonl(self, path: str | Path) -> int:
        entries = await asyncio.to_thread(self.read_jsonl, path)
        for entry in entries:
            await self.remember(entry)
        return len(entries)

    @staticmethod
    def read_jsonl(path: str | Path) -> list[MemoryEntry]:
        source_path = Path(path)
        if not source_path.exists():
            return []
        entries: list[MemoryEntry] = []
        with source_path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    entry = MemoryEntry.model_validate_json(line)
                    entries.append(
                        MemoryEntry.model_validate(sanitize_json(entry.model_dump(mode="json")))
                    )
                except ValueError as error:
                    raise ValueError(
                        f"invalid legacy memory entry at {source_path}:{line_number}"
                    ) from error
        return entries

    @staticmethod
    def write_jsonl(path: str | Path, entries: Iterable[MemoryEntry]) -> None:
        target_path = Path(path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            MemoryEntry.model_validate(
                sanitize_json(entry.model_dump(mode="json"))
            ).model_dump_json()
            + "\n"
            for entry in entries
        )
        target_path.write_text(payload, encoding="utf-8")


class LegacyMemoryRuntime:
    """One runner-facing boundary over Stage 3 orchestration and legacy writes."""

    def __init__(
        self,
        orchestrator: MemoryOrchestrator,
        legacy: LegacyMemoryAdapter | None,
    ) -> None:
        self._orchestrator = orchestrator
        self._legacy = legacy

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext:
        return await self._orchestrator.build_context(request)

    async def remember(self, entry: MemoryEntry) -> None:
        if self._legacy is not None:
            await self._legacy.remember(entry)

    async def record_transition(self, transition: ExperienceTransition) -> None:
        await self._orchestrator.record_transition(transition)

    async def clear(self, run_id: str | None = None) -> None:
        if self._legacy is not None:
            await self._legacy.clear(run_id)

    async def finalize_run(self, outcome: RunOutcome) -> None:
        await self._orchestrator.finalize_run(outcome)

    async def record_trace(self, write: AuditTraceWrite) -> AuditTraceEvent | None:
        return await self._orchestrator.record_trace(write)

    @property
    def last_context_diagnostics(self) -> MemoryContextDiagnostics:
        return self._orchestrator.last_context_diagnostics

    @property
    def context_diagnostics(self) -> dict:
        return self.last_context_diagnostics.model_dump(mode="json")

    @property
    def audit_sink(self) -> AuditTraceSink | None:
        return self._orchestrator.audit_sink


class _EpisodicMemoryRuntime(LegacyMemoryRuntime):
    async def clear(self, run_id: str | None = None) -> None:
        raise MemoryPermanentError(
            "episodic memory cannot be cleared safely; compose a fresh namespace"
        )


def legacy_memory_runtime(
    delegate: Memory | None,
    *,
    configuration: MemoryConfiguration | None = None,
    audit_sink: AuditTraceSink | None = None,
) -> LegacyMemoryRuntime:
    """Compose the canonical compatibility profile without constructing disabled memory."""

    if configuration is None and delegate is None:
        configuration = MemoryConfiguration(
            compatibility_legacy=ModuleConfig(enabled=False),
        )
    elif configuration is None:
        configuration = MemoryConfiguration.legacy_baseline()
    assert configuration is not None
    if configuration.compatibility_legacy.enabled != (delegate is not None):
        raise MemoryPermanentError(
            "legacy delegate presence must match the resolved compatibility module"
        )
    legacy = (
        LegacyMemoryAdapter(
            delegate,
            module_version=configuration.compatibility_legacy.version,
        )
        if delegate is not None
        else None
    )
    registrations = (
        [MemoryModuleRegistration(_LEGACY_MODULE_ID, lambda _: legacy)]
        if legacy is not None
        else []
    )
    orchestrator = MemoryOrchestrator(configuration, registrations, audit_sink=audit_sink)
    return LegacyMemoryRuntime(orchestrator, legacy)


def episodic_memory_runtime(
    store: StructuredMemoryStore,
    *,
    namespace: str,
    configuration: MemoryConfiguration | None = None,
    audit_sink: AuditTraceSink | None = None,
) -> LegacyMemoryRuntime:
    """Compose the experimental episodic-only runner boundary programmatically."""

    from uptick_agent.memory.episodic import EPISODIC_MODULE_ID, EpisodicMemory

    configuration = configuration or MemoryConfiguration.episodic_only()
    if configuration.compatibility_legacy.enabled or not configuration.episodic.enabled:
        raise MemoryPermanentError(
            "episodic runtime requires episodic enabled and legacy compatibility disabled"
        )
    module = EpisodicMemory(
        store,
        namespace=namespace,
        module_version=configuration.episodic.version,
    )
    orchestrator = MemoryOrchestrator(
        configuration,
        [MemoryModuleRegistration(EPISODIC_MODULE_ID, lambda _: module)],
        audit_sink=audit_sink,
    )
    return _EpisodicMemoryRuntime(orchestrator, None)
