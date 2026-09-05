"""Ordered evaluation matrix execution use case."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal, TypeVar

from uptick_agent.evaluation.contracts import (
    FrozenEvaluationBinding,
    V2AttemptRecord,
    V2Condition,
    V2Manifest,
    V2Report,
    V2RunMatrixBlock,
    aggregate_report,
    sha256_json,
)
from uptick_agent.evaluation.lifecycle import EvaluationJournal
from uptick_agent.evaluation.ports import (
    EvaluationBindingFactory,
    EvaluationConfigFactory,
    EvaluationEnvironmentFactory,
    EvaluationMemoryFactory,
    EvaluationModelFactory,
)
from uptick_agent.evaluation.runtime_adapters import (
    _FinalizationError,
    _MemoryAdapter,
    _PrestartedEnvironment,
    _TelemetryModelAdapter,
    _TraceObserver,
)
from uptick_agent.evaluation.telemetry import (
    _memory_telemetry,
    _outcome,
    _provider_telemetry,
    _trace_payload,
    _try_trace_artifact,
)
from uptick_agent.ports import DecisionModel, Environment
from uptick_agent.redaction import redact_text
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.execute import AgentRunner

T = TypeVar("T")
_STORED_ARTIFACT_COUNT_TIMEOUT_SECONDS = 1.0


def _stable_run_identifier(manifest_hash: str, *, block_id: str, condition_id: str) -> str:
    """Keep physical IDs bounded even when user-facing profile IDs are long."""

    digest = sha256_json(
        {"manifest_hash": manifest_hash, "block_id": block_id, "condition_id": condition_id}
    )
    return f"run:{manifest_hash[:16]}:{digest[:48]}"

class EvaluationRuntime:
    """Execute the ordered v2 matrix and return a verified exploratory report."""

    def __init__(
        self,
        manifest: V2Manifest,
        *,
        environment_factory: EvaluationEnvironmentFactory,
        model_factory: EvaluationModelFactory,
        memory_factory: EvaluationMemoryFactory | None = None,
        config_factory: EvaluationConfigFactory | None = None,
        binding_factory: EvaluationBindingFactory | None = None,
        runner_factory: Callable[..., AgentRunner] | None = None,
        journal: EvaluationJournal | None = None,
    ) -> None:
        self.manifest = V2Manifest.model_validate(manifest.model_dump(mode="json"))
        self.environment_factory = environment_factory
        self.model_factory = model_factory
        if memory_factory is None:
            raise ValueError(
                "evaluation execution requires an explicit memory factory; "
                "use the compatibility facade or wire composition at the application boundary"
            )
        self.memory_factory = memory_factory
        self.config_factory = config_factory or self._default_config
        if binding_factory is not None:
            self.binding_factory = binding_factory
        else:
            self.binding_factory = self._missing_binding
        self.runner_factory = runner_factory or AgentRunner
        self.journal = journal or EvaluationJournal(self.manifest)
        if self.journal.manifest.manifest_hash != self.manifest.manifest_hash:
            raise ValueError("journal does not belong to the sealed manifest")
        self.bindings: list[FrozenEvaluationBinding] = []

    async def _missing_binding(
        self, condition: V2Condition, training_attempts: tuple[V2AttemptRecord, ...]
    ) -> FrozenEvaluationBinding:
        raise ValueError("custom memory factories require an explicit binding factory")

    def _default_config(
        self, block: V2RunMatrixBlock, condition: V2Condition, attempt: V2AttemptRecord
    ) -> AgentConfig:
        return AgentConfig(
            agent_id="uptick-v2-evaluation",
            agent_version=self.manifest.profile.source.source_revision[:16],
            max_steps=self.manifest.profile.budget.max_steps,
            memory_recall_limit=min(
                condition.memory_configuration.context_budget.total_items,
                100,
            ),
            objective=(
                "Finish the simulation with uptime >=99%; minimize total infrastructure cost "
                "conditional on SLO success."
            ),
        )

    async def run(self) -> V2Report:
        training = [block for block in self.manifest.run_matrix if block.phase == "training"]
        evaluation = [block for block in self.manifest.run_matrix if block.phase == "evaluation"]
        conditions = {item.condition_id: item for item in self.manifest.profile.conditions}
        prepare = getattr(self.memory_factory, "prepare", None)
        if callable(prepare):
            await _maybe_await(prepare())
        for block in training:
            for condition_id in block.conditions:
                await self._run_bounded_cell(block, conditions[condition_id], binding=None)

        first = self.journal.reduce_attempts()
        for condition in self.manifest.profile.conditions:
            training_attempts = tuple(
                item
                for item in first
                if item.phase == "training"
                and item.condition_id == condition.condition_id
                and item.attempt_index == 0
                and item.status in {"completed", "failed", "interrupted", "excluded"}
            )
            try:
                binding = await _maybe_await(self.binding_factory(condition, training_attempts))
                if not isinstance(binding, FrozenEvaluationBinding):
                    raise TypeError("binding factory must return FrozenEvaluationBinding")
                if binding.manifest_hash != self.manifest.manifest_hash:
                    raise ValueError("binding does not match sealed manifest")
                # Persist the exact frozen input before the first evaluation
                # request.  A crash after this point must leave enough evidence
                # to distinguish a bound evaluation from an unbound one.
                self.journal.artifacts.put(
                    "binding",
                    binding.binding_id,
                    binding.model_dump(mode="json"),
                )
                self.bindings.append(binding)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Evaluation cells are retained as explicit startup failures
                # below; a failed freeze must not trigger API calls.
                self.journal.artifacts.put(
                    "binding-error",
                    f"{self.manifest.manifest_id}:{condition.condition_id}",
                    {
                        "condition_id": condition.condition_id,
                        "error": _failure_reason("binding", error),
                    },
                )
                continue

        for block in evaluation:
            for condition_id in block.conditions:
                condition_binding = next(
                    (item for item in self.bindings if item.condition_id == condition_id), None
                )
                await self._run_bounded_cell(
                    block, conditions[condition_id], binding=condition_binding
                )
        return aggregate_report(
            self.manifest,
            self.journal.reduce_attempts(),
            frozen_bindings=tuple(self.bindings),
        )

    async def _run_bounded_cell(
        self,
        block: V2RunMatrixBlock,
        condition: V2Condition,
        *,
        binding: FrozenEvaluationBinding | None,
    ) -> None:
        timeout = self.manifest.profile.budget.max_wall_seconds
        if timeout is None:
            await self._run_cell(block, condition, binding=binding)
            return
        deadline_expired = asyncio.Event()
        cell = asyncio.create_task(
            self._run_cell(
                block,
                condition,
                binding=binding,
                deadline_expired=deadline_expired,
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(cell), timeout)
        except TimeoutError:
            deadline_expired.set()
            cell.cancel()
            with suppress(asyncio.CancelledError):
                await cell
            logical_id = _stable_run_identifier(
                self.manifest.manifest_hash,
                block_id=block.block_id,
                condition_id=condition.condition_id,
            )
            attempt_id = f"{logical_id}:attempt-0"
            current = next(
                (
                    attempt
                    for attempt in reversed(self.journal.reduce_attempts())
                    if attempt.attempt_id == attempt_id
                ),
                None,
            )
            if current is not None and current.status in {"requested", "running"}:
                await self._terminal(
                    current,
                    status="interrupted",
                    failure_stage="execution",
                    failure_class="interrupted",
                    failure_reason="per-attempt wall time budget exceeded",
                )
        except asyncio.CancelledError:
            cell.cancel()
            with suppress(asyncio.CancelledError):
                await cell
            raise

    async def _run_cell(
        self,
        block: V2RunMatrixBlock,
        condition: V2Condition,
        *,
        binding: FrozenEvaluationBinding | None,
        deadline_expired: asyncio.Event | None = None,
    ) -> None:
        requested_at = datetime.now(UTC)
        logical_id = _stable_run_identifier(
            self.manifest.manifest_hash,
            block_id=block.block_id,
            condition_id=condition.condition_id,
        )
        attempt_id = f"{logical_id}:attempt-0"
        requested = V2AttemptRecord(
            manifest_id=self.manifest.manifest_id,
            attempt_id=attempt_id,
            logical_run_id=logical_id,
            block_id=block.block_id,
            phase=block.phase,
            condition_id=condition.condition_id,
            environment_id=block.environment_id,
            scenario_id=block.scenario_id,
            world_seed=block.world_seed,
            replicate_index=block.replicate_index,
            status="requested",
            requested_at=requested_at,
            frozen_binding_id=binding.binding_id if binding else None,
        )
        self.journal.append(requested)
        if block.phase == "evaluation" and binding is None:
            await self._terminal(
                requested,
                status="failed",
                failure_stage="startup",
                failure_class="validation",
                failure_reason="evaluation binding was not frozen before the cell",
            )
            return
        config: AgentConfig | None = None
        environment: Environment | None = None
        observer = _TraceObserver()
        try:
            config = await _maybe_await(self.config_factory(block, condition, requested))
            environment = await _maybe_await(self.environment_factory(block, condition, requested))
            session, latest = await environment.start(
                seed=block.world_seed,
                agent_id=config.agent_id,
                agent_version=config.agent_version,
            )
        except asyncio.CancelledError:
            trace_hash = _try_trace_artifact(self.journal, requested.attempt_id, observer, None)
            await self._terminal(
                requested,
                status="interrupted",
                failure_stage="startup",
                failure_class="interrupted",
                failure_reason=(
                    "per-attempt wall time budget exceeded"
                    if deadline_expired is not None and deadline_expired.is_set()
                    else "evaluation task cancelled"
                ),
                trace_hash=trace_hash,
            )
            await _close_resource(environment)
            raise
        except Exception as error:
            await self._terminal(
                requested,
                status="failed",
                failure_stage="startup",
                failure_class=_failure_class(error),
                failure_reason=_failure_reason("startup", error),
            )
            await _close_resource(environment)
            return

        run_id = getattr(session, "run_id", None)
        if not isinstance(run_id, str) or not run_id:
            await self._terminal(
                requested,
                status="failed",
                failure_stage="startup",
                failure_class="validation",
                failure_reason="startup returned no physical run ID",
            )
            await _close_resource(environment)
            return
        started_at = datetime.now(UTC)
        running = requested.model_copy(
            update={"status": "running", "run_id": run_id, "started_at": started_at}
        )
        self.journal.append(running)
        model: DecisionModel | None = None
        telemetry_model: _TelemetryModelAdapter | None = None
        memory_adapter: _MemoryAdapter | None = None
        try:
            model = await _maybe_await(self.model_factory(block, condition, running, run_id))
            telemetry_model = _TelemetryModelAdapter(model)
            memory = await _maybe_await(
                self.memory_factory(block, condition, running, run_id, block.phase, binding)
            )
            metadata = getattr(self.memory_factory, "memory_metadata", None)
            if callable(metadata):
                values = metadata(condition, running, block.phase)
                if not isinstance(values, Mapping):
                    raise TypeError("memory metadata factory must return a mapping")
                running = running.model_copy(
                    update={
                        key: value
                        for key, value in values.items()
                        if key in {"memory_namespace", "audit_namespace"} and isinstance(value, str)
                    }
                )
            memory_adapter = _MemoryAdapter(memory)
            runner = self.runner_factory(
                config=config,
                model=telemetry_model,
                memory=memory_adapter,
                environment=_PrestartedEnvironment(
                    environment,
                    session,
                    latest,
                    environment_id=block.environment_id,
                    scenario_id=block.scenario_id,
                ),
                observer=observer,
            )
            result = await runner.run(block.world_seed)
            await self._refresh_stored_artifact_count(memory_adapter, condition, running)
            result_hash = self.journal.artifacts.put(
                "run_result", running.attempt_id, result.model_dump(mode="json")
            )
            trace_hash = self.journal.artifacts.put(
                "trace", running.attempt_id, _trace_payload(observer, model)
            )
            outcome = _outcome(result)
            terminal_status: Literal["completed", "failed", "interrupted"]
            if result.status == "completed":
                terminal_status = "completed"
            elif result.status == "running":
                terminal_status = "interrupted"
            else:
                terminal_status = "failed" if result.status == "failed" else "interrupted"
            if terminal_status == "completed":
                await self._terminal(
                    running,
                    status="completed",
                    finished_at=datetime.now(UTC),
                    outcome=outcome,
                    result_hash=result_hash,
                    trace_hash=trace_hash,
                    provider_telemetry=_provider_telemetry(telemetry_model, model),
                    memory_telemetry=_memory_telemetry(memory_adapter, binding),
                )
            else:
                await self._terminal(
                    running,
                    status=terminal_status,
                    finished_at=datetime.now(UTC),
                    outcome=outcome,
                    failure_stage="execution",
                    failure_class="interrupted"
                    if terminal_status == "interrupted"
                    else "permanent",
                    failure_reason=(
                        "run returned running; incomplete SLO evidence is unsuccessful"
                        if result.status == "running"
                        else f"run returned status {result.status}"
                    ),
                    result_hash=result_hash,
                    trace_hash=trace_hash,
                    provider_telemetry=_provider_telemetry(telemetry_model, model),
                    memory_telemetry=_memory_telemetry(memory_adapter, binding),
                )
        except asyncio.CancelledError:
            trace_hash = _try_trace_artifact(self.journal, running.attempt_id, observer, model)
            if memory_adapter is not None:
                with suppress(asyncio.CancelledError):
                    await self._refresh_stored_artifact_count(memory_adapter, condition, running)
            await self._terminal(
                running,
                status="interrupted",
                finished_at=datetime.now(UTC),
                failure_stage="execution",
                failure_class="interrupted",
                failure_reason=(
                    "per-attempt wall time budget exceeded"
                    if deadline_expired is not None and deadline_expired.is_set()
                    else "evaluation task cancelled"
                ),
                trace_hash=trace_hash,
                provider_telemetry=_provider_telemetry(telemetry_model, model),
                memory_telemetry=_memory_telemetry(memory_adapter, binding),
            )
            raise
        except _FinalizationError as error:
            trace_hash = _try_trace_artifact(self.journal, running.attempt_id, observer, model)
            if memory_adapter is not None:
                await self._refresh_stored_artifact_count(memory_adapter, condition, running)
            await self._terminal(
                running,
                status="failed",
                finished_at=datetime.now(UTC),
                failure_stage="finalization",
                failure_class="permanent",
                failure_reason=_failure_reason("finalization", error),
                trace_hash=trace_hash,
                provider_telemetry=_provider_telemetry(telemetry_model, model),
                memory_telemetry=_memory_telemetry(memory_adapter, binding),
            )
        except Exception as error:
            trace_hash = _try_trace_artifact(self.journal, running.attempt_id, observer, model)
            if memory_adapter is not None:
                await self._refresh_stored_artifact_count(memory_adapter, condition, running)
            await self._terminal(
                running,
                status="failed",
                finished_at=datetime.now(UTC),
                failure_stage="execution",
                failure_class=_failure_class(error),
                failure_reason=_failure_reason("execution", error),
                trace_hash=trace_hash,
                provider_telemetry=_provider_telemetry(telemetry_model, model),
                memory_telemetry=_memory_telemetry(memory_adapter, binding),
            )
        finally:
            await _close_resource(model)
            await _close_resource(environment)

    async def _refresh_stored_artifact_count(
        self,
        memory: _MemoryAdapter,
        condition: V2Condition,
        attempt: V2AttemptRecord,
    ) -> None:
        counter = getattr(self.memory_factory, "stored_artifact_count", None)
        if not callable(counter):
            return
        try:
            async with asyncio.timeout(_STORED_ARTIFACT_COUNT_TIMEOUT_SECONDS):
                value = await _maybe_await(counter(condition, attempt, attempt.phase))
        except Exception:
            return
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            memory.stored_artifacts = value

    async def _terminal(self, base: V2AttemptRecord, **updates: object) -> None:
        status = updates.pop("status")
        finished_at = updates.pop("finished_at", datetime.now(UTC))
        terminal = base.model_copy(update={"status": status, "finished_at": finished_at, **updates})
        self.journal.append(terminal)


async def _maybe_await(value: T | Awaitable[T]) -> T:  # noqa: UP047
    if inspect.isawaitable(value):
        return await value
    return value


def _failure_reason(stage: str, error: BaseException) -> str:
    detail = redact_text(str(error))[:1_500]
    return f"{stage} failed: {type(error).__name__}{': ' + detail if detail else ''}"


async def _close_resource(resource: object | None) -> None:
    if resource is None:
        return
    closer = getattr(resource, "aclose", None)
    if not callable(closer):
        closer = getattr(resource, "close", None)
    if not callable(closer):
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception:
        return


def _failure_class(
    error: BaseException,
) -> Literal["validation", "transient", "permanent", "interrupted", "excluded"]:
    if isinstance(error, (ValueError, TypeError)):
        return "validation"
    return "permanent"
