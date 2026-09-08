from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import cast

from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.bootstrap_models import EnvironmentBootstrapArtifact
from uptick_agent.core.contracts import (
    Environment,
    EnvironmentBootstrapper,
    EnvironmentLauncher,
    LearningPipeline,
    Memory,
    MemoryCompatibilityOwner,
    Policy,
    RunStore,
)
from uptick_agent.core.errors import ReasonerFailure, RunExecutionError, RunStoreFailure
from uptick_agent.core.memory_models import (
    EpisodeRecord,
    EvidenceRef,
    MemoryView,
    MemoryViewRequest,
)
from uptick_agent.core.models import (
    AgentContext,
    AgentWorkingState,
    CapabilityCall,
    ClosedEpisodeBridge,
    EnvironmentProfileRef,
    EnvironmentTelemetry,
    FailureRecord,
    JsonObject,
    Observation,
    OpenDecision,
    PolicyResult,
    RunCompletion,
    RunManifest,
    RunMetrics,
    RunResult,
    RunSpec,
    SGRDecision,
    TokenUsage,
)
from uptick_agent.core.trace_models import (
    DecisionTracePayload,
    EpisodeClosedPayload,
    EpisodeCommittedPayload,
    FailureStage,
    RunFailedPayload,
    RunStartedPayload,
    TraceEvent,
    TraceEventKindV5,
    TracePayloadV5,
    run_stream_id,
)
from uptick_agent.runtime.context import ContextAssembler, RunState
from uptick_agent.runtime.episodes import (
    evidence_group_id,
    project_closed_episode,
    validate_episode_transition,
)

type RecordEvent = Callable[[TraceEventKindV5, TracePayloadV5], Awaitable[int]]


class PolicyViolationError(RuntimeError):
    """The selected decision failed local policy before environment execution."""


