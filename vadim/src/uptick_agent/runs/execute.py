from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel

from uptick_agent.decisions.runtime import (
    RuntimeDecisionContext,
    RuntimeRecentStep,
    ToolResult,
    serialize_previous_decision,
)
from uptick_agent.environment.contracts import (
    EnvironmentDecisionSpec,
    decision_actions,
    public_state_payload,
    validate_decision,
)
from uptick_agent.memory.audit_contracts import AuditTraceWrite, audit_event_id
from uptick_agent.memory.compatibility.contracts import MemoryEntry
from uptick_agent.memory.contracts import (
    ExperienceTransitionAssembler,
    MemoryContextRequest,
    RunOutcome,
    TransitionAssemblyRequest,
)
from uptick_agent.observers import NullObserver
from uptick_agent.ports import AgentMemory, DecisionModel, Environment, RunObserver
from uptick_agent.redaction import sanitize_json
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.handoff import (
    ObservationHandoffPort,
    ObservationHandoffStartup,
    ObservationReadRequest,
)
from uptick_agent.runs.observations import ObservationHistory
from uptick_agent.runs.runtime_results import RuntimeRunResult, RuntimeStepRecord
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

_DEFAULT_RUNTIME_OBJECTIVE = "Follow the objective in the environment startup instructions."


