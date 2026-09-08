from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from uptick_agent.core.bootstrap_models import (
    EnvironmentBootstrapArtifact,
    EnvironmentBootstrapIdentity,
    ToolRegistry,
)
from uptick_agent.core.memory_models import (
    ConsolidationBatch,
    ConsolidationQuery,
    EpisodeRecord,
    LearningOperationResult,
    LearningTrigger,
    LessonRecord,
    MemoryCommit,
    MemoryPacket,
    MemoryQuery,
    MemoryView,
    MemoryViewRequest,
)
from uptick_agent.core.models import (
    AgentContext,
    CapabilityCall,
    CapabilityCatalog,
    EnvironmentState,
    EnvironmentTelemetry,
    Observation,
    PolicyResult,
    ReasonerResult,
    ReasoningRequest,
    RunCompletion,
    RunManifest,
    RunResult,
    RunSpec,
    SGRDecision,
)
from uptick_agent.core.trace_models import TraceEvent


class Reasoner(Protocol):
    async def reason(self, request: ReasoningRequest) -> ReasonerResult: ...


class SGR(Protocol):
    def build_request(self, context: AgentContext) -> ReasoningRequest: ...

    def parse_result(self, context: AgentContext, result: ReasonerResult) -> SGRDecision: ...


class EnvironmentBootstrapper(Protocol):
    def identity(
        self,
        *,
        source: str,
        environment_id: str,
        capabilities: CapabilityCatalog,
    ) -> EnvironmentBootstrapIdentity: ...

    async def build(
        self,
        *,
        source: str,
        environment_id: str,
        capabilities: CapabilityCatalog,
    ) -> EnvironmentBootstrapArtifact: ...


class Environment(Protocol):
    async def initialize(self) -> str: ...

    async def bootstrap_capabilities(self) -> CapabilityCatalog: ...

    async def start(self, spec: RunSpec) -> EnvironmentState: ...

    async def capabilities(self, state: EnvironmentState) -> CapabilityCatalog: ...

    async def execute(self, call: CapabilityCall, state: EnvironmentState) -> Observation: ...

    def reduce(
        self,
        state: EnvironmentState,
        call: CapabilityCall,
        observation: Observation,
    ) -> EnvironmentState: ...

    async def result(
        self, state: EnvironmentState, completion: RunCompletion
    ) -> RunResult | None: ...

    def telemetry(self, run_id: str) -> EnvironmentTelemetry: ...


@runtime_checkable
class MemoryCompatibilityOwner(Protocol):
    """Optional stable, environment-owned memory scope, independent of LLM summaries."""

    def memory_profile_version(self) -> str | None: ...


@runtime_checkable
class ObservationReleaseOwner(Protocol):
    """Optional ownership port for explicitly releasing whole historical responses."""

    def prepare_observation_release(
        self, names: list[str]
    ) -> Callable[[EnvironmentState], EnvironmentState]:
        """Validate names without mutation and capture the currently shown snapshots.

        Return a commit that releases those snapshots and reprojects state, preserving
        latest_observation and any new same-name response. Invalid names raise ValueError.
        The caller commits only after successful composite reduction.
        """
        ...


@runtime_checkable
class EnvironmentLauncher(Protocol):
    """Launch a world and return its discovered, run-scoped execution session.

    The returned session's start() only binds the resolved bootstrap profile; it
    must not launch a second world. Preconstructed Environment implementations need no launcher.
    """

    async def run(self, spec: RunSpec) -> Environment: ...


class Memory(Protocol):
    async def resolve_view(self, request: MemoryViewRequest) -> MemoryView: ...

    async def recall(self, query: MemoryQuery) -> MemoryPacket: ...

    async def load_consolidation_batch(self, query: ConsolidationQuery) -> ConsolidationBatch: ...

    async def record_episode(
        self,
        episode: EpisodeRecord,
        base_revision: int,
    ) -> MemoryCommit: ...

    async def activate_lesson(
        self,
        lesson: LessonRecord,
        base_revision: int,
    ) -> MemoryCommit: ...


class RunStore(Protocol):
    async def load_bootstrap(
        self,
        identity: EnvironmentBootstrapIdentity,
    ) -> EnvironmentBootstrapArtifact | None: ...

    async def save_bootstrap(self, artifact: EnvironmentBootstrapArtifact) -> None: ...

    async def record(self, event: TraceEvent) -> None: ...

    async def load_stream(self, stream_id: str) -> list[TraceEvent]: ...

    async def save_result(self, result: RunResult) -> None: ...

    async def save_manifest(self, manifest: RunManifest) -> None: ...

    async def load_manifest(self, run_id: str) -> RunManifest | None: ...


class Policy(Protocol):
    def validate(self, context: AgentContext, decision: SGRDecision) -> PolicyResult: ...


class LearningPipeline(Protocol):
    @property
    def trigger(self) -> LearningTrigger: ...

    async def consolidate(
        self,
        *,
        trigger_run_id: str,
        trigger_episode_id: str | None,
        memory_view: MemoryView,
        environment_id: str,
        environment_profile_version: str,
        registry: ToolRegistry,
    ) -> LearningOperationResult: ...