class AgentRunner:
    """Own the run lifecycle while depending only on ports and shared values."""

    def __init__(
        self,
        *,
        agent_core: AgentCore,
        environment: Environment | EnvironmentLauncher,
        memory: Memory,
        run_store: RunStore,
        policy: Policy,
        bootstrapper: EnvironmentBootstrapper,
        bootstrap_artifact: EnvironmentBootstrapArtifact | None = None,
        context_assembler: ContextAssembler | None = None,
        learning: LearningPipeline | None = None,
        monotonic_fn=monotonic,
        utcnow_fn=lambda: datetime.now(UTC),
    ) -> None:
        self._agent_core = agent_core
        self._launcher = environment if isinstance(environment, EnvironmentLauncher) else None
        self._environment = cast(Environment, environment)
        self._memory = memory
        self._run_store = run_store
        self._policy = policy
        self._bootstrapper = bootstrapper
        self._bootstrap_artifact = bootstrap_artifact
        self._context_assembler = context_assembler or ContextAssembler()
        self._learning = learning
        self._monotonic = monotonic_fn
        self._utcnow = utcnow_fn

    async def run(self, spec: RunSpec) -> RunResult:
        run_started = self._monotonic()
        started_at = self._utcnow()
        environment_duration = 0.0

        if self._launcher is not None:
            port_started = self._monotonic()
            self._environment = await self._launcher.run(spec)
            environment_duration += self._elapsed(port_started)

        artifact, bootstrap_cache_hit, bootstrap_duration = await self._bootstrap(spec)
        environment_duration += bootstrap_duration
        profile = EnvironmentProfileRef(
            environment_id=artifact.bundle.profile.environment_id,
            version=artifact.bundle.profile.profile_version,
        )
        # Exact bootstrap identity remains in the state, prompt and trace. Memory
        # compatibility is owned by the environment, never by generated prose.
        # Explicit strict artifacts keep their pinned historical memory scope.
        memory_profile_version = profile.version
        if self._bootstrap_artifact is None and isinstance(
            self._environment, MemoryCompatibilityOwner
        ):
            memory_profile_version = self._environment.memory_profile_version() or profile.version
        memory_view = await self._memory.resolve_view(
            MemoryViewRequest(
                environment_id=profile.environment_id,
                environment_profile_version=memory_profile_version,
                expected_database_id=spec.metadata.memory_database_id,
                expected_revision=spec.metadata.memory_start_revision,
            )
        )
        environment_spec = spec.model_copy(update={"environment_profile": profile}, deep=True)
        port_started = self._monotonic()
        environment_state = await self._environment.start(environment_spec)
        environment_duration += self._elapsed(port_started)
        if environment_state.profile != profile:
            raise ValueError("environment returned a state for a different bootstrap profile")

        run_state = RunState(
            run_id=spec.run_id,
            step=1,
            step_limit=spec.step_limit,
            memory_view=memory_view,
            environment_state=environment_state,
        )
        sequence = 0
        store_failed = False
        operational_finished = False
        stage = "run_started"
        model_turns = 0
        capability_executions = 0
        policy_rejections = 0
        action_counts: dict[str, int] = {}
        model_duration = 0.0
        token_usage = TokenUsage()
        context_bytes = 0
        open_evidence_refs: list[EvidenceRef] = []
        open_evidence_steps: list[str] = []
        stream_id = run_stream_id(spec.run_id)

        async def record(kind: TraceEventKindV5, payload: TracePayloadV5) -> int:
            nonlocal sequence, store_failed
            sequence += 1
            try:
                await self._run_store.record(
                    TraceEvent(
                        stream_id=stream_id,
                        stream_kind="run",
                        sequence=sequence,
                        kind=kind,
                        payload=payload,
                    )
                )
            except Exception:
                store_failed = True
                raise
            return sequence

        async def save_manifest(value: RunManifest) -> None:
            nonlocal store_failed
            try:
                await self._run_store.save_manifest(value)
            except Exception:
                store_failed = True
                raise

        def metrics() -> RunMetrics:
            telemetry = self._safe_environment_telemetry(spec.run_id)
            return RunMetrics(
                model_turns=model_turns,
                capability_executions=capability_executions,
                policy_rejections=policy_rejections,
                context_bytes=context_bytes,
                action_counts=dict(action_counts),
                simulator_calls=telemetry.external_calls,
                program_executions=telemetry.program_executions,
                program_subcalls=telemetry.program_subcalls,
                model_duration_seconds=model_duration,
                environment_duration_seconds=environment_duration,
                transport_duration_seconds=telemetry.transport_duration_seconds,
                token_usage=token_usage,
            )

        manifest = _initial_manifest(
            spec,
            started_at,
            bootstrap_artifact=artifact,
            bootstrap_cache_hit=bootstrap_cache_hit,
            memory_view=memory_view,
        )
        if memory_profile_version != profile.version:
            manifest = manifest.model_copy(
                update={"memory_profile_version": memory_profile_version}
            )
        await save_manifest(manifest)
        await record(
            "run_started",
            RunStartedPayload(
                run_id=spec.run_id,
                environment_id=profile.environment_id,
                environment_profile_version=profile.version,
                memory_view=memory_view,
                initial_state=cast(JsonObject, environment_state.model_dump(mode="json")),
                started_at=started_at,
            ),
        )

        try:
            stop_reason = "step limit reached"
            completed_steps = 0
            while run_state.environment_state.status == "active" and _within_step_limit(run_state):
                context: AgentContext | None = None
                decision: SGRDecision | None = None
                policy_result: PolicyResult | None = None
                observation: Observation | None = None
                step_environment_duration = 0.0
                trace_recorded = False
                verification_validated = False
                stage = "capabilities"
                try:
                    port_started = self._monotonic()
                    capabilities = await self._environment.capabilities(run_state.environment_state)
                    environment_duration += self._elapsed(port_started)

                    stage = "memory_recall"
                    memory = await self._memory.recall(
                        self._context_assembler.memory_query(
                            objective=spec.objective,
                            environment_state=run_state.environment_state,
                            capabilities=capabilities,
                            memory_view=run_state.memory_view,
                            memory_profile_version=memory_profile_version,
                        )
                    )
                    if memory.view != run_state.memory_view:
                        raise ValueError("memory recall returned a different pinned view")
                    context = self._context_assembler.assemble(
                        objective=spec.objective,
                        environment_profile=artifact.bundle.decision_brief(),
                        run_state=run_state,
                        capabilities=capabilities,
                        memory=memory,
                        constraints=spec.constraints,
                    )
                    context_bytes += len(context.model_dump_json(indent=2).encode("utf-8"))
                    fingerprint = self._agent_core.fingerprint(context)
                    if manifest.schema_sha256 is None:
                        manifest = manifest.model_copy(
                            update={
                                "system_prompt_sha256": fingerprint.system_prompt_sha256,
                                "schema_sha256": fingerprint.schema_sha256,
                            },
                            deep=True,
                        )
                        await save_manifest(manifest)

                    stage = "reasoner"
                    decision = await self._agent_core.decide(context)
                    model_turns += decision.telemetry.attempts
                    model_duration += decision.telemetry.duration_seconds
                    token_usage = token_usage.plus(decision.telemetry.token_usage)
                    if (
                        manifest.provider_instructions_sha256 is None
                        and decision.telemetry.provider_instructions_sha256 is not None
                    ):
                        manifest = manifest.model_copy(
                            update={
                                "provider_instructions_sha256": (
                                    decision.telemetry.provider_instructions_sha256
                                )
                            },
                            deep=True,
                        )
                        await save_manifest(manifest)

                    stage = "policy"
                    policy_result = self._policy.validate(context, decision)
                    if not policy_result.accepted:
                        policy_rejections += 1
                        raise PolicyViolationError("; ".join(policy_result.violations))
                    validate_episode_transition(
                        open_decision=run_state.agent_working_state.open_decision,
                        assessment=decision.envelope.previous_verification,
                    )
                    verification_validated = True

                    stage = "environment_execute"
                    call = decision.envelope.selected_action
                    capability_executions += 1
                    action_counts[call.name] = action_counts.get(call.name, 0) + 1
                    port_started = self._monotonic()
                    try:
                        observation = await self._environment.execute(
                            call, run_state.environment_state
                        )
                    finally:
                        elapsed = self._elapsed(port_started)
                        environment_duration += elapsed
                        step_environment_duration += elapsed

                    stage = "environment_reduce"
                    port_started = self._monotonic()
                    try:
                        new_environment_state = self._environment.reduce(
                            run_state.environment_state,
                            call,
                            observation,
                        )
                    finally:
                        elapsed = self._elapsed(port_started)
                        environment_duration += elapsed
                        step_environment_duration += elapsed

                    trace_sequence = await record(
                        "decision_trace",
                        DecisionTracePayload(
                            run_id=spec.run_id,
                            iteration=run_state.step,
                            context_projection=cast(JsonObject, context.model_dump(mode="json")),
                            context_sha256=fingerprint.context_sha256,
                            capabilities=capabilities,
                            decision=decision,
                            policy_result=policy_result,
                            action=call,
                            observation=observation,
                            retrieval_diagnostics=memory.diagnostics,
                            model_duration_seconds=decision.telemetry.duration_seconds,
                            environment_duration_seconds=step_environment_duration,
                        ),
                    )
                    trace_recorded = True
                    evidence_ref = EvidenceRef(
                        stream_id=stream_id,
                        sequence=trace_sequence,
                        kind="decision_trace",
                    )
                    stage = "episode_write"
                    episode = await _record_closed_episode(
                        record=record,
                        context=context,
                        decision=decision,
                        run_id=spec.run_id,
                        world_id=spec.world_id,
                        evidence_steps=open_evidence_steps,
                        evidence_refs=_bounded_evidence_refs([*open_evidence_refs, evidence_ref]),
                        write_enabled=self._learning is not None,
                        memory_profile_version=memory_profile_version,
                    )
                    next_memory_view = await _commit_episode(
                        record=record,
                        memory=self._memory,
                        episode=episode,
                        run_id=spec.run_id,
                        memory_view=run_state.memory_view,
                        write_enabled=self._learning is not None,
                    )
                    if (
                        episode is not None
                        and self._learning is not None
                        and self._learning.trigger == "after_closed_episode"
                    ):
                        stage = "learning"
                        learning_result = await self._learning.consolidate(
                            trigger_run_id=spec.run_id,
                            trigger_episode_id=episode.record_id,
                            memory_view=next_memory_view,
                            environment_id=profile.environment_id,
                            environment_profile_version=memory_profile_version,
                            registry=artifact.bundle.registry,
                        )
                        next_memory_view = learning_result.end_view
                    working, open_evidence_refs, open_evidence_steps = _advance_working_state(
                        run_state.agent_working_state,
                        decision,
                        step=run_state.step,
                        episode=episode,
                        situation_summary=_decision_situation_summary(context, decision),
                        observation=observation,
                        current_evidence_ref=evidence_ref,
                        prior_evidence_refs=open_evidence_refs,
                        prior_evidence_steps=open_evidence_steps,
                    )
                    run_state = run_state.model_copy(
                        update={
                            "step": run_state.step + 1,
                            "memory_view": next_memory_view,
                            "environment_state": new_environment_state,
                            "agent_working_state": working,
                        },
                        deep=True,
                    )
                    completed_steps += 1
                    if observation.terminal:
                        stop_reason = observation.summary
                        break
                except Exception as error:
                    if isinstance(error, RunStoreFailure):
                        store_failed = True
                    failure_stage = stage
                    if context is not None and not trace_recorded and not store_failed:
                        failure = _failure_record(stage, error)
                        failure_telemetry = (
                            error.telemetry if isinstance(error, ReasonerFailure) else None
                        )
                        trace_sequence = await record(
                            "decision_trace",
                            DecisionTracePayload(
                                run_id=spec.run_id,
                                iteration=run_state.step,
                                context_projection=cast(
                                    JsonObject, context.model_dump(mode="json")
                                ),
                                context_sha256=self._agent_core.fingerprint(context).context_sha256,
                                capabilities=context.capabilities,
                                decision=decision,
                                policy_result=policy_result,
                                action=(
                                    decision.envelope.selected_action
                                    if decision is not None
                                    else None
                                ),
                                observation=observation,
                                model_duration_seconds=(
                                    failure_telemetry.duration_seconds
                                    if failure_telemetry is not None
                                    else (
                                        decision.telemetry.duration_seconds
                                        if decision is not None
                                        else 0.0
                                    )
                                ),
                                environment_duration_seconds=step_environment_duration,
                                failure_stage=cast(FailureStage, stage),
                                failure=failure,
                            ),
                        )
                        if decision is not None and verification_validated:
                            episode = await _record_closed_episode(
                                record=record,
                                context=context,
                                decision=decision,
                                run_id=spec.run_id,
                                world_id=spec.world_id,
                                evidence_steps=open_evidence_steps,
                                evidence_refs=_bounded_evidence_refs(
                                    [
                                        *open_evidence_refs,
                                        EvidenceRef(
                                            stream_id=stream_id,
                                            sequence=trace_sequence,
                                            kind="decision_trace",
                                        ),
                                    ]
                                ),
                                write_enabled=self._learning is not None,
                                memory_profile_version=memory_profile_version,
                            )
                            stage = "episode_write"
                            next_memory_view = await _commit_episode(
                                record=record,
                                memory=self._memory,
                                episode=episode,
                                run_id=spec.run_id,
                                memory_view=run_state.memory_view,
                                write_enabled=self._learning is not None,
                            )
                            if (
                                episode is not None
                                and self._learning is not None
                                and self._learning.trigger == "after_closed_episode"
                            ):
                                stage = "learning"
                                learning_result = await self._learning.consolidate(
                                    trigger_run_id=spec.run_id,
                                    trigger_episode_id=episode.record_id,
                                    memory_view=next_memory_view,
                                    environment_id=profile.environment_id,
                                    environment_profile_version=memory_profile_version,
                                    registry=artifact.bundle.registry,
                                )
                                next_memory_view = learning_result.end_view
                            run_state = run_state.model_copy(
                                update={"memory_view": next_memory_view},
                                deep=True,
                            )
                            stage = failure_stage
                    raise

            stage = "environment_result"
            latest = run_state.environment_state.latest_observation
            completion = RunCompletion(
                steps=completed_steps,
                duration_seconds=max(0.0, self._monotonic() - run_started),
                stop_reason=stop_reason,
                forced=not latest.terminal,
            )
            port_started = self._monotonic()
            try:
                result = await self._environment.result(
                    run_state.environment_state,
                    completion,
                )
            finally:
                environment_duration += self._elapsed(port_started)
            if result is None:
                raise RuntimeError("environment did not produce a terminal run result")

            result = result.model_copy(
                update={"forced": completion.forced, "metrics": metrics()},
                deep=True,
            )
            stage = "run_store"
            try:
                await self._run_store.save_result(result)
            except Exception:
                store_failed = True
                raise
            manifest = _final_manifest(
                manifest,
                status=result.status,
                finished_at=self._utcnow(),
                metrics=result.metrics,
                memory_view=run_state.memory_view,
            )
            await save_manifest(manifest)
            operational_finished = True
            if self._learning is not None and self._learning.trigger == "after_run":
                await self._learning.consolidate(
                    trigger_run_id=spec.run_id,
                    trigger_episode_id=None,
                    memory_view=run_state.memory_view,
                    environment_id=profile.environment_id,
                    environment_profile_version=memory_profile_version,
                    registry=artifact.bundle.registry,
                )
            return result
        except Exception as error:
            if operational_finished:
                raise
            if isinstance(error, RunStoreFailure):
                store_failed = True
            if isinstance(error, ReasonerFailure):
                model_turns += error.telemetry.attempts
                model_duration += error.telemetry.duration_seconds
                token_usage = token_usage.plus(error.telemetry.token_usage)
                if error.telemetry.provider_instructions_sha256 is not None:
                    manifest = manifest.model_copy(
                        update={
                            "provider_instructions_sha256": (
                                error.telemetry.provider_instructions_sha256
                            )
                        },
                        deep=True,
                    )
            if store_failed:
                raise
            failure = _failure_record(stage, error)
            await record(
                "run_failed",
                RunFailedPayload(
                    run_id=spec.run_id,
                    failure_stage=cast(FailureStage, stage),
                    failure=failure,
                    last_durable_sequence=sequence,
                ),
            )
            manifest = _final_manifest(
                manifest,
                status="failed",
                finished_at=self._utcnow(),
                metrics=metrics(),
                failure=failure,
                memory_view=run_state.memory_view,
            )
            await save_manifest(manifest)
            raise RunExecutionError(spec.run_id, stage, error) from error

    async def _bootstrap(
        self,
        spec: RunSpec,
    ) -> tuple[EnvironmentBootstrapArtifact, bool, float]:
        duration = 0.0
        port_started = self._monotonic()
        capabilities = await self._environment.bootstrap_capabilities()
        duration += self._elapsed(port_started)
        if self._bootstrap_artifact is not None:
            artifact = self._bootstrap_artifact
            identity = self._bootstrapper.identity(
                source=artifact.source_text,
                environment_id=spec.environment,
                capabilities=capabilities,
            )
            if artifact.identity != identity:
                raise ValueError("strict bootstrap artifact does not match the environment")
            return artifact, True, duration

        port_started = self._monotonic()
        source = await self._environment.initialize()
        duration += self._elapsed(port_started)
        identity = self._bootstrapper.identity(
            source=source,
            environment_id=spec.environment,
            capabilities=capabilities,
        )
        cached = await self._run_store.load_bootstrap(identity)
        if cached is not None:
            return cached, True, duration
        artifact = await self._bootstrapper.build(
            source=source,
            environment_id=spec.environment,
            capabilities=capabilities,
        )
        if artifact.identity != identity:
            raise ValueError("bootstrapper returned an artifact with the wrong identity")
        await self._run_store.save_bootstrap(artifact)
        return artifact, False, duration

    def _elapsed(self, started: float) -> float:
        return max(0.0, self._monotonic() - started)

    def _safe_environment_telemetry(self, run_id: str) -> EnvironmentTelemetry:
        try:
            return self._environment.telemetry(run_id)
        except Exception:
            return EnvironmentTelemetry()


