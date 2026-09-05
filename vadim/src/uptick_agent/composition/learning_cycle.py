"""Concrete wiring for the controlled experience-to-memory cycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from uptick_agent.benchmarks.incidents import (
    DEFAULT_REPAIR_MAPPING,
    ControlledIncidentEnvironment,
    evaluation_case_for_seed,
    fixture_spec,
    training_case_for_seed,
    validate_mapping,
)
from uptick_agent.composition.memory import compose_experimental_runtime
from uptick_agent.decisions.contracts import DecisionContext, NextStep
from uptick_agent.evaluation.contracts import V2SnapshotRef
from uptick_agent.evaluation.learning_cycle import (
    AttemptEvidence,
    LearningCycleJournal,
    LearningCycleManifest,
    LearningCycleReport,
    content_hash,
)
from uptick_agent.evaluation.snapshots import EvaluationMemoryFacade, SnapshotReadStore
from uptick_agent.memory.config import ContextBudgetConfig, MemoryConfiguration, ModuleConfig
from uptick_agent.memory.lesson_contracts import LessonRunDeclaration
from uptick_agent.memory.patterns import PatternQuerySettings
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.ports import DecisionModel
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.execute import AgentRunner
from uptick_agent.runs.results import RunResult, StepRecord
from uptick_agent.stage0 import sha256_file


class LearningModelFactory(Protocol):
    def __call__(self, phase: str, condition_id: str, seed: int) -> DecisionModel: ...


def hypothesis_configuration() -> MemoryConfiguration:
    """Resolve the only training memory condition used by this fixture."""

    return MemoryConfiguration(
        profile_id="controlled-world-hypothesis",
        profile_kind="experiment",
        compatibility_legacy=ModuleConfig(enabled=False),
        episodic=ModuleConfig(
            enabled=True,
            version="1.0",
            max_context_items=0,
            max_context_tokens=0,
        ),
        lessons=ModuleConfig(enabled=False),
        world_model=ModuleConfig(
            enabled=True,
            version="1.0",
            max_context_items=4,
            max_context_tokens=64_000,
        ),
        world_query_settings=PatternQuerySettings(
            scope_paths=("observation.data.incident_code",),
            action_path="action.message",
            result_path="result.data.recovered",
        ),
        context_budget=ContextBudgetConfig(total_items=4, total_tokens=64_000),
    )


def no_memory_configuration() -> MemoryConfiguration:
    return MemoryConfiguration(
        profile_id="controlled-no-memory",
        profile_kind="experiment",
        compatibility_legacy=ModuleConfig(enabled=False),
        context_budget=ContextBudgetConfig(total_items=0, total_tokens=0),
    )


class _RecordedModel:
    """Capture the exact request boundary alongside the normal model call."""

    def __init__(
        self,
        model: DecisionModel,
        *,
        on_record: Callable[[dict[str, object]], None],
    ) -> None:
        self._model = model
        self._on_record = on_record
        self.records: list[dict[str, object]] = []

    @property
    def last_telemetry(self) -> object:
        return getattr(self._model, "last_telemetry", None)

    async def decide(self, context: DecisionContext) -> NextStep:
        builder = getattr(self._model, "prompt_trace", None)
        request = (
            builder(context) if callable(builder) else {"context": context.model_dump(mode="json")}
        )
        record: dict[str, object] = {
            "status": "requested",
            "context": context.model_dump(mode="json"),
            "request": request,
        }
        self.records.append(record)
        self._on_record(record)
        try:
            decision = await self._model.decide(context)
        except Exception as error:
            record["error"] = f"{type(error).__name__}: {error}"
            record["status"] = "failed"
            self._on_record(record)
            raise
        record["decision"] = decision.model_dump(mode="json")
        record["status"] = "completed"
        self._on_record(record)
        return decision

    def prompt_trace(self, context: DecisionContext) -> dict[str, object]:
        builder = getattr(self._model, "prompt_trace", None)
        if callable(builder):
            value = builder(context)
            if isinstance(value, dict):
                return value
        return {"context": context.model_dump(mode="json")}

    async def aclose(self) -> None:
        close = getattr(self._model, "aclose", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result


class _Observer:
    def __init__(self) -> None:
        self.steps: list[StepRecord] = []

    async def on_step(self, record: StepRecord) -> None:
        self.steps.append(record.model_copy(deep=True))

    async def on_finish(self, result: RunResult) -> None:
        return None


def _content_hashes(manifest: LearningCycleManifest, code: str, variant: str) -> tuple[str, str]:
    environment_hash = content_hash(
        {"fixture_spec_hash": manifest.fixture_spec_hash, "adapter_hash": manifest.adapter_hash}
    )
    scenario_hash = content_hash(
        {"fixture_spec_hash": manifest.fixture_spec_hash, "code": code, "variant": variant}
    )
    return environment_hash, scenario_hash


def _declaration(
    manifest: LearningCycleManifest,
    *,
    run_id: str,
    logical_run_id: str,
    code: str,
    variant: str,
    phase: str,
) -> LessonRunDeclaration:
    environment_hash, scenario_hash = _content_hashes(manifest, code, variant)
    return LessonRunDeclaration(
        run_id=run_id,
        logical_run_id=logical_run_id,
        phase="learning" if phase == "training" else "frozen_evaluation",
        environment_id="controlled-incident-fixture",
        scenario_id=f"{variant}:{code}",
        environment_content_hash=environment_hash,
        scenario_content_hash=scenario_hash,
        eligible=phase == "training",
    )


def _recovered(result: RunResult | None) -> bool | None:
    if result is None:
        return None
    for metric in result.objective_metrics:
        if metric.name == "incident_recovered" and metric.unit == "boolean":
            return metric.value == 1.0
    return None


def _session_run_id(environment: ControlledIncidentEnvironment | None) -> str | None:
    if environment is None or environment.last_session is None:
        return None
    return environment.last_session.run_id


async def _close_resources(
    model: _RecordedModel | None,
    environment: ControlledIncidentEnvironment | None,
) -> tuple[str, ...]:
    errors: list[str] = []
    for label, resource in (("model", model), ("environment", environment)):
        if resource is None:
            continue
        close = getattr(resource, "aclose", None)
        if not callable(close):
            continue
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception as error:
            errors.append(f"{label} close failed: {type(error).__name__}: {error}")
    return tuple(errors)


async def _run_attempt(
    *,
    journal: LearningCycleJournal,
    manifest: LearningCycleManifest,
    phase: str,
    condition_id: str,
    seed: int,
    variant: str,
    case: object,
    mapping: Mapping[str, str],
    run_id_suffix: str,
    memory: object,
    model_factory: LearningModelFactory,
    timeout_seconds: float,
) -> AttemptEvidence:
    attempt_id = f"{phase}:{condition_id}:{seed}:{variant}"
    row = journal.start(
        attempt_id=attempt_id,
        phase=phase,  # type: ignore[arg-type]
        condition_id=condition_id,
        seed=seed,
        variant=variant,
    )
    model: _RecordedModel | None = None
    environment: ControlledIncidentEnvironment | None = None
    observer = _Observer()
    finish_updates: dict[str, object]
    cancelled: asyncio.CancelledError | None = None
    try:
        model = _RecordedModel(
            model_factory(phase, condition_id, seed),
            on_record=lambda record: journal.record_prompt(row.attempt_id, record),
        )
        environment = ControlledIncidentEnvironment(
            case,
            mapping,
            run_id_suffix=run_id_suffix,  # type: ignore[arg-type]
        )
        config = AgentConfig(
            agent_id="controlled-incident-learning",
            agent_version="1.0",
            max_steps=(
                manifest.training_max_steps
                if phase == "training"
                else manifest.evaluation_max_steps
            ),
            memory_recall_limit=4,
            objective="Recover the incident through public evidence and typed remediation.",
        )
        runner = AgentRunner(
            config=config,
            model=model,
            memory=memory,
            environment=environment,
            observer=observer,
        )
        result = await asyncio.wait_for(runner.run(seed), timeout=timeout_seconds)
        actions = tuple(
            item["decision"]["action"]
            for item in model.records
            if isinstance(item.get("decision"), Mapping)
            and isinstance(item["decision"].get("action"), Mapping)
        )
        memory_item_ids = tuple(
            item_id
            for record in model.records
            for item_id in _context_item_ids(record.get("context"))
        )
        finish_updates = {
            "status": "completed" if result.status == "completed" else "failed",
            "run_id": result.run_id,
            "selected_actions": actions,
            "recovered": _recovered(result),
            "outcome_status": result.status,
            "memory_item_ids": tuple(dict.fromkeys(memory_item_ids)),
            "prompt_records": tuple(model.records),
            "step_records": tuple(item.model_dump(mode="json") for item in observer.steps),
        }
    except TimeoutError:
        finish_updates = {
            "status": "interrupted",
            "run_id": _session_run_id(environment),
            "selected_actions": _selected_actions(model.records if model else []),
            "memory_item_ids": _all_context_item_ids(model.records if model else []),
            "prompt_records": tuple(model.records if model else ()),
            "step_records": tuple(item.model_dump(mode="json") for item in observer.steps),
            "failure": f"per-attempt timeout exceeded ({timeout_seconds}s)",
        }
    except asyncio.CancelledError as error:
        cancelled = error
        finish_updates = {
            "status": "interrupted",
            "run_id": _session_run_id(environment),
            "selected_actions": _selected_actions(model.records if model else []),
            "memory_item_ids": _all_context_item_ids(model.records if model else []),
            "prompt_records": tuple(model.records if model else ()),
            "step_records": tuple(item.model_dump(mode="json") for item in observer.steps),
            "failure": "attempt cancelled",
        }
    except Exception as error:
        finish_updates = {
            "status": "failed",
            "run_id": _session_run_id(environment),
            "selected_actions": _selected_actions(model.records if model else []),
            "memory_item_ids": _all_context_item_ids(model.records if model else []),
            "prompt_records": tuple(model.records if model else ()),
            "step_records": tuple(item.model_dump(mode="json") for item in observer.steps),
            "failure": f"{type(error).__name__}: {error}",
        }

    try:
        cleanup_errors = await _close_resources(model, environment)
    except asyncio.CancelledError as error:
        finish_updates["status"] = "interrupted"
        finish_updates["failure"] = "attempt cancelled during cleanup"
        finish_updates["cleanup_errors"] = (f"cleanup cancelled: {type(error).__name__}: {error}",)
        journal.finish(row, **finish_updates)
        raise
    finish_updates["cleanup_errors"] = cleanup_errors
    finished = journal.finish(row, **finish_updates)
    if cancelled is not None:
        raise cancelled
    return finished


def _context_item_ids(context: object) -> tuple[str, ...]:
    if not isinstance(context, Mapping):
        return ()
    memory = context.get("memory_context")
    if not isinstance(memory, Mapping) or not isinstance(memory.get("items"), list):
        return ()
    result: list[str] = []
    for item in memory["items"]:
        if not isinstance(item, Mapping):
            continue
        envelope = item.get("envelope")
        if isinstance(envelope, Mapping) and isinstance(envelope.get("item_id"), str):
            result.append(envelope["item_id"])
    return tuple(result)


def _selected_actions(records: list[dict[str, object]]) -> tuple[dict[str, object], ...]:
    return tuple(
        item["decision"]["action"]
        for item in records
        if isinstance(item.get("decision"), Mapping)
        and isinstance(item["decision"].get("action"), Mapping)
    )


def _all_context_item_ids(records: list[dict[str, object]]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item_id for record in records for item_id in _context_item_ids(record.get("context"))
        )
    )


def _record_evaluation_setup_failures(
    journal: LearningCycleJournal,
    manifest: LearningCycleManifest,
    *,
    failure: str,
) -> None:
    """Retain every declared evaluation cell when shared setup cannot complete."""

    for case_row in manifest.evaluation_cases:
        seed = int(case_row["seed"])
        variant = str(case_row["variant"])
        for condition_id in manifest.conditions:
            journal.setup_failure(
                attempt_id=f"evaluation:{condition_id}:{seed}:{variant}",
                phase="evaluation",
                condition_id=condition_id,
                seed=seed,
                variant=variant,
                failure=failure,
            )


async def _freeze(
    store: SqliteStructuredStore,
    *,
    base: str,
) -> tuple[V2SnapshotRef, ...]:
    refs: list[V2SnapshotRef] = []
    for namespace in (base, f"{base}:world", f"{base}:lessons:declarations"):
        snapshot_id = f"{namespace}:learning-freeze"
        receipt = await store.create_snapshot(
            namespace=namespace,
            snapshot_id=snapshot_id,
            operation="learning-cycle-freeze",
            idempotency_key=f"{snapshot_id}:create",
        )
        refs.append(
            V2SnapshotRef(
                namespace=namespace,
                snapshot_id=snapshot_id,
                content_hash=receipt.snapshot.content_hash,
            )
        )
    return tuple(refs)


async def run_learning_cycle(
    manifest: LearningCycleManifest,
    *,
    output: Path,
    sqlite_path: Path,
    model_factory: LearningModelFactory,
    mapping: Mapping[str, str] = DEFAULT_REPAIR_MAPPING,
) -> LearningCycleReport:
    """Run eight training attempts, freeze, reopen, and run paired evaluation."""

    validate_mapping(mapping)
    manifest.verify()
    if manifest.fixture_spec_hash != content_hash(fixture_spec(mapping)):
        raise ValueError("supplied fixture mapping does not match the sealed fixture hash")
    adapter_path = Path(__file__).resolve().parents[1] / "benchmarks" / "incidents.py"
    if manifest.adapter_hash != sha256_file(adapter_path):
        raise ValueError("controlled incident adapter hash does not match the sealed manifest")
    expected_training = tuple(
        {
            "seed": seed,
            "incident_code": training_case_for_seed(seed).incident_code,
            "variant": training_case_for_seed(seed).variant,
        }
        for seed in manifest.training_seeds
    )
    if manifest.training_cases != expected_training:
        raise ValueError("manifest training case order or content changed")
    expected_evaluation = tuple(
        {
            "seed": 101 + index,
            "incident_code": evaluation_case_for_seed(101 + index).incident_code,
            "variant": evaluation_case_for_seed(101 + index).variant,
        }
        for index in range(8)
    )
    if manifest.evaluation_cases != expected_evaluation:
        raise ValueError("manifest evaluation case order or content changed")
    expected_configs = {
        "none": no_memory_configuration(),
        "hypothesis": hypothesis_configuration(),
    }
    if set(manifest.conditions) != set(expected_configs):
        raise ValueError("manifest condition set changed")
    for condition_id, config in expected_configs.items():
        declaration = manifest.memory_configurations.get(condition_id)
        if not isinstance(declaration, Mapping):
            raise ValueError(f"manifest missing memory declaration for {condition_id}")
        expected_declaration = {
            **config.model_dump(mode="json"),
            "fingerprint": config.fingerprint,
        }
        if dict(declaration) != expected_declaration:
            raise ValueError(f"manifest memory configuration changed for {condition_id}")
    if sqlite_path.exists():
        raise ValueError("learning-cycle SQLite path must be fresh")
    journal = LearningCycleJournal(output, manifest)
    store = SqliteStructuredStore(sqlite_path)
    config = expected_configs["hypothesis"]
    base = f"learning:{manifest.manifest_hash[:32]}:hypothesis"
    training_rows: list[AttemptEvidence] = []

    for case_row in manifest.training_cases:
        seed = int(case_row["seed"])
        case = training_case_for_seed(seed)
        run_id = f"controlled:{case.variant}:{case.incident_code}:{seed}"
        declaration = _declaration(
            manifest,
            run_id=run_id,
            logical_run_id=run_id,
            code=case.incident_code,
            variant=case.variant,
            phase="training",
        )
        try:
            memory = compose_experimental_runtime(
                config,
                store,
                namespace=base,
                condition_id="hypothesis",
                run_declarations=(declaration,),
            )
        except Exception as error:
            training_rows.append(
                journal.setup_failure(
                    attempt_id=f"training:hypothesis:{seed}:{case.variant}",
                    phase="training",
                    condition_id="hypothesis",
                    seed=seed,
                    variant=case.variant,
                    failure=f"memory setup: {type(error).__name__}: {error}",
                )
            )
            continue
        training_rows.append(
            await _run_attempt(
                journal=journal,
                manifest=manifest,
                phase="training",
                condition_id="hypothesis",
                seed=seed,
                variant=case.variant,
                case=case,
                mapping=mapping,
                run_id_suffix="",
                memory=memory,
                model_factory=model_factory,
                timeout_seconds=manifest.training_timeout_seconds,
            )
        )

    try:
        refs = await _freeze(store, base=base)
        frozen_bindings = {
            "hypothesis": {
                "condition_id": "hypothesis",
                "snapshot_refs": [ref.model_dump(mode="json") for ref in refs],
                "training_attempt_ids": [row.attempt_id for row in training_rows],
                "source": "observed transitions and runner outcomes",
            },
        }
        journal.write_binding("hypothesis", frozen_bindings["hypothesis"])
    except Exception as error:
        failure = f"frozen evaluation setup: {type(error).__name__}: {error}"
        _record_evaluation_setup_failures(journal, manifest, failure=failure)
        return journal.report(frozen_bindings={}, reopened_before_evaluation=False)
    # SQLite uses one short-lived connection per operation; replacing the
    # store object after snapshots is the explicit close/reopen boundary.
    store = SqliteStructuredStore(sqlite_path)
    reopened = True
    none_config = no_memory_configuration()
    for case_row in manifest.evaluation_cases:
        seed = int(case_row["seed"])
        case = evaluation_case_for_seed(seed)
        for condition_id in manifest.conditions:
            run_id = f"controlled:{case.variant}:{case.incident_code}:{seed}:{condition_id}"
            declaration = _declaration(
                manifest,
                run_id=run_id,
                logical_run_id=run_id,
                code=case.incident_code,
                variant=case.variant,
                phase="evaluation",
            )
            if condition_id == "none":
                try:
                    memory = compose_experimental_runtime(
                        none_config,
                        store,
                        namespace=f"{base}:evaluation:{seed}:none",
                        condition_id="none",
                    )
                except Exception as error:
                    journal.setup_failure(
                        attempt_id=f"evaluation:none:{seed}:{case.variant}",
                        phase="evaluation",
                        condition_id="none",
                        seed=seed,
                        variant=case.variant,
                        failure=f"memory setup: {type(error).__name__}: {error}",
                    )
                    continue
            else:
                try:
                    read_store = SnapshotReadStore(store, refs)
                    await read_store.load()
                    read_runtime = compose_experimental_runtime(
                        config,
                        read_store,
                        namespace=base,
                        condition_id="hypothesis",
                        run_declarations=(declaration,),
                    )
                    write_runtime = compose_experimental_runtime(
                        config,
                        store,
                        namespace=f"{base}:evaluation:{seed}:hypothesis",
                        condition_id="hypothesis",
                        run_declarations=(declaration,),
                    )
                    memory = EvaluationMemoryFacade(
                        read_runtime,
                        write_runtime,
                        frozen_snapshot_members=read_store.member_count,
                    )
                except Exception as error:
                    journal.setup_failure(
                        attempt_id=f"evaluation:hypothesis:{seed}:{case.variant}",
                        phase="evaluation",
                        condition_id="hypothesis",
                        seed=seed,
                        variant=case.variant,
                        failure=f"memory setup: {type(error).__name__}: {error}",
                    )
                    continue
            await _run_attempt(
                journal=journal,
                manifest=manifest,
                phase="evaluation",
                condition_id=condition_id,
                seed=seed,
                variant=case.variant,
                case=case,
                mapping=mapping,
                run_id_suffix=condition_id,
                memory=memory,
                model_factory=model_factory,
                timeout_seconds=manifest.evaluation_timeout_seconds,
            )
    return journal.report(
        frozen_bindings=frozen_bindings,
        reopened_before_evaluation=reopened,
    )


__all__ = [
    "LearningModelFactory",
    "hypothesis_configuration",
    "no_memory_configuration",
    "run_learning_cycle",
]