def _memory_text(result: ToolResult, *, limit: int = 6_000) -> str:
    payload = json.dumps(
        sanitize_json(result.model_dump(mode="json")),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(payload) <= limit:
        return payload
    return payload[:limit] + f"\n...[{len(payload) - limit} characters omitted]"


def _prompt_trace(
    model: DecisionModel,
    context: RuntimeDecisionContext,
) -> tuple[str, dict]:
    builder = getattr(model, "prompt_trace", None)
    if not callable(builder):
        return "decision-context-surrogate", {"decision_context": context.model_dump(mode="json")}
    trace = builder(context)
    if not isinstance(trace, dict):
        raise TypeError("decision model prompt_trace must return a JSON object")
    return "provider-neutral-structured-generation-request", trace


def _decision_spec(environment: Environment) -> EnvironmentDecisionSpec:
    spec = getattr(environment, "decision_spec", None)
    if not isinstance(spec, EnvironmentDecisionSpec):
        raise TypeError("environment must provide an EnvironmentDecisionSpec")
    return spec


def _environment_state(environment: Environment, session: object) -> object:
    provider = getattr(environment, "public_state", None)
    if not callable(provider):
        return {}
    state = provider(session)
    return copy.deepcopy(public_state_payload(state))


class AgentRunner:
    """Small orchestration core: context -> decision -> action -> transition."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        model: DecisionModel,
        memory: AgentMemory,
        environment: Environment,
        observer: RunObserver | None = None,
        transition_assembler: ExperienceTransitionAssembler | None = None,
        observation_complete_record_bytes: int | None = None,
        max_actions: int | None = None,
        observation_handoff: ObservationHandoffPort | None = None,
        observation_read_action: Callable[[BaseModel], ObservationReadRequest | None] | None = None,
        observation_handoff_startup: ObservationHandoffStartup | None = None,
    ) -> None:
        configured_max_actions = (
            max_actions if max_actions is not None else getattr(config, "max_actions", None)
        )
        if configured_max_actions is not None and (
            isinstance(configured_max_actions, bool)
            or not isinstance(configured_max_actions, int)
            or configured_max_actions < 1
        ):
            raise ValueError("max_actions must be a positive integer")
        if (observation_handoff is None) != (observation_read_action is None):
            raise ValueError("handoff requires an explicit environment-owned read action mapper")
        if observation_handoff is not None and configured_max_actions is None:
            raise ValueError("handoff requires an explicit max_actions budget")
        if observation_read_action is not None and not callable(observation_read_action):
            raise TypeError("observation_read_action must be callable")
        if observation_handoff_startup is not None and observation_handoff is None:
            raise ValueError("handoff startup requires an observation handoff")
        if observation_handoff_startup is not None and not callable(
            getattr(observation_handoff_startup, "initialize_new", None)
        ):
            raise TypeError("observation_handoff_startup must provide initialize_new")
        self._observation_handoff = observation_handoff
        self._observation_read_action = observation_read_action
        self._observation_handoff_startup = observation_handoff_startup
        self.config = config
        self.model = model
        self.memory = memory
        self.environment = environment
        self.observer = observer or NullObserver()
        self.transition_assembler = transition_assembler or DefaultExperienceTransitionAssembler()
        self._observation_complete_record_bytes = observation_complete_record_bytes
        self._max_actions = configured_max_actions

    def _can_continue_batch(self, session: object, result: ToolResult) -> bool:
        """Ask an adapter whether the next explicit action is safe to run.

        The hook is intentionally a narrow environment-owned barrier.  A
        missing, malformed, or failing hook stops the current batch rather
        than guessing that a pending operation is complete.  ``ok=False`` and
        terminal results are barriers even when an adapter publishes a hook.
        """

        if not result.ok or result.terminal:
            return False
        checker = getattr(self.environment, "can_continue_batch", None)
        if not callable(checker):
            return False
        try:
            return checker(session, copy.deepcopy(result)) is True
        except Exception:
            return False

    async def _execute_action(
        self,
        *,
        session: object,
        iteration: int,
        request_id: str,
        decision_id: str,
        outcome_correlation_id: str,
        decision: BaseModel,
        action: BaseModel,
        latest: ToolResult,
        run_state: object,
        memory_context: object,
        observation_history: ObservationHistory,
        action_index: int,
        batch_mode: bool,
        decision_started: float,
        memory_read_request: ObservationReadRequest | None = None,
    ) -> tuple[ToolResult, RuntimeStepRecord]:
        """Execute one selected action through the existing audit/transition path."""

        action_started = decision_started if action_index == 0 else monotonic()
        action_payload = copy.deepcopy(
            action.model_dump(mode="json", round_trip=True, warnings="error")
        )
        execution_action = copy.deepcopy(action)
        selected_identity: tuple[object, ...] = (
            "decision.selected",
            session.run_id,
            request_id,
            decision_id,
        )
        if batch_mode:
            selected_identity += (action_index,)
        selected_metadata = {
            "action_kind": getattr(action, "kind", type(action).__name__),
            "action": action_payload,
        }
        if batch_mode:
            selected_metadata["action_index"] = action_index
        await self.memory.record_trace(
            AuditTraceWrite(
                event_id=audit_event_id(*selected_identity),
                event_type="decision.selected",
                run_id=session.run_id,
                sequence=(iteration - 1) * 100 + 30 + (action_index if batch_mode else 0),
                iteration=iteration,
                request_id=request_id,
                decision_id=decision_id,
                outcome_correlation_id=outcome_correlation_id,
                producer_id="agent-runner",
                producer_version="1.0",
                metadata=selected_metadata,
                raw_bodies={"decision_traces": {"decision": decision.model_dump(mode="json")}},
            )
        )

        is_memory_read = memory_read_request is not None
        if is_memory_read:
            assert self._observation_handoff is not None
            result = await self._observation_handoff.read(
                memory_read_request, current_iteration=iteration
            )
        else:
            result = copy.deepcopy(await self.environment.execute(session, execution_action))
            observation_history.record(iteration, action_payload, result)
        duration = monotonic() - action_started
        transition_id = hashlib.sha256(
            (
                f"experience-transition:{session.run_id}:{iteration}"
                + (f":action:{action_index}" if batch_mode else "")
            ).encode()
        ).hexdigest()
        if not is_memory_read:
            transition = self.transition_assembler.assemble(
                TransitionAssemblyRequest(
                    transition_id=transition_id,
                    run_id=session.run_id,
                    iteration=iteration,
                    occurred_at=datetime.now(UTC),
                    environment_id=getattr(session, "environment_id", None),
                    scenario_id=getattr(session, "scenario_id", None),
                    trust_classification="external_untrusted",
                    pre_state=public_state_payload(run_state),
                    observation=latest.model_dump(mode="json"),
                    action=action_payload,
                    result=result.model_dump(mode="json"),
                    before_objective_metrics=latest.objective_metrics,
                    after_objective_metrics=result.objective_metrics,
                    operation_links=result.operation_links,
                    terminal=result.terminal,
                )
            )
            await self.memory.record_transition(transition)
            if self._observation_handoff is not None:
                bookmark = await self._observation_handoff.note_transition(
                    transition_id, current_iteration=iteration + 1
                )
                record_id = getattr(bookmark, "record_id", None)
                if isinstance(record_id, str):
                    observation_history.attach_exact_read_reference(
                        iteration, action_payload, record_id
                    )
        memory_diagnostics = self.memory.context_diagnostics
        completed_metadata = {
            "prompt_included_item_ids": [item.envelope.item_id for item in memory_context.items],
            "action_kind": result.action_kind,
            "ok": result.ok,
            "terminal": result.terminal,
            "objective_metrics": [
                item.model_dump(mode="json") for item in result.objective_metrics
            ],
            "operation_links": [item.model_dump(mode="json") for item in result.operation_links],
        }
        completed_result_metadata = {
            "action_kind": result.action_kind,
            "ok": result.ok,
            "terminal": result.terminal,
            "objective_metrics": [
                item.model_dump(mode="json") for item in result.objective_metrics
            ],
            "operation_links": [item.model_dump(mode="json") for item in result.operation_links],
        }
        if batch_mode:
            completed_metadata["action_index"] = action_index
        completed_event = (
            "decision.memory_read_completed" if is_memory_read else "decision.completed"
        )
        await self.memory.record_trace(
            AuditTraceWrite(
                event_id=audit_event_id(
                    completed_event,
                    session.run_id,
                    request_id,
                    decision_id,
                    transition_id,
                    outcome_correlation_id,
                ),
                event_type=completed_event,
                run_id=session.run_id,
                sequence=(iteration - 1) * 100 + 90 + (action_index if batch_mode else 0),
                iteration=iteration,
                request_id=request_id,
                decision_id=decision_id,
                transition_id=None if is_memory_read else transition_id,
                outcome_correlation_id=outcome_correlation_id,
                producer_id="agent-runner",
                producer_version="1.0",
                metadata=completed_metadata,
                raw_bodies={
                    "observations": {"action_result": result.model_dump(mode="json")},
                    "decision_traces": {
                        "memory": memory_diagnostics,
                        "decision": decision.model_dump(mode="json"),
                        "result_metadata": completed_result_metadata,
                        "transition_id": None if is_memory_read else transition_id,
                    },
                },
            )
        )
        record = RuntimeStepRecord(
            run_id=session.run_id,
            decision_id=decision_id,
            transition_id=None if is_memory_read else transition_id,
            iteration=iteration,
            decision=copy.deepcopy(decision),
            result=copy.deepcopy(result),
            memory_diagnostics=memory_diagnostics,
            started_at=datetime.now(UTC),
            duration_seconds=duration,
            action_index=action_index if batch_mode else None,
            action=action_payload if batch_mode else None,
        )
        experience_metadata = {
            "iteration": iteration,
            "decision": decision.model_dump(mode="json"),
        }
        if batch_mode:
            experience_metadata["action_index"] = action_index
        if not is_memory_read:
            await self._remember(
                session.run_id,
                result,
                kind="experience",
                importance=0.85 if not result.ok or result.terminal else 0.5,
                metadata=experience_metadata,
            )
        await self.observer.on_step(record)
        return result, record

    async def run(self, seed: int) -> RuntimeRunResult:
        # Validate before starting the environment and create fresh run-local storage.
        observation_history = ObservationHistory(
            max_complete_record_bytes=self._observation_complete_record_bytes
        )
        run_started = monotonic()
        session, latest = await self.environment.start(
            seed=seed,
            agent_id=self.config.agent_id,
            agent_version=self.config.agent_version,
        )
        try:
            spec = _decision_spec(self.environment)
            if self._observation_handoff is not None:
                if self._observation_handoff_startup is None:
                    self._observation_handoff.begin(session.run_id)
                else:
                    await self._observation_handoff_startup.initialize_new(
                        session=session,
                        initial_result=latest,
                    )
            memory_read_result = None
            objective = spec.objective or _DEFAULT_RUNTIME_OBJECTIVE
            await self._remember(session.run_id, latest, kind="observation", importance=0.7)

            stop_reason = "maximum step limit reached"
            decision_count = 0
            action_count = 0
            last_decision_was_batch = False
            batch_used = False
            recent_steps: deque[RuntimeRecentStep] = deque(maxlen=6)
            previous_decision: str | None = None
            run_state = _environment_state(self.environment, session)
            for iteration in range(1, self.config.max_steps + 1):
                if self._max_actions is not None and action_count >= self._max_actions:
                    stop_reason = "action budget exhausted"
                    break
                decision_count = iteration
                request_id = hashlib.sha256(
                    f"memory-context:{session.run_id}:{iteration}".encode()
                ).hexdigest()
                decision_id = hashlib.sha256(
                    f"decision:{session.run_id}:{iteration}".encode()
                ).hexdigest()
                outcome_correlation_id = audit_event_id("run.outcome", session.run_id)
                memory_context = await self.memory.build_context(
                    MemoryContextRequest(
                        request_id=request_id,
                        run_id=session.run_id,
                        query=latest.summary[:11_000] + " " + _memory_text(latest, limit=4_000),
                        context={
                            "iteration": iteration,
                            "latest_result": latest.model_dump(mode="json"),
                        },
                        max_items=self.config.memory_recall_limit,
                    )
                )
                context = RuntimeDecisionContext(
                    objective=objective,
                    run_id=session.run_id,
                    decision_id=decision_id,
                    seed=seed,
                    iteration=iteration,
                    max_steps=self.config.max_steps,
                    max_actions=self._max_actions,
                    actions_executed=(action_count if self._max_actions is not None else None),
                    latest_result=latest,
                    observation_bookmarks=(
                        self._observation_handoff.snapshot(current_iteration=iteration)
                        if self._observation_handoff is not None
                        else None
                    ),
                    memory_read_result=memory_read_result,
                    previous_decision=previous_decision,
                    memory_context=memory_context,
                    recent_steps=list(recent_steps),
                    observation_history=observation_history.snapshot(
                        exclude_iteration=None if last_decision_was_batch else iteration - 1
                    ),
                    run_state=copy.deepcopy(run_state),
                )
                prompt_kind, prompt_body = _prompt_trace(self.model, context)
                await self.memory.record_trace(
                    AuditTraceWrite(
                        event_id=audit_event_id(
                            "decision.input",
                            session.run_id,
                            request_id,
                            decision_id,
                        ),
                        event_type="decision.input",
                        run_id=session.run_id,
                        sequence=(iteration - 1) * 100 + 20,
                        iteration=iteration,
                        request_id=request_id,
                        decision_id=decision_id,
                        outcome_correlation_id=outcome_correlation_id,
                        producer_id="agent-runner",
                        producer_version="1.0",
                        metadata={"prompt_kind": prompt_kind},
                        raw_bodies={
                            "prompts": prompt_body,
                            "observations": {"latest_result": latest.model_dump(mode="json")},
                        },
                    )
                )
                step_started = monotonic()
                decision = validate_decision(spec, await self.model.decide(context))
                # Copy the validated output before the environment or observer
                # can mutate the model instance passed across their boundaries.
                previous_decision = serialize_previous_decision(decision)
                batch_mode = hasattr(decision, "actions")
                batch_used = batch_used or batch_mode
                actions = decision_actions(decision)
                if batch_mode and self._max_actions is None:
                    raise ValueError(
                        "a decision with actions requires an explicit max_actions budget"
                    )
                budget_exhausted = False
                for action_index, action in enumerate(actions):
                    if self._max_actions is not None and action_count >= self._max_actions:
                        stop_reason = "action budget exhausted"
                        budget_exhausted = True
                        break
                    read_request = (
                        self._observation_read_action(copy.deepcopy(action))
                        if self._observation_read_action is not None
                        else None
                    )
                    if read_request is not None and not isinstance(
                        read_request, ObservationReadRequest
                    ):
                        raise TypeError("read action mapper returned an invalid request")
                    result, _record = await self._execute_action(
                        session=session,
                        iteration=iteration,
                        request_id=request_id,
                        decision_id=decision_id,
                        outcome_correlation_id=outcome_correlation_id,
                        decision=decision,
                        action=action,
                        latest=latest,
                        run_state=run_state,
                        memory_context=memory_context,
                        observation_history=observation_history,
                        action_index=action_index,
                        batch_mode=batch_mode,
                        decision_started=step_started,
                        memory_read_request=read_request,
                    )
                    action_count += 1
                    run_state = _environment_state(self.environment, session)
                    recent_steps.append(
                        RuntimeRecentStep(
                            iteration=iteration,
                            action=copy.deepcopy(action),
                            result_action_kind=result.action_kind,
                            result_ok=result.ok,
                            result_summary=result.summary[:2_000],
                            result_terminal=result.terminal,
                        )
                    )
                    if read_request is None:
                        latest = result
                        memory_read_result = None
                    else:
                        memory_read_result = copy.deepcopy(result)
                    if result.terminal:
                        stop_reason = result.summary
                        break
                    if self._max_actions is not None and action_count >= self._max_actions:
                        stop_reason = "action budget exhausted"
                        budget_exhausted = True
                        break
                    # Expose one bounded historical read to the next decision;
                    # it cannot authorize further batch actions on fresh state.
                    if read_request is not None:
                        break
                    if (
                        batch_mode
                        and action_index + 1 < len(actions)
                        and not self._can_continue_batch(session, result)
                    ):
                        break
                last_decision_was_batch = batch_mode
                if budget_exhausted:
                    break
                if result.terminal:
                    break

            final = await self.environment.finish(
                session,
                steps=decision_count,
                duration_seconds=monotonic() - run_started,
                stop_reason=stop_reason,
            )
            if batch_used or self._max_actions is not None:
                final = final.model_copy(update={"action_count": action_count})
        except asyncio.CancelledError as error:
            await self._record_failed_outcome(session.run_id, "interrupted", error)
            raise
        except Exception as error:
            await self._record_failed_outcome(session.run_id, "failed", error)
            raise

        outcome_status = (
            final.status
            if final.status in {"completed", "failed", "interrupted", "excluded"}
            else "interrupted"
            if final.status == "running"
            else "failed"
        )
        metrics = [(item.name, item.value, item.unit) for item in final.objective_metrics]
        outcome_summary = (
            f"Run finished with status={final.status}, "
            f"objective_metrics={metrics}, "
            f"steps={final.steps}."
        )
        outcome_evidence_error: BaseException | None = None
        try:
            await self._remember(
                session.run_id,
                ToolResult(
                    action_kind="run_outcome",
                    summary=outcome_summary,
                    data=final.model_dump(mode="json"),
                    terminal=True,
                ),
                kind="outcome",
                importance=1.0,
                tags={"run-outcome"},
            )
        except BaseException as error:
            # The world outcome is already known; still attempt typed
            # finalization so structured memory can retain it independently.
            outcome_evidence_error = error

        finalization_error: BaseException | None = None
        try:
            await self.memory.finalize_run(
                RunOutcome(
                    run_id=session.run_id,
                    status=outcome_status,
                    stop_reason=final.stop_reason[:2_000] or "run finished without a stop reason",
                    objective_metrics=final.objective_metrics,
                )
            )
        except BaseException as error:
            finalization_error = error

        if outcome_evidence_error is not None:
            if finalization_error is not None:
                outcome_evidence_error.add_note(
                    f"Memory finalization also failed with {type(finalization_error).__name__}."
                )
            raise outcome_evidence_error
        if finalization_error is not None:
            raise finalization_error
        await self.observer.on_finish(final)
        return final

    async def _record_failed_outcome(
        self,
        run_id: str,
        status: str,
        error: BaseException,
    ) -> None:
        reason = f"Run {status} by {type(error).__name__}."
        try:
            await self._remember(
                run_id,
                ToolResult(
                    action_kind="run_outcome",
                    summary=reason,
                    data={"status": status, "error_type": type(error).__name__},
                    terminal=True,
                ),
                kind="outcome",
                importance=1.0,
                tags={"run-outcome"},
            )
        except BaseException as evidence_error:
            error.add_note(
                f"Memory outcome evidence also failed with {type(evidence_error).__name__}."
            )

        try:
            await self.memory.finalize_run(
                RunOutcome(run_id=run_id, status=status, stop_reason=reason)
            )
        except BaseException as finalization_error:
            error.add_note(
                f"Memory finalization also failed with {type(finalization_error).__name__}."
            )

    async def _remember(
        self,
        run_id: str,
        result: ToolResult,
        *,
        kind: str,
        importance: float,
        tags: set[str] | None = None,
        metadata: dict | None = None,
    ) -> None:
        await self.memory.remember(
            MemoryEntry(
                id=uuid4().hex,
                run_id=run_id,
                kind=kind,
                content=_memory_text(result),
                importance=importance,
                tags=(tags or set()) | {result.action_kind},
                metadata=metadata or {},
            )
        )
