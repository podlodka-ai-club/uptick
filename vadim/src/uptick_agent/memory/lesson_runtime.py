"""Explicit composition for the experimental episodic-plus-lessons profile."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from uptick_agent.memory.audit_contracts import AuditTraceEvent, AuditTraceSink, AuditTraceWrite
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.contracts import (
    DecisionMemoryContext,
    ExperienceTransition,
    MemoryContextRequest,
    MemoryPermanentError,
    RunOutcome,
)
from uptick_agent.memory.episodic import EpisodicMemory
from uptick_agent.memory.lesson_contracts import LessonRunDeclaration
from uptick_agent.memory.lesson_evidence import StoredEpisodicLessonSource
from uptick_agent.memory.lessons import LessonsMemory
from uptick_agent.memory.orchestrator import (
    MemoryContextDiagnostics,
    MemoryModuleRegistration,
    MemoryOrchestrator,
)
from uptick_agent.memory.stores.contracts import StructuredMemoryStore, validate_namespace


class LessonsMemoryRuntime:
    """Runner-facing facade over one explicit ordered module composition."""

    def __init__(self, orchestrator: MemoryOrchestrator) -> None:
        self._orchestrator = orchestrator

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext:
        return await self._orchestrator.build_context(request)

    async def record_transition(self, transition: ExperienceTransition) -> None:
        await self._orchestrator.record_transition(transition)

    async def remember(self, entry: Any) -> None:
        """Compatibility hook; structured episodic transitions own writes."""

        return None

    async def clear(self, run_id: str | None = None) -> None:
        raise MemoryPermanentError("lessons runtime cannot clear structured memory safely")

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


def lessons_memory_runtime(
    store: StructuredMemoryStore,
    *,
    episodic_namespace: str,
    lesson_namespace: str,
    run_declarations: Sequence[LessonRunDeclaration],
    configuration: MemoryConfiguration,
    audit_sink: AuditTraceSink | None = None,
) -> LessonsMemoryRuntime:
    """Compose the opted-in episodic-plus-lessons runtime.

    The declaration namespace is derived from the lesson namespace and is
    intentionally separate from both source and derived lesson records.
    Construction performs no store reads before validating the profile and
    namespace boundaries.
    """

    episodic_namespace = validate_namespace(episodic_namespace)
    lesson_namespace = validate_namespace(lesson_namespace)
    if configuration.compatibility_legacy.enabled:
        raise MemoryPermanentError("lessons runtime requires legacy compatibility disabled")
    if configuration.lessons.enabled:
        if not configuration.episodic.enabled:
            raise MemoryPermanentError("lessons runtime requires episodic enabled")
        if configuration.lesson_settings is None:
            raise MemoryPermanentError("lessons runtime requires explicit lesson_settings")
        if isinstance(run_declarations, (str, bytes)) or not isinstance(run_declarations, Sequence):
            raise MemoryPermanentError("run_declarations must be a sequence")
        declaration_namespace = validate_namespace(f"{lesson_namespace}:declarations")
        if len({episodic_namespace, lesson_namespace, declaration_namespace}) != 3:
            raise MemoryPermanentError(
                "episodic, lesson, and declaration namespaces must be disjoint"
            )

    registrations: list[MemoryModuleRegistration] = []
    if configuration.episodic.enabled:
        registrations.append(
            MemoryModuleRegistration(
                "episodic",
                lambda module_config: EpisodicMemory(
                    store,
                    namespace=episodic_namespace,
                    module_version=module_config.version,
                ),
            )
        )
    if configuration.lessons.enabled:
        registrations.append(
            MemoryModuleRegistration(
                "lessons",
                lambda module_config: LessonsMemory(
                    store,
                    namespace=lesson_namespace,
                    source=StoredEpisodicLessonSource(
                        store,
                        episodic_namespace=episodic_namespace,
                        declaration_namespace=declaration_namespace,
                        run_declarations=run_declarations,
                    ),
                    settings=configuration.lesson_settings,
                    module_version=module_config.version,
                ),
                requires=("episodic",),
            )
        )
    orchestrator = MemoryOrchestrator(configuration, registrations, audit_sink=audit_sink)
    return LessonsMemoryRuntime(orchestrator)


__all__ = ["LessonsMemoryRuntime", "lessons_memory_runtime"]
