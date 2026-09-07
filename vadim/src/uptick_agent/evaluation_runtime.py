"""Compatibility facade for the v2 evaluation use cases.

Canonical implementations live under :mod:`uptick_agent.evaluation` and
:mod:`uptick_agent.composition`.
"""

import inspect

from uptick_agent.composition.evaluation_memory import DefaultEvaluationMemoryFactory
from uptick_agent.decisions.contracts import NextStep
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
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
        environment_factory = _legacy_environment_factory(environment_factory)
        model_factory = _legacy_model_factory(model_factory)
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


def _legacy_environment_factory(factory):
    async def wrapped(block, condition, attempt):
        environment = factory(block, condition, attempt)
        if inspect.isawaitable(environment):
            environment = await environment
        if _publishes_environment_spec(environment):
            return environment
        return _LegacyEnvironmentAdapter(environment)

    return wrapped


def _publishes_environment_spec(environment: object) -> bool:
    """Detect a startup-bound spec without evaluating its property early."""

    declared = inspect.getattr_static(environment, "decision_spec", None)
    if isinstance(declared, (property, EnvironmentDecisionSpec)):
        return True
    try:
        return isinstance(environment.decision_spec, EnvironmentDecisionSpec)
    except (AttributeError, RuntimeError):
        return False


def _legacy_model_factory(factory):
    def wrapped(block, condition, attempt, run_id, decision_spec):
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            return factory(block, condition, attempt, run_id, decision_spec)
        four_args = (block, condition, attempt, run_id)
        five_args = (*four_args, decision_spec)
        try:
            signature.bind(*five_args)
        except TypeError:
            return factory(*four_args)
        else:
            return factory(block, condition, attempt, run_id, decision_spec)

    return wrapped


class _LegacyEnvironmentAdapter:
    """Compatibility port for pre-spec evaluation test/application adapters."""

    decision_spec = EnvironmentDecisionSpec(response_model=NextStep)

    def __init__(self, environment):
        self._environment = environment

    async def start(self, **kwargs):
        return await self._environment.start(**kwargs)

    async def execute(self, session, action):
        return await self._environment.execute(session, action)

    def public_state(self, session):
        return {}

    async def finish(self, session, **kwargs):
        return await self._environment.finish(session, **kwargs)

    async def aclose(self):
        closer = getattr(self._environment, "aclose", None)
        if callable(closer):
            result = closer()
            if inspect.isawaitable(result):
                await result


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
