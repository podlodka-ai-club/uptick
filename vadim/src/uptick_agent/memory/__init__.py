from uptick_agent.memory.audit import (
    AuditTraceEvent,
    AuditTraceSink,
    AuditTraceWrite,
    RawBodyCapture,
    StructuredAuditTraceSink,
    audit_event_id,
)
from uptick_agent.memory.config import (
    AuditConfiguration,
    AuditRetentionConfiguration,
    LessonSettings,
    MemoryConfiguration,
    RawContentConfiguration,
)
from uptick_agent.memory.contracts import (
    ConsolidationDelta,
    ConsolidationParticipant,
    ConsolidationRequest,
    ConsolidationResult,
    ContextContributor,
    ContextItem,
    CreatedMemoryItem,
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
from uptick_agent.memory.lesson_contracts import (
    LESSON_QUERY_CONTRACT,
    LESSON_RETENTION_POLICY,
    LESSON_VALIDATION_AUTHORITY,
    LESSON_VALIDATION_POLICY,
    LessonCandidate,
    LessonEvidence,
    LessonRunDeclaration,
    LessonValidationManifest,
    ValidatedLesson,
    context_id,
    declaration_hash,
    snapshot_input_hash,
)
from uptick_agent.memory.lesson_evidence import StoredEpisodicLessonSource
from uptick_agent.memory.lesson_runtime import LessonsMemoryRuntime, lessons_memory_runtime
from uptick_agent.memory.lessons import (
    LESSON_BATCH_RECORD_TYPE,
    LESSONS_MODULE_ID,
    LESSONS_MODULE_VERSION,
    LessonBatch,
    LessonEvidenceSource,
    LessonsMemory,
)
from uptick_agent.memory.orchestrator import (
    MemoryContextDiagnostics,
    MemoryModuleRegistration,
    MemoryOrchestrator,
)

__all__ = [
    "AuditConfiguration",
    "AuditRetentionConfiguration",
    "AuditTraceEvent",
    "AuditTraceSink",
    "AuditTraceWrite",
    "audit_event_id",
    "LESSON_BATCH_RECORD_TYPE",
    "LESSONS_MODULE_ID",
    "LESSONS_MODULE_VERSION",
    "LESSON_QUERY_CONTRACT",
    "LESSON_RETENTION_POLICY",
    "LESSON_VALIDATION_AUTHORITY",
    "LESSON_VALIDATION_POLICY",
    "ConsolidationDelta",
    "ConsolidationParticipant",
    "ConsolidationRequest",
    "ConsolidationResult",
    "ContextItem",
    "ContextContributor",
    "CreatedMemoryItem",
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
    "LessonBatch",
    "LessonCandidate",
    "LessonEvidence",
    "LessonEvidenceSource",
    "LessonRunDeclaration",
    "LessonSettings",
    "LessonValidationManifest",
    "LessonsMemory",
    "LessonsMemoryRuntime",
    "ValidatedLesson",
    "StoredEpisodicLessonSource",
    "context_id",
    "declaration_hash",
    "snapshot_input_hash",
    "lessons_memory_runtime",
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
    "RawBodyCapture",
    "RawContentConfiguration",
    "RunOutcome",
    "RunFinalizer",
    "StructuredAuditTraceSink",
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