async def _record_closed_episode(
    *,
    record: RecordEvent,
    context: AgentContext,
    decision: SGRDecision,
    run_id: str,
    world_id: str | None,
    evidence_steps: list[str],
    evidence_refs: list[EvidenceRef],
    write_enabled: bool,
    memory_profile_version: str,
) -> EpisodeRecord | None:
    episode = project_closed_episode(
        open_decision=context.agent_working_state.open_decision,
        assessment=decision.envelope.previous_verification,
        environment_id=context.environment_state.profile.environment_id,
        environment_profile_version=memory_profile_version,
        evidence_group_id=evidence_group_id(
            environment_id=context.environment_state.profile.environment_id,
            environment_profile_version=memory_profile_version,
            world_id=world_id,
            run_id=run_id,
        ),
        run_id=run_id,
        situation_summary=(
            context.agent_working_state.open_decision.situation_summary
            if context.agent_working_state.open_decision is not None
            else context.environment_state.latest_observation.summary[:500]
        ),
        observation_summary=_episode_outcome_summary(
            evidence_steps,
            fallback=context.environment_state.latest_observation.summary,
        ),
        evidence_refs=evidence_refs,
    )
    if episode is not None:
        await record(
            "episode_closed",
            EpisodeClosedPayload(
                run_id=run_id,
                episode=episode,
                write_disposition=("commit_requested" if write_enabled else "skipped_read_only"),
            ),
        )
    return episode


