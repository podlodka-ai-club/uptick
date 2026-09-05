"""Compatibility facade for the v2 evaluation use cases.

Canonical implementations live under :mod:`uptick_agent.evaluation` and
:mod:`uptick_agent.composition`.
"""

from uptick_agent.composition.evaluation_memory import DefaultEvaluationMemoryFactory
from uptick_agent.evaluation.artifacts import (
    EvaluationArtifactStore,
    FilesystemEvaluationArtifactStore,
    InMemoryEvaluationArtifactStore,
)
from uptick_agent.evaluation.contracts import V2Manifest
from uptick_agent.evaluation.execution import EvaluationRuntime as _ExecutionEvaluationRuntime
from uptick_agent.evaluation.execution import _stable_run_identifier  # noqa: F401
from uptick_agent.evaluation.lifecycle import EvaluationJournal, LifecycleEvent
from uptick_agent.evaluation.ports import (
    EvaluationBindingFactory,
    EvaluationConfigFactory,
    EvaluationEnvironmentFactory,
    EvaluationMemoryFactory,
    EvaluationModelFactory,
)
from uptick_agent.evaluation.provenance import TrainingProvenanceValidator
from uptick_agent.evaluation.runtime_adapters import (  # noqa: F401
    _FinalizationError,
    _MemoryAdapter,
    _PrestartedEnvironment,
    _TelemetryModelAdapter,
    _TraceObserver,
)
from uptick_agent.evaluation.snapshots import EvaluationMemoryFacade, SnapshotReadStore
from uptick_agent.evaluation.telemetry import (  # noqa: F401
    _memory_telemetry,
    _outcome,
    _provider_sample,
    _provider_telemetry,
    _sum_complete,
    _sum_optional,
    _trace_payload,
    _try_trace_artifact,
)


class EvaluationRuntime(_ExecutionEvaluationRuntime):
    """Legacy constructor that wires the historical default memory factory."""

    def __init__(
        self,
        manifest: V2Manifest,
        *,
        environment_factory,
        model_factory,
        memory_factory=None,
        config_factory=None,
        binding_factory=None,
        runner_factory=None,
        journal=None,
    ) -> None:
        if memory_factory is None:
            default_memory = DefaultEvaluationMemoryFactory(manifest)
            memory_factory = default_memory
            if binding_factory is None:
                binding_factory = default_memory.freeze_binding
        super().__init__(
            manifest,
            environment_factory=environment_factory,
            model_factory=model_factory,
            memory_factory=memory_factory,
            config_factory=config_factory,
            binding_factory=binding_factory,
            runner_factory=runner_factory,
            journal=journal,
        )

__all__ = [
    "EvaluationArtifactStore",
    "InMemoryEvaluationArtifactStore",
    "FilesystemEvaluationArtifactStore",
    "LifecycleEvent",
    "EvaluationJournal",
    "EvaluationEnvironmentFactory",
    "EvaluationModelFactory",
    "EvaluationMemoryFactory",
    "EvaluationBindingFactory",
    "EvaluationConfigFactory",
    "DefaultEvaluationMemoryFactory",
    "EvaluationMemoryFacade",
    "SnapshotReadStore",
    "TrainingProvenanceValidator",
    "EvaluationRuntime",
]
