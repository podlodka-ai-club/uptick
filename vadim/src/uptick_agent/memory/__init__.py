from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.contracts import (
    ConsolidationDelta,
    ConsolidationParticipant,
    ConsolidationRequest,
    ConsolidationResult,
    ContextContributor,
    ContextItem,
    DecisionMemoryContext,
    ExperienceSink,
    ExperienceTransition,
    ExperienceTransitionAssembler,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryContractError,
    MemoryContribution,
    MemoryPermanentError,
    MemoryTransientError,
    MemoryValidationError,
    ObjectiveMetric,
    ObjectiveMetricDelta,
    OperationLink,
    ProvenanceRef,
    RunFinalizer,
    RunOutcome,
    TransitionAssemblyRequest,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.orchestrator import (
    MemoryContextDiagnostics,
    MemoryModuleRegistration,
    MemoryOrchestrator,
)

__all__ = [
    "ConsolidationDelta",
    "ConsolidationParticipant",
    "ConsolidationRequest",
    "ConsolidationResult",
    "ContextItem",
    "ContextContributor",
    "DecisionMemoryContext",
    "ExperienceSink",
    "ExperienceTransition",
    "ExperienceTransitionAssembler",
    "EpisodicMemory",
    "EPISODIC_MODULE_ID",
    "EPISODIC_MODULE_VERSION",
    "episodic_memory_runtime",
    "InMemoryMemory",
    "JsonlMemory",
    "LegacyMemoryAdapter",
    "LegacyMemoryRuntime",
    "legacy_memory_runtime",
    "MemoryConfiguration",
    "MemoryContextDiagnostics",
    "MemoryConflictError",
    "MemoryContractError",
    "MemoryContextRequest",
    "MemoryContribution",
    "MemoryPermanentError",
    "MemoryModuleRegistration",
    "MemoryOrchestrator",
    "MemoryTransientError",
    "MemoryValidationError",
    "NullMemory",
    "ObjectiveMetric",
    "ObjectiveMetricDelta",
    "OperationLink",
    "ProvenanceRef",
    "RunOutcome",
    "RunFinalizer",
    "TransitionAssemblyRequest",
    "UntrustedMemoryEnvelope",
]


def __getattr__(name: str):
    """Load model-dependent compatibility implementations without import cycles."""

    if name in {"InMemoryMemory", "NullMemory"}:
        from uptick_agent.memory import in_memory

        return getattr(in_memory, name)
    if name == "JsonlMemory":
        from uptick_agent.memory.jsonl import JsonlMemory

        return JsonlMemory
    if name in {"LegacyMemoryAdapter", "LegacyMemoryRuntime", "legacy_memory_runtime"}:
        from uptick_agent.memory.compatibility import legacy

        return getattr(legacy, name)
    if name in {"EpisodicMemory", "EPISODIC_MODULE_ID", "EPISODIC_MODULE_VERSION"}:
        from uptick_agent.memory import episodic

        return getattr(episodic, name)
    if name == "episodic_memory_runtime":
        from uptick_agent.memory.compatibility.legacy import episodic_memory_runtime

        return episodic_memory_runtime
    raise AttributeError(name)
