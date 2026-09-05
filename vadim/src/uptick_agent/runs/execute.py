from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from uptick_agent.decisions.actions import (
    AgentAction,
    ApplyFix,
    GetOperation,
    ScaleBackend,
    StartDeployment,
)
from uptick_agent.decisions.contracts import DecisionContext, RecentStep, RunState, ToolResult
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
from uptick_agent.runs.results import RunResult, StepRecord
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler


def _memory_text(result: ToolResult, *, limit: int = 6_000) -> str:
    payload = json.dumps(
        sanitize_json(result.model_dump(mode="json")),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(payload) <= limit:
        return payload
    return payload[:limit] + f"\n...[{len(payload) - limit} characters omitted]"


def _record_run_state(run_state: RunState, action: AgentAction, result: ToolResult) -> None:
    for link in result.operation_links:
        if link.relation == "initiated" and result.ok:
            run_state.operation_statuses[link.operation_id] = "accepted"
        elif link.relation == "observed":
            status = result.data.get("status")
            if isinstance(status, str):
                run_state.operation_statuses[link.operation_id] = status

    if not result.ok:
        return

    if isinstance(action, ApplyFix) and result.data.get("applied") is True:
        if action.message not in run_state.applied_fix_messages:
            run_state.applied_fix_messages.append(action.message)
        return

    operation_id = result.data.get("operation_id")
    if isinstance(action, ScaleBackend) and isinstance(operation_id, str):
        run_state.desired_backend_instances = action.desired_instances
        run_state.operation_statuses[operation_id] = "accepted"
        return

    if isinstance(action, StartDeployment) and isinstance(operation_id, str):
        if action.deployment_id not in run_state.started_deployment_ids:
            run_state.started_deployment_ids.append(action.deployment_id)
        run_state.operation_statuses[operation_id] = "accepted"
        return

    if isinstance(action, GetOperation):
        status = result.data.get("status")
        resolved_operation_id = result.data.get("operation_id", action.operation_id)
        if isinstance(resolved_operation_id, str) and isinstance(status, str):
            run_state.operation_statuses[resolved_operation_id] = status


def _prompt_trace(
    model: DecisionModel,
    context: DecisionContext,
) -> tuple[str, dict]:
    builder = getattr(model, "prompt_trace", None)
    if not callable(builder):
        return "decision-context-surrogate", {"decision_context": context.model_dump(mode="json")}
    trace = builder(context)
    if not isinstance(trace, dict):
        raise TypeError("decision model prompt_trace must return a JSON object")
    return "provider-neutral-structured-generation-request", trace


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
    ) -> None:
        self.config = config
        self.model = model
        self.memory = memory
        self.environment = environment
        self.observer = observer or NullObserver()
        self.transition_assembler = transition_assembler or DefaultExperienceTransitionAssembler()

    async def run(self, seed: int) -> RunResult:
        run_started = monotonic()
        session, latest = await self.environment.start(
            seed=seed,
            agent_id=self.config.agent_id,
            agent_version=self.config.agent_version,
        )
        try:
            await self._remember(session.run_id, latest, kind="observation", importance=0.7)

            stop_reason = "maximum step limit reached"
            completed_steps = 0
            recent_steps: deque[RecentStep] = deque(maxlen=6)
            run_state = RunState()
            for iteration in range(1, self.config.max_steps + 1):
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
                context = DecisionContext(
                    objective=self.config.objective,
                    run_id=session.run_id,
                    decision_id=decision_id,
                    seed=seed,
                    iteration=iteration,
                    max_steps=self.config.max_steps,
                    latest_result=latest,
                    memory_context=memory_context,
                    recent_steps=list(recent_steps),
                    run_state=run_state.model_copy(deep=True),
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
                decision = await self.model.decide(context)
                await self.memory.record_trace(
                    AuditTraceWrite(
                        event_id=audit_event_id(
                            "decision.selected",
                            session.run_id,
                            request_id,
                            decision_id,
                        ),
                        event_type="decision.selected",
                        run_id=session.run_id,
                        sequence=(iteration - 1) * 100 + 30,
                        iteration=iteration,
                        request_id=request_id,
                        decision_id=decision_id,
                        outcome_correlation_id=outcome_correlation_id,
                        producer_id="agent-runner",
                        producer_version="1.0",
                        metadata={
                            "action_kind": decision.action.kind,
                            "action": decision.action.model_dump(mode="json"),
                        },
                        raw_bodies={
                            "decision_traces": {"decision": decision.model_dump(mode="json")}
                        },
                    )
                )
                result = await self.environment.execute(session, decision.action)
                duration = monotonic() - step_started
                completed_steps = iteration
                transition = self.transition_assembler.assemble(
                    TransitionAssemblyRequest(
                        transition_id=hashlib.sha256(
                            f"experience-transition:{session.run_id}:{iteration}".encode()
                        ).hexdigest(),
                        run_id=session.run_id,
                        iteration=iteration,
                        occurred_at=datetime.now(UTC),
                        environment_id=getattr(session, "environment_id", None),
                        scenario_id=getattr(session, "scenario_id", None),
                        trust_classification="external_untrusted",
                        pre_state=run_state.model_dump(mode="json"),
                        observation=latest.model_dump(mode="json"),
                        action=decision.action.model_dump(mode="json"),
                        result=result.model_dump(mode="json"),
                        before_objective_metrics=latest.objective_metrics,
                        after_objective_metrics=result.objective_metrics,
                        operation_links=result.operation_links,
                        terminal=result.terminal,
                    )
                )
                await self.memory.record_transition(transition)
                memory_diagnostics = self.memory.context_diagnostics
                await self.memory.record_trace(
                    AuditTraceWrite(
                        event_id=audit_event_id(
                            "decision.completed",
                            session.run_id,
                            request_id,
                            decision_id,
                            transition.transition_id,
                            outcome_correlation_id,
                        ),
                        event_type="decision.completed",
                        run_id=session.run_id,
                        sequence=(iteration - 1) * 100 + 90,
                        iteration=iteration,
                        request_id=request_id,
                        decision_id=decision_id,
                        transition_id=transition.transition_id,
                        outcome_correlation_id=outcome_correlation_id,
                        producer_id="agent-runner",
                        producer_version="1.0",
                        metadata={
                            "prompt_included_item_ids": [
                                item.envelope.item_id for item in memory_context.items
                            ],
                            "action_kind": result.action_kind,
                            "ok": result.ok,
                            "terminal": result.terminal,
                            "objective_metrics": [
                                item.model_dump(mode="json") for item in result.objective_metrics
                            ],
                            "operation_links": [
                                item.model_dump(mode="json") for item in result.operation_links
                            ],
                        },
                        raw_bodies={
                            "observations": {"action_result": result.model_dump(mode="json")},
                            "decision_traces": {
                                "memory": memory_diagnostics,
                                "decision": decision.model_dump(mode="json"),
                                "result_metadata": {
                                    "action_kind": result.action_kind,
                                    "ok": result.ok,
                                    "terminal": result.terminal,
                                    "objective_metrics": [
                                        item.model_dump(mode="json")
                                        for item in result.objective_metrics
                                    ],
                                    "operation_links": [
                                        item.model_dump(mode="json")
                                        for item in result.operation_links
                                    ],
                                },
                                "transition_id": transition.transition_id,
                            },
                        },
                    )
                )
                record = StepRecord(
                    run_id=session.run_id,
                    decision_id=decision_id,
                    transition_id=transition.transition_id,
                    iteration=iteration,
                    decision=decision,
                    result=result,
                    memory_diagnostics=memory_diagnostics,
                    started_at=datetime.now(UTC),
                    duration_seconds=duration,
                )
                await self._remember(
                    session.run_id,
                    result,
                    kind="experience",
                    importance=0.85 if not result.ok or result.terminal else 0.5,
                    metadata={
                        "iteration": iteration,
                        "decision": decision.model_dump(mode="json"),
                    },
                )
                await self.observer.on_step(record)
                _record_run_state(run_state, decision.action, result)
                recent_steps.append(
                    RecentStep(
                        iteration=iteration,
                        action=decision.action,
                        result_action_kind=result.action_kind,
                        result_ok=result.ok,
                        result_summary=result.summary[:2_000],
                        result_terminal=result.terminal,
                    )
                )
                latest = result
                if result.terminal:
                    stop_reason = result.summary
                    break

            final = await self.environment.finish(
                session,
                steps=completed_steps,
                duration_seconds=monotonic() - run_started,
                stop_reason=stop_reason,
            )
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
        if final.objective_kind == "uptime_cost":
            outcome_summary = (
                f"Run finished with status={final.status}, uptime_ratio={final.uptime_ratio}, "
                f"slo_passed={final.slo_passed}, total_cost_minor={final.total_cost_minor}, "
                f"steps={final.steps}."
            )
        else:
            outcome_summary = (
                f"Run finished with status={final.status}, balance={final.balance_minor}, "
                f"lost_revenue={final.lost_revenue_minor}, steps={final.steps}."
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