async def _commit_episode(
    *,
    record: RecordEvent,
    memory: Memory,
    episode: EpisodeRecord | None,
    run_id: str,
    memory_view: MemoryView,
    write_enabled: bool,
) -> MemoryView:
    if episode is None or not write_enabled:
        return memory_view
    if memory_view.revision is None:
        raise ValueError("episode learning requires a revisioned memory view")
    commit = await memory.record_episode(episode, base_revision=memory_view.revision)
    if commit.new_revision is None:
        raise ValueError("learning memory returned an unrevisioned episode commit")
    await record(
        "episode_committed",
        EpisodeCommittedPayload(
            run_id=run_id,
            episode_id=episode.record_id,
            commit=commit,
        ),
    )
    return memory_view.model_copy(update={"revision": commit.new_revision})


def _advance_working_state(
    state: AgentWorkingState,
    decision: SGRDecision,
    *,
    step: int,
    episode: EpisodeRecord | None,
    situation_summary: str,
    observation: Observation,
    current_evidence_ref: EvidenceRef,
    prior_evidence_refs: list[EvidenceRef],
    prior_evidence_steps: list[str],
) -> tuple[AgentWorkingState, list[EvidenceRef], list[str]]:
    envelope = decision.envelope
    evidence_step = _episode_evidence_step(envelope.selected_action, observation)
    if state.open_decision is not None and envelope.previous_verification.status == "pending":
        return (
            state.model_copy(deep=True),
            _bounded_evidence_refs([*prior_evidence_refs, current_evidence_ref]),
            _bounded_evidence_steps([*prior_evidence_steps, evidence_step]),
        )

    last_closed = state.last_closed_episode
    if episode is not None:
        last_closed = ClosedEpisodeBridge(
            episode_id=episode.record_id,
            selected_action=episode.selected_action,
            observation_summary=episode.observation_summary,
            assessment=episode.verification,
        )
    if envelope.task_completed:
        return AgentWorkingState(last_closed_episode=last_closed), [], []

    same_strategy = state.active_strategy == envelope.strategy
    started_step = (
        state.strategy_started_step
        if same_strategy and state.strategy_started_step is not None
        else step
    )
    return (
        AgentWorkingState(
            active_strategy=envelope.strategy,
            strategy_started_step=started_step,
            open_decision=OpenDecision(
                step=step,
                phase=envelope.phase,
                strategy=envelope.strategy,
                situation_summary=situation_summary[:500],
                selected_action=envelope.selected_action,
                expected_result=envelope.expected_result,
                verification=envelope.verification,
                facts=envelope.facts,
            ),
            last_closed_episode=last_closed,
        ),
        [current_evidence_ref],
        [evidence_step],
    )


