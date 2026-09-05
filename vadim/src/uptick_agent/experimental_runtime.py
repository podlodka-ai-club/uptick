"""Composition seam for the experimental memory ablation matrix.

This module is deliberately separate from the simulator and evaluation
runner.  A v2 caller supplies its store and run declarations.  The returned facade
has the runner's memory methods plus an explicit ``consolidate_before_freeze``
operation.  No finalizer invokes consolidation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from uptick_agent.evaluation_presets import (
    EvaluationPreset,
    all_experimental_presets,
)
from uptick_agent.memory.audit import AuditTraceEvent, AuditTraceSink, AuditTraceWrite
from uptick_agent.memory.compatibility.legacy import LegacyMemoryAdapter
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.consolidation import ConsolidationMemory
from uptick_agent.memory.contracts import (
    ConsolidationRequest,
    ConsolidationResult,
    ContractModel,
    DecisionMemoryContext,
    ExperienceTransition,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    MemoryValidationError,
    RunOutcome,
)
from uptick_agent.memory.episodic import EpisodicMemory
from uptick_agent.memory.lesson_contracts import LessonRunDeclaration
from uptick_agent.memory.lesson_evidence import StoredEpisodicLessonSource
from uptick_agent.memory.maintenance import MaintenanceRetrievalView, MemoryMaintenance
from uptick_agent.memory.orchestrator import (
    MemoryModuleRegistration,
    MemoryModuleTelemetry,
    MemoryOrchestrator,
)
from uptick_agent.memory.playbooks import PlaybooksMemory
from uptick_agent.memory.retrieval import (
    AdvancedRetrievalSettings,
    AdvancedRetrievalStrategy,
    ChainedRetrievalStrategy,
    RetrievalStrategy,
    StructuredFeature,
)
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.memory.stores.contracts import StructuredMemoryStore, validate_namespace
from uptick_agent.memory.tool_knowledge import ToolKnowledgeMemory
from uptick_agent.memory.world_model import WorldModelMemory
from uptick_agent.models import MemoryEntry
from uptick_agent.ports import Memory


class OfflineSmokeResult(ContractModel):
    """Sanitized result for checking composition without a simulator call."""

    condition_id: str
    status: Literal["ok", "unsupported", "failed"]
    enabled_modules: tuple[str, ...] = ()
    context_items: int = Field(default=0, ge=0)
    error: str | None = None


@dataclass(frozen=True)
class _ConsolidationCoordinator:
    """Expose maintenance and validated knowledge under one capability."""

    maintenance: MemoryMaintenance
    knowledge: ConsolidationMemory

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        return await self.knowledge.retrieve(request)

    async def consolidate(self, request: ConsolidationRequest) -> ConsolidationResult:
        maintenance_result = await self.maintenance.consolidate(request)
        knowledge_result = await self.knowledge.consolidate(request)
        if (
            maintenance_result.request_id != request.request_id
            or maintenance_result.snapshot_id != request.snapshot_id
            or knowledge_result.request_id != request.request_id
            or knowledge_result.snapshot_id != request.snapshot_id
        ):
            raise MemoryPermanentError("consolidation participant returned another request")
        return ConsolidationResult(
            request_id=request.request_id,
            snapshot_id=request.snapshot_id,
            applied=maintenance_result.applied or knowledge_result.applied,
            deltas=[*maintenance_result.deltas, *knowledge_result.deltas],
        )


def fixed_evaluation_clock(created_at: datetime) -> Callable[[], datetime]:
    """Bind operational decay to an immutable evaluation binding timestamp."""

    if not isinstance(created_at, datetime) or created_at.utcoffset() is None:
        raise MemoryValidationError("evaluation clock requires an aware created_at")
    bound = created_at.astimezone(UTC)
    return lambda: bound


@dataclass(frozen=True)
class ExperimentalMemoryRuntime:
    """Runner-facing facade over one resolved experimental composition."""

    preset: EvaluationPreset
    orchestrator: MemoryOrchestrator
    maintenance: MemoryMaintenance | None
    legacy: LegacyMemoryAdapter | None

    @property
    def configuration(self) -> MemoryConfiguration:
        return self.preset.configuration.model_copy(deep=True)

    @property
    def enabled_module_ids(self) -> tuple[str, ...]:
        return self.orchestrator.enabled_module_ids

    @property
    def context_diagnostics(self) -> dict[str, object]:
        return self.orchestrator.last_context_diagnostics.model_dump(mode="json")

    @property
    def module_telemetry(self) -> dict[str, MemoryModuleTelemetry]:
        """Return lifecycle calls observed by the constructed modules."""

        return self.orchestrator.module_telemetry

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext:
        return await self.orchestrator.build_context(request)

    async def remember(self, entry: MemoryEntry) -> None:
        if self.legacy is not None:
            await self.legacy.remember(entry)

    async def record_transition(self, transition: ExperienceTransition) -> None:
        await self.orchestrator.record_transition(transition)

    async def clear(self, run_id: str | None = None) -> None:
        if self.legacy is not None:
            await self.legacy.clear(run_id)
        elif self.preset.configuration.episodic.enabled:
            raise MemoryPermanentError(
                "structured experimental memory cannot be cleared; compose a fresh namespace"
            )

    async def finalize_run(self, outcome: RunOutcome) -> None:
        await self.orchestrator.finalize_run(outcome)

    async def record_trace(self, write: AuditTraceWrite) -> AuditTraceEvent | None:
        return await self.orchestrator.record_trace(write)

    async def consolidate_before_freeze(
        self,
        snapshot_id: str,
        *,
        request_id: str,
        idempotency_key: str,
        apply: bool = False,
    ) -> ConsolidationResult:
        """Run the explicit Stage 9 command between training and freeze.

        ``apply=True`` requires that the same request already has a persisted
        dry-run plan.  This prevents the apply path from silently planning
        against a changed source snapshot.  The caller remains responsible
        for creating the immutable training snapshot and for freezing only
        after this method returns.
        """

        if self.maintenance is None:
            raise MemoryValidationError("consolidation is disabled for this experimental condition")
        return await self.orchestrator.consolidate(
            ConsolidationRequest(
                request_id=request_id,
                snapshot_id=snapshot_id,
                idempotency_key=idempotency_key,
                dry_run=not apply,
            )
        )


def _strategy(
    configuration: MemoryConfiguration,
    *,
    maintenance_view: MaintenanceRetrievalView | None,
    owner_strategy: RetrievalStrategy | None = None,
) -> RetrievalStrategy | None:
    stages: list[RetrievalStrategy] = []
    if owner_strategy is not None:
        stages.append(owner_strategy)
    advanced = configuration.retrieval.advanced
    if advanced.enabled:
        structured_features = (
            (
                StructuredFeature(
                    request_path="context.latest_result.action_kind",
                    candidate_path="item.lesson.action.kind",
                    weight=0.5,
                ),
                StructuredFeature(
                    request_path="context.latest_result.action_kind",
                    candidate_path="item.hypothesis.action_kind",
                    weight=0.5,
                ),
                StructuredFeature(
                    request_path="context.latest_result.ok",
                    candidate_path="item.hypothesis.result_value",
                    weight=0.25,
                ),
            )
            if configuration.retrieval.structured
            else ()
        )
        stages.append(
            AdvancedRetrievalStrategy(
                AdvancedRetrievalSettings(
                    enabled=True,
                    lexical_weight=(
                        advanced.lexical_weight if configuration.retrieval.lexical else 0.0
                    ),
                    structured_features=structured_features,
                    diversity_path=advanced.diversity_path,
                    diversity_penalty=advanced.diversity_penalty,
                    max_per_diversity_key=advanced.max_per_diversity_key,
                    deduplicate=advanced.deduplicate,
                    max_items=advanced.max_items,
                    max_estimated_tokens=advanced.max_estimated_tokens,
                )
            )
        )
    if maintenance_view is not None:
        stages.append(maintenance_view)
    if not stages:
        return None
    return stages[0] if len(stages) == 1 else ChainedRetrievalStrategy(*stages)


def compose_experimental_runtime(
    preset: EvaluationPreset | MemoryConfiguration | str,
    store: StructuredMemoryStore,
    *,
    namespace: str,
    condition_id: str | None = None,
    run_declarations: Sequence[LessonRunDeclaration] = (),
    legacy_memory: Memory | None = None,
    clock: Callable[[], datetime] | None = None,
    audit_sink: AuditTraceSink | None = None,
) -> ExperimentalMemoryRuntime:
    """Construct real enabled modules for one preset.

    The generic tool-knowledge module receives the same retained evidence
    source as the other derived modules.  Its adapter identity remains an
    explicit setting in the resolved configuration.
    """

    resolved = _resolve_preset(preset, condition_id=condition_id)
    configuration = resolved.configuration
    if configuration.retrieval.semantic:
        raise MemoryValidationError(
            "semantic retrieval is enabled but no semantic implementation is registered"
        )
    if isinstance(run_declarations, (str, bytes)):
        raise MemoryValidationError("run_declarations must be a sequence")
    try:
        owned_declarations = tuple(run_declarations)
    except TypeError as error:
        raise MemoryValidationError("run_declarations must be a sequence") from error
    base = validate_namespace(namespace)
    episodic_namespace = base
    lesson_namespace = validate_namespace(f"{base}:lessons")
    world_namespace = validate_namespace(f"{base}:world")
    playbook_namespace = validate_namespace(f"{base}:playbooks")
    tool_namespace = validate_namespace(f"{base}:tool-knowledge")
    now = clock or (lambda: datetime.now(UTC))

    source: StoredEpisodicLessonSource | None = None
    declaration_namespace: str | None = None
    if (
        configuration.lessons.enabled
        or configuration.world_model.enabled
        or configuration.playbooks.enabled
        or configuration.tool_knowledge.enabled
        or configuration.consolidation.enabled
    ):
        declaration_namespace = validate_namespace(f"{lesson_namespace}:declarations")
        source = StoredEpisodicLessonSource(
            store,
            episodic_namespace=episodic_namespace,
            declaration_namespace=declaration_namespace,
            run_declarations=owned_declarations,
        )

    maintenance: MemoryMaintenance | None = None
    maintenance_view: MaintenanceRetrievalView | None = None
    consolidation: _ConsolidationCoordinator | None = None
    if configuration.consolidation.enabled or configuration.forgetting.enabled:
        maintenance = MemoryMaintenance(
            store,
            namespace=episodic_namespace,
            maintenance_namespace=validate_namespace(f"{base}:maintenance"),
            clock=now,
        )
        if configuration.consolidation.enabled:
            if configuration.consolidation_settings is None:
                raise MemoryValidationError(
                    "consolidation requires explicit consolidation_settings"
                )
            if declaration_namespace is None:
                raise MemoryValidationError("consolidation requires a declaration namespace")
            consolidation_memory = ConsolidationMemory(
                store,
                namespace=validate_namespace(f"{base}:consolidation"),
                evidence_namespace=episodic_namespace,
                declaration_namespace=declaration_namespace,
                settings=configuration.consolidation_settings,
                module_version=configuration.consolidation.version,
                clock=now,
            )
            consolidation = _ConsolidationCoordinator(
                maintenance=maintenance,
                knowledge=consolidation_memory,
            )
        maintenance_view = MaintenanceRetrievalView(
            store,
            namespace=episodic_namespace,
            maintenance_namespace=maintenance.maintenance_namespace,
            # A5 exposes applied supersession; A9 additionally enables decay.
            decay_days=configuration.forgetting_settings.decay_days,
            apply_decay=(
                configuration.forgetting.enabled and configuration.forgetting_settings.apply_decay
            ),
            clock=now,
        )

    legacy: LegacyMemoryAdapter | None = None
    if configuration.compatibility_legacy.enabled:
        delegate = legacy_memory or _new_legacy_memory()
        legacy = LegacyMemoryAdapter(
            delegate,
            module_version=configuration.compatibility_legacy.version,
        )

    registrations: list[MemoryModuleRegistration] = []
    if configuration.compatibility_legacy.enabled:
        registrations.append(
            MemoryModuleRegistration(
                "compatibility.legacy",
                lambda _config, module=legacy: module,
            )
        )
    if configuration.episodic.enabled:
        registrations.append(
            MemoryModuleRegistration(
                "episodic",
                lambda module_config: EpisodicMemory(
                    store,
                    namespace=episodic_namespace,
                    module_version=module_config.version,
                ),
                retrieval_strategy=_strategy(configuration, maintenance_view=maintenance_view),
            )
        )
    if configuration.lessons.enabled:
        if source is None or configuration.lesson_settings is None:
            raise MemoryValidationError("lessons require explicit settings and evidence source")
        from uptick_agent.memory.lessons import LessonsMemory

        registrations.append(
            MemoryModuleRegistration(
                "lessons",
                lambda module_config, source=source: LessonsMemory(
                    store,
                    namespace=lesson_namespace,
                    source=source,
                    settings=configuration.lesson_settings,
                    module_version=module_config.version,
                ),
                requires=("episodic",),
                retrieval_strategy=_strategy(configuration, maintenance_view=maintenance_view),
            )
        )
    if configuration.world_model.enabled:
        if source is None:
            raise MemoryValidationError("world_model requires an evidence source")
        if configuration.world_query_settings is None:
            raise MemoryValidationError("world_model requires explicit query settings")
        settings = configuration.world_query_settings
        registrations.append(
            MemoryModuleRegistration(
                "world_model",
                lambda module_config, source=source, settings=settings: WorldModelMemory(
                    store,
                    namespace=world_namespace,
                    source=source,
                    settings=settings,
                    module_version=module_config.version,
                ),
                requires=("episodic",),
                retrieval_strategy=_strategy(configuration, maintenance_view=maintenance_view),
            )
        )
    if configuration.playbooks.enabled:
        if source is None:
            raise MemoryValidationError("playbooks require an evidence source")
        if configuration.playbook_query_settings is None:
            raise MemoryValidationError("playbooks require explicit query settings")
        settings = configuration.playbook_query_settings
        registrations.append(
            MemoryModuleRegistration(
                "playbooks",
                lambda module_config, source=source, settings=settings: PlaybooksMemory(
                    store,
                    namespace=playbook_namespace,
                    source=source,
                    settings=settings,
                    module_version=module_config.version,
                ),
                requires=("episodic", "lessons"),
                retrieval_strategy=_strategy(configuration, maintenance_view=maintenance_view),
            )
        )
    if configuration.tool_knowledge.enabled:
        if source is None:
            raise MemoryValidationError("tool_knowledge requires an evidence source")
        if configuration.tool_knowledge_query_settings is None:
            raise MemoryValidationError("tool_knowledge requires explicit query settings")
        settings = configuration.tool_knowledge_query_settings
        registrations.append(
            MemoryModuleRegistration(
                "tool_knowledge",
                lambda module_config, source=source, settings=settings: ToolKnowledgeMemory(
                    store,
                    namespace=tool_namespace,
                    source=source,
                    settings=settings,
                    module_version=module_config.version,
                ),
                requires=("episodic",),
                retrieval_strategy=_strategy(configuration, maintenance_view=maintenance_view),
            )
        )
    if configuration.consolidation.enabled:
        if consolidation is None:
            raise MemoryValidationError("consolidation requires maintenance capability")
        registrations.append(
            MemoryModuleRegistration(
                "consolidation",
                lambda _config, module=consolidation: module,
                requires=("episodic",),
                retrieval_strategy=_strategy(configuration, maintenance_view=maintenance_view),
            )
        )
    if configuration.forgetting.enabled:
        if maintenance_view is None:
            raise MemoryValidationError("forgetting requires an operational maintenance view")
        # The view is a real read-side strategy.  It is registered as a
        # capability marker while the same object is attached to every
        # contributor above, so it cannot drop module lifecycle methods.
        registrations.append(
            MemoryModuleRegistration(
                "forgetting",
                lambda _config, module=maintenance_view: module,
                requires=("episodic",),
            )
        )
    orchestrator = MemoryOrchestrator(
        configuration,
        registrations,
        audit_sink=audit_sink,
    )
    return ExperimentalMemoryRuntime(resolved, orchestrator, maintenance, legacy)


def _resolve_preset(
    preset: EvaluationPreset | MemoryConfiguration | str,
    *,
    condition_id: str | None = None,
) -> EvaluationPreset:
    if isinstance(preset, EvaluationPreset):
        return preset
    if isinstance(preset, MemoryConfiguration):
        return EvaluationPreset(
            condition_id=condition_id or preset.profile_id,
            configuration=preset,
        )
    if not isinstance(preset, str):
        raise MemoryValidationError("experimental preset must be an EvaluationPreset or ID")
    for candidate in all_experimental_presets():
        if candidate.condition_id == preset:
            return candidate
    raise MemoryValidationError(f"unknown experimental preset {preset!r}")


def _new_legacy_memory() -> Memory:
    from uptick_agent.memory.in_memory import InMemoryMemory

    return InMemoryMemory()


async def offline_smoke(
    presets: Iterable[EvaluationPreset] = (),
) -> tuple[OfflineSmokeResult, ...]:
    """Compose and read every supplied condition with an empty local store."""

    selected = tuple(presets) or all_experimental_presets()
    results: list[OfflineSmokeResult] = []
    request = MemoryContextRequest(
        request_id="offline-smoke",
        run_id="offline-smoke-run",
        query="health",
        context={
            "latest_result": {
                "action_kind": "get_overview",
                "ok": True,
                "summary": "offline smoke",
                "data": {},
                "terminal": False,
            }
        },
    )
    for preset in selected:
        if not preset.supported:
            results.append(
                OfflineSmokeResult(
                    condition_id=preset.condition_id,
                    status="unsupported",
                    error="; ".join(preset.unsupported_reasons),
                )
            )
            continue
        try:
            runtime = compose_experimental_runtime(
                preset,
                InMemoryStructuredStore(),
                namespace=f"offline:{preset.condition_id}",
            )
            context = await runtime.build_context(request)
            results.append(
                OfflineSmokeResult(
                    condition_id=preset.condition_id,
                    status="ok",
                    enabled_modules=runtime.enabled_module_ids,
                    context_items=len(context.items),
                )
            )
        except MemoryValidationError as error:
            results.append(
                OfflineSmokeResult(
                    condition_id=preset.condition_id,
                    status="unsupported",
                    error=str(error),
                )
            )
        except Exception as error:
            results.append(
                OfflineSmokeResult(
                    condition_id=preset.condition_id,
                    status="failed",
                    error=f"{type(error).__name__}: {error}",
                )
            )
    return tuple(results)


__all__ = [
    "ExperimentalMemoryRuntime",
    "OfflineSmokeResult",
    "compose_experimental_runtime",
    "fixed_evaluation_clock",
    "offline_smoke",
]