def _decision_situation_summary(context: AgentContext, decision: SGRDecision) -> str:
    facts = " | ".join(decision.envelope.facts)
    return (f"facts={facts}; observation={context.environment_state.latest_observation.summary}")[
        :500
    ]


def _episode_evidence_step(call: CapabilityCall, observation: Observation) -> str:
    arguments = json.dumps(
        call.arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    status = "ok" if observation.ok else "error"
    outcome = f"{call.name} [{status}] -> {observation.summary}"
    return f"{outcome}; arguments={arguments}"[:500]


def _episode_outcome_summary(evidence_steps: list[str], *, fallback: str) -> str:
    if not evidence_steps:
        return fallback[:900]
    latest = evidence_steps[-1][:900]
    earlier = " | ".join(evidence_steps[:-1])
    remaining = 900 - len(latest) - 3
    if not earlier or remaining <= 0:
        return latest
    return earlier[:remaining] + " | " + latest


def _within_step_limit(state: RunState) -> bool:
    return state.step_limit is None or state.step <= state.step_limit


def _bounded_evidence_refs(values: list[EvidenceRef]) -> list[EvidenceRef]:
    return values[-32:]


def _bounded_evidence_steps(values: list[str]) -> list[str]:
    if len(values) <= 4:
        return values
    return [values[0], *values[-3:]]


def _initial_manifest(
    spec: RunSpec,
    started_at: datetime,
    *,
    bootstrap_artifact: EnvironmentBootstrapArtifact,
    bootstrap_cache_hit: bool,
    memory_view: MemoryView,
) -> RunManifest:
    seed_value = spec.parameters.get("seed")
    seed = seed_value if isinstance(seed_value, int) and not isinstance(seed_value, bool) else None
    reasons = list(spec.metadata.provenance_reasons)
    if spec.metadata.git_revision is None and "git revision unavailable" not in reasons:
        reasons.append("git revision unavailable")
    if spec.metadata.git_dirty is None and "git dirty status unavailable" not in reasons:
        reasons.append("git dirty status unavailable")
    if spec.metadata.git_dirty:
        reasons.append("working tree under af/ is dirty")
    if spec.metadata.reasoner.provider == "unconfigured":
        reasons.append("reasoner metadata unavailable")
    if spec.metadata.experiment_id is not None:
        if (
            spec.metadata.resolved_spec_sha256 is None
            and "resolved experiment spec hash unavailable" not in reasons
        ):
            reasons.append("resolved experiment spec hash unavailable")
        if spec.metadata.uv_lock_sha256 is None and "uv.lock hash unavailable" not in reasons:
            reasons.append("uv.lock hash unavailable")
        if spec.metadata.python_version is None and "Python version unavailable" not in reasons:
            reasons.append("Python version unavailable")
        if (
            spec.metadata.simulator_endpoint_sha256 is None
            and "simulator endpoint identity unavailable" not in reasons
        ):
            reasons.append("simulator endpoint identity unavailable")
    return RunManifest(
        run_id=spec.run_id,
        seed=seed,
        agent_id=spec.agent_id,
        agent_version=spec.agent_version,
        reasoner=spec.metadata.reasoner,
        git_revision=spec.metadata.git_revision,
        git_dirty=spec.metadata.git_dirty,
        experiment_id=spec.metadata.experiment_id,
        repeat_index=spec.metadata.repeat_index,
        resolved_spec_sha256=spec.metadata.resolved_spec_sha256,
        uv_lock_sha256=spec.metadata.uv_lock_sha256,
        python_version=spec.metadata.python_version,
        simulator_endpoint_sha256=spec.metadata.simulator_endpoint_sha256,
        agent_config_source=spec.metadata.agent_config_source,
        agent_config_sha256=spec.metadata.agent_config_sha256,
        operator_guidance=spec.metadata.operator_guidance,
        environment_profile_version=bootstrap_artifact.bundle.profile.profile_version,
        bootstrap_cache_hit=bootstrap_cache_hit,
        bootstrap_reasoner=bootstrap_artifact.reasoner_telemetry,
        memory_mode=spec.metadata.memory_mode,
        carry_memory=spec.metadata.carry_memory,
        memory_database_id=memory_view.database_id,
        memory_start_revision=memory_view.revision,
        environment=spec.environment,
        started_at=started_at,
        reproducible=not reasons,
        non_reproducible_reasons=reasons,
        baseline_profile=(
            "no-memory-v1"
            if spec.metadata.memory_mode == "none" and not spec.metadata.carry_memory
            else None
        ),
    )


def _final_manifest(
    manifest: RunManifest,
    *,
    status: str,
    finished_at: datetime,
    metrics: RunMetrics,
    memory_view: MemoryView,
    failure: FailureRecord | None = None,
) -> RunManifest:
    reasons = list(manifest.non_reproducible_reasons)
    if manifest.system_prompt_sha256 is None:
        reasons.append("decision prompt was not prepared")
    if manifest.schema_sha256 is None:
        reasons.append("decision schema was not prepared")
    return manifest.model_copy(
        update={
            "finished_at": finished_at,
            "status": status,
            "failure_stage": failure.stage if failure else None,
            "failure_type": failure.error_type if failure else None,
            "failure_message": failure.message if failure else None,
            "reproducible": not reasons,
            "non_reproducible_reasons": reasons,
            "metrics": metrics,
            "memory_end_revision": memory_view.revision,
        },
        deep=True,
    )


def _failure_record(stage: str, error: Exception) -> FailureRecord:
    telemetry = error.telemetry if isinstance(error, ReasonerFailure) else None
    category = error.category if isinstance(error, ReasonerFailure) else None
    return FailureRecord(
        stage=stage,
        error_type=type(error).__name__,
        message=str(error),
        category=category,
        telemetry=telemetry,
    )
